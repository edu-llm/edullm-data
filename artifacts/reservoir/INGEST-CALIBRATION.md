# Ingest throughput, measured — and why the full pass cannot run as one job

2026-07-31. Job `reservoir-ingest-calibrate-20b` (`edullm-reservoir-ingest:1`), 4 vCPU / 16 GB,
16 workers, 20 files per config.

## The measurement

```
INGEST_START                 t = 1785533828.689
faq: 1,346,000 distinct ids  t = 1785533873.646
                             ------------------
20 files in 45.0 s  =  0.44 files/s at 16 workers
```

## What that means for all 27,104 files

| workers | projected wall clock | fits a 7200 s job? |
|---|---|---|
| 16 (as calibrated) | **16.9 h** | ✗ 8.5× over |
| 32 | 8.5 h | ✗ 4.2× over |
| 64 | 4.2 h | ✗ 2.1× over |
| 128 | 2.1 h | ✗ still over, and past the queue's 128 vCPU cap |

**The 7200 s job-definition timeout is 2.00 h.** Even assuming perfectly linear scaling — which
network-bound work does not deliver, and the compute environment caps at 128 vCPU on one
`c7i.8xlarge` type — a single job cannot finish the pass.

This is the same wall the 218-shard olmo publish hit (`CLAUDE.md` gotcha 4), reached from the other
direction: that one was single-threaded and slow; this one is parallel and simply has 27,104 units
of work.

## Why it is slower per file than the local `plan` run suggested

`plan` read **one** file per config and reported ~2.5 s for the tree listing. The per-file cost is
dominated by reading every row group's `id` column over HTTP Range against ~200 MB parquet files —
that is real network work, roughly 2.3 s per file per worker, and it does not shrink with tuning.

The scan is already minimal: footers plus one column, never the payload. There is no cheaper read of
the same information.

## The options, and what each costs

**1. Array job, sharded by file range — recommended.** Batch array jobs run N children with
`AWS_BATCH_JOB_ARRAY_INDEX` set. Give each child a slice of the file list, have it write
`_ids/part-<NNNNN>.u64`, and add a small reduce step that concatenates and `np.unique`s the parts.
20 children × ~1,355 files ≈ 51 min each, comfortably inside the timeout, and a failed child is
re-runnable without redoing the other 19. Needs: a `--shard i --of n` flag on the `ids` subcommand
and a `merge` subcommand. Roughly an hour of work.

**2. Raise the timeout to 24 h and run one job.** One `register-job-definition` call. But a single
16.9 h job with `attempts: 1` loses everything on any transient failure — and this workload makes
~27,104 HTTP requests to an external host, so a transient failure is likely rather than
hypothetical. The driver's `--tolerate-errors` flag mitigates but does not fix it.

**3. Four jobs, one per config.** ~4.2 h each, under a raised-but-modest timeout, no code change at
all — the `ids` subcommand already loops configs, so this needs only a `--config` flag. Least work
that fits; coarser retry granularity than option 1.

## Recommendation

**Option 1** for the real run. The reason is not speed — it is that a 16.9 h job with no
checkpointing is one network blip away from starting over, and this pass has a build-time deadline
(§9.7 item 4) that makes a lost day expensive.

Option 3 is a reasonable fallback if the array plumbing turns out to be more than an hour.

---

# ⚠️ THE JOB ALSO FAILED, AND THE FAILURE INVERTS THE SIZING ABOVE

The run this file was written from **exited 2**. It completed `faq` and `math`, then:

```
error: 8 of 20 table files failed and --tolerate-errors was not set.
First: table/000_00000_13.parquet: HTTP Error 429: Too Many Requests
```

**The per-IP Hugging Face rate limit again** — the same one that stalled Phase 0
(`PLAN-CORRECTIONS.md` §6), now reached from Batch at 16 workers.

## Two consequences

**1. I had reintroduced a bug this repo already documented.** The Range reader's retry was
`3*(n+1)` seconds over five attempts — it gives up after 30 s, and the 429 window outlasts that.
§6 records the identical defect in `recount.py`: *"a 3 s linear retry that could never outlast the
limit."* Fixed in `0.6.1`: exponential 4 s → 120 s cap over 8 attempts, honouring a numeric
`Retry-After`.

**2. The "20 children, all at once" plan above was wrong, and backwards.** Sharding across N
machines multiplies the request rate against a limit that does not care how many machines you have.
More children make the 429s *worse*. The throughput table above is therefore an upper bound that
cannot be reached by adding parallelism — the binding constraint is requests per IP, not CPU.

## What replaced it

- A process-wide `_RateGate`: a 429 in any worker pauses **every** worker until a shared deadline.
  A thread that backs off privately while its siblings hammer has changed nothing.
- Default `--workers` 16 → **8**.
- The array runs **10 shards × 4 workers = 40 concurrent requests, in waves** — not 20 × 32 = 640.

Revised sizing, same 2.25 worker-seconds/file:

| shape | per child | concurrent requests | verdict |
|---|---|---|---|
| 10 × 4 | 25.4 min | 40 | ✅ start here |
| 20 × 4 | 12.7 min | 80 | ⚠️ only if 40 shows no 429s |
| 20 × 8 | 6.4 min | 160 | ✗ 10× what already failed |

Each child is far inside the 7200 s timeout even at 10 × 4, so there is no reason to push
concurrency for its own sake. **If a wave reports 429s, lower the shard count — never raise it.**
`ids` now prints a running 429 count and says exactly that on failure.

## The lesson worth keeping

The calibration measured the right number (0.44 files/s) and I drew the wrong conclusion from it —
"too slow, add parallelism" — because the timeout was the visible constraint and the rate limit was
not yet. A throughput measurement that does not also record *what limited it* invites exactly that
error. The 429 counter now travels with the throughput number.

## What the calibration also proved

Beyond the number, the run end-to-end validated the whole chain: `WHEEL_VERSION=0.6.0`,
`PARTITION_OK=1`, the lifecycle guard passing, ids scanned from the real corpus, and a `.u64` id set
written to `s3://edullm-landing/_ingest/reservoir-dolma2/_ids/`. The plumbing works; only the
scheduling shape is wrong.
