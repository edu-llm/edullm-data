# CPU / H100 compute-environment verification — sbsandbox, us-east-1

Verified 2026-08-08. Account `056956104102` (`sbsandbox`), region `us-east-1`.
Identity: `eric.wu@alphaaiengineering.com`, employeeType `INTERN`, via `mcp__sb-aws__whoami`.

All AWS calls are read-only describe/get/list through the `mcp__sb-aws__aws` MCP broker.
No job was submitted, no instance started, no resource modified.
`iam:simulate-principal-policy` was NOT used (known to give false denials for this role).

Grades: **MEASURED** = an API call returned the value. **DERIVED** = arithmetic on measured
values, inputs named. **UNVERIFIED** = could not establish.

---

## 1. CPU compute environment `sbsandbox-intern-edullm-cpu` — MEASURED

Command:

```
aws batch describe-compute-environments --region us-east-1 --output json
```

Returned, verbatim, for `sbsandbox-intern-edullm-cpu`:

| field | value |
|---|---|
| `computeEnvironmentName` | `sbsandbox-intern-edullm-cpu` |
| `type` (CE) | `MANAGED` |
| `state` | `ENABLED` |
| `status` | `VALID` |
| `statusReason` | `ComputeEnvironment Healthy` |
| `computeResources.type` | **`EC2`** (not SPOT, not FARGATE) |
| `allocationStrategy` | `BEST_FIT_PROGRESSIVE` |
| `minvCpus` | **0** |
| `desiredvCpus` | **0** |
| `maxvCpus` | **384** |
| `instanceTypes` | `["c7i.8xlarge"]` — single type |
| `subnets` | 5: `subnet-0bbe2b7870da13713`, `subnet-0a4235fb98b63930f`, `subnet-0fd5ed8accae254dc`, `subnet-08792525c62ba31c0`, `subnet-01f4bf9a051404a37` |
| `instanceRole` | `.../sbsandbox-intern-edullm-batch-instance` |
| `ec2Configuration` | `imageType: ECS_AL2023`, `batchImageStatus: LATEST` |
| `updatePolicy.jobExecutionTimeoutMinutes` | **30** |
| `uuid` | `438ec46b-9302-3793-bb40-993ca9319225` |

**CLAIM A element-by-element: maxvCpus 384 CONFIRMED. instanceTypes = [c7i.8xlarge]
CONFIRMED. state ENABLED CONFIRMED.**

The doc assertion "caps at 128 vCPU on one `c7i.8xlarge` type" is **REFUTED on the number**
(384, not 128) and **CONFIRMED on the type** (one type, `c7i.8xlarge`).

DERIVED (inputs: maxvCpus 384; `c7i.8xlarge` = 32 vCPU per instance, AWS instance-type
definition): 384 / 32 = **12 concurrent `c7i.8xlarge` instances** at full scale-out.

Note also `desiredvCpus: 0` and `minvCpus: 0` — the CE is scaled to zero right now, so
384 has not been demonstrated by an actual scale-out in this snapshot.

## 2. EC2 On-Demand Standard vCPU quota — MEASURED — **1152, does NOT bind**

Command:

```
aws service-quotas get-service-quota --service-code ec2 \
  --quota-code L-1216C47A --region us-east-1 --output json
```

Returned:

```
"QuotaName": "Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances",
"QuotaCode": "L-1216C47A",
"Value": 1152.0,
"Adjustable": true,
"QuotaAppliedAtLevel": "ACCOUNT"
```

**The quota is 1152 vCPU — three times the CE's 384.** So the EC2 standard-instance quota is
NOT the binding constraint on the CPU CE. The CE's own `maxvCpus: 384` is the binding
configured ceiling.

Spot quota `L-34B43A08` is **not applicable**: `computeResources.type` is `EC2` (On-Demand),
not `SPOT`. Checked for completeness below anyway.

DERIVED (inputs: quota 1152, CE max 384): there is **768 vCPU of unused standard-instance
quota headroom**, i.e. the CE could be raised to 1152 by a config change alone, with no AWS
quota-increase ticket. That is an infra-config change, not something this verification did.

