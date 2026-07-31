# Tasks D+E — inference substrate, and two plan corrections

Written during Phase 0 execution, 2026-07-31. `DATASET-DESIGN-reservoir.md` §9.4 names three models
but not *where they run*, and it names a taxonomy that turns out not to exist. Both had to be
resolved before the smoke test could be built. Everything below was verified live, not inferred from
a card summary.

---

## CORRECTION 1 — there is no "24-topic taxonomy"

The plan (§1.2, §9.4) says to classify into **"Essential-Web's published 24-topic taxonomy"** and
warns "do not invent categories." That taxonomy does not exist. What Essential-Web actually
publishes, verified from the dataset card and the model card:

> "Essential-Web uses the **Free Decimal Correspondence**, a Dewey Decimal-inspired open taxonomy
> with **12 main categories** for classifying web content."
> — `EssentialAI/essential-web-v1.0` card

> "EAI-Distill-0.5b … designed for document classification across **12 taxonomic categories**"
> — `EssentialAI/EAI-Distill-0.5b` card

**Where "24" came from:** the paper's title is *"Essential-Web v1.0: 24T tokens of organized web
data."* 24 is the **token count**, not a category count. The plan conflated them.

**What the model actually emits.** Not one topic label — **ten comma-separated fields**, verbatim
from the model card's "Output Format" section:

```
{FDC primary},{FDC secondary or skip}
{Bloom cognitive process primary (1-6)},{... or skip}
{Bloom knowledge domain primary (1-4)},{... or skip}
{Document type v1 primary (1-17)},{... or skip}
{Extraction artifacts primary (0-4)},{... or skip}
{Missing content primary (0-6)},{... or skip}
{Document type v2 primary (1-25)},{... or skip}
{Reasoning depth primary (1-6)},{... or skip}
{Technical correctness primary (1-6)},{... or skip}
{Educational level primary (1-5)},{... or skip}
```

So "24" is not any field's cardinality either. The closest numbers are **document_type_v2 (25
codes)** and **document_type_v1 (17)**.

### The fix: FDC Level 1, ten categories

The reservoir needs **one** `domain` value per document, because `domain` is one path segment
(§1.2). FDC's Level 1 is exactly that, and it is what the plan's intent points at — Essential-Web's
own published scheme, not an invented one. Verbatim from the card:

| code | label | code | label |
|---|---|---|---|
| 0 | General works | 5 | Science |
| 1 | Philosophy | 6 | Technology |
| 2 | Religion | 7 | Arts |
| 3 | Social Sciences | 8 | Literature |
| 4 | Language | 9 | History/Geography |

Ten values, hierarchical (Level 2 refines to 00–99, Level 3 to 000–999), stable, and kebab-cases
cleanly into a path segment (`social-sciences`, `history-geography`).

**Why Level 1 rather than the finer levels or document_type_v2:**

- **Level 2/3 are too fine for a path segment.** 100 or 1,000 `domain` values fragments the tree so
  far that per-domain selection stops being useful, and each extra distinct value is permanent
  (inside `manifest_sha256`).
- **`document_type_v2` is the wrong axis.** It classifies *format* — news, academic, forum, code —
  which `source` already encodes (§1.1 fuses corpus identity into `source`). Spending the one
  `domain` level on format would duplicate `source` and lose subject matter, which is the axis a
  teammate actually wants to slice on ("train on the science slice").
- **Level 1 is what the gate can realistically clear.** Ten coarse buckets is a far easier
  agreement target than 25 fine ones, and §9.4's bar is 85%.

**Consequence for the gate:** scoring is now top-1 agreement over **10** classes, not 24. Chance is
10%, so 85% remains a demanding bar. Recorded because the plan's ≥85% figure was set against a
24-class problem that never existed.

**This is a description fix, not a design change.** The plan's intent — use Essential-Web's
published scheme via `EAI-Distill-0.5b`, don't invent categories — is preserved exactly. Only the
name and cardinality were wrong.

---

## CORRECTION 2a — HF Inference is out of credits; the substrate is Bedrock

**Discovered after the analysis below was written, and it supersedes its conclusion.** The HF
router stopped answering mid-validation:

```
HTTP 402  "You have depleted your monthly included credits. Purchase pre-paid credits to
           continue using Inference Providers. Alternatively, subscribe to PRO..."
```

This is an account-level ceiling, not a per-model problem — it kills *both* judges on HF. The first
6 test documents were labelled successfully before the quota ran out, which is how the harness got
validated at all.

**Resolution: run both judges on AWS Bedrock**, which the intern role can already invoke (verified
live via `bedrock-runtime converse`, and again through threaded `boto3` using the broker's
`credential_process` profile at `/tmp/olmo150_aws/config`).

