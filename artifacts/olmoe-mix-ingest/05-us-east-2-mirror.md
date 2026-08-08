# us-east-2 mirror — what exists, what it costs, what we cannot do ourselves

Read-only investigation, 2026-08-08. Account `sbsandbox` = `<ACCOUNT>`, principal
`arn:aws:sts::<ACCOUNT>:assumed-role/Intern-eric.wu-sbsandbox/broker-eric.wu-1786203271`
(`sts get-caller-identity`). No resource was created, copied, written or deleted.

Evidence tags: **VERIFIED** = I ran the command and quote its output. **DOCUMENTED, UNPROBED** = a
repo/template file asserts it and I did not test it live. **UNKNOWN** = not established.

---

## 0. The headline: the mirror already exists and is already 97% full

The owner's plan — "promote in us-east-1, then mirror the sealed result" — is not a proposal. It has
already been executed for the existing corpus, out-of-band, by an earlier session.

```
$ aws s3api list-objects-v2 --bucket edullm-data           --query '{Objects: length(Contents), Bytes: sum(Contents[].Size)}'
{ "Objects": 18578, "Bytes": 2749283342650 }
$ aws s3api list-objects-v2 --bucket edullm-data-us-east-2 --query '{Objects: length(Contents), Bytes: sum(Contents[].Size)}'
{ "Objects": 18455, "Bytes": 2669211517265 }
```

**VERIFIED.** 2.67 TB / 18,455 objects are already in us-east-2. Both large corpora are present in
full and byte-identical (§3). The single-region invariant in the standard is, as a factual matter,
already broken — and the two new OLMoE releases are a *marginal* 200/400 GB addition to an existing
mirror, not the thing that breaks it.

This reframes the whole question. It is no longer "should we mirror?" but "the mirror exists;
what is its state, and what does adding two releases cost?"

---

## 1. What exists in us-east-2 today

Enumerated `s3api list-buckets` (44 buckets), then `get-bucket-location` on every eduLLM-related
one. Note `LocationConstraint: null` **means us-east-1** — that is the API's legacy encoding, not a
missing value.

| bucket | region | note |
|---|---|---|
| `edullm-data` | **us-east-1** (`null`) | the sealed primary |
| `edullm-landing` | **us-east-1** (`null`) | the airlock inbox |
| **`edullm-data-us-east-2`** | **us-east-2** | **the mirror — EXISTS, 2.67 TB** |
| **`edullm-block-outputs-us-east-2`** | **us-east-2** | capacity-block checkpoint/log sink |
| **`edullm-ericwu-scratch-<ACCOUNT>`** | **us-east-2** | scratch |
| **`cdk-hnb659fds-assets-…-us-east-2`** | **us-east-2** | CDK bootstrap, unrelated |
| `edullm-checkpoints` | us-east-1 (`null`) | |
| `edullm-datasets` | us-east-1 (`null`) | the legacy bucket |
| `edullm-scratch` | us-east-1 (`null`) | |
| `edullm-memorysplit` | us-east-1 (`null`) | |
| `edullm-adaptive-inference-…` | us-east-1 (`null`) | |
| `sbsandbox-intern-edullm-artifacts` | us-east-1 (`null`) | |
| `sbsandbox-intern-edullm-lineage` | us-east-1 (`null`) | |
| `sbsandbox-intern-edullm-outputs` | us-east-1 (`null`) | |

So: **yes**, there are four us-east-2 buckets, and one of them is a fully-formed corpus mirror named
exactly as you guessed (`edullm-data-us-east-2`).

### 1a. The mirror's configuration is already hardened — and this is recent

```
$ get-bucket-versioning  --bucket edullm-data-us-east-2  → { "Status": "Enabled" }
$ get-bucket-encryption  --bucket edullm-data-us-east-2  → AES256, BlockedEncryptionTypes: [SSE-C]
$ get-public-access-block --bucket edullm-data-us-east-2 → all four blocks true
$ get-bucket-lifecycle-configuration                     → abort-incomplete-mpu, 1 day
$ get-bucket-policy      --bucket edullm-data-us-east-2  → Id "edullm-data-us-east-2-airlock-v2"
```

