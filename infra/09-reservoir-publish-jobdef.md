# Step: the reservoir publish job definition

> **STATUS: NOT DEPLOYED (2026-08-04).** The role and both policies are authored
> (`09-reservoir-publish-policy.json`, `-trust-policy.json`) and covered by
> `tests/test_reservoir_publish_policy.py`, but **no role and no job definition exist live yet.**
> Creating them requires IAM write, which a session does not have — see "Who runs this" below.

## Why this file exists

`artifacts/reservoir/PUBLISH-SPEC.md` says `publish()` "MUST run on Batch in-region". As of
2026-08-04 there was nothing to run it in: the only publish job definition in the account is
`edullm-prm800k-publish`, a different corpus with a different entry point
(`edullm-prm800k-ingest publish --run-id`) and 2 vCPU / 4 GiB / 7200 s — sized for a corpus about
1/50th of this one. So the step the spec calls mandatory had no venue, which is the kind of gap that
surfaces at the worst moment: after `verify --deep` finally passes and everyone expects one command.

## Who runs this

**A team lead with IAM write.** A session in this workspace reaches AWS through the read-only
`sb-aws` broker, and that is deliberate: the airlock is what keeps published data honest, and a
session that could mint roles could mint one that writes `s3://edullm-data`. Hand these three
commands to someone who can run them, or run them from an admin context.

```bash
# 1. the role
aws iam create-role --role-name edullm-reservoir-publish \
  --assume-role-policy-document file://infra/09-reservoir-publish-trust-policy.json \
  --description "Runs publish() for pretrain/reservoir-dolma2 on Batch. Cannot write edullm-data."

# 2. its one inline policy
aws iam put-role-policy --role-name edullm-reservoir-publish \
  --policy-name reservoir-publish \
  --policy-document file://infra/09-reservoir-publish-policy.json

# 3. the job definition (below)
aws batch register-job-definition --cli-input-json file://<the JSON from this file>
```

## The job definition

Model it on `edullm-reservoir-build-force:1` (itself cloned from `edullm-reservoir-build:9`), which
is known-good for this corpus, **not** on `edullm-prm800k-publish:2`.

| field | value | why this value and not the obvious one |
|---|---|---|
| `image` | the digest that built the corpus, or a strictly newer one whose branch **contains** `7a97c27` | Two different commits both call themselves `0.7.4` (see PUBLISH-SPEC.md "the two image lines"). A version assertion cannot identify the code; check ancestry with `git merge-base --is-ancestor`. |
| `vcpus` / `memory` | 16 / 32768 | `publish()` stream-hashes every object with `hash_workers` threads. The build's 8/14336 was sized for a tokenizer resident in RAM, a different shape. |
| `jobRoleArn` | `edullm-reservoir-publish` | **Not** `edullm-reservoir-ingest`: that role explicitly denies `PutObject` on `*manifest.json` and `*dataset.json`, which are exactly what `publish()` writes. |
| `executionRoleArn` | `sbsandbox-intern-edullm-batch-execution` | **Required.** Omitting it produces a container that never starts with no readable logs, and the symptom looks like a missing log group — this cost a full diagnosis cycle on the build job def. |
| `logConfiguration` | awslogs → `/aws/batch/sbsandbox-intern-edullm-cpu`, prefix `reservoir-publish` | Same group as the build, so one query covers the whole corpus lifecycle. |
| `timeout` | `attemptDurationSeconds: 21600` (6 h) | Single-threaded publish timed out on a 218-shard / 125 GB corpus at 3600 s. This is 10,049 shards / ~1 TB. 7200 s is the prm800k value and is not a precedent for this size. |
| `retryStrategy` | `attempts: 1` | **Deliberately no retry.** `publish()` reserves the version with a *create-only* `dataset.json`; a second attempt finds `v1` taken and either fails confusingly or lands on `v2`. Diagnose and re-run by hand. |

`hash_workers=16` / `copy_workers=16` go in the `publish()` call itself (PUBLISH-SPEC.md), not the
job definition.

## Before submitting

1. `corpus_build verify --plan-id d5c9bcd38735e1f0 --deep` must exit 0. Until then this job would
   publish a corpus whose payload was never re-hashed.
2. **`edullm-landing-manifest-created` is DISABLED.** Nothing auto-promotes, so publishing is safe
   from accidental promotion — but it also means the validator must be submitted by hand afterwards
   (by unversioned name, per `05-validator-jobdef.md`), or the rule re-enabled first. Decide before,
   not after.
3. Verify the airlock still holds after any IAM change: an intern `PutObject` to `edullm-data` must
   return `AccessDenied` as an *explicit* deny. `iam:simulate-principal-policy` returns false
   denials for this account (11 known), so smoke-test it live rather than simulating.
