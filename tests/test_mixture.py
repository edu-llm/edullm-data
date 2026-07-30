"""``build_mixture`` — a weighted, seeded, budgeted subset of one dataset.

The reference is OLMo-core's ``SourceMixtureDatasetConfig``, and reading it changed what this
does. Two findings:

* Its ``seed`` field is **dead** — declared, documented as controlling "sampling the actual
  instances", and never read anywhere in that file. There is no prior behaviour to match.
* Selection is ``ceil(available_tokens * ratio)`` from the HEAD of every path. A 10% mixture
  reads the first 10% of every shard and never touches a tail, so any ordering inside a shard
  (crawl batch, date, repo) becomes a systematic skew.

So: whole shards, drawn in a seed-determined order. No positional bias, and no need to express
"the first N tokens of this file" — which neither ``ResolvedSplit`` nor OLMo-core can represent.
The price is that a budget lands within one shard of target instead of exactly on it.

The determinism tests are the ones that matter. A mixture that is not reproducible from its
config is not a described experiment, and a determinism test that still passes when determinism
is removed is worse than none — so these are mutation-checked against a sort-by-path shuffle.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from edullm_data import publish as P
from edullm_data import read as R
from edullm_data import validate as V
from edullm_data.read import MixtureSource, build_mixture
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
DSID = "pretrain/mixture-fixture"

#: big=20 shards x 50k = 1,000,000 · mid=10 x 20k = 200,000 · tiny=2 x 5k = 10,000.
#: Deliberately lopsided: `tiny` cannot reach a large ratio without repetition, which is the
#: case `max_repetition_ratio` exists for and the case the legacy config actually hit.
SOURCES = {"big": (20, 50000), "mid": (10, 20000), "tiny": (2, 5000)}


def _publish_promote(s3: FakeS3, dsid: str = DSID) -> str:
    d = Path(tempfile.mkdtemp())
    rng = np.random.default_rng(5)
    ordinal = 0
    for src, (count, size) in SOURCES.items():
        sub = d / "tokens" / src
        sub.mkdir(parents=True, exist_ok=True)
        for _ in range(count):
            (sub / f"train-{ordinal:05d}.u32le.bin").write_bytes(
                rng.integers(1, 100278, size=size, dtype=np.uint32).tobytes()
            )
            ordinal += 1
        (sub / f"val-{ordinal:05d}.u32le.bin").write_bytes(
            rng.integers(1, 100278, size=5000, dtype=np.uint32).tobytes()
        )
        ordinal += 1
    plan = P.publish(
        d, dataset_id=dsid,
        purpose="fixture corpus for seeded weighted mixture selection across labelled sources",
        profile="pretrain-tokens/v1", s3=s3, created_at=CREATED,
        group_meta={"tokens": {"tokenizer": TOKENIZER}}, env=ENV,
    )
    res = V.validate_dataset(
        "edullm-landing", f"{dsid}/{plan.version}", s3, data_bucket="edullm-data")
    assert res.ok, [str(v) for v in res.violations]
    V.promote(res, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")
    return plan.version


def _mix(s3, ver, *, seed=42, total=600_000, srcs=None, dsid=DSID):
    return build_mixture(
        dsid, ver, s3=s3, seed=seed, total=total,
        sources=srcs or [
            MixtureSource({"source": "big"}, 0.5),
            MixtureSource({"source": "mid"}, 0.5),
        ],
    )


# ---- determinism: the load-bearing property ----

def test_same_seed_gives_the_identical_shard_list():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    a, b, c = (_mix(s3, ver, seed=7) for _ in range(3))
    assert a.paths == b.paths == c.paths
    assert a.paths, "an empty mixture would make this vacuous"


def test_a_different_seed_gives_a_different_shard_list():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    assert _mix(s3, ver, seed=7).paths != _mix(s3, ver, seed=8).paths


def test_the_same_seed_picks_differently_in_a_different_dataset():
    """``dataset_id``/``version`` are bound into the shuffle key, so reusing one seed across
    datasets is not a hidden correlation between their samples."""
    s3 = FakeS3()
    v1 = _publish_promote(s3, DSID)
    v2 = _publish_promote(s3, "pretrain/mixture-fixture-two")

    def tail(m):
        return sorted(p.split("/tokens/", 1)[1] for p in m.paths)

    assert tail(_mix(s3, v1, seed=7)) != tail(_mix(s3, v2, seed=7, dsid="pretrain/mixture-fixture-two"))


def test_selection_is_not_merely_the_sorted_order():
    """If it were, the seed would be decorative — the failure mode of the class this replaces."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    m = build_mixture(
        DSID, ver, s3=s3, seed=1, total=400_000,
        sources=[MixtureSource({"source": "big"}, 1.0)],
    )
    picked = [p.rsplit("/", 1)[-1] for p in m.paths]
    assert picked != sorted(picked), "shards came back in path order; the shuffle did nothing"


