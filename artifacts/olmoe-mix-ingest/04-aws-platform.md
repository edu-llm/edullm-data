# OLMoE-mix ingest — AWS / platform recon

Read-only recon, 2026-08-08, via `mcp__sb-aws__*` (account `sbsandbox`) plus unauthenticated
`curl`. No writes, no submissions, no EC2 starts. Every number below is command output, not
inference, unless explicitly labelled as arithmetic.

---

## 0. Headline findings (read these first)

1. **The mix is `OLMoE-mix-0824`, not `0924`, on the tokenized side.** OLMo-core ships exactly one
   OLMoE mix file: `OLMo-core/src/olmo_core/data/mixes/OLMoE-mix-0824.txt`. There is no
   `OLMoE-mix-0924.txt`. `allenai/OLMoE-mix-0924` exists on HF but is the **raw text** release.
   These are two different artifacts and the task brief conflates them.

2. **"Hundreds of GB" is wrong by ~20-40x.** Measured, not estimated:
   - tokenized (`olmo-data.org`, 1,122 shards, HEAD on every one): **14,510 GiB = 14.17 TiB
     = ~3.895T tokens**
   - HF raw text (`allenai/OLMoE-mix-0924`, 2,906 files): **6.82 TiB** (`treesize` API)

3. **We do NOT need to download the full corpus, and we do NOT need to tokenize.** The tokenized
   shards are **anonymously readable** over HTTPS and are **headerless raw uint32-LE** — byte
   identical in format to our `.u32le.bin`. A 50B release needs ~186 GiB and a 100B release
   ~373 GiB of *selected* shards. Ingest is copy+rename, not re-tokenization.

4. **Nothing OLMoE is in our S3 today.** Searched all three buckets. Confirmed absent — but
   `olmo100b/olmo-mix-1124-30b/` and `pretrain/olmo-150b-dolma2/` are adjacent OLMo mixes.

5. **Auto-promotion is live and will fire.** `edullm-landing-manifest-created` is `ENABLED` and
   matches **any** key suffixed `manifest.json` in `edullm-landing`. There is no dataset-name
   scoping. Writing a manifest submits `edullm-validator` (unversioned → currently rev 16).

6. **The 60-minute timeout failure is fixed.** `edullm-validator:16` carries
   `attemptDurationSeconds = 28800` (8h). No relevant job def is at 3600 except `edullm-fsck`.

---

## 1. Identity, accounts, region

`mcp__sb-aws__whoami`:

```
userEmail: eric.wu@alphaaiengineering.com
employeeType: INTERN
onboarded: false      mfaEnrolled: false      eligibleForSelfOnboard: false
accountsProvisioned: [ sbsandbox = <ACCOUNT> ]
accountsSelfServe:   [ sbsandbox, legacy ]
```

`mcp__sb-aws__accounts`: **ready** `[sbsandbox]`; **selfServe** `[legacy]`; **adminGated**
`[sbproduction]`.

`sts get-caller-identity` in `sbsandbox`:

```
Arn: arn:aws:sts::<ACCOUNT>:assumed-role/Intern-eric.wu-sbsandbox/broker-eric.wu-1786202711
```

**Region: `us-east-1`** (every Batch/ECR/EventBridge ARN). `get-bucket-location` on `edullm-data`
returns `LocationConstraint: null`, which is us-east-1. Note `onboarded:false` / `mfaEnrolled:false`
did **not** block any read call in this session — all 30+ calls returned `exitCode 0`.

Per instruction, `iam:simulate-principal-policy` was **not** used. All permission statements below
are either live call results or literal bucket-policy text.

---

## 2. Bucket state

### `s3://edullm-data` (published, immutable)

Top-level (`list-objects-v2 --delimiter / --prefix ''`) — 7 prefixes:

```
_catalog/   _inventory/   curriculum/   pretrain/   sft/   tokenizer/   vendor/
```

CloudWatch `BucketSizeBytes` / `NumberOfObjects` (daily, StandardStorage / AllStorageTypes):

| date | bytes | GiB | TiB | objects |
|---|---|---|---|---|
| 2026-08-03 | 1,622,857,171,326 | 1,511.4 | 1.476 | 8,510 |
| 2026-08-04 | 1,840,236,988,073 | 1,714.0 | 1.674 | 9,039 |
| 2026-08-05 | 3,520,294,753,516 | 3,278.5 | **3.201** | **25,497** |
| 2026-08-06 | 3,520,294,753,516 | 3,278.5 | 3.201 | 25,497 |

(Metric is ~2 days stale — normal for S3 storage metrics. Used deliberately instead of a recursive
listing of 25k objects.)

`pretrain/` — 21 dataset names:

```
fineweb-edu-1b            fineweb-edu-750m          fineweb2-equal-bytes
fineweb2-phase0-equal-bpe-2b   fineweb2-phase0-equal-superbpe-2b
fineweb2-unimax-bpe-20b   fineweb2-unimax-superbpe-20b
formal-proof-premises-500m     frontload-cl-10b     lean4-mathlib-bytes
lgbm-opt-10b   math-frontload-100m   math-memory-full
olmo-127b      olmo-150b-dolma2      olmo-original-30b
opt-with-synthetic-10b   refhq-instruct   refhq-regmix-5p5b
regmix-10b     reservoir-dolma2
```

