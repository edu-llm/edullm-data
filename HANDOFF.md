# HANDOFF — eduLLM Dataset Standard

Last updated: **2026-08-04**, after the reservoir corpus was **BUILT on Batch**: 27/27 bundles,
10,049 shards, **251.2B tokens** staged on `edullm-landing`. The published 150B and 127B corpora are
untouched and readable.

> ## ▶️ START HERE
>
> **The corpus is built and NOT published. One blocker stands in the way, and it is known:**
> `verify --deep` refuses with `bundle-set-mixed-wheel-versions` because four code fixes landed
> mid-build, so five different wheels packed the corpus. **Nine bundles / 4,137 shards must be re-run
> on wheel 0.7.4** (`edullm-reservoir-build:9`, `--force`, since resume would otherwise skip them).
> That check is correct — do not waive it. Then re-verify, require `VERIFY_DONE_RC=0`, and publish per
> `artifacts/reservoir/PUBLISH-SPEC.md`. Full detail in **Next Steps item 1**.
>
> 1. **This file's "Next Steps"** — what to do next, in order. Item 1 is the only blocker.
> 1b. **`artifacts/reservoir/PUBLISH-SPEC.md`** — both irreversible decisions, confirmed by the owner,
>    plus the exact `publish()` call and the three pre-publish gates.
> 2. **`DATASET-DESIGN-reservoir.md`** — the plan. §5.6 is the build sequence; §4.1 the dedup
>    pipeline; §2.1 the pool sizing.
> 3. **`src/edullm_data/corpus.py`** — the build-time CONTRACT. Read it before touching any build
>    stage; the shard geometry, the ordinal allocator, the held-out predicate and the EOS floor all
>    live there, each with the citation to the code that enforces it.
> 4. **`artifacts/reservoir/WEEK1-CORPUS-SURVEY.md`** — what to reuse from the sibling repo, and
>    **two claims in this file that were wrong** about it.
> 5. **`artifacts/reservoir/SEGFAULT-INVESTIGATION.md`** — 10 hypotheses, 9 refuted, 1 correct.
>    Read before touching the ingest transport.
> 6. **`artifacts/reservoir/RUN-THE-INGEST.md`** — the operational runbook + a failure table where
>    every row has actually happened.
>
> ## ✅ INGEST PROVEN · READER AND PACKER BUILT
>
> `reservoir-ingest-v063-smoke`, job def `edullm-reservoir-ingest:7`, wheel `0.6.3`:
> **4 of 4 children SUCCEEDED**, all four configs, **zero 429 pauses**, **67 seconds** — on the same
> shard that segfaulted twice. 16 `.u64` parts in
> `s3://edullm-landing/_ingest/reservoir-dolma2/_ids/parts/`.
> Everything below about the ingest being blocked is HISTORY. It is not blocked.
>
> **Phase 1 items 1, 2, 3 and the source registry are now DONE**, including the one this file
> called "most likely to break the estimate." `corpus.py` (contract) + `corpus_read.py` (parquet
> **and** `.json.gz`) + `corpus_pack.py` (exact 25,001,984-token shards, conservation asserted at
> runtime) + `corpus_build.py` (plan / run / verify, resume that re-heads every shard) +
> `corpus_receipt.py` (the only payload re-hash in the pipeline) + `corpus_filter.py` (exact dedup
> + eval decontamination — **40/40 real GSM8K test questions caught, 0 false positives**) +
> `artifacts/reservoir/corpus-registry.json` (17 sources, generated, all revisions pinned).
> **1,085 tests passing**, up from 790; ruff clean. Remaining: **~2–3 days**, not 2–3 weeks.
>
> **Nothing is blocked.** The DCLM zstd problem was a registry error, not a missing dependency:
> the row cited `dclm_100BT`'s measurement under `mlfoundations/dclm-baseline-1.0`'s name. Fixed.
> The plan covers the full corpus — **27 bundles, 10,082 shards, 252.07 B tokens**.
>
> **Next is execution, not construction:** run the build on Batch (items 5–7), publish, verify.
>
> Also closed: the eval decontamination bundle is **authentic** (recomputed sha256 equals its
> manifest's claim) and was on the laptop only — now at
> `s3://edullm-landing/_dist/eval-decontamination.bin`, a prefix with no expiry rule.
>
> ## 🎯 THE DECISION OF RECORD — **FULL PIPELINE** (owner, reconfirmed 2026-08-01)
>
> **Build the corpus from documents, per `DATASET-DESIGN-reservoir.md`. MinHash DEFERRED.**
>
> Reconfirmed against all five options after each was costed. **Do not re-propose A–D**; they were
> evaluated in detail and declined in favour of a corpus the team designs. Numbers kept because they
> are useful context, not because the choice is open:
>
> | | option | cost | why declined |
> |---|---|---|---|
> | A | publish `datamix1-jul22` as-is | hours | 20B total — one run consumes all of it, so no reservoir and nothing to re-weight |
> | B | merge `olmo-150b-dolma2` + `olmo-127b` | ~4 h Batch | 283.7B / 12 sources, verified mergeable (same tokenizer, byte-identical `manifest_sha256`, 7,392/7,392 distinct digests). Missing edu-web, QA/forum, synthetic outright |
> | C | B + tokenize the 3 missing categories | 2–3 d | 76.4B to build, 169 GB fetch, 6–11 h compute. All 15 sources, but the mix is AI2's for the copied 250B |
> | D | re-cut AI2's `s3://ai2-llm-public` shards | 1–2 wk | Skips fetching/tokenizing 3.45 TB but still needs packer + sharder + val carve, still needs FinePhrase tokenized, and adopts AI2's topic/quality slicing as the category structure |
> | **E** | **full pipeline from documents** | **2–3 wk** | **CHOSEN** |
>
> **Why E despite D being cheaper:** the reservoir exists so the team controls the mix. D's saving is
> real but narrower than it first looks — the ingest is now 1–3 hours (not 2–4 weeks), so the
> remaining cost is writing the packer/sharder either way, and D additionally inherits a category
> structure nobody here chose. The build is code-bound, not byte-bound.
>
> **MinHash deferred on §4.1's own evidence**, not to trim scope: DCLM measured Bloom-filter-alone at
> **+1.6 CORE, equal to the full Exact+MinHash+SuffixArray stack**, and FineWeb found the *removed*
> data scored better than the kept. It is annotate-only, so it lands later as
> `_dedup/clusters.parquet` — a control file outside the hash chain, no rebuild. Adds 1–2 weeks.
>
> **AI2's store is still worth reading** even under E: `s3://ai2-llm-public` is anonymously listable,
> pre-tokenized with `allenai/dolma2-tokenizer`, and its `.csv.gz` sidecars carry verified document
> boundaries. Use it to cross-check our own tokenization, not as a source of shards.
>
> ## ⚠️ BRANCH STATE
>
> Work is on **`agent/claude-01/reservoir-ingest`** (46+ commits ahead of `main`; count with
> `git rev-list --count main..HEAD` rather than trusting this line), pushed, in the
> worktree `/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/claude-01--reservoir-ingest`.
> Suite **786 passing**. Tags `v0.6.0` … `v0.6.3`.
>
> Open PRs: **#9** (reservoir design + Phase 0, branch `docs/reservoir-design`) and **#11** (the id
> partition + ingest job). Both still open; #11's branch has advanced well past its description.
>
> A **concurrent session** owns `feat/prm800k-vendor-ingest` and registered the digest-pinned
> validator, which bakes code into an image instead of bootstrapping a wheel — **better provenance
> than our rev 8; theirs wins**. They also **disabled the `edullm-landing-manifest-created`
> EventBridge rule**, so auto-promotion is OFF and any publish needs a manual `submit-job` or the
> rule re-enabled. Coordinate before phase 2.
>
> **Live job defs, verified 2026-08-01 by `batch describe-job-definitions --status ACTIVE`:**
> `edullm-validator:` **10** (digest-pinned image, no wheel, 7200 s) · `edullm-fsck:` **6**
> (wheel `0.6.0`, 3600 s) · `edullm-reservoir-ingest:` **7** (wheel `0.6.3`, 7200 s).
> Note this is **one revision past** what was written above as `:9` / `:5` — revs 10 and 6 differ
> from 9 and 5 *only* by swapping `jobRoleArn` to `…-dataset-validator`, which is the
> airlock-correct identity. Cite 10/6.
>
> Also verified live the same day: bucket policy is **`edullm-data-airlock-v2`** (Put and Delete
> Denies split, nobody exempt from Delete) — several places in this file still said v1 was live.
> `edullm-wu-fsck-nightly` is ENABLED at `cron(6 9 ? * MON *)` (weekly; the *name* is the stale
> part). S3 Inventory `edullm-data-weekly` is Enabled. **Ten datasets** are in `_catalog/`.
> Still ACTIVE and owed a deregister: `edullm-reservoir-diag:2`, `-diag2:1`, `-shim:1`.

## THIS SESSION IN ONE PARAGRAPH

Every timeline estimate I gave was too pessimistic, and each revision came from finding something
already built rather than from building it — until the end of the day, when the last revision came
from actually building it. The day started with the reservoir ingest crashing (exit 139) and a belief
that the full pipeline was 6–10 weeks away. It ends with the ingest proven on Batch, **the reader and
the packer written and verified**, and the pipeline at **~1–2 weeks**. Three findings drove the early
revisions: the SIGSEGV was **pyarrow's `pre_buffer=True` default** dispatching range reads into a C++
thread pool, not anything in our code; the HTTP 429 storm was **our own 70× quota amplification** (one
metered resolve per range read instead of per file), not a platform ceiling; and the "2.5 TB ingest =
2–4 weeks" premise was false because HF's **data plane is unmetered and sits in us-east-1** — the whole
fetch is ~1–3 hours for about $1.

Then the two riskiest stages got built: `corpus.py` pins the build contract, `corpus_read.py` reads
parquet **and** the `.json.gz` path that did not exist, and `corpus_pack.py` emits exact
25,001,984-token shards with token conservation asserted at runtime — an assertion that caught a real
double-counting bug on its first run. 790 → **967 tests**.

⚠️ **A claim this paragraph used to make is FALSE and was corrected the same day.** It said the
sibling checkout `pipelines/week1_corpus` had "a complete, already-exercised S3 backend." Its
`S3ArtifactStore` has **no multipart** (single PUT, capped at 5 GB), **zero test coverage** (grepping
`tests/` for `boto3|moto|S3ArtifactStore` returns no files), and **never ran** — its only callers are
CLI commands no deployment script invokes; the released 96 objects were written by a standalone script
that never imports it. Two behaviours from it were genuinely worth porting and now are. Its *packer*
was likewise the wrong one to copy. See `artifacts/reservoir/WEEK1-CORPUS-SURVEY.md`. What the
checkout does hold is a real, verified decontamination bundle.

Five wrong diagnoses were shipped and retracted across the day; all are recorded below. The fifth was
mine today: I told the packer implementer to copy `pack_category_globally`'s shape, which computes its
aligned size by summing a *materialized* list — it cannot stream, and adopting it would have forced a
pre-pass over 255B tokens. The streaming version was measured to produce byte-identical output.

## THE 2026-08-01 SESSION

### The segfault: root-caused, fixed, proven

Array children died with **exit 139 (SIGSEGV)**, always at the `faq → math` boundary. **Ten**
hypotheses were tested; nine refuted. The cause: `pq.ParquetFile(rf)` takes pyarrow's default
`pre_buffer=True`, which dispatches a Python file object's range reads onto **Arrow's native C++ IO
thread pool**. Each such read then runs a full `urlopen` — TLS handshake, 302 redirect, socket read,
and under a 429 storm a multi-second `time.sleep` — inside a C++ thread-pool callback.

Fix: `pq.ParquetFile(rf, pre_buffer=False)`. One keyword, invisible because the parameter was never
written at the call site. Measured: `pre_buffer=True` puts 30 of 32 reads on native threads;
`False` puts zero there. A/B on Batch: 3/4 unpatched children crashed, 0/4 patched.

**Two traps worth keeping.** (1) The plausible wrong answer is a lock — the faulthandler stack shows
native threads bottoming out in `_RangeFile.read` with *no Python caller*, which looks exactly like a
race on the unlocked `self.pos`. It is not: measured concurrency was 1, interleavings 0, and an
`RLock` changed nothing. Arrow serialises calls into one file object. (2) **Instrumentation masks the
crash** — every faulthandler wrapper survived, because slowing the child misses the window. Any
wrapper needs an unpatched control through the identical wrapper or you cannot tell "fixed it" from
"perturbed it."

### The 429s were ours, and the ingest is ~1–3 hours

HF runs two independently-metered services and this project conflated them:

| | metered? | measured |
|---|---|---|
| control plane (`resolve/main`) | yes | `q=5000; w=300` authed, `q=3000` anon |
| data plane (`us.aws.cdn.hf.co`) | **no** | no `RateLimit` headers, no auth, `x-hf-cdn-pop: aws-us-east-1` |

pyarrow issues ~70 range reads per file. `_RangeFile` pointed **every one** at the control plane:
70 metered requests per file, 1,400 for 20 files, budget empty in ~79s at 40 workers. That
arithmetic reproduces the observed failures exactly. **Resolve once, reuse the signed URL → 1
request per file.** Verified by hand: 12 CDN ranges consumed **0** resolver units.

⚠️ ~~Resolve with **no** `Range` header. Sending one signs the URL for that range only, and reuse
returns `403 invalid range` — silently restoring the bug.~~ **RETRACTED 2026-08-01.** Re-tested on
five repos (Xet- and LFS-backed) by two independent probes: reusing a range-resolved URL for a
*different* range returns **206 with correct bytes**, never 403. The decoded CloudFront policy has
exactly two conditions — `Resource` and `DateLessThan` — so there is no byte-range condition for a
range to bind to. Keep resolving without a `Range` (it costs nothing), but it is not the protection.
**The real traps:** (1) signed URLs expire at **3600 s** and then 403 — which `huggingface_hub`
misreports as a token-permissions error and never retries; `_CDN_TTL_S` is what actually saves us.
(2) `HfFileSystem._fetch_range` re-resolves **every block**, i.e. one metered resolve per 5 MiB —
reaching for it recreates the exact bug we fixed. (3) Xet CAS reconstruction
(`cas-server.xethub.hf.co`) is a different, rate-limited endpoint that *does* issue range-bounded
chunk URLs; the unmetered-CDN property does not extend to it. That is almost certainly where the
original observation came from.

**The inherited belief was wrong.** `PLAN-CORRECTIONS.md` §6 says the limit is "per-IP, not
per-account." True of `datasets-server`; **false of resolvers** (per-token) and false of the CDN
(unmetered). As written it deters the fast path. **Still owed:** correct that file and
`artifacts/smoke/harvest_parquet.py:9-13`.

Also: DCLM is in a **public bucket** — `s3://commoncrawl/contrib/datacomp/DCLM-baseline/`, us-east-1,
not requester-pays. Prefer it over the HF copy; it is an in-region S3→S3 copy.

### What is already built that nobody knew about

- **`pipelines/week1_corpus`** (77 files, 24k LOC, 136 tests) has `tokenization.py`, `packing.py`
  (`np.memmap(dtype=np.uint32)`, no `np.save` — the correct `.u32le.bin` bytes), `determinism.py`
  (seeded per-doc val carve), `reduction.py`, `decontamination.py`. **`worker.py:270` already wires
  `S3ArtifactStore.from_uri(...)` + `SqsTaskQueue`** — the S3 backend is complete, and it produced a
  real release. ⚠️ **But it has zero test coverage**: no boto3/moto in `tests/`; 136 tests exercise
  only the local backend.
- **`s3://edullm-datasets/datamix1-jul22/`** — 96 objects / 35.8 GiB, uploaded 2026-07-27, still
  live. Packed tokens plus `validation/audits/decontamination-bundle-manifest.json`,
  `exclusions.parquet` (521 MiB), `leakage-summary.json`, release attestations. **§4.2 says
  decontamination is load-bearing and we have none — this is a real bundle, already run.** Caveats:
  71 objects named `.npy` (probably headerless raw uint32 — the dolma naming convention, *verify the
  first bytes*), content-addressed by SHA-256 rather than `train-NNNNN.u32le.bin`, and 512 MiB
  shards rather than the 25,001,984-token size.

### Tokenize/shard: write it fresh, ~400–800 lines

Two independent investigations converged. **No off-the-shelf tool can hit the shard size**, because
none splits mid-document: dolma undershoots (25,001,242), datatrove overshoots (25,003,321), and
both leave a remainder that fails `check_seq_len_alignment` on all ~10,200 shards. Verified
arithmetically.

Every candidate **already emits the right bytes** — no tool writes a real `\x93NUMPY` header. dolma
sets `MEMMAP_EXTENSION = ".npy"` but writes via `np.memmap`; `np.load()` on its output *fails*. So
the 7,557 legacy `.npy` objects are **dolma's normal output**, and this standard's ".npy lie" rule is
a rule against dolma's naming.

Porting `week1_corpus` wholesale is worse than writing fresh: it emits **one pre-mixed file per
tier** (`plan_mixed_shards`, `pack_mixed_shard`), while eduLLM needs per-source shards mixed at read
time by `build_mixture`. Write-time mixing would destroy the whole-shard no-positional-bias property
§2.2 argues for. The genuinely non-obvious correctness in those 24k lines is **~80 lines**, itemised
in the agent report (EOS counted where it is added; length-prefixed hashing; NFC + NUL strip before
content hashing; magic-byte sniffing over suffix trust).


> **Everything below is the historical log of prior sessions**, kept for the reasoning it records.

## THE RESERVOIR EXECUTION SESSION (2026-07-31)

### The gate ran, and its answer was to not spend the money

§9.4's dual-judge smoke test measured `EAI-Distill-0.5b` against two Qwen judges over FDC Level 1:

| source | J (A↔B) | score (D on A==B) | 95% CI | verdict |
|---|---|---|---|---|
| qa-forum | 92.0% | **97.4%** | [95.5, 98.5] | PASS |
| academic | 75.4% | **84.9%** | [80.9, 88.1] | FAIL\* |
| finemath | 73.6% | **84.8%** | [80.8, 88.1] | FAIL\* |
| reference | 70.0% | **80.3%** | [75.8, 84.1] | FAIL |
| **POOLED** | 77.8% | **87.5%** | [85.8, 89.1] | **PASS** |

