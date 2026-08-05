# Step: the validator job definition + container image

> ## ✅ STATUS: DONE. THIS RUNBOOK IS HISTORY, NOT INSTRUCTIONS. (2026-08-01)
>
> **Everything below describes a problem that was solved several versions ago.** It is the origin
> of the `0.2.0` / "revision 2" fossil that spread into `CLAUDE.md` and elsewhere, so it is kept —
> but do not follow it, and do not cite its version numbers.
>
> Live state, verified by `batch describe-job-definitions --status ACTIVE`:
>
> | job def | top ACTIVE rev | how code gets in | timeout |
> |---|---|---|---|
> | `edullm-validator` | **12** (was 10 when this banner was written, 2026-08-01) | **image, digest-pinned (`…@sha256:a71e62f4…`, tag `e0984c88b7c5` = 0.8.0) — no wheel, no bootstrap** | 7200 s |
> | `edullm-fsck` | **6** | wheel `0.6.0` | 3600 s |
> | `edullm-reservoir-ingest` | **7** | wheel `0.6.3` | 7200 s |
>
> **Do not hardcode a validator revision anywhere.** Both EventBridge rules target the job def by
> *unversioned* name, so the automatic path always runs the top ACTIVE rev while a hand-written
> submission naming `:10` runs an older image. Rev 10's image (`…@sha256:339c2b6b…`) is tagged
> `prm800k-codebuild-20260731T193909Z-d732af0e67fe`, a CodeBuild commit **not in this repo at all**.
> Re-read the live revision before submitting; that is why this row now carries its own date.
>
> - **"BLOCKED on one external step"** — not blocked. The validator has been running in-cluster
>   for days and has promoted live corpora. The stated blocker (no Docker host, no git remote to
>   pip from) is doubly dead: the repo is **public**, and a concurrent session did the ECR path
>   anyway, which is what rev 9/10 is.
> - **"352 tests"** — the suite is **786**.
> - **`0.2.0` everywhere below** — the package is at **0.6.3** (tags `v0.6.0`…`v0.6.3`).
> - **"The live job defs are at revision 2"** (line ~98) — flatly false, and the single most-copied
>   error in this repo's docs. The chain went rev 6 (`0.5.1`) → 7 (first with a 7200 s timeout) →
>   8 (`0.6.0`) → **9/10 (digest-pinned image, no wheel)**.
> - **"Recommendation: do Path B now, move to Path A later"** — **Path A was done.** Rev 9/10 bakes
>   code into a digest-pinned image, which is better provenance than a wheel in `_dist/`: the digest
>   pins the whole dependency tree, and `_dist/` has **no lifecycle expiration**, so a wheel sitting
>   there is mutable-by-overwrite indefinitely.
> - ⚠️ ~~**Auto-promotion is currently DISABLED**~~ **WRONG as of 2026-08-05 — it is `ENABLED`.**
>   `describe-rule edullm-landing-manifest-created` returns `State: ENABLED`, matching **any**
>   `edullm-landing` key with suffix `manifest.json` (no prefix constraint) and submitting
>   `edullm-validator` by *unversioned* name. So landing a `manifest.json` fires Gate A and, on a
>   pass, promotion into `edullm-data` — where frozen means frozen. The old "fires nothing" claim
>   spread from here into `artifacts/reservoir/PUBLISH-SPEC.md` and a session memory, and all three
>   were wrong together, which is what a copied claim does. **Check the live rule state; do not trust
>   this bullet either.**

~~**Status: BLOCKED on one external step — everything else is ready.**~~

The validator code is done, committed, and tested (~~352~~ **786** tests). The airlock is proven in
both directions. What remains is packaging the validator so a Batch job can *run* it, and
registering a job definition that points at that image.

## The one blocker

The validator must reach the `edullm-data` package inside the container. Two ways, both
needing something this workstation does not have:

1. **Bake it into an image** (`infra/Dockerfile.validator`) and push to ECR. ~~Needs Docker
   (not installed here) and `ecr:PutImage` (untested).~~ ← **this is what shipped.**
   `edullm-validator:10` runs from a digest-pinned ECR image with the code baked in.
2. **`pip install "edullm-data @ git+https://github.com/edu-llm/edullm-data@v0.6.3"`** at
   container start (the repo is public, so no auth needed). Still how `edullm-fsck:6` and
   `edullm-reservoir-ingest:7` work. (Pin was `@v0.2.0` here until 2026-08-01.)

The wheel itself builds cleanly (`python -m pip wheel . --no-deps` →
`edullm_data-0.2.0-py3-none-any.whl`, ~115 KB), so nothing about the package blocks this.

## What is verified and ready

- `batch:RegisterJobDefinition` **is allowed** (smoke-tested: a probe def was registered and
  deregistered).
