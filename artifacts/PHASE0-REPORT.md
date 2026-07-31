# Phase 0 report — awaiting the human decision on the ~$595 classification

**Status: IN PROGRESS.** This file is written incrementally so a compacted session can resume
without re-running anything. The §9.5 report shape is at the bottom and is filled in as results
land.

**Nothing irreversible has happened.** No bytes written to `edullm-data`, no `manifest.json`
uploaded to landing, no bucket policy touched, no dataset published. One AWS mutation: a new
`edullm-validator` job-def revision (§Task C below), which is additive and reversible.

---

## THE HARD STOP (§9.1) — still in force

> STOP after the dual-judge smoke test. DO NOT run the full domain classification. DO NOT spend the
> ~$595. DO NOT begin Phase 1 assembly. **This applies whether the gate PASSES or FAILS — passing is
> not consent.**

---

## Task C — infra gaps: DONE

**1. Validator job-def timeout: FIXED.** Registered `edullm-validator:7` with
`timeout.attemptDurationSeconds = 7200`. Verified live: `timeout: null` on revisions 1–6 (the gap
`HANDOFF.md` describes), now set on 7. Everything else is byte-identical to rev 6 — same image
digest, 4 vCPU / 8 GB, same job and execution roles, and the bootstrap command string is the same
length (928 chars), so the cutover carries no behavioural change.

Safe to cut over: EventBridge targets the job def by unversioned name, so rev 7 is live
immediately — and the CPU queue was empty when I registered it (checked, no `RUNNING`/`RUNNABLE`
jobs), so nothing in flight was affected.

**2. Bucket-policy v2: NOT deployed — reported only, as instructed.** The live policy is
`edullm-data-airlock-v1`: two statements, one Deny covering `PutObject` *and*
`DeleteObject`/`DeleteObjectVersion`, with the validator and deployer roles exempt from **all
five** actions. So the bucket policy still permits the validator to delete published data, and the
only thing preventing it is an identity policy on a role whose inline policies the intern session
can edit. Runbook: `infra/DEPLOY.md:256+`. **Not deployed — that is a human decision** (§9.6: no
bucket-policy deployment during Phase 0).

**Incidental correction:** `CLAUDE.md` says the live job defs bootstrap wheel `0.2.0`. They
bootstrap **`0.5.1`**, and the family was at revision **6**, not 2. `HANDOFF.md:54` is current;
`CLAUDE.md` is stale.

---

## Task B — license metadata: DONE

**OpenStax: 129 books, 100% catalog coverage.** 75 CC BY-NC-SA 4.0 · 53 CC BY 4.0 · 1 unresolved ·
**0 non-CC**, so the premise "OpenStax is Creative Commons throughout" is confirmed. The one
unresolved row is a retired empty CMS stub with no content to license. Cross-checked against the
REX content archive with **0 disagreements** across the 116 books both sources report.

Worth knowing for a future commercial question: **NC-SA is 86.9% of the live catalog**, so
non-commercial-only covers most of OpenStax, not a fringe.

**LibreTexts: `metadata.license` verified present**, a typed struct field (queryable server-side),
populated in **40,049/40,049** rows. Distribution is exact rather than sampled (server-side filter
counts summing to exactly the row total):

| license | rows | share |
|---|---|---|
| CC BY 4.0 | 24,205 | 60.4% |
| CC BY-SA 4.0 | 12,141 | 30.3% |
| CC BY 3.0 | 1,060 | 2.6% |
| Public Domain | 1,191 | 3.0% |
| GFDL | 757 | 1.9% |
| CC BY-SA 3.0 | 692 | 1.7% |
| CC BY-SA 2.5 | 3 | — |

Two things the design did not anticipate: **1,948 rows (4.86%) are not CC at all** (Public Domain +
GFDL), so the field cannot be modelled as a CC-only enum; and at 32% share-alike, **LibreTexts is a
third SA source** alongside §7 item 4's FineWiki and StackExchange. Share-alike is a larger slice of
this reservoir than the design assumed.

