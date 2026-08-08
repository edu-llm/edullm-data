# stream-11 — plan-level file-sharding with plan-assigned disjoint ordinal ranges

**Agent:** eng-11 (ENG-EXEC Wave 4)
**Worktree:** `/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/eng-11--plan-file-shards`
**Branch:** `agent/eng-11/plan-file-shards` (base `d593db1`)
**Started:** 2026-08-08

---

## 🔴 PLAN-SCHEMA CONTRACT — **eng-12 READ THIS FIRST** (published before implementation)

Status of this section: **DESIGN FROZEN 2026-08-08, published early on purpose.** If the
implementation forces a change I will edit this section IN PLACE and say so at the bottom.

### The one-line version for eng-12

> **A file-shard part is just another `Bundle`.** `bundles_of(plan)` returns one `Bundle` per part,
> `_shard_slice(bundles_of(plan), shard, of)` distributes parts across array children exactly as it
> distributes bundles today, and **`bundle.tokens` is ALREADY the part's share.**
> Your entire job in `_reader_for` is:
>
> ```python
> files = hf_files(spec)
> idx, of = bundle.file_shard          # always a 2-tuple, NEVER None
> for entry in _shard_slice(files, idx, of):
>     ...
> ```
>
> **DO NOT divide the budget by K.** `budget = int(bundle.tokens * _CHARS_PER_TOKEN *
> _FILTER_HEADROOM / keep_rate)` is already correct, because `Bundle.tokens` is derived from the
> part's own refs (`sum(r.tokens for r in self.shards)`), and the plan gave the part only ~1/K of
> the stream's refs. Dividing again reads 1/K² and every part underfills.

### The field

On every bundle entry in `plan["bundles"]`:

```json
"file_shard": {"index": 0, "of": 1}
```

- **Always present, on every bundle, including unsharded ones** (`{"index": 0, "of": 1}`).
  Never absent, never null. A reader must never have to guess a default.
- `0 <= index < of`, `of >= 1`.
- `plan["schema"]` becomes **`"edullm-build-plan/v2"`**.

### The Python surface

`Bundle` (frozen dataclass, `corpus_build.py`) gains:

```python
file_shard: tuple[int, int] = (0, 1)     # (index, of)
```

- Always a 2-tuple. `(0, 1)` for an unsharded bundle. **Never `None`** — so
  `idx, of = bundle.file_shard` is unconditionally safe and `_shard_slice(files, idx, of)` with
  `of == 1` is the identity (`items[0::1] == items`, asserted in
  `tests/test_ingest_reservoir.py:385`).
- Convenience properties on `Bundle`:
  - `bundle.file_shard_index` -> int
  - `bundle.file_shard_count` -> int
  - `bundle.is_file_sharded` -> bool (`of > 1`)

### `bundle_id`

- `of == 1` -> **unchanged**: `"stackv2-edu--train"`. Today's 161 bundle ids and receipt keys are
  byte-identical. This is deliberate: it keeps the blast radius of the schema bump off every
  unsharded stream.
- `of > 1` -> `"<base>--p{index:02d}of{of:02d}"`, e.g. `"stackv2-edu--train--p03of07"`.
  Two digits each; `_assert_safe_key` and `SAFE_SEGMENT_RE` both accept it (lowercase + digits +
  `-`). K is capped at 99 for that reason.

### Ordinals — the whole point

- `allocate_ordinals` is **UNCHANGED** and still receives **one plan row per (source, domain,
  split) stream**. It still assigns each stream one dense, contiguous, globally-unique block.
  No child, and no part, ever allocates an ordinal.
- `plan_document` then **partitions the block the stream already owns** into K contiguous
  sub-blocks: sizes `n//K`, with the first `n % K` parts getting one extra. Deterministic, a pure
  function of `(n, K)`, no data dependence, no ordering dependence.
- **Invariant, asserted by test:** the union of the K parts' shard paths equals the unsharded
  stream's shard path set, exactly — no gaps, no overlaps, no reuse. And the union over the WHOLE
  plan is identical to the un-file-sharded plan's. **File-sharding is corpus-content-neutral: the
  set of object keys the build writes does not change.**

### Where K comes from

