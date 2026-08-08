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

**Re-judging all 2,000 documents with a corrected prompt.** The first run's score was invalid — see
"the prompt bug" below. Check with:

```bash
wc -l artifacts/smoke/judges.jsonl        # target 2000
tail -2 /tmp/phase0/judge_v2.log
```

If it died partway, just re-run — `judge.py` resumes from whatever is already in `judges.jsonl` and
only counts a row as done if at least one judge produced a label:

```bash
python3 artifacts/smoke/judge.py --workers 8
```

### Already finished

**Candidate-D classification: DONE.** Job `07d5c64b-6b6d-4b5d-b7e8-1a6537248b7e` succeeded;
`artifacts/smoke/d_labels.jsonl` holds all 2,000 labels, 100% parsed, 0 abstains. **Do not re-run
it** — D's labels are unaffected by the prompt bug (the bug was in the *judges'* prompt), and the
labels are already on disk. Source of truth in S3:
`s3://sbsandbox-intern-edullm-outputs/teams/data-prep/runs/smoke-classify-d/out/d_labels.jsonl`.

Measured throughput: **10.8 doc/s** on one A10G. That number drives `COST-RECHECK.md`, which is the
most decision-relevant artifact in this directory.

**dclm harvest: ABANDONED, deliberately.** Not slow — *hung*. A parquet row-group read blocks past 2
minutes where FineMath takes 2.2 s, reproduced standalone (footers and file metadata read fine at
0.1 s, so it is the column read specifically). Combined with `/statistics` HTTP 501 and a conversion
truncated 3,869×, DCLM-baseline is inaccessible on every path tried.

**The gate has four rows, not five, and that is fine** — but D's accuracy on diverse unfiltered web
is unmeasured, and that is the category least like the other four. `PLAN-CORRECTIONS.md` §10.

## ⚠️ The prompt bug — do not repeat it, and do not trust `judges-v1-bad-prompt.jsonl`

The first full run scored **49.1% pooled and FAILED all four sources.** That was **my prompt's fault,
not the model's.** I labelled FDC category 0 as "General works", copying the essential-web card's
abbreviation. Dewey class 0 is "**Computer science, information** & general works" — and computing
lives at `005.x`, *inside* class 0. So D correctly emitted `005.1` → 0 for programming documents while
the judges, given no computing category, sent them to 6 (Technology).

Remapping only D's `00x` codes to 6 moves qa-forum from **3.3% → 95.7%** and the pool from
**49.1% → 81.4%**. 92% of D's `0` labels were this one collision.

**The lesson, if you take one thing from this file:** it was not caught by the score looking low — a
low score is exactly what a failing candidate looks like, and "D FAILS at 49.1%" would have been
believed. It was caught by reading `raw_all`, where `label=0 <- '005.1,skip'` is visibly a programming
document filed under a category I had mislabelled. **Read the raw model output before believing any
aggregate.**

`judge.py:FDC_L1` now spells out every category's real scope. The old labels are kept at
`artifacts/smoke/judges-v1-bad-prompt.jsonl` for audit, not for use.

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
