# Dataset Creation

Start here. This directory holds the dataset standard, the audit that motivated it, and the
per-dataset specifications.

## The standard

**All datasets created from now on follow [`DATASET-STANDARD.md`](DATASET-STANDARD.md).**

| Read this | If you want |
|---|---|
| [`DATASET-STANDARD-DIAGRAMS.md`](DATASET-STANDARD-DIAGRAMS.md) | the fast version — 11 diagrams, start with §1b (the airlock) and §10 (the cheat sheet) |
| [`DATASET-STANDARD.md`](DATASET-STANDARD.md) | the rules: naming, layout, profiles, validation, governance |
| [`s3-dataset-audit-2026-07-28.md`](s3-dataset-audit-2026-07-28.md) | why the standard looks like this — 23 buckets, 2.53 TB, and everything that went wrong |

### The 60-second version

Datasets live at `s3://edullm-data/<family>/<name>/<version>/`, where `<family>` is one of
`pretrain` · `curriculum` · `sft` · `eval` · `probe` · `vendor`.

**You cannot write to `s3://edullm-data`.** Nobody can except one automated validator role. You write to
`s3://edullm-landing`; the validator checks your dataset and copies it across if it passes. This is a
locked door, not a guideline — which is the entire point (see below).

Publishing is one call with four arguments:

```python
publish(source,                            # local path, or an s3:// path already on landing
        dataset_id="eval/mcq-arc",         # <family>/<name>
        purpose="ARC/OpenBookQA/SciQ loglikelihood scores for 23 baseline models, "
                "for the adaptive-inference baseline table",
        profile="eval-results/v1")         # which validator contract applies
```

Everything else — version number, hashes, byte counts, formats, row/token counts, which machine built
it — is derived. License and tokenizer pins are inherited from a `family.json` written once per family.

Three rules worth knowing before you read anything else:

1. **Packed token shards are `.u32le.bin`, never `.npy`.** OLMo-core memmaps from byte 0, so a real
   NumPy header would corrupt both the leading tokens and the token count.
2. **No dates or version words in names.** `version` is its own path segment and is auto-allocated.
3. **If no profile fits your data, add a profile.** That's a small PR — not a reason to reach for
   `experimental/v1`.

## Why this replaced the previous policy

This file used to list nine governance requirements: immutable versioned releases, complete provenance,
machine-readable schemas, deterministic builds, checksummed manifests, dataset cards, and so on. They
were the right goals. **They were also comprehensively ignored** — the audit found zero `_SUCCESS`
sentinels in S3, five of six dataset buckets unversioned, 7,557 files with a lying extension, and eight
metadata files pointing at things that do not exist.

The reason is structural, not cultural: that list described *outcomes* and specified no *mechanism*.
Nothing checked it, so nothing followed it.

The standard keeps every one of those goals and attaches a mechanism to each:

| Old requirement | Now enforced by |
|---|---|
| immutable, versioned releases | create-only writes (`IfNoneMatch:*`) + bucket versioning + deny-delete |
| complete source and transformation provenance | `sources[]` pinned by uri+revision+sha256; `build.executor` auto-captured from Batch/ECS metadata |
| machine-readable schemas and validation rules | the profile registry — 15 typed contracts, each with its own checks |
| deterministic builds where possible | `build{seed, env, reproducibility}` with domain-separated seeds; `reproducibility: bitwise\|logical` declared honestly |
| checksummed artifacts and manifests | per-shard `sha256` + `bytes` in an exhaustive manifest, recomputed by the validator |
| explicit licensing | `license{id, basis}` — an honest `basis: unknown` beats a false `MIT` |
| documented human/synthetic boundaries | `sources[].kind` + `profile` |
| acceptance, rejection, exception logs | `_REJECTED.json` on failure; `_exceptions/` registry; `status_counts{}` per profile |
| dataset cards | generated `README.md` per release, plus `notes` and `limitations[]` |

**Privacy review is deliberately out of scope.** The standard assumes every dataset under it is
non-personal — public corpora, licensed third-party data, synthetic generations, or model outputs. There
is no PII field and no scanner. If that ever stops being true, the standard needs revising *before* such
data is published, because none of its checks would catch it.

## Per-dataset specifications

These describe two specific data efforts. They predate the standard; where they conflict with it on
layout, naming, or publication, the standard wins.

| Dataset | Purpose | Specification |
|---|---|---|
| Pedagogical Tutoring SFT | Multi-turn tutoring conversations paired with explicit pedagogical instructions, plus ordinary instruction data | [`pedagogical-tutoring-sft-dataset.md`](pedagogical-tutoring-sft-dataset.md) |
| Curriculum-Learning Pretraining | One fixed pretraining corpus with reproducible difficulty annotations and curriculum views | [`curriculum-learning-pretraining-dataset.md`](curriculum-learning-pretraining-dataset.md) |

Also here: [`static-curriculum-20b-farmshare-plan.md`](static-curriculum-20b-farmshare-plan.md).

## Status

The standard is **agreed but not yet enforced** — `edullm-landing`, `edullm-data`, and the validator do
not exist yet. Until they do, follow the naming and layout rules by hand for anything new. Build order is
in [`DATASET-STANDARD.md` §13](DATASET-STANDARD.md#13-build-order).

Settled decisions, so they don't get relitigated:

| Decision | Choice |
|---|---|
| Enforcement | airlock — producers write only to `edullm-landing`; a validator role alone can write `edullm-data` |
| Validator identity | reuse `sbsandbox-intern-edullm-batch-workload` (service-assumable only; `iam:CreateRole` is denied by the intern boundary) |
| Validator runtime | **AWS Batch only** in v1 — no Lambda path, no size dispatch |
| Batch execution | reuse job definition `sbsandbox-intern-edullm-cpu-run` with a command override (no `RegisterJobDefinition` needed); every submit must supply `vcpus` + `memory` |
| Decode smoke test | seeded random offsets, ~64 KB/shard, deterministic and recorded |
| Package | `edullm-data`, git-installable, package + IaC in one repo |
| Profiles for v1 | `pretrain-tokens`, `eval-results`, `token-order`, `sft-conversations` |
| New profiles | encouraged — a small PR, and the expected path when nothing fits |
| `experimental/v1` | max 2 live per family, quota not approval |
| Nightly integrity job | `wu-fsck`, owner Eric Wu |
| PII | out of scope — no personal data under this standard |

Training and experiment execution remain outside this directory's scope.

## Source documents

- `P7 PRD.pdf`, especially Implementation 2 on pages 7-10.
- `Copy of P1 Experiment Proposals.pdf`, especially the proposed experiment on pages 3-4.
