# Source-side text encoding & tokenization trap audit

**Scope.** 14 HuggingFace sources destined for the eduLLM pretraining reservoir, re-tokenized with
`allenai/dolma2-tokenizer`. For each: what the ingest code must do to raw text BEFORE tokenizing,
and what silently produces a wrong corpus if skipped.

**Date:** 2026-08-07. **Author:** audit subagent (source-encoding wave).

**Evidence labels used throughout** — every claim carries one:

| label | meaning |
|---|---|
| MEASURED | someone read the actual bytes/footers and computed it (ours, or an upstream paper's own measurement of its own data) |
| CARD | the HF dataset card / model card / README asserts it; NOT verified against bytes |
| ISSUE | a GitHub issue or HF discussion thread reports it |
| DERIVED | inferred from two or more of the above by argument, not observed directly |
| UNVERIFIED | nobody documents it and we have not measured it — a finding in its own right |

**Hard constraint honored:** no dataset payload was downloaded. All evidence here comes from
dataset cards, `/tree/main` listings, dataset-viewer/datasets-server metadata endpoints, upstream
papers, GitHub/HF issues, and the prior wave's on-disk artifacts under
`/Users/ericwu/Developer/Capstone_LLM/edullm-data/artifacts/recount/`.

**Status:** COMPLETE. §0 = prior-wave evidence; §1–13 = one section per source; §14 = the deliverable
table; §15 = prioritized traps worst-first; §16 = the honest gaps and the jobs that close them.

**The five things to read if you read nothing else:**
1. **§15 P0-1** — Nemotron-CC-Math is Phi-4 output and Phi-4's `<|endoftext|>` is **id 100257, the
   same id as dolma2's EOS**. A leaked stop token is a phantom document boundary in 45 B tokens.
2. **§15 P0-2** — `neutralize_boundary_markers()`'s `if "<|" not in text` guard makes any non-`<|`
   addition to the rewrite table a **silent no-op that still passes its tests**.
3. **§15 P0-3** — FinePhrase ships `finish_reason` and `max_tokens=2048`; our plan ignores it, so
   ~0.5–1.5% of the synthetic pool is mid-sentence fragments no check can see.
4. **§15 P0-4** — I MEASURED that **303/303 Cosmopedia documents across all 8 configs begin with a
   leading space** (Mixtral SentencePiece artifact). Every document's first token is its
   mid-sentence variant, right after our EOS.
5. **§16** — one per-shard "encoding receipt" of O(1) predicates, added to the tokenize path we were
   going to run anyway, converts **eight UNVERIFIED rows into MEASURED ones for free**.

---

## 0. What the prior wave already established (cite, do not re-measure)

Two on-disk bodies of prior evidence, both read this session:

- `artifacts/recount/*.json` — dolma2-exact token re-counts and **parquet-footer byte totals** for
  most Stage-1 sources.
- `origin/edullm/reservoir-dolma2-build:artifacts/reservoir/corpus-registry.json` — a
  **pinned-revision registry of 18 corpus rows** in which `text_column` is already an exact
  `path_in_schema` and each row carries a `traps[]` list. **14 of the 18 rows carry an explicit
  "text_column verified in bytes" note at a pinned commit.** This is the single most valuable
  artifact for question (a) and it means question (a) is already answered for Stage 1.

### 0.1 The registry's pinned revisions (MEASURED — "All 14 (repo, sha) pairs resolved against the HF tree API 2026-08-01")

| key | repo | config / path | format | text `path_in_schema` | id | revision (pinned) |
|---|---|---|---|---|---|---|
| finepdfs-edu | `HuggingFaceFW/finepdfs-edu` | `data/eng_Latn/train` | parquet | `text` | `id` | `9cfabe2127faca99b3d5c4dc6d1fcb397399ebde` |
| fineweb-edu | `HuggingFaceFW/fineweb-edu` | `sample/100BT` | parquet | `text` | `id` | `87f09149ef4734204d70ed1d046ddc9ca3f2b8f9` |
| essential-web | `EssentialAI/essential-web-v1.0` | `data` | parquet | `text` | **UNVERIFIED** | `ce4eccc7e9604667b6d7f32cb6274b8b41f3113d` |
| dclm-baseline | `HuggingFaceFW/dclm_100BT` | `data` | parquet | `text` | `id` | `01022d378d944de6deeb1c79d08fecb4d27b2c6f` |
| finemath | `HuggingFaceTB/finemath` | `finemath-3plus` | parquet | `text` | **`url`** (no `id` column!) | `e92b25a616738fe95dc186b64dfb19f9c8525594` |
| peS2o_filtered | `common-pile/peS2o_filtered` | (none — repo root) | `.json.gz` | `text` | `id` | `297747513bfb0ff1fbf61ddad3b03319d0f04597` |
| pubmed_filtered | `common-pile/pubmed_filtered` | (none — repo root) | `.json.gz` | `text` | `id` | `c156f0569a92d8f2edc33cebe1f72f7d3e1cae84` |
| arxiv_papers_filtered | `common-pile/arxiv_papers_filtered` | (none — repo root) | `.json.gz` | `text` | `id` | `033cf7f53f9b348deec868c1a5a48484f3ee9e52` |
| stackv2_edu_filtered | `common-pile/stackv2_edu_filtered` | (none — repo root) | `.json.gz` | `text` | `id` | `c354dbe88469a1153e97c6a63ac50591849654de` |
| stackexchange_filtered | `common-pile/stackexchange_filtered` | (none — repo root) | `.json.gz` | `text` | `id` | `c0ac7373830c688a43fc12d1988c4b19ccd884ab` |
| ubuntu_irc_filtered | `common-pile/ubuntu_irc_filtered` | (none — repo root) | `.json.gz` | `text` | `id` | `84f88c986584f11d672befab542fa4d5123f3e8f` |
| github_archive_filtered | `common-pile/github_archive_filtered` | (none — repo root) | `.json.gz` | `text` | `id` | `52282fe96670254bdc0d44dd718ee7a27210ee85` |
| finewiki | `HuggingFaceFW/finewiki` | `data/enwiki` | parquet | `text` (NOT `wikitext`) | `id` | `8bd13e72e6a002407649b3e898535f42ceb1aeb9` |
| finephrase-{faq,math,table,tutorial} | `HuggingFaceFW/finephrase` | `faq`/`math`/`table`/`tutorial` | parquet | **`rollout_results.list.element.text`** | `id` | `78cf4a5ed0099214979c094c963e699c19163838` |

**Common Pile file-prefix table (MEASURED from the tree at the pinned revision).** The `.json.gz`
shards sit at the **repo root** with a prefix that is NOT derivable from the repo name — this is
itself an ingest trap (guessing the prefix silently lists zero files, and a zero-file source is a
zero-token category, not an error):
`peS2o_filtered`→`peS2o-`, `pubmed_filtered`→`licensed_pubmed-`, `arxiv_papers_filtered`→`arxiv-papers-`,
`stackv2_edu_filtered`→`stack-edu-`, `stackexchange_filtered`→`stackexchange-dolma-`,
`ubuntu_irc_filtered`→`ubuntu-chat-dolma-`, `github_archive_filtered`→`gharchive-dolma-`.

### 0.2 The single most important prior finding for question (c) — it is MEASURED, in production, on OUR data

`src/edullm_data/corpus_pack.py` (read this session from
`origin/edullm/perf-threaded-verify-and-gatea`) documents `_BOUNDARY_MARKER_REWRITES` verbatim:

> **This is a real corpus property, not a hypothetical.** Five of the largest train bundles failed
> live on it — `dclm`, `finemath`, `fineweb-edu`, `stackexchange`, `stackv2-edu` — each reporting
> a handful more EOS occurrences than documents (measured: 1, 2 and 8 extra per ~20,000-document
> shard, so roughly **1 document in 2,500**). Web-scraped text simply contains the string
> `<|endoftext|>`, and `tokenizers` parses it as id 100257 wherever it appears.

So question (c) is not speculative for five Stage-1 sources — it is **MEASURED, and it already
took down five live bundles.** The current mitigation is one rewrite pair only:

```python
_BOUNDARY_MARKER_REWRITES = (("<|endoftext|>", "<| endoftext |>"),)

def neutralize_boundary_markers(text: str) -> str:
    if "<|" not in text:          # <-- THE GUARD. Only strings containing "<|" are scanned.
        return text
    for literal, replacement in _BOUNDARY_MARKER_REWRITES:
        text = text.replace(literal, replacement)
    return text
```

Two properties of the existing implementation matter enormously for the new sources:

1. **The `if "<|" not in text` fast-path guard means any marker NOT starting with `<|` is
   structurally invisible to this function.** `</s>`, `<s>`, `<|endoftext|>`-lookalikes with
   different casing, and `<eos>` all fail the guard and return unmodified. The docstring is
   explicit that this is by design ("the only prefix any rewrite starts with"). **Adding a
   non-`<|` literal to the table without relaxing the guard is a silent no-op** — the table grows,
   the behavior does not change, and the tests (which check the table) still pass. Grade: MEASURED
   by reading the code.
2. **The code's own justification for rewriting only the boundary id is correct and should be
   preserved**: dolma2 defines 22 added tokens, all 22 parse from raw text (the comment says
   "verified by execution"), but only id 100257 is the EOS that OLMo-core uses for
   `(mmap == eos_token_id).nonzero()`. The other 21 are ordinary in-vocab ids — "unusual, not
   dangerous". So the fix is NOT "neutralize every special token", it is "neutralize every literal
   that tokenizes to **100257**".

**Corollary that governs sections 1–13 below.** For every new source the question is narrow and
answerable: *does this source's text contain a literal that dolma2 maps to id 100257?* For dolma2
(a cl100k-derived BPE) the only such literal is `<|endoftext|>`. Non-dolma2 markers (`</s>`,
`<|im_start|>`, `<|eot_id|>`) do **not** produce a phantom boundary under dolma2 — they tokenize to
several ordinary ids. They are a *quality* problem (chat-template scaffolding leaking into a
pretraining corpus), not a *boundary* problem. **This distinction is load-bearing and I have not
seen it stated anywhere in the plan docs; treat any doc that says "neutralize `</s>`" as confusing
the two.** Grade: DERIVED from the dolma2 added-tokens list + the code comment.

### 0.3 Footer-checkability is already proven for most of Stage 1 (question f)

The prior wave answered (f) affirmatively and at scale, using HTTP-Range reads of parquet footers
and gzip ISIZE trailers, with **zero** payload download:

| source | what was footer-measured | figure |
|---|---|---|
| `finepdfs-edu` eng_Latn | 100 of 100 files, `text` column chunks | 575,753,674,201 text bytes; 49,526,501 rows; mean 25,007 B/doc |
| `finewiki` en | `text` vs `wikitext` both totalled | text 40,630,233,930 B vs wikitext 66,702,673,537 B (**ratio 1.6417** — picking `wikitext` inflates 1.64x) |
| academic (3 Common Pile) | 109 files, 679 row groups, 10.68 M docs | 367.9 GB of text, from **7.1 MB of footers** |
| `stackv2_edu_filtered` | ISIZE all 95 shards | 364,981,987,035 uncompressed JSON B × 0.69609 text fraction = 254,595,402,218 text B |
| `stackexchange_filtered` | footers, all trees | 104,984,965,100 text B exact |
| finephrase ×4 | **nested-leaf** footers at `rollout_results.list.element.text` | per-config pools 87.0–148.5 B tokens |

**The nested-leaf footer technique is the reusable tool.** `artifacts/recount/_fp_footer_leaf.py`
matches column chunks on `path_in_schema == 'rollout_results.list.element.text'` and the artifact
says why explicitly: *"never on `md.schema.names.index('text')`, which returns the ORIGINAL because
'text' appears twice in the flat leaf list."* That is the FinePhrase trap caught at the footer
level, and the same predicate is how any new source with a nested payload must be read.

**An encoding guard the prior wave invented and that must be reused:** footer
`total_uncompressed_size` equals text bytes **only for PLAIN-encoded** column chunks. The academic
artifact says: *"Verified the `text` column is PLAIN-encoded, not dictionary-encoded, for 668/679
row groups. A dict-encoded chunk's `total_uncompressed_size` is dictionary+indices and does NOT
equal text bytes."* Any new footer measurement must re-check `encodings`.

### 0.4 Two prior-wave corrections that bear on encoding

- **Common Pile "tokens" is arithmetic, not a count** (MEASURED): the paper's token column is
  `Size(GB) × 0.25` exactly (verified on Table 7: 182.6→45.65, 147.1→36.77, 19.5→4.88). So no
  Common Pile row tells you anything about its own tokenizer, and none of them is pre-tokenized.
- **`recount.py`'s `text_column: None` in `code-stackv2-edu-filtered.json`,
  `qa-stackexchange_filtered.json`, `math-swallow-math-v2.json` (config `-qa`) and
  `web-dclm-dedup.json` means the tool could NOT choose a column** — those are the four Phase-0
  rows where the column question was left open and later settled from bytes (or, for
  `swallow-math-v2-qa` and `dclm-dedup`, never settled; both are excluded from the plan).

---


## 1. DCLM — `HuggingFaceFW/dclm_100BT` and the full DCLM-baseline

### 1a. Which repo actually holds 744B+, and in what format

Three DCLM repos, and the format differs between them. **This is the trap: the two that hold the
full corpus are NOT parquet-and-`text` the way the 100BT sample is.**

| repo | format | rows | repo bytes | usable? |
|---|---|---|---|---|
| `HuggingFaceFW/dclm_100BT` | **parquet** under `data/` | 89,269,902 (EXACT, `/size` `partial:false`) | 316,008,772,992 (parquet) | ✅ the only fully-converted one |
| `mlfoundations/dclm-baseline-1.0` | **`.jsonl.zst`** — 27,838 shards | ~2.95–3.02 B (estimate only) | 7,196,105,155,016 | ⚠️ needs zstd |
| `mlfoundations/dclm-baseline-1.0-parquet` | parquet — 27,938 shards, same 10×10 layout | ~2.73–3.02 B (estimate) | 7,419,668,271,828 | ✅ format-wise |

- **MEASURED (prior wave, `artifacts/recount/web.json`):** `mlfoundations/dclm-baseline-1.0` ships
  `.jsonl.zst`. Verified from bytes — zstd magic `28 b5 2f fd` at
  `global-shard_01_of_10/local-shard_0_of_10/shard_00000000_processed.jsonl.zst`. The registry's
  earlier row claimed `parquet` under a `default` config **that does not exist**, and
  `corpus_read` refuses zstd (no `zstandard` dependency declared) — *"that row was a 30B hole in
  the corpus."*
- **CARD:** the `mlfoundations/dclm-baseline-1.0` card format facet says `json` and the Hub adds
  "Auto-converted to Parquet" — which is the **Hub's derived 5 GB-capped copy, not the original
  files**. Reading the format facet instead of the bytes is exactly how the wrong-format registry
  row got written.
- **MEASURED:** `/statistics` for `mlfoundations/dclm-baseline-1.0` returns **HTTP 501 "Job manager
  crashed"** permanently (re-confirmed, independent of rate limiting), and `/size` returns
  `num_rows=779,982` with `partial:true` — that 780k is the **converted head, 0.03% of the corpus**.
  A pipeline that reads `num_rows` from `/size` here silently sizes the source at 1/3800 of reality.

**Does 744B+ exist?** The 100BT sample is **MEASURED at 114,691,544,533 dolma2 tokens** — so it
alone cannot reach 378B. The full corpus is **DERIVED at 3,764 B dolma2 tokens** (2,949.3 M docs ×
5,461.0 mean chars × 0.2337 measured dolma2 tokens/char, CV 0.111), cross-checked against Zyphra's
independent 3,854.9 B gpt-neox to 2.4%. So **yes, ≥744B exists — but only in a repo whose row count
is an estimate and whose `/statistics` is permanently broken.** For 378B you must read
`mlfoundations/dclm-baseline-1.0-parquet` (parquet, no zstd dependency) — NOT the `.jsonl.zst`
original, and NOT the 100BT sample.

**⚠️ A provenance loss in the parquet mirror that nobody flags as a hazard.** MEASURED (prior wave):
the parquet mirror has **6 columns vs the original's 8** — it drops the WARC metadata struct and
`warcinfo`. The card says only *"an identical copy … where all the files have been mapped to a
parquet format."* It is identical in `text`, not in provenance. If the ingest needs WARC lineage
(and CC-BY-4.0 attribution arguably wants it), the parquet mirror cannot supply it.

### 1b. Text field and document id

- `HuggingFaceFW/dclm_100BT` — **MEASURED from real bytes at pinned revision
  `01022d378d944de6deeb1c79d08fecb4d27b2c6f`:** parquet under `data/`, leaves exactly
  `['text','id','url','language','language_score','fasttext_score','dataset']`, 895,229 rows in the
  first of 96 files, sampled `text` is genuine web prose. `text` → payload, `id` → document id.
  **Single plausible text column. No nested trap.**
- `mlfoundations/dclm-baseline-1.0` — **CARD/viewer:** columns are
  `bff_contained_ngram_count_before_dedupe`, `language_id_whole_page_fasttext`, `metadata`,
  `previous_word_count`, `text`, `url`, `warcinfo`,
  `fasttext_openhermes_reddit_eli5_vs_rw_v2_bigram_200k_train_prob`. `text` is the payload.
  **🛑 THERE IS NO DOCUMENT-ID COLUMN.** The card designates none. The closest candidates are `url`
  (not unique — a URL recurs across snapshots) and, inside `metadata`, `WARC-Block-Digest` /
  `WARC-Record-ID`. **This is a real ingest blocker for the full corpus and it is undocumented:**
  our reservoir id partitioning (`sha256(id) % N`) and the FinePhrase-style anti-join both require
  a stable per-document id. Grade: CARD for the column list, **UNVERIFIED** for whether
  `metadata.WARC-Record-ID` is present on every row and unique.
  - *Settling job:* one parquet row-group range-read of the mirror, projecting only the `metadata`
    struct leaves, over ~200 row groups spread across shards; count nulls and distinct
    `WARC-Record-ID`. Metadata-scale, no payload.

### 1c. Encoding hazards

- **🛑 HTML ENTITIES ARE NOT DECODED. ISSUE/CARD-adjacent, and directly observed:** the
  `mlfoundations/dclm-baseline-1.0` card's own sample row *"visibly contains raw `&quot;`
  sequences."* The card says nothing about entity handling anywhere. DCLM's extraction is a
  reproduction of RefinedWeb heuristics; no ftfy, no `html.unescape` is documented at any stage.
  Grade: **ISSUE** (observed in the card's rendered sample) for entity survival; **CARD** for the
  absence of any documented normalization step. Contrast this sharply with FinePDFs, which
  explicitly runs FTFY (§4).
- **No documented unicode normalization, no documented mojibake repair.** The card's entire
  cleaning description is three bullets: *"Heuristic cleaning and filtering (reproduction of
  RefinedWeb)"*, *"Deduplication using a Bloom filter"*, *"Model-based filtering using a fastText
  classifier"*. **Nobody documents whether DCLM text is valid UTF-8, whether lone surrogates or NUL
  bytes survive, or whether BOMs were stripped. This is a finding.**
  - *Settling job:* the cheapest possible — while tokenizing, count per shard:
    `text.encode('utf-8','surrogatepass') != text.encode('utf-8')` (lone surrogate),
    `'\x00' in text`, `text.startswith('﻿')`, and a regex count of `&(amp|lt|gt|quot|#\d+|#x[0-9a-f]+);`.
    Zero extra passes over the data — these are cheap predicates on text you are already holding.
    Emit as a per-shard receipt counter.
- **Truncation:** no policy stated on any DCLM card. Observed `text` ranges are viewer-display
  artifacts only. Grade: UNVERIFIED, but low risk — DCLM's pipeline is document-level filtering,
  not windowing.

### 1d. Already tokenized? No.

Plain text, both repos. The "4T token" figure names **no tokenizer** (CARD). Zyphra independently
counted the same documents with **gpt-neox** (CARD, on `Zyphra/dclm-dedup`), which is where the
`dolma2/gpt-neox = 0.9764` ratio came from. Nothing is pre-tokenized.

### 1e. Boundary markers: **YES, NEEDED — MEASURED, this source already broke a live bundle**

`corpus_pack.py` names `dclm` as one of the **five bundles that failed live** on extra EOS
occurrences (~1 doc in 2,500). Cover `<|endoftext|>`. The existing
`neutralize_boundary_markers()` already handles it. **No new literal needed for DCLM** —
web-scraped text quoting `<|endoftext|>` is exactly the documented case.

### 1f. Footer-checkability: partial

- `dclm_100BT`: yes — exact rows from `/size` (`partial:false`), parquet footers give `text` column
  bytes per row group. Prior wave already did the tokens/char measurement (0.22847, CV 0.1111).
- Full corpus: **document count is NOT independently checkable** — `/size` gives an estimate,
  `/statistics` is 501, and the three available figures (3.018 B estimate, 2.949 B Zyphra, "3B"
  card) disagree by 2.3%. **Row counts ARE recoverable from parquet footers** (`num_rows` per row
  group, no payload) across 27,938 files — that is a ~19 GB footer scan at ~680 KB/file, which is
  metadata-scale-ish but not free. Byte totals likewise.
  - *Recommendation:* if 378B is drawn from the mirror, footer-scan a **stratified 1,000 files**
    (~680 MB) rather than all 27,938, and get the exact row+byte totals by scaling on the exact
    per-file LFS sizes from the tree API — this is precisely the technique
    `artifacts/recount/synthetic.json` used for FinePhrase's 27,104 files.

---

## 2. `HuggingFaceFW/fineweb-edu`

### 2a. Repo, config, split, format, paths

- **MEASURED (registry, verified in bytes at pinned rev `87f09149ef4734204d70ed1d046ddc9ca3f2b8f9`):**
  parquet, config path `sample/100BT`, text `path_in_schema` = `text`, id = `id`.
- **CARD (README body, read verbatim this session):** configs are `default` (all data),
  one per CC dump (`CC-MAIN-2013-20` … `CC-MAIN-2025-26`, 110 of them), plus
  `sample-350BT`, `sample-100BT`, `sample-10BT`. Row counts from the viewer: `default` 1.53 B rows,
  `sample-350BT` 339 M, `sample-100BT` 97.3 M, `sample-10BT` 9.67 M.
- **🛑 THE SAMPLE SIZES ARE GPT2 TOKENS, VERBATIM FROM THE CARD:** *"`sample-350BT`: a subset
  randomly sampled from the whole dataset of around 350B **gpt2** tokens"* (same wording for 100BT
  and 10BT). So "100BT" is a gpt2 promise, not a dolma2 one. Prior wave MEASURED `sample-100BT` at
  **100.24 B dolma2 tokens** (registry `pool_tokens`) — so gpt2 and dolma2 happen to agree within
  0.3% here, but that is an empirical coincidence per corpus, not a rule. Grade: CARD for gpt2,
  MEASURED for the dolma2 figure.

