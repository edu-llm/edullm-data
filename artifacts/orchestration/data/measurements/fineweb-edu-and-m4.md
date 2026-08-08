# W6-FWEDU — FineWeb-Edu / FinePhrase anti-join (Part A) + M4 mean doc length (Part B)

**Worker:** W6-FWEDU · **Date:** 2026-08-08 · **Status:** COMPLETE (both parts; residual items in §Open)
**Grading:** `MEASURED` / `MEASURED-IN-CODE` (file+line) / `DERIVED` (arithmetic shown) / `CARD` / `UNVERIFIED`

---

## THE FIVE THINGS TO READ IF YOU READ NOTHING ELSE

1. 🟢 **The FinePhrase anti-join IS implementable — `id` is a stable cross-repo key.** §A3c joined a
   FinePhrase file to FineWeb-Edu `data/` and got **2,085 `id` hits, corroborated by 2,087 `url`
   hits** (99.90% agreement). An earlier, scarier reading — that the key might be broken — is
   **measured and withdrawn.**
2. 🛑 **The `fineweb-edu` registry row must become `config: data`, NOT `sample/350BT`.** FinePhrase's
   parent documents are **absent** from `sample/350BT` at the pinned revision (**0 hits / 3 files**)
   and **present** in `data` (2,085 hits / 1 file). Moving the row to `sample/350BT` would satisfy
   the 252B size requirement while making the anti-join **impossible** — and it would look fine.
3. 🛑 **`IMPLEMENTATION-PLAN.md` §10's "~72%" is arithmetically correct but computed against the
   wrong pool.** The real figure is **15.9% of the pool consumed, ~5.73B of the 36.0B FinePhrase
   draw colliding** — the concern is real in kind, **overstated 4.5× in degree.** §4.3's "free fix"
   operates on `sample/350BT` and therefore does nothing.
4. 🛑 **`sha256(id) % 4` was NEVER the anti-join, and task #21 is ALREADY IMPLEMENTED.** `keeps_id`
   is *intra*-FinePhrase dedup (`corpus_build.py:1292`); its signature cannot express "drawn by
   another source". **Shipping #21 does not close gap 4** — a separate mechanism is still unwritten.
5. 🔴 **M4 found one real blocker: dolma3 `reddit_to_flashcards` at 54.4 tok/doc** — EOS fraction
   **0.018386, only 2.7× under the 0.05 bound**, with **79.6% of documents below
   `MIN_DOC_TOKENS = 64`**. Every other stage-2 source is 25–566× clear. **M4 = RESOLVED WITH A
   BLOCKER, not resolved.**

> This file was appended to continuously and **corrects itself in place, visibly.** Where a later
> measurement reversed an earlier one, both are shown — see §Corrections.

---

## PART A — FineWeb-Edu configs, pools, and the FinePhrase anti-join

### A0. Baseline: what the repo currently says (to be checked)

- `artifacts/reservoir/corpus-registry.json` (row 2 of `corpora`): repo `HuggingFaceFW/fineweb-edu`,
  **config `sample/100BT`**, `pool_tokens: 100240000000`. Revision pinned repo-wide at
  `_revision_dates["HuggingFaceFW/fineweb-edu"] = "2025-07-11T20:16:53.000Z"`.
  **MEASURED-IN-CODE.** ⚠️ **This row is CORRECT FOR THE RESERVOIR** (which drew 20B) and is a
  **scope mismatch**, not an error, for this corpus. Do not report it as anyone's mistake.
- `docs/IMPLEMENTATION-PLAN.md:321-323` and `:1901` — the two statements quoted in the brief.
- Prior art already in-repo, and it is substantial — **I did not redo it**:
  - `artifacts/impl-plan/source-encoding-audit.md:288-320` (§2a/2b) — the config inventory and the
    "252B cannot come from `sample-100BT`" derivation.
  - `scripts/measure_finephrase_overlap.py` (1,486 lines) + `scripts/README-finephrase-overlap.md`
    (383 lines) — a **written but NEVER-RUN** census of the FinePhrase↔FineWeb-Edu id overlap.
    `EDUWEB_DEFAULT = "sample/350BT"` at `scripts/measure_finephrase_overlap.py:145`.
  - `artifacts/recount/edu-web-fineweb-edu.json` — the dolma2 measurement of `sample-100BT`.

### A1. `sample/350BT` EXISTS. Spelling, revision, layout, columns

**MEASURED** — HF tree API, `2026-08-08`, at the pinned revision.

