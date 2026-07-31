# Task A — token re-count under dolma2

Phase 0 task A of `DATASET-DESIGN-reservoir.md` §9.3. **Read this before running `recount.py`.**

## Why this task exists

§3.1: published token counts are not comparable. Of ~35 corpora surveyed only ~10 name their
tokenizer, AI2 and NVIDIA name none, and peS2o's advertised "47.37B" is *whitespace words* — OLMo
counts the same corpus as 58.6B. §2.1's pool sizes are load-bearing on these figures, so every
number in §3.2's source table is marked "must be re-derived under dolma2 before use."

Your job: replace card figures with measured ones, and record how confident each is.

## How to run it

```bash
python3 artifacts/recount/recount.py <hf-dataset-id> \
    --n-sample 300 --out artifacts/recount/<category>-<slug>.json
```

Defaults to every config in the dataset. `--config` narrows it. Runtime is ~2–8 min per config
(dominated by HTTP round-trips, not tokenization). Nothing is downloaded — see below.

## The method, and the one trap in it

```
tokens  =  num_rows  x  mean_chars_per_doc  x  (tokens / char)
           ^exact       ^whole-split         ^sampled, and tight
```

The naive estimator — `num_rows x sampled mean_tokens_per_doc` — **does not work**. Measured on
FineMath-3plus with 200 sampled docs it returned CV 9.0 and a 95% CI of **[26B, 204B]**. Document
lengths are heavy-tailed, so the sample mean converges far too slowly to size a pool from.

Factoring it fixes that, because `tokens/char` is a property of script and domain rather than of
document length (CV ~0.27 on FineMath, vs 1.47 for tokens/doc), while the heavy-tailed factor
`mean_chars_per_doc` comes from `/statistics` over the whole split instead of your sample.

Validated: FineMath-3plus → **34.69B**, against the card's 34B. 2% agreement, and the two
estimators now agree within 11.5%.

**The trap: `/statistics` is not always available.** It is computed per split and crashes
server-side on some large corpora — measured: `mlfoundations/dclm-baseline-1.0` returns HTTP 501
("job manager crashed"), `common-pile/peS2o_filtered` HTTP 500. When it is missing the tool falls
back to the sample mean and marks `confidence: "low"`. **Do not report a low-confidence number as
if it were measured** — say so, and note in your artifact that the corpus needs a real streaming
count on Batch before its pool is finalized.

Read these fields in every result:

| field | what it tells you |
|---|---|
| `est_total_tokens` | the number §2.1 needs |
| `estimator` | `stats-ratio` (good) or `sample-mean` (wide) |
| `confidence` | `high` / `medium` / `low` + `confidence_note` |
| `estimator_divergence` | the two estimators' disagreement; >25% means one is wrong |
| `stats_partial` | `/statistics` covered only the head of the split — provisional |
| `dolma2_to_upstream_ratio` | multiply card figures by this instead of re-counting |
| `text_column_chosen_by` | `guessed-longest-string` means **eyeball it** |

## Two things that will silently corrupt your result

1. **The wrong text column.** §3.3 trap 1: FinePhrase's rewrite lives in
   `rollout_results[0].text` while `text` holds the *original FineWeb-Edu document*. Counting
   `text` there measures the wrong corpus and no size or hash check catches it. The tool skips
   metadata-looking columns and reports how it chose, but for any nested payload pass
   `--text-column` explicitly. FineWiki is a live example: it ships both `text` (payload,
   6,615 mean chars) and `wikitext` (raw markup, 10,565) — picking the latter inflates the
   estimate ~1.6x.
2. **Lineage double-counting.** §3.1: these corpora are nested, not additive. FineWeb-Edu ⊂
   FineWeb-Edu-score-2 ⊂ FineWeb; Zyda-2 contains both DCLM-baseline and FineWeb-Edu-score-2;
   Essential-Web shares 89 of 101 snapshots with DCLM-Pool; `olmo100b` is 95% DCLM-baseline. Summing
   two overlapping sources overstates the pool. Note any overlap you see; do not add across it.

## No dataset bytes on a laptop

§5.7 is a hard constraint. This tool honors it: `/size`, `/statistics` and `/rows` are all
datasets-server API calls that serve arbitrary offsets server-side, so the traffic is a few MB
against corpora in the hundreds of GB — metadata-scale, the same class as the broker's read-only S3
calls. **Never** `load_dataset()` a corpus here, and never `hf_hub_download` a data file. If a
corpus can only be counted by streaming it, say so in your artifact and leave it for a Batch job.

## What to write

One JSON per source (the tool does this with `--out`), plus a `<category>.json` summarizing your
category:

```json
{
  "category": "math",
  "plan_pool_tokens": 36000000000,
  "sources": [
    {"dataset": "HuggingFaceTB/finemath", "config": "finemath-3plus",
     "card_tokens": 34000000000, "measured_tokens": 34688782547,
     "estimator": "stats-ratio", "confidence": "medium",
     "license": "odc-by", "note": "card figure confirmed within 2%"}
  ],
  "category_total_measured": 34688782547,
  "meets_3x_peak_demand": true,
  "overlaps": ["swallow-math-v2 is a FineMath rewrite — do not sum"],
  "needs_streaming_count": []
}
```

Return to the orchestrator **one line per source**: category, dataset, measured tokens, estimator,
confidence. Not the JSON — that is what the file is for.