### 2b. Does the `sample-350BT` / full-split question matter for 252B?

**Yes, decisively, and not for the reason of size.** CARD, verbatim: *"`sample-10BT` was sampled
from `sample-100BT` which in turn was sampled from `sample-350BT`."* So the samples are **strictly
nested**. And the registry carries the 🛑 trap:

> 100% of this source has a synthetic sibling: sample-100BT ⊂ sample-350BT, which is FinePhrase's
> exact parent. Draw synthetic from the ~242M sample-350BT ids NOT in sample-100BT, or every
> edu-web document appears twice in one run.

**252B cannot come from `sample-100BT` (100.24 B dolma2).** It must come from `sample-350BT`
(~350 B gpt2) or `default` (1.3 T). **And the moment you draw 252B of the 350BT sample, the
anti-join against FinePhrase's parent becomes impossible** — you would be consuming ~72% of exactly
the pool FinePhrase was rephrased from, so most FinePhrase documents' originals are also in the
corpus. Grade: DERIVED from two MEASURED/CARD facts. **This is a plan-level consequence, not an
encoding one, but it is the largest silent-corruption risk on this source and it changes with the
252B target.**

### 2c. Text field: one plausible column, but two token-count columns

Schema (CARD, viewer): `text` (payload, 150–592k chars), `id` (`<urn:uuid:...>`, 47 chars), `dump`,
`url`, `date` (**null in all shown rows**), `file_path`, `language`, `language_score`,
`token_count`, `score`, `int_score`. Only one text column — **no FinePhrase-shaped trap.**
`token_count` is a per-row **gpt2** count (DERIVED from the sample-naming statement + the FinePDFs
sibling card which says its `token_count` is gpt2/Llama3.2); do NOT sum it and call it a dolma2
figure. Prior wave (`artifacts/recount/synthetic.json`) independently MEASURED that
`dolma2 tokens / FineWeb-Edu's own token_count = 0.9897–0.99905` across the four FinePhrase
configs — i.e. within 1% of unity, so the column is *usable as a cross-check* but is not a dolma2
count.

### 2d. Encoding hazards — the card documents NONE, and that is the finding

**CARD:** the README body (read in full this session) describes annotation with Llama3-70B-Instruct,
classifier training on Snowflake-arctic-embed, and score-3 filtering. **It contains zero statements
about text extraction, HTML entity decoding, unicode normalization, ftfy, encoding repair, BOMs,
NULs, or surrogates.** Extraction is inherited from FineWeb (trafilatura, per the FineWeb paper
arXiv 2406.17557) — but **`fineweb-edu`'s own card never names the extractor**, so any claim about
trafilatura here is at best CARD-of-a-different-repo. Grade: **UNVERIFIED for every encoding
hazard.** Same one-off settling job as §1c — free counters during tokenization.

One card statement is a genuine quality warning worth recording: *"it is likely that code content
is not prevalent in our dataset"* and *"wikipedia content included in FineWeb … we did not tailor
the processing to individual websites"* — i.e. no per-site cleanup, so site-specific furniture
survives.

### 2e. Boundary markers: **YES, NEEDED — MEASURED, broke a live bundle**

`fineweb-edu` is named in `corpus_pack.py` as one of the five bundles that failed live on extra
EOS. Cover `<|endoftext|>`; already handled.

### 2f. Already tokenized / truncated

Not tokenized (plain text). No truncation policy stated (UNVERIFIED); the 150-char floor and
`score ≥ 2.52` are filter thresholds, and `token_count` maxes at 159k in the viewer, which is far
above any plausible window — so mid-document truncation is unlikely. The `date` column being
**empty in all shown rows** is a real (if harmless) card-vs-schema mismatch worth not depending on.

### 2g. Footer-checkability: yes

Parquet. `num_rows` per row group and `text` column `total_uncompressed_size` both come from
footers. Prior wave used exactly this on the sibling `finepdfs-edu` (100/100 files). **Re-check
`encodings` for PLAIN vs RLE_DICTIONARY** (§0.3) before trusting a footer byte total.

---

## 3. CODE — `common-pile/stackv2_edu_filtered` and the full `common-pile/stackv2`

### 3a. Repo, format, paths

Both are `.json.gz` (Dolma-format JSONL, gzipped), shards at the **repo root** — no `data/` prefix,
no config.

| repo | shard prefix | docs (CARD) | UTF-8 GB (CARD) | MEASURED text bytes (prior wave) |
|---|---|---|---|---|
| `common-pile/stackv2_edu_filtered` | `stack-edu-NNNN.json.gz` | 69,588,607 | 255 | **254,595,402,218** (ISIZE all 95 shards × 0.69609 text fraction, CV 0.064) |
| `common-pile/stackv2` (raw) | `documents/*.gz` (see below) | 218,364,133 | 4,774.7 | 4,774,700,069,355 |

**⚠️ The two repos differ in layout, not just size.** `stackv2`'s README front-matter declares
`data_files: - split: train, path: - documents/*.gz` — a **`documents/` subdirectory**. The
`_filtered` repo has its shards at the root with the `stack-edu-` prefix (MEASURED from the tree at
pinned rev `c354dbe88469a1153e97c6a63ac50591849654de`). **A code path that hardcodes one layout
silently lists zero files against the other, and a zero-file source is a silently empty category.**

- **MEASURED:** `text` holds source code and markdown directly. From `artifacts/recount/code.json`,
  verbatim: *"Confirmed by reading rows: `text` holds source code and markdown directly. No SWHID
  indirection, so this is the correct route around the Software Heritage bulk agreement."* **This
  is the load-bearing reason to use `common-pile/stackv2*` and NOT `bigcode/the-stack-v2`** —
  the latter ships SWHIDs only (pointers), and a naive ingest reading `bigcode/the-stack-v2` gets a
  corpus of hashes, not code. Grade: MEASURED for common-pile; CARD for the SWHID claim about
  bigcode.
- Text path `text`, id path `id`, license in `metadata.license` (CARD, and MEASURED per the registry
  note that Common Pile licenses are visible only per-row).

### 3b. Do you need the full `stackv2` for 90B? No — and using it would be a mistake.

`stackv2_edu_filtered` is **MEASURED at 74.81 B dolma2 tokens** (registry `pool_tokens`), against a
90B need. That is 83% of the target, so the gap is real but small. The full raw `stackv2` is
4,774.7 GB — **a 19x larger, unfiltered, and differently-licensed-in-practice pool.** Two reasons
not to reach for it:

1. Prior wave labelled it under `raw_upper_bounds_do_not_use_as_pool_sizes` explicitly.
2. `stackv2_edu_filtered`'s pitch is that all licenses are Blue Oak certified; the raw repo's card
   makes the *same* license claim, so the difference is the edu filter, not licensing.

**Recommendation:** close the 90B−74.81B gap by *not* closing it (take 74.81B and rebalance) or by
adding `common-pile/stackv2_html_filtered` (named on both cards as the sibling filtered release,
**UNVERIFIED** size). Do NOT ingest raw `stackv2`.

### 3c. Encoding hazards — code is the worst case for two of them, and one is severe

- **🛑 NUL BYTES AND INVALID UTF-8 ARE MOST LIKELY HERE OF ANY SOURCE.** The Stack v2 upstream
  contains binary-ish and mixed-encoding files (minified JS, files declaring latin-1/shift-JIS,
  vendored binaries with source extensions). Common Pile's card advertises "UTF-8 GB" as its size
  unit, which implies a UTF-8 decode happened — but the card makes **no statement** about what was
  done with undecodable bytes, NULs, or lone surrogates. Grade: **UNVERIFIED, and this is the
  single most likely place in the corpus for a NUL byte to reach the tokenizer.** A NUL in text is
  not a tokenizer error under `tokenizers` (it encodes to ordinary bytes) so **nothing fails** —
  you just get a corpus with NULs in it, which later breaks any consumer doing C-string handling.
- **🛑 73 DISTINCT LANGUAGES IN ONE SHARD, AND THE VALUE GOES IN A PATH.** MEASURED (registry):
  *"'C#' publishes clean and passes Gate A, then urlparse puts everything after the '#' in
  `fragment` and THE SHARD NAME LEAVES THE PATH."* Gate A now rejects `#` and brackets
  (`validate._segment_breakage`); other values still need `slug_path_segment`. Mitigation is
  already specified: `'C#'→'c-sharp'`, `'C++'→'c-plus-plus'`, fold to top ~20 by token count and
  the rest to `other`. **This is an encoding hazard of the *label*, not the text — but it is the
  one that has already been observed to corrupt an S3 address.**
- **Literal `\n` two-character escapes:** in a JSONL source, `\n` inside a JSON string is the
  standard escape and `json.loads` turns it into a real newline. The hazard is the *opposite*
  direction: **code text legitimately contains the two-character sequence `\n`** (in every C, Java,
  Python, JS string literal). So any "fix double-escaped newlines" heuristic applied to code will
  **corrupt real source code**. Grade: DERIVED. **Mitigation: never apply an unescape heuristic to
  this source. Trust `json.loads` and stop.**
- **No mojibake repair, no ftfy, no entity decoding documented.** Correct for code — entity
  decoding would corrupt HTML/XML source files, which are legitimately in a code corpus.

### 3d. Already tokenized? No.

Plain text. Common Pile's "tokens" column is **`Size(GB) × 0.25` arithmetic, MEASURED by the prior
wave against paper Table 7** — it is not a count and names no tokenizer.

### 3e. Boundary markers: **YES — MEASURED, `stackv2-edu` is one of the five bundles that failed live**

Cover `<|endoftext|>`. Already handled by `neutralize_boundary_markers()`.

**⚠️ But code is also the source most likely to contain OTHER special-token literals as ordinary
text** — a Python file in the Stack that builds a chat template will literally contain
`"<|im_start|>"`, `"<|endoftext|>"`, `"</s>"`, `"<|eot_id|>"` as string literals. Under dolma2:
- `<|endoftext|>` → **id 100257, the EOS. PHANTOM BOUNDARY. Must be neutralized.**
- `<|im_start|>` / `<|im_end|>` → ids 100264/100265, ordinary in-vocab added tokens. Not a
  boundary. **Do not rewrite** — you would be modifying real source code to fix a non-problem.
- `</s>`, `<s>`, `<|eot_id|>` → not in dolma2's added-token list at all; tokenize to several
  ordinary BPE ids. **Not a boundary. Do not rewrite.**

Grade for the dolma2 id assignments: **MEASURED** (read from
`allenai/dolma2-tokenizer/tokenizer_config.json` this session — full 22-token list is in §14).

### 3f. Footer-checkability: YES, and it was already done

`.json.gz` has no parquet footer, but **the gzip ISIZE trailer (last 4 bytes) gives the exact
uncompressed size mod 2^32**, and the prior wave used exactly that: *"= 364,981,987,035 exact
uncompressed JSON bytes (ISIZE, all 95 shards) × 0.69609 measured text/json fraction (CV 0.064
across 95 shards)."* Cost: 8 bytes per shard. **Document count is NOT footer-derivable from
`.json.gz`** — you must either trust the card (69,588,607) or count newlines, which needs a full
read. Prior wave got rows from one `/size` call.

