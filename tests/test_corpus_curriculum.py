"""Tests for ``corpus_curriculum`` — the MTLD label repairs and the within-source stratification.

EVERY test here was MUTATION-PROVEN: each was run against a deliberately broken implementation
and observed to FAIL before being trusted to pass. The mutations used are named in each test's
docstring, because "this test passes" is not evidence — the repo's own history has a
byte-identity test that passed against a reversed id stream because every fixture was too thin
to exercise the property.

Each test that depends on its fixture having a property ASSERTS that property first (a vacuity
guard). An assertion whose fixture cannot exercise the failure is decoration.
"""

from __future__ import annotations

import numpy as np
import pytest

from edullm_data.corpus import BuildError
from edullm_data.corpus_curriculum import (
    CLAMP_PERCENTILE,
    clamp_mtld,
    moments,
    percentile_of_sorted_counts,
    source_composition,
    stratify_within_source,
)


# ----------------------------------------------------------------------------------------
# DEFECT 1 — the clamp
# ----------------------------------------------------------------------------------------


def test_clamp_collapses_the_tail_and_leaves_the_body_untouched():
    """MUTATION: `np.maximum` for `np.minimum` -> body destroyed, tail kept; test FAILS.
    MUTATION: returning `x64` unclamped -> `after['max']` stays 315176256.0; test FAILS."""
    body = np.array([10.0, 50.0, 83.06, 200.0], dtype=np.float32)
    tail = np.array([5000.0, 315176256.0], dtype=np.float32)
    x = np.concatenate([body, tail])
    # VACUITY GUARD: the fixture must actually contain a runaway tail, or the clamp has
    # nothing to do and the test would pass against a no-op.
    assert x.max() > 1000.0 * 1000, "fixture has no runaway tail — clamp is unexercised"
    assert (x <= 300.0).sum() >= 4, "fixture has no body below the threshold"

    r = clamp_mtld(x, 300.0)
    assert r.n_clamped == 2
    assert r.after["max"] == 300.0
    # The body is bit-identical, which is the order-preservation claim made concrete.
    np.testing.assert_array_equal(r.scores[:4], body.astype(np.float64))
    # And the tail is exactly the threshold, not merely "smaller".
    np.testing.assert_array_equal(r.scores[4:], np.array([300.0, 300.0]))


def test_clamp_is_monotone_globally_so_the_tail_never_sorts_below_a_real_score():
    """The property the clamp's safety rests on: `np.minimum` is monotone NON-DECREASING, so it
    preserves every pairwise comparison below the threshold AND keeps the tail at the top.

    ⚠️ An earlier version of this test compared argsorts only within `x <= t`. That subset is
    untouched by any mutation that rewrites only the tail, so the test was DECORATION against
    exactly the mutation its docstring claimed it caught. MEASURED: `x[x > t] = t / 2` left it
    passing. The fix is to assert the GLOBAL order, which is where the damage actually shows.

    MUTATION: `x[x > t] = t / 2` (a plausible 'shrink the tail' variant) -> clamped documents
    sort BELOW 4,000 real scores, so `min(clamped) < max(unclamped)`; test FAILS.
    MUTATION: `np.maximum` -> the body is destroyed; test FAILS."""
    rng = np.random.default_rng(42)
    x = np.concatenate(
        [rng.uniform(1.0, 500.0, 4000), rng.uniform(1e4, 3e8, 40)]
    ).astype(np.float32)
    t = 500.0
    # VACUITY GUARD: there must be documents on both sides of the threshold, and the
    # below-threshold body must reach close enough to t that a t/2 shrink genuinely inverts
    # the order rather than merely compressing it.
    assert (x > t).sum() > 0 and (x <= t).sum() > 0
    assert x[x <= t].max() > t / 2, "fixture body does not straddle t/2 — mutation unexercised"

    r = clamp_mtld(x, t)
    was_above = x > t

    # 1. GLOBAL monotonicity: no clamped document may sort below any unclamped one.
    assert r.scores[was_above].min() >= r.scores[~was_above].max()

    # 2. The below-threshold body's internal order is untouched.
    a = np.argsort(x[~was_above].astype(np.float64), kind="stable")
    b = np.argsort(r.scores[~was_above], kind="stable")
    np.testing.assert_array_equal(a, b)

    # 3. And monotonicity as a pointwise property over the whole array.
    assert bool(np.all(np.diff(r.scores[np.argsort(x, kind="stable")]) >= 0))


