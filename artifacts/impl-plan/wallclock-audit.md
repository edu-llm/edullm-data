# Wall-clock audit of the 251.2B reservoir build, and a 1.0T projection

> ## ⚠️ SUPERSEDED ON ONE INPUT — the vCPU cap is 384, not 128, so every floor here is 3× too high
>
> **Corrected 2026-08-07** by a direct read of the compute environment
> (`artifacts/impl-plan/cpu-env-verification.md`): `sbsandbox-intern-edullm-cpu` has **`maxvCpus: 384`**,
> EC2 quota **1,152** with 1,060 free, **one** queue targeting it 1:1 with **zero** jobs queued, and
> `c7i.8xlarge` offered in **all 5** of its AZs. **Nothing binds below 384.**
>
> This audit's **R1** takes "128 vCPU" from `INGEST-CALIBRATION.md:60` and grades it
> `MEASURED-ELSEWHERE`. Right grade, wrong source: **`INGEST-CALIBRATION.md` is the file whose own
> retraction banner says not to size anything from its tables**, and its cap figure was never read back from
> the CE. That banner *"keeps"* the 128 while retracting the timeout beside it — so the retraction is what
> lent the number false credibility.
>
> | this audit says | actual |
> |---|---|
> | 128 vCPU cap (R1) | **384** |
> | 6.61 h tokenize floor at 1.0T | **2.21 h** |
> | 7.19 h for the 1.087T encode | **2.40 h** |
>
> **Everything else here stands** — the anchors, the self-inflicted-slowness findings, the fix ranking, and
> its refutation of my read-amplification claim are unaffected, because they are per-vCPU rates and code
> facts rather than capacity claims. **Only figures derived from the cap move.**
>
> ⚠️ **And the correction makes one conclusion WORSE:** a 3× lower aggregate floor with an unchanged
> per-child ceiling (32 vCPU = one instance) leaves the un-splittable DCLM bundle at **4.9×** the floor
> rather than 1.6×. See `docs/IMPLEMENTATION-PLAN.md` §8A.3.
>
> **The lesson is this audit's own, turned on itself:** *a throughput measurement that does not record what
> limited it invites exactly that error.* A capacity figure inherited from a retracted document is not a
> measurement either.

**Written** 2026-08-07. **Scope:** every measured wall-clock number in
`/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/final-dataset/`, a
self-inflicted-slowness audit, a fix ranking, and a 1.0T projection twice over
(as-measured and fixed).

**Labels used throughout, per the grading contract:**

- **MEASURED** — a real run produced it. Cited to `file:line`.
- **DERIVED** — arithmetic from MEASURED inputs. The inputs are shown.
- **PROJECTED** — a model with assumptions. The assumptions are named.
- **RETRACTED** — appears in the repo but has been superseded. What superseded it is named.
- **NEVER MEASURED** — a finding in its own right.

**Method note.** This audit takes `artifacts/reservoir/INGEST-CALIBRATION.md` as its
epistemic model: that file contains a correct measurement (0.44 files/s) plus two
confidently wrong conclusions drawn from it, both later retracted in-place, plus a
self-flagged 16x unit error that propagated to another file. Every projection below is
therefore stated with its binding-constraint hypothesis explicit, because the
calibration file's own failure mode was recording a rate without recording *what
limited it*.

---

## TL;DR

**The answer to "painfully slow for stupid reasons": yes, and it is ~26 h of a ~46–56 h 1.0T
pipeline.** Two things in the whole build are real physical limits — the **128 vCPU**
compute-environment cap and the **~85 MB/s single-TCP-stream** ceiling to S3. Everything else in
the slow column is our own code, our own default, or our own job-def setting.

**The top of the ranking is free.** `verify --deep` at 1.0T is **13.03 h** single-threaded — which
does not merely run slowly, it **exceeds its 4 h timeout and fails**. The fix ships in `0.7.5`
already: **`--hash-workers 8`, 7.82x measured, zero lines of code, one `register-job-definition`**
(and it is already wired at `edullm-reservoir-verify:3`). Same story for the timeouts: 7200 s was
never an AWS limit, it was a value we set, and raising it converts two guaranteed failures into
slow successes for free.

**Four of the top seven fixes were already known and written down.** #1 shipped in code and was
left unwired in the job def. The publish connection-pool throttle was **predicted verbatim in
`infra/10-dataset-publish-jobdef.md:76-78` before the driver shipped with the bug**. The duplicate
HEAD cache was recommended in `pipeline-scale-audit.md` and never done. The linear retry backoff
was fixed on one branch of a `try` and left on the other — **behind a preflight assertion that
structurally cannot see it**. This is the `families/` half-fix pattern, four more times.

**Three things in the repo's own analysis are wrong, and I corrected them** (Q2b, Q2c, and an
off-by-two in the Gate A model). The largest: the celebrated "2.1x over-read baked into two
constants" is a **budget ceiling that is never reached** — the generator chain is lazy and
`partial_source=True` explicitly refuses to drain it. So the recommended `_CHARS_PER_TOKEN` fix
saves **exactly zero bytes**, and the recommended `val_fraction` fix saves **exactly zero** too
because `val_fraction` **cancels out of the read algebraically**. The real over-read is 2.02x and
**43% of all bytes moved is the val split serving 0.39% of the tokens** — fixable only by carving
both splits in one pass.

**The largest evidence gap:** no in-region `publish()` duration has ever been measured, at any
scale, and it is the one stage that must pull 4 TB through a client. **The most likely 2x:** HF CDN
single-stream throughput from a Batch container has never been measured either, and my
reconciliation of the measured 8 h build implies it may be **8.4 MB/s, not the 85 MB/s that every
projection in this repo borrows from an S3 measurement.** One already-mandatory single-bundle job
settles that plus two other wide bands at once.

**One completion blocker that is not a timing issue at all:** the per-bundle dedup set is 155 B/entry
**measured**, and `stackv2-edu` at 1.0T is ~168 M documents ⇒ **~26 GB against a 14 GiB container**
that was correctly sized for 42 M. That build does not run slowly; it OOMs.

---

## Q1 — Every measured wall-clock number in the repo

All paths are relative to
`/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/final-dataset/`.

### 1.1 The ground truth of the corpus itself (MEASURED, from receipts)

| fact | value | source |
|---|---|---|
| bundles | 27 | `artifacts/reservoir/realized-tokens.json:14` |
| shards | 10,049 | `:15` |
| tokens | 251,218,001,920 | `:15` |
| train / val | 250,242,924,544 / 975,077,376 | `:193-194` |
| documents | 308,291,107 | `HANDOFF.md:396` (receipt table) |
| payload bytes | 1,004,979,748,864 (1.005 TB) | `artifacts/reservoir/verify-job.json:31` |
| sealed bytes in `edullm-data` | 1,004,872,007,680 | `infra/DEPLOY.md:826` |

Note the two byte figures differ by 107,741,184 B (0.0107%). `10,049 × 4 × 25,001,984 =
1,004,879,769,... ` does not equal either exactly, because the last shard of a bundle can be
short. **DERIVED:** `251,218,001,920 tokens × 4 B = 1,004,872,007,680 B`, which matches the SEAL
exactly. So `verify-job.json`'s `bytes_rehashed` is 107.7 MB *larger* than the token count implies
— **FINDING (minor): one of the two is wrong and nothing in the repo reconciles them.** The seal
figure is the one derivable from tokens, so treat `bytes_rehashed` as approximate. It moves the
throughput figure by 0.01%, so it changes no conclusion.

### 1.2 The stage-by-stage table