**Flagged for a human, not resolved here:** 6,974 LibreTexts rows attributed to OpenStax are
relabelled CC BY 4.0, dropping the NC clause that OpenStax itself declares (≥1,375 confirmed
conflicting across 5 title probes). Which party is right is a legal question. It is
machine-detectable, which is why the proposed schema carries `license_authority` and
`license_conflict` rather than a single license column.

Artifacts: `artifacts/licenses/{openstax-books.json, libretexts-distribution.json, SCHEMA.md}`.

---

## Two plan defects found while executing — see `PLAN-CORRECTIONS.md`

Full detail, with the verification for each, is in `artifacts/PLAN-CORRECTIONS.md`. The two that
change a decision the plan lists as CLOSED:

**1. `_dedup/clusters.parquet` would be REJECTED by Gate A.** §1.3 recommends it as a control file
with "no Gate A risk." Verified by execution: `_is_control_key('_dedup/clusters.parquet')` returns
`False`, so it trips `unlisted-object-dataset-level`. The allowlist is closed
(`CONTROL_BASENAMES` + `CONTROL_PREFIXES = {'_catalog/', 'dependents/'}`) and anchors basenames to
depth 0. Task B's `_licenses.parquet` fails identically. **Cost: one line in `validate.py` plus a
test and fixture — but it must land before the first publish**, which the plan does not budget for.

**2. There is no "24-topic taxonomy."** §1.2/§9.4 say to classify into "Essential-Web's published
24-topic taxonomy." Essential-Web publishes the **Free Decimal Correspondence, 12 main categories**,
whose **Level 1 has 10 values**; "24" is the paper's *token count* ("24T tokens"). And
`EAI-Distill-0.5b` emits **ten structured fields**, not a topic. Resolved by using **FDC Level 1**,
Essential-Web's own scheme, so the "don't invent categories" intent holds. **Consequence: the ≥85%
bar was calibrated against a 24-class problem that never existed; it is a 10-class problem
(chance 10%).**

Plus one that adds an irreversible decision the plan does not list: **a per-document license (or
cluster ID) has no join key.** The manifest's grain is one shard object. A `(shard_path, doc_index)`
key would work — EOS boundaries are recoverable from `.u32le.bin` — but **the tokenizer must emit it
at build time**; after tokenization the document→row mapping is gone. This belongs on §1's
irreversible list.

---

## Task D/E — substrate changes, all verified live

Detail in `artifacts/smoke/SUBSTRATE.md`. The plan named three models but not where they run, and
two of the three were unusable as specified.

| role | plan | actual | why |
|---|---|---|---|
| **D** candidate | `EAI-Distill-0.5b` | same, **self-hosted on Batch GPU** | it has *no* inference provider; 0.5 B fits the available A10G |
| **A** judge | `Qwen3-235B-A22B-Instruct-2507` (HF) | **`qwen.qwen3-next-80b-a3b`** (Bedrock) | HF Inference returns **HTTP 402, credits depleted** |
| **B** judge | `Qwen2.5-32B-Instruct` (HF) | **`qwen.qwen3-32b-v1:0`** (Bedrock) | not served by any enabled HF provider, and 32B does not fit one A10G |

The teacher attribution in the plan is correct (verified verbatim: D was distilled from
`Qwen2.5-32B-Instruct`), and Bedrock's `qwen3-32b` is a **dense 32B Qwen** — the teacher's exact
parameter count, so a closer proxy than the 72B HF sibling that was the first fallback. Both remain
proxies, so `inherited_error_rate` is an estimate; `J = agreement(A,B)` is measured on the pair
actually used, so the gate arithmetic holds regardless.

**Harness validated end to end** on 16 documents: 0 call failures, 16/16 replies parsed, **J = 75%**,
labels semantically sensible (FineMath → 5 = Science; FineWiki spread across History/Arts/SocSci/
Technology). Also handled: Qwen3 emits `<think>` blocks unprompted, which `parse_label` strips —
a digit inside the reasoning trace is not the answer.

**Scorer validated against a hand-constructed known-answer case**: J 60.0%, n_scored 6, score 83.3%
→ FAIL, inherited 50.0%, excluded 1 — every figure matched the expected value, including the FAIL at
83.3% against the 85% gate.

---

## Task A — token re-count: PARTIAL, and here is the honest reason

