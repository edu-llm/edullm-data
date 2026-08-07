# Measuring FinePhrase cross-config id overlap

`measure_finephrase_overlap.py` + `measure_finephrase_overlap.sbatch`

This is **open item 1** of [`docs/FINAL-DATASET-MIX.md`](../docs/FINAL-DATASET-MIX.md).

---

## Why this exists

`HuggingFaceFW/finephrase` publishes four configs — `faq`, `math`, `table`, `tutorial`. They are
**not four corpora.** They are ONE corpus rephrased four ways over the same ~339 M FineWeb-Edu
documents. Drawing 15 B tokens from each yields ~15 B of distinct documents wearing four hats, and
nothing downstream catches it:

- four rephrasings are four different strings, so exact-digest dedup passes;
- MinHash at the usual threshold misses paraphrase, so fuzzy dedup passes;
- every token count still adds up, so no sizing check fires.

A prior spot-measurement at revision `78cf4a5e` sampled **4 × 1000 ids** and found 90.3–93.2%
pairwise overlap and **28.5% distinct** across all four. **Every synthetic sizing number in this
project scales off that 28.5%** — the weighted-partition 131.0 B, the whole synthetic category
budget — and it rests on 4,000 ids out of 1.354 billion rows. This script replaces the estimate
with a census.

It also measures the **second collision**: FinePhrase rephrases FineWeb-Edu, which `edu-web` also
draws. Untreated, one document can appear as real edu-web text **and** as its own rephrasing in a
single training run.

---

## What it reads, and why that is the whole performance argument

FinePhrase carries **12 columns** — FineWeb-Edu's 11 plus `rollout_results`:

```
text          str    <- the ORIGINAL, UNREPHRASED FineWeb-Edu document. NOT the rewrite.
id            str    <- "<urn:uuid:e2300ad5-01dd-4e80-92b3-7ec88785cc9d>"  (49 bytes)
dump, url, file_path, language, language_score, token_count, score, int_score, dataset
rollout_results  LIST<STRUCT{finish_reason, text, usage{...}}>
                      <- THE REWRITE IS rollout_results[0].text
```

**The trap is real.** Point a counting script at the top-level `text` and you have counted the
original FineWeb-Edu document, not the rewrite — a corpus of unrephrased real data labelled
synthetic, which no size or hash check can notice. This script needs the **id**, not the text, so
the trap does not bite it; but it asserts the schema anyway (`verify_schema_trap`) so the JSON
output carries the evidence for the next reader.

The script reads **only the `id` column**. Measured from a real footer (`faq/000_00000_0.parquet`
at the pinned revision):

| leaf | compressed bytes | share of file |
|---|---|---|
| `text` | 186,644,641 | 69.1% |
| `rollout_results.list.element.text` | 73,385,752 | 27.2% |
| **`id`** | **2,542,091** | **0.94%** |

Against **6.16 TB** of parquet (5.16 TB FinePhrase + 0.998 TB FineWeb-Edu `sample/350BT`) the job
moves **~111 GB** — **1.8%**. From the measured per-file id bytes: 27,104 × 3.5 MB = 94.9 GB
FinePhrase + 472 × 33.9 MB = 16.0 GB FineWeb-Edu.

### Scale of the census, from measured rows per file

| group | files | rows/file | ids | uint64 keys |
|---|---|---|---|---|
| `faq` | 6,791 | 67,000 | 455 M | 3.64 GB |
| `math` | 6,787 | 76,000 | 516 M | 4.13 GB |
| `table` | 6,772 | 77,000 | 521 M | 4.17 GB |
| `tutorial` | 6,754 | 67,000 | 453 M | 3.62 GB |
| FineWeb-Edu `sample/350BT` | 472 | 728,000 | 344 M | 2.75 GB |
| **total** | **27,576** | | **2.29 × 10⁹** | **18.3 GB** |

Note the FinePhrase row sum, 1.945 × 10⁹, **exceeds the card's stated 1,354,044,711 output
samples by 44%.** The per-file row counts above are extrapolated from one sampled file per config,
so the discrepancy is most likely non-uniform file sizes rather than a card error — but it is
unresolved, and the census settles it: `per_config.<c>.rows_read` in the output is an exact count.
If it lands at 1.354 × 10⁹, these extrapolations were high and the scratch/memory figures below
are correspondingly conservative.

---

## Confirmed facts (asserted in the script, not assumed)