# ---- ratios and budget ----

def test_ratios_land_near_target():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    m = _mix(s3, ver, total=600_000)
    for name, want in m.requested_ratios.items():
        assert abs(m.actual_ratios[name] - want) < 0.15, m.actual_ratios


def test_the_budget_is_approximately_met_with_whole_shards():
    """400k over big+mid: 200k each, and mid has exactly 200k, so both are satisfiable.

    (600k would demand 300k from mid, which only holds 200k — a shortfall, not a budget
    miss. That case is asserted separately below.)
    """
    s3 = FakeS3()
    ver = _publish_promote(s3)
    m = _mix(s3, ver, total=400_000)
    assert not m.shortfall, m.shortfall
    assert 400_000 <= m.total <= 400_000 + 50_000  # at most one `big` shard of overshoot
    assert m.unit == "tokens"


def test_counts_are_reported_per_source_and_sum_to_the_total():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    m = _mix(s3, ver)
    assert sum(m.counts_by_source.values()) == m.total
    assert set(m.counts_by_source) == {"source=big", "source=mid"}


def test_every_selected_uri_is_a_real_train_shard():
    """A mixture must never hand back a held-out shard."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    m = _mix(s3, ver)
    train = set(R.dataset_paths(DSID, ver, s3=s3).train)
    assert set(m.paths) <= train
    assert not any("/val-" in p for p in m.paths)


def test_the_format_triple_comes_through():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    m = _mix(s3, ver)
    assert (m.dtype, m.numpy_dtype, m.header_bytes) == ("uint32", "<u4", 0)


# ---- shortfall, and the two legacy knobs ----

def test_a_source_that_cannot_reach_its_ratio_reports_a_shortfall():
    """`tiny` has 10,000 tokens; 50% of 600,000 is 300,000. Silence here would be a mixture
    that is not the mixture you asked for."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    m = _mix(s3, ver, total=600_000, srcs=[
        MixtureSource({"source": "tiny"}, 0.5),
        MixtureSource({"source": "big"}, 0.5),
    ])
    assert m.counts_by_source["source=tiny"] == 10_000
    assert m.shortfall["source=tiny"] == 290_000
    assert "source=big" not in m.shortfall


