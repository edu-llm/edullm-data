# Stream 12 — file-slice sharding into `_reader_for`

**Agent:** eng-12 · **Branch:** `agent/eng-12/reader-file-slice` (base `d593db1`) · **2026-08-08**
**Worktree:** `/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/eng-12--reader-file-slice`
**Status:** IMPLEMENTED. Tests **1338 → 1355** (+17), no regressions. Both mutations caught.

---

## 1. What landed

All in `src/edullm_data/corpus_build.py` + `tests/test_corpus_build.py`. **No changes to
`ingest_reservoir.py`** — `_shard_slice` needed no generalising, it was already generic over
`items: list`. I import it rather than reimplement, so its own union tests keep protecting it.

| # | change | where |
|---|---|---|
| 1 | `Bundle.file_shard` / `Bundle.file_shards` fields, default `(0, 1)` = whole source | `corpus_build.py` `Bundle` |
| 2 | `Bundle.__post_init__` refuses out-of-range / `file_shards < 1` | same |
| 3 | `Bundle.from_plan_entry` reads both via `.get` w/ defaults — old plans still read | same |
| 4 | **`_bundle_files(spec, bundle)`** — the one place deciding which files a child touches | new, before `_reader_for` |
| 5 | `_reader_for` iterates `_bundle_files(spec, bundle)` instead of `hf_files(spec)` | `_reader_for` |
| 6 | **`_assert_file_shard_family`** — whole-plan sibling check, called from `bundles_of` | new |

**The `_reader_for` change is one line.** Everything else is the guard rails that make the one line
safe, which is the correct ratio for this defect class.

### Three Wave-0 behaviours: all preserved, all still tested
- **FinePhrase id partition** — untouched; `_finephrase_format` still keys on `spec.repo`. New test
  `test_file_sharding_composes_with_the_finephrase_id_partition` proves the two partitions compose
  (union of K children == exactly the `faq` partition of the id set, recomputed via `format_for_id`).
- **Budget / keep-rate division** — untouched (see §2).
- **`_CHARS_PER_TOKEN = 6.0`** — NOT touched. It is a withdrawn change (§3.1).
- **Break between FILES, never mid-file** — untouched; the loop still breaks at the file boundary.

---

## 2. THE BUDGET DECISION — do NOT divide by K

**Decision: the budget is NOT divided by `file_shards`.** The existing formula is already correct.

**Justification.** `budget = bundle.tokens × _CHARS_PER_TOKEN × _FILTER_HEADROOM / keep_rate`
converts *tokens this bundle must deliver* into *characters to read*. It is not a fraction of a
pool. So "divide by K?" reduces to **"does `bundle.tokens` describe the whole stream or this
child's share?"** — and the plan answers it: `allocate_ordinals` runs once at plan time and gives
each of the K siblings its **own disjoint refs** (this is exactly why §8A.5a calls it a plan-shape
change, not a reader tweak). `Bundle.tokens` sums *this* bundle's refs, so **it is already 1/K**.

Dividing again would read 1/K of what the child needs → unfilled refs → `verify` fails **at the end
of the run, after full billable work**. That is the same end-of-run failure shape that
`partial_source=True` and the FinePhrase `/ N_PARTITIONS` factor were each added to fix.

**Feasibility survives the split exactly.** The child gets 1/K of the pool and owes 1/K of the
tokens, so its **target/pool ratio is unchanged** — a bundle fillable before the split is fillable
after. This argument depends on the slice holding ~1/K of the *bytes*, which the stride delivers to
within a MEASURED 2.6% (§4); `_FILTER_HEADROOM`'s 1.5× absorbs that. **It would NOT hold for a
contiguous split of a size-clustered source** — a second, independent reason the stride is not
optional.

**The decision is guarded, not trusted.** `_assert_file_shard_family` recomputes, over the whole
plan, that siblings cover `0..K-1` exactly once and hold **disjoint** shard refs. If a plan instead
gave all K children the full ref list, each would over-read K× *and* write the same ordinals K
times, and **nothing downstream would see it**: token counts add up, ordinals stay dense, shards
decode, and in S3 the last writer wins.

⚠️ **Caveat, per ENG-EXEC.** These are byte-scaled predictions at a uniform tok/s rate, and PDF and
code are two of the four sources — neither tokenizes like web text. **My budget reasoning does NOT
depend on per-child token counts being exact.** It depends only on refs being split by the plan and
bytes striding to ~1/K. A ±30% per-child tok/byte error changes wall clock, not correctness,
because the budget is a CEILING `pack` stops short of (`corpus_pack.py:727-741`).

