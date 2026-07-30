"""Schema v2: ``split`` + ``labels`` on manifest entries, and the closed split vocabulary.

Two properties carry this change, and both are load-bearing:

1. **A v1 manifest re-serializes byte-identically.** Both new fields are omitted when absent,
   so ``manifest_sha256`` does not move and an already-published dataset is not retroactively
   invalidated. Verified here against a v1-shaped manifest and, out of band, against the real
   218-entry live corpus (whose recomputed hash still matches the ``f05702fa…`` that
   ``dataset.json`` declares).
2. **``split`` cannot disagree with the bytes.** Gate A recomputes it from the object's own
   filename via ``parse_shard_name`` — a value the validator already computed and then threw
   away — so declaring ``split: "train"`` on ``val-00000.u32le.bin`` is a violation.

The vocabulary is closed (``{train, val, test}``) because the alternative is what shipped: four
families disagreeing (``train`` / ``heldout`` / ``test`` / ``val``) and a profile guessing which
side was held out by substring-matching the partition name. That guess misreads ``trainval`` as
held-out, and rejects ``dev`` outright. ``held_out`` is now DERIVED (``split != "train"``), so
there is no second name to get wrong.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from edullm_data import publish as P
from edullm_data import validate as V
from edullm_data.contracts import (
    READABLE_SCHEMA_VERSIONS,
    SCHEMA_VERSION,
    SPLITS,
    TRAINABLE_SPLITS,
    is_trainable,
)
from edullm_data.manifest import ManifestEntry, manifest_sha256
from edullm_data.s3 import FakeS3

CREATED = "2026-07-29T12:00:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}
DSID = "pretrain/splitcheck-fixture-10b"

V1_ENTRY = {
    "path": "tokens/train-00000.u32le.bin",
    "sha256": "a" * 64,
    "bytes": 240000,
    "count": {"unit": "tokens", "value": 60000},
    "format": {
        "container": "raw",
        "dtype": "uint32",
        "byte_order": "little",
        "header_bytes": 0,
        "codec": "none",
    },
}


# ---- compatibility: the whole reason this is safe ----

def test_a_v1_entry_round_trips_byte_identically():
    assert ManifestEntry.from_dict(V1_ENTRY).to_dict() == V1_ENTRY


def test_a_v1_entry_reads_as_split_unknown_not_split_train():
    """Unknown must not collapse to "trainable" — that is the failure being prevented."""
    e = ManifestEntry.from_dict(V1_ENTRY)
    assert e.split is None
    assert e.labels is None
    assert not is_trainable(e.split)


def test_manifest_hash_is_unchanged_for_a_v1_shaped_manifest():
    """If this moves, every published dataset's declared manifest_sha256 becomes wrong."""
    man = {
        "schema_version": "edullm-dataset/v1",
        "group": "tokens",
        "entries": [V1_ENTRY],
        "objects": 1,
        "bytes": 240000,
    }
    before = manifest_sha256(man)
    rebuilt = dict(man, entries=[ManifestEntry.from_dict(V1_ENTRY).to_dict()])
    assert manifest_sha256(rebuilt) == before


def test_both_schema_versions_stay_readable():
    assert SCHEMA_VERSION == "edullm-dataset/v2"
    assert "edullm-dataset/v1" in READABLE_SCHEMA_VERSIONS


# ---- the new fields ----

def test_a_v2_entry_round_trips_and_sorts_labels():
    e2 = dict(V1_ENTRY, split="train", labels={"source": "arxiv", "domain": "science"})
    out = ManifestEntry.from_dict(e2).to_dict()
    assert out == e2
    assert list(out["labels"]) == ["domain", "source"]  # sorted, so the hash is stable


@pytest.mark.parametrize("split", sorted(SPLITS))
def test_every_vocabulary_word_is_accepted(split):
    ManifestEntry.from_dict(dict(V1_ENTRY, split=split))


@pytest.mark.parametrize("bad", ["heldout", "held-out", "holdout", "dev", "eval", "TRAIN", ""])
def test_words_outside_the_vocabulary_are_rejected(bad):
    with pytest.raises(ValueError, match="entry.split"):
        ManifestEntry.from_dict(dict(V1_ENTRY, split=bad))


