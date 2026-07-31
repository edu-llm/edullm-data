# Task F — §2.1 pool sizing against measured counts

Phase 0 task F. Recomputes the §2.1 table against what task A actually measured, and flags any pool
now below 3× peak plausible demand.

**Headline: 6 of 8 pools verified met, 2 unverified, none failing.** `reference` was the one failure and
was **resolved 2026-07-31** by resizing the pool 14 B → 9 B (owner decision, below). The four categories
that measured 0.00 B are being re-counted now by the footer method (§"What would settle the unverified
pools"), so this file will move again.

**Reservoir total: 260 B → 255 B**, which makes the design doc's `reservoir-260b-dolma2` name wrong by
5 B. Flagged there as a decision to take once the re-counts land — the name is part of the address and
cannot be changed without republishing.

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
| **reference** | **9 B** ✅ *(was 14 B)* | 7.2 B → **2.4 B** | **8.87 B** | ✅ **MET 3.70×** | finewiki/en, footer-exact. Resolved by resizing the POOL, not by padding it |
| **synthetic** | 60 B (4×15 B) | 18 B | *unmeasured* | ⚠️ **UNVERIFIED, ≥6× headroom** | two independent non-sampling routes agree within 3–9% of card |

## reference — RESOLVED 2026-07-31 by owner decision: the pool is 9 B

`finewiki/en` measures **8.87 B** by exact parquet-footer bytes — 2.5× the "~3.5 B" §3.2 claimed (a
figure that appears nowhere on the card, which names no token count and no tokenizer), but well under
14 B.

**The owner resized the pool to 8.87 B → 9 B rather than padding it to 14 B.** Three options were on
the table and only one avoided a worse problem:

| option | tokens | what it costs |
|---|---|---|
| all-wiki (add `wikimedia_filtered`, `wikiteam_filtered`) | ~16 B | **~90% share-alike** — defeats §7 item 4's separability goal across the whole category |
| public-domain only (`pre_1929_books` + `gutenberg`) | ~14.5 B | no SA, but factually **stale** for an encyclopedic pool |
| split `reference-sa` ~9 B + `reference-pd` ~5 B | ~14 B | hits the number, but half the pool is pre-1929 books answering a *different* question than "encyclopedia" |
| ✅ **CHOSEN: a 9 B pool, max share 15% → 12%** | **8.87 B** | forecloses runs wanting >12% reference. Nothing else changes |

**Why this is the right trade.** The alternatives all reach 14 B by adding material that is either
share-alike-encumbered or not encyclopedic — so the pool would hit its number while being *worse* at
the thing it exists for. Sizing to what exists keeps the category honest.

The max-share change is forced arithmetic, not taste: at 8.87 B a 15% share means 3.0 B peak demand and
**2.96× headroom**, which would have made reference the only row in §2.1 violating that document's own
≥3× invariant — by 1.3%, the kind of miss that survives review because it looks fine. 12% gives 3.70×.

**The default stays 5%**, so no run anyone was likely to configure changes. What narrows is the ceiling
on a reference-heavy experiment. That region is one no downstream-validated study points at anyway —
12% is already 7.5× RegMix's measured Wikipedia optimum of 1.6%.

*Had a bigger pool been reachable without those costs, the governing principle (over-provision, because
a small pool forecloses permanently) would have said take it. It wasn't.*

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
