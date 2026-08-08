# Pipeline scale audit — can the corpus-build pipeline execute a 1.0T-token ingest?

**Date:** 2026-08-07
**Auditor:** subagent (read-only; no pytest, no AWS calls, no data loaded)
**Code under audit:** `/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/final-dataset/`
**Baseline that works:** "the reservoir" — ~253B tokens, 9 bundles / 4,137 shards, published.
**New target:** ~1.0T tokens (~4x), more sources, 2-stage (stage1 ~900B + stage2 ~100B).

## Grading legend

- **MEASURED-IN-CODE** — a number or behaviour I read directly in the source at a cited file:line.
- **MEASURED-ELSEWHERE** — a number from an on-disk artifact, cited by path.
- **DERIVED** — arithmetic on the above; the arithmetic is shown.
- **UNVERIFIED** — I could not confirm it; stated as such.

Every finding: FINDING / EVIDENCE (file:line) / SEVERITY / FIX + effort.
Severity vocabulary: **blocks the build** | **needs code before ingest** | **operational only**.

---

_(sections appended as each question is answered)_
## Q1 — Ordinal allocation and the plan

### F1.1 The plan is computed ONCE, is pure, and is content-addressed. A child never allocates.

**FINDING.** `plan_document()` is a pure function of `(specs, tokens_per_source, val_fraction,
domain_map, registry_meta)`. It calls `shard_plan(targets)` → `allocate_ordinals(plan)` and
serialises every shard path into the artifact. `plan_id` is `sha256(json.dumps(doc,
sort_keys=True))[:16]` — a content address over the whole document, deliberately with **no
timestamp** so regeneration is idempotent. Children fetch the plan from S3 (`_load_plan`) and
rebuild `Bundle` objects with `Bundle.from_plan_entry`, which **parses** the ordinal back out of the
stored path via `parse_shard_name` rather than recomputing it. There is no runtime counter anywhere
in the child path.

**EVIDENCE (MEASURED-IN-CODE).**
- `src/edullm_data/corpus_build.py:182-292` — `plan_document`; purity contract stated at `:196-199`.
- `src/edullm_data/corpus_build.py:244-245` — `plan = shard_plan(targets)`; `refs = allocate_ordinals(plan)`.
- `src/edullm_data/corpus_build.py:289-291` — `plan_id = sha256(canonical json)[:16]`.
- `src/edullm_data/corpus_build.py:342-368` — `Bundle.from_plan_entry` parses `parse_shard_name(path)`, i.e. the ordinal is read, never derived.
- `src/edullm_data/corpus_build.py:663-665` + `:671` — `_cmd_run` loads the plan from S3 before doing anything.
- `src/edullm_data/corpus.py:315-360` — `allocate_ordinals`; the ordinal counter is `ordinal_by_split`, keyed **by split only**, and it is global across sources.

**SEVERITY:** operational only (this part works).

---

### F1.2 ⚠️ **BLOCKS THE BUILD: adding one source to a plan renames a majority of already-written shards.**

**FINDING.** `allocate_ordinals` sorts the plan rows by `(source, domain or "", split)` and then
assigns ordinals **densely and ascending in that sorted order, from a single per-split counter**:

```python
for source, domain, split, n_shards in sorted(plan, key=lambda t: (t[0], t[1] or "", t[2])):
    nxt = ordinal_by_split.get(split, 0)
    for i in range(n_shards):
        refs.append(ShardRef(..., ordinal=nxt + i))
    ordinal_by_split[split] = nxt + n_shards
```

So the ordinal block a source receives is a function of the **cumulative shard count of every
source that sorts before it alphabetically**. Insert a new source whose `source_label` sorts early
(e.g. `arxiv`, `code-stackv2`, `dclm`) and every source after it shifts by that source's shard
count. The shard *path* is `tokens/<source>[/<domain>]/<split>-<ordinal:05d>.u32le.bin`
(`corpus.py:293-312`), and the path is what goes into the manifest entry and therefore into
`manifest_sha256`. Concretely: a renamed shard means (a) a new `plan_id`, (b) every receipt from the
old plan is rejected because `bundle_is_done` compares `receipt.plan_id != plan_id` and the shard
path set (`corpus_build.py:413-417`), and (c) all the already-uploaded objects are orphans in
landing. **Every byte of tokenize work done before the source was added is thrown away.**

There is nothing in the code that mitigates this. There is no "append-only" or "reserve a block"
mode; there is no `ordinal_base` argument; there is no per-source ordinal namespace.

**EVIDENCE (MEASURED-IN-CODE).**
- `src/edullm_data/corpus.py:353-359` — the sort + single per-split counter, quoted above.
- `src/edullm_data/corpus.py:311-312` — the ordinal is inside the object key.
- `src/edullm_data/corpus_build.py:413-417` — a receipt from another `plan_id` or with a different shard-path set is "not done".
- `src/edullm_data/corpus_build.py:13-17` — the module docstring states the design intent (ordinals from a whole-plan allocation) but not this consequence.

**Also note the ordinal ceiling.** `_ORDINAL_MAX = 99_999` and `SHARD_RE` requires **exactly five
digits**, so a six-digit name does not parse as a shard at all and `parse_shard_name` returns
`None`, silently un-split-checking the object.
- `src/edullm_data/corpus.py:290`, `:305-310`; `src/edullm_data/manifest.py:659-661`, `:675-683`.

**DERIVED headroom at 1.0T.** At the decided shard size of 50,003,968 tokens
(HANDOFF-FINAL-DATASET.md:181), `1.0e12 / 50,003,968 = 19,998` train shards. Plus val at
`VAL_FRACTION 0.005` → ~100 shards. **Ordinals are per-split** (`ordinal_by_split` keyed by split),
so `train` needs ~20,000 of its 100,000 slots and `val` ~100. Headroom is 5x. **Not a blocker at
1.0T; it becomes one past ~5T on one split.** Note the current constant in code is still
`SHARD_TOKENS = 3052 * 8192 = 25,001,984` (`corpus.py:89`), which at 1.0T gives **40,000 train
shards** — still under the cap, but it doubles Gate A's object count vs. the decision in the
handoff. **The code has not been updated to the 50,003,968 decision.** `grep` shows `SHARD_TOKENS`
is a module constant with no override parameter anywhere: `corpus_pack.py:449`,
`corpus_build.py:223,255,266`, `corpus.py:89,282`.

**SEVERITY: blocks the build** — not because it fails loudly, but because it makes the natural
incremental execution ("ingest source by source, each a separately-approved platform job" —
HANDOFF-FINAL-DATASET.md:231) throw away all prior work on every added source.

**FIX (two options).**

*Option A — freeze the plan before the first job (0 code, operational discipline).* Compute the
FULL plan for all ~20 sources of both stages up front, including sources whose data is not yet
sourced, then run bundles in whatever order approval allows. This works because `_cmd_run
--shard/--of` slices bundles (`corpus_build.py:673-676`) and `bundle_is_done` skips finished ones —
partial execution of a complete plan is already supported. **Cost: the mix must be final before the
first token is written.** Effort: zero code; requires the report's §mix tables to be frozen.

