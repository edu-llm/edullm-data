# Resume state — Phase 0, written 2026-07-31

Read this if the session died. It says exactly what exists, what is running, and the next command to
type. Everything referenced is committed except the harvested corpus text, which is `.gitignore`d and
reproducible.

## Where the hard stop is

**§9.1 has not been reached yet, and it has not been crossed.** No `manifest.json` uploaded to
landing, no bytes in `edullm-data`, no bucket policy touched, no domain classification run at scale.
The ~$595 spend is still gated and still requires a human.

## Done

| task | state | artifact |
|---|---|---|
| **C** infra | ✅ `edullm-validator:7` registered with `timeout: 7200`; bucket-policy v2 reported NOT deployed | `PHASE0-REPORT.md` |
| **B** licenses | ✅ 129 OpenStax books, LibreTexts distribution over all 40,049 rows | `licenses/*.json`, `licenses/SCHEMA.md` |
| **A** re-count | ⚠️ partial — 6 sources measured, rest blocked by a per-IP rate limit | `recount/*.json` |
| **D** harvest | ✅ 4 of 5 sources at 500 docs each (dclm was still running) | `smoke/samples/*.jsonl` (gitignored) |
| **E** judges | ✅ 2,000 labels, 100% parse rate, 0 failures | `smoke/judges.jsonl` (gitignored) |
| **E** candidate D | ⏳ Batch GPU job submitted | job `180e2ff7-4878-46b4-b016-83458749f9e3` |
| **F** sizing | ✅ reconciled against measured counts | `sizing-revised.md` |

## In flight when this was written

**1. Candidate-D classification, AWS Batch GPU.**

```
job id    180e2ff7-4878-46b4-b016-83458749f9e3
queue     sbsandbox-intern-edullm-gpu   (g5.xlarge, 1x A10G 24GB)
writes    s3://sbsandbox-intern-edullm-outputs/teams/data-prep/smoke-out/d_labels.jsonl
```

Check it, then pull the result:

```bash
aws batch describe-jobs --jobs 180e2ff7-4878-46b4-b016-83458749f9e3 \
    --query 'jobs[0].{s:status,r:statusReason}'
aws s3 cp s3://sbsandbox-intern-edullm-outputs/teams/data-prep/smoke-out/d_labels.jsonl \
    artifacts/smoke/d_labels.jsonl
```

If it failed, resubmit with `python3 artifacts/smoke/submit_classify_d.py --submit` (the script is
idempotent and `classify_d.py` resumes from whatever is already in `d_labels.jsonl`). Its logs are in
CloudWatch group `/aws/batch/sbsandbox-intern-edullm-gpu`.

**2. The dclm harvest.** `mlfoundations/dclm-baseline-1.0-parquet` has 27,938 shards, so
`list_repo_files` alone takes minutes. Re-run just that source:

```bash
python3 artifacts/smoke/harvest_parquet.py --n-docs 500 --only dclm \
    --out-dir artifacts/smoke/samples
python3 artifacts/smoke/judge.py --only dclm --workers 6
```

**dclm is not required to reach the gate.** Four sources give the gate table four rows; dclm adds a
fifth. If it keeps failing, score without it and say so — it is the source whose datasets-server
support is broken in every direction (`/statistics` HTTP 501, `partial` conversion).

## The one command that produces the gate table

Once `d_labels.jsonl` exists:

```bash
python3 artifacts/smoke/score.py
```

That writes `artifacts/smoke/results.json` and `spot-check-50.jsonl`, and prints the §9.5 table.
**Then stop.** §9.1: *"This applies whether the gate PASSES or FAILS — passing is not consent."*

## Reproducing the harvest from scratch

Deterministic — `--seed 20260731`, and each source derives its own seed as `seed + i*7919`:

```bash
python3 artifacts/smoke/harvest_parquet.py --n-docs 500 --out-dir artifacts/smoke/samples
```

Samples are gitignored deliberately: they are third-party corpus text, several sources are
share-alike, and they are cheap to regenerate.

## Things that will bite you

1. **datasets-server is rate-limited per-IP, not per-account.** Verified: authenticated and anonymous
   requests both 429 in 0.1s. Do not fan out parallel jobs against it, and do not believe a token
   fixes it. `harvest_parquet.py` and the footer-bytes method avoid it entirely by reading the hub
   CDN instead — prefer those.
2. **HF Inference credits are exhausted (HTTP 402).** Both judges run on **Bedrock**, not HF. Judge A
   `qwen.qwen3-next-80b-a3b`, judge B `qwen.qwen3-32b-v1:0`.
3. **Local torch is broken** (`torchvision::nms does not exist`, an ABI mismatch). `classify_d.py`
   cannot run on this laptop, which is fine — §5.7 puts it on Batch anyway. Do not "fix" the user's
   global Python environment to work around it.
4. **Bedrock needs the isolated credential profile** for threaded access:
   `AWS_CONFIG_FILE=/tmp/olmo150_aws/config AWS_PROFILE=sbsandbox`. `judge.py` sets this itself if the
   file exists. If it is gone, recreate it with `credential_process = sb-aws-creds credential_process
   --profile sbsandbox`.
5. **Disk was at 92%** (34 GiB free). Do not download datasets.

## What a human has to decide, beyond the $595

Found during execution, all in `PLAN-CORRECTIONS.md` — three of these must be settled **before the
first publish**, not before the classification:

1. **`_dedup/clusters.parquet` and `_licenses.parquet` are rejected by Gate A today.** One-line
   `validate.py` change plus a test and fixture.
2. **A per-document join key does not exist** (manifest grain is one shard object). If per-document
   licenses or cluster IDs are wanted, the tokenizer must emit `(shard_path, doc_index)` at build
   time — unrecoverable afterwards.
3. **A `share_alike` selector.** SA covers all of QA/forum, all of reference, 32% of LibreTexts. If
   SA must ever be dropped, QA/forum falls to ~1.4B and fails the 3× bar.
4. **The reference pool cannot reach 14B** without going ~90% share-alike. Recommendation in
   `sizing-revised.md`: split into `reference-sa` ~9B + `reference-pd` ~5B as `source` values.
5. **`github_archive_filtered` is claimed by two categories** and is issue/PR prose, not code.
