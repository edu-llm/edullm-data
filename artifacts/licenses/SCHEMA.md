# `_licenses/` — schema and rationale

**Status:** proposed design, Phase 0 task B. **Revised 2026-07-31 after the owner decided §9.7 item 3
(SKIP the per-document key).** The grain is now **one reservoir `source`**, not one document. Companion
data: `openstax-books.json`, `libretexts-distribution.json` (both in this directory).

**These are research findings, NOT legal advice.** Every license string here is transcribed from a
publisher's or dataset's own machine-readable metadata. Nothing in this file is an opinion on what use
is permitted, and one measured finding (§7) is that two upstream sources *disagree* with each other.

---

## 1. What this file has to answer

The acceptance test, restated for the source-level grain:

```sql
-- "Which sources in this reservoir are non-commercial-only, and which are share-alike?"
SELECT source,
       count(*)                                      AS distinct_licenses,
       max(commercial_use = 'denied')                AS contains_nc,
       max(share_alike)                              AS contains_sa,
       -- Fractions sum only over `sole` rows: a `co-declared` row describes the SAME documents
       -- as its coverage-group siblings, so including both would double-count (FineWiki -> 200%).
       -- `contains_*` above is a max() and is unaffected, which is why the booleans are the
       -- robust half of this query and the fractions are the half with a footnote.
       sum(CASE WHEN license_relation = 'sole'
                THEN row_fraction * (commercial_use = 'denied') END) AS nc_fraction_sole,
       sum(CASE WHEN license_relation = 'sole'
                THEN row_fraction * share_alike END)                 AS sa_fraction_sole
FROM   '_licenses/sources.parquet'
GROUP  BY source
ORDER  BY contains_sa DESC, sa_fraction_sole DESC NULLS LAST;
```

Read the two pairs of columns together. `contains_nc` answers *"is any of this NC?"*; `nc_fraction_sole`
answers *"how much."* **A source-level selector can only act on the first**, so the second column is
what tells you the price — see §3. `distinct_licenses = 1` is the definition of a *uniform* source and
is deliberately **derived, not stored**, so it cannot drift from the rows it summarizes (the same
reasoning that makes the per-dataset README a derived artifact).

⚠️ **A null fraction is not zero.** A source whose `measurement` is `unmeasured` (three of §3.2's named
sources — `pubmed`, `arxiv_papers`, `algebraic-stack`) has `row_fraction` null throughout, so its
fraction column is null while `contains_sa` may still be true. Any query that reads the fraction must
say what it does with null; coalescing it to 0 turns "unknown" into "clean," which is the exact failure
§5 exists to prevent.

Two secondary queries must also work, because they are the ones that get asked next:

```sql
-- The exclusion list: source names to omit from a build_mixture call to drop all SA content
SELECT DISTINCT source FROM '_licenses/sources.parquet' WHERE share_alike;

-- Attribution roster for a public release
SELECT DISTINCT license_id, attribution_name, attribution_url
FROM '_licenses/sources.parquet' WHERE attribution_required;
```

---

## 2. The join key question is CLOSED, and the answer shapes everything below

An earlier revision of this file opened by diagnosing a gap: `_licenses.parquet` was a table of
documents, and the reservoir does not contain documents. It contains **headerless raw `uint32` token
shards** (`DATASET-DESIGN-reservoir.md` §2), whose finest addressable unit is one shard object. That
diagnosis stands, verified three independent ways:

| where I looked | what it says |
|---|---|
| `src/edullm_data/manifest.py:220-233` | A `ManifestEntry` is `path` / `sha256` / `bytes` / `count` / `format` / `split` / `labels`. The finest addressable unit in the manifest is **one shard object**. There is no document row anywhere. |
| `entry.labels` | Constrained to a flat `dict[str,str]` **that must equal `labels_from_path(entry.path)` exactly** (`validate.py:771`, `_check_labels_match_path`; verified: any extra key → violation `labels-contradict-path`). So labels cannot carry a per-document or even a per-book license. |
| the payload bytes | Tokenized text. Documents are concatenated and separated only by an EOS token; the document→book→license association is **destroyed by tokenization** unless something records it at build time. |

