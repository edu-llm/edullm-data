#!/usr/bin/env python3
"""Label harvested documents with the two JUDGES (A and B) over FDC Level 1, via AWS Bedrock.

Phase 0 task E, judge half, of DATASET-DESIGN-reservoir.md §9.4. The candidate model D runs
separately (`classify_d.py`, on Batch GPU — it has no inference provider at all). This script
produces A and B labels; `score.py` combines all three into the gate table.

READ `SUBSTRATE.md` FIRST. Three things in the plan needed correcting, all verified live:
  1. There is no "24-topic taxonomy." Essential-Web publishes the **Free Decimal Correspondence**,
     **12 main categories**, whose **Level 1 has 10 values**. "24" is the paper's TOKEN count.
  2. `Qwen2.5-32B-Instruct` (the real teacher, and the plan's judge B) has no enabled HF provider
     and does not fit the one available A10G.
  3. **HF Inference credits are exhausted** — every router call returns HTTP 402 ("You have
     depleted your monthly included credits"). So the substrate is **Bedrock**, not HF.

## Why Bedrock is a straight upgrade, not a workaround

| role | plan | actual | why it is better or equal |
|---|---|---|---|
| **A** independent | `Qwen3-235B-A22B-Instruct-2507` (HF) | `qwen.qwen3-next-80b-a3b` | Same family and generation as the plan's judge, reachable, and no per-call credit ceiling |
| **B** teacher proxy | `Qwen2.5-32B-Instruct` (HF, unreachable) | `qwen.qwen3-32b-v1:0` | **A 32B dense Qwen — the teacher's exact parameter count.** Closer to the real teacher than the 72B HF sibling I first fell back to |

Judge B exists for exactly one purpose: detecting **inherited error**, where D reproduces a bad
teacher label and a single-judge design scores that as a *success*. For that, B must share the
teacher's biases — so matching its size and family matters more than raw capability, and a 32B
dense Qwen is the closest available instrument.

Both are still **proxies** — neither is literally `Qwen2.5-32B-Instruct` — so the measured
`inherited_error_rate` is an estimate. The `J = agreement(A, B)` ceiling is measured on the pair
actually used, so the gate arithmetic is sound regardless of which pair that is.

## The scoring subtlety that makes or breaks the gate

Score D **on the consensus subset {A == B}**, not against both judges jointly. Requiring D to
match both caps the achievable score at the judges' own agreement rate: if A and B agree only 75%
of the time, an 85% bar is *mathematically unreachable* for reasons that have nothing to do with D.

    J     = agreement(A, B)                      <- the measurement CEILING. Report it.
    score = accuracy(D) restricted to {A == B}    <- THE GATE
    inherited = P(D == B AND A != B)              <- what judge B exists to expose

## Prompt design

Both judges get the **same** prompt: the 10 FDC Level 1 labels, the document, and an instruction to
answer with one digit.

- **Identical prompts** — any difference between judges shows up as disagreement and deflates J,
  which is the ceiling the gate is measured against.
- **A digit, not a word** — free-text labels invite synonyms ("Sciences", "science and tech") that
  need fuzzy matching, and fuzzy matching is where a scoring harness quietly invents agreement.
- **`temperature=0`** — this is a measurement; sampling noise would be indistinguishable from real
  disagreement.
- **Qwen3 models emit `<think>` blocks unprompted** (observed: a probe for "OK" returned
  `<think>Okay, the user wants...`). `parse_label` strips them before parsing, and `max_tokens` is
  large enough that a truncated think block does not eat the answer.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Free Decimal Correspondence, Level 1.
#
# ⚠️ CLASS 0 IS NOT JUST "GENERAL WORKS" -- READ THIS BEFORE EDITING.
#
# The essential-web-v1.0 card abbreviates class 0 as "General works". That abbreviation is
# incomplete, and taking it literally invalidates the whole measurement. In Dewey (which FDC
# mirrors), class 0 is "Computer science, information & general works", and **computing lives at
# 005.x -- inside class 0, not class 6**.
#
# Measured cost of the omission, on a real 2,000-document run: the judges, told only "General
# works", sent every programming document to 6 (Technology), while the candidate model correctly
# emitted 005.x -> 0. On qa-forum (StackOverflow-heavy) that alone drove the score from 95.7% to
# **3.3%**, and the pooled score from 81.4% to 49.1% -- a total artifact of the prompt, with the
# model behaving correctly throughout.
#
# So each label below carries enough scope to disambiguate. This is not embellishment; it is what
# the taxonomy actually means, and the judges cannot agree with the candidate without it.
FDC_L1 = {
    0: "Computer science, information, programming, software, data, and general reference works",
    1: "Philosophy and psychology",
    2: "Religion",
    3: "Social sciences, economics, law, education, politics",
    4: "Language and linguistics",
    5: "Natural sciences and mathematics",
    6: "Technology and applied sciences (engineering, medicine, agriculture, business practice)",
    7: "Arts and recreation",
    8: "Literature",
    9: "History and geography",
}

# Path-segment form for the eventual `domain` label (§1.2), pinned here so the smoke test and the
# real run cannot drift apart on spelling.
FDC_L1_SLUG = {
    0: "general-works", 1: "philosophy", 2: "religion", 3: "social-sciences", 4: "language",
    5: "science", 6: "technology", 7: "arts", 8: "literature", 9: "history-geography",
}

JUDGE_A = "qwen.qwen3-next-80b-a3b"   # independent judge
JUDGE_B = "qwen.qwen3-32b-v1:0"       # teacher proxy: 32B dense, matches the real teacher's size

CATEGORY_BLOCK = "\n".join(f"{k} = {v}" for k, v in FDC_L1.items())

PROMPT = """Classify this document's PRIMARY SUBJECT MATTER using the Free Decimal Correspondence top level.

