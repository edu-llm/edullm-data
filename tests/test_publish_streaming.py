"""Prove publish() never holds a payload whole in the caller — the fix for TB-scale sources.

The guarantee: no dataset byte transits the client. publish() hashes by streaming from S3
(s3.hash_object), moves payload by server-side copy (s3.copy), and only ever reads whole
objects for small control files. These tests instrument a FakeS3 subclass to FAIL if a
payload object is ever fetched whole via get(), which is what the old (path, bytes) path did.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from edullm_data import publish as P
from edullm_data import validate as V
from edullm_data.s3 import FakeS3

CREATED = "2026-07-29T01:00:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}
TOK_META = {"tokens": {"tokenizer": {"repo_id": "r", "revision": "abc", "fingerprint_sha256": "c" * 64, "vocab_size": 100278, "eos_token_id": 100257}}}


class NoWholePayloadGetS3(FakeS3):
    """FakeS3 that forbids get() on a raw token payload — those must be hashed by streaming
    (hash_object) and moved by copy(), never pulled whole. get() on json/jsonl control or
    small text is fine."""

    def get(self, bucket: str, key: str) -> bytes:
        if key.endswith(".u32le.bin") or key.endswith(".u16le.bin"):
            raise AssertionError(f"payload fetched whole via get(): {key} — must stream/copy")
        return super().get(bucket, key)


def _tokens_dir(n: int = 80000) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    (d / "tokens" / "train-00000.u32le.bin").write_bytes((np.arange(1, n + 1, dtype=np.uint32) % 90000).tobytes())
    (d / "tokens" / "train-00001.u32le.bin").write_bytes((np.arange(1, n // 2, dtype=np.uint32) % 90000).tobytes())
    # A val shard: the pretrain family now requires held-out data
    # (families/pretrain.json validation_required=true), so a train-only
    # corpus is a missing-required-split violation.
    (d / "tokens" / "val-00000.u32le.bin").write_bytes((np.arange(1, n // 4, dtype=np.uint32) % 90000).tobytes())
    return d


def test_publish_never_gets_payload_whole_local_source():
    s3 = NoWholePayloadGetS3()
    plan = P.publish(
        _tokens_dir(),
        dataset_id="pretrain/dolma2-150b",
        purpose="150B-token Dolma2 mix for 370M ladder pretraining at 1.25xC",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at=CREATED,
        group_meta=TOK_META,
        env=ENV,
    )
    # if we got here, no .u32le.bin was ever get()-whole. And it still validates:
    r = V.validate_dataset("edullm-landing", f"pretrain/dolma2-150b/{plan.version}", s3, data_bucket="edullm-data")
    assert r.ok, [str(v) for v in r.violations]


def test_publish_never_gets_payload_whole_s3_source():
    s3 = NoWholePayloadGetS3()
    # stage a token dataset straight into landing (the AWS-native / migration case)
    for i, n in [(0, 80000), (1, 40000)]:
        body = (np.arange(1, n + 1, dtype=np.uint32) % 90000).tobytes()
        s3.seed("edullm-landing", f"_pending/mig/tokens/train-{i:05d}.u32le.bin", body)
    # val shard: pretrain requires held-out data (validation_required=true)
    val = (np.arange(1, 20001, dtype=np.uint32) % 90000).tobytes()
    s3.seed("edullm-landing", "_pending/mig/tokens/val-00000.u32le.bin", val)
    plan = P.publish(
        "s3://edullm-landing/_pending/mig/",
        dataset_id="pretrain/fineweb-edu-10b",
        purpose="10B-token FineWeb-Edu mix for pretraining ablations at 370M scale",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at=CREATED,
        group_meta=TOK_META,
        env=ENV,
    )
    r = V.validate_dataset("edullm-landing", f"pretrain/fineweb-edu-10b/{plan.version}", s3, data_bucket="edullm-data")
    assert r.ok, [str(v) for v in r.violations]


def test_token_count_is_arithmetic_no_read():
    # token count must come from size / dtype_size — never from reading the object
    from edullm_data.manifest import Format

    fmt = Format.for_tokens("uint32")
    # s3/bucket/key are unused for the raw path; pass a poisoned s3 to prove it
    class Boom(FakeS3):
        def get(self, *a, **k):  # noqa: ANN002
            raise AssertionError("must not read for a raw token count")
        def get_range(self, *a, **k):  # noqa: ANN002
            raise AssertionError("must not read for a raw token count")
    c = P._count_for("tokens/x.u32le.bin", 4000, fmt, Boom(), "b", "k")
    assert c == {"unit": "tokens", "value": 1000}