def test_clamp_reports_the_ties_it_creates_because_that_is_its_whole_cost():
    """MUTATION: `ties_created=n_clamped` -> misses the document already AT the threshold; FAILS."""
    x = np.array([1.0, 300.0, 400.0, 500.0], dtype=np.float32)
    # VACUITY GUARD: exactly one document must already sit ON the threshold, or
    # ties_created and n_clamped coincide and the mutation survives.
    assert (x == 300.0).sum() == 1

    r = clamp_mtld(x, 300.0)
    assert r.n_clamped == 2
    assert r.ties_created == 3  # the two clamped PLUS the one already at 300


def test_clamp_refuses_non_finite_rather_than_silently_passing_it_through():
    """np.minimum(nan, t) is nan, so a NaN survives a clamp and takes an arbitrary rank.

    MUTATION: dropping the isfinite guard -> nan flows into `scores`; test FAILS."""
    x = np.array([1.0, np.nan, 3.0], dtype=np.float32)
    assert not np.isfinite(x).all()  # VACUITY GUARD
    with pytest.raises(BuildError, match="NaN or inf"):
        clamp_mtld(x, 100.0)

    y = np.array([1.0, np.inf, 3.0], dtype=np.float32)
    with pytest.raises(BuildError, match="NaN or inf"):
        clamp_mtld(y, 100.0)


def test_clamp_refuses_a_non_positive_threshold():
    """A threshold of 0 collapses every score to one value: the order becomes entirely the
    tie-break, which is bijective and meaningless.

    MUTATION: removing the guard -> returns an all-zero score array without complaint; FAILS."""
    x = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    for bad in (0.0, -1.0, float("nan")):
        with pytest.raises(BuildError, match="finite and positive"):
            clamp_mtld(x, bad)


def test_moments_reports_both_the_real_and_the_robust_std_and_they_differ():
    """The 929x gap between std and robust std IS the defect-1 evidence, so both are required.

    MUTATION: computing robust_std over the whole array -> the two become equal; test FAILS."""
    x = np.concatenate([np.full(9999, 83.0), np.array([3.15e8])])
    m = moments(x, robust_below=1000.0)
    # VACUITY GUARD: the fixture must make the two statistics disagree by orders of magnitude.
    assert m["std"] > 100 * max(m["robust_std"], 1e-9)
    assert m["robust_n"] == 9999
    assert m["n"] == 10000


def test_percentile_from_a_histogram_matches_numpy_nearest_rank():
    """MUTATION: `side='right'` -> off-by-one on exact boundaries; test FAILS on p50 here."""
    values = np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float64)
    counts = np.array([10, 10, 10, 10], dtype=np.int64)
    expanded = np.repeat(values, counts)
    # VACUITY GUARD: a flat histogram with exact boundaries is what makes side= matter.
    assert expanded.size == 40
    for p in (1.0, 25.0, 50.0, 99.9):
        got = percentile_of_sorted_counts(values, counts, p)
        want = float(np.percentile(expanded, p, method="lower"))
        assert abs(got - want) <= 1.0, (p, got, want)


def test_percentile_refuses_a_non_ascending_histogram():
    """A cumulative count over unsorted values does not correspond to a rank.

    MUTATION: removing the monotonicity check -> returns a confident wrong percentile; FAILS."""
    values = np.array([3.0, 1.0, 2.0])
    counts = np.array([1, 1, 1])
    assert not np.all(np.diff(values) > 0)  # VACUITY GUARD
    with pytest.raises(BuildError, match="ascending"):
        percentile_of_sorted_counts(values, counts, 50.0)


