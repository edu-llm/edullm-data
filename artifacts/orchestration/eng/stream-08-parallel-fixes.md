# eng-08 — parallel fixes (B3 / B1 / B2)

Branch: `agent/eng-08/parallel-fixes` @ base `f5a4017`
Worktree: `/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/eng-08--parallel-fixes`
Started: 2026-08-08

## Scope
- **B3 / #10** — thread the five Gate A profile checks in `profiles/pretrain_tokens_v1.py`;
  raise `max_pool_connections` at `s3.py:196` FIRST; optionally drop the redundant HEAD.
- **B1 / #23** — pin `tokenizers` in `pyproject.toml`.
- **B2** — boundary-marker prefix guard in `corpus_pack.py:131` + its length-1 test.
- **B4 / #16 — STRUCK by CEO ruling (no-op; key exists in no registry row). NOT implemented.**

## Status — ALL THREE LANDED, 4 commits, **1227 passing** (baseline 1214, +13)
- [x] **B3 / #10** — `3545fe7` + `d3be62a`
- [x] **B1 / #23** — `2f14465`
- [x] **B2** — `7f2cf84`
- [x] B4 — STRUCK, not implemented (correct: the key exists in no registry row)

Not pushed. No S3 write, no Batch submission, no job-def registration, no `manifest.json` written.

## Log
(append-only; every material finding lands here immediately)

### Baseline, MEASURED 2026-08-08 in this worktree
`python3 -m pytest -q` at `f5a4017` → **1214 passed, 14 warnings in 58.89s**. Matches ENG-EXEC.

### Ground truth read before touching anything (all MEASURED-IN-CODE)

| fact | location |
|---|---|
| `Boto3S3.default()` builds `boto3.client("s3", region_name=…)` with **no `Config`** | `s3.py:192-196` |
| botocore default pool = **10** (recomputed live: `botocore.config.Config().max_pool_connections`) | botocore 1.43.56 in this env |
| the size-sweep threading pattern to copy | `validate.py:_prefetch_heads`, `:536-579` (pool at `:577`) |
| **the CLI ALREADY sizes the pool** when `max(head_workers, promote_workers) > 8` | `validate.py:2476-2492` |
| profile checks: **zero** threading | `profiles/pretrain_tokens_v1.py` (whole file, 503 lines) |
| per-object profile I/O | 1 HEAD (`_observed_size`, `:222`, cached) + 4 `get_range` (`_sampled_ids`, `:240`) + 1 `get_range` (npy sniff, `:438`) + `check_seq_len_alignment` = cache hit |
| `GroupContext` has no worker knob | `profiles/base.py:69-79` |
| `_BOUNDARY_MARKER_REWRITES` | `corpus_pack.py:128`, one entry; guard at `:141` |
| the length-1 test that must move with B2 | `tests/test_corpus_pack.py:1209-1213` |
| `tokenizers` imported at | `corpus_build.py:631` (function-local), declared nowhere |
| declared deps | `pyproject.toml:27-41` — exactly `boto3`, `numpy<2.5` |
| installed here | tokenizers **0.22.2**, boto3/botocore 1.43.56, numpy 2.4.4 |

### ⚠️ CORRECTION TO MY OWN BRIEF, filed before implementing (B3 step 1)

**Brief claim:** "`s3.py:196` builds `boto3.client("s3")` with **no `Config` at all**, so it inherits
botocore's default of 10 … this must come first or step 2 underdelivers."

**Evidence:** the first half is TRUE (`s3.py:192-196`). The consequence is **already mitigated on the
Gate A path**: `validate.py:2476-2492` (landed before this branch) builds its own
`boto3.client(config=Config(max_pool_connections=want_pool + 2))` whenever
`max(--head-workers, --promote-workers) > 8`, and falls back to `Boto3S3.default()` otherwise.
So **rev 14's `--head-workers 16` already gets an 18-connection pool.**

**Numbers it moves:** step 1's marginal speedup on the Gate A CLI path is **zero**, not the
"step 2 underdelivers" the plan implies. The whole B3 win is step 2.