def test_the_word_validation_is_deliberately_not_in_the_vocabulary():
    """`val` is the chosen word. "validation" would work by substring luck in the SFT
    detector (`val` is a prefix of it), which is exactly the kind of accident the closed
    enum removes."""
    assert "validation" not in SPLITS
    with pytest.raises(ValueError):
        ManifestEntry.from_dict(dict(V1_ENTRY, split="validation"))


@pytest.mark.parametrize(
    "labels",
    [
        {"source": 123},  # non-string value
        {"source": None},
        {"source": {"nested": "x"}},
        {"": "x"},  # empty key
        {"split": "train"},  # reserved: two places to state one fact
    ],
)
def test_bad_labels_are_rejected(labels):
    with pytest.raises(ValueError, match="entry.labels"):
        ManifestEntry.from_dict(dict(V1_ENTRY, labels=labels))


def test_unknown_keys_are_still_rejected():
    """Widening the allow-list must not have opened it up generally."""
    with pytest.raises(ValueError, match="unknown key"):
        ManifestEntry.from_dict(dict(V1_ENTRY, splits="train"))  # note the typo


# ---- trainability is derived, never named ----

def test_only_train_is_trainable():
    assert TRAINABLE_SPLITS == {"train"}
    assert is_trainable("train")
    for held in SPLITS - TRAINABLE_SPLITS:
        assert not is_trainable(held)


def test_unknown_split_is_not_trainable():
    assert not is_trainable(None)
    assert not is_trainable("something-else")


# ---- Gate A recomputes split from the filename ----

def _publish_with_splits(s3: FakeS3, entry_splits: dict[str, str]):
    """Publish a corpus, then rewrite the manifest's declared splits before validating."""
    import json

    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    ids = (np.arange(1, 60001) % 40000).astype(np.uint32) + 1
    for name in entry_splits:
        (d / "tokens" / name).write_bytes(ids.tobytes())
    plan = P.publish(
        d,
        dataset_id=DSID,
        purpose="fixture corpus for the split-contradicts-filename recompute check",
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
                },
                "partitions": [
                    {"name": "train", "by": "path", "glob": "train-*.u32le.bin"},
                    {"name": "val", "by": "path", "glob": "val-*.u32le.bin"},
                ],
                "coverage": "partition",
            }
        },
        env=ENV,
    )
    prefix = f"{DSID}/{plan.version}"
    key = f"{prefix}/tokens/manifest.json"
    man = json.loads(s3.get("edullm-landing", key).decode())
    for e in man["entries"]:
        base = e["path"].rsplit("/", 1)[-1]
        if base in entry_splits:
            e["split"] = entry_splits[base]
    # rewrite the manifest AND dataset.json's hash of it, so only the split claim is under test
    from edullm_data.contracts import canonical_json

    body = canonical_json(man)
    s3.put("edullm-landing", key, body)
    ds = json.loads(s3.get("edullm-landing", f"{prefix}/dataset.json").decode())
    ds["groups"][0]["manifest_sha256"] = manifest_sha256(man)
    s3.put("edullm-landing", f"{prefix}/dataset.json", canonical_json(ds))
    return V.validate_dataset("edullm-landing", prefix, s3, data_bucket="edullm-data")


def test_a_truthful_split_declaration_passes():
    res = _publish_with_splits(
        FakeS3(), {"train-00000.u32le.bin": "train", "val-00000.u32le.bin": "val"}
    )
    contradictions = [v for v in res.violations if v.code == "split-contradicts-filename"]
    assert contradictions == [], [str(v) for v in contradictions]


def test_a_split_that_contradicts_the_filename_is_rejected():
    """Declaring the val shard as train — the failure that puts held-out data in training."""
    res = _publish_with_splits(
        FakeS3(), {"train-00000.u32le.bin": "train", "val-00000.u32le.bin": "train"}
    )
    codes = [v.code for v in res.violations]
    assert "split-contradicts-filename" in codes, codes
    assert not res.ok
    msg = next(str(v) for v in res.violations if v.code == "split-contradicts-filename")
    assert "val-00000" in msg and "'train'" in msg and "'val'" in msg
