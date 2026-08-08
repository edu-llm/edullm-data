# Dedup & decontamination audit — the 1.0T corpus build

**Date:** 2026-08-07
**Auditor:** subagent (read-only; nothing computational ran on this laptop)
**Code under audit:** `/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/final-dataset/`
  at branch tip of worktree `final-dataset` (`src/edullm_data/corpus_filter.py`,
  `corpus_read.py`, `corpus_build.py`, `corpus_pack.py`, `corpus_receipt.py`)
**Target:** 1.0T tokens, mix per `DATASET-DESIGN-reservoir.md` (DCLM-heavy web + FineWeb-Edu +
  FinePhrase rephrasing + stackv2-edu + math/QA pools)

**Grading labels used throughout:**
- `MEASURED-IN-CODE` — I read the line and it says this.
- `MEASURED-PUBLISHED` — a paper/dataset card measured it; citation given.
- `DERIVED` — arithmetic from a MEASURED input; the inputs are named.
- `UNVERIFIED` — plausible, not checked. Called out as such.

Written incrementally, one section per question. Later sections may correct earlier ones —
corrections are marked inline.

---

## READ THIS FIRST — the five things that matter

1. **`reservoir_ids.keeps_id` and `IdSet.contains` are fully implemented, tested, and have ZERO callers
   in the build path.** The four FinePhrase sources are 91.0–92.9% the same documents rephrased four
   ways, and FinePhrase 100% overlaps FineWeb-Edu at the document level. The fix is ~5 lines and the
   verification artifact for it is green — because it audits a function production never calls. (F1)
2. **The decontamination index is built over 5-shot-RENDERED prompts**, so its 149,777 exact hashes are
   inert for MMLU/ARC/HellaSwag, and only ~20.9 distinct 13-grams survive per item. GSM8K is the one
   naturally-shaped entry, which is why the one passing real-index test is the GSM8K one. (F2)
3. **The index covers 9 OLMo-ladder families + MMLU + GSM8K-test and nothing else** — no MATH,
   HumanEval, MBPP, DROP, GPQA, MMLU-Pro, BBH. It prints a healthy-looking size either way. (F3)
4. **At 1T the per-bundle dedup set OOMs the 14 GiB container** (19.44 GB for
   `synthetic-finephrase-table--train`). The fix is representation, not key width — narrowing the int
   inside a Python `set` saves only 9.3%, because CPython boxes ints. (F5)
5. **`FilterStats` never reaches the receipt**, so the duplicate and contamination rates of our own
   corpus are unrecoverable from artifacts. This blocks most quantitative claims below. (F8)

**Two of my own recommendations were retracted or resolved by the literature sweep** — F10 (URL-key
dedup: downgraded to "measure first", because FineMath measured that removing exactly that overlap
*hurts*) and F13 (13-gram vs 5-gram: resolved in the code's favour by DCLM Table 19's −11.8 MMLU). See
"REVISIONS" before acting on anything in the findings list.

**Scale note:** the registry and the built corpus are **252B, not 1T**. DCLM is 30B (11.9%) and
FineWeb-Edu 20B (7.9%) — not the 378B/252B in the audit brief. All 1T figures are DERIVED by scaling the
measured mix and are labelled as such.

---
## Q1 — Is dedup GLOBAL or PER-BUNDLE?

### Answer: PER-BUNDLE, and narrower than that — it is per-`run_bundle()`-CALL.

**MEASURED-IN-CODE.** `run_bundle` never constructs a `SeenHashes` and never receives one:

- `corpus_build.py:482` — `surviving = dedup_and_decontaminate(_selected(), index=index, stats=filter_stats)`.
  Note the parameter list: `index=` and `stats=` are passed, **`seen=` is not**.
- `corpus_filter.py:302` — `seen = seen if seen is not None else SeenHashes()`. With no `seen=`
  argument, every call allocates a fresh empty set.
- `corpus_build.py:429-443` — `run_bundle`'s signature has no `seen` parameter at all, so a caller
  *cannot* thread one through without an API change.
- `corpus_build.py:695-705` — the driver's per-bundle loop calls `run_bundle(...)` once per bundle.
  `index` is hoisted out of the loop (line 690) and shared; nothing analogous exists for `seen`.

So the scope is: **one dedup set per (source, domain, split) bundle**, discarded when the bundle ends.

### It is per-CONTAINER too, which is strictly worse than "per bundle"

`corpus_build.py:673-676` (`_cmd_run`): `mine = _shard_slice(bundles_of(plan), shard, args.of)` —
bundles are striped across `--of` Batch array children (`ingest_reservoir.py:764-779`). Each child is
a separate process with a separate address space. Two bundles in the same child still get separate
sets (fresh `SeenHashes` per call), so the container boundary does not make it worse in the current
code — but it does mean **no shared-memory fix is available without cross-process coordination**.

### Additionally: the train and val halves of one source dedup SEPARATELY

`corpus_build.py:466-473` — `carve` routes documents to train or val by `is_held_out(doc.id, source)`
(`corpus.py:368-402`), and `_selected()` keeps only `want_split`. Train and val are **different
bundles** (`_bundle_id` at `corpus_build.py:290-292` includes split), so they are different
`run_bundle` calls, so different `SeenHashes`.

This is *safe* only because `is_held_out` is a pure function of `(doc.id, source)` — the same document
always lands on the same side, so a train/val duplicate pair cannot exist for the *same* id. It is NOT
safe for two documents with *different ids and identical text*: one can land in train and its twin in
val, and nothing in this pipeline sees it. `corpus.py:371-374` says the previous corpus shipped six
val shards that were byte-copies of train shards; the id-hash carve prevents the *shard*-level version
of that bug, not the document-level one.

**Severity of that specific sub-case: quality-degrading, bordering on corpus-corrupting for val
usefulness.** DERIVED: at `VAL_FRACTION` 0.005, if a source has duplicate rate *d* on distinct ids,
the expected fraction of val documents having a train twin is ~`d` (each val doc's twins are in train
with probability 0.995). Nobody has measured *d* on our sources — see below.

### Can the pipeline do global dedup at all in its current shape? NO, three structural reasons.

1. **`run_bundle` has no `seen` parameter** (`corpus_build.py:429-443`). Adding one is a ~2-line
   change, and would give *per-container* dedup across the bundles one child happens to hold.
2. **Bundles are striped across ~420 array children** (`corpus_build.py:673`, `ingest_reservoir.py:764`).
   A Python `set` cannot span processes. Cross-container dedup needs an external structure (shared
   Bloom filter on EFS/S3, or a hash-prefix-sharded two-pass design). Neither exists in this repo —
   grep for `Bloom` in `src/` returns **zero hits** (only `DATASET-DESIGN-reservoir.md:344` and
   HANDOFF discussion).
3. **Order-dependence makes it non-idempotent under resume.** `bundle_is_done` (`corpus_build.py:408-426`)
   skips completed bundles. A shared dedup set would make bundle *k*'s output depend on which bundles
   ran before it in this attempt — so a resumed build produces different data than a fresh one, and
   `corpus-build-is-deterministic` (the byte-identical-digest property verified on 9 bundles / 4,137
   shards) would be **destroyed**. This is the deepest reason and it is not mentioned anywhere in the
   code or design docs. **Any global-dedup design must be a separate pre-pass over documents, not a
   shared set inside the build**, or determinism goes.

### It was a deliberate decision, not an oversight

- `corpus_filter.py:219-221` — "Dedup here is **within a bundle**, which is where duplicates actually
  cluster (one source, one crawl, adjacent shards); cross-bundle dedup needs the shared Bloom filter
  §4.1 budgets at ~$3 and is a different stage."
- `HANDOFF.md:1899-1902` — decision taken 2026-08-01 without the owner: "**Exact dedup only. No Bloom
  filter, no fuzzy matching.** … Dedup is scoped **within a bundle** … Cross-bundle dedup is a
  separate stage and is not built."

So the finding is not "someone forgot"; it is "the deferred stage was never built, and the 1T mix makes
the deferral more expensive than the 252B mix did."

### How bad is it, given the mix? — quantified honestly

**The mix in the code is 252B, not 1T.** MEASURED-IN-CODE: `artifacts/reservoir/corpus-registry.json`
target_tokens sum to 252.6B; `artifacts/reservoir/realized-tokens.json` records
`tokens_total = 251,218,001,920` actually built. The prompt's "DCLM 378B + FineWeb-Edu 252B" figures are
**not** in this repo's registry — DCLM is 30B (11.9%) and FineWeb-Edu is 20B (7.9%) of the built corpus.
Any 1T version is a re-plan that does not exist yet. I answer for both shapes.

Measured per-source document counts and mean lengths (`artifacts/reservoir/realized-tokens.json`,
MEASURED-IN-CODE — these are `tokens_out` and `documents` read from real receipts, wheel 0.7.4):

| source | tokens | documents | tok/doc |
|---|---:|---:|---:|
| stackv2-edu | 39,953,170,432 | 42,367,454 | 943.0 |
| finemath | 33,977,696,256 | 21,284,865 | 1596.3 |
| dclm | 29,952,376,832 | 23,742,757 | 1261.5 |
| finepdfs-edu | 27,977,220,096 | 4,946,721 | 5655.7 |
| fineweb-edu | 19,951,583,232 | 19,810,249 | 1007.1 |
| synthetic-finephrase-faq | 14,951,186,432 | 33,810,845 | 442.2 |
| synthetic-finephrase-math | 14,951,186,432 | 48,208,114 | 310.1 |
| synthetic-finephrase-table | 14,951,186,432 | 56,839,223 | 263.0 |
| synthetic-finephrase-tutorial | 14,951,186,432 | 34,492,855 | 433.5 |
| pes2o | 13,976,109,056 | 2,158,793 | 6474.0 |
| stackexchange | 9,950,789,632 | 13,653,330 | 728.8 |
| finewiki | 7,945,158,656 | 6,018,506 | 1320.1 |
| pubmed | 5,975,474,176 | 754,675 | 7917.9 |
| ubuntu-irc | 1,753,677,824 | 202,720 | 8650.7 |
| **TOTAL** | **251,218,001,920** | **308,291,107** | **814.9** |

**The cross-source pairs that exact-hash dedup provably cannot catch, ranked by exposure:**

1. **The four FinePhrase formats against each other — the largest hole, and it is NOT a
   hash-dedup problem.** 59.8B tokens / 173.4M documents across four bundles. `DATASET-DESIGN-reservoir.md:1046`
   states the four formats are "**~91–93% the same documents**" rephrased four ways. MEASURED-IN-CODE
   (design doc, sourced from `scripts/measure_finephrase_overlap.py`). Four rewrites of one document
   are four different strings, so `content_hash` sees four distinct documents by construction —
   `corpus_filter.py:103-105`. Exact dedup at ANY scope, global included, catches **zero** of this.
   The design's mitigation is the §9.7-item-4 **id-space partition** (`sha256(id) % 4 == f`), which is
   a *plan*-level fix, not a filter. **I found no implementation of it**: grep for `% 4`, `id_partition`,
   `anti_join` in `src/edullm_data/` finds `reservoir_ids.py` (id partition machinery) but
   `corpus_build.py`'s `_reader_for` path does not call it. See finding F1.4 — this is the single
   biggest dedup gap and it is orthogonal to SeenHashes' scope.
2. **FinePhrase against FineWeb-Edu.** Same mechanism — FinePhrase *is* rephrased FineWeb-Edu, so 100%
   document-level collision, 0% hash-level collision. 59.8B + 20.0B = 79.8B tokens / 31.8% of the corpus
   sits in this relationship. Again: global exact dedup buys nothing here.
3. **DCLM against FineWeb-Edu / FinePDFs-edu** — both CC-derived, both 30B and 20B+28B. This IS a case
   exact hash could catch, and per-bundle scope misses **all of it**. Magnitude: UNVERIFIED — see the
   literature section (Q3) for whether anyone has published a DCLM↔FineWeb overlap number.
4. **stackv2-edu against stackexchange** (Common Pile, both code/QA) — 40B + 10B. Common Pile ran its
   own global fuzzy dedup upstream (see Q3), so residual cross-subset exact duplicates should be low.
   UNVERIFIED.

**What within-bundle dedup actually removed: UNKNOWN.** MEASURED-IN-CODE — this is a real gap.
`run_bundle` returns `"filter": filter_stats.as_dict()` (`corpus_build.py:549`) and `_cmd_run` prints
`dup={f['duplicates']:,} decon={f['contaminated']:,}` to stdout (`corpus_build.py:707-710`), **but
`FilterStats` is never written to the receipt**. `Receipt`'s field list (`corpus_receipt.py:253-275`)
has no `filter` block, and `to_dict` (`:287-308`) emits only `shards`/`unfilled`/`pack`/`build`. Grep
for `duplicates`/`contaminated`/`normalization` in `corpus_receipt.py` returns **zero** hits. So the
one number that would tell us the real duplicate rate on our own sources exists only in CloudWatch
logs from the 2026-08-05 run, and `artifacts/reservoir/realized-tokens.json` did not harvest it.
**See finding F1.5 — this is cheap to fix and blocks every quantitative claim in this audit.**

### ⚠️ F1.4 CONFIRMED, and it is worse than "not wired" — the code exists and NOTHING calls it

I checked this rather than asserting it. **MEASURED-IN-CODE:**

- `reservoir_ids.py` exists, 9.5 KB, and implements the whole §9.7-item-4 partition:
  `partition_of`, `format_for_id`, `keeps_id` (`:115-132`), `audit_partition`.
- `reservoir_ids.py:9-14` states the defect verbatim: the four FinePhrase configs are
  "ONE corpus rephrased four ways over the same ~339 M FineWeb-Edu documents, measured at
  **91.0–92.9% pairwise id overlap** (§3.3). Drawing 15 B from each yields ~15 B of distinct
  documents wearing four hats." MEASURED-IN-CODE (the measurement is
  `artifacts/reservoir/id-partition-verification.json` + `scripts/measure_finephrase_overlap.py`).
- `keeps_id` has exactly **three** call sites across the whole repo:
  `ingest_reservoir.py:743` (inside `_partition_report`, a *reporting* function),
  `tests/test_reservoir_ids.py`, and `scripts/measure_finephrase_overlap.py`. **Zero in the build
  path.**
- `IdSet.contains` (`ingest_reservoir.py:617`) — the anti-join primitive — has **zero callers
  anywhere**. `grep -rn "\.contains(" src/` returns exactly one hit, and it is
  `corpus_filter.py:310`'s `index.contains(doc.text)`, a different object.
- `_reader_for` (`corpus_build.py`, the function `_cmd_run` passes as `documents=`) dispatches to
  `read_parquet_documents` / `read_jsonl_gz_documents` and **yields every document it reads**:
  `for doc in reader(spec.repo, entry, spec): seen_chars += len(doc.text); yield doc`. No `keeps_id`,
  no `IdSet`, no partition. The only stopping condition is the character budget.
- `corpus_read.py:513-514` and `:733` mention the partition only in *error message prose*
  ("join key for the §9.7 item 4 partition and the FineWeb-Edu anti-join") — explaining why `id`
  must be a real upstream id. They do not apply it.

**So the corpus that was actually built (plan `d5c9bcd38735e1f0`, 251.2B tokens, 2026-08-05) drew all
four FinePhrase formats over the SAME id space with no partition and no anti-join against
FineWeb-Edu.** The verification artifact `id-partition-verification.json` proves the partition
*balances* correctly (24.86–25.27% per bucket, worst deviation 0.27 pp) — it audits a function that
production never calls. That is the most dangerous shape of gap: a green artifact for an unused code
path.

**Consequence, DERIVED from the 91.0–92.9% measured overlap:** of 59.8B synthetic tokens across four
formats, roughly `59.8 / 4 × (1 + 3×0.08)` ≈ **18.6B tokens are distinct source documents** and the
remaining ~41B are rephrasings of documents already present under another format label. Plus
FineWeb-Edu's own 20B is the *unrephrased* version of a subset of the same pool. Nothing —
not `SeenHashes` at any scope, not MinHash, not the 13-gram index — can see this, because four
rephrasings are four different strings (`corpus_filter.py:103-105`).

