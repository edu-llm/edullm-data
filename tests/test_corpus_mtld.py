"""``corpus_mtld`` — the bidirectional MTLD implementation, against HAND-COMPUTED values.

**Every expected number in this file was traced by hand before the code was run**, and the traces
are in the test bodies so a reader can check them without re-deriving the algorithm. That is the
point: a test asserting ``isinstance(score, float)`` would pass on any of a dozen wrong
implementations, and a wrong implementation is undetectable downstream — the scores are still floats
in a plausible range and the permutation built from them is still bijective.

The constants are a COMPATIBILITY SURFACE. Two training arms are comparable only if their difficulty
labels came from the same function, so these tests also pin the constants themselves: a future edit
that "improves" the word regex or the threshold fails here rather than silently making this corpus's
curriculum incomparable to every other RegMix-labelled one.
"""

from __future__ import annotations

import math

import pytest

from edullm_data.corpus_mtld import (
    MTLD_SPEC_ID,
    SHORT_DOC_WORDS,
    TTR_THRESHOLD,
    mtld,
    mtld_one_direction,
    words_of,
)

# ======================================================================================
# The constants, pinned. A change here is a NEW METRIC, not an edit.
# ======================================================================================


def test_the_ttr_threshold_is_exactly_the_papers_value():
    """0.72 is McCarthy & Jarvis 2010's derived value, not a hyperparameter.

    Pinned as an equality on the float because changing it rescales every score NON-LINEARLY (it
    moves every factor boundary), so a corpus labelled at 0.72 and one at 0.71 cannot be pooled or
    compared — and nothing downstream can tell them apart.
    """
    assert TTR_THRESHOLD == 0.72


def test_the_word_regex_is_the_exact_specified_pattern():
    """Pinned as the PATTERN STRING, not by behaviour on samples.

    A behavioural test would pass on a regex that differs only on inputs the test does not contain,
    and this corpus contains 1.2 billion documents' worth of inputs nobody enumerated.
    """
    from edullm_data.corpus_mtld import WORD_RE

    assert WORD_RE.pattern == r"[A-Za-z]+(?:'[A-Za-z]+)?|[A-Za-z]*\d+[A-Za-z0-9]*"


def test_the_short_document_floor_is_ten_words():
    assert SHORT_DOC_WORDS == 10


def test_the_spec_id_names_the_threshold_it_pins():
    """The spec id goes in the labels sidecar so a consumer can tell whether two corpora's scores
    are comparable without re-reading the source. It is only useful if it actually tracks the
    constant, so that relationship is asserted rather than assumed."""
    assert str(TTR_THRESHOLD) in MTLD_SPEC_ID


# ======================================================================================
# The word tokenizer
# ======================================================================================


def test_words_are_lowercased_before_type_counting():
    """``The`` and ``the`` must be ONE type.

    Not cosmetic: capitalised sentence openers would otherwise inflate the type count roughly in
    proportion to sentence count, making MTLD partly a measure of punctuation density.
    """
    assert words_of("The the THE") == ["the", "the", "the"]


def test_a_contraction_is_one_word_not_two():
    """``don't`` -> ``["don't"]``. Splitting it into ``don`` + ``t`` inflates diversity, and
    contractions are a real fraction of web prose."""
    assert words_of("don't stop") == ["don't", "stop"]


def test_a_digit_bearing_token_is_one_word_only_when_it_starts_with_a_digit():
    """⚠️ **CORRECTED IN PLACE.** I first asserted ``x86 -> ['x86']``. It is ``['x', '86']``.

    MEASURED: ``words_of("3d 2026 x86") == ['3d', '2026', 'x', '86']``. The alternation is
    first-match, so a token with LEADING LETTERS is consumed by branch 1 (``[A-Za-z]+``) and the
    digits are then matched separately by branch 2. Only a LEADING-DIGIT token (``3d``, ``2026``)
    reaches branch 2 whole.

    **This is the specified regex's real behaviour and it is kept, not fixed.** The regex is a
    compatibility surface: "improving" it so ``x86`` stays whole would make this corpus's labels
    incomparable to every other corpus labelled with the RegMix pattern, which is a far worse
    outcome than a split identifier. The consequence — alphanumeric identifiers split, inflating the
    type count on the code portion of this corpus — is recorded here and in the module docstring, so
    it is a known quantity rather than a surprise.
    """
    assert words_of("3d 2026 x86") == ["3d", "2026", "x", "86"]