**The owner resolved it 2026-07-31: the tokenizer will NOT emit `(shard_path, doc_index)`.** No
per-document key exists for this dataset version. That is a decision, not an oversight, and it is
recorded at `DATASET-DESIGN-reservoir.md` §9.7 item 3 and in the §1 header table. Three consequences
this schema is built around:

1. **Per-document licensing is permanently unavailable for `v1`.** Not deferred — unrecoverable after
   tokenization, because the information no longer exists in the artifact.
2. **The join key becomes `source`**, and that is a strict *upgrade* in one respect worth stating
   plainly. `(shard_path, doc_index)` would have been an ordinal that nothing recomputes — fragile
   under re-sharding, and a producer assertion no gate falsifies. `source` is the opposite: it is the
   `<source>` path segment, Gate A **recomputes** it from each object's own key
   (`labels_from_path` → `_check_labels_match_path`), and an entry whose declared label disagrees with
   its path is rejected. Verified by execution:

   ```
   labels_from_path('tokens/dclm/train-00000.u32le.bin')                  -> {'source': 'dclm'}
   labels_from_path('tokens/stackexchange/mathoverflow/train-00001.…bin') -> {'source': 'stackexchange',
                                                                             'domain': 'mathoverflow'}
   ```

   Every shard in this reservoir sits under a `<source>` directory (§2.3), so **every** manifest entry
   carries `source`. The join is set membership on a gate-enforced label, not a lookup on an ordinal.
3. **The join is many-to-many in the wrong direction.** One `source` maps to thousands of shards and
   to *N* license rows. So the table cannot say which shard is which license — it can only characterize
   a whole source. §3 is the cost of that.

A future `v2` could still emit the key. This forecloses it for **this** version, not forever.

---

## 3. What source-level granularity costs, measured

Exclusion by omitting a `source` name from a `build_mixture` call is **conservative in the safe
direction — no SA content leaks through — but lossy.** How lossy depends on whether the source is
uniform or mixed, and both cases are measured:

| source | SA share of upstream rows | omitting the name drops | collateral (non-SA rows lost) | basis |
|---|---|---|---|---|
| `stackexchange` | **100%** (CC-BY-SA-4.0 throughout) | exactly the SA content | **0%** | row `metadata.all_licenses`, sampled n=2 |
| `finewiki` | **100%** (CC-BY-SA-4.0 + GFDL) | exactly the SA content | **0%** | card + repo tags |
| `libretexts` | **32.05%** (12,836 / 40,049) | the whole source | **67.95%** — 27,213 rows, incl. 24,205 CC BY 4.0 | exact server-side aggregation |
| `peS2o` | **≈1.92%** (120,150 / 6,254,908) | the whole source | **≈98%** — the academic pool's largest source | Common Pile paper Table 3 |

`peS2o` is the case to understand before relying on name-level exclusion: **to remove 1.9% of a source
you remove 100% of it**, and §2.1 sizes the 20B academic pool on it. `libretexts` is the same shape,
milder. Both are why §1.5's framing — "SA sources are wholly-SA *by source*" — is **true for
`stackexchange` and `finewiki` and false for `libretexts` and `peS2o`** (§10 records the reconciliation;
§1.5 in the plan now carries the qualifier).
The decision to skip the per-document key is defensible because the *precautionary* requirement §1.5
actually states is "SA remains identifiable," which a source-level table satisfies exactly. It is the
hypothetical *surgical* requirement — strip SA and keep the rest of a mixed source — that is now
foreclosed.

**Three further things this table cannot answer, by construction:**

- **Token-weighted exposure.** A shard's `count.tokens` covers a whole object, and a shard interleaves
  documents under different licenses. So license exposure is expressible in **upstream document
  counts** only, never in reservoir tokens — and tokens are the number that matters for a mix.
- **Post-pipeline fractions.** Every `row_fraction` is a property of the **upstream** corpus. §4.1's
  destructive stages run between here and the shards — URL-key dedup alone removes ~53% of documents,
  then exact-hash dedup, then whole-document decontamination removal. Any of those can shift the
  license mix of a source, and **without a join key the shift is unmeasurable.** Treat every fraction
  as ±unknown after assembly, and say so in `limitations`.
