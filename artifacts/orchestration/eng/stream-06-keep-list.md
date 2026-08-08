# stream-06 — keep-list consumer, ChecksumSHA256, FilterStats receipt

**Agent:** eng-06 | **Branch:** `agent/eng-06/keep-list` | **Started:** 2026-08-08
**Territory:** `run_bundle` (`corpus_build.py:429-570`, incl. `sink` at `:461`) + `corpus_receipt.py`

## Status
- [ ] T1 A2b/#22 — consume eng-05's keep-list in `run_bundle`
- [ ] T2 B7/#29 — declare `ChecksumSHA256` on shard upload
- [ ] T3 — record `FilterStats` in `Receipt`

## Log
(appending as I go)

## Baseline (MEASURED, 2026-08-08, this worktree)
`python3 -m pytest -q` → **1214 passed, 14 warnings in 58.39s** at `f5a4017`. Matches ENG-EXEC's 1214.

## Ground truth I read before touching anything (all MEASURED-IN-CODE)

| fact | location |
|---|---|
| `run_bundle` spans | `corpus_build.py:429-570` (`return {...}` block ends 570) |
| `sink` closure | `:461-464`; `s3.put(bucket, key, payload)` at `:463`, sha256 at `:464` — one line late |
| per-bundle `SeenHashes` created implicitly | `corpus_filter.py:303` (`seen = seen if seen is not None else SeenHashes()`), because `run_bundle:482` passes no `seen=` |
| `Receipt` frozen dataclass | `corpus_receipt.py:222-...`, `schema_version` field at `:275` |
| `RECEIPT_SCHEMA_VERSION` | `corpus_receipt.py:102` = `"edullm-corpus-receipt/v1"`; `READABLE_RECEIPT_SCHEMAS` at `:103`; enforced at `:523` |
| `put_file_verified` (local-path only) | `s3.py:290-345`; `_MULTIPART_COPY_THRESHOLD` guard at `:325` |
| `FakeS3.put_file_verified` mirrors the guard | `s3.py:623-644` |
| `grep duplicates\|contaminated corpus_receipt.py` | **0 hits** — §5.6's claim CONFIRMED |

## Interface I need from eng-05 (A2a producer) — my ASSUMPTION until its file lands

eng-05's `stream-05-hash-prepass.md` at the time I started was a 9-line STARTED stub with no format.
Re-checked twice. So I am coding the consumer against an **explicitly-marked assumption** and an
**adapter boundary**, so a format change is a one-function edit, not a `run_bundle` edit.

**What `run_bundle` actually needs, and it is very little:** a predicate
`keep(doc) -> bool` evaluable per-document with no shared mutable state.

The narrowest thing that satisfies both §5.3's determinism requirement and `run_bundle`'s call shape:

```python
class KeepList(Protocol):
    def keeps(self, doc: Document) -> bool: ...    # immutable, order-independent
```

I will implement `corpus_filter`-side glue ONLY if eng-05 has not; otherwise I adapt to its type.
**My change to `run_bundle` is a new keyword-only `keep_list=None` parameter threaded into
`dedup_and_decontaminate`.** `dedup_and_decontaminate`'s signature is eng-05's territory
(`corpus_filter.py`), so I pass through the existing `seen=` slot if eng-05 keeps it, or the new
`keep_list=` slot if eng-05 introduces one. **Marked ASSUMPTION; reconcile at merge.**

## ⚠️ FINDING F1 — the keep-list CANNOT be a set of content hashes, and this is a real constraint on eng-05

**Grade: DERIVED, from MEASURED-IN-CODE call shape.**

`dedup_and_decontaminate` (`corpus_filter.py:296-314`) consults exactly one method:
`seen.add_if_new(hex_digest) -> bool` (`:305`). It is **stateful by construction** — the `add_` in the
name is load-bearing: two byte-identical documents in one bundle get `True` then `False`.

A keep-list is *immutable* (that is the whole point, §5.3 constraint 2). **A stateless membership test
on the CONTENT HASH therefore cannot dedup**: the same text twice returns `True` twice, and both copies
ship. Exact dedup would silently become a no-op — the failure mode where nothing raises and the corpus
is quietly wrong.

The keep-list must therefore be keyed on the **document**, not the text: `(source, id)`, or the
`(hash, source, ref)` triple's `ref`. §5.3 supports this — *"resolve winners by an explicit source
priority (§5.5), emit a keep-list"* — a *winner* is a chosen document, not a surviving hash.