def test_max_repetition_ratio_upsamples_a_small_source():
    """The knob the legacy 10b-config.yaml actually used (1.05 on arxiv and wikipedia)."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    m = _mix(s3, ver, total=60_000, srcs=[
        MixtureSource({"source": "tiny"}, 0.5, max_repetition_ratio=3.0),
        MixtureSource({"source": "big"}, 0.5),
    ])
    got = m.counts_by_source["source=tiny"]
    assert got > 10_000, "repetition did not happen"
    assert got <= 30_000, "repeated beyond the 3x ceiling"
    assert "source=tiny" not in m.shortfall


def test_repetition_is_deterministic_too():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    kw = dict(total=60_000, srcs=[
        MixtureSource({"source": "tiny"}, 0.5, max_repetition_ratio=3.0),
        MixtureSource({"source": "big"}, 0.5)])
    assert _mix(s3, ver, seed=9, **kw).paths == _mix(s3, ver, seed=9, **kw).paths


def test_max_source_fraction_is_a_hard_cap_not_a_target():
    """A budget may overshoot by part of a shard; a LIMIT may not. Filling toward the target and
    letting the last shard straddle the line turned a 10% cap into 13.5% against the live
    corpus, which is not a cap.

    **The fraction must NOT land on a shard boundary or this proves nothing.** `big` is
    20 x 50,000; a 25% cap is exactly 5 shards, so the guard is never exercised and removing it
    changes no output — verified by mutation. 22% = 220,000 falls between 4 shards (200,000) and
    5 (250,000), which is the only shape that distinguishes a cap from a target.
    """
    s3 = FakeS3()
    ver = _publish_promote(s3)
    m = _mix(s3, ver, total=1_000_000, srcs=[
        MixtureSource({"source": "big"}, 1.0, max_source_fraction=0.22)])
    got = m.counts_by_source["source=big"]
    assert got <= 220_000, f"breached the cap: {got:,} > 220,000"
    assert got == 200_000, "should take the 4 whole shards that fit, and stop"


def test_both_knobs_compose():
    s3 = FakeS3()
    ver = _publish_promote(s3)
    m = _mix(s3, ver, total=100_000, srcs=[
        MixtureSource({"source": "tiny"}, 1.0, max_repetition_ratio=4.0, max_source_fraction=0.5)])
    # ceiling = 10,000 available * 0.5 fraction * 4.0 repetition = 20,000
    assert m.counts_by_source["source=tiny"] <= 20_000


# ---- refusals ----

@pytest.mark.parametrize(
    "srcs,total,fragment",
    [
        ([], 100, "at least one source"),
        ([MixtureSource({"source": "big"}, 1.0)], 0, "total must be > 0"),
        ([MixtureSource({}, 1.0)], 100, "empty label predicate"),
        ([MixtureSource({"source": "big"}, 0.3)], 100, "must sum to 1.0"),
        ([MixtureSource({"source": "big"}, 1.5)], 100, "ratio must be in"),
        ([MixtureSource({"source": "nope"}, 1.0)], 100, "matches no shards"),
        ([MixtureSource({"source": "big"}, 1.0, max_repetition_ratio=0.5)], 100,
         "max_repetition_ratio must be >= 1"),
        ([MixtureSource({"source": "big"}, 1.0, max_source_fraction=0)], 100,
         "max_source_fraction must be in"),
        ([MixtureSource({"source": "big"}, 0.5), MixtureSource({"source": "big"}, 0.5)], 100,
         "duplicate source predicates"),
    ],
)
def test_bad_input_is_refused_with_a_specific_message(srcs, total, fragment):
    s3 = FakeS3()
    ver = _publish_promote(s3)
    with pytest.raises(R.ReadError) as e:
        build_mixture(DSID, ver, s3=s3, seed=1, total=total, sources=srcs)
    assert fragment in str(e.value)


def test_ratios_must_sum_to_one_because_a_remainder_would_be_silent():
    """0.5 + 0.3 leaves 20% of the budget undecided; refusing beats inventing a rule."""
    s3 = FakeS3()
    ver = _publish_promote(s3)
    with pytest.raises(R.ReadError):
        build_mixture(DSID, ver, s3=s3, seed=1, total=100_000, sources=[
            MixtureSource({"source": "big"}, 0.5), MixtureSource({"source": "mid"}, 0.3)])


def test_source_names_are_stable_and_readable():
    src = MixtureSource({"domain": "Python", "source": "stack-edu"}, 1.0)
    assert src.name == "domain=Python,source=stack-edu"  # sorted, so order-independent
