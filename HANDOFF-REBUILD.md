# HANDOFF — the labeled rebuild of `pretrain/edu-mix-983b`

**Written 2026-08-09 by the CEO session.** Read this file alone and you can continue.

> ## ▶️ READ ORDER
>
> 1. **This file** — the rebuild's state, and the only file describing the MTLD/curriculum work.
> 2. **`artifacts/orchestration/LEDGER.md`** — every ruling, every measurement, and **~25 CEO errors**
>    recorded against evidence. It is the reasoning behind everything below. **Long, and worth it.**
> 3. `HANDOFF-FINAL-DATASET.md` (2026-08-07) — the corpus *design*. **Its numbers are now STALE**: it
>    predates the 986 B → 936 B correction, the 39,307 → 37,307 shard change, and every `plan_id` move.
>    **Trust its intent, not its figures.**
> 4. `HANDOFF.md` (2,250 lines) — the **reservoir** build. Accurate about that, unrelated to this.

---

## Goal

Build and publish **`pretrain/edu-mix-983b`** — a ~936 B-token dolma2 corpus — plus
**`curriculum/edu-mix-983b`**, an MTLD-ranked easy→hard token-order permutation, so the OLMo-core
curriculum trainer consumes both unchanged.

A prior unlabeled run went **terminal at 169/185 bundles (88.2% of tokens)** with 16 failures. This
rebuild adds inline MTLD labels and fixes every cause of those failures.

---

## Current Progress

### The authoritative numbers — recompute, never copy

| | value |
|---|---|
| **build ref** | **`912f17c`** on `origin/edullm/final-dataset-phase0` |
| **`PLAN_ID`** | **`79e53d1e5e131649`** |
| bundles / shards / tokens | **185 / 37,307 / 932,749,017,088** |
| registry row-sum | 936,000,000,000 |
| tests | **1,574 passing**, 2 deselected |

⚠️ **Row-sum 936.00 B ≠ plan-sum 932.75 B.** `plan_document` re-derives every target to whole
25,001,984-token shards; the 3.25 B gap is quantisation across 132 rows, not a lost draw. **The build
writes the plan figure.**

⚠️ **`plan_id` has moved four times.** `29968a2b` → `364cb4dd` (401 fix) → `79e53d1e` (stackv2 pool).
**Always recompute from the tree you are about to build; never copy a value from prose — including
this file.**

### Registered and live

| artifact | state |
|---|---|
| `edullm-validator:17` | 4 vCPU / **12,288 MiB** / 28,800 s · **no `--promote`** |
| `edullm-promote:3` | 4 vCPU / **12,288 MiB** / 28,800 s · promotes |
| `edullm-dataset-publish:2` | 16 vCPU / 32,768 MiB / 28,800 s · dry-run default |
| `edullm-final-dataset-build` IAM role | 7 statements, `NeverWrite…` Deny intact |
| `edullm-landing-manifest-created` | **DISABLED** (twice tonight — it drifts) |

### In flight at handoff

`edullm-prm800k-image-build:56599af4-…`, tag `912f17cd3bbd`. **The build array has NOT been
submitted.**

---

## What Worked

- **Adversarial division of labour.** Four executives + ~12 workers. **Nine subagent refusals, nine
  real problems** — three of them my own instructions. The single highest-value pattern all night.
- **Double-assigning one question, uncoordinated.** Promote duration went to two agents forbidden from
  citing each other; they converged at 3.1–3.7 h and 3.32 h from different data. **Both found the
  plan's "~1 min" was ~200× wrong.**
- **Validating an instrument against ground truth before trusting it.** The pool measurement predicted
  the *packer's own receipts* to **+0.06%** on a holdout. The one measurement that skipped this step
  was wrong by 2×.
- **Behavioural preflights, not version strings.** A version string failed to identify code **six
  times**. The labels assert fails with `ModuleNotFoundError` on the pre-labels image; the cap assert
  reproduces the OOM at `1 batch, 76.3 MiB`.
- **Mutation-proving every new test.** *"An assertion that has never failed is decoration."* Three
  agents caught their own tests passing on broken code this way.
- **Using a completed run as evidence.** **125 of 132 drawn rows proved their pools at 100% fill with
  `unfilled: []`** — stronger than any footer scan, and free.