`plan_document(..., file_shards: Mapping[str, int] | None = None)`, keyed by **`spec.key`**
(same key space as `tokens_per_source`). If the argument is `None`, it falls back to
`registry_meta["_file_shards"]` — a **top-level registry metadata map**, not a row field, so
`CorpusSpec` and the registry row schema are untouched.

```json
"_file_shards": {"stackv2-edu": 7, "finepdfs-edu": 4,
                 "nemotron-cc-math-3": 3, "nemotron-cc-math-4plus": 2}
```

Refused at plan time (all raise `BuildDriverError`): `K < 1`, non-int, `K > 99`, a key that names
no drawn row, and **`K > n_shards` for any stream of that source** (a part with zero refs has no
destination — the same failure `shard_plan` refuses for a whole stream).

### What a part does NOT change

- `source_label` — **unchanged**. Parts share the `source` path segment, so nothing
  consumer-visible moves. This is the entire reason to file-shard instead of calling
  `split_source_rows`, which would publish `stackv2-edu-01…-07` permanently inside
  `manifest_sha256`.
- `domain`, `split`, the shard paths, the token targets, `SHARD_TOKENS`.
- `pack`, `_pack_stream`, `_drain_surplus` — a part hands `pack` its own ref subset for its own
  stream, which is the shape `pack` already accepts.

### The two things eng-12 must NOT do

1. **Do not re-divide the budget.** (Stated above; it is the failure that silently starves every
   child.)
2. **Do not switch `_shard_slice` to contiguous blocks.** Keep the stride. eng-12's own
   measurement already showed contiguous costs `stackv2-edu` 8.35 h at 1.132x imbalance versus
   striding's 7.57 h at 1.026x.

---

## Log

- [x] read `IMPLEMENTATION-PLAN.md` §8A.5a
- [x] read `allocate_ordinals`, `ShardRef`, `plan_document`, `shard_plan`, `pack`,
      `_drain_surplus`, `run_bundle`, `bundles_of`, `verify_bundle_set`
- [x] publish plan-schema contract for eng-12
- [x] implement — commit `b0eff92`
- [x] tests (union-of-ordinals, several K, mutation proof) — 1338 → **1363**
- [x] new PLAN_ID — **`68ebedaaddc7eb06`** at K=7/4/3/2; `2dee727972725556` v2-unsharded

**STATUS: COMPLETE.** The contract section above needed **no in-place correction** — the design as
published is the design as shipped.

### Baseline, MEASURED-IN-CODE 2026-08-08 (this worktree, `d593db1`)

```
python3 -m pytest -q --collect-only   ->  1338 tests collected
plan_document(registry) -> plan_id 9f969e08a5bbbd07  bundles 161  shards 39307
                           tokens 982,752,985,088   (drawn rows 132 of 133)
```
Top bundles by shard count (MEASURED-IN-CODE): `stackv2-edu--train` 4,298 shards /
107,458,527,232 tok; `finepdfs-edu--train` 2,507 / 62,679,973,888; `nemotron-cc-math-3--train`
1,512 / 37,802,999,808; `nemotron-cc-math-4plus--train` 915 / 22,876,815,360; then eight
`fineweb-edu-NN--train` at 626 each.

---

## ✅ LANDED — commit `b0eff92`, tests **1338 → 1363**, all green

`src/edullm_data/corpus.py` (+83), `src/edullm_data/corpus_build.py` (+249),
`tests/test_corpus_build.py` (+472). Branch `agent/eng-11/plan-file-shards`. **Not pushed.**

### Contract reconciliation with eng-12 — **its assumed shape and mine AGREE; nothing to rename**

ENG-EXEC relayed the interface eng-12 coded against before my file was populated. Item by item:

| eng-12 assumed | I shipped | verdict |
|---|---|---|
| `Bundle.file_shard`, default `(0, 1)` = whole source | `Bundle.file_shard: tuple[int,int] = (0, 1)` | **IDENTICAL** |
| `from_plan_entry` reads with `.get`, pre-file-shard plans still parse | exactly that | **IDENTICAL** |
| each sibling carries its own disjoint `shards` list | yes | **IDENTICAL** |
| unique `bundle_id` per sibling, `…--fs0of3` style | `…--p00of03` | **same semantics, different spelling** |
| `K <= file count` | I enforce **`K <= shard count`** | **different constraint, see below** |