**VERIFIED.** Encryption and public-access posture match the primary (`edullm-data` returns the
identical AES256 + SSE-C-blocked config). The bucket policy has two Deny statements:

- `OnlyMirrorWriterWrites` — Deny `PutObject`/`PutObjectTagging`/`AbortMultipartUpload` on
  `arn:aws:s3:::edullm-data-us-east-2/*` unless `aws:PrincipalArn` equals
  `arn:aws:iam::<ACCOUNT>:role/sbsandbox-intern-edullm-infra-deployer`.
- `NobodyDeletesPublishedData` — Deny `DeleteObject`/`DeleteObjectVersion` on the same resource for
  every non-service principal.

**This closes an exposure that was live for ~1 day.** `artifacts/orchestration/LEDGER.md:856-880`
records that on 2026-08-07 this bucket held 2.67 TB of published corpus with
`get-bucket-policy → NoSuchBucketPolicy` and versioning **off**, and that a `delete-object` probe
with the broker principal **succeeded** (exit 0) against it while the same call on `edullm-data`
returned an explicit-deny. That gap is now closed on both counts (VERIFIED above). Treat
2026-08-07→08 as a window in which published data had no delete protection.

### 1b. One real defect remains in the mirror's airlock

**The sole principal the policy permits to write cannot write.** `sbsandbox-intern-edullm-infra-deployer`
holds three inline policies (`list-role-policies`); `list-attached-role-policies` → `[]`. I read all
three. Their only S3 grants are in `deploy-phase2-admission-stacks`, scoped to
`arn:aws:s3:::sbsandbox-intern-edullm-*`, `arn:aws:s3:::edullm-scratch`, and
`arn:aws:s3:::sbsandbox-intern-edullm-artifacts/*`. Phases 1 and 3 contain **zero** S3 statements
(`Statement[?contains(to_string(Action), 's3')]` → `[]` for both).

**VERIFIED: no S3 ARN in that role matches `edullm-data-us-east-2`, and none is a wildcard that
would.** Also, its trust policy admits only GitHub OIDC from three
`edu-llm/platform` workflows on `refs/heads/main` — no human or broker session can assume it.

Consequence: the mirror is currently **write-sealed against everyone, including its own designated
writer.** Good for safety, but it means *no existing principal can add the OLMoE releases to it.*
That is the crux of §5 and §7.

---

## 2. Replication is NOT configured anywhere

```
$ get-bucket-replication --bucket edullm-data
An error occurred (ReplicationConfigurationNotFoundError) … The replication configuration was not found   [exit 254]
$ get-bucket-replication --bucket edullm-landing
An error occurred (ReplicationConfigurationNotFoundError) … The replication configuration was not found   [exit 254]
$ get-bucket-replication --bucket edullm-data-us-east-2
An error occurred (ReplicationConfigurationNotFoundError) … The replication configuration was not found   [exit 254]
```

**VERIFIED, all three.** CRR/SRR has never been set up. The 2.67 TB in us-east-2 therefore arrived
by an explicit copy (`s3 sync`-shaped), not by replication — corroborated by
`LEDGER.md:645` ("a bulk `s3 sync`") and by the ETag evidence in §3.

**CRR's prerequisites, checked:**

- Source versioning — `get-bucket-versioning --bucket edullm-data` → `{"Status": "Enabled"}` ✅ **VERIFIED**
- Destination versioning — `edullm-data-us-east-2` → `{"Status": "Enabled"}` ✅ **VERIFIED**

Both are satisfied. The blocker for CRR is not versioning; it is the IAM role (§5).

**One thing CRR cannot do that matters here:** replication only acts on objects written *after* the
rule exists. It would not move the 123 already-missing objects, and for the OLMoE releases it would
only fire if the rule were in place *before* promotion — which conflicts with "mirror the sealed
result afterwards." Backfilling existing objects requires S3 Batch Replication, a separate job.

---

## 3. The size of the thing being mirrored, and bytes/token

### 3a. The two large published corpora

| dataset | primary objects | primary bytes | mirror objects | mirror bytes |
|---|---|---|---|---|
| `pretrain/reservoir-dolma2/v1/` | 10,053 | 1,004,875,466,439 | 10,053 | 1,004,875,466,439 |
| `pretrain/olmo-150b-dolma2/v1/` | 6,915 | 629,871,994,438 | 6,915 | 629,871,994,438 |

