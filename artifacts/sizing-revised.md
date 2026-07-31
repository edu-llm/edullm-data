# Task F — §2.1 pool sizing against measured counts

Phase 0 task F. Recomputes the §2.1 table against what task A actually measured, and flags any pool
now below 3× peak plausible demand.

**Headline: ALL 8 POOLS MEASURED, all 8 clear their 3× floor.** Phase 0c finished 2026-07-31. Seven
clear their nominal pool outright; `math` clears its floor 4.96× but sits 3.6% under its nominal 36 B,
and `reference` was resized 14 B → 9 B by owner decision rather than padded. **No category is short of
usable data.** Phase 0's four `0.00 B` readings were all rate-limit artifacts, not scarcity.

Every figure below is `exact whole-split text bytes (parquet footers or gzip ISIZE) × sampled dolma2
tokens/byte`. No datasets-server `/rows` calls, no downloads, all tokens/byte CVs under 0.25.

⚠️ **Two things Phase 0c corrected that this file previously asserted:**

1. **Common Pile card figures are not conservative floors.** The `Size(GB) × 0.25` assertion errs in
   *both* directions — measured tok/byte is peS2o 0.2212 (so 0.25 is **13% too high**), pubmed 0.2556,
   arxiv 0.3265 (**23% too low**). peS2o's card of 43.3 B against a measured **40.48 B** means the card
   is **7% HIGH**. `PLAN-CORRECTIONS.md` §8 previously said the error direction was "at least
   favourable"; it isn't, and no single multiplier corrects it.
2. **The peS2o∩pubmed overlap is real and large** — **49.7% of peS2o's bytes are PubMedCentral-derived**,
   per its own per-document `metadata.pdf_src`. This file called it a hazard to "check"; it is now
   measured and netted out below.

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
| **academic** | 20 B | 12 B | **64.12 B** | ✅ **MET 3.2× (5.3× the floor)** | footer-exact; non-overlapping after dropping peS2o's **measured 49.7% PMC share** (naive sum would be 84.26 B) |
| **code** | 40 B | 24 B | **74.81 B** | ✅ **MET 1.87× (3.12× the floor)** | `stackv2_edu_filtered` ALONE; ISIZE over all 95 shards × 0.2938 tok/byte. Excludes swallow (a rewrite of the same blobs) and github_archive (not code) |
| **QA/forum** | 12 B | 7.2 B | **25.93 B** | ✅ **MET 2.16× (3.60× the floor)** | stackexchange 24.05 B + ubuntu_irc 1.87 B. ⚠️ **92.8% share-alike** — drop SA and it is 1.87 B, which FAILS even peak demand |
| **reference** | **9 B** ✅ *(was 14 B)* | 7.2 B → **2.4 B** | **8.87 B** | ✅ **MET 3.70×** | finewiki/en, footer-exact. Resolved by resizing the POOL, not by padding it |
| **synthetic** | 60 B (4×15 B) | 18 B | **478.15 B** | ✅ **MET 8.0× (26× the floor)** | exact nested-leaf footers; faq 148.54 / tutorial 147.92 / math 94.74 / table 86.95 |

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

**2. `github_archive_filtered` — RESOLVED: it is QA/forum, and it is budgeted there.** §3.2 lists it
under **code**, but every sampled row is `gharchive/issue` or `gharchive/pull-request` prose, mean
1,707 bytes, and its tokens/byte (0.278–0.283) sits between StackExchange prose and IRC dialogue —
nowhere near code.

**Two agents measured it independently and agree within 2.4%** (11.51 B vs 11.23 B), reaching the same
verdict without conferring. The deciding argument is asymmetric need, not taxonomy: **code clears its
floor 3.12× without it, while QA/forum's SA-free pool cannot clear anything without it** — 1.87 B alone
is 0.26× the floor, and adding github_archive's ~11.5 B of *permissive* tokens takes the SA-free pool to
13.39 B, which passes at 1.86×. So it goes where it is load-bearing. `code.json` counts 0 of it.

