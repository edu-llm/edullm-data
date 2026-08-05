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

## The realized mix (train split, 250,242,924,544 tokens)

Recomputed from the 27 receipts on 2026-08-05, after the nine-bundle re-run; machine-readable in
`artifacts/reservoir/realized-tokens.json`. **Two figures in the earlier version of this table were
wrong**, both in the direction that flatters the corpus, which is why item 2 below says to derive
these from receipts rather than retype them:

- the header said "251.1B tokens" for the train split. That was the **total** (251,218,001,920).
  Train is 250,242,924,544; val is 975,077,376.
- `reference` was listed at 8.8B / 3.5%. Realized is **7.92B / 3.16%** — `finewiki--train` has 33
  unfilled refs and landed at 90.5% of its plan, so the planned figure was being cited as measured.

| category | tokens | share of train | sources |
|---|---|---|---|
| synthetic | 59,604,729,856 | 23.82% | 4x FinePhrase (faq, math, table, tutorial) |
| edu-web | 47,728,787,456 | 19.07% | finepdfs-edu, fineweb-edu |
| code | 39,778,156,544 | 15.90% | stackv2-edu |
| math | 33,827,684,352 | 13.52% | finemath |
| web-diverse | 29,827,366,912 | 11.92% | dclm |
| academic | 19,876,577,280 | 7.94% | peS2o, pubmed |
| qa-forum | 11,679,465,472 | 4.67% | stackexchange, ubuntu-irc |
| reference | 7,920,156,672 | 3.16% | finewiki (**partial: 90.5% of plan**) |

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
   `edullm-validator` (**rev 12** as of 2026-08-04, running as
   `sbsandbox-intern-edullm-dataset-validator`) manually, or re-enable the rule first. Decide which
   BEFORE publishing, not after. **Do not hardcode a revision** — both EventBridge rules target the
   job def by *unversioned* name, so a manual submission naming an old revision runs a different
   validator than the automatic path would. Re-read the current revision before submitting.
4. **`publish()` has nowhere to run yet.** Item 3's premise is that this runs on Batch, but there is
   no reservoir publish job definition — `edullm-prm800k-publish` is a different corpus with a
   different entry point (`edullm-prm800k-ingest publish --run-id`). One must be registered first;
   see "The publish job definition" below.

## The two image lines, and why the version string cannot identify the code

`validator:12` and the image that built this corpus come from **branches that diverge** at
`a372bf8`, and **two different commits both call themselves `0.7.4`**:

| commit | version | `distinct_ids_min` | line |
|---|---|---|---|
| `7a97c27` | 0.7.4 | 256 | reservoir — **built this corpus** (`edullm-reservoir-build:9`) |
| `d08aa05` | 0.7.4 | 128 | vendored/prm — lowered the bound for a 152k-vocab Qwen corpus |
| `e0984c8` | 0.8.0 | 128 | the merge of both; **`validator:12`'s image** |

So `assert __version__ == '0.7.4'` — which every build job definition does — is satisfied by two
different trees with different gate bounds. A version assertion is not a code identity; the ECR tag
(a commit sha) is. `validator:10` is older still: its image is tagged
`prm800k-codebuild-20260731T193909Z-d732af0e67fe`, a CodeBuild commit **not present in this repo**.

**Measured, so this does not block the publish.** The bound is enforced off the same bytes in two
places (`pretrain_tokens_v1.check_decode_smoke` and `corpus_pack._verify_shard`), so a corpus built
at 256 and validated at 128 is validated *more loosely* than it was built — never the reverse. And
it is moot here: sampling Gate A's own window (65,536 B in 4 windows) from the **smallest shard of
each of the 27 bundles** gives 2,278–4,507 distinct ids against a floor of 256 — a ≥8.9x margin on
the worst case, `ubuntu-irc--train`'s 3.5M-token tail. Every sampled id is also `< 100278`. Either
validator passes this corpus on this check.

## The publish job definition

`publish()` stream-hashes every object, so it PULLS every byte to wherever it runs — 587 GiB at the
0.8 MiB/s this project measured off-region is ~9 days. It must run in-region. Model it on
`edullm-reservoir-build-force:1` (which was itself cloned from `edullm-reservoir-build:9`), not on
`edullm-prm800k-publish:2`, whose 2 vCPU / 4 GiB and 7200 s timeout are sized for a small corpus:

- image: the same digest that built the corpus, or a strictly newer one from a branch that
  **contains** `7a97c27` — check ancestry with `git merge-base --is-ancestor`, do not trust versions
- `hash_workers=16`, `copy_workers=16` (single-threaded publish timed out on the 218-shard/125 GB
  olmo run), and `--timeout attemptDurationSeconds=7200` or more — 10,049 shards is ~50x that corpus
- `executionRoleArn` MUST be set, or the container starts with no readable logs (this cost a full
  diagnosis cycle once: the symptom looks like a missing log group)
- the role needs `PutObject` on `edullm-landing` only. It must NOT have it on `edullm-data` — that
  bucket is writable solely by the validator role, which is the airlock, and is an IAM Deny rather
  than a convention.
