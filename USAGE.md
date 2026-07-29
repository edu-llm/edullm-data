# Using eduLLM Datasets

For people (and agents) who create or consume datasets. The authoritative spec is
[`../docs/dataset-creation/DATASET-STANDARD.md`](../docs/dataset-creation/DATASET-STANDARD.md);
this is the practical how-to.

---

## What you install

| You are… | Install | Why |
|---|---|---|
| **Publishing** a dataset | the `edullm-data` package | gives you `publish()`, which builds the manifests, hashes everything, and writes to landing correctly |
| **Reading** a dataset for training | the `edullm-data` package | `dataset_paths()` returns the object URIs **and the correct dtype** (the uint16/uint32 trap is real) |
| Working via an **agent** in this repo | nothing extra | the `edullm-datasets` skill is already in `.claude/skills/` and loads automatically |

Install the package:

```bash
# pinned git install (production) — pin a tag so the publisher and validator agree
uv add "edullm-data @ git+https://github.com/edu-llm/edullm-data@v0.2.0"

# editable (local dev)
python3 -m pip install -e /path/to/edullm-data
python3 -m pytest tests/ -q   # 380 passing
```

> The repo is public at [github.com/edu-llm/edullm-data](https://github.com/edu-llm/edullm-data);
> the `git+https` install needs no auth. For local dev, `pip install -e` the checkout; on AWS
> Batch, use the wheel-from-S3 bootstrap in [`infra/05-validator-jobdef.md`](infra/05-validator-jobdef.md).

---

## Mental model: the airlock

```
you ──write──► s3://edullm-landing ──validator──► s3://edullm-data ──read──► trainers
              (anyone writes,                    (ONLY the validator
               nothing trains here)               role can write;
                                                  everyone reads)
```

You never write `s3://edullm-data`. It is a locked bucket — an IAM Deny, not a policy on
paper. You write to landing; the validator checks your dataset and, if it passes, copies it
into `edullm-data` and writes a catalog entry. If it fails, it drops a `_REJECTED.json` next
to your upload and the bytes expire in 14 days.

---

## Publish a dataset

```python
from edullm_data.publish import publish
from edullm_data.s3 import Boto3S3
import datetime

publish(
    "/scratch/runs/mcq-2026-07-28/artifacts/public",   # local dir, or s3://edullm-landing/<staged>/
    dataset_id="eval/mcq-arc",
    purpose="ARC/OpenBookQA/SciQ loglikelihood scores for 23 baseline models, for the baseline table",
    profile="eval-results/v1",
    s3=Boto3S3.default(),
    created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    group_meta={
        "results": {
            "model": {"id": "qwen/Qwen2.5-0.5B", "revision": "..."},
            "task": "arc",
            "decode": {"temperature": 0.0, "top_p": 1.0, "max_tokens": 8},
            "status_counts": {"ok": 200, "error": 0, "filtered": 0},
        }
    },
)
```

**You type four things** (`source`, `dataset_id`, `purpose`, `profile`) plus the per-group
`group_meta` your profile requires. Everything else — version, hashes, counts, formats, build
provenance, license — is derived or inherited from `families/<family>.json`.

**Tokenizers are per-dataset.** A pretrain/curriculum corpus names the tokenizer it was built
with by passing `tokenizer="tokenizer/<name>"` to `publish()` (publish the tokenizer once with
profile `tokenizer/v1`). The validator derives `vocab_size`/`eos_token_id` from that published
tokenizer and checks every token id against it. There is no single canonical tokenizer, so
don't rely on a family default — name the exact one each corpus used.

Layout of your `source` directory: one subdirectory per **group**, files under it.

```
artifacts/public/
└── results/                 ← group name (its profile is eval-results/v1)
    └── eval-00000.jsonl
```

For token data:

```
artifacts/public/
└── tokens/
    ├── train-00000.u32le.bin      ← NEVER .npy
    └── train-00001.u32le.bin
```

### Naming rules (the user manually reviews names — get them right)

`<family>/<name>` where family ∈ `pretrain curriculum sft eval probe vendor`, and name is
kebab-case, 2–5 words, no dates, no version tokens, no person names, no relative words. State
**what it is + the axis that distinguishes it from siblings**. See the skill or §2 of the spec
for the full good/bad tables. Quick examples: `pretrain/dolma2-150b`, `curriculum/flesch-linear-370m`,
`eval/mcq-arc-openbookqa-sciq`. Not: `pretrain/final-v2`, `eval/results`, anything with a date.

### From an AWS Batch job

If your job already wrote output into landing, pass that prefix and no bytes move:

```python
publish("s3://edullm-landing/eval/mcq-arc/_pending/", dataset_id="eval/mcq-arc", ...)
```

### Describe the data mix — the README is generated from this

Every published dataset carries a **generated `README.md`** at its prefix
(`s3://edullm-data/<id>/<version>/README.md`), rendered from `dataset.json` by the validator
during promotion — you never hand-write or upload it, and it can't drift from the manifest. To
make it say something useful, pass the descriptive fields to `publish()` (all optional):

```python
publish(
    tokens_dir, dataset_id="pretrain/olmo-mix-1124-31b", purpose="...",
    profile="pretrain-tokens/v1", tokenizer="tokenizer/dolma2-bpe",
    s3=Boto3S3.default(), created_at=...,
    about="What this corpus is and how it was produced (a curated paragraph).",
    sources=[                                # → the 'Data mix / sources' table
        {"name": "DCLM-Baseline", "tokens": 3_700_000_000_000, "license": "CC-BY-4.0",
         "scope": "upstream-full-collection",         # prints an honesty caveat: these are
         "uri": "https://huggingface.co/datasets/allenai/olmo-mix-1124"},  # upstream figures,
        {"name": "pes2o", "tokens": 58_600_000_000, "license": "ODC-By-1.0",  # not this subset's
         "scope": "upstream-full-collection"},                                # measured mix
    ],
    license={"id": "ODC-By-1.0", "basis": "declared"},   # overrides the family's honest "unknown"
    notes="Free-text caveat.",
    limitations=[{"kind": "contamination", "benchmark": "gsm8k", "overlap_rate": 0.003}],
)
```

- `about` is the one **curated** prose block; everything else in the README (contents,
  tokenizer, splits, inventory, how-to-read) is **derived** from `dataset.json`.
- A section is **omitted** when its data is absent — an empty `sources=[]` renders no mix table,
  never a fake one.
- `scope: "upstream-full-collection"` marks a source as describing the upstream collection rather
  than a measured breakdown of *this* dataset, and the README prints a caveat saying so — use it
  whenever your corpus is a subset/derivation and you didn't separately measure per-source shares.
- None of these are validator-required fields — they only feed the README, so adding them never
  changes what Gate A accepts.

**Already-promoted datasets get a README too.** The README is a control file, so it (and the
descriptive fields) can be backfilled into an existing frozen dataset in place — the validator
role re-writes `dataset.json` with the added descriptive keys and writes `README.md`, without
touching any payload byte, manifest hash, or the inventory. See the HANDOFF for the backfill
driver used on the migrated olmo-mix corpus.

---

## Read a dataset

```python
from edullm_data.read import dataset_paths, resolve_latest
from edullm_data.s3 import Boto3S3

s3 = Boto3S3.default()
version = resolve_latest("pretrain/dolma2-150b", s3=s3)            # -> "v3"
r = dataset_paths("pretrain/dolma2-150b", version, split="train", s3=s3)

# r.paths : list of s3:// URIs
# r.dtype : "uint32"  ← feed to your loader; do not let it default to uint16
# r.rows  : declared token/row count for the split
```

`dataset_paths` refuses a dataset with no `_VALIDATED.json` — unvalidated data is not readable.

---

## Find what exists

- **By catalog:** list `s3://edullm-data/_catalog/` — one JSON per published dataset/version.
- **By family:** list `s3://edullm-data/<family>/`.
- **Programmatically:** `resolve_latest(dataset_id, s3=...)`.

---

## What gets rejected

The validator recomputes every claim. It will reject, among others: a manifest hash that
doesn't match the bytes; a missing or unlisted shard; a size mismatch; `count × dtype != bytes`;
a `.npy` that's actually headerless raw; duplicate shard digests; an inventory that doesn't
match reality; a shard shared with a `depends_on` parent (copy vs reference); all-zeros /
all-EOS / wrong-dtype token shards; an eval file where every row errored; a non-permutation
curriculum ordering; train/heldout leakage. Full list in §7 of the spec.

If you hit a rejection, read the `_REJECTED.json` the validator wrote next to your upload — each
violation names the concrete failure.

---

## Adding a new profile

If no profile fits your data, **write one** — that is the intended path, not `experimental/v1`.
See [`CONTRIBUTING.md`](CONTRIBUTING.md): a registry entry, a schema fragment, check functions
that *recompute* something, and two fixtures (one passing, one deliberately broken).

---

## Operational facts

- One region: **us-east-1**. Keep compute there.
- `edullm-data` is versioned + deny-delete; deletions need the break-glass role.
- Weekly S3 Inventory + nightly `wu-fsck` (owner: **Eric Wu**) catch post-publish decay.
- **No PII in scope.** If you are handling personal data, stop — this standard doesn't cover it.
