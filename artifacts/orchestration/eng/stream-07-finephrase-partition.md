# eng-07 — C1 / task #21: wire the FinePhrase id partition into `_reader_for`

**Status:** STARTED 2026-08-08. Worktree `/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/eng-07--finephrase-partition`, branch `agent/eng-07/finephrase-partition`.

**Territory:** `_reader_for` in `src/edullm_data/corpus_build.py` (nominally :884-931) ONLY.

## Log

- [init] Output file created before any other work, per obligation.

---

## 1. Baseline confirmed

- `python3 -m pytest -q` on `agent/eng-07/finephrase-partition` @ `f5a4017`: **1214 passed, 14 warnings in 24.40s**. MEASURED. Matches the brief's floor exactly; CLAUDE.md's 786 is a different branch, confirmed not the baseline.
- Worktree clean at `f5a4017` before my edits.

## 2. The gap, confirmed MEASURED-IN-CODE

`grep -rn "keeps_id\|partition_of\|format_for_id\|IdSet" src tests scripts`:

| symbol | production callers | grade |
|---|---|---|
| `reservoir_ids.keeps_id` | **1**, and it is a REPORTING function — `ingest_reservoir._partition_report:743`, which only builds a JSON audit dict | MEASURED-IN-CODE |
| `reservoir_ids.partition_of` | **0** outside `reservoir_ids` itself (`format_for_id` → `partition_of`), plus tests + `scripts/measure_finephrase_overlap.py` (which deliberately RE-implements it, not imports it) | MEASURED-IN-CODE |
| `ingest_reservoir.IdSet.contains` | **0 anywhere in the repo** | MEASURED-IN-CODE |
| `corpus_build._reader_for` | filters NOTHING on id; `corpus_build.py:926-931` yields every document the reader returns | MEASURED-IN-CODE |

Confirms the plan's §0 blocker 2 exactly. The blocker is real and it is in my function.

## 3. ✅ THE COLUMN TRAP IS ALREADY CLOSED — measured, not assumed

The brief asked me to verify this rather than assume it. **It comes out GREEN, and by a mechanism stronger than the trap needs.**

- `artifacts/reservoir/corpus-registry.json` — all four FinePhrase rows carry `"text_column": "rollout_results.list.element.text"`, the exact `path_in_schema`. MEASURED-IN-CODE (read the JSON).
- `corpus_read.read_parquet_documents:479` calls `_resolve_leaf(md, spec.text_column, what="text_column")` **before reading a single row**, and `_resolve_leaf` (`corpus_read.py:106-119`) delegates to `ingest_reservoir._leaf_index`, which is exact-match-or-raise. There is **no** `.names.index("text")` anywhere on the read path. MEASURED-IN-CODE.
- `_compile_walk` (`corpus_read.py:122-211`) then re-checks the leaf against the arrow schema and raises `ReadError` when a path "does not descend" — which is precisely the `rollout_results.text` zero-column near-miss.
- Existing tests already pin all three near-miss spellings plus a typo: `tests/test_corpus_read.py:173-176`, and `:199` asserts `read_row_group(columns=["rollout_results.text"])` returns a ZERO-column table with no error.

**Verdict: the §4.2 trap needs no work from me.** I add one test that recomputes it from the *registry* rather than from a fixture (see §6), because what the tests pin today is the reader's behaviour given a spec, not that the shipped registry rows actually carry the safe value.

## 4. ⚠️ AMBIGUITY IN "FinePhrase, one partition" — the pool number decides it, and I need ENG-EXEC to confirm

`FINAL-DATASET-REPORT.md:88` reads `| FinePhrase, one partition (synthetic) | 4% | 36.0B | 123.3B | 0.29 | MEASURED |`.

"One partition" admits two readings, and they are **not** the same corpus:

| reading | what is drawn | available pool | epochs at 36.0B |
|---|---|---|---|
| **(A) one CONFIG's quarter** | e.g. `faq` only, keeping `partition_of(id)==0` | 148.54B × 0.2486 = **36.9B** DERIVED | **0.98** — effectively a full epoch, ~0 headroom |
| **(B) all four configs, each keeping its OWN quarter** | four disjoint quarters, union = the whole parent doc set once | 478.15B × 0.25 = **119.5B** DERIVED | **0.30** |

