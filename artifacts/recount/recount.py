#!/usr/bin/env python3
"""Estimate a HF dataset's token count UNDER THE DOLMA2 TOKENIZER.

Phase 0, task A of DATASET-DESIGN-reservoir.md §9.3. The problem this solves is §3.1:
published token counts are not comparable across corpora — of ~35 surveyed only ~10 name
their tokenizer, AI2 and NVIDIA name none, and peS2o's "47.37B" is *whitespace words*
(OLMo counts the same corpus as 58.6B). Any sizing table built from card figures is fiction,
and §2.1's pool sizes are load-bearing on these numbers.

NO DATASET DOWNLOAD. Everything here is a datasets-server API call plus local tokenization of
a few hundred sampled documents — a few MB of traffic against corpora in the hundreds of GB.
These are metadata-scale reads, the same class as the broker's read-only S3 calls, so this
does not violate §5.7's "no dataset byte touches a laptop".

## Two estimators, and why there are two

The obvious estimator is `num_rows x mean_tokens_per_doc` from a sample. It is nearly useless
on real corpora: measured on FineMath-3plus with 200 sampled docs it gave CV 9.0 and a 95% CI
of [26B, 204B] — an 8x range. Document lengths are heavy-tailed (that sample's max was 36x its
own mean), so the sample mean converges far too slowly to size a pool from.

The fix is to factor the estimate so the heavy-tailed part is measured over the whole corpus
instead of the sample:

    tokens  =  num_rows  x  mean_chars_per_doc  x  (tokens / char)
               ^exact       ^whole-corpus         ^sample, and TIGHT

`tokens/char` is a property of the *script and domain*, not of document length — English prose
runs ~0.25 tok/char with a CV around 0.1, so a few hundred docs pin it down. All the variance
lives in `mean_chars_per_doc`, and datasets-server's /statistics endpoint reports that as a
mean over hundreds of thousands of rows.

    PRIMARY   ("stats-ratio")  num_rows x stats.mean_chars x sampled tokens/char
    FALLBACK  ("sample-mean")  num_rows x sampled mean_tokens_per_doc

/statistics is not always available — it is computed per split and crashes server-side on some
large corpora (measured: DCLM-baseline HTTP 501 "job manager crashed", peS2o_filtered HTTP 500).
So the fallback is real, not decorative, and every result records which estimator produced it
plus a `confidence` field. Do not treat a sample-mean number as equal evidence to a stats-ratio
one: on FineMath the two agreed to 1.5% while the sample-mean CI alone spanned 8x.

⚠ /statistics may report `partial: true`, meaning its mean covers the first N rows rather than
all of them. That is a real bias risk when parquet row order correlates with content (crawl
date, repo name). It is recorded in the output as `stats_partial` — treat a partial mean as
provisional and cross-check against the sample mean, which IS drawn from random offsets.

## Comparability ratio

Where a corpus ships its own token count (FineMath's `token_count`), the output records
`dolma2_to_upstream_ratio`. That ratio is what makes card figures usable at all: multiply the
card's number by it rather than re-counting the corpus. Measured on FineMath-3plus: 1.0246, so
FineMath's advertised counts are already close to dolma2. peS2o's will not be — it reports
words.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SERVER = "https://datasets-server.huggingface.co"


def _headers() -> dict:
    """Authenticate when a token is available.

    Not cosmetic: the datasets-server rate limit for anonymous callers is low enough that a few
    parallel jobs trip HTTP 429 within minutes (measured — 9 concurrent agents took down every
    in-flight count at once). An authenticated call gets a much larger quota."""
    h = {"User-Agent": "edullm-data/recount"}
    tok = os.environ.get("HF_TOKEN")
    if not tok:
        p = os.path.expanduser("~/.cache/huggingface/token")
        if os.path.exists(p):
            with open(p) as f:
                tok = f.read().strip()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


UA = _headers()

# Columns that hold document text, in preference order. Checked against the real schema rather
# than assumed -- FinePhrase is the cautionary tale (§3.3 trap 1): its rewrite lives in
# rollout_results[0].text while `text` holds the ORIGINAL FineWeb-Edu document, so grabbing
# `text` there silently builds a corpus of unrephrased real data labelled synthetic. Pass
# --text-column explicitly for any corpus whose payload is not a plain top-level string.
TEXT_COLUMNS = ("text", "content", "raw_content", "document", "body", "markdown")

# Fields that are metadata, never payload -- excluded from the "longest string column" guess.
NON_TEXT_HINTS = (
    "id", "url", "title", "language", "license", "source", "domain", "crawl", "snapshot",
    "warc_filename", "mime", "date", "author", "path", "repo", "filename", "hash", "digest",
    "wikidata_id", "uri", "doi",
)

UPSTREAM_TOKEN_COLUMNS = ("token_count", "num_tokens", "tokens", "n_tokens", "token_cnt")


def _get(path: str, params: dict, tries: int = 6, timeout: int = 120):
    """GET with backoff. HTTP 4xx/5xx bodies are RETURNED as data, not raised -- a corpus whose
    /statistics crashes is a fact the caller must record and route around, not a crash.

    429 gets a much longer, exponential backoff than other transients. Measured the hard way:
    running several counts in parallel exhausted the shared rate limit and every job failed at
    once with 429, which the old 3s-linear backoff could not outlast. A 429 is not a broken
    corpus -- retrying slowly is the correct response, and giving up mislabels a fine source as
    unmeasurable."""
    url = f"{SERVER}/{path}?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode("utf8", "replace")
            # 400/404 = won't ever work; 500/501 = server gave up on this corpus. Both final.
            if e.code in (400, 404, 500, 501):
                return {"_http_error": e.code, "_body": body}
            last = f"HTTP {e.code}: {body[:120]}"
            if e.code == 429:
                time.sleep(min(120, 15 * 2 ** attempt))
                continue
        except Exception as e:  # noqa: BLE001 - transient network, retry
            last = f"{type(e).__name__}: {e}"
        time.sleep(3 * (attempt + 1))
    return {"_error": last}


def sizes(dataset: str) -> dict:
    return _get("size", {"dataset": dataset})


def splits(dataset: str) -> dict:
    return _get("splits", {"dataset": dataset})


def statistics_ep(dataset: str, config: str, split: str) -> dict:
    return _get("statistics", {"dataset": dataset, "config": config, "split": split})


def rows(dataset: str, config: str, split: str, offset: int, length: int) -> dict:
    return _get(
        "rows",
        {"dataset": dataset, "config": config, "split": split, "offset": offset, "length": length},
    )


def pick_text_column(row: dict) -> tuple[str | None, str]:
    """Return (column, how_it_was_chosen). The second element matters: a guessed column is a
    result a caller should eyeball, a named one is not."""
    for c in TEXT_COLUMNS:
        if isinstance(row.get(c), str):
            return c, "known-name"
    best, best_len = None, -1
    for k, v in row.items():
        if not isinstance(v, str):
            continue
        if any(h in k.lower() for h in NON_TEXT_HINTS):
            continue
        if len(v) > best_len:
            best, best_len = k, len(v)
    return best, "guessed-longest-string"


def stats_text_column(stats: dict, prefer: str | None = None) -> tuple[str | None, dict | None]:
    """Find the payload column in a /statistics response. Prefers an explicitly named column,
    else the string_text column with the largest mean -- FineWiki ships 7 string columns and
    only `text` is the payload (`wikitext` is the larger RAW markup; picking it would inflate
    the estimate ~1.6x)."""
    cands = []
    for col in stats.get("statistics", []):
        if col.get("column_type") != "string_text":
            continue
        name = col.get("column_name")
        cs = col.get("column_statistics", {}) or {}
        if not isinstance(cs.get("mean"), (int, float)):
            continue
        if prefer and name == prefer:
            return name, cs
        if any(h in (name or "").lower() for h in NON_TEXT_HINTS):
            continue
        cands.append((cs["mean"], name, cs))
    if not cands:
        return None, None
    cands.sort(reverse=True)
    return cands[0][1], cands[0][2]


def bootstrap_ci(values: list[float], n_boot: int = 2000, alpha: float = 0.05, seed: int = 0):
    """Percentile bootstrap CI for the MEAN. Heavy tails make the normal approximation
    optimistic, which is the failure mode that matters -- an over-tight CI makes a pool look
    adequately sized when it is not."""
    if len(values) < 2:
        return (None, None)
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    return (means[int(alpha / 2 * n_boot)], means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))])


def measure(
    dataset: str, config: str, split: str, n_rows_total: int, tokenizer,
    n_sample: int = 300, batch: int = 5, seed: int = 0, text_column: str | None = None,
) -> dict:
    """Sample rows at RANDOM offsets and tokenize.

    batch defaults to 5, not 20: parquet row groups are often content-clustered, so 20
    consecutive rows are far from 20 independent draws. Small batches at many distinct offsets
    buy real independence for the same number of rows (the cost is more HTTP requests)."""
    rng = random.Random(seed)
    tok_counts: list[int] = []
    char_counts: list[int] = []
    byte_counts: list[int] = []
    upstream_counts: list[int] = []
    chosen_col, how = text_column, "explicit" if text_column else ""
    upstream_col = None
    errors: list[str] = []
    empty = 0

    n_batches = max(1, n_sample // batch)
    offsets = sorted({rng.randrange(0, max(1, n_rows_total - batch)) for _ in range(n_batches)})

    for off in offsets:
        got = rows(dataset, config, split, off, batch)
        if "rows" not in got:
            errors.append(f"offset {off}: {str(got)[:140]}")
            if len(errors) > 5:
                break
            continue
        for item in got["rows"]:
            row = item.get("row", {})
            if chosen_col is None:
                chosen_col, how = pick_text_column(row)
                if chosen_col is None:
                    errors.append(f"no text column; keys={sorted(row)[:12]}")
                    break
            if upstream_col is None:
                for c in UPSTREAM_TOKEN_COLUMNS:
                    if isinstance(row.get(c), (int, float)):
                        upstream_col = c
                        break
            text = row.get(chosen_col)
            if not isinstance(text, str) or not text:
                empty += 1
                continue
            tok_counts.append(len(tokenizer(text, add_special_tokens=False)["input_ids"]))
            char_counts.append(len(text))
            byte_counts.append(len(text.encode("utf8")))
            if upstream_col and isinstance(row.get(upstream_col), (int, float)):
                upstream_counts.append(int(row[upstream_col]))

    if not tok_counts:
        return {"status": "FAILED", "errors": errors[:5], "n_rows": n_rows_total,
                "text_column": chosen_col}

    mean_tok = statistics.fmean(tok_counts)
    sd = statistics.stdev(tok_counts) if len(tok_counts) > 1 else 0.0
    tpc = sum(tok_counts) / sum(char_counts)
    # tokens/char per DOCUMENT, so its spread is comparable across corpora
    per_doc_tpc = [t / c for t, c in zip(tok_counts, char_counts) if c]
    tpc_sd = statistics.stdev(per_doc_tpc) if len(per_doc_tpc) > 1 else 0.0
    lo, hi = bootstrap_ci([float(x) for x in tok_counts], seed=seed)

    out = {
        "status": "ok",
        "text_column": chosen_col,
        "text_column_chosen_by": how,
        "n_rows": n_rows_total,
        "n_sampled": len(tok_counts),
        "n_distinct_offsets": len(offsets),
        "mean_tokens_per_doc": round(mean_tok, 2),
        "median_tokens_per_doc": statistics.median(tok_counts),
        "cv_tokens_per_doc": round(sd / mean_tok, 3) if mean_tok else None,
        "mean_chars_per_doc_sampled": round(statistics.fmean(char_counts), 1),
        "tokens_per_char": round(tpc, 5),
        "tokens_per_char_cv": round(tpc_sd / statistics.fmean(per_doc_tpc), 4) if per_doc_tpc else None,
        "tokens_per_byte": round(sum(tok_counts) / sum(byte_counts), 5) if sum(byte_counts) else None,
        "sample_mean_est_tokens": int(mean_tok * n_rows_total),
        "sample_mean_ci95": [int(lo * n_rows_total), int(hi * n_rows_total)] if lo else None,
        "sample_mean_rel_halfwidth": round((hi - lo) / 2 / mean_tok, 4) if lo and mean_tok else None,
        "n_empty_text": empty,
        "errors": errors[:5],
    }
    if upstream_counts:
        up = statistics.fmean(upstream_counts)
        out["upstream_token_column"] = upstream_col
        out["upstream_mean_tokens_per_doc"] = round(up, 2)
        out["dolma2_to_upstream_ratio"] = round(mean_tok / up, 4) if up else None
    return out


def combine(m: dict, stats: dict | None, n_rows: int) -> dict:
    """Attach the stats-ratio estimate when /statistics is available, and pick the primary."""
    out = dict(m)
    out["stats_available"] = False
    if stats and "statistics" in stats:
        col, cs = stats_text_column(stats, prefer=m.get("text_column"))
        if cs and isinstance(cs.get("mean"), (int, float)):
            mean_chars = cs["mean"]
            out.update({
                "stats_available": True,
                "stats_column": col,
                "stats_num_examples": stats.get("num_examples"),
                "stats_partial": stats.get("partial"),
                "stats_mean_chars": round(mean_chars, 1),
                "stats_median_chars": cs.get("median"),
                "stats_ratio_est_tokens": int(n_rows * mean_chars * m["tokens_per_char"]),
            })
            # Agreement between two independent estimators is the strongest evidence available
            # here. Divergence >25% means one of them is wrong -- usually a partial stats mean
            # over a content-clustered head, or a sample too small for the tail.
            sm = m["sample_mean_est_tokens"]
            sr = out["stats_ratio_est_tokens"]
            if sm and sr:
                out["estimator_divergence"] = round(abs(sr - sm) / max(sr, sm), 4)

    if out["stats_available"]:
        out["est_total_tokens"] = out["stats_ratio_est_tokens"]
        out["estimator"] = "stats-ratio"
        div = out.get("estimator_divergence")
        partial = out.get("stats_partial")
        if div is not None and div > 0.25:
            out["confidence"] = "low"
            out["confidence_note"] = (
                f"estimators disagree by {div:.0%}"
                + (" and the /statistics mean is PARTIAL (head of the split, may be content-clustered)"
                   if partial else "")
            )
        elif partial:
            out["confidence"] = "medium"
            out["confidence_note"] = "/statistics mean is partial; cross-checked against a random sample"
        else:
            out["confidence"] = "high"
    else:
        out["est_total_tokens"] = out.get("sample_mean_est_tokens")
        out["estimator"] = "sample-mean"
        out["confidence"] = "low"
        why = ""
        if stats:
            why = f" (/statistics unavailable: {stats.get('_http_error') or stats.get('_error')})"
        out["confidence_note"] = (
            "no /statistics for this split, so the estimate is num_rows x a heavy-tailed sample "
            "mean; the CI is wide by nature" + why
        )
    return out


def load_tokenizer():
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained("allenai/dolma2-tokenizer")


def main() -> int:
    p = argparse.ArgumentParser(description="Re-count a HF dataset under the dolma2 tokenizer.")
    p.add_argument("dataset")
    p.add_argument("--config", default=None, help="default: every config")
    p.add_argument("--split", default=None, help="default: the config's first split")
    p.add_argument("--n-sample", type=int, default=300)
    p.add_argument("--batch", type=int, default=5)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--text-column", default=None,
                   help="force a column -- required for nested payloads (FinePhrase, §3.3)")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    sz = sizes(args.dataset)
    if "size" not in sz:
        payload = {"dataset": args.dataset, "status": "NO_SIZE", "detail": str(sz)[:400]}
        print(json.dumps(payload, indent=2))
        if args.out:
            open(args.out, "w").write(json.dumps(payload, indent=2) + "\n")
        return 1

    # ⚠ `num_rows` IS NOT ALWAYS THE CORPUS. When the viewer's parquet conversion hits its 5 GB
    # cap, /size sets a top-level `partial: true` and `num_rows` describes only the converted
    # HEAD, with the real figure in `estimated_num_rows`. Measured on DCLM-baseline:
    #     num_rows            779,982
    #     estimated_num_rows  3,017,780,768     <- 3,869x larger
    # Multiplying a tokens/doc mean by 779,982 reported 1.23B tokens for a ~3.8T corpus. The
    # ratio is silent -- both numbers are plausible-looking integers -- so this check is not
    # optional. Prefer estimated_num_rows when partial, and always record which was used.
    partial_conversion = bool(sz.get("partial"))
    if partial_conversion:
        print(f"[recount] ⚠ /size reports partial:true for {args.dataset} -- the parquet "
              f"conversion is truncated; using estimated_num_rows where available", file=sys.stderr)

    per_config = {c["config"]: c for c in sz["size"]["configs"]}
    sp = splits(args.dataset)
    split_of = {}
    for s in (sp.get("splits") or []):
        split_of.setdefault(s["config"], s["split"])

    targets = [args.config] if args.config else sorted(per_config)
    tok = load_tokenizer()
    results = []
    for cfg in targets:
        if cfg not in per_config:
            results.append({"config": cfg, "status": "NO_SUCH_CONFIG", "available": sorted(per_config)})
            continue
        split = args.split or split_of.get(cfg, "train")
        converted_rows = per_config[cfg]["num_rows"]
        estimated_rows = per_config[cfg].get("estimated_num_rows")
        # The row count to EXTRAPOLATE with. `num_rows` only equals the corpus when the
        # conversion is complete (see the partial-conversion note in main()).
        n = estimated_rows if (partial_conversion and estimated_rows) else converted_rows
        rows_basis = ("estimated_num_rows" if n is estimated_rows and estimated_rows
                      else "num_rows")
        if rows_basis == "estimated_num_rows":
            print(f"[recount] {args.dataset}/{cfg}/{split}: {n:,} rows (ESTIMATED; only "
                  f"{converted_rows:,} converted = {converted_rows/n:.2%})", file=sys.stderr)
        else:
            print(f"[recount] {args.dataset}/{cfg}/{split}: {n:,} rows", file=sys.stderr)
        st = statistics_ep(args.dataset, cfg, split)
        m = measure(args.dataset, cfg, split, converted_rows, tok, n_sample=args.n_sample,
                    batch=args.batch, seed=args.seed, text_column=args.text_column)
        # Sampling can only reach CONVERTED rows (that is all /rows serves), but extrapolation
        # must use the true corpus size. Keep the two explicitly separate so the artifact can
        # never be read as "we sampled the whole corpus."
        if m.get("status") == "ok":
            m["n_rows_sampled_from"] = converted_rows
            m["n_rows_extrapolated_to"] = n
            m["rows_basis"] = rows_basis
            m["sample_mean_est_tokens"] = int(m["mean_tokens_per_doc"] * n)
        r = {"config": cfg, "split": split} | (
            combine(m, st, n) if m.get("status") == "ok" else m
        )
        r["parquet_bytes"] = per_config[cfg].get("num_bytes_parquet_files")
        r["conversion_partial"] = partial_conversion
        if partial_conversion:
            r.setdefault("confidence", "low")
            r["confidence_note"] = (
                f"parquet conversion is PARTIAL: only {converted_rows:,} of ~{n:,} rows are "
                f"reachable via /rows, so tokens/char and mean_chars are measured on the "
                f"converted head and assumed to hold for the rest. A shard stream on Batch is "
                f"needed for a real number. "
            ) + (r.get("confidence_note") or "")
        results.append(r)
        if r.get("status") == "ok":
            print(f"[recount]   -> {r['est_total_tokens']:,} tokens "
                  f"({r['estimator']}, confidence={r['confidence']})", file=sys.stderr)

    payload = {
        "dataset": args.dataset,
        "tokenizer": "allenai/dolma2-tokenizer",
        "method": "num_rows (exact) x mean_chars (/statistics, whole-split) x tokens/char (sampled); "
                  "falls back to num_rows x sampled mean_tokens/doc when /statistics is unavailable",
        "dataset_total_rows": sz["size"]["dataset"]["num_rows"],
        "configs": results,
    }
    text = json.dumps(payload, indent=2)
    if args.out:
        open(args.out, "w").write(text + "\n")
        print(f"[recount] wrote {args.out}", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
