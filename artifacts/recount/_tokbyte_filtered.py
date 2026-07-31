#!/usr/bin/env python3
"""tok/byte + per-document LICENSE, sampled from the datasets-server-converted `_filtered` parquet.

Why this and not `_tokbyte_qa.py` (which samples common-pile/raw_v0.1_parquet):

  1. RIGHT ROW SET. §3.2 specifies the `_filtered` corpora. raw_v0.1_parquet is the RAW
     collection, so a ratio sampled there is a ratio for a different (superset) row set.
  2. 100-700x CHEAPER PER DOC. The raw repo writes ~40k-120k-row row groups (measured: one
     ubuntu_irc text chunk = 565 MB for 85 usable docs). The converted parquet writes 437-912
     row groups (0.8-5.5 MB), so a 600-doc sample costs tens of MB, not tens of GB.
  3. IT CARRIES THE LICENSE COLUMN. The `_filtered` schema exposes a top-level `license` string
     per document. That is what makes the CC-BY-SA vs non-SA token split MEASURABLE rather than
     assumed -- Phase 0 could only read it out of `metadata.all_licenses` on 2 rows.

The `_filtered` parquet conversion is PARTIAL for stackexchange (1.28M of ~27.5M rows) and
github_archive (2.50M of ~23.3M) -- fine for a RATIO (an intensive property), fatal for a total
(an extensive one). Totals come from footers over the complete raw repo, scaled; see qa-forum.json.
Row-group clustering is handled the same way as _tokbyte_qa.py: the between-row-group CV, not the
per-document CV, is the honest uncertainty on the ratio.

NO DATASET DOWNLOAD (§5.7): sampled column chunks only, nothing written to disk.
"""
from __future__ import annotations

import io
import json
import math
import os
import random
import statistics
import sys
import time
import urllib.request

import pyarrow.parquet as pq

sys.path.insert(0, __file__.rsplit("/", 1)[0])


def _tok_env():
    if not os.environ.get("HF_TOKEN"):
        p = os.path.expanduser("~/.cache/huggingface/token")
        if os.path.exists(p):
            os.environ["HF_TOKEN"] = open(p).read().strip()


_tok_env()
from _footer_chars import RangeFile, parquet_urls  # noqa: E402


def enumerate_rgs(repo: str, config: str = "default", text_column: str = "text") -> tuple[list[dict], bool]:
    """Footer pass. NOTE the per-document license is `metadata.license`, a STRUCT CHILD -- the
    parquet leaf-path list shows a bare 'license' but arrow exposes it only under `metadata`,
    so selecting columns=['license'] raises KeyError. We request `metadata` and read the child."""
    files, partial = parquet_urls(repo, config)
    files = [f for f in files if f.get("split") == "train"] or files
    out = []
    partial_flag = partial
    for f in files:
        rf = RangeFile(f["url"], f["size"])
        pf = pq.ParquetFile(rf)
        md = pf.metadata
        idx = md.schema.names.index(text_column)
        top = set(pf.schema_arrow.names)
        has_meta = "metadata" in top
        has_src = "source" in top
        for rg in range(md.num_row_groups):
            out.append({"url": f["url"], "size": f["size"], "rg": rg,
                        "rows": md.row_group(rg).num_rows,
                        "compressed": md.row_group(rg).column(idx).total_compressed_size,
                        "has_metadata": has_meta, "has_source": has_src})
    return out, partial_flag