**VERIFIED, identical on both sides** — 1.005 TB and 0.630 TB respectively. (Object counts exceed
the seals' 10,049 / 6,911 by 4 each: the control files `dataset.json`, `README.md`,
`_VALIDATED.json`, `manifest.json`, which are not manifest entries.)

### 3b. bytes/token is exactly 4.0 — confirmed against a real corpus

From `pretrain/reservoir-dolma2/v1/dataset.json`: `inventory.bytes = 1004872007680`, and
`partitions[].rows` = 250,242,924,544 train + 975,077,376 val = **251,218,001,920 tokens**.

```
1,004,872,007,680 B ÷ 251,218,001,920 tok = 4.0000 B/token   ← exact, no overhead
```

**VERIFIED.** `.u32le.bin` is headerless raw uint32, so the payload is exactly 4 B/token with zero
framing. Your estimate is confirmed:

- **50B tokens → 200.0 GB** (200,000,000,000 B ≈ 186.3 GiB)
- **100B tokens → 400.0 GB** (400,000,000,000 B ≈ 372.5 GiB)

Shard granularity, for object counts: the reservoir uses 25,001,984-token shards = **100,007,936 B**
each (VERIFIED — every sampled `train-*.u32le.bin` in that corpus is exactly that size). At that
granularity 50B tokens ≈ **2,000 shards** and 100B ≈ **4,000 shards**. If OLMoE-mix uses the
150B-style variable shards instead, counts differ but bytes do not.

### 3c. The mirror is byte-identical — verified by recomputed checksum, not by trusting size

Sizes matching is weak evidence. ETags actively *mismatch* here, and that is expected rather than
alarming: the primary's objects are single-part (`"c853792e116f662c6b85218e6709c7e2"`) while the
mirror's are 12-part multipart (`"3c211a51eb4af3caaf5ebff2f4393e5d-12"`) — the signature of a
`s3 sync`/multipart copy. **ETag is therefore useless for comparison across the two buckets.**

So I used the seal's own per-shard checksums. `pretrain/olmo-150b-dolma2/v1/_VALIDATED.json` carries
a `crc64nvme` map of **6,911 entries**. I re-read the checksum from the *mirror's* objects with
`head-object --checksum-mode ENABLED` and compared to the seal:

| key (under `pretrain/olmo-150b-dolma2/v1/`) | seal CRC64NVME | mirror CRC64NVME | match |
|---|---|---|---|
| `tokens/all-dressed-snazzy2/adult_content/train-00000.u32le.bin` | `42AWgDrQJ/I=` | `42AWgDrQJ/I=` | ✅ |
| `…/adult_content/train-00001.u32le.bin` | `6W1SAQf9edY=` | `6W1SAQf9edY=` | ✅ |
| `…/adult_content/train-00002.u32le.bin` | `80gTjaaVCQA=` | `80gTjaaVCQA=` | ✅ |
| `tokens/s2pdf-redacted/electronics_and_hardware/train-03705.u32le.bin` | `E3a38FzOtDE=` | `E3a38FzOtDE=` | ✅ |
| `tokens/s2pdf-redacted/industrial/train-04585.u32le.bin` | `cxTQCYkyKNk=` | `cxTQCYkyKNk=` | ✅ |
| `tokens/s2pdf-redacted/entertainment/train-03814.u32le.bin` | `A6qxSABN5mI=` | `A6qxSABN5mI=` | ✅ |
| `tokens/stack-edu/PHP/train-06400.u32le.bin` | `QIV4plrMWpU=` | `QIV4plrMWpU=` | ✅ |

**7/7 VERIFIED** (last four chosen by `random.seed(11)` over the sorted 6,911 keys, so they are not
cherry-picked). Additionally `_VALIDATED.json` itself is byte-identical across buckets — same
275 bytes, same ETag `"03bf19e6b1e991be1eb960c2d6291c7a"`. This is a sample, not a full sweep:
**7 of 6,911 shards checked.** A complete verification is cheap and needs no egress — `head-object`
returns the stored checksum, so it is 6,911 metadata calls ≈ $0.003.

