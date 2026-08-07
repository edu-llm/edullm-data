# HANDOFF — `pretrain/final-dataset` for M20

**Last updated 2026-08-07.** Read this file alone and you can continue with no other context.
Branch `final-dataset`, worktree
`/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/final-dataset`, based on
`origin/edullm/perf-threaded-verify-and-gatea` @ `390a34b` (**0.9.1 — the DEPLOYED code**).

> **Scope note.** This file covers the `final-dataset` corpus work only. The repo-wide `HANDOFF.md`
> next to it (2,243 lines, dated 2026-08-04) is inherited from the branch base and describes the
> *reservoir* build — it is still accurate about that and is deliberately left alone.

> ## ▶️ START HERE
>
> ### The role
>
> **Orchestrate this build autonomously via subagents.** Owner instruction, standing: keep agents to
> evidence-based numbers, question every assumption *including your own*, everything runs **on AWS**
> through the platform (landing → airlock validator → `edullm-data`), and **never auto-publish** —
> describe each job in plain terms, let the owner release it. Update the owner periodically.
>
> ### The three facts everything else depends on
>
> 1. **THE MODEL IS `M20`: 20,002,742,272 total / 1,279,369,216 active = 15.63:1.**
>    Pinned in `OLMo-core/src/olmo_core/nn/transformer/config.py:1125` (`MAPLE_RUNGS["M20"]`,
>    commented *"THE MISSION DELIVERABLE"*) and `:1166`. 256 experts, **top-8, ZERO shared**,
>    d_model 2048, L=24, GQA 16/4. `maple/HANDOFF.md:60-63`: *"Active is 1.279B, not 1.49B…
>    **Plan against 1.279B.**"*
>    ⚠️ The owner has described this verbally as "20B total / 2B active at 10:1" and earlier as
>    "40B/4B". **The code is authoritative** — 2B active is wrong by 56%. On the non-embedding
>    convention most model cards use it is 0.868B active = 22.56:1.
> 2. **BUDGET 1.0T.** Not 650B (the midpoint of two numbers is not an argument), not 1.3T (sized for
>    4B active). Rests on **Ling-mini-beta being nearly the same model** — 0.855B/17.5B against our
>    0.868B/19.59B on the same convention, **active matching to 1.6%, d_model identical** — which
>    reached *measured* parity with a dense 6.11B at exactly 1T tokens. Also the minimum disclosed
>    budget in the 14–22B-total class.
> 3. **⚠️ H100 IS NOT PROVISIONED.** `maple/HANDOFF.md` D-072: live `compute_profile` values are
>    T4 / L4 / L40S / A10G / `gpu-8xa100` only; both H100 shapes went unprovisioned after 6,815
>    capacity failures. **Every 400 TFLOP/s figure in reports 01–17 is 3.2–4.3× optimistic.** At the
>    real active count, 1T costs **$69.9k / 88.8 days on 8×A100** today versus $36.7k / 27.8 days on
>    H100. The owner is unblocking it — **that unblock is worth more than the entire 400B→1T
>    increment.**
>
> ### The corpus is TWO STAGES, not a flat mix — the biggest correction of the session
>
> No published comparable run used a flat web share. Verified at source in OLMo 2 §2.3
> (arXiv 2501.00656, read directly, not summarized): stage 1 is **"≥90% of training FLOPs"** and
> **"mostly web-sourced"**; stage 2 (5–10% of FLOPs) **"up-sample[s] the highest-quality web documents
> and curated non-web sources"** plus **"synthetic data crafted to patch math capabilities."**
> OLMo 2 stage 1 is 95.1% web; Olmo 3 is 76.1%; Olmix fixes web at 75%. Stage 2 drops to 27.5–52%.
>
> | | tokens | web share |
> |---|---|---|
> | **stage 1 (bulk)** | ~900B | **75–80%** |
> | **stage 2 (cooldown)** | ~100B | **30–45%** |
>
> An earlier draft used a **flat 54%**, which is ~20–40pp too LOW for the bulk and ~10–25pp too HIGH
> for the anneal. **Do not resurrect it.**
>
> ### The most useful measured result for stage 2 — and it answers the owner's QA ask
>
> **QA and worked examples pull our four headline metrics in OPPOSITE directions.** OLMo 3's
> compute-matched microanneals, each against a web-only control:
>
> | intervention | MMLU | ARC | GSM8K | Minerva | HumanEval |
> |---|---|---|---|---|---|
> | QA / MC-format (Table 8, 10B) | **+3.2** | **+2.6** | −1.2 | −1.6 | −1.5 |
> | reasoning traces (Table 9, 5B) | **−1.5** | +0.1 | **+8.4** | **+7.3** | **+11.6** |
>
> **They partially cancel. Any share recommendation treating them as one category is arithmetically
> wrong** — which is what the owner's "include QA and worked examples" ask must be translated into.
> QA saturates at **~14% of the anneal** (AI2 gave 13.9%; beyond it the curve is flat at −0.1 / +0.7).
>
> **Be honest about what the QA gain is.** AI2's QA is GPT-4o-mini-rewritten **multiple-choice** from
> academic subreddits — **format-matched to MMLU and ARC by construction.** It does *not* transfer to
> StackExchange (prose Q&A, 24.05B measured). Our own P1 §2.6 refuses to call this knowledge, and P1's
> collapse is why: **65.24% under QA-shaped prompts → 3.02% chat, 0.00% short-answer.**
>
> ⚠️ P1's worked-example data came from **MetaMathQA, an augmented rewrite of the GSM8K and MATH TRAIN
> sets.** Our own best evidence was produced on data derived from the benchmarks we steer on. The
> holdout was by problem *family*, so the result stands — but this is exactly the contamination vector
> n-grams cannot catch after augmentation.

