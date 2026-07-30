"""The seal must bind to CONTENT, so "frozen means frozen" is falsifiable.

Before this, ``_VALIDATED.json`` and the catalog entry carried only ``{dataset_id, version,
objects, bytes}``. A hash chain existed — each group declares ``manifest_sha256`` — but it had
no ROOT: nothing hashed ``dataset.json`` itself. So the seal asserted "someone ran the
validator" and nothing more, and a tampered ``dataset.json`` was indistinguishable from the
one that was actually sealed.

Rooting it costs two small GETs per group and no payload bytes.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from edullm_data import publish as P
from edullm_data import read as R
from edullm_data import validate as V
from edullm_data.contracts import canonical_json, sha256_bytes
from edullm_data.s3 import FakeS3

CREATED = "2026-07-29T12:00:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}
DSID = "pretrain/seal-fixture"


def _publish_and_promote(s3: FakeS3) -> str:
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    ids = (np.arange(1, 60001) % 40000).astype(np.uint32) + 1
    (d / "tokens" / "train-00000.u32le.bin").write_bytes(ids.tobytes())
    plan = P.publish(
        d,
        dataset_id=DSID,
        purpose="fixture corpus for verifying the rooted hash chain in the seal",
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
    r = V.validate_dataset("edullm-landing", f"{DSID}/{plan.version}", s3, data_bucket="edullm-data")
    assert r.ok, [str(v) for v in r.violations]
    V.promote(r, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")
    return plan.version


def test_seal_carries_the_root_and_every_group_manifest_hash():
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    seal = json.loads(s3.get("edullm-data", f"{DSID}/{ver}/_VALIDATED.json").decode())
    assert "dataset_sha256" in seal
    assert seal["manifest_sha256"].get("tokens")

    # the root must actually be the hash of the published dataset.json
    actual = sha256_bytes(s3.get("edullm-data", f"{DSID}/{ver}/dataset.json"))
    assert seal["dataset_sha256"] == actual


def test_catalog_entry_carries_the_root_too():
    """A reader that only ever sees the catalog still gets a content commitment."""
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    cat = json.loads(s3.get("edullm-data", f"_catalog/{DSID}/{ver}.json").decode())
    actual = sha256_bytes(s3.get("edullm-data", f"{DSID}/{ver}/dataset.json"))
    assert cat["dataset_sha256"] == actual


def test_verify_seal_passes_on_an_untouched_dataset():
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    assert R.verify_seal(DSID, ver, s3=s3, data_bucket="edullm-data") == []


def test_verify_seal_catches_a_tampered_dataset_json():
    """The failure the root exists to make detectable."""
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    ds = json.loads(s3.get("edullm-data", f"{DSID}/{ver}/dataset.json").decode())
    ds["purpose"] = "silently repurposed after sealing, which used to be undetectable"
    s3.put("edullm-data", f"{DSID}/{ver}/dataset.json", canonical_json(ds))

    problems = R.verify_seal(DSID, ver, s3=s3, data_bucket="edullm-data")
    assert problems, "a rewritten dataset.json must not verify"
    assert any("dataset.json" in p and "NOT the one published" in p for p in problems), problems


def test_verify_seal_catches_a_tampered_manifest():
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    man = json.loads(s3.get("edullm-data", f"{DSID}/{ver}/tokens/manifest.json").decode())
    man["entries"][0]["count"]["value"] = 999_999_999  # claim far more tokens than the bytes hold
    s3.put("edullm-data", f"{DSID}/{ver}/tokens/manifest.json", canonical_json(man))

    problems = R.verify_seal(DSID, ver, s3=s3, data_bucket="edullm-data")
    assert problems, "a rewritten manifest must not verify"
    assert any("manifest" in p for p in problems), problems


def test_verify_seal_reports_an_unrooted_legacy_seal_rather_than_passing():
    """A seal predating the root is UNVERIFIABLE, which is not the same as verified.

    The live datasets were sealed before this change, so this is the state they are in until
    they are re-promoted. Silence would misrepresent them as checked.
    """
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    seal = json.loads(s3.get("edullm-data", f"{DSID}/{ver}/_VALIDATED.json").decode())
    del seal["dataset_sha256"]
    s3.put("edullm-data", f"{DSID}/{ver}/_VALIDATED.json", canonical_json(seal))

    problems = R.verify_seal(DSID, ver, s3=s3, data_bucket="edullm-data")
    assert any("no dataset_sha256" in p for p in problems), problems


def test_verify_seal_raises_when_there_is_no_seal_at_all():
    s3 = FakeS3()
    with pytest.raises(R.NotValidated):
        R.verify_seal(DSID, "v1", s3=s3, data_bucket="edullm-data")
