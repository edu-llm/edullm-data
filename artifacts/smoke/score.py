#!/usr/bin/env python3
"""Score the dual-judge smoke test and emit the §9.5 gate table.

Phase 0 task E, scoring half, of DATASET-DESIGN-reservoir.md §9.4/§9.5. Reads judge labels
(`judges.jsonl`, from `judge.py`) and candidate labels (`d_labels.jsonl`, from `classify_d.py`)
and produces the table that a human uses to decide whether ~$595 gets spent.

THIS SCRIPT DOES NOT DECIDE ANYTHING. It reports. The plan's §9.1 hard stop applies whether the
gate passes or fails — "passing is not consent."

## The arithmetic, and the one trap in it

    J         = agreement(A, B) over docs where both parsed   <- the measurement CEILING
    score     = accuracy(D) restricted to {A == B}            <- THE GATE, vs 85%
    inherited = P(D == B AND A != B)                          <- what judge B exists to expose

**Why the gate is scored on {A == B} and not against both judges jointly.** Requiring D to match
both caps the achievable score at J. If A and B agree only 75% of the time, an 85% bar is
*mathematically impossible* for reasons that have nothing to do with D — you would be measuring
the judges, and rejecting a fine candidate. So the consensus subset is the ground truth, its size
is reported (`n_scored`), and J is reported alongside as the ceiling.

**Read J before you read the score.** A high score on a tiny consensus subset is weak evidence.
If J is low (say <60%), the taxonomy is ambiguous for that source and the honest conclusion is
"this measurement can't settle it," not "D passed."

## The five patterns (§9.4)

| pattern | scored as |
|---|---|
| D = A = B | correct |
| D = A, B differs | correct — D sided with the independent judge |
| **D = B, A differs** | **INCORRECT — inherited error** |
| A = B, D differs | incorrect |
| all three differ | excluded |

Note the third row is *outside* the consensus subset (A != B), so it does not enter `score`. It is
reported separately as `inherited_error_rate` because it is the specific failure a distilled model
is most prone to and a single-judge design would score it as a success.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

GATE = 0.85


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.open() if l.strip()]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval. Used instead of the normal approximation because n per source is a
    few hundred and the proportion sits near 0.85, where the normal interval misbehaves."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return ((c - m) / d, (c + m) / d)


def score_source(judged: list[dict], d_labels: dict[str, int]) -> dict:
    n_total = len(judged)
    both_parsed = [r for r in judged if r.get("a") is not None and r.get("b") is not None]
    n_unparsed = n_total - len(both_parsed)

    consensus = [r for r in both_parsed if r["a"] == r["b"]]
    J = len(consensus) / len(both_parsed) if both_parsed else 0.0

    # The gate: D on the consensus subset, counting only docs D actually labelled.
    scored = [r for r in consensus if d_labels.get(r["doc_id"]) is not None]
    correct = sum(1 for r in scored if d_labels[r["doc_id"]] == r["a"])
    score = correct / len(scored) if scored else 0.0
    lo, hi = wilson(correct, len(scored))

    # Inherited error: D reproduced B while A disagreed. Measured on the NON-consensus set.
    disagree = [r for r in both_parsed if r["a"] != r["b"]]
    d_on_disagree = [r for r in disagree if d_labels.get(r["doc_id"]) is not None]
    inherited = sum(1 for r in d_on_disagree if d_labels[r["doc_id"]] == r["b"])
    sided_with_a = sum(1 for r in d_on_disagree if d_labels[r["doc_id"]] == r["a"])
    all_differ = len(d_on_disagree) - inherited - sided_with_a

    d_missing = sum(1 for r in both_parsed if d_labels.get(r["doc_id"]) is None)

    return {
        "n_judged": n_total,
        "n_both_judges_parsed": len(both_parsed),
        "n_unparsed": n_unparsed,
        "J_judge_agreement": round(J, 4),
        "n_consensus": len(consensus),
        "n_scored": len(scored),
        "n_correct": correct,
        "score": round(score, 4),
        "score_ci95": [round(lo, 4), round(hi, 4)],
        "verdict": "PASS" if score >= GATE else "FAIL",
        "gate": GATE,
        # the non-consensus breakdown -- where judge B earns its keep
        "n_judges_disagree": len(disagree),
        "n_excluded_all_differ": all_differ,
        "inherited_error_n": inherited,
        "inherited_error_rate": round(inherited / len(d_on_disagree), 4) if d_on_disagree else None,
        "d_sided_with_independent_judge_n": sided_with_a,
        "n_d_label_missing": d_missing,
    }


def label_distribution(rows: list[dict], key: str) -> dict:
    c = Counter(r[key] for r in rows if r.get(key) is not None)
    return {str(k): c[k] for k in sorted(c)}


def main() -> int:
    p = argparse.ArgumentParser(description="Score the dual-judge smoke test (Phase 0 task E).")
    p.add_argument("--judges", default="artifacts/smoke/judges.jsonl")
    p.add_argument("--d-labels", default="artifacts/smoke/d_labels.jsonl")
    p.add_argument("--out", default="artifacts/smoke/results.json")
    p.add_argument("--spot-check-out", default="artifacts/smoke/spot-check-50.jsonl",
                   help="50 docs where A != B, for the human spot-check §9.4 requires")
    p.add_argument("--samples-dir", default="artifacts/smoke/samples")
    p.add_argument("--seed", type=int, default=20260731)
    args = p.parse_args()

    judged = load_jsonl(Path(args.judges))
    d_rows = load_jsonl(Path(args.d_labels))
    if not judged:
        raise SystemExit(f"no judge labels at {args.judges} -- run judge.py first")
    d_labels = {r["doc_id"]: r.get("label") for r in d_rows}
    if not d_labels:
        print(f"[score] ⚠ no candidate labels at {args.d_labels}; reporting JUDGES ONLY "
              f"(J and the ceiling). The gate cannot be evaluated without D.", file=sys.stderr)

    by_source: dict[str, list[dict]] = defaultdict(list)
    for r in judged:
        by_source[r["source"]].append(r)

    per_source = {}
    for src in sorted(by_source):
        per_source[src] = score_source(by_source[src], d_labels)
        per_source[src]["judge_a_label_dist"] = label_distribution(by_source[src], "a")
        per_source[src]["judge_b_label_dist"] = label_distribution(by_source[src], "b")

    # Overall = pooled, not the mean of per-source rates (sources have different n).
    all_rows = [r for rows in by_source.values() for r in rows]
    overall = score_source(all_rows, d_labels)

    payload = {
        "task": "Phase 0 task E -- dual-judge smoke test",
        "taxonomy": "Free Decimal Correspondence Level 1 (10 categories) -- see SUBSTRATE.md; "
                    "the plan's '24-topic taxonomy' does not exist",
        "judge_a": "Qwen/Qwen3-235B-A22B-Instruct-2507 (independent)",
        "judge_b": "Qwen/Qwen2.5-72B-Instruct (PROXY for the real teacher Qwen2.5-32B-Instruct, "
                   "which has no enabled provider and does not fit the available A10G)",
        "candidate_d": "EssentialAI/EAI-Distill-0.5b (self-hosted; no inference provider)",
        "gate": GATE,
        "scoring": "score = accuracy(D) on {A == B}; J = agreement(A,B) is the CEILING",
        "per_source": per_source,
        "overall_pooled": overall,
        "any_source_below_gate": any(v["verdict"] == "FAIL" for v in per_source.values()),
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, indent=2) + "\n")

    # The human spot-check §9.4 requires: 50 docs where the judges disagree. This is what
    # distinguishes "the taxonomy is genuinely fuzzy here" from "one judge is broken," and it is
    # what makes the published accuracy number believable.
    texts = {}
    for f in Path(args.samples_dir).glob("*.jsonl"):
        for line in f.open():
            r = json.loads(line)
            texts[r["doc_id"]] = r["text"]
    disagreements = [r for r in judged
                     if r.get("a") is not None and r.get("b") is not None and r["a"] != r["b"]]
    rng = random.Random(args.seed)
    rng.shuffle(disagreements)
    with Path(args.spot_check_out).open("w") as f:
        for r in disagreements[:50]:
            f.write(json.dumps({
                "doc_id": r["doc_id"], "source": r["source"],
                "judge_a": r["a"], "judge_b": r["b"], "d": d_labels.get(r["doc_id"]),
                "text": texts.get(r["doc_id"], "")[:1200],
            }) + "\n")

    # ---- the §9.5 table, printed for the report ----
    w = sys.stdout.write
    w("\nDUAL-JUDGE SMOKE TEST\n")
    w(f"| {'source':<10} | {'J (A~B)':>8} | {'score (D on A==B)':>17} | {'inherited err':>13} | "
      f"{'n scored':>8} | {'n excl':>6} | verdict |\n")
    w("|" + "-" * 12 + "|" + "-" * 10 + "|" + "-" * 19 + "|" + "-" * 15 + "|"
      + "-" * 10 + "|" + "-" * 8 + "|---------|\n")
    for src, v in per_source.items():
        ie = "n/a" if v["inherited_error_rate"] is None else f"{v['inherited_error_rate']:.1%}"
        sc = "n/a" if not v["n_scored"] else f"{v['score']:.1%}"
        w(f"| {src:<10} | {v['J_judge_agreement']:>7.1%} | {sc:>17} | {ie:>13} | "
          f"{v['n_scored']:>8} | {v['n_excluded_all_differ']:>6} | "
          f"{v['verdict'] if v['n_scored'] else 'NO-D':<7} |\n")
    v = overall
    ie = "n/a" if v["inherited_error_rate"] is None else f"{v['inherited_error_rate']:.1%}"
    sc = "n/a" if not v["n_scored"] else f"{v['score']:.1%}"
    w(f"| {'POOLED':<10} | {v['J_judge_agreement']:>7.1%} | {sc:>17} | {ie:>13} | "
      f"{v['n_scored']:>8} | {v['n_excluded_all_differ']:>6} | "
      f"{v['verdict'] if v['n_scored'] else 'NO-D':<7} |\n")
    w(f"\nwrote {args.out} and {args.spot_check_out} "
      f"({min(50, len(disagreements))} disagreement docs for human review)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
