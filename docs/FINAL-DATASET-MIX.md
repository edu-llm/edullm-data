# `pretrain/final-dataset` v1 — the locked mix

**Status: AWAITING OWNER APPROVAL.** Nothing is ingested until this document is approved.
Branch `final-dataset`, worktree `../Capstone_LLM-worktrees/edullm-data/final-dataset`,
based on `origin/edullm/perf-threaded-verify-and-gatea` @ `390a34b` (**0.9.1, the DEPLOYED code**).

## Target

**~1,000B tokens** (owner decision, 2026-08-07), **50,003,968-token shards** → **19,998 objects**,
**4.00 TB**, **~$92/month**. Shard size = 6,104 × 8,192; the 8,192 alignment is mandatory.

## The mix

| category | tokens | share | design target | delta | to ADD |
|---|---|---|---|---|---|
| edu-web | 359.0B | **35.9%** | 25.5% | +10.4 | +311.3B |
| web-diverse | 209.7B | **21.0%** | 16.0% | +5.0 | +179.9B |
| synthetic | 181.1B | **18.1%** | 29.0% | −10.9 | +164.1B |
| math | 86.8B | 8.7% | 7.0% | +1.7 | +53.0B |
| code | 74.8B | 7.5% | 11.0% | −3.5 | +35.0B |
| academic | 46.6B | 4.7% | 4.5% | +0.2 | +26.8B |
| QA/forum | 25.9B | 2.6% | 4.5% | −1.9 | +14.2B |
| reference | 16.0B | 1.6% | 2.5% | −0.9 | +8.1B |
| **total** | **1,000.0B** | | | | **+792.4B** |

**This is NOT the design mix, and it cannot be.** Measured upstream ceilings cap an exact-mix corpus
at **576B**, binding on QA/forum (25.9B ÷ 4.5%). Above that, the only categories with headroom are
the two web pools, so a 1T corpus is web-heavy **by arithmetic, not by choice**. Every category
except edu-web/web-diverse/math is **at its ceiling** — there is no more of it in existence under an
acceptable licence.

Defensible on prior evidence: every downstream-validated small-model mix lands 75–90% web. But it is
a *different* corpus from the designed one, so **the mixture must be re-fitted by probe runs**
(P1's best-evidenced lever) rather than assumed.

## Sources, with the trap on each

| category | source | licence | provenance | trap |
|---|---|---|---|---|
| edu-web | FineWeb-Edu full (~1,293B) + FinePDFs-Edu-EN (161B) | odc-by, ungated | MEASURED dolma2 | **cap FinePDFs at 10% of any RUN** — already 1.74× past its measured optimum |
| web-diverse | DCLM-baseline dedup (732.6B) | card says *"research purposes"* | MEASURED via 0.9764 ratio | licence tag does not show that clause; read before any public release |
| synthetic | FinePhrase (weighted `keeps_id` partition) + Cosmopedia 25B + Nemotron Math-Textbooks 25.1B | odc-by / apache-2.0 / CC-BY-4.0 | MEASURED dolma2 | see §synthetic below — the whole reason for the blocker |
| math | MegaMath-Web (263.9B) | odc-by, **ungated** | CARD — **no tokenizer named** | **REPLACES FineMath, does not add** (both fastText CC math). Documents **NO decontamination**; FineMath does |
| code | `common-pile/stackv2_edu_filtered` (74.81B) | per-document permissive | MEASURED | the ONLY ungated content-shipping permissive code pool |
| academic | peS2o + PubMed + arXiv | mixed | MEASURED | **20.14B measured overlap** — 49.7% of peS2o bytes are PMC-derived |
| QA/forum | StackExchange | **92.8% CC-BY-SA** | MEASURED | only 1.87B is non-SA |
| reference | Wikipedia EN 8.87B + rest | ~90% CC-BY-SA | MEASURED | hard ceiling ~16B |

**Excluded, with cause:** more `finepdfs-edu` (past optimum) · `stack-edu` 125B (**no licence field
at all**) · Stack-v2 proper (**HTTP-401 gated**, ships SWHIDs not content) · swallow-code-v2 (74%
`no_license`) · Nemotron-CC main (§2.2.2 forbids "making available to others") · Nemotron-STEM-SFT
(**MMLU-contaminated** — GSM8K/MATH/AOPS-seeded, reformatted MMLU-style, zero decontam) ·
Nemotron-CC-Math (gated + `licence: other`) · CK-12 (**bans AI training**).

## Synthetic: why 181.1B and not 198.6B

The four FinePhrase configs are **one corpus rephrased four ways** over the same ~339M FineWeb-Edu
documents — 90.3–93.2% pairwise id overlap, ~28.5% distinct across all four. Nothing downstream
catches it: four rephrasings give four digests (exact dedup passes), MinHash misses paraphrase, and
every token count adds up.

Three options, measured:

| option | distinct tokens | format diversity |
|---|---|---|
| take one config (`faq`) | 148.5B | **none** — one format only |
| equal 4-way `keeps_id` partition | 119.5B | full |
| **weighted partition 35/35/15/15** | **131.0B** | **full** |

**Chosen: the weighted partition.** It costs 17.5B against single-config but keeps all four formats.
That matters because P1 measured QA-formatting gains as **format-dependent** — 65.24% under QA-shaped
prompts collapsing to 3.02% (chat) and 0.00% (short-answer). A single-format synthetic pool teaches
one retrieval interface. `partition_of(doc_id, n)` already accepts arbitrary `n`, so the weighting
needs no new mechanism.

**Second collision, also handled by the same module:** FinePhrase rephrases FineWeb-Edu, which
edu-web *also* draws. Untreated, one document can appear as real edu-web text **and** as its own
rephrasing in a single run. That is the anti-join half of §9.7 item 4.

## Blocker — nothing ingests before this

**`reservoir_ids.keeps_id` is implemented, tested on 287,000 ids, and never wired in.** Verified on
the deployed 0.9.1 branch: its only callers are `_cmd_plan` and `_cmd_ids`;
`corpus_read.py`, `corpus_build.py`, `corpus_filter.py` and `corpus_pack.py` reference it **zero
times**. It must run at ingest — after tokenization there is no document→id mapping left, so redoing
it means re-tokenizing the synthetic half.

## Open items before ingest

1. **Re-measure the FinePhrase overlap.** 28.5% comes from HF revision `78cf4a5e`, 4×1000 sampled
   ids. Every synthetic number here scales off it.
2. **MegaMath needs a dolma2 footer count.** Its 263.9B is CARD-grade with no tokenizer named; ±5–15%.
3. **Quantify MegaMath-Web ↔ FineMath overlap.** Union is bounded [263.9B, 298.6B], probably near the
   low end. Treat as replacement.
4. **Decontamination.** MegaMath documents none, and this pipeline has none of its own. The synthetic
   half is already effectively undecontaminated (rephrasing defeats n-gram matching).
5. **Pipeline at ~20,000 objects** — the 50M shard choice halves it from 40k, but promotion (not
   Gate A) is what SIGKILLed the last build at 6,324/10,051.
