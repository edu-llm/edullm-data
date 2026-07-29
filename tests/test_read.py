"""dataset_paths() tests. The point of the reader is: right dtype, refuses unvalidated,
resolves splits. Tests build a published+promoted dataset the way the real flow would."""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from edullm_data import publish as P
from edullm_data import read as R
from edullm_data import validate as V
from edullm_data.s3 import FakeS3

CREATED = "2026-07-28T23:10:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}


def _publish_and_promote(s3: FakeS3) -> str:
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    (d / "tokens" / "train-00000.u32le.bin").write_bytes((np.arange(1, 60001, dtype=np.uint32) % 90000).tobytes())
    (d / "tokens" / "train-00001.u32le.bin").write_bytes((np.arange(1, 40001, dtype=np.uint32) % 90000).tobytes())
    plan = P.publish(
        d,
        dataset_id="pretrain/dolma2-150b",
        purpose="150B-token Dolma2 mix for 370M ladder pretraining at 1.25xC",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at=CREATED,
        group_meta={
            "tokens": {
                "tokenizer": {
                    "repo_id": "allenai/dolma2-tokenizer",
                    "revision": "abc123",
                    "fingerprint_sha256": "c" * 64,
                    "vocab_size": 100278,
                    "eos_token_id": 100257,
                }
            }
        },
        env=ENV,
    )
    r = V.validate_dataset("edullm-landing", f"pretrain/dolma2-150b/{plan.version}", s3, data_bucket="edullm-data")
    assert r.ok, [str(v) for v in r.violations]
    V.promote(r, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")
    # promotion copies dataset.json + manifests + payload, but not a _VALIDATED marker;
    # main() writes that. Simulate it so the reader's require_validated has something.
    s3.seed("edullm-data", f"pretrain/dolma2-150b/{plan.version}/_VALIDATED.json", b"{}")
    return plan.version


def test_returns_correct_dtype():
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    res = R.dataset_paths("pretrain/dolma2-150b", ver, s3=s3)
    assert res.dtype == "uint32"  # the uint16/uint32 trap, closed
    assert len(res.paths) == 2
    assert all(p.startswith("s3://edullm-data/pretrain/dolma2-150b/") for p in res.paths)


def test_split_selection_by_path():
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    res = R.dataset_paths("pretrain/dolma2-150b", ver, split="train", s3=s3)
    assert len(res.paths) == 2  # both train-* shards
    assert res.rows == 100000  # 60000 + 40000, from the manifest


def test_refuses_unvalidated():
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    # remove the validation marker
    del s3._store[("edullm-data", f"pretrain/dolma2-150b/{ver}/_VALIDATED.json")]
    with pytest.raises(R.NotValidated):
        R.dataset_paths("pretrain/dolma2-150b", ver, s3=s3)


def test_can_read_unvalidated_when_explicitly_allowed():
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    del s3._store[("edullm-data", f"pretrain/dolma2-150b/{ver}/_VALIDATED.json")]
    res = R.dataset_paths("pretrain/dolma2-150b", ver, s3=s3, require_validated=False)
    assert res.dtype == "uint32"


def test_unknown_split_raises():
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    with pytest.raises(R.ReadError):
        R.dataset_paths("pretrain/dolma2-150b", ver, split="test", s3=s3)


def test_resolve_latest():
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    assert R.resolve_latest("pretrain/dolma2-150b", s3=s3) == ver
    assert R.resolve_latest("pretrain/does-not-exist", s3=s3) is None
