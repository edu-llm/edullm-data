# 02 — Branch lessons and capability recon: `final-dataset` @ `8e2524a`

**Purpose.** Basis for an execution plan to ingest `allenai/OLMoE-mix-0924` and publish 50B- and
100B-token releases into the eduLLM dataset standard, dedup/decontam SKIPPED.

**Source of truth for this document.** Read-only recon of the worktree
`/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/final-dataset`, branch `final-dataset`
(== `origin/edullm/final-dataset-phase0`) @ `8e2524a`, 160 commits ahead of `origin/main`
(merge base `38bf831a6c3f445e394784018441fd59288b876c`). The canonical checkout at
`/Users/ericwu/Developer/Capstone_LLM/edullm-data` is stale and was not read for facts.

No file outside `artifacts/olmoe-mix-ingest/` was modified. No branch switched. No test suite run.
No AWS mutation.

---

## 0. THE BLOCKER THAT INVALIDATES THE PREMISE — read this first

**`allenai/OLMoE-mix-0924` is NOT pre-tokenized. It ships raw text documents as `.json.gz`.**

Verified directly against the HuggingFace dataset page (the repo contains zero references to
`OLMoE`, so this is not recoverable from the branch — `grep -rni olmoe` hits only
`docs/FINAL-DATASET-REPORT.md:166`, a model-comparison table row):

| fact | value |
|---|---|
| format | `data/<subset>/<subset>-train-NNNN.json.gz` — gzipped JSON documents |
| schema | `id`, `text`, `added`, `created`; some files add `doc`, `metadata`, `attributes` |
| total size | **7.5 TB** |
| tokens | **4.07T** (3.08B docs, 17.4T bytes) |
| license | **ODC-By v1.0**, plus "bound to licenses and Terms of Services of underlying datasets" |
| tokenizer | **none shipped.** The card names no tokenizer for the data; text is unsegmented |

Per-subset token counts from the card: DCLM-baseline 3.86T, Starcoder 101B, peS2o 57.2B, arXiv
21.1B, OpenWebMath 12.7B, Algebraic Stack 12.6B, Wikipedia+Wikibooks 3.69B.

**Consequences for the plan, in order of severity:**

1. **The task is not "copy + publish". It is a full tokenization build** — exactly the pipeline
   `corpus_read.py` + `corpus_pack.py` + `corpus_build.py` exist to run, at ~10 h of Batch compute
   per the branch's own measured floor. The stated goal ("already-processed… as fast as possible…
   uint32 shards") rests on a false premise about this upstream.
2. **"Skip dedup and decontam" is a coherent choice but is NOT free** — it is a code path, not an
   omission. `dedup_and_decontaminate` is wired into `run_bundle`; skipping it is a flag/edit, and
   it is also **~78% of build cost** (see §3, the measured rate), so skipping it is the single
   largest available speedup, worth roughly 4.5×.
3. **The 50B/100B framing is a *subset selection*, not a size of the upstream.** 4.07T upstream →
   50B is 1.2%. This is a `target_tokens` per registry row problem, which the plan machinery
   already expresses.
