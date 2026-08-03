# Publish spec — `pretrain/reservoir-dolma2`

Both irreversible decisions were confirmed by the owner on 2026-08-03. This file exists so the
`publish()` call is transcribed rather than reconstructed from memory, and so the reasoning behind
the two decisions survives the session that made them.

## Decision 1 — the name is `pretrain/reservoir-dolma2`

Validated mechanically, not by eye:

```
validate_dataset_id('pretrain/reservoir-dolma2')  -> ('pretrain', 'reservoir-dolma2')  OK
```

**No size in the name**, deliberately. `pretrain/reservoir-dolma2-252b` also validates, and the
sibling corpora in the bucket do carry budgets (`olmo-150b-dolma2`, `regmix-10b`, `fineweb-edu-1b`).
It is omitted anyway because those names describe corpora built to a budget, and this one is a
**reservoir**: teammates draw their own 20B mixtures from it via `build_mixture`. A token count in
the name would read as "train on 252B", which is the opposite of the point. The realized count is
in `dataset.json` and the README either way.

`pretrain/reservoir-final` was checked and is correctly REJECTED — `final` is a version token.

## Decision 2 — publish with the synthetic half UNDECONTAMINATED, and say so

**59.6B tokens (23.7% of train) have no effective decontamination.** The pipeline runs a 13-word-gram
blake2b matcher against a 3.1M-ngram eval index, proven at 40/40 verbatim GSM8K questions caught with
0/2 false positives. FinePhrase is *rephrased* FineWeb-Edu, and rephrasing is precisely what defeats
n-gram matching — so the gate runs over the synthetic bundles and finds almost nothing, which is not
the same as there being nothing.

Approved rather than fixed because the alternative (`lm-sys/llm-decontaminator`, an LLM-judged second
tier, ~$200 over 60B tokens) is unbuilt and would block the publish for days. The corpus is more
useful published with a stated limitation than delayed for an unstated one.

**This MUST reach `limitations[]`.** It is the one thing a consumer cannot recompute from the
artifact: a wrong benchmark score months from now looks like a modelling result, not a data defect.
The README renders `limitations[]`, and absent sections are omitted rather than faked — so silence
here would read as "decontaminated".

## The realized mix (train split, 251.1B tokens)

| category | tokens | share | sources |
|---|---|---|---|
| synthetic | 59.6B | 23.7% | 4x FinePhrase (faq, math, table, tutorial) |
| edu-web | 47.7B | 19.0% | finepdfs-edu, fineweb-edu |
| code | 39.8B | 15.8% | stackv2-edu |
| math | 33.8B | 13.5% | finemath |
| web-diverse | 29.8B | 11.9% | dclm |
| academic | 19.9B | 7.9% | peS2o, pubmed |
| qa-forum | 11.7B | 4.7% | stackexchange, ubuntu-irc |
| reference | 8.8B | 3.5% | finewiki |

Val: 0.975B (0.39%). `ubuntu-irc` has **no val split** — a val shard needs 5,000,396,800 source
tokens at `VAL_FRACTION` 0.005 and the source holds 1.87B, so whole-shard selection cannot produce
one. Nothing leaks and nothing is lost (its documents all go to train), but "which sources have no
held-out data" must be answerable from the artifact, so it goes in `notes`.

## License — the honest answer is "mixed, with share-alike"

Not a single id. Per source:

| license | sources |
|---|---|
| ODC-BY-1.0 | finepdfs-edu, fineweb-edu, dclm, finemath, all 4 FinePhrase |
| CC-BY / CC0 (mixed) | peS2o, pubmed |
| Blue Oak (permissive per-doc) | stackv2-edu |
| **CC-BY-SA-4.0** | stackexchange, finewiki (finewiki also GFDL) |
| Public Domain | ubuntu-irc |

**`CC-BY-SA-4.0` on stackexchange + finewiki is the one that constrains downstream use** — 20.5B
tokens (8.2%) carry share-alike. A `license={"id": ...}` naming one identifier would be false, so
the per-source `sources[]` entries carry their own and the top-level stays as the family's honest
`unknown` with the detail in `notes`.

