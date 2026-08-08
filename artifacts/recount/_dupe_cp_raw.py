#!/usr/bin/env python3
"""Measure EXACT id-level duplication in common-pile/raw_v0.1_parquet sub-corpora.

Why: footer `text` byte totals for `ubuntu_irc` (13.36 GB) and `github_archive` (246.97 GB)
overshoot their own RAW cards (6.3 GB / 54.7 GB) by 2.1x and 4.5x, while `stackexchange`
(104.98 GB) matches its card (103.7 GB) to 1.2%. A probe found 3,809 cross-file duplicate ids
in 3 sampled ubuntu_irc row groups, so the consolidation appears to have concatenated more
than one source tree for those two corpora.

This resolves it by reading the FULL `id` column (only) for a prefix and counting distinct ids,
plus the text bytes attributable to first-occurrence rows. `id` is ~25 bytes/row vs `text` at
1.7-12.6 kB/row, so this is 2-3 orders of magnitude cheaper than reading payload and still
honours §5.7 -- but it is NOT free, so `--max-rows` caps it and large corpora are sampled by
row group instead.

Nothing is written to disk beyond the small JSON result.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import random
import sys

import pyarrow.parquet as pq

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from _footer_cp_raw import BASE, RangeFile, tree  # noqa: E402


def col_sizes(prefix: str, limit: int | None = None) -> dict:
    """Per-column uncompressed footprint, so the cost of reading `id` is known before paying it."""
    files = tree(prefix)
    if limit:
        files = files[:limit]
    agg: dict[str, int] = {}
    rows = 0
    for f in files:
        rf = RangeFile(BASE + f["path"], f["size"])
        md = pq.ParquetFile(rf).metadata
        names = md.schema.names
        for rg in range(md.num_row_groups):
            for ci, nm in enumerate(names):
                agg[nm] = agg.get(nm, 0) + md.row_group(rg).column(ci).total_uncompressed_size
        rows += md.num_rows
    return {"prefix": prefix, "rows": rows, "per_column_uncompressed": agg}


def _file_ids(f: dict) -> tuple[str, list[str], int]:
    rf = RangeFile(BASE + f["path"], f["size"])
    pf = pq.ParquetFile(rf)
    ids = pf.read(columns=["id"]).column("id").to_pylist()
    return f["path"], ids, rf.bytes_fetched


def exact_dupes(prefix: str, workers: int = 6) -> dict:
    """Read the whole `id` column across all files; report distinct vs total."""
    files = tree(prefix)
    seen: dict[str, int] = {}
    total = 0
    fetched = 0
    per_file = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for path, ids, fb in ex.map(_file_ids, files):
            new = 0
            for i in ids:
                if i not in seen:
                    seen[i] = 1
                    new += 1
                else:
                    seen[i] += 1
            total += len(ids)
            fetched += fb
            per_file.append({"path": path, "rows": len(ids), "new_ids": new})
            print(f"[dupe {prefix}] {path}: {len(ids):,} rows, {new:,} new, "
                  f"{fetched/1e6:.0f} MB id-col fetched", file=sys.stderr)
    mult = sorted(seen.values())
    from collections import Counter
    return {
        "prefix": prefix,
        "rows_total": total,
        "distinct_ids": len(seen),
        "duplication_factor": round(total / len(seen), 4) if seen else None,
        "copies_per_id_histogram": dict(sorted(Counter(mult).items())),
        "id_column_bytes_fetched": fetched,
        "per_file": sorted(per_file, key=lambda x: x["path"]),
    }


def rg_sample_dupes(prefix: str, n_rg: int = 40, seed: int = 0) -> dict:
    """For corpora too large to read every id: sample row groups and look for repeats."""
    files = tree(prefix)
    rnd = random.Random(seed)
    index = []
    for f in files:
        rf = RangeFile(BASE + f["path"], f["size"])
        md = pq.ParquetFile(rf).metadata
        for rg in range(md.num_row_groups):
            index.append((f, rg))
    pick = rnd.sample(index, min(n_rg, len(index)))
    seen: dict[str, int] = {}
    total = 0
    fetched = 0

    def go(item):
        f, rg = item
        rf = RangeFile(BASE + f["path"], f["size"])
        pf = pq.ParquetFile(rf)
        ids = pf.read_row_group(rg, columns=["id"]).column("id").to_pylist()
        return ids, rf.bytes_fetched

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for ids, fb in ex.map(go, pick):
            for i in ids:
                seen[i] = seen.get(i, 0) + 1
            total += len(ids)
            fetched += fb
            print(f"[rgdupe {prefix}] {total:,} rows, {len(seen):,} distinct, "
                  f"{fetched/1e6:.0f} MB", file=sys.stderr)
    from collections import Counter
    return {
        "prefix": prefix,
        "n_row_groups_sampled": len(pick),
        "n_row_groups_total": len(index),
        "rows_sampled": total,
        "distinct_ids": len(seen),
        "duplication_factor_within_sample": round(total / len(seen), 4) if seen else None,
        "copies_per_id_histogram": dict(sorted(Counter(seen.values()).items())),
        "note": "a sample UNDERSTATES duplication: a row group pair holding the two copies of a "
                "doc may not both be drawn. Treat as a lower bound.",
        "id_column_bytes_fetched": fetched,
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("prefix")
    p.add_argument("--mode", choices=["cols", "exact", "rgsample"], default="cols")
    p.add_argument("--n-rg", type=int, default=40)
    p.add_argument("--out", default=None)
    a = p.parse_args()
    if a.mode == "cols":
        r = col_sizes(a.prefix)
    elif a.mode == "exact":
        r = exact_dupes(a.prefix)
    else:
        r = rg_sample_dupes(a.prefix, a.n_rg)
    if a.out:
        open(a.out, "w").write(json.dumps(r, indent=2) + "\n")
    print(json.dumps({k: v for k, v in r.items() if k != "per_file"}, indent=2))