Caveat on the quota being account-wide: the 1152 is shared by every On-Demand standard
instance in the account, including any non-Batch EC2. See §5 for the observed non-zero
`desiredvCpus` on the GPU environments (those are P/G families, counted against *different*
quotas, so they do not consume the 1152).

## 3. Job queues attached to the CPU CE — MEASURED — exactly one, NOT shared

Command:

```
aws batch describe-job-queues --region us-east-1 --output json
```

16 job queues exist. **Exactly one references the CPU compute environment:**

| field | value |
|---|---|
| `jobQueueName` | `sbsandbox-intern-edullm-cpu` |
| `state` | `ENABLED` |
| `status` | `VALID` / `JobQueue Healthy` |
| `priority` | 1 |
| `computeEnvironmentOrder` | **exactly 1 entry**: `order: 1` → `.../compute-environment/sbsandbox-intern-edullm-cpu` |
| `jobQueueType` | `ECS` |
| CFN stack | `sbsandbox-intern-edullm-phase3-batch`, logical id `BatchJobQueue` |

**No queue-fan-in contention risk from configuration.** The mapping is strictly 1 queue ↔ 1 CE.
The other 15 queues each map 1:1 to a *GPU* CE (`gpu`, `gpu-1xt4`, `gpu-4xt4`, `gpu-8xt4`,
`gpu-1xl4`, `gpu-4xl4`, `gpu-8xl4`, `gpu-1xl40s`, `gpu-4xl40s`, `gpu-8xl40s`, `gpu-4xa10g`,
`gpu-8xa10g`, `gpu-8xa100`, `gpu-1xh100`, `gpu-8xh100`) — all `ENABLED`, all `priority: 1`, all
with a single-entry `computeEnvironmentOrder`. Zero CEs are targeted by two queues.

So contention on the CPU CE can only come from **other jobs on the same single queue**
(different submitters, or our own array jobs), not from queue sharing.

### Undocumented finding: every queue auto-CANCELs stuck jobs after 1800 s

Each queue (CPU one included) carries `jobStateTimeLimitActions`:

```
RUNNABLE + CAPACITY:INSUFFICIENT_INSTANCE_CAPACITY            -> CANCEL after 1800s
RUNNABLE + MISCONFIGURATION:COMPUTE_ENVIRONMENT_MAX_RESOURCE  -> CANCEL after 1800s
RUNNABLE + MISCONFIGURATION:JOB_RESOURCE_REQUIREMENT          -> CANCEL after 1800s
```

Operationally important and not in either claim: a job that cannot get capacity within
**30 minutes is cancelled, not queued indefinitely**. For a long tokenization fan-out this
means capacity shortfall surfaces as cancelled jobs rather than a slow queue.

### Undocumented finding: the CPU CE's job-execution timeout is 30 minutes

`sbsandbox-intern-edullm-cpu.updatePolicy.jobExecutionTimeoutMinutes = 30` — vs **360** on
every GPU CE. This is the CE *update* policy (how long Batch waits for running jobs before
replacing instances on a CE update), not a per-job timeout, but it is the tightest value in
the account and is worth knowing before planning multi-hour CPU work on this CE.

## 4. EC2 On-Demand **P** instance quota — MEASURED — 768, and **already fully committed**

Command:

```
aws service-quotas get-service-quota --service-code ec2 \
  --quota-code L-417A185B --region us-east-1 --output json
```

Returned:

```
"QuotaName": "Running On-Demand P instances",
"QuotaCode": "L-417A185B",
"Value": 768.0,
"Adjustable": true,
"QuotaAppliedAtLevel": "ACCOUNT"
```

**The P quota is 768 vCPU — NOT zero.** So the "H100 is not provisioned" doc claim is refuted
at the quota level: P-family capacity is permitted.

**But** — and this is the decisive detail — from the same
`describe-compute-environments` output, the A100 environment is holding the entire quota:

| CE | instanceTypes | maxvCpus | **desiredvCpus** |
|---|---|---|---|
| `sbsandbox-intern-edullm-gpu-8xa100` | `p4d.24xlarge`, `p4de.24xlarge` | 768 | **768** |
| `sbsandbox-intern-edullm-gpu-1xh100` | `p5.4xlarge` | 384 | **0** |
| `sbsandbox-intern-edullm-gpu-8xh100` | `p5.48xlarge`, `p5en.48xlarge` | 768 | **0** |

