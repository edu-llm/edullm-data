"""Profile ``vendored/v1`` — a byte-preserving third-party mirror.

The profile exists for a deliberately narrow claim: the payload is an upstream tree whose
paths and file identities are preserved.  It does *not* make the payload train-ready and it
does not invent a schema for the upstream records.

The producer is responsible for hashing the staged bytes and comparing them with the upstream
release before it writes a manifest.  Unlike ordinary profiles, this profile also stream-hashes
the landing payload at Gate A: its essential claim is that the copied bytes are the upstream
tree, and an untrusted mutable landing prefix cannot establish that claim through metadata alone.
"""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any, Mapping

from ..manifest import ManifestEntry
from .base import GroupContext, Violation

NAME = "vendored/v1"

REQUIRED_FIELDS: Mapping[str, Any] = {
    "vendor_root": {"type": "string"},
    "upstream": {
        "type": "object",
        "required": ["name", "uri", "revision", "retrieved_at"],
    },
    "sentinels": {"type": "array"},
    # Immutable upstream witnesses.  Each is a path *relative to vendor_root* plus the
    # expected source byte count and SHA-256.
    "upstream_files": {"type": "array"},
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_JSONL_SAMPLE_BYTES = 64 * 1024
_MAX_JSONL_LINES = 16


def _object_key(prefix: str, path: str) -> str:
    return f"{prefix.rstrip('/')}/{path.lstrip('/')}" if prefix else path


def _is_placeholder(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    normalized = value.strip().lower()
    return (
        not normalized
        or "todo" in normalized
        or normalized in {"unknown", "tbd", "none", "null"}
    )


def _vendor_root(ctx: GroupContext) -> str | None:
    root = ctx.group.get("vendor_root")
    if not isinstance(root, str):
        return None
    root = root.strip().strip("/")
    if _is_placeholder(root) or root in {".", ".."} or ".." in root.split("/"):
        return None
    return root


def _entries(ctx: GroupContext) -> tuple[list[ManifestEntry], list[Violation]]:
    entries: list[ManifestEntry] = []
    violations: list[Violation] = []
    for raw in ctx.manifest.get("entries", []):
        try:
            entries.append(ManifestEntry.from_dict(raw))
        except Exception as exc:  # noqa: BLE001 - surface malformed control data as a gate failure
            path = raw.get("path") if isinstance(raw, Mapping) else None
            violations.append(
                Violation(
                    "bad-manifest-entry", f"unparseable manifest entry: {exc}", path
                )
            )
    return entries, violations


def _relative_to_root(path: str, root: str) -> str | None:
    prefix = root + "/"
    if not path.startswith(prefix):
        return None
    return path[len(prefix) :]


def _valid_timestamp(value: Any) -> bool:
    if _is_placeholder(value):
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def check_vendor_metadata(ctx: GroupContext) -> list[Violation]:
    """Recompute that metadata describes the actual vendored tree, not placeholders.

    ``REQUIRED_FIELDS`` is intentionally not a generic schema gate today, so this check
    validates the fields itself.  The root test compares every real manifest path against the
    declared root; a plausible-looking ``vendor_root`` that points somewhere else is rejected.
    """
    out: list[Violation] = []
    root = _vendor_root(ctx)
    if root is None:
        out.append(
            Violation(
                "invalid-vendor-root",
                "vendored/v1 requires a non-placeholder relative vendor_root without '..' segments",
            )
        )

    upstream = ctx.group.get("upstream")
    if not isinstance(upstream, Mapping):
        out.append(
            Violation(
                "missing-upstream", "vendored/v1 requires an upstream metadata object"
            )
        )
    else:
        for key in ("name", "uri", "revision"):
            if _is_placeholder(upstream.get(key)):
                out.append(
                    Violation(
                        "invalid-upstream-metadata",
                        f"upstream.{key} must be a concrete, non-placeholder value",
                    )
                )
        if not _valid_timestamp(upstream.get("retrieved_at")):
            out.append(
                Violation(
                    "invalid-upstream-retrieved-at",
                    "upstream.retrieved_at must be an ISO-8601 timestamp with timezone",
                )
            )

    sentinels = ctx.group.get("sentinels")
    if not isinstance(sentinels, list) or any(
        _is_placeholder(item) for item in sentinels
    ):
        out.append(
            Violation(
                "invalid-sentinels",
                "vendored/v1 requires sentinels to be a list of concrete upstream marker paths (or [])",
            )
        )
    elif len({str(item) for item in sentinels}) != len(sentinels):
        out.append(
            Violation(
                "duplicate-sentinel",
                "sentinels contains duplicate upstream marker paths",
            )
        )

    entries, entry_violations = _entries(ctx)
    out.extend(entry_violations)
    if root is not None:
        entry_paths = {entry.path for entry in entries}
        for entry in entries:
            if _relative_to_root(entry.path, root) is None:
                out.append(
                    Violation(
                        "vendor-root-mismatch",
                        f"{entry.path!r} is outside declared vendor_root {root!r}",
                        entry.path,
                    )
                )
        # Sentinels are optional because many upstream releases have none, but a declared one
        # must be a real member of the mirrored tree.  Otherwise a list of plausible marker
        # names is metadata decoration rather than a check against the staged bytes.
        if isinstance(sentinels, list):
            for sentinel in sentinels:
                if not isinstance(sentinel, str):
                    continue
                path = sentinel.strip().strip("/")
                if not path or ".." in path.split("/"):
                    out.append(
                        Violation(
                            "invalid-sentinel-path",
                            "sentinel paths must be non-empty relative paths without '..' segments",
                            str(sentinel),
                        )
                    )
                    continue
                full_path = f"{root}/{path}"
                if full_path not in entry_paths:
                    out.append(
                        Violation(
                            "missing-sentinel",
                            f"declared upstream sentinel {path!r} is absent from the mirrored tree",
                            full_path,
                        )
                    )
    return out


def check_upstream_file_witnesses(ctx: GroupContext) -> list[Violation]:
    """Compare the manifest to the immutable upstream file witness list.

    The witness list is independently fixed before transfer (for PRM800K, OpenAI's Git-LFS
    OIDs).  This catches a producer that silently changes paths, drops a source file, or emits
    a manifest whose declared size/hash does not correspond to the approved upstream release.
    """
    root = _vendor_root(ctx)
    raw_witnesses = ctx.group.get("upstream_files")
    if root is None:
        return []
    if not isinstance(raw_witnesses, list) or not raw_witnesses:
        return [
            Violation(
                "missing-upstream-file-witnesses",
                "vendored/v1 requires a non-empty upstream_files witness list",
            )
        ]

    expected: dict[str, tuple[int, str]] = {}
    out: list[Violation] = []
    for witness in raw_witnesses:
        if not isinstance(witness, Mapping):
            out.append(
                Violation(
                    "bad-upstream-file-witness",
                    "each upstream_files item must be an object",
                )
            )
            continue
        path = witness.get("path")
        size = witness.get("bytes")
        digest = witness.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or ".." in path.split("/")
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
        ):
            out.append(
                Violation(
                    "bad-upstream-file-witness",
                    "each upstream witness needs relative path, non-negative bytes, and lowercase SHA-256",
                    str(path) if isinstance(path, str) else None,
                )
            )
            continue
        full_path = f"{root}/{path}"
        if full_path in expected:
            out.append(
                Violation(
                    "duplicate-upstream-file-witness",
                    f"duplicate witness for {path!r}",
                    full_path,
                )
            )
            continue
        expected[full_path] = (size, digest)

    entries, entry_violations = _entries(ctx)
    out.extend(entry_violations)
    actual = {entry.path: entry for entry in entries}
    for path in sorted(set(expected) - set(actual)):
        out.append(
            Violation(
                "missing-upstream-file",
                f"upstream witness {path!r} is absent from the manifest",
                path,
            )
        )
    for path in sorted(set(actual) - set(expected)):
        out.append(
            Violation(
                "unwitnessed-vendor-file",
                f"manifest entry {path!r} has no approved upstream file witness",
                path,
            )
        )
    for path in sorted(set(expected) & set(actual)):
        expected_size, expected_digest = expected[path]
        entry = actual[path]
        if entry.bytes != expected_size:
            out.append(
                Violation(
                    "upstream-size-mismatch",
                    f"manifest bytes={entry.bytes} but the approved upstream witness declares {expected_size}",
                    path,
                )
            )
        if entry.sha256 != expected_digest:
            out.append(
                Violation(
                    "upstream-sha256-mismatch",
                    "manifest SHA-256 does not match the approved upstream file witness",
                    path,
                )
            )
        # Landing is intentionally an untrusted, mutable airlock.  Comparing a manifest with
        # witness metadata alone proves only that two documents agree; stream-hash the object
        # Gate A is about to admit so a same-size replacement cannot ride through on a stale
        # manifest digest.  Capture an ETag before and after the stream as well.  Promotion
        # passes that stable ETag as CopySourceIfMatch, binding its later server-side copy to
        # the exact landing object this check observed rather than merely detecting a race
        # after writing an unsealed destination object.
        try:
            before = ctx.s3.head(ctx.landing_bucket, _object_key(ctx.prefix, path))
            actual_digest, actual_size = ctx.s3.hash_object(
                ctx.landing_bucket, _object_key(ctx.prefix, path)
            )
            after = ctx.s3.head(ctx.landing_bucket, _object_key(ctx.prefix, path))
        except Exception as exc:  # noqa: BLE001 - turn storage errors into a normal gate failure
            out.append(
                Violation(
                    "upstream-payload-hash-read-failed",
                    f"could not stream-hash vendored payload: {exc}",
                    path,
                )
            )
            continue
        etag = after.get("etag")
        if (
            before.get("size") != actual_size
            or after.get("size") != actual_size
            or not isinstance(before.get("etag"), str)
            or not isinstance(etag, str)
            or before.get("etag") != etag
        ):
            out.append(
                Violation(
                    "upstream-payload-changed-during-validation",
                    "payload changed while Gate A was stream-hashing it; retry after the producer stops mutating landing",
                    path,
                )
            )
            continue
        # This is a validator-local observation, never a producer-provided assertion.  The
        # orchestrator carries it in its in-memory promotion snapshot; it is intentionally not
        # written into dataset.json or a manifest where an airlock producer could edit it.
        observed = ctx.observations.setdefault("vendored_payloads", {})
        if isinstance(observed, dict):
            observed[path] = {
                "bytes": actual_size,
                "sha256": actual_digest,
                "etag": etag,
            }
        if actual_size != expected_size:
            out.append(
                Violation(
                    "upstream-payload-size-mismatch",
                    f"payload bytes={actual_size} but the approved upstream witness declares {expected_size}",
                    path,
                )
            )
        if actual_digest != expected_digest:
            out.append(
                Violation(
                    "upstream-payload-sha256-mismatch",
                    "payload SHA-256 does not match the approved upstream file witness",
                    path,
                )
            )
    return out


def check_jsonl_samples(ctx: GroupContext) -> list[Violation]:
    """Parse bounded first-record samples of plain JSONL payloads.

    This is intentionally a format plausibility check, not a semantic interpretation of a
    third-party corpus.  It reads at most 64 KiB per plain ``.jsonl`` file and only complete
    lines, avoiding a whole-object read of a large vendor file.
    """
    entries, out = _entries(ctx)
    for entry in entries:
        if not entry.path.lower().endswith(".jsonl"):
            continue
        try:
            sample = ctx.s3.get_range(
                ctx.landing_bucket,
                _object_key(ctx.prefix, entry.path),
                0,
                min(entry.bytes, _JSONL_SAMPLE_BYTES),
            )
        except Exception as exc:  # noqa: BLE001 - translate provider failures into a gate violation
            out.append(
                Violation(
                    "jsonl-sample-read-failed",
                    f"could not read JSONL sample: {exc}",
                    entry.path,
                )
            )
            continue

        sample_covers_object = len(sample) >= entry.bytes
        complete_lines = sample.splitlines()
        if sample and not sample_covers_object and not sample.endswith((b"\n", b"\r")):
            complete_lines = complete_lines[:-1]
        parsed = 0
        for line in complete_lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                out.append(
                    Violation(
                        "invalid-jsonl-sample",
                        f"invalid JSONL record: {exc}",
                        entry.path,
                    )
                )
                break
            if not isinstance(record, dict):
                out.append(
                    Violation(
                        "jsonl-record-not-object",
                        "JSONL records must be JSON objects",
                        entry.path,
                    )
                )
                break
            parsed += 1
            if parsed >= _MAX_JSONL_LINES:
                break
        # A valid JSON object may be larger than the bounded sample.  When no complete record
        # fits inside the first window the check is inconclusive, not a reason to reject a
        # byte-preserving mirror.  If the sample covered the whole object, though, zero records
        # is a real empty/malformed-file finding.
        if (
            entry.bytes > 0
            and sample_covers_object
            and parsed == 0
            and not any(v.path == entry.path for v in out)
        ):
            out.append(
                Violation(
                    "empty-jsonl-sample",
                    "the first JSONL sample contained no complete non-empty object record",
                    entry.path,
                )
            )
    return out


CHECKS = [
    check_vendor_metadata,
    check_upstream_file_witnesses,
    check_jsonl_samples,
]


import sys  # noqa: E402

try:
    from . import registry as _registry

    _registry.register(sys.modules[__name__])
except Exception:
    pass