- **Embedded third-party assets.** OpenStax states that CC BY-NC-SA books may contain third-party
  material under separate terms; a per-book (let alone per-source) variant does not resolve
  embedded-asset rights.

---

## 4. Columns — `_licenses/sources.parquet`

**One row = one (source, upstream license string).** A **uniform** source has exactly one row; a
**mixed** source has one row per distinct license value, each carrying its own share. This is the whole
reason the grain is not "one row per source": a single `license_id` column cannot express
*"60.4% CC BY 4.0, 30.3% CC BY-SA 4.0, 3.0% Public Domain"* — it would have to pick a winner, and
picking a winner is how ~5% of LibreTexts gets silently mis-parsed (§6).

### Join / identity

| column | type | null? | meaning |
|---|---|---|---|
| `source` | `string` | no | The reservoir source label — byte-identical to the `<source>` path segment and to `entry.labels['source']`, which Gate A recomputes from the key. **The join key.** |
| `license_raw` | `string` | no | **Verbatim, unnormalized** upstream string. Never parsed, never cleaned. With `source`, the primary key. Two raw strings may normalize to one `license_id` — measured: 8 OpenStax books report the localized deed URL `…/licenses/by/4.0/deed.pl` in the content archive while the CMS reports plain `CC BY 4.0`. Keeping `license_raw` as part of the key is what makes that visible instead of collapsed. |
| `upstream_dataset` | `string` | yes | The repo/collection the row was measured on, e.g. `common-pile/libretexts_filtered`. Distinct from `source` because one reservoir source may fuse several upstream collections (§1.1's `synthetic-` fusion; the code pool's fallback chain). |

### The license

| column | type | null? | meaning |
|---|---|---|---|
| `license_id` | `string` | yes | Normalized SPDX-style identifier: `CC-BY-4.0`, `CC-BY-NC-SA-4.0`, `CC-BY-SA-3.0`, `GFDL-1.3-or-later`, `public-domain`, `MIT`, `Apache-2.0`, `UNRESOLVED`, `UNKNOWN`. |
| `license_family` | `string` | yes | `CC`, `GFDL`, `public-domain`, `permissive-software`, `other`, `unknown`. **Not a CC-only enum — see §6.** |
| `license_version` | `string` | yes | `4.0`, `3.0`, `2.5`, … **Separate from the variant on purpose:** LibreTexts carries `by-sa` at 4.0, 3.0 *and* 2.5, and version-specific obligations are unqueryable if version is glued into one string. |
| `license_url` | `string` | yes | Canonical deed URL. |
| `commercial_use` | `string` | no | **`allowed` / `denied` / `unknown`.** Three-valued, never boolean — see §5. |
| `share_alike` | `boolean` | yes | Copyleft/SA obligation present. Read together with `license_family`: **CC-BY-SA and GFDL are different copylefts and are not interchangeable**, so `share_alike = TRUE` alone does not tell a compliance step what to do. FineWiki carries both. |
| `attribution_required` | `boolean` | yes | True for every CC variant except CC0. |
| `attribution_name` | `string` | yes | Who to credit. |
| `attribution_url` | `string` | yes | Where to point the credit. |
| `license_relation` | `string` | no | **`sole`** — this license governs its `covers_rows` alone — or **`co-declared`**: the upstream source declares two or more licenses over the *same* content. FineWiki is the measured case (`cc-by-sa-4.0 AND gfdl`). Whether co-declared terms are conjunctive or disjunctive for a reuser is a legal question this table does not decide; it records that both were declared. |
| `coverage_group` | `string` | yes | Groups `co-declared` rows that describe the **same** documents. Load-bearing arithmetic: without it, summing `row_fraction` over FineWiki yields 200%. **The sum-to-one invariant is per coverage group, not per source:** `Σ row_fraction` over all `sole` rows plus one representative per `coverage_group` must equal 1.0. Null for `sole` rows. |

### How much of the source it covers

| column | type | null? | meaning |
|---|---|---|---|
| `covers_rows` | `int64` | yes | Upstream documents this license applies to. Null where only a fraction is known. |
| `row_fraction` | `double` | yes | `covers_rows` / the source's upstream row total. This is the column that makes a mixed source honest. Null where unmeasured — **never defaulted to 0 or 1.** |
| `upstream_rows_total` | `int64` | yes | The denominator, carried explicitly so a fraction is never a bare number with an unstated base. |

