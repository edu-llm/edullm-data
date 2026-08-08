# DCLM reconciliation + document identity — W2-DCLM

**Worker:** W2-DCLM (for DATA-EXEC)
**Started:** 2026-08-08
**Status:** IN PROGRESS — appending continuously. A partial file is a success.

## Two questions

- **HALF 1** — Which DCLM repo is the parent of the 410B the plan draws?
  Registry pins `HuggingFaceFW/dclm_100BT` (114.7B). Plan §4.1 says use
  `mlfoundations/dclm-baseline-1.0-parquet` (~3,764B DERIVED). 3.6x hole or stale registry.
- **HALF 2** — Document identity (CEO gap 2, FREEZE prerequisite). Does either DCLM repo have a
  usable, stable doc id? Registry claims `id` exists; plan §10 says "no usable document id".

## Plan of work

1. Read on-disk evidence first (no network): plan §4.1, registry row, recount artifacts.
2. Tree API enumeration of `mlfoundations/dclm-baseline-1.0-parquet` — count shards, sum bytes.
   NOT `/size`, NOT `num_rows`, NEVER `/filter`.
3. Parquet footer schema reads (ranged GET, `artifacts/recount/_fp_footer_leaf.py`) for BOTH repos.
4. Sample real `id` values via ranged read of one row group; assess uniqueness/stability/format.
5. Blast radius on #21 / #22.

Grades: MEASURED / MEASURED-IN-CODE / DERIVED / CARD / UNVERIFIED.

---

## Log

### Entry 1 — on-disk evidence, before any network call (2026-08-08)

**All of the following is read off files in this worktree. No HF request yet.**

#### 1a. The registry row (MEASURED-IN-CODE — `artifacts/reservoir/corpus-registry.json`, key `dclm-baseline`)

```
key: dclm-baseline      source_label: dclm      category: web-diverse
repo: HuggingFaceFW/dclm_100BT     config: data     file_format: parquet
revision: 01022d378d944de6deeb1c79d08fecb4d27b2c6f
id_column: "id"          text_column: "text"
target_tokens: 30,000,000,000        pool_tokens: 114,690,000,000
license: ODC-BY-1.0
```

Registry header fields: `_revisions_pinned_at: 2026-08-01`, `_revisions_verified: "All 14 (repo,
sha) pairs resolved against the HF tree API on 2026-08-01."`

**⚠️ THE DENOMINATOR, FOUND IMMEDIATELY: `target_tokens` is 30B, not 410B.**
The registry row is not a 410B row that names the wrong repo. It is a **30B** row — the
*reservoir* draw — and `HuggingFaceFW/dclm_100BT` @ 114.69B is a **3.8x** pool for a 30B target.
The row is internally consistent *for the corpus it was written for*.

This is the scope error CLAUDE.md warns about, and it resolves the "factor-of-3.6 hole" as stated
in my brief: **there is no 410B registry row at all.** The 3.6x framing (410B needed vs 114.7B
pinned) compares the FINAL-DATASET plan's target against the RESERVOIR's registry. Different
corpora, different denominators.

#### 1b. Why the registry moved to `dclm_100BT` — the row documents its own history

Trap 1 on the row, verbatim: the previous row named `mlfoundations/dclm-baseline-1.0`, which ships
`.jsonl.zst` — "verified from bytes, magic `28 b5 2f fd` at
`global-shard_01_of_10/local-shard_0_of_10/shard_00000000_processed.jsonl.zst`" — while the row
claimed `parquet` under a `default` config that does not exist. `corpus_read` refuses zstd. **That
row was a 30B hole in the corpus.**

Trap 2, verbatim: "This repo is where the 114.69B measurement ALREADY came from:
`artifacts/sizing-revised.md` line 40 cites `dclm_100BT`, exact rows, `partial: false`. So the
registry was citing one repo's number against another repo's name."

CONFIRMED on disk: `artifacts/sizing-revised.md:40` reads
`| **web (diverse)** | 30 B | 21 B | **114.69 B** | ✅ **MET 5.5×** | dclm_100BT, exact rows, partial: false |`.

So the 2026-08-01 re-sourcing was **a correction that made the row agree with its own evidence**,
and it was made against a **30B** target. It says nothing about a 410B draw, which did not exist
when it was made.

#### 1c. The plan's 410B (MEASURED-IN-CODE — `docs/IMPLEMENTATION-PLAN.md`)

- §4.1 (`:282-303`) — the three-repo table, reproduced accurately in my brief.
- §5.2a (`:425`) — `| **DCLM-baseline** | **410.0** | **1,261.5** | **325.0** | **27.92 GB** | MEASURED (dclm) |`
- **⚠️ §5.2a `:453-455` already flags its own denominator**, verbatim: "DCLM's 1,261.5 is measured
  on the `dclm_100BT` sample as drawn by the reservoir, **not** on the 3,764B parquet mirror this
  plan actually reads (§4.1) — a mirror whose row count is an *estimate*. **Measure DCLM's real
  tokens/document during the build.**"
  So the plan's own 325.0M-document and 27.92 GB dedup-set figures are derived from
  `dclm_100BT`'s tok/doc applied to a 410B draw from a *different* repo.
