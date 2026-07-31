# Dataset design: `pretrain/reservoir-260b-dolma2`

**Status: DESIGN — no bytes written.** Written 2026-07-30, revised 2026-07-31 after review. Supersedes
the earlier 64B `olmo100b`-only draft. Built from a six-agent research sweep plus local verification:
every load-bearing claim was re-checked against source (code, HF API, arXiv) rather than taken from a
research summary — where a claim is unverified it says so.

```
purpose:  A reservoir. One published pool that every 20B training run draws a weighted, seeded subset
          from, so a run is described by (dataset, version, sources, ratios, total, seed) rather than
          by a bespoke corpus per experiment.
family:   pretrain
profile:  pretrain-tokens/v1              [verified in registry]
name:     pretrain/reservoir-260b-dolma2  [validate_dataset_id: PASS]
```

**⚠️ IMPLEMENTERS: PHASE 0 IS DONE (2026-07-31).** Do not re-run it. Its results, the ten plan defects
it found, and the resume state are in `artifacts/` — start with `artifacts/PHASE0-REPORT.md`, then
`artifacts/PLAN-CORRECTIONS.md`. §9's hard stop has been reached and **resolved by the owner**: the
domain classification it gated is **cancelled** (§1.2), so §9.4's dual-judge gate is now historical
record rather than pending work. Phase 1 (§5.6) is the next unstarted phase, and it still requires
the three pre-publish items listed in §9.7.

**All open decisions are CLOSED as of 2026-07-31.** In review order:

| decision | resolution | §  |
|---|---|---|
| synthetic path encoding | `synthetic-` prefix on the `source` segment | 1.1 |
| **domain labels** | **REVISED 2026-07-31 (post-Phase-0): NO classification. `domain` only where a source SHIPS one upstream; every other source is flat.** | **1.2** |
| MinHash cluster-ID home | **`_dedup/clusters.parquet` control file** — NOT `entry.labels`, which Gate A rejects. ⚠️ needs a one-line `validate.py` allowlist change first | 1.3 |
| dedup actions | Bloom **deletes**, MinHash **annotates only** | 1.3, 4.1 |
| shard size | **25,001,984 tokens** (~100 MB) → ~10,400 objects | 2.2 |
| curated-source pools | over-provisioned (15% of default) rather than tuned to the evidence's 2% | 2.1 |
| synthetic split | **equal 15B** from each of faq/tutorial/math/table | 3.3 |
| decontamination | n-gram over all 260B **+ LLM-based over the synthetic 60B** | 4.2 |
| proxy sweep | **deferred** until after the reservoir exists; no escalation path | 5.2 |
| **share-alike** | **keep SA sources SEPARABLE (precautionary only); do NOT drop them** | **1.5** |
| mix guidance | shipped in the generated README (`notes`, outside the hash chain) | 5.5 |
| OpenStax / Nemotron licensing | resolved — record per-book variants; Nemotron excluded | 7 |

**Budget: ~$411 one-time + $24/month storage.** The ~$595 domain classification is **cancelled**
(§1.2) — measured throughput put its real cost at $920–$10k, not $595 (`artifacts/COST-RECHECK.md`),
and inheriting upstream labels is free. Not included: training runs, and the deferred proxy sweep
($180–900).

**The governing principle, which resolves most of the open questions below:** in a reservoir, **ratios
are a read-time decision and pool sizes are not.** A ratio is a config field, changeable per run at zero
cost. A pool that is too small forecloses an experiment permanently. So where the evidence is
ambiguous, over-provision the pool and let the default be adjusted later — the asymmetry is
$24/month against a re-publish.

---

## 1. The irreversible decisions

Everything else here can be revised in a `v2`. These cannot — they are inside `manifest_sha256`, so
changing one means republishing, which means re-copying every payload byte.

**The list moved twice, both times because someone checked rather than assumed:**

| § | decision | status |
|---|---|---|
| **1.1** | realness fused into `source` with a `synthetic-` prefix | **irreversible** |
| **1.2** | which sources carry a `domain`, and the **slug + fold** of each inherited value | **irreversible** ⚠️ see §9.7 item 2 |
| **1.4** | held-out carved from documents, per source, before tokenizing | **irreversible** |
| **NEW** | whether the tokenizer emits a per-document key `(shard_path, doc_index)` | **irreversible** ⚠️ see §9.7 item 3 |
| 1.3 | MinHash cluster-ID home | *demoted* — backfillable under the control-file design |
| 1.5 | share-alike separability | *not on the list* — SA maps onto whole `source` values, so it survives in the names alone |

The new fourth item is the one the original plan missed entirely: the manifest's grain is **one shard
object**, so there is no per-document key. EOS boundaries are recoverable from a `.u32le.bin` later, but
the document→row mapping is gone the moment tokenization finishes. If per-document licenses or cluster
IDs are ever wanted, the tokenizer has to emit that key at build time.

### 1.1 Real vs synthetic goes in the `source` label — NOT in separate groups

`build_mixture` resolves **exactly one group**: `_choose_group` raises `ReadError` when there are
multiple and none is named (`read.py:164-182`, called from `read.py:751-753`). If real and synthetic
were separate groups, a teammate could not express "70% real / 30% synthetic" in one call — they would
call `build_mixture` twice and concatenate path lists by hand, destroying the single
`(dataset, version, sources, ratios, total, seed)` descriptor that makes a run reproducible.

So realness is **fused into the `source` name, with an explicit `synthetic-` prefix**:

```
tokens/dclm/science/train-00000.u32le.bin                        <- real
tokens/synthetic-finephrase-math/science/train-00001.u32le.bin   <- synthetic
tokens/synthetic-finephrase-tutorial/train-00002.u32le.bin       <- synthetic, no subdomain
```

Verified by execution — the prefix survives label extraction cleanly:

```
tokens/synthetic-finephrase-math/science/…
    -> {'source': 'synthetic-finephrase-math', 'domain': 'science'}
```

The prefix earns its place three ways: `grep synthetic-` audits the whole reservoir; "dial synthetic to
zero" is visually obvious in a run config rather than requiring corpus knowledge; and a reader who has
never seen this doc cannot mistake generated text for real. Dial-to-zero works as intended — omit those
sources, or give them ratio 0.

### 1.2 Two label levels is the whole budget

`PATH_LABEL_KEYS == ('source','domain')`, and **both** `publish.py:296` and `validate.py:779` call
`labels_from_path` with no `keys=` override — a third level raises `ValueError`. Verified by execution.

- level 1 `source` = **corpus + realness**, fused (see 1.1)
- level 2 `domain` = **subdomain**; a 1-level key legally yields `{source: …}` alone

Do **not** spend a level on quality tier or grade band. Fold quality into the source name
(`dclm` vs `dclm-hq`) or make it a child dataset.

#### REVISED 2026-07-31 (owner decision, post-Phase-0): inherit `domain`, never classify it

**The rule: a source gets a `domain` segment if and only if it SHIPS one upstream. Every other source
is published flat.** No classification model, no smoke-test gate, no ~$595 (or ~$920–$10k) run.

Supersedes the earlier "classify every source" decision. Two reasons, both measured in Phase 0:

1. **Cost.** The plan budgeted ~$595. Measured throughput of `EAI-Distill-0.5b` was **10.8 doc/s on
   one A10G**, putting 112 M documents at 3,080 GPU-hours ≈ **$920 spot / $3,100 on-demand** — and
   that is a floor, since the measurement used 256-token prefixes while real documents average
   11,010 chars (≈10.8× the input). `artifacts/COST-RECHECK.md`.
2. **The gate was equivocal anyway.** Pooled 87.5% PASS, but per-source: qa-forum 97.4% PASS,
   academic 84.9% and finemath 84.8% with CIs *spanning* the 85% bar, reference 80.3% clearly below.
   And the judges' own agreement ceiling was only 70–78%, because the taxonomy is genuinely ambiguous
   at the boundaries (science vs medicine, computing vs engineering). Paying five figures to buy a
   label that is ~85% right on a scale whose ground truth is 75% self-consistent is poor value.

**An inherited label is strictly better evidence than a classified one.** It is the upstream
publisher's own metadata — `metadata.site` for a StackExchange post is *where the post was actually
posted*, not a guess about it. That is the golden rule's spirit (`CLAUDE.md`: recompute, never trust)
applied to labels: prefer the fact over the inference.

#### Who ships a domain — verified by reading real schemas, not cards

| source | upstream field | `domain` value | verified |
|---|---|---|---|
| `stackexchange` | `metadata.site` | the site (`mathoverflow`, `physics`, …) | ✅ read from a real record |
| `stackv2-edu` | `metadata.gha_language` | the language (73 distinct in one shard) | ✅ read from a real record |
| `essential-web` | `eai_taxonomy.free_decimal_correspondence` | FDC level 1 | ✅ on the card + a real row |
| `finemath` | — | **flat** | ✅ no subject field in the schema |
| `finepdfs-edu` | — | **flat** | ✅ 20 columns, none a subject |
| `fineweb-edu` | — | **flat** | ✅ 10 columns, none a subject |
| `peS2o` / `pubmed` / `arxiv` | — | **flat** | ✅ Common Pile metadata is provenance, not subject |
| `finewiki` | — | **flat** | ✅ |
| `dclm` | — | **flat** | ✅ |
| `synthetic-finephrase-*` | — | **flat** (format is already in `source`, §1.1) | ✅ |

⚠️ **`essential-web` is the interesting case.** It already carries the *exact* field the cancelled run
would have computed (`free_decimal_correspondence`, whose level 1 is the 10-category scheme Phase 0
measured against). So the one source that most wanted a domain label already has one, for free. Use it.

#### Two landmines in inherited values — both verified, both must be handled

**1. Slug the value. `C#` in a key silently truncates any `s3://` URI.** `#` is the URI fragment
delimiter, so a shard at `tokens/stackv2-edu/C#/train-00000.u32le.bin` parses as:

```
path     = /pretrain/.../tokens/stackv2-edu/C
fragment = /train-00000.u32le.bin      <-- the shard name is GONE from the path
```

