#!/usr/bin/env python3
"""Sample tokens-per-BYTE under allenai/dolma2-tokenizer from random parquet row groups.

Phase 0c, QA/forum category. This category has the WORST tok/byte variance of any: Phase 0
measured 0.4524 tok/char on a terminal/code-heavy StackExchange answer vs 0.2101 on a prose
answer -- a 2.15x spread inside one corpus -- because Q&A interleaves prose with code blocks,
shell transcripts and tracebacks. The single-ratio estimator
`tokens = total_text_bytes x tokens_per_byte` rests on tok/byte having LOW variance, so this
script's job is as much to MEASURE the CV as to produce the ratio.

Sampling design, and why it is not a head read:
  * every (file, row_group) pair in the prefix is enumerated from footers;
  * row groups are drawn at RANDOM, spread across distinct files (at most `--per-file` per file),
    so no contiguous prefix of the corpus dominates;
  * within a drawn row group, documents are drawn at random rather than taken in order.

Because a whole column chunk is the minimum unit parquet will serve, docs are CLUSTERED by row
group. That inflates the true sampling variance relative to n independent draws, so the script
reports BOTH the pooled per-document CV and the between-row-group CV of the ratio -- the latter
is the honest uncertainty on the corpus-level ratio.

Two ratios are reported and they are NOT interchangeable:
    tokens_per_byte  -- what multiplies footer `total_uncompressed_size` (which is BYTES)
    tokens_per_char  -- comparable to Phase 0's figures and to recount.py's /statistics path
Non-ASCII content makes these differ; the byte one is the one this category needs.

NO DATASET DOWNLOAD (§5.7): only the sampled column chunks cross the wire and nothing is written
to disk. `--max-mb` caps total transfer.
"""
from __future__ import annotations

import json
import math
import random
import statistics
import sys

import pyarrow.parquet as pq

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from _footer_cp_raw import BASE, RangeFile, tree  # noqa: E402


def _tok():
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained("allenai/dolma2-tokenizer")


def enumerate_row_groups(prefix: str, text_column: str = "text") -> list[dict]:
    """Footer-only pass: every row group with its COMPRESSED text chunk size (the transfer cost)."""
    out = []
    for f in tree(prefix):
        rf = RangeFile(BASE + f["path"], f["size"])
        md = pq.ParquetFile(rf).metadata
        idx = md.schema.names.index(text_column)
        for rg in range(md.num_row_groups):
            cc = md.row_group(rg).column(idx)
            out.append({
                "path": f["path"], "size": f["size"], "rg": rg,
                "rows": md.row_group(rg).num_rows,
                "compressed": cc.total_compressed_size,
                "uncompressed": cc.total_uncompressed_size,
            })
    return out


