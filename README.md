# edullm-data

Publisher, validator, and reader for the eduLLM dataset standard. The standard itself — naming,
manifest shape, the airlock, profiles, validation gates — lives at
[`docs/dataset-creation/DATASET-STANDARD.md`](docs/dataset-creation/DATASET-STANDARD.md). This
package is code that implements that document; it is not a second source of truth. If the two disagree,
the standard wins and this package has a bug.

## Start here

| You are… | Read |
| --- | --- |
| brand new to this | [`docs/ONBOARDING.md`](docs/ONBOARDING.md) — the 2-minute mental model |
| **about to build a dataset** (no bytes yet) | [`docs/DESIGN-A-DATASET.md`](docs/DESIGN-A-DATASET.md) — the decisions that can't be undone later |
| publishing or reading data that exists | [`USAGE.md`](USAGE.md) |
| adding a profile / changing a check | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

## What is in `s3://edullm-data` right now

Two datasets. **6,927 objects, 586.6 GiB** (as of 2026-07-30):

| Dataset | Contents |
| --- | --- |
| `pretrain/olmo-150b-dolma2/v1` | **157,467,202,883 dolma2 tokens** across 6,911 headerless uint32 shards — 6,851 `train` + 60 `val`. One `tokens/` group nested `<source>/<domain>/`, so every shard's provenance is in its key and mirrored into `entry.labels` (which Gate A recomputes from that key). 6 sources / 65 source-domain strata. |
| `tokenizer/dolma2-bpe/v1` | `allenai/dolma2-tokenizer` — tokenizer.json, merges.txt, vocab.json, configs. `vocab_size 100278` / `eos 100257` **derived** from tokenizer.json, never typed. The corpus above pins it by `manifest_sha256`. |

Read it the way a trainer does — **always pass `r.dtype` to the loader**, because OLMo-core
defaults to `uint16` and reading these `uint32` shards as `uint16` does not raise, it silently
doubles the token count:

```python
r = dataset_paths("pretrain/olmo-150b-dolma2", "v1")   # trainable data only
r.dtype        # "uint32"   -> pass this explicitly
r.numpy_dtype  # "<u4"      -> little-endian is explicit; np.dtype("uint32") is host order
r.train, r.val # 6,851 and 60 shards, disjoint
```

⚠️ **"Two datasets" is stale — there are TEN as of 2026-08-01.** Verified live by
`aws s3 ls s3://edullm-data/_catalog/ --recursive`:

```
pretrain/lean4-mathlib-bytes/v3      pretrain/olmo-original-30b/v1    tokenizer/bytes-utf8/v1
pretrain/math-memory-full/v1         pretrain/refhq-regmix-5p5b/v2    tokenizer/dolma2-bpe/v1
pretrain/olmo-127b/v1                pretrain/regmix-10b/v1           vendor/openai-prm800k/v1
pretrain/olmo-150b-dolma2/v1
```

`_catalog/` is the authoritative list; never this file. `pretrain/regmix-10b/v1` is the one a real
training run has read end to end (see `HANDOFF.md`). The object/byte totals quoted above cover only
the two originally-listed datasets and are correspondingly low.

An earlier corpus, `pretrain/olmo-mix-1124-31b/v1` (31.3B tokens, 218 shards), was deleted on
2026-07-29: it had no `val` split and, being frozen, could not gain one, so under
validation-required-by-default it failed permanently. Its bytes remain recoverable two ways — a
byte-identical legacy copy at `s3://edullm-datasets/olmo30b/` (218 `.npy` shards,
125,336,003,336 bytes) and 342 noncurrent versions in the bucket itself.

~~Publishing a replacement corpus is the open work.~~ Replacements were published (see the ten-dataset
list above) and one has been consumed by a real training run. The open work is now
`pretrain/reservoir-dolma2`. See `HANDOFF.md`.

## Install

Public repo — the `git+https` install needs no auth. Pin a tag so the publisher and validator agree:

```bash
uv add "edullm-data @ git+https://github.com/edu-llm/edullm-data@v0.6.3"
```

For local development, editable install with test extras:

```bash
python3 -m pip install -e ".[test]"
```

## Publishing a dataset

Four arguments, everything else derived or inherited from `<family>/family.json`:

```python
publish(source,
        dataset_id="eval/mcq-arc",
        purpose="ARC/OpenBookQA/SciQ loglikelihood scores for 23 baseline models, "
                "for the adaptive-inference baseline table",
        profile="eval-results/v1")
```

`source` is a local path or an `s3://edullm-landing/...` URI already written by a Batch job. `publish()`
only ever writes to landing — never to `edullm-data` — and if `source` is already in landing it just
seals the manifest without moving bytes.

### The generated README