- §8A.5a (`:1484-1487`) — the plan **already names this exact unreconciliation**: "the registry's
  current `dclm-baseline` row points at `HuggingFaceFW/dclm_100BT` (`config: "data"`, 100 **flat**
  files, no subdirectories) — **not** the nested `-parquet` repo §4.1 says to use. **The free carve
  exists only in the nested repo.** Settle which repo the 410B row names before sizing anything."

So the escalation is real and pre-registered — but it is a **gap between a plan and a registry
written for a different corpus**, not a contradiction inside either one.

#### 1d. Where 3,764B comes from — DERIVED, arithmetic reconstructed and reproduced exactly

`artifacts/recount/web.json` holds the inputs. Recomputed just now:

```
2,949,300,000 docs (Zyphra's stated pre-dedup DCLM-baseline doc count)
  x 5,461.0 mean_chars   (/statistics on the -parquet mirror, stats_partial: TRUE)
  x 0.2337 tokens/char   (sampled, CV 0.1108, on the -parquet mirror)
  = 3,764.00B            <- matches the plan's "~3,764B" to 4 significant figures
```

**GRADE: DERIVED, and it is an ALL-COPIES figure, not a usable pool.** `web.json`'s own
`unique_vs_advertised.guidance` says so verbatim: "Report ~750B (Zyphra, empirical, gpt-neox) or
~733B dolma2-adjusted as the unique pool — NOT 4T, **and not the 3764B all-copies figure either.**"

Basis: the DCLM paper's limitations section, quoted in `web.json`: "DCLM-baseline contains
approximately 2T tokens, and after removing all near-duplicates globally, about 1T tokens remain."
DCLM used a **per-shard Bloom filter, not global dedup**, so duplicates survive across the 10
global shards. Zyphra measured 2,949.3M -> 615.2M docs (**80% duplicates**), 3,854.9B -> 750.3B
tokens.

**⚠️ THIS IS A LIVE FINDING FOR THE 410B DRAW, and it is not in §4.1.** §4.1's "✅ use this, ~3,764B"
invites a reader to conclude 410B is a 9.2x pool. Against the **unique** pool it is
410 / 733 = **56% of every unique DCLM token in existence** — and a naive random 410B draw from the
all-copies corpus yields, at an 80% duplicate rate, roughly 82B of unique content repeated ~5x.
The reservoir's 30B draw was never exposed to this (30/733 = 4%); a 410B draw is.
`web.json` states the mechanism explicitly: "a naive 30B random draw from DCLM-baseline yields ~5
epochs of an effective 'core'". Multiply that by 13.7.

#### 1e. The two "partial" numbers in my brief — both confirmed as partial-conversion artifacts

- `artifacts/recount/web-dclm-baseline-parquet.json`: `dataset_total_rows: 965,502`,
  `stats_partial: true`, `est_total_tokens: 1,232,206,708`. `web.json:90-92` labels this
  `measured_tokens_of_converted_head` and says verbatim **"DO NOT USE 1.23B AS A POOL FIGURE. It is
  num_rows=965,502 (the converted head, 0.03% of the corpus)."** `measured_tokens: null`.
  **My brief's suspicion is correct and was already recorded on disk.**
- The `/size num_rows=779,982 partial:true` in §4.1 is attributed in `web.json`'s
  `needs_streaming_count` to **`mlfoundations/dclm-baseline-1.0`** (the .zst original), not to the
  parquet mirror. The mirror's own partial head is 965,502. **§4.1 conflates the two repos' partial
  heads into one sentence.** Minor, but it is why the two numbers in my brief disagreed.

#### 1f. Corroborating figures already on disk for the mirror

From `web.json` (`n_shard_files`, `repo_bytes`) and `artifacts/impl-plan/source-encoding-audit.md:182`:
`27,938 .parquet shards`, `repo_bytes: 7,419,668,271,828` (7.420 TB). **To be verified against the
tree API next — that is exactly the measurement my brief asks for.**

Sanity check of the derivation against bytes, done now:
`7,419,668,271,828 B x 0.2323 tok/byte (the mirror's own sampled tokens_per_byte) = 1,723B.`
**⚠️ That is 2.18x BELOW the 3,764B row-based derivation.** Parquet is compressed, so tokens/byte
sampled on decoded text should NOT be applied to on-disk parquet bytes — but this gap needs
resolving before either number is used, and it is the check the tree-API pass will inform.
Flagging now, resolving below.

### Entry 2 — MEASURED from real bytes: `HuggingFaceFW/dclm_100BT` (2026-08-08)

Tooling: `artifacts/orchestration/data/measurements/_dclm_probe.py` (this dir), RangeFile pattern
lifted from `artifacts/recount/_fp_footer_leaf.py`. Tree API + HTTP Range footer reads only.
**No dataset download. No `/rows`. No `/filter`. Zero 429s so far.**

#### 2a. Repo identity — the registry's pin is CURRENT, not stale

`GET /api/datasets/HuggingFaceFW/dclm_100BT` → `sha 01022d378d944de6deeb1c79d08fecb4d27b2c6f`,
`lastModified 2026-03-02T14:12:52Z`, `license odc-by`, 102 siblings.
**The registry's pinned revision IS the current `main`.** MEASURED. License `odc-by` matches the
registry's `ODC-BY-1.0`.