**The `text/json` structural fraction is the reusable calibration** and it differs per source:
stackv2_edu 0.69609 (CV 0.064), github_archive 0.77809 (CV 0.012), swallow-code-v2 **0.22814**
(because each row carries four near-duplicate copies of the code plus an LLM evaluation blob).
**That 0.228 is the warning: a JSONL row is not mostly text, and assuming it is overstates a pool
4.4x.**

---

## 4. `HuggingFaceFW/finepdfs-edu` — the highest-risk source, and the card is unusually good

### 4a. Repo, config, split, format, paths

- **MEASURED (registry + `artifacts/recount/_footer-finepdfs-edu.json`, pinned rev
  `9cfabe2127faca99b3d5c4dc6d1fcb397399ebde`):** parquet at `data/eng_Latn/train`, text
  `path_in_schema` = `text`, id = `id`. Verified against the live schema: **`eng_Latn` has 10
  string columns and `text` is the only payload one** (next-longest values are `file_path` ~109
  chars and `url` ~68). **No nested payload, no FinePhrase-shaped trap.**
- Footer scan MEASURED: 100/100 files, 575,753,674,201 corrected text bytes, 49,526,501 rows,
  **mean 25,007 bytes/doc** — an order of magnitude longer than web documents.
- **CARD (parent `finepdfs` README, read in full this session):** the schema is
  `text`, `id`, `dump`, `url`, `date`, `file_path`, `offset`, `language`, `per_page_languages`,
  `page_average_lid`, `page_average_lid_score`, `full_doc_lid`, `full_doc_lid_score`,
  `is_truncated`, `processor`, `page_ends`, `token_count`.
- Config naming is `<ISO639-3>_<Script>`, e.g. `eng_Latn`. **1,733 language-script pairs.** Most
  languages also ship a **`test` split which the card says must not be trained on** — a real
  ingest hazard if you glob `data/eng_Latn/**`.
- **⚠️ Version pinning matters here more than anywhere.** CARD changelog: *"v1.5.0 (11-11-2025):
  Classifier labels added (DCLM, EDU, EDU-V2, OCR-QUALITY), fixed CommonCrawl paths, and
  **corrected misalignment of labels (docling vs rolmOCR)**."* So a pre-v1.5.0 revision has
  **mislabelled `processor` values**. Our pinned revision is dated 2025-11-11 — the same day as
  v1.5.0 — so it is *probably* the fixed one, but that is DERIVED, not verified.

### 4b. 🛑 Extraction artifacts — the card answers most of the brief's questions DIRECTLY

This is the one source where the card documents its postprocessing. **CARD, verbatim:**

