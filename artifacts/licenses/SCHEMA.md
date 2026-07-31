# `_licenses.parquet` — schema and rationale

**Status:** proposed design, Phase 0 task B. Companion data: `openstax-books.json`,
`libretexts-distribution.json` (both in this directory).

**These are research findings, NOT legal advice.** Every license string here is transcribed from a
publisher's or dataset's own machine-readable metadata. Nothing in this file is an opinion on what use
is permitted, and one measured finding (§6) is that two upstream sources *disagree* with each other.

---

## 1. What this file has to answer

The driving query from `DATASET-DESIGN-reservoir.md` §7 item 2:

> *"Which documents in this reservoir are non-commercial-only?"*

Restated as SQL, this is the acceptance test for the schema:

```sql
SELECT shard_path, doc_index, doc_uri, license_id
FROM   '_licenses.parquet'
WHERE  commercial_use = 'denied';
```

Two secondary queries must also work, because they are the ones that will actually get asked next:

```sql
-- Share-alike exposure (design §7 item 4 currently names only FineWiki + StackExchange;
-- LibreTexts is a measured THIRD share-alike source at 32.0% of its rows)
SELECT source, count(*) FROM '_licenses.parquet'
WHERE share_alike = TRUE GROUP BY source;

-- Attribution roster for a public release
SELECT DISTINCT license_id, attribution_name, attribution_url FROM '_licenses.parquet';
```

---

## 2. The honest finding first: **there is no join key today**

This is the part the design needs to hear, so it goes before the column list.

`_licenses.parquet` is a table of documents. The reservoir does not contain documents. It contains
**headerless raw `uint32` token shards** (`DATASET-DESIGN-reservoir.md` §2):

```
tokens/<source>[/<domain>]/<split>-<NNNNN>.u32le.bin
  dtype uint32 · little-endian · header_bytes 0 · container raw
```

Verified against the code, three independent ways:

| where I looked | what it says |
|---|---|
| `src/edullm_data/manifest.py:208-221` | A `ManifestEntry` is `path` / `sha256` / `bytes` / `count` / `format` / `split` / `labels`. The finest addressable unit in the manifest is **one shard object**. There is no document row anywhere. |
| `entry.labels` | Constrained to a flat `dict[str,str]` **that must equal `labels_from_path(entry.path)` exactly** (design §1.3, verified: any extra key → violation `labels-contradict-path`). So labels cannot carry a per-document or even a per-book license. |
| the payload bytes | Tokenized text. Documents are concatenated and separated only by an EOS token; the document→book→license association is **destroyed by tokenization** unless something records it at build time. |

So: **no existing field joins a license row back to a document.** Not a gap in the parquet design — a
gap in the corpus. Recording it is a **build-time** obligation and is **not backfillable** after
tokenization, because the information no longer exists in the artifact.

### 2.1 The key I propose, and what it costs

`(shard_path, doc_index)` — the shard's dataset-relative key, plus the document's 0-based ordinal
within that shard.

It works because document boundaries *are* recoverable from a `.u32le.bin` shard without a sidecar.
Design §2 (citing `utils.py:193-197`): for a **local** path with a known `eos_token_id` and dtype,
boundaries come from `(mmap == eos_token_id).nonzero()[0]` on a headerless raw memmap. So a consumer
can enumerate `doc_index` values from the bytes and join, with no new payload objects and nothing
added to the hash chain.

Three caveats, stated plainly:

1. **The tokenizer must emit it.** `doc_index` is only meaningful if the build records, per document,
   which shard it landed in and at which ordinal. Nothing in the pipeline does this today. **This is
   the one action item that must precede the first publish** of any license-tracked source — after
   tokenization it is unrecoverable.
2. **It is ordinal, so it is fragile.** Re-shard, re-tokenize, or change EOS handling and every
   `doc_index` shifts. Mitigated by `shard_sha256` (§3) — a stale row is then *detectable* rather than
   silently wrong. This is the same class of hazard as the ordinal-REUSE contradiction already known
   in this repo's migration work.
