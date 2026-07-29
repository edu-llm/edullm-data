# HANDOFF — eduLLM Dataset Standard

Last updated: 2026-07-29. Author: prior agent. Read this file alone and you can
continue with no other context.

---

## Goal

Replace ad-hoc S3 dataset sprawl with **one enforced way to create, store, read, and discover
datasets** for the eduLLM project. The end state: an engineer or agent runs `publish(...)`, and
a validated dataset appears in the official bucket automatically, with no human in the loop and
no way to write bad or unvalidated data into the read path.

Motivating audit: `../docs/dataset-creation/s3-dataset-audit-2026-07-28.md` (23 buckets,
~2.53 TB, sprawl + broken metadata). Full spec: `../docs/dataset-creation/DATASET-STANDARD.md`
+ `...-DIAGRAMS.md`.

---

## Current Progress — the system is BUILT, DEPLOYED, and PROVEN AUTOMATIC

Everything through the standard's §13 build order is done except two clearly-scoped items
(baked container image; publish real tokenizers). The full pipeline has been proven end-to-end on
live AWS with real data, INCLUDING fully-automatic event-triggered validation.

**Code** (this repo, `edullm-data/`, its own git root; **363 tests passing**):
- `src/edullm_data/contracts.py` — canonical JSON, hashing, naming/purpose validation (7-family enum)
- `src/edullm_data/manifest.py` — per-file format, manifest build/verify, arithmetic + extension checks
- `src/edullm_data/s3.py` — `S3` protocol, `Boto3S3` (real), `FakeS3` (tests). Now also
  `hash_object` (streaming), `put_file` (streaming upload), `delete`.
- `src/edullm_data/validate.py` — Gate A orchestrator + `promote()` + `discover_pending()` + CLI;
  resolves a corpus's tokenizer dependency and injects derived vocab into `GroupContext.resolved`
- `src/edullm_data/publish.py` — producer `publish()`; **never holds a payload whole** (stream-hash +
  server-side copy, TB-scale safe); `tokenizer=` per-dataset arg; version auto-alloc; family inheritance
- `src/edullm_data/read.py` — `dataset_paths()` (returns correct dtype), `resolve_latest()`
- `src/edullm_data/fsck.py` — `wu-fsck` Gate B (post-publish decay sweep), owner Eric Wu
- `src/edullm_data/profiles/` — registry + **5** v1 profiles: pretrain-tokens, eval-results,
  token-order, sft-conversations, **tokenizer** (`tokenizer_v1.derive_vocab` computes vocab from tokenizer.json)
- `families/*.json` — **7** families: pretrain, curriculum, sft, eval, probe, vendor, tokenizer
- `infra/` — CloudFormation, policies, Dockerfile.validator, DEPLOY.md, 05-validator-jobdef.md
- `skill/SKILL.md` — copy of the agent skill (canonical copy at `../.claude/skills/edullm-datasets/`)
- `USAGE.md` — human how-to

**Git commits (this repo, branch `main`, nothing pushed — no remote yet), newest last:**
- `f177e19` steps 1–4 (core + airlock infra) · `a8844b1` steps 5–11 (publish/read/fsck + deploys)
- `30f26a5` skill + usage · `cdfb6b3` skill hard-prereq + fsck schedule notes
- `85236c7` HANDOFF · `8d8f9e6` streaming publish (no local bytes) · `4b7a163` tokenizer artifact
- `58b590e` per-dataset tokenizer · `b69e3be` HANDOFF per-dataset tokenizer

**Deployed live in AWS account `sbsandbox` (<ACCOUNT_ID>), us-east-1** (NOT in git — broker-applied):
- Buckets: `edullm-landing` (write-anything, expiry) + `edullm-data` (read-only; validator writes only)
  — CFN stacks `edullm-data-buckets`, `edullm-data-event-wiring` both CREATE_COMPLETE
- `edullm-data` bucket policy: 2 statements — `OnlyValidatorWrites` Deny (with
  `BoolIfExists aws:PrincipalIsAWSService=false`) + `AllowS3InventoryDelivery`
- Validator identity: EXISTING role `<BATCH_JOB_ROLE>` (ecs-tasks-only trust),
  inline policy `dataset-validator` (S3 rw scoped to the two buckets)
- Batch job defs: `edullm-validator:1` (self-discovering validate+promote), `edullm-fsck:1`
- **Event rule `edullm-landing-manifest-created` — ENABLED**: manifest.json upload → validate+promote,
  RoleArn `<EVENTBRIDGE_INVOKE_ROLE>` + its inline `edullm-validator-submit` (SubmitJob+PassRole)
- **Schedule rule `edullm-wu-fsck-nightly` — ENABLED**: `cron(6 9 * * ? *)` UTC (04:06 local) → fsck
- S3 Inventory (weekly) on `edullm-data`; landing lifecycle scoped to family prefixes (keeps `_dist/`)
- `s3://edullm-landing/_dist/edullm_data-0.1.0-py3-none-any.whl` — the durable bootstrap wheel