> For the **Docling pipeline**, we removed page-number tags while preserving genuine singleton
> numbers, cleaned tables by dropping empty rows and columns, and discarded malformed image
> annotations with an alpha-to-all-character ratio `<= 0.8`. We then applied a boilerplate detector
> to strip repetitive content from page headers and footers. Finally we applied
> [FTFY](https://pypi.org/project/ftfy/) to fix encoding issues 🔧.

> For the **RolmOCR pipeline**, we removed pages that ran out of context, were detected to contain
> repeated content, or failed entirely. During analysis, we noticed that **pages with no or very
> little text often produced hallucinated content**; to address this, we used VLM to detect and
> discard such cases. As in the Docling pipeline, we concluded by applying boilerplate detection to
> remove repetitive headers and footers and applying FTFY.

Mapping to the brief's PDF checklist:

| hazard asked about | status | grade |
|---|---|---|
| header/footer repetition | **handled upstream** — boilerplate detector, both pipelines | CARD |
| ligatures ﬁ/ﬂ | **handled upstream, as a side effect of FTFY** — `fix_latin_ligatures=True` by default | DERIVED (CARD says FTFY; ftfy docs MEASURED for the default) |
| mojibake / double-encoding | **handled upstream** — FTFY `fix_encoding=True` default | DERIVED, same basis |
| lone surrogates | **handled upstream** — FTFY `fix_surrogates=True` default | DERIVED |
| control chars / terminal escapes | **handled upstream** — FTFY `remove_control_chars=True`, `remove_terminal_escapes=True` | DERIVED |
| line-break normalization to `\n` | **handled upstream** — FTFY `fix_line_breaks=True` | DERIVED |
| unicode normalization | **NFC applied upstream** — FTFY `normalization="NFC"` default | DERIVED |
| HTML entities | **`unescape_html="auto"`** — decodes entities *unless* a literal `<` appears in the segment, in which case it skips | DERIVED — **so entity survival is possible in exactly the segments that also contain `<`** |
| **soft hyphen / de-hyphenation at line breaks** | **NOT MENTIONED ANYWHERE. FTFY does not do this.** | **UNVERIFIED — a genuine gap** |
| **column interleaving / reading order** | **NOT MENTIONED as an artifact.** Docling runs a layout model (`docling-layout-heron`, int8-quantized) which is *how* reading order is determined, but the card never claims it is correct | **UNVERIFIED** |
| **math dropped to garbage** | **NOT MENTIONED.** RolmOCR is a VLM OCR model; Docling+PyMuPDF has no math handling described | **UNVERIFIED — and this matters because finepdfs-edu is a STEM-heavy pool** |

**The FTFY finding cuts both ways and this is the important part.** FTFY's defaults were read this
session from the ftfy docs (MEASURED for the defaults): `fix_latin_ligatures`, `fix_character_width`,
`uncurl_quotes`, `fix_line_breaks`, `fix_surrogates`, `remove_control_chars`,
`remove_terminal_escapes`, `fix_encoding`, `restore_byte_a0`, `replace_lossy_sequences`,
`decode_inconsistent_utf8`, `fix_c1_controls` are all **True by default**;
`unescape_html="auto"`; `normalization="NFC"`.

**⚠️ So: DO NOT run ftfy again on this source.** It is idempotent for most fixes but
`uncurl_quotes` and `fix_character_width` are **lossy and already applied** — re-running is
harmless, but *assuming they were not applied* and writing your own curly-quote or fullwidth
normalizer produces a source that is normalized differently from every other source in the corpus.
**The corpus-wide consequence: finepdfs-edu text is NFC-normalized, straight-quoted,
ligature-split, and halfwidth; DCLM/FineWeb-Edu text is none of those.** That is a systematic
per-source distribution shift that no validator sees. Grade: DERIVED, high confidence.

### 4c. 🛑 Truncation — this source HAS a documented truncation mechanism, in two places

1. **CARD:** *"The total context length of the model, including the input, was set to **8096
   tokens**"* for RolmOCR, and *"we removed pages that ran out of context"*. So OCR'd documents
   **lose whole pages** past the model's context. The document that remains is a *page-subset* of
   the PDF, not a truncated string — so it will not look truncated. **`page_ends` is the only
   signal, and comparing `len(page_ends)` to the true page count is impossible from the dataset.**
   Grade: CARD. This is the "truncated mid-thought but undetectable" case the brief asks about, in
   its worst form: **the discontinuity is at a page boundary in the middle of the document.**
2. **CARD:** *"Many of the PDFs are truncated in CommonCrawl … we first identified such documents"*
   and refetched them; unrefetchable ones were processed anyway with `is_truncated` flagged.
   **`is_truncated` is a per-row boolean and it is the single most actionable field on this
   source.** Nobody in our plan docs mentions filtering on it.
   - **Mitigation: filter `is_truncated == False`, or at minimum record its rate per shard.** Cost
     is one extra projected column at read time. **UNVERIFIED what fraction of `eng_Latn` is
     `is_truncated=True`** — settle it with a footer/column-chunk read of just that boolean column
     (it is 1 byte/row, so ~50 MB for all 49.5 M rows, or use the parquet **column statistics**
     min/max/null_count per row group for free).

Also **CARD:** *"pages with no or very little text often produced hallucinated content"* — they
filtered the cases they detected. **Undetected VLM hallucination is present in this source by the
card's own admission**, at an unstated rate.

### 4d. Boundary markers: **probably yes, and for a NEW reason nobody has flagged**

- `finepdfs-edu` is **NOT** among the five bundles that failed live on `<|endoftext|>` — but that
  is because the reservoir build drew from it *and* the failure rate is ~1/2,500, so absence is
  weak evidence. PDFs of ML papers and documentation absolutely contain `<|endoftext|>` in prose.
  **Include it. Cost is a substring scan.**
- **🛑 THE NEW RISK IS ROLMOCR OUTPUT.** RolmOCR is a fine-tuned VLM. VLM OCR output can emit its
  own chat/EOS scaffolding when it degenerates — the card confirms degeneration happens
  ("hallucinated content", "repeated content"). **UNVERIFIED whether any RolmOCR-processed document
  contains its template tokens verbatim.** RolmOCR is Qwen2.5-VL-derived, whose markers are
  `<|im_start|>`, `<|im_end|>`, `<|vision_start|>`, `<|endoftext|>`.
  - Under dolma2: `<|im_start|>`/`<|im_end|>` are ordinary ids 100264/100265 (not boundaries);
    **`<|endoftext|>` IS the boundary.** So the mitigation is the one you already have.
  - *Settling job, and it is nearly free:* while ingesting, count documents where
    `'<|' in text`, split by the `processor` column. If rolmOCR rows are enriched relative to
    docling rows, you have found VLM scaffolding leakage and can quantify it. **This is a two-line
    counter and it answers a question nobody upstream has asked.**

### 4e. Already tokenized? No — but two `token_count` columns exist and the card contradicts itself

**CARD, and this is a real internal contradiction in the finepdfs card:**
- Data Fields section: *"`token_count` (int): number of tokens when applying the **`gpt2`**
  tokenizer to this sample"*
- Annotations section, ~40 lines later: *"`token_count` is generated by applying the **LLama3.2**
  tokenizer to the `text` column"*
- And the ablation section: models trained *"tokenized using the [Llama-3.2] tokenizer"*.

**These cannot both be true.** Grade: **CARD, self-contradictory — so `token_count` names no
tokenizer reliably and must not be summed as a token figure.** Prior wave sidestepped this entirely
by measuring dolma2 tokens/byte directly. Keep doing that.

### 4f. Footer-checkability: YES — already done at full coverage

100/100 files scanned, 575,753,674,201 text bytes, 49,526,501 rows. Re-check `encodings` for PLAIN
before reusing (§0.3). The per-row booleans (`is_truncated`) and the `processor` string are
additionally checkable via **row-group column statistics** at zero payload cost — an
under-exploited capability.

---

## 5. `nvidia/Nemotron-CC-Math-v1` — the math-notation source, and it is Phi-4 output

### 5a. Repo, config, split, format

- **CARD:** *"The datasets has 3 subsets: **3** (documents with quality label 3), **4plus**
  (documents with quality labels 4 and 5) and **4plus_MIND**."*
- **🛑 "3+" IS NOT A LOADABLE CONFIG.** CARD: to get the 3-plus tier you must *"load both 3 and
  4plus subsets."* The brief's "the '3+' quality subset" therefore names a **union of two configs**,
  not a config. An ingest row that says `config="3plus"` fails to resolve. Grade: CARD.
- Format: **parquet** (metadata sidebar). **Split names are never stated on the card.** Grade: CARD
  for parquet, UNVERIFIED for splits.
- **🛑 THE SCHEMA IS NOT AVAILABLE WITHOUT AUTH.** MEASURED this session:
  `datasets-server/info?dataset=nvidia%2FNemotron-CC-Math-v1` returns
  `"The dataset does not exist, or is not accessible without authentication (private or gated)"`.
  The dataset is **gated** ("you have to accept the conditions to access its files and content").
  The card says *"you can download subsets of the data based on the metadata schema described
  above"* — **and no such schema appears on the card.** So:
  **text `path_in_schema`: UNVERIFIED. id `path_in_schema`: UNVERIFIED.** This is the only Stage-1
  source whose text column we cannot name. **It must be settled before any ingest code is written.**
  - *Settling job:* accept the gate, then one authenticated parquet-footer read of a single file
    (~700 KB) gives the full leaf list. Trivial, but it requires a human to accept a license.
- Counts (CARD, **no tokenizer named**): `nemotron-cc-math-3plus` = 133 B tokens / 101.15 M docs;
  `nemotron-cc-math-4plus` = 52 B / 45.10 M; `nemotron-mind-v1` = 73 B / 88.73 M. Against a 45 B
  need, `4plus` alone (52 B, CARD) is marginal and **3plus (133 B) is the safe draw** — but 133 B
  is a card figure with an unnamed tokenizer, so **DERIVE a dolma2 figure before committing.**
- License: **"other" — NVIDIA Open Data License Agreement**, gated, with the stated intent
  restriction *"I intend to use this data for model training purposes only."* **And a downstream
  condition:** since the data *"is created using phi-4 model"*, resulting models *"may be subject to
  redistribution and use requirements"* in the **Phi-4 License Agreement**. Grade: CARD. Compare
  the memory note about Nemotron's §2.2.2 blocking a shared reservoir — **this is a second,
  different license entanglement on a different Nemotron repo and it should be read before use.**

### 5b. 🛑 Math notation: LaTeX, and it is LLM-normalized — the answer, with a caveat

**CARD, verbatim:** the LLM cleaning step *"standardizes mathematical expressions into consistent
**LaTeX**"*, using *"a lightweight LLM (**Phi-4, 14B**)"*. The rendering step *"convert[s] HTML into
structured text while preserving equations and layout."* Motivation given: prior corpora had
*"Missing or corrupted equations"* and *"Lossy HTML-to-text conversions."*
**No mention of MathML. No mention of unicode math symbols.** Grade: CARD.

**So: LaTeX. That is the answer to the brief's key question.** Three consequences:

1. **LaTeX is dense in backslashes and braces, which changes tokens/byte substantially.** Prior
   wave MEASURED finemath at **2.56 chars/token** under dolma2 (from
   `artifacts/reservoir/chars-per-token.json`) versus fineweb-edu at 4.62 — math is already the most
   token-dense source we have, and heavily-LaTeX'd math will be denser still. **`_CHARS_PER_TOKEN`
   is a reader stopping rule and it must be measured for this source before a build is planned**
   (§09-tokenizer-decision.md documents that getting it wrong leaves the last shard unfilled and
   `verify` refuses the bundle, costing a whole re-run).
2. **Escaping:** LaTeX text in a JSON/parquet string is fine, but a LaTeX `\n` sequence
   (`\newcommand`, `\nabla`, `\neq`) begins with a literal backslash-n. **Any "convert literal \n
   to newline" heuristic destroys LaTeX.** Same rule as code (§3c): **never unescape.** Grade:
   DERIVED.
3. **`lynx` did the HTML→text conversion.** CARD: *"Instead of relying on brittle DOM parsing, we
   use `lynx` to convert HTML into structured text."* **The card does not enumerate lynx's
   artifacts.** lynx is a *text browser*: its known output characteristics are **hard-wrapped lines
   at a terminal width, bracketed link reference markers (`[1]`, `[BUTTON]`, `[IMG]`), and tables
   flattened to aligned spaces.** Grade: **UNVERIFIED whether these survive** — the Phi-4 pass is
   claimed to *"remove boilerplate"* and improve formatting, which may or may not strip them.
   - *Settling job:* one `/first-rows` call after accepting the gate; grep 20 documents for
     `^\s*\[\d+\]`, `[IMG]`, `[BUTTON]`, and lines hard-wrapped at a constant width. Five minutes.
   - **Why it matters and nobody says so: hard-wrapped lines are a tokenization tax and a
     distribution shift.** A corpus where one 45 B source is wrapped at 80 columns and every other
     source is not teaches the model that newlines arrive every ~12 tokens in math contexts.

### 5c. 🛑 Boundary markers: **YES, and this is the highest-confidence NEW finding in this audit**

**The text is Phi-4 output. Phi-4's tokenizer defines `<|endoftext|>` at id 100257 — the SAME
STRING AND THE SAME ID as dolma2's EOS.** Both are cl100k-derived.

MEASURED this session, from the two `added_tokens.json`/`tokenizer_config.json` files:

| | dolma2 | Phi-4 |
|---|---|---|
| `<|endoftext|>` | **100257** (eos_token AND bos_token) | **100257** |
| `<|im_start|>` | 100264 | 100264 |
| `<|im_end|>` | 100265 | 100265 |
| `<|endofprompt|>` | 100276 | 100276 |
| `<|fim_prefix/middle/suffix|>` | 100258/59/60 | 100258/59/60 |

**Why this is severe rather than theoretical.** A generating LLM's most common failure mode is
emitting its own stop token as text — and vLLM/TensorRT-LLM pipelines that use
`skip_special_tokens=False`, or that detokenize with the special tokens preserved, write
`<|endoftext|>` and `<|im_end|>` into the output string. The card documents **no** post-generation
scrubbing of special tokens. So:

- `<|endoftext|>` in a Nemotron-CC-Math document → **id 100257 → a phantom document boundary in
  our corpus.** Our existing `neutralize_boundary_markers()` catches it. **This source MUST be run
  through it.** Grade: DERIVED (from MEASURED id equality + CARD absence of scrubbing), and it is
  the exact same mechanism as the five bundles that already failed.
- `<|im_end|>` → id 100265, an ordinary added token. Not a boundary. **But it is a quality defect
  worth counting**, because its presence is direct evidence that generation scaffolding leaked, and
  its *rate* tells you whether `<|endoftext|>` leakage is likely too.
- *Settling job, nearly free:* during ingest, count per shard the documents containing
  `<|endoftext|>`, `<|im_end|>`, `<|im_start|>`, `<|endofprompt|>`. Emit to the receipt. **If
  `<|im_end|>` appears at any measurable rate, escalate — it means the generation pipeline did not
  strip specials and every marker is suspect.**

### 5d. Already tokenized? No — but it is LLM-*re*written, which is worse in one specific way

Not pre-tokenized (parquet text). But the text is **Phi-4 output detokenized from Phi-4's BPE.**
The brief asks about detokenization artifacts. For a cl100k-family tokenizer, `decode` is
byte-exact and **does NOT drop leading spaces or leak `Ġ`** (that artifact is specific to
naive GPT-2/RoBERTa-style decoders that map `Ġ`→space by hand, or to `convert_ids_to_tokens`
output being joined). Grade: DERIVED. **So `Ġ` leakage is NOT a plausible hazard for this source**
— but it *would* be for any source generated by a SentencePiece model and detokenized wrongly
(missing leading space after `▁` handling).

### 5e. Truncation: UNVERIFIED, and there is a specific reason to worry

CARD says nothing about max length or truncation. But the Phi-4 rewriting pass necessarily ran with
some `max_tokens`, and **a rewrite that hits its output cap ends mid-sentence with
`finish_reason="length"` — which this dataset does not ship** (FinePhrase does; see §6). So
**Nemotron-CC-Math may contain length-truncated rewrites with no field to detect them.** Grade:
UNVERIFIED. *Settling job:* sample 200 documents post-gate and count those ending without terminal
punctuation; compare against finemath as a control.

### 5f. Footer-checkability: yes, once the gate is accepted

Parquet. Blocked on auth today.

### 5g. Decontamination — carry the prior finding forward

Memory + `artifacts/1t-research/11-decontamination-audit.md`: **Nemotron-CC-Math left 13.2x more
contamination than it removed (verbatim GSM8K test at Jaccard 1.0).** The card claims *"LLM-based
decontamination against MATH, GSM8K, MMLU, MMLU-Pro"* (CARD). **The card's claim and our
measurement disagree, and ours is the measurement.** Not an encoding issue, but it belongs on any
ingest row for this source.

---

## 6. `HuggingFaceFW/finephrase` — the trap is CONFIRMED, and there are THREE more nobody named

### 6a. The nested column trap: CONFIRMED, MEASURED, and quantified

**CARD, verbatim from the Data Schema section (read this session):**

> - `id`
> - `text` (**source input text from FineWeb-Edu, not the generated output**)
> - `rollout_results` (list of generation result objects; one per rollout)
>   - each rollout object contains: `finish_reason`, `text` (generated transformed output; for
>     single-rollout runs this is in `rollout_results[0].text`), `usage`

And **CARD** front-matter: `source_datasets: - HuggingFaceFW/fineweb-edu/sample-350BT`, generator
`HuggingFaceTB/SmolLM2-1.7B-Instruct`, `Input column: text`.

**So the card itself documents the trap.** It is not hidden. What our prior wave added is the
MEASURED confirmation and the magnitude:

- **MEASURED (`artifacts/recount/synthetic.json`):** the top-level `text`'s sibling `dataset` field
  reads the literal string `'HuggingFaceFW/fineweb-edu'` in 34/34 rows sampled, and `token_count`
  is FineWeb-Edu's own count of the ORIGINAL (dolma2/`token_count` = 1.0091 on the original).
- **MEASURED, exactly one rollout per row, always:** `rollout_results` length min=max=1.0 over
  842,000 rows via `/statistics`, and rollouts_per_row min=max=1 in **every one of 160 row groups
  across all four configs (n=160,000)**. So `[0]` is the entire payload; no multi-rollout
  aggregation is needed. Grade: MEASURED.
- **MEASURED magnitude, corrected:** reading `text` instead of `rollout_results[0].text` overcounts
  by **faq 2.25x, tutorial 2.37x, math 3.80x, table 3.92x, whole corpus 2.90x** (478.15 B rewrite
  vs 1,386.8 B original). Phase 0's "27x" was wrong (it divided by a degenerate head sample);
  **the corrected multiplier is 2.90x and the severity is unchanged** — it substitutes 1.39
  trillion tokens of unrephrased real FineWeb-Edu for the synthetic pool.
- **🛑 AND THE FOOTER PATH IS VULNERABLE TOO.** MEASURED: `md.schema.names` returns a **flat
  17-name list containing `'text'` TWICE** (index 0 = the original, index 12 = the rewrite leaf), so
  `.names.index('text')` silently returns the ORIGINAL. Our own earlier tool `_footer_chars.py` did
  exactly that. **The fix, and it is the only correct predicate:**
  match on `path_in_schema == 'rollout_results.list.element.text'`.
- **And `rollout_results.text` does NOT raise** — it returns **zero columns**. So a wrong path fails
  silently in the *other* direction too: an empty source, which reads as a zero-token category.

### 6b. 🛑 NEW TRAP #1: `finish_reason` is shipped and it is the truncation detector — and we ignore it

The nested struct has a **`finish_reason` field** (CARD, and MEASURED: the footer for
`rollout_results.list.element.finish_reason` is a real column chunk, 78 B in the sampled row group —
tiny because it is dictionary-compressible, i.e. very few distinct values).

**CARD, generation config: `max_tokens=2048`, `model_max_context=8192`.** And **CARD, Limitations:**
*"Some long inputs can be truncated to satisfy context budgets."*

**Therefore:** any rewrite whose generation hit 2048 output tokens ends **mid-sentence**, and
`finish_reason` is the ONLY field that says so — almost certainly `"length"` vs `"stop"`.

- **Nobody in our plan docs, registry traps, or recount artifacts mentions `finish_reason`.** The
  registry's five FinePhrase traps cover the column, the cross-format duplication, short rewrites,
  and decontamination — **not truncation.** This is a genuine gap.
- Is the rate material? DERIVED from the MEASURED percentiles: p99 tokens/doc is
  faq 1,847 / math 1,372 / table 1,633 / tutorial 1,948. **So the 2048 cap sits just above p99 for
  faq and tutorial** — meaning roughly **0.5–1.5% of faq and tutorial rewrites are plausibly
  length-truncated**, and the fraction is *concentrated in the longest documents*, which carry
  disproportionate token mass. Grade: DERIVED, and it needs the direct check.
- **Mitigation, and it costs one extra projected column:** read
  `rollout_results.list.element.finish_reason` alongside the text and **drop rows where it is not
  `"stop"`.** Alternatively keep them but record the rate. **Doing neither means ~1% of the
  synthetic pool is mid-sentence fragments, and no hash, size, EOS-fraction, or decode check sees
  it** — this is exactly the shape of failure the brief asks for.
  - *Settling job, nearly free:* the parquet **column statistics** for that leaf give per-row-group
    min/max of the dictionary values; or read the one leaf for 20 row groups (~a few hundred KB)
    and histogram it.

### 6c. 🛑 NEW TRAP #2: the generator is SmolLM2, whose EOS is `<|im_end|>` — but whose `<|endoftext|>` is id **0**

MEASURED this session from `HuggingFaceTB/SmolLM2-1.7B-Instruct/tokenizer_config.json`:

| token | SmolLM2 id | dolma2 id |
|---|---|---|
| `<|endoftext|>` | **0** (unk_token) | **100257 (EOS)** |
| `<|im_start|>` | 1 (bos_token) | 100264 |
| `<|im_end|>` | 2 (**eos_token**, also pad) | 100265 |
| `<repo_name>`, `<file_sep>`, `<filename>`, `<gh_stars>`, `<issue_start>`, `<issue_comment>`, `<issue_closed>`, `<jupyter_start>`, `<jupyter_text>`, `<jupyter_code>`, `<jupyter_output>`, `<empty_output>`, `<reponame>` | 3–16 | **not in dolma2 at all** |

**Two findings from this table, and they point in opposite directions:**

1. **SmolLM2's stop token is `<|im_end|>`, not `<|endoftext|>`.** So the *most likely* leaked
   marker in FinePhrase text is `<|im_end|>` — which under dolma2 is id **100265, an ordinary added
   token, NOT the boundary.** So SmolLM2 leakage is a **quality** defect here, not a corpus-splitting
   one. That is a genuinely reassuring result and it is the opposite of the Nemotron/Phi-4 case
   (§5c), where the generator's marker *is* our EOS.
2. **But FinePhrase's input is web text**, and web text contains `<|endoftext|>` at the measured
   ~1/2,500 rate. Does the rewrite echo it? **UNVERIFIED.** SmolLM2 rewriting a document that
   contains `<|endoftext|>` may well reproduce it. **Run `neutralize_boundary_markers()` on the
   rewrite anyway** — it is a substring scan and the guard makes it free on the 2,499/2,500 case.

### 6d. 🛑 NEW TRAP #3: 2.07% of rewrites are under 16 dolma2 tokens, and 12.6% of `math` is under 50

MEASURED at n≈160,000 (40 row groups × 4 configs, each from a different random file, seed 99,
cluster-aware SEs computed):

| config | mean tok | median | <16 tok | <50 tok | <100 tok |
|---|---|---|---|---|---|
| faq | 438.5 | 412 | 2.07% | 5.30% | 8.63% |
| math | 282.3 | 234 | **2.12%** | **12.56%** | 22.27% |
| table | 265.3 | 210 | 1.08% | 3.11% | 11.59% |
| tutorial | 436.3 | 409 | 1.54% | 3.93% | 7.23% |

Registry trap, verbatim: *"No upstream quality control: a sampled rewrite was the entire string
`'Question: Can light accelerate to the speed of light?'` (~12 tokens). `corpus.MIN_DOC_TOKENS`
filtering is what makes this half publishable — a mean under 20 tokens fails the EOS bound."*

**The encoding-relevant consequence:** `MIN_DOC_TOKENS = 64` filters these, so the corpus is safe —
**but the filter changes the token yield per config by a measurable amount and the plan's
`target_tokens` must be set on the POST-filter pool.** MEASURED: quality filtering at ≥50 tokens
costs at most 1.3% of tokens. At ≥64 it is somewhat more for `math`. Grade: MEASURED for the
distribution, DERIVED for the yield impact at 64.

**⚠️ And a methodological warning worth repeating because it nearly poisoned the prior wave:**
Phase 0's n=34 **head** sample gave mean 40.5 tokens, median 33, 67.6% under 50 — **wrong by 10x**.
Verbatim: *"the first ~34 rows of faq are a genuinely degenerate contiguous block, and parquet row
order is content-clustered so a head read lands entirely inside it."* **Never sample a FineWeb-lineage
parquet from the head. Random file × random row group, always.** This applies to every source in
this audit and it is the single most transferable methodological finding on disk.

### 6e. Do the 4 configs share document ids? YES — MEASURED, and the mitigation is already specified

**MEASURED (`artifacts/reservoir/id-partition-verification.json`, read this session):** across the
four configs, `sha256(id) % 4` partitions land at 24.86–25.27% (worst deviation 0.27 pp from the
ideal 25.0%), and — decisively — the **combined** audit over 77,002 ids reproduces the *same
partition shares* as each individual config. Combined with row counts of 338.97 M / 338.75 M /
338.55 M / 337.78 M against a source split of **339,347,842 documents**, the conclusion is direct:
**all four configs are four views of ONE ~339 M-document universe, sharing the `id` key.**
Registry states it as ~91–93% the same documents. Also MEASURED: `ids_distinct == ids_sampled`
within each config (67,000/67,000 etc.) — so `id` is unique *within* a config.

**Mitigation (already in the registry, and it is irreversible-if-skipped):** apply
`sha256(id) % 4 == {0:faq, 1:math, 2:table, 3:tutorial}` **BEFORE tokenizing.** Registry, verbatim:
*"after tokenization there is no document→id mapping and it cannot be retrofitted."* Each format
needs 10.1–17.3% of its pool and a disjoint quarter gives 25.0% — so the partition is affordable.

**And the second-order join that composes with it:** exclude from the **edu-web** draw any
FineWeb-Edu `id` that entered the synthetic draw. MEASURED framing from `synthetic.json`: *"the two
pools are drawn from ONE 339M-document universe."*

### 6f. Encoding, tokenization, footers

- **Encoding hazards: no card statement, and the text is model-generated**, so mojibake/double-encoding
  is *less* likely than in scraped sources (an LM emits well-formed UTF-8) but **markdown table
  pipes and LaTeX-ish math notation are dense** — MEASURED: `table` has the **lowest** tokens/byte
  (0.2010) and the **highest** dispersion (CV 0.25), *"markdown table pipes and dashes tokenize
  unlike prose"*; `math` the highest tokens/byte (0.2336). Grade: MEASURED.
- **Already tokenized? No.** But the source ships SmolLM2 `completion_tokens`, and MEASURED
  dolma2/SmolLM2 = faq 1.00248, tutorial 1.00049, table 0.97156, math 0.96500 — within 3.5%.
  Do not treat the card's completion-token figures as dolma2, but they are close.
- **Footers: YES, at leaf level, and it is already done** — see §0.3. Row counts independently
  confirmed to within 0.07% by byte-ratio extrapolation against `/size`.
- **`/size`, `/rows` and `/info` all return HTTP 500 for config `faq`** (MEASURED). Any ingest
  tooling that depends on datasets-server for this source will fail on one of four configs. Use the
  tree API + Range reads.

---

## 7. Academic — `common-pile/{peS2o,pubmed,arxiv_papers}_filtered`

### 7a. Repo, format, paths — all three identical in shape

`.json.gz` Dolma-format JSONL at the **repo root**, prefixes `peS2o-`, `licensed_pubmed-`,
`arxiv-papers-` (MEASURED from the tree at pinned revisions). Text `text`, id `id`, license in
`metadata.license`. **MEASURED text byte totals from 109 files / 679 row groups of the
`raw_v0.1_parquet` mirror, 7.1 MB of footers:**

| source | RAW exact text bytes | filtered (ISIZE-scaled) | dolma2 pool |
|---|---|---|---|
| peS2o | 188,231,620,481 | 183,405,975,332 | 40.48 B tokens |
| pubmed | 158,924,718,730 | 146,609,054,427 | 37.54 B tokens |
| arxiv_papers | 20,696,587,734 | 19,458,048,619 | 6.23 B tokens |

Card figures are **`Size(GB) × 0.25`**, MEASURED-verified as arithmetic. peS2o's card says 43.3 B —
**7% HIGH** vs the footer-exact 40.48 B.

### 7b. 🛑 Encoding hazard: peS2o is **Grobid-over-PDF output**, so it is a PDF-extraction source in disguise

**CARD, verbatim:** *"PeS2o is derived from S2ORC … converted to a structured format using
**Grobid**. Starting from Grobid's XML output, peS2o filters papers that are too short, have
incorrect metadata, are in languages other than English, and **contain OCR errors** using a
combination of heuristic- and model-based filtering steps."*

**This is the finding.** peS2o is categorized as "academic" in our plan and treated as clean prose,
but it is **PDF→Grobid XML→text**, which means every hazard in §4b applies to 40.48 B tokens of our
academic pool — **with none of FinePDFs' mitigations.** Specifically:

- **No FTFY.** The card names no encoding repair. So peS2o may retain ligatures (ﬁ/ﬂ), mojibake,
  and soft hyphens that FinePDFs-Edu has already had removed. Grade: **CARD for the pipeline,
  UNVERIFIED for the artifacts** — and **this asymmetry between two PDF-derived sources in the same
  corpus is a real systematic difference nobody has flagged.**
- **The card admits OCR errors exist** and were *filtered*, not fixed — so surviving documents are
  those whose OCR error rate fell below a threshold, not zero.
- **De-hyphenation:** Grobid does perform some line-join/de-hyphenation. Whether peS2o's output has
  hyphen artifacts is **UNVERIFIED**.
- **Math:** Grobid emits `<formula>` elements; what peS2o does with them is **not stated**. For
  20.7 GB of arXiv papers and 188 GB of S2ORC full text, **math notation handling is completely
  undocumented.** Grade: UNVERIFIED. *Settling job:* range-read 50 documents from each and count
  `\\(`, `$`, `\\begin{equation}`, `<formula`, and bare unicode math (U+2200–U+22FF).
- **🛑 The prior wave found the overlap and it is an encoding-adjacent finding:** MEASURED, **49.7%
  of peS2o's BYTES are PubMedCentral-derived** (from per-document `metadata.pdf_src`,
  `artifacts/recount/_overlap-pes2o-pmc.json`). Registry trap, verbatim: *"The same article via
  Grobid-over-PDF and pandoc-over-nXML differs in >10% of 20-grams and **SURVIVES fuzzy dedup as
  distinct**."* — i.e. **the two extraction pipelines produce text different enough that no digest
  and no MinHash catches the duplication.** That is an *extraction-artifact* fingerprint doing
  damage. Mitigation (registry): drop peS2o's PMC share rather than dedup it, or take pubmed
  instead.