3. **EOS-derived boundaries assume exactly one EOS per document boundary and no EOS inside a
   document.** True for the planned build (design §2: "EOS must be in your bytes"), but it is an
   assumption the reader inherits, not a guarantee the format enforces.

`doc_uri` (§3) is carried alongside as a **stable, non-ordinal secondary key** — the upstream
identifier, which survives re-sharding even though it cannot be derived from the bytes. For
LibreTexts this is verified to exist and to be naturally unique: the dataset's top-level `id` column
*is* the page URL (e.g.
`https://math.libretexts.org/Bookshelves/PreAlgebra/...`), and `metadata.provenance` additionally
pins the source file and record offset (`libretexts-0000.json.gz:1`).

---

## 3. Columns

One row = one document. Grain is deliberately per-document, not per-book, because the acceptance query
in §1 asks for *documents* and because a single shard interleaves many books.

### Join / identity

| column | type | null? | meaning |
|---|---|---|---|
| `shard_path` | `string` | no | Dataset-relative key of the token shard, byte-identical to `ManifestEntry.path` (e.g. `tokens/libretexts/math/train-00042.u32le.bin`). Joins to the manifest. |
| `doc_index` | `int32` | no | 0-based ordinal of the document within `shard_path`, in EOS order. With `shard_path`, the primary key. |
| `shard_sha256` | `string` | no | The declared `sha256` of `shard_path` at the time this row was written. **Staleness detector:** if it no longer matches the manifest, `doc_index` is not trustworthy. |
| `doc_uri` | `string` | yes | Stable upstream document identifier — the source URL or dataset `id`. Survives re-sharding. Null where upstream ships no id. |
| `upstream_provenance` | `string` | yes | Upstream file+offset (e.g. `libretexts-0000.json.gz:1`). Lets a row be re-derived from the source corpus. |
| `token_count` | `int32` | yes | Tokens in this document, incl. its EOS. Lets a query weight license exposure **by tokens, not row count** — the number that actually matters for a mix. |

### Provenance of the work

| column | type | null? | meaning |
|---|---|---|---|
| `source` | `string` | no | Reservoir source label, matching the `<source>` path segment / `entry.labels.source`. |
| `collection` | `string` | yes | Upstream collection (`openstax`, `libretexts`, …). Distinct from `source` because one reservoir source may fuse several collections. |
| `work_title` | `string` | yes | Book/page title. |
| `work_id` | `string` | yes | Stable upstream work id — e.g. OpenStax `book_uuid`, LibreTexts `book_url`. Groups pages belonging to one book. |
| `author` | `string` | yes | Declared author/attribution name. |

### The license itself

| column | type | null? | meaning |
|---|---|---|---|
| `license_raw` | `string` | yes | **Verbatim, unnormalized** upstream string. Never parsed, never cleaned. The audit trail: if normalization is wrong, this is what proves it. |
| `license_id` | `string` | yes | Normalized SPDX-style identifier: `CC-BY-4.0`, `CC-BY-NC-SA-4.0`, `CC-BY-SA-3.0`, `GFDL-1.3-or-later`, `public-domain`, or `UNKNOWN`. |
| `license_family` | `string` | yes | `CC`, `GFDL`, `public-domain`, `other`, `unknown`. **Not a CC-only enum — see §5.** |
| `license_version` | `string` | yes | `4.0`, `3.0`, `2.5`, … **Separate from the variant on purpose:** LibreTexts carries `by-sa` at 4.0, 3.0 *and* 2.5, and version-specific obligations are unqueryable if version is glued into one string. |
| `license_url` | `string` | yes | Canonical deed URL. |
| `commercial_use` | `string` | no | **`allowed` / `denied` / `unknown`.** The §1 acceptance query reads this column. Three-valued, never boolean — see §4. |
| `share_alike` | `boolean` | yes | Copyleft/SA obligation present. Feeds design §7 item 4. |
| `attribution_required` | `boolean` | yes | True for every CC variant except CC0. |
| `attribution_name` | `string` | yes | Who to credit. |
| `attribution_url` | `string` | yes | Where to point the credit. |

