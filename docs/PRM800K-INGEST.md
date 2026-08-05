# PRM800K raw vendor ingest

This runbook publishes a byte-preserving mirror at `vendor/openai-prm800k/v1`.
It is a provenance artifact, not a train-ready SFT or process-reward dataset. The
normal reader requires `allow_vendored=True`; any normalized reward-model or SFT
representation must be a separate derived dataset.

## Pinned source identity

Hugging Face is transport only:

- transport repo/revision: `tasksource/PRM800K@547b19506677a59037ee888838834b65e9b1ddd4`
- canonical upstream: `openai/prm800k@00811d6de065642a6967b9017d4cee59550c0ef4`
- canonical license basis: `MIT`, declared by the canonical GitHub repository

The command stream-hashes every fetched file and rejects a difference before it
writes a staging receipt.

| File | Bytes | SHA-256 |
|---|---:|---|
| `phase1_test.jsonl` | 829,105 | `f4b3bc5b095e45c816453dc4d748b755c680d61d55f9895d929a335b487c727d` |
| `phase1_train.jsonl` | 7,900,236 | `e9da6a73f827ffb9a8c0dc644c541d34ed76b3d4d1e4896ff5f7b37ddf5ae34d` |
| `phase2_test.jsonl` | 12,240,719 | `6b172efa884ac8341a946dd82e06947c135b7254109fb3f7aa907c715d98aaad` |
| `phase2_train.jsonl` | 456,135,365 | `1110237feeb51d1bc200cb37b8f965cfdc1036eac7d506094049366fe7dc1089` |

Total: 477,105,425 bytes.

## Shared pipeline

```text
PRM800K producer → edullm-landing → shared validator → edullm-data
```

PRM800K is one input to the normal data path. It does not get a separate
validator, validation role, or direct write path to `edullm-data`.

- `edullm-prm800k-producer` runs only `stage` and `publish`. It can use the
  PRM800K staging and final landing prefixes, but cannot write `edullm-data` or
  validation markers. It can list only three exact PRM800K control-key prefixes
  in `edullm-data` to distinguish a missing object from an access denial; that
  metadata permission grants no `edullm-data` write path.
- `sbsandbox-intern-edullm-batch-workload` is the existing shared validator
  role. It is trusted only by ECS tasks and remains the only normal role that
  can promote a validated dataset into `edullm-data`.
- `sbsandbox-intern-edullm-batch-execution` only pulls the validator image and
  writes task logs. Its ECR access includes the immutable platform image below.

The producer policy and ECS-only trust template are:

- [`infra/06-prm800k-ingest-policy.json`](../infra/06-prm800k-ingest-policy.json)
- [`infra/06-prm800k-ingest-trust-policy.json`](../infra/06-prm800k-ingest-trust-policy.json)

The producer is explicitly denied `_VALIDATED.json` and `_REJECTED.json`, so
only the shared validator can create terminal validation evidence. It cannot
write `edullm-data`.

## Immutable publisher image

The reviewed recovery workspace was rebuilt by CodeBuild and published to the
existing ECR repository as this immutable reference:

```
<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/sbsandbox-intern-edullm-data@sha256:63a9d45f31d3f7ae266e8164471e297fd6ecaba286be1a551c973ab1c49f3ec2
```

The corresponding tag,
`prm800k-recovery-f86954d644a1-r2`, is audit metadata only. Batch definitions
use the `@sha256:` reference, never the tag.

`edullm-prm800k-publish:2` records its revisioned Batch job-definition ARN,
image repository, and image digest in `dataset.json`. This is provenance
evidence; the IAM role remains the authorization boundary.

## Current deployment

The following definitions are registered in `sbsandbox` / `us-east-1`:

| Job definition | Role | Purpose |
|---|---|---|
| `edullm-prm800k-stage:1` | `edullm-prm800k-producer` | stream and verify the four source files into non-triggering landing staging |
| `edullm-prm800k-publish:2` | `edullm-prm800k-producer` | verify the receipt and publish the final landing artifact using the recovery image above |
| `edullm-validator:9` | existing shared validator role | generically discover, validate, and promote eligible landing datasets |