### 7c. Boundary markers

None of the three is among the five bundles that failed live. Academic PDFs of ML papers **do**
discuss `<|endoftext|>` in prose (it appears in dozens of arXiv papers about tokenizers). **Include
the scan; expect a low rate.** Grade: DERIVED. No new literal needed.

### 7d. Already tokenized? No. Truncation?

Not tokenized. **Truncation: the peS2o pipeline drops papers that are "too short" — a document
filter, not a truncation.** But the prior wave flagged a related hazard worth carrying:
`_head_bias_check.py` exists in the recount artifacts precisely because head-prefix reads of
`.json.gz` (the only cheap way to sample a gzip stream) are **content-biased**. Grade: MEASURED that
the bias was checked; UNVERIFIED whether upstream truncation exists.

### 7e. Footers: YES — and re-check the encoding guard

MEASURED and already done. **The encoding guard is essential here specifically:** *"Verified the
`text` column is PLAIN-encoded, not dictionary-encoded, for 668/679 row groups … The 11
RLE_DICTIONARY chunks are 0.36% (pubmed) and 2.37% (arxiv)."* So **2.37% of arxiv's row groups would
give a wrong byte total** if the guard were skipped.

---

## 8. Reference — `HuggingFaceFW/finewiki` + pre-1929 books

### 8a. 🛑 The `text` vs `wikitext` trap — a SECOND instance of the FinePhrase shape, MEASURED

FineWiki ships **BOTH** a cleaned payload and the raw source markup as top-level string columns:

- **CARD, verbatim Data Fields:** *"`text` (string): cleaned, structured article text preserving
  headings, lists, code/pre blocks, tables and math. **Has some markdown formatting**"* and
  *"`wikitext` (string): original wikitext when available"*.
- **MEASURED (`artifacts/recount/reference.json`):** `text` totals **40,630,233,930 bytes**,
  `wikitext` totals **66,702,673,537 bytes** — ratio **1.6417**. The recount README calls it out as
  a live example: *"picking the latter inflates the estimate ~1.6x."*

**Unlike FinePhrase, both are top-level, so a leaf-name scan finds two DIFFERENT names** — the trap
is not "same name twice", it is **"two plausible names and the wrong one is bigger"**, which is
worse for a heuristic that picks the longest string column. **`recount.py`'s own
`guessed-longest-string` heuristic would pick `wikitext`.** Grade: MEASURED. **The correct column is
`text`.** Registry pins it.

- Config `data/enwiki` (note: the *loadable config name* is `enwiki`, and the **path** is
  `data/enwiki` — CARD shows `load_dataset("HuggingFaceFW/finewiki", name="eswiki")` and
  `ParquetReader("hf://datasets/HuggingFaceFW/finewiki/data/ptwiki")`). Pinned rev
  `8bd13e72e6a002407649b3e898535f42ceb1aeb9`. id = `id`, format `<wikiname>/<page_id>`.
- Full schema (CARD): `text`, `id`, `wikiname`, `page_id`, `title`, `url`, `date_modified`,
  `in_language`, `wikidata_id`, `bytes_html`, `wikitext`, `version`, `infoboxes`, `has_math`.
- en: 6,614,655 pages, 35.1 GB. Pool MEASURED at **8.87 B dolma2 tokens** (footer-exact); registry
  notes *"The card names NO token count and NO tokenizer."*
- License: **CC-BY-SA-4.0 AND GFDL** — two copyleft regimes. Registry: *"Do not model SA as a
  boolean — record the license string."*

### 8b. Encoding hazards — extraction is from **HTML**, not wikitext, and that changes the answer

**CARD:** *"We heavily adapted **mwparserfromhtml** to parse the HTML content"*, from the
**Wikimedia Enterprise HTML dumps** (August 2025), *"pre-rendered HTML over the more commonly used
wikitex/markdown dumps"*. Consequences:

- **`text` contains markdown formatting deliberately** — `# Heading`, tables, lists. CARD says so.
  So the "reference" pool is **markdown-flavored**, not plain prose. Not a defect, but it must not
  be "cleaned".
- **Math is PRESERVED and flagged.** CARD: *"besides keeping all math content, pages containing math
  are flagged with a **`has_math`** metadata attribute"*, and *"notably, `wikimedia/Wikipedia`
  removes all tables and math content."* **`has_math` is a free per-row filter/label nobody in our
  plan uses.** In what notation the math is kept is **UNVERIFIED** — Wikipedia HTML renders `<math>`
  as MathML + an `alttext` LaTeX attribute, so mwparserfromhtml could plausibly emit either. *Settling
  job:* range-read 30 rows with `has_math=true` and look for `\\displaystyle`, `<math`, `<annotation`.
  **This matters: if it emits MathML, our corpus has XML tag soup in the reference pool.**
- **HTML entities:** parsing rendered HTML with a real parser normally decodes entities. **CARD makes
  no statement.** Grade: UNVERIFIED, but lower risk than DCLM.
- **`infoboxes` is a JSON-encoded STRING column** (CARD) — so it contains escaped quotes and
  `\"`-style escapes as literal text. **Never concatenate it into `text`.**
- No mention of ftfy, NFC, BOMs, NULs, surrogates. UNVERIFIED.

### 8c. Boundary markers: yes, include; low expected rate

Wikipedia articles about tokenizers/GPT contain `<|endoftext|>`. Not among the five failed bundles.
Cheap scan. No new literal.

### 8d. Pre-1929 books — **the brief names a source that our registry does not contain**

The reference category in the pinned registry is **finewiki only** (8.87 B, target 8.80 B). There is
**no pre-1929 books row.** Candidates that would fill it, all **UNVERIFIED for our purposes**:

- `common-pile/gutenberg_filtered`, `common-pile/library_of_congress_filtered`,
  `common-pile/pre_1929_books_filtered`, `common-pile/biodiversity_heritage_library_filtered` —
  the reference.json artifact lists a set of Common Pile public-domain collections as reserve
  (`wikimedia_filtered`, `wikiteam_filtered`, `libretexts_filtered`, `pressbooks_filtered`,
  `oercommons_filtered`, `public_domain_review_filtered`, `uspto_filtered`) but **does not name a
  pre-1929 books repo**, so the exact repo id is **UNVERIFIED and must be resolved before ingest.**
- **The predictable encoding hazard for ANY pre-1929 books source is OCR**, and it is the worst case
  in the whole corpus: long-s (ſ), broken ligatures, hyphenation at every line break in scanned
  columns, page numbers and running heads interleaved, and no FTFY. **Anything sourced from
  Internet Archive / Gutenberg scans needs its own extraction audit.** Grade: DERIVED from the
  nature of the source; **nobody documents it because we have not chosen the repo yet. That choice
  is the blocker.**
- Note also: Gutenberg texts carry a **standard boilerplate header and license footer** on every
  volume. Repeated ~60k times, that is a memorization magnet. Whether a given `_filtered` repo
  strips it is **UNVERIFIED per repo.**

---

## 9. `common-pile/stackexchange_filtered`

### 9a. Repo, format, paths

`.json.gz` at repo root, prefix `stackexchange-dolma-`, pinned rev
`c0ac7373830c688a43fc12d1988c4b19ccd884ab`. Text `text`, id `id`.
**MEASURED:** 104,984,965,100 exact text bytes (footers, all trees); filtered estimate
90,811,488,615 bytes → **24.05 B dolma2 tokens** pool against a 10 B target. CARD: 30,987,814
documents / 89.7 UTF-8 GB. Prior wave MEASURED tokens/byte **0.2649**.

### 9b. 🛑 Encoding: the text is **PyMarkdown-converted markdown → plain text**, and one document is a whole thread

**CARD, verbatim:** *"We use a question, its comments, its answers, and the comments on each answer
as a single document. Following the display order on StackExchange, answers are ordered by the
number of votes they received, with the exception that the 'accepted answer' always appears first.
**PyMarkdown was used to convert each comment into plain text.**"*

Three consequences:

1. **A document is a *concatenation* of turns with no stated delimiter.** The card does not say what
   separates the question from the answers, or one comment from the next. **UNVERIFIED — and it is
   an ingest-relevant unknown**, because if the delimiter is something like `---` or a bare newline,
   the model sees an unmarked speaker change. *Settling job:* range-read 10 documents; look at the
   joins. Ten minutes, and it determines whether a QA-shaped source is actually QA-shaped.
2. **Markdown→plain-text conversion drops code fences.** StackExchange answers are ~heavily code.
   If PyMarkdown flattened fenced blocks to bare indented text, the code/prose boundary is gone.
   **UNVERIFIED.** Prior wave's tokens/byte evidence is suggestive that it is prose-dominated:
   MEASURED 0.2649 tok/byte, *"sits between StackExchange prose-Q&A (0.2649) and IRC dialogue
   (0.3486), and NOWHERE near a code corpus."*
3. **HTML entities: StackExchange's XML dumps store post bodies as HTML with entities.** A
   markdown-oriented converter is not guaranteed to unescape them. **`&quot;`/`&#39;`/`&amp;`
   survival is a live possibility here and it is UNVERIFIED.** *Settling job:* the same 10-document
   read; grep for `&[a-z]+;` and `&#\d+;`.

### 9c. Boundary markers: **YES — MEASURED, `stackexchange` is one of the five bundles that failed live**

Cover `<|endoftext|>`. Already handled. **And it is the most explicable of the five**: StackOverflow
questions *about* GPT tokenizers literally quote `<|endoftext|>` in their bodies.

### 9d. Other

- Not tokenized. Common Pile "tokens" is arithmetic.
- **License: 100% CC-BY-SA-4.0, visible ONLY in per-row `metadata.all_licenses`** — `cardData`
  declares none (MEASURED, registry).
- **~180 sites, and the site name becomes a path segment.** Same permanent-cardinality fold as
  stackv2-edu; slug `'3dprinting.stackexchange.com' → '3dprinting'`.
- **Footers: YES**, ISIZE + parquet-mirror footers, already done.
- **The duplicate-tree trap that hit a sibling:** MEASURED on `ubuntu_irc` — the source repo has
  BOTH `raw/documents` and `v0/documents`, and reading the full id column (1,062,264 rows →
  733,149 distinct, histogram exactly `{1: 404034, 2: 329115}`) proved **one whole tree is
  duplicated inside the other**. For `github_archive`, the same check found **zero** duplicate ids —
  it is a genuinely larger pre-filter set, not a duplicate. **So "two trees in one repo" means
  duplication sometimes and not others, and only an id-level check distinguishes them.** Apply this
  check to any Common Pile repo before summing its trees. Grade: MEASURED.

---

# STAGE 2 (cooldown)

## 10. The AI2 dolma3 midtraining mix — **it is TEXT, not pre-tokenized, and the repo id took work to pin**

### 10a. The actual repo id, and there are five candidates

The brief asks for "the AI2 dolma3 midtraining mix" and warns it may ship pre-tokenized `.npy`.
**MEASURED this session against the HF API — it ships `.jsonl.zst` TEXT, not tokens.**

| repo | what it is | MEASURED status |
|---|---|---|
| **`allenai/dolma3_dolmino_mix-100B-1125`** | the 32B-model stage-2 mix, **two "ingredients"** | ✅ tree listed: `data/` holds **323 subdirectories**, files are `*.jsonl.zst` |
| `allenai/dolma3_dolmino_mix-100B-1025` | the 7B/1025 variant | exists (search API) |
| `allenai/dolma3_dolmino_mix-10B-1025` | 10B variant | exists |
| `allenai/dolma3-dolmino-mix-1025` | **DOES NOT EXIST / not accessible** — the tree API returns `{"error": "Invalid username or password."}` and its README is 29 bytes | ⚠️ the search snippet named this id; **it is wrong or private** |
| `allenai/dolma3_mix-6T-1025-7B` | the 6T PRETRAIN mix (not midtraining) | exists |

**Recommendation: `allenai/dolma3_dolmino_mix-100B-1125`.** Note the underscore-vs-hyphen and the
`-1125` vs `-1025` date suffix — **three of the five plausible ids differ only in punctuation, and
one of them 404s.** Pin the exact id and the revision.

### 10b. Format, schema, and the id

- **MEASURED (tree API):** `data/<ingredient><N>-<source>/CC-MAIN-YYYY-WW-part-NNNNN.jsonl.zst`,
  e.g. `data/ingredient1-nemotron-synth-qa/CC-MAIN-2013-20-part-00005.jsonl.zst` (7,973,126 bytes).
  **`.jsonl.zst` — so it needs `zstandard`, which our `corpus_read` does NOT declare** (the same
  gap that made the old `mlfoundations/dclm-baseline-1.0` registry row unreadable, §1a).
  **This is a hard code blocker, not an encoding one, and it is the single cheapest thing to fix.**
- **CARD front-matter (declared features, read verbatim):** `id`, `text`, `metadata`, `source`,
  `version`, `created`, `added`, `doc`, `attributes` — **all `dtype: string`.** So text is `text`,
  id is `id`, and **`metadata` and `attributes` are JSON-encoded STRINGS**, not structs. Only one
  plausible text column — but **note `doc` is also a string and its content is undocumented.**
  Grade: CARD. **UNVERIFIED whether `doc` ever holds text** — worth 60 seconds of a `zstd -dc | head`
  before writing the ingest row.
- **NOT pre-tokenized.** ✅ The brief's worry does not materialize: this is Dolma-format JSONL text,
  so re-tokenization is required and filtering IS possible.

### 10c. 🛑 It is QA-bearing, and the directory names ARE the category labels — a real gift

MEASURED from the tree: the 323 directories name their contents. Non-`common_crawl` ones include:

- **QA:** `ingredient{1,2}-nemotron-synth-qa`, `-reddit_to_flashcards`, `-wiki_to_rcqa-part1/part2`
- **Thinking/reasoning:** `-math-meta-reasoning`, `-code-meta-reasoning`, `-general_reasoning_mix`,
  `-omr-rewrite-fullthoughts`, `-program_verifiable`
- **Math:** `-tinymath-mind`, `-tinymath-pot`, `-cranemath`, `-megamatt`, `-dolmino-math`
- **Instruction:** `-tulu-3-sft`, `-dolmino_1-flan`
- **Code:** `-stack_edu-fim_vigintile_{15,16,17,19}_{C,CSharp,Cpp,Go,Java,JavaScript,Markdown,PHP,Python,Ruby,Rust,SQL,Shell,Swift,TypeScript}`
- **PDFs:** `-olmocr_science_pdfs-high_quality-<topic>-{2e12,2e13}` (and
  `-length_2e12/2e13` in ingredient2 — **the two ingredients use DIFFERENT directory naming for the
  same source**, so a glob written against ingredient1 silently misses ingredient2)
- **Web:** `-stem-heavy-crawl`, `-common_crawl-high-quality_{19,20}_<24 topics>`

**So the 14 B QA-bearing draw is selectable by prefix, with no classifier needed.** This is the
best-labelled source in the entire plan. Use `ingredient1-nemotron-synth-qa`,
`-reddit_to_flashcards`, `-wiki_to_rcqa-*`.

### 10d. 🛑 Boundary markers and chat scaffolding: **the WORST case in the whole audit**

Every non-web directory in this mix is **LLM-generated or SFT-derived**, and the generator set is
enormous. Three specific hazards:

