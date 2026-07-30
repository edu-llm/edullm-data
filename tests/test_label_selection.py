"""``dataset_paths(labels=…)`` — the read side of ``entry.labels``.

Labels were populated and Gate A recomputes them from each object's key, but nothing on the
read path consumed them: ``ResolvedSplit`` had no labels field and ``dataset_paths`` had no
label parameter. Selecting a subset therefore meant fetching ``tokens/manifest.json`` yourself
and rebuilding URIs from ``entry.path`` — about thirty lines that every consumer would write
slightly differently. A field that is written, verified, and never read is the decoration
``CONTRIBUTING.md`` warns about.

Two things here are easy to get wrong and are what most of these tests defend:

* **``rows`` must be RECOMPUTED under a filter.** The partition's declared ``rows`` describes the
  whole partition, so inheriting it hands a trainer a count for data it did not select. That is
  the same failure ``validate``'s ``partition-rows-mismatch`` exists to catch on the write side,
  where the comment reads "read.dataset_paths hands that number straight to a trainer".
* **The filter must reach ``splits`` as well as ``paths``.** They are built by separate code
  paths, and ``docs/CONSUMER-CONTRACT.md`` tells adapter authors that ``.train`` disagreeing with
  ``.paths`` is "a bug worth failing on".
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from edullm_data import publish as P
from edullm_data import read as R
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

#: (source, domain, split, ordinal, tokens). Two sources, two domains inside one of them, and a
#: one-level source with no domain — the three shapes a real corpus mixes.
LAYOUT = [
    ("stack-edu", "Python", "train", 0, 30000),
    ("stack-edu", "Python", "train", 1, 20000),
    ("stack-edu", "SQL", "train", 2, 10000),
    ("stack-edu", "Python", "val", 3, 5000),
    ("arxiv", None, "train", 4, 40000),
    ("arxiv", None, "val", 5, 4000),
]


def _publish_promote(s3: FakeS3, dsid: str = "pretrain/labelsel-fixture") -> str:
    d = Path(tempfile.mkdtemp())
    rng = np.random.default_rng(11)
    for src, dom, split, ordinal, n in LAYOUT:
        sub = d / "tokens" / src / dom if dom else d / "tokens" / src
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"{split}-{ordinal:05d}.u32le.bin").write_bytes(
            rng.integers(1, 100278, size=n, dtype=np.uint32).tobytes()
        )
    plan = P.publish(
        d,
        dataset_id=dsid,
        purpose="fixture corpus for read-side label selection across sources and domains",
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
    V.promote(res, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")
    return plan.version


# ---- selecting ----

def test_no_labels_returns_everything_trainable():
    """The baseline the filter narrows from."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    r = R.dataset_paths("pretrain/labelsel-fixture", ver, s3=s3)
    assert len(r.paths) == 4  # four train shards
    assert not any("/val-" in p for p in r.paths)


def test_filter_by_one_key():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    r = R.dataset_paths("pretrain/labelsel-fixture", ver, labels={"source": "stack-edu"}, s3=s3)
    assert len(r.paths) == 3
    assert all("/stack-edu/" in p for p in r.paths)


def test_filter_by_two_keys_is_an_AND():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    r = R.dataset_paths(
        "pretrain/labelsel-fixture", ver,
        labels={"source": "stack-edu", "domain": "Python"}, s3=s3,
    )
    assert len(r.paths) == 2
    assert all("/stack-edu/Python/" in p for p in r.paths)


def test_a_one_level_entry_has_no_domain_key_and_is_not_matched_by_one():
    """``arxiv`` shards carry only ``{"source": …}``. Asking for a domain must exclude them,
    not match them loosely."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    assert len(R.dataset_paths(
        "pretrain/labelsel-fixture", ver, labels={"source": "arxiv"}, s3=s3).paths) == 1
    assert R.dataset_paths(
        "pretrain/labelsel-fixture", ver,
        labels={"source": "arxiv", "domain": "Python"}, s3=s3).paths == []


def test_a_predicate_matching_nothing_returns_empty_not_an_error():
    """On a LABELLED dataset an empty result is a real answer to a real question."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    r = R.dataset_paths("pretrain/labelsel-fixture", ver, labels={"source": "nope"}, s3=s3)
    assert r.paths == []


