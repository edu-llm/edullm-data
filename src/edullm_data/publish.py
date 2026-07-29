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

import gzip
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import (
    NamingError,
    SCHEMA_VERSION,
    canonical_json,
    sha256_bytes,
    validate_dataset_id,
    validate_purpose,
)
from .manifest import (
    EXTENSION_FORMAT,
    Format,
    ManifestEntry,
    build_manifest,
    manifest_sha256,
)
from .s3 import S3, NotFound

LANDING_BUCKET = "edullm-landing"
FAMILIES_DIR = Path(__file__).resolve().parent.parent.parent / "families"


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
        if path.endswith(ext):
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


def _count_for(path: str, body: bytes, fmt: Format) -> dict[str, Any] | None:
    """Best-effort count. Fixed-width raw → tokens (bytes / dtype_size). Line formats →
    rows. Everything else → omit (a tar part or sentinel has no honest count, §5)."""
    if fmt.container == "raw" and fmt.dtype_size:
        if len(body) % fmt.dtype_size != 0:
            # let the validator's arithmetic gate report this precisely; still declare it
            return {"unit": "tokens", "value": len(body) // fmt.dtype_size}
        return {"unit": "tokens", "value": len(body) // fmt.dtype_size}
    if path.endswith(".jsonl"):
        return {"unit": "rows", "value": body.count(b"\n") if body else 0}
    if path.endswith(".jsonl.gz"):
        try:
            return {"unit": "rows", "value": gzip.decompress(body).count(b"\n")}
        except OSError:
            return None
    return None


# --------------------------------------------------------------------------------------
# source enumeration
# --------------------------------------------------------------------------------------


def _enumerate_local(source: Path) -> list[tuple[str, bytes]]:
    """(group-relative path, body) for every file under source, sorted, excluding control
    files a prior partial publish may have dropped."""
    out: list[tuple[str, bytes]] = []
    for p in sorted(source.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(source).as_posix()
        base = rel.rsplit("/", 1)[-1]
        if base in {"dataset.json", "manifest.json", "_SUCCESS", "_VALIDATED.json", "_REJECTED.json"}:
            continue
        out.append((rel, p.read_bytes()))
    return out


def _enumerate_s3(s3: S3, bucket: str, prefix: str) -> list[tuple[str, bytes]]:
    prefix = prefix.strip("/")
    out: list[tuple[str, bytes]] = []
    for obj in sorted(s3.list(bucket, prefix + "/"), key=lambda o: o["key"]):
        key = obj["key"]
        rel = key[len(prefix) + 1 :]
        base = rel.rsplit("/", 1)[-1]
        if base in {"dataset.json", "manifest.json", "_SUCCESS", "_VALIDATED.json", "_REJECTED.json"}:
            continue
        out.append((rel, s3.get(bucket, key)))
    return out


def _group_of(rel_path: str) -> str:
    """The group a payload file belongs to = its first path segment (tokens/, sidecars/…)."""
    return rel_path.split("/", 1)[0] if "/" in rel_path else ""


# --------------------------------------------------------------------------------------
# build the plan
# --------------------------------------------------------------------------------------


def build_plan(
    files: Sequence[tuple[str, bytes]],
    *,
    dataset_id: str,
    version: str,
    purpose: str,
    profile: str | Mapping[str, str],
    family: dict[str, Any],
    created_at: str,
    build_executor: dict[str, Any],
    source_kind: str,
    owner: str | None = None,
    group_meta: Mapping[str, Mapping[str, Any]] | None = None,
) -> PublishPlan:
    """Pure: turn (files, identity, family) into the exact objects to write. No S3."""
    defaults = family.get("defaults", {})
    group_meta = group_meta or {}

    # group files by their first path segment
    by_group: dict[str, list[tuple[str, bytes]]] = {}
    for rel, body in files:
        g = _group_of(rel)
        if not g:
            raise PublishError(
                f"payload file {rel!r} is not under a group prefix; every object must live "
                f"under <group>/… so its profile is unambiguous (§4)"
            )
        by_group.setdefault(g, []).append((rel, body))

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
        entries: list[ManifestEntry] = []
        for rel, body in sorted(by_group[g], key=lambda t: t[0]):
            fmt = _format_for(rel, defaults)
            entries.append(
                ManifestEntry(
                    path=rel,
                    sha256=sha256_bytes(body),
                    bytes=len(body),
                    count=_count_for(rel, body, fmt),
                    format=fmt,
                )
            )
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
        # merge profile-specific metadata: family tokenizer default, then explicit group_meta
        if defaults.get("tokenizer") and profile_for(g).startswith("pretrain-tokens/"):
            gm["tokenizer"] = defaults["tokenizer"]
        if defaults.get("partitions") and "partitions" not in group_meta.get(g, {}):
            # The family default declares the partition SHAPE (name + by:path glob) but
            # cannot know rows — that count is only knowable once the shards exist. Fill it
            # in now from the manifest we just built, so Gate A's "every partition declares
            # rows" holds. Path partitions with a glob matching no shard are dropped, not
            # shipped empty (a dataset may only carry a train split, not the family's full set).
            gm["partitions"] = _resolve_path_partitions(defaults["partitions"], entries)
            gm["coverage"] = defaults.get("coverage", "partition")
        gm.update(group_meta.get(g, {}))
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
        "sources": family.get("sources", []),
        "build": {
            "executor": build_executor,
            "reproducibility": defaults.get("reproducibility", "logical"),
        },
        "license": family.get("license", {"id": None, "basis": "unknown"}),
    }

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


def _resolve_path_partitions(
    templates: Sequence[Mapping[str, Any]], entries: Sequence[ManifestEntry]
) -> list[dict[str, Any]]:
    """Turn family-default path-partition templates (name + glob, no rows) into concrete
    partitions with rows counted from the manifest. Only ``by: path`` templates can be
    auto-resolved here; a partition with no matching shard is dropped rather than shipped
    with rows=0, because a dataset legitimately may not contain every split the family
    anticipates. Non-path templates are passed through untouched — their rows must come
    from group_meta, and Gate A will flag any that still lack a count."""
    import fnmatch

    resolved: list[dict[str, Any]] = []
    for tmpl in templates:
        if tmpl.get("by") != "path":
            resolved.append(dict(tmpl))
            continue
        glob = tmpl.get("glob", "")
        matched = [
            e for e in entries
            if fnmatch.fnmatch(e.path.rsplit("/", 1)[-1], glob) or fnmatch.fnmatch(e.path, glob)
        ]
        if not matched:
            continue  # this split isn't present in this dataset
        rows = 0
        for e in matched:
            if e.count and e.count.get("unit") in {"tokens", "rows", "items"}:
                rows += int(e.count["value"])
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


def publish(
    source: str | Path,
    *,
    dataset_id: str,
    purpose: str,
    profile: str | Mapping[str, str],
    s3: S3,
    created_at: str,
    landing_bucket: str = LANDING_BUCKET,
    owner: str | None = None,
    group_meta: Mapping[str, Mapping[str, Any]] | None = None,
    build_executor: dict[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    max_version_attempts: int = 8,
) -> PublishPlan:
    """Publish a dataset to landing. Returns the plan that was written.

    ``s3`` and ``created_at`` are injected so the whole thing is testable and deterministic;
    the CLI supplies a real client and an ISO timestamp.
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
    build_executor = build_executor or _build_executor_from_env(env)

    # enumerate source bytes
    src_str = str(source)
    if src_str.startswith("s3://"):
        rest = src_str[len("s3://") :]
        bkt, _, pfx = rest.partition("/")
        files = _enumerate_s3(s3, bkt, pfx)
        source_kind = "s3"
    else:
        files = _enumerate_local(Path(source))
        source_kind = "local"
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
            owner=owner,
            group_meta=group_meta,
        )
        ds_prefix = f"{dataset_id}/{version}"
        # 1. reserve the version: create-only dataset.json FIRST (§6 order)
        try:
            _put_create_only(s3, landing_bucket, f"{ds_prefix}/dataset.json", canonical_json(plan.dataset_json))
        except FileExistsError as e:
            last_err = e
            continue  # someone took this version; bump and retry

        # 2. payload objects (idempotent: skip if identical already present)
        if source_kind == "local":
            file_by_rel = dict(files)
            for rel, body in files:
                _put_idempotent(s3, landing_bucket, f"{ds_prefix}/{rel}", body)
        else:
            # already in landing under the same prefix? copy within landing if source differs
            src_bkt = src_str[len("s3://") :].split("/", 1)[0]
            src_pfx = src_str[len("s3://") :].split("/", 1)[1].strip("/") if "/" in src_str[len("s3://"):] else ""
            for rel, body in files:
                dst = f"{ds_prefix}/{rel}"
                if not (src_bkt == landing_bucket and f"{src_pfx}/{rel}" == dst):
                    _put_idempotent(s3, landing_bucket, dst, body)

        # 3. group manifests LAST — the commit point (§6)
        for g, man in plan.manifests.items():
            _put_idempotent(s3, landing_bucket, f"{ds_prefix}/{g}/manifest.json", canonical_json(man))

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