{categories}

Rules:
- Choose the single best category for the document's main subject.
- Judge the subject matter, not the format or the writing quality.
- You are shown only the beginning of the document.
- Answer with ONE digit (0-9) and nothing else.

Document:
---
{document}
---

Answer with one digit:"""

_print_lock = threading.Lock()


def make_client():
    import boto3
    from botocore.config import Config

    # The broker's isolated credential_process profile. One boto3 client with retries beats one
    # broker call per document by ~3 orders of magnitude (see project memory on bulk S3).
    if "AWS_CONFIG_FILE" not in os.environ and Path("/tmp/olmo150_aws/config").exists():
        os.environ["AWS_CONFIG_FILE"] = "/tmp/olmo150_aws/config"
        os.environ.setdefault("AWS_PROFILE", "sbsandbox")
    return boto3.client(
        "bedrock-runtime", region_name="us-east-1",
        # Bedrock throttles hard under concurrency; adaptive retries handle it in-SDK rather than
        # surfacing as label failures that would bias the gate's subset.
        config=Config(retries={"max_attempts": 8, "mode": "adaptive"}, read_timeout=120),
    )


def parse_label(raw: str) -> int | None:
    """First standalone digit 0-9, after stripping any reasoning block.

    Refuses to guess at prose -- an unparseable reply must stay None so it shows up as a failure
    rather than a silent 0 (which would be a real category, 'General works')."""
    if not raw:
        return None
    # Qwen3 emits <think>...</think> unprompted; a digit inside it is reasoning, not the answer.
    cleaned = re.sub(r"<think>.*?</think>", " ", raw, flags=re.S)
    cleaned = re.sub(r"<think>.*$", " ", cleaned, flags=re.S)  # unterminated (hit max_tokens)
    m = re.search(r"\b([0-9])\b", cleaned)
    if m:
        return int(m.group(1))
    m = re.search(r"([0-9])", cleaned)
    return int(m.group(1)) if m else None


def call_judge(client, model: str, document: str, tries: int = 4) -> tuple[int | None, str]:
    """Return (label, raw). label is None when the call failed or the reply didn't parse."""
    last = ""
    for attempt in range(tries):
        try:
            r = client.converse(
                modelId=model,
                messages=[{"role": "user", "content": [{"text": PROMPT.format(
                    categories=CATEGORY_BLOCK, document=document)}]}],
                # Generous: Qwen3 may spend tokens on a think block before answering.
                inferenceConfig={"maxTokens": 512, "temperature": 0},
            )
            raw = "".join(b.get("text", "") for b in r["output"]["message"]["content"]).strip()
            return parse_label(raw), raw
        except Exception as e:  # noqa: BLE001 - throttling and transient 5xx both land here
            last = f"{type(e).__name__}: {e}"
            time.sleep(3 * (attempt + 1))
    return None, last[:200]


