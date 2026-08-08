# Curriculum-Learning Pretraining Dataset

## Objective

Create one fixed, reproducible pretraining corpus that can be viewed and ordered using multiple definitions of sample difficulty without changing the underlying data composition.

## Base corpus requirements

The corpus must be:

- cleaned and normalized using one versioned pipeline;
- deduplicated within the training data and against held-out data;
- assigned stable document and sample identifiers;
- tokenized with one frozen tokenizer version;
- accompanied by per-sample token counts;
- traceable to source and license metadata;
- partitioned into immutable training and validation sets; and
- published through checksummed manifests.

All curriculum views must reference the same accepted training samples. Difficulty ordering must not silently add, remove, or duplicate content unless repetition is an explicitly versioned dataset policy.

## Sampling unit

Define one consistent unit to which difficulty is assigned, such as a document, paragraph, or fixed-length token sequence. The chosen unit must remain stable across every difficulty metric and curriculum view.

Each sample should contain at least:

```yaml
sample_id: string
document_id: string
source_id: string
text: string
token_count: integer
split: train | validation
difficulty:
  learnability: number | null
  compression_ratio: number | null
  flesch_reading_ease: number | null
  mtld: number | null
quality_flags: []
pipeline_version: string
tokenizer_fingerprint: string
```

## Difficulty annotations

Compute and store the following difficulty signals independently for every eligible training sample.

### Learnability

Measure learnability from the change in per-sample loss between defined early and late checkpoints of a scoring proxy. The scoring procedure must specify:

- the proxy dataset and its separation from validation data;
- checkpoint selection rules;
- the exact loss-delta equation;
- whether higher values mean easier or harder samples;
- treatment of truncated or invalid samples; and
- the scoring, tokenizer, and pipeline fingerprints needed to reproduce each score.

Raw early loss, raw late loss, and the derived delta should all be retained.

### Compression ratio

Calculate the compressed size relative to the original encoded size using a fixed compression algorithm and fixed parameters. Record the algorithm and implementation version.

### Flesch reading ease

Calculate readability with a fixed sentence, word, and syllable segmentation implementation. Record edge cases for non-English, code-heavy, mathematical, or malformed samples.

### Measure of Textual Lexical Diversity

Calculate MTLD using a fixed tokenizer and implementation. Establish a minimum eligible sample length and record how short or undefined samples are handled.

## Score normalization and ordering

For each difficulty metric:

1. Define whether larger scores mean easier or harder data.
2. Define missing-value and outlier handling.
3. Preserve both raw and normalized values.
4. Use deterministic tie-breaking based on stable sample IDs.
5. Publish distribution summaries and correlations between metrics.
6. Version every scoring implementation and configuration.

Normalization parameters must be fitted only on the training split and then frozen.

## Required dataset views

Publish manifests or deterministic samplers for the following views without copying the underlying text unnecessarily:

### Random control

A seeded random permutation of the complete training corpus.

### Strict easy-to-hard

A deterministic ordering from easiest to hardest under each difficulty metric.

### Linear pacing

A sequence of sampling manifests that gradually increases the proportion of harder samples while retaining a mixture of easier material. The exact bucket boundaries and proportions must be explicit and versioned.

### Curriculum warmup

A curriculum-ordered view for the first half of the dataset budget followed by a seeded random mixture. The boundary and transition behavior must be defined in the manifest.

Each non-control difficulty metric therefore requires three curriculum views: strict easy-to-hard, linear pacing, and curriculum warmup. The random control should be represented once per seed, not redundantly regenerated for every metric.

## Validation and reproducibility checks

Before publication, verify that:

- all views contain the intended sample population and token budget;
- sample text is identical across views;
- only ordering or sampling weights differ;
- manifests reproduce the same order from the same seed;
- no training sample appears in the validation set;
- difficulty values are finite or carry an explicit missing-value code;
- score direction agrees with the documented easy-to-hard interpretation;
- metric distributions are inspected for degenerate values and source artifacts;
- random controls have no accidental difficulty ordering; and
- every artifact is linked to code, configuration, and input checksums.

## Required deliverables

- Immutable cleaned training corpus.
- Immutable held-out validation corpus.
- Dataset and tokenizer manifests.
- Per-sample raw and normalized difficulty table.
- Proxy-loss table containing early loss, late loss, and learnability delta.
- Seeded random-control manifests.
- Strict, linear, and curriculum-warmup manifests for each difficulty metric.
- Dataset statistics and metric-correlation report.
- Deduplication and leakage report.
- Dataset card covering provenance, licensing, limitations, and known biases.
- Reproduction instructions for rebuilding every score and view.

## Decisions still required

- Corpus sources, licensing policy, and target corpus size.
- Sampling unit and context-boundary policy.
- Tokenizer selection.
- Cleaning, quality filtering, and deduplication thresholds.
- Proxy dataset and checkpoint-selection procedure.
- Exact definitions and implementations of all four difficulty metrics.
- Score normalization and missing-value policies.
- Linear pacing curve, bucket boundaries, and sampling proportions.
- Number of random seeds for published control manifests.

## Source

`Copy of P1 Experiment Proposals.pdf`, especially the proposed experiment on pages 3-4.