Every promoted dataset carries a `README.md` at its prefix, **generated from `dataset.json`** by the
validator (§3: the README is a derived artifact, never hand-written). Pass optional descriptive
fields to `publish()` to fill it — `about` (one curated prose block), `sources` (the data-mix table;
mark a source `scope:"upstream-full-collection"` to print an honesty caveat when it's upstream
figures rather than this dataset's measured mix), `license`, `notes`, `limitations`. None are
validator-required; they only feed `render_readme()` (`src/edullm_data/readme.py`). Sections whose
data is absent are omitted, never faked. Already-promoted datasets get a README too — it's a control
file, backfilled in place without touching any payload byte or manifest hash.

## The one thing to know

**Producers write ONLY to `s3://edullm-landing`.** The validator role
(`<BATCH_JOB_ROLE>`, service-assumable only — no human or intern session can
assume it) is the *only* principal with `PutObject` on `s3://edullm-data`. That is an IAM bucket-policy
Deny, not a convention.

This package is a convenience, not the gate. If you refuse to install it and hand-roll a manifest, the
airlock still catches you the same way it catches everyone else — you cannot write directly to the read
bucket regardless of what tooling you use. Use the package because it's easier, not because it's
required.

## Package tree

Status legend: **done** — shipped and in `main`; **in progress** — being written now; **TODO** — not
started.

```
edullm-data/                    ← git root
├── pyproject.toml              [project.name = "edullm-data"]                    done
├── src/edullm_data/
│   ├── contracts.py            canonical_json, hashing, naming                   done
│   ├── manifest.py             build + verify manifests                          done
│   ├── s3.py                   S3 protocol + Boto3S3 + FakeS3                     done
│   ├── publish.py              publish() — landing-only writes                   done
│   ├── read.py                 dataset_paths() reader                            done
│   ├── validate.py             Gate A + promote() (writes the generated README)  done
│   ├── readme.py               render_readme() — README generated from dataset.json  done
│   ├── fsck.py                 wu-fsck, Gate B, weekly integrity re-check        done
│   ├── ingest_reservoir.py     HF → S3 ingest, array-sharded (Batch)             done
│   ├── reservoir_ids.py        §9.7 id partition (key-derived, reproducible)     done
│   └── profiles/
│       ├── base.py             Violation / GroupContext / sample_offsets         done
│       ├── registry.py         profile lookup by name                            done
│       ├── pretrain_tokens_v1.py   tokenizer pin, vocab bound, alignment         done
│       ├── eval_results_v1.py      model pin, decode params, failure accounting  done
│       ├── token_order_v1.py       permutation/index-vector checks               done
│       ├── tokenizer_v1.py         tokenizer-family checks                       done
│       └── sft_conversations_v1.py messages[] schema, dedup + leakage report     done
├── infra/                      CloudFormation + policies + Dockerfile + runbooks done (deployed)
├── families/                   the seven family.json files                       done
└── tests/                      786 passing                                       done
```

## Build status (standard §13)

| # | Step | Status |
|---|---|---|
| 1 | git root created, package dirs scaffolded | done |
| 2 | infrastructure: `edullm-landing` / `edullm-data`, bucket policy, lifecycle, versioning | **done — deployed live** |
| 3 | `contracts.py` + `manifest.py` | done |
| 4 | validator (Gate A), Batch-only, profile-driven | done |
| 5 | v1 profiles + tests — now **six**: `pretrain-tokens`, `eval-results`, `token-order`, `sft-conversations`, `tokenizer`, `vendored` (`registry.available()`, 2026-08-01) | done |
| 6 | `publish()` | done |
| 7 | `dataset_paths()` reader | done |
| 8 | seven `family.json` files (`contracts.FAMILIES`) | done |
| 9 | event wiring (EventBridge on landing → Batch queue) | **done — deployed live.** ⚠️ `edullm-landing-manifest-created` is **DISABLED** as of 2026-08-01, so auto-promotion is off; submit the validator job manually or re-enable the rule |
| 10 | S3 Inventory on `edullm-data` | **done — deployed live** (`edullm-data-weekly`, Enabled, Weekly — verified 2026-08-01) |
| 11 | `wu-fsck` (Gate B), weekly, owner **Eric Wu** | **done — code AND schedule.** ~~re-scheduling TODO~~: the live rule is ENABLED at `cron(6 9 ? * MON *)`, i.e. Mondays (verified 2026-08-01). Only its *name*, `edullm-wu-fsck-nightly`, is still wrong |
| 12 | generate the agent skill from the profile registry | partly — `.claude/skills/edullm-datasets/` and `edullm-dataset-design/` exist; whether they are *generated from the registry* rather than hand-written is the open part |

~~**One packaging step remains before the validator runs in-cluster**~~ — **DONE (2026-08-01).** The
validator has been running in-cluster for days and has promoted the ten datasets listed above. The
stated obstacle was doubly dead: this repo is **public**, so a git remote to `pip install` from
always existed, and the ECR path was taken anyway — `edullm-validator:10` runs code **baked into a
digest-pinned image** with no wheel bootstrap at all. `infra/05-validator-jobdef.md` is now history
rather than instructions. The airlock is proven end-to-end (live promotion test).

`wu-fsck` is named for its owner deliberately — an unowned recurring job gets muted after its first
false alarm. Ownership transfers by renaming the job, not by editing a config field.

It runs **weekly**, not nightly. Every fact it re-checks (an object deleted or truncated, a payload
overwritten at the same length, a `depends_on` parent republished or removed, an ECR image expired)
changes only when something mutates a frozen prefix or another dataset's lifecycle — rare, and no more
urgent at 24-hour granularity than at 7-day. The sweep is cheap either way; the scarce resource is the
owner's attention, and nightly spent it seven times over for the same information.
