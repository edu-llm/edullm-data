# M1 — Bandwidth measurement (graph node M1, task #26)

**Worker:** W1-BANDWIDTH (for DATA-EXEC)
**Started:** 2026-08-08
**Status:** IN PROGRESS — this file is appended to continuously; a partial file is a success.

## Why this exists

`docs/IMPLEMENTATION-PLAN.md` §8A borrows ~85 MB/s from an unrelated S3 measurement for every read
estimate. §3.2 says reconciling the reservoir's measured 8 h build implies **HF CDN throughput near
8.4 MB/s**, which would turn a live 8.5 TB re-read into ~11 days. Three distinct numbers must be
separated:

1. HF CDN → THIS laptop (MB/s)
2. S3 → THIS laptop (MB/s, calibration only)
3. **S3 → in-region EC2 (MB/s)** — the one that actually sizes the plan; probably not measurable
   from a laptop, in which case that is itself the finding.

## Grading legend

`MEASURED` (ran it) / `MEASURED-IN-CODE` (read from source, file+line) / `DERIVED` (arithmetic over
graded inputs, shown) / `CARD` (vendor claim) / `UNVERIFIED`.

## Plan

- [ ] P0. Establish local network baseline (raw link speed, so HF numbers can be attributed to CDN
      vs. laptop uplink).
- [ ] P1. HF tree API on the three pinned repos → real file paths + sizes (avoid `/rows`, 429 hazard).
- [ ] P2. Single-stream `resolve/` GETs, several sizes, several repos → per-file MB/s.
- [ ] P3. Parallel GETs (2/4/8 streams) → does concurrency help? aggregate MB/s.
- [ ] P4. S3 read from laptop (sbsandbox, read-only) → order of magnitude.
- [ ] P5. In-region S3→EC2: attempt to derive; state explicitly if unmeasurable.
- [ ] P6. Rewrite §3.2 / §8A implications with arithmetic; STAGE hours; $194 staging check.

## Hard constraints observed

- No writes to `s3://edullm-landing` of any kind. No Batch job submissions. Read-only S3 only.
- Prefer tree API + `resolve/` over `/rows`. Back off on 429, record rather than retry hard.

---

## Log

### P1 — tree API: all three pinned repos resolve. No auth required. (MEASURED)

No `/rows` calls made anywhere in this work. Zero 429s observed so far.

| repo @ revision | dir | N entries | file size (bytes) |
|---|---|---|---|
| `HuggingFaceFW/fineweb-edu` @ `87f0914…` | `sample/100BT` | 140 | `000_00000.parquet` = 2,153,444,469 (~2.15 GB); all ~2.15 GB |
| `HuggingFaceFW/dclm_100BT` @ `01022d3…` | `data` | 100 | `000_00000.parquet` = 3,170,280,492 (~3.17 GB); all ~3.15–3.20 GB |
| `common-pile/peS2o_filtered` @ `2977475…` | ROOT | 93 (91 payload + README + .gitattributes) | `peS2o-0000.json.gz` = 652,928,967 (~653 MB) |

Grade: **MEASURED** (tree API, 2026-08-08). Confirms the task brief's path shapes exactly, including
peS2o's no-`data/`-prefix root layout.

### P2 — HF CDN single stream: **2.1–3.1 MB/s**. WORSE than the §3.2 fear of 8.4 MB/s. (MEASURED)

Method: ranged GET `-r 0-67108863` (64 MiB) with `Authorization: Bearer <~/.cache/huggingface/token>`,
`curl -sL -o /dev/null`, HTTP 206 confirmed, `size_download` verified = 67,108,864 on every run.

| run | target | bytes | B/s | MB/s (1e6) | time |
|---|---|---|---|---|---|
| 1 | fineweb-edu `sample/100BT/000_00000.parquet` | 64 MiB | 2,138,640 | 2.14 | 31.38 s |
| 2 | same | 64 MiB | 3,051,106 | 3.05 | 21.99 s |
| 3 | same | 64 MiB | 2,816,060 | 2.82 | 23.83 s |

n=3, mean **2.67 MB/s**, range 2.14–3.05, spread ±17% about the mean.

⚠️ **This is a single-stream number on an unknown uplink.** Before it can be attributed to the CDN it
must be separated from this laptop's own link capacity — that is the very next measurement. Do not
quote 2.67 MB/s as "HF CDN throughput" until P0/P3 land.