4. **A pre-tokenized alternative may not exist for this mix.** `s3://ai2-llm-public` is
   anonymously listable (I verified: top-level `checkpoints/`, `eval-data/`, `preprocessed/`,
   `pretraining-data/`). Its `preprocessed/` holds **12 prefixes and none is an OLMoE mix** —
   `cc_all_dressed`, `common-pile_{codetextish,texbookish,wikish}`, `dolma4pdfs`, `hplt-project`,
   `olmo4_mixing_calibration`, two `sponge_211_*`, `the-stack-v2`,
   `tokyotech-llm_swallow-{code,math}-v2`. `pretraining-data/sources/` holds only `common-crawl/`.
   Both listings are `IsTruncated: false`, so this is complete at that depth.
   **However** the branch records that AI2 pre-tokenized dolma shards ARE byte-compatible with our
   format (`docs/FINAL-DATASET-REPORT.md:354-355`: "AI2's pre-tokenized shards are byte-identical
   to our format (verified by range-read: no NumPy header, valid token ids from byte 0), so their
   5.93T corpus is a byte copy rather than a re-tokenization"). **If the real goal is speed, find a
   pre-tokenized dolma2/dolma3 mix and copy+rename it — that is hours, not days.** That is a
   different upstream than `OLMoE-mix-0924`, and the decision belongs to the owner.
5. **`OLMoE-mix-0924` is dolma-family content but its own tokenizer is the OLMoE one, not
   dolma2.** If we tokenize it ourselves we choose dolma2 (the standing decision) and the token
   counts on the card do not transfer — see §6, the token-count trap.

**Recommendation to surface before any execution plan is written:** confirm with the owner whether
the intent was (a) tokenize OLMoE-mix ourselves at 50B/100B subsets, or (b) find an
already-tokenized mix to byte-copy. They differ by roughly an order of magnitude in wall-clock and
by which half of this repo you use.

---

## 1. HANDOFF state — what is built, deployed, live, broken

**Two handoff files, and they are BOTH current for different scopes.** `CLAUDE.md:8-30` on this
branch is explicit about the order.

- **`HANDOFF-FINAL-DATASET.md`** (431 lines, 2026-08-07) — the *newer* doc. Scope: the ~1.0T
  `final-dataset` corpus for the 96/32-expert MoEs. Its own scope note
  (`HANDOFF-FINAL-DATASET.md:8-10`) says the repo-wide `HANDOFF.md` is inherited from the branch
  base and "is still accurate about that and is deliberately left alone."
- **`HANDOFF.md`** (2,250 lines, 2026-08-04) — the reservoir build + the standing platform state.
  This is where the *pipeline* facts live, and it is what matters for our ingest.

### 1.1 Built (code)

Every build stage exists. `HANDOFF.md:452-467` is the status table:

| module | lines | what it is |
|---|---|---|
| `src/edullm_data/corpus.py` | 617 | build-time CONTRACT: shard geometry, ordinal allocator, held-out predicate, EOS floor |
| `src/edullm_data/corpus_read.py` | 1088 | per-corpus readers — parquet **and** `.json.gz` |
| `src/edullm_data/corpus_pack.py` | 1114 | exact 25,001,984-token shards, conservation asserted at runtime |
| `src/edullm_data/corpus_build.py` | 1925 | Batch driver: plan / run / resume / verify |
| `src/edullm_data/corpus_receipt.py` | 1933 | build receipts; the one place the pipeline re-hashes payload |
| `src/edullm_data/corpus_filter.py` | 886 | exact dedup + eval decontamination (40/40 GSM8K caught, 0 FP) |
| `src/edullm_data/validate.py` | 2646 | Gate A orchestrator + `promote()` + `discover_pending()` + CLI |
| `src/edullm_data/publish.py` | 1101 | producer `publish()`; never holds a payload whole |
| `src/edullm_data/ingest_prm800k.py` | 1096 | the external-vendor ingest precedent |
| `src/edullm_data/ingest_reservoir.py` | 1094 | HF→S3 transport, array-sharded |
| `src/edullm_data/read.py` | 1219 | `dataset_paths`, `resolve_latest`, `verify_seal`, `build_mixture` |
| `src/edullm_data/fsck.py` | 297 | Gate B post-publish decay sweep |

`corpus_read.py` reading `.json.gz` matters directly: **that is OLMoE-mix's format**, and the
handoff says the `.json.gz` path "did not exist" before this branch built it
(`HANDOFF.md:137-138`).

### 1.2 Deployed — and `infra/` is STALE; `artifacts/orchestration/` is the only current record

⚠️ **The whole `infra/` directory is stale on job-def revisions.** The current record lives in
`artifacts/orchestration/LEDGER.md` and `artifacts/orchestration/plat/status.md` (2026-08-08), which
I verified by grep.

| job def | rev | timeout | promotes? | notes |
|---|---|---|---|---|
| **`edullm-validator`** | **16** | **28,800 s** | **NO — `--promote` removed** | Gate A only. `LEDGER.md:1557,1563` |
| **`edullm-promote`** | **2** | 28,800 s | **yes** — `--promote --promote-workers 16` | **the only promoting def.** `LEDGER.md:1563,1690` |
| `edullm-reservoir-build` | **12** | **64,800 s (18 h)** | — | role `edullm-final-dataset-build` (revs 11/12); rev 9 "builds the WRONG corpus" |
| `edullm-reservoir-verify` | 3 | 14,400 s | — | `HASH_WORKERS=8` |
| `edullm-dataset-publish` | 1 | ≥21,600 s | — | AUTHORED, **NOT DEPLOYED** — `iam:CreateRole` denied to every broker session |
| `edullm-validator-preflight` | 4 | — | — | in-container assertion runner; touches no data |
| `edullm-fsck` | 6 | 3,600 s | — | wheel 0.6.0 |

**The revision number has been reported as 2, 8, 9, 10, 12, 14, and 16 across seven files.** Chain:
`infra/05-validator-jobdef.md:138` (rev 2 — and that file self-declares *"STATUS: DONE. THIS RUNBOOK
IS HISTORY, NOT INSTRUCTIONS"* and calls its own rev-2 claim *"the single most-copied error in this
repo's docs"*) → `CLAUDE.md:144` (rev 10) → `docs/PRM800K-INGEST.md:82` (rev 9) →
`docs/IMPLEMENTATION-PLAN.md:956` (rev 12) → `infra/DEPLOY.md:809` (rev 14) →
`artifacts/orchestration/plat/status.md:54-56` (*"CLAUDE.md's job-def table is stale"*) →
**`LEDGER.md:1557`: rev 16.** **Cite rev 16 and `edullm-promote:2`; re-verify live before submitting.**

Code enters the validator **as a digest-pinned ECR image, not a bootstrapped wheel**. Current image
`sha256:5fb76f66…a906`, tag `5450f538363d`, shared by `edullm-validator:16` and `edullm-promote:2`.

⚠️ **An ECR tag is also not a code identity.** `infra/DEPLOY.md:803` says identify an image by its
ECR tag, never the version string. But `LEDGER.md:1411` refutes even that — *"an image tag is
exactly a version string"* — and `plat/status.md:2284` records *"I did not trust the tag."*
**Only an in-container preflight settles it.** `plat/status.md:2195` records that pinning
`sha256:5fb76f66…a906` "would have launched 185 bundles against code that cannot file-shard — the
`0.5.1` wheel failure mode in image form." (One genuine guard: **ECR tag immutability** — a build
failed with `tag invalid: The image tag '69667edbb070' already exists … the tag is immutable`
because a concurrent session pushed the same commit 7 minutes earlier. `LEDGER.md:2262-2270` calls
this "the same class of protection as the airlock Deny.")

### 1.3 Live in S3

- `s3://edullm-data` — **ten datasets** in `_catalog/` (`HANDOFF.md:121`). Named explicitly:
  `tokenizer/dolma2-bpe/v1`, `pretrain/olmo-150b-dolma2/v1` (157.2B), a 126.7B corpus, and
  `pretrain/reservoir-dolma2/v1` — the last sealed at **10,049 objects / 1,004,872,007,680 bytes**
  (`infra/DEPLOY.md:823-826`). So the reservoir DID publish after `HANDOFF.md` was written; that
  file's "nothing is published" banner is stale.
- `s3://edullm-landing` — the airlock inbox, 14-day expiry, plus `_ingest/` on a separate
  `expire-ingest-30d` rule and `_dist/` with **no expiry**.
- Bucket policy live is **`edullm-data-airlock-v2`** — Put and Delete Denies split, nobody exempt
  from Delete (`HANDOFF.md:118-119`).

### 1.4 Broken / stale / gotchas that bite

1. **⚠️ Auto-promotion is OFF — and the promotion PATH changed.** Three independent guards now stand
   between a staged manifest and a published dataset, and the branch reports the rule's state **four
   different ways** — the sharpest contradiction in the repo:
   - `infra/` (4 files) + `CLAUDE.md:172-174` + `HANDOFF.md:2185-2188`: rule **DISABLED**.
   - `artifacts/orchestration/plat/status.md:91` **measured it ENABLED on 2026-08-08** via
     `aws events list-rules`, and called it *"the most dangerous item in my report"*: **"Writing a
     `manifest.json` to `s3://edullm-landing` IS publishing. There is no publish-without-promote
     mode."** Commit `7cfb62e` is titled *"the rule is ENABLED — three places said DISABLED and all
     three were wrong."*
   - `plat/status.md:135-147`: an owner-authorized mutation then **disabled it** (`aws events
     disable-rule --name edullm-landing-manifest-created`, exit 0). Re-enable is the symmetric
     `enable-rule`.
   - `LEDGER.md:1567-1573`: **`edullm-validator:16` no longer carries `--promote` at all.** *"Even if
     the rule were re-enabled by accident, it cannot promote. This is a stronger guarantee than the
     disable-rule mutation gave us — the disable gated submission; this removes the capability."*

   **Operationally today: no auto-promotion.** Promotion is a **deliberate, separate submission to
   `edullm-promote:2`**. Guard three is IAM: every build role is explicitly Denied from writing
   `*manifest.json` (`infra/11-final-dataset-build-policy.json:24-28` calls this "the third
   independent guard"). **Re-run `events describe-rule` AND `batch describe-job-definitions` at
   submission time** — a rev 17 with `--promote` would silently restore auto-promotion. This
   *inverts* the memory note "publishing to landing auto-promotes."
2. **`HANDOFF.md`'s START HERE banner is stale.** It says the reservoir is built-and-unpublished
   blocked on `bundle-set-mixed-wheel-versions`. `infra/DEPLOY.md:823` shows
   `pretrain/reservoir-dolma2/v1` sealed and live. The blocker was cleared.
3. **`publish()` must run in-region, on Batch.** It stream-hashes every object, so it pulls every
   byte to wherever it runs — **measured 0.8 MiB/s off-region**, ~9 days for a 1 TB corpus
   (`HANDOFF.md:2183-2184`). This is a hard blocker for any laptop-driven publish.
4. **A capacity-starved Batch job hangs FOREVER, silently** (`CLAUDE.md`, `IMPLEMENTATION-PLAN.md`
   §8B.7). Every queue declares `CAPACITY:INSUFFICIENT_INSTANCE_CAPACITY → CANCEL after 1800s`, but
   Batch leaves `statusReason` null for capacity failures **so the rule never matches**. Set your
   own wall-clock expectation per job.
5. **`state: ENABLED` is not evidence a shape can run.** H100 (`gpu-1xh100`) is ENABLED/VALID with
   **zero successful jobs ever**. Largest working GPU shape is `gpu-8xa100`. (Irrelevant to our
   CPU-only ingest, but it is the same class of error.)
6. **CPU cap is 384 vCPU, not 128** (`sbsandbox-intern-edullm-cpu`, `maxvCpus: 384`, MEASURED
   2026-08-07). The "128 vCPU hard floor" every older wall-clock figure rested on was a 3×
   understatement.

### 1.5 Immediate next steps as the branch left them

`HANDOFF-FINAL-DATASET.md:300-345` — six blockers for the 1.0T build, "none of which fails loudly."
Of these, the ones that touch our ingest at all:

- **#20 / blocker 1 — ordinals shift when a source is added.** Adding one 4B source renames **98%
  of shards** and voids 882B tokens. **Fix is zero code: freeze the FULL plan first.** This bites
  us directly if we publish 50B and then "add" to reach 100B. See §7.
- **#10 — Gate A exceeds its timeout unfixed** at large object counts. Landed on this branch
  (threaded); see §3.
- **#22 — dedup set OOMs at 1T** (DCLM needs 27.92 GB in a 15.03 GB container). We skip dedup, so
  **irrelevant** — but note the flat `np.uint64` fix exists if we ever turn it on.
- #21 (FinePhrase partition), #24 (decon index from 5-shot renders), #25 (val split is 43% of bytes
  moved), #28 (file-shard DCLM), #29 (retire `verify --deep`).
- **Two embarrassments**: `tokenizers` was undeclared while `corpus_build.py:631` imports it (now
  declared, `pyproject.toml:66`); and every Cosmopedia doc begins with a leading space, changing its
  first token under byte-level BPE (MEASURED 303/303).

---

## 2. Version, greenness, tests

**Version string is `0.9.1`, in THREE places — all three must be bumped together or the image build
fails.** (I initially reported two; corrected and verified by reading the file.)

1. `pyproject.toml:7` — `version = "0.9.1"`
2. `src/edullm_data/__init__.py:3` — `__version__ = "0.9.1"`
3. **`.edullm/Dockerfile:23`** — a **hardcoded literal inside a build-time assertion**:
   ```
   RUN python -m pip install --no-cache-dir . \
       && python -c "import boto3, numpy, edullm_data; assert edullm_data.__version__ == '0.9.1'"
   ```

**Bumping only 1+2 breaks the image build. Bumping only 3 also breaks it.** This is the file the
memory note `deploy-is-image-push-not-wheel` refers to — a wheel dropped into `_dist/` does not
deliver a version change at all.

`.edullm/Dockerfile` carries a **second build-time assertion** (`:31-35`) worth knowing:
`_family_decode_bounds()[0] == 0.05`, so a `families/` packaging regression **fails the image rather
than silently loosening the EOS gate in a job**. `ARG BASE_IMAGE` has no default deliberately, "so
the image cannot silently build from an unregistered base," and no `command` is set so ingress and
validator share one digest. `.dockerignore` excludes `artifacts/`, so the corpus registry is **not**
in the image and must be staged from S3 via `--registry`.

Beyond the three declarations, job definitions run `assert __version__ == '<X>'` inside the
container. 🔴 **At least one live def still asserts `'0.7.4'` while the verified image is 0.9.1**
(`plat/status.md:2083`, called "precisely the 'two parallel lines' hazard").

⚠️ **A version string is not a code identity, and this repo has been burned by it twice.**
`infra/DEPLOY.md:803-805`: *"Identify an image by its ECR tag (a commit sha), never by version
string — two commits in this repo say `0.7.4` and two more say `0.8.0`, so every job def's
`assert __version__ == '<X>'` passes on materially different trees."* And `CLAUDE.md:161-170`
records a deployed `0.5.1` wheel containing a Gate A function
(`pretrain_tokens_v1._cap_min_distinct_by_vocab`) that existed **in no commit on any branch** —
built from a dirty tree.

**Is the branch GREEN?** Not stated for `8e2524a`. The evidence chain:

| claim | source | dated |
|---|---|---|
| **786 passing** | `CLAUDE.md` working-style section | 2026-08-01, on `agent/claude-01/reservoir-ingest` |
| 1,085 passing | `HANDOFF.md:53` | earlier |
| 1,107 / 1,101 / 1,090 | `HANDOFF.md:380,469` and the four-bugs section | 2026-08-04 |
| **974 tests** | memory note `corpus-build-modules-exist` | — |

The counts are mutually inconsistent and none is dated to the branch tip. `tests/` holds **47 test
files**. Treat greenness as **UNVERIFIED at `8e2524a`** and re-measure on FarmShare or AWS before
relying on it — do not quote a number from a doc.

**How tests are run** (`pyproject.toml:136-152`):

```
python -m pytest -q
```

`pythonpath = ["src"]` (src-layout, works with no editable install). `addopts = "-ra -m 'not
network'"` — **the default run is offline**; network tests are opt-in via `-m network`. That marker
exists because a registry `config` that 404s is invisible to every offline test: the row parses,
the plan builds, `plan_id` is stable, and the failure arrives inside a billing Batch container.
**One row of 133 shipped that way** (`cosmopedia`, E17 — and `8e2524a`, the branch tip, is the fix
for exactly that). **Run `-m network` before spending money on a build.**

Dependency pins that are load-bearing (`pyproject.toml:27-82`): `numpy<2.5`,
`tokenizers>=0.21,<0.23`, `pyarrow>=24,<26`, `requires-python >=3.10`. The `tokenizers` bound is a
*reproducibility* bound, not a byte-identity claim: below 0.21 `\p{L}`/`\p{N}` resolve through
Oniguruma's Unicode 14 tables (upstream WONTFIX), so two builds on two mornings can disagree on
recently-assigned codepoints with no gate able to see it. **A tokenizer change silently emits
different ids that stay inside the vocabulary and decode cleanly — every check this package owns
would pass on the wrong bytes.**

---

## 3. PERFORMANCE and CORRECTNESS fixes on this branch, absent from `main`

This is the section the whole exercise is for. The user's belief — that this branch carries the
fixes that make the process faster — **is correct**, and the ones that matter most for a
publish+validate of many objects are all here.

### 3.1 The performance fixes, with knobs and defaults

| # | fix | commit | file / knob | effect | relevance to our ingest |
|---|---|---|---|---|---|
| P1 | **Threaded Gate A profile checks** | `3545fe7` (B3/#10), merged `1e18a63` | `validate.py`, `--head-workers`; **default 1** | Gate A on ~10k entries is ~85 min of latency-bound serial I/O (~80,392 round trips at ~15.8/s). Threading trims **~12%, not half** | **RELEVANT** — and see the honest number: threading is NOT the big win at Gate A |
| P2 | **One HEAD per object per run, shared across the profile's checks** | `db437b6` | `validate.py` shared HEAD cache | removes duplicate HEADs; the measured cost model is **8 round trips per object**, not 6 | **RELEVANT** |
| P3 | **Gate A's HEADs were serial too — same omission, same guarantee** | `9195098` | `validate.py` | Gate A's own per-entry loop was unthreaded even after the profile checks were | **RELEVANT** |
| P4 | **`--head-workers 16` against a 10-connection pool caps itself, silently** | `3b11d7d` | `s3.py` sized connection pool (`test_s3_pool.py`) | **This is the one that makes P1–P3 actually work.** Without it, 16 workers contend on a 10-connection botocore pool and you get ~10-way concurrency while believing you have 16 | **BLOCKER-IF-MISSING** — the threading flags are a lie without it |
| P5 | **Threaded deep re-hash (`verify`)** | `796d8a6` | `corpus_receipt` / verify path, `--hash-workers` via `HASH_WORKERS=8`; **1 worker stays byte-for-byte the old path** | **7.82× on 8 workers** (`artifacts/impl-plan/orchestrator-findings.md:396`) | RELEVANT if you run a deep verify |
| P6 | **Threaded publish hash + copy** | `8b8e63f` (on `main` already) | `publish.py:321,467,472` `hash_workers`; `:849,1022,1025` `copy_workers`; **both default 1** | "a ~45-min sequential hash and a ~40-min sequential copy into a few minutes each" (`publish.py:860-866`) | **RELEVANT — set both to 16** |
| P7 | **`ChecksumSHA256` on the sink** (B7) | `5118308`, merged `23ef8e9` | `corpus_build.py` upload sink + `s3.py` | a verified PUT gives free what the 1.49 h `verify --deep` re-hash was buying. Enables #29 (delete the deep verify) | **RELEVANT** — the fast path to skipping deep verify |
| P8 | **Keep-list consumer** | `5118308`, merged `23ef8e9` (`test_corpus_keeplist.py`) | `corpus_build.py`, `corpus_receipt.py` | lets a build consume a precomputed keep-list instead of recomputing the filter | **RELEVANT — this is how you cheaply skip/shortcut filtering** |
| P9 | **File-shard one bundle across K children, plan-assigned disjoint ordinals** | `6f3f23e`, `b0eff92`, merged `91defb9`; receipt part `5a44229`, `5184f44`; verify `22ed7d4` | `corpus_build.py`, plan schema **v2** | the fix for blocker 0: one bundle could not be split, so DCLM's 410B was ONE child at 10.85 h even on a whole 32-vCPU instance. Path 54.5 h → **15.5 h** | **RELEVANT** if any one subset of OLMoE-mix is large (DCLM is 3.86T of it — it certainly is) |
| P10 | **Registry `_file_shards`** | `69667ed` | registry | the artifact carries its own file-shard answer | RELEVANT |
| P11 | **Flat `np.uint64` dedup pre-pass**, replacing the 27.92 GB set | `0700b7e`/`d1d9c8f`, merged `457b463`; sizing corrected `a372bf8` | `corpus_filter.py` | **memory, not speed**: 27.92 GB → 2.60 GB; the docstring's 155 B/entry was itself a correction of a claimed 113 B | **IRRELEVANT — we skip dedup.** Noted only so nobody re-derives it |
| P12 | **429 was the ONLY retried status, so one 503 killed five bundles** | `7a97c27` | transport | one transient 503 lost five bundles of work | **BLOCKER-IF-MISSING** for any multi-hour HF fetch |
| P13 | **Resolve once per file, not once per range — a 70× quota reduction** | `cd1024e` | `ingest_reservoir.py` | pyarrow issues ~70 range reads/file; every one hit the *metered* HF control plane. 1,400 metered requests for 20 files, budget empty in ~79 s at 40 workers | **BLOCKER-IF-MISSING** for an HF fetch at scale |
| P14 | **Validator timeout 7200 → 14400 s** | job def rev 14, `infra/DEPLOY.md:809` | job def, not code | **7200 s is what SIGKILLed the reservoir promotion at 6,324 of 10,051 objects.** `infra/DEPLOY.md:816-819`: *"Budget >=4h for a corpus of that size and more if it grows"* | **BLOCKER-IF-MISSING** |

**The honest performance picture, and it is not what "just thread it" implies**
(`infra/DEPLOY.md:816-819` + `HANDOFF-FINAL-DATASET.md:361-368`):

- **Gate A is latency-bound serial I/O**: `objects × 8 round trips × latency`. Threading trims
  ~12%. **The real lever on Gate A is fewer objects** — i.e. larger shards. At `SHARD_TOKENS =
  25,001,984` (`corpus.py:89`), 50B ≈ **2,000 objects** and 100B ≈ **4,000 objects**. The branch
  measured stage 2 at 4,000 objects validating in **0.56 h with no code change**
  (`HANDOFF-FINAL-DATASET.md:278-281`). **Our releases are small enough that Gate A is a non-issue.**
- **Build cost is filter-bound, not tokenizer-bound.** MEASURED end-to-end **72,615 tok/s/vCPU**,
  not the 0.328 M tok/s/vCPU of `encode_batch` in isolation — a **4.52× optimism**. *"Tokenize is
  only ~22% of build cost; ~78% is `dedup_and_decontaminate`, single-threaded Python holding the
  GIL."* **Since we skip the filter, our build should run near the 4.5×-faster figure** — this is
  the single largest speedup available to this task, and it comes from the scope decision, not from
  code.
- **More machines cap at ~6.2×** because the filter is serial. Skipping the filter removes that
  ceiling too.
- **`--shard/--of` strides BUNDLES**, so an aggregate vCPU floor is not a per-child bound. The
  aggregate floor was moved 6.61 → 2.21 → **9.96 h** in one session; the cap correction (3×
  better) and the rate correction (4.5× worse) nearly cancel.

### 3.2 The correctness fixes — each one prevents a specific silent wrong corpus

| # | fix | commit | prevents |
|---|---|---|---|
| C1 | **One control-file allowlist, shared by producer and validator** | `4d6768e` | `_dedup/clusters.parquet` and `_licenses/` were rejected by Gate A *and* swept into `manifest_sha256` as payload by `publish.py`, which had a **separate** basename-only allowlist. Fixed at the root in `contracts.py`. **A green suite could only ever prove the two copies agreed, never that there was one definition** |
| C2 | **Refuse duplicate `source_label`/key** | `5004159` | **33.3% silent token loss** — a duplicate label collapses rows and loses one row's tokens with no error |
| C3 | **The reader had no stop condition, and the corpus was tokenized twice** | `241334c` | double work + double counting |
| C4 | **`<\|endoftext\|>` appears in scraped text, and it IS the boundary id** | `1a0912e` | a document containing the literal boundary marker splits a document in the packed stream |
| C5 | **Boundary-marker guard derived from the table, not hardcoded** | `7f2cf84` (B2) | the two-character prefix guard made any addition to the rewrite table a **silent no-op**. Under dolma2 **only the end-of-text token is a document boundary**; the other 21 added tokens are ordinary ids |
| C6 | **One canonical format table — derive the gate from the reader registry** | `2bb1a36`, merged `9cd41c2` | two format tables drifting |
| C7 | **`ReadStats.problems()` wired into `run_bundle`** | `fb29a02`, merged `aca08a8` | read problems were computed and never surfaced |
| C8 | **The build could not have read 4 of 14 sources, and ignored the pins** | `b6d9d4f` | four registry rows unreadable; only real bytes could tell |
| C9 | **A deliberate SUBSET is not an under-allocated plan** | `0b8135e` | pack refusing a legitimate subset build — **directly relevant: our 50B/100B ARE subsets** |
| C10 | **Reject a label segment that breaks a consumer** | `39a539f` | `C#` in an object key **silently truncates any `s3://` URI at the `#`**; naive slugging sends `C#`/`C++`/`C`/`C--` all to `c`. `build_domain_slug_map` now **raises** on collision |
| C11 | **`pre_buffer=False` on `pq.ParquetFile`** | `59969bb` | exit 139 SIGSEGV: pyarrow's default dispatches a Python file object's range reads onto Arrow's native C++ IO thread pool. 3/4 unpatched children crashed, 0/4 patched |
| C12 | **Read exactly n bytes — a short HTTP read was SIGSEGV-ing pyarrow** | `79b8481` | same crash class, different cause |
| C13 | **Lower pretrain `distinct_ids_min` 256 → 128** | `d08aa05` | a legitimate corpus rejected by an over-tight floor. **`families/pretrain.json` now declares 128** |
| C14 | **K file-shard siblings share one stream BY CONSTRUCTION** | `5184f44` | receipt/stream identity under file-sharding |
| C15 | **Checksum-verified upload — the golden rule on the write path** | `e2fa75a` | an upload that silently wrote different bytes |
| C16 | **`families/` force-included in the wheel** | `pyproject.toml:117-131` | **THE canonical failure of this project.** A missing families dir does not raise — it silently falls back to each profile's laxer constant, so it fails **only in production**. *"the live corpus was validated at 50% EOS / 50% zeros instead of the family's 5% / 1%"* |
| C17 | **`bundle-set-mixed-wheel-versions`** (cheap tier) | `corpus_receipt.py:30-50` | *"The gates a bundle passed are a property of the wheel that packed it."* Blocked the reservoir publish and was correctly not waived. **This will fire on us if we let a version bump land mid-build** |
| C18 | **`tokenizers` + `pyarrow` declared and bounded** | `2f14465` (B1/#23) | production resolving whatever PyPI served that morning — and a tokenizer change is **silent**, not a crash |
| C19 | **Registry `config` resolution / network marker** | `8e2524a` (branch tip) | `cosmopedia` shipped with a `config` that 404s — invisible offline, fails inside a billing container |
| C20 | **Cheap-tier / deep-tier separation, `verify_bundle_set` pure with no `s3=`** | — | **predict a 1 TB gate's verdict for free** by running only its pure half. Copy the receipts locally (1.6 MiB) and get the verdict in under a second |

### 3.2b ⚠️ THE BRANCH IS DIVERGENT, NOT A SUPERSET — and `main` has the profile we need

**This reverses a premise I stated earlier in this document.** `origin/main` is **18 commits ahead of
the merge base too.** I verified: `git rev-list --count 38bf831..origin/main` → **18**, and
`git log --oneline HEAD..origin/main` lists 19 commits (incl. merges) that are **not on this branch**.
`main` is at `__version__ = "0.8.0"`; the branch is at `0.9.1`. Neither contains the other.

#### 🔴 The profile sets are DISJOINT, and this is the single most important finding for our task

Verified by `git ls-tree` + reading each `registry.py`'s `_SHIPPED`:

| profile | on `final-dataset` | on `origin/main` |
|---|---|---|
| `pretrain-tokens/v1`, `eval-results/v1`, `token-order/v1`, `sft-conversations/v1`, `tokenizer/v1` | ✅ | ✅ |
| **`vendored/v1`** | ✅ | ❌ **absent** |
| **`text-corpus/v1`** | ❌ **absent** | ✅ **present** |

**`origin/main` ships `src/edullm_data/profiles/text_corpus_v1.py` — a registered profile for RAW
UNTOKENIZED DOCUMENTS, which is exactly OLMoE-mix's format.** Its docstring (read from
`git show origin/main:src/edullm_data/profiles/text_corpus_v1.py`):

> *"Profile `text-corpus/v1` — raw untokenized documents (§4). Companion to `pretrain-tokens/v1`: the
> same corpus can ship a `text/` group of document JSONL next to a `tokens/` group of packed shards.
> The failure this profile prevents is publishing "text" that is empty, not JSONL, or missing the
> declared text field… Rows come from `.jsonl` / `.jsonl.gz` per manifest entry (Dolma-style)… Row
> counts are **recomputed by streaming-parsing the payload**… never trusted from the manifest alone."*

`REQUIRED_FIELDS`: `record_schema` (required); optional `text_field` (default `"text"`),
`min_text_chars` (default 1), `max_identical_fraction` (default 1.0 = disabled). **OLMoE-mix's
`.json.gz` files carry exactly a `text` field.** This **supersedes §4.2b's claim that `text-corpus/v1`
does not exist** — it does not exist *on this branch*, and it does exist on `main`.

**Consequence:** the two things our task needs most are on **opposite branches**. `vendored/v1`
(byte-preserving mirror, upstream witness table) is branch-only; `text-corpus/v1` (raw documents, the
actual upstream format) is main-only. **Neither branch alone can both mirror and publish OLMoE-mix's
documents.** Someone must decide: merge, cherry-pick, or pick one route.

#### 🔴 `main`'s landing-lifecycle fix is NOT on this branch, and deploying the branch's template destroys data

`b63e73a` *"Stop the landing expiry reaching the two prefixes nothing else holds a copy of (#22)"* is
main-only. I diffed `infra/01-buckets.yaml` — the branch still carries **one unfiltered
`expire-landing-objects-14d` over the whole bucket**; `main` replaced it with one rule per family
prefix plus `tests/test_landing_lifecycle.py`. Quoting main's own comment block:

> *"`_dist/` the bootstrap wheel every Batch job pip-installs before it runs. Expiring it breaks the
> validator, the publisher and fsck at once. … `_preserved/` operator rescue copies of corpora that
> were refused and therefore never promoted, so nothing else holds them. As of 2026-08-06 that is
> **364 objects and 122,828,050,823 bytes of `pretrain/olmo-127b` v2, which exists nowhere else on
> AWS.** … **NEITHER IS PROTECTED BY ANYTHING TODAY.** They survive because the DEPLOYED
> configuration was hand-edited away from this template… **That is the realistic way this data dies:
> not somebody adding a broad rule, but somebody redeploying the one already here.**"*

**Do NOT `cloudformation deploy infra/01-buckets.yaml` from this branch.** It would restore the
unfiltered rule and delete 122 GB that exists nowhere else, on a 14-day fuse. Note also that
lifecycle rules **do not shadow one another** and a `Disabled` rule naming `_preserved/` would not
help; a bucket policy would not either, because expiry is performed by the S3 *service*, which every
Deny deliberately lets through.

#### The other main-only commits

`3c0e7ef` *"Refuse a boto3 client at the reader's door"* (`require_s3_adapter()` — grep count 0 on
this branch), `7c818c0` CRC-32C for checkpoint writes, `aa047de` run the suite on a PR, `6f2bac6`
build an image for main, `f0254dd` register the repo with the platform build pipeline, `cb1a12b` +
`41c287a` the platform agent layer, and `3fa594d`/`e2bb252`/`04cdbf3` hardening of the text-corpus
path.

#### 🔴 The deployment trap this creates

`edullm-validator` is targeted by EventBridge **by unversioned name**, so registering a tip-built
revision **cuts over immediately for every dataset in the estate.** A validator image built from
`final-dataset` does not know `text-corpus/v1` and would emit **`unknown-profile`** for any live
dataset declaring it — the mirror image of the `vendored/v1` problem. **Before building a validator
image from this branch, list `s3://edullm-data/_catalog/` and confirm no published dataset declares
`text-corpus/v1`.** Conversely, `main`'s validator cannot validate a `vendored/v1` dataset, and
`vendor/openai-prm800k/v1` **is** published.

**Bottom line:** ~83 of the 160 branch commits are docs/plans/measurements. The genuinely
task-relevant branch-only code is `3545fe7` (`--check-workers`), `9195098` (`--head-workers`),
`db437b6` (HEAD cache), `3b11d7d` (pool sizing), `4d6768e` (shared control-file allowlist),
`d9b8f98` (publisher role), `39a539f` (label-segment gate), `e2fa75a`/`5118308` (verified writes),
and `vendored/v1`. **`hash_workers`/`copy_workers`, streaming reads, and `--promote-workers` are
already on `main`** and are therefore not reasons to choose this branch.

### 3.2c The flag that is NOT in the deployed image, and the one you would have missed

`3545fe7` added **`--check-workers`** (`validate.py:2480-2492`), and it is the largest validate-side
win on the branch: **seven of Gate A's eight round trips per object live in the profile checks**, so
`--head-workers` alone threads 1/8 and Amdahl caps it near 12% — which is exactly why `infra/DEPLOY.md`
reports threading trimming "~12%, not half". Measured: **8.27 s → 0.60 s at 16 workers (13.77×)** on
100 objects at 10 ms simulated latency, with round-trip count identical at every worker count.

Its default is `None`, which **inherits `--head-workers`** — deliberate, because `edullm-validator:14`
passes only `--head-workers 16`, so a default of 1 would have left the fan-out off in production with
no error.

⚠️ **The deployed validator image is `3b11d7d` (tag `3b11d7d3f4e2`), which is ~27 commits behind the
tip and does NOT contain `--check-workers`.** Getting it requires building and registering a new
image — which is the same moment you inherit the two divergence hazards above.

⚠️ **`artifacts/reservoir/publish_driver.py:155` calls plain `Boto3S3.default()` while passing
`hash_workers=16, copy_workers=16`** — i.e. the publish driver is **still unsized** and silently caps
at ~10. If we copy that driver, pass `max_pool_connections=18` explicitly.

### 3.3 Theme breakdown of the 160 commits

Approximate, by commit-subject clustering:

| theme | ~count |
|---|---|
| final-dataset planning / research docs (report, impl plan, dependency graph, tasks, mix) | ~30 |
| reservoir design + Phase 0 measurement + pool sizing | ~25 |
| doc consistency sweeps, retractions, stale-figure fixes | ~25 |
| corpus build / pack / read / receipt (feat + fix) | ~20 |
| ingest transport (HF→S3, segfault, rate limits) | ~15 |
| validator + publish perf and correctness | ~12 |
| infra / IAM / job defs / image build | ~10 |
| registry (17 rows → 40 rows, pins, file shards) | ~10 |
| profiles (`vendored/v1`, family defaults, labels) | ~8 |
| releases | 5 |

**Release commits:** `525a9a9` 0.7.0 → `b7cb3c1` 0.7.5 ("a third commit calling itself 0.7.4 was
the trap I just documented") → `e0984c8`/`b484814` 0.8.0 (merging the two parallel lines) →
`f9460ed` 0.9.1 → deployment recorded in `390a34b` (validator:14, verify:3). Current: **0.9.1**.

---

## 4. Profiles and families — which one for our corpus

### 4.1 Every profile (`src/edullm_data/profiles/`, registered in `registry.py:34-41`)

| profile `NAME` | module | accepts what shape |
|---|---|---|
| `pretrain-tokens/v1` | `pretrain_tokens_v1.py` (752) | **packed uint32 token shards.** Headerless raw LE, `.u32le.bin`. Decodes ~64 KB/shard at seeded offsets as the *declared* dtype and asserts every id against a vocab derived from a published tokenizer |
| `eval-results/v1` | `eval_results_v1.py` (259) | benchmark model outputs / scores; `.jsonl(.gz)` / `.csv(.gz)`. Recomputes row counts; refuses `n_ok == 0` |
| `token-order/v1` | `token_order_v1.py` (251) | uint32 **index vectors** over a parent token pool (curricula, views). Proves permutation / subset / repeating |
| `sft-conversations/v1` | `sft_conversations_v1.py` (268) | `messages[]`-shaped conversation rows, gzipped JSONL; recomputes train/heldout leakage |
| `tokenizer/v1` | `tokenizer_v1.py` (106) | a published HF tokenizer; must contain `tokenizer.json`; `derive_vocab` computes `vocab_size`/`eos_token_id` |
| `vendored/v1` | `vendored_v1.py` (464) | **a byte-preserving third-party mirror.** Upstream paths and filenames intact |

### 4.2 Every family (`families/*.json`, enum at `contracts.py:130`)

`FAMILIES = frozenset({"pretrain", "curriculum", "sft", "eval", "probe", "vendor", "tokenizer"})`

| family | default profile | `validation_required` | `decode_smoke_test` |
|---|---|---|---|
| `pretrain` | `pretrain-tokens/v1` | **true** | `window_bytes 65536`, **`distinct_ids_min 128`**, **`eos_fraction_max 0.05`**, **`zero_run_max 256`** |
| `curriculum` | `token-order/v1` | true | `window_bytes 65536` only |
| `sft` | `sft-conversations/v1` | true | — |
| `eval` | (none — deliberately empty) | false | — |
| `probe` | `eval-items/v1` | false | — |
| `vendor` | `vendored/v1` | **false** | **absent entirely** |
| `tokenizer` | `tokenizer/v1` | false | — |

### 4.2b ⚠️ The spec advertises 15 profiles. Only 6 are registered. Do not plan around the other 9.

`docs/dataset-creation/DATASET-STANDARD.md:506-520` tabulates **fifteen** profiles, and
`skill/SKILL.md:167-176` lists them as if selectable. `registry.py:34-41` ships **six** (§4.1).
Publishing into any of the other nine raises `ProfileError`.

**Unregistered on THIS BRANCH, verified absent:** `text-corpus/v1`, `eval-items/v1`, `annotations/v1`,
`weights-sidecar/v1`, **`tabular/v1`**, `media/v1`, `metrics-timeseries/v1`, `provenance-log/v1`,
`distribution-artifact/v1`, `experimental/v1`.

Two of these matter to us:

- **`tabular/v1` does not exist on EITHER branch**, so "publish the upstream `.csv.gz`
  document-boundary sidecars as a `tabular/v1` group" is **not available without writing the
  profile.** See §6.4 on boundaries.
- **`text-corpus/v1` does not exist on this branch — but it DOES exist on `origin/main`, and it is
  the profile for OLMoE-mix's actual format.** See **§3.2b**, which is the most important finding in
  this document. On this branch alone, raw documents can only be *staged* to landing `_ingest/`, or
  mirrored via `vendored/v1` as a provenance artifact.

⚠️ Note `families/probe.json` defaults to `profile: "eval-items/v1"` — **a family whose default
profile is unregistered.** The `probe` family cannot publish at its own default.

### 4.3 The answer to the actual question

**An already-tokenized external pretraining corpus of uint32 shards → `pretrain` family,
`pretrain-tokens/v1` profile.** Not `vendor`. Reasoning, from the families' own notes:

- `families/vendor.json` grants exactly two exemptions and says so explicitly: *"shard naming is
  EXEMPT because renaming destroys upstream verifiability… Content-addressed groups are exempt for
  the same underlying reason. **The exemptions are from naming rules ONLY.**"* A vendored tree
  "keeps upstream's structure verbatim; imposing our split layout on it would destroy the
  byte-for-byte correspondence."
- So `vendor` is for a **mirror you do not intend to train from directly** — it makes no
  train-readiness claim (`vendored_v1.py:3-5`: *"It does not make the payload train-ready and it
  does not invent a schema for the upstream records"*), it has **no `train`/`val` partitions**, and
  `validation_required: false` means **no held-out split**. A `vendor` dataset is not
  `build_mixture`-able and `read.dataset_paths` routes it through the "no trainable split declared"
  path (`contracts.py:149-156`).
- If we re-shard and rename into `train-NNNNN.u32le.bin` with a val carve — which is what a
  trainable release requires — **we are no longer preserving upstream layout**, so `vendored/v1`'s
  central claim is false and its witness checks would reject us anyway (see §5.2).

**An untokenized text corpus** (which is what OLMoE-mix actually is) **needs no profile at all on
the way in.** It is not published as a dataset; it is *staged* to
`s3://edullm-landing/_ingest/<name>/` and consumed by `corpus_build.py`, which emits `.u32le.bin`
shards that are then published under `pretrain` / `pretrain-tokens/v1`. That is the reservoir
playbook end to end.

**`vendor` / `vendored/v1` is the right home for one thing only:** if you want an auditable
byte-for-byte mirror of the upstream `.json.gz` tree as a published artifact (provenance), publish
*that* under `vendor`, and publish the tokenized shards separately under `pretrain`.

---

## 5. Validator gates — every check, and which ones an external corpus trips

### 5.0 Three structural facts before the check list

**(a) The letters name whole gates, not checks.** Gate A = `validate.py:1` (publish-time airlock).
Gate B = `fsck.py:1` (weekly post-publish decay sweep; **never reads a payload byte**). **Gate C
("rebuild spot-check", `DATASET-STANDARD.md:773`) IS NOT IMPLEMENTED** — it exists in the spec and in
no source file. Within Gate A, checks are named by their kebab-case violation `code`.

**(b) Nothing short-circuits** (`validate.py:99-102`) — one run surfaces every problem. Verdict is
`not violations and not incomplete` (`:113-115`). Good: one round trip tells you everything.

**(c) 🔴 SKIPPING DEDUP AND DECONTAMINATION IS COMPLETELY INVISIBLE TO EVERY GATE.** There is no
Gate A check for near-duplicates, MinHash clusters, or eval contamination — verified by exhaustive
grep of `validate.py` and all six profiles. `_dedup/` is an **optional** control prefix
(`contracts.py:195-198`), never required. The only duplicate detection in the entire pipeline is
byte-identical **whole-shard** digests, **scoped per-group**. So the scope decision to skip them will
not be caught, questioned, or recorded anywhere by this repo. That is a risk we carry ourselves, and
it should be written into `notes=`/`limitations=` at publish time, because it is exactly the class of
claim §6.2 says a consumer cannot recompute from the artifact.

### 5.1 The gates

Gate A is the orchestrator in `validate.py` (2646 lines). Layers, from cheapest:

1. **Naming / purpose / schema** (`contracts.py`) — `dataset_id` must be exactly `<family>/<name>`
   with one `/` (`validate_dataset_id`); family must be in the 7-enum; `purpose` must be 20–300
   chars and survive a normalized blocklist (`""`, `todo`, `tbd`, `data`, `dataset`, `see readme`,
   … — punctuation is stripped first so `"TODO."` cannot route around it).
2. **Manifest exhaustiveness, both directions** — every listed object is a manifest entry and every
   entry exists. Recomputed by LIST.
3. **Per-entry SIZE recompute** (`validate.py:399-431`) — `s3.head` for `ContentLength`, compared
   to the declared `bytes`. Then **set-membership on the declared digest.**
4. **Arithmetic + extension identity** (`manifest.py`) — `count.value × dtype_size == bytes`
   exactly, and the extension must match the real bytes (the `.npy` honesty rule).
5. **Magic-byte sniff** — declared format vs real leading bytes.
6. **Profile CHECKS** — the value-domain layer, threaded via `--head-workers`.
7. **Partitions / splits** — glob-based `by: "path"` partitions; an empty split is `empty-split`.
   The four-form partition set is **closed** and `by: "label"` does not exist
   (`validate.py:653-658` rejects a label-named partition as `empty-split`).
8. **Seal + hash chain** — `promote()` writes `_VALIDATED.json` with `dataset_sha256` rooted over
   per-group `manifest_sha256`; `read.verify_seal` recomputes it on every read.

**⚠️ The one gap, and `CLAUDE.md:120-131` states it outright: Gate A never re-reads payload bytes
for `pretrain-tokens/v1`.** `s3.hash_object` has exactly one non-definition caller —
`publish.py:280`, the **PRODUCER**. `fsck.py`'s docstring says "never a payload byte." **So a
manifest `sha256` is a producer assertion no gate falsifies.** What defends integrity instead: the
airlock IAM Deny, S3 durability, CRC64NVME. **Do not restate the recompute rule as a payload
re-hash.**

**The exception is `vendored/v1`, which DOES stream-hash every payload object at Gate A**
(`vendored_v1.py:8-10`, and the code at `:310-366`): it HEADs, `hash_object`s, HEADs again, and
compares ETags before/after to catch a same-size replacement riding through on a stale manifest
digest. That ETag is then passed as `CopySourceIfMatch` at promotion. **This is a cost, not a
bonus, for a large corpus** — see §5.2.

### 5.2 What an external corpus is likely to trip

Ordered by likelihood.

| gate | trips? | why, and what to do |
|---|---|---|
| **`eos_fraction_max = 0.05`** (`families/pretrain.json:46`) | **LIKELY** | Enforced at `pretrain_tokens_v1.py:606` (strict `>`). `corpus.py:161-172`: a packed shard's EOS fraction **is** `1 / mean_doc_tokens`, so **0.05 == a 20-token mean-document floor**. OLMoE-mix contains StarCoder and algebraic-stack — short code files and short pes2o abstracts push this. 🔴 **AND IT IS SILENTLY SKIPPED WHEN `eos_token_id` IS NOT AN INT** (`:604`, verified: `if isinstance(eos_id, int) and not isinstance(eos_id, bool)`). **So this check and the tokenizer dependency are COUPLED: fixing the tokenizer is what ENABLES the EOS gate.** A corpus with no resolvable tokenizer passes the EOS check vacuously. This is the mechanism behind `superbpe-tokenizer-has-no-eos` — two published corpora already skipped it |
| **`distinct_ids_min = 128`** | possible | 3-stage chain, all verified: (1) `_bound` (`:111-137`) group→family→constant, floor may only RISE (`max(val, fam)`, `:129-130`); (2) vocab cap `_cap_min_distinct_by_vocab` (`:161-188`) = `min(min_distinct, max(16, vocab_size // 16))` — binds only below vocab 2048, so **useless at 100k vocab**; (3) sample scaling `min(min_distinct, max(n // 4, 2 if n > 1 else 1))` (`:592`). The floor of **2** is load-bearing — `max(n//4, 1)` collapses to a vacuous 1 for n≤4 |
| **`zero_run_max = 256`** | **LIKELY on external shards** | A *contiguous-run* test at `>= 256` (**`>=`, not `>`** — `:630`). It was a density test once and **measured punctuation, because dolma2 maps id 0 to `!`** — it rejected two healthy prose shards at 0.0106/0.0108 against a 0.010 bound, whose zeros were 30 scattered singletons with longest run 1. The run form is tokenizer-independent and strictly more sensitive: a 4 KiB hole in a 64 KiB sample is 6% of tokens (under a 10% density bound) but has run length 1,024. **Externally-processed shards with any padding region or writer-side zero fill trip this immediately** |
| **duplicate-shard-digest** | **LIKELY if you carve val by copying** | `validate.py:747-756`. 🔴 **PER-GROUP, verified**: `seen_sha = set()` is declared at `validate.py:717` **inside `_validate_group`** — a fresh set per group. The dataset-level `all_shas` dict exists and is populated, but the only check reading it fires **exclusively** on the `"PARENT:"` sentinel (`:759`); `my_shas` is populated and **never read by any check**. **So two byte-identical shards in two different groups of the same dataset produce NO violation.** The 150B corpus shipped 6 val shards that were byte-copies of train shards = 100% leakage; Gate A caught 5 of 6 **only because it was ONE group**. Carve val by *content*, never by copying |
| **`count.value × dtype_size == bytes`** | weaker than it looks | `manifest.py:602`. ⚠️ **This is NOT a dtype check** — its own docstring (`manifest.py:611-617`) says `publish()` derives `count = bytes // dtype_size`, so the identity **collapses to `bytes % dtype_size == 0`**, which uint16 and uint32 satisfy equally. A uint16-vs-uint32 lie is caught only by `dtype-too-narrow-for-vocab` (`validate.py:1579-1592`), which is **one-sided** — declaring wider than needed is legal. Silently skipped when unit ∉ {tokens, indices}, `dtype_size is None`, or `codec != "none"` |
| **extension/magic honesty** | **HARD if we keep upstream `.npy` names** | Two orchestrator-level checks, **neither exempted by any profile**: `extension-format-mismatch` (`validate.py:740` → `manifest.py:490-496`, `.npy` requires `header_bytes >= 1`) and `fixed-width-dtype-in-nonraw-container` (`validate.py:1557-1569`, fires if you dodge the first by declaring `container: "npy"`). Plus `npy-magic-bytes` recomputing the **first 8 bytes** against `b"\x93NUMPY"` (`pretrain_tokens_v1.py:658-691`). **Fix = rename to `.u32le.bin` and declare headerless.** Note the branch's own finding: dolma sets `MEMMAP_EXTENSION = ".npy"` but writes via `np.memmap`, so `np.load()` on its output *fails* — the `.npy` files are dolma's normal output and this rule is a rule against dolma's naming |
| **`unlisted-object-dataset-level`** | **HARD on any HF repo cruft** | `validate.py:1227-1236` LISTs the whole dataset prefix. 🔴 **Only a DEPTH-0 `README.md` is a control file** (`_is_control_key`, `validate.py:229-250`) — `manifest.json` is control at depth 0 **and** depth 1; everything else is depth-0 only. So a `data/README.md`, `.gitattributes`, or stray `config.json` under the prefix **is payload and will be reported**. The docstring records why depth anchoring was added: matching a basename at any depth let `sneaky/README.md` and `sneaky/dataset.json` hide from the sweep entirely |
| **tokenizer `depends_on` resolvable** | **BLOCKER if omitted** | `publish.py:867-875`: *"Required in practice for a pretrain corpus: without a resolvable tokenizer the decode smoke test cannot recompute its bound and Gate A rejects the dataset."* Pass `tokenizer="tokenizer/dolma2-bpe"`. It resolves in `data_bucket` and pins by `manifest_sha256` |
| **`missing-required-split`** | **BLOCKER — this deleted a corpus** | `pretrain` declares `validation_required: true` and `train`/`val` partitions. `pretrain/olmo-mix-1124-31b/v1` (31B, 218 shards) **was DELETED** because it had no `val` split and, being frozen, could not gain one. **Carve a val split at build time or the release is unpublishable** |
| **label-segment legality** | possible | a `#` in any path segment truncates an `s3://` URI at the fragment; `build_domain_slug_map` raises on slug collision |
| **README / control files** | no | `README.md` is a **control file** (`_CONTROL_BASENAMES`/`CONTROL_BASENAMES`), never a manifest entry, never in the hash chain. It is DERIVED — `promote()` renders it from `dataset.json` via `readme.py`. Control prefixes: `CONTROL_PREFIXES = ("_catalog/", "dependents/", "_dedup/", "_licenses/")` (`contracts.py:213`) |
| **minimum shard count / tiny shards** | watch | `test_tiny_shards.py` exists. Two 20-byte shards caused *both* the duplicate-digest and eos-fraction failures on the 150B publish; dropping both cost 10 tokens and cleared it |
| **`seq_len`/8192 alignment** | **NO — free** | `pretrain_tokens_v1.py:458-460` **skips** the alignment check unless the group declares `seq_len`, and the published corpus does not. Shard size is free. (An earlier claim that 8192 alignment is mandatory was retracted) |
| **`vendored/v1` witness checks** | **only if you use `vendor`** | Requires a non-empty `upstream_files` witness list (relative path + `bytes` + lowercase sha256) fixed *before* transfer, plus `vendor_root`, and `upstream{name,uri,revision,retrieved_at}` with **no TODO/unknown/tbd placeholders** (`_is_placeholder`, `vendored_v1.py:46-54`). Violation codes: `missing-upstream-file-witnesses`, `bad-upstream-file-witness`, `duplicate-upstream-file-witness`, `missing-upstream-file`, `unwitnessed-vendor-file`, `upstream-size-mismatch`, `upstream-sha256-mismatch`, `upstream-payload-hash-read-failed`, `upstream-payload-changed-during-validation`, `upstream-payload-size-mismatch`, `upstream-payload-sha256-mismatch` |

**Does `vendored/v1` relax anything relative to `pretrain-tokens/v1`?** It relaxes the things that
would matter (no EOS bound, no distinct-ids floor, no zero-run test, no required val split, no
naming convention, no tokenizer dependency) **because `families/vendor.json` carries no
`decode_smoke_test` block at all and `validation_required: false`.** But it *adds* a far more
expensive obligation: a pre-fixed per-file witness table and a **full stream-hash of every payload
object at Gate A**. For a 50B corpus (~2,000 objects × ~100 MB) that is ~200 GB of reads inside the
validator job — and the validator timeout is 14400 s.

**Verdict on profile choice for a trainable release: `pretrain` / `pretrain-tokens/v1`, and satisfy
the gates rather than route around them.** Four reasons, each verified in code:

1. **`vendored/v1` probably cannot finish.** It stream-hashes every payload object at Gate A
   (`vendored_v1.py:312`) **single-threaded** — the module contains no `ThreadPoolExecutor` and never
   reads `ctx.check_workers` (confirmed by grep). Then `promote()` re-hashes every *destination*
   object (`validate.py:2079-2087`), threaded only by `--promote-workers` (default 1). That is ~200 GB
   (50B) or ~400 GB (100B) read **twice**. By contrast `pretrain-tokens/v1` reads ~64 KiB + 8 B per
   object and *is* threaded via `--check-workers`, which the live job def already supplies at 16.
2. **Its witness table would be self-referential here.** `upstream_files` must be non-empty
   (`:212-218`), path-set-equal to the manifest both ways (`:268-283`), and digest-equal to the
   streamed payload (`:351-366`). But **we are re-sharding into uint32, so our shards are not
   upstream's files** — there is no authoritative upstream witness to copy, and we would be writing
   witnesses from our own hashes. That reduces the check to *"prove only that two documents agree"* —
   the exact decoration `vendored_v1.py:303-305` says it exists to prevent.
3. **It gives a uint32 corpus essentially zero content checking.** Its only payload-content check is
   `check_jsonl_samples`, gated on `.jsonl` (`:379`). No vocab-range, no zero-run, no npy-magic, no
   distinct-ids, no EOS. **Combined with skipped dedup and decontamination, nothing anywhere would
   have decoded a single token of a 100B-token corpus before it is trained on.** An all-zeros or
   wrong-endian region would publish clean.
4. **The exemptions buy nothing we need.** They are naming, split-from-filename, and labels —
   `families/vendor.json`: *"The exemptions are from naming rules ONLY."* We control the shard names,
   so adopting `<split>-<NNNNN>.u32le.bin` costs one line in the writer and clears all of those at
   once, **and** simultaneously clears the extension/magic checks that *neither* profile exempts.

Also note `families/vendor.json` ships **five `"TODO-verify…"` placeholders** (`vendor_root` plus
`upstream.{name,uri,revision,retrieved_at}`) and `_is_placeholder` (`vendored_v1.py:46-54`) rejects
any string containing `"todo"` — so the vendor family cannot publish at its own defaults, and
`retrieved_at` must be tz-aware ISO-8601.

### 4.4 🔴 An unclosed gap in the standard: family and profile are independent

**Verified by grep — no check anywhere asserts that a group's `profile` matches its family's
`defaults.profile`.** The two are resolved from different places:

- **Bounds and `validation_required` come from the FAMILY**, keyed on `dataset_id.split("/")[0]`
  (`_family_defaults_for`, `validate.py:1126`).
- **CHECKS come from the group's `profile` field** (`validate.py:812`).

So `vendor/olmoe-mix-0924-50b` with `profile: "pretrain-tokens/v1"` would yield
`validation_required: false` (no val split required) **and** bounds falling back to the profile
constants **16 / 0.5 / 256** instead of the pretrain family's 128 / 0.05 / 256 — a **10× looser EOS
bound**. Worse, `_bound`'s anti-loosening clamp is **inert in that configuration**, because it is
guarded on `fam is not None` (`pretrain_tokens_v1.py:127-134`), so a group override of
`max_eos_fraction: 0.99` would be accepted unclamped.

**I am flagging this so it is recognized, not recommending it.** Taking that route means the corpus is
validated at 10× the EOS bound our own family file declares, with **no record anywhere that anything
was loosened** — which is precisely the failure mode `_bound`'s docstring says it exists to prevent,
and the same shape as the missing-`families/` bug that published a corpus at 50% EOS.

The inverse (`pretrain/…` + `profile: "vendored/v1"`) is a trap rather than a loophole: it **keeps**
`validation_required: true` (so the val-split requirement still fires) while switching off every
decode check — the worst of both.

---

## 6. Publishing mechanics we will use

### 6.1 Address shape and the 50B/100B question

`<family>/<name>/<version>/`. `dataset_id` is `<family>/<name>` only — **the version segment is
allocated, never typed** (`validate_dataset_id`'s own error message says so). `publish()` auto-allocates
`vN`; `expected_version` is an opt-in fixed reservation that resumes a matching landing
`dataset.json` and **fails rather than silently allocating another version** if it differs.

`families/pretrain.json` notes: *"Names carry corpus plus token budget, because the budget is the
axis that distinguishes siblings (`pretrain/dolma2-150b`, `pretrain/fineweb-edu-10b`)."*

**Therefore: 50B and 100B are TWO NAMES, not two versions of one name.** e.g.
`pretrain/olmoe-mix-50b` and `pretrain/olmoe-mix-100b`, each at `v1`. Three independent reasons:

1. The budget **is** the distinguishing axis in this family (quote above).
2. **`vN` asserts a RELATION, and `publish()` hardcodes it.** `contracts.py:503-514` requires
   `version = {id, relation ∈ {supersedes, extends, sibling}, of}`, and **`publish.py:547` hardcodes
   `relation: "supersedes"`** with `of = _prev_version(version)`. So a `v2` mechanically claims it
   *supersedes* `v1` — false here, since both releases stay live. **There is no kwarg to override
   the relation** (verified: `grep relation src/edullm_data/publish.py` → only line 547).
3. **Frozen means frozen** — the only sanctioned in-place write is a descriptive-keys-only backfill,
   guarded by an assertion that `groups`/`manifest_sha256`/`inventory` stay byte-identical.

**Name rules, mechanically enforced** (`contracts.validate_name`, verified by grep):
`_NAME_RE = ^[a-z0-9]+(-[a-z0-9]+)*$` (`contracts.py:228`), **2–5 words**
(`_MIN_WORDS = 2`, `_MAX_WORDS = 5`, `contracts.py:230-231`), no dates —
`_YEAR_RE = (?<!\d)(?:19|20)\d{2}(?!\d)` (`contracts.py:247`) — no version tokens (`v1`–`v99`,
`final`, `latest`, `new`, `fixed`), no content-free words (`test`, `data`, `results`, `misc`), no
relative words (`big`, `improved`, `best`).

**Candidate names, checked against those rules:** `pretrain/olmoe-mix-50b` (3 words) passes.
`pretrain/olmoe-mix-0924-50b` (4 words) **also passes** — `0924` is four digits but not `19xx`/`20xx`,
so it survives `_YEAR_RE` and the bare-ordinal ban, exactly as `olmo-mix-1124-31b` does. Preferring
the `0924` form preserves the upstream release code and matches the existing precedent.

⚠️ Do **not** put `vendored` in a `pretrain/` name — it is misleading, since `pretrain` is the
train-ready route and `vendor` is the mirror route (§4.3).

### 6.2 `publish()` signature (`publish.py:832-857`) — verified in code

```python
publish(
    source,                      # str | Path
    *, dataset_id, purpose, profile, s3, created_at,
    tokenizer=None,              # "tokenizer/dolma2-bpe" — REQUIRED in practice for pretrain
    data_bucket="edullm-data",
    landing_bucket=LANDING_BUCKET,
    owner=None,
    group_meta=None,
    build_executor=None,
    env=None,
    max_version_attempts=8,
    hash_workers=1,              # SET TO 16
    copy_workers=1,              # SET TO 16
    sources=None,                # [{name, share?, tokens?, documents?, license?, uri?, scope?}]
    about=None,
    notes=None,                  # STILL PRESENT — see below
    limitations=None,            # [{kind, ...}]
    license=None,
    expected_payload=None,       # immutable witness table, for mirrors
    expected_version=None,       # fixed reservation
) -> PublishPlan
```

**Contradiction resolved — and my first reading was wrong.** Commit `d9b8f98`'s subject *"README
dropped `notes` entirely"* describes **a bug that commit FIXED**, not a removal. Verified by reading
`readme.py:219-237`: `_notes_section`'s docstring says `notes` *"used to be accepted by `publish()`,
stored in `dataset.json`, and then **silently dropped here** — so nothing a producer put in it ever
reached a consumer."* It is now rendered at `readme.py:358`, between License and Limitations.
**`notes=` is live end-to-end. Use it.** It was found while publishing `pretrain/reservoir-dolma2`,
whose `notes` carries the mixed-license (share-alike) disclosure — exactly the class of claim a
consumer cannot recompute from the artifact. **This is where our "we did not tokenize this ourselves"
statement belongs.**

None of `sources`/`about`/`notes`/`limitations`/`license` adds a validator-required field — they
are read only by the README generator. `sources`/`license` fall back to family inherited values.

### 6.3 The publish path we should copy

`HANDOFF.md:2241-2244`, the proven playbook (written for a legacy migration, and it is the closest
match to a copy-and-publish):

> broker-copy headerless `.npy`→`.u32le.bin` into `s3://edullm-landing/_migrate/<name>/tokens/`,
> ship wheel+driver+families to `_dist/`, then Batch submit `_dist/publish_driver.py` via the boto3
> bootstrap with `PUB_*` env (incl. `PUB_HASH_WORKERS`/`PUB_COPY_WORKERS=16`) and
> `--timeout attemptDurationSeconds=7200`.

⚠️ **The last clause is now wrong twice:** EventBridge auto-validation is **DISABLED**, and the
validator job def is at **14400 s**, not 7200. And `edullm-dataset-publish:1` exists as a
general-purpose publisher job def (`infra/10-dataset-publish-jobdef.md`,
`infra/10-dataset-publish-policy.json`) — **use that rather than reinventing a driver.**

⚠️ **Renaming caveat:** `.u32le.bin` silently breaks remote `.csv.gz` sidecar lookup on the
consumer side (`.replace(".npy", ".csv.gz")` — `docs/PLATFORM-INTEGRATION.md:102`). If the upstream
ships sidecars, they will not be found under the new extension.

### 6.4 Consumer constraints that bind shard layout

From `docs/CONSUMER-CONTRACT.md` and the proven training run (`HANDOFF.md:353-367`):

- **Explicit `dtype`, always.** OLMo-core defaults to `uint16` while these corpora are `uint32`, so
  an inferred dtype **halves the token count silently**. `ResolvedSplit.numpy_dtype` emits `"<u4"`
  because `np.dtype("uint32")` uses HOST byte order.
- **Explicit path list, `expand_glob: false`.** The proven run used `NumpyFSLDatasetConfig` with 41
  explicit paths. Globs diverge from `fnmatch` — `*` does not cross `/`.
- OLMo-core memmaps from byte 0 and derives token count from raw file size, so a real `.npy` header
  corrupts both leading tokens and the count.
- **`build_mixture` is scoped to ONE group of ONE dataset.** Real/synthetic must be fused into the
  `source` label, not separate groups. Mixing two corpora risks combining different tokenizers
  whose vocab sizes are close enough that every id still looks valid.
- `PATH_LABEL_KEYS` is exactly **two levels deep**, so "stage" cannot be a third path segment —
  which is why the 1.0T plan publishes two datasets rather than one.

### 6.5 The PRM800K precedent

`docs/PRM800K-INGEST.md` (181 lines) + `src/edullm_data/ingest_prm800k.py` (1096) +
`infra/06-prm800k-ingest-policy.json` + trust policy + `tests/test_ingest_prm800k.py` +
`tests/test_prm800k_ingest_policy.py`. This is the branch's only worked external-vendor ingest: it
streams four upstream files into landing staging **with byte and digest witnesses**, and publishes
under `vendor` / `vendored/v1`. Entry point `edullm-prm800k-ingest`
(`pyproject.toml:113-115`). **It is the model for the witness-table discipline, and it is four
files — it is not a model for a 7.5 TB, 3B-document corpus.**

---

## 7. AWS Batch, image builds, branch triggers, job defs

### 7.1 Image builds — the trigger is the trap

**Container images build ONLY from `edullm/**` branches — NOT from `main`, and NOT from
`agent/**`.** A merge to `main` builds nothing, **silently**, and a submission naming that commit is
refused for having no image. The agent-worktree namespace `agent/<id>/<slug>` does not trigger a
build either.

Dispatch explicitly:

```bash
gh workflow run edullm-platform-build.yml --repo edu-llm/edullm-data --ref <branch>
```

Workflow file: `.github/workflows/edullm-platform-build.yml` (the only workflow in the repo).

**Our branch `final-dataset` is checked out from `origin/edullm/final-dataset-phase0` — the remote
name is in the `edullm/**` namespace and DOES trigger a build; the local name `final-dataset` does
not.** Push to the `edullm/`-prefixed remote branch, or dispatch by explicit `--ref`.

### 7.2 Job definitions

| job def | rev | purpose |
|---|---|---|
| `edullm-validator` | **14** | Gate A + `promote()`. `--head-workers 16`, timeout **14400 s**. Runs as `sbsandbox-intern-edullm-dataset-validator` — the only principal that can write `edullm-data` |
| `edullm-reservoir-verify` | **3** | deep verify, `HASH_WORKERS=8` |
| `edullm-dataset-publish` | 1 | general-purpose publisher (0.7.5 image) |
| `edullm-fsck` | 6 | Gate B weekly sweep (wheel 0.6.0) |
| `edullm-reservoir-ingest` | 7 | HF→S3 transport (wheel 0.6.3) |
| `edullm-reservoir-build` | 1–9 | build driver; idle |

**EventBridge targets job defs by UNVERSIONED name**, so registering a new revision cuts over
immediately — for better and worse. Registering `edullm-validator:14` cut over every future
auto-promotion, which is why a preflight ran against the exact digest first
(`edullm-validator-preflight:2`, 10 in-container assertions: version, all six profiles incl.
`vendored/v1`, `## Notes` rendering, `families/` at 0.05, both threading params, `head_workers`
default still **1**, the shared HEAD cache, the sized connection pool).

**Rollback is not "leave rev 13 in place"** — the automatic path always takes the top revision, so
a rollback means registering a new revision mirroring the old one.

### 7.3 The four Batch gotchas (`CLAUDE.md`, all four still bite for a wheel-from-S3 job)

1. **The Batch image has no `aws` CLI** — download the wheel/driver/families with **boto3**.
2. **`families/` must travel in the wheel** — FIXED via `pyproject.toml:130-131` force-include.
   Verified in a clean venv. A missing families dir **does not raise**; it silently falls back to
   each profile's laxer constant, so it fails only in production.
3. **pip requires the PEP-427 wheel filename** — keep `edullm_data-<version>-py3-none-any.whl`
   end-to-end; a renamed `w.whl` is rejected. A wheel-bootstrapping job def names the wheel **by
   exact filename**, so shipping a new wheel changes nothing until that def is re-registered.
4. **Single-threaded publish times out** — use `hash_workers`/`copy_workers` and pass
   `--timeout attemptDurationSeconds=7200` (raise it; see below).

**There is NO maximum Batch job timeout** — corrected 2026-08-01 against the AWS Batch user guide:
*"There's no maximum timeout value for an AWS Batch job"*, `attemptDurationSeconds` "must be at
least 60 seconds", and by default Batch has no job timeout at all. **The 3600 s that killed a job
was a value we set ourselves.** The one real ceiling (14 days, Fargate) does not apply — our defs
are EC2. Shard work for blast radius, not because of a platform maximum.

**Exit codes:** 137 is our own SIGKILL/termination (or a timeout); **139 is SIGSEGV** and means the
pyarrow `pre_buffer` class of bug. `docs/` records "exit 139 is not exit 137, and the difference is
the diagnosis."

### 7.4 IAM and the airlock

- Producers write **only** to `s3://edullm-landing`. The validator role (ecs-tasks trust only — no
  human/intern session can assume it) is the **only** principal that can `PutObject` to
  `s3://edullm-data`.
- Live policy is `edullm-data-airlock-v2`: `OnlyValidatorWrites` Deny (Put, validator/deployer
  exempt) + **`NobodyDeletesPublishedData` Deny (Delete*, NO exemption — binds the validator too)**
  + `AllowS3InventoryDelivery`.
- **After any live test that touched permissions, re-verify the Deny still fires** (intern
  `PutObject` to `edullm-data` → `AccessDenied`, explicit deny) before calling the task done.
- **`iam:simulate-principal-policy` LIES for the intern role** (11 known false denials) —
  smoke-test a permission live, never trust the simulator.
- Reusable least-privilege roles for a new ingest: `infra/08-reservoir-ingest-policy.json` +
  trust, `infra/09-reservoir-publish-policy.json` + trust, `infra/10-dataset-publish-policy.json` +
  trust, `infra/11-final-dataset-build-policy.json`, `infra/06-prm800k-ingest-policy.json` + trust.
  `infra/09-mirror-bucket-policy.json` is untracked-new (a region mirror).
- The validator role **cannot read the legacy `edullm-datasets` bucket**, so legacy→landing
  rename-copies must be broker-driven, not Batch-driven.

### 7.5 All eduLLM AWS jobs go through the platform

Per the container `CLAUDE.md`: use the **`edullm-platform-runs` skill** (`edullm check`, then
`edullm submit`) for any training run, eval, tokenization or corpus-validation batch job. Not
`aws batch submit-job`, not `mcp__sb-aws__*`. Read-only AWS inspection stays with `sb-aws-readonly`.
Authoring/publishing a corpus belongs to the `edullm-datasets` skill.

---

## 8. Contradictions found, and which side I verified

**Four claims of my own that I corrected during this recon**, listed first because they were wrong in
the direction of under-warning:

| my claim | correction | how verified |
|---|---|---|
| "the branch is 160 commits ahead of main" implying a superset | **Divergent.** `main` is 18 commits ahead of the merge base too, with 19 commits absent here | `git rev-list --count 38bf831..origin/main` |
| "`text-corpus/v1` does not exist" | Does not exist **on this branch**; **exists on `origin/main`** and is the profile matching OLMoE-mix's format | `git ls-tree`, `git show origin/main:.../registry.py` |
| "version is in exactly two places" | **Three** — `.edullm/Dockerfile:23` hardcodes the literal in a build-time assertion | read the file |
| "treat `notes=` rendering as unverified" | `notes` is **live end-to-end**; commit `d9b8f98` *fixed* the drop rather than causing it | `readme.py:219-237,358` |

| contradiction | resolution |
|---|---|
| Validator revision reported as 2 / 8 / 9 / 10 / 12 / 14 / 16 across seven files | **Rev 16 + `edullm-promote:2`** per `LEDGER.md:1557,1563`. **The whole `infra/` tree is stale on this**, including `DEPLOY.md:809` which I first cited as authoritative; `infra/05-validator-jobdef.md` self-declares "HISTORY, NOT INSTRUCTIONS" and calls its own rev-2 claim "the single most-copied error in this repo's docs". `artifacts/orchestration/` is the only current record. Verified by grep; **not** verified against live AWS |
| `HANDOFF.md` START HERE says the reservoir is unpublished, blocked on `bundle-set-mixed-wheel-versions` | **Stale.** `infra/DEPLOY.md:823` shows `pretrain/reservoir-dolma2/v1` sealed at 10,049 objects / 1,004,872,007,680 bytes. Verified in DEPLOY.md |
| Memory: "publishing to landing auto-promotes" | **False on this branch.** `edullm-landing-manifest-created` is DISABLED (`CLAUDE.md:172-174`, `HANDOFF.md:2185`). Verified in both docs |
| `d9b8f98` "README dropped `notes` entirely" vs `publish.py:852` | **`notes=` kwarg is present.** I verified the signature in code. The README-rendering behaviour is unresolved — `infra/DEPLOY.md:824` says a live README "still carries `## Notes`" |
| Test count: 786 / 974 / 1,085 / 1,090 / 1,101 / 1,107 | All undated relative to `8e2524a`. **Treat as unverified**; 47 test files exist. Did not run the suite (per instruction and per the no-local-compute rule) |
| CLAUDE.md "60-min job-def limit" | **Explicitly retracted in the same file.** No maximum Batch timeout exists |
| `HANDOFF.md:2244` "EventBridge auto-validates+promotes" in the migrate playbook | **Contradicts the DISABLED rule 60 lines earlier in the same file.** The rule is disabled; the playbook line is stale |
| "8192 shard alignment is mandatory" | **Retracted.** `pretrain_tokens_v1.py:458-460` skips it unless the group declares `seq_len`; the published corpus does not |

---

## 9. What this means for the execution plan — the blocker list

**Ordered by whether it stops the work.**

1. **STOP: the premise is wrong.** `OLMoE-mix-0924` is 7.5 TB of `.json.gz` text, not uint32
   shards. Get the owner to choose: tokenize it ourselves (~days, uses `corpus_*`), or find a
   pre-tokenized dolma2-compatible mix to byte-copy (~hours, uses `publish` only). §0.
1b. **STOP: the branch is divergent and the profile we need is on `main`.** `text-corpus/v1` (raw
   documents — OLMoE-mix's format) exists only on `origin/main`; `vendored/v1` exists only on
   `final-dataset`. **Neither branch alone can do this job.** Decide merge vs cherry-pick vs
   single-route **before** any code. §3.2b.
1c. **DO NOT deploy `infra/01-buckets.yaml` from this branch.** It restores a whole-bucket 14-day
   expiry that reaches `_dist/` and `_preserved/`, deleting 122,828,050,823 bytes of
   `pretrain/olmo-127b` v2 **that exists nowhere else on AWS.** `main`'s `b63e73a` is the fix and is
   not here. §3.2b.
1d. **Before registering a tip-built `edullm-validator`, list `s3://edullm-data/_catalog/`.**
   EventBridge targets the job def by unversioned name, so the cutover is estate-wide and immediate.
   A branch-built validator emits `unknown-profile` for any `text-corpus/v1` dataset; a main-built one
   does the same for `vendor/openai-prm800k/v1`, which **is** published. §3.2b.
2. **BLOCKER: a `val` split must be carved at build time.** `pretrain` declares
   `validation_required: true`; a corpus without `val` is **permanently unpublishable** once frozen,
   and this already caused the deletion of a 31B corpus. Do not carve val by copying train shards —
   duplicate-digest will catch it, and it is 100% leakage.
3. **BLOCKER: freeze the FULL plan before building anything.** Ordinals shift when a source is
   added: one added source renames 98% of shards. If 50B and 100B are built incrementally, the 50B
   release's keys move. Plan both releases as one frozen plan, or make them fully independent
   datasets with independent ordinal blocks.
4. **BLOCKER: `publish()` must run on Batch in-region.** It stream-hashes every byte; off-region is
   0.8 MiB/s.
5. **BLOCKER: auto-promotion is OFF.** Budget a manual `submit-job` of `edullm-validator` (or
   re-enable the shared rule).
6. **BLOCKER: pre-flight EOS fraction AND zero-run bounds LOCALLY, before uploading anything.**
   `eos_fraction_max = 0.05` is a 20-token mean-document floor; `zero_run_max` fires at **`>= 256`
   consecutive zeros**, which externally-processed shards with any padding trip immediately. These are
   the two most likely rejections, and **Gate A discovers them only after 200–400 GB is uploaded,
   because `promote()` is all-or-nothing.** Free mitigation: `corpus_pack.py:1022-1042` recomputes the
   *identical* bounds at build time, reading them from `families/pretrain.json` rather than re-typing
   them, and `:1038` notes it matches Gate A's `>=` exactly.
7. **BLOCKER: publish the tokenizer FIRST, then pass `tokenizer="tokenizer/dolma2-bpe"`.** Without a
   resolvable tokenizer, `missing-tokenizer-field` is a guaranteed reject. 🔴 **And the ordering
   matters for a second, non-obvious reason: the EOS check is silently skipped when `eos_token_id` is
   not an int** (`pretrain_tokens_v1.py:604`). **Fixing the tokenizer is what turns the EOS gate on.**
   A corpus published without one passes EOS *vacuously* — which is how two corpora already shipped
   unchecked. So do step 7 before step 6, or step 6 measures nothing.
7b. **Verify `families/` resolves inside the validator container.** If it does not,
   `_family_defaults_for` returns `{}` (`validate.py:1120-1125`) and every bound silently falls back
   to the profile's laxer constant — validated at 0.5 EOS while `dataset.json` claims 0.05. This is
   `CLAUDE.md` gotcha 2 and it has already shipped a bad corpus once. `.edullm/Dockerfile:31-35`
   asserts it at **image build time**, which is the strongest available guard — use that image.
7c. **Keep the tree ≤2 levels deep and the prefix clean.** `PATH_LABEL_KEYS` is exactly
   `("source", "domain")` (`manifest.py:693`); OLMoE-mix's native `data/<subset>/<file>` layout plus a
   group prefix will exceed it and raise `labels-unnameable-path`. And any HF cruft under the prefix
   (`.gitattributes`, a nested `README.md`) is reported as `unlisted-object` — only **depth-0**
   control basenames are exempt.
8. **BLOCKER: image builds fire only on `edullm/**`.** Push to the `edullm/`-prefixed remote or
   `gh workflow run … --ref <branch>` explicitly.
9. **Do not bump `__version__` mid-build** — `bundle-set-mixed-wheel-versions` is a cheap-tier check
   that will refuse the whole bundle set, correctly, and it must not be waived.
10. **Run `pytest -m network`** before spending money: one registry row with a 404 `config` is
    invisible to the default offline run and fails inside a billing container.
11. **Set the knobs.** The full command surface, verified against the CLI and signatures:

    Validator — `python -m edullm_data.validate` (Gate A) and `edullm-promote:2` (promotion):
    ```
    --head-workers 16          # default 1
    --check-workers 16         # default = --head-workers; threads 7 of Gate A's 8 round trips
    --promote-workers 16       # default 1  (promote job only)
    ```
    Pool is auto-sized to `max(head, promote, check) + 2` when that exceeds 8 (`validate.py:2509-2526`).

    Publish — **Python only, there is no publish CLI** (`pyproject.toml:87-115` ships
    `edullm-data-validate`, `wu-fsck`, `edullm-ingest-reservoir`, `edullm-corpus-build`,
    `edullm-prm800k-ingest`):
    ```python
    publish(source, s3=Boto3S3.default(max_pool_connections=18),   # size it YOURSELF
            hash_workers=16, copy_workers=16, ...)
    ```
    Batch: `--timeout attemptDurationSeconds=21600` for publish, ≥ 14400 for Gate A.
    `retryStrategy attempts: 1` — **no retry**, because `publish()` reserves the version with a
    create-only `dataset.json` and attempt 2 either fails confusingly or lands on `v2`.
    Use `expected_version="v1"` to pin the reservation (PRM800K did).

12. **`put_*_verified` refuses objects ≥ 5 GiB** (`s3.py:175`), because `ChecksumSHA256` is a
    FULL_OBJECT digest for a single PUT but a **COMPOSITE of per-part digests** for multipart — not
    the file's sha256, and not comparable to one. boto3's `upload_file` multipart threshold is 8 MiB,
    so it would silently take that path. Keep shards under 5 GiB (at 25,001,984 tokens ≈ 100 MB, we
    are far under). Also: the bytes-shaped `put_bytes_verified` path has **no live
    deliberate-corruption assertion yet** — its own docstring says to add one before retiring
    anything downstream on its strength.
13. **Free wins available**: skipping the filter removes ~78% of build cost AND the ~6.2× scaling
    ceiling; 50B/100B are only ~2,000/4,000 objects so Gate A is ~0.56 h and not a problem;
    `ChecksumSHA256` on the sink (P7) lets you delete the deep verify entirely; run
    `verify_bundle_set` with no `s3=` to predict the gate verdict for free.
14. **One check worth running on the upstream shards regardless of route.** `1a0912e` found that
    `tokenizers` maps the literal string `<|endoftext|>` in *input text* to id 100257, and
    web-scraped documents contain it (~1 in 2,500) — producing **false document boundaries**, because
    OLMo-core recovers boundaries via `(mmap == eos_token_id).nonzero()`. We are ingesting **someone
    else's tokenization**, and **nothing in this repo would catch it**: Gate A's decode smoke samples
    64 KB per object and does not count EOS. If we ever copy pre-tokenized shards, do a one-off
    EOS-count-vs-document-count comparison on a few of them.
15. **If we pin an upstream revision, verify the BYTES came from the pinned sha.** `b6d9d4f` found
    the pins were defeated at read time: the build LISTed files at the pinned sha then fetched bytes
    from whatever `main` pointed at that morning, because the resolver hardcoded `resolve/main` — and
    **nothing downstream notices**, since the manifest hashes whatever arrived and Gate A passes it.
