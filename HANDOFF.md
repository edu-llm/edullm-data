# HANDOFF — eduLLM Dataset Standard

Last updated: 2026-07-29 (post olmo30b migration + public release + per-dataset generated READMEs,
MERGED to main as `afac933` via PR #1; then the `v0.2.0` release bump — see Next Step #6). Author:
prior agent. Read this file alone and you can continue with no other context.

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

## Current Progress — BUILT, DEPLOYED, PROVEN AUTOMATIC, PUBLIC, and carrying REAL DATA

The full pipeline is proven end-to-end on live AWS with real data, including fully-automatic
event-triggered validation. The repo is now **public at
`https://github.com/edu-llm/edullm-data`** (tag `v0.1.0`). The first real migration is DONE:
two datasets are live, validated, promoted, and READABLE in `edullm-data`.

**Two datasets now in `edullm-data` (migrated 2026-07-29 from `s3://edullm-datasets/olmo30b/`):**
- `tokenizer/dolma2-bpe/v1` — the real `allenai/dolma2-tokenizer` (tokenizer.json + merges.txt +
  vocab.json + configs). `vocab_size 100278` & `eos 100257` DERIVED from tokenizer.json.
- `pretrain/olmo-mix-1124-31b/v1` — 31,334,000,834 dolma2 tokens, 218 shards, 125.336 GB,
  uint32 headerless `.u32le.bin`, `depends_on tokenizer/dolma2-bpe`. Both have `_VALIDATED.json`
  + `_catalog/` entries; `read.dataset_paths()` returns them with dtype uint32.
- **Both now carry a generated `README.md`** at their prefix, plus enriched descriptive metadata
  in `dataset.json`. The olmo corpus has the real OLMo-mix-1124 data-mix table (DCLM-Baseline,
  starcoder, pes2o, arxiv, open-web-math, algebraic-stack, wiki — upstream token/doc counts,
  per-source licenses) with an explicit "these are upstream-collection figures, not this subset's
  measured mix" caveat, an `about` block, and `license {id: ODC-By-1.0, basis: declared}`. The
  tokenizer README keeps license `unknown` (upstream terms unverified — not fabricated). These were
  added by an **in-place backfill** that changed ONLY descriptive keys — every group's
  `manifest_sha256`, the `depends_on` pin, and `inventory` are byte-identical to pre-backfill
  (verified), so the frozen contract on the 218 shards holds and no payload byte was re-copied.
- Legacy `s3://edullm-datasets/olmo30b/` left fully intact (greenfield + provenance). The legacy
  `.npy` shards were headerless-raw-uint32 (the ".npy lie"), so migration was a pure server-side
  rename `.npy → .u32le.bin` + publish — zero re-encode, zero bytes through the laptop.

**Code** (this repo, `edullm-data/`, its own git root; **380 tests passing**):
- `src/edullm_data/contracts.py` — canonical JSON, hashing, naming/purpose validation (7-family enum)
- `src/edullm_data/manifest.py` — per-file format, manifest build/verify, arithmetic + extension checks
- `src/edullm_data/s3.py` — `S3` protocol, `Boto3S3` (real), `FakeS3` (tests). Now also
  `hash_object` (streaming), `put_file` (streaming upload), `delete`.
- `src/edullm_data/validate.py` — Gate A orchestrator + `promote()` + `discover_pending()` + CLI;
  resolves a corpus's tokenizer dependency and injects derived vocab into `GroupContext.resolved`.
  `promote()` now also renders + writes a `README.md` into edullm-data (before the `_VALIDATED` seal)
- `src/edullm_data/publish.py` — producer `publish()`; **never holds a payload whole** (stream-hash +
  server-side copy, TB-scale safe); `tokenizer=` per-dataset arg; version auto-alloc; family
  inheritance; optional `sources`/`about`/`notes`/`limitations`/`license` descriptive fields (feed the README)
- `src/edullm_data/readme.py` — **NEW** pure `render_readme(dataset.json) -> markdown`. The README is a
  DERIVED artifact (§3): one source of truth, can't drift from the manifest. Omits any section whose
  data is absent (never fabricates); prints an upstream-scope caveat for `sources[].scope == "upstream…"`
- `src/edullm_data/read.py` — `dataset_paths()` (returns correct dtype), `resolve_latest()`
- `src/edullm_data/fsck.py` — `wu-fsck` Gate B (post-publish decay sweep), owner Eric Wu
- `src/edullm_data/profiles/` — registry + **5** v1 profiles: pretrain-tokens, eval-results,
  token-order, sft-conversations, **tokenizer** (`tokenizer_v1.derive_vocab` computes vocab from tokenizer.json)
- `families/*.json` — **7** families: pretrain, curriculum, sft, eval, probe, vendor, tokenizer
- `infra/` — CloudFormation, policies, Dockerfile.validator, DEPLOY.md, 05-validator-jobdef.md
- `skill/SKILL.md` — copy of the agent skill (canonical copy at `../.claude/skills/edullm-datasets/`).
  Both copies now instruct writing a generated README for EVERY dataset, incl. already-promoted ones.
- `USAGE.md` — human how-to. Install line (all docs): `uv add "edullm-data @
  git+https://github.com/edu-llm/edullm-data@v0.1.0"` (public repo, no auth).
- `docs/ONBOARDING.md` — **NEW** 2-minute, paste-friendly intro to the pipeline for a teammate who
  has never worked on it (the airlock, bucket layout, the address shape, what the validator forces).

**Git commits — branch `main`, PUSHED to `origin` (github.com/edu-llm/edullm-data), newest last:**
- `f177e19`…`b69e3be` — original build (core, airlock infra, publish/read/fsck, skill, streaming
  publish, per-dataset tokenizer) + `226eb42` HANDOFF refresh.
- `60f53b3` — `.txt`→`text` container (so a tokenizer's merges.txt publishes)
- `8b8e63f` — parallelize `publish()` hash+copy (`hash_workers`/`copy_workers`, default 1)
- `b988d1f` — `promote()` writes `_VALIDATED.json` INTO edullm-data (the readability seal)
- `da7f88e` — scrub internal AWS ids → `<PLACEHOLDER>` for public release
- `3b38288` — real `git+https@v0.1.0` install URLs. **Tag `v0.1.0` pushed** (the install pin).
- `afac933` — **per-dataset generated README feature (PR #1, squash-merged to `main`)**: `readme.py`
  (`render_readme`), `promote()` writes `README.md`, `publish()` gains `sources`/`about`/`notes`/
  `limitations`/`license`, `README.md` a control file in both publish + Gate A, +15 tests
  (**380 passing**), + `docs/ONBOARDING.md` (2-min pipeline intro for a newcomer). Branch
  `feat/per-dataset-readme` merged + deleted; local `main` == `origin/main` == `afac933`, tree clean.
  NOTE: no new `v0.x` tag was cut for this — `v0.1.0` still points at `3b38288`. The `_dist` wheel was
  already rebuilt from this code, so git now matches what's deployed.

Working tree is CLEAN — nothing uncommitted as of this handoff.

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

**From the olmo30b migration (first real `publish` on Batch — surfaced 4 issues, all fixed):**
- **`publish()` couldn't find `families/` on Batch** — `FAMILIES_DIR` is repo-root-relative but the
  wheel packages only `src/edullm_data`. Fix: `aws s3 cp families/ s3://edullm-landing/_dist/families/`
  + a tiny driver that sets `P.FAMILIES_DIR=/tmp/families` before calling publish. (Proper fix TODO:
  package families into the wheel.) The Batch driver lives at `_dist/publish_driver.py`.
- **`.txt` had no format** — a tokenizer's `merges.txt` hit `cannot determine format` (`.txt` not in
  EXTENSION_FORMAT, tokenizer family has no format default). Fix `60f53b3`: added `.txt`→`text`
  container (no dtype, so arithmetic never applies).
- **125 GB publish TIMED OUT at Batch's 60-min `attemptDurationSeconds`** — `publish()` stream-hashed
  then server-side-copied 218 shards STRICTLY SEQUENTIALLY single-threaded (~48 MB/s), 31 of 32 vCPUs
  idle. Fix `8b8e63f`: `hash_workers`/`copy_workers` ThreadPoolExecutor fan-out (order-preserving →
  byte-identical manifest; default 1). Driver passes 16. ALSO pass `--timeout attemptDurationSeconds=7200`
  on submit-job to override the 60-min job-def default.
- **Promoted datasets were UNREADABLE** — `promote()` wrote `_VALIDATED.json` only to LANDING, but
  `read.dataset_paths()` requires it in edullm-data (and landing's copy expires in 14d). The tests had
  papered over this by manually seeding the marker. Fix `b988d1f`: `promote()` writes a durable
  `_VALIDATED.json` seal into edullm-data, last. Backfill for already-promoted datasets: a MARKER-ONLY
  Batch job that reads the promoted dataset.json and `s3.put`s the seal — a full re-`promote()` wastes
  ~20 min re-copying 218 shards to write one file.

**From the README feature (this session):**
- **The renamed-wheel gotcha bit again** — the in-place verify Batch job downloaded the wheel to
  `/tmp/w.whl` and `pip install /tmp/w.whl` failed exit 1 (`w.whl is not a valid wheel filename`). pip
  rejects any non-PEP-427 wheel filename. Fix: keep the real filename
  (`edullm_data-0.1.0-py3-none-any.whl`) end-to-end. NOT a validation failure — the datasets were fine;
  the harness just never ran. Resubmitted with the correct filename → clean pass. (Same lesson as the
  first-ever validator run; it's in the runbook but easy to re-trip in an ad-hoc driver.)
- **`gh pr merge` is blocked by the auto-mode permission classifier** by default — it is NOT in the
  allowed Bash set, and the block cannot (and must not) be worked around with `gh api` / a direct push
  to `main` (same action). It needs the user to grant a Bash permission rule for it (they did, via
  `/permissions`), after which the squash-merge + delete-branch worked. `gh pr create` and `gh pr view`
  are allowed; only the merge is gated.

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
- **README is a GENERATED, DERIVED artifact + a CONTROL file** — not hand-written, so it can't drift
  from the manifest (STANDARD §3). `readme.py:render_readme(dataset.json)` renders markdown;
  `promote()` writes it for EVERY promotion, before the `_VALIDATED` seal; `render_readme` is
  best-effort (a render bug never fails an otherwise-valid promotion). `README.md` is in
  `CONTROL_BASENAMES` (publish + validate) so it is never a manifest entry and never flagged "extra" —
  which is exactly what lets it be backfilled into a frozen dataset in place without touching a
  manifest hash. Sections omit when their data is absent (never fabricate); `sources[].scope ==
  "upstream…"` prints a caveat so upstream-collection figures are never shown as this dataset's
  measured mix. Descriptive content comes from optional `publish()` args
  (`sources`/`about`/`notes`/`limitations`/`license`); none is validator-required.

## Next Steps (priority order)

DONE this session: first tokenizer published (`tokenizer/dolma2-bpe/v1`); first pretrain corpus
migrated + published + promoted + readable (`pretrain/olmo-mix-1124-31b/v1`, 31.334B tokens); repo
pushed public with `v0.1.0` tag + real install URLs. The pipeline is proven with real data end to end.

DONE (per-dataset README, this session): added `readme.py` (`render_readme`), wired `promote()` to
write a generated `README.md` into edullm-data for EVERY promotion, extended `publish()` with
`sources`/`about`/`notes`/`limitations`/`license`, made `README.md` a control file in both publish
and Gate A, and **backfilled the two live datasets in place** (README + enriched data-mix metadata,
descriptive-keys-only, manifests/inventory byte-identical). 380 tests pass. Rebuilt wheel (77.9 KB,
now includes `readme.py`) + shipped to `_dist/`. Verified: intern PutObject to edullm-data still
AccessDenied (airlock intact); Gate A re-run in place against the enriched datasets = clean pass.
This retires old Next-Step #3 (license.basis) for the olmo corpus — now `ODC-By-1.0`/`declared`.
The README backfill driver + guardrails live at `$CLAUDE_JOB_DIR/tmp/driver/backfill_readme.py`
(also mirrored to `s3://edullm-landing/_dist/backfill_readme.py`); enrichment content in
`.../driver/enrich.json`; read-only in-place verifier at `.../driver/verify_inplace.py`.

**COMMITTED + MERGED**: the README feature (+ `docs/ONBOARDING.md`) shipped via PR #1,
squash-merged to `main` as `afac933` and the `feat/per-dataset-readme` branch deleted. Working tree
is clean; local `main` == `origin/main`. The two live datasets were verified by a read-only Gate A
re-run in place (job `e72522a4…`, SUCCEEDED): both `ok=True, violations=0`, READMEs present. Nothing
outstanding to commit for this feature.

1. **Package `families/` INTO the wheel** (drops the `_dist/families` + `FAMILIES_DIR` override the
   Batch publisher currently needs). Either move `families/` under `src/edullm_data/families/` +
   `importlib.resources`, or add `[tool.hatch.build.targets.wheel.force-include]`. Then rebuild the
   wheel + `aws s3 cp` to `_dist/` (same filename) and simplify `_dist/publish_driver.py`.
2. **Add per-shard progress logging to `publish()` / the driver.** The ~8-min silent hash of 125 GB
   looked exactly like a hang (I had to probe S3 object counts to tell progress from stall). Emit a
   line every N shards from `build_plan`'s hash loop and the copy loop.
3. ~~Set the corpus's real `license.basis`.~~ **DONE for `pretrain/olmo-mix-1124-31b`** (now
   `{id: ODC-By-1.0, basis: declared}`, set via the README backfill). The tokenizer's license is
   still an honest `unknown` — set it if/when the upstream dolma2-tokenizer terms are confirmed.
   Pattern for future datasets: pass `license=` (and `sources=`/`about=`) to `publish()` at publish
   time so it lands in `dataset.json` and the generated README from the start.
4. **(Optional) Parallelize `promote()`'s copy loop** like `publish()` — it's still sequential
   per-shard (~7/min), which is why promotion of the 218-shard corpus took ~30 min. Fine at this scale;
   revisit if promotion latency matters. The validator's Gate A reads are single-threaded too but
   I/O-light (~64 KB range-read per shard), so those are fine as-is.
5. **(Optional, better steady state) Bake the validator container image (Path A).** Docker + ECR push:
   `infra/Dockerfile.validator` → new ECR repo → re-register `edullm-validator` + `edullm-fsck` job
   defs at the image (drops the ~30-60s pip-install per run). `infra/05-validator-jobdef.md`. Not
   blocking — wheel bootstrap works.
6. **`v0.2.0` release — DONE in git, wheel reship STILL OUTSTANDING (deployment lag).** The version
   was bumped to `0.2.0` (`pyproject.toml`, `src/edullm_data/__init__.py`) and every **team-facing**
   install pin updated to `@v0.2.0` (`README.md` — also fixed its stale "no tag exists" line —,
   `USAGE.md`, `skill/SKILL.md`, `.claude/skills/edullm-datasets/SKILL.md`). Shipped via branch
   `release/v0.2.0` → PR. **Tag `v0.2.0` is cut on `main` AFTER the PR merges** (the merge is
   permission-gated; do it once the PR is approved). 380 tests pass. `v0.1.0` still points at the
   pre-README commit `10c18fb`, which is why the pin was stale.
   **NOT yet done, needs a broker/creds session (this session had neither `sb-aws` nor local AWS
   creds, so it could not write S3):**
   - Two pin sites were deliberately LEFT at `0.1.0` because they describe the *deployed* artifact,
     not what the team installs: `infra/05-validator-jobdef.md` and `infra/DEPLOY.md` (the git+https
     line + the `_dist` wheel filename + the ECR tag), and the `CLAUDE.md` gotcha #3 wheel filename.
     The live Batch validator bootstraps `s3://edullm-landing/_dist/edullm_data-0.1.0-py3-none-any.whl`
     by exact filename — so bumping those docs to `0.2.0` without reshipping would break the bootstrap.
   - **Reship steps (run in a broker session):** `python3 -m pip wheel . --no-deps` →
     `edullm_data-0.2.0-py3-none-any.whl`; `aws s3 cp` it to `s3://edullm-landing/_dist/`; update the
     hardcoded `0.1.0` filename in `_dist/publish_driver.py` and the validator/fsck bootstrap command
     (`infra/05-validator-jobdef.md:95`) to `0.2.0` (or ship both wheels and cut over deliberately);
     then update the two infra docs + the `CLAUDE.md` gotcha to `0.2.0`. Next validator/fsck run picks
     up the new wheel. Consider `gh release create v0.2.0` if a formal Release page is wanted (only a
     lightweight tag exists).
7. **Migrate more high-value legacy datasets** using the proven playbook: server-side rename any
   headerless `.npy`→`.u32le.bin` into `s3://edullm-landing/_migrate/<name>/`, then run the Batch
   publish driver with `PUB_HASH_WORKERS`/`PUB_COPY_WORKERS=16`. (Verify each shard is headerless first:
   first bytes ≠ `\x93NUMPY` and `tokens×dtype_size == bytes`.)

## How to operate it (quick reference)

- **Publish**: `from edullm_data.publish import publish` — args (source, dataset_id, purpose,
  profile) + `tokenizer="tokenizer/<name>"` for a pretrain corpus + optional group_meta. See `USAGE.md`.
- **Read**: `from edullm_data.read import dataset_paths, resolve_latest`.
- **Discover what's published**: list `s3://edullm-data/_catalog/` (now has `tokenizer/dolma2-bpe/v1`
  + `pretrain/olmo-mix-1124-31b/v1`).
- **Migrate a legacy corpus (proven playbook)**: broker-copy headerless `.npy`→`.u32le.bin` into
  `s3://edullm-landing/_migrate/<name>/tokens/`, ship wheel+driver+families to `_dist/`, then Batch
  submit `_dist/publish_driver.py` via the boto3 bootstrap with `PUB_*` env (incl. `PUB_HASH_WORKERS`/
  `PUB_COPY_WORKERS=16`) and `--timeout attemptDurationSeconds=7200`. EventBridge auto-validates+promotes.
- **All AWS access in this project goes through the `sb-aws` MCP broker** (read-only default; the
  intern session CANNOT write `edullm-data` by design — that's the airlock working). publish/validate/
  promote run on AWS Batch as the validator role (which can't read the legacy `edullm-datasets` bucket
  — so legacy→landing rename-copies must be broker-driven, not Batch-driven).
- **Durable AWS memory note**: `../.claude/.../memory/dataset-standard-airlock.md` +
  `publish-on-batch-needs-families.md` have the full live-resource inventory and every hard-won fact.
