# Orchestrator's own code findings (verified by reading, not delegated)

## F1 — SHARD SIZE: the report and the code disagree
- `corpus.py:89`: `SHARD_TOKENS = 3052 * SEQ_LEN` = **25,001,984** (100.0 MB/shard).
- `docs/FINAL-DATASET-REPORT.md` §11 claims **50,003,968** (~20,000 objects).
- At 1.0T: code gives **39,997 objects**, report gives 19,998. Task #9 ("decide shard size") is
  marked complete but the decision never landed in code.
- VERDICT: the report is aspirational; the code is authoritative. One of the two must change.
- ✅ **RESOLVED 2026-08-07: the report changed.** `FINAL-DATASET-REPORT.md` §11 now says 25,001,984 →
  ~40,000 objects, matching the code. The 50,003,968 value appeared in no commit and is withdrawn. Its
  companion "0.33% worst-case mixture error" is the same quantity at the withdrawn size; at the real size
  it is 0.007%–0.278%.

## F2 — Gate A does not fit the validator timeout at either shard size
> ⚠️ **The call count here is CORRECTED to 8 by F22 below.** The 6 listed are the ones I could name in the
> profile checks; the measured total is 8 (a call-counting spy recorded 80,392 trips ÷ 10,049 objects =
> exactly 8). **The wall-clock figures below are unaffected**, because they scale from the measured
> 507.5 ms/object rather than from the call count. Left as written because F22 explains the error.

- MEASURED anchor: 10,049 objects -> 85 min => **507.5 ms/object**.
- Per object the profile checks issue **6 SERIAL round trips** *(of the 8 real ones)*: 1 HEAD
  (`_observed_size`) + 4 ranged GETs (`_N_WINDOWS = 4`, `pretrain_tokens_v1.py:240`) + 1 8-byte npy
  sniff (`:438`).
- Extrapolated: 20,000 objects = **2.82 h**; 40,000 = **5.64 h**. The cap is **7200 s (2.0 h)**.
- So Gate A is OVER at BOTH candidate shard sizes. Shard size is forced by the validator, not by
  mixture error (which is only 1/shards_per_component).

## F3 — the cause is probably connection pooling, not latency
- 507.5 ms / 6 = **84.6 ms per round trip**, far above a normal in-region ranged GET (~10-25 ms).
  *(At F22's corrected 8 calls it is **63.4 ms** — still well above a normal ranged GET, so this finding
  holds; it gets slightly weaker, not refuted.)*
- `validate.py:2477` says `max_pool_connections` is "the default 10"; `s3.py:196`
  (`Boto3S3.from_region`) builds `boto3.client("s3")` with **no Config at all**.
- `validate.py:577` DOES thread the size sweep (`head_workers`), but grep finds **zero** threading
  in `profiles/pretrain_tokens_v1.py`.
- FIX = raise the pool AND thread the profile checks. Threading alone against a 10-connection pool
  self-throttles.

## F4 — dedup is PER-BUNDLE, and that is deliberate but does not scale to this mix
- `corpus_build.py:482` calls `dedup_and_decontaminate(...)` with **no `seen=`**, so
  `corpus_filter.py:302` allocates a fresh `SeenHashes()` per bundle.
- `SeenHashes` docstring: MEASURED 85.9 B/entry for `set[int]`; 120M docs = 18.6 GB, which already
  "sat at 97% memory" in a 20 GiB container.
- At 1.0T and 438.5 tok/doc (MEASURED, `artifacts/recount/synthetic.json`): **2.28 B documents**.
  - global set: **195.9 GB** — does not fit anything.
  - DCLM alone (378B tok) = 862M docs = **74.05 GB** in ONE bundle (`domain_column: None`, so it
    does not fan out).
- Bloom filter at n=2.28B, fp=1e-6: **8.20 GB, k=20**. This is the only affordable global option.

> ⚠️ **BOTH numbers in the two lines above are SUPERSEDED — see the CORRECTION below and
> `IMPLEMENTATION-PLAN.md` §5.2a.** 438.5 tok/doc is the *synthetic* mean, not the corpus mean; the real
> mix is **1,041 tok/doc → 960M documents**. And *"the only affordable global option"* was true only against
> a global `set` at 195.9 GB — the **flat `np.uint64` pre-pass is 7.68 GB global and exact**, which the
> Bloom filter is not. **Bloom is the fallback, not the plan.**
>
> **The one thing this finding got RIGHT that the plan got wrong:** DCLM is the worst single bundle, because
> `domain_column: None` means it does not fan out. §5's bundle table omitted DCLM entirely (it scaled the
> reservoir mix, where DCLM was only 29.8B). Re-derived at the report's real 410B share and DCLM's own
> measured 1,261.5 tok/doc: **325M documents = 27.92 GB**, still the largest, and 1.44× worse than the
> `finephrase-table` figure the plan called the blocker. **This finding located the right bundle with the
> wrong arithmetic.**