`vendor/` — one entry: `vendor/openai-prm800k/`.
`pretrain/reservoir-dolma2/` — one version: `v1/`.

The `_catalog/` index is the cheapest full inventory of addresses (36 objects, `family/name/vN.json`):

```
curriculum: lgbm-opt-10b/v1, opt-with-synthetic-10b/v1, regmix-370m/v1
pretrain:   fineweb-edu-1b/v2, fineweb-edu-1b/v6, fineweb-edu-750m/v2,
            fineweb2-equal-bytes/v1, fineweb2-phase0-equal-bpe-2b/v1,
            fineweb2-phase0-equal-superbpe-2b/v1, fineweb2-unimax-bpe-20b/v1,
            fineweb2-unimax-superbpe-20b/v1, formal-proof-premises-500m/{v2,v3},
            frontload-cl-10b/v1, lean4-mathlib-bytes/v3, lgbm-opt-10b/v2,
            math-frontload-100m/v1, math-memory-full/v1, olmo-127b/v1,
            olmo-150b-dolma2/v1, olmo-original-30b/v1, opt-with-synthetic-10b/v1,
            refhq-instruct/v3, refhq-regmix-5p5b/v2, regmix-10b/v1,
            reservoir-dolma2/v1
sft:        frontload-cl-chat-sft/v1, math-sft-60m/v1, pedagogy70-normal30/v1
tokenizer:  bytes-utf8/v1, dolma2-bpe/v1, gigatoken-bpe/v1, gigatoken-superbpe/v1,
            qwen25-vendored/v1, smollm2-bpe/v1
vendor:     openai-prm800k/v1
```

**No `olmoe` anywhere.** Address shape is confirmed `<family>/<name>/<version>/`, versions are bare
`vN`.

### Airlock Deny — verified intact (policy text, not simulator)

`get-bucket-policy` on `edullm-data`, Id `edullm-data-airlock-v2`:

- **`OnlyValidatorWrites`** — `Deny` `PutObject`/`PutObjectTagging`/`AbortMultipartUpload` on
  `edullm-data/*` to `Principal:*` unless `aws:PrincipalArn` is one of
  `role/sbsandbox-intern-edullm-infra-deployer` or
  `role/sbsandbox-intern-edullm-dataset-validator`.
- **`NobodyDeletesPublishedData`** — `Deny` `DeleteObject`/`DeleteObjectVersion` to every
  non-service principal. **Frozen means frozen is enforced at the bucket, not by convention.**
- `AllowS3InventoryDelivery` — service-only `PutObject` into `_inventory/*`.

Consequence for this task: the intern role cannot write to `edullm-data`, so the publish must land
in `edullm-landing` and be promoted by the validator role. Only two roles can write, and
`edullm-prm800k-producer` / `edullm-dataset-publish` / `edullm-final-dataset-build` are **not**
among them — those write to landing.

### `s3://edullm-landing` (scratch inbox)

Top-level — 13 prefixes:

```
_dist/  _ingest/  _migrate/  _preserved/  _scratch/  _src/  _staging/  _tmp/
curriculum/  pretrain/  sft/  tokenizer/  vendor/
```

Size (CloudWatch): 2026-08-05 **6,695,538,272,043 B = 6.09 TiB**; 08-06 6,449,017,982,839 B
(5.87 TiB) — shrinking as the 14-day expiry reaps.

Sub-prefixes that matter:

```
_ingest/    final-dataset/, reservoir-dolma2/
_migrate/   olmo-150b-dolma2/, olmo-150b-staged/
_src/       nemotron-cc-math-v1/
_staging/   eval/, pretrain/, vendor/
_scratch/   fpov/, plan-a-fineweb/
```

**Lifecycle (`get-bucket-lifecycle-configuration`) — plan around this:**

| rule | prefix | expiry |
|---|---|---|
| expire-pretrain-14d | `pretrain/` | **14 days** |
| expire-curriculum-14d | `curriculum/` | 14 days |
| expire-sft-14d | `sft/` | 14 days |
| expire-eval-14d | `eval/` | 14 days |
| expire-probe-14d | `probe/` | 14 days |
| expire-vendor-14d | `vendor/` | 14 days |
| expire-pending-14d | `_pending/` | 14 days |
| expire-ingest-30d | `_ingest/` | **30 days** |
| abort-incomplete-multipart-uploads-1d | `` (all) | aborts MPU after 1 day |

Two operational consequences:
- A 373 GiB staging area under `pretrain/` is reaped at 14 days. `_ingest/` buys 30. **`_dist/`,
  `_src/`, `_migrate/`, `_staging/`, `_scratch/` have NO expiry rule** — they persist.
- `abort-incomplete-multipart-uploads-1d` will kill any multipart upload of a 14 GiB shard that
  stalls past 24h.

### `s3://edullm-landing/_dist/` — wheels and drivers

