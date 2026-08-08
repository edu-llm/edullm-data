#!/usr/bin/env python3
"""Count FinePhrase's REWRITE tokens from parquet footers at the NESTED LEAF level.

Phase 0c, synthetic (rephrased). Supersedes `recount_finephrase.py`'s HTTP path, which went
through datasets-server /rows -- unusable here: /size, /rows and /info all return HTTP 500 for
config `faq`, and the /rows quota is per-IP and was already exhausted once by parallel agents.

## Why the naive footer read overcounts, and why this one does not

`_footer_chars.py` sums `total_uncompressed_size` for a TOP-LEVEL column. FinePhrase's payload
is `rollout_results[0].text`, nested inside a list-of-structs, and `rollout_results` as a whole
also carries `finish_reason` and a 4-field `usage` struct. Summing "the rollout_results column"
therefore counts three fields we do not want.

MEASURED: pyarrow exposes parquet column chunks at LEAF granularity, and the leaves carry
dotted `path_in_schema`. For faq/000_00000_0.parquet, row group 0:

    rollout_results.list.element.finish_reason                 78 B
    rollout_results.list.element.text                  1,769,213 B   <- the payload
    rollout_results.list.element.usage.completion_tokens    5,688 B
    rollout_results.list.element.usage.prompt_tokens        7,280 B
    rollout_results.list.element.usage.prompt_tokens_details   47 B
    rollout_results.list.element.usage.total_tokens         7,520 B

So NO ratio correction is needed: we select the one leaf by exact path and get the exact
serialized byte total for the rewrite alone. The sibling leaves are 1.6% of the group -- had we
summed the parent we would have overcounted by that much, plus counted `text` if we had guessed
the wrong column entirely (the 27x trap).

## The two corrections that remain, and how each is validated

1. PLAIN BYTE_ARRAY stores 4-byte length + utf8 payload, so subtract 4 x num_values.
2. A NESTED leaf's pages also carry repetition/definition levels, which `total_uncompressed_size`
   includes. For a list that is always length 1 these RLE-compress to almost nothing, but that is
   a claim, not a fact -- so `--calibrate` READS a row group and compares
   `sum(len(rewrite.encode('utf8')))` against that same row group's footer figure. The residual
   is reported as `footer_overhead_fraction` and applied as a correction, so the final byte total
   is anchored on real decoded bytes rather than on an assumption about parquet internals.

## Scaling from a file sample to the config

Every config has ~6.8k files and a footer is ~680 KB, so scanning all 27,104 would move 18 GB.
Instead: scan a random sample of files, then scale by the EXACT total parquet bytes per config
from the Hub tree API (which agrees with /size's `num_bytes_parquet_files` to the byte for the
three configs /size can serve, and is the only route for `faq`). The scaled quantity is
`leaf_bytes / parquet_file_bytes`, a compression-ratio-like quantity that is far tighter than
anything per-document; its across-file CV is reported so the extrapolation is checkable.

NO DATASET DOWNLOAD (§5.7): HTTP Range footer reads, plus a handful of individual row groups
for tokenization/calibration. Nothing is written to disk beyond a few KB of JSON.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import io
import json
import os
import random
import statistics
import sys
import time
import urllib.request

import pyarrow.parquet as pq

REPO = "HuggingFaceFW/finephrase"
LEAF_REWRITE = "rollout_results.list.element.text"
LEAF_FINISH = "rollout_results.list.element.finish_reason"
LEAF_COMPL = "rollout_results.list.element.usage.completion_tokens"
COL_ORIGINAL = "text"

_TOK = None


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


class RangeFile(io.RawIOBase):
    """Seekable read-only file over HTTP Range. pyarrow fetches only what it asks for."""

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
        req = urllib.request.Request(
            self.url, headers={**H, "Range": f"bytes={self.pos}-{self.pos + n - 1}"}
        )
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    data = r.read()
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(4 * (attempt + 1))
        self.pos += len(data)
        self.bytes_fetched += len(data)
        self.n_requests += 1
        return data


def url_for(path: str) -> str:
    return f"https://huggingface.co/datasets/{REPO}/resolve/main/{path}"


def scan_footer(entry: dict) -> dict:
    """Read ONE file's footer; return exact leaf byte totals. No payload page is fetched."""
    rf = RangeFile(url_for(entry["path"]), entry["size"])
    md = pq.ParquetFile(rf).metadata
    acc = {LEAF_REWRITE: 0, COL_ORIGINAL: 0, LEAF_FINISH: 0, LEAF_COMPL: 0}
    vals = {k: 0 for k in acc}
    parent = 0  # whole rollout_results subtree -- to quantify the naive overcount
    for rg in range(md.num_row_groups):
        g = md.row_group(rg)
        for i in range(g.num_columns):
            cc = g.column(i)
            p = cc.path_in_schema
            if p.startswith("rollout_results."):
                parent += cc.total_uncompressed_size
            if p in acc:
                acc[p] += cc.total_uncompressed_size
                vals[p] += cc.num_values
    return {
        "path": entry["path"],
        "file_bytes": entry["size"],
        "rows": md.num_rows,
        "row_groups": md.num_row_groups,
        "rewrite_bytes": acc[LEAF_REWRITE],
        "rewrite_values": vals[LEAF_REWRITE],
        "original_bytes": acc[COL_ORIGINAL],
        "original_values": vals[COL_ORIGINAL],
        "rollout_parent_bytes": parent,
        "footer_bytes_fetched": rf.bytes_fetched,
        "footer_requests": rf.n_requests,
    }