def test_labels_on_an_unlabelled_dataset_raises():
    """"This dataset has no labels" and "nothing matched" are different facts.

    A flat corpus (or any v1 manifest) carries no labels at all. Returning [] there looks
    identical to a predicate that simply missed, and the caller trains on nothing.
    """
    s3 = FakeS3()
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir(parents=True)
    rng = np.random.default_rng(3)
    for name, n in (("train-00000.u32le.bin", 30000), ("val-00000.u32le.bin", 20000)):
        (d / "tokens" / name).write_bytes(
            rng.integers(1, 100278, size=n, dtype=np.uint32).tobytes()
        )
    plan = P.publish(
        d, dataset_id="pretrain/flatsel-fixture",
        purpose="flat corpus proving a label filter on an unlabelled dataset fails loudly",
        profile="pretrain-tokens/v1", s3=s3, created_at=CREATED,
        group_meta={"tokens": {"tokenizer": TOKENIZER}}, env=ENV,
    )
    res = V.validate_dataset(
        "edullm-landing", f"pretrain/flatsel-fixture/{plan.version}", s3, data_bucket="edullm-data")
    V.promote(res, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")

    with pytest.raises(R.ReadError) as e:
        R.dataset_paths("pretrain/flatsel-fixture", plan.version, labels={"source": "x"}, s3=s3)
    assert "unlabelled" in str(e.value)


# ---- the counts, which are the part that silently lies ----

def test_rows_is_recomputed_from_the_selected_entries():
    """THE check. Inheriting the partition's declared total overstates a filtered read."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    unfiltered = R.dataset_paths("pretrain/labelsel-fixture", ver, split="train", s3=s3)
    assert unfiltered.rows == 100000  # 30000 + 20000 + 10000 + 40000

    r = R.dataset_paths(
        "pretrain/labelsel-fixture", ver, split="train",
        labels={"source": "stack-edu", "domain": "Python"}, s3=s3,
    )
    assert r.rows == 50000, "rows must count only what was selected"
    assert r.rows != unfiltered.rows


def test_split_rows_is_recomputed_too():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    r = R.dataset_paths("pretrain/labelsel-fixture", ver, labels={"source": "arxiv"}, s3=s3)
    assert r.split_rows == {"train": 40000, "val": 4000}


def test_an_unsplit_filtered_read_reports_rows():
    """Unfiltered this is None (no partition to inherit from); filtered we can do better."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    assert R.dataset_paths("pretrain/labelsel-fixture", ver, s3=s3).rows is None
    r = R.dataset_paths("pretrain/labelsel-fixture", ver, labels={"source": "stack-edu"}, s3=s3)
    assert r.rows == 60000  # the three stack-edu TRAIN shards


# ---- the two views must not disagree ----

def test_train_and_paths_agree_under_a_filter():
    """``CONSUMER-CONTRACT.md`` calls a disagreement here "a bug worth failing on"; ``splits``
    and ``paths`` are built by separate code, so the filter has to reach both."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    r = R.dataset_paths("pretrain/labelsel-fixture", ver, labels={"source": "stack-edu"}, s3=s3)
    assert sorted(r.train) == sorted(r.paths)


def test_the_filter_reaches_val_as_well_as_train():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    r = R.dataset_paths(
        "pretrain/labelsel-fixture", ver,
        labels={"source": "stack-edu", "domain": "Python"}, s3=s3,
    )
    assert r.val is not None and len(r.val) == 1
    assert "/stack-edu/Python/val-00003" in r.val[0]


def test_include_held_out_also_respects_the_filter():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    r = R.dataset_paths(
        "pretrain/labelsel-fixture", ver, labels={"source": "arxiv"},
        include_held_out=True, s3=s3,
    )
    assert len(r.paths) == 2  # the arxiv train AND val shard, nothing from stack-edu
    assert all("/arxiv/" in p for p in r.paths)


def test_an_explicit_split_plus_labels_composes():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    r = R.dataset_paths(
        "pretrain/labelsel-fixture", ver, split="val", labels={"source": "arxiv"}, s3=s3)
    assert len(r.paths) == 1 and "/arxiv/val-00005" in r.paths[0]
    assert r.rows == 4000


def test_the_format_triple_survives_a_filter():
    """dtype is the field a loader must not get wrong; filtering must not disturb it."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    r = R.dataset_paths("pretrain/labelsel-fixture", ver, labels={"source": "arxiv"}, s3=s3)
    assert (r.dtype, r.numpy_dtype, r.byte_order, r.header_bytes) == (
        "uint32", "<u4", "little", 0)