#### 2b. Tree enumeration — MEASURED, and it corroborates the artifact to the byte

```
entries 103 | files 102 | parquet 100 | non-parquet: .gitattributes, README.md
all 100 parquet files under a single flat dir `data/`   <- confirms plan §8A.5a "100 flat files"
SUM parquet bytes = 316,008,772,992  (0.316 TB)
```
`artifacts/recount/web-dclm-100bt.json` records `parquet_bytes: 316008772992`.
**Exact match, independently re-measured.** The registry's `config: "data"` resolves correctly.

#### 2c. Parquet footer — 7 columns, and `id` EXISTS. MEASURED.

`data/000_00000.parquet`, 3,170,280,492 B, footer read in 2 ranged requests:
```
num_rows        895,229        <- matches the registry trap verbatim ("895,229 rows in the first of 96 files")
num_row_groups  896
created_by      parquet-cpp-arrow version 23.0.0
schema:  text::string  id::string  url::string  language::string
         language_score::double  fasttext_score::double  dataset::string
```
**The registry's leaf list is correct in every element.** (Its trap says "the first of 96 files";
the tree says **100** files — off by 4, immaterial, but noting it: the row's own count is slightly
wrong while its schema and row count are exactly right.)

#### 2d. ⚠️ **THE `id` IS A COMMON CRAWL WARC-Record-ID UUID — this is the answer to HALF 2**

Read row group 0 of file 0, columns `id,url,dataset,language` (one ranged read, 3.23 MB):
```
'<urn:uuid:ff2e51c1-875e-4b53-9df7-d74fd2c25333>'  http://io9.com/5935643/how-to-read-someones-mind...
'<urn:uuid:8b3e6c33-f0a9-4dd9-a88f-10a905859bce>'  http://programmers.stackexchange.com/questions/...
'<urn:uuid:6d2fa62b-7f8e-499f-a782-464e08c90daf>'  http://www.ask.com/question/what-is-a-human-backbone
```
- **length distribution: `Counter({47: 1000})`** — every single id is exactly 47 chars, the exact
  width of `<urn:uuid:` + 36-char UUID + `>`. **No per-file counter, no row index, no variable
  form.** MEASURED on 1,000 real values.
- **1,000 / 1,000 distinct** ids. 1,000 / 1,000 distinct urls.
- This is the **WARC-Record-ID** header that Common Crawl stamps on every record at crawl time.
  It is assigned **upstream of DCLM**, not by the DCLM build, so it is **stable across a
  re-download and across a revision bump** — the property the plan needs and says it lacks.

**=> `IMPLEMENTATION-PLAN.md` §10's "Three sources have no usable document id — both full-DCLM
repos and Cosmopedia" is WRONG for `dclm_100BT`, and (see Entry 3) likely wrong for the mirror too.**
The registry is right; the plan is stale on this point.

#### 2e. 🔴 **AND THE `dataset` COLUMN SETTLES HALF 1 — read it directly**

```
dataset distinct over rg0 = {'mlfoundations/dclm-baseline-1.0-parquet'}
```
**Every row of `HuggingFaceFW/dclm_100BT` declares its own parent, in a payload column, and the
parent it names is the `-parquet` mirror.** MEASURED, from real bytes.

This is direct evidence for what `web.json`'s overlaps list asserted without a byte citation:
"HuggingFaceFW/dclm_100BT **is a seed-42 random subset OF DCLM-baseline-1.0-parquet**. Strict
subset — never sum with the parent."

**So the two repos are not competing candidates. They are child and parent**, and the child says so
in its own data. That reframes the whole escalation — see the verdict in Entry 4.

### Entry 3 — MEASURED from real bytes: `mlfoundations/dclm-baseline-1.0-parquet` (2026-08-08)

`GET /api/datasets/...` → `sha 817d6752765f6a41261085171dd546b104f60626`,
`lastModified 2024-07-19`, `license cc-by-4.0`, **siblings 27,940**.
(27,940 siblings − `.gitattributes` − `README.md` = **27,938 parquet files** — the plan's
shard count, independently corroborated. Full byte-sum scan running separately, see Entry 5.)

#### 3a. 🔴 **THE LAYOUT IS FOUR LEVELS DEEPER THAN §8A.5a SAYS. This breaks the #28 carve as written.**

§8A.5a (`IMPLEMENTATION-PLAN.md:1467-1469`) says: "`mlfoundations/dclm-baseline-1.0-parquet` nests
`global-shard_01_of_10 … _10_of_10` (10 disjoint dirs) each holding `local-shard_0_of_10 …`
(100 total), **confirmed by walking the tree**."