**PROVEN end-to-end on live AWS (all cleaned up + re-locked after):**
1. Deny side: intern session PutObject to `edullm-data` → AccessDenied (repeatedly re-verified)
2. Allow side: Batch job as validator role promoted real bytes into `edullm-data`
3. Real `validate.py` Gate A ran on Batch, validated + promoted a `publish()`-produced dataset
4. **Fully automatic**: manifest upload → EventBridge → Batch → `edullm-validate-on-manifest` job
   SUCCEEDED → "PASS + promoted", zero human steps
5. `wu-fsck` runs cleanly on Batch (clean JSON report, exit 0)

**Official bucket contents RIGHT NOW: `edullm-data` is EMPTY (0 objects).** No real dataset has
been published yet — only test probes, all cleaned up. This is the correct expected state.

---

## What Worked

- **The airlock model** (two buckets, IAM Deny on the read bucket) — enforcement that can't be
  routed around, unlike the previous written-policy-only approach that was 100% ignored.
- **Reusing the existing `<BATCH_JOB_ROLE>` role** instead of creating one
  (`iam:CreateRole` is boundary-denied; `iam:PutRolePolicy` is allowed).
- **Wheel-from-S3 bootstrap (Path B)** to run the validator without a Docker host: Batch job
  `pip install boto3 numpy` → boto3-download the wheel from `_dist/` → `pip install` it → run.
- **Self-discovering validator** (`validate.py --promote`, no `--prefix`): scans landing for
  sealed-but-unvalidated datasets, so the EventBridge event is a pure "wake up" with no payload —
  dissolves EventBridge's inability to pass the object key to a Batch target.
- **FakeS3** — the entire validator/publish/read/fsck suite is testable with zero AWS.
- **Building load-bearing code in the main thread** — subagents kept stalling on rate limits
  mid-inference; the main thread caught real integration bugs via smoke tests before writing tests.
- **Testing live, not just asserting** — every deploy step was proven by exercising it; this is how
  the invocation-role gap (below) was caught.

## What Didn't Work (and the fix)

- **Subagents for the orchestrator** — stalled twice on rate limits (transcript frozen >10 min,
  ending on a tool_result with no assistant turn). Signature to watch for. Fix: build in main thread.
- **`iam:simulate-principal-policy` for the intern role** — LIED about 11 actions that actually work
  (CreateBucket, PutBucketPolicy, PutRule, SubmitJob, RegisterJobDefinition, ECR, …). **Never trust
  it; smoke-test instead.**
- **CloudFormation rejects `NotificationConfiguration:{EventBridgeConfiguration:{}}`** at validate
  time, though the raw `s3api put-bucket-notification-configuration` accepts it. Fix: apply it
  out-of-band via the API (DEPLOY.md step 1b).
- **The minimal Batch image has no `aws` CLI and no boto3** — first validator runs failed 127
  (`aws: not found`) then 1 (`w.whl is not a valid wheel filename`). Fix: use boto3 to download,
  keep the PEP-427 wheel filename, `pip install boto3 numpy` first.
- **Event rule fired but didn't invoke** — `<EVENTBRIDGE_INVOKE_ROLE>` trusts events.amazonaws.com
  (so PutTargets accepted it) but had NO `batch:SubmitJob` (only events:PutEvents cross-account).
  `TriggeredRules=1, FailedInvocations=1`. **PutTargets FailedEntryCount:0 does NOT prove
  invocability.** Fix: added inline `edullm-validator-submit` (SubmitJob + PassRole).
- **My own TZ mistake** — queried CloudWatch in local time treating it as UTC (this box is CDT,
  UTC−5), saw empty metrics, nearly misdiagnosed "rule never fired." **Convert to UTC for
  CloudWatch/cron math.**
- **S3 lifecycle rules are ADDITIVE, not override** — a bare-`Prefix:""` expiry rule still matched
  `_dist/` and would have deleted the bootstrap wheel in 14 days. Fix: explicit per-family-prefix
  expiry rules, leaving `_dist/` untouched.

## Key Decisions

- **SSE-S3 (AES256), not SSE-KMS** — decided, not placeholder. KMS's second auth system can make an
  intact bucket unreadable; no PII in scope, so KMS's revocation/audit buys nothing here.
- **No Object Lock** — protects a version not a path, blocks lifecycle, irreversible. Immutability
  = create-only writes + versioning + deny-delete.
- **One bucket for data, lifecycle class as a field** (not bucket-per-class) — else promotion changes
  URIs and invalidates the hashes that gate promotion.
- **`.u32le.bin` never `.npy`** for packed tokens — OLMo-core memmaps from byte 0; a real .npy header
  corrupts tokens + the size-derived count. dtype is declared+read, never inferred (default is uint16,
  corpora are uint32).