| # | stage | measured value | unit-of-work | hardware / workers | date | grade |
|---|---|---|---|---|---|---|
| M1 | ids/fetch **calibration** | **20 files in 45.0 s = 0.44 files/s** | parquet `id`-column scan over HTTP Range | `edullm-reservoir-ingest:1`, **4 vCPU / 16 GB, 16 workers** | 2026-07-31 | **MEASURED** — `artifacts/reservoir/INGEST-CALIBRATION.md:39-42` |
| M1a | ↳ per-file cost | **~2.3 s/file/worker** | one file | same | 2026-07-31 | **MEASURED** `INGEST-CALIBRATION.md:70` |
| M1b | ↳ "2.25 worker-s/file" | — | — | — | — | **RETRACTED** by the file's own banner at `:140-150`: it is **wall**-s/file; worker-s/file is 36.0. Superseded 16x. Propagated to `RUN-THE-INGEST.md:35` ("~25 min per child"), also retracted in place at `:38-39`. |
| M1c | ↳ 16.9 h / 8.5 h / 4.2 h / 2.1 h projection table | — | — | — | — | **RETRACTED** `INGEST-CALIBRATION.md:47-52` — calibrated against a 429 ceiling that was our own bug. |
| M2 | ids/fetch **after the CDN fix** | **67 s, zero 429 pauses** | all 4 FinePhrase configs | `edullm-reservoir-ingest:7`, wheel `0.6.3`, **2 vCPU / 8 GB** | 2026-08-01 | **MEASURED** `RUN-THE-INGEST.md:35`, `INGEST-CALIBRATION.md:150`, `PLAN-CORRECTIONS.md:256` |
| M2a | ↳ full-fetch estimate | ~1–3 h for ~$1 | 27,104 files | — | 2026-08-01 | **PROJECTED** `INGEST-CALIBRATION.md:25` (assumption: the 67 s 4-shard rate extrapolates; **never run at full scale**) |
| M3 | HF tree listing | **~6,790 files in 7 pages, ~2.5 s** per config; with `expand=1` **26 s/page → ~1 h** | one config's file list | local | 2026-07-31 | **MEASURED** `src/edullm_data/ingest_reservoir.py:497-500` |
| M4 | **tokenize, `encode_batch`** | **10.5 M tok/s across 32 vCPU** (0.328 M/vCPU) | tokens | 32 vCPU, dolma2-tokenizer, real prose | ≤2026-08-04 | **MEASURED-IN-CODE** `src/edullm_data/corpus_pack.py:247` |
| M4a | ↳ single-document `encode` | **1.10 M tok/s** | tokens | 1 core | same | **MEASURED-IN-CODE** `corpus_pack.py:247` |
| M5 | **the whole 251.2B build** | **~8 h at 12 concurrent containers** (pulled in from ~19 h at ~6) | 27 bundles | Batch, 8 vCPU / 14 GiB per child | 2026-08-02→04 | **MEASURED** `HANDOFF.md:1869-1871` |
| M5a | ↳ DERIVED container-hours | `8 h × 12 = ` **96 container-h** = **0.382 container-h per B tokens** | | | | **DERIVED** from M5; used as the anchor in `artifacts/impl-plan/dedup-decontam-audit.md:1001-1003` |
| M5b | ↳ per-shard pack time | **29.6 s/shard uncontended, ~43 s at 12-way** | one 25M-token shard | 8 vCPU child | 2026-08-04 | **MEASURED** `HANDOFF.md:1485-1486` |
| M5c | ↳ worst-bundle resident memory | **~12.1 GB** (10.3 dedup set + 0.45 decon + 0.4 tokenizer + 0.1 shard + 0.5 pyarrow + interp) | `stackv2-edu--train` | | 2026-08-04 | **MEASURED** `HANDOFF.md:1876-1880` |
| M5d | ↳ dedup set cost | **155 B/entry**, not the 113 B claimed | one document hash | | 2026-08-04 | **MEASURED** `HANDOFF.md:1465-1469` |
| M5e | ↳ per-bundle ETAs given during the run | 9.1 h→2.5, 1–2 h→8, a spurious 33 h | | | 2026-08-04 | **RETRACTED, all of them** `HANDOFF.md:1482-1489`. The author's own verdict: *"every per-bundle ETA I gave was wrong, in both directions."* **Do not reuse any per-bundle figure from that window.** |
| M6 | **`publish()` off-region (laptop)** | **2.7 GiB in 61 min = 0.8 MiB/s ⇒ 9-day ETA** for 587 GiB | stream-hash | MacBook | 2026-07-30 | **MEASURED** `HANDOFF.md:1718` |
| M6a | ↳ server-side `copy_object` from a laptop | **586.6 GiB in 498 s** (= ~1,265 MB/s equivalent, zero bytes through the laptop) | 218-shard copy | MacBook | 2026-07-30 | **MEASURED** `HANDOFF.md:1720-1721` |
| M6b | ↳ 125 GB / 218-shard publish on Batch | **TIMED OUT at 3600 s**, single-threaded ~48 MB/s, 31 of 32 vCPU idle | | 32 vCPU | ≤2026-07-30 | **MEASURED** `HANDOFF.md:1814-1816` |
| M6c | ↳ marker-only backfill vs re-promote | a full re-`promote()` wastes **~20 min** re-copying 218 shards | | | | **MEASURED** `HANDOFF.md:1829` |
| M7 | **Gate A, the 10,049-object run** | **~85 min at 0.3% CPU, 0.1 MB/s, 15.8 round trips/s** | 10,049 manifest entries | `edullm-validator:1x`, **4 vCPU** | 2026-08-05 | **MEASURED-IN-CODE** `src/edullm_data/profiles/pretrain_tokens_v1.py:205-210`, and in the commit message of `9195098` |
| M7a | ↳ per-object serial cost | **507.5 ms/object** (`85×60/10,049`) | | | | **DERIVED** from M7 |
| M7b | ↳ round-trip count before the HEAD-cache fix | **80,392**; after **70,343** (−12%) | | | 2026-08-05 | **MEASURED** (call-counting spy) commit `db437b6` |
| M7c | ↳ **the promotion that was SIGKILLed** | **7200 s reached at 6,324 of 10,051 objects copied** | Gate A + promote | 4 vCPU | 2026-08-05 | **MEASURED** `infra/DEPLOY.md:820-821` |
| M7d | ↳ Gate A on the **6,900-shard** olmo-150b corpus | **~55 min at 8.4 range-reads/s** (~27,600 reads), ±25% run-to-run | 6,913 objects | | 2026-07-30 | **MEASURED** `HANDOFF.md:866-872` |
| M7e | ↳ threading gain claimed at 16 head workers | **~12%, not half** | | | 2026-08-05 | **MEASURED** `infra/DEPLOY.md:822`, commit `db437b6` — this **corrects** an earlier "bigger win than threading" claim, retracted in the commit body |
| M8 | **`verify --deep`, the ONLY payload re-hash** | **3.27 h for 1,004,979,748,864 B = 87.8 MB/s sustained, SINGLE-THREADED, 1 of 16 vCPU** | 10,049 shards | `edullm-reservoir-verify:1`, **16 vCPU**, 4 h timeout | 2026-08-05 04:11:20Z → 07:27:42Z | **MEASURED** `artifacts/reservoir/verify-job.json:16,28-34` |
| M8a | ↳ duration recomputed from the timestamps | **11,782 s = 3.2728 h**; implied rate **85.3 MB/s**, not 87.8 | | | | **DERIVED** — I computed this myself from `:28-29`. The 87.8 figure implies 11,446 s, 336 s short. Both round to "3.27 h / ~86 MB/s"; the discrepancy is container startup + the 27 receipt GETs. **Use 85.3 MB/s for a wall-clock projection and 87.8 for a pure-stream projection.** |
| M8b | ↳ `--hash-workers` speedup | **7.82x at 8 workers** ⇒ the same run is ~25 min | | | ≤2026-08-05 | **MEASURED** `artifacts/reservoir/PUBLISH-SPEC.md:167-168`. **Where it was measured is not recorded** — see Q5. |
| M9 | **promote()** | *"~2 S3 round-trips per object"*; a 6,913-object corpus is ~13,800 serial calls, which **overran the 60-min limit** | objects | | | **MEASURED-IN-CODE** `validate.py:1943-1948` (cited in `artifacts/impl-plan/pipeline-scale-audit.md:645-647`) |
| M10 | GPU classify (`EAI-Distill-0.5b`) | **10.1–10.6 doc/s** steady over 2,000 docs, 0 abstains | 256-token prefixes | 1 × `g5.xlarge` (A10G 24 GB), fp16, batch 16 | 2026-07-31 | **MEASURED** `artifacts/COST-RECHECK.md:15-17`, `artifacts/RESUME.md:50` |
| M10a | ↳ the 3,080 GPU-h / 128-day / $920-spot table | | | | | **MEASURED-input, PROJECTED-output, and flagged by its own author as an "optimistic FLOOR"** `COST-RECHECK.md:34-51`: the smoke test ran 1,024-char prefixes against an 11,010-char mean full document, a **10.8x** gap. Also the 112 M document count is undocumented and *"plausibly an order of magnitude above."* |
| M11 | a full-bucket LIST of landing | **21,005 objects / 4.1 s** | | | ≤2026-08-01 | **MEASURED** `HANDOFF.md:857` |
| M12 | the training smoke run (consumer proof) | **19.3 min**, 150 steps, 3 checkpoints, inside a 3600 s attempt | | 4 vCPU / 1 GPU | 2026-08-01 15:09:30Z→15:28:50Z | **MEASURED** `HANDOFF.md:346-348` |
| M13 | olmo-150b `.npy` migration copy | 6,921 objects / 630,140,294,600 B, **21:24–21:53 UTC = ~29 min** | server-side copy | | 2026-07-29 | **MEASURED** `HANDOFF.md:805`. **DERIVED rate: 630.1 GB / 1,740 s = 362 MB/s** server-side. |
| M14 | recount per-config runtime | **~2–8 min per config** | | | 2026-07-31 | **MEASURED** `artifacts/recount/README.md:21` |
| M15 | DCLM-baseline parquet column read | **hangs >2 min** where FineMath takes **2.2 s**; footer 0.1 s | one row group | local | 2026-07-31 | **MEASURED** `artifacts/PHASE0-REPORT.md:178`, `PLAN-CORRECTIONS.md:129` |
| M16 | a full re-hash of the 758 GB olmo corpus | ~16.5 min / ~$0.18 | | | | **PROJECTED**, `HANDOFF.md:2085` — and **inconsistent with M8**: 758 GB at M8's 85.3 MB/s single-stream is **2.47 h**, not 16.5 min. 16.5 min implies ~765 MB/s, i.e. ~9-way parallelism that did not exist when written. **Treat M16 as RETRACTED-BY-M8.** |

### 1.3 What has NEVER been measured (findings in their own right)

1. **The full ids/fetch pass at 27,104 files.** Only 20 files (M1) and 4 configs in 67 s (M2). The
   ~1–3 h figure everyone now quotes is an extrapolation from a 67-second run.
2. **HF CDN single-stream throughput from inside a Batch container.** `corpus_build.py:901-904`
   says so outright: `_reader_for` is *"UNVERIFIED against live HF from inside a Batch container —
   every offline test injects `documents=`."* Every download projection in this repo rests on the
   **S3** figure (M8) standing in for the **HF CDN**.
3. **Gate A above 10,049 objects.** `HANDOFF-FINAL-DATASET.md:249` flags it: *"Gate A's cost at
   ~20,000 objects (my own extrapolation from a measured 85 min at 10,049, never tested)."*
4. **`--hash-workers` at 8 against real S3 at TB scale.** The 7.82x (M8b) has no recorded venue.
5. **`publish()` in-region, at any scale.** M6 is off-region; M6b timed out. The reservoir was
   published — but no duration for it is recorded anywhere in this repo. **That is a gap in the
   ground truth for the single stage that must move 1 TB twice.**
6. **`fsck` at any scale.** No timing exists.


---

## Q2 — THE SELF-INFLICTED-SLOWNESS AUDIT

Verdict up front, and it is the answer to the owner's framing:

> **Of the reservoir build's ~36 h of serial pipeline cost, roughly 26 h is self-inflicted and
> fixable with constants and flags. Only two things in the whole pipeline are real physical
> limits: the 128-vCPU compute-environment cap, and the ~85–88 MB/s single-TCP-stream ceiling to
> S3 in-region. Everything else in the "slow" column is our own code, our own default, or our
> own job-definition setting.**

The calibration file's 70x is the largest single instance, and it is already fixed. What follows
is the rest, which is not.

### 2.0 The two things that ARE real

| # | limit | value | why it is real | grade |
|---|---|---|---|---|
| R1 | compute-environment vCPU cap | **128 vCPU on one `c7i.8xlarge` type** | An AWS-side quota on the CE. `INGEST-CALIBRATION.md:60` says so and its retraction banner explicitly *keeps* this one ("The 128 vCPU compute-environment cap *is* real") while retracting the timeout beside it. Also `update-compute-environment` **refuses to scale down** (`HANDOFF.md:1479-1480`), so it is not an elastic dial. | **MEASURED-ELSEWHERE** |
| R2 | single-TCP-stream S3 read | **85.3–87.8 MB/s** | M8. One `get_object` body streamed in 8 MiB chunks (`s3.py:257`). This is ordinary per-connection S3 throughput; the fix is more connections, not a faster one. | **MEASURED** |
| R3 | per-round-trip S3 latency | **~85 ms effective** (15.8 rt/s serial) | M7. Also real — but it is *latency*, and latency is exactly what concurrency hides. It is real *per request*, not real *per corpus*. | **MEASURED** |

Note what is **NOT** on this list, and used to be:

- ❌ **"requests per IP"** — RETRACTED. It was our own 70x amplification
  (`INGEST-CALIBRATION.md:10-18`, `ingest_reservoir.py:313-357`). The CDN data plane is
  **unmetered**, verified by `curl -I` (`PLAN-CORRECTIONS.md:248`).
- ❌ **"the 7200 s Batch timeout"** — RETRACTED. *"7200 s was a value we set"*
  (`INGEST-CALIBRATION.md:19-22`). AWS publishes no maximum. It has since been raised to 14400 s
  on `edullm-validator:14` (`infra/DEPLOY.md:809`) — **which proves it was a dial all along.**
- ❌ **"tokenization is slow"** — the 10.5 M tok/s figure (M4) is already the *fast* path; the
  slow path was a **second full encode** in `corpus_read.filter_documents` at 1.10 M tok/s, i.e.
  ~91% of the build's compute on 1 of 32 cores. **Already fixed** by moving the length filter
  inside `tokenize_documents` (`corpus_pack.py:243-249`). Recording it because it is the same
  shape as everything below.

---

### 2.1 SERIAL CODE ON A MULTI-vCPU BOX

I grepped `ThreadPoolExecutor|concurrent.futures|max_workers` across `src/edullm_data/`. Hits
exist in exactly five files: `corpus_receipt.py:866`, `ingest_reservoir.py:796,890`,
`publish.py:470,734,1023`, `validate.py:567,2070,2099`. **`corpus_read.py`, `corpus_build.py`,
`corpus_pack.py`, `corpus_filter.py`, `s3.py`, `fsck.py`, and `read.py` contain ZERO threading.**

