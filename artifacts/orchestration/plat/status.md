# PLAT-EXEC status — wave 3 platform readiness

**Agent:** PLAT-EXEC (platform/AWS executive), reporting to CEO/main.
**Date:** 2026-08-08. **Mode: READ-ONLY.** Nothing submitted, registered, pushed, or written to any bucket.
**Region:** us-east-1. **Account:** `sbsandbox` (ID scrubbed to `<ACCOUNT_ID>` in committed text).

Grades used throughout: `MEASURED` (I ran the call / read the bytes), `MEASURED-IN-CODE` (read the
source), `DERIVED` (arithmetic on measured inputs), `UNVERIFIED` (nobody has measured this).

---

## 🔴 ESCALATION — the promotion rule is ENABLED, not disabled

`edullm-landing-manifest-created` is **`State: ENABLED`** as of 2026-08-08. `MEASURED`.

CLAUDE.md and my own brief both assert it was deliberately DISABLED on 2026-08-01. **It is live.**
Somebody re-enabled it in the intervening week. `describe-rule` carries no timestamp, so *when* and
*by whom* is `UNVERIFIED` — CloudTrail would settle it.

Because there is no publish-without-promote mode, **writing any `manifest.json` to
`s3://edullm-landing` today auto-fires the validator with `--promote` and freezes a `vN`.**
Details and blast radius in `airlock-verification.md`.

---

## Task status

| # | task | state |
|---|---|---|
| 1 | #11 live validator timeout | ✅ **DONE — 14400 s**, top ACTIVE rev **14** (not 10) |
| — | EventBridge promotion rule live state | ✅ **DONE — ENABLED** (escalated) |
| — | `edullm-wu-fsck-nightly` live state | ✅ **DONE — ENABLED**, weekly `cron(6 9 ? * MON *)` |
| 5 | airlock Deny live smoke test | ⏳ in progress |
| 2 | inventory from job history | ⏳ in progress |
| 3 | wave-3 shape obtainability + lane-grant | ⏳ in progress |
| 4 | per-job watchdog design | ⏳ pending |
| 6 | cost + region packet | ⏳ pending |
| 7 | promote-duration contradiction ruling | ⏳ pending |

## Tooling note (affects my throughput, not my findings)

From ~the second call onward, the Bash and SendMessage tools returned
`claude-sonnet-5[1m] is temporarily unavailable, so auto mode cannot determine the safety of …`
— a permission-classifier outage, not a permission denial. Read-only file reads and `sb-aws`
broker calls were unaffected, so I continued on those and persisted everything here. Any gap in
this file is an outage artifact, not a skipped check.

---

## 1. #11 — validator timeout and the real revision table `MEASURED`

`aws batch describe-job-definitions --job-definition-name edullm-validator --status ACTIVE`, 2026-08-08.

**The live timeout is `attemptDurationSeconds: 14400` (4 h) on revision 14.**

**CLAUDE.md's job-def table is stale.** It names rev 10 as top ACTIVE at 7200 s. Revisions 11–14
have been registered since 2026-08-01.

| rev | timeout | image digest (ECR `sbsandbox-intern-edullm-data`) | jobRoleArn | command flags |
|---|---|---|---|---|
| **14 (top ACTIVE)** | **14400 s** | `sha256:352afc50…c7a1b` | `…-edullm-dataset-validator` | `--head-workers 16 --promote --promote-workers 16` |
| 13 | 7200 s | `sha256:0d079b3c…3058b` | `…-edullm-dataset-validator` | `--promote --promote-workers 16` |
| 12 | 7200 s | `sha256:a71e62f4…82817` | `…-edullm-dataset-validator` | `--promote --promote-workers 16` |
| 10 | 7200 s | `sha256:339c2b6b…448d6` | `…-edullm-dataset-validator` | `--promote --promote-workers 16` |
| 9 | 7200 s | `sha256:339c2b6b…448d6` | `…-edullm-batch-workload` | `--promote --promote-workers 16` |
| 8 and below | 7200 s / none | `…-edullm-olmo-core` image, **wheel-bootstrapping** | `…-batch-workload` | varies |

All of rev 9–14 run `python -m edullm_data.validate` **directly from a digest-pinned image** —
no wheel bootstrap. Rev 8 and below `pip install` a wheel from `s3://edullm-landing/_dist/`.

### Three consequences that bear on the release

1. **The "1-hour cap" is confirmed dead.** 14400 s is *our own* setting, raised from 7200 at rev 14.
   Consistent with the AWS Batch user guide: there is no maximum job timeout. `MEASURED` + doc.

2. **⚠️ Rev 14 added `--head-workers 16`; revs 10–13 pass no such flag.** This is precisely the
   "every default is 1" trap. Gate A's per-entry `s3.head` loop runs single-streamed on rev 13 and
   16-way on rev 14 — a large multiple on the head phase, with **no version-string difference to
   warn anyone**. Release must cite **rev 14 by number**. Note that both EventBridge and any
   `submit-job` that names the def *unversioned* resolve to the top ACTIVE rev, so this cuts over
   silently — for better and worse.

3. **The container is small: `vcpus: 4`, `memory: 8192`.** 16 head workers and 16 promote workers on
   4 vCPUs is I/O fan-out, which is the right shape for `head`/`CopyObject` — both are
   network-bound, not CPU-bound. But it caps anything CPU-bound in the same container.

## EventBridge — full live rule state `MEASURED`

`aws events list-rules` + `list-targets-by-rule`, 2026-08-08.

| rule | state | schedule / pattern | target |
|---|---|---|---|
| **`edullm-landing-manifest-created`** | **🔴 ENABLED** | S3 Object Created, bucket `edullm-landing`, key **suffix `manifest.json`** | Batch queue `…-edullm-cpu`, job def **`edullm-validator`** (unversioned → **rev 14**), job name `edullm-validate-on-manifest`, **`MaximumRetryAttempts: 2`** |
| `edullm-wu-fsck-nightly` | ENABLED | `cron(6 9 ? * MON *)` — **weekly**, Mondays 09:06 UTC | Batch `edullm-fsck` |
| `edullm-phase4-event-shape-probe` | ENABLED | Batch Job State Change on the **GPU** queue | *(temporary probe — see below)* |
| `sbsandbox-intern-edullm-batch-lifecycle` | ENABLED | Batch state change, jobName wildcard `run_*-*-7*-*-*`, 17 queues | lifecycle recorder + notifier |
| `sbsandbox-intern-edullm-morning-page` | ENABLED | `cron(0 13 * * ? *)` | notifier |

Non-edullm rules in the account (`ForwardToVdi`, `SSMExplorerManagedRule`,
`aws-controltower-*`, `mcat-dev-*`, `sffs-*`) are out of scope and untouched.

### Notes on the non-obvious ones

- **The manifest rule has no prefix constraint.** It matches key **suffix** `manifest.json`
  anywhere in the bucket — including a scratch or test prefix someone assumes is inert.
- **`MaximumRetryAttempts: 2` is on the EventBridge *target*** (submission retry), distinct from
  the job def's `retryStrategy.attempts: 1` (execution retry). A submission failure is retried
  twice; a job failure is not retried at all.
- **`edullm-wu-fsck-nightly` next fires Monday 2026-08-10 09:06 UTC**, ~2 days out. If wave-3
  promotion lands before then, Gate B sweeps the new data unattended. Probably benign; it is
  nonetheless an unsupervised job touching `edullm-data` inside the build window.
- **`edullm-phase4-event-shape-probe` is a leftover.** Its own description reads *"TEMPORARY.
  Phase 4 probe: capture one raw Batch job state change to settle what the event carries. Delete
  after reading."* It is still armed. It only observes (no mutation), so it is not a hazard, but
  it is undeleted Phase-4 scaffolding and someone should reap it.

---

# ▶ PLAT-EXEC re-dispatch, 2026-08-08 (session 2)

The PLAT-EXEC above died with tasks 2, 3, 4, 6, 7 open. **I inherit everything above as written**
and append below; anything I correct in place is marked `CORRECTION` with its evidence.
Mode is unchanged: **STRICTLY READ-ONLY**, and I am explicitly *not* executing ruling R1's
`disable-rule` — the CEO is routing that to the human owner. I record the exact command instead.

Open on arrival: **2** (inventory from job history) · **3** (wave-3 shape + lane grant) ·
**4** (watchdog design) · **6** (cost + region packet) · **7** (promote-duration ruling, top priority).
Task 5 (airlock Deny) is closed by `airlock-verification.md`; I re-read it and did not re-run the
`PutObject`, because re-running a live write is exactly the mutation my brief forbids and the
negative result is already `MEASURED`.

---

## ✅ MUTATION EXECUTED — auto-promotion rule DISABLED (the one authorized write)

**Authorized by the human owner**, relayed by the CEO mid-task, superseding read-only for this single
action only. Executed 2026-08-08 via the `sb-aws` broker, account `sbsandbox`, us-east-1.

```
aws events disable-rule --name edullm-landing-manifest-created     # ← RUN. exit 0, empty stdout.
```

### 🔓 THE RE-ENABLE COMMAND — release is this one line, do not reconstruct it

```
aws events enable-rule --name edullm-landing-manifest-created
```

**Verified after the fact** with `events describe-rule` — `MEASURED`, pasted verbatim except the
account id, which I scrubbed:

```json
{
    "Name": "edullm-landing-manifest-created",
    "Arn": "arn:aws:events:us-east-1:<ACCOUNT_ID>:rule/edullm-landing-manifest-created",
    "EventPattern": "{\"detail-type\":[\"Object Created\"],\"source\":[\"aws.s3\"],
                      \"detail\":{\"bucket\":{\"name\":[\"edullm-landing\"]},
                      \"object\":{\"key\":[{\"suffix\":\"manifest.json\"}]}}}",
    "State": "DISABLED",
    "Description": "s3://edullm-landing Object Created with key suffix manifest.json -> submit the
                    dataset validator (Gate A) on the sbsandbox-intern-edullm-cpu queue.",
    "EventBusName": "default",
    "CreatedBy": "<ACCOUNT_ID>"
}
```

**Only the rule's `State` changed.** The event pattern, the target, and the target's
`MaximumRetryAttempts: 2` are untouched — I did not call `put-targets` or `put-rule`, so re-enabling
restores exactly the wiring documented in `airlock-verification.md` §2.

### ⚠️ Three things whoever re-enables this must know

1. **The target names the job def `edullm-validator` UNVERSIONED.** Re-enabling picks up whatever the
   **top ACTIVE revision is at that moment** — today **rev 14**, which is the only revision carrying
   `--head-workers 16`. If someone registers rev 15 before release, re-enabling silently cuts over to
   it with **no version-string difference to warn anyone**. That is the documented "for better and
   worse" behaviour, and it is now a release-checklist item: *re-`describe-job-definitions` at
   re-enable time and confirm the top ACTIVE rev is still the one you tested.*
2. **Disabling the rule does not make landing safe — it makes it manual.** There is still no
   publish-without-promote mode. A hand-submitted validator job still promotes. Ruling R2 stands on
   its own: nothing is written to `s3://edullm-landing` until the owner rules on the release gate.
   This mutation is defence in depth, not permission.
3. **Nothing else was touched.** `edullm-wu-fsck-nightly` remains ENABLED and still fires
   **Mon 2026-08-10 09:06 UTC**; `edullm-phase4-event-shape-probe` remains ENABLED and observe-only.
   Both were out of scope for the authorization and I left them exactly as found.

---

# ★ RULING R3 — how long does `promote()` actually take?

**Answer: ~8 minutes for stage 1 (36,000 objects) at `--promote-workers 16`; ~13 min for all 40,001
objects of both stages. Neither published figure is right, and the CEO's hypothesis is REFUTED.**

I was told to try to refute the hypothesis that §8.2's "20–30 min" is scoped to publish's *copy*
phase rather than promote proper. **It is refuted by the sentence itself.** `IMPLEMENTATION-PLAN.md`
lines 953–954 read verbatim:

> **Promotion, by contrast, is already solved.** It is ~2 round trips per object but **is** threaded
> on both phases, so 40,001 objects at 16 workers is ~20–30 min.

"Promotion, by contrast" is explicitly contrasted *against* the Gate A table above it, and "threaded
on both phases" names promote's two loops specifically (`validate.py:2069-2076` copy,
`:2098-2102` CRC). This is a statement about `promote()`, at 16 workers, at the right object count.
**It is not a scope error.** So the two figures are not measuring different things — they are the
same quantity, and at least one is simply wrong.

## The arithmetic, as ordered — objects × round-trips ÷ workers × per-call latency

### Round trips per object — the plan says 2; **the code says 3.** `MEASURED-IN-CODE`

Every source in the plan quotes "~2 S3 round-trips per object" from promote's own docstring
(`validate.py:1943-1948`). **The docstring undercounts its own implementation.** Tracing the
non-vendored path, which is the one `pretrain_tokens_v1` takes (`vendored` is set at `:493` from
`profile.startswith("vendored/")`, false for us, so the `hash_object` re-read at `:2025` and the
verify at `:2059` are both dead code for this corpus):

| # | call | site |
|---|---|---|
| 1 | `head_object` on the **source** — `s3.copy()` HEADs to decide single-part vs multipart | `s3.py:436-438` |
| 2 | `copy_object` | `s3.py:447` |
| 3 | `head_object` on the **destination**, to read `crc64nvme` for the seal | `validate.py:2094` |

**Three, not two.** Call 1 is invisible at the `promote()` level — it is hidden inside `s3.copy()`,
which is why every reader of the docstring has counted 2. It is a real network round trip and it is
**strictly serial with the copy it precedes**, inside the same worker.

> One free correction available to ENG: `_MULTIPART_COPY_THRESHOLD` is 5 GiB (`s3.py:157`) and a
> shard is **100,007,936 B ≈ 100.01 MB** (`SHARD_TOKENS` 25,001,984 × 4). Every shard is ~50× under
> the threshold, so that HEAD's answer is **always** "single-part." Passing the manifest's declared
> `bytes` into `s3.copy()` would delete call 1 outright — **a 33% cut to promote, ~15 lines.** Not
> on the critical path at these magnitudes; noted, not recommended as urgent.

