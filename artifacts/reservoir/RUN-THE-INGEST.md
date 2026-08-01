# Running the id-partition ingest (§9.7 item 4)

The operational procedure. `INGEST-CALIBRATION.md` explains *why* the shape is what it is; this is
what to type.

## What it produces and why it exists

`s3://edullm-landing/_ingest/reservoir-dolma2/_ids/finephrase-<config>.u64` — one sorted `uint64`
digest set per FinePhrase config. Phase 1 uses these two ways:

1. **The partition**: format `f` keeps only ids where `sha256(id) % 4 == f`, so the four configs stop
   being ~73% the same documents.
2. **The anti-join**: every FinePhrase id is dropped from the FineWeb-Edu draw, so a document cannot
   appear as real edu-web text *and* as its own rephrasing in one 20 B run.

**Build-time deadline.** After tokenization there is no document→id mapping (§9.7 item 3), so this
cannot be retrofitted — redoing it means re-tokenizing the synthetic half.

## Deployed pieces

| piece | name |
|---|---|
| role | `edullm-reservoir-ingest` — cannot write `edullm-data` |
| job def | **`edullm-reservoir-ingest:7`** — bootstraps **`0.6.3`**, 2 vCPU / 8 GB / 7200 s (verified live 2026-08-01; ~~`:3` / `0.6.1`~~ was 4 revisions stale) |
| queue | `sbsandbox-intern-edullm-cpu` |
| lifecycle | `expire-ingest-30d` on `_ingest/` — the driver refuses to start without it |

## The run

~~**Sizing is limited by requests-per-IP, not by CPU.**~~ ⚠️ **Superseded 2026-08-01.** 16 concurrent
workers did produce 8/20 failures at HTTP 429 — but the cause was **our own 70× amplification**
(one metered resolve per range read instead of per file), not a per-IP ceiling. Fixed in `0.6.2`;
the real 4-shard run logged **zero 429 pauses in 67 s**. The conservative shape below is still a
safe way to start and costs nothing, but the *reason* has changed: widen on evidence, and if 429s
appear in volume, check `_cdn_url` before blaming concurrency. `ids` prints a running 429 count.

```bash
# One wave of 10 shards. (The "~25 min per child" this comment used to give came from a 16x unit
# error in INGEST-CALIBRATION.md; the fixed run does all four configs in about a minute.)
aws batch submit-job \
  --job-name reservoir-ingest-w1 \
  --job-queue sbsandbox-intern-edullm-cpu \
  --job-definition edullm-reservoir-ingest \
  --array-properties size=10 \
  --container-overrides '{"environment":[
      {"name":"INGEST_SHARDS","value":"10"},
      {"name":"INGEST_WORKERS","value":"4"},
      {"name":"INGEST_RUN_ID","value":"reservoir-<date>"}]}'
```

`AWS_BATCH_JOB_ARRAY_INDEX` becomes `--shard`, so child *i* takes files `[i::10]` — **striped, not
contiguous**, because FinePhrase files are name-ordered with sizes varying by an order of magnitude
and a contiguous slice can be all-large.

Each child writes `_ids/parts/finephrase-<config>.<shard>-of-<N>.u64`.

### Then merge

```bash
aws batch submit-job \
  --job-name reservoir-ingest-merge \
  --job-queue sbsandbox-intern-edullm-cpu \
  --job-definition edullm-reservoir-ingest \
  --container-overrides '{"command":["sh","-lc","… merge --of 10 --run-id reservoir-<date>"]}'
```

`merge` **refuses an incomplete part set.** That refusal is the point: a missing part yields a
smaller anti-join set, the merge succeeds, the counts look plausible, and edu-web silently keeps
documents that should have been dropped. If it refuses, re-run only the failed array indices — the
striping means a re-run of child *i* reproduces exactly child *i*'s file list.

## Reading the result

```
faq: 1,346,000 distinct ids -> s3://…/finephrase-faq.u64
```

Check `_ids/_merge-summary.json` for `cross_shard_duplicates` (should be ~0 — shards are disjoint by
construction, so a large number means the striping is broken) and each shard's `_index.*.json` for
`n_429` and `keep_fraction_pct`.

The partition must clear its per-format floor: faq/tutorial 10.1%, math 15.8%, **table 17.3%** (the
worst case). Measured on 287,000 real ids it lands at 24.86–25.26%, so there is ~1.44× margin on the
tightest one.

## If it fails

| symptom | cause | fix |
|---|---|---|
| `HTTP Error 429` | the **resolver** quota (metered per *token*, not per IP). In volume, suspect one resolve per *range read* instead of per *file* | confirm `_cdn_url` resolves once and reuses the signed CDN URL (`0.6.2`+); only then lower shard count or workers. Never raise. |
| `ImportError: cannot import name …` | job def bootstraps an older wheel | re-register against the current version |
| `no enabled Expiration lifecycle rule` | `expire-ingest-30d` missing | deploy `infra/07-landing-ingest-lifecycle.json` (merge, don't replace) |
| `AccessDenied … GetLifecycleConfiguration` | policy predates the guard | re-apply `infra/08-reservoir-ingest-policy.json` |
| **exit 139, `Segmentation fault`** | **pyarrow's `pre_buffer=True` default** dispatching range reads onto Arrow's native C++ IO thread pool | `pq.ParquetFile(rf, pre_buffer=False)`, shipped in **`0.6.3`**. If it recurs, that keyword is gone. |
| `shard parts are missing` | a child failed | re-run those indices, then merge again |

⚠️ **The exit-139 row was wrong until 2026-08-01** and would have sent you to the wrong fix. It read:
*"short HTTP read handed to pyarrow — fixed in `0.6.2`; if it recurs, the read loop is gone."* The
short-read fix was **hypothesis 1** of ten and is recorded as **"✗ still crashed"** in
`SEGFAULT-INVESTIGATION.md:101`. The read loop is still worth keeping (it is a real bug, and
`test_range_file_read_returns_exactly_n_bytes` guards it), but it is not what stopped the SIGSEGV.

Every row has actually happened, in that order.

### ⚠️ Exit 139 is not exit 137, and the difference is the whole diagnosis

**137** is SIGKILL — the container hit its memory cap. **139** is SIGSEGV — a crash inside C++.

When three of four array children returned 139, the tempting read was "I halved memory from 16 GB
to 8 GB when sizing the array down, so give it more RAM." That is what 137 would have meant. 139
meant pyarrow segfaulted, and the cause was a **short HTTP read**: `RawIOBase.read` may legally
return fewer bytes than requested, a throttled connection does exactly that, and pyarrow does not
re-request the remainder — it parses a page header at an offset inside the wrong bytes.

Two details that identified it as deterministic rather than flaky: every child died on the config
*after* its first successful one (long enough to get throttled), and shard 0 survived only because
it happened not to be cut. Random memory pressure would not produce that shape.

The job definition now asserts the read loop is present before doing any work.

## The preflight the job def runs before touching anything

```
assert edullm_data.__version__ == '0.6.1'
assert _backoff_delay(3) == 32.0            # catches a linear-backoff regression
assert 97-item 4-way split round-trips      # catches a lossy shard split
```

These cost milliseconds and both guard a defect that already shipped once.