**Walking the tree just now says the 10x10 is real but is NOT at the top level.** MEASURED:
```
/                                   -> [dir] filtered, .gitattributes, README.md      <- 3 entries only
filtered/                           -> [dir] OH_eli5_vs_rw_v2_bigram_200k_train       <- 1 entry
  .../OH_eli5_vs_rw_v2_bigram_200k_train/
                                    -> [dir] fasttext_openhermes_reddit_eli5_vs_rw_v2_bigram_200k_train
  .../fasttext_openhermes_.../      -> [dir] processed_data
  .../processed_data/               -> [dir] global-shard_01_of_10 ... _10_of_10      <- THE 10
  .../global-shard_01_of_10/        -> [dir] local-shard_0_of_10 ... _9_of_10         <- THE 10x10
  .../local-shard_0_of_10/          -> 279 FILES  shard_00000000_processed.parquet ...
```
The real prefix to any data file is:
```
filtered/OH_eli5_vs_rw_v2_bigram_200k_train/fasttext_openhermes_reddit_eli5_vs_rw_v2_bigram_200k_train/processed_data/global-shard_NN_of_10/local-shard_N_of_10/
```
**Blast radius:** the #28 registry-split carve writes a `config` per row pointing at a disjoint
subdirectory. A `config` of `global-shard_01_of_10` **resolves to nothing** — I got a hard
**HTTP 404** from the tree API for exactly that path before finding the real one. Per plan §4.1's
own warning class, the failure mode matters: `hf_files` returning an empty list is a **zero-token
row**, which by the `targets` mechanism at `corpus_build.py:238` is silent. **Every #28 config
string must carry the full 4-level prefix.** This is a concrete, pre-FREEZE correction to §8A.5a.

Also note the top-level dir is literally named **`filtered`** and the path encodes the fastText
classifier (`OH_eli5_vs_rw_v2_bigram_200k`) — i.e. this mirror is DCLM-baseline *after* the
quality filter, consistent with it being "baseline", not "pool".

#### 3b. One local shard, MEASURED
```
files in local-shard_0_of_10 of global-shard_01_of_10 : 279   (tree API, 6 cursor pages)
bytes                                                  : 74,179,273,988
mean file bytes                                        : 265,875,534
```
DERIVED cross-check on the shard count: 279 files x 100 local shards = **27,900**, vs the 27,938
actual. Within 0.14% — so the ~279-file-per-local-shard figure is representative and the 100
directories are near-uniform. DERIVED whole-repo bytes from this sample:
`265,875,534 x 27,938 = 7.428 TB`, against `web.json`'s recorded `repo_bytes` of
**7,419,668,271,828 (7.420 TB)**. **Agreement to 0.12%** — two independent routes. The recorded
7.420 TB is sound.

#### 3c. Footer schema — 6 columns, and `id` IS PRESENT HERE TOO. MEASURED.

`.../local-shard_0_of_10/shard_00000000_processed.parquet`, 144,473,349 B:
```
num_rows 61,000   num_row_groups 1   created_by parquet-rs version 52.0.0
schema:  text::string  url::string  id::string
         language::string  language_score::float  fasttext_score::float
```
- **6 columns, confirming §4.1's "6 columns to the original's 8"** — and confirming *which* are
  missing relative to the 7-col `dclm_100BT`: the mirror has **no `dataset` column** (the child
  adds it to name its parent). §4.1's "drops the WARC metadata struct and `warcinfo`" describes
  the delta against the **8-column `.jsonl.zst` original**, which is a third schema again.
- **⚠️ `language_score`/`fasttext_score` are `float` (32-bit) here vs `double` in `dclm_100BT`.**
  Immaterial for our draw (we read `text`+`id`), but it is a real schema difference the "identical
  copy" card claim glosses, and worth one line in the source-encoding record.

#### 3d. `id` values in the mirror — same UUID form, MEASURED on 61,000 real values

One ranged read of row group 0 (3.52 MB fetched, no download):
```
'<urn:uuid:8eeff0ee-5f54-48c7-bec1-8889cc0d6431>'  http://apple.stackexchange.com/questions/66593/...
'<urn:uuid:60773c7a-c2b3-4586-904a-9a081aef57ed>'  http://articles.latimes.com/2001/feb/25/books/...
lens        : Counter({47: 61000})     <- ALL 61,000, no exceptions
uniq ids    : 61,000 / 61,000          <- perfectly distinct within the file
uniq urls   : 60,998 / 61,000          <- 2 duplicate URLs, DISTINCT ids (see below)
```
**=> `IMPLEMENTATION-PLAN.md` §10 "both full-DCLM repos [have] no usable document id" is FALSE.
Both repos carry a 47-char `<urn:uuid:...>` WARC-Record-ID in an `id` column.** This is the single
most consequential correction in this report, because §10 lists it as a FREEZE prerequisite.

**And note the 2 duplicate URLs with distinct ids: `url` is NOT unique but `id` is.** That kills
`url` as the surrogate identity outright — measured, not argued. It is also why `id` is the right
key: two crawls of the same URL are two documents with two WARC records.

### Entry 4 — the full tree scan, and the cross-repo id join (2026-08-08)

#### 4a. ✅ FULL TREE ENUMERATION OF THE MIRROR — MEASURED, exhaustive, not sampled