*Option B — per-source ordinal namespacing (small code change).* Change the counter key from
`split` to `(source, domain, split)` in `allocate_ordinals`. Ordinals then restart at 0 per stream,
so adding a source cannot move an existing one. The docstring at `corpus.py:322-330` argues against
this — but the argument it makes is *only* about human legibility ("it just makes two distinct
shards share a name in every log"), and it explicitly verifies that **nothing in `validate.py`
rejects reuse**: no contiguity, gap, or uniqueness check exists, and `parse_shard_name` returns
`('train', 0)` for two different sources' `train-00000`. The path prefix (`tokens/<source>/`)
already disambiguates. Effort: ~10 lines + update the docstring + 2 tests. **Recommend A for this
build and B for the next**, because B changes `plan_id` for any plan and so cannot be adopted
mid-build either.

---

### F1.3 The 2-stage corpus: what it does to ordinals

**FINDING.** Three shapes are available, and they differ sharply in ordinal risk.

1. **Two datasets** (`pretrain/final-stage1/v1`, `pretrain/final-stage2/v1`) → **two plans, two
   ordinal spaces, zero interaction.** Adding a stage-2 source cannot renumber a stage-1 shard,
   because they are different `plan_id`s and different published addresses. This is the only shape
   where F1.2 is structurally contained.
2. **One dataset, stage as the `split` segment** (`stage1-00000`, `stage2-00000`) → also safe on
   ordinals, because `ordinal_by_split` is keyed by split (`corpus.py:356`), so the two stages get
   independent counters. `SHARD_RE`'s `_SPLIT_WORD` (`manifest.py:659`) accepts `stage1`? **No —
   `_SPLIT_WORD = r"[a-z0-9]*[a-z][a-z0-9]*"` requires at least one letter and allows digits, so
   `stage1` matches.** Verified against the pattern by reading it. But this overloads `split`, whose
   other consumer is the train/val carve (`corpus.py:368-415`, `is_held_out`), and `plan_document`
   builds exactly two splits per source (`corpus_build.py:238-240`). A four-way `stage1/stage2` ×
   `train/val` split space needs plumbing that does not exist.
3. **One dataset, stage as a `source`-segment prefix** (`s1-dclm`, `s2-dclm`) → ordinals are shared
   in one `train` counter, so F1.2 applies in full across both stages.

**EVIDENCE (MEASURED-IN-CODE).** `corpus.py:353-359` (counter keyed by split);
`manifest.py:659-661` (`_SPLIT_WORD` allows digits); `corpus_build.py:209-240` (only train/val are
generated per source).

**SEVERITY:** needs code before ingest (if shape 2 or 3 is chosen); operational only for shape 1.

**FIX.** Choose shape 1 (two datasets) unless Q6 finds a reason not to — see Q6 for the mixture-side
argument, which points the same way for a different reason.

---

## Q2 — Resumability

### F2.1 Progress is **per-bundle only**. Mid-bundle restart is not supported and cannot cheaply be.

**FINDING.** The unit of resumable work is the `Bundle` = one `(source, domain, split)` stream. There
is no per-shard and no per-job checkpoint. `_cmd_run`'s loop is:

```python
for bundle in mine:
    if not args.force and bundle_is_done(bundle, args.plan_id, s3, args.bucket, args.prefix):
        skipped += 1; print(f"SKIP {bundle.bundle_id} ..."); continue
    info = run_bundle(bundle, plan, specs[bundle.spec_key], ...)
```
(`corpus_build.py:694-712`)

`run_bundle` (`corpus_build.py:429-563`) is atomic-by-construction: it builds the whole generator
chain (`_selected` → `dedup_and_decontaminate` → `tokenize_documents` → `pack`), and only after
`pack` returns for the *entire* stream does it call `verify_receipt` and then `write_receipt`
(`:530-541`). **If the process dies at shard 900 of 1,200, every one of those 900 shard objects is
already in S3** (the sink uploads inside `pack`, `corpus_build.py:461-464`) **but no receipt exists,
so `bundle_is_done` returns False and the whole bundle is re-read and re-tokenized from document
zero.** The 900 uploaded objects are re-PUT with identical content (deterministic — see F2.4), so
they are not corruption, just wasted compute and egress-free re-writes.

**Why it cannot be cheaper.** `_pack_stream` fills refs from **one carry buffer that spans documents
and input files** (`corpus_pack.py:719-760`; `pack`'s docstring at `:598-605` says a per-file carry
reset is deliberately *unrepresentable*). Shard *k* is a function of every token before it, so
resuming at shard *k* requires re-reading and re-tokenizing every document before it. The module
docstring states this: *"There is no cheaper unit than 'the whole bundle', and pretending otherwise
would mean re-reading the input to skip the output"* (`corpus_build.py:24-28`).

**EVIDENCE (MEASURED-IN-CODE).** `corpus_build.py:24-28`, `:429-541`, `:694-712`;
`corpus_pack.py:598-605`, `:719-760`.

**SEVERITY:** needs code before ingest at 1.0T — see F2.3 for why bundle size, not the mechanism, is
the problem.

---

### F2.2 `bundle_is_done` is genuinely strong — it re-HEADs and compares SIZES, not just presence.

**FINDING.** Three independent conditions, all of which must hold:
1. the receipt is readable and `receipt.plan_id == plan_id` (`corpus_build.py:409-414`);
2. the receipt's declared shard-path set equals the bundle's planned set (`:415-417`);
3. **every** shard key `head`s successfully AND `int(head["size"]) == shard.bytes` (`:418-425`).

Any exception at any step → `False`, i.e. it fails closed toward re-doing the work, which is the
correct direction. The docstring names the exact failure it defends against: a worker that uploads
3 of 4 shards, writes a receipt, and dies (`:396-398`). It is explicitly **not** a re-hash, because
re-hashing on every resume is a second full pass over the corpus (`:403-404`).

**Cost of the resume check itself at 1.0T (DERIVED).** One `s3.head` per shard. At 20,000 shards and
the ~10 ms/HEAD that a single-threaded in-region boto3 client gets, `20,000 × 0.01 s = 200 s`
sequential — trivial. But note `bundle_is_done` is called **inside the per-bundle loop** and is
**not threaded**, so a job that skips 40 already-done bundles pays 40 × (its shard count) HEADs
serially before starting real work. At ~1,200 shards/bundle that is ~12 s per skipped bundle.
Acceptable.

**EVIDENCE (MEASURED-IN-CODE).** `corpus_build.py:387-426`.

**SEVERITY:** operational only.

---

### F2.3 ⚠️ **The natural unit of work is too COARSE at 1.0T. A single bundle can exceed any sane timeout.**

**FINDING.** The reservoir ran **27 bundles / 10,049 shards / 251.2B tokens**
(`artifacts/reservoir/realized-tokens.json`: `"bundles": 27, "shards": 10049, "tokens_total":
251218001920`). Largest bundle: `stackv2-edu--train` at 1,598 shards / 39.78B tokens.

At 1.0T with ~20 sources the same shape gives ~40 bundles and, if one source draws e.g. 200B (web at
77% of a 900B stage 1, split over 2-3 web sources), **a single bundle would be ~150-200B tokens.**
Because a bundle is atomic, that bundle must complete inside one Batch attempt or it restarts from
zero.

**DERIVED wall-clock for one 200B-token bundle.** From the measured `encode_batch` rate of
**10.5 M tok/s across 32 vCPU** (`corpus_pack.py:245-247`): `200e9 / 10.5e6 = 19,048 s = 5.29 h`
of pure tokenize — before the HTTP read, which Q4 shows dominates. So the largest plausible bundle
needs a job timeout in the 12-30 h range with **no checkpoint**, and a single transient failure
(HF 429, 502, a spot reclaim) costs the whole thing.

**The fix already exists in the plan-shaping layer, not the code.** `plan_document` fans a source
into one bundle per `domain` value (`corpus_build.py:229-240`, using the `domain_map` argument), and
`allocate_ordinals` gives each `(source, domain, split)` its own bundle. **So a big source can be
cut into N bundles by supplying a synthetic `domain_map`** — e.g. `{"dclm": {"p00":"p00", ...,
"p15":"p15"}}` → 16 bundles of ~12B each. Each becomes a separately-resumable unit, and the domain
segment lands in the object key and in `entry.labels` (Q7).

⚠️ **But `_reader_for` does not know how to give each domain-bundle a DIFFERENT slice of the source.**
It calls `hf_files(spec)` and reads from file 0 forward until the character budget is met
(`corpus_build.py:926-931`). **Sixteen `dclm/pNN` bundles would each read the same first files and
produce sixteen copies of the same documents.** The dedup in `corpus_filter.dedup_and_decontaminate`
is **per-bundle** (`run_bundle` constructs a fresh `FilterStats` per bundle, `corpus_build.py:481-482`),
so cross-bundle duplication is not caught at build time — and Gate A's duplicate-digest check would
only fire if two *shards* came out byte-identical, which they would not (different ordinals, and the
carve/ordering differs). **This is a real, silent, 16x-duplication hazard.**

**EVIDENCE (MEASURED-IN-CODE).** `corpus_build.py:229-240` (domain fan-out), `:884-931`
(`_reader_for` — no shard/offset argument at all), `:481-482` (per-bundle FilterStats).
**MEASURED-ELSEWHERE:** `artifacts/reservoir/realized-tokens.json` (27 bundles, 10,049 shards,
1,598-shard max).

**SEVERITY: needs code before ingest.**

**FIX.** Add file-level sharding to `_reader_for`, mirroring the striping that already exists for the
id ingest. `ingest_reservoir._shard_slice(items, shard, of)` does `items[shard::of]` and is already
imported by `corpus_build` (`corpus_build.py:87-93`) — it is *used* for slicing bundles across array
children (`:676`) but never for slicing FILES within a bundle. The change:
1. carry `file_shard`/`file_of` (or an explicit file-path list) on each plan bundle entry;
2. in `_reader_for`, `for entry in _shard_slice(hf_files(spec), bundle.file_shard, bundle.file_of)`.
Striping (not contiguous blocks) is the right choice for the same reason `_shard_slice`'s docstring
gives (`ingest_reservoir.py:765-776`): file sizes vary by an order of magnitude and cluster by name.
**Effort: ~30 lines + a plan-schema field + 3 tests.** This is the single highest-value code change
in this audit, because it simultaneously (a) makes bundles small enough to finish in one attempt and
(b) removes the duplication hazard.

---

### F2.4 The build is deterministic, so a re-run of a lost bundle is byte-identical — resume is safe.

**FINDING.** `carve`/`is_held_out` is a pure hash of `(source, doc_id)` with no PRNG
(`corpus.py:368-402`, docstring at `:376-382`). `allocate_ordinals` sorts rather than using input
order (`corpus.py:353`). `_reader_for` breaks **between files**, never mid-file, explicitly so the
document set does not depend on where the budget ran out (`corpus_build.py:896-899`). And it was
verified live: nine bundles re-run under `--force` reproduced **byte-identical digests**
(`artifacts/reservoir/realized-tokens.json` `_comment`: *"the nine re-run ones reproduced
byte-identical digests"*; the 9 jobs are listed in `artifacts/reservoir/rerun-jobs.json`).

**SEVERITY:** operational only — this is a strength, and it is what makes a coarse resume unit
survivable rather than fatal.

---

### F2.5 There is no per-JOB progress artifact; a killed array child leaves no trace of partial bundles.

**FINDING.** `_cmd_run` prints `RUN_START` / `DONE <bundle>` / `RUN_END` to stdout
(`corpus_build.py:677`, `:708-712`) and writes nothing else. Progress state lives entirely in
S3 receipts. A child killed mid-bundle leaves: shard objects with no receipt (invisible to
`bundle_is_done`) and a CloudWatch log. There is no "I got to document N" marker, which is
consistent with F2.1 (it would be useless anyway).

**Operational consequence:** orphaned shard objects accumulate in `s3://edullm-landing/_ingest/.../data/`
under keys that a *later* successful run of the same bundle will overwrite with identical bytes — so
they are harmless, **unless the plan_id changes** (F1.2), in which case they are dead weight against
the `expire-ingest-30d` lifecycle rule (`artifacts/reservoir/RUN-THE-INGEST.md:26`) rather than a
correctness problem.

**SEVERITY:** operational only.

---

## Q3 — Wall-clock and cost for the tokenize

### F3.1 The tokenize itself is CHEAP: ~26 CPU-hours and ~$38 for 1.0T tokens.

**The measured rate (MEASURED-IN-CODE).** `corpus_pack.py:245-247`, verbatim: *"Measured on the
pinned dolma2 tokenizer over real prose: 1.10 M tok/s single-document versus **10.5 M tok/s for
`encode_batch` across 32 vCPU**"*. Batch size is 1,000 documents (`corpus_pack.py:161`,
`_ENCODE_BATCH`). The parallelism comes from `tokenizers`' rayon pool, which is why
`TOKENIZERS_PARALLELISM` must be `"true"` and why `_assert_tokenizers_parallelism`
(`corpus_build.py:763-778`) refuses to run until the operator sets it.

**DERIVED wall-clock.** Assume the rate scales linearly in vCPU (`10.5 M / 32 = 328,125 tok/s per
vCPU`). This is optimistic — rayon scaling is sublinear past ~16 threads and the GIL-bound Python
loop around it (`_batched`, the per-document `np.fromiter` + range assertion at
`corpus_pack.py:358-374`) is single-threaded — so treat these as **lower bounds on time**:

| vCPU | derived rate | 1.0T tokens |
|---|---|---|
| 32 | 10.5 M tok/s | `1.0e12 / 10.5e6 = 95,238 s = **26.46 h**` |
| 64 | 21.0 M tok/s | `1.0e12 / 21.0e6 = 47,619 s = **13.23 h**` |
| 96 | 31.5 M tok/s | `1.0e12 / 31.5e6 = 31,746 s = **8.82 h**` |

**DERIVED cost.** Rate used: **`c7i` on-demand, us-east-1, `$0.04462` per vCPU-hour** — derived from
the published `c7i.8xlarge` on-demand price of `$1.428/h` for 32 vCPU (`1.428 / 32 = 0.044625`).
⚠️ **UNVERIFIED against a live price API** — I did not call AWS (instructed not to), and this repo
records no `c7i` price anywhere; the figure is from my training data for us-east-1 Linux on-demand
and should be re-checked with `aws pricing` before it goes in a budget. `m7i` is ~13% higher
(`$1.6128/h` for `m7i.8xlarge` = `$0.0504`/vCPU-h).

Because the price is per vCPU-hour and the work is fixed, **the cost is the same at every size** —
only wall-clock moves:

`26.46 h × 32 vCPU × $0.04462 = 1,000,000 s / 3600 × ... ` → concretely:
- 32 vCPU: `26.46 h × $1.428/h = **$37.8**`
- 64 vCPU: `13.23 h × $2.856/h = **$37.8**`
- 96 vCPU: `8.82 h × $4.284/h = **$37.8**`

**Tokenization is a rounding error in this build's cost.** ~$38 on-demand, ~$12 on spot. **SEVERITY:
operational only.** The compute cost of the ingest is NOT the tokenize — see Q4.

⚠️ One caveat that could multiply this: the pipeline tokenizes **everything it reads**, and
`_reader_for` over-reads by design (`_CHARS_PER_TOKEN 6.0 × _FILTER_HEADROOM 1.5 = 9 chars per
planned token`, `corpus_build.py:865`, `:881`, `:924`) against a **measured** mean of 4.31
chars/token (`artifacts/reservoir/chars-per-token.json`: `"_mean": 4.31, "_worst_observed": 5.58`).
So the encode sees `9 / 4.31 = 2.09x` the tokens it keeps. **Corrected tokenize: ~2.09 × $38 = ~$79
and 55 h at 32 vCPU / 18 h at 96.** Still negligible.

---

### F3.2 There is NO Batch maximum timeout. Every timeout in this repo is self-imposed and changeable.

**FINDING.** The 7200 s figure that CLAUDE.md and several artifacts treat as a wall is **our own job
definition setting**, and this was explicitly corrected in-repo:

> *"There is **no maximum Batch timeout** — AWS: 'There's no maximum timeout value for an AWS Batch
> job.' 7200 s was a value *we* set. 'A single job cannot finish the pass' was therefore never a
> platform fact; it was a consequence of our own job def, changeable with one
> `register-job-definition`."*
> — `artifacts/reservoir/INGEST-CALIBRATION.md:19-22` (MEASURED-ELSEWHERE)

**Every timeout actually observed in this repo (MEASURED-ELSEWHERE, each cited):**

| job def | timeout | source |
|---|---|---|
| `edullm-validator:7` | 7200 s (2 h) | `artifacts/PHASE0-REPORT.md:23-24` — revisions 1-6 had `timeout: null` |
| `edullm-reservoir-ingest:7` | 7200 s, 2 vCPU / 8 GB | `artifacts/reservoir/RUN-THE-INGEST.md:24` |
| `edullm-reservoir-verify:1` | **14400 s (4 h)** | `artifacts/reservoir/verify-job.json:33` `"timeout_hours": 4.0` |
| `edullm-prm800k-publish:2` | 7200 s, 2 vCPU / 4 GiB | `artifacts/reservoir/PUBLISH-SPEC.md:223` |
| the GPU classify job def | 3600 s | `artifacts/smoke/submit_classify_d.py:31` |
| **recommended** dataset-publish job def | **≥ 21600 s (6 h)** | `infra/10-dataset-publish-jobdef.md:70` |

The 60-min figure in `CLAUDE.md` gotcha 4 traces to the olmo publish that "timed out at 3600 s
single-threaded" (`infra/10-dataset-publish-jobdef.md:70`) — a job def at 3600 s, not a platform cap.

**The REAL hard constraint is the compute environment's vCPU ceiling, not time.**
`artifacts/reservoir/INGEST-CALIBRATION.md:52,60`: *"past the queue's 128 vCPU cap"* and *"The 128
vCPU compute-environment cap **is** real."* (MEASURED-ELSEWHERE.) So one job can have at most ~128
vCPU (one `c7i.8xlarge`-family instance shape per the note), and total concurrent fleet capacity is
the thing to negotiate.

**SEVERITY: operational only** — but it inverts the framing of the question. The decomposition is
driven by **failure blast radius**, not by a timeout wall.

---

### F3.3 How many parallel jobs a 1.0T tokenize decomposes into

**DERIVED, given F3.1, F3.2 and F2.3.** Three independent constraints:

1. **One bundle must finish in one attempt** (F2.1 — no mid-bundle resume). This is the binding
   constraint, and it argues for MANY SMALL bundles regardless of timeout.
2. **128 vCPU per compute environment** (MEASURED-ELSEWHERE, INGEST-CALIBRATION.md:60). At 32 vCPU
   per child that is **4 concurrent children**; at 8 vCPU per child, 16.
3. **`_reader_for` is single-threaded HTTP** (F4.3) — so per-child throughput is set by network, and
   giving one child 32 vCPU to feed a single-threaded reader wastes 31 of them.

**Recommended shape:** target ~**8-12B tokens per bundle** (≈160-240 shards at the current
25,001,984-token constant, or ≈80-120 at the decided 50,003,968). That gives:

`1.0e12 / 1.0e10 = **~100 bundles**`

Each bundle at 10B tokens: tokenize `10e9 × 2.09 / 10.5e6 = 1,990 s = 0.55 h` at 32 vCPU. Add the
HTTP read (Q4, the dominant term) and each child lands in the **2-6 h** range. **Run them as Batch
array jobs of ~16-25 children in waves**, per the shape that already worked
(`RUN-THE-INGEST.md:38-48`: `--array-properties size=10`, `AWS_BATCH_JOB_ARRAY_INDEX` → `--shard`).
`_cmd_run --of N` already does exactly this slicing (`corpus_build.py:673-676`).

Set the job-def timeout to **28800 s (8 h)** with `attempts: 1` (per `infra/10-...:79` — retry is
unsafe for publish; for *build* it is safe because `bundle_is_done` skips finished bundles, so
`attempts: 2` is actually fine here and is worth setting).

**FIX/effort:** requires F2.3's file-sharding change (~30 lines) to be able to cut a big source into
many bundles at all. Otherwise ~40 bundles with one of them at 150-200B tokens.

---

## Q4 — Download bandwidth: the real bottleneck

### F4.1 The tokens-per-byte ratio, from MEASURED values

**MEASURED-ELSEWHERE — `artifacts/recount/*.json`, dolma2-tokenizer, per source:**

| source | tokens/byte | file |
|---|---|---|
| fineweb-edu | 0.21029 | `edu-web-fineweb-edu.json` `/configs[0]/tokens_per_byte` |
| finepdfs-edu | 0.27873 | `edu-web-finepdfs-edu.json` |
| dclm (100BT) | 0.22755 | `web-dclm-100bt.json` |
| dclm-baseline parquet | 0.23230 | `web-dclm-baseline-parquet.json` |
| finemath-4plus | 0.29615 | `math-finemath-4plus.json` |
| swallow-math-v2 | 0.28739 | `math-swallow-math-v2.json` |
| finewiki-en | 0.21819 | `reference-finewiki-en.json` |
| FinePhrase faq / math / table / tutorial | 0.215625 / 0.220412 / 0.233647 / 0.201033 | `synthetic.json` `/sources[0..3]` |
| code (3 sources) | 0.2938 / 0.2729 / 0.2786 | `code.json` `/sources[0..2]` |
| peS2o / pubmed / arxiv (RAW) | 0.219116 / 0.251969 / 0.347222 | `academic.json` `/sources[*]/RAW` |
| stackexchange / ubuntu-irc / gharchive | 0.264852 / 0.348641 / 0.283121 | `qa-forum.json` |

`n = 19`; **min 0.201, max 0.349, mean 0.2605, median 0.2649.**

**DERIVED, weighted by the 2-stage mix** (HANDOFF-FINAL-DATASET.md:60-63: stage 1 = 900B at 77% web,
stage 2 = 100B at 32% web; web sources blend to ~0.225 tok/B, non-web to ~0.27):
```
stage 1 tpb = 0.77×0.225 + 0.23×0.27 = 0.2354
stage 2 tpb = 0.32×0.225 + 0.68×0.27 = 0.2556
overall     = (900×0.2354 + 100×0.2556)/1000 = 0.2374
text bytes  = 1.0e12 / 0.2374 = 4.213e12 = 4.21 TB
```
**So the question's "roughly 4-5 TB" is confirmed at ~4.2 TB of UTF-8 text.** (The prompt's range is
right; the mean-tpb figure alone would give 3.84 TB, and the fineweb-edu-heavy extreme 4.76 TB.)