---

## Goal

Build the pretraining corpus for **M20**, an education-focused MoE steered on MMLU, GSM8K, ARC and
HellaSwag, and publish it through this repo's airlock so it arrives validated, sliceable and labelled.
~1.0T tokens in two stages, tokenized with **dolma2**.

## Current Progress

**Nothing ingested. Nothing published. Nothing committed outside this worktree.** What exists here:

| file | state |
|---|---|
| `docs/FINAL-DATASET-REPORT.md` + `.pdf` | ⚠️ **STALE** — written for 40B/4B at 1.3T with a flat 54% web share. §1–§5 and §7 need rewriting for M20; **§6 (contamination) and §8 (what we got wrong) survive intact** |
| `docs/FINAL-DATASET-MIX.md` | ⚠️ same staleness (rev 2) |
| `scripts/measure_finephrase_overlap.py` + `.sbatch` + README | FarmShare census of FinePhrase id overlap. **Selftest passes locally.** Now confirmatory only — the overlap was measured directly (0.2683 distinct on a complete-column read of 287,000 ids) |
| `scripts/md2html.py` + `README-pdf.md` | Markdown→PDF via headless Chrome, since no converter is installed. Two commands in that README |

**17 research reports, 22,401 lines, in `artifacts/1t-research/` — on the MAIN checkout, not this
worktree.** Before touching the mix, read `16` (regime), `17` (the reversal red-team), `15`
(QA/worked examples), `11` (decontamination, 4,030 lines).

## What Worked

- **Demanding incremental disk writes in every subagent brief.** Four agents died mid-run on a
  Bedrock budget cap and **6,415 lines survived** because of it. Put this in every brief.
- **Reading code and primary sources rather than summaries.** Every significant correction came from
  opening the actual file: the M20 config, the conditional `seq_len` check, `build_mixture`'s fill
  loop, OLMo 2 §2.3, the SuperBPE tokenizer's empty `added_tokens`.
- **Agents that correct themselves.** The reversal red-team retracted its own replacement number
  mid-report and left the retraction visible; the MoE agent demoted its own #1 recommendation to #4.
  **Ask for this explicitly in the brief.**
- **Verifying peer claims instead of relaying them.** MegaMath checked out. The Qwen3 "36T" figure did
  not — it is a whole-family corpus size, not one model's budget.

## What Didn't Work — my own errors, each with the fix

| claim | what was wrong |
|---|---|
| "8192 shard alignment is mandatory" | `pretrain_tokens_v1.py:458-460` skips the check unless the group declares `seq_len`; the published corpus **does not**. Shard size is free |
| "50M shards cost 2.4%→4.8% p90 mixture error" | **False by ~15×.** Error is `1/shards_per_component`; worst real case 0.33%. **I used this to justify a decision** |
| "code caps at 74.81B" | `common-pile/stackv2` (full, ungated, content-bearing) was never checked. **~707B** |
| "77.5% Common Crawl is a problem" | Normal — Llama 1 was 82%, dolma3 76%. Retracted |
| "min_ngram 13→5 costs −11.8 MMLU" | That is a **deduplication** result (DCLM Table 19), not decontamination |
| "Nemotron Nano 2 has zero occurrences of 'decontamin'" | False. I stated it as verified |
| "Aryabumi brackets our scale" | Its code sweep ran **only at 470M**. Does not bracket 1.279B |
| "AutoScale shows optima shift with scale" | **AutoScale has no model-size axis at all**; its "6.8%→0.07%" figures are not in the paper |

**Three of the four claims that drove the web-led reversal do not hold** (`17-redteam-the-reversal.md`):
the "FineWeb authors recommend 50% FinePDFs" claim traces to a dataset card saying *"inspired by"* an
**unaffiliated community blog** using a 70M-param GPT-2 on 10–100M tokens — their actual published
guidance is *"keep PDF data below 25%"*; the NVIDIA "replication" deltas appear **nowhere in that
paper**, whose one *controlled* test shrinks the gap to **+0.6 with FineWeb-Edu winning** the 9-task
average; and my "18× the data, worse result" framing was **backwards** because the Nemotron anneal *is*
token-matched at 30% per arm. **DCLM-over-FineWeb-Edu survives in direction only — never quote the
magnitudes.**

## Key Decisions