26 objects. Wheels present: `0.1.0, 0.2.0, 0.3.0, 0.4.0, 0.5.0, 0.5.1, 0.6.0, 0.6.1, 0.6.2, 0.6.3,
0.7.0, 0.7.1, 0.7.2, 0.7.3, 0.7.4, 0.8.0`. All PEP-427 named
`edullm_data-<version>-py3-none-any.whl`.

**The newest wheel here is 0.7.4 (2026-08-03; 0.8.0 was pushed 08-02 and is smaller, 134,989 B).
The live container images are built from commits at version 0.9.1.** The `_dist/` wheels are a
stale distribution channel — see §6. Also present: `eval-decontamination.bin` (54,350,848 B =
51.8 MiB, the 13-gram index), `families/`, `olmo150-artifacts/`, `publish_driver.py`,
`publish_driver_v2.py`, `publish_olmo150.py`, `backfill_*.py`, `verify_inplace.py`,
`olmo150_plan.json` (1,956,818 B).

---

## 3. Is any OLMoE mix already in our S3? — **No**

Searched all three candidate buckets for `olmoe`, `olmo`, `dclm`, `starcoder`, `pes2o`, `mix-0924`.

`s3://edullm-datasets` (the legacy corpus bucket — confirmed present in **sbsandbox**, readable;
CloudWatch size **1,792,639,191,239 B = 1.63 TiB**, flat across 08-04→08-06) — 15 top-level prefixes:

```
_manifests/  _scratch/  curriculum-p1-jul23/  datamix1-jul22/  mixlaw/  mythos-rdt/
olmo-150b-dolma2/  olmo100b/  olmo30b/  p1hypothesis/  p3math/  plan-b/  refhq/
regmix/  tmp/
```

`olmo100b/` contains `_scratch/` and **`olmo-mix-1124-30b/`** (`data/`, `plan/`, `README.md`).

**Verdict:** no `olmoe`, no `mix-0924`, no `dclm`/`starcoder`/`pes2o` prefix in any of
`edullm-data`, `edullm-landing`, `edullm-datasets`. The three OLMo-family things we hold are
`olmo-mix-1124-30b` (a *different, later* mix — Nov 2024), `olmo-150b-dolma2`, `olmo30b`.
**Nothing here saves the download.** What saves the download is §4.

---

## 4. AI2's public data — the important finding

### `ai2-llm` S3 bucket is closed to anonymous access

- `https://ai2-llm.s3.us-west-2.amazonaws.com/?list-type=2&prefix=preprocessed/olmoe-mix-0924/…`
  → `PermanentRedirect`, `<Endpoint>s3.amazonaws.com</Endpoint>` (**the bucket is NOT in us-west-2**;
  it is us-east-1, same region as us).
- Retried against `s3.amazonaws.com` with prefixes `preprocessed/olmoe-mix-0924/`, `preprocessed/`,
  `olmoe-mix-0924/` → all **`AccessDenied`**. Anonymous **listing is off**.
- Anonymous **GET** of a real shard key → **`403`**.

So `ai2-llm` is unusable anonymously, both list and get. (An older era of this bucket was public;
that is stale.)

### `olmo-data.org` **does** serve the tokenized shards anonymously — verified

Cloudflare-fronted (`server: cloudflare`, `cf-ray: …-LAX`, IPs `104.21.83.123` / `172.67.175.213`).
Listing is off (404 on `?list-type=2`), but **object GET/HEAD works** — so you must bring your own
key list, which OLMo-core provides.

The tokenizer path segment is **`allenai/dolma2-tokenizer`**, not `dolma2-tokenizer`:

| host | `{TOKENIZER}` value | range-GET result |
|---|---|---|
| ai2-llm.s3.amazonaws.com | `gpt-neox-olmo-dolma-v1_5` | 403 |
| ai2-llm.s3.amazonaws.com | `dolma2-tokenizer` | 403 |
| ai2-llm.s3.amazonaws.com | `allenai/dolma2-tokenizer` | 403 |
| olmo-data.org | `gpt-neox-olmo-dolma-v1_5` | 404 |
| olmo-data.org | `dolma2-tokenizer` | 404 |
| **olmo-data.org** | **`allenai/dolma2-tokenizer`** | **206, 16 bytes** |

### Full measured inventory — HEAD on all 1,122 shards, zero errors

Key list generated from `OLMoE-mix-0824.txt` with `{TOKENIZER}` → `allenai/dolma2-tokenizer`.
Tokens computed as `bytes / 4` (uint32).

| source | shards | GiB | GiB/shard | Btok | Btok/shard | share |
|---|---|---|---|---|---|---|
| dclm | 945 | 13,802.18 | 14.61 | 3,704.99 | 3.92 | 95.12% |
| starcoder | 100 | 309.36 | 3.09 | 83.04 | 0.83 | 2.13% |
| pes2o | 26 | 218.12 | 8.39 | 58.55 | 2.25 | 1.50% |
| proofpile-2-arxiv | 20 | 77.39 | 3.87 | 20.77 | 1.04 | 0.53% |
| proofpile-2-open-web-math | 13 | 45.39 | 3.49 | 12.19 | 0.31% | 0.31% |
| proofpile-2-stack | 16 | 44.03 | 2.75 | 11.82 | 0.74 | 0.30% |
| wikipedia | 2 | 13.64 | 6.82 | 3.66 | 1.83 | 0.09% |
| **TOTAL** | **1,122** | **14,510.11** | | **3,895.03** | | 100% |