---

## 3. MUTATION RESULTS — both bite

**Mutation 1 (the one the brief named): `items[shard::of]` → `items[shard:of]` in
`ingest_reservoir.py:764`.**
→ **14 tests FAIL** (1341 passed). **10 are mine:** all 7 sharded `union_of_files` params, the
striding test, the budget test, and the FinePhrase-composition test. The other 4 are
`_shard_slice`'s pre-existing tests. Correctly, the `K=1` and `(7,1)` params do **not** fail —
`_bundle_files` short-circuits before `_shard_slice`, which is the intended default path.

**Mutation 2 (the budget decision, which the brief told me to justify): add `/ bundle.file_shards`
to the budget.**
→ **exactly 1 test FAILS** — `test_the_read_budget_is_not_divided_by_k_because_the_refs_already_are`.
Nothing else in 1,355 tests notices. **That is the finding**: the wrong budget is invisible to the
entire rest of the suite, and in production it would surface only at end-of-run `verify` after the
full spend. Both mutations reverted; suite back to 1355 green.

---

## 4. FILE-SIZE UNIFORMITY — independently reproduced, MEASURED

ENG-EXEC sent measurements mid-task; I re-ran them from the pinned revisions in
`artifacts/final-dataset/corpus-registry.json` via `hf_files()` (HF tree API, **metadata only, no
payload bytes**). **My numbers match theirs exactly.**

| source | files | CV | max/min | total | stride imbalance @carve K | contiguous |
|---|---|---|---|---|---|---|
| `stackv2-edu` | **95** | **0.211** | 2.09× | 83.0 GB | K=7 → **1.0259×** | 1.1413× |
| `finepdfs-edu` | 100 | 0.034 | 1.16× | 298.7 GB | K=4 → **1.0010×** | 1.0373× |
| `nemotron-cc-math-3` | 57 | 0.096 | **3.61×** | 107.4 GB | K=3 → **1.0132×** | 1.0134× |
| `nemotron-cc-math-4plus` | 46 | 0.114 | 3.10× | 62.2 GB | K=2 → **1.0101×** | 1.0103× |

All MEASURED 2026-08-08. **95, not 97** — the 97 counts `.gitattributes` and `README.md`;
`hf_files` filters by `_PAYLOAD_EXT` and returns 95, so the code already excludes them.

**Verdict: non-uniform enough to matter for the METHOD, not enough to change the carve.** Worst
stride imbalance across all four is **2.6%** (`stackv2-edu`). **Keep the stride** — it is now
empirically justified on our real data, not merely documented. On `stackv2-edu` contiguous blocks
cost 11.4% worst-child imbalance vs 2.6%, i.e. **8.35 h vs 7.57 h**.

Note `nemotron-cc-math-3` (CV 0.096) has the **largest max/min at 3.61×** — CV and max/min disagree
about which source is "least uniform" because one outlier file drives max/min while CV is dominated
by the bulk. The stride is robust to both.

---

## 5. ⚠️ BLOCKER I FOUND, WHICH I DO NOT OWN — `verify` REJECTS A CORRECT SHARDED BUILD

**Claim.** `corpus_receipt.verify_bundle_set` fires **`bundle-set-duplicate-stream`** on a correctly
file-sharded bundle, so `verify` fails the whole run at the end.

**(a) File/line.** `corpus_receipt.py:1401-1411` groups receipts by `r.stream` =
`(source, domain, split)` and raises when `len(group) > 1`. `corpus_build.py:1264` (`_cmd_verify`)
passes `[b.stream for b in bundles]`. **K file-shard siblings share one `(source, domain, split)` by
construction** — that is the entire point of file-sharding, as against `split_source_rows` which
gives each child a distinct `source_label`.

**(b) Evidence — MEASURED, reproduced free with no `s3=`, exactly as the brief specified:**

```
3 correct sibling receipts, DISJOINT shards, one stream ("stackv2-edu", None, "train")
→ verify_bundle_set(...) returns:
  code:    bundle-set-duplicate-stream
  message: 3 receipts claim stream ('stackv2-edu', None, 'train')
           (bundle ids ['stackv2-edu--train--f0', ...--f1', ...--f2'])
```

**(c) Numbers moved.** None of mine. But the **whole 51.38 h → ~11.19 h win is unrealisable** until
this is resolved: every file-sharded build fails `verify` at end of run. `verify` is a **gate, not a
report** (`corpus_build.py` module docstring) and exits non-zero.

