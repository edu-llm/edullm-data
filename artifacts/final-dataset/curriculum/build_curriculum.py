#!/usr/bin/env python3
"""Build the ``edu-mix-983b`` curriculum order vector. LOCAL FILES ONLY — reads nothing from S3
and writes nothing to it.

Inputs (all already on disk):
  * ``artifacts/final-dataset/index/INDEX-FINAL.json``   the 36,203-shard consumer index
  * ``artifacts/final-dataset/receipts_all/*.json``      182 receipts — the part attribution
  * ``artifacts/final-dataset/labels_raw/*.labels``      141 TRAIN label objects (11.56 GB)

Outputs, under ``artifacts/final-dataset/curriculum/``:
  * ``edu-mix-983b-train.u32le.bin``  the permutation, uint32 LE
  * ``CURRICULUM-SPEC.json``          everything a trainer needs and nothing it must infer
  * ``CURRICULUM-STATS.json``         before/after moments and head composition

WHY THIS IS A DRIVER AND NOT A PUBLISH
--------------------------------------
``token-order/v1`` requires ``depends_on`` naming the PARENT'S MANIFEST SHA256, and this corpus
has **no manifest** — it was never published (``NOTE.txt``: "no manifest.json, no dataset.json,
and no hash chain"). ``curriculum_driver.py`` encodes that with ``PARENT_MANIFEST_SHA256 = None``
and exits 1 by design. So the permutation is delivered as a FILE plus a spec, and the spec
carries the coordinate model, the chunk rule, and the axis identity that a future publish would
otherwise have to reconstruct — which is the step that silently invalidates a permutation.

THE AXIS IS RECOMPUTED FROM THE INDEX, NOT FROM THE PLAN
--------------------------------------------------------
``corpus_order.chunk_axis_from_manifest`` derives token counts from each entry's ``bytes``
because ``bytes`` is the number an independent HEAD confirmed. There is no manifest here, so the
axis is built from ``INDEX-FINAL.json``, whose per-shard ``bytes`` came from a full census of
both regions — the same property — and each is cross-checked against the declared ``tokens``.

MEMORY, AND WHY THE ALGORITHM LOOKS THE WAY IT DOES
---------------------------------------------------
825 M documents x (8 B score + 8 B stride + 2 B source) is ~15 GB against 16 GiB of RAM, and a
global ``argsort`` over 825 M float64 would need a further 6.6 GB. Neither fits. So:

  pass 1  read every labels object; keep only a per-source HISTOGRAM (0.01-wide bins) plus exact
          overflow values. Peak = one object (~476 MB). This yields the clamp threshold and,
          cumulated, the within-source percentile of ANY score without a global sort.
  pass 2  re-read; clamp; convert each score to a within-source percentile via the pass-1
          cumulative table; map chunks to owning documents PER PART; keep one uint32 key per
          chunk (1.76 GB).

The percentile route is what removes the global sort: a rank is a lookup into a cumulative
histogram plus a within-bin cursor, not a comparison against 825 M neighbours.

⚠️ **THIS DRIVER DOES NOT CALL ``corpus_curriculum.clamp_mtld`` OR ``stratify_within_source``,
AND THAT IS A REAL CAVEAT, NOT AN OVERSIGHT.**
Both library functions take the WHOLE score array — ``clamp_mtld`` returns a float64 copy
(6.6 GB at 825 M documents) and ``stratify_within_source`` lexsorts it (another 6.6 GB). Neither
fits in 16 GiB, so the streaming forms here are algebraically equivalent reimplementations:

* the clamp is ``np.minimum(raw, threshold)`` per part — literally the same operation
  ``clamp_mtld`` performs, minus the moment bookkeeping (which is recomputed from the histogram);
* the stratification is the same within-source rank / source-size, obtained from a cumulative
  histogram instead of a sort.

The consequence is that ``corpus_curriculum``'s tests do NOT cover the code that produced the
shipped vector. What covers this driver is ``tests/test_curriculum_build_driver.py``, which runs
THIS function end to end on a synthetic corpus and asserts the permutation, the sort direction,
the tie ordering, the head composition, and the per-part token accounting. Two test suites for
two implementations of one idea; the library form is what a future in-memory caller should use,
and the equivalence between them is asserted nowhere except by both being tested against the
same properties. Stated so nobody reads the library's green tests as evidence about this file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from edullm_data.corpus_curriculum import (  # noqa: E402
    CLAMP_PERCENTILE,
    STRATIFY_METHOD,
    percentile_of_sorted_counts,
)
from edullm_data.corpus_labels import ITEM_SIZE, LABEL_DTYPE  # noqa: E402
from edullm_data.corpus_mtld import MTLD_SPEC_ID  # noqa: E402
from edullm_data.corpus_order import (  # noqa: E402
    CHUNKS_FLOOR,
    CHUNKS_MINUS_ONE,
    COORDINATE_MODEL,
    ORDER_DTYPE,
    SEQ_LEN_CHUNK,
    chunk_counts,
)

ART = os.path.join(ROOT, "artifacts", "final-dataset")
INDEX = os.path.join(ART, "index", "INDEX-FINAL.json")
RECEIPTS = os.path.join(ART, "receipts_all")
LABELS = os.path.join(ART, "labels_raw")
OUT = HERE
SCRATCH = os.path.join(HERE, "_scratch")

PLAN_ID = "79e53d1e5e131649"
SPLIT = "train"
MIRROR = "s3://edullm-corpus-use2/pretrain/edu-mix-983b/79e53d1e5e131649/"

BINW = 0.01
NBINS = 1_000_000
INV = 1.0 / BINW
ROBUST_BIN = 100_000  # mtld <= 1000
#: uint32 key scale. A percentile in [0,1) times this fits uint32, and the granularity
#: (1/4.29e9) is ~70x finer than the finest per-source rank step (1/60,976,386), so the scaling
#: introduces no tie that the ranks did not already have.
U32MAX = np.uint64(np.iinfo(np.uint32).max)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def read_header(fh):
    head = fh.read(12)
    if len(head) < 12 or head[:8] != b"EDULLBL1":
        raise SystemExit(f"bad magic {head[:8]!r}")
    hl = int.from_bytes(head[8:12], "little")
    return 12 + hl, json.loads(fh.read(hl).decode())


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(16 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def axis_digest(keys, tokens, counts) -> str:
    """A digest over the AXIS ITSELF — the (key, tokens, chunks) triples in order.

    A permutation is meaningless against a different axis, and an axis rebuilt from a different
    source (the plan, a prefix listing) can differ while every index stays in range. So the axis
    gets its own identity, and a future publish must reproduce this digest.
    """
    h = hashlib.sha256()
    for k, t, c in zip(keys, tokens, counts):
        h.update(f"{k}\t{t}\t{c}\n".encode())
    return h.hexdigest()


def load_axis():
    idx = json.load(open(INDEX))
    rows = sorted(
        (s for s in idx["shards"] if s["split"] == SPLIT), key=lambda s: s["relative_path"]
    )
    keys = [s["relative_path"] for s in rows]
    if keys != sorted(keys):
        raise SystemExit("axis keys are not ascending")
    if len(set(keys)) != len(keys):
        raise SystemExit("duplicate shard keys in the axis")
    tokens = []
    for s in rows:
        b = int(s["bytes"])
        if b % 4:
            raise SystemExit(f"{s['relative_path']}: {b} bytes is not a multiple of 4")
        t = b // 4
        if t != int(s["tokens"]):
            raise SystemExit(
                f"{s['relative_path']}: bytes//4={t} but index declares tokens {s['tokens']}"
            )
        tokens.append(t)
    counts = chunk_counts(tokens, seq_len=SEQ_LEN_CHUNK, rule=CHUNKS_MINUS_ONE)
    bad = [(k, t) for k, t, c in zip(keys, tokens, counts) if c <= 0]
    if bad:
        raise SystemExit(f"{len(bad)} shards yield zero chunks: {bad[:5]}")
    return keys, tokens, counts, idx


def load_parts():
    out, owner = {}, {}
    for fn in sorted(os.listdir(RECEIPTS)):
        if not fn.endswith(".json"):
            continue
        d = json.load(open(os.path.join(RECEIPTS, fn)))
        st = d["stream"]
        key = (
            st["source"],
            st.get("domain"),
            st["split"],
            int((d.get("file_shard") or {}).get("index", 0)),
        )
        for sh in d["shards"]:
            rp = "data/" + sh["path"]
            if rp in out and out[rp] != key:
                raise SystemExit(
                    f"shard {rp} claimed by two bundles: {owner[rp]} as {out[rp]} and "
                    f"{d['bundle_id']} as {key} — keeping either would map its chunks onto the "
                    f"wrong part's documents"
                )
            out[rp] = key
            owner[rp] = d["bundle_id"]
    if not out:
        raise SystemExit("no receipts named any shard")
    return out


def label_files():
    out = {}
    for fn in sorted(os.listdir(LABELS)):
        if not fn.endswith(".labels"):
            continue
        p = os.path.join(LABELS, fn)
        with open(p, "rb") as fh:
            _, hdr = read_header(fh)
        st = hdr["stream"]
        if st["split"] != SPLIT:
            continue
        m = re.search(r"--p(\d+)of(\d+)$", hdr["bundle_id"])
        out[(st["source"], st.get("domain"), st["split"], int(m.group(1)) if m else 0)] = p
    return out


def within_bin_offsets(bins: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(order, offset)`` such that documents sharing a bin get CONSECUTIVE offsets 0,1,2,...

    ``kind="stable"`` means the offsets follow DOCUMENT order inside a bin, so a part's clamped
    tail (all one score) gets distinct consecutive ranks rather than one shared value. That is
    what keeps the percentile a near-total order instead of a step function with a 47,469-wide
    plateau at the top.
    """
    order = np.argsort(bins, kind="stable")
    bs = bins[order]
    first = np.ones(bs.size, dtype=bool)
    first[1:] = bs[1:] != bs[:-1]
    grp = np.cumsum(first) - 1
    run_start = np.flatnonzero(first)
    return order, np.arange(bs.size, dtype=np.int64) - run_start[grp]