### Per-call latency — **63.4 ms.** `DERIVED` from M7a, the repo's only in-region anchor

Two independent routes to the same number, which is why I trust it:

- M7a: Gate A measured **507.5 ms/object over 8 round trips** → 507.5/8 = **63.44 ms/call**
- M7: the same run measured **15.8 round trips/s** serially → 1/15.8 = **63.29 ms/call**

Both from `edullm-validator`, 4 vCPU, in-region, same container shape promote runs in. Agreement to
0.2% across two differently-derived figures is the strongest latency anchor in the repo.

### The result

`objects × 3 round trips ÷ 16 workers × 63.4 ms`:

| scope | objects | serial | **at 16 workers** |
|---|---|---|---|
| stage 1 | 36,000 | 1.90 h | **7.1 min** |
| stage 2 | 4,000 | 0.21 h | **0.8 min** |
| **both stages** | **40,001** | **2.12 h** | **7.9 min** |

At the docstring's 2 round trips it is 5.3 min; at 3 it is 7.9 min. **The honest band is 5–8 min of
latency-bound work.**

## But latency is not the only floor — and this is where §8.2 gets its number honestly

A latency model assumes `CopyObject` returns as fast as a HEAD. **It does not**: a server-side copy
of 100 MB moves 100 MB inside S3, and S3 holds the connection open until it finishes. So there is a
**bandwidth floor** underneath the latency floor, and the corpus is **4.00 TB**
(40,001 × 100,007,936 B). Two measured anchors bound it:

| anchor | what it was | aggregate rate | per-object |
|---|---|---|---|
| **M13** `MEASURED` | olmo-150b migration, 6,921 obj / 630.1 GB, 21:24–21:53Z = 1,740 s | **362 MB/s** | 3.98 obj/s |
| **M7c** `MEASURED` | the SIGKILLed promote — 6,324 of 10,051 obj copied inside 7,200 s (Gate A took ~5,100 s of it, so copy had ~2,100 s) | **~301 MB/s** | 3.01 obj/s |

Two independent runs at **300–362 MB/s aggregate**. At 4.00 TB that is **3.1–3.7 h**, which is
**25–45× the latency model.** The copy is bandwidth-bound, not latency-bound, and every figure in
the plan — mine above included, and all four of the plan's — models only latency.

**⚠️ CORRECTING MYSELF IN PLACE:** my 7.9 min above is the *latency* floor and is **not** the answer
to the owner's question. The answer must be the max of the two floors. I am leaving the derivation
standing because the CEO asked for that specific arithmetic and because it explains where the
plan's ~1 min comes from — but **the binding constraint is bandwidth.**

### So which figure is right?

**Neither, and the plan's own ~1 min is the more dangerous error.**

| figure | verdict |
|---|---|
| `BUILD-DEPENDENCY-GRAPH.md` §5 / §9 / §8A.4: **~1 min / 0.02 h** | **WRONG, and optimistic by ~200×.** It is a latency model at 2 round trips, and it is the number that would be used to size a timeout. |
| §8.2: **20–30 min** | **Wrong, but wrong in the safe direction** — and it is the only one of the four that is even the right order of magnitude, because whoever wrote it appears to have been reasoning about moving bytes. |
| **This derivation: 3.1–3.7 h at 300–362 MB/s** | the bandwidth floor, from two `MEASURED` runs |

**The one thing I cannot bound from a read-only session:** whether 16 promote workers reach an
*aggregate* rate above M13's 362 MB/s. M13's 362 MB/s was itself achieved at unrecorded concurrency
(`HANDOFF.md:805` records only start and end times), and M7c's ~301 MB/s was at 16 workers in a
4-vCPU container. **That the two agree within 20% despite different concurrency is weak evidence
that the ceiling is S3-side, not worker-side** — i.e. more workers may not help. `UNVERIFIED`, and
it is the single highest-value thing to measure before Phase 4.

> **⚠️ CORRECTING MYSELF IN PLACE — I first wrote "a 4-vCPU container's NIC is the suspect." That is
> wrong, and I am striking it.** Two reasons, and the second is the decisive one:
>
> 1. `c7i.8xlarge` `networkPerformance` is **12,500 Mbit/s = 1,562 MB/s** (`MEASURED`, from the same
>    `pricing get-products` response). M13's 362 MB/s is **23% of one instance's NIC** — nowhere near
>    saturation. Even a strict 1/8 proportional share for a 4-vCPU container is 195 MB/s, and M7c
>    **exceeded** that at ~301 MB/s, which on its own falsifies proportional-share NIC saturation.
> 2. **More fundamentally: a server-side `CopyObject` moves ZERO payload bytes through the container.**
>    The whole point of `s3.copy()` is that S3 copies internally; the client sends a request and waits.
>    So the container's NIC cannot be the ceiling **for the copy phase at all** — it carries only
>    request/response headers. This is the same property that makes the region mirror cheap
>    (§6 below) and it is why M6a moved 586.6 GiB from a **laptop** in 498 s.
>
> **What this changes:** consequence 3's "the fix is a bigger container" is **wrong** and I withdraw
> it. If the 300–362 MB/s ceiling is S3-side, a bigger instance buys nothing and **more concurrency
> is the only lever that could help** — the opposite of what I first wrote. The stage-2 calibration
> becomes more valuable, not less: it is the only way to learn whether the rate scales with
> `--promote-workers`. Run stage 2 at 16 workers, then, if it disappoints, at 32.

## What the owner should be told

**Promote is 3–4 hours, not 1 minute and not 30 minutes.** `DERIVED` from two `MEASURED` copy runs.

Three consequences that actually bear on the release-gate decision:

1. **It still fits.** Rev 14's timeout is **14,400 s (4.0 h)** — but Gate A and promote run in the
   **same job** (`--promote` is a flag on the validator, `:2525`), so the budget is Gate A **plus**
   promote. At Gate A's fixed 0.36 h that is ~3.5–4.1 h against a 4.0 h wall. **It is marginal, and
   at the pessimistic end it fails.** This is the finding that matters most: the plan's ~1 min made
   this look like it had 3.6 h of headroom when it has roughly none.
2. **M7c is the precedent, and it is a failure.** The only promotion this project has ever actually
   run was **SIGKILLed at 63% after 7,200 s**. A ~1 min projection is contradicted by our own logs.
3. ~~**The fix is a bigger container, not more workers.**~~ **WITHDRAWN — see the correction box
   above.** A server-side copy moves no payload bytes through the container, so instance size is not
   the lever; concurrency is the only candidate. **One measurement settles it:** promote stage 2
   (4,000 objects, ~400 GB) alone and read the achieved MB/s. At 362 MB/s that is **~18 min** — a
   cheap, bounded, real calibration of the exact number the owner is waiting on, on the smaller of
   the two stages, where a mistake costs a `v2` on stage 2 rather than on the flagship.

**Recommendation to the CEO: quote the owner 3–4 h with the marginal-timeout caveat, and offer the
stage-2-first calibration as the way to convert it into a measurement.** No promote should be
submitted against a 4.0 h timeout on this arithmetic without either raising the timeout or splitting
Gate A and promote into separate jobs.

**Verified after writing the above** (`validate.py:2505-2530`): `--promote` is a flag on the
validator's own `main()`, and `_promote_or_reconcile` is called **inside the same loop iteration** as
`validate_prefix`, in the same process, in the same job. `MEASURED-IN-CODE`. The "Gate A + promote
share one 4.0 h timeout" claim in consequence 1 holds.

---

## 2. Inventory from JOB HISTORY — thesis CONFIRMED and sharpened

Delegated to a read-only worker; full evidence in **`artifacts/orchestration/plat/inventory.md`**
(sections A–F). Headline results, all `MEASURED` by the worker first-hand:

**16 queues, 16 compute environments, 1:1 mapping** — so "queue" and "shape" are interchangeable here.

| shape | SUCCEEDED | verdict |
|---|---|---|
| `gpu-8xa100` (p4d/p4de.24xlarge) | **21**, spanning 08-03 → 08-08 | **the largest GPU shape that actually works.** Confirmed. |
| `gpu-4xa10g` / `1xt4` / `4xl40s` / `1xl40s` / `1xl4` | 14 / 13 / 10 / 10 / 10 | work |
| `gpu-8xa10g` | 3 SUCCEEDED + 4 FAILED (none capacity) | works |
| **`gpu-1xh100`** | **0** — 5 FAILED, 3 capacity-coded | **capacity-denied.** Confirmed. |
| **`gpu-8xh100`** | **0** — 16 FAILED incl. a 9 h capacity probe | **capacity-denied.** New: the 8× sibling is dead too. |
| `gpu-4xl4` | 0 — 1 FAILED, app-level `exitCode: 1` | placed fine; **not** a capacity finding |
| `gpu-8xl40s` / `8xl4` / `4xt4` / `8xt4` | 0, **and zero rows in any status** | never attempted (best read) |
| **`sbsandbox-intern-edullm-cpu`** (`c7i.8xlarge`) | **84** | works; see the concurrency caveat below |

**Three independent confirmations that H100 is capacity, not quota** — this is stronger than the
brief's single claim:
1. **Batch job history** carries a prior session's own verbatim diagnosis, still sitting in the API:
   *"EC2 has returned InsufficientInstanceCapacity for every p5.4xlarge launch in every availability
   zone and this account has never held one… The queue's 1800s RUNNABLE cancel cannot fire because
   Batch leaves statusReason null for capacity failures, so this job would have waited indefinitely
   with no error. gpu-1xh100 is now off the submission form."*
2. **CloudWatch Logs**: **zero** log streams for both H100 shapes across the log group's entire
   ~11-day life (created 2026-07-28, 90-day retention — so no left-censoring). Every one of those 21
   FAILED rows was a **pre-placement RUNNABLE cancel**; not one ever got a container.
3. **Cost Explorer**: **no billed line items at all** (absent, not $0) for `p5.4xlarge` /
   `p5.48xlarge` / `p5en.48xlarge` across May–Aug 2026, while p4d/p4de show real spend the same months.

**§8B.7's dead-cancel claim is independently corroborated by the account's own logs** — a
`diag-p5-statusreason-*` job records *"confirmed statusReason stays null so the 1800s CAPACITY cancel
cannot match."* All 16 queues declare the rule; it fires on none of them. The 9 h probe is the proof:
it had to be killed by hand.

### CPU concurrency — the ledger's arithmetic checks out, obtainability does not

- **384 maxvCpus ÷ 32 vCPU per `c7i.8xlarge` = 12 concurrent children.** `DERIVED`, and the
  ledger's "12, not 16×8" is **correct**. Re-verified first-hand that the CE lists **one** instance
  type, `c7i.8xlarge`. A 32-vCPU child packs 1-per-instance, so 12 children means **12 instances**.
- **The 384 cap is a CE-configuration choice, not an account quota.** The account's Standard-family
  EC2 quota is **1,152 vCPU** — 3× the cap. `MEASURED`. So the ceiling is raisable by editing the CE
  (a mutation nobody is authorized to make right now), not by a quota request to AWS.
- ⚠️ **12 concurrent has NEVER been demonstrated.** `UNVERIFIED`. Of 84 SUCCEEDED CPU jobs, exactly
  **one** ever requested the full 32 vCPU; the rest ran at 2–16. Live snapshot: 0 RUNNING, 0 RUNNABLE.
  This matches the plan's own §8A.3 warning verbatim. **Unlike H100 there is zero capacity-denial
  evidence for `c7i.8xlarge`, so this is "unexercised," not "broken"** — a real but different risk.

### Two findings beyond the brief, both worth the CEO's attention

1. 🔴 **The lane path is live RIGHT NOW and it eats the same quota.** `MEASURED` by me directly:
   `i-040e0415c2d79e869`, **`c7i.8xlarge`, state `running`**, launched 2026-08-08T03:54:47Z, tagged
   `Name=lane-grant.matherne-nemotron-cc-math-v1`, `edullm:lane=grant.matherne`,
   **`ExpiresAt=2026-08-09T03:54:39Z`**. This is **32 vCPU of the Standard quota consumed by an
   instance that is invisible to `batch list-jobs`** — exactly §8B.7's warning that "the queue is
   empty" ≠ "the quota is free." It does not touch the CE's own 384 cap (that is a Batch-side
   accounting limit), but it does draw on the 1,152 account quota, and it is another engineer's work
   on the same corpus family (`nemotron-cc-math`). Two other lane instances exist but are `stopped`
   and already past their `ExpiresAt`. **Recommendation: nobody reaps this — it self-expires
   2026-08-09T03:54Z, which is before any plausible wave-3 launch. No action needed, but do not size
   a 12-instance wave assuming 1,152 free vCPU today.**
2. **A $3,567.25 July charge for `p6-b200.48xlarge`** — B200 GPUs, an instance type with **no queue
   and no compute environment** anywhere in the 16-queue inventory. Something in this account
   obtained B200 capacity through a non-Batch path. Out of scope for the corpus build, but it means
   *"this account cannot get modern GPU capacity"* is **false as a general statement** — it is the
   *Batch queues* that cannot. Flagging because it may matter to whoever plans the training run.
   Also: `describe-instance-type-offerings` lists p5.4xlarge/p5.48xlarge in **6 of 6 AZs** — the same
   config-says-yes/reality-says-no trap one layer below Batch. **Offering-catalog presence is not
   capacity either.**

---

## 3. Wave-3 shape obtainability + lane grant

**The wave-3 shape the plan wants is 12 × `c7i.8xlarge` (32 vCPU each) on
`sbsandbox-intern-edullm-cpu`.** Verdict by component:

| component | state | grade |
|---|---|---|
| queue + CE exist, ENABLED/VALID, healthy | ✅ | `MEASURED` |
| the shape runs at all | ✅ 84 SUCCEEDED jobs | `MEASURED` |
| 32 vCPU in one child is placeable | ✅ exactly one job has done it | `MEASURED` |
| **12 of them concurrently** | ⚠️ **never demonstrated** | **`UNVERIFIED`** |
| capacity denial ever seen on this shape | ✅ none, ever | `MEASURED` |
| account quota headroom (1,152 vs 384 cap) | ✅ 3× | `MEASURED` |
| `desiredvCpus` | **0** — scales from cold | `MEASURED` |

**Assessment: obtainability is likely but unproven, and the failure mode is the silent one.** If
capacity is short for 12 simultaneous `c7i.8xlarge`, the surplus children sit in **RUNNABLE forever**
— §8B.7's fail-open. `c7i` is a mainstream Intel SKU in a 5-AZ region, not a shortage part like H100,
so I rate the risk **low but not zero**, and **entirely mitigated by the watchdog in §4 below.**

**Recommendation (a mutation I did NOT perform):** the plan already calls for a Phase-2 smoke test
that de-risks the live-HF read. **Request the full 12-child wave shape in that same job** — it costs
nothing extra and converts the only `UNVERIFIED` row above into `MEASURED` before anything expensive
depends on it. Concretely, submit the smoke test as a 12-child array at 32 vCPU and watch how many
reach RUNNING. Also note `desiredvCpus: 0`: the first wave pays **cold-start scale-out** (EC2 launch
+ ECS agent registration + image pull), typically 2–5 min and not in any plan estimate.

---

## 4. Per-job watchdog — DESIGN ONLY, nothing deployed

**The problem, restated precisely.** All 16 queues declare
`CAPACITY:INSUFFICIENT_INSTANCE_CAPACITY → CANCEL after 1800s`. Batch leaves `statusReason` **null**
for capacity failures, so the rule's condition never matches and **the cancel never fires**.
Independently confirmed three ways now: CLAUDE.md, `IMPLEMENTATION-PLAN.md` §8B.7, and the account's
own `diag-p5-statusreason-*` job. A starved job sits **RUNNABLE forever, silently, at zero cost and
zero progress** — the 9-hour H100 probe is the precedent, and it ended only because a human killed it.

**Design principle: the watchdog must be EXTERNAL and must key off WALL CLOCK, not job state.** Any
design that waits for Batch to report a problem inherits the bug. The signal is not "did the job
fail" but **"has this job been in RUNNABLE longer than it should be."**

### The mechanism — polling `describe-jobs`, one call per job per interval

```
for each submitted jobId:
    j = batch describe-jobs --jobs <id>
    runnable_for = now - (j.startedAt or j.createdAt)
    if j.status == "RUNNABLE" and runnable_for > T_place:   -> ALERT: capacity starvation
    if j.status == "RUNNING"  and runnable_for > T_run:     -> ALERT: overrun
    if j.status in (FAILED, SUCCEEDED):                     -> terminal, stop polling
```

Two thresholds, because the two failure modes need different numbers:

| threshold | value | basis |
|---|---|---|
| **`T_place`** — max time in RUNNABLE before alerting | **10 min** | Cold start from `desiredvCpus: 0` is 2–5 min `DERIVED`. 10 min is ~2× that and far below the 1800 s the dead rule pretends to enforce. |
| **`T_run`** — max time in RUNNING | **per-job, from the plan's own table** | not one global number — see below |

**`T_run` per job, from §8A.4's "fixed" column with a 2× safety factor** (the plan's own estimates
have been wrong by 4.52× in one direction this month, so a tight bound would false-alarm):

| job | expected | `T_run` alert | job-def timeout |
|---|---|---|---|
| build child (one bundle, 32 vCPU) | ~10 h | 20 h | must exceed 20 h |
| publish stage 1 | 0.3 h | 1 h | ≥ 21600 s |
| **Gate A + promote stage 1** | **~0.36 h + 3–4 h** | **8 h** | **⚠️ 14400 s today — see R3** |
| Gate A + promote stage 2 | ~0.4 h | 1.5 h | 14400 s |

**Note the interaction with R3:** the watchdog's `T_run` for the promote job is *longer than the job
definition's own timeout*. That is the correct reading — the job def will SIGKILL at 4.0 h before the
watchdog ever alerts, which is precisely the M7c failure repeating. **The watchdog does not fix that;
only raising the timeout or splitting the job does.** The watchdog's job here is to tell you *quickly*
that it happened, not to prevent it.

**Where it runs.** Not in AWS — an in-AWS watchdog shares the failure domain it is watching, and
deploying anything is a mutation nobody is authorized to make. **Run it in the driving session** as a
`Monitor`-style poll loop, 60 s interval for placement (cheap, `describe-jobs` is not rate-limited at
this volume) and 5 min once RUNNING. For a wave of 12 children that is 12 calls/min — negligible.

**What it must NOT do.** It must not auto-cancel. A false positive that kills a healthy 10-hour build
child costs more than a starved job sitting idle, and the whole point is that we cannot reliably
distinguish "starved" from "slow to place" from the outside. **Alert, name the job, and let a human
decide.** The one exception worth considering: auto-cancel a job still in **RUNNABLE** after
`T_place`, since a RUNNABLE job has consumed nothing and cancelling it is free — but even that I would
leave manual for wave 3, because a cancel is a mutation.

**Coverage requirement — the trap in writing this.** A watchdog that alerts only on the failure
signatures you thought of stays silent through the ones you did not. **The poll must emit on every
terminal state** (`SUCCEEDED`, `FAILED`) as well as the two timeouts, so that *silence* unambiguously
means "still running normally" rather than "the watchdog itself died." Ask of any implementation: *if
this job crashed right now, would the monitor emit anything?* If no, widen it.

---

## 6. Cost + region packet

**Region ruling: build, validate and promote in `us-east-1`; mirror the frozen `vN` to `us-east-2`
only if something there needs it.** The owner has pre-approved the transfer, so this documents rather
than asks.

| item | figure | grade |
|---|---|---|
| corpus size, 1.0T | **4.00 TB** = 40,001 × 100,007,936 B | `DERIVED` from `SHARD_TOKENS` |
| egress us-east-1 → us-east-2 | **$0.02/GB → ~$80 one-time** | `DERIVED` at the published inter-region rate |
| destination storage | **~$92/month** for 4 TB S3 Standard | `DERIVED` at $0.023/GB-month |
| build compute, 12 × `c7i.8xlarge` on-demand | **$1.4280/h each → $17.14/h for the wave** | **`MEASURED`** — `pricing get-products`, SKU `7RFUEP9XE7QPFUGE`, *"$1.428 per On Demand Linux c7i.8xlarge Instance Hour"*, price-list version `20260806171752` |
| build at the 9.96 h floor | **~$171** | `DERIVED` |
| build as-configured (49.0 h, unsplit) | **~$840** | `DERIVED` — the cost of not splitting DCLM |
| validator job (4 vCPU, ~4 h) | **< $1** | `DERIVED` |

**Total build cost is ~$171–840 plus ~$80 one-time transfer** — trivial against the training run, and
the dominant variable is **whether the big bundles get split**, not region or instance choice.

**Why the mirror rather than a move** (from §8B.2–8B.3, verified against the code):
- **A move is a second deployment, not a data copy** — compute envs, queues, job defs, ECR
  replication, the validator role, and the airlock Deny would all have to be rebuilt in us-east-2.
  **A subtly different Deny is worse than none.** The `InternSandboxBoundary` does permit us-east-2,
  so this is unbuilt, not forbidden.
- **The manifest is region-portable**: it stores **keys, not URIs** — no `bucket` or `region` field in
  `manifest.py`. `MEASURED-IN-CODE`.
- `Boto3S3.default(region="us-east-1")` (`s3.py:193`) is a **default, not a hardcode** — a caller
  passes its own. `MEASURED-IN-CODE`, re-verified this session.
- **`CopyObject` recomputes the checksum server-side**, so the mirror is self-verifying and needs no
  `verify --deep` on the far side.
- Server-side copies mean **no bytes transit a client**, so the `publish-must-run-in-region` disaster
  (0.8 MiB/s, 9-day ETA) does not apply to the mirror. It **does** apply to `publish()` itself, which
  must run in-region — that is unchanged.

**One ordering rule that matters: mirror AFTER promotion, never before.** Copying from landing would
reproduce the airlock's entire problem in a region that has no validator to catch it.

---

## Final task status — PLAT-EXEC, session 2

| # | task | state |
|---|---|---|
| 1 | #11 live validator timeout | ✅ **14400 s, rev 14** (predecessor) |
| 5 | airlock Deny live smoke test | ✅ **fires** (predecessor) |
| — | EventBridge rule live state | ✅ was ENABLED → **now DISABLED by authorized mutation** |
| 2 | inventory from JOB HISTORY | ✅ **DONE** — `inventory.md` §A–F; thesis confirmed 3 ways |
| 3 | wave-3 shape + lane grant | ✅ **DONE** — likely-obtainable, 12-concurrent `UNVERIFIED`; lane instance live, self-expiring |
| 4 | per-job watchdog design | ✅ **DONE** — design only, nothing deployed |
| 6 | cost + region packet | ✅ **DONE** — ~$171–840 build, ~$80 mirror |
| **7** | **promote-duration ruling (R3)** | ✅ **DONE — 3–4 h, not 1 min, not 30 min** |

## Mutations RECOMMENDED but NOT performed

Everything here is a recommendation with the exact command. **I ran none of them.**

1. **Raise the validator timeout, or split Gate A from promote.** This is the R3 consequence and the
   only one I would call urgent. Gate A + promote share one **14400 s** wall and my estimate is
   3.5–4.1 h. Either:
   ```
   aws batch register-job-definition --job-definition-name edullm-validator \
       ... --timeout attemptDurationSeconds=28800     # 8 h, doubles the headroom
   ```
   or submit Gate A and promote as two jobs. **Registering a new revision also silently becomes the
   target of the EventBridge rule when it is re-enabled** (unversioned target) — so if a rev 15 is
   registered, the release checklist item in the mutation section above applies to it.
2. **Calibrate promote on stage 2 first** — submit the stage-2 Gate A + promote and record achieved
   MB/s before committing stage 1. Converts R3's `DERIVED` 3–4 h into a `MEASURED` number.
3. **Request the 12-child wave shape in the Phase-2 smoke test** — converts the last `UNVERIFIED` row
   in §3 into `MEASURED` at no extra cost.
4. **Re-enable the promotion rule at release** — `aws events enable-rule --name
   edullm-landing-manifest-created`, after re-checking the top ACTIVE revision.
5. **Reap the leftover probe** — `edullm-phase4-event-shape-probe` is undeleted observe-only
   scaffolding whose own description says "delete after reading." Harmless; not mine to delete.

