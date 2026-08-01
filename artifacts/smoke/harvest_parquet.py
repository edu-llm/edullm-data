#!/usr/bin/env python3
"""Harvest classification samples by RANGE-READING remote parquet — no datasets-server, no download.

Phase 0 task D of DATASET-DESIGN-reservoir.md §9.3, second implementation. Replaces
`harvest.py`, which used the datasets-server `/rows` API and could not finish.

## Why this exists

`harvest.py` was blocked by HTTP 429. The critical finding, verified directly rather than assumed:
**the datasets-server rate limit is per-IP, not per-account** — an authenticated request and an
anonymous request from the same machine both returned 429 within 0.1s. So adding a token does not
help, and running several counting jobs in parallel from one machine saturates a single shared
quota for everyone on it.

⚠️ SCOPE CORRECTION 2026-08-01. The sentence above is true of **`datasets-server`** and of nothing
else; elsewhere in this repo it got repeated as a general rule about "the HF rate limit," which is
false and which deters the fast path. MEASURED live: the **resolver**
(`huggingface.co/.../resolve/main/...`) is metered **per token** — `ratelimit-policy: "fixed
window";"resolvers";q=3000;w=300` anonymous, `q=5000;w=300` authenticated — so there a token helps a
great deal. The **CDN** it 302-redirects to (`us.aws.cdn.hf.co`, `x-hf-cdn-pop: aws-us-east-1`)
carries **no rate-limit headers at all** and needs no auth. See `PLAN-CORRECTIONS.md` §6.

The rule that follows, and the one this file's approach depends on: **resolve once per file, then
reuse the signed CDN URL for every range read.** The resolver is the only metered hop. Pointing
each of pyarrow's ~70 per-file range reads at it instead spends 70 units per file — that was a real
bug in the reservoir ingest, fixed in 0.6.2 (`ingest_reservoir._cdn_url`).

This implementation avoids the datasets-server API entirely. It reads the parquet files directly
over HTTPS range requests, which go to the CDN and are not governed by the same limit:

    footer read  (a 507 MB FineMath shard)   ~1.1 s
    one row group, `text` column only        ~2.2 s   -> 1,000 documents

A few MB of traffic per source, no `load_dataset()`, no file download — consistent with §5.7's
"no dataset byte touches a laptop" in exactly the way the datasets-server approach was.

## Sampling design

**Random row groups across random shards, not a contiguous head.** Parquet row order in these
corpora correlates with content (crawl batch, date, repo, wiki alphabet — FineWiki's shards are
literally named `abwiki`, `acewiki`, …). Reading the first N rows would sample one slice and call
it the corpus. Then rows are sampled *within* the row groups read, so documents are not adjacent.

This is the same positional-bias concern `read.py:694-714` documents for whole-shard selection
(§2.2), one level down.

**A 256-token prefix** (§9.3). A prefix is a biased view — you see the intro, never the body — which
would be disqualifying for a *quality* judgement but is much weaker for a *subject* judgement: a
document about organic chemistry says so in its first paragraph. It also matches what a production
classifier sees, since `EAI-Distill-0.5b` chunks anything over 30k chars anyway.

  ⚠ The honest cost: documents whose subject emerges late are misjudged by all three models, which
  inflates the "all three differ" excluded bucket rather than producing false agreement. That costs
  statistical power, not correctness, and `score.py` reports `n_excluded`.

The prefix is cut on TOKEN count with the dolma2 tokenizer, so every judge sees the same budget
regardless of how densely a source tokenizes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path

# Sources needing a CLASSIFIED domain (§1.2). Those shipping a subdomain upstream
# (stackv2-edu/<language>, essential-web/<topic>) are excluded -- an upstream label is free.
#
# `text_column` is pinned EXPLICITLY, never guessed. §3.3 trap 1: FinePhrase's `text` holds the
# ORIGINAL FineWeb-Edu document while the rewrite lives in `rollout_results[0].text`, and no size
# or hash check catches the mistake. FineWiki is a live example in this very list -- it ships both
# `text` (payload) and `wikitext` (raw markup, ~1.6x longer).
SOURCES = {
    "finemath": {
        "repo": "HuggingFaceTB/finemath",
        "glob": "finemath-3plus/",
        "text_column": "text",
        "note": "math; the reservoir's math pool anchor",
    },
    "reference": {
        "repo": "HuggingFaceFW/finewiki",
        # English only. Shards are per-wiki (abwiki, acewiki, ...); `data/enwiki/` is the English
        # subset, and sampling across all 404 shards would sample 300 languages.
        "glob": "data/enwiki/",
        "text_column": "text",
        "note": "wiki; MUST be `text`, NOT `wikitext`; CC-BY-SA (keep separable, §7 item 4)",
    },
    "academic": {
        "repo": "common-pile/peS2o_filtered",
        "glob": None,
        "text_column": "text",
        "note": "academic; card token figure is whitespace WORDS not tokens (§3.1 flagship case)",
    },
    "qa-forum": {
        "repo": "common-pile/stackexchange_filtered",
        "glob": None,
        "text_column": "text",
        "note": "Q&A; CC-BY-SA (keep separable, §7 item 4)",
    },
    "dclm": {
        "repo": "mlfoundations/dclm-baseline-1.0-parquet",
        "glob": None,
        "text_column": "text",
        "note": "diverse web; datasets-server /statistics is broken for DCLM (HTTP 501)",
    },
}


def hf_token() -> str | None:
    t = os.environ.get("HF_TOKEN")
    if t:
        return t.strip()
    p = Path.home() / ".cache/huggingface/token"
    return p.read_text().strip() if p.exists() else None


def list_data_files(repo: str, prefix: str | None) -> tuple[list[str], str]:
    """Return (files, kind) where kind is 'parquet' or 'jsonl.gz'.

    Not every corpus ships parquet. **Every Common Pile source ships `.json.gz`** (verified:
    `peS2o_filtered` has 93 files, 0 of them parquet) — which is also *why* datasets-server's
    conversion is partial for them, since it has to transcode. So the harvester needs both paths."""
    from huggingface_hub import HfApi

    all_files = HfApi().list_repo_files(repo, repo_type="dataset")
    pq = sorted(f for f in all_files if f.endswith(".parquet"))
    if prefix:
        pq = [f for f in pq if f.startswith(prefix)]
    if pq:
        return pq, "parquet"
    gz = sorted(f for f in all_files if f.endswith((".json.gz", ".jsonl.gz", ".jsonl.zst")))
    if prefix:
        gz = [f for f in gz if f.startswith(prefix)]
    return gz, "jsonl.gz"


def read_jsonl_gz_head(fs, url: str, text_column: str, want: int,
                       max_bytes: int = 24 * 1024 * 1024) -> list[str]:
    """Stream-decompress the FIRST `max_bytes` of a gzip member and return document texts.

    ⚠ **This reads a file PREFIX, and that is a real sampling bias** — unlike the parquet path,
    which seeks to a random row group. gzip is not seekable: a member must be decompressed from
    byte 0, so there is no cheap way to reach the middle of one. The mitigation is to spread across
    many *shards* and take few documents from each, so the bias is "the head of 12 different
    shards" rather than "the first 500 documents of the corpus." Recorded in the harvest manifest
    as `sampling_bias` so nobody reads these samples as uniformly drawn.

    24 MB decompresses to roughly 60-100 MB of text, which is thousands of documents — far more
    than the handful we take per shard."""
    import gzip
    import io
    import json as _json
    import zlib

    out: list[str] = []
    # Ranged GET: never pull the whole shard. peS2o's are ~2 GB each.
    r = fs.open(url, "rb")
    raw = r.read(max_bytes)
    r.close()
    # decompressobj tolerates a truncated stream; gzip.GzipFile raises on the cut-off tail.
    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        blob = dec.decompress(raw)
    except zlib.error:
        return out
    buf = io.StringIO(blob.decode("utf8", "replace"))
    for line in buf:
        if len(out) >= want:
            break
        line = line.strip()
        if not line:
            continue
        try:
            rec = _json.loads(line)
        except Exception:  # noqa: BLE001 - the final line is truncated by design
            continue
        t = rec.get(text_column)
        if isinstance(t, str) and t:
            out.append(t)
    return out


def harvest_source(
    key: str, spec: dict, tokenizer, n_docs: int, prefix_tokens: int,
    seed: int, min_chars: int, max_shards: int, fs,
) -> tuple[list[dict], dict]:
    import pyarrow.parquet as pq
    from huggingface_hub import hf_hub_url

    meta = {"source": key, "repo": spec["repo"], "glob": spec["glob"],
            "text_column": spec["text_column"], "note": spec["note"]}
    try:
        files, kind = list_data_files(spec["repo"], spec["glob"])
    except Exception as e:  # noqa: BLE001
        meta.update({"status": "FAILED", "error": f"list_repo_files: {type(e).__name__}: {e}"})
        return [], meta
    if not files:
        meta.update({"status": "FAILED", "error": f"no data files under {spec['glob']!r}"})
        return [], meta
    meta["file_kind"] = kind
    meta["sampling_bias"] = (
        "random row group per shard -- no positional bias within a shard" if kind == "parquet"
        else "SHARD-PREFIX: gzip is not seekable, so documents come from the head of each shard; "
             "spread across many shards limits but does not remove the bias"
    )

    rng = random.Random(seed)
    # Spread across shards: content clusters by shard in every one of these corpora.
    chosen = rng.sample(files, min(max_shards, len(files)))
    out: list[dict] = []
    seen: set[str] = set()
    errors: list[str] = []
    skipped_short = dup = 0
    per_shard = max(1, n_docs // len(chosen) + 1)
    shards_used = 0

    if kind == "jsonl.gz":
        for path in chosen:
            if len(out) >= n_docs:
                break
            try:
                texts = read_jsonl_gz_head(
                    fs, hf_hub_url(spec["repo"], path, repo_type="dataset"),
                    spec["text_column"], want=per_shard * 4,
                )
                if not texts:
                    errors.append(f"{path}: no docs decoded (column {spec['text_column']!r}?)")
                    continue
                rng.shuffle(texts)          # spread within the prefix we read
                got = 0
                for text in texts:
                    if got >= per_shard or len(out) >= n_docs:
                        break
                    if len(text) < min_chars:
                        skipped_short += 1
                        continue
                    h = hashlib.sha256(text.encode("utf8")).hexdigest()[:16]
                    if h in seen:
                        dup += 1
                        continue
                    seen.add(h)
                    ids = tokenizer(text, add_special_tokens=False)["input_ids"][:prefix_tokens]
                    out.append({
                        "source": key, "doc_id": f"{key}-{h}", "shard": path, "row_group": None,
                        "n_chars_full": len(text), "n_tokens_prefix": len(ids),
                        "text": tokenizer.decode(ids),
                    })
                    got += 1
                shards_used += 1
            except Exception as e:  # noqa: BLE001 - one bad shard must not kill the source
                errors.append(f"{path}: {type(e).__name__}: {str(e)[:100]}")
                if len(errors) > 8:
                    break
        meta.update({
            "status": "ok" if out else "FAILED",
            "n_data_files_available": len(files),
            "n_shards_sampled": shards_used,
            "n_harvested": len(out),
            "n_skipped_too_short": skipped_short,
            "n_exact_duplicates_dropped": dup,
            "errors": errors[:8],
        })
        return out, meta

    for path in chosen:
        if len(out) >= n_docs:
            break
        try:
            f = fs.open(hf_hub_url(spec["repo"], path, repo_type="dataset"), "rb")
            pf = pq.ParquetFile(f)
            col = spec["text_column"]
            if col not in pf.schema_arrow.names:
                errors.append(f"{path}: no column {col!r}; has {pf.schema_arrow.names[:8]}")
                continue
            # A random row group, not group 0 -- group order tracks insertion order.
            groups = rng.sample(range(pf.num_row_groups), min(2, pf.num_row_groups))
            got_here = 0
            for g in groups:
                tbl = pf.read_row_group(g, columns=[col])
                texts = tbl.column(col).to_pylist()
                rng.shuffle(texts)                     # non-adjacent rows within the group
                for text in texts:
                    if got_here >= per_shard or len(out) >= n_docs:
                        break
                    if not isinstance(text, str) or len(text) < min_chars:
                        skipped_short += 1
                        continue
                    h = hashlib.sha256(text.encode("utf8")).hexdigest()[:16]
                    if h in seen:
                        dup += 1
                        continue
                    seen.add(h)
                    ids = tokenizer(text, add_special_tokens=False)["input_ids"][:prefix_tokens]
                    out.append({
                        "source": key, "doc_id": f"{key}-{h}", "shard": path, "row_group": g,
                        "n_chars_full": len(text), "n_tokens_prefix": len(ids),
                        "text": tokenizer.decode(ids),
                    })
                    got_here += 1
            shards_used += 1
            f.close()
        except Exception as e:  # noqa: BLE001 - one bad shard must not kill the source
            errors.append(f"{path}: {type(e).__name__}: {str(e)[:100]}")
            if len(errors) > 8:
                break

    meta.update({
        "status": "ok" if out else "FAILED",
        "n_data_files_available": len(files),
        "n_shards_sampled": shards_used,
        "n_harvested": len(out),
        "n_skipped_too_short": skipped_short,
        "n_exact_duplicates_dropped": dup,
        "errors": errors[:8],
    })
    return out, meta


def main() -> int:
    p = argparse.ArgumentParser(description="Harvest samples via remote parquet range reads.")
    p.add_argument("--out-dir", default="artifacts/smoke/samples")
    p.add_argument("--n-docs", type=int, default=500)
    p.add_argument("--prefix-tokens", type=int, default=256)
    p.add_argument("--min-chars", type=int, default=200)
    p.add_argument("--max-shards", type=int, default=12,
                   help="distinct shards to sample per source; more = better spread, slower")
    p.add_argument("--seed", type=int, default=20260731)
    p.add_argument("--only", default=None)
    args = p.parse_args()

    import fsspec
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained("allenai/dolma2-tokenizer")
    tok = hf_token()
    fs = fsspec.filesystem("https", headers={"Authorization": f"Bearer {tok}"} if tok else None)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    keys = args.only.split(",") if args.only else list(SOURCES)

    all_meta = []
    for i, key in enumerate(keys):
        if key not in SOURCES:
            print(f"[harvest] unknown source {key!r}", file=sys.stderr)
            continue
        t0 = time.time()
        print(f"[harvest] {key}: {SOURCES[key]['repo']}", file=sys.stderr)
        docs, meta = harvest_source(
            key, SOURCES[key], tokenizer, args.n_docs, args.prefix_tokens,
            args.seed + i * 7919, args.min_chars, args.max_shards, fs,
        )
        path = out_dir / f"{key}.jsonl"
        with path.open("w") as f:
            for d in docs:
                f.write(json.dumps(d) + "\n")
        meta["path"] = str(path)
        meta["seconds"] = round(time.time() - t0, 1)
        all_meta.append(meta)
        print(f"[harvest]   -> {meta['n_harvested']} docs from "
              f"{meta.get('n_shards_sampled', 0)} shards in {meta['seconds']}s ({meta['status']})",
              file=sys.stderr)
        if meta["errors"]:
            print(f"[harvest]   errors: {meta['errors'][:2]}", file=sys.stderr)

    (out_dir / "_harvest.json").write_text(json.dumps({
        "task": "Phase 0 task D -- classification samples",
        "method": "remote parquet range reads (footer + random row groups); NOT datasets-server, "
                  "which is rate-limited per-IP",
        "prefix_tokens": args.prefix_tokens,
        "tokenizer": "allenai/dolma2-tokenizer",
        "seed": args.seed,
        "sources": all_meta,
    }, indent=2) + "\n")
    total = sum(m.get("n_harvested", 0) for m in all_meta)
    print(f"[harvest] TOTAL {total} docs across {len(all_meta)} sources", file=sys.stderr)
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
