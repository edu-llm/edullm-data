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
    # warn_partial_labels=False: this fixture MIXES depths, so a `domain` predicate legitimately
    # drops the flat arxiv shards and warns about it. The narrowing is the point of the test.
    r = R.dataset_paths(
        "pretrain/labelsel-fixture", ver,
        labels={"source": "stack-edu", "domain": "Python"}, warn_partial_labels=False, s3=s3,
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
        "pretrain/labelsel-fixture", ver, labels={"source": "arxiv", "domain": "Python"},
        warn_partial_labels=False, s3=s3).paths == []


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
        labels={"source": "stack-edu", "domain": "Python"}, warn_partial_labels=False, s3=s3,
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
        labels={"source": "stack-edu", "domain": "Python"}, warn_partial_labels=False, s3=s3,
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


# --------------------------------------------------------------------------------------
# the PARTIAL-coverage case: a requested key that only SOME entries carry
# --------------------------------------------------------------------------------------
#
# These live here rather than in ``test_read.py`` because the fixture above is already the exact
# shape the case needs — ``stack-edu`` nested under a ``domain``, ``arxiv`` flat — and because the
# load-bearing claim is that ``dataset_paths`` and ``build_mixture`` warn on the SAME condition,
# which is only demonstrable with both side by side.
#
# The gap: ``_matches_labels`` requires every requested key to be present AND equal, and Gate A
# imposes no depth uniformity, so a group legitimately holds both shapes. Ask it for
# ``labels={"domain": …}`` and the flat sources are dropped by the key's ABSENCE — not by its
# value. Verified before this shipped:
#
#   request labels={'domain': 'science'}
#     tokens/dclm/train-00000.u32le.bin                  skip   <-- flat, no domain key at all
#     tokens/essential-web/science/train-00001.u32le.bin  MATCH
#     tokens/stackv2-edu/Python/train-00002.u32le.bin     skip
#
# Non-empty result, consistent counts, no error. A teammate asks for "the science slice" and gets
# a corpus with no DCLM, FineMath or peS2o in it and nothing to tell them so.
#
# WARN, not raise. The all-flat case raises because the result would be empty and an empty result
# cannot be distinguished from "your filter matched nothing" — there is no honest value to hand
# back. Here the result is real and is exactly what was asked for; what is wrong is the caller's
# model of what they asked for. Raising would also break the legitimate "only the labelled
# sources, please" request, which is not expressible any other way.


def _warnings_from(fn):
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = fn()
    return result, [w for w in caught if w.category is R.PartialLabelCoverage]


def test_a_domain_filter_warns_that_it_dropped_the_flat_sources():
    """THE check. Without it this read is silent."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    r, warned = _warnings_from(
        lambda: R.dataset_paths("pretrain/labelsel-fixture", ver, labels={"domain": "Python"}, s3=s3)
    )
    assert len(warned) == 1
    msg = str(warned[0].message)
    assert "'domain'" in msg or "['domain']" in msg
    assert "source=arxiv" in msg, "it must name WHICH sources were excluded"
    assert r.paths, "the read still succeeds — this is a real result, not an error"


def test_the_warning_quantifies_the_loss_in_shards_and_tokens():
    """"Some sources were dropped" is not actionable; "40.4% of the group's tokens" is.

    Scoped to the WHOLE group here, because a ``dataset_paths`` filter narrows every declared
    split — ``paths``, ``splits``, ``train`` and ``val`` all — so both arxiv shards were dropped
    and both are the caller's loss. ``build_mixture`` reports against its split-filtered pool
    instead; see ``test_the_mixture_warning_counts_only_shards_the_mixture_could_have_drawn``.
    """
    s3 = FakeS3()
    ver = _publish_promote(s3)
    _, warned = _warnings_from(
        lambda: R.dataset_paths("pretrain/labelsel-fixture", ver, labels={"domain": "Python"}, s3=s3)
    )
    msg = str(warned[0].message)
    assert "2 shards" in msg
    assert "44,000 tokens" in msg  # the flat arxiv train AND val shards
    assert "40.4% of the group's tokens" in msg


def test_the_warning_names_the_way_out():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    _, warned = _warnings_from(
        lambda: R.dataset_paths("pretrain/labelsel-fixture", ver, labels={"domain": "Python"}, s3=s3)
    )
    msg = str(warned[0].message)
    assert "warn_partial_labels=False" in msg  # the narrowing was intended
    assert "'source'" in msg  # the key every entry carries
    assert "simplefilter('error', PartialLabelCoverage)" in msg  # make it fatal


def test_a_filter_on_a_key_every_entry_carries_does_NOT_warn():
    """The failing fixture for this check: `source=` is unaffected by mixed depth, which is the
    whole reason it stays the primary selector. A warning here would be noise on the happy path
    and would train everyone to ignore the one that matters."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    _, warned = _warnings_from(
        lambda: R.dataset_paths(
            "pretrain/labelsel-fixture", ver, labels={"source": "stack-edu"}, s3=s3
        )
    )
    assert warned == []


