# Static Curriculum Annotations for the Live 20B FarmShare Run

## Summary

Extend the running corpus build with compression ratio, Flesch reading ease, and MTLD only. Do not modify the active run or add any GPU/learnability work.

Choose scheduling option 2: launch a separate one-CPU watcher, then have it submit one persistent annotation array after the validated 100M tier commit appears. Start at two workers and adapt up to 16.

At the final planning inspection, 2026-07-22 15:29 PDT:

- Active run: `/scratch/users/ericrcwu/agent-runs/week1-corpus-20260722`
- Eight workers plus one coordinator were running.
- Allocated resources: 148 CPUs and 520 GiB.
- Progress: `100m/reduce`, 235/256 tasks; no packed or tier commits yet.
- Current jobs expire at 01:06 PDT if they do not complete or requeue.
- Inspection was read-only; nothing was edited, submitted, transferred, or cancelled.

## Observed capacity and scheduling decision

Observed FarmShare limits:

- Normal QoS: 512 CPUs, 128 running jobs, 1,024 submitted jobs per user.
- Normal partition maximum: two days.
- Maximum memory: 4,000 MB per requested CPU.
- Roughly 200 usable idle CPUs were observed outside `wheat-01`; availability is not reserved.
- Current fair-share was low, so queue delay remains uncertain.
- Scratch is one shared TrueNAS filesystem with 58 TB cluster-wide free space; available capacity does not imply low I/O latency.

The proposed eight-task allocation is valid:

```text
--cpus-per-task=8
--mem=32000M
--time=12:00:00
--requeue
--signal=B:USR1@120
--export=NONE
--exclude=wheat-01
```

Use `32000M`, not `32G`: `32G` exceeds the 4,000 MB/CPU limit. Eight tasks request 64 CPUs and 256,000 MB; existing plus annotation resources remain below QoS limits.

Final topology uses `--array=0-15%2`: sixteen submitted slots, initially only two active. Pending elements consume no allocation and permit scaling to 4, 8, or 16 without another worker submission.

### Scheduling comparison

All timing values below are estimates until the 100M benchmark replaces them.

| Option | Queue delay | Idle cost before 100M | Throughput estimate | Base impact | Combined completion estimate |
|---|---:|---:|---:|---:|---:|
| Persistent array now | 0–60 min | `%2`: 16 CPU/64 GB per hour; all eight: 64 CPU/256 GB per hour | 4–16M tok/s at eight | Unbounded before a baseline exists | Base finish + 0.5–2 h |
| Watcher then array | Watcher 0–15 min; 2–4 workers 0–30 min | 1 CPU/2.4 GB per hour | Same | Controlled below 5% | Base finish + 0.5–2.5 h |
| Reuse existing workers | None | None | Uncertain | Estimated 5–30%+ slowdown | Potentially slower overall |

Reject option 3. The active workers will need their nominal headroom for later stages, do not contain annotation code, and cannot be reused without altering the running environment.

## Implementation and artifact contracts

Implement the extension in a new run root:

```text
/scratch/users/ericrcwu/agent-runs/week1-static-curriculum-20260722/
  request.json
  input/
    base-run.json
    tokenizer/
  runtime/
  jobs/
  logs/
  queue/
    tasks/<task-id>.json
    pending/<task-id>.json
    claimed/<task-id>.<attempt>.<claim-id>.json
    dead/<task-id>.<attempt>.json
  control/
    desired-workers.json
    PAUSE
    STOP
  staging/
    sidecars/<object-sha>/.partial.<job>.<attempt>.parquet
  sidecars/
    <object-sha>/<annotation-config-id>.parquet
  commits/
    objects/<object-sha>.json
  monitoring/
    base-windows.parquet
    scratch-windows.parquet
    annotation-windows.parquet
  published/
    population.parquet
    orders/*.uint32
    views/*.json
    reports/
    ARTIFACT_SHA256SUMS
    FINAL.COMMIT.json
```

The active corpus root remains read-only.

### Interfaces

Add these commands to the corpus package:

- `static-curriculum-watch BASE_RUN ANNOTATION_RUN CONFIG`
- `static-curriculum-worker BASE_RUN ANNOTATION_RUN CONFIG`
- `static-curriculum-finalize BASE_RUN ANNOTATION_RUN CONFIG`
- `static-curriculum-status ANNOTATION_RUN`

An annotation task contains:

```text
base_config_id
annotation_config_id
producer_commit_key
object_key
object_sha256
byte_size
token_count
first_block
block_count
tokenizer_fingerprint
sequence_length
```

An annotation commit additionally contains:

```text
sidecar_path
sidecar_sha256
row_count
metric null/status counts
code and dependency fingerprints
Slurm job/array IDs
attempt and restart count
```

### Stable block ID

For block index `i` within an immutable packed object:

