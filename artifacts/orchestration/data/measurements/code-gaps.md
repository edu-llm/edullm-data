# Code gaps 1 and 3 — DATA-EXEC, own hands

**Author:** DATA-EXEC. **Date:** 2026-08-08. Written directly because the `W4-CODEGAPS` dispatch
failed twice on a permission-classifier outage (`claude-sonnet-5[1m] temporarily unavailable`) —
the same outage PLAT hit in the prior wave. Read-only work is unaffected, so I did it myself.

Grades: `MEASURED` / `MEASURED-IN-CODE` (file+line) / `DERIVED` / `CARD` / `UNVERIFIED`.

---

# GAP 3 — the per-document quality-percentile label

## 🔴 VERDICT: `FINAL-DATASET-REPORT.md` §11 IS NOT IMPLEMENTABLE AS WRITTEN.

The report calls this label **"unbackfillable"** and frames the decision as *do it at ingest, or pay
a re-copy later.* **That framing is wrong, and it is wrong in a way that makes the item cheaper, not
dearer: a per-DOCUMENT label cannot be stored at ingest either.** There is no "now" option to take.
The schema has no per-document tier at all.

### Evidence, all `MEASURED-IN-CODE`

**1. `labels` hangs off `ManifestEntry`, and a `ManifestEntry` is ONE OBJECT — one shard file.**
- `ManifestEntry.labels: dict[str, str] | None` — `src/edullm_data/manifest.py:233`
- `build_manifest` keys entries by `entry.path` and **raises on a duplicate path**
  (`manifest.py:528-533`: *"a manifest is a set of paths (§5 proves completeness by path-set
  equality)"*). So the manifest is a **set of objects**. There is no row beneath an object.
- A shard is `SHARD_TOKENS = 25,001,984` tokens (`corpus.py:89`). At the reservoir's measured
  **814.9 tokens/doc** (`IMPLEMENTATION-PLAN.md` §4.5), **one label would have to cover ~30,680
  documents.** `DERIVED`: 25,001,984 ÷ 814.9 = 30,681.
- A percentile averaged over 30,680 documents is not a per-document label; it is a shard statistic.

**2. The value type forbids it independently.** `manifest.py:230-232`, verbatim: *"Flat and
string-valued ONLY, deliberately: a partition selects by exact label match, so a nested or
richly-typed value would need a query language and a validator nobody has written."* Enforced at
`manifest.py:292-309` — non-string or empty keys raise, non-string values raise. **A per-document
array of percentiles is not a flat string and cannot be made into one.**

**3. Only TWO label keys exist, and the third level is refused.**
`PATH_LABEL_KEYS = ("source", "domain")` — `manifest.py:693`. `labels_from_path` **raises** when the
key nests deeper (`manifest.py:730-742`). The `keys=` parameter looks like an escape hatch and the
docstring says outright it is not (`manifest.py:715-724`): *"no production caller passes it"*;
`publish.py` and `validate.py` both use the default; the only `keys=` caller in the repo is a test.
**"Two levels is the whole budget."** A `quality` label would be a **third dimension** and must be
flattened into the `source` segment — i.e. `dclm-q0`, `dclm-q1`, … as separate directories.

**4. Gate A actively enforces this — a label is RECOMPUTED, never accepted.**
`_check_labels_match_path` (`validate.py:1380`, called at `:830`) recomputes labels from the object's
own key with the default `PATH_LABEL_KEYS` and compares by **full dict equality**. Its docstring
states the principle: *"a label nothing recomputes is a producer assertion."* **So a producer cannot
write a quality label at all** — an entry declaring one that is not derivable from its own path is
rejected as `labels-contradict-path`. This is the golden rule ("recompute, never trust") working
exactly as designed, and it happens to close this door.

**5. Unbackfillability itself — CONFIRMED, but now moot.** `manifest_sha256` (`manifest.py:545-554`)
is the sha256 of the canonical JSON of the whole manifest dict, whose `entries` are
`entry.to_dict()` (`manifest.py:539`), and `to_dict` includes `labels` when non-empty
(`manifest.py:329-330`). So **labels ARE inside `manifest_sha256`** — the report is right about
that. The sanctioned in-place backfill is **descriptive-keys-only**, guarded by an assertion that
`groups`/`manifest_sha256`/`inventory` stay byte-identical (CLAUDE.md), so a label is **not** a
descriptive key and cannot ride that path. Both halves confirmed; the point is simply that there is
nothing to backfill *into*.

### What this means — and it CHANGES A DECISION

**A per-document quality percentile has exactly one encoding this pipeline accepts: bucket the
documents by percentile and make the bucket part of the `source` segment**, so quality becomes a
directory that Gate A can recompute from the key. That is not a label change — **it is a
SHARD-ROUTING change**, and it multiplies the source count by the number of buckets.

| option | what it is | cost |
|---|---|---|
| **(a) quality-bucketed sources** | `source=dclm-q0…q4`, quality in the key | **5× the source rows.** Interacts head-on with **#28**, which already needs N registry rows to split DCLM. Ordinals are allocated across the whole plan alphabetically, so this must be in the FROZEN plan — adding it later renames 98% of shards and voids 882B tokens |
| **(b) skip it** | no quality dimension in v1 | free. Loses the 2026-era reweighting-by-quality option |
| **(c) retrofit later** | re-copy | ~40,000 objects, Gate A at **8 round trips/object, 7 serial** (`IMPLEMENTATION-PLAN.md` §8.1) |

