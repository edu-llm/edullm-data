---
name: edullm-datasets
description: MUST USE for anything involving eduLLM datasets on S3 — creating, publishing, naming, reading, or discovering a training/eval/curriculum/probe dataset, or auditing what exists. Covers where datasets live (s3://edullm-data), how they are named, how to publish one through the airlock validator, how to read one into training, and what the validator will reject. Trigger whenever a task mentions publishing a dataset, a token corpus, an eval set, a curriculum ordering, edullm-landing, edullm-data, dataset.json, a manifest, or "where is the dataset".
---

# eduLLM Datasets

The single way datasets are created, stored, and read in this project. The full spec is
`docs/dataset-creation/DATASET-STANDARD.md`; the diagrams are `DATASET-STANDARD-DIAGRAMS.md`.
This skill is the operational summary — follow it, don't re-derive it.

## The one thing to understand first

There are two buckets:

- **`s3://edullm-landing`** — anyone may write here. Nothing trains from here. 14-day expiry.
- **`s3://edullm-data`** — everyone reads; **only the validator role can write.** This is a
  locked bucket (an IAM Deny), not a convention. You cannot `aws s3 cp` into it, and neither
  can anyone else. Datasets get here only by passing the validator.

So publishing is: write to landing → the validator checks it → it copies the passing dataset
into `edullm-data` and writes a catalog entry. You never write `edullm-data` directly.

## Install (what a dataset creator needs)

```bash
# 1. the package (git-installable; pin a tag)
uv add "edullm-data @ git+ssh://git@github.com/<org>/<edullm-data-repo>@v0.1.0"
#    editable, for local dev:
python3 -m pip install -e /path/to/edullm-data

# 2. this skill — already in .claude/skills/edullm-datasets/ for agents in this repo
```

The package gives you `publish()` (produce a dataset) and `dataset_paths()` (read one). You
do **not** need it to read if you resolve the manifest yourself, but it returns the correct
dtype for free — use it.

## Publishing a dataset — four hand-typed arguments

```python
from edullm_data.publish import publish
from edullm_data.s3 import Boto3S3

publish(
    source,                       # local dir OR "s3://edullm-landing/<staged-prefix>/"
    dataset_id="eval/mcq-arc",    # <family>/<name> — see naming rules below
    purpose="ARC/OpenBookQA/SciQ loglikelihood scores for 23 baseline models, for the baseline table",
    profile="eval-results/v1",    # or a dict {group: profile} for multi-group datasets
    s3=Boto3S3.default(),
    created_at="<ISO-8601 UTC>",
    group_meta={...},             # profile-specific fields per group (see profiles below)
)
```

Everything else is derived: version (auto-allocated), object hashes, byte/row/token counts,
formats, build provenance (from the environment), and `license`/`tokenizer`/`sources`
(inherited from `families/<family>.json`, written once per family).

From an AWS Batch job that already wrote output to landing, pass the `s3://` prefix as
`source` — no bytes leave S3.

## Naming — the user will manually verify name quality, so get it right

`s3://edullm-data/<family>/<name>/<version>/`

- **`<family>`** is a fixed enum: `pretrain` · `curriculum` · `sft` · `eval` · `probe` · `vendor`
- **`<name>`** is kebab-case, 2–5 words, stating **what the data is + the one axis that
  distinguishes it from its siblings**. No dates, no version tokens, no person names, no
  relative words.
- **`<version>`** is `v1`, `v2`, … auto-allocated. Never put it in the name.

**Good names** (copy this style):

| Name | Why |
|---|---|
| `pretrain/dolma2-150b` | corpus + token budget (the distinguishing axis) |
| `pretrain/olmo-mix-1124-30b` | upstream release code + budget |
| `pretrain/fineweb-edu-10b` | corpus + budget |
| `curriculum/flesch-linear-370m` | difficulty signal + schedule + target model |
| `curriculum/zlib-strict-370m` | same axes, different values — siblings read clearly |
| `sft/pedagogical-tutoring-100students` | task + defining scale |
| `eval/mcq-arc-openbookqa-sciq` | task type + exactly which benchmarks |
| `eval/judge-blinded-5x5` | protocol + design shape |
| `probe/multihop-wikidata-4hop` | task + source + depth |
| `vendor/dclm-hero-run-fasttext` | upstream name preserved verbatim |

**Reject these** (the validator rejects them mechanically, but don't propose them):
`pretrain/datamix1-jul22` (date), `pretrain/final-v2` (version tokens), `eval/results`
(says nothing), `curriculum/experiment-3` (meaningless ordinal), `sft/good-data` (relative),
`pretrain/dolma2_150B` (snake_case + caps), `eval/mcq-v2-fixed-final` (three version tokens).

Test: *if two people independently built this, would they pick the same name? Could you tell
it from its five nearest siblings without opening it?*

## Purpose — one line: what it is, what consumes it, what it decides

Good: `"150B-token Dolma2 mix for 370M ladder pretraining at 1.25xC"` ·
`"Blinded judge verdicts, 5 judges x 5 prompt variants x 3 replicates, to measure judge reliability"`

Reject: `"training data"`, `"the dataset"`, `"TODO"`, `"experiments"`, `"see README"` — all
either say nothing or will never be filled in. Shape: `<what it is> for <what consumes it> to
<what it decides>`.

## Choosing a profile

A profile is the contract for one payload **group** (a dataset can have several — e.g. token
shards + a sidecar table — each with its own profile). Pick by what the bytes are:

| Payload | Profile | Required in `group_meta` |
|---|---|---|
| packed pre-tokenized corpus (`.u32le.bin`) | `pretrain-tokens/v1` | `tokenizer{repo_id, revision, fingerprint_sha256, vocab_size, eos_token_id}` |
| raw untokenized documents | `text-corpus/v1` | record schema, `text` field |
| instruction / conversation data | `sft-conversations/v1` | `record_schema` w/ `messages[]`, `partitions` incl. heldout, `dedup`, `leakage` |
| benchmark inputs | `eval-items/v1` | stable per-item id |
| model outputs / scores | `eval-results/v1` | `model{id, revision}`, `task`, `decode{...}`, `status_counts{}` |
| per-record derived metrics | `annotations/v1` | `parent_group`, row-i == parent row-i |
| index vectors (views, curricula) | `token-order/v1` | `depends_on[]`, ordering kind |
| parallel float arrays (loss weights) | `weights-sidecar/v1` | `parent_group` |
| any tabular w/ column schema | `tabular/v1` | column schema |
| image/video/audio | `media/v1` | decode facts; label index if supervised |
| telemetry / timeseries | `metrics-timeseries/v1` | timestamp field |
| verbatim third-party tree | `vendored/v1` | `upstream{}`, `vendor_root` |
| transfer archives | `distribution-artifact/v1` | `packages{}` + part checksums |
| doesn't fit / shipping today | `experimental/v1` | `exception{reason, approver, expires_at}` — max 2 live per family |

**If nothing fits, write a profile** — that's the expected path, not `experimental`. A profile
is a small PR (see `edullm-data/CONTRIBUTING.md`): a registry entry, a schema fragment, check
functions that RECOMPUTE something, and two fixtures. Only reach for `experimental/v1` when you
must ship today and don't yet know the shape.

## Format rules that bite

- **Packed uint32 tokens are `.u32le.bin`, NEVER `.npy`.** OLMo-core memmaps from byte 0 and
  derives the token count from raw file size; a real `.npy` header corrupts both the tokens and
  the count. Declare `dtype` explicitly (uint32, not the uint16 default) — it is read, never
  inferred.
- **Shard names are `<split>-<NNNNN>.<ext>`, no `-of-N`.** The total is unknowable at write
  time; completeness is proven by the manifest, not the filename.
- **Every file's extension must match its declared format.** The validator sniffs magic bytes.

## What the validator will reject (so you don't ship it)

Gate A recomputes, it doesn't trust. It rejects: a manifest hash that doesn't match the bytes;
a shard listed but missing, or present but unlisted; a HEAD size disagreeing with the manifest;
`count × dtype_size != bytes` (truncation / wrong dtype); a `.npy` that's really headerless raw;
duplicate shard digests; an inventory count that doesn't match reality; a shard sha256 shared
with a `depends_on` parent (copy instead of reference); an unknown profile. Per profile it also
reads bytes: all-zeros / all-EOS / wrong-endianness token shards; an eval-results file where
every row errored (`n_ok == 0`); a degenerate curriculum ordering that isn't a permutation;
train/heldout leakage in SFT.

## Reading a published dataset

```python
from edullm_data.read import dataset_paths, resolve_latest
from edullm_data.s3 import Boto3S3

s3 = Boto3S3.default()
ver = resolve_latest("pretrain/dolma2-150b", s3=s3)         # e.g. "v3"
r = dataset_paths("pretrain/dolma2-150b", ver, split="train", s3=s3)
# r.paths  -> ["s3://edullm-data/pretrain/dolma2-150b/v3/tokens/train-00000.u32le.bin", ...]
# r.dtype  -> "uint32"   (feed this to the loader; do NOT let it default to uint16)
# r.rows   -> declared row/token count for the split
```

It refuses a prefix with no `_VALIDATED.json` — unvalidated data is not readable.

## Discovering what exists

List `s3://edullm-data/_catalog/` — one immutable JSON per published `<dataset_id>/<version>`.
That is the discovery surface; there is no single mutable index. To find datasets by family,
list `s3://edullm-data/<family>/`.

## Governance you can rely on

`edullm-data` is versioned, deny-delete except break-glass, AES256, one region (us-east-1),
weekly S3 Inventory. Nightly `wu-fsck` (owner: Eric Wu) re-checks that published datasets still
resolve (sources, parents, object presence) — the failures that only appear after publish.
There is no PII in scope; if you are handling personal data, this standard does not cover it —
stop and escalate.
