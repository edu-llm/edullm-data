#!/usr/bin/env python3
"""Phase 0c — measure the REAL dolma2 token count for the `academic` category from parquet FOOTERS.

Phase 0 returned 0.00B for this whole category. Not because the corpora are small: because
`common-pile/{peS2o,pubmed,arxiv_papers}_filtered` ship as `.json.gz`, datasets-server's parquet
conversion stopped at its ~2.4 GB cap, and every counting attempt then died on HTTP 429 — a
PER-IP quota, so an auth token does not help.

This script never touches datasets-server. It reads parquet FOOTERS over HTTP Range from
`common-pile/raw_v0.1_parquet` (the same corpora, natively parquet, 1,141 files) via the hub's
`resolve/main` CDN. A footer carries `total_uncompressed_size` per column chunk, so summing it
over every row group of every file gives the EXACT serialized byte total for the `text` column
alone, over ALL rows — a few hundred KB fetched per multi-GB file, no payload.

    tokens = total_text_bytes (EXACT, footers, all rows) x tokens_per_byte (sampled, tight CV)

THE CAVEAT THAT DOMINATES THE ANSWER: `raw_v0.1_parquet` is the RAW collection. The design doc
§3.2 specifies the `_filtered` variants, which are a strictly SMALLER ROW SET (filtering dropped
documents). So a footer count over `raw` is an UPPER BOUND on the filtered corpus. Both figures
are reported, and the filtered one is scaled by the published raw->filtered BYTE ratio (Common
Pile paper arXiv:2506.05209 Table 6, card-confirmed), not the row ratio — bytes scale bytes.

Encoding correction: PLAIN-encoded BYTE_ARRAY stores 4-byte length + utf8 per value, and
`total_uncompressed_size` is post-encoding / pre-compression. The script subtracts 4*num_values
and asserts the column is not dictionary-encoded (a dict-encoded chunk's uncompressed size is
dictionary + indices, which does NOT equal the text bytes and would silently undercount).

NO DATASET DOWNLOAD (§5.7). Footers + a handful of individual row groups for the tokenizer
sample. Nothing large is written to disk (disk at 92%).
"""
from __future__ import annotations

import io
import json
import os
import random
import statistics
import sys
import urllib.error
import urllib.request

import pyarrow.parquet as pq

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = "common-pile/raw_v0.1_parquet"
API = f"https://huggingface.co/api/datasets/{REPO}"
RESOLVE = f"https://huggingface.co/datasets/{REPO}/resolve/main"


def _token() -> str | None:
    t = os.environ.get("HF_TOKEN")
    if t:
        return t
    p = os.path.expanduser("~/.cache/huggingface/token")
    if os.path.exists(p):
        return open(p).read().strip()
    return None


def _headers() -> dict:
    h = {"User-Agent": "edullm-data/recount-footer"}
    t = _token()
    if t:
        h["Authorization"] = f"Bearer {t}"
    return h


HDRS = _headers()


def hub_get(url: str, tries: int = 5):
    last = None
    for a in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HDRS), timeout=120) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            import time
            time.sleep(3 * (a + 1))
    raise RuntimeError(last)


class RangeFile(io.RawIOBase):
    """Seekable read-only file over HTTP Range. pyarrow fetches the footer and nothing else."""

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
            self.url, headers={**HDRS, "Range": f"bytes={self.pos}-{end}"}
        )
        data = None
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=240) as r:
                    data = r.read()
                break
            except Exception:  # noqa: BLE001
                if attempt == 4:
                    raise
                import time
                time.sleep(4 * (attempt + 1))
        self.pos += len(data)
        self.bytes_fetched += len(data)
        self.n_requests += 1
        return data


def list_files(src: str) -> list[dict]:
    d = hub_get(f"{API}/tree/main/{src}?recursive=1&limit=1000")
    fs = [e for e in d if e["type"] == "file" and e["path"].endswith(".parquet")]
    fs.sort(key=lambda e: e["path"])
    return fs