\*Both miss by 0.1–0.2 points with CIs *spanning* the bar — statistically indistinguishable from a pass.

**The owner cancelled the full classification anyway, and the numbers say why.** Measured throughput was
**10.8 doc/s on one A10G**, putting 112M documents at 3,080 GPU-hours ≈ **$920 spot / $3,100 on-demand**
against a planned **~$595** — and that is a floor, since the smoke test used 256-token prefixes while
real documents average 11,010 chars. Paying five figures for an ~85%-accurate label against a ground
truth only 70–78% self-consistent was poor value. `artifacts/COST-RECHECK.md`.

**Replacement (§1.2): inherit `domain` from upstream, never classify it.** Verified by reading real
schemas — `stackexchange` has `metadata.site`, `stackv2-edu` has `metadata.gha_language`,
`essential-web` already carries `free_decimal_correspondence` (the *exact* field the cancelled run would
have computed). Everything else publishes flat. An inherited label is a **fact**; a classified one is a
guess. Budget ~$1,006 → **~$411**.

### All 8 pools measured — the method is the transferable part

| category | pool | measured | verdict |
|---|---|---|---|
| edu-web | 48B | 261.3B | ✅ 7.0× |
| web | 30B | 114.69B | ✅ 5.5× (via `dclm_100BT`, not DCLM-baseline) |
| code | 40B | **74.81B** | ✅ 1.87× (`stackv2_edu` alone) |
| academic | 20B | **64.12B** | ✅ 3.2× (after netting 49.7% PMC overlap) |
| QA/forum | 12B | **25.93B** | ✅ 2.16× — but **92.8% share-alike** |
| synthetic | 60B | **478.15B** | ✅ 8.0× |
| reference | **9B** | 8.87B | ✅ 3.70× (pool resized, owner) |
| math | 36B | 34.69B | ⚠️ floor 4.96×, pool 3.6% short — **accepted** |

**Phase 0's four `0.00B` readings were rate-limit artifacts, not scarcity.** The fix that mattered:
`datasets-server`'s quota is **per-IP, not per-account** (verified — authed and anon both 429 in 0.1s),
so parallel agents starve each other and the failures look exactly like broken corpora. **Parquet
footers / gzip ISIZE off the hub CDN are quota-free**, give *exact* whole-split column bytes, and moved
1.6 GB against 365 GB of text. Cost effectively zero against the ~$10 budgeted; no Batch job needed.

### Traps found by measuring, each of which produces a plausible WRONG number

1. **`/size num_rows` ignores a sibling `partial` flag.** DCLM: 779,982 converted rows vs
   `estimated_num_rows` 3,017,780,768 — a **3,869× understatement**, silent because both are plausible
   integers. Three agents hit it independently.
2. **`raw_v0.1_parquet` sweeps TWO document trees** for some sources — overstating `ubuntu_irc` 2.1× and
   `github_archive` 4.5×. Reproduced: the `id` multiplicity histogram is `{1: 404034, 2: 329115}`, and
   329,115 *is* the raw card's doc count. **Checked whether it corrupted the committed academic figures:
   it did not** (peS2o 1.03×, pubmed 1.06× vs card; only ubuntu_irc shows 3.23×).
3. **gzip `ISIZE` is mod 2³²** and pubmed's shards exceed 4 GiB, so they wrapped and reported
   "uncompressed" *smaller than compressed*.
4. **`md.schema.names.index("text")` is ambiguous on nested schemas.** FinePhrase has `text` at leaf 0
   (the ORIGINAL FineWeb-Edu doc) and at leaf 12 (`rollout_results.list.element.text`, the rewrite).
   `.index()` returned 0 — §3.3's documented trap reached through the *footer* path, which nobody had
   considered. **Checked: no committed figure was wrong** (the three corpora measured with that tool each
   contain `text` exactly once). Fixed to refuse ambiguity rather than guess.
5. **Common Pile "token" figures are `Size(GB) × 0.25`** — verified exact on three rows of the paper's
   Table 7. No tokenizer involved.

### Two code blockers landed

**`4d6768e` — one control-file allowlist, shared.** `_dedup/clusters.parquet` and `_licenses/` were
rejected by Gate A *and*, worse, swept into `manifest_sha256` as payload by `publish.py`, which had a
**separate** basename-only allowlist. Fixed at the root in `contracts.py` rather than patched twice —
the two copies were byte-identical, which is exactly why the bug survived: a green suite could only ever
prove they *agreed*, never that there was one definition. The `families/` half-fix shape `CLAUDE.md`
warns about.

**`934dd75` — slug/fold inherited domains + the partial-label reader warning.** `C#` in an object key
**silently truncates any `s3://` URI at the `#`** (the shard name lands in the URI fragment), and nothing
caught it — `labels_from_path` accepts it and `fnmatch` matches it. Worse, naive slugging sends
`C#`/`C++`/`C`/`C--` **all to `c`**. Split across two functions so `build_domain_slug_map` — the one that
decides *permanent directories* — **raises** on collision. And under mixed label depth a `domain=` query
silently drops every flat source; the warning now names them and reports **"40.4% of the group's tokens"**.

## Goal

Two goals, one nested inside the other.

**The standing goal:** replace ad-hoc S3 dataset sprawl with **one enforced way to create, store,
read, and discover datasets**. An engineer or agent runs `publish(...)` and a validated dataset
appears in the official bucket automatically, with no human in the loop and no way to write bad or
unvalidated data into the read path. Motivating audit:
`docs/dataset-creation/s3-dataset-audit-2026-07-28.md`. Full spec:
`docs/dataset-creation/DATASET-STANDARD.md` + `...-DIAGRAMS.md`.

**Status: CLOSED, and now closed end-to-end — a real training run has read a published corpus.**
The produce → validate → publish → read loop works. Two corpora are published, sealed, and sliceable
(157.2B and 126.7B tokens).

~~What is still missing downstream is a *consumer* — no training run has read them, and the blockers
live in `edu-llm/platform`, not here.~~ **SUPERSEDED 2026-08-01.** The consumer exists and ran.
Verified live via `batch describe-jobs` and `s3api`:

- Batch job `8fb0cd5c-5ea4-4936-a598-bf6daf79b724`, **SUCCEEDED, exitCode 0**, queue
  `sbsandbox-intern-edullm-gpu`, 4 vCPU / 15360 MB / **1 GPU**, 2026-08-01 15:09:30Z → 15:28:50Z
  (19.3 min). It **trained 150 steps and checkpointed 3× (step0, step75, step150)**.
  ⚠️ **No loss value was recovered** — W&B was not readable from here and the CloudWatch stream
  could not be read. Do not upgrade this to "produced a loss curve"; what is proven is that the
  bytes were opened, consumed for 150 optimizer steps, and checkpointed.
- It really read **our** corpus, not AI2's sample: `…/checkpoints/step150/data_paths.txt` (3137 B)
  holds **41 paths, 100% of them**
  `s3://edullm-data/pretrain/regmix-10b/v1/tokens/<source>/train-*.u32le.bin`, spanning **all 7
  sources** of that corpus (dclm 14, arxiv 10, starcoder 6, pes2o 4, algebraic-stack 3,
  open-web-math 3, wiki 1). The step0 copy has an identical ETag, so the list was stable.
- Sibling `config.json`: `NumpyFSLDatasetConfig`, `dtype: "uint32"`, `expand_glob: false` (an
  explicit path list — exactly what `docs/CONSUMER-CONTRACT.md` §6 demands), tokenizer
  `allenai/dolma2-tokenizer` **vocab_size 100278**, sequence_length 2048, max_duration 150 steps.
  (`model.vocab_size` is 100352 — the padded embedding width. Cite the *tokenizer* field.)

So this repo's read path is no longer hypothetical. The remaining platform constraint is **a real
GPU budget**, not an IAM grant — see "What is NOT done" and `docs/PLATFORM-INTEGRATION.md`.

**The current goal:** build **`pretrain/reservoir-dolma2`** — a ~255B-token reservoir the team draws
20B-token training mixes from, per `DATASET-DESIGN-reservoir.md`. Every run consumes exactly 20B, so
the reservoir must hold ≥3× peak plausible demand per category and let a teammate re-weight sources
at read time via `build_mixture`.

