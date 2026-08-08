#!/usr/bin/env python3
"""Label harvested documents with candidate model D — `EssentialAI/EAI-Distill-0.5b`.

Phase 0 task E, candidate half, of DATASET-DESIGN-reservoir.md §9.4. Runs on **Batch GPU**
(`g5.xlarge`, one A10G) because this model has **no HF inference provider** — verified:
`inferenceProviderMapping` is empty, so it must be self-hosted. At 0.5 B params / ~1 GB fp16 it
fits the available A10G with room to spare.

Pairs with `judge.py` (judges A and B, via the HF router) and `score.py` (the gate table).

## The output format is NOT a topic label — read this before parsing anything

The model does not emit a category name. Per its card's "Output Format" section it emits **ten
comma-separated fields**, one per line:

    {FDC primary},{FDC secondary or skip}
    {Bloom cognitive process primary (1-6)},{... or skip}
    {Bloom knowledge domain primary (1-4)},{... or skip}
    {Document type v1 primary (1-17)},{... or skip}
    {Extraction artifacts primary (0-4)},{... or skip}
    {Missing content primary (0-6)},{... or skip}
    {Document type v2 primary (1-25)},{... or skip}
    {Reasoning depth primary (1-6)},{... or skip}
    {Technical correctness primary (1-6)},{... or skip}
    {Educational level primary (1-5)},{... or skip}

We want **line 1, primary, first digit** — the FDC code, whose leading digit is Level 1 (0–9).
`SUBSTRATE.md` explains why Level 1 is the right granularity for a `domain` path segment, and why
the plan's "24-topic taxonomy" does not exist.

An FDC code may be `5`, `53`, or `530` (Level 1 / 2 / 3), and may be `-1` for **Abstain**. So:
take the first character, reject a leading `-`, and keep everything else the model emits as
`raw_all` — the other nine fields are genuinely useful later (`educational_level`,
`reasoning_depth`, and `extraction_artifacts` are all filter candidates) and re-running 112 M
documents to recover a field we threw away would be absurd.

## The prompt is fixed by the model, not chosen by us

The card's usage example passes a system message of the literal string `"taxonomy"` and the
document as the user turn, via the chat template. That is what the model was trained on. Do not
"improve" it — a different prompt measures a different model.

Long documents: the card's own `chunk_text` takes head + a random middle + tail for anything over
30k chars. Our inputs are 256-token prefixes (§9.3 task D), far under that, so chunking never
fires here. It is reproduced faithfully anyway so this script stays correct if reused for the full
run, where documents ARE long.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path


def chunk_text(text: str, max_char_per_doc: int = 30000, rng: random.Random | None = None) -> str:
    """Verbatim from the model card, with the RNG made injectable so a run is reproducible.
    (The card calls the global `random`; seeding matters when the middle window is random.)"""
    if len(text) <= max_char_per_doc:
        return text
    rng = rng or random.Random(0)
    chunk_size = max_char_per_doc // 3
    start = text[:chunk_size]
    middle_start = chunk_size
    middle_end = len(text) - chunk_size
    mid_point = rng.randint(middle_start + chunk_size // 2, middle_end - chunk_size // 2)
    middle = text[mid_point - chunk_size // 2: mid_point + chunk_size // 2]
    end = text[-chunk_size:]
    return f"[beginning]\n{start}\n[middle]\n{middle}\n[end]\n{end}"


def parse_fdc_level1(raw: str) -> tuple[int | None, str]:
    """Extract FDC Level 1 from the model's 10-line output.

    Returns (level1, reason). `level1` is None for abstain/unparseable, and `reason` says which --
    the two must stay distinguishable, because a model that ABSTAINS a lot is a different problem
    from one that emits garbage, and they imply different decisions."""
    lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
    if not lines:
        return None, "empty-output"
    first = lines[0]
    primary = first.split(",")[0].strip()
    if not primary:
        return None, "empty-primary"
    if primary.lstrip().startswith("-"):
        return None, "abstain"          # -1 = "Abstain: Unable to classify", per the card
    for ch in primary:
        if ch.isdigit():
            return int(ch), "ok"        # leading digit of an FDC code IS its Level 1
        if ch not in " \t":
            break
    return None, f"unparseable:{primary[:12]!r}"


def main() -> int:
    p = argparse.ArgumentParser(description="Label samples with candidate D (Phase 0 task E).")
    p.add_argument("--samples-dir", default="artifacts/smoke/samples")
    p.add_argument("--out", default="artifacts/smoke/d_labels.jsonl")
    p.add_argument("--model", default="EssentialAI/EAI-Distill-0.5b")
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--max-new-tokens", type=int, default=100)   # card's example uses 100
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--only", default=None)
    p.add_argument("--device", default=None, help="cuda | cpu; default auto")
    p.add_argument("--seed", type=int, default=20260731)
    args = p.parse_args()

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[classify-d] device={device} model={args.model}", file=sys.stderr)
    if device == "cpu":
        print("[classify-d] ⚠ running on CPU -- fine for a 2,500-doc smoke test, "
              "hopeless for the 112M-doc full run", file=sys.stderr)

    tok = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        args.model, dtype=torch.float16 if device == "cuda" else torch.float32,
    ).to(device).eval()
    # Left padding: with a decoder-only model, right padding puts PAD between the prompt and the
    # first generated token, so the model continues from padding and the output is garbage.
    tok.padding_side = "left"
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    files = sorted(Path(args.samples_dir).glob("*.jsonl"))
    if args.only:
        want = set(args.only.split(","))
        files = [f for f in files if f.stem in want]
    if not files:
        raise SystemExit(f"no sample files in {args.samples_dir}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    if out_path.exists():
        for line in out_path.open():
            try:
                done.add(json.loads(line)["doc_id"])
            except Exception:  # noqa: BLE001 - truncated final line after a crash
                pass
        print(f"[classify-d] resuming: {len(done)} already labelled", file=sys.stderr)

    recs = []
    for f in files:
        rows = [json.loads(l) for l in f.open() if l.strip()]
        if args.limit:
            rows = rows[: args.limit]
        recs += [r for r in rows if r["doc_id"] not in done]
    print(f"[classify-d] {len(recs)} documents to label", file=sys.stderr)

    rng = random.Random(args.seed)
    out_f = out_path.open("a")
    stats = {"ok": 0, "abstain": 0, "bad": 0}
    t0 = time.time()

    for i in range(0, len(recs), args.batch_size):
        batch = recs[i: i + args.batch_size]
        prompts = [
            tok.apply_chat_template(
                [{"role": "system", "content": "taxonomy"},
                 {"role": "user", "content": chunk_text(r["text"], rng=rng)}],
                tokenize=False, add_generation_prompt=True,
            )
            for r in batch
        ]
        enc = tok(prompts, return_tensors="pt", padding=True, truncation=True,
                  max_length=16384).to(device)   # card: sequence length 16,384
        with torch.no_grad():
            gen = model.generate(**enc, max_new_tokens=args.max_new_tokens,
                                 do_sample=False,                 # a measurement, not a sample
                                 pad_token_id=tok.pad_token_id)
        # Slice off the prompt so `raw` is only what the model generated.
        new = gen[:, enc["input_ids"].shape[1]:]
        for r, ids in zip(batch, new):
            raw = tok.decode(ids, skip_special_tokens=True)
            lvl, reason = parse_fdc_level1(raw)
            stats["ok" if reason == "ok" else ("abstain" if reason == "abstain" else "bad")] += 1
            out_f.write(json.dumps({
                "doc_id": r["doc_id"], "source": r["source"],
                "label": lvl, "parse": reason,
                "raw_all": raw.strip()[:400],   # keep all 10 fields -- see the docstring
            }) + "\n")
        out_f.flush()   # persist continuously; this machine has died mid-run (CLAUDE.md)
        if (i // args.batch_size) % 5 == 0:
            el = time.time() - t0
            n = i + len(batch)
            print(f"[classify-d]   {n}/{len(recs)} ({el:.0f}s, {n/max(el,1e-9):.1f} doc/s) "
                  f"ok={stats['ok']} abstain={stats['abstain']} bad={stats['bad']}", file=sys.stderr)
    out_f.close()

    total = sum(stats.values())
    print(f"[classify-d] done: {total} labelled in {time.time()-t0:.0f}s -> {out_path}", file=sys.stderr)
    print(f"[classify-d] ok={stats['ok']} abstain={stats['abstain']} unparseable={stats['bad']}",
          file=sys.stderr)
    if total and stats["bad"] / total > 0.05:
        print("[classify-d] ⚠ >5% unparseable -- check the chat template and the output format "
              "before trusting the gate", file=sys.stderr)
        return 2
    if total and stats["abstain"] / total > 0.20:
        print("[classify-d] ⚠ >20% abstain -- the model is declining to classify these documents, "
              "which is a finding in itself (it was trained on resiliparse-extracted web text)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
