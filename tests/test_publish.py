"""publish() tests. The load-bearing ones are round-trips: a dataset produced by publish()
must pass Gate A. If the producer and the validator ever disagree, that's the bug that
matters, so most of these publish then validate."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from edullm_data import publish as P
from edullm_data import validate as V
from edullm_data.s3 import FakeS3

CREATED = "2026-07-28T23:00:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}


def _eval_dir() -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "results").mkdir()
    rows = b"".join(b'{"status":"ok","score":%d}\n' % (i % 5) for i in range(200))
    (d / "results" / "eval-00000.jsonl").write_bytes(rows)
    return d


def _eval_meta() -> dict:
    return {
        "results": {
            "model": {"id": "qwen/Qwen2.5-0.5B", "revision": "abc"},
            "task": "arc",
            "temperature": 0.0,
            "top_p": 1.0,
            "max_tokens": 8,
            "status_counts": {"ok": 200, "error": 0, "filtered": 0},
        }
    }


def _tokens_dir(n: int = 60000) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    (d / "tokens" / "train-00000.u32le.bin").write_bytes((np.arange(1, n + 1, dtype=np.uint32) % 90000).tobytes())
    # val shard: pretrain now requires held-out data (validation_required=true)
    (d / "tokens" / "val-00000.u32le.bin").write_bytes((np.arange(1, n // 3, dtype=np.uint32) % 90000).tobytes())
    return d


def _tokens_meta() -> dict:
    return {
        "tokens": {
            "tokenizer": {
                "repo_id": "allenai/dolma2-tokenizer",
                "revision": "abc123",
                "fingerprint_sha256": "c" * 64,
                "vocab_size": 100278,
                "eos_token_id": 100257,
            }
        }
    }


# --------------------------------------------------------------------------------------
# round-trips — the real integration tests
# --------------------------------------------------------------------------------------


def test_eval_publish_then_validate():
    s3 = FakeS3()
    plan = P.publish(
        _eval_dir(),
        dataset_id="eval/mcq-arc",
        purpose="ARC/OpenBookQA/SciQ loglikelihood scores for 23 baseline models, for the baseline table",
        profile="eval-results/v1",
        s3=s3,
        created_at=CREATED,
        group_meta=_eval_meta(),
        env=ENV,
    )
    assert plan.version == "v1"
    r = V.validate_dataset("edullm-landing", f"{plan.dataset_id}/{plan.version}", s3, data_bucket="edullm-data")
    assert r.ok, [str(v) for v in r.violations]


def test_pretrain_publish_then_validate_partition_rows_computed():
    s3 = FakeS3()
    plan = P.publish(
        _tokens_dir(),
        dataset_id="pretrain/dolma2-150b",
        purpose="150B-token Dolma2 mix for 370M ladder pretraining at 1.25xC",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at=CREATED,
        group_meta=_tokens_meta(),
        env=ENV,
    )
    r = V.validate_dataset("edullm-landing", f"pretrain/dolma2-150b/{plan.version}", s3, data_bucket="edullm-data")
    assert r.ok, [str(v) for v in r.violations]
    ds = json.loads(s3.get("edullm-landing", f"pretrain/dolma2-150b/{plan.version}/dataset.json"))
    part = ds["groups"][0]["partitions"][0]
    assert part["name"] == "train" and part["rows"] == 60000  # computed from the manifest


def _multishard_tokens_dir(shards: int = 6) -> Path:
    """A multi-shard token group with varied per-shard sizes, so parallel hashing/copying
    has something to reorder if it were going to."""
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    for i in range(shards):
        n = 40000 + i * 7000  # distinct sizes per shard
        arr = (np.arange(1, n + 1, dtype=np.uint32) % 90000)
        (d / "tokens" / f"train-{i:05d}.u32le.bin").write_bytes(arr.tobytes())
    # val shard: pretrain requires held-out data (validation_required=true)
    (d / "tokens" / "val-00000.u32le.bin").write_bytes((np.arange(1, 20001, dtype=np.uint32) % 90000).tobytes())
    return d


def test_parallel_workers_produce_identical_manifest_and_validate():
    """hash_workers/copy_workers > 1 must yield a byte-identical dataset.json + manifest to
    the sequential path (executor.map preserves submission order), and the result must still
    pass Gate A. Regression for the parallelism added to unblock the 218-shard/125GB olmo
    migration, which timed out hashing/copying single-threaded."""
    src = _multishard_tokens_dir()

    def run(hw: int, cw: int) -> tuple[bytes, bytes]:
        s3 = FakeS3()
        plan = P.publish(
            src,
            dataset_id="pretrain/olmo-mix-1124-31b",
            purpose="OLMo-mix-1124 token corpus for 370M ladder pretraining, parallel-publish regression",
            profile="pretrain-tokens/v1",
            s3=s3,
            created_at=CREATED,
            group_meta=_tokens_meta(),
            env=ENV,
            hash_workers=hw,
            copy_workers=cw,
        )
        r = V.validate_dataset("edullm-landing", f"{plan.dataset_id}/{plan.version}", s3, data_bucket="edullm-data")
        assert r.ok, [str(v) for v in r.violations]
        ds = s3.get("edullm-landing", f"{plan.dataset_id}/{plan.version}/dataset.json")
        man = s3.get("edullm-landing", f"{plan.dataset_id}/{plan.version}/tokens/manifest.json")
        return ds, man

    seq_ds, seq_man = run(1, 1)
    par_ds, par_man = run(8, 8)
    assert par_man == seq_man, "parallel hashing changed the manifest bytes"
    assert par_ds == seq_ds, "parallel publish changed dataset.json bytes"


def test_publish_from_s3_source():
    s3 = FakeS3()
    # stage payload directly in landing (the AWS-native producer case)
    body = (np.arange(1, 40001, dtype=np.uint32) % 90000).tobytes()
    s3.seed("edullm-landing", "_pending/mcq/tokens/train-00000.u32le.bin", body)
    # val shard: pretrain requires held-out data (validation_required=true)
    s3.seed("edullm-landing", "_pending/mcq/tokens/val-00000.u32le.bin", body[: (len(body) // 3 // 4) * 4])
    plan = P.publish(
        "s3://edullm-landing/_pending/mcq/",
        dataset_id="pretrain/fineweb-edu-10b",
        purpose="10B-token FineWeb-Edu mix for pretraining ablations at 370M scale",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at=CREATED,
        group_meta=_tokens_meta(),
        env=ENV,
    )
    r = V.validate_dataset("edullm-landing", f"pretrain/fineweb-edu-10b/{plan.version}", s3, data_bucket="edullm-data")
    assert r.ok, [str(v) for v in r.violations]


# --------------------------------------------------------------------------------------
# descriptive metadata (drives the generated README)
# --------------------------------------------------------------------------------------


def test_descriptive_fields_land_in_dataset_json_and_validate():
    """sources/about/notes/limitations/license passed to publish() are written into dataset.json
    (the README renders from them) and the result still passes Gate A — none of them is a
    validator-required field, so they're purely additive."""
    s3 = FakeS3()
    plan = P.publish(
        _tokens_dir(),
        dataset_id="pretrain/olmo-mix-1124-31b",
        purpose="OLMo-mix-1124 ~31B-token corpus for 370M ladder pretraining on OLMo-core",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at=CREATED,
        group_meta=_tokens_meta(),
        env=ENV,
        sources=[{"name": "DCLM-Baseline", "tokens": 3700000000000, "license": "CC-BY-4.0",
                  "scope": "upstream-full-collection",
                  "uri": "https://huggingface.co/datasets/allenai/olmo-mix-1124"}],
        about="A document-trimmed ~31B-token subset of allenai/olmo-mix-1124.",
        notes="Subset proportions were not separately measured.",
        limitations=[{"kind": "contamination", "benchmark": "gsm8k", "overlap_rate": 0.003}],
        license={"id": "ODC-By-1.0", "basis": "declared"},
    )
    r = V.validate_dataset("edullm-landing", f"{plan.dataset_id}/{plan.version}", s3, data_bucket="edullm-data")
    assert r.ok, [str(v) for v in r.violations]
    ds = json.loads(s3.get("edullm-landing", f"{plan.dataset_id}/{plan.version}/dataset.json"))
    assert ds["sources"][0]["name"] == "DCLM-Baseline"
    assert ds["about"].startswith("A document-trimmed")
    assert ds["notes"].startswith("Subset proportions")
    assert ds["limitations"][0]["kind"] == "contamination"
    assert ds["license"] == {"id": "ODC-By-1.0", "basis": "declared"}


