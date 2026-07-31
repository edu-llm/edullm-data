# The array-ingest segfault: pyarrow's `pre_buffer` default

**Status: MECHANISM IDENTIFIED, FIX APPLIED, CONFIRMATION PENDING.**
`edullm-reservoir-ingest` array children die with exit 139, and the evidence points at
`pq.ParquetFile(rf)` taking pyarrow's default `pre_buffer=True`, which dispatches a Python file
object's range reads to **Arrow's native C++ IO thread pool**. The fix is one keyword:
`pq.ParquetFile(rf, pre_buffer=False)`.

The A/B is 3/4 crashes unpatched vs 0/4 patched — strong, but one unpatched child survived on the
slow path, so a speed confound is not yet excluded. See the warning under "The A/B" before
treating this as closed. A 2×8 replication is in flight.

## The one-line answer

```python
pf = pq.ParquetFile(rf, pre_buffer=False)   # ingest_reservoir.py, _scan_ids
```

`pre_buffer` is **not written at the call site**, so the crash was caused by a default nobody had
read. That is also why it was so hard to see: there is no line to suspect.

## The A/B that proves it

Two 4-child arrays, submitted together, on the same queue, same image, same wheel (0.6.2), same
pyarrow (25.0.0) / numpy (2.4.6), same shard/worker/file counts. The **only** difference is a shim
that forces `pre_buffer=False`. Job def `edullm-reservoir-shim`, 2026-07-31:

| job | patch | children: crashed / total |
|---|---|---|
| `resshim-J-noop` | none (unpatched control) | **3 of 4** exit 139 |
| `resshim-K-prebuf` | `pre_buffer=False` only | **0 of 4** — all completed all four configs |

The `noop` arm matters: it runs the same wrapper script with **no patch applied**, which rules out
"the wrapper masked it." Unpatched-in-a-wrapper still crashes; patched does not.

### ⚠️ The one `noop` survivor, and the confound it exposes

**`resshim-J-noop` child 1 SURVIVED**, so this is 3/4 vs 0/4, not a clean separation. That child
spent **383 s in `math` with 40 rate-limit pauses**; the three that died were on the fast path
(child 0 crashed 58 s into `math`). This is the same "slow runs survive" pattern that made every
early instrumented run pass, and it means **arrival rate is a live confound in this A/B**:

- If `pre_buffer=False` merely made the `prebuf` children *slower*, they could have survived for
  the survivor's reason rather than the fix's.
- Against that: the `prebuf` children were **not** uniformly slow. Child 3 finished all four
  configs in **95 s** — faster than the crashing `noop` children lived — and children 0/1/2 logged
  **52 / 40 / 31** pauses, straddling the survivor's 40. So `prebuf` includes both fast and heavily
  throttled children and **none** crashed, while `noop` crashed on every fast child.

That is suggestive but **not conclusive at n=4 per arm**. A 2×8 replication
(`resshim-L-noop8` / `resshim-M-prebuf8`) is running to settle it. Until it reports, treat the
mechanism below as **strongly supported, not proven**, and read the fix as "the best-evidenced
change we have" rather than a closed case.

Independently reproduced on the deployed job def itself (`edullm-reservoir-ingest:6`, unmodified):
`resdiag-F-plainarray` → children 0, 1, 2 exit 139; `resdiag-G-faulthandler` → all 4 exit 139.

## Why the native IO pool is fatal here

Arrow sizes its IO pool from the **host** CPU count, not the cgroup. Logged live from inside a
2-vCPU container on a `c7i.8xlarge`:

```
DIAG cpu_count=32 affinity=32 arrow_cpu=32 arrow_io=8
```

So with `pre_buffer=True`, native threads call `_RangeFile.read()` — and each call runs a full
`urlopen`: TLS handshake, a 302 redirect chain, a socket read, and under the array's per-IP 429
storm, a multi-second `time.sleep` backoff. All of that executes inside a C++ thread-pool
callback. Measured on pyarrow 25.0.0 with a spy file object:

```
pre_buffer=True : read(NATIVE)=30, seek(NATIVE)=30, read(pool)=2
pre_buffer=False: read(pool)=32,   seek(pool)=34,   read(NATIVE)=0
```

The faulthandler dump from a crashing child shows exactly this: threads whose Python stack
**bottoms out at `ingest_reservoir.py line 305 in read` with no Python caller beneath it** — a
thread that entered the interpreter from C.

```
Thread 0x00007f0c09f616c0 (most recent call first):
  File ".../ssl.py", line 1103 in read
  File ".../socket.py", line 720 in readinto
  File ".../ingest_reservoir.py", line 305 in read      <-- no caller: entered from C++
```

## Why it looked like "faq works, then math dies"

It never was about `math`. Reversing the config order (`resdiag-B-reversed`, `math,faq`) completed
both. `math`-only (`resdiag-D-mathonly`) completed. The two configs' parquet geometry is
near-identical (measured: ~270 MB, 66–78 row groups, SNAPPY, same encodings, id chunks ~0.04 MB).