| # | site | what loops serially | job def vCPU | vCPU wasted | measured / derived cost | grade |
|---|---|---|---|---|---|---|
| **S1** | `corpus_receipt.py:711-819` `_check_objects` + `:1023-1024` `verify_bundle_set` | `verify --deep` — one HEAD then one full `hash_object` per shard, receipts looped serially | **16** (`edullm-reservoir-verify:1`) | **15 of 16** | **3.27 h MEASURED** (M8); the job's own comment says *"the job def's 16 vCPU buys nothing"* (`verify-job.json:15`) | **MEASURED** — **PARTIALLY FIXED** in `0.7.5` (`796d8a6`), but `edullm-reservoir-verify:1` **did not pass the flag**; fixed operationally at **rev 3** (`infra/DEPLOY.md:810`, `HASH_WORKERS=8`) |
| **S2** | `profiles/pretrain_tokens_v1.py:240` (4 ranged GETs), `:438` (the `.npy` sniff), `:220` (the cached HEAD) | Gate A's **profile checks** — 5 of the 6 network calls per object, all serial, inside plain `for raw, entry, bad in _entries(ctx)` loops at `:280`, `:432`, `:462` | **4** at measurement time (`9195098` commit body: *"0.3% CPU on a 4-vCPU box"*); the deployed validator is now 16 | **~15 of 16** | **~85 min MEASURED** for 10,049 objects (M7), of which `--head-workers` addresses **1 call in 6** ⇒ **12%, MEASURED** (`infra/DEPLOY.md:822`) | **MEASURED. NOT FIXED.** This is the single largest un-fixed serial site. |
| **S3** | `fsck.py:112-137` `_check_objects_present`, and `:243-261` the dataset loop | Gate B's weekly sweep — one HEAD per object, no pool anywhere, no `--workers` flag exists | not recorded | all but 1 | **NEVER MEASURED.** DERIVED at R3's 85 ms: 10,049 objects = **14.2 min**; at 40,000 = **56.7 min**; across the whole catalog (7 corpora, ~25,000 objects today) ≈ 35 min | **DERIVED. NOT FIXED.** Lowest priority — it is weekly and metadata-only — but it is the same defect and it will cross an hour. |
| **S4** | `corpus_build.py:419-425` `bundle_is_done` | Resume check — re-HEADs **every shard** of **every** already-done bundle, serially, before any work starts | 8 (build child) | 7 of 8 | DERIVED: at 85 ms/HEAD a fully-done 27-bundle plan costs `10,049 × 0.085 = 854 s = 14.2 min` **of pure resume overhead**; `pipeline-scale-audit.md:206` estimates *"~12 s per skipped bundle"* at ~1,200 shards/bundle | **DERIVED. NOT FIXED.** Cheap in absolute terms — but at 1.0T / 40,000 shards this is **57 min** per re-run, and the whole point of a re-run is that it is cheap. |
| **S5** | `publish.py:1044-1050` (manifest PUTs), `:477-478`, `read.py:691,748` | Small control-object loops. **Correctly serial** — one object per group, not per shard. | — | — | negligible | **not a defect** |

**S1 and S2 are the same omission** and the repo says so: *"`verify --deep` had it; so does Gate
A. Same omission, same guarantee"* (commit `9195098` subject line). The fix pattern is also
already written down twice — a **pure prefetch function** that threads the I/O and returns facts,
leaving the decision loop byte-for-byte serial (`validate.py:544-546`,
`corpus_receipt.py:716-727`). S2 needs the same treatment and has not received it.

---

### 2.2 CONNECTION-POOL STARVATION — and the fix is a HALF-FIX

`s3.py:192-196`:

```python
@classmethod
def default(cls, region: str = "us-east-1") -> "Boto3S3":
    import boto3
    return cls(boto3.client("s3", region_name=region))
```

No `botocore.config.Config`, so `max_pool_connections` is botocore's default **10**. The failure
mode is documented in three places in this repo and is **silent**: botocore does not pass
`block=True` to urllib3, so past 10 in-flight requests urllib3's `_put_conn` **discards** the
surplus connection and logs `"Connection pool is full"` — workers 11..N pay a fresh TLS handshake
per object, with no error anywhere (`validate.py:2476-2482`, `corpus_build.py:574-580`,
`corpus_receipt.py:505-512`, `infra/10-dataset-publish-jobdef.md:72-78`).

| site | worker count | pool sized? | verdict |
|---|---|---|---|
| `validate.main` (Gate A + promote) | `--head-workers 16 --promote-workers 16` | ✅ **yes** — `validate.py:2483-2490`, `Config(max_pool_connections=want_pool+2)`, but **only when `want_pool > 8`** | **FIXED** (commit `3b11d7d`, deployed at `validator:14`) |
| `corpus_build._cmd_verify` | `--hash-workers 8` | ✅ **yes** — `corpus_build.py:734`, `max(10, hash_workers+4)` | **FIXED** |
| **`artifacts/reservoir/publish_driver.py:155`** | **`hash_workers=16, copy_workers=16`** (`:120-121`) | ❌ **NO** — it calls `Boto3S3.default()` | ⚠️ **BROKEN, AND THIS IS THE ONE THAT MATTERS** |
| `fsck.main:275` | 1 | n/a | fine today; breaks the moment S3 is threaded |
| `ingest_prm800k.py:1042` | ? | ❌ no | check before scaling that path |