DERIVED (inputs: P quota 768 vCPU; `gpu-8xa100.desiredvCpus` = 768; p4d/p5 are both P-family
and share `L-417A185B`): if the A100 CE is actually holding 768 P vCPUs, **the remaining P
quota available to either H100 CE is 0**, and an H100 job would fail to place on quota alone —
independent of physical p5 availability. Both H100 CEs are at `desiredvCpus: 0`, i.e. **neither
has ever demonstrated a scale-out in this snapshot.**

DERIVED sum of configured P demand: 768 (a100) + 384 (1xh100) + 768 (8xh100) = **1920 vCPU of
P-family CE ceilings against a 768 vCPU account quota — 2.5x oversubscribed.**

Whether the 768 is *actually consumed* right now is checked in §4b below.

## 4b. Is the P quota actually consumed? — MEASURED — **576 of 768 in use RIGHT NOW**

Command:

```
aws ec2 describe-instances --region us-east-1 \
  --filters Name=instance-state-name,Values=pending,running \
  --query 'Reservations[].Instances[].{ID:InstanceId,Type:InstanceType,AZ:Placement.AvailabilityZone,State:State.Name,Launch:LaunchTime}'
```

13 instances running. **Six are `p4d.24xlarge`:**

| instance | AZ | launched (UTC) |
|---|---|---|
| `i-024dde6d15033aa0e` | us-east-1d | 2026-08-07T23:16:16 |
| `i-00b093b091577c1f2` | us-east-1d | 2026-08-07T23:29:02 |
| `i-09e2f08c3f4f51ceb` | us-east-1d | 2026-08-07T23:45:58 |
| `i-056cf5febc119557f` | us-east-1b | 2026-08-08T02:45:40 |
| `i-05ded67cda8cebcb3` | us-east-1d | 2026-08-08T04:55:08 |
| `i-02c98c31871ec27ad` | us-east-1b | 2026-08-08T05:19:02 |

vCPU per type — MEASURED via
`aws ec2 describe-instance-types --instance-types c7i.8xlarge p5.4xlarge p5.48xlarge p5en.48xlarge p4d.24xlarge`:

| type | vCPU | GPUs | GPU model |
|---|---|---|---|
| `c7i.8xlarge` | **32** | — | — |
| `p4d.24xlarge` | **96** | 8 | **A100** |
| `p5.4xlarge` | **16** | 1 | **H100** |
| `p5.48xlarge` | **192** | 8 | **H100** |
| `p5en.48xlarge` | **192** | 8 | **H200** ← not H100 |

DERIVED (inputs: 6 running `p4d.24xlarge`, 96 vCPU each): **6 x 96 = 576 P vCPU consumed.**
DERIVED (inputs: P quota 768, consumed 576): **192 P vCPU of headroom remains.**

DERIVED, and this is the deciding H100 number:
- `p5.4xlarge` = 16 vCPU → 192/16 = **up to 12 single-H100 instances could fit the quota** today.
- `p5.48xlarge` = 192 vCPU → 192/192 = **exactly ONE 8xH100 node fits, and only if not one
  more p4d launches.** A second 8xH100 node is quota-impossible without either the A100 fleet
  scaling down or a quota increase.

So the H100 CEs are not blocked by a zero quota — but the 8xH100 CE's `maxvCpus: 768` (= 4
nodes) is **4x what the remaining quota can currently deliver**, and its own `desiredvCpus: 0`
means no p5 has ever been placed in this snapshot. Physical p5 obtainability is separately
**UNVERIFIED** — see §5.

Note the A100 fleet is actively churning (launches at 23:16, 23:29, 23:45, 02:45, 04:55, 05:19
across two nights), i.e. someone is running on it now. This is a moving number, not a floor.

## 5. Standard-instance quota consumption, and the "lane" instances — MEASURED

From the same `describe-instances` output, the non-P, non-G instances (all counting against
`L-1216C47A`, the 1152 standard quota):

