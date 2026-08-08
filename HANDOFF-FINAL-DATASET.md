# HANDOFF — `pretrain/final-dataset`

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
> 1. **THE MODELS ARE A 96-EXPERT FLAGSHIP AND A 32-EXPERT BASELINE**, both with **2 shared + top-4
>    routed** experts:
>
>    | | flagship | baseline |
>    |---|---|---|
>    | total | **20.00B** | **7.11B** |
>    | experts | 96 (2 shared + top-4) | 32 (2 shared + top-4) |
>    | **active** | **1.876B** | **1.873B** |
>    | sparsity | 10.66:1 | 3.80:1 |
>
>    **Active params are 0.17% apart** — both activate 6 experts of identical width, so **per-token
>    compute is the same** and the baseline is cheaper only in MEMORY. The pair is an **expert-count
>    ablation at fixed active compute** (Clark 2202.01169's axis).
>    ⚠️ **Shape is DERIVED, not given:** d_model 2048 / 24 layers / expert intermediate 1366, solved so
>    96 experts land on 20.0B. **If the real shape differs, active params move and every token and cost
>    figure moves with them.** Ask the owner and re-derive before relying on any of it.
>    ⚠️ **Maple is a SEPARATE experiment — owner instruction, 2026-08-07. Do not read its configs or
>    cite its numbers.** Any `M20` / `1.279B-active` / `15.63:1` figure in an older note came from
>    there and is void.
> 2. **CORPUS ~1.0T unique; TRAINING 1.25–2.0T.** These are deliberately different numbers. ~1.0T is
>    what is sourceable at acceptable quality; the training floor comes from public precedent — every
>    disclosed model at ~1.9B active trained **1.25T–20T** (JetMoE 1.25T is the class floor;
>    Ling-mini-2.0 at 11.4:1, the closest ratio match to our 10.66:1, trained **20T**). 1.25–2.0T over
>    a 1.0T corpus is 1.25–2.0 epochs at ~98.4% average token value.
> 3. **⚠️ H100 IS NOT PROVISIONED.** Live `compute_profile` values are T4 / L4 / L40S / A10G /
>    `gpu-8xa100` only. **Every 400 TFLOP/s figure in reports 01–17 is 3.2–4.3× optimistic.** At the
>    real active count: **1.0T = $70k / 89 days on 8×A100**, 2.0T = $140k / 178 days. On H100 it would
>    be $37k / 28 days. **Unblocking H100 is worth more than any budget decision available to us.**
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
>
> ### ⚠️ AND THE MEASUREMENT ITSELF IS BROKEN BELOW ~400B TOKENS
>
> **OLMES (arXiv:2406.08446) Fig 1 caption, verbatim:** *"During early training, there is good signal
> from CF while MCF is random. **Around 400B tokens, the model starts gaining the ability on the MCF
> format.**"* Multiple-choice answering is a **separately-acquired skill.**
>
> This separates the uninformative nulls from the informative zeros across the whole literature — and
> it invalidates part of our own plan: **P1's probes ran 300M–10B tokens, far inside the dead zone**,
> so any MMLU column from a probe at that budget measures nothing. Score **CF (cloze)** or use **BPB**
> (which P1 in fact did — its 1.6062/1.6329 figures are bits-per-byte). The relationship **inverts**
> at the top end (93.7% MCF vs 69.0% CF on ARC-C), so this is scale-dependent, not a blanket rule.
>
> **Consequently the honest central estimate for adding QA data is MMLU +0.** The decisive experiment
> is Nemotron-CC (arXiv:2412.02595) at **8B/1T**, where MMLU is live (48–53) and synthetic QA still
> gives **+0.2 / −1.1**. The famous Nemotron-CC "+5.6 MMLU" is **classifier selection, not QA
> synthesis** — do not cite it for QA. A realistic 7B–13B-class band is MMLU +2 to +4 / ARC +2 to +7 /
> GSM8K +20 to +40, **minus the LR anneal, which alone gave +2.0 MMLU in OLMo 2** and is routinely
> misattributed to the data.
>
> **HellaSwag is negative in four independent papers** — QA data costs us there, and it is also where
> contamination persists longest while measuring 0.00% n-gram-dirty.

---

## Goal

Build the pretraining corpus for the **96-expert flagship and 32-expert baseline** (§START HERE),
education-focused MoEs steered on MMLU, GSM8K, ARC and
HellaSwag, and publish it through this repo's airlock so it arrives validated, sliceable and labelled.
~1.0T tokens in two stages, tokenized with **dolma2**.

## Current Progress

**Nothing ingested. Nothing published. Nothing committed outside this worktree.** What exists here:

| file | state |
|---|---|
| `docs/FINAL-DATASET-REPORT.md` + `.pdf` | ✅ **CURRENT — the plan of record for WHAT to build.** 13 sections, self-contained, first-time-reader framing. Its §9 is the baseline-subset recipe |
| **`docs/IMPLEMENTATION-PLAN.md` + `.pdf`** | ✅ **CURRENT — HOW to build it.** 25 pages, 11 sections. **Five blockers, none of which fails loudly.** Also carries the wall-clock (§8A), the gigatoken verdict (§7), and two of my own retracted claims |
| **`docs/BUILD-DEPENDENCY-GRAPH.md` + `.pdf`** | ✅ **CURRENT — WHEN to build each piece.** 33-node DAG, critical path **13.31 h**, and an orchestrator brief in §8 that can be handed to an agent verbatim |
| **`docs/TASKS.md`** | ✅ **NEW — the definition of every `#NN` id**, plus the crosswalk between task ids, graph nodes and Phase 0 items. These lived only in a session tool before, so three documents cited ids nothing defined |
| `artifacts/impl-plan/*.md` | 7 audit reports, **7,769 lines**. Evidence behind the plan. `orchestrator-findings.md` is the index of my own findings and corrections — **F2 and F4 now carry superseding banners** |
| `docs/FINAL-DATASET-MIX.md` | superseded stub pointing at the report; kept because older commits link to the filename |
| `scripts/measure_finephrase_overlap.py` + `.sbatch` + README | FarmShare census of FinePhrase id overlap. **Selftest passes locally.** Now confirmatory only — the overlap was measured directly (0.2683 distinct on a complete-column read of 287,000 ids) |
| `scripts/md2html.py` + `README-pdf.md` | Markdown→PDF via headless Chrome. **Now renders mermaid too** — `--virtual-time-budget=20000` is REQUIRED or the diagram is silently missing |

**17 research reports, 22,401 lines, in `artifacts/1t-research/` — on the MAIN checkout, not this
worktree.** Before touching the mix, read `16` (regime), `17` (the reversal red-team), `15`
(QA/worked examples), `11` (decontamination, 4,030 lines).

## What Worked

- **Demanding incremental disk writes in every subagent brief.** Four agents died mid-run on a
  Bedrock budget cap and **6,415 lines survived** because of it. Put this in every brief.
- **Reading code and primary sources rather than summaries.** Every significant correction came from
  opening the actual file: the conditional `seq_len` check, `build_mixture`'s fill
  loop, OLMo 2 §2.3, the SuperBPE tokenizer's empty `added_tokens`.
- **Agents that correct themselves.** The reversal red-team retracted its own replacement number
  mid-report and left the retraction visible; the MoE agent demoted its own #1 recommendation to #4.
  **Ask for this explicitly in the brief.**
- **Verifying peer claims instead of relaying them.** MegaMath checked out. The Qwen3 "36T" figure did
  not — it is a whole-family corpus size, not one model's budget.
- **Reading an agent's EVIDENCE section, not its summary.** The dedup audit's notification said the
  decontamination rule is "tuned to report clean" and the code must change; its own `F13` graded the
  same finding *"cosmetic-to-quality-degrading"* and said nobody has measured the alternative. **When a
  summary outruns its evidence section, the evidence section is right.** Its key citation also appeared
  nowhere in the artifact — `grep` settled it.
- **Making agents audit each other.** The wall-clock audit demolished a headline blocker I had already
  written into the plan and opened a task for; the source audit corrected my boundary-marker advice.
  Neither would have surfaced from one pass. **Four of six audits corrected something of mine.**
- **Simulating a defect instead of describing it.** "Adding a source shifts ordinals" is easy to
  under-rate; running the real stage-1 mix through `allocate_ordinals` produced *"98% of shards, 882B
  tokens, 23 h of tokenize"* — which is what made it the plan's first blocker.
- **Checking whether execution reaches the code I was reading.** Adopted only after it caught me twice
  (see below). A budget constant is not a byte count until a consumer drains it.

## What Didn't Work — my own errors, each with the fix

| claim | what was wrong |
|---|---|
| "8192 shard alignment is mandatory" | `pretrain_tokens_v1.py:458-460` skips the check unless the group declares `seq_len`; the published corpus **does not**. Shard size is free |
| "50M shards cost 2.4%→4.8% p90 mixture error" | **False by ~15×.** Error is `1/shards_per_component`; worst real case 0.33%. **I used this to justify a decision** |
| "code caps at 74.81B" | `common-pile/stackv2` (full, ungated, content-bearing) was never checked. **~707B** |
| "77.5% Common Crawl is a problem" | Normal — Llama 1 was 82%, dolma3 76%. Retracted |
| "min_ngram 13→5 costs −11.8 MMLU" | That is a **deduplication** result (DCLM Table 19), not decontamination |
| "Nemotron Nano 2 has zero occurrences of 'decontamin'" | False. I stated it as verified |
| "Aryabumi brackets our scale" | Its code sweep ran **only at 470M**. Does not bracket ~1.9B active |
| "AutoScale shows optima shift with scale" | **AutoScale has no model-size axis at all**; its "6.8%→0.07%" figures are not in the paper |

**Three of the four claims that drove the web-led reversal do not hold** (`17-redteam-the-reversal.md`):
the "FineWeb authors recommend 50% FinePDFs" claim traces to a dataset card saying *"inspired by"* an
**unaffiliated community blog** using a 70M-param GPT-2 on 10–100M tokens — their actual published
guidance is *"keep PDF data below 25%"*; the NVIDIA "replication" deltas appear **nowhere in that
paper**, whose one *controlled* test shrinks the gap to **+0.6 with FineWeb-Edu winning** the 9-task
average; and my "18× the data, worse result" framing was **backwards** because the Nemotron anneal *is*
token-matched at 30% per arm. **DCLM-over-FineWeb-Edu survives in direction only — never quote the
magnitudes.**

### From the 2026-08-07 implementation review — four more of mine, caught by my own audits

**Read these before trusting a number in `IMPLEMENTATION-PLAN.md`; two of them were in that document
as findings until an audit refuted them.**

| claim of mine | what was wrong |
|---|---|
| **"The pipeline reads 18 TB to fetch 4.21 TB"** — a headline blocker, with a task opened | **Wrong twice over.** `val_fraction` **cancels** out of the read algebraically (val tokens `want×VF` over divisor `VF`), and the budget is a **ceiling never reached** — verified on the real run, 26 of 27 bundles filled every shard. So the `_CHARS_PER_TOKEN` fix I proposed saves **zero bytes** and starves bundles if applied. Real over-read is 2.02× |
| **"Gate A is 6 round trips per object"** | It is **8**. 8 gives 15.76 rt/s against the measured 15.8, and a call-counting spy recorded 80,392 trips for 10,049 objects. I had "explained" the gap with LISTs that *were* the missing calls |
| **"Extend the boundary-marker rewrite table"** | Under dolma2 **only the end-of-text token is a document boundary**; the other 21 added tokens are ordinary ids. I conflated a quality concern with a corpus-splitting one. The single-entry table is correct — the real defect is the two-character prefix guard, which makes any addition a silent no-op |
| **`stackv2-edu` as the OOM example** | Used a stale 155 B/entry figure that predates the int narrowing. At the current 85.9 B it fits; **`synthetic-finephrase-table` (225.6M docs) is the one that OOMs.** Conclusion unchanged, example wrong |

### From the 2026-08-07 consistency audit — the owner found 23 more, and 3 were substantive

**The owner cross-checked the four documents by arithmetic and found 23 inconsistencies. All 23 verified.**
Most were denominators or stale labels. **Three changed a number somebody would have acted on:**

| what was wrong | mechanism |
|---|---|
| **`BUILD` = 6.6 h was unreachable, and the graph said so without noticing** | 6.6 h is the *aggregate* floor at 128 vCPU. Per-child duration is that child's tokens ÷ its own vCPU — and `--shard/--of` strides **bundles**, so DCLM's 410B is one child at **10.85 h even on a whole 32-vCPU instance**. The graph marked the fix DEFERRABLE while depending on it. **Critical path 13.31 h → 21.31 h**, and the file-shard is a prerequisite, not an optimization |
| **§5's OOM table scaled the wrong mix, and omitted the worst bundle** | It scaled the *reservoir* (23.82% synthetic) instead of this corpus (4.3%). Redone with each source's own measured tok/doc, the worst bundle is **DCLM at 325M docs / 27.92 GB**, not `finephrase-table` at 19.37 GB — **1.44× worse, and absent from the table**, because the reservoir drew only 29.8B of DCLM. `orchestrator-findings.md` F4 had named DCLM correctly with the wrong arithmetic; I propagated the arithmetic and dropped the bundle |
| **Two cost figures 1.46× apart, in one document** | §2 said 400B ≈ $41k/52 d; §11 said 1.0T = $70k/89 d. Both imply **$32.8/hr**, so it is a throughput disagreement, not a price one. §11 survives (it postdates the H100 retraction). §11 is now labelled the single anchor |

**Plus one that was invisible rather than wrong: `#NN` task ids appeared in three documents and were
defined in none of them** — they lived only in a session task tool. Now `docs/TASKS.md`, which is also the
crosswalk between task ids, graph nodes and Phase 0 items. **Three numbering schemes with no crosswalk is
how B5 and B6 got left out of Phase 0 entirely** while the graph treated both as gating.

**The lesson, and it is the same one twice now:** every one of the three substantive errors is a
**denominator or scope error** — a per-child figure read as aggregate, a reservoir mix read as this mix, a
throughput read as a price. None was a wrong measurement. **When two numbers for one quantity disagree,
check what each divides by before deciding which is stale.**

**And one process point worth keeping:** the graph's own §9 said *"durations for code items are estimates"*
and *"`BUILD`'s 6.6 h assumes linear vCPU scaling."* Both caveats were present and neither caught the
defect, because the problem was not the estimate — it was that **6.6 h and "C3b is deferrable" could not
both be true**, and nothing checked pairs of claims against each other. A caveat on a number does not
protect against an inconsistency between numbers.

**And one agent claim I rejected rather than relayed:** an audit asked me to retract a citation because
"arXiv:2604.13977 does not exist." It resolves — full record, 12 authors, COLM 2026. The fetch tool had
flagged its April 2026 date as "the future"; today is 2026-08-07. **"This looks like it's from the
future" is a claim about the checker's calendar, not the paper.**

**The transferable lesson**, and it is the same one `INGEST-CALIBRATION.md` teaches about the reservoir:
**a formula is not a measurement, and a model that needs a hand-waved remainder to match a measurement
is not a model yet.** Both of my retracted numbers came from reading code arithmetic without checking
whether execution reaches it.

## Key Decisions

- **Keep `allenai/dolma2-tokenizer`.** Survives even with re-tokenization free — no alternative clears
  zero measured downstream gain. And **`allenai/dolma3-tokenizer` IS dolma2** (AI2 says so at
  `mixes/__init__.py:97-98`), while **AI2's pre-tokenized shards are byte-identical to our
  `.u32le.bin`** — verified by range-read: no NumPy magic, valid ids <100,278 from byte 0. Their 5.93T
  is a **copy, not a re-tokenization.** ⚠️ Four of the nine reasons in `09-tokenizer-decision.md` are
  broken; `13-redteam-tokenizer-free.md` names them.
- **Shard size 25,001,984 tokens** (`SHARD_TOKENS = 3052 × 8192`, `corpus.py:89`) → **~40,000 objects** at
  1.0T. **Corrected 2026-08-07:** this entry previously said 50,003,968 → ~20,000, a value that appeared in
  no commit; the report said one thing and the code another, and eight places carried the disagreement. The
  companion "0.33% worst-case mixture error" was the same quantity at the withdrawn size — at the real size
  it is **0.007%–0.278%**. Both of my original objections stay retracted; the *constant* was the error.
- **Math is ONE artifact.** Nemotron-CC-Math-3+ refetches WARCs for URLs harvested from FineMath +
  OpenWebMath + MegaMath, so it nearly *contains* them; taking it plus any of them double-counts.
- **Code 10% of stage 1, 18% of the cooldown** (the report's tables are authoritative). Aryabumi
  measured reasoning peaking near 25% code while **world knowledge is already −3.4% at 25%** and −31%
  at 75% — so code belongs late and concentrated. Caveat: that sweep ran only at 470M, so it does not
  bracket our ~1.9B active.
- **Gate on MMLU and GSM8K directly, and use BPB not accuracy.** AI2 abandoned accuracy at 1B/100B as
  *"too difficult to show improvement"*; an accuracy harness at ~1.9B active returns chance on every
  arm. Knowledge collapse is the most reproducible harm in this literature and **no loss-based gate
  sees it.**
- **Nobody earns a decontamination TRUST verdict** across 17 audited corpora. Tier 1 = exclude 9 items
  at source, **<1% of tokens, carrying nearly all the value.**
- **Two stages, not a flat mix** — 900B bulk at 77% web, 100B cooldown at 32%. Verified against OLMo 2
  §2.3 directly. A flat share is wrong in *shape*, not just level.
- **QA and worked examples are SEPARATE categories**, because they move our headline metrics in
  opposite directions: MC-format QA is **+3.2 MMLU / −1.2 GSM8K**, reasoning traces are **−1.5 MMLU /
  +8.4 GSM8K**. A single blended share would be arithmetically wrong. QA saturates at **~14% of the
  anneal**. And the honest MMLU expectation from QA data is **+0** — the one experiment above the
  ~400B MCF threshold (8B/1T) gives +0.2/−1.1.
- **The baseline model reuses the same corpus** at a different ratio vector (report §9) — shed
  knowledge-shaped data, raise reasoning-shaped. **No second dataset, no re-ingest.**

### Build-side decisions, from the 2026-08-07 review (details in `IMPLEMENTATION-PLAN.md`)

- **Publish TWO datasets**, `pretrain/final-stage1-900b` and `-stage2-100b`, not one. `build_mixture`
  is scoped to one group of one dataset and `PATH_LABEL_KEYS` is exactly two levels deep, so "stage"
  cannot be a third path segment. A cooldown is sequential anyway, so nothing needs a mixture spanning
  both — and stage 2 at 4,000 objects **validates in 0.56 h today with no code change.**
- **Interleaving is TRAINER-side, and this is now settled.** `labels` is one dict per shard path, and
  Gate A recomputes it from the key by full dict equality, so an interleaved shard **cannot** carry
  per-source labels. The micro-batch fix is `MoELoadBalancingLossGranularity`, not the data.
- **Keep 13-gram / `minimum_hits=2` decontamination** and document the divergence from the design doc's
  5-gram. Two audits split on this; the asymmetry decides it — a false negative leaves one benchmark
  item in 1T, while a false positive at 5-gram risks the mechanism that cost DCLM 11.8 MMLU. **Fix the
  5-shot-render defect first: it breaks decontamination at any `n`.**
- **Do not put gigatoken on the critical path for this build.** Its pretokenizer regex is
  byte-identical to dolma2's and the repo's own parity fixture is semantically our tokenizer — but CI
  runs **no tests**, Unicode divergence from HF is WONTFIX, and tokenization is ~$38 and not the
  bottleneck. Gate it (`IMPLEMENTATION-PLAN.md` §7.5) and revisit for the *next* build, where
  re-tokenization is the point.
- **One agent owns one FUNCTION, never one file.** Five code items edit `corpus_build.py`; inside it
  they cluster into `_reader_for` and `run_bundle`. This is the binding constraint on parallelism —
  file contention, not logic.
- **Batch all pre-job code into ONE image.** Images build only from `edullm/**`, and a measured
  comparison put a two-image scheme **0.1 h worse** because the second build lands on the critical path.

## Next Steps

> **⚠️ SUPERSEDED — do not follow the old "ingest source by source" advice.** A 2026-08-07 review found
> that literally executing it **discards 98% of the work on every added source** (see blocker 1 below).
> **`docs/BUILD-DEPENDENCY-GRAPH.md` §8 has the launch order as an orchestrator brief.** Read that
> instead of reconstructing one from this list.

**The five blockers, from `IMPLEMENTATION-PLAN.md` §0. None of them fails loudly** — each either
silently discards work or produces a corpus that passes every gate while being wrong.

| # | blocker | consequence | fix | task |
|---|---|---|---|---|
| 0 | **⚠️ NEW — one bundle cannot be split, so `BUILD` is 16.8 h not 6.6 h** | `--shard/--of` strides *bundles*, so DCLM's 410B is ONE child — **10.85 h even given a whole 32-vCPU instance.** The graph's 6.6 h floor was unreachable | plan-time ordinal ranges, **or** give DCLM a synthetic `domain_column` so it fans out for free | **#28** |
| 1 | **Ordinals shift when a source is added** | one 4B source renames **98% of shards**, voids **882B tokens** | freeze the FULL plan first — **0 code** | #20 |
| 2 | **The FinePhrase de-dup predicate is never called** | the report already specifies the fix (36B from **one** partition); the code cannot express it. Draw 36B from all four and exposure returns at ~2.4× | ~5 lines, at the reader | #21 |
| 3 | **The dedup set OOMs at 1T** | **DCLM needs 27.92 GB** in a 15.03 GB container — 1.44× worse than the bundle the plan named, and it was missing from the plan's table entirely | flat `np.uint64` → 2.60 GB | #22 |
| 4 | **The decon index is built from 5-shot RENDERS** | 149,777 exact hashes are **dead** for MMLU/ARC/HellaSwag; and `minimum_hits=2` needs **≥14 words**, which ≥5% of items in the 11- and 12-word suites do not have | rebuild from raw fields | #24 |
| 5 | **43% of bytes moved is the val split** | serving 0.39% of tokens | file-shard val bundles | #25 |

**Plus two that would embarrass us:** `tokenizers` is **not a declared dependency** while
`corpus_build.py:631` imports it, so production resolves whatever PyPI served that morning (#23); and
every Cosmopedia document begins with a leading space, which changes its first token under byte-level
BPE (MEASURED 303/303 across all 8 configs).

**Run FIRST, before trusting any duration:** measure **in-region S3 and HF CDN bandwidth** (#26, ~10
min). Every read estimate borrows ~85 MB/s from an *S3* measurement; one reconciliation of the real
build implies the HF CDN may be **~8.4 MB/s**. If so, staging becomes the most valuable decision in
the plan.

**Then the blocking measurements:** Dolma 3 adult-content prevalence at **random offsets** (#14 — a
prior attempt could not separate signal from HuggingFace preview ordering); and mean document length for 5
unmeasured stage-2 sources.

✅ **Nemotron-CC-Math's TOKEN COUNT is DONE, by a teammate with gate access.** (This closes the graph's
**M3** node. It is **not** task #17, which is the 13-gram contamination re-scan and is still open — now
unblocked, since the gate is accepted and the bytes are staged.) **134.0B under dolma2** =
472,213,218,716 uncompressed text bytes (exact footers) × 0.283686 tok/byte over 1,920 random-offset
documents at seed 42; `3` ≈ 83.6B + `4plus` ≈ 50.4B. Artifact
`_nemotron_cc_math_dolma2_measure.json`. **No `CARD` figure remains in either mix table.** Two follow-ups
that are not the count: **write down the exact `text_column` and id column** before the registry row exists,
and **keep `4plus_MIND` out of the pool** (it is a rewrite of `4plus`; including both double-counts).

**Then**: freeze the mix → generate the plan → stage sources → **mandatory single-bundle smoke test**
(`_reader_for` has never run against live HuggingFace from a Batch container, and the code says so) →
build in waves → publish as **two datasets** → Gate A → `verify --deep` → promote.

**Wall-clock: ~10 h of job time, ~36 h as-configured, and a 21.31 h critical path** — revised up from
13.31 h on 2026-08-07 because that figure assumed `BUILD` = 6.6 h with blocker 0 unfixed, which is not
achievable. With #28 done the path is **21.31 h**; without it, **23.54 h**. Two stages also don't merely run
slowly today — Gate A at 5.6 h and `verify --deep` at 13.0 h each **exceed their job timeouts**, so the
corpus could not be promoted at all.

**Task ids `#NN` are defined in `docs/TASKS.md`** — they used to live only in a session task tool, so three
documents referenced ids that a fresh agent could not resolve. That file is also the crosswalk to the graph
nodes (`C3b`, `A2a`, `B5`…) and the Phase 0 item numbers.

Regenerate any PDF after editing with the commands in `scripts/README-pdf.md`.

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
DENSE. No mixture ablation on a sparse MoE exists at any scale.** Transfer to our architecture is
UNVERIFIED, and
that unknown is larger than any error the red team found.
