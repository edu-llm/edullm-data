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
from typing import Any, Mapping

from .contracts import SPLITS, is_trainable
from .manifest import ManifestEntry, parse_shard_name
from .s3 import S3, NotFound

DATA_BUCKET = "edullm-data"


class ReadError(RuntimeError):
    """Cannot resolve the requested dataset/split for reading."""


class NotValidated(ReadError):
    """The prefix carries no validation marker — refuse to read (§9)."""


class SealMismatch(ReadError):
    """The dataset's bytes do not match the seal written when it was validated.

    Distinct from :class:`NotValidated`: the marker IS there, and it disagrees with what is on
    the shelf. That is a stronger signal than absence — something changed a frozen dataset.
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
) -> ResolvedSplit:
    """Resolve a dataset to concrete object URIs + dtype.

    ``group`` selects which payload group when a dataset has several; defaults to the single
    group if there is exactly one, else raises so the caller is explicit about what they read.

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

    groups = ds.get("groups", [])
    if not groups:
        raise ReadError(f"{prefix} declares no groups")

    if group is not None:
        chosen = next((g for g in groups if g.get("name") == group), None)
        if chosen is None:
            raise ReadError(f"group {group!r} not found; groups are {[g.get('name') for g in groups]}")
    elif len(groups) == 1:
        chosen = groups[0]
    else:
        raise ReadError(
            f"{prefix} has {len(groups)} groups {[g.get('name') for g in groups]}; "
            f"pass group= to choose one"
        )

    gname = chosen["name"]
    manifest = _load_json(s3, data_bucket, f"{prefix}/{chosen.get('manifest', f'{gname}/manifest.json')}")
    entries = [ManifestEntry.from_dict(e) for e in manifest.get("entries", [])]

    # dtype: uniform across the group's raw shards, read from the manifest, never inferred
    dtypes = {e.format.dtype for e in entries if e.format.dtype}
    dtype = dtypes.pop() if len(dtypes) == 1 else None

    def _uri(entry: Any) -> str:
        return f"s3://{data_bucket}/{prefix}/{entry.path}"

    def _select(part: Mapping[str, Any]) -> tuple[list[Any], dict[str, Any]]:
        """Entries belonging to one partition, plus any loader kwargs it implies."""
        if part.get("by") == "path":
            glob = part.get("glob", "")
            return [
                e for e in entries
                if fnmatch.fnmatch(e.path.rsplit("/", 1)[-1], glob) or fnmatch.fnmatch(e.path, glob)
            ], {}
        # field/range/indices: the split is a row predicate, not a file subset. Return all
        # shards plus the predicate so the loader applies it — never silently return the whole
        # set as if it were the split.
        return list(entries), {"row_predicate": {k: part[k] for k in part if k not in {"name"}}}

    # Resolve EVERY declared split, always. This is what makes "a dataset returns train and
    # val" true without flattening them together.
    declared = [p for p in (chosen.get("partitions") or []) if isinstance(p, Mapping) and p.get("name")]
    splits: dict[str, list[str]] = {}
    split_rows: dict[str, int | None] = {}
    for part in declared:
        name = str(part["name"])
        sel, _ = _select(part)
        splits[name] = [_uri(e) for e in sel]
        split_rows[name] = part.get("rows")

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
                )
            raise ReadError(
                f"split {split!r} is not in the vocabulary {sorted(SPLITS)}; group {gname!r} "
                f"declares {sorted(splits)}"
            )
        rows = part.get("rows")
        selected, kwargs = _select(part)
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
                selected = list(entries)
        else:
            # No trainable split DECLARED — a tokenizer, a vendored tree, an eval set. There is
            # normally nothing to protect here, so the whole artifact is the payload.
            selected = list(entries)

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


__all__ = [
    "dataset_paths",
    "SealMismatch",
    "resolve_latest",
    "verify_seal",
    "ResolvedSplit",
    "ReadError",
    "NotValidated",
]
