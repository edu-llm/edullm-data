# Task #28 / node C3b — split the big bundles: findings brief

**Written** 2026-08-08. **Audience:** the agent that implements #28 (or decides not to).
**Read this instead of `IMPLEMENTATION-PLAN.md` §8A.5/§8A.5a alone** — two of that section's
load-bearing numbers are superseded below, and one of its recommended routes does not work.

**Where the code is.** Everything cited lives in the worktree
`/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/final-dataset`, at commit **`8bc3d62`**
(branch `final-dataset`, `__version__` 0.9.1). ⚠️ **Not in the canonical checkout** — I confirmed
`/Users/ericwu/Developer/Capstone_LLM/edullm-data` contains no `corpus_build.py` and no
`docs/IMPLEMENTATION-PLAN.md` at all. If your line numbers do not resolve, you are in the wrong tree.

**How to read the grades.** MEASURED-LIVE = from a real run's logs. MEASURED-IN-CODE = I read the
statement and, where marked, executed it. DERIVED = arithmetic on a graded input; the input's grade
is named. Nothing here is CARD or vibes. Where I did not verify something, it says so.

---

## 1. The question and the answer

**Q:** does splitting DCLM into multiple bundles/jobs reduce build wall clock?

**A: yes, and it is the difference between a job that finishes and one that cannot.** Not the 2.2 h
the dependency graph credits to C3b. Wall clock is set by the slowest array child, and DCLM unsplit
is that child by 20×.

| DCLM 410B | per-child build, 8 vCPU | grade |
|---|---|---|
| unsplit | **196 h** | DERIVED on MEASURED-LIVE rate (§3) |
| split 10 ways | 19.6 h | DERIVED |
| split 20 ways | **9.8 h** | DERIVED |

A Batch child cannot exceed one instance, so no vCPU allocation fixes the unsplit case — 32 vCPU
still leaves it at 49 h. Splitting is the only lever. Every other item on the wave-0 list is
cosmetic next to this.

---

## 2. ✅ The cheapest route is a REGISTRY EDIT, not code — and it is not either route in the plan

`docs/IMPLEMENTATION-PLAN.md` §8A.5a offers two routes: 12 h of plan-time ordinal-range code, or a
synthetic `domain_column` "for free." **There is a third that needs no code**, and the mechanism it
uses is exactly the hard part the 12 h was budgeted to build.

**The route:** split DCLM into N registry rows, each pointing at a **disjoint subdirectory** via
`config`, each with `target_tokens = 410B/N`. Then `plan_document` emits N streams → N bundles →
`_shard_slice` spreads them across children, and `allocate_ordinals` hands each its own dense
ordinal block **at plan time**. Plan-time disjoint ordinal ranges are the thing C3b exists to
create; N rows get them from machinery that already ships.

**Why it works on this source — MEASURED-LIVE, I walked the tree.** `mlfoundations/dclm-baseline-1.0-parquet`
(the 410B repo) nests:

```
filtered/OH_eli5_vs_rw_v2_bigram_200k_train/
  fasttext_openhermes_reddit_eli5_vs_rw_v2_bigram_200k_train/
    processed_data/
      global-shard_01_of_10 … global-shard_10_of_10     <- 10 disjoint dirs, CONFIRMED
        local-shard_0_of_10 … local-shard_9_of_10       <- 100 disjoint dirs, CONFIRMED
          shard_00000000_processed.parquet …
```

`hf_files` builds `{base}/{spec.config}?recursive=1&limit=1000` and paginates on the `Link` header
(`corpus_build.py:813-829`, MEASURED-IN-CODE), so a `config` of
`filtered/…/processed_data/global-shard_01_of_10` resolves and returns `.parquet`. **I fetched that
exact path and it returns files.** Disjoint by construction — no two children can read the same file.