---

## What Didn't Work

- **A memory raise as a remedy for an unexplained failure.** Twice a **two-line fix** beat a raise
  (`combine_chunks`, `_ENCODE_BATCH_CHARS`). My approved 15,806 MiB would have given 1.45× against a
  term with **no upper bound**. **Doctrine now: a raise is not a remedy for an unexplained failure.**
- **Trusting a declared number over bytes.** `stackv2-edu`'s pool was **24.5× too high** because a
  report row labelled `common-pile/stackv2` (raw) was joined to a registry `repo` of
  `stackv2_edu_filtered`. **The edu filter keeps 5.33% of raw bytes.**
- **Guards fed unmeasured inputs.** The pool guard **existed and passed** (`108 B < 707 B`). A relation
  between two *declared* numbers cannot detect that one describes a different dataset. My replacement
  greps for the literal `"MEASURED"` — and passed on a trap saying *"did not measure it."*
- **Reasoning from a name.** An `AccessDenied` naming a permissions boundary read as absolute; the deny
  was **conditional** on `--permissions-boundary`, and prior sessions had recorded the working command.
- **A single `ru_maxrss` draw.** Six runs of identical input spanned **2.52–7.28 GiB**.
- **Un-paginated API reads.** `limit=50` returned **48 of 95 files**; the estimate was exactly half.

---

## Key Decisions

1. **One dataset, `pretrain/edu-mix-983b`** — not two staged sets. The registry states the draw is
   **combined stage-1+2 because a source is read once**; slicing it by shards would give **both**
   datasets the wrong mixture. Stage selection is reader-side (OLMo-core takes a per-path budget).
2. **`983b`, not `1t`** — `1t` overstates by 1.75% in a frozen address. `pretrain/final-dataset` is
   **rejected by the validator**: `final` is a version token (`contracts.py:383`).
3. **Ship 936 B; no code backfill.** Every code candidate descends from `the-stack-v2` — ours is the
   Blue Oak re-filter, swallow is a rewrite of the same blobs, stack-edu is pointers to them.
   **Summing any two is not pool growth.** Code 10.95% → 6.2%; the loss lands in the bulk phase, where
   the report's own 64-run sweep says code matters least.
4. **Labels, not text.** MTLD computed inline at tokenize time: **14.2 B/doc ≈ 17 GB** versus **4.13 TB**
   to persist text. Also removes the handoff's "re-walk in tokenization order" step — we *are* in that
   order, so `source_doc` is a counter.
5. **`(config, full-repo-path, row_index)`** as cosmopedia's surrogate id — **not** basename: the same
   basename exists under `data/openstax/` and `data/wikihow/`.
6. **48-wide at 8 vCPU**, array `size=185`. **`--of` is the work decomposition; array size is
   concurrency.** `size=48` against `--of 185` builds **48 of 185 bundles and exits 0**.
7. **`_backup/` is unnecessary.** `publish()` deletes the staged source **only when
   `source_kind == "local"`** (`publish.py:1053`). With an `s3://` source it never fires, so the staged
   shards **are** the safety net, under `_ingest/`'s 30-day rule.
8. **Chunks per shard = `(tokens − 1) // 2048` = 12,207.** `curriculum_loader.py:66` applies its own
   `−1`; `ParentChunkDataset` is standalone and never touches `numpy_dataset.py:679`'s 12,208.

---

## Next Steps

### 1. Launch the build — the only thing between here and a corpus
On image completion: **preflight** (labels behavioural + `_ENCODE_BATCH_CHARS` binding) → **register**
`edullm-reservoir-build` with `memory 14,336`, **`PLAN_ID` recomputed from the built tree**, `--labels`,
digest-pinned → **report the revision by number** → **launch `--array-properties size=185`**.

`T_place` 10 min (the CE cold-starts from 0); `T_run` 2× per-bundle from `startedAt`, **externally
tracked** — Batch leaves `statusReason` **null** on capacity failures, and the app emits **one log line
per bundle**, so **shard `LastModified` deltas are the live progress signal.**

### 2. Report two numbers nobody has
- **The first `DONE` rate** — and re-derive the makespan; every figure quoted against the old
  982.8 B/39,307-shard plan is stale.
