#!/usr/bin/env python3
"""Re-count FinePhrase's REWRITE under dolma2 -- recount.py cannot, and here is why.

Phase 0 task A, synthetic (rephrased) category. This is a standalone companion to `recount.py`,
not a replacement: it exists solely because FinePhrase's payload is NOT a top-level string.

## The trap (README.md "two things that will silently corrupt your result", item 1)

FinePhrase's 12 columns are the 11 columns of its FineWeb-Edu parent plus one:

    text          str   <- the ORIGINAL FineWeb-Edu document. NOT the rewrite.
    dataset       str   <- literally the string "HuggingFaceFW/fineweb-edu"
    token_count   int   <- FineWeb-Edu's own count of `text`, NOT of the rewrite
    id, dump, url, file_path, language, language_score, score, int_score
    rollout_results  LIST<STRUCT{finish_reason: str, text: str,
                                 usage: STRUCT{completion_tokens, prompt_tokens,
                                               prompt_tokens_details, total_tokens}}>

The rewrite is `rollout_results[0].text`. /statistics reports `rollout_results` with
column_type "list" and min == max == 1.0, i.e. exactly one rollout per document, always --
so element [0] is the whole payload, there is no multi-rollout aggregation to do.

## Why recount.py cannot be pointed at it

`measure()` does `text = row.get(chosen_col)` then `if not isinstance(text, str)`. With
`--text-column rollout_results` every row yields a LIST, every row increments `n_empty_text`,
`tok_counts` stays empty and the run returns status FAILED. `--text-column` cannot express a
path. And `pick_text_column()` would silently choose `text` -- the original document -- which
is precisely the corruption the README warns about: a corpus of UNREPHRASED REAL DATA labelled
synthetic, with no size or hash check able to notice.

## The estimator, and why the heavy-tail problem is milder here

recount.py's `num_rows x stats.mean_chars x tokens/char` factoring is unavailable: /statistics
publishes no mean for a string nested inside a list, so there is no whole-split char mean to
borrow. But the rewrite does not need it. Generation ran with `max_tokens=2048`, so rewrite
length is BOUNDED, unlike the source documents (whose `text` runs to 640,136 chars). The
sample mean therefore converges at a normal rate. We report the measured CV so that claim is
checkable rather than asserted, and we keep the factored form anyway as an independent
cross-check anchored on an EXACT whole-corpus quantity:

    A  sample-mean      num_rows x mean(dolma2 tokens of rollout_results[0].text)
    B  card-anchored    advertised_completion_tokens x (dolma2 tokens / completion_tokens)

B's first factor is exact: `usage.completion_tokens` is the generator's own count, and the
card's 486,367,076,933 is its whole-corpus sum. Its second factor is a tokenizer-vs-tokenizer
ratio (SmolLM2 -> dolma2), which is a property of script and domain, so a few hundred docs
pin it down. A and B are independent in their heavy-tailed factor, so their divergence is
real evidence.

We tokenize the ORIGINAL `text` in the same pass, so the rewrite/original ratio comes from
the same documents rather than two different samples.

NO DATASET DOWNLOAD -- datasets-server /rows only (§5.7).
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from recount import bootstrap_ci, load_tokenizer  # noqa: E402  reuse, don't re-implement

SERVER = "https://datasets-server.huggingface.co"
UA = {"User-Agent": "edullm-data/recount"}

# Whole-corpus completion-token totals. Total is from the dataset card ("Completion tokens
# across all configs: 486,367,076,933"); per-config figures are the design doc's §3.2 numbers,
# which this script verifies against its own num_rows x mean(completion_tokens).
CARD_COMPLETION_TOKENS = {
    "faq": 148_100_000_000,
    "tutorial": 147_400_000_000,
    "math": 98_400_000_000,
    "table": 92_400_000_000,
}
CARD_TOTAL_COMPLETION_TOKENS = 486_367_076_933
CARD_SOURCE_DOCS = 339_347_842
CARD_OUTPUT_SAMPLES = 1_354_044_711


def _get(path: str, params: dict, tries: int = 6, timeout: int = 120):
    """GET with backoff. 429 is retried (datasets-server rate-limits hard on /rows); 4xx/5xx
    bodies are RETURNED as data -- a config whose /rows crashes is a fact to record, not a
    crash. FinePhrase's `all` and `faq` configs do exactly that."""
    url = f"{SERVER}/{path}?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode("utf8", "replace")
            if e.code == 429:
                last = f"HTTP 429: {body}"
                time.sleep(8 * (attempt + 1))
                continue
            if e.code in (400, 404, 500, 501):
                return {"_http_error": e.code, "_body": body}
            last = f"HTTP {e.code}: {body}"
        except Exception as e:  # noqa: BLE001 - transient network, retry
            last = f"{type(e).__name__}: {e}"
        time.sleep(4 * (attempt + 1))
    return {"_error": last}