**Two deltas, both deliberate, neither needs a call-site change in `_reader_for`:**

1. **Spelling `p00of03` not `fs0of3`.** Two digits on BOTH numbers. `fs0of3` is one digit on the
   index, and `MAX_FILE_SHARDS` is 99 — at K=12 a one-digit index collides parts 1 and 11 onto one
   receipt key, which `bundle_is_done` reads as a DONE child that never ran. The eng-12 code does
   not construct or parse this string (the plan supplies it), so this is a zero-call-site delta.
   **If eng-12 hard-coded the string anywhere, that is the one thing to re-check at merge.**
2. **`K <= shard count`, not `K <= file count`.** Both are needed and they bind at different
   places. Mine is what the PLAN can check — a part with no ordinals has nowhere to write, so `pack`
   reports `orphan_streams` and drops its documents in full. eng-12's is what the READER can check
   and I cannot: `plan_document` is PURE and never lists HF, so it cannot know the file count.
   **Keep both. They are complementary, not redundant.** Note which one binds in practice: the
   **val** stream is 0.5% of a source, so `stackv2-edu` has 4,298 train shards and **21 val shards**
   — a 7-way split is fine, a 25-way split of the val stream is not, and my error message says so.

### The mechanism, and why it is small

`allocate_ordinals` is **UNCHANGED**. It still receives one plan row per `(source, domain, split)`
and still assigns each stream one dense contiguous globally-unique block. The new
`corpus.partition_ordinals(refs, K)` **cuts a block that already exists** into K contiguous
sub-blocks: sizes `n//K`, first `n % K` parts take one extra. A pure function of `(n, K)` — no data
dependence, no ordering dependence, nothing a child observes.

**The property that makes this safe pre-FREEZE (MEASURED-IN-CODE on the shipping registry):**

```
flat     plan: 161 bundles, 39,307 shard paths
sharded  plan: 185 bundles, 39,307 shard paths     K = 7/4/3/2
sorted(sharded paths) == sorted(flat paths)  ->  True
biggest unit of work: 4,298 shards -> 627 shards
```

**File-sharding does not change WHICH objects the build writes — only their grouping into units of
work.** No shard path, no `source_label`, no token count, nothing inside `manifest_sha256` moves.
That is the whole difference from `split_source_rows`, which would publish `stackv2-edu-01…-07`
permanently and consumer-visibly.

### 🔴 NEW PLAN_ID — `artifacts/final-dataset/corpus-registry.json` (133 rows, 986,000,000,000)

| plan | `plan_id` | bundles | shards |
|---|---|---|---|
| **before this change** (v1 schema) | `9f969e08a5bbbd07` | 161 | 39,307 |
| **v2 schema, no `file_shards`** | **`2dee727972725556`** | 161 | 39,307 |
| **v2 schema, K = 7/4/3/2** | **`68ebedaaddc7eb06`** | **185** | 39,307 |

All MEASURED-IN-CODE 2026-08-08 in this worktree. **Byte-identical on re-run: verified.**

⚠️ **The `plan_id` moves even with no source file-sharded** (`9f969e08…` → `2dee7279…`), because
`plan_id` is the sha256 of the plan document and `file_shard` is on every bundle entry. That is
expected and stated in the CEO's own framing of the question. It is acceptable **only pre-FREEZE**:
after FREEZE, adding this field is a re-plan of the whole corpus. **The plan is not frozen and no
plan has been uploaded to S3 from this worktree.**

Which `plan_id` is authoritative is **a decision for ENG-EXEC/CEO, not me** — it depends on whether
K is set in the registry's `_file_shards` or passed per-run. My recommendation: put K in the
registry so the plan artifact is self-describing and `68ebedaaddc7eb06` is the one number to quote.

### The plan-schema contract, as shipped

Unchanged from the contract published above except the two reconciliation deltas. Summary:

- `plan["schema"] = "edullm-build-plan/v2"` (`corpus_build.PLAN_SCHEMA`).
- every bundle entry carries `"file_shard": {"index": i, "of": k}`, **including unsharded**
  (`{"index": 0, "of": 1}`). Mandatory, never null.
- `Bundle.file_shard: tuple[int, int] = (0, 1)`; props `file_shard_index`, `file_shard_count`,
  `is_file_sharded`. `Bundle.__post_init__` refuses an out-of-range pair.