```text
SHA256(
  "packed-block/v1\0"
  || object_sha256_bytes
  || uint64_le(i)
  || uint32_le(4096)
  || tokenizer_fingerprint_bytes
)
```

Store the full 32-byte digest. Do not include tier, path, global position, or manifest ID; the ID must survive reuse in nested tiers.

### Sidecar schema

Each Parquet row contains:

```text
block_id: fixed_size_binary[32]
object_sha256: fixed_size_binary[32]
block_index: uint32
global_block_ordinal: uint64
category: dictionary<string>
token_count: uint16               # always 4096
decoded_utf8_bytes: uint32
compressed_bytes: uint32
compression_ratio: float64?
flesch_reading_ease: float64?
flesch_word_count: uint32
flesch_sentence_count: uint32
flesch_syllable_count: uint32
mtld: float64?
mtld_word_count: uint32
compression_status: string
flesch_status: string
mtld_status: string
quality_flags: list<string>
annotation_config_id: string
tokenizer_fingerprint: string
```

Use Zstandard-compressed Parquet with 8,192-row groups and stable block-index order.

## Producer, watcher, and worker lifecycle

### Producer

The existing pipeline remains completely unchanged.

Packed objects are raw little-endian `uint32` despite their `.npy` suffix. A mixed-pack task publishes:

```text
packed/objects/<sha256>.npy
staging/<tier>/mixed_pack/<task-id>/commit.json
```

A tier publishes:

```text
views/<tier>/manifest.json
commits/<tier>.json
```

### Watcher

1. Poll only metadata; never scan packed objects as a readiness signal.
2. Wait for `commits/100m.json`.
3. Parse the commit and referenced manifest twice, five seconds apart, requiring identical bytes. This compensates for the producer's missing `fsync`/rename durability.
4. Validate the manifest ID, object sizes, block counts and paths.
5. Enqueue the 100M object and submit the worker array once.
6. Thereafter, discover new objects only through valid mixed-pack task commits.
7. Retry malformed or changing producer commits; never enqueue them.
8. Ignore all `.partial` and uncommitted files.
9. When `commits/20b.json` exists, reconcile its manifest against annotation commits.
10. Submit finalization only after every referenced object has a valid sidecar commit.
11. Write `STOP`; running workers exit and pending annotation elements may be cancelled. Never cancel or alter base jobs.

Later object task commits may be annotated before their tier manifest exists. Objects not ultimately referenced by the 20B manifest are reported as extras and excluded from publication.

### Worker

1. Atomically rename one pending marker into a unique claim.
2. Stream the object once from shared scratch into `$SLURM_TMPDIR`, capped initially at 100 MB/s, while calculating SHA-256.
3. Reject the task if size or SHA-256 differs from its producer contract.
4. Split the verified local object into eight contiguous block ranges.
5. Decode each block once and compute all three metrics from the same decoded text.
6. Merge worker results in block-index order.
7. Write the Parquet sidecar to an adjacent `.partial` file.
8. Close, `fsync`, reopen, validate schema/row count, and calculate its SHA-256.
9. Install it using an atomic same-filesystem link/rename that cannot overwrite an existing sidecar.
10. Write and `fsync` canonical commit JSON, then install the commit using `O_CREAT|O_EXCL`.
11. If a commit already exists, validate its referenced sidecar and exit successfully only if identities match.
12. Delete node-local data after commit. Resume must never depend on node-local state.

Claims receive a 30-minute lease and heartbeat every 60 seconds. Maximum attempts: five; the fifth failure enters `dead/` and pauses the entire annotation run.

### Requeue behavior

The worker and watcher install `USR1` handlers. On signal they:

- stop claiming new work;
- abandon any uncommitted partial result;
- atomically return the claim to `pending`;
- explicitly requeue the specific Slurm job or array element;
- exit without creating a commit.

`--requeue` alone is not considered sufficient.

## Metric definitions

Decode with the pinned Dolma2 tokenizer using:

```text
skip_special_tokens=false
clean_up_tokenization_spaces=false
```

Split at EOS token ID `100257`, decode segments independently, and join them with `"\n\n"`. Remove PAD tokens. An invalid token ID makes all metrics null with `INVALID_TOKEN_ID`.

### Compression ratio

```text
encoded = decoded_text.encode("utf-8")
compressed = zlib.compress(encoded, level=9)
compression_ratio = len(compressed) / len(encoded)
```

Record Python and zlib runtime versions. Lower values are easier. Empty decoded text produces null with `EMPTY_DECODE`.

### Flesch reading ease

Words match:

```regex
[A-Za-z]+(?:['’][A-Za-z]+)?
```

Sentences are maximal runs matching `[.!?]+`; when words exist but no terminator exists, use one sentence.

Syllables:

