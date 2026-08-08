# edullm-data — orientation for a fresh session

You are working on **`edullm-data`**: the publisher, validator, and reader for the eduLLM dataset
standard. Public repo: `github.com/edu-llm/edullm-data`. This file auto-loads on every launch; it
orients you, it is not the spec.

## ⚠️ ON THIS BRANCH (`final-dataset`), READ THESE TWO FIRST

This worktree is the **corpus build for the 96-expert flagship / 32-expert baseline MoEs**, which is
newer than anything in `HANDOFF.md`:

1. **`HANDOFF-FINAL-DATASET.md`** — the living state of THIS work. Read it alone and you can continue.
2. **`docs/FINAL-DATASET-REPORT.md`** (+ PDF) — the plan of record: mix, sources, measurement, and how
   to draw the baseline subset from the same corpus.

Two things to know before touching any number: **Maple is a separate experiment and its configs must
not be read or cited** (owner instruction), and the model shape in the report is **derived**, not
given — confirm d_model and layer count with the owner before relying on token or cost figures.

`HANDOFF.md` below remains accurate about the **reservoir** corpus and the pipeline; it predates this
work and does not describe it.

## Then the standing orientation, in order

1. **`HANDOFF.md`** — the living state of the project. Read it alone and you can continue with no
   other context: what is built, deployed, live, and what's next.
2. **`docs/ONBOARDING.md`** — the 2-minute mental model (the airlock, the bucket layout, the address
   shape `<family>/<name>/<version>/`, what the pipeline forces).
3. **`CONTRIBUTING.md`** — the golden rule and how to add a profile.
4. **`docs/dataset-creation/DATASET-STANDARD.md`** — the full spec (+ `-DIAGRAMS.md`, + the motivating
   `s3-dataset-audit-2026-07-28.md`). **In this repo as of 2026-07-31**; it used to live in `../docs`
   and need `--add-dir`, which is why older notes mention that. No flag needed now. **If the code and
   the standard disagree, the standard wins and this package has a bug** — which is the reason it is
   versioned alongside the code: a document that adjudicates the implementation has to move with it,
   or the two drift and nobody notices which is stale.

## The one rule that governs every check

**Recompute, never trust.** Every validator check must recompute something from the bytes (a
`ContentLength`, a magic-byte sniff, a sampled decode) and compare it to what the manifest claims. A
check that only asserts a field is *present* is decoration — coding agents satisfy schemas
effortlessly and that is exactly how plausible garbage shipped before. See `CONTRIBUTING.md` "the
golden rule."

**Known gap — do not restate this rule as a payload re-hash.** `s3.hash_object` has exactly one
non-definition caller, `publish.py:280`, the PRODUCER. Gate A's per-entry loop
(`validate.py:399-431`) does `s3.head` for SIZE and then set-membership on the *declared* digest; it
never re-reads payload bytes. `fsck.py`'s docstring says so outright ("never a payload byte"). So a
manifest `sha256` is a producer assertion no gate falsifies. What the validator *does* recompute is
listed in `USAGE.md` "What gets rejected"; what `sha256` is actually for (content addressing +
the hash chain) and what defends integrity instead (the airlock's IAM Deny, S3 durability,
CRC64NVME) is in `docs/ONBOARDING.md`. Adding a real re-hash is an open decision, not a fact.

## Invariants you must not break

- **The airlock is an IAM Deny, not a convention.** Producers write ONLY to `s3://edullm-landing`
  (scratch inbox, 14-day expiry). The validator role (`<BATCH_JOB_ROLE>`, assumable only by
  `ecs-tasks` — no human/intern session can assume it) is the *only* principal that can `PutObject`
  to `s3://edullm-data`. After **any** live test that touched permissions, re-verify
  the Deny still fires (intern `PutObject` to `edullm-data` → `AccessDenied`, explicit deny) before
  you consider the task done.
- **Frozen means frozen.** Never edit a published `vN` in place to change data — publish `v2`. The one
  sanctioned in-place write is a **descriptive-keys-only** backfill (see README below), guarded by an
  assertion that `groups`/`manifest_sha256`/`inventory` stay byte-identical.
- **Token shards are `.u32le.bin`, never `.npy`.** The legacy `.npy` files were headerless raw uint32
  (the ".npy lie"); extension must match real bytes. `tokens × dtype_size == file bytes` exactly.