### 3d. The delta reconciles exactly to two datasets

The 123-object / 80,071,825,385-byte gap is fully accounted for:

| prefix | primary | mirror | Δ objects | Δ bytes |
|---|---|---|---|---|
| `pretrain/` | 18,435 | 18,329 | 106 | 79,993,815,085 |
| `curriculum/` | 27 | 15 | 12 | 78,009,228 |
| `_catalog/` | 36 | 32 | 4 | — |
| `_inventory/` | 5 | 4 | 1 | 0 |
| **total** | **18,578** | **18,455** | **123** | **80,071,825,385** |

The `pretrain/` delta is **exactly** two datasets — `lgbm-opt-10b` (52 obj / 39,996,729,075 B) and
`opt-with-synthetic-10b` (54 obj / 39,997,086,010 B): 52+54 = 106 and the bytes sum to
79,993,815,085 **to the byte** (VERIFIED). The same two are the missing `_catalog/pretrain/*` and
`_catalog/curriculum/*` entries. `_inventory/` differs by one object at Δ0 bytes — the primary's
weekly inventory has since written a new manifest. Top-level prefix sets are otherwise identical
(`_catalog/ _inventory/ curriculum/ pretrain/ sft/ tokenizer/ vendor/` on both).

Conclusion: nothing is silently corrupt or half-copied. The mirror is a clean point-in-time copy
that is two datasets stale.

---

## 4. Cross-region transfer cost and time

### 4a. Unit prices

| item | price | source |
|---|---|---|
| DTO us-east-1 → **us-east-2** | **$0.01/GB** | AWS S3 pricing page verbatim + Price List API `USE1-USE2-AWS-Out-Bytes` |
| DTO us-east-1 → other US regions | $0.02/GB | Price List API `USE1-USW1/USW2-AWS-Out-Bytes` |
| Data transfer IN | $0.00/GB | Price List API (all inbound SKUs 0.0) |
| PUT/COPY/POST/LIST | $0.005 / 1,000 | Price List API `Requests-Tier1` |
| GET/HEAD/other | $0.0004 / 1,000 | Price List API `Requests-Tier2` |
| S3 Standard storage | $0.023 / GB-month | Price List API `TimedStorage-ByteHrs` |
| RTC add-on (optional) | $0.015/GB | Price List API `S3RTC-Out-Bytes` |
| S3 Batch Operations | $0.25/job + $1.00/M objects | Price List API `BatchOperations-*` |

**Correction to the repo's own figure.** `docs/IMPLEMENTATION-PLAN.md:1195` and
`artifacts/orchestration/plat/status.md:523` both cost the mirror at **$0.02/GB**. For
**us-east-1 → us-east-2 specifically that is 2× the real rate**: this pair is discounted to
$0.01/GB, stated verbatim on AWS's S3 pricing page ("The data transfer charge from US East
(N. Virginia) to US East (Ohio) is $0.01 per GB"). $0.02/GB is the correct rate for us-west-*/EU,
not for Ohio. Every prior dollar figure in this repo for this mirror is therefore **halved**.

### 4b. The concrete figures

S3 bills decimal GB (10⁹ B). Object counts at 100,007,936 B/shard.

| scenario | GB | objects | egress | PUT | GET | **one-time** | storage/mo |
|---|---|---|---|---|---|---|---|
| 50B tokens | 200 | 2,000 | $2.00 | $0.01 | $0.00 | **$2.01** | $4.60 |
| 100B tokens | 400 | 4,000 | $4.00 | $0.02 | $0.00 | **$4.02** | $9.20 |
| both + overhead | 600 | 6,000 | $6.00 | $0.03 | $0.00 | **$6.03** | $13.80 |
| *(the actual 2-dataset gap)* | 80 | 106 | $0.80 | $0.00 | $0.00 | **$0.80** | $1.84 |
| *(whole bucket, if ever re-done)* | 2,749 | 18,578 | $27.49 | $0.09 | $0.01 | **$27.59** | $63.23 |

