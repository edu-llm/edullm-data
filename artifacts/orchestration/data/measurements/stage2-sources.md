# Stage-2 source identity strings — W3-STAGE2

**Status: COMPLETE — all 7 rows resolved + 1 EXCLUDED row recorded. 2026-08-08.**
**Owner:** W3-STAGE2 (worker for DATA-EXEC), started 2026-08-08.
**Scope:** the six stage-2 sources in `docs/FINAL-DATASET-REPORT.md` §4 that have NO row in
`artifacts/reservoir/corpus-registry.json`, plus pre-1929 books (part of the §3 1% reference row).

**Schema = the field names `load_registry` feeds straight into `CorpusSpec(**row)`**
(`src/edullm_data/corpus_build.py:130` → `src/edullm_data/corpus.py:197-243`).

Grades: `MEASURED` / `MEASURED-IN-CODE` (file+line) / `DERIVED` (arithmetic shown) / `CARD` / `UNVERIFIED`.

---

## Row table (status board — details in the per-source sections below)

| # | source | key | repo | config | revision (40-char) | text_column | id_column | license | pool_tokens | status |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Nemotron-CC-Math `3` | `nemotron-cc-math-3` | `nvidia/Nemotron-CC-Math-v1` | `3` | `397a2502f2028c659ba411a6c4935b464a7f03aa` | `text` | `id` | NVIDIA Data Agreement for Model Training (v. Aug 15 2025) 🛑 | 83.6B MEASURED | ✅ RESOLVED, license blocked |
| 2 | Nemotron-CC-Math `4plus` | `nemotron-cc-math-4plus` | `nvidia/Nemotron-CC-Math-v1` | `4plus` | `397a2502f2028c659ba411a6c4935b464a7f03aa` | `text` | `id` | same 🛑 | 50.4B MEASURED | ✅ RESOLVED, license blocked |
| X | Nemotron-CC-Math `4plus_MIND` | — **EXCLUDED** — | `nvidia/Nemotron-CC-Math-v1` | `4plus_MIND` | (same) | — | — | — | — | ✅ recorded EXCLUDED |
| 3 | dolma3 midtraining mix (QA) | `dolma3-midtrain-qa` | `allenai/dolma3_dolmino_mix-100B-1125` | 4 dirs, ingredient1 (list) | `f23aa129fda8335ba9760057bcc1f0c02f3d068b` | `text` (JSON key) | `id` | `ODC-BY-1.0` | null (19.46 GB compressed MEASURED) | ✅ RESOLVED, 2 flags |
| 4 | reasoning traces / worked examples | `reasoning-traces` | 🛑 **NONE NAMED** — best candidate `nvidia/Nemotron-Pretraining-Specialized-v1` | `Nemotron-Pretraining-InfiniByte-Reasoning` | `9ed3718b5f2ae29074c5e34e64115432b7c4320f` | `text` | `uuid` | `CC-BY-4.0` | null (~15–19B DERIVED) | 🛑 **FREEZE BLOCKER** — no repo chosen |
| 5 | Cosmopedia | `cosmopedia` | `HuggingFaceTB/cosmopedia` | 5–6 of 8 configs (decision) | `0ae6ec63f91742bd2d1eaef4f02232c55d719385` | `text` | **NONE** — surrogate proposed | `Apache-2.0` | null (CARD 21.7B is Mistral-7B) | ✅ RESOLVED, 1 flag |
| 6 | Nemotron Math-Textbooks | `nemotron-math-textbooks` | `nvidia/Nemotron-Pretraining-Specialized-v1` | `Nemotron-Pretraining-Math-Textbooks` | `9ed3718b5f2ae29074c5e34e64115432b7c4320f` | `text` | `uuid` | `CC-BY-4.0` ✅ clean | null (~23–28B DERIVED) | ✅ RESOLVED |
| 7 | pre-1929 books | `pre-1929-books` | `common-pile/pre_1929_books_filtered` | (none) | `23f9d96dbb1db3324bbc9fbfe1f8299cc799c4d1` | `text` | `id` | `Public Domain` | null (~10.7B DERIVED) | ✅ RESOLVED — repo now named |

---

## 🛑 READ THIS FIRST — what blocks a freeze, and what is merely a decision

**Two hard blockers.** Neither is a missing measurement; both need a human.

| # | blocker | scope | where |
|---|---|---|---|
| **B1** | **`nvidia/Nemotron-CC-Math-v1`'s licence forbids *"otherwise mak[ing] available to others"* (§2.2.2) — the SAME clause that blocked the shared reservoir, verbatim, in this repo's own `LICENSE.md`.** Also §3.3: on termination we must **delete all copies within 14 days**, which collides head-on with "frozen means frozen." | **61.0B tokens = 6.1% of the corpus** (45.0B stage 1 + 16.0B stage 2) | license section below |
| **B2** | **The 8.0B "reasoning traces" row names NO REPO — in any document.** Not the report, not the plan, not the graph, not TASKS, not the registry. Two candidates measured and ready; the choice is unmade. | **8.0B = 0.8%** | row 4 |

**A third, softer one:** the gate on `Nemotron-CC-Math-v1` is **not accepted for this machine's
token** (HTTP 403 `GatedRepo` on payload bytes, though the tree and README are public). **Gate access
is per-account** — whoever runs the ingest needs it, not just whoever measured 134.0B.

**Seven decisions that are NOT blockers but WILL silently pick themselves if ignored:**

| # | decision | default if ignored | row |
|---|---|---|---|
| D1 | How to split 61.0B across Nemotron-CC-Math `3` and `4plus` | I propose pool-proportional 38.05B / 22.95B (DERIVED, not owner-approved) | 1–2 |
| D2 | **Which dolma3 ingredient** — they are two *versions of the same 100B mix*, not two halves | drawing both **double-counts documents** | 3 |
| D3 | Is the 14% QA draw from the QA *subset* (⇒ ~0.85 epochs, not 0.14) or the whole mix? | the report's epoch column is wrong either way | 3 |
| D4 | Which Cosmopedia configs — the web-free set is only ~2.0B, **less than the 4.0B target** | falling back to `web_samples_*` adds a 4th correlated view of web text | 5 |
| D5 | `nemotron-math-textbooks` category: `synthetic` or `math`? | the two documents already disagree with each other | 6 |
| D6 | Reference split between `finewiki` (8.87B claimed 8.8B) and pre-1929 books | the category over-draws | 7 |
| D7 | A **ninth category** for reasoning, or fold into `synthetic`? §7 says do NOT blend QA and reasoning | no category exists to put the row in | 4 |

**Three numbers in the report that do not reconcile** (flagged, not resolved — check the denominator
before calling either side stale): the reference pool `26.2B` vs 8.87 + ~10.7 = **19.6B** (row 7); the
reasoning pool `~50B` vs **~15–19B** from the best candidate (row 4); the QA row's `0.14` epochs vs
**~0.85** under prefix selection (row 3).

**One prior finding I am correcting, and one tasking error:**
- `source-encoding-audit.md` §12b called Math-Textbooks *"implausibly dense (1.23 bytes/token), do not
  use 25.1B"* — **that divided COMPRESSED parquet bytes by tokens.** Corrected with a real footer read:
  **3.997 bytes/token.** The card's figure is fine. (row 6)
- My brief called `WEEK1-CORPUS-SURVEY.md` a ~35-corpus index. **It is a code-reuse audit of a sibling
  checkout and mentions none of these corpora.** The real index is
  `artifacts/impl-plan/source-encoding-audit.md` §14. (row 4)