def rewrite_of(row: dict) -> tuple[str | None, dict, int]:
    """THE schema path: rollout_results[0].text. Returns (text, usage, n_rollouts)."""
    rr = row.get("rollout_results")
    if not isinstance(rr, list) or not rr:
        return None, {}, 0
    first = rr[0] if isinstance(rr[0], dict) else {}
    txt = first.get("text")
    return (txt if isinstance(txt, str) else None), (first.get("usage") or {}), len(rr)


def measure_config(dataset: str, config: str, split: str, n_rows_total: int, tokenizer,
                   n_sample: int, batch: int, seed: int) -> dict:
    """Sample at random offsets; tokenize BOTH the rewrite and the original from each row."""
    rng = random.Random(seed)
    rew_tok: list[int] = []
    rew_chars: list[int] = []
    orig_tok: list[int] = []
    orig_chars: list[int] = []
    comp_tok: list[int] = []          # generator's own SmolLM2 count of the rewrite
    upstream_tok: list[int] = []      # FineWeb-Edu's own count of the ORIGINAL
    n_rollouts: list[int] = []
    finish_reasons: dict[str, int] = {}
    dataset_field: dict[str, int] = {}
    short_rewrites: list[dict] = []
    errors: list[str] = []
    empty = 0

    n_batches = max(1, n_sample // batch)
    offsets = sorted({rng.randrange(0, max(1, n_rows_total - batch)) for _ in range(n_batches)})

    for i, off in enumerate(offsets):
        got = _get("rows", {"dataset": dataset, "config": config, "split": split,
                            "offset": off, "length": batch})
        if "rows" not in got:
            errors.append(f"offset {off}: {str(got)[:160]}")
            if len(errors) > 8:
                break
            continue
        for item in got["rows"]:
            row = item.get("row", {})
            txt, usage, nr = rewrite_of(row)
            n_rollouts.append(nr)
            dataset_field[str(row.get("dataset"))] = dataset_field.get(str(row.get("dataset")), 0) + 1
            rrl = row.get("rollout_results") or []
            if rrl and isinstance(rrl[0], dict):
                fr = str(rrl[0].get("finish_reason"))
                finish_reasons[fr] = finish_reasons.get(fr, 0) + 1
            if not txt:
                empty += 1
                continue
            n = len(tokenizer(txt, add_special_tokens=False)["input_ids"])
            rew_tok.append(n)
            rew_chars.append(len(txt))
            if isinstance(usage.get("completion_tokens"), (int, float)):
                comp_tok.append(int(usage["completion_tokens"]))
            orig = row.get("text")
            if isinstance(orig, str) and orig:
                orig_tok.append(len(tokenizer(orig, add_special_tokens=False)["input_ids"]))
                orig_chars.append(len(orig))
            if isinstance(row.get("token_count"), (int, float)):
                upstream_tok.append(int(row["token_count"]))
            if n < 200:
                short_rewrites.append({"dolma2_tokens": n, "chars": len(txt),
                                       "completion_tokens": usage.get("completion_tokens"),
                                       "text": txt[:180]})
        if (i + 1) % 10 == 0:
            print(f"[{config}] {i+1}/{len(offsets)} offsets, {len(rew_tok)} docs", file=sys.stderr)
        time.sleep(0.4)

    if not rew_tok:
        return {"config": config, "status": "FAILED", "errors": errors[:8],
                "n_rows": n_rows_total,
                "note": "datasets-server /rows unavailable for this config"}

    mean_rew = statistics.fmean(rew_tok)
    sd_rew = statistics.stdev(rew_tok) if len(rew_tok) > 1 else 0.0
    lo, hi = bootstrap_ci([float(x) for x in rew_tok], seed=seed)

    out = {
        "config": config,
        "split": split,
        "status": "ok",
        "schema_path": "rollout_results[0].text",
        "n_rows": n_rows_total,
        "n_sampled": len(rew_tok),
        "n_distinct_offsets": len(offsets),
        "n_empty_rewrite": empty,
        "rollouts_per_row_min_max": [min(n_rollouts), max(n_rollouts)] if n_rollouts else None,
        "dataset_field_values": dataset_field,
        "finish_reasons": finish_reasons,
        # --- the rewrite (the thing we actually want) ---
        "rewrite_mean_tokens_per_doc": round(mean_rew, 2),
        "rewrite_median_tokens_per_doc": statistics.median(rew_tok),
        "rewrite_cv_tokens_per_doc": round(sd_rew / mean_rew, 3) if mean_rew else None,
        "rewrite_max_tokens_seen": max(rew_tok),
        "rewrite_mean_chars_per_doc": round(statistics.fmean(rew_chars), 1),
        "rewrite_tokens_per_char": round(sum(rew_tok) / sum(rew_chars), 5),
        "sample_mean_est_tokens": int(mean_rew * n_rows_total),
        "sample_mean_ci95": [int(lo * n_rows_total), int(hi * n_rows_total)] if lo else None,
        "sample_mean_rel_halfwidth": round((hi - lo) / 2 / mean_rew, 4) if lo and mean_rew else None,
        "short_rewrite_examples": sorted(short_rewrites, key=lambda d: d["dolma2_tokens"])[:6],
    }

    # short-rewrite distribution -- bears on whether OUR quality filter is load-bearing
    n = len(rew_tok)
    buckets = {}
    for thr in (16, 50, 100, 200, 500):
        k = sum(1 for t in rew_tok if t < thr)
        buckets[f"under_{thr}"] = {"n": k, "frac": round(k / n, 4)}
    out["short_rewrite_buckets"] = buckets
    out["rewrite_token_percentiles"] = {
        p: statistics.quantiles(rew_tok, n=100)[p - 1] for p in (1, 5, 10, 25, 50, 75, 90, 99)
    } if n >= 100 else None
    # tokens lost to sub-threshold rewrites, if we filtered them out
    for thr in (50, 200):
        keep = [t for t in rew_tok if t >= thr]
        out[f"tokens_retained_if_filter_ge_{thr}"] = round(sum(keep) / sum(rew_tok), 4)

    # --- the ORIGINAL, from the same rows ---
    if orig_tok:
        mean_orig = statistics.fmean(orig_tok)
        out.update({
            "original_text_column": "text",
            "original_mean_tokens_per_doc": round(mean_orig, 2),
            "original_median_tokens_per_doc": statistics.median(orig_tok),
            "original_mean_chars_per_doc": round(statistics.fmean(orig_chars), 1),
            "original_sample_mean_est_tokens": int(mean_orig * n_rows_total),
            # paired, same documents -- the honest expansion factor
            "rewrite_over_original_token_ratio": round(sum(rew_tok) / sum(orig_tok), 4),
            "rewrite_over_original_ratio_median_of_pairs": round(statistics.median(
                [r / o for r, o in zip(rew_tok, orig_tok) if o]), 4),
        })
        if upstream_tok:
            out["original_upstream_mean_token_count"] = round(statistics.fmean(upstream_tok), 2)
            out["dolma2_to_fineweb_edu_ratio_on_original"] = round(
                mean_orig / statistics.fmean(upstream_tok), 4)

    # --- estimator B: card-anchored ---
    if comp_tok:
        out["generator_mean_completion_tokens"] = round(statistics.fmean(comp_tok), 2)
        out["dolma2_per_completion_token"] = round(sum(rew_tok) / sum(comp_tok), 5)
        per_doc = [r / c for r, c in zip(rew_tok, comp_tok) if c]
        out["dolma2_per_completion_token_cv"] = round(
            statistics.stdev(per_doc) / statistics.fmean(per_doc), 4) if len(per_doc) > 1 else None
        out["implied_config_completion_tokens"] = int(statistics.fmean(comp_tok) * n_rows_total)
        card = CARD_COMPLETION_TOKENS.get(config)
        if card:
            out["card_completion_tokens"] = card
            out["card_vs_implied_completion_divergence"] = round(
                abs(out["implied_config_completion_tokens"] - card) / card, 4)
            out["card_anchored_est_tokens"] = int(card * out["dolma2_per_completion_token"])

    a = out["sample_mean_est_tokens"]
    b = out.get("card_anchored_est_tokens")
    if a and b:
        out["estimator_divergence"] = round(abs(a - b) / max(a, b), 4)
        out["est_total_tokens"] = b
        out["estimator"] = "card-anchored (exact whole-corpus completion tokens x sampled dolma2/completion ratio)"
        div = out["estimator_divergence"]
        if div > 0.25:
            out["confidence"] = "low"
            out["confidence_note"] = f"estimators disagree by {div:.0%}"
        elif div > 0.10:
            out["confidence"] = "medium"
            out["confidence_note"] = (
                f"estimators agree within {div:.1%}; the card's per-config completion total is "
                "unverified against the repo (only the 486.4B grand total is on the card)")
        else:
            out["confidence"] = "high"
            out["confidence_note"] = (
                f"two independent estimators agree within {div:.1%}; rewrite length is bounded "
                f"by max_tokens=2048 so CV is {out['rewrite_cv_tokens_per_doc']} "
                "(vs 1.47 for unbounded source docs) and the sample mean converges")
    else:
        out["est_total_tokens"] = a
        out["estimator"] = "sample-mean"
        out["confidence"] = "medium"
        out["confidence_note"] = "no card anchor for this config; single estimator"
    out["errors"] = errors[:8]
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dataset", default="HuggingFaceFW/finephrase")
    p.add_argument("--configs", default="faq,math,table,tutorial")
    p.add_argument("--n-sample", type=int, default=200)
    p.add_argument("--batch", type=int, default=5)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", default=None)
    args = p.parse_args()

    tok = load_tokenizer()
    sz = _get("size", {"dataset": args.dataset})
    per_config = {c["config"]: c for c in (sz.get("size", {}).get("configs") or [])}
    size_failed = [f["config"] for f in (sz.get("failed") or []) if f.get("kind") == "config-size"]

    results = []
    for cfg in [c for c in args.configs.split(",") if c]:
        if cfg in per_config:
            n = per_config[cfg]["num_rows"]
        else:
            # /size crashed for this config; fall back to /rows' own num_rows_total
            probe = _get("rows", {"dataset": args.dataset, "config": cfg, "split": "train",
                                  "offset": 0, "length": 1})
            n = probe.get("num_rows_total")
            if not n:
                results.append({"config": cfg, "status": "FAILED",
                                "reason": "no row count: /size failed AND /rows failed",
                                "size_error": "config-size job failed server-side",
                                "rows_error": str(probe)[:200]})
                print(f"[{cfg}] UNCOUNTABLE via datasets-server", file=sys.stderr)
                continue
        print(f"[{cfg}] {n:,} rows", file=sys.stderr)
        r = measure_config(args.dataset, cfg, "train", n, tok,
                           args.n_sample, args.batch, args.seed)
        r["parquet_bytes"] = per_config.get(cfg, {}).get("num_bytes_parquet_files")
        results.append(r)
        if r.get("status") == "ok":
            print(f"[{cfg}] -> {r['est_total_tokens']:,} dolma2 tokens "
                  f"({r['confidence']}); orig would have been "
                  f"{r.get('original_sample_mean_est_tokens', 0):,}", file=sys.stderr)
        if args.out:  # persist after every config -- this machine has died mid-run
            open(args.out, "w").write(json.dumps(
                {"dataset": args.dataset, "tokenizer": "allenai/dolma2-tokenizer",
                 "size_endpoint_failed_for": size_failed, "configs": results}, indent=2) + "\n")

    payload = {
        "dataset": args.dataset,
        "tokenizer": "allenai/dolma2-tokenizer",
        "schema_path": "rollout_results[0].text",
        "size_endpoint_failed_for": size_failed,
        "card": {"source_docs": CARD_SOURCE_DOCS, "output_samples": CARD_OUTPUT_SAMPLES,
                 "completion_tokens_total": CARD_TOTAL_COMPLETION_TOKENS},
        "configs": results,
    }
    text = json.dumps(payload, indent=2)
    if args.out:
        open(args.out, "w").write(text + "\n")
        print(f"[recount] wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