The second config is simply **when the fleet's 429s begin**. N children hammering one per-IP rate
limit take ~20 s to trip it, which is about one config. So the boundary was a clock, not a cause.

## What was refuted, and how (do not re-test)

| # | hypothesis | test | verdict |
|---|---|---|---|
| 1 | short HTTP read | read loop shipped in 0.6.2 | ✗ still crashed |
| 2 | co-scheduled host contention | shards read disjoint files | ✗ nothing shared |
| 3 | cgroup memory | 8→16 GB | ✗ **worse**; RSS peaked at **149 MB of 8192 MB** |
| 4 | numpy/pyarrow ABI skew | pinned numpy 2.4.6 | ✗ identical crash |
| 5 | the config *transition* | two configs back-to-back, locally and on Batch | ✗ passes |
| 6 | `math` specifically / "the second config" | reversed order; math-only | ✗ both pass |
| 7 | thread-pool teardown across configs | `--workers 1` (`resdiag-C-w1`) | ✗ passes |
| 8 | **a data race on `_RangeFile.pos`** | instrumented per-object concurrency | ✗ see below |
| 9 | an over-read (200-with-whole-body) | injected a whole-file response | ✗ raises cleanly |
| 10 | exception raised out of an Arrow callback | injected `IngestError` at depth | ✗ propagates cleanly |

**Hypothesis 8 deserves its own note, because it is the plausible wrong answer.** The native
threads in the dump look exactly like a data race on the unlocked `self.pos`. It is not:

- per-object concurrency instrumentation measured **max 1**, **0 overlapping entries**
  (`DIAG2_WORST_CONCURRENCY=0`, both locally and on Batch);
- adding a per-object `RLock` did **not** change the outcome (`resdiag2-I-lock`);
- Arrow serialises calls into a single Python file object — it exposes
  `ReadAt(position, nbytes, void* out)` and holds the pairing internally.

So **do not "fix" this with a lock.** The problem is the native callback context, not concurrent
mutation of `pos`.

Hypotheses 9 and 10 are worth keeping as *non*-causes: pyarrow raises `OSError: File too short` on
both a short and an oversized buffer, so neither can produce a silent crash. A buffer of the wrong
bytes at the right length would be the dangerous case — and that is the one the measurements in 8
rule out.

## A trap in reproducing this

**Instrumentation hides it.** Every early wrapper (`-X faulthandler` + a sampling thread + a
per-file trace) *survived*, which nearly produced a second wrong answer. Those runs were also
**slower** — 270–447 s in `math` versus ~54 s for a crashing child — because they shared the 429
budget differently. The crash needs the fast path.

That is what the `noop` arm is for. **Any wrapper you add must ship an unpatched control through
the identical wrapper**, or you cannot distinguish "fixed it" from "perturbed it."

Other reproduction requirements, all confirmed:
- **It needs an array.** Four single jobs with identical parameters, co-scheduled on one host, all
  succeeded (`resdiag-A/B/C/D`). One job cannot generate the 429 pressure.
- The unsharded calibration crossing `faq → math` was **not** evidence against a config boundary;
  it was a single job, hence no 429 storm.
- Exit **139** vs **137** remains the right first distinction, and RSS ≤149 MB against an 8 GB cap
  settles it: nothing here is a memory problem.

## The fix, as shipped

`src/edullm_data/ingest_reservoir.py`, `_scan_ids`: `pq.ParquetFile(rf, pre_buffer=False)`, with
the reasoning inline at the call site (an invisible default needs a visible comment).

Two regression tests in `tests/test_ingest_reservoir.py`:
- `test_scan_ids_disables_pyarrow_pre_buffer` — asserts the keyword is present in source. A
  source-level assertion is right here: the failure it guards appears only in production, only
  under an array, and only after the first config.
- `test_range_file_read_returns_exactly_n_bytes` — recomputes the short-read loop against a
  transport that always returns short, so the 0.6.2 fix stays covered too.

Suite: **782 passing**.

## What still has to happen before a production wave

1. **Ship a wheel with the fix and re-register the job def.** `edullm-reservoir-ingest:6`
   bootstraps `edullm_data-0.6.2-py3-none-any.whl` **by exact filename**; publishing a new wheel
   changes nothing until the job def is re-registered. Keep the PEP-427 name.
2. **Re-run the 4-child smoke test on the real job def** (`--limit-files 24`) and require 4/4.
3. Only then widen. The 429 rate limit is unchanged by this fix — it is why `math` is slow, not why
   it crashed. Start at 10 shards × 4 workers and read `n_429` from each `_index.*.json`.

## Diagnostic job definitions left registered

`edullm-reservoir-diag`, `edullm-reservoir-diag2`, `edullm-reservoir-shim` — all write to
`_ingest/reservoir-dolma2/_diag*` / `_shim` and none to `edullm-data`. Deregister them once the
fix is deployed; their prefixes expire with `expire-ingest-30d`.
