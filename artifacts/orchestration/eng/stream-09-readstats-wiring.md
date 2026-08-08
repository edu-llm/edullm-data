# stream-09 — ReadStats.problems() wiring + FilterStats short-doc counter

**Agent:** eng-09 | **Branch:** `agent/eng-09/readstats-wiring` | **Started:** 2026-08-08
**Worktree:** `/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/eng-09--readstats-wiring`

## Status
- [x] TASK 1 — wire `problems()` into `run_bundle` (WARN, not RAISE)
- [x] TASK 2 — RESOLVED BY REFUTATION: the counter would be permanently ZERO; the real hole (`length` block had no reader in `src/`) is fixed

## Log
(appending as I go)

### 00 — file created, investigation starting

### 01 — baseline + naming correction

- `python3 -m pytest -q` at `5450f53`: **1306 passed, 14 warnings, 49.42s**. MEASURED. Matches the brief.
- ⚠️ **CORRECTION — there is no class named `ReadStats` anywhere in the repo.**
  `grep -rn "ReadStats" src/ tests/` → **0 hits** (MEASURED-IN-CODE). The class the brief means is
  `corpus_read.FilterStats` (`src/edullm_data/corpus_read.py:785-869`), which `corpus_build.run_bundle`
  imports under the local alias `LengthStats` (`corpus_build.py:763`). Two classes are named `FilterStats`
  in this codebase and that collision is exactly why the brief's "TWO stats objects" warning exists.
  I use **`corpus_read.FilterStats` (= length filter)** and **`corpus_filter.FilterStats` (= dedup pass)**
  throughout below.
- `grep -rn "problems()" src/` → **0**; `tests/` → **5** (not 4). MEASURED-IN-CODE. Brief said 4 in the
  LEDGER; the fifth is `tests/test_corpus_read.py:895`. Immaterial, recorded for accuracy.

### 02 — 🔴 F1: I reproduced the dolma3 QA shape end-to-end. TASK 1's premise HOLDS, exactly.

**MEASURED**, `run_bundle` on `FakeS3`, 4,000 docs drawn `normal(54.4, 54.4x0.212)` (= LEDGER's
`reddit_to_flashcards` row), `min_doc_tokens=64`, seed 42, whitespace tokenizer:

| quantity | value |
|---|---|
| pool below the 64-token floor | **78.7%** (target 79.6%; the 0.9pp is the gaussian draw, not a model difference) |
| `length.seen` / `length.kept` | 2,200 / **462** |
| `length.dropped_short` | **1,738** |
| realized `drop_fraction` | **0.790** — **1.97x** over `max_drop_fraction=0.4` |
| `length.mean_kept_tokens` | **70.09** |
| `MIN_MEAN_DOC_TOKENS` floor | 20 |
| **`run_bundle` verdict** | ✅ **SUCCEEDED.** 2 shards, 32,768 tokens, `unfilled=0`, receipt written. |

**The mean guard is not merely inert here — it reports the bundle as 3.5x SAFE.** 70.09 / 20 = 3.50.
The brief's "anti-correlated with the defect" claim is **CONFIRMED BY EXECUTION**, with a number.
A 79% attrition bundle publishes clean and silent today.

Control: an all-dropped source (every doc 3 tokens) DOES fail — but with
`receipt-empty-bundle: "claims completion but names no shards"`, which names the **symptom**
(no shards) and not the **cause** (wrong text column / floor). `problems()`'s first clause says the
cause verbatim and is never consulted.

### 03 — 🔴 F2: **TASK 2's PREMISE IS FALSE.** The subtraction yields **0**, not the short-doc count.

**(a) claim** — LEDGER: *"`FilterStats` tracks seen/kept/duplicates/contaminated … so the [short-doc]
loss is only inferable as `seen − kept − duplicates − contaminated`."*
(`artifacts/orchestration/LEDGER.md:1055-1058`, restated 1148-1151, and in my brief.)

**(b) evidence — MEASURED, same run as F1:**