> **⚠️ F-POOL — the highest-value single-line fix in this audit.**
> **The reservoir's publish ran with `hash_workers=16` against a 10-connection pool.**
> `publish_driver.py:155` is `publish(SOURCE, s3=Boto3S3.default(), ...)` and `:120-121` sets
> both worker counts to 16. `infra/10-dataset-publish-jobdef.md:76-78` predicts this exact case
> verbatim: *"a publish driver calling `Boto3S3.default()` directly with `hash_workers=16` is
> subject to the same ceiling."* **Nobody fixed the driver.**
>
> **Cost, DERIVED.** 6 of 16 workers run over churned connections. A TLS handshake to S3 in-region
> is ~2 RTT ≈ 3–5 ms; the useful work per object is a 100 MB stream at R2 ≈ 1.17 s, so the
> handshake itself is <0.5% — **the handshake is NOT the cost.** The real cost is that
> `urllib3` also *caps concurrency at the pool size for connection REUSE*, so effective
> steady-state parallelism is **~10, not 16** ⇒ **a 1.6x throttle on the most expensive stage in
> the pipeline.** At 1.0T (4.02 TB to hash) that is `4.02e12 / (85.3e6 × 10) = 1.31 h` instead of
> `0.82 h` — **~0.5 h thrown away**, and at 2 TB per stage across two stages it recurs.
> **Grade: DERIVED (the 1.6x), from MEASURED inputs (R2, the pool default of 10 verified in
> three docstrings, and the driver's literal `hash_workers=16`).** It has **never been measured
> directly** — see Q5.
>
> **THE PROPER FIX IS NOT THE DRIVER.** Give `Boto3S3.default()` a `max_pool_connections`
> parameter, or better, make it size itself. Three call sites already work around the same
> default with three near-identical blocks of code and three near-identical 6-line docstrings
> explaining the same urllib3 behaviour. That is the "families/ half-fix" pattern this repo has
> already paid for once: **fixed in the validator, left in the producer.**

---

### 2.3 REDUNDANT NETWORK CALLS

| # | duplication | count on the reservoir | status | grade |
|---|---|---|---|---|
| **D1** | `_sampled_ids` and `check_seq_len_alignment` each HEADed the same key for the same fact, on top of `_validate_group`'s per-entry HEAD ⇒ **3 HEADs per entry** | ~30,000 HEADs to learn 10,049 sizes | ✅ **FIXED** `db437b6` (`_observed_size`, `pretrain_tokens_v1.py:201-223`). Round trips **80,392 → 70,343, −12% MEASURED** by a call-counting spy. | **MEASURED** |
| **D2** | ⚠️ **`_prefetch_heads` and `_observed_size` STILL maintain SEPARATE caches.** `validate.py:703` builds a local `heads` dict; `pretrain_tokens_v1.py:220` builds `ctx.observations["object_sizes"]`. Grep confirms `object_sizes` is written in **exactly one place** and `validate.py` never seeds it. **Every object is HEADed twice per run.** | **10,049 wholly redundant HEADs** | ❌ **NOT FIXED** | **CONFIRMED BY CODE READ + grep.** The count is **DERIVED** (one HEAD per unique path in each cache). |
| **D3** | Is a separate HEAD needed at all? **No — a ranged GET returns `Content-Range: bytes a-b/TOTAL`.** `_sampled_ids` already issues 4 ranged GETs and `check_first_bytes_not_npy` a 5th; any one of them could yield the size for free. But `s3.get_range` (`s3.py:217-224`) **discards the response metadata** and returns only `bytes`. | up to **20,098 HEADs removable** (D2's 10,049 + D1's surviving 10,049) | ❌ **NOT FIXED, and not noticed anywhere in the repo** | **CODE READ.** The `\x93NUMPY` sniff at `:438` reads bytes 0–7 unconditionally and would carry `/TOTAL` on its 206. |
| **D4** | Gate A's decision loop HEAD (`validate.py:703`) vs `promote()`'s post-copy CRC HEAD (`validate.py:2093-2096`) | 10,049 + 10,049 | **not a duplication** — different buckets (landing vs `edullm-data`) and different facts (pre-copy size vs post-copy CRC). `promote` is also already threaded. | **not a defect** |
| **D5** | `bundle_is_done`'s re-HEAD of every shard (S4) vs `verify_receipt`'s per-shard HEAD at the end of `run_bundle` (`corpus_build.py:530`) | 2 per shard across a resume | **arguably necessary** — one runs before work, one after. But on a *resumed* bundle both fire for the same fact within minutes. | **CODE READ**, low value |

**DERIVED total removable round trips at 1.0T / 40,000 objects:** Gate A is 8 calls/object today
(D1 dropped it to 7; the model at `pipeline-scale-audit.md:595` says 6, which does not reconcile —
see below). At 7 calls/object, killing D2 gives 6, and killing D3 gives **5**. That is a
**28% reduction in Gate A round trips for ~20 lines**, before any threading.

> **⚠️ Arithmetic note, because two documents disagree and neither flags it.**
> `pipeline-scale-audit.md:595` and `docs/IMPLEMENTATION-PLAN.md:670` both model Gate A at **6
> calls/object** and compute `10,049 × 6 = 60,294 rt ⇒ 11.8 rt/s`, then say the measured rate was
> 15.8 and attribute the gap to LISTs. But commit `db437b6` states the real counts: **80,392
> before the fix and 70,343 after** — which are exactly `10,049 × 8` and `10,049 × 7` (I checked:
> 80392/10049 = 8.000, 70343/10049 = 7.000). At 8 calls/object, `80,392 / (85×60) = 15.76 rt/s`
> — **which reproduces the measured 15.8 exactly, with no LIST fudge factor.** So the 6-call model
> is wrong and the 8-call count is right, and **the "gap explained by LISTs" was explaining a gap
> that an off-by-two created.** This does not change the 85 min, but it means every "6 calls"
> projection downstream is **25% optimistic**. I use 7 (post-fix) below.
>
> Independent confirmation from bytes: 4 windows × 16,384 B + 8 B = 65,544 B/object × 10,049 =
> **658.7 MB over 85 min = 0.129 MB/s**, against the live-measured **0.1 MB/s NetworkIn**
> (`9195098`). The byte model and the call model agree.

---

### 2.4 OVER-READING — the largest waste in the build, and it is two constants

`corpus_build.py:924`:

```python
budget = int(bundle.tokens * _CHARS_PER_TOKEN * _FILTER_HEADROOM / keep_rate)
```

with `_CHARS_PER_TOKEN = 6.0` (`:865`), `_FILTER_HEADROOM = 1.5` (`:881`), and `keep_rate` =
`val_fraction` (0.005) for a val bundle, `1 − val_fraction` for a train bundle
(`corpus_build.py:331-340`).

**I recomputed the reservoir's exact waste per source, using each source's own MEASURED
chars/token from `artifacts/reservoir/chars-per-token.json` and each bundle's realized tokens
from `realized-tokens.json`. DERIVED, inputs MEASURED:**

| | budgeted chars | true chars | over-read |
|---|---|---|---|
| **27 train bundles** | **2.264 TB** | 1.051 TB | **2.15x** |
| **26 val bundles** | **1.755 TB** | 0.0040 TB | **435x** |
| **whole build** | **4.019 TB** | 1.055 TB | **3.81x** |

**The headline finding: the val split budgeted 43.7% of the entire build's read for 0.39% of its
tokens.** The reservoir's val is 975,077,376 tokens — 0.39% of the corpus — and it was allotted
1.755 TB of the 4.019 TB read budget.

Three separate multipliers stack, and only one of them is defensible:

1. **`6.0 / 4.31 = 1.39x`** — the constant is set above the *worst observed* (5.58,
   `finephrase-table`) rather than per-source. Defensible in *intent* (`corpus_build.py:858-864`
   explains the asymmetry honestly: under-reading costs a whole-bundle re-run, over-reading costs
   time) but **needless**, because `chars-per-token.json` already holds a measured value for all
   14 sources. `finemath` at 2.56 is budgeted at 6.0 — **a 2.34x over-read on 13.5% of the
   corpus.**
2. **`× 1.5` filter headroom** — genuinely needed and genuinely unmeasured for 13 of 14 sources
   (`:876-878`: measured 3.4–12.6% for FinePhrase, *"unmeasured for the rest"*). 1.5 against a
   measured 12.6% worst case is **~4x more slack than the one measurement supports.**
3. **`÷ 0.005` for val** — this is the 200x, and it is where the waste actually lives.

**Was the wasted time real?** Partly, and this is the honest caveat. `_reader_for` breaks
**between files** once `seen_chars >= budget` (`corpus_build.py:930-931`), and it also stops when
the file list is exhausted. For a val bundle whose source pool is smaller than 200x its val
target, the reader hit **end-of-files** first and the 200x was never paid — `ubuntu-irc` is the
proof (`HANDOFF.md:1444-1446`: it *"passed because its pool is 1.04x its target so the reader hit
end-of-files first"*). So **the 1.755 TB val budget is an upper bound on the val read, not a
measurement of it.** The nine re-run bundles included **six val bundles**
(`artifacts/reservoir/rerun-jobs.json` indices 7, 9, 11, 13, 15 + `fineweb-edu--val`), which is
consistent with val bundles being slow enough to have been late in the wave, but no per-bundle
duration survives (M5e — all retracted).

**⚠️ At 1.0T this stops being an upper bound and becomes real.** `VAL_FRACTION 0.005 × 1.0T = 5B
val tokens ⇒ 5e9 × 9 / 0.005 = 9.0 TB of val read alone`, against pools that are 4–20x larger at
1T than at 252B. The end-of-files escape hatch that saved the reservoir will not fire.

#### Is there a cheaper way to reach held-out documents?

The code's own answer is no, stated twice as if settled:

> *"The carve is a pure function of the document id (`corpus.is_held_out`) and cannot be predicted
> per file, so there is no cheaper way to reach a val document than reading the train ones
> alongside it and discarding them."* — `corpus_build.py:918-921`

> *"there is no way to reach a held-out document except to read the train documents interleaved
> with it, because `is_held_out` is a hash of the document id and is not knowable per file."*
> — `corpus_build.py:336-339`

**That claim is TRUE of the current design and FALSE as a statement about what is possible.** Four
alternatives, cheapest first:

| option | val read at 1.0T | statistical cost | code |
|---|---|---|---|
| **A. Lower `val_fraction` to 0.001** | 9.0 TB → **1.8 TB** | **None to the val set's representativeness** — the *held-out* documents are still a uniform hash-slice of id space, just a smaller one. It only shrinks the val set: 1B tokens = ~40 shards at 25M, or 20 at 50M. That is 1B held-out tokens, ~100x more than anyone evaluates on. | **~15 lines.** `plan_document(val_fraction=…)` already takes it (`corpus_build.py:186`); `_cmd_plan` does not expose it. **One CLI flag.** |
| **B. Carve val ONCE, globally, in a single pass** | ≈ the train read, **+0** | None | The train bundles already read every val document and throw it away (`run_bundle._selected()` filters on `want_split`, `corpus_build.py:468-472`). Emit both splits from **one** read instead of two. **This is the real fix and it is architectural:** ~60–100 lines, because `pack` is per-bundle and a bundle is `(source, domain, split)`. |
| **C. FILE-level val sample** (the option the prompt asks about) | 9.0 TB → **~0.045 TB** (0.5% of files) | ⚠️ **Real, and it is not small.** Documents inside one parquet file are **not** exchangeable with the corpus: HF shards are usually written in crawl/ingest order, so a file is correlated in date, domain, and often language. A file-level val set measures "held-out *files*", which is a *harder* generalization test (good) but is **no longer an unbiased estimate of the training distribution** (bad, and it is what a val loss is for). It also destroys the property `is_held_out` exists to guarantee: `HANDOFF.md`-documented history is that a previous corpus shipped **6 val shards that were byte-copies of train shards, 100% leakage** (`corpus.py:371-374`), and the file-level variant reintroduces a *state-dependent* selection where a re-run with a different file list selects a different val set. | ~30 lines, but it changes the published contract in `about=` (*"Held-out documents are carved BEFORE tokenizing by a hash of (source, document id)"*) |
| **D. Reservoir-sample val from the train read** | **+0** | None if the reservoir is per-source and large enough | Equivalent to B with bounded memory. |

**Recommendation: A now (one flag, zero risk), B before 1.0T (it removes the term entirely).**
Do **not** do C — the statistical cost is real, the leakage-history reason it exists is real, and
B is cheaper in wall-clock than C anyway.

---

### 2.5 SINGLE-STREAM WHERE MULTI-STREAM IS AVAILABLE

| # | site | today | available | grade |
|---|---|---|---|---|
| **T1** | `s3.hash_object` (`s3.py:249-262`) | one `get_object`, `iter_chunks(8 MiB)`, **one stream** ⇒ R2's 85 MB/s | S3 supports N parallel ranged GETs on one object. A 100 MB shard at 4 ranges = ~340 MB/s for that object. **But** `hash_object` returns a **sha256**, which is inherently sequential — you cannot combine partial digests. So the fix is not intra-object parallelism; it is **inter-object** (`--hash-workers`), which exists. **Correctly scoped as-is.** | **CODE READ** |
| **T2** | `verify --deep` inter-object | 1 worker by default (`corpus_receipt.py:453`) | **7.82x at 8** MEASURED (M8b) | ✅ **FIXED** in `0.7.5`, live at `edullm-reservoir-verify:3` |
| **T3** | `publish()` hash | `hash_workers` exists and the driver passes 16 | **but throttled to ~10 by F-POOL** | ⚠️ see 2.2 |
| **T4** | **`_reader_for` / `corpus_read`** | **one HTTP stream per child** — the generator chain is `hf_files → reader → carve → dedup → tokenize → pack`, fully sequential, zero threading (grep-verified) | The pattern already exists twice in this repo: `ingest_reservoir.py:796,890` use `pool.map` over a file list, **which preserves order** — exactly what a deterministic build needs | **CONFIRMED by grep. NOT FIXED.** |
| **T5** | read and tokenize do not overlap | a child's wall clock is `read + tokenize`, not `max(read, tokenize)` | A bounded prefetch queue makes it `max(...)`. At the reservoir's shape this is worth ~30–50% of a child's wall clock. | **CODE READ. NOT FIXED.** |

**T4/T5 is the one place where I think the repo's own analysis may be over-confident, in the
pessimistic direction.** `pipeline-scale-audit.md:536-543` reasons that across ~100 children the
download fans out and **tokenize** becomes the binding constraint against R1. That is probably
right — but it rests on assuming per-child HF CDN throughput ≈ R2's **S3** figure, and the code
itself flags that as unverified (`corpus_build.py:901-904`). **If HF CDN single-stream is 20 MB/s
rather than 88, the ordering flips and T4 becomes the #1 fix.** That measurement costs one
single-bundle Batch job and nobody has run it.

---

### 2.6 RETRY / BACKOFF — the fix landed in ONE of the two paths

`_backoff_delay` (`ingest_reservoir.py:232-247`) is correct: exponential `4 × 2**attempt` capped
at 120 s, honours a numeric `Retry-After`, `_MAX_ATTEMPTS = 8`. It is even guarded by a
job-definition preflight assertion (`RUN-THE-INGEST.md:126`: `assert _backoff_delay(3) == 32.0`
*"catches a linear-backoff regression"*).

> **⚠️ F-BACKOFF — the linear retry `PLAN-CORRECTIONS.md` §6 records as fixed is STILL IN THE CODE,
> on the sibling branch of the same `try`.**
>
> `ingest_reservoir.py:474-481`, the generic-`Exception` (transport) arm of `_read_once`:
>
> ```python
> except Exception as exc:  # noqa: BLE001 - transport retry, re-raised below
>     ...
>     time.sleep(min(_BACKOFF_CAP_S, 3 * (attempt + 1)))
> ```
>
> **`3 * (attempt + 1)`.** That is verbatim the *"3 s linear retry that could never outlast the
> limit"* from `PLAN-CORRECTIONS.md:231`, and verbatim the *"`3*(n+1)` seconds over five
> attempts"* that `INGEST-CALIBRATION.md:123-126` says was **fixed in `0.6.1`**. It was fixed on
> the `HTTPError` branch (lines 455-473) and **left on the transport branch**.
>
> **Totals I computed:** the HTTPError path sleeps `4+8+16+32+64+120+120 = 364 s` across 7
> retries. The transport path sleeps `3+6+9+12+15+18+21 = 84 s`. **A connection reset, a DNS
> blip, a socket timeout, or a TLS error gets 84 s of patience where a 429 gets 364 s.**
>
> **The preflight assertion does not catch it** — `assert _backoff_delay(3) == 32.0` tests the
> *function*, and the transport branch never calls the function. This is the exact shape of the
> `families/` half-fix: **the guard tests the fixed half.**
>
> **Is it costing wall clock, or saving it?** It is *saving* wall clock and *costing* reliability
> — the opposite direction from over-waiting. 84 s is too *short*: the failure mode is a child
> dying and a whole bundle's billable work being lost, which is exactly what the 503 incident cost
> (**5 of 8 children overnight, hours of work each**, `HANDOFF.md:1458-1463`). **Fix: replace with
> `_backoff_delay(attempt)`. One line.**

**Does the current config over-wait anywhere?** Checked, and no:

- The 120 s cap is deliberate and documented (`:189-191`: *"capping at 120 s keeps a stuck worker
  from idling for half the job's timeout"*).
- `403` is deliberately **excluded** from `_TRANSIENT_STATUSES` (`:201-203`), which is right — a
  CDN signature expiry needs a re-resolve, and retrying it would burn 364 s to fail anyway.
- `501`/`505` are deliberately excluded (`:199-201`) precisely so a permanent error is not buried
  behind 8 backoffs. **The set is right.**
- The `_RateGate` is now a **backstop**, not the mechanism (`:263-264` says so), so a false 429
  no longer stalls the fleet for the whole pass.
- `_CDN_TTL_S = 3000` against a measured 3600 s signature life is 600 s of margin — correct, and
  the *real* trap the old docstring was masking (`:344-348`).

One genuine over-wait risk at 1.0T: a `_RateGate.penalise` from **any one** worker pauses **every**
worker (`:266-268`). That is the correct design against a metered endpoint, but the endpoint is now
**unmetered**, so a single spurious 429 from an unrelated cause costs `delay × n_workers` of idle
capacity. Worth a `--no-rate-gate` escape or a threshold (e.g. penalise only after k 429s in a
window). **~10 lines, low priority** — `ingest_reservoir.py:948-950` already prints the penalty
count, so it is observable.

---

### 2.7 WRONG INSTANCE TYPE / WASTED PROVISIONED CAPACITY

The cap is **128 vCPU on one `c7i.8xlarge` type** (R1, real).

| stage | shape actually used | vCPU in use | vCPU available | utilization | grade |
|---|---|---|---|---|---|
| ids/fetch calibration | 4 vCPU / 16 workers | 4 | 128 | **3.1%** — and it was *over*-subscribed on threads (16 workers on 4 vCPU) while under-subscribed on the CE | **MEASURED** `INGEST-CALIBRATION.md:33` |
| ids/fetch real run | 2 vCPU / 8 GB, 10 array children × 4 workers | ≤20 | 128 | **≤16%** | **MEASURED** `RUN-THE-INGEST.md:24,44-48` |
| **the build** | **12 concurrent children × 8 vCPU = 96** | **96** | **128** | **75%** — the best-utilized stage in the pipeline, and it was **1.5x under-parallel at first** (~6 children ⇒ ~19 h) until raised to 12 | **MEASURED** `HANDOFF.md:1869-1871`, `:1876-1880` |
| Gate A | 4 vCPU, single-threaded | **0.3% CPU of 4 vCPU** = 0.012 vCPU | 128 | **0.01%** | **MEASURED** `9195098` |
| `verify --deep` | 16 vCPU, single-threaded | **1 of 16** | 128 | **0.8%** | **MEASURED** `verify-job.json:15,34` |
| publish | 16 vCPU, `hash_workers=16` throttled to ~10 by F-POOL | ~10 of 16 | 128 | ~8% | **DERIVED** |

**Verdict: the reservoir was badly under-parallel against a 128-vCPU cap in every stage except the
build.** The build reached 75% and its finish moved `~19 h → ~8 h at identical cost, since
vCPU-hours are conserved` (`HANDOFF.md:1870-1871`) — which is the single clearest demonstration in
the repo that the remaining stages are leaving the same factor on the table.

**Two operational lessons already recorded, worth carrying forward:**

- **Individual jobs, not an array, capped concurrency.** `HANDOFF.md:1865-1871` — chosen
  deliberately (Batch has no per-array concurrency limit and a queue fair-share policy would have
  affected the owner's own jobs), so this was a *reasonable* self-inflicted slowness with a stated
  reason. It is still 1.5x.
- **Terminating children while a queue can backfill gains nothing.** `HANDOFF.md:1473-1481` —
  Batch refilled from a 10-deep queue faster than the cancels landed: *"~50 minutes × 4 bundles of
  work destroyed, zero capacity freed."* Drain the queue first or terminate the array in one call.

---

### 2.8 Summary — real vs self-inflicted, by stage

| stage | measured | binding constraint | real or self-inflicted |
|---|---|---|---|
| ids/fetch (pre-fix) | 0.44 files/s ⇒ 16.9 h projected | our own 70x metered-resolve amplification | **SELF-INFLICTED, 70x. FIXED.** |
| ids/fetch (post-fix) | 67 s for 4 configs | HF CDN + per-file resolve | **REAL** (and now trivial) |
| build (read+tokenize+pack) | ~8 h at 12×8 vCPU | 3.81x over-read (2.4) + no reader threading (T4) + 75% of the CE | **MOSTLY SELF-INFLICTED** |
| tokenize alone | 10.5 M tok/s / 32 vCPU | R1's 128 vCPU | **REAL** (6.61 h floor at 1.0T) |
| publish | not measured | F-POOL 1.6x throttle | **SELF-INFLICTED** |
| Gate A | 85 min / 10,049 obj | 5 of 6 network calls serial (S2) + 2 redundant HEADs (D2/D3) | **SELF-INFLICTED, ~10x available** |
| promote | already threaded | — | **FIXED** |
| `verify --deep` | 3.27 h at 1 of 16 vCPU | single-threaded loop (S1) | **SELF-INFLICTED, 7.82x. FIXED in code, and now in the job def.** |
| `fsck` | never measured | serial HEADs (S3) | **SELF-INFLICTED** |


---

## Q2b — ⚠️ A CORRECTION TO §2.4, AND TO THE REPO'S OWN OVER-READ FINDING

**I have to retract part of my own §2.4, and with it `pipeline-scale-audit.md` F4.2 and
`docs/IMPLEMENTATION-PLAN.md`'s tokenize projections.** The 4.019 TB figure is a **budget ceiling
that is never reached**, not a volume of bytes transferred. Here is the chain, read line by line.

`run_bundle` (`corpus_build.py:466-505`) composes **five lazy generators**:

```
_reader_for  ->  carve  ->  [filter on want_split]  ->  dedup_and_decontaminate
             ->  tokenize_documents  ->  pack
```

Every stage is a generator, and each says so in its own docstring: `carve` — *"A generator, so a
2.5 TB stream is never materialised"* (`corpus.py:410`); `dedup_and_decontaminate` — *"A
generator: the corpus does not fit in memory"* (`corpus_filter.py:300`); `_batched` — *"Chunk an
iterable without materialising it"* (`corpus_pack.py:382`).

**So the consumer sets the read volume, not the budget.** `_pack_stream` (`corpus_pack.py:726-742`)
calls `next(doc_iter, None)` only while `cursor < ref.tokens` on a ref that still needs filling.
When the last ref is full, it stops calling `next`. `_drain_surplus` with `partial_source=True`
— which `run_bundle:519` passes — then **explicitly refuses to drain**:

> *"Stop pulling. The remaining stream is the part of the pool this bundle was never meant to take,
> and counting it would cost a full read of data that is about to be ignored."*
> — `corpus_pack.py:891-894`

The generator is left suspended inside `yield doc` and **never resumes**, so the rest of that
parquet file is never fetched. `_reader_for`'s `if seen_chars >= budget: break` (`:930-931`) is
therefore **reached only if pack's demand exceeds the budget first.** I checked whether it can:

| | pack DEMANDS (chars per planned token) | budget ALLOWS | binds? |
|---|---|---|---|
| train bundle, 0% attrition | `4.31 / 0.995 = 4.33` | `6.0 × 1.5 / 0.995 = 9.05` | **no — pack stops at 48% of budget** |
| train bundle, 12.6% attrition (worst measured) | `4.96` | `9.05` | **no — 55%** |
| val bundle, 0% attrition | `4.31 / 0.005 = 862` | `6.0 × 1.5 / 0.005 = 1,800` | **no — 48%** |

**The budget never binds. It is belt-and-braces, and after `0.7.1` it is nearly dead code** — the
thing that actually stopped the runaway walk of bug #1 was `partial_source=True` (fix #2,
`0b8135e`), not the budget (fix #1, `241334c`).

**The receipts prove it independently.** `HANDOFF.md:396` records that conservation
`tokens_in == tokens_out + tail + surplus` **holds on all 27 bundles**, and under
`partial_source=True` `surplus = pending_left` (at most one document) because `unread` is forced to
0. So `tokens_in ≈ tokens_out` on every bundle — **which is receipt-level evidence that the reader
was never drained.** If a 2.09x over-read had really been pulled and tokenized, `tokens_in` would
be ~2x `tokens_out` on 27 of 27 receipts. It is not.

### The corrected numbers

**DERIVED, from MEASURED inputs (per-source chars/token, realized per-split tokens, an 8%
attrition midpoint of the measured 3.4–12.6% range):**

| | reservoir (measured shape) | 1.0T (projected) |
|---|---|---|
| **bytes actually READ** | **2.026 TB** | **8.06 TB** |
| ↳ of which the 27 **train** bundles | 1.148 TB | 4.57 TB |
| ↳ of which the 26 **val** bundles | **0.877 TB (43.3%)** | **3.49 TB (43.3%)** |
| corpus payload written | 1.005 TB | 4.00 TB |
| read : payload | **2.02x** | 2.02x |
| the never-reached budget ceiling | 4.019 TB | 18.05 TB |
| **tokens actually ENCODED** | **0.273 T = 1.087x the corpus** | **1.087 T** |

### What this changes

| claim | status |
|---|---|
| *"the pipeline pulls ~9 TB, not 4.2 TB — a 2.1x over-read baked into two constants"* (`pipeline-scale-audit.md:520-530`) | ⚠️ **HALF WRONG.** The 2x is real; **the two constants are not its cause.** The cause is `keep_rate` alone. |
| *"a val bundle reads 200x its own token count... The val split doubles the download"* (`pipeline-scale-audit.md:527-530`) | ✅ **CORRECT, and it is the ENTIRE over-read.** 43.3% of the read for 0.39% of the tokens. |
| *"the whole 2.09T-token encode takes 13.8 h"* (`pipeline-scale-audit.md:540`); *"18–55 h with the 2.09x over-read"* (`:532`) | ❌ **WRONG by 1.92x.** Tokenize sees **1.087T**, not 2.09T, because `carve` and the split filter run **before** `tokenize_documents`. At 128 vCPU: **7.19 h, not 13.8 h.** |
| *"Lower `_CHARS_PER_TOKEN` from 6.0 to ~5.8"* / *"make it PER-SOURCE... a 28% saving"* (`pipeline-scale-audit.md:533-537`) | ❌ **WORTHLESS. Saves zero bytes**, because the budget it tightens is never reached. It would only narrow the safety margin against an unfilled final shard — i.e. **pure downside.** **Do not do this.** |
| *"Lower `val_fraction`"* (`pipeline-scale-audit.md:538-540`) | ✅ **CORRECT, and it is now the ONLY over-read fix worth making.** Its value is *higher* than that document credits, because it is the whole term. |

**The constants' docstrings were right and the audit criticizing them was wrong.**
`corpus_build.py:858-864` and `:876-878` both argue the asymmetry correctly — under-reading costs a
whole-bundle re-run, over-reading costs only time — and it turns out over-reading costs **not even
time**, because the ceiling is never touched. This is the third instance in this repo of a correct
measurement supporting a wrong conclusion, and I produced the first draft of it myself in §2.4
above, which I have left in place rather than deleting so the correction is visible.


---

## Q2c — ⚠️ AND THE `val_fraction` FIX DOES NOT WORK EITHER. `val_fraction` CANCELS.

Both `pipeline-scale-audit.md:538-540` and `docs/IMPLEMENTATION-PLAN.md:1205` recommend lowering
`val_fraction` as the val fix — *"Lower `val_fraction` to 0.001 and this becomes ~1 h."* **It does
not. The saving is exactly zero, and it is an algebra error.** Both documents state the formula
themselves and neither cancelled it:

```
val_read = val_tokens × chars_per_token / keep_rate
         = (S × vf)   × 9               / vf          ← pipeline-scale-audit.md:529's own formula
         =  S × 9                                     ← vf CANCELS
```

Numerically, from that document's own expression: `vf=0.005 → 9.00 TB`, `vf=0.001 → 9.00 TB`,
`vf=0.0005 → 9.00 TB`. **Identical.**

The mechanism is obvious once seen: a val bundle must read the *whole source* to find its
held-out slice, because `is_held_out` is a uniform hash over id space. Halving the slice halves
the target **and** doubles the read-per-target-token. Lowering `val_fraction` makes the val set
smaller for **the same read**.

**DERIVED, from MEASURED per-source inputs, at the reservoir's shares scaled to 1.0T:**

| `val_fraction` | sources keeping a val bundle | val bytes read | sources silently losing val |
|---|---|---|---|
| **0.005 (today)** | 14 of 14 | **4.56 TB** | — |
| 0.001 | 12 of 14 | **4.43 TB** (−2.9%) | `pubmed`, `ubuntu-irc` |
| 0.0005 | 10 of 14 | **4.09 TB** (−10%) | + `finewiki`, `stackexchange` |

The only reason it drops at all is the `if val < SHARD_TOKENS: val = 0` floor
(`corpus_build.py:225-227`), which *deletes whole val bundles*. **So the recommended fix buys 3%
of the read by silently discarding held-out data for two sources** — and the break-even target
rises from 5.00B to 25.00B tokens, which at 1.0T shares takes out `pubmed` and `ubuntu-irc`
immediately. That is a **coverage regression sold as a performance fix**, and `PUBLISH-SPEC.md`
already has to carry a `limitations[]` entry for the *one* source this happened to at 0.005.

### The only fix that actually removes the val over-read

**Emit both splits from ONE read (§2.4 option B).** The train bundles already read every val
document and discard it — `run_bundle._selected()` filters `if split == want_split`
(`corpus_build.py:468-472`), throwing the other half away. Carving both sides in one pass makes
the val read **free**, not smaller:

| approach | val read at 1.0T | val coverage | code |
|---|---|---|---|
| today | 4.56 TB | all 14 sources | — |
| lower `val_fraction` to 0.001 | 4.43 TB | **12 of 14** | 15 lines |
| **both splits from one pass** | **0.00 TB** | **all 14, unchanged** | **60–100 lines** |

**This is the single largest wall-clock item in the whole 1.0T build: 4.56 TB of 8.06 TB read,
43% of all bytes moved, for 0.39% of the tokens.** It is also the only fix in this audit that
needs real architectural work, because a bundle is keyed `(source, domain, split)` and `pack`
takes one stream per bundle. The shape: make `run_bundle` accept **two** streams and two ref lists
for one source and hand `pack` both (`pack` already takes a `Mapping[stream, Iterable]` and loops
streams at `corpus_pack.py:668-684` — **it can already write two streams in one call**). The
blocker is that the CLI's unit of work is a bundle, so `_cmd_run`'s slicing
(`corpus_build.py:673-676`) and `bundle_is_done` would need to pair train with val.


---

## Q3 — THE RANKING: current → fixed, lines of code, risk

All wall-clocks are at the **1.0T target** (39,997 objects at the code's current
`SHARD_TOKENS = 25,001,984`; 4.00 TB payload; 8.06 TB read). Rates are the MEASURED anchors: R2
85.3 MB/s single-stream, R3 15.8 rt/s serial, M4 10.5 M tok/s per 32 vCPU, M8b 7.82x at 8 hash
workers. **"h saved / line" is the ranking key the owner asked for.**

| rank | fix | site | current | fixed | Δh | LOC | h/LOC | risk |
|---|---|---|---|---|---|---|---|---|
| **1** | **Pass `--hash-workers 8` to `verify --deep`** | job def, **not code** (`0.7.5` already ships it, `corpus_receipt.py:865-879`) | **13.03 h** ❌ *exceeds a 4 h timeout — a FAILURE, not a slow run* | **1.67 h** | **11.4** | **0** (one `register-job-definition`) | **∞** | **NONE.** `hash_workers=1` is byte-for-byte the old path; violations confirmed element-identical at 1/2/4/16 workers, and 1 worker touches only `MainThread` (`PUBLISH-SPEC.md:172-177`). Pool already sized (`corpus_build.py:734`). **✅ ALREADY DONE at `edullm-reservoir-verify:3`** (`infra/DEPLOY.md:810`) — verify it is still rev 3 before the run. |
| **2** | **Thread Gate A's profile checks** (the 5 ranged GETs) | `profiles/pretrain_tokens_v1.py` `check_decode_smoke:278-320`, `check_first_bytes_not_npy:421-450`, `_sampled_ids:226-250` + `validate.py` CLI wiring | **4.92 h** ❌ *exceeds 14400 s* | **0.22 h** | **4.70** | **~100** | **0.047** | **LOW.** The checks are order-independent: each builds a fresh local `out` list and iterates `_entries(ctx)` (`:280`, `:432`, `:462`). The order-sensitive logic (`duplicate-shard-digest`, `shared-sha-with-parent`) lives in `_validate_group`'s loop (`validate.py:733-752`), **not** here. **Use the prefetch pattern:** a pure function that threads the I/O and returns `{path: (size, head8, windows)}`, leaving the check loops byte-for-byte serial. Must fold the new worker count into `validate.py:2483`'s `max()` or F-POOL bites. |
| **3** | **Carve both splits in ONE read** | `corpus_build.py:466-505` `run_bundle`, `:668-700` `_cmd_run`, `:387-426` `bundle_is_done` | **4.56 TB / ~1.9–11.0 h** of the build | **0.00 TB / 0 h** | **1.9–11.0** | **60–100** | **0.02–0.18** | **MEDIUM.** Highest-value read fix and the only one that works (Q2c). `pack` already accepts multiple streams in one call (`corpus_pack.py:668-684`), so the packer needs no change — the work is in the CLI's unit of work and the resume check. **Guard with a determinism test asserting byte-identical digests against the two-pass build**, which is cheap because the corpus is already proven deterministic (9 bundles reproduced identical digests, `HANDOFF.md:373`). |
| **4** | **Size the connection pool at the source** | `s3.py:192-196` `Boto3S3.default` + `artifacts/reservoir/publish_driver.py:155` | publish hash **1.30 h** (16 workers throttled to ~10) | **0.81 h** at a real 16; **0.41 h** at 32 | **0.49–0.89** | **~6** (a `max_pool_connections` kwarg on `default()`), or **1** in the driver | **0.08–0.15** | **NONE.** Pure client config. **Do the 6-line version, not the 1-line one** — three call sites already carry three near-identical workarounds with three near-identical 6-line docstrings (`validate.py:2476-2490`, `corpus_build.py:571-596`, `corpus_receipt.py:505-512`), and `infra/10-dataset-publish-jobdef.md:76-78` predicted this driver bug in writing before it shipped. This is the `families/` half-fix pattern again. |
| **5** | **Seed `object_sizes` from `_prefetch_heads`** (kill D2) | `validate.py:703` → write into the group's `observations` dict that `:846` passes to `GroupContext` | 7 calls/object | 6 calls/object | **0.70** serial, ~0.03 threaded | **~3** | **0.23** | **NONE.** Both caches hold the same observation of the same key from the same bucket in the same run. Already recommended at `pipeline-scale-audit.md:657-662` and never done. **Do this even if #2 lands** — 40,000 fewer round trips is 40,000 fewer chances to hit a transient. |
| **6** | **Take size from `Content-Range`, drop the HEAD entirely** (kill D3) | `s3.py:217-224` `get_range` returns only `bytes`; needs to also surface `ContentRange` | 6 calls/object (after #5) | **5** | **0.70** serial, ~0.03 threaded | **~15** (a `get_range_with_meta`, plus `_observed_size` preferring it) | **0.047** | **LOW-MEDIUM.** ⚠️ **The golden rule still holds** — `Content-Range`'s `/TOTAL` is S3 recomputing the object length, exactly as a HEAD does; it is not a producer claim. But it is a **semantic** change to a validator that has a "recompute, never trust" contract, so it needs its own test asserting a truncated object is still caught. Lower priority than #5 for that reason alone. |
| **7** | **Fix the linear transport backoff** | `ingest_reservoir.py:481` `time.sleep(min(_BACKOFF_CAP_S, 3 * (attempt + 1)))` → `time.sleep(_backoff_delay(attempt))` | 84 s of patience for a socket error vs 364 s for a 429 | 364 s both | **not a wall-clock fix — a RELIABILITY fix** | **1** | n/a | **NONE.** The function is already tested (`tests/test_ingest_reservoir.py:399-418`) and already asserted in the job-def preflight (`RUN-THE-INGEST.md:126`) — **the preflight passes today because the transport branch never calls the function.** The downside of *not* fixing it is measured: the 503 incident cost **5 of 8 children overnight, hours each** (`HANDOFF.md:1458-1463`). **Highest value-per-line in the audit after #1.** |
| **8** | **Thread `bundle_is_done`** | `corpus_build.py:419-425` | **42 min** of pure resume overhead at 40,000 shards | **2.6 min** | **0.66** | **~12** | **0.055** | **NONE.** Pure reads, boolean AND over independent facts, no order dependence. Only pays on a re-run — but a re-run is exactly when you are already behind. |
| **9** | **Thread `fsck`** | `fsck.py:112-137`, needs a `--workers` flag | **~42 min** at 40,000 objects (**NEVER MEASURED**) | **~2.6 min** | **0.66** | **~15** | **0.044** | **NONE.** Weekly, metadata-only, findings-only. Lowest urgency; include it when touching #8 since it is the same pattern. |
| **10** | **`_RateGate` threshold** | `ingest_reservoir.py:280-283` | one spurious 429 pauses all N workers | penalise after k in a window | small, situational | ~10 | low | **LOW.** The gate was built for a **metered** endpoint that is now known **unmetered** (`:263-264` says so). Observable already via `:948-950`. Do only if 429s show up in volume. |
| — | ~~**Lower `_CHARS_PER_TOKEN` / make it per-source**~~ | `corpus_build.py:865` | — | — | **0.00** | ~20 | **0** | ❌ **DO NOT DO THIS.** Q2b: the budget is never reached, so it saves no bytes and only narrows the margin against an unfilled final shard. Pure downside. `pipeline-scale-audit.md:533-537` recommends it; that recommendation is wrong. |
| — | ~~**Lower `val_fraction` to 0.001**~~ | `corpus_build.py:186` | — | — | **0.13** (2.9%) | 15 | 0.009 | ❌ **DO NOT DO THIS AS A PERF FIX.** Q2c: `val_fraction` cancels out of the read. The 2.9% comes entirely from **deleting `pubmed`'s and `ubuntu-irc`'s val bundles** — a coverage regression. Lower it only if you *want* a smaller val set. |
| — | **Raise every timeout** | job defs | Gate A ❌ and `verify --deep` ❌ **fail outright** | pass | — | **0** | ∞ | **NONE. DO THIS FIRST, REGARDLESS.** It is a dial we set, not an AWS limit (`INGEST-CALIBRATION.md:19-22`), already exercised (7200 → 14400 on `validator:14`). **≥ 21600 s (6 h) everywhere.** It converts two guaranteed failures into slow successes, which is what makes the other fixes optional rather than blocking. |

### The three fixes that actually matter

**Ranked by hours saved, not by hours-per-line:** #1 (11.4 h, zero code), #2 (4.7 h, ~100 lines),
#3 (1.9–11.0 h, ~60–100 lines). Together **~18–27 h of a ~36 h serial pipeline**, for ~200 lines
and one `register-job-definition`.

**Ranked by hours-per-line**, which is what the owner asked for: **#1 (free) → #7 (1 line, buys
reliability not hours) → #5 (3 lines) → #4 (6 lines) → #8 → #2 → #6 → #9**.

**The honest framing of "self-inflicted".** #1 was already fixed in code and left unwired in the
job def. #4 was predicted in writing in `infra/10-dataset-publish-jobdef.md` before it shipped and
never fixed. #5 was recommended in `pipeline-scale-audit.md` and never done. #7 was fixed on one
branch of a `try` and left on the other, **behind a preflight assertion that cannot see it.**
**Four of the top seven are not "we didn't know" — they are "we knew, wrote it down, and the fix
landed in the wrong half."** That is the same failure mode as the `families/` half-fix that this
repo's `CLAUDE.md` already immortalizes: *"grep for the pattern everywhere before calling a bug
fixed."*


---

## Q3b — TWO MORE SELF-INFLICTED ITEMS, FOUND WHILE RECONCILING THE BUILD

Before the projection, I had to explain the measured 8 h build (M5), and the accounting did not
close. It surfaced two more defects.

**The reconciliation, DERIVED from MEASURED inputs:**

| term | value |
|---|---|
| tokens actually encoded (Q2b) | 0.273 T |
| tokenize at 96 vCPU (12 children × 8, M4's per-vCPU rate) | **2.41 h** |
| **measured wall clock (M5)** | **8.00 h** |
| **residual** | **5.59 h = 70% of the build** |

If the residual were all download, it implies **8.4 MB/s per child** — 10x below R2's S3 figure.
Two readings, and both are findings:

- **HF CDN single-stream really is ~8 MB/s from a Batch container.** Then T4 (thread the reader) is
  the #1 build fix, not #3, and every projection in this repo that borrows the 85.3 MB/s S3 number
  for HF is **10x optimistic**. **NEVER MEASURED** — `corpus_build.py:901-904` flags exactly this.
- **Or the residual is CPU in the filter stage,** which the tokenize figure does not cover.

I found evidence for the second, and it is a defect:

> **⚠️ F-DECON — the 13-gram scan is a Python-level per-window hash over every document, and it is
> plausibly comparable to tokenization.** `corpus_filter.py:190-196`: for each document it loops
> `range(len(words) - 12)` and calls `_ngram_hash` — a `"\x1f".join(...)` plus a `blake2b` — **once
> per window**. DERIVED from the realized shape (815 mean tokens/doc ⇒ ~639 words ⇒ **~627 windows
> per document** × 308 M documents = **~193 billion `blake2b` calls**): at 0.5–2 µs per window that
> is **27–107 core-hours**, i.e. **0.28–1.12 h at 96 vCPU** for the reservoir and **1.11–4.45 h at
> 1.0T**. The `minimum_hits >= 2` early return (`:194-195`) helps only on documents that *are*
> contaminated, which is ~0.03% of them (`artifacts/impl-plan/dedup-decontam-audit.md` records real
> decontam loss at ~0.026%) — **so effectively every document pays the full scan.** **Grade:
> DERIVED with a wide band; the per-window cost has NEVER been measured.** It is the most likely
> home of the 5.59 h residual and it is worth one `cProfile` run on a Batch box before the 1.0T
> build. Fix if confirmed: hash the rolling window incrementally, or drop to a sampled window
> stride, or push it to a Rust/numpy path.

> **⚠️ F-NFC — every document is NFC-normalized THREE times, and it is a pure duplicate.**
> `unicodedata.normalize("NFC", text).rstrip()` (`corpus_filter.py:100`) is called from
> `content_hash` (`:105`) and from `_words` (`:110`). The call chain:
> `dedup_and_decontaminate:306` → `content_hash(doc.text)` **[pass 1]**, then `index.contains(text)`
> → `:183 content_hash(text)` **[pass 2, on the identical string]** → `:185 _words(text)`
> **[pass 3]**. **DERIVED:** ~1.9 core-h per NFC pass over the reservoir's 2.03 TB, so ~3.8 core-h
> wasted — small in absolute terms (0.04 h at 96 vCPU) but it is **~5 lines** to normalize once and
> pass the normalized string down, and pass 2 recomputes a digest the caller **already has in a
> local variable**. Ranks alongside #5 on hours-per-line.


---

## Q4 — THE 1.0T PROJECTION, TWICE

**Scaling basis.** 1.0T / 251,218,001,920 = **3.981x**. Objects **10,049 → 39,997** at the code's
current `SHARD_TOKENS = 3052 × 8192 = 25,001,984` (`corpus.py:89`). Payload **1.005 → 4.00 TB**.
Read **2.03 → 8.06 TB** (Q2b, not the 18 TB the budget ceiling implies). Tokens encoded
**0.273 → 1.087 T** (Q2b, not 2.09 T).

> ⚠️ `HANDOFF-FINAL-DATASET.md:181` decides a **50,003,968-token shard**, which would halve objects
> to **19,998** and halve every per-object stage. **It is not implemented** — `corpus.py:89` still
> says 3052 × 8192. I project the **code's** shard size, because that is what would run today. The
> decision is free (`check_seq_len_alignment` is skipped unless the group declares `seq_len`, which
> the published corpus does not — `pretrain_tokens_v1.py:458-460`, `HANDOFF-FINAL-DATASET.md:155`)
> and `50,003,968 / 8192 = 6104.0` exactly, so it satisfies `_assert_ref_alignable`. **Implementing
> it is a one-constant 2x on stages 4–7 below** and belongs in the Q3 ranking at roughly #2.

### 4.1 Per-stage table

Parallel shape is `children × vCPU`, capped at **R1 = 128 vCPU**.

| # | stage | unit of work | measured rate (anchor) | AS-IS | FIXED | parallel shape (fixed) |
|---|---|---|---|---|---|---|
| 0 | ids/fetch (synthetic partition) | 27,104 files | 67 s for 4 configs (M2) | **1–3 h** | 1–3 h | 10 × 2 vCPU, 4 workers each |
| 1 | `plan --upload` | 1 plan | — | < 5 min | < 5 min | 1 × 2 |
| 2 | **build: read** | 8.06 TB | ⚠️ **8.4 MB/s/child DERIVED** (Q3b) or 85.3 (R2, S3 proxy) | **8.06 TB** | **4.57 TB** (carve val in the train pass, #3) | 16 × 8 = 128 |
| 3 | **build: tokenize** | 1.087 T tok | **10.5 M tok/s / 32 vCPU** (M4) | **7.19 h at 128 vCPU** — the **REAL FLOOR** | 7.19 h | 16 × 8 = 128 |
| 3b | build: dedup + 13-gram decon | 1.2 B docs | **NEVER MEASURED** (F-DECON) | **1.1–4.5 h** at 128 vCPU | 1.1–4.5 h, or ~0 if fixed | same containers |
| — | **build TOTAL (the honest range)** | 100 bundles | naive scale of M5 | **23.9–31.8 h** | **12–18 h** | 16 × 8 in 6–7 waves |
| 4 | **publish** (hash 4.0 TB + 39,997 server-side copies) | 4.00 TB | **85.3 MB/s/stream** (R2/M8a) | **1.36 h** (16 workers throttled to ~10 by F-POOL) | **0.85 h** at a real 16; **0.42 h** at 32 | 1 × 16–32 |
| 5 | **Gate A** | 39,997 objects × 7 calls | **15.8 rt/s serial** (M7) | **4.92 h** ❌ *fails a 14400 s timeout* | **0.22 h** (#2 + #5 + #6) | 1 × 16 |
| 6 | **`verify --deep`** | 4.00 TB re-hash | **85.3 MB/s ×7.82 at 8w** (M8/M8b) | **13.03 h** ❌ *fails a 4 h timeout* | **1.67 h** | 1 × 16, `--hash-workers 8` |
| 7 | **promote** | 39,997 × 2 rt | already threaded (M9) | **1.41 h** at 1 worker | **0.09 h** at 16 | 1 × 16 |
| 8 | `fsck` (weekly, post-publish) | 39,997 HEADs | never measured (S3) | **0.70 h** | **0.04 h** | 1 × 16 |

### 4.2 Totals and the critical path

| | AS-IS | FIXED |
|---|---|---|
| pre-build (0–1) | 1–3 h | 1–3 h |
| **build (2–3b)** | **23.9–31.8 h** | **12–18 h** |
| publish (4) | 1.4 h | 0.4–0.9 h |
| **Gate A (5)** | **4.9 h** ❌ | **0.2 h** |
| **verify --deep (6)** | **13.0 h** ❌ | **1.7 h** |
| promote (7) | 1.4 h | 0.1 h |
| **TOTAL, serial** | **45.6–55.5 h** | **15.4–24.7 h** |

**Both ❌ rows are FAILURES, not slow runs** — they exceed their job-definition timeouts, so as-is
the corpus cannot be validated or verified at all. **Raising the timeouts (free, zero code, and
already exercised once) is what converts the as-is column from "impossible" to "slow."**

**The critical path is THE BUILD, and it dominates by 2–5x over everything downstream combined.**
That is a change from the reservoir era, where `verify --deep` at 13 h would have rivalled it. Once
#1 and #2 land, the build is 60–75% of total wall clock and the entire publish→promote tail is
~2–3 h.

**Within the build, which term binds is UNRESOLVED and it is the most important open question in
this projection:**

| hypothesis | build wall clock | what settles it |
|---|---|---|
| **tokenize-bound** (`pipeline-scale-audit.md:539-540`'s reading) | **7.19 h floor** at 128 vCPU, and no amount of reader threading goes below it | if a profiled child shows >60% CPU in `encode_batch` |
| **read-bound at ~8.4 MB/s/child** (my residual analysis, Q3b) | `8.06 TB / (16 × 8.4 MB/s) = 16.7 h`, and T4 (thread the reader) becomes the #1 fix | one single-bundle Batch job with `nettop`-equivalent instrumentation |
| **decon-bound** (F-DECON) | +1.1–4.5 h on top of either | one `cProfile` run |

**All three are cheap to settle and none has been measured.** The honest projection is the union:
**12–18 h fixed, 24–32 h as-is.**

### 4.3 Where linear scaling is an ASSUMPTION, not a measurement

The calibration file's history is the reason to enumerate these explicitly.

| # | scaled quantity | scales linearly? | why it might not |
|---|---|---|---|
| A1 | **Gate A round trips** | ✅ **yes, safely** — it is `objects × calls × latency` with no shared state and no per-corpus term. The 6,913-object olmo run (M7d, ~55 min) and the 10,049-object reservoir run (M7, ~85 min) give 8.0 and 8.5 ms/object-call — **two independent points, 21% apart, consistent with the ±25% run-to-run variance the repo already records** (`HANDOFF.md:872`). This is the best-supported linear scaling in the audit. | S3 request-rate limits (3,500 PUT / 5,500 GET per prefix-second) are far above 15.8 rt/s even at 16x |
| A2 | **`verify --deep` bytes** | ✅ **yes** for the single-stream term | the **7.82x at 8 workers is a single unattributed measurement** (M8b). 8-way linear would be 8.00x; 7.82 is 98% efficient, which is *suspiciously* good for network I/O and was measured at unknown scale. At 4 TB the aggregate is 667 MB/s from one container — **plausible on a c7i.8xlarge (12.5 Gbps) but not verified** |
| A3 | **tokenize tokens** | ✅ **yes to 128 vCPU, then hard stop** — CPU-bound, embarrassingly parallel, R1 is the wall | `_ENCODE_BATCH = 1_000` documents at a ~3.5 KB mean is ~3.5 MB in flight (`corpus_pack.py:158-161` assumes ~2 KB); at 128 vCPU the rayon pool contends with 15 sibling containers on one host. **The 10.5 M tok/s was measured at 32 vCPU and never at 128.** |
| A4 | ⚠️ **the read** | ❌ **NO, and this is the calibration file's exact trap** | Network-bound work does not scale linearly, and the repo has already been burned twice: the 16.9 h projection that became 67 s, and the "add parallelism" advice that made 429s worse. **Aggregate download across 16 children is an assumption with zero measurements behind it.** |
| A5 | ⚠️ **the build as a whole (M5 × 3.981)** | ❌ **NO** | M5 is 12 concurrent children over **27 bundles of wildly unequal size**. At 1.0T there are ~100 bundles and the largest single source (`stackv2-edu` at ~159B tokens = 6,361 shards) is **one child that cannot be split** — `_reader_for` has no file-sharding, so `pipeline-scale-audit.md:409` estimates that one bundle at 4.4–5.5 h. **The long tail, not the total work, sets the wall clock**, and a naive 3.981x multiply hides it. |
| A6 | ⚠️ **container memory** | ❌ **NO — this one is superlinear and it OOMs** | The dedup `SeenHashes` set is **155 B/entry MEASURED** (M5d) and scoped **per bundle**. At 42.2 M documents `stackv2-edu--train` needed ~12.1 GB resident (M5c) in a 14 GiB container with 1.9 GB headroom. At 1.0T that bundle is ~168 M documents ⇒ **~26 GB of dedup set alone.** **The 14 GiB container that worked at 252B will OOM at 1.0T**, and bundles must be split by file range or the set must move to disk. **This is a correctness/completion blocker, not a performance one, and it is not in any ranking above** because the fix is F2.3's file-sharding, already scoped at ~30 lines in `pipeline-scale-audit.md:415`. |
| A7 | **the ids/fetch pass** | unknown | 67 s → "1–3 h" is a 100x extrapolation from a 4-config run. Cheap either way. |
| A8 | **promote / publish copies** | ✅ yes | server-side, threaded, and M13 gives an independent anchor (**362 MB/s server-side** for the 6,921-object olmo copy) |

**A5 and A6 are the two that would actually bite**, and neither is a throughput question — they
are "one child is too big to finish or too big to fit."


---

## Q5 — THE HONEST ERROR BARS

The instruction was to treat my own projections with the suspicion the calibration file earned.
Applying its lesson — *"a throughput measurement that does not also record what limited it invites
exactly that error"* (`INGEST-CALIBRATION.md:171-174`) — here is what limited each measurement I
relied on, and what makes each projection 2x worse.

### 5.1 What makes each stage 2x worse

| stage | projected (fixed) | what makes it 2x worse | how likely | how to kill the uncertainty |
|---|---|---|---|---|
| **build** | 12–18 h | **HF CDN single-stream is ~8 MB/s, not ~85.** My own residual analysis (Q3b) computes 8.4 MB/s/child from the measured 8 h. If that is the read and not the decon scan, the read is 16.7 h and threading the reader (T4) becomes mandatory rather than optional. | ⚠️ **HIGH — I consider this the most likely 2x in the whole audit** | **one single-bundle Batch job**, which `corpus_build.py:901-904` already demands *"before committing a full array"* |
| **build** | 12–18 h | **A5: the long tail.** ~100 bundles of unequal size, and one 159B-token `stackv2-edu` child that cannot be split. Wall clock is set by the slowest child, not by total work ÷ concurrency. | HIGH | file-shard the reader (~30 lines, `pipeline-scale-audit.md:415`) |
| **build** | 12–18 h | **F-DECON: the 13-gram scan.** My own 1.1–4.5 h band spans 4x, because the per-window `blake2b` cost has never been measured. At 4 µs/window it is 9 h. | MEDIUM | one `cProfile` run |
| **tokenize floor** | 7.19 h | **M4's 10.5 M tok/s was measured at 32 vCPU, never at 128.** 16 containers on one `c7i.8xlarge` share memory bandwidth and one rayon-per-container thread pool; `_ENCODE_BATCH`'s sizing comment assumes a ~2 KB mean document against a realized **~3.5 KB**. Sublinear scaling at 4x the cores is ordinary. | MEDIUM | measure `encode_batch` at 128 vCPU |
| **Gate A** | 0.22 h | **The threading does not exist yet.** 0.22 h is a projection of code that has not been written; the *measured* threading result in this repo is **12%, not 16x** (M7e) — because it threaded 1 call of 7. If the profile-check threading hits any serialization I have not spotted, it lands nearer 4.9 h than 0.22 h. | MEDIUM | write it and measure; determinism test at 1/4/16 workers |
| **Gate A** | 0.22 h | **The 8-calls-per-object count is mine, from a commit message.** It reproduces the measured 15.8 rt/s exactly, but if the 1.0T corpus declares `seq_len` (which the reservoir did not) `check_seq_len_alignment` stops being a free cache hit and the count changes. | LOW | count calls with the existing spy harness |
| **`verify --deep`** | 1.67 h | **M8b's 7.82x has no recorded venue, scale, or date.** 98% parallel efficiency on network I/O is unusually good. If the real figure at 4 TB is 4x, this is 3.3 h; if the container's 12.5 Gbps NIC saturates first, 667 MB/s is 43% of line rate and plausible — but unverified. | MEDIUM | it is the cheapest of all these to check: the flag exists and the job def is already at rev 3 |
| **publish** | 0.85 h | **No in-region `publish()` duration has EVER been measured, at any scale.** M6 is off-region (0.8 MiB/s), M6b timed out. The reservoir was published and **its duration is recorded nowhere in this repo.** I am projecting purely from R2 × workers. | ⚠️ **HIGH — this is the largest evidence gap** | read the reservoir publish job's CloudWatch timestamps; it already ran |
| **promote** | 0.09 h | M9 is `validate.py`'s docstring, not a run. The one *measured* promotion **was SIGKILLed at 63% after 7200 s** (M7c) — so the only real data point is a failure. | MEDIUM | it is in the same job as Gate A; measure both |
| **ids/fetch** | 1–3 h | 100x extrapolation from a 67 s run. But it is ~$1 and off the critical path. | HIGH probability, LOW consequence | — |

### 5.2 The things that are 2x worse in the *other* direction (my projections may be pessimistic)

Symmetry matters, and the calibration file's error was pessimism, not optimism:

- **The build's 8 h (M5) ran with `hash_workers`-free publish, no `--hash-workers`, the F-POOL
  throttle, and 12 rather than 16 children.** At 128 vCPU with 16 children it is 23.9 h at 1.0T,
  not 31.8 — and that is before any fix.
- **The 50,003,968-token shard decision halves objects to 19,998**, which halves Gate A, promote and
  fsck outright. It is one constant. My table projects the un-implemented state.
- **Q2b's finding cut the tokenize load nearly in half** (2.09 T → 1.087 T). Every tokenize
  projection in the repo before this audit was 1.92x pessimistic.
- **`verify --deep` may not need to run twice.** `PUBLISH-SPEC.md:171-177` establishes that a
  `0.7.5` verifier accepts `0.7.4` receipts, and the corpus is proven deterministic
  (`HANDOFF.md:373`, 9 bundles → byte-identical digests). One deep verify, not one per stage.

### 5.3 NEVER MEASURED AT THE TARGET SCALE — the complete list

Stated plainly, because "nobody measured this" is a finding:

1. **HF CDN throughput from inside a Batch container** — at ANY scale. Flagged in the code itself
   (`corpus_build.py:901-904`). Every download number in this repo substitutes an **S3**
   measurement for it.
2. **`publish()` in-region** — at ANY scale. The one stage that must move 4 TB through a client.
3. **Gate A above 10,049 objects** — `HANDOFF-FINAL-DATASET.md:249` flags this itself.
4. **`verify --deep` with `--hash-workers` against real S3 at TB scale** — the 7.82x has no venue.
5. **`encode_batch` above 32 vCPU.**
6. **The 13-gram decontamination scan's throughput** — at any scale (F-DECON).
7. **`fsck` at any scale.**
8. **The full 27,104-file ids pass.**
9. **A ~168 M-document bundle's memory footprint** — A6 projects **~26 GB of dedup set** against a
   14 GiB container that was sized from a **measured** 12.1 GB at 42 M documents. **This is the one
   item on this list that is a completion blocker rather than a timing uncertainty.**
10. **Any per-bundle build duration** — M5e retracted all of them, and no replacement was recorded.

### 5.4 The suspicion I hold about my own audit

Three specific places where I think I could be the next entry in `INGEST-CALIBRATION.md`:

1. **Q2b's laziness argument is a code-reading, not a measurement.** I am confident in it — the
   generators are explicit, `partial_source=True` is passed, `_drain_surplus` refuses to drain, and
   receipt-level conservation on 27 of 27 bundles corroborates it — but **the decisive experiment
   (instrument `_RangeFile.bytes_fetched` on one bundle) has not been run.** If I am wrong, the read
   is 18 TB and the two constants matter after all.

   **⚠️ And this is a self-inflicted OBSERVABILITY defect, which is why I had to reason instead of
   read.** `_RangeFile.bytes_fetched` counts exactly the quantity in question
   (`ingest_reservoir.py:306`, incremented at `:423`). The **ingest** path surfaces it — it
   aggregates into a `"bytes_fetched"` field in both index summaries (`ingest_reservoir.py:730`,
   `:802`, `:811`, `:899`, `:931`). The **build** path constructs the identical `_RangeFile`
   (`corpus_read.py:425`, `:684`) and **discards the counter**: `grep bytes_fetched
   src/edullm_data/corpus_read.py src/edullm_data/corpus_build.py` returns **nothing**. So the one
   number that settles "how many bytes did this build actually pull" is computed on every read and
   thrown away, while its sibling module reports it. **Printing it in `_cmd_run`'s `DONE` line
   (`corpus_build.py:707-711`, which already reports shards, tokens, docs, dupes and decon counts)
   is ~3 lines, and it would have made this entire section a lookup instead of an inference** —
   and it would let a 1.0T build report its own read amplification per bundle, live. Same shape as
   every other finding here: **the fix exists in one module and was left out of the other.**
2. **My 8.4 MB/s/child residual is a subtraction, not a measurement.** It assumes the 5.59 h
   residual is *all* read. F-DECON is an equally good candidate and the two are indistinguishable
   from the outside. **I have deliberately reported both rather than picking.**
3. **I trust M7's 85 min more than any other number here, and it is the one with the most
   downstream leverage.** It is corroborated three ways (the round-trip count reproduces 15.8 rt/s
   exactly; the byte model gives 0.129 MB/s against a live 0.1 MB/s; and M7d's independent
   6,913-object run agrees within 21%). But it was measured on a **4-vCPU** validator against a
   corpus in **one group**, and the 1.0T plan is two datasets. If per-group fixed costs matter,
   per-object scaling understates.

### 5.5 What I would do before spending anything

**Four measurements, all cheap, that between them collapse most of the bands above:**

| # | measurement | cost | resolves |
|---|---|---|---|
| 1 | **One single-bundle `run --of <n>` on the smallest source, with `bytes_fetched` and `cProfile` printed** | ~1 h, one child | HF CDN rate (5.1 row 1), F-DECON, Q2b's laziness — **all three of my widest bands, in one job** |
| 2 | **Read the reservoir publish job's CloudWatch start/stop** | 0 — it already ran | the `publish()` gap, which is my largest evidence hole |
| 3 | **`verify --deep --hash-workers 8` on the existing 1.005 TB corpus** | ~25 min, ~$0.20 | M8b's unattributed 7.82x, against a corpus whose correct answer is already known (PASS) |
| 4 | **`encode_batch` on a 128-vCPU box** | minutes | A3's linear-scaling assumption |

Measurement 1 is already **mandatory** by the code's own instruction
(`corpus_build.py:901-904`: *"Settle it with a single-bundle `run --of <n_bundles>` against the
smallest source (`ubuntu-irc`, 1.87B) before committing a full array"*) and
`pipeline-scale-audit.md:1202` marks it **⚠️ MANDATORY**. Adding `bytes_fetched` and a profile to
that already-required job costs nothing extra and settles three of my four widest uncertainties.

**And one free prediction, in the spirit of the trick this repo already invented** (predicting a
1 TB gate's verdict by running only its pure half, `HANDOFF.md:1197-1210`): `_reader_for`'s budget
arithmetic is a **pure function**. `bundle.tokens × 9.0 / keep_rate` versus what `pack` demands can
be computed for all ~100 planned bundles with **no network at all**, straight off `plan.json`. If
that says the budget never binds for any bundle — which it does for every case I checked — then
Q2b is settled for the price of a local script, and `_CHARS_PER_TOKEN` can be left alone with
confidence rather than argued about.