Arithmetic, 600 GB case: `600 × $0.01 = $6.00` egress; `6,000 × $0.005/1000 = $0.03` PUT;
`6,000 × $0.0004/1000 = $0.0024` GET. Total **$6.03**, plus `600 × $0.023 = $13.80/month`
recurring for the destination copy.

**The transfer cost is negligible — single-digit dollars.** The recurring storage ($13.80/mo for
both releases; $63/mo if the whole bucket stays mirrored) exceeds the one-time transfer within a
month. Cost is *not* the deciding factor here; permissions and the invariant are.

### 4c. Does `CopyObject` pull through the caller?

**No.** Cross-region `CopyObject` (and `s3 sync` between two S3 buckets, which uses it) is
**server-side**: S3 reads the source and writes the destination internally; the bytes never traverse
the caller. This is why the existing 2.67 TB could be mirrored despite the memory note
"publish() must run in-region" — that constraint applies to *hashing*, which pulls bytes, not to
copies. **Transfer is still billed** at $0.01/GB regardless, and the caller pays one request per
object. A `s3 sync` driver therefore needs only enough runtime to issue 6,000 API calls — minutes,
not hours — and its own bandwidth is irrelevant.

Same-region vs cross-region matters only for the egress line: an SRR/same-region copy would be
$0.00/GB transfer, cross-region is $0.01/GB. Requests and destination storage are identical either
way.

### 4d. Wall clock

Server-side copy of 6,000 objects at 100 MB each, parallelized ~32-64 ways: **~10-30 minutes**,
dominated by per-object copy latency rather than bandwidth. For reference, the existing 2.67 TB /
18,455-object mirror was completed within a single session on 2026-08-07 (mirror
`LastModified` 2026-08-08T02:22:10Z vs primary 2026-08-05T22:31:51Z). Objects >5 GB would need
multipart copy; none here approach that.

---

## 5. Which mechanisms are actually available to us

### 5a. CRR needs an admin. Plainly: yes.

S3 Cross-Region Replication requires a service role that `s3.amazonaws.com` can assume. Creating it
needs `iam:CreateRole`, which is **denied** to broker sessions by `InternSandboxBoundary` —
`docs/dataset-creation/DATASET-STANDARD.md:167` is the only ❌ in its otherwise-green table:

> `iam:CreateRole` | ❌ denied by `InternSandboxBoundary` | **not needed** — design reuses an existing role

Restated at `infra/DEPLOY.md:26-27` and `:756`. No existing role is assumable by `s3.amazonaws.com`
(the candidate roles' trust policies name `ecs-tasks.amazonaws.com` or GitHub OIDC — VERIFIED for
`infra-deployer`, DOCUMENTED for the ingest/publish roles). So **CRR cannot be enabled by us. It
needs an admin to create the replication role.** `s3:PutBucketReplication` is additionally
**absent from the permitted table entirely** — DOCUMENTED as untested, not as allowed.

### 5b. `s3:CreateBucket` — moot, and here is the correction

`DATASET-STANDARD.md:154-155` lists `s3:CreateBucket`, `s3:PutBucketPolicy` and
`s3:PutBucketVersioning` as **✅ allowed**, each smoke-tested live against a probe bucket
("probe bucket created", "conditional-write Deny applied"). **DOCUMENTED; I did not probe** — per
your instruction I did not attempt a create.

Two corrections to the task's framing:

1. That table is in **`docs/dataset-creation/DATASET-STANDARD.md` §1, not `infra/DEPLOY.md` §1.**
   DEPLOY.md has no §1 and no table of its own; its ~20 references to "§1's table" all cross-refer
   to the standard.
2. **The question is moot** — the bucket already exists, correctly configured (§1a). We do not need
   `CreateBucket`.

Also note `iam:PutRolePolicy` is listed ✅ but is the one row verified **by the simulator only**
(`DATASET-STANDARD.md:164`, "simulator-confirmed allowed"). Given `:170-172` warns the simulator
returned `explicitDeny` for eleven actions that all work, a simulator *allow* is the weakest
evidence in the table and points the wrong way. **Treat `iam:PutRolePolicy` as UNPROBED**, not as
established. I did not test it. Per your instruction I report no simulator output as truth.

### 5c. No existing policy grants write to the mirror, and no wildcard reaches it