| instance | type | vCPU | AZ | launched |
|---|---|---|---|---|
| `i-0844a4533a5657bea` | c6i.8xlarge | 32 | 1d | 2026-07-24 |
| `i-04a17dddb3827bc2c` | t3.micro | 2 | 1d | 2026-08-07 |
| `i-03edf0fbf2e4d601a` | t4g.small | 2 | 1a | 2026-08-01 |
| `i-00f19fc54cadd214a` | c6i.4xlarge | 16 | 1a | 2026-08-03 |
| `i-05041b9905ba361ad` | m7i-flex.2xlarge | 8 | 1a | 2026-08-07 |
| `i-040e0415c2d79e869` | **c7i.8xlarge** | 32 | 1b | 2026-08-08T03:54:47 |

DERIVED: 32+2+2+16+8+32 = **92 standard vCPU consumed of 1152 → 1060 vCPU headroom.**
The 384 CE ceiling is comfortably inside that even with everything else running.

### Undocumented finding, and it matters: the running `c7i.8xlarge` is NOT a Batch instance

```
aws ec2 describe-instances --instance-ids i-040e0415c2d79e869 --query '...Tags'
```

returned:

```
Name        = lane-grant.matherne-nemotron-cc-math-v1
Project     = nemotron-cc-math-v1
edullm:lane = grant.matherne
ExpiresAt   = 2026-08-09T03:54:39Z
```

So there is a **parallel, non-Batch "lane" provisioning path** in this account (per-engineer
long-lived EC2 with a 24h `ExpiresAt` tag), on the *same instance type* the CPU CE uses, and it
draws on the same 1152 standard quota. It is not visible to `batch list-jobs` at all. Any
capacity model built only from Batch APIs will miss it. At today's scale (32 vCPU) it is
immaterial; if several lanes ran at once it would not be.

## 6. AZ coverage — MEASURED — no gap for the CPU env

Subnet → AZ, command:

```
aws ec2 describe-subnets --region us-east-1 --subnet-ids <the 6 ids> \
  --query 'Subnets[].{Subnet:SubnetId,AZ:AvailabilityZone,AZID:AvailabilityZoneId,VPC:VpcId,IPs:AvailableIpAddressCount}'
```

| subnet | AZ | AZ ID | free IPs | in CPU CE? | in H100 CEs? |
|---|---|---|---|---|---|
| `subnet-0bbe2b7870da13713` | us-east-1a | use1-az1 | 4087 | yes | yes |
| `subnet-0a4235fb98b63930f` | us-east-1b | use1-az2 | 4088 | yes | yes |
| `subnet-0fd5ed8accae254dc` | us-east-1c | use1-az4 | 4088 | yes | yes |
| `subnet-08792525c62ba31c0` | us-east-1d | use1-az6 | 4087 | yes | yes |
| `subnet-01f4bf9a051404a37` | us-east-1f | use1-az5 | 4091 | yes | yes |
| `subnet-08858943a74e2befe` | us-east-1e | use1-az3 | 4091 | **no** | yes (both) |

All in one VPC `vpc-0622b8d314ff5f800`. The user's resolution of the 5 CPU subnets to
**1a/1b/1c/1d/1f is CONFIRMED**. IP exhaustion is not a constraint anywhere (~4090 free per
subnet vs 12 instances needed).

Offerings, command:

```
aws ec2 describe-instance-type-offerings --region us-east-1 \
  --location-type availability-zone \
  --filters Name=instance-type,Values=c7i.8xlarge,p5.4xlarge,p5.48xlarge,p5en.48xlarge,p4d.24xlarge \
  --query 'InstanceTypeOfferings[].{Type:InstanceType,AZ:Location}'
```

| instance type | offered in AZs | CE subnets | usable AZs / gap |
|---|---|---|---|
| `c7i.8xlarge` | 1a, 1b, 1c, 1d, 1f | CPU CE: 1a,1b,1c,1d,1f | **5/5 — NO GAP** |
| `p5.4xlarge` | 1a, 1b, 1c, 1d, 1e, 1f | 1xh100 CE: all 6 | **6/6 — no gap** |
| `p5.48xlarge` | 1a, 1b, 1c, 1d, 1e, 1f | 8xh100 CE: all 6 | **6/6 — no gap** |
| `p5en.48xlarge` | **1b, 1d only** | 8xh100 CE: all 6 | **2/6 — 4-AZ GAP** |
| `p4d.24xlarge` | 1a, 1b, 1c, 1d | 8xa100 CE: 1a,1b,1c,1d | 4/4 — no gap |