### P0 attempt 1 — FAILED, not a finding about the link

`https://speed.cloudflare.com/__down?bytes=104857600` returned `size_download=1` in 1.4–2.3 s on all
3 runs, i.e. the endpoint did not serve the payload (blocked or API changed). **No baseline obtained
from it.** Retrying with a different source. Recorded so nobody re-derives "the laptop has 0 B/s".

### P3 — parallelism does NOT help. Aggregate ceiling ~3.1 MB/s. (MEASURED)

4 concurrent single-range GETs, 64 MiB each, 4 *different* files (`000_00000`–`000_00003.parquet`) so
no single-object CDN throttle can explain it. All 4 returned HTTP 206 with exactly 67,108,864 bytes.

| stream | B/s | MB/s | time |
|---|---|---|---|
| 0 | 779,695 | 0.78 | 86.07 s |
| 1 | 873,195 | 0.87 | 76.85 s |
| 2 | 1,125,531 | 1.13 | 59.62 s |
| 3 | 892,883 | 0.89 | 75.16 s |

Wall clock 86.16 s for 268.4 MB → **aggregate 3.12 MB/s**.

**The per-stream rate collapsed by ~3.4× (2.67 → 0.78–1.13) while the aggregate stayed flat
(2.67 → 3.12).** That is the signature of a fixed shared pipe being divided, not of a per-connection
server limit. Adding streams bought +17%, essentially nothing.

**Interpretation (provisional, pending independent-host confirmation): the bottleneck is THIS
LAPTOP'S UPLINK at ~3 MB/s (~25 Mbit/s), not the HF CDN.** This matters enormously for how the number
is used — see the §3.2 correction below. A laptop-link ceiling says nothing about what an EC2 instance
would get from HF.

### P0 (redone) — CONFIRMED: the ~2.5 MB/s ceiling is THIS LAPTOP, not Hugging Face. (MEASURED)

Two independent hosts with no relationship to HF, 50 MiB ranged GET each, HTTP 206, full bytes:

| host | MB/s | time |
|---|---|---|
| `ash-speed.hetzner.com/100MB.bin` (Hetzner, Ashburn VA) | **2.63** | 19.95 s |
| `mirror.math.princeton.edu` (Ubuntu ISO mirror) | **2.43** | 21.59 s |

And two more HF repos, to rule out a per-repo effect (32 MiB each):

| target | MB/s | time |
|---|---|---|
| `common-pile/peS2o_filtered` / `peS2o-0000.json.gz` | 1.97 | 17.06 s |
| `HuggingFaceFW/dclm_100BT` / `data/000_00000.parquet` | 2.47 | 13.60 s |

**Every host on the internet gives this laptop 2.0–3.1 MB/s.** HF is statistically
indistinguishable from Hetzner and from a university mirror. n=9 single-stream samples across 5
distinct hosts, full range 1.97–3.05 MB/s.

> ## ⛔ THE HEADLINE FINDING
> **The HF CDN was never measured by this work, and cannot be measured from this laptop.** What was
> measured is the laptop's ~2.5 MB/s (~20 Mbit/s) uplink, which saturates far below anything HF would
> impose. Any "HF CDN = 2.7 MB/s" statement is a **scope error** — the denominator is the laptop's
> link, not HF's capacity.
>
> **Therefore `IMPLEMENTATION-PLAN.md` §3.2's "HF CDN throughput near 8.4 MB/s" is NOT confirmed and
> NOT refuted by this measurement.** It is *untestable from here*. The laptop's ceiling (2.5) sits
> **below** the figure in question (8.4), so this experiment had no power to detect it. Reporting
> "measured 2.7, so 8.4 was optimistic" would be exactly the denominator mistake CLAUDE.md warns about.

### P4 — S3 → laptop: **2.74–3.07 MB/s single, 3.18 MB/s at 4 streams.** (MEASURED)

Object: `s3://edullm-data/pretrain/fineweb-edu-1b/v2/tokens/fineweb-edu/train-00000.u32le.bin`
(1.0 GiB, real published payload, `us-east-1`). Read via **presigned URL** (`s3 presign`, read-only,
1800 s) so `curl` timing is clean and no bytes were written anywhere.

