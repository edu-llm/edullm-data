"""The two MTLD label defects, fixed — and the within-source stratification that replaces a
global sort.

This module is the repair layer between ``corpus_labels`` (which WROTE the labels) and
``corpus_order`` (which turns document difficulty into a chunk permutation). It changes no
published byte: the ``_labels/`` objects are the immutable input, and everything here is a
DERIVED transform applied in memory on the way into :func:`corpus_order.build_order`.

Both defects were found by reading 11.56 GB of real labels, and both are recorded in the
corpus mirror's own ``NOTE.txt``. Neither is detectable downstream: a curriculum built on
unrepaired labels is bijective, passes ``check_order_domain``, and trains the wrong schedule.

DEFECT 1 — THE RUNAWAY UPPER TAIL IS DOCUMENT LENGTH, NOT DIVERSITY
-------------------------------------------------------------------
``corpus_mtld.mtld_one_direction`` returns ``float(n)`` — the WORD COUNT — when no factor
closes and the partial factor is zero (``corpus_mtld.py:146-147``). Its own docstring flags
this as "a DECISION, not a value from the paper". For an all-distinct document the returned
number is a length in words, on a scale that has nothing to do with the ~83 median of a real
MTLD score.

MEASURED over all 825,415,196 train documents (see ``CURRICULUM-STATS.json``): the maximum is
315,176,256, which is 3.8 million times the median. The corpus's ``std`` is 38,021.8 while its
ROBUST std (over ``mtld <= 1000``, 99.993% of documents) is 40.92 — a 929x inflation caused
entirely by a tail of 0.0066% of documents.

**The fix is a CLAMP, not a drop, and the reason is structural rather than statistical.**
:func:`corpus_order.build_order` maps every chunk to the document owning its first token, and
a permutation must cover every chunk in the parent pool EXACTLY once
(``profiles/token_order_v1.py:209``). Dropping a document does not remove its tokens: those
tokens are already packed into shards, so its chunks still exist in the axis and still need a
rank. A "drop" would therefore mean either

* leaving those chunks unranked — which ``build_order`` refuses outright, because a chunk with
  no owning labelled document is a curriculum over unlabelled data; or
* assigning them a sentinel rank — which is a clamp with an undeclared threshold.

So the only choices are "clamp" and "clamp while pretending otherwise". Clamping is declared.

**The threshold is p99.9 of the train distribution, and it is a MEASURED quantity, not a
round number.** Above it the values are lengths; below it they are scores. The clamp is
order-preserving on everything below the threshold, so it changes NO pairwise comparison among
99.9% of documents — it only collapses the tail's mutual ordering, which was meaningless
anyway (it ranked those documents by word count).

⚠️ **A clamp creates TIES, and ties are why the secondary sort key is load-bearing.** Every
clamped document shares one score, so their relative order is decided entirely by
``corpus_order``'s ``global_chunk_idx`` tie-break — which is a total order, so the result stays
deterministic. See :func:`clamp_mtld`'s ``ties_created`` return field.

DEFECT 2 — A GLOBAL ASCENDING SORT IS A DOMAIN SORT
---------------------------------------------------
MTLD tracks REGISTER, not difficulty. MEASURED per-source means span 5.5x:
``stackv2-edu`` 39.78 -> ``dclm`` 108.76 -> ``cosmopedia`` 217.85. A global ascending sort
therefore opens the curriculum on almost pure code: MEASURED, ``stackv2-edu`` is **96.25% of
the easiest 1%** of documents and **94.43% of the easiest 5%**, against a corpus share of
7.39%.

🔴 **Why that is a training defect and not a cosmetic one.** Domain-pure micro-batches SUPPRESS
MoE expert specialization — measured at 0.13-0.18 PPL and +5-6 GSM8K (arXiv 2501.11873). The
target here is a 96-expert MoE, and an ascending global sort manufactures exactly the pathology
for the first ~44 B tokens.

**The fix is to rank WITHIN each source and order globally by that percentile.** A document's
difficulty becomes "how hard is this FOR ITS DOMAIN", so every training window draws from all
sources in proportion to their size, and the head is easy-for-its-domain rather than all code.
See :func:`stratify_within_source`, whose verification is that no source's share of the easiest
1% / 5% materially exceeds its share of the corpus.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
It does not RE-SCORE anything. The right long-term fix to defect 1 is for
``mtld_one_direction`` to return a defined value in the degenerate case rather than a length —
but that would change ``MTLD_SPEC_ID``, i.e. make this corpus's labels incomparable to every
other corpus labelled under ``mtld-bidirectional-mccarthy-jarvis-2010/ttr0.72``, and it would
require re-reading 4.13 TB of text that this corpus does not ship. The clamp is a repair
applied to the scores that exist, and it is declared in the artifact so a consumer can tell
it happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

import numpy as np

from .corpus import BuildError

__all__ = [
    "CLAMP_PERCENTILE",
    "STRATIFY_METHOD",
    "ClampResult",
    "clamp_mtld",
    "percentile_of_sorted_counts",
    "stratify_within_source",
    "source_composition",
    "moments",
]

#: The clamp threshold's percentile. **p99.9, measured on the train split's own distribution.**
#:
#: Not a round cutoff like 1,000: the boundary between "a score" and "a word count" is a
#: property of this corpus, so it is READ from the data rather than asserted. At p99.9 the
#: clamp touches 0.1% of documents by construction, which is ~15x the 0.0066% that exceed
#: 1,000 — deliberately conservative, because a value just under 1,000 is no more a real MTLD
#: score than one just over it, and clamping a few extra high-but-plausible scores costs only
#: their mutual ordering while leaving 99.9% of pairwise comparisons untouched.
CLAMP_PERCENTILE = 99.9

#: Named in the artifact so a consumer can tell WHICH stratification produced the vector. A
#: differently-stratified order is a valid permutation of the same length, so the method has to
#: be declared or the difference is undetectable — the same argument as ``COORDINATE_MODEL``.
STRATIFY_METHOD = "within_source_percentile_rank_v1"


@dataclass(frozen=True)
class ClampResult:
    """The outcome of :func:`clamp_mtld` — the repaired scores plus everything a report needs.

    ``scores`` is float64 even though the input is float32. The clamp itself does not need the
    width, but the percentile RANKING in :func:`stratify_within_source` divides by a document
    count, and doing that in float32 would collapse distinct ranks in a 60-million-document
    source into the same float — reintroducing ties that the secondary key would then have to
    resolve, i.e. silently replacing "hardest-for-its-domain" with "latest chunk index".
    """

    scores: np.ndarray
    threshold: float
    n_clamped: int
    n_total: int
    #: How many documents now SHARE the threshold value, including any that already sat exactly
    #: on it. Reported because a clamp's whole cost is the ordering it destroys, and that cost
    #: is this number — not ``n_clamped``.
    ties_created: int
    before: Mapping[str, float]
    after: Mapping[str, float]

    @property
    def fraction_clamped(self) -> float:
        return self.n_clamped / self.n_total if self.n_total else 0.0


def moments(x: np.ndarray, *, robust_below: float = 1000.0) -> dict[str, float]:
    """Mean/std/min/max plus a ROBUST std, computed in float64.

    The robust std — over ``x <= robust_below`` — is reported alongside the real one because on
    this corpus they differ by 929x, and quoting only the real one makes the distribution look
    like it has no usable scale. Quoting only the robust one hides the defect. Both, always.
    """
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        raise BuildError("moments() on an empty array — there is nothing to summarise")
    sub = x[x <= robust_below]
    return {
        "n": int(x.size),
        "mean": float(x.mean()),
        "std": float(x.std()),
        "min": float(x.min()),
        "max": float(x.max()),
        "robust_n": int(sub.size),
        "robust_mean": float(sub.mean()) if sub.size else float("nan"),
        "robust_std": float(sub.std()) if sub.size else float("nan"),
    }


def percentile_of_sorted_counts(
    values: np.ndarray, counts: np.ndarray, pct: float
) -> float:
    """The ``pct``-th percentile of a distribution given as (sorted distinct values, counts).

    Exists so the threshold can be computed over 825 M documents without holding them all, and
    so the same arithmetic is used on the full corpus and in a test. ``side="left"`` on the
    cumulative counts picks the first value whose cumulative count REACHES the target rank,
    which is the conventional nearest-rank definition.
    """
    if values.size != counts.size:
        raise BuildError(
            f"{values.size} values against {counts.size} counts — the two arrays describe one "
            f"histogram and must be parallel"
        )
    if values.size == 0:
        raise BuildError("percentile of an empty histogram")
    if not 0.0 <= pct <= 100.0:
        raise BuildError(f"percentile must be in [0, 100]; got {pct}")
    if not bool(np.all(np.diff(values) > 0)):
        raise BuildError(
            "histogram values are not strictly ascending, so a cumulative count does not "
            "correspond to a rank in the distribution"
        )
    cum = np.cumsum(counts, dtype=np.int64)
    total = int(cum[-1])
    target = pct / 100.0 * total
    i = int(np.searchsorted(cum, target, side="left"))
    return float(values[min(i, values.size - 1)])


def clamp_mtld(
    scores: np.ndarray, threshold: float, *, robust_below: float = 1000.0
) -> ClampResult:
    """Clamp ``scores`` at ``threshold``. Defect 1's fix.

    ``np.minimum``, elementwise — so the transform is monotone non-decreasing and every pairwise
    comparison BELOW the threshold is preserved exactly. That is the property that makes a clamp
    safe here: the curriculum's claim is about relative order, and the clamp changes the order of
    nothing except the tail whose order was a word-count artifact.

    Refuses a non-finite input rather than clamping around it. ``np.minimum(nan, t)`` is ``nan``,
    so a NaN would survive the clamp untouched and then land at an arbitrary rank —
    ``build_order`` checks for that too, but failing here names the labels file instead of the
    axis. MEASURED on this corpus: 0 non-finite of 825,415,196, so this is a guard rather than a
    live path.
    """
    x = np.asarray(scores)
    if x.ndim != 1:
        raise BuildError(f"scores must be 1-D; got shape {x.shape}")
    if x.size == 0:
        raise BuildError("no scores to clamp")
    if not np.isfinite(threshold) or threshold <= 0:
        raise BuildError(
            f"clamp threshold must be finite and positive; got {threshold!r}. A non-positive "
            f"threshold collapses every score to one value, producing a curriculum whose order "
            f"is entirely the tie-break — bijective, and meaningless."
        )
    x64 = x.astype(np.float64, copy=True)
    n_bad = int(np.count_nonzero(~np.isfinite(x64)))
    if n_bad:
        raise BuildError(
            f"{n_bad:,} of {x64.size:,} scores are NaN or inf. np.minimum(nan, t) is nan, so a "
            f"clamp would leave them in place to take an arbitrary rank. Fix the labels."
        )
    before = moments(x64, robust_below=robust_below)
    n_clamped = int(np.count_nonzero(x64 > threshold))
    out = np.minimum(x64, threshold)
    ties = int(np.count_nonzero(out == threshold))
    after = moments(out, robust_below=robust_below)
    return ClampResult(
        scores=out,
        threshold=float(threshold),
        n_clamped=n_clamped,
        n_total=int(x64.size),
        ties_created=ties,
        before=before,
        after=after,
    )


def stratify_within_source(
    scores: np.ndarray, source_id: np.ndarray, *, n_sources: int | None = None
) -> np.ndarray:
    """Replace each document's score with its PERCENTILE RANK WITHIN ITS OWN SOURCE. Defect 2.

    Returns a float64 array in ``[0, 1)``: ``rank_within_source / documents_in_source``. Sorting
    ascending on this puts the easiest-for-its-domain documents of EVERY source at the head, in
    proportion to each source's size — which is the fix. A global sort on the raw score puts the
    lowest-REGISTER source at the head instead.

    **Why a percentile and not a z-score.** A z-score assumes the per-source distributions are
    comparable in shape after centring and scaling, and MEASURED they are not: ``stackv2-edu``
    has median 20.61 against mean 39.78 (strongly right-skewed) while ``cosmopedia`` has median
    205.10 against mean 217.85 (near-symmetric). A percentile rank is invariant to the shape of
    each source's distribution — it only uses the within-source ORDER, which is the only thing
    MTLD is trusted for here.

    **Ties within a source are broken by document index**, via ``kind="stable"`` on the argsort,
    so a source's clamped tail (all sharing one score) gets distinct consecutive percentiles in
    document order rather than one shared value. That keeps the returned array a near-total
    order and leaves ``corpus_order``'s ``global_chunk_idx`` tie-break as the final arbiter only
    for cross-source collisions.

    ⚠️ **The denominator is per source, so a source with ONE document gets percentile 0.0** —
    i.e. it lands at the very head. That is arithmetically correct and substantively odd, so it
    is REPORTED rather than special-cased: see the ``tiny_sources`` field of
    :func:`source_composition`. MEASURED on this corpus the smallest train source is
    ``math-textbooks`` at 1,501,381 documents, so no such source exists here.
    """
    s = np.asarray(scores, dtype=np.float64)
    sid = np.asarray(source_id)
    if s.shape != sid.shape:
        raise BuildError(
            f"{s.size} scores against {sid.size} source ids — index i of one must be the same "
            f"document as index i of the other"
        )
    if s.size == 0:
        raise BuildError("nothing to stratify")
    if not np.issubdtype(sid.dtype, np.integer):
        raise BuildError(f"source_id must be an integer array; got dtype {sid.dtype}")
    if int(sid.min()) < 0:
        raise BuildError("source_id must be non-negative")
    k = int(sid.max()) + 1 if n_sources is None else int(n_sources)
    if k <= 0:
        raise BuildError(f"n_sources must be positive; got {k}")
    if int(sid.max()) >= k:
        raise BuildError(
            f"source_id {int(sid.max())} is out of range for n_sources={k}; a document assigned "
            f"to a source that does not exist would be ranked against an empty denominator"
        )
    sizes = np.bincount(sid, minlength=k).astype(np.int64)
    if int(sizes.sum()) != s.size:
        raise BuildError("source sizes do not sum to the document count")

    # A SINGLE global lexsort by (score, source) groups each source contiguously and orders it
    # internally — one O(n log n) pass instead of k passes, which at 825 M documents and 130
    # sources is the difference between minutes and hours. `lexsort` takes keys last-is-primary.
    order = np.lexsort((np.arange(s.size, dtype=np.int64), s, sid))
    # Position of each document within its own source's block.
    starts = np.zeros(k + 1, dtype=np.int64)
    np.cumsum(sizes, out=starts[1:])
    within = np.arange(s.size, dtype=np.int64) - starts[sid[order]]
    out = np.empty(s.size, dtype=np.float64)
    out[order] = within / sizes[sid[order]]
    return out


def source_composition(
    ranked_scores: np.ndarray,
    source_id: np.ndarray,
    source_names: Sequence[str],
    *,
    fractions: Iterable[float] = (0.01, 0.05),
    weights: np.ndarray | None = None,
) -> dict[str, dict]:
    """Which sources make up the easiest ``fraction`` of the ordering. Defect 2's VERIFICATION.

    ``weights`` (token counts, typically) makes the head a fraction of TOKENS rather than of
    documents. Both are reported by the caller because they answer different questions: a
    document-weighted head describes the label distribution, and a token-weighted one describes
    what the model actually reads first — and on this corpus they differ, since mean document
    length varies ~7x across sources.

    The verdict a caller checks is ``share_of_head`` against ``share_of_corpus``: if a single
    source's share of the easiest 1% far exceeds its share of the corpus, the stratification did
    NOT work and the vector should not ship.
    """
    s = np.asarray(ranked_scores, dtype=np.float64)
    sid = np.asarray(source_id)
    if s.shape != sid.shape:
        raise BuildError("scores and source ids must be parallel")
    w = (
        np.ones(s.size, dtype=np.float64)
        if weights is None
        else np.asarray(weights, dtype=np.float64)
    )
    if w.shape != s.shape:
        raise BuildError("weights must be parallel to scores")
    k = len(source_names)
    order = np.lexsort((np.arange(s.size, dtype=np.int64), s))
    sid_sorted = sid[order]
    w_sorted = w[order]
    cw = np.cumsum(w_sorted)
    total_w = float(cw[-1])
    corpus_w = np.bincount(sid, weights=w, minlength=k)
    out: dict[str, dict] = {}
    for frac in fractions:
        cut = int(np.searchsorted(cw, frac * total_w, side="left")) + 1
        cut = min(cut, s.size)
        head_w = np.bincount(sid_sorted[:cut], weights=w_sorted[:cut], minlength=k)
        hw = float(head_w.sum())
        rows = []
        for i in range(k):
            if head_w[i] <= 0:
                continue
            rows.append(
                {
                    "source": source_names[i],
                    "head_weight": float(head_w[i]),
                    "share_of_head": float(head_w[i] / hw) if hw else 0.0,
                    "share_of_corpus": float(corpus_w[i] / total_w) if total_w else 0.0,
                    "over_representation": (
                        float((head_w[i] / hw) / (corpus_w[i] / total_w))
                        if hw and corpus_w[i]
                        else float("inf")
                    ),
                }
            )
        rows.sort(key=lambda r: -r["share_of_head"])
        out[f"easiest_{frac * 100:g}pct"] = {
            "head_items": cut,
            "head_weight": hw,
            "sources_present": len(rows),
            "max_over_representation": max((r["over_representation"] for r in rows), default=0.0),
            "composition": rows,
        }
    return out