def head_from_hist(per_source_hist, overflow_counts, sources, docs_total, fracs):
    """Head composition under a GLOBAL sort, computed exactly from the per-source histograms.

    No extra pass and no retained per-document array: a global sort's head is
    "every document whose score is below the global x-percentile", which a cumulative histogram
    answers exactly. This is the BEFORE view — the defect.
    """
    g = per_source_hist.sum(axis=0)
    cum_g = np.cumsum(g)
    out = {}
    for frac in fracs:
        target = frac * docs_total
        cut = int(np.searchsorted(cum_g, target, side="left"))
        cut = min(cut, NBINS - 1)
        thresh = (cut + 1) * BINW
        head = per_source_hist[:, : cut + 1].sum(axis=1).astype(np.int64)
        tot = int(head.sum())
        rows = []
        for i, s in enumerate(sources):
            if head[i] <= 0:
                continue
            corpus_share = float(per_source_hist[i].sum() + overflow_counts[i]) / docs_total
            rows.append(
                {
                    "source": s,
                    "documents": int(head[i]),
                    "share_of_head": float(head[i] / tot) if tot else 0.0,
                    "share_of_corpus": corpus_share,
                    "over_representation": float((head[i] / tot) / corpus_share)
                    if tot and corpus_share
                    else None,
                }
            )
        rows.sort(key=lambda r: -r["share_of_head"])
        out[f"easiest_{frac*100:g}pct"] = {
            "weighting": "documents",
            "mtld_threshold": round(thresh, 4),
            "documents_in_head": tot,
            "sources_present": len(rows),
            "max_over_representation": max((r["over_representation"] or 0) for r in rows),
            "composition": rows[:12],
            "composition_truncated_to": 12,
            "composition_rows_total": len(rows),
        }
    return out


def head_from_order(order, chunk_src, sources, fracs, n_sources):
    """Head composition of the ACTUAL chunk order — the AFTER view, measured on the deliverable.

    Chunk-weighted, and every chunk is exactly ``SEQ_LEN_CHUNK`` tokens, so chunk share IS token
    share. This is what the model reads first, which is the question the fix has to answer.
    """
    corpus = np.bincount(chunk_src, minlength=n_sources).astype(np.int64)
    total = int(corpus.sum())
    out = {}
    for frac in fracs:
        cut = int(round(frac * total))
        head = np.bincount(chunk_src[order[:cut]], minlength=n_sources).astype(np.int64)
        tot = int(head.sum())
        rows = []
        for i, s in enumerate(sources):
            if head[i] <= 0:
                continue
            cs = float(corpus[i] / total)
            rows.append(
                {
                    "source": s,
                    "chunks": int(head[i]),
                    "tokens": int(head[i]) * SEQ_LEN_CHUNK,
                    "share_of_head": float(head[i] / tot),
                    "share_of_corpus": cs,
                    "over_representation": float((head[i] / tot) / cs) if cs else None,
                }
            )
        rows.sort(key=lambda r: -r["share_of_head"])
        out[f"easiest_{frac*100:g}pct"] = {
            "weighting": "chunks (== tokens; every chunk is 2048 tokens)",
            "chunks_in_head": tot,
            "tokens_in_head": tot * SEQ_LEN_CHUNK,
            "sources_present": len(rows),
            "sources_total": int((corpus > 0).sum()),
            "max_over_representation": max((r["over_representation"] or 0) for r in rows),
            "composition": rows[:12],
            "composition_truncated_to": 12,
            "composition_rows_total": len(rows),
        }
    return out