**What is genuinely new here vs the prior audit:** all 8 pinned revision shas; the Nemotron-CC-Math
`text`/`id` columns (the plan's named open item) from a real footer; the exact NVIDIA licence
instrument and its §2.2.2/§3.3 text; the two-ingredient double-count; the hyphen/underscore
`wiki_to_rcqa` trap; footer confirmation of all 8 Cosmopedia configs; the corrected Math-Textbooks
arithmetic; and the pre-1929 repo id with its schema read from real bytes.

---

## Pinned revisions — MEASURED 2026-08-08 via `GET https://huggingface.co/api/datasets/<repo>` → `.sha`

All are full 40-char commit shas. **Never read `resolve/main`.**

| repo | sha (40-char) | gated | lastModified | card license tag |
|---|---|---|---|---|
| `nvidia/Nemotron-CC-Math-v1` | `397a2502f2028c659ba411a6c4935b464a7f03aa` | **`auto`** | 2025-12-23T00:17:16Z | `other` |
| `allenai/dolma3_dolmino_mix-100B-1125` | `f23aa129fda8335ba9760057bcc1f0c02f3d068b` | false | 2026-02-23T19:03:37Z | `odc-by` |
| `HuggingFaceTB/cosmopedia` | `0ae6ec63f91742bd2d1eaef4f02232c55d719385` | false | 2024-08-12T22:05:49Z | `apache-2.0` |
| `nvidia/Nemotron-Pretraining-Specialized-v1` | `9ed3718b5f2ae29074c5e34e64115432b7c4320f` | false | 2025-12-22T17:17:17Z | `cc-by-4.0` |
| `common-pile/pre_1929_books_filtered` | `23f9d96dbb1db3324bbc9fbfe1f8299cc799c4d1` | false | 2025-06-06T03:58:46Z | (none in cardData) |
| `common-pile/library_of_congress_filtered` | `56725c7aa1bb320703e22eb5f42903173d5bac3d` | false | 2025-06-06T03:55:59Z | (none in cardData) |
| `common-pile/biodiversity_heritage_library_filtered` | `0486ed637d0d7aaff264bc77fe21a7444e0215cd` | false | 2025-06-06T03:51:12Z | (none in cardData) |
| `common-pile/gutenberg_filtered` | **HTTP 404 — DOES NOT EXIST** | — | — | — |

**Config lists, MEASURED from the same call's `cardData.configs[].config_name`:**

- `nvidia/Nemotron-CC-Math-v1` → **exactly three: `3`, `4plus`, `4plus_MIND`.** ✅ Confirms the card:
  **`3plus` is NOT a loadable config**, so the registry needs TWO rows, and `4plus_MIND` is the third
  config that must be recorded EXCLUDED.
- `HuggingFaceTB/cosmopedia` → 8: `auto_math_text`, `khanacademy`, `openstax`, `stanford`, `stories`,
  `web_samples_v1`, `web_samples_v2`, `wikihow`.
- `nvidia/Nemotron-Pretraining-Specialized-v1` → 6, including `Nemotron-Pretraining-Math-Textbooks`.
- `allenai/dolma3_dolmino_mix-100B-1125` → one declared config, `default` (the 323 `data/<dir>/`
  subdirectories are NOT configs; selection is by path prefix, see §3 below).

---

---

## 🛑 LICENSE BLOCKER — `nvidia/Nemotron-CC-Math-v1` carries the SAME §2.2.2 that blocked the reservoir

**MEASURED 2026-08-08.** Fetched `LICENSE.md` in full (11,011 bytes) from
`https://huggingface.co/datasets/nvidia/Nemotron-CC-Math-v1/resolve/397a2502f2028c659ba411a6c4935b464a7f03aa/LICENSE.md`
with an authenticated token (the gate is `auto` and this account has accepted it).

**The exact license string for the registry row is not "other" and not "NVIDIA Open Data License
Agreement".** It is:

> **`NVIDIA Data Agreement for Model Training (v. August 15, 2025)`**

The `license: other` tag on the card is the HF placeholder; the real instrument is the file above.
An earlier note in `source-encoding-audit.md` §5a called it the *"NVIDIA Open Data License
Agreement"* — **that name does not appear anywhere in the actual document. Corrected here.**

**Verbatim clauses that bear on this corpus:**

- **§2.1 Availability** — *"NVIDIA makes the Datasets available to Company solely for the purpose of
  **internal training** of Company AI Solutions with facts and ideas, including patterns and
  correlations (\"Purpose\")."*
- **§2.2.1** — *"Use, store or retain the Datasets for any other purpose than the Purpose."* (forbidden)
- 🛑 **§2.2.2** — *"Sell, rent, sublicense, transfer, distribute, sublicence, publicly display,
  publicly perform or **otherwise make available to others** the Datasets."* (forbidden)
- **§2.2.3** — *"Use the Datasets in any manner that would cause them to become subject to an
  open-source license."* (forbidden)
- **§2.3.1** — NVIDIA *"does not grant and does not purport to grant any rights to access or use any
  copyrighted material that may be contained within the Datasets."*
- **§3.3 Effect of Termination** — on termination for convenience (either party, 30 days' notice)
  *"within fourteen (14) days, Company will stop using the Datasets and delete and destroy copies"*,
  and NVIDIA may demand written certification of compliance. **A frozen `vN` in `s3://edullm-data`
  cannot be deleted or edited in place** (CLAUDE.md "frozen means frozen"), so this clause and our
  own immutability invariant are in direct tension.
- **§2.3.3 / §6.2** — model weights and outputs remain ours; this is the one clause in our favour.

**Why this is the same blocker, not a new one.** Memory note *"Nemotron license blocks the
reservoir"* recorded §2.2.2 as fatal to a **shared** corpus. The clause here is **verbatim identical**
in this different repo's own LICENSE.md. So the question is not resolved by switching repos —
**`Nemotron-CC-Math-v1` inherits it.**

**What is DIFFERENT and is why this needs an owner decision rather than an automatic exclusion:**
the reservoir was explicitly a *shared* artifact. Whether `s3://edullm-data` counts as "making
available to others" turns on who can read it, which is a fact about our bucket policy, not about
the license. **This worker does not decide it. FLAGGED FOR THE OWNER, and it gates 61.0B tokens
(45.0B stage 1 + 16.0B stage 2 = 6.1% of the 1.0T corpus).**

**A second, separate entanglement, CARD (§5a of the encoding audit, re-confirmed here):** the data is
Phi-4 output, so resulting models *"may be subject to redistribution and use requirements"* in the
**Phi-4 License Agreement**. That attaches to our **weights**, not to the corpus.

**Contrast — the other two Nemotron-adjacent sources are clean:**

| repo | license | share/redistribution clause? |
|---|---|---|
| `nvidia/Nemotron-CC-Math-v1` | **NVIDIA Data Agreement for Model Training (v. Aug 15 2025)** | 🛑 **YES — §2.2.2** |
| `nvidia/Nemotron-Pretraining-Specialized-v1` | `cc-by-4.0` (card tag; ungated) | no (attribution only) — see §6 |
| `allenai/dolma3_dolmino_mix-100B-1125` | `odc-by` | no |
| `HuggingFaceTB/cosmopedia` | `apache-2.0` | no |

---

## Detail sections

### Rows 1 & 2 — `nvidia/Nemotron-CC-Math-v1`, configs `3` and `4plus`

**The open item from `IMPLEMENTATION-PLAN.md` §10 — "the `text` and id column NAMES are still
unconfirmed in writing" — is now CLOSED. Both names came from a real parquet footer.**

#### 🛑 First, the access finding, because it changes how the schema was obtained

**The gate is NOT accepted on this machine's token.** MEASURED 2026-08-08:
`HEAD resolve/397a2502…/3/part_000000.parquet` with `Authorization: Bearer <~/.cache/huggingface/token>`
returns **HTTP 403** with header **`X-Error-Code: GatedRepo`** — *"Access to dataset
nvidia/Nemotron-CC-Math-v1 is restricted and you are not in the authorized list."*
`datasets-server` `/info`, `/splits` all return **HTTP 404 `ExternalAuthenticatedError`**.

What IS readable without the gate: the repo metadata, the **tree API**, `README.md`, and `LICENSE.md`.
So the file inventory and the license below are MEASURED; the payload bytes are not reachable here.

**⚠️ This contradicts nothing prior, but it does mean the 134.0B measurement was made by a teammate
whose token IS in the authorized list. Whoever runs the ingest needs that access, not just an
accepted checkbox on a different account.** Add it to the blocker list: **gate access is per-account.**

#### The schema — MEASURED from an UNGATED mirror, and here is exactly why that is legitimate

The gate prompt itself points at `nvidia/Nemotron-Pretraining-Dataset-sample` (for its LICENSE.md).
That repo is **ungated** (`gated: false`), sha **`3ad096e6394e487bb4f778733300da85275bb449`**, and it
carries a config named **`Nemotron-CC-MATH`** — NVIDIA's own published sample of this exact corpus.

Footer read over HTTP Range, **65,536 bytes fetched, 1 request, no dataset download**:
`Nemotron-CC-MATH/part_0000.parquet` (1,746,193 bytes, 954 rows, 1 row group,
`created_by: parquet-cpp-arrow version 20.0.0`).

**Complete leaf list, `path_in_schema` verbatim, with footer `total_uncompressed_size`:**

| `path_in_schema` | uncompressed bytes | num_values |
|---|---|---|
| **`text`** | 3,194,664 | 954 |
| `metadata.warc_filename` | 115,336 | 954 |
| `metadata.warc_id` | 50,005 | 954 |
| **`id`** | 39,489 | 954 |
| `metadata.nemocurator_scores` (double) | 2,651 | 954 |
| `metadata.finemath_scores` (double) | 1,090 | 954 |
| `metadata.nemocurator_int_scores` (int64) | 488 | 954 |
| `metadata.finemath_int_scores` (int64) | 132 | 954 |
| `metadata.models_used.list.element` | 77 | 954 |
| `metadata.category` | 67 | 954 |

Parquet schema, verbatim: `id` and `text` are **top-level `optional binary (String)`**; `metadata` is
a **struct**, and `metadata.models_used` is a `list<string>`.

- ✅ **`text_column` = `text`** — a **flat top-level leaf**. There is **exactly one leaf named `text`**
  in the whole schema, so the FinePhrase `.names.index("text")` trap **does not fire here**. Grade:
  **MEASURED** (footer, ungated mirror).
- ✅ **`id_column` = `id`** — top-level, present, 39,489 bytes / 954 values ≈ 41 bytes each
  (DERIVED — consistent with a UUID-or-URL-shaped string, not an integer).
- ✅ **`domain_column` = `metadata.category`** — but see the warning below before using it.

#### ⚠️ The corroboration caveat, stated plainly rather than buried

**This is the SAMPLE repo, not the gated one.** The risk that the sample's schema differs from the
full corpus is real but small, and here is the evidence bounding it, not an assurance:

- The gated repo's `README.md` (fetched at the pinned sha) says users *"can download subsets of the
  data based on the metadata schema described above"* — **and the card contains no such schema**, which
  is the same gap §5a of the encoding audit recorded. So the card cannot corroborate it.
- The gated repo's own **`refs/convert/parquet` branch IS listable** and its file sizes match the main
  branch **byte-for-byte** (`3/train/0000.parquet` = 1,899,869,110 = `3/part_000000.parquet`). That is
  consistent with an untransformed auto-conversion, but it is **not** a schema read.
- **Grade: MEASURED for the sample repo; DERIVED (high confidence) for configs `3`/`4plus`.**
  **Settling job, ~1 minute for whoever holds gate access:** run
  `artifacts/recount/_fp_footer_leaf.py`-style footer read on
  `3/part_000000.parquet` at sha `397a2502f2028c659ba411a6c4935b464a7f03aa` and confirm the ten leaves
  above. **Do not skip this — a wrong `text_column` is the exact silent-corruption class this file exists
  to prevent.**

#### 🛑 `metadata.category` is a real column but is NOT a safe `domain_column` as-is

MEASURED: 67 uncompressed bytes across 954 values — i.e. it dictionary-compresses to near nothing, so
it holds **one or very few distinct values in this sample**. `domain_column` becomes a **path segment**
(`CorpusSpec` docstring, `corpus.py:237-240`), and `SAFE_SEGMENT_RE` is enforced only on `source_label`,
**not** on inherited domain values — the `'C#'` precedent (encoding audit §3a) shows an unsafe inherited
value is not caught downstream. **Recommendation: set `domain_column = None` (flat) for both rows**
unless someone first enumerates the distinct values of `metadata.category` and checks them against
`SAFE_SEGMENT_RE`. The §1.2 rule (inherit a domain only where upstream ships one) is satisfied either
way; a single-valued column buys nothing.

#### File inventory — MEASURED from the tree API at the pinned sha

| config | first files (bytes) | note |
|---|---|---|
| `3` | `3/part_000000.parquet` 1,899,869,110 · `…000001` 1,914,514,052 · `…000002` 1,907,254,608 … | ~1.9 GB/file, uniform |
| `4plus` | `4plus/part_000000.parquet` 1,139,076,682 · `…000001` 1,436,993,413 … | ~1.1–1.5 GB/file, variable |
| `4plus_MIND` | `4plus_MIND/part_000000.parquet` 1,010,906,573 … | **EXCLUDED — see below** |

Path template for the reader: **`<config>/part_NNNNNN.parquet`** (six digits) — note the sample repo
uses `part_NNNN` (four), so **do not copy the sample's filename pattern into the ingest row.**

Split, from the README front-matter verbatim: every config declares `split: train` over
`path: <config>/*.parquet`. **Grade: MEASURED (README at pinned sha).** This closes the encoding
audit's "split names are never stated on the card" as UNVERIFIED — they are stated, in the
front-matter rather than the prose.

#### 🛑 Row X — `4plus_MIND` is EXCLUDED. Recorded so nobody re-adds it by prefix match.

- **It exists.** ✅ Confirmed from the card front-matter: the repo has **exactly three configs —
  `3`, `4plus`, `4plus_MIND`** — and from the tree (a populated `4plus_MIND/` directory).
- **Reason for exclusion: it is a REWRITE of `4plus`, so including both double-counts the same
  documents.** Same trap class as FinePhrase (encoding audit §4.2/§6). The 134.0B measurement is
  `3` + `4plus` **only**, which is correct.
- 🛑 **A prefix match on `4plus` MATCHES `4plus_MIND`.** Any registry/glob/config filter written as
  `startswith("4plus")` silently pulls in ~50–70B of duplicate tokens. **Match the config name
  EXACTLY, with `==`.** This is the single most likely way this source gets corrupted.
- Memory note *"Nemotron-CC-Math measured: 134.0B"* records the same: *"4plus_MIND is a REWRITE of
  4plus — including both double-counts."*

#### Pool tokens — carried forward, NOT re-measured (per instruction)

| config | pool_tokens | grade |
|---|---|---|
| `3` | **83,600,000,000** (≈83.6B) | **MEASURED** — teammate, 2026-08-07 |
| `4plus` | **50,400,000,000** (≈50.4B) | **MEASURED** — teammate, 2026-08-07 |
| total | **134,000,000,000** | 472,213,218,716 bytes × 0.283686 tok/byte, 1,920 random-offset docs, seed 42 |

DERIVED check on the split: 83.6 + 50.4 = 134.0 ✅. Artifact named in the plan:
`_nemotron_cc_math_dolma2_measure.json`.

#### Draw targets (from `FINAL-DATASET-REPORT.md` §3 + §4 — the two stages SUM against one pool)

Stage 1 45.0B + stage 2 16.0B = **61.0B total**, against a 134.0B pool = **0.46 epochs**
(report §4's own combined table). **If the registry carries one row per config, the 61.0B must be
split across the two rows and the split is a DECISION nobody has made.** The natural one is
pool-proportional: `3` gets 61.0 × (83.6/134.0) = **38.05B**, `4plus` gets 61.0 × (50.4/134.0) =
**22.95B** (DERIVED, my arithmetic, **not an owner decision**). Quality-weighting toward `4plus`
would be defensible too and would change both numbers. **FLAGGED: needs an owner call before the
registry row is frozen.**

#### Traps for both rows

1. 🛑 **P0 — `<|endoftext|>` is dolma2's EOS id 100257 AND Phi-4's, identically.** The corpus is Phi-4
   output with no documented special-token scrubbing. A leaked stop token becomes a **phantom document
   boundary**. `neutralize_boundary_markers()` is **mandatory**; emit a per-shard
   `eos_occurrences − document_count` counter. (encoding audit §5c, MEASURED id equality.)
2. 🛑 **NEVER unescape `\n`** — the text is LaTeX; `\neq` and `\nabla` are real and an unescape
   heuristic destroys exactly the math this source is bought for. (MEASURED on real Nemotron bytes,
   encoding audit §12d.)
3. 🛑 **Match the config with `==`, never a prefix** — `4plus` prefix-matches `4plus_MIND`.
4. **Config `3plus` DOES NOT EXIST.** A row with `config="3plus"` fails to resolve. Two rows required.
5. **Decontamination: the card's claim and our measurement disagree, and ours is the measurement.**
   Card claims LLM-based decontamination against MATH/GSM8K/MMLU/MMLU-Pro; our 13-gram scan found it
   **left 13.2× more contamination than it removed** (verbatim GSM8K test at Jaccard 1.0).
6. **Truncation is undetectable** — Phi-4 rewriting ran with some `max_tokens` and this repo ships
   **no `finish_reason` column** (confirmed: not among the 10 leaves). UNVERIFIED rate.
7. **`lynx` did the HTML→text conversion** — hard-wrapped lines / `[1]` / `[IMG]` artifacts
   UNVERIFIED (blocked on gate access here).
8. **Denser than prose: ~3.53 bytes/token** (DERIVED from the 134.0B measurement) vs ~4.31 for English
   on dolma2. `_CHARS_PER_TOKEN` is a reader stopping rule — using the prose default leaves the last
   shard unfilled and `verify` refuses the bundle.
9. 🛑 **LICENSE §2.2.2 — see the license blocker section above. This is unresolved and it gates 61.0B.**

#### The two registry rows, ready to paste (pending the two FLAGGED decisions)

```json
{
  "key": "nemotron-cc-math-3",
  "category": "math",
  "source_label": "nemotron-cc-math",
  "repo": "nvidia/Nemotron-CC-Math-v1",
  "config": "3",
  "revision": "397a2502f2028c659ba411a6c4935b464a7f03aa",
  "file_format": "parquet",
  "text_column": "text",
  "id_column": "id",
  "domain_column": null,
  "license": "NVIDIA Data Agreement for Model Training (v. August 15, 2025)",
  "share_alike": false,
  "target_tokens": 38050000000,
  "pool_tokens": 83600000000
}
```

```json
{
  "key": "nemotron-cc-math-4plus",
  "category": "math",
  "source_label": "nemotron-cc-math",
  "repo": "nvidia/Nemotron-CC-Math-v1",
  "config": "4plus",
  "revision": "397a2502f2028c659ba411a6c4935b464a7f03aa",
  "file_format": "parquet",
  "text_column": "text",
  "id_column": "id",
  "domain_column": null,
  "license": "NVIDIA Data Agreement for Model Training (v. August 15, 2025)",
  "share_alike": false,
  "target_tokens": 22950000000,
  "pool_tokens": 50400000000
}
```

⚠️ **`source_label` is the SAME string for both rows on purpose** — `3` and `4plus` are two quality
tiers of one corpus, and §1.1 fuses realness into the label, not the tier. `nemotron-cc-math` matches
`SAFE_SEGMENT_RE` (verified below). **If the two tiers must be distinguishable downstream, that is a
`labels`/partition concern, not a second `source_label`** — and note memory records `entry.labels` is
inside `manifest_sha256` and therefore **unbackfillable**, so decide before publish.

⚠️ **`share_alike: false` is asserted, not inferred** — the NVIDIA agreement is restrictive but has no
copyleft clause; §2.2.3 actually forbids the OPPOSITE (causing the data to become subject to an
open-source license).

**EXCLUDED row, to be carried in the registry as a comment or a `target_tokens: 0` reserve so that a
future agent finds it rather than rediscovering it:**

```
key: nemotron-cc-math-4plus-mind   EXCLUDED — DO NOT INGEST
repo/config: nvidia/Nemotron-CC-Math-v1 @ 4plus_MIND
reason: a REWRITE of config `4plus`. Ingesting both double-counts the same source documents.
        Prefix matching on "4plus" WILL pull this in — match config names with == only.
```

---

### Row 3 — `allenai/dolma3_dolmino_mix-100B-1125`, the QA-bearing 14.0B

**Pinned revision: `f23aa129fda8335ba9760057bcc1f0c02f3d068b`.** Ungated, `odc-by`.

#### 🛑 THE HEADLINE FINDING: this repo is TWO ALTERNATIVE 100B mixes, not one 100B pool

**CARD, verbatim, §Ingredients:**

> *"There were two ingredients used during stage 2 midtraining annealling of Olmo 3 32B. There were
> **2 versions of a 100B mix**: Ingredient 1 — **100B tokens** … Ingredient 2 — **100B tokens**"*

So `ingredient1` and `ingredient2` are **two versions of the same 100B mix** — alternatives AI2
compared, **not two halves of a 200B pool.**

**MEASURED corroboration from the tree, and it is strong.** For **every single one** of the 22
directories I sized, ingredient1 and ingredient2 have **IDENTICAL file counts** with **different byte
totals**:

| source | files (i1) | files (i2) | bytes (i1) | bytes (i2) | i2/i1 |
|---|---|---|---|---|---|
| `nemotron-synth-qa` | 1,024 | **1,024** | 7,921,661,574 | 8,936,868,750 | 1.128 |
| `reddit_to_flashcards` | 1,204 | **1,204** | 7,923,725,257 | 8,976,976,626 | 1.133 |
| `wiki_to_rcqa` part1 | 8,629 | **8,629** | 1,878,352,857 | 2,092,773,088 | 1.114 |
| `wiki_to_rcqa` part2 | 8,628 | **8,628** | 1,734,890,193 | 1,925,043,516 | 1.110 |
| `dolmino_1-flan` | 209 | **209** | 7,533,258,328 | 8,501,494,713 | 1.129 |
| `tulu-3-sft` | 75 | **75** | 1,292,477,554 | 1,408,004,810 | 1.089 |
| `math-meta-reasoning` | 36 | **36** | 348,561,677 | 381,635,235 | 1.095 |
| `code-meta-reasoning` | 50 | **50** | 447,601,056 | 494,714,383 | 1.105 |
| `general_reasoning_mix` | 529 | **529** | 5,830,467,832 | 6,029,891,703 | 1.034 |
| `omr-rewrite-fullthoughts` | 21 | **21** | 2,405,074,795 | 2,628,257,428 | 1.093 |
| `program_verifiable` | 9 | **9** | 102,518,829 | 112,787,200 | 1.100 |

Eleven sources, **eleven exact file-count matches** with byte ratios clustered at 1.09–1.13. That is
the signature of the same shard structure re-sampled at a slightly different rate, **not** of two
disjoint halves.

🛑 **CONSEQUENCE: drawing from both ingredients double-counts documents — the same failure mode as
`4plus_MIND`, on a bigger source. PICK ONE INGREDIENT.** The report's *"100.0B pool"* figure is
**correct only if it means one ingredient**; a naive sum of the whole `data/` tree would say ~200B and
would be wrong.

**Grade: MEASURED (file counts + bytes, tree API at the pinned sha) + CARD (the 100B-each statement).**
**What is NOT measured: whether the two ingredients' document `id` sets actually overlap.** The
inference above is strong but circumstantial. **Settling job: decompress one matching
`.jsonl.zst` from each ingredient and intersect the `id` fields — a few hundred KB.** I did not run it
because the `.zst` reader is another worker's scope; **this is the one open question on this row.**

**Recommendation: `ingredient1`.** Rationale: it is the only ingredient that carries `tinymath-pot`
(MEASURED — see the naming table below), so it is the more complete of the two; and it is smaller,
which matters not at all for a 14B draw. **Either is defensible. What is NOT defensible is both.**

#### The taxonomy — MEASURED, 323 directories enumerated at the pinned sha

Tree API, paginated, `data/` non-recursive. **323 directories** ✅ (matches the prior wave's count),
resolving to **200 distinct source names** after stripping the `ingredientN-` prefix:

| bucket | distinct names | note |
|---|---|---|
| `common_crawl-*` | 48 | web |
| `stack_edu-fim_vigintile_*` | 60 | code, 4 vigintiles × 15 languages |
| `olmocr_science_pdfs-*` | 72 | PDFs, and the two ingredients name them DIFFERENTLY (below) |
| **everything else** | **20** | the QA / math / thinking / instruction sources |

The 20 non-bulk names, verbatim, with which ingredients carry each:

```
code-meta-reasoning        i1 i2      nemotron-synth-qa          i1 i2
cranecode                  i1 i2      omr-rewrite-fullthoughts   i1 i2
cranemath                  i1 i2      program_verifiable         i1 i2
dolmino-math               i1 i2      reddit_to_flashcards       i1 i2
dolmino_1-flan             i1 i2      stem-heavy-crawl           i1 i2
general_reasoning_mix      i1 i2      tinymath-mind              i1 i2
math-meta-reasoning        i1 i2      tinymath-pot               i1 ONLY
megamatt                   i1 i2      tulu-3-sft                 i1 i2
wiki_to_rcqa-part1         i1 ONLY    wiki_to_rcqa_part1         i2 ONLY
wiki_to_rcqa-part2         i1 ONLY    wiki_to_rcqa_part2         i2 ONLY
```

#### 🛑 A NAMING TRAP NOBODY HAS RECORDED: hyphen vs underscore across ingredients

**MEASURED.** The same source is spelled differently in the two ingredients:

| ingredient1 | ingredient2 |
|---|---|
| `data/ingredient1-wiki_to_rcqa-part1` (**hyphen** before `part1`) | `data/ingredient2-wiki_to_rcqa_part1` (**underscore**) |
| `data/ingredient1-wiki_to_rcqa-part2` | `data/ingredient2-wiki_to_rcqa_part2` |

A glob written as `*wiki_to_rcqa-part*` silently matches **only ingredient1**; `*wiki_to_rcqa_part*`
matches **only ingredient2**. This is the same class as the prior wave's note that the two ingredients
name the olmOCR PDF directories differently (`-<topic>-2e12` vs `-<topic>-length_2e12`) — **but that
note did not cover `wiki_to_rcqa`, and this one bites the QA draw specifically.**
Since the recommendation is to pick ONE ingredient, the trap is defused by construction —
**but only if the selection is an explicit directory list, never a glob.**

#### Which prefixes are the QA-bearing 14B — MEASURED against the card's own Category column

The card ships a `Source | Category` table. Mapping it onto the measured directory names, the
**`QA (synth)`** category is exactly three sources:

| card source | card category | directory (ingredient1) | compressed bytes |
|---|---|---|---|
| Nemotron Synth QA | **QA (synth)** | `data/ingredient1-nemotron-synth-qa` | 7,921,661,574 |
| Reddit To Flashcards | **QA (synth)** | `data/ingredient1-reddit_to_flashcards` | 7,923,725,257 |
| Wiki To RCQA | **QA (synth)** | `data/ingredient1-wiki_to_rcqa-part1` + `-part2` | 3,613,243,050 |
| | | **QA total (i1)** | **19,458,629,881** |

✅ **This confirms the prior wave's prefix recommendation exactly** (`-nemotron-synth-qa`,
`-reddit_to_flashcards`, `-wiki_to_rcqa-*`) — and now it is grounded in the card's own category
labels rather than in name-reading.

**Is 19.46 GB compressed enough for a 14.0B-token draw?** DERIVED, and it is **tight**:
zstd on text runs ~3.0–3.5× so ~58–68 GB of text → at ~4.0 bytes/token (prose; QA is prose-shaped)
that is **~15–17B tokens**. **So the 14.0B target is ~85% of the QA pool — roughly 0.85 epochs on
these three directories, not the 0.14 the report's table states.** The report's 0.14 assumes the
draw is spread over the whole 100.0B mix; **it is not, if QA is selected by prefix.**

🛑 **This is a real discrepancy and I am flagging it rather than resolving it.** Two readings:
(a) the 14% row means "14% of the cooldown drawn from the QA-bearing *subset*", in which case
epochs ≈ 0.85 and the row is near-exhausted; or (b) it means 14B drawn from the full 100B mix
without prefix selection, in which case it is not "QA-bearing" in any selective sense and the
+3.2 MMLU evidence in report §7 does not apply. **The report says "QA-bearing" and §7 leans on the
QA-specific ablation, which points to (a).** **Owner decision. It changes the epoch column and it
may change the 14% share.**
**Grade of the byte totals: MEASURED. Grade of the token conversion: DERIVED with an assumed
compression ratio — the compression ratio is UNMEASURED and is the weak link.**
*Settling job:* parse `Frame_Content_Size` from the zstd frame headers of ~20 files (18 bytes each)
for exact uncompressed sizes; then one sampled tokens/byte. Cheap, and it collapses the range.

**If more QA volume is needed, the adjacent candidates are `Instruction (synth)`:**
`dolmino_1-flan` (7.53 GB) and `tulu-3-sft` (1.29 GB). ⚠️ **They are a different category on the
card and report §7 measures QA and instruction-following separately — do not silently merge them
into the QA row.**

#### Schema

- `text_column` = **`text`**, `id_column` = **`id`**. **Grade: CARD** — the front-matter at the pinned
  sha declares exactly 9 features, all `dtype: string`: `id`, `text`, `metadata`, `source`, `version`,
  `created`, `added`, `doc`, `attributes`.
- ⚠️ **NOT a parquet `path_in_schema`** — this source is `.jsonl.zst`, so `text_column` names a **JSON
  key**, not a parquet leaf. The `CorpusSpec` field is the same field, but the FinePhrase nested-leaf
  hazard does not apply; the corresponding hazard is a nested JSON object. **`metadata` and
  `attributes` are JSON-encoded STRINGS, not structs.**
- 🛑 **`doc` is also a `string` and is completely undocumented.** If `doc` ever holds the document text
  and `text` holds something else, that is the FinePhrase trap in JSON form. **UNVERIFIED — one
  `zstd -dc | head -1` settles it, and it MUST be done before the row is frozen.**
- `domain_column`: **`None`, with the domain carried by the DIRECTORY, not a column.** `source` exists
  as a feature but its values are UNVERIFIED. Recommendation: derive the label from the selected
  directory list (it is the category, per the card table) rather than inheriting an unverified column.
- **File layout, MEASURED:** `data/<ingredient><N>-<source>/CC-MAIN-YYYY-WW-part-NNNNN.jsonl.zst`.

#### Traps

1. 🛑 **Two ingredients = two versions of the same mix. Pick one. See above.**
2. 🛑 **hyphen-vs-underscore `wiki_to_rcqa` across ingredients.** Use an explicit directory list.
3. 🛑 **`.jsonl.zst` is unreadable by `corpus_read` today** — `zstandard` is not a declared dependency.
   Hard code blocker (owned by another worker; recorded here because it gates this row).
4. 🛑 **`neutralize_boundary_markers()` is MANDATORY.** The generator set spans Phi-4 (whose
   `<|endoftext|>` is **id 100257 = dolma2's EOS**), Qwen3, QwQ, DeepSeek, Gemini, Llama-Nemotron,
   gpt-oss, OpenThoughts2 — the card's source table names them.
5. **`[REMOVED]` redaction placeholders.** The sibling 6T card states AI2 redacted some olmOCR science
   PDFs *"indicated with `[REMOVED]` in the text field."* UNVERIFIED for this repo. A whole-document
   `[REMOVED]` (~4 tokens) is dropped by `MIN_DOC_TOKENS`, **but a PARTIAL one passes every filter.**
   Check both forms. (Not in the QA directories, but it is in the same repo.)
6. **`<think>` / `</think>`** in the reasoning directories are NOT dolma2 special tokens, so they are
   not boundaries — but shipping them in a pretraining corpus creates **an undeclared control token.**
7. **NO parquet footers and NO gzip ISIZE.** zstd has no ISIZE trailer. This is the **least
   footer-checkable source in the plan**; sizes require the frame header or decompression.
8. **AI2's dolma3 token figures ARE dolma2-comparable** (dolma3-tokenizer IS dolma2, per AI2's code) —
   the one source where a card token figure can be used directly. **But the card here gives no
   per-source token figures**, only "100B" per ingredient.

#### Registry row (pending the ingredient choice and the `doc` check)

```json
{
  "key": "dolma3-midtrain-qa",
  "category": "qa-forum",
  "source_label": "dolma3-midtrain-qa",
  "repo": "allenai/dolma3_dolmino_mix-100B-1125",
  "config": "data/ingredient1-nemotron-synth-qa,data/ingredient1-reddit_to_flashcards,data/ingredient1-wiki_to_rcqa-part1,data/ingredient1-wiki_to_rcqa-part2",
  "revision": "f23aa129fda8335ba9760057bcc1f0c02f3d068b",
  "file_format": "jsonl.zst",
  "text_column": "text",
  "id_column": "id",
  "domain_column": null,
  "license": "ODC-BY-1.0",
  "share_alike": false,
  "target_tokens": 14000000000,
  "pool_tokens": null
}
```

⚠️ **`pool_tokens: null` is deliberate**, per the `CorpusSpec` docstring (*"None where we have no
measurement we trust — better than a card figure, which would look like evidence"*). My ~15–17B
estimate carries an unmeasured compression ratio. **Note `__post_init__` (`corpus.py:261-266`) only
enforces `pool_tokens >= target_tokens` when `pool_tokens` is not None — so a null here also disables
the epoch guard on the tightest row in the table. That is a reason to measure it, not to guess it.**

⚠️ **`config` as a comma-joined directory list is a SHAPE THE REGISTRY HAS NEVER CARRIED.** Every
existing row's `config` is a single string. **Whoever writes the reader must confirm this parses;
otherwise the four directories need four rows sharing a `source_label`.** FLAGGED.

---

### Row 5 — `HuggingFaceTB/cosmopedia`, the 4.0B synthetic draw

**Pinned revision: `0ae6ec63f91742bd2d1eaef4f02232c55d719385`.** Ungated, **`apache-2.0`**.

#### Schema — MEASURED FROM REAL PARQUET FOOTERS, all 8 configs, not from `/info`

The prior wave read this from `datasets-server/info`. **I re-read it from the actual parquet footers
over HTTP Range at the pinned sha — 8 files, one per config, no dataset download.** The two agree,
which is worth stating because it is the first independent confirmation.

Every config has the **identical flat 6-leaf schema, zero nesting**
(`created_by: parquet-cpp-arrow version 14.0.2`):

`text_token_length` (int64) · `prompt` (string) · **`text`** (string) · `seed_data` (string) ·
`format` (string) · `audience` (string)

| config | files | total parquet bytes | rows (file 0) | `text` uncompressed (file 0) | `prompt` (file 0) |
|---|---|---|---|---|---|
| `web_samples_v1` | 139 | 38,978,124,936 | 89,399 | 358,067,225 | 134,093,226 |
| `web_samples_v2` | 118 | 32,658,254,617 | 87,677 | 350,874,735 | 142,249,430 |
| `stories` | 43 | 11,902,294,709 | 116,116 | 318,422,526 | 173,732,160 |
| `auto_math_text` | 18 | 4,461,401,898 | 108,328 | 303,401,870 | 178,747,346 |
| `stanford` | 13 | 3,302,284,560 | 78,464 | 387,841,097 | 95,568,911 |
| `wikihow` | 2 | 502,284,600 | 89,596 | 411,278,810 | 30,900,339 |
| `openstax` | 2 | 346,992,522 | 63,166 | 243,765,990 | 86,702,882 |
| `khanacademy` | 1 | 49,139,761 | 24,123 | 89,166,931 | 31,899,701 |
| **total** | **336** | **92,200,777,603** | | | |

- ✅ **`text_column` = `text`** — top-level, flat. **Exactly one leaf named `text`; no nesting anywhere
  in the file.** The FinePhrase `.names.index("text")` trap **cannot fire on this source.**
  Grade: **MEASURED** (8 independent footers).
- 🛑 **But the FinePhrase-class hazard is still present in a different form: `prompt` is REAL WEB TEXT.**
  MEASURED: `prompt` is 8–59% of `text` by uncompressed bytes depending on config
  (`wikihow` 7.5%, `auto_math_text` **58.9%**). Ingesting `prompt` would put un-attributed seed
  extracts into a corpus labelled synthetic. The columns have different names, so this is a
  lower-probability trap than FinePhrase's — **but `auto_math_text`'s prompt is over half the payload,
  so a column mistake there is not a rounding error.**

#### 🛑 `id_column` = **NONE**. Verified from real bytes, and here is the surrogate proposal.

**MEASURED: there is no `id`, no `uuid`, no `url`, no hash column in ANY of the 8 configs.** The
complete leaf list is the six columns above. This is not an oversight in a card — it is the file.

`sha256(id) % N` (the reservoir's partition and anti-join key) therefore **has no key on this source.**

**Proposed surrogate, and the reasoning for each rejection:**

| candidate | verdict |
|---|---|
| `sha256(text)` | 🛑 **REJECT.** The plan already says a hash-of-text id *"is not stable across a re-download"*. Worse here: the `lstrip()` fix below **changes the text**, so the id would depend on whether normalization ran before or after hashing. Two pipeline orderings ⇒ two different ids for one document. |
| `(config, row_index)` | 🛑 **REJECT.** Row index is only stable if row ORDER is stable, which is a property of the upstream files, not of the dataset. |
| ✅ **`(config, file_basename, row_index_within_file)`** | **PROPOSE.** e.g. `cosmopedia/web_samples_v2/train-00000-of-00118.parquet#87676`. |

**Why the proposed one is actually stable, stated as a claim with its condition:** the filenames encode
`-of-NNNNN`, so **any change to the file count changes every filename** and the drift is loud rather
than silent. Row order within a fixed parquet file is fixed by the file's bytes. **The condition:
this is stable only against the PINNED revision** — which is exactly why the revision above is
mandatory. **Grade: DERIVED. It is a proposal, not a measurement, and the reader must implement it.**

⚠️ **One consequence nobody should discover later: a surrogate id is NOT comparable across sources.**
Every other row's `id` is upstream's own identifier. Any cross-source anti-join or dedup keyed on `id`
silently excludes Cosmopedia. Note it on the row.

#### Which config(s) should the 4.0B come from — the question the brief asks

**Recommendation: `auto_math_text` + `stanford` + `openstax` + `khanacademy` + `wikihow`, and
EXCLUDE `web_samples_v1`/`v2` and `stories`.** Reasoning:

1. 🛑 **`web_samples_v1`/`v2` are ~75% of Cosmopedia (CARD) and are seeded from RefinedWeb**, i.e.
   **web text rephrased by Mixtral.** The corpus is already **41.0% DCLM + 25.2% FineWeb-Edu + 3.6%
   FinePhrase** (report §4) — and FinePhrase is *itself* rephrased FineWeb-Edu. Adding rephrased web
   as the "synthetic" 4% adds a **fourth correlated view of web text**, which is the exact concentration
   failure the report's own §6 warns about for math. **The 4% synthetic row is the one place the corpus
   can buy something that is NOT web-shaped — spending it on rephrased web wastes it.**
2. ✅ The five recommended configs are **textbook/course/how-to shaped** — the closest thing in this
   corpus to the pedagogical register the eduLLM target implies.
3. **They are big enough. DERIVED:** `auto_math_text` + `stanford` + `openstax` + `khanacademy` +
   `wikihow` = 4,461,401,898 + 3,302,284,560 + 346,992,522 + 49,139,761 + 502,284,600 =
   **8,662,103,341 parquet bytes = 9.4% of the repo.** The report's 21.7B pool figure × 0.094 ≈ **2.0B
   tokens — WHICH IS LESS THAN THE 4.0B TARGET.** 🛑
   **Adding `stories` (11.9 GB) brings it to 20,564,398,050 bytes = 22.3% ≈ 4.8B tokens**, which clears
   4.0B with little headroom.
   **So the clean recommendation does NOT fit the target and this is a real finding, not a detail.**
   Three ways out, for the owner: (a) include `stories` — narrative, not web-rephrased, and arguably
   fine for an education corpus; (b) cut the Cosmopedia row from 4% to ~2% and give the rest to
   Nemotron Math-Textbooks, which has 8× headroom; (c) accept `web_samples_*`. **I recommend (a) or
   (b); (c) defeats the purpose of the row.** FLAGGED FOR THE OWNER.

⚠️ **`openstax` and `khanacademy` are Cosmopedia configs NAMED for their seed sources.** Per the
CK-12 rule I was asked to apply: **neither is CK-12** — CK-12 does not appear among Cosmopedia's 8
configs, so **the CK-12 prohibition does not touch this source.** Separately, these are
Mixtral-*generated* text seeded from OpenStax/Khan outlines, not the textbooks themselves;
`artifacts/licenses/openstax-books.json` covers the real OpenStax corpus, which is a different thing.
**Cosmopedia's own license is `apache-2.0` and governs the generated text.**

#### 🛑 The leading-space trap, and exactly where the `lstrip()` goes

**Carried forward, MEASURED by the prior wave at 303/303 documents across all 8 configs** — a Mixtral
SentencePiece `▁` detokenization artifact. Under dolma2's byte-level BPE the space-prefixed and bare
forms are **different token ids** (`The` = 791, `ĠThe` = 578), so **every document's first token is its
mid-sentence variant, immediately after our appended EOS.**

**Mitigation: `text.lstrip()` — or more precisely, strip exactly one leading space — applied
PER-SOURCE, on the Cosmopedia rows only.**

🛑 **It must NOT be applied globally: leading whitespace is semantic in code**, and this corpus draws
90.0B tokens of `common-pile/stackv2` (report §3) plus 60 `stack_edu-fim` directories from dolma3.
A global `lstrip()` would corrupt indentation-significant Python in both. **The fix belongs on the
registry row / the per-source normalization hook, never in the shared tokenize path.**

**Note for whoever implements it:** if the surrogate id above is used, **hash the id, not the text**,
so the `lstrip()` cannot change the partition assignment.

#### Traps

1. 🛑 **Leading space on 100% of documents — per-source `lstrip()` only, never global.**
2. 🛑 **No document id at all — needs the surrogate above; ids are not cross-source comparable.**
3. 🛑 **`prompt` holds real web seed text — never ingest it.** Up to 58.9% of `text` size.
4. **HTML entities MEASURED in `auto_math_text`** (16 `&amp;` in 41 docs). `html.unescape()` is safe
   for this prose source, **not** for code or markdown tables. Per-source, again.
5. **`text_token_length` is a MISTRAL-7B count, not dolma2** (CARD, verbatim). The card's "25 billion
   tokens" is likewise Mistral-7B. **Do not sum this column as dolma2 tokens.** Cosmopedia is NOT in
   `artifacts/recount/`, so the report's 21.7B pool figure is **CARD-derived, not measured by us.**
6. **Truncation: UNVERIFIED.** No `finish_reason` column (confirmed: not among the 6 leaves).
7. **Near-duplication by construction.** CARD: seed samples were reused with different
   `format`/`audience` (e.g. `stanford`: 4 prompt styles each) while MinHash found "under 1%".
   ✅ **`format` and `audience` ARE real columns** (MEASURED) — use them to partition, the way
   `sha256(id)%4` partitions FinePhrase.
8. **v0.2 exists** in `HuggingFaceTB/smollm-corpus` with a **different generator** (SmolLM lineage) and
   therefore different artifacts. The pinned sha above is **v0.1**. They are not interchangeable.
9. **Boundary markers: MEASURED ZERO** `<|endoftext|>`/`<s>`/`</s>`/`[INST]` in 303 sampled documents.
   Run `neutralize_boundary_markers()` anyway and expect ~zero.

#### Registry row (pending the config-set decision)

```json
{
  "key": "cosmopedia",
  "category": "synthetic",
  "source_label": "cosmopedia",
  "repo": "HuggingFaceTB/cosmopedia",
  "config": "auto_math_text,stanford,openstax,khanacademy,wikihow,stories",
  "revision": "0ae6ec63f91742bd2d1eaef4f02232c55d719385",
  "file_format": "parquet",
  "text_column": "text",
  "id_column": "NONE — no id/uuid/url column exists (MEASURED, 8/8 config footers). Surrogate: (config, file_basename, row_index). Hash the SURROGATE, never the text: the per-source lstrip() mutates text.",
  "domain_column": "seed_data",
  "license": "Apache-2.0",
  "share_alike": false,
  "target_tokens": 4000000000,
  "pool_tokens": null
}
```

⚠️ **`domain_column: "seed_data"` is a SUGGESTION and is UNVERIFIED.** The column exists and is tiny
(8,536 bytes / 87,677 values — so very few distinct values), which is the right shape for a domain
label, **but I have not read its values and it must be checked against `SAFE_SEGMENT_RE` before use.**
Safer default: `null`, with the config name carrying the distinction.

⚠️ **`pool_tokens: null`** — the 21.7B in the report is CARD-derived from a Mistral-7B count. Setting a
number here that came from a different tokenizer would look like evidence. Note again that a null
**disables the epoch guard** in `corpus.py:261-266`.

---

### Row 6 — Nemotron Math-Textbooks, the 3.0B draw

**Repo: `nvidia/Nemotron-Pretraining-Specialized-v1`** — ✅ **NOT `nvidia/Nemotron-Pretraining-SFT-v1`**,
which is gated and has no math-textbooks config. **Pinned revision:
`9ed3718b5f2ae29074c5e34e64115432b7c4320f`.** Ungated. Config **`Nemotron-Pretraining-Math-Textbooks`**.

The repo's own card title is **"Nemotron-Pre-Training-Dataset-v2.1"** and it states this collection
*"is an extension of the previously released Nemotron-Pretraining-SFT-v1 with updated naming"* and
*"is ready for commercial use."*

#### Schema — MEASURED from a real parquet footer at the pinned sha

`Nemotron-Pretraining-Math-Textbooks/part_000000.parquet`, 2,391,061,448 bytes, **1,000,000 rows**,
4 row groups, `created_by: **Polars**`. 65,536 bytes fetched, 1 request.

| `path_in_schema` | uncompressed bytes | num_values |
|---|---|---|
| **`text`** (`large_string`) | 7,782,530,544 | 1,000,000 |
| **`uuid`** (`large_string`) | 40,004,246 | 1,000,000 |
| `metadata.category` | 39,004,127 | 1,000,000 |
| `metadata.models_used` | 33,003,000 | 1,000,000 |
| `license` | **332** | 1,000,000 |

- ✅ **`text_column` = `text`** — top-level, flat, **exactly one leaf named `text`.** No trap.
  Grade: **MEASURED**. The card agrees and is unusually explicit: *"**text**: The **primary data
  field**, containing the content to be used for pretraining."*
- ✅ **`id_column` = `uuid`** — present, top-level. Card: *"**uuid**: The unique identifier for this
  dataset entry."* 40,004,246 / 1,000,000 = **40.0 bytes/value**, i.e. a 36-char UUID + 4-byte length
  prefix (DERIVED — the arithmetic closes exactly, which is a nice independent confirmation that this
  really is a UUID). **This is the ONLY stage-2 source with a clean upstream id.**
- **`domain_column`**: `metadata.category` exists but, per the card, its value is the config name
  itself (*"e.g. 'Nemotron-Pretraining-RQA'"*) — **single-valued within a config, so it carries no
  information.** Recommendation: **`null`**.
- ⚠️ **`metadata` is a struct of two `large_string`s** — note `models_used` is a **STRING here**, while
  in the `Nemotron-CC-Math` sample it is a `list<string>`. Different NVIDIA repos, different shapes.
- **`license` is a per-document column** (332 bytes / 1,000,000 values ⇒ one distinct value,
  dictionary-encoded). Prior wave sampled it as `'cc-by-4.0'`. **License is checkable per row, which
  is better than any other source in this corpus.**

#### 🛑 I am correcting a prior finding: the "1.23 bytes/token, does not close" contradiction was a DENOMINATOR ERROR

`source-encoding-audit.md` §12b flagged: *"30.84 GB of files ÷ 25.1 B tokens ≈ 1.23 bytes/token, which
is implausibly dense… Do not use 25.1 B."* **That divided COMPRESSED PARQUET bytes by tokens.**

**Corrected, with the footer measurement that was missing:**

```
text uncompressed / parquet bytes  (file 0)  = 7,782,530,544 / 2,391,061,448  = 3.25484
total text uncompressed (est.)  = 30,841,263,255 x 3.25484        = 100,383,481,778
minus 4-byte length prefix      - 4 x 12,899,767 rows             = 100,331,882,710
100,331,882,710 / 25.1e9 CARD tokens                              = 3.997 bytes/token
```

**3.997 bytes/token is exactly what English prose looks like** (dolma2 English ≈ 4.31; textbook prose
with LaTeX slightly denser). **The contradiction is resolved and the card's 25.1B is now plausible
rather than suspect.** Grade: **DERIVED** (footer ratio MEASURED on 1 of 13 files; row count from the
prior wave's `/size`).

**Implied dolma2 pool, showing the sensitivity rather than a single number:**

| assumed bytes/token | implied pool |
|---|---|
| 3.53 (Nemotron-CC-Math's MEASURED LaTeX-heavy rate) | 28.4B |
| 4.00 | 25.1B |
| 4.31 (dolma2 English prose) | 23.3B |

**So the pool is ~23–28B and the report's 27.5B sits inside that band.** Against a 3.0B target that is
**7.8–9.5× headroom** — the most comfortable row in stage 2. **I still recommend `pool_tokens: null`**
until someone runs a real sampled tokens/byte, because every figure above inherits the card's 25.1B
or an assumed rate. **⚠️ Do not "upgrade" 27.5B to MEASURED on the strength of this section — it is
DERIVED and it agrees with the card, which is weaker evidence than it looks.**

Mean document size, MEASURED: **7,778.5 text bytes/doc** (file 0) ≈ **~1,950 tokens** — long-form
textbook prose, comfortably above `MIN_DOC_TOKENS`.

**File inventory, MEASURED:** 13 files, `Nemotron-Pretraining-Math-Textbooks/part_NNNNNN.parquet`,
**30,841,263,255 bytes total** (matches the prior wave's figure exactly ✅). Split `train`, declared in
the front-matter as `path: Nemotron-Pretraining-Math-Textbooks/*.parquet`.

#### License — the exact string, and it is CLEAN (contrast Nemotron-CC-Math)

**CARD, verbatim:**

> *"The **Nemotron-Pretraining-Specialized-v1** collection of datasets is governed by the **Creative
> Commons Attribution 4.0 International License (CC BY 4.0)**, except for the
> Nemotron-Pretraining-Wiki-Rewrite and Nemotron-Pretraining-Scientific-Coding subsets, which are
> governed by the Creative Commons Attribution-ShareAlike 4.0 International License (CC BY-SA 4.0) and
> the GNU Free Documentation License Version 1.3 (GFDL)."*

- ✅ **`Math-Textbooks` is in the CC-BY-4.0 majority, NOT in the ShareAlike exception.**
  Registry string: **`CC-BY-4.0`**, `share_alike: false`.
- ✅ **NO share/redistribution restriction on the DATA.** The NVIDIA Data Access Agreement (the §2.2.2
  instrument) governs *"Nemotron-CC-Code-v1, Nemotron-CC-v2.1, Nemotron-Pretraining-Code-v2"* — **this
  collection is explicitly carved out of it.** ✅ **This is the material difference from row 1/2 and it
  is why this source is safe where Nemotron-CC-Math is blocked.**
- ⚠️ **A downstream obligation on the WEIGHTS, not the corpus.** CARD, verbatim: *"If this dataset is
  used to create, train, fine-tune, or otherwise improve an AI model, which is distributed or made
  available, such AI model **may be subject to redistribution and use requirements in the Qwen License
  Agreement, the DeepSeek License Agreement, and the Phi-4 license agreement**."* **Three model
  licenses attach to our weights through this one 3.0B row.** Record it; it is not a blocker but it is
  not nothing.
- Generators (CARD, for `Math-Textbooks` specifically): **Qwen3-30B-A3B, Qwen3-235B-A22B.**

#### Traps

1. 🛑 **NEVER unescape `\n`** — MEASURED on real bytes of this repo: `\neq`, `\nabla` appear inside
   LaTeX. Math is `$…$` / `$$…$$` in Math-Textbooks (with non-canonical spaces inside the delimiters,
   `$ P(u) $`) and `\(…\)` in RQA — **the delimiter convention differs BETWEEN configs of one repo.**
2. 🛑 **`neutralize_boundary_markers()` anyway.** Prior wave MEASURED **zero** markers in ~544 KB
   across 3 configs — real but far too small to prove absence at the ~1/2,500 rate that broke five
   live bundles. Phi-4 is in the repo's generator list and its `<|endoftext|>` **is** dolma2's 100257.
3. 🛑 **DO NOT DRAW `Nemotron-Pretraining-STEM-SFT`** — MMLU-contaminated, seeded from GSM8K/MATH/AOPS
   train splits, reformatted MMLU-style, zero decontamination, and MEASURED as literal MC-shaped text.
   **It is in the same repo and one config name away.** `Math-Textbooks` is the safe config.
4. **`Nemotron-Pretraining-RQA`'s card figure of 134.6B is inflated** — prior wave measured **31.7B
   unique**. Carried here because it means **this repo's card token figures include repetition**, so
   the 25.1B for Math-Textbooks may too. Another reason to keep `pool_tokens: null`.
5. **Truncation: UNVERIFIED.** No `finish_reason` column (confirmed: not among the 5 leaves).
6. **Not pre-tokenized.** Parquet text.

#### Registry row

```json
{
  "key": "nemotron-math-textbooks",
  "category": "synthetic",
  "source_label": "nemotron-math-textbooks",
  "repo": "nvidia/Nemotron-Pretraining-Specialized-v1",
  "config": "Nemotron-Pretraining-Math-Textbooks",
  "revision": "9ed3718b5f2ae29074c5e34e64115432b7c4320f",
  "file_format": "parquet",
  "text_column": "text",
  "id_column": "uuid",
  "domain_column": null,
  "license": "CC-BY-4.0",
  "share_alike": false,
  "target_tokens": 3000000000,
  "pool_tokens": null
}
```

⚠️ **`category: "synthetic"` follows report §4's own footnote** (*"Nemotron Math-Textbooks 0.3% —
which §4 lists as its own row, not as synthetic"* — i.e. the report's **bucket** counts it as synthetic
even though its **table row** is separate). **Putting it in `math` instead would change the category
totals in the registry's `categories` block. Pick one and make the two documents agree.** FLAGGED.

---

### Row 7 — pre-1929 books ✅ **THE REPO EXISTS AND IS NOW NAMED**

`source-encoding-audit.md` §8d recorded this as *"the brief names a source that our registry does not
contain… the exact repo id is UNVERIFIED and must be resolved before ingest. **That choice is the
blocker.**"* **The blocker is cleared.**

**Repo: `common-pile/pre_1929_books_filtered`**
**Pinned revision: `23f9d96dbb1db3324bbc9fbfe1f8299cc799c4d1`** (lastModified 2025-06-06T03:58:46Z).
Ungated. **MEASURED — the repo resolves, the tree lists, and I read its real bytes.**

⚠️ **Correction to §8d's candidate list: `common-pile/gutenberg_filtered` returns HTTP 404 — IT DOES
NOT EXIST.** MEASURED 2026-08-08. Do not put it on a candidate list again. `library_of_congress_filtered`
(`56725c7aa1bb320703e22eb5f42903173d5bac3d`) and `biodiversity_heritage_library_filtered`
(`0486ed637d0d7aaff264bc77fe21a7444e0215cd`) **do** exist if more public-domain volume is ever wanted.

#### Structure — MEASURED from the tree

**26 `.json.gz` files at the REPO ROOT** (no `data/` prefix, no config), named
**`public_library_1929_dolma-NNNN.json.gz`**, totalling **16,585,052,788 compressed bytes**.

🛑 **The filename prefix is `public_library_1929_dolma-`, which is NOT derivable from the repo name.**
This is the same trap the registry already records for the other Common Pile rows
(`stackv2_edu_filtered` ships `stack-edu-*`, `github_archive_filtered` ships `gharchive-dolma-*`) —
**and this one is a THIRD unrelated spelling. List the tree at the pinned revision; never guess it.**
It belongs in `_common_pile_file_prefix` alongside the other seven.

#### Schema — MEASURED from real decompressed bytes (ranged GET + `zlib.decompressobj`)

Read the first 6,000,000 compressed bytes of `public_library_1929_dolma-0000.json.gz` and decompressed
**49 complete documents**. No full download.

**Top-level JSON keys: `added`, `id`, `metadata`, `source`, `text`** — standard Dolma format.

- ✅ **`text_column` = `text`.** Exactly one text-bearing key; **no second candidate anywhere in the
  object**, so no FinePhrase-class trap. Grade: **MEASURED in bytes**.
- ✅ **`id_column` = `id`.** MEASURED: values are Internet Archive item identifiers, e.g.
  `askaroskassiscop00deleiala`, `taxationarticles00bemarich`, `proceedingsofcon00roya`.
  **Unique across all 49 sampled documents.**
- **`domain_column`: `None` recommended.** `source` is a top-level key but is constant
  (`'public_library'` on every sampled document), and the rich `metadata` fields (`author`, `title`,
  `year`, `place`, `language`) are bibliographic, not domain labels.
- **`file_format` = `json.gz`** — matches the existing Common Pile rows exactly.
- **Rich metadata worth knowing exists:** `metadata.{author, hathi_url, htid, ia_ark_id, ia_url,
  language, license, place, provenance, text_file_url, title, year}`.

#### License — MEASURED per-document, and it is the cleanest in the whole corpus

- **`metadata.license` = `'Public Domain'`** on the sampled documents (a real per-row column, like
  StackExchange's — better than a card assertion).
- **Card provenance:** *"Books published in the US before 1929 passed into the public domain on
  January 1, 2024."* Identified via HathiTrust Hathifiles; **OCR plain text downloaded from the
  Internet Archive.**
- Registry string: **`Public Domain`** — matching the exact string the registry already uses for
  `ubuntu_irc_filtered`. **`share_alike: false`.**
- ⚠️ **The card's own caveat, verbatim:** *"license laundering and inaccurate metadata can cause us to
  erroneously assign the incorrect license to some documents."* Same disclaimer as every Common Pile
  repo. Record it; it is not a blocker.
- ✅ **This row is `share_alike: false`, and that MATTERS**: `finewiki`, the other half of the
  reference category, is **100% share-alike under two regimes** (CC-BY-SA-4.0 AND GFDL). Adding a
  public-domain source is the only way the reference category stops being wholly copyleft.
  `artifacts/sizing-revised.md:60` anticipated exactly this pairing.

#### 🛑 The OCR hazard prediction was OVERSTATED — MEASURED, and I am correcting it

§8d predicted *"the worst case in the whole corpus: long-s (ſ), broken ligatures, hyphenation at every
line break, page numbers and running heads, Gutenberg boilerplate ×60k."*

**MEASURED across 49 documents (~2.5 MB of text):**

| marker | occurrences |
|---|---|
| long-s `ſ` | **0** |
| `ﬁ` / `ﬂ` ligatures | **0** |
| end-of-line hyphenation `-\n` | **0** |
| "Project Gutenberg" boilerplate | **0** |

**Zero on all four.** Two of those predictions were structurally wrong, not just unobserved:
✅ **This is HathiTrust/Internet Archive, NOT Project Gutenberg — so there is no Gutenberg boilerplate
to strip, by construction.** And the *"`_filtered`"* suffix means Common Pile's quality filter already
ran (the raw sibling `common-pile/pre_1929_books` is 137,127 docs / 73.8 GB versus this one's
124,898 docs / 46.3 GB — **the filter dropped 8.9% of documents and 37% of the bytes**, which is a
heavy filter and plausibly where the OCR sludge went).

⚠️ **What this does NOT prove.** n=49 documents from the HEAD of one of 26 files. The prior wave's own
warning applies — head sampling on clustered data was wrong by 10× once. **Long-s specifically is a
pre-1800 typographic feature and these are pre-*1929* books, so most of the corpus is late-19th/early-
20th century where `ſ` genuinely should not appear** (DERIVED — this is why I believe the zero rather
than merely reporting it). **Downgrade the hazard from "worst case" to "expected mild"; do not
downgrade it to "clean."** The encoding receipt should still count these.

Also MEASURED: first-character histogram over 49 documents is `P`×14, `T`×7, `I`×5, `E`×3, `C`×3,
`"`×2 — **no leading-whitespace documents.** The Cosmopedia `lstrip()` fix is **not** needed here, and
this is one more reason it must be per-source.

#### Sizing — DERIVED, and the reference category has room

Card statistics for the filtered version: **124,898 documents / 46.3 UTF-8 GB.**
At dolma2 English prose ~4.31 bytes/token: **46.3e9 / 4.31 ≈ 10.7B tokens** (DERIVED).
⚠️ **Grade the 46.3 GB as CARD.** Common Pile card figures need care — memory records that the
Common Pile "tokens" column is `GB × 0.25` arithmetic — **but this card gives GB, not tokens, and GB
is a measurable quantity, so it is the safer of the two.** *Settling job:* gzip ISIZE across the 26
files gives exact uncompressed bytes for ~26 ranged reads (the `_gzip_isize.py` trick already in
`artifacts/recount/`) — **and `.json.gz` DOES have an ISIZE trailer, unlike the dolma3 `.zst`.**

**Context:** report §3 gives *"reference — Wikipedia + pre-1929 books | 1% | 9.0B | 26.2B pool"*.
`finewiki` is 8.87B MEASURED, so pre-1929 books must supply the remaining ~17B of that 26.2B claim.
**My DERIVED 10.7B does not reach it.** Either the 26.2B includes another public-domain source, or it
is optimistic. 🛑 **FLAGGED: the reference pool figure does not reconcile from the two named sources
(8.87 + 10.7 = 19.6B, not 26.2B). Check the denominator before treating either number as stale.**

#### Traps

1. **Filename prefix `public_library_1929_dolma-` is not derivable from the repo name** — list the tree.
2. **OCR: expected mild, not absent.** n=49 head sample; count `ſ`, `ﬁ`/`ﬂ`, `-\n` on the encoding
   receipt rather than assuming.
3. 🛑 **Documents are ENORMOUS — this is the real risk on this row and nobody has flagged it.**
   MEASURED: document 1 is **665,094 characters** (~154k dolma2 tokens). At 25,001,984 tokens/shard,
   **one book is ~0.6% of a shard**, and 124,898 books is only ~124k documents for ~10.7B tokens
   (~86k tokens/document average). Consequences: (a) **mixture precision** — memory records that
   shards-per-component sets mixture error, and a source whose documents are 4 orders of magnitude
   larger than a web page will quantize badly; (b) the EOS fraction is *very* low, the opposite of the
   `ubuntu_irc` failure mode, so `FAMILY_MAX_EOS_FRACTION` is not at risk here; (c) any per-document
   memory buffer in the tokenize path must tolerate a 665 KB string.
4. **Pre-1929 text is factually STALE for reference/encyclopedic purposes** — `sizing-revised.md:61`
   makes this point: half the pool would be *"pre-1929 books answering a different question than
   'encyclopedia'."* Not a defect, but it means this is **not** a Wikipedia substitute.
5. **A raw sibling exists** (`common-pile/pre_1929_books`, sha `a158135b4765f01d321b5cbe1ff90ebbd354b00f`,
   77 `.jsonl.gz` in `data/documents/`, 137,127 docs / 73.8 GB). **Note `.jsonl.gz` and a `data/`
   prefix — a different layout from the filtered version. Use the filtered one.**
6. **Boundary markers: expect zero** (pre-1929 human text, no LLM in the pipeline). Run the scan; it is
   the one source where a `<|endoftext|>` hit would mean something is badly wrong upstream.

#### Registry row

```json
{
  "key": "pre-1929-books",
  "category": "reference",
  "source_label": "pre-1929-books",
  "repo": "common-pile/pre_1929_books_filtered",
  "config": null,
  "revision": "23f9d96dbb1db3324bbc9fbfe1f8299cc799c4d1",
  "file_format": "json.gz",
  "text_column": "text",
  "id_column": "id",
  "domain_column": null,
  "license": "Public Domain",
  "share_alike": false,
  "target_tokens": 0,
  "pool_tokens": null
}
```

⚠️ **`target_tokens` left at 0 (= RESERVE) deliberately.** Report §3 gives the reference category a
combined 9.0B across Wikipedia **and** pre-1929 books without splitting it, and `finewiki`'s existing
row already claims 8.8B of it. **Setting a number here without resolving that overlap would silently
over-draw the category.** The split is an owner decision. FLAGGED.

⚠️ **`source_label: "pre-1929-books"` matches `SAFE_SEGMENT_RE`** — verified:
`re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")` (`src/edullm_data/manifest.py:788`) accepts it because the
digits `1929` form a valid segment. (For the record I checked every label in this file against that
regex; the only string that FAILS is `4plus_MIND`, which is a config name, not a label — underscores
are illegal in a segment. **Never derive a `source_label` from a Nemotron config name.**)

---

### Row 4 — reasoning traces / worked examples, 8.0B 🛑 **NO REPO IS NAMED. THIS IS A FREEZE BLOCKER.**

**I searched and the answer is: nobody has chosen a repo for this row. Saying so plainly, as
instructed, rather than filling it with a plausible guess.**

#### What I searched, so the negative result is checkable

| searched | result |
|---|---|
| `docs/FINAL-DATASET-REPORT.md` | **3 mentions, ZERO repo names.** §4's table row is literally `reasoning traces / worked examples \| 8% \| 8.0B \| ~50B \| 0.16`. §7 gives the *ablation* (−1.5 MMLU / +8.4 GSM8K / +7.3 Minerva / +11.6 HumanEval) and §1 lists the category. **No repo, no config, no revision, anywhere.** |
| `docs/IMPLEMENTATION-PLAN.md` | zero repo names for this row |
| `docs/BUILD-DEPENDENCY-GRAPH.md`, `docs/TASKS.md` | **zero mentions of "reasoning trace" at all** |
| `artifacts/reservoir/corpus-registry.json` | no row, and no `reasoning` category |
| `artifacts/reservoir/WEEK1-CORPUS-SURVEY.md` | 🛑 **does NOT survey ~35 corpora** — see the correction below |
| `artifacts/recount/` | no artifact for any reasoning corpus |
| `artifacts/impl-plan/source-encoding-audit.md` §13 | **the only place candidates are listed at all** — explicitly as *"candidate repos"*, and it says outright *"None of these is in our registry or `artifacts/recount/`, so every size figure here is CARD or UNVERIFIED."* |

**Conclusion: the 8.0B row (0.8% of the 1.0T corpus) has a share, a token target, a pool estimate
(`~50B`, itself ungrounded) and an ablation result — but NO SOURCE.** Per instruction: **an unnamed
8.0B source is a FREEZE BLOCKER and this is the finding.**

#### 🛑 A correction to my own tasking, recorded because the next agent will be told the same thing

My brief said `artifacts/reservoir/WEEK1-CORPUS-SURVEY.md` *"surveys ~35 corpora and is the best
index."* **It does not.** MEASURED: the file is 195 lines and is a **code-reuse audit of the sibling
`pipelines/week1_corpus` checkout** — packers, S3 backends, decontamination, nine "traps worth
stealing." Grepping it for `reasoning`, `trace`, `gutenberg`, `books`, `1929`, `cosmopedia`,
`nemotron` returns **zero hits for all of them.** It surveys **modules, not corpora.** It is a genuinely
useful document; it is not a corpus index. **The best corpus index in this repo is
`artifacts/impl-plan/source-encoding-audit.md` (1,757 lines, §14 is a one-row-per-source table).**

#### The two viable candidates, both MEASURED today, so the decision is cheap

**Candidate A — `nvidia/Nemotron-Pretraining-Specialized-v1`, config `Nemotron-Pretraining-InfiniByte-Reasoning`**

Footer MEASURED at the pinned sha `9ed3718b5f2ae29074c5e34e64115432b7c4320f`
(`part_000000.parquet`, 1,412,348,603 bytes, 50,000 rows, 1 row group, `created_by: Polars`):

| `path_in_schema` | uncompressed bytes | num_values |
|---|---|---|
| **`text`** | 3,273,425,291 | 50,000 |
| `metadata.category` | 2,250,359 | 50,000 |
| **`uuid`** | 2,000,223 | 50,000 |
| `metadata.models_used` | 550,052 | 50,000 |
| `license` | 83 | 50,000 |

✅ **Identical 5-leaf schema to Math-Textbooks** — `text_column` = `text`, `id_column` = `uuid`, flat,
no trap. ✅ **Same clean `CC-BY-4.0`**, ungated, and **explicitly carved out of the NVIDIA Data Access
Agreement** (it is in the Specialized-v1 collection, not the CC-Code/CC-v2.1/Code-v2 group).

**Sizing, DERIVED:** 30 files, **28,345,959,943 bytes** total (tree MEASURED).
`3,273,425,291 / 1,412,348,603 = 2.31772` text-uncompressed per parquet byte ⇒
**~65.7 GB of text** ⇒ **15.2–18.6B dolma2 tokens** at 4.31–3.53 bytes/token.
Implied 3.386 bytes/token against the card's 19.4B — **dense, consistent with math/code-heavy reasoning
traces.** Against an 8.0B target that is **~2× headroom.** Thin but workable.

⚠️ **A row-count discrepancy I could not close, flagged rather than hidden.** The prior wave recorded
**1,478,301 rows** from `/size`. File 0 has **50,000 rows** in 1,412,348,603 bytes; scaling by total
bytes implies **~1,003,504 rows**, not 1,478,301 — a **32% gap**. Either row counts vary a lot across
the 30 files (file 0 is one row group of exactly 50,000, which smells like a fixed chunk size, so
later files may differ) or one of the two figures is wrong. **DERIVED, unresolved. It does not change
the byte-based token estimate, which does not depend on row counts.**

MEASURED: mean **65,465 text bytes/document** — these are *very* long documents (~16k tokens each),
consistent with full reasoning traces.

**Candidate B — the dolma3 reasoning directories** (same repo as row 3, so it is nearly free)

MEASURED today at sha `f23aa129fda8335ba9760057bcc1f0c02f3d068b`, ingredient1:

| directory | files | compressed bytes | card category |
|---|---|---|---|
| `data/ingredient1-general_reasoning_mix` | 529 | 5,830,467,832 | Thinking (synth) |
| `data/ingredient1-omr-rewrite-fullthoughts` | 21 | 2,405,074,795 | Thinking (synth) |
| `data/ingredient1-code-meta-reasoning` | 50 | 447,601,056 | Thinking (synth) |
| `data/ingredient1-math-meta-reasoning` | 36 | 348,561,677 | Thinking (synth) |
| `data/ingredient1-program_verifiable` | 9 | 102,518,829 | Thinking (synth) |
| **total** | **645** | **9,134,224,189** | |

DERIVED at ~3.0–3.5× zstd and ~4.0 bytes/token: **~6.9–8.0B tokens.** 🛑 **That BARELY reaches the
8.0B target — call it 1.0 epoch, and the report's stated `0.16` epochs is far off if this is the
source.** ✅ The card's own `Thinking (synth)` category label makes the selection unambiguous, which is
the same gift as row 3's QA selection.

#### Recommendation

**Take Candidate A (`InfiniByte-Reasoning`) as the primary, optionally topped up with Candidate B.**
Reasons, in order:

1. **Volume.** A alone gives ~2× headroom; B alone gives ~1.0 epoch, which violates the spirit of the
   report's own ≥3× peak-demand convention.
2. **A has a real `uuid`**; B's `id` is a declared-but-unread JSON key.
3. **A is parquet** (footer-checkable, already-supported reader); **B is `.jsonl.zst`, which
   `corpus_read` cannot read today.**
4. **Independence.** If the QA row already draws 14.0B from the dolmino mix, sourcing reasoning from
   the *same* mix concentrates two of stage 2's rows in one upstream artifact — the concentration
   failure the report warns about for math.
5. NVIDIA's scrubbing on this repo MEASURED clean at small n (zero `<think>`/`</think>`, zero
   `<|endoftext|>` in ~544 KB); **AI2's is UNVERIFIED on the same question.**

🛑 **Both remain EXPLICITLY REJECTED: the raw upstream SFT repos** — `open-thoughts/OpenThoughts2-1M`,
`open-r1/OpenR1-Math-220k`, `nvidia/Llama-Nemotron-Post-Training-Dataset`. Their text lives in
`messages[].content`, **the FinePhrase nested-leaf trap in its purest form**, plus a role-scaffolding
decision we would have to make ourselves. Recorded here so nobody "discovers" them later.

#### Provisional registry row — **DO NOT FREEZE WITHOUT AN OWNER DECISION**

```json
{
  "key": "reasoning-traces",
  "category": "reasoning",
  "source_label": "reasoning-traces",
  "repo": "nvidia/Nemotron-Pretraining-Specialized-v1",
  "config": "Nemotron-Pretraining-InfiniByte-Reasoning",
  "revision": "9ed3718b5f2ae29074c5e34e64115432b7c4320f",
  "file_format": "parquet",
  "text_column": "text",
  "id_column": "uuid",
  "domain_column": null,
  "license": "CC-BY-4.0",
  "share_alike": false,
  "target_tokens": 0,
  "pool_tokens": null
}
```

⚠️ **`target_tokens: 0` (RESERVE) is deliberate and is the honest state.** The identity strings are
MEASURED and ready; **the SOURCE CHOICE is not made.** Writing `8000000000` here would convert an
unmade decision into a fact the build would silently execute.

⚠️ **`category: "reasoning"` DOES NOT EXIST in the registry's `categories` block** (the eight are
`academic`, `code`, `edu-web`, `math`, `qa-forum`, `reference`, `synthetic`, `web-diverse`). Adding
this row requires **either a ninth category or a decision to fold it into `synthetic`.** Report §7 is
emphatic that QA and reasoning must NOT be blended because they move the metrics in opposite
directions — **which argues for a distinct category, not a fold.** FLAGGED.

⚠️ **The report's `~50B` pool figure for this row has no derivation anywhere.** Neither candidate
approaches it (A ≈ 15–19B, B ≈ 7–8B; both together ≈ 22–27B). **Treat `~50B` as UNVERIFIED and
probably optimistic — the `0.16` epochs it implies is not achievable from any source now on the table.**

