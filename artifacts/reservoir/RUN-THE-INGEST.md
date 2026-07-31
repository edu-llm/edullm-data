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
| job def | `edullm-reservoir-ingest:3` — bootstraps `0.6.1`, 2 vCPU / 8 GB / 7200 s |
| queue | `sbsandbox-intern-edullm-cpu` |
| lifecycle | `expire-ingest-30d` on `_ingest/` — the driver refuses to start without it |

## The run

**Sizing is limited by requests-per-IP, not by CPU.** 16 concurrent workers produced 8/20 failures
at HTTP 429. Start at **10 shards × 4 workers = 40 concurrent** and only widen if a wave reports
zero 429s. `ids` prints a running 429 count.

```bash
# One wave of 10 shards. ~25 min per child, well inside the 7200 s timeout.
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
| `HTTP Error 429` | per-IP rate limit | **lower** shard count or workers; never raise |
| `ImportError: cannot import name …` | job def bootstraps an older wheel | re-register against the current version |
| `no enabled Expiration lifecycle rule` | `expire-ingest-30d` missing | deploy `infra/07-landing-ingest-lifecycle.json` (merge, don't replace) |
| `AccessDenied … GetLifecycleConfiguration` | policy predates the guard | re-apply `infra/08-reservoir-ingest-policy.json` |
| `shard parts are missing` | a child failed | re-run those indices, then merge again |

Each of the first four has actually happened, in that order.

## The preflight the job def runs before touching anything

```
assert edullm_data.__version__ == '0.6.1'
assert _backoff_delay(3) == 32.0            # catches a linear-backoff regression
assert 97-item 4-way split round-trips      # catches a lossy shard split
```

These cost milliseconds and both guard a defect that already shipped once.
