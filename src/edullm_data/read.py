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
from dataclasses import dataclass
from typing import Any

from .manifest import ManifestEntry
from .s3 import S3, NotFound

DATA_BUCKET = "edullm-data"


class ReadError(RuntimeError):
    """Cannot resolve the requested dataset/split for reading."""


class NotValidated(ReadError):
    """The prefix carries no validation marker — refuse to read (§9)."""


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
) -> ResolvedSplit:
    """Resolve a dataset (optionally one split) to concrete object URIs + dtype.

    ``group`` selects which payload group when a dataset has several; defaults to the single
    group if there is exactly one, else raises so the caller is explicit about what they read.
    """
    prefix = f"{dataset_id}/{version}"
    if require_validated:
        _require_validated(s3, data_bucket, prefix)

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

    # split selection via the group's partitions (path form resolvable here; field/range/
    # indices need the row-level machinery a loader applies, so we return all paths + note it)
    selected = entries
    rows: int | None = None
    kwargs: dict[str, Any] = {}
    if split is not None:
        part = _find_partition(chosen, split)
        if part is None:
            raise ReadError(f"split {split!r} not declared in group {gname!r} partitions")
        rows = part.get("rows")
        by = part.get("by")
        if by == "path":
            glob = part.get("glob", "")
            selected = [
                e for e in entries
                if fnmatch.fnmatch(e.path.rsplit("/", 1)[-1], glob) or fnmatch.fnmatch(e.path, glob)
            ]
        else:
            # field/range/indices: the split is a row predicate, not a file subset. Return
            # all shards plus the predicate so the loader applies it — never silently return
            # the whole set as if it were the split.
            kwargs["row_predicate"] = {k: part[k] for k in part if k not in {"name"}}

    paths = [f"s3://{data_bucket}/{prefix}/{e.path}" for e in selected]
    return ResolvedSplit(
        dataset_id=dataset_id,
        version=version,
        split=split or "*",
        paths=paths,
        dtype=dtype,
        rows=rows,
        kwargs=kwargs,
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
    "resolve_latest",
    "verify_seal",
    "ResolvedSplit",
    "ReadError",
    "NotValidated",
]
