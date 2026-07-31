# The array-ingest segfault: four hypotheses, all refuted

**Status: UNRESOLVED.** `edullm-reservoir-ingest` array children die with exit 139 (SIGSEGV) and I
have not found the cause. This file exists so the next person starts from the evidence rather than
from my wrong answers — and so nobody re-tries a hypothesis that is already dead.

The unsharded path works and is the recommended fallback (see the end).

## The failure, invariant across six runs

```
WHEEL_VERSION=0.6.2
PYARROW=25.0.0 NUMPY=2.4.6
PREFLIGHT_OK=1
INGEST_START shard=1 of=4 workers=4
faq: shard 1/4 -> 6 of 24 files
faq: 403,000 distinct ids -> …/parts/finephrase-faq.00001-of-00004.u64
math: shard 1/4 -> 6 of 24 files
Segmentation fault (core dumped)
```

Every single time:
- **`faq` completes and writes its part.**
- **`math` starts and the process dies**, before a single progress line.
- Exit **139** = SIGSEGV. **Not 137**, which is the container memory kill — this is a crash inside
  C++, and the distinction is the first thing to get right.

## What has been refuted, and how

| # | hypothesis | test | verdict |
|---|---|---|---|
| 1 | short HTTP read handed to pyarrow | shipped the read loop in `0.6.2` | ✗ identical failure |
| 2 | contention between co-scheduled children | shards read **disjoint** files (`[0,4,8…]` vs `[1,5,9…]`) | ✗ nothing is shared |
| 3 | cgroup memory limit | 2 vCPU/8 GB → 4 vCPU/16 GB, workers 4 → 2 | ✗ **got worse** — all 4 died, not 3 |
| 4 | numpy/pyarrow ABI skew | pinned `numpy<2.5`; ran with `NUMPY=2.4.6` + live interop check | ✗ identical failure |

Hypothesis 3 deserves note: **more memory made it worse.** That is evidence against any
resource-exhaustion story, and it is why "just give it more RAM" should not be tried again.

Hypothesis 4 looked strongest and was the most instructive failure. The container was running
numpy 2.5.1 where my local venv had 2.4.6 — a real, unnoticed skew. But `pip install numpy==2.5.1`
fails on macOS ("No matching distribution found"), so the local "control" I believed was pinned had
silently used 2.4.6 all along. **A version comparison you did not print is not a control.** The
preflight now logs `PYARROW=` and `NUMPY=` on every run, which is how the skew was finally seen —
and then how it was ruled out.

## What has NOT been tested, in the order I would try it

**1. The config TRANSITION, reproduced locally.** This is the gap. Every local reproduction ran
*one* config and passed — including shard 1's exact file list, 4 threads, pyarrow 25.0.0. The crash
only ever happens at `faq → math`, and that boundary is the one thing never exercised outside Batch.
Between configs the code does: `IdSet.from_digest_chunks(chunks)` → `s3.put_object` → `del chunks`
→ a **new** `ThreadPoolExecutor` for the next config. A local run of two configs back to back is the
cheapest remaining experiment. (I started one; it exceeded a 2-minute foreground limit and I did not
re-run it in the background.)

**2. `faulthandler` inside the container.** `python -X faulthandler` prints the C-level stack on
SIGSEGV. My diagnostic job failed on the documented PEP-427 wheel-filename trap (`w.whl is not a
valid wheel filename`) before reaching it — a trivial fix, and it would likely name the frame
outright. **Do this before hypothesising further.**

**3. Is it `math` specifically, or the second config?** Run `--limit-files 24` with the config order
reversed (math first). If math-first succeeds and *faq* then dies, it is the transition; if math
dies first, it is that config's data. One run, decisive.

**4. Thread-pool reuse across configs.** Each config creates its own executor inside a loop. If a
worker thread outlives its pool while pyarrow state is torn down, that is a classic native crash.
Testing `--workers 1` in the container isolates it.

## What works today

**The unsharded, single-job path.** The calibration run processed `faq` and `math` fine at 16
workers and only stopped at an HTTP 429 (`INGEST-CALIBRATION.md`), which is a rate-limit issue with
a known fix, not a crash. Notably it crossed the same `faq → math` boundary that kills every array
child — so whatever this is, it is **specific to the sharded path or to something the array changed**
(2 vCPU, `--of > 1`, or the `parts/` write path).

That asymmetry is probably the strongest clue in this file and I did not get to exploit it.

## Recommended fallback

Four per-config jobs — `--config faq`, `--config math`, … — at ~4.2 h each. It needs a `--config`
flag (small) and uses the code path that already crossed configs successfully. It sidesteps the
array entirely.

Do **not** run a 10-shard production wave until the array is proven on a smoke test. It has now
failed on four consecutive attempts.