## ⚠️ Two traps in `raw_v0.1_parquet` that any re-measurement must handle

Both were caught by agents mid-measurement and both produce a **plausible wrong number**, not an error.

**1. The mirror sweeps TWO document trees for some sources.** A naive footer read overstates
`ubuntu_irc` **2.1×** and `github_archive` **4.5×**. The proof is exact and I reproduced it myself —
reading the whole `id` column for `ubuntu_irc` gives the multiplicity histogram:

```
{1: 404034, 2: 329115}      and 329,115 IS the raw card's document count
```

so a third of the ids appear twice. Row counts confirm the signature cheaply: `ubuntu_irc` has
**3.23× its card's rows** while `peS2o` (1.03×) and `pubmed` (1.06×) are normal. **I verified the
academic figures in this table are unaffected** — only `ubuntu_irc` shows it, and the qa-forum agent
corrected for it before reporting. But `stackv2`, `cccc`, `peS2o` and `uspto` are all large enough to
hide a second tree, so **check the row-count ratio against the card before trusting any footer sum**.

**2. gzip `ISIZE` is mod 2³².** `pubmed`'s shards exceed 4 GiB and wrapped, reporting "uncompressed"
*smaller than compressed*. A ratio guard caught it; per-shard wrap recovery then matched the card ratio
to 0.35%. Any ISIZE-based byte count on shards >4 GiB needs the same guard.

**And the raw→filtered gap is not a size gap, it is a LENGTH-BIAS gap.** For `stackv2_edu` the edu
filter keeps 31.4% of documents but only **5.33% of bytes** — an 18.8× byte reduction, because it
preferentially drops long files. The raw upper bound (~1,403 B) overstates usable supply by ~19× and
must never enter §2.1. This is why every row above uses a filtered-corpus measurement rather than a
scaled raw one.

## Done — and what the method cost

**Every category is measured. `needs_streaming_count` is empty across all eight artifacts**, so the
Batch streaming job this section used to call for is unnecessary. The parquet-footer / gzip-ISIZE route
did it from a laptop in metadata-scale traffic: the qa-forum agent moved **1.6 GB against 365 GB of
text**, and the academic agent 7.1 MB of footers for three corpora.

That is the durable lesson from Phase 0c. Phase 0 stalled because it used datasets-server, whose quota
is **per-IP, not per-account** — so parallel agents starve each other and the failures look exactly like
broken corpora. The footer route touches the hub CDN instead and is quota-free, which is why four agents
could run at once here where eight could not before.

**Cost: effectively zero.** No Batch job, no egress beyond footers, well under the ~$10 budgeted.

## Still open, and each is a decision rather than a measurement

1. **`math` sits 3.6% under its nominal 36 B pool** (34.69 B, floor cleared 4.96×). Adding
   `algebraic-stack` would close it, but its license is *not a grant* — "we do not alter the license of
   any of the underlying data" means the union of 17 languages' GitHub repos.
2. **QA/forum is 92.8% share-alike**, measured from per-document `metadata.license`. The owner has
   decided SA stays in, separable. Worth knowing what separable costs here: dropping SA leaves 1.87 B,
   which fails even peak demand — unless `github_archive`'s ~11.5 B permissive tokens are counted, which
   takes the SA-free pool to 13.39 B (1.86× the floor). That is the strongest argument for filing
   `github_archive` here.
3. **`swallow-code-v2` is measurable after all** (59.38 B) — Phase 0 called it unmeasurable, but its
   blockers were datasets-server-only. It is excluded anyway: a Python-only rewrite of the same Stack-v2
   blobs, with measured surface similarity to its own originals of **0.064**, so no n-gram or MinHash
   dedup catches the duplication. Also **74% of its bytes are `no_license` upstream** despite the repo's
   apache-2.0 tag. If that is ever cleared, the total is 124.32 B (74.81 − 9.88 Python + 59.38), never
   the naive 134.19 B.
4. **The reservoir total moved**, so the dataset name `reservoir-260b-dolma2` needs deciding — see the
   design doc's header. Recommendation there: drop the number.