## F5 — interleaving (task #15) is TRAINER-side. Definitively.
- `manifest.py:233`: `labels` is a field of `ManifestEntry` — **one dict per shard path**, not per
  document.
- `manifest.py:693`: `PATH_LABEL_KEYS = ("source", "domain")` — exactly two levels.
- Gate A recomputes labels from the path and compares by **full dict equality**
  (`labels_from_path` docstring, `validate.py` `declared != expected`).
- => An interleaved shard holds documents from N sources but can carry only ONE `source` label. It
  would either be mislabelled or fail Gate A. **Data-side interleaving is not expressible.**

## F6 — there is no room for a "stage" label
- `PATH_LABEL_KEYS` is 2 deep and `labels_from_path` RAISES when the tree is deeper.
- So stage 1 vs stage 2 cannot be a third path segment. Options: fuse stage into `source`
  (e.g. `dclm-s1` / `dclm-s2`), publish two datasets, or let the trainer's config do the staging.

## F7 — ordinals are globally allocated and capped at five digits
- `corpus.py:305`: ordinal must satisfy `0 <= ordinal <= 99999`, else `shard_key` raises, and a
  6-digit name fails `SHARD_RE` so `parse_shard_name` returns None and the object silently stops
  being split-checked.
- Cap = 99,999 shards = **2.50 T tokens per group** at 25.0M/shard. 1.0T fits (40k), but a 2-epoch
  re-pack or a second stage in the same group eats the headroom faster than it looks.

## F8 — val bundles read ~200x their own token count
- `corpus.py:163`: `VAL_FRACTION = 0.005`. `Bundle.keep_rate` returns 0.005 for a val bundle and
  `_reader_for` divides the byte budget by it (`corpus_build.py:~880`).
- `is_held_out` is a hash of the document id, so a val document cannot be located without reading
  the train documents beside it.
- `plan_document` sets `val = 0` when 0.5% of the target is under one shard; break-even is
  **5,000,396,800 tokens**.

## F9 — tokenize is NOT the bottleneck, so gigatoken's ceiling is small
- MEASURED (`corpus_pack.py` docstring): **10.5 M tok/s** batched across 32 vCPU; 1.10 M tok/s
  single-document.
- 1.0T tokens = **26.5 h at 32 vCPU**, 8.8 h at 96.
- Source text to move = **4.27 TB kept** (from MEASURED tok/byte per source), ~6.1 TB read at a
  70% keep rate.
- A PERFECT 1000x tokenizer speedup saves at most 26.5 h and cannot touch the download.
- The real hazard is the double-encode the docstring warns about: 1.10 M tok/s = **10.5 days**.

## F10 — boundary-marker neutralization covers exactly ONE literal
- `corpus_pack.py:128`: `_BOUNDARY_MARKER_REWRITES = (("<|endoftext|>", "<| endoftext |>"),)`.
- The fast-path guard is `if "<|" not in text: return text` (`:141`), so ANY marker that does not
  start with `<|` is never even scanned: `</s>`, `<s>`, `[EOS]`, `<end_of_turn>`, `<|eot_id|>`
  (which DOES start with `<|` but is not in the rewrite list, so it passes through unrewritten).
- The reservoir's sources were web/code/academic, where `<|endoftext|>` is the realistic leak. The
  NEW mix adds instruction-tuned and synthetic sources (Nemotron SFT, dolma3 midtrain, reasoning
  traces, Cosmopedia) whose text is generated by CHAT models and is much likelier to carry
  `<|im_start|>`, `<|eot_id|>`, `</s>`.
- SEVERITY: a leaked marker that tokenizes to the EOS id creates a PHANTOM document boundary.
  Since §2.3 ships no `.csv.gz` sidecars, EOS is the only boundary the corpus has, so a phantom
  boundary is unrecoverable and invisible to every gate (the id is in range and decodes).
- FIX: extend the tuple, and replace the `"<|"` guard with a cheap multi-prefix test. Must be done
  BEFORE ingesting any chat-derived source.