**What is genuinely missing, and what I am fixing instead:** the *S3 layer itself* has no knob, so
every other entry point — `fsck.py:275`, `ingest_prm800k.py:1042`, and any future threaded caller —
gets a hardcoded 10, and the `Config` construction is **duplicated** in `validate.py:2488` and
`corpus_build.py:590`. I am adding the parameter to `Boto3S3.default()` and routing `validate.main`
through it (`corpus_build.py` is **not mine** — its duplicate stays, flagged below).

**Blast radius:** additive keyword-only parameter with a `None` default; the unsized path builds the
identical client it does today.

### B3 — MEASURED speedup (not the plan's estimate)

Harness: the real `CHECKS` over a 100-object fake corpus, per-call `time.sleep(10 ms)` standing in
for S3 latency (a sleep releases the GIL exactly as a socket wait does). Run in this worktree
2026-08-08.

| `check_workers` | wall-clock | round trips | calls/object | speedup |
|---|---|---|---|---|
| **1** (today) | **8.27 s** | 600 | 6.00 | 1.00× |
| 4 | 2.13 s | 600 | 6.00 | 3.89× |
| 8 | 1.11 s | 600 | 6.00 | 7.44× |
| **16** | **0.60 s** | 600 | 6.00 | **13.77×** |
| 32 | 0.34 s | 600 | 6.00 | 24.14× |

**Grade: MEASURED** for the ratio under a fixed synthetic latency; **DERIVED** for anything it
implies about real Gate A wall-clock (real S3 latency varies and one shared TCP/TLS pool is not
modelled). **The round-trip count is identical at every worker count** — the concurrency moves
waiting, not work.

**Do not restate the plan's "5.63 h → 21 min" as measured.** That is 16.1×, above my measured 13.77×
at the same worker count, and it also assumes threading covers all 8 calls when validate's own head
loop is a separate knob. My honest projection at 16 workers is a **DERIVED ~10–14×** on the profile
portion.

### ⚠️ The per-object call count is 6 in the profile, not 7 — the plan's step 3 is ALREADY DONE

**MEASURED-IN-CODE above:** `calls/obj = 6.00` exactly, for all of `CHECKS`. That is 1 HEAD
(`_observed_size`) + 4 decode windows + 1 npy sniff. Adding `validate`'s own per-entry HEAD gives
**7**, which is exactly the plan's own post-fix measurement (70,343 ÷ 10,049 = 7.00). So §8.3 step 3
("8 calls → 7") describes work that landed before this branch; the brief says as much
("already partly done and MEASURED"). **There is no second 8→7 saving to collect.**

### Worker count at 4 vCPU — asked for by the brief

**MEASURED-IN-CODE:** the profile-check work per object is one `np.unique` + a few
`count_nonzero` over a 16,384-element array — microseconds against a ~50 ms round trip.
**MEASURED live** (recorded at `pretrain_tokens_v1.py:205-210`): Gate A ran at **0.3% CPU**.

So the container's `vcpus: 4` is **not** the binding constraint; concurrency here is bounded by the
connection pool and by S3 itself, and 4 vCPU is ample for a latency-bound fan-out. My measurement
still scales cleanly at 32 on a 10-core laptop. **Recommendation: 16, which is what rev 14 already
passes** — my measured curve is still ~86% efficient there (13.77× of 16), while 32 buys 1.75× more
for 2× the sockets and 2× the risk of S3 503 slow-downs on one prefix. Memory is the other reason
not to go higher blindly, and it is handled: the prefetch is batched at `4 × workers` objects, so
peak sample bytes is `64 KB × 4 × workers` ≈ **4 MB at 16 workers** — against a whole-manifest warm
of 40,001 objects, which would have been **2.6 GB** of an 8,192 MB container.

### Does B3 clear rev 14's 4.00 h timeout? — DERIVED, and it INDEPENDENTLY CONFIRMS eng-04

