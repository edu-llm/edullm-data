"""``promote(copy_workers=N)`` must produce a byte-identical result to the sequential path.

Promotion is ~2 S3 round-trips per object — one copy, one HEAD for the CRC reference — and both
loops were strictly sequential. At 6,913 objects that is ~13,800 serial calls, which overruns the
60-minute Batch job-def limit. ``publish()`` already had ``hash_workers``/``copy_workers`` for
exactly this wall; ``promote()`` did not, so CLAUDE.md's documented fix for the timeout did not
apply to the second half of the pipeline.

The risk in parallelising is not speed, it is determinism: the CRC reference map feeds the seal, so
if it depended on completion order the seal would depend on thread scheduling and a re-promote
could disagree with itself. These tests assert the observable result is identical, not merely that
the fast path runs.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from edullm_data import publish as P
from edullm_data import validate as V
from edullm_data.s3 import FakeS3

CREATED = "2026-07-30T00:00:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}
TOKENIZER = {
    "repo_id": "allenai/dolma2-tokenizer",
    "revision": "abc123",
    "fingerprint_sha256": "c" * 64,
    "vocab_size": 100278,
    "eos_token_id": 100257,
}


def _publish_and_validate(s3: FakeS3, dsid: str, *, nshards: int = 12):
    d = Path(tempfile.mkdtemp())
    rng = np.random.default_rng(7)
    for i in range(nshards):
        sub = d / "tokens" / f"src{i % 3}" / f"dom{i % 2}"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"train-{i:05d}.u32le.bin").write_bytes(
            rng.integers(1, 100278, size=3000 + i, dtype=np.uint32).tobytes()
        )
    (d / "tokens" / "src0" / "dom0" / "val-00099.u32le.bin").write_bytes(
        rng.integers(1, 100278, size=2000, dtype=np.uint32).tobytes()
    )
    plan = P.publish(
        d,
        dataset_id=dsid,
        purpose="fixture corpus for asserting parallel promotion matches the sequential path",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at=CREATED,
        group_meta={"tokens": {"tokenizer": TOKENIZER}},
        env=ENV,
    )
    res = V.validate_dataset(
        "edullm-landing", f"{dsid}/{plan.version}", s3, data_bucket="edullm-data"
    )
    assert res.ok, [str(v) for v in res.violations]
    return plan, res


def _promote_and_dump(dsid: str, *, workers: int) -> dict[str, bytes]:
    s3 = FakeS3()
    plan, res = _publish_and_validate(s3, dsid)
    V.promote(
        res, s3, data_bucket="edullm-data", landing_bucket="edullm-landing",
        copy_workers=workers,
    )
    return s3.dump("edullm-data")


def test_parallel_promotion_is_byte_identical_to_sequential():
    """THE test. Anything scheduling-dependent shows up as a diff here.

    Same dataset_id on both sides, in separate FakeS3s, so the two buckets are directly
    comparable key-for-key and byte-for-byte — including the seal, which is the object that
    would move if the CRC map depended on completion order.
    """
    seq = _promote_and_dump("pretrain/promotecmp-10b", workers=1)
    par = _promote_and_dump("pretrain/promotecmp-10b", workers=8)

    assert sorted(seq) == sorted(par), "different object sets"
    assert seq == par, "promoted bytes differ between sequential and parallel"
    assert any(k.endswith("_VALIDATED.json") for k in seq), "no seal written — test is vacuous"
    assert sum(k.endswith(".u32le.bin") for k in seq) == 13


def test_the_crc_reference_map_does_not_depend_on_worker_count():
    """The seal's CRC map must be identical at every worker count.

    Note what actually guarantees this, because it is NOT the submission-order collection in
    ``promote``: the seal is serialised with ``canonical_json``, which sorts keys, so dict
    insertion order cannot reach the bytes. Verified by mutation — collecting the futures with
    ``as_completed`` instead of ``pool.map`` leaves every test here passing.

    So this test pins the OBSERVABLE contract (same seal regardless of concurrency) rather than
    the mechanism. The ordered collection stays because relying on a downstream sort to launder
    nondeterminism is fragile: ``crc_reference`` is also returned into ``seal`` as a plain dict,
    and any future consumer that iterates it instead of re-serialising would inherit the order.
    """
    maps = {}
    dsid = "pretrain/crcref-fixture-10b"  # SAME id every time, in separate buckets, so the
    for workers in (1, 4, 16):            # seal bytes are directly comparable
        s3 = FakeS3()
        plan, res = _publish_and_validate(s3, dsid)
        V.promote(res, s3, data_bucket="edullm-data", landing_bucket="edullm-landing",
                  copy_workers=workers)
        seal_bytes = s3.get("edullm-data", f"{dsid}/{plan.version}/_VALIDATED.json")
        maps[workers] = (json.loads(seal_bytes).get("crc64nvme") or {}, seal_bytes)
    assert maps[1][0] == maps[4][0] == maps[16][0]
    assert maps[1][1] == maps[4][1] == maps[16][1], "seal BYTES differ across worker counts"
    assert maps[1][0], "no CRC reference captured at all — the check would be vacuous"
    assert len(maps[1][0]) == 13, "CRC map is missing entries"


def test_crc_pairs_are_collected_in_submission_order():
    """Pin the mechanism directly, since the seal's canonical_json would hide a regression.

    ``promote`` builds ``crc_reference`` from ``pool.map``, which yields in SUBMISSION order.
    A refactor to ``as_completed`` would pass every other test in this file and silently make
    the in-memory map scheduling-dependent.
    """
    import inspect

    src = inspect.getsource(V.promote)
    assert "pool.map(_crc_for, payload_paths)" in src, (
        "the CRC reference is no longer collected in submission order; if that was deliberate, "
        "confirm no consumer iterates crc_reference before re-serialising it"
    )
    assert "as_completed" not in src


def test_every_object_actually_arrives_under_concurrency():
    """A dropped future would silently under-promote; count both ends."""
    s3 = FakeS3()
    plan, res = _publish_and_validate(s3, "pretrain/promotecount-10b", nshards=40)
    V.promote(res, s3, data_bucket="edullm-data", landing_bucket="edullm-landing",
              copy_workers=16)
    landed = s3.dump("edullm-data")
    payload = [k for k in landed if k.endswith(".u32le.bin")]
    assert len(payload) == 41, f"expected 41 payload objects, promoted {len(payload)}"
    assert f"pretrain/promotecount-10b/{plan.version}/_VALIDATED.json" in landed


def test_a_failing_copy_still_raises_when_parallel():
    """pool.map must not swallow an error — a silent partial promote is the worst outcome."""
    s3 = FakeS3()
    plan, res = _publish_and_validate(s3, "pretrain/promoteraise-10b")

    boom = {"n": 0}
    real_copy = s3.copy

    def flaky(src_b, src_k, dst_b, dst_k):
        boom["n"] += 1
        if boom["n"] == 5:
            raise OSError("simulated S3 failure mid-promotion")
        return real_copy(src_b, src_k, dst_b, dst_k)

    s3.copy = flaky  # type: ignore[method-assign]
    with pytest.raises(OSError):
        V.promote(res, s3, data_bucket="edullm-data", landing_bucket="edullm-landing",
                  copy_workers=8)


def test_default_is_still_sequential():
    """Callers that never pass the arg get exactly the old behaviour."""
    import inspect

    assert inspect.signature(V.promote).parameters["copy_workers"].default == 1
