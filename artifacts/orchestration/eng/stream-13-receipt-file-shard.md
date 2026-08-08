# stream-13 — receipt file-shard awareness (E15)

**Agent:** eng-13 | **Branch:** `agent/eng-13/receipt-file-shard` (base `d593db1`) | **2026-08-08**
**Worktree:** `/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/eng-13--receipt-file-shard`
**Status: COMPLETE.** Commit `5184f44`. Tests **1338 → 1372** (+34), no regressions. 5 mutations, all
caught, all reverted. Not pushed.

---

## 1. E15 is FIXED. The reproduction, before and after

**Before** (MEASURED-IN-CODE 2026-08-08, no `s3=`, no network) — three siblings, disjoint ordinals,
distinct digests, one stream:

```
VIOLATION bundle-set-duplicate-stream: 3 receipts claim stream ('stackv2-edu', None, 'train')
```

**After**, at plan shape — 38 receipts / 14 streams / **8 file-sharded streams** (four sources ×
train+val, matching eng-11's count), K ∈ {7, 4, 3, 2} plus three unsharded sources:

| # | scenario | result | grade |
|---|---|---|---|
| A | full correct set | **ZERO VIOLATIONS** | MEASURED-IN-CODE |
| B | unique `bundle_id`s | **38 of 38** | MEASURED-IN-CODE |
| C | drop `stackv2-edu/train` part 3 of 7 | `bundle-set-incomplete-file-shard`, names part `[3]` | MEASURED-IN-CODE |
| D | plain retry on an **unsharded** stream | still `bundle-set-duplicate-stream` | MEASURED-IN-CODE |

The 40 h lever is unblocked **on the receipt side**. See §5 — one wiring line in `corpus_build.py`
(not my file) is still required for end-to-end.

## 2. What landed — all in `corpus_receipt.py` + `test_corpus_receipt.py`

| # | change | where |
|---|---|---|
| 1 | `Receipt.file_shard` / `.file_shards`, default `(0, 1)` = whole stream; `.is_file_sharded` | `Receipt` |
| 2 | `Receipt.__post_init__` — out-of-range index / `file_shards < 1` / non-int is a **LOAD failure** | `Receipt` |
| 3 | `to_dict` emits nested `{"index": i, "of": k}` **only when `of > 1`** | `Receipt.to_dict` |
| 4 | `_parse_file_shard(doc)` — accepts nested AND flat, absent → `(0,1)`, garbled → **raises** | new, before `Receipt` |
| 5 | `from_pack_result(file_shard=, file_shards=)` | `Receipt` |
| 6 | **`_check_set_file_shard_families`** — the fix; replaces the inline duplicate-stream loop | new, `:~1580` |
| 7 | `bundle_id_for(..., file_shard=, file_shards=)` — **FIXED**, not documented | `:~770` |

### The fix: grouping narrowed by ONE field, never removed

Grouping is now `(source, domain, split, **file_shard**)`. Four codes:

- `bundle-set-duplicate-stream` — **unchanged in force.** For an unsharded stream `(stream, 0)` *is*
  the old key, so that path is byte-for-byte what it was. Two receipts for the same *part* still fire;
  the message now names the part and says the other siblings are unaffected.
- `bundle-set-incomplete-file-shard` — **NEW GROUND** (§3).
- `bundle-set-file-shard-count-conflict` — members disagree about K, **or** K contradicts the number
  of bundles the plan holds for that stream. Checked *before* coverage, so a wrong K never produces a
  confident "part 5 of 7 missing".
- `bundle-set-file-shard-overlap` — siblings claim one path. Reported separately from the generic
  `bundle-set-shard-path-collision` because between siblings the cause is specific and actionable: the
  plan failed to partition the stream's ordinal block.

**I did NOT take the easy route.** Grouping on `bundle_id` makes the key unique by construction, so
the check could never fire again and the retry defect would be silently deleted. Mutation 1 (§4)
measures exactly that: it fails the **pre-existing** `test_two_receipts_for_one_stream_are_reported`,
which is the proof the old check survived.

**Recomputed, never trusted.** The family is counted from the receipts present; `of` is used only as
the expectation to falsify and is reported next to the recomputed reality. `expected` is deliberately
NOT used to source K (after file-sharding it contains the stream K times, so trusting it would compare
the plan against itself) — only to cross-check plan-vs-receipt K.

## 3. The gate got STRONGER — `bundle-set-incomplete-file-shard`

Today a missing part is **invisible**. `bundle-set-incomplete` fires only when a stream has *no*
receipt; a family missing part 3 of 7 still has six. Asserted literally in the test: every survivor
returns `verify_receipt(...) == []`, ordinals are disjoint, counts are internally consistent, Gate A
would pass — the corpus publishes short by that part's share while the mixture still names the full
source. **This is ground no check previously covered.** Grade: MEASURED-IN-CODE.

## 4. MUTATIONS — 5 applied, 5 caught, all reverted (suite back to 1372)

| # | mutation | tests failing | note |
|---|---|---|---|
| 1 | accept any duplicate stream (the check never fires) | **4** | incl. **pre-existing** `test_two_receipts_for_one_stream_are_reported` → the retry check was NOT weakened |
| 2 | **trust the declared `of`** (`range(len(by_part))`) | **2** | see the finding below |
| 3 | garbled `file_shard` defaults to `(0,1)` instead of raising | 4 | all four param cases |
| 4 | emit `file_shard` unconditionally | 2 | catches the historical-digest break |
| 5 | drop `file_shard` from `bundle_id_for`'s material | 2 | K parts collide |

🔴 **FINDING (mutation 2).** The naive test — *drop part 1 of 3* — does **NOT** catch "trust the
declared `of`". Only `test_the_missing_part_check_recomputes_the_family_and_does_not_trust_the_declared_of`
(two receipts both declaring `of=7`) does. A suite containing only the obvious negative test would
have shipped a coverage check that reads a field and believes it. This is the same shape as eng-12's
budget finding: the wrong behaviour was visible to 1 test of 1,372.

## 5. 🔴 STILL OPEN — one wiring line, NOT in my file. **Assign it.**

`corpus_build.py:895` calls `Receipt.from_pack_result(...)` and passes no file-shard information. I
added the parameters; the call site must pass them:

```python
file_shard=bundle.file_shard, file_shards=bundle.file_shards,   # ← plus the tuple/int shape fix
```

Without it, K siblings each write a receipt declaring the default `(0,1)`, `verify_bundle_set` sees K
plain duplicate-stream retries, and **E15 is not fixed end-to-end** — the run fails its own
verification after the full ~11 h spend. I must not edit `corpus_build.py` (eng-12's surface).
Documented in `from_pack_result`'s docstring with a ⚠️. Grade: MEASURED-IN-CODE (read the call site).

I deliberately did **not** work around this by parsing the index out of `bundle_id` — eng-11
(`--p03of07`) and eng-12 (`--fs0of3`) disagree on that string, and recovering semantics from a name is
the coupling this codebase refuses.

## 6. 🔴 UPSTREAM MISMATCH — eng-11 nested vs eng-12 flat (ENG-EXEC is resolving in favour of nested)

- eng-11's contract: plan entry carries **nested** `"file_shard": {"index": 0, "of": 1}`, mandatory.
- eng-12's shipped code: `Bundle.from_plan_entry` reads **flat** `int(entry.get("file_shard", 0))`.

`int({"index":0,"of":1})` **raises `TypeError`** — fail-closed, a crash, not a silent mis-read — but it
crashes `bundles_of()`, the funnel every command uses. Grade: MEASURED-IN-CODE.

**My module accepts BOTH shapes** (`_parse_file_shard`), so the receipt is immune to however this
resolves. That is four lines against a resume that reads every sibling as unsharded. ENG-EXEC says it
is resolving to eng-11's nested dict at merge; my `to_dict` **emits** nested, matching eng-11 and the
document's other multi-field blocks (`stream`, `pack`, `build`).

## 7. THE SCHEMA DECISION — **no v3. Purely additive under v2.**

`RECEIPT_SCHEMA_VERSION` stays `edullm-corpus-receipt/v2`; `READABLE_RECEIPT_SCHEMAS` stays `{v1, v2}`.

**Justification.** `Receipt` already carries three defaulted-optional siblings (`unfilled`, `filter`,
`keep`); `from_dict` reads every field with a default. A bump would be **actively harmful**:
`verify_receipt` SHORT-CIRCUITS on an unrecognised `schema_version` (`:738-749`) and
`corpus_build.bundle_is_done` reads receipts to decide what to skip — so a v3 that any deployed reader
did not know would make every receipt in S3 unverifiable and every completed bundle look unbuilt,
silently mandating a full rebuild. That is Wave 0 eng-06's finding, and it argues against bumping here.

**Why this addition does not need one, where `filter` did.** v2 exists because absent `filter` changed
*meaning* (v1: "no slot existed"; v2: "the producer chose not to record"). Absent `file_shard` means
the same thing under every version — *the whole stream, not file-sharded* — which is a **true**
statement about every receipt ever written. There is no interpretation to version.

**Two guards, per eng-06's precedent.** Absent parses as the **default** `(0,1)`, never as zeros that
look like data. And `to_dict` **omits** the key when `of == 1`: `receipt_sha256` is documented as an
idempotency key a resumed driver may compare, so emitting `{"index":0,"of":1}` unconditionally would
change the canonical bytes of every receipt already in S3 and make a resumed build see a digest
mismatch on bit-identical work. Mutation 4 measures this.

## 8. `bundle_id_for` — **FIXED**, not documented

It derived the id from `(plan_id, stream)` only, so K parts got one id (eng-11 measured
`8d16efcd49721e9b`). `receipt_key` keys on the id, so K parts sharing one would overwrite each other's
receipt in S3 — one object where K should be — after which `bundle_is_done` declares the K-1 never-run
children **DONE**, because the surviving receipt's shards really are all present at the right size.

**Not live today** — `run_bundle` always passes `bundle_id` explicitly, so the defaulting branch is
never taken on the build path. Grade: MEASURED-IN-CODE. Fixed anyway: "safe only because every current
caller happens to pass the argument" is the property that quietly stops holding, and it stops holding
into silent data loss rather than an exception.

**The unsharded id is byte-identical to before** (`if file_shards > 1` guards the suffix), so every
receipt key already in S3 and every completed bundle's resume state survive. An unconditional suffix
would have renamed every receipt in the bucket — the same blast radius as an unguarded schema bump.
Both numbers go into the material: part 1-of-3 and part 1-of-7 read different files and own different
ordinal ranges, so they are different work.

## 9. Numbers, graded

| value | grade | source |
|---|---|---|
| baseline **1338** tests | MEASURED-IN-CODE | `pytest -q --collect-only`, this worktree at `d593db1` |
| **1372** tests after (+34) | MEASURED-IN-CODE | `pytest -q`, `5184f44` |
| E15 = **1** violation on one stream | MEASURED-IN-CODE | my own repro, no `s3=` |
| E15 = **8** violations at plan scale | MEASURED-IN-CODE | eng-11's, independently reproduced here at plan shape (38 receipts, 8 file-sharded streams) |
| `bundle-set-shard-path-collision` does **not** fire on a correct family | MEASURED-IN-CODE | proves ordinals disjoint; only the grouping was confused |
| mutation kill counts 4 / 2 / 4 / 2 / 2 | MEASURED-IN-CODE | §4 |
| `int(dict)` raises `TypeError` | MEASURED-IN-CODE | Python semantics + read both artifacts |
| eng-11's `8d16efcd49721e9b` colliding id | UNVERIFIED (relayed) | I did not re-derive it; the *collision* is MEASURED-IN-CODE via mutation 5 |
| 51.38 h → ~11 h, the 40 h lever | UNVERIFIED (inherited) | eng-12's makespan simulation; not my measurement |
| K=100 for DCLM | CARD (registry) | `d593db1`; tested as a param, not verified against the registry |

## 10. Decisions needed from ENG-EXEC

1. **Assign the `corpus_build.py:895` wiring line** (§5). Without it E15 is not fixed end-to-end and
   the 40 h saving stays unrealisable. **This is the only remaining blocker on the receipt side.**
2. **Confirm the nested plan shape resolution** (§6). My module is immune either way; `corpus_build.py`
   is not.
3. Note for whoever merges: `_assert_file_shard_family` (eng-12, `corpus_build.py`) and
   `_check_set_file_shard_families` (mine) are **deliberately independent** — one validates the plan
   from the plan's refs, the other validates the receipts from the receipts' paths. The failure being
   hunted is a receipt set that does not match a *correct* plan, so they must not share a code path.
   Do not "de-duplicate" them at merge.

## 11. Constraints honoured

No S3 writes, no Batch submits, no `manifest.json`, no `git push`. No edits to `corpus_build.py`,
`corpus.py`, or `corpus_read.py` — `git diff --stat` is exactly
`corpus_receipt.py` (+410/−17) and `test_corpus_receipt.py` (+431). No Maple config read or cited.
`ruff check` clean on both files.