- `bundle_id`: unchanged for `of == 1`; `--p{index:02d}of{of:02d}` otherwise.
- K from `plan_document(file_shards={spec.key: K})` or `registry_meta["_file_shards"]`.
  The explicit argument WINS, so an operator can override the registry without editing it.
- **`bundle.tokens` is ALREADY the part's share** — derived from the part's own refs. A reader must
  NOT divide the budget by K again; that reads 1/K² and every part underfills.

### `bundles_of` is STRICT where `from_plan_entry` is LENIENT — and that asymmetry is the point

`from_plan_entry` must tolerate a missing `file_shard`: a v1 plan could not express a part, and
"unsharded" is the correct reading of one. But a **v2** plan that dropped the field on a bundle that
IS a part would hand that child the whole file list against 1/K of the ordinals — a K-fold over-read
whose **only symptom is surplus, and `partial_source=True` ignores surplus by design**
(`run_bundle` passes it, deliberately, because `_reader_for` over-delivers). So the refusal lives in
`bundles_of`, which can see `plan["schema"]`, not in `from_plan_entry`, which cannot.

### 🔴 MUTATION RESULTS — three mutations, all caught. The union test bites.

Every mutation applied to the real source, suite re-run, then reverted (sources restored from
backup and 1363 re-verified green).

| # | mutation | result | the message |
|---|---|---|---|
| **1** | `partition_ordinals` overlaps ranges by 1 (`ordered[at-1 : at+take]`) | **20 failed** | `tokens/stackv2-edu/train-00003.u32le.bin is claimed by both 'stackv2-edu--train--p00of03' and 'stackv2-edu--train--p01of03'` |
| **2** | **floor-only cut** (`take = base`), which drops `n % K` shards with **no overlap, no duplicate id, no path collision** — the plan-time guard is BLIND to it | **7 failed** | `K=3: the union of what 3 children wrote is not the unsharded set. missing=[9] extra=[] reused=[]` |
| **3** | all K parts share one `bundle_id` (the CEO/ENG-EXEC-named failure) | **20 failed** | `two bundles share bundle_id 'stackv2-edu--train' … their receipts overwrite each other in S3 and bundle_is_done() then declares every one of them DONE` |

**Mutation 2 is the one that proves the union test earns its place.** Mutations 1 and 3 are caught
by the cheap plan-time guard `_assert_plan_is_disjoint`. Mutation 2 produces a plan that is
internally perfectly consistent — disjoint ranges, unique ids, unique paths — and is only visible
by comparing the union of what the children **actually wrote to S3** against the unsharded set. A
test asserting "K children ran" or "the counts add up" passes mutation 2. Ours names the missing
ordinal.

### 🔴 BLOCKER E15 — I reproduced it INDEPENDENTLY, before ENG-EXEC's message arrived, and it is worse at scale than the 3-part repro suggests

**(a) Claim.** `verify_bundle_set` REJECTS a correct file-sharded build.
`corpus_receipt.py:1401-1411` groups receipts by `(source, domain, split)` and raises
`bundle-set-duplicate-stream` when more than one claims a stream. **K parts share one stream by
construction.**

**(b) Evidence.** MEASURED, free, no `s3=` (the cheap tier `verify_bundle_set`'s own docstring
advertises). I synthesised a **PERFECT** receipt set for the whole 185-bundle `68ebedaaddc7eb06`
plan — every shard path distinct, every digest distinct, one plan_id, one wheel:

```
VERDICT on a PERFECT file-sharded build:
   bundle-set-duplicate-stream   8
   total violations              8
```

**8, not 4** — one per file-sharded STREAM, and each of the four sources has both a train and a val
stream. `bundle-set-shard-path-collision` does **not** fire, which is the proof that the ordinal
ranges really are disjoint and only the stream GROUPING is confused.

**(c) Numbers moved.** `verify` is a gate that exits non-zero. The build runs ~11 h and then fails
its own verification. **The entire 51.38 h → ~11 h win is unrealisable until this is fixed.**
I concur with ENG-EXEC's assessment without reservation.

**(d) Blast radius.** `_cmd_verify` returns 1; nothing publishes. No data is lost — this is a
false-negative gate, not corruption. But it also means **no file-sharded build can be verified at
all**, so the gate that catches the real `bundle-set-incomplete` case is unusable on exactly the
builds that need it most (185 children instead of 161 is 24 more chances of a lost child).