### F4.2 ⚠️ **But the pipeline pulls ~9 TB, not 4.2 TB — a 2.1x over-read baked into two constants.**

`_reader_for`'s budget is `bundle.tokens × _CHARS_PER_TOKEN × _FILTER_HEADROOM / keep_rate` with
`_CHARS_PER_TOKEN = 6.0` and `_FILTER_HEADROOM = 1.5` (`corpus_build.py:865`, `:881`, `:924`), i.e.
**9 characters of source text read per planned token.** But the MEASURED mean is 4.31 chars/token and
the worst observed is 5.58 (`artifacts/reservoir/chars-per-token.json`: `"_mean": 4.31,
"_worst_observed": 5.58, "_constant_chosen": 6.0`).

```
reader budget = 1.0e12 tokens × 9 chars/token = 9.0e12 chars ≈ 9.0 TB
true text     = 4.21 TB
over-read     = 9.0 / 4.21 = 2.14x
```
Also note the reader breaks **between files**, so the actual read overshoots the budget by up to one
whole file (`corpus_build.py:930-931`, `:896-899`). And **a val bundle divides by `keep_rate = 0.005`**
(`Bundle.keep_rate`, `corpus_build.py:331-340`; used at `:923-924`), so **a val bundle reads 200x its
own token count.** At `VAL_FRACTION 0.005` × 1.0T = 5B val tokens, the val bundles alone read
`5e9 × 9 / 0.005 = 9.0e12 chars = another 9.0 TB`. **The val split doubles the download.** The
reservoir hid this because val was 0.975B tokens; at 1.0T it is 5B and the read is 9 TB.

**SEVERITY: needs code before ingest.** (Or an operational decision: drop `val_fraction` to ~0.001,
which still yields 20 val shards at 50M tokens each, and cuts the val read from 9 TB to 1.8 TB.)

**FIX.** Two cheap wins, both single-constant:
1. Lower `_CHARS_PER_TOKEN` from 6.0 to ~5.8 (still above the worst observed 5.58) — marginal; the
   constant is deliberately conservative and the docstring explains why (`corpus_build.py:858-864`).
   **Better: make it PER-SOURCE from `chars-per-token.json`, which already holds a measured value for
   every source.** That takes 9.0 TB → `1.0e12 × 4.31 × 1.5 = 6.5 TB` (a 28% saving) with no loss of
   safety, because each source uses its own measured ratio + the same 1.5x headroom.
2. Lower `val_fraction` for the 1.0T build. `plan_document(val_fraction=...)` already takes it
   (`corpus_build.py:186`) and `_cmd_plan` does not expose it — one CLI flag. Effort: ~15 lines total.

### F4.3 `corpus_read` DOES stream with ranged GETs and DOES retry — but it is entirely SINGLE-THREADED.

**FINDING — streaming: yes.** Neither reader downloads a whole file to disk.
- **parquet:** `_open_parquet` wraps the pinned URL in `_RangeFile` and opens
  `pq.ParquetFile(rf, pre_buffer=False)` (`corpus_read.py:415-421`). Reads are per row group with an
  explicit **column projection** — only the text/id/domain leaves (`read_parquet_documents`,
  `corpus_read.py:487-503`), so the footer plus those column chunks and nothing else. `del table`
  after each row group (`:521`).
- **json.gz:** `_range_chunks(rf, chunk_bytes=JSONL_CHUNK_BYTES)` where `JSONL_CHUNK_BYTES = 8 MiB`
  (`corpus_read.py:540`, `:627-640`), fed to a streaming `zlib.decompressobj` (`_gunzip_lines`,
  `:543`) that checks `.eof` to distinguish a complete stream from a truncated one.

**FINDING — retries: yes, and they are well-built.** `_RangeFile.read` (`ingest_reservoir.py:388+`)
loops until exactly `n` bytes are satisfied (the segfault fix). Around it: `_MAX_ATTEMPTS = 8`,
exponential `4.0 × 2**attempt` capped at `_BACKOFF_CAP_S = 120.0`, honouring numeric `Retry-After`
(`ingest_reservoir.py:187-247`). `_TRANSIENT_STATUSES` = {408, 425, 429, 500, 502, 503, 504, 509} —
503 is in the set because *"it used to be just 429, and that cost five wave-1 bundles overnight"*
(`ingest_reservoir.py:193-213`). 403 is deliberately absent (CDN signature expiry needs a fresh
resolve, not a retry). A process-wide `_RateGate` pauses every worker on any 429
(`ingest_reservoir.py:250-297`). Signed CDN URLs are resolved **once per file** and cached with a TTL
(`_cdn_url`, `ingest_reservoir.py:313-373`) — this was the 70x metered-request amplification fix, and
the CDN data plane is **unmetered** and served from `x-hf-cdn-pop: aws-us-east-1`, the same region as
our buckets (`ingest_reservoir.py:318-332`, MEASURED-ELSEWHERE).

**⚠️ FINDING — the read is SINGLE-THREADED. This is the blocker.** `grep` for
`ThreadPool|concurrent.futures|max_workers` across `src/edullm_data/` returns hits **only** in
`corpus_receipt.py:866`, `ingest_reservoir.py:796,890`, `publish.py:470,734,1023`, and
`validate.py:567,2070,2099`. **`corpus_read.py`, `corpus_build.py`, `corpus_pack.py`, and
`corpus_filter.py` contain ZERO threading.** `_reader_for` is a plain nested `for` loop over
`hf_files(spec)` → `reader(...)` (`corpus_build.py:926-931`), and the generator chain is fully
sequential: read one row group → tokenize it → pack it → next.

Consequence: **one child gets one HTTP stream.** In-region single-stream from the HF CDN is the
throughput to beat, and this repo's only in-region single-stream measurement is against **S3**:
**87.8 MB/s** (`artifacts/reservoir/verify-job.json`: `"sustained_mb_s": 87.8, "single_threaded":
true`). HF CDN single-stream is UNVERIFIED but the same order.

### F4.4 The comparison: download DOMINATES tokenize by ~4-10x. The read is the whole build.

