"""Bidirectional MTLD (McCarthy & Jarvis 2010) — the curriculum difficulty metric, inline.

MTLD (Measure of Textual Lexical Diversity) is the per-document score the
``curriculum/edu-mix-983b`` ordering sorts on. It is computed HERE, at tokenize time, from the
text that is already in memory, and only the scalar survives.

WHY INLINE AND NOT A SECOND PASS
--------------------------------
The RegMix handoff labels text in a separate pass, which requires the text to be persisted. This
corpus ships no text: ``corpus_build`` reads documents from HF, tokenizes them in memory, and
writes only packed ``uint32`` shards (``corpus_pack.py``'s "the EOS is the only document boundary
this corpus ships"). Re-persisting it is 4.13 TB against 14.5 GB of labels, and re-reading HF later
cannot reproduce the document order without re-running the same build — which is exactly what
invalidates a permutation. So the only place MTLD can be computed is the place the text exists.

⚠️ THE CONSTANTS BELOW ARE A COMPATIBILITY SURFACE, NOT A TUNING KNOB
---------------------------------------------------------------------
Two training arms are comparable only if their difficulty labels came from the same function. A
"better" word regex or a "more principled" threshold silently makes this corpus's curriculum
incomparable to every other RegMix-labelled corpus, and NOTHING downstream can see it: the scores
are still floats in a plausible range, the permutation is still bijective, and Gate A still passes.
That is the same shape as the ``.npy`` lie and the wrong-``text_column`` trap — real-looking data
that is semantically wrong. So :data:`TTR_THRESHOLD` and :data:`WORD_RE` are frozen, and a change
to either is a new metric under a new name, not an edit here.

THE ALGORITHM, STATED SO IT CAN BE CHECKED BY HAND
--------------------------------------------------
One direction (McCarthy & Jarvis 2010, §"MTLD"):

1. Walk the word sequence, accumulating types and tokens.
2. Whenever the running type-token ratio falls to or below :data:`TTR_THRESHOLD`, close a factor
   (``factors += 1``) and reset both accumulators.
3. Any words left at the end form a PARTIAL factor worth ``(1 - final_ttr) / (1 - threshold)``.
4. The score is ``len(words) / factors``.

The bidirectional score is ``0.5 * (forward + backward)``, where backward runs the same walk over
the reversed sequence. Averaging the two removes the direction dependence of where the resets land.

Higher MTLD = more lexically diverse = HARDER. ``METRIC_SORT["mtld"] = ("mtld", False)`` therefore
sorts ASCENDING, so rank 0 is the easiest document.
"""

from __future__ import annotations

import re

__all__ = [
    "MTLD_SPEC_ID",
    "TTR_THRESHOLD",
    "WORD_RE",
    "SHORT_DOC_WORDS",
    "mtld",
    "mtld_one_direction",
    "words_of",
]

#: A name for the exact triple (regex, threshold, short-doc rule) below. It goes in the labels
#: sidecar header and in the curriculum group's metadata, so a consumer can tell whether two
#: corpora's scores are comparable WITHOUT re-reading this file. A version string is not a code
#: identity (``CLAUDE.md``), but a spec id next to the numbers it names is at least a claim that
#: can be contradicted — where an unlabelled float cannot be.
MTLD_SPEC_ID = "mtld-bidirectional-mccarthy-jarvis-2010/ttr0.72"

#: The TTR value at or below which a factor closes. **0.72 is from the paper and is not a
#: hyperparameter.** McCarthy & Jarvis derived it as the point where TTR curves stabilise; changing
#: it rescales every score non-linearly, so a corpus labelled at 0.72 and one at 0.71 cannot be
#: pooled or compared.
TTR_THRESHOLD = 0.72

#: The word tokenizer, verbatim. Three alternations, in this order:
#:
#: * ``[A-Za-z]+(?:'[A-Za-z]+)?`` — an alphabetic word, optionally with ONE internal apostrophe
#:   group, so ``don't`` is one type rather than ``don`` + ``t``. Contractions are a real fraction
#:   of web prose and splitting them inflates diversity.
#: * ``[A-Za-z]*\d+[A-Za-z0-9]*`` — any token containing a digit (``3d``, ``x86``, ``2026``),
#:   captured as one type. Code and technical prose are ~15% of this corpus.
#:
#: ⚠️ **ASCII-only by construction.** A document with no ASCII letters or digits yields ZERO words
#: (CJK, Cyrillic, pure symbols). That is a real outcome, not a bug, and :func:`mtld` returns
#: ``0.0`` for it rather than raising or returning NaN — see the short-document rule.
WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?|[A-Za-z]*\d+[A-Za-z0-9]*")