**Pinned by a test asserting the CURRENT WRONG BEHAVIOUR** —
`test_verify_bundle_set_REJECTS_a_correct_file_sharded_build___KNOWN_GAP`. Its docstring instructs
the fixer to **INVERT it, not delete it**, and repeats ENG-EXEC's constraint: do NOT group on
`bundle_id`, which would weaken the genuine retry-that-did-not-replace case.

### ✅ ENG-EXEC's question answered: CAN `Receipt` carry these fields? YES — and the schema cost is ZERO

**Yes, cleanly, and the shape I chose is the one that makes it cheapest.** Three reasons:

1. **`Receipt` already has the sibling fields that establish the pattern.** `unfilled: tuple[str,...]
   = ()`, `filter: FilterRecord | None = None`, `keep: KeepRecord | None = None` — all added as
   defaulted optional fields, and `Receipt.from_dict` reads every field with `.get(…, default)`
   (verified at `corpus_receipt.py:532-560`). So `file_shard: tuple[int, int] = (0, 1)` slots in
   with **no change to `from_dict`'s tolerance** and **no v1 receipt becoming unreadable**.
2. **`RECEIPT_SCHEMA_VERSION` is ALREADY `edullm-corpus-receipt/v2`** with
   `READABLE_RECEIPT_SCHEMAS = {v1, v2}`. Wave 0 took the bump. Adding a defaulted field needs **no
   further bump** — a v2 reader ignores an unknown key and a v2 receipt without the key reads as
   `(0, 1)`, which is exactly right for the ~157 unsharded streams. **This is a pure additive
   field, not a schema migration.**
3. **The plan already assigns it, so the producer change is one line.** `run_bundle` passes
   `bundle_id=bundle.bundle_id` to `Receipt.from_pack_result` explicitly; adding
   `file_shard=bundle.file_shard` alongside it is the whole producer side. `Bundle.file_shard` is
   never `None`, so there is no absent case to handle.

**The fix shape I would recommend to whoever owns it** (not implemented — not my surface): group by
`(source, domain, split, file_shard)` in the duplicate-stream check, and add a **new**
`bundle-set-incomplete-file-shard` violation for the case only that grouping can now see — **K
declared but fewer than K parts present.** That is a strictly STRONGER gate than today's, because
today a missing part of a file-sharded stream is invisible: the stream has a receipt, so
`bundle-set-incomplete` stays quiet. **Fixing E15 correctly does not weaken the gate, it closes a
hole file-sharding would otherwise open.**

### ⚠️ SECOND FINDING for the same fixer — `bundle_id_for` also collides, and nothing currently notices

**(a) Claim.** `corpus_receipt.bundle_id_for(plan_id, stream)` (`corpus_receipt.py:636-648`) derives
the id from `(plan_id, source, domain, split)` **only**, so K parts of one stream get the SAME
default id.

**(b) Evidence.** MEASURED: `bundle_id_for('p', ('stackv2-edu', None, 'train'))` returns
`8d16efcd49721e9b` for every part. It is the default at
`corpus_receipt.py:616` — `bundle_id=bundle_id or bundle_id_for(plan_id, stream)`.

**(c) Numbers moved.** None **today**, and that is why it needs writing down: `run_bundle` always
passes `bundle_id=bundle.bundle_id` explicitly, so the defaulting branch is never taken on the
build path. **The collision is latent, not live.**

**(d) Blast radius.** Any future caller of `Receipt.from_pack_result` that omits `bundle_id` — a
test fixture, a repair script, a re-receipting tool — silently gives K parts one receipt key and one
overwrites the others. This is the same class as `_assert_unique_identities`' `source_label` case:
a defaulted derivation that is correct for streams and wrong for parts. **Recommend the E15 fixer
extend `bundle_id_for` with the `(index, of)` pair while it is in that file.** Grading: the
collision is MEASURED; "nothing currently reaches it" is MEASURED by grep (2 non-definition
callers, both in tests plus the one defaulting line).

### Tests added — 25 new, 1338 → 1363

The named anti-pattern was *"a test asserting the function exists, or that K children ran, is
decoration."* Every test below recomputes a set and compares it; **none asserts a count as its
primary claim.**