def test_descriptive_fields_default_to_family_inheritance():
    """Omitting the descriptive args must reproduce today's behavior exactly: sources/license
    inherit from the family, and about/notes/limitations are simply absent (not empty)."""
    s3 = FakeS3()
    plan = P.publish(
        _tokens_dir(),
        dataset_id="pretrain/dolma2-150b",
        purpose="150B-token Dolma2 mix for 370M ladder pretraining at 1.25xC",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at=CREATED,
        group_meta=_tokens_meta(),
        env=ENV,
    )
    ds = json.loads(s3.get("edullm-landing", f"{plan.dataset_id}/{plan.version}/dataset.json"))
    fam = P._load_family("pretrain")
    assert ds["sources"] == fam.get("sources", [])
    assert ds["license"] == fam.get("license", {"id": None, "basis": "unknown"})
    assert "about" not in ds and "notes" not in ds and "limitations" not in ds


# --------------------------------------------------------------------------------------
# version allocation
# --------------------------------------------------------------------------------------


def test_version_auto_increments():
    s3 = FakeS3()
    kw = dict(
        dataset_id="pretrain/dolma2-150b",
        purpose="150B-token Dolma2 mix for 370M ladder pretraining at 1.25xC",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at=CREATED,
        group_meta=_tokens_meta(),
        env=ENV,
    )
    assert P.publish(_tokens_dir(), **kw).version == "v1"
    p2 = P.publish(_tokens_dir(), **kw)
    assert p2.version == "v2"
    ds2 = json.loads(s3.get("edullm-landing", "pretrain/dolma2-150b/v2/dataset.json"))
    assert ds2["version"] == {"id": "v2", "relation": "supersedes", "of": "v1"}


