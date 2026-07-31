#!/usr/bin/env python3
"""Recover an EXACT whole-split mean bytes-per-doc for the text column from parquet FOOTERS.

Why this exists: `recount.py`'s primary estimator needs `mean_chars_per_doc` over the whole
split, which normally comes from /statistics. For `HuggingFaceFW/finepdfs-edu/eng_Latn` that
endpoint is PERMANENTLY broken -- not rate-limited, not a crashed job:

    Error for ListColumn=fw_edu_scores: StatisticsComputationError(
      "... len(bin_edges=[1, 2]) is 2 but bin_edges[0]=1 != bin_edges[1]=2")

a histogram bug on an unrelated column. Retrying never fixes it. Without it the tool falls back
to `num_rows x sampled mean_tokens_per_doc`, and finepdfs documents are extremely heavy-tailed
(measured CV 4.4, mean 11.7k tokens vs median 1.2k), so that estimate spanned [77B, 625B] -- an
8x range, useless for sizing a pool.

The fix: a parquet file's footer carries `total_uncompressed_size` PER COLUMN CHUNK. Summing that
over every row group of every file in the split gives the exact serialized byte total for the
`text` column alone, over ALL rows -- the same quantity /statistics would have averaged. Combine
with the sampled tokens/byte (CV ~0.13, tight) and the estimate is high-confidence:

    tokens = total_text_bytes(exact, footers) x tokens_per_byte(sampled, tight)

NO DATASET DOWNLOAD (§5.7). This reads only footers via HTTP Range requests -- a few hundred KB
per file against 3 GB files. A lazy range-reading file object is handed to pyarrow so it fetches
the footer and nothing else; payload column chunks are never requested. Nothing is written to
disk (disk is at 92%).

CAVEAT, stated because it changes the number: parquet stores PLAIN-encoded strings as
4-byte-length + utf8 bytes, and `total_uncompressed_size` is the post-encoding, pre-compression
size. So it overcounts by ~4 bytes/value plus any dictionary/RLE overhead. At finepdfs' ~42k
chars/doc that is a ~0.01% effect; the script reports the correction it applied. It is calibrated
against fineweb-edu/sample-100BT, where /statistics DOES work, so the footer method can be
checked against a known-good stats mean before it is trusted on finepdfs.
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from recount import _headers, _get  # noqa: E402

import pyarrow.parquet as pq  # noqa: E402


class RangeFile(io.RawIOBase):
    """Minimal seekable read-only file over HTTP Range. pyarrow reads the footer only."""

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
        req = urllib.request.Request(
            self.url, headers={**_headers(), "Range": f"bytes={self.pos}-{end}"}
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = r.read()
                break
            except Exception:
                if attempt == 3:
                    raise
                import time
                time.sleep(5 * (attempt + 1))
        self.pos += len(data)
        self.bytes_fetched += len(data)
        self.n_requests += 1
        return data


def parquet_urls(dataset: str, config: str) -> tuple[list[dict], bool]:
    d = _get("parquet", {"dataset": dataset, "config": config})
    files = d.get("parquet_files") or []
    return files, bool(d.get("partial"))


def scan(dataset: str, config: str, text_column: str = "text", limit: int | None = None) -> dict:
    files, partial = parquet_urls(dataset, config)
    if not files:
        return {"status": "NO_PARQUET"}
    files = [f for f in files if f.get("split") == "train"] or files
    if limit:
        files = files[:limit]

    total_bytes = 0
    total_rows = 0
    n_values = 0
    fetched = 0
    reqs = 0
    done = 0
    errs: list[str] = []

    def _resolve_leaf(md, want: str, url: str) -> int:
        """Index of the leaf column chunk for `want`, refusing to guess when it is ambiguous.

        ⚠️ THIS REPLACED `md.schema.names.index(want)`, WHICH SILENTLY READ THE WRONG COLUMN.
        `md.schema.names` is a FLAT list of leaf names, so a nested schema can contain the same
        bare name twice and `.index()` returns the FIRST. Measured on
        `HuggingFaceFW/finephrase`: 17 leaves with `'text'` at BOTH index 0 and index 12 —

            col  0: text                                (the ORIGINAL FineWeb-Edu document)
            col 12: rollout_results.list.element.text   (the synthetic rewrite we want)

        so `.index('text')` returned 0 and would have measured the original corpus while
        reporting it as synthetic. That is §3.3's documented trap reached through the *footer*
        path, which nobody had considered — the trap was only ever described for row reads.

        Checked before fixing: the three corpora whose committed numbers came from this tool
        (`finepdfs-edu`, `fineweb-edu`, `finewiki/en`) each contain `text` exactly once, so no
        published figure was wrong. The bug was latent, not active. Fixed anyway, because the
        next nested corpus would have hit it and the failure is silent.

        Matches a FULL `path_in_schema` first (pass `rollout_results.list.element.text` to be
        unambiguous), then falls back to a bare leaf name — but only if it is UNIQUE. Ambiguity
        raises rather than picking one, because picking one is exactly what caused this.

        ⚠️ WHAT THIS DOES *NOT* PROTECT YOU FROM, stated because it is the residual trap. On
        FinePhrase, `text` is a real top-level column, so an exact-path match wins and
        `_resolve_leaf(md, "text")` returns the ORIGINAL document — correctly, because that is
        what you asked for. This function fixes *ambiguity*, not a wrong request. The rewrite
        must be named in full. §3.3's trap is ultimately a caller-intent problem and no schema
        check can close it: both columns are legitimately `text`, and only the caller knows which
        corpus they mean to measure.
        """
        rg0 = md.row_group(0)
        paths = [rg0.column(c).path_in_schema for c in range(rg0.num_columns)]
        if want in paths:
            return paths.index(want)
        hits = [i for i, p in enumerate(paths) if p.rsplit(".", 1)[-1] == want]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise KeyError(f"no leaf column named {want!r}; leaves are {paths}")
        raise KeyError(
            f"{want!r} is AMBIGUOUS in {url.rsplit('/', 1)[-1]} — it matches "
            f"{[paths[i] for i in hits]}. Pass the full path_in_schema. Guessing here is how a "
            f"synthetic-rewrite count silently becomes an original-document count."
        )

    for f in files:
        try:
            rf = RangeFile(f["url"], f["size"])
            pf = pq.ParquetFile(rf)
            md = pf.metadata
            idx = _resolve_leaf(md, text_column, f["url"])
            for rg in range(md.num_row_groups):
                cc = md.row_group(rg).column(idx)
                total_bytes += cc.total_uncompressed_size
                n_values += cc.num_values
            total_rows += md.num_rows
            fetched += rf.bytes_fetched
            reqs += rf.n_requests
            done += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"{f['url'].rsplit('/', 1)[-1]}: {type(e).__name__}: {e}"[:200])
            if len(errs) > 6:
                break
        print(f"[footer] {done}/{len(files)} files, {total_rows:,} rows, "
              f"{total_bytes/1e9:.1f} GB text, {fetched/1e6:.1f} MB footers fetched",
              file=sys.stderr)

    if not total_rows:
        return {"status": "FAILED", "errors": errs[:6]}

    # PLAIN-encoded BYTE_ARRAY = 4-byte length prefix + payload, per value.
    overhead = 4 * n_values
    corrected = total_bytes - overhead
    return {
        "status": "ok",
        "dataset": dataset,
        "config": config,
        "text_column": text_column,
        "n_files_scanned": done,
        "n_files_total": len(files),
        "parquet_listing_partial": partial,
        "rows_covered": total_rows,
        "text_bytes_raw_total_uncompressed": total_bytes,
        "n_values": n_values,
        "length_prefix_overhead_bytes": overhead,
        "text_bytes_corrected": corrected,
        "overhead_fraction": round(overhead / total_bytes, 6) if total_bytes else None,
        "mean_text_bytes_per_doc": round(corrected / total_rows, 1),
        "footer_bytes_fetched": fetched,
        "footer_http_requests": reqs,
        "errors": errs[:6],
    }


def main() -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("dataset")
    p.add_argument("--config", required=True)
    p.add_argument("--text-column", default="text")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--out", default=None)
    a = p.parse_args()
    r = scan(a.dataset, a.config, a.text_column, a.limit)
    t = json.dumps(r, indent=2)
    if a.out:
        open(a.out, "w").write(t + "\n")
    print(t)
    return 0 if r.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