⚠️ **The registry's current DCLM row is a DIFFERENT, smaller repo.** `artifacts/reservoir/corpus-registry.json`
has `dclm-baseline` → `HuggingFaceFW/dclm_100BT`, `config: "data"`, `target_tokens` 30B, pool 114.69B.
I listed it: **100 flat files, no subdirectories**, ~3.1 GB each. So the flat repo has no free carve —
the 10/100-way structure is only in the `-parquet` repo the plan's §284-285 table says to use. Reconcile
which repo the 410B row will name before sizing anything.

### 2.1 Three traps, all verified — two fail SILENTLY

**Trap 1 — each row needs a distinct `source_label`.** `corpus_build.py:238` is
`targets[(spec.source_label, dom, "train")] = share_t`. Two rows sharing a label collapse to one
dict entry. **I executed this**: two rows for `('dclm', None, 'train')` with 100 and 200 tokens
leave `{('dclm', None, 'train'): 200}` — row 1's tokens are gone, no error. `load_registry`
(`corpus_build.py:130-152`) has no uniqueness check; `CorpusSpec.__post_init__` (`corpus.py:245-266`)
checks `source_label` against `SAFE_SEGMENT_RE` and token sanity, **not uniqueness**.

**Trap 2 — do NOT keep one label and vary `domain` instead.** I checked this path; it is worse.
`spec_by_label = {s.source_label: s for s in drawn}` (`corpus_build.py:251`) also collapses, and it
is what supplies `spec_key`/`repo`/`config` to every plan bundle entry (`:274-280`). All N bundles
would inherit ONE config, `_cmd_run` would resolve one spec, and **all N children would read the same
subdirectory — N× duplicate data, silently.** Distinct `source_label` is the only safe shape.

**Trap 3 — the label is permanent and consumer-visible.** `ShardRef.path` → `shard_key(source, domain,
split, ordinal)` (`corpus.py:285-293`) puts source in the path; `labels_from_path` reads it back
(`manifest.py:696`, keys `("source","domain")` at `:693`); Gate A recomputes and rejects a mismatch;
the path is inside `manifest_sha256`. So you publish `dclm-01`…`dclm-20`, and a consumer selecting
`source=dclm` sees twenty labels. Same pollution the plan charges to the synthetic-domain route,
just at the source segment instead of the domain segment. Not a blocker — a decision to make
deliberately, before FREEZE, because it cannot be backfilled.

### 2.2 ❌ Correction: the synthetic-`domain_column` route is NOT free

§8A.5a says a `domain_column` "even a synthetic one derived from the file index" makes DCLM fan out
"for free." **It cannot be derived from the file index.** `_domain_of(row, spec, domain_map=, walk=)`
(`corpus_read.py:322-345`) receives only the parquet **row** — the call site at `:521` is inside the
per-row loop and the file entry is not in scope. A file-index-derived domain is not expressible
without threading the entry through `corpus_read`, which is code. And a *real* column needs a
counting pass first to build `domain_map`, or every distinct value becomes a permanent directory
(the docstring at `corpus_read.py:318-320` says so: 73 for stackv2-edu, ~180 for StackExchange).
Only 3 of 17 registry rows use `domain_column` today, and DCLM is not one.

---

## 3. ⚠️ TWO corrections to the plan's arithmetic — they push in OPPOSITE directions

This is the part most likely to change your decision. **Both corrections are already known to this
project; only one has landed in the docs.**

### 3.1 The throughput is 4.5× optimistic — NOT yet in any doc

Every figure in §8A.5 and §8A.3 uses **0.328 M tok/s/vCPU** (10.5 M across 32 vCPU), sourced to
`corpus_pack.py:230-250`. **That is `encode_batch` in isolation, not end-to-end build throughput.**

MEASURED-LIVE end-to-end: **72,615 tok/s/vCPU**. Provenance: CloudWatch `DONE` lines, log group
`/aws/batch/sbsandbox-intern-edullm-cpu`, streams `reservoir-build/*`, build plan `d5c9bcd38735e1f0`,
2026-08-02→05, 7 train bundles totalling 171B of the 251.2B corpus, 8-vCPU containers
(`edullm-reservoir-build:9` = 8 vCPU / 14336 MiB).