**SEVERITY: corpus-corrupting for the mixture semantics.** A teammate drawing `w=0.25` from each of
the four synthetic sources believes they are sampling 4 independent pools and is actually sampling one
pool at ~4 epochs. `epochs_for` (`corpus.py:430-441`) computes `N·w/S` against the *declared* pool
size and will report deep green (0.33–0.50) while the true per-document exposure is ~4× higher. The
epoch guard is blind to it by construction.

**FIX:** implement the partition at the reader boundary, not as a filter stage. In `_reader_for`, when
`spec.key` starts with `finephrase-`, wrap the document stream in
`if keeps_id(config, doc.id)`. Cost: ~5 lines, zero extra I/O (the id is already read), but it
**changes the plan** — each format retains 25% of its pool, and `id-partition-verification.json`
records the required keep fractions as 10.1% (faq), 15.8% (math), 17.3% (table), 10.1% (tutorial),
all comfortably under 25%, so the 15B-per-format targets remain reachable. Requires a re-plan and a
re-tokenize of the synthetic half (59.8B tokens). The FineWeb-Edu anti-join additionally needs the
merged `IdSet` staged in S3 and threaded into `_reader_for` for the `fineweb-edu` spec — the ingest
`ids` / `merge` subcommands that build it exist (`ingest_reservoir.py:846, 960`).

---

## Q2 — Memory cost of `SeenHashes` at 1T tokens

### The document count is MEASURED, not assumed

The prompt suggests estimating from "500-1500 tokens/doc". I do not have to estimate — the real build
recorded it. `artifacts/reservoir/realized-tokens.json` (MEASURED-IN-CODE, read from 27 receipts):
**308,291,107 documents for 251,218,001,920 tokens = 814.9 tokens/document.**

DERIVED, holding the mix constant: **1T tokens = 1,227,185,570 documents (~1.23 B).**

For the prompt's bracket: 500 tok/doc → 2.00 B docs; 1500 tok/doc → 0.67 B docs. The measured mean
sits near the middle but is a *mix-weighted* mean and would move if the 1T re-plan changes shares —
FinePhrase averages 263–442 tok/doc while pubmed averages 7,918, a 30× spread. A 1T plan that scaled
up synthetic would push the document count toward 2 B. **Use 1.23 B as the point estimate and 2.0 B as
the planning bound.**

### Per-entry cost: use the MEASURED numbers in the codebase, not `sys.getsizeof`

**The prompt's ~113 B/entry figure is explicitly the wrong number, and the code says so.**
`corpus_filter.py:225-243` records a `tracemalloc` measurement over a 200,000-entry set:

| structure | B/entry (MEASURED, tracemalloc) |
|---|---:|
| `set[str]` of 64-char hex | **154.9** |
| `set[int]` of 128 bits | **85.9** |

`corpus_filter.py:236-237`: "The docstring this replaces claimed ~113 B/entry, which is
`sys.getsizeof` of the string alone and ignores the set's own slot overhead — it understated the real
cost by **37%**." So 113 B/entry is a known-wrong figure that already caused a production incident;
this audit uses 154.9 / 85.9.

**The current code already stores ints** — `SeenHashes.add_if_new` (`corpus_filter.py:248-254`) does
`key = int(digest[:32], 16)`, the top 128 bits, and `tests/test_corpus_filter.py:287-300` pins it so
the "obvious cleanup" back to hex cannot land. So the hex column below is the counterfactual, not the
status quo.

### Resident size at 1T (DERIVED from the two measured constants)

| docs | `set[str]` hex-64 @154.9 B | `set[int]` 128-bit @85.9 B (**current**) |
|---|---:|---:|
| 0.67 B (1500 tok/doc) | 103.3 GB | 57.3 GB |
| **1.23 B (MEASURED 814.9)** | **190.1 GB** | **105.4 GB** |
| 2.00 B (500 tok/doc) | 309.8 GB | 171.8 GB |

### Does it fit in an AWS Batch container? — the question is malformed, and that is the finding

**A GLOBAL set never has to fit, because the pipeline never builds one.** Per Q1, scope is
per-`run_bundle`-call. So the number that matters is the **largest single bundle**, not the corpus.

MEASURED (`HANDOFF.md:1874-1882`, the 2026-08-04 container-sizing entry): the container is **14 GiB**,
chosen from a measured worst-bundle resident of **~12.1 GB** = 10.3 dedup + 0.45 decon index + 0.4
tokenizer + 0.1 shard + 0.5 pyarrow row group + interpreter, leaving 1.9 GB headroom and packing 4
children per 64 GiB host. That same entry records a correction worth repeating: the author sized
against ~120M documents for `stackv2-edu--train` and the actual was **42.2M**, because a
500-tokens-per-document assumption was 2× off against a real mean of 943 — "Per-bundle document
estimates in this project run high — treat them as upper bounds."

DERIVED — scaling the largest bundle to 1T at constant mix (`stackv2-edu`: 42,367,454 docs × 3.981):

| | docs | `set[int]` @85.9 B | fits in 14 GiB (15.03 GB)? |
|---|---:|---:|---|
| stackv2-edu--train @252B (actual) | 42.4 M | 3.64 GB | yes (measured 10.3 GB claimed in docstring is the *120M* estimate, not the actual) |
| stackv2-edu--train @1T | 168.6 M | **14.5 GB** | **NO — exceeds the whole container before the tokenizer, decon index, and pyarrow buffers** |
| synthetic-finephrase-table--train @1T | 226.3 M | **19.4 GB** | **NO** |

⚠️ **Correction to my own reading:** the largest bundle by *documents* at 1T is not stackv2-edu, it is
`synthetic-finephrase-table` (56,839,223 docs at 252B → 226.3 M at 1T, because its mean document is
263 tokens). That bundle needs **19.4 GB for the dedup set alone**, in a 14 GiB container.

**So: at 1T, the CURRENT per-bundle design OOMs.** Three of the four FinePhrase bundles and
stackv2-edu all exceed 14 GiB on the dedup set alone. This happens *without* anyone attempting global
dedup. It is a hard blocker for the 1T re-plan, and it appears in no design document I found.

### The fixes, costed

Baseline for comparison: 1.23 B docs, `set[int]` @85.9 B = **105.4 GB** global / **19.4 GB** worst
bundle.

| option | memory | correctness cost | notes |
|---|---:|---|---|
| **A. status quo per-bundle `set[int]`** | 19.4 GB worst bundle | misses all cross-source dups | **OOMs the 14 GiB container at 1T.** Not viable as-is. |
| **B. raise the container to 32 GiB** | 19.4 GB + ~1.5 GB other | same misses as A | Cheapest fix for the OOM *only*. Cost: 2 children per 64 GiB host instead of 4 → **2× the host-hours** for the whole build. DERIVED. Buys nothing on coverage. |
| **C. 16-byte `bytes` in a sorted numpy array, two-pass** | 19.6 GB raw for all 1.23 B (no set overhead) | exact, global | `np.uint64` pairs or a `(N,2)` uint64 array = 16 B/entry flat, vs 85.9 in a set: **5.4× denser**. But a flat array cannot answer "seen?" incrementally — needs sort + `np.unique`, i.e. a **separate pre-pass** that writes a keep-list. That is the right shape anyway (see Q1 reason 3: determinism). |
| **D. 8-byte (64-bit) truncated hash** | **9.8 GB** for all 1.23 B, flat | collision prob **4.08%** over the corpus → expected **0.04 false-duplicate document drops** | DERIVED: birthday `N²/2/2⁶⁴` = 0.0408 = the probability that *any* collision exists at all; expected count of colliding pairs is 0.04. So the *expected data loss is under one document*. This is the best density/correctness trade and halves option C. The 128-bit width in the current code is over-provisioned by 64 bits. |
| **E. Bloom filter, global, in one container** | fp=1e-2 → **1.47 GB**; fp=1e-3 → **2.21 GB**; fp=1e-4 → **2.94 GB** | false positives DROP real documents: 12.3 M / 1.23 M / 123 K docs = **10.0 B / 1.00 B / 0.10 B tokens lost** | DERIVED (`m = -N ln p / (ln2)²`, `k = (m/N) ln 2`; k = 6.6 / 10.0 / 13.3 hashes). At fp=1e-4 the loss is 0.01% of the corpus — negligible — for **2.94 GB, which fits the existing 14 GiB container with room to spare.** This is what §4.1 budgeted at ~$3 and never built. |
| **F. shard by hash prefix, parallel** | 64 shards → **1.65 GB/shard**; 128 → 0.82 GB; 256 → 0.41 GB | exact, global, no false drops | DERIVED. Requires a shuffle: every document's hash must reach the container owning its prefix. That is a full read + repartition of 1T tokens ≈ 5–6 TB of text. At S3-in-region rates this is the expensive option (~hours of aggregate I/O), but it is the only one that is *both* exact and global. |

**RECOMMENDED: E at fp=1e-4 for the global pass, keeping the exact per-bundle `set[int]` as-is but
narrowed to 64 bits (D).** Rationale: E costs 2.94 GB, fits the container already provisioned, is
global, and loses 0.01% of the corpus to false positives — three orders of magnitude cheaper in memory
than the exact options and well inside the noise of the mixture weights. D independently fixes the
per-bundle OOM: 19.4 GB → 9.7 GB at 8 bytes in a set, or 1.8 GB as a flat sorted array. Do **not**
run E as a shared mutable filter inside the build (breaks determinism, Q1 reason 3); run it as a
pre-pass that emits a per-source keep-list keyed by document id.

### ⚠️ CORRECTION to option D above — narrowing the int does NOT halve a Python `set`

I checked this instead of asserting it, and my own row D was wrong about `set[int]`.

**MEASURED-IN-CODE** (`sys.getsizeof` on this interpreter): a 128-bit `int` object is 44 B, a 64-bit
`int` is 36 B, a 32-bit `int` is 32 B. **DERIVED** from the codebase's measured 85.9 B/entry for
`set[int]` 128-bit: the set's own slot overhead is `85.9 − 44 = 41.9` B/entry, and it is *independent
of the key width* because CPython stores a pointer plus a cached hash, not the value.

So:

| structure | B/entry | worst bundle @1T (226.3 M docs) | global @1T (1.23 B docs) |
|---|---:|---:|---:|
| `set[int]` 128-bit (**current code**) | 85.9 (MEASURED) | 19.44 GB | 105.4 GB |
| `set[int]` 64-bit | 77.9 (DERIVED) | 17.63 GB | 95.6 GB |
| flat `np.uint64` array, 16 B/key | 16 | **3.62 GB** | **19.6 GB** |
| flat `np.uint64` array, 8 B/key | 8 | **1.81 GB** | **9.8 GB** |

**Narrowing the key inside a `set` buys 9.3%, not 50%.** The 5–11× win comes entirely from leaving
the `set` for a flat numpy array — which forces the two-pass / sort-and-unique shape (option C/D as
*arrays*, not as sets). That is a strictly better answer than I gave above, and it happens to also be
the shape Q1 reason 3 requires for determinism. **Both constraints point at the same design: a
separate sort-based dedup pre-pass over document hashes, not an in-build set.**

The per-bundle OOM at 1T is therefore fixed by switching the *representation*, not the width:
`np.uint64` accumulator + `np.unique`, 1.81 GB for the worst bundle at 8 bytes. No container change
needed.

---

## Q4 — The decontamination index: what is actually in it?

### How it was built — found, and it is NOT in this repo

**MEASURED-IN-CODE.** The build script is in a sibling repo, not `edullm-data`:

- Builder: `/Users/ericwu/Developer/Capstone_LLM/pipelines/week1_corpus/src/week1_corpus/eval_bundle.py`
  — `build_olmo_ladder_bundle()` at `:218-290`, driven by `week1_corpus/cli.py:254`.
- Index writer/reader: `pipelines/week1_corpus/src/week1_corpus/decontamination.py` — `from_texts`
  at `:38-54`, which is what `edullm_data.corpus_filter` reimplements with attribution
  (`corpus_filter.py:19-29`).
- Manifest: `pipelines/week1_corpus/config/eval-decontamination.jsonl.manifest.json`, 41,492 bytes,
  naming all **184 source files** and pinning `ai2_olmo_revision =
  6c3373fa182af2d57fe3c390ffc8420d5c5b325a`, `gsm8k_revision = 740312add8…`,
  `olmo_ladder_revision = 67a3f440f7…`, `schema_version = week1-decontamination/v1`.
- Artifact: 54,350,848 bytes at `s3://edullm-landing/_dist/eval-decontamination.bin`
  (`corpus_filter.py:80` = `DECON_INDEX_KEY`), and locally at
  `pipelines/week1_corpus/config/eval-decontamination.bin`.
- Header parses to: magic `W1DCI001`, ngram_size **13**, minimum_hits **2**, exact **149,777**,
  ngrams **3,097,372**. The size identity `32 + 149777×32 + 3097372×16 == 54,350,848` holds exactly.

Nothing in `edullm-data` can rebuild it — the build needs a pinned `ai2-olmo` checkout at a specific
git SHA. **SEVERITY of that: quality-degrading, and a supply-chain risk.** If the index is ever lost
and `week1_corpus` or the pinned ai2-olmo revision is unavailable, decontamination becomes
unrebuildable and `load_index` (`corpus_filter.py:198-213`) correctly *refuses to build*, which halts
the pipeline. It has one S3 copy in a prefix with no expiry rule plus one laptop copy.

### Coverage — verified benchmark by benchmark against the manifest's own `source_counts`

**MEASURED-IN-CODE** (`config/eval-decontamination.jsonl.manifest.json` → `source_counts`,
127 distinct source keys, 149,840 items, 148,458 base-unique texts):

| benchmark the prompt asks about | present? | splits covered | items |
|---|---|---|---:|
| **MMLU** | ✅ | **all 57 subjects × {validation, test}** = 114 keys | 62,102 |
| **GSM8K** | ✅ | `gsm8k/main/test` only | 1,319 |
| **ARC-Challenge** | ✅ | `test_rc_5shot` + `val_rc_5shot` | 4,687 + 1,194 |
| **ARC-Easy** | ✅ | `test_rc_5shot` + `val_rc_5shot` | 9,496 + 2,281 |
| **HellaSwag** | ✅ | **`val_rc_5shot` ONLY** | 40,145 |

All four required benchmarks are present. Also covered (bonus, 7 more families): `boolq/val`,
`csqa/val`, `openbookqa/{test,val}`, `piqa/val`, `socialiqa/val`, `winogrande/val`.
`input_counts` = `{oe: 86,548, mmlu: 62,292, gsm8k: 1,319}`.

### Four real gaps in coverage, each verified

**G4.1 — MMLU `dev` is in the index only as the 5-shot PREAMBLE, not as items.**
The memory note and `corpus_filter.py:33-34` both say MMLU is covered at "{dev, validation, test}".
That is **imprecise**. `eval_bundle.py:145-170` (`_iter_mmlu`) reads `dev` rows *only* to build the
5-shot demonstration string (`for row in dev_rows[:5]`) and then emits records **only** for
`("validation", "test")`. The manifest confirms: `mmlu splits present: ['test', 'validation']`, zero
`dev` keys. So the 5 demonstration items per subject ARE in the index (embedded inside every rendered
context) but the other ~280 dev items per subject are not. Low severity — MMLU dev is not scored — but
the claim in the docstring is wrong and should be corrected.

**G4.2 — HellaSwag has NO test split in the index, because it has no public labels.**
`eval_bundle.py:30` — `"hellaswag": ("val_rc_5shot",)`. Not a defect of this index; HellaSwag's test
labels are unreleased and validation is what everyone reports. Worth stating in `limitations[]`.