**A rate limit stopped it, and the diagnosis matters for anyone who re-runs this.**
`datasets-server`'s quota is **per-IP, not per-account** — verified directly: an authenticated and an
anonymous request from this machine both returned HTTP 429 in 0.1 s. My own fan-out caused it (8
category agents plus a harvest, all hitting one shared quota), and the first failures looked exactly
like broken corpora in the artifacts, which is the trap worth recording.

Two fixes applied: send the HF token (helps for other endpoints, not this limit) and give 429 an
exponential backoff to 120 s. Then the real fix — **stop the parallel load**, and for task D switch
to a different transport entirely (below).

**Measured before the quota ran out** (all `stats-ratio`, the good estimator):

| source | measured | card | note |
|---|---|---|---|
| `finemath/finemath-3plus` | **34.69 B** | 34 B | agrees within 2% — validates the method |
| `finemath/finemath-4plus` | **10.06 B** | — | ⊂ 3plus, do not sum |
| `finewiki/en` | **9.58 B** | ~3.5 B | 2.7× the card figure |
| `dclm-baseline-1.0-parquet` | 1.23 B | — | a partial conversion, not the real corpus |
| `swallow-math-v2-text` | 1.44 B | 32 B | needs re-checking |

Per-category artifacts are in `artifacts/recount/`, each recording measured values where obtained
and `rate-limited-not-measured` where not — card figures are kept separate and **never presented as
measured**.

⚠️ **Consequence for §2.1: the pool sizing is NOT yet verified.** Task F cannot be completed
against card figures without becoming the fiction §3.1 warns about. What is needed is a streaming
count on Batch for the unmeasured sources — in-region, which is where §5.7 says this work belongs
anyway.

### The method, for whoever finishes it

The naive estimator does not work. `num_rows × sampled mean_tokens_per_doc` on FineMath-3plus with
200 docs gave **CV 9.0 and a 95% CI of [26 B, 204 B]** — an 8× range, useless for sizing a pool.
Factoring it fixes that:

```
tokens = num_rows × mean_chars_per_doc × (tokens/char)
         ^exact      ^whole-split         ^sampled, and tight (CV 0.27 vs 1.47)
```

`tokens/char` is a property of script and domain, not of document length, so a few hundred docs pin
it down; the heavy-tailed factor comes from `/statistics` over the whole split. That took FineMath's
estimator divergence from 800% to 11.5%. `artifacts/recount/README.md` documents it, including that
`/statistics` genuinely fails for some corpora (DCLM HTTP 501, peS2o HTTP 500 — both verified
independently of any rate limiting).

---

## §9.5 REPORT — to be completed

    DUAL-JUDGE SMOKE TEST
    | source    | J (A↔B) | score (D on A==B) | inherited err | n scored | n excluded | verdict |
    | dclm      |       … |                 … |             … |        … |          … | … |
    | finemath  |       … |                 … |             … |        … |          … | … |
    | academic  |       … |                 … |             … |        … |          … | … |
    | reference |       … |                 … |             … |        … |          … | … |
    | qa-forum  |       … |                 … |             … |        … |          … | … |

    Human spot-check (50 docs where A≠B): <pending>

    TOKEN RE-COUNT VS PLAN
    <partial — see Task A above; streaming count required on Batch>

    INFRA
    - validator job-def timeout: SET to 7200s (rev 7)
    - bucket-policy v2 deployed: NO (reported only, as instructed)

    COST SO FAR
    <a few dollars of Bedrock + Batch; well under the <$1 smoke-test figure for judging itself>

    ARTIFACTS
    artifacts/PLAN-CORRECTIONS.md          two plan defects + one new irreversible decision
    artifacts/smoke/SUBSTRATE.md           taxonomy + model substrate corrections
    artifacts/smoke/{harvest,harvest_parquet,judge,classify_d,score,submit_classify_d}.py
    artifacts/recount/{README.md,recount.py,*.json}
    artifacts/licenses/{openstax-books.json,libretexts-distribution.json,SCHEMA.md}

    DECISION NEEDED
    Proceed with the ~$595 full domain classification (112M docs, EAI-Distill-0.5b)?