**Why:** tokenize is only ~22% of build cost. ~78% is `corpus_filter.dedup_and_decontaminate` — a
pure-Python generator doing, per document, a sha256 dedup hash, a second sha256 for the decon exact
test, then up to `len(words)-12` `blake2b` 13-gram window hashes against a 3.1M-entry frozenset.
~122B window hashes over the sample, ~0.41M/s/container. **It holds the GIL, so it serializes the
whole pipeline regardless of container size.** `dedup_and_decontaminate` is at
`corpus_filter.py:288-302`; `run_bundle` calls it at `corpus_build.py:482` — before tokenization,
deliberately (§4.1 step 2).

I grepped `docs/IMPLEMENTATION-PLAN.md`, `docs/BUILD-DEPENDENCY-GRAPH.md` and `docs/TASKS.md` for
`72,615` / `72.6` / `filter-bound` at `8bc3d62`: **no hits.** This correction has not propagated.

### 3.2 The vCPU cap is 3× pessimistic — this one HAS landed

The cap is **384 vCPU**, not 128 (`sbsandbox-intern-edullm-cpu`, MaxvCpus 384; account quota 1,152
on-demand and spot). §8A.3 and §8B.5 at `8bc3d62` already carry this. Note `platform/infra/batch-compute.yaml:83`
still says 128, and `artifacts/1t-research/02-pipeline-limits.md` reasons from 128.

### 3.3 Net effect — the two corrections partly cancel, and the split depth changes

| quantity | plan @ `8bc3d62` | corrected | note |
|---|---|---|---|
| aggregate floor, 1.0T | **2.21 h** | **9.96 h** | 384 vCPU, measured rate |
| DCLM unsplit, 32 vCPU | 10.85 h | **49 h** | |
| DCLM unsplit, 8 vCPU | 43.4 h | **196 h** | |
| ways for DCLM to reach the floor | ~7–8 | **≥20** | |
| ways for FineWeb-Edu (252B) | ~5 | **≥13** | |

**Consequence for your sizing:** the 10-way `global-shard_NN_of_10` carve leaves DCLM at 19.6 h —
better than 196, still ~2× the floor. Descend one level to `local-shard_N_of_10` (100 disjoint dirs,
confirmed present) if you want ≥20 ways. **Size at ~20, not 10.**

⚠️ **This also means §8A.3's "the 12 h ordinal route buys only 0.64 h" conclusion is computed from
the 0.328 M rate and should be recomputed** before anyone relies on it to reject C3b. I have not
redone that specific comparison; flagging it, not asserting it is wrong.

---

## 4. What splitting does NOT fix

**The filter becomes the constraint at ~20 ways.** Same MEASURED-LIVE source as §3.1: the
reservoir's longest single bundle was **14.4 h** with the filter unparallelized, and total work was
89.3 container-hours — so with unlimited machines the floor was 14.4 h, and more machines cap at
**6.2×**. Splitting bundles buys the ability to *use* 384 vCPU; it makes no single vCPU faster. That
note estimates parallelizing the filter (multiprocessing over document chunks, or 13-gram hashing in
Rust) takes 14.4 h → ~3.2 h. **Ordering: parallelize the filter before buying machines.**

**Dedup scope narrows, and this is a correctness change, not a free win.** `SeenHashes` is per-bundle
(`corpus_filter.py:290`, `:302`). Splitting DCLM 20 ways means 20 independent dedup sets, so
cross-part duplicates survive. It does help the OOM blocker (#22 / A6: 27.92 GB set in a 15.03 GB
container) — 27.92/20 ≈ 1.4 GB fits — but **the flat `np.uint64` global pre-pass (A2a/A2b) is what
actually fixes dedup.** Splitting just stops it crashing. Do not let it be read as closing #22.

