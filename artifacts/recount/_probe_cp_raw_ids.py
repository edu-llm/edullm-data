#!/usr/bin/env python3
"""Probe WHICH source directory common-pile/raw_v0.1_parquet actually consolidated.

Motivation: footer byte totals for `ubuntu_irc` and `github_archive` overshoot their own RAW
dataset cards by 2.1x and 4.5x, while `stackexchange` matches its card to 1.2%. The two that
overshoot are exactly the two raw repos that ship TWO document trees:

    common-pile/ubuntu_irc      ->  raw/documents/ (8 files)  AND  v0/documents/ (8 files)
    common-pile/github_archive  ->  gharchive/raw/documents/ (2000)  AND  gharchive/v0/documents/ (2000)
    common-pile/stackexchange   ->  <site>/documents/ only (one tree)

So the hypothesis is that the parquet consolidation swept BOTH trees (or swept the pre-`v0`
`raw/` tree) for those corpora, making its row set a superset of what the card describes.

This reads ONLY the tiny `id`/`source` columns of a few row groups -- no `text` payload. It
checks for duplicate ids (the signature of two trees concatenated) and prints id shapes so the
provenance is visible.
"""
from __future__ import annotations

import json
import random
import sys

import pyarrow.parquet as pq

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from _footer_cp_raw import BASE, RangeFile, tree  # noqa: E402


def probe(prefix: str, n_files: int = 3, seed: int = 0) -> dict:
    files = tree(prefix)
    rnd = random.Random(seed)
    pick = files if len(files) <= n_files else (
        [files[0], files[len(files) // 2], files[-1]] if n_files == 3
        else rnd.sample(files, n_files)
    )
    out = {"prefix": prefix, "n_files_total": len(files), "files": []}
    all_ids: dict[str, str] = {}
    dupes = 0
    for f in pick:
        rf = RangeFile(BASE + f["path"], f["size"])
        pf = pq.ParquetFile(rf)
        md = pf.metadata
        rg = 0
        t = pf.read_row_group(rg, columns=["id", "source"])
        ids = t.column("id").to_pylist()
        srcs = t.column("source").to_pylist()
        for i in ids:
            if i in all_ids and all_ids[i] != f["path"]:
                dupes += 1
            all_ids[i] = f["path"]
        out["files"].append({
            "path": f["path"],
            "rows": md.num_rows,
            "row_groups": md.num_row_groups,
            "rg0_rows": len(ids),
            "sources_seen": sorted(set(srcs))[:8],
            "n_distinct_sources_rg0": len(set(srcs)),
            "id_samples": ids[:4],
            "id_distinct_in_rg0": len(set(ids)),
            "bytes_fetched_mb": round(rf.bytes_fetched / 1e6, 1),
        })
        print(f"[probe {prefix}] {f['path']}: rg0 {len(ids)} rows, "
              f"{round(rf.bytes_fetched/1e6,1)} MB fetched", file=sys.stderr)
    out["cross_file_duplicate_ids"] = dupes
    return out


if __name__ == "__main__":
    res = {p: probe(p) for p in sys.argv[1:]}
    print(json.dumps(res, indent=2))