```
filter block: {'seen': 3000, 'kept': 3000, 'duplicates': 0, 'contaminated': 0, ...}
length block: {'seen': 2200, 'kept': 462, 'dropped_short': 1738, ...}

seen - kept - duplicates - contaminated  =  3000 - 3000 - 0 - 0  =  0
TRUTH: dropped_short = 1738
```

**The subtraction is off by 1,738 — it is identically zero for EVERY input.**

**(c) why, MEASURED-IN-CODE.** `run_bundle` orders the stages
`carve → dedup_and_decontaminate → tokenize_documents(min_tokens=…)` (`corpus_build.py:727-769`).
`corpus_filter.FilterStats` is filled by `dedup_and_decontaminate`, which **runs BEFORE any length is
known** and takes no tokenizer (`corpus_filter.py:299-327`). Its closure
`seen == kept + duplicates + contaminated` is enforced per-document by a three-way branch, so the
residual is **structurally 0** — that is the identity the test asserts, not a leftover bucket.
The length filter moved INSIDE `tokenize_documents` (`corpus_pack.py:337-400`) for a measured
91%-of-compute reason, and reports into `corpus_read.FilterStats`.

**A dedicated short-doc counter on `corpus_filter.FilterStats` would be permanently zero — the exact
"decoration" the CEO named as the anti-pattern for this task.** It is neither a subdivision of an
existing bucket nor a new term in the identity; it is a counter for a stage this class never sees.
**I am not adding it.**

**(d) the counter the LEDGER asks for ALREADY EXISTS**, on the class that can compute it:
`corpus_read.FilterStats.dropped_short` (`corpus_read.py:807`) — explicit, never derived, with
`dropped_empty` split off from it so a schema bug is distinguishable from genuinely short text.
Root cause of the LEDGER error: **two classes are both named `FilterStats`.** The LEDGER cited
`corpus_filter.py:283-287` while describing `corpus_read`'s job.

### 04 — 🔴 F3: THE REAL TASK-2 GAP — the short-doc numbers are computed, returned, and **THROWN AWAY**

**MEASURED-IN-CODE.** `run_bundle` returns a `"length"` block (`corpus_build.py:857-865`). The only
production caller, `_cmd_run`, does:

```python
f = info["filter"]
print(f"DONE {bundle.bundle_id} shards=… docs={f['kept']:,}/{f['seen']:,} "
      f"dup={f['duplicates']:,} decon={f['contaminated']:,} …")
```
`corpus_build.py:1010-1013` — **`info["length"]` is never read.** `grep -n "info\[.length.\]" src/` → 0.

`FilterRecord`'s docstring justifies keeping `length` out of the receipt because *"it is returned
below and printable"* (`corpus_read`-block comment, `corpus_build.py:808-810`). **It is returned and
NOT printed.** So on the Batch path the number that would have killed the dolma3 QA row survives
**nowhere at all** — not the receipt, not stdout, not CloudWatch. That is strictly worse than the
§5.6 condition that put `FilterRecord` in the receipt in the first place.

**This is the real defect behind TASK 2, and it is a reporting hole, not a missing counter.**

### 05 — WHAT LANDED

**Tests: 1306 → 1314 (+8). MEASURED**, `python3 -m pytest -q`, 43.57 s. Also green under
`-W error::edullm_data.corpus_read.AttritionWarning` (1314) — i.e. **no test in the suite emits an
unhandled attrition warning**, so the guard is not noise.

#### TASK 1 — `problems()` wired. Verdict: **WARN, not RAISE.**

| file | change |
|---|---|
| `corpus_read.py:80-95` | new `AttritionWarning(UserWarning)` + `__all__` entry |
| `corpus_read.py` `FilterStats.accounted` | new property, `kept + dropped_short + dropped_empty` |
| `corpus_read.py` `problems()` | new FIRST clause: closure check; docstring records the anti-correlation with numbers |
| `corpus_build.py:763-764` | imports `AttritionWarning` |
| `corpus_build.py` (after `pack`, before receipt) | `attrition = length_stats.problems()` → `warnings.warn(f"{bundle.bundle_id}: {p}", AttritionWarning)` |
| `corpus_build.py` return | new `"attrition": [...]` and `length["drop_fraction"]` |
| `corpus_build.py` `_cmd_run` | prints the `length` block + one `ATTRITION <bundle>:` line per problem |