def sample(prefix: str, n_docs: int = 600, per_file: int = 1, seed: int = 0,
           max_mb: int = 900, text_column: str = "text") -> dict:
    rgs = enumerate_row_groups(prefix, text_column)
    rnd = random.Random(seed)

    # Spread across files: shuffle files, then take up to `per_file` random row groups from each.
    by_file: dict[str, list[dict]] = {}
    for r in rgs:
        by_file.setdefault(r["path"], []).append(r)
    files = sorted(by_file)
    rnd.shuffle(files)
    order = []
    for p in files:
        cand = by_file[p][:]
        rnd.shuffle(cand)
        order.extend(cand[:per_file])

    tk = _tok()
    per_rg = []
    docs_tok, docs_byte, docs_char = [], [], []
    transferred = 0
    used = []
    for r in order:
        if len(docs_tok) >= n_docs or transferred / 1e6 > max_mb:
            break
        rf = RangeFile(BASE + r["path"], r["size"])
        pf = pq.ParquetFile(rf)
        try:
            col = pf.read_row_group(r["rg"], columns=[text_column]).column(text_column).to_pylist()
        except Exception as e:  # noqa: BLE001
            print(f"[tokbyte {prefix}] {r['path']}#{r['rg']} FAILED {type(e).__name__}", file=sys.stderr)
            continue
        transferred += rf.bytes_fetched
        # random docs inside the row group, never the head
        want = min(max(1, n_docs // max(1, min(len(order), math.ceil(n_docs / 50)))), len(col))
        want = min(max(want, 40), len(col))
        picks = rnd.sample(range(len(col)), want)
        rt, rb, rc = 0, 0, 0
        n_empty = 0
        for i in picks:
            s = col[i]
            if not s:
                n_empty += 1
                continue
            b = len(s.encode("utf-8"))
            t = len(tk(s, add_special_tokens=False)["input_ids"])
            docs_tok.append(t)
            docs_byte.append(b)
            docs_char.append(len(s))
            rt += t
            rb += b
            rc += len(s)
        if rb:
            per_rg.append({"path": r["path"], "rg": r["rg"], "n_docs": len(picks) - n_empty,
                           "tokens_per_byte": rt / rb, "tokens_per_char": rt / rc,
                           "mean_bytes": rb / max(1, len(picks) - n_empty)})
            used.append(f"{r['path']}#{r['rg']}")
        print(f"[tokbyte {prefix}] {len(docs_tok)} docs, {transferred/1e6:.0f} MB, "
              f"rg t/b={rt/rb:.4f}" if rb else "", file=sys.stderr)

    if not docs_tok:
        return {"status": "FAILED", "prefix": prefix}

    ratios_b = [t / b for t, b in zip(docs_tok, docs_byte) if b]
    ratios_c = [t / c for t, c in zip(docs_tok, docs_char) if c]
    agg_b = sum(docs_tok) / sum(docs_byte)
    agg_c = sum(docs_tok) / sum(docs_char)
    rg_ratios = [r["tokens_per_byte"] for r in per_rg]

    def cv(xs):
        m = statistics.fmean(xs)
        return (statistics.stdev(xs) / m) if len(xs) > 1 and m else None

    cv_doc = cv(ratios_b)
    cv_rg = cv(rg_ratios)
    # honest SE on the corpus ratio: between-cluster, n = number of row groups
    se_rg = (statistics.stdev(rg_ratios) / math.sqrt(len(rg_ratios))) if len(rg_ratios) > 1 else None
    return {
        "status": "ok",
        "prefix": prefix,
        "tokenizer": "allenai/dolma2-tokenizer",
        "n_docs": len(docs_tok),
        "n_row_groups_sampled": len(per_rg),
        "n_row_groups_total": len(rgs),
        "n_files_touched": len({r["path"] for r in per_rg}),
        "bytes_transferred": transferred,
        # BYTE-weighted aggregate: the correct multiplier for a footer byte total
        "tokens_per_byte": agg_b,
        "tokens_per_char": agg_c,
        "tokens_per_byte_doc_mean": statistics.fmean(ratios_b),
        "tokens_per_byte_doc_median": statistics.median(ratios_b),
        "tokens_per_byte_doc_min": min(ratios_b),
        "tokens_per_byte_doc_max": max(ratios_b),
        "tokens_per_byte_doc_spread_x": max(ratios_b) / min(ratios_b),
        "tokens_per_byte_doc_cv": cv_doc,
        "tokens_per_byte_rowgroup_cv": cv_rg,
        "tokens_per_byte_rowgroup_se": se_rg,
        "tokens_per_byte_rowgroup_min": min(rg_ratios),
        "tokens_per_byte_rowgroup_max": max(rg_ratios),
        "rel_ci95_from_rowgroups": (1.96 * se_rg / agg_b) if se_rg else None,
        "mean_doc_bytes_sampled": statistics.fmean(docs_byte),
        "bytes_per_char": sum(docs_byte) / sum(docs_char),
        "per_row_group": per_rg,
        "row_groups_used": used,
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("prefix")
    p.add_argument("--n-docs", type=int, default=600)
    p.add_argument("--per-file", type=int, default=1)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--max-mb", type=int, default=900)
    p.add_argument("--out", default=None)
    a = p.parse_args()
    r = sample(a.prefix, a.n_docs, a.per_file, a.seed, a.max_mb)
    if a.out:
        open(a.out, "w").write(json.dumps(r, indent=2) + "\n")
    print(json.dumps({k: v for k, v in r.items() if k not in ("per_row_group", "row_groups_used")}, indent=2))