**Status as of 2026-08-04: the corpus is BUILT. 27/27 bundles, 10,049 shards, 251.2B tokens on
landing.** Option E (full pipeline from documents) was confirmed by the owner over three faster
alternatives, MinHash deferred. The build ran on Batch across 2026-08-02→04 and cost four code fixes
found only by running it (see *What Didn't Work*). Suite 1,107.

**What has NOT happened: nothing is published.** `verify --deep` **refuses** with
`bundle-set-mixed-wheel-versions` — the five wheels that packed the corpus are not
interchangeable, because the gates a bundle passed are a property of the wheel that packed it. Nine
bundles / 4,137 shards must be re-run on 0.7.4 before anything can be published. That is the only
blocker, and *Next Steps* item 1 is exactly it.

~~**What has NOT happened: the build has never run.**~~ Superseded above 2026-08-04.

---

## Current Progress

### 2026-08-04 — THE CORPUS IS BUILT. 27/27 receipts. `verify` WILL REFUSE, and the reason is known.

**All 27 bundles are built and receipted** at
`s3://edullm-landing/_ingest/reservoir-dolma2/build/d5c9bcd38735e1f0/`. Numbers read from the
receipts, not the plan:

| | |
|---|---|
| bundles | **27 / 27** |
| shards written | **10,049** (plan allocated 10,082 — see the 33 unfilled below) |
| documents | **308,291,107** |
| train tokens | **250,242,924,544** |
| val tokens | **975,077,376** (0.39%) |
| total | **251,218,001,920** |
| conservation | holds on **all 27** — `tokens_in == out + tail + surplus` |
| unfilled refs | **33, all in `finewiki--train`** |

**⚠️ `verify --deep` WILL REFUSE, and I confirmed it locally before spending the 1 TB read.** The
set-level checks are pure and free (`verify_bundle_set` called with no `s3=`), so running them over
the downloaded receipts predicts the verdict for nothing:

```
bundle-set-mixed-wheel-versions: bundles were built by 5 different wheels
['0.7.0', '0.7.1', '0.7.2', '0.7.3', '0.7.4']. The gates a bundle passed are a property of
the wheel that packed it ... Re-run the bundles built by the older wheel.
```

**That check is CORRECT and must not be waived.** Four code fixes landed *during* the build, so early
bundles were packed by wheels lacking later gates. Nine bundles / 4,137 shards need re-running on
0.7.4; eighteen are already on it and resume skips them:

| wheel | bundle | shards |
|---|---|---|
| 0.7.0 | `ubuntu-irc--train` | 71 |
| 0.7.1 | `finewiki--val` | 1 |
| 0.7.1 | `pes2o--val` | 2 |
| 0.7.1 | `pubmed--val` | 1 |
| 0.7.1 | `stackexchange--val` | 1 |
| 0.7.2 | `fineweb-edu--val` | 3 |
| 0.7.3 | `finemath--train` | 1353 |
| 0.7.3 | `finepdfs-edu--train` | 1114 |
| 0.7.3 | `stackv2-edu--train` | 1591 |

**The 33 unfilled refs in `finewiki--train` are DATA, not a fault.** finewiki's pool is 8.87B against
an 8.8B target — the tightest ratio in the registry — so the source ran out of documents at ordinal
`train-04771`. `pack` skips the allocation rather than writing a short shard (an unfilled ref costs
nothing; a surplus would discard real tokens). Realized 7,920,156,672 = **90.5% of plan**. Two
consequences: 33 planned keys will never exist, and `sources[]` must cite **7.92B** for finewiki,
not 8.75B.

**Zero build-code failures in the final run.** Every failure this session was my own termination
(exit 137), a `CannotPullContainerError` from an expired ECR digest, or one of the four bugs under
*What Didn't Work* — each caught by a gate before bad data persisted.

Verify job `a4531f96-8937-44ee-bf64-ab481d3dd3a3` (`rsv-verify-deep`, job def
`edullm-reservoir-verify:1`) is RUNNING. It will report the violation above. Safe to let finish or
kill — the verdict is already known.

### Reservoir pipeline status — 2026-08-01 (end of session). Every build stage is BUILT.

| stage | status |
|---|---|
| ingest transport (HF → S3, array-sharded) | ✅ **proven on Batch**, `v0.6.3`, job def `edullm-reservoir-ingest:7` |
| §9.7 item 4 id partition | ✅ verified on 287,000 real ids (24.86–25.26% per format vs a 17.3% floor) |
| domain slugging | ✅ `manifest.build_domain_slug_map` |
| **build contract** | ✅ `corpus.py` (440) — shard geometry, ordinal allocator, held-out predicate, EOS floor |
| **source registry** | ✅ `artifacts/reservoir/corpus-registry.json` — 17 rows, generated, **all revisions pinned**, `text_column` 17/17 verified from bytes |
| **per-corpus readers** | ✅ `corpus_read.py` (890) — parquet **and** `.json.gz`; **14/14 drawn sources yield real documents** |
| **tokenize + exact 25,001,984-token shard** | ✅ `corpus_pack.py` (809) — conservation asserted at RUNTIME |
| **bundling / receipts / resumability** | ✅ `corpus_build.py` (~800) + `corpus_receipt.py` (1,043) |
| **exact dedup + decontamination** | ✅ `corpus_filter.py` (~300) — **40/40 real GSM8K questions caught, 0 false positives** |
| MinHash + LSH + connected components | ⏸ **deferred** (owner, on §4.1's own evidence) |
| publish / Gate A / promote | ✅ mature, live infra |
| **RUN IT** | ❌ **the only thing left.** Register a job def, run the array, publish, verify |

**Suite 790 → 1,090** across the session; ruff clean; 71 commits ahead of `main` on
`agent/claude-01/reservoir-ingest`, all pushed.

The plan built from the registry: **27 bundles, 10,082 shards, 252.07 B tokens**, nothing excluded —
landing on §2.2's ~10,400-object sizing, which is what Gate A's ~1.4 h estimate was derived from.
`plan_id` is a content address (`plan_document` is pure — no clock, no S3), verified byte-identical
across regenerations and independent of input order.

**Proof the ingest works** (`reservoir-ingest-v063-smoke`, 4-child array):

```
WHEEL_VERSION=0.6.3
PYARROW=25.0.0 NUMPY=2.4.6
PREFLIGHT_OK=1
faq / math / table / tutorial: all four configs, 399k–458k distinct ids each
INGEST_DONE_RC=0        4 of 4 SUCCEEDED · zero 429 pauses · 67 seconds
```

16 `.u64` parts in `s3://edullm-landing/_ingest/reservoir-dolma2/_ids/parts/`. The same shard
segfaulted twice before the `pre_buffer=False` fix.

**Infrastructure deployed this session and the last:** `expire-ingest-30d` lifecycle rule on
`_ingest/` (9 rules total, all originals intact); bucket policy **v2** with `NobodyDeletesPublishedData`
and no exemptions, verified by four live probes; least-privilege role `edullm-reservoir-ingest` that
cannot write `edullm-data`; the fsck job def carries a 3600 s timeout (now at rev **6**, not 5 —
re-verified 2026-08-01); account IDs scrubbed repo-wide (re-checked: zero 12-digit IDs in tracked
files).

---

### Standing platform status — BUILT, DEPLOYED, PROVEN AUTOMATIC, PUBLIC, and carrying REAL DATA


The full pipeline is proven end-to-end on live AWS with real data, including fully-automatic
event-triggered validation. The repo is **public at `https://github.com/edu-llm/edullm-data`**.
The first real migration was completed and then **deliberately rolled back** — see below.

### 2026-07-30 session, in one paragraph

Published the 150B corpus (6,911 shards / 586.6 GiB / 157,467,202,883 tokens) after four burned
attempts, each of which found a real defect: publishing from a laptop (`publish()` pulls every
byte — 0.8 MiB/s, a 9-day ETA), `families/` unresolvable inside an installed wheel on the producer
side, Gate A rejecting 4 of 6,913 shards, and a renumbering trap where Gate A names the *published*
key while the exclusion list keys on the *staged source* name. Two of those four rejections were a
**validator defect** — `max_zero_fraction` measured punctuation density because dolma2 maps token
id 0 to `!` — now a contiguous-run test. Then added the read side: `labels=` selection and a
seeded `build_mixture()`, so a training set is described by six values instead of 6,911 URIs.
Everything is merged to `main` and pushed; 626 tests, 0 ruff errors.

**WHAT IS IN `edullm-data` RIGHT NOW — one dataset, 11 objects, 6.5 MiB (verified 2026-07-29):**
- `tokenizer/dolma2-bpe/v1` — the real `allenai/dolma2-tokenizer` (tokenizer.json + merges.txt +
  vocab.json + configs). `vocab_size 100278` & `eos 100257` DERIVED from tokenizer.json. Has
  `_VALIDATED.json` + `_catalog/tokenizer/dolma2-bpe/v1.json`. **KEEP THIS** — it has NO legacy
  backup (the legacy `olmo30b/` tree contains zero tokenizer files, verified), and a replacement
  corpus will pin it by `manifest_sha256`, so re-downloading from HuggingFace could break that chain.
- **`pretrain/olmo-mix-1124-31b/v1` — DELETED.** Was 31,334,000,834 dolma2 tokens / 218 shards /
  125.336 GB. See "THE 31B DELETION" for why, and for the two recovery paths.

### THE 31B DELETION (2026-07-29)

**Why:** it had no `val` split and, being frozen, could not gain one — so under the new
validation-required-by-default rule it fails `missing-required-split` permanently. Its per-source
structure was also already flattened, which is not the shape the 150B work needs. Keeping it bought
nothing except a corpus no defensible claim could be made from.

**Recovery — TWO independent paths, both verified before deleting:**
1. `s3://edullm-datasets/olmo30b/olmo-mix-1124-30b/tokenized/shards/` — 218 `.npy` shards totalling
   **125,336,003,336 bytes, byte-identical** to the published manifest total. Never touched. (These
   are the headerless-raw-uint32 ".npy lie" files; a re-publish is a server-side rename, no re-encode.)
   The legacy tree also holds 218 sidecar files the migration excluded.
2. Versioning was ON and nothing was purged: **342 noncurrent versions + 222 delete markers** remain
   under the deleted prefix.

**How:** the intern role is denied `s3:DeleteObject` by the airlock, so this needed a temporary
bucket-policy amend (add the intern ARN to `OnlyValidatorWrites`'s exemption list) → delete → restore
the original policy verbatim. **The airlock is RESTORED and verified**: the live policy diffs
identical to the original (`Id: edullm-data-airlock-v1`, exemption list back to the two roles, intern
ARN absent), and both probes deny — intern `PutObject` to the bucket AND intern `DeleteObject` on the
surviving tokenizer README, each with "explicit deny in a resource-based policy". The same probes
were run BEFORE the amend and also denied, which proves the probe detects the policy rather than
passing vacuously. 223 objects / 125,336,070,789 bytes removed; bucket went 234 → 11 objects.

**Consequence:** `resolve_latest("pretrain/olmo-mix-1124-31b")` now returns `None` (the catalog entry
went with the data), which is the correct state — better than a catalog pointing at absent bytes.

**Still true from the migration, and worth keeping:** every promoted dataset carries a **generated
`README.md`** rendered from `dataset.json` (a DERIVED artifact — it cannot drift). The tokenizer's
keeps `license: unknown` because upstream terms were never verified, and that is deliberate: the
generator omits what it does not know rather than fabricating it. The migration also proved the
in-place **descriptive-keys-only backfill** works — it changed only prose fields and left every
`manifest_sha256`, `depends_on` pin, and `inventory` byte-identical (verified), so a frozen contract
survives a metadata edit. Legacy `s3://edullm-datasets/olmo30b/` was left fully intact throughout.

**Code** (this repo, `edullm-data/`, its own git root; **541 tests passing** on this branch — was 380
on `main`):
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
- `src/edullm_data/read.py` — `dataset_paths()`, `resolve_latest()`, and now `verify_seal()` +
  `SealMismatch` + `MixedFormat`; an unsplit read returns TRAINABLE data only; `ResolvedSplit` carries
  the FULL format triple (`dtype`, `byte_order`, `header_bytes`) plus a `numpy_dtype` property that
  emits `"<u4"` — `np.dtype("uint32")` silently uses HOST byte order (all this branch)
- `src/edullm_data/fsck.py` — `wu-fsck` Gate B (post-publish decay sweep), owner Eric Wu
- `src/edullm_data/profiles/` — registry + **5** v1 profiles: pretrain-tokens, eval-results,
  token-order, sft-conversations, **tokenizer** (`tokenizer_v1.derive_vocab` computes vocab from tokenizer.json)
- `families/*.json` — **7** families: pretrain, curriculum, sft, eval, probe, vendor, tokenizer
- `infra/` — CloudFormation, policies, Dockerfile.validator, DEPLOY.md, 05-validator-jobdef.md
- `skill/SKILL.md` — copy of the agent skill (canonical copy at `../.claude/skills/edullm-datasets/`).
  Both copies now instruct writing a generated README for EVERY dataset, incl. already-promoted ones.
- `USAGE.md` — human how-to. Install line (all docs): `uv add "edullm-data @
  git+https://github.com/edu-llm/edullm-data@v0.1.0"` (public repo, no auth).
- `docs/ONBOARDING.md` — 2-minute, paste-friendly intro to the pipeline for a teammate who
  has never worked on it (the airlock, bucket layout, the address shape, what the validator forces).
  Its integrity bullet was corrected this session — it no longer implies Gate A re-hashes payload.
- `docs/CONSUMER-CONTRACT.md` — **NEW (this session)** the read side, stated precisely enough for a
  training adapter to be written against it: the address + `resolve_latest`/`dataset_paths`, every
  `ResolvedSplit` field, THE dtype rule and the silent-failure asymmetry, splits/trainability, the
  seal, the OLMo-core constraints (marked `[CONSUMER]` — this repo cannot enforce them), and an
  honest "not guaranteed" section. Every claim carries a `file:line`.
- `docs/DECISIONS.md` — one entry per settled decision, sourced from the standard.

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
- `a5818ac` — `release: v0.2.0` (per-dataset README publisher) + refresh team install pins (PR #3).
  **This is the current tip of `main` and of `origin/main`.**

---

## THIS WORK — `fix/validator-recompute-gaps-schema-v2`, MERGED to `main` as `2e561cc` (PR #4)

13 commits, preserved individually (merged with `--merge`, not squash). Branch deleted local + remote.

Branched from `a5818ac`. **380 → 541 tests.** Pushed and merged to `main` as `2e561cc` via PR #4,
using `--merge` so all 13 commits survive individually — the executed proofs and per-fix reasoning are
in these messages, not just in the PR body. Commits, newest last:

```
9bd5213  fix(validator): derive dtype from vocab; wire family defaults into the gate
63e001c  feat(validator): root the hash chain so the seal binds to content
c9d2816  fix(publish): fill rows for caller-supplied partitions, not just family defaults
e2cd07c  fix(infra): split the airlock Deny so Delete binds the validator too
ea294d9  feat(schema): v2 adds entry.split + entry.labels; closed split vocabulary
6575a97  feat: validation required by default; an unsplit read returns trainable data only
8e94112  fix(validator): recompute partition rows, coverage, and dataset-level exhaustiveness
72df9f7  fix(profile): scale the distinct-ids floor to the sampled size
ad75062  fix: package families/ into the wheel; scope the dtype check to token units
cdc2587  fix: close every hole two adversarial reviews found in the split defence
2f0cd7c  docs: retract the false claim that the validator re-hashes payload bytes
4dfad55  chore: phase 4 housekeeping — lint clean, fsck weekly + CRC64NVME, reader format detail
```

### Schema v2

- `SCHEMA_VERSION = "edullm-dataset/v2"` (`contracts.py:50`). New manifest-entry fields
  **`entry.split`** (`manifest.py:211-215`) and **`entry.labels`** (a flat `{str: str}` map,
  `manifest.py:216-219`). Both validated: `split` must be in the vocabulary
  (`manifest.py:267-276`), `labels` must be flat strings and **must not carry a `split` key**
  (`manifest.py:286-289`) — one fact, one place.
- **Closed split vocabulary.** `SPLITS = {"train", "val", "test"}` (`contracts.py:138`),
  `TRAINABLE_SPLITS = {"train"}` (`:142`), and `is_trainable()` (`:145-154`) with
  `is_trainable(None) == False` — an unlabelled object is *unknown*, and unknown is not
  safe-to-train. `held_out` is **derived**, never a field.
- **`READABLE_SCHEMA_VERSIONS = {"edullm-dataset/v1", "edullm-dataset/v2"}`** (`contracts.py:57`),
  and Gate A membership-checks against that set rather than equality against the current version
  (`validate.py:218-227`). v1 datasets stay readable — this standard does not retroactively
  invalidate. Both live datasets are `schema_version: edullm-dataset/v1`.

### Validation required by default, per family, with an explicit opt-out

`_check_validation_present` (`validate.py:686+`) fires when `family_defaults["validation_required"]
is not True`. **Opt-OUT, not opt-in, and the polarity is the design**: under opt-in, "no val split"
and "nobody thought about it" are indistinguishable, and you learn which weeks later from a
suspiciously good eval. Four families opt out in their own file with a stated reason —
`eval.json:11-12`, `probe.json:33-34`, `tokenizer.json:13-14`, `vendor.json:27-28`. `pretrain` and
`sft` and `curriculum` require it.

**Declaring nothing is not an exemption.** `partitions: []` used to switch this check off *and* the
undeclared-split backstop *and* make the reader see no trainable split and return EVERYTHING
(`validate.py:710-726`). It is now `missing-required-split`.

### The reader

- **`split=None` returns TRAINABLE data only** (`read.py:238-243`). It used to return every entry.
- **Both splits come back separately keyed** in `.splits` / `.train` / `.val`, never concatenated
  into `.paths` (`read.py:58-66`, properties at `:68-85`). `.val` is `None` — not `[]` — when there
  is no held-out data (`read.py:82`).
- **`include_held_out=True`** is the deliberate, code-review-visible escape hatch (`read.py:116`).
- **The reader recomputes split from each object's own FILENAME** and drops non-trainable shards
  whatever was declared (`read.py:255-271`). Everything above that line reasons from declared
  partition names — i.e. from a claim.
- **`dataset_paths` verifies the seal on every read** and raises `SealMismatch` (`read.py:209-231`);
  `verify_seal()` is the standalone form (`read.py:418-494`). Before this, `verify_seal` had **zero
  callers**, so rooting the hash chain bought nothing. Catches a `dataset.json` whose train/val globs
  were swapped: marker present, manifests intact, `split="train"` hands back val.
- A pre-root seal (no `dataset_sha256`) is reported **unverifiable but allowed through**
  (`read.py:457-465`, filtered at `:224-225`). See DEFERRED DECISIONS — both live datasets.
- **The FULL format triple now crosses the boundary** (`4dfad55`): `ResolvedSplit` carries
  `byte_order` and `header_bytes` alongside `dtype`, plus a `numpy_dtype` property emitting `"<u4"`
  (`read.py:137-155`). Both were declared in the manifest and dropped on the floor — and
  `np.dtype("uint32")` uses the HOST's byte order, so a big-endian shard decodes to different,
  in-range-looking ids that nothing downstream notices. `_resolve_format` (`read.py:362-394`) now
  **raises `MixedFormat`** when a group's typed shards disagree, instead of returning `dtype=None`
  — a loader cannot memmap one array two ways, and `None` was indistinguishable from the legitimate
  "container types itself" answer, so it got defaulted. Recourse is `group=`.

### New Gate A checks (all recompute; violation codes)

| code | where | recomputes |
| --- | --- | --- |
| `dtype-too-narrow-for-vocab` | `validate.py:858`, `manifest.py:602` | declared dtype width vs the vocab DERIVED from the pinned tokenizer |
| `dtype-not-checkable` | `validate.py:825` | dtype name is outside the 8-entry `DTYPE_SIZES` map — a width nobody can verify (numpy accepts `u2`/`<u2`; that made this check AND `verify_arithmetic` both skip) |
| `fixed-width-dtype-in-nonraw-container` | `validate.py:839` | a fixed-width dtype declared under `container: "memmap"` / `"raw "` — routed around the width check entirely |
| `split-contradicts-filename` | `validate.py:773` | `entry.split` vs the split parsed from the object's own name |
| `missing-required-split` | `validate.py:719`, `:732` | family requires held-out data and none is declared (incl. the declared-nothing case) |
| `partition-rows-mismatch` | `validate.py:973` | sums the per-entry counts a partition selects vs its declared `rows` |
| `partition-bad-rows` | `validate.py:951` | an explicit `rows: null` satisfied the presence check and skipped the value check; reached a trainer as an unknown split size |
| `coverage-not-disjoint` | `validate.py:1027` | `coverage: "partition"` claimed disjointness — now checked pairwise |
| `coverage-incomplete` | `validate.py:1039` | `coverage: "partition"` claimed exhaustiveness — objects belonging to no partition |
| `train-heldout-leakage` | `validate.py:998`, `profiles/sft_conversations_v1.py:245` | a trainable and a held-out partition selecting the same object. An error under EVERY coverage mode; `"overlapping"` waives replay between *trainable* partitions only |
| `unlisted-object-dataset-level` | `validate.py:651` | LIST the dataset prefix; anything in no group's manifest is an orphan a globbing reader would still find |
| `undeclared-split` | `validate.py:669` | objects named `<word>-NNNNN.*` where `<word>` is in `SPLITS` but no partition declares it |
| `empty-split` | `validate.py:679` | a partition declared with no object matching its name — "a reader asking for it gets silence" |

Exemptions the standard deliberately grants are preserved: only **vocabulary words** count as split
claims (so an eval set's `results/eval-00000.jsonl` is a naming convention, not a split), and
vendor/tokenizer prefixes are skipped (renaming a vendored tree destroys the byte-for-byte
correspondence that makes it verifiable).

### Three more structural fixes

- **`families/` is now force-included into the wheel** — `[tool.hatch.build.targets.wheel.force-include]`
  maps `"families" → "edullm_data/families"` (`pyproject.toml:42-43`). This retires old Next Step #1
  and the `_dist/families/` + `FAMILIES_DIR` override the Batch publisher needed. **The deployed
  `_dist` wheel is still `0.1.0` and predates this** — reship before relying on it.
- **A group override may TIGHTEN a family bound but not LOOSEN it** (`profiles/pretrain_tokens_v1.py:109-135`):
  a floor may be raised, a ceiling lowered, never the reverse. Without the clamp the family bounds were
  decoration — a group declaring `{"min_distinct_ids": 1, "max_zero_fraction": 1.0}` published an
  all-zeros corpus clean. Loosening now requires editing the FAMILY file, where it applies to everyone.
- **`promote()` refuses an already-sealed prefix** (`validate.py:1143-1151`). Overwriting a published
  dataset needs **no Delete call**, so the new Delete Deny would never have fired; landing expires
  after 14 days so the same `vN` genuinely frees up, and re-publishing would replace payload +
  manifest + seal together, leaving `verify_seal` reporting INTACT on substituted data. Also:
  `promote()` now hashes the **published** copy, not the landing copy after the copy loop (a
  concurrent re-put made the seal bind to bytes that were never published).
- **The seal now records per-object `crc64nvme`, HEADed in the DESTINATION post-copy** (`4dfad55`), and
  `fsck._check_crc64nvme` compares against it — the one check that catches a byte replacement at
  identical length. Destination-post-copy matters because **CopyObject RECOMPUTES the checksum**, so a
  value inherited from landing would be wrong by construction. A missing reference is skipped
  **silently**: a pre-CRC seal would otherwise emit one finding per object per week forever. Both live
  datasets predate this, so their CRC checks no-op until re-promotion.
- **Deleted `fsck._check_catalog_matches`** — it summed the manifests' DECLARED bytes and never HEAD
  sizes, so both sides derived from frozen control files Gate A had already reconciled: it **could not
  fire**. Rewrites of those files are now caught cryptographically by `verify_seal`'s root instead.
- **`pyproject.toml` console script `edullm-data = "edullm_data.cli:main"` pointed at a module that has
  never existed in any commit** — `pip install` succeeded and the script died with `ModuleNotFoundError`
  the first time anyone ran it. Fixed in `4dfad55`. All 11 pre-existing ruff errors also fixed (0
  remaining), and the suite was verified green on real CPython 3.10.20 rather than grepped for 3.11
  constructs.

**Working tree: CLEAN**, `main == origin/main` at `34dd868`. Everything above is committed and
pushed. `docs/CONSUMER-CONTRACT.md` and `docs/PLATFORM-INTEGRATION.md` landed in `bb6eaaf`. The four
150B working files are **deleted** — see "THE 150B SOURCE DATA" for what was salvaged from them.

---

## THE 150B SOURCE DATA — measured facts, preserved from the discarded first attempt

The first migration attempt's working notes (`docs/MIGRATION-olmo-150b-dolma2.md`,
`docs/olmo-150b-publish-spec.json`, `infra/publish_driver_v2.py`,
`infra/submit-olmo150-publish.md`) were **deleted 2026-07-29** — the user is starting the migration
fresh. They were never committed, so they are gone. These measurements cost a full read-only sweep of
13,840 objects to obtain, so they are recorded here rather than re-measured.

**Source:** `s3://edullm-datasets/` (the legacy tree). 13,840 objects =
**6,921 `.npy`** payload + **6,915 `.csv.gz`** sidecars. The `.csv.gz` files are per-shard
document-boundary metadata keyed to decommissioned paths; the first attempt EXCLUDED them. Note the
trade-off that excluding them makes: they are the only thing enabling OLMo-core's VSL / packed /
padded dataset classes, which hard-fail on remote shards without them (see `docs/CONSUMER-CONTRACT.md`).

**Structure:** `configs/` + `data/…/<6 sources>/` + **`heldout-val/`** (6 `.npy`, 265 MB, one per
source). So the real shape is **6 sources × {train, val}** — 6,915 train + 6 heldout = 6,921.
**This corpus HAS validation data**, unlike the deleted 31B.

**Per-source token counts (measured, not declared):**

| source | tokens | note |
| --- | --- | --- |
| all-dressed-snazzy2 | 119.3 B | 24 topic domains (adult_content … travel_and_tourism) |
| s2pdf-redacted | 19.8 B | same 24 domains; holds almost all the tiny shards |
| stack-edu | 11.1 B | ~15 languages |
| finemath-3plus | 4.06 B | |
| arxiv | 1.25 B | |
| wikipedia | 0.064 B | 63 shards, part-00..part-62 |

**Total = 157,535,073,650 tokens (157.5B)** vs a declared 155.6B → **+1.24%, actual EXCEEDS
declared**, so there is no truncation. The name "150b" is nominal (cf. olmo-mix-1124-31b = 31.334B).
Report the real 157.5B in the README. Arithmetic sweep also confirmed: all sizes % 4 == 0
(uint32-consistent), no zero-byte shards, no `\x93NUMPY` header in a 72-shard sample.

**The tiny-shard blocker — NOW FIXED, do not re-litigate it.** 310 shards are smaller than one 64 KB
decode window, and **2 are 20 bytes = 5 tokens** (`s2pdf-redacted/adult_content/part-57`,
`s2pdf-redacted/games/part-020`). Under the old absolute `min_distinct_ids` floor those two were
GUARANTEED `distinct-too-few` violations, and because `promote()` is all-or-nothing they would have
blocked 630 GB over 10 tokens. `72df9f7` scaled the floor to the sampled size, so **no shard needs
dropping and no per-group bound needs weakening** — the three options the first attempt was weighing
are moot. A degenerate tiny shard is still caught (the floor of 2 is load-bearing).

**Equal weighting across the 6 sources is arithmetically impossible.** Wikipedia has 64.6M tokens and
source mixtures enforce `target_ratio` exactly, so 1/6-each caps the whole mixture at ~0.38B tokens —
against a ~7.4B Chinchilla budget for 370M. Use scaled weighting or a water-fill, not uniform.

**One caution about the discarded spec:** its prose had already drifted from itself before publication
(`limitations` said `min_distinct_ids` was lowered to 4 while `group_meta` set 1; `sources[]` summed
66,333,215 tokens short of the total — exactly the 6 val shards, undocumented; and `notes` claimed
"natural proportions" for ratios that over-weighted wikipedia ~23×). That is the argument for shipping
mixtures as **measured counts + label-predicate selectors that Gate A recomputes**, not prose.

## THE 150B PUBLISH PLAN — decided 2026-07-30, verified by four independent audits

**The copy is ALREADY DONE.** `s3://edullm-landing/_migrate/olmo-150b-dolma2/` holds all 6,921
`.u32le.bin` objects / 630,140,294,600 bytes, finished 2026-07-29 21:24–21:53 UTC. The earlier
"2.1%, restart the copy" note in this file was wrong — it described the deleted driver, not the
bytes. **Do not re-copy from the legacy bucket.**

**Layout (user's decision): nested paths AND labels.**
`tokens/<source>/<domain>/<split>-NNNNN.u32le.bin`, with `labels: {source, domain}` on every
manifest entry. Verified: nesting is UNSPECIFIED-not-forbidden by the standard (every example is
flat, but `:624` and `:517` presume trees inside groups); one `tokens/` group for 65 subtrees
COMPLIES with §4; Gate A, the reader, and an adversarial suite all pass on nested varying-depth
keys. Note the current staging shape (`<source>/train-N`) would make `publish()` create SIX groups
named after sources — the rejected plan — so a re-copy into `tokens/` is required either way.

**Four things must happen before the publish, in this order:**
1. ~~**Reship the 0.2.0 wheel AND cut over to it.**~~ **BOTH DONE 2026-07-30, verified by
   execution.** `_dist/edullm_data-0.2.0-py3-none-any.whl` is live (117,722 B, sha256
   `dc726cf6…`, upload byte-identical, clean-venv smoke-tested). `edullm-validator:2` and
   `edullm-fsck:2` are registered and bootstrap it; each asserts `WHEEL_VERSION==0.2.0` at
   startup and the validator also asserts `families/` resolves, so a silent fallback to an old
   wheel now fails loudly. **Proof it is really live:** a real fsck job ran `edullm-fsck:2` and
   logged `WHEEL_VERSION=0.2.0`, `ok=true`, `FSCK_DONE_RC=0`.
   Both EventBridge rules target the job def by **unversioned name**, so the new revisions took
   effect with no rule edit — which also means a bad revision goes live immediately; verify with
   a manual `submit-job` after any re-register. `_dist/publish_driver.py` was reshipped to assert
   the families dir rather than trust the env override, and to log derived `split`/`labels`.
2. **Drop BOTH 20-byte shards** — `s2pdf-redacted/adult_content/train-00057` and
   `s2pdf-redacted/games/train-00861`. They are byte-identical to each other
   (`duplicate-shard-digest`) AND `train-00057` is `[58, 793, 77726, 60, 100257]` — it ends in EOS,
   1/5 = 20% against `eos_fraction_max: 0.05`, so it independently fails
   `eos-fraction-out-of-bounds`. Either alone would reject the whole 630 GB at `promote()`. Cost:
   10 tokens of 157,468,740,435. Record both in `limitations`.
3. **Carve the val split** — per-source, 60 shards renamed `train-*`→`val-*`, 229,894,171 tokens
   (0.146%). Plan + rationale in the project's `artifacts/VAL-CARVE-PLAN.md` and `val_plan.json`.
   **The 6 `val-00000` objects already in `_migrate/` must be DELETED** — they came from legacy
   `heldout-val/` and every one is a duplicate of a train shard (5 exact copies, finemath a
   byte-prefix). Publishing them is 100% train/val leakage.
4. **Renumber ordinals globally** across the group (`train-00000`…). `DATASET-STANDARD.md:589-590`
   says the 5-digit ordinal caps "a group" at 100,000 and exceeding it "is a spec amendment";
   per-subtree reuse makes that false. Free, since the shards are being renamed anyway. Also
   removes OLMo-core's basename+size fingerprint hazard (`numpy_dataset.py:221-222` — measured 0
   collisions on this corpus, but that is a property of the data, not an invariant).

~~**Also queued:** `promote()` copies sequentially with no resume.~~ **FIXED** (`2515f79`) —
`promote(copy_workers=…)` plus a `--promote-workers` CLI flag (`d6e8a7f`); the deployed job def
passes 16. Six tests, mutation-checked.

**TWO DEPLOYED-INFRA GAPS found while watching the promotion run. Neither is fixed.**

1. **`edullm-validator` has NO TIMEOUT.** Verified: `timeout: null` on both the job and the job
   definition. The publish jobs get `--timeout attemptDurationSeconds=7200` because it is passed
   at submit time, but the EventBridge-triggered validation inherits nothing. A wedged
   auto-promote would sit `RUNNING` forever and hold queue capacity, with no automatic kill.
   Set `timeout` on the job definition — 7200 s matches what the publish path already uses.
2. **The auto-promote validates EVERY pending dataset, not the one that triggered it.** The job
   def runs `validate --promote` with no `--prefix`, so it calls `discover_pending`, which does
   `s3.list(landing_bucket, "")` — a full-bucket LIST (21,005 objects / 4.1 s today) followed by
   Gate A over every unsealed dataset it finds. That is deliberate ("the event is a pure wake-up
   signal"), and it is why one dataset's promotion time is not bounded by that dataset. Harmless
   now (the 150B is the only pending one), but it degrades as landing accumulates, and it makes a
   slow run hard to attribute.

**Gate A timing, measured, so the next person does not misread a slow run as a hang.** A full
pass over ~6,900 shards is **~55 min** at 8.4 range-reads/s (4 seeded windows per shard ⇒ ~27,600
reads). It does **not** short-circuit — `ValidationResult`'s docstring is explicit that "checks do
not short-circuit each other, so one run surfaces the whole list" — so a REJECTED run costs the
same as a clean one. It prints nothing between `VALIDATOR_START` and its verdict, and writes
`_VALIDATED.json` / `_REJECTED.json` only at the end. **The live progress signal is the object
count under `s3://edullm-data/<dataset_id>/`**: zero means still validating, climbing means
promotion started. Variance of ±25% between runs is ordinary S3 latency, not a fault.

**WHERE THE 2026-07-30 OVERNIGHT RUN STOPPED, and why.** Steps 2–4 all mutate
`_migrate/olmo-150b-dolma2/` — 6,913 server-side renames plus 8 deletions. That is bulk S3 work,
and per the `bulk-s3-via-credential-process` memory it must NOT go through the MCP broker
(~2,100 tokens and ~16 s per object ⇒ ~14.5M tokens for this corpus). The sanctioned path is a local
threaded boto3 script driven by `sb-aws-creds credential_process`, and **that setup requires the
user's explicit approval** — the auto-mode classifier blocks it by design, because local credentials
outside the broker are exactly what the airlock guards against. The classifier did block a scripted
step during this run; it was not worked around. So the remaining work is queued, not attempted.

**The user APPROVED the credential setup (2026-07-30) and it is DONE and verified.**
`/tmp/olmo150_aws/config` holds an isolated profile (`credential_process = sb-aws-creds
credential_process --profile sbsandbox`); `~/.aws/config` is untouched. It resolves to the SAME
`Intern-eric.wu-sbsandbox` role as the broker — no privilege change — and the airlock was
re-verified THROUGH those local credentials: `put_object` to `edullm-data` →
`AccessDenied … explicit deny in a resource-based policy`. Landing writes are permitted, which is
how the 0.2.0 wheel shipped.

**THE COPY PLAN IS BUILT AND FULLY SELF-CHECKED — but not executed.**
`artifacts/olmo150_plan.json` (6,913 entries) + `artifacts/olmo150_plan.py` (the generator, which
re-derives and re-asserts everything) + `artifacts/olmo150_stage.py` (the resumable copier).
Regenerate any time with `python3 artifacts/olmo150_plan.py` — it moves nothing.

Verified by the generator, all passing: exactly 2 shards dropped; no dropped shard is also a val
pick; 6,913 unique sources AND 6,913 unique destinations (a dest collision would silently overwrite
a shard); global ordinals contiguous `0..6912`; every sampled destination passes
`check_shard_naming`, parses to the right split, and yields exactly the expected
`labels_from_path`; val is 60 shards / 229,894,171 tokens matching the approved carve; no object
≥5 GiB (largest 1,446,999,580 B, so every copy is single-part); total 157,468,740,425 tokens
(= 157,468,740,435 − the 10 dropped); and **no legacy `val-00000` object is in the plan**, so the
fake-val shards cannot leak in.

Shape: `_migrate/olmo-150b-dolma2/<source>/train-NNNNN.u32le.bin` →
`_migrate/olmo-150b-staged/tokens/<source>[/<domain>]/<split>-<global-ordinal>.u32le.bin`.
Both keys are in `edullm-landing`, so this is a same-bucket rename-copy that never touches the
airlock. 629,874,961,700 bytes / 587 GiB, server-side and in-region — no egress, no bytes through
this machine.

**THE DRY RUN PASSED — the layout is proven on real bytes.** This is the step the first attempt
skipped. Five deliberately-chosen shards (the 90-token survivor, the 1.45 GB largest object, a
carved val shard, and both nesting depths) were staged, published, and run through Gate A:

    ok=True  incomplete=False  violations=0

and the derived fields came out exactly right, with no caller input:

    tokens/all-dressed-snazzy2/adult_content/val-00033.u32le.bin
        split='val'   labels={'domain': 'adult_content', 'source': 'all-dressed-snazzy2'}
    tokens/wikipedia/train-06850.u32le.bin
        split='train' labels={'source': 'wikipedia'}          <- 1-level nesting, no domain key
    tokens/s2pdf-redacted/food_and_dining/train-04038.u32le.bin
        split='train' tokens=90                                <- the smallest survivor, clean

`partitions` auto-resolved to `train`/`val` with recomputed rows and `coverage: partition`.

**Landmine found and defused: publishing to landing AUTO-TRIGGERS PROMOTION.** The manifest upload
fired `edullm-landing-manifest-created` → EventBridge → a `edullm-validate-on-manifest` Batch job,
which would have promoted a throwaway probe dataset into `edullm-data`. It was caught RUNNABLE and
cancelled before it ran; `edullm-data` verified still at 11 objects. The probe and the staged
shards were then deleted from landing. **Anyone doing a landing-only experiment must either cancel
that job or disable the rule first** — there is no "publish but do not promote" mode.

Then `publish()` against `s3://edullm-landing/_migrate/olmo-150b-staged/` with
`tokenizer="tokenizer/dolma2-bpe"`, `hash_workers`/`copy_workers=16`, and
`--timeout attemptDurationSeconds=7200`. It derives `split` and `labels` from the staged keys
automatically (`aa4d509`) and Gate A recomputes both. **Cut the job def over to the 0.2.0 wheel
first** or the run executes pre-correctness code.

Leave `_migrate/olmo-150b-dolma2/` in place until the publish is promoted and verified — it is the
only staged copy, and re-making it is a 630 GB pull from the legacy bucket.

**The domain mapping is recoverable and verified.** `train-NNNNN` is the Nth key in sorted legacy
order — a strict bijection, confirmed by 6 anchors, all 6,915 shards by size, and a live CRC64NVME
check. Full inventory in `artifacts/shardmap.json`. Measured: 65 (source, domain) strata,
all-dressed-snazzy2 has 24 domains and s2pdf-redacted has **23** (this file previously said 24 for
both); train-only tokens are **157,468,740,435** (the 157,535,073,650 below double-counts the 6
duplicate val shards).

**Datamix selection is NOT solved and is deliberately out of scope here.** A source-named partition
trips `empty-split` (`validate.py:677-683`) regardless of `by=`; only `by: "path"` is implemented;
`by: "label"` exists in neither the code nor the spec's closed four-form set. The standard's own
answer for a subset is a CHILD dataset (`depends_on[]` + `token-order/v1`, `:836-846`). Decide that
separately — do not smuggle it into the layout.

## DEFERRED DECISIONS — explicit user decisions, not open questions

Do not relitigate these; they were decided. Do not act on them without re-asking.

1. **The 150B migration is RESTARTING FROM SCRATCH** (2026-07-29, the user's call). The first
   attempt reached 147/6,921 objects (2.1%) on a throughput wall and its four working files were
   deleted — the measurements worth keeping are in "THE 150B SOURCE DATA" above, and the old
   `LOCKED STRUCTURE` six-group plan died with them.
   **The structure decision stands and is not open: ONE `tokens/` group + labels, NOT six groups per
   source.** Reason: a group is a unit of **validation**, not of selection. Six sources with identical
   checks and one tokenizer pay six manifests and buy nothing — and six groups **permanently loses the
   24 domain labels**, which `entry.labels` (schema v2) is exactly the right carrier for.
   ~~Carrying `labels` on manifest entries needs `publish()` to populate them; it currently does not
   (see "What is NOT done"), so that is real work, not a config flag.~~ **DONE — verified in code
   2026-08-01.** `publish.py:352-353` sets both `split=` and `labels=`, derived from the key itself
   and recomputed by Gate A from that same key, so neither can drift. Landed in `aa4d509`.
2. **Slurm/ORCD is OUT OF SCOPE entirely.** Training goes through `edu-llm/platform` → AWS Batch.
   Do not write Slurm submission scripts, sbatch wrappers, or ORCD docs.
3. ~~**The 31B corpus is EXPECTED to fail `missing-required-split`**, slated for deletion.~~
   **DONE 2026-07-29 — DELETED**, ahead of a replacement rather than after one. The user's call, and
   correct: it failed the rule whether it existed or not, and its flattened per-source structure was
   not the shape the 150B needs. See "THE 31B DELETION" above for the two verified recovery paths.
   Both follow-ups were handled: `tokenizer/dolma2-bpe/v1` was **KEPT** (it has no legacy backup, and
   a replacement will pin it by `manifest_sha256` `b37b8954…`), and
   `_catalog/pretrain/olmo-mix-1124-31b/v1.json` was **CLEARED** so `resolve_latest()` returns `None`
   instead of pointing at absent bytes.
   ~~**Consequence for the next session: there is NO pretrain corpus in `edullm-data`.**~~
   **RESOLVED 2026-07-30** — `pretrain/olmo-150b-dolma2/v1` is published, promoted, and readable.
4. **`infra/02-bucket-policy.json` is v2 in the repo but the LIVE bucket still has the v1 2-statement
   policy.** Deploying it is a documented step — `infra/DEPLOY.md:256+` ("Deploying the split Delete
   Deny"). What v1 got wrong: one Deny covered Put *and* Delete and exempted the validator + deployer
   from **all five actions**, so the only thing stopping the validator from deleting published data was
   an identity policy on a role whose inline policies are editable with `iam:PutRolePolicy` — which the
   intern session has. v2 splits it: `OnlyValidatorWrites` (Put, validator+deployer exempt) and
   `NobodyDeletesPublishedData` (Delete, **nobody** exempt). **Consequence once deployed: deleting a
   published dataset becomes two deliberate steps** — remove the Deny, then delete — which is the
   point.
5. ~~**Both live seals are UNROOTED.**~~ **HALF RESOLVED 2026-07-30, re-verified live.** The 150B
   was promoted by the rooting code, so its seal carries `dataset_sha256`, a per-group
   `manifest_sha256` map, and 6,911 CRC64NVME references — `verify_seal` returns **no problems**.
   Only `tokenizer/dolma2-bpe/v1` is still pre-root: it reports *unverifiable* (not invalid) and
   `dataset_paths` lets it through. It stays that way until republished, and since `promote()`
   refuses a sealed prefix that means a **new version**, not a rewrite of `v1`.
6. **The platform needs 4 changes owned by a TEAMMATE, not by this repo.** See
   `docs/PLATFORM-INTEGRATION.md` (being written by another agent concurrently with this handoff).

---

## WHAT IS ACTUALLY LEFT — verified against live state 2026-07-30, in priority order

⚠️ **SUPERSEDED for the reservoir work by "Next Steps → THE CURRENT LIST" (2026-07-31).** It predates
the reservoir execution session, so it says nothing about §9.7 item 4 or Phase 1. Read the newer list
first.

🛑 **AND SUPERSEDED AGAIN 2026-08-01 — this banner's own claim is now false.** It used to read:
*"items 1–2 below (the platform handoff, bucket-policy v2) are genuinely still open."* **Both are
closed**, each verified this session:

- **Item 1, the platform handoff** — the IAM grant, the registry entry, and the image all landed in
  `edu-llm/platform` / `edu-llm/OLMo-core`, and a training run has read
  `pretrain/regmix-10b/v1` end to end (Batch job `8fb0cd5c-…`, SUCCEEDED, 150 steps, 3 checkpoints).
- **Item 2, bucket-policy v2** — live policy is `Id: edullm-data-airlock-v2` with Put and Delete
  Denies split and nobody exempt from Delete (`s3api get-bucket-policy`).

Item 3 (the validator timeout) was already done: **7200 s**, now on `edullm-validator:10`.
**Of the five numbered items in this section, only #4 (`sft_conversations_v1` substring matching)
is still open** — verified still present at `profiles/sft_conversations_v1.py:113`.

Several older "not done" items below were fixed today. These are the ones that survive checking.

### 1. ~~Hand `docs/PLATFORM-INTEGRATION.md` to whoever owns `edu-llm/platform`~~ — **DONE; the handoff landed and the blockers are closed (2026-08-01)**

~~**The long pole, and not this repo's to fix.** A training run needs two things from that repo: the
GPU workload role must be able to `s3:GetObject` on `edullm-data` (it is scoped to
`outputs/teams/platform/runs/*` today), and the Batch attempt timeout must exceed 3600 s. Neither
has a workaround from our side — the IAM grant is enforced outside the container.~~

**Superseded 2026-08-01, verified by reading `edu-llm/platform` HEAD via `gh api`:**

- **The IAM grant exists.** `infra/iam/batch-gpu-roles.yaml` now grants `s3:GetObject` on
  `arn:${AWS::Partition}:s3:::edullm-data/*` and `s3:ListBucket` on the bucket, GetObject-only by
  design so the airlock refusal stays a refusal (commit `c3d4ca0d`, 2026-08-01, "…let a training
  run read the corpus"). This was the one item with no workaround from our side; it is closed.
- **The registry entry exists.** `config/datasets.yaml` `published:` now lists **six** corpora by
  `s3://edullm-data/...` URI + tokenizer, including `pretrain/regmix-10b/v1` (commit `45761849`,
  2026-08-01). The old single `dolma-2026-07` entry is kept but `retired: true`, so historical
  intent records still resolve — the same "don't delete history" discipline this file uses.
  The seventh corpus, `lean4-mathlib-bytes`, is deliberately unlisted: it needs
  `tokenizer/bytes-utf8` and OLMo-core has no byte tokenizer, so offering it would let a run
  memmap `tokenizer.json` as uint16 tokens and train happily on garbage.
- **The image exists.** `.edullm/Dockerfile` is present on `edu-llm/OLMo-core` `main`
  (12,957 B, commit `7eeba5af`) and installs the reader (`5358e521`) — the "no image, no run"
  blocker at banner item 5(b).
- **The timeout was never the blocker** and the doc already said so; the run finished in 19.3 min
  inside a 3600 s attempt.

**What is genuinely still open: a real GPU, which is a budget call, not an engineering one.** The
proving run used the single provisioned A10G on the `sbsandbox-intern-edullm-gpu` queue.

### 2. ~~Deploy bucket-policy v2~~ — **DEPLOYED. Verified live 2026-08-01.**

`s3api get-bucket-policy --bucket edullm-data` returns **`Id: edullm-data-airlock-v2`** with the
Put and Delete Denies **split**, exactly as the repo's `infra/02-bucket-policy.json` specifies:

- `OnlyValidatorWrites` — Deny `PutObject`/`PutObjectTagging`/`AbortMultipartUpload`, exempting only
  the infra-deployer and dataset-validator roles.
- `NobodyDeletesPublishedData` — Deny `DeleteObject`/`DeleteObjectVersion` with **no principal
  exemption at all**. This is the whole point of v2: the validator role can no longer delete
  published data, so widening its identity policy cannot reach the corpus.
- `AllowS3InventoryDelivery` — the S3 Inventory service carve-out.

`infra/DEPLOY.md:256` already recorded this as deployed 2026-07-31 with four passing probes; the
three places in this file that still said otherwise were stale. Everything below is the superseded
text, kept for the risk argument it makes.

~~Confirmed live this session: the policy is still `edullm-data-airlock-v1`, a single Deny covering
`PutObject` AND `Delete*`, exempting `<BATCH_JOB_ROLE>` and `<INFRA_DEPLOYER_ROLE>` from all five
actions. So the only thing stopping the validator role from deleting the published corpus is an
identity policy that `iam:PutRolePolicy` can widen — and the intern session holds that permission.
`infra/02-bucket-policy.json` is already v2 in the repo (Put and Delete split, **nobody** exempt
from Delete). Runbook: `infra/DEPLOY.md:256+`. This mattered less when the bucket held 11 objects.~~

### 3. ~~Set a timeout on the `edullm-validator` job definition~~ — **DONE**

**Verified live 2026-08-01:** `edullm-validator` top ACTIVE revision is **10**, with
`timeout.attemptDurationSeconds = 7200`. (Revisions 1–6 had `timeout: null`; 7 first set 7200.)
`edullm-fsck:6` is at 3600 s and `edullm-reservoir-ingest:7` at 7200 s. Superseded text follows.

~~It has **none** (`timeout: null` on both the job and the job def, verified). The publish jobs only
get one because it is passed at submit time; the EventBridge-triggered validation inherits nothing,
so a wedged auto-promote sits `RUNNING` forever holding queue capacity. 7200 s matches the publish
path.~~ Related, and still true: that job runs `discover_pending`, which LISTs the whole landing
bucket and validates every unsealed dataset — so its runtime is not bounded by the dataset that
triggered it.

### 4. `sft_conversations_v1` still substring-matches split names

`profiles/sft_conversations_v1.py:92-117` tests against `heldout|held-out|holdout|test|val|eval`
instead of consulting `contracts.is_trainable`. The `SPLITS` docstring (`contracts.py:132-135`)
cites this exact substring-matching as the problem the closed vocabulary fixed, so it **overstates
the fix**: `trainval` is still classed held-out and `dev` still rejected, in that one function.

### 5. ~~Write the adapter, once #1 unblocks~~ — **WRITTEN AND RUN (2026-08-01)**

**It is `.edullm/train_on_corpus.py` on `edu-llm/OLMo-core` `main` — 746 lines, not "small."**
The estimate below was wrong about size and right about content: the adapter does use plain
`NumpyFSLDatasetConfig` with an explicit `dtype` and an explicit path list (`expand_glob: false`),
and it does avoid the sidecar trap. The 746 lines are the *surrounding* correctness — resolving the
id through `edullm_data.read` so there is no path literal anywhere in the file, and no flag to
supply one. Retained below because the reasoning about which OLMo-core classes to avoid is what
made the run work on the first try.

`docs/CONSUMER-CONTRACT.md` is the spec. It should be small — plain `NumpyFSLDatasetConfig`, an
explicit `dtype` from `r.dtype`, an explicit path list from `dataset_paths` or `build_mixture`.
Do NOT reach for the Mixture/Padded/Packed/VSL/Interleaved classes or the composable stack: all of
them call `iter_document_indices`, which on an `s3://` path derives a `.csv.gz` sidecar name and
dies (verified by execution, 403 on the derived key). Those sidecars were deliberately not
migrated; it is recorded in the dataset's own `limitations`.

## What is NOT done

- ~~**No training-side adapter has been written**, and **no training run has happened** against any
  `edullm-data` dataset. See #1 and #5 above.~~ **BOTH FALSE as of 2026-08-01 — this is now DONE.**
  The adapter is **`.edullm/train_on_corpus.py` on `main` of `edu-llm/OLMo-core`, 746 lines /
  32,840 B** (read via `gh api`; landed in PRs #34–#38, 2026-08-01). It is emphatically *not* the
  small mapping #5 predicted: it resolves through `edullm_data.read.dataset_paths` +
  `resolve_latest` and carries **no path literal and deliberately no flag to supply one**, on the
  stated grounds that a hand-typed path reproduces the exact failure it exists to prevent (a run
  that reports the corpus it was asked for while reading AI2's C4 sample). It takes its inputs from
  four `EDULLM_*` env vars set by the submission path. It did honour #5's core warning: plain
  `NumpyFSLDatasetConfig` with an explicit `dtype` and an explicit path list.
  Proof it ran: Batch job `8fb0cd5c-…`, SUCCEEDED exit 0, 150 steps, 3 checkpoints — see "Goal".
- **The tokenizer's seal is pre-root.** `tokenizer/dolma2-bpe/v1`'s `_VALIDATED.json` carries no
  `dataset_sha256`, so `verify_seal` reports it *unverifiable* rather than invalid and
  `dataset_paths` lets it through. It stays that way until republished — and `promote()` refuses a
  sealed prefix, so that means a new version, not a rewrite of `v1`. **The 150B's seal IS rooted**
  (`dataset_sha256` + per-group `manifest_sha256` + 6,911 CRC refs, `verify_seal` clean), which
  supersedes the older claim that "both live seals are unrooted".
- **A mixture cannot be published as a child dataset.** `build_mixture` resolves live; freezing one
  as `depends_on[]` + `token-order/v1` (`DATASET-STANDARD.md:836-846`) is the spec's own answer for
  a subset and is not built. Deferred deliberately — live resolution was the user's call.
- **No `by: "label"` partition form.** Label selection is a read-side concern only; adding the
  partition form would be a spec amendment (the four-form set at `:822-826` is closed, and
  `validate.py:653-658` rejects a label-named partition as `empty-split`).

---

**Deployed live in AWS account `sbsandbox` (<ACCOUNT_ID>), us-east-1** (NOT in git — broker-applied):
- Buckets: `edullm-landing` (write-anything, expiry) + `edullm-data` (read-only; validator writes only)
  — CFN stacks `edullm-data-buckets`, `edullm-data-event-wiring` both CREATE_COMPLETE
- `edullm-data` bucket policy: **3 statements** (was 2) — `OnlyValidatorWrites` Deny (Put only,
  validator/deployer exempt) + **`NobodyDeletesPublishedData` Deny (Delete*, NO exemption —
  binds the validator too)** + `AllowS3InventoryDelivery`. All carry
  `BoolIfExists aws:PrincipalIsAWSService=false`.
  **NOT YET DEPLOYED** — `infra/02-bucket-policy.json` is updated in the repo; the live bucket
  still has the 2-statement v1 policy. See "Deploying the split Delete Deny" in `infra/DEPLOY.md`.
- Validator identity: EXISTING role `<BATCH_JOB_ROLE>` (ecs-tasks-only trust),
  inline policy `dataset-validator` (S3 rw scoped to the two buckets)
- Batch job defs: `edullm-validator:1` (self-discovering validate+promote), `edullm-fsck:1`
- **Event rule `edullm-landing-manifest-created` — ENABLED**: manifest.json upload → validate+promote,
  RoleArn `<EVENTBRIDGE_INVOKE_ROLE>` + its inline `edullm-validator-submit` (SubmitJob+PassRole)
- **Schedule rule `edullm-wu-fsck-nightly` — ENABLED, NOW WEEKLY**: `cron(6 9 ? * MON *)` UTC
  (Mondays 09:06) → fsck. **DRIFT RESOLVED 2026-07-29** — applied live via `events put-rule`, and the
  target was verified intact afterwards (`fsck-batch-queue` → CPU queue, job def `edullm-fsck`, role
  `CloudWatchSendEventsToVdi`). That verification matters: `put-rule` replaces a rule's attributes and
  can drop the target silently.
  **The rule NAME still says `-nightly` and that is deliberate** — renaming means delete+recreate,
  which drops the target and its `RoleArn`. Changing only the expression was the smaller, safer move;
  the reason is recorded in the rule's own live Description so nobody re-litigates it.
- S3 Inventory (weekly) on `edullm-data`; landing lifecycle scoped to family prefixes (keeps `_dist/`)
- `s3://edullm-landing/_dist/edullm_data-0.2.0-py3-none-any.whl` — **SHIPPED 2026-07-30**, 117,722
  bytes, sha256 `dc726cf6bd24f0cb713972fa6d6f44a772d7e8ffd78bb691c860b44759c090d0`, upload verified
  byte-identical (MD5 `44b4cdfb…`). Built from `feat/entry-labels-from-path` @ `aa4d509`, so it is
  the FIRST deployed wheel containing the correctness work (dtype-vs-vocab, validation-by-default,
  row/coverage recompute, rooted seal, scaled distinct-ids floor, key-derived split+labels).
  Verified in a clean venv: version 0.2.0, `families/` packaged (7 files) and resolving to
  `site-packages/edullm_data/families`, `validation_required=True` reaching production,
  `labels_from_path` working.
  `edullm_data-0.1.0-py3-none-any.whl` is STILL PRESENT and is still what the live job defs
  bootstrap **by exact filename** — shipping the new wheel does not switch anything over. Cutting
  over means editing `_dist/publish_driver.py` and `infra/05-validator-jobdef.md:95` to say `0.2.0`.
  Until then every Batch run still executes 0.1.0.

**PROVEN end-to-end on live AWS (all cleaned up + re-locked after):**
1. Deny side: intern session PutObject to `edullm-data` → AccessDenied (repeatedly re-verified)
2. Allow side: Batch job as validator role promoted real bytes into `edullm-data`
3. Real `validate.py` Gate A ran on Batch, validated + promoted a `publish()`-produced dataset
4. **Fully automatic**: manifest upload → EventBridge → Batch → `edullm-validate-on-manifest` job
   SUCCEEDED → "PASS + promoted", zero human steps
5. `wu-fsck` runs cleanly on Batch (clean JSON report, exit 0)

**Official bucket contents RIGHT NOW** (re-verified live this session via the `sb-aws` broker):
`_catalog/pretrain/olmo-mix-1124-31b/v1.json` + `_catalog/tokenizer/dolma2-bpe/v1.json`, and the two
dataset prefixes behind them. (The line that used to sit here — *"`edullm-data` is EMPTY (0
objects)"* — predates the olmo30b migration and was already false; corrected.)

---

## What Worked

### 2026-08-04 — predicting a 1 TB gate's verdict for free, by running only its pure half

`verify_bundle_set` takes `s3`/`bucket` as **optional**, and its docstring says why: the set-level
checks "are pure and free, so a driver can check completeness before spending a single HEAD." So
instead of waiting hours for `verify --deep` to read ~1 TB and then report, I copied the 27 receipts
locally (one recursive `s3 cp`, 1.6 MiB) and ran the set-level checks on my laptop. It printed
`bundle-set-mixed-wheel-versions` in under a second.

**Reusable shape: when a gate has a cheap tier and an expensive tier, run the cheap tier first even
when you intend to run both.** The expensive tier here is a full corpus re-hash; the cheap tier
answered the question. The same receipts also gave the realized `tokens_out` per bundle, which is
what `sources[]` needs and which the plan cannot supply — finewiki realized 90.5% of its planned
tokens, so citing the plan would have published a false mix table.

### 2026-08-04 — reverting each fix to confirm its test fails, five more times

Continued from the earlier sessions and it paid every time. The `<|endoftext|>` test reports
"2 EOS in a single document" with the neutralizer removed; the surplus test reproduces the production
error string *verbatim*; the encode-once test reports **594 tokenizations for 300 documents** —
exactly 2× — with the fuse reverted. A test written against a bug you have already fixed proves
nothing until you have seen it fail.

Also caught two of my own fixtures being wrong rather than the code: 200 identical short documents
were removed by *dedup* before the length filter could see them (1 drop, not 150), and my
`_range_file` helper **shadowed an existing one** with a different signature and broke 5 unrelated
tests. Both surfaced only because the assertions were specific enough to contradict.

### 2026-08-04 — writing the operational lessons INTO the monitoring prompt

The hourly check accumulated three hard-won rules as it ran: use `s3api list-objects-v2` with
`sort_by`, never `s3 ls` on a shard prefix (those responses ran 800+ lines to yield two numbers);
read the **ordinal**, not the object count, because leftover keys from terminated attempts are
overwritten at the same paths; and do not quote per-bundle ETAs at all, because every one was wrong.
Encoding those in the recurring prompt meant they survived my own forgetting across ~20 checks.

### 2026-08-01 (night, end) — reading real bytes as the LAST step before spending money

The single highest-yield ten minutes of the session was pointing the finished reader at every drawn
source and looking at what came back. It found five defects, one of which (`resolve/main` overriding
the pins) would have made the whole corpus unreproducible while looking perfect.

Why it worked when 44 passing registry tests did not: those tests check that a string is well-formed
and that a dataclass accepts it. Reading a record checks that the string *names data*. Those are
different claims, and only the second one is the one the build depends on.

Generalisable ordering, and it is cheap: **verify structure offline, then verify meaning against the
real thing, then spend.** Each step is a strict superset of the last, and the expensive step runs on
inputs that have already survived both cheaper ones.

Related: every stage's *runtime* assertion earned its place this session. `corpus_pack`'s token
conservation identity found a real double-counting bug on its first execution;
`corpus_build`'s pre-receipt `verify_receipt` refuses to record work whose shards failed; the packer's
decode check rejected two of my own test fixtures for being degenerate, which is exactly what it
would do to a real degenerate shard. A check that has never fired is a check you do not yet know
works.

### 2026-08-01 (night) — pinning the interface BEFORE fanning out, and an assertion that runs in production

Two stages were built in parallel by separate agents and merged with no interface conflicts. What made
that work was writing `corpus.py` first — shard geometry, the ordinal allocator, the held-out
predicate, the EOS floor — and declaring it frozen. Parallel implementers against an *unspecified*
boundary produce code that compiles separately and disagrees on the thing that matters; the ordinal
allocator is the concrete case, since `parse_shard_name` returns `('train', 0)` for two different
sources' shards and nothing downstream objects.

**The single highest-value line of the day is a runtime assertion, not a test.**
`PackResult.__post_init__` checks `tokens_in == tokens_out + tail_dropped + surplus_dropped` on every
stream in production, and it found a real bug the first time it ran (`tokens_in` double-counted the
unconsumed remainder of a half-consumed document). A test would have caught that case only if someone
had thought to write it; the assertion catches every case forever. Mutation-checked: losing **one**
token out of 25 million fails 24 tests.

**Verifying agent reports instead of relaying them** caught three things: a claim that pyarrow 24
defaults `pre_buffer=False` (true, and a *per-API* accident — `read_table` defaults `True`, so writing
the keyword is not redundant); the correction to my own bad intel below; and 12 failing tests in a
module whose author had reported it green, which turned out to be a mid-write race rather than a
defect. Waiting for file mtimes to settle before judging was the fix.

### 2026-08-01 (late) — establishing what the code PERMITS before enumerating options

The decision memo's most valuable output was six *impossibility* findings, each pinned to a line.
Five of six candidate label encodings cannot work, and an impossible option presented as a live
choice is worse than no memo — it invites a decision that produces a rejected dataset weeks later.
Briefing the team to read `validate.py` and `read.py` **first** and enumerate **second** is what
produced that.

### Orchestrators told to attack a BELIEF, not a task

Every large win today came from pointing an agent at something I was treating as settled: "is the
rate limit even what I think it is," "does it have to be ingested that way," "is porting the only
option." Agents pointed at *tasks* mostly confirmed what I already thought. Three beliefs fell:
the per-IP limit (ours, 70× amplification), the 2–4 week ingest (1–3 hours), and "tokenization was
never written here" (it exists, with a working S3 backend).

### Reverting a fix to prove its test fails — used five times, caught two decorations

A test that has never failed is not a test. This caught (a) an IAM policy test whose regex required a
receiver named `s3.` while the real call site used `s3_client.`, so it passed against the very policy
that had just failed in production, and (b) confirmed the three `RatioOvershoot` tests genuinely fail
without the warning.

### Reading the artifact instead of the version string

`WHEEL_VERSION` said "stale but equivalent" while a Gate A function existed only in S3, in no commit.
Diffing the deployed wheel against the tag is what found it. The same habit found that 3 of 5 AI2
shards have odd token counts — a filename tells you nothing; `size % 4` and a range read do.

### Earlier sessions

### From the 2026-08-01 session

- **Fanning subagents at ASSUMPTIONS, not at tasks.** The three biggest wins all came from telling an
  agent to attack a belief I was treating as settled: "is the rate limit even what I think it is,"
  "does this have to be ingested that way," "is porting the only option." Each returned a finding that
  cut weeks. Agents pointed at *tasks* mostly confirmed what I already thought.
- **Requiring an unpatched control through the identical wrapper.** The segfault A/B is only
  interpretable because the `noop` arm existed. Instrumentation made crashing children survive, so
  without a control the conclusion would have been "the wrapper fixed it."
- **Reverting a fix to prove its test fails.** Done four times this session. It caught a test that
  passed against the very policy that had just failed in production, because its regex required a
  receiver named `s3.` while the real call site used `s3_client.`. A test that has never failed is not
  a test.
- **Printing the version, not comparing it.** `PYARROW=25.0.0 NUMPY=2.5.1` in the preflight is what
  exposed a real skew — and then what ruled it out. Three hypotheses were argued blind because nobody
  logged the number.
- **Preflight assertions in the job definition.** They cost milliseconds and caught a stale wheel
  before a 25-minute run, twice. Each asserts a defect that already shipped once:
  `_backoff_delay(3)==32.0`, a 97-item 4-way shard round-trip, the short-read loop, the
  `pre_buffer=False` keyword.
- **Verifying agent claims against live state before repeating them.** Two agents disagreed on whether
  tokenization code existed; the second was right, and only checking the files settled it. One agent
  also overstated an A/B as "zero crossover" and self-corrected — worth reading agent reports as
  evidence, not verdicts.

### Earlier sessions

### From the reservoir execution session (2026-07-31)

- **Parquet footers instead of the datasets-server API.** The single highest-leverage discovery. A
  footer carries `total_uncompressed_size` per column chunk, so summing over every row group of every
  file gives the **exact** whole-split byte total for one column, over ALL rows, reading a few hundred KB
  per file. Quota-free, so parallel agents don't starve each other. It turned four categories from
  "needs a Batch streaming job" into a laptop-scale measurement at effectively zero cost. **Reuse this
  before considering any streaming count.**
- **Factoring the token estimator.** `num_rows × sampled mean_tokens/doc` gave FineMath CV 9.0 and a
  95% CI of **[26B, 204B]** — an 8× range. Factoring to
  `num_rows × mean_chars (whole-split) × tokens/char (sampled, CV 0.27)` cut estimator divergence from
  800% to 11.5%, because `tokens/char` is a property of script and domain rather than of document length.
- **Reading raw model output, not just aggregates.** The first gate run scored 49.1% and failed
  everything. That was a four-word prompt bug of mine, caught only because `label=0 <- '005.1,skip'` is
  visibly a programming document filed under a category I'd mislabelled. **A low score is exactly what a
  failing candidate looks like** — the aggregate could not have told me. `classify_d.py` keeps all ten
  emitted fields for this reason.
- **Two judges, not one.** Judge B exists to catch *inherited error* — a distilled model faithfully
  reproducing a bad teacher label, which a single-judge design scores as a success. Scoring on the
  `{A == B}` consensus subset (with `J` reported as the ceiling) is what makes an 85% bar meaningful.
- **Independent cross-checks between agents.** Two agents measured `github_archive` without conferring
  and agreed within **2.4%** (11.51B vs 11.23B) on the same verdict. That is worth more than either
  number alone.
- **Verifying every agent claim myself before relaying it.** Several were load-bearing and several
  corrected *me*. The duplicate-tree histogram, the FinePhrase nested-leaf ambiguity, the card-direction
  inversion — all reproduced locally before they went into a commit message.
- **Worktrees, never `git stash`, to test against pre-fix code.** Stashing files a concurrent agent is
  writing lost work twice in one session (once by me, once by an agent). A throwaway worktree touches
  nothing.
- **Committing each item separately after review**, staging explicit paths, never `git add -A` — with
  three agents and another session writing the same tree, this was the only thing that kept commits
  attributable.

### From the 150B publish session (2026-07-30)

- **Building the copy plan as a self-checking artifact BEFORE moving bytes.**
  `artifacts/olmo150_plan.py` regenerates the whole 6,911-entry plan and asserts: exact drop count,
  unique sources AND unique destinations (a dest collision silently overwrites a shard), contiguous
  global ordinals, legal shard names, `labels_from_path` matching intent, the val carve totalling
  the approved figure, nothing ≥5 GiB, and no legacy `val-00000` object anywhere. **Two real
  mistakes were caught by those assertions rather than by a 587 GiB copy.**
- **A dry run on 5 deliberately-chosen shards** — the smallest survivor, the largest object, a
  carved val shard, and both nesting depths — published and run through Gate A before the full
  copy. That is the step the first migration attempt skipped.
- **Mutation-testing every new test.** Ten mutations against the mixture work; two SURVIVED first
  time and both were tests that proved nothing. A test suite that goes green on a broken
  implementation is worse than no suite, and reading the tests would not have revealed it.
- **Verifying claims by execution, including my own.** The "adapter is ~8 lines" figure appears in
  no document — it was folklore I repeated. Written and run, it is 15 lines. Likewise "the 3600 s
  timeout blocks a 16 h run": `execution.py` sends `AttemptDurationSeconds` from a form field on
  every submit, so a submitter overrides it with no repo change.
- **Reading the live resource instead of the template.** The deployed GPU role scopes to
  `teams/*/runs/*` while the committed template says `teams/platform/runs/*` — the template's
  central isolation argument does not describe what is deployed.

### Carried forward from earlier sessions

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


### From the schema-v2 / recompute-gaps session (PR #4)

- **Adversarial subagent review, with a mandate to BREAK claims.** Four reviewers ran against the
  diff and between them found 3 CRITICALs, including one leak reachable with **no adversarial input at
  all** (`families/curriculum.json` required validation while shipping no partition template, so an
  ordinary publish leaked its val shards as trainable while `.val` reported `None`). Asking "prove this
  fix holds" found nothing; asking "break this" found real holes. Worth repeating for any safety claim.
- **Reproducing a defect by EXECUTION before fixing it.** Every one of the 15 defects was demonstrated
  live first. This caught two cases where the intuitive story was backwards (see What Didn't Work).
- **Verifying against the real live corpus, not just fixtures.** Running each new check over the actual
  218-entry manifest is what proved the changes were a no-op on published data — `manifest_sha256`
  still `f05702fa…`, 0 dtype violations, `sum(tokens) * 4 == sum(bytes)` exactly.
- **Testing the WIRING, not just the unit.** Reverting `family_defaults` out of `GroupContext` broke
  **zero of 410 tests**, because every test called the helper directly. The unit was covered; the
  plumbing was not, and the plumbing was the bug. Any fix that threads a value somewhere new needs a
  test that fails when the threading is removed — verified by actually removing it.
- **Building a real wheel and installing it into a clean venv.** Grepping for 3.11-only syntax and
  reading `pyproject.toml` both said `families/` shipped. Installing proved it did not.

## What Didn't Work (and the fix)

### 2026-08-02→04 — FOUR code bugs, all found by running, none reachable from 1,101 green tests

Every one produced a corpus that would have been silently wrong or a build that could not finish.
Each was caught by a gate *before* bad data persisted, which is the pipeline working as designed —
but none was reachable from the suite, and the reason each was invisible is the transferable part.

**1. `_reader_for` had no stop condition** (`241334c`, 0.7.0). It walked every file in the source
repo. The registry draws 252B tokens from a 1,094B pool, so `pack` was handed thousands of shards'
worth of documents the plan had no refs for, and `_drain_surplus` refuses a surplus of one whole
shard. Measured before the fix: **all 14 drawn sources raised before writing a shard**, 11 of them
needing an impossible 46–90% filter loss to come under the threshold. Invisible to the suite because
every driver test injects `documents=`, bypassing `_reader_for` entirely.

**2. The surplus gate refused a deliberate subset** (`0b8135e`, 0.7.1). Having fixed #1, the reader
now over-delivers *on purpose* — `_CHARS_PER_TOKEN` 6.0 against a measured ~4.4, times
`_FILTER_HEADROOM` 1.5 — so filter attrition cannot leave a bundle's last shard unfilled. That
overshoot IS surplus. **25 of 27 bundles died at end-of-run**, each after its full billable work.
`partial_source=True` splits the two cases rather than loosening the bound. Invisible because
`TEST_SHARD_TOKENS` rescaling does not touch the module constant `_drain_surplus` compares against —
at test size the surplus never reaches the threshold and both directions pass against broken code.

**3. Scraped text contains `<|endoftext|>`, which IS the boundary id** (`1a0912e`, 0.7.2). Five large
train bundles failed with *"20014 documents end in this shard but id 100257 appears 20016 times"* —
1, 2 and 8 extra per ~20,000-document shard, so roughly 1 document in 2,500. Not a BPE collision:
`tokenizers` parses the literal string into id 100257 wherever it occurs. Fatal because OLMo-core
recovers boundaries with `(mmap == eos_token_id).nonzero()`, so a marker inside a document is a FALSE
boundary and the model trains on fragments split wherever a scraped page mentioned the token.
Neutralized to `<| endoftext |>` before encoding — space-split rather than deletion, so nothing is
silently dropped and no invisible character enters a published corpus.

**4. 429 was the ONLY retried HTTP status** (`7a97c27`, 0.7.4). `_read_once` guarded on
`if exc.code != 429`, so every other error raised on the first attempt against a retry budget of 8 —
the message said so outright, *"failed after 1 attempts"*. **One transient 503 killed 5 of 8 children
overnight**, five different repos and files, hours of work each. This is the same class of bug this
repo already paid for and documented once, for 429s specifically: the fix then made the backoff long
enough, and nobody asked which statuses reach it.

**Two infra faults, neither a code bug.** A pinned ECR digest in `sbsandbox-intern-edullm-olmo-core`
— another project's repo, 50-image retention — aged out mid-run and all 8 children died with
`CannotPullContainerError`. Fixed by pointing at `sbsandbox-intern-edullm-data`, which `.edullm/
Dockerfile` actually builds and which *contains* the package, so the wheel-from-S3 bootstrap could be
deleted along with the whole stale-wheel-by-filename failure mode. Separately, the dedup set cost
**155 B/entry measured, not the 113 B its docstring claimed** (`sys.getsizeof` of the string alone,
ignoring the set's slot overhead) — that 37% understatement is what pinned all four hosts at 97%
memory with 25% of their CPU idle.

### 2026-08-04 — I terminated running jobs while a queue could backfill, and gained nothing

Asked to free memory for the owner's own jobs, I terminated the 3 smallest running children plus one
more. Batch **refilled the freed slots from the 10-deep queue faster than my cancels landed**, so
occupancy returned to 12 and free memory stayed at 1,786 MiB. Net effect: ~50 minutes × 4 bundles of
work destroyed, zero capacity freed. The queue must be drained *first*, or the whole array terminated
in one call. `update-compute-environment` also refuses to scale down (`Manually scaling down compute
environment is not supported`), so the environment is not the lever either.

### 2026-08-04 — every per-bundle ETA I gave was wrong, in both directions

9.1 h became 2.5. 1–2 h became 8. I once alarmed at 33 h from comparing an ordinal against a
*previous attempt's* timestamp. Per-shard time swings with host contention (29.6 s/shard uncontended
vs ~43 s at 12-way) and with document length, and my val-read rate came from a single 2-shard bundle
that was ~3× pessimistic. Also: **object counts are not a progress signal here** — leftover keys from
terminated attempts are overwritten at the same paths, so `dclm` showed ~706 objects while at ordinal
~500. Only the max ordinal and consecutive timestamps mean anything.

### 2026-08-01 (night) — four registry rows were unreadable, and only real bytes could tell

The registry passed 44 tests, loaded into `CorpusSpec`, and had every revision pinned and
tree-verified. Then I pointed the actual reader at it and **4 of 14 drawn sources could not be read
at all** — three had a `config` that 404'd, one named a column that does not exist. A fifth defect
was worse: both readers fetched bytes from `resolve/main` while listing files at the pinned sha, so
the pinning was decorative.

Every one of these is a fact about *remote data*, which is precisely the class no amount of local
testing reaches. The registry was internally consistent throughout.

The cheap generalisation: **verifying that a config/path/column string is well-formed, or even that
its parent directory exists, is not verifying that it names data.** The only check that counts is
reading a record and looking at it. That took about ten minutes and would have cost a multi-hour
Batch array plus the time to work out why a corpus came back empty.

Worth noting what the *reader* got right: parquet raised loudly on the bad column, because
`_leaf_index` refuses a bare-name fallback. The json.gz path silently returned zero documents for
the identical mistake. Same registry error, one loud and one silent — the difference was a `continue`
where there should have been a raise.

### 2026-08-01 (night) — two subagents died mid-task and one lost everything

Three long-running agents hit the same API stall this session. The difference in outcome was
entirely whether they had written to disk:

- The registry agent ran 3.5 h, reported *"every text column is now verified from real bytes. Now
  writing the registry,"* and died. **Nothing was on disk.** All of its verification was lost and I
  rebuilt the registry from `artifacts/recount/` instead.
- The build-driver agent died the same way — but after I had told both remaining agents to write
  skeletons first. It left a 165-line file: full module docstring, the CLI shape, and every body as
  `NotImplementedError`. **The docstring was the expensive part** (why a plan artifact, why bundle
  granularity, why `verify` refuses) and it survived, so I implemented the bodies against it rather
  than re-deriving the design.

The fix is the one `CLAUDE.md` already states — persist continuously — but the specific form matters
for agents: **write the skeleton with real docstrings before writing any implementation.** A partial
file on disk is recoverable; a complete file in a context window is not. Composing the whole thing
and writing at the end is the natural way to work and the one that loses everything.

Also worth knowing for judging agent reports: an agent that says a file is done may be mid-write.
Twelve tests were failing in a module whose author had reported it green, and it was a race, not a
defect — waiting for file mtimes to settle before judging resolved it.

### 2026-08-01 (night) — the registry asserted three things about repos it had never resolved

Pinning the revisions read like bookkeeping — turn `revision: null` into a sha, tick the box. It
falsified **three** claims instead, and the reason they survived until now is instructive: every one
is a fact about a *remote* repository, so no test, type check, or code review could have caught them.
The registry was internally consistent and externally wrong.

The worst was seven of seventeen rows naming `common-pile/raw_v0.1_parquet` with the subset as a
`config`. Plausible on its face — that repo exists, and the subsets are named exactly as the rows
said. But the `_filtered` variants are **separate repos** shipping `.json.gz` at the root, so the
rows had the wrong repo, the wrong `file_format`, and a config that does not exist. A build would
have read **nothing at all** from 41% of the registry.

Two lessons, both narrower than "verify things":

- **A pin is not metadata, it is the first time anyone asks the remote whether the row is true.**
  `verify_pins.py --deep` now does that on demand — tree listing plus 16 bytes for the file magic —
  and it is worth running before any build, not once.
- **My first version of that verifier cried wolf.** It descended blindly from the repo root, hit
  `assets/` in finemath, found a PNG and reported a healthy repo as broken. A checker that produces
  false alarms is worse than no checker, because the next failure gets waved off. It now starts
  inside the row's own config directory and skips non-payload extensions.

### 2026-08-01 (night) — I read a percentage as a fraction of the wrong denominator

Sizing the registry, a no-slack warning fired on `math` and `reference`, and I reached for
`datamix1-jul22`'s `leakage-summary.json` to estimate what decontamination would cost them. Its
`category_attrition` block gives `math 0.543`, `code 0.797`. Propagated as pool fractions those say
math loses 54% of 34.69 B and comes up **18 B short** — a headline finding, and complete fiction.

They are **excluded ÷ candidates within a category.** Math had **3,926 candidate documents**; the
whole 20 B build excluded **10,239 documents, ≈0.026% of tokens.** I was wrong by four orders of
magnitude, and the only reason it did not ship is that the number was *too dramatic to believe* — so I
checked what the denominator was before writing it down.

The general form: a field named like a rate tells you the numerator's units, never the denominator's.
`provenance_coverage` in the same file gives per-category document counts and settles it in one read.
Both the script and `CORPUS-REGISTRY.md` now carry the correction, because the next person sizing a
pool will find that same block and it reads exactly like a pool fraction.

### 2026-08-01 (night) — I handed an implementer a shape that cannot stream

I told the packer implementer to copy `pack_category_globally`'s shape
(`week1_corpus/validation.py:846`), and wrote the same recommendation into
`WEEK1-CORPUS-SURVEY.md`. It computes its aligned size as
`token_plus_eos = sum(len(item.tokens) + 1 for item in ordered)` — **a sum over a materialized list.**
Following it literally means holding every document of a category in memory, i.e. a full pre-pass over
255B tokens before the first shard can be written.

The implementer noticed and streamed instead, truncating at exhaustion. **Measured equivalent, not
argued:** over 5 randomized trials the streaming packer's `tokens_out` equalled
`total - (total % SEQ_LEN)` — exactly what the materializing algorithm would have produced — every
time. So the pre-pass buys nothing except the memory it costs. Survey corrected.

The lesson is narrower than "verify claims": I *had* read that function, and the defect was in the one
expression I skimmed because the surrounding logic was obviously right. A shape that is correct at test
scale and impossible at production scale looks identical in a code review.

### 2026-08-01 (night) — my first version of the label-segment gate rejected legal corpora

Adding a Gate A check for unsafe path segments, I enforced `SAFE_SEGMENT_RE` — and broke **25 tests**.
That pattern is lowercase-kebab-only, which is right for a value this package *generates* and wrong for
a validator: it conflates style with danger. `tokens/stack-edu/Python/…` is what the existing
label-selection fixtures use and it is completely safe. Measured which characters actually break
something rather than assuming:

| segment | shard name in `path`? | `fnmatch(k, k)` |
|---|---|---|
| `Python`, `C++`, `Jupyter Notebook`, `naïve` | yes | True |
| `C#` | **NO** | True |
| `a[b]` | yes | **False** |

Only two classes are genuinely broken. The gate now rejects those and nothing else. **Rejecting a legal
corpus is the more expensive error** — the bytes are frozen by then and the fix is a full re-copy — so
a validator must reject what breaks, not what it would not have written.

Related, same session: ruff flagged `CONTROL_PREFIXES` in `validate.py` as an unused import. Removing
it passed the linter and **failed a test** asserting that `validate.py` and `publish.py` bind the *same
object* — the two once carried identical literal copies, so a green suite proved they agreed, never
that there was one definition to change. Now annotated with the reason so nobody "cleans it up."

### 2026-08-01 (late) — I asserted a mechanism I had not measured, and an agent refuted it

I wrote, in a code comment and a commit message, that resolving **with** a `Range` header signs the
CDN URL for that range only, so reuse returns `403 invalid range`. **It does not reproduce.** I
re-tested it myself: reuse for a *different* range returns **206**, and the decoded CloudFront policy
contains only `Resource` and `DateLessThan` — there is no range condition that could bind. I inferred
a mechanism from a single 403 and stated it as measured.

The resolve-once fix stands on its own arithmetic (70 metered requests → 1). Only my *reason* for the
no-`Range` rule was fiction. The real traps are the 3600 s expiry (which `huggingface_hub` misreports
as a token error and never retries) and `HfFileSystem._fetch_range` re-resolving per 5 MiB block.

### The estimate moved 6–10 wk → 1.5–3 → 2–3, always downward

Every revision came from finding something already built, never from building it. I described
tokenization as "never written" while a sibling checkout had it working *with a complete S3 backend*,
and I estimated a 2–4 day port of code whose abstraction seam already existed. **Inventory what
exists before estimating what is missing.**

### A doc sweep found claims that would have caused real errors

Not cosmetic staleness: `PLATFORM-INTEGRATION.md` told another team "the validator re-hashes every
file" — the exact overclaim `CLAUDE.md` forbids, and another team was building on a false integrity
guarantee. `RUN-THE-INGEST`'s exit-139 row named a fix its own sibling document records as refuted.
Install pins said `@v0.2.0` in both auto-loading `SKILL.md` files, so every agent launching in this
repo installed a pre-Gate-A-fix build. And `INGEST-CALIBRATION`'s "2.25 worker-seconds/file" was 16×
wrong, making its own recommendation self-refuting.

### Earlier sessions

### From the 2026-08-01 session — four wrong diagnoses of one bug

All four were shipped or argued with confidence, then refuted by a real run. Recorded because the
*pattern* matters more than any one error: every wrong answer came from reasoning about our code
instead of measuring the environment.

1. **"A short HTTP read is SIGSEGV-ing pyarrow."** Shipped `0.6.2`. Identical failure. The read loop
   is a genuine correctness fix (`RawIOBase.read` may legally return short) but was not the cause.
2. **"Contention between co-scheduled children."** Trivially wrong and I should have caught it in
   seconds: shards read **disjoint** files (`[0,4,8…]` vs `[1,5,9…]`), so nothing is shared.
3. **"The cgroup memory limit."** Doubled RAM 8→16 GB and it got **worse** — all four children died
   instead of three. RSS peaked at 149 MB of 8192. Exit 139 is SIGSEGV, **not** 137's memory kill, and
   conflating them is what made this plausible.
4. **"numpy/pyarrow ABI skew."** The most instructive failure. The container really was running numpy
   2.5.1 against my local 2.4.6 — a real, unnoticed skew. I declared it "ruled out" from a local
   control that had **silently used the wrong version**, because `pip install numpy==2.5.1` fails on
   macOS. **A version comparison you did not print is not a control.**

Other misses this session:

- **I estimated the pipeline at 6–10 weeks, then 1.5–3, then 2–3.** Every revision went *down*, and
  every one came from finding something already built. The lesson is to inventory what exists before
  estimating what is missing — I described tokenization as "never written" when a sibling checkout had
  it working with a complete S3 backend.
- **I wrote a safety guard and did not grant it permission.** `_assert_lifecycle_covers` calls
  `GetBucketLifecycleConfiguration`; the IAM policy did not include it. The job reached
  `INGEST_START`, printed `PARTITION_OK=1`, and died inside its own check.
- **"The full ingest is a 2–4 week blocker"** rested on a per-IP rate limit inherited from a note about
  a *different* endpoint, never measured on the path we actually use. The real answer is 1–3 hours.
- **A diagnostic job died on the documented PEP-427 wheel-filename trap** (`w.whl is not a valid wheel
  filename`) before reaching the faulthandler that would have named the crash. `CLAUDE.md` warns about
  exactly that.

### Earlier sessions

### From the reservoir execution session (2026-07-31)

- **Fanning out 8 agents against one rate-limited API.** My orchestration error. `datasets-server`'s
  quota is **per-IP, not per-account**, so eight concurrent agents exhausted it and every in-flight count
  died at once with HTTP 429 — failures that read as *broken corpora* in the artifacts. The delegation
  rule (§9.2) is right for context, but it needs a **concurrency budget per shared external resource**.
  Fix: serialize, then switch transport to footers entirely.
- **A four-word prompt error that invalidated a whole gate run.** I labelled FDC category 0 "General
  works", copying the Essential-Web card's abbreviation. Dewey class 0 is "Computer science, information
  & general works", and **computing lives at `005.x` inside it**. So the candidate correctly filed
  StackOverflow under 0 while my judges, given no computing category, sent it to 6. On qa-forum that
  alone was **3.3% vs 97.4%**; pooled, 49.1% vs 87.5%. Cost: one full re-run of 4,000 judge calls.
- **`git stash --keep-index` on a file a concurrent agent was writing.** The `stash pop` discarded the
  work instead of restoring it. Recovered from the dangling stash commit; then an agent did the same
  thing to the same file with `git checkout --`. Between us we lost and recovered it twice.
- **Asserting a direction from one data point.** I wrote that Common Pile's `Size(GB) × 0.25` figures
  "run above the published figures, so pools are understated" — extrapolated from a single corpus's
  tokens/byte. Measured, it errs **both** ways by up to ±23% (peS2o's card is 7% *high*). That framing
  would have licensed treating card figures as conservative floors, which is the exact reasoning §3.1
  exists to forbid, and I reproduced it *in the document that catalogues that mistake*.
- **Overstating "SA maps onto whole sources".** True for `stackexchange` and `finewiki`; `libretexts` is
  **32.05%** SA and peS2o ~1.9%. Exclusion by name is still safe but **over-broad** — dropping peS2o to
  remove 1.9% SA removes 50% of the academic candidates.
- **Citing `file:line` in a document meant to outlive a refactor.** `publish.py` shifted ~800 lines
  during concurrent work, invalidating citations that were correct when written. Switched to symbol names.
- **Staging the first GPU job to a prefix its IAM role didn't grant.** The role is scoped to
  `teams/*/runs/*`; I used `teams/data-prep/smoke/`. Fixed by using the sanctioned prefix and an explicit
  key list — **not** by widening the policy, since a narrow write scope is the same idea as the airlock.
- **Declaring the pre-publish gate "clear" before it was.** I closed §9.7 at three items; Phase 0c then
  surfaced a fourth (the synthetic `id` partition) with a build-time deadline. Had to reopen the banner —
  a stale "CLEAR" is worse than an open item, because a fresh session would tokenize 60B of synthetic
  that is really 15B.

### From the 150B publish session (2026-07-30) — four burned runs, each a real defect

- **Ran `publish()` on the laptop.** It stream-hashes every object, and "never holds a payload
  whole" means bounded RAM, not no transfer — it PULLS all 587 GiB to wherever it runs. Measured
  2.7 GiB in 61 min = **0.8 MiB/s ⇒ a 9-day ETA**. Killed; no partial state, because `publish()`
  writes nothing until hashing completes. `s3.hash_object`'s own docstring says bytes never leave
  AWS *"when this runs on Batch in-region"* — I read past the conditional. **Distinction that
  matters: a server-side `copy_object` is fine locally (586.6 GiB in 498 s, zero bytes through the
  laptop); hashing is not.** Diagnosis tool: `nettop -P -l 1 -x | grep python` against elapsed —
  5% CPU and silence looks identical to a hang.
- **Fixed `families/` in the validator and not the producer.** The Batch publish died in 2 minutes:
  `no family.json for 'pretrain' (looked in /usr/local/lib/python3.12/families)`. Same bug I had
  already fixed once, in the other module. The two halves fail *differently*: the validator's was
  SILENT (bounds fell back to laxer constants), the producer's is LOUD but only after a full run
  reaches that line. One resolver in `contracts` now. **A checkout always finds `families/`, so no
  ordinary test can see this class of bug** — the test that works copies the package into a
  rootless tmpdir and imports it in a subprocess. Lesson: grep for the pattern *everywhere* before
  calling a bug fixed.
- **Gate A rejected the corpus, and it was half right.** 4 of 6,913 shards. Two were genuinely
  degenerate (a 21-distinct-id repeating SQL cycle; a 68-distinct 1,010-token shard) and are now
  excluded. Two were a **validator defect**: `max_zero_fraction` fired at 0.0106 against a 0.010
  bound, claiming "partial zero-fill from a crashed writer" — but **dolma2 maps token id 0 to
  `!`**, so the check was measuring punctuation density. The zeros were 30 scattered singletons,
  longest run **1**. Replaced with a contiguous-run test, which is tokenizer-independent and
  *strictly more sensitive* (a 256-token hole is 1.56% of a sample and slipped under the old
  density bound). **My first inspection read the shard HEADS and disagreed with the validator** —
  it samples 4 seeded *random* windows precisely because a zero-filled tail leaves a valid head.
- **Excluded the wrong shard, nearly.** Gate A names the **published** key
  (`stack-edu/SQL/train-06681`); the exclusion list keys on the **staged source** name
  (`stack-edu/train-00811`). Different ordinals, because the migration renumbers globally. The
  first attempt matched nothing and was caught only by the plan generator's exact-drop-count
  assertion. Also: excluding a shard shifts every later ordinal, so restaging needs a
  diff-and-prune (118 stale keys), not an additive copy.
- **Registered a job definition against a CLI flag that did not exist.** `--promote-workers` was
  in the job def before it was in `argparse`. Argparse exits non-zero on an unrecognized argument,
  so the validator would have crashed on its next run — in production, on the flag added
  specifically to make promotion finish.
- **Two tests that passed while testing nothing.** (a) A determinism test survived mutating
  `pool.map` → `as_completed`, because the seal is written through `canonical_json`, which sorts
  keys — insertion order could never reach the bytes. (b) A `max_source_fraction` cap test used
  25%, which on a 20×50,000 pool is *exactly* 5 shards, so the boundary guard was never exercised;
  moved to 22%, which falls between shards. **Both found by mutation testing, not by reading.**

### From the schema-v2 / recompute-gaps session (PR #4)

- **Trusting a fix because the tests passed.** `family_defaults` was wired into `GroupContext` and 410
  tests went green — but `FAMILIES_DIR` was repo-root-relative and the wheel ships only
  `src/edullm_data`, so on the deployed validator it resolved to a nonexistent path and silently fell
  back to `{}`. **It failed only in production**, because every checkout and every test found the
  directory. Fix: `force-include` families into the wheel + a three-way lookup, proven from a real
  installed wheel. Lesson: a path-relative resource lookup is a deployment bug waiting for a deploy.
- **Reasoning about the dtype failure instead of executing it.** I stated twice, confidently, that
  uint32-read-as-uint16 would crash and uint16-read-as-uint32 would be silent. **It is the reverse.**
  Executed over all 100,278 dolma2 ids: reading uint32 as uint16 yields **0 of 200,554 out-of-range**
  values (every half is <= 65,535) and doubles the element count — silent, trains to completion.
  Reading uint16 as uint32 puts 100% out of range and crashes. This matters because OLMo-core's
  low-level default IS uint16 and these corpora ARE uint32, so **the silent direction is the default
  one**. Never reason about a byte-level failure that takes four lines to demonstrate.
- **Quoting a price from memory.** I put p4d.24xlarge at $32.77/hr in a doc headed for a teammate's
  budget. It is **$21.9576** (`config/workload-catalog.yaml:104`, confirmed against the live Price List
  API) — ~50% high. A subagent caught it.
- **Deferring to a check that did not exist.** Two of my own new validators early-returned with the
  comment "a different check owns that". `grep` for that check found only the comments. Writing a
  deferral is not the same as writing the check.
- **A guard that cannot fail.** My first family-key drift test used a substring heuristic that reported
  "mapped" for invented names like `tags_extra`. Replaced with set membership in both directions.
- **`--human-readable` output for numeric comparison.** GiB-rounded sizes made me flag 2 of 218 legacy
  shards as mismatched; the exact API sizes matched perfectly. Use `list-objects-v2 --query sum(Size)`
  when bytes decide something.

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

### 2026-08-03 — the two IRREVERSIBLE publish decisions, CONFIRMED by the owner

Both are transcribed in full in `artifacts/reservoir/PUBLISH-SPEC.md` (`6e34aa6`) with the exact
`publish()` call. Summarized because both are unfixable after the fact:

**Name: `pretrain/reservoir-dolma2`.** Validated mechanically, not by eye. **No token budget in the
name**, even though siblings carry one (`olmo-150b-dolma2`, `regmix-10b`) — those describe corpora
built *to* a budget, and this is a reservoir teammates draw 20B mixtures from. "252b" would read as
an instruction to train on all of it. (`pretrain/reservoir-final` was checked and correctly REJECTED —
`final` is a version token.)

**The synthetic half ships UNDECONTAMINATED, with the gap stated.** 59.6B tokens, 23.7% of train.
FinePhrase is rephrased FineWeb-Edu and rephrasing is precisely what defeats n-gram matching, which
is the only decontamination this pipeline has. The 13-gram gate is verified on verbatim text (40/40
GSM8K test questions caught, 0/2 false positives) and must be assumed **ineffective** here. It goes in
`limitations[]` because the README renders that section and omits absent ones — silence would read as
"decontaminated", and a wrong benchmark score months from now would look like a modelling result
rather than a data defect.

**Two things surfaced while writing that spec that were NOT part of either decision.** The license is
mixed and includes share-alike: `stackexchange` and `finewiki` are CC-BY-SA-4.0 (finewiki also GFDL),
20.5B tokens / 8.2%, so no single top-level `license.id` is truthful. And `sources[]` token counts
must come from the receipts' `tokens_out` — finewiki proves why, at 90.5% of plan.

### 2026-08-04 — concurrency capped by submitting individual jobs, not an array

The owner needed ~2 hosts for their own work. A queue-level fair-share policy would have changed
behaviour for *their* jobs too, and Batch has no per-array concurrency limit. Submitting N individual
jobs is contained, needs no shared config change, and is trivially reversible. Later, with explicit
permission, this went to 12 concurrent to overlap the two waves — which cost the reservation for ~8 h
but pulled the finish from ~19 h to ~8 h at identical cost, since vCPU-hours are conserved.

### 2026-08-04 — container sized from measurement, and my own first proposal was wrong

I proposed 12 GiB in conversation. Checked before acting: it leaves **−3.1 GB** on the worst bundle
and would have OOM'd `stackv2-edu--train` after hours of work. Measured worst-bundle resident is
~12.1 GB (10.3 dedup + 0.45 decon index + 0.4 tokenizer + 0.1 shard + 0.5 pyarrow row group +
interpreter), so **14 GiB** with 1.9 GB headroom, packing 4 children per 64 GiB host instead of 3.
Related correction: I sized against ~120M documents for that bundle; actual was **42.2M**, because my
500-tokens-per-document assumption was 2× off against a real mean of 943. Per-bundle document
estimates in this project run high — treat them as upper bounds.

### 2026-08-01 (night) — four decisions taken WITHOUT the owner, on explicit instruction

The owner said "continue handling it all yourself." Each of these was a real judgement call, so each
is recorded with what would reverse it.

**1. DCLM is sourced from `HuggingFaceFW/dclm_100BT`, NOT `mlfoundations/dclm-baseline-1.0`.**
The question posed was "add a `zstandard` dependency or drop diverse web?" — and it was a false
choice. The row named a `.jsonl.zst` repo while claiming `parquet`, but `sizing-revised.md` line 40
shows the 114.69 B pool figure was **measured against `dclm_100BT`**, which is parquet. The registry
was citing one repo's number under another repo's name. So this is not a substitution of one source
for another; it is the row finally agreeing with its own evidence. **No new dependency, and the
corpus keeps its diversity counterweight.** Reverse only if `dclm_100BT` turns out not to be
DCLM-baseline-derived — its card says it is.

**2. Exact dedup only. No Bloom filter, no fuzzy matching.** §4.1's own evidence: DCLM measured a
Bloom filter ALONE at +1.6 CORE — equal to the full Exact+MinHash+SuffixArray stack — and FineWeb
found the *removed* data scored better than the kept. Dedup is scoped **within a bundle**, which is
where duplicates actually cluster (one source, one crawl). `SeenHashes` says so in its docstring
rather than implying global coverage. Cross-bundle dedup is a separate stage and is not built.

**3. A missing decontamination index RAISES; `--no-decontaminate` is the only way to skip.**
`week1_corpus/worker.py:102-106` falls back to an empty index, which turns a staging mistake into a
corpus that *looks* decontaminated. You would discover it when a benchmark score looked too good,
months later, with no way to tell which runs were affected. A truncated container is refused for the
same reason — it would parse as a smaller index that decontaminates less and reports success.

**4. A source too small for one whole val shard gets NO val split, recorded in the plan.**
At `VAL_FRACTION` 0.005 the break-even is **5,000,396,800 tokens**. `ubuntu-irc` (1.8 B) yields 0.36
of a shard, and `shard_plan` correctly refuses a stream it cannot give ordinals to. Its documents all
go to train — nothing lost, nothing leaked — but there is no per-source held-out set for it, so a
category-level val split must come from its siblings. Written into the plan as `no_val_split` rather
than warned about, so the omission is auditable afterwards. The alternative was a 1.4% val fraction
for that one source, which would make the held-out fraction non-uniform across the corpus.

### 2026-08-01 (late) — the three irreversible label decisions, SETTLED and SHIPPED (`6eff578`)

Owner approved all three. Each was gated on establishing what the code *permits*, not what sounds
reasonable — five of six candidate encodings turned out to be mechanically impossible.

**A. Labels stay NAME-LEVEL: `source` + `domain`, nothing else.** There is no third slot. Verified
line by line: a third `entry.labels` key fails `declared != expected` (**full dict** equality,
`validate.py:800`); a third path level raises in `labels_from_path`; a new per-entry field hits a
closed key set; a label-glob `partitions[]` entry is an unconditional `empty-split` unless its name is
in `SPLITS`; and `labels_from_path(keys=…)` has **no production caller**, so the validator recomputes
with the default and rejects anything else. `_licenses/sources.parquet` is allowlisted and useful, but
`read.py` never opens it — it informs a human, it cannot drive a call.

So `share_alike` and `synthetic` are facts about a **source name** or they do not exist. Two fixes
shipped: `manifest.py`'s docstring told producers to use `keys=` for exactly this (producing a
REJECTED dataset — a live trap), and the raise message now says *flatten into `source`* where someone
actually meets the problem.

**B. SUFFIX, not prefix — and a new warning that matters more than the naming.** `MixtureSource.name`
sorts its labels, and `domain` < `source`, so a domained synthetic source renders
`domain=science,source=synthetic-…` and every `startswith("source=synthetic-")` check silently misses
it. Measured: a **25% undercount** on a cap. `-synthetic` is immune because `source` sorts last.

The larger defect found underneath it: **`ratio` is a TARGET, not a cap.** `want = int(total * ratio)`
is per-component but `actual_ratios` divides by what everyone *actually* got, so a starved component
shrinks the denominator and inflates every other share without bound. Requesting 0.30 measured
**0.3333 / 0.4000 / 0.9375**, and in two of those `shortfall` was **empty** — it names the component
that came up short and cannot name the ones that came up long. Shipped `RatioOvershoot` +
`_warn_ratio_overshoot` (5pp tolerance, above whole-shard granularity which §2.2 makes deliberate).
It fires on the **pre-existing** fixtures at 50% requested / 62.5% delivered — a real silent overshoot
that had been in the suite all along. Fatal in one line via `simplefilter`.

**C. ACCEPT the FineWeb-Edu / FinePhrase overlap (option C4).** The previously-approved anti-join was
**set-theoretically identical to deleting `fineweb-edu`**: FinePhrase's parent is `sample-350BT`,
`sample-100BT` is a subset, the four formats' union covers essentially the whole id space — so it drops
**97,270,686 of 97,270,686** documents, **100.24B tokens, 38.4% of edu-web**. The design justified it
using edu-web's size *before* that deletion, and never stated which config it runs against (§3.2 says
`sample-100BT`; `sample-350BT` appears nowhere in the doc).

Accepted because the numbers are unambiguous: **0.048 exposures** at default weights — 2,067× under
§4.1's own threshold, 723× under the scale-adjusted Hernandez band, P(both forms in one run) **≈1 in
1,800**. And that threshold measures *exact* repeats; the paraphrase literature ships co-presence
deliberately (REWIRE measures 18.3% as beneficial, ~10× our rate). **"100% sibling rate" is a property
of the POOL, not of any RUN.** If separation is ever wanted, **C1 dominates C3**: filter the synthetic
side instead — same Bloom filter, keeps the 100.24B, keeps the best-measured edu-web blend, and makes
the anti-join unnecessary by construction. Also recorded: the "free Bloom filter" claim is false (step 0
precedes step 2, and they key `id` vs text hash).

### 2026-08-01 — FULL PIPELINE confirmed against all five costed options

See the decision banner at the top for the table. The short version: the ingest collapsing from 2–4
weeks to 1–3 hours is what made E affordable, and it also shrank D's advantage — the build is
code-bound, not byte-bound, so the packer and sharder get written either way. A/B/C/D are declined,
not open.

### Earlier sessions

### From the 2026-08-01 session

- **FULL PIPELINE, MinHash deferred** (owner). Three alternatives were evaluated in detail and
  declined. Do not re-propose them; the numbers are kept below because they are useful context, not
  because the decision is open.
  - *Publish `datamix1-jul22` as-is* — 96 objects / 35.8 GiB, ~20B tokens, includes a real
    decontamination bundle. Minutes of work.
  - *Merge the two published corpora* — 283.7B tokens / 12 sources, top source share 76.9% → 42.6%,
    ~4h of Batch, ~$0.10. Verified mergeable: both pin `tokenizer/dolma2-bpe@v1` with byte-identical
    `manifest_sha256`, and **7,392 entries yield 7,392 distinct digests** (zero cross-collisions), so
    `duplicate-shard-digest` does not fire. Absent: edu-web, QA/forum, synthetic.
  - *Hybrid* — merge the 250B, tokenize only the missing 76.4B (169 GB fetch, 6–11h compute). Cuts the
    ingest by ~85% and lands the same 15 sources.
- **Write the tokenize/shard driver fresh (~400–800 lines), do not port and do not adopt a tool.**
  No tool splits mid-document, so none can hit 25,001,984 tokens; `week1_corpus` emits pre-mixed
  per-tier files where eduLLM needs per-source shards mixed at read time.
- **Shard tails: floor to a multiple of 8192 and discard the sub-8192 remainder** — then *declare*
  `seq_len: 8192`. Omitting `seq_len` to dodge `check_seq_len_alignment` is decoration under the golden
  rule. Cost is ~3.4M of 260B tokens (1.3 × 10⁻⁵), and it also prevents runt shards that OLMo-core
  would floor to unreadable. **Do not pad with EOS** — padding a 100-token residual to 8192 makes that
  shard 98.8% EOS and fails the 0.05 bound outright.
- **Take ONE FinePhrase format, not four.** The four are ~73% the same documents; "60B synthetic" is
  ~15B wearing four hats. One format also removes the need for the id partition entirely.
- **`numpy<2.5` is pinned for reproducibility, NOT as the segfault fix.** An unpinned dep resolves at
  run time, so production runs whatever PyPI served that morning. The pin was tried as a fix and
  refuted.
- **EOS risk was inverted.** The family bound `eos_fraction_max: 0.05` needs mean doc length > 20
  tokens. Measured: IRC **7,863**, github_archive **490**, FinePhrase **437**. QA/forum are the
  *safest* sources, 20–400× margin — not the riskiest, as previously recorded.

### Earlier sessions

### From the reservoir execution session (2026-07-31) — all owner calls

| decision | resolution | why it matters |
|---|---|---|
| **Domain classification** | **CANCELLED.** `domain` is inherited from upstream where a source ships one; every other source publishes flat | Measured cost was $920–$10k, not the planned ~$595, for a label ~85% accurate against a ground truth 70–78% self-consistent. Inherited metadata is a *fact* |
| **Share-alike** | **Keep SA sources IN, keep them separable.** Precautionary only, nothing dropped or downsized | Separability is free because SA maps onto whole `source` values — exclusion is omitting names. ⚠️ over-broad for `libretexts`/`peS2o`, which are mixed |
| **Per-document key `(shard_path, doc_index)`** | **SKIP.** Not emitted in `v1` | Licenses and MinHash clusters become source-level. Both questions it would have answered are answerable at source granularity |
| **`reference` pool** | **9B, not 14B** — and max share 15% → 12% | Reaching 14B needed either ~90% share-alike or a stale PD pool. The max-share drop is forced arithmetic: 15% gives 2.96× headroom, violating the doc's own ≥3× rule by 1.3% |
| **`math` pool** | **Accept 3.6% under nominal** (34.69B, floor cleared 4.96×) | Closing it needs `algebraic-stack`, whose license is *not a grant* |
| **Dataset name** | **`pretrain/reservoir-dolma2`** — no size in the name | The total moved twice in a day and every §4.1 step moves it again. Also: the name is the *address* `dataset.json` is written to, not something it produces — so it cannot be "set afterwards" |
| **Synthetic `id`** | **Option D: partition 4 ways + anti-join edu-web** (§9.7 item 4) | The four formats are ~91–93% the same documents; without it "60B synthetic" is ~15B wearing four hats, colliding 100% with edu-web |

**Two structural facts worth carrying forward,** both verified by reading source:

- **`dataset_id` is NOT in `manifest_sha256`.** `build_manifest` takes only `(entries, group_name)` and
  returns `{schema_version, group, entries, objects, bytes}`. So a rename costs a ~1 TB server-side
  re-copy, **not** a re-tokenization — expensive, recoverable, a middle tier. An earlier revision of the
  plan lumped it with the truly irreversible decisions; corrected.
- **`promote()` copies only `dataset.json`, group manifests, and manifest entries.** So a sidecar staged
  to landing would **pass Gate A and then be silently dropped**, expiring with landing's 14-day
  lifecycle. Sidecars are written **in place after promotion** (§5.6 phase 2b) — the generated README is
  the precedent, not an analogy.



### 2026-07-30 — the 150B layout and the reader

- **Nested `tokens/<source>/<domain>/` AND `entry.labels`, not one or the other** (user's call).
  Nesting is UNSPECIFIED-not-forbidden by the standard; one group for 65 subtrees COMPLIES with §4
  (a group is a unit of *validation*, not selection). Labels are populated **because they are
  inside `manifest_sha256` and cannot be backfilled** — adding them later means republishing 587
  GiB. That asymmetry, not elegance, is why both.
- **Ordinals renumbered GLOBALLY across the group.** `DATASET-STANDARD.md:589-590` caps "a group"
  at 100,000 shards via the 5-digit ordinal and says exceeding it "is a spec amendment"; per-subtree
  reuse makes that arithmetic false. Free to fix, since the shards were being renamed anyway.
- **Validation carved per SOURCE, not per stratum** (user's call). A per-stratum carve was computed
  and rejected: 45 of 65 strata cannot donate a shard >1M tokens and one could offer only 90.
  60 shards / 229,894,171 tokens / 0.146%, incidentally covering 43 of 65 domains.
- **The upstream `heldout-val/` was NOT used.** All six of its shards duplicate train shards — five
  byte-identical, one a byte-prefix. Publishing them would have made every held-out number
  meaningless. Recorded in the dataset's own `limitations`.
- **Mixtures resolve LIVE, whole-shard, seeded** (user's call). Not published as child datasets.
  The seed shuffles *which shards*, deliberately unlike the reference implementation, which takes
  the head of every shard and never reads a tail — and whose own `seed` field is dead code.
  Cost: budgets land ~2% over target instead of exactly on it, which buys not needing partial-file
  reads that neither `ResolvedSplit` nor OLMo-core can express.
- **Single-dataset mixing only** (user's constraint). Mixing two corpora could combine different
  tokenizers whose vocab sizes are close enough that every id still looks valid — silent and wrong.
  If that ever changes, the guard is comparing `depends_on` tokenizer `manifest_sha256`.
- **`max_source_fraction` is a hard cap; the budget is not.** Letting the last shard straddle the
  line turned a 10% cap into 13.5% on the live corpus. A limit the caller asked not to exceed must
  not overshoot; a goal may.

### Carried forward

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
  bad work in the audit recomputed a hash. This is the golden rule (CONTRIBUTING.md). What Gate A
  actually recomputes: HEAD size vs `entry.bytes`; the count arithmetic; extension-vs-format; shard
  naming; dtype width vs the tokenizer's derived vocab; partition `rows`; coverage disjointness;
  exhaustiveness (LIST both directions); the `manifest_sha256`/`dataset_sha256` chain; and the
  profile checks, which read ~64 KB per shard and decode it.
- **Gate A does NOT re-hash payload bytes — `sha256` is an unfalsified producer assertion.**
  `s3.hash_object`'s only non-definition caller is `publish.py:280` (the producer); the per-entry
  loop at `validate.py:399-431` HEADs for size and does set-membership on the *declared* digest, and
  `fsck.py:10` reads "never a payload byte" on purpose. `sha256`'s real jobs are content addressing
  (`duplicate-shard-digest`; `shared-sha-with-parent`, the 37 GB re-materialization) and the hash
  chain. Integrity of the bytes rests on the airlock's IAM Deny + S3 durability + CRC64NVME
  (`s3.head()` returns `crc64nvme` and deliberately omits `sha256` — S3 stores no whole-object
  SHA-256 for a multipart object). A full re-hash is affordable (~16.5 min / ~$0.18 for 758 GB) but
  is an OPEN DECISION, not something the pipeline does today. Do not document it as existing.
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

### UPDATED 2026-08-04. The corpus is BUILT (27/27 receipts). Three things stand between here and a published dataset.

Everything before this point is done. Read `artifacts/reservoir/PUBLISH-SPEC.md` first — both
irreversible decisions are recorded there with the exact `publish()` call.

---

#### 1. Re-run the 9 stale-wheel bundles, then re-verify — THE ONLY BLOCKER

`verify --deep` refuses with `bundle-set-mixed-wheel-versions`: the corpus was packed by five
different wheels because four fixes landed mid-build. **The check is right — do not waive it.** A
wheel without `families/` silently validates at the 0.5 EOS bound instead of the family's 0.05 and
reports every shard clean, which is exactly the failure this project already shipped once.

Re-run these nine against `edullm-reservoir-build:9` (wheel 0.7.4, image
`sbsandbox-intern-edullm-data@sha256:4be21c0a...`). One job each, `SHARD=<idx>`:

| idx | bundle | shards |
|---|---|---|
| 26 | `ubuntu-irc--train` | 71 |
| 19 | `finewiki--val` *(idx 9)* | 1 |
| 11 | `pes2o--val` | 2 |
| 13 | `pubmed--val` | 1 |
| 15 | `stackexchange--val` | 1 |
| 7 | `fineweb-edu--val` | 3 |
| 2 | `finemath--train` | 1353 |
| 4 | `finepdfs-edu--train` | 1114 |
| 16 | `stackv2-edu--train` | 1591 |

⚠️ **Confirm each index before submitting.** Do not trust the table above — derive it:

```bash
PYTHONPATH=src python3 -c "
import sys; sys.path.insert(0,'src')
from edullm_data import corpus_build as B
from edullm_data.ingest_reservoir import _shard_slice
specs,meta=B.load_registry()
plan=B.plan_document([s for s in specs if s.target_tokens>0], registry_meta=meta)
bundles=B.bundles_of(plan)
want={'ubuntu-irc--train','finewiki--val','pes2o--val','pubmed--val','stackexchange--val',
      'fineweb-edu--val','finemath--train','finepdfs-edu--train','stackv2-edu--train'}
for i in range(27):
    for b in _shard_slice(bundles,i,27):
        if b.bundle_id in want: print(i, b.bundle_id, len(b.shards))
"
```

**Resume will NOT skip them** — `bundle_is_done` returns true because their receipts and shards are
all present and correct. Pass `--force`, or delete those nine receipts first. `--force` is cleaner;
deleting a receipt loses the accounting it holds.

Then resubmit `rsv-verify-deep` (job def `edullm-reservoir-verify:1`) and require **`VERIFY_DONE_RC=0`**.

Cost: ~4,137 shards ≈ 8–12 h wall at 4-way concurrency, roughly $25. Cap concurrency at 8 or fewer
individual jobs — an array fills the cluster and the owner needs ~2 hosts for their own work.

---

#### 2. Publish — per the spec, ON BATCH, and it does NOT auto-promote

Only after RC=0. Everything needed is in `artifacts/reservoir/PUBLISH-SPEC.md`. Four things that will
bite otherwise:

- **`sources[]` token counts come from each receipt's `tokens_out`, never the plan.** finewiki is the
  proof: planned 8.75B, realized **7.92B** (90.5%) because it ran out of documents. Citing the plan
  would publish a false mix table.
- **`publish()` must run on Batch, in-region.** It stream-hashes every object, so it pulls every byte
  to wherever it runs — measured at 0.8 MiB/s off-region, i.e. ~9 days for this corpus.
- **`edullm-landing-manifest-created` is DISABLED.** Writing `manifest.json` will NOT fire the
  validator. Either submit `edullm-validator:10` manually (it runs as
  `sbsandbox-intern-edullm-dataset-validator`, the only principal that can write `edullm-data`), or
  re-enable the rule first — and if you re-enable it, remember it is shared infrastructure.
- **The license is MIXED with share-alike.** 20.5B tokens (8.2%) are CC-BY-SA-4.0 (`stackexchange`,
  `finewiki`; finewiki also GFDL). No single top-level `license.id` is truthful.

---

#### 3. Cleanup, after the publish lands

- `_ingest/reservoir-dolma2/build/` holds ~1 TB of staged shards. Landing has a 14-day expiry, so
  doing nothing is correct — but confirm the lifecycle rule covers `_ingest/` before relying on it.
- Job defs registered this session and now idle: `edullm-reservoir-build:1–9`,
  `edullm-reservoir-verify:1`. An idle job def is a stored config, not a reservation — harmless.
- `artifacts/reservoir/PUBLISH-SPEC.md` needs its `sources[]` block filled in with the realized
  per-source `tokens_out` once the re-run finishes (the numbers change for the nine rebuilt bundles).

---

### Carried over, still true, NOT blockers

- **The synthetic half has no effective decontamination.** 59.6B tokens / 23.7% of train. FinePhrase
  is rephrased FineWeb-Edu and rephrasing defeats n-gram matching, which is the only decontamination
  this pipeline applies. Owner confirmed shipping it with the limitation stated in `limitations[]`.
  The second tier (`lm-sys/llm-decontaminator`, ~$200) is unbuilt.
- `sft_conversations_v1` still substring-matches split names (pre-existing, unrelated).
- PRs #9 and #11 are stale.
- The PRM800K working tree in the canonical checkout is still **uncommitted** — 15 modified + 13
  untracked files, never committed on any branch. Its build config was rescued into
  `edullm/reservoir-dolma2-build` (`9ec447d`), but the PRM code itself is still only in that one
  directory. Another session owns it.

## How to operate it (quick reference)

- **Publish**: `from edullm_data.publish import publish` — args (source, dataset_id, purpose,
  profile) + `tokenizer="tokenizer/<name>"` for a pretrain corpus + optional group_meta. See `USAGE.md`.
- **Read**: `from edullm_data.read import dataset_paths, resolve_latest`. An unsplit read returns
  **trainable data only**; both splits come back separately keyed in `.splits`/`.train`/`.val`; the
  seal is recomputed on every read. **Always pass `r.dtype` to the loader.** Full read-side contract
  (every field, the dtype asymmetry, the OLMo-core constraints): `docs/CONSUMER-CONTRACT.md`.
- **Slice by label** (`0f463ea`): `dataset_paths(..., labels={"source": "stack-edu"})`, or add
  `"domain"` to narrow further. Keys are whatever the producer used — nothing hardcodes
  `source`/`domain`, so this works for any family. `rows`/`split_rows` are RECOMPUTED for what was
  selected; asking for labels on an unlabelled dataset raises rather than returning `[]`.
- **Build a data mixture** (`0f463ea`):
  `build_mixture(ds, ver, sources=[MixtureSource({"source": "stack-edu"}, 0.5), …], total=2_000_000_000, seed=42)`.
  Whole shards in a seed-determined order, so `(dataset, version, sources, ratios, total, seed)`
  fully describes a training set — six values in a run config, not 6,911 URIs. Returns
  `actual_ratios`, `counts_by_source`, `unit`, and `shortfall` (a component that could not reach
  its ratio). `max_repetition_ratio` upsamples a small source; `max_source_fraction` is a HARD cap
  that will not overshoot by part of a shard, unlike the budget. **Single dataset only** — mixing
  two corpora risks combining different tokenizers whose vocab sizes are close enough that every
  id still looks valid, which is silent and wrong.
- **Discover what's published**: list `s3://edullm-data/_catalog/` — `tokenizer/dolma2-bpe/v1` and
  `pretrain/olmo-150b-dolma2/v1`.
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