- **Every dataset carries a generated `README.md`.** It is a **derived** artifact (`readme.py`,
  `render_readme(dataset.json) → markdown`) written by `promote()`, and a **control file** (in
  `_CONTROL_BASENAMES`/`CONTROL_BASENAMES`) — never a manifest entry, never in the hash chain. It
  renders only from `dataset.json`, so it can't drift. Feed it via `publish(sources=/about=/notes=/
  limitations=/license=)`; absent sections are omitted, never faked. This applies to
  already-promoted datasets too — backfill the README in place, don't re-copy payload.

## AWS access in this workspace

All AWS goes through the **`sb-aws` MCP broker**, and it is **read-only by default** — you cannot (and
must not try to) write to `edullm-data` from a session; that's the airlock's whole point. Live
mutations happen only via validator-role Batch jobs. `iam:simulate-principal-policy` **lies** for the
intern role (11 known false denials) — smoke-test a permission live, never trust the simulator.

## Running on AWS Batch (hard-won gotchas)

If you ship a wheel-from-S3 Batch job (validate/publish/backfill), all four bite:
1. The Batch image has **no `aws` CLI** — download the wheel/driver/families with **boto3**.
2. ~~`families/` is **not in the wheel**~~ **FIXED** — `pyproject.toml` force-includes it, and
   `_resolve_families_dir()` finds it inside an installed package. Verified in a clean venv on
   `0.2.0`. The `EDULLM_FAMILIES_DIR` override in the publish driver is now redundant (harmless).
   Why it mattered: a missing families dir does not raise, it silently falls back to each
   profile's laxer constant — so it fails **only in production**, which is how the live corpus
   came to be validated at 50% EOS instead of the declared 5%.
3. **pip requires the PEP-427 wheel filename** — keep `edullm_data-<version>-py3-none-any.whl`
   end-to-end; a renamed `w.whl` is rejected. A wheel-bootstrapping job def names the wheel **by
   exact filename**, so shipping a new wheel changes nothing until that def is re-registered. Both
   EventBridge rules target the job def by unversioned name, so a new revision cuts over
   immediately — for better and worse.

   **Live state, verified by `batch describe-job-definitions --status ACTIVE` on 2026-08-01:**

   | job def | top ACTIVE rev | how code gets in | timeout |
   |---|---|---|---|
   | `edullm-validator` | **10** | **image, digest-pinned — no wheel** | 7200 s |
   | `edullm-fsck` | **6** | wheel `0.6.0` | 3600 s |
   | `edullm-reservoir-ingest` | **7** | wheel `0.6.3` | 7200 s |

   ~~`edullm-validator:8` and `edullm-fsck:5` bootstrap `0.6.0`~~ — **superseded 2026-08-01.**
   `edullm-validator:9/10` no longer bootstrap a wheel at all: they run
   `python -m edullm_data.validate …` directly out of `sbsandbox-intern-edullm-data@sha256:339c2b6b…`,
   with code **baked into a digest-pinned image**. That is strictly better provenance than a wheel
   dropped in `_dist/` — the image digest pins every byte of the dependency tree, and `_dist/` has
   **no lifecycle expiration**, so a wheel there is mutable-by-overwrite forever. Registered by a
   concurrent session; it wins. Revs 10 and 6 differ from 9 and 5 *only* in `jobRoleArn`
   (`…-dataset-validator` / `…-batch-workload`) — the airlock-correct identity is the newer one, so
   cite 10/6, not 9/5.

   ⚠️ **Auto-promotion is OFF as of 2026-08-01.** The `edullm-landing-manifest-created` EventBridge
   rule is `DISABLED` (verified via `events describe-rule`), so landing a `manifest.json` no longer
   triggers a validator job. Re-enable it or `submit-job` by hand. `edullm-wu-fsck-nightly` is still
   ENABLED — and is weekly, not nightly: `cron(6 9 ? * MON *)`.

   ⚠️ **NEVER TRUST A VERSION STRING AS A DEPLOYMENT CHECK — DIFF THE ARTIFACT.** The wheel
   deployed before this, `0.5.1`, contained a Gate A function
   (`pretrain_tokens_v1._cap_min_distinct_by_vocab`) that existed in **no commit on any branch**;
   `git log --all -S` found nothing. It was built from a dirty tree, and `__version__` was the
   only *other* difference from `main` — so comparing versions said "stale but equivalent" while a
   real behaviour lived only in S3. Rebuilding from git and reshipping would have silently
   regressed a check that published frozen data depends on (`pretrain/lean4-mathlib-bytes/v3`'s
   own `about` names it). Recovered and tested in `0.6.0`. To audit a deployed wheel, download it
   and diff every `.py` against the tag — normalising line endings, since a Windows-built wheel
   differs from `main` on every line otherwise.
4. Single-threaded publish **times out** on large corpora — the 218-shard/125 GB olmo run was
   killed at 3600 s. Use `hash_workers`/`copy_workers` and pass
   `--timeout attemptDurationSeconds=7200`.

   ⚠️ ~~"the 60-min job-def limit"~~ — **corrected 2026-08-01. There is no such limit.** Per the
   AWS Batch user guide (*Job timeouts*): "**There's no maximum timeout value for an AWS Batch
   job**", `attemptDurationSeconds` "must be at least 60 seconds", and "**By default, AWS Batch
   doesn't have a job timeout** — if you don't define a job timeout, the job runs until the
   container exits." So 3600 s was **a value we set in our own job definition**, not a ceiling AWS
   imposes, and raising it is a `register-job-definition` call rather than a constraint to design
   around. (One real ceiling exists and does not apply to us: jobs on **Fargate** resources cannot
   expect to run beyond **14 days**. Our defs are EC2.) The reason to shard work anyway is
   blast radius — a long single-attempt job with `attempts: 1` loses everything to one transient
   failure — not a platform maximum.

## Working style

Tests mirror source modules (`test_manifest.py` ↔ `manifest.py`); every profile ships a passing **and**
a failing fixture. Run `python -m pytest -q` (currently **786 passing** on
`agent/claude-01/reservoir-ingest`, measured 2026-08-01). Public repo — scrub internal
AWS account IDs to placeholders in anything committed (bucket names are functional constants, keep
them). Autonomous runs are fine; keep subagent fan-out ≤~16 in sequential waves and persist to disk
continuously (this machine has died mid-run). Ship changes via a branch + PR, not a direct push to
`main`.