**CPU CE: perfect AZ coverage. `c7i.8xlarge` is offered in every one of its 5 subnets' AZs.**
(It is not offered in us-east-1e, but the CPU CE has no 1e subnet, so this costs nothing.)

**Bonus answer for the 8xH100 CE:** `p5.48xlarge` is offered in all 6 of its AZs.
`p5en.48xlarge` is offered in only **2 of 6** (us-east-1b, us-east-1d) — so the H200 fallback
type has one third the placement surface of the primary. With `BEST_FIT_PROGRESSIVE` this
narrows the fallback path rather than blocking it.

Note "offered" means the type exists in that AZ's catalogue. It says **nothing** about
whether capacity is available to launch. See §7 — the distinction turns out to be everything.

## 7. H100 obtainability — MEASURED — **the job history settles it: H100 is NOT usable**

I did not have to infer this. Someone already ran the experiment and the failed-job records
carry the verdict in `statusReason`.

Command:

```
aws batch list-jobs --region us-east-1 --job-queue sbsandbox-intern-edullm-gpu-8xh100 \
  --job-status FAILED --query 'jobSummaryList[].{n:jobName,s:status,r:statusReason,created:createdAt}'
```

Two records, quoted verbatim:

> `h100-capacity-probe-1785731833` →
> **"capacity probe complete: p5.48xlarge InsufficientInstanceCapacity in all five reachable
> AZs over 9h"**

> `diag-p5-statusreason-1785871745` →
> **"diagnostic complete: p5.48xlarge InsufficientInstanceCapacity in all reachable AZs;
> confirmed statusReason stays null so the 1800s CAPACITY cancel cannot match"**

And `--job-status SUCCEEDED` on that queue returns **`[]` — zero jobs have ever succeeded on
the 8xH100 queue.**

Same for the 1xH100 queue:

```
aws batch list-jobs --region us-east-1 --job-queue sbsandbox-intern-edullm-gpu-1xh100 \
  --job-status FAILED --query 'jobSummaryList[].{n:jobName,r:statusReason,created:createdAt}'
```

Two platform cancellations, quoted verbatim (identical text on both
`run_019fcec7-cff9-7024-9fea-d3df5a67775f` and `run_019fced9-479d-702f-ae7a-44ae16e9cc4c`):

> **"cancelled by platform: gpu-1xh100 cannot place a job. EC2 has returned
> InsufficientInstanceCapacity for every p5.4xlarge launch in every availability zone and this
> account has never held one. The queue's 1800s RUNNABLE cancel cannot fire because Batch
> leaves statusReason null for capacity failures, so this job would have waited indefinitely
> with no error. gpu-1xh100 is now off the submission form. Resubmit on gpu-8xa100, which has
> capacity."**

`--job-status SUCCEEDED` on `gpu-1xh100` also returns **`[]`**.

Three findings from this, all MEASURED:

1. **`gpu-1xh100` has been REMOVED FROM THE SUBMISSION FORM** by the platform operators
   ("is now off the submission form"). The CE and queue remain `ENABLED`/`VALID` in the Batch
   API — so `describe-compute-environments` showing ENABLED is *not* evidence the shape is
   offered to submitters. This is exactly the trap the claim-B verification was aimed at.
2. **The account has never held a p5 instance** ("this account has never held one"), and a
   9-hour probe got `InsufficientInstanceCapacity` in every AZ. So the AZ-offering table in §6
   (p5.48xlarge offered in 6/6 AZs) is a catalogue fact with **zero** operational meaning here.
3. **The 1800s auto-cancel does NOT protect against capacity failures.** Batch leaves
   `statusReason` null for `InsufficientInstanceCapacity`, so the
   `CAPACITY:INSUFFICIENT_INSTANCE_CAPACITY` time-limit action never matches, and a job on an
   H100 queue **waits indefinitely with no error** rather than being cancelled at 30 min. This
   directly contradicts the reassurance I drew from §3 and is the most operationally dangerous
   thing in this report.