- The validator role can `pip install` and write `edullm-data` (the live promotion test).
- The image runtime is python 3.12 with pip, no boto3, no uv — hence the bundled image.
- `<EVENTBRIDGE_INVOKE_ROLE>` is the correct EventBridge invocation role (deployed in the
  event-wiring stack, DISABLED).

## To finish (pick one path)

### Path A — bundled image (recommended)

On a machine with Docker + ECR push rights:

```bash
cd edullm-data
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
# A dedicated repo is cleaner than reusing olmo-core; create it if allowed:
aws ecr create-repository --repository-name <VALIDATOR_JOBDEF> || true
docker build -f infra/Dockerfile.validator -t \
  <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/<VALIDATOR_JOBDEF>:v0.2.0 .
docker push \
  <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/<VALIDATOR_JOBDEF>:v0.2.0
```

Then register the job definition (run through the sb-aws broker; all args verified-allowed):

```
mcp__sb-aws__aws(account="sbsandbox", command=[
  "batch","register-job-definition",
  "--job-definition-name","edullm-validator",
  "--type","container",
  "--container-properties", <the JSON below>
])
```

Container properties (fills the vcpus/memory that revision 1 leaves null, and pins the
image by digest after the push resolves one):

```json
{
  "image": "<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/<VALIDATOR_JOBDEF>@sha256:<digest>",
  "vcpus": 2,
  "memory": 4096,
  "jobRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/<BATCH_JOB_ROLE>",
  "executionRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/<BATCH_EXEC_ROLE>",
  "logConfiguration": {
    "logDriver": "awslogs",
    "options": {
      "awslogs-group": "/aws/batch/<JOB_QUEUE>",
      "awslogs-region": "us-east-1",
      "awslogs-stream-prefix": "validator"
    }
  }
}
```

Because the entrypoint is self-discovering (`python -m edullm_data.validate --promote` with
no args), the EventBridge rule can target this definition with empty `BatchParameters` — the
gap documented in `04-event-wiring.yaml` dissolves. Update the rule's `JobDefinition` to
`edullm-validator`, re-point `RuleTargetRoleArn` is already `<EVENTBRIDGE_INVOKE_ROLE>`, then
flip `RuleState` to `ENABLED`.

### Path B — wheel-from-S3 bootstrap (no Docker needed)

Upload the built wheel to a private prefix (the validator role can read landing), and use
the existing `<JOB_DEFINITION>` job definition with a command override that
installs it first:

```
command: ["sh","-lc",
  "pip install -q boto3 numpy && aws s3 cp s3://edullm-landing/_dist/edullm_data-0.2.0-py3-none-any.whl /tmp/ && pip install -q /tmp/edullm_data-0.2.0-py3-none-any.whl && python -m edullm_data.validate --promote"]
```

> ❌ ~~**The live job defs are at revision 2 and bootstrap `0.2.0` (cut over 2026-07-30).** Both
> `edullm-validator:2` and `edullm-fsck:2`~~ — **FALSE since long before 2026-08-01; see the banner
> at the top.** Live: `edullm-validator:10` (digest-pinned image, **no wheel at all**),
> `edullm-fsck:6` (wheel `0.6.0`), `edullm-reservoir-ingest:7` (wheel `0.6.3`). This sentence is
> where `CLAUDE.md` picked up the same error.
>
> The rest of the paragraph is still the right lesson: a wheel-bootstrapping job def should
> assert the version and the presence of `families/` immediately after install and exit non-zero
> if either is wrong — a silent fallback to an old wheel is what let the live corpus be validated
> against the wrong bounds once already. (`edullm-reservoir-ingest:7` still does exactly this,
> plus three source-inspection preflight asserts.) The image has no `aws` CLI, so a deployed
> command downloads with boto3 rather than `aws s3 cp` as sketched above.
>
> Both EventBridge rules target the job definition by **unversioned name**
> (`edullm-validator`, `edullm-fsck`), so registering a new revision cuts traffic over with no
> rule edit. That also means a bad revision takes effect immediately — verify with a manual
> `submit-job` before relying on it.
>
> `0.2.0` packages `families/` into the wheel, so the `EDULLM_FAMILIES_DIR` override the
> publish driver sets is no longer required (harmless to leave).

with `--container-overrides` supplying `vcpus`/`memory`. This works today with zero new
infra, at the cost of a pip install per run. Good enough to run the validator manually or on
a schedule; Path A is better for the event-driven steady state.

## Recommendation

Do Path B now to get the validator running end-to-end on real data (it needs only a wheel
upload, which the current session can do), and move to Path A when a Docker host with ECR
push is available. Either way, the code and the airlock are done; this is packaging, not
design.