Everything below was verified against the pinned revision before the script was written, and each
is re-checked at runtime as a hard error:

| fact | how it is enforced at runtime |
|---|---|
| resolved revision `78cf4a5e` → `78cf4a5ed0099214979c094c963e699c19163838` | `resolve_revision` fails unless the full sha prefix-matches; the sha is recorded in the output |
| the id column is named **`id`** on both repos | `read_one_file` dies listing the actual columns if absent |
| 12 columns = FineWeb-Edu's 11 + `rollout_results` | exact set comparison per file; missing *or* extra is fatal |
| `rollout_results` is **always length exactly 1** | proved from the FOOTER for **every row group of every file**, at zero extra I/O — see below |
| `text` != `rollout_results[0].text` | one row group per shard is read directly and compared |
| FinePhrase's parent is FineWeb-Edu **`sample-350BT`** | card at the pinned revision: `source_datasets: [HuggingFaceFW/fineweb-edu/sample-350BT]`; repo path `sample/350BT`, 472 files, 0.998 TB |
| file counts: faq 6,791 · math 6,787 · table 6,772 · tutorial 6,754 = **27,104** | the `tree` phase lists them at the pinned sha and every shard index must report in |

### The length-1 proof is stronger than the evidence it replaces

The design cites `/statistics` (mean = median = min = max = 1.0 over 842,000 rows). This script does
better for free. The parquet leaf `rollout_results.list.element.text` reports `num_values` = the
number of list **elements** in a row group. A length-2 list would push `num_values` above
`num_rows`; a length-0 list would pull it below. So `num_values == num_rows` for every row group of
every file is a **complete proof over all 1.354 × 10⁹ rows**, read from the footer we already have.

---

## Exact or sampled?

**Exact mode runs, and it is the default.** The reason is worth stating plainly, because the
instinct is backwards here:

> **Sampling saves almost nothing.** A hash-prefix sample still requires you to READ every id in
> order to evaluate its hash. Sampling cuts RAM and sort time; it does not cut the HTTP bytes or the
> request count, and **those are the binding cost.** Exact mode is therefore nearly free relative to
> sampled mode.

`--sample-mod k` exists as the fallback for a node that cannot hold the arrays, not as the expected
path. When it is used the output stamps `mode: "sampled"` and reports a **Wilson 95% interval** on
the distinct fraction. In exact mode the CI is `null` and `ci_method` says why — reporting a
sampling interval on a census would be a category error.

**Never sample by taking the first N rows.** Parquet row order in these repos follows CommonCrawl
dump order, so a prefix is a sample of the earliest crawls. That is the prior measurement's
weakness and more rows from the front does not fix it.

### The representation, and its one admitted approximation

An id is a 49-byte string; a Python `set` of the 2.29 × 10⁹ ids in scope would run to several
hundred GB. Ids are stored instead as
**the high 8 bytes of `sha256(id)`** as sorted `numpy.uint64` — 18.3 GB for the whole census, with
exact set arithmetic by `searchsorted`.

**This introduces a hash-collision probability.** Two different ids sharing a 64-bit key are counted
as one document, which **understates** the union and therefore **understates** the distinct
fraction. With `n` distinct ids:

```
E[colliding pairs] = n(n-1)/2 / 2**64  ≈  n² / 2**65
```

At n = 4.0 × 10⁸ that is **4.3 × 10⁻³ pairs** — the census would have to run ~230 times before one
collision is expected at all. It cannot move any reported fraction at three significant figures.
The realized value is computed from the actual `n` and written to
`hash_keying.hash_collision_expected_pairs`.

---

## Resource request, and where every number comes from

All timings **measured** against real files at the pinned revision, from a residential link.
FarmShare's network is better, so these are conservative.

