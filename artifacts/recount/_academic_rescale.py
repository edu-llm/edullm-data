#!/usr/bin/env python3
"""Rescale the academic category's recount results past a datasets-server PARTIAL conversion.

WHY THIS EXISTS. recount.py's estimator is

    tokens = /size num_rows  x  mean_chars_per_doc  x  tokens/char

and it treats `/size num_rows` as exact. For all three common-pile academic corpora that
assumption is FALSE: they are shipped as `.json.gz`, and datasets-server stopped its parquet
conversion at its ~2.4 GB partial-conversion cap (`/parquet` -> `"partial": true`, 10 files of
48 / 17 / 8 real shards). So `/size num_rows` counts only the converted HEAD:

    peS2o         165,031 rows converted  vs  6,117,280 real docs  (37.07x short)
    pubmed        163,259 rows converted  vs  3,829,689 real docs  (23.46x short)
    arxiv_papers   78,823 rows converted  vs    304,048 real docs  ( 3.86x short)

Nothing in recount.py notices this -- `stats_partial` flags a partial /statistics MEAN, not a
partial ROW COUNT -- so its `est_total_tokens` for these three is an estimate of the converted
head, not of the corpus, and is low by the factors above. This is the same class of bug as the
README's two traps: a plausible number that no check falsifies.

THE FIX, and why it is better than just multiplying by the row factor. The Common Pile paper
(arXiv:2506.05209) Table 6 reports exact whole-corpus document counts AND exact UTF-8 GB, and
those agree with the HF cards to the digit. Bytes are the sturdier anchor: tokens/byte is a
property of script and domain (the README's own argument for tokens/char), it needs no
mean-document-length term at all, and it is immune to the head being made of atypically short
or long documents. So the primary estimator here is

    tokens = whole_corpus_utf8_bytes (exact, published)  x  tokens/byte (sampled, measured)

with the row-count-rescaled version reported alongside as an independent cross-check. Where the
two agree, the head is representative; where they diverge, the head is length-biased and the
byte anchor is the one to trust.
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# Exact whole-corpus figures. Common Pile paper (arXiv:2506.05209) Table 6 "Filtered" columns,
# independently confirmed by each HF dataset card's "Dataset Statistics" table.
CORPORA = {
    "common-pile/peS2o_filtered": {
        "slug": "pes2o",
        "true_docs": 6_117_280,
        "utf8_gb": 182.6,
        "card_tokens": 43_300_000_000,   # design doc 3.2
        "gz_bytes": 30_570_879_987,      # summed over all 48 .json.gz shards via HF tree API
        "n_shards": 48,
    },
    "common-pile/pubmed_filtered": {
        "slug": "pubmed",
        "true_docs": 3_829_689,
        "utf8_gb": 147.1,
        "card_tokens": 36_600_000_000,
        "gz_bytes": 43_555_182_605,
        "n_shards": 17,
    },
    "common-pile/arxiv_papers_filtered": {
        "slug": "arxiv",
        "true_docs": 304_048,
        "utf8_gb": 19.0,
        "card_tokens": 6_000_000_000,
        "gz_bytes": 6_037_455_094,
        "n_shards": 8,
    },
}


def load(slug: str) -> dict | None:
    p = os.path.join(HERE, f"academic-{slug}.json")
    if not os.path.exists(p):
        return None
    return json.load(open(p))


def main() -> int:
    rows = []
    for ds, meta in CORPORA.items():
        raw = load(meta["slug"])
        if raw is None:
            print(f"{ds}: NO RESULT FILE YET")
            continue
        cfg = raw["configs"][0]
        if cfg.get("status") != "ok":
            print(f"{ds}: status={cfg.get('status')} errors={str(cfg.get('errors'))[:200]}")
            continue

        utf8_bytes = meta["utf8_gb"] * 1e9
        tpb = cfg["tokens_per_byte"]
        tpc = cfg["tokens_per_char"]
        converted_rows = cfg["n_rows"]
        row_factor = meta["true_docs"] / converted_rows

        byte_anchored = int(utf8_bytes * tpb)
        rescaled = int(cfg["est_total_tokens"] * row_factor)
        divergence = abs(byte_anchored - rescaled) / max(byte_anchored, rescaled)

        # chars/doc implied by the whole corpus vs measured in the converted head: >1 means the
        # head is made of SHORTER docs than the corpus average.
        head_mean_chars = cfg["mean_chars_per_doc_sampled"]
        corpus_mean_bytes = utf8_bytes / meta["true_docs"]

        rows.append({
            "dataset": ds,
            "true_docs": meta["true_docs"],
            "utf8_gb": meta["utf8_gb"],
            "converted_rows": converted_rows,
            "row_factor": round(row_factor, 3),
            "tokens_per_byte": tpb,
            "tokens_per_char": tpc,
            "head_mean_chars_sampled": head_mean_chars,
            "corpus_mean_bytes_per_doc": round(corpus_mean_bytes, 1),
            "head_length_bias": round(head_mean_chars / corpus_mean_bytes, 3),
            "byte_anchored_tokens": byte_anchored,
            "rowfactor_rescaled_tokens": rescaled,
            "cross_check_divergence": round(divergence, 4),
            "uncorrected_est_total_tokens": cfg["est_total_tokens"],
            "estimator_uncorrected": cfg["estimator"],
            "confidence_uncorrected": cfg["confidence"],
            "n_sampled": cfg["n_sampled"],
            "card_tokens": meta["card_tokens"],
            "card_over_measured": round(meta["card_tokens"] / byte_anchored, 4),
            "text_column": cfg["text_column"],
            "text_column_chosen_by": cfg["text_column_chosen_by"],
            "stats_available": cfg.get("stats_available"),
            "sample_mean_rel_halfwidth": cfg.get("sample_mean_rel_halfwidth"),
            "cv_tokens_per_doc": cfg.get("cv_tokens_per_doc"),
            "tokens_per_char_cv": cfg.get("tokens_per_char_cv"),
        })

    out = os.path.join(HERE, "_academic_rescale.json")
    json.dump(rows, open(out, "w"), indent=2)
    for r in rows:
        print(f"\n=== {r['dataset']}")
        for k, v in r.items():
            if k != "dataset":
                print(f"    {k:32} {v}")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