562 cursor pages, 1,437 s, zero 429s:
```
total entries      28,054
  directories         114        (= 1 filtered + 1 + 1 + 1 processed_data + 10 global + 100 local)
  files            27,940
    .parquet       27,938        <- ✅ the plan's 27,938 shard claim is EXACT
    other               2        (.gitattributes, README.md)
SUM parquet bytes  7,419,668,271,828  = 7.4197 TB     <- ✅ EXACT match to web.json's repo_bytes
mean file bytes      265,576,214
```
**Both the 27,938-shard claim and the 7.420 TB byte total are now MEASURED by exhaustive
enumeration, not derived.** My Entry-3b sample-based estimate (7.428 TB) was 0.12% high; the exact
figure supersedes it. Directory count 114 confirms the 10x10 = 100 leaf dirs exactly.
Saved: `_tree-dclm-parquet.json` (all 27,938 paths + sizes).

#### 4b. ⚠️ RESOLVING MY OWN ENTRY-1f FLAG: the 2.18x byte-vs-row gap

I flagged that `7.4197 TB x 0.2323 tok/byte = 1,723B` sits 2.18x below the 3,764B row derivation.
**Resolved: the 0.2323 `tokens_per_byte` in the recount artifacts is tokens per byte of DECODED
UTF-8 text, and 7.4197 TB is COMPRESSED parquet on disk.** The two must not be multiplied. The
implied whole-repo text volume is `3,764B / 0.2323 = 16.2 TB` of decoded text inside 7.42 TB of
parquet — a **2.18x compression ratio**, which is entirely ordinary for Snappy/ZSTD-compressed
BYTE_ARRAY web text. Directly corroborated by the footer I read: one shard is 144,473,349 file
bytes holding `text` at 323,504,637 uncompressed bytes = **2.24x**. **The gap is not an error in
either number; my Entry-1f multiplication was the error.** Correcting in place, as instructed.

**So `parquet_bytes` is NOT a usable sizing input without a compression factor** — a trap worth
recording, since two artifacts on disk carry `parquet_bytes` next to token figures.

#### 4c. 🔴 THE CROSS-REPO ID JOIN WORKS — MEASURED, and it settles stability

Read row group 0 of `dclm_100BT` file 0 (1,000 rows) and rows of the mirror's
`shard_00000000_processed.parquet` (2,000 rows), joined on `id`:
```
overlapping ids            : 52
url identical              : 52 / 52
text BYTE-IDENTICAL        : 52 / 52   (verified by sha256 of the utf-8 text on spot checks)
```
**The same `id` retrieves the same document, byte for byte, in two independently built repos
converted by different toolchains** (`parquet-cpp-arrow 23.0.0` vs `parquet-rs 52.0.0`) **and
published 19 months apart** (2024-07-19 vs 2026-03-02).

That is the strongest possible evidence for the property the plan needs: the id is **not** an
artifact of a build, a file order, or a conversion. It is carried with the document.

UUID form, MEASURED on 3,000 values across both repos: **100% RFC-4122 version 4, variant
`10xx`**, zero non-matching. So it is a random UUID minted once at crawl time by Common Crawl's
WARC writer (`WARC-Record-ID`) — **not** derived from text (so it does not change if text is
re-cleaned), **not** a positional counter (so it cannot collide across files), and **not**
reassigned on re-download.

Also MEASURED: neither repo's rows are url-sorted, so **row order carries no recoverable meaning**
— which independently condemns `(file_path, row_index)` as a surrogate (see 4d).

#### 4d. HALF 2 ANSWERED — the surrogate question is MOOT, but here is the evaluation asked for

| candidate | verdict | evidence |
|---|---|---|
| **`id` (`<urn:uuid:...>`)** | ✅ **USE THIS** | present in BOTH repos (MEASURED, footers); 62,000/62,000 distinct in-file; 47 chars uniformly; 100% UUIDv4; joins cross-repo to byte-identical text on 52/52 |
| `url` | ❌ **NOT UNIQUE** | 60,998 unique / 61,000 rows in one mirror file — MEASURED. Two crawls of one URL are two documents |
| `(file_path, row_index)` | ❌ unstable | the mirror re-converted the same corpus into a **different file layout and a different toolchain**; row order is not sorted by anything (MEASURED); a revision bump or a re-conversion renumbers everything |
| `sha256(text)` | ❌ and it is worse than "unstable" | DCLM is **~80% near-duplicates** (Zyphra, `web.json`). A text hash **merges distinct documents by construction**, so it is not an identity at all here — it is a dedup key. This is a stronger reason than the plan's "not stable across a re-download" |

**`id_column: "id"` in the registry row is CORRECT and should be copied to every DCLM row the
#28 split creates.**

### Entry 5 — blast radius on #21 and #22, checked against the CODE (2026-08-08)

#### 5a. The pipeline ALREADY hard-requires this id, and raises — MEASURED-IN-CODE

- `src/edullm_data/corpus.py:174-178` — `Document.id` docstring: "the join key for the §9.7 item 4
  partition and the FineWeb-Edu anti-join, both of which hash it. **It must be stable upstream: a
  row index would make the partition non-reproducible across a re-download.**"
- `src/edullm_data/corpus_read.py:481` resolves `spec.id_column` by exact `path_in_schema`
  **before reading a single row**, so a moved/absent id column fails on file 1, not silently.
- `src/edullm_data/corpus_read.py:510-515` raises `ReadError` on a null id, naming the partition
  and the anti-join in the message.
