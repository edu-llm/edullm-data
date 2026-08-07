# `pretrain/final-dataset` v1 — the locked mix

**Status: AWAITING OWNER APPROVAL.** Nothing is ingested until this is approved.
Branch `final-dataset`, worktree `../Capstone_LLM-worktrees/edullm-data/final-dataset`,
based on `origin/edullm/perf-threaded-verify-and-gatea` @ `390a34b` (**0.9.1, the DEPLOYED code**).

**Revision 2, 2026-08-07.** Rev 1 was built on four card-grade figures. All are now measured
(`artifacts/1t-research/05-...md`, `06-...md`). **The design mix is achievable at 1T** — which rev 1
said it was not — because multi-epoching three short categories costs almost nothing.

## Target

**~1,000B tokens**, **50,003,968-token shards** (= 6,104 × 8,192; the alignment is mandatory)
→ **19,998 objects**, **4.00 TB**, **~$92/month**.

## The mix — DESIGN SHARES, achieved

| category | share | tokens | distinct pool | epochs | token value | to ADD |
|---|---|---|---|---|---|---|
| synthetic | 29.0% | 290.0B | 172.5B | **1.68** | 99.1% | +155.5B |
| edu-web | 25.5% | 255.0B | 1,583.1B | 1.00 | 100% | +207.3B |
| web-diverse | 16.0% | 160.0B | 744.6B | 1.00 | 100% | +130.2B |
| code | 11.0% | 110.0B | 74.8B | **1.47** | 99.5% | +35.0B |
| math | 7.0% | 70.0B | 241.1B | 1.00 | 100% | +36.2B |
| academic | 4.5% | 45.0B | 46.6B | 1.00 | 100% | +25.1B |
| QA/forum | 4.5% | 45.0B | 25.9B | **1.74** | 99.0% | +14.2B |
| reference | 2.5% | 25.0B | 26.2B | 1.00 | 100% | +17.1B |
| **total** | **100%** | **1,000.0B** | | | **−0.35%** | **+620.6B unique** |

**Three categories repeat, none past 1.74 epochs, for a mix-weighted cost of 0.35%.** Five run at
exactly one epoch. Rev 1's "1T cannot hold the design mix" was a **one-epoch** claim; at ≤2 epochs on
the short categories the exact mix reaches **1,133B**.

Muennighoff 2305.16264 fits R_D\* = 15.3878: a 2nd-epoch token is worth 98.4% of a fresh one.
⚠️ **That law was fit on DENSE models — zero MoE runs, verified.** MoE's larger memorization capacity
points toward a *smaller* R_D\*, so treat every epoch multiplier as an **upper bound**.

## Sources — every figure below is MEASURED-dolma2 unless marked

| category | source | tokens | licence | trap |
|---|---|---|---|---|
| edu-web | FineWeb-Edu full | **1,583.1B** | odc-by, ungated | card's 1.3T is **18% STALE** (13 crawls added). Cap FinePDFs at 10% of any RUN — already 1.74× past its measured optimum |
| web-diverse | DCLM-baseline dedup | **744.6B** | `cc-by-4.0` tag | card **BODY** says *"research use only"* in 3 places, never in the tag. Two independent measurement routes agree to **0.05%** |
| synthetic | FinePhrase weighted partition | **123.3B** | odc-by | overlap is **~1.00 pairwise**, not 0.90–0.93. Distinct fraction **0.2683** |
| | Cosmopedia | **21.72B** | apache-2.0 | card is a **Mistral-7B** count, 15% high. Decontaminated vs **8** benchmarks |
| | Nemotron Math-Textbooks | **27.49B** | CC-BY-4.0, ungated | card 9.5% LOW. Repo is `nvidia/Nemotron-Pretraining-Specialized-v1` (rev 1 had a nonexistent id). Passes through **Qwen model-licence** obligations |
| math | MegaMath-Web | **241.05B** | odc-by, ungated | card's 263.9B was **8.7% optimistic**. **No decontamination documented — assume contaminated.** Union with FineMath bounded [241.1B, 297.9B] |
| code | `common-pile/stackv2_edu_filtered` | **74.81B** | per-doc permissive | the ONLY ungated content-shipping permissive code pool. **1.0002 epochs at target — budget 2 epochs of a filtered subset** |
| academic | peS2o + PubMed + arXiv | **46.6B** | mixed | minus a **measured 20.14B** overlap (49.7% of peS2o bytes are PMC-derived) |
| reference | finewiki 8.87B + pre-1929 books **17.36B** | **26.23B** | SA + **PUBLIC DOMAIN** | books card is **72% LOW** (OCR tokenizes at 0.375, not the asserted 0.25). SA share falls ~90% → **34%** |
| QA/forum | StackExchange | **25.9B** | **92.8% CC-BY-SA** | confirmed hard ceiling; only 1.87B non-SA |