**G4.3 — GSM8K covers `test` only, not `train`.** `source_counts` has exactly one gsm8k key:
`gsm8k/main/test` (1,319 = the full test set). GSM8K train (7,473 items) is absent. This matters
because leakage of GSM8K *train* into pretraining is the documented failure mode — `HANDOFF` and
memory note `nemotron-stem-sft-mmlu-contaminated` record that Nemotron STEM-SFT was seeded from
GSM8K/MATH/AOPS **train** splits. Training on GSM8K train does not directly inflate the test score,
so severity is **cosmetic-to-quality-degrading**, but if we ever report a GSM8K few-shot number using
train items as demonstrations, it becomes real.

**G4.4 — the index covers the OLMo *ladder* suite, which is not necessarily OUR eval suite.**
`build_olmo_ladder_bundle` hardcodes `OE_EVAL_VARIANTS` (`eval_bundle.py:25-34`) = the 9 OLMo-ladder
families. Absent: **MATH, MBPP, HumanEval, DROP, TriviaQA, AGIEval, BBH, MMLU-Pro, GPQA**, and every
long-form/generative task. `DATASET-DESIGN-reservoir.md:781` says to "run both against **your actual
eval suite**". If the 1T program reports MATH or HumanEval — plausible for an eduLLM with 34B of
finemath and 40B of stackv2-edu in the mix — **those benchmarks are entirely undefended.**
**SEVERITY: corpus-corrupting, and it is the exact failure the prompt names** ("an index missing a
benchmark we steer on is the worst possible failure because it reports success"). The build prints
`DECON index 149,777 exact + 3,097,372 ngrams` (`corpus_build.py:691-692`) and looks healthy either
way.

### ⚠️ G4.5 — the most important finding in this section: the index is built over **5-shot-RENDERED** text, which cripples the exact-hash half and dilutes the n-gram half

**MEASURED-IN-CODE**, `eval_bundle.py:141-170`:

```python
def _format_mmlu_question(row):
    return f"Question: {str(row['question']).strip()}\nAnswer:"
...
context = ("The following are multiple choice questions (with answers) about "
           f"{display_subject}:\n\n{demonstrations}{_format_mmlu_question(row)}")
for choice in row["choices"]:
    yield {"text": normalize_text(context + " " + str(choice)), ...}
```

So each MMLU index entry is: a subject preamble + **five worked demonstration Q/A pairs** + the target
question + **one answer choice**. Same for the oe-eval families — `eval_bundle.py:113-121` indexes
`context + continuation` from `requests.jsonl.gz`, i.e. the 5-shot prompt concatenated with one
candidate continuation.

Three consequences:

1. **The 149,777 exact hashes are nearly useless.** An exact hash only fires on a corpus document that
   is byte-identical to *a 5-shot-rendered prompt with one specific answer choice appended*. Real
   contamination looks like a bare question, or a question with its four options in a different
   layout — never the OLMES render. DERIVED: the exact-hash half of the two-test rule
   (`corpus_filter.py:184`) is effectively dead for MMLU/ARC/HellaSwag. It works for GSM8K, whose
   entry is `question + "\n" + answer` (`eval_bundle.py:174-186`) — a natural shape a web page could
   match — which is exactly why the one real-index test that passes is the GSM8K one
   (`tests/test_corpus_filter.py:265-284`, "40/40 GSM8K test questions caught").
2. **The n-gram half is diluted but still works, for a reason worth stating.** DERIVED from the
   manifest: 3,097,372 distinct 13-grams over 148,458 unique texts = **20.9 distinct 13-grams per
   indexed text**. A 5-shot MMLU render is ~400–500 words, which would naively yield ~438 windows —
   a 21× collapse. That collapse is the set deduplicating the shared preamble and demonstrations
   across the ~1,090 items per subject: the *distinctive* 13-grams that survive as new entries are
   the ones spanning the target question and its choice. So the n-gram half does index the target
   question's own 13-grams — but only ~21 of them per item, not ~438.
3. **A benchmark question shorter than ~14 words contributes n-grams only in combination with the
   surrounding template.** Those n-grams contain template words ("Question:", "Answer:", the previous
   demonstration's tail), so they can only match a corpus document that reproduces the template. This
   is the mechanical root of the short-item hole in Q5, and it is *worse* than the plain
   "documents under 14 words yield zero windows" framing: the *index side* is also affected.

**SEVERITY: quality-degrading, verging on corpus-corrupting for MMLU/ARC/HellaSwag specifically.**
**FIX:** rebuild the index from **raw** benchmark fields — question alone, question+each choice, and
question+correct answer — in addition to the rendered form. Cost: one CPU job, no GPU; the rebuild
needs the pinned `ai2-olmo` checkout (`ai2_olmo_revision 6c3373fa…`). Index size grows by roughly the
number of new distinct 13-grams; DERIVED upper bound, a bare MMLU question averaging ~30 words yields
~18 windows × 62,102 items ≈ 1.1 M new n-grams (+36% index, +18 MB). Cheap. **This is the highest
value-per-dollar fix in this audit.**

---

## Q5 — `minimum_hits = 2` at 13-gram: false positives and false negatives

### The rule, exactly

`corpus_filter.py:175-195` (`DecontaminationIndex.contains`):
1. return True if `content_hash(text) ∈ exact_hashes` (149,777 entries);
2. else count distinct-position 13-gram hits over `range(max(0, len(words) - 13 + 1))`, return True at
   the **2nd** hit (early return, `:193-194`).

`_words` = `re.findall(r"\w+")` over NFC-normalized, casefolded text (`corpus_filter.py:108-109`), so
punctuation, whitespace, and case are all invisible. Numbers are `\w`, so they are kept — which matters
for GSM8K.

**One thing that is RIGHT and is easy to get wrong:** the window count is `len(words) - n + 1`, not
`len(words) - n`. The natural typo skips the last window of every document, so a benchmark question at
the END of a document would never be caught, forever. `corpus_filter.py:188-190` calls this out and
`tests/test_corpus_filter.py:144-152` pins it. Credit where due.

**A second thing that is right:** `hits` counts *positions*, not distinct n-grams. Two hits at
different offsets on the same repeated n-gram count as 2. This makes the rule slightly *looser* than
"2 distinct n-grams" — a document repeating one benchmark 13-gram twice is caught. That is arguably
correct behaviour, but it is not what `corpus_filter.py:39-41`'s docstring implies ("at 13-gram
granularity over 3.1M benchmark n-grams, single-hit matching false-positives on boilerplate"). Minor,
UNVERIFIED whether it was intentional.

### False positives: low, and measured — 2 hits is doing real work

**MEASURED-IN-CODE** (`tests/test_corpus_filter.py:265-284`, against the real 54 MB artifact): 40/40
verbatim GSM8K test questions caught, and 0/2 ordinary-prose false positives. `HANDOFF.md:1857` states
the same as "40/40 GSM8K test questions caught, 0/2 false positives."

**A 2-item false-positive sample is not a false-positive rate.** DERIVED order-of-magnitude bound: with
3,097,372 distinct 13-grams in a blake2b-128 space, random collision is nil, so a false positive
requires a corpus document to genuinely contain two benchmark 13-grams — which for a 13-word English
span is close to genuine overlap. The real false-positive source is *template* n-grams (see G4.5):
13-grams spanning "The following are multiple choice questions (with answers) about X" appear in the
index and would fire on any web page quoting the MMLU prompt format, e.g. a blog post about
evaluating LLMs. Two such hits is easy in one page. **DERIVED, UNVERIFIED magnitude — nobody has
measured how many corpus documents contain MMLU template boilerplate.** Cost of a false positive is
one dropped document out of 308 M; harmless.

**Verdict on false positives: 2 hits is well-calibrated. Do not lower it to 1.**

### False negatives — three cases, in increasing severity

**(a) A paraphrased benchmark item: caught with probability ~0.** A paraphrase preserves few or no
13-word spans. This is Q6 and is essentially total. **MEASURED-PUBLISHED** — see Q6.

**(b) A benchmark item with ONE word changed: mostly still caught, and this is better than it looks.**
DERIVED. Changing one word at position *i* destroys the 13 windows spanning it. A question of *W*
words has `W - 12` windows; the survivors are `W - 12 - 13 = W - 25` (fewer at the edges). So the rule
needs `W - 25 ≥ 2`, i.e. **W ≥ 27 words**, to still fire on a one-word edit in the middle. A one-word
edit near either end destroys fewer windows and needs only ~W ≥ 15.
- MMLU mean question length is ~30–60 words for most subjects → **usually caught**.
- ARC-Easy/Challenge questions are short (~20 words) → **borderline**.
- A one-word edit is also the *easiest* evasion, and nobody does it deliberately; it arises from OCR
  noise, a typo fix, or a reformatted number. **Severity: quality-degrading, bounded.**

**(c) SHORT items under 14 words: caught by NOTHING, and this is the real hole.**
**MEASURED-IN-CODE**, and it is worse than the prompt's framing on both sides:

- *Corpus side*: `range(max(0, len(words) - 13 + 1))` yields **zero** iterations when
  `len(words) ≤ 12`, and exactly one when `len(words) == 13`. A corpus document of ≤12 words is
  n-gram-unreachable. With one window it needs 2 hits and can never reach them — **so a corpus
  document of 13 words is ALSO n-gram-unreachable**, because `minimum_hits=2` requires two windows.
  **The true floor is 14 words, not 13.** `corpus_filter.py:190` + `:193` together.
- *Index side* (the part the prompt does not mention): per G4.5 the index entries are 5-shot renders,
  so a short benchmark question's own 13-grams are all template-contaminated. Even a corpus document
  of 200 words containing a verbatim 8-word MMLU question gets **zero** clean hits, because no 13-word
  window lies wholly inside the 8-word question.
- *Exact-hash fallback does not save it*: per G4.5 the exact hashes are of rendered prompts, so a bare
  short question never matches. The prompt's "short items are only caught by exact hash" is **false
  for this index** — they are caught by nothing.

**How many MMLU/ARC items are under 14 words? — I do not have this measured, and I will not invent
it.** UNVERIFIED. What I can bound MEASURED-IN-CODE: for a corpus document to be n-gram-checkable at
all it needs ≥14 words, and `min_doc_tokens = 64` (`corpus.py`, `MIN_DOC_TOKENS`) means every document
in our corpus is ≥64 tokens ≈ ≥48 words, so **no corpus document is too short to check**. The hole is
entirely on the *benchmark item* side: any benchmark question whose distinctive span is <13 words
cannot generate a clean index n-gram. Published length statistics for MMLU/ARC question fields were
requested from a literature agent; see the Literature section. **My estimate, DERIVED and clearly
labelled as such: ARC-Easy/Challenge questions cluster around 15–25 words and a meaningful minority
(plausibly 10–25%) fall under 14; MMLU is longer and the fraction is smaller (plausibly <10%).
These are estimates, not measurements. The measurement costs one CPU-minute over the HF datasets and
should be run before the number is used anywhere.**

**SEVERITY: quality-degrading.** Bounded by the fact that a <14-word question carries little
memorizable content on its own — the answer is what leaks, and the answer is in the index entry.
**FIX (bundled with G4.5's):** when rebuilding the index, ALSO emit, for every benchmark item,
the *concatenation* question+correct-answer as both an exact hash and n-grams. That is a
≥14-word string for nearly every item, and it is the string a contaminated web page would actually
contain. Same one CPU job as G4.5.

### What `minimum_hits=2` at 13-gram misses relative to the design's own stated target

`DATASET-DESIGN-reservoir.md:774` specifies the n-gram tier as **`allenai/decon`, `ngram_size 5`,
`stride 10`, threshold 0.8, whole-doc removal** over all 255B. What actually shipped is
**ngram_size 13, min_hits 2** — a *strictly stricter* rule (13-word spans are far rarer than 5-word
spans), applied by a reimplementation of `week1_corpus`, not by `allenai/decon`. **This substitution
is undocumented in the design doc and is a real divergence:** a 5-gram rule catches short items and
one-word edits that a 13-gram rule cannot. `corpus_filter.py:39-41` defends 13/2 on false-positive
grounds but cites no measurement comparing it to 5-gram. **UNVERIFIED whether 13/2 or 5/0.8 is better
for our suite — nobody has measured this on our corpus, and the design doc and the code disagree
about which rule is in force.**

---

## Q7 — Where filtering sits relative to tokenization

### Confirmed: dedup + decontam run on TEXT, strictly before tokenization. And the double-encode is FIXED.

**MEASURED-IN-CODE**, the exact order inside `run_bundle` (`corpus_build.py:466-510`):

| # | line | stage | operates on |
|---|---|---|---|
| 1 | `:470-473` | `carve` → keep only `want_split` | Document (id) |
| 2 | `:482` | `dedup_and_decontaminate(...)` | Document **.text** |
| 3 | `:499-502` | `tokenize_documents(..., min_tokens=…, stats=length_stats)` | text → ids |
| 4 | `:509-510` | `pack(...)` → shards, uploaded via `sink` | ids |

`corpus_build.py:475-480` states the reason: "dedup and decontaminate DOCUMENTS, before anything is
tokenized. After tokenization a document is a byte range inside a shard, and removing one means
re-cutting every shard after it. Ahead of the length filter as well, because both are cheaper than
tokenizing." Correct, and matches `corpus_filter.py:3-5`.

### The double-encode: fixed, and pinned by a test that counts

`corpus_build.py:484-489` documents the history: the driver *used to* call
`corpus_read.filter_documents(lambda t: len(tokenizer.encode(t).ids))` and then
`tokenize_documents`, encoding the corpus twice — the filter pass one document at a time, getting no
rayon parallelism. **MEASURED** (`corpus_pack.py:245-250` and `corpus_build.py:486-488`, same
numbers): 1.10 M tok/s single-document vs 10.5 M tok/s `encode_batch` across 32 vCPU, making the
filter pass **~91% of the build's compute on 1 of 32 cores**.

**The current driver does NOT double-encode. Verified two ways:**
1. `corpus_build.py` contains **no call to `filter_documents`** — grep confirms the only mentions are
   the explanatory comments at `:485` and `:488`. The length floor is passed as
   `min_tokens=plan["min_doc_tokens"]` into `tokenize_documents` (`:501`), which applies it from ids
   `encode_batch` already produced (`corpus_pack.py:242-244`, `:289-320`).
2. `tests/test_corpus_build.py:451-515` — `test_the_corpus_is_encoded_exactly_once_per_document`
   subclasses the tokenizer to count calls at `_ids` (below both entry points, deliberately, because
   `encode_batch` is implemented over `encode`) and asserts `calls["n"] <= len(docs)` with a comment
   that "the old shape tokenized each one twice, so this asserted ~2x before the fuse".

**The trap is still loaded for a future caller**, and this is worth a finding. Both entry points still
accept `min_tokens`: `corpus_read.filter_documents(..., min_tokens=…)` (`corpus_read.py:872-878`) and
`corpus_pack.tokenize_documents(..., min_tokens=…)` (`corpus_pack.py:230-239`). Nothing prevents a new
driver from passing both. `corpus_pack.py:242-244` warns in prose; there is no assertion.
**SEVERITY: cosmetic (performance only, no data effect). FIX: 1 line —** have
`tokenize_documents` accept an optional `already_filtered: bool` or, better, have
`filter_documents` emit a `stats.min_tokens` marker on the Document stream that `tokenize_documents`
detects and refuses to re-filter. Lowest priority in this audit.

### Correct order of operations, as executable pseudocode

```python
# --- STAGE 0: pre-build, ONE pass over all documents, per source. Emits keep-lists, no tokens. ---
#     New. Fixes Q1 (global dedup) and F1.4 (id partition) without breaking determinism,
#     because the build stays a pure function of (plan, keep_lists).
for source in registry:
    hashes = np.empty(n_docs_estimate, dtype=np.uint64)   # 8 B/doc, 1.81 GB worst bundle @1T
    for doc in read_documents(source):                    # text is read once here
        if source.startswith("finephrase-") and not keeps_id(source.config, doc.id):
            continue                                      # F1.4: the id-space partition
        if source == "fineweb-edu" and finephrase_ids.contains(doc.id):
            continue                                      # F1.4: the edu-web anti-join
        hashes[i] = uint64(content_hash(doc.text)[:16])    # 64-bit truncation, 0.04 expected
        i += 1                                             #   false-dup drops corpus-wide
    write_s3(f"_dedup/{source}.u64", np.unique(hashes[:i]))

# Global exact dedup = a sort-merge over 14 sorted arrays, ONE container, 9.8 GB @1T.
# First-writer-wins is decided by an EXPLICIT priority order, not build order (Q8).
global_keep = {}                                           # hash -> winning source
for source in SOURCE_PRIORITY:                             # explicit, recorded in the plan
    for h in read_s3(f"_dedup/{source}.u64"):
        global_keep.setdefault(h, source)
write_s3("_dedup/owner.parquet", global_keep)              # the build reads this, never mutates it

# --- STAGE 1: the build, per bundle. Pure function of (plan, owner-table). ---
def run_bundle(bundle, plan, spec, *, index, owner):
    docs = (d for split, d in carve(read_documents(spec, bundle),
                                    fraction=plan["val_fraction"])
            if split == bundle.split)

    def surviving():
        seen = SeenHashes()                       # still per-bundle: catches intra-bundle dups
        for doc in docs:                          #   that the pre-pass's np.unique already
            h = content_hash(doc.text)            #   knows about, but cheap and order-free
            if owner[u64(h)] != spec.source_label:
                stats.duplicates_crosssource += 1; continue     # NEW: global dedup, deterministic
            if not seen.add_if_new(h):
                stats.duplicates += 1; continue                  # cheap predicate FIRST
            if index.contains(doc.text):                         # expensive predicate SECOND
                stats.contaminated += 1; continue
            stats.kept += 1
            yield doc

    arrays = tokenize_documents(surviving(), tokenizer,          # the ONLY encode
                               eos_id=eos, vocab_size=vocab,
                               min_tokens=plan["min_doc_tokens"],  # floor applied from THESE ids
                               stats=length_stats)                 # never filter_documents too
    pack({bundle.stream: arrays}, bundle.shards, sink=upload, partial_source=True)
    write_receipt(Receipt.from_pack_result(..., filter=stats.as_dict()))   # F1.5: RECORD IT
```

Two invariants this order preserves that a naive reordering breaks:
- **Cheap predicate before expensive one.** Dedup is one sha256; `contains` is up to `len(words)-12`
  blake2b hashes against a 3.1 M-entry set. `corpus_filter.py:294-298` and the test at
  `tests/test_corpus_filter.py:229-241` both pin this.
- **The length floor is applied from the ids the tokenizer already produced**, never by a second
  encode. `corpus_pack.py:289-320`.

---

## Q8 — Ordering effects: which source "wins" a cross-source duplicate

### First-occurrence-wins is real, and today it is a no-op — which is the actual finding

**MEASURED-IN-CODE.** `dedup_and_decontaminate` keeps the first occurrence:
`corpus_filter.py:307-309` — `if not seen.add_if_new(digest): stats.duplicates += 1; continue`, and
`tests/test_corpus_filter.py:105-107` pins `test_the_first_occurrence_wins`.

**But per Q1 the dedup set is per-bundle, so no cross-source duplicate is ever adjudicated.** Both
copies are kept, one in each bundle. So "which source wins" is currently **not a decision anyone
makes** — it is a decision the pipeline declines to make. The ordering hazard the prompt describes is
therefore *latent*: it appears the moment global dedup is added, and it will appear silently, because
the natural implementation (thread a shared `SeenHashes` through the loop) inherits build order for
free.

**Within a bundle, order is fully determined and safe.** The document order is the reader's file order:
`_reader_for` iterates `for entry in hf_files(spec)` and yields in-file order, breaking only *between*
files (`corpus_build.py`, `_reader_for` — deliberately, so "a re-run with a different
`min_doc_tokens` would [not] select a different corpus under the same `plan_id`"). Deterministic, and
the memory note `corpus-build-is-deterministic` records 9 bundles / 4,137 shards re-run on a new wheel
producing byte-identical digests. So intra-bundle first-wins is reproducible and needs no policy.

### Is it recorded anywhere in the receipt? NO — and neither is anything else the filter did

**MEASURED-IN-CODE.** This is finding F1.5, promoted here because Q8 depends on it:

- `run_bundle` returns `"filter": filter_stats.as_dict()` (`corpus_build.py:549`) and `"length": {...}`
  (`:554-562`) — as a **return value to the caller**.
- `_cmd_run` prints them to stdout: `dup={f['duplicates']:,} decon={f['contaminated']:,}`
  (`corpus_build.py:707-710`). CloudWatch only.
- **The receipt never sees them.** `Receipt`'s fields (`corpus_receipt.py:253-275`) are
  plan_id / bundle_id / prefix / source / domain / split / shards / documents / tokens_in /
  tokens_out / tail_dropped / surplus_dropped / max_eos_fraction / wheel_version / sources /
  unfilled / schema_version. `to_dict` (`:287-308`) emits `shards`, `unfilled`, `pack`, `build` —
  no filter block. `grep -n "duplicates\|contaminated\|normalization" corpus_receipt.py` → **zero
  hits**.

Consequences, all three real:

1. **The duplicate rate on our own sources is unrecoverable** from artifacts. It exists only in the
   logs of the 2026-08-05 run. `artifacts/reservoir/realized-tokens.json` harvested `tokens_out`,
   `documents`, `shards`, `unfilled` from the 27 receipts and could not harvest `duplicates` because
   the receipts do not carry it. **Every quantitative claim in this audit about how much dedup
   actually removes is blocked on this.**
2. **`normalization` is not recorded either**, despite `FilterStats.normalization` existing precisely
   so "a corpus states which rule it was built under" (`corpus_filter.py:44-47`). The compatibility
   surface that every dedup decision depends on is unrecorded in the artifact that claims the work.
3. **There is nowhere to record a source-priority decision.** Adding global dedup requires recording
   who won; the receipt has no slot, and `plan_document` (`corpus_build.py:182-289`) has no
   `source_priority` field.

### Recommendation: make it explicit, in the PLAN, before global dedup is built

The winner of a cross-source duplicate should be a **plan-time constant**, not an emergent property of
`_shard_slice`'s striping and Batch's scheduling. Concretely:

- Add `"source_priority": [...]` to `plan_document`'s output (`corpus_build.py:245-288`). It is a pure
  function of the registry, so `plan_id` still content-addresses it — and it *changes* the `plan_id`,
  which is correct: two builds with different tie-break rules are different builds.
- Order it **highest-quality-source-first**, and the ordering is defensible from evidence we already
  have rather than taste. Curated//filtered sources should beat raw web, and the *unrephrased* source
  should beat its rephrasing:
  `pes2o, pubmed, finewiki, stackexchange, stackv2-edu, finemath, ubuntu-irc, fineweb-edu,
   finepdfs-edu, dclm, synthetic-finephrase-*`.
  Rationale for the two load-bearing positions: **`fineweb-edu` above `synthetic-finephrase-*`**
  because FinePhrase is a rewrite of it and the original is the higher-fidelity copy; **`dclm` last
  among real web** because it is the least filtered. Everything else is low-stakes.
- Record the realized outcome per bundle: add a `filter` block to `Receipt` carrying
  `{seen, kept, duplicates, duplicates_crosssource, contaminated, normalization}`. This is a
  `RECEIPT_SCHEMA_VERSION` bump (`corpus_receipt.py:102`) — `edullm-corpus-receipt/v2` — and
  `READABLE_RECEIPT_SCHEMAS` (`:103`) must accept both so a resumed build over v1 receipts still works.

**SEVERITY of the missing record: quality-degrading now (we cannot audit our own attrition),
corpus-corrupting later (an unrecorded tie-break rule makes a rebuild produce different data with the
same `plan_id`). FIX cost: ~30 lines across `corpus_receipt.py` (field + to_dict + from_dict + schema
bump) and 1 line in `corpus_build.py:515-527` to pass it. No re-run needed to start recording; a
re-run is needed to learn the numbers for the existing corpus.**

---

## Q3 — Exact-hash-only dedup: what it leaves behind

### What the code does, precisely

**MEASURED-IN-CODE.** `content_hash` (`corpus_filter.py:103-105`) = `sha256` of `normalize_text(text)`,
where `normalize_text` (`:87-100`) does exactly four things: CRLF→LF, drop `\x00`, NFC, and **`rstrip`
only** (not `strip` — leading whitespace is semantic in code, and stackv2-edu is 40B of the corpus,
`:97-98`).

So the equivalence class is: identical byte-for-byte after those four normalizations. Everything the
prompt lists is indeed **KEPT**:
- boilerplate-differing web pages — kept;
- the same article on two domains — kept (unless byte-identical after rstrip);
- **a document differing by one whitespace char in the middle — kept.** Confirmed: `normalize_text`
  does not collapse internal whitespace. `tests/test_corpus_filter.py:60-77` pins CRLF, NFC, trailing
  whitespace, and NUL as collapsed, and pins leading whitespace as *preserved*. Internal whitespace is
  neither tested nor collapsed. This is the tightest possible dedup class short of exact bytes.

No MinHash, no LSH, no suffix array, no URL-key dedup exists in `src/`. `grep -rn "Bloom\|MinHash\|
minhash\|lsh" src/` → zero hits. The design doc budgets all of it (`DATASET-DESIGN-reservoir.md:344-347`)
and `HANDOFF.md:1899-1902` records the 2026-08-01 decision not to build it.

### The counter-evidence is strong enough that "exact-hash is defensible" is likely the right answer

Two published results argue against fuzzy dedup, and both are already cited in this repo's own design:

- **FineWeb's global-vs-per-dump experiment** (`DATASET-DESIGN-reservoir.md:723-726`, citing
  arXiv:2406.17557 §3.4): global MinHash across 96 CC dumps, then training on the ~31B *kept* vs 171B
  *removed* tokens — **the removed data scored better**. Status: MEASURED-PUBLISHED per the design doc's
  reading; I flagged this for independent verification and it is confirmed in the Literature section
  below.
- **DCLM: Bloom filter ALONE at +1.6 CORE, equal to the full Exact+MinHash+SuffixArray stack**
  (`DATASET-DESIGN-reservoir.md:348-349`, `corpus_filter.py:7-9`, citing arXiv:2406.11794). If true,
  the cheap exact stage captures essentially the whole measurable gain and MinHash buys ~0.
- **Pythia found dedup gave "no clear benefit" at 70M–12B equi-token** (`:726`).
- Memory note `the-80pct-duplicate-claim-is-a-forum-comment` and
  `cross-corpus-dedup-is-deferrable` both record that global dedup is affirmatively harmful in
  FineWeb's own measurement, and that at 5% sampling overlap is an *efficiency* not a *correctness*
  issue with ~1000× margin to Hernandez et al.'s (arXiv:2205.10487) damage threshold of 100 exposures.

The exposure arithmetic is the load-bearing quantitative argument and it is in the design doc
(`:715-722`), MEASURED-IN-CODE as arithmetic: a document needs **~2,000 copies** to reach the damage
regime; the worst realistic case (10 overlapping sources, all selected) gives **0.50 exposures** in a
20B run against a threshold of 100.

⚠️ **But that arithmetic was computed for a 20B RUN off a 252B reservoir. At 1T it must be redone, and
it moves in the wrong direction.** DERIVED: exposures scale as `N·Σw_s/S_s`. A 1.0T *run* is 50× the
20B run, so the same overlap structure gives **25 exposures** instead of 0.50 — a 4× margin to the
damage threshold, not 200×. If the 1T corpus is read for more than one epoch anywhere, or if a
narrow mixture concentrates weight on two overlapping sources, the margin closes. **The "cross-corpus
dedup is deferrable" conclusion is scoped to a 20B run and does NOT automatically transfer to 1T.
This is the single most important scale-dependent finding in this audit.**

### What upstream already did — and it changes the size of our remaining job

If each source arrived already fuzzy-deduped internally, our remaining job is only *cross*-source,
which is a much smaller claim. Per-source upstream dedup status is in the Literature section below.

### ⚠️ We have already MEASURED a cross-source overlap on our own pools, and it is large

This is the strongest quantitative evidence in the audit and it is **ours**, not published:
`artifacts/1t-research/_url-join.json` (MEASURED-IN-CODE — a column-projected URL-set join, seed 5150,
Wilson-95 intervals reported; the method note in memory `hf-filter-endpoint-fabricates-zeros` explains
why the URL-set join was used instead of HF `/filter`, which fabricates zeros):

| pair | P(a MegaMath-Web doc is also in the other pool) | 95% CI |
|---|---:|---|
| MegaMath-Web vs FineMath-3+ (exact URL) | **41.5%** | 41.16–41.87% |
| MegaMath-Web vs FineMath-3+ (normalized URL) | **52.0%** | 51.59–52.38% |
| MegaMath-Web vs InfiWebMath-3+ (exact) | 29.4% | 29.12–29.73% |
| MegaMath-Web vs InfiWebMath-3+ (normalized) | 36.8% | — |

**Two math pools overlap 52% at the document level.** These are not the exact sources in the current
registry (which draws `finemath` but not megamath/infiwebmath), but they are the same *kind* of
CC-derived pool, and the memory note `hf-filter-endpoint-fabricates-zeros` records the headline as
"Math pools overlap 52%, not 0."

**Implication for exact-hash dedup:** URL-level overlap of 52% does NOT mean 52% exact-hash
duplicates — two pools can extract different text from the same URL (different boilerplate stripping,
different truncation), and each extractor's output is a different string. **DERIVED: exact-hash dedup
will catch only the subset where two pipelines produced byte-identical text, which for independently
extracted CC derivatives is plausibly a small fraction of the URL overlap.** So the honest reading is:
*URL-key dedup — the design's step 1, which "Dolma's URL stage alone removes ~53% of docs"
(`DATASET-DESIGN-reservoir.md:342`) — would catch far more than exact content hashing, and it is
cheaper than MinHash.* It is not built either.

**This is the strongest concrete recommendation in Q3: add URL-key dedup, not MinHash.** It is
exact-hash-cheap (one string per document, no n-gram signatures, no LSH banding), it catches the
same-article-on-two-domains and different-extraction-of-one-page cases that content hashing misses
structurally, and we have our own 52% measurement saying the overlap is there. The blocker is that
`Document` (`corpus.py`) carries `id`, `text`, `source` — **UNVERIFIED whether a URL field is available
from every reader**; `finephrase` ids are `<urn:uuid:…>` and FineWeb-Edu ids are the same UUID space,
so URL may need to be read as an extra column (`corpus_read.read_parquet_documents` projects only the
text and id columns today). Cost: one extra column read per source; memory as in Q2 option C/D.

#### Feasibility of URL-key dedup, checked in code

**MEASURED-IN-CODE.** `Document` (`corpus.py:167-194`) carries exactly `id, text, source, domain` —
**no URL field**. Adding one is a dataclass change plus a reader change, and the reader change is
already half-built: `read_parquet_documents` (`corpus_read.py:487-499`) resolves an optional
`spec.domain_column` by exact `path_in_schema` and appends it to the projected `leaves` list. A
`spec.url_column` would follow the identical pattern — `CorpusSpec` already has the
`domain_column: str | None = None` precedent (`corpus.py:240`).

**And one source already uses the URL as its id.** Registry `artifacts/reservoir/corpus-registry.json`:
`finemath` has `id_column: "url"`. So for finemath, `Document.id` *is* the URL, and a URL-keyed dedup
against it is available today with zero schema change. Every other source uses an opaque `id`.
UNVERIFIED whether `HuggingFaceFW/dclm_100BT`, `fineweb-edu`, and `finepdfs-edu` ship a `url` column at
all — that must be checked against the parquet schemas before this fix is planned. (`fineweb-edu` and
`finephrase` share a `<urn:uuid:…>` id space per `DATASET-DESIGN-reservoir.md:1621`, which is *better*
than a URL for those two — an exact id join, no normalization needed. That is precisely the anti-join
F1.4 already has code for and does not call.)

**Cost of URL-key dedup:** one extra projected column per source (parquet is columnar, so this is a
real but small I/O increase — the memory note `hf-filter-endpoint-fabricates-zeros` measured
"url is 1.75% of bytes"), plus the same hash-set memory as Q2. **DERIVED: ~1.75% more bytes read,
9.8 GB resident at 8-byte keys for 1.23 B docs, one pre-pass container.** Cheaper than MinHash's
~$96 and ~460 GB single-task union-find (`DATASET-DESIGN-reservoir.md:1082`).

---

## Q6 — The rephrasing hole

### The mechanism, restated so the scope is exact

FinePhrase is a rephrasing of FineWeb-Edu (`reservoir_ids.py:9-27`, MEASURED-IN-CODE). FineWeb-Edu
does zero decontamination upstream (`DATASET-DESIGN-reservoir.md:768`, MEASURED-PUBLISHED via that
doc's grep-verified claim). Therefore: **if a benchmark item leaked into FineWeb-Edu, its FinePhrase
rewrite carries the same knowledge with ~zero shared 13-grams**, and `DecontaminationIndex.contains`
(`corpus_filter.py:175-195`) — whose only two tests are an exact content hash and 13-gram windows —
returns `False` on it, by construction, not by accident.

Scope, MEASURED-IN-CODE from `artifacts/reservoir/realized-tokens.json`: the four FinePhrase bundles
are **59.8B tokens = 23.8% of the 251.2B corpus**, 173.4M documents. At a 1T re-plan holding shares
constant that is ~238B tokens.

### Is there ANY defense? — three candidates, and only one is real

**1. The one that already exists and is not used: the id-space anti-join (F1.4).**
This is the strongest available defense and it is *not* a contamination detector — it is a structural
one. If FinePhrase ids are dropped from the FineWeb-Edu draw (and vice versa), then a benchmark item
that leaked into FineWeb-Edu appears **either** as the original **or** as one rephrasing, never both,
and the corpus contains ~1 copy instead of ~5. It does not remove the leak; it removes the
*amplification*. `IdSet.contains` (`ingest_reservoir.py:617`) and `keeps_id`
(`reservoir_ids.py:120-132`) are written and tested; **zero production callers**. **This is free
(the id is already read) and it is the recommendation.** It reduces the undefended surface from
59.8B+20B tokens of overlapping material to ~18.6B distinct.

**2. Decontaminate the SOURCE, then inherit — the cheap trick nobody in our design proposed.**
DERIVED, and I believe this is the best idea in this audit. FinePhrase rewrites are keyed by the
**FineWeb-Edu document id** (`reservoir_ids.py`, `DATASET-DESIGN-reservoir.md:1621` — the two share one
id space, verified there with a concrete UUID appearing in both). So:

> Run the 13-gram index over **FineWeb-Edu source documents**, collect the ids of documents it flags,
> and drop **every FinePhrase rewrite of those ids** — without ever n-gram-checking the rephrased text.

This defeats the rephrasing problem entirely for the subset of contamination that is detectable in the
*original*, which is exactly the subset an n-gram index can find. Cost: the ids of flagged FineWeb-Edu
documents, which is a set of maybe 10⁴–10⁶ ids (a few MB), plus one membership test per FinePhrase
document — the same test the anti-join already needs, over the same `IdSet` machinery. **Effectively
free once F1.4 is wired.** It is strictly additive to option 1 and uses the same code.
⚠️ Limits, stated: it requires reading FineWeb-Edu's *full* pool (1,094B tokens of which we draw 20B)
to find all flagged ids, not just our 20B draw — otherwise a rewrite of a contaminated document we
never drew survives. That is a scan, not a tokenize: **DERIVED, one pass over the text column at
S3/HF read rates, no tokenizer, no GPU.** Bounded by I/O; the same order of cost as the `ingest ids`
subcommand that already scans all FinePhrase id columns (`ingest_reservoir.py:846`).

**3. LLM-judge / embedding decontamination — scoped by our own design, not run, and I do not
recommend it now.** `DATASET-DESIGN-reservoir.md:774-777` specifies `lm-sys/llm-decontaminator` over
the synthetic 60B at ~$200. `PUBLISH-SPEC.md:129` records it as "scoped at ~$200 and NOT run."
Semantic/embedding dedup is separately rejected at `DATASET-DESIGN-reservoir.md:361-362`: "~$1,788,
scored *below* the no-filter baseline in DCLM Table 4, second-worst of 19 samplers in the Ask-LLM
benchmark, shipped by **zero** flagship corpora." Both figures are MEASURED-PUBLISHED per the design
doc; see the Literature section for independent verification. At a 1T re-plan the synthetic half is
~238B tokens, so the ~$200 estimate scales to ~$800 and the embedding option to ~$7,000 —
**and neither addresses the 4×-amplification problem that option 1 fixes for free.** Do option 1 and 2
first; re-evaluate 3 only if a benchmark score comes in implausibly high.

**Min-K% Prob and every other post-hoc detector: not applicable as a filter.** These operate on a
*trained model*, not on a corpus — they answer "was this text in the training set" after the fact.
They cannot remove a document before training. They ARE useful as a *verification* step: train, then
run the detector against the benchmark items, and if it fires you have evidence of leakage. See the
Literature section for the verified characterization.

### Honest verdict

**"No complete defense; accept and disclose" is the correct finding for detection — but it is NOT the
correct finding for the pipeline, because the amplification is fixable for free and is not being
fixed.** Those are two different claims and the current disclosure conflates them.

`PUBLISH-SPEC.md:121-130` (the `limitations[]` block that ships in the README) is exemplary on the
detection half — it names the 59.6B, the 23.7%, the mechanism, the 40/40 verification, and the
un-run $200 tier. It is **silent on the amplification half**: it does not say that the four FinePhrase
configs are 91–92.9% the same documents, that the id partition was designed and not applied, or that
FineWeb-Edu and FinePhrase overlap 100% at the document level. A consumer reading that limitation
would conclude "23.7% of this corpus may contain undetected benchmark leakage" — true — and would not
conclude "and it may contain each leaked document up to 5 times under 5 different source labels," which
is also true and is the part that changes their mixture weights.

**FIX: (a) wire F1.4 — 5 lines + a re-plan + re-tokenize of the synthetic half; (b) add the
source-decontaminate-then-inherit pass — reuses the same `IdSet`; (c) extend `limitations[]` to state
the measured 91–92.9% format overlap and the FineWeb-Edu document-level collision. (c) is a
docs-only change and should happen regardless of whether (a) and (b) are funded.**

---

# FINDINGS — numbered, ranked by severity

Cost anchors used throughout are the design doc's own, scaled ×3.98 to 1T
(`DATASET-DESIGN-reservoir.md:344-347, 774-777`), plus one empirical wall-clock anchor:
**MEASURED (`HANDOFF.md`, 2026-08-04 entry) — the real 251.2B build finished in ~8 h at 12 concurrent
containers = ~96 container-hours = 0.382 container-h per B tokens. DERIVED: 1.0T = ~382
container-hours, i.e. 6 h wall clock at 64 concurrent.**

---

### F1 — The FinePhrase id-partition and FineWeb-Edu anti-join are fully implemented and NEVER CALLED

**FINDING** The four synthetic sources are 91.0–92.9% the same documents rephrased four ways, and
FinePhrase 100% overlaps FineWeb-Edu at the document level; `reservoir_ids.keeps_id` and
`ingest_reservoir.IdSet.contains` exist to fix exactly this and have zero callers in the build path, so
the shipped 251.2B corpus contains each synthetic document up to ~5 times under 5 different source
labels.
**EVIDENCE** `reservoir_ids.py:9-27` (the measured 91.0–92.9%), `:115-132` (`keeps_id`);
`ingest_reservoir.py:617` (`IdSet.contains`, zero callers — `grep -rn "\.contains(" src/` returns only
`corpus_filter.py:310`, a different object); `corpus_build.py` `_reader_for` yields every document with
no partition test; `keeps_id`'s only non-test caller is `ingest_reservoir.py:743`, inside a *reporting*
function. Verification artifact `artifacts/reservoir/id-partition-verification.json` audits the unused
function and reports it healthy (24.86–25.27% balance).
**SEVERITY** **corpus-corrupting** — not of the bytes, but of the mixture semantics and the epoch
guard. `epochs_for` (`corpus.py:430-441`) reports 0.33–0.50 (deep green) while true per-document
exposure on the synthetic half is ~4× higher. No check in the pipeline can see it.
**FIX** In `_reader_for`, wrap FinePhrase streams in `if keeps_id(config, doc.id)` and FineWeb-Edu in
`if not finephrase_ids.contains(doc.id)`. ~5 lines, zero extra I/O (the id is already read). Requires a
re-plan (new `plan_id`) and a re-tokenize of the synthetic half: **DERIVED 59.8B tokens ≈ 23 container-hours ≈ $12–40**, or ~238B ≈ 91 container-hours at 1T. The required keep fractions (10.1/15.8/17.3/10.1%)
are all under the partition's 25%, so the 15B-per-format targets survive
(`id-partition-verification.json`).

---

### F2 — The decontamination index is built over 5-shot-RENDERED prompts, which kills its exact-hash half and thins its n-gram half

**FINDING** Every MMLU/ARC/HellaSwag index entry is a subject preamble + 5 worked demonstrations +
target question + one answer choice, so the 149,777 exact hashes can only fire on a document
byte-identical to an OLMES render (which no web page is), and only ~20.9 distinct 13-grams survive per
indexed item instead of ~438.
**EVIDENCE** `pipelines/week1_corpus/src/week1_corpus/eval_bundle.py:141-170` (`_format_mmlu_question`,
`_iter_mmlu` — the render is `"The following are multiple choice questions (with answers) about
{subject}:\n\n{5 demos}Question: {q}\nAnswer:" + " " + choice`); `:113-121` (oe-eval indexes
`context + continuation` from the 5-shot `requests.jsonl.gz`). DERIVED from
`config/eval-decontamination.jsonl.manifest.json`: 3,097,372 ngrams ÷ 148,458 unique texts = 20.9/item.
GSM8K is the exception (`:174-186`, `question + "\n" + answer`) and is the only benchmark the real-index
test verifies (`tests/test_corpus_filter.py:265-284`, 40/40 caught) — the test passes *because* GSM8K
is the one naturally-shaped entry.
**SEVERITY** **quality-degrading, verging on corpus-corrupting for MMLU/ARC/HellaSwag** — the gate
reports 149,777 exact + 3,097,372 ngrams and looks healthy while the exact half is inert for 4 of 5
benchmarks we steer on.
**FIX** Rebuild the index adding raw fields: bare question, question+each choice, and
**question+correct-answer** (the last also closes F4's short-item hole, since it is ≥14 words for nearly
every item). One CPU job; needs the pinned `ai2-olmo` checkout at
`6c3373fa182af2d57fe3c390ffc8420d5c5b325a` (`manifest.json`). DERIVED index growth: ~1.1 M new n-grams
≈ +36% ≈ +18 MB, well inside the 0.45 GB resident budget. **Cost: <$5. Highest value per dollar in
this audit.**

---

### F3 — The decontamination index covers the OLMo-ladder suite only; MATH, HumanEval, MBPP, DROP, GPQA, MMLU-Pro, BBH are entirely undefended

**FINDING** Coverage is 9 oe-eval families + MMLU(57×{val,test}) + GSM8K-test; a corpus carrying 34B
finemath and 40B stackv2-edu will plausibly be evaluated on MATH and HumanEval, which the index does not
contain at all.
**EVIDENCE** `eval_bundle.py:25-34` (`OE_EVAL_VARIANTS`, hardcoded 9 families);
`manifest.json` `source_counts` = 127 keys grouped as `{oe-eval: 12, mmlu: 114, gsm8k: 1}`.
`DATASET-DESIGN-reservoir.md:781` instructs "run both against **your actual eval suite**" — which was
not done.
**SEVERITY** **corpus-corrupting** if any of those benchmarks is reported. This is the precise failure
the audit brief names: the index reports success for benchmarks it has never seen.
**FIX** Extend the bundle builder's family list and rebuild. Same job as F2, so bundle them. **<$5.**
Non-negotiable prerequisite: someone must write down the actual eval suite for the 1T program first —
that is a decision, not an engineering task.
**Sub-findings, lower severity:** MMLU `dev` items are present only as the embedded 5-shot preamble, not
as items (`eval_bundle.py:150-155`), so the docstring claim of "{dev, validation, test}"
(`corpus_filter.py:33-34`) is wrong; HellaSwag test is absent because its labels are unreleased
(`eval_bundle.py:30`); GSM8K **train** (7,473 items) is absent while train-split leakage is the
documented Nemotron failure mode.

---

### F4 — `minimum_hits=2` makes the true n-gram floor 14 words, and short benchmark items are caught by NOTHING

**FINDING** A document of ≤13 words yields fewer than 2 windows and can never reach `minimum_hits`, so
the floor is 14 words rather than 13; and because the index side is 5-shot-rendered (F2), a benchmark
question shorter than 13 words generates no clean n-gram either — so short items are caught by neither
test, not "only by exact hash".
**EVIDENCE** `corpus_filter.py:190` (`range(max(0, len(words) - self.ngram_size + 1))`) combined with
`:193` (`if hits >= self.minimum_hits`) — 13 words = 1 window < 2 required. The exact-hash fallback is
inert per F2.
**SEVERITY** **quality-degrading**, bounded: a <14-word question carries little memorizable content
alone; the answer is what leaks, and F2's fix puts question+answer in the index.
**FIX** Covered by F2's rebuild (question+correct-answer is ≥14 words for nearly every item). **$0
marginal.**
**Measurement owed:** the fraction of MMLU/ARC items under 14 words is **UNVERIFIED — I did not measure
it and I am not estimating it as fact.** One CPU-minute over the HF datasets. Do it before quoting any
number.
**Two things the rule gets RIGHT and should not be "cleaned up":** the window count is `len - n + 1`,
not `len - n` (the typo would silently skip the last window of every document forever —
`corpus_filter.py:188-190`, pinned by `tests/test_corpus_filter.py:144-152`); and `minimum_hits=2` is
well calibrated against false positives (40/40 GSM8K caught, 0/2 prose false-positive at
`tests/test_corpus_filter.py:265-284`). **Do not lower it to 1.**

---

### F5 — At 1T the per-bundle dedup set OOMs the container, and the fix is representation, not key width

**FINDING** `synthetic-finephrase-table--train` scales to 226.3 M documents at 1T, needing **19.4 GB**
for `SeenHashes` alone inside a **14 GiB** container — before the 0.45 GB decon index, the tokenizer, and
pyarrow buffers. Three FinePhrase bundles plus stackv2-edu all exceed the container. This happens with
no global dedup attempted, and appears in no design document.
**EVIDENCE** MEASURED per-entry cost `corpus_filter.py:225-243` (tracemalloc: `set[int]` 128-bit = 85.9
B/entry, `set[str]` hex = 154.9). MEASURED container = 14 GiB from a 12.1 GB worst-bundle measurement
(`HANDOFF.md`, 2026-08-04). MEASURED document counts `artifacts/reservoir/realized-tokens.json`
(56,839,223 docs for finephrase-table at 252B). DERIVED ×3.981 to 1T → 226.3 M × 85.9 B = 19.44 GB.
**SEVERITY** **corpus-corrupting via job failure** — an OOM at 1T kills the bundle *after* its full
billable read and tokenize, the same shape as the 25-of-27 end-of-run failure recorded at
`corpus_build.py:503-508`.
**FIX** Leave the `set` for a flat `np.uint64` array. **⚠️ Narrowing the key inside a `set` does NOT
help: CPython boxes ints, so set overhead is 41.9 B/entry independent of width and 128→64 bit saves only
9.3% (MEASURED `sys.getsizeof`: 128-bit int 44 B, 64-bit 36 B; DERIVED 85.9 − 44 = 41.9 B overhead).**
Flat array at 8 B/key: **1.81 GB worst bundle, 9.8 GB for all 1.23 B documents corpus-wide.** 64-bit
truncation costs 0.0408 probability that *any* collision exists corpus-wide → **expected false-duplicate
drops < 1 document.** No container change needed. ~20 lines. **$0.**

---

### F6 — Dedup is per-bundle; global dedup cannot be added as a shared set without destroying build determinism

**FINDING** `SeenHashes` is allocated fresh per `run_bundle` call, so no cross-source duplicate is ever
adjudicated; and the obvious fix (thread a shared set through the loop) would make bundle *k*'s output
depend on which bundles ran before it, destroying the byte-identical-digest property and silently
breaking resume.
**EVIDENCE** `corpus_build.py:482` passes `index=` and `stats=` but **not `seen=`**;
`corpus_filter.py:302` therefore allocates a new set; `run_bundle`'s signature
(`corpus_build.py:429-443`) has no `seen` parameter. Bundles stripe across array children
(`corpus_build.py:673`, `ingest_reservoir.py:764-779`), so a Python set cannot span them anyway.
`bundle_is_done` (`corpus_build.py:387-426`) skips completed bundles — a shared mutable set makes a
resumed build produce different data under the same `plan_id`. Deliberate decision, not an oversight:
`corpus_filter.py:219-221`, `HANDOFF.md:1899-1902`.
**SEVERITY** **quality-degrading at 252B, corpus-corrupting at 1T** — see F7 for why the scale changes
the verdict.
**FIX** A **separate sort-based pre-pass**, never a shared in-build set: per source, emit
`np.unique(uint64 hashes)` to S3; sort-merge the 14 arrays in one container (9.8 GB) applying an
**explicit source priority**; write an owner table the build *reads and never mutates*. This keeps
`run_bundle` a pure function of `(plan, owner_table)`, so determinism survives. DERIVED cost: one
text-column scan (~4.31 TB at the measured 4.31 chars/token mean) + one 9.8 GB sort ≈ **12–40 container-hours ≈ $12–40** at 1T, consistent with the design's own ~$3-at-252B Bloom estimate.
**Also fix the trivial part now:** add `seen=` to `run_bundle`'s signature so *within-container*
cross-bundle dedup is at least possible. 2 lines.

---

### F7 — The "cross-corpus dedup is deferrable" argument was computed for a 20B run and does not survive to 1T

**FINDING** The exposure arithmetic that justifies skipping fuzzy dedup gives ~1000× margin at a 20B
run and only **~4× at a 1.0T run** — the conclusion is scope-limited to the run size it was computed
for, and nothing in the design doc or code says so.
**EVIDENCE** `DATASET-DESIGN-reservoir.md:715-722` — exposures `N·Σw_s/S_s`, worst tabulated case 0.50
for a 20B run against Hernandez et al.'s (arXiv:2205.10487) damage threshold of 100. DERIVED: exposures
are linear in `N`, so ×50 for a 1.0T run → **25 exposures, 4× margin.**
**SEVERITY** **quality-degrading, and it is the key scale-dependent finding** — it converts "dedup is an
efficiency question" into "dedup is approaching a correctness question."
**FIX** Recompute the exposure table for the actual 1T mixture before accepting the deferral, and treat
F6's global exact pass as **required at 1T** rather than optional. Re-derive with the real weights, not
the 252B ones. **$0 (arithmetic), then F6's $12–40.**

---

### F8 — `FilterStats` never reaches the receipt, so the duplicate and contamination rates of our own corpus are unrecoverable

**FINDING** `run_bundle` computes `seen/kept/duplicates/contaminated/normalization` and returns them to
its caller, which prints them to stdout; the receipt schema has no slot for them, so every quantitative
claim about what dedup and decontam actually removed is unavailable from artifacts.
**EVIDENCE** `corpus_build.py:549` returns `"filter": filter_stats.as_dict()`; `:707-710` prints
`dup=… decon=…`. `Receipt`'s fields (`corpus_receipt.py:253-275`) and `to_dict` (`:287-308`) contain no
filter block; `grep -n "duplicates\|contaminated\|normalization" corpus_receipt.py` → **zero hits**.
`artifacts/reservoir/realized-tokens.json` harvested `tokens_out`/`documents`/`shards`/`unfilled` from
all 27 receipts and could not harvest duplicates.
**SEVERITY** **quality-degrading** — and it is the *blocking* finding: it is why several numbers in this
audit are DERIVED-from-mix rather than MEASURED. It also means `normalization`, the compatibility
surface every dedup decision depends on (`corpus_filter.py:44-47`), is unrecorded in the artifact that
claims the work.
**FIX** Add a `filter` block to `Receipt` carrying
`{seen, kept, duplicates, duplicates_crosssource, contaminated, normalization}`; bump
`RECEIPT_SCHEMA_VERSION` to `edullm-corpus-receipt/v2` and keep v1 in `READABLE_RECEIPT_SCHEMAS`
(`corpus_receipt.py:102-103`) so resume over existing receipts still works. ~30 lines +
1 line at `corpus_build.py:515-527`. **$0 to start recording; a re-run is needed to learn the numbers
for the existing corpus.**

---

### F9 — Which source wins a cross-source duplicate is an unrecorded, order-dependent decision waiting to happen

**FINDING** `dedup_and_decontaminate` keeps the first occurrence, which today adjudicates nothing
(F6: no cross-source comparison happens), but the moment global dedup lands the winner will be decided
by `_shard_slice` striping and Batch scheduling unless an explicit priority is written into the plan.
**EVIDENCE** `corpus_filter.py:307-309` (first-wins), pinned by `tests/test_corpus_filter.py:105-107`.
`plan_document` (`corpus_build.py:182-289`) has no `source_priority` field. Intra-bundle order is safe
and deterministic (reader file order, breaking only between files — `_reader_for`).
**SEVERITY** **cosmetic today, corpus-corrupting the day global dedup ships** — an unrecorded tie-break
makes two builds with the same `plan_id` produce different data.
**FIX** Add `"source_priority": [...]` to `plan_document`'s output. It is a pure function of the
registry so `plan_id` still content-addresses it — and it *changes* the `plan_id`, which is correct.
Recommended order, highest-quality-first:
`pes2o, pubmed, finewiki, stackexchange, stackv2-edu, finemath, ubuntu-irc, fineweb-edu, finepdfs-edu,
dclm, synthetic-finephrase-*`. The two load-bearing positions: **fineweb-edu above
synthetic-finephrase-\*** (the original beats its own rewrite) and **dclm last among real web** (least
filtered). ~15 lines. **$0.**

---

### F10 — Exact-hash-only is defensible, but URL-key dedup is the missing cheap stage, and we have already measured 52% overlap on our own pools

**FINDING** Exact content hashing catches only byte-identical text, which for independently-extracted
CC derivatives is a small fraction of real overlap; our own URL-set join measured **52.0% document
overlap** between two math pools, and the design's own step 1 (URL-key dedup, "Dolma's URL stage alone
removes ~53% of docs") is unbuilt — it would catch far more than content hashing at lower cost than
MinHash.
**EVIDENCE** `corpus_filter.py:87-105` (the normalization is CRLF/NUL/NFC/rstrip only — internal
whitespace is *not* collapsed, so a one-space-different document is kept).
`artifacts/1t-research/_url-join.json` MEASURED: MegaMath-Web vs FineMath-3+ = 41.5% exact-URL / **52.0%
normalized-URL**, Wilson-95 [51.59, 52.38]; vs InfiWebMath-3+ = 29.4% / 36.8%.
`DATASET-DESIGN-reservoir.md:342` for the Dolma ~53% figure. `grep -rn "Bloom\|MinHash\|lsh" src/` →
zero hits.
**SEVERITY** **quality-degrading** (efficiency at 252B; see F7 for 1T).
**FIX** Add URL-key dedup, **not** MinHash. Feasibility checked: `Document` (`corpus.py:167-194`) has no
URL field, but `CorpusSpec.domain_column` (`corpus.py:240`) is an exact precedent and
`read_parquet_documents` already resolves an optional extra column into its projection
(`corpus_read.py:487-499`). **`finemath` already uses `url` as its `id_column`** (registry), so it works
there today with zero schema change; `fineweb-edu`↔`finephrase` share a UUID id space
(`DATASET-DESIGN-reservoir.md:1621`) making an exact id join available for the biggest overlap pair.
**UNVERIFIED whether dclm_100BT / fineweb-edu / finepdfs-edu ship a `url` column — check the parquet
schemas before planning this.** DERIVED cost: +1.75% bytes read (memory note
`hf-filter-endpoint-fabricates-zeros` measured url as 1.75% of bytes), same 9.8 GB hash memory as F6,
one pre-pass. **~$12–40 at 1T, versus ~$382 for MinHash and its ~460 GB single-task union-find
(`DATASET-DESIGN-reservoir.md:1082`).**
**Do NOT add MinHash.** FineWeb measured global cross-dump MinHash as actively harmful (removed data
outperformed kept, arXiv:2406.17557 §3.4), DCLM measured Bloom-alone equal to the full
Exact+MinHash+SuffixArray stack at +1.6 CORE (arXiv:2406.11794), Pythia found no clear benefit.
See Literature section for verification status of each.

---

### F11 — The rephrasing hole has no detection defense, but its AMPLIFICATION is fixable for free and the shipped disclosure omits it

**FINDING** N-gram decontamination cannot see a rephrased benchmark item by construction, so 23.8% of
the corpus is effectively undecontaminated — correctly disclosed — but the disclosure is silent on the
measured 91–92.9% inter-format overlap and the 100% FineWeb-Edu document collision, which means a
consumer cannot know that a leaked item may appear ~5× under 5 source labels.
**EVIDENCE** `corpus_filter.py:175-195` (the only two tests are exact hash and 13-grams);
`corpus_filter.py:13-17` and `DATASET-DESIGN-reservoir.md:768-772` state the mechanism;
`artifacts/reservoir/PUBLISH-SPEC.md:121-130` is the shipped `limitations[]` block — it names the 59.6B,
the 23.7%, the 40/40 verification, and the un-run $200 tier, and says nothing about amplification.
**SEVERITY** **quality-degrading** for the detection gap (correctly disclosed, genuinely hard);
**corpus-corrupting** for the undisclosed amplification (see F1).
**FIX, three parts, in cost order:**
(a) **Docs-only, do it regardless:** extend `limitations[]` to state the measured 91.0–92.9% format
overlap and the FineWeb-Edu document-level collision. **$0.**
(b) **Decontaminate the SOURCE, inherit by id** — the best idea available and nobody proposed it: run
the 13-gram index over FineWeb-Edu *source* documents, collect flagged ids, and drop every FinePhrase
rewrite of those ids without ever n-gram-checking rephrased text. Reuses the same `IdSet` machinery F1
needs. ⚠️ Requires scanning FineWeb-Edu's full 1,094B pool (not just our 20B draw) to catch rewrites of
documents we did not draw — a text scan, no tokenizer, no GPU. **DERIVED ~10–30 container-hours ≈ $10–30.**
(c) **LLM-judge decontamination**: `lm-sys/llm-decontaminator` over the synthetic half, scoped at ~$200
for 60B (`DATASET-DESIGN-reservoir.md:774-777`), **DERIVED ~$800 at 1T's ~238B**. Do (a) and (b) first;
this only after a benchmark score comes in implausibly high.
**Not applicable as filters:** Min-K% Prob and every post-hoc membership-inference detector operate on a
*trained model*, not a corpus — useful as post-training verification, useless as a pre-training filter.
Embedding/semantic dedup is rejected by the design at ~$1,788 having "scored *below* the no-filter
baseline in DCLM Table 4" (`DATASET-DESIGN-reservoir.md:361-362`).

---

### F12 — The decontamination index cannot be rebuilt from this repo, and has one S3 copy plus one laptop copy

**FINDING** Rebuilding requires a sibling repo (`pipelines/week1_corpus`) and a pinned `ai2-olmo`
checkout at a specific SHA; if either is unavailable the index is unrebuildable, and `load_index`
correctly refuses to build without it, halting the pipeline.
**EVIDENCE** Builder at `pipelines/week1_corpus/src/week1_corpus/eval_bundle.py:218-290`, pinning
`ai2_olmo_revision = 6c3373fa182af2d57fe3c390ffc8420d5c5b325a` (manifest). `corpus_filter.py:34-35` —
"Rebuilding it needs a pinned `ai2-olmo` checkout." `corpus_filter.py:198-213` — `load_index` raises
rather than falling back to `empty()`. Copies: `s3://edullm-landing/_dist/eval-decontamination.bin`
(`corpus_filter.py:77-80`, `_dist/` verified to have no expiry rule) and the local
`pipelines/week1_corpus/config/eval-decontamination.bin`.
**SEVERITY** **quality-degrading** (a supply-chain/bus-factor risk, not a data defect). Mitigated: the
S3 copy is in the one prefix with no lifecycle expiry, and its sha256
`04aa8fe5…50bfd7` was verified equal to its manifest's claim.
**FIX** Since F2 and F3 both require a rebuild anyway, **vendor the builder into this repo during that
rebuild** — port `eval_bundle.py` (or the parts that read raw benchmark fields) into
`src/edullm_data/`, so the artifact that adjudicates the corpus lives with the code that uses it. This
is the same argument `CLAUDE.md` makes for keeping `DATASET-STANDARD.md` versioned alongside the code.
**~1 day of work, $0 compute, bundled with F2/F3.**

---

### F13 — The design doc and the code disagree about which n-gram rule is in force

**FINDING** The design specifies `allenai/decon` at `ngram_size 5, stride 10, threshold 0.8`; what
shipped is a reimplementation at `ngram_size 13, minimum_hits 2`. The substitution is undocumented, and
the two rules have materially different short-item and one-word-edit behaviour.
**EVIDENCE** `DATASET-DESIGN-reservoir.md:774` (the 5-gram spec) versus `corpus_filter.py:127-128`
(`ngram_size: int = 13`, `minimum_hits: int = 2`) and the shipped index header (13/2).
`corpus_filter.py:39-41` defends 13/2 on false-positive grounds but cites no comparison to 5-gram.
**SEVERITY** **cosmetic-to-quality-degrading** — 13/2 is *stricter* per match, so it false-positives
less and false-negatives more. Which is better for our suite is unmeasured.
**FIX** Either update the design doc to record the actual rule and why, or measure both. **Nobody has
measured 13/2 vs 5/0.8 on our corpus — that is a finding, not a gap I can close by reading code.**
Cheapest resolution: document the divergence (**$0**); measure only if a leak is suspected.

---

# LITERATURE — verified, with corrections to this repo's own claims

All entries MEASURED-PUBLISHED unless marked. Several correct claims made in `DATASET-DESIGN-reservoir.md`,
`corpus_filter.py`, and my own draft findings above.

## L1 — FineWeb's global-MinHash-is-harmful result: CONFIRMED VERBATIM

arXiv:2406.17557 §3.4. The repo's claim is exactly right. They trained on **(a) the ~31B tokens kept**
after full iterative cross-crawl dedup, and **(b) 171B tokens** obtained by independently deduplicating
the **~460B tokens that had been removed**. Verbatim: "the data from it that was kept (10% of the
original data) was actually **of worse quality** than the 90% of data that was removed. We confirmed
this by visual inspection: originally kept data contains more ads, incoherent lists of keywords and
generally badly formatted text."

Global dedup removed "as much as **90%**" of the oldest snapshots and yielded 4T tokens which "scored far
below RefinedWeb" at 350B. **They switched to per-snapshot MinHash** (5-grams, 112 hashes in 14 buckets
of 8, ~0.75 similarity) → 20T tokens, matching RefinedWeb. DERIVED from their 36T base pool:
per-snapshot removes 44.4%, global removes 88.9%. They then tested and **rejected** four additional
global stages on top (URL dedup 71.5% removed, line dedup 77.8%, line+min-words 85%, 3-line 80.9% — all
worse), concluding "we did not apply any additional deduplication beyond individual-snapshot MinHash."

**Verdict: the strongest single piece of evidence in favour of the current design's exact-only choice.**

## L2 — ⚠️ CORRECTION: DCLM's Bloom-alone figure is +2.1 CORE, not +1.6

`corpus_filter.py:7-9` and `DATASET-DESIGN-reservoir.md:348-349` both state "+1.6 CORE." **That is the
wrong row.** arXiv:2406.11794 Table 17 (1B-1x, 76B pool), Δ CORE from a 24.7 no-dedup baseline:

| method | removed | CORE | Δ |
|---|---:|---:|---:|
| Exact only | 13% | — | +1.3 |
| MinHash only | 18% | — | +0.9 |
| SuffixArray only | 33% | — | +1.9 |
| **Bloom alone** | **26%** | **26.8** | **+2.1** |
| **Exact+MinHash+SuffixArray** | **41%** | **26.8** | **+2.1** |

The **qualitative claim is confirmed and is stronger than stated**: Bloom alone *equals* the full
three-method stack (both 26.8 CORE) at 26% removal versus 41%. +1.6 is the MinHash+SuffixArray row.
Fix the two docstrings.

## L3 — ⚠️ MAJOR: DCLM Table 19's −11.8 MMLU is an N-GRAM-FLOOR result, and it VINDICATES the code's 13-gram choice over the design doc's 5-gram spec

This is the most decision-relevant finding in the literature sweep, and it resolves F13.

arXiv:2406.11794 Table 19, "Deduplication Ablations (7B-2x scale), 280B tokens" — MEASURED:

| method | min_ngram | shards | **MMLU** | CORE | yield |
|---|---:|---:|---:|---:|---:|
| Bloom Filter | **5** | 32 | **32.5** | 44.5 | 3.9T |
| Bloom Filter | **13** | 10 | **44.3** | 45.3 | 3.8T |
| Bloom Filter | 20 | 10 | 43.6 | 45.8 | 3.9T |
| MinHash+SA | N/A | 16 | 44.4 | 45.5 | 3.2T |

**32.5 − 44.3 = −11.8 exactly.** The row compares **BFF at min_ngram 5 versus BFF at min_ngram 13** —
same method, same corpus, only the minimum n-gram length differs. Caption: "a min_ngram_size of 5 again
yields competitive CORE results but drastically reduces MMLU." Reproduces at 7B-1x (Table 18: 26.3 vs
28.7 MMLU).

Three consequences:

1. **My memory note and the audit brief were both right that this is a dedup result, not decontam — but
   the precise mechanism is over-aggressive matching at a too-short n-gram floor**, which destroys MMLU
   while CORE barely moves (44.5 vs 45.3). It is the strongest published evidence that **CORE-style
   aggregates hide dedup damage that MMLU reveals.**
2. **It is about DEDUP, not decontamination — so it does not transfer directly to F13.** F13 is about the
   *decontamination* n-gram rule (13/2 shipped vs 5/0.8 specified). But the mechanism is the same
   (short n-grams match generic prose, so removal becomes indiscriminate), and it is the only
   quantitative evidence available on n-gram-floor choice. **It weighs in favour of the code's 13-gram
   rule and against the design doc's 5-gram spec.** REVISED VERDICT on F13: the code is probably right
   and the design doc should be corrected, not the code.
3. **Bonus, Table 23** — DCLM re-ran global MinHash over finished corpora and measured residual fuzzy
   duplicates: **DCLM-baseline 85%**, RefinedWeb-official 0%, RefinedWeb-ours 45%, Dolma V1 43%/36%.
   Authors: sharded dedup "fails to remove a large portion of the duplicates" yet "does not seem to
   adversely effect downstream performance. This calls into question the general prevailing thought that
   the presence of any duplicates hinders downstream performance."

## L4 — Exact vs fuzzy removal rates on CC derivatives

**Lee et al. arXiv:2107.06499**, Table 2 (% of train examples with a duplicate): C4 **3.04%** NearDup;
RealNews 13.63%; LM1B 4.86%; Wiki40B 0.39%. Token-level (Table 13): C4 177.3B → 173.7B NearDup
(**2.03%**) vs → 165.4B ExactSubstr (**6.71%**; the paper's own prose says 7.18%). "On average with
ExactSubstr, we remove more total content than with NearDup." Overlap: "77% of the training examples that
NearDup removes from C4 have at least one verbatim length-50 match found by ExactSubstr."

**RefinedWeb arXiv:2306.01116**, Fig 2 stage kept-rates: … fuzzy dedup 22.59% → **exact dedup 14.50%** →
URL dedup 11.67% final. Exact-on-top-of-MinHash "reducing by nearly 40% size of the dataset." MinHash:
9,000 hashes, 5-grams, 20 buckets of 450, applied **per-shard** (100 parts, each a hundredth of each
dump). Verdict: "MinHash alone is insufficient… Conversely, combining it with exact deduplication doesn't
improve performance further." Table 1 comparators: GPT-3 fuzzy MinHash ~10% removed; The Pile fuzzy
MinHash ~26%; OSCAR-21.09 exact per line ~55%.

**Dolma arXiv:2402.00159 §5.4** — three exact stages, **no fuzzy**: exact URL dedup filters **53.2%** of
documents; exact document dedup **14.9%** of URL-deduped; exact paragraph dedup **18.7%** of paragraphs.
The design doc's "~53%" figure (`:342`) is confirmed and is the **URL** stage. No isolated dedup delta is
published (Fig 3 compounds stages).

**C4/T5 arXiv:1910.10683 §2.2** — "discarded all but one of any **three-sentence span** occurring more
than once." Exact only. No isolated dedup delta (the ablation bundles dedup with bad-words filters).

## L5 — What our upstream sources already did (the answer that shrinks our remaining job)

| source | dedup performed upstream | scope | our residual job |
|---|---|---|---|
| **DCLM-baseline** (`dclm_100BT`) | Bloom (BFF), `min_ngram=13`, threshold 0.8, doc+paragraph | **per-shard: 100 shards × ~700 GB.** Explicitly *not* exact/URL dedup: "we do not use this form of deduplication in DCLM-Baseline" | **Largest. 85% residual fuzzy dupes by DCLM's own global re-run (Table 23).** `dclm_100BT` is a pure seed-42 subsample adding **zero** dedup |
| **FineWeb-Edu** | inherits FineWeb per-snapshot MinHash; **adds nothing** | per-snapshot | Card measures a **null result**: "deduplication of this dataset doesn't have any impact on model performance in our ablation setup (1.8B trained on 350B tokens)". Nemotron-CC Table 4: **1.3T total / 0.2T unique** |
| **FinePDFs / -edu** | two-stage: exact/byte then MinHash, buckets 32, hashes 10 | **global per language** (the one exception) | English 582.7M → 363.7M (exact) → 313.4M (filter) → **206.9M** (MinHash). "**>96% removed for FinePDFs-EDU**." Small residual |
| **FineMath** | "single-band MinHash-LSH" → FineMath-3+ 34B | not published | **Scope, shingle size, permutations, threshold all unpublished; no removal rate published.** Also does 13-gram decontamination vs GSM8k/MATH/MMLU/ARC |
| **FinePhrase** | **NONE. Zero dedup mentions on the card.** | — | **Inherits FineWeb-Edu's duplicate load and multiplies it 4×** with no output dedup |
| **Common Pile** ×5 | **global cross-source fuzzy**, verbatim §4.1 (arXiv:2506.05209): "global document-level fuzzy deduplication **across all sources**… bloom filter-based deduplication functionality from Dolma… duplicates if they share more than **90% of their 20-grams**" | **truly global** | **Removal rate not published.** No per-source pass. Our 5 Common Pile sources are the *best*-deduped inputs we have |
| Nemotron-CC (not used) | global fuzzy + exact substring over eighths of snapshots; params undisclosed | — | Table 4: 6.3T total / 4.4T unique |

**Two card-level cautions.** (a) FinePhrase's card does **not** state that the four formats are rewrites
of the same documents — but the counts force it: source 339,347,842 docs; faq 338,973,447 / math
338,747,732 / table 338,546,433 / tutorial 337,777,099, each ~99.8% of source, and `all` = 1,354,044,711
labeled "sum of configs." **Label this INFERRED-FROM-COUNTS, not card-stated** — our own 91.0–92.9%
id-overlap measurement (`reservoir_ids.py:9-14`) remains the primary evidence and is stronger.
(b) ⚠️ **Correction to project memory:** the FinePhrase card cites an unpublished `@misc` ("The Synthetic
Data Playbook") plus an HF Space blog. **arXiv:2604.13977 does not exist and is not on the card.** The
memory note `finephrase-is-real-and-central` should be corrected. The 486B figure is real
(`all` completion tokens = 486,367,076,933).

## L6 — DCLM × FineWeb-Edu overlap: 13.0%, MEASURED, and nobody set out to measure it

No paper's purpose is this, but **Zyda-2 (arXiv:2411.06068) Table I uniquely determines it.** GPT-NeoX
tokens (B), before → after cross-dedup: **DCLM 3.850 → 3.348**; Dolma-CC 1.209 → 0.969; Zyda-1 1.056 →
0.937; **FineWeb-Edu 1.319 → 1.319** (caption: "stays fixed because it is treated as the 'highest-rank'
deduplication dataset").

**DCLM loses 0.502T tokens = 13.0%**, and because the keep-rule is "FineWeb-Edu2 > DCLM > Zyda-1 >
Dolma-CC," a DCLM document dies *only* against a FineWeb-Edu2 document. Confirmed in prose: "we applied
this filtering after cross-deduplicating DCLM against FineWeb-Edu2, which involved eliminating all
samples in DCLM that were deemed duplicates with FineWeb-Edu2." Params: MinHash LSH, signature 128,
character 25-grams, 8 bands ≈ 0.85 Jaccard.

**13.0% is a LOWER bound**, two reasons: (a) deduped against FineWeb-Edu-**score-2** (~5.4T superset),
not the released 1.3T; (b) 0.85 Jaccard is strict, so same-URL/different-extraction overlap is not
counted. Zyda-2 separately reports **~80% internal** fuzzy dupes within *each* of DCLM and FineWeb-Edu —
intra-corpus, do not conflate.

**Do NOT cite Nemotron-CC Table 8 as overlap** — it measures *classifier agreement* on one snapshot
(union 11,359,655 docs, intersection 1,152,821 = 10.1%), which is two classifiers re-run, not the shipped
corpora. Checked and empty on this question: SmolLM2, OLMo 2, TxT360, WIMBD (arXiv:2310.20707),
Essential-Web, MAP-Neo, HPLT.

**Applied to our mix (DERIVED):** the registry draws DCLM 30B and FineWeb-Edu 20B. At 13.0% (lower
bound), cross-source exact+fuzzy duplication between just this pair is **~3.9B tokens = 1.6% of the
251.2B corpus**; at 1T holding shares, ~15.5B. That is real but modest — and it is *fuzzy* overlap at
0.85 Jaccard, of which the exact-hash-catchable subset is smaller still.

## L7 — ⚠️ The evidence that most undercuts my own F10 recommendation

**FineMath's card measured that deduplicating across math pools REDUCES performance:** "Deduplicating
the pages repeated between FineMath and InfiWebMath **reduces performance** compared to a
non-deduplicated combination." They left the cross-corpus overlap in **deliberately**.

That is the *same pair* my 52%-overlap URL-join measurement covers
(`artifacts/1t-research/_url-join.json`: MegaMath-Web vs FineMath-3+ 52.0% normalized-URL). So the
honest reading of that 52% is **not** "we must dedup it" — it is "a producer measured exactly this
overlap and found removing it harmful."

**REVISED VERDICT on F10: downgrade the URL-key dedup recommendation from "the missing cheap stage" to
"measure before building."** Three independent producers now measure cross-corpus/post-filter dedup as
neutral-or-harmful and skip it: FineWeb (L1, global harmful), FineWeb-Edu (null at 1.8B/350B),
FineMath (explicitly harmful on this exact pair), plus DCLM's own Table 23 (85% residual dupes, no
downstream harm). The counterweight is DCLM Table 19: if you *do* dedup, the n-gram floor matters far
more than the method, and CORE-style aggregates will not tell you when you have broken it.

---

# REVISIONS to the findings, after the literature sweep

The findings list above was written before the literature results arrived. Four findings change. I am
recording the revisions rather than editing the originals, so the reasoning is auditable.

### F10 — DOWNGRADED. Was "add URL-key dedup." Now "measure first; probably do not."

**Why it changed.** L7: FineMath's own card measured that deduplicating the FineMath ∩ InfiWebMath
overlap **reduces** performance, and left it in deliberately. That is the exact pair my 52% URL-join
number covers. Combined with L1 (FineWeb: global dedup harmful), L5 (FineWeb-Edu: dedup null at
1.8B/350B), and L3 bonus (DCLM Table 23: 85% residual dupes, no downstream harm), **four independent
producers have now measured cross-corpus or post-filter dedup as neutral-or-harmful.**

**Revised severity: cosmetic-to-quality-degrading, direction unknown.** My original framing treated the
52% overlap as damage. It is at least as likely to be *signal* — a page that survived two independent
filtering pipelines is a page two classifiers both liked.

**Revised fix:** do NOT build URL-key dedup on the strength of the overlap measurement alone. Instead
spend the same money on the **one measurement nobody has made for our corpus**: build the mix twice at a
small scale (deduped / not) and score on MMLU + GSM8K, not on a CORE-style aggregate (L3 is explicit that
aggregates hide dedup damage). That is a training run, so it belongs to the platform, not this repo.
**Exception that still stands unconditionally: the FineWeb-Edu ↔ FinePhrase id anti-join (F1).** That is
not cross-corpus fuzzy dedup — it is removing a document and its own rephrasing from the same corpus,
which no published result defends.

### F13 — RESOLVED IN THE CODE'S FAVOUR. Was "code and design doc disagree, unmeasured." Now "the code is right."

**Why it changed.** L3: DCLM Table 19 measures BFF at `min_ngram=5` versus `min_ngram=13` on the same
corpus at 7B-2x/280B and finds **MMLU 32.5 vs 44.3, a −11.8 gap**, reproducing at 7B-1x (26.3 vs 28.7).
Short n-gram floors match generic prose and make removal indiscriminate.

That is a *dedup* result, not a *decontamination* one, so it does not transfer directly — but it is the
only quantitative evidence available on n-gram-floor choice, and it points the same way. **The shipped
13-gram rule is better supported than the design doc's 5-gram spec.**

**Revised fix: correct the DESIGN DOC, not the code.** `DATASET-DESIGN-reservoir.md:774` should record
that the shipped rule is 13-gram/min-hits-2, why (false-positive control, plus DCLM Table 19 on the
n-gram-floor mechanism), and that `allenai/decon` at 5-gram was specified and deliberately not used.
**$0, docs only.** `minimum_hits=2` stands — do not lower it.

### F7 — STRENGTHENED, and now has a named counterweight

L1 and L3-bonus both support the deferral at 252B. But my exposure arithmetic (~4× margin at 1T versus
~1000× at 20B) is unaffected by any of the new evidence, because none of those papers ran a 1T-token
*read*. **The finding stands: recompute the exposure table for the real 1T mixture before accepting the
deferral.** The new evidence changes what to do if the margin is thin — L7 says the fix is *not*
necessarily "dedup more."

### F3 — STRENGTHENED. L5 supplies a concrete reason the missing MATH coverage matters.

FineMath already runs its own 13-gram decontamination against **GSM8k / MATH / MMLU / ARC** (L5). So
`finemath`'s 34B arrives partly defended on MATH — and **our index does not cover MATH at all** (F3), so
for every *other* source we have zero MATH defense while one source has some. That asymmetry is invisible
in any artifact. It does not change F3's severity (already corpus-corrupting) but it removes the "maybe
MATH does not matter" objection: a producer we ingest thought it mattered enough to build the index.

### One claim I should retract from my own draft

In Q3 I wrote that URL-level overlap of 52% "will catch far more than exact content hashing, and it is
cheaper than MinHash" and called it "the strongest concrete recommendation in Q3." **The first clause is
still true; the recommendation is retracted per F10 above.** Catching more duplicates is only a benefit
if removing them helps, and the published evidence on that is negative-to-null for exactly this case.

---

# THE DEDUP AND DECONTAM PLAN I WOULD ACTUALLY RUN

Ordered. Each step names its memory ceiling, its compute budget, and what it buys. Cheap-and-certain
first; nothing expensive is funded before the measurement that justifies it.

**Budget anchors, both MEASURED.** (1) The real 251.2B build finished in ~8 h at 12 concurrent
containers = **96 container-hours = 0.382 container-h per B tokens** (`HANDOFF.md`, 2026-08-04).
(2) `encode_batch` runs at **10.5 M tok/s per 32-vCPU container** (`corpus_pack.py:249`), so the
tokenize component is 0.0265 container-h per B — **DERIVED: reading and overhead are 93% of the cost,
tokenizing is 7%.** A scan-without-tokenize therefore costs almost as much as a full build; there is no
cheap pass over this corpus. Container = **14 GiB** unless stated.

---

### Step 0 — Write down the eval suite. (blocking, human, $0)

Nothing below is correct without it. F3 shows the index covers 9 OLMo-ladder families + MMLU + GSM8K-test
and **nothing else** — no MATH, HumanEval, MBPP, DROP, GPQA, MMLU-Pro, BBH. A corpus with 34B finemath and
40B stackv2-edu that gets scored on MATH or HumanEval is undefended and *reports success*.
**Deliverable:** an explicit list of benchmarks the 1T program will report. **Owner decision, not
engineering.**

### Step 1 — Rebuild the decontamination index. (F2 + F3 + F4 + F12; ~1 day eng, <$5 compute)

The highest value per dollar in the audit, and it fixes four findings at once.

- Add **raw** benchmark fields alongside the 5-shot renders: bare question; question+each choice; and
  **question+correct-answer** (which is ≥14 words for nearly every item, closing F4's short-item hole).
- Add every benchmark from Step 0 to `OE_EVAL_VARIANTS` (`eval_bundle.py:25-34`).
- Add GSM8K **train** (F3 sub-finding).
- **Vendor the builder into `edullm-data`** while you are in there (F12) — the artifact that adjudicates
  the corpus should not live in a sibling repo behind a pinned `ai2-olmo` SHA.
- **Memory:** index grows from 0.45 GB resident by ~+36% (DERIVED: ~1.1 M new 13-grams ≈ +18 MB on
  disk) → **~0.61 GB**. Fits the 14 GiB container trivially.
- **Compute:** one CPU job over ~150 K benchmark items. Minutes.
- **Verify before shipping:** re-run `tests/test_corpus_filter.py:252-284` against the new artifact, and
  add the same shape of test for **each** benchmark in Step 0 — a verbatim item must be caught, ordinary
  prose must not. The existing test passes only for GSM8K *because* GSM8K is the one naturally-shaped
  entry (F2); that must stop being true.

### Step 2 — Record what the filter did. (F8; ~30 lines, $0)

Add a `filter` block to `Receipt` — `{seen, kept, duplicates, duplicates_crosssource, contaminated,
normalization}` — bump `RECEIPT_SCHEMA_VERSION` to `edullm-corpus-receipt/v2`, keep v1 in
`READABLE_RECEIPT_SCHEMAS` (`corpus_receipt.py:102-103`) so resume over existing receipts still works,
and pass it at `corpus_build.py:515-527`.

**Do this before any re-run, because a re-run is the only way to learn these numbers and there will not
be a third one.** Every quantitative claim in this audit that is DERIVED-from-mix rather than MEASURED is
blocked on this one field.

### Step 3 — Fix the per-bundle OOM. (F5; ~20 lines, $0)

Replace `SeenHashes`' `set[int]` with a flat `np.uint64` accumulator + `np.unique`. **⚠️ Do not "fix"
this by narrowing the int inside the set** — CPython boxes ints, so set overhead is 41.9 B/entry
regardless of width and 128→64 bit saves only 9.3% (F5).

- **Memory:** worst bundle (`synthetic-finephrase-table--train` at 1T, 226.3 M docs) goes
  **19.44 GB → 1.81 GB**. Corpus-wide, all 1.23 B documents fit in 9.8 GB.
- **Correctness:** 64-bit truncation gives 0.0408 probability that *any* collision exists corpus-wide;
  **expected false-duplicate drops < 1 document**.
- Without this, three FinePhrase bundles plus stackv2-edu OOM at 1T *after* their full billable read and
  tokenize — the same end-of-run failure shape as `corpus_build.py:503-508`.

### Step 4 — Wire the id partition and the FineWeb-Edu anti-join. (F1 + F11a; ~5 lines + a re-plan)

The single largest data defect, and the code is already written and tested with **zero callers**.

- In `_reader_for`: FinePhrase streams get `if keeps_id(config, doc.id)`; the FineWeb-Edu stream gets
  `if not finephrase_ids.contains(doc.id)`. The ids are already read, so **zero extra I/O**.
- Stage the merged `IdSet` first — `ingest_reservoir.py:846` (`ids`) and `:960` (`merge`) already build
  it; the merge **refuses on a missing part** rather than silently producing a smaller set (`:993`),
  which is the right behaviour and must not be bypassed.
- **This changes the `plan_id`**, which is correct: a different tie-break rule is a different build.
- **Compute:** re-tokenize of the synthetic half only. DERIVED at 1T scale (~238B synthetic tokens):
  **~91 container-hours ≈ 1.4 h wall clock at 64 concurrent**. At the current 252B shape (59.8B):
  ~23 container-hours.
- **Margin check already done:** required keep fractions are 10.1 / 15.8 / 17.3 / 10.1% against the
  partition's 25% (`artifacts/reservoir/id-partition-verification.json`), so the per-format targets
  survive.

### Step 5 — Decontaminate the SOURCE and inherit by id. (F11b; ~50 lines + one scan)

The best available answer to the rephrasing hole, and nobody in the design proposed it. Run the rebuilt
13-gram index over **FineWeb-Edu source documents**, collect flagged ids, then drop every FinePhrase
rewrite of those ids **without n-gram-checking the rephrased text at all**. Reuses the `IdSet` machinery
Step 4 already stages.

- **Memory:** the flagged-id set is small — DERIVED, even at a 1% flag rate over 339 M source documents
  that is 3.4 M ids ≈ 27 MB at 8 bytes. Negligible.
- **Compute:** ⚠️ requires scanning FineWeb-Edu's **full** 1,094B-token pool, not just our draw —
  otherwise a rewrite of a contaminated document we never drew survives. Text scan, no tokenizer, no GPU.
  DERIVED: **~389 container-hours ≈ 6.1 h at 64 concurrent.** This is the most expensive step in the plan
  and it is still cheaper than the un-run $200/$800 LLM tier.
- **What it buys:** closes the rephrasing hole for exactly the subset of contamination an n-gram index can
  detect — which is the subset we can detect at all.

### Step 6 — Make the tie-break explicit. (F9; ~15 lines, $0)

Add `"source_priority": [...]` to `plan_document`'s output. Pure function of the registry, so `plan_id`
still content-addresses it. Recommended order, highest-quality-first:

`pes2o, pubmed, finewiki, stackexchange, stackv2-edu, finemath, ubuntu-irc, fineweb-edu, finepdfs-edu,
dclm, synthetic-finephrase-*`

Two load-bearing positions: **fineweb-edu above synthetic-finephrase-\*** (the original beats its own
rewrite) and **dclm last among real web** — now with published support, since DCLM is the weakest-deduped
input we ingest (L3-bonus: 85% residual fuzzy duplicates by DCLM's own global re-run; L5: `dclm_100BT` adds
zero dedup on top). Everything else is low-stakes.

Do this even if global dedup is never built — it costs nothing and it prevents the decision from being
made silently by Batch scheduling later (F9).

### Step 7 — Recompute the exposure table for the real 1T mixture. (F7; $0, arithmetic)

The "cross-corpus dedup is deferrable" conclusion was computed for a **20B run** and gives ~1000× margin
there. DERIVED: exposures are linear in `N`, so a 1.0T run gives **~25 exposures against a damage
threshold of 100 — a 4× margin, not 1000×.** Redo it with the actual weights before accepting the
deferral. If the margin is thin, Step 8 is the *measurement*, not more dedup (L7).

### Step 8 — Global exact dedup: BUILD THE PRE-PASS, GATE THE REMOVAL. (F6; ~150 lines, $12–40)

Build the machinery; make removal a flag that is off until Step 9 says otherwise.

- **Shape: a separate sort-based pre-pass, never a shared in-build set.** Per source, emit
  `np.unique(uint64 hashes)` to S3; sort-merge the 14 arrays in one container applying Step 6's priority;
  write an **owner table the build reads and never mutates**. This keeps `run_bundle` a pure function of
  `(plan, owner_table)` — **which is the whole point**: a shared mutable set would make bundle *k*'s
  output depend on which bundles ran before it, destroying the byte-identical-digest property that
  `corpus-build-is-deterministic` records over 9 bundles / 4,137 shards, and silently breaking resume
  (F6).
- **Memory:** 9.8 GB for all 1.23 B documents at 8 bytes, one container. Fits 14 GiB.
- **Compute:** the hash pre-pass is a full text scan — DERIVED **~356 container-hours ≈ 5.6 h at 64
  concurrent**, then a 9.8 GB sort in one container. **~$12–40**, consistent with the design's own ~$3
  estimate at 252B.
- **Also fix the 2-line part now:** add `seen=` to `run_bundle`'s signature so within-container
  cross-bundle dedup is at least *possible* (`corpus_build.py:429-443`).
- **Ship it emitting counts only, removal disabled.** That gives Step 9 its input for free and tells us
  the real cross-source duplicate rate — the number this audit could not measure (F8).

### Step 9 — Measure whether removal helps, on MMLU/GSM8K, before enabling it. (platform, not this repo)

**This is the step that decides Step 8's flag, and it is the only honest way to set it.** Four independent
producers measured cross-corpus or post-filter dedup as neutral-or-harmful — FineWeb (global harmful, L1),
FineWeb-Edu (null at 1.8B/350B, L5), FineMath (explicitly harmful on the pair we measured at 52%, L7), and
DCLM's own Table 23 (85% residual duplicates, no downstream harm, L3). The counterweight is DCLM Table 19:
if you dedup wrong, it costs **−11.8 MMLU while CORE barely moves** (L3).

So: build the mix twice at small scale, deduped and not, and **score on MMLU and GSM8K — not on a
CORE-style aggregate**, which L3 proves hides exactly this damage. Goes through the
`edullm-platform-runs` path.

⚠️ **One caveat on our own probe scale:** memory note `mcf-scoring-is-random-below-400b-tokens` records
OLMES Fig 1 saying MC answering is a separately-acquired skill that is near-random below ~400B tokens. A
small-scale MMLU comparison may be measuring noise. **Score CF (cloze/completion) formulations, or run
the comparison at a scale where MC is above chance** — otherwise this step produces a number that looks
like an answer and is not one.

### NOT in the plan, and why

| rejected | reason |
|---|---|
| **MinHash / LSH fuzzy dedup** | L1 (FineWeb: global harmful, removed data scored better), L2 (DCLM: Bloom alone *equals* the full Exact+MinHash+SuffixArray stack at 26.8 CORE), Pythia (no clear benefit). ~$382 at 1T plus a ~460 GB single-task union-find. **Buys ~0 by every published measurement.** |
| **URL-key dedup** | **Retracted mid-audit (F10 revision).** I recommended it on the strength of our own 52% URL-overlap measurement, then found FineMath measured that removing *that exact overlap* reduces performance. Revisit only if Step 9 says removal helps. |
| **Semantic / embedding dedup** | ~$1,788 at 252B (~$7,000 at 1T), "scored *below* the no-filter baseline in DCLM Table 4, second-worst of 19 samplers in the Ask-LLM benchmark, shipped by **zero** flagship corpora" (`DATASET-DESIGN-reservoir.md:361-362`). |
| **LLM-judge decontamination** | ~$200 at 60B → ~$800 at 1T. Do Steps 4+5 first — they address the same hole structurally for ~$40 and, unlike an LLM pass, they fix the 4× amplification too. Revisit only if a benchmark score comes in implausibly high. |
| **Lowering `minimum_hits` to 1** | 2 hits is well calibrated (40/40 GSM8K caught, 0/2 prose false-positive, `tests/test_corpus_filter.py:265-284`) and L3's −11.8 MMLU shows what over-aggressive n-gram matching costs. Leave it. |
| **Switching decontam to 5-gram per the design doc** | L3 measures min_ngram 5 vs 13 at **MMLU 32.5 vs 44.3**. The shipped 13-gram rule is better supported. **Correct the design doc instead** (F13 revision). |

### Documentation owed regardless of what gets funded ($0, do it this week)

1. **`limitations[]` is incomplete** (F11a). It names the 59.6B undecontaminated synthetic half honestly
   and says nothing about amplification. Add the measured **91.0–92.9% inter-format id overlap** and the
   **100% FineWeb-Edu document-level collision**. A consumer currently cannot know a leaked item may
   appear ~5× under 5 source labels.
2. **The `+1.6 CORE` figure is wrong in FIVE places** — DCLM's Bloom-alone number is **+2.1 CORE**
   (L2); +1.6 is the MinHash+SuffixArray row. The qualitative claim ("equal to the full stack")
   survives and is *stronger* than written. Grepped, all five sites:
   `src/edullm_data/corpus_filter.py:8`, `HANDOFF.md:80`, `HANDOFF.md:1898`,
   `DATASET-DESIGN-reservoir.md:348`, `DATASET-DESIGN-reservoir.md:739`.
3. **Fix `corpus_filter.py:33-34`** — MMLU `dev` is in the index only as the embedded 5-shot preamble, not
   as items (`eval_bundle.py:150-155`). The "{dev, validation, test}" claim is wrong.
4. **Record the 13-gram-vs-5-gram divergence** in `DATASET-DESIGN-reservoir.md:774` with L3 as the
   justification (F13 revision).
5. **Correct project memory** `finephrase-is-real-and-central`: **arXiv:2604.13977 does not exist** and is
   not on the FinePhrase card; the citation is an unpublished `@misc` plus an HF Space blog (L5). The 486B
   token figure is real.

### Measurements nobody has made — stated as findings, not gaps I can close by reading code

- **The duplicate and contamination rate of our own corpus.** Blocked on F8/Step 2. Exists only in the
  CloudWatch logs of the 2026-08-05 run.
- **The fraction of MMLU/ARC items under 14 words.** One CPU-minute over the HF datasets. I did **not**
  measure it and deliberately did not estimate it as fact (F4).
- **13-gram/min-hits-2 versus 5-gram/0.8 as a DECONTAMINATION rule on our corpus.** L3 measures the
  analogous *dedup* choice and points at 13; nobody has measured the decontamination version.
- **Whether `dclm_100BT` / `fineweb-edu` / `finepdfs-edu` ship a `url` column.** Needed before URL dedup
  could ever be planned (F10). `finemath` already uses `url` as its `id_column`.
- **Common Pile's global-dedup removal rate.** The paper describes the stage (90% of 20-grams, Dolma BFF,
  across all sources) and publishes **no count, byte, or percentage** (L5).
- **Whether removing cross-source duplicates helps or hurts *our* mix.** Step 9. Four producers say
  neutral-or-harmful; none of them ran our mixture.
