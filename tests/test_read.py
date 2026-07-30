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
    # A val shard: the pretrain family now requires held-out data
    # (families/pretrain.json validation_required=true), so a train-only
    # corpus is a missing-required-split violation.
    (d / "tokens" / "val-00000.u32le.bin").write_bytes((np.arange(1, 20001, dtype=np.uint32) % 90000).tobytes())
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
    # promote() now writes _VALIDATED.json into edullm-data itself (the marker the reader
    # requires) — no manual seeding. This is the real end-to-end path: a promoted dataset is
    # readable with no extra step. Regression for the olmo migration, where the marker had
    # only ever been written to landing and dataset_paths() would refuse a promoted dataset.
    return plan.version


def test_promote_writes_validated_marker_into_data_bucket():
    """promote() must seal the promoted prefix with _VALIDATED.json IN edullm-data, because
    that is exactly where dataset_paths() looks. Without it, a correctly-promoted dataset is
    unreadable through the standard API (the bug the first real migration surfaced)."""
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    # the marker exists in the DATA bucket (not just landing), so the default reader works
    assert s3._store.get(("edullm-data", f"pretrain/dolma2-150b/{ver}/_VALIDATED.json")) is not None
    res = R.dataset_paths("pretrain/dolma2-150b", ver, s3=s3)  # require_validated=True default
    assert len(res.paths) == 2


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


def test_a_declared_vocabulary_split_this_dataset_lacks_returns_empty_not_raises():
    """Asking "does this have a test split?" must be a question, not an exception.

    Previously any undeclared split raised, so a caller could not probe optimistically — it had
    to catch ReadError to find out, which is control flow by exception for an ordinary fact.
    """
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    res = R.dataset_paths("pretrain/dolma2-150b", ver, split="test", s3=s3)
    assert res.paths == []
    assert res.rows is None
    assert not res.has_split("test")
    assert res.has_split("train") and res.has_split("val")  # the ones it does have


def test_a_split_outside_the_vocabulary_still_raises():
    """An undeclared-but-valid split is a fact; a nonexistent word is a mistake."""
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    with pytest.raises(R.ReadError, match="vocabulary"):
        R.dataset_paths("pretrain/dolma2-150b", ver, split="heldout", s3=s3)


def test_resolve_latest():
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    assert R.resolve_latest("pretrain/dolma2-150b", s3=s3) == ver
    assert R.resolve_latest("pretrain/does-not-exist", s3=s3) is None


# --------------------------------------------------------------------------------------
# format detail: byte_order + header_bytes must SURVIVE the read, and ambiguity must be loud
# --------------------------------------------------------------------------------------


def test_byte_order_and_header_bytes_reach_the_caller():
    """The manifest declares all three format facts; the reader used to hand back only dtype.

    dtype alone does not let a caller read the bytes. np.memmap(dtype="uint32") uses the HOST's
    byte order, so a big-endian shard on a little-endian host decodes every token to a
    different, plausible-looking id with nothing downstream to notice.
    """
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    res = R.dataset_paths("pretrain/dolma2-150b", ver, s3=s3)
    assert res.dtype == "uint32"
    assert res.byte_order == "little"   # from .u32le.bin, declared not inferred
    assert res.header_bytes == 0        # headerless — NOT the .npy lie


def test_numpy_dtype_is_order_qualified_and_numpy_accepts_it():
    """`numpy_dtype` must be a string numpy actually takes, and must carry the order.

    numpy accepts "<u4" but REJECTS "<uint32", so the property maps the long name to the type
    character rather than just prefixing it.
    """
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    res = R.dataset_paths("pretrain/dolma2-150b", ver, s3=s3)
    assert res.numpy_dtype == "<u4"
    d = np.dtype(res.numpy_dtype)  # would raise if we built a bogus string
    assert d.itemsize == 4 and d.kind == "u"
    # and it is explicitly little-endian, not "whatever this host is"
    assert np.dtype(res.numpy_dtype).str in ("<u4", "|u4", "=u4")


def test_untyped_container_group_is_not_mixed():
    """A group whose entries carry no dtype (parquet/jsonl/tokenizer files) types itself.

    dtype=None there is the legitimate answer and must NOT be confused with ambiguity.
    """
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    prefix = f"pretrain/dolma2-150b/{ver}"
    import json as _json

    key = ("edullm-data", f"{prefix}/tokens/manifest.json")
    man = _json.loads(s3._store[key])
    for e in man["entries"]:
        e["format"] = {"container": "parquet", "dtype": None, "byte_order": None,
                       "header_bytes": 0, "codec": "none"}
    s3._store[key] = _json.dumps(man).encode()

    res = R.dataset_paths("pretrain/dolma2-150b", ver, s3=s3, require_validated=False)
    assert res.dtype is None and res.byte_order is None
    assert res.numpy_dtype is None  # nothing to hand a loader, and it says so


def test_disagreeing_shard_dtypes_raise_instead_of_returning_none():
    """A group whose shards disagree has no single correct dtype, so it must RAISE.

    dtype=None on ambiguity was a silent failure: it is indistinguishable from the legitimate
    self-typing-container answer, so it flows into a loader which then defaults to uint16 and
    halves the token count without a word.
    """
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    prefix = f"pretrain/dolma2-150b/{ver}"
    import json as _json

    key = ("edullm-data", f"{prefix}/tokens/manifest.json")
    man = _json.loads(s3._store[key])
    man["entries"][0]["format"]["dtype"] = "uint16"  # one shard disagrees with the rest
    s3._store[key] = _json.dumps(man).encode()

    with pytest.raises(R.MixedFormat, match="different fixed-width formats"):
        R.dataset_paths("pretrain/dolma2-150b", ver, s3=s3, require_validated=False)


def test_disagreeing_header_bytes_also_raise():
    """Same dtype, different header sizes: one memmap stride cannot read both, and the shape of
    the disagreement is the .npy lie (some shards headerless, some not)."""
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    prefix = f"pretrain/dolma2-150b/{ver}"
    import json as _json

    key = ("edullm-data", f"{prefix}/tokens/manifest.json")
    man = _json.loads(s3._store[key])
    man["entries"][0]["format"]["header_bytes"] = 128
    s3._store[key] = _json.dumps(man).encode()

    with pytest.raises(R.MixedFormat):
        R.dataset_paths("pretrain/dolma2-150b", ver, s3=s3, require_validated=False)


def test_missing_byte_order_falls_back_to_bare_dtype_name():
    """A dataset that declares no byte_order genuinely does not say, so native is the only
    available reading — and numpy_dtype must not fabricate an order prefix."""
    s3 = FakeS3()
    ver = _publish_and_promote(s3)
    prefix = f"pretrain/dolma2-150b/{ver}"
    import json as _json

    key = ("edullm-data", f"{prefix}/tokens/manifest.json")
    man = _json.loads(s3._store[key])
    for e in man["entries"]:
        e["format"]["byte_order"] = None
    s3._store[key] = _json.dumps(man).encode()

    res = R.dataset_paths("pretrain/dolma2-150b", ver, s3=s3, require_validated=False)
    assert res.byte_order is None
    assert res.numpy_dtype == "uint32"  # honest: no order claimed, none invented
    assert np.dtype(res.numpy_dtype).itemsize == 4