#: Below this many words a document gets ``len(set(words))`` instead of the ratio.
#:
#: **The purpose is finiteness, not accuracy.** MTLD's factor arithmetic is unstable on very short
#: sequences: a 3-word document of distinct words closes no factor and leaves a partial factor of
#: ``(1 - 1.0) / (1 - 0.72) == 0``, i.e. a division by zero. Returning the type count instead is
#: bounded, monotone in diversity, and on the same scale as MTLD at these lengths. It is also what
#: keeps ``np.isfinite(mtld)`` true for every document, which the ranking depends on: a NaN sorts
#: unpredictably and an ``inf`` would take rank 0 or rank N-1 by accident of comparison order.
SHORT_DOC_WORDS = 10


def words_of(text: str) -> list[str]:
    """The lowercased word sequence :func:`mtld` scores.

    Lowercasing happens BEFORE type counting, so ``The`` and ``the`` are one type. That is not
    cosmetic: capitalised sentence openers would otherwise inflate the type count roughly in
    proportion to sentence count, making MTLD partly a measure of punctuation density.

    ``str.lower()`` is applied to the whole document once rather than per match, which is both
    faster and the only order that makes the regex's behaviour independent of case (``[A-Za-z]``
    matches either case, so the matches are the same set either way — but the TYPES are not).
    """
    return WORD_RE.findall(text.lower())


def mtld_one_direction(words: list[str] | tuple[str, ...]) -> float:
    """MTLD over ``words`` in the order given. The caller supplies direction.

    Split out from :func:`mtld` so a test can pin ONE direction against a hand-computed value.
    Testing only the bidirectional average would let two compensating errors pass — a forward pass
    that closes a factor one word early and a backward pass that closes one late average back to
    the right answer on a symmetric input, which is exactly the input a test author reaches for.

    Returns ``float(len(words))`` when no factor closes and the partial factor is zero — the
    all-distinct case. ⚠️ **This is a DECISION, not a value from the paper**, which leaves the
    degenerate case undefined; see the module docstring's finiteness argument and
    ``artifacts/orchestration/curriculum/status.md`` for the flag. It is the natural limit (with
    maximal diversity, MTLD tends to the sequence length) and it is finite, which the ranking
    requires.
    """
    n = len(words)
    if n == 0:
        return 0.0

    factors = 0.0
    types: set[str] = set()
    seen = 0
    for w in words:
        seen += 1
        types.add(w)
        # `<=`, not `<`. The paper closes a factor when TTR *reaches* the threshold, and the
        # difference is not cosmetic: at exactly 0.72 the two spellings disagree about where every
        # factor boundary lands, which shifts the score on every document long enough to close one.
        if len(types) / seen <= TTR_THRESHOLD:
            factors += 1.0
            types = set()
            seen = 0
    if seen > 0:
        # The PARTIAL factor. Its weight is how far the remainder got toward the threshold,
        # normalised by the full distance — 1.0 if it reached the threshold (unreachable here,
        # since the loop would have closed it) and 0.0 if it is still all-distinct.
        factors += (1.0 - len(types) / seen) / (1.0 - TTR_THRESHOLD)

    if factors <= 0.0:
        return float(n)
    return n / factors


def mtld(text: str) -> float:
    """The bidirectional MTLD of one document. **Always finite.**

    ``0.5 * (forward + backward)``, per the spec. Below :data:`SHORT_DOC_WORDS` words the score is
    ``len(set(words))`` instead — see that constant for why the substitution exists and why it is
    about finiteness rather than about MTLD being wrong at short lengths.

    Returns a Python ``float``. The labels sidecar stores it as ``float32``, which is a deliberate
    narrowing: MTLD's own reproducibility across implementations is nowhere near 7 significant
    digits, the value is used only to RANK, and float32 halves the sidecar. The narrowing is
    applied at write time, not here, so a caller doing arithmetic gets full precision.
    """
    words = words_of(text)
    if len(words) < SHORT_DOC_WORDS:
        # `len(set(...))` — the number of distinct types. Bounded by SHORT_DOC_WORDS, so it can
        # never collide with a real MTLD score's range in a way that matters for ranking (short
        # documents rank as easy, which is what they are).
        return float(len(set(words)))
    forward = mtld_one_direction(words)
    backward = mtld_one_direction(words[::-1])
    return 0.5 * (forward + backward)