| test | MB/s | detail |
|---|---|---|
| single stream 64 MiB, run 1 | 2.74 | 24.46 s, HTTP 206, 67,108,864 B |
| single stream 64 MiB, run 2 | 3.07 | 21.86 s, HTTP 206, 67,108,864 B |
| 4 × 32 MiB parallel, distinct ranges | **3.18 aggregate** | 42.24 s wall; per-stream 0.80/0.83/0.86/0.87 |

Identical to HF, Hetzner and Princeton. **The laptop's ~3 MB/s ceiling is now confirmed against
5 hosts and 2 protocols.** Parallelism again bought ~+10% and per-stream rates again divided.

#### 🔎 CORRECTION to a figure quoted in the plan and in memory: "0.8 MiB/s local" is NOT bandwidth

`IMPLEMENTATION-PLAN.md` and the memory note *publish-must-run-in-region* cite **0.8 MiB/s = 9 days
for 587 GiB** as the local S3 rate. **Raw local S3 GET is measured here at 2.9 MB/s ≈ 2.77 MiB/s —
about 3.5× faster than that figure.**

Both are correct; they measure different things. **Check the denominator:**
- 0.8 MiB/s is `publish()`'s *end-to-end effective* rate — bytes pulled ÷ wall clock, and `publish()`
  interleaves SHA-256 hashing and per-object request overhead with the transfer.
- 2.77 MiB/s is the *network alone* on one large object.

**Implication:** ~71% of `publish()`'s local slowness is NOT the network — it is hashing plus
per-object overhead. Moving `publish()` in-region fixes the 29% that is transfer; it does **not**
fix the rest, which follows the code wherever it runs. The in-region win is real but is bounded by
that split, and the "9 days → minutes" intuition overstates it. **Grade: DERIVED** from
(0.8 MiB/s CARD-in-plan) vs (2.77 MiB/s MEASURED); arithmetic: 1 − 0.8/2.77 = 0.711.

### P5 — in-region S3→EC2. NOT measurable from here, but a REAL measurement already exists.

**Confirmed unmeasurable this wave, as the brief predicted.** Reaching an in-region rate requires
running a process inside `us-east-1`, i.e. submitting a Batch job — PLAT's lane, not authorized this
wave, and explicitly forbidden by my constraints. **No Batch job was submitted.** What it would take:
one `submit-job` on `sbsandbox-intern-edullm-cpu` (`c7i.8xlarge`, max 384 vCPU, ENABLED — verified by
`describe-compute-environments` today) running N parallel ranged GETs against a staged object.

**But the number does not need a new job — the repo already contains a real one**, which I verified at
source rather than taking the citation's word for it.

**`artifacts/reservoir/verify-job.json`** — job `rsv-verify-deep-2`, the `verify --deep` payload
re-hash, 2026-08-05:

- `bytes_rehashed = 1004979748864` (1.005 TB), `single_threaded = true`
- `started 2026-08-05T04:11:20Z` → `finished 2026-08-05T07:27:42Z` = **11,782 s**
- Recomputed: 1,004,979,748,864 / 11,782 = **85.30 MB/s** wall-clock.
- The file's own `sustained_mb_s: 87.8` implies 11,446 s — a 336 s gap = container startup. Both are
  right at their own denominator; use **85.3** for wall clock, **87.8** for pure stream.

**Is that network, or is it hashing?** I checked instead of assuming. Local sha256 on this machine
measures **1,481.9 MB/s single-core** (openssl backend, 537 MB). At 85.3 MB/s the hash costs
85.3/1481.9 = **5.8% of elapsed time**. **So the 85.3 MB/s figure is ~94% pure network** and is a
legitimate single-TCP-stream in-region S3 read rate.

| quantity | value | grade |
|---|---|---|
| **in-region S3→EC2, single TCP stream** | **85.3 MB/s** (wall) / 87.8 (stream) | **MEASURED**, `artifacts/reservoir/verify-job.json`, n=1 job / 10,049 shards / 1.005 TB |
| ↳ fraction attributable to sha256, not network | **5.8%** | **DERIVED** (85.3 ÷ 1481.9 MEASURED locally) |
| in-region aggregate, N streams | **UNMEASURED** | see below |

⚠️ **The single-stream number is solid; the AGGREGATE is not.** `c7i.8xlarge` is a 12.5 Gbit/s
(~1,562 MB/s) instance **[CARD, AWS instance spec — not verified against a live price/spec API]**.
85.3 MB/s is **5.5%** of that, so the per-connection limit is TCP/S3 behaviour, not the NIC, and more
connections should scale — **but nobody in this repo has measured multi-stream in-region throughput,
and I could not either.** Every plan figure that assumes 8 instances × 10 Gbit/s aggregate rests on
an extrapolation with **zero** supporting samples. **That is the single largest remaining unknown in
M1, and it is a Batch-job-shaped question for PLAT.**