**RAISE-vs-WARN JUSTIFICATION (the decision I was asked to make deliberately):**

1. **By the time these counters are final the shards are ALREADY IN S3.** The upload happens inside
   `pack`'s sink (`corpus_build.py:701-721`), and `length_stats` is only complete once `pack` has
   drained the generator. Raising here pays the full billable read + tokenize + upload and *then*
   refuses — **precisely the `_drain_surplus` shape that killed 25 of 27 bundles**, and precisely the
   `unused > 0` check `_check_keep_accounting` had to delete for the same reason. Twice is a pattern.
2. **A raise would ALSO orphan the uploaded shards** — written but un-receipted, so `bundle_is_done`
   reports the bundle unbuilt and the next run rewrites the same keys. Strictly worse than warning.
3. **>40% is not necessarily wrong** — `problems()` says so in its own message (§3.3 expects
   3.4-12.6% on FinePhrase). The condition means "a human must re-check the pool arithmetic", not
   "these bytes are bad". Making it fatal would fail legitimate bundles.
4. **Escalation needs no code change:** `AttritionWarning` is its own category, so an operator runs
   `python3 -W error::edullm_data.corpus_read.AttritionWarning -m edullm_data.corpus_build run …`.
   That is *why* it is not a bare `RuntimeWarning` — `corpus_pack` already emits one of those on
   this path (TOKENIZERS_PARALLELISM), so a shared category would be unusable as a filter and a
   test asserting on it would pass for the wrong reason.
5. **stdout as well as `warnings`**, because `warnings` writes to **stderr** and a 27-child Batch
   array job's stderr interleaves. `ATTRITION <bundle_id>: …` is greppable and attributed.

⚠️ **Stated so "we warn" is not misread as "we are covered": the place this SHOULD gate is the
pre-flight, not here.** DATA's domain-purity clearance is a prediction from per-source sampled means;
this is the only *runtime* check that reality matches it, but it is a post-hoc one. A cheap version
fails on a sample **before** the read. That is a plan-stage change and is **not in this commit.**

**Which stats object:** `corpus_read.FilterStats` (the `LengthStats` alias, `corpus_build.py:765`).
Never merged with `corpus_filter.FilterStats`. Cross-check asserted in the pre-existing fixture test.

#### TASK 2 — **counter NOT added; the premise was false (F3). The real hole is fixed instead.**

Adding `dropped_short` to `corpus_filter.FilterStats` would ship a permanently-zero field (F3).
What landed instead:
- `_cmd_run` now **prints** the length block — it had **no reader in `src/`** despite `FilterRecord`'s
  docstring justifying its exclusion from the receipt on the grounds that it *"is returned below and
  printable."*
- `drop_fraction` added to the returned `length` block. The one place a ratio is safe: it ships
  **next to both its terms**, so the `category_attrition` failure mode (a denominator a reader must
  guess) cannot occur.

**Schema implication: NONE, deliberately.** No receipt change, `RECEIPT_SCHEMA_VERSION` stays
`edullm-corpus-receipt/v2`, `FilterRecord` untouched, all Wave-0 cross-block relations
(`hits + repeats + misses == filter.seen` etc.) unaffected — I added no term to either identity.
`FilterRecord`'s reason for excluding `length` is **sound as long as `_cmd_run` prints it**, which is
now true and now tested. **Putting it in the receipt remains open — see DECISIONS.**

### 06 — MUTATION RESULTS: 6 mutants, 6 caught (one only after I strengthened a weak test)