## The call

```python
from edullm_data.publish import publish
from edullm_data.s3 import Boto3S3

publish(
    "s3://edullm-landing/_ingest/reservoir-dolma2/build/d5c9bcd38735e1f0/data/",
    dataset_id="pretrain/reservoir-dolma2",
    purpose=(
        "252B-token multi-source reservoir for the eduLLM team to draw 20B-token training "
        "mixtures from, to compare data mixes at fixed compute"
    ),
    profile="pretrain-tokens/v1",
    tokenizer="tokenizer/dolma2-bpe",
    s3=Boto3S3.default(),
    created_at="<ISO-8601 UTC at publish time>",
    # MUST run on Batch in-region: publish() stream-hashes every object, so it PULLS every byte to
    # wherever it runs. Measured on a laptop against a 587 GiB corpus: 0.8 MiB/s = ~9 days.
    hash_workers=16,
    copy_workers=16,
    about=(
        "Eight source categories tokenized with the published dolma2-bpe tokenizer into exact "
        "25,001,984-token shards. Held-out documents are carved BEFORE tokenizing by a hash of "
        "(source, document id), so val is drawn from different documents than train rather than "
        "sampled from the same shuffled pool. Exact-duplicate documents are removed within each "
        "bundle, and a 13-word-gram index of eval benchmarks is used to drop contaminated "
        "documents -- see limitations for where that does not reach."
    ),
    sources=[...],   # one entry per source: name, tokens (REALIZED, from each receipt), license, uri
    limitations=[
        {"kind": "contamination",
         "detail": (
             "The 59.6B-token synthetic portion (23.7% of train, the four FinePhrase configs) is "
             "effectively UNDECONTAMINATED. FinePhrase is rephrased FineWeb-Edu, and rephrasing "
             "defeats n-gram matching, which is the only decontamination this corpus applies. The "
             "13-gram gate is verified effective on verbatim text (40/40 GSM8K test questions "
             "caught, 0/2 false positives) and should be assumed ineffective on this portion. A "
             "second, LLM-judged tier was scoped at ~$200 and NOT run."
         )},
        {"kind": "coverage",
         "detail": (
             "ubuntu-irc has no val split: one val shard requires 5,000,396,800 source tokens at "
             "val_fraction 0.005 and the source holds 1.87B, so whole-shard selection cannot "
             "produce one. Its documents are all in train."
         )},
    ],
    notes=(
        "Licensing is MIXED and includes share-alike: stackexchange and finewiki (20.5B tokens, "
        "8.2%) are CC-BY-SA-4.0, finewiki additionally GFDL. Per-source licenses are in sources[]; "
        "there is no single dataset-level license id. Shard granularity is 25,001,984 tokens, so a "
        "mixture drawn from this reservoir has ~0.1-0.4% weight granularity per source (>=238 "
        "shards each, except ubuntu-irc at 71)."
    ),
)
```

## Before running it

1. `corpus_build verify --plan-id d5c9bcd38735e1f0 --deep` must pass. `--deep` re-hashes every
   payload byte and is the ONLY payload re-hash in this pipeline; `verify` without it checks sizes.
2. `sources[]` token counts come from the **receipts**, not the plan — `tokens_out` is what was
   realized. `stackv2-edu--train` landed at 100.00% of plan but that is not guaranteed, and citing
   planned figures as measured ones is exactly the honesty failure `scope:
   "upstream-full-collection"` exists to flag.
3. **`edullm-landing-manifest-created` is DISABLED.** Writing `manifest.json` normally fires
   EventBridge -> the validator -> promotion. With the rule off, nothing auto-promotes: submit
   `edullm-validator` (currently rev 10, running as `sbsandbox-intern-edullm-dataset-validator`)
   manually, or re-enable the rule first. Decide which BEFORE publishing, not after.