Also worth noting: three earlier `gpu-1xh100` jobs
(`memorysplit-adarsh-trace-a1-h100-v2-20260803`, `...-h100-exact-v6-20260803`, and
`memorysplit-adarsh-trace-a1-20260803`) failed for unrelated reasons ("A10G trace job started
successfully", "Instrumentation patch target corrected in v4", "Redundant after completed A10G
and A100 trace reruns") — i.e. even the H100-*named* work that people did ended up executing on
A10G/A100, never on H100.

## 8. CPU queue occupancy right now — MEASURED — completely idle

Commands (one per state):

```
aws batch list-jobs --region us-east-1 --job-queue sbsandbox-intern-edullm-cpu \
  --job-status <STATE> --query 'length(jobSummaryList)'
```

| state | count |
|---|---|
| SUBMITTED | **0** |
| PENDING | **0** |
| RUNNABLE | **0** |
| STARTING | **0** |
| RUNNING | **0** |

**Zero jobs in every pre-terminal state.** The CPU queue is entirely idle as of this
verification, so the full 384 vCPU is available to the next submitter with no Batch-side
contention.

(Caveat from §5: the idle Batch queue does *not* mean the account is idle — a 32-vCPU
`c7i.8xlarge` "lane" instance belonging to another engineer is running outside Batch. It draws
on the 1152 standard quota, of which 1060 remains, so it does not reduce the 384.)

---

## Verdicts

### CLAIM A — **CONFIRMED**

`sbsandbox-intern-edullm-cpu`: `maxvCpus: 384`, `instanceTypes: ["c7i.8xlarge"]`,
`state: ENABLED`, `status: VALID` / "ComputeEnvironment Healthy". All four elements returned
directly by `batch describe-compute-environments`. Additionally `type: EC2` (On-Demand, not
Spot), `allocationStrategy: BEST_FIT_PROGRESSIVE`, `minvCpus: 0`, `desiredvCpus: 0`.

The project doc's "**caps at 128 vCPU**" is **REFUTED** — the real figure is 384, a 3x
understatement. The doc's "one `c7i.8xlarge` type" half is correct.

### CLAIM B — **PARTIALLY CONFIRMED — configuration yes, usable capacity NO**

Both H100 CEs exist exactly as described: `gpu-1xh100` (`p5.4xlarge`, maxvCpus 384, ENABLED,
VALID) and `gpu-8xh100` (`p5.48xlarge` + `p5en.48xlarge`, maxvCpus 768, ENABLED, VALID). As a
statement about Batch configuration, CLAIM B is confirmed.

As a statement about *available H100 compute*, it is **REFUTED by the job history**: zero
SUCCEEDED jobs on either queue ever; a 9-hour probe returning `InsufficientInstanceCapacity`
in every AZ; a platform cancellation stating "this account has never held one"; and
`gpu-1xh100` explicitly **removed from the submission form**. Both CEs sit at
`desiredvCpus: 0`, never scaled.

The stale doc's conclusion ("**H100 IS NOT PROVISIONED**") is therefore **right in substance
and wrong in mechanism** — H100 CEs *are* provisioned and the P quota is *not* zero (768); the
capacity simply cannot be obtained from EC2. The doc's list of live compute_profiles should add
`gpu-8xa100` as the top usable shape (that CE is at `desiredvCpus: 768` with 6 `p4d.24xlarge`
actually running, and the platform's own cancellation message says "Resubmit on gpu-8xa100,
which has capacity").

### THE NUMBER: effective usable vCPU ceiling for a CPU batch job today = **384**

Set by **CE configuration** (`maxvCpus: 384`), **not** by the EC2 quota and **not** by queue
contention:

- EC2 standard On-Demand quota `L-1216C47A` = **1152 vCPU**, with only 92 in use → 1060 free.
  **The quota does NOT bind. It has 3x headroom over the CE ceiling.**
- Exactly **one** job queue targets the CE (1:1), and it holds **0 jobs in all five
  pre-terminal states**. **Contention does NOT bind today.**
- `c7i.8xlarge` is offered in **all 5** of the CE's AZs. **AZ gaps do NOT bind.**

**So the wall-clock floor stands at the 384-vCPU figure — 2.2 h, not 6.6 h.** The binding
constraint is a CloudFormation-managed `maxvCpus` value, which is the cheap kind of constraint:
raising it to 1152 needs a config change and no AWS quota ticket. DERIVED: 384/32 = 12
concurrent `c7i.8xlarge`; the quota would permit up to 33 (1060/32) if the CE were raised.

One caveat that is *not* about the ceiling: `c7i.8xlarge` capacity obtainability at 12
instances is **UNVERIFIED**. Given §7 — where a configured, ENABLED, quota-permitted CE turned
out to be entirely unable to place instances — I will not assert that 384 is *placeable*
without a live scale-out. `c7i` is a mainstream Intel type with none of p5's scarcity and one
is running in the account right now, so the risk is low, but it is unmeasured. The honest
statement: 384 is the ceiling, nothing in configuration or quota stands below it, and no probe
has demonstrated a 12-instance scale-out.

### H100: **not usable.** Deciding evidence

The P quota is **768 vCPU, not zero** — so the quota is not the blocker. The blockers are:

1. **EC2 physical capacity.** Verbatim from `h100-capacity-probe-1785731833`: "p5.48xlarge
   InsufficientInstanceCapacity in all five reachable AZs over 9h". Verbatim from the platform
   cancellation: "this account has never held one".
2. **Quota already consumed by A100s.** 6 running `p4d.24xlarge` x 96 vCPU = **576 of 768 P
   vCPU in use**, leaving 192 — DERIVED. That is exactly one `p5.48xlarge` (192 vCPU), so even
   if capacity appeared, the 8xH100 CE's 768 ceiling could deliver **1 node, not 4**.
3. **`gpu-1xh100` is off the submission form** — an operator decision invisible to the Batch
   API, which still reports the queue ENABLED/VALID.

Usable large-GPU shape today is **`gpu-8xa100`** (p4d.24xlarge, A100, 768 maxvCpus,
desiredvCpus 768, actively running).

---

## Things I found that were not asked for, and that contradict the claims

1. **The 1800 s auto-cancel is fail-open for capacity failures.** Every queue declares
   `CAPACITY:INSUFFICIENT_INSTANCE_CAPACITY → CANCEL after 1800s`, but Batch leaves
   `statusReason` null for capacity failures so the rule **never matches** — measured and
   documented in the `diag-p5-statusreason-1785871745` statusReason. A job on a capacity-starved
   queue **hangs forever with no error**. Anyone reading the queue config would conclude the
   opposite.
2. **`state: ENABLED` is not evidence a shape is submittable.** `gpu-1xh100` is ENABLED and
   VALID in the API while being explicitly removed from the platform's submission form. Any
   capacity inventory built from `describe-compute-environments` alone will overstate what can
   actually be run.
3. **A non-Batch "lane" provisioning path exists** (`edullm:lane` / `ExpiresAt` tags,
   per-engineer EC2, currently `lane-grant.matherne-nemotron-cc-math-v1` on a `c7i.8xlarge`).
   It consumes the same standard vCPU quota as the CPU CE and is invisible to `batch list-jobs`.
4. **P-family CEs are 2.5x oversubscribed against their own quota**: 768 (8xa100) + 384
   (1xh100) + 768 (8xh100) = 1920 configured vs a 768 quota — DERIVED. The three CEs cannot be
   busy simultaneously regardless of physical capacity.
5. **`p5en.48xlarge` is an H200, not an H100** (`GpuInfo.Gpus[0].Name = "H200"`, measured). The
   "8xh100" CE would deliver H200s if it ever placed that type. Also it is offered in only 2 of
   the CE's 6 AZs.
6. **The CPU CE's `jobExecutionTimeoutMinutes` is 30** vs 360 on every GPU CE — the tightest
   CE update-policy value in the account.
7. **`p4d.24xlarge` is not offered in us-east-1f**, and the 8xa100 CE has no 1f subnet — so it
   is consistent, but the A100 fleet has 4 AZs of placement surface, not 6.


