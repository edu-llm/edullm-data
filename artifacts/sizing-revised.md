# Task F — §2.1 pool sizing against measured counts

Phase 0 task F. Recomputes the §2.1 table against what task A actually measured, and flags any pool
now below 3× peak plausible demand.

**Headline: 5 of 8 pools are verified met. One is verified NOT met. Two are unverified.** The
sizing does not collapse — but §2.1 cannot be called confirmed, and one category needs a design
decision before the first publish.

## The table

"Measured" means a token count derived from real bytes under the dolma2 tokenizer. "Card" means a
published figure — and for every Common Pile source, published figures are `Size(GB) × 0.25`, pure
arithmetic with no tokenizer involved (§8 of `PLAN-CORRECTIONS.md`), so they are not evidence about
any tokenizer.

| category | plan pool | 3× peak | measured | verdict | basis |
|---|---|---|---|---|---|
| **edu-web** | 48 B | 37.2 B | **261.3 B** | ✅ **MET 7.0×** | finepdfs-edu 161.1 B + fineweb-edu 100.24 B, both measured |
| **web (diverse)** | 30 B | 21 B | **114.69 B** | ✅ **MET 5.5×** | `dclm_100BT`, exact rows, `partial: false` |
| **math** | 36 B | 21 B | **34.69 B** | ⚠️ **3× met (4.96×), pool short 3.6%** | finemath-3plus alone; everything else in the category overlaps it |
| **academic** | 20 B | 12 B | *unmeasured* | ⚠️ **UNVERIFIED, not at risk** | peS2o alone is 182.6 GB of UTF-8; missing 12 B would need >15.2 bytes/token |
| **code** | 40 B | 24 B | *unmeasured* | ⚠️ **UNVERIFIED, likely met** | stackv2_edu desk floor 61–80 B from 83.0 GB × 0.274 tok/char |
| **QA/forum** | 12 B | 7.2 B | *unmeasured* | ⚠️ **UNVERIFIED, likely met** | stackexchange_filtered ~23.9 B; survives a pessimistic 0.21 tok/byte bound at ~18.8 B |
| **reference** | 14 B | 9 B | **8.87 B** | ❌ **NOT MET** | finewiki/en, footer-exact; 1.5% short of even the 3× floor |
| **synthetic** | 60 B (4×15 B) | 18 B | *unmeasured* | ⚠️ **UNVERIFIED, ≥6× headroom** | two independent non-sampling routes agree within 3–9% of card |

## The one real failure: reference

`finewiki/en` measures **8.87 B**, not the "~3.5 B" in §3.2 (that figure appears nowhere on the card,
which names no token count and no tokenizer) — but also not 14 B. It is **63% of the plan's pool and
1.5% below the 9 B three-times floor.**

Three ways to reach 14 B, and they are not equivalent:

| option | tokens | cost |
|---|---|---|
| all-wiki (add `wikimedia_filtered`, `wikiteam_filtered`) | ~16 B | **~90% share-alike** — defeats §7 item 4's separability goal |
| public-domain only (`pre_1929_books` + `gutenberg`) | ~14.5 B | no SA, but factually stale for an *encyclopedic* pool |
| **two labelled partitions: `reference-sa` ~9 B + `reference-pd` ~5 B** | **~14 B** | recommended — hits the pool *and* makes separability structural |

Per §1.1's constraint (`build_mixture` resolves exactly one group), those must be **`source` label
values inside one group**, not separate groups.

**If the pool must be encyclopedic AND current AND English AND redistributable, 14 B does not exist.
The honest ceiling is ~9 B.** That is a design decision, not a measurement problem.

## Math is 3.6% short, and the shortfall is structural

The plan lists four math sources. Measured, they are **one lineage**:

- `finemath-4plus` ⊂ `finemath-3plus` — a subset, cannot be summed
- `swallow-math-v2` is an LLM **rewrite** of FineMath-3+ — near-duplicate, so *substitute*, don't
  dedupe (rephrasing defeats document dedup, and it silently voids FineMath's own 13-gram
  decontamination)
- `infiwebmath` overlaps FineMath by the FineMath card's own admission ("Deduplicating the pages
  repeated between FineMath and InfiWebMath reduces performance"), and HF does **not** net that out
  of the advertised 54 B
- `algebraic-stack` (math *code*) is the one genuinely additive source at ~11 B card

So the defensible non-overlapping total is **34.69 B = finemath-3plus alone**, which clears 3× peak
demand 4.96× but misses the 36 B pool by 1.31 B. Adding `algebraic-stack` would clear it — its
license is *not a grant* though ("we do not alter the license of any of the underlying data" = the
union of 17 languages' GitHub repos), so that is a licensing decision.

## Two lineage collisions the plan does not account for

**1. `dclm-edu` ⊂ DCLM-baseline.** The plan's `web (diverse)` pool exists as "the diversity
counterweight to edu filtering" (§2.1), but `dclm-edu` is a strict subset of DCLM, so the two pools
are **not independent**. To get a real counterweight, draw from the DCLM documents that the edu
classifier *rejected* (`edu_score ≤ 2`), or source edu-web from FineWeb-Edu instead. Measured, not
inferred: `olmo-mix-1124` is **99.9% DCLM by document count** (3,033,948,632 of 3,035,925,377 rows).

**2. `github_archive_filtered` is claimed by two categories and is neither.** §3.2 lists it under
**code**, but its rows are GitHub issue/PR/comment **prose** (`source` = `gharchive/issue`). It
belongs in QA/forum. Two agents found this independently. Whichever category keeps it, the other's
total drops.

## What would settle the unverified pools

All four unmeasured categories need the same thing, and it is cheap: **a footer-bytes scan or a ~1%
shard stream, run in-region on Batch.** The method that worked is worth reusing — reading parquet
footers off the hub CDN gives *exact* whole-split column bytes with **zero** datasets-server quota,
and it caught two bad numbers during Phase 0 (a 60-doc sample put finepdfs-edu doc length 68% high;
`/statistics` was partial at 6.4% of finewiki's rows with 32% estimator divergence).

For Common Pile sources specifically, the parquet mirror is `common-pile/raw_v0.1_parquet` — not
`comma_v0.1`, which is `.json.gz` and has no footers to read.

**Cost estimate: well under $10 and about an hour.** None of it is on the far side of the §9.1 hard
stop, so it can be done before the ~$595 decision rather than after.