`labels_from_path` accepts `C#`, `C++`, and `Jupyter Notebook` happily, and `fnmatch` matches them —
so **nothing in the pipeline catches this**; it breaks at read time in a consumer. Slug every
inherited value to `[a-z0-9-]` before it becomes a path segment: `C#` → `c-sharp`, `C++` → `c-plus-plus`,
`Jupyter Notebook` → `jupyter-notebook`, `3dprinting.stackexchange.com` → `3dprinting`. Record the
slug map in the README so a teammate can map back.

**2. Cardinality is permanent.** 73 languages and ~180 StackExchange sites each become a directory
that lives inside `manifest_sha256` forever. Fold the long tail: keep the top ~20 by token count and
map the rest to `other`. A domain with three shards in it is not a useful slice, and every distinct
value is a permanent commitment.

#### The consequence you must document: a `domain=` query silently drops flat sources

Verified by execution against `read.py:_matches_labels`, which requires every requested key to be
present *and* equal:

```
request labels={'domain': 'science'}
  tokens/dclm/train-00000.u32le.bin                     skip   <-- flat, no domain key
  tokens/essential-web/science/train-00001.u32le.bin    MATCH
  tokens/stackv2-edu/Python/train-00002.u32le.bin       skip
```

So `build_mixture(..., labels={'domain': 'science'})` returns **only** the sources that happen to be
nested — silently, with no error, because absence is one-directional by design
(`validate.py:_check_labels_match_path` docstring). A teammate could ask for "the science slice" and
get a corpus that excludes DCLM, FineMath and peS2o entirely without being told.

**Mitigations, all cheap:**

- **`source=` selection is unaffected** by mixed depth — verified. That is the primary selector and it
  keeps working exactly as designed.
- **The README lists which sources carry a `domain` and which are flat** (§5.5). This is the single
  most important thing to write down; it is the difference between a documented limitation and a
  silent one.
- Gate A permits mixed depth: `_check_labels_match_path` is **per-entry** with no uniformity
  requirement, and there is no depth-consistency check anywhere in `validate.py` (grepped). Verified
  by execution — a group holding both `tokens/dclm/train-*.bin` and
  `tokens/essential-web/science/train-*.bin` validates clean.

**What this does NOT foreclose.** Classification stays possible later as a **child dataset** — a
separate `curriculum/`-family artifact keyed to this reservoir's shards, which is where a *derived,
model-generated* label belongs anyway. Nothing here is a one-way door except the slug and cardinality
choices above.

### 1.3 Dedup: Bloom deletes, MinHash annotates — but NOT via `entry.labels`

⚠️ **CORRECTION (2026-07-31, found while assessing implementation-readiness).** An earlier draft said
cluster IDs live in `entry.labels`. **That is not implementable — Gate A would reject the dataset.**
`_check_labels_match_path` (`validate.py:770-793`) requires `declared == expected` *exactly*, where
`expected = labels_from_path(entry.path)`. Verified by execution:

```
expected from path:   {'source': 'dclm', 'domain': 'science'}
declared w/ cluster:  {'source': 'dclm', 'domain': 'science', 'cluster': 'c48291'}
declared == expected ? False   →  violation 'labels-contradict-path'  →  REJECTED
```

`entry.labels` is a **derived mirror of the path**, not a free-form metadata bag. Any extra key fails.

**So cluster IDs need a different home, and this is an open design decision for the implementer —
not a settled one.** Three options, in preference order:

| option | how | trade-off |
|---|---|---|
| **A. A sibling non-manifest artifact** (recommended) | Write `_dedup/clusters.parquet` (doc-id → cluster-id → source) alongside the payload, as a **control file** outside the hash chain | Backfillable, no schema change, no Gate A risk. Cost: not covered by `manifest_sha256`, so it is not tamper-evident. Precedent: RedPajama-V2 ships `.duplicates.parquet` exactly this way |
| **B. A second group** | `dedup/` group holding the cluster table as its own payload | Inside the hash chain and tamper-evident. Cost: makes the dataset multi-group, so **every `build_mixture` call must name `group=`** (§1.1) — a permanent ergonomic tax on every reader |
| **C. Extend the schema** | Add an `entry.meta` free-form field to `manifest.py` + a Gate A check | Correct long-term, but it is a spec change to a shared standard, needs a profile bump, and blocks the build until merged |

**Recommendation: option A**, and drop cluster IDs from the "irreversible" list — under A they are
backfillable, which means **this is no longer a decision that must precede the first publish.** See the
§1 header for the current list.

⚠️ **Two corrections to option A, both from Phase 0.** (a) `_dedup/clusters.parquet` is **rejected by
Gate A today** — `_is_control_key` returns `False`, so it needs a one-line `CONTROL_PREFIXES` entry
first (§9.7 item 1). The "no Gate A risk" claim above was wrong. (b) Even with that fixed, a cluster
table needs a **per-document join key**, which the manifest does not have — that is the new fourth
irreversible decision in the §1 header. So option A is still right, but it is not free.

**The two-stage design, and why the order matters:**

| stage | catches | action | cost |
|---|---|---|---|
| **1. Bloom filter** (exact hash) | byte-identical documents | **DELETE** | ~$3 for the whole reservoir |
| **2. MinHash LSH** (5-gram, 14×8 bands) | near-duplicates — same article, different boilerplate | **ANNOTATE ONLY** — store cluster IDs, delete nothing | ~$96 |

Bloom runs first because it is nearly free and removes the bulk, so MinHash operates on far less data.
DCLM's ablation measured Bloom filtering **alone** at +1.6 CORE — matching the full
Exact+MinHash+SuffixArray stack — so the cheap stage captures essentially the entire measurable gain.

**MinHash must not delete.** FineWeb ran global MinHash across 96 crawls, then trained on the kept vs
the removed halves: **the removed data scored better**, because aggressive near-dedup preferentially
keeps unique-but-low-quality text (ads, keyword lists). Storing cluster IDs instead means a teammate
combining two overlapping sources can be *warned at mix time* (§4.1) without the data having been
pre-destroyed. This is RedPajama-V2's design: ship the signatures, keep the duplicates.

**Skip embedding/semantic dedup entirely** — ~$1,788, and it scored *below* the no-filter baseline in
DCLM's own comparison. Zero flagship corpora ship it.

### 1.4 Held-out is carved from documents, before tokenizing, per source

Not "is there a val split" but "is val drawn from a different pool than train." A val split sampled
from the same shuffled pool is not a val split. This project has already shipped a corpus whose six
held-out shards were byte-copies of train shards.

### 1.5 Share-alike: keep it SEPARABLE, keep it IN

**Owner decision 2026-07-31: SA sources stay in the reservoir. Separability is precautionary only.**
The judgement is that an SA obligation is very unlikely to bite, and the design should reflect that
rather than distort around it — so **nothing is dropped, nothing is downsized, and no category loses
tokens.** The only requirement is that SA sources remain *identifiable* after publication, so that if
the question ever does arise it is a query rather than a re-audit.

Why it is worth the small effort anyway: Phase 0 measured SA as far more load-bearing than §7 item 4
assumed — it is **100% of QA/forum** (StackExchange is CC-BY-SA), **100% of reference** (FineWiki is
CC-BY-SA 4.0 + GFDL), **32% of LibreTexts rows**, and ~2% of peS2o (per the Common Pile paper, and
invisible from repo metadata). So SA is not a fringe slice that could be dropped cheaply later; it is
structural. Separability is what keeps a future answer *possible* at zero cost today, and that is the
whole reason to bother.

**How separability is achieved, without spending a label level.** The `domain` level is now reserved
for inherited upstream values (§1.2) and `source` is already fused with realness (§1.1), so SA does
**not** get its own path segment. Two mechanisms instead, neither of which costs anything permanent:

