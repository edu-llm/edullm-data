#!/usr/bin/env python3
"""Exact `text`-column byte totals for common-pile/raw_v0.1_parquet sub-corpora, from FOOTERS ONLY.

Why this file exists and not `_footer_chars.py`: that script sources its file list from
datasets-server's `/parquet` endpoint, which is the SAME per-IP-quota API that 429'd all of
Phase 0. `common-pile/raw_v0.1_parquet` is a plain parquet repo, so its files are addressable
directly on the HF CDN (`/resolve/main/<path>`) and the tree API gives their sizes. That path
touches no datasets-server quota at all.

Method (identical arithmetic to _footer_chars.py):
    text_bytes = sum over every row group of every file of
                 row_group.column(text_idx).total_uncompressed_size
    minus 4 bytes per value (PLAIN BYTE_ARRAY length prefix)

NO DATASET DOWNLOAD (§5.7): a lazy Range-reading file object is handed to pyarrow, which fetches
the 8-byte trailer + the footer thrift blob and nothing else. Payload column chunks are never
requested. Nothing large is written (disk at 92%).
"""
from __future__ import annotations

import concurrent.futures as cf
import io
import json
import os
import sys
import time
import urllib.request

import pyarrow.parquet as pq

REPO = "common-pile/raw_v0.1_parquet"
BASE = f"https://huggingface.co/datasets/{REPO}/resolve/main/"
UA = {"User-Agent": "edullm-data/recount-footer"}


def _tok() -> str | None:
    t = os.environ.get("HF_TOKEN")
    if t:
        return t
    p = os.path.expanduser("~/.cache/huggingface/token")
    if os.path.exists(p):
        v = open(p).read().strip()
        return v or None
    return None


def _headers() -> dict:
    h = dict(UA)
    t = _tok()
    if t:
        h["Authorization"] = f"Bearer {t}"
    return h


class RangeFile(io.RawIOBase):
    def __init__(self, url: str, size: int):
        self.url, self.size, self.pos = url, size, 0
        self.bytes_fetched = 0
        self.n_requests = 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off)
        return self.pos

    def tell(self):
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        end = self.pos + n - 1
        req = urllib.request.Request(self.url, headers={**_headers(), "Range": f"bytes={self.pos}-{end}"})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = r.read()
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(3 * (attempt + 1))
        self.pos += len(data)
        self.bytes_fetched += len(data)
        self.n_requests += 1
        return data


def tree(prefix: str) -> list[dict]:
    """List files under a prefix with their real sizes (expand=true gives lfs.size).

    NOTE `limit` caps at 100 on this endpoint -- limit=1000 returns HTTP 400, so pagination
    via the Link header is mandatory for prefixes like stackexchange (40) and github_archive (72).
    """
    import re
    out, cursor = [], None
    seen = set()
    while True:
        url = (
            f"https://huggingface.co/api/datasets/{REPO}/tree/main/{prefix}"
            f"?expand=true&limit=100" + (f"&cursor={cursor}" if cursor else "")
        )
        req = urllib.request.Request(url, headers=_headers())
        with urllib.request.urlopen(req, timeout=120) as r:
            page = json.load(r)
            link = r.headers.get("Link", "")
        for e in page:
            if e.get("type") != "file" or not e["path"].endswith(".parquet"):
                continue
            if e["path"] in seen:
                continue
            seen.add(e["path"])
            sz = (e.get("lfs") or {}).get("size") or e.get("size")
            out.append({"path": e["path"], "size": sz})
        m = re.search(r'cursor=([^&>]+)', link) if 'rel="next"' in link else None
        cursor = m.group(1) if m else None
        if not cursor:
            break
    return sorted(out, key=lambda x: x["path"])


def one_file(f: dict, text_column: str) -> dict:
    rf = RangeFile(BASE + f["path"], f["size"])
    pf = pq.ParquetFile(rf)
    md = pf.metadata
    names = md.schema.names
    idx = names.index(text_column)
    tb = 0
    nv = 0
    comp = 0
    for rg in range(md.num_row_groups):
        cc = md.row_group(rg).column(idx)
        tb += cc.total_uncompressed_size
        comp += cc.total_compressed_size
        nv += cc.num_values
    return {
        "path": f["path"],
        "file_bytes": f["size"],
        "rows": md.num_rows,
        "row_groups": md.num_row_groups,
        "text_uncompressed": tb,
        "text_compressed": comp,
        "n_values": nv,
        "schema": names,
        "footer_bytes": rf.bytes_fetched,
        "encodings": sorted({str(e) for rg in range(min(md.num_row_groups, 1))
                             for e in md.row_group(rg).column(idx).encodings}),
    }


def scan(prefix: str, text_column: str = "text", workers: int = 8, limit=None) -> dict:
    files = tree(prefix)
    if limit:
        files = files[:limit]
    res, errs = [], []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one_file, f, text_column): f for f in files}
        for i, fu in enumerate(cf.as_completed(futs), 1):
            try:
                res.append(fu.result())
            except Exception as e:  # noqa: BLE001
                errs.append(f"{futs[fu]['path']}: {type(e).__name__}: {e}"[:250])
            if i % 10 == 0 or i == len(futs):
                print(f"[{prefix}] {i}/{len(futs)} files", file=sys.stderr)
    tb = sum(r["text_uncompressed"] for r in res)
    nv = sum(r["n_values"] for r in res)
    rows = sum(r["rows"] for r in res)
    overhead = 4 * nv
    return {
        "status": "ok" if res and not errs else ("partial" if res else "FAILED"),
        "repo": REPO,
        "prefix": prefix,
        "text_column": text_column,
        "schema": res[0]["schema"] if res else None,
        "encodings_first_rg": res[0]["encodings"] if res else None,
        "n_files_scanned": len(res),
        "n_files_total": len(files),
        "rows": rows,
        "row_groups": sum(r["row_groups"] for r in res),
        "text_bytes_raw_total_uncompressed": tb,
        "n_values": nv,
        "length_prefix_overhead_bytes": overhead,
        "text_bytes_corrected": tb - overhead,
        "overhead_fraction": round(overhead / tb, 6) if tb else None,
        "mean_text_bytes_per_doc": round((tb - overhead) / rows, 1) if rows else None,
        "repo_file_bytes": sum(r["file_bytes"] for r in res),
        "footer_bytes_fetched": sum(r["footer_bytes"] for r in res),
        "errors": errs[:8],
        "per_file": [{k: r[k] for k in ("path", "rows", "text_uncompressed", "n_values", "file_bytes")}
                     for r in sorted(res, key=lambda x: x["path"])],
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("prefix")
    p.add_argument("--text-column", default="text")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--out", default=None)
    a = p.parse_args()
    r = scan(a.prefix, a.text_column, a.workers, a.limit)
    if a.out:
        open(a.out, "w").write(json.dumps(r, indent=2) + "\n")
    slim = {k: v for k, v in r.items() if k != "per_file"}
    print(json.dumps(slim, indent=2))
    raise SystemExit(0 if r["status"] == "ok" else 1)