**Excluded, with cause:** Stack-v2 proper (**HTTP-401 gated**, ships SWHIDs) · `stack-edu` 125B (**no
licence field**) · **MegaMath-code 28.1B** (*ships zero content* — 16/16 files metadata only, 135
bytes/doc) · swallow-code-v2 (74% `no_license`) · `algebraic-stack` 9.72B (**licence UNRESOLVED**) ·
Nemotron-CC main (§2.2.2 "making available to others") · Nemotron-STEM-SFT (**MMLU-contaminated**) ·
Nemotron-CC-Math (gated) · CK-12 (**bans AI training**) · `uspto_filtered` 144B (CARD only, 0.65%
coverage) · essential-web-v1.0 24T (*this was rev 1's unexplained figure — a different dataset*).

## Synthetic: the decision that got harder

The four FinePhrase configs are **the same document set, four times, exactly.** A complete-column read
of 287,000 ids gives pairwise overlap **0.99876–1.00000** and distinct **0.2683** — agreeing with the
prior 4,000-id sample to 6%. Corroborated by the card's own scale: 1,354,044,711 outputs ÷
339,347,842 sources = **3.99 rephrasings per document**.

**So "take one config" is not one option among three — it is the CEILING on distinct documents**
(union exceeds the largest config by **2 ids out of 287,000**).

| option | distinct | format diversity |
|---|---|---|
| `faq` single-format | **148.5B** | none |
| **weighted partition 35/35/15/15** | **123.3B** | full |
| equal 4-way partition | 112.4B | full |

**Chosen: the weighted partition**, and the 1.68 epochs above absorbs the 25.2B gap. Justification is
P1's measured format-dependence — QA-shaped gains of 65.24% collapse to 3.02% (chat) and **0.00%**
(short-answer). A single-format pool teaches one retrieval interface.
**Re-run the partition weights against 0.2683, not 0.285.**

⚠️ **Do NOT multi-epoch synthetic beyond this** — repetition compounds with the ~1.0 cross-format
overlap. The 1.68 figure already assumes the partition is applied.

**Edu-web collision is real but cheap:** FinePhrase rephrases `sample-350BT`, which is only **22.1%**
of FineWeb-Edu — leaving **1,394.8B collision-free** against a 255B target. Avoidable at **zero token
cost**; the anti-join is belt-and-braces. Untreated, ~22B appears twice undetectably.

## Blocker — nothing ingests before this

**`reservoir_ids.keeps_id` is implemented, tested on 287,000 ids, and wired into nothing.** Verified
on this branch: only `_cmd_plan` and `_cmd_ids` call it; `corpus_read/build/filter/pack.py` reference
it **zero times**. It must run at ingest — after tokenization there is no document→id mapping left.

## Remaining unknowns, stated rather than filled in

1. **MegaMath-Web contamination vs GSM8K/MATH/MMLU** — undocumented and unmeasured. Assume dirty.
2. **What fraction of MegaMath-Web survives a FineMath-grade filter** — identifiable spam at
   classifier 0.67–0.73; admits documents to 0.40. Post-filter pool size unmeasured.
3. **Whether R_D\* = 15.39 transfers to an MoE** — it does not, directionally. Upper bound only.
4. **The overlap read is one file position** (`000_00000_0.parquet`) of ~6,780 per config; the
   mis-aligned control failed (`paths-info` IndexError). Graded MEASURED because the effect is
   exactly 1.0 on six pairs and a file artifact would show partial overlap.
5. **This pipeline applies no decontamination of its own.**
