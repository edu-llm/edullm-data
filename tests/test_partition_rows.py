"""A caller-supplied partition must get its ``rows`` filled, or Gate A rejects the publish.

The pretrain family defaults to a ``train`` partition only, so declaring a ``val`` split means
supplying your own ``partitions`` list in ``group_meta``. That used to bypass row-filling
entirely — ``publish.py`` only resolved rows when the caller had supplied *no* partitions, and
then ``gm.update(group_meta[g])`` copied the caller's verbatim.

Result: partitions with no ``rows``, which Gate A rejects (``partition-no-rows``). And that
rejection lands at ``promote()`` — after the copy and the entire publish run. For a 630 GB
corpus that is hours of work thrown away over a number the publisher could have computed.

This is the shape ``docs/olmo-150b-publish-spec.json`` declares.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

from edullm_data import publish as P
from edullm_data import read as R
from edullm_data import validate as V
from edullm_data.s3 import FakeS3

CREATED = "2026-07-29T12:00:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}
DSID = "pretrain/rows-fixture"

TOKENIZER = {
    "repo_id": "allenai/dolma2-tokenizer",
    "revision": "abc123",
    "fingerprint_sha256": "c" * 64,
    "vocab_size": 100278,
    "eos_token_id": 100257,
}

# Exactly the partition block the 150B spec declares: train + val, by path, NO rows.
TRAIN_VAL_PARTITIONS = [
    {"name": "train", "by": "path", "glob": "train-*.u32le.bin"},
    {"name": "val", "by": "path", "glob": "val-*.u32le.bin"},
]


def _shard(n: int) -> bytes:
    return ((np.arange(1, n + 1) % 40000).astype(np.uint32) + 1).tobytes()


def _publish(s3: FakeS3, *, partitions, with_val: bool = True):
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    (d / "tokens" / "train-00000.u32le.bin").write_bytes(_shard(60000))
    (d / "tokens" / "train-00001.u32le.bin").write_bytes(_shard(40000))
    if with_val:
        (d / "tokens" / "val-00000.u32le.bin").write_bytes(_shard(20000))
    gm = {"tokens": {"tokenizer": TOKENIZER}}
    if partitions is not None:
        gm["tokens"]["partitions"] = partitions
        gm["tokens"]["coverage"] = "partition"
    plan = P.publish(
        d,
        dataset_id=DSID,
        purpose="fixture corpus for caller-supplied partition row filling at publish time",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at=CREATED,
        group_meta=gm,
        env=ENV,
    )
    res = V.validate_dataset(
        "edullm-landing", f"{DSID}/{plan.version}", s3, data_bucket="edullm-data"
    )
    return plan, res


def test_caller_supplied_partitions_get_rows_and_pass_gate_a():
    """The regression this fixes: the 150B spec shape now validates."""
    s3 = FakeS3()
    _, res = _publish(s3, partitions=TRAIN_VAL_PARTITIONS)
    no_rows = [v for v in res.violations if v.code == "partition-no-rows"]
    assert no_rows == [], [str(v) for v in no_rows]
    assert res.ok, [str(v) for v in res.violations]


def test_rows_are_counted_per_split_not_shared():
    s3 = FakeS3()
    plan, _ = _publish(s3, partitions=TRAIN_VAL_PARTITIONS)
    ds = R._load_json(s3, "edullm-landing", f"{DSID}/{plan.version}/dataset.json")
    parts = {p["name"]: p["rows"] for p in ds["groups"][0]["partitions"]}
    assert parts == {"train": 100000, "val": 20000}  # 60000+40000 train, 20000 val


def test_an_explicit_rows_is_not_overwritten():
    """Filling is for MISSING rows only — a caller who states a count keeps it.

    Stating a wrong one is a separate problem, and the right place to catch it is a Gate A
    recompute, not silently correcting the input here.
    """
    s3 = FakeS3()
    pinned = [
        {"name": "train", "by": "path", "glob": "train-*.u32le.bin", "rows": 12345},
        {"name": "val", "by": "path", "glob": "val-*.u32le.bin"},
    ]
    plan, _ = _publish(s3, partitions=pinned)
    ds = R._load_json(s3, "edullm-landing", f"{DSID}/{plan.version}/dataset.json")
    parts = {p["name"]: p["rows"] for p in ds["groups"][0]["partitions"]}
    assert parts["train"] == 12345  # untouched
    assert parts["val"] == 20000  # filled


def test_a_declared_split_with_no_shards_is_kept_not_silently_dropped():
    """Unlike the family-default path, an explicit claim is preserved so Gate A can object.

    Deleting it would hide the caller's mistake; ``partition-glob-empty`` is where it belongs.
    """
    s3 = FakeS3()
    _, res = _publish(s3, partitions=TRAIN_VAL_PARTITIONS, with_val=False)
    codes = [v.code for v in res.violations]
    assert "partition-glob-empty" in codes, codes


def test_family_default_path_still_works_and_still_drops_absent_splits():
    """No regression: with no caller partitions, the family default resolves as before."""
    s3 = FakeS3()
    plan, res = _publish(s3, partitions=None, with_val=False)
    assert res.ok, [str(v) for v in res.violations]
    ds = R._load_json(s3, "edullm-landing", f"{DSID}/{plan.version}/dataset.json")
    names = [p["name"] for p in ds["groups"][0]["partitions"]]
    assert names == ["train"]  # family declares train only; nothing empty shipped


def test_val_split_is_readable_end_to_end():
    """Rows existing is not the point — the split has to actually resolve for a trainer."""
    s3 = FakeS3()
    plan, res = _publish(s3, partitions=TRAIN_VAL_PARTITIONS)
    assert res.ok, [str(v) for v in res.violations]
    V.promote(res, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")

    train = R.dataset_paths(DSID, plan.version, split="train", s3=s3)
    val = R.dataset_paths(DSID, plan.version, split="val", s3=s3)
    assert len(train.paths) == 2 and train.rows == 100000
    assert len(val.paths) == 1 and val.rows == 20000
    assert val.dtype == "uint32"
    assert set(train.paths).isdisjoint(val.paths)