Computed this session from the plan's **MEASURED 507.5 ms/object** at 8 calls/object ⇒ **63.4 ms per
round trip**, then re-apportioned over the **7** calls that actually remain (1 in validate's head
loop, **6 in the profile** — the 6 is MEASURED-IN-CODE by my spy this session), and scaled by my
MEASURED efficiency at 16 workers (13.77/16 = 86%).

| 40,001 objects (1.0T at `SHARD_TOKENS`) | wall-clock | vs 14,400 s |
|---|---|---|
| serial, 8 calls/object | 5.64 h | 141% over |
| serial, 7 calls/object (post-HEAD-cache) | 4.93 h | 123% over |
| **rev 14 TODAY** — `--head-workers 16`, profile serial | **4.28 h** | **7% OVER — FAILS** |
| **with B3, both at 16** | **0.36 h** | **9% of budget** |
| with B3, both at 32 | 0.20 h | 5% of budget |

**This reproduces eng-04's 5.64 h / 4.98 h from a different apportionment**, which is worth more
than either figure alone — we started from the same measured 507.5 ms but split the calls
differently and landed in the same place. The residual gap (my 4.28 vs their 4.98) is that I credit
the HEAD-cache fix that is already in the tree; theirs is the 8-call figure.

**Verdict: B3 is sufficient.** Gate A goes from ~7% over the timeout to ~9% of it, a **~12×**
margin. AUDIT's promote figure of 3.3–5.9 h then becomes the binding term (0.36 + 3.32 = **3.68 h**
against 4.00 h — it fits, but with only 8% of headroom, and 0.36 + 5.9 does **not**). **B3 does not
solve promote; it just stops Gate A being the reason the job dies.**

