#!/usr/bin/env python3
"""W2-DCLM: tree enumeration + parquet footer schema + id sampling for the DCLM repos.

NO dataset download. Tree API (cursor-paginated) + HTTP Range footer reads only.
NEVER touches /filter (fabricates zeros) and avoids /rows (429 quota, shared per-IP).
Adapted from artifacts/recount/_fp_footer_leaf.py's proven RangeFile.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

import pyarrow.parquet as pq


def headers() -> dict:
    h = {"User-Agent": "edullm-data/recount"}
    tok = os.environ.get("HF_TOKEN")
    if not tok:
        p = os.path.expanduser("~/.cache/huggingface/token")
        if os.path.exists(p):
            tok = open(p).read().strip()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


H = headers()
N_429 = 0


def get(url: str, extra: dict | None = None, tries: int = 6):
    global N_429
    req = urllib.request.Request(url, headers={**H, **(extra or {})})
    for a in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                N_429 += 1
                sys.stderr.write(f"429 (#{N_429}) backing off {8*(a+1)}s :: {url[:110]}\n")
                time.sleep(8 * (a + 1))
                continue
            raise
        except Exception:
            if a == tries - 1:
                raise
            time.sleep(4 * (a + 1))
    raise RuntimeError(f"exhausted retries {url}")


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
        data, _ = get(self.url, {"Range": f"bytes={self.pos}-{self.pos + n - 1}"})
        self.pos += len(data)
        self.bytes_fetched += len(data)
        self.n_requests += 1
        return data


def tree(repo: str, rev: str, path: str = "", recursive: bool = True):
    """Cursor-paginated tree API. Yields every entry dict."""
    base = f"https://huggingface.co/api/datasets/{repo}/tree/{rev}"
    url = base + (f"/{path}" if path else "")
    url += "?recursive=1&expand=1" if recursive else "?expand=1"
    n_pages = 0
    while url:
        body, hdrs = get(url)
        for e in json.loads(body):
            yield e
        n_pages += 1
        link = hdrs.get("Link") or hdrs.get("link") or ""
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if m else None
    sys.stderr.write(f"  tree {repo}@{rev[:8]}/{path or '.'}: {n_pages} pages\n")


def resolve_url(repo: str, rev: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/{rev}/{path}"


def footer_schema(repo: str, rev: str, path: str, size: int) -> dict:
    rf = RangeFile(resolve_url(repo, rev, path), size)
    pf = pq.ParquetFile(rf)
    md = pf.metadata
    sch = pf.schema_arrow
    leaves = []
    g0 = md.row_group(0)
    for i in range(g0.num_columns):
        cc = g0.column(i)
        leaves.append(
            {
                "path_in_schema": cc.path_in_schema,
                "physical_type": str(cc.physical_type),
                "rg0_uncompressed": cc.total_uncompressed_size,
                "rg0_values": cc.num_values,
            }
        )
    return {
        "repo": repo,
        "revision": rev,
        "file": path,
        "file_bytes": size,
        "num_rows": md.num_rows,
        "num_row_groups": md.num_row_groups,
        "created_by": md.created_by,
        "arrow_schema": [{"name": f.name, "type": str(f.type)} for f in sch],
        "leaves": leaves,
        "footer_bytes_fetched": rf.bytes_fetched,
        "footer_requests": rf.n_requests,
    }


def read_cols(repo: str, rev: str, path: str, size: int, cols: list[str], rg: int = 0):
    rf = RangeFile(resolve_url(repo, rev, path), size)
    pf = pq.ParquetFile(rf)
    t = pf.read_row_group(rg, columns=cols)
    return t, rf.bytes_fetched


if __name__ == "__main__":
    print("library module; import from a driver")