### Trust in the license claim (the columns §7 exists to justify)

| column | type | null? | meaning |
|---|---|---|---|
| `measurement` | `string` | no | **How the share was obtained**, and the single most important column for reading this file honestly: `exact-aggregate` (server-side count over the whole split, residual proven empty) · `sampled` · `paper-table` · `card-declaration` · `unmeasured`. |
| `measured_rows` | `int64` | yes | Sample size where `measurement = 'sampled'`. Keeps an n=2 probe from reading like an exact count — which matters, because `stackexchange`'s SA fact currently rests on **2 rows**. |
| `license_authority` | `string` | no | **Who asserted this:** `publisher-api` (the rights holder's own API) · `aggregator-metadata` (a redistributor's field, e.g. `metadata.license`) · `corpus-row-metadata` (per-row field read directly) · `corpus-card` · `paper-table` · `inferred`. |
| `license_source_url` | `string` | yes | The exact URL the assertion was read from. Makes any row re-checkable — the only integrity defence this file has (§8). |
| `license_asserted_at` | `timestamp[s]` | yes | When it was read. Licenses get re-versioned; a claim without a date cannot be aged out. |
| `license_conflict` | `boolean` | no | True when ≥2 authorities disagree for this source. **Measured non-zero — §7.** |
| `license_conflict_note` | `string` | yes | Human-readable description of the disagreement. |
| `verified` | `boolean` | no | True = transcribed from a machine-readable authority. False = inferred/defaulted. Keeps the VERIFIED/INFERRED split queryable instead of buried in prose. |

### Worked rows, from real Phase 0 data

`libretexts` is the mixed case and produces **7 rows** (`measurement = exact-aggregate`, denominator
40,049 for all seven, residual query proven empty):

| `license_raw` (abbreviated) | `license_id` | `family` | `covers_rows` | `row_fraction` | `share_alike` |
|---|---|---|---|---|---|
| `Creative Commons - Attribution - …/by/4.0/` | `CC-BY-4.0` | CC | 24,205 | 0.6044 | false |
| `Creative Commons - Attribution Share-Alike - …/by-sa/4.0/` | `CC-BY-SA-4.0` | CC | 12,141 | 0.3032 | true |
| `Public Domain` | `public-domain` | public-domain | 1,191 | 0.0297 | false |
| `Creative Commons - Attribution - …/by/3.0/` | `CC-BY-3.0` | CC | 1,060 | 0.0265 | false |
| `GNU Free Documentation License` | `GFDL-1.3-or-later` | GFDL | 757 | 0.0189 | true |
| `Creative Commons - Attribution Share-Alike - …/by-sa/3.0/` | `CC-BY-SA-3.0` | CC | 692 | 0.0173 | true |
| `Creative Commons - Attribution Share-Alike - …/by-sa/2.5/` | `CC-BY-SA-2.5` | CC | 3 | 0.0001 | true |

`stackexchange` is the uniform case and produces **1 row** — `CC-BY-SA-4.0`, `row_fraction 1.0`,
`measurement = sampled`, `measured_rows = 2`, `license_authority = corpus-row-metadata`. Note what that
row records and what it does *not*: the fact came from `metadata.all_licenses` on two rows read at
offset 700,000. **`cardData` carries no license field at all**, so an ingest that trusted repo metadata
would have recorded `unknown` for a source that is 100% share-alike. That is the same lesson as peS2o's
2% in the opposite direction (§6): a source's declared license can both understate and omit what is
inside it. `measurement`/`measured_rows` are what keep this row's weakness visible.

`finewiki` produces **2 rows** — `CC-BY-SA-4.0` and `GFDL-1.3-or-later`, both `license_relation =
co-declared`, sharing one `coverage_group`, each `row_fraction 1.0`. Summing them naively gives 200%;
the coverage-group invariant is what prevents that.

### Suggested physical layout

One Parquet file at `_licenses/sources.parquet`, `zstd`, sorted by `(source, row_fraction DESC)`. **No
partitioning** — this table is one row per (source, license), i.e. on the order of **tens of rows** for
the whole 260B reservoir. The per-document design needed partitioning by `source` to keep a query from
scanning ~10⁸ rows; the source-level design does not, and saying so is the point: the artifact that
replaces it is small enough to read whole and to diff by eye.