| mechanism | what it gives | cost |
|---|---|---|
| **`source` naming is already sufficient** | SA sources are wholly-SA *by source*: `stackexchange`, `finewiki`, `libretexts`. Excluding SA = omitting those source names from a `build_mixture` call. No new machinery. | zero |
| **`_licenses.parquet`** (task B's schema) | the precise license string per source — needed anyway because SA is not a boolean (FineWiki carries **GFDL** alongside CC, a different copyleft, and LibreTexts has Public Domain + GFDL rows) | one control file |

⚠️ **Do not model SA as a boolean.** Phase 0 found 7 distinct license values in LibreTexts alone, of
which **4.86% are not Creative Commons at all** (Public Domain, GFDL). Record the license *string*;
let a consumer decide what counts.

⚠️ **`_licenses.parquet` is rejected by Gate A today** — `_is_control_key` returns `False` for it, so
it trips `unlisted-object-dataset-level`. It needs the same one-line `CONTROL_PREFIXES` change as
§1.3's `_dedup/`. Both must land before the first publish.

**Not a blocker for the first publish**, unlike §1.2's slugging: because SA maps cleanly onto whole
sources, the ability to exclude it survives in the `source` names alone even if
`_licenses.parquet` slips to a later backfill.

*Licensing notes are research findings, not legal advice.*

---

## 2. Size and shape

### 2.1 Reservoir size, derived from peak demand

A 20B run drawing ratio `r` from a category needs `r × 20B` **in that category**. Size the reservoir
to the largest ratio anyone will plausibly ask for, per category, times a headroom factor.

| category | **default** | @20B | max share supported | **pool** |
|---|---|---|---|---|
| edu-filtered web | **40%** | 8.0B | 62% | **48B** |
| web (diverse, unfiltered) | **15%** | 3.0B | 35% | **30B** |
| code | **12%** | 2.4B | 40% | **40B** |
| synthetic (rephrased) | **10%** | 2.0B | 30% † | **60B** |
| math | **8%** | 1.6B | 35% | **36B** |
| academic | **7%** | 1.4B | 20% | **20B** |
| reference/wiki | **5%** | 1.0B | 15% | **14B** |
| QA/forum | **3%** | 0.6B | 12% | **12B** |
| **total** | **100%** | 20.0B | | **260B** |

† synthetic is capped by evidence (§3.3), not by pool size — 60B is 10× what a 30%-synthetic run needs.

Every pool holds **≥3× its peak plausible demand**, verified arithmetically. Headroom absorbs MinHash
losses, decontamination, tokenizer re-counting, and reshuffling without re-epoching. **200B real + 60B
synthetic = 260B**, 0.95 TiB, **~$24/month**, and **13 distinct 20B runs before any token repeats**.

**Why curated sources are over-provisioned rather than tuned down.** An earlier draft of this table cut
academic/reference/QA to 2% combined, because at ~1B params every downstream-validated study lands
web-heavy:

| evidence | scale | finding |
|---|---|---|
| RegMix optimum (arXiv:2407.01492) | 512×1M proxies → 1B/25B | **Pile-CC 0.870**, Wikipedia **0.016**, GitHub **0.0002** |
| DoReMi (arXiv:2305.10429) | 280M → 8B | Pile-CC 11.2→**60.6%**; Wikipedia **down** 9.19→6.99; ArXiv 10.52→**0.36** |
| SlimPajama-DC (arXiv:2309.10818) | 1.3B/330B | **100% RefinedWeb (41.0) beats the 7-domain mix (40.0)**; Wikipedia-heavy **worst (37.6)** |
| RegMix correlations | 64×1B models | Pile-CC val loss r=**0.911** with the downstream average vs Wikipedia **0.273** |

That evidence is real and it is why the *default* is web-heavy (55% web across the two web rows). But
setting the **pools** from it was the wrong inference, for the reason in the header: a ratio is a config
field; a pool is permanent. At 2% pools, a wiki-heavy experiment becomes impossible for the life of the
dataset. At 15% combined pools it costs ~$5/month extra and stays possible. **Curated sources therefore
sit at 15% of the default and 20/14/12B in the pool** — enough for a teammate to run academic-heavy or
reference-heavy without a re-publish.

Expect the tuned answer to come in below 15%. Ship that expectation as guidance in the README (§5.5)
rather than as a pool constraint.

**On the reconciliation with AutoScale** (which finds curated sources should be *larger* at small
budgets): the difference is the **objective, not the scale.** AutoScale minimized *average loss across
seven RedPajama domains*, so it must spend tokens on arXiv to reduce arXiv loss. RegMix, DoReMi and
SlimPajama-DC optimized web loss or worst-case and validated on **downstream benchmarks**. For an
MMLU/ARC-targeted model the web-heavy answer is correct. AutoScale's result survives only as a
statement about *drift* — see §5.1 consequence 3.

### 2.2 Shard size: 25M tokens (~100 MB) → ~10,400 objects

**Keep whole-shard selection. Do NOT add partial-file token budgets.** An earlier draft recommended the
opposite, on the grounds that OLMo-core natively supports per-path budgets
(`source_mixture.py:500`, `numpy_dataset.py:700,778,839`) so our reader's whole-shard rule looked like a
self-imposed limitation. Reading `read.py:694-714` shows it is a deliberate design choice, and the
docstring gives the reason:

> Drawing whole shards in a seeded order **has no positional bias** … The legacy
> `SourceMixtureDatasetConfig` took `ceil(available * ratio)` from the front of every path, so a 10%
> mixture read the first 10% of every shard and never touched a tail; any ordering inside a shard
> (crawl batch, date, repo) became a systematic skew.

Partial takes do not corrupt documents — standard FSL training concatenates and re-chunks anyway. The
problem is **positional bias**: reading the head of every file yields only the earliest crawl dates and
the alphabetically-first repos, silently. Whole-shard selection is the correct choice; the cost is that
precision now depends on **shard count**, which is what sets the size below.

Measured p90 component error (real `build_mixture` loop, ±35% shard-size jitter, 120 trials):

| shard size | objects | Gate A | @20% weight | @5% | @1% |
|---|---|---|---|---|---|
| 50M | 5,200 | 0.69 h | 1.2% | 4.8% | **25%** |
| **25M** | **10,400** | **1.38 h** | **0.6%** | **2.4%** | **12%** |
| 10M | 26,000 | **3.45 h — exceeds the 7200 s timeout** | 0.3% | 1.2% | 6% |

**25M is the pick**: the finest granularity that still fits the Batch job timeout with margin. 10M is
not merely slower — it breaks the deployed validator, and it doubles the per-sample path scan (26,000
vs 10,400 comparisons, in the training hot loop, since OLMo-core's `__getitem__` scans `self.offsets`
linearly). Storage 0.95 TiB, ~$24/mo. Single-part copies throughout (100 MB ≪ 5 GiB).

**Shard tokens must be a multiple of `sequence_length`** — `file_size // (item_size * seq_len)` floors,
so anything under 8192 tokens yields **zero** instances. Use **3052 × 8192 = 25,001,984**.

### 2.3 Layout

```
tokens/<source>[/<domain>]/<split>-<NNNNN>.u32le.bin
  dtype uint32 · little-endian · header_bytes 0 · container raw
  25,001,984 tokens/shard · global ordinals across the group · ~10,400 objects
  synthetic sources carry a `synthetic-` prefix on <source> (§1.1)
```

Ship **no `.csv.gz` document-boundary sidecars.** `utils.py:217` derives the sidecar name by
`basename.replace(".npy", ".csv.gz")` — a **no-op** on `.u32le.bin`, so the path resolves to the token
shard, which then fails inside `gzip.open` rather than raising the intended error. But
`utils.py:193-197`: for a **local** path with known `eos_token_id` and `dtype`, boundaries come from
`(mmap == eos_token_id).nonzero()[0]` on a headerless raw memmap — which works on `.u32le.bin`
unchanged. So: stage locally for VSL/packed/padded classes; plain `NumpyFSLDataset` never needs them.
This also halves object count.

**EOS must be in your bytes** — OLMo-core adds no special tokens.

---

## 3. What goes in it

### 3.1 The lineage trap: token counts are not summable

The corpora that look like independent additions are mostly nested:

- FineWeb-Edu ⊂ FineWeb-Edu-score-2 ⊂ FineWeb
- **Zyda-2 contains both DCLM-baseline and FineWeb-Edu-score-2**
- Essential-Web shares **89 of 101 snapshots** with DCLM-Pool
- Dolma 3's CC reuses DCLM-Pool's Resiliparse extractions
- `olmo100b` (olmo-mix-1124) is **95% DCLM-baseline**
- DCLM's advertised "4T" is **~1T unique** after global near-dedup
- **stack-edu IS inside Stack v2**; FineMath is **NOT** inside MegaMath (independent CC re-extractions)

Only three families are structurally near-orthogonal to CC-HTML: **FinePDFs** (PDFs), **Common
Corpus** (books/gov/PD), **Common Pile** (openly licensed).

And **published token counts are not comparable at all** — of ~35 corpora surveyed only ~10 name
their tokenizer, NVIDIA and AI2 name none, and peS2o's "47.37B" is *whitespace words* (OLMo counts it
as 58.6B). **Re-count every source under dolma2 before setting any ratio.** Any sizing table built
from card figures is fiction.

Also: OLMo 3's mix lists **151B FineMath against a 34B source** (≈4.4 epochs) and **409B stack-edu
against 125B** (≈3.3 epochs) — *effective*, not unique tokens.

### 3.2 Real half (~200B): sources, one per lineage branch

| category | pool | source (priority order) | license |
|---|---|---|---|
| edu-web/PDF | 48B | `HuggingFaceFW/finepdfs-edu` `eng_Latn` → `fineweb-edu` `sample-100BT` → `EssentialAI/essential-web-v1.0` filtered | ODC-BY |
| code | 40B | **`common-pile/stackv2_edu_filtered` 67.8B** (ships real text, Blue Oak) → `tokyotech-llm/swallow-code-v2` 49.8B (Apache-2.0) → `common-pile/github_archive_filtered` | Blue Oak / Apache-2.0 |
| math | 36B | `HuggingFaceTB/finemath` 3plus 34B → `proof-pile-2/algebraic-stack` 11B (math *code*, uniquely non-overlapping) → `swallow-math-v2` 32B (Apache-2.0; a FineMath rewrite — dedupe or substitute) | ODC-BY / Apache-2.0 |
| web (diverse) | 30B | `mlfoundations/dclm-baseline-1.0` — the diversity counterweight to edu filtering | CC-BY-4.0 |
| academic | 20B | `common-pile/peS2o_filtered` 43.3B → `pubmed_filtered` 36.6B → `arxiv_papers_filtered` 6.0B | CC-BY/CC0 |
| reference | 14B | `HuggingFaceFW/finewiki` en ~3.5B (Aug 2025, math+tables retained) | ⚠ CC-BY-SA 4.0 (share-alike) |
| QA/forum | 12B | `common-pile/stackexchange_filtered` 23.9B | ⚠ CC-BY-SA |

**Avoid `stack-edu` and `the-stack-v2` directly** — they ship **SWHIDs only**, and bulk access
"requires an agreement with SoftwareHeritage and INRIA" whose LLM principles demand open model
release. `common-pile/stackv2_edu_filtered` is an independent re-filter that ships text.

**Nemotron-CC is EXCLUDED — resolved, not pending.** Its license forbids making the data available to
others, which a shared reservoir does by definition. Full clause analysis in §7 item 1. Use
`nvidia/Nemotron-Pretraining-Specialized-v1` (CC-BY-4.0, ungated) if you want NVIDIA material.

⚠️ **Every pool figure in this table is a card-reported number and must be re-derived under dolma2
before use** (§3.1). They are load-bearing for §2.1's sizing and several are known-incomparable.

### 3.3 Synthetic half: ingest FinePhrase, do not generate

**FinePhrase is real** — verified independently via the HF API and arXiv:2604.13977 (COLM-adjacent, HF
FineWeb team, submitted 2026-04-15): `HuggingFaceFW/finephrase`, **486,367,076,933 tokens**,
`license: odc-by`, ungated, configs `all`/`faq`/`math`/`table`/`tutorial`, rephrased from FineWeb-Edu
by **SmolLM2-1.7B-Instruct**.