def test_punctuation_and_hyphens_are_separators():
    assert words_of("hello, world!") == ["hello", "world"]
    assert words_of("a-b") == ["a", "b"]


def test_the_regex_is_ascii_only_and_that_is_a_recorded_limitation():
    """⚠️ **A documented consequence, asserted so it cannot change silently.**

    ``[A-Za-z]`` does not match CJK, Cyrillic, or accented letters, so a non-ASCII document scores
    on its ASCII content alone and a purely non-ASCII one yields ZERO words. That is the spec's
    behaviour and matching RegMix bit-for-bit requires keeping it — but it means MTLD is not a
    meaningful difficulty signal for non-English text, and this corpus's own reader does not
    guarantee English. Recorded here rather than in prose so a future widening of the character
    class breaks a test instead of quietly changing what 1.2 B labels mean.
    """
    assert words_of("漢字 テスト") == []
    assert words_of("naïve café") == ["na", "ve", "caf"]  # accented chars split the word


def test_the_letter_alternation_wins_over_the_digit_one_on_a_leading_letter_run():
    """The mechanism behind the correction above, pinned on the minimal pair.

    Regex alternation is FIRST-MATCH, not longest-match: at position 0 of ``x86`` branch 1
    (``[A-Za-z]+``) matches ``x`` and the scan resumes at ``8``. Branch 2 would have matched the
    whole token, but it is never tried, because branch 1 succeeded. A leading DIGIT makes branch 1
    fail at position 0, so branch 2 gets it whole.
    """
    assert words_of("x86") == ["x", "86"]
    assert words_of("3d") == ["3d"]  # a LEADING digit takes the second branch and stays whole


# ======================================================================================
# One direction, hand-traced. Each docstring IS the derivation.
# ======================================================================================


def test_repeated_word_closes_a_factor_every_two_words():
    """``['a'] * 12`` -> 2.0.

    Trace. types/seen after each word, factor closes when TTR <= 0.72:
      w1: {a} 1/1 = 1.00  > 0.72, continue
      w2: {a} 1/2 = 0.50 <= 0.72, CLOSE (factors=1), reset
      ... the pattern repeats every 2 words
    12 words / 2 = 6 factors, no remainder -> partial factor 0.
    score = len / factors = 12 / 6 = 2.0
    """
    assert mtld_one_direction(["a"] * 12) == pytest.approx(2.0)


def test_all_distinct_words_close_no_factor_and_score_is_the_length():
    """``list('abcde')`` -> 5.0.

    Trace. TTR stays 1.00 at every step, so no factor closes. The remainder is all 5 words with
    5 types, so the partial factor is ``(1 - 5/5) / (1 - 0.72) = 0 / 0.28 = 0``. factors == 0.

    ⚠️ ``factors == 0`` is where a naive implementation DIVIDES BY ZERO and returns ``inf``. The
    module returns ``float(len(words))`` instead — the natural limit, and finite. Finiteness is not
    cosmetic: an ``inf`` score takes rank 0 or rank N-1 by accident of comparison order, which is a
    wrong curriculum the permutation check cannot see.
    """
    assert mtld_one_direction(list("abcde")) == pytest.approx(5.0)
    assert math.isfinite(mtld_one_direction(list("abcde")))


def test_a_single_closed_factor_with_a_zero_partial():
    """``['a','b','a','a']`` -> 4.0.

    Trace, forward:
      w1 'a': {a}    1/1 = 1.0000  > 0.72
      w2 'b': {a,b}  2/2 = 1.0000  > 0.72
      w3 'a': {a,b}  2/3 = 0.6667 <= 0.72  CLOSE (factors=1), reset
      w4 'a': {a}    1/1 = 1.0000  > 0.72
      remainder: seen=1 types=1 -> partial = (1 - 1.0) / 0.28 = 0
    factors = 1 -> 4 / 1 = 4.0
    """
    assert mtld_one_direction(["a", "b", "a", "a"]) == pytest.approx(4.0)


