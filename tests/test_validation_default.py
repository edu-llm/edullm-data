"""Validation is required by default, and an unsplit read never hands back held-out data.

Two changes, one intent: you should not be able to train on your own validation set by
accident, and you should not be able to ship a corpus you cannot measure held-out loss on
without saying so on purpose.

**Publishing (opt-OUT).** ``families/<family>.json`` carries ``validation_required``, defaulting
to true. Under opt-IN, a corpus with no val split is indistinguishable from one where nobody
thought about it — and the second is a mistake you discover weeks later from a suspiciously good
eval. Under opt-out, "this family has nothing to hold out" is a claim stated in a file with a
reason attached, which a reviewer can disagree with. Four families legitimately opt out:
``eval`` and ``probe`` are held out in their entirety, ``tokenizer`` is a model artifact with no
rows, ``vendor`` preserves upstream bytes verbatim.

**Reading (V9).** ``dataset_paths(...)`` with no ``split=`` used to return EVERY entry, so a
caller who did not ask for a split got the val shards mixed in with no way to tell which was
which. Now it returns trainable data only, plus every declared split separately in ``.splits`` /
``.train`` / ``.val``. Silence means the safe subset.
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
from edullm_data.s3 import FakeS3
from edullm_data.validate import FAMILIES_DIR, _family_defaults_for

CREATED = "2026-07-29T12:00:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}
DSID = "pretrain/heldout-fixture-10b"
TOKENIZER = {
    "repo_id": "allenai/dolma2-tokenizer",
    "revision": "abc123",
    "fingerprint_sha256": "c" * 64,
    "vocab_size": 100278,
    "eos_token_id": 100257,
}

REQUIRED = ("pretrain", "curriculum", "sft")
OPTED_OUT = ("eval", "probe", "tokenizer", "vendor")


# ---- the family declarations ----

@pytest.mark.parametrize("family", REQUIRED)
def test_corpus_families_require_validation(family):
    assert _family_defaults_for(f"{family}/x-10b")["validation_required"] is True


@pytest.mark.parametrize("family", OPTED_OUT)
def test_opting_out_is_explicit_and_carries_a_reason(family):
    """An opt-out must be a stated decision, not an omission."""
    defaults = _family_defaults_for(f"{family}/x-10b")
    assert defaults["validation_required"] is False
    reason = defaults.get("validation_not_required_because", "")
    assert len(reason) > 40, f"{family} opts out without explaining why: {reason!r}"


def test_every_family_states_a_position():
    """No family may be silent — that is the whole point of opt-out over opt-in."""
    for path in sorted(FAMILIES_DIR.glob("*.json")):
        defaults = _family_defaults_for(f"{path.stem}/x-10b")
        assert "validation_required" in defaults, f"{path.stem} states no position"


def test_family_split_names_are_all_in_the_closed_vocabulary():
    """sft used to declare 'heldout', which the vocabulary no longer contains."""
    from edullm_data.contracts import SPLITS

    for path in sorted(FAMILIES_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        for part in (raw.get("defaults", {}).get("partitions") or []):
            assert part["name"] in SPLITS, f"{path.stem} declares split {part['name']!r}"


# ---- Gate A enforcement ----

def _publish(s3: FakeS3, *, with_val: bool):
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    ids = (np.arange(1, 60001) % 40000).astype(np.uint32) + 1
    (d / "tokens" / "train-00000.u32le.bin").write_bytes(ids.tobytes())
    (d / "tokens" / "train-00001.u32le.bin").write_bytes(ids[:40000].tobytes())
    if with_val:
        (d / "tokens" / "val-00000.u32le.bin").write_bytes(ids[:20000].tobytes())
    plan = P.publish(
        d,
        dataset_id=DSID,
        purpose="fixture corpus for validation-required-by-default and the unsplit-read rule",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at=CREATED,
        group_meta={"tokens": {"tokenizer": TOKENIZER}},
        env=ENV,
    )
    res = V.validate_dataset("edullm-landing", f"{DSID}/{plan.version}", s3, data_bucket="edullm-data")
    return plan.version, res


def test_a_pretrain_corpus_with_no_heldout_split_is_rejected():
    _, res = _publish(FakeS3(), with_val=False)
    assert "missing-required-split" in {v.code for v in res.violations}
    assert not res.ok


def test_the_violation_says_how_to_fix_it_and_names_the_known_exception():
    _, res = _publish(FakeS3(), with_val=False)
    msg = next(str(v) for v in res.violations if v.code == "missing-required-split")
    assert "val" in msg
    assert "validation_required=false" in msg  # the opt-out escape hatch
    # the live corpus is expected to fail this; the message must say so rather than look like a bug
    assert "olmo-mix-1124-31b" in msg and "EXPECTED" in msg


def test_a_corpus_with_a_val_split_passes():
    _, res = _publish(FakeS3(), with_val=True)
    assert res.ok, [str(v) for v in res.violations]


def test_families_that_opted_out_are_not_asked_for_a_split():
    """A tokenizer dataset has no rows to split and must validate clean."""
    s3 = FakeS3()
    d = Path(tempfile.mkdtemp())
    (d / "files").mkdir()
    doc = {
        "model": {"type": "BPE", "vocab": {str(i): i for i in range(100256)}},
        "added_tokens": [{"id": 100257, "content": "<|endoftext|>", "special": True}],
    }
    (d / "files" / "tokenizer.json").write_bytes(json.dumps(doc).encode())
    plan = P.publish(
        d,
        dataset_id="tokenizer/dolma2-bpe",
        purpose="Published Dolma2 tokenizer so corpora own the tokenizer they were produced with",
        profile="tokenizer/v1",
        s3=s3,
        created_at=CREATED,
        env=ENV,
    )
    res = V.validate_dataset(
        "edullm-landing", f"tokenizer/dolma2-bpe/{plan.version}", s3, data_bucket="edullm-data"
    )
    assert "missing-required-split" not in {v.code for v in res.violations}
    assert res.ok, [str(v) for v in res.violations]


# ---- V9: the read path ----

def _promoted(s3: FakeS3) -> str:
    ver, res = _publish(s3, with_val=True)
    assert res.ok, [str(v) for v in res.violations]
    V.promote(res, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")
    return ver


def test_an_unsplit_read_returns_trainable_data_only():
    """V9. This is the bug: a trainer asked for "the data" and got its own val shards."""
    s3 = FakeS3()
    ver = _promoted(s3)
    res = R.dataset_paths(DSID, ver, s3=s3)
    assert len(res.paths) == 2
    assert not any("val-" in p for p in res.paths)


def test_both_splits_come_back_separately_keyed():
    """Your requirement — a dataset returns train AND val — without flattening them."""
    s3 = FakeS3()
    ver = _promoted(s3)
    res = R.dataset_paths(DSID, ver, s3=s3)
    assert sorted(res.splits) == ["train", "val"]
    assert len(res.train) == 2
    assert res.val is not None and len(res.val) == 1
    assert res.split_rows == {"train": 100000, "val": 20000}
    assert set(res.train).isdisjoint(res.val)


def test_val_is_none_not_empty_when_a_dataset_has_none():
    """"No validation data" and "an empty validation split" are different facts."""
    s3 = FakeS3()
    d = Path(tempfile.mkdtemp())
    (d / "files").mkdir()
    (d / "files" / "tokenizer.json").write_bytes(
        json.dumps({"model": {"type": "BPE", "vocab": {"a": 0}}, "added_tokens": []}).encode()
    )
    plan = P.publish(
        d,
        dataset_id="tokenizer/tiny-bpe",
        purpose="Minimal published tokenizer for asserting the reader reports absent validation data",
        profile="tokenizer/v1",
        s3=s3,
        created_at=CREATED,
        env=ENV,
    )
    res = V.validate_dataset(
        "edullm-landing", f"tokenizer/tiny-bpe/{plan.version}", s3, data_bucket="edullm-data"
    )
    V.promote(res, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")
    r = R.dataset_paths("tokenizer/tiny-bpe", plan.version, s3=s3)
    assert r.val is None
    assert r.paths, "a dataset with no trainable split must still return its payload"


def test_include_held_out_is_the_deliberate_escape_hatch():
    s3 = FakeS3()
    ver = _promoted(s3)
    res = R.dataset_paths(DSID, ver, s3=s3, include_held_out=True)
    assert len(res.paths) == 3
    assert any("val-" in p for p in res.paths)


def test_asking_for_val_explicitly_gets_only_val():
    s3 = FakeS3()
    ver = _promoted(s3)
    res = R.dataset_paths(DSID, ver, split="val", s3=s3)
    assert len(res.paths) == 1
    assert "val-00000" in res.paths[0]
    assert res.rows == 20000