It was **ablated at 1.2B params / 21B tokens — almost exactly this setup** — where the best format
scored **17.18 macro vs DCLM's 13.77 (+3.41)**. Largest same-scale effect in the literature.

**So don't spend ~$18K generating 200B tokens. Ingest ~60B of FinePhrase instead.**

**DECIDED 2026-07-31 — equal 15B from each of the four formats**, as four separately-weightable
sources:

```
tokens/synthetic-finephrase-faq/…       15B   (of 148.1B available)
tokens/synthetic-finephrase-tutorial/…  15B   (of 147.4B)
tokens/synthetic-finephrase-math/…      15B   (of  98.4B)
tokens/synthetic-finephrase-table/…     15B   (of  92.4B)
```

Uses only ~16% of the smallest config. Keeping the formats separate matters because they measurably
differ (table 17.18 vs tutorial 15.88 macro), so a teammate can A/B them — a single blended
`synthetic` source would destroy that permanently. Reasons to ingest rather than generate:

- The paper states **scaling the generator past ~1B params yields no additional benefit** — generator
  *family* mattered more than size (SmolLM2 16.55 > Falcon3 15.54 > Qwen3 14.49). A bigger generator
  is not an upgrade.
- **The per-run ceiling is 30%** (up to 50% with your own eval validation) — where the two largest
  independent studies converge (Kang et al., EMNLP 2025, >1,000 LLMs; Du et al., Pythia 410M–12B,
  degradation "accelerates beyond 30%"). At 30% of 20B that is **6B synthetic consumed per run**, so
  60B in the reservoir is 10× headroom. 200B would be dead weight.
- FinePhrase's own sweep peaks at 60–90%, but it mixes against **FineWeb-Edu-HQ (11.82) not DCLM
  (13.77)** — part of "more synthetic is better" is "less weak-real is better." 100% synthetic falls
  off a cliff.
- **NLU never recovers at any ratio** (5.40–5.98 vs DCLM 6.58). Synthetic buys reading/math/knowledge,
  not commonsense.
- **Rephrase, don't invent.** Cosmopedia (10.33) and SYNTH (10.03), both generate-from-scratch, score
  *below every real baseline*.

⚠ **Two traps, both verified by querying the dataset:**
1. **The rewrite is in `rollout_results[0].text`, NOT `text`** — `text` holds the *original
   FineWeb-Edu document* (the `dataset` field literally reads `HuggingFaceFW/fineweb-edu`). Ingesting
   `text` builds a reservoir of unrephrased FineWeb-Edu labelled synthetic. No hash or size check
   catches it.
2. **No post-generation quality control.** A sampled row's entire rewrite is *"Question: Can light
   accelerate to the speed of light?"* (12 tokens). Own filtering is load-bearing.
3. `all` is **4 correlated views of the same 339M documents** — not 486B independent tokens. Dedup
   must be document-aware. And its source is FineWeb-Edu, so it overlaps any FineWeb-lineage real
   tokens.

**Revised total: ~200B real + ~60B synthetic = 260B**, ~10,400 objects at 25M tokens each, ~1.4 h
Gate A, 0.95 TiB, ~$24/mo, **13 distinct 20B runs before reuse**. If you want the full 400B, add real
tokens rather than synthetic ones.

---

## 4. Dedup, decontamination, and the epoch guard

### 4.1 Cross-corpus dedup: measure at build time, remove at mix time

Exposures for a document over a run are `N · Σ_s w_s / S_s` (doc length cancels). Verified:

| scenario (20B run) | exposures |
|---|---|
| 1 source, w=1.0, 400B pool | 0.05 |
| 2 overlapping sources, w=0.5 each | 0.10 |
| 10 overlapping, all selected | 0.50 |
| **Hernandez et al. damage threshold** (arXiv:2205.10487) | **100** |

~1,000× margin; a doc needs **~2,000 copies** to reach the damage regime. So cross-corpus overlap is
an **efficiency** cost, not a correctness one.

And **destructive global dedup is affirmatively harmful**: FineWeb ran MinHash globally across 96 CC
dumps, then trained on the ~31B *kept* vs 171B *removed* tokens — **the removed data scored better**
(arXiv:2406.17557 §3.4). Pythia found dedup gave "no clear benefit" at 70M–12B equi-token.

