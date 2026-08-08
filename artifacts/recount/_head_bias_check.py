#!/usr/bin/env python3
"""Is the tok/byte ratio biased by sampling the datasets-server PARTIAL head?

The ratio in `_tokbyte_filtered.py` is sampled from the converted `_filtered` parquet, whose
conversion covers only 1.28M of ~27.5M rows for stackexchange and 2.50M of ~23.3M for
github_archive. If parquet row order correlates with content, that head is not a random sample.

For StackExchange the risk is concrete and large: the source repo is laid out ONE DIRECTORY PER
SITE in alphabetical order (`3dprinting.meta...`, `academia...`, ... `stackoverflow.com` with 95
files, ... `writers...`). Stack Overflow is both the biggest site and by far the most code-dense,
and code raises tok/byte sharply (Phase 0: 0.4524 on a terminal-heavy doc vs 0.2101 on prose).
If the converted head stops before `s`, the sampled ratio is a PROSE-SITE ratio and understates
the corpus.

This reads only the small `metadata` struct (site / repo) -- never `text` -- across row groups
spread over every converted file, and reports which sites the head actually covers.
"""
from __future__ import annotations

import json
import os
import random
import sys
from collections import Counter

import pyarrow.parquet as pq

sys.path.insert(0, __file__.rsplit("/", 1)[0])
if not os.environ.get("HF_TOKEN"):
    p = os.path.expanduser("~/.cache/huggingface/token")
    if os.path.exists(p):
        os.environ["HF_TOKEN"] = open(p).read().strip()
from _footer_chars import RangeFile, parquet_urls  # noqa: E402


def check(repo: str, field: str, n_rg: int = 60, seed: int = 0) -> dict:
    files, partial = parquet_urls(repo, "default")
    files = [f for f in files if f.get("split") == "train"] or files
    index = []
    for f in files:
        rf = RangeFile(f["url"], f["size"])
        md = pq.ParquetFile(rf).metadata
        for rg in range(md.num_row_groups):
            index.append((f, rg))
    rnd = random.Random(seed)
    pick = rnd.sample(index, min(n_rg, len(index)))
    vals: Counter = Counter()
    fetched = 0
    first_last = []
    for f, rg in pick:
        rf = RangeFile(f["url"], f["size"])
        pf = pq.ParquetFile(rf)
        try:
            t = pf.read_row_group(rg, columns=["metadata"])
        except Exception as e:  # noqa: BLE001
            print(f"  rg{rg} FAILED {type(e).__name__}", file=sys.stderr)
            continue
        got = [(m or {}).get(field) for m in t.column("metadata").to_pylist()]
        vals.update([g for g in got if g])
        fetched += rf.bytes_fetched
        if got:
            first_last.append((got[0], got[-1]))
        print(f"[{repo}] {sum(vals.values()):,} rows, {len(vals)} distinct {field}, "
              f"{fetched/1e6:.0f} MB", file=sys.stderr)
    tot = sum(vals.values())
    return {
        "repo": repo,
        "field": field,
        "parquet_conversion_partial": partial,
        "n_row_groups_sampled": len(pick),
        "n_row_groups_total": len(index),
        "rows_seen": tot,
        "distinct_values": len(vals),
        "top_25_by_rows": [{"v": k, "rows": v, "share": round(v / tot, 5)}
                           for k, v in vals.most_common(25)],
        "alphabetical_range": (min(vals), max(vals)) if vals else None,
        "bytes_fetched": fetched,
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("repo")
    p.add_argument("--field", default="site")
    p.add_argument("--n-rg", type=int, default=60)
    p.add_argument("--out", default=None)
    a = p.parse_args()
    r = check(a.repo, a.field, a.n_rg)
    if a.out:
        open(a.out, "w").write(json.dumps(r, indent=2) + "\n")
    print(json.dumps(r, indent=2))