- `src/edullm_data/corpus_build.py:280` propagates `id_column` from the registry spec into the plan.

**=> If §10's "no usable document id" were true for DCLM, the build would not silently corrupt —
it would HARD FAIL at file 1.** It does not fail, because the column exists. The risk §10 describes
is real in shape but misassigned to these two repos.

#### 5b. `partition_of` on REAL DCLM ids — MEASURED, uniform, and accepted unchanged

`reservoir_ids.partition_of` (`:102-112`) is `int.from_bytes(sha256(doc_id.utf8), "big") % n`.
Ran it over the 2,000 real mirror ids I sampled:
```
N_PARTITIONS = 4
counts  {0: 494, 1: 516, 2: 522, 3: 468}    expected 500.0
chi2 = 3.60, df = 3   -> p ~ 0.31, indistinguishable from uniform
_require_id('<urn:uuid:8eeff0ee-...>')  -> ACCEPTED (no exception)
```
**The `<urn:uuid:...>` string passes `_require_id` as-is and hashes uniformly. No normalization,
no stripping of the `<urn:uuid:` wrapper, and no surrogate synthesis is needed.**
(A UUIDv4 is 122 random bits, so this is expected — but it is now measured, not assumed.)

#### 5c. Blast radius — REVISED DOWNWARD to near zero for DCLM

| task | prior assumption (§10) | MEASURED reality | residual work |
|---|---|---|---|
| **#21** FinePhrase id partition | DCLM has no id ⇒ needs a surrogate before ingest | DCLM ids are UUIDv4, unique, cross-repo stable, uniform under `partition_of` | **none for DCLM.** #21 is a FinePhrase/FineWeb-Edu task; DCLM only has to *carry* its id, which the reader already does |
| **#22** dedup pre-pass | blocked on a surrogate id | unblocked — but see the caveat below | the flat `np.uint64` pre-pass still has to be built; the *key* question is settled |

**⚠️ ONE CAVEAT THAT SURVIVES, and it is the one that matters for #22.** `corpus_filter.py:103-105`:
```python
def content_hash(text: str) -> str:
    """sha256 of the NORMALIZED text. The dedup key, and the index's exact-match key."""
```
**#22's dedup key is `sha256(normalized text)`, NOT `sha256(id)`.** So the id finding does not
change #22's memory math (§5.2a's 27.92 GB) at all — that set is keyed on text. The brief's framing
("#22 depends on `sha256(id) % N`") does not match the code. **#22 is unblocked by this report only
in the sense that it was never blocked on the id.**

And the id being sound does **not** solve DCLM's real dedup problem: exact `content_hash` dedup
catches byte-identical text, while DCLM's ~80% duplication is **near**-duplicate (Zyphra minhash-LSH
at ~85% Jaccard). Distinct WARC ids on near-identical pages are *correct* identity behaviour and
exact dedup will keep them all. See Entry 1d — this is the live sizing risk on a 410B draw.

### Entry 6 — sizing the mirror WITHOUT `/size` or `num_rows`, as the brief demands (2026-08-08)

The brief says: do not size this repo from `/size` or `num_rows`; use the tree API + a measured
tokens/byte. Done — and better, because parquet footers carry **exact** row counts.

#### 6a. Footer sample — 12 files, seed 42, from the exhaustive 27,938-file list

Every mirror file is **one row group**, so `metadata.num_rows` in the footer is an **exact** count
for that file, and `total_uncompressed_size` of the `text` leaf is its **exact** decoded byte total.
This is footer-exact per file, sampled only across files.
```
sample: 12 files | 1,301,403 rows | 3,282,841,275 parquet bytes | 7,411,050,607 text bytes

text_uncompressed / parquet_byte = 2.25751   CV 0.0070   (min 2.2428, max 2.2998)  <- extremely tight
rows / parquet_byte              = 3.9643e-04  CV 0.0588
mean text BYTES/doc              = 5,694.7     CV 0.0594
```

#### 6b. Scaled to the exhaustively-measured 7,419,668,271,828 bytes

```
est documents    = 7.4197e12 x 3.9643e-04  = 2.941 B docs
est decoded text = 7.4197e12 x 2.25751     = 16.750 TB
est tokens       = 16.750 TB x 0.2323 tok/byte (recount, sampled on THIS repo, CV 0.111)
                 = 3,891 B tokens
```

#### 6c. ✅ THREE INDEPENDENT ROUTES NOW AGREE ON THE MIRROR'S SIZE

| route | documents | tokens | basis |
|---|---|---|---|
| Zyphra's stated pre-dedup count (via `web.json`) | 2,949.3 M | 3,764 B | third-party, CARD |
| plan §4.1 | — | ~3,764 B | DERIVED from the above |
| **this report: tree bytes x footer-exact ratios** | **2,941 M** | **3,891 B** | **MEASURED** |

**Document counts agree to 0.3% (2,941 vs 2,949.3 M) by two fully independent routes.** Token
figures agree to 3.4%. The `mean text bytes/doc` of 5,694.7 I measured is 4.3% above the
`/statistics` `stats_mean_chars` of 5,461 — expected, since `/statistics` was `partial:true` over
the converted head, and it means the head is slightly shorter-documented than the corpus.