| resource | value | justification |
|---|---|---|
| `--cpus-per-task` | **4** | I/O bound, not compute bound. Measured hashing cost is **0.71 µs/id**, so the entire 2.29 × 10⁹-id census is **0.45 core-hours** of real CPU. The 4 CPUs cover the hashing thread, ~24 socket-blocked prefetch threads, and pyarrow decompression. |
| `--mem` | **8G** per task | Peak RSS is the uint64 key array a task accumulates before writing: 2.29 × 10⁹ / 64 = 36 M ids × 8 B = **286 MB**, plus prefetch cache (~34 MB × 8 readers) and one row-group batch of id strings (~3 MB). Measured peak on a 5-file probe: **230 MB**. 8 G is a >10× margin; 2 G would be tight on the 728-row-group FineWeb-Edu files. |
| `--time` | **08:00:00** | Measured **7.9 s** per FinePhrase file and **84.8 s** per FineWeb-Edu file. Serial total = 27,104 × 7.9 + 472 × 84.8 = **71.6 core-hours**; over 64 tasks that is **1.12 h mean**. Tasks are unequal — the FineWeb-Edu files are 10× slower each and only 472 of them spread over 64 tasks, so the worst task carries ~8. 8 h is ~7× the mean and ~4× the worst plausible task, deliberately loose. Tighten to `03:00:00` after a first array reports real times. |
| `--array` | **0-63** | 64 tasks × 8 in-task readers = 512 concurrent range requests. Lower this if the Hub starts returning 429 (retries handle them but cost wall clock). `--nshards` **must** equal the array size. |
| GPU | **none** | Nothing here is a tensor. |

**Total cost: ~72 core-hours of mostly-idle I/O wait, ~111 GB of HTTP, ~37 GB of scratch.**
Real CPU inside that is under one core-hour; the rest is socket latency.

### Why the read is fast at all

The naive read is **latency-bound, not byte-bound**, and it is worth knowing why. An `id` column
chunk is ~38 KB, but consecutive chunks sit ~4 MB apart because `text` and `rollout_results` lie
between them. So reading the id column is 67 **small, scattered** ranges. Measured: 3.2 MB in 69
serial requests took **49 s** — 0.72 s each, essentially pure round-trip latency. Extrapolated
naively, that is **378 single-threaded hours**.

Coalescing is not the fix: merging those chunks into one range means spanning the holes too, i.e.
downloading the whole 270 MB file — 84× the bytes, which destroys the id-column-only argument.
The fix is to issue the scattered ranges **concurrently**, from exact offsets read out of the
footer. Same request count, same bytes, no longer serial: **7.9 s per file, a 6.3× speedup.**

> **Gotcha, measured and worth remembering.** Do **not** use the footer's `file_offset` to locate a
> column chunk. In these files it is **0** for every `id` chunk (the field is optional in the Thrift
> spec and this writer leaves it unset). Trusting it collapses every range to offset 0 and the
> prefetch silently becomes a no-op that fetches the wrong bytes *while still reporting success*.
> Use `dictionary_page_offset` when present, else `data_page_offset`.

---

## How to run it

Three phases, deliberately separate: one four-hour HTTP job is a fragile job.

### 0. Stage the files and build a venv (login node, once)

```bash
SUNET=YOUR_SUNETID
WORK=/scratch/users/$SUNET/finephrase-overlap
ssh -S /tmp/farmshare-$SUNET.sock -o BatchMode=yes $SUNET@login.farmshare.stanford.edu \
  "mkdir -p $WORK/logs $WORK/hash"

# from the repo, upload the two files (an upload, not a download — see the transfer rules)
scp -o "ControlPath=/tmp/farmshare-$SUNET.sock" \
  scripts/measure_finephrase_overlap.py scripts/measure_finephrase_overlap.sbatch \
  $SUNET@login.farmshare.stanford.edu:$WORK/

# venv: numpy + pyarrow, nothing else. The script imports nothing from edullm_data.
ssh -S /tmp/farmshare-$SUNET.sock -o BatchMode=yes $SUNET@login.farmshare.stanford.edu \
  "source /etc/profile.d/z00_lmod.sh && module load python/3.11 && \
   python3 -m venv $WORK/venv && $WORK/venv/bin/pip -q install --upgrade pip numpy pyarrow"
```

### 1. Selftest (login node, seconds, no network)

Run this **first**. It proves the set arithmetic and the partition formula against a synthetic
fixture whose answer is known in advance, and it catches a broken numpy/pyarrow before an array
spends node-hours.

```bash
$WORK/venv/bin/python3 $WORK/measure_finephrase_overlap.py selftest --work $WORK
# expect: SELFTEST PASSED
```

### 2. `tree` (login node, once, ~1 minute)

Resolves the pinned revision to a full commit sha and lists every parquet file. Login-node safe —
a few hundred KB of Hub API calls. **Must not** be run per-task: 64 tasks each paginating the tree
API is both rude and a correctness risk, since a listing fetched per-task can differ per-task.