**Consequence for the split of work:** the swap is NOT a drop-in at the `seen=` slot. `dedup_and_
decontaminate` needs the `Document`, which it has, but its current seam passes only the digest.
**That signature change is eng-05's**, in eng-05's function. My half stays on my side of that seam.

## Design decisions for my three tasks (recorded before coding, so a reviewer can object)

### T1 — what `run_bundle` will and will not do
`run_bundle` already injects `documents`, `tokenizer`, and `index` rather than constructing them
(`corpus_build.py:437-441`). The keep-list follows that established shape: a new **keyword-only
`keep_list=None`** parameter, threaded to `dedup_and_decontaminate`. `run_bundle` does NOT load it,
does not parse eng-05's format, and does not know its partitioning.

Three things `run_bundle` DOES own, and they are the determinism half:
1. **`keep_list=None` is byte-identical to today** — `corpus_filter` default-constructs `SeenHashes`.
   That is what keeps 1214 tests green while eng-05 is mid-flight.
2. **A capability check, fail-fast.** If a keep-list is supplied and the installed
   `dedup_and_decontaminate` has no such keyword, raise `BuildDriverError` naming the mismatch —
   never silently fall back to per-bundle dedup, which would produce a corpus that *looks* globally
   deduped. Same failure class as `load_index`'s refusal (`corpus_filter.py:265-276`).
3. **An immutability probe.** §5.3's determinism argument is that the filter is immutable; nothing
   enforced it. `run_bundle` measures `len(keep_list)` before and after the bundle and refuses if it
   moved. **Necessary, not sufficient** (a swap of two entries is invisible) — stated as such in the
   docstring, not oversold.