1. Lowercase the ASCII word.
2. Count maximal `[aeiouy]+` groups.
3. Subtract one for terminal silent `e` when the count exceeds one, except consonant+`le`.
4. Clamp to at least one.

Formula:

```text
206.835
- 1.015 × words / sentences
- 84.6 × syllables / words
```

Higher values are easier. Zero words produces null with `NO_ASCII_WORDS`. Code-heavy or non-English text remains scored but receives quality flags.

### MTLD

Use the same case-folded word sequence as Flesch.

- Minimum length: 50 words.
- TTR threshold: 0.72.
- Complete a factor whenever running TTR is at most 0.72.
- Final partial factor: `(1 - final_ttr) / (1 - 0.72)`.
- Compute forward and reversed MTLD and average them.

Higher MTLD is harder. Fewer than 50 words produces null with `TOO_SHORT`. Zero factors or non-finite output produces null with `UNDEFINED`.

Any non-finite metric is replaced by null with `NONFINITE`. Missing values sort after every valid value.

## Slurm topology and adaptive control

### Watcher

```text
partition=normal
qos=normal
account=operator
cpus=1
mem=2400M
time=24:00:00
requeue=true
signal=B:USR1@120
export=NONE
exclude=wheat-01
```

### Annotation array

```text
array=0-15%2
partition=normal
qos=normal
account=operator
cpus-per-task=8
mem=32000M
time=12:00:00
requeue=true
signal=B:USR1@120
export=NONE
exclude=wheat-01
```

No Slurm dependency can represent a filesystem commit. The watcher's validated file barrier is authoritative.

### Finalizer

```text
partition=normal
qos=normal
account=operator
cpus=16
mem=64000M
time=02:00:00
export=NONE
exclude=wheat-01
```

The watcher submits it only after the final reconciliation barrier.

### Automatic concurrency

Start at two workers. The watcher writes the desired concurrency to `control/desired-workers.json`; array elements above that ordinal idle without reading corpus data.

Base throughput is measured from producer task commit times, normalized as follows:

- tokenization: declared compressed input bytes;
- reduction/category packing: committed input bytes;
- mixed packing: committed output blocks.

Compare only the same `(tier, stage)` and reset the baseline at every stage transition. Require at least three comparable completions; otherwise remain at two.

Stage caps:

- `tokenize`: may scale to 16.
- `reduce` or `category_pack`: maximum two.
- `mixed_pack`: pause annotations.
- Unknown/stale producer state: pause.

Scaling sequence: 2 → 4 → 8 → 16, no more than once every 15 minutes.

Scale up only when:

- backlog is at least twice the proposed concurrency;
- three matched five-minute windows show less than 5% base degradation;
- scratch median read latency is below 1.5× baseline;
- scratch p95 is below 2× baseline;
- no retry, checksum, Parquet or dead-letter error occurred.

Immediately halve concurrency after one validated window with base degradation of at least 5%. Pause after:

- two consecutive ≥5% windows;
- one ≥10% window;
- median scratch latency ≥1.5× baseline;
- p95 scratch latency ≥2× baseline;
- any checksum or Parquet validation failure;
- any base or annotation dead-letter signal.

Resume only after three healthy windows, always at two workers.

## 100M benchmark and estimates

The 100M tier is one 400,015,360-byte object containing 24,415 blocks. Only one array element can claim it; that element must use all eight CPUs internally.

Record:

- end-to-end elapsed time;
- blocks and tokens per second;
- shared-copy throughput;
- CPU utilization and MaxRSS;
- Parquet bytes and row count;
- null/status distribution;
- base throughput and scratch latency before and during annotation.

Gates:

- Exactly 24,415 rows and 100,003,840 tokens.
- Input and output checksums pass.
- No duplicate block IDs.
- Base slowdown remains below 5%.
- Compression valid for at least 99.9% of blocks.
- Flesch and MTLD each valid for at least 70%; otherwise stop for metric inspection.
- Completion ≤10 minutes: normal go.
- Completion 10–30 minutes: allow only if the measured eight-worker projection fits before the desired deadline.
- Completion >30 minutes: do not run concurrently; pause until the base build completes.

Estimated per eight-CPU task throughput is 0.5–2.0M tokens/s:

| Workers | CPU / memory | Estimated aggregate | Estimated isolated 20B time |
|---:|---:|---:|---:|
| 2 | 16 CPU / 64 GB | 1–4M tok/s | 1.4–5.6 h |
| 4 | 32 CPU / 128 GB | 2–8M tok/s | 0.7–2.8 h |
| 8 | 64 CPU / 256 GB | 4–16M tok/s | 0.35–1.4 h |
| 16 | 128 CPU / 512 GB | 8–32M tok/s | 0.17–0.7 h |

These are estimates, not measured results.

## Final reconciliation and ten views

After validating `commits/20b.json` and its manifest:

- Require exactly 4,882,813 blocks and 20,000,002,048 tokens.
- Require one valid annotation row per manifest block.
- Require unique block IDs.
- Require all object, tokenizer, sequence-length and metric fingerprints to agree.
- Build `population.parquet` in manifest object order and within-object block order.
- Compute `population_root = SHA256(concatenated ordered block_id bytes)`.
- Fit empirical percentile ranks using the final training population only, with `0 = easiest` and `1 = hardest`.
- Exclude sidecars for objects outside the final manifest.

Represent each view as a small JSON header plus a raw little-endian `uint32` vector of population ordinals. Each vector is approximately 19.5 MB; ten total approximately 195 MB.

Views:

1. Random control: sort by `SHA256("random/v1" || uint64_le(6198) || block_id)`.
2. Compression strict: ascending ratio, missing last, block ID tie-break.
3. Flesch strict: descending score, missing last, block ID tie-break.
4. MTLD strict: ascending score, missing last, block ID tie-break.
5. Linear pacing for compression.
6. Linear pacing for Flesch.
7. Linear pacing for MTLD.
8. Curriculum warmup for compression.
9. Curriculum warmup for Flesch.
10. Curriculum warmup for MTLD.

For each linear-pacing view:

- use ten equal difficulty buckets and ten equal phases;
- allocate 73% of each phase from its corresponding bucket and 3% from every other bucket;
- use deterministic largest-remainder rounding;
- use seeded hash ordering within every phase/bucket cell;
- include every sample exactly once.

For each curriculum-warmup view:

- the first half is the easiest half in strict order;
- the second half is a seeded hash permutation of the remaining samples;
- there is no repetition or omission.

Every manifest records the same `population_root`, sample count, token count, tokenizer fingerprint and annotation configuration. Verify every order vector is a permutation of `0..4,882,812`.

View sorting and publication are estimated at 0.5–2 hours.

## Tests and failure handling

Before submission, run fixture and synthetic-object tests for:

- golden compression, Flesch and MTLD values;
- EOS-to-paragraph decoding;
- short, empty, non-English, code and malformed token cases;
- stable IDs across 100M/1B/5B/20B reuse;
- uncommitted and `.partial` objects being ignored;
- malformed/truncated producer commits being retried;
- wrong size or checksum rejection;
- two workers racing for one claim;
- kill before sidecar installation;
- kill after sidecar installation but before commit;
- restart with empty node-local storage;
- committed-task requeue producing no duplicate rows;
- `USR1` claim release and explicit requeue;
- retry exhaustion and dead-letter pause;
- automatic 2→4→8 scaling and 8→4→pause throttling;
- final population reconciliation;
- all ten order vectors being exact permutations;
- identical population roots across all views.

Safe rollback consists solely of creating the annotation `PAUSE` marker and cancelling annotation-owned jobs. Preserve the queue, commits and partials for inspection. Never remove or alter any file, allocation, queue marker, or job belonging to the base run.

## Timeline from the live state

- T0–T+90 min: implement and test locally while the base run continues unchanged; stage only the annotation code, pinned tokenizer and runtime into the separate annotation root.
- As soon as tests pass: submit the one-CPU watcher.
- On validated 100M commit: watcher submits the array once and runs the 100M benchmark.
- Following benchmark: operate at two workers, scaling only under the stated gates.
- During every base mixed-pack stage: pause annotation claims.
- On validated 20B commit: reconcile remaining objects; annotations should normally have only the final production burst left.
- Estimated annotation tail after base completion: 0.5–2.5 hours, including view creation.
- Final step: write checksums and `FINAL.COMMIT.json`.

The active base jobs' 01:06 PDT time limit remains an external risk. If they fail to publish `commits/20b.json`, retain all completed annotations and wait for an operator-directed base recovery; do not modify or restart the base jobs from this workflow.

## Adversarial objections addressed

- Producer commits are not fully crash-durable: watcher requires stable repeated reads, and workers independently hash objects.
- Eight workers cannot parallelize the one-object 100M tier: each object task uses eight internal processes.
- Concurrent scratch reads can slow source copying and packing: one shared read per object, node-local processing, stage caps and automatic pause.
- `--requeue` alone is insufficient: explicit signal handling and per-element requeue are required.
- Current worker headroom is not reusable capacity: option 3 is rejected.
- Full JSON block-ID manifests would be unnecessarily large: compact `uint32` order vectors are used.
- The base job itself may hit its existing wall-time: this is monitored but deliberately not repaired by the annotation workflow.

## Assumptions

- The existing packed corpus is approved for processing on FarmShare.
- The pinned tokenizer can be staged into the separate annotation runtime.
- At least 10 GB of additional scratch headroom is available for sidecars, views, logs and retries.
- No learnability fields, models, checkpoints, losses, GPU jobs or learnability views are introduced.
