# `pretrain/final-dataset` — what we are building and why

**For the whole team. Revision 3, 2026-08-07.** Status: **plan awaiting owner approval. Nothing
ingested.** Branch `final-dataset` @ `origin/edullm/perf-threaded-verify-and-gatea` (0.9.1, the
deployed code).

---

## 1. The one-paragraph version

We are building a **1.0-trillion-token pretraining corpus** for **M20** — a **20.0B-total /
1.279B-active** Mixture-of-Experts model, education-focused, steered on MMLU, GSM8K, ARC and
HellaSwag. The corpus ships in **two stages**, because no comparable published run used a flat
mixture: a **900B bulk phase at 77% web**, then a **100B cooldown at 32% web** that concentrates
math, code, QA and reasoning traces. Every token is headerless raw `uint32` little-endian under the
**dolma2 tokenizer**, which we are keeping — that also makes AI2's 5.93T pre-tokenized corpus a byte
copy rather than a re-tokenization. **No source exceeds 0.90 epochs**, so nothing repeats.

**Why revision 3 exists.** Revision 2 was written for a 40B/4B model at 1.3T with a *flat* 54% web
share. All three of those numbers were wrong. §8 lists every claim that changed and why. Read it
before relying on any figure here.

---

## 2. The model, from the code rather than from a conversation

| | |
|---|---|
| total params | **20,002,742,272** |
| active params | **1,279,369,216** |
| sparsity | **15.63 : 1** |
| shape | 256 experts, **top-8, zero shared**, d_model 2048, L=24, GQA 16/4 |

Pinned at `OLMo-core/src/olmo_core/nn/transformer/config.py:1125` (`MAPLE_RUNGS["M20"]`, commented
*"THE MISSION DELIVERABLE"*) and `:1166`. `maple/HANDOFF.md`: *"Active is 1.279B, not 1.49B… **Plan
against 1.279B.**"*

This differs from how the model has been described verbally — "20B total / 2B active at 10:1" is
**wrong by 56% on active params**. On the non-embedding convention most model cards use it is 0.868B
active = 22.56:1. The 20.0B total is *correct* rather than an error: it differs from DeepGrove's
published 20.2B/1.49B only by vocabulary, and the gap is exactly `2·2048·(151,936−100,352)`.

---

## 3. Stage 1 — bulk, 900B (90% of budget), 77% web

| source | share | tokens | pool | epochs | grade |
|---|---|---|---|---|---|
| DCLM-baseline | 42% | 378.0B | 744.6B | 0.51 | MEASURED |
| FineWeb-Edu | 28% | 252.0B | 1,583.1B | 0.16 | MEASURED |
| `common-pile/stackv2` | 10% | 90.0B | 707.0B | 0.13 | DERIVED |
| FinePDFs-Edu | 7% | 63.0B | 70.0B | 0.90 | CORRECTED |
| Nemotron-CC-Math-3+ | 5% | 45.0B | 133.0B | 0.34 | CARD ⚠️ |
| FinePhrase (1 partition) | 4% | 36.0B | 123.3B | 0.29 | MEASURED |
| academic (peS2o + PubMed + arXiv) | 2% | 18.0B | 46.6B | 0.39 | MEASURED |
| reference (Wikipedia + pre-1929 books) | 1% | 9.0B | 26.2B | 0.34 | MEASURED |
| StackExchange | 1% | 9.0B | 25.9B | 0.35 | MEASURED |

## 4. Stage 2 — cooldown, 100B (10% of budget), 32% web

| source | share | tokens | pool | epochs |
|---|---|---|---|---|
| DCLM-baseline | 32% | 32.0B | 744.6B | 0.04 |
| `common-pile/stackv2` | 18% | 18.0B | 707.0B | 0.03 |
| Nemotron-CC-Math-3+ | 16% | 16.0B | 133.0B | 0.12 |
| **AI2 dolma3 midtraining mix** | 14% | 14.0B | 100.0B | 0.14 |
| reasoning traces / worked examples | 8% | 8.0B | ~50B | 0.16 |
| Cosmopedia | 4% | 4.0B | 21.7B | 0.18 |
| Nemotron Math-Textbooks | 3% | 3.0B | 27.5B | 0.11 |
| academic | 3% | 3.0B | 46.6B | 0.06 |
| reference | 2% | 2.0B | 26.2B | 0.08 |

