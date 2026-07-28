# edullm-data

Publisher, validator, and reader for the eduLLM dataset standard. The standard itself — naming,
manifest shape, the airlock, profiles, validation gates — lives at
[`../docs/dataset-creation/DATASET-STANDARD.md`](../docs/dataset-creation/DATASET-STANDARD.md). This
package is code that implements that document; it is not a second source of truth. If the two disagree,
the standard wins and this package has a bug.

## Install

Not yet published — no tag exists. Once `v0.1.0` is tagged (placeholder org/repo below, fill in on
first release):

```bash
uv add "edullm-data @ git+ssh://git@github.com/<org>/<repo>@v0.1.0"
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

## The one thing to know

**Producers write ONLY to `s3://edullm-landing`.** The validator role
(`sbsandbox-intern-edullm-batch-workload`, service-assumable only — no human or intern session can
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
├── pyproject.toml              [project.name = "edullm-data"]                    TODO
├── src/edullm_data/
│   ├── contracts.py            canonical_json, hashing                          in progress
│   ├── manifest.py             build + verify manifests                         in progress
│   ├── publish.py              publish() — landing-only writes                  TODO
│   ├── read.py                 dataset_paths() reader                           TODO
│   ├── validate.py             Gate A (runs on AWS Batch as the validator)      TODO
│   ├── fsck.py                 wu-fsck, Gate B, nightly integrity re-check      TODO
│   └── profiles/
│       ├── registry.py         profile lookup by name+version                  TODO
│       ├── pretrain_tokens_v1.py   tokenizer pin, vocab bound, alignment       TODO
│       ├── eval_results_v1.py      model pin, decode params, failure accounting TODO
│       ├── token_order_v1.py       permutation/index-vector checks            TODO
│       └── sft_conversations_v1.py messages[] schema, dedup + leakage report   TODO
├── infra/                      CloudFormation for buckets, IAM, event wiring    TODO
├── families/                   the six family.json files                       TODO
└── tests/fixtures/             one passing + one broken fixture per profile     TODO
```

## Build status (standard §13)

| # | Step | Status |
|---|---|---|
| 1 | git root created, package dirs scaffolded | done |
| 2 | infrastructure: `edullm-landing` / `edullm-data`, bucket policy, lifecycle, versioning | todo |
| 3 | `contracts.py` + `manifest.py` | in progress |
| 4 | validator (Gate A), Batch-only, profile-driven | todo |
| 5 | four v1 profiles + fixtures (`pretrain-tokens`, `eval-results`, `token-order`, `sft-conversations`) | todo |
| 6 | `publish()` | todo |
| 7 | `dataset_paths()` reader | todo |
| 8 | six `family.json` files | todo |
| 9 | event wiring (EventBridge on landing → Batch queue) | todo |
| 10 | S3 Inventory on `edullm-data` | todo |
| 11 | `wu-fsck` (Gate B), nightly, owner **Eric Wu** | todo |
| 12 | generate the agent skill from the profile registry | todo |

`wu-fsck` is named for its owner deliberately — an unowned nightly job gets muted after its first false
alarm. Ownership transfers by renaming the job, not by editing a config field.
