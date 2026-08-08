# Source identity dossier — `pretrain/final-dataset`

**Purpose.** One row per source in the approved mix (`FINAL-DATASET-REPORT.md` §3 Stage 1, §4 Stage 2),
carrying the exact strings a `CorpusSpec` registry row needs: repo id, config, revision, the exact
`text_column` as a parquet `path_in_schema`, the exact id column (or an explicit NONE), the domain
column, the format, and a graded token count.

**Every item here is a silent-corruption vector** — it produces a wrong corpus rather than an error.

**Author:** DATA-EXEC (re-dispatch #2), consolidating worker output. **Date:** 2026-08-08.
**Status:** IN PROGRESS — written incrementally; a partial file is intentional, not a crash.

**Grades:** MEASURED (bytes/footers read this session or a prior wave) · MEASURED-IN-CODE (read out of
this repo's source) · DERIVED (arithmetic over measurements) · CARD (the HF card asserts it; not
verified) · UNVERIFIED (nobody has measured or recorded it — a finding in its own right).

---

## 0. ⚠️ READ THIS BEFORE USING ANY ROW — the provenance split

**A registry already exists and supplies 10 of these rows' identity strings**, at
`artifacts/reservoir/corpus-registry.json` (`_schema: edullm-corpus-registry/v1`, 17 rows,
`_revisions_pinned_at: 2026-08-01`, `_revisions_verified`: all 14 (repo, sha) pairs resolved against
the HF tree API). It is loaded by `load_registry` (`corpus_build.py:130`) straight into
`CorpusSpec(**row)`, **so its field names ARE this dossier's columns.** `MEASURED-IN-CODE`.

🔴 **BUT IT IS THE RESERVOIR'S MIX, NOT THIS CORPUS'S.** This is precisely the denominator/scope
error CLAUDE.md warns about, so it is stated once, here, in full:

| | reservoir registry | `final-dataset` |
|---|---|---|
| total draw | ~250B | **1,000B** |
| `fineweb-edu` config | **`sample/100BT`** (pool 100.24B) | needs **252B** — 2.5× more than that config holds |
| `finephrase` | **FOUR rows** (faq/math/table/tutorial), 15B each | ONE row, 36B |
| rows it has, we don't draw | `essential-web` (RESERVE), `finemath`, `ubuntu_irc`, `github_archive` (RESERVE), `arxiv_papers` (RESERVE) | — |
| rows we need, it lacks | — | nemotron-cc-math ×2, dolma3 midtrain, reasoning traces, cosmopedia, nemotron math-textbooks, pre-1929 books |

**→ INHERIT the identity strings (repo / config / revision / columns / file prefixes). DO NOT
inherit `target_tokens` or `pool_tokens` without re-checking the denominator.**
The reservoir rows are **correct for the reservoir**; where they differ from this corpus that is a
scope difference, not an error by their author.

---

## A. The registry table

Fill order was traps first. `—` = not applicable. Blank = still open (a worker owns it).

| # | key | repo | config | revision (pinned, 40-char) | format | `text_column` (`path_in_schema`) | id column | domain column | license | grade |
|---|---|---|---|---|---|---|---|---|---|---|
| 1a | **dclm-baseline** (child, 114.7B) | `HuggingFaceFW/dclm_100BT` | `data` (100 flat files) | `01022d378d944de6deeb1c79d08fecb4d27b2c6f` | parquet | `text` | **`id`** ✅ | — | ODC-BY-1.0 | MEASURED |
| 1b | **dclm-baseline** (parent — **use this for 410B**) | `mlfoundations/dclm-baseline-1.0-parquet` | ⚠️ **4-level prefix, see §B7** | `817d6752765f6a41261085171dd546b104f60626` | parquet | `text` | **`id`** ✅ | — | **CC-BY-4.0** (differs from the child!) | MEASURED |
| 2 | **fineweb-edu** ⚖️ **RULED → `data`, see §B19** | `HuggingFaceFW/fineweb-edu` | **`data`** (FULL; 2,410 files / 4.523 TB) — **NOT `sample/350BT`, NOT `sample/100BT`** | `87f09149ef4734204d70ed1d046ddc9ca3f2b8f9` | parquet | `text` | `id` ✅ **joins cross-repo** | — | ODC-BY-1.0 | MEASURED |
| 3 | **stackv2-edu** (code) | `common-pile/stackv2_edu_filtered` | `null` (repo root, files `stack-edu-NNNN.json.gz`) | `c354dbe88469a1153e97c6a63ac50591849654de` | json.gz | `text` | `id` | **`metadata.gha_language`** ⚠️ 73 values, fold to ~20 | Blue Oak (100% permissive per-doc) | MEASURED |
| 4 | **finepdfs-edu** | `HuggingFaceFW/finepdfs-edu` | `data/eng_Latn/train` | `9cfabe2127faca99b3d5c4dc6d1fcb397399ebde` | parquet | `text` | `id` | — | ODC-BY-1.0 | MEASURED |
| 5 | **nemotron-cc-math-3** ✅ staged, see §B17 | `nvidia/Nemotron-CC-Math-v1` | `3` ⚠️ **explicit prefix, never a glob** | `397a2502f2028c659ba411a6c4935b464a7f03aa` | parquet | `text` | `id` | `None` — see §B8 | 🛑 **NVIDIA Data Agreement for Model Training (v. Aug 15 2025)** §2.2.2 BLOCKER | **MEASURED** (real config-3 footer, §B17) |
| 6 | **nemotron-cc-math-4plus** ✅ staged, see §B17 | `nvidia/Nemotron-CC-Math-v1` | `4plus` ⚠️ **match with `==`; `4plus_MIND` sits in the SAME parent prefix** | `397a2502f2028c659ba411a6c4935b464a7f03aa` | parquet | `text` | `id` | `None` | 🛑 same | **MEASURED** (real config-3 footer, §B17) |
| 7 | **finephrase** (synthetic) | `HuggingFaceFW/finephrase` | `faq` · `math` · `table` · `tutorial` (**4 configs**) | `78cf4a5ed0099214979c094c963e699c19163838` | parquet | 🛑 **`rollout_results.list.element.text`** | `id` | — | ODC-BY-1.0 | MEASURED |
| 8 | **peS2o** (academic) | `common-pile/peS2o_filtered` | `null` (repo root, `peS2o-NNNN.json.gz`) | `297747513bfb0ff1fbf61ddad3b03319d0f04597` | json.gz | `text` | `id` | — | CC-BY / CC0 (mixed), **share-alike ≈1.9%, invisible in metadata** | MEASURED |
| 9 | **pubmed** (academic) | `common-pile/pubmed_filtered` | `null` (repo root, `licensed_pubmed-NNNN.json.gz`) | `c156f0569a92d8f2edc33cebe1f72f7d3e1cae84` | json.gz | `text` | `id` | — | CC-BY / CC0 (mixed) | MEASURED |
| 10 | **arxiv_papers** (academic) | `common-pile/arxiv_papers_filtered` | `null` (repo root, `arxiv-papers-NNNN.json.gz`) | `033cf7f53f9b348deec868c1a5a48484f3ee9e52` | json.gz | `text` | `id` | — | CC-BY / CC0 (mixed) | MEASURED — **RESERVE, target 0** |
| 11 | **finewiki** (reference) | `HuggingFaceFW/finewiki` | `data/enwiki` | `8bd13e72e6a002407649b3e898535f42ceb1aeb9` | parquet | `text` | `id` | — | **CC-BY-SA-4.0 AND GFDL** (two copyleft regimes) | MEASURED |
| 12 | **pre-1929 books** (reference) | `common-pile/pre_1929_books_filtered` | `null` (repo root) | `23f9d96dbb1db3324bbc9fbfe1f8299cc799c4d1` | json.gz | `text` | `id` | — | **Public Domain** ✅ | MEASURED |
| 13 | **stackexchange** | `common-pile/stackexchange_filtered` | `null` (repo root, `stackexchange-dolma-NNNN.json.gz`) | `c0ac7373830c688a43fc12d1988c4b19ccd884ab` | json.gz | `text` | `id` | **`metadata.site`** ⚠️ ~180 values, fold to ~20 | CC-BY-SA-4.0, **100% share-alike, visible only in `metadata.all_licenses`** | MEASURED |
| 14 | **dolma3 midtrain mix** (QA) | `allenai/dolma3_dolmino_mix-100B-1125` | 4 `ingredient1` dirs of **209** under `data/` (NOT 323) — prefix-selectable | `f23aa129fda8335ba9760057bcc1f0c02f3d068b` | 🔴 **`.jsonl.zst` — NO READER**; 99,674 of 99,676 files | `text` (JSON key) | `id` ⚠️ **but see §B11 — schemas DIVERGE across dirs** | — | ODC-BY-1.0 | MEASURED |
| 15 | **reasoning traces** ⚖️ **RULED 2026-08-08** — `source_label` must **NOT** be `nemotron-*` (§B16) | `nvidia/Nemotron-Pretraining-Specialized-v1` | `Nemotron-Pretraining-InfiniByte-Reasoning` | `9ed3718b5f2ae29074c5e34e64115432b7c4320f` | parquet | `text` | **`uuid`** ⚠️ not `id`; **`large_string`** | — | **CC-BY-4.0 ✅ CLEAN** | MEASURED |
| 16 | **cosmopedia** | `HuggingFaceTB/cosmopedia` | 5 of 8 configs (a DECISION — see §B12) | `0ae6ec63f91742bd2d1eaef4f02232c55d719385` | parquet | `text` 🛑 **never `prompt`** | 🔴 **NONE** — surrogate §B12 | — | Apache-2.0 | MEASURED |
| 17 | **math-textbooks** ⚖️ `source_label` RULED — **NOT `nemotron-*`**, see §B16 | `nvidia/Nemotron-Pretraining-Specialized-v1` | `Nemotron-Pretraining-Math-Textbooks` | `9ed3718b5f2ae29074c5e34e64115432b7c4320f` | parquet | `text` | **`uuid`** ⚠️ not `id` | — | **CC-BY-4.0 ✅ CLEAN — no §2.2.2** | MEASURED |

✅ **ALL 17 ROWS CARRY MEASURED IDENTITY STRINGS.** Row 15 was never a measurement gap — no repo had
ever been chosen — and the CEO **RULED it 2026-08-08** onto `InfiniByte-Reasoning`. **No row is open.**

**Rows 1–4, 7–11, 13 came from the existing reservoir registry** (each carries its own
`"text_column verified in <artifact>"` provenance trap in the registry). **7 rows open.**

🛑 **EXCLUDED, recorded so it is never re-added:** `nemotron-cc-math-4plus_MIND` — a **rewrite** of
`4plus`, so including both double-counts the same documents. The 134.0B measurement is `3` + `4plus`
**only**, which is correct. If `4plus_MIND` is ever staged, label it so a prefix match cannot sweep
it into the math pool.

### A.1 A column the skeleton did not have, and that cannot be guessed — `MEASURED-IN-CODE`
Every Common Pile source ships `.json.gz` **at the repo root** with `config: null` and a file prefix
that **does not match the repo name and cannot be derived from it.** From
`corpus-registry.json._common_pile_file_prefix`:

| repo | file prefix |
|---|---|
| `common-pile/peS2o_filtered` | `peS2o-` |
| `common-pile/pubmed_filtered` | `licensed_pubmed-` |
| `common-pile/arxiv_papers_filtered` | `arxiv-papers-` |
| `common-pile/stackv2_edu_filtered` | `stack-edu-` |
| `common-pile/stackexchange_filtered` | `stackexchange-dolma-` |
| `common-pile/ubuntu_irc_filtered` | `ubuntu-chat-dolma-` |
| `common-pile/github_archive_filtered` | `gharchive-dolma-` |

**List the tree at the pinned revision rather than guessing a prefix.**

---

## B. Trap resolutions

### B1 — FinePhrase's nested rewrite column. ✅ **TRAP NOT BITTEN.**
The brief warned the rewrite is in `rollout_results[0].text`, not `text`. **The live registry already
has it right**: all four rows carry `"text_column": "rollout_results.list.element.text"`.
`MEASURED-IN-CODE`, verified in `artifacts/reservoir/id-partition-verification.json`.

⚠️ **Notation, because the two spellings look like a discrepancy and are not:** `rollout_results[0].text`
(the brief's shorthand) and `rollout_results.list.element.text` (the parquet `path_in_schema`) are the
**same leaf**. Only the second string works. The defences are real and layered:
- `corpus.py:200-206` documents the trap in the `CorpusSpec` docstring.
- `corpus_read.py:106 _resolve_leaf` resolves by exact `path_in_schema`.
- The near-miss `rollout_results.text` **does not raise** — it returns a table with **zero columns**
  (`IMPLEMENTATION-PLAN.md` §4.2). Both near-miss spellings were tested and rejected.
- **Why it matters:** top-level `text` holds the ORIGINAL FineWeb-Edu document (its `dataset` field
  literally reads `HuggingFaceFW/fineweb-edu`). A flat leaf scan finds `text` twice and
  `.names.index("text")` returns the original — building a corpus of unrephrased web text **labelled
  synthetic**, which **no hash, size, or decode check catches**.

### B2 — Token counts are not summable. ✅ **HONOURED THROUGHOUT.**
Almost no HF card names its tokenizer. **Common Pile's "tokens" column is `Size(GB) × 0.25`** — pure
arithmetic, not a count. Every `pool_tokens` in the registry is a **dolma2 re-count** from
`artifacts/recount/` or `artifacts/sizing-revised.md`, never a card figure — the registry's own
`_note` states this. Method (`artifacts/recount/README.md`): `num_rows` (exact) × `mean_chars_per_doc`
(whole-split `/statistics`) × `tokens/char` (sampled). ⚠️ **The naive `num_rows × sampled
mean_tokens/doc` does NOT work** — measured CV 9.0 and a 95% CI of **[26B, 204B]** on FineMath,
because document lengths are heavy-tailed. Validated: FineMath-3plus → 34.69B vs the card's 34B.

### B3 — 🔴 The mean-document-length floor: 14 sources MEASURED, all clear. Stage 2 is the gap.
**This is a publish blocker, not a preference.** `families/pretrain.json:46` sets
`eos_fraction_max: 0.05` = **a mean-document floor of 20 tokens**; a mean under 20 puts the packed
shard's EOS fraction over the bound and **Gate A rejects the shard — after the tokenize and after
the upload.** `corpus.MIN_DOC_TOKENS = 64` (`corpus.py:149`) is what prevents that.

Derived by me from `artifacts/reservoir/sources.json` (`tokens ÷ documents`), `DERIVED` over
`MEASURED` inputs — this is a **larger set than the 5 rows `IMPLEMENTATION-PLAN.md` §4.5 lists**:

| source | tokens/doc | EOS fraction | margin to 0.05 |
|---|---|---|---|
| ubuntu-irc | 8,650.7 | 0.000116 | 431× |
| pubmed | 7,884.8 | 0.000127 | 394× |
| pes2o | 6,450.9 | 0.000155 | 323× |
| finepdfs-edu | 5,630.4 | 0.000178 | 281× |
| finemath | 1,589.3 | 0.000629 | 79× |
| finewiki | 1,316.0 | 0.000760 | 66× |
| dclm | 1,256.3 | 0.000796 | 63× |
| fineweb-edu | 1,003.3 | 0.000997 | 50× |
| stackv2-edu | 938.9 | 0.001065 | 47× |
| stackexchange | 727.0 | 0.001376 | 36× |
| **synthetic-finephrase-faq** | 440.7 | 0.002269 | 22× |
| **synthetic-finephrase-tutorial** | 432.0 | 0.002315 | 22× |
| **synthetic-finephrase-math** | 309.1 | 0.003235 | 15× |
| **synthetic-finephrase-table** | **262.2** | **0.003814** | **13× ← tightest measured** |

**All 14 clear comfortably.** FinePhrase is the tightest, as §4.5 predicts (it has a sampled rewrite
only **12 tokens** long — the whole string `"Question: Can light accelerate to the speed of light?"`),
but even `table` sits 13× under the bound **after** `MIN_DOC_TOKENS = 64` filtering.

⚠️ **The stage-2 sources are the ones with NO measurement** — the dolma3 QA mix (GPT-4o-mini-rewritten
multiple choice) is the one plausibly near the floor. That is node **M4**, and it **gates FREEZE**
(`BUILD-DEPENDENCY-GRAPH.md:151`). Assigned to W6.

### B4 — Permanent cardinality: two sources ship a domain
`stackv2-edu` carries **73** languages (`metadata.gha_language`); `stackexchange` carries **~180**
sites (`metadata.site`). **Every distinct value becomes a directory inside `manifest_sha256`,
forever** (`manifest.py:849`). Fold to the top ~20 with the rest as `other`. **The fold map is a
reader ARGUMENT, not a spec field**, because a streaming pass cannot know the top 20 before it has
read everything.
🛑 **SLUG THE VALUE.** `C#` publishes clean and passes Gate A, then `urlparse` puts everything after
the `#` into the URI fragment and **the shard name leaves the path**. Gate A now rejects `#` and
brackets (`validate._segment_breakage`); other values still need `slug_path_segment`.
`C#`→`c-sharp`, `C++`→`c-plus-plus`, `3dprinting.stackexchange.com`→`3dprinting`.

### B5 — Licenses that constrain, recorded as strings not booleans
- **`finewiki`: CC-BY-SA-4.0 AND GFDL** — two different copyleft regimes on one source. **Do not
  model share-alike as a boolean** (`corpus.py` §1.5).
- **`stackexchange`: 100% share-alike**, and the license is visible **only** in per-row
  `metadata.all_licenses` — `cardData` declares none. A declared license both over- and under-states
  what is inside.
- **`peS2o`: ≈1.9% share-alike and INVISIBLE from repo metadata** (Common Pile paper Table 3).
  Name-level SA exclusion therefore **drops 100% of the source to remove 1.9%**.
- 🛑 **Nemotron §2.2.2 forbids "making available to others"** — this blocked a shared reservoir.
  Every Nemotron row must carry its exact license string; W3 is checking each.
- 🛑 **CK-12 bans AI training outright** (§4.2/§4.6, verified verbatim). Excluded. Separately, ALL
  open textbooks total only ~0.6B tokens, so that category is an anneal set at best, never a pillar.

### B6 — ⏳ Open, worker-owned
- **B1 (dossier §A row 1): the DCLM repo reconciliation.** The registry pins `HuggingFaceFW/dclm_100BT`
  (114.7B); the plan §4.1 says use `mlfoundations/dclm-baseline-1.0-parquet` (~3,764B) because the
  draw is **410B**. → W2.
- **B2 (row 1 id column): "no usable document id" vs the registry's verified `id` leaf.** The plan
  §10 says both full-DCLM repos lack one; the registry's dclm row lists `id` among leaves verified
  from real bytes. **One is scoped to a different repo.** → W2.
- **B3 (row 2): `sample/100BT` holds 100.24B and the draw is 252B.** → W6.
- **Gap 1 (row 14): `.jsonl.zst` has no reader.** → W4, with my analysis in `code-gaps.md`.

### B7 — 🔴 The DCLM parent's real path prefix, and why a wrong one costs 410B
**`MEASURED` by exhaustive tree scan (562 cursor pages, 1,437 s, zero 429s).**
`IMPLEMENTATION-PLAN.md` §8A.5a says the mirror nests `global-shard_01_of_10` **at the top level**.
It does not. The real prefix to any data file is four levels deeper:
```
filtered/OH_eli5_vs_rw_v2_bigram_200k_train/fasttext_openhermes_reddit_eli5_vs_rw_v2_bigram_200k_train/processed_data/global-shard_NN_of_10/local-shard_N_of_10/shard_NNNNNNNN_processed.parquet
```
A `config` of `global-shard_01_of_10` returns a hard **HTTP 404** — the worker executed the real
`hf_files` and **corrected its own earlier prediction that this would fail silently**: it fails
loudly, so severity is a docs fix, not a data-integrity risk. **Still pre-FREEZE mandatory**, because
**#28 is on the critical path and writes one `config` per split row.**
Verified counts: **27,938** `.parquet` (exactly the plan's claim), **7,419,668,271,828 bytes =
7.4197 TB** (exact match to `web.json`'s recorded `repo_bytes`), 114 directories = 1+1+1+1+10+100.
✅ **The 10 global shards are balanced to 0.24%** (`MEASURED`, exhaustive scan) — so **whatever**
carve is chosen along that axis needs **no counting pass**. This is a property of the DATA and stands.

🔴 **`BLOCKED-ON-F2` — HOW MANY WAYS to carve is NOT settled and must not be filled from the plan.**
Per the CEO HOLD of 2026-08-08 (AUDIT finding F2): the plan's split depth ("5-way DCLM, 4-way
FineWeb-Edu") and its "12 children × 32 vCPU" shape both derive from applying `72,615 tok/s/vCPU`
per-vCPU at 32 vCPU, but that rate was **MEASURED on 8-vCPU containers**. With ~78% of cost
single-threaded under the GIL, only the 22% encode half scales: 384 vCPU as 12×32 → **33.2 h**, as
48×8 → **9.96 h**. **Leave ways-per-source, container shape, and every per-child duration EMPTY in
this dossier until the CEO rules.** None of the identity columns depend on them.

### B8 — ⚠️ `parquet_bytes` is NOT a sizing input, and this trap already bit once in this session
The worker flagged a 2.18× gap between a byte-based and a row-based token derivation, then
**corrected itself in place**: `tokens_per_byte` in the recount artifacts is per byte of **decoded
UTF-8 text**, while `parquet_bytes` is **compressed on-disk parquet**. Multiplying them is the error.
The implied 2.18× is just the compression ratio — **footer-confirmed at 2.24×** on a real shard
(144,473,349 file bytes holding `text` at 323,504,637 uncompressed). **Two artifacts on disk carry
`parquet_bytes` next to token figures; do not multiply them.**

### B9 — ✅ The DCLM document id is SOUND, and `IMPLEMENTATION-PLAN.md` §10 is FALSE on this point
§10 lists *"both full-DCLM repos [have] no usable document id"* as a **FREEZE prerequisite**. Refuted
from real bytes. Both repos carry a 47-char `<urn:uuid:…>` **Common Crawl WARC-Record-ID**:

| property | evidence | grade |
|---|---|---|
| present in both repos | parquet footers, `id::string` top-level | MEASURED |
| uniform width | `Counter({47: 1000})` and `Counter({47: 61000})` | MEASURED |
| distinct | 61,000/61,000 in-file | MEASURED |
| **100% UUIDv4** | 3,000 values across both repos, RFC-4122 v4, variant `10xx`, zero exceptions | MEASURED |
| **stable across a re-download** | 52/52 shared ids return **byte-identical text** across two repos built by different toolchains (`parquet-cpp-arrow 23.0.0` vs `parquet-rs 52.0.0`) **19 months apart** | MEASURED |
| hashes uniformly | real `partition_of` over 2,000 ids: χ²=3.60, df=3, p≈0.31 | MEASURED |
| accepted unmodified | `_require_id('<urn:uuid:…>')` → no exception; **no stripping of the wrapper needed** | MEASURED |

**Rejected surrogates, on measurement not argument:** `url` is **not unique** (60,998/61,000 — two
crawls of one page are two documents); `(file_path, row_index)` dies because the mirror re-converted
into a different layout and **row order is not sorted by anything**; `sha256(text)` is worse than
unstable — DCLM is ~80% near-duplicate, so a text hash **merges distinct documents by construction**.

**And the pipeline already hard-requires this id** (`MEASURED-IN-CODE`): `corpus.py:174-178`
documents it; `corpus_read.py:481` resolves `spec.id_column` **before reading a row**;
`corpus_read.py:510-515` raises `ReadError` on a null id. **So if §10 were true, the build would HARD
FAIL at file 1, not silently corrupt.** It does not fail, because the column exists.

⚠️ **A correction to my own brief:** I told workers *"#21 and #22 both depend on `sha256(id) % N`."*
**#22 does not.** `corpus_filter.py:103-105` keys the dedup set on **`sha256(normalized text)`**, so
§5.2a's 27.92 GB memory figure is **unaffected** by the id finding. #22 was never blocked on the id.

### B10 — 🔴 THE LARGEST OPEN RISK IN THE MIX: the 410B DCLM draw is ~56% of the unique pool
`FINAL-DATASET-REPORT.md` §3 scores DCLM at **0.51 epochs against a 744.6B pool** — and §4.1's
"~3,764B ✅ use this" invites reading 410B as a 9.2× pool. **Both rest on an ALL-COPIES figure.**
`artifacts/recount/web.json` says verbatim: report ~750B (Zyphra, empirical) or ~733B dolma2-adjusted
as the unique pool — **"and not the 3764B all-copies figure either."** DCLM used a **per-shard Bloom
filter, not global dedup**; Zyphra measured 2,949.3M → 615.2M docs (**80% duplicates**).

**DERIVED: 410 / 733 = 56% of every unique DCLM token in existence**, and a naive random 410B draw
yields roughly **82B of unique content repeated ~5×.** The reservoir's 30B draw was never exposed to
this (30/733 = 4%). **Exact `content_hash` dedup will NOT catch it** — the duplication is *near*-
duplicate (Zyphra minhash-LSH ~85% Jaccard), and distinct WARC ids on near-identical pages are
*correct* identity behaviour. **This is a deviation candidate against the report's largest share and
its under-1-epoch claim. Escalated to the CEO; not decided here.**

### B11 — 🔴 The dolma3 midtrain mix needs MULTIPLE registry rows: its records have INCOMPATIBLE SCHEMAS
`MEASURED` on the first record of one file from each of six `data/` directories:

| directory | record keys |
|---|---|
| `ingredient1-nemotron-synth-qa` | `language, text, url, warc_record_id` — **NO `id`** |
| `ingredient1-dolmino-math` | `dolminos_category, id, metadata, text` |
| `ingredient1-general_reasoning_mix` | `id, metadata, text` |
| `ingredient1-reddit_to_flashcards` | `id, text` |
| `ingredient1-tulu-3-sft` | `id, metadata, text` |
| `ingredient1-wiki_to_rcqa-part1` | `id, text` |

`CorpusSpec` carries **one** `id_column` per row, and the reader **correctly RAISES** when it
resolves to nothing (`corpus_read.py:726-732`). **So one row cannot cover this repo.**
`nemotron-synth-qa` (1,024 files) needs `id_column: "warc_record_id"`; the rest need `"id"`.
**That means multiple `source_label`s → multiple bundles → multiple ordinal allocations, i.e.
plan-shaped work INSIDE the FREEZE.** ⚠️ **Only 6 of 209 directories were sampled — the true row
count is UNKNOWN**, and this is in nobody's 14B estimate.

### B12 — Cosmopedia: no id at all, and a second FinePhrase-class column trap
**`id_column` = NONE, `MEASURED`:** no `id`, no `uuid`, no `url`, no hash column in **any** of the 8
configs. The complete leaf list is 6 columns. `sha256(id) % N` has **no key** on this source.

**Surrogate proposed (`DERIVED` — a proposal, not a measurement; the reader must implement it):**
**`(config, file_basename, row_index_within_file)`**, e.g.
`cosmopedia/web_samples_v2/train-00000-of-00118.parquet#87676`. Stable because filenames encode
`-of-NNNNN`, so **any change to the file count changes every filename — loud, not silent.**
**Condition: only against the PINNED revision.**
- `sha256(text)` **rejected** — unstable per the plan, **and worse here: the required `lstrip()` fix
  CHANGES the text**, so the id would depend on whether normalization ran before or after hashing.
- `(config, row_index)` **rejected** — row order is a property of upstream files, not the dataset.
- ⚠️ **A surrogate id is NOT comparable across sources.** Any cross-source anti-join or dedup keyed
  on `id` **silently excludes Cosmopedia.**

🛑 **`prompt` IS REAL WEB TEXT — never ingest it.** `MEASURED`: `prompt` is **8–59% of `text`** by
uncompressed bytes (`wikihow` 7.5%, **`auto_math_text` 58.9%**). Ingesting it puts un-attributed seed
web text into a corpus labelled synthetic — FinePhrase's trap in a different column.

🛑 **Cosmopedia's "21.7B tokens" is a MISTRAL-7B count, not dolma2.** `pool_tokens` is recorded as
`null` rather than a false figure — same non-summability rule as Common Pile's `GB × 0.25`.

✅ **CK-12 check applied and CLEARED:** `openstax` and `khanacademy` are Cosmopedia configs *named for
their seed sources*; **CK-12 is not among the 8 configs.** No CK-12 exposure in this corpus.

### B13 — 🛑 THE LICENSE BLOCKER ON ROWS 5 AND 6 (61.0B tokens, 6.1% of the corpus)
The real instrument, fetched in full (11,011 bytes) at the pinned sha, is
**`NVIDIA Data Agreement for Model Training (v. August 15, 2025)`** — **not** the card's
`license: other`, and **not** the *"NVIDIA Open Data License Agreement"* an earlier audit named
(**that name appears nowhere in the document** — corrected in place by the worker).
- **§2.1** limits use to *"**internal** training"*; **§2.2.2** forbids *"…or otherwise **make
  available to others** the Datasets."* **Verbatim the clause that blocked the shared reservoir.**
- ⚠️ **§3.3 collides with our own invariant:** on termination we must *"delete and destroy copies"*
  within 14 days — **but a frozen `vN` cannot be deleted or edited in place.** Publishing this source
  writes a contractual obligation we have architecturally disabled ourselves from honouring.
- **No drop-in substitute:** MegaMath-Web is HARMFUL (31.60 vs 44.20). Row 17
  (`Nemotron-Pretraining-Specialized-v1`, **CC-BY-4.0, ungated**) is clean but is only ~27.5B and is
  textbooks, not web math.
- 🛑 **Gate access is PER-ACCOUNT:** `HEAD` on `3/part_000000.parquet` → **HTTP 403,
  `X-Error-Code: GatedRepo`.** The 134.0B measurement was taken by a teammate whose token is
  authorized; **ours is not.** So rows 5/6 are blocked on *both* a license ruling and an access grant.
- **→ OWNER DECISION. Not decidable by this dossier.**

### B14 — 🛑 ROW 15 IS A FREEZE BLOCKER: 8.0B of the corpus has NO SOURCE
**Nobody has chosen a repo for "reasoning traces / worked examples."** Stated plainly rather than
filled with a plausible guess. The negative result is checkable:

| searched | result |
|---|---|
| `FINAL-DATASET-REPORT.md` | 3 mentions, **ZERO repo names**. §4's row is literally `reasoning traces / worked examples \| 8% \| 8.0B \| ~50B \| 0.16`; §7 gives an *ablation* (−1.5 MMLU / +8.4 GSM8K / +7.3 Minerva / +11.6 HumanEval) |
| `IMPLEMENTATION-PLAN.md` | zero repo names |
| `BUILD-DEPENDENCY-GRAPH.md`, `TASKS.md` | **zero mentions of "reasoning trace" at all** |
| `corpus-registry.json` | no row, no `reasoning` category |
| `artifacts/recount/` | no artifact |
| `source-encoding-audit.md` §13 | the **only** place candidates appear, and it says outright *"None of these is in our registry or `artifacts/recount/`, so every size figure here is CARD or UNVERIFIED"* |

**So the row has a share, a token target, an ungrounded `~50B` pool, and an ablation — but no source.**

**Two candidates, both MEASURED today, so the decision is cheap:**
- **A — `nvidia/Nemotron-Pretraining-Specialized-v1`, config `Nemotron-Pretraining-InfiniByte-Reasoning`**
  @ `9ed3718b5f2ae29074c5e34e64115432b7c4320f`. ✅ **Identical 5-leaf schema to Math-Textbooks**:
  `text_column = text`, `id_column = uuid`, flat, no trap. ✅ **`CC-BY-4.0`, ungated, and explicitly
  OUTSIDE the NVIDIA Data Access Agreement** (Specialized-v1 collection, not the CC-Math group) — so
  **it does NOT carry the §2.2.2 blocker.** Sizing `DERIVED`: 30 files / 28,345,959,943 bytes ×
  2.31772 text-per-parquet-byte ⇒ ~65.7 GB text ⇒ **15.2–18.6B dolma2 tokens**, ~2× headroom on 8.0B.
  Documents are **very** long (65,465 text bytes/doc ≈ 16k tokens), consistent with full traces.
  ⚠️ **An unresolved 32% row-count discrepancy flagged rather than hidden** (prior wave's `/size` says
  1,478,301 rows; byte-scaling file 0 implies ~1,003,504). **It does not affect the byte-based token
  estimate, which does not depend on row counts.**
- **B — the dolma3 reasoning directories** (same repo as row 14, so nearly free) — but inherits the
  `.jsonl.zst` reader gap and the schema fan-out of §B11.

### B15 — ⚠️ A correction to MY OWN tasking, so the next agent is not misdirected
I told workers `artifacts/reservoir/WEEK1-CORPUS-SURVEY.md` *"surveys ~35 corpora and is the best
index."* **It does not.** `MEASURED`: 195 lines, and it is a **code-reuse audit of the sibling
`pipelines/week1_corpus` checkout** (packers, S3 backends, decontamination, nine "traps worth
stealing"). Grepping for `reasoning`, `trace`, `gutenberg`, `books`, `1929`, `cosmopedia`, `nemotron`
returns **zero hits for all of them. It surveys MODULES, not corpora.**
**The best corpus index in this repo is `artifacts/impl-plan/source-encoding-audit.md`** (1,757
lines; **§14 is a one-row-per-source table**).

### B16 — ⚖️ RULED 2026-08-08: `source_label` discipline. **The org name is NOT the licence boundary.**

**Ruling (CEO, verified independently):** row 17's `source_label` is **`math-textbooks`**, and row 15
(`InfiniByte-Reasoning`, same `Specialized-v1` family) must **likewise NOT be labelled `nemotron-*`.**
Only the two CC-Math tiers carry **`nemotron-cc-math`**.

**Why — `MEASURED`.** All five candidate labels pass `SAFE_SEGMENT_RE` (`manifest.py:788`, called at
`corpus.py:284`), so the regex offers no protection. It cannot: **it validates characters WITHIN one
segment and is structurally incapable of comparing two labels.** And:
```
PREFIX COLLISION: 'nemotron-cc-math' is a prefix of 'nemotron-cc-math-3'
PREFIX COLLISION: 'nemotron-cc-math' is a prefix of 'nemotron-cc-math-4plus'
```
The failure that sells it: **`--prefix tokens/nemotron-` would sweep in `nemotron-math-textbooks`,
which is CC-BY-4.0 and EXPLICITLY CARVED OUT of the NVIDIA Data Agreement** — destroying 3.0B tokens
we are entitled to keep. **The org name is not the licence boundary; the instrument is.**

With `{nemotron-cc-math, math-textbooks}` there are **zero** remaining prefix collisions, and
`tokens/nemotron-cc-math/` is an **exact** enumeration of the restricted objects.

**Rejected: encoding licence state in the label** (`restricted-…`). It bakes a **mutable legal status
into an immutable key** inside `manifest_sha256`. Licence status changes; a frozen key cannot.

⚠️ **Must be set BEFORE FREEZE** — `source_label` is a path segment inside `manifest_sha256`, so a
later change is a republish, a full re-copy, and an ordinal rename.

**Why no schema change was needed:** `source` is a path segment that **Gate A recomputes** from the
key (`_check_labels_match_path`, `validate.py:1380`), so **the key IS the enumeration** —
`list-objects-v2 --prefix tokens/<source>/` is exact by construction. The mitigation for the owner's
§3.3 exposure already existed in the address shape.

### B17 — ✅ The Nemotron-CC-Math bytes are STAGED IN OUR ACCOUNT, verified from real footers
**✅ COPIED to a non-expiring prefix 2026-08-08: `s3://edullm-landing/_src/nemotron-cc-math-v1/`**
— 103 files / **169,606,727,240 bytes, byte-exact** to source, verified by recomputing the total.
`4plus_MIND/` deliberately NOT copied, so **the glob hazard is removed by the layout itself.**
No `manifest.json` written (the prefix holds 103 parquet files and nothing else).
**Read the registry rows against `_src/`, not against the scratch prefix.**

Original staging location, **`MEASURED` via broker (expires 2026-11-07):**

| prefix | files | bytes | note |
|---|---:|---:|---|
| `3/` | 57 | 107,417,646,757 | ✅ ingest |
| `4plus/` | 46 | 62,189,080,483 | ✅ ingest |
| **`4plus_MIND/`** | 90 | ~86 GiB | 🛑 **NEVER INGEST** |

🛑 **THE GLOB HAZARD IS NOW PHYSICAL: the excluded bytes sit in the SAME PARENT PREFIX as the wanted
ones.** A `nemotron-cc-math-v1/*` pattern **silently double-counts ~86 GiB of rewritten text**, and a
`startswith("4plus")` config match **also catches `4plus_MIND`.** **The registry MUST name `3/` and
`4plus/` by explicit prefix, and match config names with `==`. Two independent mechanisms fail the
same way.**

**Footer verification of `3/part_000000.parquet` (1,899,869,110 B, 993,681 rows, 4 row groups,
`created_by: Polars`), `MEASURED` over a ranged GET — this CLOSES W3's corroboration caveat:**
- ✅ `text_column = **text**`, flat top-level `large_string`, **exactly one leaf named `text`** — the
  FinePhrase trap cannot fire here.
- ✅ `id_column = **id**`, flat top-level `large_string`, 39,751,486 B / 993,681 rows = **40.0 B/id**,
  consistent with a UUID.
- ✅ **The 10-leaf schema matches the ungated NVIDIA sample repo EXACTLY.** W3 graded the real config
  `DERIVED (high confidence)` from the sample and named a 1-minute settling job; **this is that job,
  run against the real config-`3` bytes. Rows 5 and 6 upgrade to MEASURED.**
- ✅ `ContentLength` **1,899,869,110 is byte-identical to the HF tree API's size** for the same file,
  recorded independently before this prefix was known. **Two routes agree to the byte — these are the
  real upstream files, unmodified.**
- ⚠️ `metadata.category` compresses to **1,203 bytes over 993,681 values** — near-single-valued,
  independently confirming `domain_column = None`.

**⏳ EXPIRY, `MEASURED`:** `head-object` returns `expiry-date="Sat, 07 Nov 2026", rule-id=
"expire-working-objects"`, and the bucket lifecycle is **`Days: 90`, `Filter: {Prefix: ""}` — an
EMPTY prefix, i.e. the WHOLE BUCKET.** Written 2026-08-08 → **deleted 2026-11-07.** It does not
vanish imminently, **but this is a working directory on a 90-day clock, not a curated dataset.**
**Recommend a server-side copy into a non-expiring prefix before any build depends on it.**

### B18 — 🛑 ROW 14 (dolma3 midtrain QA, 14.0B) IS **DROPPED**. CEO ruling, 2026-08-08.
Free by the plan's own arithmetic (worst redistributed epoch **0.558** vs the unchanged **0.900**
max) and reversible before FREEZE. Four independent reasons converged:
1. **No reader** — `.jsonl.zst`, and `READABLE_FORMATS = {parquet, json.gz}`. 99,674 of 99,676 files.
2. **No copy escape hatch** — zero pre-tokenized shards in the repo (or in 8 dolma3 repos, ~487k
   files, all text). The "AI2 shards are byte-compatible" fact describes AI2's *training-data drops*,
   not any `dolma3_*` HF repo.
3. **Unbounded schema fan-out** — records disagree across directories (§B11); one registry row cannot
   cover it, and only **6 of 209** directories were surveyed.
4. 🔴 **The content is the least publishable in the corpus** — `reddit_to_flashcards` (40.7% of the
   QA pool by bytes) means **54.4 tok/doc, median 53.0, CV 0.212**, i.e. **79.6% of its documents sit
   below `MIN_DOC_TOKENS = 64`**. The floor does not trim a tail; **it deletes four fifths of the
   directory.** EOS margin 2.7× where every other source is 25–566×.

**Why DROP beat KEEP-and-exclude:** excluding one directory needs **directory-level selection, which
one registry row cannot express** — so keeping was not the smaller change.

✅ **Consequence: `zstandard` now has NO consumer.** Every remaining row is parquet (11) or json.gz
(6). **Do not add the dependency** — the `READABLE_FORMATS` gap closes by removal, and `#23`
(pin `tokenizers`, observed **0.22.2**) ships alone.
⚠️ **Still to fix independently:** the **three-table format divergence** — `READABLE_FORMATS`
(`corpus_build.py:127`), the **inline dict at `corpus_build.py:908-911` that actually runs**, and
`_READERS` (`corpus_read.py:748-752`, which uniquely accepts `jsonl.gz`) disagree. A **live
false-negative exists today: a `jsonl.gz` row is silently droppable despite a working reader.**
`read_documents` is dead code (tests only).

### B19 — ⚖️ TASK 1, AUTHORED: the `fineweb-edu` row is `config: data`. Four lines converge.

**The row, as authorized 2026-08-08:**
```json
{
  "key": "fineweb-edu",
  "category": "edu-web",
  "source_label": "fineweb-edu",
  "repo": "HuggingFaceFW/fineweb-edu",
  "config": "data",
  "revision": "87f09149ef4734204d70ed1d046ddc9ca3f2b8f9",
  "file_format": "parquet",
  "text_column": "text",
  "id_column": "id",
  "domain_column": null,
  "license": "ODC-BY-1.0",
  "share_alike": false,
  "target_tokens": 252000000000,
  "pool_tokens": 1583146000000
}
```
Revision **unchanged** — the config moves, the pin does not. Files: **2,410 / 4.523 TB**
(`MEASURED`, exhaustive tree read). Pool `1,583.1B` reproduces the report's §3 figure to 4 s.f. from
exact byte totals × a measured tokens/byte ratio. Draw is **15.9%** of pool.

**Why `data` and not either sample — four independent arguments, and they were not coordinated:**
1. **Arithmetic** — `sample/100BT` holds **100.24B MEASURED** against a **252B** draw: a 2.51×
   shortfall. It cannot supply the row at all.
2. **Headroom** — `sample/350BT` at 349.4B gives 252B only **1.04×** of margin.
3. 🔴 **Joinability, and this is the one that makes the choice SAFE rather than merely adequate.**
   `MEASURED`: the same FinePhrase file scores **0 hits across three `sample/350BT` files** and
   **2,085 hits in ONE `data/CC-MAIN-2013-20` file** — replicating to **4,170 on the second file,
   exactly 2.0000×**, with the content-intrinsic `url` control agreeing at **99.86%**.
   ⚠️ **So choosing `sample/350BT` would have satisfied the 252B size requirement while silently
   making the anti-join impossible — and it would have looked correct.**
4. **The owner's own repoint ruling** (`sample/100BT` → `data/`), issued independently.

**Collision, corrected:** **15.9% of pool / ~5.73B of the 36.0B FinePhrase draw** — real in kind but
**overstated 4.5× by `IMPLEMENTATION-PLAN.md` §10**, which computed 72.1% against `sample/350BT`.
⚠️ **And §4.3's "free fix" operates on `sample/350BT`, so as written it does NOTHING.**
Both queued for the doc sweep — **not edited piecemeal.**

**Two premise corrections that must not be lost:**
- **Task #21 is ALREADY IMPLEMENTED** (`corpus_build.py:1292`). `docs/TASKS.md:35` calls it unshipped
  "~5 lines". Its real residual is that it is **unexercised against live HF**.
- 🛑 **`sha256(id) % 4` was NEVER the anti-join.** `keeps_id(fmt, doc_id)` (`reservoir_ids.py:120`)
  takes a config and an id and is **structurally incapable** of expressing "drawn by another
  source". **Shipping #21 does not close gap 4.** They are separate items.
