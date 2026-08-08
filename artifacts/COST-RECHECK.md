# The ~$595 figure does not survive a measured throughput check

Written during Phase 0 execution, 2026-07-31, from the smoke test's own numbers. **This is the single
most decision-relevant finding of Phase 0**, because the ~$595 is exactly what the §9.1 hard stop
gates, and the real number appears to be several times larger.

**This is an estimate flagged for a human, not a conclusion.** It rests on one measurement extrapolated
a long way, and the direction of every unmodelled factor is stated below.

## What was measured

`EAI-Distill-0.5b` on one `g5.xlarge` (A10G 24 GB), fp16, batch 16, `max_new_tokens=100`:

```
10.1–10.6 documents/second     (steady state over 2,000 documents)
0 abstains, 0 unparseable      (the model emits the documented 10-field format cleanly)
```

## What that implies for 112 M documents

| fleet | wall clock | on-demand | spot |
|---|---|---|---|
| 1 × g5.xlarge | 128 days | ~$3,100 | ~$920 |
| 4 × g5.xlarge | 32 days | ~$3,100 | ~$920 |
| 8 × g5.xlarge | 16 days | ~$3,100 | ~$920 |
| 16 × g5.xlarge | 8 days | ~$3,100 | ~$920 |

3,080 GPU-hours total. Cost is fleet-independent (you buy the same GPU-hours either way); only wall
clock changes.

**Against the plan's ~$595, that is 1.5× at best-case spot and 5× at on-demand.**

## ⚠️ And 3,080 GPU-hours is an optimistic FLOOR, not an estimate

The smoke test classifies **256-token prefixes**. The real run classifies **full documents**. Measured
over the same 2,000 sampled documents:

```
mean full-document length   11,010 chars
smoke-test input             ~1,024 chars  (256 tokens)
ratio                        10.8x
```

Transformer prefill is roughly linear in sequence length at this scale, so full documents cost
substantially more per document than what was measured. The model's own `chunk_text` caps input at
30k chars (head + random middle + tail), which bounds the worst case but does not remove the gap.

Two smaller factors push the same way: `max_new_tokens=100` is generous for a 10-field answer, and
batch 16 at 256 tokens leaves an A10G badly underutilised — so there is real headroom from tuning
(larger batches, vLLM instead of `generate()`, bf16). **Tuning could plausibly recover a large factor.
It has not been measured, so it cannot be claimed.**

## What I did not verify, and it matters

**Where does "112 M documents" come from?** The plan asserts it without derivation, and Phase 0's
own counts make it look low: DCLM-baseline alone is ~3.0 B documents, peS2o 6.1 M, FineWiki 61.5 M,
FineMath 21.4 M. If the intent is "every document in the ~200 B-token real half," the count is
plausibly **an order of magnitude above 112 M**, and the cost scales linearly with it.

**Conversely, the classification may not need every document.** Two of the eight categories ship a
usable subdomain upstream (`stackv2-edu/<language>`, `essential-web/<topic>`), and Essential-Web
already carries `eai_taxonomy.free_decimal_correspondence` — the *exact* field this run would compute.
Any source that inherits a label instead of computing one drops out of the bill entirely.

## The honest summary for the decision

| | |
|---|---|
| plan says | ~$595 |
| measured floor, spot | ~$920 |
| measured floor, on-demand | ~$3,100 |
| with the 10.8× full-document factor, untuned | **plausibly $3k–10k+** |
| with tuning (vLLM, larger batches) | unmeasured, could recover much of it |
| if the document count is 10× the plan's | multiply again |

**Recommendation, offered not taken:** before authorising the full run, spend under an hour measuring
two things — throughput on *full* documents rather than prefixes, and the real document count per
source after excluding sources with usable upstream labels. Those two numbers turn this range into an
estimate. Both are cheap, and both are on this side of the hard stop.