**Pipeline (≈$150–450 one-time):**
1. URL-key dedup (Dolma's URL stage alone removes ~53% of docs) — **destructive**
2. Exact document-hash dedup, within and across sources — **destructive**, **~$3**, and DCLM measured
   Bloom-filter-alone at **+1.6 CORE**, equal to the full Exact+MinHash+SuffixArray stack. Highest
   value step in the pipeline, and the only defence against silent val duplication.
3. **Decontamination** (`allenai/decon`, `ngram_size 5`, threshold 0.8, whole-doc removal), per source
4. MinHash signatures — **compute and keep, do not filter** (datatrove defaults: 5-gram, 14×8)
5. LSH → connected components → global cluster IDs (single-task, ~460 GB RAM, 1–2 days, on-demand)
6. Per-cluster × per-source membership counts → shipped as metadata

**Skip semantic/embedding dedup**: ~$1,788, scored *below* the no-filter baseline in DCLM Table 4,
second-worst of 19 samplers in the Ask-LLM benchmark, shipped by **zero** flagship corpora.

**Inherit quality scores, don't recompute** — every source here is already classifier-filtered.

### 4.2 Decontamination is now load-bearing, and our pipeline has none

Grep-verified: no hit for `decontamin|contamina` anywhere in `src/edullm_data/`, `families/`, `docs/`,
or `CLAUDE.md`. Defensible company (DCLM and Nemotron-CC skip it; DCLM found removing MMLU overlap
*raised* scores 51.8→52.7) but it is an unstated gap, and synthetic data changes the calculus:

**FineWeb-Edu does zero decontamination → FinePhrase is FineWeb-Edu rephrased → rephrasing is exactly
what defeats n-gram decontamination.** arXiv:2311.04850 (verified): "paraphrasing, translation can
easily bypass these decontamination measures"; **8–18% of HumanEval** overlaps RedPajama-1T /
StarCoder-Data invisibly to n-gram checks; contamination found **in GPT-3.5/4-generated synthetic
data**.

And don't lean on "contamination barely matters" — OLMo 3's `decon` found GSM8K **"complete leakage"**
via Flan/Nemotron-Synth-QA and removed **>60,000 DROP examples**. Benchmark-dependent, not uniformly
negligible — and GSM8K is one an eduLLM would report.

**DECIDED 2026-07-31 — two-tier decontamination:**

| tier | scope | tool | cost |
|---|---|---|---|
| n-gram | **all 260B** | `allenai/decon` — `ngram_size 5`, `stride 10`, threshold 0.8, whole-doc removal | ~$10 |
| **LLM-based** | **the synthetic 60B only** | `lm-sys/llm-decontaminator` | ~$200 |

The second tier is targeted exactly at the blind spot: n-gram cannot see paraphrased leakage, and the
synthetic half *is* paraphrase. The 200B real half is already-filtered public corpora, so an LLM pass
over all 260B (~$800+) would be mostly wasted. Run both against **your actual eval suite**, and record
the outcome in `limitations` — including the null result if nothing is found, since "we checked" is
itself the claim worth publishing.

### 4.3 The epoch guard (~5 lines, highest value per line in this design)

`epochs_s = N · w_s / S_s` → green ≤4 · amber 4–16 (R_D*≈15.39) · red >16 · hard-fail >40
("repeating is worthless", Muennighoff et al. arXiv:2305.16264).

**Narrow selection — not cross-corpus overlap — is the real repetition risk.** Verified: a 5B source
at w=1.0 in a 20B run is *exactly* 4.0 epochs; a 1B source is 20. Every category at the §2.1 pool
sizes lands at 0.33–0.50 epochs, i.e. deep green — the guard exists for the teammate who narrows to
one small source.

**Reservoir reuse across runs is free** — each run is an independent model; nothing accumulates.

---

## 5. Default mix, and why it is provisional

**AutoScale** (arXiv:2407.20177, COLM 2025 — verified: largest model is GPT-2 Large 774M, every "3B"
is *tokens*) measured that optimal weights drift with token budget, by 2×–100×:

| domain | 0.3B tokens → 412B tokens | γᵢ |
|---|---|---|
| C4 | 21.4% → **44.0%** (rises) | 1.186 (slowest-saturating) |
| Books | 16.6% → 21.6% (rises) | 1.536 |
| Wikipedia | 20.3% → **6.8%** (falls 3×) | 1.829 |
| StackExchange | 9.7% → 4.5% (falls) | 1.954 (fastest) |
| ArXiv | 6.8% → **0.07%** (collapses 100×) | 1.927 |

**Read this as a statement about DRIFT, not LEVEL.** AutoScale minimized average loss across seven
RedPajama domains, so it must spend tokens on arXiv to reduce arXiv loss. §2.1 explains why the
*default* for curated sources at 20B is web-heavy: every study optimizing for downstream benchmarks
lands there. What survives from AutoScale is the **sign of the drift** — see §5.1 consequence 3.

**One-stage, not a curriculum, at 20B.** SmolLM2's four-stage schedule presumes 11T tokens; only its
**stage 1 (90% web / 10% code / 0% math)** is defensible at this budget. And what HuggingFace actually
*shipped* for its sub-1B models is not a curriculum at all: SmolLM2-135M and 360M trained on **one
double-filtered corpus** (`HuggingFaceTB/dclm-edu` — DCLM re-filtered with the FineWeb-Edu classifier).
Verified on the card: `edu_int_score>=3` "yields even better downstream performance when training small
language models," and DCLM-Edu wins on **MMLU and ARC** at 360M/200B.

⚠ **And the same card carries the sharpest caution in this whole design.** Verified verbatim: the gains
from that filter "weren't consistent with the ablation findings" at **1.7B mid-training**, so
"we only use the dataset for SmolLM2 135M and 360M." Same lab, same pipeline, validated at 360M,
**failed at 1.7B**. Whatever you tune at 370M, re-check before trusting it at 1B+.

### 5.1 …but the literature genuinely disagrees, and the disagreement is resolvable

**Apple, "Scaling Laws for Optimal Data Mixtures" (arXiv:2507.09404v2) reaches the opposite
conclusion, and I verified its wording in the full text:**

> "Since this scaling law is additive, the optimal domain weights h∗ that minimize it are independent
> of the model size N"
> "the minimizer of equation 5.2 is *independent* of N,D; in other words, it does not depend on scale."

And crucially, the additive law is the one they **deployed**: *"Since the additive scaling law gave us
the lowest MREs, we use it to estimate the optimal data mixture."* Fitted at 412M–1.4B, validated
unchanged at **3B and 7B/150B**, MRE 0.31% (C4) to 4.45% (Wikipedia). Their scale-dependence
demonstration is **multimodal-only** — there is no LLM-side demonstration of drift in the paper.

**Reconciliation (this is the useful part, not a coin-flip):** the two camps measure different
quantities.

- **Rank correlation over mixtures transfers well.** RegMix reports ρ=0.97 from 1M-param proxies to
  1B/25B targets (arXiv:2407.01492); DataDecide recovers **80.3%** of 1B pairwise decisions from 150M
  at 1.94% of the compute, and **no scaling-law method among 8 beat it** (arXiv:2504.11393).
- **The argmin drifts.** Data Mixing Laws found only **10 of 20** mixture rankings survive 70M→410M
  (arXiv:2403.16952); DoReMi's 1B proxy is *worse* than its 70M proxy at the 8B target, and its two
  proxies land in different corners of weight space ("multiple possible local minima").

A loss surface that is flat near its minimum produces exactly this: high rank correlation **and** a
wandering optimum. Both are true.

**Consequences for this design — all four are actionable:**

1. **Tune once, ship everywhere.** Tuning at all buys 28–48% step savings; re-tuning per scale buys
   single digits. Per-run mix freedom is real but second-order.
2. **Proxy size is non-monotone — do NOT use a bigger proxy.** RegMix's own ablation: 1000× bigger
   proxies bought **−0.1 points** at 7B/100B, and a 60M proxy predicted a 1B target *worse* than a
   1M proxy (ρ 0.94 vs 0.97). DoReMi agrees (1B proxy worse than 70M). A 370M proxy is already above
   DataDecide's 150M knee.
3. **When moving up in scale, shade broad web UP and small curated corpora DOWN.** Four independent
   lines converge on this sign: AutoScale's γᵢ ordering, BETR's optimal keep-rate rising 3%→30% with
   compute (arXiv:2507.12466), Apple's multimodal "bigger models rely more on text", and Repetition
   Mismatch's optimal broad-web share moving 0.25→0.85 from 30M→757M (arXiv:2606.07597).
4. **Match held-out repetition counts between proxy and target.** At 20B tokens a 100M-token curated
   pool repeats ~200×, far past the ~4-epoch safe band. Subsample *all* sources document-level so
   repetition rates match — otherwise proxy results don't transfer for reasons unrelated to scale.

⚠ **Do not rank mixtures on the wrong benchmark at small scale.** DataDecide: at 150M, WinoGrande
decisions are **46.9% — below chance** and BoolQ 56.5%, while ARC-Easy is 93.8% and MMLU 89.0%.
Switching the proxy *metric* from accuracy to correct-probability-per-char moves HellaSwag
predictability **+50 points**. Use continuous likelihood metrics. Separately, RegMix found Pile-CC
validation loss correlates r=0.911 with the downstream average vs Wikipedia's 0.273 — but is
*anti*-correlated with LogiQA (−0.347) and MultiRC (−0.327).

**Still provisional:** the §2.1 balanced column is a **defensible starting prior**, not a measured
optimum. Treat the first run as calibration.

### 5.2 The proxy sweep is DEFERRED until after the reservoir exists

**Sequencing decision (2026-07-31): build the reservoir first, run the sweep second, publish the
results into the README third.** Three reasons:

1. **A sweep needs the reservoir to sweep over.** Every method below samples mixtures of *your* labelled
   sources. Running it against surrogate corpora measures a different thing.
2. **Nothing irreversible depends on it.** Ratios are read-time config; §1's three permanent decisions
   are all independent of the sweep's outcome. Building first forecloses nothing.
3. **The method cannot deliver the precision the question implies.** Proxy sweeps transfer *rankings*
   well (ρ≈0.97) and *weights* badly — DoReMi's Wikipedia weight swung 0.67 → <0.20 from a change in
   proxy size alone, and HuggingFace validated a filter at 360M that then failed at 1.7B, same lab and
   pipeline. Spending even 0.15% of a run to over-determine a config field, at a fidelity the method
   does not have, is the wrong order of operations.

So the §2.1 default ships as a **stated prior**, the README says so plainly (§5.5), and the sweep
becomes a follow-up whose output is a README revision — cheap, because `notes` sits outside the hash
chain and is backfillable on a frozen dataset.

**The one control that is NOT deferred:** every run, tuned or not, should be accompanied by a plain
web-only baseline. See §5.3 control 1 — in published work the optimized mix scored 48.6 and "just use
web" scored 48.5.

### 5.2.1 When you do run it — ordered by cost

Take the first one you can execute. All costs are relative to **one 1B/20B target run** (~1.2×10²⁰
FLOPs at 6ND).

| # | method | cost | what you get |
|---|---|---|---|
| **A** | **MEDU + UtiliMax** (arXiv:2501.11747) — classify ~256 docs per source with a strong instruct model into a 5-point utility scale, then solve a Markowitz mean-variance program over the simplex | **~0.5%, zero training runs** (inference only) | A prior in a day. Best mean rank (3.4) of 10 methods at 3×10²¹ FLOPs. ⚠ authors say it **systematically underrates large web corpora** — which is exactly where §2.1 puts most of the mass |
| **B** | **MixMin** (arXiv:2502.10510, ICML 2025) — one ~160M proxy **per source** on ~5M tokens each, then a convex solve against your eval set | **~0.15%** (7 sources ⇒ ~32M tokens total) | 1–5% relative NLL; the only method in its study that improved *uniformly*. Precondition: no covariate shift across sources — check that your sources don't differ systematically in length/format |
| **C** | **RegMix scaled down** (arXiv:2407.01492) — 256 proxies at 1M–10M params × **0.25B tokens**, Dirichlet-sampled (α = token-distribution × scalar ∈ [0.1,5]), **fit LightGBM** | **~0.3–0.7%** in FLOPs; real cost is 256 job orchestrations | ρ≈0.97 ranking / Pearson≈0.94 magnitude at target scale. Use LightGBM not ridge — the gap is ρ 98.45 vs 90.08, Pearson **94.36 vs 72.57** |
| **D** | **Aioli** (arXiv:2411.05735) — fit a linear dynamic mixing law on the target run's own loss trajectory, update by exponentiated gradient | **free** — no extra runs | ~0.27 ppl over stratified |

**Do NOT port Data Mixing Laws** (arXiv:2403.16952). Its fitting runs are 70M–410M params × **30B
tokens** each and it needs ~120 of them — one 70M/30B run alone is ~10% of your entire target run, and
the fitting runs are *longer in tokens than the target run is*. It was built for a 1B/100B target.

**Tokens saturate before proxies do.** RegMix's own conclusion: ρ saturates after **~0.25B tokens**, and
**512 proxies at 0.2B beat 128 proxies at 0.8B**. Prefer many short proxies over few long ones. And per
§5.1 consequence 2, do **not** scale the proxy up.

### 5.3 Four non-negotiable controls

1. **Run a stratified / token-proportional control at target scale.** UniMax — a nearly free
   token-count heuristic — beat all nine learned baselines in the UtiliMax study, and Aioli found **no
   existing method consistently beat plain stratified sampling** across six settings. RegMix's own
   optimized mix scores 48.6 vs **48.5 for "Pile-CC only"** — inside noise.
2. **Steer on MMLU / ARC-Easy, never GSM8K.** DataDecide: GSM8K and Minerva decision accuracy is
   near-trivial at this scale; ARC-Easy is predictable at 5 orders of magnitude less compute.
3. **Treat any two mixes within ~2 accuracy points as unranked** — that is measured 1B seed noise, and
   it is larger than several published mixture wins.
4. **Match repetition rate per source, not token count.** Uncontrolled, a single proxy at 1/16 of target
   tokens is off by **0.75**; repetition-controlled it lands within **0.05** — 15× better
   (arXiv:2606.07597). This is the highest-leverage single fix in the whole method.

### 5.4 Annealing: yes. Difficulty curriculum: no.

**Support a two-phase bulk + anneal split.** Every model that publishes one converges on **anneal =
1.5–20% of tokens**, containing upsampled math/code/instruction plus the densest web, buying **+6 to
+10 average points** and **+30 to +43 on GSM8K** (OLMo 2 7B: avg 50.6→61.2, GSM8K 24.1→67.5).

For a 20B run: reserve **~10% (~2B tokens)**. Math/instruction at **10–20% of the anneal mix is
enough** — OLMo 2's microanneals found math at 10/90 (+32.5) ≈ math at 35/65 (+35.0), so presence
beats dominance. Duplicate high-value sources **2×, not 4×**. And **rewrite domain data into natural
language, never code-form**: TinyGSM-*Inline* (code-form answers) scored **−3.5, below baseline**,
while TinyGSM-MIND at 2× gave **+41.5**.

Four honest caveats: the **LR-decay confound is almost never separated** from data quality; gains
**vanish at frontier scale** (Llama 3 405B: "the improvements are negligible"); Dolmino's math pool
contains the **GSM8K train split**, and GSM8K is the headline gain; and gains saturate past ~26B
anneal tokens.

**Do not build difficulty-curriculum support.** The evidence is null-to-negative and it is the
best-controlled evidence in this whole area: BabyLM's multi-team competition measured data ordering as
the most popular intervention and found **β = −3.6, p = 0.055** — an actively negative coefficient —
concluding curriculum learning "is not an effective strategy"; the 2023 edition found it "largely
unsuccessful"; and the only 6B-scale test of pure ordering (chronological vs shuffled) found it
**neutral on capability**. Every positive result is either annealing wearing a curriculum label, a
convergence-speed win with no higher ceiling, or a data-selection method renamed. Also: a hard
quality→diverse cutover caused catastrophic forgetting (17.84 → 103.83 PPL) and static mixing trained
2.2× faster.

### 5.5 Ship the mix guidance in the generated README

`notes` and `limitations[]` render into the per-dataset README (`readme.py:209,327`) and are **control
fields outside the hash chain** — so this text is revisable on a frozen dataset, unlike the label
schema. That makes it the right home for guidance that will change as your own results arrive, and the
place to publish the deferred sweep's output (§5.2).

Draft `notes` content:

> **Choosing ratios.** This pool is deliberately over-provisioned for curated sources (academic 7%,
> reference 5%, QA/forum 3% by default) so that curated-heavy experiments remain *possible*. The
> published evidence at ~1B params / ~20B tokens points the other way: RegMix's optimum put Wikipedia
> at 1.6% and GitHub at 0.02%; DoReMi drove Wikipedia *down* and ArXiv to 0.36%; SlimPajama-DC found
> 100% RefinedWeb (41.0) beat a balanced 7-domain mix (40.0), with the Wikipedia-heavy variant worst
> (37.6). Pile-CC validation loss correlates r=0.911 with the downstream average against Wikipedia's
> 0.273.
>
> Those studies optimized for **downstream benchmarks**. Work optimizing *average loss across domains*
> (e.g. AutoScale) finds the opposite — curated sources should be much larger at small budgets. The
> difference is the objective, not the scale. **If you are targeting MMLU/ARC, start web-heavy.**
>
> **Always run a web-only baseline alongside any tuned mix.** Two mixes within ~2 accuracy points are
> unranked at this scale — that is measured 1B seed noise, and it is larger than several published
> mixture wins.
>
> **Repetition, not ratio, is the usual failure.** `epochs = total × weight / pool_tokens`. Green ≤4,
> amber 4–16, red >16, hard-fail >40. Narrow selection from a small source is the real risk.
>
> **⚠️ Only SOME sources carry a `domain` label, and a `domain=` query silently drops the rest.**
> A `domain` here is always the upstream publisher's own metadata — never a label we generated — so it
> exists only where the source shipped one:
>
> | carries a `domain` | from | flat (no `domain`) |
> |---|---|---|
> | `stackexchange` | the site it was posted on | `dclm`, `finemath`, `finepdfs-edu`, `fineweb-edu` |
> | `stackv2-edu` | the file's language | `peS2o`, `pubmed`, `arxiv`, `finewiki` |
> | `essential-web` | FDC subject, level 1 | every `synthetic-finephrase-*` |
>
> `build_mixture(..., labels={'domain': ...})` matches only shards that carry that key, so it returns
> **only the nested sources** — silently, with no error. Ask for "the science slice" and you get a
> corpus with no DCLM, FineMath or peS2o in it. **Select by `source` unless you specifically want one
> of the three labelled sources**; `source=` is unaffected by the mixed layout.
>
> We deliberately did **not** classify domains onto the unlabelled sources. A smoke test on 2,000
> documents put a 0.5 B classifier at 87.5% pooled agreement — but its own judges agreed with each
> other only 70–78% of the time, because the subject boundaries are genuinely ambiguous (is a biofilm
> proteomics paper *science* or *medicine*?). Paying five figures for an ~85%-accurate label against a
> 75%-self-consistent ground truth was not worth it. Inherited metadata is a *fact*; a classified label
> would have been a guess.
>
> **Share-alike sources are separable by name.** `stackexchange`, `finewiki` and `libretexts` are
> share-alike (CC-BY-SA, and FineWiki also GFDL); omit those `source` values to exclude SA entirely.
> Per-source license strings are in `_licenses.parquet`. Note SA is not a boolean — LibreTexts alone
> carries 7 distinct licenses, ~5% of them not Creative Commons at all.

---

### 5.6 Build sequence

The deferral in §5.2 only makes sense as an ordering, so here it is explicitly.

| phase | do | why here |
|---|---|---|
| **0. Pre-flight** ✅ **DONE 2026-07-31** | Re-counted 6 sources under dolma2; per-book licenses for OpenStax (129 books) + LibreTexts (40,049 rows); validator timeout set to 7200 s; airlock re-verified. Found 10 plan defects. | Token counts from cards are not comparable — most name no tokenizer, and every Common Pile "token" figure is `Size(GB) × 0.25`, pure arithmetic. |
| **0b. Pre-publish gate** ⬅ **NEXT** | The three items in **§9.7**: two `CONTROL_PREFIXES` entries, slug+fold the inherited `domain` values, decide on `(shard_path, doc_index)`. Optionally finish the 4 unverified token counts (~$10). | Each is irreversible if wrong and cheap if done first. `domain` slugs and the doc-index key are both inside `manifest_sha256`. |
| **1. Assemble** | Bloom-dedup (delete) → decontaminate → MinHash (annotate) → carve val from documents per source → tokenize → **attach inherited `domain` where the source ships one (§1.2), flat otherwise** → shard at 25,001,984 tokens. | §4.1 order. Val carve precedes tokenization (§1.4). The `domain` attach is now a metadata join, not a classification pass. |
| **2. Publish** | `publish()` on Batch, in-region, `hash_workers`/`copy_workers=16`, `--timeout 7200`. Gate A ≈1.4 h. | §8. |
| **3. Verify** | `verify_seal`, read a shard back, confirm `dataset_paths(labels={...})` slices, confirm the airlock still denies intern writes. | The airlock re-check is a standing project rule after anything touching permissions. |
| **4. First run** | Train once on the §2.1 default **plus a web-only baseline**. | Calibration, not a result. The baseline is what tells you whether any later tuning helped. |
| **5. Sweep** | Now run the proxy search (§5.2.1) against the real reservoir. | It needs the reservoir to exist. |
| **6. Publish findings** | Revise `notes` in the README with your measured mix (§5.5). | `notes` is outside the hash chain — backfillable on the frozen dataset, no re-publish. |

Phases 0–3 are the build. Phases 4–6 are the loop your team repeats; only phase 6 writes back to the
dataset, and it writes text, not payload.

### 5.7 WHERE COMPUTE RUNS — no bytes on a laptop, ever

**Hard constraint. Every phase below runs in AWS us-east-1.** The only thing that happens locally is
issuing API calls and reading back JSON/logs.

| phase | runs on | why it cannot be local |
|---|---|---|
| **0. token re-count** | Batch job (small, 1 vCPU) | Requires streaming every source; counting locally means egressing TBs |
| **0. HF ingest** | Batch job writing straight to `s3://edullm-landing/_ingest/` | ~2.5 TB of source documents. Use `hf_hub_download` inside the job, or `hf transfer` to EFS/S3 — never through a laptop |
| **1. dedup + decontam** | Batch, `c7g.16xlarge` spot (~$96 total) | 2.5 TB working set |
| **1. MinHash cluster stage** | Batch, `r8g.16xlarge` **on-demand, not spot** | Single-task union-find, ~460 GB RAM, 1–2 days; a 2-minute spot warning cannot checkpoint it |
| **1. tokenize** | Batch, CPU fleet | ~$5 and 0.5–6 h; see the FinePhrase note below |
| **2. publish** | **Batch, mandatory** | `publish()` GETs every byte to wherever it runs. Measured **0.8 MiB/s** from a laptop ⇒ a **9-day** ETA for this corpus. This already burned one attempt in this project |
| **3. verify** | broker read-only calls + one Batch job | Reads are metadata-scale, safe from anywhere |
| **4–5. training / proxy sweep** | `edu-llm/platform` → Batch GPU | Out of scope for this repo |

**Credentials.** All AWS access goes through the `sb-aws` MCP broker (read-only by default). The intern
role **cannot** write to `s3://edullm-data` — that is the airlock working, not a problem to solve. Every
mutation happens as the validator role inside a Batch job. For bulk S3 work driven from a session, use
the isolated `credential_process` profile pattern (`/tmp/olmo150_aws/config`), never one broker call
per object.

**Two deployed-infra gaps to fix before phase 2** (both verified open in `HANDOFF.md`):
1. **`edullm-validator` job def has `timeout: null`.** Set it to **7200 s**. A ~1.4 h Gate A is fine,
   but a wedged EventBridge-triggered promote would otherwise sit `RUNNING` forever holding queue
   capacity.
2. **Bucket policy v2 is not deployed** — the live bucket still has the v1 two-statement policy, where
   one Deny covers Put *and* Delete and exempts the validator from all five actions. Tolerable
   protecting 11 objects; this will protect ~1 TB. Runbook: `infra/DEPLOY.md:256+`.

**Landmine, already burned once:** uploading a `manifest.json` to landing **auto-fires EventBridge →
promotion**. There is no "publish but don't promote" mode. For any landing-only experiment, cancel the
validator job or disable `edullm-landing-manifest-created` **first**.

## 6. Reader work this design requires

1. ~~Sub-shard token budgets in `build_mixture`.~~ **WITHDRAWN** — whole-shard selection is deliberate
   and correct (§2.2); partial takes introduce positional bias. **No change needed.** Shard size
   absorbs the precision requirement instead.
2. **The epoch guard** (§4.3) — `epochs = N·w/S`, warn at 4/16/40. ~5 lines, highest value per line
   in this design.
3. **Cluster-aware overlap accounting at mix time** — a dot product over the shipped MinHash cluster
   metadata, no re-hashing. This is what §1.3's annotate-don't-delete buys.
4. Confirm the dataloader shuffle **spans sources**: both OLMo-core mixers *concatenate*
   (`ConcatenatedTokenSource`), so cross-source shuffling must come from
   `NumpyDataLoaderConfig(global_batch_size=…, seed=…)` or you get curriculum-by-accident.
5. **NEW (§1.2): warn when a `labels=` filter silently drops sources.** Under the mixed-depth layout,
   `labels={'domain': …}` matches only the three sources that carry a `domain`, so it can quietly
   discard most of the reservoir. `read.py` already distinguishes "this dataset is unlabelled" from
   "nothing matched" and raises for the former (`read.py:296-300`) — extend that to the *partial* case:
   if a requested label key is absent from some entries in the group, say which sources were excluded
   and how many tokens that removed. **~10 lines, and it is the difference between a documented
   limitation and a silent one.** Second-highest value per line in this design after the epoch guard.

---

## 7. Open items needing a human

1. ~~**Nemotron-CC-v2's gate + license.**~~ **RESOLVED 2026-07-31 — EXCLUDED.** Gate accepted on the
   user's HF account and the binding `LICENSE.md` read directly. Verbatim:
   **§2.1** the data is available "solely for the purpose of **internal training** of Company AI
   Solutions"; **§2.2.2** Company may not "…distribute… or **otherwise make available to others** the
   Datasets"; **§2.2.3** nor use them "in any manner that would cause them to become subject to an
   **open-source license**."
   A shared reservoir in `s3://edullm-data` that teammates read *is* making them available to others,
   and publishing it under `odc-by` collides with §2.2.3. Non-commercial use does not help — these are
   distribution and purpose restrictions, not a commercial bar. **§2.3.1** also disclaims any grant to
   the underlying copyrighted material.
   **Use instead:** `nvidia/Nemotron-Pretraining-Specialized-v1` — **CC-BY-4.0 and ungated**, verified
   the same session (RQA 134.6B, Math-Textbooks 25.1B, InfiniByte-Reasoning 19.4B, Scientific-Coding
   1.2B). `Nemotron-Pretraining-Code-v3` is likewise ungated CC-BY-4.0. The design already sourced the
   web half from `odc-by`/`cc-by` corpora, so nothing in §3.2 changes.
2. ~~**OpenStax's license.**~~ **RESOLVED by the user 2026-07-31: OpenStax is Creative Commons
   throughout, but the variant differs per textbook** (some CC BY, some CC BY-NC-SA). Use is
   non-commercial, so no variant blocks it today. **Action: record the license per book in metadata
   anyway** — variant, title, source URL — so that a future commercial question is a metadata query
   rather than a re-audit. Same treatment for LibreTexts, which mirrors OpenStax books and declares
   licenses per page (`common-pile/libretexts_filtered` carries `metadata.license`; filter on it rather
   than assuming the collection is uniform).
3. **Software Heritage bulk agreement** — gates `the-stack-v2`/`stack-edu`; its LLM principles demand
   open model release. A decision, not a workaround. (Routed around via
   `common-pile/stackv2_edu_filtered`.)
4. **Share-alike segregation** — FineWiki and StackExchange are CC-BY-SA. Private S3 triggers nothing
   (CC obligations attach on *public* Share), but keep them separable in case of publication.
5. **Common Crawl §9** — you indemnify CC for AI-training claims against a **$100** liability cap.
   Rides under nearly every web source here.

*Licensing notes are research findings, not legal advice.*

---

## 8. The publish call

```python
from edullm_data.publish import publish
from edullm_data.s3 import Boto3S3
import datetime

publish(
    "s3://edullm-landing/_migrate/reservoir-260b-staged/",
    dataset_id="pretrain/reservoir-260b-dolma2",
    purpose="A ~260B-token reservoir that any 20B training run draws a weighted, seeded subset from",
    profile="pretrain-tokens/v1",
    tokenizer="tokenizer/dolma2-bpe",
    s3=Boto3S3.default(),
    created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    hash_workers=16, copy_workers=16,
)
```

Run **on Batch, in-region** — `publish()` GETs every byte to wherever it runs (0.8 MiB/s measured
locally). Pass `--timeout attemptDurationSeconds=7200`. Publishing a manifest to landing
**auto-triggers promotion** via EventBridge; cancel the validator job or disable the rule for any
landing-only experiment.

---

## 9. EXECUTION RUNBOOK — for a fresh agentic session

**Scope of this section: PHASE 0 ONLY.** It ends at a hard stop that requires a human decision. Do not
plan past it.

### 9.0 Read this first

1. `CLAUDE.md` — project invariants and hard-won gotchas (PEP-427 wheel filename, `families/`
   resolution, the airlock re-verification rule). Not design decisions, but they will bite.
2. `HANDOFF.md` — live state of deployed infra.
3. This document, §1 (irreversible decisions), §5.7 (where compute runs), §9 (this section).

**Non-negotiable constraints:**
- **No dataset byte touches a laptop.** Everything runs in AWS us-east-1. `publish()` from a laptop was
  measured at 0.8 MiB/s ⇒ a 9-day ETA; it already burned one attempt.
- AWS goes through the `sb-aws` MCP broker (read-only default). For bulk S3, use the isolated
  `credential_process` profile — never one broker call per object.
- **Persist every artifact to disk as you go.** This machine has died mid-run before.

### 9.1 🛑 THE HARD STOP — REACHED AND RESOLVED 2026-07-31

```
╔══════════════════════════════════════════════════════════════════════════╗
║  ✅ CLEARED. The gate ran, the owner decided, and the answer was NO:     ║
║     the full domain classification is CANCELLED (§1.2).                  ║
║                                                                          ║
║  `domain` is now INHERITED from upstream where a source ships one, and   ║
║  omitted otherwise. No classification model, no ~$595, no ~$920-$10k.    ║
║                                                                          ║
║  The gate below is HISTORICAL RECORD. Do not re-run it.                  ║
║  Phase 1 is still gated — by the three pre-publish items in §9.7.        ║
╚══════════════════════════════════════════════════════════════════════════╝
```

**What the gate measured, for the record.** Pooled **87.5% PASS** (CI [85.8, 89.1], n=1,555), but
per-source: qa-forum 97.4% PASS; academic 84.9% and finemath 84.8% with CIs *spanning* the 85% bar
(statistically indistinguishable from a pass); reference 80.3% clearly below. The judges' own agreement
ceiling was 70–78%, and a 50-document human spot-check confirmed that ceiling reflects **real taxonomic
ambiguity** (science vs medicine, computing vs engineering), not broken judges.

**Why the answer was still no**, on the owner's call: the measured cost was $920–$10k rather than
~$595, and ~85% accuracy against a 75%-self-consistent ground truth is poor value at that price when
upstream labels are free and are *facts* rather than inferences.

**The rationale for having stopped here remains sound and is worth keeping**: the smoke test cost under
$1 and gated both a five-figure spend and a permanent labelling decision (a `domain` is inside
`manifest_sha256`; a wrong one costs a ~1 TB re-copy). That asymmetry is exactly what a hard stop is
for — and in this case the cheap measurement is what revealed the expensive step was mispriced.

**One lesson from running it, recorded because it nearly produced a wrong decision** (full detail in
`artifacts/smoke/SUBSTRATE.md`): the first gate run scored **49.1% and failed everything**, entirely
because of a four-word prompt error — category 0 was labelled "General works", copying the
Essential-Web card's abbreviation, when Dewey class 0 is "Computer science, information & general
works" and computing lives at `005.x` *inside* it. Fixing those words moved qa-forum from 3.3% to
97.4%. It was not caught by the score looking low — a low score is exactly what a failing candidate
looks like — but by reading the model's **raw output**. Never trust an aggregate you have not traced
to a raw example.

### 9.2 Delegation strategy — why, and the rule

Phase 0 reads ~35 dataset cards, streams metadata over ~2.5 TB of corpora, and touches several AWS
surfaces. Done inline that fills the orchestrator's context with material it will never need again.

**The rule: delegate work with LARGE INPUT and SMALL OUTPUT.** A subagent that reads 12 dataset cards
and returns 6 numbers is a context win. One that returns a transcript is a loss.

**Fan-out discipline** (per `CLAUDE.md`): ≤~16 concurrent, in sequential waves, each subagent writing
its own artifact file. The orchestrator holds only the file paths and the summary numbers.

### 9.3 Phase 0 task graph — ✅ EXECUTED 2026-07-31, kept as the record

**All six tasks ran. Do not re-run them.** Outcomes and artifacts: `artifacts/PHASE0-REPORT.md`.
Two notes for anyone reusing this graph shape:

- **The 8-way fan-out on task A backfired.** HuggingFace's datasets-server throttles **per-IP, not
  per-account**, so eight concurrent agents exhausted one shared quota and every in-flight count died
  with HTTP 429 — failures that look identical to broken corpora in the artifact. The delegation rule
  in §9.2 is sound for *context*, but it needs a concurrency budget per shared external resource.
  What worked instead: reading parquet footers off the hub CDN, which is quota-free (500 documents
  from 12 shards in 75 s).
- **Task D's S3 path in the table is wrong** — writing samples to `s3://edullm-landing/_smoke/` risks
  the EventBridge auto-promote landmine (§5.7). They went to
  `s3://sbsandbox-intern-edullm-outputs/teams/data-prep/runs/…` instead, which is also the only prefix
  the GPU role's IAM policy grants.

Wave 1 and Wave 2 are internally parallel; Wave 3 depends on Wave 1.

| task | wave | agent | writes | returns to orchestrator |
|---|---|---|---|---|
| **A. Token re-count** — for each of the 8 categories, resolve the real dolma2 token count per candidate source (§3.2). Card figures are not comparable (§3.1). | 1 | **8 subagents, one per category** | `artifacts/recount/<category>.json` | one line each: category, source, tokens, method |
| **B. License metadata** — per-book license variant for OpenStax + LibreTexts; write the schema for `_licenses.parquet` (§7 item 2) | 1 | 1 subagent | `artifacts/licenses/` | row count + distinct variants |
| **C. Infra gaps** — set `edullm-validator` job-def timeout to 7200s; report whether bucket-policy v2 is deployed (§5.7). **Report only, do not deploy the policy.** | 1 | 1 subagent | `artifacts/infra-status.md` | 2 booleans |
| **D. Sample harvest** — pull 500 docs per source needing classification (DCLM, FineMath, academic, reference, QA), 256-token prefixes, to S3 | 2 | 1 subagent | `s3://edullm-landing/_smoke/samples/` | doc counts per source |
| **E. Dual-judge smoke test** — label each sample with D/A/B, score per §9.4 | 3 | 1 subagent (needs D complete) | `artifacts/smoke/results.json` | the §9.4 table |
| **F. Sizing reconciliation** — recompute §2.1 pools against task A's real numbers; flag any pool now below 3× peak demand | 3 | orchestrator (small) | `artifacts/sizing-revised.md` | pass/fail per category |

**Task A is the highest-value delegation** — 8 parallel agents each reading several dataset cards and
returning a handful of integers, versus the orchestrator reading ~35 cards serially.

**Orchestrator responsibilities** (do NOT delegate): the §9.4 pass/fail judgement, the stop decision,
and the final report. Everything else is delegable.

### 9.4 The dual-judge gate — ✅ RAN, and its outcome CANCELLED the thing it gated

**Historical record. Do not re-run.** The protocol below is preserved because the reasoning is reusable
(and because §1.2's decision rests on what it measured), but three things in it were wrong as written
and had to be corrected live — see `artifacts/smoke/SUBSTRATE.md`:

1. **"Essential-Web's 24 topics" does not exist.** Essential-Web publishes the **Free Decimal
   Correspondence, 12 main categories**, whose **level 1 has 10 values**. "24" is the *paper's title*
   — "24T **tokens**". The run used FDC level 1.
2. **Both named judge models were unreachable.** HF Inference returned **HTTP 402, credits depleted**,
   and `Qwen2.5-32B-Instruct` has no enabled provider besides. Judges moved to **Bedrock**:
   A = `qwen.qwen3-next-80b-a3b`, B = `qwen.qwen3-32b-v1:0` — B being a dense 32 B Qwen, i.e. the real
   teacher's exact parameter count, so a *better* proxy than the plan's own fallback.
3. **The candidate has no inference provider at all** and must be self-hosted; it ran on Batch GPU
   (`g5.xlarge`), where local `torch` being broken was moot anyway.

**Result:** pooled 87.5% PASS; qa-forum 97.4% PASS; academic 84.9% / finemath 84.8% with CIs spanning
the bar; reference 80.3% FAIL. Judge ceiling J = 70–78%. See §9.1 for what the owner decided and why.

**Models** — all Apache-2.0, all ungated, all verified present on HF 2026-07-31:

| role | model |
|---|---|
| **D** candidate | `EssentialAI/EAI-Distill-0.5b` (0.5B Qwen2, purpose-trained on the 24-topic scheme) |
| **A** independent judge | `Qwen/Qwen3-235B-A22B-Instruct-2507` (MoE ~22B active; **Instruct**, not Thinking) |
| **B** native-teacher judge | `Qwen/Qwen2.5-32B-Instruct` (the model D was distilled from) |

**Taxonomy:** Essential-Web's 24 topics, as published. Do not invent categories.

**Scoring — the five patterns:**

| pattern | interpretation | scored as |
|---|---|---|
| D = A = B | unambiguous | ✅ correct |
| D = A, B differs | D sides with the independent judge; teacher is the outlier | ✅ correct |
| **D = B, A differs** | **D faithfully reproduced a bad teacher label — inherited error** | ❌ incorrect |
| A = B, D differs | both judges agree, D is the outlier | ❌ incorrect |
| all three differ | the document has no single topic | **excluded from scoring** |

That third row is the entire reason for two judges: with only the native teacher, an inherited error
scores as a *success*.

**⚠️ Score on the consensus subset, NOT against both judges jointly.** Requiring D to match both caps
the achievable score at the judges' own agreement rate — if A and B agree only 75% of the time, an 85%
bar is *mathematically impossible* for reasons unrelated to D.

```
J     = agreement(A, B)                       <- the measurement CEILING; report it
score = accuracy(D) restricted to {A == B}    <- THE GATE
inherited_error_rate = P(D == B AND A != B)   <- report it
```

**Plus a human spot-check of 50 documents where A and B disagree** — this is what distinguishes "the
taxonomy is genuinely fuzzy here" from "one judge is broken," and it is what makes the published
accuracy number believable.

*(Ran, and it earned its place: the disagreements clustered on real boundaries — NatSci/Math vs
Tech/Applied 13, Computing vs Tech/Applied 9, Arts vs Literature 4 — each a document a careful human
would hesitate on. That is what established the 70–78% ceiling as a property of the taxonomy rather
than a defect in the instruments, and it is the finding that made cancelling the run the obvious call.)*

**Gate: `score ≥ 85%` per source.** Then **stop either way** (§9.1).

⚠️ **The gate design has one flaw worth knowing if you reuse it: a per-source PASS can be
near-meaningless.** qa-forum scored 97.4%, but 96% of its documents carry a single label (StackOverflow
→ Technology), so a classifier emitting one constant would have scored ~96% there. **Always report the
label distribution beside the score** — J and accuracy are both inflated by a degenerate class prior.
`reference` was the opposite and the most informative row: 9 distinct labels, 28% mode, lowest score.

### 9.5 The report to hand back

Emit exactly this shape (kept as an indented block so it does not read as this document's own
structure):

    Phase 0 complete — awaiting decision on the ~$595 classification

    DUAL-JUDGE SMOKE TEST
    | source    | J (A↔B) | score (D on A==B) | inherited err | n scored | n excluded | verdict |
    | dclm      |      …% |                …% |            …% |        … |          … | PASS/FAIL vs 85% |
    | finemath  |       … |                 … |             … |        … |          … | … |
    | academic  |       … |                 … |             … |        … |          … | … |
    | reference |       … |                 … |             … |        … |          … | … |
    | qa-forum  |       … |                 … |             … |        … |          … | … |

    Human spot-check (50 docs where A≠B): <ambiguity is real | one judge systematically wrong>

    TOKEN RE-COUNT VS PLAN
    | category | §2.1 assumed | measured | Δ | still ≥3× peak demand? |

    INFRA
    - validator job-def timeout: <set to 7200s | failed, why>
    - bucket-policy v2 deployed: <yes|no>   (report only — do not deploy)

    COST SO FAR
    <$X of the ~$1,006 one-time estimate>

    ARTIFACTS
    <paths written, so a compacted session can resume without re-running anything>

    DECISION NEEDED
    Proceed with the ~$595 full domain classification (112M docs, EAI-Distill-0.5b)?
    - If ANY source scored <85%, the plan says HARD STOP with no escalation path.
    - Options then: ship that source flat (omit its `domain` level — still legal, still
      sliceable by `source`) · escalate the model (out of scope in the current plan) ·
      revise the taxonomy.

**ANSWERED 2026-07-31: NO — cancelled.** The owner took the first option, generalised: *every* source
without an upstream label ships flat (§1.2). The deciding facts were that the measured cost was
$920–$10k rather than ~$595, and that the accuracy on offer (~85%) sat below the ground truth's own
self-consistency (70–78%). Nothing about the reservoir's structure changes — `domain` was always
optional and `source` selection is unaffected.

### 9.6 Fail-safes

- **Any irreversible action outside Phase 0 → stop and ask.** Publishing, deleting from
  `edullm-data`, deploying a bucket policy, or spending >$50 in one step.
- **Do not upload a `manifest.json` to landing during Phase 0.** It auto-fires EventBridge →
  promotion. There is no "publish but don't promote" mode.
- **After anything touching permissions, re-verify the airlock**: intern `PutObject` to
  `edullm-data` must return `AccessDenied` (explicit deny). Standing project rule.
- **If a subagent stalls** (transcript frozen >10 min, ending on a `tool_result` with no assistant
  turn — a known failure mode in this project): kill it, re-spawn narrower, and record the artifact
  path it had reached.

### 9.7 🛑 THE NEXT GATE — three items that must land BEFORE the first publish

Phase 0 is done; the classification is cancelled. **Phase 1 (§5.6) is still blocked**, on three things
that are each irreversible-if-wrong and cheap-if-done-first. All three were found by executing Phase 0,
not by reading the plan.

**1. Two `CONTROL_PREFIXES` entries in `validate.py`.** Both sidecars this design depends on are
rejected by Gate A today — verified by execution:

```
_is_control_key('_dedup/clusters.parquet')  -> False    # §1.3, MinHash cluster IDs
_is_control_key('_licenses.parquet')        -> False    # §1.5, per-source license strings
```

They match neither `CONTROL_BASENAMES` (anchored to depth 0) nor `CONTROL_PREFIXES`
(`{'_catalog/', 'dependents/'}`), so they trip `unlisted-object-dataset-level` (`validate.py:626`).
One-line fix plus a test and a failing fixture per `CONTRIBUTING.md`. **Do not hide them under
`_catalog/`** — that prefix passes, but it is the version resolver's namespace.

**2. Slug and fold every inherited `domain` value (§1.2).** `C#` in an object key silently truncates
any `s3://` URI at the `#`, and nothing in the pipeline catches it — `labels_from_path` accepts it and
`fnmatch` matches it. Slug to `[a-z0-9-]`, fold the long tail to `other`, and publish the slug map in
the README. Irreversible because the segment is inside `manifest_sha256`.

**3. Decide whether the tokenizer emits `(shard_path, doc_index)`.** The manifest's grain is one shard
object, so there is **no per-document join key**. If per-document licenses or MinHash cluster IDs are
ever to be joined back to documents, the tokenizer must emit that key **at build time** — EOS
boundaries are recoverable from a `.u32le.bin` afterwards, but the document→row mapping is not. This is
the one genuinely irreversible item the original §1 did not list.

**Also worth doing before Phase 1, but not blocking** (~$10, ~1 h, in-region): finish the token
re-count for the four unverified categories via the footer-bytes method that worked in Phase 0. §2.1's
pools are currently 5-of-8 verified, 1 verified-failing (`reference`, see `artifacts/sizing-revised.md`),
and 2 unverified.
