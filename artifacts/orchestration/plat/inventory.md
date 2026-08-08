# PLAT-EXEC read-only inventory: AWS Batch job history vs compute-env config

Worker session, `final-dataset` branch. STRICTLY READ-ONLY on AWS (describe-*/list-*/get-*/head-* only).
Account IDs scrubbed to `<ACCOUNT_ID>`. Every number graded MEASURED / DERIVED / UNVERIFIED.

Thesis under test: `state: ENABLED` in `describe-compute-environments` is NOT evidence a compute
shape can actually run. Build the real inventory from JOB HISTORY, not configuration.

Prior context supplied by orchestrator (not re-verified by me unless noted):
- All 16 compute envs are ENABLED/VALID (MEASURED by orchestrator, prior session).
- `sbsandbox-intern-edullm-cpu` = c7i.8xlarge, maxvCpus 384, desiredvCpus 0.
- `gpu-8xa100` = p4d/p4de.24xlarge, maxv 768, desiredvCpus 480 (only nonzero-desired env).
- `gpu-1xh100` = p5.4xlarge, maxv 384.
- `gpu-8xh100` = p5.48xlarge/p5en.48xlarge, maxv 768.
- Validator job def container: vcpus 4, memory 8192 (MEASURED by orchestrator, prior session).

---

## Progress log (append-only, most recent last)

- Session start. Directory created. This file initialized.

## A. Job queues and compute-environment mapping — MEASURED

`batch describe-job-queues`, account `sbsandbox`, region us-east-1. **16 job queues, all
`state: ENABLED` / `status: VALID` / `statusReason: "JobQueue Healthy"`, priority 1, each mapped
1:1 to a single compute environment of the same name** (order 1, no fallback CE). All 16 also carry
the same three `jobStateTimeLimitActions` (RUNNABLE -> CANCEL after 1800s) for
`CAPACITY:INSUFFICIENT_INSTANCE_CAPACITY`, `MISCONFIGURATION:COMPUTE_ENVIRONMENT_MAX_RESOURCE`,
`MISCONFIGURATION:JOB_RESOURCE_REQUIREMENT` — matches CLAUDE.md's note that the capacity reason
never actually fires because Batch leaves `statusReason` null for capacity failures.

| queue name | CE (same name) | CFN stack | maps to CPU env? |
|---|---|---|---|
| `sbsandbox-intern-edullm-cpu` | `sbsandbox-intern-edullm-cpu` | `sbsandbox-intern-edullm-phase3-batch` | **YES — the only CPU queue** |
| `sbsandbox-intern-edullm-gpu` | `sbsandbox-intern-edullm-gpu` | `sbsandbox-intern-edullm-phase4-gpu` | no (generic GPU queue, own stack) |
| `sbsandbox-intern-edullm-gpu-1xt4` | same | `...-phase4-gpu-shapes` | no |
| `sbsandbox-intern-edullm-gpu-4xt4` | same | `...-phase4-gpu-shapes` | no |
| `sbsandbox-intern-edullm-gpu-8xt4` | same | `...-phase4-gpu-shapes` | no |
| `sbsandbox-intern-edullm-gpu-1xl4` | same | `...-phase4-gpu-shapes` | no |
| `sbsandbox-intern-edullm-gpu-4xl4` | same | `...-phase4-gpu-shapes` | no |
| `sbsandbox-intern-edullm-gpu-8xl4` | same | `...-phase4-gpu-shapes` | no |
| `sbsandbox-intern-edullm-gpu-4xa10g` | same | `...-phase4-gpu-shapes` | no |
| `sbsandbox-intern-edullm-gpu-8xa10g` | same | `...-phase4-gpu-shapes` | no |
| `sbsandbox-intern-edullm-gpu-1xl40s` | same | `...-phase4-gpu-shapes` | no |
| `sbsandbox-intern-edullm-gpu-4xl40s` | same | `...-phase4-gpu-shapes` | no |
| `sbsandbox-intern-edullm-gpu-8xl40s` | same | `...-phase4-gpu-shapes` | no |
| `sbsandbox-intern-edullm-gpu-1xh100` | same | `...-phase4-gpu-shapes` | no |
| `sbsandbox-intern-edullm-gpu-8xh100` | same | `...-phase4-gpu-shapes` | no |
| `sbsandbox-intern-edullm-gpu-8xa100` | same | `...-phase4-gpu-shapes` | no |

