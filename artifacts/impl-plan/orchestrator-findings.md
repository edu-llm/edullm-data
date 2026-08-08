# Orchestrator's own code findings (verified by reading, not delegated)

## F1 — SHARD SIZE: the report and the code disagree
- `corpus.py:89`: `SHARD_TOKENS = 3052 * SEQ_LEN` = **25,001,984** (100.0 MB/shard).
- `docs/FINAL-DATASET-REPORT.md` §11 claims **50,003,968** (~20,000 objects).
- At 1.0T: code gives **39,997 objects**, report gives 19,998. Task #9 ("decide shard size") is
  marked complete but the decision never landed in code.
- VERDICT: the report is aspirational; the code is authoritative. One of the two must change.

## F2 — Gate A does not fit the validator timeout at either shard size
- MEASURED anchor: 10,049 objects -> 85 min => **507.5 ms/object**.
- Per object the profile checks issue **6 SERIAL round trips**: 1 HEAD (`_observed_size`) +
  4 ranged GETs (`_N_WINDOWS = 4`, `pretrain_tokens_v1.py:240`) + 1 8-byte npy sniff (`:438`).
- Extrapolated: 20,000 objects = **2.82 h**; 40,000 = **5.64 h**. The cap is **7200 s (2.0 h)**.
- So Gate A is OVER at BOTH candidate shard sizes. Shard size is forced by the validator, not by
  mixture error (which is only 1/shards_per_component).

## F3 — the cause is probably connection pooling, not latency
- 507.5 ms / 6 = **84.6 ms per round trip**, far above a normal in-region ranged GET (~10-25 ms).
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