**Combined: 1,000B, max epoch 0.90, nothing repeated.** DCLM 41.0% · FineWeb-Edu 25.2% · code 10.8% ·
FinePDFs 6.3% · math 6.1% · synthetic 4.3% · everything else 6.3%.

**FineWeb-Edu lineage is 28.8%** — that matters because FinePhrase *is* rephrased FineWeb-Edu, so
"edu-web" and "synthetic" are partly the same upstream pool. Revision 2 had this at **54.5%**: a
quarter of the corpus was one pool entered twice under two labels. No published corpus does that.

---

## 5. Why two stages, and why 77% then 32%

**No comparable published run used a flat share.** OLMo 2 §2.3 (arXiv 2501.00656, read directly):
stage 1 is *"≥90% of training FLOPs"* and *"mostly web-sourced"*; stage 2 *"up-sample[s] the
highest-quality web documents and curated non-web sources"* plus *"synthetic data crafted to patch
math capabilities."* OLMo 2 stage 1 is **95.1% web**, Olmo 3 **76.1%**, Olmix fixes web at **75%**;
stage 2 drops to 27.5–52%.

Revision 2's flat 54% was therefore wrong in *shape*, not just level — about 25pp too low for the
bulk and 20pp too high for the anneal.

**And at our size the cooldown is where the metrics actually move.** At 1B params, 4T tokens of
95%-web pretraining leaves MMLU near chance; mid-training moves MMLU **+17.4** and GSM8K **3.3 →
43.8**. That benefit is *largest* at our scale (**+37.0% at 1B vs +12.3% at 32B**). At 7B, across
seven mid-training mixes, **MMLU spans 1.3 points while GSM8K spans 27.5**.

**Web is DCLM-led rather than FineWeb-Edu-led**, on direction only. DCLM's Table 8 is token-matched at
276B/7B and favours DCLM on MMLU — but hyperparameters were tuned on DCLM-baseline, and under
FineWeb-Edu's own eval protocol the same paper says the two *"perform quite similarly."* Nemotron-CC
and SmolLM2 place FineWeb-Edu on **opposite sides** of DCLM. So we keep both, weighted toward DCLM,
and treat the choice as unsettled. **"Which web" is worth ~16 MMLU at constant share — 4–8× any share
effect** — which is why the split matters more than the percentage.

---

## 6. QA and worked examples — first-class, and they pull in opposite directions

These were requested as explicit categories. The measured evidence says they must be **two** categories,
not one. OLMo 3's compute-matched microanneals, each against a web-only control:

| intervention | MMLU | ARC | GSM8K | Minerva | HumanEval |
|---|---|---|---|---|---|
| QA / MC-format (Table 8, 10B) | **+3.2** | **+2.6** | −1.2 | −1.6 | −1.5 |
| reasoning traces (Table 9, 5B) | **−1.5** | +0.1 | **+8.4** | **+7.3** | **+11.6** |

**They partially cancel on our four headline metrics.** Any single "QA and worked examples" share
would be arithmetically wrong. QA saturates at **~14% of the anneal** — AI2 gave it 13.9%, and beyond
that the curve is flat (−0.1 / +0.7). Hence 14% dolma3-midtraining plus 8% reasoning traces in stage 2,
scheduled separately.

**Two honesty notes the team should carry.**

**The QA gain is format-matching, not knowledge.** AI2's QA is GPT-4o-mini-rewritten *multiple-choice*
from academic subreddits — format-matched to MMLU and ARC by construction. It does **not** transfer to
StackExchange, which is prose Q&A. Our own P1 paper measured the ceiling directly: 65.24% under
QA-shaped prompts collapses to **3.02%** chat-style and **0.00%** short-answer. P1 §2.6 lists
format-independent knowledge as *not supported*.

**The honest MMLU expectation from QA data is +0.** The one experiment above the measurement threshold
— Nemotron-CC at **8B/1T**, where MMLU is live at 48–53 — gives synthetic QA **+0.2 / −1.1**. The
often-quoted Nemotron-CC "+5.6 MMLU" is *classifier selection*, not QA synthesis. And note the LR
anneal alone gave **+2.0 MMLU** in OLMo 2, which is routinely misattributed to the data.

**Worked examples buy math capability and cost something else, in two independent experiments.** OLMo 3
at 7B: **+8.4 GSM8K, −1.5 MMLU**. Our own P1 at 370M: Pass@8 13.49 → 18.21, but **PassRatio@8 fell on
every scaffolded arm** (5.11 → 4.33/4.52/4.59), and the pedagogically-ordered fade *lost* to random
order. Both find capability up, reliability or knowledge down.