def main() -> int:
    p = argparse.ArgumentParser(description="Label samples with judges A and B (Phase 0 task E).")
    p.add_argument("--samples-dir", default="artifacts/smoke/samples")
    p.add_argument("--out", default="artifacts/smoke/judges.jsonl")
    p.add_argument("--limit", type=int, default=None, help="cap docs per source (for a dry run)")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--judge-a", default=JUDGE_A)
    p.add_argument("--judge-b", default=JUDGE_B)
    p.add_argument("--only", default=None, help="comma-separated source keys")
    args = p.parse_args()

    files = sorted(Path(args.samples_dir).glob("*.jsonl"))
    if args.only:
        want = set(args.only.split(","))
        files = [f for f in files if f.stem in want]
    if not files:
        raise SystemExit(f"no sample files in {args.samples_dir}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Resume support: this machine has died mid-run before (CLAUDE.md), and re-judging costs money.
    done: set[tuple[str, str]] = set()
    if out_path.exists():
        for line in out_path.open():
            try:
                r = json.loads(line)
                # Only count it as done if at least one judge actually produced a label --
                # otherwise a rate-limited run would permanently poison the sample.
                if r.get("a") is not None or r.get("b") is not None:
                    done.add((r["source"], r["doc_id"]))
            except Exception:  # noqa: BLE001 - truncated last line is expected after a crash
                pass
        print(f"[judge] resuming: {len(done)} already judged", file=sys.stderr)

    jobs = []
    for f in files:
        rows = [json.loads(l) for l in f.open() if l.strip()]
        if args.limit:
            rows = rows[: args.limit]
        jobs += [r for r in rows if (r["source"], r["doc_id"]) not in done]
    print(f"[judge] {len(jobs)} documents x 2 judges = {len(jobs)*2} Bedrock calls", file=sys.stderr)
    print(f"[judge] A={args.judge_a}  B={args.judge_b}", file=sys.stderr)

    client = make_client()
    out_f = out_path.open("a")
    write_lock = threading.Lock()
    counters = {"done": 0, "a_fail": 0, "b_fail": 0}

    def work(rec: dict):
        la, ra = call_judge(client, args.judge_a, rec["text"])
        lb, rb = call_judge(client, args.judge_b, rec["text"])
        row = {
            "source": rec["source"], "doc_id": rec["doc_id"],
            "a": la, "b": lb, "a_raw": ra[:60], "b_raw": rb[:60],
            "n_tokens_prefix": rec.get("n_tokens_prefix"),
        }
        with write_lock:
            out_f.write(json.dumps(row) + "\n")
            out_f.flush()          # persist continuously -- a crash must not lose paid-for calls
            counters["done"] += 1
            counters["a_fail"] += la is None
            counters["b_fail"] += lb is None
            if counters["done"] % 50 == 0:
                with _print_lock:
                    print(f"[judge]   {counters['done']}/{len(jobs)} "
                          f"(A fail {counters['a_fail']}, B fail {counters['b_fail']})",
                          file=sys.stderr)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work, jobs))
    out_f.close()

    n = max(1, len(jobs))
    print(f"[judge] wrote {out_path}", file=sys.stderr)
    print(f"[judge] A failures: {counters['a_fail']}  B failures: {counters['b_fail']}", file=sys.stderr)
    if counters["a_fail"] > n * 0.05 or counters["b_fail"] > n * 0.05:
        print("[judge] ⚠ >5% judge failures -- the gate would be measured on a biased subset. "
              "Investigate before scoring.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