**(d) Blast radius.** `corpus_receipt.py` — **not my module and not eng-11's.** I did **not** edit
it: it is very likely another stream's territory and a naive fix would weaken a real check (the
retry-that-did-not-replace case, which is exactly what this violation exists to catch).

**Options, for ENG-EXEC to assign — I recommend the third:**
1. Group by `bundle_id` instead of `stream` — **weakens** the genuine duplicate-retry check. No.
2. Have `_cmd_verify` pass streams with multiplicity — `expected_streams` is consumed as a `set`
   (`expected_set = set(expected)`), so this does not reach the duplicate check at all. Does not work.
3. **Make the check file-shard-aware**: allow `len(group) > 1` iff the receipts carry distinct
   `file_shard` values covering `0..K-1` with disjoint shard paths — i.e. lift
   `_assert_file_shard_family`'s logic to the receipt side. **This requires `file_shard`/`file_shards`
   on `Receipt`**, which is a receipt-schema change and therefore also touches `run_bundle`'s
   `write_receipt` call. Strictly stronger than today's check, not weaker.

**Also unverified and adjacent:** `receipt_key(prefix, plan_id, bundle_id)` keys receipts by
`bundle_id`, so **siblings MUST get distinct `bundle_id`s** or they overwrite each other's receipts
in S3 (and `bundle_is_done` then declares K-1 children done that never ran). That is **eng-11's**
`plan_document` / `_bundle_id`, listed in §6 as a contract requirement.

---

## 6. THE PLAN CONTRACT I CODED AGAINST — ⚠️ ASSUMED, NOT CONFIRMED

