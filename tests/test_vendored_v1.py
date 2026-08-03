"""Tests for the ``vendored/v1`` profile and raw-mirror publication path."""

from __future__ import annotations

import hashlib
from pathlib import Path

from edullm_data import publish as P
from edullm_data import validate as V
from edullm_data.manifest import Format, ManifestEntry, build_manifest
from edullm_data.profiles import vendored_v1
from edullm_data.profiles.base import GroupContext
from edullm_data.profiles.registry import available
from edullm_data.s3 import FakeS3

BUCKET = "edullm-landing"
CREATED = "2026-08-01T12:00:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _entry(path: str, body: bytes) -> ManifestEntry:
    return ManifestEntry(
        path=path,
        sha256=_sha(body),
        bytes=len(body),
        count=None,
        format=Format(container="jsonl", codec="none"),
    )


def _metadata(bodies: dict[str, bytes], **overrides) -> dict:
    root = "raw"
    meta = {
        "profile": vendored_v1.NAME,
        "vendor_root": root,
        "upstream": {
            "name": "openai/prm800k",
            "uri": "https://github.com/openai/prm800k",
            "revision": "0123456789abcdef0123456789abcdef01234567",
            "retrieved_at": CREATED,
        },
        "sentinels": [],
        "upstream_files": [
            {
                "path": path.removeprefix(root + "/"),
                "bytes": len(body),
                "sha256": _sha(body),
            }
            for path, body in sorted(bodies.items())
        ],
    }
    meta.update(overrides)
    return meta


def _ctx(bodies: dict[str, bytes], *, group: dict | None = None) -> GroupContext:
    s3 = FakeS3()
    for path, body in bodies.items():
        s3.seed(BUCKET, f"vendor/openai-prm800k/v1/{path}", body)
    entries = [_entry(path, body) for path, body in sorted(bodies.items())]
    return GroupContext(
        dataset_id="vendor/openai-prm800k",
        version="v1",
        landing_bucket=BUCKET,
        prefix="vendor/openai-prm800k/v1",
        group=group or _metadata(bodies),
        manifest=build_manifest(entries, group_name="raw"),
        s3=s3,
        rng_seed="f" * 64,
    )


def _codes(ctx: GroupContext) -> set[str]:
    return {violation.code for check in vendored_v1.CHECKS for violation in check(ctx)}


def test_vendored_profile_is_shipped_and_clean_jsonl_mirror_passes():
    bodies = {
        "raw/phase1_train.jsonl": b'{"question":{"problem":"x"},"label":{"steps":[]}}\n',
        "raw/phase2_test.jsonl": b'{"question":{"problem":"y"},"label":{"steps":[]}}\n',
    }
    assert vendored_v1.NAME in available()
    assert _codes(_ctx(bodies)) == set()


def test_vendored_profile_rejects_unapproved_file_hash():
    bodies = {"raw/phase1_train.jsonl": b'{"question":{}}\n'}
    meta = _metadata(bodies)
    meta["upstream_files"][0]["sha256"] = "0" * 64
    assert "upstream-sha256-mismatch" in _codes(_ctx(bodies, group=meta))


def test_vendored_profile_rejects_placeholder_metadata_and_paths_outside_root():
    bodies = {"elsewhere/phase1_train.jsonl": b'{"question":{}}\n'}
    meta = _metadata({"raw/phase1_train.jsonl": b'{"question":{}}\n'})
    meta["vendor_root"] = "TODO-set-this"
    meta["upstream"]["revision"] = "TODO-pin-the-upstream"
    codes = _codes(_ctx(bodies, group=meta))
    assert "invalid-vendor-root" in codes
    assert "invalid-upstream-metadata" in codes


def test_vendored_profile_rejects_malformed_jsonl_sample():
    bodies = {"raw/phase1_train.jsonl": b'{"question":}\n'}
    assert "invalid-jsonl-sample" in _codes(_ctx(bodies))


def test_vendored_profile_treats_an_oversized_first_jsonl_record_as_inconclusive():
    # The profile samples only 64 KiB.  A valid upstream record may be larger than that, so no
    # complete newline-delimited record in the first window is not itself evidence of bad JSONL.
    bodies = {"raw/phase1_train.jsonl": b'{"question":"' + b"x" * (70 * 1024) + b'"}\n'}
    assert _codes(_ctx(bodies)) == set()


def test_vendored_profile_requires_a_declared_sentinel_to_be_mirrored():
    bodies = {"raw/phase1_train.jsonl": b'{"question":{}}\n'}
    meta = _metadata(bodies, sentinels=["_SUCCESS"])
    assert "missing-sentinel" in _codes(_ctx(bodies, group=meta))


def test_publish_does_not_fabricate_vendor_retrieved_at(tmp_path: Path):
    root = tmp_path / "source"
    raw = root / "raw"
    raw.mkdir(parents=True)
    body = b'{"question":{"problem":"x"},"label":{"steps":[]}}\n'
    (raw / "phase1_train.jsonl").write_bytes(body)
    group_meta = _metadata({"raw/phase1_train.jsonl": body})
    del group_meta["upstream"]["retrieved_at"]

    s3 = FakeS3()
    plan = P.publish(
        root,
        dataset_id="vendor/openai-prm800k",
        purpose="Byte-preserving OpenAI PRM800K mirror for provenance and later derived datasets",
        profile=vendored_v1.NAME,
        s3=s3,
        created_at=CREATED,
        env=ENV,
        group_meta={"raw": group_meta},
        license={"id": "MIT", "basis": "inherited"},
    )
    result = V.validate_dataset(
        "edullm-landing",
        f"vendor/openai-prm800k/{plan.version}",
        s3,
        data_bucket="edullm-data",
    )
    assert not result.ok
    assert "invalid-upstream-retrieved-at" in {
        violation.code for violation in result.violations
    }


def test_publish_allocates_after_a_published_catalog_version(tmp_path: Path):
    root = tmp_path / "source"
    raw = root / "raw"
    raw.mkdir(parents=True)
    body = b'{"question":{"problem":"x"},"label":{"steps":[]}}\n'
    (raw / "phase1_train.jsonl").write_bytes(body)

    s3 = FakeS3()
    s3.seed("edullm-data", "_catalog/vendor/openai-prm800k/v3.json", b"{}")
    plan = P.publish(
        root,
        dataset_id="vendor/openai-prm800k",
        purpose="Byte-preserving OpenAI PRM800K mirror for provenance and later derived datasets",
        profile=vendored_v1.NAME,
        s3=s3,
        created_at=CREATED,
        env=ENV,
        group_meta={"raw": _metadata({"raw/phase1_train.jsonl": body})},
        license={"id": "MIT", "basis": "inherited"},
    )
    assert plan.version == "v4"