---

## 4.1 `_licenses/works.parquet` — the upstream catalog snapshot

`DATASET-DESIGN-reservoir.md` §7 item 2 (owner-resolved) says to *"record the license per book in
metadata anyway — variant, title, source URL — so that a future commercial question is a metadata query
rather than a re-audit."* That obligation survives the SKIP decision, because it is a claim about the
**upstream catalog**, not a join to the shards. It gets its own table, and one loud caveat.

**⚠️ This table is NOT joinable to the reservoir.** There is no key from a work to a shard, an offset,
or a token. It exists as (a) the evidence behind `sources.parquet`'s aggregates, and (b) an audit trail
so a future question is a query against a dated snapshot rather than a re-scrape. Nothing in
`build_mixture` can select on it.

| column | type | meaning |
|---|---|---|
| `source` / `upstream_dataset` | `string` | Same semantics as §4. |
| `work_id` | `string` | Stable upstream work id — OpenStax `book_uuid`, LibreTexts `book_url`. |
| `work_title`, `work_url`, `author` | `string` | Attribution material. |
| `license_raw` … `verified` | — | The full §4 license block, per work rather than per source. |
| `work_state` | `string` | Upstream lifecycle where the publisher declares one. Load-bearing for OpenStax: the 129-book catalog is **live 84 / retired 34 / unlisted 8 / deprecated 3**, and only `live` is the current public catalog. An ingest list that ignores this is 35% stale. |

Measured content, ready to load: OpenStax **129 books, 100% of `meta.total_count`** — 75 `CC BY-NC-SA
4.0` / 53 `CC BY 4.0` / 1 unresolved / **0 non-CC**, cross-checked against the REX content archive with
**0 disagreements** across the 116 books both sources report. The one unresolved row is a retired empty
CMS stub (`book_uuid` null, no content), so it is not evidence of a non-CC book. **NC-SA is 86.9% of
the *live* catalog (73 of 84)** — a future commercial question removes most of OpenStax, not a fringe.

---

## 5. Why `commercial_use` is three-valued

A boolean has no way to say *"nobody has checked."* That is the state most rows are in on day one, and
it is the state that silently becomes `false`→"fine" under a boolean. Encoding `unknown` explicitly
makes the safe query trivially correct and self-documenting:

```sql
-- everything NOT provably clear for commercial use
WHERE commercial_use <> 'allowed'
```

`denied` means an NC clause was actually observed. `unknown` means no authority was read, or the
authorities conflict. Conflating those two is exactly the failure this file exists to prevent — and
under source-level granularity it matters more, not less, because a single `unknown` row now
characterizes an entire pool rather than one document.

`unmeasured` is a real and common state in this reservoir, not a placeholder: `pubmed` and
`arxiv_papers` are documented as CC BY / CC BY-SA / CC0 only, with **the SA share unpublished**; and
`proof-pile-2/algebraic-stack` has no license in `cardData` and no license tag, its effective terms
being the union of every upstream repo's license — **not a single grantable term**, so its `license_id`
is `UNRESOLVED` with `measurement = unmeasured`. Three of §3.2's named sources land here.

---

## 6. Why `license_family` is not a CC-only enum

The design doc's premise is that these collections are "Creative Commons throughout." For OpenStax that
is **confirmed** (128/128 resolvable books are CC; zero non-CC). For LibreTexts it is **measured false**:
1,948 of 40,049 rows (**4.86%**) are not CC at all — 1,191 `Public Domain` and 757 `GNU Free
Documentation License`.

Those counts are exact, not sampled (server-side `/filter` aggregation; the seven distinct values sum
to exactly 40,049 and the residual query returned 0 rows, which proves the enumeration is complete).

So a schema that modeled the license as a CC variant enum would mis-parse ~5% of LibreTexts on the
first ingest. Hence `license_family` admits `GFDL` / `public-domain` / `permissive-software` / `other`,
and `license_raw` always retains the original string. The code pool needs the fourth of those: the
sampled licenses in `stackv2_edu_filtered` are `MIT` / `Apache-2.0` / `Unlicense` / `BSD-3-Clause`, and
the field is **not case-normalized upstream** — both `MIT` and `mit` occur — which is precisely why
`license_raw` and `license_id` are separate columns rather than one.