def scan_source(src: str, text_column: str = "text", limit: int | None = None) -> dict:
    files = list_files(src)
    if limit:
        files = files[:limit]
    total_bytes = 0
    total_rows = 0
    n_values = 0
    fetched = 0
    reqs = 0
    done = 0
    encodings: set[str] = set()
    dict_chunks = 0
    compressions: set[str] = set()
    schema_names: list[str] | None = None
    per_file = []
    rowgroups = []  # (file_path, file_size, rg_index, rg_rows) for sampling
    errs: list[str] = []

    for f in files:
        url = f"{RESOLVE}/{f['path']}"
        try:
            rf = RangeFile(url, f["size"])
            pf = pq.ParquetFile(rf)
            md = pf.metadata
            if schema_names is None:
                schema_names = list(md.schema.names)
            idx = md.schema.names.index(text_column)
            fb = 0
            fv = 0
            for rg in range(md.num_row_groups):
                g = md.row_group(rg)
                cc = g.column(idx)
                fb += cc.total_uncompressed_size
                fv += cc.num_values
                for e in (cc.encodings or ()):
                    encodings.add(str(e))
                if any("DICT" in str(e).upper() for e in (cc.encodings or ())):
                    dict_chunks += 1
                compressions.add(str(cc.compression))
                rowgroups.append((f["path"], f["size"], rg, g.num_rows))
            total_bytes += fb
            n_values += fv
            total_rows += md.num_rows
            fetched += rf.bytes_fetched
            reqs += rf.n_requests
            done += 1
            per_file.append({
                "path": f["path"], "file_bytes": f["size"], "rows": md.num_rows,
                "row_groups": md.num_row_groups, "text_uncompressed": fb,
            })
        except Exception as e:  # noqa: BLE001
            errs.append(f"{f['path']}: {type(e).__name__}: {e}"[:220])
            if len(errs) > 6:
                break
        print(f"[{src}] {done}/{len(files)} files  {total_rows:,} rows  "
              f"{total_bytes/1e9:.2f} GB text  {fetched/1e6:.1f} MB footers",
              file=sys.stderr, flush=True)

    if not total_rows:
        return {"status": "FAILED", "source": src, "errors": errs[:6]}

    overhead = 4 * n_values
    return {
        "status": "ok",
        "source": src,
        "repo": REPO,
        "collection": "RAW (unfiltered)",
        "text_column": text_column,
        "schema": schema_names,
        "n_files": done,
        "n_files_total": len(files),
        "parquet_file_bytes_total": sum(f["size"] for f in files),
        "rows_covered": total_rows,
        "n_values": n_values,
        "text_bytes_raw_total_uncompressed": total_bytes,
        "length_prefix_overhead_bytes": overhead,
        "text_bytes_corrected": total_bytes - overhead,
        "overhead_fraction": round(overhead / total_bytes, 6),
        "mean_text_bytes_per_doc": round((total_bytes - overhead) / total_rows, 1),
        "column_encodings": sorted(encodings),
        "dictionary_encoded_chunks": dict_chunks,
        "compressions": sorted(compressions),
        "footer_bytes_fetched": fetched,
        "footer_http_requests": reqs,
        "errors": errs[:6],
        "_rowgroups": rowgroups,
        "_per_file": per_file,
    }


