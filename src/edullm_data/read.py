"""dataset_paths() — the reader (§9).

The convenient way to fill a training config from a published dataset, and — critically —
the way that returns the *correct dtype*. OLMo-core's ``NumpyFSLDataset`` defaults to
``uint16`` while these corpora are ``uint32``; inferring dtype silently halves the token
count. So the reader reads it from the manifest and hands it back, and a gate nobody routes
through is not a gate — this must be the path of least resistance, or people paste raw globs.

It refuses a prefix that has not been validated: no ``_VALIDATED.json`` (or a legacy
``_SUCCESS``) means the dataset is not readable. With the airlock, unvalidated bytes can't
even be in ``edullm-data`` — the reader's refusal is belt-and-suspenders for the case where
someone points it at a landing prefix or a hand-assembled directory.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .contracts import SPLITS, is_trainable
from .manifest import ManifestEntry, parse_shard_name
from .s3 import S3, NotFound

DATA_BUCKET = "edullm-data"

#: dtype name -> numpy type CHARACTER, for building a byte-order-qualified dtype string.
#: Necessary because numpy accepts ``"<u4"`` but REJECTS ``"<uint32"`` — the long names carry
#: no order prefix. Verified: ``np.dtype("<uint32")`` raises "data type not understood".
#: A name absent from this map falls through unqualified rather than producing a string numpy
#: would reject.
_NUMPY_CHAR = {
    "uint8": "u1", "int8": "i1",
    "uint16": "u2", "int16": "i2",
    "uint32": "u4", "int32": "i4",
    "uint64": "u8", "int64": "i8",
    "float16": "f2", "float32": "f4", "float64": "f8",
}


class ReadError(RuntimeError):
    """Cannot resolve the requested dataset/split for reading."""


class NotValidated(ReadError):
    """The prefix carries no validation marker — refuse to read (§9)."""


class SealMismatch(ReadError):
    """The dataset's bytes do not match the seal written when it was validated.

    Distinct from :class:`NotValidated`: the marker IS there, and it disagrees with what is on
    the shelf. That is a stronger signal than absence — something changed a frozen dataset.
    """


class PartialLabelCoverage(UserWarning):
    """A ``labels=`` filter's key is absent from some entries, so they were dropped unasked.

    A WARNING and not an error, and the line between the two is which failure it is:

    * The dataset carries NO labels at all -> :class:`ReadError`. The result would be empty, and
      an empty result is indistinguishable from "your filter matched nothing" — the caller
      cannot tell a broken query from a true negative, so there is no honest value to return.
    * The key exists on SOME entries -> this warning. The result is non-empty and exactly what
      was asked for; what is wrong is the caller's mental model of what they asked for.
      ``labels={"domain": "science"}`` against a corpus where only three sources inherited a
      ``domain`` returns a real science slice — of three sources, silently excluding the rest.

    Raising on the partial case would break a legitimate use: wanting only the labelled sources
    is a reasonable request, and it is not expressible any other way. So the read succeeds and
    says what it left out.

    Its own class so that a caller who wants the strict reading can get it in one line —
    ``warnings.simplefilter("error", PartialLabelCoverage)`` in a training entrypoint makes
    every silent drop fatal, without also promoting unrelated ``UserWarning``s. That is the
    right shape for "the default is permissive, the strict mode is opt-in and cheap."
    """


class MixedFormat(ReadError):
    """A group's fixed-width shards do not agree on how to decode them.

    RAISED rather than reported as a ``dtype=None`` / ``"mixed"`` value, which is what this
    used to do. The reason is that there is nothing a caller can *do* with the softer answer:
    ``ResolvedSplit`` hands back ONE dtype for the whole group because a loader memmaps the
    shards as one array, so "these shards are uint16 and those are uint32" has no valid
    resolution — a caller receiving it must either raise itself or pick one, and picking one is
    precisely the silent-halving bug this module exists to prevent (§5, and the module
    docstring: OLMo-core's ``uint16`` default). ``dtype=None`` was the worst of both: it looks
    like the legitimate "this container carries its own typing" answer, so it flows into a
    loader and gets defaulted.

    The recourse is structural and already in the standard: put the differently-typed shards in
    separate GROUPS and pass ``group=``. That is what groups are for, and it makes the choice
    visible in a training config instead of resolved by a coin flip.

    Note that a group whose entries ALL carry no dtype (parquet, jsonl, a tokenizer tree, a
    vendored directory) is not mixed and does not raise — ``dtype`` is legitimately ``None``
    there, because the container does its own typing.
    """


@dataclass
class ResolvedSplit:
    """What a trainer needs: the object URIs to read, the numpy dtype to read them as, and
    kwargs a loader may want (seq-agnostic here; the caller sets sequence_length). ``dtype``
    is a string like ``"uint32"`` — the reader does not import numpy so the package stays
    importable in a metadata-only environment; the caller maps it to ``np.dtype``."""

    dataset_id: str
    version: str
    split: str
    paths: list[str]  # full s3:// URIs
    dtype: str | None
    rows: int | None
    kwargs: dict[str, Any]
    #: ``"little"`` / ``"big"``, or ``None`` for a container that carries its own typing.
    #:
    #: Carried because the manifest declares it and DISCARDING it made the reader lossy in the
    #: one way that silently corrupts data: ``np.memmap(path, dtype="uint32")`` uses the HOST's
    #: byte order, so a big-endian shard read on a little-endian host (or the reverse) decodes
    #: every token to a different, in-range-looking id. Nothing downstream notices — the token
    #: count is right, the ids are plausible, the loss curve is merely bad. ``dtype`` alone is
    #: not enough to read the bytes correctly; see :attr:`numpy_dtype`.
    byte_order: str | None = None
    #: Leading bytes to skip before the first element (0 for the headerless ``.u32le.bin``
    #: form). A ``.npy``-style header is nonzero, and a reader that memmaps from offset 0
    #: decodes the header AS DATA — the exact ".npy lie" the standard was written against. A
    #: loader must honour this; it is not decoration.
    header_bytes: int = 0
    #: Every declared split, keyed by name — ``{"train": [...uris], "val": [...uris]}``.
    #:
    #: A dataset returns BOTH by default, which is what a run actually needs (train for the
    #: dataset config, val for the eval callback). They are kept SEPARATE rather than
    #: concatenated into ``paths`` because a flat list is precisely the bug: a caller cannot
    #: tell the two apart, so held-out shards end up in training with nothing to notice.
    splits: dict[str, list[str]] = field(default_factory=dict)
    #: Per-split declared row counts, same keys as ``splits``.
    split_rows: dict[str, int | None] = field(default_factory=dict)

    @property
    def train(self) -> list[str]:
        """The trainable URIs. Empty if this dataset declares no trainable split."""
        return [p for name, ps in self.splits.items() if is_trainable(name) for p in ps]

    @property
    def val(self) -> list[str] | None:
        """Held-out URIs, or ``None`` when the dataset has none.

        ``None`` rather than ``[]`` on purpose: "this dataset has no validation data" and "the
        validation split is empty" are different facts, and a caller that wants to branch on
        the first should not have to guess. Never raises — asking is not an error.
        """
        held = [p for name, ps in self.splits.items() if not is_trainable(name) for p in ps]
        return held or None

    def has_split(self, name: str) -> bool:
        return name in self.splits

    @property
    def numpy_dtype(self) -> str | None:
        """``dtype`` and ``byte_order`` combined into a numpy dtype string — ``"<u4"``, ``">u4"``.

        The whole point of carrying ``byte_order`` is that it must reach the loader, and a
        caller doing ``np.dtype(r.dtype)`` gets NATIVE order, which is only accidentally
        correct. This is the string that is correct on any host: pass it straight to
        ``np.dtype`` / ``np.memmap(..., dtype=...)``.

        ``None`` when there is no fixed-width dtype (a container that types itself). Falls back
        to the bare dtype name when the manifest declared no byte order, which is honest —
        that dataset genuinely does not say, so native is the only available reading.
        """
        return _numpy_dtype_of(self.dtype, self.byte_order)


def _numpy_dtype_of(dtype: str | None, byte_order: str | None) -> str | None:
    """``("uint32", "little")`` -> ``"<u4"``. Shared by ResolvedSplit and ResolvedMixture so the
    two cannot drift on the one field a loader must not get wrong."""
    if dtype is None:
        return None
    prefix = {"little": "<", "big": ">"}.get(byte_order or "")
    if not prefix:
        return dtype
    return prefix + _NUMPY_CHAR.get(dtype, dtype)


def _choose_group(
    groups: list[Any], group: str | None, dataset_id: str, version: str
) -> Mapping[str, Any]:
    """The one payload group to read, or a ReadError explaining the choice the caller must make."""
    if not groups:
        raise ReadError(f"{dataset_id}/{version} declares no groups")
    if group is not None:
        chosen = next((g for g in groups if g.get("name") == group), None)
        if chosen is None:
            raise ReadError(
                f"group {group!r} not found; groups are {[g.get('name') for g in groups]}"
            )
        return chosen
    if len(groups) == 1:
        return groups[0]
    raise ReadError(
        f"{dataset_id}/{version} has {len(groups)} groups "
        f"{[g.get('name') for g in groups]}; pass group= to choose one"
    )


def _load_json(s3: S3, bucket: str, key: str) -> Any:
    import json

    return json.loads(s3.get(bucket, key).decode("utf-8"))


def _require_validated(s3: S3, bucket: str, prefix: str) -> None:
    for marker in ("_VALIDATED.json", "_SUCCESS"):
        try:
            s3.head(bucket, f"{prefix}/{marker}")
            return
        except NotFound:
            continue
    raise NotValidated(
        f"{bucket}/{prefix} has no _VALIDATED.json — refusing to read an unvalidated dataset (§9). "
        f"If this is a landing prefix, run the validator first; only edullm-data holds validated data."
    )


def dataset_paths(
    dataset_id: str,
    version: str,
    *,
    split: str | None = None,
    s3: S3,
    data_bucket: str = DATA_BUCKET,
    require_validated: bool = True,
    group: str | None = None,
    include_held_out: bool = False,
    labels: Mapping[str, str] | None = None,
    warn_partial_labels: bool = True,
) -> ResolvedSplit:
    """Resolve a dataset to concrete object URIs + dtype.

    ``group`` selects which payload group when a dataset has several; defaults to the single
    group if there is exactly one, else raises so the caller is explicit about what they read.

    ``labels`` narrows to the shards carrying EVERY given label key/value — the read-side use of
    ``entry.labels``, which Gate A recomputes from each object's own key so the label cannot
    drift from the file it describes. Keys are whatever the producer used
    (``{"source": …, "domain": …}`` for a pretrain corpus; another family will differ), so
    nothing here is hardcoded. ``rows`` and ``split_rows`` are then RECOMPUTED from the selected
    entries' counts: the partition's declared total describes a superset, and handing a trainer
    an inflated row count is the failure ``validate``'s ``partition-rows-mismatch`` exists to
    catch on the write side. Asking for labels on an unlabelled dataset raises rather than
    returning nothing, so "this dataset has no labels" is distinguishable from "nothing matched".

    ``warn_partial_labels`` (default on) covers the case between those two: a corpus where a
    label key exists on SOME entries. Label depth is per-entry and legitimately mixed — a
    ``domain`` segment is only present where the upstream source shipped one, so
    ``labels={"domain": …}`` matches only the nested sources and silently discards every flat
    one. That read is not wrong, and raising would break the legitimate "give me only the
    labelled sources" request, so it succeeds and emits
    :class:`PartialLabelCoverage` naming what it dropped. Pass ``False`` when the narrowing is
    deliberate; ``warnings.simplefilter("error", PartialLabelCoverage)`` is the opposite dial
    for a caller who wants it fatal.

    **``split=None`` returns TRAINABLE data only**, and every declared split separately in
    ``.splits`` / ``.train`` / ``.val``. Returning everything — which is what this used to do —
    hands a trainer its own held-out shards with no way to tell them apart. Silence means the
    safe subset.

    A dataset that declares no trainable split at all (a tokenizer, a vendored tree, an eval
    set) returns everything: there is nothing to protect, and the whole artifact is the payload.

    ``include_held_out=True`` opts back into the old behaviour for the rare deliberate case.
    It is spelled out so it shows up in a code review of a training config.

    Asking for a split the dataset does not have returns an EMPTY result rather than raising,
    so "does this have validation data?" is a question, not an exception. A split outside the
    vocabulary is still an error.
    """
    prefix = f"{dataset_id}/{version}"
    if require_validated:
        _require_validated(s3, data_bucket, prefix)
        # RECOMPUTE the seal, do not merely observe that it exists. A marker whose presence is
        # the only thing checked is the decoration this standard exists to remove: rooting the
        # hash chain buys nothing if no read path verifies it.
        #
        # This catches the tampering that matters most here — a rewritten dataset.json whose
        # train and val globs have been SWAPPED. The marker is present, every group manifest is
        # intact, and `split="train"` hands back the val shards. Two small GETs per group, no
        # payload bytes.
        #
        # A pre-root seal (written before dataset_sha256 existed) is unverifiable rather than
        # invalid, so it is reported and allowed through: refusing would make every
        # already-published dataset unreadable, which is the retroactive invalidation the
        # standard forbids.
        problems = [p for p in verify_seal(dataset_id, version, s3=s3, data_bucket=data_bucket)
                    if "no dataset_sha256" not in p]
        if problems:
            raise SealMismatch(
                f"{data_bucket}/{prefix} does not match its own seal — refusing to read:\n  "
                + "\n  ".join(problems)
                + "\nThe dataset was altered after it was validated. Do not train on it."
            )

    try:
        ds = _load_json(s3, data_bucket, f"{prefix}/dataset.json")
    except NotFound:
        raise ReadError(f"no dataset.json at {data_bucket}/{prefix}") from None

    chosen = _choose_group(ds.get("groups", []), group, dataset_id, version)
    gname = chosen["name"]
    manifest = _load_json(s3, data_bucket, f"{prefix}/{chosen.get('manifest', f'{gname}/manifest.json')}")
    entries = [ManifestEntry.from_dict(e) for e in manifest.get("entries", [])]

    # The FULL format triple, read from the manifest and never inferred (§5). dtype alone does
    # not let a caller read the bytes: byte_order decides how each element is assembled and
    # header_bytes decides where the elements start. Both were being dropped on the floor here
    # while the manifest declared them.
    dtype, byte_order, header_bytes = _resolve_format(entries, prefix, gname)

    def _uri(entry: Any) -> str:
        return f"s3://{data_bucket}/{prefix}/{entry.path}"

    # A labels= filter must not change how a PARTITION resolves. The glob matcher below is a
    # deliberate three-way mirror with validate._matches_glob and publish._count_rows_for_glob
    # ("the same order the reader uses, so a partition that resolves at validation resolves
    # identically at read time"). So the label filter is applied to _select's OUTPUT, never
    # inside it: partition resolution stays byte-identical to what Gate A verified, and the
    # filter is a narrowing on top.
    if labels:
        if not any(getattr(e, "labels", None) for e in entries):
            raise ReadError(
                f"labels={dict(labels)!r} was requested but no entry in group {gname!r} of "
                f"{dataset_id}/{version} carries any labels — this dataset is unlabelled (a flat "
                f"layout, or a manifest written before schema v2). Returning an empty result "
                f"would be indistinguishable from 'your filter matched nothing'."
            )
        if warn_partial_labels:
            _warn_partial_label_coverage(entries, labels, dataset_id, version, gname)

    def _label_filter(sel: list[Any]) -> list[Any]:
        return [e for e in sel if _matches_labels(e, labels)] if labels else sel

    def _select(part: Mapping[str, Any]) -> tuple[list[Any], dict[str, Any]]:
        """Entries belonging to one partition, plus any loader kwargs it implies."""
        if part.get("by") == "path":
            glob = part.get("glob", "")
            return _label_filter([
                e for e in entries
                if fnmatch.fnmatch(e.path.rsplit("/", 1)[-1], glob) or fnmatch.fnmatch(e.path, glob)
            ]), {}
        # field/range/indices: the split is a row predicate, not a file subset. Return all
        # shards plus the predicate so the loader applies it — never silently return the whole
        # set as if it were the split.
        return _label_filter(list(entries)), {
            "row_predicate": {k: part[k] for k in part if k not in {"name"}}
        }

    # Resolve EVERY declared split, always. This is what makes "a dataset returns train and
    # val" true without flattening them together.
    declared = [p for p in (chosen.get("partitions") or []) if isinstance(p, Mapping) and p.get("name")]
    splits: dict[str, list[str]] = {}
    split_rows: dict[str, int | None] = {}
    for part in declared:
        name = str(part["name"])
        sel, _ = _select(part)
        splits[name] = [_uri(e) for e in sel]
        # Under a label filter the partition's DECLARED rows describes a superset of what was
        # selected, so recompute from the selected entries' own counts. Unfiltered, keep the
        # declared value: it is what Gate A recomputed and sealed.
        split_rows[name] = _sum_counts(sel)[0] if labels else part.get("rows")

    rows: int | None = None
    kwargs: dict[str, Any] = {}
    if split is not None:
        part = _find_partition(chosen, split)
        if part is None:
            # Deliberately NOT an error when the dataset simply has no such split: asking "does
            # this have validation data?" must not require catching an exception. An unknown
            # word — one outside the vocabulary — is still a mistake worth reporting.
            if split in SPLITS:
                return ResolvedSplit(
                    dataset_id=dataset_id, version=version, split=split, paths=[], dtype=dtype,
                    rows=None, kwargs={}, splits=splits, split_rows=split_rows,
                    byte_order=byte_order, header_bytes=header_bytes,
                )
            raise ReadError(
                f"split {split!r} is not in the vocabulary {sorted(SPLITS)}; group {gname!r} "
                f"declares {sorted(splits)}"
            )
        selected, kwargs = _select(part)
        rows = _sum_counts(selected)[0] if labels else part.get("rows")
    else:
        # THE V9 FIX. This used to return every entry, so a caller who asked for no split in
        # particular was handed the held-out shards along with the training ones and had no way
        # to tell which was which — i.e. train on your own validation set, silently.
        #
        # Now: trainable data only. Silence means the SAFE subset, never "everything".
        trainable = [name for name in splits if is_trainable(name)]
        if trainable:
            if not include_held_out:
                selected = [e for name in trainable for e in _select(_find_partition(chosen, name))[0]]
            else:
                selected = _label_filter(list(entries))
        else:
            # No trainable split DECLARED — a tokenizer, a vendored tree, an eval set. There is
            # normally nothing to protect here, so the whole artifact is the payload.
            selected = _label_filter(list(entries))

        if not include_held_out:
            # RECOMPUTE from the bytes' own names, do not trust the declaration. Everything
            # above reasons from declared partition NAMES, which is a claim in dataset.json —
            # and every way that claim can be wrong (a val-only partition, an empty or malformed
            # partitions list, a partition with no name, a `by: field` partition that selects
            # every shard, a group whose val shards nobody declared) ends with held-out data
            # inside the trainable set.
            #
            # A filename that parses to a non-trainable split is dropped regardless of what was
            # declared. Names that do not parse as shards are kept, so tokenizer files and
            # vendored trees are unaffected.
            selected = [
                e for e in selected
                if (parsed := parse_shard_name(e.path)) is None
                or parsed[0] not in SPLITS
                or is_trainable(parsed[0])
            ]

    # An unsplit read has no partition to inherit `rows` from, so it has always reported None.
    # Under a label filter we can do better for free: the counts of exactly what was selected.
    if labels and rows is None:
        rows = _sum_counts(selected)[0]

    paths = [_uri(e) for e in selected]
    return ResolvedSplit(
        dataset_id=dataset_id,
        version=version,
        split=split or "*",
        paths=paths,
        dtype=dtype,
        rows=rows,
        kwargs=kwargs,
        splits=splits,
        split_rows=split_rows,
        byte_order=byte_order,
        header_bytes=header_bytes,
    )


#: Count units whose values can be summed into a meaningful total. Mirrors
#: ``validate._COUNTABLE_UNITS``; ``bytes`` is excluded there and here because summing bytes
#: across shards answers a different question than summing rows or tokens.
_COUNTABLE_UNITS = frozenset({"tokens", "indices", "rows", "items"})


def _matches_labels(entry: Any, want: Mapping[str, str]) -> bool:
    """Whether one entry carries EVERY requested label key with the requested value.

    Exact match, AND across keys, no wildcards. A predicate language here would need a
    validator nobody has written — the same reasoning that keeps ``entry.labels`` flat and
    string-valued (``manifest.py``'s ``labels`` docstring).

    Deliberately generic: it never names ``source`` or ``domain``. Those are the *pretrain*
    convention (``manifest.PATH_LABEL_KEYS``); a curriculum or sft dataset will label by
    something else entirely, and this must work for whatever the next family invents.
    """
    have = getattr(entry, "labels", None) or {}
    return all(have.get(k) == v for k, v in want.items())


#: Label keys tried, in order, when naming which slices a partial-coverage filter dropped.
#: ``source`` first because that is what the pretrain convention calls the top level
#: (``manifest.PATH_LABEL_KEYS``) and what an operator recognizes. Falls back to the whole
#: label dict for a family that labels by something else entirely — the message must stay
#: useful for keys this module has never heard of.
_EXCLUSION_NAME_KEYS: tuple[str, ...] = ("source", "corpus", "lang", "dataset")


def _slice_name(entry: Any) -> str:
    """A short human name for the slice an excluded entry belongs to.

    Named from the keys the entry DOES carry, including keys that are part of the query. That
    looks redundant and is not: under ``labels={"source": "arxiv", "domain": "python"}`` the
    excluded arxiv shards are the answer — "you asked for arxiv AND a domain, and arxiv has no
    domain at all, so you got none of it" is the message, and suppressing ``source=arxiv`` as
    "already in your query" reduces it to ``<unlabelled>``, which names nothing.
    """
    have = getattr(entry, "labels", None) or {}
    for key in _EXCLUSION_NAME_KEYS:
        if key in have:
            return f"{key}={have[key]}"
    if have:
        return ",".join(f"{k}={v}" for k, v in sorted(have.items()))
    return "<no labels>"


def _warn_partial_label_coverage(
    entries: list[Any],
    labels: Mapping[str, str],
    dataset_id: str,
    version: str,
    gname: str,
) -> None:
    """Warn when a requested label KEY is missing from some entries, and say what that cost.

    The gap this closes: ``_matches_labels`` requires every requested key to be present AND
    equal, and label depth is per-entry — Gate A's ``_check_labels_match_path`` has no
    uniformity requirement, so a group legitimately holds both ``tokens/dclm/train-*.bin`` and
    ``tokens/essential-web/science/train-*.bin``. Ask that group for
    ``labels={"domain": "science"}`` and DCLM does not fail to match on its value, it fails to
    match on the key's ABSENCE. Nothing else notices: the result is non-empty, the counts are
    internally consistent, and the caller believes they hold "the science slice" while holding
    three sources out of ten.

    So this reports the KEY-MISSING population only, not the value mismatches. A shard whose
    ``domain`` is ``medicine`` was correctly excluded by a question it could answer; a shard
    with no ``domain`` at all was excluded by a question that does not apply to it, which is
    the part the caller did not intend and cannot see.

    Counts come from ``entry.count`` where the entries agree on a summable unit, and are
    omitted rather than guessed when they do not — a wrong number in a warning is worse than
    no number, because it will be quoted.
    """
    import warnings

    want_keys = frozenset(labels)
    missing: list[Any] = []
    kept: list[Any] = []
    # Partitioned in ONE pass rather than by `e not in missing`: a reservoir group holds tens of
    # thousands of entries and the membership test is O(n^2) on a dataclass __eq__, which would
    # make a diagnostic the slowest thing in the read.
    for entry in entries:
        (kept if want_keys <= frozenset(getattr(entry, "labels", None) or {}) else missing).append(
            entry
        )
    if not missing:
        return

    absent_keys = sorted(
        {k for k in want_keys for e in missing if k not in (getattr(e, "labels", None) or {})}
    )
    by_slice: dict[str, list[Any]] = {}
    for entry in missing:
        by_slice.setdefault(_slice_name(entry), []).append(entry)

    total, unit = _sum_counts(missing)
    kept_total, kept_unit = _sum_counts(kept)

    def _detail(sel: list[Any]) -> str:
        n, u = _sum_counts(sel)
        shards = f"{len(sel)} shard{'s' if len(sel) != 1 else ''}"
        return f"{shards}, {n:,} {u}" if n is not None and u == unit else shards

    excluded = "; ".join(f"{name} ({_detail(sel)})" for name, sel in sorted(by_slice.items()))
    share = ""
    if total is not None and kept_total is not None and kept_unit == unit:
        denominator = total + kept_total
        if denominator:
            share = f" That is {total / denominator * 100:.1f}% of the group's {unit}."

    warnings.warn(
        f"labels={dict(labels)!r} on {dataset_id}/{version} group {gname!r}: the key(s) "
        f"{absent_keys} are ABSENT from {len(missing)} of {len(entries)} entries, so those were "
        f"excluded by the key not existing rather than by its value. Label depth is per-entry and "
        f"legitimately mixed — a segment like 'domain' is present only where the source shipped "
        f"one upstream — so this filter narrows to the labelled sources ONLY. Excluded: "
        f"{excluded}.{share} This is a real result, not an error: pass "
        f"warn_partial_labels=False if the narrowing is intended, select by a key every entry "
        f"carries (usually 'source') to reach all of them, or "
        f"warnings.simplefilter('error', PartialLabelCoverage) to make it fatal.",
        PartialLabelCoverage,
        stacklevel=3,
    )


def _sum_counts(entries: list[Any]) -> tuple[int | None, str | None]:
    """``(total, unit)`` summed over entries, or ``(None, None)`` if that is not meaningful.

    Returns ``None`` rather than a wrong number in three cases: no entry declares a count, the
    entries disagree about the unit, or the unit is not summable. A caller that filtered a
    partition needs the count of what it *selected* — inheriting the partition's declared total
    would overstate it, which is the exact failure ``validate``'s ``partition-rows-mismatch``
    exists to catch on the write side ("read.dataset_paths hands that number straight to a
    trainer").
    """
    total = 0
    unit: str | None = None
    seen = False
    for e in entries:
        count = getattr(e, "count", None)
        if not count:
            continue
        u = count.get("unit")
        if u not in _COUNTABLE_UNITS:
            continue
        if unit is not None and u != unit:
            return None, None  # mixed units: no single honest total
        unit = u
        total += int(count.get("value", 0))
        seen = True
    return (total, unit) if seen else (None, None)


def _resolve_format(
    entries: list[Any], prefix: str, gname: str
) -> tuple[str | None, str | None, int]:
    """The one ``(dtype, byte_order, header_bytes)`` that describes every fixed-width shard in a
    group — or :class:`MixedFormat` if there is no such single answer.

    DISAGREEMENT IS AN ERROR, not a ``None``. See :class:`MixedFormat`: the caller gets one
    triple because the loader memmaps the group as one array, so an ambiguous group has no
    correct single answer and the softer signals (``None``, ``"mixed"``) are indistinguishable
    from the legitimate "container types itself" answer and get defaulted by the loader.

    Only fixed-width entries participate: an entry with ``dtype=None`` is a self-typing
    container (parquet/jsonl/csv/text) or a tokenizer/vendored file and makes no claim, so it
    can neither set nor contradict the group's dtype. A group of ONLY such entries resolves to
    ``(None, None, 0)`` — legitimately untyped, not mixed.

    ``header_bytes`` is checked alongside dtype for the same reason: two shards with the same
    dtype but different header sizes cannot be read by one memmap stride either, and a
    disagreement there is the ".npy lie" shape (some shards headerless, some not).
    """
    typed = [e for e in entries if e.format.dtype]
    if not typed:
        return None, None, 0
    triples = {(e.format.dtype, e.format.byte_order, e.format.header_bytes) for e in typed}
    if len(triples) == 1:
        return triples.pop()
    raise MixedFormat(
        f"{prefix} group {gname!r} declares {len(triples)} different fixed-width formats "
        f"{sorted(str(t) for t in triples)}; there is no single dtype/byte_order/header_bytes "
        f"that reads all of its shards. A loader memmaps a group as one array, so this cannot "
        f"be resolved here — split the differently-typed shards into separate groups and select "
        f"one with group=."
    )


def _find_partition(group: dict[str, Any], split: str) -> dict[str, Any] | None:
    for p in group.get("partitions", []):
        if p.get("name") == split:
            return p
    return None


def resolve_latest(dataset_id: str, *, s3: S3, data_bucket: str = DATA_BUCKET) -> str | None:
    """Highest published version of a dataset per the catalog. Returns e.g. ``"v3"`` or None."""
    highest = 0
    found = False
    for obj in s3.list(data_bucket, f"_catalog/{dataset_id}/"):
        base = obj["key"].rsplit("/", 1)[-1]
        if base.endswith(".json"):
            seg = base[:-5]
            if seg.startswith("v") and seg[1:].isdigit():
                highest = max(highest, int(seg[1:]))
                found = True
    return f"v{highest}" if found else None


def verify_seal(
    dataset_id: str,
    version: str,
    *,
    s3: S3,
    data_bucket: str = DATA_BUCKET,
) -> list[str]:
    """Recompute the sealed hashes and report every mismatch. Empty list = intact.

    The seal written by ``promote()`` carries ``dataset_sha256`` (the root) and each group's
    ``manifest_sha256``. This walks the chain the way a verifier should: recompute
    ``sha256(dataset.json)`` from the bytes and compare to the root; then, for each group in
    that file, recompute ``sha256(manifest.json)`` and compare to both the seal's copy and
    ``dataset.json``'s own copy. Payload digests hang off the manifests from there.

    Recompute, never trust — a seal that merely asserts "someone validated this" is
    decoration. This is the check that makes "frozen means frozen" falsifiable, and it is
    cheap: two small GETs per group, no payload bytes.

    Returns human-readable mismatch descriptions rather than raising, so a caller can report
    all of them at once (an fsck sweep wants the full picture, not the first failure).
    """
    import json

    from .contracts import sha256_bytes

    prefix = f"{dataset_id}/{version}"
    problems: list[str] = []

    try:
        seal = _load_json(s3, data_bucket, f"{prefix}/_VALIDATED.json")
    except NotFound:
        raise NotValidated(f"{data_bucket}/{prefix} has no _VALIDATED.json") from None

    try:
        ds_bytes = s3.get(data_bucket, f"{prefix}/dataset.json")
    except NotFound:
        return [f"{prefix}: sealed but dataset.json is absent"]

    sealed_root = seal.get("dataset_sha256")
    actual_root = sha256_bytes(ds_bytes)
    if sealed_root is None:
        # Pre-root seal (written before the chain had a root). Say so rather than passing
        # silently: an unverifiable seal is a different state from a verified one.
        problems.append(
            f"{prefix}: seal carries no dataset_sha256 — written before the chain was rooted, "
            f"so it cannot be verified (recomputed root is {actual_root})"
        )
    elif sealed_root != actual_root:
        problems.append(
            f"{prefix}/dataset.json: sealed dataset_sha256={sealed_root} but recomputed "
            f"{actual_root} — the sealed dataset.json is NOT the one published"
        )

    ds = json.loads(ds_bytes.decode("utf-8"))
    sealed_manifests = seal.get("manifest_sha256") or {}
    for group in ds.get("groups", []):
        gname = str(group.get("name"))
        man_rel = group.get("manifest") or "manifest.json"
        declared = group.get("manifest_sha256")
        try:
            man_bytes = s3.get(data_bucket, f"{prefix}/{man_rel}")
        except NotFound:
            problems.append(f"{prefix}/{man_rel}: group {gname!r} manifest is absent")
            continue
        actual = sha256_bytes(man_bytes)
        if declared and declared != actual:
            problems.append(
                f"{prefix}/{man_rel}: dataset.json declares manifest_sha256={declared} but "
                f"recomputed {actual}"
            )
        sealed = sealed_manifests.get(gname)
        if sealed and sealed != actual:
            problems.append(
                f"{prefix}/{man_rel}: seal records manifest_sha256={sealed} but recomputed {actual}"
            )
    return problems


# --------------------------------------------------------------------------------------
# data mixtures — choose a weighted subset, reproducibly
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MixtureSource:
    """One weighted component of a mixture.

    ``labels`` is the predicate that picks this component's shards — generic key/value match,
    so it works for a pretrain corpus's ``{"source": …}``, a finer ``{"source": …, "domain": …}``,
    or whatever keys another family uses.
    """

    labels: Mapping[str, str]
    #: Share of the total budget this component should contribute, in (0, 1].
    ratio: float
    #: Upsampling ceiling. ``1.0`` means never reuse a shard. ``1.05`` means this component may
    #: contribute up to 1.05x its own size by repeating shards — the only way a small source can
    #: reach a large ratio. The legacy config used exactly this on arxiv and wikipedia.
    max_repetition_ratio: float = 1.0
    #: Never consume more than this fraction of the component's available count, even if the
    #: ratio asks for more. Holds data in reserve.
    max_source_fraction: float = 1.0

    @property
    def name(self) -> str:
        """A stable display key, e.g. ``source=stack-edu,domain=Python``."""
        return ",".join(f"{k}={self.labels[k]}" for k in sorted(self.labels))


@dataclass(frozen=True)
class ResolvedMixture:
    """A resolved mixture: the URIs to train on, plus what you actually got.

    ``actual_ratios`` and ``shortfall`` are the point. Whole-shard selection cannot hit a ratio
    exactly, and a component whose pool is too small silently under-delivers — so the result
    states what was achieved rather than echoing what was asked for.
    """

    dataset_id: str
    version: str
    paths: list[str]
    dtype: str | None
    byte_order: str | None
    header_bytes: int
    seed: int
    #: The count unit these totals are in — ``tokens`` for a pretrain corpus, but the manifest
    #: decides, not this module.
    unit: str | None
    total: int
    counts_by_source: dict[str, int]
    actual_ratios: dict[str, float]
    requested_ratios: dict[str, float]
    #: Per component, how far short of its requested budget it fell. Absent when it was met.
    shortfall: dict[str, int]

    @property
    def numpy_dtype(self) -> str | None:
        """Byte-order-qualified dtype string, as :class:`ResolvedSplit` gives."""
        return _numpy_dtype_of(self.dtype, self.byte_order)


def _shuffle_key(seed: int, dataset_id: str, version: str, path: str) -> bytes:
    """Deterministic sort key for one shard.

    Counter-mode SHA-256, the house pattern (``profiles.base.sample_offsets``): no PRNG object,
    no ``random``/``numpy``, so the permutation is a pure function of its inputs and
    reproducible across processes, machines and Python versions.

    ``dataset_id`` and ``version`` are bound in — mirroring
    ``validate``'s ``sha256(f"{dataset_id}|{version_id}|{gname}")`` — so seed 42 picks an
    unrelated subset of each dataset. Reusing one seed across datasets is then not a hidden
    correlation between their samples.

    Note this does NOT reuse ``sample_offsets`` itself: that function dedups with
    ``sorted(set(...))`` and so can return fewer values than asked for. Fine for sampling bytes
    within a file, wrong for choosing a set of distinct shards.
    """
    import hashlib

    return hashlib.sha256(f"{seed}|{dataset_id}|{version}|{path}".encode()).digest()


def build_mixture(
    dataset_id: str,
    version: str,
    *,
    sources: Sequence[MixtureSource],
    total: int,
    seed: int,
    s3: S3,
    data_bucket: str = DATA_BUCKET,
    require_validated: bool = True,
    group: str | None = None,
    split: str | None = "train",
    warn_partial_labels: bool = True,
) -> ResolvedMixture:
    """Choose a weighted, seeded subset of one dataset.

    ``total`` is a budget in the group's own count unit (tokens for a pretrain corpus). Each
    component gets ``ratio * total``, filled with WHOLE shards drawn in a seed-determined order.

    **Whole shards, chosen randomly — not the head of every shard.** The legacy
    ``SourceMixtureDatasetConfig`` took ``ceil(available * ratio)`` from the front of every path,
    so a 10% mixture read the first 10% of every shard and never touched a tail; any ordering
    inside a shard (crawl batch, date, repo) became a systematic skew. Drawing whole shards in a
    seeded order has no positional bias, and needs no way to express "the first N tokens of this
    file" — which neither :class:`ResolvedSplit` nor OLMo-core can represent. The cost is that a
    budget lands within one shard of target rather than exactly on it.

    Same ``seed`` and same inputs always give the same shard list; a different seed gives a
    different one. That pair of properties is what makes a run reproducible from its config.

    Single-dataset by construction. Mixing two datasets would risk combining corpora tokenized
    with different tokenizers whose vocab sizes are similar enough that every id still looks
    valid — semantically wrong and silent. Doing that safely needs a tokenizer-identity check
    across the datasets' ``depends_on`` pins, which is deliberately not built here.

    ``warn_partial_labels`` behaves as in :func:`dataset_paths`, and is checked PER COMPONENT
    because a mixture is where a partial-coverage predicate does real damage: a ``ratio`` is a
    share of the budget, so a component whose predicate reaches only the nested sources still
    draws its full ratio — the mix looks balanced, ``shortfall`` is empty, and the corpus is
    composed of a fraction of the sources the caller believed they had named. The two entry
    points warn on the same condition on purpose; a warning present in the direct read and
    absent from the mixture would be a warning that fires only where it is least needed.
    """
    if not sources:
        raise ReadError("build_mixture needs at least one source")
    if total <= 0:
        raise ReadError(f"total must be > 0; got {total}")
    for src in sources:
        if not src.labels:
            raise ReadError(f"source {src.name!r} has an empty label predicate")
        if not 0 < src.ratio <= 1:
            raise ReadError(f"source {src.name!r}: ratio must be in (0, 1]; got {src.ratio}")
        if src.max_repetition_ratio < 1:
            raise ReadError(
                f"source {src.name!r}: max_repetition_ratio must be >= 1 "
                f"({src.max_repetition_ratio} would DISCARD data rather than repeat it; use "
                f"max_source_fraction to consume less)"
            )
        if not 0 < src.max_source_fraction <= 1:
            raise ReadError(
                f"source {src.name!r}: max_source_fraction must be in (0, 1]; "
                f"got {src.max_source_fraction}"
            )
    names = [s.name for s in sources]
    if len(set(names)) != len(names):
        raise ReadError(f"duplicate source predicates in the mixture: {sorted(names)}")
    ratio_sum = sum(s.ratio for s in sources)
    if abs(ratio_sum - 1.0) > 1e-6:
        raise ReadError(
            f"source ratios must sum to 1.0; got {ratio_sum:.6f} for {names}. An implicit "
            f"remainder would silently decide part of your data mix."
        )

    # One resolve for the whole mixture: seal verified once, manifest read once. Each component
    # then filters this in memory — no extra S3 calls per source.
    base = dataset_paths(
        dataset_id, version, split=split, s3=s3, data_bucket=data_bucket,
        require_validated=require_validated, group=group,
    )
    pool_group, pool = _mixture_entries(
        dataset_id, version, s3=s3, data_bucket=data_bucket, group=group, split=split,
    )

    chosen: list[str] = []
    counts: dict[str, int] = {}
    shortfall: dict[str, int] = {}
    unit: str | None = None
    #: Distinct label-KEY sets already reported. Ten components keyed on ``domain`` describe one
    #: coverage gap, not ten — repeating it per component would train the reader to skim past it.
    warned_keysets: set[frozenset[str]] = set()

    for src in sources:
        matching = [e for e in pool if _matches_labels(e, src.labels)]
        if not matching:
            raise ReadError(
                f"source {src.name!r} matches no shards in {dataset_id}/{version}. Check the "
                f"label keys — a predicate that matches nothing would otherwise contribute "
                f"silently zero to the mixture."
            )
        if warn_partial_labels and frozenset(src.labels) not in warned_keysets:
            warned_keysets.add(frozenset(src.labels))
            # Against `pool`, which is already split-filtered, so the reported exclusion covers
            # the shards this mixture could actually have drawn — not held-out data it was never
            # eligible for.
            _warn_partial_label_coverage(pool, src.labels, dataset_id, version, pool_group)
        available, src_unit = _sum_counts(matching)
        if available is None:
            raise ReadError(
                f"source {src.name!r}: shards declare no summable count, or disagree about the "
                f"unit, so a budget over them is meaningless"
            )
        if unit is not None and src_unit != unit:
            raise ReadError(
                f"mixture components disagree about the count unit ({unit!r} vs {src_unit!r}); "
                f"a single budget cannot span both"
            )
        unit = src_unit

        want = int(total * src.ratio)
        # A CEILING and a TARGET behave differently at the last shard, and conflating them makes
        # max_source_fraction a lie. The budget is a goal: overshooting it by part of one shard
        # is the accepted cost of whole-shard selection. The fraction is a LIMIT the caller
        # asked not to exceed — "use at most 10% of arxiv" must not consume 13.5% because one
        # 40M-token shard straddled the line. So: fill toward `want`, but never cross `hard_cap`.
        hard_cap = int(available * src.max_source_fraction * src.max_repetition_ratio)
        target = min(want, hard_cap)
        capped = hard_cap < want

        ordered = sorted(matching, key=lambda e: _shuffle_key(seed, dataset_id, version, e.path))
        got = 0
        picked: list[Any] = []
        # Passes > 1 only happen when max_repetition_ratio > 1: the pool is walked again in the
        # same seeded order, so a repeat is deterministic too.
        while got < target:
            before = got
            for e in ordered:
                if got >= target:
                    break
                n = int((getattr(e, "count", None) or {}).get("value", 0))
                if capped and got + n > hard_cap:
                    continue  # would breach the limit; try a smaller shard
                picked.append(e)
                got += n
            if got == before:
                break  # nothing left that fits, or every shard counts zero; refuse to spin

        counts[src.name] = got
        chosen.extend(f"s3://{data_bucket}/{dataset_id}/{version}/{e.path}" for e in picked)
        if got < want:
            shortfall[src.name] = want - got

    grand = sum(counts.values())
    return ResolvedMixture(
        dataset_id=dataset_id,
        version=version,
        paths=chosen,
        dtype=base.dtype,
        byte_order=base.byte_order,
        header_bytes=base.header_bytes,
        seed=seed,
        unit=unit,
        total=grand,
        counts_by_source=counts,
        actual_ratios={k: (v / grand if grand else 0.0) for k, v in counts.items()},
        requested_ratios={s.name: s.ratio for s in sources},
        shortfall=shortfall,
    )


def _mixture_entries(
    dataset_id: str,
    version: str,
    *,
    s3: S3,
    data_bucket: str,
    group: str | None,
    split: str | None,
) -> tuple[str, list[Any]]:
    """``(group_name, entries)`` a mixture may draw from, split-filtered the way the reader is.

    Kept separate from :func:`dataset_paths` because a mixture needs the ENTRIES (for their
    labels and counts), while ``dataset_paths`` returns URI strings. Both read the same manifest
    and apply the same trainable-split rule, so a mixture can never draw a held-out shard that
    an unsplit read would have withheld.

    The group NAME comes back alongside because it is resolved here (``group=None`` on a
    single-group dataset) and a diagnostic that names the wrong group, or no group, sends the
    reader to the wrong manifest.
    """
    prefix = f"{dataset_id}/{version}"
    ds = _load_json(s3, data_bucket, f"{prefix}/dataset.json")
    groups = ds.get("groups") or []
    chosen_group = _choose_group(groups, group, dataset_id, version)
    gname = str(chosen_group.get("name"))
    manifest = _load_json(
        s3, data_bucket, f"{prefix}/{chosen_group.get('manifest', f'{gname}/manifest.json')}"
    )
    entries = [ManifestEntry.from_dict(e) for e in manifest.get("entries", [])]
    if split is None:
        return gname, entries
    # Recompute the split from each filename rather than trusting a declaration — the same
    # hardening dataset_paths applies, for the same reason.
    keep = []
    for e in entries:
        parsed = parse_shard_name(e.path)
        if parsed is None or parsed[0] not in SPLITS:
            continue  # not a split-bearing shard; a mixture is over shards
        if parsed[0] == split:
            keep.append(e)
    return gname, keep


__all__ = [
    "dataset_paths",
    "build_mixture",
    "MixtureSource",
    "ResolvedMixture",
    "MixedFormat",
    "PartialLabelCoverage",
    "SealMismatch",
    "resolve_latest",
    "verify_seal",
    "ResolvedSplit",
    "ReadError",
    "NotValidated",
]
