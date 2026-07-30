"""Four fields that were declared and never recomputed.

"Recompute, never trust" is the standard's governing rule, and a required-but-unchecked field is
what it calls decoration. Each of these was reproduced passing Gate A before the fix:

* ``partition.rows`` — ``rows: 999999999`` on a 60,000-token group validated clean, and
  ``read.dataset_paths`` hands that number to a trainer as ``ResolvedSplit.rows``.
* ``coverage: "partition"`` — two partitions with 100%-overlapping globs validated clean, so
  summing partition rows double-counts. Only the WORD was checked against an enum.
* dataset-level exhaustiveness — the per-group check LISTs each group's own prefix, so an object
  under a top-level prefix belonging to no declared group is listed by nobody. An injected
  ``sneaky/val-00000.u32le.bin`` passed.
* observed vs declared splits — a ``val-*`` shard with no declared ``val`` partition validated
  clean, was unreachable via ``split="val"``, and was returned by an unsplit read. Trainable by
  accident, in both directions at once.

All four are metadata arithmetic plus one LIST: no payload bytes are read.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

from edullm_data import publish as P
from edullm_data import validate as V
from edullm_data.manifest import ManifestEntry
from edullm_data.s3 import FakeS3
from edullm_data.validate import _validate_partitions

CREATED = "2026-07-29T12:00:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}
DSID = "pretrain/recompute-fixture-10b"
TOK = {
    "repo_id": "allenai/dolma2-tokenizer",
    "revision": "abc123",
    "fingerprint_sha256": "c" * 64,
    "vocab_size": 100278,
    "eos_token_id": 100257,
}
FMT = {"container": "raw", "dtype": "uint32", "byte_order": "little", "header_bytes": 0, "codec": "none"}


def _entry(name: str, tokens: int) -> ManifestEntry:
    return ManifestEntry.from_dict({
        "path": f"tokens/{name}", "sha256": "a" * 64, "bytes": tokens * 4,
        "count": {"unit": "tokens", "value": tokens}, "format": FMT,
    })


def _partitions(group: dict) -> list[str]:
    entries = [_entry("train-00000.u32le.bin", 60000), _entry("val-00000.u32le.bin", 20000)]
    v: list = []
    _validate_partitions(group, v, "tokens", {e.path for e in entries}, entries)
    return [x.code for x in v]


# ---- partition.rows is recomputed ----

def test_a_rows_lie_is_caught():
    codes = _partitions({"coverage": "partition", "partitions": [
        {"name": "train", "by": "path", "glob": "train-*.u32le.bin", "rows": 999_999_999},
        {"name": "val", "by": "path", "glob": "val-*.u32le.bin", "rows": 20000},
    ]})
    assert "partition-rows-mismatch" in codes


def test_truthful_rows_pass():
    codes = _partitions({"coverage": "partition", "partitions": [
        {"name": "train", "by": "path", "glob": "train-*.u32le.bin", "rows": 60000},
        {"name": "val", "by": "path", "glob": "val-*.u32le.bin", "rows": 20000},
    ]})
    assert codes == []


def test_an_off_by_one_in_rows_is_caught():
    """The check is exact, not approximate — a token budget is not a rounding matter."""
    codes = _partitions({"coverage": "partition", "partitions": [
        {"name": "train", "by": "path", "glob": "train-*.u32le.bin", "rows": 59999},
        {"name": "val", "by": "path", "glob": "val-*.u32le.bin", "rows": 20000},
    ]})
    assert "partition-rows-mismatch" in codes


# ---- coverage is recomputed ----

def test_coverage_partition_with_overlapping_globs_is_caught():
    """This is what makes summing partition rows unsafe — and it used to pass."""
    codes = _partitions({"coverage": "partition", "partitions": [
        {"name": "train", "by": "path", "glob": "*.u32le.bin", "rows": 80000},
        {"name": "val", "by": "path", "glob": "*.u32le.bin", "rows": 80000},
    ]})
    assert "coverage-not-disjoint" in codes


def test_coverage_partition_that_leaves_an_object_unclaimed_is_caught():
    codes = _partitions({"coverage": "partition", "partitions": [
        {"name": "train", "by": "path", "glob": "train-*.u32le.bin", "rows": 60000},
    ]})
    assert "coverage-incomplete" in codes


def test_overlapping_waives_disjointness_by_design():
    """Curriculum replay legitimately revisits the same shards."""
    codes = _partitions({"coverage": "overlapping", "partitions": [
        {"name": "train", "by": "path", "glob": "*.u32le.bin", "rows": 80000},
        {"name": "val", "by": "path", "glob": "*.u32le.bin", "rows": 80000},
    ]})
    assert "coverage-not-disjoint" not in codes


def test_the_leakage_case_is_called_out_by_name():
    """A trainable and a held-out partition sharing objects IS train/test leakage."""
    codes = _partitions({"coverage": "partition", "partitions": [
        {"name": "train", "by": "path", "glob": "*.u32le.bin", "rows": 80000},
        {"name": "val", "by": "path", "glob": "*.u32le.bin", "rows": 80000},
    ]})
    assert "coverage-not-disjoint" in codes
    entries = [_entry("train-00000.u32le.bin", 60000), _entry("val-00000.u32le.bin", 20000)]
    v: list = []
    _validate_partitions(
        {"coverage": "partition", "partitions": [
            {"name": "train", "by": "path", "glob": "*.u32le.bin", "rows": 80000},
            {"name": "val", "by": "path", "glob": "*.u32le.bin", "rows": 80000},
        ]}, v, "tokens", {e.path for e in entries}, entries,
    )
    msg = next(str(x) for x in v if x.code == "coverage-not-disjoint")
    assert "leakage" in msg


# ---- dataset-level sweep ----

def _publish(s3: FakeS3, *, extra: dict[str, bytes] | None = None, partitions=None):
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    ids = (np.arange(1, 60001) % 40000).astype(np.uint32) + 1
    (d / "tokens" / "train-00000.u32le.bin").write_bytes(ids.tobytes())
    (d / "tokens" / "val-00000.u32le.bin").write_bytes(ids[:20000].tobytes())
    meta: dict = {"tokenizer": TOK}
    if partitions is not None:
        meta["partitions"] = partitions
        meta["coverage"] = "incomplete"  # isolate the split check from coverage
    plan = P.publish(
        d, dataset_id=DSID,
        purpose="fixture proving the dataset-level sweep sees orphan prefixes and stray splits",
        profile="pretrain-tokens/v1", s3=s3, created_at=CREATED,
        group_meta={"tokens": meta}, env=ENV,
    )
    prefix = f"{DSID}/{plan.version}"
    for key, body in (extra or {}).items():
        s3.put("edullm-landing", f"{prefix}/{key}", body)
    return V.validate_dataset("edullm-landing", prefix, s3, data_bucket="edullm-data")


def test_an_object_in_no_declared_group_is_caught():
    """V8: a group-scoped LIST cannot see a top-level prefix nobody declared."""
    res = _publish(FakeS3(), extra={"sneaky/val-00000.u32le.bin": np.arange(1, 101, dtype=np.uint32).tobytes()})
    assert "unlisted-object-dataset-level" in {v.code for v in res.violations}
    assert not res.ok


def test_a_clean_dataset_has_no_orphans():
    res = _publish(FakeS3())
    assert res.ok, [str(v) for v in res.violations]


def test_a_shard_whose_split_is_not_declared_is_caught():
    """The silent hole: unreachable via split= AND formerly returned by an unsplit read."""
    res = _publish(FakeS3(), partitions=[{"name": "train", "by": "path", "glob": "train-*.u32le.bin"}])
    codes = {v.code for v in res.violations}
    assert "undeclared-split" in codes
    msg = next(str(v) for v in res.violations if v.code == "undeclared-split")
    assert "val-00000" in msg
    assert "trainable by accident" in msg


def test_a_declared_split_with_no_matching_object_is_caught():
    """The other direction: a reader asking for it would get silence."""
    s3 = FakeS3()
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    ids = (np.arange(1, 60001) % 40000).astype(np.uint32) + 1
    (d / "tokens" / "train-00000.u32le.bin").write_bytes(ids.tobytes())
    plan = P.publish(
        d, dataset_id=DSID,
        purpose="fixture proving the dataset-level sweep sees orphan prefixes and stray splits",
        profile="pretrain-tokens/v1", s3=s3, created_at=CREATED,
        group_meta={"tokens": {
            "tokenizer": TOK,
            # claim a test split with no test-* shards anywhere
            "partitions": [
                {"name": "train", "by": "path", "glob": "train-*.u32le.bin", "rows": 60000},
                {"name": "test", "by": "path", "glob": "test-*.u32le.bin", "rows": 0},
            ],
            "coverage": "incomplete",
        }}, env=ENV,
    )
    res = V.validate_dataset("edullm-landing", f"{DSID}/{plan.version}", s3, data_bucket="edullm-data")
    codes = {v.code for v in res.violations}
    assert "empty-split" in codes or "partition-glob-empty" in codes, codes