### Trust in the license claim (the columns §6 exists to justify)

| column | type | null? | meaning |
|---|---|---|---|
| `license_authority` | `string` | no | **Who asserted this:** `publisher-api` (the rights holder's own API), `aggregator-metadata` (a redistributor's field, e.g. `metadata.license`), `corpus-card` (dataset-level card), `inferred`. |
| `license_source_url` | `string` | yes | The exact URL the assertion was read from. Makes any row re-checkable. |
| `license_asserted_at` | `timestamp[s]` | yes | When it was read. Licenses get re-versioned; a claim without a date cannot be aged out. |
| `license_conflict` | `boolean` | no | True when ≥2 authorities disagree for this document. **Measured non-zero — §6.** |
| `license_conflict_note` | `string` | yes | Human-readable description of the disagreement. |
| `verified` | `boolean` | no | True = transcribed from a machine-readable authority. False = inferred/defaulted. Keeps the VERIFIED/INFERRED split queryable instead of buried in prose. |

### Suggested physical layout

Partition by `source`, sort by `(shard_path, doc_index)`. `zstd`. One row per document across a ~200B
reservoir is large; partitioning by `source` keeps the common single-source query from scanning it all.

---

## 4. Why `commercial_use` is three-valued

A boolean has no way to say *"nobody has checked."* That is the state most rows are in on day one, and
it is the state that silently becomes `false`→"fine" under a boolean. Encoding `unknown` explicitly
makes the safe query trivially correct and self-documenting:

```sql
-- everything NOT provably clear for commercial use
WHERE commercial_use <> 'allowed'
```

`denied` means an NC clause was actually observed. `unknown` means no authority was read, or the
authorities conflict. Conflating those two is exactly the failure this file exists to prevent.

---

## 5. Why `license_family` is not a CC-only enum

The design doc's premise is that these collections are "Creative Commons throughout." For OpenStax
that is **confirmed** (128/128 resolvable books are CC; zero non-CC). For LibreTexts it is
**measured false**: 1,948 of 40,049 rows (**4.86%**) are not CC at all — 1,191 `Public Domain` and 757
`GNU Free Documentation License`.

Those counts are exact, not sampled (server-side `/filter` aggregation; the seven distinct values sum
to exactly 40,049 and the residual query returned 0 rows, which proves the enumeration is complete).

So a schema that modeled the license as a CC variant enum would mis-parse ~5% of LibreTexts on the
first ingest. Hence `license_family` admits `GFDL` / `public-domain` / `other`, and `license_raw`
always retains the original string.

Corollary worth flagging to the design: **`libretexts_filtered` contains zero NC rows** (verified —
`LIKE '%by-nc%'` → 0, `LIKE '%NonCommercial%'` → 0; Common Pile filtered NC out upstream). Taken
alone, that would make per-page license tracking look unnecessary for this corpus. §6 is why it is
not.

---

## 6. The measured conflict that justifies `license_authority`

7,121 LibreTexts rows (17.8%) are attributed to `OpenStax`. LibreTexts declares **6,974 of them
`CC BY 4.0`** — no NC clause.

OpenStax's own APIs declare **73 of its 84 live books (86.9%) as `CC BY-NC-SA 4.0`.**

Per-title spot checks, exact row counts, LibreTexts' claim vs the publisher's:

| LibreTexts URL pattern | rows | LibreTexts says | OpenStax says |
|---|---|---|---|
| `U.S._History_(OpenStax)` | 138 | CC BY 4.0 | CC BY-NC-SA 4.0 |
| `Pharmacology_for_Nurses_(Openstax)` | 327 | CC BY 4.0 | CC BY-NC-SA 4.0 |
| `Nutrition_for_Nurses_(OpenStax)` | 178 | CC BY 4.0 | CC BY-NC-SA 4.0 |
| `Psychology` | 639 | CC BY 4.0 | CC BY-NC-SA 4.0 |
| `American_Government` | 93 | CC BY 4.0 | CC BY-NC-SA 4.0 |