A subagent read all 11 policy files in
`/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/claude-20--olmoe-mix-ingest/infra/`.
**Every S3 Resource ARN names `edullm-data` or `edullm-landing` literally.** There is no
`arn:aws:s3:::*`, no `arn:aws:s3:::edullm-*`, and no bare `"Resource": "*"`. Representative:

- `03-validator-policy.json:12` — `arn:aws:s3:::edullm-data/*` (the validator's only write grant)
- `09-reservoir-publish-policy.json:100` — Deny PutObject on `arn:aws:s3:::edullm-data/*`
- `10-dataset-publish-policy.json:52-58` — seven family prefixes, all under `edullm-landing/`

Your ARN observation is correct and load-bearing: S3 ARNs are region-agnostic in *form*
(`arn:aws:s3:::name`), so a policy naming a bucket works regardless of region — **but the name must
appear.** `arn:aws:s3:::edullm-data/*` does **not** match `edullm-data-us-east-2`, because the
wildcard sits in the key position after the bucket segment is already fixed. **Confirmed: no
existing policy would cover the mirror.** A new statement is required.

On the platform side, `/tmp/edullm-platform/infra/iam/`:

- **`block-fleet-roles.yaml:65`** — `DataBucket` defaults to **`edullm-data-us-east-2`**, committed
  on `main`. So the mirror is already a committed dependency of the capacity-block fleet.
- **`block-fleet-roles.yaml:370-378`** — role `sbsandbox-intern-edullm-block-node`, Sid
  `ReadTheCorpusMirror`: `s3:GetObject`/`ListBucket`/`GetBucketLocation` on `${DataBucket}`.
  **Read-only — no PutObject.** Deliberate; the comment at `:349-352` says "the asymmetry is the
  point … nothing on these machines should be able to rewrite the corpus."
- `infra-deployer-role.yaml:210` scopes bucket-config actions to
  `arn:aws:s3:::sbsandbox-intern-edullm-*`, which does **not** match the mirror — and `:216-223`
  documents a deliberate refusal to widen to `edullm-*`.
- `researcher-role.yaml:211-214` has `Action: "*"` on `Resource: "*"`, narrowed by later Denies. Its
  `DenyWorkingTierWritesOutsideYourOwnPrefix` uses `NotResource` (`:471-474`) that does not list the
  mirror, so PutObject there is Denied — but by a fence built for another purpose. **Fragile; do not
  rely on it either as a grant or as a protection.**

**Answer: no role, in either repo, holds `s3:PutObject` on a us-east-2 bucket except the block-node
on `edullm-block-outputs-us-east-2`. Nothing can write the corpus mirror.**

Two side corrections worth recording:

- **CLAUDE.md's claim that the CPU workload role holds no `s3:GetObject` on
  `edullm-landing`/`edullm-data` is now FALSE.** `batch-roles.yaml:366-383` grants GetObject on
  both `edullm-data/*` and `edullm-landing/*` and ListBucket on both buckets; the template documents
  the change at `:248-253`. The predicted `AccessDenied` for `edullm-data-validate` should no longer
  fire. DOCUMENTED from the template; **re-probe live before planning around it either way.**
- **Batch cannot run in us-east-2 at all.** `/tmp/edullm-platform/infra/README.md:450-454` and
  `batch-network.yaml:18-22`: "`us-east-2` is not a fallback, and looks like one … `ec2:CreateVpc`,
  `ec2:CreateSubnet`, `ec2:CreateSecurityGroup` and `ec2:RunInstances` are all
  `UnauthorizedOperation` there. An EC2 compute environment in `us-east-2` is not possible at all."
  This is exactly *why* the capacity-block fleet needs an S3 mirror rather than a Batch job. Any
  copy job must therefore run **from us-east-1**, writing cross-region — which is fine, since
  `CopyObject` is server-side (§4c).

---

## 6. Does the standard or the validator care about region?

**No code enforces the single-region rule. It is documentation and a cost argument only.**

- **The invariant, stated once:** `docs/dataset-creation/DATASET-STANDARD.md:938-939` —
  > **One region.** All datasets and all compute in `us-east-1`. Cross-region reads of a 633 GB
  > corpus would cost ~$6.34/epoch in egress plus a latency tax on 13,840 GETs.

  It is an **economic** rule, not a mechanism. No validator gate, no assertion, no check enforces
  it. And the capacity-block fleet is already a live, committed exception
  (`block-fleet-roles.yaml:65`).

- **`read.py` — bucket is a default, not a hardcode.** `src/edullm_data/read.py:25` sets
  `DATA_BUCKET = "edullm-data"`, but it is threaded as a keyword argument:
  `dataset_paths(..., data_bucket: str = DATA_BUCKET)` at `read.py:383`, and likewise
  `resolve_latest` (`:805`), `verify_seal` (`:824`), `build_mixture` (`:990`). So
  **a mirrored copy IS readable** — `dataset_paths(..., data_bucket="edullm-data-us-east-2")`
  works with **no code change**. There is **no region parameter and no `EDULLM_*_BUCKET` env var**
  (the only `EDULLM_`-prefixed var in `src/` is `EDULLM_FAMILIES_DIR`).

- **`s3.py` region is a default kwarg.** `src/edullm_data/s3.py:212` —
  `def default(cls, region: str = "us-east-1", ...)`, passed to `boto3.client("s3", region_name=region)`
  at `:238`. S3 GETs are not region-scoped in practice, so a us-east-1 client reads a us-east-2
  bucket fine (paying egress). Note the CLI drivers expose `--data-bucket`
  (`validate.py:2462`, `fsck.py:269`) but **none exposes `--region`**. Other hardcoded regions:
  `fsck.py:279` (ECR, not S3), `corpus_build.py:1323`, and `publish.py:759`
  (`env.get("AWS_REGION", "us-east-1")` — a provenance field recorded into `dataset.json`, not used
  to route calls). `LocationConstraint` appears **nowhere** in `src/`.

- **`_VALIDATED.json` is NOT region-aware — and that is what makes mirroring cheap.**
  `validate.py:2192-2215` builds the seal with exactly: `dataset_id`, `version`, `objects`, `bytes`,
  `dataset_sha256`, `manifest_sha256`, plus optional `crc64nvme` and `validated_at`. **No bucket,
  no region, no URI.** It is a pure content claim, so **a byte-copied mirror's seal remains valid
  verbatim** — confirmed empirically in §3c, where the mirror's `_VALIDATED.json` is byte-identical
  and the mirror's shards match its CRC64NVME map. `verify_seal(..., data_bucket=<mirror>)` would
  pass. **No re-validation and no re-sealing is needed.**

- **`_catalog/` is the one place a bucket name is recorded — and the reader never reads it.**
  `validate.py:2142-2158` writes `"uri": f"s3://{data_bucket}/{prefix}/"`. But
  `read.resolve_latest` (`read.py:805-816`) only *lists* `_catalog/{dataset_id}/` and parses
  **key names**, never opening the JSON; `dataset_paths` then builds URIs from its own
  `data_bucket` argument (`read.py:482`). `fsck.py:90-98` likewise parses only keys.

  So: **yes, a mirror needs its own `_catalog/` entries to be discoverable** — `resolve_latest`
  lists the catalog in whatever bucket it is given. But since only key names are consulted,
  **copying `_catalog/` verbatim suffices.** The stale `uri` field inside will point at
  `s3://edullm-data/…` and is harmless to today's reader, though it is a latent trap for any future
  code (or human) that trusts it. The mirror already has 32 such entries copied this way.

---

## 7. Recommendation

The owner's instinct was right and is already implemented. The decision is narrower than it looks.

**Reframe:** we are not deciding whether to break the single-region invariant. It was broken on
2026-08-07 when 2.67 TB was copied to us-east-2, and it is *load-bearing* — `block-fleet-roles.yaml:65`
commits the capacity-block fleet to reading `edullm-data-us-east-2`, and Batch/EC2 **cannot run in
us-east-2**, so the fleet has no alternative. The live question is only: **how do the two OLMoE
releases get added to a mirror that no principal can currently write?**

Costs are trivial either way — **$2.01 for 50B, $4.02 for 100B, $6.03 for both** one-time, plus
$13.80/month. Halve every prior estimate in this repo: `IMPLEMENTATION-PLAN.md:1195` uses $0.02/GB,
but us-east-1→us-east-2 is $0.01/GB.

### Option A — Batch `s3 sync` under a role granted on the mirror  ← recommended

Promote in us-east-1 exactly as normal (unchanged, no new risk), then run a post-promotion copy job
from us-east-1 writing cross-region.

- **Cost** $6.03 one-time + $13.80/mo. **Wall clock** ~10-30 min for 6,000 objects.
- **Needs an admin?** **One step only.** Add a statement granting `s3:PutObject` +
  `s3:AbortMultipartUpload` on `arn:aws:s3:::edullm-data-us-east-2/*` to an existing ecs-tasks role,
  and add that role's ARN to the mirror bucket policy's `OnlyMirrorWriterWrites` exemption. Whether
  we can do this ourselves hinges on `iam:PutRolePolicy` + `s3:PutBucketPolicy`, both listed ✅ but
  the former **simulator-only, hence UNPROBED**. **Smoke-test it; do not assume.** If it fails, an
  admin does one `put-role-policy`.
- **Preserves** the airlock shape (a single named writer), needs no new role, no new bucket, and no
  change to the promotion path. Seals travel unchanged (§6).
- **Do not** grant the *validator* this write. It does not run in us-east-2 and must not become the
  mirror's writer — keep promotion and mirroring as distinct principals.

### Option B — CRR with an admin-created replication role

- **Cost** same egress; +$0.015/GB if RTC is enabled (unnecessary here).
- **Needs an admin: YES, unavoidably.** `iam:CreateRole` is **denied** (`DATASET-STANDARD.md:167`),
  and no existing role is assumable by `s3.amazonaws.com`. `s3:PutBucketReplication` is not even on
  the permitted list.
- **Two structural mismatches:** replication only fires on objects written *after* the rule exists,
  so (i) it would not backfill the 123 missing objects without a separate S3 Batch Replication job,
  and (ii) it fires *at promotion time*, contradicting "mirror the sealed result afterwards" — it
  would stream objects into us-east-2 before the seal is written. Worth it only if we want ongoing
  automatic mirroring of all future datasets, which is a different decision.

### Option C — one-shot copy from a session with the right grants

- **Cheapest to reason about, no new IAM**, but it requires the broker principal to hold PutObject
  on the mirror — which the bucket policy **explicitly Denies** for every principal except
  `infra-deployer` (§1a). A bucket-policy Deny cannot be overridden by an identity grant. So this
  needs `s3:PutBucketPolicy` to add ourselves as an exempt writer, which **defeats the airlock's
  purpose** by making an interactive session a writer to published data. **Not recommended.**

### What we CANNOT do ourselves

1. **Create the CRR replication role** — `iam:CreateRole` denied, documented and probed 2026-08-05.
   Option B is admin-gated, full stop.
2. **Make `infra-deployer` work as the mirror's writer** — it has no S3 grant matching the mirror
   (VERIFIED) and is assumable only by three GitHub OIDC workflows on `main`, not by us. Fixing it
   means a platform PR, i.e. an admin/lead.
3. **Run any compute in us-east-2** — VPC/EC2 creation is `UnauthorizedOperation` there. Every copy
   must be driven from us-east-1.
4. **`iam:PutRolePolicy` / `s3:PutBucketPolicy`** — listed ✅ in the standard's table, but
   `PutRolePolicy` rests on simulator evidence only. **UNPROBED. Smoke-test before committing to a
   plan that assumes it.**

### Two things to fix regardless of the option chosen

- **The mirror has no writer.** Its bucket policy exempts a role that holds no S3 permission on it
  and that no one here can assume. Until that is reconciled the mirror is frozen — including
  against the 123-object backfill.
- **Close the 123-object gap** (`lgbm-opt-10b`, `opt-with-synthetic-10b`, and their `_catalog/`
  entries) for **$0.80**, so "the mirror" means the whole published corpus rather than a
  two-dataset-stale snapshot. Also consider a full 6,911-shard checksum sweep of
  `olmo-150b-dolma2` — `head-object` reads the stored CRC64NVME with no egress, so verifying every
  shard against its seal costs ~$0.003 and would upgrade §3c from a 7-shard sample to proof.