- **Keep `allenai/dolma2-tokenizer`.** Survives even with re-tokenization free — no alternative clears
  zero measured downstream gain. And **`allenai/dolma3-tokenizer` IS dolma2** (AI2 says so at
  `mixes/__init__.py:97-98`), while **AI2's pre-tokenized shards are byte-identical to our
  `.u32le.bin`** — verified by range-read: no NumPy magic, valid ids <100,278 from byte 0. Their 5.93T
  is a **copy, not a re-tokenization.** ⚠️ Four of the nine reasons in `09-tokenizer-decision.md` are
  broken; `13-redteam-tokenizer-free.md` names them.
- **Shard size 50,003,968 tokens** → ~20,000 objects at 1T. Both of my objections retracted (above).
- **Math is ONE artifact.** Nemotron-CC-Math-3+ refetches WARCs for URLs harvested from FineMath +
  OpenWebMath + MegaMath, so it nearly *contains* them; taking it plus any of them double-counts.
- **Code ~15% of stage 1, 20–25% of the cooldown.** Aryabumi measured world knowledge at **−3.4% by
  25% code** — caveat, at 470M only.
- **Gate on MMLU and GSM8K directly, and use BPB not accuracy.** AI2 abandoned accuracy at 1B/100B as
  *"too difficult to show improvement"*; an accuracy harness at 1.279B active returns chance on every
  arm. Knowledge collapse is the most reproducible harm in this literature and **no loss-based gate
  sees it.**
- **Nobody earns a decontamination TRUST verdict** across 17 audited corpora. Tier 1 = exclude 9 items
  at source, **<1% of tokens, carrying nearly all the value.**

## Next Steps (priority order)

**Blockers — all pure code or measurement, no AWS approval needed:**

1. **Wire `keeps_id` into the build path** (task #4). It exists, is tested on 287,000 ids, and is
   called from nothing that writes data — so today's 59.6B of declared synthetic rests on ~17B of real
   documents. Verified on the deployed branch: `corpus_read/build/filter/pack.py` reference it **zero
   times**.
2. **Fix `MoELoadBalancingLossGranularity`** (task #19) — defaults to `local_batch` in four places. At
   M20's 15.63:1 with **zero shared experts**, Sigma-MoE-Tiny documents this as routing **collapse**.
   Cannot be annealed in: switching at 10% of training recovers only ~55%.
3. **Fix the missing EOS in `gigatoken-superbpe`** (task #12) — `added_tokens: []` makes Gate A's EOS
   check **skip silently** in two already-published corpora.
4. **Drop `data_provenance_initiative`** (task #16) — it ships `fc-cot-cot_gsm8k` (GSM8K in Flan CoT
   format) at 6 repeats and a ~9× cooldown upweight. **0.51% of tokens.**

**Then the three blocking measurements** (tasks #14, #17, plus a footer count): Dolma 3 adult-content
prevalence (a subagent saw explicit content at ~0.999 quality scores but **could not separate it from
HuggingFace preview ordering** — sample at random offsets, not the preview endpoint); re-run Marin's
13-gram scan on Nemotron-CC-Math post-PR-7051 (their published run **predates their own recall fix**
and found **11,868 contaminated docs against a <902-doc removal budget**, including verbatim GSM8K
*test* items at Jaccard 1.0); and a real dolma2 footer count for Nemotron-CC-Math, currently CARD-grade.

**Then rewrite the mix and the report** for M20 / 1.0T / two stages, and regenerate the PDF.

**Then ingest**, source by source, each as a separately-approved platform job.

## Background agents

All landed. Reports `01`–`17` in `artifacts/1t-research/`. **There is no `14`** — that brief hit the
concurrency cap and was never launched; it would have attacked the remaining pipeline claims, including
the untested Gate A extrapolation at ~20,000 objects and **whether an interleaved shard can still carry
per-source `labels`** (which decides if the micro-batch fix is data-side or trainer-side). `12` is
truncated at 202 lines by an API failure but its final table survived.

**Standing brief template for any new agent:** constraints lifted (licences off, re-tokenization free);
grade every number MEASURED-dolma2 / MEASURED-other / CARD / DERIVED / PROJECTED; never present a card
claim as measured; **write findings to disk incrementally**; *"nobody has measured this"* is a finding,
not a failure; do not fabricate a citation; and **read `artifacts/recount/*.json` first** — it already
holds dolma2-exact counts and re-measuring wastes a wave.

## Still unverified — flagged, not used

Gate A's cost at ~20,000 objects (my own extrapolation from a measured 85 min at 10,049, never tested);
Nemotron-CC-Math's 133B (CARD); FinePDFs-Edu's ~70B (byte-derived, after the recorded 161.07B was found
wrong by 2.3×); MegaMath-Web's post-filter pool size; and **the tension between an agent's "MMLU 26.9
at 4T" figure and OLMo 2 Table 2's 1B/100B DCLM model scoring MMLU 34.8–35.2 (CF format)** — both
cannot be casually true, and I did not resolve it.

**The largest residual risk in the whole plan: every mixture study cited across all 17 reports is
DENSE. No mixture ablation on a sparse MoE exists at any scale.** Transfer to M20 is UNVERIFIED, and
that unknown is larger than any error the red team found.