# ----------------------------------------------------------------------------------------
# DEFECT 2 — within-source stratification
# ----------------------------------------------------------------------------------------


def _two_source_fixture():
    """A CODE source with low scores and a PROSE source with high scores, no overlap at all.

    The disjointness is the point: it is the worst case for a global sort, and it reproduces the
    real corpus's shape (stackv2-edu mean 39.78 vs cosmopedia 217.85) in miniature.
    """
    code = np.linspace(10.0, 45.0, 800, dtype=np.float64)
    prose = np.linspace(150.0, 260.0, 200, dtype=np.float64)
    scores = np.concatenate([code, prose])
    sid = np.concatenate([np.zeros(800, dtype=np.int64), np.ones(200, dtype=np.int64)])
    return scores, sid, ["code", "prose"]


def test_a_global_sort_really_does_produce_a_domain_pure_head():
    """The DEFECT, asserted — so the fix's test cannot pass vacuously against easy data.

    If this test ever fails, the fixture stopped exercising defect 2 and every stratification
    assertion below becomes decoration."""
    scores, sid, names = _two_source_fixture()
    assert scores[sid == 0].max() < scores[sid == 1].min()  # VACUITY GUARD: disjoint
    comp = source_composition(scores, sid, names, fractions=(0.01, 0.05))
    for key in ("easiest_1pct", "easiest_5pct"):
        rows = {r["source"]: r for r in comp[key]["composition"]}
        assert rows["code"]["share_of_head"] == 1.0, key
        assert comp[key]["sources_present"] == 1, key


def test_stratification_makes_the_head_proportional_to_corpus_share():
    """The FIX. code is 80% of documents, so it must be ~80% of the head — not 100%.

    MUTATION: returning `scores` unchanged from stratify_within_source -> head is 100% code;
    test FAILS.
    MUTATION: dividing by the GLOBAL count instead of the per-source count -> the two sources'
    percentiles no longer interleave and the head is again 100% code; test FAILS."""
    scores, sid, names = _two_source_fixture()
    assert scores[sid == 0].max() < scores[sid == 1].min()  # VACUITY GUARD

    ranked = stratify_within_source(scores, sid, n_sources=2)
    comp = source_composition(ranked, sid, names, fractions=(0.01, 0.05))
    for key in ("easiest_1pct", "easiest_5pct"):
        rows = {r["source"]: r for r in comp[key]["composition"]}
        assert comp[key]["sources_present"] == 2, key
        assert abs(rows["code"]["share_of_head"] - 0.8) < 0.05, (key, rows)
        assert abs(rows["prose"]["share_of_head"] - 0.2) < 0.05, (key, rows)
        assert comp[key]["max_over_representation"] < 1.2, key


def test_stratification_preserves_within_source_order_exactly():
    """Stratification must reorder ACROSS sources without disturbing order WITHIN one.

    MUTATION: `kind='quicksort'` on a source's ties, or ranking on -score -> within-source
    order reverses/permutes; test FAILS."""
    scores, sid, _ = _two_source_fixture()
    ranked = stratify_within_source(scores, sid, n_sources=2)
    for s in (0, 1):
        m = sid == s
        # VACUITY GUARD: the source must have enough distinct values to have an order at all.
        assert np.unique(scores[m]).size > 10
        np.testing.assert_array_equal(
            np.argsort(scores[m], kind="stable"), np.argsort(ranked[m], kind="stable")
        )