## F11 — `keeps_id`'s only caller is a REPORTING function
- `ingest_reservoir.py:743` is inside `_partition_report`, which builds a dict for a report.
- So the predicate never gates a write anywhere. Confirms task #4: declared synthetic volume rests
  on ~28% as many distinct documents (0.2683 distinct MEASURED on 287,000 ids).
- `partition_of` uses the FULL 256-bit sha256 of the doc id mod n (`reservoir_ids.py:102-113`), and
  `N_PARTITIONS = len(FINEPHRASE_FORMATS)` (`:70`), so the partition count is derived from the
  format list rather than typed.

## F12 — ⚠️ THE BLOCKER: incremental ingest throws away nearly all prior work
Found by the pipeline-scale audit; I verified it myself in `corpus.py:352-359`.

`allocate_ordinals` assigns ordinals from **one per-split counter**, walking the plan **sorted
alphabetically** by `(source, domain, split)`. So a source's ordinal block depends on the
cumulative shard count of every source sorting before it.

**SIMULATED on the real stage-1 mix at the code's shard size (25,001,984 tokens):**

| | |
|---|---|
| baseline plan | 35,998 train shards |
| add ONE 4B source (`cosmopedia-synthetic`) | **8 of 9 sources renamed** |
| shards renamed | **35,278 of 35,998 = 98.0%** |
| completed work discarded | **882.0B tokens** |
| re-tokenize cost | **23.3 h at 32 vCPU** |

The path carries the ordinal (`corpus.py:311`) and the path is inside `manifest_sha256`, so a
rename is a different dataset identity. `bundle_is_done` rejects any receipt whose `plan_id`
differs (`corpus_build.py:413-417`), so every prior receipt is void and every uploaded object is
an orphan.

**This directly contradicts the plan of record**, which says "Then ingest, source by source, each
as a separately-approved platform job" (`HANDOFF-FINAL-DATASET.md:231`). Executed literally, each
new approval discards the previous one's output.

**FIX — the plan must be FROZEN before the first job.** Compute the complete plan for every source
of BOTH stages up front, then run bundles in whatever order approval allows: `_cmd_run --shard/--of`
already slices bundles and `bundle_is_done` already skips finished ones, so partial execution of a
complete plan is fully supported today. **Zero code. But it means the mix must be final before the
first token is written** — which makes freezing the mix the true blocking step, not the ingest.

**Second option, for the NEXT build:** key the counter by `(source, domain, split)` instead of
`split`. Ordinals then restart per stream and cannot move. The docstring at `corpus.py:322-330`
argues against this, but its argument is **only about human legibility in logs**, and it explicitly
verifies that nothing in `validate.py` rejects ordinal reuse (no contiguity, gap, or uniqueness
check; the `tokens/<source>/` prefix already disambiguates). ~10 lines. Cannot be adopted mid-build
because it changes every `plan_id`.

## CORRECTION to F4 — my document count was wrong; use the measured one
I derived 2.28B documents at 1T from 438.5 tok/doc. That is the **synthetic** mean
(`artifacts/recount/synthetic.json`), not the corpus mean.

**MEASURED, from 27 real receipts** (`artifacts/reservoir/realized-tokens.json`):
308,291,107 docs / 251,218,001,920 tokens = **814.9 tok/doc**.