**eng-11's `stream-11-plan-file-shards.md` was still an unfilled stub at the time of writing**
(checked twice: 05:44 and again after implementing — status boxes all unticked, "publish plan-schema
contract for eng-12 (EARLY)" not done). So I **defined** the interface and coded against it. **Marked
as an assumption, per the brief — I am not guessing silently.**

**What I consume, per plan bundle entry:**

```jsonc
{
  "bundle_id": "stackv2-edu--train--f03",  // MUST be unique per sibling (receipt_key uses it)
  "source": "stackv2-edu",                  // same across siblings
  "domain": null,                            // same across siblings
  "split": "train",                         // same across siblings
  "file_shard":  3,                          // 0 <= file_shard < file_shards
  "file_shards": 7,                          // K; omitted or 1 == whole source
  "shards": [ ... ]                          // THIS sibling's OWN disjoint refs (1/K of the stream)
}
```

**Four requirements on eng-11's side, in priority order:**
1. **`shards` must be this sibling's own disjoint 1/K slice** — *not* the whole stream's list. **My
   no-division budget decision depends on this**, and `_assert_file_shard_family` enforces it.
2. **`bundle_id` must be unique per sibling** — else receipts overwrite in S3 (§5).
3. **Siblings must cover `0..K-1` exactly once**, all declaring the same `file_shards`.
4. **`file_shards <= len(hf_files(spec))`** — `_bundle_files` refuses otherwise, because
   `items[7::5]` on a 5-file source is `[]` and an empty child does **not** fail on its own: it
   yields nothing, writes a receipt, and leaves unfilled refs that look like filter attrition.

**Both field names are cheap to change** — they are read in exactly two places
(`Bundle.from_plan_entry`, `_bundle_files`). If eng-11 has named them differently, that is a
rename, not a redesign. **If eng-11 puts the slice in a nested object** (e.g.
`"file_slice": {"index": 3, "of": 7}`) the change is confined to `from_plan_entry`.

**My defaults are chosen so eng-11 merging first cannot break me**: absent keys mean `(0, 1)` =
whole source = today's behaviour, so my code is a no-op on every plan that does not opt in.

---

## 7. Tests added (17)

| test | what it proves |
|---|---|
| `test_the_union_of_files_read_across_k_children_is_exactly_the_file_list` **×8 params** | **union RECOMPUTED as a set**, no file twice, none dropped; child sizes differ by ≤1 |
| `test_striding_not_contiguous_blocks_on_a_realistically_skewed_source` | stride balances on the "big ones cluster" shape; contiguous is 3.4× imbalanced. Asserted on **BYTES** |
| `test_the_slice_a_child_reads_does_not_depend_on_k_ordering_or_which_child_ran_first` | determinism; also that within-child **order** is stable (`pack` concatenates in read order) |
| `test_k_equals_one_reads_every_file_exactly_as_before_file_sharding` | the default is the old behaviour; a pre-file-shard plan entry still reads |
| `test_the_read_budget_is_not_divided_by_k_because_the_refs_already_are` | **the budget decision**, both directions (not K×, not 1/K²) |
| `test_a_bundle_whose_source_has_fewer_files_than_k_is_refused_before_reading` | empty slice refused before the first byte |
| `test_an_out_of_range_file_shard_is_refused_when_the_bundle_is_built` | out-of-range is not a no-op |
| `test_a_plan_that_gives_every_sibling_the_whole_shard_list_is_refused` | the silent K-fold duplication |
| `test_a_missing_sibling_is_refused_so_files_are_never_silently_unread` | + the positive case: 3 complete siblings accepted, refs disjoint & complete |
| `test_file_sharding_composes_with_the_finephrase_id_partition` | the two partitions are orthogonal; union == exactly the `faq` partition |

**The 8 params are the real file counts:** `(57,4)` and `(95,7)` are the **uneven** cases the brief
demanded — ENG-EXEC correctly flagged that `57/K=3` divides evenly, so I use **57/4 = 14,14,14,15**
and **95/7 = 13 or 14** as the uneven ones, keeping the even carve Ks as controls.

**Two of my own tests failed first and were fixed** (recorded rather than hidden): I used
`partition_of` (returns an **int** index) where `format_for_id` (returns the format **string**) was
meant; and my skew fixture put 10 big files across K=4, which a stride cannot balance evenly — I
resized to 12 so the assertion could be a clean equality rather than an unjustifiable tolerance.

---

## 8. Numbers, graded

| number | value | grade |
|---|---|---|
| baseline tests | 1338 | MEASURED |
| tests after | **1355** (+17) | MEASURED |
| mutation 1 failures | 14 (10 mine) | MEASURED |
| mutation 2 failures | **1** (only the test written for it) | MEASURED |
| `stackv2-edu` files / CV / max-min | 95 / 0.211 / 2.09× | MEASURED (HF tree API, pinned rev) |
| `finepdfs-edu` | 100 / 0.034 / 1.16× | MEASURED |
| `nemotron-cc-math-3` | 57 / 0.096 / 3.61× | MEASURED |
| `nemotron-cc-math-4plus` | 46 / 0.114 / 3.10× | MEASURED |
| worst stride imbalance | **2.6%** (`stackv2-edu` K=7) | MEASURED |
| contiguous imbalance, `stackv2-edu` K=7 | 14.1% | MEASURED (I get 1.1413; ENG-EXEC 1.132 — same conclusion) |
| `verify_bundle_set` rejects a correct sharded build | fires `bundle-set-duplicate-stream` | **MEASURED**, reproduced free with no `s3=` |
| 51.38 h → ~11.19 h | the makespan win | DERIVED (ENG-EXEC), **not reachable until §5 is fixed** |
| 7.57 h / 11.07 h carve figures | ENG-EXEC's simulation | DERIVED — byte-scaled at a uniform tok/s rate |
| per-child tok/byte for PDF and code | **nobody has measured this** | UNVERIFIED |
| `_reader_for` against live HF in a Batch container | still never run | UNVERIFIED (pre-existing) |

---

## 9. Decisions needed from ENG-EXEC

1. **§5 is a hard blocker and needs an owner.** `verify_bundle_set` must become file-shard-aware or
   no sharded build passes `verify`. Recommend option 3; it needs `file_shard`/`file_shards` on
   `Receipt` and therefore touches `run_bundle`'s `write_receipt` call. **Not mine, not eng-11's.**
2. **Confirm the plan field names with eng-11** (§6). Cheap to change; two call sites.
3. **eng-11 must guarantee siblings get distinct `bundle_id`s** — otherwise receipts overwrite in S3
   and `bundle_is_done` marks K-1 never-run children as done.
4. **Should `_bundle_files` also refuse a K that leaves a child with very few files?** Today it only
   refuses `K > len(files)`. `nemotron-cc-math-4plus` at K=2 gives 23 files/child, fine; but a future
   K=40 on a 46-file source gives 1-2 files/child and the per-file size variance (3.10× max/min there)
   would then dominate the makespan. **Not a defect today; a footgun at larger K.**

## Constraints honoured
No S3 writes · no Batch submits · no `manifest.json` · no `git push` · no live HF **bulk** reads
(tree-API metadata only; all tests inject `documents=`) · no Maple configs read or cited ·
stayed out of `plan_document` and `allocate_ordinals`.