```bash
$WORK/venv/bin/python3 $WORK/measure_finephrase_overlap.py tree \
  --work $WORK --revision 78cf4a5e
# expect: 27,576 files, 6.160 TB total -> $WORK/tree.json
```

### 3. `hash` (the job array)

```bash
sbatch --exclude=wheat-01 --array=0-63 \
  --export=ALL,FP_WORK=$WORK,FP_VENV=$WORK/venv,FP_REVISION=78cf4a5e \
  $WORK/measure_finephrase_overlap.sbatch
```

Monitor, then confirm every shard reported in:

```bash
squeue --me
sacct -j <JOBID> --format=JobID,State,Elapsed,MaxRSS,ExitCode
ls $WORK/hash/*.json | wc -l    # must be 64
```

**The hash phase is idempotent.** A shard whose `.npz` + `.json` already exist and validate is
skipped, so a partially-failed array is resumed by **resubmitting the same command**. Use
`FP_EXTRA_ARGS=--force` to redo one deliberately.

### 4. `reduce` (one task, serial, memory-heavy)

```bash
sbatch --exclude=wheat-01 --cpus-per-task=2 --mem=48G --time=02:00:00 \
  --job-name=fp-overlap-reduce \
  --output=$WORK/logs/reduce-%j.out --wrap \
  "$WORK/venv/bin/python3 $WORK/measure_finephrase_overlap.py reduce \
     --work $WORK --nshards 64 --out $WORK/overlap.json"
```

**48 G, not 8 G**, and for a specific reason: `reduce` calls `np.unique` on one group's full key
array, which peaks at roughly **3× that array** (input + argsort workspace + output). The largest
group is `table` at ~521 M ids ≈ **4.17 GB**, so ~**12.5 GB** at peak, on top of the concatenation
buffer that produced it — call it ~17 GB worst case. Groups are processed **one at a time** and
each is spilled to a `.npy` reopened `mmap_mode='r'`, which is what keeps the combined **18.3 GB**
out of resident memory during the bucket walk; without that the peak would be
"whole census + largest group × 3" ≈ 31 GB. 48 G leaves ~2.8× headroom over the 17 GB estimate;
24 G would likely suffice. `cost.reduce_peak_rss_mb` in the output tells you for next time.

Scratch usage: **~18.3 GB** of `.npz` shards plus **~18.3 GB** of sorted `.npy` spills ≈ **37 GB**.
Check `df` on `/scratch/users/$USER` before submitting.

Then fetch the result — it is a small JSON, and pulling it local is a **gated download**, so ask
first:

```bash
ssh -S /tmp/farmshare-$SUNET.sock -o BatchMode=yes $SUNET@login.farmshare.stanford.edu \
  "cat $WORK/overlap.json"   # inspect remotely; do not redirect to a local file unprompted
```

---

## Reading the output

`--out` writes JSON; the same content is printed as a human summary. The fields that matter:

| field | meaning |
|---|---|
| `mode` | `exact` (census) or `sampled`. **Check this first.** |
| `finephrase_revision_sha` | the full resolved commit. A run at `main` and a run at `78cf4a5e` are different measurements. |
| `partial` | `true` means shards were missing and **the result must not be consumed**. |
| `per_config.<c>.distinct_ids` | distinct source documents in that config |
| `per_config.<c>.within_config_duplicate_ids` | should be **0** — one rewrite per source document per config. Nonzero is a finding. |
| `pairwise.<a>\|<b>.jaccard` | \|A∩B\| / \|A∪B\|. The prior spot-measure found 0.903–0.932. |
| **`distinct_fraction`** | **the number everything scales off**: 4-way union ÷ sum of per-config distinct counts. Prior: 0.285. |
| `distinct_fraction_ci95` | Wilson 95% in sampled mode; `null` in exact mode, with `ci_method` saying why |
| `documents_in_exactly_k_configs` | the overlap structure, not just its summary. If nearly everything is at k=4, the four configs are one corpus and the design's read is confirmed. |
| `eduweb_collision.fraction_of_finephrase_union` | share of distinct FinePhrase source documents that also live in the FineWeb-Edu subset `edu-web` draws — the anti-join half of §9.7 item 4 |
| `partition_audit.worst_deviation_pp` | worst \|share − 25.0%\| across all 4 partitions × 4 configs, in **percentage points** |
| `partition_audit.all_partitions_meet_floor` | every partition ≥ **17.3%** (the design bar) |
| `hash_keying.hash_collision_expected_pairs` | the collision budget, from the realized union size |
| `verdict.synthetic_sizing_implication` | the measured fraction rescaled onto the 131.0 B weighted-partition figure |

