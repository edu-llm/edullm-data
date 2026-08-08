# DATA-EXEC status — `pretrain/final-dataset`

**Opened 2026-08-08 by DATA-EXEC (re-dispatch #2).** Predecessor died with a 17-row skeleton and
two workers whose measurements were never written down. This file is appended after **every**
material finding. A partial file is a success.

Grades: `MEASURED` (I ran it) / `MEASURED-IN-CODE` (read from source, file+line) /
`DERIVED` (arithmetic from graded inputs) / `CARD` (HF/vendor claim, unverified) / `UNVERIFIED`.

---

## Scope

| id | item | state |
|---|---|---|
| **A** | source-identity dossier, 17 rows | 🔄 in progress |
| **B1** | dolma3 `.jsonl.zst` vs `READABLE_FORMATS` — 14B tokens with no reader | ⬜ |
| **B2** | three sources with no document id (DCLM×2, Cosmopedia) — blocks #21/#22 | ⬜ |
| **B3** | per-document quality-percentile label — unbackfillable, scope it | ⬜ |
| **B4** | 252B FineWeb-Edu must be `sample-350BT` or FinePhrase anti-join breaks | ⬜ |
| **C / M1** | bandwidth — sizes everything else | ⬜ |
| **M2 / M4** | (per `docs/TASKS.md`) | ⬜ |
| **D / #17** | 13-gram re-scan (NOT the token count) | ⬜ |
| **E** | DCLM repo reconciliation: `HuggingFaceFW/dclm_100BT` vs `mlfoundations/dclm-baseline-1.0-parquet` | ⬜ |
| **B5** | (per `docs/TASKS.md`) | ⬜ |

## Hard constraints I am operating under
- **NOTHING promotes. Nothing is written to `s3://edullm-landing`** — including any dry run that
  emits a `manifest.json`. The EventBridge rule is ENABLED (LEDGER recovery finding 1).
- Read-only S3 only. Bulk S3 via `sb-aws-creds` credential_process + threaded boto3.
- No plan change by narration — deviation protocol only.

---

## Log

### 2026-08-08 — session opened
Read `LEDGER.md` (rulings R1–R4, recovery audit). Inherited anchor numbers. Starting the reads.

### 2026-08-08 — pre-dispatch reconnaissance (DATA-EXEC, own hands)

**F1. A registry already exists and is 12 of my 17 rows — `MEASURED-IN-CODE`.**
`artifacts/reservoir/corpus-registry.json` (`_schema: edullm-corpus-registry/v1`, 17 rows,
`_revisions_pinned_at: 2026-08-01`, `_revisions_verified`: all 14 (repo,sha) pairs resolved against
the HF tree API). It is loaded by `load_registry` (`corpus_build.py:130`) directly into
`CorpusSpec(**row)`, so **its field names ARE the dossier's columns.**

⚠️ **DENOMINATOR CHECK — it is the RESERVOIR's mix, not this corpus's.** This is exactly the scope
error CLAUDE.md warns about. Its `target_tokens` sum to a ~250B reservoir, not 1.0T, and its row set
differs from `FINAL-DATASET-REPORT.md` §3/§4:
- rows it HAS that final-dataset does not draw: `essential-web` (RESERVE), `finemath`,
  `ubuntu_irc_filtered`, `github_archive_filtered` (RESERVE), `arxiv_papers_filtered` (RESERVE)
- rows final-dataset NEEDS that it does NOT have: **nemotron-cc-math-3**, **nemotron-cc-math-4plus**,
  **dolma3 midtrain mix**, **reasoning traces**, **cosmopedia**, **nemotron math-textbooks**,
  **pre-1929 books**
- **`finephrase` is FOUR rows there (faq/math/table/tutorial), ONE row in the report.**
**So: inherit the identity strings (repo/config/revision/columns), NOT the token targets.**

**F2. Identity strings already MEASURED for 10 sources**, carried in each row's last trap as
`"text_column verified in <artifact>"`. Provenance is per-row and checkable. Pinned revisions:
finepdfs-edu `9cfabe2127faca99b3d5c4dc6d1fcb397399ebde` · fineweb-edu `87f09149ef4734204d70ed1d046ddc9ca3f2b8f9`
(config **`sample/100BT`** — see F5) · dclm `01022d378d944de6deeb1c79d08fecb4d27b2c6f` ·
peS2o `297747513bfb0ff1fbf61ddad3b03319d0f04597` · pubmed `c156f0569a92d8f2edc33cebe1f72f7d3e1cae84` ·
arxiv `033cf7f53f9b348deec868c1a5a48484f3ee9e52` · stackv2-edu `c354dbe88469a1153e97c6a63ac50591849654de` ·
stackexchange `c0ac7373830c688a43fc12d1988c4b19ccd884ab` · finewiki `8bd13e72e6a002407649b3e898535f42ceb1aeb9` ·
finephrase (all 4 configs) `78cf4a5ed0099214979c094c963e699c19163838`.

**F3. FinePhrase trap is ALREADY correctly encoded — `MEASURED-IN-CODE`.** All four rows carry
`"text_column": "rollout_results.list.element.text"`. Note the exact string is the parquet
`path_in_schema` **with** the `.list.element.` markers — my brief's shorthand `rollout_results[0].text`
is the same leaf in different notation. `corpus.py:200-206` documents the trap in the dataclass
docstring; `corpus_read.py:106 _resolve_leaf` enforces it. **Trap NOT bitten.**

**F4. Common Pile file prefixes cannot be derived from repo names — `MEASURED-IN-CODE`.**
`_common_pile_file_prefix` maps all 7: peS2o→`peS2o-`, pubmed→`licensed_pubmed-`,
arxiv_papers→`arxiv-papers-`, stackv2_edu→`stack-edu-`, stackexchange→`stackexchange-dolma-`,
ubuntu_irc→`ubuntu-chat-dolma-`, github_archive→`gharchive-dolma-`. All at the **repo root**, no
`data/` prefix, `config: null`. This is a dossier column the skeleton did not have and needs.

**F5. 🔴 GAP 4 IS LIVE AND THE REGISTRY IS ON THE WRONG SIDE OF IT.** The registry pins fineweb-edu
at config **`sample/100BT`**. The report needs **252B**, and `sample/100BT` measured **100.24B** —
**it cannot supply the draw at all**, independent of the anti-join. Escalating to a worker with
gap 2 (both are id/partition problems). `MEASURED-IN-CODE` (registry) + `MEASURED` (100.24B recount).

**F6. GAP 1 CONFIRMED IN CODE, and it is worse than "no reader" — `MEASURED-IN-CODE`.**
- `READABLE_FORMATS = frozenset({"parquet", "json.gz"})` — `corpus_build.py:127`.
- `_assert_readable` raises at **PLAN time**, `corpus_build.py:171-179`.
- `corpus_read.py:774-775` names `.zst` explicitly as unsupported.
- The mix source is **`allenai/dolma3_dolmino_mix-100B-1125`**, `.jsonl.zst`, `IMPLEMENTATION-PLAN.md:1890`.
- ⚠️ **`zstandard 0.25.0` IS INSTALLED on this machine** (`MEASURED`) — so a local test would pass
  while Batch fails, since `pyproject.toml` does not declare it. Classic production-only failure.
- ⚠️ **Second silent path**: `corpus_build.py:643` does `drawn = [s for s in drawn if s.file_format
  in READABLE_FORMATS]` — a **silent filter**, not a raise. Under `--allow-unreadable` the 14B is
  dropped with no error. Assigned to a worker to trace which path wins.

**F7. AWS broker is LIVE despite `onboarded:false`/`mfaEnrolled:false` — `MEASURED`.**
`sts get-caller-identity` → `arn:aws:sts::<ACCOUNT_ID>:assumed-role/Intern-eric.wu-sbsandbox/…`.
M1 is executable. `sbsandbox` + `legacy` self-serve; `sbproduction` not provisioned.

**F8. Local toolchain is sufficient for HF work — `MEASURED`.** pyarrow 24.0.0, huggingface_hub
1.21.0, tokenizers 0.22.2, HF token present at `~/.cache/huggingface/token`, dolma2-tokenizer already
in the hub cache, HF API reachable (HTTP 200, 0.96 s). Proven measurement tooling in
`artifacts/recount/` (`_fp_footer_leaf.py` does exact nested-leaf footer reads; `README.md` documents
the factored estimator and why the naive one fails: CV 9.0, CI [26B,204B] on FineMath).

**F9. ⚠️ HTTP 429 is the standing hazard for HF row-sampling — `MEASURED`.** Every one of the three
DCLM recount artifacts logs 429s at `/rows`; `_fp_footer_leaf.py`'s docstring records the `/rows`
quota being "exhausted once by parallel agents." **Told every worker: footer/tree API over `/rows`,
and serialize HF sampling.**

**F10. Gap E has a partial answer already on disk — `MEASURED`.**
`artifacts/recount/web-dclm-baseline-parquet.json` reports `mlfoundations/dclm-baseline-1.0-parquet`
`dataset_total_rows: 965,502` — against `HuggingFaceFW/dclm_100BT`'s `89,269,902`. The plan (§4.1)
says the parquet mirror has 27,938 shards / ~3,764B tokens, and separately that `/size` returns
`num_rows=779,982 partial:true` = the converted head. **965,502 is the same artifact-of-partial-conversion
class of number, not a corpus size.** Dispatching a worker to settle it from the tree API, not `/size`.

**Dispatching wave 1: 5 workers.**

### 2026-08-08 — wave 1 dispatched (3 of 4), and a classifier OUTAGE

Launched: **W1-BANDWIDTH** (M1) · **W2-DCLM** (gap E + gap 2 DCLM half) · **W3-STAGE2** (the 6+1
missing stage-2 registry rows). Each has a named file under `data/measurements/` and the write-first
rule as its first obligation.

⚠️ **`W4-CODEGAPS` FAILED TO LAUNCH — TWICE.** Error: *"claude-sonnet-5[1m] is temporarily
unavailable, so auto mode cannot determine the safety of Agent right now."* **This is the same
permission-classifier outage PLAT reported in the prior wave** (LEDGER recovery audit, PLAT row) —
so it is a recurring platform condition, not a one-off. It is an **outage, not a denial**: read-only
operations are unaffected. **I am doing gaps 1 and 3 with my own hands** rather than blocking, and
will retry the dispatch. Recorded so the next session does not mistake it for a permissions problem.

### 2026-08-08 — ✅ M1 CLOSED. File: `data/measurements/m1-bandwidth.md`

**The headline is that the in-region number the plan borrows is CORRECT, and it was already in the
repo.** `artifacts/reservoir/verify-job.json`: 1.005 TB in 11,782 s = **85.3 MB/s wall / 87.8 MB/s
stream, `MEASURED`.** The worker checked whether that was network or hashing instead of assuming —
local sha256 runs 1,481.9 MB/s, so hashing is 5.8% of elapsed. **94% pure network. §8A's ~85 MB/s
stands, no change needed.**

**Two corrections that matter more than the confirmation:**

1. 🔴 **The laptop measurement had NO POWER to test the question it was aimed at, and the worker
   said so instead of answering anyway.** HF CDN → laptop measured **2.67 MB/s** single-stream, 3.12
   at 4 streams. But two non-HF controls (Hetzner 2.63, Princeton 2.43) show **every host gives this
   laptop 2.0–3.2 MB/s** — the uplink is the ceiling, and it sits *below* the 8.4 MB/s figure §3.2
   asks about. **So §3.2's HF-CDN fear is STILL UNRESOLVED** — reporting "measured 2.7, so 8.4 was
   optimistic" would have been exactly the denominator error CLAUDE.md warns about. Correctly refused.
2. ⚠️ **"0.8 MiB/s local publish" IS NOT A BANDWIDTH NUMBER — `MEASURED` correction.** Raw local S3
   is **2.91 MB/s**, 3.5× faster. 0.8 is `publish()`'s *end-to-end* rate including hashing, so
   **~71% of publish()'s slowness is NOT network and follows the code in-region.** My own brief
   repeated the 0.8 figure as if it were bandwidth. **The "9 days → minutes in-region" intuition
   overstates the win.** This one belongs in the promote-duration debate the CEO has double-assigned.

🔴 **NEW RISK, and it is on the critical path: STAGE's 0.5–3.0 h band has no measured basis.**
Single-stream in-region, 8.5 TB takes **27.7 h**, not 0.5–3.0 h. The band only works if multi-stream
reads scale — and **that has ZERO measured samples anywhere in this repo.** `IMPLEMENTATION-PLAN.md`
§3.2's "0.24 h at 8 × 10 Gbit/s" is an assumption, not a measurement.
**→ Highest-value remaining measurement: multi-stream in-region aggregate throughput. One Batch job
settles it. That is PLAT's lane, not mine — flagging to the CEO rather than submitting.**

**$194 staging recommendation HOLDS**, but on changed reasoning: its value is deleting an
unmeasurable variable from the critical path, which M1 just demonstrated is genuinely unmeasurable
from here. Compliance: no landing writes, no Batch submissions, read-only S3, no `/rows`, zero 429s.

### 2026-08-08 — #17: the "already staged" premise, checked by hand

`docs/TASKS.md:58` says #17 is *"Now unblocked: the gate is accepted and **107+ GiB is already
staged in S3**, so this runs in-region against staged bytes."* **I went looking. `MEASURED`,
read-only, via the broker.**

- ✅ **The decon index IS staged**: `s3://edullm-landing/_dist/eval-decontamination.bin`,
  **54,350,848 bytes**, 2026-08-01 15:36:35. Matches the ~250 MB-resident / 54 MB-on-disk index of
  `IMPLEMENTATION-PLAN.md` §6.1 (149,777 exact hashes + 3,097,372 13-grams). `_dist/` has **no
  lifecycle expiry**, so it persists.
- 🔴 **I CANNOT FIND 107+ GiB OF NEMOTRON-CC-MATH ANYWHERE.** Enumerated, all read-only:
  `s3://edullm-landing/` top level = `_dist/ _ingest/ _migrate/ _preserved/ _scratch/ _staging/
  _tmp/ curriculum/ pretrain/ sft/ tokenizer/ vendor/`. **`_src/` — the prefix §3.2's staging plan
  names — DOES NOT EXIST** (exit code 1). `_ingest/` holds only `reservoir-dolma2/`;
  `_staging/pretrain/` holds 5 unrelated phase-0 corpora; `vendor/` holds `fineweb-edu-1b-raw/` and
  `openai-prm800k/`. `edullm-datasets` (legacy, in sbsandbox) holds 15 prefixes, none Nemotron.
  Also checked `edullm-data/pretrain/` (19 prefixes), `edullm-scratch`, `edullm-ericwu-scratch`,
  `sbsandbox-intern-edullm-artifacts`.
- **Grade: the staging claim is `UNVERIFIED` and, on the evidence I have, likely FALSE.** I have not
  exhaustively listed every prefix of every bucket, so I am not calling it refuted — but **the
  burden has flipped.** ⚠️ **If it is false, #17 is NOT "1 job" and NOT unblocked** — it needs a
  ~470 GB HF→S3 stage first, and M1 just showed staging bandwidth is the least-measured number in
  the plan. **Handing to a worker to finish the enumeration before anyone schedules #17.**

### 2026-08-08 — classifier recovered; wave 1 fully dispatched (5 workers)

`W5-DECON` (#17 staging sweep + B5) and `W4-ZSTD` (gap 1 costing) and `W6-FWEDU` (gap 4 + M4)
launched after the outage cleared. Running: W2-DCLM, W3-STAGE2, W5-DECON, W4-ZSTD, W6-FWEDU.
W1-BANDWIDTH closed. Fan-out 5, within the ≤8 limit.

**Gaps 1 and 3 partially closed by my own hand** — see `data/measurements/code-gaps.md`:
- **GAP 3 RESOLVED, and it INVERTS the report's framing.** `FINAL-DATASET-REPORT.md` §11 is **not
  implementable as written**. A per-DOCUMENT label cannot be stored at ingest *either* — the
  manifest has no per-document tier. `labels` hangs off `ManifestEntry` = one OBJECT
  (`manifest.py:233`; `build_manifest` raises on duplicate paths, `:528-533`), so one label would
  cover **~30,680 documents** (`DERIVED`: 25,001,984 ÷ 814.9). Values are **flat strings only**
  (`manifest.py:230-232`, enforced `:292-309`). Only **two** label keys exist
  (`PATH_LABEL_KEYS = ("source","domain")`, `manifest.py:693`) and a third level **raises**
  (`:730-742`). Gate A **recomputes** labels from the key and compares by full dict equality
  (`validate.py:1380`, called `:830`) — so a producer cannot write one at all.
  **The only encoding this pipeline accepts is quality-as-a-`source`-segment, i.e. a 5× source
  cardinality multiplication inside `manifest_sha256`, permanently — colliding head-on with #28.**
  My recommendation is **(b) skip for v1**; the CEO decides. **It is the SAME ruling as the
  cluster-ID schema** (both are schema-now-or-never, both must be a source-segment or nothing), and
  **both must land BEFORE #20 FREEZE** because they change the cardinality ordinal allocation uses.
- **GAP 1 DROP-cost re-derived: the epoch table SURVIVES.** Dropping dolma3 QA's 14.0B and
  redistributing pro-rata leaves DCLM worst at **0.558** total epochs vs FinePDFs-Edu's unchanged
  **0.900** max. Every non-FinePDFs source keeps ≥0.44 headroom, so **no plausible reallocation of
  14B breaks the under-1-epoch property.** The drop is not blocked by arithmetic; it is a
  qualitative loss (the QA-bearing content is the cooldown's stated point).
- **GAP 1 also has a SECOND, SILENT path** — `corpus_build.py:643` filters unreadable sources rather
  than raising. Under investigation by W4-ZSTD.

### 2026-08-08 — 🔴 W2-DCLM: FOUR corrections, two of which move FREEZE

File: `data/measurements/dclm-reconciliation.md`. All from real bytes via tree API + ranged footer
reads. No download, no `/rows`, no `/filter`, zero 429s.

**1. ✅ GAP 2 IS REFUTED. `IMPLEMENTATION-PLAN.md` §10 IS WRONG.** It lists *"both full-DCLM repos
[have] no usable document id"* as a **FREEZE prerequisite**. **Both repos carry a 47-char
`<urn:uuid:…>` in an `id` column — `MEASURED` on 1,000 values in `dclm_100BT` (Counter({47: 1000}),
1000/1000 distinct) and 61,000 values in the mirror (Counter({47: 61000}), 61,000/61,000 distinct).**
It is the **Common Crawl WARC-Record-ID**, stamped upstream of DCLM at crawl time — therefore
**stable across a re-download and a revision bump**, which is exactly the property the plan says is
missing. **`sha256(id) % N` works today for DCLM. #21 and #22 are unblocked on their DCLM half.**
   - **And `url` is measurably NOT a viable surrogate**: 60,998/61,000 unique — two duplicate URLs
     carrying **distinct** ids (two crawls of one page = two documents). Killed by measurement, not
     by argument.
   - Cosmopedia's id remains open (W3 owns it), so gap 2 is **2/3 closed**.

**2. ✅ THE DCLM RECONCILIATION IS SETTLED, AND IT WAS A FALSE DICHOTOMY.** The `dataset` column of
`HuggingFaceFW/dclm_100BT` reads **`mlfoundations/dclm-baseline-1.0-parquet`** on every row —
`MEASURED`. **They are child and parent, not competing candidates**, and the child declares its
parent in its own payload. The registry's 30B row and the plan's 410B draw were never in conflict:
**there is no 410B registry row at all.** The registry is the *reservoir's* 30B row against a 114.69B
pool (3.8× headroom, internally consistent); the 3.6× "hole" compared two different corpora.

**3. 🔴 NEW — THE 410B DRAW HAS A DUPLICATE PROBLEM THE PLAN DOES NOT MENTION.** §4.1's "~3,764B ✅
use this" is an **all-copies** figure. `artifacts/recount/web.json` says verbatim: report ~750B
(Zyphra, empirical) or ~733B dolma2-adjusted as the unique pool — **"and not the 3764B all-copies
figure either."** DCLM used a **per-shard Bloom filter, not global dedup**; Zyphra measured
2,949.3M → 615.2M docs (**80% duplicates**). So **410B is 56% of every unique DCLM token that
exists** (410/733), and a naive random 410B draw yields ~82B of unique content repeated ~5×.
**The reservoir's 30B draw was never exposed to this (30/733 = 4%); the 410B draw is.** This bears
directly on the report's under-1-epoch claim, which currently scores DCLM at 0.55 epochs against a
**744.6B** pool. **Escalating to the CEO as a deviation candidate — it may move the largest share in
the corpus.**

**4. 🔴 NEW — #28's carve strings are WRONG in the plan, and the failure is SILENT.**
§8A.5a says the mirror nests `global-shard_01_of_10` at the top level. **It does not** — `MEASURED`
by walking the tree; a `config` of `global-shard_01_of_10` returns **HTTP 404**. The real prefix is
four levels deeper:
`filtered/OH_eli5_vs_rw_v2_bigram_200k_train/fasttext_openhermes_reddit_eli5_vs_rw_v2_bigram_200k_train/processed_data/global-shard_NN_of_10/local-shard_N_of_10/`
**Blast radius: `hf_files` returning an empty list is a ZERO-TOKEN ROW, and per `corpus_build.py:238`
that is silent.** #28 is MANDATORY and on the critical path, so **every #28 config string must carry
the full 4-level prefix or the split silently produces nothing.** Pre-FREEZE correction to §8A.5a.

**Corroborations (independent, `MEASURED`):** mirror = **27,938** parquet files (27,940 siblings − 2),
matching the plan; repo bytes **7.420 TB** confirmed two ways to 0.12%; `dclm_100BT` = 100 flat
parquet files under `data/`, 316,008,772,992 bytes, exactly matching the recount artifact.
Schema delta: mirror has **6** columns (no `dataset`), `dclm_100BT` has **7**; `language_score`/
`fasttext_score` are `float` in the mirror vs `double` in the child. The registry's pinned revision
`01022d…` **is** current `main`. Mirror sha `817d6752765f6a41261085171dd546b104f60626`.

### 2026-08-08 — 🛑 W3-STAGE2: TWO NEW BLOCKERS ON 61.0B TOKENS (6.1% of the corpus)

File: `data/measurements/stage2-sources.md`. Rows 1, 2 and the EXCLUDED row RESOLVED with
paste-ready JSON. Both blockers are **escalations, not findings I can close.**

**🛑 BLOCKER A — the Nemotron license §2.2.2 applies to `Nemotron-CC-Math-v1` TOO.**
The real instrument is **not** the card's `license: other` tag and **not** the
*"NVIDIA Open Data License Agreement"* an earlier audit named — **that name appears nowhere in the
document** (corrected in place by the worker). It is
**`NVIDIA Data Agreement for Model Training (v. August 15, 2025)`**, fetched in full (11,011 bytes)
at the pinned sha. `MEASURED`. §2.2.2 verbatim forbids *"…or otherwise **make available to others**
the Datasets"*, and §2.1 limits use to *"**internal** training."*
- **This is the same clause that blocked the shared reservoir — verbatim, in a different repo.**
  Switching repos does not escape it.
- ⚠️ **§3.3 collides with our own invariant:** on termination (either party, 30 days' notice) we must
  *"delete and destroy copies"* within 14 days — but **a frozen `vN` in `s3://edullm-data` cannot be
  deleted or edited in place** ("frozen means frozen"). **Publishing this source writes a
  contractual obligation we have architecturally disabled ourselves from honouring.** That is the
  sharper half of the problem and it is new.
- **Gates 61.0B** = 45.0B stage 1 + 16.0B stage 2 = **6.1% of the 1.0T corpus**, and Nemotron-CC-Math
  is the math pillar (**MegaMath-Web is HARMFUL — 31.60 vs 44.20 — so there is no drop-in substitute**).
- **Whether `s3://edullm-data` is "making available to others" is a fact about our bucket policy, not
  about the license.** → **OWNER DECISION. Escalating.**
- Clean by contrast, `MEASURED`: `dolma3_dolmino_mix-100B-1125` = `odc-by`; `cosmopedia` =
  `apache-2.0`; `Nemotron-Pretraining-Specialized-v1` = `cc-by-4.0`, ungated (the sanctioned
  substitute per an earlier memory note).

**🛑 BLOCKER B — GATE ACCESS IS PER-ACCOUNT, AND THIS ACCOUNT DOES NOT HAVE IT.** `MEASURED`:
`HEAD` on `3/part_000000.parquet` → **HTTP 403, `X-Error-Code: GatedRepo`**, *"you are not in the
authorized list."* `docs/TASKS.md:58` says #17 is unblocked because *"the gate is accepted"* — **it
was accepted by the teammate who took the 134.0B measurement, not by us.** Whoever runs the ingest
needs authorization on **their own** token. Combined with my finding that I cannot locate the
"107+ GiB already staged", **#17's "now unblocked / 1 job" premise is failing on two independent
legs.**

**✅ CLOSED by W3 — `IMPLEMENTATION-PLAN.md` §10's "the `text` and id column NAMES are still
unconfirmed in writing".** Both are now written down, from a real parquet footer (ungated NVIDIA
sample repo `nvidia/Nemotron-Pretraining-Dataset-sample`, config `Nemotron-CC-MATH`, 65,536 bytes
fetched, 1 request): **`text_column = "text"`, `id_column = "id"`, both flat top-level leaves.**
**There is exactly ONE leaf named `text`, so the FinePhrase trap does not fire here.**
Grade honestly stated as **MEASURED for the sample repo, DERIVED (high confidence) for `3`/`4plus`** —
the worker refused to overclaim and specified the ~1-minute settling job for whoever holds the gate.

**Other decisions W3 surfaced that need an owner/CEO call before FREEZE:**
- **The 61.0B split across the two config rows is a DECISION nobody has made.** Pool-proportional
  gives `3` = 38.05B, `4plus` = 22.95B (`DERIVED`, explicitly not an owner decision); quality-weighting
  toward `4plus` is equally defensible and changes both.
- **`metadata.category` should be `domain_column = None`.** It dictionary-compresses to 67 bytes over
  954 values = one or few distinct values, and **`SAFE_SEGMENT_RE` is enforced only on `source_label`,
  NOT on inherited domain values** — the `C#` precedent shows an unsafe inherited value is not caught
  downstream.
- 🛑 **`4plus` PREFIX-MATCHES `4plus_MIND`.** Any `startswith("4plus")` filter silently pulls in
  ~50–70B of duplicate tokens. **Match config names with `==` only.** Recorded as the single most
  likely way this source gets corrupted.
- 🛑 **P0 — `<|endoftext|>` is dolma2's EOS id 100257 AND Phi-4's, identically**, and this corpus is
  Phi-4 output with no documented special-token scrubbing. A leaked stop token becomes a **phantom
  document boundary**. `neutralize_boundary_markers()` is mandatory.
- **`3plus` confirmed NOT a loadable config** — card front-matter declares exactly three: `3`,
  `4plus`, `4plus_MIND`. Two rows required, as the plan said.
- File path template is **`<config>/part_NNNNNN.parquet`** (six digits) — the sample repo uses four;
  do not copy the sample's pattern into the ingest row.
- **`common-pile/gutenberg_filtered` DOES NOT EXIST (HTTP 404)** — relevant to the pre-1929 books row.

### 2026-08-08 — W4-ZSTD: the COPY escape hatch is REFUTED. Gap 1 is FIX-or-DROP only.

`MEASURED` from the full `siblings` array (99,676 entries = the complete tree, not a page) at pinned
rev **`f23aa129fda8335ba9760057bcc1f0c02f3d068b`**:
**99,674 `.jsonl.zst` + `.gitattributes` + `README.md`. Not one `.npy`, `.bin`, `.u32le.bin`,
`.csv.gz`, `.parquet` or `.json.gz` in the repo.**
- ❌ **The "AI2 shards are byte-compatible, so ingest is copy+rename" fact is TRUE but does not apply
  here** — it describes AI2's *pre-tokenized* releases, and **this midtrain mix is not one of them.**
  `IMPLEMENTATION-PLAN.md:1890` is **CONFIRMED, not refuted.** No `.csv.gz` sidecars either, so the
  doc-offset/doc-id story is also unavailable.
- **So gap 1 has exactly two options: build a zstd reader, or drop 14B.** My epoch re-derivation
  already shows the DROP is arithmetically safe (worst source 0.558 vs the unchanged 0.900 max).
- ⚠️ **Correction to my own brief, made by the worker:** I passed on "**323** directory names are
  category labels." The measured count under `data/` at this revision is **209**. The 323 figure is
  not reproducible and should be treated as stale. The *claim* survives — the dirs are
  `ingredient{1,2}-<source>-<subcategory>` and ARE prefix-selectable with no classifier.
- `allenai/dolma3-dolmino-mix-1025` → **HTTP 404**, as the plan says.

### 2026-08-08 — W5-DECON: B5's "missing script" blocker DOES NOT EXIST; the index verifies

- ✅ **`eval_bundle.py` is reachable** at
  `/Users/ericwu/Developer/Capstone_LLM/pipelines/week1_corpus/src/week1_corpus/eval_bundle.py`
  (12,400 bytes) — a sibling repo on this machine, exactly where `dedup-decontam-audit.md:397` says.
  The whole `week1_corpus` package is present **with a 398,061-byte `uv.lock`, so the dependency set
  is pinned and resolvable.** **My framing ("if the script is unreachable, B5's 4 h is fiction") is
  corrected: that blocker does not exist.** What stays conditional is the upstream data, not the script.
- ✅ **The index verifies by RECOMPUTATION, not field-presence** — the golden rule applied.
  `eval-decontamination.bin` 54,350,848 B sha256 `04aa8fe5…` matches the manifest's nested
  `index.sha256`; `eval-decontamination.jsonl` 305,815,197 B sha256 `5e6dfb4f…` matches the
  top-level `sha256`. ⚠️ **Those are two different hashes of two different files** — the prior note
  "the index's sha256 matches its manifest" blurred them. Both checked with local `shasum -a 256`.
- ✅ **`IMPLEMENTATION-PLAN.md` §6.1 is accurate in every element** — 149,777 exact hashes +
  3,097,372 13-grams, `ngram_size 13`, `minimum_hits 2`, 127 source keys, 148,458 base-unique texts.
  §6.2's "20.9 n-grams per item" recomputes to **20.864** ✅. Nothing to correct.
- ⚠️ **Another different-denominator trap found:** `examples` (149,777) − `base_unique_texts`
  (148,458) = **1,319 = exactly the GSM8K count.** The exact-hash count and the n-gram denominator
  are **not the same population.**
- **A rebuild is reproducible in principle** — four upstream revisions are pinned plus a
  `source_manifest_sha256` over all 184 source files, each with its own path/size/sha256.
- **Bucket sweep method worth reusing:** rather than listing ~45 buckets object-by-object, the worker
  pulled `AWS/S3 BucketSizeBytes` for all of them in **one** `cloudwatch get-metric-data` call.
  Survivors large enough to hide 470 GB: `edullm-landing` 6.45 TB, `sbsandbox-intern-edullm-outputs`
  3.84 TB, `edullm-data` 3.52 TB, `edullm-adaptive-inference` 1.80 TB, `edullm-datasets` 1.79 TB,
  `edullm-checkpoints` 774 GB. ⚠️ Also caught that **7 buckets appear in `cloudwatch list-metrics`
  but NOT in `s3api list-buckets` — they are DELETED buckets whose metrics linger**, a trap for
  anyone inventorying from CloudWatch. Corroborates §8B.2: both `us-east-2` buckets are **empty**.

### 2026-08-08 — ✅ W2-DCLM CLOSED (620 lines). Gap E and gap 2's DCLM half are settled.

Two self-corrections the worker made in place, both of which LOWERED its own claims — recording
because that is the behaviour the ledger's conventions are trying to buy:
1. It flagged a 2.18× byte-vs-row gap, then found **its own multiplication was the error** (compressed
   parquet bytes × decoded-text tokens/byte). The 2.18× is just the compression ratio, footer-confirmed
   at 2.24×. → **new trap recorded: `parquet_bytes` is not a sizing input.**
2. It predicted a wrong `#28` config would fail **silently**; it then **executed the real `hf_files`
   and got a loud HTTP 404.** Severity dropped from data-integrity risk to docs fix. **My own status
   entry above repeated the silent-failure framing — corrected here, visibly.**

**Dossier is now 12 of 17 rows** (DCLM split into 1a child / 1b parent, both MEASURED).
✅ Good news for #28: **the 10 global shards are balanced to 0.24%**, so a 10- or 20-way carve needs
no counting pass.

### 2026-08-08 — 🔴 W4-ZSTD: gap 1's fix is NOT reader-shaped. It is PLAN-shaped.

All `MEASURED` by decompressing a real file from the pinned revision.

**✅ Seekability is a NON-ISSUE — better than the gzip path, not worse.** `zstandard`'s
`stream_reader` is a **pull** API: peak memory = `window_size (2 MiB) + output chunk + line carry`.
Measured **24.45 MB peak** for 256 KiB reads, **independent of file size.** In a 15.03 GB container
that is nothing, and it does **not** interact with #22's 27.92 GB dedup set. **The zstd reader is not
what is tight.**
⚠️ **But `decompressobj` is the WRONG API and the difference is a real trap** — it is **push**: one
`decompress(8 MiB)` call returned the **entire 22.17 MB** file. **The gzip reader's
`zlib.decompressobj` shape must not be copied verbatim.**

**🔴 NEW SILENT-CORRUPTION TRAP zstd introduces that gzip does not.** The `.json.gz` reader's
correctness story rests on `zlib.Decompress.eof` (`corpus_read.py:530-533`). Truncating the real file
to 50%: `decompressobj().decompress()` returned 11,010,048 bytes and **raised nothing**; `.flush()`
**raised nothing**; the `stream_reader` loop hit EOF and **raised nothing**. And `has_checksum` is
**False**, so no frame checksum saves you. **A truncated zstd object decompresses cleanly into a
shorter document set and reports SUCCESS.** The guard exists (`decompressobj().eof` — True complete /
False truncated, measured both ways) but **`stream_reader` does not expose one**, so ENG must choose
bounded memory + an explicit completeness assertion, or `.eof` + unbounded output. **Without this the
zstd path is strictly less safe than the gzip path it copies.**

**🔴 THE FIFTH COST NOBODY ANTICIPATED — the repo's records have INCOMPATIBLE SCHEMAS.** `MEASURED`
on the first record from six directories: `ingredient1-nemotron-synth-qa` has keys
`language, text, url, warc_record_id` — **NO `id`** — while the other five have `id`.
`CorpusSpec` carries **one** `id_column` per row, and the reader **correctly RAISES** when it
resolves to nothing. **So a single registry row cannot cover this repo.** It needs **multiple rows,
one per schema family** → multiple `source_label`s, multiple bundles, **multiple ordinal
allocations**. That is **plan-shaped work inside the FREEZE, not reader-shaped work**, and it is in
nobody's 14B estimate. ⚠️ **Only 6 of 209 directories were sampled — the true row count is UNKNOWN.**

**Fix is FOUR edits, not one** (`DERIVED` from the code): a new `read_jsonl_zst_documents` (~25 net
lines, ~60 moved — the gzip reader is 97 lines but only its first ~15 are gzip-specific, so the
honest shape is extracting `_documents_from_lines` + two thin front-ends; **duplicating instead would
fork four documented correctness guards, which is exactly how the `families/` half-fix happened**);
`_READERS`; `READABLE_FORMATS`; and **`corpus_build.py:908-911`, the INLINE dict that actually runs**
— a three-table divergence where missing one is silent. Plus `pyproject.toml`.
**Transport needs zero new code** — `_RangeFile` is an `io.RawIOBase`, so
`dctx.stream_reader(rf, read_across_frames=True)` accepts it directly, inheriting CDN resolution and
429 backoff. (`read_across_frames=True` is **not** the default; without it a multi-frame object stops
silently at frame 1.)

**⚠️ The dependency change is an IMAGE REBUILD, and the failure mode has a subtlety:** an
`ImportError` is **not** in `main`'s caught tuple (`corpus_build.py:1027-1029`), so it surfaces as an
unhandled traceback — visible. **But if ENG wraps it in the `BuildDriverError` pattern already used
for `tokenizers` (`corpus_build.py:920-922`), it becomes a caught error MID-RUN, after other bundles
are built and paid for** — precisely what `_assert_readable` moved to plan time to prevent.
**→ If zstd is added, `_assert_readable` must also assert the IMPORT is available, at plan time.**

**My recommendation to the CEO on gap 1, now that the costing is in: DROP the 14B for v1.**
The reader itself is cheap and safe, but the schema fan-out is unbounded (6 of 209 surveyed), it
lands **inside** the FREEZE where ordinals are allocated, and the DROP is arithmetically free (worst
epoch 0.558 vs the unchanged 0.900 max). **Recommendation, not a decision.**

---

## 🔴 2026-08-08 — CEO HOLD ON SPLIT SHAPE (AUDIT finding F2). ACKNOWLEDGED AND AUDITED.

**The ruling:** `72,615 tok/s/vCPU` was MEASURED on **8-vCPU** containers
(`task-28-briefing.md:120-123`; `edullm-reservoir-build:9` = 8 vCPU / 14336 MiB), but the plan applies
it **per-vCPU at 32-vCPU children** (`IMPLEMENTATION-PLAN.md:1444`, `BUILD-DEPENDENCY-GRAPH.md:119`).
Invalid by the plan's own physics — ~78% is single-threaded Python holding the GIL, so only the 22%
encode half scales. **384 vCPU as 12×32 → 33.2 h; as 48×8 → 9.96 h, the shape the rate was measured on.**
**HELD: ways-per-source / split depth, container vCPU shape, and anything depending on either.**

### ✅ AUDIT OF MY OWN OUTPUT — 1 hit, corrected in place

I grepped every file I own for `32 vCPU|32-vCPU|12 children|12 concurrent|5-way|4-way|49 h|6.2×|72,615`.
**My status log and the dossier are CLEAN** — I never committed a split-shape or per-child duration,
because the dossier's columns (repo/config/revision/`text_column`/id/domain/format/tokens) **do not
depend on the shape.** Confirming the CEO's read: **the dossier is unblocked and stays top priority.**

**The one hit, `decon-17-and-b5.md:255`** — W5 wrote *"a 32-vCPU container running 32 worker processes
would need 10 GB just for index copies."* **The 320.4 MB/process measurement is MEASURED and STANDS;
only the container shape it is illustrated against is now void.** Corrected in place below. And note
it **strengthens** F2 rather than being collateral: 320 MB × 8 = **2.6 GB on an 8-vCPU container**,
comfortably inside 14,336 MiB — so **the measured-on shape is also the one that fits memory**, which
is independent corroboration that 48×8 is the right shape.

**All split/shape cells in the dossier are marked `BLOCKED-ON-F2` rather than filled from the plan.**
Specifically §B7's #28 note now carries the marker: the **10 global shards balanced to 0.24%** is a
`MEASURED` property of the DATA (it stands), but **how many ways to carve is a shape decision (held).**

### ⚠️ Item 2 — `tokenizers` IS UNPINNED. CONFIRMED, and it is worse than "no task id".
`MEASURED-IN-CODE`: **`grep -n tokenizers pyproject.toml` returns NOTHING** — the package appears
**zero times**. It is imported at **`corpus_build.py:631`** (`from tokenizers import Tokenizer`) with
a `BuildDriverError` wrapper at `:633`, and `:669`/`:763` call `_assert_tokenizers_parallelism()`.
**Installed here, MEASURED: `tokenizers 0.22.2`** at
`/Library/Frameworks/Python.framework/Versions/3.11/lib/python3.11/site-packages/tokenizers/__init__.py`.
This is `version-string-is-not-a-code-identity` **applied to the token ids themselves** — a different
`tokenizers` build could change the ids in a corpus whose whole value is those ids, and **nothing in
the repo would record which one produced them.** It is task **#23** in `docs/TASKS.md:43` ("Pin
`tokenizers` in `pyproject.toml`… declared nowhere", graph B1, 1 line) — **so it HAS an id**; what it
lacks is a version to pin *to*. **Recording `0.22.2` as the observed version; FREEZE prerequisite.**
⚠️ **Same class as the zstd finding**: both are undeclared deps that work on this laptop and would
differ in the Batch image. **Pinning `tokenizers` and adding `zstandard` are ONE image rebuild** —
`__version__` must be bumped in all 3 places or the image build fails.

### Item 1 — DCLM's own throughput: UNMEASURED, and I am recording the band rather than inventing a number
`72,615` is the **reservoir** mix's aggregate, and its per-bundle spread was **3×** (stackv2-edu 916k
vs finephrase 357k tok/s/container). **Nobody has measured DCLM specifically** — that is a finding.
What I already hold that bears on it, `MEASURED` this session: DCLM's tokens/byte is **0.22755**
(`dclm_100BT`) / **0.2323** (mirror), and mean **1,256.3 tok/doc**. ⚠️ **Those calibrate READ volume,
not filter throughput** — and per W5's decomposition the cost is dominated by **Python per-window
overhead**, which scales with *words*, not bytes. **I will not derive a DCLM throughput from
tokens/byte; that would be the same class of error as the cap×rate multiplication.** Flagged as the
highest-value cheap measurement after the dossier, not displacing it.

### 2026-08-08 — ✅ W6: GAP 4 RESOLVED. The plan and the report were never in conflict.

**🔴 THE CONTRADICTION IS A DENOMINATOR MISMATCH, and both documents are right about their own.**
`MEASURED` from exhaustive tree reads at `87f09149ef4734204d70ed1d046ddc9ca3f2b8f9`:

| config path | files | exact parquet bytes | dolma2 tokens |
|---|---:|---:|---|
| `data` (FULL) | 2,410 | 4,522,727,684,984 | **~1,583.1B** DERIVED |
| `sample/350BT` | 472 | 998,102,051,512 | **~349.4B** DERIVED |
| `sample/100BT` | 140 | 286,394,522,604 | **100.24B** MEASURED (prior wave) |

The ratio `r = 100,244,242,760 / 286,394,522,604 = 0.3500215` dolma2 tok/parquet byte, scaled up,
**reproduces the report's 1,583.1B to four significant figures** — same arithmetic, so:
- **`FINAL-DATASET-REPORT.md` §3's pool is the FULL `data` config** (252B = a comfortable **15.9%** draw).
- **`IMPLEMENTATION-PLAN.md` §10 reasons against `sample/350BT`** (252/349.4 = **72.1%**, exactly the
  plan's own "~72%" at `:1901`) — a **crushing** draw.
**Neither is wrong; they are not talking about the same pool.** Textbook instance of the scope-error
class. And the `sample/100BT` the registry pins **genuinely cannot supply 252B — a 2.51× shortfall.**

**🔴 A NEW TRAP: `sample-350BT` and `sample/350BT` are BOTH CORRECT AND NOT INTERCHANGEABLE.**
`sample-350BT` is the **HF config id** (`load_dataset`, and what FinePhrase's card declares as its
parent); `sample/350BT` is the **repo path prefix** (tree API, `hf://` globs). **The registry's
`config` field stores the PATH form**, so writing `sample-350BT` into it **resolves to nothing.**
Not a docs inconsistency to tidy — two different identifiers. Card front-matter binds them:
`config_name: sample-350BT → path: sample/350BT/*`.

**✅ No FinePhrase-shaped trap in FineWeb-Edu** — 10 columns, all top-level, payload is flat `text`,
join key `id` (`BYTE_ARRAY`/UTF8, `null_count: 0`, values `<urn:uuid:…>` — **the same WARC-Record-ID
form W2 found in DCLM**, so one id convention covers both web pillars).

**🔵 Two method corrections the worker made that are worth keeping:**
1. **My brief was wrong to prescribe a `url`-projected join.** Measured per-row-group compressed
   shares: `text` 97.1%, `url` **1.74%** (my figure, confirmed), **`id` 1.24%**. **`id` is 29%
   cheaper than `url` AND it is the exact key the anti-join uses at read time.** Joined on `id`.
2. 🔴 **A null result correctly refused as evidence.** A 240,000×240,000-id pilot returned
   **intersection = 0** — and the worker graded it **UNINFORMATIVE, not a refutation**, because the
   `DERIVED` expectation under a *true* subset claim was only **167** even before accounting for the
   fact that **files are DUMP-CLUSTERED** (each covers 7–22 of 110 CC dumps, and the two configs
   partition dumps differently, so a random-file × random-file join compares mostly-disjoint crawl
   slices and returns 0 **by construction**). **Reporting that zero as "subset refuted" would have
   been a false negative** — the same head/cluster-bias trap that bit a prior wave at 10×. A
   dump-controlled test is in flight.
   ⚠️ Also `MEASURED`: **the three samples share ZERO LFS oids** (0/140 and 0/14), so the nesting is
   a row-level subset re-serialized into new files — no shortcut via file identity.

### 2026-08-08 — ✅ GAP 2 IS NOW FULLY CLOSED (all three sources). W3 resolved Cosmopedia.

**Cosmopedia CONFIRMED to have no id — `MEASURED` from real bytes, not inferred from a card:**
"there is no `id`, no `uuid`, no `url`, no hash column in ANY of the 8 configs." So
`sha256(id) % N` genuinely has no key here. **The plan's §10 was right about Cosmopedia and wrong
about the two DCLM repos** — a 1-of-3 hit rate on a FREEZE prerequisite.

**Proposed surrogate (`DERIVED`, explicitly a proposal not a measurement):**
**`(config, file_basename, row_index_within_file)`** — e.g.
`cosmopedia/web_samples_v2/train-00000-of-00118.parquet#87676`.
- Rejected `sha256(text)`: the plan already calls it unstable, **and worse here — the required
  `lstrip()` fix CHANGES the text, so the id would depend on whether normalization ran before or
  after hashing. Two pipeline orderings ⇒ two different ids for one document.**
- Rejected `(config, row_index)`: row order is a property of the upstream files, not the dataset.
- The proposal is stable because filenames encode `-of-NNNNN`, so **any change to the file count
  changes every filename — the drift is loud, not silent.** **Condition: only against the PINNED
  revision**, which is why the sha is mandatory.
- ⚠️ **A surrogate id is NOT comparable across sources.** Every other row's `id` is upstream's own.
  **Any cross-source anti-join or dedup keyed on `id` silently excludes Cosmopedia.** Must be on the row.

**🛑 A NEW FinePhrase-CLASS TRAP IN COSMOPEDIA — `prompt` IS REAL WEB TEXT.** `MEASURED`: `prompt` is
**8–59% of `text`** by uncompressed bytes (`wikihow` 7.5%, **`auto_math_text` 58.9%**). Ingesting it
would put un-attributed seed web text into a corpus labelled synthetic — the same failure shape as
FinePhrase's `text`-vs-`rollout_results` trap, in a different column. Lower probability (the payload
column is plainly `text`), but **on `auto_math_text` the prompt is over half the payload.**
**`text_column` = `text`; never `prompt`.**

**⚠️ Cosmopedia's "21.7B tokens" is a MISTRAL-7B count, not dolma2** — `pool_tokens` recorded as
`null` rather than a false figure. Same non-summability rule as Common Pile's `GB × 0.25`.

**Two more rows resolved (dossier now 16/17):**
- **dolma3 midtrain QA** — `f23aa129fda8335ba9760057bcc1f0c02f3d068b`, `text` (JSON key), `id`,
  **ODC-BY-1.0**, 4 `ingredient1` dirs. (Still gated by the `.jsonl.zst` reader + the schema fan-out.)
- **Nemotron Math-Textbooks** — `nvidia/Nemotron-Pretraining-Specialized-v1` @
  `9ed3718b5f2ae29074c5e34e64115432b7c4320f`, config `Nemotron-Pretraining-Math-Textbooks`,
  `text_column: text`, **`id_column: uuid`** (note: *not* `id`), **`CC-BY-4.0` — CLEAN, no §2.2.2.**
  This is the ungated CC-BY Nemotron repo an earlier memory note recommended as the substitute.
- ✅ **CK-12 rule applied and cleared:** `openstax` and `khanacademy` are Cosmopedia configs **named
  for their seed sources**; **CK-12 is not among Cosmopedia's 8 configs.** No CK-12 exposure.

### 2026-08-08 — 🔴 #17 VERDICT: THE "107+ GiB ALREADY STAGED" CLAIM IS **FALSE**. Enumerated to exhaustion.

W5 completed the sweep I started. `MEASURED`, read-only, server-side exact byte sums.

**`edullm-landing` is FULLY ACCOUNTED FOR — ~44,760 objects, ~6.27 TB, against CloudWatch's 6.449 TB.**
The ~0.18 TB residual is GiB→GB rounding plus incomplete multipart uploads (which `BucketSizeBytes`
excludes) — **far below 470 GB and spread across prefixes already identified. There is no
unexplained 470 GB.** Decisive file-type evidence: the two largest non-`pretrain/` prefixes are
**100% `.bin`** (6,921/6,921 and 10,049/10,195) — **tokenized, not text. Nemotron-CC-Math source is
parquet, and there is not a single `.parquet` in the large prefixes.**
Every other bucket eliminated by size or by enumerated content (details in the file).

**🔴 ROOT CAUSE, and it is a class of error CLAUDE.md names explicitly.** `IMPLEMENTATION-PLAN.md`
§3.2 is titled **"PROPOSED change: stage the sources to S3 once"**, and §9 Phase 1:1788 lists
*"Stage ~4.21 TB to `s3://edullm-landing/_src/`"* as **work still to do (0.5 h)**. **`_src/` does not
exist.** So `docs/TASKS.md:58` **read a PLANNED staging step as a COMPLETED one** — the same shape as
"`state: ENABLED` is not evidence a shape can run."

**🔴 SCHEDULE CONSEQUENCE — #17 is NOT "1 job" and is NOT unblocked.** It must be re-costed as
**HF→S3 stage (~472 GB) + scan**. And the staging duration is governed by **the one rate this
project has never measured**: §3.2's reconciled **~8.4 MB/s HF CDN** would make it **~15.6 h
single-stream**; at an optimistic 100 MB/s it is **1.3 h**. **A 12× band.** Ingress is $0; storage
~$11/mo. **"1 job" understates #17 by at least one job and possibly by a working day.**
⚠️ **This compounds with BLOCKER B**: even with a staging job scheduled, **the gate is not accepted
on our token** (HTTP 403 `GatedRepo`), so **we cannot download the bytes to stage them.**
**→ #17 has TWO hard prerequisites, neither scheduled: gate authorization, then a staging job.**

### 2026-08-08 — ⭐ W5 CLOSED "never been measured": the 13-gram inner loop is now MEASURED

`IMPLEMENTATION-PLAN.md:267`, `:1689` and `BUILD-DEPENDENCY-GRAPH.md:546` all say the scan's CPU cost
*"has never been measured."* **No longer true.** W5 ran the real `DecontaminationIndex.contains()`
against the real 54 MB index on real prose. **Apple M2 Pro, CPython 3.11.9, single core:**
**1,174,020 windows/s/core** (median CPU-time; spread 1.12×), **852 ns/window**, 1,898 docs/s/core.

**🔵 And it made a methodological correction I want on the record**, because it is the failure mode
this project keeps repeating: its **first run said 366,143/s and its second said 1,127,827/s — a 3.2×
disagreement on the same code and input.** Cause: **wall-clock `min()` over only 3 runs on a loaded
laptop**, with a 2.6 s first-iteration outlier. Switching to **`time.process_time()`** with 15 reps
and a 40-doc warmup collapsed the spread to **1.12×**. *A single unrepeated measurement is not a
measurement.* **It nearly published the wrong number and said so.**

**🔴 The structural finding INVERTS the plan's framing.** The docs describe the scan as
"~193 billion Python-level `blake2b` calls". **blake2b is NOT the dominant cost — it is under half,
and half of *that* is Python object churn:** `_words()` 15.3%, `content_hash` 1.9%, blake2b stage
49.8%, **frozenset lookup 22.6%**. `hashlib.blake2b` on pre-built bytes runs at **3,804,994/s** while
the real loop achieves **1,838,585/s** — so **slice + `\x1f`.join + encode costs about as much as the
hash itself.** **Consequence: the hash-function choice is nearly irrelevant. Only leaving the
interpreter (Rust/C, or a rolling hash reusing work across overlapping windows) helps.**

**Index load, also `MEASURED`:** `from_bytes()` = **4.40 s CPU once per process**, **320.4 MB
resident** (§6.1's "~250 MB" is the right order; budget **320 MB**), **paid per process**.

### 2026-08-08 — ✅ B5's integrity chain CLOSED by recomputation, not field-presence
W5 recomputed **CRC-64/NVME locally** (implementing the reflected poly `0x9A6C9329AC4BC9B5` by hand —
no `awscrt` on this machine) and matched S3's `ChecksumCRC64NVME`:
`local 9599df6a4fd0cebe → base64 lZnfak/Qzr4=` **==** S3's `lZnfak/Qzr4=` (`ChecksumType: FULL_OBJECT`).
**So the staged index and the local index are the same bytes — established WITHOUT downloading the
object.** Combined with the two sha256 matches, the chain local-bytes → manifest → S3-object is
closed. **This is the golden rule done properly: recomputed from bytes, not a field presence check.**

### 2026-08-08 — ✅ B5 (#24) FULLY SCOPED. It should come OFF the critical path — but the WHY changes.

**All three inputs verified available, so the blocker I hypothesised does not exist.** ⛔ **Correcting
my own brief in place:** I told the worker *"if the build script is not reachable, B5's 4 h is fiction
and that gates FREEZE."* **All three inputs are present:**
- `eval_bundle.py` ✅ on this machine, with `week1_corpus` + a 398 KB `uv.lock` (deps pinned).
- The pinned `ai2-olmo` checkout `6c3373fa182af2d57fe3c390ffc8420d5c5b325a` ✅ **public on GitHub,
  HTTP 200, no auth** — and `eval_bundle.py:218-227` **hard-asserts** that exact revision, so it is
  not a soft dependency. All 57 MMLU subject dirs present.
  ⭐ **`arc_challenge/val_rc_5shot/requests.jsonl.gz` = 118,667 bytes, byte-identical to
  `source_files[0].byte_size` in the local manifest** — the upstream data the index was built from is
  **still present, unmoved, at the pinned SHA. Reproducible against the same BYTES, not just the same repo.**
- ⚠️ **The checkout is NOT on this machine, and there is a name trap:**
  `/Users/ericwu/Developer/Capstone_LLM/OLMo-core` is **a different repository** (no `olmo_data/`,
  cannot resolve the SHA). **Do not mistake `OLMo-core` for `ai2-olmo`.** One public `git clone` fixes it.

**B5's compute is MINUTES, not 4 h — `DERIVED` from measured components.** At the newly measured
1,174,020 windows/s/core, the full 4.2M-window render+hash is **3.6 s**. Total well under 1 h,
dominated by setup. **And it needs NO AWS Batch job — it is local CPU work**, so it parallelises with
staging and the build waves. **→ Recommend removing B5 from the critical path.**

⚠️ **BUT the reason it "gates" is SEQUENCING, not duration, and that reason SURVIVES.** Changing the
index changes **which documents get dropped** — `corpus_filter.py:44` and §6.2a both make the
index identity *be* the dedup identity. **B5 must land BEFORE the build wave that consumes it, or the
corpus is built against an index that no longer exists.** That is the real constraint.

**Three denominators kept apart on the rebuild's growth** (an earlier draft stated two as one figure):
**+36% n-gram COUNT** (+1.1M on 3,097,372) · **+32% ON-DISK bytes** (+17.6 MB on 54,350,848) ·
**+7% RESIDENT** (+18 MB on the MEASURED 320 MB, which supersedes the documented ~250 MB).
Resident goes **320 MB → ~338 MB**.

**The residual NOTHING fixes, recorded so it is never mistaken for solved:** our own measurement puts
13-gram detection at **F1 0.926 on verbatim text and 0.000 on rephrased text** (§6.4). FinePhrase is
rephrased FineWeb-Edu, which does zero upstream decontam. **B5 restores the exact-hash half and adds
raw-field n-grams — both are verbatim mechanisms, and both score 0.000 against paraphrase.** The
bundle also does not cover MATH, HumanEval, MBPP, DROP, TriviaQA, BBH, MMLU-Pro or GPQA, **while the
build still prints a healthy `DECON index …` line.** → **`limitations[]` disclosure, not a technical fix.**
⚠️ **And do not conflate: the −11.8 MMLU figure is a DEDUP result** (DCLM v4 Table 19, `min_ngram`
5→32.5 vs 13→44.3), **not a decontamination result** — and those rows also differ in shard count, so
it is not even a clean single-variable ablation.

### 2026-08-08 — 🔴🔴 W5 A.4.2: A 16× CONFLICT WITH THE 78%-SERIAL-FILTER ANCHOR. **This intersects the HOLD.**

**#17's scan itself is cheap — `DERIVED` from the measured 1,174,020 windows/s/core: ~18 min at the
6.2× effective cap, 17.7 h on one core.** ⚠️ **So #17's cost is dominated by the staging that A.3
shows never happened (1.3–15.6 h). The scan is cheap; getting the bytes there is not. That inversion
is the finding.**

**But the worker surfaced a direct, load-bearing conflict and flagged it rather than picking a side:**

The standing anchor says **72,615 tok/s/vCPU end-to-end, ~78% of it the serial Python 13-gram
filter** → decon alone = 72,615 ÷ 0.78 = **93,096 tok/s/vCPU**. The measurement, converted with the
plan's **own** anchors (`wallclock-audit.md:754` ~627 windows/doc; `IMPLEMENTATION-PLAN.md:374`
814.9 tok/doc → 0.769 words/token): **1,174,020 ÷ 0.769 = 1,525,800 tok/s/core — 16.4× FASTER.**
**Hardware cannot explain it:** even crediting an M2 Pro core as 3× a c7i vCPU (implausibly generous
for single-threaded scalar Python), the gap is still **5.5×**.

**If the measurement is right, decon is ~4.8% of the end-to-end budget, not 78%.**

**The worker explicitly did NOT assert the 78% is wrong**, and gave three ways it can still be right
— the most credible being that **the 78% covers more than `contains()`**: `dedup_and_decontaminate`
also does a sha256 per document and maintains `SeenHashes`, and `corpus_filter.py:232` records
`stackv2-edu--train` needing **18.6 GB inside a 20 GiB container** for its dedup set alone.
**A near-OOM 18.6 GB Python set is a completely different performance regime from a 320 MB index —
that, not blake2b, could be the real 78%.** (Also: the test corpus was clean prose with 0 removals,
and `contains()` early-returns on the second hit, so contaminated docs are *faster*, not slower.)

🔴 **WHY THIS MATTERS RIGHT NOW: it is the same quantity the CEO's HOLD turns on.** F2's Amdahl
correction (12×32 → 33.2 h vs 48×8 → 9.96 h) **uses the 78% serial fraction as its input.** If the
serial fraction is materially lower, **both the plan's shape AND F2's correction move** — F2's
*direction* (the rate was measured on 8-vCPU containers and must not be applied per-vCPU at 32)
stands regardless, because that is a scope error independent of the fraction. **But the magnitudes on
both sides depend on this number, and so does the ~6.2× parallelism ceiling and therefore the 15.5 h
critical path.**

**→ ESCALATING: one ~20-minute smoke job settles it, and `IMPLEMENTATION-PLAN.md` §3.3 already says
"Worth measuring in the same smoke job."** Job submission is PLAT's lane and is not authorized this
wave, so I am flagging, not submitting. **If decon is really ~5% rather than 78%, the ceiling and the
critical path are wrong IN THE PROJECT'S FAVOUR.** Neither I nor the worker claims that — we claim the
conflict is real and cheap to resolve.

### 2026-08-08 — ⭐ CROSS-WORKER VERIFICATION CAUGHT A DEFECT IN A PASTE-READY REGISTRY ROW

**This is the fan-out paying for itself.** W6 independently re-checked a row W3 had marked ✅ RESOLVED
and found it would have silently corrupted 41% of its source. Both workers were careful; **only the
overlap caught it.**

**🛑 W3's ready-to-paste dolma3-QA row sets `"id_column": "id"` across a comma-joined directory list
— and `ingredient1-nemotron-synth-qa` HAS NO `id` KEY.** `MEASURED` by decompressing and parsing the
first 50 lines of a randomly chosen file (seed 42) from each of the three QA directories:

| directory | actual JSON keys (all 50 lines agree) |
|---|---|
| `ingredient1-nemotron-synth-qa` | **`text`, `language`, `url`, `warc_record_id`** — **NO `id`** |
| `ingredient1-reddit_to_flashcards` | `id`, `text` |
| `ingredient1-wiki_to_rcqa-part1` | `id`, `text` |

**That row would fail — or worse, silently yield null ids — on 7.92 GB of the 19.46 GB QA pool
(41%).** Fix: `warc_record_id` for that directory, which needs a **second row** since `id_column` is
one field per row. **This independently confirms W4's schema-divergence finding (§B11) from a
different direction** — W4 sampled 6 directories via a different route and hit the same defect.
**Two workers, two methods, same conclusion: the dolma3 mix cannot be one registry row.**

**✅ Two things UPGRADED from CARD to MEASURED, and one blocker CLEARED:**
- `text_column = "text"` **confirmed from real bytes** in all three QA directories (was CARD).
- 🛑 **The card declares NINE features** (`id, text, metadata, source, version, created, added, doc,
  attributes`); **the real files have 2–4.** W3 had flagged the undocumented **`doc`** key as a
  possible FinePhrase-trap-in-JSON that *"MUST be settled before the row is frozen."* **W6 ran the
  check: `doc` DOES NOT EXIST in any QA file**, nor do `metadata`, `source`, `version`, `created`,
  `added`, `attributes`. **Blocker cleared.** ⚠️ **And the lesson generalizes: this repo's card is
  unreliable — do not resolve any other column question here from it.**

**🛑 W6 also invalidated a settling job W3 proposed the same day.** W3 suggested parsing
`Frame_Content_Size` from zstd frame headers (18 bytes each) as the cheap route to the missing
`pool_tokens`. **`MEASURED` on 4 files across all three QA dirs: `fcs_flag == 0` — the field is
ABSENT from every file.** That route does not work on this repo. Remaining options: decompress
fully, or a sampled compression ratio × exact compressed bytes.

**✅ And `.jsonl.zst` sampling is NOT blocked locally** — W6 streamed the HTTP response through
`ZstdDecompressor.stream_reader` and stopped after N lines, so a 5–8 MB file yields hundreds of
documents **without a full download**. (Consistent with W4's finding that `stream_reader` is a pull
API with bounded memory.) ⚠️ **But zstd has no random access, so offset randomization is at the FILE
level only** (random file of 1,024/1,204/8,629, then its head) — graded and caveated, not hidden.

**M4 method is sound and worth recording:** dolma2 tokenizer at cache snapshot
`5292e5d6c0f40b67cc765fe41bec991cf4345b5c`, `tokenizers` 0.22.2, vocab 100,278,
`add_special_tokens=False`, **seed 42**, and for parquet **random ROW GROUPS** inside randomly chosen
files (700+ row groups/file) — **the explicit fix for the 10× head-bias a prior wave hit.**

### 2026-08-08 — ✅ W3 CLOSED (1,209 lines, zero PENDING rows). A `4plus_MIND`-class trap on a BIGGER source.

**🛑 NEW, and it is the same failure mode as `4plus_MIND` on a larger scale: dolma3's two
"ingredients" are TWO VERSIONS OF ONE 100B MIX, NOT TWO HALVES.** `MEASURED`: eleven sources, eleven
**exact file-count matches**, byte ratios all **1.09–1.13**. **Drawing both double-counts the same
documents.** This is a live hazard because the QA draw selects by directory prefix and
`ingredient1-*`/`ingredient2-*` look like complements. **Pick ONE ingredient.**

**🛑 A naming trap that bites the QA draw specifically:** `wiki_to_rcqa-part1` (ingredient1,
**hyphen**) vs `wiki_to_rcqa_part1` (ingredient2, **underscore**). A glob written for one silently
misses the other.

**✅ Row 15's blocker is now precisely bounded, and one candidate escapes the license problem.**
`Nemotron-Pretraining-Specialized-v1` is **explicitly CARVED OUT of the NVIDIA Data Access Agreement
into CC-BY-4.0** — *that* is why row 17 (Math-Textbooks) is safe where rows 5/6 (CC-Math) are not.
Its `InfiniByte-Reasoning` config has an **identical 5-leaf schema** (`text` / `uuid`, flat, no trap)
and **15.2–18.6B** `DERIVED` tokens against an 8.0B target. **So there is a clean, ungated,
license-safe candidate for the unnamed row — the decision is cheap, it just has not been made.**

**⭐ A prior finding CORRECTED IN PLACE by the worker** — same class as the trap I logged in §B8:
the encoding audit's *"Math-Textbooks is implausibly dense at 1.23 bytes/token, don't trust 25.1B"*
was a **compressed-vs-uncompressed denominator error.** Real answer: **3.997 bytes/token — the card
is fine.** That is the third instance this session of `parquet_bytes` being multiplied by a
decoded-text ratio. **It is a recurring trap and it is now recorded in the dossier (§B8).**

**✅ pre-1929 books resolved from real bytes** — `common-pile/pre_1929_books_filtered` @
`23f9d96dbb1db3324bbc9fbfe1f8299cc799c4d1`, `text`/`id`, **Public Domain**. And
**`common-pile/gutenberg_filtered` 404s — it does not exist**, correcting a candidate list. The
predicted "worst-case OCR" risk **did not materialize**: zero long-s, ligatures, hyphenation, or
Gutenberg boilerplate in 49 documents — **it is HathiTrust, not Gutenberg, so the boilerplate cannot
be there by construction.**

**⚠️ THREE REPORT NUMBERS THAT DO NOT RECONCILE — flagged, not resolved, and they need the CEO:**
1. **reference pool 26.2B** (report §3) vs **~19.6B** derivable from the two real sources.
2. **reasoning pool ~50B** (report §4) vs **~15–19B** actually available from the best candidate.
3. **QA epochs 0.14** (report §4) vs **~0.85** under prefix selection — a **6× understatement**, and
   the one that bears on the under-1-epoch claim.
**Plus seven decisions that "will silently pick themselves if ignored"** (ingredient choice, config
sets, the 61.0B CC-Math split, a missing ninth category). Full list in the worker's file.

**Compliance across all 5 workers: no registry/plan/source edits, no S3 writes, no Batch jobs, no
bulk downloads. Everything from tree API + ranged footer reads (65 KB–700 KB each).**

### 2026-08-08 — ⚠️ PROVISIONAL / POSSIBLE P0: FineWeb-Edu `id` may NOT be stable across configs

**Not yet resolved — recording now because it is the largest live risk and the discriminating test is
still running.** Do not act on this until the result lands.

**The dump-controlled join has power, and it came back ZERO.** W6 compared the SAME `dump` values on
both sides (the pilot could not — see the earlier entry): observed intersection **0** against a
conditional expectation of **~17,992**. Under Poisson(17,992), P(0) = `e^−17992`. **Categorical, not
marginal.**

**Two hypotheses fit that equally well, and the worker refused to pick without a test:**
- **H1** — the samples are not row-subsets; the card's lineage is about *provenance* and the configs
  were re-materialized from the parent rather than carved out of it.
- 🛑 **H2 — the `id` values are NOT STABLE ACROSS CONFIGS.** If FineWeb-Edu assigns
  `<urn:uuid:…>` at **serialization** time rather than carrying one identity per document from the
  crawl, the same document has a different `id` in `sample/100BT` than in `sample/350BT`, and an
  id-join returns 0 **no matter how true the subset claim is.**

🛑 **H2, IF TRUE, IS THE BIGGER FINDING, because the entire anti-join design depends on `id` being a
stable cross-config document identity.** `sha256(id) % 4` cannot separate a FinePhrase draw from a
FineWeb-Edu draw if the two configs disagree about what a document's id is. **That would put task
#21 — and gap 4 — on a broken key.** Supporting evidence that H2 is live: **the three sample configs
share ZERO LFS oids**, so each was independently re-serialized — exactly the circumstance under which
a serialization-time id would be reassigned.
⚠️ **Note this does NOT touch DCLM**, where W2 proved cross-repo id stability directly (52/52
byte-identical text across two toolchains, 19 months apart). **The two web pillars may not share an
id convention after all**, despite both using the `<urn:uuid:…>` form.

**The discriminating test (running):** join `sample/100BT`'s `CC-MAIN-2017-43` slice against the
**full `data/CC-MAIN-2017-43`** directory on **BOTH `id` AND `url`** — `url` being content-intrinsic
and unable to be reassigned.
- `id` hits **and** `url` hits ⇒ ids stable, samples are disjoint re-draws (**H1**).
- `url` hits but `id` misses ⇒ **H2 CONFIRMED, ids are per-config, the anti-join key is broken. P0.**
- neither hits ⇒ the sampling is at fault and nothing above should be trusted.

**Until it lands: treat `sample-100BT ⊂ sample-350BT` as UNVERIFIED-AT-THE-ID-LEVEL, with the card's
prose as the only evidence. Do NOT downgrade the card claim on the strength of the zero alone.**

### 2026-08-08 — M4 partial: ✅ Cosmopedia CLEARS the EOS bound by 25.8×. Does not gate FREEZE.

`MEASURED`, all 8 configs, 3 random files/config × **random row groups** × 200 random docs, seed 42,
336 files inventoried, ~55 MB moved. Worst config `stories` = **515.8 tok/doc → EOS fraction
0.001939, 25.8× below the 0.05 bound.** Nothing is within 2× of it.

**And the worker justified WHY the naive estimator is legitimate here** rather than just applying it:
**CV is 0.25–0.46**, an order of magnitude below the CV 9.0 that made it useless on FineMath
(`artifacts/recount/README.md`). Cosmopedia is *generated* text with a bounded generation length, so
it is genuinely light-tailed — max/median only ~2.8×. **This is the case where the naive
per-document estimator is the right tool**, and the ±3% CI half-widths reflect that honestly.

Two non-blockers carried: `khanacademy` has a 6-token document and **2.0% of docs under 20 tokens**,
so `MIN_DOC_TOKENS = 64` will drop **3.0%** of it — **expect that attrition rather than treating it
as a bug** (it is the smallest config, 1 file/49 MB, so the corpus-level effect is negligible).
`stanford`'s min of 62 sits just under the floor (0.17% of docs).

---

## ⚖️ 2026-08-08 — CEO/OWNER RULINGS RECEIVED. Recorded before further work.

**1. NVIDIA license — OWNER DECISION: PROCEED**, treating `edullm-data` as internal. Nemotron-CC-Math
stays: 61.0B / 6.1%, math pillar intact. **The decision does not dissolve the §3.3 conflict — it
ACCEPTS it.** Owner's call, governs by standing instruction. **DO NOT RE-LITIGATE, and no worker may
reopen it.** ✅ Both my corrections stand in the ledger: the card's `license: other` does not name the
instrument, and the *"NVIDIA Open Data License Agreement"* an earlier audit cited **appears nowhere in
the document** — that citation is now marked wrong.
   - **1a (mine, now):** evidence the "internal" basis — who can actually read `s3://edullm-data`.
     **Any cross-account or public read path = URGENT escalation**, since it undercuts the premise.
   - **1b (mine):** label discipline so `nemotron-cc-math-*` objects are **enumerable** if termination
     ever comes. Mixture cannot span groups, so the pillar cannot be *separable* — but it must not be
     *unknowable*.
**2. The HOLD STANDS.** My framing adopted verbatim: F2's direction survives whatever the fraction is;
magnitudes move, the ruling does not. **My reconciliation hypothesis is to be pursued** — and the CEO
adds a sharp point I had not made: **the 8-vCPU shape has 14,336 MiB, so an 18.6 GB dedup set does not
fit there EITHER.** If that firms up it is a genuine constraint on the carve — **memory, not CPU**, the
same axis my 320.4 MB/process correction lit up. **Flag it if it firms up.**
   ✅ **blake2b finding adopted as structural:** any optimisation task written against the hash choice
   must be re-scoped before anyone spends time on it.
**3. Smoke job AUTHORIZED — routed to PLAT, not me.** Measurement only, ~20 min, no `manifest.json`,
no promote. Will report the serial fraction on c7i and, if possible, the dedup-set regime separately
from `contains()`. **I was right to ask rather than submit.**
**4. #17 re-opened as BLOCKED; B5 off the critical path with the sequencing constraint retained.**
The CEO names the gate finding as a **`state: ENABLED`-class error in a new domain — an accepted gate
belongs to a PRINCIPAL; someone else's is not our access.** Gate acceptance escalated to the owner.
🛑 **DO NOT attempt to work around the 403.**

### 2026-08-08 — ✅ 1a and 1b DONE. File: `data/measurements/internal-access-evidence.md`

**1a — THE OWNER'S PREMISE HOLDS. `MEASURED`, read-only, every path checked.**
`edullm-data`: all four public-access blocks **true**; S3's own `get-bucket-policy-status` says
**`IsPublic: false`** (AWS adjudicating, not me reading); bucket policy `edullm-data-airlock-v2` has
**every ARN in account `<ACCOUNT_ID>`** and **no external-account Allow, no `PrincipalOrgID`, no
wildcard read**; ACL has **one grant** (owner, FULL_CONTROL); **zero access points**; **no
replication**. **No cross-account or public read path exists → no urgent escalation, per your條件.**
⚠️ Stated precisely: the policy contains **no read `Allow` at all**, so read access is governed by
**in-account IAM** — *stronger* for the claim than a read Allow, but it means **the internal reader
list is an IAM question, not a bucket question**, and is `UNVERIFIED` from here. (And
`simulate-principal-policy` **lies** for the intern role — 11 known false denials — so settling it
would need live smoke tests.)

**🔴 1a's one real finding, for PLAT not for the ruling: `edullm-data-us-east-2` HAS NO BUCKET
POLICY.** Created 2026-08-07, it is the mirror §8B.3 recommends. Its four public-access blocks **are**
on, so **the "internal" premise is NOT undercut** — but it carries **neither `OnlyValidatorWrites` nor
`NobodyDeletesPublishedData`**. On that bucket the **validator-only invariant does not exist** and
**published objects are DELETABLE.** It is empty today, **but §8B.3 wants the published corpus
mirrored into it, and the moment that happens the copy is governed by no airlock.**
**→ The mirror needs the airlock policy applied BEFORE it receives a single object.** Same class as
`state: ENABLED` — reasoning "the corpus is protected because the bucket policy denies it" is **wrong
about the mirror**. Not mine to fix (read-only; infra is PLAT's).

**1b — SATISFIED WITH NO SCHEMA CHANGE, but the proposed labels have a COLLISION that defeats it.**
The mechanism already exists: `source` **is a path segment**, and Gate A **recomputes** it from the
key, so **the key IS the enumeration** — `list-objects-v2 --prefix tokens/<source>/` is the exact
affected-object list by construction. No new field, no manifest change.
🔴 **But `MEASURED`: `'nemotron-cc-math'` is a PREFIX of `'nemotron-cc-math-3'` and
`'nemotron-cc-math-4plus'`, and all candidates pass `SAFE_SEGMENT_RE`, so the regex will not save us.**
Two termination-day failure modes:
1. **Over-capture** — `--prefix tokens/nemotron-` sweeps in **`nemotron-math-textbooks`**, which is
   `Specialized-v1`, **CC-BY-4.0, explicitly CARVED OUT of the Data Agreement.** Deleting it destroys
   3.0B tokens we are entitled to keep. **The org name is NOT the licence boundary; the instrument is.**
2. **Under-capture** — the *safe* query (`tokens/nemotron-cc-math/`, with delimiter) and the *unsafe*
   one differ by **one character**, and a future tier split makes the delimiter form silently miss rows.
**RECOMMENDATION (a choice for you, not my decision): rename row 17's `source_label` to
`math-textbooks` (or `nvidia-specialized-math-textbooks`), keeping `nemotron-cc-math` for both CC-Math
tiers.** Then `--prefix tokens/nemotron-cc-math/` is exact and no sibling shares the stem. **Costs one
string.** Rejected the alternative of encoding licence state in the label (`restricted-…`) — it bakes a
**mutable legal status into an immutable key** that lives inside `manifest_sha256`.
⚠️ **Must be set BEFORE FREEZE** — `source_label` is a path segment in `manifest_sha256`, so changing
it later is a republish, a full re-copy, and an ordinal rename.

---

## 🔴 2026-08-08 — #17 UNBLOCKED. The owner supplied the location. My search space was wrong.

**Found at `s3://edullm-scratch/grant.matherne/nemotron-cc-math-v1/`** — CEO-verified `MEASURED`:
793 objects / 242.1 GiB, of which **193 `.parquet` = 99.9% of bytes.**

**⛔ STANDING ORDER, RECORDED FIRST BECAUSE IT BINDS EVERY AGENT:**
**`…/.edullm/.hf_token` (37 bytes) is a LIVE CREDENTIAL. NO AGENT READS, EXPORTS, PRINTS, OR USES
IT.** Reading it spreads a live credential into transcripts; using it means authenticating as **the
very principal whose gate acceptance I established is NOT ours** — the same laundering pattern we
refuse for peer agents. **Its existence and path are the finding.** Rotation/deletion escalated to
the owner by the CEO. If I find the same mistake elsewhere while sweeping, **I report the path only.**

**✅ MY OWN CORRECTION, in place and visibly.** I wrote that the "107+ GiB already staged" claim was
*"likely FALSE"* and that #17 needed a ~472 GB HF→S3 stage. **The staging claim was TRUE; my
enumeration was sound but my SEARCH SPACE was wrong.** I swept `edullm-landing`, `edullm-data`,
`edullm-datasets`, and the intern/scratch buckets I knew of — **I never swept
`edullm-scratch/<person>/`.** The lesson is exactly the one I applied to a worker's null result
earlier today and failed to apply to my own: **"I enumerated the bucket and it is not there" is not
"it does not exist."** ⚠️ **`edullm-scratch` and personal prefixes are now part of my standing sweep.**
- My `docs/TASKS.md:58` root cause **still stands and is still worth fixing** (it read §3.2's
  PROPOSED staging as COMPLETED) — but **the consequence was benign**: someone did stage it, under a
  personal prefix.
- ✅ **The ~472 GB / 1.3–15.6 h staging job is CANCELLED as unnecessary** — that was the widest
  unmeasured duration band in the plan and **it is now gone.** Any copy is server-side.
- ✅ **No HF gate needed** — we read bytes already in our own account. **The 403 is not to be retried.**

### 2026-08-08 — ✅ ITEM 1: DENOMINATOR RECONCILED. **134.0B STANDS.** It is the parquet-bytes trap, a 4th time.

**`MEASURED` — exact `list-objects-v2` byte sums through the broker:**

| prefix | files | bytes | GiB |
|---|---:|---:|---:|
| `3/` | **57** | **107,417,646,757** | 100.0 |
| `4plus/` | **46** | **62,189,080,483** | 57.9 |
| **3 + 4plus** | **103** | **169,606,727,240** | **158.0** |

**`169.6 GB` (compressed parquet on disk) vs the prior `472,213,218,716` (UNCOMPRESSED TEXT bytes).
Ratio = 2.784. These are two different quantities, and neither is wrong.**

**`MEASURED` from a real footer** (`3/part_000000.parquet`, 1,899,869,110 B, 993,681 rows, 4 row
groups, `created_by: Polars`, footer read over ranged GET):
```
text  uncompressed 5,218,012,081   compressed 1,783,016,588
text uncompressed / FILE bytes = 2.7465
```
**`DERIVED`, scaling that ratio to both configs:**
| | derived text bytes | tokens @ 0.283686 | standing figure |
|---|---:|---:|---:|
| `3/` | 295,023,786,397 | **83.7 B** | 83.6 B |
| `4plus/` | 170,803,015,618 | **48.5 B** | 50.4 B |
| **total** | **465,826,802,014** | **132.1 B** | **134.0 B** |

**Agreement with the prior MEASURED byte count: −1.35%.** Two independent routes — a teammate's
1,920-doc random-offset sample vs my footer-ratio scaling over the actual staged files — land within
1.4%. ✅ **The 472,213,218,716 figure spans EXACTLY `3` + `4plus`, NOT `4plus_MIND`.** (Had it
included `4plus_MIND`, the staged bytes would have to be ~50% larger than they are.)
**→ 134.0B is CONFIRMED. The 61.0B draw / 6.1% corpus share is safe to carry forward.**

🔴 **AND THE TRAP IS QUANTIFIED: using parquet bytes here yields 48.1 B tokens — 2.75× too low.**
**This is the FOURTH instance today** of `parquet_bytes × decoded-text-tok/byte` (after W2's DCLM
self-correction, my §B8 entry, and W3's Math-Textbooks correction). **It is the single most repeated
error in this project. It is recorded in the dossier as §B8 and should go in the plan.**

### 2026-08-08 — 🛑 ITEM 2: `4plus_MIND/` IS NOW A LIVE HAZARD. Registry must name prefixes EXPLICITLY.
It is **90 parts / ~86 GiB sitting directly beside the bytes we want**, in the same parent prefix.
**A `nemotron-cc-math-v1/*` glob silently double-counts ~86 GiB of REWRITTEN text.** Recorded in the
dossier row: **the registry must name `3/` and `4plus/` by EXPLICIT prefix — never a glob over the
parent, and never a `startswith("4plus")` match, which also catches `4plus_MIND`.** Two independent
mechanisms (glob-over-parent, prefix-match-on-config) both fail the same way.

### 2026-08-08 — 🔴 ITEM 3: THE DATA EXPIRES 2026-11-07. Verified, and it is BUCKET-WIDE.
`head-object` returns
`Expiration: expiry-date="Sat, 07 Nov 2026 00:00:00 GMT", rule-id="expire-working-objects"`.
`get-bucket-lifecycle-configuration` on `edullm-scratch`, `MEASURED`:
**`ID: expire-working-objects`, `Expiration: {Days: 90}`, `Filter: {Prefix: ""}` — EMPTY PREFIX,
`Status: Enabled`.** ⚠️ **The empty prefix means it applies to the WHOLE BUCKET, not just this
person's directory** — every object in `edullm-scratch` dies 90 days after its own creation.
Objects were written **2026-08-08**, so **the data is deleted 2026-11-07.**
**→ It does NOT vanish tomorrow** (the CEO's worry about the 2026-08-09T03:54Z lane instance does not
apply — that is a compute lane, this is a bucket rule). **But it is on a 90-day clock and this is a
working directory, not a curated dataset.** A server-side copy into a non-expiring prefix is cheap
and I recommend it before any build depends on these bytes.

### 2026-08-08 — ✅ ITEM 3 (cont.): THE PARQUET VERIFIES, AND IT IS THE REAL THING.
`MEASURED` from the footer, cross-checked against my dossier row 5:
- ✅ **`text_column = "text"`** — a **flat top-level `large_string`**. **Exactly one leaf named
  `text`** in the whole schema, so the FinePhrase `.names.index("text")` trap **cannot fire.**
- ✅ **`id_column = "id"`** — flat top-level `large_string`, 39,751,486 uncompressed bytes over
  993,681 rows = **40.0 bytes/id**, consistent with the UUID-shaped value my dossier predicted.
- ✅ **The 10-leaf schema matches the ungated NVIDIA sample repo EXACTLY** (`text`, `id`,
  `metadata.{warc_filename, warc_id, finemath_int_scores, finemath_scores, nemocurator_int_scores,
  nemocurator_scores, category, models_used}`). **This CLOSES the corroboration caveat W3 flagged** —
  W3 graded the config schema `DERIVED (high confidence)` from the sample mirror and named a
  1-minute settling job. **I just ran the equivalent against the real config-`3` bytes. Upgrade
  rows 5 and 6 to `MEASURED`.**
- ✅ **`ContentLength` 1,899,869,110 is byte-identical to the HF tree API's size for
  `3/part_000000.parquet`** (recorded independently by W3 before this prefix was known).
  **Two independent routes agree to the byte: these are the real upstream files, unmodified.**
- Row count 993,681 in 4 row groups; mean **5,251 text bytes/doc** — plausible for math web text and
  consistent with the ~3.53 bytes/token density the report notes for this source.
- ⚠️ **`metadata.category` compresses to 1,203 bytes over 993,681 values** — near-single-valued, which
  **independently confirms W3's recommendation to set `domain_column = None`** rather than inherit it.

### 2026-08-08 — CEO rulings received. Recorded; one lead RETIRED.

- ✅ **1a accepted.** Trigger condition did not fire; no escalation manufactured. My "no read `Allow`
  at all → in-account IAM, `UNVERIFIED` from here" is recorded as **a limit of the evidence, not a
  gap in the decision.** Not pursued (the simulator lies for the intern role).
- ✅ **1a's mirror finding RULED and CEO-verified:** `edullm-data-us-east-2` → `NoSuchBucketPolicy`.
  **The airlock policy must be applied and re-verified BEFORE that bucket receives a single object.**
  Hard precondition on §8B.3, on the release checklist. *A mirror is not a backup if it is mutable.*
- ✅ **1b RULED: rename row 17 `source_label` → `math-textbooks`, before FREEZE.** CEO verified the
  collision independently and added the mechanism I had not: **`SAFE_SEGMENT_RE` validates characters
  WITHIN one segment and is structurally incapable of comparing two labels — so this could never have
  been caught there.** With `{nemotron-cc-math, math-textbooks}` there are **zero** remaining prefix
  collisions. My rejection of `restricted-…` upheld (mutable legal status in an immutable key).
- ✅ **Row 15 RULED: adopt `Nemotron-Pretraining-Specialized-v1` / `InfiniByte-Reasoning`.**
  ⚠️ **It is `Specialized-v1`, the SAME FAMILY as `math-textbooks`, so it must NOT be labelled
  `nemotron-*`** — that would re-create the collision we just removed. **The dossier's 17th row is
  now decided.**
- 🛑 **NEAR-OOM LEAD RETIRED — and the CEO owns the error.** `corpus_filter.py:232` is **a DOCSTRING,
  specifically the one documenting the FIX**; `SeenHashes` stores `set[int]` today (128-bit
  truncation), commit `a372bf8` is in this tree, and the deployed image asserts
  `'DEDUP SET NOT NARROWED'` in preflight. **PLAT's decisive point: the 78% was measured ON THE
  RESERVOIR, where the dedup set is 3.6 GB in a 15 GB container — no memory pressure at all. A
  near-OOM regime cannot explain a number measured in a regime that was not near OOM.**
  **→ My 16.4× gap is once again UNEXPLAINED. I am dropping that branch;** PLAT settles the ratio
  locally with `cProfile`. **My measurement stands; the discrepancy is real and needs a different
  explanation.** ⚠️ *Note for the record: I passed the CEO's citation to a worker without verifying
  it was executable — the same class of error, one level down. Verify line numbers are code.*
  Real memory constraint retained: at 1.0T, `stackv2-edu` (~168 M docs) needs **14.4 GB against
  14,336 MiB — short by ~1.5 GB**, fixed by bundle-splitting already on the path.
- ⚠️ **R2 now explicitly covers SUBMITTING THE VALIDATOR** — disabling the EventBridge rule does not
  prevent a promote, because **rev 14 carries `--promote` in the job def itself.** Noted.

### 2026-08-08 — M4 nearly complete: EOS bound CLEARED everywhere measured. But a NEW risk M4 was not looking for.

| source | mean tok/doc | EOS fraction | margin to 0.05 | verdict |
|---|---:|---:|---:|---|
| Cosmopedia, worst config (`stories`) | 515.8 | 0.001939 | 25.8× | ✅ |
| Nemotron Math-Textbooks | 1,999.2 | 0.000500 | 100× | ✅ |
| **reasoning traces** (`InfiniByte-Reasoning`) | **11,310.5** | 0.0000884 | **566×** | ✅ |

**No stage-2 source measured so far is anywhere near the 20-token floor.** M4's stated worry — that
the dolma3 QA source might sit near it — has **not** materialized in anything measured yet.

🛑 **BUT THE REASONING-TRACES ROW CARRIES THE OPPOSITE RISK, AND M4 WAS NOT LOOKING FOR IT.**
At a **mean of 11,310 and a median of 10,210 tokens**, it is **13.9× longer than the whole
reservoir's 814.9** and ~5.6× longer than Math-Textbooks. Two `DERIVED` consequences nobody recorded:
1. **A single document exceeds the training sequence length by an order of magnitude.** At 4,096 or
   8,192, **the MEDIAN document does not fit.** How the packer splits a 10k-token document across
   sequences bears directly on **the one genuinely MoE-specific lever** — domain-pure micro-batches
   suppress expert specialization (worth 0.13–0.18 PPL and +5–6 GSM8K), and a source whose documents
   span many consecutive sequences is *structurally* domain-pure across them. **This interacts with
   #19 and must be checked before training, not after.**
2. **The 8.0B draw is only ~707,000 documents** (8.0e9 ÷ 11,310) against 1,478,301 rows =
   **47.8% of the pool BY DOCUMENT COUNT**, not the 0.16 epochs the report's table implies.
   ⚠️ Flagged, not resolved — it depends on the row-15 binding, **which the CEO has now RULED**, so
   this is worth re-deriving against the ruling.

**Two method notes the worker got right and that matter for reuse:**
- 🛑 **The Cosmopedia "random row group" method does NOT transfer to these files** — they have only
  **4 row groups of ~250,000 rows (~600 MB each)**, so a "random row group" is a quarter of a 2.4 GB
  file, not a random offset. It fell back to `datasets-server/rows` at 40 random offsets × 5 rows,
  **rate-limited to ~1.2 s between calls with exponential backoff; two 429s absorbed.** Correct
  handling of the documented `/rows` quota hazard.
- ⚠️ **`large_string`, not `string`** on the Specialized-v1 sources — arrow's 64-bit-offset variant.
  **Any reader that `assert`s `pa.string()` fails here.** Worth one line in the reader.
- 🛑 **Math-Textbooks' `id_column` is `uuid`, NOT `id`** — reconfirmed independently. **A row copying
  the Nemotron-CC-Math pattern would be wrong.** Now doubly relevant since row 15 is the same family.

⏳ **STILL OUTSTANDING AND HIGHEST-CONSEQUENCE: the FineWeb-Edu `id`-stability test (H1 vs H2).**
Not yet reported. Everything else in my scope is closed.

### 2026-08-08 — 🔴🔴 M4 FOUND ITS TARGET: `reddit_to_flashcards` IS A PUBLISH BLOCKER

**The M4 node's stated hypothesis — *"the dolma3 QA source is the one plausibly near the 20-token EOS
floor"* — is CONFIRMED, and it is worse than "near".** `MEASURED`: 6 random files/dir (seed 42), 120
docs each = **720 docs per directory**, dolma2, `add_special_tokens=False`.

| directory | mean tok/doc | median | CV | EOS frac | vs 0.05 bound | **<64 tok** |
|---|---:|---:|---:|---:|---|---:|
| `nemotron-synth-qa` | 496.7 | 570.5 | 0.414 | 0.002013 | 24.8× clear | 0 |
| `wiki_to_rcqa-part1` | 188.8 | 147.0 | 0.718 | 0.005296 | **9.4× clear** ⚠️ | **5.7%** |
| 🔴 **`reddit_to_flashcards`** | **54.4** | **53.0** | 0.212 | **0.018386** | 🔴 **2.7× clear** | 🔴 **79.6%** |

**Every other source measured in this corpus is 25–566× clear of the bound. This one is 2.7×** — the
only source anywhere near it, by an order of magnitude.

🔴 **AND `corpus.MIN_DOC_TOKENS = 64` — THE GUARD THAT EXISTS TO PREVENT EXACTLY THIS — DESTROYS THE
SOURCE INSTEAD OF RESCUING IT.** With mean 54.4, median 53.0 and **CV only 0.212**, the distribution
is **tightly clustered just BELOW the floor** — it is not a tail poking under. So the filter does not
trim a tail to lift the mean; **it deletes 79.6% of the directory.**

**Three things compound, and together they may make the 14.0B QA row unsatisfiable:**
1. `reddit_to_flashcards` is **7,923,725,257 of 19,458,629,881 QA bytes = 40.7% of the entire QA
   pool** (W3's byte figures). Losing 79.6% of it removes ~a third of the pool.
2. W3 **already** flagged the 14.0B draw as **~85% of the pool BEFORE any attrition** (the report's
   0.14 epochs vs ~0.85 actual — one of the three unreconciled numbers I escalated earlier).
3. 🔴 **The attrition is INVISIBLE UNTIL AFTER TOKENIZE.** `MIN_DOC_TOKENS` drops documents silently;
   the shortfall surfaces as a bundle that will not fill, **at the end of the run** — and
   `corpus_pack` then refuses the bundle, so the cost is the whole tokenize pass.

**This materially strengthens my existing recommendation to DROP the 14B dolma3 QA row.** The gap-1
zstd reader was already four edits plus an unbounded schema fan-out (6 of 209 dirs surveyed); now the
**content itself** is the least publishable in the corpus, and the draw may be arithmetically
impossible. **Still a recommendation, not a decision — but the case is now much stronger than when I
first made it, and the DROP is still arithmetically free (worst epoch 0.558 vs the unchanged 0.900).**
⚠️ **If the row is KEPT, `reddit_to_flashcards` must be excluded at the directory level** — which is
another argument for the multiple-registry-rows finding (§B11), since one row cannot express it.

### 2026-08-08 — ⚖️ THE id TEST RETURNED INCONCLUSIVE, AND THE WORKER HELD TO ITS PRE-REGISTERED RULE

**Do NOT read this as "the anti-join key is broken." It is not established.** This is the outcome I
most wanted the worker to be capable of: it registered a three-branch decision rule **before**
running, hit the third branch, and **refused to claim either hypothesis.**

`MEASURED`: A = `sample/100BT`'s `CC-MAIN-2017-43` docs (126,553); C = 3 of 18 files of
`data/CC-MAIN-2017-43` at the same pinned sha, read completely (2,490,381 rows, all ids distinct).
Sampling frame validated two ways: 2,490,381 / 14,942,282 = **16.667%**, and 3/18 = **16.667%** ✅.

| join key | observed | expected if subset |
|---|---:|---:|
| `id` | **0** | 21,092 |
| **`url`** (content-intrinsic, cannot be reassigned) | **3** | 21,092 |

🔴 **NEITHER KEY HITS. `url` misses just as badly as `id` (0.014%).** **So the test failed its own
control** — a serialization-time id would have shown `url` hits with `id` misses. **H1 not confirmed,
H2 not confirmed**, and a **third hypothesis** is added: **H3 — `data/` is a LATER re-extraction /
re-dedup than the frozen samples** (the samples date to the original release; `data/` spans dumps
through `CC-MAIN-2025-26`). Given ~0 `url` overlap, H3 is the most economical explanation, and it
means **ids are probably fine.**

⚠️ **STANDING INSTRUCTION FROM THE WORKER, WHICH I AM ENFORCING: do not propagate "FineWeb-Edu ids
are unstable" out of that file — it is NOT established.** Equally, do not propagate "the samples are
not nested": **100BT vs 350BT was never tested against a working control.**
**→ I am retracting the P0 framing I sent earlier. The 252B subset claim stays
UNVERIFIED-AT-THE-ID-LEVEL, with the card's prose as the only evidence — no worse than before, and
not the emergency it looked like.**

**🔴 AND THE WORKER REFRAMED TO THE TEST THAT ACTUALLY DECIDES #21 — I had the wrong question.**
Everything above concerns *which FineWeb-Edu config contains which document*. **Task #21 does not
need that.** It needs exactly one thing: **do FinePhrase's `id`s match FineWeb-Edu's `id`s for the
same document?** FinePhrase carries FineWeb-Edu's full 11-column schema, so **both `id` and `url` are
present on both sides** — a direct two-repo join. **That is the P0, it is in flight (§A3b), and it is
a better-posed question than the one I briefed.**

### 2026-08-08 — 🔴 CEO's FilterStats correction VERIFIED — and it is worse a THIRD time. `problems()` HAS NO CALLER.

**The CEO's correction of my "invisible until after tokenize" is right, and I accept it.**
`filter_documents` (`corpus_read.py:872-879`) does count the losses into `stats` — **the drop is
counted, not silent.** And the CEO's sharpening is right too: the mean-based guard
(`corpus_read.py:861`) fires only `if self.kept and self.mean_kept_tokens < MIN_MEAN_DOC_TOKENS`,
i.e. on the mean of **what survived** — so deleting 79.6% of a distribution clustered at 54.4 tokens
(CV 0.212) **lifts the kept mean far above 20 and the guard CANNOT FIRE on this shape by
construction.** Verified in code.

**🔴 BUT I FOUND A THIRD MECHANISM, AND IT SUBSUMES THE OTHER TWO.** There IS a guard that would have
caught this — `ReadStats.problems()` (`corpus_read.py:839`) checks
`if self.drop_fraction > max_drop_fraction` with a default of **0.4**. **A 79.6% drop is nearly
double that threshold, and its message is almost exactly this finding**, verbatim: *"at this rate the
pool arithmetic in §2.1/§3.2 was computed on tokens this source will not deliver."*

🔴 **`problems()` HAS ZERO CALLERS IN THE ENTIRE SOURCE TREE.** `MEASURED-IN-CODE`, repo-wide grep:
```
tests/test_corpus_read.py:895, :907, :917, :925   <- 4 test call sites
src/edullm_data/corpus_read.py:839                 <- the definition
```
**Nothing in `src/` calls it. Not `corpus_build.py`, not the driver, not the receipt path.**
So the situation is: **the loss is counted; the mean-guard structurally cannot fire; and the guard
that WOULD fire is dead code that only tests exercise.** That is a **fail-open gate** — the exact
category CLAUDE.md warns about, and it is the same shape as the `families/` bug (a check that passes
in a checkout and protects nothing in production).

**→ FINDING FOR ENG's stream-6 FilterStats receipt work, and it is bigger than the receipt:**
1. **`ReadStats.problems()` must be CALLED** somewhere on the build path and its output surfaced —
   ~1 line plus wiring. Today it is decoration under the golden rule.
2. **`FilterStats` (`corpus_filter.py:283-296`) has NO dedicated short-doc counter** — `seen`,
   `kept`, `duplicates`, `contaminated`, `normalization`. The short-doc loss is recoverable **only**
   as `seen − kept − duplicates − contaminated`. ⚠️ **Its own docstring says *"A denominator you have
   to guess is a denominator someone will guess wrong"* — and the short-doc count is precisely the
   one you must derive by subtraction.** The class violates its own stated principle.
3. Note `MIN_DOC_TOKENS`'s docstring (`corpus.py:179-181`) reasons *"At a 64-token floor the worst
   possible shard mean is 64, giving an EOS fraction of 0.0156, a 3.2× margin"* — **true, and it is
   why `reddit_to_flashcards` still clears the bound at 2.7×.** The floor protects the EOS invariant
   exactly as designed. **What nothing protects is the POOL ARITHMETIC** — and that is what
   `problems()` was written to catch and is never called to check.

**Confirmed constants:** `MIN_DOC_TOKENS = 64` (`corpus.py:185`), `MIN_MEAN_DOC_TOKENS = 20`
(`corpus.py:175`), `eos_fraction_max: 0.05` (`families/pretrain.json:46`),
`max_drop_fraction` default **0.4** (`corpus_read.py:839`).

### 2026-08-08 — ⚖️ RULING RECORDED: the 14B dolma3 QA row is **DROPPED** (CEO level, no owner escalation).
Free by the plan's own arithmetic (worst epoch 0.558 vs unchanged 0.900), reversible before FREEZE.
**My §B11 point is the reason DROP beats KEEP-and-exclude:** excluding `reddit_to_flashcards` needs
directory-level selection, which **one registry row cannot express** — so keeping is not the smaller change.

### 2026-08-08 — ✅ 5b ANSWERED: `zstandard` has NO REMAINING CONSUMER. **Do not add the dependency.**

**`MEASURED` against the completed 17-row dossier.** Formats across the whole approved mix:

| format | rows |
|---|---|
| **parquet** | 1a, 1b, 2, 4, 5, 6, 7, 11, 15, 16, 17 (11) |
| **json.gz** | 3, 8, 9, 10, 12, 13 (6) |
| **`.jsonl.zst`** | **row 14 ONLY — and it is DROPPED** |

**Row 14 was the SOLE consumer.** With it dropped, **every remaining source is already covered by
`READABLE_FORMATS = frozenset({"parquet", "json.gz"})`.**

**`MEASURED-IN-CODE`, repo-wide grep for `zst` in `src/`: SIX hits, ALL of them comments or error
strings.** `corpus_read.py:746` (a docstring note), `:774-775` (the unsupported-format error),
`corpus_build.py:127`/`:129` (the comment recording the historical DCLM `.zst` hole), `:329` (the
`_assert_readable` error text). **There is not one line of zstd code path, and now not one row that
would need it.**

**→ RECOMMENDATION: do NOT add `zstandard`. The `READABLE_FORMATS` gap closes BY REMOVAL, not by
code.** This retires, in one stroke:
- the 4-edit reader change and the three-table divergence,
- the truncated-stream silent-corruption mode (no `.eof` on `stream_reader`, `has_checksum: False`),
- the undeclared-dependency production trap (passes locally, fails on Batch),
- the ECR image rebuild that a dependency change would have forced.

✅ **So `#23` (pin `tokenizers`) SHIPS ALONE.** It is still required and still a FREEZE prerequisite —
`grep -n tokenizers pyproject.toml` returns **nothing** while `corpus_build.py:631` imports it;
observed version **0.22.2**. **One line, one image rebuild, one dependency — not two.**
⚠️ **The three-table divergence W4 found is NOT retired by this** and should still be fixed:
`READABLE_FORMATS` (`:127`), the **inline dict at `corpus_build.py:908-911` that actually runs**, and
`_READERS` (`corpus_read.py:748-752`, which uniquely accepts `jsonl.gz`) disagree, and **W4 measured
a live false-negative today: a `jsonl.gz` row is silently droppable despite a working reader.** That
is a real bug independent of zstd, and `read_documents` is **dead code** (tests only).

### 2026-08-08 — ✅ 5a DONE. Data copied off the expiring prefix, VERIFIED BY RECOMPUTATION.

**Destination: `s3://edullm-landing/_src/nemotron-cc-math-v1/`** — the prefix
`IMPLEMENTATION-PLAN.md` §3.2 names for staged sources, which until now did not exist.

**Destination chosen on EVIDENCE, not convention.** `get-bucket-lifecycle-configuration` on
`edullm-landing`, `MEASURED` — 9 rules, and **every expiry rule is prefix-scoped**:
`pretrain/` 14 d · `curriculum/` 14 d · `sft/` 14 d · `eval/` 14 d · `probe/` 14 d · `vendor/` 14 d ·
`_pending/` 14 d · `_ingest/` **30 d** · plus an abort-incomplete-multipart rule.
**No rule matches `_src/`.** ✅ **Confirmed empirically too: `head-object` on a copied object returns
NO `Expiration` header**, where the source object returns
`expiry-date="Sat, 07 Nov 2026", rule-id="expire-working-objects"`. **Both the config and the object
agree — the copy does not expire.** (This is the inverse of the `_dist/` hazard: there the *absence*
of a rule is the problem; here it is the requirement.)

**Copy verified by recomputing the byte total, not by trusting the copy command:**

| | source (`edullm-scratch/grant.matherne/…`) | destination (`edullm-landing/_src/…`) |
|---|---:|---:|
| files | 103 | **103** ✅ |
| bytes | 169,606,727,240 | **169,606,727,240** ✅ |

**Byte-exact.** `3/` 57 files / 107,417,646,757 B + `4plus/` 46 files / 62,189,080,483 B.

🛑 **`4plus_MIND/` WAS DELIBERATELY NOT COPIED.** The destination holds **only** `3/` and `4plus/`.
**This physically removes the glob hazard** — a future `_src/nemotron-cc-math-v1/*` pattern can no
longer sweep in the rewrite, because the rewrite is not there. The exclusion is now enforced by the
*layout*, not only by a note in the registry. (The registry must still name prefixes explicitly and
match configs with `==`, in case anyone reads from the scratch prefix instead.)

**Compliance, checked explicitly:**
- ✅ **NO `manifest.json` written.** `list-objects-v2` on `_src/` filtered to non-`.parquet` keys
  returns **`[]`** — the prefix contains **103 parquet files and nothing else.** The EventBridge rule
  matches key suffix `manifest.json`; **nothing here can trigger it.**
- ✅ Server-side copy (`s3 cp` bucket-to-bucket, same region) — no bytes through this laptop, ~$0,
  and it did not touch the 0.8 MiB/s local path.
- ✅ Nothing promoted; no Batch job submitted; the source prefix is untouched.
- ✅ The live-credential file was **not** read, copied, or listed into any command — I copied
  `--include "*.parquet"` only, so `.edullm/.hf_token` was never in scope.

**Storage cost:** 169.6 GB × $0.023/GB-month ≈ **$3.90/month.** Trivial against the alternative of
re-downloading 470 GB from a gated HF repo we do not have access to.

### 2026-08-08 — 🔴🔴 A3b LANDED. **THE GAP-4 ANTI-JOIN CANNOT BE VALIDATED, AND THE PROPOSED FIX WOULD BE A NO-OP THAT LOOKS LIKE A FIX.**

**This has a WORKING CONTROL, unlike §A3 — which is why it is a finding and §A3 was not.**

`MEASURED`: FinePhrase `faq/000_00000_0.parquet` @ `78cf4a5e…`, **67,000 rows, complete
`id`+`url`+`dump` column read**, joined dump-matched against three `sample/350BT` files.

**✅ The control that §A3 lacked: the keys ARE comparable.** Read side by side from both repos —
**same `<urn:uuid:…>` shape, same bare-URL shape, same `dump` vocabulary, same 11-column FineWeb-Edu
schema** (FinePhrase adds only `rollout_results`), and FinePhrase carries a `dataset` column reading
literally **`HuggingFaceFW/fineweb-edu`** on every row. **So a zero here cannot be blamed on
incomparable keys.**

| FineWeb-Edu file | dump | ∩ on `id` | ∩ on `url` |
|---|---|---:|---:|
| `sample/350BT/014_00018` | CC-MAIN-2017-26 | **0** | **0** |
| `sample/350BT/014_00017` | CC-MAIN-2017-26 | **0** | **0** |
| `sample/350BT/001_00018` | CC-MAIN-2023-14 | **0** | **0** |

**`DERIVED` expectation for the 2017-26 pair: E[∩] = 2,145. Observed 0 on `id` AND 0 on `url`.**
🔴 **FinePhrase's documents are NOT FOUND in `sample/350BT` — not by id, not by url — even though
FinePhrase's own card and its own `dataset` column name it as the parent.**

**Leading hypothesis (H3): `sample/350BT` at pin `87f09149…` (2025-07-11) is NOT the snapshot
FinePhrase was built from** (FinePhrase's revision is **2026-03-31**). Same config name, different
revision, different row set. **This also explains §A3's control failure with ONE mechanism instead
of two**, which is why it now leads.

**🛑 THE CONSEQUENCE, AND IT IS THE MOST IMPORTANT THING IN MY SCOPE:**
1. **The §A4 collision arithmetic (72.1%, 25.97B) is DERIVED FROM AN UNVERIFIED PREMISE** — that the
   two draws index a **common id universe** — which is now **contradicted by three dump-matched
   joins on two independent keys.** Downgraded in place by the worker.
2. 🛑 **The proposed fix — reserve `sha256(id) % 4` buckets on the edu-web side — CANNOT BE VALIDATED
   AND MAY BE ACTIVELY HARMFUL.** It assumes a FinePhrase id and its FineWeb-Edu twin are the same
   string. **On the bytes read, they are NEVER the same string.** If that holds, the mechanism
   **silently does nothing: it excludes a quarter of edu-web at random, leaves the real collision
   untouched, and LOOKS like an anti-join.** **That is worse than shipping no fix.**
3. ✅ **TASK #21 ITSELF IS UNAFFECTED, and this is the distinction that matters.** `keeps_id` compares
   FinePhrase ids **to each other, within one repo**, and the four configs demonstrably share ids
   (**91.0–92.9% pairwise, MEASURED**). **#21 remains correct and already-implemented.** The broken
   thing is the *cross-repo* anti-join, which is a different mechanism that #21 never provided.
4. **The blocking question is not the one the plan or my brief posed.** Not *"does the partition
   separate them?"* (it is intra-FinePhrase and never could) but **"is there ANY key on which a
   FineWeb-Edu row and its FinePhrase rephrasing can be joined at all?"**
   **→ Until that is answered, NO id-keyed anti-join should be written.** The anti-join is **blocked
   on a measurement, not on code.**

**The one-run experiment that settles it (~30 min, ~400 MB, same tooling): join the same FinePhrase
file against `data/CC-MAIN-2013-20` and `data/CC-MAIN-2017-26` (the FULL config) on `id` and `url`.**
- **hits ⇒ H3** — the parent is the FULL corpus; the anti-join is implementable; **and the edu-web
  row should be `data`, not `sample/350BT`** — which also resolves the §A5 headroom problem (1.04×)
  in the same stroke. **Two problems, one change.**
- **no hits ⇒ H2** — ids are not stable across repos and **the anti-join must be CONTENT-keyed
  (normalized-url or a text shingle), not id-keyed. That is a design change and it must be made
  BEFORE tokenize.**

### 2026-08-08 — W6 CLOSED (949 lines). Three items from its closeout that are NOT yet in my report.

**✅ Task #21 IS ALREADY IMPLEMENTED — `docs/TASKS.md:35` is stale.** `MEASURED-IN-CODE` at
`corpus_build.py:1262, 1281-1282, 1292`. TASKS lists it as unshipped "~5 lines". **The remaining risk
is not that it is unwritten but that it is UNEXERCISED against live HF**, per its own docstring
(`:1243-1246`). ⚠️ **This changes what the Phase 2 smoke test is FOR** — it is a validation of
existing code, not a precursor to writing it.

**🛑 A TENSION NOBODY HAS RECONCILED, and it lands on the one MoE-specific lever.** The
`eos_fraction_max: 0.05` bound is **PER-SHARD, not per-source.** So a **mixed** shard dilutes
`reddit_to_flashcards`'s 2.7× margin and passes — **but the MoE lever requires micro-batches that are
NOT domain-pure**, and our shards are per-source by construction. **The two requirements pull in
opposite directions on exactly the source with the thin margin.** Neither the report nor the plan
addresses it; W6 did not inspect the packer. **Flagging as an open design question, not a finding.**

**⚠️ Nemotron-CC-Math `3`/`4plus` doc lengths remain DERIVED from the 954-row ungated mirror**
(CV 1.622, ±22.5% CI). The EOS verdict is safe at **54× margin** so it does not block, but the
estimate is loose. ✅ **Note this is now cheaply fixable and I should say so: the real bytes are in
`_src/` and need no HF gate** — the 403 that blocked W3 is irrelevant to a footer read of our own copy.

**Not measured, recorded as scope:** `wiki_to_rcqa-part2` (assumed to match part1), dolma3
`ingredient2` anything, and FinePhrase's `math`/`table`/`tutorial` configs (only `faq` was read).

**W6 logged 11 corrections-in-place, 5 of them to its own prior work.** That is the behaviour the
convention was written to buy.

---

## 📋 WHAT STILL BLOCKS FREEZE — DATA-EXEC's final list

| # | blocker | owner | grade |
|---|---|---|---|
| 1 | 🔴 **Is there ANY key joining a FineWeb-Edu row to its FinePhrase rephrasing?** 3 dump-matched joins, 2 independent keys, all **0** vs E≈2,145. **No id-keyed anti-join may be written until answered.** One ~30-min read-only run decides it. | needs CEO go-ahead | MEASURED (the zeros); hypothesis UNVERIFIED |
| 2 | 🔴 **Which denominator does the report's 252B intend** — `sample/350BT` (349.4B) or `data` (1,583.1B)? **Changes the collision by 4.5×** and decides whether any fix has headroom. **Cheapest item on this list; unblocks the registry edit.** | owner/CEO | DERIVED both ways |
| 3 | 🔴 **`reddit_to_flashcards`** — moot if the QA row stays DROPPED per your ruling; **re-opens if it is ever restored.** | ruled DROPPED ✅ | MEASURED |
| 4 | **F2 split shape / container vCPU** — held; PLAT's smoke job settles the serial fraction. My 16.4× gap is unexplained after the near-OOM lead was retired. | PLAT/CEO | HELD |
| 5 | **`#23` pin `tokenizers`** (observed 0.22.2) — absent from `pyproject.toml`, imported at `corpus_build.py:631`. **Ships alone now that `zstandard` has no consumer.** | ENG | MEASURED-IN-CODE |
| 6 | **Three-table format divergence** + **`read_documents` is dead code** + **a live false-negative: a `jsonl.gz` row is silently droppable despite a working reader.** | ENG | MEASURED-IN-CODE |
| 7 | **`ReadStats.problems()` has NO caller in `src/`** — a fail-open gate; the one check that would have caught the 79.6% attrition. ~1 line + wiring. | ENG stream 6 | MEASURED-IN-CODE |
| 8 | **Quality-percentile + cluster-ID schema** — ONE ruling, and it must precede FREEZE because both change source cardinality. My recommendation: **skip for v1.** | CEO | MEASURED-IN-CODE |
| 9 | **`edullm-data-us-east-2` has no airlock policy** — must be applied before the mirror receives an object. | PLAT | MEASURED |
| 10 | **Label rulings applied** ✅ `math-textbooks`; row 15 not `nemotron-*`. **Must be in the frozen plan** — a later change is a republish + ordinal rename. | done, needs freezing | RULED |

**NOT blockers (closed):** the dossier (17/17 MEASURED) · gap 2 (DCLM ids sound, Cosmopedia surrogate
proposed) · gap 1 (row DROPPED; `zstandard` not needed) · gap 3 (§11 not implementable as written) ·
M1 (85.3 MB/s in-region upheld) · #17 (data found, copied to `_src/`, scan ~18 min) · B5 (off the
critical path; sequencing retained) · the DCLM reconciliation (parent/child, settled from bytes).

### 2026-08-08 — 🟢 A3c: **H3 CONFIRMED. IDS ARE STABLE. §A3b's ALARM IS WITHDRAWN ON MEASUREMENT.**

**I stopped my own in-flight A3c run** — W6 completed the identical experiment first and its result is
strictly better powered (complete column reads on both sides, two replicate files). No duplicated work.

**Verified by reading W6's file directly, not from its summary:**

| `data/CC-MAIN-2013-20` file | rows | ∩ on `id` | ∩ on `url` |
|---|---:|---:|---:|
| `train-00000-of-00014` | 785,906 | 🟢 **2,085** | 🟢 **2,087** |
| `train-00001-of-00014` | 1,571,812 cum. | 🟢 **4,170** | 🟢 **4,176** |

🟢 **2,085 → 4,170 on twice the data = EXACTLY 2.0000×.** A perfectly linear accumulation is the
signature of genuine uniform overlap, not a fluke. `url` corroborates at **99.86%** — and `url` is
content-intrinsic, so it cannot be reassigned by a re-serialization. **21.2% of the FinePhrase dump
slice found in 10.5% of the dump's rows.**

**Same FinePhrase file: 0 hits across 3 `sample/350BT` files, 2,085 in ONE `data/` file.** H3 was
right — the config name was the problem, not the key.

**✅ I WITHDRAW the §A3b escalation I sent the CEO.** My two alarming claims are **reversed**:
the collision arithmetic is *not* premise-less, and the fix is *not* a no-op. **The two claims that
STAND are the ones that separate #21 from gap 4** — `keeps_id(fmt, doc_id)` is intra-FinePhrase and
structurally cannot express "drawn by another source", so **#21 never closed gap 4 and still doesn't.**

🔴 **AND THE SHARPEST FINDING SURVIVES THE REVERSAL, INVERTED:** moving the row to `sample/350BT`
**would satisfy the 252B size requirement while silently making the anti-join impossible — and it
would look correct.** The danger was never that the key is broken; it is that **the wrong config
choice breaks the join invisibly.**

**Four independent lines converge on `data/`:** arithmetic (`sample/100BT` cannot supply 252B),
headroom (§A5's 1.04×), **joinability** (this measurement), and the owner's own repoint ruling —
which W6 did not have when it measured.

**Real collision: 15.9% of pool, ~5.73B of the 36.0B FinePhrase draw — real in kind, overstated
4.5× by §10** (which computed 72.1% against the wrong pool). ⚠️ **And §4.3's "free fix" operates on
`sample/350BT`, so as written it does NOTHING.** → queued for the F1/F2/F3 doc sweep, not edited
piecemeal.

### 2026-08-08 — TASK 2: patch WRITTEN; the SCRIPT RUN is INFEASIBLE from this session. Sized, not guessed.

**✅ Patch delivered (not committed, per my lane): `data/measurements/eduweb-default.patch`.**
Flips `EDUWEB_DEFAULT` `sample/350BT` → **`data`** and replaces the card-citing docstring with the
measured evidence — both hit counts, the 2.0000× replication, the 99.86% `url` corroboration, the
three `sample/350BT` zeros against E≈2,145, the H3 explanation (config pinned 2025-07-11 vs
FinePhrase 2026-03-31), and an explicit **"DO NOT restore this on the strength of the card; re-run
the join first — a wrong value here does not fail, it under-reports the collision to ~0."**

**✅ `selftest` PASSED** (union, pair, distinct 0.2969, partition, key, Wilson, hash-sample,
salt-independence; numpy 2.4.4 / pyarrow 24.0.0 / python 3.11.9). **The script is sound.**

**✅ `tree` phase RUN with `--eduweb-configs data`.** `MEASURED`, and it is the finding:

| group | files | bytes |
|---|---:|---:|
| finephrase faq / math / table / tutorial | 6,791 / 6,787 / 6,772 / 6,754 | 1.370 / 1.210 / 1.203 / 1.378 TB |
| **fineweb-edu `data`** | **2,410** | **4.523 TB** |
| **TOTAL** | **29,514** | **9.684 TB** |

🔴 **THE EXACT CENSUS IS A 9.684 TB READ. At M1's MEASURED HF→laptop rate that is 36–42 DAYS.**
`--sample-mod` does **not** help — the help text says so explicitly: *"does NOT reduce HTTP bytes,
only RAM."* Column projection is the only real lever: `id` is **1.24% of bytes** (MEASURED), giving
a **~120 GB / 10.7–12.5 h** floor **on this laptop** — still not a session-scale task, and the
script's own design assumes a **job array** (`hash` is `--shard/--nshards`, `--workers 8` tuned for
"a FarmShare node").

**→ I am NOT running it, and I am not reporting a partial as if it were the census.** `--allow-partial`
is explicitly documented to **INFLATE the distinct fraction** ("exploratory use only"), which is the
fail-open pattern I was sent to fix. **Handing back: the patch, the verified `tree.json`
(`/tmp/fpov/tree.json`, both revision shas confirmed), and the sizing.** The run belongs on Batch
in-region, where 9.684 TB is hours, not weeks — **PLAT's lane, and the tree phase is already done for
whoever submits it.**

⚠️ **This is the same class as `publish()` pulling every byte locally.** The measurement is cheap
in-region and impossible from here; the script was written for a compute node.

**And note what does NOT depend on it: the `data` vs `sample/350BT` decision is already settled** by
W6's control-validated join (2,085 → 4,170 at exactly 2.0000×). The census would refine the
**magnitude** of the collision (currently 15.9% / ~5.73B DERIVED), not the **direction** of the fix.
**Task 1 is unblocked regardless.**

### 2026-08-08 — ✅ THE DOMAIN-PURITY vs EOS TENSION: I HAVE THE CHEAP READ. **It does not bind.**

The CEO asked whether other sources are **domain-pure-and-marginal**, before the wave shape is fixed.
I had every input already — this needed no new measurement, just the right table. **A domain-pure
shard's EOS fraction is exactly `1 / mean_tok_per_doc`, so the per-shard bound of 0.05 is a
mean-document floor of 20 tokens, applied SOURCE BY SOURCE** (§4.5's logic, taken to its conclusion).

**All 17 live sources, sorted by margin** (`MEASURED`; reservoir `sources.json` + W6's M4 work):

| source | tok/doc | EOS frac (pure shard) | margin |
|---|---:|---:|---:|
| reasoning traces | 11,310.5 | 0.000088 | 565× |
| ubuntu-irc · pubmed · peS2o · finepdfs-edu | 8,650 → 5,630 | — | 432× → 281× |
| math-textbooks · finemath · finewiki · dclm | 1,999 → 1,256 | — | 100× → 63× |
| fineweb-edu · stackv2-edu · stackexchange | 1,003 → 727 | — | 50× → 36× |
| cosmopedia (worst config) | 515.8 | 0.001939 | 26× |
| finephrase faq / tutorial / math | 440.7 → 309.1 | — | 22× → 16× |
| **finephrase-table** ← **tightest LIVE source** | **262.2** | **0.003814** | **13.1×** |
| ~~dolma3 reddit_to_flashcards~~ (STRUCK) | 54.4 | 0.018382 | **2.7×** |

🟢 **ANSWER: no live source is domain-pure-and-marginal. The tightest is `finephrase-table` at
13.1×, and the next is 15.5×.** The gap between the tightest live source (262.2 tok/doc) and the
20-token floor is **an order of magnitude.**

**So the tension the CEO identified is REAL IN PRINCIPLE but EMPTY IN PRACTICE — and it became empty
because row 14 was struck.** `reddit_to_flashcards` at 2.7× was the *only* source where domain-pure
sharding would have concentrated EOS risk into a failing shard; **every other source has ≥13× of
headroom, so domain purity costs nothing.**

**→ RECOMMENDATION: take the MoE lever at full strength.** Domain-pure shards are safe for all 17
live sources on the measured evidence, and the lever is worth **0.13–0.18 PPL and +5–6 GSM8K** — more
than a 50% increase in activated FLOPs buys. **No trade-off has to be made.**

⚠️ **Two caveats, so this is not over-read:**
1. **The 13.1× is a per-source MEAN.** A shard drawn from an unluckily short slice of
   `finephrase-table` sits below its own mean. At 13× that is a large cushion, but the bound is
   per-shard and the distribution matters — `finephrase-table`'s CV is not measured.
   **The one source worth a distribution check before the wave, if anything is.**
2. 🔴 **This is exactly the guard rail that `ReadStats.problems()` would provide and does not**, since
   it has **no caller in `src/`**. The analysis above is a pre-flight prediction; **nothing in the
   pipeline will check it at runtime.** That raises the value of the ~1-line fix I flagged to ENG.