1. **`-tulu-3-sft` and `-dolmino_1-flan` are SFT datasets.** SFT data is *stored* with role
   scaffolding. **UNVERIFIED whether AI2 flattened it to plain text or left chat markers in.** If
   left in, `<|im_start|>`/`<|im_end|>` (or AI2's own `<|user|>`/`<|assistant|>`) appear as literal
   text. Under dolma2 `<|im_start|>`/`<|im_end|>` are ids 100264/100265 — **ordinary, not
   boundaries.** So this is a quality/format hazard, not a corpus-splitting one. **But see 3.**
2. **`-omr-rewrite-fullthoughts`, `-*-meta-reasoning`, `-general_reasoning_mix`, `QWQ Reasoning
   Traces`, `Gemini Reasoning Traces`, `Llama Nemotron Reasoning Traces`, `OpenThoughts2 Reasoning
   Traces` (all named on the CARD source table) are REASONING TRACES.** Reasoning traces from
   Qwen/QwQ models contain **`<think>` and `</think>`** as literal delimiters — MEASURED: Qwen3's
   tokenizer defines `<think>`=151667 and `</think>`=151668. **Under dolma2 these are NOT special
   tokens at all** (not in the 22-token list), so they tokenize as ordinary text. Not a boundary
   hazard. **But they ARE a semantic marker the model will learn as structure, and if we ship
   `<think>` in a pretraining corpus we have created an undeclared control token.** Grade: DERIVED.
3. **🛑 `<|endoftext|>` is a live risk here from MULTIPLE generators at once.** The CARD's source
   table plus the Nemotron lineage means the generator set spans Qwen3 (`<|endoftext|>`=151643),
   DeepSeek, Phi-4 (**100257 — same as dolma2's EOS**), gpt-oss, QwQ, Gemini, Llama-Nemotron. Any
   one of them leaking its stop token as text puts `<|endoftext|>` in our corpus.
   **`neutralize_boundary_markers()` is MANDATORY on this source.**

**And a fourth, AI2-specific one that is MEASURED from the card and is a data-integrity trap, not an
encoding one:**

> **CARD (`dolma3_mix-6T-1025-7B`), verbatim:** *"Some olmOCR science PDFs in the current dataset
> have been **redacted** following the training of Olmo 3 7B. These texts are indicated with
> `[REMOVED]` in the text field."*

**🛑 So AI2 ships documents whose entire `text` is the literal string `[REMOVED]`.** That warning is
on the *6T* card; **UNVERIFIED whether the `-100B-1125` dolmino mix has the same redactions** — it
contains `olmocr_science_pdfs-high_quality-*` directories, so it plausibly does.
- **Mitigation, and it is trivial:** filter `text == '[REMOVED]'` or `'[REMOVED]' in text`.
- **Why it silently corrupts:** a `[REMOVED]` document is ~9 characters → ~4 dolma2 tokens. Below
  `MIN_DOC_TOKENS = 64` it gets dropped, **so our pipeline happens to survive this by accident.**
  But if the redaction is *partial* (a `[REMOVED]` marker inside an otherwise long document), the
  document passes every filter and we train on a placeholder. **Check for both forms.**

### 10e. Encoding, tokenizer, footers

- **Encoding:** no card statement on entities, normalization, NULs, surrogates, BOMs. UNVERIFIED.
  Mixed provenance (web + OCR'd PDFs + many LLMs) means **hazard classes are mixed within one
  source** — the olmOCR PDF directories carry PDF artifacts, the CC directories carry web artifacts.
  **Treat each `data/<dir>` as its own source for hazard purposes.**
- **Already tokenized: NO.** Text. And the token figures on the 6T card (4.51 T common_crawl etc.)
  name no tokenizer — but memory records that **dolma3-tokenizer IS dolma2** (AI2 says so in code),
  so AI2's dolma3 token counts ARE dolma2-comparable. **That makes this the ONE source whose card
  token figures we can use directly.** Grade: MEASURED-by-prior-session (see memory note
  "AI2's dolma3 shards are byte-compatible").
- **⚠️ But note the distinction:** that memory note is about AI2's **pre-tokenized `.npy`/uint32
  shards** being byte-compatible with our `.u32le.bin`. **These `dolma3_*_mix` HF repos are the TEXT
  form.** Both exist. **If you want the pre-tokenized route (copy+rename, no re-tokenization), it is
  NOT these HF repos — it is AI2's S3 shards.** That is a genuine strategic fork and the brief's
  worry was half-right: pre-tokenized dolma3 exists, just not here.
- **Footers: NO parquet footers.** `.jsonl.zst` — and **zstd has no ISIZE trailer** the way gzip
  does, so the gzip-ISIZE trick from §3f/§7e **does not work here.** Uncompressed size can come from
  the zstd frame header's `Frame_Content_Size` field *if the writer set it* (**UNVERIFIED**), else
  from the skippable/seekable-format index (**UNVERIFIED**). Document counts are not checkable
  without decompressing. **This is the least footer-checkable source in the audit.**
  - *Settling job:* range-read the first 18 bytes of 20 files and parse the zstd frame header for
    `Frame_Content_Size`. ~400 bytes total. If present, exact uncompressed sizes are free.

---

## 11. `HuggingFaceTB/cosmopedia` — **I MEASURED a leading-space artifact on 303/303 documents**

### 11a. Repo, configs, schema — MEASURED, not from the card

**MEASURED this session via `datasets-server/info`:** 8 configs, each with exactly one `train`
split, each with **identical 6-column schema, all top-level, no nesting:**

`prompt` (string), `text` (string), `text_token_length` (int64), `seed_data` (string),
`format` (string), `audience` (string).

Configs: `auto_math_text`, `khanacademy`, `openstax`, `stanford`, `stories`, `web_samples_v1`,
`web_samples_v2`, `wikihow`. (`web_samples_v1/v2` are ~75% of the dataset — CARD.)

### 11b. 🛑 TWO plausible text columns, and the wrong one is the PROMPT — FinePhrase's shape again

`text` is the generated content; **`prompt` is the input**, and **CARD confirms the prompt contains
seed text from another dataset**: *"the prompts include some text from another dataset/an external
source."* MEASURED: in `web_samples_v2` first-rows, 33 rows carried 133,309 chars of `text` and
**54,683 chars of `prompt`** — so `prompt` is ~41% the size of `text`, big enough that a
longest-string heuristic would not pick it, but big enough to matter if a schema scan picked wrong.

- Unlike FinePhrase the two columns have **different names**, so `.index('text')` is safe here.
- **But the trap has a different shape: `prompt` contains REAL WEB TEXT, so ingesting it would put
  un-attributed RefinedWeb-like extracts into a corpus labelled synthetic** — the same class of
  error as FinePhrase, via a different mechanism. Grade: MEASURED (column sizes) + CARD (that the
  prompt embeds seed text).
- **No stable document id.** There is **no `id`/`uuid` column at all.** MEASURED from the schema.
  **So the reservoir's `sha256(id) % N` partitioning has no key on this source.** Options: hash the
  `text` itself, or synthesize `(config, file, row_index)`. **This must be decided before ingest;
  a hash-of-text id is not stable across a re-download if the repo is revised.** UNVERIFIED whether
  a stable surrogate exists.

### 11c. 🛑 **MEASURED: every single document begins with a leading space.** This is a detokenization artifact.

I measured this directly against `datasets-server/first-rows` for **all 8 configs** this session:

| config | rows sampled | begin with `' '` | begin with `'  '` | begin with `'\n'` | HTML entities in `text` |
|---|---|---|---|---|---|
| web_samples_v1 | 35 | **35** | 0 | 0 | 0 |
| web_samples_v2 | 33 | **33** | 0 | 0 | 0 |
| auto_math_text | 41 | **41** | 0 | 0 | **16 (`&amp;`)** |
| stories | 44 | **44** | 0 | 0 | 0 |
| openstax | 33 | **33** | 0 | 0 | 0 |
| khanacademy | 46 | **46** | 0 | 0 | 0 |
| wikihow | 39 | **39** | 0 | 0 | 0 |
| stanford | 32 | **32** | 0 | 0 | 0 |
| **total** | **303** | **303 (100%)** | 0 | 0 | 16 |

**Grade: MEASURED** (n=303, but note these are `/first-rows` = HEAD reads, so per §6d they are
content-clustered — the *100% rate across 8 independent configs* is what makes it convincing, not
the n).

**Why this happens (DERIVED, high confidence):** Cosmopedia was generated by
**Mixtral-8x7B-Instruct-v0.1**, a **SentencePiece/Llama-family** model. SentencePiece prepends `▁`
to the first token, and a decode that maps `▁`→`' '` without stripping the leading one emits a
document starting with a space. **This is the exact `Ġ`/`▁` leakage class the brief asks about,
caught in the wild — it just manifests as a space rather than a visible `Ġ`.**

**Why it silently corrupts (and this is the important part):** under a BPE with byte-level
pretokenization like dolma2, **`" Behavior"` and `"Behavior"` are DIFFERENT TOKENS.** So:
- Every Cosmopedia document's first word is tokenized in its *mid-sentence* form, not its
  *sentence-initial* form.
- **Immediately after our appended EOS, the model sees a token that normally never follows a
  document boundary.** We are teaching the model that documents begin with a space-prefixed token.
- Nothing catches it: the text decodes, the hash is consistent, the EOS fraction is fine, the token
  count is off by exactly one per document (4 B tokens × 1 = ~30 M tokens, negligible), and the
  decode smoke test would render `" Behavior Change…"` which looks perfectly normal to a human.

**Mitigation: `text.lstrip()` — or more conservatively, strip exactly one leading space —
before tokenizing.** One line. **And the general rule this establishes: for every LLM-generated
source, check `text[0]` across a sample. It costs nothing and it is invisible afterwards.**
Do NOT apply a blanket `lstrip()` to non-generated sources without checking — leading whitespace
can be meaningful in code (§3).

### 11d. HTML entities: MEASURED present in `auto_math_text`

16 occurrences of `&amp;` in 41 sampled `auto_math_text` documents. Grade: MEASURED (small n).
Explanation (DERIVED): `auto_math_text`'s seed is `math-ai/AutoMathText`, itself web-scraped, and
Mixtral echoed the entity from the prompt. **So entity leakage in Cosmopedia is
seed-source-dependent** — present in the math config, absent in the other seven at this sample size.
- **Mitigation: `html.unescape()` is safe for THIS source** (it is prose, not markup) but **must not
  be applied to code (§3) or to FineWiki's markdown tables.** Per-source decision, not global.

### 11e. Boundary markers, truncation, tokenizer

- **`<|endoftext|>`, `<s>`, `</s>`, `[INST]`, `[/INST]`: MEASURED ZERO occurrences** in the 303
  sampled documents and in the sampled prompts. Mixtral's markers are `<s>`/`</s>`/`[INST]`, and
  **none leaked at this sample size.** Grade: MEASURED (small n) — a genuinely clean result.
  Under dolma2, `<s>`/`</s>` would not be boundaries anyway (not in the 22-token list).
  **Still run the scan; expect ~zero.**
- **`text_token_length` is a Mistral-7B count** — CARD, verbatim: *"the number of tokens in `text`,
  computed using Mistral-7B's tokenizer."* **Do not sum it as dolma2.** The card's "25 billion
  tokens" is likewise Mistral-7B. Our 4 B target needs a dolma2 re-count; **Cosmopedia is NOT in
  `artifacts/recount/`, so this is genuinely unmeasured for us.**
- **Truncation: UNVERIFIED.** No `finish_reason` field, no `max_tokens` on the card. Mixtral
  generations were certainly capped. **Same blind spot as Nemotron-CC-Math (§5e) and for the same
  reason: no field records it.**
- **v0.2 exists** — CARD: *"Note: Cosmopedia v0.2 is available at
  [smollm-corpus](https://huggingface.co/datasets/HuggingFaceTB/smollm-corpus)"*. v0.2 is a
  different generator (SmolLM lineage) and would have **different** artifacts. **Pin v0.1 or v0.2
  deliberately; they are not interchangeable for encoding purposes.**
- **Dedup: CARD says MinHash removed "under 1%"** of duplicates — but the same card says seed
  samples were reused with different `format`/`audience` (stanford: 4 prompt styles each). **So
  near-duplication by construction, with MinHash finding <1%** — the same "rewrites survive dedup"
  pattern as peS2o/PMC (§7b) and FinePhrase's four configs (§6e). **`format` and `audience` are the
  columns that let you partition it; use them the way we use `sha256(id)%4` on FinePhrase.**

---

## 12. Nemotron math-textbooks — **use `Nemotron-Pretraining-Specialized-v1`, and I MEASURED its schema**

### 12a. The repo the brief should name

The brief says "`nvidia/Nemotron-Pretraining-SFT-v1`". **Two corrections, both MEASURED:**

1. **`nvidia/Nemotron-Pretraining-SFT-v1` is GATED and has only 3 configs, none of which is math
   textbooks.** MEASURED: `datasets-server/info` returns
   *"does not exist, or is not accessible without authentication (private or gated)"*; the README
   front-matter (fetched via `resolve/main`) declares exactly **`Nemotron-SFT-Code`,
   `Nemotron-SFT-General`, `Nemotron-SFT-MATH`** — parquet, split `train`. Gate requires Company +
   Institutional Email + a model-training-only checkbox. License: **"other" — NVIDIA Data Agreement
   for Model Training.**
2. **`nvidia/Nemotron-Pretraining-Specialized-v1` is the successor, is CC-BY-4.0, is UNGATED, and
   HAS a `Nemotron-Pretraining-Math-Textbooks` config.** CARD: it *"is an extension of the
   previously released Nemotron-Pretraining-SFT-v1 with updated naming"* and *"is ready for
   commercial use."* **This aligns with the existing memory note ("Use
   Nemotron-Pretraining-Specialized-v1, CC-BY-4.0, ungated instead").**

### 12b. MEASURED schema and sizes (zero gate, `datasets-server`, `partial: false`)

All six configs share **one flat schema**, MEASURED from `/info`:

`text` (large_string), `license` (large_string), `metadata.category` (large_string),
`metadata.models_used` (large_string), **`uuid` (large_string)**.

**Text `path_in_schema` = `text`. Id `path_in_schema` = `uuid`.** **Exactly one plausible text
column — no trap.** And there IS a stable id, unlike Cosmopedia.

| config | rows (MEASURED, `partial:false`) | original file bytes (MEASURED) | CARD tokens (B) | CARD generator |
|---|---|---|---|---|
| `Nemotron-Pretraining-Math-Textbooks` | 12,899,767 | 30,841,263,255 | 25.1 | Qwen3-30B-A3B, Qwen3-235B-A22B |
| `Nemotron-Pretraining-RQA` | 17,476,335 | 184,550,910,116 | 134.6 | Qwen3-235B-A22B-Thinking-2507, gpt-oss-120b |
| `Nemotron-Pretraining-STEM-SFT` | 20,909,342 | 96,755,548,278 | 82.5 | DeepSeek-R1-0528, Qwen2.5-32B |
| `Nemotron-Pretraining-InfiniByte-Reasoning` | 1,478,301 | 28,345,959,943 | 19.4 | QwQ-32B, Qwen3-235B-A22B-Thinking-2507 |
| `Nemotron-Pretraining-Wiki-Rewrite` | 6,986,129 | 9,880,668,134 | 7.9 | Qwen3-30B-A3B |
| `Nemotron-Pretraining-Scientific-Coding` | 905,966 | 548,476,054 | 1.2 | Qwen3-235B-A22B |

**For the 3 B math-textbooks target, `Math-Textbooks` at 25.1 B (CARD) gives 8.4x headroom.**
DERIVED sanity check: 30.84 GB of files ÷ 25.1 B tokens ≈ 1.23 bytes/token, which is **implausibly
dense** — real prose runs ~4 bytes/token. So either the byte figure is compressed-parquet (likely)
or the token figure is inflated. **Do not use 25.1 B without a dolma2 re-count.** Grade: the rows
and bytes are MEASURED; the reconciliation is DERIVED and it does not close.

- **License nuance, CARD:** the collection is CC-BY-4.0 **"except for the Wiki-Rewrite and
  Scientific-Coding subsets (CC BY-SA 4.0 and GFDL 1.3)."** And **a per-row `license` column
  exists** (MEASURED: value `'cc-by-4.0'` on sampled Math-Textbooks rows) — so license is
  checkable per document, which is better than most sources.
- **Downstream model obligation, CARD, verbatim:** *"If this dataset is used to create, train,
  fine-tune, or otherwise improve an AI model, which is distributed or made available, such AI model
  may be subject to redistribution and use requirements in the Qwen License Agreement, the DeepSeek
  License Agreement, and the Phi-4 license agreement."* **Three model licenses attach to our
  weights via this one source.** Not encoding, but it belongs on the ingest row.

### 12c. 🛑 MEASURED: math is **LaTeX with `$`/`$$` and `\(...\)` delimiters**, and I read real rows

I pulled `first-rows` for `Nemotron-Pretraining-Math-Textbooks` and read the text directly.
Verbatim from row 0:

> `### Understanding Profit Models and Quadratic Functions\n\nIn business and economics, profit`
> `functions are often modeled using mathematical equations… A common type of profit function is a`
> `quadratic function, which has the general form:\n\n$$\nP(u) = au^2 + bu + c\n$$\n\nwhere $ P(u) $`
> `represents the profit (in riyals, for instance), and $ u $ is the number of units sold.`

And row 1 contains `$$\nS_n = \frac{n(n+1)(n+2)}{6}\n$$` and `$n^{th}$`.

**MEASURED findings:**
- **Notation is LaTeX in `$…$` / `$$…$$`**, with markdown `###` headings. Not MathML, not unicode
  math.
- **⚠️ Note the spacing: `$ P(u) $` and `$ u $` — spaces INSIDE the delimiters.** That is
  non-canonical LaTeX (normally `$P(u)$`). It is harmless for training but it means **any
  regex-based math extraction or normalization must tolerate it**, and it is a fingerprint of
  LLM generation rather than human authoring.
- On `Nemotron-Pretraining-RQA` I MEASURED `\(…\)` inline delimiters too: 145 occurrences of `\(`
  and 332 `$` in 13 sampled documents, plus 3 `\boxed`. **So the delimiter convention differs
  BETWEEN configs of the same repo** — `$`/`$$` in Math-Textbooks, `\(`/`\)` heavily in RQA.
  Grade: MEASURED (small n).

### 12d. 🛑 The `\n`-vs-LaTeX collision, MEASURED in the actual bytes

I searched the RQA sample for a literal backslash-n and found LaTeX commands, verbatim matches:

```
' \text{ s.t. } S_a^{(j)} \neq S_b^{(j)} \text{ despi'
'\mathcal{C}_{\text{OAM}} \neq 0 \), with sign determ'
'\xi} \), where \( \xi = |\nabla \eta|^{-1} \) is loc'
'ta_{\text{gap}} \propto |\nabla \eta| \) (from Dirac'
```

**Every one of those is `\neq` or `\nabla` — a LaTeX command beginning with backslash-n.**
**Grade: MEASURED.** So the §3c/§5b warning is now demonstrated on real bytes:
**any heuristic that rewrites literal `\n` to a newline turns `\neq` into a newline followed by
`eq`, and `\nabla` into a newline followed by `abla`.** It would destroy the math in exactly the
source we are buying for its math. **NEVER unescape. Trust the parquet/JSON reader.**

### 12e. Boundary markers: 🛑 **YES, and the generator table makes this the second-worst source**

Generators (CARD): Qwen3-30B-A3B, Qwen3-235B-A22B, Qwen3-235B-A22B-Thinking-2507, QwQ-32B,
DeepSeek-R1-0528, DeepSeek-R1, Qwen2.5-32B-Instruct, **Phi-4**, Mixtral-8x22B-Instruct-v0.1,
gpt-oss-120b.

MEASURED marker inventories (read this session):

| generator family | `<|endoftext|>` id | its EOS | dolma2 collision? |
|---|---|---|---|
| **Phi-4** | **100257** | `<|im_end|>` 100265 | **🛑 `<|endoftext|>` IS dolma2's EOS 100257** |
| Qwen3 / QwQ | 151643 | `<|im_end|>` 151645 | string collides, id differs — **still lands on 100257 under dolma2** |
| Mixtral | n/a (`</s>`) | `</s>` | no — `</s>` is not a dolma2 special |
| SmolLM2 (for reference) | 0 | `<|im_end|>` 2 | string collides → 100257 under dolma2 |

**The key point, and it is easy to get wrong: the generator's ID does not matter. What matters is
the STRING.** If any generator writes the literal characters `<|endoftext|>` into `text`, **dolma2
maps that string to 100257 regardless of what it meant upstream.** So the collision is
string-level and universal.

**MEASURED scan result (reassuring, small n):** across the sampled rows of
`InfiniByte-Reasoning` (10 rows, 187,283 chars), `RQA` (13 rows, 180,813 chars) and `STEM-SFT`
(27 rows, 175,896 chars) I found **zero** occurrences of `<|endoftext|>`, `<|im_start|>`,
`<|im_end|>`, `<|eot_id|>`, `</s>`, `<think>`, `</think>`, HTML entities, BOM, NUL, `ﬁ`, `ﬂ`, or
soft hyphen. **Only the LaTeX backslash-n hits.** So NVIDIA appears to have scrubbed specials on
this repo. **Grade: MEASURED at ~544 KB of text across 3 configs — enough to say the rate is not
high, nowhere near enough to say it is zero.** At the ~1/2,500 rate that broke five of our bundles,
50 documents would show nothing even if the problem were present at that rate.
**Run `neutralize_boundary_markers()` anyway and COUNT the hits.**

Also worth noting: **`<think>`/`</think>` did NOT appear** in the reasoning configs sampled — so
NVIDIA seems to have stripped the thinking delimiters. Contrast §10d, where AI2's reasoning-trace
directories are UNVERIFIED on the same question.

### 12f. Truncation, tokenization, footers

- **Already tokenized: NO.** Parquet text. **CARD names no tokenizer for the 25.1 B / 134.6 B
  figures.** Prior wave measured **RQA is 31.7 B unique, not 134.6 B** (memory: "Nemotron STEM-SFT
  is MMLU-contaminated"), i.e. the card's figures include repetition. **Carry that forward.**
- **Truncation: UNVERIFIED, no `finish_reason` column.** Same blind spot as §5e and §11e.
- **Footers: YES.** Parquet, ungated, `partial: false` on `/size`. **Byte totals and row counts are
  free.** This is the most measurable Stage-2 source.
- **Carry the prior warning:** memory records `Nemotron-Pretraining-STEM-SFT` is
  **MMLU-contaminated** — seeded from GSM8K/MATH/AOPS train splits, reformatted MMLU-style, zero
  decontamination. And I MEASURED the STEM-SFT sample text as literal MMLU-shaped
  multiple choice (`A: … B: … C: …`). **Do not draw STEM-SFT.** `Math-Textbooks` (prose textbook
  form, MEASURED above) is the safe config in this repo.

---

## 13. Reasoning traces / worked examples (8 B) — candidate repos

The brief asks to identify candidates. **None of these is in our registry or `artifacts/recount/`,
so every size figure here is CARD or UNVERIFIED.**

| candidate | route | text/id path | boundary-marker risk | grade |
|---|---|---|---|---|
| **`allenai/dolma3_dolmino_mix-100B-1125`, dirs `ingredient{1,2}-{math,code}-meta-reasoning`, `-general_reasoning_mix`, `-omr-rewrite-fullthoughts`, `-program_verifiable`** | ✅ **best route** — already needed for §10, one repo, `.jsonl.zst`, `text`/`id` declared | `text` / `id` | **HIGH** — QwQ/Gemini/Llama-Nemotron/OpenThoughts2 traces, `<think>` UNVERIFIED | MEASURED (tree) |
| **`nvidia/Nemotron-Pretraining-Specialized-v1` config `Nemotron-Pretraining-InfiniByte-Reasoning`** | ✅ ungated, CC-BY-4.0, 1,478,301 rows / 19.4 B CARD tokens | `text` / `uuid` | LOW-MEASURED — zero markers in 10 sampled docs | MEASURED (schema+rows) |
| `open-thoughts/OpenThoughts2-1M`, `open-r1/OpenR1-Math-220k`, `nvidia/Llama-Nemotron-Post-Training-Dataset` | the upstream trace sets AI2 mixed in | UNVERIFIED | **HIGHEST** — raw SFT/RL traces, chat templates and `<think>` almost certainly present | UNVERIFIED |
| `EleutherAI/proof-pile-2` config `algebraic-stack` | already footer-measured by prior wave (`_alg_footer.py`, `_alg-footer-train.json`) | UNVERIFIED | LOW — human-written | prior-wave MEASURED bytes |
| our own PRM800K vendor ingest (this branch: `src/edullm_data/ingest_prm800k.py`) | already built | n/a | n/a | in-repo |

**Recommendation: take reasoning traces from the two ✅ rows and NOT from the raw upstream SFT
repos.** Rationale, and it is an encoding rationale: AI2 and NVIDIA have each already done *some*
scrubbing (NVIDIA's is MEASURED-clean at small n; AI2's is UNVERIFIED but the data was used for
pretraining, which implies flattening). **The raw OpenThoughts/OpenR1/Llama-Nemotron repos are
stored in chat-message list form** — meaning the text is not even in a `text` column, it is in a
`messages[].content` list, which is **the FinePhrase nested trap in its purest form** plus a
role-scaffolding decision we would have to make ourselves.

**Also carry P1's own result forward, because it changes how much of this to buy:** our OLMo-2 370M
paper MEASURED that worked-example scaffolding **raised Pass@8 (+3.8 to +4.7 pp, p=0.0002) but
LOWERED PassRatio@8 reliability (5.11 → 4.33/4.52/4.59)**, and the pedagogically-ordered schedule
**LOST to random order** (p=0.0202). For a model scored on single-sample MC accuracy that is a bad
trade. **8 B is a defensible small allocation; it is not a category to expand.**

---

# 14. DELIVERABLE — one row per source

**Boundary-marker column reads: `Y` = must pass through `neutralize_boundary_markers()`.**
Every `Y` is for the SAME string, `<|endoftext|>`, because that is the only literal dolma2 maps to
the EOS id 100257. Markers listed in parentheses are ones present-or-plausible in that source that
are **NOT** boundaries under dolma2 (they become ordinary ids) — listed so nobody "fixes" them.

| # | repo id | config / path | format | text `path_in_schema` | id `path_in_schema` | encoding hazards | boundary neutralization | already tokenized | verification |
|---|---|---|---|---|---|---|---|---|---|
| 1a | `HuggingFaceFW/dclm_100BT` | `data` | parquet | `text` | `id` | **HTML entities NOT decoded (`&quot;` seen in card sample)**; no normalization documented; UTF-8/NUL/surrogate/BOM UNVERIFIED | **Y** `<\|endoftext\|>` — MEASURED, failed a live bundle | N | prior wave read real bytes at pinned rev; leaves enumerated |
| 1b | `mlfoundations/dclm-baseline-1.0` | none | **`.jsonl.zst`** (needs zstd dep we lack) | `text` | **NONE — no id column**; `metadata.WARC-Record-ID` UNVERIFIED | as 1a | **Y** | N | MEASURED zstd magic `28 b5 2f fd`; `/statistics` HTTP 501 permanently |
| 1c | `mlfoundations/dclm-baseline-1.0-parquet` | none | parquet, 27,938 files | `text` | **NONE** | as 1a; **drops WARC metadata + `warcinfo` (6 cols vs 8)** | **Y** | N | prior wave: tree API + `/size` partial head |
| 2 | `HuggingFaceFW/fineweb-edu` | `sample/100BT` (252B needs `sample-350BT` or `default`) | parquet | `text` | `id` | **all UNVERIFIED — card documents no extraction or normalization at all** | **Y** — MEASURED, failed a live bundle | N | MEASURED in bytes at pinned rev; README read in full |
| 3a | `common-pile/stackv2_edu_filtered` | none, repo root, prefix `stack-edu-` | `.json.gz` | `text` | `id` | **NUL bytes / invalid UTF-8 most likely of any source (UNVERIFIED)**; `'C#'` in a path segment breaks S3 addressing (MEASURED); **never unescape `\n` — it is real code** | **Y** — MEASURED, failed a live bundle. (`<\|im_start\|>`,`</s>` appear as code literals — NOT boundaries, leave them) | N | MEASURED: `text` holds code directly, no SWHID; ISIZE all 95 shards |
| 3b | `common-pile/stackv2` (raw) | `documents/*.gz` — **different layout** | `.json.gz` | `text` | `id` | as 3a, worse (unfiltered) | **Y** | N | CARD front-matter + card stats |
| 4 | `HuggingFaceFW/finepdfs-edu` | `data/eng_Latn/train` (⚠️ also ships a `test` split) | parquet | `text` | `id` | **FTFY ALREADY APPLIED upstream** → NFC, ligatures split, straight quotes, halfwidth, surrogates fixed (DERIVED). **Do NOT re-normalize.** Soft-hyphen/de-hyphenation, column order, math: UNVERIFIED. `is_truncated` bool shipped. RolmOCR page-loss at 8096 ctx | **Y** (+ count `<\|im_*\|>` split by `processor` to detect VLM scaffolding leakage) | N — `token_count` is **gpt2 per one card section, Llama-3.2 per another; self-contradictory** | footers 100/100 files MEASURED; parent README read in full |
| 5 | `nvidia/Nemotron-CC-Math-v1` | **`3` + `4plus` — "3plus" is NOT a config**; splits unstated | parquet | **UNVERIFIED — GATED, schema unavailable** | **UNVERIFIED** | **Math is LaTeX (CARD)**; `lynx` extraction — hard-wrap/`[1]`/`[IMG]` artifacts UNVERIFIED; **never unescape `\n` (LaTeX)** | **Y — HIGHEST PRIORITY. Phi-4-generated, and Phi-4's `<\|endoftext\|>` is id 100257, IDENTICAL to dolma2's EOS (MEASURED)** | N (Phi-4 output, detokenized) | MEASURED: datasets-server returns gated error. CARD for everything else |
| 6 | `HuggingFaceFW/finephrase` | `faq`/`math`/`table`/`tutorial` | parquet | **`rollout_results.list.element.text`** | `id` | markdown-table/math token density MEASURED; **`finish_reason` shipped and IGNORED by our plan — `max_tokens=2048` truncates ~1% mid-sentence** | Y (generator EOS is `<\|im_end\|>`→100265, NOT a boundary; but web-echoed `<\|endoftext\|>` possible) | N (SmolLM2 output; dolma2/SmolLM2 within 3.5%) | MEASURED: leaf footers, 160k rewrites, id partition audit; CARD documents the trap explicitly |
| 7a | `common-pile/peS2o_filtered` | repo root, prefix `peS2o-` | `.json.gz` | `text` | `id` | **🛑 IT IS GROBID-OVER-PDF — a PDF source with NO FTFY.** Card admits OCR errors were *filtered not fixed*. Ligatures/hyphens/math UNVERIFIED. **49.7% of bytes are PMC-derived and survive fuzzy dedup as distinct (MEASURED)** | Y (low rate expected) | N (card "tokens" = GB×0.25 arithmetic, MEASURED) | footers 109 files/679 row groups MEASURED; card read verbatim |
| 7b | `common-pile/pubmed_filtered` | repo root, prefix `licensed_pubmed-` | `.json.gz` | `text` | `id` | pandoc-over-nXML (cleaner than 7a); rest UNVERIFIED | Y (low) | N | footers MEASURED |
| 7c | `common-pile/arxiv_papers_filtered` | repo root, prefix `arxiv-papers-` | `.json.gz` | `text` | `id` | **2.37% of row groups are RLE_DICTIONARY — footer byte totals wrong without the encoding guard (MEASURED)**; LaTeX → never unescape | Y (papers quote the token) | N | footers MEASURED |
| 8a | `HuggingFaceFW/finewiki` | config `enwiki`, path `data/enwiki` | parquet | **`text`, NOT `wikitext`** | `id` (`<wiki>/<page_id>`) | **🛑 `wikitext` is 1.6417x bigger (MEASURED) and a longest-string heuristic picks it.** `text` is markdown-flavored by design. `has_math` flag shipped; math notation UNVERIFIED (MathML vs LaTeX). `infoboxes` is a JSON string — never concatenate | Y (low) | N (card names no count and no tokenizer) | MEASURED: both columns footer-totalled; README read in full |
| 8b | pre-1929 books | **repo NOT CHOSEN** | — | — | — | **OCR: long-s, ligatures, per-line hyphenation, running heads, Gutenberg boilerplate ×60k. No FTFY anywhere.** | Y | N | **UNVERIFIED — the repo id is the blocker** |
| 9 | `common-pile/stackexchange_filtered` | repo root, prefix `stackexchange-dolma-` | `.json.gz` | `text` | `id` | **PyMarkdown→plain text: code fences possibly flattened; turn delimiter UNSTATED; SE stores HTML so `&quot;`/`&#39;` survival is a live UNVERIFIED risk.** ~180 site values become path segments | **Y** — MEASURED, failed a live bundle | N | footers MEASURED; card read verbatim |
| 10 | **`allenai/dolma3_dolmino_mix-100B-1125`** | `data/ingredient{1,2}-<source>/` — 323 dirs; QA = `-nemotron-synth-qa`, `-reddit_to_flashcards`, `-wiki_to_rcqa-*` | **`.jsonl.zst`** (needs zstd) | `text` | `id` | mixed per-directory (web + olmOCR PDFs + 10 LLMs). **`[REMOVED]` redaction placeholders in `text` (CARD on the 6T sibling)**. `metadata`/`attributes` are JSON strings. `doc` column undocumented | **Y — mandatory.** Generators include Phi-4 (100257 collision). `<think>`/`</think>` from QwQ traces: UNVERIFIED, not a boundary but an undeclared control token | **N — it is TEXT.** (Pre-tokenized dolma3 exists but on AI2 S3, not here) | MEASURED tree API (323 dirs, `.jsonl.zst` sizes); CARD features |
| 11 | `HuggingFaceTB/cosmopedia` | 8 configs, `train` | parquet | `text` (**NOT `prompt` — prompt embeds real web seed text**) | **NONE — no id/uuid column at all** | **🛑 MEASURED: 303/303 docs across all 8 configs begin with a leading space** (Mixtral SentencePiece detokenization). **MEASURED: `&amp;` ×16 in 41 `auto_math_text` docs.** No `finish_reason` | Y (**MEASURED zero** `<\|endoftext\|>`/`<s>`/`</s>`/`[INST]` in 303 docs — run it anyway) | N (`text_token_length` is **Mistral-7B**, CARD) | **MEASURED by me this session**: `/info` schema + `/first-rows` × 8 configs |
| 12 | **`nvidia/Nemotron-Pretraining-Specialized-v1`** config `Nemotron-Pretraining-Math-Textbooks` (**NOT `-SFT-v1`, which is gated and has no math-textbooks config**) | `train` | parquet | `text` | **`uuid`** | **MEASURED: LaTeX in `$…$`/`$$…$$` with spaces inside delimiters; RQA uses `\(…\)`. MEASURED `\neq`/`\nabla` in real bytes — an unescape heuristic would destroy the math.** No `finish_reason` | Y (**MEASURED zero markers in ~544 KB across 3 configs** — not enough to prove absence at the 1/2,500 rate) | N (CARD names no tokenizer; prior wave: RQA is 31.7 B unique not 134.6 B) | **MEASURED by me this session**: `/info` schema, `/size` `partial:false`, `/first-rows` text read |
| 13 | reasoning traces | `dolma3_dolmino…/-*-meta-reasoning` etc. **and/or** `Specialized-v1/…InfiniByte-Reasoning` (1,478,301 rows) | `.jsonl.zst` / parquet | `text` | `id` / `uuid` | as 10 / as 12 | **Y** | N | MEASURED tree + schema |
| — | ⚠️ **do NOT use** `bigcode/the-stack-v2` (SWHIDs, not code), `nvidia/Nemotron-Pretraining-STEM-SFT` (MMLU-contaminated, MEASURED MC-shaped), raw `open-thoughts/*` / `open-r1/*` / `Llama-Nemotron-Post-Training` (text lives in `messages[].content` — the FinePhrase trap in pure form) | | | | | | | | |

---

# 15. PRIORITIZED TRAPS — worst first, each with its mitigation

Ranked by **(probability it happens) × (invisibility once it has) × (cost to undo)**. "Invisible"
below means specifically: no hash mismatch, no size anomaly, no decode failure, no Gate A rejection.

### P0-1. Nemotron-CC-Math is Phi-4 output, and Phi-4's `<|endoftext|>` is dolma2's EOS id 100257
- **Mechanism:** a leaked stop token in 45 B tokens of math becomes a phantom document boundary.
  OLMo-core recovers boundaries with `(mmap == eos).nonzero()` and there is no second signal.
- **Why invisible:** the shard's byte count, digest, and decode are all correct. Gate A's
  EOS-fraction bound (`FAMILY_MAX_EOS_FRACTION = 0.05`) only fires if the *rate* is high; at
  1/2,500 it passes. **The five bundles that failed live were caught by a per-shard
  EOS-count-vs-document-count comparison, not by the fraction bound.**
- **Evidence:** MEASURED id equality (both tokenizer configs read this session); CARD confirms Phi-4
  did the cleaning and documents no special-token scrubbing.
- **Mitigation:** run `neutralize_boundary_markers()` on this source **and emit a per-shard counter
  of `eos_occurrences − document_count`.** That counter is what caught it before; make it a receipt
  field, not a log line.
- **Same applies to:** §10 dolma3-dolmino (Phi-4 among its generators), §12 Nemotron-Specialized.

### P0-2. The `neutralize_boundary_markers()` guard makes any non-`<|` addition a silent no-op
- **Mechanism:** `if "<|" not in text: return text`. Adding `("</s>", …)` to
  `_BOUNDARY_MARKER_REWRITES` changes nothing, and the unit tests (which assert on the table) still
  pass.
- **Why invisible:** the code looks fixed. Grade: MEASURED by reading the function.
- **Mitigation:** **do not add non-`<|` literals.** Under dolma2 they are not boundaries anyway
  (§0.2). If a future tokenizer needs them, **the guard must be relaxed in the same commit**, and
  the test must assert the *behavior* (`neutralize("a</s>b") != "a</s>b"`) not the table's contents.

### P0-3. FinePhrase's `finish_reason` is shipped, and our plan ignores it
- **Mechanism:** `max_tokens=2048`; p99 rewrite length is 1,847–1,948 tokens (MEASURED). Roughly
  0.5–1.5% of faq/tutorial rewrites end mid-sentence, concentrated in the longest documents.
- **Why invisible:** a mid-sentence document is valid UTF-8, above `MIN_DOC_TOKENS`, hashes fine,
  and reads plausibly in a decode sample. **Nothing in the pipeline can see it. Only the column can.**
- **Mitigation:** project `rollout_results.list.element.finish_reason` alongside the text leaf and
  **drop rows where it != `"stop"`**, or at minimum record the rate per shard. One extra column.
- **Generalization — this is the shape to look for everywhere:** Nemotron-CC-Math (§5e), Cosmopedia
  (§11e), and Nemotron-Specialized (§12f) have the SAME truncation exposure with **no field at
  all.** FinePhrase is the only generated source that tells you. **For the other three the only
  detector is a heuristic** (fraction of documents ending without terminal punctuation, benchmarked
  against a human-written control like finemath).

### P0-4. Cosmopedia: 100% of documents begin with a leading space
- **Mechanism:** MEASURED 303/303 across all 8 configs. SentencePiece `▁` detokenization artifact
  from Mixtral. Under a byte-level BPE, `" Behavior"` ≠ `"Behavior"`, so **every document's first
  token is its mid-sentence variant, immediately after our appended EOS.**
- **Why invisible:** off-by-one token per document (~30 M of 4 B, i.e. 0.0008%); decodes and renders
  perfectly normally to a human reviewer.
- **Mitigation:** strip leading whitespace before tokenizing this source.
- **Generalization:** **check `text[0]` on a sample of every LLM-generated source.** It costs
  nothing. Do NOT `lstrip()` globally — leading whitespace is semantic in code (§3a).

### P1-5. peS2o is a PDF-extraction source with none of FinePDFs' repairs, and 49.7% of it duplicates PubMed undetectably
- **Mechanism:** CARD: Grobid-over-PDF, OCR errors *filtered* not fixed, no FTFY named. So 40.48 B
  tokens of "academic" text may carry ligatures, mojibake, and soft hyphens that
  `finepdfs-edu` has already had removed — **a systematic per-source normalization mismatch inside
  one corpus.** Separately MEASURED: 49.7% of peS2o's bytes are PMC-derived and *"differs in >10% of
  20-grams and SURVIVES fuzzy dedup as distinct."*
- **Why invisible:** two extractions of one paper are genuinely different byte strings. No digest,
  no MinHash at usual thresholds, catches it.
- **Mitigation:** (a) drop peS2o's PMC share via `metadata.pdf_src` rather than trying to dedup it,
  or take pubmed instead; (b) decide *deliberately* whether to run ftfy on peS2o so that the two
  PDF-derived sources are normalized alike — **and record the decision**, because "some sources are
  NFC and some are not" is a corpus property that will outlive anyone's memory of why.

### P1-6. FineWiki's `wikitext` is 1.64x bigger than `text`, and a longest-string heuristic picks it
- **Mechanism:** MEASURED 66.70 GB vs 40.63 GB. Picking `wikitext` yields a corpus of raw
  `{{Infobox …}}` template markup instead of prose.
- **Why invisible:** both are valid UTF-8 top-level strings named plausibly; the pool just looks
  1.64x bigger, which reads as good news. `recount.py`'s own
  `text_column_chosen_by: guessed-longest-string` would choose wrong.
- **Mitigation:** the registry already pins `text`. **Never let a heuristic choose a text column —
  require an explicit `path_in_schema` per source, and fail loudly if it is absent.** (Our
  `corpus.py` docstring already says `text_column` is an exact `path_in_schema`; enforce it.)

### P1-7. Never unescape `\n` — MEASURED to destroy LaTeX and code
- **Mechanism:** I found `\neq`, `\nabla` in real Nemotron RQA bytes (§12d). `\n`-unescaping turns
  `\neq` → newline + `eq`. In code, `"\n"` inside a string literal is the source text.
- **Why invisible:** the result is still valid text; it just has wrong math and wrong code.
- **Mitigation:** **no unescape step anywhere.** `json.loads` / parquet already produce real
  newlines. If a source genuinely has doubled escapes, that is a per-source finding requiring
  evidence, never a global filter.
- **Where entity decoding IS appropriate (per-source, never global):** DCLM (MEASURED `&quot;`
  present), Cosmopedia `auto_math_text` (MEASURED `&amp;` ×16), possibly stackexchange
  (UNVERIFIED). **Where it is actively harmful:** stackv2 (HTML/XML source files), FineWiki
  (markdown/tables), finepdfs-edu (already `unescape_html="auto"`'d, and re-running on a segment
  containing `<` behaves differently).

### P1-8. dolma3-dolmino ships `[REMOVED]` redaction placeholders in the `text` field
- **Mechanism:** CARD on the 6T sibling states it outright. UNVERIFIED for the 100B dolmino mix,
  which contains the same `olmocr_science_pdfs-*` directories.
- **Why partly invisible:** a whole-document `[REMOVED]` is ~4 tokens and dies on
  `MIN_DOC_TOKENS = 64` — **we survive that case by accident.** A *partial* redaction inside a long
  document passes every check.
- **Mitigation:** filter on `'[REMOVED]' in text`, both forms, and count.

### P1-9. `.jsonl.zst` is unreadable by our code, and that has already cost us a 30 B hole
- **Mechanism:** MEASURED — the old registry row for `mlfoundations/dclm-baseline-1.0` claimed
  parquet; the bytes are zstd; `corpus_read` declares no `zstandard` dependency. Registry verbatim:
  *"that row was a 30B hole in the corpus."* **The dolma3-dolmino mix (§10) is `.jsonl.zst` too**, so
  the same blocker now sits on the entire Stage-2 QA draw and on any full-DCLM route.
- **Why invisible at plan time:** a card's format facet says `json` and the Hub says "Auto-converted
  to Parquet", which is the viewer's 5 GB copy, not the files.
- **Mitigation:** add `zstandard` to the dependency set and a `.zst` branch to `corpus_read`; and
  **verify every source's format from magic bytes, never from the card facet.**

### P1-10. Common Pile shard prefixes are not derivable from repo names, and a wrong prefix lists zero files
- **Mechanism:** MEASURED — `stackv2_edu_filtered` → `stack-edu-`,
  `github_archive_filtered` → `gharchive-dolma-`, `pubmed_filtered` → `licensed_pubmed-`. Also
  `common-pile/stackv2` puts shards under `documents/` while `_filtered` puts them at the root.
- **Why invisible:** an empty file list is a zero-token source, not an exception. A category quietly
  contributes nothing.
- **Mitigation:** list the tree at the pinned revision and **assert `n_files > 0` and
  `n_files == expected`** before a build starts. Cheap, and it converts a silent hole into a loud one.

### P2-11. FinePhrase's four configs share one 339 M-document id space; the partition must precede tokenization
- MEASURED: `sha256(id) % 4` shares 24.86–25.27%, and the *combined* audit matches the per-config
  audits — four views of one document set. Registry: *"after tokenization there is no document→id
  mapping and it cannot be retrofitted."* Each format needs only 10.1–17.3%, so a disjoint quarter
  is affordable. **Also anti-join the edu-web draw against the ids that entered synthetic.**
- **Cost if skipped:** near-duplicate documents across four "different" synthetic sources, plus every
  synthetic document's unrephrased original also present as real edu-web. **Undoing it = rebuilding
  the bundles.**

### P2-12. `finepdfs-edu` ships `is_truncated`, and RolmOCR silently dropped pages at 8096 context
- CARD for both. Page-level loss inside a document is undetectable from the text.
- **Mitigation:** filter `is_truncated == False` (or record the rate); the parquet row-group
  statistics give min/max/null_count for that boolean **for free.** Also record `processor` so
  docling and rolmOCR documents are distinguishable downstream — they have different artifact
  profiles and someone will need to know which is which.

### P2-13. "3plus" is not a config; `Nemotron-Pretraining-SFT-v1` has no math-textbooks config
- Two card-vs-plan mismatches that would each fail at ingest time (loudly, so lower priority):
  Nemotron-CC-Math's 3-plus tier is `3` ∪ `4plus`; and the math-textbooks data lives in
  **`Nemotron-Pretraining-Specialized-v1`** (CC-BY-4.0, ungated, `uuid` id) not in the gated
  `-SFT-v1`. Also `allenai/dolma3-dolmino-mix-1025` **does not resolve** — use
  `allenai/dolma3_dolmino_mix-100B-1125`.

### P2-14. `text` column: three sources have NO usable document id
- MEASURED: `mlfoundations/dclm-baseline-1.0{,-parquet}` (no id column at all),
  `HuggingFaceTB/cosmopedia` (no id/uuid column at all), `HuggingFaceTB/finemath` (registry uses
  **`url`** as the id). `EssentialAI/essential-web-v1.0`'s id is marked UNVERIFIED in the registry.
- **Why it matters:** every de-duplication, anti-join, and `sha256(id) % N` partition in the plan
  needs a stable per-document key. **Decide the surrogate per source and write it down**, because a
  hash-of-text id is not stable across a repo revision.

### P3-15. Head sampling lies on content-clustered parquet — a methodological trap that already fired
- MEASURED: Phase 0's n=34 head read of FinePhrase `faq` gave mean 40.5 tokens; proper random
  sampling gave 438.5. **Wrong by 10x**, because *"the first ~34 rows of faq are a genuinely
  degenerate contiguous block, and parquet row order is content-clustered."*
- **Applies to every `/first-rows` figure in this document, including my own Cosmopedia and
  Nemotron measurements** — they are head reads. The Cosmopedia leading-space result is convincing
  because it is 100% across 8 *independent* configs, not because n=303. **Any rate I report at
  <100% from `/first-rows` must be re-measured at random file × random row group before it is used
  to size or gate anything.**
- **Mitigation:** random file × random row group, always. `_fp_footer_leaf.py` and `recount.py`
  already implement it.

---

# 16. What nobody documents — the honest gaps, and the one job that closes most of them

Marked UNVERIFIED above and **not settled by any card, issue, or paper I could find:**

1. **UTF-8 validity, NUL bytes, lone surrogates, BOMs — for EVERY source except finepdfs-edu.**
   Nobody publishes this. Not one card.
2. **HTML entity survival rates** — only DCLM (card sample) and Cosmopedia `auto_math_text` (my
   measurement) have any evidence at all.
3. **Soft-hyphen / de-hyphenation in finepdfs-edu and peS2o.** Both are PDF-derived; neither card
   mentions hyphenation. FTFY does not fix it.
4. **Math notation in FineWiki (`has_math=true` rows): MathML or LaTeX?**
5. **`lynx` artifacts (hard wrapping, `[1]`/`[IMG]` markers) in Nemotron-CC-Math.**
6. **Whether stackexchange's PyMarkdown pass flattened code fences, and what delimits turns.**
7. **Whether dolma3-dolmino's SFT/reasoning directories retain chat scaffolding or `<think>`.**
8. **Truncation rates for every generated source except FinePhrase** (which ships `finish_reason`).

### The single job that closes 1, 2, 8 — and it needs no extra pass over the data

**Add a per-shard "encoding receipt" to the tokenize path.** These are all O(1)-per-document
predicates on text the packer is already holding in memory:

```
eos_occurrences_minus_doc_count   # the counter that caught 5 live bundles — make it a receipt field
n_docs_with_marker__endoftext     # before neutralization
n_docs_with_marker__im_start / __im_end / __think / __endofprompt
n_docs_lone_surrogate             # text.encode('utf-8','surrogatepass') != text.encode(...)
n_docs_with_nul                   # '\x00' in text
n_docs_with_bom                   # text.startswith('﻿')
n_docs_leading_space              # text[:1].isspace()      <- catches P0-4 on any source
n_html_entity_matches             # regex &(amp|lt|gt|quot|#\d+|#x[0-9a-f]+);
n_docs_no_terminal_punct          # truncation proxy for sources with no finish_reason
n_docs_equal_REMOVED              # dolma3 redaction placeholders
```

Cost: a few string scans per document against a pipeline already doing BPE encoding — **negligible
against tokenization, and it converts eight UNVERIFIED rows into MEASURED ones as a side effect of
the build we were going to run anyway.** Emit into the existing corpus receipt
(`src/edullm_data/corpus_receipt.py`) so it lands in the manifest lineage rather than a log.

**The two jobs that need a human first:**
- **Accept the `nvidia/Nemotron-CC-Math-v1` gate** — then one authenticated parquet-footer read
  (~700 KB) names its text and id columns. Until then §5's row (a) is blank and it is the only
  Stage-1 source in that state.
- **Choose the pre-1929 books repo** (§8b). No audit is possible against an unnamed source, and OCR
  books are the highest-artifact-risk category in the plan.

### Two cheap footer jobs worth doing before ingest code is written
- **zstd frame headers** for dolma3-dolmino: 18 bytes × 20 files tells you whether
  `Frame_Content_Size` is set, i.e. whether uncompressed sizes are free (§10e).
- **`finish_reason` histogram** for FinePhrase: the one leaf, ~20 row groups, a few hundred KB —
  settles P0-3 quantitatively (§6b).

---

**Status: COMPLETE.** All 13 source groups covered; 16 sections. Every claim carries a grade.