### The partition audit is an independent check, on purpose

The script **reimplements**

```python
partition_of(doc_id) = int.from_bytes(sha256(doc_id.encode()).digest(), 'big') % 4
```

rather than importing `src/edullm_data/reservoir_ids.py`. Importing it would make the check
circular — the module would be validating itself. If the two ever disagree, one of them is a bug
and this measurement is how you find out. The one thing to keep in sync by hand is
`CONFIGS = ("faq","math","table","tutorial")`, which must match
`reservoir_ids.FINEPHRASE_FORMATS`: **that order IS the partition assignment**, so reordering it
silently reassigns every document.

Note the audit reports shares **per config over that config's own rows**, because the partition
decides which rows a config KEEPS. Partition counts are accumulated during `hash`, where the id
strings still exist — only the 64-bit key survives into `reduce`, and the partition is a function
of the string.

### `verdict` is all-else-equal only

`synthetic_sizing_implication` rescales the 131.0 B weighted-partition figure by
`measured / 0.285`. That is arithmetic, not a new sizing: the weighted 35/35/15/15 partition's real
yield also depends on per-format token means, which this script does **not** measure — it reads no
text at all. Combine with `artifacts/recount/synthetic.json` for the token side.

---

## Failure posture

This project has been bitten repeatedly by a **silent empty result reporting success**, so the
script fails loudly:

- a config yielding zero rows is a **hard error**, not a zero;
- a file whose footer lacks an `id` leaf, or whose column set differs from the expected 12, is a
  hard error;
- `rollout_results` not length 1 in any row group of any file is a hard error;
- a null / empty / non-string id anywhere is a hard error — both repos carry a URN-shaped uuid on
  every row, so it means the column selection is wrong;
- a short HTTP range response (server ignored `Range`) is retried, then fatal, rather than being
  fed to pyarrow as truncated parquet;
- **`reduce` refuses to run if any hash shard is missing**, and names the missing indices. A short
  reduce shrinks every config by roughly the same fraction, so the Jaccards stay plausible and the
  distinct fraction comes out roughly right — *inflated*, in the direction that flatters us, with
  no visible symptom. `--allow-partial` overrides it and stamps `partial: true`; nothing downstream
  should consume that.

A `FATAL:` line and exit code 2 mean an assumption was violated. Read it; do not retry around it.

### One bug this script's own test caught, preserved as a regression

The first version sampled on `int(sha256(id)) % k` and partitioned on `int(sha256(id)) % 4` — the
**same integer**. Whenever 4 divides `k` (4, 100, 1000 — every round number an operator would
actually type), the sample predicate *forces* `partition == 0`. A real `--sample-mod 1000` run duly
reported **100.0% of documents in partition 0** and 0.0% in the other three: a partition audit that
looked catastrophically broken when the partition was fine and the **sampler** was broken. The
sampling digest is now salted (`SAMPLE_SALT`), and `selftest` asserts that all four partitions stay
populated under a 4-divisible `sample_mod`.

---

## Things this script does NOT do

- **It does not count tokens.** It reads no text. Token means per format come from
  `artifacts/recount/synthetic.json` and `artifacts/recount/_fp_footer_leaf.py`.
- **It does not measure the full FineWeb-Edu corpus by default.** The default `--eduweb-configs
  sample/350BT` is FinePhrase's *declared parent* — the id space where the collision can exist at
  all. But `docs/FINAL-DATASET-MIX.md` has `edu-web` drawing **FineWeb-Edu full (~1,293 B)**, which
  is `--eduweb-configs data`: 2,410 files, 4.52 TB, ~59 GB of id bytes and roughly +8 h of array
  time. **Run both.** `sample/350BT` bounds the collision from below (a 350 BT-sampled parent means
  some FinePhrase sources are outside any given `edu-web` draw); `data` is the number that matters
  operationally, and it should come out near 100% since every FinePhrase source document is by
  construction a FineWeb-Edu document.
- **It does not decontaminate anything**, and rephrasing is exactly what defeats n-gram
  decontamination. Separate problem, separate open item.
- **It does not wire in `keeps_id`.** That is the standing ingest blocker in
  `docs/FINAL-DATASET-MIX.md`; this script only measures whether the partition it computes is
  balanced enough for the design to hold.