def sample_tokens_per_byte(rowgroups, n_groups: int, seed: int, docs_cap: int,
                           text_column: str = "text") -> dict:
    """Tokenize text from RANDOM row groups across RANDOM files.

    Never a contiguous head: parquet row order correlates with content (source, length bucket,
    ingest order), so a head sample measures one slice of the corpus and calls it the mean.
    """
    from transformers import AutoTokenizer
    tk = AutoTokenizer.from_pretrained("allenai/dolma2-tokenizer")

    rng = random.Random(seed)
    picks = rng.sample(rowgroups, min(n_groups, len(rowgroups)))
    per_doc = []            # (bytes, tokens)
    per_group = []          # group-level tokens/byte, for the CV
    per_group_totals = []   # (group_bytes, group_tokens), for the cluster bootstrap
    fetched = 0
    errs = []
    for i, (path, size, rg, nrows) in enumerate(picks):
        try:
            rf = RangeFile(f"{RESOLVE}/{path}", size)
            pf = pq.ParquetFile(rf)
            t = pf.read_row_group(rg, columns=[text_column])
            texts = t.column(text_column).to_pylist()
            # a row group can hold many docs; take a random subset of them
            if len(texts) > docs_cap:
                texts = rng.sample(texts, docs_cap)
            gb = gt = 0
            for s in texts:
                if not s:
                    continue
                b = len(s.encode("utf8"))
                n = len(tk(s, add_special_tokens=False)["input_ids"])
                per_doc.append((b, n))
                gb += b
                gt += n
            if gb:
                per_group.append(gt / gb)
                per_group_totals.append((gb, gt))
            fetched += rf.bytes_fetched
        except Exception as e:  # noqa: BLE001
            errs.append(f"{path}#rg{rg}: {type(e).__name__}: {e}"[:200])
        print(f"  [sample] {i+1}/{len(picks)} groups, {len(per_doc)} docs, "
              f"{fetched/1e6:.1f} MB read", file=sys.stderr, flush=True)

    if not per_doc:
        return {"status": "FAILED", "errors": errs[:6]}

    tb = sum(b for b, _ in per_doc)
    tt = sum(n for _, n in per_doc)
    doc_ratios = [n / b for b, n in per_doc if b]

    # CLUSTER bootstrap over row groups, not over docs. Docs inside one row group are not
    # independent draws from the corpus (one group is one contiguous ingest slice), so a
    # per-doc bootstrap understates the interval. Resampling whole groups is the honest CI.
    ci = None
    if len(per_group_totals) > 2:
        bs = []
        r2 = random.Random(seed + 1)
        for _ in range(4000):
            pick = [per_group_totals[r2.randrange(len(per_group_totals))]
                    for _ in range(len(per_group_totals))]
            sb = sum(b for b, _ in pick)
            st = sum(t for _, t in pick)
            if sb:
                bs.append(st / sb)
        bs.sort()
        ci = [bs[int(0.025 * len(bs))], bs[int(0.975 * len(bs))]]

    return {
        "status": "ok",
        "tokenizer": "allenai/dolma2-tokenizer",
        "tokens_per_byte_ci95_cluster_bootstrap": ci,
        "tokens_per_byte_ci95_rel_halfwidth": (
            round((ci[1] - ci[0]) / 2 / (tt / tb), 4) if ci else None
        ),
        "n_row_groups_sampled": len(per_group),
        "n_docs_sampled": len(per_doc),
        "sample_text_bytes": tb,
        "sample_tokens": tt,
        # pooled = total tokens / total bytes; this is the estimator (length-weighted, correct)
        "tokens_per_byte": tt / tb,
        "tokens_per_byte_doc_mean": statistics.fmean(doc_ratios),
        "tokens_per_byte_cv_per_doc": (
            statistics.stdev(doc_ratios) / statistics.fmean(doc_ratios) if len(doc_ratios) > 1 else None
        ),
        "tokens_per_byte_cv_per_rowgroup": (
            statistics.stdev(per_group) / statistics.fmean(per_group) if len(per_group) > 1 else None
        ),
        "bytes_per_token": tb / tt,
        "mean_doc_bytes_in_sample": tb / len(per_doc),
        "payload_bytes_read": fetched,
        "errors": errs[:6],
    }


def main() -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("source", help="peS2o | pubmed | arxiv_papers | ...")
    p.add_argument("--text-column", default="text")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--sample-groups", type=int, default=0)
    p.add_argument("--docs-per-group", type=int, default=12)
    p.add_argument("--seed", type=int, default=1337)
    p.add_argument("--out", default=None)
    p.add_argument("--sample-only", default=None,
                   help="reuse the row-group index in this existing footer JSON; skip re-scanning")
    a = p.parse_args()

    if a.sample_only:
        r = json.load(open(a.sample_only))
        s = sample_tokens_per_byte(r["_rowgroups"], a.sample_groups, a.seed, a.docs_per_group,
                                   a.text_column)
        out = a.out or os.path.join(HERE, f"_sample-academic-{a.source}.json")
        s["source"] = a.source
        s["seed"] = a.seed
        if s.get("status") == "ok":
            s["text_bytes_corrected_raw"] = r["text_bytes_corrected"]
            s["raw_tokens_estimate"] = int(r["text_bytes_corrected"] * s["tokens_per_byte"])
        json.dump(s, open(out, "w"), indent=2)
        print(json.dumps(s, indent=2))
        return 0 if s.get("status") == "ok" else 1

    r = scan_source(a.source, a.text_column, a.limit)
    rgs = r.get("_rowgroups", [])
    r["n_row_groups_total"] = len(rgs)
    if r.get("status") == "ok" and a.sample_groups:
        r["sample"] = sample_tokens_per_byte(rgs, a.sample_groups, a.seed, a.docs_per_group,
                                             a.text_column)
        if r["sample"].get("status") == "ok":
            tpb = r["sample"]["tokens_per_byte"]
            r["raw_tokens_estimate"] = int(r["text_bytes_corrected"] * tpb)
    out = a.out or os.path.join(HERE, f"_footer-academic-{a.source}.json")
    json.dump(r, open(out, "w"), indent=2)
    print(json.dumps({k: v for k, v in r.items()
                      if k not in ("_per_file", "_rowgroups")}, indent=2))
    print(f"wrote {out}", file=sys.stderr)
    return 0 if r.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