def test_republish_same_version_idempotent(monkeypatch):
    # If dataset.json for the picked version is absent but payload partially exists, the
    # create-only guard still reserves cleanly and idempotent puts don't fail.
    s3 = FakeS3()
    d = _tokens_dir()
    kw = dict(
        dataset_id="pretrain/dolma2-150b",
        purpose="150B-token Dolma2 mix for 370M ladder pretraining at 1.25xC",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at=CREATED,
        group_meta=_tokens_meta(),
        env=ENV,
    )
    p1 = P.publish(d, **kw)
    # a second identical publish gets a NEW version (v1 is sealed), never an error
    p2 = P.publish(d, **kw)
    assert (p1.version, p2.version) == ("v1", "v2")


# --------------------------------------------------------------------------------------
# input validation
# --------------------------------------------------------------------------------------


def test_bad_dataset_id_rejected_before_write():
    s3 = FakeS3()
    with pytest.raises(P.PublishError):
        P.publish(
            _eval_dir(),
            dataset_id="eval/final-v2",  # version token in name
            purpose="ARC scores for the baseline table across twenty three models",
            profile="eval-results/v1",
            s3=s3,
            created_at=CREATED,
            group_meta=_eval_meta(),
            env=ENV,
        )
    assert not s3.dump("edullm-landing")  # nothing written


def test_bad_purpose_rejected():
    s3 = FakeS3()
    with pytest.raises(P.PublishError):
        P.publish(
            _eval_dir(),
            dataset_id="eval/mcq-arc",
            purpose="training data",  # blocklisted
            profile="eval-results/v1",
            s3=s3,
            created_at=CREATED,
            group_meta=_eval_meta(),
            env=ENV,
        )


def test_unknown_family_rejected():
    s3 = FakeS3()
    # validate_dataset_id enforces the family enum, so an unknown family fails at naming
    with pytest.raises(P.PublishError):
        P.publish(
            _eval_dir(),
            dataset_id="bogus/mcq-arc",
            purpose="ARC scores for the baseline table across twenty three models",
            profile="eval-results/v1",
            s3=s3,
            created_at=CREATED,
            group_meta=_eval_meta(),
            env=ENV,
        )


def test_build_executor_aws_batch_captured():
    # build_plan hashes objects from S3 by streaming — seed the staged object, pass (path, size)
    s3 = FakeS3()
    body = np.arange(1, 1001, dtype=np.uint32).tobytes()
    s3.seed("edullm-landing", "_staging/pretrain/dolma2-150b/tokens/train-00000.u32le.bin", body)
    plan = P.build_plan(
        [("tokens/train-00000.u32le.bin", len(body))],
        dataset_id="pretrain/dolma2-150b",
        version="v1",
        purpose="150B-token Dolma2 mix for 370M ladder pretraining at 1.25xC",
        profile="pretrain-tokens/v1",
        family=P._load_family("pretrain"),
        created_at=CREATED,
        build_executor=P._build_executor_from_env({"AWS_BATCH_JOB_ID": "job-123", "AWS_REGION": "us-east-1"}),
        source_kind="local",
        s3=s3,
        source_bucket="edullm-landing",
        source_prefix="_staging/pretrain/dolma2-150b",
        group_meta=_tokens_meta(),
    )
    ex = plan.dataset_json["build"]["executor"]
    assert ex["kind"] == "aws-batch" and ex["job_id"] == "job-123"


def test_build_executor_external_from_env():
    ex = P._build_executor_from_env({"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64})
    assert ex["kind"] == "external" and ex["code_sha256"] == "a" * 64