**14.17 TiB / ~3.895T tokens.** Note `starcoder` here is 83.04B tokens, matching the
"83.0B" figure in `artifacts/mixture-research/06-published-mixes.md:208` — an independent
cross-check that these are the right shards.

### The shards are headerless raw uint32 — the format we already use

First 16 bytes of one shard per source (range-GET `0-15`). **No `\x93NUMPY` magic in any of them**:

```
proofpile-2-stack          02000000 63840000 0e000000 43030000
proofpile-2-arxiv          3b000000 3f0b0000 5a000000 8d080100
proofpile-2-open-web-math  db010000 ba4a0000 b7010000 dc1e0000
pes2o                      4b150000 e6790000 7e580000 3b010000
starcoder                  1b000000 09010000 6d020000 75010000
dclm                       27000000 b3c30000 a78b0000 58000000
wikipedia                  3fb30000 66360000 40010000 4c100100
```

Every group reads as small little-endian uint32 token IDs. This is the ".npy lie" again — the
extension says `.npy`, the bytes are raw `u32le`. Per this repo's invariant, ingest must **rename to
`.u32le.bin`**. This matches the already-established
`ai2-dolma3-shards-are-byte-compatible` finding: **ingest = copy + rename, no re-tokenization.**

Two caveats worth flagging before anyone commits to these bytes:
- The tokenizer is **dolma2** (50,280-ish vocab family), *not* our SuperBPE / gigatoken tokenizers.
  Mixing these shards with a corpus tokenized differently is invalid.
- `dclm` shards are **14.61 GiB / 3.92B tokens each** — see §8 for why that dictates the slicing plan.

### HF `allenai/OLMoE-mix-0924` is the RAW TEXT release — different artifact

`https://huggingface.co/api/datasets/allenai/OLMoE-mix-0924`:

```
id: allenai/OLMoE-mix-0924   downloads: 1367   lastModified: 2024-12-02
gated: false   private: false   tags: [license:odc-by, size_categories:1B<n<10B]
treesize /: 7,504,048,280,704 bytes = 6.82 TiB
```

2,906 files, all **`.json.gz` (933) / `.zst` (1,970)** — text, not tokens:

```
data/dclm            1970      data/starcoder    863     data/open-web-math  26
data/pes2o             26      data/algebraic-stack 16    data/wiki            2
```

`allenai/OLMoE-mix-0824` does **not** exist on HF (API returns non-JSON / 404).

So the two releases are: **0924 = raw text on HF (6.82 TiB)**, **0824 = tokenized on
olmo-data.org (14.17 TiB)**. License **odc-by** on the HF side.

**Recommendation:** take the tokenized `olmo-data.org` shards. It skips tokenization entirely
(memory: tokenize is filter-bound at ~8.4 GB/h/container — tokenizing 6.82 TiB would be a
multi-day critical path), and the bytes are already in our shard format.

### Transfer cost / speed

- The bucket redirect proves `ai2-llm` is **us-east-1** — *same region as us* — but it is closed,
  so we cannot exploit that with a server-side copy. There is no cross-region egress question of
  the kind the brief anticipated.
- `olmo-data.org` is **Cloudflare**, so bytes arrive over the public internet from a CDN edge (LAX
  PoP observed). AWS charges **$0.00 for ingress**, so a Batch job in us-east-1 pulling from
  Cloudflare pays only for compute + the S3 PUTs. Cloudflare's R2/CDN egress is AI2's cost, not
  ours.
- Because listing is off, the **key list must come from `OLMoE-mix-0824.txt`** (in the OLMo-core
  checkout) — it cannot be discovered at runtime.

---

## 5. Batch state

### Queues — 16, all `ENABLED` / `VALID` / `JobQueueType: ECS`, priority 1, one CE each

**CPU (this is the one that matters):**

| queue | compute env | max vCPU | desired | instance | type |
|---|---|---|---|---|---|
| `sbsandbox-intern-edullm-cpu` | `sbsandbox-intern-edullm-cpu` | **384** | **0** | `c7i.8xlarge` | **EC2 (on-demand)** |

Allocation strategy `BEST_FIT_PROGRESSIVE`, `minvCpus: 0`. **GPU queues are entirely separate** — 15
of them, listed below. So CPU publish/validate work never contends with training.

| GPU queue | max vCPU | desired | instance |
|---|---|---|---|
| gpu (1xA10G) | 384 | 0 | g5.xlarge |
| gpu-1xt4 | 96 | 0 | g4dn.xlarge |
| gpu-4xt4 | 288 | 0 | g4dn.12xlarge |
| gpu-8xt4 | 288 | 0 | g4dn.metal |
| gpu-1xl4 | 96 | 0 | g6.xlarge |
| gpu-4xl4 | 288 | 0 | g6.12xlarge |
| gpu-8xl4 | 576 | 0 | g6.48xlarge |
| gpu-1xl40s | 96 | 0 | g6e.xlarge |
| gpu-4xl40s | 144 | **48** | g6e.12xlarge |
| gpu-8xl40s | 576 | 0 | g6e.48xlarge |
| gpu-4xa10g | 288 | 0 | g5.12xlarge |
| gpu-8xa10g | 576 | 0 | g5.48xlarge |
| gpu-8xa100 | 768 | **96** | p4d.24xlarge, p4de.24xlarge |
| gpu-1xh100 | 384 | 0 | p5.4xlarge |
| gpu-8xh100 | 768 | 0 | p5.48xlarge, p5en.48xlarge |

