# Airlock verification + promotion-rule live state

**Run 2026-08-08 by PLAT-EXEC, read-only session.** Account `sbsandbox`, region us-east-1.
Account ID scrubbed to `<ACCOUNT_ID>` below; it appeared verbatim in the raw API output.

---

## 1. The airlock Deny — ✅ **FIRES.** `MEASURED`, live, not simulated.

Per CLAUDE.md, `iam:simulate-principal-policy` **lies** for the intern role (11 known false
denials), so this was smoke-tested live: a real `PutObject` against the protected bucket,
zero-byte body, to a key under a `_airlock-probe/` prefix.

**Call:**
```
aws s3api put-object --bucket edullm-data \
  --key _airlock-probe/plat-exec-20260808-readonly-negative-test.txt --content-length 0
```

**Result — exit 254:**
```
An error occurred (AccessDenied) when calling the PutObject operation:
User: arn:aws:sts::<ACCOUNT_ID>:assumed-role/Intern-eric.wu-sbsandbox/broker-eric.wu-1786172073
is not authorized to perform: s3:PutObject
on resource: "arn:aws:s3:::edullm-data/_airlock-probe/plat-exec-...txt"
with an explicit deny in a resource-based policy
```

Three things this establishes, precisely:

1. **`AccessDenied`** — the write was refused. No object was created.
2. **"with an explicit deny"** — this is a *deny*, not an absence of allow. A missing-permission
   failure produces different wording ("no identity-based policy allows…"). The Deny is doing the
   work, which is the invariant.
3. **"in a resource-based policy"** — the Deny lives on the **bucket policy**, so it binds every
   principal reaching the bucket, not just this role. It cannot be escaped by an intern-side
   identity-policy change.

The denied principal is the broker's intern session
(`assumed-role/Intern-eric.wu-sbsandbox/broker-eric.wu-…`), which is the principal every agent
session in this workspace uses. **The airlock holds. No object was written; the probe is a
negative test and left no residue.**

> Scope note, so this is not over-read: this verifies the Deny **for the intern/broker principal**.
> It does not re-verify that `…-edullm-dataset-validator` (assumable only by `ecs-tasks`) *can*
> still write — that is the positive half, and it is unprovable from a read-only session by
> construction. The last positive evidence is the promoted datasets in `edullm-data` and the
> SUCCEEDED `edullm-validate-on-manifest` jobs listed in `inventory.md`, the most recent of which
> promoted successfully. `MEASURED` (negative half) + `DERIVED` (positive half, from job history).

---

## 2. 🔴 `edullm-landing-manifest-created` is **ENABLED**

`aws events describe-rule --name edullm-landing-manifest-created` → **`"State": "ENABLED"`**.
Corroborated by `aws events list-rules`. `MEASURED`.

**CLAUDE.md, `HANDOFF-FINAL-DATASET.md`, and my own tasking all state this rule was deliberately
DISABLED on 2026-08-01 and verified so via `events describe-rule`. That is no longer true.**
It was re-enabled sometime in the last week. `describe-rule` returns no `LastModified`, so the
who/when is `UNVERIFIED` — CloudTrail `PutRule`/`EnableRule` events would settle it, and I can
pull those on request.

### Full live wiring

```
EventPattern: {"detail-type":["Object Created"],
               "source":["aws.s3"],
               "detail":{"bucket":{"name":["edullm-landing"]},
                         "object":{"key":[{"suffix":"manifest.json"}]}}}
State:        ENABLED
Description:  "... -> submit the dataset validator (Gate A) on the ...-edullm-cpu queue."
```

Target (`list-targets-by-rule`):

| field | value |
|---|---|
| Id | `validator-batch-queue` |
| Queue | `sbsandbox-intern-edullm-cpu` |
| JobDefinition | **`edullm-validator`** — **unversioned**, so it resolves to top ACTIVE = **rev 14** |
| JobName | `edullm-validate-on-manifest` |
| RoleArn | `…:role/CloudWatchSendEventsToVdi` |
| **RetryPolicy** | **`MaximumRetryAttempts: 2`** |

### Why this is the most dangerous item in my report

- **Writing a `manifest.json` to `s3://edullm-landing` IS publishing.** There is no
  publish-without-promote mode (memory: *"Publishing to landing auto-promotes"*). Rev 14's command
  carries `--promote --promote-workers 16`. So the chain is:
  `PutObject …/manifest.json` → EventBridge → Batch → Gate A → **promote into `edullm-data`** →
  **frozen `vN`**. Recovery from a wrong publish is a `v2`, never an edit.
- **The pattern matches key *suffix* `manifest.json` with no prefix constraint.** *Any* path in
  landing triggers it — including a scratch, staging, or test prefix someone reasonably assumes is
  inert. A Phase-2/3 dry run that happens to emit a manifest will publish.
- **`MaximumRetryAttempts: 2` is on the EventBridge target**, i.e. *submission* retry — distinct
  from the job definition's `retryStrategy.attempts: 1` (*execution* retry). A transient submission
  failure is retried twice; the job itself is never retried.
- Landing has a **14-day expiry** and is a scratch inbox, which reinforces the wrong intuition that
  writing there is consequence-free. With this rule armed, it is not.

### Recommended action — **CEO/owner decision, I did not touch it**

Before Phase 4, either **disable the rule** (`events disable-rule`, one call, reversible) **or**
adopt the standing discipline that *every* write to landing is a live publish requiring the same
sign-off as a promotion. I recommend disabling it for the duration of the build and re-enabling
deliberately at release, because the plan involves staging manifests we do **not** want promoted.

Note the interaction with #11: because the target names the job def **unversioned**, re-enabling
later automatically picks up whatever the top ACTIVE revision is *then* — today rev 14 with
`--head-workers 16`, tomorrow whatever someone registers. That is the documented
"for better and worse" cutover behaviour.

---

## 3. `edullm-wu-fsck-nightly` — **ENABLED**, weekly `MEASURED`

`ScheduleExpression: cron(6 9 ? * MON *)` → **Mondays 09:06 UTC**. Name says "nightly"; the rule's
own description explains the misnomer (renaming requires delete+recreate, which drops the target
and its RoleArn, so only the expression was changed). Target: Batch job `edullm-fsck`.

**Next fire: Monday 2026-08-10 09:06 UTC** — roughly two days out. If wave-3 promotion lands
before then, Gate B sweeps the newly promoted data unattended. Almost certainly benign (fsck is a
read/decay sweep, and its docstring says it reads "never a payload byte"), but it is an
unsupervised job touching `edullm-data` inside the build window, so it belongs on the timeline.

---

## 4. Incidental: a leftover Phase-4 probe is still armed `MEASURED`

`edullm-phase4-event-shape-probe`, **ENABLED**, matching all `Batch Job State Change` events on
the **GPU** queue. Its own description: *"TEMPORARY. Phase 4 probe: capture one raw Batch job
state change to settle what the event carries. Delete after reading."*

Observation-only, so not a hazard — but it is undeleted scaffolding, and its existence suggests
Phase-4 event plumbing was explored more recently than the handoff documents. Someone should reap
it; I have not.