### T2 / B7 — `put_bytes_verified`, and why NOT a `checksum=` kwarg on `put`
Mirrors `put_file_verified` (`s3.py:290`) exactly: the method computes the digest ITSELF from the
bytes it is about to send. A `put(..., checksum_sha256=...)` kwarg would let a caller declare the
digest of *different* bytes — decoration that reads as verification.
`put_object` is **always single-part** (only `upload_file` has boto3's 8 MiB `multipart_threshold`),
so the declared `ChecksumSHA256` is always `FULL_OBJECT`, never a composite. Guard at
`_MULTIPART_COPY_THRESHOLD` (`s3.py:157` = 5 GiB) anyway, and **raise rather than degrade**:
a production shard is 25,001,984 × 4 = **100,007,936 bytes = 1.9% of the limit** (DERIVED from
`corpus.py:89`), so the guard can only fire on a misconfiguration, and it fires on shard 1 not 400.

### T3 — schema bump to `edullm-corpus-receipt/v2`, v1 kept readable
`verify_receipt` **short-circuits** on an unknown `schema_version` (`corpus_receipt.py:523-535`), so
bumping without adding v1 to `READABLE_RECEIPT_SCHEMAS` would make **every existing receipt
unverifiable** and break resume. Both go in the set.
**Why bump at all, given an old reader just ignores an extra key:** the version is documented as "an
interpretation of these fields, not of a shape" (`:99-101`). There is a genuine interpretive change —
under v1, an absent filter block means *the schema had no slot*; under v2 it means *the producer chose
not to record*. That distinction is the entire auditability claim, so it earns a version.

---

## ✅ T2 / B7 / #29 — LANDED

**Files:** `src/edullm_data/s3.py` (Protocol `+put_bytes_verified`, `Boto3S3.put_bytes_verified`,
`FakeS3.put_bytes_verified` + `_accept_verified_put` + `declared_checksum`),
`src/edullm_data/corpus_build.py` sink at `:461`.

The sink is now (net change is the two operative lines; the rest is the comment explaining B3-before-B7):
```python
key = _assert_safe_key(f"{root}/{ref.path}")
digest = s3.put_bytes_verified(bucket, key, payload)   # digest computed BEFORE the put, declared to S3
digests[ref.path] = (digest, len(payload))
```

**Tests added (4), all recomputing:**
1. `test_every_shard_upload_declares_a_checksum_recomputed_from_its_own_payload` — reads each
   shard back out of the store, re-derives `b64(sha256(bytes))`, compares to the value the write
   DECLARED. Not "a checksum is present"; a constant fails it.
2. `test_the_receipt_digest_is_the_same_digest_that_was_declared_to_s3` — the receipt's `sha256`
   and the declared checksum are BOTH recomputed from the stored payload. Catches drift in either.
3. `test_a_corrupted_body_is_rejected_by_the_server_and_never_becomes_an_object` — asserts
   `BadDigest` AND that the key is absent afterwards.
4. `test_a_plain_put_declares_nothing_so_the_test_above_cannot_pass_vacuously` — the control.
   Without it, tests 1–2 would pass even if `declared_checksum` returned a value unconditionally.

**One pre-existing test repaired, and it is a finding in itself.**
`test_the_receipt_is_written_only_after_its_shards_verify` (`tests/test_corpus_build.py:267`)
overrode `FakeS3.put` to simulate a dropped upload. After the sink moved to `put_bytes_verified`
that override **dropped nothing** — the fixture no longer reached the code it exists to break, and
pytest reported it green while the assertion it guards (`DID NOT RAISE`) actually failed. Re-pointed
at `put_bytes_verified`, returning the digest the real method would, so the drop stays invisible to
the caller. **Generalisable: any test that intercepts a specific S3 method is coupled to the write
path's choice of method, and a silent decoupling looks exactly like a passing test.**

### ⚠️ B7 is NOT a win yet, and I am not claiming one
- **Critical path moved by 0.00 h.** DERIVED from `IMPLEMENTATION-PLAN.md` §8.3a: B7 permits deleting
  `VD1`, deleting `VD1` exposes `GA1`, and at `GA1`'s unthreaded **5.08 h** the path is **24.90 h**
  either way. B7 pays only once **B3** (eng-08, `pretrain_tokens_v1.py` + `s3.py`) lands. I did not
  do B3 and did not depend on it.
- **I did not retire `verify --deep`.** Job-def change, not code, not mine. And it retires **ONE
  TIER** — the cheap tier keeps `bundle-set-mixed-wheel-versions`, the reservoir's live publish blocker.
- **The live-verification caveat, stated for the record and carried in the docstring + test 3:**
  the 2026-08-01 `BadDigest` evidence (`s3.py:312-320`) covers the **file-shaped** path. The
  bytes-shaped call is the same `put_object` with the same header, so the argument is by identity of
  the API call — **not a second measurement**. **ACTION FOR PHASE 2 SMOKE TEST: assert a deliberate
  digest corruption on the bytes-shaped path against real S3.** I cannot run it (forbidden from S3).

### Numbers
| number | value | grade |
|---|---|---|
| production shard bytes | 25,001,984 × 4 = **100,007,936** | DERIVED from `corpus.py:89` + `DTYPE_SIZE` |
| that as a fraction of the 5 GiB single-PUT limit | **1.86%** | DERIVED |
| critical-path change from B7 alone | **0.00 h** | DERIVED, §8.3a |
| critical-path change from B7 **after** B3 | −1.17 h (not −1.49 h) | plan's own figure, §8.3a |
| `tests/test_corpus_build.py` | 33 → **37 passed** | MEASURED |

---

## ❌ CORRECTION — F1 ABOVE IS WRONG. I retract it.

eng-05's contract landed after I wrote F1 (`stream-05-hash-prepass.md` §1, FROZEN; commit `d1d9c8f`).
**I read its code before writing this**, via `git show d1d9c8f:src/edullm_data/corpus_filter.py`.

**What I got wrong.** F1 argued a keep-list keyed on the content hash *cannot* dedup, because an
immutable membership test returns the same answer for both copies of a duplicated text, so exact
dedup would silently become a no-op. I concluded the key had to be `(source, id)` and that
`dedup_and_decontaminate`'s signature had to change.

**Why it is wrong.** `KeepFilter` (`corpus_filter.py:763` on `d1d9c8f`) splits the two things I had
conflated. The **KeepList is immutable** — the shared artifact pass 1 froze, which is what makes
execution order unable to reach the output. The **used-bitmap is per-instance mutable**, one
`bytearray` constructed in `__post_init__` at `(len+7)//8` bytes. So the second copy of a text finds
its key already marked and returns `False`. Intra-bundle repeats are handled by state that **no
other bundle can observe** — which satisfies §5.3's determinism constraint exactly, while I had
assumed determinism required total statelessness. It does not: it requires no *shared* state.

**Blast radius of my error: zero code.** I had not yet written the T1 wiring. The correct wiring is
smaller than what I planned — `add_if_new(digest: str) -> bool` is duck-type identical to
`SeenHashes`, so `dedup_and_decontaminate` is UNCHANGED and my edit is a `seen=` passthrough.

**What survives from F1:** the general point that a *stateless* content-hash membership test cannot
dedup is still true, and is the reason `KeepFilter` needs its bitmap. It is not a problem with the
design; it is a thing the design already handles. Recording the retraction rather than deleting it,
because the failure mode I described is real and someone "simplifying away" the bitmap would hit it.

## Contract accepted, and the identity it hands me for free

`hits + repeats + misses == filter.seen`, and `hits == filter.kept + filter.contaminated`, and
`repeats + misses == filter.duplicates`. **MEASURED-IN-CODE** from `dedup_and_decontaminate`
(`corpus_filter.py:305-314`): `add_if_new` is called **exactly once per `stats.seen`**, and each call
increments exactly one of the three `KeepFilter` counters, while its return value routes the document
into exactly one of the three `FilterStats` counters.

That is a **cross-check between two independently maintained counter sets** — much stronger than
either block's own internal identity, which a single wrong `+= 1` in one place would satisfy. It goes
into the verifier.

## New obligation accepted: the `keep` block (from eng-05 §4.2 via ENG-EXEC)
`KeepFilter.unused > 0` is the only signal pass 1 and pass 2 saw different inputs, and it is free.
Landing `filter` + `keep` in one schema bump rather than bumping twice.

**THREE denominators, THREE blocks, and I am deliberately shipping only TWO of them:**

| block | `seen`-equivalent counts | in the receipt? |
|---|---|---|
| `filter` | documents entering **dedup** | **YES** (§5.6) |
| `keep` | probes against the **keep-list key space** | **YES** (eng-05 §4.2) |
| `length` | documents **surviving** dedup | **NO — deliberate** |

`length` stays out because it is `corpus_read.FilterStats`, a different pass with a different
denominator, and it is **not** unrecoverable — `run_bundle` already returns it and `_cmd_run` could
print it. The §5.6 argument for persisting is "these numbers exist only in CloudWatch"; that argument
applies to `filter` and `keep`. Adding `length` too would be scope creep into a third denominator in
the same commit, which is the precise shape of the `category_attrition` mistake. **Flagging it as an
open item rather than silently including or silently omitting it.**

---

# ✅ ALL THREE TASKS LANDED — commit `5118308` on `agent/eng-06/keep-list`

**Tests: 1214 baseline → 1244 (eng-05 cherry-picked) → 1273 (mine). 29 new, zero regressions.**

**Commit attribution for ENG-EXEC's merge:**
- `0700b7e` = **eng-05's `d1d9c8f`, cherry-picked by me.** NOT my work. Cherry-picked so the
  integration is genuinely tested rather than assumed — `KeepFilter` does not exist on `f5a4017`.
- `5118308` = **mine, all three tasks.** `corpus_build.py`, `corpus_receipt.py`, `s3.py`,
  `tests/test_corpus_build.py`, `tests/test_corpus_receipt.py`. Zero overlap with eng-05's files.

## ✅ T1 / A2b / #22 — keep-list consumer
`run_bundle` gains keyword-only `keep_list=`, threaded to `dedup_and_decontaminate` as `seen=`.
`corpus_filter.py` **untouched** — eng-05's frozen contract held exactly.

`_keep_filter_for` (new, `corpus_build.py`) guards three silent mis-wirings, each of which produces
a wrong corpus with no error:
1. **another bundle's keep-list** → nearly every doc rejected as won-by-another; an almost-empty
   bundle indistinguishable from a genuinely duplicate-heavy source;
2. **a `KeepFilter` passed where a `KeepList` belongs** → the plausible mistake (same duck type,
   parameter named `keep_list`) that reintroduces shared mutable state and order-dependence;
3. **a keep-list that mutates mid-build** → key-count check. Necessary, not sufficient; the comment
   and the test both say so (it catches append/truncate, not a swap).

⚠️ **Scope limit I found and documented, weaker than it first looks:** `read_keep_list(raw,
bundle_id)` takes the id from its CALLER, defaulting to the placeholder `"keep-list"` — the
`.keep64` payload carries no bundle id. So check 1 catches a caller that reads the right bytes and
labels them wrong; it does NOT catch one that reads the wrong object and labels it confidently.
Only `keeplists.json`'s per-bundle `sha256` binds a `bundle_id` to bytes. **Open item for whoever
writes the pass-1 driver.**

## ✅ T2 / B7 / #29 — see the LANDED section above. No critical-path claim made.

## ✅ T3 — `FilterRecord` + `KeepRecord`, schema `edullm-corpus-receipt/v2`
Bumped, **v1 kept in `READABLE_RECEIPT_SCHEMAS`** — `verify_receipt` short-circuits on an unknown
version (`:523`) and `bundle_is_done` reads receipts, so dropping v1 would make every completed
bundle look unbuilt and silently mandate a full rebuild.

Absent parses as `None`, never zeros, and `to_dict` OMITS rather than emitting null: an all-zero
record is a positive claim ("the filter saw no documents"), absent means "nothing was recorded".
`from_dict` tests `isinstance(..., Mapping)` rather than `.get() or {}`, which would collapse a
legitimately-empty bundle's record to "unrecorded".

`length` stats deliberately NOT in the receipt — third denominator, and unlike the other two it is
not unrecoverable.

## ⚠️⚠️ MY SECOND SELF-CORRECTION, AND IT IS THE MOST IMPORTANT FINDING IN THIS STREAM

**`unused > 0` is NOT a pass-1/pass-2 divergence signal, and treating it as one breaks the build.**

- **(a) Claim + location.** `corpus_filter.py:820` (eng-05, `d1d9c8f`), `KeepFilter.unused`:
  *"⚠️ Non-zero means the two passes disagreed about the input, which is the only signal that the
  staged read changed between them. Surface it; do not swallow it. Zero is the expected value."*
  Restated in eng-05's findings §1.2 and in ENG-EXEC's message to me. I implemented it literally.
- **(b) Evidence — MEASURED 2026-08-08, this branch.** `corpus_pack.pack` **stops as soon as its
  planned shards are full and does not drain the document iterator.** Instrumented the generator:
  **offered 200,015 documents, pulled 50,264 (25.1%)**. `run_bundle` passes `partial_source=True`
  precisely because `_reader_for` over-delivers *by design* (`_CHARS_PER_TOKEN` 6.0 vs a measured
  ~4.4, × `_FILTER_HEADROOM` 1.5). **Zero is therefore NOT the expected value — it is the exception.**
- **(c) Numbers it moves.** Nothing in the plan's budget. It moves a **verifier gate from
  fail-almost-always to correct.** My own two-bundle test tripped it at `unused=1` of 260 keys.
- **(d) Blast radius — this is the serious part.** `run_bundle` calls `verify_receipt` and **raises
  before writing the receipt**. A gate on `unused > 0` fails a bundle at **end-of-run, after its
  full billable read + tokenize + upload**, and the resume path then rebuilds it, forever. That is
  **exactly the `_drain_surplus` bug that killed 25 of 27 bundles in the first array run** — same
  location in the pipeline, same "full billable work then throw it away" shape. Shipping it would
  have re-run that incident at 1.0T scale.
- **What `unused` actually conflates:** (a) the two passes read different inputs — the real alarm;
  (b) pass 2 stopped early having filled its shards — the normal case. **The receipt cannot separate
  them**, because that needs pass 1's `scanned` for this bundle, which lives in `keeplists.json`.
  **The diagnostic comparison that WOULD work: `keep.probes` vs `keeplists.json`'s `scanned` for
  this bundle** — equal probes with unused keys = early stop; fewer probes than scanned = divergence.
  Wiring `keeplists.json` into the verifier is the fix. **NOT in this change — open item.**
- **Replaced with `hits > keys`**, the one direction an early stop cannot produce (each hit consumes
  a distinct key; the used-bitmap prevents a second).

**Recommendation to eng-05 / ENG-EXEC: amend `KeepFilter.unused`'s docstring.** It is the source of
this error and it will produce it again in the next reader. `unused` is still worth RECORDING —
it is in the receipt — it is just not a gate.

## The cross-check I added, which is stronger than either block alone
`dedup_and_decontaminate` calls `add_if_new` exactly once per `stats.seen` (MEASURED-IN-CODE,
`corpus_filter.py:305-314`), so three relations hold across **two independently maintained counter
sets**: `hits+repeats+misses == filter.seen`, `repeats+misses == filter.duplicates`,
`hits == filter.kept + filter.contaminated`. Each block's OWN identity survives a single wrong
`+= 1`; these do not.

## Tests — 29 new, mutation-checked
**Mutation testing (MEASURED), because a test that passes against broken code is decoration:**
| mutation | tests that fail |
|---|---|
| sink reverted to plain `s3.put` | **3** |
| `keep_list` silently ignored (the no-op) | **2** |
| `filter` block never written | **3** |

**Two of my own tests were defective and I fixed them rather than shipping them:**
1. `test_a_keep_list_that_mutates_mid_build_is_caught` built an elaborate mutating fixture and then
   **asserted nothing about it** — passed without invoking the guard once.
2. `test_the_output_does_not_depend_on_which_bundle_ran_first` used the same texts for both bundles,
   which is **degenerate**: global dedup awards every hash to one winner, so the loser wrote nothing
   and there was nothing for an ordering to change (MEASURED: `tiny--train` 150 keys, `two--train`
   **0**). Rebuilt with per-bundle texts plus a contested overlap, and it now asserts both bundles
   win keys before comparing.
3. The parametrized keep cross-check asserted code **membership**; tightened to an **exact set**, so
   one over-broad check cannot stand in for all four.

**One pre-existing test was silently decoupled by my own change** —
`test_the_receipt_is_written_only_after_its_shards_verify` overrode `FakeS3.put`, which the sink no
longer calls, so it dropped nothing and pytest reported it green. Re-pointed at
`put_bytes_verified`. **Generalisable: any test intercepting a specific S3 method is coupled to the
write path's choice of method, and a silent decoupling is indistinguishable from a passing test.**

## Process failure of mine, recorded because it nearly cost the work
I ran `git checkout src/edullm_data/corpus_receipt.py` to revert a deliberate mutation-test edit and
**wiped every uncommitted change in that file** — ~350 lines. Recovered by re-applying from my own
context, verified by re-running the suite (1273). **Two lessons: (a) never `git checkout` a dirty
file to undo a mutation — copy to `/tmp` first, as I correctly did for `corpus_build.py` and
incorrectly failed to do here; (b) commit before mutation-testing, not after.**

## Every number, graded
| number | value | grade |
|---|---|---|
| test baseline | 1214 | MEASURED |
| after eng-05 cherry-pick | 1244 | MEASURED |
| **after my commit** | **1273** | **MEASURED** |
| documents `pack` pulls of 200,015 offered | **50,264 (25.1%)** | **MEASURED 2026-08-08** |
| production shard bytes | 100,007,936 | DERIVED (`corpus.py:89` × `DTYPE_SIZE`) |
| that vs the 5 GiB single-PUT limit | **1.86%** | DERIVED |
| critical-path change from B7 alone | **0.00 h** | DERIVED, §8.3a |
| critical-path change from B7 after B3 | −1.17 h | plan §8.3a |
| degenerate-fixture key split | 150 / **0** | MEASURED |
| cross-bundle dedup today | **none exists** | MEASURED-IN-CODE (eng-05 §2.1, re-verified) |

## Decisions needed
1. **`KeepFilter.unused`'s docstring should be amended** (eng-05's file). It states a gate condition
   that is false in this pipeline and it is what produced my bug. Not mine to edit.
2. **`keeplists.json` → verifier** is the real pass-1/pass-2 divergence check (`keep.probes` vs
   `scanned`). Unowned. Belongs with whoever writes the pass-1 driver.
3. **Keep-list provenance:** `.keep64` carries no bundle id, so my bundle-id check is a label check,
   not a bytes check. Binding via `keeplists.json`'s per-bundle `sha256` is unowned.
4. **Should a receipt with NO filter block eventually block a publish?** I made absence a non-violation
   (every existing receipt predates the block). That is a policy question for the plan, and I
   deliberately did not smuggle it in as a verifier default.
5. **`length` stats in the receipt?** Deliberately omitted. Flagging rather than silently deciding.
6. **Phase 2 smoke test MUST assert a deliberate digest corruption on the BYTES-shaped path.** The
   2026-08-01 `BadDigest` evidence covers the file-shaped path only; mine is the same API call with
   the same header, which is an argument by identity, **not a second measurement**. I cannot run it.