- **No `-of-N` in shard names** — unknowable at write time; completeness via manifest path-set equality.
- **Profile on the GROUP, not the dataset** — one dataset can hold multiple typed payload groups.
- **Validators RECOMPUTE, never just assert a field is present** — the only check that ever rejected
  bad work in the audit recomputed a hash. This is the golden rule (CONTRIBUTING.md).
- **`experimental/v1` is quota-limited (2 live per family), not approval-gated** — approvals erode.
- **Greenfield** — legacy ~2.53 TB is NOT migrated; new datasets only.
- **No dataset byte is ever managed locally.** `publish()` stream-hashes (never loads a payload
  whole), counts tokens as `size // dtype_size` (zero reads), stages local sources to landing then
  moves everything by server-side `s3.copy`. Built for TB-scale migration sources.
- **Tokenizer is a PUBLISHED artifact, named PER DATASET** — not an HF reference, not a family
  default. There is no single canonical tokenizer; each corpus passes `tokenizer="tokenizer/<name>"`.
  The validator DERIVES vocab_size/eos from the published `tokenizer.json` and rejects a corpus with
  no resolvable tokenizer. A family-wide default is off by design (a wrong one passes silently because
  vocab sizes are all ~100k, so mismatched ids usually still fall in range).

## Next Steps (priority order)

1. **Publish the tokenizer, then wire the family dependency, before the first REAL pretrain dataset.**
   The tokenizer is now a first-class PUBLISHED artifact (profile `tokenizer/v1`, family `tokenizer/`),
   not a hand-typed pin — the validator DERIVES vocab_size/eos_token_id from the published
   `tokenizer.json` (`tokenizer_v1.derive_vocab`) and a `pretrain-tokens` corpus `depends_on` it.
   TOKENIZERS ARE PER-DATASET — there is no single canonical one. To finish: (a) publish each real
   tokenizer once, e.g. `publish(tok_dir, dataset_id="tokenizer/dolma2-bpe", profile="tokenizer/v1", …)`;
   (b) when publishing a pretrain corpus, NAME its tokenizer: `publish(..., tokenizer="tokenizer/dolma2-bpe")`.
   The validator derives vocab_size/eos from that tokenizer's tokenizer.json and rejects a corpus with no
   resolvable tokenizer. Do NOT set a family-wide default (`tokenizer_dependency_optional` is off by
   design; a wrong default passes silently). `curriculum/` inherits the tokenizer transitively through
   its parent pool. The old inline `vocab_size`/`revision`/`fingerprint_sha256` pins are GONE.
2. **DONE — repo pushed public + install URLs updated.** Remote is
   `https://github.com/edu-llm/edullm-data` (public; internal AWS ids scrubbed to placeholders in
   commit da7f88e). Install lines in `README.md`, `USAGE.md`, `skill/SKILL.md`,
   `../.claude/skills/edullm-datasets/SKILL.md`, and the infra docs now read
   `git+https://github.com/edu-llm/edullm-data@v0.1.0`. Requires the `v0.1.0` git tag to exist
   (create it if a fresh clone can't resolve the pin).
3. **(Optional, better steady state) Bake the validator container image (Path A).** On a machine with
   Docker + ECR push: `infra/Dockerfile.validator` → push to a new ECR repo → re-register
   `edullm-validator` + `edullm-fsck` job defs pointing at the image (drops the ~30-60s pip-install per
   run). Details in `infra/05-validator-jobdef.md`. Not blocking — the wheel bootstrap works today.
4. **When the wheel changes**, rebuild it (`python -m pip wheel . --no-deps`) and
   `aws s3 cp` it to `s3://edullm-landing/_dist/edullm_data-0.1.0-py3-none-any.whl` (same filename).
   Next validator/fsck run picks it up. (Bump the version + filename for a real release.)
5. **Publish the first real dataset** to exercise the live pipeline for actual work; then watch
   `edullm-data/_catalog/` populate and the nightly fsck report on it.
6. **Consider migrating high-value legacy datasets** into the standard if desired (out of original
   scope) — e.g. the tokenized corpora in `edullm-datasets` / `edullm-checkpoints` from the audit.

## How to operate it (quick reference)

- **Publish**: `from edullm_data.publish import publish` — args (source, dataset_id, purpose,
  profile) + `tokenizer="tokenizer/<name>"` for a pretrain corpus + optional group_meta. See `USAGE.md`.
- **Read**: `from edullm_data.read import dataset_paths, resolve_latest`.
- **Discover what's published**: list `s3://edullm-data/_catalog/`.
- **All AWS access in this project goes through the `sb-aws` MCP broker** (read-only default; the
  intern session CANNOT write `edullm-data` by design — that's the airlock working).
- **Durable AWS memory note**: `../.claude/.../memory/dataset-standard-airlock.md` has the full
  live-resource inventory and every hard-won fact.