| test | what it RECOMPUTES |
|---|---|
| `test_K_children_of_one_bundle_write_EXACTLY_the_unsharded_ordinal_set[1,2,3,7]` | **THE test.** Runs every child end to end through `run_bundle` on a `FakeS3`, then re-parses the ordinals out of `s3._store` **through `parse_shard_name`** — the very function whose blindness makes reuse invisible — and asserts set equality with the unsharded plan. Names `missing` / `extra` / `reused` on failure. Each child gets a DIFFERENT document set (seeded by `file_shard_index`) so a cross-range write is genuinely different bytes, not a harmless duplicate |
| `test_the_PLAN_hands_out_disjoint_ranges_before_any_child_runs[1,2,3,7]` | the same union at plan time (where the CEO's condition binds) **plus** contiguity per part |
| `test_a_K_that_does_not_divide_evenly_still_partitions_every_shard` | the exact size **vector** `[6,6,6,6,6,5,5]` for 40 over 7. A floor-only cut gives 7×5=35 (drops 5); a ceil-only cut gives 7×6=42 (reuses 2). Both fail this |
| `test_the_shard_path_set_is_UNCHANGED_by_file_sharding_on_the_real_registry` | corpus-content neutrality on the shipping 133-row registry, not a fixture |
| `test_every_part_gets_its_OWN_bundle_id_because_receipts_are_keyed_on_it` | uniqueness **through `receipt_key()` itself**, because the key is what actually collides. Also pins that unsharded and domain-bearing ids are unchanged |
| `test_a_part_id_survives_the_round_trip_into_a_real_receipt_key` | the suffix satisfies `_assert_safe_key` at K=12 — a failure there raises after a bundle's full billable work |
| `test_a_part_reads_its_own_file_slice_and_the_union_of_files_is_the_whole_source` | strides a real 95-file list with each part's own pair and asserts the union is a **partition** — the plan's half of eng-12's contract |
| `test_each_part_carries_its_OWN_token_budget_so_a_reader_must_not_divide_again` | token **conservation** across parts, for train AND val |
| `test_the_plan_stays_a_pure_deterministic_content_address_under_file_sharding` | byte-identical re-run; different K ⇒ different `plan_id`; **K=1 ≡ unsharded, same document** |
| `test_the_file_shard_field_is_on_EVERY_bundle_including_unsharded_ones` | mandatory-ness |
| `test_a_v2_plan_missing_the_field_is_REFUSED_rather_than_defaulted` | the strict/lenient asymmetry, both directions |
| `test_more_parts_than_shards_is_refused_because_an_empty_part_has_nowhere_to_write` | at both layers (`plan_document` and `partition_ordinals`) |
| `test_a_bad_file_shards_map_is_refused_at_plan_time_not_discovered_hours_in` | unknown key, `0`, `True`, `100` |
| `test_the_map_can_come_from_the_registry_metadata_rather_than_the_call` | both sources agree; the argument wins |
| `test_the_disjointness_guard_catches_a_plan_that_reuses_an_ordinal` | the guard itself, by mutating a correct plan |
| `test_partition_ordinals_refuses_refs_from_two_different_streams` | the one-stream precondition |
| `test_an_out_of_range_file_shard_pair_is_refused_when_the_bundle_is_built` | `index >= of` and `index < 0` |
| `test_K_children_are_distributed_across_array_children_by_the_EXISTING_stride` | parts are a partition of `_shard_slice` output at `of` = 1,3,5,16 — i.e. no new driver machinery is needed |
| `test_verify_bundle_set_REJECTS_a_correct_file_sharded_build___KNOWN_GAP` | **pins blocker E15.** Asserts the current WRONG behaviour, with instructions to invert |

### Determinism — MEASURED, no build required

`plan_document` remains **PURE**: no clock, no S3, no environment. Verified byte-identical output
across re-runs on the real registry. Nothing in the partition depends on execution order, on which
child ran first, or on data — `partition_ordinals` is a function of `(len(refs), K)` and
`allocate_ordinals`' sort is by tuple, not input order. **File-sharding cannot make output depend
on which child ran first, because the child receives its range and reads it; it computes nothing.**

**The free `verify_bundle_set` prediction was run** (no `s3=`) and is the source of the 8-violation
E15 measurement above. Beyond E15 the set-level verdict is clean: no path collision, no duplicate
digest, no plan mismatch, no wheel mismatch, no revision conflict.

### Numbers, graded

| number | value | grade |
|---|---|---|
| tests before / after | 1338 / **1363** | **MEASURED** (this worktree) |
| `plan_id` before this change | `9f969e08a5bbbd07` | **MEASURED-IN-CODE** |
| `plan_id` v2, unsharded | **`2dee727972725556`** | **MEASURED-IN-CODE** |
| `plan_id` v2, K=7/4/3/2 | **`68ebedaaddc7eb06`** | **MEASURED-IN-CODE** |
| bundles: 161 → 185 | +24 | **MEASURED-IN-CODE** |
| shard paths, both plans | **39,307, identical set** | **MEASURED-IN-CODE** |
| biggest unit of work | 4,298 → **627** shards | **MEASURED-IN-CODE** |
| `stackv2-edu--train` | 4,298 shards / **107,458,527,232** tok | **MEASURED-IN-CODE** |
| `stackv2-edu--val` | 21 shards | **MEASURED-IN-CODE** (this is what binds K, not the train stream) |
| E15 violations on a perfect 185-bundle build | **8** | **MEASURED** (free tier, no `s3=`) |
| `bundle_id_for` collides for parts | `8d16efcd49721e9b` for all K | **MEASURED** |
| 51.38 h → ~11.07 h, 40.3 h saved | — | **DERIVED** (ENG-EXEC/eng-12's simulation, not re-derived by me) |
| 72,615 tok/s/vCPU | — | **MEASURED** upstream, inherited, not re-measured here |
| file counts 95/100/57/46, CV 0.211/0.034/0.096/0.114 | — | **MEASURED** by ENG-EXEC + eng-12; I relied on it and did not re-measure |

**Nobody has measured** whether a file-sharded child's realized token yield matches its ordinal
range on real data. My design does not require it to — `pack` tolerates `unfilled`, ordinal gaps
are legal, and `partial_source=True` absorbs surplus — but **the DIRECTION of the error is
untested at scale.** See the next section.

### The one thing I could not settle, and it is a real residual risk

**A part's ordinal range is a RANGE, not a QUOTA**, and how many refs a part fills depends on the
tokens ITS files hold. `_shard_slice` strides, which eng-12 measured holds imbalance to **2.6%**, so
the expected miss is small. Both directions are already handled:

- **Underfill** — `pack` leaves surplus refs `unfilled`, ordinal gaps are legal
  (`allocate_ordinals`' docstring: nothing in `validate.py` checks contiguity), the receipt records
  the list. Costs nothing.
- **Overfill** — `_drain_surplus` refuses a leftover of one whole shard **unless
  `partial_source=True`**, which `run_bundle` passes unconditionally. So an overfilling part does
  not raise; it silently stops at its last ref and **discards the excess documents.**

⚠️ **That asymmetry is worth stating plainly: with `partial_source=True` an overfilled part loses
tokens silently.** It is not new — `run_bundle` has always passed it and the reader has always
over-delivered — but file-sharding multiplies the number of boundaries at which it can happen by K.
At 2.6% imbalance against `_FILTER_HEADROOM` 1.5 the margin is large, so I did not change it. **I
flag it rather than fix it because changing `partial_source` is `run_bundle`'s surface and the
end-of-run-failure history (25 of 27 bundles) says the current default is the right one.**

### Decisions needed

1. **Which `plan_id` is authoritative** — `2dee727972725556` (v2, K set per-run) or
   `68ebedaaddc7eb06` (K in `registry_meta._file_shards`, plan self-describing). I recommend the
   latter. **ENG-EXEC/CEO, not mine.**
2. **E15 must be assigned and must land before any file-sharded build runs.** Confirmed
   implementable at zero receipt-schema cost; recommended fix shape and the `bundle_id_for` sibling
   finding are above.
3. **Whether K goes into `artifacts/final-dataset/corpus-registry.json` as `_file_shards`.** I did
   **not** edit the registry — that is a plan-content decision and the file is another stream's
   artifact. The mechanism reads it if present.
4. **The `p00of03` vs `fs0of3` spelling** — mine, for the two-digit reason. Flag if eng-12
   hard-coded the string.