**Bundles remain atomic.** `pack` cuts shards from one carry buffer spanning documents and files
(`corpus_pack.py:593-601`), which is also why resume is bundle-granular (`bundle_is_done`,
`corpus_build.py:387`). N smaller bundles = finer resume granularity, a side benefit.

---

## 5. Everything I did NOT verify — do not treat these as settled

1. **DCLM's own throughput.** 72,615 tok/s/vCPU is the reservoir mix, and per-bundle spread there
   was **3×** (stackv2-edu 916k tok/s vs finephrase 357k, per-container). DCLM is unmeasured. Task
   **#26** (measure in-region S3 + HF CDN bandwidth, 10 min, wave 0 stream 1) is the cheapest thing
   that would reduce this uncertainty.
2. **Token counts per subdirectory.** I confirmed the dirs are disjoint and non-empty. I did **not**
   verify the 10 `global-shard` dirs are equal-sized in tokens. If they are skewed, N equal
   `target_tokens` rows will not produce N equal children — and `_shard_slice` strides, it does not
   balance (`ingest_reservoir.py:764-779`).
3. **The 410B figure itself.** It is `IMPLEMENTATION-PLAN.md:420`, graded MEASURED there; I did not
   re-derive it, and note §284-285 grades the `-parquet` repo pool as "~3,764B DERIVED."
4. **Whether `hf_files` pagination completes on a 100-way listing.** One fetch of
   `global-shard_01_of_10?recursive=1` was truncated mid-response by my fetch tool at ~204 files. The
   `Link` loop at `corpus_build.py:825-829` looks correct by reading, but a full listing of a
   ~2,800-file subtree is untested.
5. **The 12 h estimate for C3b.** It is the graph author's judgement, not a measurement — the graph's
   own §9 says so verbatim. `TASKS.md:15` and `IMPLEMENTATION-PLAN.md:1483` both say **1–2 days** for
   the same item, so the 12 h is the optimistic end of a 12–24 h band.
6. **Nothing was run on AWS or locally beyond three trivial Python snippets** (the `targets`-collapse
   demo and two arithmetic scripts). No pytest, no Batch job, no S3 write.

---

## 6. Recommended next actions, in order

1. **Add a `source_label` uniqueness check to `load_registry`** (`corpus_build.py:130-152`). ~5 lines.
   Do this first regardless of route — silent token loss with a green build is exactly the failure
   class this repo's golden rule exists to catch, and trap 1 is live today.
2. **Reconcile which DCLM repo the 410B row names** (`dclm_100BT` flat vs `-parquet` nested). The
   free carve exists only in the nested one.
3. **Run #26.** 10 minutes, and it is the only cheap thing that shrinks the §5.1 uncertainty.
4. **Size the split at ~20 ways** via `local-shard_N_of_10`, as registry rows. Apply the same to
   FineWeb-Edu (≥13).
5. **Then reconsider C3b's 12 h of ordinal-range code.** If registry rows deliver ≥20 disjoint
   children, the ordinal machinery may be unnecessary for this corpus — but it remains the general
   fix for a source with no natural subdirectory carve.
6. **Treat parallelizing `dedup_and_decontaminate` as the follow-on**, not optional. It is the
   constraint once the bundles are split.

**Standing repo rules that apply to you:** nothing computational runs on the laptop; every AWS job
goes through the `edullm-platform-runs` skill and needs a human release; images build only from
`edullm/**` branches (a merge to `main` builds nothing, silently); worktree per agent. Items that
change the plan must land **before FREEZE**, and splitting DCLM changes every `plan_id` — `plan_document`
is a pure function whose sha256 IS the id (`corpus_build.py:182-199, 289-292`), so it collides with
**B6** (`SHARD_TOKENS`). One agent owns `corpus.py`'s plan surface.