Every CE is `type: EC2` — **no SPOT anywhere** in this account. `gpu-4xl40s` (48) and `gpu-8xa100`
(96) have non-zero `desiredvCpus`, i.e. instances are warm/running there right now.

All 16 queues carry identical `jobStateTimeLimitActions`: **CANCEL after 1800s in `RUNNABLE`** for
`CAPACITY:INSUFFICIENT_INSTANCE_CAPACITY`, `MISCONFIGURATION:COMPUTE_ENVIRONMENT_MAX_RESOURCE`, and
`MISCONFIGURATION:JOB_RESOURCE_REQUIREMENT`. A job that cannot be placed in 30 min is killed, not
queued indefinitely.

### Queue occupancy — **idle, we can move immediately**

```
list-jobs --job-queue sbsandbox-intern-edullm-cpu --job-status RUNNING   -> []
list-jobs --job-queue sbsandbox-intern-edullm-cpu --job-status RUNNABLE  -> []
list-jobs --job-queue sbsandbox-intern-edullm-cpu --job-status SUBMITTED -> []
```

CPU CE `desiredvCpus: 0` corroborates. **Full 384 vCPU available**; expect a cold-start scale-up.

### ACTIVE job definitions — latest revision of each `edullm-*`

Timeouts in seconds. Image is always `<ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com/<repo>@<digest>`.

| job def | latest rev | timeout | job role | image repo @ digest (abbrev) |
|---|---|---|---|---|
| **`edullm-validator`** | **16** | **28800 (8h)** | `…-dataset-validator` | `…-edullm-data@sha256:5fb76f66…` |
| `edullm-validator-main` | 1 | 7200 | `…-dataset-validator` | `…-edullm-data@sha256:bf5c0985…` |
| `edullm-validator-preflight` | 7 | 900 | `…-dataset-validator` | `…-edullm-data@sha256:1ada3f2d…` |
| **`edullm-promote`** | **2** | **28800 (8h)** | `…-dataset-validator` | `…-edullm-data@sha256:5fb76f66…` |
| **`edullm-fsck`** | **6** | **3600 (1h)** | `…-dataset-validator` | `…-olmo-core@sha256:4ebdba1b…` |
| **`edullm-dataset-publish`** | **1** | **21600 (6h)** | `role/edullm-dataset-publish` | `…-edullm-data@sha256:055ff803…` |
| `edullm-reservoir-build` | 12 | **64800 (18h)** | `role/edullm-final-dataset-build` | `…-edullm-data@sha256:1ada3f2d…` |
| `edullm-reservoir-build-force` | 1 | 64800 | `role/edullm-reservoir-ingest` | `…-edullm-data@sha256:4be21c0a…` |
| `edullm-reservoir-ingest` | 7 | 7200 | `role/edullm-reservoir-ingest` | `…-olmo-core@sha256:4ebdba1b…` |
| `edullm-reservoir-verify` | 3 | 14400 | `role/edullm-reservoir-ingest` | `…-edullm-data@sha256:352afc50…` |
| `edullm-reservoir-shim` | 1 | 1800 | `role/edullm-reservoir-ingest` | `…-olmo-core@sha256:4ebdba1b…` |
| `edullm-reservoir-diag` | 2 | 1800 | `role/edullm-reservoir-ingest` | `…-olmo-core@sha256:4ebdba1b…` |
| `edullm-reservoir-diag2` | 1 | 1800 | `role/edullm-reservoir-ingest` | `…-olmo-core@sha256:4ebdba1b…` |
| `edullm-prm800k-publish` | 2 | 7200 | `role/edullm-prm800k-producer` | `…-edullm-data@sha256:63a9d45f…` |
| `edullm-prm800k-stage` | 1 | 7200 | `role/edullm-prm800k-producer` | `…-edullm-data@sha256:339c2b6b…` |
| `edullm-prm800k-validate` | 1 | 7200 | `role/edullm-prm800k-validator` | `…-edullm-data@sha256:339c2b6b…` |

There is **no** `edullm-verify` and **no** `edullm-tokenize` job def, and no `edullm-data-*`
job def. (`edullm-dataset-publish` is the closest to a "publish" def; `edullm-reservoir-verify`
to a "verify".)

`edullm-validator:16` full container command:

```
python -m edullm_data.validate --landing-bucket edullm-landing
                              --data-bucket edullm-data --head-workers 16
```