At 1.0T that is **1.23B documents** (point estimate), 2.0B as a planning bound if the mix shifts
toward synthetic (FinePhrase averages 263-442 tok/doc against pubmed's 7,918 — a 30x spread).

Re-sized:

| approach | at 1.23B docs | at 2.0B docs |
|---|---|---|
| one global `set[int]` | 105.4 GB | 171.8 GB |
| Bloom, fp=1e-6, k=20 | 4.41 GB | 7.19 GB |
| sharded /64 | 1.65 GB/worker | 2.68 GB/worker |
| **sharded /256** | **0.41 GB/worker** | **0.67 GB/worker** |

Conclusion unchanged (a global set fits nothing; sharding is cheap), but the numbers are now
measured rather than derived from the wrong mean.

## F13 — ⚠️ THE OTHER BLOCKER: the FinePhrase partition is written, verified, and never called
This is a bigger correctness problem than cross-source dedup, and it is invisible to every dedup
method including MinHash.

- The four FinePhrase configs are **ONE corpus rephrased four ways** over the same ~339M
  FineWeb-Edu documents, **MEASURED at 91.0-92.9% pairwise id overlap**.
- `keeps_id` has exactly three call sites: a **reporting** function, a test, and a measurement
  script. **Zero in the build path.** `IdSet.contains`, the anti-join primitive, has **zero callers
  anywhere**.
- `id-partition-verification.json` proves the partition balances (24.86-25.27% per bucket) — it
  audits a function production never calls. **A green artifact for an unused code path** is the most
  dangerous shape of gap.
- DERIVED from the measured overlap: of 59.8B synthetic tokens, only **~18.5B are distinct source
  documents**; ~41B are rephrasings of documents already present under another format label.
- **Exact hashing cannot see this at any scope** — four rephrasings are four different strings
  (`corpus_filter.py:103-105`). Neither can MinHash, reliably.
- **Worse, the epoch guard reports green.** `epochs_for` (`corpus.py:430-441`) divides by the
  *declared* pool size, so a teammate drawing 0.25 from each of four synthetic sources sees
  0.33-0.50 epochs while true per-document exposure is **~4x**.

**FIX:** apply it at the READER boundary, not as a filter stage — in `_reader_for`, wrap the stream
in `if keeps_id(config, doc.id)` when `spec.key.startswith("finephrase-")`. ~5 lines, zero extra
I/O (the id is already read). Required keep fractions are 10.1% (faq), 15.8% (math), 17.3% (table),
10.1% (tutorial) — all under the 25% a disjoint quarter provides, so the targets stay reachable.
**But it changes the plan**, so it must land BEFORE the plan is frozen (F12). The registry's own
trap says it must happen **before tokenizing**: "after tokenization there is no document->id
mapping and it cannot be retrofitted."

## F14 — the receipt does not record what dedup removed
`run_bundle` returns `filter_stats.as_dict()` and `_cmd_run` prints it to stdout, but **`Receipt`
has no filter block** — grep for `duplicates`/`contaminated` in `corpus_receipt.py` returns zero.
So the actual duplicate and contamination rates on our own sources exist only in CloudWatch logs
from the 2026-08-05 run. Cheap to fix, and it blocks every quantitative dedup claim we might make.

## F15 — DCLM: the registry's source repo cannot deliver 378B, and the obvious fix is unreadable
From the source-encoding audit; I verified the code half myself.

- The plan wants **378B tokens** of DCLM. `HuggingFaceFW/dclm_100BT` is **MEASURED at
  114,691,544,533 dolma2 tokens** — it cannot reach 378B. It is 30% of what stage 1 asks for.
- The full corpus `mlfoundations/dclm-baseline-1.0` ships **`.jsonl.zst`** (verified from bytes:
  zstd magic `28 b5 2f fd`).
- **VERIFIED IN CODE:** `corpus_build.py:127` sets `READABLE_FORMATS = frozenset({"parquet",
  "json.gz"})` and `_assert_readable` (`:171`) rejects anything else **at plan time**.
  `corpus_read.py:774-775` says so outright: "`.zst` is NOT among them … needs a zstandard
  dependency this package does not declare." `pyproject.toml` declares only `boto3` and
  `numpy<2.5`.
- => **Use `mlfoundations/dclm-baseline-1.0-parquet`** (27,938 shards, parquet, no new dependency).

**Two traps in that mirror:**
1. Its row count is an **estimate**, not measured: `/statistics` returns a permanent HTTP 501, and
   `/size` returns `num_rows=779,982` with `partial:true` — the converted head, **0.03% of the
   corpus**. A pipeline reading `num_rows` from `/size` sizes the source at **1/3800 of reality**.
2. The parquet mirror has **6 columns to the original's 8** — it drops the WARC metadata struct and
   `warcinfo`. The card calls it "an identical copy"; it is identical in `text`, not in provenance.
   If WARC lineage is wanted for CC-BY attribution, this mirror cannot supply it.

Full-corpus size is **DERIVED at ~3,764B dolma2 tokens** (2,949.3M docs x 5,461.0 mean chars x
0.2337 measured tokens/char), cross-checked against an independent Zyphra figure to 2.4%. So 378B
exists comfortably — but only in the mirror, and only on a derived row count.

## F16 — ⚠️ THE THIRD BLOCKER: the CURRENT per-bundle dedup OOMs at 1T
Found by the dedup audit; I verified both halves myself.

The container is **14 GiB (15.03 GB)**, sized from a measured worst-bundle resident of ~12.1 GB
(HANDOFF.md:1874-1882). Scaling the largest bundle by DOCUMENT count to 1T:

`synthetic-finephrase-table--train`: 56,839,223 docs at 252B -> **225.6 M docs at 1T** (its mean
document is only 263 tokens, so it has the most documents despite not being the biggest by tokens).

| representation | worst bundle @1T | fits 15.03 GB? |
|---|---|---|
| `set[int]` 128-bit (**current code**) | **19.37 GB** | **NO** |
| `set[int]` 64-bit | 17.57 GB | **NO** |
| flat `np.uint64`, 16 B/key | 3.61 GB | yes |
| flat `np.uint64`, 8 B/key | **1.80 GB** | yes |

And dedup is only one resident structure: +0.45 GB decon index, +0.4 tokenizer, +0.1 shard,
+0.5 pyarrow row group, +interpreter. **Three of the four FinePhrase bundles and stackv2-edu all
exceed the container on the dedup set alone — without anyone attempting global dedup.**

**VERIFIED MYSELF — and this corrects an intuition worth recording.** Narrowing the key inside a
Python `set` does NOT help: `sys.getsizeof` gives 44 B for a 128-bit int and 36 B for a 64-bit one,
so against the codebase's measured 85.9 B/entry the set's own slot overhead is **41.9 B/entry and is
width-independent** (CPython stores a pointer plus a cached hash, not the value). Narrowing
128->64 buys **9.3%, not 50%**.

**The 5-11x win comes from leaving the `set` for a flat numpy array**, which forces a
sort-and-unique shape — i.e. a separate pre-pass rather than an in-build set. That is independently
what determinism wants (a shared mutable filter inside the build makes the output depend on bundle
execution order). **Both constraints point at the same design**, which is what §5 now recommends.

## F17 — ⚠️ `tokenizers` is UNPINNED and UNDECLARED, and it produces the corpus
Two corrections to the gigatoken audit's Unicode finding, one favourable and one not:

- The audit reported `tokenizers-0.20.1` in the workspace (the Unicode-14 generation it flagged).
  **VERIFIED: this environment has 0.22.2.** So we are not on the bad generation locally.
- **But `tokenizers` appears NOWHERE in `pyproject.toml`.** `grep 'tokenizers' pyproject.toml`
  returns nothing, while `corpus_build.py:631` does `from tokenizers import Tokenizer` and
  `corpus_pack.tokenize_documents` calls `encode_batch` on it. Declared dependencies are exactly
  `boto3` and `numpy<2.5`.

**So the package that decides every token id in the corpus is resolved at container build time to
whatever PyPI served that morning.** This is precisely the hazard `pyproject.toml`'s own `numpy<2.5`
comment was added to prevent: *"An unpinned `numpy` resolves at RUN TIME inside a Batch container, so
the version that runs in production is whatever PyPI served that morning."* The same reasoning
applies with more force to `tokenizers`, because a numpy change crashes and a tokenizer change
**silently emits different ids that stay in range and decode**.

The Unicode-version axis makes this concrete rather than theoretical: `tokenizers` 0.20.x resolves
`\p{L}`/`\p{N}` against Unicode 14 and later versions against newer tables, so two builds of the
same corpus on different mornings can differ on documents containing recently-assigned codepoints —
with no gate able to tell.

**FIX: declare and pin `tokenizers` in `pyproject.toml` before the first ingest job.** Then record
the resolved version in the receipt beside `wheel_version`, so a corpus can name the tokenizer
implementation that produced it, not just the vocabulary it used.

## ⚠️ CORRECTION to F10 — my "extend the rewrite list" advice was WRONG
The source-encoding audit corrected me and the code proves it.

`tests/test_corpus_pack.py:1205-1207` states the real position: *"The dolma2 tokenizer defines 22
added tokens and all 22 parse from raw text, but **only 100257 is the document boundary**. The other
21 are ordinary in-vocab ids — unusual, not dangerous — and rewriting them would modify documents to
fix a problem that does not exist."*

So under dolma2, `</s>`, `<|im_start|>`, `<|eot_id|>` and `[EOS]` are **not** boundaries. They
tokenize to ordinary ids. My F10 conflated a *quality* concern (chat scaffolding in pretraining text)
with a *corpus-splitting* one (a phantom document boundary). Only the second is a correctness defect,
and only `<|endoftext|>` causes it. **The single-entry table is correct.**

**But two real defects survive, and one is worse than what I claimed:**

1. **The guard makes any future addition a silent no-op.** VERIFIED by reproducing the logic:
   `if "<|" not in text: return text` short-circuits *before* the table is consulted, so adding
   `("</s>", …)` changes nothing for text lacking `<|`. And **`tests/test_corpus_pack.py:1211`
   asserts `len(_BOUNDARY_MARKER_REWRITES) == 1`** — so the suite actively locks the table at one
   entry. A future maintainer who adds a marker gets a failing test, not a working fix. Fix the guard
   and the test together, or leave a comment saying why the table must stay at one.
2. **Nemotron-CC-Math is Phi-4 output, and Phi-4's `<|endoftext|>` IS id 100257** — byte-identical to
   dolma2's EOS. The card documents no special-token scrubbing. So a leaked stop token inside 45B
   tokens of math becomes a **phantom document boundary** — the exact mechanism that already took
   down five live bundles at a ~1/2,500 rate. `neutralize_boundary_markers` DOES cover this (it is
   `<|endoftext|>`), so the existing code is adequate — **but it must actually run on this source**,
   and the same exposure applies to `dolma3-dolmino`, where Phi-4 is one of ten generators.

## F18 — one audit claim REJECTED: arXiv 2604.13977 does resolve
The dedup/decontam audit asked me to correct project memory on the grounds that
"arXiv:2604.13977 does not exist." **I checked, and it does.**

Fetching `arxiv.org/abs/2604.13977` returns a full record: "How Can We Synthesize High-Quality
Pretraining Data? A Systematic Study of Prompt Design, Generator Model, and Source Data", 12 authors
(Niklaus, Yamaguchi, Štefánik, Penedo, Kydlíček, Bakouch, Tunstall, Beeching, Frere, Raffel, von
Werra, Wolf), cs.CL with cs.AI/cs.LG cross-lists, submitted 15 Apr 2026, revised 30 Jul 2026,
accepted at COLM 2026. The abstract names FinePhrase and the 486B figure, and states the
generator-scaling result.

**Why the agent got it wrong, and why I nearly did too:** the tool that fetched the page flagged the
April-2026 date as "a future date" and concluded the listing must be fabricated. Today is
**2026-08-07**, so April 2026 is four months in the PAST. This is a knowledge-cutoff artifact, not
evidence about the paper.

**Lesson worth keeping:** "this arXiv id looks like it is from the future" is a claim about the
checker's calendar, not about the paper. Verify the date against today before retracting a citation.
The memory `finephrase-is-real-and-central` stands unchanged.

The audit's four OTHER documentation corrections are accepted and worth making:
1. `limitations[]` should carry the measured 91.0-92.9% inter-format id overlap and the 100%
   FineWeb-Edu document-level collision — a consumer currently cannot know a leaked item may appear
   ~5x under 5 source labels.
2. `corpus_filter.py:7-9` understates DCLM's Bloom-alone figure: **+2.1 CORE, not +1.6**. The
   qualitative claim survives and is stronger than written.
3. `corpus_filter.py:33-34`'s "{dev, validation, test}" MMLU coverage claim is wrong — `dev` is in the
   index only as the embedded 5-shot preamble.
4. Record the 13-gram-vs-5-gram divergence with its justification rather than leaving it implicit.

## F19 — the self-inflicted-slowness pattern: every worker default is 1, and job defs do not pass the flags

**The shape, and it is systematic rather than scattered:** five threading facilities exist in the
package and work. **Every default is 1.** And the build CLI exposes only ONE of them
(`--hash-workers`, `corpus_build.py:968`); `validate.py`'s `head_workers` and `publish.py`'s
`hash_workers`/`copy_workers` have no CLI flag on the build path at all.

**The clearest case, MEASURED.** `verify --deep` on 2026-08-05: **1.005 TB in 3.27 h at 87.8 MB/s
sustained, single stream, on a 16 vCPU box**, finishing with only 0.73 h (22%) of margin against a
4 h timeout. `verify-job.json`'s comment blames "single-threaded" code.

**⚠️ CORRECTION to my first reading of that.** The code is not the problem, and the fix already
shipped: `PUBLISH-SPEC.md:167` records that **`0.7.5` threads the deep re-hash and it was MEASURED at
7.82x on 8 workers** — so the same run is ~25 min. We are on **0.9.1**, so the code is present today.

**The blocker is the JOB DEFINITION.** `PUBLISH-SPEC.md:168` states it outright: *"`edullm-reservoir-verify:1`
does **not** pass the flag — a new revision is needed before the speedup is reachable, and the job
def must also move to a `0.7.5` image."*

**At 1.0T this stops being cosmetic:**

| | 1.005 TB (measured) | 4.00 TB (at 1.0T) |
|---|---|---|
| single-threaded | 3.27 h | **13.02 h — times out** |
| 8 workers (measured 7.82x) | ~0.42 h | **1.66 h — fits** |

**And the whole build path has ZERO threading**: `corpus_read.py`, `corpus_build.py`,
`corpus_pack.py`, `corpus_filter.py` and `s3.py` have no `ThreadPoolExecutor` or `max_workers` at
all. Per child, read and tokenize are serialized in one generator chain; all parallelism came from
Batch array children plus the tokenizer's internal rayon.

**THE LESSON FOR THE WALL-CLOCK SECTION: the time a 1T build actually takes depends on job-definition
revisions, not on the package version.** A stage can be fixed in code, shipped, and still run at the
old speed because the registered job def never passes the flag. **Every wall-clock figure in the plan
must therefore name the job def and the flags it passes**, not just the stage.

**Related, and it is why this file distrusts the calibration doc:** `INGEST-CALIBRATION.md` measured a
correct number (0.44 files/s at 16 workers) and drew two confidently wrong conclusions from it, both
later retracted in a banner — "the binding constraint is requests per IP" was actually **our own 70x
quota amplification** (resolving a signed CDN URL per range read instead of once), and "the 7200 s
timeout" was **our own setting**, not an AWS limit. It also contains a self-flagged **16x unit error**
(wall-seconds vs worker-seconds per file) that propagated into `RUN-THE-INGEST.md:35`. After the CDN
fix the same pass took **67 s**. Do not size anything from that file's tables.

## F20 — reconciling two audits that reached opposite verdicts on 13-gram vs 5-gram

The dedup/decontam audit's *notification summary* claimed "the shipped decontamination rule is tuned
to report clean" and that "the design doc was right; the code is what to change." **I did not
propagate that, for three reasons.**

**1. Its own written finding is much weaker than its summary.** `F13` in the file itself grades the
divergence **"cosmetic-to-quality-degrading"** and concludes: *"Nobody has measured 13/2 vs 5/0.8 on
our corpus — that is a finding, not a gap I can close by reading code. Cheapest resolution: document
the divergence ($0); measure only if a leak is suspected."* That is the honest version and it does
not reverse anything. **When an agent's summary is stronger than its own evidence section, trust the
evidence section.**

**2. The ConTAM claim is undocumented.** `grep ConTAM` in the audit file returns **zero hits** — the
paper, its n=8→10 halving figure, and the `mincount>1` quote exist only in the notification, not in
the artifact. An unrecorded citation cannot be checked or cited by a successor.

**3. Prior art already engaged this question and reached the opposite conclusion, with a mechanism.**
`artifacts/1t-research/11-decontamination-audit.md` (294 KB, which the agent stated it did NOT read)
calls `ngram_size 5` **"the most dangerous number in this whole audit"** at line 88. Both audits agree
DCLM Table 19 is a *dedup* result — but the prior one goes further and explains why it transfers:

> *"The mechanism is n-gram collision at short lengths causes mass removal of MMLU-relevant material,
> and that mechanism is identical whether the match set is other documents in the corpus (dedup) or
> benchmark items (decontamination). Decontamination is arguably worse: the match set is deliberately
> concentrated on exactly the knowledge MMLU tests, so the collisions are not random with respect to
> the metric — they are aimed at it."*

It also supplies independent corroboration (Duan et al. measured non-member **7-gram** overlap at
32.5-77% by domain; 5-gram collision is higher still) and the v4 table numbers verbatim
(min_ngram 5 → MMLU 32.5, min_ngram 13 → 44.3, Core 44.5 vs 45.3), while noting the two rows also
differ in shard count so it is **not a clean single-variable ablation**.

**And it names the detail the newer audit never addresses:** `allenai/decon` at 5-gram weights by
**IDF** and requires **cluster expansion**, which suppresses exactly the common-phrase collisions
that would sink a naive 5-gram filter. So the design doc's 5-gram is not the same object as DCLM's
5-gram row. *"That is a real difference and it may well be sufficient — but nobody has measured it,
and adopting it means betting the project's headline metric on an untested assumption."*

**VERDICT: keep 13/2 for this build, and document the divergence.** The asymmetry decides it — a
decontamination false negative leaves one benchmark item in a 1T corpus, while a false positive at
5-gram risks the mechanism that cost DCLM 11.8 MMLU. Revisit only with a measurement on our own
corpus, and record the ConTAM citation properly first so it can be checked.

**The one thing both audits agree on, and it is the real fix:** the index is built over 5-shot
RENDERED prompts, which is a defect at *any* n-gram size (task #24). Fix that before touching `n`.

**Also accepted from this audit:** GPT-3 Table C.1 gives the bound I had left unquantified — 5th-percentile
benchmark item lengths are **11/12/13 words**, so **≥5% of ARC and HellaSwag items are shorter than a
single 13-gram** and are reachable only by the exact-hash half, which F2 shows is inert for those
suites. That makes the short-item hole concrete rather than theoretical.

## F21 — the wall-clock audit demolished my own read-amplification finding. It was right.

**RETRACTED: F5/§3.1's "the pipeline reads 18 TB to fetch 4.21 TB."** I verified the refutation two
independent ways and it holds on both.

1. **`val_fraction` cancels out of the read, algebraically.** A val bundle's tokens are `want × VF`
   and its budget divisor is `VF`; a train bundle's are `want × (1−VF)` over `(1−VF)`. **Both equal
   `want × 9.0` for any `val_fraction`.** The "200× divisor" amplifies nothing — I read the formula
   and failed to cancel it. Both source documents state the formula and neither cancels it either.
2. **The budget is a CEILING never reached.** The reader is a lazy generator feeding `pack`, which
   iterates `stream_refs` and sets `exhausted` only when documents run out
   (`corpus_pack.py:727-741`). **Verified on the real run: 26 of 27 bundles filled every shard**;
   unfilled refs appear only in `finewiki--train` (33). So `pack` stopped before the reader drained
   its budget in every other bundle.

**So correcting `_CHARS_PER_TOKEN` saves exactly zero bytes and is pure downside** — too low and a
bundle starves, producing the unfilled refs `verify` rejects. **Task #25's first item is withdrawn.**
The real over-read is **2.02×**, and the intrinsic finding survives: **43% of bytes moved is the val
split serving 0.39% of tokens**, which file-sharding fixes and a constant does not.

**The lesson is the one §8A.1 already draws about the calibration file, now applied to me:** I derived
a headline number from a formula without checking whether the code reaches it. **A budget is not a
measurement.**

## F22 — my Gate A model undercounted by two calls, and I had explained the gap away
At 6 calls/object, 10,049 objects over 85 min is 11.8 rt/s; the MEASURED rate is **15.8**. I attributed
the gap to per-group LISTs and manifest GETs. **It was the two calls I had missed:** 8 calls/object
gives **15.76 rt/s**, reproducing the measurement almost exactly. Independently corroborated by a
call-counting spy in commit `db437b6`: **80,392 round trips before the HEAD-cache fix, 70,343 after**
— 80,392 / 10,049 = exactly 8.

**A model that needs a hand-waved remainder to match a measurement is not a model yet.** The §8.2
wall-clock is scaled from the measured 507.5 ms/object directly, so those figures are unaffected; only
the per-call attribution changes.

## F23 — the OOM conclusion is double-sourced, but the audit's example used a stale constant
The audit reported `stackv2-edu` at **~26 GB** using **155 B/entry** (`HANDOFF.md:1469`, MEASURED —
and the cause of all four hosts pinning at 97% memory with 25% of CPU idle). That figure predates the
int narrowing: the current code stores `int(digest[:32], 16)`, measured at **85.9 B/entry**.

Re-derived at the current representation:

| bundle @1T | docs | `set[int]` 85.9 B | verdict |
|---|---|---|---|
| `stackv2-edu` | 168.1 M | 14.44 GB | **fits** 15.03 GB (barely, and before the decon index) |
| **`synthetic-finephrase-table`** | **225.6 M** | **19.37 GB** | **OOM** |

**So the conclusion stands but the example must change:** the OOM is driven by
`synthetic-finephrase-table` (263 tok/doc mean → most documents), not `stackv2-edu`. Task #22 is
unaffected — a flat `np.uint64` array is 1.80 GB either way.

## F24 — three new defects worth having, and one measurement that would settle everything
From the same audit, not yet verified by me:
- **The 13-gram decontamination scan is ~193 billion Python-level `blake2b` calls** and has never been
  measured. Plausibly the unexplained hours in the build. **This is a real risk to the §8A projection**
  because it is CPU work I attributed entirely to tokenization.
- **Documents are NFC-normalized three times.**
- **`bytes_fetched` — the exact counter that would have settled the over-read question — is reported by
  the ingest module and discarded by the build module.**

**And the largest evidence gap in the whole wall-clock section:** no in-region `publish()` duration has
**ever** been measured, and it is the one stage that pulls ~4 TB through a client. Related and
alarming: the audit's reconciliation of the measured 8 h build implies **HF CDN throughput may be
~8.4 MB/s, not the ~85 MB/s that every read projection borrows from an S3 measurement.** If true, the
staged-copy recommendation in §3.2 becomes much more valuable, and Phase 0b's bandwidth measurement is
the single highest-value job in the plan.
