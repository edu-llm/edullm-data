# eng-04 — plan surface (SHARD_TOKENS + C3b bundle split)

**Agent:** eng-04, ENG-EXEC wave, branch `agent/eng-04/plan-surface`, worktree
`/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/eng-04--plan-surface`
**Started:** 2026-08-08
**Owns:** `load_registry` (corpus_build.py:130), `plan_document` (:182), `SHARD_TOKENS` (corpus.py:89),
`allocate_ordinals` (corpus.py:315), `artifacts/reservoir/corpus-registry.json` rows.

Grades used throughout: MEASURED / MEASURED-IN-CODE / DERIVED / CARD / UNVERIFIED.

## STATUS: starting — reading briefings

## Orientation notes (read, not yet acted on)

- Briefing `artifacts/impl-plan/task-28-briefing.md` read in full. Its §2.1 trap 1 reproduced
  below by execution (not by reading).
- **The registry I own is the RESERVOIR registry, not a 1.0T final-dataset registry.**
  `artifacts/reservoir/corpus-registry.json` has 17 rows summing to **252.6B** `target_tokens`
  (MEASURED-IN-CODE, summed below), NOT 1.0T. `dclm-baseline` there is **30B**, not 410B.
  There is no `dclm` row at 410B anywhere in the repo's registry. **This is a scope finding:
  the 410B/252B figures in the plan are for a corpus whose registry does not exist yet.**
  See "Registry scope" section below for the full reconciliation.

---

# B6 / task #9 — `SHARD_TOKENS`: CONFIRMED at 25,001,984, but the docstring's justification is FALSE

**Verdict: DO NOT CHANGE THE VALUE.** `SHARD_TOKENS = 3052 * SEQ_LEN = 25,001,984` stays.
**But constraint 2 in its docstring (`corpus.py:80-89`) no longer holds at 1.0T, and I am rewriting
the docstring** (a docstring is not in `plan_document`, so `plan_id` is unaffected — verified: the plan
document embeds `SHARD_TOKENS` the integer, `corpus_build.py:256`).

## The inputs, graded

| input | value | grade |
|---|---|---|
| `SEQ_LEN` | 8192 | MEASURED-IN-CODE `corpus.py:78` |
| `SHARD_TOKENS` | 3052 × 8192 = **25,001,984** | MEASURED-IN-CODE `corpus.py:89` |
| shard bytes | 25,001,984 × 4 = **100,007,936** (100.0 MB) | DERIVED |
| Gate A cost | **507.5 ms/object**, 8 round trips | MEASURED-LIVE, `pretrain_tokens_v1.py:205-210` (10,049 objects / 85 min) |
| live validator timeout | **14,400 s** (`edullm-validator` rev **14**) | **MEASURED — I re-confirmed it myself** via `batch describe-job-definitions --status ACTIVE` on 2026-08-08. Revs 7-13 are 7200 s; revs 1-6 are `timeout: null`. PLAT-EXEC's figure is right. |

## The recomputation — and it FAILS on the docstring's own terms

`objects × 0.5075 s`, serial (`head_workers=1`). "hw16" threads 1 of 8 calls, so it is `7/8 + 1/128` = 0.883×.

| corpus | objects @25M | Gate A serial | @hw16 | vs **14,400 s** |
|---|---|---|---|---|
| reservoir 252.6B (what the docstring was written for) | 10,103 | **1.42 h** | 1.26 h | ✅ fits, 2.8× margin |
| **1.0T, one dataset** | **39,997** | **5.64 h (20,299 s)** | 4.98 h | ❌ **EXCEEDS by 41%** |
| **1.0T stage 1 only (900B)** | **35,997** | **5.07 h** | 4.48 h | ❌ **EXCEEDS by 27%** |
| 1.0T stage 2 only (100B) | 4,000 | 0.56 h | 0.50 h | ✅ |
| *hypothetical* 50,003,968 @1.0T | 19,998 | 2.82 h | 2.49 h | ✅ |

All DERIVED on the MEASURED 507.5 ms/object.

**Break-even: 28,373 objects = 709.4B tokens** is the most a 25M-shard corpus can hold and still clear
14,400 s serially. 1.0T is 1.41× over that. **Even splitting into the planned two datasets does not
rescue it** — stage 1 alone is 36,000 objects.

## ⚠️ DEVIATION FILED — the plan's own justification for 25M is now circular

(a) **Plan claim:** `corpus.py:85-88` — *"~100 MB / ~10,400 objects is the finest granularity whose Gate A
pass (~1.4 h) still fits the 7200 s validator timeout with margin."*
(b) **Countervailing evidence:** BOTH premises moved. The timeout is 14,400 s (MEASURED above), and the
object count at 1.0T is ~40,000, not ~10,400 (the 10,400 is the **252.6B reservoir**, a 4× smaller corpus).
The two errors do NOT cancel: the timeout doubled, the objects quadrupled.
(c) **Numbers it moves:** Gate A at the shipping size goes 1.38 h → **5.64 h**; margin goes ×5.2 → **×0.71**.
(d) **Blast radius:** none to the value, because the constraint that actually settles it is different — see below.