`resourceRequirements: []` on every `edullm-*` def — **vCPU/memory are not set in the job def** and
must be supplied at submit time via `containerOverrides` (or they inherit whatever the def's legacy
`vcpus`/`memory` fields hold). Worth confirming before submitting; a 373 GiB publish with default
sizing will be slow. Contrast `sbsandbox-intern-fpov-reduce` which *does* pin `2 VCPU / 55000 MEMORY`.

`edullm-dataset-publish:1` carries `environment: [{PUBLISH_MODE: "--dry-run"}]` — **it defaults to a
dry run**; a real publish requires overriding that env var.

Revision history is informative for staleness: `edullm-validator` revs 1-8 pointed at the
**olmo-core** image and had `timeout: null`; revs 9-13 moved to the `edullm-data` image at 7200;
rev 14 → 14400; revs 15-16 → 28800. `edullm-fsck` revs 1-4 had `timeout: null`.

---

## 6. ECR

`describe-repositories` — 13 repos. The two that job defs use:

```
<ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com/sbsandbox-intern-edullm-data       (created 2026-07-29)
<ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com/sbsandbox-intern-edullm-olmo-core  (created 2026-07-26)
```

Others: `sbsandbox-intern-edullm-p1`, `-alt-cl`, `-olmo-eval-full`,
`-open-instruct-scored-rewards`, plus unrelated (`gt-web`, `zappi-*`, `memorysplit-aws-gpu`, CDK
assets).

**Image tags are git commit SHAs and they resolve in the local checkout.** Newest 8 in
`sbsandbox-intern-edullm-data`:

| pushed (local time) | tag | digest (abbrev) | size | git commit |
|---|---|---|---|---|
| 2026-08-08 10:01 | `8e2524a6cfba` | `sha256:d56d18c6…` | 141.5 MiB | `8e2524a` "fix(registry): cosmopedia config web_samples_v2 -> data…" |
| 2026-08-08 07:14 | `69667edbb070` | `sha256:1ada3f2d…` | 141.5 MiB | `69667ed` "feat(registry): add _file_shards…" |
| 2026-08-08 04:07 | `5450f538363d` | `sha256:5fb76f66…` | 141.4 MiB | `5450f53` "docs: scrub the AWS account ID…" |
| 2026-08-07 15:02 | `44d4d7d79de3` | `sha256:6485aecd…` | 83.2 MiB | — |
| 2026-08-06 10:10 | `b63e73a02dc6` | `sha256:51d9c6b4…` | 83.2 MiB | — |
| 2026-08-05 18:16 | `3b11d7d3f4e2` | `sha256:352afc50…` | 78.7 MiB | `3b11d7d` "fix(validate): --head-workers 16 against a 10-connection pool caps itself, silently" |
| 2026-08-05 12:56 | `b484814529cf` | `sha256:0d079b3c…` | 78.7 MiB | `b484814` "merge(0.9.0): converge the two lines, because the validator writes the README" |
| 2026-08-05 12:39 | `d8398a25040e` | `sha256:055ff803…` | 78.6 MiB | `d8398a2` "fix(infra): IAM rejects non-ASCII and _comment in a policy document" |

**Which commit is live where:**
- `edullm-validator:16` and `edullm-promote:2` run `sha256:5fb76f66…` = **`5450f53`**, pushed
  2026-08-08 04:07.
- `edullm-validator-preflight:7` and `edullm-reservoir-build:12` run `sha256:1ada3f2d…` =
  **`69667ed`** (08-08 07:14).
- The newest image `sha256:d56d18c6…` = **`8e2524a`** (08-08 10:01) is **not referenced by any
  job def** — built but not wired up.
- `edullm-dataset-publish:1` runs `sha256:055ff803…` = **`d8398a2`** (08-05), i.e. **3 days and
  many commits behind** the validator.

Version at the newest image commit: `git show 8e2524a:src/edullm_data/__init__.py` → **`0.9.1`**.

**Staleness flags:**
1. The local canonical checkout is at `a9c7eab` / `__version__ = "0.5.2"`, which is **137 commits
   behind** `8e2524a`. It *is* an ancestor (`git merge-base --is-ancestor` → yes), so no divergence,
   just lag. Fetch before editing.
2. `_dist/` tops out at wheel **0.7.4** while images are at **0.9.1** — consistent with the known
   `deploy-is-image-push-not-wheel` finding. Do not ship a wheel expecting it to change behaviour;
   the job defs run digest-pinned images.
3. A branch **`agent/claude-20/olmoe-mix-ingest` already exists locally** and contains `8e2524a`,
   as does `final-dataset` and `remotes/origin/edullm/final-dataset-phase0`. Someone has already
   started this task — reconcile before creating a new worktree.

Per this repo's CLAUDE.md, images build **only** from `edullm/**` branches. `agent/**` does not
trigger a build; dispatch explicitly.

---

## 7. EventBridge — auto-promotion confirmed, and how to stop it

`events list-rules` → 11 rules. Three are eduLLM-relevant:

### `edullm-landing-manifest-created` — **ENABLED**, the auto-promote trigger

`describe-rule` event pattern, verbatim:

```json
{"detail-type":["Object Created"],
 "source":["aws.s3"],
 "detail":{"bucket":{"name":["edullm-landing"]},
           "object":{"key":[{"suffix":"manifest.json"}]}}}
```