**Two corollaries worth flagging to the design:**

1. **`libretexts_filtered` contains zero NC rows** (verified — `LIKE '%by-nc%'` → 0,
   `LIKE '%NonCommercial%'` → 0; Common Pile filtered NC out upstream). Taken alone, that would make
   license tracking look unnecessary for this corpus. §7 is why it is not.
2. **A source's declared license can understate what is inside it.** peS2o is ~2% CC-BY-SA per the
   Common Pile paper's Table 3 (120,150 of 6,254,908 train-split rows), and that is **invisible from
   repo metadata** — `cardData.license` is null and there is no license tag. A pipeline that read only
   declared metadata would record peS2o as unlicensed-unknown and miss a real SA obligation entirely.
   ⚠️ Note the denominator: the paper's table totals 6,254,908 rows while the filtered corpus reports
   6,117,280 documents, so `≈1.92%` is approximate for the reservoir, not exact. `measurement =
   paper-table` is how the row says so.

---

## 7. The measured conflict that justifies `license_authority`

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

Three consequences for the schema:

1. **A single `license` column is not sufficient.** It would have to pick a winner between two
   disagreeing authorities and would silently return "commercially clean" for content whose rights
   holder asserts NC. `license_authority` + `license_conflict` keep the disagreement *visible*, and
   let a conservative query take the strictest claim.
2. **`publisher-api` should outrank `aggregator-metadata`** when computing `commercial_use`. Rule:
   if any authority says `denied`, the row is `denied`; if authorities conflict on anything else,
   `unknown` with `license_conflict = TRUE`.
3. **⚠️ Source-level granularity makes this conflict coarser than it was.** Under the per-document
   design, an unresolved conflict flagged ~1,375–6,974 specific rows. Under source-level, a conflict
   anywhere in `libretexts` is a property of *`libretexts`* — so applying the conservative rule sets
   `commercial_use = 'unknown'` on the whole source, not on the disputed subset. That is safe and it is
   blunt: it is the §3 over-exclusion problem again, now for NC rather than SA. State it in
   `limitations`; do not soften it by resolving the conflict silently in favour of the aggregator.

Which party is legally correct is **not** resolved here. The finding is only that the two declarations
disagree, and that the disagreement is machine-detectable — which is precisely what makes it worth
storing.

---

## 8. This file is not falsifiable from the bytes — and that is the honest framing

`CONTRIBUTING.md`'s golden rule is *recompute, never trust*: a check earns its place by recomputing
something from the payload and comparing it to a claim. **Nothing in `_licenses/` can be recomputed
from the published shards.** It is a control file, outside `manifest_sha256`, and after the SKIP
decision there is no key from a license row to a document. So every row is a **producer assertion no
gate falsifies** — the same class as the known `sha256` gap `CLAUDE.md` documents, and it should be
described that way rather than dressed up.

What defends it instead, and what the columns are *for*:

- **Re-checkability, not re-computation.** `license_source_url` + `license_asserted_at` +
  `license_authority` + `measurement` make every row independently re-derivable from upstream. That is
  a weaker guarantee than a recomputed digest and a stronger one than a bare string.
- **`measurement` is the anti-decoration column.** `exact-aggregate` (LibreTexts, OpenStax) and
  `unmeasured` (pubmed, arxiv, algebraic-stack) must not look alike in a query. A schema where they do
  is exactly the plausible garbage the standard exists to stop.
- **The join key itself *is* gate-enforced.** `source` is recomputed from every object's key by
  `_check_labels_match_path`. So while the license *claims* are unfalsifiable, the thing they attach to
  is not — a row naming a `source` that no shard carries is detectable with a manifest read.

---

## 9. Blocker: where the file lives, and the producer half of the same bug

**Status: LANDED 2026-07-31 in commit `4d6768e` — and re-verify after any allowlist change, because the
failure mode is silent.** This section was written while the fix was still uncommitted; it now records
what was confirmed by execution against the committed tree, both halves:

```
CONTROL_PREFIXES = ('_catalog/', 'dependents/', '_dedup/', '_licenses/')

_licenses/sources.parquet    validate._is_control_key -> True    publish._is_control_source -> True
_licenses/works.parquet      validate._is_control_key -> True    publish._is_control_source -> True
_dedup/clusters.parquet      validate._is_control_key -> True    publish._is_control_source -> True
_licenses.parquet            validate._is_control_key -> False   publish._is_control_source -> False
```

That last row is correct and must stay `False` — see the naming note below.

The diagnosis is kept because it is the durable part: **there were two halves, and the earlier version
of this file only found one.** Anyone extending the allowlist again needs both.

**(a) The validator half.** `_is_control_key` (`validate.py:145`) exempts control **basenames** —
`dataset.json`, `manifest.json`, `_SUCCESS`, `_VALIDATED.json`, `_REJECTED.json`, `README.md`, anchored
to depth 0 — and control **prefixes**, which were `('_catalog/', 'dependents/')` and are now the four
above. A leading underscore buys nothing; the set is a closed allowlist. Anything else under the dataset
prefix that is in no group manifest raises `unlisted-object-dataset-level` (`validate.py:640`):

> *"is under the dataset prefix but is in no group's manifest… a globbing reader would still find it."*

**(b) The producer half, which the earlier draft missed and which was the dangerous one.**
`publish.py`'s `_CONTROL_BASENAMES` was **basename-only, with no prefix support at all**, and both
enumeration paths (`_stage_local_to_landing`, `_enumerate_s3`) matched on it. So an un-allowlisted
sidecar was not merely rejected — it was **swept in as PAYLOAD**: `_group_of` made `_dedup` its group,
it got a manifest entry, and it entered `manifest_sha256`. That is strictly worse than rejection,
because it makes a **mutable** sidecar part of a **frozen** dataset's identity: recomputing the cluster
table (which §1.3 expects as sources are added) would then invalidate the hash chain of an
already-published dataset. Fixing only the validator half would have produced exactly that divergence
— the validator accepting the file as control while the producer folded it into the hash — which is the
`families/` half-fix shape this repo has already been bitten by. `4d6768e` fixed it at the root, moving
both names into `contracts.py` so the two callers cannot drift again.

**Name it under the prefix, not at the root.** `_licenses/sources.parquet`, never
`_licenses.parquet`. Verified by execution:

```
_licenses.parquet           _is_control_key -> False    _group_of -> ''
_licenses/sources.parquet   _is_control_key -> True     _group_of -> '_licenses'
```

A bare depth-0 parquet fails twice over: it needs a third matching rule in `_is_control_key`, and
`_group_of('_licenses.parquet')` returns `''`, which is a hard `PublishError` — *"every object must live
under a group prefix."* A prefix also absorbs growth (`sources.parquet` today, `works.parquet` beside
it) without editing the allowlist again.

**How the file actually reaches the published prefix — and it is NOT via `publish()`.** Once a sidecar
is a control file, `publish()` skips it entirely, so it never reaches landing; and `promote()` copies
only `dataset.json`, the group manifests, and manifest-listed keys, so it would not carry it even if it
had. **Staging a sidecar to landing anyway is the trap**: it would pass Gate A and then be *silently
dropped*, expiring with landing's 14-day lifecycle, with no error at any step. Sidecars arrive the way
the generated `README.md` does — **written in place under the published prefix, after promotion**, which
is the descriptive-keys-only backfill `CLAUDE.md` sanctions and is now **phase 2b** of
`DATASET-DESIGN-reservoir.md` §5.6 (added in `257e889`, which verified the `promote()` copy loop
directly). So "publish the licenses table" is a separate deliberate write, and phase 3 must confirm it
survived.

**Do not work around any of this by hiding the file under `_catalog/`** (which does pass). `_catalog/`
is reserved for one catalog JSON per dataset/version — `fsck.py:92` parses
`_catalog/<family>/<name>/<version>.json`, and `read.py:508` / `publish.py:586` LIST it to resolve
versions, parsing every key they find as a version. A parquet there would corrupt discovery.