`edullm-validator:9` is unchanged and remains pinned to
`<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/sbsandbox-intern-edullm-data@sha256:339c2b6b046ea738a8cdb258fd181e1eb0e755cb923e8335a2831f70cdd448d6`,
with the existing shared roles, 4 vCPU, 8192 MiB, one 7200-second attempt, and
the generic command:

```text
python -m edullm_data.validate \
  --landing-bucket edullm-landing \
  --data-bucket edullm-data \
  --promote \
  --promote-workers 16
```

The generic `edullm-landing-manifest-created` EventBridge rule remains
**disabled**. When separately approved and enabled, it targets the unversioned
`edullm-validator` family, which uses revision `:9`; it is not a PRM-only
trigger.

An earlier PRM-only validator definition and role are unused legacy setup. They
are not part of this flow and require separate destructive-cleanup approval to
retire.

## Recovery-run status

The publish-only recovery for staging run
`prm800k-29cef669-04bf-4688-bddf-d2efd8fd8260` succeeded through
`edullm-prm800k-publish:2`. The final landing prefix now contains the four
source-pinned JSONL payloads, `dataset.json`, and `raw/manifest.json`.

The unchanged generic `edullm-validator:9` job was then submitted without
command, environment, or role overrides and completed successfully. It wrote
the ordinary `README.md`, `_VALIDATED.json`, and catalog entry under
`edullm-data`; the four raw payloads retain their source-pinned byte counts.
The generic EventBridge rule remains disabled.

## Batch sequence

Both producer subcommands refuse to run unless `AWS_BATCH_JOB_ID` is present.
They are intended only for the approved in-region Batch jobs.

```bash
edullm-prm800k-ingest stage \
  --run-id prm800k-<unique-run-id>

edullm-prm800k-ingest publish \
  --run-id prm800k-<same-unique-run-id>
```

`stage` writes only:

```text
s3://edullm-landing/_staging/vendor/openai-prm800k/<run-id>/
  payload/raw/<the four original JSONL filenames>
  receipt.json
```

It never creates `dataset.json` or `manifest.json` in staging. `receipt.json`
is outside `payload/`, records the observed S3 checksum for each upload, and is
verified against the immutable table above on retry and during `publish`.

`publish` revalidates the receipt, creates the final `v1` landing artifact, and
writes `raw/manifest.json` last. It streams staged objects for the manifest,
server-side-copies them, then rechecks final copies before publishing the
manifest. A retry resumes only the same matching `v1` reservation; it never
auto-allocates `v2`.

When the shared validator discovers `vendor/openai-prm800k/v1`, it runs the
ordinary generic checks plus the PRM800K-specific code-pinned OpenAI Git-LFS
table. It does not trust a mutable landing `upstream_files` list as source of
truth. It stream-hashes payloads while recording stable ETags, uses
`CopySourceIfMatch` during promotion, and stream-hashes final objects before
sealing. A mismatch leaves the data prefix unsealed and writes terminal evidence
in landing; it never silently overwrites a sealed dataset.

Do not call `promote()` manually and never write `edullm-data` directly. Only
the shared validator promotes a passing artifact via server-side copy.

## Before a live run

Before staging data, confirm that raw human text, opaque labeler IDs, and
timestamps have privacy/legal approval. Then perform a read-only preflight that
establishes:

- the producer definitions and shared `edullm-validator:9` match this source
  package and immutable image;
- the shared validator recognizes `vendored/v1` and does not bootstrap a
  mutable landing wheel;
- there are no unrelated complete, unmarked landing datasets before enabling
  the generic trigger;
- `vendor/openai-prm800k/v1` has no catalog entry, final data prefix, landing
  terminal marker, or incompatible reservation.

Only after separate approval covering the 477,105,425-byte Hugging Face-to-
landing transfer, landing writes, and validation/promotion should the producer
jobs run and the generic trigger be enabled (or an approved generic validation
job be submitted). Monitor until either the catalog plus
`edullm-data/.../_VALIDATED.json` appears or a landing `_REJECTED.json` explains
the terminal failure.