**DERIVED.** Aggregate bytes to move (F4.2, using the current constants, train+val): **~18 TB**
(9 TB train + 9 TB val). With the two F4.2 fixes: **~7.8 TB**.

| sustained aggregate rate | 9 TB (train only, current) | 18 TB (train+val, current) |
|---|---|---|
| 100 MB/s (1 single-threaded child) | 25.0 h | 50.0 h |
| 250 MB/s | 10.0 h | 20.0 h |
| 500 MB/s | 5.0 h | 10.0 h |
| 1.0 GB/s | 2.5 h | 5.0 h |

Against F3.1's tokenize of **8.8-26.5 h** (or 18-55 h with the 2.09x over-read).

**The verdict depends entirely on aggregate concurrency, and here is the honest reading:**
- **Per child, the read and the tokenize are SERIALIZED in one Python generator chain** — they do not
  overlap. So a child's wall clock is `read_time + tokenize_time`, and at 100 MB/s per child the read
  is the larger term by ~2-4x.
- **Across ~100 children in waves of 16-25**, aggregate download is 1.6-2.5 GB/s and total download
  wall-clock falls to ~2-3 h, while total tokenize CPU is bounded by the **128 vCPU cap** — at 128
  vCPU the whole 2.09T-token encode takes `2.09e12 / (10.5e6 × 128/32) = 49,762 s = 13.8 h`.
- **So at realistic fleet size, TOKENIZE dominates** — because the vCPU cap is a real ceiling while
  the download simply fans out. **UNVERIFIED at scale**: the single-stream HF CDN rate is not
  measured anywhere in this repo, and if it is well under 100 MB/s per child the ordering flips.

**Data-transfer cost: $0.** HF → EC2 is **inbound** to AWS, and AWS charges nothing for data transfer
IN. The only egress in this pipeline is S3 PUT to `edullm-landing` (same region, free) and later
publish/validate reads (same region, free). ⚠️ **Confirm the compute environment is in `us-east-1`**,
because the CDN POP is `aws-us-east-1` (`ingest_reservoir.py:322`) and a cross-region compute
environment would add both latency and NAT-gateway processing charges (~$0.045/GB × 18 TB = **$810**
if traffic goes through a NAT gateway rather than an internet gateway on a public subnet — **this is
the one non-trivial transfer cost and it is an architecture question, not a data question**).

**SEVERITY: needs code before ingest** (the threading; see FIX).

**FIX.** Add a thread pool over FILES inside `_reader_for`, feeding a bounded queue that the
tokenize/pack chain drains. Documents must reach `pack` in a deterministic order or the shard bytes
change — so the pool should prefetch **file bytes** (or whole-file document lists) while preserving
file order, not interleave documents. The pattern already exists twice in this repo
(`ingest_reservoir.py:796,890` uses `pool.map` over a file list, which preserves order). Effort:
~60-80 lines + a determinism test asserting identical digests at 1 and 8 workers, mirroring the one
already written for `--hash-workers` (`PUBLISH-SPEC.md:172-177`). **Alternative with zero code: rely
on child-level fan-out only** — 100 children each single-threaded. That works and is what I recommend
for the first wave, because it needs no new code and the determinism risk is zero.

---

## Q5 — Gate A / validation at ~20,000 objects

### F5.1 What Gate A does per object: **1 HEAD + 5 ranged GETs = 6 network round trips.** All threading is opt-in and only ONE of the six is threaded.

**FINDING — the exact per-object network cost (MEASURED-IN-CODE).** Per manifest entry, in one
validation run:

| where | call | file:line | threaded? |
|---|---|---|---|
| Gate A decision loop | `s3.head` for the real SIZE vs declared `bytes` | `validate.py:703` via `_prefetch_heads` | **YES** — `--head-workers` |
| `check_decode_smoke` → `_sampled_ids` → `_observed_size` | `s3.head` (cached in `ctx.observations["object_sizes"]`) | `pretrain_tokens_v1.py:220-222` | **NO** |
| `check_decode_smoke` → `_sampled_ids` | **4 × `s3.get_range(..., off, window)`** at seeded offsets | `pretrain_tokens_v1.py:240` | **NO** |
| `check_first_bytes_not_npy` | `s3.get_range(key, 0, 8)` — the `\x93NUMPY` sniff | `pretrain_tokens_v1.py:438` | **NO** |
| `check_seq_len_alignment` | `_observed_size` — **cache hit, free** | `pretrain_tokens_v1.py:472` | n/a |
| `check_entries_declare_token_counts` | none (pure manifest) | `pretrain_tokens_v1.py:253-277` | n/a |

Window arithmetic: `DECODE_SAMPLE_BYTES` split into `_N_WINDOWS = 4` (`pretrain_tokens_v1.py:55`),
i.e. `65536 / 4 = 16384 B` per range read = 4,096 uint32 tokens each, 16,384 tokens pooled
(`corpus_pack.py:151` derives `DECODE_WINDOW_TOKENS` the same way so build and Gate A agree).

**So: 6 network calls per object, of which exactly 1 is threadable today.** The `_observed_size`
cache (`pretrain_tokens_v1.py:201-224`) already removed a third redundant HEAD — its docstring
records the measurement: *"a 10,049-object corpus spent ~30,000 round trips discovering 10,049 sizes.
Measured live on `pretrain/reservoir-dolma2`: **Gate A ran ~85 min at 0.3% CPU and ~15.8 round
trips/s** -- purely latency-bound, which is what pushed the first promotion attempt past its 2 h
timeout"* (`pretrain_tokens_v1.py:205-210`). **This is the 85-minute measurement the plan cites, and
it is MEASURED-IN-CODE at that line.**

**Reconciling 85 min with 6 calls/object (DERIVED).** `10,049 × 6 = 60,294` round trips.
`60,294 / (85 × 60 s) = 11.8 rt/s` — close to the recorded 15.8 rt/s (the difference is the
one-per-group LIST for the exhaustiveness diff plus manifest GETs). **The model holds: Gate A is
`objects × 6 × latency`, purely serial, ~85 µs of CPU per call and ~85 ms of waiting.**

### F5.2 ⚠️ **A 20,000-object validate does NOT fit a 1-hour cap, and does not fit 2 hours either at default settings.**

**DERIVED, by direct scaling of the measured 85 min:**