def tokenizer():
    global _TOK
    if _TOK is None:
        from transformers import AutoTokenizer
        _TOK = AutoTokenizer.from_pretrained("allenai/dolma2-tokenizer")
    return _TOK


def read_row_group(entry: dict, want_original: bool, rng: random.Random) -> dict:
    """Fetch ONE random row group. Reads the rewrite leaf (+ optionally the original column)."""
    rf = RangeFile(url_for(entry["path"]), entry["size"])
    pf = pq.ParquetFile(rf)
    md = pf.metadata
    rg = rng.randrange(md.num_row_groups)
    cols = ["rollout_results"] + (["text", "token_count"] if want_original else [])
    tbl = pf.read_row_group(rg, columns=cols)
    rr = tbl.column("rollout_results").to_pylist()
    rewrites, finish, compl = [], [], []
    for lst in rr:
        if not lst:
            rewrites.append(None)
            continue
        e = lst[0]
        rewrites.append(e.get("text"))
        finish.append(e.get("finish_reason"))
        u = e.get("usage") or {}
        compl.append(u.get("completion_tokens"))
    out = {
        "path": entry["path"], "row_group": rg, "n_rows": tbl.num_rows,
        "rewrites": rewrites, "finish": finish, "completion_tokens": compl,
        "n_rollouts": [len(x) if x else 0 for x in rr],
        # footer figure for THIS row group -- the calibration anchor
        "footer_rewrite_bytes": md.row_group(rg).column(
            [md.row_group(rg).column(i).path_in_schema
             for i in range(md.row_group(rg).num_columns)].index(LEAF_REWRITE)
        ).total_uncompressed_size,
        "footer_rewrite_values": md.row_group(rg).column(
            [md.row_group(rg).column(i).path_in_schema
             for i in range(md.row_group(rg).num_columns)].index(LEAF_REWRITE)
        ).num_values,
        "bytes_fetched": rf.bytes_fetched,
    }
    if want_original:
        out["originals"] = tbl.column("text").to_pylist()
        out["upstream_token_count"] = tbl.column("token_count").to_pylist()
        idx = [md.row_group(rg).column(i).path_in_schema
               for i in range(md.row_group(rg).num_columns)].index(COL_ORIGINAL)
        out["footer_original_bytes"] = md.row_group(rg).column(idx).total_uncompressed_size
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--tree", default=None, help="cached tree json; else fetched")
    p.add_argument("--n-files", type=int, default=120, help="files whose footers are scanned")
    p.add_argument("--n-rowgroups", type=int, default=0, help="row groups to read+tokenize")
    p.add_argument("--n-rowgroups-paired", type=int, default=0,
                   help="of those, how many also read the ORIGINAL text column")
    p.add_argument("--workers", type=int, default=10)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", required=True)
    a = p.parse_args()

    tree = json.load(open(a.tree or f"/tmp/fp/tree_{a.config}.json"))
    tree = [t for t in tree if t["path"].endswith(".parquet")]
    total_parquet_bytes = sum(t["size"] for t in tree)
    rng = random.Random(a.seed)
    sample = rng.sample(tree, min(a.n_files, len(tree)))

    res: dict = {
        "config": a.config, "n_files_in_config": len(tree),
        "total_parquet_bytes_exact": total_parquet_bytes,
        "leaf_path": LEAF_REWRITE, "seed": a.seed,
    }

    rows: list[dict] = []
    errs: list[str] = []
    t0 = time.time()
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(scan_footer, e): e for e in sample}
        for i, f in enumerate(cf.as_completed(futs)):
            try:
                rows.append(f.result())
            except Exception as e:  # noqa: BLE001
                errs.append(f"{futs[f]['path']}: {type(e).__name__}: {e}"[:180])
            if (i + 1) % 20 == 0:
                print(f"[{a.config}] footers {i+1}/{len(sample)} "
                      f"({time.time()-t0:.0f}s)", file=sys.stderr)
    if not rows:
        res["status"] = "FAILED"
        res["errors"] = errs[:8]
        open(a.out, "w").write(json.dumps(res, indent=2) + "\n")
        return 1

    sp = sum(r["file_bytes"] for r in rows)
    srw = sum(r["rewrite_bytes"] for r in rows)
    sor = sum(r["original_bytes"] for r in rows)
    spar = sum(r["rollout_parent_bytes"] for r in rows)
    srows = sum(r["rows"] for r in rows)
    nval = sum(r["rewrite_values"] for r in rows)
    scale = total_parquet_bytes / sp
    per_file_ratio = [r["rewrite_bytes"] / r["file_bytes"] for r in rows]
    res.update({
        "status": "ok",
        "n_files_scanned": len(rows),
        "footer_mb_fetched": round(sum(r["footer_bytes_fetched"] for r in rows) / 1e6, 1),
        "footer_scan_seconds": round(time.time() - t0, 1),
        "sample_parquet_bytes": sp,
        "sample_rows": srows,
        "sample_rewrite_leaf_bytes_raw": srw,
        "sample_original_bytes_raw": sor,
        "sample_rollout_parent_bytes": spar,
        "naive_parent_overcount_fraction": round(spar / srw - 1, 5),
        "rewrite_values": nval,
        "length_prefix_overhead_bytes": 4 * nval,
        "extrapolation_scale": round(scale, 5),
        "rewrite_bytes_per_parquet_byte": round(srw / sp, 6),
        "rewrite_bytes_per_parquet_byte_cv": round(
            statistics.stdev(per_file_ratio) / statistics.fmean(per_file_ratio), 5),
        "est_config_rows": int(srows * scale),
        "est_config_rewrite_bytes_raw": int(srw * scale),
        "est_config_original_bytes_raw": int(sor * scale),
        "footer_rewrite_over_original_bytes": round(srw / sor, 5),
        "mean_rewrite_bytes_per_row_footer": round((srw - 4 * nval) / srows, 2),
        "mean_original_bytes_per_row_footer": round((sor - 4 * srows) / srows, 2),
        "errors": errs[:8],
    })
    open(a.out, "w").write(json.dumps(res, indent=2) + "\n")
    print(json.dumps({k: v for k, v in res.items() if k != "errors"}, indent=2))

    # ---- row-group reads: calibration + tokenization ----
    if a.n_rowgroups:
        tok = tokenizer()
        rg_sample = rng.sample(tree, min(a.n_rowgroups, len(tree)))
        rew_tok, rew_bytes, rew_chars = [], [], []
        compl_tok, finish_counts = [], {}
        n_rollouts_mm = [10**9, 0]
        cal = []          # (decoded utf8 bytes, footer bytes, n_values) per row group
        paired = []       # (rewrite_tokens, orig_tokens, rewrite_bytes, orig_bytes)
        upstream = []
        short_examples: list[dict] = []
        empty = 0
        tb = 0
        for j, e in enumerate(rg_sample):
            want_orig = j < a.n_rowgroups_paired
            try:
                d = read_row_group(e, want_orig, rng)
            except Exception as ex:  # noqa: BLE001
                errs.append(f"rg {e['path']}: {type(ex).__name__}: {ex}"[:180])
                continue
            tb += d["bytes_fetched"]
            n_rollouts_mm = [min(n_rollouts_mm, default=0) if False else
                             min(n_rollouts_mm[0], min(d["n_rollouts"])),
                             max(n_rollouts_mm[1], max(d["n_rollouts"]))]
            for fr in d["finish"]:
                finish_counts[str(fr)] = finish_counts.get(str(fr), 0) + 1
            dec = 0
            enc_all = []
            for t in d["rewrites"]:
                if not isinstance(t, str) or not t:
                    empty += 1
                    enc_all.append(None)
                    continue
                b = t.encode("utf8")
                dec += len(b)
                enc_all.append(t)
            ids = tok([t for t in enc_all if t is not None], add_special_tokens=False)["input_ids"]
            k = 0
            for t in enc_all:
                if t is None:
                    continue
                n = len(ids[k]); k += 1
                rew_tok.append(n)
                rew_bytes.append(len(t.encode("utf8")))
                rew_chars.append(len(t))
                if n < 200 and len(short_examples) < 40:
                    short_examples.append({"dolma2_tokens": n, "chars": len(t), "text": t[:160]})
            for c in d["completion_tokens"]:
                if isinstance(c, (int, float)):
                    compl_tok.append(int(c))
            cal.append({"path": d["path"], "row_group": d["row_group"],
                        "decoded_utf8_bytes": dec,
                        "footer_bytes": d["footer_rewrite_bytes"],
                        "n_values": d["footer_rewrite_values"]})
            if want_orig:
                oids = tok([o for o in d["originals"] if isinstance(o, str) and o],
                           add_special_tokens=False)["input_ids"]
                m = 0
                for idx2, o in enumerate(d["originals"]):
                    if not (isinstance(o, str) and o):
                        continue
                    ot = len(oids[m]); m += 1
                    r = d["rewrites"][idx2]
                    if isinstance(r, str) and r:
                        paired.append((len(tok(r, add_special_tokens=False)["input_ids"]), ot,
                                       len(r.encode("utf8")), len(o.encode("utf8"))))
                    u = d["upstream_token_count"][idx2]
                    if isinstance(u, (int, float)):
                        upstream.append((ot, int(u)))
            print(f"[{a.config}] row groups {j+1}/{len(rg_sample)}: "
                  f"{len(rew_tok)} rewrites, {tb/1e6:.0f} MB", file=sys.stderr)

        if rew_tok:
            n = len(rew_tok)
            tpb = sum(rew_tok) / sum(rew_bytes)
            per_doc_tpb = [t / b for t, b in zip(rew_tok, rew_bytes) if b]
            cal_dec = sum(c["decoded_utf8_bytes"] for c in cal)
            cal_foot = sum(c["footer_bytes"] for c in cal)
            cal_val = sum(c["n_values"] for c in cal)
            # footer, after the 4-byte length prefix, vs real decoded utf8 bytes
            resid = (cal_foot - 4 * cal_val) / cal_dec - 1
            res["tokenization"] = {
                "n_row_groups_read": len(cal),
                "n_rewrites_tokenized": n,
                "n_empty_rewrite": empty,
                "payload_mb_fetched": round(tb / 1e6, 1),
                "rollouts_per_row_min_max": n_rollouts_mm,
                "finish_reasons": finish_counts,
                "tokens_per_byte": round(tpb, 6),
                "tokens_per_byte_cv_per_doc": round(
                    statistics.stdev(per_doc_tpb) / statistics.fmean(per_doc_tpb), 4),
                "tokens_per_char": round(sum(rew_tok) / sum(rew_chars), 6),
                "mean_tokens_per_doc": round(statistics.fmean(rew_tok), 2),
                "median_tokens_per_doc": statistics.median(rew_tok),
                "cv_tokens_per_doc": round(statistics.stdev(rew_tok) / statistics.fmean(rew_tok), 4),
                "max_tokens_seen": max(rew_tok),
                "mean_rewrite_bytes_per_doc_decoded": round(statistics.fmean(rew_bytes), 2),
                "calibration": {
                    "decoded_utf8_bytes": cal_dec,
                    "footer_bytes_minus_length_prefix": cal_foot - 4 * cal_val,
                    "footer_overhead_fraction": round(resid, 6),
                    "note": ("footer total_uncompressed_size for a NESTED leaf, minus 4 bytes per "
                             "value, vs sum(len(text.encode('utf8'))) on the SAME row groups. "
                             "Residual = repetition/definition levels + page headers."),
                },
                "percentiles": {str(q): statistics.quantiles(rew_tok, n=100)[q - 1]
                                for q in (1, 5, 10, 25, 50, 75, 90, 95, 99)},
                "short_buckets": {f"under_{t}": {"n": sum(1 for x in rew_tok if x < t),
                                                 "frac": round(sum(1 for x in rew_tok if x < t) / n, 5)}
                                  for t in (16, 32, 50, 100, 200, 500)},
                "tokens_retained_if_filter_ge_50": round(
                    sum(x for x in rew_tok if x >= 50) / sum(rew_tok), 5),
                "tokens_retained_if_filter_ge_200": round(
                    sum(x for x in rew_tok if x >= 200) / sum(rew_tok), 5),
                "docs_retained_if_filter_ge_50": round(
                    sum(1 for x in rew_tok if x >= 50) / n, 5),
                "short_examples": sorted(short_examples, key=lambda d: d["dolma2_tokens"])[:12],
            }
            if compl_tok:
                res["tokenization"]["mean_completion_tokens_generator"] = round(
                    statistics.fmean(compl_tok), 2)
                res["tokenization"]["dolma2_per_completion_token"] = round(
                    sum(rew_tok) / sum(compl_tok), 5)
            if paired:
                rt = sum(x[0] for x in paired); ot = sum(x[1] for x in paired)
                rb = sum(x[2] for x in paired); ob = sum(x[3] for x in paired)
                res["tokenization"]["paired"] = {
                    "n_pairs": len(paired),
                    "rewrite_over_original_tokens": round(rt / ot, 5),
                    "rewrite_over_original_bytes": round(rb / ob, 5),
                    "median_of_per_doc_token_ratios": round(statistics.median(
                        [a2 / b2 for a2, b2, _, _ in paired if b2]), 5),
                    "original_mean_tokens_per_doc": round(ot / len(paired), 2),
                    "original_tokens_per_byte": round(ot / ob, 6),
                }
                if upstream:
                    res["tokenization"]["paired"]["dolma2_over_fineweb_edu_token_count"] = round(
                        sum(x[0] for x in upstream) / sum(x[1] for x in upstream), 5)

            # ---- FINAL ESTIMATE ----
            corr = 1.0 / (1.0 + resid)
            bytes_exact = (res["est_config_rewrite_bytes_raw"]
                           - 4 * int(res["rewrite_values"] * res["extrapolation_scale"]))
            res["est_config_rewrite_utf8_bytes"] = int(bytes_exact * corr)
            res["est_config_rewrite_tokens"] = int(bytes_exact * corr * tpb)
            res["est_via_rows_x_mean_tokens"] = int(res["est_config_rows"]
                                                    * statistics.fmean(rew_tok))
        res["errors"] = errs[:10]
        open(a.out, "w").write(json.dumps(res, indent=2) + "\n")
        print(json.dumps(res.get("tokenization", {}), indent=2)[:3000])
    print(f"[{a.config}] wrote {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