⚠️ **Grade discipline:** everything in that table is **DERIVED**. The only MEASURED inputs are
507.5 ms/object (the plan's, live) and my 13.77× efficiency (synthetic latency). Real S3 tail
latency, one shared TLS pool, and 503 slow-downs on a hot prefix are all unmodelled and all push the
same direction. Treat 0.36 h as an order of magnitude, not a promise.

### What I did NOT do, and why

- **Step 3 of §8.3 (drop the redundant HEAD).** Already landed; my spy measures **6.00 calls/object**
  in the profile, and 6 + validate's own HEAD = 7 = the plan's own post-fix measurement
  (70,343 ÷ 10,049). No saving left to collect.
- **B7 (`ChecksumSHA256` on the shard sink).** eng-06's, per D1. Not touched.
- **`corpus_build.py`.** Not touched at all — eng-04/06/07 are in it. Read-only, as instructed.
  Its `_s3(max_pool_connections=…)` duplicate of the Config construction is now redundant with
  `Boto3S3.default`; **flagged, not changed** — a one-line cleanup for whoever owns that file next.

### B1 — a SECOND undeclared dependency, found by the test rather than by the task

`pyarrow` is imported by `corpus_read.py` and `ingest_reservoir.py` and was declared nowhere either.
The code had already noticed and nobody acted: **`corpus_read.py:404` reads "production resolved
pyarrow 25.0.0 unpinned (`pyproject.toml:29`)"** — citing a line that declares *numpy*. This is the
package that **SEGFAULTED the live array job**. Bounded `>=24,<26` (24.0.0 is what this suite runs,
25.0.0 is what production resolved).

`tokenizers` bounded `>=0.21,<0.23`. Floor excludes 0.20.x's Unicode-14 tables (upstream WONTFIX);
ceiling excludes 0.23.1, the current PyPI head, which this suite has never run. **Stated as
reproducibility, NOT as a claim that 0.21–0.22 are byte-identical — nobody has run that
differential.** That is a finding, not a gap in the work.

The test **recomputes** the import set from every module's AST **at any scope**. Function-local
imports count: `corpus_build.py:631` imports inside a function, so a module-level scan would have
called the package clean — which is precisely how the gap survived.

### B2 — fixed the guard (not the "comment why it must stay at one entry" option)

`_boundary_marker_guard(table)` = longest common prefix of every literal, `lru_cache`d on the table.
One entry → the whole literal; several sharing a prefix → that prefix (the old `"<|"` exactly);
several sharing nothing → `""`, which disables the fast path and runs every rewrite. Slower, never
wrong. **A future addition can no longer be a silent no-op.**

- **Cheap path preserved, MEASURED head-to-head** (best of 5 × 500,000 calls, ~880-char clean doc):
  old **0.311 µs/call** → new **0.382 µs/call** = **+0.071 µs** = **24 s** over 340M documents
  against a 9.96 h floor = **0.07%**. And the derived guard is *longer* than `"<|"` in the shipping
  case, so the fast path is now strictly **more** selective.
- **Idempotency preserved**, asserted for the multi-entry tables too, since resume depends on it.
- **The `len(...) == 1` test is GONE.** It states the policy without testing it: it passes for a
  one-entry table that rewrites the wrong thing, and *fails* for a correct two-entry table — so
  whoever legitimately extended the table would simply delete it.
- **VERIFIED BY MUTATION:** restoring the hardcoded `"<|"` makes the new test fail on the non-`<|`
  table with the exact silent-no-op symptom. Per ENG-EXEC's warning, a green suite is not evidence a
  fixture still bites — this one was checked by breaking the code on purpose.

### A claim of mine the test refuted, corrected in place

I wrote the duplicate-entry test expecting **parity** between serial and threaded. It failed: the
**serial** path re-reads a duplicate's decode windows (**20** calls vs threaded **15**), because only
the size HEAD was ever cached and payload windows never were. The honest claim is a **bound**
(threading must never cost *more*), not equality. Closing the serial gap needs a manifest-lifetime
payload cache = the 2.6 GB hazard the batching exists to avoid, for a saving that only appears on
duplicated entries. Deliberately not done.

### Test-integrity note (ENG-EXEC's warning, applied)

I replaced `test_the_cli_sizes_the_http_pool_to_the_worker_count`'s **source-substring grep**
(`assert "max(args.head_workers, args.promote_workers)" in src`) with one that **runs `main()` and
records what it asked the client for**. The grep version would have passed unchanged while
`--check-workers` sized the pool to a quarter of the concurrency actually in flight — exactly the
decoupling ENG-EXEC described, in the file I was editing.

### Files changed (11 files, +947 / −47)

`pyproject.toml` · `src/edullm_data/{corpus_pack,s3,validate}.py` ·
`src/edullm_data/profiles/{base,pretrain_tokens_v1}.py` ·
`tests/{test_corpus_pack,test_declared_dependencies,test_profiles_pretrain_tokens,test_s3_pool,test_validate}.py`

⚠️ **Two files outside my declared four**, both unavoidable and both uncontested:
`profiles/base.py` (the `check_workers` field the profile reads) and `validate.py` (the CLI flag and
the call chain that sets it). **No other stream is in either**, verified against
`artifacts/orchestration/eng/*.md` before editing. `s3.py` change is the **pool config only** — I
did not touch the put path, so **no collision with eng-06's B7**.

### Decisions needed from ENG-EXEC / CEO

1. **`edullm-validator` needs a rev 15 to pass `--check-workers`** — or nothing changes in
   production. It defaults to `--head-workers`, so **rev 14's existing `--head-workers 16` picks the
   fan-out up with no flag change**; a rev 15 is only needed to set it *independently*. Recommend
   leaving rev 14's flags alone and shipping the image.
2. **Promote, not Gate A, is now the timeout risk** (AUDIT's 3.3–5.9 h vs a 4.00 h shared budget).
   Outside my stream; flagging because B3 moves the constraint rather than removing it.
3. **`corpus_build._s3`'s Config duplicate is now redundant.** One-line cleanup for that file's
   owner; I did not touch it.
4. **`pyarrow>=24,<26` was my call, not the plan's** — the plan only asked for `tokenizers`. Revert
   the pyarrow line if a wider range is wanted; the test will then fail and needs the exemption list
   updated with a reason.

