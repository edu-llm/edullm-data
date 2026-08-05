# The general-purpose dataset publisher: `edullm-dataset-publish`

> **STATUS: AUTHORED, NOT DEPLOYED (2026-08-05).** Policies and tests are in the repo
> (`10-dataset-publish-policy.json`, `-trust-policy.json`,
> `tests/test_dataset_publish_policy.py` — 12 tests, 6 mutations verified to fail them).
> **Creating the role requires `iam:CreateRole`, which the `sb-aws` broker does not grant to any
> session — including a lead's.** Probed live 2026-08-05:
>
> ```
> AccessDenied: User: arn:aws:sts::<ACCOUNT>:assumed-role/Intern-<user>-sbsandbox/broker-<user>-...
>   is not authorized to perform: iam:CreateRole
> ```
>
> The broker mints an **Intern-** session regardless of the human's role in the org, so "I am a lead"
> does not change the credential. Run the three commands below from an admin context (SSO console,
> or a profile that is not the broker).

**This supersedes `09-reservoir-publish-*.json`**, which was scoped to one corpus. Those files are
kept for their reasoning but the role they describe should not be created; use this one.

## Why one role instead of one per corpus

`edullm-prm800k-producer` hardcodes `vendor/openai-prm800k/*` in five of its six statements. So
corpus number two needed role number two, and number three would need a third — each a fresh chance
to fat-finger a `Deny` that nobody reviews. One reviewed identity that every publish job assumes is
less surface, not more.

**What bounds it, since a dataset name no longer does:** the §2 family enum.
`contracts.validate_dataset_id` rejects any `dataset_id` whose first segment is not one of the seven
families — verified in the test suite against the real function, not asserted in a comment. So the
write grant is seven family prefixes, and the pipeline's own control prefixes (`_ingest/`, `_dist/`,
`_staging/`, `_catalog/`) are **additionally and explicitly denied**. `_dist/` matters most: it holds
bootstrap wheels and has **no lifecycle expiry**, so anything written there persists indefinitely and
gets executed by a later job.

**Unchanged:** the airlock. Every write is to `edullm-landing`. `edullm-data` is writable only by the
validator role, and this policy re-denies it so that attaching a broad managed policy later cannot
quietly open it. Forging `_VALIDATED.json` / `_REJECTED.json` is denied bucket-wide.

**Why it may write `manifest.json` and `dataset.json`** — the two names the *build* roles are
explicitly denied — is that publishing is the step that is supposed to write them. A builder that
could would fire the validator at a half-built prefix. That asymmetry is the reason this is a
separate identity rather than a widening of `edullm-reservoir-ingest`.

## Create it (admin context required)

```bash
aws iam create-role --role-name edullm-dataset-publish \
  --assume-role-policy-document file://infra/10-dataset-publish-trust-policy.json \
  --description "General-purpose eduLLM dataset publisher. Writes edullm-landing only; cannot write edullm-data."

aws iam put-role-policy --role-name edullm-dataset-publish \
  --policy-name dataset-publish \
  --policy-document file://infra/10-dataset-publish-policy.json

# then verify the airlock still fires, live — the simulator lies for this account (11 known false
# denials), so probe it rather than simulating:
#   as an intern session: aws s3api put-object --bucket edullm-data --key pretrain/_probe/x --body /dev/null
#   expect: AccessDenied (explicit deny)
```

## The job definition

| field | value | why not the obvious value |
|---|---|---|
| `image` | the digest that built the corpus, or a newer one whose branch **contains** it | Two commits have both declared `0.7.4`; `assert __version__` cannot tell them apart. Use the ECR tag (a commit sha) + `git merge-base --is-ancestor`. |
| `jobRoleArn` | `edullm-dataset-publish` | Not a build role — those Deny `*manifest.json` / `*dataset.json`. |
| `executionRoleArn` | `sbsandbox-intern-edullm-batch-execution` | **Required.** Omitting it yields a container that never starts and no readable logs; the symptom mimics a missing log group. Cost a full diagnosis cycle on the build job def. |
| `vcpus` / `memory` | 16 / 32768 | `publish()` threads its hashing (`hash_workers`), so the cores are used. As of `0.7.5` `verify --deep` does too, via `--hash-workers`; before that it was single-threaded and left 15 of 16 idle. |
| `timeout` | ≥ 21600 s (6 h) | A 218-shard / 125 GB corpus timed out at 3600 s single-threaded. Measured single-stream in-region throughput is ~88 MB/s (from the `verify --deep` run), so ~1 TB is ~3.2 h at one worker; `hash_workers=16` cuts that, but leave headroom. |

**If `hash_workers` > 10, the client needs a matching `max_pool_connections`.** `Boto3S3.default()`
passes no `botocore.config.Config`, so the pool is botocore's default **10** — and botocore does not
pass `block=True` to urllib3, so exceeding it neither raises nor waits: urllib3 discards the surplus
connection and logs "Connection pool is full", and workers 11..N silently pay a fresh TLS handshake
per object. The speedup is capped with no error anywhere. `corpus_build._s3(max_pool_connections=…)`
handles this for verify; a publish driver calling `Boto3S3.default()` directly with
`hash_workers=16` is subject to the same ceiling.
| `retryStrategy` | `attempts: 1` | **No retry.** `publish()` reserves the version with a *create-only* `dataset.json`; attempt 2 finds `v1` taken and either fails confusingly or lands on `v2`. Diagnose by hand. |

`hash_workers` / `copy_workers` are arguments to `publish()`, not job-definition fields.