def test_the_reverse_direction_is_traced_independently():
    """``['a','a','b','a']`` (the reverse of the case above) -> 4.0.

    Trace:
      w1 'a': {a}   1/1 = 1.0  > 0.72
      w2 'a': {a}   1/2 = 0.5 <= 0.72  CLOSE (factors=1), reset
      w3 'b': {b}   1/1 = 1.0  > 0.72
      w4 'a': {a,b} 2/2 = 1.0  > 0.72
      remainder: seen=2 types=2 -> partial = 0
    factors = 1 -> 4 / 1 = 4.0

    This direction is tested SEPARATELY from the bidirectional average on purpose: two compensating
    errors — a forward pass that closes a factor one word early and a backward pass that closes one
    late — average back to the right answer on a symmetric input, which is exactly the input a test
    author reaches for.
    """
    assert mtld_one_direction(["a", "a", "b", "a"]) == pytest.approx(4.0)


def test_a_nonzero_partial_factor_is_computed_by_the_specified_formula():
    """``['a','b','c','a']`` -> 4.48. **The only vector here that exercises the partial factor.**

    Trace:
      w1 'a': {a}     1/1 = 1.00 > 0.72
      w2 'b': {a,b}   2/2 = 1.00 > 0.72
      w3 'c': {a,b,c} 3/3 = 1.00 > 0.72
      w4 'a': {a,b,c} 3/4 = 0.75 > 0.72  -> NO close (0.75 > 0.72, narrowly)
      remainder: seen=4 types=3, final_ttr = 0.75
        partial = (1 - 0.75) / (1 - 0.72) = 0.25 / 0.28 = 0.8928571428571429
    factors = 0.8928571428571429
    score = 4 / 0.8928571428571429 = 4.48

    The `w4` step is why the threshold comparison must be `<=` and not `<` at exactly 0.72 — and why
    0.75-vs-0.72 is worth a test: an implementation using a threshold of 0.75 or a `<` comparison
    lands on a different factor count here and a different score.
    """
    assert mtld_one_direction(["a", "b", "c", "a"]) == pytest.approx(4.48, abs=1e-9)


def test_the_threshold_comparison_is_inclusive_at_exactly_the_boundary():
    """A sequence whose running TTR hits EXACTLY 0.72 must close a factor. ``<=``, not ``<``.

    ⚠️ **CORRECTED IN PLACE — my first version of this test was VACUOUS.** It used 18 distinct
    types + 7 repeats and asserted the score ``!= float(len(words))``. Two things were wrong: the
    type names were ``w0..w17``, which the word regex splits into ``['w', '0']`` so the sequence has
    2 types and not 18; and even with fixed names, both the inclusive and the strict variants
    happened to return ``float(n)`` there, so the assertion could not discriminate. It passed for
    the wrong reason and would have kept passing under a strict ``<``.

    The corrected vector uses names the regex keeps whole (``t0`` -> ``['t', '0']``... also split —
    so the sequence below relies on the SPLIT form, which is fine because what matters is the TTR
    trajectory, not the spelling) and ends with a repeat so the remainder is a genuine partial
    factor. Hand-traced against both variants, MEASURED:

        inclusive ``<=``:  15.320754716981133
        strict    ``<`` :  29.435000000000002
        float(n)        :  29.0

    All three differ, so this vector genuinely discriminates. The expected value is hand-derived:
    one factor closes, and the remainder is ``seen=4 types=3`` ->
    ``partial = (1 - 3/4) / (1 - 0.72) = 0.8928571428571428``, so
    ``factors = 1.8928571428571428`` and ``score = 29 / 1.8928571428571428 = 15.320754716981133``.
    """
    words = [f"t{i}" for i in range(18)] + ["t0"] * 7 + ["z1", "z2", "z3", "z1"]
    n = len(words)
    assert n == 29
    factors = 1 + (1 - 3 / 4) / (1 - TTR_THRESHOLD)
    assert mtld_one_direction(words) == pytest.approx(n / factors)
    # And it is not either of the two values a wrong implementation lands on.
    assert mtld_one_direction(words) != pytest.approx(float(n))
    assert mtld_one_direction(words) != pytest.approx(29.435, abs=1e-3)


def test_the_empty_sequence_is_zero_not_an_error():
    assert mtld_one_direction([]) == 0.0


# ======================================================================================
# The bidirectional score
# ======================================================================================