**The report's own 123.3B and 0.29 match reading (B), not (A)** — 119.5B DERIVED vs 123.3B stated is a 3.1% gap, while reading (A) gives 36.9B, off by 3.3×. Registry `pool_tokens` used: faq 148.54B, math 94.74B, table 86.95B, tutorial 147.92B (MEASURED, `artifacts/reservoir/corpus-registry.json`).

- **Grade on 123.3B: UNVERIFIED provenance.** I could not find it in `artifacts/` (`grep -rn "123.3"` → no hit). The nearest recomputation from registry pools is **119.5B DERIVED**. Not a blocker for my code; flagged so nobody treats 123.3B as recomputable.
- **Reading (A) would be dangerous**: at 0.98 epochs the `_FILTER_HEADROOM` 1.5 over-read has nowhere to go, and MIN_DOC_TOKENS attrition (measured 3.4–12.6% for FinePhrase's short rewrites) would leave the bundle short.

**✅ My implementation is deliberately reading-agnostic and correct under BOTH.** It applies `keeps_id(spec.config, doc.id)` per FinePhrase config row, so:
- if only one row is drawn → that row keeps its quarter, no sibling to overlap with;
- if all four are drawn → four disjoint quarters, no document appears twice.
Which rows are drawn stays a `target_tokens` question in the registry, i.e. **eng-04's file and a mix decision, not mine.** I do not touch it.

## 5. §4.3 — the FineWeb-Edu anti-join. REPORTED, NOT IMPLEMENTED (per brief)

`sample-100BT ⊂ sample-350BT`, and `sample-350BT` is FinePhrase's exact parent, so **100%** of a FineWeb-Edu draw has a synthetic sibling. The registry says so itself, in the `fineweb-edu` row's own trap text (MEASURED-IN-CODE, `corpus-registry.json`).

- Free fix per §4.3: draw synthetic from the ~242M `sample-350BT` ids **not** in `sample-100BT`.
- The primitive exists and is unused: `ingest_reservoir.IdSet` with **zero `contains` callers**.
- **This is a CEO-level mix question and I did not touch it.** The id partition (my task) makes the four *synthetic* configs disjoint from each other; it does **nothing** about synthetic-vs-edu-web overlap. Two different defects, one of them fixed by me, the other still open.

---

## 6. ✅ WHAT LANDED

Commit `6830af4` on `agent/eng-07/finephrase-partition`. **Not pushed.** `src/edullm_data/corpus_build.py` +64 lines (3 hunks, all inside my territory — the import block, and `_finephrase_format` / `_reader_for` adjacent to `_CHARS_PER_TOKEN`); `tests/test_corpus_build.py` +259.

**`git diff --stat` touched two files and nothing else.** No edit to `load_registry`, `plan_document`, `corpus.py`, `run_bundle`, its `sink`, `corpus_filter.py`, or the registry JSON — eng-04's, eng-05's and eng-06's territories are untouched.

### 6.1 The code, both halves

**(1) `_finephrase_format(spec) -> str | None`** — new, adjacent to `_reader_for`.

- Keyed on **`spec.repo == FINEPHRASE_REPO`**, deliberately **not** `source_label` or `key`. `source_label` is a *naming* decision (§1.1 fuses realness into it — today `synthetic-finephrase-faq`), so it is exactly the field a later mix edit rewrites; `repo` is upstream identity. A partition that silently stops applying because someone renamed a label is a 4× over-exposure no check downstream can see. Pinned by a test.
- **Raises `BuildDriverError` on a FinePhrase row whose `config` is not one of the four.** Preserves `keeps_id`'s deliberate refusal and moves it **earlier** — before the first HTTP request, so a bad row fails locally rather than 6,800 files into a billable job. Both failure directions are covered: a typo'd config would either drop 100% of rows and report a successful ingest of an empty source, **or** — if I had returned `None` for "unrecognised" — skip the partition entirely and restore the 4× exposure. Both look green.

**(2) `_reader_for`** — the drop plus the budget division.

```python
fp_format = _finephrase_format(spec)          # resolved BEFORE any listing
keep_rate = bundle.keep_rate
if fp_format is not None:
    keep_rate /= N_PARTITIONS                 # the budget correction
budget = int(bundle.tokens * _CHARS_PER_TOKEN * _FILTER_HEADROOM / keep_rate)
...
        seen_chars += len(doc.text)           # charged BEFORE the drop
        if fp_format is not None and not keeps_id(fp_format, doc.id):
            continue
        yield doc
```

**The `seen_chars` ordering is load-bearing and easy to get backwards.** The budget is denominated in characters **READ**, and `/ N_PARTITIONS` is what converts read into kept. Charging only survivors would apply the correction twice and read **16×**. Stated in a code comment so the next editor does not "tidy" it.

`N_PARTITIONS` rather than the measured 24.86–25.26%: this is a **ceiling** `pack` stops short of as soon as the planned shards fill (`corpus_pack.py:727-741`), so the exact-quarter idealisation errs by ≤0.6% and errs **into** `_FILTER_HEADROOM`'s slack.

### 6.2 The three "do not"s — all honoured

| instruction | status |
|---|---|
| Do NOT touch `_CHARS_PER_TOKEN` (6.0) | ✅ untouched — `git diff` shows no change at `:865` |
| Do NOT change break-between-FILES semantics | ✅ the `if seen_chars >= budget: return` remains at the **file** loop level; `continue` skips a document without touching the file boundary |
| Do NOT start C3 (val file-sharding) or C11 (`bytes_fetched`) | ✅ neither started |

**✅ §3.1's `val_fraction` cancellation SURVIVES the change** — I checked this specifically, because a divisor added next to `keep_rate` is exactly where that algebra would break. VERIFIED-IN-CODE on the real `finephrase-faq` row: train budget 539.1e9 chars, val budget 360.0e9 chars, against `want × 9.0 × 4 = 540.0e9`. Train matches to 0.2% (integer shard rounding: 14.901B not 15.0B). Val is lower **for the pre-existing reason §3.1 documents** — `plan_document` floors the val target to whole shards — not because of my divisor. **The `/ N_PARTITIONS` factor is common to both splits and cancels the same way `val_fraction` does.**

### 6.3 Tests — 7 new, and every one recomputes

| test | what it RECOMPUTES |
|---|---|
| `..._keeps_exactly_the_ids_this_finephrase_config_owns` | Runs the real `_reader_for` four times over one known 4,000-id set and asserts survivors **are exactly** `{i for i in ids if FINEPHRASE_FORMATS[partition_of(i)] == fmt}`, computed on the spot. Then asserts the four results are **disjoint AND their union is all 4,000** — the actual property the blocker is about. **Deliberately not a spy on `keeps_id`**: a call-recording mock passes even if the reader discards the result, which is precisely this defect's shape. |
| `..._realised_partition_shares_are_balanced_on_a_sample` | Shares recomputed from **reader output** over 12,000 ids; asserts each >17.3% (the `table` design floor) and within 2pp of 25.0%. |
| `..._reads_four_times_the_text_to_deliver_its_tokens` | The budget correction, asserted through **delivered characters** (what reaches `pack`) and **files read**, not through the budget expression — so it survives a constant change. |
| `..._non_finephrase_source_is_not_partitioned` | Scoping. Mis-applying the partition discards 75% of a legitimate pool — same magnitude of error, opposite direction. |
| `..._unnameable_config_is_refused_not_skipped` | Refusal on `"tables"`, `"FAQ"`, `None`, `""`, **with `hf_files` patched to raise**, proving the refusal precedes any listing. |
| `..._keyed_on_the_upstream_repo_not_the_label` | Partition still resolves when `source_label` and `key` are renamed. |
| `..._shipped_registry_carries_the_nested_rewrite_leaf...` | §4.2, recomputed against the **committed registry** rather than a fixture: all 4 rows carry `rollout_results.list.element.text` and a nameable config. |

### 6.4 ⚠️ I VERIFIED THE TESTS FAIL WITHOUT THE FIX — both halves independently

Asserting a new test passes proves nothing about whether it can fail. Two deliberate regressions, run and then reverted:

| regression | result | grade |
|---|---|---|
| **A — remove the `keeps_id` drop**, keep the budget division | **3 failed** (both partition tests + the budget test), 37 passed | MEASURED |
| **B — remove the budget division**, keep the drop | **1 failed**: `FinePhrase delivered 36,500 chars vs 150,000 for the same planned tokens (ratio 0.243)` | MEASURED |

**Regression B is the brief's exact predicted failure, reproduced numerically: 0.243 ≈ 1/4.** This is why the two ship together — with only the partition, every FinePhrase bundle completes and then fails `verify` on unfilled refs, after its full billable work.

### 6.5 Test count

| | count | grade |
|---|---|---|
| baseline, `f5a4017` | **1214 passed**, 24.40 s | MEASURED |
| after, `6830af4` | **1221 passed**, 62.26 s | MEASURED |

Meets the ≥1214 bar. Import order checked both ways — `reservoir_ids` imports nothing from `corpus_build`, so no cycle.

---

## 7. ⚠️ A NEW DEFECT I FOUND WHILE WIRING THIS — the pool guard is now fail-open by 4×

**Not in my brief, not fixed by me, and it is a direct consequence of the partition landing.**

- **(a) Plan claim.** `CorpusSpec.__post_init__` (`corpus.py:261-266`) refuses a row where `pool_tokens < target_tokens`: *"drawing more than a pool holds means repeating documents, which the epoch guard exists to flag."*
- **(b) Evidence.** MEASURED-IN-CODE: it compares against the **undivided** `pool_tokens`. After my change a FinePhrase row can only reach **1/4** of its declared pool. The registry's `pool_tokens` are the full per-config figures (faq 148.54B etc.), and I did **not** change them — that file is eng-04's.
- **(c) Numbers.** The band where the guard passes but the reader cannot deliver, per row (DERIVED from registry `pool_tokens` ÷ 4):

| row | declared pool | reachable post-partition | guard trips at | **silent band** |
|---|---|---|---|---|
| `finephrase-faq` | 148.54B | **37.13B** | >148.54B | **111.41B** |
| `finephrase-math` | 94.74B | **23.68B** | >94.74B | **71.06B** |
| `finephrase-table` | 86.95B | **21.74B** | >86.95B | **65.21B** |
| `finephrase-tutorial` | 147.92B | **36.98B** | >147.92B | **110.94B** |

- **(d) Blast radius.** **Zero at today's targets** — every row is at 15.0B, comfortably under its 21.74–37.13B reachable quarter, so nothing is broken right now. It matters only if someone raises a FinePhrase target, which the 36.0B mix line makes plausible: **36.0B from `table` or `math` alone exceeds the reachable quarter** (1.66 and 1.52 epochs) while `CorpusSpec` accepts it silently and `epochs_for` reports green off the declared pool.

**Recommended fix — eng-04's call, since it is `load_registry` + the registry JSON:** divide FinePhrase `pool_tokens` by 4 in the registry (making the declared pool the *reachable* pool), **or** teach the guard about the partition. The first is one line per row and keeps `epochs_for` honest for free. **I did not do it — wrong territory, and it changes the plan.** Requirement written here per the brief.

---

## 8. ⚠️ THE MIX AMBIGUITY, NOW QUANTIFIED — one reading is INFEASIBLE

Following up §4 with numbers, because it turns out this is decidable rather than stylistic. All DERIVED from `artifacts/recount/synthetic.json` measured per-config tokens.

**Reading (A) — all 36.0B from ONE config's quarter:**

| config | its quarter | epochs at 36.0B | verdict |
|---|---|---|---|
| faq | 37.14B | **0.97** | fits with **+3.2%** margin — effectively none |
| tutorial | 36.98B | **0.97** | fits with **+2.7%** margin |
| math | 23.68B | **1.52** | ❌ **IMPOSSIBLE** — 34.2% short |
| table | 21.74B | **1.66** | ❌ **IMPOSSIBLE** — 39.6% short |

**Reading (B) — 9.0B from each of four disjoint quarters:** every config lands at 0.24–0.41 epochs; union pool **119.5B** DERIVED, against the report's stated **123.3B** and **0.29** epochs.

**So reading (B) is what the report's own numbers describe** (119.5B vs 123.3B is 3.1%; reading A is off by 3.3×), **and two of the four configs cannot satisfy reading (A) at all.** Even the two that can have ~3% headroom against a `_FILTER_HEADROOM` sized for 50% and a measured FinePhrase filter attrition of 3.4–12.6% — i.e. they would fail.

**⚠️ DECISION NEEDED FROM ENG-EXEC / the owner: does "FinePhrase, one partition" at 36.0B mean all four configs at 9.0B each (B), or one config at 36.0B (A)?** My code is correct under both and I changed no target. But if anyone intends (A) with `math` or `table`, **the plan is infeasible and freezing it would bake in a bundle that cannot fill its shards.**

Correcting §4 of this file in place: I originally graded 123.3B's provenance UNVERIFIED and that stands, but I under-stated the consequence — I called reading (A) merely "dangerous", and for two of four configs it is **impossible**, not risky.

---

## 9. Cost of the fix, stated honestly

| quantity | before | after | grade |
|---|---|---|---|
| **FinePhrase tokens packed** | 36.0B | **36.0B — unchanged** | DERIVED |
| **tokenize / decon CPU** | — | **unchanged** — dropped docs never reach `encode_batch` or the 13-gram filter | MEASURED-IN-CODE (`run_bundle:503-509`, filter is downstream of the reader) |
| **parquet bytes read** | ~0.167 TB | **~0.66–0.67 TB (+0.50 TB)** | DERIVED from measured per-config tok/byte |
| **critical path** | — | **no change** | see below |

**The partition costs read volume, not wall-clock on the critical path.** FinePhrase is **not** on it — `IMPLEMENTATION-PLAN.md:1641` puts it at 1 bundle that "fits" (0.95 h at 32 vCPU, isolated rate), against DCLM's 410B. Quadrupling its *read* leaves the CPU-bound critical path (DCLM/FineWeb-Edu) untouched.

**One honest gap:** dropped documents are still materialized by `to_pylist()` in the parquet reader before `_reader_for` sees them, so there is some extra decode CPU inside the FinePhrase bundle. **Magnitude UNVERIFIED** — I did not measure it. It is bounded by that bundle's slack and does not touch the critical path.

---

## 10. Requirements for other streams

| for | requirement |
|---|---|
| **eng-04** (`load_registry`, registry JSON) | **§7** — FinePhrase `pool_tokens` are 4× the reachable pool post-partition. Divide by 4 in the registry, or teach the guard. Harmless at today's 15.0B targets; fail-open above ~21.74B for `table`. |
| **ENG-EXEC / owner** | **§8** — resolve reading (A) vs (B) of "FinePhrase, one partition" **before FREEZE**. Two of four configs cannot support (A). |
| **ENG-EXEC / owner** | **§5** — the §4.3 FineWeb-Edu anti-join is still unimplemented (`IdSet.contains`: zero callers). 100% of an edu-web draw has a synthetic sibling. Separate defect from mine; a mix question, not a code one. |
| **eng-06** (`run_bundle`) | No conflict. I did not touch `run_bundle` or its `sink`. My change is upstream of it and its `documents=` injection point is unchanged, so `run_bundle`'s tests are unaffected (all 40 in `test_corpus_build.py` pass). |
| **merge order** | Graph §8 says stream 4 → **stream 7** → stream 6. My diff is 3 hunks in `corpus_build.py`, none overlapping eng-06's `:429-570`. |

---

## 11. Status: DONE

- ✅ Partition wired, budget divided, shipped together.
- ✅ **1221 passing** (baseline 1214). New tests recompute; verified to fail without each half of the fix.
- ✅ Committed `6830af4` on `agent/eng-07/finephrase-partition`. **Not pushed.**
- ✅ No S3 writes, no Batch submissions, no `manifest.json`, no bulk HF fetches, no Maple.
- ⚠️ Two decisions escalated (§7 pool guard, §8 mix reading); one pre-existing gap re-reported (§5 anti-join).