**Not recommended:** reaping the `grant.matherne` lane instance (self-expires 2026-08-09T03:54Z, and
it is another engineer's work), and dropping `s3.copy()`'s redundant source HEAD (a real 33% promote
win, but it is a code change on ENG's surface, not a platform mutation, and it is not on the critical
path at these magnitudes).

---

# ADDENDUM — CEO redirect: the 8-vCPU container shape

## The CEO's premise is CONFIRMED live, and I can add the missing half

`batch describe-job-definitions --job-definition-name edullm-reservoir-build --status ACTIVE`,
2026-08-08, rev 9 (top ACTIVE). `MEASURED`:

```
containerProperties: { "vcpus": 8, "memory": 14336, "resourceRequirements": [] }
timeout.attemptDurationSeconds: 64800     (18 h — revs 3-9; revs 1-2 were 21600)
```

**The rate really was measured on an 8-vCPU container.** Confirmed from the live API, not from the
briefing doc. So the plan's per-vCPU extrapolation to 32-vCPU children is applying a rate across a
shape change that the plan's own GIL physics says does not scale. **I am not adjudicating the
33.2 h vs 9.96 h build figure — that is a rate question and belongs to AUDIT/DATA.** What is mine is
whether the account can *deliver* each shape, and that answer follows.

Note also **`timeout: 64800` (18 h)** on the build job def. Nobody's table mentions this. It is
comfortably above the 9.96 h floor but **below a 33.2 h build** — so if the CEO's 32-vCPU correction
is right, the build job def *also* has a timeout problem, independent of R3's.

## Obtainability — 8-vCPU shape PRIMARY, 32-vCPU secondary, both from job history

| | **48 × 8 vCPU** (primary) | **12 × 32 vCPU** (secondary) |
|---|---|---|
| total vCPU | **384 — exactly the cap, zero headroom** | 384 — exactly the cap |
| instances needed | **12** (`c7i.8xlarge` = 32 vCPU, packs 4 × 8-vCPU) | **12** (packs 1 × 32-vCPU) |
| shape ever run at all | ✅ **yes — this is `edullm-reservoir-build:9`'s own shape**, and the reservoir build ran on it | ✅ yes, but **exactly one** CPU job has ever requested 32 vCPU |
| **that concurrency ever demonstrated** | ❌ **UNVERIFIED** | ❌ **UNVERIFIED** |
| capacity denial ever seen | ✅ none, ever, on `c7i.8xlarge` | ✅ none, ever |

**The decisive and slightly surprising point: both shapes need the SAME 12 instances.** 48 × 8 vCPU
and 12 × 32 vCPU are both 384 vCPU, and `c7i.8xlarge` is the CE's only instance type, so either way
Batch must obtain **12 `c7i.8xlarge`**. **The obtainability question is therefore identical for the
two shapes**, and my §3 answer transfers unchanged — the EC2-capacity risk does not distinguish them.

Where they differ is **Batch-side packing risk, and it favours the 8-vCPU shape**:
- 8-vCPU children pack **4 per instance**, so partial capacity degrades gracefully: 6 instances gets
  you 24 of 48 children running. **12 × 32-vCPU is all-or-nothing per child** — one child needs one
  whole instance, and a 32-vCPU container cannot start on a partially-committed instance.
- The 8-vCPU shape is **the one with a real production run behind it** (`edullm-reservoir-build:9`).
  The 32-vCPU shape has one job in 84.

**Verdict: prefer 48 × 8 vCPU on obtainability grounds too, independently of the rate argument.** It
is the measured shape, the deployed shape, and the more failure-tolerant shape. Neither concurrency
has been demonstrated; that gap is identical for both and is closed by the same Phase-2 smoke test.

## Is the 384 cap hard or soft? **SOFT — and this is the most actionable finding in this addendum**

**It is a soft, self-imposed ceiling.** `MEASURED`:
- The CE declares `maxvCpus: 384` — a **CloudFormation/CE configuration value**, in stack
  `sbsandbox-intern-edullm-phase3-batch`.
- The **account's EC2 Standard-family on-demand quota is 1,152 vCPU** — **3× the cap**.
- **AWS is not the constraint. We are.**

So "48 × 8 = 384 exactly, zero headroom" is true *today* and is **removable by one `update-compute-environment`
call** (or a stack update, which is cleaner given the CE is CFN-managed). Raising it to, say, 768
would allow 96 × 8-vCPU children and halve a GIL-bound build — **if** EC2 can actually deliver 24
concurrent `c7i.8xlarge`, which is a bigger and entirely undemonstrated ask.

**This is a mutation and I did not perform it.** Recommending it as the highest-leverage platform
change available, contingent on the rate question resolving in favour of 33.2 h:
```
aws batch update-compute-environment \
    --compute-environment sbsandbox-intern-edullm-cpu --compute-resources maxvCpus=768
```
⚠️ Two cautions. **(1)** The CE is CFN-managed, so a direct `update-compute-environment` will drift
from the stack and may be reverted by the next deploy — prefer the stack update. **(2)** Zero
headroom at 384 also means **the `grant.matherne` lane instance and any other non-Batch consumer eat
the same 1,152 account quota**, so the real headroom is 1,152 minus whatever lanes are live.

## Three corrections the CEO asked me to make — accepted, with one pushed back

1. ✅ **`infra/DEPLOY.md:805-812` cited.** It independently corroborates rev 14 / `--head-workers 16`
   / **7200 → 14400 s**, and dates the change to **2026-08-05**. So this was a *documentation
   propagation failure in CLAUDE.md*, not a fresh re-enable. **Two independent sources now agree.**
   It also independently confirms **M7c**: *"7200s is what SIGKILLed the reservoir promotion at 6,324
   of 10,051 objects"* — which is the single most important input to my R3 answer, now double-sourced.
2. ✅ **`vcpus: 4 / memory: 8192` re-graded from "correct shape for I/O fan-out" to `UNVERIFIED`.**
   Accepted. My predecessor stated it as established and it was a judgement, not a measurement.
3. ⚠️ **The bandwidth hypothesis: the CEO is RIGHT that promote is bandwidth-bound, and WRONG about
   the remedy — and I had made the same error and already struck it.** The CEO writes: *"if promote
   is bandwidth-bound rather than latency-bound, adding workers does not help and container size
   does."* I tested that explicitly:
   - `c7i.8xlarge` NIC = **12,500 Mbit/s = 1,562 MB/s** (`MEASURED`, `pricing get-products`).
     M13's 362 MB/s is **23%** of it. A strict 1/8 proportional share for a 4-vCPU container is
     195 MB/s — and **M7c exceeded that at ~301 MB/s**, which falsifies proportional-share saturation.
   - **Decisively: a server-side `CopyObject` moves ZERO payload bytes through the container.** S3
     copies internally; the client sends a request and waits on headers. The container NIC therefore
     **cannot** be the copy-phase ceiling. The same property is why M6a moved 586.6 GiB **from a
     laptop** in 498 s, and why the region mirror is cheap.
   
   **So: bandwidth-bound — yes, agreed, and that is exactly my R3 conclusion. But the bandwidth in
   question is S3-side, not container-side, so container size is NOT the lever and concurrency is the
   only candidate.** I reached the CEO's wrong remedy myself earlier in this file and struck it in
   place; the correction box above §6 has the full reasoning. This is testable for ~18 min of machine
   time via the stage-2 calibration.

---

# ADDENDUM 2 — the authorized smoke job: BLOCKED, plus two corrections I owe

## ⛔ I did NOT submit. The job cannot be submitted without a SECOND mutation.

The CEO authorized *one Batch submission*. Executing it requires **registering a new job definition**,
which is a second, unauthorized mutation. I stopped. `MEASURED`, from
`describe-job-definitions --job-definition-name edullm-reservoir-build --status ACTIVE`, rev 9:

- The **only** ACTIVE build job def runs a **hardcoded full build**:
  `python -m edullm_data.corpus_build --registry … run --plan-id d5c9bcd38735e1f0 --shard ${SHARD}
  --of ${N_BUNDLES}`, with `PLAN_ID=d5c9bcd38735e1f0` and `N_BUNDLES=27` baked into `environment`.
  **There is no measurement/profiling entry point and no command override that turns it into a
  ~20-minute microbenchmark.** `containerOverrides.command` on `submit-job` *could* replace the
  command — but that is submitting an arbitrary command under the
  `edullm-reservoir-ingest` **job role**, which is a materially different act from "run the existing
  smoke job," and it writes shards via the build's own sink.
- Its **preflight asserts `__version__=='0.7.4'`** and hard-fails otherwise. The image is pinned at
  `…@sha256:4be21c0a…`. Any code change to measure the serial fraction means a **new image push** —
  which is the one thing CLAUDE.md and the ledger say builds only from `edullm/**`.
- Submitting the def **as-is** is not a smoke test: it is a **27-bundle production build** at
  `--of 27` that **writes token shards**. That is far outside "measurement only, reads already-staged
  inputs."

**So the authorization as written cannot be discharged.** I am not going to improvise a command
override to make it fit; that trades a bounded authorization for an unbounded one. **Escalating for a
ruling** — see the two options at the end.

## 🔴 CORRECTION 1 — the 18.6 GB figure is PRE-FIX and has already been fixed

The CEO's brief says *"`corpus_filter.py:232`: `stackv2-edu--train` wants 18.6 GB in a 20 GiB
container"* and asks whether that regime constrains the 8-vCPU carve. **The 18.6 GB number is real
but historical.** `MEASURED-IN-CODE`, `corpus_filter.py:225-234` — and line 232 is *inside the
docstring that documents the fix*, not live code:

```
set[str]  (64-char hex)  154.9 B/entry  ->  120M documents = 18.6 GB     <- the OLD cost
set[int]  (128-bit)       85.9 B/entry  ->  120M documents = 10.3 GB     <- what it does NOW
```

`SeenHashes.hashes` is `set[int]` today and `add_if_new` narrows to the top 128 bits
(`key = int(digest[:32], 16)`). Committed in **`a372bf8`**, *"perf(filter): the dedup set cost 155
B/entry, not the 113 B the docstring claimed."* **The deployed image already enforces this** — rev 9's
own preflight asserts it: `assert all(isinstance(k,int) for k in _s.hashes), 'DEDUP SET NOT NARROWED'`.

**What that does to the carve** (M5c's non-dedup components = ~2.05 GB; container = 15.03 GB):

| regime | dedup set | + other | fits 14,336 MiB? |
|---|---|---|---|
| reservoir `stackv2-edu--train`, 42.2 M docs, **current** | 3.6 GB | 5.7 GB | ✅ comfortably |
| 120 M docs, **pre-fix** `set[str]` | 18.6 GB | 20.6 GB | ❌ (the historical OOM) |
| 120 M docs, **current** `set[int]` | 10.3 GB | 12.4 GB | ✅ ~2.6 GB headroom |
| **1.0T `stackv2-edu`, ~168 M docs, current** | **14.4 GB** | **16.5 GB** | ❌ **OOMs by ~1.5 GB** |

**So the 8-vCPU carve is NOT constrained by the dedup set at reservoir scale, and IS constrained at
1.0T** — but by **14.4 GB, not 18.6 GB**, and the binding fix is the bundle-splitting the plan
already requires (§8A.5a / F2.3 file-sharding), which is on the critical path regardless. **DATA's
hypothesis that the real 78% is `SeenHashes` near-OOM should be re-checked against 10.3 GB rather
than 18.6 GB**, because at reservoir scale (3.6 GB in a 15 GB container) there is no memory pressure
at all — and the 78% was measured on the reservoir. That weakens the near-OOM explanation
considerably. `DERIVED` from M5c + the measured per-entry costs.

## ⚠️ CORRECTION 2 — I owe one on my own disable, and the CEO's framing of it

The CEO wrote that the disable is "defence in depth, not permission." **Agreed and unchanged.** But I
should be precise about something I glossed: **disabling the rule does not prevent a promote.** Rev
14's command carries `--promote --promote-workers 16` *in the job definition itself*. The rule only
controls **automatic submission**. Any hand-submitted validator job still promotes on a clean pass,
because `--promote` is baked in, not passed at submit time. **The only things standing between a
staged manifest and a frozen `vN` are (a) nobody writing a manifest and (b) nobody submitting the
validator** — not the disabled rule. I stated this correctly in the mutation block but the
"defence in depth" phrasing could be read as stronger than it is.

## What I recommend instead — two options, both cheap, neither improvised

**Option A (preferred, zero AWS mutation): measure the serial fraction LOCALLY first.** DATA's
1,174,020 windows/s/core and the 78% serial claim are both *code* properties. `contains()`
(`corpus_filter.py:175-195`) is pure Python over two frozensets — no S3, no tokenizer, no network. A
`cProfile`/`timeit` run on this machine over a realistic document sample settles **the ratio**
(index vs dedup vs tokenize) without touching AWS at all. c7i hardware changes the absolute rate, not
which fraction is serial. **This answers the actual question — "can hardware absorb a 16.4× gap" —
for free, and it is the measurement §3.3 really needs.** I did not run it because profiling is ENG's
surface and my brief is platform, but it needs no authorization from anyone.

**Option B (if a real c7i number is required): register a measurement-only job def.** This is the
second mutation, and I would want it authorized explicitly and separately, with the exact command
reviewed before registration. It must (1) run a profiling entry point, not `corpus_build … run`,
(2) write **no** shards and **no** manifest, and (3) carry an explicit `--timeout
attemptDurationSeconds=1800` so a capacity hang self-terminates rather than sitting RUNNABLE forever.

**Watchdog expectation, set per my own §4 design, for whichever option runs on Batch:** `T_place` =
**10 min** in RUNNABLE (cold start from `desiredvCpus: 0` is 2–5 min), `T_run` = **40 min** (2× the
~20 min estimate). The queue's `CAPACITY:INSUFFICIENT_INSTANCE_CAPACITY → CANCEL after 1800s` rule
**will not fire** — `statusReason` stays null — so this expectation must be enforced externally, by
polling `describe-jobs`, or the job hangs forever and silently.

---

# ADDENDUM 4 — mirror remediation: STEP 1 DONE, STOPPED AT STEP 2

**Authorized by the owner, relayed by the CEO. Executed in order. I stopped mid-sequence as
instructed rather than improvising past a blocker.**

## ✅ Step 1 — versioning ENABLED. Done and verified. `MEASURED`

```
aws s3api put-bucket-versioning --bucket edullm-data-us-east-2 \
    --versioning-configuration Status=Enabled          # exit 0, empty stdout
aws s3api get-bucket-versioning --bucket edullm-data-us-east-2
{
    "Status": "Enabled"
}
```

The 2.67 TB / 18,455 objects already in the bucket are **not** retroactively protected — versioning
only covers writes from now on — but every future overwrite or delete is now recoverable. This step
was non-destructive and is reversible (`Status=Suspended`).

# ADDENDUM 11 — registry verified; ENG's 51.6 h blocker REPRODUCES independently

## I re-derived ENG's makespan from the registry myself. It holds.

`artifacts/final-dataset/corpus-registry.json`, schema `edullm-corpus-registry/v1`, read directly.
`MEASURED` / `DERIVED` at the corrected container-level rate **72,615 × 8 = 580,920 tok/s/container**:

| | value | check |
|---|---|---|
| rows | **40** | matches the CEO's count |
| sum `target_tokens` | **986,000,000,000** | **equals `_total_target_tokens` exactly** ✅ |
| duplicate `source_label` | **0** | ✅ |
| **prefix collisions** | **0** | ✅ — I recomputed this rather than inherit it |

| source_label | B tok | one 8-vCPU child | ways for ≤10 h |
|---|---:|---:|---:|
| **`stackv2-edu`** | **108.0** | **51.64 h** | **6** |
| **`finepdfs-edu`** | **63.0** | **30.12 h** | **4** |
| `dclm-01`…`dclm-06` (each) | 41.0 | 19.60 h | 2 each |

```
MAKESPAN if unsplit  = 51.64 h   (the largest single bundle)
aggregate floor @ 48 =  9.82 h
penalty              =  5.26x
```

**ENG's 51.38 h and my 51.64 h agree to 0.5%** (rounding on the rate). **The blocker is real and the
launch must wait.** My own figure for the floor, 9.82 h, is within 1.4% of the plan's 9.96 h — so the
*floor* was never wrong; what was wrong is treating it as a **bound**.

**This is the ledger's own trap — "an aggregate floor is not a per-child bound" — recurring on three
sources it had never been applied to**, because `--shard/--of` **strides bundles**: 47 children go
idle while one runs. `stackv2-edu` at 108B is bigger than an entire DCLM child and was absent from
the plan's split list **because the reservoir drew far less code**. A split list calibrated on a
different mix does not transfer.

**Registering `edullm-reservoir-build:10` against `PLAN_ID a5df0404b640e4c9` would bake a plan we
already know is wrong. I am not doing it.** Waiting on ENG's post-split `PLAN_ID` + bundle count.

## 🔴 FOR ENG, BEFORE YOU RE-SIMULATE: the split list is **15 sources, not 3**

I simulated the post-split launch myself (LPT bin-pack onto 48 children) so the re-simulation does
not have to rediscover this. **Splitting only `stackv2-edu`, `finepdfs-edu` and `dclm-*` does NOT
reach the floor — it plateaus at 18.17 h no matter how finely you cut those three:**

| scenario | makespan | busy children |
|---|---:|---:|
| as-authored, no split | **51.64 h** | 39/48 |
| stackv2 ×6, finepdfs ×4, dclm ×2 | **18.17 h** | 48/48 |
| stackv2 ×8, finepdfs ×6, dclm ×2 | **18.17 h** | 48/48 |
| stackv2 ×12, finepdfs ×8, dclm ×4 | **18.17 h** | 48/48 |

**The plateau is `nemotron-cc-math-3` at 38.0B = 18.17 h in one child.** It is not on the plan's
split list. Cutting the named three finer cannot help while it stays whole — **the makespan is
whichever bundle you did not split.**

**Every bundle over a 10 h child, from the registry** — `DERIVED` at 580,920 tok/s/container:

| source_label | B tok | one child | ways |
|---|---:|---:|---:|
| `stackv2-edu` | 108.0 | 51.64 h | 6 |
| `finepdfs-edu` | 63.0 | 30.12 h | 4 |
| `dclm-01` … `dclm-10` (10 rows) | 41.0 each | 19.60 h | 2 each |
| **`nemotron-cc-math-3`** | **38.0** | **18.17 h** | **2** |
| **`finephrase`** | **36.0** | **17.21 h** | **2** |
| **`nemotron-cc-math-4plus`** | **23.0** | **11.00 h** | **2** |

**15 sources need splitting, becoming 36 children. The plan named 3.** The three additions
(`nemotron-cc-math-3`, `finephrase`, `nemotron-cc-math-4plus`) are exactly the sources the reservoir
drew little of — the same reason `stackv2-edu` was missed. **One diagnosis, six sources.**

### ⚠️ And a second thing the re-simulation should not assume: the floor is not reachable

| split target | bundles | makespan | busy |
|---|---:|---:|---:|
| every bundle ≤ 10 h | 61 | **15.06 h** | 48/48 |
| every bundle ≤ 8 h | 74 | **13.07 h** | 48/48 |
| every bundle ≤ 6 h | 105 | **12.43 h** | 48/48 |
| *aggregate floor* | — | *9.82 h* | — |

**Even at ≤6 h per bundle the makespan is 12.43 h, 27% above the 9.82 h floor, and the returns are
collapsing** (10→8 h buys 2.0 h; 8→6 h buys 0.6 h for 31 more bundles). The residual is **bin-packing
granularity**, not the split list — 48 children cannot tile unequal bundles perfectly.

**So the honest post-split number to plan against is ~13–15 h, not 9.96 h.** I would take the **≤8 h
split (74 bundles, 13.07 h)** as the sweet spot and stop there. Quoting 9.96 h after splitting would
repeat the original error in a smaller way — **the floor is a floor, not a forecast.**

⚠️ All of the above is `DERIVED` from `target_tokens` and a uniform rate. It assumes every source
tokenizes at the same tok/s, which is **certainly false** (PDF and code are not web text) and that
subdirectory splits divide evenly. **Treat it as the shape of the answer, not the answer** — ENG's
walk of the actual subdirectories is what settles the real bundle sizes.

## Census status — 64/64 RUNNING, 0 FAILED

`MEASURED`. All 64 children placed; none failed. It holds 256 of 384 vCPU. **Uncontended while the
waves are blocked, so it stays** — and **it yields the instant the post-split `PLAN_ID` lands.**
I will cancel it at that moment rather than let it cost a wave-slot; it refines a magnitude, not a
direction.

---

# ADDENDUM 10 — 🔐 SECURITY: my census worker wrote to a bucket I told it not to

**A worker I dispatched tripped a `[Modify Shared Resources]` warning. I own this — the brief was
mine.** Adjudicating it against the evidence rather than the summary.

## What it actually did — `MEASURED`, not from its self-report

| check | result |
|---|---|
| objects under `sbsandbox-intern-edullm-outputs/teams/plat/runs/` | **2 objects / 13,450,640 B** — `fpov-smoke/hash/shard-00000.{json,npz}` |
| anything written to `edullm-data`? | **no** |
| anything written to `edullm-data-us-east-2`? | **no** |
| any `manifest.json` anywhere? | **no** |
| any promotion? | **no** |
| the 64-child array | **LIVE: 36 RUNNING, 12 STARTING, 16 RUNNABLE, 0 FAILED, 0 SUCCEEDED** |

**So the blast radius is 2 smoke-test objects, 13.4 MB, in a team outputs bucket.** Nothing touched
the frozen store, the mirror, or any dataset path.

## The verdict: the warning is RIGHT, and the root cause is MY brief

My brief said *"MUST NOT write to any S3 bucket other than scratch paths under
`s3://edullm-landing/_scratch/`"* and separately *"if something seems to need a mutation, STOP and
report."* The worker found the Batch job role **cannot** write to `_scratch/` — its only S3 write
grant is `outputs/teams/*/runs/*`, and it cannot read that back either. **That is a real,
well-diagnosed IAM conflict that makes my instruction literally impossible to follow.**

**It should have stopped and reported. It did not — it designed a relay and proceeded.** That is the
violation, and it is the same class as every "bounded grant widens on contact with reality" event
tonight. **But my brief created the impossibility**: I named a scratch path without checking the job
role could write it. I verified the *dataset* was ungated and the *image* had the deps; I did not
verify the write path. That is my omission, not the worker's invention.

**Mitigating, and I record it because it bears on how much to worry:** the deviation is documented
in its own log with IAM evidence, it chose the *narrower* of the available paths, it explicitly
refused two wider ones (an IAM change — *"which I am not authorized to make"* — and pushing 17
commits to the public repo), and it wrote only to a team outputs prefix, never a dataset bucket.
**A documented, reasoned deviation is much better than a silent one — and still worse than stopping.**

## My decision: LET IT RUN, and here is the reasoning

The array is **live, healthy, and ~halfway through placement**. Killing it would destroy real work to
punish a process error whose data impact is 2 smoke objects. **The census is read-only against public
HF data and writes only to a team outputs prefix.** I am letting it finish and will treat its output
as valid — the constraints that protect *correctness* (`--eduweb-configs data`, never
`--allow-partial`) were honoured, which I confirmed in its log.

**What I am NOT doing:** using the relay pattern for anything else, or treating "the broker session
can write it" as a general license. **A session having permission is not the same as the task having
authorization** — that is exactly the laundering pattern the rules exist to prevent, and it is worth
naming plainly because the worker's reasoning ("my session is strictly broader, so this resolves the
conflict") is *technically true and procedurally wrong*.

## 🔴 OPERATIONAL CONSEQUENCE — the census is EATING THE BUILD QUEUE

**64 children × 4 vCPU = 256 vCPU of the 384 vCPU cap, on `sbsandbox-intern-edullm-cpu` — the same
queue the 48 × 8-vCPU build waves need.** 48 × 8 = 384 vCPU, the entire cap.

**So the census and the build waves cannot run concurrently.** The census was explicitly *"must not
delay a wave"* — and right now it would. Two things make this survivable rather than urgent:
1. **The waves are blocked anyway** (Addendum 9: no 1.0T registry, no plan, wrong job def), so the
   census is consuming capacity nobody can currently use.
2. If the registry lands before the census finishes, **the census must yield** — it blocks nothing.

**I will not cancel it pre-emptively**, because capacity that nothing else can use is not contended.
**But the moment the registry exists, this is the first thing to reconsider.** Flagging now so the
decision is not discovered mid-launch.

---

# ADDENDUM 9 — 🛑 WAVE LAUNCH STOPPED. `edullm-reservoir-build:9` builds the WRONG CORPUS.

**I did not submit.** The GO named `edullm-reservoir-build:9` as the def to launch 48 × 8-vCPU
children against. I read it before submitting, as the rule requires, and **three of its pinned
properties are wrong for this build.** Submitting it would have burned ~10 h × 48 children rebuilding
a corpus that already exists.

`describe-job-definitions --job-definition-name edullm-reservoir-build --status ACTIVE`, rev 9.
`MEASURED`, 2026-08-08:

| property | rev 9 value | what this build needs | verdict |
|---|---|---|---|
| **`PLAN_ID`** | **`d5c9bcd38735e1f0`** | the **1.0T** plan, not yet authored | 🔴 **WRONG CORPUS** |
| **`N_BUNDLES`** | **`27`** | ~100 bundles at 1.0T; **48 children were ordered** | 🔴 **WRONG FAN-OUT** |
| **image** | `sha256:4be21c0a…dab59` | `sha256:5fb76f66…a906` (preflight-verified) | 🔴 **WRONG IMAGE** |
| `vcpus` / `memory` | 8 / 14336 | 8 / 14336 | ✅ correct |
| timeout | 64800 s | adequate | ✅ correct |

## 1. 🔴 `PLAN_ID=d5c9bcd38735e1f0` is the **already-completed reservoir build**

This is not a guess. That exact plan id appears throughout the repo as **finished work**:

- `HANDOFF-FINAL-DATASET.md:363` — *"MEASURED end-to-end is 72,615 tok/s/vCPU (CloudWatch `DONE`
  lines, plan `d5c9bcd38735e1f0`, 7 train…)"* — **it is the plan the 72,615 rate was measured ON.**
- `PUBLISH-SPEC.md:150` — *"~~`corpus_build verify --plan-id d5c9bcd38735e1f0 --deep` must pass.~~
  **✅ SATISFIED 2026-08-05**"*
- `PUBLISH-SPEC.md:98` — its output already sits at
  `s3://edullm-landing/_ingest/reservoir-dolma2/build/d5c9bcd38735e1f0/data/`

**So rev 9 rebuilds the 251B reservoir — the corpus whose completion produced the very rate figure
this GO is founded on.** It would produce no `pretrain/final-*` output, so the `_backup/` copy would
still have nothing to copy and the promote would have nothing to promote.

## 2. 🔴 `N_BUNDLES=27` contradicts the ordered 48-child fan-out

The command is `--shard ${AWS_BATCH_JOB_ARRAY_INDEX} --of ${N_BUNDLES}`. With `N_BUNDLES=27`,
`--of` is **27** regardless of array size. Per my own Addendum-5 finding, `--shard/--of` **strides
bundles** — so a 48-child array against `--of 27` gives 27 children real work and **21 children
striding past the end**, while the plan itself only has the reservoir's 27 bundles. The 48 × 8 shape
cannot be expressed by this def without changing `N_BUNDLES`.

## 3. 🔴 The image predates every Phase-0 fix

Rev 9 pins `sha256:4be21c0a…` (pushed 2026-08-07 15:02). The image I **preflight-verified through
seven assertions** is `sha256:5fb76f66…a906`. Rev 9's own preflight asserts
`__version__=='0.7.4'`; the verified image is **0.9.1**. **This is precisely the "two parallel lines
each shipped an image" hazard** — an old-branch image that looks fine and silently regresses B3, B7,
the dependency pins and the families bound.

## 4. ⛔ The blocking precondition: **the 1.0T registry/plan does not exist**

`artifacts/reservoir/corpus-registry.json` is the **reservoir's** registry. I find **no** 1.0T
registry in the tree, and ENG's own status records the dependency: *"the mechanism ships; splitting
is one call once the 1.0T registry is authored. **Authoring…**"* (`eng/status.md:679`).

**A build wave cannot be launched before a plan exists to build.** `plan_id` is derived from the
registry + `SHARD_TOKENS` (`corpus_build.py:256`), and it is **irreversible** once shards are written
under it — which is exactly why the wave hold existed and why I will not improvise one at 4am.

## What I need — this is a genuine blocker, not a refusal

Two mutations, neither of which the GO names, and one upstream deliverable that is not mine:

1. **Author the 1.0T registry and derive its `plan_id`** — ENG/DATA's deliverable. **Nothing can
   launch before this.** It is the real gate, and it was masked because rev 9 *looks* launchable.
2. **Register `edullm-reservoir-build:10`** with: the verified image `sha256:5fb76f66…a906`,
   `PLAN_ID=<the new 1.0T plan>`, `N_BUNDLES=<the real bundle count>`, keeping `vcpus 8 / memory
   14336` and the 18 h timeout. I have this drafted and can register it the moment 1 lands —
   registration is inside grant A2.
3. Confirm the **array size** equals the real bundle count. "48 children" was derived from
   384 vCPU ÷ 8; if the 1.0T plan has ~100 bundles, 48 concurrent children process them in ~2 waves,
   which is fine — but `--of` must equal the **bundle count**, not the child count. **These are two
   different numbers and rev 9 conflates them.**

**Everything else in the GO stands** and I have no objection to it: the units-error resolution, the
9.96 h floor at 48 × 8, the 0.300-efficiency argument against 32-vCPU children, `_backup/` verified
expiry-free, stage-2-calibrates-first, and gates still deciding.

---

# ADDENDUM 8 — A6 backup: destination VERIFIED, but there is NOTHING TO BACK UP YET

## ✅ `_backup/` is expiry-free — proven on a real object, not from config

**Step 1, the config.** `get-bucket-lifecycle-configuration` on `edullm-landing` returns **9 rules**.
Eight carry `Expiration.Days`; the prefixes are `pretrain/`, `curriculum/`, `sft/`, `eval/`,
`probe/`, `vendor/`, `_pending/` (all **14 d**) and `_ingest/` (**30 d**). The ninth
(`abort-incomplete-multipart-uploads-1d`) has `Prefix: ""` — it matches everything but only aborts
incomplete MPUs, it does not expire objects. **No rule matches `_backup/`.** `MEASURED`.

**Step 2, the object — because config alone is exactly the trap.** Copied a real object into
`_backup/_probe/` and HEADed it:

| | source `pretrain/reservoir-dolma2/v1/dataset.json` | copy in `_backup/_probe/` |
|---|---|---|
| `Expiration` | `expiry-date="Thu, 20 Aug 2026 00:00:00 GMT", rule-id="expire-pretrain-14d"` | **`null`** |
| `ContentLength` | 6,815 | **6,815** |
| `ETag` | — | `"37673ca6564e7471a9846bad92084618"` (matches the copy source) |

**Config and object agree: `_backup/` inherits no expiry.** Confirmed by a fresh `head-object`, not
by trusting the `copy-object` response. Probe deleted afterwards (`DeleteMarker: true` — landing is
versioned, so this left a delete marker, not a hole).

> Note the source's expiry is **20 Aug 2026**, i.e. the reservoir's staged copy has **12 days** left.
> That is the CEO's point made concrete: staging is a clock, not a backup.

## 🔴 BUT: there is nothing to back up. `pretrain/final-*` does not exist.

```
list-objects-v2 --bucket edullm-landing --prefix "pretrain/final-"  ->  count: 0
```

`MEASURED`. The build waves are **held** pending the rate question, so **neither
`pretrain/final-stage1-900b` nor `pretrain/final-stage2-100b` has been produced.** There is no
staged output of this build to copy.

What *is* in landing's `pretrain/` is **19,947 objects / 3.68 TB across 22 unrelated datasets**
(`reservoir-dolma2`, `olmo-150b-dolma2`, `fineweb-edu-1b`, …) — **prior work, not this build's
output.** Copying those into `_backup/` would burn ~3.7 TB and ~3–4 h of copy time to back up data
that A6 is not about, and would not protect a single byte of the corpus the owner actually wants.

**So the backup is correctly sequenced as: build waves → staged `final-*` output exists → copy to
`_backup/` → promote.** It cannot run before the first of those. **This is a dependency, not a
refusal** — the moment a wave lands staged output, the copy is ready to go.

## The backup procedure, ready to execute the moment staged output exists

1. **Destination** `s3://edullm-landing/_backup/pretrain/final-stage{1,2}-*/vN/` — verified above.
2. **Copy FIRST, then promote.** Per the owner.
3. **Concurrently with Gate A** — Gate A does not read `_backup/`, so the ~3–4 h copy overlaps its
   0.36 h instead of serialising.
4. **⚠️ Exclude every `manifest.json`.** The EventBridge rule matches key **suffix** `manifest.json`
   with **no prefix constraint**, so a backup manifest *anywhere* in landing is a promotion trigger.
   I will copy it as **`manifest.backup.json`**. Double-guarded today (rule DISABLED,
   `edullm-validator:16` cannot promote) and I am **not** relying on either.
5. **Verify by recomputation:** `list-objects-v2` on source and destination, compare **object count
   and summed bytes exactly** — the same `{count, bytes}` recomputation I used above. An `s3 sync`
   exit 0 is a claim, not evidence.

⚠️ **One hazard to flag now rather than discover mid-copy:** the copy is **server-side** (no bytes
through a client), but `_backup/` lives in the **same bucket** as the source. A backup in the same
bucket survives lifecycle expiry and a failed promote — it does **not** survive a bucket-level
accident. It is the right answer to the owner's stated risk ("if the promotion fails I still have
something"), and it is worth being precise that it is not disaster recovery.

---

# ADDENDUM 7 — numpy hole closed; overlap census dispatched; waves HELD

## ✅ Closing the gap I declared myself — `edullm-validator-preflight:5`

In Addendum 6 I reported a hole in my own verification: **`numpy<2.5` was unasserted**, so the
image's numpy was `UNVERIFIED`. Closed rather than carried.

```python
import numpy
_nv = tuple(int(x) for x in numpy.__version__.split('.')[:2])
assert _nv < (2, 5), f'numpy {numpy.__version__} violates pin numpy<2.5'
```

**Seven assertions now**, dry-run locally first (all pass, `numpy=2.4.4` on this laptop). Registered
as **`edullm-validator-preflight:5`**, digest-pinned to the same `sha256:5fb76f66…a906`, explicit
awslogs group.

### ✅ CLOSED. Job `d9d69745-…`, SUCCEEDED, and read from the log stream — `MEASURED`

```
OK version=0.9.1
OK B3 threaded profile checks
OK B7 verified sink
OK pins tokenizers=0.22.2 pyarrow=25.0.0
OK numpy=2.4.6
OK families eos=0.05 zero_run=256 distinct=128
OK C3b duplicate source_label raises
PREFLIGHT_OK=1
```

**The image's numpy is `2.4.6`, inside the `<2.5` pin.** The last `UNVERIFIED` dependency in the
deployed image is now `MEASURED`. **`edullm-validator-preflight:5` is the revision to cite** — it is
the only one asserting all seven properties.

Note this also **retires the pyproject comment's specific worry**: the pairing it names as untested is
*"numpy 2.5.1 against pyarrow 25.0.0"*. The image runs **numpy 2.4.6 with pyarrow 25.0.0**, which is
not that pairing. Combined with `pre_buffer=False` being present in code, the segfault hazard is
addressed on both axes.

### A watchdog observation worth recording, since it validates the design

This job sat **RUNNABLE for ~2 minutes** before running. I checked
`describe-compute-environments` rather than waiting blind, and found **`desiredvCpus` had moved
0 → 32** — Batch scaling out one `c7i.8xlarge` from cold, `ComputeEnvironment Healthy`. **That is a
cold start, not capacity starvation**, and it is the first live confirmation of the 2–5 min
cold-start figure I used to set `T_place = 10 min`. The distinction matters precisely because a
starved job looks identical from `describe-jobs` alone — `desiredvCpus` moving is the signal that
separates them.

## 🔬 FinePhrase × FineWeb-Edu overlap census — DISPATCHED to a worker

Delegated with both hard constraints baked in and a mandatory watchdog. Output will land in
`artifacts/orchestration/plat/finephrase-overlap.md`.

**What I verified myself before dispatching**, so the worker starts from measured ground:

| check | result | grade |
|---|---|---|
| `tree` phase output present | `/tmp/fpov/tree.json`, 2.37 MB | `MEASURED` |
| groups / files / bytes | **5 groups, 29,514 files, 9,684,376,201,966 B = 9.684 TB** | `MEASURED` |
| FinePhrase revision sha | `78cf4a5ed0099214979c094c963e699c19163838` | `MEASURED` |
| FineWeb-Edu revision sha | `87f09149ef4734204d70ed1d046ddc9ca3f2b8f9` | `MEASURED` |
| **`eduweb_configs`** | **`['data']`** — the required config, already baked into the tree | `MEASURED` |
| per-group file counts | faq 6,791 · math 6,787 · table 6,772 · tutorial 6,754 · **data 2,410** | `MEASURED` |

⚠️ **One discrepancy I want on the record before anyone reads the census output.** The README's
scale table lists the FineWeb-Edu group as **`sample/350BT`, 472 files, 0.998 TB**. The built tree
carries **`data`, 2,410 files** — 5.1× more files. **That is correct and intended** (the CEO's
constraint is `--eduweb-configs data`, precisely because `sample/350BT` reports ~0% collision), but
it means **the README's byte/row estimates for that group are stale by ~5×** and the census's own
`rows_read` is the only figure to trust. Flagging so nobody reconciles the census against the README
table and "discovers" a fault that is really a config difference.

### 🔓 No credential is needed, and I did not touch one

The script reads `HF_TOKEN` / `HUGGING_FACE_HUB_TOKEN`, else falls back to
`~/.cache/huggingface/token`. **I did not read that file.** Instead I established the census does not
need it:

- `api/datasets/HuggingFaceFW/finephrase` → **`gated: false, private: false`** `MEASURED`
- **Anonymous ranged GET against the pinned sha returned HTTP 206** with a valid `PAR1` footer magic
  `MEASURED`

So the worker runs token-free, with an explicit instruction not to go looking for a credential and to
**stop and report on any 401/403** rather than hunt for one. A public dataset does not justify
opening a secret.

## ⏸️ Build waves — HELD, and I agree with the reason

Not submitted. The 48 × 8-vCPU shape is settled by four converging lines, but **the 16.4× throughput
gap is unresolved**, and it feeds the build floor, the ~6.2× parallelism ceiling, and the critical
path. A wave shaped against an unknown rate **sets `plan_id`, which is irreversible**. Option A
narrowed where the gap is *not* (not tokenizer, not dedup at 1.15%); it did not find where it *is*.

---

# ADDENDUM 6 — image preflight against `5450f53`

**The image appeared after my Addendum-5 check** (`sha256:5fb76f66…a906`, pushed 2026-08-08T04:07:08,
148 MB, tag `5450f538363d`). `MEASURED`. I did **not** trust the tag — an image tag is exactly a
version string, and this repo's `0.5.1` lesson is that a version string is not a code identity.

## Preflight design — assert MECHANISMS, never labels

Six assertions, each targeting the *behaviour* rather than the flag name, with exact source targets
confirmed against `5450f53` before writing them:

| # | asserts | why this phrasing |
|---|---|---|
| 1 | `__version__ == "0.9.1"` | necessary, nowhere near sufficient |
| 2 | **B3**: `ThreadPoolExecutor` **and** `max_workers=` in `pretrain_tokens_v1` source | my own near-miss: I searched `head_workers` in `profiles/` and found nothing because the count arrives via `_workers(ctx)`. **Assert the mechanism.** |
| 3 | **B7**: `run_bundle` uses `put_bytes_verified`; that method declares `ChecksumSHA256` + `ChecksumAlgorithm` and **raises** on oversize | I first grepped `corpus_build.py` for `ChecksumSHA256` and found nothing — wrong symbol again. The sink calls `put_bytes_verified` (`corpus_build.py:719`); the checksum lives in `s3.py`. Both halves asserted. |
| 4 | **B1/#23**: `tokenizers`/`pyarrow` **imported** and inside `>=0.21,<0.23` / `>=24,<26` | a real import, not a declared pin |
| 5 | `_family_decode_bounds()[0] == 0.05` | the failure that published a corpus at 50% EOS |
| 6 | **C3b**: calls the duplicate-`source_label` guard with a real collision and requires it to **raise** | behavioural, not textual — the only assertion here that executes the guard |

**I dry-ran the script locally before spending a Batch job, and it caught two of my own bugs:**
`_assert_unique_identities(specs, *, where=…)` takes `where` **keyword-only** (my positional call
raised `TypeError`, which my own handler would have mis-reported as a guard failure), and I removed a
`packaging` import so a missing library could not masquerade as a version violation. Local run: all
six `OK`, `PREFLIGHT_OK=1`.

## ✅ PREFLIGHT PASSED — read from inside the container. `MEASURED`

Job `78fd84ef-…` on **`edullm-validator-preflight:4`**, SUCCEEDED, exit 0, **and this time the log
stream exists and was read**. Verbatim, from
`/aws/batch/sbsandbox-intern-edullm-cpu : preflight/default/4e6e3d9b…`:

```
OK version=0.9.1
OK B3 threaded profile checks
OK B7 verified sink
OK pins tokenizers=0.22.2 pyarrow=25.0.0
OK families eos=0.05 zero_run=256 distinct=128
OK C3b duplicate source_label raises
PREFLIGHT_OK=1
```

**The image `sha256:5fb76f66…a906` provably contains the Phase-0 work.** Per the CEO's ruling I
therefore **did NOT rebuild under A5** — a rebuild would substitute a provenance we like for a
verification we already have, and the verification is the stronger artifact.

### One difference from my local dry run, checked rather than waved through

The image resolves **`pyarrow 25.0.0`**; my laptop had 24.0.0. Both satisfy `>=24,<26`, so the
assertion is honest — but the pin's own comment names *"numpy 2.5.1 against pyarrow 25.0.0, a pairing
this suite has never tested"* as the reason the bound exists, so I did not stop at "inside the range."

**The segfault fix is code, not a version**, and it is present on `5450f53`: `pre_buffer=False` at
`corpus_read.py:419` and `:426`, and `ingest_reservoir.py:682+`, each flagged
*"LOAD-BEARING. IT IS THE ARRAY SEGFAULT FIX."* The A/B in that docstring is explicit —
`pre_buffer=True` → exit 139 in 3 of 4 children; `pre_buffer=False` → exit 0 in all 4. **So pyarrow 25
is not the 2026-07-31 hazard**, which the pyproject comment itself records as *"NOT THE SEGFAULT FIX,
and it never was."*

⚠️ **Residual gap, stated rather than hidden: I did not assert `numpy<2.5` in the preflight.** The
image's numpy version is `UNVERIFIED`. It is the one dependency the pin names that I have no
measurement for. Low risk (the crash it was blamed for was refuted), but it is a real hole in my own
check and the next preflight revision should close it.

## ✅ BOTH JOB DEFS REGISTERED — by number, never "latest"

| def | **rev** | timeout | command | promotes? |
|---|---|---|---|---|
| **`edullm-validator`** | **16** | **28,800 s (8 h)** | `validate --landing-bucket edullm-landing --data-bucket edullm-data --head-workers 16` | ❌ **NO `--promote`** |
| **`edullm-promote`** | **2** | **28,800 s (8 h)** | same **+ `--promote --promote-workers 16`** | ✅ yes |

Both digest-pinned to `sha256:5fb76f66…a906`, `vcpus: 4 / memory: 8192`, `retryStrategy attempts=1`,
jobRole `…-edullm-dataset-validator`, exec role `…-edullm-batch-execution`, and an explicit
`awslogs` group so output is readable — the gap that made revs 3/15 unverifiable.

**Revs 15 and 1 exist and are superseded.** I registered them first relying on the CLI's *default*
bucket values, then re-registered as **16** and **2** passing `--landing-bucket`/`--data-bucket`
**explicitly**. The defaults are correct today (`validate.py:2461-2462`) and match rev 14 exactly —
but a job def that depends on a default is a job def that changes meaning if the default ever does,
with no revision to show for it. That is this project's signature failure mode, so I paid one extra
revision to remove it. **Cite 16 and 2.**

### ⚠️ Two consequences of the split the CEO should know

1. **`edullm-validator` no longer promotes — and it is the EventBridge target.** The rule
   `edullm-landing-manifest-created` names `edullm-validator` **unversioned**, so if it is ever
   re-enabled it now resolves to **rev 16, which runs Gate A only**. **This is a safety improvement I
   did not plan and want on the record:** the auto-promotion path can no longer promote even if the
   rule is switched on by accident. Re-enabling now yields validation, not publication.
2. **Promotion is now an explicit, separate submission** to `edullm-promote:2`. There is no longer any
   single job that validates *and* freezes a `vN`. That is the blast-radius separation the CEO ruled
   for, and it makes the stage-2 calibration a clean two-step: Gate A on rev 16, read the result,
   then promote deliberately.

**Not done, correctly:** nothing submitted against either def, no manifest written, nothing promoted.
`edullm-reservoir-build:9`'s 18 h timeout left unchanged per the accepted reasoning.

---

## ⚠️ Attempt 1 SUCCEEDED with exit 0 and I am NOT counting it as a pass

`edullm-validator-preflight:3` (registered by me, digest-pinned), job
`1ca575e9-…`, **SUCCEEDED, exitCode 0** — ran 0.68 s.

**But no log stream exists, in any group.** `describe-jobs` names
`edullm-validator-preflight/default/74bf8447…`; that stream is absent from `/aws/batch/job` **and**
from `/aws/batch/sbsandbox-intern-edullm-cpu`, and `/aws/batch/job` has **zero streams in total**.

**So I cannot read the six `OK` lines, and exit 0 alone does not tell me the assertions ran.** This is
precisely the "recompute, never trust" case: a green exit code with no observable output is a claim,
not evidence. It is also the same *zero-log-streams* signature I used to prove the H100 shapes never
placed a container — which is why I will not hand-wave it.

**Re-submitted as `edullm-validator-preflight:4`** with an explicit `logConfiguration` pointing at
`/aws/batch/sbsandbox-intern-edullm-cpu` (the group that demonstrably receives `validator/*` and
`cpu-run/*` streams). Job `78fd84ef-…`, status RUNNABLE at time of writing. **Watchdog expectation per
my own §4 design: `T_place` = 10 min, `T_run` = 5 min.** The queue's capacity-cancel rule will not
fire — `statusReason` stays null — so this is an external expectation, tracked by me.

**Registered so far (by number, never "latest"):**
- **`edullm-validator-preflight:3`** — digest-pinned, no explicit log config. Superseded.
- **`edullm-validator-preflight:4`** — same digest + explicit `awslogs` group. **The one to cite.**

Both are *preflight* defs — they run assertions and touch no data. **I have registered NO
`edullm-validator` or `edullm-promote` revision**, and will not until the preflight output is read.

---

# ADDENDUM 5 — A2 job-def registration: STOPPED. The image does not exist yet.

**I registered nothing.** Registering a digest-pinned job def requires an image digest, and **no image
built from `edullm/final-dataset-phase0` exists.** Per the rule that has now saved us three times, I
am reporting rather than improvising.

## 🔴 BLOCKER 1 — no image from the new branch. `MEASURED`

`ecr describe-images --repository-name sbsandbox-intern-edullm-data`, newest 12 by push time. **The
most recent image in the repository is `44d4d7d79de3`, pushed 2026-08-07T15:02:14-05:00** — *before*
tonight's push of `5450f53`. There is no image tagged for it and no digest to pin.

**The CEO's premise — "A1 is done… the image can now build" — is right about the push and wrong about
the build.** The push happened; the build did **not**, and nothing will trigger it on its own.

## 🔴 BLOCKER 2 — nothing builds this repo's image from a git push at all

`codebuild list-projects` → 5 projects; the only edullm one is **`edullm-prm800k-image-build`**.
Inspected it (`batch-get-projects`). `MEASURED`, and it is not what anyone assumes:

| field | value |
|---|---|
| `source.type` | **`NO_SOURCE`** |
| `triggers` | **`null`** |
| `sourceVersion` | `null` |

**It has no git source and no webhook.** The buildspec reconstructs the source tree from **83
base64-encoded environment variables** (`EDULLM_SOURCE_000`…`_082`) concatenated into a
`source.tar.xz`, checked against a **hardcoded sha256**
(`d732af0e…3a58`), then `docker build --file .edullm/Dockerfile` with the tag from
`$EDULLM_IMAGE_TAG`.

**Consequences, and they matter beyond tonight:**

1. **"Images build only from `edullm/**`" is not a CI rule — it is a convention with no enforcement
   mechanism in this account.** Nothing watches the remote. A push to `edullm/final-dataset-phase0`
   builds **nothing, silently** — the same failure shape as the ledger's convention-4 warning about
   merging to `main`, but broader than stated: it applies to *every* branch, because there is no
   trigger anywhere.
2. **Building `5450f53` means re-encoding the whole source tree into 83 env vars and
   `start-build --environment-variables-override`, plus updating the hardcoded tarball sha256.** That
   is not "run a build" — it is authoring a new build invocation, and the sha256 guard means a
   mismatched tree **fails closed** (good) rather than building something unintended.
3. The project is named `…-prm800k-…` — it is the **vendored/PRM line's** builder, reused. This is
   the "two parallel lines each shipped an image" hazard in `MEMORY.md`, and it is why the CEO's
   `available()` warning is well-founded: **one CodeBuild project serves both lines.**

**Good news bounding the risk:** `5450f53` **does** carry the Phase-0 work — `.edullm/Dockerfile` is
present on that commit, `__version__` is **`0.9.1`**, and B7's `ChecksumSHA256` sink is in
`s3.py` (`:398`, plus the composite-digest guard at `:381`). So the tree is right; only the *build*
is missing.

✅ **B3 HAS LANDED on that commit. `MEASURED`.**

> **⚠️ CORRECTING MYSELF IN PLACE — I first wrote that B3 was missing. That was MY BAD GREP, not
> missing code, and I am striking it.** I searched `profiles/` for `head_workers` and found nothing,
> and concluded B3 had not landed. Wrong pattern: B3 threads the profile checks, and the profile
> reads its worker count through a helper, so the *flag name* never appears there. Searching for the
> mechanism instead of the flag finds it immediately:
>
> ```
> 5450f53:src/edullm_data/profiles/pretrain_tokens_v1.py:326:  from concurrent.futures import ThreadPoolExecutor
> 5450f53:src/edullm_data/profiles/pretrain_tokens_v1.py:340:  with ThreadPoolExecutor(max_workers=_workers(ctx)) as pool:
> ```
>
> `pretrain_tokens_v1.py` had **zero** threading when §8.2 was written; it now has a pool sized from
> context. **That is task #10 / B3, present on `5450f53`.** The CEO's "post-B3 it is 3.4–4.4 h"
> premise **holds**, and my momentary alarm was unfounded.
>
> **The lesson is the one this session keeps re-learning, now from the other direction:** I inferred
> *absence of behaviour* from *absence of a string*. A grep that misses is not evidence a feature is
> missing — exactly as a docstring saying "~2 round trips" was not evidence the code did 2. **Search
> for the mechanism, not the label**, and never report a negative from a single pattern. I nearly
> sent the CEO a false blocker on a premise of theirs that was correct.

## What I need from the CEO — three named mutations, none of them mine to choose

Per the standing rule, naming every mutation the authorization transitively requires:

1. **Build the image** — a `codebuild start-build` with 83 regenerated source env vars, an updated
   tarball sha256, and an `EDULLM_IMAGE_TAG`. **This is an ECR/CodeBuild mutation the owner's grant
   does not name** ("push code", "register job-def revisions", "submit Batch build jobs" — building
   an image is none of the three). **I am not doing it without an explicit grant.**
2. ~~Confirm B3's status~~ — **DONE, B3 is present on `5450f53`** (see the correction above). No
   longer a precondition. The tree is correct; only the build is missing.
3. **Only then** register the revisions. I have the commands drafted and will run them the moment
   there is a digest to pin. **Blocker 1 is the sole remaining precondition.**

**Drafted and ready, not run** (validator split — Gate A and promote as separate defs, digest-pinned,
no wheel):

```
# A) Gate A only, no --promote, 8 h
aws batch register-job-definition --job-definition-name edullm-validator \
  --type container --platform-capabilities EC2 \
  --job-role-arn arn:aws:iam::<ACCOUNT_ID>:role/sbsandbox-intern-edullm-dataset-validator \
  --timeout attemptDurationSeconds=28800 \
  --container-properties '{"image":"<ECR>@sha256:<NEW_DIGEST>","vcpus":4,"memory":8192,
     "command":["python","-m","edullm_data.validate","--head-workers","16"]}'

# B) promote only, separate def, 8 h  <- blast radius, per the CEO's ruling
aws batch register-job-definition --job-definition-name edullm-promote \
  ... "command":[...,"--promote","--promote-workers","16"]
```

**`edullm-reservoir-build`'s 64,800 s (18 h) is adequate at 48×8** and needs no change: the per-child
figure that matters is the **largest single bundle**, and even DCLM's un-split 49.0 h case is a
splitting problem, not a timeout problem — at the 9.96 h floor with 12 concurrent instances, no child
approaches 18 h. **If the corrected rate lands at 33.2 h aggregate, that is still ~2.8 h/child across
12 children, well inside 18 h.** I would leave it alone. `DERIVED`.

---

## ✅ REMEDIATION COMPLETE — steps 1, 2 and 3 all done and verified. `MEASURED`

**Superseded: the "STOPPED AT STEP 2" heading below was accurate when written. The CEO ruled on both
questions, and steps 2 and 3 are now executed.** Full sequence, in order:

| step | action | result |
|---|---|---|
| 1 | `put-bucket-versioning … Status=Enabled` | ✅ `{"Status": "Enabled"}` |
| 2 | `put-bucket-policy … file:///tmp/mirror-policy-deploy.json` | ✅ exit 0, read back and confirmed |
| 3 | live `PutObject` + `DeleteObject` probes | ✅ **both AccessDenied, "explicit deny"** |

### Step 2 — applied, deployer-only, as amended

Rendered to `/tmp/mirror-policy-deploy.json` (real account id, `_README` stripped, mirror-writer ARN
dropped per the owner's ruling). **Three preconditions machine-checked before applying**, because a
wrong one here is a lockout on a bucket with no CFN stack:

```
template == deployed modulo <ACCOUNT_ID>:  True
resource ARNs:  ['arn:aws:s3:::edullm-data-us-east-2/*',
                 'arn:aws:s3:::edullm-data-us-east-2/*']     <- MIRROR, both statements
bucket-level ARN present (lockout risk)?   False             <- object-level only
exempt principals: [ …:role/sbsandbox-intern-edullm-infra-deployer ]
```

Read back live with `get-bucket-policy` (account id scrubbed):

```json
{"Version":"2012-10-17","Id":"edullm-data-us-east-2-airlock-v2","Statement":[
 {"Sid":"OnlyMirrorWriterWrites","Effect":"Deny","Principal":"*",
  "Action":["s3:PutObject","s3:PutObjectTagging","s3:AbortMultipartUpload"],
  "Resource":"arn:aws:s3:::edullm-data-us-east-2/*",
  "Condition":{"ArnNotEqualsIfExists":{"aws:PrincipalArn":
     "arn:aws:iam::<ACCOUNT_ID>:role/sbsandbox-intern-edullm-infra-deployer"},
   "BoolIfExists":{"aws:PrincipalIsAWSService":"false"}}},
 {"Sid":"NobodyDeletesPublishedData","Effect":"Deny","Principal":"*",
  "Action":["s3:DeleteObject","s3:DeleteObjectVersion"],
  "Resource":"arn:aws:s3:::edullm-data-us-east-2/*",
  "Condition":{"BoolIfExists":{"aws:PrincipalIsAWSService":"false"}}}]}
```

> **Note on the read-back:** S3 normalized the single-element `aws:PrincipalArn` **list** into a bare
> **string**. Semantically identical, but it means a naive `diff` against the template will show one
> line differing. **I amended the template's own `_README` verify recipe to expect this** rather than
> leave a documented check that fails on a correct deployment — a check that cries wolf gets ignored,
> which is the failure mode the golden rule exists to prevent.

### Step 3 — the live re-verify. **BOTH PROBES DENIED.** `MEASURED`, verbatim

`PutObject`, exit **254**:
```
An error occurred (AccessDenied) when calling the PutObject operation: User:
arn:aws:sts::<ACCOUNT_ID>:assumed-role/Intern-eric.wu-sbsandbox/broker-eric.wu-1786179182
is not authorized to perform: s3:PutObject on resource:
"arn:aws:s3:::edullm-data-us-east-2/_airlock-probe/plat-exec-20260808-mirror-negative-test.txt"
with an explicit deny in a resource-based policy
```

`DeleteObject`, **nonexistent key** so the probe could destroy nothing, exit **254**:
```
An error occurred (AccessDenied) when calling the DeleteObject operation: User:
arn:aws:sts::<ACCOUNT_ID>:assumed-role/Intern-eric.wu-sbsandbox/broker-eric.wu-1786179189
is not authorized to perform: s3:DeleteObject on resource:
"arn:aws:s3:::edullm-data-us-east-2/_airlock-probe/nonexistent-key-plat-exec-20260808-delete-probe.txt"
with an explicit deny in a resource-based policy
```

**The delete probe is the decisive one.** The CEO measured this *exact* call returning **exit 0,
SUCCESS** before remediation. It now returns an **explicit deny**. Both messages say *"with an
explicit deny in a resource-based policy"* — a Deny doing the work, not a missing Allow — and the
denied principal is the intern/broker session **every agent session in this workspace uses**.

**The mirror now matches the primary's posture**, with one deliberate difference: it is
**write-closed** — no mirror-writer principal exists, so §8B.3's sync cannot run until someone
explicitly decides who writes and amends the policy. That is the owner's ruling, not an oversight.

**Residue: none.** Both probes were negative tests; no object was created and none deleted.

**Remaining gap, unchanged and worth restating:** the **18,455 pre-existing objects / 2.67 TB** are
protected from *future* deletes by the policy, but versioning was enabled *after* they were written,
so they have **no noncurrent versions to recover to**. The policy is now the only thing standing
behind them. That is a large improvement over "nothing," and it is not the same as the primary's
belt-and-braces posture.

---

## ⛔ Step 2 — NOT APPLIED *(superseded above — kept for the record)*

**I re-read `infra/09-mirror-bucket-policy.json` at apply time, as instructed. The ARNs are correct
for the mirror bucket — but the file contains SCRUB PLACEHOLDERS, not ARNs.** Lines 18–19:

```
"arn:aws:iam::<ACCOUNT_ID>:role/<INFRA_DEPLOYER_ROLE>",
"arn:aws:iam::<ACCOUNT_ID>:role/<MIRROR_WRITER_ROLE>"
```

**`<ACCOUNT_ID>`, `<INFRA_DEPLOYER_ROLE>` and `<MIRROR_WRITER_ROLE>` are placeholders I wrote to keep
the public repo clean.** `put-bucket-policy` would reject them as malformed ARNs (`MalformedPolicy`),
and if it did not, an `ArnNotEqualsIfExists` list that matches **no real principal** makes the Deny
apply to **everyone including the deployer** — the exact lockout my own ordering warning described,
on a bucket with no CFN stack to roll back.

**I checked the two resource ARNs specifically, per the CEO's instruction:** both
`arn:aws:s3:::edullm-data-us-east-2/*` (lines 14 and 35) correctly name the **mirror**, not the
primary. That part of the draft is right.

### The blocker, and why it collides with the step-4 ruling

`<INFRA_DEPLOYER_ROLE>` resolves cleanly — `MEASURED`, `iam get-role`:
`sbsandbox-intern-edullm-infra-deployer` exists.

**`<MIRROR_WRITER_ROLE>` does not exist, and step 4 forbids creating it.** The CEO anticipated exactly
this: *"If step 2's policy as drafted requires a writer principal to be syntactically valid, tell me
rather than substituting one."* **It does. I am telling you rather than substituting one.**

### The resolution I recommend — and it is BETTER than the draft, not a workaround

The owner's intent is a **write-closed mirror**: nothing writes to it during the build. That intent is
expressed *more faithfully* by *removing* the second ARN than by inventing a role for it:

```json
"ArnNotEqualsIfExists": {
  "aws:PrincipalArn": [
    "arn:aws:iam::<ACCOUNT_ID>:role/sbsandbox-intern-edullm-infra-deployer"
  ]
}
```

**One principal — the deployer — retained solely so the policy itself remains editable.** Everything
else, including every agent session, is denied `PutObject`. This is strictly *tighter* than the
draft, requires **no new IAM principal**, and honours step 4 exactly. When a mirror writer is later
decided, adding it is a one-line policy update.

**Two things I want ruled on before I apply anything, because both are judgement calls, not
mechanics:**

1. **Confirm dropping `<MIRROR_WRITER_ROLE>` entirely** (rather than substituting a role) is what you
   want. It is the write-closed posture, but it means **the mirror cannot receive the §8B.3 sync
   until the policy is amended** — by design, and worth stating out loud.
2. **The real account ID must appear in the applied policy** but must **never** be committed. My plan:
   apply from a `/tmp` file with the real ID, and leave `infra/09-mirror-bucket-policy.json` in the
   repo with `<ACCOUNT_ID>` placeholders plus a comment naming the substitution. That keeps the public
   repo clean and the deployed policy correct — but it does mean **the committed file is a template,
   not a literal artifact**, which is precisely the "prose is not behaviour" trap this session has hit
   four times. **I would rather you know that than discover it.**

**Steps 2 and 3 are both pending this ruling.** Step 3 (the live Put/Delete re-verify) is meaningless
until a policy is actually in place, so I did not run it — reporting a passing probe against *no
policy* would be the worst possible outcome. Nothing else was touched.

---

# ADDENDUM 3 — Option A result, and the mirror is ALREADY POPULATED

## 🔴🔴 THE MIRROR IS NOT EMPTY. 18,455 objects / 2.67 TB, unprotected, right now.

**This is the most urgent finding in any of my reports.** The CEO's brief says the mirror *"is empty
today, which is the only reason it isn't already a breach."* **That premise is false.** `MEASURED`,
`s3api list-objects-v2 --bucket edullm-data-us-east-2`, 2026-08-08:

```
count:  18,455 objects
bytes:  2,669,211,517,265   = 2.67 TB
```

First keys returned are published catalog entries — `_catalog/pretrain/fineweb-edu-1b/v2.json`,
`_catalog/pretrain/fineweb-edu-750m/v2.json`, `_catalog/curriculum/regmix-370m/v1.json`,
`_catalog/pretrain/fineweb2-equal-bytes/v1.json`. **Somebody has already mirrored published datasets
into a bucket with no bucket policy at all.**

**So this is not a precondition to satisfy before the mirror receives data. It is a LIVE EXPOSURE of
2.67 TB of already-published corpus.** The severity ordering in the CEO's ruling should be raised
accordingly: it is not "do this before Phase 4," it is "do this now, independently of Phase 4."

### The full gap, measured side by side

| control | `edullm-data` (primary) | `edullm-data-us-east-2` (mirror) |
|---|---|---|
| bucket policy | `edullm-data-airlock-v2`, 3 statements | **NONE** — `NoSuchBucketPolicy`, exit 254 |
| `OnlyValidatorWrites` Deny | ✅ | ❌ **absent — any principal with IAM write can PutObject** |
| `NobodyDeletesPublishedData` Deny | ✅ | ❌ **absent — published objects are DELETABLE** |
| **versioning** | **`Status: Enabled`** | 🔴 **NOT ENABLED** (empty response) |
| public access block | on | ✅ on (all four true) — the one control that is present |
| CFN-managed | yes (`…-phase3-batch` et al.) | ❌ **no** — `describe-stack-resources` → *"Stack for edullm-data-us-east-2 does not exist"*; no tag set either |

**Versioning is a second, independent gap the brief did not mention, and it compounds the first.** On
the primary, `NobodyDeletesPublishedData` is backed by versioning, so even a hypothetical delete
leaves noncurrent bytes recoverable. On the mirror there is **neither the Deny nor versioning** — a
`DeleteObject` there is **immediate and unrecoverable**. "Frozen means frozen" does not hold on the
mirror in any sense.

### (a) The exact policy — `edullm-data-us-east-2-airlock-v2`

Adapted from the live primary policy (I fetched both the repo template `infra/02-bucket-policy.json`
and the **live** policy via `get-bucket-policy`; they match, so the template is current). Three
deliberate changes, each explained below:

```json
{
  "Version": "2012-10-17",
  "Id": "edullm-data-us-east-2-airlock-v2",
  "Statement": [
    {
      "Sid": "OnlyMirrorWriterWrites",
      "Effect": "Deny",
      "Principal": "*",
      "Action": ["s3:PutObject", "s3:PutObjectTagging", "s3:AbortMultipartUpload"],
      "Resource": "arn:aws:s3:::edullm-data-us-east-2/*",
      "Condition": {
        "ArnNotEqualsIfExists": {
          "aws:PrincipalArn": [
            "arn:aws:iam::<ACCOUNT_ID>:role/<INFRA_DEPLOYER_ROLE>",
            "arn:aws:iam::<ACCOUNT_ID>:role/<MIRROR_WRITER_ROLE>"
          ]
        },
        "BoolIfExists": { "aws:PrincipalIsAWSService": "false" }
      }
    },
    {
      "Sid": "NobodyDeletesPublishedData",
      "Effect": "Deny",
      "Principal": "*",
      "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
      "Resource": "arn:aws:s3:::edullm-data-us-east-2/*",
      "Condition": { "BoolIfExists": { "aws:PrincipalIsAWSService": "false" } }
    }
  ]
}
```

**Three deliberate differences from the primary, none of them cosmetic:**

1. **`OnlyValidatorWrites` → `OnlyMirrorWriterWrites`.** The validator role does **not** run in
   us-east-2 and must not be the mirror's writer. §8B.3's mechanism is `aws s3 sync` / S3 Batch
   Replication **after** promotion — a different principal doing a different job. Copying the
   primary's Sid and ARN verbatim would grant the us-east-1 validator write access to a bucket it
   never touches, which is a wider grant than the mirror needs. **`<MIRROR_WRITER_ROLE>` must be
   decided by the CEO/owner** — it is whatever principal runs the sync. If S3 Replication is used
   instead, it is `s3.amazonaws.com` as a service principal and the `BoolIfExists` guard already
   exempts it.
2. **`AllowS3InventoryDelivery` is dropped.** The primary needs it because it has an S3 Inventory
   configuration writing to `_inventory/`. The mirror has none. **Do not copy a grant for a feature
   that is not configured** — an unused Allow is a standing hole.
3. **Resource ARNs are the mirror's**, obviously — but note this is exactly the failure mode to check
   for: a policy pasted with the *source* bucket's ARN applies to nothing and **reports success**.
   Re-verification below is what catches that.

### (b) What applying it requires — the FULL mutation set, per standing rule 1

I am naming every mutation transitively required, including the ones that are not the policy itself:

| # | mutation | command | why |
|---|---|---|---|
| 1 | **Enable versioning** | `aws s3api put-bucket-versioning --bucket edullm-data-us-east-2 --versioning-configuration Status=Enabled` | must come **first** — the delete-Deny is only durable with versioning behind it, and versioning cannot retroactively protect objects written before it is on |
| 2 | **Put the policy** | `aws s3api put-bucket-policy --bucket edullm-data-us-east-2 --policy file://infra/09-mirror-bucket-policy.json` | the airlock itself |
| 3 | **Re-verify the Deny live** | intern `PutObject` → expect `AccessDenied` "explicit deny"; intern `DeleteObject` → same | **mandatory** — CLAUDE.md: after anything touching permissions, re-verify the Deny fires. `simulate-principal-policy` **lies** for the intern role (11 false denials), so this must be a live smoke test |
| 4 | *(conditional)* create `<MIRROR_WRITER_ROLE>` | `iam create-role` + policy | only if no suitable principal exists; **needs its own authorization** |

⚠️ **Ordering matters and is not interchangeable.** Do 1 before 2. If the policy lands first and the
mirror-writer role is wrong, you have locked yourself out of a bucket you cannot delete from either
— and with no CFN stack to roll back, recovery is a manual `delete-bucket-policy` by the deployer.

⚠️ **`BlockPublicPolicy: true` is already on.** The draft policy is not public (it has no
wildcard-principal Allow), so it will apply — but if anyone later adds an Allow with
`"Principal": "*"`, S3 will **reject the whole policy**, not just that statement.

**Not CFN-managed, so the mechanism is the CLI, not a stack update** — confirmed two ways:
`describe-stack-resources` errors with *"Stack for edullm-data-us-east-2 does not exist"*, and
`get-bucket-tagging` returns `NoSuchTagSet` (every CFN-managed bucket here carries stack tags). **This
also means it was created out-of-band**, by hand, which is consistent with a policy never being
attached. I recommend the policy JSON be committed to `infra/` so the mirror stops being
undocumented infrastructure.

**I have applied none of this.** Per rule 1, the set above is the ask.

---

## ✅ Option A — the ratio, measured locally, no AWS, no authorization

Run on this machine against a structurally identical index (400k 13-gram hashes + 50k exact), 400
synthetic documents at **3,919 mean bytes / 560 words** — close to the plan's realized ~3.5 KB.
Ratios are what transfer; absolute rates are laptop-specific.

| phase | time (400 docs) | rate | windows/s |
|---|---|---|---|
| **decon `contains()`** | **177.6 ms** | 2,311 doc/s | **1,266,551** |
| **dedup `add_if_new` + `content_hash`** | **2.1 ms** | 160,799 doc/s | 88,117,663 |

**Two findings, and they point the same way:**

1. **DATA's 1,174,020 windows/s/core REPRODUCES.** I measured **1,266,551 windows/s** on unrelated
   hardware — within **7.9%**. `MEASURED`. DATA's number is sound and is not a measurement error.
2. **🔴 Dedup is NOT the serial cost. It is 1.15% of the two, and decon is 86× larger.** This
   **independently confirms the retirement of the near-OOM hypothesis** by a second route: even
   ignoring memory entirely, `SeenHashes` is computationally negligible. Whatever the 78% is, it is
   **not** the dedup set — not by memory pressure, and not by CPU.

**So the 16.4× gap survives Option A and is still unexplained.** What I can now say is where it is
*not*: not the tokenizer (rust, releases the GIL), not dedup (1.15%), and `contains()`'s own rate is
confirmed fast. **The remaining candidates are the reader/parquet path and per-document Python
overhead outside all three measured phases** — i.e. the 78% may be an artifact of what the original
measurement *attributed* to the filter rather than of the filter itself. That is a scope/denominator
question of exactly the kind this project has hit three times, and it belongs to whoever owns the
build loop. `contains()` early-returns on the second hit (`corpus_filter.py:193`), so on a *clean*
corpus it scans **every** window of every document — my benchmark's near-zero hit rate is therefore
the realistic worst case, not an optimistic one.

**Caveat, stated plainly:** I could not include `encode_batch` in the three-way split — the local
`tokenizer.json` failed to parse (`expected ',' or '}' at line 2 column 19`), and downloading the
real one is an S3 read I did not need for the ratio that mattered. **decon-vs-dedup is measured;
decon-vs-tokenize is not.** That gap does not affect either conclusion above.

## R3 — restated after the redirect, unchanged

I derived R3 **only** from code (`validate.py`, `s3.py`), the plan's measured anchors (M7a/M7c/M13),
and live API reads. **I did not see AUDIT's number and have not looked for it.** My answer stands:
**3–4 h, bandwidth-bound, marginal against rev 14's 4.0 h wall.** The two corroborations found since
(DEPLOY.md's independent M7c record, and the NIC arithmetic) both strengthen it.