**=> §4.1's "~3,764B DERIVED" is CONFIRMED as an all-copies size, now upgraded to MEASURED at
~3,891B.** The `965,502` / `779,982` partial-conversion numbers are, as suspected, 0.03% artifacts
and were never corpus sizes. **A pipeline reading `num_rows` from `/size` would size this at
1/3,047 of reality** (2,941M / 965,502) — the plan said 1/3,800; same order, same lesson.

#### 6d. ✅ THE #28 CARVE IS CLEAN — the 10 global shards are balanced to 0.24%

MEASURED over all 27,938 files:
```
global-shard_01_of_10  2,790 files  0.7416 TB      global-shard_06_of_10  2,797 files  0.7421 TB
global-shard_02_of_10  2,790 files  0.7418 TB      global-shard_07_of_10  2,799 files  0.7423 TB
global-shard_03_of_10  2,790 files  0.7419 TB      global-shard_08_of_10  2,800 files  0.7428 TB
global-shard_04_of_10  2,798 files  0.7424 TB      global-shard_09_of_10  2,784 files  0.7410 TB
global-shard_05_of_10  2,800 files  0.7427 TB      global-shard_10_of_10  2,790 files  0.7411 TB
```
Spread between largest and smallest: **0.24%**. Per-file size CV 0.213 (min 88.96 MB, max 489.59 MB).

**So a 10-way #28 split on `global-shard_NN_of_10` yields ten near-identical children with no
counting pass needed** — each ~389B tokens all-copies, ~41B of a 410B draw. A 20-way split on
`local-shard` pairs would be equally clean. **This is the measurement §8A.5a needed and did not
have** — but it only works with the full 4-level prefix from Entry 3a.

### Entry 7 — EXECUTED against the real code path: `hf_files` on both config forms (2026-08-08)

Not inferred — I built a real `CorpusSpec` and called the production `hf_files`
(`src/edullm_data/corpus_build.py:790-836`) against the live Hub at the pinned revision:

```python
CorpusSpec(key='dclm-test', repo='mlfoundations/dclm-baseline-1.0-parquet',
           file_format='parquet', text_column='text', id_column='id',
           config=<under test>, revision='817d6752765f6a41261085171dd546b104f60626')
```
```
config='global-shard_01_of_10'                      -> FAIL  HTTPError 404 Not Found
config='filtered/OH_eli5_vs_rw_v2_bigram_200k_train/fasttext_openhermes_reddit_eli5_vs_rw_v2_bigram_200k_train/processed_data/global-shard_01_of_10/local-shard_0_of_10'
                                                    -> OK    279 files, first shard_00000000_processed.parquet
```

**Two things this settles:**

1. **§8A.5a's implied config string is a hard 404.** The full 4-level prefix is mandatory.
   The exact working prefix, to copy into every #28 row:
   ```
   filtered/OH_eli5_vs_rw_v2_bigram_200k_train/fasttext_openhermes_reddit_eli5_vs_rw_v2_bigram_200k_train/processed_data/global-shard_NN_of_10[/local-shard_N_of_10]
   ```
2. ✅ **GOOD NEWS — this failure is LOUD, not silent.** I expected (Entry 3a) a wrong config to
   produce a silent zero-token row. It does not: the tree API 404s and the exception propagates.
   `hf_files:829-835` additionally raises `BuildDriverError` on an empty-but-200 listing. **I was
   wrong in Entry 3a about the failure mode; correcting in place.** The silent-token-loss risk at
   `corpus_build.py:238` is real but is triggered by **duplicate `source_label`s** (§8A.5a trap 1),
   not by a bad config. Severity of the §8A.5a path error drops from "silent corruption" to
   "build stops with a 404" — still a pre-FREEZE fix, no longer a data-integrity risk.