**1,375 rows** from five probes — a **non-exhaustive lower bound**, upper-bounded by the 6,974
OpenStax-attributed CC BY rows. Not every such row is a genuine conflict: some OpenStax books really
are CC BY (1st-edition *College Physics* is CC BY 4.0 while *College Physics 2e* is CC BY-NC-SA 4.0),
so resolving the true figure needs per-title **edition** matching, which was not done here.

Two consequences for the schema:

1. **A single `license` column is not sufficient.** It would have to pick a winner between two
   disagreeing authorities and would silently return "commercially clean" for content whose rights
   holder asserts NC. `license_authority` + `license_conflict` keep the disagreement *visible*, and
   let a conservative query take the strictest claim.
2. **`publisher-api` should outrank `aggregator-metadata`** when computing `commercial_use`. Rule:
   if any authority says `denied`, the row is `denied`; if authorities conflict on anything else,
   `unknown` with `license_conflict = TRUE`.

Which party is legally correct is **not** resolved here. The finding is only that the two declarations
disagree, and that the disagreement is machine-detectable — which is precisely what makes it worth
storing.

---

## 7. Blocker: `_licenses.parquet` will be REJECTED as currently named

Verified by executing the validator, not by reading it:

```
$ python3 -c "from edullm_data.validate import _is_control_key; ..."
False  _licenses.parquet
False  _licenses/licenses.parquet
False  _dedup/clusters.parquet          # <-- the design's OWN §1.3 recommendation
 True  _catalog/x.json
 True  README.md
 True  tokens/manifest.json
```

`validate.py:139-152` exempts only these control **basenames** — `dataset.json`, `manifest.json`,
`_SUCCESS`, `_VALIDATED.json`, `_REJECTED.json`, `README.md` (at depth 0) — and these control
**prefixes**: `_catalog/`, `dependents/`. A leading underscore buys nothing; the set is a closed
allowlist.

Anything else under the dataset prefix that is not in a group manifest raises
`unlisted-object-dataset-level` (`validate.py:624-631`):

> *"is under the dataset prefix but is in no group's manifest… a globbing reader would still find it."*

So shipping `_licenses.parquet` as a control file requires **adding `_licenses/` (or the basename) to
`CONTROL_PREFIXES` / `CONTROL_BASENAMES` in `validate.py`** — a small, contained change to the
validator, but a change that must land *before* the first publish that ships this file.

**This finding generalizes beyond this task:** `_dedup/clusters.parquet`, which design §1.3 recommends
as option A specifically because it is "no Gate A risk," fails the same check. Option A is sound, but
it is **not** zero-code as written. Both control files need the same one-line allowlist change, and
they should be added together.

Do **not** work around this by hiding the file under `_catalog/` (which does pass). `_catalog/` is
bucket-root-reserved for one catalog JSON per dataset/version — `fsck.py:92` parses
`_catalog/<family>/<name>/<version>.json`, and `read.py:508` / `publish.py:546` list it to resolve
versions. Putting a parquet there would collide with the resolver.

---

## 8. Summary of what is verified vs inferred

**Verified by execution or exact server-side aggregation:**
- 129 OpenStax books enumerated (100% of `meta.total_count`); 128 licenses resolved; two independent
  APIs agree on all 116 books where both report, 0 disagreements.
- `libretexts_filtered.metadata.license` exists as a typed struct field and is populated in
  40,049/40,049 rows (0 null, 0 empty).
- Exactly 7 distinct license values, summing to 40,049 with an empty residual set.
- `_is_control_key()` returns `False` for `_licenses.parquet` and `_dedup/clusters.parquet`.
- `ManifestEntry` has no per-document field; `entry.labels` must equal `labels_from_path`.

**Inferred / proposed, not verified:**
- The `(shard_path, doc_index)` key itself — it does not exist yet and depends on the tokenizer being
  changed to emit it.
- Every column above is a proposal.
- The true size of the OpenStax↔LibreTexts conflict (bounded: ≥1,375, ≤6,974 rows).

**Not determined:** which license declaration is legally correct where they conflict. That needs a
human, and it is the one thing this file deliberately does not decide.
