#!/usr/bin/env python3
"""Between-row-group variance of the short-rewrite rate -- the effective-n correction.

A parquet row group here holds 1000 CONSECUTIVE rows, and parquet row order in FinePhrase is
inherited from FineWeb-Edu, which is content-clustered (by dump/URL). So 12 row groups x 1000
rewrites is NOT 12,000 independent draws: within-group correlation inflates the apparent
precision. Phase 0's n=34 head sample failed for exactly this reason (it found 40.5 mean tokens
where the corpus-wide figure is ~270, because one contiguous block of the split is degenerate).

This script reports the PER-ROW-GROUP short-rewrite fraction and mean tokens, so the design-eater
question -- "is our quality filter load-bearing?" -- is answered with a cluster-aware interval
rather than a naive binomial one. It reads one random row group from each of N random files,
rewrite leaf column only.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import random
import statistics
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from _fp_footer_leaf import read_row_group, tokenizer  # noqa: E402


def one(entry, seed):
    d = read_row_group(entry, False, random.Random(seed))
    return d


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--n-rowgroups", type=int, default=30)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--seed", type=int, default=99)
    p.add_argument("--out", required=True)
    a = p.parse_args()

    tree = [t for t in json.load(open(f"/tmp/fp/tree_{a.config}.json"))
            if t["path"].endswith(".parquet")]
    rng = random.Random(a.seed)
    sample = rng.sample(tree, min(a.n_rowgroups, len(tree)))
    tok = tokenizer()

    groups: list[dict] = []
    all_tok: list[int] = []
    finish: dict[str, int] = {}
    degen: dict[str, int] = {}
    errs: list[str] = []
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(one, e, a.seed + i): e for i, e in enumerate(sample)}
        for i, f in enumerate(cf.as_completed(futs)):
            try:
                d = f.result()
            except Exception as e:  # noqa: BLE001
                errs.append(f"{futs[f]['path']}: {type(e).__name__}: {e}"[:160])
                continue
            texts = [t for t in d["rewrites"] if isinstance(t, str) and t]
            if not texts:
                continue
            ids = tok(texts, add_special_tokens=False)["input_ids"]
            ts = [len(x) for x in ids]
            all_tok += ts
            for fr in d["finish"]:
                finish[str(fr)] = finish.get(str(fr), 0) + 1
            for t, n in zip(texts, ts):
                if n <= 5:
                    degen[t[:60]] = degen.get(t[:60], 0) + 1
            groups.append({
                "path": d["path"], "row_group": d["row_group"], "n": len(ts),
                "mean_tokens": round(statistics.fmean(ts), 1),
                "median_tokens": statistics.median(ts),
                "frac_under_50": round(sum(1 for x in ts if x < 50) / len(ts), 4),
                "frac_under_16": round(sum(1 for x in ts if x < 16) / len(ts), 4),
            })
            print(f"[{a.config}] {i+1}/{len(sample)} groups, {len(all_tok)} rewrites",
                  file=sys.stderr)

    fr50 = [g["frac_under_50"] for g in groups]
    mt = [g["mean_tokens"] for g in groups]
    n = len(all_tok)
    out = {
        "config": a.config,
        "n_row_groups": len(groups),
        "n_rewrites": n,
        "pooled_mean_tokens": round(statistics.fmean(all_tok), 2),
        "pooled_median_tokens": statistics.median(all_tok),
        "pooled_frac_under_50": round(sum(1 for x in all_tok if x < 50) / n, 5),
        "pooled_frac_under_16": round(sum(1 for x in all_tok if x < 16) / n, 5),
        "pooled_frac_under_100": round(sum(1 for x in all_tok if x < 100) / n, 5),
        "pooled_frac_under_200": round(sum(1 for x in all_tok if x < 200) / n, 5),
        "per_group_frac_under_50": sorted(fr50),
        "per_group_mean_tokens": sorted(mt),
        "between_group_sd_frac_under_50": round(statistics.stdev(fr50), 5) if len(fr50) > 1 else None,
        "between_group_sd_mean_tokens": round(statistics.stdev(mt), 2) if len(mt) > 1 else None,
        # cluster-aware SE: SD of the per-group statistic / sqrt(#groups). Compare to the
        # naive binomial SE, which pretends the 1000 rows in a group are independent.
        "cluster_se_frac_under_50": round(statistics.stdev(fr50) / len(fr50) ** 0.5, 5)
        if len(fr50) > 1 else None,
        "naive_binomial_se_frac_under_50": round(
            (statistics.fmean(fr50) * (1 - statistics.fmean(fr50)) / n) ** 0.5, 5),
        "cluster_se_mean_tokens": round(statistics.stdev(mt) / len(mt) ** 0.5, 2)
        if len(mt) > 1 else None,
        "finish_reasons": finish,
        "degenerate_strings_le_5_tokens": sorted(degen.items(), key=lambda kv: -kv[1])[:15],
        "groups": groups,
        "errors": errs[:6],
    }
    open(a.out, "w").write(json.dumps(out, indent=2) + "\n")
    print(json.dumps({k: v for k, v in out.items() if k != "groups"}, indent=2)[:2500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