Note: 16 queues, 16 compute envs (matches orchestrator's prior "all 16 compute envs" count) — **1:1
queue:CE mapping**, no queue fans out to multiple CEs or vice versa. So "queue" and "shape" are
interchangeable in this account. `sbsandbox-intern-edullm-gpu` (no shape suffix, from the older
`phase4-gpu` stack) is a separate legacy generic GPU queue predating the shape-specific
`phase4-gpu-shapes` stack that produced the other 14 GPU queues.

Account ID scrubbed as `<ACCOUNT_ID>` from here on (raw value seen: `<ACCOUNT_ID>` — replacing).

## B. Job history per queue — MEASURED (`batch list-jobs --job-status SUCCEEDED`, `--max-items 100`)

Ran `list-jobs` for `SUCCEEDED` across all 16 queues (no pagination NextToken seen in any response —
every queue's SUCCEEDED count is ≤100 and each response appears complete within Batch's retention
window). Numbers below count **jobSummaryList rows** (an array job is one row here; its internal
`arrayProperties.size` — e.g. 48 — is noted separately, not added to the row count, since we don't yet
know per-child capacity request for array jobs).

| queue (= shape) | SUCCEEDED rows | notable capacityUsage (VCPU, container-level) | most recent SUCCEEDED (UTC) |
|---|---|---|---|
| `sbsandbox-intern-edullm-cpu` | **84** | 2, 4, 8, 16, **32** (one job only) | 2026-08-08T05:39:38 |
| `sbsandbox-intern-edullm-gpu` (generic) | **~46** | mostly 4, some 8 | 2026-08-08T05:39:38 |
| `sbsandbox-intern-edullm-gpu-1xt4` | **13** | 4 | 2026-08-08T01:45:09 |
| `sbsandbox-intern-edullm-gpu-4xt4` | **0** | — | — |
| `sbsandbox-intern-edullm-gpu-8xt4` | **0** | — | — |
| `sbsandbox-intern-edullm-gpu-1xl4` | **10** | 4 | 2026-08-08T02:43:37 |
| `sbsandbox-intern-edullm-gpu-4xl4` | **0** | — | — |
| `sbsandbox-intern-edullm-gpu-8xl4` | **0** | — | — |
| `sbsandbox-intern-edullm-gpu-4xa10g` | **14** | mostly 48 | 2026-08-07T19:04:42 |
| `sbsandbox-intern-edullm-gpu-8xa10g` | **3** | 192 | 2026-08-05T15:52:38 |
| `sbsandbox-intern-edullm-gpu-1xl40s` | **10** | 4, one array size 18 | 2026-08-08T02:06:56 |
| `sbsandbox-intern-edullm-gpu-4xl40s` | **10** | mostly 48, two array size 6 | 2026-08-08T06:22:13 |
| `sbsandbox-intern-edullm-gpu-8xl40s` | **0** | — | — |
| `sbsandbox-intern-edullm-gpu-1xh100` | **0** | — | — |
| `sbsandbox-intern-edullm-gpu-8xh100` | **0** | — | — |
| `sbsandbox-intern-edullm-gpu-8xa100` | **21** | mostly 96, array sizes 3/18/27 | 2026-08-08T04:42:29 (earliest seen 2026-08-03T10:05:49) |

**THESIS CONFIRMED on the two headline claims, MEASURED, not just from prior session's config read:**
- **`gpu-8xa100` has real SUCCEEDED jobs** (21 rows, spanning 2026-08-03 through 2026-08-08 — i.e.
  recent and repeated, not a one-off). Job names include `df-proof-gpu-8xa100-b`,
  `memorysplit-adarsh-trace-a1-a100-exact-v6-20260803`, and many `run_<ulid>` jobs. This is real usage,
  not a smoke test.
- **`gpu-1xh100` has ZERO SUCCEEDED jobs.** `list-jobs --job-status SUCCEEDED` returned an empty
  `jobSummaryList`. Matches the thesis exactly.
- **New finding beyond the thesis's own framing: `gpu-8xh100` is ALSO zero**, and so is
  **`gpu-8xl40s`** — while `gpu-1xl40s` and `gpu-4xl40s` both have successes. So the "scale cliff" isn't
  H100-specific: the account has a **repeating pattern of the 8x-per-instance shape failing while
  1x/4x of the same GPU family succeeds** (L40S: 1x/4x yes, 8x no; H100: both x1 and x8 no — the whole
  H100 family is unproven, not just the flagship 8x). Also **`4xl4`, `8xl4`, `4xt4`, `8xt4` are all
  zero** — only `1xt4` and `1xl4` have ever succeeded in the T4/L4 family. So the zero-success set is
  larger than "H100 only": `{1xh100, 8xh100, 8xl40s, 4xl4, 8xl4, 4xt4, 8xt4}` — 7 of 15 GPU-shaped
  queues (excluding the generic `gpu` queue) have never produced a SUCCEEDED job, at least not within
  whatever window `list-jobs` is retaining.
- Grade: **MEASURED** that these specific queues return zero rows for SUCCEEDED today
  (2026-08-08). Per CLAUDE.md and the task brief, `list-jobs` is understood to have a **short
  retention window (~24h after completion is the documented Batch behavior)** — but several queues
  above show SUCCEEDED rows going back to 2026-08-01, a full week, so **that 24h claim looks wrong for
  this account/API surface**, or `list-jobs` retention is longer than commonly cited. Flagging this
  explicitly: **the "zero rows" queues could still be "never attempted" OR "attempted and failed silently
  with no FAILED record either" OR "attempted outside whatever true retention boundary exists"** —
  checking FAILED status and CloudWatch logs next to disambiguate.

## B2. FAILED-status disambiguation — MEASURED (`batch list-jobs --job-status FAILED`, and `RUNNABLE` spot-checks)

Ran `FAILED` for the 7 zero-SUCCEEDED queues plus `cpu` and `gpu-8xa100` (for contrast), plus
`gpu-8xa10g` (to sanity-check the "8x cliff" framing), plus `RUNNABLE` spot-checks on the 4 queues
that came back with zero FAILED too. **This materially changes the B1 framing** — "zero SUCCEEDED"
was being treated as one bucket; it is actually three different situations:

**Bucket A — attempted, explicitly capacity-denied (richest evidence, confirms the thesis almost
verbatim):**
- `gpu-1xh100`: **5 FAILED rows.** Two are unrelated smoke-test jobs (4 VCPU, application-level
  reasons). The other three are the important ones — VCPU 16 each, `statusReason` (self-reported by
  a prior session's cancel/update call, not a native AWS capacity code, but present verbatim in the
  API response):
  > "cancelled by platform: gpu-1xh100 cannot place a job. EC2 has returned InsufficientInstanceCapacity
  > for every p5.4xlarge launch in every availability zone and this account has never held one. The
  > queue's 1800s RUNNABLE cancel cannot fire because Batch leaves statusReason null for capacity
  > failures, so this job would have waited indefinitely with no error. gpu-1xh100 is now off the
  > submission form. Resubmit on gpu-8xa100, which has capacity."
  This is a prior session's own diagnostic conclusion, sitting in AWS job history — it independently
  corroborates the thesis's exact framing (capacity, not quota; form removed; gpu-8xa100 works).
- `gpu-8xh100`: **16 FAILED rows**, all VCPU 192 (matches p5.48xlarge total vCPU). Multiple explicit
  probes: `h100-capacity-probe-*` → "capacity probe complete: p5.48xlarge InsufficientInstanceCapacity
  in all five reachable AZs over 9h"; `diag-p5-statusreason-*` → "diagnostic complete: p5.48xlarge
  InsufficientInstanceCapacity in all reachable AZs; **confirmed statusReason stays null so the 1800s
  CAPACITY cancel cannot match**." That second one is a direct, independent confirmation of the
  CLAUDE.md claim about the dead cancel rule — and its own existence as a FAILED row with a
  human-readable reason implies a **prior session had to manually terminate/update** a RUNNABLE job
  after ~9h, because the automatic 1800s cancel genuinely never fired on the null reason.
  **Both H100 shapes (1x and 8x) have been actively attempted and both are capacity-denied,
  not merely unconfigured.**

**Bucket B — attempted, but capacity was NOT the blocker (real placement, application-level failure):**
- `gpu-4xl4`: **1 FAILED row**, VCPU 48, real `startedAt`/`stoppedAt`, `statusReason: "Essential
  container in task exited"`, `exitCode: 1`. The container placed and ran — this shape **does** get
  capacity; it just hasn't had a successful run yet. Different failure mode than H100; should not be
  lumped into "capacity cliff."
- `gpu-8xa10g` (checked for contrast with the "8x cliff" idea, not one of the original 7): **4 FAILED
  rows**, all VCPU 192, all with real `startedAt`/`stoppedAt` — reasons are a smoke-test cutoff, two
  timeouts, and a manual stop for an unrelated resubmit — **zero capacity-denial reasons**, plus B1
  already found 3 SUCCEEDED rows here. **This refutes my own B1 framing of a blanket "8x-per-instance
  cliff."** `gpu-8xa100` and `gpu-8xa10g` both run fine at 8x. Only `gpu-8xh100` and `gpu-8xl40s` fail
  (or show zero history) at 8x — so whatever pattern remains is **GPU-model-specific** (H100 is a
  well-known global shortage SKU), not a property of "8x" scale itself.

**Bucket C — no job history at all, in any status (best-available evidence for "never attempted"):**
- `gpu-8xl40s`, `gpu-8xl4`, `gpu-4xt4`, `gpu-8xt4`: **zero rows for SUCCEEDED (B1) AND zero rows for
  FAILED (this section) AND zero rows for RUNNABLE** (checked all four explicitly). No RUNNING/
  STARTING/PENDING/SUBMITTED check was run (those only matter for something mid-flight *right now*,
  a live-state question, not a history one). Grade: **UNVERIFIED — never attempted, to the extent
  `list-jobs` retention covers it** (B1 already flagged this account's effective retention looks like
  at least a week, so "no rows in any status" is decent — not perfect — evidence of "never submitted,"
  rather than "submitted and fell outside a 24h window").

**Revised zero-SUCCEEDED breakdown (supersedes the flat "7 of 15" framing in B1):**

| shape | SUCCEEDED | FAILED | verdict |
|---|---|---|---|
| `gpu-1xh100` | 0 | 5 (3 capacity-coded) | **capacity-denied, actively attempted** |
| `gpu-8xh100` | 0 | 16 (2+ capacity-coded) | **capacity-denied, actively attempted, repeatedly** |
| `gpu-8xl40s` | 0 | 0 | **no history in any status — never attempted (best guess)** |
| `gpu-4xl4` | 0 | 1 (app-level, not capacity) | **attempted, got capacity, app bug — not a capacity finding** |
| `gpu-8xl4` | 0 | 0 | **no history in any status — never attempted (best guess)** |
| `gpu-4xt4` | 0 | 0 | **no history in any status — never attempted (best guess)** |
| `gpu-8xt4` | 0 | 0 | **no history in any status — never attempted (best guess)** |

So of the original "zero-success 7," only **2 shapes (`1xh100`, `8xh100`) are actually demonstrated
capacity failures** — which happen to be exactly the thesis's own headline shape plus its 8x sibling.
The other 5 are either unattempted or attempted-but-not-capacity-limited. **This sharpens the thesis
rather than contradicting it: the account's zero-evidence GPU shapes are mostly unexplored, not
proven-broken — H100 (both sizes) is the one family with hard, repeated, self-diagnosed capacity
denial on record.**

## B3. CPU-queue 96-VCPU FAILED jobs vs. single-instance-type CE — resolved discrepancy, MEASURED

B1/B2's CPU-queue FAILED sample contains many rows with `capacityUsage` VCPU **96**, several with
real `startedAt`/`stoppedAt` (i.e., actually ran, then failed at the application level — not a
placement failure). This looked like a contradiction: the CE has `maxvCpus: 384` but only ONE
instance type. Re-ran `batch describe-compute-environments --compute-environments
sbsandbox-intern-edullm-cpu` directly (independent re-verification, not just trusting the prior
session's summary) and confirmed:

```
"computeResources": { "instanceTypes": ["c7i.8xlarge"], "minvCpus": 0, "maxvCpus": 384, "desiredvCpus": 0 }
```

**Single instance type confirmed, MEASURED first-hand — c7i.8xlarge only, 32 vCPU/instance.** A
single-container job requesting 96 vCPU cannot fit on one 32-vCPU instance under normal (non-array,
non-multi-node) EC2 Batch placement. The 96-VCPU FAILED rows that show real `startedAt`/`stoppedAt`
are therefore most plausibly **AWS Batch multi-node parallel jobs** (e.g. 3 nodes × 32 vCPU = 96
total `capacityUsage`, spread across 3 instances) rather than evidence the CE secretly allows a
bigger instance type. **Grade: UNVERIFIED** — confirming this needs `batch describe-jobs` on a
specific `jobId` to inspect `nodeProperties`, which was not run (in scope, just not yet done; cheap
to add if this matters later). What IS resolved: **the CE truly has only `c7i.8xlarge`** — the
384/32=12 concurrency arithmetic in Task C is not undermined by this.

## D. CloudWatch Logs as an independent evidence source — MEASURED

`logs describe-log-groups --log-group-name-prefix /aws/batch` returns exactly **4** groups, not the
1 generic one CLAUDE.md's own framing might suggest:

| log group | retention | stored bytes | created (UTC) | what's in it |
|---|---|---|---|---|
| `/aws/batch/job` | (none set = never expire) | **0** | 2026-07-27ish | confirmed genuinely empty — never used by this account's job defs. AWS's own default group; irrelevant here. |
| `/aws/batch/sbsandbox-intern-edullm-cpu` | 90 days | 432,410 | **2026-07-28T02:55:14Z** | CPU-queue job logs |
| `/aws/batch/sbsandbox-intern-edullm-gpu` | 90 days | 356,959,335 (~357 MB) | **2026-07-28T21:48:10Z** | GPU-queue job logs, all 15 shapes + generic |
| `/aws/batch/edullm-prm800k` | 14 days | 490 | 2026-07-31T20:05:35Z | a third, job-def-named (not CE-named) group — matches the separate PRM/vendored image line from memory, not part of the 16-queue reservoir/corpus-build inventory; noted, not chased further |

**This is a materially important finding for retention, independent of the Task under test:** both
real Batch log groups were **created 2026-07-28**, i.e. this whole GPU/CPU compute-environment setup
is only **~11 days old** as of 2026-08-08, and retention is 90 days — so **CloudWatch evidence covers
the CE's entire lifetime to date, with zero left-censoring.** A "zero log streams for shape X" result
below is not "zero in the retention window," it is **zero, ever, since this compute environment was
created.** That's a stronger claim than anything `list-jobs` alone could support, and it resolves
B1's open worry about `list-jobs`' own retention window being unclear — CloudWatch has none of that
ambiguity here.

Stream names are prefixed by shape (`<shape>-run/default/<uuid>`, e.g. `gpu-8xa100-run/`,
`gpu-4xl40s-run/`, generic `gpu-run/`, `cpu-run/`, `validator/`) — this lets each shape's log
existence be queried directly with `--log-stream-name-prefix`, independent of `list-jobs` retention.
**A log stream can only be created once a container has actually been placed on an instance** (the
ECS/Batch agent creates the stream when the task starts) — so this is a stronger placement signal
than a Batch `FAILED` row with a self-authored `statusReason`, which (per B2 Bucket A) can describe a
job that was cancelled from `RUNNABLE` and **never got a container at all**.

Ran `describe-log-streams --log-stream-name-prefix <shape>-run` against
`/aws/batch/sbsandbox-intern-edullm-gpu` for every Bucket A/B/C shape from B2, plus `gpu-8xa10g` as a
cross-check:

| shape | log streams found | earliest / latest (UTC) | cross-check against B1/B2 |
|---|---|---|---|
| `gpu-1xh100` | **0** | — | Matches B2 Bucket A. **Zero containers ever placed, in the CE's entire 11-day life** — stronger than "5 FAILED rows," since it confirms none of those 5 ever got an instance either. |
| `gpu-8xh100` | **0** | — | Matches B2 Bucket A, and sharpens it: **16 FAILED rows exist in Batch history, but 0 log streams exist.** This means every one of those 16 was a pre-placement (RUNNABLE-stage) capacity cancel, never a running container — consistent with, and stronger evidence for, "capacity-denied" than statusReason text alone. |
| `gpu-8xl40s` | **0** | — | Matches B2 Bucket C ("never attempted"). Now upgraded from "no rows in list-jobs" to "no container ever placed in 11 days of CE lifetime." |
| `gpu-4xl4` | **1** | 2026-08-08T02:01:56Z | Matches B2 Bucket B exactly — corroborates the 1 FAILED row's claim of "real placement, app-level failure." One real container ran; consistent with one real (non-capacity) failure. |
| `gpu-8xl4` | **0** | — | Matches B2 Bucket C. |
| `gpu-4xt4` | **0** | — | Matches B2 Bucket C. |
| `gpu-8xt4` | **0** | — | Matches B2 Bucket C. |
| `gpu-8xa10g` (cross-check, not a B2 zero-shape) | **7** | 2026-08-03T16:21:14Z → 2026-08-05T16:01:36Z | **Exact match**: B1 found 3 SUCCEEDED + B2 found 4 FAILED = 7 total attempts on this shape, and exactly 7 log streams exist. This is a clean internal-consistency check — CloudWatch and `list-jobs` agree perfectly here, with no hidden extra attempts and no missing ones, which raises confidence in the "0 streams = 0 attempts" reading for the shapes above too. |

**Every one of these 8 checks corroborates, and none contradicts, the B2 bucket assignments.**
Grade: **MEASURED**. The H100 finding is now the strongest in the file: two independent AWS APIs
(Batch job history AND CloudWatch Logs container-placement evidence), covering the compute
environment's entire operational lifetime with no retention ambiguity, both show **zero successful
container placements ever, on either H100 shape.**

## D2. Cost Explorer by INSTANCE_TYPE — MEASURED (`ce get-cost-and-usage`, monthly, 2026-05-01 to 2026-08-08)

Independent third evidence source (billing, not Batch or logs at all). Grouped by `INSTANCE_TYPE`,
`UnblendedCost`, monthly granularity, full available history:

- **May 2026, June 2026: only a single `NoInstanceType` line ($3.03, $12.75).** No compute-instance-tagged
  cost of any kind — consistent with the log-group evidence above that the CPU/GPU compute
  environments weren't created until **2026-07-28**; there was nothing to bill before that.
- **July 2026** (full month, `Estimated: false` — final billed data): instance types billed include
  `c6i.2xlarge/8xlarge` ($0), `c7i.8xlarge` (**$0**, `-0` exactly), `g4dn.xlarge`/`g5.*`/`g6.*`/`g6e.xlarge`
  (all $0 or a few cents — smoke tests), and crucially **`p4d.24xlarge` $138.30** and **`p4de.24xlarge`
  $79.31** (both are `gpu-8xa100`'s instance family — real, substantial, corroborating spend). **Zero
  line items at all for `p5.4xlarge`, `p5.48xlarge`, `p5en.48xlarge`, or `g6e.48xlarge`** (H100 x1/x8 and
  L40S x8's instance types) — not "$0," **absent from the group-by entirely**, meaning EC2 never even
  opened a billing record for these types that month.
- **Aug 1–8 2026** (partial month, `Estimated: true` — flagged explicitly by the API, so read with more
  caution): same pattern repeats — **`p4d.24xlarge` $1,589.92**, **`p4de.24xlarge` $84.23** (both real,
  much higher than July, consistent with `gpu-8xa100` being the account's actual production GPU shape
  this month per B1's SUCCEEDED list going into August). Still **zero `p5.*`/`p5en.*`/`g6e.48xlarge` line
  items.** A small `g6e.12xlarge` entry appears at **-$0.0000000001** — ten-billionths of a dollar, a
  rounding/credit artifact, not real usage; flagging as a minor open wrinkle (Bucket-B/`gpu-4xl40s` has
  10 real SUCCEEDED jobs with `capacityUsage=48`, which is g6e.12xlarge-shaped, so I'd naively expect
  nonzero cost there — possibly explained by Spot pricing + short job durations rounding to near-zero
  at monthly granularity, or by consolidated-billing attribution landing on a different linked account
  than the one this CE query is scoped to. **UNVERIFIED**, does not change any headline conclusion,
  not chased further given effort budget).
- **Notable finding outside the 16-queue inventory entirely**: July 2026 shows **`p6-b200.48xlarge`
  $3,567.25** — a real, large, billed GPU expenditure for an instance type (B200) that has **no
  corresponding job queue or compute environment anywhere in Task A's 16-row inventory.** This is not
  Batch-visible at all. Flagging explicitly for PLAT-EXEC: **something in this account obtained B200
  capacity and spent real money on it, through a mechanism this inventory does not cover** (likely a
  directly-launched EC2 instance or a SageMaker training job, not AWS Batch). Out of scope to chase
  further under this brief, but relevant context if the real question upstream is "can this account get
  GPU capacity at all" rather than "can *these 16 Batch queues* get it."
- **Grade: MEASURED**, and this is a genuinely independent (billing-system) corroboration of the
  H100/8xL40S capacity-denial conclusion already reached via Batch job history and CloudWatch Logs —
  three unrelated AWS subsystems now agree.

## D3. EC2 service quotas — MEASURED (`service-quotas get-service-quota`)

(`service-quotas list-service-quotas --service-code ec2` itself **timed out with a 408** both times
tried — noting as an operational quirk, not a finding; substituted targeted `get-service-quota` calls
by known quota code, both of which returned immediately.)

| quota code | name | value | covers |
|---|---|---|---|
| `L-417A185B` | Running On-Demand P instances | **768 vCPU** | all P-family (p2/p3/p4/p5/p5en) on-demand vCPUs, account-wide |
| `L-1216C47A` | Running On-Demand Standard (A,C,D,H,I,M,R,T,Z) instances | **1,152 vCPU** | includes C-family, i.e. `c7i.8xlarge` |

**Both quotas comfortably exceed what any single H100 or CPU shape would need**: one `p5.48xlarge` is
192 vCPU (768/192 = room for 4 concurrently), one `p5.4xlarge` is 32 vCPU (768/32 = room for 24
concurrently) — so the P-family account quota is nowhere close to the constraint on `gpu-1xh100`/
`gpu-8xh100`. This is a fourth independent corroboration that the blocker is **physical capacity, not
an account quota ceiling** (matches CLAUDE.md's "capacity, not quota" framing verbatim).

**New finding beyond the original brief**: the CPU compute environment's own `maxvCpus: 384` (from B3,
re-confirmed MEASURED) is **only one third of the account's Standard-family quota (1,152 vCPU)** — i.e.
`sbsandbox-intern-edullm-cpu` could be reconfigured up to 36 concurrent `c7i.8xlarge` instances (1152/32)
without touching any account-level quota at all. **The 384-vCPU / 12-instance ceiling is a
compute-environment configuration choice, not a quota limit** — worth flagging to PLAT-EXEC as
distinct from "the account can't get more CPU capacity."

## C. CPU-queue concurrency arithmetic — DERIVED / UNVERIFIED

- **384 maxvCpus / 32 vCPU per `c7i.8xlarge` = 12 concurrent instances.** Grade: **DERIVED** (simple
  division on two MEASURED CE fields, re-confirmed first-hand in B3).
- **Has 12-way concurrency on this queue ever actually been demonstrated?** From B1: **84 SUCCEEDED
  rows total, but only ONE ever requested 32 vCPU** (i.e., ran on a full `c7i.8xlarge` alone) — the
  rest requested 2/4/8/16. A live snapshot taken this session (`list-jobs --job-status RUNNING` and
  `--job-status RUNNABLE` on this queue) returned **zero rows for both** — nothing is in flight right
  now. Nothing in the SUCCEEDED/FAILED history sampled in B1–B3 shows multiple concurrently-`RUNNING`
  32-vCPU jobs; Batch's `list-jobs` doesn't report concurrency directly (no timestamp-overlap check
  was run across all 84 SUCCEEDED rows' `startedAt`/`stoppedAt` — that would be the rigorous way to
  settle this and was not done, effort budget). **Grade: UNVERIFIED — obtainability of the full
  384-vCPU / 12-instance ceiling has never been demonstrated in any evidence gathered this session.**
  The account's own Standard-family quota (D3, 1,152 vCPU) would technically allow it, and there is no
  capacity-denial evidence on this queue at all (unlike H100) — so nothing found here suggests it
  *can't* work, only that nobody has *shown* it working. That distinction (capacity absent vs. capacity
  merely unexercised) is exactly the one CLAUDE.md/the thesis asks to preserve rather than collapse.

## E. Instance-type-offering check — MEASURED (`ec2 describe-instance-type-offerings`)

`describe-instance-type-offerings --location-type availability-zone` for
`c7i.8xlarge,p4d.24xlarge,p5.4xlarge,p5.48xlarge,p5en.48xlarge,g6e.48xlarge` in us-east-1:

- **`p5.4xlarge` and `p5.48xlarge` are each offered in 6 of 6 checked AZs** (1a/1b/1c/1d/1e/1f) —
  i.e., every AZ. `p5en.48xlarge` in 2 AZs (1b/1d), `g6e.48xlarge` in 4 AZs (1a/1b/1c/1d), `p4d.24xlarge`
  in 4 AZs (1a/1b/1c/1d), `c7i.8xlarge` in 6 AZs.
- **This is the SAME epistemic trap the thesis is about, one layer down.** "Offered" here means AWS
  sells this SKU as a catalog entry in that AZ — it says nothing about whether EC2 currently has spare
  physical hosts to fulfil a launch request. `p5.4xlarge`/`p5.48xlarge` being offered in literally every
  AZ checked is exactly consistent with the FAILED-job statusReason text quoted in B2 ("InsufficientInstanceCapacity
  for every p5.4xlarge launch in every availability zone") — the offering existing everywhere and the
  capacity being unavailable everywhere are **not in tension**; they are two different AWS concepts
  (catalog vs. physical fleet) that this task's whole thesis warns not to conflate. Grade: **MEASURED**
  for the offerings themselves; **the "AZ offering ≠ capacity" reading is the same UNVERIFIED-elevated-to-refuted
  pattern as `state: ENABLED`**, now confirmed at the EC2 API layer too, not just the Batch CE layer.

## F. Consolidated verdict — supersedes all prior partial tables

| shape (queue) | SUCCEEDED (B1) | FAILED (B2) | log streams (D) | billed cost (D2) | verdict |
|---|---|---|---|---|---|
| `gpu-8xa100` | **21** | (not exhaustively checked, has successes) | (not re-checked, redundant) | **real, substantial** ($138→$1,590/mo) | **PROVEN — runs today, in production use** |
| `gpu-8xa10g` | 3 | 4 (non-capacity) | 7 (exact match) | not isolated in CE data | **PROVEN — runs, some app-level failures unrelated to capacity** |
| `gpu-4xl40s` | 10 | — | (not re-checked) | ~$0 (unexplained, D2) | **PROVEN via job history; billing corroboration inconclusive** |
| `gpu-4xl4` | 0 | 1 | 1 (matches) | not isolated | **attempted, placed, app-level bug — NOT a capacity finding** |
| `gpu-1xh100` | **0** | 5 (3 capacity-coded) | **0** | **$0, absent from billing entirely** | **CAPACITY-DENIED — confirmed by 3 independent AWS subsystems (Batch history, CloudWatch Logs, Cost Explorer), across the CE's entire 11-day lifetime** |
| `gpu-8xh100` | **0** | 16 (2+ capacity-coded) | **0** | **$0, absent from billing entirely** | **CAPACITY-DENIED — same 3-subsystem confirmation; FAILED rows now shown to be pre-placement cancels, never a running container** |
| `gpu-8xl40s` | 0 | 0 | **0** | **$0, absent from billing entirely** | **best-available evidence: never attempted, across the CE's entire 11-day lifetime — not proven broken, just unexercised** |
| `gpu-8xl4`, `gpu-4xt4`, `gpu-8xt4` | 0 | 0 | 0 (checked, `8xl4`/`4xt4`/`8xt4`) | not isolated | **never attempted, same as above** |
| CPU queue (`c7i.8xlarge`) | 84 (max 32 vCPU/job seen) | some (96 VCPU, likely multi-node, B3) | — | $0 in July, $0 in Aug (both exact `-0`) | **works at small scale; the 384-vCPU/12-instance ceiling is UNVERIFIED — never demonstrated, and is itself a CE-config choice, not a quota limit (D3)** |

**Bottom line, matching the thesis almost exactly**: `state: ENABLED` said nothing false about
`gpu-1xh100` or `gpu-8xh100` being *configured* — they are. But three independent, first-hand-MEASURED
AWS evidence sources (Batch job history, CloudWatch Logs container-placement records spanning the
compute environment's entire operational life, and Cost Explorer billing records) agree: **neither
H100 shape has ever produced a running container or a dollar of billed cost, while `gpu-8xa100` has
done both repeatedly and recently.** The account's own EC2 service quotas rule out "quota" as the
explanation. The one part of the original 7-shape "zero-SUCCEEDED" framing that does **not** hold up
under FAILED/logs/cost cross-checking is the blanket "8x cliff" — `gpu-8xa100` and `gpu-8xa10g` both
run fine at 8x; the pattern that survives every cross-check is **GPU-model-specific (H100), not
scale-specific.**