`list-targets-by-rule`:

```
Id: validator-batch-queue
Arn: arn:aws:batch:us-east-1:<ACCOUNT>:job-queue/sbsandbox-intern-edullm-cpu
RoleArn: arn:aws:iam::<ACCOUNT>:role/CloudWatchSendEventsToVdi
BatchParameters: { JobDefinition: "edullm-validator", JobName: "edullm-validate-on-manifest" }
RetryPolicy: { MaximumRetryAttempts: 2 }
```

**Confirms the memory `publishing-to-landing-auto-promotes`.** Critical properties:

- The match is on **key suffix only** — `{"suffix":"manifest.json"}`. There is **no** prefix or
  dataset-name filter. **Any** `manifest.json` written anywhere in `edullm-landing` fires it,
  including a test or a partial staging write.
- `JobDefinition: "edullm-validator"` is **unversioned** → resolves to the highest active revision,
  **currently 16**. Registering rev 17 cuts over instantly, for better or worse.
- `MaximumRetryAttempts: 2` — a failing validate is retried twice.

**To stop it (all read-only-safe to plan, each is a WRITE so not performed here):**
1. `aws events disable-rule --name edullm-landing-manifest-created` — cleanest pre-emptive stop.
2. Write the manifest **last**, after all payload bytes are in place, so the fire is intentional.
3. Stage under a key whose basename is not exactly `manifest.json` (e.g. `manifest.json.staged`),
   then rename when ready.
4. If it fires unintentionally: `aws batch cancel-job` / `terminate-job` on
   `edullm-validate-on-manifest`. Racy — the job may already be promoting.

Option 1 or 2 is the right control. Note the previously-recorded gap: there is **no "publish but
don't promote" mode** in the tooling.

### `edullm-wu-fsck-nightly` — **ENABLED**, weekly despite the name

`ScheduleExpression: cron(6 9 ? * MON *)` → Mondays 09:06 UTC. Target: queue
`sbsandbox-intern-edullm-cpu`, `JobDefinition: edullm-fsck` (unversioned → rev 6),
`JobName: edullm-wu-fsck-nightly`, `MaximumRetryAttempts: 1`. Gate B decay sweep over
`edullm-data`. The rule description confirms the name is a known misnomer. **Today is Saturday
2026-08-08 — the next fire is Monday 08-10 09:06 UTC**, which will scan whatever we have published
by then, on the same CPU queue.

### `edullm-phase4-event-shape-probe` — **ENABLED**, and self-described as garbage

Description: *"TEMPORARY. Phase 4 probe: capture one raw Batch job state change to settle what the
event carries. **Delete after reading.**"* It is still enabled. **Flagged as stale** — someone
should delete it; harmless but noise.

Also present and eduLLM-adjacent: `sbsandbox-intern-edullm-batch-lifecycle` (Batch job state
changes → lifecycle recorder + notifier) and `sbsandbox-intern-edullm-morning-page`
(`cron(0 13 * * ? *)`). Non-eduLLM: `ForwardToVdi`, `SSMExplorerManagedRule`,
`aws-controltower-ConfigComplianceChangeEventRule`, `mcat-dev-outbox-schedule`,
`sffs-email-dw-export-hourly`, `sffs-test-results-dw-export-hourly`.

---

## 8. Capacity reality check + the timeout question

**CPU capacity right now:** `sbsandbox-intern-edullm-cpu` → **384 max vCPU**, `desiredvCpus 0`,
`c7i.8xlarge` (32 vCPU each → up to **12 concurrent instances**), **EC2 on-demand, no spot**,
`BEST_FIT_PROGRESSIVE`. Queue is empty in all of RUNNING/RUNNABLE/SUBMITTED, so the whole 384 is
ours. This matches the recorded "live CPU queue is 384 vCPU not 128".

**GPU queues are separate** — 15 distinct queues, each with its own CE and its own max vCPU
(96-768). CPU publish work does not compete with them.

**Timeouts — the 60-minute failure is history:**

| job def (latest rev) | attemptDurationSeconds | hours |
|---|---|---|
| `edullm-validator:16` | 28800 | **8** |
| `edullm-promote:2` | 28800 | **8** |
| `edullm-dataset-publish:1` | 21600 | 6 |
| `edullm-reservoir-build:12` | 64800 | **18** |
| `edullm-reservoir-verify:3` | 14400 | 4 |
| `edullm-validator-preflight:7` | 900 | 0.25 |
| `edullm-fsck:6` | 3600 | 1 |

No relevant def sits at 3600 except `edullm-fsck`. The historical 60-min job-def cap that killed the
218-shard/125 GB olmo publish is **gone**; `submit-job --timeout attemptDurationSeconds=…` can still
override per-submission.

### Sizing the two releases (arithmetic on measured bytes)

At 3.92B tokens per dclm shard (14.61 GiB each), whole-shard selection:

| target | dclm shards | actual tokens | bytes |
|---|---|---|---|
| 50B | 12.75 → **13** | 51.0B | **190 GiB** |
| 100B | 25.51 → **26** | 101.9B | **380 GiB** |