def sample(repo: str, n_docs: int = 700, seed: int = 0, max_mb: int = 400,
           text_column: str = "text", per_file: int = 12) -> dict:
    rgs, partial = enumerate_rgs(repo, "default", text_column)
    rnd = random.Random(seed)
    by_file: dict[str, list[dict]] = {}
    for r in rgs:
        by_file.setdefault(r["url"], []).append(r)
    files = sorted(by_file)
    rnd.shuffle(files)
    # round-robin across files so no single file dominates the sample
    picks: list[dict] = []
    pools = []
    for u in files:
        c = by_file[u][:]
        rnd.shuffle(c)
        pools.append(c[:per_file])
    for i in range(per_file):
        for pool in pools:
            if i < len(pool):
                picks.append(pool[i])

    from transformers import AutoTokenizer
    tk = AutoTokenizer.from_pretrained("allenai/dolma2-tokenizer")

    docs_tok, docs_byte, docs_char = [], [], []
    lic_bytes: dict[str, int] = {}
    lic_tokens: dict[str, int] = {}
    lic_docs: dict[str, int] = {}
    src_bytes: dict[str, int] = {}
    src_tokens: dict[str, int] = {}
    src_docs: dict[str, int] = {}
    per_rg = []
    transferred = 0

    has_lic_any = bool(rgs) and rgs[0]["has_metadata"]
    has_src_any = bool(rgs) and rgs[0]["has_source"]
    cols = [text_column] + (["metadata"] if has_lic_any else []) + (["source"] if has_src_any else [])

    for r in picks:
        if len(docs_tok) >= n_docs or transferred / 1e6 > max_mb:
            break
        rf = RangeFile(r["url"], r["size"])
        try:
            tb = pq.ParquetFile(rf).read_row_group(r["rg"], columns=cols)
        except Exception as e:  # noqa: BLE001
            print(f"[tbf {repo}] rg{r['rg']} FAILED {type(e).__name__}: {e}"[:160], file=sys.stderr)
            continue
        transferred += rf.bytes_fetched
        texts = tb.column(text_column).to_pylist()
        if has_lic_any:
            meta = tb.column("metadata").to_pylist()
            lics = [(m or {}).get("license") for m in meta]
        else:
            lics = [None] * len(texts)
        srcs = tb.column("source").to_pylist() if has_src_any else [None] * len(texts)
        k = min(len(texts), max(20, n_docs // max(1, len(picks))) if len(picks) else len(texts))
        k = min(max(k, 25), len(texts))
        idxs = rnd.sample(range(len(texts)), k)
        rt = rb = rc = 0
        for i in idxs:
            s = texts[i]
            if not s:
                continue
            b = len(s.encode("utf-8"))
            t = len(tk(s, add_special_tokens=False)["input_ids"])
            docs_tok.append(t)
            docs_byte.append(b)
            docs_char.append(len(s))
            rt += t
            rb += b
            rc += len(s)
            L = (lics[i] or "UNKNOWN")
            lic_bytes[L] = lic_bytes.get(L, 0) + b
            lic_tokens[L] = lic_tokens.get(L, 0) + t
            lic_docs[L] = lic_docs.get(L, 0) + 1
            S = (srcs[i] or "UNKNOWN")
            src_bytes[S] = src_bytes.get(S, 0) + b
            src_tokens[S] = src_tokens.get(S, 0) + t
            src_docs[S] = src_docs.get(S, 0) + 1
        if rb:
            per_rg.append({"rg": r["rg"], "n": k, "tpb": rt / rb, "tpc": rt / rc})
        print(f"[tbf {repo}] {len(docs_tok)} docs, {transferred/1e6:.0f} MB, "
              f"{len(per_rg)} rgs", file=sys.stderr)

    if not docs_tok:
        return {"status": "FAILED", "repo": repo}

    ratios = [t / b for t, b in zip(docs_tok, docs_byte) if b]
    rg_r = [x["tpb"] for x in per_rg]
    agg = sum(docs_tok) / sum(docs_byte)

    def cv(xs):
        m = statistics.fmean(xs)
        return (statistics.stdev(xs) / m) if len(xs) > 1 and m else None

    se_rg = (statistics.stdev(rg_r) / math.sqrt(len(rg_r))) if len(rg_r) > 1 else None
    tot_b = sum(lic_bytes.values())
    return {
        "status": "ok",
        "repo": repo,
        "tokenizer": "allenai/dolma2-tokenizer",
        "parquet_conversion_partial": partial,
        "n_docs": len(docs_tok),
        "n_row_groups_sampled": len(per_rg),
        "n_row_groups_total": len(rgs),
        "bytes_transferred": transferred,
        "tokens_per_byte": agg,
        "tokens_per_char": sum(docs_tok) / sum(docs_char),
        "bytes_per_char": sum(docs_byte) / sum(docs_char),
        "tpb_doc_mean": statistics.fmean(ratios),
        "tpb_doc_median": statistics.median(ratios),
        "tpb_doc_min": min(ratios),
        "tpb_doc_max": max(ratios),
        "tpb_doc_spread_x": max(ratios) / min(ratios),
        "tpb_doc_cv": cv(ratios),
        "tpb_rowgroup_cv": cv(rg_r),
        "tpb_rowgroup_se": se_rg,
        "rel_ci95_from_rowgroups": (1.96 * se_rg / agg) if se_rg else None,
        "mean_doc_bytes": statistics.fmean(docs_byte),
        "median_doc_bytes": statistics.median(docs_byte),
        "license_column_present": has_lic_any,
        "license_byte_share": {k: round(v / tot_b, 6) for k, v in
                               sorted(lic_bytes.items(), key=lambda x: -x[1])} if tot_b else None,
        "license_token_share": {k: round(v / sum(lic_tokens.values()), 6) for k, v in
                                sorted(lic_tokens.items(), key=lambda x: -x[1])} if lic_tokens else None,
        "license_doc_counts": dict(sorted(lic_docs.items(), key=lambda x: -x[1])),
        "license_tokens_per_byte": {k: round(lic_tokens[k] / lic_bytes[k], 5)
                                    for k in lic_bytes if lic_bytes[k]},
        "source_doc_counts": dict(sorted(src_docs.items(), key=lambda x: -x[1])),
        "source_token_share": {k: round(v / sum(src_tokens.values()), 6) for k, v in
                               sorted(src_tokens.items(), key=lambda x: -x[1])} if src_tokens else None,
        "per_row_group": per_rg,
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("repo")
    p.add_argument("--n-docs", type=int, default=700)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--max-mb", type=int, default=400)
    p.add_argument("--per-file", type=int, default=12)
    p.add_argument("--out", default=None)
    a = p.parse_args()
    r = sample(a.repo, a.n_docs, a.seed, a.max_mb, per_file=a.per_file)
    if a.out:
        open(a.out, "w").write(json.dumps(r, indent=2) + "\n")
    print(json.dumps({k: v for k, v in r.items() if k != "per_row_group"}, indent=2))