| item | value | grade |
|---|---|---|
| repo | `HuggingFaceFW/fineweb-edu` | MEASURED |
| **full 40-char revision** | **`87f09149ef4734204d70ed1d046ddc9ca3f2b8f9`** | MEASURED (`/api/datasets/.../revision/<sha>` echoes `sha` = same 40 chars; `lastModified 2025-07-11T20:16:53.000Z`, which matches the registry's `_revision_dates` entry to the second) | 
| **repo path spelling** | **`sample/350BT`** (a directory) | MEASURED |
| **config-name spelling** | **`sample-350BT`** (the HF *config* id) | CARD |
| format | parquet | MEASURED |
| files | **472** | MEASURED |

🔴 **BOTH SPELLINGS ARE CORRECT AND THEY ARE NOT INTERCHANGEABLE.** `sample-350BT` is the
**config name** (what you pass to `load_dataset(..., "sample-350BT")` and what FinePhrase's card
declares in `source_datasets`). `sample/350BT` is the **repo path prefix** (what a tree API call or
an `hf://` glob needs). The registry's `config` field stores the **path** form
(`sample/100BT`), so a `sample-350BT` string written into that field would resolve to nothing.
This is not a docs inconsistency to clean up — it is two different identifiers.

**Directory inventory of the whole repo at that revision (3,038 siblings), MEASURED:**
`data/CC-MAIN-*` (110 dump dirs, 2,410 files) · `sample/350BT` (472) · `sample/100BT` (140) ·
`sample/10BT` (14) · `README.md` · `.gitattributes`.

**Columns:** `text` is the payload, `id` is the join key. Confirmed by footer read in §A3 below —
`path_in_schema` for the payload is the **top-level `text`** (flat, unlike FinePhrase's nested
`rollout_results.list.element.text`). **There is no FinePhrase-shaped column trap in FineWeb-Edu.**

### A2. Pool measurement — THE THREE DENOMINATORS

**MEASURED** — exact LFS byte totals from the tree API at
`87f09149ef4734204d70ed1d046ddc9ca3f2b8f9`, `recursive=1&expand=1`, cursor-paginated to exhaustion:

| config path | files | exact parquet bytes | TB |
|---|---:|---:|---:|
| `data` (FULL) | **2,410** | **4,522,727,684,984** | 4.5227 |
| `sample/350BT` | **472** | **998,102,051,512** | 0.9981 |
| `sample/100BT` | **140** | **286,394,522,604** | 0.2864 |
| `sample/10BT` | 14 | 28,518,193,415 | 0.0285 |

**The anchor, MEASURED (not mine — prior wave, `artifacts/recount/edu-web-fineweb-edu.json`):**
`sample-100BT` = **100,244,242,760 dolma2 tokens** over **97,270,686 rows**, by the factored
estimator (`num_rows × /statistics mean_chars 4762.8 × tokens/char 0.21638`, `tokens_per_char` CV
0.1005). Its recorded `parquet_bytes` is **286,394,522,604 — byte-identical to my independent tree
read above**, which cross-validates both.

**DERIVED — dolma2 tokens per parquet byte, from the one anchored config:**

```
r = 100,244,242,760 / 286,394,522,604 = 0.3500215 dolma2 tok / parquet byte
```

**DERIVED — the other two denominators by scaling that ratio:**

```
sample/350BT :  998,102,051,512 × 0.3500215 = 349,352,000,000  ->  ~349.4 B dolma2
data (FULL)  : 4,522,727,684,984 × 0.3500215 = 1,583,146,000,000 -> ~1,583.1 B dolma2
```

🔴 **THAT REPRODUCES `FINAL-DATASET-REPORT.md` §3's 1,583.1B TO FOUR SIGNIFICANT FIGURES.**
The agreement is not a coincidence — it is the same arithmetic. **So the report's FineWeb-Edu row is
sized against the FULL `data` config (~1,583B), NOT against any sample.** That resolves the
denominator question the brief flagged:

| plan number | its denominator | grade |
|---|---|---|
| **1,583.1B** (`FINAL-DATASET-REPORT.md` §3 pool) | **`data` — FULL FineWeb-Edu, 2,410 files, 4.52 TB** | DERIVED, reproduced exactly |
| **252B** (report §3 draw) | **not stated** — but 252/349.4 = **72.1%** of `sample/350BT`, which is precisely the "~72%" in `IMPLEMENTATION-PLAN.md:1901`. So the plan's §10 text is denominated against **`sample/350BT`.** | DERIVED |
| **100.24B** (registry `pool_tokens`) | **`sample/100BT`** — the RESERVOIR's config | MEASURED |

**⚠️ SO THE PLAN AND THE REPORT ARE DENOMINATED AGAINST DIFFERENT CONFIGS, AND THAT IS THE WHOLE
CONTRADICTION.** The report sizes the pool from `data` (1,583B, where 252B is a comfortable 15.9%
draw); §10 of the plan reasons about the collision from `sample/350BT` (349B, where 252B is a
crushing 72.1% draw). **Neither is wrong about its own denominator.** They give opposite answers
because they are not talking about the same pool — this is exactly the scope-error class the
consistency audit named.

**The `sample-350BT` card figure is GPT-2, not dolma2.** CARD, verbatim (via
`artifacts/impl-plan/source-encoding-audit.md:296`): *"`sample-350BT`: a subset randomly sampled
from the whole dataset of around 350B **gpt2** tokens."* My DERIVED dolma2 figure of **349.4B**
happens to agree with the gpt2 promise to 0.2%, which is consistent with the measured
`dolma2_to_upstream_ratio: 0.9946` on this corpus — but that is an empirical coincidence per corpus,
never a rule.

**Verdict on the brief's question 2:** `sample/350BT` is **349.4B DERIVED** and therefore
**≥ 252B — it CAN supply the draw.** But only at **72.1% of the pool**, which is the anti-join
problem, not a sizing problem. `sample/100BT` at **100.24B MEASURED** cannot supply 252B at all
(a **2.51×** shortfall).

### A3. Subset claim `sample-100BT ⊂ sample-350BT`

**Card evidence — CARD, verbatim, fetched from
`resolve/87f09149ef4734204d70ed1d046ddc9ca3f2b8f9/README.md` this session (26,308 bytes):**

> `sample-10BT` was sampled from `sample-100BT` which in turn was sampled from `sample-350BT`.

and, from the YAML front matter, the config→path binding that settles the spelling question:

> ```yaml
>   - config_name: sample-350BT
>     ...
>         path: sample/350BT/*
> ```

**MEASURED — file layout.** 472 files `sample/350BT/{000..016}_{NNNNN}.parquet`; 140 files
`sample/100BT/{000..013}_{NNNNN}.parquet`; 14 files `sample/10BT/{000..013}_00000.parquet`.

**MEASURED — the samples are NOT byte-shared.** I compared the **LFS oid** (content sha256) of every
file across the three configs: **0 / 140** `sample/100BT` oids appear in `sample/350BT`, and
**0 / 14** `sample/10BT` oids appear in either. So the nesting, if real, is a **row-level** subset
re-serialized into new files — there is no shortcut via file identity. (This also rules out the lazy
hypothesis that `sample/100BT` is literally the first 140 files of `sample/350BT`.)

**MEASURED — schema, from a real footer** (`sample/100BT/000_00000.parquet`, 726,000 rows,
726 row groups, 2,153,444,469 bytes, footer read over HTTP Range, 3.08 MB moved):

```
text string · id string · dump string · url string · file_path string · language string
language_score double · token_count int64 · score double · int_score int64
```

**10 columns, all top-level.** `path_in_schema` for the payload is **`text`** (flat — *no* nested
leaf, unlike FinePhrase). The join key is **`id`**, physical `BYTE_ARRAY`/UTF8,
`null_count: 0`, values shaped `<urn:uuid:003baaf4-69c7-4ee7-b37f-468bf9b55842>`.

🔵 **METHOD CORRECTION vs the brief.** The brief prescribed a **`url`**-projected join ("`url` is
~1.75% of bytes"). **MEASURED per-row-group compressed shares on a real footer:**
`text` 3,014,177 B (**97.1%**) · `url` 53,883 B (**1.74%** — the brief's figure, confirmed) ·
**`id` 38,389 B (1.24%)**. **`id` is 29% cheaper than `url` AND it is the exact key the anti-join
would use at read time.** I joined on `id`. The `url` route would have been a proxy for the same
thing at higher cost.

**MEASURED — files are DUMP-CLUSTERED, which invalidates a naive random-file join.** Reading `dump`
column statistics from every row group of 6 sampled files (footer only, ~3 MB each):

| config | file | row groups | distinct `dump` values in the file |
|---|---|---:|---:|
| 100BT | `002_00008.parquet` | 733 | **19** |
| 100BT | `000_00006.parquet` | 725 | **18** |
| 100BT | `007_00000.parquet` | 732 | **22** |
| 350BT | `004_00005.parquet` | 725 | **9** |
| 350BT | `003_00024.parquet` | 741 | **9** |
| 350BT | `002_00011.parquet` | 729 | **7** |

Each file covers only 7–22 of the **110** CC dumps, and the two configs' files partition the dumps
**differently** (350BT packs ~2× more rows per dump per file). **A random-file × random-file join
therefore compares mostly-disjoint crawl slices and returns 0 by construction, whatever the truth
is.** This is the same head/cluster-bias trap the prior wave hit at 10×.

**PILOT (recorded so the next reader does not repeat it) — seed 42, 4 files × 60 row groups per
config, 240,000 ids each side, 44.6 MB moved, 282 s: intersection = 0.**
**I grade this UNINFORMATIVE, not evidence against the subset claim.** DERIVED expectation even
if the subset claim is 100% true and the dumps had matched perfectly:
```
E[|A ∩ B|] ≈ |A| · |B| / N_350BT  =  240,000 × 240,000 / 344,600,000  =  167
```
…and once the dump mismatch is folded in, the expectation collapses toward 0. **A zero here was
predicted by the arithmetic before the run; it discriminates nothing.** Reporting it as
"subset claim refuted" would have been a false negative.

**The dump-controlled test.** The only cheap design with power is to compare **the same `dump`
value** on both sides, and score the observed intersection against the *conditional* expectation
`|A_d| · |B_d| / N_350BT,d`.

#### 🔴 THE DUMP-CONTROLLED RESULT — ZERO INTERSECTION AT AN EXPECTATION OF ~18,000

**MEASURED.** I read the **complete `id` + `dump` columns of two whole files** — every row of every
row group, not a sample:

- A = `sample/100BT/002_00008.parquet` — **733,000 rows, 733,000 distinct ids** (zero internal dupes)
- B = `sample/350BT/002_00011.parquet` — **729,000 rows, 729,000 distinct ids** (zero internal dupes)
- 86.3 MB moved over 2,924 concurrent ranged GETs. No dataset download.

The two files share **three** `dump` values. Per-dump, with the conditional expectation under the
null hypothesis *"`sample/100BT` is a uniform random subset of `sample/350BT`"*
(`E = |A_d| · |B_d| / N_350BT,d`, where `N_350BT,d` is that dump's share of the 344.6M-row 350BT
population, apportioned by the dump's exact byte share of the full `data/` tree):

| `dump` | \|A_d\| | \|B_d\| | **observed ∩** | N_350BT,d | **E[∩] if subset** |
|---|---:|---:|---:|---:|---:|
| `CC-MAIN-2014-35` | 56,257 | 196,309 | **0** | 2,546,800 | 4,336 |
| `CC-MAIN-2017-43` | 126,553 | 220,906 | **0** | 3,144,402 | 8,891 |
| `CC-MAIN-2020-29` | 124,628 | 131,045 | **0** | 3,427,406 | 4,765 |
| **total** | | | **0** | | **17,992** |

**Observed 0 against an expectation of ~18,000.** Under Poisson(17,992) the probability of observing
zero is `e^−17992` — that is not a marginal result, it is a categorical one. **This test HAS power,
unlike the pilot, and it comes back negative.**

#### ⚠️ WHAT THAT DOES AND DOES NOT MEAN — read this before citing it

**I am NOT concluding that the card is lying about the sampling lineage.** There is a second
hypothesis that fits the data exactly as well, and it has a specific, testable signature:

- **H1 — the samples are not row-subsets.** The card's lineage statement is about *provenance* and
  the configs were re-materialized from the parent corpus rather than carved out of it.
- **H2 — the `id` values are NOT STABLE ACROSS CONFIGS.** If FineWeb-Edu assigns
  `<urn:uuid:...>` ids at *serialization* time rather than carrying one identity per document from
  the source crawl, then the same document has a different `id` in `sample/100BT` than in
  `sample/350BT`, and an id-join returns 0 no matter how true the subset claim is.

🛑 **H2, IF TRUE, IS THE MORE IMPORTANT FINDING OF THE TWO — because the entire anti-join design
depends on `id` being a stable cross-config document identity.** `sha256(id) % 4` cannot separate a
FinePhrase draw from a FineWeb-Edu draw if the two configs disagree about what a document's id is.
The supporting evidence that H2 is live: **the LFS oids share nothing across configs** (§A3 above),
so every sample config was independently re-serialized — exactly the circumstance under which a
serialization-time id would be reassigned.

**The discriminating test, which is running:** join `sample/100BT`'s `CC-MAIN-2017-43` slice against
the **full `data/CC-MAIN-2017-43`** directory (18 files, the authoritative copy of that crawl),
on **BOTH `id` AND `url`**. `url` is a content-intrinsic key that cannot be reassigned.
- **`id` hits and `url` hits** ⇒ ids are stable; the samples really are disjoint re-draws (H1).
- **`url` hits but `id` misses** ⇒ **H2 confirmed — ids are per-config and the anti-join key is
  broken.** This would be a P0.
- **neither hits** ⇒ my sampling is at fault, and nothing above should be trusted.

#### 🔴 THE DISCRIMINATING TEST HAS LANDED — AND IT CAME OUT ON THE **THIRD** BRANCH

**MEASURED.** A = the `CC-MAIN-2017-43` slice of `sample/100BT/002_00008.parquet` (**126,553 rows,
126,553 distinct ids, 126,553 distinct urls** — complete column read, every row group).
C = **3 of the 18 files** of `data/CC-MAIN-2017-43` at the same pinned sha, read completely:
**2,490,381 rows, 2,490,381 distinct ids, 2,490,353 distinct urls.** 366 MB moved.

**MEASURED — the full dump's exact size**, from `datasets-server`
`config=CC-MAIN-2017-43`: **14,942,282 rows.** So C is `2,490,381 / 14,942,282` = **16.667%** of the
dump (and 3/18 files = 16.667% ✅ — the two agree exactly, which validates the sampling frame).

**DERIVED — the expectation under "the sample's 2017-43 docs live in `data/`'s 2017-43":**
```
E[hits] = |A| × (C_rows / dump_rows) = 126,553 × 0.166667 = 21,092
```

| join key | observed hits | expected if subset |
|---|---:|---:|
| **`id`** | **0** | 21,092 |
| **`url`** (content-intrinsic) | **3** | 21,092 |

🔴 **NEITHER KEY HITS. `url` — which CANNOT be reassigned by a re-serialization — misses just as
badly as `id` (3 out of 21,092 = 0.014%).**

**Per the decision rule I registered BEFORE running it, this is the third branch, and I am holding
myself to it: this does NOT confirm H2, and it does NOT confirm H1. The test failed its own
control.** Reporting it as "the anti-join key is broken" would be exactly the kind of overclaim this
file exists to prevent.

**What it does establish, and this is substantive:** the documents in `sample/100BT` labelled
`dump = CC-MAIN-2017-43` are **essentially absent from `data/CC-MAIN-2017-43` at revision
`87f09149ef4734204d70ed1d046ddc9ca3f2b8f9`** — not merely under a different id, but **not there as
URLs either.** The three configs are not row-subsets of the `data/` config *as published at this
revision*. Since `url` overlap is ~0, the most economical explanation is that **`data/` is a later
re-extraction/re-dedup of FineWeb-Edu than the frozen samples** (the samples date to the original
release; `data/` spans dumps through `CC-MAIN-2025-26`), not that ids are unstable.

**Grade: MEASURED (the counts). The interpretation is UNVERIFIED — I have ruled out neither H1 nor
H2, and I have added a third hypothesis (H3: `data/` ≠ the samples' parent snapshot).**

⚠️ **DO NOT propagate "FineWeb-Edu ids are unstable" out of this file. It is not established.**
Likewise do not propagate "the samples are not nested" — 100BT vs 350BT was never tested against a
working control.

**The test that actually settles task #21 — and it is a different, more direct join.** All of the
above is about *which FineWeb-Edu config contains which document*. **Task #21 does not need that.**
It needs one thing: **do FinePhrase's `id`s match FineWeb-Edu's `id`s for the same document?**
That is a direct two-repo join on `id` **and** `url` (FinePhrase carries FineWeb-Edu's full 11-column
schema, so both keys are present on both sides), and it is the P0. **It is running; see §A3b.**

#### A3b. FinePhrase `id` ↔ FineWeb-Edu `sample/350BT` — ZERO ON BOTH KEYS

> 🔵 **READ §A3c FIRST.** At the time I wrote this section I read the zeros below as *possibly*
> meaning the anti-join key was broken. **§A3c measured that it is not** — the same FinePhrase file
> hits `data/` 2,085 times. **The correct conclusion from this section is narrower and still
> important: `sample/350BT` is not FinePhrase's parent at this pin.** Left in full because it is
> the evidence for that, and because the reasoning discipline it records is worth keeping.

**This is the test that matters for gap 4, and unlike §A3 it has a control that passes.**

**MEASURED.** FinePhrase `faq/000_00000_0.parquet` @ **`78cf4a5ed0099214979c094c963e699c19163838`**
(269,976,082 B, **67,000 rows, complete `id`+`url`+`dump` column read** — every row group).

**✅ First: the keys ARE comparable. Verified by reading real values from both repos side by side —
this is the control §A3 lacked.**

| | FinePhrase `faq` | FineWeb-Edu `sample/350BT` |
|---|---|---|
| `id` | `<urn:uuid:ef8e1562-50a9-4d13-8ee0-06c887976497>` | `<urn:uuid:3cd4ab7f-3bd4-4365-876a-a765e384f7e4>` |
| `url` | `http://www.windows2universe.org/headline_universe/…` | `http://hubblesite.org/news_release/news/1999-45` |
| `dump` | `CC-MAIN-2013-20` | `CC-MAIN-2017-17` |
| `token_count` | 511 | 781 |

**Byte-identical formats: same `<urn:uuid:...>` shape, same bare-URL shape, same `dump` vocabulary,
same 11-column FineWeb-Edu schema (FinePhrase adds only `rollout_results`).** And FinePhrase carries
a `dataset` column whose value is literally **`HuggingFaceFW/fineweb-edu`** on every row. **So a
zero here cannot be blamed on incomparable keys, unlike §A3.**

**The joins**, dump-matched (FinePhrase's 4 dumps vs `sample/350BT` files whose footer `dump`
statistics cover the same dump — 20 files' footers scanned to find them):

| FineWeb-Edu file | dump | \|A_d\| (FinePhrase) | \|B_d\| (FW-Edu) | **∩ on `id`** | **∩ on `url`** |
|---|---|---:|---:|---:|---:|
| `sample/350BT/014_00018.parquet` | `CC-MAIN-2017-26` | 19,601 | 174,000 | **0** | **0** |
| `sample/350BT/014_00017.parquet` | `CC-MAIN-2017-26` | 19,601 | 149,000 | **0** | **0** |
| `sample/350BT/001_00018.parquet` | `CC-MAIN-2023-14` | 8,396 | 88,000 | **0** | **0** |

**DERIVED expectation** for the two `CC-MAIN-2017-26` files combined
(`N_350BT,CC-MAIN-2017-26` ≈ 339,347,842 × 0.00870 byte-share = 2,951,535):
```
E[∩] = 19,601 x 323,000 / 2,951,535 = 2,145
```
**Observed 0 on `id` AND 0 on `url`, against an expectation of ~2,145.**

🔴 **THIS IS THE FINDING. FinePhrase's documents are NOT FOUND in `sample/350BT` — not by id, and
not by url — even though FinePhrase's own card and its own `dataset` column say
`sample-350BT` is its parent.**

**Because `url` is content-intrinsic and the key formats are proven identical, the "the join is
broken" explanation is much weaker here than in §A3.** The live readings, in the order I'd bet on:

1. **H3 (now the leading hypothesis) — `sample/350BT` AS PUBLISHED AT
   `87f09149ef4734204d70ed1d046ddc9ca3f2b8f9` IS NOT THE SNAPSHOT FINEPHRASE WAS BUILT FROM.**
   FinePhrase's revision is dated **2026-03-31**; the FineWeb-Edu pin is **2025-07-11**. The same
   config name at a different revision can be a different row set. This also explains §A3's control
   failure with one mechanism instead of two.
2. **H2 — ids/urls are re-derived per serialization.** Weaker now: `url` should survive that.
3. **H1 — genuinely disjoint.** Contradicted by FinePhrase's own card and `dataset` column.

**⚠️ POWER AND SCOPE, STATED HONESTLY.** One FinePhrase file (67,000 rows, complete), three
FineWeb-Edu files, three dumps, E≈2,145 for the pair I computed. **That is enough power to reject
"most FinePhrase documents are in this `sample/350BT` snapshot". It is NOT enough to establish
WHICH of H1/H2/H3 is true**, and I did not test FinePhrase against `data/` (the FULL config), which
is the obvious next probe and would discriminate H3 from H2 in one run.

#### ⚠️ WHAT THIS MEANT BEFORE §A3c — SUPERSEDED, kept for the reasoning trail

> 🔵 **Everything in this sub-section was written before the decisive run and is REVERSED by §A3c.**
> Items 1 and 2 below (the arithmetic being unsupported, the fix being possibly harmful) are
> **withdrawn**. Items 3 and 4 (task #21 unaffected; the partition is not the anti-join) **stand.**

- **The §A4 collision arithmetic is now CONDITIONAL, and I am downgrading it in place.** Its
  premise — that the edu-web draw and the FinePhrase draw index a **common id universe** — is
  **not supported by any measurement I could get, and is actively contradicted by three
  dump-matched joins on two independent keys.** Treat the 72.1% / 25.97B figures as **DERIVED FROM
  AN UNVERIFIED PREMISE**, not as measurements.
- 🛑 **The proposed fix in §A5 — reserve `sha256(id) % 4` buckets on the edu-web side — CANNOT BE
  VALIDATED TODAY.** It assumes a FinePhrase id and its FineWeb-Edu twin are the same string. **On
  the bytes I read, they are never the same string.** If that holds, the mechanism silently does
  nothing: it would exclude a quarter of edu-web at random and leave the real collision untouched,
  while *appearing* to be an anti-join. **That is a worse outcome than shipping no fix**, because it
  looks like the problem is solved.
- ✅ **Task #21 itself is UNAFFECTED.** `keeps_id` compares FinePhrase ids **to each other**, within
  one repo, and the four configs demonstrably share ids (91.0–92.9% pairwise, MEASURED). Nothing
  here touches that. **#21 remains correct and remains already-implemented (§A5).**
- **The blocking question for gap 4 is now a different one than the brief posed.** Not *"does the
  partition separate them?"* (§A4: no, it is intra-FinePhrase and never could) but
  **"is there ANY key on which a FineWeb-Edu row and its FinePhrase rephrasing can be joined at
  all?"** Until that is answered, **no id-keyed anti-join should be written**, and the honest
  registry state is: `fineweb-edu` needs the config/pool/target changes in §A5 for **sizing**
  reasons, and the anti-join is **blocked on a measurement, not on code**.

**The one-run experiment that settles it** (~30 min, ~400 MB, same tooling): join the same
FinePhrase `faq` file against **`data/CC-MAIN-2013-20`** and **`data/CC-MAIN-2017-26`** (the FULL
config) on `id` and `url`.
- **hits ⇒ H3** — FinePhrase's parent is the FULL corpus (or a newer snapshot), the anti-join is
  implementable, and **the edu-web row should be `data`, not `sample/350BT`** — which also resolves
  the §A5 headroom problem (1.04×) in the same stroke.
- **no hits on either ⇒ H2** — ids are not stable across repos and **the anti-join must be
  content-keyed (normalized-url or a text shingle), not id-keyed.** That is a design change, and it
  must be made before tokenize.

#### A3c. ✅ **THE DECISIVE RUN LANDED — H3 CONFIRMED. IDS ARE STABLE. THE ANTI-JOIN IS IMPLEMENTABLE.**

🟢 **MEASURED, and it reverses the pessimistic reading of §A3b.**

| side | content |
|---|---|
| **A** | FinePhrase `faq/000_00000_0.parquet` @ `78cf4a5ed0099214979c094c963e699c19163838`, `dump == CC-MAIN-2013-20` slice: **19,700 ids / 19,700 urls** (complete column read) |
| **B** | **`data/CC-MAIN-2013-20/train-00000-of-00014.parquet`** @ `87f09149ef4734204d70ed1d046ddc9ca3f2b8f9` — **785,906 rows**, complete `id`+`url` read |

| `data/CC-MAIN-2013-20` file | rows | cumulative FW-Edu ids | **∩ on `id`** | **∩ on `url`** |
|---|---:|---:|---:|---:|
| `train-00000-of-00014.parquet` | 785,906 | 785,906 | 🟢 **2,085** | 🟢 **2,087** |
| `train-00001-of-00014.parquet` | 785,906 | 1,571,812 | 🟢 **4,170** | 🟢 **4,176** |

🟢 **THE SECOND FILE REPLICATED THE FIRST EXACTLY: 2,085 → 4,170 is 2.0000×, on twice the data.**
A perfectly linear accumulation is the signature of a genuine uniform overlap, not of a fluke or an
artifact. `url` tracks `id` at 4,176 vs 4,170 (**99.86% agreement**) across both files.
**DERIVED cumulative rate: 4,170 / 19,700 = 21.2% of this FinePhrase dump slice found in
10.5% of the dump's rows** — consistent with, and slightly richer than, uniform.

**DERIVED expectation** per file: `19,700 × (785,906 / 14,942,282) = 1,036`. **Observed 2,085 —
2.0× the uniform expectation, in the right direction** (FinePhrase rephrases higher-quality
documents, so it over-samples relative to uniform).

**⚠️ Extrapolation, clearly labelled DERIVED and NOT a census:** if the linear rate holds across all
14 files, this FinePhrase dump slice would find **~29,200 matches against 19,700 available ids** —
i.e. it saturates before the dump is exhausted, implying **essentially all** of these FinePhrase
documents live in `data/`. **I read 2 of 14 files; do not quote a total overlap percentage from
this.** The census tool for that is named below.

**Three things are now settled, from real bytes:**

1. 🟢 **FineWeb-Edu `id` IS A STABLE CROSS-REPO DOCUMENT IDENTITY.** A FinePhrase document and its
   FineWeb-Edu original **carry the same `<urn:uuid:...>` string.** The `id` and `url` joins agree
   to within 2 rows out of ~2,086 (99.90%) — mutual corroboration by an intrinsic key.
   **H2 IS REFUTED.**
2. 🟢 **H3 IS CONFIRMED: FinePhrase's parent is the FULL `data` config, NOT `sample/350BT`
   as published at this revision.** Same FinePhrase file: **0 hits against three `sample/350BT`
   files (§A3b), 2,085 hits against one `data/` file.** The card's `source_datasets:
   [.../sample-350BT]` does **not** correspond to the `sample/350BT` directory at pin
   `87f09149ef4734204d70ed1d046ddc9ca3f2b8f9`.
3. 🟢 **The §A4 arithmetic is REINSTATED as sound in mechanism**, and its **`data` FULL branch is
   the correct one**: pool **1,583.1B**, draw 252.0B = **15.9% consumed**, **~5.73B of the 36.0B
   FinePhrase draw collides**. The 72.1% / 25.97B figures were computed against `sample/350BT`,
   which is now **measured NOT to be the relevant parent** — so **do not use them.**

#### 🔴 CONSEQUENCES — these change §A5's recommendation

- ✅ **An id-keyed anti-join WILL WORK.** §A3b's warning that it might silently do nothing is
  **withdrawn** — ids join across the two repos, verified against a content-intrinsic control.
  **Gap 4 is fixable, and `sha256(id) % 4` bucket reservation is a valid mechanism.**
- 🛑 **THE `fineweb-edu` ROW MUST USE `data`, NOT `sample/350BT`.** This is now a **correctness**
  requirement, not a sizing preference. Three reasons, in order:
  1. **`sample/350BT` does not contain FinePhrase's parent documents** (0 / 3 files). Drawing
     edu-web from it and synthetic from FinePhrase gives two pools that **cannot be anti-joined at
     all** — not because ids are broken, but because the id sets do not meet.
  2. Only `data` reproduces the report's own **1,583.1B** pool figure (§A2).
  3. It resolves the §A5 headroom problem: reserving a quarter of **1,583.1B** leaves **1,187.4B**
     for a 252.0B draw = **4.7× headroom**, versus `sample/350BT`'s unusable **1.04×**.
- ⚠️ **Registry change #1 in §A5 is superseded: `config` should become `data`, not `sample/350BT`**,
  and `pool_tokens` **1,583,146,000,000**. I am correcting my own recommendation in place.
- ⚠️ **`data` is 2,410 files / 4.52 TB** vs `sample/350BT`'s 472 / 0.998 TB. A 252B draw reads a
  fraction of it either way (`_reader_for` stops on a character budget, `corpus_build.py:1283-1296`),
  but the **file list is 5× longer** and `hf_files(spec)` will page more.

**⚠️ Scope: one FinePhrase file × TWO `data/` files × one dump.** Decisive for *"do ids join"*
(a single hit refutes H2; there are 4,170, replicating linearly) but **not** a measurement of the
overall overlap rate.
**For that, run `scripts/measure_finephrase_overlap.py` with `--eduweb-configs data`** — it is
written, `selftest`-covered, and has never been run. Its README predicted this outcome
(*"should come out near 100%"*); my three `sample/350BT` zeros are why the **default
`--eduweb-configs sample/350BT` would have produced a badly misleading 0%.**
🛑 **Change that default, or at minimum run `data` too, before anyone trusts that script's output.**

<details>
<summary>Superseded: this section as written before the run landed</summary>

**I started it. It did not finish. Recording it as owed rather than leaving it implied.**

**Exact design, so it can be resumed verbatim:**
- **A** = FinePhrase `faq/000_00000_0.parquet` @ `78cf4a5ed0099214979c094c963e699c19163838`,
  the `dump == CC-MAIN-2013-20` slice: **19,700 ids / 19,700 urls** (complete column read).
  ✅ **This side completed and is confirmed.**
- **B** = `data/CC-MAIN-2013-20` @ `87f09149ef4734204d70ed1d046ddc9ca3f2b8f9` — **14 files,
  32,599,389,096 bytes** (MEASURED from the tree). I read the first 4 on `id` + `url`.
- Join on **both** keys; any nonzero on either is decisive.

**Why it is slow and how to run it faster:** the FULL config's files are **~2.3 GB each** with ~830k
rows, so an all-row-group `id`+`url` prefetch moves ~180 MB per file over ~1,700 scattered ranges.
Budget **~10 min per file** on a residential link. **In-region, or with `--nfiles 1`, this is
minutes.** A single file is enough: E[∩] for one file ≈ 19,700 × (830,127 / 14,942,282) ≈ **1,094**,
which has ample power against a zero.

**Working script:** `/tmp/a3c.py` (uses `/tmp/rf.py`'s `RangeFile` + prefetch; no dataset download).
The pattern is the same one `scripts/measure_finephrase_overlap.py` industrializes — **that script
already exists, is tested (`selftest`), and its `--eduweb-configs data` mode is exactly this
experiment at census scale.** It has **never been run.** Running it would settle §A3, §A3b and §A3c
together, and its README's own note anticipated this: *"`data` is the number that matters
operationally, and it should come out near 100%."* **My measurements say it will not — which makes
running it more valuable, not less.**

**Interpretation rule, fixed in advance (do not relitigate after seeing the number):**
| result | conclusion | consequence |
|---|---|---|
| **≥1 hit on `id`** | **H3** — the samples are the wrong snapshot; ids ARE stable | anti-join is implementable **on `id`**; edu-web row should be **`data`**, which also fixes §A5's 1.04× headroom |
| **`url` hits, `id` misses** | **H2** — ids are per-serialization | **anti-join MUST be content-keyed.** Design change, before tokenize |
| **both zero** | FinePhrase's parent is not this repo revision at all | escalate: the FinePhrase card's `source_datasets` cannot be taken at face value |

**Outcome: row 1 — `id` hit 2,085 times. H3.**
</details>

### A4. Consequence for the anti-join — THE ARITHMETIC

**Inputs, all carried from measurements (none are mine except where marked):**

| quantity | value | grade |
|---|---|---|
| FineWeb-Edu draw (report §3) | **252.0B** | plan |
| FinePhrase draw, one partition (report §3) | **36.0B** | plan |
| `sample/350BT` pool | **349.4B** dolma2 | **DERIVED, §A2** |
| `data` FULL pool | **1,583.1B** dolma2 | **DERIVED, §A2** — reproduces the report |
| FinePhrase parent | `sample-350BT`, **339,347,842 rows** | CARD (`source_datasets`, pinned sha) |
| FinePhrase 4-format pairwise overlap | **91.0–92.9%** | MEASURED (prior wave) |
| FinePhrase distinct rate | **0.2683** on 287,000 ids, complete column | MEASURED (task #5) |
| disjoint quarter | **25.0%** (measured 24.86–25.26%) | MEASURED |

**DERIVED — the collision depends entirely on which denominator the 252B is drawn from:**

```
IF the 252B comes from sample/350BT (the §10 reading):
    252.0 / 349.4 = 0.7213                      -> 72.1% of the rephrase parent consumed
    colliding synthetic tokens = 36.0 x 0.7213  = 25.97 B  of the 36.0 B draw

IF the 252B comes from data FULL (the report §3 reading):
    252.0 / 1583.1 = 0.1592                     -> 15.9% of FineWeb-Edu consumed
    colliding synthetic tokens = 36.0 x 0.1592  =  5.73 B  of the 36.0 B draw
```

**Gap 4 is real under either reading, and 4.5× worse under the plan's.** §10's "~72%" is
**confirmed to three significant figures (72.13%)** by my independent 349.4B pool. §10 is
arithmetically correct; what it omits is that the report is sized against a different pool.

#### 🔴 DOES `sha256(id) % 4` STILL SEPARATE THEM? **NO — AND IT NEVER DID. THIS IS A CATEGORY ERROR.**

**MEASURED-IN-CODE**, `src/edullm_data/reservoir_ids.py:60,65,102,120` and its one live caller
`src/edullm_data/corpus_build.py:1292`:

```python
FINEPHRASE_FORMATS = ("faq", "math", "table", "tutorial")     # reservoir_ids.py:65
def keeps_id(fmt: str, doc_id: str) -> bool: ...              # reservoir_ids.py:120
if fp_format is not None and not keeps_id(fp_format, doc.id): # corpus_build.py:1292
    continue
```

**What the partition does:** it decides **WHICH OF THE FOUR FINEPHRASE FORMATS keeps a document**,
so `faq`/`math`/`table`/`tutorial` do not ship the same document four times.
`reservoir_ids.py:60` states it: *"`FINEPHRASE_FORMATS[partition_of(id)]` is the format that keeps
that document."*

🛑 **It is INTRA-FinePhrase deduplication. It has no term for FineWeb-Edu.** The predicate's only
inputs are a FinePhrase format and an id. **No branch anywhere asks whether that id was also drawn
as real edu-web text**, and FineWeb-Edu rows never reach it (`fp_format is None` for them,
`corpus_build.py:1262,1292`).

**DERIVED:** the partition reduces FinePhrase from ~339.3M documents-wearing-four-hats to
**84.8M distinct documents** (339.3 × 0.25). Every one is a FineWeb-Edu document, and under the §10
denominator **72.1% of them (61.2M) have their unrephrased original in the 252B edu-web draw.**
**The partition and the anti-join are two different mechanisms; only the first is built.**

**And §4.3's "free fix" is no longer free.** *"Draw synthetic from the ~242M `sample-350BT` ids not
in `sample-100BT`"* was written when edu-web was assumed to be `sample/100BT`. **At a 252B draw from
`sample/350BT` the residual is only 27.9% of the parent ≈ 94.7M documents, and the FinePhrase draw
needs 84.8M of them — a ratio of 1.12×.** DERIVED: ~12% headroom, against measurement error of the
same order. **Barely feasible, and only if the two draws are coordinated.**

🟢 **RESOLVED BY §A3c — READ THIS BEFORE USING EITHER BRANCH ABOVE.** The caveat that stood here
(that the two draws might not share an id universe) is **settled: they do.** FinePhrase ids join
FineWeb-Edu ids exactly, corroborated by `url` (§A3c, 2,085 vs 2,087 hits).

🛑 **But the `sample/350BT` branch of the arithmetic above is now MEASURED TO BE THE WRONG ONE.**
FinePhrase's documents are **absent from `sample/350BT` at this pin** (0 hits / 3 files) and
**present in `data`** (2,085 hits / 1 file). **Use the `data` FULL branch: 15.9% consumed, ~5.73B of
the 36.0B FinePhrase draw collides.** The 72.1% / 25.97B figures — and §10's "~72%", which I
confirmed arithmetically — are computed against a pool that **is not FinePhrase's parent**.
The plan's §10 concern is therefore **real in kind but overstated by 4.5× in degree**, and its
proposed remedy (draw from `sample/350BT` ids not in `sample/100BT`) operates on the wrong pool.

### A5. Blast radius: task #21 and the registry rows

#### 🔵 CORRECTION TO THE BRIEF'S PREMISE: task #21 IS ALREADY IMPLEMENTED

`docs/TASKS.md:35` lists #21 as *"Wire the FinePhrase id partition into `_reader_for`… ~5 lines"*,
unshipped. **MEASURED-IN-CODE: it is already in the tree.** `corpus_build.py:1292` calls `keeps_id`;
`:1262` resolves `fp_format` before the first HTTP request; `:1281-1282` divides the read budget by
`N_PARTITIONS`; `:1233-1246` documents it. (`docs/TASKS.md:93` already records *"#4 … superseded by
#21"*.) The ~5-line change is **done**. What remains is its own docstring's warning at `:1243-1246`:

> ⚠️ UNVERIFIED against live HF from inside a Batch container — every offline test injects
> `documents=` instead, so this dispatch is exercised only by its own unit test.

**So the live risk on #21 is not "unwritten", it is "unexercised".** Its recommended settling job
(a single-bundle `run --of <n>` against `ubuntu-irc`, 1.87B) is unchanged and still owed.

🛑 **AND #21 WAS NEVER THE ANTI-JOIN.** Per §A4, `keeps_id` is intra-FinePhrase dedup.
**Shipping #21 does not close gap 4.** Reading `IMPLEMENTATION-PLAN.md` §10 alongside `TASKS.md:35`,
one could reasonably conclude that landing #21 fixes the FineWeb-Edu/FinePhrase collision. It does
not. Gap 4 needs a mechanism that exists nowhere in the codebase — `keeps_id`'s signature
`(fmt, doc_id)` **cannot express "was this id also drawn by another source."**

#### What has to change, concretely — the registry fields

🔵 **ROWS 1–2 UPDATED AFTER §A3c.** I originally wrote `sample/350BT` here. **§A3c measured that
FinePhrase's parent documents are NOT in `sample/350BT` at this pin, and ARE in `data`.** Corrected
in place — this is now a correctness requirement, not a sizing preference.

| # | field | from → to | note |
|---|---|---|---|
| 1 | `config` | `sample/100BT` → **`data`** | ~~`sample/350BT`~~ — **superseded by §A3c.** `data` is the only config containing FinePhrase's parents, and the only one reproducing the report's 1,583.1B |
| 2 | `pool_tokens` | `100240000000` → **`1583146000000`** | ~~`349352000000`~~ — must match the config, or `corpus.py:261-266`'s epoch guard is meaningless |
| 3 | `target_tokens` | reservoir's 20B → **`252000000000`** | |
| 4 | `revision` | **unchanged** — `87f09149ef4734204d70ed1d046ddc9ca3f2b8f9` | ✅ one commit serves all configs (MEASURED, §A1) |
| 5 | `text_column` / `id_column` | **unchanged** — `text` / `id` | ✅ MEASURED from a real footer, flat top-level (§A3) |
| 6 | **the anti-join** | **NEW CODE — does not exist** | see below |

**Answering the brief's question directly — config swap, second row, or read-time anti-join?**
**All three, and they are not alternatives:**

1. **A config swap is REQUIRED and NOT SUFFICIENT.** `sample/100BT` cannot supply 252B (2.51×
   short), so the row must move regardless of the anti-join. 🛑 **And per §A3c it must move to
   `data` specifically** — moving it to `sample/350BT` would satisfy the size requirement while
   making the anti-join *impossible*, because that config does not contain FinePhrase's parents.
   ⚠️ **This is a scope change, not a correction** — the existing row is right for the reservoir's
   20B draw. If one registry ever serves both corpora, this **must be a SECOND ROW with a different
   `key`**, not an edit, or the reservoir's provenance changes retroactively.
2. **A second row does NOT implement the anti-join.** `CorpusSpec` has no field expressing "exclude
   ids drawn by another row". Two rows give two independent draws from overlapping pools; nothing
   correlates them.
3. **So an id-level anti-join AT READ TIME is unavoidable**, with a hard constraint:
   🛑 **it must run inside `_reader_for`, in the same pass, for the same reason #21 does** — after
   tokenization *"a document is a byte range inside a shard and there is no document → id mapping
   left"* (`corpus_build.py:1239`). **It cannot be retrofitted after tokenize.**

**The cheap implementation (a PROPOSAL, not a measurement).** Make the FinePhrase side deterministic
rather than coordinated: `partition_of(id)` already assigns every FineWeb-Edu-lineage document to
one of 4 buckets **from its id alone**. Reserve bucket(s) for synthetic and exclude those same
buckets from the edu-web draw — one extra `keeps_id`-shaped predicate on the `fineweb-edu` row, no
cross-row state, no second pass, no join table.
🟢 **§A3c CONFIRMS THIS MECHANISM IS VALID** — ids join across the two repos (2,085 hits,
corroborated by `url`). The §A3b warning that it might silently do nothing is **withdrawn**.
🟢 **And on `data` the headroom is fine:** reserving a quarter of **1,583.1B** leaves **1,187.4B**
for a 252.0B draw = **4.7×**. (On `sample/350BT` it was an unusable **1.04×** — one more reason
row 1 must be `data`.)
⚠️ Still a **proposal**: I measured that the key works, not that this particular bucket scheme is
the best design.

⚠️ **FREEZE ORDERING.** Memory records `entry.labels` is inside `manifest_sha256` and therefore
unbackfillable; CLAUDE.md warns source-by-source ingest renames 98% of shards. **The
partition-reservation decision changes which documents enter which shard, so it must be made BEFORE
the first bundle is tokenized — not before publish. There is no later window.**

---

## PART B — M4: mean tokens/doc for unmeasured stage-2 sources

### B0. Method, seed, n

**Tokenizer:** `allenai/dolma2-tokenizer`, local HF cache snapshot
**`5292e5d6c0f40b67cc765fe41bec991cf4345b5c`**, `tokenizers` 0.22.2. Vocab **100,278**.
`add_special_tokens=False` — so a length is the *content* length; the EOS this pipeline appends is
**not** counted (correct: the EOS-fraction bound is `1 appended EOS / (content + 1)`, and at means
of hundreds the distinction is <1%).

**Seed 42 everywhere. Random offsets, never the head:**
- **parquet** — pick files with `random.sample`, then pick **random ROW GROUPS** inside each file
  and shuffle the rows drawn from them. These repos have 700+ row groups per file, so a random row
  group is a genuine random offset into the file. This is the fix for the 10× head-bias the prior
  wave hit.
- **`.jsonl.zst`** — zstd has **no random access** (see B1), so the offset randomization is at the
  **file** level: a random file out of 1,024 / 1,204 / 8,629, then its head. Graded and caveated
  per-source below.

**Reported per source:** `n`, mean, median, p05/p25/p75/p95, **CV**, a **95% CI on the mean**
(`mean ± 1.96·sd/√n` — the naive estimator, whose heavy-tailed weakness is the subject here, per
`artifacts/recount/README.md`), the **implied EOS fraction = 1/mean**, and the fraction of documents
below `corpus.MIN_DOC_TOKENS = 64` and below 20.

### B1. dolma3 midtrain QA mix (`allenai/dolma3_dolmino_mix-100B-1125`)

**Revision `f23aa129fda8335ba9760057bcc1f0c02f3d068b`** (ungated, `odc-by`) — carried from
W3-STAGE2's `artifacts/orchestration/data/measurements/stage2-sources.md`, not re-resolved.

**✅ The brief's worry — "`.jsonl.zst`, so it may be hard to sample; if you cannot, say so" — does
NOT block the measurement. `zstandard` 0.25.0 IS installed locally** (and `/opt/homebrew/bin/zstd`
exists). I read these files by streaming the HTTP response through `ZstdDecompressor.stream_reader`
and stopping after N lines, so a 5–8 MB file yields hundreds of documents without a full download.

**But two real limitations, stated rather than papered over:**

1. 🛑 **`Frame_Content_Size` is ABSENT from every file I checked** (`fcs_flag == 0` in the frame
   header; MEASURED on 4 files across all three QA directories). So W3-STAGE2's proposed settling
   job — *"parse `Frame_Content_Size` from the zstd frame headers of ~20 files (18 bytes each) for
   exact uncompressed sizes"* — **WILL NOT WORK ON THIS REPO.** That is a correction to a plan
   another worker wrote this same day, and it matters because it was the cheap route to the
   `pool_tokens` this row is missing. The remaining routes are (a) decompress fully, or (b) a
   measured compression ratio on a sample × the exact compressed bytes.
2. **Within a file, I read from the head.** Randomization is over the 1,024 / 1,204 / 8,629 files.

#### 🔴 THE HEADLINE: the QA directories' ACTUAL JSON schema is NOT what the card declares, and the three directories DISAGREE WITH EACH OTHER

**MEASURED** — decompressed and `json.loads`'d the first 50 lines of a **randomly chosen** file
(seed 42) from each of the three QA directories at the pinned sha:

| directory | file sampled | **actual JSON keys (all 50 lines agree)** |
|---|---|---|
| `data/ingredient1-nemotron-synth-qa` | `CC-MAIN-2018-13-part-00000.jsonl.zst` | **`text`, `language`, `url`, `warc_record_id`** |
| `data/ingredient1-reddit_to_flashcards` | `merged_qa_prefilter_densesubs_lowthresh-0010_f1.jsonl.zst` | **`id`, `text`** |
| `data/ingredient1-wiki_to_rcqa-part1` | `00016_f79.jsonl.zst` | **`id`, `text`** |

**Compare the card**, which W3-STAGE2 recorded (grade CARD) as declaring **nine** features:
`id`, `text`, `metadata`, `source`, `version`, `created`, `added`, `doc`, `attributes`.

**Consequences, and they are concrete:**

- ✅ **`text_column = "text"` is CONFIRMED FROM REAL BYTES** in all three directories. W3-STAGE2
  graded this CARD; it is now **MEASURED**. Good news.
- ✅ **The undocumented `doc` key that W3-STAGE2 flagged as a possible FinePhrase-trap-in-JSON
  (*"one `zstd -dc | head -1` settles it, and it MUST be done before the row is frozen"*) —
  I RAN THAT CHECK. **`doc` DOES NOT EXIST in any QA file.** Neither do `metadata`, `source`,
  `version`, `created`, `added`, or `attributes`. **That blocker is CLEARED for the QA rows.**
- 🛑 **`id_column = "id"` IS WRONG FOR `nemotron-synth-qa` — that directory has NO `id` KEY.**
  It carries `warc_record_id` (a bare 36-char UUID, e.g.
  `6e289392-1301-4dcd-bf35-3affbab5f97b`) and `url` instead. W3-STAGE2's ready-to-paste registry row
  sets `"id_column": "id"` across a comma-joined list containing this directory — **that row would
  fail, or worse, silently yield null ids, on 7.92 GB of the 19.46 GB QA pool (41%).**
  The fix is either `warc_record_id` for that directory (needing a **second row**, since
  `id_column` is one field per row) or a surrogate.
- 🛑 **The card's own feature list is unreliable for this repo.** Do not resolve any other column
  question here from the card.

**⚠️ Scope honesty:** one random file per directory, 50 lines each. Schema is a property of the
writer, so it is far more stable than a content statistic — but I have not proven all 1,024 files of
`nemotron-synth-qa` lack `id`. Grade: **MEASURED** on 3 files; **DERIVED (high confidence)** for the
directories.

#### 🔴🔴 M4 DOC-LENGTH RESULT — **THIS IS THE BLOCKER. `reddit_to_flashcards` IS BELOW THE FLOOR.**

**MEASURED.** 6 random files per directory (seed 42, out of 1,024 / 1,204 / 8,629), 120 documents
from each = **720 documents per directory**, dolma2, `add_special_tokens=False`.

| directory | n | **mean tok/doc** | median | CV | 95% CI | **EOS frac = 1/mean** | **vs 0.05 bound** | **<64 tok** | <20 tok |
|---|---:|---:|---:|---:|---|---:|---|---:|---:|
| `nemotron-synth-qa` | 720 | 496.7 | 570.5 | 0.414 | [481.7, 511.7] | 0.002013 | 24.8× clear | 0 | 0 |
| `wiki_to_rcqa-part1` | 720 | 188.8 | 147.0 | 0.718 | [178.9, 198.7] | 0.005296 | **9.4× clear** ⚠️ | **5.7%** | 0 |
| 🔴 **`reddit_to_flashcards`** | 720 | **54.4** | **53.0** | 0.212 | **[53.5, 55.2]** | **0.018386** | 🔴 **only 2.7× clear** | 🔴 **79.6%** | 0 |

🔴 **`reddit_to_flashcards` IS THE SOURCE THE M4 NODE WAS LOOKING FOR, AND IT FAILS.** The brief
predicted *"the dolma3 QA source is the one plausibly near the 20-token EOS floor."* **Confirmed —
and it is worse than "near":**

**DERIVED, the arithmetic that makes this a publish blocker:**
```
mean = 54.4 tokens/doc
EOS fraction of a shard packed purely from this source = 1 / 54.4 = 0.018386
families/pretrain.json  eos_fraction_max = 0.05
0.05 / 0.018386 = 2.72   ->  only 2.7x of margin
```
**It clears the bound on its own — but every other source in this corpus is 25–566× clear, and this
one is 2.7×.** It is the only measured source anywhere near the bound, by an order of magnitude.

🔴 **AND `corpus.MIN_DOC_TOKENS = 64` — THE VERY GUARD THAT IS SUPPOSED TO PREVENT THIS — DESTROYS
THE SOURCE INSTEAD.** MEASURED: **79.6% of `reddit_to_flashcards` documents are under 64 tokens**
(mean 54.4, median 53.0, CV only 0.212 — the distribution is *tightly* clustered just below the
floor, not a tail poking under it). So:

- The filter does **not** rescue the mean by trimming a tail; it **deletes four fifths of the
  directory.**
- `reddit_to_flashcards` is **7,923,725,257 of the 19,458,629,881 QA bytes = 40.7% of the entire
  QA pool** (byte figures MEASURED by W3-STAGE2). Losing 79.6% of it removes roughly a third of the
  pool the 14.0B draw is supposed to come from — **and W3-STAGE2 already flagged that draw as
  ~85% of the pool before any attrition.** Those two findings compound: **the 14.0B QA row may not
  be satisfiable at all.**
- **This attrition is invisible until after tokenize.** `MIN_DOC_TOKENS` drops documents silently;
  the shortfall shows up as a bundle that will not fill, at the end of the run.

**Why the CI is trustworthy here despite the heavy-tail warning:** CV is **0.212**, the *lowest* of
any source I measured. These are flashcards — a Q/A pair with a bounded template — so the
distribution is genuinely narrow. The 95% CI **[53.5, 55.2]** is ±1.6%. **There is no plausible
reading of this sample in which the mean is above 64.** This is not an estimate that needs more n.

**Recommended dispositions (owner's call, not mine):**
1. **Drop `reddit_to_flashcards` from the QA selection.** Cleanest. Costs 40.7% of QA bytes and
   makes the 14.0B target harder — see the compounding above.
2. **Concatenate flashcards into multi-card documents** before tokenize (the `ubuntu-irc` precedent:
   8,650 tok/doc *because turns are concatenated*, which is exactly this fix already applied
   elsewhere in this corpus). ~2 cards per doc clears `MIN_DOC_TOKENS`; ~10 clears it comfortably.
   ⚠️ This changes document identity, so it must be decided **before** the id/partition scheme is
   frozen.
3. **Keep it and accept 79.6% attrition**, with `pool_tokens` re-derived at 20.4% of the directory.
   Only defensible if someone re-checks that the 14.0B row still closes.

⚠️ **`wiki_to_rcqa-part1` also deserves a note:** 9.4× clear is fine, but **5.7% of its documents
are under 64 tokens** and its **CV is 0.718** — the widest of the three. It is not a blocker; it is
the second-shortest source in the corpus and should be watched.

⚠️ **Scope limits, stated plainly:** 6 files per directory out of up to 8,629; and **within each
file I read the first 120 lines**, because zstd has no random access. If line order inside a
`.jsonl.zst` correlates with length, these means are biased. **Mitigating evidence: the 6 per-file
means agree very closely** — `reddit_to_flashcards` 52.6/53.3/54.7/54.7/55.3/55.6,
`nemotron-synth-qa` 458.6–527.6 — so between-file variance is small and a within-file ordering
effect would have to be identical across 6 independently-chosen files to fool this. I did **not**
measure `wiki_to_rcqa-part2` (assumed to match part1) or ingredient2.

### B2. Cosmopedia — ✅ MEASURED, ALL 8 CONFIGS, NO EOS RISK

`HuggingFaceTB/cosmopedia` @ **`0ae6ec63f91742bd2d1eaef4f02232c55d719385`**, `text_column = "text"`
(top-level, flat — confirmed independently by W3-STAGE2's 8 footer reads).
**3 random files per config (all files where fewer exist) × random row groups × 200 random docs per
file, seed 42.** 336 files inventoried; ~55 MB moved total.

| config | n | **mean tok/doc** | median | p05 | p95 | min | max | CV | 95% CI on mean | **EOS frac = 1/mean** | <64 tok | <20 tok |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|
| `stanford` | 600 | **903.0** | 873.5 | 497 | 1,436 | 62 | 2,380 | 0.347 | [878.0, 928.1] | 0.001107 | 0.17% | 0 |
| `wikihow` | 400 | **866.0** | 850.0 | 604 | 1,220 | 142 | 1,758 | 0.250 | [844.8, 887.2] | 0.001155 | 0 | 0 |
| `openstax` | 400 | **763.7** | 666.0 | 395 | 1,567 | 124 | 2,079 | 0.462 | [729.1, 798.3] | 0.001309 | 0 | 0 |
| `web_samples_v1` | 600 | **709.4** | 679.0 | 342 | 1,144 | 133 | 1,769 | 0.317 | [691.4, 727.3] | 0.001410 | 0 | 0 |
| `web_samples_v2` | 600 | **693.2** | 662.0 | 473 | 1,052 | 136 | 2,077 | 0.303 | [676.4, 709.9] | 0.001443 | 0 | 0 |
| `khanacademy` | 200 | **632.9** | 641.5 | 152 | 1,097 | **6** | 1,450 | 0.435 | [594.8, 671.1] | 0.001580 | **3.0%** | **2.0%** |
| `auto_math_text` | 600 | **633.7** | 572.0 | 329 | 1,201 | 138 | 2,437 | 0.459 | [610.4, 656.9] | 0.001578 | 0 | 0 |
| `stories` | 600 | **515.8** | 510.0 | 325 | 727 | 127 | 1,043 | 0.254 | [505.3, 526.3] | 0.001939 | 0 | 0 |

**✅ VERDICT: Cosmopedia is nowhere near the EOS bound.** The worst config (`stories`) implies an
EOS fraction of **0.001939 — 25.8× below the 0.05 bound**. Nothing here is within 2× of it; the
closest is 25.8× clear. **Cosmopedia does not gate FREEZE.**

**Why the CIs are tight and trustworthy here:** CV is **0.25–0.46**, an order of magnitude below the
CV 9.0 that made the naive estimator useless on FineMath (`artifacts/recount/README.md`). Cosmopedia
is *generated* text with a bounded generation length, so it is genuinely light-tailed — max/median
is only ~2.8×. **This is the case where the naive per-document estimator is the right tool**, and
the ±3% CI half-widths reflect that honestly.

**Two observations worth carrying, neither a blocker:**
1. `khanacademy` has a **6-token document** and **2.0% of its documents under 20 tokens**. Its own
   mean is fine, but this is the only config with a short-document tail. `corpus.MIN_DOC_TOKENS = 64`
   drops **3.0%** of `khanacademy` — expect that attrition rather than treating it as a bug.
   `khanacademy` is the smallest config (1 file, 49 MB), so the corpus-level effect is negligible.
2. `stanford`'s min of 62 also sits just under the 64 floor (0.17% of docs).

### B3. Nemotron Math-Textbooks — ✅ MEASURED, 4,000× clear of the bound

`nvidia/Nemotron-Pretraining-Specialized-v1` @ **`9ed3718b5f2ae29074c5e34e64115432b7c4320f`**,
config **`Nemotron-Pretraining-Math-Textbooks`** (ungated, `cc-by-4.0`).

**Schema, MEASURED from a real parquet footer** (`part_000000.parquet`, 2,391,061,448 B, 1,000,000
rows, **4 row groups**), leaf list with `total_uncompressed_size`:

| `path_in_schema` | uncompressed bytes | arrow type |
|---|---:|---|
| **`text`** | 2,041,026,252 | `large_string` |
| `uuid` | 10,490,717 | `large_string` |
| `metadata.category` | 10,228,446 | `large_string` (struct child) |
| `metadata.models_used` | 8,654,695 | `large_string` (struct child) |
| `license` | 83 | `large_string` |

- ✅ **`text_column = "text"`** — top-level, exactly one leaf named `text`, no nesting. MEASURED.
- 🛑 **`id_column` is `uuid`, NOT `id`.** There is no `id` column. A row copying the
  Nemotron-CC-Math pattern (`"id_column": "id"`) would be wrong on this source.
- ⚠️ **`large_string`, not `string`** — arrow's 64-bit-offset variant. Any reader that
  `assert`s `pa.string()` fails here. Worth one line in the reader.

**Exact size, MEASURED from `datasets-server/size`:** **12,899,767 rows**, 30,841,263,255 parquet
bytes (13 files), `num_bytes_memory` 101,849,528,475.

**Sampling method, and why it differs from Cosmopedia.** 🛑 **These files have only 4 row groups of
~250,000 rows each (~600 MB per group).** A "random row group" is therefore *not* a random offset —
it is a quarter of a 2.4 GB file, and materializing one would mean downloading it. The Cosmopedia
method does not transfer. I used **`datasets-server/rows` at 40 random offsets × 5 rows, seed 42**,
over the full 12,899,767-row range. Two HTTP 429s were hit and absorbed by exponential backoff
(20 s × attempt); all 40 offsets ultimately returned. Rate-limited to ~1.2 s between calls, per the
brief's warning that a prior wave exhausted this quota.

| statistic | value |
|---|---|
| **n** | 200 documents at 40 distinct random offsets |
| **mean tok/doc** | **1,999.2** |
| median | 1,937.0 |
| p05 / p95 | 1,318 / 2,846 |
| min / max | 741 / 3,861 |
| **CV** | **0.237** |
| 95% CI on mean | **[1,933.5, 2,065.0]** (±3.3%) |
| **EOS fraction = 1/mean** | **0.000500** |
| docs < 64 tok / < 20 tok | **0 / 0** |

**✅ VERDICT: 0.000500 is 100× below the 0.05 bound.** Not a risk. CV 0.237 is very light-tailed —
generated textbook chapters have a bounded length — so the ±3.3% CI is real, not an artifact.

### B4. Reasoning traces — ✅ MEASURED. The LONGEST source in the corpus, and the risk is the opposite one

**Identification.** `FINAL-DATASET-REPORT.md` §4 names the row *"reasoning traces / worked
examples"* with a **~50B** pool and no repo. W3-STAGE2 left row 4 PENDING. The only candidate in the
already-pinned source set is **`nvidia/Nemotron-Pretraining-Specialized-v1`, config
`Nemotron-Pretraining-InfiniByte-Reasoning`** @ `9ed3718b5f2ae29074c5e34e64115432b7c4320f`.
⚠️ **Grade: DERIVED, not confirmed.** I measured the best-supported candidate; **the report does not
name a repo for this row, so the binding is my inference and an owner should confirm it.** If the
row is meant to be dolma3's `general_reasoning_mix` / `*-meta-reasoning` directories instead, the
numbers below do not apply.

**Exact size, MEASURED:** **1,478,301 rows**, 28,345,959,943 parquet bytes (30 files).
Same 4-column schema as Math-Textbooks (`text`, `license`, `metadata`, `uuid`).

Same `/rows` method, 40 random offsets × 5 rows, seed 42, two 429s absorbed:

| statistic | value |
|---|---|
| **n** | 200 documents at 40 distinct random offsets |
| **mean tok/doc** | **11,310.5** |
| median | 10,210.0 |
| **CV** | **0.606** |
| 95% CI on mean | **[10,360.0, 12,261.0]** (±8.4%) |
| **EOS fraction = 1/mean** | **0.0000884** |
| docs < 64 tok / < 20 tok | **0 / 0** |

**✅ VERDICT on the EOS bound: 0.0000884 is 566× below 0.05.** Zero EOS risk.

🛑 **BUT THIS SOURCE CARRIES THE OPPOSITE RISK, AND M4 WAS NOT LOOKING FOR IT.** At a **mean of
11,310 tokens and a median of 10,210**, reasoning traces are **13.9× longer than the whole
reservoir's 814.9** and **~5.6× longer than Math-Textbooks**. Two consequences nobody has recorded:

1. **DERIVED — a single document can exceed the training sequence length by an order of magnitude.**
   Whatever the packer's sequence length (4,096 or 8,192), the *median* document does not fit.
   How the packer splits a 10k-token document across sequences is a correctness question for the
   MoE's domain-purity lever (memory: *"domain-pure micro-batches suppress specialization"*) —
   a source whose documents span many sequences interacts with that directly.
2. **DERIVED — the 8.0B stage-2 draw is only ~707,000 documents** (8.0e9 / 11,310). Against
   1,478,301 rows total that is **47.8% of the pool by document count**, not the 0.16 epochs the
   report's table implies. ⚠️ **Flagged, not resolved** — it depends on the row binding above being
   the right repo.

**Honest note on the CI:** CV 0.606 here is 2.5× Math-Textbooks', so the ±8.4% half-width is wider,
and n=200 is modest for a source this heavy-tailed. The mean is nonetheless **two orders of
magnitude** from the bound, so no plausible CI widening changes the verdict.

### B5. Nemotron-CC-Math (`3`, `4plus`) — GATE-BLOCKED; measured on NVIDIA's own ungated mirror

🛑 **THE GATE IS NOT ACCEPTED ON THIS MACHINE'S TOKEN — I re-confirmed it, it is not stale.**
`GET resolve/397a2502f2028c659ba411a6c4935b464a7f03aa/3/part_000000.parquet` with `Range: bytes=0-64`
returns **HTTP 403, `X-Error-Code: GatedRepo`**, *"you are not in the authorized list."*
This reproduces W3-STAGE2's finding independently. **Gate access is per-account, and this account
does not have it** — so configs `3` and `4plus` **cannot be measured from here at all.**

**What I measured instead, and its exact standing.** NVIDIA publishes
`nvidia/Nemotron-Pretraining-Dataset-sample` @ **`3ad096e6394e487bb4f778733300da85275bb449`**
(**ungated**), whose config **`Nemotron-CC-MATH`** is NVIDIA's own published sample of this corpus.
**MEASURED:** 1 file, `Nemotron-CC-MATH/part_0000.parquet`, 1,746,193 bytes, **954 rows**, 1 row
group. 200 documents drawn at random from it (seed 42), dolma2:

| statistic | value |
|---|---|
| n | 200 (of the 954 available) |
| **mean tok/doc** | **1,081.5** |
| median | **620.5** |
| p05 / p25 / p75 / p95 | 294 / 435 / 940 / 3,704 |
| min / max | 174 / **17,585** |
| **CV** | **1.622** ← by far the heaviest tail measured |
| 95% CI on mean | **[838.4, 1,324.6]** (±22.5%) |
| **EOS fraction = 1/mean** | **0.000925** |
| docs < 64 tok / < 20 tok | **0 / 0** |

**✅ VERDICT: no EOS risk. 0.000925 is 54× below the bound**, and even the CI's pessimistic end
(mean 838 → EOS 0.001193) is 42× clear.

⚠️ **GRADE: MEASURED for the sample repo; DERIVED (moderate confidence) for configs `3`/`4plus`.**
Lower confidence than W3-STAGE2's schema inference from the same mirror, and deliberately so:
a **schema** is a property of the writer and transfers well; a **length distribution** is a property
of the data and a 954-row sample may not represent an 83.6B-token config. **CV 1.622 with n=200
gives a ±22.5% CI** — this is precisely the heavy-tailed regime `artifacts/recount/README.md` warns
about, and I am reporting it rather than hiding it. **The verdict survives anyway only because the
margin is 54×, not because the estimate is tight.**

**Cross-check that raises confidence in the mean, DERIVED:** the teammate's gated measurement gives
**134.0B tokens** over `3`+`4plus`, and `3` alone is 1,899,869,110 × ~13 files. At ~3.53 bytes/token
(DERIVED from that same measurement, recorded in W3-STAGE2's trap 8), a 1,081.5-token mean implies
~3,818 bytes/doc — an unremarkable web-page size. Nothing contradicts the sample.

**Settling job for whoever holds gate access (~1 minute):** 200 random-row-group documents from
`3/part_000000.parquet` and `4plus/part_000000.parquet` at
`397a2502f2028c659ba411a6c4935b464a7f03aa`. **Combine it with the schema settling job W3-STAGE2
already specified — same file, same read.**

**`4plus_MIND`: EXCLUDED, not measured, per the brief.** It is a rewrite of `4plus` and including
both double-counts. 🛑 Recorded again because it is the highest-probability corruption path:
**a `startswith("4plus")` filter matches `4plus_MIND`. Match config names with `==`.**

### B6. Summary table + EOS-fraction flags

**The bound: `families/pretrain.json` `eos_fraction_max = 0.05`, which IS a 20-token mean-doc floor.
`corpus.MIN_DOC_TOKENS = 64` is the guard that keeps a source clear of it.**

Sorted by risk. Sources marked ✅ were already measured and are shown for calibration only.

| source | mean tok/doc | **EOS frac** | **margin vs 0.05** | <64 tok | grade |
|---|---:|---:|---|---:|---|
| 🔴 **dolma3 `reddit_to_flashcards`** | **54.4** | **0.018386** | 🔴 **2.7×** | 🔴 **79.6%** | **MEASURED** |
| ⚠️ dolma3 `wiki_to_rcqa-part1` | 188.8 | 0.005296 | ⚠️ 9.4× | 5.7% | MEASURED |
| dolma3 `nemotron-synth-qa` | 496.7 | 0.002013 | 24.8× | 0 | MEASURED |
| Cosmopedia `stories` | 515.8 | 0.001939 | 25.8× | 0 | MEASURED |
| Cosmopedia `khanacademy` | 632.9 | 0.001580 | 31.6× | 3.0% | MEASURED |
| Cosmopedia `auto_math_text` | 633.7 | 0.001578 | 31.7× | 0 | MEASURED |
| Cosmopedia `web_samples_v1` | 709.4 | 0.001410 | 35.5× | 0 | MEASURED |
| Cosmopedia `web_samples_v2` | 693.2 | 0.001443 | 34.6× | 0 | MEASURED |
| Cosmopedia `openstax` | 763.7 | 0.001309 | 38.2× | 0 | MEASURED |
| ✅ whole reservoir (calibration) | 814.9 | 0.001227 | 40.8× | — | prior |
| Cosmopedia `wikihow` | 866.0 | 0.001155 | 43.3× | 0 | MEASURED |
| Cosmopedia `stanford` | 903.0 | 0.001107 | 45.2× | 0.17% | MEASURED |
| Nemotron-CC-Math (mirror proxy) | 1,081.5 | 0.000925 | 54.1× | 0 | **DERIVED** |
| ✅ finewiki (calibration) | 1,320.1 | — | — | — | prior |
| **Nemotron Math-Textbooks** | **1,999.2** | **0.000500** | 100.0× | 0 | **MEASURED** |
| ✅ peS2o / pubmed (calibration) | 6,474 / 7,918 | — | — | — | prior |
| **reasoning traces (InfiniByte)** | **11,310.5** | **0.0000884** | 566× | 0 | **MEASURED** |

#### The M4 verdict, in one line

**Four of the five unmeasured stage-2 sources are clear by 25–566×. ONE IS NOT: dolma3's
`reddit_to_flashcards` at 2.7×, with 79.6% of its documents below `MIN_DOC_TOKENS`. M4 should be
marked RESOLVED-WITH-A-BLOCKER, not RESOLVED.**

**Nothing else is within 2× of the bound.** The only other flag is `wiki_to_rcqa-part1` at 9.4× —
comfortable, but it is the second-shortest source in the corpus and shares a directory tree with the
failing one, so re-check it if the QA selection changes.

⚠️ **The 0.05 bound is per-SHARD, not per-source.** A shard mixing `reddit_to_flashcards` with
anything longer dilutes its EOS fraction and passes. **So whether this blocks depends on the packer's
shard composition, which I did not inspect** — memory records the MoE lever wants *domain-pure*
micro-batches, and a domain-pure `reddit_to_flashcards` shard is exactly the 2.7× case.
**Those two requirements are in tension and someone should reconcile them.**

---

## Corrections made in place

Every one of these is a correction to something written down, by me or by someone else. Listed so
nobody re-derives the superseded version.

| # | claim as written | correction | where |
|---|---|---|---|
| 1 | The brief: *"join on `url` — `url` is ~1.75% of bytes"* | `url` **is** 1.74% (confirmed) but **`id` is 1.24%** — 29% cheaper AND it is the key the anti-join actually uses. Joined on `id`, carried `url` as the control. | §A3 |
| 2 | My own pilot: 4 files × 60 row groups, intersection 0 | **UNINFORMATIVE, not a refutation.** E[∩] was 167 before dump-clustering, ~0 after. I predicted the zero from arithmetic before running it and refused to read it as evidence. Recorded so the next agent does not repeat it. | §A3 |
| 3 | Implicit assumption that a random-file join tests the subset claim | **FALSE — files are DUMP-CLUSTERED** (7–22 of 110 dumps per file, MEASURED on 6 footers), and the two configs partition dumps differently. Any such join returns 0 by construction. | §A3 |
| 4 | W3-STAGE2: dolma3 `text_column`/`id_column` = `text`/`id`, grade CARD | **`text` CONFIRMED (upgraded CARD → MEASURED). `id` is WRONG for `nemotron-synth-qa` — that directory has NO `id` key** (it has `warc_record_id` + `url`), and it is 41% of the QA pool. | §B1 |
| 5 | W3-STAGE2: *"`doc` is undocumented… one `zstd -dc \| head -1` settles it, and it MUST be done"* | **RAN IT. `doc` does not exist** in any QA file — nor do `metadata`, `source`, `version`, `created`, `added`, `attributes`. The card's 9-feature list is wrong for these directories. **Blocker cleared.** | §B1 |
| 6 | W3-STAGE2: *"parse `Frame_Content_Size` from the zstd frame headers… cheap, and it collapses the range"* | **WILL NOT WORK. `fcs_flag == 0` on every file checked** — no `Frame_Content_Size` is stored. The cheap route to the missing `pool_tokens` does not exist. | §B1 |
| 7 | `docs/TASKS.md:35` — task #21 unshipped, *"~5 lines"* | **ALREADY IMPLEMENTED** (`corpus_build.py:1262,1281-1282,1292`). The remaining risk is that it is **unexercised against live HF**, per its own docstring at `:1243-1246`. | §A5 |
| 8 | Implicit in §10 + `TASKS.md:35` — that shipping #21 addresses the FineWeb-Edu collision | **NO. `keeps_id` is intra-FinePhrase dedup**; its signature `(fmt, doc_id)` cannot express "drawn by another source". #21 does not close gap 4. | §A4 |
| 9 | My own §A4 collision arithmetic (72.1%, 25.97B) | Downgraded after §A3b, then **REINSTATED IN MECHANISM by §A3c — but the `sample/350BT` BRANCH IS THE WRONG ONE.** Use the `data` branch: **15.9% consumed, ~5.73B collides.** §10's "~72%" is real in kind, **overstated 4.5× in degree**, and computed against a pool measured NOT to be FinePhrase's parent. | §A3c |
| 10 | My own §A5 proposal to reserve `sha256(id) % 4` buckets | Called "possibly harmful" after §A3b; **§A3c WITHDRAWS that warning — ids DO join (2,085 hits, `url`-corroborated). The mechanism is valid.** | §A3c |
| 11 | My own §A5 registry rows 1–2 (`config` → `sample/350BT`, `pool_tokens` → 349.4B) | 🛑 **SUPERSEDED IN PLACE → `data` / 1,583,146,000,000.** `sample/350BT` would satisfy the size requirement while making the anti-join **impossible** — FinePhrase's parents are not in it (0 hits / 3 files vs 2,085 hits / 1 `data` file). | §A3c |
| 12 | My own §A3b reading that H3 was "leading" but H1/H2 undecided | **H2 REFUTED, H3 CONFIRMED.** FineWeb-Edu `id` **is** a stable cross-repo identity; the `sample/*` configs are simply not FinePhrase's parent snapshot. | §A3c |
| 13 | `scripts/measure_finephrase_overlap.py:145` `EDUWEB_DEFAULT = "sample/350BT"` | 🛑 **THE DEFAULT IS WRONG AND WOULD REPORT ~0% COLLISION.** MEASURED: 0 hits across 3 `sample/350BT` files. The script must be run with `--eduweb-configs data`. Its own README half-anticipated this (*"`data` is the number that matters operationally"*) but shipped the sample as default. | §A3c |
| 11 | Brief: *"the dolma3 QA source is the one plausibly near the 20-token EOS floor"* | **CONFIRMED and it is worse than "near"** — `reddit_to_flashcards` at 54.4 tok/doc, 2.7× margin, **79.6% below `MIN_DOC_TOKENS`.** | §B1 |

## Open / unresolved

**Ranked by consequence.**

1. ✅ **CLOSED by §A3c — "is there ANY key joining FineWeb-Edu to FinePhrase?" YES: `id`, verified
   against a `url` control (2,085 vs 2,087 hits).** The anti-join is implementable. **What replaces
   it as the top item: the `fineweb-edu` row MUST be `config: data`** — `sample/350BT` contains none
   of FinePhrase's parents (0 / 3 files) and would make the anti-join impossible while looking
   correct. **Registry edit, blocked only on item 4's owner call.**
1b. **Still owed (no longer P0): the overlap RATE at census scale.** Run
   `scripts/measure_finephrase_overlap.py --eduweb-configs data` — written, `selftest`-covered,
   **never run**, and 🛑 **its default `sample/350BT` (line 145) would report ~0% and be believed.**
2. 🔴 **P0 — dolma3 `reddit_to_flashcards`: drop / concatenate / accept 79.6% attrition?** Owner
   decision. Compounds with W3-STAGE2's finding that the 14.0B QA draw is already ~85% of the pool.
   **The 14.0B QA row may not be satisfiable.**
3. 🔴 **The `nemotron-synth-qa` `id_column` fix** — needs a second registry row (`warc_record_id`)
   or a surrogate. W3-STAGE2's pasted row is wrong on 41% of the QA pool as written.
4. ✅ **ANSWERED by §A2 + §A3c — the denominator is `data` (1,583.1B), and it is no longer a
   judgement call.** §A2 showed the report's pool figure reproduces `data` exactly; §A3c showed
   `data` is the only config containing FinePhrase's parents. **The collision is 15.9% / ~5.73B,
   not 72.1% / 25.97B.** What remains is only for the owner to ratify the registry edit.
   ⚠️ **`IMPLEMENTATION-PLAN.md` §10 and §4.3 are now known to reason from the wrong pool** and
   should be corrected by whoever owns that doc — I am read-only.
5. **`sample-100BT ⊂ sample-350BT` remains UNVERIFIED at the id level**, and is now **much less
   important**: neither sample is FinePhrase's parent, so the nesting question no longer bears on
   the anti-join. Recorded only so nobody re-opens it thinking it is load-bearing.
   **Do not propagate "the samples are not nested" from my zeros** — that was never tested against a
   working control.
   ⚠️ **What IS newly interesting:** `sample/350BT`'s relationship to `data` is now itself
   unclear — a FinePhrase file hit `data` 2,085 times and `sample/350BT` 0 times, so the sample is
   not a straightforward subset of `data` at this pin either. Nothing in the plan depends on it.
6. **Nemotron-CC-Math `3`/`4plus` doc lengths are DERIVED from a 954-row ungated mirror** (CV 1.622,
   ±22.5% CI). Verdict is safe at 54× margin but the estimate is loose. **Settling job needs gate
   access — this account does not have it (HTTP 403 re-confirmed).**
7. **The reasoning-traces row binding** — `InfiniByte-Reasoning` was adopted by the CEO after I
   measured it, so B4 stands. ⚠️ **Note it must NOT be labelled `nemotron-*`** (per the same ruling
   that names Math-Textbooks `math-textbooks`).
8. **Shard composition vs the 0.05 bound.** The bound is **per-shard**, not per-source, so a mixed
   shard dilutes `reddit_to_flashcards` and passes — but the MoE lever wants **domain-pure**
   micro-batches, which is exactly the 2.7× case. **These two requirements are in tension and
   nobody has reconciled them.** I did not inspect the packer.
9. **Not measured:** `wiki_to_rcqa-part2` (assumed to match part1), dolma3 `ingredient2` anything,
   and FinePhrase's `math`/`table`/`tutorial` configs (only `faq` was read).
