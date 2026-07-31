"""publish() — the producer entrypoint (§6).

Four hand-typed arguments; everything else derived or inherited::

    publish(source, dataset_id="eval/mcq-arc", purpose="...", profile="eval-results/v1")

``source`` is a local directory *or* an ``s3://edullm-landing/...`` prefix already staged
by an AWS-native producer (a Batch job that wrote its output straight to landing). Either
way the result is the same object set under ``s3://edullm-landing/<dataset_id>/<version>/``,
sealed by the group manifests written last (§6: the manifest is the commit point).

Design rules this file exists to honor:

* **Derive, never ask.** The publisher hashes every file, counts tokens/rows, sniffs the
  format from the family default + extension, allocates the version, and reads the build
  executor from the environment. The producer types four things.
* **Inherit once per family.** ``license``, ``sources``, and the tokenizer pin come from
  ``families/<family>.json`` so the tiny high-volume case (a 92-file eval set) stays near
  free to publish correctly (§11).
* **Create-only writes.** Every ``put`` uses ``IfNoneMatch:*`` semantics via the S3 layer's
  create-only path; a version is reserved by writing ``dataset.json`` first (§2), and a
  collision means "someone took this version" → bump and retry.
* **Determinism.** No ``datetime.now``/``random``; ``created_at`` and the build stamp are
  passed in (the CLI fills them from the environment, not from wall-clock inside logic).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import jsonl as jsonl_mod
from .contracts import (
    NamingError,
    SCHEMA_VERSION,
    SPLITS,
    _resolve_families_dir,
    canonical_json,
    validate_dataset_id,
    validate_purpose,
)
from .manifest import (
    EXTENSION_FORMAT,
    Format,
    ManifestEntry,
    build_manifest,
    labels_from_path,
    manifest_sha256,
    parse_shard_name,
)
from .s3 import S3, NotFound

LANDING_BUCKET = "edullm-landing"
#: Shared with the validator ON PURPOSE — see ``validate._resolve_families_dir`` for the three
#: layouts it covers (env override, installed wheel, source checkout). This module used to
#: hardcode the repo-root-relative path, which resolves to a nonexistent directory inside an
#: installed wheel: `publish()` then died with "no family.json for 'pretrain'" the first time it
#: ran on Batch, having already spent the whole run getting there. The validator was fixed and
#: the producer was not, which is the same half-fix twice — one module's bounds silently wrong,
#: the other's publish loudly dead.
FAMILIES_DIR = _resolve_families_dir()


class PublishError(RuntimeError):
    """publish() could not proceed — a naming failure, a missing family, a bad source."""


class VersionConflict(PublishError):
    """The reserved version was taken between the read and the create-only write."""


@dataclass
class PublishPlan:
    """What a publish will write, computed before anything hits S3 so a dry run is real."""

    dataset_id: str
    version: str
    dataset_json: dict[str, Any]
    manifests: dict[str, dict[str, Any]]  # group name -> manifest dict
    payload_keys: list[str]  # full landing keys of every payload object
    source_kind: str  # "local" | "s3"


# --------------------------------------------------------------------------------------
# family inheritance
# --------------------------------------------------------------------------------------


def _load_family(family: str) -> dict[str, Any]:
    path = FAMILIES_DIR / f"{family}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PublishError(
            f"no family.json for {family!r} (looked in {FAMILIES_DIR}); "
            f"a family must be defined before publishing into it"
        ) from None


# --------------------------------------------------------------------------------------
# format inference from extension + family default (never inferred from bytes — §5)
# --------------------------------------------------------------------------------------


def _format_for(path: str, family_defaults: Mapping[str, Any]) -> Format:
    """The declared format for a file. Extension is authoritative for the honest raw types
    (``.u32le.bin`` etc.); for self-typed containers (parquet/jsonl/csv) the family default
    fills dtype/byte_order as None. This is a *declaration*, checked against magic bytes by
    the validator — publish() declaring it wrong is caught at Gate A, not hidden."""
    # longest-suffix match against the known extension→format table
    for ext in sorted(EXTENSION_FORMAT, key=len, reverse=True):
        if path.lower().endswith(ext):
            spec = EXTENSION_FORMAT[ext]
            return Format(
                container=spec["container"],
                dtype=spec.get("dtype"),
                byte_order=spec.get("byte_order"),
                header_bytes=spec.get("header_bytes", 0),
                codec=spec.get("codec", "none"),
            )
    # unknown extension: fall back to the family's format default if it has one
    fam_fmt = family_defaults.get("format")
    if isinstance(fam_fmt, Mapping):
        return Format.from_dict(fam_fmt)
    raise PublishError(
        f"cannot determine format for {path!r}: unknown extension and the family declares "
        f"no default format. Name it with a self-describing extension (§5) or add a family default."
    )


def _count_for(path: str, size: int, fmt: Format, s3: S3, bucket: str, key: str) -> dict[str, Any] | None:
    """Best-effort count, computed WITHOUT loading the payload whole.

    * Fixed-width raw → tokens = size / dtype_size. Pure arithmetic on the object size;
      **zero bytes read.** This is the common (and largest) case — a 633 GB token shard
      gets its count for free.
    * Line formats (.jsonl / .jsonl.gz) → rows, by streaming-parsing JSON objects with
      :mod:`edullm_data.jsonl` (same definition Gate A's text-corpus profile uses).
    * Everything else → omit (a tar part or sentinel has no honest count, §5).
    """
    if fmt.container == "raw" and fmt.dtype_size:
        return {"unit": "tokens", "value": size // fmt.dtype_size}
    is_jsonl, gzipped = jsonl_mod.is_jsonl_path(path)
    if is_jsonl:
        try:
            n = jsonl_mod.count_jsonl_objects_s3(s3, bucket, key, gzipped=gzipped)
        except (OSError, ValueError, EOFError):
            return None
        return {"unit": "rows", "value": n}
    return None

# --------------------------------------------------------------------------------------
# source enumeration
# --------------------------------------------------------------------------------------


_CONTROL_BASENAMES = {"dataset.json", "manifest.json", "_SUCCESS", "_VALIDATED.json", "_REJECTED.json", "README.md"}


def _stage_local_to_landing(source: Path, s3: S3, landing_bucket: str, staging_prefix: str) -> None:
    """Upload a local directory to a landing staging prefix, one object at a time via a
    STREAMING put (boto3 upload_file handles multipart, bounded memory). After this, a
    local source is indistinguishable from an s3:// source and the rest of publish() only
    ever works with objects already in S3 — payload bytes are never held whole in the
    caller. For anything but a laptop-scale dataset you should stage on Batch, not here;
    this path exists for small local publishes and dev."""
    for p in sorted(source.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(source).as_posix()
        if rel.rsplit("/", 1)[-1] in _CONTROL_BASENAMES:
            continue
        s3.put_file(landing_bucket, f"{staging_prefix}/{rel}", str(p))


def _enumerate_s3(s3: S3, bucket: str, prefix: str) -> list[tuple[str, int]]:
    """(group-relative path, SIZE) for every payload object under the prefix. Metadata
    only — NEVER the bytes. Size comes from the LIST result, so this is one paginated call
    regardless of dataset size."""
    prefix = prefix.strip("/")
    out: list[tuple[str, int]] = []
    for obj in sorted(s3.list(bucket, prefix + "/"), key=lambda o: o["key"]):
        key = obj["key"]
        rel = key[len(prefix) + 1 :]
        if rel.rsplit("/", 1)[-1] in _CONTROL_BASENAMES:
            continue
        out.append((rel, obj["size"]))
    return out


def _group_of(rel_path: str) -> str:
    """The group a payload file belongs to = its first path segment (tokens/, sidecars/…)."""
    return rel_path.split("/", 1)[0] if "/" in rel_path else ""




# --------------------------------------------------------------------------------------
# build the plan
# --------------------------------------------------------------------------------------


def build_plan(
    files: Sequence[tuple[str, int]],
    *,
    dataset_id: str,
    version: str,
    purpose: str,
    profile: str | Mapping[str, str],
    family: dict[str, Any],
    created_at: str,
    build_executor: dict[str, Any],
    source_kind: str,
    s3: S3,
    source_bucket: str,
    source_prefix: str,
    owner: str | None = None,
    group_meta: Mapping[str, Mapping[str, Any]] | None = None,
    hash_workers: int = 1,
    sources: Sequence[Mapping[str, Any]] | None = None,
    about: str | None = None,
    notes: str | None = None,
    limitations: Sequence[Mapping[str, Any]] | None = None,
    license: Mapping[str, Any] | None = None,
    tokenizer_depends_on: Mapping[str, Any] | None = None,
) -> PublishPlan:
    """Turn (path, size) metadata + the staged S3 objects into the exact objects to write.

    Hashes each object by STREAMING it from S3 (``s3.hash_object``) — never loads a payload
    whole. ``files`` carries sizes only; bytes stay in S3. ``source_bucket``/``source_prefix``
    locate the already-staged objects to hash and count.

    ``hash_workers`` > 1 hashes a group's objects concurrently on a thread pool. Hashing is
    network-bound (stream the object, feed hashlib), so threads scale it near-linearly despite
    the GIL — a 125 GB / 218-shard corpus hashes in minutes instead of ~45 min single-threaded.
    Default 1 keeps the original strictly-sequential behavior (and every existing test) intact.
    Results are collected in submission order, so the manifest is identical regardless of
    worker count."""
    defaults = family.get("defaults", {})
    group_meta = group_meta or {}
    source_prefix = source_prefix.strip("/")

    # group files by their first path segment
    by_group: dict[str, list[tuple[str, int]]] = {}
    for rel, size in files:
        g = _group_of(rel)
        if not g:
            raise PublishError(
                f"payload file {rel!r} is not under a group prefix; every object must live "
                f"under <group>/… so its profile is unambiguous (§4)"
            )
        by_group.setdefault(g, []).append((rel, size))

    if not by_group:
        raise PublishError("no payload files found under the source")

    # resolve the profile per group
    def profile_for(g: str) -> str:
        if isinstance(profile, str):
            return profile
        if g in profile:
            return profile[g]
        raise PublishError(f"group {g!r} has no profile in the profile mapping {dict(profile)!r}")

    manifests: dict[str, dict[str, Any]] = {}
    groups_meta: list[dict[str, Any]] = []
    total_objects = 0
    total_bytes = 0
    payload_keys: list[str] = []
    ds_prefix = f"{dataset_id}/{version}"

    for g in sorted(by_group):
        group_files = sorted(by_group[g], key=lambda t: t[0])
        if tokenizer_depends_on is not None and profile_for(g).startswith("pretrain-tokens/"):
            supplied_deps = group_meta.get(g, {}).get("depends_on", []) or []
            if any(isinstance(dep, Mapping) and dep.get("role") == "tokenizer" for dep in supplied_deps):
                raise PublishError(
                    f"group_meta[{g!r}].depends_on declares a tokenizer while tokenizer= was "
                    "also provided. Remove the group_meta tokenizer pin or omit tokenizer=; "
                    "two competing tokenizer identities are unsafe."
                )

        def _entry_for(item: tuple[str, int]) -> ManifestEntry:
            rel, _size = item
            fmt = _format_for(rel, defaults)
            src_key = f"{source_prefix}/{rel}" if source_prefix else rel
            sha, hashed_size = s3.hash_object(source_bucket, src_key)  # streamed, no whole-object RAM
            parsed = parse_shard_name(rel)
            try:
                derived_labels = labels_from_path(rel)
            except ValueError as e:
                # Re-raise in the producer's own vocabulary: a caller who staged a tree too
                # deep to label needs a PublishError like every other staging mistake, not a
                # bare ValueError from a metadata helper.
                raise PublishError(str(e)) from e
            return ManifestEntry(
                path=rel,
                sha256=sha,
                bytes=hashed_size,
                count=_count_for(rel, hashed_size, fmt, s3, source_bucket, src_key),
                format=fmt,
                # Both derived from the key itself, never asked of the caller, and both
                # recomputed by Gate A from that same key — so neither can drift from the
                # object it describes. `split` stays None for a name that is not a shard
                # (a tokenizer file, a vendored blob): absent, not guessed.
                split=parsed[0] if parsed and parsed[0] in SPLITS else None,
                labels=derived_labels or None,
            )

        if hash_workers > 1 and len(group_files) > 1:
            # Concurrent, but order-preserving: executor.map yields results in submission
            # order, so the manifest is byte-identical to the sequential path.
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=hash_workers) as pool:
                entries = list(pool.map(_entry_for, group_files))
        else:
            entries = [_entry_for(item) for item in group_files]

        for rel, _size in group_files:
            payload_keys.append(f"{ds_prefix}/{rel}")
        man = build_manifest(entries, group_name=g)
        manifests[g] = man
        total_objects += man["objects"]
        total_bytes += man["bytes"]

        gm: dict[str, Any] = {
            "name": g,
            "profile": profile_for(g),
            "prefix": f"{g}/",
            "manifest": f"{g}/manifest.json",
            "manifest_sha256": manifest_sha256(man),
        }
        if defaults.get("partitions") and "partitions" not in group_meta.get(g, {}):
            # The family default declares the partition SHAPE (name + by:path glob) but
            # cannot know rows — that count is only knowable once the shards exist. Fill it
            # in now from the manifest we just built, so Gate A's "every partition declares
            # rows" holds. Path partitions with a glob matching no shard are dropped, not
            # shipped empty (a dataset may only carry a train split, not the family's full set).
            #
            # When the family's globs are token-shaped (*.u32le.bin) but this group is JSONL
            # text, fall back to split-name-compatible ``<name>-*.jsonl*`` templates so a
            # companion text/ group still gets train/val partitions without hand-writing them.
            resolved = _resolve_path_partitions(defaults["partitions"], entries)
            if not resolved:
                resolved = _resolve_path_partitions(
                    _jsonl_partition_templates(defaults["partitions"]), entries
                )
            if resolved:
                gm["partitions"] = resolved
                gm["coverage"] = defaults.get("coverage", "partition")
        gm.update(group_meta.get(g, {}))
        # A CALLER-SUPPLIED partitions list used to bypass row-filling entirely: the branch
        # above is guarded on the caller NOT having supplied one, and this gm.update() copies
        # theirs verbatim. So any caller declaring its own splits — which is the only way to
        # get a val split, since the pretrain family defaults to train only — shipped
        # partitions with no `rows`, and Gate A rejects those (validate.py: partition-no-rows).
        #
        # That rejection lands at promote() time, i.e. AFTER the copy and the whole publish
        # run. For a 630 GB corpus that is hours of work discarded over a field the publisher
        # could have computed. Fill rows for every by:path partition regardless of who wrote
        # it; an explicitly declared rows is left alone so a caller can still override.
        if gm.get("partitions"):
            gm["partitions"] = _fill_missing_partition_rows(gm["partitions"], entries)
            gm.setdefault("coverage", defaults.get("coverage", "partition"))
        # A named tokenizer is authoritative for every pretrain-tokens group. Rejecting an
        # overlapping group_meta pin above avoids silently selecting one of two identities.
        if profile_for(g).startswith("pretrain-tokens/"):
            existing = list(gm.get("depends_on", []) or [])
            if tokenizer_depends_on is not None:
                existing.append(dict(tokenizer_depends_on))
                gm["depends_on"] = existing
            elif not any(
                isinstance(dep, Mapping) and dep.get("role") == "tokenizer"
                for dep in existing
            ):
                fam_dep = defaults.get("tokenizer_dependency_optional")
                if isinstance(fam_dep, Mapping) and fam_dep.get("dataset_id"):
                    existing.append(
                        {
                            k: fam_dep[k]
                            for k in ("role", "dataset_id", "version", "manifest_sha256")
                            if fam_dep.get(k)
                        }
                    )
                    gm["depends_on"] = existing
        groups_meta.append(gm)

    dataset_json = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "version": {"id": version, "relation": "supersedes", "of": _prev_version(version)},
        "created_at": created_at,
        "owner": owner or family.get("owner", "unknown"),
        "purpose": purpose,
        "mutability": defaults.get("mutability", "frozen"),
        "inventory": {"objects": total_objects, "bytes": total_bytes},
        "groups": groups_meta,
        # sources/license inherit from the family (written once, §11), but a per-dataset value
        # overrides: a corpus knows its real upstream mix and checked license terms, which the
        # family's honest "unknown" placeholder cannot. These are what the generated README's
        # data-mix and license sections render from (§3: the README is generated from here).
        "sources": list(sources) if sources is not None else family.get("sources", []),
        "build": {
            "executor": build_executor,
            "reproducibility": defaults.get("reproducibility", "logical"),
        },
        "license": dict(license) if license is not None else family.get("license", {"id": None, "basis": "unknown"}),
    }
    # about/notes/limitations are optional free-text/structured provenance (§3: they exist
    # because the README is generated). Emit only when provided — an absent key reads as "not
    # recorded", which is honest; an empty string/[] would read as "deliberately none".
    if about is not None and str(about).strip():
        dataset_json["about"] = str(about).strip()
    if notes is not None and str(notes).strip():
        dataset_json["notes"] = str(notes).strip()
    if limitations:
        dataset_json["limitations"] = list(limitations)

    return PublishPlan(
        dataset_id=dataset_id,
        version=version,
        dataset_json=dataset_json,
        manifests=manifests,
        payload_keys=payload_keys,
        source_kind=source_kind,
    )


def _prev_version(version: str) -> str | None:
    n = int(version[1:])
    return f"v{n - 1}" if n > 1 else None


def _count_rows_for_glob(glob: str, entries: Sequence[ManifestEntry]) -> int | None:
    """Sum the declared counts of every manifest entry whose name matches ``glob``.

    Returns ``None`` when nothing matches, so a caller can distinguish "no shards for this
    split" from "shards totalling zero rows". Matches the basename first, then the full
    manifest-relative path — the same order ``read.dataset_paths`` uses, so a partition that
    resolves here resolves identically at read time.
    """
    import fnmatch

    matched = [
        e for e in entries
        if fnmatch.fnmatch(e.path.rsplit("/", 1)[-1], glob) or fnmatch.fnmatch(e.path, glob)
    ]
    if not matched:
        return None
    rows = 0
    for e in matched:
        if e.count and e.count.get("unit") in {"tokens", "rows", "items"}:
            rows += int(e.count["value"])
    return rows


def _fill_missing_partition_rows(
    partitions: Sequence[Mapping[str, Any]], entries: Sequence[ManifestEntry]
) -> list[dict[str, Any]]:
    """Add ``rows`` to any ``by: path`` partition that lacks it, counted from the manifest.

    Idempotent and non-destructive: a partition that already declares ``rows`` is returned
    unchanged (a caller may have a reason to state it), and a non-path partition is passed
    through because its count cannot be derived from filenames alone. Unlike
    ``_resolve_path_partitions`` this does NOT drop a partition whose glob matches nothing —
    a caller who explicitly named a split is making a claim, and silently deleting it would
    hide the mistake. Gate A's ``partition-glob-empty`` is the right place for that to surface.
    """
    out: list[dict[str, Any]] = []
    for part in partitions:
        p = dict(part)
        if "rows" not in p and p.get("by") == "path":
            rows = _count_rows_for_glob(p.get("glob", ""), entries)
            if rows is not None:
                p["rows"] = rows
        out.append(p)
    return out


def _jsonl_partition_templates(
    templates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Rewrite family path-partition globs to JSONL-compatible ``<name>-*.jsonl*``.

    Pretrain family defaults use ``train-*.u32le.bin``; a companion ``text/`` group of
    ``.jsonl`` shards would otherwise get zero auto-partitions. Matching uses the
    partition ``name`` so train/val/heldout keep their identities.
    """
    out: list[dict[str, Any]] = []
    for tmpl in templates:
        t = dict(tmpl)
        if t.get("by") == "path":
            name = str(t.get("name") or "train")
            t["glob"] = f"{name}-*.jsonl*"
        out.append(t)
    return out


def _resolve_path_partitions(
    templates: Sequence[Mapping[str, Any]], entries: Sequence[ManifestEntry]
) -> list[dict[str, Any]]:
    """Turn family-default path-partition templates (name + glob, no rows) into concrete
    partitions with rows counted from the manifest. Only ``by: path`` templates can be
    auto-resolved here; a partition with no matching shard is dropped rather than shipped
    with rows=0, because a dataset legitimately may not contain every split the family
    anticipates. Non-path templates are passed through untouched — their rows must come
    from group_meta, and Gate A will flag any that still lack a count."""
    resolved: list[dict[str, Any]] = []
    for tmpl in templates:
        if tmpl.get("by") != "path":
            resolved.append(dict(tmpl))
            continue
        rows = _count_rows_for_glob(tmpl.get("glob", ""), entries)
        if rows is None:
            continue  # this split isn't present in this dataset
        out = dict(tmpl)
        out["rows"] = rows
        resolved.append(out)
    return resolved


# --------------------------------------------------------------------------------------
# version allocation + write
# --------------------------------------------------------------------------------------


def _next_version(s3: S3, landing_bucket: str, dataset_id: str) -> str:
    """Highest existing version under the dataset id (in landing OR the catalog) + 1. Not a
    lock — the create-only write of dataset.json is the actual guard; this just picks a good
    first guess so the common case doesn't collide."""
    highest = 0
    for obj in s3.list(landing_bucket, f"{dataset_id}/"):
        parts = obj["key"][len(dataset_id) + 1 :].split("/", 1)
        seg = parts[0]
        if seg.startswith("v") and seg[1:].isdigit():
            highest = max(highest, int(seg[1:]))
    return f"v{highest + 1}"


def _build_executor_from_env(env: Mapping[str, str]) -> dict[str, Any]:
    """Capture provenance from the environment (§3). AWS Batch producers get their job id
    and image digest for free; everything else records source + lockfile hashes, which are
    obtainable anywhere (this repo has no root .git, so a commit sha is not universal)."""
    if env.get("AWS_BATCH_JOB_ID"):
        return {
            "kind": "aws-batch",
            "job_id": env.get("AWS_BATCH_JOB_ID"),
            "job_attempt": int(env.get("AWS_BATCH_JOB_ATTEMPT", "1")),
            "job_definition_arn": env.get("AWS_BATCH_JQ_NAME") or env.get("AWS_BATCH_JOB_DEFINITION_ARN"),
            "region": env.get("AWS_REGION", "us-east-1"),
        }
    return {
        "kind": "external",
        "host_class": env.get("EDULLM_HOST_CLASS", "unknown"),
        "code_sha256": env.get("EDULLM_CODE_SHA256"),
        "packages_lock_sha256": env.get("EDULLM_PACKAGES_LOCK_SHA256"),
    }


def _resolve_tokenizer_dependency(tokenizer: str, s3: S3, data_bucket: str) -> dict[str, Any]:
    """Turn a ``tokenizer/<name>[/vN]`` reference into a pinned depends_on entry by looking
    up the PUBLISHED tokenizer dataset in the data bucket. Fails loudly if it isn't published
    — a corpus must reference a real, owned tokenizer, never a bare string."""
    parts = tokenizer.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "tokenizer":
        raise PublishError(
            f"tokenizer={tokenizer!r} must be 'tokenizer/<name>' or 'tokenizer/<name>/vN' — "
            f"the id of a PUBLISHED tokenizer/v1 dataset"
        )
    tok_id = f"{parts[0]}/{parts[1]}"
    version = parts[2] if len(parts) >= 3 else None
    if version is None:
        # latest published version from the catalog
        best = 0
        for obj in s3.list(data_bucket, f"_catalog/{tok_id}/"):
            base = obj["key"].rsplit("/", 1)[-1]
            if base.endswith(".json") and base[:-5].startswith("v") and base[:-5][1:].isdigit():
                best = max(best, int(base[:-5][1:]))
        if best == 0:
            raise PublishError(
                f"no published version of tokenizer {tok_id!r} found in {data_bucket} — "
                f"publish it first (profile tokenizer/v1)"
            )
        version = f"v{best}"
    dprefix = f"{tok_id}/{version}"
    try:
        pds = _load_family_json_from_s3(s3, data_bucket, f"{dprefix}/dataset.json")
    except Exception as e:  # noqa: BLE001
        raise PublishError(f"tokenizer {dprefix!r} is not readable in {data_bucket}: {e}") from e
    groups = pds.get("groups", [])
    if not groups:
        raise PublishError(f"tokenizer {dprefix!r} has no groups")
    man_sha = groups[0].get("manifest_sha256")
    return {"role": "tokenizer", "dataset_id": tok_id, "version": version, "manifest_sha256": man_sha}


def _load_family_json_from_s3(s3: S3, bucket: str, key: str) -> dict[str, Any]:
    import json

    return json.loads(s3.get(bucket, key).decode("utf-8"))


def publish(
    source: str | Path,
    *,
    dataset_id: str,
    purpose: str,
    profile: str | Mapping[str, str],
    s3: S3,
    created_at: str,
    tokenizer: str | None = None,
    data_bucket: str = "edullm-data",
    landing_bucket: str = LANDING_BUCKET,
    owner: str | None = None,
    group_meta: Mapping[str, Mapping[str, Any]] | None = None,
    build_executor: dict[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    max_version_attempts: int = 8,
    hash_workers: int = 1,
    copy_workers: int = 1,
    sources: Sequence[Mapping[str, Any]] | None = None,
    about: str | None = None,
    notes: str | None = None,
    limitations: Sequence[Mapping[str, Any]] | None = None,
    license: Mapping[str, Any] | None = None,
) -> PublishPlan:
    """Publish a dataset to landing. Returns the plan that was written.

    ``hash_workers`` / ``copy_workers`` > 1 parallelize the two network-bound, per-shard
    phases (stream-hash in ``build_plan``; server-side copy to the final prefix) across a
    thread pool. Both default to 1 (strictly sequential, unchanged behavior). For a TB-scale
    corpus on a multi-vCPU host, setting these to e.g. 16 turns a ~45-min sequential hash and
    a ~40-min sequential copy into a few minutes each. Concurrency is safe here: each shard is
    an independent create-only write to a distinct key, and hashing has no shared state.

    ``tokenizer`` names the PUBLISHED tokenizer this corpus was tokenized with — the primary,
    per-dataset way to attach it. Tokenizers vary per dataset (a family has no single
    canonical one), so this is named at publish time, not inherited from a family default.
    Accepts ``"tokenizer/<name>"`` (resolves the latest published version) or
    ``"tokenizer/<name>/vN"`` (exact). publish() looks up that dataset in ``data_bucket``,
    pins it by ``manifest_sha256``, and attaches it as a ``depends_on`` on every
    ``pretrain-tokens`` group — so the validator derives the vocab bound from the real
    tokenizer.json. Required in practice for a pretrain corpus: without a resolvable
    tokenizer the decode smoke test cannot recompute its bound and Gate A rejects the dataset.

    ``s3`` and ``created_at`` are injected so the whole thing is testable and deterministic;
    the CLI supplies a real client and an ISO timestamp.

    ``sources`` / ``about`` / ``notes`` / ``limitations`` / ``license`` are the descriptive
    metadata the generated per-dataset README renders from (§3: the README is generated *from*
    dataset.json). All optional and all defaulting to today's behavior: ``sources`` and
    ``license`` fall back to the family's inherited values when omitted, and the free-text fields
    are simply absent. ``sources`` is a list of ``{name, share?, tokens?, documents?, license?,
    uri?, scope?}`` describing the data mix; ``about`` is a curated narrative block; ``notes`` is
    a free-text caveat; ``limitations`` is a list of structured ``{kind, ...}`` caveats. None of
    these add a validator-required field — they are read only by the README generator.
    """
    # validate the four typed things up front — a bad name fails before any bytes move
    try:
        family_name, _ = validate_dataset_id(dataset_id)
    except NamingError as e:
        raise PublishError(f"invalid dataset_id: {e}") from e
    try:
        validate_purpose(purpose)
    except NamingError as e:
        raise PublishError(f"invalid purpose: {e}") from e

    family = _load_family(family_name)
    env = env if env is not None else dict(os.environ)

    # Resolve the per-dataset tokenizer (if named). Attachment to groups happens in
    # build_plan by *resolved profile* (pretrain-tokens/*), not by group_meta keys —
    # otherwise a multi-group publish with group_meta only for ``text`` would pin the
    # tokenizer on the raw-text group and leave the token group without vocab bounds.
    tokenizer_depends_on: Mapping[str, Any] | None = None
    if tokenizer is not None:
        tokenizer_depends_on = _resolve_tokenizer_dependency(tokenizer, s3, data_bucket)
    build_executor = build_executor or _build_executor_from_env(env)

    # Resolve the source to an (S3 bucket, prefix). A local dir is first STREAMED up to a
    # landing staging area, after which it is indistinguishable from an s3:// source — so
    # from here on, no payload byte is ever held whole in the caller.
    src_str = str(source)
    if src_str.startswith("s3://"):
        rest = src_str[len("s3://") :]
        source_bucket, _, source_prefix = rest.partition("/")
        source_prefix = source_prefix.strip("/")
        source_kind = "s3"
    else:
        source_bucket = landing_bucket
        source_prefix = f"_staging/{dataset_id}"
        _stage_local_to_landing(Path(source), s3, landing_bucket, source_prefix)
        source_kind = "local"

    files = _enumerate_s3(s3, source_bucket, source_prefix)  # (path, size) — metadata only
    if not files:
        raise PublishError(f"no files found at source {source!r}")

    # allocate version + write, retrying on create-only collision
    last_err: Exception | None = None
    for _ in range(max_version_attempts):
        version = _next_version(s3, landing_bucket, dataset_id)
        plan = build_plan(
            files,
            dataset_id=dataset_id,
            version=version,
            purpose=purpose,
            profile=profile,
            family=family,
            created_at=created_at,
            build_executor=build_executor,
            source_kind=source_kind,
            s3=s3,
            source_bucket=source_bucket,
            source_prefix=source_prefix,
            owner=owner,
            group_meta=group_meta,
            hash_workers=hash_workers,
            sources=sources,
            about=about,
            notes=notes,
            limitations=limitations,
            license=license,
            tokenizer_depends_on=tokenizer_depends_on,
        )
        ds_prefix = f"{dataset_id}/{version}"
        # 1. reserve the version: create-only dataset.json FIRST (§6 order)
        try:
            _put_create_only(s3, landing_bucket, f"{ds_prefix}/dataset.json", canonical_json(plan.dataset_json))
        except FileExistsError as e:
            last_err = e
            continue  # someone took this version; bump and retry

        # 2. payload objects: SERVER-SIDE COPY from the staged/source location to the final
        #    dataset prefix. Bytes move S3→S3 in-region; nothing transits the client. If the
        #    source is already exactly the final key (rare), skip. Each copy is an independent
        #    write to a distinct key, so copy_workers>1 fans them out on a thread pool.
        def _copy_one(item: tuple[str, int]) -> None:
            rel, _size = item
            src_key = f"{source_prefix}/{rel}" if source_prefix else rel
            dst_key = f"{ds_prefix}/{rel}"
            if source_bucket == landing_bucket and src_key == dst_key:
                return
            s3.copy(source_bucket, src_key, landing_bucket, dst_key)

        if copy_workers > 1 and len(files) > 1:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=copy_workers) as pool:
                list(pool.map(_copy_one, files))  # raises if any copy failed
        else:
            for item in files:
                _copy_one(item)

        # 3. group manifests LAST — the commit point (§6). Small control objects, safe to put.
        for g, man in plan.manifests.items():
            _put_idempotent(s3, landing_bucket, f"{ds_prefix}/{g}/manifest.json", canonical_json(man))

        # 4. best-effort: clear the local-staging area now that bytes are at the final prefix.
        if source_kind == "local":
            for rel, _size in files:
                try:
                    s3.delete(landing_bucket, f"{source_prefix}/{rel}")
                except Exception:  # noqa: BLE001 - staging cleanup is not load-bearing
                    pass

        return plan

    raise VersionConflict(f"could not allocate a version for {dataset_id!r} after {max_version_attempts} attempts") from last_err


def _put_create_only(s3: S3, bucket: str, key: str, body: bytes) -> None:
    """Write only if absent. FakeS3/Boto3S3 both expose plain put; we emulate create-only by
    a head-then-put, and the bucket policy's IfNoneMatch requirement enforces it for real on
    the server (a concurrent racer still gets rejected server-side)."""
    try:
        s3.head(bucket, key)
    except NotFound:
        s3.put(bucket, key, body, content_type="application/json")
        return
    raise FileExistsError(key)


def _put_idempotent(s3: S3, bucket: str, key: str, body: bytes) -> None:
    """Write unless a byte-identical object is already there (a resumed publish must not
    fail on its own prior partial upload)."""
    try:
        existing = s3.get(bucket, key)
        if existing == body:
            return
    except NotFound:
        pass
    ct = "application/json" if key.endswith(".json") else None
    s3.put(bucket, key, body, content_type=ct)


__all__ = ["publish", "build_plan", "PublishPlan", "PublishError", "VersionConflict"]