| # | mutation | result |
|---|---|---|
| 1 | `attrition = []` (un-wire `problems()`, exact pre-change behaviour) | 🔴 **2 failed** |
| 2 | delete the `drop_fraction` clause, keep the mean clause ("redundant") | 🔴 **4 failed** |
| 3 | threshold drift `0.4 → 0.5` | 🔴 **3 failed** |
| 4 | `_cmd_run` stops reading `info["length"]` | 🟢 **SURVIVED** → fixed → 🔴 **1 failed** |
| 5 | delete the closure check from `problems()` | 🔴 **1 failed** |
| 6 | `_cmd_run` stops printing `ATTRITION` lines | 🔴 **1 failed** |

🔴 **I shipped the anti-pattern I was warned about, and the mutation test caught it.** My first
`_cmd_run` test used `inspect.getsource` + a re-implementation of the format strings — it asserted
the *text of the function* rather than its *behaviour*, so mutant 4 passed cleanly. Replaced with a
test that drives the **real `_cmd_run`** via `monkeypatch` on its I/O seams and reads `capsys`.
**Recording this because it is the CEO's exact "a test that only checks the method exists" failure,
committed by the agent assigned to remove it — and only a mutation run found it.**

### 07 — TRUE POSITIVE on a pre-existing fixture (not a false alarm)

`test_the_length_filter_still_drops_short_documents_and_reports_them` (written before this change,
for another purpose) now warns: **`dropped 199/308 documents (64.6%)`**. MEASURED, and **correct** —
that fixture is 200 three-token docs against 400 long ones with an early packer stop. Wrapped in
`pytest.warns` and given a cross-check that the message's counts equal the `length` block's, rather
than left as tolerated log noise. It doubles as evidence the wiring reads the right `FilterStats`.

### 08 — DECISIONS NEEDED FROM ENG-EXEC

1. **Should attrition go in the RECEIPT?** I did not put it there. Argument for: `_cmd_run`'s stdout
   is CloudWatch, which is exactly the §5.6 condition that motivated `FilterRecord`. Argument
   against: it is a **fourth denominator** in an artifact that documents three, and Wave 0 spent real
   effort keeping them apart. **Needs a schema v3 call if yes — I did not bump silently.**
2. **The pre-flight gate is unbuilt.** Warning post-hoc still pays the bundle. A sampled pre-read
   check is the cheap version and is plan-stage work nobody owns.
3. **LEDGER correction to relay:** the `seen − kept − duplicates − contaminated` claim
   (`LEDGER.md:1055-1058`, 1148-1151) is **false, and identically 0**. Root cause: two classes named
   `FilterStats`. Worth fixing in the LEDGER so it is not re-assigned.
4. **`max_drop_fraction=0.4` is a DEFAULT, applied uniformly.** §3.3 expects 3.4-12.6% for
   FinePhrase, so 0.4 is loose for it and possibly tight elsewhere. A per-source threshold is a
   registry field. **UNVERIFIED that 0.4 is right for any specific source** — nobody has measured it.

### 09 — NOTE FOR eng-10 (concurrent editor)

I touched `corpus_build.py` (`run_bundle` body + return, `_cmd_run`, two imports) and `corpus_read.py`
(module `__all__`, new `AttritionWarning` class above `ReadError`, `FilterStats` only). **I did not
touch `_reader_for`, `_assert_readable`, `READABLE_FORMATS`, `_READERS`, or `read_documents`.**
Both my `corpus_read.py` edits are ABOVE/BELOW eng-10's territory; the `__all__` list is the one
shared line — I added `"AttritionWarning"` to it.

### 10 — DONE

- Committed `fb29a02` on `agent/eng-09/readstats-wiring`. **Not pushed.** Working tree clean.
- `python3 -m pytest -q` → **1314 passed**, 42.03 s. MEASURED. (baseline 1306, +8)
- No S3 writes, no Batch submissions, no `manifest.json`, no `zstandard`, no Maple citations.
  Diff grepped for `put_object`/`submit_job`/`manifest.json`/`--promote`/12-digit account ids → 0.
- Status: TASK 1 ✅ done. TASK 2 ✅ resolved — **by refutation + the real fix**, not by adding the
  counter as specified (see F3 / §05).