Pure-dclm slices land within ~2% of target. **But** a dclm-only release throws away the mix's
character (code/math/papers). If the releases are meant to *mirror the OLMoE mix proportions*, the
14.61 GiB dclm shard granularity is the binding constraint — recorded finding
`shard-granularity-vs-mixture-precision` says shards-per-component sets mixture error, and the
small components have very few shards (wikipedia has **2**, at 1.83B tokens each). Hitting OLMoE's
proportions at 50B means e.g. ~0.09% wikipedia = 45M tokens, which is **4% of a single shard** —
unreachable by whole-shard selection. Either accept distorted proportions, or use the sub-shard
token-budget read path (OLMo-core takes a per-path token budget and reads a file prefix, per
`whole-shard-selection-is-our-limitation`).

**This is a design decision to settle before ingest, not after.** Worth raising with the caller.

Object counts are trivially small (13 and 26 payload objects), so Gate A per-entry HEAD cost is
negligible — nothing like the 40k-object/5.3h scenario. Publishing 380 GiB from Cloudflare into S3
in-region, with `hash_workers`/`copy_workers` set, fits comfortably inside the 8h validator window.
Note the recorded constraint `publish-must-run-in-region`: `publish()` **pulls every byte to
wherever it runs**, so this must run on Batch in us-east-1, never locally.

---

## 9. Flagged as stale or broken

1. **`OLMoE-mix-0924` vs `-0824`** — the tokenized mix OLMo-core knows is **0824**. The brief's
   `0924` names the HF *raw text* release. Do not assume they are interchangeable.
2. **"Hundreds of GB"** — actually 14.17 TiB tokenized / 6.82 TiB raw text. Any plan sized on
   "hundreds of GB" needs revisiting. (A ~190-380 GiB *slice* is hundreds of GB — the confusion is
   probably slice-vs-corpus.)
3. **`ai2-llm.s3.us-west-2.amazonaws.com` is doubly wrong** — the bucket is us-east-1 (proved by
   the `PermanentRedirect` endpoint) and it is closed to anonymous list *and* get.
4. **`_dist/` wheels (max 0.7.4) are behind the live images (0.9.1)**. Shipping a wheel does not
   change job behaviour; the defs are digest-pinned. CLAUDE.md's "bootstraps 0.2.0 by exact
   filename" is stale for the current defs.
5. **Local canonical checkout is 137 commits / `0.5.2` behind `8e2524a` / `0.9.1`.** Fetch first.
6. **`agent/claude-20/olmoe-mix-ingest` already exists** and contains the newest image commit. This
   task may already be underway — reconcile before starting a new worktree.
7. **Newest image `8e2524a` (`sha256:d56d18c6…`) is referenced by no job def** — built, unwired.
8. **`edullm-phase4-event-shape-probe` is still ENABLED** and its own description says
   "TEMPORARY … Delete after reading."
9. **`edullm-wu-fsck-nightly` runs weekly, not nightly** (known, name can't change without dropping
   the target). Next fire Mon 2026-08-10 09:06 UTC.
10. **Every `edullm-*` job def has empty `resourceRequirements`** — vCPU/memory come from submit-time
    overrides or legacy fields. Verify before submitting a 380 GiB publish.
11. **`edullm-dataset-publish:1` defaults to `PUBLISH_MODE=--dry-run`** and runs a 3-day-old image.
12. **Broker says `onboarded: false`, `mfaEnrolled: false`** yet all reads succeed. Writes/submits
    may still be gated — the platform GitHub Actions form is the sanctioned path anyway and needs no
    AWS creds.

## 10. Denied calls

Only these, all anonymous HTTP to AI2 (not AWS), reported verbatim:

- `GET https://ai2-llm.s3.us-west-2.amazonaws.com/?list-type=2&prefix=preprocessed/olmoe-mix-0924/…`
  → `PermanentRedirect` — "The bucket you are attempting to access must be addressed using the
  specified endpoint." `<Endpoint>s3.amazonaws.com</Endpoint>`
- `GET https://ai2-llm.s3.amazonaws.com/?list-type=2&prefix={preprocessed/olmoe-mix-0924/,preprocessed/,olmoe-mix-0924/}`
  → `AccessDenied` — "Access Denied" (RequestIds 6FVWM7RS9J8BNTBZ, JQ141CBT9Z3ZBV3B, JQ1CWWHXKV7MABX8)
- `HEAD https://ai2-llm.s3.amazonaws.com/preprocessed/dclm/…/part-000-00000.npy` → `403` (all three
  tokenizer variants)
- `GET https://olmo-data.org/?list-type=2&…` → `404` Cloudflare "Object not found … or is not
  publicly accessible" (listing disabled; object GET works)
- HF API `allenai/OLMoE-mix-0824` → non-JSON/404 (dataset does not exist)

**No AWS call was denied.** Every `mcp__sb-aws__aws` invocation returned `exitCode: 0`. Note the
transient trap: `python3 urllib` against `olmo-data.org` gets Cloudflare-`403`d on default UA —
setting `User-Agent: curl/8.7.1` fixed it (1,122/1,122 HEADs, zero errors). Not a permission issue.