| role | plan | now | verdict |
|---|---|---|---|
| **A** independent | `Qwen3-235B-A22B-Instruct-2507` (HF) | **`qwen.qwen3-next-80b-a3b`** | same family/generation, reachable, no credit ceiling |
| **B** teacher proxy | `Qwen2.5-32B-Instruct` (HF, unreachable) | **`qwen.qwen3-32b-v1:0`** | **better than the HF fallback** — a 32B *dense* Qwen, the teacher's exact parameter count |

Bedrock is the better substrate on three counts beyond availability: it runs in-region (consistent
with §5.7), it bills to the same AWS account as everything else in this project rather than a
personal HF plan, and judge B now matches the real teacher's size instead of being a 72B sibling.

**Validated end to end on 16 documents**: 0 call failures, 16/16 replies parsed, **J = 75%**, and
the labels are semantically right (FineMath documents → 5 = Science; FineWiki spread across
History/Arts/SocSci/Technology as a general encyclopedia should).

One harness detail this surfaced: **Qwen3 models emit `<think>` blocks unprompted** even when asked
for a single digit. `parse_label` strips them before parsing, because a digit inside the reasoning
trace is not the answer.

The analysis below is kept because its *reasoning* still governs the choice of judge B — B must
share the teacher's biases to detect inherited error — and because the HF probe results document
why the plan's named models could not be used.

---

## CORRECTION 2 — judge B, the teacher, is unreachable

§9.4 assigns judge B to `Qwen/Qwen2.5-32B-Instruct`, "the model D was distilled from." The teacher
attribution is **correct** — verified verbatim on the model card:

> "**Training Data**: 82B synthetic tokens generated by **Qwen2.5-32B-Instruct (teacher model)** on
> 104M Common Crawl documents"

But it cannot be called. Probed live against the HF router:

```
Qwen/Qwen2.5-32B-Instruct  ->  HTTP 400
  "The requested model 'Qwen/Qwen2.5-32B-Instruct' is not supported by any provider you have enabled."
```

It is served only by `featherless-ai`, which is not enabled on this account. Self-hosting is not an
option either: the only GPU compute environment is **`g5.xlarge`** (one A10G, 24 GB) and a 32B model
needs ~64 GB in fp16.

### Substrate, as verified

| role | model | where it runs | status |
|---|---|---|---|
| **D** candidate | `EssentialAI/EAI-Distill-0.5b` | **self-hosted, Batch GPU** — it has *no* inference provider (checked: `providers: NONE`) | 0.5 B / ~1 GB fp16, fits `g5.xlarge` with room to spare |
| **A** independent judge | `Qwen/Qwen3-235B-A22B-Instruct-2507` | HF router | ✅ live, answered a probe |
| **B** teacher proxy | `Qwen/Qwen2.5-72B-Instruct` | HF router | ✅ live, answered a probe |

**Why `Qwen2.5-72B-Instruct` is the right substitute.** Judge B exists for exactly one purpose —
detecting **inherited error**, the §9.4 pattern where D reproduces a bad teacher label and a
single-judge design would score that as a *success*. For that, B must share the teacher's biases.
`Qwen2.5-72B-Instruct` is the same family, generation, and instruction-tuning recipe as
`Qwen2.5-32B-Instruct`, so its errors should correlate with the teacher's in a way an unrelated model's
would not.

**It is a proxy, and the report must say so.** A 72B sibling is not the 32B teacher, so the measured
`inherited_error_rate` is an *estimate* of inherited error, not a direct measurement. Two honest
consequences:

- If the proxy is a *better* classifier than the teacher, it will side with A more often, which
  shifts cases out of the "D = B, A differs" bucket and **understates** inherited error.
- The `J = agreement(A, B)` ceiling is measured on the pair actually used, so the gate arithmetic
  stays sound regardless — it just describes this pair.

Rejected alternatives: `Qwen2.5-Coder-32B-Instruct` (right size, live, but code-specialized — wrong
instrument for subject classification); `Qwen2.5-7B-Instruct` (HTTP 403); self-hosting the real
teacher (won't fit).

---

## Cost, and why the smoke test stays under $1

Judges run over ~500 docs/source × 5 sources = 2,500 documents, at 256-token prefixes (§9.3 task D).
That is ~0.64 M prompt tokens per judge and a handful of output tokens each — small enough that both
judges together stay comfortably inside the plan's <$1 figure. The candidate D runs on Batch GPU at
`g5.xlarge` spot rates for well under an hour.

The ~$595 this gates is the **full** run: 112 M documents through D. That asymmetry — under a dollar
to decide, ~$595 plus a permanent labelling commitment to be wrong — is the whole reason §9.1 stops
here.