---

## 7. Measurement — the constraint that would have voided our own experiments

**Multiple-choice scoring is a separately-acquired skill and is random below ~400B tokens.** OLMES
(arXiv 2406.08446) Figure 1 caption, verbatim: *"During early training, there is good signal from CF
while MCF is random. **Around 400B tokens, the model starts gaining the ability on the MCF format.**"*

That sorts the literature into uninformative nulls and real zeros — and it invalidates part of our own
plan, because **P1's probes ran 300M–10B tokens, far inside the dead zone.** Any MMLU column from a
probe at that budget measures nothing.

**So: score CF (cloze), or use bits-per-byte.** P1 in fact used BPB — its 1.6062 vs 1.6329 figures are
bits-per-byte, which is why that result stands. AI2 separately abandoned accuracy metrics at 1B/100B as
*"too difficult to show improvement."* The relationship **inverts** at the top end (93.7% MCF vs 69.0%
CF on ARC-Challenge), so this is scale-dependent, not a blanket rule.

**Gate on MMLU and GSM8K directly, at fixed intervals, and treat a knowledge regression as a stop
condition.** Knowledge collapse is the most reproducible harm in this literature — phi-4's
13.8×-synthetic arm cost **TriviaQA −14.8**; MoE data reuse took **MMLU 32.92 → 24.57**; Qwen2's 12T
relaxed-quality run showed *no* improvement over 7T. **Repetition costs knowledge and spares
reasoning**, which is exactly our weighted axis, and no loss- or BPB-based gate sees it.

---

## 8. Contamination — read this before trusting any eval number

Seventeen corpora audited from papers and source code rather than dataset cards. **Nobody earns a
TRUST verdict.** Two findings are directly actionable:

1. **Nemotron-CC-Math's decontamination gate effectively never fired.** An independent 13-gram scan
   found **11,868 contaminated documents remaining against MATH500 alone — 13.2× more than NVIDIA's
   entire removal budget** — including verbatim GSM8K test items at Jaccard 1.0. This does not change
   the pillar choice (every alternative scores worse) but it makes mitigation mandatory.
2. **The Common Pile ships GSM8K in Flan CoT format** (`fc-cot-cot_gsm8k`, 6 repeats, ~9× cooldown
   upweight). AI2 dropped Winogrande *the benchmark* rather than the data, and GSM8K was not in their
   suite so it never triggered the reflex. **Fix: drop one source, 0.51% of tokens.**

| tier | action | cost |
|---|---|---|
| **1** | **exclude 9 items at source — carries nearly all the value** | **<1% of tokens** |
| 2 | chunked embedding cos≥0.5 → LLM judge, on the anneal + synthetic + question-bank slices | $500–2,000 |
| 3 | 13-gram as an **instrument**, not a filter — publish the number | $10–50 |
| 4 | **do not do** — including our own previously-decided `decon --ngram_size 5` | — |

Three things everyone should know: **a quality classifier is a contamination amplifier** (the DCLM
classifier puts *all* MMLU and GSM8K needles above the 99th percentile — and DCLM is now our largest
source); **13-gram matching scores F1 0.926 on verbatim text but exactly 0 on rephrased text**, so our
synthetic slice is structurally un-decontaminatable by n-grams; and **HellaSwag is negative in four
independent papers** while being where contamination persists longest — our least-served and
least-instrumented metric from both directions.

---

## 9. Build and cost

**Tokenizer: dolma2, kept.** No alternative clears zero measured downstream gain — SuperBPE is a null
at MoE scale with a code regression, a bigger vocab is contradicted by the resolved scaling law, and
`dolma2_sigdig` is a 29-minute abandoned experiment AI2 never trained on. Keeping dolma2 also means
**AI2's pre-tokenized shards are byte-identical to our format** (verified by range-read: no NumPy
magic, valid ids from byte 0), so their corpus is a copy.

**Shards: 50,003,968 tokens → ~20,000 objects.** Both earlier objections were wrong: the 8192 alignment
is *not* mandatory (the check is skipped unless a group declares `seq_len`, and ours does not), and
mixture error is bounded by `1/shards_per_component` — **0.33% worst case, not 4.8%**.

**⚠️ H100 is not provisioned.** Live compute profiles are T4 / L4 / L40S / A10G / `gpu-8xa100`; both
H100 shapes went unprovisioned after 6,815 capacity failures. **Every 400 TFLOP/s figure in our
research reports is 3.2–4.3× optimistic.**