## Why the value survives anyway (three reasons, and only the third is load-bearing)

1. **Constraint 1 still binds and is satisfied.** `25,001,984 % 8192 == 0` exactly (3052.0). VERIFIED by
   execution. `check_seq_len_alignment` (`profiles/pretrain_tokens_v1.py:426`) recomputes
   `bytes % (dtype_size * seq_len)` from a real `head` and requires zero. 50,003,968 would also pass
   (6104.0), so this does not discriminate — it only rules out non-multiples.
2. **Mixture error is 2× better at 25M** and both are negligible: `1/shards_per_component` gives
   **0.007%–0.278%** at 25M vs 0.33% worst case at 50M (`FINAL-DATASET-REPORT.md:363-365`). Not decisive.
   ⚠️ Note `DATASET-DESIGN-reservoir.md:537` still carries the **"50M → 4.8% @5% weight"** table, which
   `HANDOFF-FINAL-DATASET.md:175` says is **"False by ~15×"** and was used to justify a decision. Do not
   cite that table.
3. **The binding fix is #10, not the shard size.** Threading the Gate A profile checks + raising
   `max_pool_connections` takes 40,000 objects to **0.32-0.36 h** (DERIVED at 16× on all 8 calls).
   `FINAL-DATASET-REPORT.md:367-370` and `IMPLEMENTATION-PLAN.md` §8.3 both already say this, and
   `TASKS.md:44` schedules #10 for exactly it. Halving the shard would buy 2.8 h; #10 buys 5.3 h and
   is needed regardless. **Changing `SHARD_TOKENS` to dodge a validator cost that #10 removes would be
   paying a permanent schema price for a temporary code defect.**

## ⛔ THE ONE THING ENG-EXEC MUST CARRY FORWARD

**#10 is now a HARD PREREQUISITE of publishing 1.0T at this shard size, not an optimization.**
At 25M shards and 1.0T, Gate A does not fit rev 14's 14,400 s even with `--head-workers 16`, and
**stage 1 alone still does not fit.** Either #10 ships, or the validator job def needs a 5th revision at
≥ 25,000 s, or the corpus is published in ≥ 3 datasets. This is a schedule dependency the graph does not
currently show as blocking.

## Other shard-size figures still in flight (file + line, as requested)

**`50,003,968` — the withdrawn value, still asserted as "the decided" figure in 12 places:**
- `artifacts/impl-plan/pipeline-scale-audit.md:99-100, 106, 414, 487, 696, 703-704, 759, 882, 1183-1185, 1196, 1267`
  — this artifact reasons *throughout* from "the decided shard size of 50,003,968" and its :106 says
  *"The code has not been updated to the 50,003,968 decision"* — i.e. it has the polarity backwards; the
  **code was right and the doc was wrong**. `orchestrator-findings.md:32-33` records the withdrawal.
- `artifacts/impl-plan/wallclock-audit.md:784-789, 893` — same, though :786 correctly says
  *"I project the code's shard size, because that is what would run today."*
- **These are historical evidence artifacts; I did not rewrite them.** But anyone grepping for a shard
  size will hit `pipeline-scale-audit.md` and get the withdrawn number stated as a decision. **Recommend
  ENG-EXEC add a one-line withdrawal banner at the top of `pipeline-scale-audit.md`** rather than editing
  1,196 lines of downstream arithmetic.