**This generalized beyond this task, which is why it was fixed together:** `_dedup/clusters.parquet`,
which design §1.3 recommends specifically because it is "no Gate A risk," failed identically on both
halves. Both prefixes landed in the same commit.

⚠️ **One residual risk the fix introduces, and the reason to re-verify rather than assume.** An
allowlist entry *disables* the exhaustiveness sweep for its subtree — the sweep is the check that
catches an object no manifest lists, so a prefix grown too broad silently switches off the check it
lives in. Every entry must therefore be a leading-underscore directory that cannot be mistaken for a
group name. `4d6768e` ships a test for exactly this
(`test_sidecar_allowlist_does_not_disable_the_exhaustiveness_sweep`); anyone adding a fifth prefix
should extend it rather than trust the name.

---

## 10. Reconciliations this revision forced on the plan

Recorded because a reader who trusts the earlier text will get one thing wrong:

1. **§1.5's "SA sources are wholly-SA *by source*" is only two-thirds true.** `stackexchange` and
   `finewiki` are uniform; `libretexts` is **32.05% SA / 60.4% CC BY** and `peS2o` is **≈1.9% SA** and
   is not named as an SA source in §1.5 or §7 item 4 at all. Source-level exclusion still *works* —
   it never leaks SA — but for the mixed sources it is over-broad, by the amounts in §3. The owner's
   rationale for skipping the per-document key survives this correction; the claim it rests on needs
   the qualifier.
2. **"Record the license per book" (§7 item 2) is not a join.** It is honoured by
   `_licenses/works.parquet` as a dated upstream snapshot (§4.1), explicitly not selectable at mix time.
3. **The design's `_licenses.parquet` filename is unusable** — it must be `_licenses/sources.parquet`
   (§9). §1.5 and §9.7 have been updated; **§5.5's draft README `notes` still names
   `_licenses.parquet`** and should be corrected when that text is next touched (it is `notes`, outside
   the hash chain, so it is revisable on a frozen dataset).
4. **§4.1's heading said "remove at mix time."** It cannot: removal needs to know *which* documents are
   duplicates. Corrected to "warn at mix time," which is what the shipped artifact supports and what
   §1.3's annotate-don't-delete argument always intended.

---

## 11. Summary of what is verified vs inferred

**Verified by execution or exact server-side aggregation:**
- 129 OpenStax books enumerated (100% of `meta.total_count`); 128 licenses resolved; two independent
  APIs agree on all 116 books where both report, 0 disagreements; `book_state` live 84 / retired 34 /
  unlisted 8 / deprecated 3.
- `libretexts_filtered.metadata.license` exists as a typed struct field and is populated in
  40,049/40,049 rows (0 null, 0 empty). Exactly 7 distinct values, summing to 40,049 with an empty
  residual set. SA = 12,836 rows (32.05%); non-CC = 1,948 (4.86%); NC = 0.
- `_is_control_key()` / `_is_control_source()` / `_group_of()` behaviour for `_licenses.parquet` vs
  `_licenses/sources.parquet` (§9), including the depth-0 `PublishError`, re-run against the committed
  tree after `4d6768e`: both halves now return `True` for the prefix form and `False` for the bare one.
- `labels_from_path` yields `{'source': …}` for a 1-level layout and `{'source', 'domain'}` for a
  2-level one; `ManifestEntry` has no per-document field; `entry.labels` must equal `labels_from_path`.

**Inferred / proposed, not verified:**
- Every column above is a proposal.
- `stackexchange`'s uniform CC-BY-SA-4.0 rests on **2 sampled rows**; it is recorded as `sampled`,
  not `exact-aggregate`, for that reason.
- peS2o's ≈1.92% SA is a paper-table figure on a denominator (6,254,908) that differs from the
  corpus's reported document count (6,117,280).
- The true size of the OpenStax↔LibreTexts conflict (bounded: ≥1,375, ≤6,974 rows).
- Every `row_fraction` describes the **upstream** corpus. §4.1's destructive stages (URL dedup ~53% of
  documents, exact-hash dedup, whole-document decontamination removal) run between here and the shards,
  and **without a join key their effect on the license mix is unmeasurable** (§3).

**Not determined:** which license declaration is legally correct where they conflict. That needs a
human, and it is the one thing this file deliberately does not decide.