def test_a_value_mismatch_is_not_reported_as_a_coverage_gap():
    """``domain=Python`` correctly excludes the SQL shard — it answered the question and lost.
    Only the entries that could not be ASKED are the caller's blind spot."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    _, warned = _warnings_from(
        lambda: R.dataset_paths("pretrain/labelsel-fixture", ver, labels={"domain": "Python"}, s3=s3)
    )
    msg = str(warned[0].message)
    assert "SQL" not in msg
    assert "stack-edu" not in msg
    # 2 of 6, not 3: the SQL shard answered the question and lost, so it is not a blind spot.
    assert "ABSENT from 2 of 6 entries" in msg


def test_an_unlabelled_dataset_still_RAISES_rather_than_warning():
    """The two cases stay distinct: all-flat has no honest value to return, so it is an error."""
    s3 = FakeS3()
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir(parents=True)
    rng = np.random.default_rng(17)
    for name, n in (("train-00000.u32le.bin", 30000), ("val-00000.u32le.bin", 20000)):
        (d / "tokens" / name).write_bytes(
            rng.integers(1, 100278, size=n, dtype=np.uint32).tobytes()
        )
    plan = P.publish(
        d, dataset_id="pretrain/flatwarn-fixture",
        purpose="flat corpus proving the all-flat case still raises while partial only warns",
        profile="pretrain-tokens/v1", s3=s3, created_at=CREATED,
        group_meta={"tokens": {"tokenizer": TOKENIZER}}, env=ENV,
    )
    res = V.validate_dataset(
        "edullm-landing", f"pretrain/flatwarn-fixture/{plan.version}", s3,
        data_bucket="edullm-data")
    V.promote(res, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")
    with pytest.raises(R.ReadError) as e:
        R.dataset_paths("pretrain/flatwarn-fixture", plan.version, labels={"domain": "x"}, s3=s3)
    assert "unlabelled" in str(e.value)


def test_warn_partial_labels_False_silences_it_without_changing_the_result():
    """The opt-out has to be a pure diagnostic switch. If it moved a single path it would be a
    second selection semantics hiding behind a logging flag."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    loud, warned = _warnings_from(
        lambda: R.dataset_paths("pretrain/labelsel-fixture", ver, labels={"domain": "Python"}, s3=s3)
    )
    quiet, silent = _warnings_from(
        lambda: R.dataset_paths(
            "pretrain/labelsel-fixture", ver, labels={"domain": "Python"},
            warn_partial_labels=False, s3=s3,
        )
    )
    assert len(warned) == 1 and silent == []
    assert loud.paths == quiet.paths
    assert (loud.rows, loud.splits, loud.split_rows) == (quiet.rows, quiet.splits, quiet.split_rows)


def test_the_strict_reading_is_one_line_away():
    """Its own warning class exists so a training entrypoint can make exactly THIS fatal without
    promoting every unrelated UserWarning."""
    import warnings

    s3 = FakeS3()
    ver = _publish_promote(s3)
    with warnings.catch_warnings():
        warnings.simplefilter("error", R.PartialLabelCoverage)
        with pytest.raises(R.PartialLabelCoverage):
            R.dataset_paths("pretrain/labelsel-fixture", ver, labels={"domain": "Python"}, s3=s3)
        # …and an unaffected read still works under the same filter.
        assert R.dataset_paths(
            "pretrain/labelsel-fixture", ver, labels={"source": "arxiv"}, s3=s3).paths


def test_build_mixture_warns_on_the_same_condition():
    """A mixture is where this does real damage: `ratio` is a share of the budget, so a component
    reaching only the nested sources still draws its full ratio — the mix looks balanced,
    `shortfall` is empty, and the corpus is a fraction of what was named."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    mix, warned = _warnings_from(
        lambda: R.build_mixture(
            "pretrain/labelsel-fixture", ver, s3=s3, seed=3, total=40_000,
            sources=[R.MixtureSource({"domain": "Python"}, 1.0)],
        )
    )
    assert len(warned) == 1
    assert "source=arxiv" in str(warned[0].message)
    assert mix.paths and not mix.shortfall, "the silence this warning breaks: nothing looks wrong"


def test_a_mixture_selecting_only_by_source_does_not_warn():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    _, warned = _warnings_from(
        lambda: R.build_mixture(
            "pretrain/labelsel-fixture", ver, s3=s3, seed=3, total=40_000,
            sources=[
                R.MixtureSource({"source": "stack-edu"}, 0.5),
                R.MixtureSource({"source": "arxiv"}, 0.5),
            ],
        )
    )
    assert warned == []


def test_components_sharing_a_key_set_warn_once_not_once_each():
    """Ten components keyed on `domain` describe ONE coverage gap. Repeating it per component
    would teach everyone to skim past it."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    _, warned = _warnings_from(
        lambda: R.build_mixture(
            "pretrain/labelsel-fixture", ver, s3=s3, seed=3, total=20_000,
            sources=[
                R.MixtureSource({"domain": "Python"}, 0.5),
                R.MixtureSource({"domain": "SQL"}, 0.5),
            ],
        )
    )
    assert len(warned) == 1


def test_build_mixture_can_opt_out_too():
    """The two entry points take the same switch; a flag on one and not the other is a trap."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    _, warned = _warnings_from(
        lambda: R.build_mixture(
            "pretrain/labelsel-fixture", ver, s3=s3, seed=3, total=40_000,
            sources=[R.MixtureSource({"domain": "Python"}, 1.0)],
            warn_partial_labels=False,
        )
    )
    assert warned == []


def test_the_mixture_warning_counts_only_shards_the_mixture_could_have_drawn():
    """Reported against the split-filtered pool, so held-out shards it was never eligible for do
    not inflate "what you lost"."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    _, warned = _warnings_from(
        lambda: R.build_mixture(
            "pretrain/labelsel-fixture", ver, s3=s3, seed=3, total=40_000,
            sources=[R.MixtureSource({"domain": "Python"}, 1.0)],
        )
    )
    msg = str(warned[0].message)
    assert "1 shard" in msg and "40,000 tokens" in msg  # the arxiv TRAIN shard only
    assert "44,000" not in msg  # not train+val