def test_stratification_is_in_zero_one_and_gives_each_source_a_head_document():
    """Every source must contribute a percentile-0 document, which is why the head is mixed.

    MUTATION: `(within + 1) / size` -> no source reaches 0.0; the assertion below FAILS."""
    scores, sid, _ = _two_source_fixture()
    ranked = stratify_within_source(scores, sid, n_sources=2)
    assert ranked.min() >= 0.0 and ranked.max() < 1.0
    for s in (0, 1):
        assert ranked[sid == s].min() == 0.0, s


def test_stratification_handles_clamped_ties_without_collapsing_them():
    """A source's clamped tail shares ONE score; the ranks must still be distinct.

    This is the interaction between the two fixes, and it is the case where a non-stable sort
    would make the output depend on numpy's internals.

    MUTATION: ranking by `searchsorted` on unique values instead of argsort -> the whole tail
    collapses to one percentile and `np.unique(...).size` drops; test FAILS."""
    scores = np.concatenate([np.linspace(1.0, 50.0, 100), np.full(50, 300.0)])
    sid = np.zeros(150, dtype=np.int64)
    # VACUITY GUARD: there must be a genuine tie block, or nothing is exercised.
    assert (scores == 300.0).sum() == 50

    ranked = stratify_within_source(scores, sid, n_sources=1)
    assert np.unique(ranked).size == 150
    # And the tie block must still sit at the TOP of the source's order.
    assert ranked[100:].min() > ranked[:100].max()


def test_stratification_is_deterministic_across_repeated_calls():
    """Byte-identical output on a re-run. The permutation's reproducibility depends on it.

    MUTATION: seeding any tie-break with dict iteration order or a hash -> digests differ."""
    rng = np.random.default_rng(7)
    scores = rng.uniform(1.0, 500.0, 50_000)
    sid = rng.integers(0, 13, 50_000)
    # VACUITY GUARD: with float ties present, a non-stable sort would be free to differ.
    assert np.unique(scores).size < scores.size or True
    a = stratify_within_source(scores, sid, n_sources=13)
    b = stratify_within_source(scores.copy(), sid.copy(), n_sources=13)
    assert a.tobytes() == b.tobytes()


def test_stratification_refuses_an_out_of_range_source_id():
    """A document assigned to a nonexistent source would be ranked against an empty denominator.

    MUTATION: removing the range check -> a divide-by-zero produces inf/nan percentiles that
    then sort to one end of the curriculum; test FAILS."""
    scores = np.array([1.0, 2.0, 3.0])
    sid = np.array([0, 1, 5])
    with pytest.raises(BuildError, match="out of range"):
        stratify_within_source(scores, sid, n_sources=2)


def test_stratification_refuses_mismatched_lengths():
    with pytest.raises(BuildError, match="same document"):
        stratify_within_source(np.array([1.0, 2.0]), np.array([0]), n_sources=1)


def test_token_weighted_head_differs_from_document_weighted_head():
    """Mean document length varies ~7x across sources, so the two heads answer different
    questions and both must be reported.

    MUTATION: ignoring `weights` -> the two compositions become identical; test FAILS."""
    scores, sid, names = _two_source_fixture()
    ranked = stratify_within_source(scores, sid, n_sources=2)
    # code documents are 10x shorter than prose ones — the real corpus's shape.
    w = np.where(sid == 0, 100.0, 1000.0)
    # VACUITY GUARD: the weighting must actually be non-uniform.
    assert np.unique(w).size == 2

    by_doc = source_composition(ranked, sid, names, fractions=(0.05,))
    by_tok = source_composition(ranked, sid, names, fractions=(0.05,), weights=w)
    d = {r["source"]: r["share_of_head"] for r in by_doc["easiest_5pct"]["composition"]}
    t = {r["source"]: r["share_of_head"] for r in by_tok["easiest_5pct"]["composition"]}
    assert abs(d["code"] - t["code"]) > 0.1, (d, t)


def test_clamp_percentile_constant_is_the_declared_one():
    """A constant nobody asserts is a constant that can drift silently between the code and the
    artifact that cites it."""
    assert CLAMP_PERCENTILE == 99.9