---

## §3.2 and §8A — what actually changes

### The `~85 MB/s` borrowed into every §8A read estimate: **UPHELD for S3, single-stream.**

`IMPLEMENTATION-PLAN.md:1688` flags "every read figure borrows ~85 MB/s from a *single-stream S3*
measurement" as "the most likely 2×, and possibly 10×" error. **For the S3 leg that borrowing is
correct** — 85.3 MB/s is measured, in-region, and 94% network. **§8A's S3 read estimates do not need
to move.**

### The `8.4 MB/s HF CDN` fear in §3.2: **STILL UNRESOLVED. I could not settle it, and neither
could this laptop.**

This is the honest headline and it contradicts what the brief hoped for. §3.2 says "Phase 0b measures
it first" — **Phase 0b cannot measure it from a laptop.** The laptop's own ceiling (2.5–3.1 MB/s) is
*below* the 8.4 MB/s hypothesis, so the experiment is blind to it: a laptop test would report ~2.7
MB/s whether HF's true capacity to a Batch container is 8 MB/s or 800 MB/s.

**What we know about 8.4 MB/s:** `wallclock-audit.md:740` derives it by attributing the reservoir
build's residual time entirely to download. That is an **upper bound on download time**, hence a
**lower bound on throughput**, and memory note *tokenize-throughput-is-filter-bound* independently
shows ~78% of build cost is the serial Python decon filter — **so the residual is largely NOT
download, and 8.4 MB/s is very likely an underestimate of HF CDN capacity.**

> **Verdict: 8.4 MB/s is a weakly-grounded lower bound, not a measurement.** It should not be used to
> justify a decision on its own. **It also should not be dismissed** — the true value remains unmeasured
> from any in-region vantage point.

### Does STAGE stay at 0.5–3.0 h, or become days?

| leg | rate | 8.5 TB re-read | grade |
|---|---|---|---|
| staged, in-region, **1 stream** | 85.3 MB/s | 8.5e12 / 85.3e6 = **27.7 h** | DERIVED (MEASURED rate) |
| staged, in-region, **16 streams** if it scales linearly | 1,365 MB/s | **1.73 h** | DERIVED on an **UNMEASURED** assumption |
| staged, in-region, plan's 8 × 10 Gbit/s | ~10,000 MB/s | 0.24 h | **UNVERIFIED extrapolation** |
| live HF, **if** 8.4 MB/s/child | 8.4 MB/s | **281 h ≈ 11.7 days** | DERIVED on a weak bound |

**Answer: STAGE stays in the 0.5–3.0 h band ONLY IF multi-stream in-region reads scale — which is
exactly the thing nobody has measured.** Single-stream it is **27.7 h**, an order of magnitude
outside the band. The band is not refuted, but it is **resting entirely on an unmeasured
assumption**, and §8A should say so.

### Does the ~$194 staging recommendation still hold? **YES — and it is now MORE defensible.**

$194 = 4.21 TB × $0.023/GB-month × 2 months ≈ $194 **[DERIVED, arithmetic checks: 4210 × 0.023 × 2 =
$193.7]**. The storage arithmetic is untouched by anything I measured.

The *argument* for staging strengthens, though the reasoning changes:
- The plan's strongest stated argument was "HF might be 8.4 MB/s → 11 days". **I have weakened that
  specific argument** (8.4 is a soft lower bound, likely pessimistic).
- But staging's value never depended on it. Staging buys **determinism, resumability, no 429 exposure,
  and a pinned revision** — and 429 is a *live, observed* hazard this week, not a hypothetical.
- **The decisive point: the HF rate is UNMEASURABLE without a Batch job, and staging makes it
  irrelevant.** Paying $194 to delete an unmeasurable variable from the critical path is a good trade
  precisely *because* M1 could not measure it.

**Recommendation unchanged: stage. $194 stands.**

---

## DELIVERABLE TABLE