- **The labels overhead.** ENG-3 measured **2.80%** on a laptop; AUDIT got **1.66%**. One bundle's wall
  clock settles it free.

### 3. Then publish, in this order
`_backup/` is unnecessary (see decision 7) → **Gate A on `edullm-validator:17`** → **`publish()` as
`pretrain/edu-mix-983b`, profile `pretrain-tokens/v1`** (hyphen — the module is `pretrain_tokens_v1.py`;
the wrong spelling publishes 3.93 TB and fails Gate A on one character) → **promote on
`edullm-promote:3`** → **report the measured promote duration**, which is still DERIVED at 3–4 h.

⚠️ **Re-verify `edullm-landing-manifest-created` is DISABLED in the same breath as any `manifest.json`
write.** It has re-armed twice. **On a third drift: STOP** — that means something is actively
re-enabling it.

### 4. The curriculum dataset
Branch has it built (`corpus_mtld`, `corpus_labels`, `corpus_order`, `artifacts/final-dataset/curriculum_driver.py`).
**`PARENT_MANIFEST_SHA256 = None` and the driver exits 1** until the parent is promoted. Then:
`curriculum/edu-mix-983b`, `{"mtld": "token-order/v1"}`, two groups (train/val), ascending sort,
`depends_on` the exact parent manifest, **`max_order_bytes` declared** (the vector is 3.57× the 512 MiB
default).

🔴 **`block_count` is mandatory.** Omitting it makes the permutation check **vacuous** — a 64-index
permutation passes against a 478 M-chunk parent.

### 5. Blocking infrastructure — no longer deferrable
**The 83-var base64 build payload is ~1,500 chars from a hard `StartBuild` ceiling** (found by
bisection: accepted at 401,875, rejected at 403,043). **The next person to add a source file will find
builds simply won't start, with an error naming neither cause nor margin.** Build a dedicated CodeBuild
project with a real git source.

### 6. Open, named, not hidden

| item | status |
|---|---|
| **`stackv2-edu`'s exit-137** | 🔴 **UNEXPLAINED.** Not the row-group term, not the batch term. Nothing measurable gets it within **6×** of the container. Co-scheduling **refuted** by placement data. It died **1,012 s after its twin finished identical work** — slow accumulation, consistent with allocator fragmentation, **untestable without a Linux container.** **If a part dies again, report — do not raise.** |
| `stackv2-edu` under-delivery | Expected: 58 B against a 61.64 B pool (94.1%), the tightest row |
| `pre-1929-books`, `math-textbooks` pools | Unmeasured; 4.3× and 9.2× headroom — **cannot bite** |
| The `"MEASURED"` grep guard | Satisfiable by the word in a sentence denying measurement. **Require a numeric derivation.** |
| `ReadStats.problems()` / 4-table format divergence | Fixed on branch; verify post-merge |
| Census resume | 3 of 64 children FAILED, **never diagnosed** — read those logs before trusting a resume |
| MTLD's ASCII-only regex | Non-English documents rank as maximally easy. **Declared in `limitations`; not fixable without breaking RegMix comparability.** |

---

## The rules that earned their keep

- **A version string is not a code identity. A docstring is not behaviour. A tag is not contents. A
  timestamp is not contents.** Six, three and three instances respectively.
- **A complete-looking response can be a first page.**
- **Absence of a string is not absence of behaviour** — top-level greps miss function-local imports.
- **A guard fed a wrong input passes while the thing it guards is broken.**
- **A verification step that writes into the artifact it verifies has corrupted the artifact.**
- **Any comparison must first prove it can SEE what it compares** — a `git show` returned zero bytes at
  exit 0 and nearly produced a false "identical."
- **A test proving output is right cannot prove the path is affordable** — 16 byte-identity tests passed
  over a 2 GiB regression.
- **A fix that widens a scope invalidates every uniqueness assumption inside the old scope.**
- **"Was this sized on the reservoir?"** — nine defects.
- **An over-harsh retraction is still an error.** A withdrawn figure later proved 0.82× of reality.
- **A timeout is a statement about your budget, not about the code.**
- **Stop when an instruction requires a mutation nobody named.** Nine for nine.
- **Knowing a failure mode is not the same as not committing it.** Several of my ~25 errors were classes
  I had already written into the ledger as warnings. **A rule in a document does not execute.**