| | 8×A100 (today) | 8×H100 (if unblocked) |
|---|---|---|
| **1.0T tokens** | **$69.9k / 88.8 days** | **$36.7k / 27.8 days** |

**Unblocking H100 is worth more than any budget decision available to us.**

**The one genuinely MoE-specific requirement: micro-batches must not be domain-pure.** Load-balancing
loss computed over a domain-pure micro-batch forces uniform routing and suppresses expert
specialization — worth **0.13–0.18 PPL and +5–6 GSM8k**, more than a 50% increase in activated FLOPs
buys. Our shards are per-source, so every micro-batch is domain-pure by construction, and
`MoELoadBalancingLossGranularity` defaults to `local_batch` in four places. At M20's 15.63:1 with zero
shared experts this is documented as routing **collapse**, not merely suppressed specialization. It
must be fixed **before** training: switching at 10% of the run recovers only ~55%.

**Everything else about the corpus we choose exactly as for a dense model** — Qwen3 published that a
MoE reaches dense performance *on the same data*.

**The one unbackfillable decision: store a per-document quality percentile as a label, at ingest.**
Every 2026 corpus reweights on per-document quality and topic labels. `entry.labels` sits inside
`manifest_sha256`, so this cannot be added later without re-copying the corpus. A wrong budget is a
config field; a wrong schema is a v2.

---

## 10. What changed since revision 2, so you can calibrate

| claim | correction |
|---|---|
| "40B total / 4B active" | **20.0B / 1.279B**, from the code |
| "1.3T budget" | **1.0T**, on Ling-mini-beta parity (active params match to 1.6%, d_model identical, measured parity with a dense 6.11B at exactly 1T) |
| "flat 54% web" | **two stages, 77% then 32%** |
| "8192 alignment is mandatory" | Conditional on a `seq_len` our corpus never declares |
| "50M shards cost 2.4→4.8% error" | Off by ~15×. Real bound 0.33%. **This was used to justify a decision** |
| "code caps at 74.81B" | `common-pile/stackv2` was never checked. **~707B** |
| "FineWeb authors recommend 50% FinePDFs" | Traces to a card saying *"inspired by"* an unaffiliated blog using a **70M-param GPT-2**. Their real guidance is *"keep PDF below 25%"* |
| "NVIDIA replicates DCLM > FineWeb-Edu" | Those deltas appear **nowhere** in that paper; its controlled test has **FineWeb-Edu winning** |
| "MegaMath: 18× the data, worse result" | The anneal **is** token-matched; my framing was backwards |
| "min_ngram 13→5 costs −11.8 MMLU" | A **deduplication** result, not decontamination |
| "77.5% Common Crawl is a problem" | Normal — Llama 1 was 82%, dolma3 76% |
| "Aryabumi brackets our scale" | Its code sweep ran **only at 470M** |

**Still unverified, and flagged rather than used:** Gate A's cost at ~20,000 objects (our own
extrapolation, never tested); Nemotron-CC-Math's 133B (card-grade); FinePDFs-Edu's ~70B (byte-derived,
after the recorded 161.07B proved wrong by 2.3×); MegaMath-Web's post-filter pool size; and Dolma 3's
adult-content prevalence, which is **blocking** for that source.

**The largest residual risk: every mixture study cited in our seventeen research reports is DENSE. No
mixture ablation on a sparse MoE exists at any scale.** Transfer to M20 is unverified, and that unknown
is larger than any error listed above.

---

## 11. What happens next

1. **Wire `keeps_id` into the build path.** It exists, is tested on 287,000 ids, and is called from
   nothing that writes data — so today's declared synthetic rests on ~28% as many real documents.
2. **Fix `MoELoadBalancingLossGranularity`** (§9) and the **missing EOS** in `gigatoken-superbpe`,
   where `added_tokens: []` makes Gate A's EOS check skip silently in two published corpora.
3. **Drop `data_provenance_initiative`** — 0.51% of tokens, removes GSM8K-in-Flan-CoT.
4. **Decide the label schema** (§9, unbackfillable).
5. **Measure the three blockers:** Dolma 3 adult content, Nemotron-CC-Math contamination post-fix, and
   a real dolma2 count for Nemotron-CC-Math.
6. **Then ingest**, source by source, each as a separately-approved platform job.

Every AWS job goes through the platform form and needs a human release. Nothing auto-publishes.