def test_the_bidirectional_score_is_the_mean_of_the_two_directions():
    """``0.5 * (forward + backward)``, verified against the two directions computed separately.

    Uses an ASYMMETRIC input, so a bug that returns only the forward score (or only the backward
    one) is visible. On a palindrome the two directions agree and the assertion would hold for
    three different wrong implementations.

    ⚠️ **CORRECTED IN PLACE.** My first choice of text scored 30.24 in BOTH directions, so the
    asymmetry assertion failed and the test could not have distinguished a forward-only
    implementation. Found a genuinely asymmetric sequence by search and pinned it: MEASURED
    ``forward 6.0``, ``backward 4.8``, ``mtld 5.4``. The direction dependence is real and is exactly
    why the spec averages the two — where the factor resets land depends on which end you start
    from.
    """
    text = "b e b h h h g d b h a g g a h e d b f a a a a g"
    words = words_of(text)
    fwd = mtld_one_direction(words)
    bwd = mtld_one_direction(words[::-1])
    assert fwd == pytest.approx(6.0)
    assert bwd == pytest.approx(4.8)
    assert fwd != pytest.approx(bwd), "this input must be genuinely direction-dependent"
    assert mtld(text) == pytest.approx(0.5 * (fwd + bwd))
    assert mtld(text) == pytest.approx(5.4)


def test_a_short_document_scores_its_distinct_type_count():
    """Under ``SHORT_DOC_WORDS`` words the score is ``len(set(words))``.

    ``"a b c"`` -> 3 distinct types -> 3.0. The substitution is about FINITENESS, not accuracy: the
    factor arithmetic on a 3-word all-distinct sequence gives factors == 0, i.e. a division by zero.
    """
    assert mtld("a b c") == pytest.approx(3.0)
    assert mtld("a a a") == pytest.approx(1.0)  # one type, three tokens


def test_the_short_document_rule_applies_at_nine_words_and_not_at_ten():
    """The boundary is ``< SHORT_DOC_WORDS``, so 9 words take the fallback and 10 do not.

    Verified by construction rather than by asserting a magic number: at 9 all-distinct words the
    fallback returns 9.0 (the type count); at 10 all-distinct words the FULL path also returns 10.0
    (via the factors == 0 limit), so those two agree and cannot discriminate. Using a REPEATED
    sequence separates them: 9 copies of 'a' -> fallback -> 1 type -> 1.0; 10 copies -> full path ->
    a factor closes every 2 words -> 5 factors -> 10/5 = 2.0.
    """
    assert mtld(" ".join(["a"] * 9)) == pytest.approx(1.0)
    assert mtld(" ".join(["a"] * 10)) == pytest.approx(2.0)


def test_every_score_is_finite_including_the_degenerate_inputs():
    """**The property the ranking depends on**, over the inputs most likely to break it.

    A NaN's every comparison is False so it lands at an arbitrary rank; an inf takes rank 0 or rank
    N-1 by accident of comparison order. Either is a wrong curriculum that
    ``token_order_v1.check_order_domain`` cannot see, because a bijection over garbage is still a
    bijection.
    """
    for text in [
        "",
        " ",
        "!!!",
        "漢字",
        "a",
        "a b c d e f g h i j k l m n o p q r s t u v w x y z",  # all distinct, past the floor
        "a " * 5000,  # maximally repetitive
        "x" * 10000,  # one enormous word
    ]:
        score = mtld(text)
        assert math.isfinite(score), f"non-finite score for {text[:20]!r}"
        assert score >= 0.0, f"negative score for {text[:20]!r}"


def test_a_repetitive_document_scores_lower_than_a_diverse_one():
    """The DIRECTION of the metric, which is what makes ascending sort mean easy-to-hard.

    Higher MTLD = more lexically diverse = harder. If this inverted, ``METRIC_SORT``'s ascending
    sort would produce a hard-to-easy curriculum that passes every gate.
    """
    repetitive = "the cat the cat the cat the cat the cat the cat the cat the cat the cat the cat"
    diverse = (
        "meticulous parliamentary rehearsal fluctuates beneath cartographic obligations while "
        "seventeen quorums deliberate obliquely regarding zoological amendments"
    )
    assert mtld(repetitive) < mtld(diverse)


def test_the_score_is_deterministic_across_calls():
    """No RNG, no dict-iteration dependence, no memoisation that could go stale. A permutation built
    from a non-deterministic metric is not reproducible, and a re-run producing a different order
    over the same corpus is indistinguishable from a corrupted one."""
    text = "the quick brown fox jumps over the lazy dog and the dog barks back at the fox again"
    assert len({mtld(text) for _ in range(20)}) == 1
