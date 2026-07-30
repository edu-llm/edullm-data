"""wu-fsck tests. Each simulates a post-publish decay — the failures no publish gate can
catch — and asserts the sweep flags it. Builds a real published dataset via the full flow,
then breaks it after the fact."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np

from edullm_data import fsck as F
from edullm_data import publish as P
from edullm_data import validate as V
from edullm_data.s3 import FakeS3

CREATED = "2026-07-28T23:20:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}
DID = "pretrain/dolma2-150b"


def _publish_promote(s3: FakeS3, dataset_id: str = DID) -> str:
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    (d / "tokens" / "train-00000.u32le.bin").write_bytes((np.arange(1, 60001, dtype=np.uint32) % 90000).tobytes())
    # A val shard: the pretrain family now requires held-out data
    # (families/pretrain.json validation_required=true), so a train-only
    # corpus is a missing-required-split violation.
    (d / "tokens" / "val-00000.u32le.bin").write_bytes((np.arange(1, 20001, dtype=np.uint32) % 90000).tobytes())
    plan = P.publish(
        d,
        dataset_id=dataset_id,
        purpose="150B-token Dolma2 mix for 370M ladder pretraining at 1.25xC",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at=CREATED,
        group_meta={"tokens": {"tokenizer": {"repo_id": "r", "revision": "abc", "fingerprint_sha256": "c" * 64, "vocab_size": 100278, "eos_token_id": 100257}}},
        env=ENV,
    )
    r = V.validate_dataset("edullm-landing", f"{dataset_id}/{plan.version}", s3, data_bucket="edullm-data")
    assert r.ok, [str(v) for v in r.violations]
    V.promote(r, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")
    return plan.version


def _codes(report) -> set[str]:
    return {f.code for f in report.findings}


def test_clean_catalog_passes():
    s3 = FakeS3()
    _publish_promote(s3)
    report = F.fsck(s3, data_bucket="edullm-data")
    assert report.checked == 1
    assert report.ok, [str(f) for f in report.findings]


def test_object_deleted_after_publish():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    # simulate a post-publish deletion of a payload object
    del s3._store[("edullm-data", f"{DID}/{ver}/tokens/train-00000.u32le.bin")]
    report = F.fsck(s3, data_bucket="edullm-data")
    assert "object-gone" in _codes(report)
    assert not report.ok


def test_object_resized_after_publish():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    s3.override_head("edullm-data", f"{DID}/{ver}/tokens/train-00000.u32le.bin", size=5)
    report = F.fsck(s3, data_bucket="edullm-data")
    assert "object-resized" in _codes(report)


def test_catalog_drift():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    # mutate the published dataset.json inventory to disagree with reality
    key = ("edullm-data", f"{DID}/{ver}/dataset.json")
    ds = json.loads(s3._store[key])
    ds["inventory"]["objects"] = 999
    s3._store[key] = json.dumps(ds).encode()
    report = F.fsck(s3, data_bucket="edullm-data")
    assert "catalog-object-drift" in _codes(report)


def test_dangling_parent():
    s3 = FakeS3()
    # publish a parent, then a child depending on it, then delete the parent
    pver = _publish_promote(s3, dataset_id="pretrain/datamix-370m")
    # hand-write a child catalog entry + dataset.json referencing the parent
    child_prefix = "curriculum/flesch-linear-370m/v1"
    child_ds = {
        "schema_version": "edullm-dataset/v1",
        "dataset_id": "curriculum/flesch-linear-370m",
        "version": {"id": "v1", "relation": "supersedes", "of": None},
        "inventory": {"objects": 0, "bytes": 0},
        "groups": [{"name": "order", "manifest": "order/manifest.json",
                     "depends_on": [{"dataset_id": "pretrain/datamix-370m", "version": pver, "manifest_sha256": "d" * 64}]}],
    }
    s3.seed("edullm-data", f"{child_prefix}/dataset.json", json.dumps(child_ds).encode())
    s3.seed("edullm-data", f"{child_prefix}/order/manifest.json", json.dumps({"entries": []}).encode())
    s3.seed("edullm-data", f"_catalog/curriculum/flesch-linear-370m/v1.json", b"{}")
    # delete the parent dataset.json
    del s3._store[("edullm-data", f"pretrain/datamix-370m/{pver}/dataset.json")]
    report = F.fsck(s3, data_bucket="edullm-data")
    assert "dangling-parent" in _codes(report)


def test_report_json_shape():
    s3 = FakeS3()
    _publish_promote(s3)
    report = F.fsck(s3, data_bucket="edullm-data")
    d = report.to_dict()
    assert d["schema_version"] == "edullm-fsck/v1"
    assert d["owner"] == "eric.wu"
    assert "checked" in d and "findings" in d