**⚠️ (a) and (b) are not symmetric with what the report implies.** The report says storing the label
is the cautious default. **It is not cautious — it is a 5× multiplication of the source cardinality
inside `manifest_sha256`, permanently**, and `manifest.py:849` warns in exactly these terms: *"Every
distinct value is a directory that exists inside `manifest_sha256` forever."* Cardinality is the
thing this codebase repeatedly says is irreversible (the same reason stackv2-edu's 73 languages and
stackexchange's ~180 sites must be folded to ~20).

**Question 3 — where would the percentile even come from?** `dclm_100BT` ships `fasttext_score`
(registry row's verified leaf list, `MEASURED-IN-CODE` in `corpus-registry.json`); FineWeb-Edu ships
an edu score; FinePDFs-Edu is already edu-filtered. **But the scores are not comparable across
sources**, so a percentile is per-source, and it needs either a global sort or a sampled CDF per
source before the tokenize pass can route a document. That is **a second pass over the source
bytes** — with staging that is ~0.25 h, without it is the 3.8–19 h live-HF re-read
(`IMPLEMENTATION-PLAN.md` §3.2). **`UNVERIFIED`: nobody has measured the CDF-sampling cost.**

**The principle question I am NOT deciding, per the deviation protocol — CEO ruling needed.**
`corpus.py:237-240` says an inherited value is *"the upstream publisher's own statement of fact…
Never a classifier."* A **percentile of an inherited score** is a monotone transform of a publisher
fact — arguably still inherited. But the bucket boundaries are ours, and `fasttext_score` is itself
a *classifier* output, so "inherited" would be laundering a model score into a fact. **Framed, not
decided.**

### Interaction with the cluster-ID schema — SAME DECISION, ONE RULING
Cross-corpus dedup is deferrable at 5% sampling (~1000× margin to damage) **but the cluster-ID
schema must be set before the first publish.** Both are "schema now or never." **And both resolve
the same way: neither can be a per-document field, because the manifest has no per-document tier.**
Both must become a `source`-segment dimension or be dropped. **They should be one CEO ruling, and
that ruling must land BEFORE `#20` FREEZE**, because both change the source cardinality that ordinal
allocation depends on.

**Recommendation to the CEO: (b) skip for v1**, on the evidence that (a) is not the cheap
schema-hygiene item §11 describes but a 5× cardinality multiplication colliding with #28, and that
its input is an unvalidated cross-source score. **This is a recommendation, not a decision.**

---

# GAP 1 — `.jsonl.zst` has no reader (14B tokens of stage 2)

## Status: CONFIRMED in code, and there is a SECOND, SILENT path. Costing in progress.

**Confirmed, `MEASURED-IN-CODE`:**
- `READABLE_FORMATS = frozenset({"parquet", "json.gz"})` — `corpus_build.py:127`
- `_assert_readable` raises at **PLAN time** — `corpus_build.py:171-179`. The comment above it
  (`:120-126`) explains why plan-time: *"discovering it mid-run means other bundles have already
  been built and paid for, and the corpus quietly lacks a whole category."* It records that this
  **already fired once for real** — `dclm-baseline` pointed at a `.jsonl.zst` repo, a 30B hole.
- `corpus_read.py:774-775` names `.zst` explicitly: *".zst is NOT among them — Common Pile ships
  some prefixes as `.json.zst`, which needs a zstandard dependency this package does not declare."*
- `_READERS` maps only `parquet` and `json.gz` — `corpus_read.py:749-751`
- `_reader_for` raises for an unmapped format — `corpus_build.py:908-916`
- Source: **`allenai/dolma3_dolmino_mix-100B-1125`**, `.jsonl.zst` — `IMPLEMENTATION-PLAN.md:1890`
- Draw: **14.0B = 14% of stage 2** — `FINAL-DATASET-REPORT.md:100`

**🔴 THE SILENT PATH — `MEASURED-IN-CODE`, `corpus_build.py:643`:**
```
drawn = [s for s in drawn if s.file_format in READABLE_FORMATS]
```
**A filter, not a raise.** `_assert_readable`'s own error message advertises `--allow-unreadable` as
the deliberate opt-out that *"EXCLUDES it from the corpus rather than failing."* So the loud
plan-time gate has a companion that drops the source and continues. **If that filter runs without
the operator having consciously passed the flag, the build ships 14B tokens short while every bundle
reports success** — precisely the failure mode `_assert_readable`'s comment says it exists to
prevent. **Which path wins under which flags is the open question; handed to a worker to trace.**

**⚠️ PRODUCTION-ONLY FAILURE MODE — `MEASURED`.** `zstandard 0.25.0` **is installed on this
machine**, but `pyproject.toml` declares only `boto3` and `numpy<2.5`. **A local test of a `.zst`
reader would pass while the Batch container fails.** This is the same shape as the `families/` bug
in CLAUDE.md — a missing dependency that does not raise locally and fails only in production.

**Third option worth checking before costing FIX vs DROP:** AI2's dolma3 **pre-tokenized** shards
are byte-compatible with our `.u32le.bin` (dolma3-tokenizer IS dolma2; verified by range-read;
ingest = copy+rename with `.csv.gz` sidecars for doc offsets + ids). `IMPLEMENTATION-PLAN.md:1890`
says *this* mix ships text, not pre-tokenized shards — **but if pre-tokenized shards exist for it,
the gap evaporates into a copy.** Assigned to a worker to verify from the tree API.

**Not yet done:** the DROP-cost re-derivation of the §4 epoch table. **The report warns that table is
"one arithmetic accident away from being wrong"**, so redistributing 14B across the remaining
stage-2 sources must be recomputed, not eyeballed.

## GAP 1, DROP cost — the epoch table SURVIVES. `DERIVED`, arithmetic shown.

I re-derived it rather than eyeballing it, because `FINAL-DATASET-REPORT.md` §4 warns the "max epoch
0.90" property is *"one arithmetic accident away from being wrong."* Method: drop the 14.0B dolma3
QA row, redistribute **pro-rata** across the remaining 8 stage-2 rows (each gets
`t + 14.0 × t / 86.0`), then sum stage-1 + stage-2 epochs per source against the SAME pool.

| source | s2 old | s2 new | pool | ep s2 | ep s1 | **TOTAL** |
|---|---|---|---|---|---|---|
| DCLM-baseline | 32.0 | 37.21 | 744.6 | 0.050 | 0.508 | **0.558** |
| code (stackv2) | 18.0 | 20.93 | 707.0 | 0.030 | 0.127 | 0.157 |
| Nemotron-CC-Math | 16.0 | 18.60 | 134.0 | 0.139 | 0.336 | **0.475** |
| reasoning traces | 8.0 | 9.30 | 50.0 | 0.186 | — | 0.186 |
| Cosmopedia | 4.0 | 4.65 | 21.7 | 0.214 | — | 0.214 |
| Nemotron Math-Textbooks | 3.0 | 3.49 | 27.5 | 0.127 | — | 0.127 |
| academic | 3.0 | 3.49 | 46.6 | 0.075 | 0.386 | 0.461 |
| reference | 2.0 | 2.33 | 26.2 | 0.089 | 0.344 | 0.432 |
| **FinePDFs-Edu** (stage 1 only) | — | — | 70.0 | — | 0.900 | **0.900 ← still the max** |

**✅ The under-1-epoch property HOLDS.** Worst redistributed source is DCLM at **0.558**, far below
FinePDFs-Edu's unchanged **0.900**. The report's warning is about adding FinePDFs to the cooldown or
raising Nemotron-CC-Math past ~2.2× — **this drop does neither** (Nemotron-CC-Math moves 0.46 → 0.475,
a 3.3% increase, nowhere near 2.2×).

**So the DROP is arithmetically safe. It costs 14.0B = 1.4% of the 1.0T corpus, and it costs the
QA-bearing content itself** — which §6 of the report argues is the *point* of the cooldown, so the
loss is qualitative, not quantitative. It is not blocked by the epoch table.

**Caveat, stated so it is not overread:** pro-rata is one redistribution among many, and the CEO may
re-cut shares instead. The result is robust to that — every non-FinePDFs source has ≥0.44 of headroom
to 0.90, so **no plausible reallocation of 14B breaks the property.** `DERIVED`.

---

## GAP 1 — W4-ZSTD detail

**Worker:** W4-ZSTD. **Started:** 2026-08-08. **Status:** IN PROGRESS (appended live; a partial
section is intentional, not a truncation).

**Scope:** (1) trace the silent-drop path at `corpus_build.py:643` end to end; (2) cost the zstd
reader incl. seekability + the undeclared-dependency production trap; (3) check whether AI2 ships
PRE-TOKENIZED shards for `allenai/dolma3_dolmino_mix-100B-1125`, which would make the fix a copy.

**Environment note (MEASURED):** `python3 -c "import zstandard"` → `0.25.0`, backend `cext`, on this
laptop. `pyproject.toml` does NOT declare it (verified below). This is the production-only trap.

### Q3 — THE THIRD OPTION (pre-tokenized copy): **REFUTED for this repo.** No shards exist.

Checked first, as instructed. It does not dissolve the gap.

**Pinned revision (MEASURED, HF `/api/datasets`):**
`allenai/dolma3_dolmino_mix-100B-1125` @ **`f23aa129fda8335ba9760057bcc1f0c02f3d068b`**
(`lastModified` 2026-02-23T19:03:37Z, `license: odc-by`, `gated: false`, `private: false`).
**Never cite `main`.** `allenai/dolma3-dolmino-mix-1025` → **HTTP 404**, confirming the brief.

**File inventory (MEASURED — full `siblings` array from the metadata endpoint, 99,676 entries,
which is the complete repo tree, not a page):**

| count | extension |
|---|---|
| **99,674** | `.jsonl.zst` |
| 1 | `.gitattributes` |
| 1 | `README.md` |

**There is not one `.npy`, `.bin`, `.u32le.bin`, `.csv.gz`, `.parquet`, or `.json.gz` in the repo.**
Top-level entries are exactly three: `data/`, `.gitattributes`, `README.md`. So:

- ❌ **COPY is not available from this repo.** The `ai2-dolma3-shards-are-byte-compatible` finding is
  true of AI2's *pre-tokenized* releases; **this midtrain mix is not one of them.** It ships TEXT.
  `IMPLEMENTATION-PLAN.md:1890` is **CONFIRMED**, not refuted.
- ❌ There are **no `.csv.gz` sidecars** here, so the doc-offset/doc-id story does not apply either.

**Category labels — MEASURED, and I correct the brief's number in place:**
the brief says "**323** directory names ARE category labels". The measured count of distinct
directories under `data/` is **209**, not 323. (`len(set(f.split('/')[1] for f in siblings if
f.startswith('data/'))) == 209`.) The 323 figure is not reproducible against rev `f23aa129`; treat
it as stale or as a different repo/revision. The *claim* it supports survives: the directory names
are `ingredient{1,2}-<source>-<subcategory>` and ARE selectable by prefix with no classifier.

### Q1 — THE SILENT DROP: **CONFIRMED, and it is narrower AND wider than stated.**

**Verdict in one line:** the drop **cannot happen by accident** (the operator must type
`--allow-unreadable` on `plan`), but **once typed, nothing downstream ever notices** — not `verify`,
not the receipts, not `plan_document`'s totals. The flag is a **one-way door with no witness.**

#### (a) Which fires under which flag — MEASURED-IN-CODE, `corpus_build.py:637-644`

```
637  def _cmd_plan(args) -> int:
638      specs, meta = load_registry(args.registry)
639      drawn = [s for s in specs if s.target_tokens > 0]
640      if not args.allow_unreadable:
641          _assert_readable(drawn)
642      else:
643          drawn = [s for s in drawn if s.file_format in READABLE_FORMATS]
644      plan = plan_document(drawn, registry_meta=meta)
```

A clean `if/else`. **Line 643 is reachable ONLY when `args.allow_unreadable` is truthy.** There is no
third path into it.

**Can a build reach `:643` without the operator consciously passing the flag? — NO. Four independent
confirmations:**

1. **It is a bare `store_true` with no `default=` override** — `corpus_build.py:949-950`:
   `p.add_argument("--allow-unreadable", action="store_true", help="EXCLUDE sources with no reader
   instead of failing")`. argparse defaults `store_true` to `False`.
2. **It is registered on the `plan` SUBPARSER ONLY** (`p = sub.add_parser("plan", ...)`, `:947`), not
   on the top-level parser (`:934-940`) and not on `run` (`:953`) or `verify` (`:965`). So
   `args.allow_unreadable` **does not even exist** on a `run`/`verify` namespace.
3. **No environment-variable back door.** `grep -n "os.environ" corpus_build.py` returns exactly two
   hits — `:674` (`AWS_BATCH_JOB_ARRAY_INDEX`) and `:772` (`TOKENIZERS_PARALLELISM`). Neither
   touches readability. There is no `EDULLM_ALLOW_UNREADABLE`.
4. **`plan_document` is not otherwise callable in production.** `grep -rn "plan_document"
   --include="*.py" src tests artifacts` → exactly ONE non-test, non-`__all__` call site:
   `corpus_build.py:644`. Every other hit is `tests/test_corpus_build.py`. **There is no second
   driver that could bypass `_assert_readable`.**

So the CLI surface is honest. **I therefore REFUTE the strong form of the worry** ("a build can
silently ship 14B short"): it cannot do so *unprompted*. The flag is the deliberate opt-out its own
error text advertises, and it behaves exactly as advertised.

#### (b) 🔴 BUT: once the flag IS typed, the exclusion leaves NO TRACE ANYWHERE. This is the real defect.

This is the part that is **worse than the brief supposed**, and it is where I answer
"does the plan's declared total still claim 1.0T while the corpus holds 986B?"

**The answer is subtler and more dangerous than yes: the plan NEVER DECLARES A DESIGN TOTAL AT ALL.**

`plan_document` (`:182-289`) computes every total **from `drawn`** — the already-filtered list:
- `:200` `drawn = [s for s in specs if s.target_tokens > 0]` (re-derived inside, on the passed list)
- `:271` `"tokens": sum(r.tokens for r in shards)` — per bundle, from the surviving shard refs
- `_cmd_plan`'s own stdout line `:646-648` prints `tokens={sum(b['tokens'] for b in plan['bundles'])}`
  — again a sum over survivors.

**MEASURED-IN-CODE:** `grep -n "1_000_000_000_000\|design_target\|TARGET_TOKENS\|expected_total"` over
`src/edullm_data/*.py` and `artifacts/reservoir/*.py` → **zero hits.** No constant anywhere states
what the corpus is *supposed* to total. So there is **no 1.0T number for a 986B corpus to contradict.**

**This inverts the failure mode, and makes it harder to catch, not easier:**
> The plan does not lie about the total. **It silently redefines the total** to be whatever survived
> the filter, and reports that new number as if it were the plan. An operator who reads
> `tokens=986,000,000,000` off stdout has no artifact to compare it against — the 1.0T target lives
> only in `FINAL-DATASET-REPORT.md`, in prose, outside the code.

**And the exclusion is not recorded in the plan document.** `plan_document`'s output dict
(`:252-286`) has keys: `schema`, `group`, `shard_tokens`, `min_doc_tokens`, `val_fraction`,
`tokenizer`, `registry_revisions_pinned_at`, `no_val_split`, `bundles`, `plan_id`. **There is no
`excluded` / `unreadable` / `dropped_sources` key.** Verified by `grep -n "excluded|unreadable"
corpus_build.py` → only `:178` (the error text) and `:411` (an unrelated comment).

**This is a self-inconsistency in the module's own design standard, and it is the cleanest possible
argument for the fix.** Twelve lines above, at `:261-267`, the SAME function goes out of its way to
record an analogous omission:

```
263      "no_val_split": sorted(...)
```
with the comment (`:261-262`): *"Sources that get NO held-out split because 0.5% of their target is
under one shard. **Recorded in the plan, not just warned about, so the omission is auditable
afterwards** — 'which sources have no val data' must be answerable from the artifact."*

**By the module's own stated rule, "which sources were excluded as unreadable" must ALSO be
answerable from the artifact — and it is not.** A missing val split for one 1.8B source is recorded;
a missing 14B *category* is not. That asymmetry is the bug.

#### (c) Does `verify` notice? — **NO. Structurally cannot. MEASURED-IN-CODE.**

`_cmd_verify` (`corpus_build.py:716-762`):
- `:736` `plan = _load_plan(s3, args.bucket, args.prefix, args.plan_id)` — it loads **the same
  post-filter plan**.
- `:737` `bundles = bundles_of(plan)`
- `:748-751` `verify_bundle_set(receipts, [b.stream for b in bundles], ...)`

**`expected_streams` is derived FROM THE PLAN.** An excluded source produced no bundle, so it is not
in `bundles`, so it is not in `expected_streams`, so `verify_bundle_set`'s `bundle-set-incomplete`
check (`corpus_receipt.py`, the `for stream in sorted(expected_set...)` loop) **has nothing to miss.**
`verify` prints `OK 27 bundles, N shards` and returns 0.

This is doubly ironic because `verify_bundle_set`'s docstring is *explicitly about this exact failure
class*: *"a missing bundle gives a smaller corpus — the remaining shards are all valid, every count
is internally consistent, Gate A passes, and the mixture the README names is quietly not the mixture
that was built. Nothing objects unless something checks the *set*."* **It checks the set against the
plan. It cannot check the plan against the design.** `--allow-unreadable` moves the loss one level
up, out of `verify`'s reach.

`_cmd_verify:753-757` even prints the right warning — *"A missing bundle is not a smaller corpus, it
is a wrong one"* — but only on the branch that cannot fire here.

#### (d) Do the receipts notice? — NO.

`Receipt` fields (`corpus_receipt.py:253-275`): `plan_id, bundle_id, prefix, source, domain, split,
shards, documents, tokens_in, tokens_out, tail_dropped, surplus_dropped, max_eos_fraction,
wheel_version, sources, unfilled, schema_version`. Every one is **per-bundle**. A receipt for a
bundle that was never planned cannot exist. The token-conservation assertion
(`corpus_receipt.py:686`, `tokens_out + tail_dropped + surplus_dropped == tokens_in`) is *within* one
bundle and passes perfectly on a 986B corpus.

#### (e) A SECOND, DISTINCT defect found while tracing: three format tables that disagree.

**MEASURED-IN-CODE — there are THREE independent format→reader maps, and they are not the same set:**

| # | location | contents |
|---|---|---|
| 1 | `corpus_build.py:127` `READABLE_FORMATS` | `{"parquet", "json.gz"}` |
| 2 | `corpus_build.py:908-911` inline dict in `_documents_for` | `{"parquet", "json.gz"}` |
| 3 | `corpus_read.py:748-752` `_READERS` | `{"parquet", "json.gz", **"jsonl.gz"**}` |

`_READERS` accepts a third spelling, `jsonl.gz`, which the other two **reject**. Its own comment
(`:744-747`) says the spelling exists because *"the dolmino `math` prefix mixes `*.jsonl`,
`*.jsonl.gz`, `*.json.gz` and `*.json.zst` in ONE directory."*

**Consequence: a registry row declaring `file_format: "jsonl.gz"` — a real upstream spelling that
`corpus_read` handles fine — is rejected by `_assert_readable` as unreadable, and under
`--allow-unreadable` is SILENTLY DROPPED despite a working reader existing.** That is a live
false-negative in the gate, today, independent of zstd.

Further: **`corpus_read.read_documents` (`:755-776`) — the "named seam so the per-source loop in the
build driver does not grow a format `if`" — IS DEAD CODE.** `grep -rn "read_documents"` over `src`
finds only its definition and its `__all__` entry; the only callers are
`tests/test_corpus_read.py:805-824`. The build driver **grew the format `if` anyway**, inline at
`corpus_build.py:908-911`. **So adding zstd to `_READERS` alone would change NOTHING about a real
build** — a fix must touch all three tables. Flag this to ENG: it is the single most likely way a
zstd patch ships and does nothing.

### Q2 — COST TO FIX, and the seekability question, ANSWERED BY EXPERIMENT

All of the following is **MEASURED** on a real file from the pinned revision, decompressed locally
with `zstandard 0.25.0` (scratch scripts in `/tmp`, nothing written to the repo).

**Test object:** `data/ingredient1-nemotron-synth-qa/CC-MAIN-2013-20-part-00005.jsonl.zst`
@ `f23aa129…`. 7,973,126 compressed bytes → 22,168,327 decompressed (**ratio 2.78**), 9,130 JSON
lines. HF serves it with `accept-ranges: bytes` and a correct `content-length`, so the existing
`_RangeFile` transport works against it unchanged (verified: a `Range: bytes=0-1048575` returned
`206`, 1,048,576 bytes).

#### Seekability — **the good news. It streams. It does NOT need whole frames in memory.**

MEASURED frame parameters (`zstandard.get_frame_parameters`):

| field | value | what it means |
|---|---|---|
| `window_size` | **2,097,152 (2 MiB)** | the decoder's back-reference window — **the real memory bound** |
| `content_size` | `2**64-1` = **UNKNOWN** | AI2 streamed the compression; no size in the header |
| `has_checksum` | **False** | ⚠️ no per-frame integrity check (see truncation, below) |
| `dict_id` | 0 | no external dictionary needed |
| frames per object | **1** (`raw.count(b'\x28\xb5\x2f\xfd') == 1`) | single-frame, not the multi-member gzip case |

**MEASURED memory, three ways, all bounded:**

| approach | tracemalloc peak | max single output chunk |
|---|---|---|
| `stream_reader(fileobj).read(1 MiB)` loop | 21.2 MB RSS delta | 1 MiB |
| `decompressobj().decompress(8 MiB chunk)` | 61.07 MB | **22.17 MB** ← unbounded: one call emitted the WHOLE file |
| **`stream_reader(<chunk-iterator adapter>).read(256 KiB)`** | **24.45 MB** | **262,144 B (exactly the ask)** | 

**→ ANSWER: a `.zst` reader CAN stream incrementally, at a caller-chosen output chunk size.**
`ZstdDecompressor.stream_reader` is a **pull** API: it reads from the source only as the consumer
asks for output, so peak memory is `window_size (2 MiB) + your output chunk + your line carry`, i.e.
**a few MB, independent of file size.** This is *better* than the gzip path's situation, not worse.

⚠️ **But `decompressobj` is the WRONG API here and the difference is a real trap.** `decompressobj`
is **push**: one `decompress(8 MiB)` call returned **all 22.17 MB** at once, because zstd's 2.78x
ratio applied to an 8 MiB input chunk (and its internal buffering) emits everything available. On a
large member that is an unbounded materialization. **The gzip reader's `zlib.decompressobj` shape
must NOT be copied verbatim.** ENG must use `stream_reader` with an explicit `read(n)`.

**Memory verdict for the 15.03 GB container:** at ~24 MB peak this is **not a memory risk at all**,
and it does not interact with task #22's 27.92 GB dedup-`set` problem. **The zstd reader is not
what's tight.** (`read_across_frames=True` must be passed — it is not the default — or a
multi-frame object would stop silently at frame 1. Our sample is single-frame, but 99,674 files were
not all produced identically and I have not sampled them all.)

#### 🔴 A NEW SILENT-CORRUPTION TRAP zstd introduces that gzip does NOT — MEASURED

The `.json.gz` reader's whole correctness story rests on `zlib.Decompress.eof`
(`corpus_read.py:530-533`: *"`decompressobj` exposes `eof` — the flag that distinguishes a complete
stream from a truncated one"*). **I tested the zstd equivalent by truncating the real file to 50%:**

| API | behaviour on a truncated stream |
|---|---|
| `decompressobj().decompress(half)` | returned 11,010,048 bytes, **raised nothing** |
| `.flush()` after it | **raised nothing** |
| `stream_reader(...).read()` loop | read 11,010,048 bytes, hit EOF, **raised nothing** |

**A truncated zstd object decompresses cleanly into a shorter document set and reports success.**
And `has_checksum` is **False**, so there is no frame checksum to save you either.

**The guard exists but must be written explicitly — MEASURED:**

```
COMPLETE    bytes_out=22,168,327   .eof=True
TRUNCATED   bytes_out=11,010,048   .eof=False
```

`ZstdDecompressor.decompressobj()` **does** expose `.eof`, and it is an exact analogue of
`zlib.Decompress.eof`. **`stream_reader` does not expose one.** So either ENG uses `stream_reader`
for bounded memory and adds a separate completeness assertion (bytes consumed == declared size), or
uses `decompressobj` for `.eof` and pays unbounded output. **Property 3 of `_gunzip_lines`'s
four documented correctness properties does not port for free.** This must be in the patch or the
zstd path is strictly less safe than the gzip path it copies.

#### 🔴 PRODUCTION-ONLY FAILURE MODE — LOUD, as requested

**MEASURED:** `pyproject.toml:28-40` declares exactly two runtime dependencies:
```
dependencies = [ "boto3", "numpy<2.5" ]
```
`zstandard` is **not there**, and `[project.optional-dependencies] test = ["pytest>=7.0"]`
(`:42-43`) does not add it. **`zstandard 0.25.0` is installed on this laptop only.**

**Therefore: every local test would PASS and the Batch container would FAIL.** This is the same
shape as the `families/` bug CLAUDE.md documents — a lookup that silently succeeds in a checkout and
fails only in production. Two aggravating specifics:

1. **The failure is not even loud in production.** An `ImportError` inside a reader is caught by
   `main`'s `except (BuildDriverError, BuildError, IngestError)` (`corpus_build.py:1027-1029`) —
   **`ImportError` is NOT in that tuple**, so it propagates as an unhandled traceback and the array
   child exits non-zero. That is at least visible. But if ENG wraps it in a `try: import zstandard /
   except ImportError: raise BuildDriverError(...)` (the pattern already used for `tokenizers` at
   `corpus_build.py:920-922`), it becomes a caught error mid-run — **after other bundles have been
   built and paid for**, which is precisely what `_assert_readable` moved to plan time to avoid.
   If ENG adds zstd, **`_assert_readable` must also assert the import is available**, at plan time.
2. **This is an IMAGE rebuild, not a wheel drop.** `edullm-validator` (top ACTIVE rev per CLAUDE.md;
   the brief cites :14) runs `python -m edullm_data.validate` out of a **digest-pinned ECR image**.
   A new dependency means: bump `pyproject.toml` → rebuild the image → new digest → **re-register the
   job definition**. Per the memory note `deploy-is-image-push-not-wheel`, `__version__` must be
   bumped in all 3 places or the image build fails. **A wheel drop into `_dist/` will NOT deliver
   this change.**

#### Line count of the fix — DERIVED from the code as it stands

**Not one function. FOUR edits, because of the three-table divergence in Q1(e):**

| # | file:line | change | ~lines |
|---|---|---|---|
| 1 | `corpus_read.py` (new fn near `:642`) | `read_jsonl_zst_documents` | **~25 net** |
| 2 | `corpus_read.py:748-752` | add `"json.zst"`/`"jsonl.zst"` to `_READERS` | 2 |
| 3 | `corpus_build.py:127` | add both spellings to `READABLE_FORMATS` | 1 |
| 4 | **`corpus_build.py:908-911`** | add both to the **inline** dict — **the one that actually runs** | 2 |
| 5 | `pyproject.toml:28-40` | declare `zstandard>=0.22` | 1 |

**(1) is small because the body is shared.** `read_jsonl_gz_documents` (`:642-739`) is ~97 lines but
only its **first ~15** are gzip-specific (`_gunzip_lines`, `GZIP_WBITS`). Everything after
`for lineno, line in enumerate(...)` — the JSON parse, the first-record `text_column` guard
(`:707-721`, the one that stops a typo yielding an empty corpus), the `id_column` guard
(`:726-732`), `_json_walk` dotted-path resolution, `_domain_of` — is **format-agnostic and should be
extracted, not duplicated.** The honest shape is: refactor `read_jsonl_gz_documents` into
`_documents_from_lines(lines, ...)` + two thin decompression front-ends. **~25 net new lines,
~60 moved.** Duplicating instead would fork four documented correctness guards, which is how the
`families/` half-fix happened (`families-dir-half-fix` in memory: *"fixed in validate.py, left in
publish.py"*).

**The transport needs NO new code.** `_RangeFile` is an `io.RawIOBase`
(`ingest_reservoir.py:299`), which is already a `read()`-able — so `dctx.stream_reader(rf,
read_across_frames=True)` accepts it **directly**, with the CDN-resolution, 429-backoff, and
exact-count-or-raise logic (`ingest_reservoir.py:365-402`) all inherited unchanged. `_range_chunks`
is not even needed on this path.

#### 🔴 A FIFTH COST THE BRIEF DID NOT ANTICIPATE: the records have INCOMPATIBLE SCHEMAS.

**MEASURED** — first record of one file from each of six directories, decompressed and key-listed:

| directory | record keys |
|---|---|
| `ingredient1-nemotron-synth-qa` | `language, text, url, warc_record_id` — **NO `id`** |
| `ingredient1-dolmino-math` | `dolminos_category, id, metadata, text` |
| `ingredient1-general_reasoning_mix` | `id, metadata, text` |
| `ingredient1-reddit_to_flashcards` | `id, text` |
| `ingredient1-tulu-3-sft` | `id, metadata, text` |
| `ingredient1-wiki_to_rcqa-part1` | `id, text` |

`CorpusSpec` carries **one** `id_column` per row (`corpus.py:219` region), and
`read_jsonl_gz_documents:726-732` **RAISES** — correctly — when `id_column` resolves to nothing:
*"The id is the join key for the id partition and the anti-join (corpus.py:176-179); a line number
would not survive a re-download."*

**So a single registry row cannot cover this repo.** `nemotron-synth-qa` (1,024 files) would need
`id_column: "warc_record_id"` while everything else needs `"id"`. **The mix needs MULTIPLE registry
rows, one per schema family** — which also means multiple `source_label`s, multiple bundles, and
multiple ordinal allocations. That is plan-shaped work, not reader-shaped work, and it is **not**
in anyone's 14B estimate. **Nobody has surveyed all 209 directories' schemas — I sampled 6.**

### Q3b — the QA prefixes, enumerated. **And the QA subset is ~8-10B, NOT 14B. DEVIATION.**

**The card confirms the prefix-selectability claim.** `README.md` @ `f23aa129…` ships a
Source→Category table naming **24 sources**, and **exactly three are `QA (synth)`**:

> | Reddit To Flashcards | QA (synth) | · | Wiki To RCQA | QA (synth) | · | Nemotron Synth QA | QA (synth) |

Those map 1:1 onto directory prefixes with **no classifier**, exactly as claimed:

| prefix | files | MEASURED mean bytes/file (n=20 random, seed 42, HEAD `x-linked-size`) | est. compressed |
|---|---|---|---|
| `data/ingredient1-nemotron-synth-qa/` | 1,024 | 7,778,888 | 7.966 GB |
| `data/ingredient1-reddit_to_flashcards/` | 1,204 | 4,759,792 | 5.731 GB |
| `data/ingredient1-wiki_to_rcqa-part1/` | 8,629 | 211,033 | 1.821 GB |
| `data/ingredient1-wiki_to_rcqa-part2/` | 8,628 | 206,824 | 1.784 GB |
| **QA total** | **19,485** | — | **17.30 GB** |

(Sampled, not enumerated: full pagination is 390 tree calls at `q=1000;w=300` and I was already at
`r=994`. Sampling is the deliberate choice under the 429 warning in the brief.)

#### ⚠️ DEVIATION PROTOCOL — the 14.0B row cannot be met from the QA prefixes.

**(a) Plan claim:** `FINAL-DATASET-REPORT.md:100` — *"**AI2 dolma3 midtraining mix** (QA-bearing) |
14% | 14.0B | 100.0B | 0.14"*. Pool stated as **100.0B**, draw **14.0B**, epochs **0.14**.

**(b) Countervailing evidence — DERIVED from three MEASURED inputs:**
- compressed 17.30 GB (sampled HEAD, above)
- **× 2.78** decompression ratio (MEASURED on the real file: 7,973,126 → 22,168,327 bytes)
- **× 0.9229** text-field share of decompressed JSON (MEASURED: 20,459,498 text bytes of 22,168,327
  total, over all 9,130 records of that file)
  → **44.39 GB of actual text.**

Converted with **this repo's own measured chars/token band**
(`artifacts/reservoir/chars-per-token.json`, dolma2-tokenizer, 400 docs/source):

| chars/token | source of the constant | QA tokens |
|---|---|---|
| 4.31 | the file's `_mean` | **10.30 B** |
| 4.62 | its `fineweb-edu` row (closest analogue to synth QA prose) | **9.61 B** |
| 5.58 | its `_worst_observed` | 7.96 B |
| 6.00 | `_CHARS_PER_TOKEN` as set in `corpus_build.py` | 7.40 B |

**The QA-labelled content is ~8-10B tokens. The report asks for 14.0B.**

**(c) Numbers it moves:** the 14.0B row is **~1.4x oversubscribed against its own category**. Meeting
14.0B from the QA prefixes alone requires **~1.4 epochs of every QA document**, or it requires
pulling in non-QA prefixes (`general_reasoning_mix`, `tulu-3-sft`, `dolmino_1-flan`) — at which point
the row is no longer "QA-bearing" and the "pool available 100.0B / epochs 0.14" cells are
**describing the whole 100B mix, not the QA subset the row's own label names.** That is a
**denominator error of exactly the class CLAUDE.md warns about**: the pool figure and the label
belong to different scopes.
**Note this does NOT make the epoch table wrong** — the 100.0B pool cell is true *of the mix*. It
makes the row's **label** wrong, and the label is what a builder would filter on.

**(d) Blast radius:** small if caught now, and it **strengthens the DROP case rather than weakening
it** — the row was buying less than it claimed. If the CEO keeps the row, the registry needs either
(i) a broadened prefix set with a re-derived category label, or (ii) a reduced target of ~9B with the
remaining 5B redistributed. My §"DROP cost" table above already shows every non-FinePDFs source has
≥0.44 epochs of headroom, so **redistributing 5B is even safer than redistributing 14B.**

**Correcting myself in place:** my Q3 section above reported **209** directories against the
brief's/plan's **323** (`IMPLEMENTATION-PLAN.md:1892`). I stand by 209 as the measured count at
`f23aa129…`. Additional structure now measured: the mix has **two ingredients**, and they are
**not parallel** — `ingredient1-*` has 22 distinct families (all 24 card sources), while
`ingredient2-*` has only **47 directories across 3 families** (`code-meta-reasoning`,
`common_crawl-high-quality_19*`, `common_crawl-high-quality_20*`). **`ingredient2` contains NO QA
directories at all.** So a prefix selector must say `ingredient1-`, and a naive `*-nemotron-synth-qa`
glob is fine only by accident.

### Q3c — COPY is dead for the WHOLE dolma3 family, not just this repo. MEASURED.

I widened the check rather than stopping at one repo, because the brief allowed "or a sibling repo
covering the same content." **Every dolma3 repo AI2 publishes ships `.jsonl.zst` text.** Full
`siblings` extension census per repo, each at its own pinned sha:

| repo | pinned sha | files | extensions |
|---|---|---|---|
| `allenai/dolma3_dolmino_mix-100B-1125` | `f23aa129fda8335ba9760057bcc1f0c02f3d068b` | 99,676 | **99,674 `.jsonl.zst`** + 2 control |
| `allenai/dolma3_dolmino_mix-100B-1025` | `f23942ae8a8114af6e992efe8188ce8c531acd16` | 71,093 | **71,090 `.jsonl.zst`** + 3 |
| `allenai/dolma3_dolmino_mix-10B-1025` | `275d00e588098d2658e36be74ffd010dfb54d8a6` | 1,117 | **1,113 `.jsonl.zst`**, 1 `.jsonl` + 3 |
| `allenai/dolma3_dolmino_pool` | `091589c58ab6acc180d71017ecea8201776f05b2` | 87,980 | **85,033 `.jsonl.zst`** + 1,428 `.py` + 1,428 `.pyc` (a vendored venv) |
| `allenai/dolma3_mix-150B-1025` | `afa92bfb22366821c5e6cd427cdd036b34b713ef` | 6,084 | **6,081 `.jsonl.zst`** + 3 |
| `allenai/dolma3_mix-6T` | `689a3ea2d8217e64d73a5058913fa43ad15e81aa` | 63,913 | **63,911 `.jsonl.zst`** + 2 |
| `allenai/dolma3_pool` | `6462556697df1a8f5c953727e9c686629ad98b68` | 99,587 | **99,585 `.jsonl.zst`** + 2 |
| `allenai/dolma3_longmino_mix-100B-1125` | `28fea4330d8f8e27221010d42c4bc53ba9ec3236` | 59,129 | **59,126 `.jsonl.zst`** + 3 |

**Not one `.npy`, `.bin`, `.u32le.bin`, `.csv.gz`, or `.parquet` in ~487,000 files across eight
repos.**

**I therefore SCOPE-CORRECT the memory note `ai2-dolma3-shards-are-byte-compatible` (which the brief
restates as a "verified fact").** Its technical content is not refuted — dolma3-tokenizer IS dolma2
and AI2's pre-tokenized shards ARE headerless raw uint32 LE. **What is refuted is its reach.** The
byte-compatible shards live in AI2's *training-data* releases (the `olmo`/`dolma` numpy drops the
150B corpus in our legacy bucket came from), **not in the `dolma3_*` HF dataset repos.** Anyone
reading that note as "dolma3 content is always a copy away" will be wrong for **every** dolma3 mix
and pool on the Hub. The `.csv.gz` sidecar story likewise does not apply here — there are none.

**⇒ COPY is not an option. The choice is FIX or DROP.**

---

## GAP 1 — W4-ZSTD RECOMMENDATION

**FIX — but a bigger fix than "add a zstd reader", and only if the CEO still wants the row after
reading the Q3b sizing correction. DROP remains arithmetically safe and is the right call if the
row's value was the 14B, not the QA-ness.**

**Why not COPY:** impossible. ~487k files across 8 dolma3 repos, 100% text. (Q3c)

**Why not DROP by default:** the DROP-cost table earlier in this file shows the epoch property
survives (worst redistributed source 0.558 vs FinePDFs-Edu's unchanged 0.900), so DROP is *safe* —
but it costs the only QA-shaped, ungated, `odc-by` content in the stage-2 design, and
`FINAL-DATASET-REPORT.md` §6 argues that shape is the *point* of the cooldown. Safe ≠ free.

**What FIX actually costs — the honest total, in three tiers:**

| tier | work | size |
|---|---|---|
| **A. the reader** | new `read_jsonl_zst_documents` on `stream_reader(_RangeFile, read_across_frames=True)`, sharing the extracted line-loop; **+ an explicit truncation guard**, because zstd raises nothing on a half object and `has_checksum=False` (Q2) | **~25 net lines, ~60 moved** |
| **B. the three tables** | `READABLE_FORMATS` (`corpus_build.py:127`), the **inline dict** (`corpus_build.py:908-911`) — *the one that actually runs* — and `_READERS` (`corpus_read.py:748-752`). **Patching only `_READERS` changes nothing**, because `read_documents` is dead code. Fix the `jsonl.gz` divergence in the same pass. | **5 lines, 3 files** |
| **C. 🔴 the parts nobody has costed** | (i) `zstandard` → `pyproject.toml` → **ECR image rebuild + job-def re-registration**, not a wheel drop; (ii) `_assert_readable` must also assert the *import* at plan time or the dependency becomes a mid-run failure; (iii) **the records have incompatible schemas** — `nemotron-synth-qa` has **no `id` field** (only `warc_record_id`) while the others have `id`, and `CorpusSpec` carries **one** `id_column`, so **the mix needs multiple registry rows**, hence multiple `source_label`s, bundles and ordinal allocations; (iv) only **6 of 209** directories have been schema-sampled. | **unbounded until (iv) is done** |

**Tier C(iii) is the real cost and it is plan-shaped, not reader-shaped.** ENG can ship A+B in an
afternoon and the row still will not build.

**Order of operations I recommend:**
1. **CEO decision first, on the Q3b correction** — the row buys ~9B of QA, not 14B. Decide whether
   the row is "14B of tokens" (→ DROP or re-cut; the reader is not worth it) or "the QA shape" (→
   FIX, retarget to ~9B, redistribute 5B).
2. **Only if FIX:** survey all 209 directories' first-record schemas before writing any registry row.
   That is one cheap scripted pass and it sets the row count. Doing it after the reader lands is how
   the reader ships and the build still fails.
3. **B before A.** The three-table divergence is a live bug *today* (a `jsonl.gz` row is silently
   droppable despite a working reader) and is worth fixing regardless of the zstd decision.
4. **Independently of all of the above — fix the `--allow-unreadable` blindness (Q1b).** Add an
   `"excluded_unreadable"` key to `plan_document`'s output dict, modelled exactly on the
   `"no_val_split"` key twelve lines above it (`corpus_build.py:261-267`) whose own comment demands
   that omissions be *"recorded in the plan, not just warned about, so the omission is auditable
   afterwards."* **~6 lines.** It does not depend on the zstd decision, it does not depend on the CEO
   decision, and it converts a silent one-way door into an auditable one. **This is the highest
   value-per-line item in the whole gap.**

**Summary of the four corrections this section makes to the standing plan:**
1. The QA subset is **~8-10B, not 14.0B** (`FINAL-DATASET-REPORT.md:100`). — DEVIATION, Q3b
2. The mix has **209** directories, not 323 (`IMPLEMENTATION-PLAN.md:1892`); `ingredient2` has no QA. — Q3/Q3b
3. `ai2-dolma3-shards-are-byte-compatible` **does not reach the `dolma3_*` HF repos** — 8 repos,
   ~487k files, 100% text. — Q3c
4. `read_documents`/`_READERS` is **dead code** on the build path; a zstd patch there is a no-op. — Q1e

**— end W4-ZSTD —**

### W4-ZSTD — citation errata (self-audit; every line number above re-verified with `sed -n Np`)

I re-read every line I cited rather than trusting my notes. **Four were off. Corrected here, in
place. The claims they support are all unchanged** — each correct line says what I said it says.

| I wrote | correct | what is actually there |
|---|---|---|
| `corpus_build.py:1027-1029` (`main`'s `except` tuple) | **`corpus_build.py:980`** | `except (BuildDriverError, BuildError, IngestError) as exc:` — `ImportError` is still NOT in it. Claim stands. |
| `corpus_build.py:920-922` (the `tokenizers` ImportError pattern) | **`corpus_build.py:633`** | `raise BuildDriverError(f"the \`tokenizers\` package is required to tokenize: {exc}")` — still the pattern ENG would copy. Claim stands. |
| `corpus_read.py:707-721` (first-record `text_column` guard) | **`corpus_read.py:710-724`** (comment opens `:710`, raise text at `:720`) | Claim stands. |
| `corpus_read.py:726-732` (`id_column` guard) | **`corpus_read.py:730-736`** (raise text at `:732`) | Claim stands. |

**Verified exact and unchanged:** `corpus_build.py:127` (`READABLE_FORMATS = frozenset({"parquet",
"json.gz"})`), `:637` (`def _cmd_plan`), `:643` (the silent filter), `:261` (*"Recorded in the plan,
not just warned about…"*), `:263` (`"no_val_split": sorted(`), `:908-911` (the inline reader dict),
`:949` (`p.add_argument("--allow-unreadable", action="store_true",`);
`corpus_read.py:642` (`def read_jsonl_gz_documents(`), `:748-752` (`_READERS`, closing `}` at 752);
`ingest_reservoir.py:299` (`class _RangeFile(io.RawIOBase):`);
`FINAL-DATASET-REPORT.md:100` (the 14.0B row, verbatim); `IMPLEMENTATION-PLAN.md:1892` (the "323
directory names" claim I measured at 209).