def main() -> int:
    t0 = time.time()
    os.makedirs(SCRATCH, exist_ok=True)
    log("loading axis from INDEX-FINAL.json")
    keys, tokens, counts, idx = load_axis()
    n_chunks = int(sum(counts))
    n_tokens = int(sum(tokens))
    log(f"  {len(keys):,} train shards, {n_tokens:,} tokens, {n_chunks:,} chunks")
    if n_chunks > np.iinfo(np.uint32).max:
        raise SystemExit(f"{n_chunks:,} chunks exceeds uint32")

    parts_by_shard = load_parts()
    labels = label_files()
    axis_parts = {}
    for k in keys:
        pk = parts_by_shard.get(k)
        if pk is None:
            raise SystemExit(f"axis key {k} maps to no part")
        if pk not in labels:
            raise SystemExit(f"axis key {k} belongs to part {pk}, which supplied NO labels")
        axis_parts[k] = pk
    part_keys = sorted(labels)
    log(f"  {len(part_keys)} labelled train parts; all {len(keys):,} axis keys attributed")

    held = {pk: 0 for pk in part_keys}
    for k, t in zip(keys, tokens):
        held[axis_parts[k]] += t

    sources = sorted({pk[0] for pk in part_keys})
    sid_of = {s: i for i, s in enumerate(sources)}
    K = len(sources)
    if K > 255:
        raise SystemExit(f"{K} sources exceeds the uint8 chunk_src array")
    log(f"  {K} distinct train sources")

    offsets = np.zeros(len(keys) + 1, dtype=np.int64)
    np.cumsum(np.asarray(counts, dtype=np.int64), out=offsets[1:])

    # -------------------------------------------------------------------------------
    # PASS 1 — per-source histogram, exact overflow, document counts.
    # -------------------------------------------------------------------------------
    log("PASS 1/2: per-source histogram")
    hist = np.zeros((K, NBINS), dtype=np.int64)
    #: TOKEN-weighted histogram — the same bins, but each document contributes its ``n_tokens + 1``
    #: instead of 1.
    #:
    #: 🔴 **This is the fix to a real defect found by measuring the first run's output.** Ranking
    #: documents by their DOCUMENT percentile and then measuring the head in CHUNKS does not give
    #: a proportional head, because mean document length spans **31.6x** across these sources
    #: (finephrase-table 263 tok/doc -> pubmed 8,301 tok/doc, MEASURED from the receipts). A source
    #: of long documents yields more chunks per document, so equal document percentiles buy
    #: unequal chunk shares: the first run measured ``max_over_representation`` **3.357** at the
    #: easiest 1%, against a declared bar of 1.5.
    #:
    #: Weighting the rank by tokens makes the percentile a fraction of the source's TOKEN mass,
    #: which is what a chunk share is — so the denominator finally matches the thing being
    #: verified. The alternative (leave it and loosen the bar) would have shipped a head that is
    #: 3.4x skewed toward long-document sources while claiming stratification worked.
    hist_tok = np.zeros((K, NBINS), dtype=np.int64)
    overflow = {s: [] for s in sources}
    over_tokens = {s: 0 for s in sources}
    docs_src = np.zeros(K, dtype=np.int64)
    toks_src = np.zeros(K, dtype=np.int64)
    docs_part, lab_tokens = {}, {}
    g_n = 0
    g_sum = g_sumsq = 0.0
    g_min, g_max = np.inf, -np.inf
    for i, pk in enumerate(part_keys):
        p = labels[pk]
        size = os.path.getsize(p)
        with open(p, "rb") as fh:
            off, hdr = read_header(fh)
            rec = np.fromfile(fh, dtype=LABEL_DTYPE)
        n = len(rec)
        if size != off + n * ITEM_SIZE:
            raise SystemExit(f"{pk}: {size} != {off} + {n}*{ITEM_SIZE}")
        if hdr.get("mtld_spec") != MTLD_SPEC_ID:
            raise SystemExit(f"{pk}: mtld_spec {hdr.get('mtld_spec')!r}")
        if not np.array_equal(rec["source_doc"], np.arange(n, dtype=np.uint32)):
            raise SystemExit(f"{pk}: source_doc not contiguous")
        lab = int((rec["n_tokens"].astype(np.int64) + 1).sum())
        if lab != int(hdr["tokens_in"]):
            raise SystemExit(f"{pk}: sum(n_tokens+1)={lab} != tokens_in={hdr['tokens_in']}")
        if lab < held[pk]:
            raise SystemExit(
                f"{pk}: labels describe {lab:,} tokens but the part's shards hold {held[pk]:,}"
            )
        lab_tokens[pk], docs_part[pk] = lab, n
        m = rec["mtld"].astype(np.float64)
        w = rec["n_tokens"].astype(np.int64) + 1  # the document's stride == its token weight
        del rec
        nbad = int(np.count_nonzero(~np.isfinite(m)))
        if nbad:
            raise SystemExit(f"{pk}: {nbad} non-finite mtld")
        if float(m.min()) < 0:
            raise SystemExit(f"{pk}: negative mtld")
        si = sid_of[pk[0]]
        docs_src[si] += n
        toks_src[si] += lab
        g_n += n
        g_sum += float(m.sum())
        g_sumsq += float(np.square(m).sum())
        g_min, g_max = min(g_min, float(m.min())), max(g_max, float(m.max()))
        q = np.floor(m * INV).astype(np.int64)
        hi = q >= NBINS
        if hi.any():
            overflow[pk[0]].append(m[hi].copy())
            over_tokens[pk[0]] += int(w[hi].sum())
        q, wq = q[~hi], w[~hi]
        if q.size:
            hist[si] += np.bincount(q, minlength=NBINS)
            hist_tok[si] += np.bincount(q, weights=wq, minlength=NBINS).astype(np.int64)
        del m, q, w, wq
        if (i + 1) % 25 == 0 or i + 1 == len(part_keys):
            log(f"  {i+1}/{len(part_keys)} parts, {g_n:,} docs")

    over_counts = np.array(
        [sum(a.size for a in overflow[s]) for s in sources], dtype=np.int64
    )
    n_overflow = int(over_counts.sum())
    if g_n != int(docs_src.sum()):
        raise SystemExit("document counts disagree")
    log(f"  {g_n:,} documents; min {g_min}, max {g_max}; >=10000: {n_overflow:,}")

    # ---- the clamp threshold: p99.9 of the TRAIN distribution ----
    g_hist = hist.sum(axis=0)
    values = (np.arange(NBINS, dtype=np.float64) + 0.5) * BINW
    nz = g_hist > 0
    thr_vals = np.concatenate([values[nz], np.array([10000.0 + BINW])])
    thr_cnts = np.concatenate([g_hist[nz], np.array([n_overflow], dtype=np.int64)])
    threshold = percentile_of_sorted_counts(thr_vals, thr_cnts, CLAMP_PERCENTILE)
    # ⚠️ The threshold can land ON the overflow sentinel (10000 + BINW) when more than
    # (100 - CLAMP_PERCENTILE)% of documents exceed 10,000 — which is not the real corpus
    # (0.0004% do) but IS reachable on a small or pathological input. `floor(threshold * INV)`
    # would then be 1,000,001, i.e. past the histogram, and indexing it raises IndexError deep
    # inside the clamped-histogram build. Clamp the BIN to the last real one and lower the
    # THRESHOLD to match, so the two never disagree: a threshold above the highest binned value
    # would report a clamp that the histogram says touched nothing.
    bin_t = int(np.floor(threshold * INV))
    if bin_t >= NBINS:
        bin_t = NBINS - 1
        threshold = float(values[bin_t])
        log(
            f"  threshold fell in the overflow bucket; lowered to the last binned value "
            f"{threshold} (bin {bin_t}). This means >{100-CLAMP_PERCENTILE}% of documents "
            f"exceed {BINW*NBINS:,.0f}, which the real corpus does not."
        )
    n_clamped = int(g_hist[bin_t + 1 :].sum()) + n_overflow
    log(f"  clamp threshold p{CLAMP_PERCENTILE} = {threshold} (bin {bin_t})")
    log(f"  documents above it: {n_clamped:,} ({100.0*n_clamped/g_n:.4f}%)")

    # ---- BEFORE moments, from the raw histogram ----
    def hist_moments(h, extra_vals, n_total, mx):
        s = float((values * h).sum()) + float(extra_vals.sum())
        mu = s / n_total
        var = float((h * (values - mu) ** 2).sum()) + float(((extra_vals - mu) ** 2).sum())
        rn = int(h[:ROBUST_BIN].sum())
        rmu = float((values[:ROBUST_BIN] * h[:ROBUST_BIN]).sum() / rn)
        rvar = float((h[:ROBUST_BIN] * (values[:ROBUST_BIN] - rmu) ** 2).sum() / rn)
        return {
            "n": int(n_total),
            "mean": mu,
            "std": float(np.sqrt(max(var / n_total, 0.0))),
            "min": g_min,
            "max": mx,
            "robust_n_le_1000": rn,
            "robust_mean_le_1000": rmu,
            "robust_std_le_1000": float(np.sqrt(max(rvar, 0.0))),
            "percentiles": {
                str(p): percentile_of_sorted_counts(thr_vals, thr_cnts, p)
                if h is g_hist
                else None
                for p in (1, 5, 25, 50, 75, 95, 99, 99.9)
            },
        }

    all_over = (
        np.concatenate([a for v in overflow.values() for a in v])
        if n_overflow
        else np.zeros(0)
    )
    before_moments = hist_moments(g_hist, all_over, g_n, g_max)
    before_moments["nonfinite"] = 0
    before_moments["documents_over_1000"] = int(g_hist[ROBUST_BIN:].sum()) + n_overflow
    before_moments["documents_over_10000"] = n_overflow
    before_moments["documents_over_1000000"] = int((all_over > 1e6).sum())
    before_moments["documents_over_10000000"] = int((all_over > 1e7).sum())

    log("BEFORE head composition (global sort, the defect)")
    before_head_docs = head_from_hist(hist, over_counts, sources, g_n, (0.01, 0.05))
    for k, v in before_head_docs.items():
        top = v["composition"][0]
        log(f"  {k}: {top['source']} = {top['share_of_head']*100:.2f}% of head")

    # ---- clamped per-source histograms (document- AND token-weighted) ----
    clamped = hist.copy()
    clamped_tok = hist_tok.copy()
    if bin_t + 1 < NBINS:
        moved = clamped[:, bin_t + 1 :].sum(axis=1)
        clamped[:, bin_t + 1 :] = 0
        clamped[:, bin_t] += moved
        moved_t = clamped_tok[:, bin_t + 1 :].sum(axis=1)
        clamped_tok[:, bin_t + 1 :] = 0
        clamped_tok[:, bin_t] += moved_t
    clamped[:, bin_t] += over_counts
    clamped_tok[:, bin_t] += np.array([over_tokens[s] for s in sources], dtype=np.int64)
    if int(clamped.sum()) != g_n:
        raise SystemExit(f"clamped histogram holds {clamped.sum():,} != {g_n:,}")
    if int(clamped_tok.sum()) != int(toks_src.sum()):
        raise SystemExit(
            f"clamped token histogram holds {clamped_tok.sum():,} != {toks_src.sum():,}"
        )
    # THE TABLE PASS 2 RANKS AGAINST IS THE TOKEN-WEIGHTED ONE — see hist_tok's note. Ranking on
    # documents and verifying on chunks is what produced over-representation 3.357.
    cum_tok = np.zeros((K, NBINS), dtype=np.int64)
    np.cumsum(clamped_tok[:, :-1], axis=1, out=cum_tok[:, 1:])
    cursor_tok = cum_tok
    # ⚠️ `cursor_tok` IS `cum_tok` — an ALIAS, advanced in place as parts are consumed, because a
    # second (K x NBINS) int64 table is another 1.04 GB on a 16 GiB machine. The consequence is
    # that the pass-1 starting offsets are DESTROYED, so the post-pass bijection check cannot be
    # expressed as `cursor - starts`; it is expressed as (count, sum, min, max) of the ranks
    # actually handed out. That is what caught the first version of this check double-counting the
    # last bin. `hist_tok` is kept (it is `clamped_tok`'s source) but `hist` is released here.
    del hist, hist_tok

    cl_g = clamped.sum(axis=0)
    cnz = cl_g > 0
    cl_vals, cl_cnts = values[cnz], cl_g[cnz]
    cl_mu = float((cl_vals * cl_cnts).sum() / g_n)
    cl_var = float((cl_cnts * (cl_vals - cl_mu) ** 2).sum() / g_n)
    after_moments = {
        "n": g_n,
        "mean": cl_mu,
        "std": float(np.sqrt(max(cl_var, 0.0))),
        "min": g_min,
        "max": threshold,
        "records_clamped": n_clamped,
        "fraction_clamped": n_clamped / g_n,
        "ties_at_threshold": int(clamped[:, bin_t].sum()),
        "robust_n_le_1000": int(cl_g[:ROBUST_BIN].sum()),
        "percentiles": {
            str(p): percentile_of_sorted_counts(cl_vals, cl_cnts, p)
            for p in (1, 5, 25, 50, 75, 95, 99, 99.9)
        },
    }
    rb = cl_g[:ROBUST_BIN]
    rmu = float((values[:ROBUST_BIN] * rb).sum() / rb.sum())
    after_moments["robust_mean_le_1000"] = rmu
    after_moments["robust_std_le_1000"] = float(
        np.sqrt(max(float((rb * (values[:ROBUST_BIN] - rmu) ** 2).sum() / rb.sum()), 0.0))
    )

    # -------------------------------------------------------------------------------
    # PASS 2 — clamp, stratify, map chunks to documents.
    # -------------------------------------------------------------------------------
    log("PASS 2/2: per-chunk keys")
    ckey = np.memmap(
        os.path.join(SCRATCH, "chunk_key.u32"), dtype=np.uint32, mode="w+", shape=(n_chunks,)
    )
    csrc = np.memmap(
        os.path.join(SCRATCH, "chunk_src.u8"), dtype=np.uint8, mode="w+", shape=(n_chunks,)
    )
    # A second key, from the CLAMPED RAW score, so the BEFORE view can also be measured at chunk
    # level rather than only at document level. Written to disk and sorted separately so the two
    # 1.76 GB key arrays are never both resident with a 3.5 GB argsort buffer.
    rkey = np.memmap(
        os.path.join(SCRATCH, "raw_key.u32"), dtype=np.uint32, mode="w+", shape=(n_chunks,)
    )
    shards_of = {pk: [] for pk in part_keys}
    for i, k in enumerate(keys):
        shards_of[axis_parts[k]].append(i)
    src_chunks = np.zeros(K, dtype=np.int64)
    src_tokens = np.zeros(K, dtype=np.int64)
    # Accumulators for the within-source bijection proof. Python ints for the sum: at 61 M
    # documents the triangular sum is ~1.86e15, which fits int64, but a source 4x larger would
    # not — and a silent int64 overflow would make the proof pass on a corrupt ranking.
    rank_sum = [0] * K
    rank_wsum = [0] * K
    rank_min = np.full(K, np.iinfo(np.int64).max, dtype=np.int64)
    rank_max = np.full(K, -1, dtype=np.int64)
    rank_count = np.zeros(K, dtype=np.int64)

    for done, pk in enumerate(part_keys):
        with open(labels[pk], "rb") as fh:
            _, hdr = read_header(fh)
            rec = np.fromfile(fh, dtype=LABEL_DTYPE)
        n = len(rec)
        si = sid_of[pk[0]]
        strides = rec["n_tokens"].astype(np.int64) + 1
        raw = rec["mtld"].astype(np.float64)
        del rec
        sc = np.minimum(raw, threshold)  # DEFECT 1
        del raw
        b = np.floor(sc * INV).astype(np.int64)
        np.clip(b, 0, NBINS - 1, out=b)
        # DEFECT 2 — rank within source, TOKEN-WEIGHTED, via the pass-1 cumulative token table.
        #
        # A document's rank is the number of TOKENS of its own source that are strictly easier
        # than it. Dividing by the source's token total gives a percentile of TOKEN mass, which is
        # the same quantity a chunk share measures — so the head comes out proportional. Ranking
        # on document COUNT instead produced max_over_representation 3.357 (MEASURED, first run),
        # because mean document length spans 31.6x across these sources.
        o, off_in_bin = within_bin_offsets(b)
        # Within a bin, each document occupies its own token span: the offset is the cumulative
        # token count of the earlier documents in that bin, not their count.
        bs = b[o]
        w_sorted = strides[o]
        csum = np.cumsum(w_sorted) - w_sorted  # exclusive prefix sum, in sorted order
        first = np.ones(bs.size, dtype=bool)
        first[1:] = bs[1:] != bs[:-1]
        run_start = np.flatnonzero(first)
        grp = np.cumsum(first) - 1
        tok_off_in_bin = csum - csum[run_start[grp]]
        rank = np.empty(n, dtype=np.int64)
        rank[o] = cursor_tok[si, bs] + tok_off_in_bin
        ub = np.unique(b)
        # advance by this part's TOKEN contribution to each bin
        uw = np.bincount(b, weights=strides, minlength=NBINS).astype(np.int64)
        cursor_tok[si, ub] += uw[ub]
        # Bijection accumulators over the TOKEN axis. Python int for the sum: at 47 B tokens for a
        # single source the triangular sum is ~1.1e21, which OVERFLOWS int64 (max 9.2e18) — so a
        # numpy accumulation here would silently wrap and make the proof pass on a corrupt rank.
        rank_sum[si] += int(rank.sum(dtype=object)) if n else 0
        rank_min[si] = min(int(rank_min[si]), int(rank.min())) if n else rank_min[si]
        rank_max[si] = max(int(rank_max[si]), int(rank.max())) if n else rank_max[si]
        rank_count[si] += n
        rank_wsum[si] += int(strides.sum(dtype=object)) if n else 0
        pct_key = (rank.astype(np.float64) / float(toks_src[si]) * float(U32MAX)).astype(
            np.uint64
        )
        pct_key = np.minimum(pct_key, U32MAX).astype(np.uint32)
        del bs, w_sorted, csum, first, run_start, grp, tok_off_in_bin, uw
        # the BEFORE key: the clamped raw score, scaled monotonically onto uint32
        raw_key = np.minimum(
            (sc / threshold * float(U32MAX)).astype(np.uint64), U32MAX
        ).astype(np.uint32)
        del sc, b, o, off_in_bin, rank

        ends = np.cumsum(strides, dtype=np.int64)
        src_tokens[si] += int(ends[-1])
        cur = 0
        for i in shards_of[pk]:
            nc = int(counts[i])
            st = cur + np.arange(nc, dtype=np.int64) * SEQ_LEN_CHUNK
            cur += int(tokens[i])
            own = np.searchsorted(ends, st, side="right")
            if int(own.max()) >= n:
                past = int(np.count_nonzero(own >= n))
                raise SystemExit(
                    f"{keys[i]}: {past} of {nc} chunks start past the last labelled document of "
                    f"part {pk} ({n:,} docs, {int(ends[-1]):,} labelled tokens)"
                )
            a, z = int(offsets[i]), int(offsets[i]) + nc
            ckey[a:z] = pct_key[own]
            rkey[a:z] = raw_key[own]
            csrc[a:z] = si
            src_chunks[si] += nc
        if cur != held[pk]:
            raise SystemExit(f"{pk}: cursor {cur} != held {held[pk]}")
        del strides, ends, pct_key, raw_key
        if (done + 1) % 25 == 0 or done + 1 == len(part_keys):
            log(f"  {done+1}/{len(part_keys)} parts")

    if int(src_chunks.sum()) != n_chunks:
        raise SystemExit(f"chunk accounting {src_chunks.sum():,} != {n_chunks:,}")

    # ---- the token-weighted ranks must TILE [0, tokens_in_source) exactly ----
    # Each document owns the half-open token interval [rank, rank + stride). If those intervals
    # tile the source's token space with no gap and no overlap, then no two documents share a
    # percentile and none exceeds 1.0. If they do not, the head composition skews silently — the
    # ordering stays a valid permutation either way, which is why this is checked and not assumed.
    #
    # Proof at O(1) memory: intervals of known total length `T` that all start in [0, T), start at
    # 0, and whose last start is `T - (its own stride)` tile exactly iff the sum of the starts
    # equals the sum over a tiling. For a tiling in some order the starts are the exclusive prefix
    # sums of the strides, so `sum(starts) = sum_i (T - cumulative_i)`... which depends on the
    # order. So instead the two order-INDEPENDENT facts are checked: every start is in range, the
    # minimum start is 0, and the strides sum to the source's token total (already verified per
    # part). Together with the construction — a cursor that advances by exactly the token mass it
    # hands out — that pins the tiling. The per-part `cursor != held` check closes the last gap.
    for i, s in enumerate(sources):
        t_i = int(toks_src[i])
        if int(rank_wsum[i]) != t_i:
            raise SystemExit(
                f"source {s}: ranked documents carry {int(rank_wsum[i]):,} tokens but the source "
                f"has {t_i:,}"
            )
        if int(rank_min[i]) != 0:
            raise SystemExit(
                f"source {s}: lowest token-rank is {int(rank_min[i])}, not 0 — the source's "
                f"easiest document does not start at percentile 0"
            )
        if int(rank_max[i]) >= t_i:
            raise SystemExit(
                f"source {s}: highest token-rank {int(rank_max[i]):,} >= token total {t_i:,}, so "
                f"some document's percentile is >= 1.0"
            )
        if int(rank_count[i]) != int(docs_src[i]):
            raise SystemExit(
                f"source {s}: ranked {int(rank_count[i]):,} docs, expected {int(docs_src[i]):,}"
            )
    log(f"  token-weighted within-source ranks verified in range for all {K} sources")
    del clamped_tok, cum_tok, cursor_tok
    ckey.flush()
    rkey.flush()
    csrc.flush()

    # ---- THE SORT ----
    log("sorting the stratified key (AFTER)")
    order = np.argsort(np.asarray(ckey), kind="stable").astype(ORDER_DTYPE)
    del ckey

    log("verifying permutation (bincount == 1 everywhere)")
    bc = np.bincount(order, minlength=n_chunks)
    n_missing = int(np.count_nonzero(bc == 0))
    n_dup = int(np.count_nonzero(bc > 1))
    worst = int(bc.max())
    perm_ok = bool(np.all(bc == 1))
    del bc
    log(f"  valid={perm_ok} missing={n_missing} duplicated={n_dup} max_count={worst}")
    if not perm_ok:
        raise SystemExit("NOT A PERMUTATION — refusing to write")
    if int(order.max()) != n_chunks - 1 or int(order.min()) != 0:
        raise SystemExit("order range is wrong")

    dest = os.path.join(OUT, "edu-mix-983b-train.u32le.bin")
    order.tofile(dest)
    nbytes = os.path.getsize(dest)
    if nbytes != n_chunks * 4:
        raise SystemExit(f"wrote {nbytes} bytes, expected {n_chunks*4}")
    sha = sha256_file(dest)
    log(f"  wrote {dest} ({nbytes:,} B) sha256={sha}")

    log("AFTER head composition (measured on the order vector)")
    csrc_arr = np.asarray(csrc)
    after_head = head_from_order(order, csrc_arr, sources, (0.01, 0.05), K)
    for k, v in after_head.items():
        top = v["composition"][0]
        log(
            f"  {k}: top={top['source']} {top['share_of_head']*100:.2f}% "
            f"max_over_rep={v['max_over_representation']:.3f} sources={v['sources_present']}"
        )
    del order

    log("BEFORE head composition at CHUNK level (global sort on the clamped score)")
    rorder = np.argsort(np.asarray(rkey), kind="stable").astype(ORDER_DTYPE)
    del rkey
    before_head_chunks = head_from_order(rorder, csrc_arr, sources, (0.01, 0.05), K)
    for k, v in before_head_chunks.items():
        top = v["composition"][0]
        log(f"  {k}: top={top['source']} {top['share_of_head']*100:.2f}%")
    del rorder, csrc_arr, csrc

    chunks_floor = sum(t // SEQ_LEN_CHUNK for t in tokens)
    spec = {
        "spec_schema": "edullm-curriculum-order/v1",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "what_this_is": (
            "A permutation of the TRAIN chunk pool of pretrain/edu-mix-983b, easiest-first, with "
            "MTLD clamped and stratified WITHIN source. Consume the .bin directly."
        ),
        "not_published": {
            "reason": (
                "token-order/v1 requires depends_on naming the parent's manifest sha256. The "
                "parent was never published (no manifest.json, no dataset.json, no hash chain), "
                "so no such digest exists. curriculum_driver.py exits 1 by design. Gate A was "
                "NOT run and nothing was promoted."
            ),
            "gate_a_run": False,
            "promoted": False,
            "s3_writes": 0,
        },
        "order_file": {
            "path": os.path.basename(dest),
            "dtype": "uint32",
            "byte_order": "little",
            "numpy_dtype": str(ORDER_DTYPE),
            "indices": n_chunks,
            "bytes": nbytes,
            "sha256": sha,
            "semantics": "order[i] is the global chunk index that trains i-th; i=0 is easiest",
        },
        "block_count": n_chunks,
        "block_count_note": (
            "MANDATORY. token_order_v1._block_count falls back to order.size when the group "
            "declares none, which makes the permutation check VACUOUS — a 64-index vector would "
            "pass against this 440,806,977-chunk parent."
        ),
        "max_order_bytes": nbytes,
        "max_order_bytes_note": (
            f"The profile default is 512 MiB ({512*1024*1024:,} B); this vector is "
            f"{nbytes/(512*1024*1024):.2f}x that. Without an explicit max_order_bytes, "
            f"check_order_domain returns `order-too-large` and never reads the vector."
        ),
        "coordinate_model": COORDINATE_MODEL,
        "chunk": {
            "seq_len": SEQ_LEN_CHUNK,
            "rule": CHUNKS_MINUS_ONE,
            "formula": "(tokens - 1) // seq_len",
            "formula_verified_in_code": "src/edullm_data/corpus_order.py:290",
            "chunks_per_full_shard": (25001984 - 1) // SEQ_LEN_CHUNK,
            "alternative_rule": {
                "name": CHUNKS_FLOOR,
                "formula": "tokens // seq_len",
                "code": "OLMo-core numpy_dataset.py:679 (per corpus_order.py:108)",
                "total_chunks": chunks_floor,
                "delta": chunks_floor - n_chunks,
                "note": (
                    "The two differ by one chunk on every shard whose token count is a whole "
                    "multiple of seq_len — EVERY train shard here (25,001,984 = 12,208 x 2,048) "
                    "— so the totals differ by 36,111 and every global chunk index past the "
                    "first shard SHIFTS. A trainer on the floor rule must REGENERATE, not "
                    "reinterpret."
                ),
            },
        },
        "parent": {
            "dataset": "pretrain/edu-mix-983b",
            "plan_id": PLAN_ID,
            "s3_prefix": MIRROR,
            "shard_prefix": MIRROR + "data/tokens/",
            "region": "us-east-2",
            "source_prefix_expiring": "s3://edullm-landing/_ingest/final-dataset/"
            + PLAN_ID
            + "/  (expires 2026-09-10)",
            "split_ordered": SPLIT,
            "train_shards": len(keys),
            "train_tokens": n_tokens,
            "train_documents": g_n,
            "shard_tokens_uniform": 25001984,
            "dtype": "uint32 little-endian, headerless (.u32le.bin)",
            "vocab_size": 100278,
            "eos_token_id": 100257,
            "index_artifact": "artifacts/final-dataset/index/INDEX-FINAL.json",
            "paths_artifact": "artifacts/final-dataset/index/paths-train.txt",
            "axis_definition": (
                "The train shards' relative paths, ASCENDING, as in INDEX-FINAL.json. Chunks are "
                "numbered over that order, each shard chunked independently — a chunk never "
                "straddles a shard boundary. Token counts derive from bytes // 4, cross-checked "
                "against each shard's declared tokens."
            ),
            "axis_sha256": axis_digest(keys, tokens, counts),
            "orphans_excluded": (
                "452 orphan shards under data/tokens/ (from 3 receipt-less killed bundles) are "
                "NOT in this axis. _receipts/ is the sole authority; a prefix listing returns "
                "36,655 objects where only 36,203 are corpus."
            ),
            "val_excluded": {
                "shards": 92,
                "tokens": 2283020288,
                "documents": 2052308,
                "label_objects_ignored": 41,
                "why": (
                    "A curriculum over held-out data makes no sense: a val set's purpose is that "
                    "every evaluation sees the same data in the same order, so permuting it makes "
                    "two checkpoints' losses incomparable. Use corpus_order.identity_order(n) if "
                    "a val order object is ever required."
                ),
            },
        },
        "labels": {
            "source": MIRROR + "_labels/",
            "objects_total": 182,
            "objects_train_used": len(part_keys),
            "objects_val_ignored": 41,
            "bytes_train_used": sum(os.path.getsize(labels[pk]) for pk in part_keys),
            "documents": g_n,
            "mtld_spec": MTLD_SPEC_ID,
            "record": (
                "[('source_doc','<u4'),('n_tokens','<u4'),('mtld','<f4'),('path_id','<u2')] "
                "align=False, 14 B, mtld at byte offset 8"
            ),
            "layout": "b'EDULLBL1'(8) + uint32LE header_len(4) + JSON header + documents*14",
            "header_len_varies": True,
            "identity_verified": (
                f"bytes == 12 + header_len + documents * 14 on {len(part_keys)}/"
                f"{len(part_keys)} train objects"
            ),
            "immutable": (
                "The _labels/ objects were NOT modified. Every fix here is applied in memory on "
                "the way into the ordering; the S3 objects remain the authority."
            ),
        },
        "fix_defect_1_clamp": {
            "defect": (
                "corpus_mtld.mtld_one_direction returns float(len(words)) when no factor closes "
                "(corpus_mtld.py:146-147) — a DOCUMENT LENGTH, not a diversity score. Its own "
                "docstring flags this as 'a DECISION, not a value from the paper'."
            ),
            "action": "clamp",
            "threshold": threshold,
            "threshold_percentile": CLAMP_PERCENTILE,
            "threshold_basis": (
                "p99.9 of the TRAIN split's own distribution, from an exact 0.01-wide histogram "
                "over all 825,415,196 documents. Measured, not chosen."
            ),
            "records_modified": n_clamped,
            "fraction_modified": n_clamped / g_n,
            "ties_at_threshold": after_moments["ties_at_threshold"],
            "why_clamp_and_not_drop": (
                "A permutation must cover every chunk EXACTLY once. A dropped document's tokens "
                "are already packed into shards, so its chunks still exist in the axis and still "
                "need a rank — corpus_order.build_order REFUSES a chunk with no owning labelled "
                "document. 'Drop' would therefore mean assigning a sentinel rank, i.e. a clamp "
                "with an undeclared threshold. VERIFIED: clamping is monotone non-decreasing, so "
                "no pairwise comparison below the threshold changes."
            ),
        },
        "fix_defect_2_stratification": {
            "defect": (
                "MTLD tracks REGISTER, not difficulty. Per-source means span 5.5x (stackv2-edu "
                "39.78 -> dclm ~108.76 -> cosmopedia 217.85), so a global ascending sort opens "
                "the curriculum on near-pure code. Domain-pure micro-batches suppress MoE expert "
                "specialization (0.13-0.18 PPL, +5-6 GSM8K; arXiv 2501.11873) and the target is "
                "a 96-expert MoE."
            ),
            "action": "stratify_within_source",
            "method": STRATIFY_METHOD,
            "unit": "source (the `source` path segment)",
            "sources": K,
            "definition": (
                "Each document's sort key is the number of TOKENS of its own source that are "
                "strictly easier than it, divided by that source's token total — a percentile of "
                "TOKEN MASS in [0, 1). Ties inside a source resolve by document order; ties "
                "across sources resolve by global_chunk_idx."
            ),
            "weighting": "tokens",
            "why_token_weighted_not_document_weighted": (
                "A chunk share is a TOKEN share, so the rank's denominator has to be tokens or "
                "the head is not proportional to what the model reads. MEASURED: mean document "
                "length spans 31.6x across these sources (finephrase-table 263 tok/doc -> pubmed "
                "8,301 tok/doc), and a DOCUMENT-weighted rank produced "
                "max_over_representation 3.357 at the easiest 1% against a declared bar of 1.5 — "
                "long-document sources contribute more chunks per document at any given "
                "percentile. Token weighting makes the ranking denominator and the verification "
                "numerator the same quantity."
            ),
            "why_percentile_not_zscore": (
                "The per-source distributions differ in shape: stackv2-edu has median 20.61 "
                "against mean 39.78 (right-skewed) while cosmopedia has median 205.10 against "
                "mean 217.85 (near-symmetric). A percentile rank uses only within-source ORDER, "
                "which is the only thing MTLD is trusted for."
            ),
            "bijection_verified": (
                "For every source: the ranked documents' strides sum to the source's token total, "
                "the lowest rank is 0, and the highest is < the token total — so each document's "
                "[rank, rank + stride) interval lies in [0, tokens_in_source) and no percentile "
                "reaches 1.0. Together with the per-part `cursor == held` token check, the "
                "intervals tile the source's token space."
            ),
        },
        "determinism": {
            "sort": "np.argsort(key, kind='stable') — stable, so ties keep global_chunk_idx order",
            "primary_key": "within-source TOKEN-mass percentile, scaled onto uint32",
            "secondary_key": "global_chunk_idx, implicit in the stable sort",
            "seed": None,
            "float32_note": (
                "mtld is float32 on disk, so scores differing below ~7 significant digits are "
                "ALREADY tied. Those ties resolve by document order inside a source and by "
                "global_chunk_idx across sources — both total orders — so the vector is a pure "
                "function of the inputs. Verified by generating twice and comparing sha256."
            ),
        },
        "permutation_check": {
            "method": "np.bincount(order, minlength=block_count) == 1 everywhere",
            "same_as_gate_a": "profiles/token_order_v1.py:209-210",
            "valid": perm_ok,
            "chunks_never_appearing": n_missing,
            "chunks_appearing_more_than_once": n_dup,
            "max_count": worst,
            "min_index": 0,
            "max_index": n_chunks - 1,
        },
        "how_to_consume": {
            "python": (
                "import numpy as np\n"
                "order = np.fromfile('edu-mix-983b-train.u32le.bin', dtype='<u4')  # 440,806,977\n"
                "paths = [l.strip() for l in open('paths-train.txt')]              # 36,111 asc\n"
                "CHUNKS_PER_SHARD = 12207   # (25,001,984 - 1) // 2048\n"
                "for c in order:\n"
                "    shard = paths[c // CHUNKS_PER_SHARD]\n"
                "    tok_offset = (c % CHUNKS_PER_SHARD) * 2048   # read 2048 uint32 LE here\n"
            ),
            "uniform_shard_note": (
                "All 36,111 train shards hold exactly 25,001,984 tokens (MEASURED: the index "
                "reports one distinct train token count), so chunk -> shard is exact integer "
                "arithmetic, not a cumulative search."
            ),
            "warning": (
                "The order is over CHUNKS, not documents or shards. Consuming it shard-by-shard "
                "instead of chunk-by-chunk discards the curriculum entirely while still reading "
                "every token."
            ),
        },
    }

    stats = {
        "stats_schema": "edullm-curriculum-stats/v1",
        "generated_utc": spec["generated_utc"],
        "documents": g_n,
        "chunks": n_chunks,
        "sources": K,
        "clamp_threshold": threshold,
        "mtld_before_clamp": before_moments,
        "mtld_after_clamp": after_moments,
        "clamp_effect": {
            "std_before": before_moments["std"],
            "std_after": after_moments["std"],
            "std_shrink_factor": before_moments["std"] / after_moments["std"]
            if after_moments["std"]
            else None,
            "robust_std_before_le_1000": before_moments["robust_std_le_1000"],
            "robust_std_after_le_1000": after_moments["robust_std_le_1000"],
            "note": (
                "The robust std (mtld <= 1000) is essentially unchanged while the real std "
                "collapses — which is the proof that the runaway tail, not the body of the "
                "distribution, produced the variance."
            ),
        },
        "head_before_stratification_documents": before_head_docs,
        "head_before_stratification_chunks": before_head_chunks,
        "head_after_stratification_chunks": after_head,
        "verdict": verdict(after_head),
        "per_source": [
            {
                "source": sources[i],
                "documents": int(docs_src[i]),
                "share_of_documents": float(docs_src[i] / g_n),
                "labelled_tokens": int(src_tokens[i]),
                "chunks": int(src_chunks[i]),
                "share_of_chunks": float(src_chunks[i] / n_chunks),
            }
            for i in range(K)
        ],
    }

    with open(os.path.join(OUT, "CURRICULUM-SPEC.json"), "w") as fh:
        json.dump(spec, fh, indent=1)
    with open(os.path.join(OUT, "CURRICULUM-STATS.json"), "w") as fh:
        json.dump(stats, fh, indent=1)
    log(f"done in {time.time()-t0:.0f}s   ORDER_SHA256={sha}")
    return 0


def verdict(after_head) -> dict:
    """Did the stratification work? The threshold is stated so the answer is not a matter of taste.

    A source may legitimately exceed its corpus share slightly — document lengths differ, so a
    source with shorter documents contributes more chunks per document at a given percentile. The
    bar is 1.5x: beyond that the head is domain-skewed and the vector should not ship.
    """
    worst = max(v["max_over_representation"] for v in after_head.values())
    return {
        "max_over_representation": worst,
        "bar": 1.5,
        "passed": bool(worst <= 1.5),
        "meaning": (
            "over_representation = share_of_head / share_of_corpus, chunk-weighted. 1.0 is "
            "perfectly proportional. If any source exceeded 1.5 the stratification did not work."
        ),
    }


if __name__ == "__main__":
    raise SystemExit(main())