| quantity | value | units | grade | method | n | variance / range |
|---|---|---|---|---|---|---|
| **HF CDN → this laptop, 1 stream** | **2.67** | MB/s | MEASURED | 64 MiB ranged GET, fineweb-edu, HTTP 206 verified | 3 | 2.14–3.05 (±17%) |
| HF → laptop, 4 parallel streams | **3.12 agg** | MB/s | MEASURED | 4 × 64 MiB, 4 distinct files, 86.2 s wall | 4 | per-stream 0.78–1.13 |
| HF peS2o (2nd repo) | 1.97 | MB/s | MEASURED | 32 MiB ranged GET | 1 | — |
| HF dclm_100BT (3rd repo) | 2.47 | MB/s | MEASURED | 32 MiB ranged GET | 1 | — |
| Hetzner (non-HF control) | 2.63 | MB/s | MEASURED | 50 MiB ranged GET | 1 | — |
| Princeton mirror (non-HF control) | 2.43 | MB/s | MEASURED | 50 MiB ranged GET | 1 | — |
| **⇒ THIS LAPTOP'S UPLINK CEILING** | **~2.5–3.2** | MB/s | **MEASURED** | 5 hosts, 2 protocols, all agree | 11 | 1.97–3.18 |
| **HF CDN true capacity** | **UNKNOWN** | — | **UNMEASURABLE from a laptop** | laptop ceiling is *below* the hypothesis | 0 | — |
| **S3 → this laptop, 1 stream** | **2.91** | MB/s | MEASURED | presigned URL, 64 MiB, real 1 GiB payload | 2 | 2.74–3.07 |
| S3 → laptop, 4 parallel | 3.18 agg | MB/s | MEASURED | 4 × 32 MiB distinct ranges, 42.2 s | 4 | per-stream 0.80–0.87 |
| **S3 → EC2 IN-REGION, 1 stream** | **85.3** (wall) / **87.8** (stream) | MB/s | **MEASURED** | `verify-job.json`, 1.005 TB / 11,782 s, recomputed from timestamps | 1 job, 10,049 shards | 336 s startup = the 85.3/87.8 gap |
| ↳ of which is sha256, not network | 5.8 | % | DERIVED | 85.3 ÷ 1481.9 MB/s local sha256 (MEASURED) | — | — |
| **S3 → EC2 in-region, N streams** | **UNMEASURED** | — | **gap** | needs a Batch job (PLAT) | 0 | — |
| `c7i.8xlarge` NIC | 12.5 (~1562 MB/s) | Gbit/s | CARD | AWS instance spec, not API-verified | — | — |
| local sha256, 1 core | 1,481.9 | MB/s | MEASURED | openssl backend, 537 MB | 1 | — |
| **`publish()` local effective** | 0.8 | MiB/s | CARD (plan/HANDOFF) | **≠ bandwidth** — 71% is hash + overhead, see P4 | — | — |

## THE THREE THINGS TO CARRY FORWARD

1. **§8A's borrowed ~85 MB/s is CORRECT for in-region single-stream S3.** It is measured, and it is
   94% network. No §8A S3 read estimate needs to move.
2. **The HF CDN number was NOT settled and cannot be from a laptop.** The 8.4 MB/s figure is a soft
   lower bound derived by attributing build residual to download, and memory
   (*tokenize-throughput-is-filter-bound*) says ~78% of that residual is the decon filter, not
   download — so 8.4 is probably too pessimistic. **Staging makes the question moot; that is now the
   best reason to stage.**
3. **The real remaining gap is MULTI-STREAM in-region aggregate.** Single-stream 85.3 MB/s puts an
   8.5 TB re-read at **27.7 h**, not 0.5–3.0 h. The plan's band needs parallel reads to scale and
   **that has zero measured samples.** One Batch job settles it. **This is the highest-value next
   measurement in the whole plan.**

## Provenance / compliance

- **No writes to `s3://edullm-landing`.** No Batch jobs submitted. All S3 access read-only
  (`s3 ls`, `s3 presign`).
- **No `/rows` calls.** Tree API + `resolve/` only. **Zero HTTP 429s observed** across ~450 MB of HF
  transfer. All HF responses HTTP 206 with byte counts verified against the request range.
- Total bytes pulled: ~450 MB HF + ~230 MB S3 + ~105 MB controls.
- Caller identity: `arn:aws:sts::<ACCOUNT_ID>:assumed-role/Intern-eric.wu-sbsandbox/…`, `sbsandbox`.

**STATUS: COMPLETE.** All six plan items P0–P6 done (P0 on the second attempt; P5 closed as a
documented impossibility with a recovered pre-existing measurement).