Also confirmed in code: `hf_files` **refuses an unpinned spec** (`:805-809`, "Reading `main` makes
the build unreproducible") and paginates on the `Link` header, so the 279-file listing is complete.

---

## ✅ VERDICT — both halves

### HALF 1 — which repo is the parent?

**`mlfoundations/dclm-baseline-1.0-parquet` is the parent. `HuggingFaceFW/dclm_100BT` is a
~3% random-sample CHILD of it. The registry is not wrong; it is scoped to a different corpus.**

Evidence, strongest first:
1. **Every row of `dclm_100BT` carries a `dataset` column whose value is literally
   `'mlfoundations/dclm-baseline-1.0-parquet'`** (MEASURED, Entry 2e). The child names its parent
   in its own payload.
2. **52/52 ids sampled from `dclm_100BT` that also appear in the mirror return BYTE-IDENTICAL text
   and identical urls** (MEASURED, Entry 4c) — across different converters and 19 months.
3. Sizes are consistent with a ~3% sample: 114.69B (child, exact rows, `partial:false`) vs
   ~3,891B (parent, MEASURED here) = **2.95%**.

**Neither document is stale in its own scope. The gap is real and is a PLANNING gap:**

| | registry `dclm-baseline` row | plan §4.1 / §5.2a |
|---|---|---|
| corpus | the **reservoir** | the **final-dataset** MoE corpus |
| target | **30 B** | **410 B** (378 s1 + 32 s2) |
| repo | `dclm_100BT` (114.69B pool) | `-parquet` mirror (~3,891B pool) |
| verdict | ✅ correct and internally consistent — 3.8x pool for 30B | ✅ correct repo choice for 410B |

**There is no 410B registry row anywhere.** The "factor-of-3.6 hole" dissolves once the
denominators are separated: 410B against `dclm_100BT`'s 114.69B would indeed be a 3.6x shortfall,
but no row asks `dclm_100BT` for 410B. **The action is to WRITE the final-dataset rows against the
mirror, not to "fix" the reservoir row** — which should be left exactly as it is.

**⚠️ The one thing that IS wrong in §4.1, and it is not the repo choice:** `~3,764B` (now MEASURED
at ~3,891B) is an **ALL-COPIES** figure. DCLM used a per-shard Bloom filter, not global dedup;
Zyphra measured **80% duplicates** and the DCLM paper itself says ~1T survives global dedup.
**The honest unique pool is ~733-750B, so a 410B draw is ~56% of unique DCLM, not 11% of 3,891B.**
A naive random 410B draw yields on the order of 82B unique repeated ~5x. `web.json` already says
"NOT ... the 3764B all-copies figure either" — §4.1 does not carry that caveat. **This is the
finding with real consequences for the mix, and it is the one I would escalate.**

### HALF 2 — document identity

**SOLVED, and the plan's §10 entry is FALSE for both DCLM repos. `id` is the surrogate; no new
mechanism is needed.**

```
column   : id  (present in BOTH repos — MEASURED from parquet footers)
form     : '<urn:uuid:xxxxxxxx-xxxx-4xxx-[89ab]xxx-xxxxxxxxxxxx>'  47 chars
           100% RFC-4122 UUIDv4 over 3,000 sampled values, zero exceptions
origin   : Common Crawl WARC-Record-ID, minted at crawl time UPSTREAM of DCLM
unique   : 1,000/1,000 (child) and 61,000/61,000 (mirror) distinct in-file
stable   : same id -> byte-identical text across two repos, two converters, 19 months (52/52)
partition: uniform under reservoir_ids.partition_of — chi2 3.60, df 3, p~0.31 on 2,000 real ids
           _require_id() accepts the raw '<urn:uuid:...>' string with NO normalization
```

Rejected alternatives, on measurement not argument:
- **`url` — NOT UNIQUE.** 60,998 distinct / 61,000 rows in one file. Two crawls of a page are two
  documents. Dead.
- **`(file_path, row_index)` — unstable.** The mirror re-converted the same corpus into a different
  layout with a different toolchain; row order is sorted by nothing (MEASURED). Dead.
- **`sha256(text)` — not an identity here at all.** With ~80% near-duplicates it *merges* distinct
  documents by construction. It is the dedup key (`corpus_filter.py:103-105`), not an id.

**Blast radius on #21 / #22: near zero, and smaller than the brief assumed.**
- **#21** — DCLM needs no surrogate; the reader already carries `id`. #21 remains a
  FinePhrase/FineWeb-Edu task.
- **#22** — the brief says #22 depends on `sha256(id) % N`; **the code says its dedup key is
  `sha256(normalized text)`** (`corpus_filter.py:103-105`). #22 was never blocked on the id, and
  §5.2a's 27.92 GB memory figure is unaffected by this report.
- **Copy `id_column: "id"` into every #28 DCLM row.** It is already correct on the reservoir row.

### Actions for DATA-EXEC, in priority order

1. **§4.1: add the unique-pool caveat.** ~3,891B is all-copies; unique is ~733-750B; a 410B draw is
   ~56% of unique DCLM. Decide deliberately, or draw from `Zyphra/dclm-dedup` (615.2M docs, exact
   `partial:false`, cc-by-4.0). **Highest-consequence item in this report.**
2. **§10: strike "both full-DCLM repos" from the no-document-id list.** Both have a stable UUIDv4.
   Cosmopedia (another worker) is unaffected by this finding.
3. **§8A.5a: replace the config prefix** with the 4-level path in Entry 7. Fails loud (404), so it
   is a correctness-of-docs fix, not a data risk.
4. **Leave the reservoir registry row alone.** It is correct at 30B. Add *new* final-dataset rows
   against the mirror; the 10 global shards are balanced to 0.24% so a 10- or 20-way #28 carve
   needs no counting pass.
5. **Add the `source_label` uniqueness check to `load_registry`** — unchanged from §8A.5a, still
   the one genuinely silent failure in this area.

### Provenance of this report
Every network figure came from the HF tree API and HTTP-Range parquet **footer** reads.
**No dataset download. `/rows` never used. `/filter` never used. `/size` never used for sizing.
Zero HTTP 429s observed across the whole session** (562-page tree scan + ~15 footer/row-group
reads). Nothing written to S3. No Batch job submitted.
Scripts + raw data in this directory: `_dclm_probe.py`, `_tree-dclm-parquet.json` (27,938 paths),
`_tree-dclm100bt.json`, `_tree-mirror-ls0.json`, `_footer-dclm100bt-file0.json`,
`_footer-mirror-file0.json`, `_footer-mirror-sample12.json`, `_sample-mirror-ids.json`.