**`10M` — a rejected candidate, cited with a stale reason:**
- `DATASET-DESIGN-reservoir.md:539` — *"10M | 26,000 | 3.45 h — exceeds the 7200 s timeout"*. **The
  timeout premise is now false (14,400 s), so 10M would in fact fit it.** 10M remains correctly rejected
  on the *other* stated ground (doubling OLMo-core's linear `self.offsets` scan in `__getitem__`), which
  is unaffected. Flagged so nobody "reopens 10M because the timeout moved" and then re-derives the wrong
  conclusion — the surviving objection is the trainer hot loop, not the validator.

**Object-count figures that are correct-for-the-reservoir and wrong-for-1.0T if read as current:**
- `DATASET-DESIGN-reservoir.md:84, 554` — "~10,400 objects". Correct for 252.6B. That doc is
  self-scoped to the reservoir so I read it as accurate, not stale.
- `CLAUDE.md:39` and `HANDOFF-FINAL-DATASET.md:249` both correctly say **~40,000 objects at 1.0T**. ✅
- `docs/FINAL-DATASET-MIX.md:16`, `docs/FINAL-DATASET-REPORT.md:357-360` ✅ agree with the code.

**No third value found.** Grepped `25,001,984|25001984|3052|SHARD_TOKENS|50,003,968|50003968|6104|10M`
across `*.md`, `*.py`, `*.json`, `*.yaml`. The repo carries exactly two shard sizes: the live 25,001,984
and the withdrawn 50,003,968.

---

# C3b / task #28 — part 1: the silent-loss guard. **LANDED.**

## Trap 1 reproduced by execution, with a number (MEASURED-IN-CODE, I ran it)

Two rows, `source_label="dclm"` both, `config` `dirA`/`dirB`, 100 and 200 shards' worth:

```
bundles emitted: 2          <- looks correct
   dclm--train  spec_key=dclm-b  config=dirB  tokens=4,975,394,816
   dclm--val    spec_key=dclm-b  config=dirB  tokens=   25,001,984
   tokens IN  = 7,500,595,200
   tokens OUT = 5,000,396,800   LOST = 2,500,198,400 (33.3%)
```

**Both halves of the trap fire at once and neither raises:** row A's 2.5B tokens vanish, AND the
surviving bundle carries `config=dirB` only — so under an N-way split every child would read `dirB`.
The plan looks internally consistent: bundle count right, ordinals dense, token sums add up.

## What landed

- **`corpus_build._assert_unique_identities(specs, where=)`** — new private helper.
  Checks **`source_label` AND `key`**. I added `key` on my own initiative because it is the same bug
  class one step later: `_cmd_run` does `{s.key: s for s in load_registry(...)[0]}`
  (`corpus_build.py:672`) and `plan_document` looks up `tokens_per_source` by `spec.key` (`:206`), so
  a duplicate key routes a build to the **wrong upstream repo** — plan names one source, run reads
  another. Same silence.
- Called from **`load_registry`** (the file path) **and from `plan_document`** (the in-memory path).
  Both, deliberately: `plan_document` is reachable without any registry file, and the caller most
  likely to collide is precisely the one constructing N split rows in memory. A guard only at the
  file reader would be bypassed by the exact use case it exists for.
- Error message names the fix (`dclm-01 … dclm-NN`) **and** warns that the label is permanent and
  inside `manifest_sha256`.

## The 6 new tests — each recomputes

`tests/test_corpus_build.py`:
1. `test_two_rows_sharing_a_source_label_would_silently_LOSE_tokens_so_they_are_refused` — asserts
   the raise, then **measures the loss** by planning each row separately and comparing sums; asserts
   `lost/total > 0.3`. Not a field-presence check.
2. `test_the_colliding_config_is_what_makes_it_worse_than_a_hole` — proves the *repaired* form keeps
   **both** configs (`{"dirA","dirB"}`) and loses no tokens. This is the property C3b depends on.
3. `test_two_rows_sharing_a_key_are_refused_because_run_resolves_specs_by_key` — plus a live
   demonstration that `{s.key: s for s in rows}` really does collapse to 1.
4. `test_the_shipping_registry_has_unique_identities` — the real 17-row registry round-trips clean
   (guard is not one the shipping data trips).
5. `test_load_registry_refuses_a_registry_file_with_a_duplicated_label` — writes a real registry JSON
   with a hand-injected duplicate and asserts the file reader rejects it. This is the path a
   hand-edited split row actually takes.
6. (counted above) — all 38 tests in `test_corpus_build.py` pass.

---

# C3b part 2 — the DCLM repo reconciliation. **MEASURED. ESCALATING TO ENG-EXEC / CEO.**

I walked both trees myself against the HF tree API on 2026-08-08. Everything in this section is
MEASURED unless marked.

## The two repos are NOT interchangeable

| | **A. `HuggingFaceFW/dclm_100BT`** (registry today) | **B. `mlfoundations/dclm-baseline-1.0-parquet`** (the free carve) |
|---|---|---|
| registry row | `dclm-baseline`, `config: "data"`, `target_tokens` 30B | not in the registry |
| revision pinned | `01022d378d944de6deeb1c79d08fecb4d27b2c6f` | `817d6752765f6a41261085171dd546b104f60626` (2024-07-19) |
| **tree shape** | **100 flat files, ZERO directories** | `filtered/OH_eli5…/fasttext_openhermes…/processed_data/` → **10 × `global-shard_NN_of_10`** → **10 × `local-shard_N_of_10`** = **100 disjoint dirs** |
| **files** | **100** | **27,938** |
| **bytes (parquet)** | **316,008,772,992 = 316.0 GB** | **7,419,668,271,828 = 7.420 TB** |
| **license (HF API `cardData`)** | **ODC-BY-1.0** (per registry row) | **`cc-by-4.0`** — I fetched the API and read it |
| pool_tokens | 114.69B (MEASURED, `artifacts/sizing-revised.md:40`, exact rows, `partial:false`) | **~2,693B DERIVED** (see below) |
| size ratio | 1× | **23.5× larger by bytes** |

**Repo B is 23.5× the bytes of repo A.** Repo A cannot supply 410B tokens — its whole measured pool is
114.69B. **A 410B DCLM draw is only possible from repo B (or from `mlfoundations/dclm-baseline-1.0`,
which is `.jsonl.zst` and which `corpus_read` refuses).** So this is not a preference between two
equivalent sources; **the 410B figure presupposes repo B.**

### Repo B's token pool — DERIVED, and I am flagging the assumption

Repo A gives a measured anchor: 316.008 GB parquet ↔ 114.69B tokens = **2.7553 parquet-bytes/token**.
Applying it to repo B: `7.4197e12 / 2.7553` = **~2,693B tokens (DERIVED)**.
⚠️ This assumes equal parquet compression and equal content distribution between a 100BT subsample and
the full baseline. It is an order-of-magnitude check, **not a measurement.** The plan's §284-285
independently grades repo B at *"~3,764B DERIVED"*; mine is 28% lower. **Either way the pool clears a
410B draw by >6×, which is all the split sizing needs.** Do not use either figure for a mix decision.

## ✅ Two of the briefing's open questions are now CLOSED by measurement

**Briefing §5.2 — "I did NOT verify the 10 global-shard dirs are equal-sized."** They are, essentially
exactly. **MEASURED, all 10 walked:**

| dir | files | bytes |
|---|---|---|
| `global-shard_01_of_10` | 2,790 | 741,607,714,140 |
| `global-shard_02_of_10` | 2,790 | 741,797,320,215 |
| `global-shard_03_of_10` | 2,790 | 741,948,813,220 |
| `global-shard_04_of_10` | 2,798 | 742,363,565,526 |
| `global-shard_05_of_10` | 2,800 | 742,705,748,852 |
| `global-shard_06_of_10` | 2,797 | 742,065,678,556 |
| `global-shard_07_of_10` | 2,799 | 742,251,245,815 |
| `global-shard_08_of_10` | 2,800 | 742,820,572,394 |
| `global-shard_09_of_10` | 2,784 | 740,981,463,317 |
| `global-shard_10_of_10` | 2,790 | 741,126,149,793 |
| **total** | **27,938** | **7,419,668,271,828** |

**max/min = 1.0025 — 0.25% skew.** The briefing's worry that *"N equal `target_tokens` rows will not
produce N equal children"* **does not apply to this carve.** N equal rows give N equal children to
within 0.25%. (Caveat: bytes, not tokens. A tokens/byte skew across shards would not show here — but
these are random-assigned shards of one crawl, so a systematic skew would be surprising.)

Independent corroboration: **27,938 files exactly reproduces `IMPLEMENTATION-PLAN.md` §4.1's "27,938
files"** figure, derived by a different agent by a different route. Two independent walks agreeing on a
5-digit count is good evidence the tree is what we both think it is.

**Briefing §5.4 — "whether `hf_files` pagination completes on a 100-way listing is UNTESTED."**
**It completes.** I ran the real `corpus_build.hf_files` against the live API (read-only, no AWS, no
write):

| `config` | files returned | unique paths | bytes | wall |
|---|---|---|---|---|
| `…/global-shard_01_of_10/local-shard_0_of_10` | **279** | 279 | 74.18 GB | 1.8 s |
| `…/global-shard_01_of_10` (10 locals) | **2,790** | 2,790 | 741.61 GB | 6.5 s |

2,790 > the 1,000 page limit, so the `Link`-header loop (`corpus_build.py:874-885`) **paginated
correctly across 3 pages with zero duplicates and zero truncation.** MEASURED. The briefing's concern
was its own fetch tool truncating, not the code.

## ⚠️ DEVIATION #2 — the report's own pool figures CONFIRM repo B, and settle the reconciliation

(a) **Plan claim:** `FINAL-DATASET-REPORT.md:83` — DCLM-baseline pool **744.6B**, grade MEASURED.
The registry's `dclm-baseline` row measures its pool at **114.69B** (`HuggingFaceFW/dclm_100BT`).
(b) **Evidence:** those cannot be the same source. 744.6B ≠ 114.69B, and 114.69B **cannot supply the
378B stage-1 draw** (it would be 3.3 epochs of a source the report grades at 0.51 epochs).
**The report's DCLM row was never about the registry's repo.** The reconciliation is therefore not a
choice — repo A is arithmetically incapable of the planned draw.
(c) **Numbers it moves:** none in the mix. It renames which upstream the row points at.
(d) **Blast radius:** license string changes ODC-BY-1.0 → **cc-by-4.0** (both permit the use; §1.5 says
record the string, never model it as a boolean). Revision changes. `pool_tokens` changes.

### The same test run on FineWeb-Edu **finds the identical mismatch, and this one is NOT in the plan**

**This is a finding nobody has filed.** The registry's `fineweb-edu` row is `config: "sample/100BT"`,
pool **100.24B**, target **20B**. The report asks for **252B** from a pool of **1,583.1B**.

I derived the full-repo pool independently and it lands on the report's number:

| subset | bytes (MEASURED, HF tree API) | tokens | **bytes/token** |
|---|---|---|---|
| `sample/10BT` | 28,518,193,415 | 10B (CARD) | 2.8518 |
| `sample/100BT` | 286,394,522,604 | **100.24B (MEASURED, registry)** | **2.8571** |
| `sample/350BT` | 998,102,051,512 | 350B (CARD) | 2.8517 |
| **`data/` (full, 110 dirs, 2,410 files)** | **4,522,727,684,984 = 4.523 TB** | **→ 1,583.0B DERIVED** | — |

**1,583.0B DERIVED vs the report's 1,583.1B — a 0.01% match**, from a bytes/token ratio that is stable
to 0.19% across three independently-sized subsets. **This is strong evidence the report's FineWeb-Edu
row means `HuggingFaceFW/fineweb-edu` `data/` (the whole repo), not `sample/100BT`.** And again the
registry's configured pool (100.24B) cannot supply the 252B draw — it would be 2.5 epochs against a
declared 0.16.

**So the repo/config reconciliation is TWO rows, not one.** ENG-EXEC's brief names only DCLM. FineWeb-Edu
has the same defect and I found it by running the same check.

## The carve, measured on both sources

### DCLM — repo B, `global-shard` level. Near-perfect balance.
Anchor: 316.008 GB / 114.69B tok = **2.7553 bytes/token** (MEASURED both sides, from repo A).

| N | via | per-row target | one dir holds (DERIVED) | verdict |
|---|---|---|---|---|
| **5** (32 vCPU) | `global-shard_NN_of_10` | 82.0B | **269.2B** | ✅ **3.28× headroom** |
| 10 | `global-shard_NN_of_10` | 41.0B | 269.2B | ✅ 6.56× |
| **20** (8 vCPU) | `local-shard_N_of_10` | 20.5B | **26.9B** | ✅ 1.31× — tight but clears |
| 13 | `local-shard_N_of_10` | 31.5B | 26.9B | ❌ **TOO SMALL** — a 13-way local carve does not fit |

**Skew 0.25%** (measured above), so N equal rows give N equal children.

### FineWeb-Edu — `data/CC-MAIN-*`, and it is a WORSE carve. **This is the real constraint.**
110 dirs, but **3.1× skew** (max `CC-MAIN-2023-40` 70.9 GB = 24.8B tok; min `CC-MAIN-2016-26` 22.8 GB
= 8.0B tok). One-dir-per-row **fails below N=16**:

| N | per-row target | dirs large enough | verdict |
|---|---|---|---|
| **4** (the 32-vCPU figure in the plan) | 63.0B | **0** | ❌ **INFEASIBLE — no single CC-MAIN dir holds 63B tokens** |
| 5 | 50.4B | 0 | ❌ |
| 8 | 31.5B | 0 | ❌ |
| 13 (the 8-vCPU figure) | 19.4B | 11 | ❌ (need 13) |
| **16** | 15.75B | 31 | ✅ |
| 20 | 12.6B | 73 | ✅ |

**⚠️ THIS BREAKS THE PLAN'S 4-WAY FINEWEB-EDU SPLIT AS A ONE-DIR-PER-ROW OPERATION.** The plan
(`IMPLEMENTATION-PLAN.md` §8A.3) says FineWeb-Edu needs 4 ways at 32 vCPU. **A 4-way split cannot be
expressed as 4 rows each naming one `CC-MAIN-*` dir**, because the largest dir in the repo is 24.8B
tokens and each row needs 63B.

**The fix is that `config` does not have to be one directory per row — it has to be a DISJOINT SET.**
A greedy bin-pack of the 110 dirs balances well:
- **4-way: max/min bin = 1.0206 (2.1% imbalance)** — MEASURED by bin-packing the real dir sizes.
- 13-way: max/min = 1.0874 (8.7%).

But `CorpusSpec.config` is a **single string** passed to one `hf_files` URL (`corpus_build.py:870`), so
**a multi-directory row is not expressible today.** ⛔ **This is a REAL code requirement the plan does
not carry**, and it is in `hf_files`, which is outside my owned surface. See "Requirements for other
streams" below.

---

# C3b part 3 — what LANDED, and the plan's 5-way figure VERIFIED end to end

## `corpus_build.split_source_rows(spec, subdirs, *, total_tokens=, label_width=)` — NEW

Assembles N registry rows from one parent spec + N disjoint subdirectories. It is the registry route
made executable and testable instead of a hand-edit nobody can regression-test. It:
- gives each row a distinct `key` **and** `source_label` (`dclm` → `dclm-01`…`dclm-NN`);
- puts one subdirectory in each row's `config`;
- divides `target_tokens` evenly **and `pool_tokens` too** — an undivided pool would leave every child
  claiming the parent's whole pool, silently defeating `CorpusSpec.__post_init__`'s per-row epoch guard;
- appends a `traps` line recording the split and that the label is permanent;
- **calls `_assert_unique_identities` on its own output** — belt and braces, since a bad `label_width`
  or a parent label already ending in a digit could still collide;
- refuses: <2 parts, repeated subdirs, a reserve (0-token) parent, and N that overflows `label_width`
  (truncating `dclm-100`→`dclm-10` would collide two labels = silent token loss).

**I did NOT mutate the shipping registry row.** See "Why no registry file edit" below.

## END-TO-END VERIFICATION against the REAL live tree (MEASURED, read-only, no AWS, no writes)

Ran `plan_document` over rows built from the actual `mlfoundations/dclm-baseline-1.0-parquet` paths
at revision `817d6752…`:

| N | via | bundles | train children | largest child | distinct configs | **largest child @32 vCPU** | `plan_id` |
|---|---|---|---|---|---|---|---|
| **5** | `global-shard_{01,03,05,07,09}` | 10 | 5 | **81.6B** | 5/5 ✅ | **9.75 h** | `321f52cb19307bbe` |
| 10 | all 10 `global-shard` dirs | 20 | 10 | 40.8B | 10/10 ✅ | 4.87 h | `43cae3088c217865` |

- Token conservation: 409,907,527,680 of 410,000,000,000 = **99.977%** (the 0.023% is `shard_plan`
  rounding down to whole 25,001,984-token shards — correct, not loss).
- Ordinals: **16,315 train shards, all unique, dense 0…16,314**, max well under the 99,999 five-digit
  limit. Each child got its own contiguous block **at plan time**. This is the property C3b exists for
  and it comes free from `allocate_ordinals`.

**✅ THE PLAN'S "5 WAYS AT 32 vCPU" IS CONFIRMED BY CONSTRUCTION.** 9.75 h vs the 9.96 h aggregate
floor — DCLM stops being the binding child at exactly N=5, which is what
`IMPLEMENTATION-PLAN.md` §8A.3 predicted. DERIVED at the MEASURED 72,615 tok/s/vCPU.

## ⛔ THE LABEL SCHEME I CHOSE — **CEO DECISION REQUIRED (trap 3)**

**Chosen: `<parent_label>-<NN>`, zero-padded to 2 digits.** So `dclm` → `dclm-01, dclm-02, dclm-03,
dclm-04, dclm-05`; FineWeb-Edu → `fineweb-edu-01`…`-16`.

Verified properties (MEASURED-IN-CODE, tested):
- matches `SAFE_SEGMENT_RE` = `^[a-z0-9]+(?:-[a-z0-9]+)*$` ✅
- round-trips: `labels_from_path("tokens/dclm-01/train-00042.u32le.bin")` → `{'source': 'dclm-01'}` ✅
- zero-padded so lexical order == numeric order (`dclm-10` does not sort before `dclm-2`)

**The cost, stated plainly:** this is **permanent and consumer-visible**. It lands in the shard path,
`labels_from_path` reads it back, Gate A recomputes and rejects a mismatch, and the path is inside
`manifest_sha256`. **It cannot be backfilled** (memory: `labels-are-unbackfillable-and-selection-is-unimplemented`).
A consumer writing `source=dclm` matches **nothing** — they must match 5 labels, and `build_mixture`'s
glob `*` **does not cross `/`** (memory: `olmo-core-consumer-constraints`), so `dclm-*` works as a
prefix within one segment but the consumer still has to know to write it.

**The alternatives the CEO should weigh:**
1. **`dclm-01`…`dclm-05` (what I implemented).** Cheapest, no code. Pollutes the source segment.
2. **Keep `source=dclm`, put the part in a `domain` segment** (`tokens/dclm/part-01/train-…`).
   Consumer-friendlier — `source=dclm` still selects everything. **But it is a LIE about what `domain`
   means**: §1.2's rule is that a domain segment exists iff the source SHIPS one upstream, and
   `_domain_of` cannot derive one from the file index anyway (briefing §2.2). Would need code.
3. **Renumber to a flat `dclm` after the build, before publish.** Not possible — ordinals are allocated
   at plan time and the path is in `manifest_sha256`.

**My recommendation: option 1, and document the N labels in the dataset README's `sources` field** so a
consumer discovers them from the artifact rather than by a failed glob. But **I am not the owner of a
schema decision and this is flagged, not settled.**

## Why I did NOT edit `artifacts/reservoir/corpus-registry.json`

**The registry I own is the RESERVOIR's, and it is not the corpus this task is about.** MEASURED:
17 rows summing to **252.6B** target tokens; `dclm-baseline` is **30B** at `HuggingFaceFW/dclm_100BT`.
The task's 410B DCLM / 252B FineWeb-Edu / 108B code are the **1.0T `pretrain/final-dataset` mix**
(`FINAL-DATASET-REPORT.md` §3), and **no registry file for that corpus exists anywhere in the repo** —
I searched (`find -name '*registry*'`: only `build_registry.py`, the reservoir JSON, and an unrelated
`profiles/registry.py`).

Editing the reservoir's 30B `dclm-baseline` row to 5×82B against a different upstream would:
- change the reservoir's mix, which is the owner's to freeze, for a corpus that is not this one;
- change **every `plan_id`** for the reservoir build (`plan_document`'s sha256 IS the id), invalidating
  its receipts;
- be a repo-repointing (repo A → repo B) that the brief explicitly forbids doing silently.

So I shipped the **mechanism**, tested against the real tree, and left the row alone. When the
final-dataset registry is authored, splitting DCLM is one call:
`split_source_rows(dclm_row, [f"{P}/global-shard_{i:02d}_of_10" for i in (1,3,5,7,9)])`.

---

# ⚠️ SELF-CORRECTION — my "FineWeb-Edu 4-way is INFEASIBLE" framing above was too strong

Earlier in this file I wrote that a 4-way FineWeb-Edu split *"cannot be expressed"* and implied
`hf_files` needs a multi-directory `config`. **That conflated two different things and I am
correcting it in place.** The accurate statement:

**A 4-way split cannot be ONE DIRECTORY PER ROW** (largest CC-MAIN dir = 24.8B tok, a 4-way row needs
63B). That part stands. **But 4 ways is not the requirement — a duration under the 9.96 h floor is.**
And a *higher* N with one dir per row clears it with no code at all:

| N | per-child | **@ 8 vCPU** | @ 32 vCPU | dirs qualifying | vCPU used @8 |
|---|---|---|---|---|---|
| 13 | 19.4B | — | — | **11 of 110** ❌ infeasible | — |
| **16** | 15.8B | **7.53 h** ✅ | 1.88 h | 31 ✅ | 128 of 384 |
| **20** | 12.6B | **6.02 h** ✅ | 1.51 h | 73 ✅ | 160 of 384 |
| 26 | 9.7B | 4.63 h ✅ | 1.16 h | 106 ✅ | 208 of 384 |

**So FineWeb-Edu is solved by the registry route at N=16–20 with 8-vCPU children, one CC-MAIN dir per
row, zero code.** At 32 vCPU those N would demand 512–832 vCPU, over the 384 cap — **so for
FineWeb-Edu the correct child size is 8 vCPU, not 32.** That is the opposite of DCLM.

**Corrected recommendation, stating child vCPU as instructed:**

| source | tokens | **recommended split** | child size | per-child | vCPU | carve |
|---|---|---|---|---|---|---|
| **DCLM** | 410B | **10 ways** | **32 vCPU** | 41.0B → **4.90 h** | 320 | one `global-shard_NN_of_10` each; all 10 qualify (269B each) |
| **FineWeb-Edu** | 252B | **16 ways** | **8 vCPU** | 15.8B → **7.53 h** | 128 | one `data/CC-MAIN-*` each, from the 31 dirs ≥15.8B |
| code (stackv2) | 108B | 2 ways | 32 vCPU | 54B → 6.45 h | 64 | UNVERIFIED — I did not walk this tree |

⚠️ **DCLM 10-way + FineWeb-Edu 16-way = 448 vCPU, which EXCEEDS the 384 cap if run concurrently.**
They do not have to be concurrent. But **if the wave is planned as one array, it must be scheduled in
two passes, or DCLM drops to 5 ways at 32 vCPU (9.80 h, 160 vCPU) giving 288 total — which fits.**
The plan's own 5+4 combination at 32 vCPU is stated as 288 vCPU and fitting; my correction is that the
4 must become 16 at 8 vCPU (128 vCPU), so **5-way DCLM @32 + 16-way FWE @8 = 288 vCPU and fits.**
That is the configuration I recommend.

**Why I still recommend keeping a multi-directory `config` on the requirements list:** the one-dir-per-row
route works here only because 110 dirs happen to exist and 31 are big enough. A greedy 4-way bin-pack of
those 110 dirs balances to **2.1%** (MEASURED), which would be strictly better — fewer, larger children.
But it needs `config` to accept a list, which is `hf_files` (`corpus_build.py:869-871`), **not my
surface**. It is an optimization, **not a blocker**. Downgrading from "REAL code requirement" to
"nice-to-have" — correcting my own overstatement.

---

# Requirements for other streams (I did NOT touch these functions)

1. **`hf_files` (`corpus_build.py:846-892`) — multi-directory `config`, OPTIONAL.** Would let one row
   name a *set* of subdirectories, enabling balanced bin-packed splits instead of one-dir-per-row.
   Not a blocker (see above). Owner: whoever owns `hf_files` — **not eng-06 (`run_bundle`) or eng-07
   (`_reader_for`)**, though it is adjacent to eng-07's function.
2. **`_reader_for` (eng-07) — no change needed from me.** It budgets by characters and breaks between
   files, so a split row simply reads fewer files. `partial_source=True` is already passed at
   `corpus_build.py:566`, so the surplus from an over-large subdirectory is expected, not an error.
   **Confirmed by reading; eng-07 should not need to do anything for C3b.**
3. **`run_bundle` (eng-06) — no change needed from me.** Split rows are ordinary bundles.

---

# FINAL STATE

**Commits on `agent/eng-04/plan-surface` (NOT pushed):**
- `5004159` fix(corpus_build): refuse duplicate source_label/key — 33.3% silent token loss
- `82f0953` feat(corpus_build): split_source_rows — N array children from N disjoint subdirs
- `fcfd1b0` docs(corpus): SHARD_TOKENS confirmed at 25,001,984 — but its stated reason was false

**Tests: 1214 baseline → 1227 passing** (+13). Every new test recomputes.

**Constraints honoured:** no S3 write, no Batch job, no `manifest.json` written anywhere, no
`git push`, no Maple config read. AWS use was one read-only
`batch describe-job-definitions`. HF use was read-only tree listing.

## Numbers I moved

| number | from | to | grade |
|---|---|---|---|
| validator timeout | 7200 s (docstring) / "UNVERIFIED rev 12" | **14,400 s, rev 14** | **MEASURED** (I re-confirmed independently of PLAT-EXEC) |
| Gate A @1.0T, 25M shards | "fits with margin" | **5.64 h serial / 4.98 h @hw16 — EXCEEDS by 41%** | DERIVED on MEASURED 507.5 ms/obj |
| Gate A break-even | not stated | **28,373 objects = 709B tokens** | DERIVED |
| duplicate-label token loss | "tokens vanish" (qualitative) | **33.3%, measured** | MEASURED-IN-CODE |
| DCLM repo B tree | "confirmed by walking" | **27,938 files / 7.420 TB / 100 disjoint dirs** | **MEASURED** |
| DCLM global-shard skew | UNVERIFIED (briefing §5.2) | **max/min = 1.0025 (0.25%)** | **MEASURED** |
| `hf_files` pagination @100-way | UNTESTED (briefing §5.4) | **works: 2,790 files, 0 dupes, 3 pages** | **MEASURED** |
| DCLM repo B pool | ~3,764B (plan §284-285) | **~2,693B** — 28% lower | DERIVED, flagged |
| DCLM 5-way @32 vCPU | 9.8 h predicted | **9.75 h, built and verified** | DERIVED on MEASURED rate |
| FineWeb-Edu full pool | 1,583.1B (report, "MEASURED") | **1,583.0B — 0.01% match** | **DERIVED, independently reproduced** |
| FineWeb-Edu carve skew | not stated | **3.1× across 110 dirs** | **MEASURED** |
| FineWeb-Edu ways | 4 @32 vCPU | **16 @8 vCPU** (4 @32 is not expressible one-dir-per-row) | DERIVED |

## DECISIONS NEEDED FROM THE CEO

1. **Label scheme `dclm-01`…`dclm-NN`** (trap 3). Permanent, in `manifest_sha256`, unbackfillable.
   Implemented and tested; needs a ruling. My recommendation: accept, and list the N labels in the
   README's `sources`.
2. **Repoint DCLM `HuggingFaceFW/dclm_100BT` → `mlfoundations/dclm-baseline-1.0-parquet`.** Forced by
   arithmetic (114.69B pool cannot serve a 378B draw), but it is a mix/licence change
   (ODC-BY-1.0 → cc-by-4.0). **Owner's call.**
3. **Same for FineWeb-Edu: `sample/100BT` → `data/`.** Same forcing (100.24B cannot serve 252B).
   **This one is not in any plan document — I found it.**
4. **#10 as a hard prerequisite** for publishing 1.0T at 25M shards, or a 5th validator revision at
   ≥ 25,000 s, or ≥3 datasets. Currently not shown as blocking in the dependency graph.