| objects | round trips | at 15.8 rt/s (measured, `head_workers=1`) | at `head_workers=16` |
|---|---|---|---|
| 10,049 (measured) | 60,294 | **85 min** ✅ under 2 h, ❌ over 1 h | ~71 min (only 1 of 6 calls threaded) |
| **20,000** | 120,000 | **169 min = 2.82 h** ❌ over both | ~141 min = 2.35 h ❌ |
| 40,000 (at today's 25,001,984-token shard) | 240,000 | **338 min = 5.63 h** ❌ | ~282 min ❌ |

**Why `--head-workers` alone barely helps.** It threads exactly one of six calls
(`_prefetch_heads`, `validate.py:531-579`). Amdahl: even at infinite head workers the remaining 5
serial calls per object cost `5/6 = 83%` of the original time. **`head_workers=16` on a
20,000-object corpus takes ~2.35 h, not 10 min.** The CLI help text is accurate about *what* it does
(*"Gate A issues one HEAD per manifest entry and nothing else in that loop touches the network"* —
`validate.py:2451-2458`) but that statement is scoped to **the decision loop**; the profile checks
run afterwards, in a different function, and they are where 5 of the 6 calls live.

**The validate cap.** ⚠️ **I could not confirm a "1-hour validate cap" in this worktree.** What I
found: `edullm-validator:7` was registered with `timeout.attemptDurationSeconds = 7200` (2 h) and
revisions 1-6 had `timeout: null` (`artifacts/PHASE0-REPORT.md:23-24`, MEASURED-ELSEWHERE); the
current live revision is **12** (`artifacts/reservoir/PUBLISH-SPEC.md:184`) and **its timeout is not
recorded anywhere in this repo** — UNVERIFIED. The 1-hour figures in `docs/PLATFORM-INTEGRATION.md`
(`:37`, `:132`, `:439-442`, `:628`) are all `attemptDurationSeconds: 3600` on **platform** workload
job defs (`infra/batch-compute-gpu.yaml:170`, the GPU/smoke path), not the dataset validator. **Get
the live number before submitting**: `aws batch describe-job-definitions --job-definition-name
edullm-validator`. Either way — **1 h fails, 2 h fails, and the honest requirement is ≥ 4 h at
default settings.**

**AND promotion is a separate, comparable cost.** `promote()` is *"~2 S3 round-trips per object"*
and *"a 6,913-object corpus is ~13,800 serial calls, which overruns the 60-minute Batch job-def
limit"* (`validate.py:1943-1948`). At 20,000 objects that is 40,000 calls — but `promote()` **is**
threaded on both phases (`validate.py:2069-2076`, `:2098-2102`) via `--promote-workers`, so at 16
workers it is ~10-15 min. **Promotion is solved; validation is not.**

**SEVERITY: blocks the build** (a validate that cannot finish means the corpus cannot be promoted).

### F5.3 Task #10 ("thread the profile checks' ranged GETs before 40k objects") is the right fix and IS sufficient — with one correction.

**Assessment.** Threading the 5 remaining calls is correct and safe, and here is why, from the code:

- **The checks are independent per entry and append to their own local `out` list**
  (`pretrain_tokens_v1.py:280`, `:432`, `:456` all build a fresh `out: list[Violation]` and iterate
  `_entries(ctx)`). Unlike Gate A's decision loop, **there is no order-dependent shared state**: the
  order-sensitivity that `_prefetch_heads` carefully preserves (`duplicate-shard-digest` /
  `shared-sha-with-parent` fire on the *second* occurrence of a digest and so depend on iteration
  order — `validate.py:690-695`) lives in `_validate_group`'s loop, **not** in the profile checks.
- The one shared mutable is `ctx.observations["object_sizes"]`
  (`pretrain_tokens_v1.py:220`), a plain dict — needs a lock or a pre-populated pass.
- **The cheapest correct shape:** follow `_prefetch_heads`' own pattern — a **pure prefetch function**
  that threads the I/O and returns `{path: (size, head8, sampled_bytes)}`, then let the existing
  check loops stay byte-for-byte serial and read from the dict. That keeps every violation's order
  and content identical at any worker count, which is the property the repo already treats as
  non-negotiable (`validate.py:539-546`, `PUBLISH-SPEC.md:172-177`).

**DERIVED payoff.** All 6 calls threaded at 16 workers: `120,000 / (15.8 × 16) = 475 s = **7.9 min**`
for 20,000 objects. Comfortably inside 1 h. At 40,000 objects, ~16 min.

**⚠️ Two corrections to task #10 as stated:**
1. **`--head-workers` must also seed `object_sizes`.** Today `_prefetch_heads` HEADs every key
   (`validate.py:531`) and then `_observed_size` HEADs **the same keys again**
   (`pretrain_tokens_v1.py:220-222`) because the two caches are separate — `_prefetch_heads` returns
   a local dict that is never written into `ctx.observations`. **That is a free 20,000-call saving
   available today, in ~3 lines**, and it is a strictly larger win than it looks: it turns 6 calls
   per object into 5 and makes the second one threaded.
2. **The connection-pool trap applies.** `validate.main` already sizes
   `max_pool_connections` from `max(head_workers, promote_workers)` but only when `> 8`
   (`validate.py:2483-2492`). A new profile-check worker count must be folded into that `max()`, or
   workers 11..N silently pay a fresh TLS handshake per object with no error
   (`validate.py:2478-2487` documents exactly this).

**FIX / effort:** the prefetch-and-thread change is **~80-120 lines** in `pretrain_tokens_v1.py` +
`validate.py` plus a determinism test (identical `Violation` lists at 1 / 4 / 16 workers). The
3-line `object_sizes` seeding is worth doing immediately regardless.

### F5.4 The alternative fixes, ranked

1. ✅ **Thread the profile checks** (task #10). ~100 lines. Fixes it for good, up to ~100k objects.
2. ✅ **Raise the validator timeout to 21600 s (6 h).** One `register-job-definition`, zero code, and
   it is what `infra/10-dataset-publish-jobdef.md:70` already recommends for the sibling publish job.
   **Do this regardless of #1** — it costs nothing and removes the cliff.
3. ⚠️ **Bigger shards.** At the decided 50,003,968 tokens (HANDOFF-FINAL-DATASET.md:181) 1.0T is
   20,000 objects instead of 40,000 — a 2x saving, and the shard-size decision is free because
   `check_seq_len_alignment` is skipped unless the group declares `seq_len`
   (`pretrain_tokens_v1.py:456-458`) and the published corpus does not
   (HANDOFF-FINAL-DATASET.md:155). **But the code still has `SHARD_TOKENS = 3052 × 8192`
   (`corpus.py:89`) — the decision is not implemented.** Effort: one constant + the `MIN_MEAN_DOC` /
   ordinal-headroom re-checks; but note `SHARD_TOKENS` must remain a multiple of `SEQ_LEN`
   (`_assert_ref_alignable`, `corpus_pack.py:687-701`) — `50,003,968 / 8192 = 6104.0` exactly ✅.
4. ✅ **Split into TWO datasets (stage 1 / stage 2).** 18,000 + 2,000 objects at the 50M shard, each
   validated independently. **This alone brings stage 2 under any cap and stage 1 to ~2.5 h** — and
   it is the shape F1.3 already recommends for the ordinal reason. Zero code.
5. ❌ **Do not split stage 1 into arbitrary sub-datasets to fit the cap.** `build_mixture` cannot span
   groups or datasets (Q6), so slicing the corpus for validator convenience makes it unmixable.

---

## Q6 — `build_mixture`, and whether the 2-stage plan is expressible

### F6.1 CONFIRMED: `build_mixture` is scoped to exactly ONE group of ONE dataset, and it is enforced in three places.

**FINDING.** Both prior claims verified, plus a third constraint neither mentioned.

**(a) One dataset.** `build_mixture(dataset_id, version, ...)` — singular, positional
(`read.py:982-983`). The docstring states the reason: *"Single-dataset by construction. Mixing two
datasets would risk combining corpora tokenized with different tokenizers whose vocab sizes are
similar enough that every id still looks valid — semantically wrong and silent. Doing that safely
needs a tokenizer-identity check across the datasets' `depends_on` pins, which is deliberately not
built here."* (`read.py:1013-1018`, MEASURED-IN-CODE.)

**(b) One group.** `_mixture_entries` calls `_choose_group(groups, group, ...)`
(`read.py:1183`), which **raises** on a multi-group dataset when `group=` is not given
(`read.py:352-355`) and returns exactly ONE group's manifest either way (`read.py:1185-1188`). Every
component then filters that one entry list (`read.py:1070`). **There is no code path that unions two
groups' entries.** ✅ Prior finding confirmed. This is exactly why `corpus.GROUP = "tokens"` is a
single group and its docstring says so: *"NOT a stylistic choice: `read.build_mixture` is scoped to a
single group... Realness is fused into the `source` segment instead"* (`corpus.py:99-103`).

**(c) ⚠️ A THIRD constraint, not in the prior findings: one SPLIT, from a CLOSED vocabulary.**
`build_mixture(split="train")` defaults to train (`read.py:993`) and `_mixture_entries` discards any
shard whose parsed split is not in `SPLITS` (`read.py:1196-1199`). `SPLITS = frozenset({"train",
"val", "test"})` (`contracts.py:142`) is a **closed enum**, and `ManifestEntry.__post_init__` raises
on anything else (`manifest.py:284-292`). **So `stage1` / `stage2` cannot be split names.** This
kills option 2 from F1.3.

### F6.2 CONFIRMED: whole-shard selection bounds mixture error at ~1/shards_per_component.

**FINDING.** The fill loop takes WHOLE entries:
```python
ordered = sorted(matching, key=lambda e: _shuffle_key(seed, dataset_id, version, e.path))
while got < target:
    for e in ordered:
        if got >= target: break
        n = int((getattr(e, "count", None) or {}).get("value", 0))
        if capped and got + n > hard_cap: continue
        picked.append(e); got += n
```
(`read.py:1115-1132`.) It stops on the first shard that crosses `target`, so the overshoot is at
most one shard: **error ≤ shard_tokens / component_tokens = 1 / shards_per_component.** ✅ Confirmed.
The docstring names the cost explicitly: *"The cost is that a budget lands within one shard of target
rather than exactly on it"* (`read.py:1010-1011`). And `max_source_fraction` flips overshoot to
undershoot rather than removing it — the `capped` branch `continue`s past a shard that would breach
`hard_cap`, so the component lands *under* target (`read.py:1105-1112`, `:1125-1126`).

**DERIVED at 1.0T with the decided 50,003,968-token shard:** the smallest components in the report's
stage-1 table are 1% (`StackExchange`, 9.0B) → `9.0e9 / 50.0e6 = 180 shards` → **0.56% max error**.
Stage 2 at 100B: the 2% `reference` component is 2.0B → `40 shards` → **2.5% max error**. Acceptable
for both. ⚠️ At today's un-updated `SHARD_TOKENS = 25,001,984` the errors halve again, which is the
argument *against* the bigger shard — but 0.56% is not a decision-relevant error.

### F6.3 RECOMMENDATION: **publish TWO datasets. Do not use one dataset with a stage label, and do not delegate to the trainer's mixing config.**

Four options, evaluated against the code:

| option | works? | why |
|---|---|---|
| **A. Two datasets** (`pretrain/final-stage1/v1`, `pretrain/final-stage2/v1`) | ✅ **RECOMMENDED** | Each is one group / one dataset / one train split — exactly what `build_mixture` supports. Two `build_mixture` calls, one per stage, is the natural expression of "stage 1 = these shares, stage 2 = those shares". Also solves F1.2 (ordinal isolation) and F5.2 (validate fits the cap). |
| B. One dataset, `stage` as a THIRD label key | ❌ **IMPOSSIBLE** | `labels_from_path` names at most 2 levels (`PATH_LABEL_KEYS`), and Gate A recomputes with the default and compares by **full dict equality**, rejecting a third level as `labels-contradict-path` (`manifest.py:715-742`). The docstring is unusually blunt: *"Two levels is the whole budget. A third dimension must be FLATTENED into the `source` segment."* |
| B′. One dataset, stage FLATTENED into `source` (`s1-dclm`, `s2-dclm`) | ⚠️ **works but bad** | Legal, and `build_mixture` can express it (one call per stage, listing only that stage's `source` values). But: it doubles the source cardinality; every ordinal shares one `train` counter so F1.2 bites across stages; and it validates as one ~20,000-object dataset (F5.2). |
| C. One dataset, stage as `split` | ❌ **IMPOSSIBLE** | `SPLITS` is closed at `{train, val, test}` (`contracts.py:142`, enforced `manifest.py:284-292`). |
| D. One dataset, let OLMo-core's mixing config do it | ❌ **actively harmful** | The consumer's `SourceMixtureDatasetConfig` takes `ceil(available * ratio)` **from the head of every path** — *"a 10% mixture there reads the first 10% of every shard and never touches a tail, so any ordering inside a shard becomes a systematic skew"* — and *"Its own `seed` field is declared, documented as controlling sampling, and **never read**"* (`docs/CONSUMER-CONTRACT.md:193-199`, MEASURED-ELSEWHERE, and `read.py:1004-1009` says the same). Our own memory records the same: OLMo-core takes a per-path TOKEN BUDGET and reads a file *prefix*. Delegating stage mixing to the trainer means (a) positional skew and (b) an unreproducible sample. |

**The tradeoff of A, stated honestly.**
- **Cost 1 — a source that appears in both stages is tokenized twice** (DCLM is 42% of stage 1 and
  32% of stage 2; code is 10% then 18%). At 1.0T the duplicated work is stage 2's 100B → ~10% more
  tokenize, ~$8. Negligible. **But the DOCUMENTS may overlap**, and nothing dedups across datasets:
  `corpus_filter`'s dedup is per-bundle (F2.3). Fix operationally: give stage 2 a disjoint file
  slice of each shared source via F2.3's `file_shard` mechanism, which makes the two stages read
  provably different files.
- **Cost 2 — two publishes, two validates, two promotes.** More approval round trips, which is
  actually a *feature* under "never auto-publish" (HANDOFF-FINAL-DATASET.md:19).
- **Cost 3 — a run that wants both stages needs two `build_mixture` calls and a concatenation the
  trainer performs.** But that is what a 2-stage run *is*: the trainer switches data at the stage
  boundary and re-anneals the LR. It is not a mixture that must be expressed as one object.
- **Benefit not yet mentioned:** the two stages get independent `limitations[]` and per-stage token
  accounting in their own READMEs, which is honest — stage 2's decontamination story is different
  (it holds the QA and reasoning-trace data, which is where contamination concentrates).

**EFFORT: zero code.** It is a naming and plan decision. Both `dataset_id`s validate:
`contracts.validate_dataset_id` accepts any `pretrain/<safe-name>` (`FAMILIES` includes `pretrain`,
`contracts.py:130`). ⚠️ Avoid `final` as a version-ish token — `pretrain/reservoir-final` was checked
and is **correctly REJECTED** because `final` reads as a version (`PUBLISH-SPEC.md:22`). Test the
exact names with `validate_dataset_id` before committing.

---

## Q7 — The interleaving requirement (task #15): is it possible?

### F7.1 **DEFINITIVE ANSWER: a label is per-SHARD, derived from the object's KEY. An interleaved shard CANNOT carry per-source labels. The micro-batch fix must be TRAINER-SIDE.**

This is the question the never-launched agent brief #14 would have answered
(HANDOFF-FINAL-DATASET.md:237). Here is the answer with the chain of evidence.

**Step 1 — a label is a property of the ENTRY, and an entry is one object.**
`ManifestEntry.labels: dict[str, str] | None` (`manifest.py:233`), one per row of the group manifest.
`build_manifest(entries, group_name=g)` takes a list of entries, one per file (`publish.py:481`).
**There is no per-document, per-byte-range, or per-offset label structure anywhere in the schema.**

**Step 2 — the label is DERIVED FROM THE KEY, not supplied.** `publish()` builds every entry with
`labels=derived_labels or None` where `derived_labels = labels_from_path(rel)` (`publish.py:438-464`;
the assignment is at `:464`). The comment at `:459-462` is explicit: *"Both derived from the key
itself, **never asked of the caller**, and both recomputed by Gate A from that same key — so neither
can drift from the object it describes."* `labels_from_path` is a pure string split on the directory
segments between the group and the basename (`manifest.py:727-742`).

**Step 3 — Gate A REJECTS any label that does not equal the recomputation from the key.**
`_check_labels_match_path` compares `declared != expected` by **full dict equality**
(`validate.py:1380-1430`, and the mechanism is named in `manifest.py:717-719`). So even if a producer
hand-wrote `{"source": "mixed"}` or `{"source": "dclm+finemath"}`, that is either (a) rejected
because it contradicts the key, or (b) accepted only if the key literally says `tokens/mixed/` —
i.e. **the label can only ever name ONE value per shard, and that value is whatever single directory
segment the shard sits under.**

**Step 4 — a value is a flat string. There is no list type.** `manifest.py:295-300` requires
`labels` be a Mapping of non-empty string keys, and the docstring at `:230-233` states the rule:
*"Flat and string-valued ONLY, deliberately: a partition selects by exact label match, so a nested
or richly-typed value would need a query language and a validator nobody has written."*

**Step 5 — and the label is inside the hash chain, so it is unbackfillable.**
`manifest.py:739-741`: *"`entry.labels` is inside `manifest_sha256`, so a label that is wrong today
cannot be fixed without republishing the payload."*

**CONCLUSION.** Option (a) — interleave documents from multiple sources into each shard at build
time — **destroys per-source selection**. A shard at `tokens/mixed/train-00042.u32le.bin` carries
`{"source": "mixed"}` and nothing else. Consequences, each verified in code:
- `build_mixture` becomes **useless**: every component's predicate would match the same set, or one
  `{"source": "mixed"}` predicate would match everything. `MixtureSource.labels` is the only
  selection mechanism (`read.py:1070`, `_matches_labels`), and the whole per-source share machinery
  collapses.
- `dataset_paths(labels={...})` likewise (`read.py:490-501`).
- **`max_source_fraction`, `max_repetition_ratio`, `shortfall`, `actual_ratios`, and the epoch guard
  all become unreportable** — they are all keyed on `src.name`, which is built from `labels`
  (`MixtureSource.name`, `read.py:922-925`).
- The baseline model's whole plan breaks. HANDOFF-FINAL-DATASET.md:201-202: *"The baseline model
  reuses the same corpus at a different ratio vector (report §9) — shed knowledge-shaped data, raise
  reasoning-shaped. **No second dataset, no re-ingest.**"* That re-weighting is a `build_mixture`
  call over per-source labels. **Interleaving at build time makes the baseline model impossible
  without a second ingest.**

**Also note: the packer could not easily do it anyway.** `pack(streams, refs, ...)` maps ONE
`(source, domain, split)` triple to one document iterable and refuses any mismatch between the
stream set and the ref set (`corpus_pack.py:658-666`, the `orphan_refs`/`orphan_streams` raise).
`_pack_stream` fills one stream's refs from one carry buffer (`corpus_pack.py:703-760`). Interleaving
would mean fusing sources upstream of `pack` and labelling the result with a single fused `source` —
which is Step 3's problem again.

**SEVERITY: blocks the build IF option (a) is chosen. Choosing option (b) makes it operational only.**

### F7.2 The fix is trainer-side, and the repo already identifies the exact setting.

**FINDING.** Option (b) — leave shards pure, fix it in the data loader — is the only viable one, and
the report already names the mechanism: *"Our shards are per-source, so every micro-batch is
domain-pure by construction, and the relevant setting defaults to per-batch scope in four places in
the trainer. **It must be fixed before training starts:** switching at 10% of the run recovers only
~55% of the gap"* (`docs/FINAL-DATASET-REPORT.md:319-324`, MEASURED-ELSEWHERE). The concrete item is
`MoELoadBalancingLossGranularity`, *"defaults to `local_batch` in four places"*
(HANDOFF-FINAL-DATASET.md:211-214).

⚠️ **But note these are TWO different fixes and the report partly conflates them:**
1. **Load-balancing loss GRANULARITY** — computing the aux loss over a wider scope than the local
   micro-batch. A pure config change in OLMo-core. **This does not interleave anything**; it stops
   the aux loss from *rewarding* domain-pure routing.
2. **Actual micro-batch composition** — the loader drawing from multiple shards per micro-batch. Our
   own memory records the measured value of this (0.13-0.18 PPL, +5-6 GSM8k, arXiv 2501.11873) and
   that it must be set before training.

For (2), the corpus side's obligation is only to hand the trainer **many shards per source** so its
shuffler has something to interleave. At 50,003,968-token shards and ≥180 shards for even the
smallest 1% component (F6.2), that obligation is met. **The corpus does not need to change at all.**

**FIX / effort: ZERO on the data side.** Write it down as a **consumer obligation** in the published
dataset's `notes` — this is exactly the kind of fact `docs/CONSUMER-CONTRACT.md` §7 ("What is NOT
guaranteed") exists to carry, and an adapter author who does not know shards are domain-pure will
build a loader that reads them in order. One `notes=` sentence at publish time
(`publish(notes=...)`), plus the OLMo-core config change, which belongs to the training repo.

**Recommended `notes` text:** *"Every shard contains documents from exactly one source (the `source`
label equals the key segment), so a loader that reads shards sequentially produces domain-pure
micro-batches. Interleave across shards in the data loader, and set the MoE load-balancing loss
granularity wider than `local_batch`."*

---

## Q8 — The `keeps_id` gap (task #4)

### F8.1 CONFIRMED: `keeps_id` and `IdSet` are called from NOTHING that writes corpus data.

**FINDING.** `grep` over `src/edullm_data/` for `keeps_id|partition_of|format_for_id|reservoir_ids|IdSet`:
- `keeps_id` has exactly **one** caller in the package: `ingest_reservoir._partition_report`
  (`ingest_reservoir.py:743`), a **reporting** function — `kept = [i for i in ids if keeps_id(config,
  i)]` — whose output goes into `_index.*.json` / the `plan` printout. It **decides nothing about
  which documents get tokenized.**
- `IdSet` is built and uploaded by `_cmd_ids` (`ingest_reservoir.py:846`, `:916`) and merged by
  `_cmd_merge` (`:1001-1004`). **Nothing reads it back.** `IdSet.from_bytes` is called only inside
  `_cmd_merge`'s own part-reassembly.
- `corpus_read.py`, `corpus_build.py`, `corpus_pack.py`, `corpus_filter.py` mention it **only in
  prose** — `corpus.py:175-176` and `corpus_read.py:513`, `:733` cite it in docstrings explaining why
  `Document.id` must be stable, and `corpus_build.py:49` and `corpus_receipt.py:935` cite the
  *refusal* it protects. **Zero code references.** ✅ The prior finding is exactly right.

**What the two mechanisms do (MEASURED-IN-CODE).**
1. **The 4-way partition.** `partition_of(doc_id) = int.from_bytes(sha256(id), "big") % 4`
   (`reservoir_ids.py:102-112`), and `format_for_id` maps that index into the FIXED tuple
   `FINEPHRASE_FORMATS = ("faq","math","table","tutorial")` (`reservoir_ids.py:65`, `:115-117`).
   `keeps_id(fmt, id)` is `format_for_id(id) == fmt` (`:120-132`). **`sha256` and not `hash()`
   because Python's builtin is salted per process** (`reservoir_ids.py:35-40`), which would assign
   the same document to different formats on different workers, silently.
2. **The edu-web anti-join.** `IdSet` holds sorted 64-bit sha256 prefixes; `contains(doc_id)` is a
   `np.searchsorted` (`ingest_reservoir.py:617-622`). Chosen over a Bloom filter because *"a Bloom
   filter's 1% FPR would drop ~1% of edu-web silently, which is 2.6 B tokens"* whereas 64-bit
   collisions over 339M ids are `n²/2^65 ≈ 0.003` expected (`ingest_reservoir.py:569-574`).

**Why it matters, quantified.** The four FinePhrase configs are *"ONE corpus rephrased four ways over
the same ~339 M FineWeb-Edu documents, measured at 91.0-92.9% pairwise id overlap"*, and *"a content
digest sees four DIFFERENT strings, so exact dedup passes; MinHash at the usual threshold sees four
differently-worded texts, so fuzzy dedup passes; every token count still adds up, so no sizing check
fires"* (`reservoir_ids.py:9-19`). **So the reservoir's published 59.6B "synthetic" tokens rest on
~17B of distinct documents, and no gate in this pipeline can see it.**

**And it is BUILD-TIME irreversible:** *"after tokenization there is no document→id mapping left, so
redoing it means re-tokenizing the synthetic half"* (`reservoir_ids.py:28-33`).

**SEVERITY: blocks the build.** Not because the job fails — because it succeeds while producing a
corpus whose declared composition is wrong by ~3.5x on the synthetic component, and the defect cannot
be repaired without re-tokenizing.

### F8.2 EXACTLY where it must be wired in

There is one correct insertion point, and it is not `corpus_filter`.

**The place: `corpus_build._reader_for`, in the inner `for doc in reader(...)` loop
(`corpus_build.py:927-929`).** Reason: it must run on a `Document` that still has `.id`, before
`carve` (which routes by id but does not drop), before `dedup_and_decontaminate` (which is
content-based), and before `tokenize_documents` (after which the id is gone). `_reader_for` is the
only function that sees `(spec, bundle, doc)` together and can therefore know *which* partition
predicate applies to *this* source.

**Why not `corpus_filter.dedup_and_decontaminate`?** Because its signature is `(docs, *, index, seen,
stats)` (`corpus_filter.py:286-292`) — it has no `spec`, no `bundle`, no source identity, so it cannot
know whether to apply the `faq` predicate or the `table` one. Threading a source in would make a
content-filter aware of provenance, and the `FilterStats` identity `seen == kept + duplicates +
contaminated` (asserted in tests and reported in the receipt, `corpus_build.py:491-495`) would break.
**Add a THIRD stats object instead**, mirroring the existing two-stats decision.

**The concrete change, function by function:**

| # | function | file:line | change |
|---|---|---|---|
| 1 | `CorpusSpec` | `corpus.py:197-266` | add two fields: `id_partition: str \| None = None` (the partition key this source keeps, e.g. `"faq"`) and `anti_join_key: str \| None = None` (the S3 key of an `IdSet` this source must EXCLUDE, e.g. `_ids/finephrase-all.u64` for the fineweb-edu row). Both default `None` = no filtering, so every existing row is unchanged. |
| 2 | `plan_document` | `corpus_build.py:252-288` | carry both onto each bundle entry, alongside `text_column` / `id_column` (`:279-280`). ⚠️ **This changes `plan_id`** (it is the sha256 of the whole doc, `:289-291`) — so it must land BEFORE the plan is frozen (F1.2). |
| 3 | `Bundle` / `from_plan_entry` | `corpus_build.py:300-368` | two more fields read off the entry. |
| 4 | `run_bundle` | `corpus_build.py:429-443` | accept an optional `id_set: IdSet \| None`, loaded once per job like `index` is. |
| 5 | **`_reader_for`** | `corpus_build.py:926-931` | the actual predicate: filter inside the loop, count into a new `IdStats`, and **RAISE if the realized keep fraction misses the required floor** (`_REQUIRED_FRACTION_PCT`, `ingest_reservoir.py:762`) rather than silently under-delivering. |
| 6 | `_cmd_run` | `corpus_build.py:684-692` | load the merged `IdSet` from `s3://edullm-landing/_ingest/.../\_ids/...` exactly the way `load_index` is loaded, and **RAISE when absent unless an explicit `--no-id-partition` flag is passed** — copying `--no-decontaminate`'s design (`:685-690`), whose comment says the reason: *"a build that silently skipped would produce a corpus indistinguishable from a [treated] one, discovered when a benchmark score looks too good months later."* |
| 7 | `_reader_for` budget | `corpus_build.py:923-924` | ⚠️ **the character budget must be divided by the keep fraction**, exactly as it already is for `keep_rate`. A `faq` bundle keeping 25% of what it reads needs 4x the budget or every shard comes up short. `Bundle.keep_rate` (`:331-340`) is the pattern to extend: `keep_rate *= id_keep_fraction`. **Missing this is the failure mode that would waste a whole wave** — the bundle would run to completion and `verify` would refuse it for unfilled refs. |
| 8 | `Receipt` | `corpus_receipt.py:224-274` | add the id-filter counts so the realized distinct-document base is auditable from the artifact, not just from a log line. |

**The test that proves it (three tests, and the first is the load-bearing one):**

1. **Disjointness end-to-end, at the driver level, not the predicate level.** Build four bundles from
   ONE injected document list (the `documents=` seam `run_bundle` already exposes,
   `corpus_build.py:437`, `:447-448`), one per FinePhrase format, and assert the four output
   *document-id sets are pairwise disjoint and their union is the input*. This is what
   `tests/test_reservoir_ids.py:52-60` already asserts about `keeps_id` in isolation — the new test
   asserts the wiring, which is the thing that is missing. **A test that only calls `keeps_id` again
   would pass today and prove nothing.**
2. **The anti-join fires.** A FineWeb-Edu bundle given an `IdSet` containing 3 of its 10 document ids
   must yield 7 documents, and the receipt must report 3 excluded.
3. **The floor guard fires.** A partition realizing below `_REQUIRED_FRACTION_PCT` must raise, not
   under-deliver. `audit_partition(required_min_share_pct=...)` already implements the raise
   (`reservoir_ids.py:204-211`); the test proves the driver calls it.

**EFFORT: ~150-200 lines across 5 files + 3 tests. Half a day.** Items 1-6 are mechanical; **item 7
(the budget scaling) is the one that is easy to get wrong and expensive to discover**, and item 2
means this cannot be retrofitted after the plan is frozen.

**MEASURED-ELSEWHERE, the margin is comfortable:** the partition was verified on real ids —
`artifacts/reservoir/id-partition-verification.json` shows all four configs at **24.86-25.26%**
against required floors of 10.1 / 15.8 / **17.3** / 10.1%, `worst_deviation_pp: 0.2672` over 287,000
distinct ids. **~1.44x margin on the tightest case (`table`).** The predicate is proven; only the
wiring is absent.

---

## Q9 — The EOS-fraction gate interaction

### F9.1 The gate: three independent checks, all recomputing from bytes. The arithmetic confirmed.

**FINDING (MEASURED-IN-CODE).** `families/pretrain.json` → `defaults.decode_smoke_test.eos_fraction_max
= 0.05` (verified by reading the file). Three enforcement points:

1. **`estimate_eos_fraction(mean_doc_tokens) = 1.0 / mean_doc_tokens`** (`corpus_pack.py:469-480`).
   Exact, not approximate: one EOS per document and no padding means the EOS count in a shard **is**
   the number of documents ending in it.
2. **`assert_eos_fraction_publishable(stream, documents, tokens, ...)`** — the **stream-level** gate
   at end of `_pack_stream` (`corpus_pack.py:483-516`, called at `:816-818`). Uses the **realized**
   mean.
3. **`_verify_shard`** — the **per-shard AND per-window** gate, run on the buffer *before* it is
   written (`corpus_pack.py:916-1021`). Two tiers: whole-shard fraction (`:975-981`) then
   `_max_window_fraction(ids == eos_id, DECODE_WINDOW_TOKENS)`, a **sliding** window (`:982-983`,
   `:1033-1057`). `DECODE_WINDOW_TOKENS = 65536/4/4 = 4,096` (`corpus_pack.py:151`). Sliding rather
   than tiled *"because non-overlapping windows can miss a spike straddling a boundary by up to 2x,
   and the spike IS the failure"* (`:1035-1037`) — so passing at build time is **sufficient** for
   Gate A, not merely necessary (`:928-935`).

`_family_decode_bounds()` reads the bounds from the family file and **raises if it cannot**
(`corpus_pack.py:176-227`), the opposite of Gate A's degrade-to-laxer-default — deliberately, because
a packer that degrades *"would report every shard clean while writing shards Gate A will reject"*
(`:181-188`). It also cross-checks the file value against `corpus.FAMILY_MAX_EOS_FRACTION` and raises
on divergence (`:214-223`). **This is well built.**

**The floor, DERIVED:** `1/mean ≤ 0.05 ⟺ mean ≥ 20`. ✅ Confirmed; `MIN_MEAN_DOC_TOKENS = 20` with the
arithmetic spelled out at `corpus.py:122-138` (*"at a mean of 20 tokens the fraction is exactly
0.0500; at 16 it is 0.0625 and the shard is REJECTED"*).

### F9.2 The `min_tokens` interaction: `MIN_DOC_TOKENS = 64` gives a **3.2x guaranteed** margin, by construction.

**FINDING.** `MIN_DOC_TOKENS = 64` (`corpus.py:149`), and it is applied inside
`tokenize_documents(min_tokens=...)` from the ids `encode_batch` already produced
(`corpus_pack.py:342-355`), fed by `run_bundle` as `min_tokens=plan["min_doc_tokens"]`
(`corpus_build.py:499-502`). The reasoning at `corpus.py:141-149`: *"At a 64-token floor the **worst
possible** shard mean is 64, giving an EOS fraction of 0.0156, a 3.2x margin under the family
bound."* **This is a proof, not an estimate** — after the filter no document is under 64 tokens, so
no arithmetic mean of surviving documents can be under 64. **The gate cannot fire as long as
`min_doc_tokens ≥ 20` is enforced.**

⚠️ **The one way to break it: setting `min_doc_tokens` in the plan below 20.** `tokenize_documents`
only refuses `min_tokens < 1` (`corpus_pack.py:290-296`), and `plan_document` writes
`"min_doc_tokens": MIN_DOC_TOKENS` from the constant with **no override parameter**
(`corpus_build.py:256`). So today it is safe. **A future plan-level override must assert
`≥ MIN_MEAN_DOC_TOKENS`** — there is no such assertion anywhere. Effort to add: 3 lines.
**SEVERITY: operational only** (latent, not live).

### F9.3 Per-source verdict: **every measured source clears the floor with ≥13x margin. No source is at risk.**

**MEASURED-ELSEWHERE — realized means, from the 27 reservoir receipts**
(`artifacts/reservoir/realized-tokens.json`, `total / documents`, DERIVED arithmetic on measured fields):

| source | tokens / documents | mean tok/doc | EOS fraction | margin vs 20 |
|---|---|---|---|---|
| synthetic-finephrase-**table** | 14.95B / 56.84M | **263.0** | 0.00380 | **13.2x** ← tightest |
| synthetic-finephrase-math | 14.95B / 48.21M | 310.1 | 0.00322 | 15.5x |
| synthetic-finephrase-tutorial | 14.95B / 34.49M | 433.5 | 0.00231 | 21.7x |
| synthetic-finephrase-faq | 14.95B / 33.81M | 442.2 | 0.00226 | 22.1x |
| stackexchange | 9.95B / 13.65M | 728.8 | 0.00137 | 36.4x |
| stackv2-edu | 39.95B / 42.37M | 943.0 | 0.00106 | 47.2x |
| fineweb-edu | 19.95B / 19.81M | 1007.1 | 0.00099 | 50.4x |
| dclm | 29.95B / 23.74M | 1261.5 | 0.00079 | 63.1x |
| finewiki | 7.95B / 6.02M | 1320.1 | 0.00076 | 66.0x |
| finemath | 33.98B / 21.28M | 1596.3 | 0.00063 | 79.8x |
| finepdfs-edu | 27.98B / 4.95M | 5655.7 | 0.00018 | 282.8x |
| peS2o | 13.98B / 2.16M | 6474.0 | 0.00015 | 323.7x |
| pubmed | 5.98B / 0.75M | 7917.9 | 0.00013 | 395.9x |
| ubuntu-irc | 1.75B / 0.20M | 8650.7 | 0.00012 | 432.5x |

**And the pre-filter upstream means agree** (`artifacts/recount/*.json`,
`mean_tokens_per_doc`): fineweb-edu 1177.51, finepdfs-edu 11752.12, dclm 1396.16/1524.89,
finemath-4plus 1504.88, swallow-math-v2 1416.67, finewiki-en 986.20, finephrase faq/math/table/tutorial
438.47 / 282.30 / 265.25 / 436.26.

**The tightest source is `finephrase-table` at 263 tok/doc — 13.2x margin. Nothing is close to the
gate, and the STREAM-level check is not the risk. The per-WINDOW check is.** A 4,096-token window
needs `> 204 EOS` to fail, i.e. a run of ~20 consecutive documents averaging under 20 tokens. **The
64-token filter makes that impossible by construction** (20 documents × 64 tokens = 1,280 tokens,
2 orders of magnitude short of 204 EOS in 4,096 tokens).

### F9.4 ⚠️ **The real short-document risk is real, and it is FinePhrase — but the filter already handles it, and it was nearly the opposite mistake.**

**MEASURED-ELSEWHERE, `artifacts/recount/synthetic.json` `short_rewrite_stats`, n=159,961 rewrites
from 160 distinct files at random row-group offsets, dolma2-tokenizer:**

| config | mean | median | under_16 | under_50 | under_100 |
|---|---|---|---|---|---|
| faq | 438.47 | 412 | 2.07% | 5.30% | 8.63% |
| **math** | 282.30 | 234 | 2.12% | **12.56%** | 22.27% |
| table | 265.25 | 210 | 1.08% | 3.11% | 11.59% |
| tutorial | 436.26 | 409 | 1.54% | 3.92% | 7.23% |

Verbatim degenerate rewrites recorded: `"Yes"` (13x), `"FAQ"` (9x), `"Answer:"` (8x), `"Document:"`
(6x faq + 3x tutorial), `"---"` (11x table), `"No table is provided."` (4x), `"<no text provided>"`
(3x). And a finding worth carrying forward: **`finish_reason` is ANTI-correlated with quality** —
*"every degenerate string above carries `finish_reason='stop'`... filtering on `finish_reason !=
'stop'` would delete the 2048-token outputs and keep 'No answer'. **Length remains the only
signal.**"* This is exactly why `MIN_DOC_TOKENS` is the mechanism.

**Cost of the 64-token filter (DERIVED from the measured `>=50` retention, a lower bound on the cost
at 64):** tokens retained 98.75-99.80%, documents retained 87.36-96.63%. **`math` is the worst case:
it loses 12.6% of its documents for 1.25% of its tokens.** Post-filter means rise to 274-463 tok/doc.

⚠️ **DO NOT raise the threshold.** `synthetic.json`'s own verdict: *"Do NOT set the threshold above
~200 tokens: at >=200 `table` loses 23% of its tokens, because table rewrites are legitimately short
(a markdown table IS a short document) — a high threshold would silently reshape the format mix
rather than remove failures."* 64 is the right value and is well below that cliff.

⚠️ **And note the near-miss that produced this constant.** Phase 0's `n=34` head sample measured
`mean 40.5, median 33, 67.6% under 50` — **wrong by 10x**, because parquet row order is
content-clustered and the first ~34 rows of `faq` are a genuinely degenerate contiguous block
(`synthetic.json` `verdict_on_phase0_head_sample`). Had that figure been believed, the EOS gate would
have appeared to be a blocker for the whole synthetic half. **Sample at random offsets, never at the
head** — the same lesson our memory records about the HF preview endpoint.

### F9.5 ⚠️ **UNMEASURED: five of the nine stage-2 sources have NO mean-document-length measurement.**

**FINDING.** `docs/FINAL-DATASET-REPORT.md:77-87` lists the stage-2 mix. Cross-referencing
`artifacts/recount/*.json`, these have **no `mean_tokens_per_doc` anywhere in the repo**:

| stage-2 source | share | mean doc tokens | risk assessment |
|---|---|---|---|
| Nemotron-CC-Math-3+ | 16% | **UNMEASURED** (the whole 133B figure is CARD-grade, report §12) | Math web text; FineMath measures 1504.88, Nemotron-CC-Math is the same shape. **Low risk.** |
| AI2 dolma3 midtraining mix (QA-bearing) | 14% | **UNMEASURED** | ⚠️ **THE ONE TO CHECK.** It is *"GPT-4o-mini-rewritten **multiple-choice** [items] from academic subreddits"* (HANDOFF-FINAL-DATASET.md:82). **An MC question with four options is plausibly 60-150 tokens** — above the 64 floor but the closest of anything in the mix, and the floor would then *drop* a real fraction of it. |
| reasoning traces / worked examples | 8% | **UNMEASURED** | Worked solutions are long. **Low risk.** |
| Cosmopedia (synthetic) | 4% | **UNMEASURED** | Synthetic textbook prose, ~1000 tokens typical. **Low risk.** |
| Nemotron Math-Textbooks | 3% | **UNMEASURED** | Textbook prose. **Low risk.** |

**SEVERITY: needs measurement before ingest** (one source), **operational only** for the rest.

**FIX.** For the QA source, the check is cheap and needs no download: sample 400 documents at
**random row-group offsets** (not the head — F9.4) and tokenize them, exactly as
`artifacts/recount/recount.py` and `_filtered_tpb.py` already do over HTTP Range. Report
`mean_tokens_per_doc`, `median`, and `frac_under_64`. **If `frac_under_64 > ~10%`, the 64-token floor
is silently reshaping the QA component's mix** — which matters because the whole point of that
component is format-matching to MMLU/ARC, and dropping the shortest items biases toward long-stem
questions. That is a *composition* problem, not an EOS problem. Effort: ~1 h of a FarmShare/Batch job
using the existing script.

**One structural note:** if a QA source genuinely has a mean near or below 64, the sanctioned answer
is **not** to lower `MIN_DOC_TOKENS`. It is to **concatenate several QA items into one document
upstream of the reader** — which is what every other pretraining corpus does with short-form data.
`corpus.py:146-149` forbids the packer from doing it (*"concatenating two documents into one loses
the boundary that the EOS marks"*), correctly; the fusion belongs in the reader or in a pre-staged
artifact, where it is a deliberate content decision with its own document ids.

---

# The build as I would actually execute it

## Severity roll-up

**Blocks the build (must be resolved before job 1):**
- **F1.2** — adding a source renumbers every later source's ordinals; incremental per-source ingest
  throws away all prior work. **Fix: freeze the whole plan up front (0 code) or namespace ordinals (~10 lines).**
- **F8.1** — `keeps_id` is wired into nothing; declared synthetic volume rests on ~28% as many
  distinct documents, and it is unrepairable after tokenization. **Fix: ~150-200 lines, 8 touchpoints
  (F8.2). Must land before the plan is frozen because it changes `plan_id`.**
- **F5.2** — a 20,000-object Gate A takes ~2.8 h at defaults and ~2.35 h at `head_workers=16`;
  the validator job def is 7200 s at best-known revision and the live revision-12 timeout is
  UNVERIFIED. **Fix: raise the timeout (0 code) AND/OR task #10 (~100 lines).**
- **F7.1** — interleaving documents into shards at build time would destroy per-source labels and
  make the baseline model impossible without a re-ingest. **Fix: don't do it; the fix is trainer-side.**

**Needs code before ingest:**
- **F2.3** — `_reader_for` has no file-sharding, so a big source is one atomic bundle AND sixteen
  domain-bundles would read the same files sixteen times. **~30 lines. Highest value change in this audit.**
- **F4.2** — the reader over-reads 2.1x and val bundles over-read 200x; ~18 TB pulled for 4.2 TB of
  text. **~15 lines (per-source chars/token + a `--val-fraction` flag).**
- **F4.3** — `corpus_read`/`corpus_build`/`corpus_pack`/`corpus_filter` have ZERO threading.
  Mitigable by child-level fan-out with no code.

**Needs measurement before ingest:**
- **F9.5** — the AI2 dolma3 midtraining (QA) source's mean document length is unmeasured and is the
  one source plausibly near the 64-token floor. ~1 h.

**Operational only:** F1.1, F2.2, F2.4, F2.5, F3.1, F3.2, F3.3, F9.1-F9.4, F6.1-F6.2.

## The shape

**Two datasets, one plan each, frozen before the first job.**
`pretrain/<name>-stage1/v1` (~900B, ~18,000 shards) and `pretrain/<name>-stage2/v1` (~100B, ~2,000
shards) at the decided `SHARD_TOKENS = 50,003,968` — which is a multiple of `SEQ_LEN`
(`50,003,968 / 8192 = 6104` exactly, so `_assert_ref_alignable` passes, `corpus_pack.py:687-701`) but
**is not yet the value in the code** (`corpus.py:89` still says `3052 * 8192`).

Rationale, all from the audit: F1.3 (ordinal isolation), F5.4 (each stage validates independently),
F6.3 (`build_mixture` is one-dataset / one-group / one-split by construction).

## Phase 0 — code and measurement, ZERO AWS jobs (est. 1.5-2 days)

| # | change | file(s) | effort |
|---|---|---|---|
| 0.1 | Wire `keeps_id` + the `IdSet` anti-join, 8 touchpoints per **F8.2**, incl. the budget scaling in item 7 | `corpus.py`, `corpus_build.py`, `corpus_receipt.py` | ~200 lines, 3 tests |
| 0.2 | File-sharding in `_reader_for` via the existing `_shard_slice` | `corpus_build.py` | ~30 lines, 3 tests |
| 0.3 | `SHARD_TOKENS` → 50,003,968; re-verify `% SEQ_LEN == 0` and the F1.2 ordinal headroom | `corpus.py` | 1 line + test |
| 0.4 | Per-source `chars_per_token` from `artifacts/reservoir/chars-per-token.json`; `--val-fraction` on `plan` | `corpus_build.py` | ~15 lines |
| 0.5 | Assert `min_doc_tokens >= MIN_MEAN_DOC_TOKENS` on any plan override (**F9.2**) | `corpus_build.py` | 3 lines |
| 0.6 | Seed `ctx.observations["object_sizes"]` from `_prefetch_heads` — free 20,000-call saving (**F5.3**) | `validate.py` | 3 lines |
| 0.7 | Task #10: thread the profile checks' ranged GETs via a pure prefetch, per **F5.3** | `pretrain_tokens_v1.py`, `validate.py` | ~100 lines + determinism test |
| 0.8 | Measure the QA source's mean document length at random row-group offsets (**F9.5**) | `artifacts/recount/` | ~1 h, one small job |
| 0.9 | Bump `__version__` (currently `0.9.1`) and dispatch the image build — **only `edullm/**` branches build** (`CLAUDE.md`) | `__init__.py` + `gh workflow run` | 15 min |

**Then freeze both plans** and record their `plan_id`s. **After this point, adding a source to a plan
is a full restart of that stage.**

## Phase 1 — build, decomposed into separately-approved submissions

Each row is one platform submission. Timeouts are **28800 s (8 h)** with `attempts: 2` — retry is
safe on build because `bundle_is_done` skips finished bundles (F2.2); it is **not** safe on publish
(`infra/10-dataset-publish-jobdef.md:79`).

| job | scope | shape | wall-clock (DERIVED) | notes |
|---|---|---|---|---|
| **1a** | `plan --upload` for stage 1 | 1 child, 2 vCPU | < 5 min | writes `plan.json`; prints `plan_id`, bundle and shard counts |
| **1b** | `plan --upload` for stage 2 | 1 child, 2 vCPU | < 5 min | |
| **1c** | **SMOKE: one bundle end-to-end** | `run --of <n_bundles>` on the smallest source | 1-2 h | ⚠️ **MANDATORY.** `_reader_for` is *"UNVERIFIED against live HF from inside a Batch container — every offline test injects `documents=`"* (`corpus_build.py:901-904`), and it says to settle it exactly this way before committing an array. |
| **1d** | id-partition scan for the synthetic sources | array 10 × 4 workers | ~1-3 h, ~$1 | `ids` then `merge`. `merge` **refuses an incomplete part set** — re-run only failed indices. `RUN-THE-INGEST.md` is the runbook. |
| **2.1-2.N** | stage-1 train bundles | **~90-100 bundles at ~8-10B each**, run as **4-6 array waves of 16-25 children**, 8-16 vCPU per child | **2-6 h per child**, ~2-3 days elapsed across waves | download-bound per child, tokenize-bound in aggregate against the **128 vCPU** cap (F3.3, F4.4) |
| **2.V** | stage-1 val bundles | 1 wave | 4-8 h | ⚠️ **val reads 200x its own tokens** (F4.2). Lower `val_fraction` to 0.001 and this becomes ~1 h. |
| **3.1-3.N** | stage-2 bundles | ~12-15 bundles | 1 wave, 2-4 h | |
| **4a** | `verify --plan-id <s1>` (cheap tier) | 1 child, 4 vCPU | ~15 min | refuses an incomplete build — a gate, not a report (`corpus_build.py:44-50`) |
| **4b** | `verify --plan-id <s1> --deep --hash-workers 8` | 1 child, 16 vCPU, **timeout 14400 s** | **~1.5 h** for 3.6 TB | 3.60 TB / (87.8 MB/s × 7.82) = 1.46 h. ⚠️ `edullm-reservoir-verify:1` **does not pass `--hash-workers`** — needs a new revision (`PUBLISH-SPEC.md:169-170`). This is the ONLY payload re-hash in the pipeline. |
| **4c** | `verify --deep` stage 2 | 1 child | ~10 min | 0.40 TB |

**Total tokenize cost (DERIVED, F3.1 + F4.2's 2.09x over-read): ~$80 on-demand, ~$25 spot.** Data
transfer IN is free; **confirm the compute environment sits on a public subnet with an internet
gateway, not behind a NAT gateway** — 18 TB × $0.045/GB = **$810** if it is (F4.4).

## Phase 2 — publish, validate, promote (per stage, sequentially)

⚠️ **`edullm-landing-manifest-created` must be DISABLED first, or writing `manifest.json` fires
EventBridge → the validator → auto-promotion** (`PUBLISH-SPEC.md:182-188`, and our own memory:
"publishing to landing auto-promotes"). **Decide before publishing, not after.** The owner's standing
instruction is never auto-publish (HANDOFF-FINAL-DATASET.md:19), so: disable the rule, then submit
each validator job manually, by **unversioned** job-def name.

| job | scope | shape | wall-clock (DERIVED) |
|---|---|---|---|
| **5a** | `publish()` stage 1 with `hash_workers=16, copy_workers=16` | 16 vCPU / 32 GiB, **timeout ≥ 21600 s** | **~1 h** (3.60 TB / 1053.6 MB/s). Single-threaded it is **12.7 h** and would blow any timeout. |
| **5b** | Gate A + promote, stage 1: `--head-workers 16 --promote-workers 16` | 16 vCPU, **timeout ≥ 14400 s** | **~1.6 h today**; **~8 min with task #10** (F5.2/F5.3). Promote adds ~3 min. |
| **6a** | `publish()` stage 2 | same | ~7 min |
| **6b** | Gate A + promote, stage 2 | same | ~11 min today; ~1 min with task #10 |

**Two hard preconditions on every job above, both of which have already cost this project a full
diagnosis cycle:**
1. `executionRoleArn` **must** be set, or the container never starts and there are no readable logs
   — the symptom mimics a missing log group (`infra/10-dataset-publish-jobdef.md:68`).
2. The **image must be the one that contains the Phase-0 commits.** `assert __version__ == '<x>'`
   **cannot** identify the code — two commits both declared `0.7.4` with different gate bounds
   (`PUBLISH-SPEC.md:194-208`). Check ancestry with `git merge-base --is-ancestor` against the ECR
   tag, which is a commit sha.
3. `hash_workers`/`head_workers`/`copy_workers` > 10 need a matching `max_pool_connections`, or
   workers 11..N silently pay a fresh TLS handshake per object with no error anywhere
   (`corpus_build.py:571-581`, `validate.py:2478-2492`, `infra/10-...:72-78`).

## Publish-time metadata that must not be forgotten

Both `publish()` calls need, per this audit:
- `limitations[]` — the synthetic portion is effectively undecontaminated (rephrasing defeats n-gram
  matching), stage 2 carries the QA/reasoning data where contamination concentrates, and **Gate A
  does not re-hash payload bytes** so `sha256` is a producer assertion (F5.1's evidence chain,
  `docs/CONSUMER-CONTRACT.md:626-640`).
- `notes` — **F7.2's shard-purity sentence.** Every shard is domain-pure by construction; interleave
  in the loader and widen the MoE load-balancing loss granularity. An adapter author who does not
  know this will build a loader that produces domain-pure micro-batches.
- `notes` — shard granularity 50,003,968 tokens ⇒ per-source mixture weight granularity 0.56% at the
  smallest 1% component (F6.2).
- `sources[]` token counts from the **receipts** (`tokens_out`), never the plan (`PUBLISH-SPEC.md:178-181`).
- Mixed licensing with the share-alike subset called out separately — no single `license.id`.

## The single biggest untested claim in this plan

**`_reader_for` has never run against live HuggingFace from inside a Batch container.** The code says
so itself (`corpus_build.py:901-904`) and names the exact remedy. Everything else in this audit is
either measured or arithmetic; that one is a hole. **Job 1c exists for it, and no array should be
committed before 1c returns exit 0.**

