# Wall-clock audit of the 251.2B reservoir build, and a 1.0T projection

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

<!-- Sections are appended one numbered question at a time. -->
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

