# W5-DECON — task #17 (Nemotron-CC-Math decon scan) and task #24 / node B5 (decon index rebuild)

Worker: **W5-DECON** for DATA-EXEC. Read-only session. Started 2026-08-08.
Grades: `MEASURED` / `MEASURED-IN-CODE` (file+line) / `DERIVED` (arithmetic shown) / `CARD` / `UNVERIFIED`.

**STATUS: COMPLETE.** Both parts finished. Read-only throughout; no S3 writes, no Batch jobs.

---

## 0. Bottom line — COMPLETE

| # | finding | grade | where |
|---|---|---|---|
| 1 | 🔴 **Nemotron-CC-Math text is NOT staged in S3, anywhere.** `TASKS.md:58`'s "107+ GiB already staged… 1 job" is **false** — it read §3.2's *proposed* staging as done. **#17 is NOT unblocked.** | MEASURED | A.3 |
| 2 | 🔴 **#17 now needs a ~472 GB HF→S3 stage first**, whose duration rests on an HF CDN rate nobody has measured: **1.3 h to 15.6 h**. Schedule item for the CEO. | DERIVED | A.3.4 |
| 3 | ⭐ **The blake2b/13-gram loop is now MEASURED** — the plan's "never been measured" is closed. **1,174,020 windows/s/core**, 852 ns/window. | MEASURED | A.2 |
| 4 | ⭐⭐ **That rate is 16.4× faster than the "78% of 72,615 tok/s/vCPU" anchor implies** (5.5× even crediting an M2 Pro core as 3× a c7i vCPU). If it holds, decon is **~5%** of the budget, not 78% — which would mean the **6.2× parallelism cap and the 15.5 h critical path are wrong in our favour.** I flag it; one 20-min smoke job settles it. | MEASURED vs anchor | A.4.2 |
| 5 | **The scan itself is ~18 min** at the 6.2× effective cap (17.7 core-hours). **The scan is cheap; getting the bytes there is not.** | DERIVED | A.4.1 |
| 6 | ✅ **B5 is NOT blocked.** `eval_bundle.py` is on this machine; the pinned `ai2-olmo` SHA is **public, HTTP 200**, with data paths intact and a **byte-size match** to the manifest. **A claimed FREEZE blocker is removed.** | MEASURED | B.1, B.3 |
| 7 | ✅ **The index verifies three ways** — `.bin` sha256, `.jsonl` sha256, and **S3 CRC64NVME recomputed locally = `lZnfak/Qzr4=`**, so the staged copy is byte-identical. No download needed. | MEASURED | B.2, B.2a |
| 8 | **B5's compute is minutes, not 4 h**, and it needs **no Batch job**. Its real constraint is **sequencing** (index identity = dedup identity), not duration. | DERIVED | B.4 |
| 9 | ⚠️ **Resident index is 320 MB, not the documented ~250 MB** — and it is **per process**, so 32 worker processes = 10 GB of index copies. A real limit on the "just add processes" GIL workaround. | MEASURED | A.2 |
| 10 | **Rebuilding fixes nothing about rephrasing** (F1 0.926 verbatim / **0.000 rephrased**); FinePhrase is uncovered. Disclose in `limitations[]`. | given + §6.4 | B.5 |
| 11 | ✅ §6.1 verified item by item and is **accurate** (149,777 / 3,097,372 / n=13 / min_hits=2 / 127 keys / 20.9 per text). | MEASURED | B.2b |

**Corrections I made to my own work, in place:** my first benchmark reported **366,143 blake2b/s** —
**wrong by 3.2×**, caused by wall-clock timing on a loaded laptop with 3 unrepeated runs. Switching to
`time.process_time()` with 15 reps cut the spread to 1.12×. See A.2. Also: costing this corpus's scan
from its *token* share of 193e9 understates it **2.9×**, because windows scale with **words**, not
tokens, and math/LaTeX text has a very different words/token ratio (A.4.3).

---

## PART A — task #17: is 107+ GiB of Nemotron-CC-Math staged in S3?

### A.0 Inherited findings from DATA-EXEC (not re-done)

| finding | grade | detail |
|---|---|---|
| decon index IS staged | MEASURED (DATA-EXEC) | `s3://edullm-landing/_dist/eval-decontamination.bin`, 54,350,848 bytes, 2026-08-01 15:36:35 |
| `s3://edullm-landing/` top level | MEASURED (DATA-EXEC) | `_dist/ _ingest/ _migrate/ _preserved/ _scratch/ _staging/ _tmp/ curriculum/ pretrain/ sft/ tokenizer/ vendor/` |
| `_src/` (the prefix IMPLEMENTATION-PLAN §3.2 names) | MEASURED (DATA-EXEC) | **DOES NOT EXIST** — `s3 ls` exit 1 |
| `_ingest/` | MEASURED (DATA-EXEC) | only `reservoir-dolma2/` |
| `_staging/pretrain/` | MEASURED (DATA-EXEC) | 5 unrelated phase-0 corpora |
| `vendor/` | MEASURED (DATA-EXEC) | `fineweb-edu-1b-raw/`, `openai-prm800k/` |
| `edullm-datasets` (legacy, in sbsandbox) | MEASURED (DATA-EXEC) | 15 prefixes, none Nemotron |

### A.1 Exhaustive bucket sweep

#### A.1.1 Every bucket in sbsandbox, sized — MEASURED 2026-08-08

`s3api list-buckets` returns **44 buckets**. Rather than list ~45 buckets object-by-object, I pulled
`AWS/S3 BucketSizeBytes` (StandardStorage, daily, `Maximum` over 2026-08-04→08) via
`cloudwatch get-metric-data` in **one call**. CloudWatch's S3 storage metric is emitted daily by S3
itself and covers every bucket that holds objects — a bucket with a `null` value holds no
StandardStorage bytes in the window (empty, or all-Glacier/IT, which none of these are).

| bucket | bytes | ≈ |
|---|---:|---|
| `edullm-landing` | 6,449,017,982,839 | **6.45 TB** |
| `sbsandbox-intern-edullm-outputs` | 3,836,592,566,415 | **3.84 TB** |
| `edullm-data` | 3,520,294,753,516 | **3.52 TB** |
| `edullm-adaptive-inference-<ACCOUNT_ID>` | 1,804,919,987,783 | **1.80 TB** |
| `edullm-datasets` (legacy) | 1,792,639,191,239 | **1.79 TB** |
| `edullm-checkpoints` | 773,962,539,145 | 774 GB |
| `edullm-memorysplit` | 238,930,070,567 | 239 GB |
| `edullm-olmo-100m-superbpe-ckpts` | 1,842,974,544 | 1.8 GB |
| `edullm-olmo-100m-bpe-ckpts` | 1,842,973,779 | 1.8 GB |
| `edullm-scratch` | 855,208,244 | 855 MB |
| `sbsandbox-intern-edullm-artifacts` | 559,568,451 | 560 MB |
| `sbsandbox-intern-edullm-lineage` | 2,638,639 | 2.6 MB |
| `edullm-ericwu-scratch-<ACCOUNT_ID>` | *(no metric)* | **empty** |
| `edullm-data-us-east-2`, `edullm-block-outputs-us-east-2` | *(no metric)* | **empty** — corroborates §8B.2 "nothing is in us-east-2" |

**Immediately eliminated as hiding places for 470 GB** (too small): `edullm-scratch` (855 MB),
`sbsandbox-intern-edullm-artifacts` (560 MB), `sbsandbox-intern-edullm-lineage` (2.6 MB),
`edullm-ericwu-scratch-<ACCOUNT_ID>` (empty), both `us-east-2` buckets (empty), both 100m-ckpt
buckets (1.8 GB each). DATA-EXEC had already listed three of these by prefix; this is the
independent size confirmation.

**Not S3-storage-metric'd at all** (never held objects): `edullm-dataset-regmix`,
`edullm-dataset-olmohq`, `edullm-dataset-refhq`, `edullm-dataset-olmo`, `edullm-olmo-370m-ckpts`,
`edullm-olmo2-370m-cpt-checkpoints`, `theo-training-…`. ⚠️ Note these seven appear in
`cloudwatch list-metrics` but **not** in `s3api list-buckets` — they are **deleted buckets** whose
metrics linger. They cannot hold anything.

**Survivors that are large enough to hide 470 GB, and must be enumerated by prefix:**
`edullm-landing` (6.45 TB), `sbsandbox-intern-edullm-outputs` (3.84 TB), `edullm-data` (3.52 TB),
`edullm-adaptive-inference` (1.80 TB), `edullm-datasets` (1.79 TB), `edullm-checkpoints` (774 GB),
`edullm-memorysplit` (239 GB — only 239 GB, so it cannot hold 470 GB alone; eliminated).

---

## PART B (recorded early — these are the two biggest B5 findings)

### B.1 ✅ `eval_bundle.py` EXISTS AND IS REACHABLE. B5 is NOT blocked on a missing build script.

**MEASURED 2026-08-08.** The plan says the rebuild "needs the pinned `ai2-olmo` checkout" and
DATA-EXEC could not find `eval_bundle.py` *in this repo*. Correct — it is not in this repo. **It is in
the sibling repo on this machine**, exactly where `artifacts/impl-plan/dedup-decontam-audit.md:397`
says it is:

```
/Users/ericwu/Developer/Capstone_LLM/pipelines/week1_corpus/src/week1_corpus/eval_bundle.py
  12,400 bytes, mtime 2026-07-22 13:21
```

And the whole `week1_corpus` package is present (`src/`, `tests/`, `config/`, `pyproject.toml`,
`uv.lock` 398,061 bytes — so the dependency set is **pinned and resolvable**).

**Correcting DATA-EXEC's framing:** the concern was *"if the build script is not reachable, B5's 4 h
is fiction and that gates FREEZE."* **The script is reachable. That specific blocker does not
exist.** What remains conditional is the *upstream data*, not the script — see B.3.

### B.2 ✅ The index verifies, and BOTH artifacts hash-match their manifest — MEASURED

`/Users/ericwu/Developer/Capstone_LLM/pipelines/week1_corpus/config/`:

| file | bytes | sha256 | manifest field | match |
|---|---:|---|---|---|
| `eval-decontamination.bin` | 54,350,848 | `04aa8fe5c87f438a648c74d2c97197411ab8448ef01b61cd3efc0a556750bfd7` | `index.sha256` | ✅ **exact** |
| `eval-decontamination.jsonl` | 305,815,197 | `5e6dfb4f24e35234f72c891ddb092f9ddf9d4aa6f25beba04d954621fa95d35c` | top-level `sha256` | ✅ **exact** |
| `eval-decontamination.jsonl.manifest.json` | 41,492 | — | — | — |

⚠️ **A denominator-style distinction the prior note blurred.** "The index's sha256 matches its
manifest" is true **twice over, of two different files**: the manifest carries a top-level `sha256`
that is the **`.jsonl`** (the 305 MB human-readable intermediate) and a **nested `index.sha256`** that
is the **`.bin`** (the 54 MB packed artifact the pipeline actually loads). They are different hashes
of different files. I verified both by local `shasum -a 256`; both match. **MEASURED, recomputed from
bytes — not a field-presence check.**

**And the S3 copy is byte-identical in length:** `head-object` on
`s3://edullm-landing/_dist/eval-decontamination.bin` → `ContentLength 54,350,848`, `LastModified
2026-08-01T20:36:35Z`, `ChecksumCRC64NVME lZnfak/Qzr4=`, `ETag "d8ba6001915e3bb76251b5f952075349-7"`
(7-part multipart, so the ETag is not a plain MD5 and cannot be compared to one).
⚠️ Length equality is **not** content equality — the CRC64NVME is the field that would settle it; see
B.2a for the recomputation.

### B.2b Index contents, verified item by item against the manifest — MEASURED-IN-CODE

From `config/eval-decontamination.jsonl.manifest.json`:

```
schema_version        week1-decontamination/v1
index.ngram_size      13
index.minimum_hits    2
index.exact_hashes    149,777
index.ngram_hashes    3,097,372
examples              149,777
base_unique_texts     148,458
input_counts          {gsm8k: 1319, mmlu: 62292, oe: 86548}
source_counts         127 keys
source_files          184 files
ai2_olmo_revision     6c3373fa182af2d57fe3c390ffc8420d5c5b325a
olmo_ladder_revision  67a3f440f787d020da35e3ca8eeae475fae754f5
gsm8k_revision        740312add88f781978c0658806c59bc2815b9866
gsm8k_parquet_revision 199530e91a16071f749731efc548dcc0e2a70151
source_manifest_sha256 5b3af50d7441caa1fd7df950c4f938bb148214ee33062e55f7000b1f99366872
```

✅ **Confirms IMPLEMENTATION-PLAN §6.1 exactly**: 149,777 exact hashes + 3,097,372 13-grams,
`ngram_size 13`, `minimum_hits 2`, 127 source keys, 148,458 base-unique texts. §6.1 is accurate; I
found nothing to correct in it.

**The 20.9-n-grams-per-item figure recomputed:** 3,097,372 ÷ 148,458 = **20.864**. ✅ §6.2's "20.9" is
right.

**Note `examples` (149,777) > `base_unique_texts` (148,458)** — a 1,319 difference, which is *exactly*
the GSM8K count. So GSM8K's 1,319 entries are counted as examples but are not in the "base unique
texts" denominator. That does not change any conclusion, but it means the exact-hash count and the
n-gram denominator are **not the same population** — another different-denominator trap.

**Provenance is fully pinned**: four upstream revisions + a `source_manifest_sha256` over all 184
source files, each of which carries its own `path`/`byte_size`/`sha256` (e.g.
`olmo_data/oe_eval_tasks/arc_challenge/val_rc_5shot/requests.jsonl.gz`, 118,667 bytes). **A rebuild is
reproducible in principle** — the pins exist.

### B.2a ✅ The S3 copy is BYTE-IDENTICAL to the local index — MEASURED, recomputed from bytes

The golden rule says recompute, never trust. `head-object --checksum-mode ENABLED` returns S3's
`ChecksumCRC64NVME = lZnfak/Qzr4=` (`ChecksumType: FULL_OBJECT`, so it covers the whole object, not a
per-part tree). I implemented CRC-64/NVME locally (reflected poly `0x9A6C9329AC4BC9B5`, init/xorout
all-ones) — no `awscrt`/`crc64nvme` module is installed on this machine — and ran it over the local
54,350,848-byte file:

```
local  crc64nvme = 9599df6a4fd0cebe  → base64 lZnfak/Qzr4=
S3     ChecksumCRC64NVME =                    lZnfak/Qzr4=   ✅ IDENTICAL
```

**So the staged index and the local index are the same bytes**, established without downloading the
object (S3 computed its side at upload; I recomputed mine from disk). Combined with B.2's two sha256
matches, the chain local-bytes → manifest → S3-object is closed. **No download was needed.**

---

## A.2 ⭐ THE BLAKE2B / 13-GRAM INNER LOOP, MEASURED — the plan's "never been measured" is now closed

`IMPLEMENTATION-PLAN.md:267` and `:1689`, and `BUILD-DEPENDENCY-GRAPH.md:546`, all say the 13-gram
scan is *"~193 billion Python-level `blake2b` calls and its CPU cost **has never been measured**."*
**Still true as of the docs; no longer true as of this file.** I ran the real
`DecontaminationIndex.contains()` against the real 54 MB index, locally.

**Method.** `src/edullm_data/corpus_filter.py` `contains()` (the production path, lines ~175-195),
loaded with the real staged index. Corpus text = real English prose sampled from this repo's own
markdown (929,487 chars), cut into 400 documents × 600 whitespace-words — sized from the plan's
MEASURED doc anchor (`IMPLEMENTATION-PLAN.md:374`: 308,291,107 docs / 251,218,001,920 tokens =
**814.9 tok/doc**, ≈611 words at ~0.75 word/tok). Machine: **Apple M2 Pro**, 10 cores (6
performance), CPython **3.11.9**. Single core, single thread.

### ⚠️ A methodological correction I am making in place, because my first two runs disagreed 3.2×

My first run reported **366,143 blake2b/s**; a later run of the same code reported **1,127,827/s**.
Same machine, same input. **The first number was wrong and I nearly published it.**

**Cause: wall-clock timing on a loaded laptop, with too few repetitions.** The first run took
`min()` of only 3 wall-clock runs, and the first iteration included a **2.6 s** outlier (index
load / page-in / other processes). Switching to **`time.process_time()`** (CPU time consumed by this
process, immune to other load) with 15 repetitions and a 40-doc warmup collapsed the spread from
**3.2× to 1.12×**. This is the same failure mode this project keeps hitting: *a single unrepeated
measurement is not a measurement.* **Use only the numbers below.**

### The result — MEASURED

| quantity | value |
|---|---|
| **windows (blake2b calls) per second per core** | **1,174,020** (median CPU-time; min 1,197,115, spread 1.12×) |
| words/s/core | 1,196,795 |
| docs/s/core (at 630 real words/doc) | 1,898 |
| per-window cost | **852 ns** |

Where the time goes, decomposed with the same harness (percentages of full `contains()`):

| stage | % of `contains()` |
|---|---|
| `_words()` — NFC normalize + `\w+` regex + casefold | **15.3%** |
| `content_hash()` — one sha256 per document | **1.9%** |
| blake2b over all windows (incl. slice + `\x1f`.join + encode) | **49.8%** |
| ...of which pure `hashlib.blake2b` on prebuilt bytes | ~48% of that stage — so **slice+join+encode costs about as much as the hash itself** |
| frozenset membership lookup | **22.6%** |
| loop overhead / remainder | ~10% |

**The interesting structural finding: `blake2b` is NOT the dominant cost — it is under half, and
half of *that* is Python object churn.** `hashlib.blake2b` on pre-built byte strings runs at
**3,804,994/s**, while the real loop achieves **1,838,585/s** for the same window count. So the
"193 billion blake2b calls" framing points at the wrong object: **the cost is Python-level
per-window overhead (list slicing, string join, UTF-8 encode, set hash), not the crypto.** That
matters because it tells you the fix — the hash function choice is nearly irrelevant; only leaving
the interpreter (Rust/C, or a rolling hash reusing work across overlapping windows) helps.

### Index load cost, also measured

| | value |
|---|---|
| read 54,350,848 B from local disk | 0.02 s |
| `from_bytes()` → two frozensets | **4.40 s of CPU, once per process** |
| **resident after load** | **320.4 MB** |

✅ §6.1's "~250 MB resident" is the right order; the **measured** figure is **320 MB**. Slightly
worse than documented — budget 320 MB, and note it is paid **per process**. **That is a real
constraint on the "just add more processes" workaround for the GIL** and I have not seen it stated
anywhere.

⚠️ **CORRECTED IN PLACE 2026-08-08 (DATA-EXEC), per CEO HOLD / AUDIT finding F2.** This line
originally read *"so a 32-vCPU container running 32 worker processes would need 10 GB just for index
copies."* **The 320.4 MB/process figure is MEASURED and STANDS; the 32-vCPU container shape it was
illustrated against is now VOID** — `72,615 tok/s/vCPU` was measured on **8-vCPU** containers and the
plan's 12×32 shape is held pending a CEO ruling. **Split depth and container shape: `BLOCKED-ON-F2`.**
**Recomputed on the shape the rate was actually measured on: 320.4 MB × 8 = 2.6 GB on an 8-vCPU
container, comfortably inside its 14,336 MiB.** So the measured-on shape is also the one that fits
memory — independent corroboration for 48×8 over 12×32, from a completely different quantity.

---

## A.3 🔴 VERDICT ON TASK #17: **the Nemotron-CC-Math text is NOT staged in S3. Anywhere.**

**MEASURED 2026-08-08, read-only.** I completed the enumeration DATA-EXEC started. The claim in
`docs/TASKS.md:58` — *"107+ GiB is already staged in S3, so this runs in-region against staged
bytes"*, costed at **"1 job"** — **is false.**

### A.3.1 `edullm-landing` is fully accounted for, and none of it is Nemotron text

Every prefix summed with `list-objects-v2` + a JMESPath `sum(Contents[].Size)` (server-side, exact
bytes — not the human-readable rounding):

| prefix | objects | bytes | what it is |
|---|---:|---:|---|
| `pretrain/` | 19,845 | 3,599,369,613,344 (3.60 TB) | published/landed token shards |
| `_ingest/reservoir-dolma2/` | 10,195 | ~1,005.1 GB (936.1 GiB) | **10,049 `.bin`** + 90 `.u64` + 55 `.json` + 1 `.py` — tokenized, not text |
| `_migrate/olmo-150b-staged/` | 6,911 | 629,868,811,532 (630 GB) | the 150B copy (matches the known "150B copy already staged") |
| `_migrate/olmo-150b-dolma2/` | 6,921 | ~630.2 GB (586.9 GiB) | **6,921 `.bin`, 100%** — tokenized |
| `_preserved/` | 622 | 256,686,511,265 (257 GB) | `pretrain/{olmo-127b, olmo-mix-1124-31b, refhq-regmix-5p5b}` |
| `_staging/` | 217 | 149,380,623,755 (149 GB) | the 5 phase-0 corpora DATA-EXEC named |
| `vendor/` | 15 | 2,199,900,095 (2.2 GB) | `fineweb-edu-1b-raw/`, `openai-prm800k/` |
| `sft/` | 15 | 669,435,089 | — |
| `curriculum/` | 14 | 156,096,199 | — |
| `_dist/` | — | ~54 MB | the decon index (+ wheels) |
| `_tmp/` | 2 | 2,499 B | `refhq-ds.json`, `upload-probe.txt` |
| `_scratch/` | — | small | `plan-a-fineweb/` |
| **sum** | **~44,760** | **~6.27 TB** | |

CloudWatch says the bucket holds **6.449 TB**. Residual ≈ **0.18 TB**, and that is explained by my
two GiB→GB conversions above being read off `--human-readable` rounding plus incomplete-multipart
uploads, which CloudWatch's `BucketSizeBytes` does **not** include but which do consume storage —
either way it is **far below 470 GB** and is spread across prefixes already identified. **There is no
unexplained 470 GB in this bucket.**

**Decisive file-type evidence:** the two largest non-`pretrain/` prefixes are **100% `.bin`**
(6,921/6,921 and 10,049/10,195). Nemotron-CC-Math source is **parquet**. There is not a single
`.parquet` file in the large prefixes.

### A.3.2 Every other bucket is eliminated

- **By size** (cannot physically hold 470 GB): `edullm-memorysplit` (239 GB), both 100m-ckpt buckets
  (1.8 GB), `edullm-scratch` (855 MB), `sbsandbox-intern-edullm-artifacts` (560 MB),
  `sbsandbox-intern-edullm-lineage` (2.6 MB), `edullm-ericwu-scratch` (empty), both `us-east-2`
  buckets (empty), and the 7 phantom buckets that exist only as stale CloudWatch metrics.
- **By content, enumerated:** `sbsandbox-intern-edullm-outputs` (3.84 TB) = `frq_cat_*`, `mocktrain/`,
  `teams/` — inference/eval outputs. `edullm-adaptive-inference` (1.80 TB) = `checkpoints/`,
  `downloads/`, `results/`, `tutorbench-responses*/`, `edu-judge-validation/` — an unrelated
  experiment. `edullm-checkpoints` (774 GB) = `liv-kda-sub500m/`, `mixlaw/`, `olmo-370m/`,
  `olmo2-370m-cpt/`, `runpod/`, `token-selection/` — model weights. `edullm-data` (3.52 TB) = the
  frozen published corpus (19 prefixes, checked by DATA-EXEC). `edullm-datasets` (1.79 TB, legacy) =
  15 prefixes, none Nemotron (DATA-EXEC).
- **Non-`edullm` buckets** (`mcat-dev-*`, `austin-speedrun-*`, `lsatspeedrun-*`, `memorysplit-*`,
  `hermes-sffs-media`, `danielle-media`, `sina-reels-media`, `amas-alex-photo`, `gt-*`, `zappi-state`,
  `castlebreak-*`, `cdk-*`, `aws-sam-cli-*`, `theo-training`) are other products/people's; none is
  large enough or plausibly holds a math pretraining corpus. `cdk-…-assets` is 19 MB.

### A.3.3 Where the "107+ GiB is already staged" claim came from — the likely origin

`IMPLEMENTATION-PLAN.md` §3.2 is titled **"Proposed change: stage the sources to S3 once"** and §3
opens with **"The proposal."** The `_src/` prefix is a **plan**, not a fact — and §9 Phase 1 line
1788 lists *"Stage ~4.21 TB to `s3://edullm-landing/_src/`"* as **work still to do, 0.5 h**.
`s3://edullm-landing/_src/` **does not exist** (DATA-EXEC, `s3 ls` exit 1 — I did not re-run it).

**So `TASKS.md:58` appears to have read a *planned* staging step as a *completed* one.** This is the
same class of error CLAUDE.md warns about — reading a configuration/intent as evidence of reality.

### A.3.4 🔴 SCHEDULE CONSEQUENCE — #17 IS NOT "1 JOB", AND IT IS NOT UNBLOCKED

#17 must be re-costed as **HF→S3 stage (~470 GB) + scan**, not "1 job against staged bytes":

| step | cost | grade |
|---|---|---|
| stage 472,213,218,716 B (~472 GB / 440 GiB) HF→S3 | **not free, and not measured.** §3.2 warns HF CDN throughput may be **~8.4 MB/s** (reconciled from the reservoir's 8 h build), which for 472 GB is **~15.6 h single-stream**. At a more optimistic 100 MB/s it is **1.3 h**. Parallel children compress this, but the **HF-side rate is the unmeasured risk**, and it is exactly the number §3.2 calls "the strongest argument for staging" | DERIVED, wide bar |
| ingress cost | **$0** (AWS does not charge ingress) | CARD (§3.2) |
| storage | ~$11/month at $0.023/GB-mo for 472 GB | DERIVED |
| then the scan | see A.4 | DERIVED |

**The honest statement for the CEO: #17 needs a staging job that nobody has scheduled, and its
duration is governed by an HF download rate this project has never measured (the range spans 1.3 h to
15.6 h). "1 job" understates it by at least one job and possibly by a working day.**

---

## A.4 ⭐⭐ THE SCAN COST — and a **16×** disagreement with this project's headline anchor

### A.4.1 The scan of Nemotron-CC-Math, costed from the MEASURED rate

Arithmetic shown in full.

**Step 1 — bytes → words.** Nemotron-CC-Math = **472,213,218,716** uncompressed text bytes
(MEASURED, given). From my benchmark corpus: 929,487 chars / 140,307 whitespace-words = **6.62
bytes/whitespace-word**, and `_words()` yields 1.051 words per whitespace-word (252,227 / 240,000),
so **6.304 bytes per `_words()` word**.

```
472,213,218,716 B ÷ 6.304 B/word = 74,912,534,656 words ≈ 7.49e10
```

**Step 2 — words → windows.** Windows = `len(words) − 13 + 1` per document. Documents average ~630
words, so the −12 is a 1.9% correction; windows ≈ words ≈ **7.49e10**.

**Step 3 — windows → core-hours** at the MEASURED **1,174,020 windows/s/core**:

```
7.49e10 ÷ 1,174,020 = 63,809 core-seconds = 17.7 CORE-HOURS
```

**Step 4 — wall-clock.** The filter is serial Python holding the GIL, so the honest ceiling is the
project's own **~6.2× parallelism cap**, i.e. ~62 effective vCPU of the 384:

| | wall-clock |
|---|---|
| at 384 vCPU (if it parallelised perfectly — **it does not**) | **0.05 h** (3 min) |
| **at the 6.2× effective cap (~62 vCPU)** | **0.29 h ≈ 18 min** |
| on ONE core | 17.7 h |

**So the scan itself is ~18 minutes of compute, not a schedule item.** ⚠️ **But #17's cost is
dominated by the staging that A.3 shows has not happened** (1.3–15.6 h). **The scan is cheap; getting
the bytes there is not.** That inversion is the finding.

### A.4.2 🔴 My measured rate is **16× faster** than the project's 78%-of-72,615 anchor implies

This is a direct, load-bearing conflict and I am flagging it rather than quietly picking a side.

The standing anchor: **72,615 tok/s/vCPU end-to-end MEASURED**, of which **~78% is the serial Python
13-gram decon filter**. Taken literally:

```
decon alone = 72,615 ÷ 0.78 = 93,096 tok/s/vCPU
```

My measurement, converted to tokens using the plan's *own* internal anchors (`wallclock-audit.md:754`
uses ~627 windows/doc; `IMPLEMENTATION-PLAN.md:374` gives 814.9 tok/doc → **0.769 words/token**):

```
1,174,020 windows/s/core ÷ 0.769 = 1,525,800 tok/s/core for decon alone
1,525,800 ÷ 93,096 = 16.4x FASTER
```

**Hardware cannot explain this.** Even crediting an M2 Pro performance core as **3× a c7i vCPU**
(implausibly generous — c7i is Sapphire Rapids at 3.2 GHz, and this is single-threaded scalar Python
in both cases), the gap is still **5.5×**:

| M2 Pro core vs c7i vCPU | remaining gap |
|---|---|
| 1.0× | 16.4× |
| 1.5× | 10.9× |
| 2.0× | 8.2× |
| 3.0× | 5.5× |

**What this implies if my number is right:** decon is **~4.8%** of the 72,615 tok/s/vCPU end-to-end
budget (9.5% if an M2 Pro core is worth 2 c7i vCPUs) — **not 78%**.

**⚠️ I am NOT asserting the 78% is wrong.** Three ways it can still be right, none of which I can
settle read-only:

1. **The 78% may cover more than `contains()`.** `dedup_and_decontaminate` (`corpus_filter.py:286+`)
   also does a **sha256 per document** and maintains `SeenHashes` — and the docstring at
   `corpus_filter.py:232` notes `stackv2-edu--train` at ~120M documents wants **18.6 GB inside a 20
   GiB container** for its dedup set alone. **A near-OOM 18.6 GB Python set is a completely different
   performance regime from my 320 MB index** — that, not blake2b, could be the real 78%.
2. **My corpus is clean prose that matches almost nothing** (0 removals). `contains()` early-returns
   on the *second* hit — so on **contaminated** documents it is *faster*, not slower. Cache behaviour
   against a 3.1M-entry frozenset could differ on real web text with different word distributions,
   but not by 16×.
3. **The 78% may have been derived, not measured**, from the same "193 billion blake2b calls" framing
   the plan itself flags as **never measured**.

**Recommendation to DATA-EXEC/CEO: this is worth one 20-minute smoke job to settle**, because the
whole "the filter is 78% and caps parallelism at 6.2×" architecture conclusion rests on it. Note
`IMPLEMENTATION-PLAN.md` §3.3 already says *"Worth measuring in the same smoke job."* **If decon is
really ~5% rather than 78%, the 6.2× parallelism ceiling — and therefore the 15.5 h critical path —
is wrong in the project's favour.** I flag it; I do not claim it.

### A.4.3 The plan's "193 billion blake2b calls" — recomputed, and it is for the whole build

`IMPLEMENTATION-PLAN.md:267`, `:1689`, `BUILD-DEPENDENCY-GRAPH.md:546`,
`orchestrator-findings.md:532` all cite **~193e9**. `wallclock-audit.md:754` shows the derivation:
**~627 windows/doc × 308M documents**. **That is the WHOLE ~1.0T build, not Nemotron-CC-Math.**

⚠️ **Denominator check** — Nemotron-CC-Math is 134.0B of ~1.0T, so its *pro-rata* share would be
`193e9 × 0.134 = 2.59e10` windows, but I derive **7.49e10** from its actual bytes — **2.9× more**.
Why: 193e9 uses **627 windows/doc at 814.9 tok/doc = 0.769 words/tok**, whereas Nemotron-CC-Math's
own MEASURED **0.283686 tok/byte** with 6.304 B/word implies **0.556 tok/word → 1.80 words/token**.
Math/LaTeX text tokenizes into **more tokens per word** than general web text, so per *token* it has
**fewer** words — no wait: 1.80 words/token means **more words per token**, i.e. `\w+` splits LaTeX
(`\neq`, `\nabla`, subscripts) into many short words that the dolma2 BPE packs efficiently. **Either
way the scan is windows-bound, and windows scale with WORDS, not tokens** — so costing this corpus
off a token share understates it 2.9×. **Use the byte-derived figure.**

**Whole-build scan at my measured rate:** `193e9 ÷ 1,174,020 = 45.7 core-hours` → **0.74 h** at the
6.2× effective cap, **0.12 h** at a full 384 vCPU. Against a 15.5 h critical path that is **not the
hidden CPU consumer §3.3 feared** — *if* A.4.2 resolves in my favour.

---

## B.3 ✅ THE PINNED `ai2-olmo` CHECKOUT IS PUBLICLY REACHABLE — B5's last input is confirmed

`build_olmo_ladder_bundle()` (`eval_bundle.py:218-227`) takes an `olmo_checkout` path and **hard-asserts
the revision**:

```python
if head != AI2_OLMO_REVISION:
    raise ValueError(f"ai2-olmo checkout is {head}, expected {AI2_OLMO_REVISION}")
```

`AI2_OLMO_REVISION = "6c3373fa182af2d57fe3c390ffc8420d5c5b325a"` (`eval_bundle.py:21`). So the rebuild
**cannot proceed without that exact checkout** — it is not a soft dependency.

**Verified live, 2026-08-08 (MEASURED):**

| check | result |
|---|---|
| `allenai/OLMo` commit `6c3373fa…` via GitHub API | **HTTP 200** — exists, public, no auth |
| what it is | committed **2024-12-17**, *"Bump version to v0.6.0 for release"* |
| `olmo_data/oe_eval_tasks` @ that ref | **HTTP 200, 12 entries** |
| `olmo_data/hf_datasets/hails/mmlu_no_train` @ that ref | **HTTP 200, 57 entries** = all 57 MMLU subjects ✅ |
| `oe_eval_tasks/arc_challenge/val_rc_5shot/requests.jsonl.gz` | **118,667 bytes** |

⭐ **That 118,667 is byte-identical to `source_files[0].byte_size` in the local manifest.** So the
upstream data the index was built from is **still present, unmoved, at the pinned SHA** — the rebuild
is reproducible against the same bytes, not merely against the same repo.

⚠️ **The checkout is NOT on this machine.** I searched `/Users/ericwu/Developer` — the only OLMo repo
present is `/Users/ericwu/Developer/Capstone_LLM/OLMo-core` (remote `github.com/edu-llm/OLMo-core`,
HEAD `bcc05d66…`), which is **a different repository**: it has no `olmo_data/` directory and
`git cat-file` cannot resolve `6c3373fa…`. **Do not mistake `OLMo-core` for `ai2-olmo`.** A fresh
clone of `allenai/OLMo` is required (one `git clone`, public, no credentials).

### B.3.1 ⛔ Correcting the task premise: **B5 is NOT blocked, and it does not gate FREEZE on access**

DATA-EXEC's brief said: *"If the build script is not reachable, B5's '4 h' is fiction and that gates
FREEZE — a major finding."* **I checked all three inputs and every one is available:**

| B5 input | status |
|---|---|
| build script `eval_bundle.py` | ✅ **on this machine**, `…/pipelines/week1_corpus/src/week1_corpus/eval_bundle.py` |
| its package + pinned deps | ✅ `week1_corpus` with `uv.lock` (398 KB) |
| pinned `ai2-olmo` @ `6c3373fa…` | ✅ **public on GitHub, HTTP 200, data paths intact, byte-sizes match** |
| GSM8K @ pinned revisions | pinned as `740312ad…` / parquet `199530e9…` — on HF, **not verified by me** (UNVERIFIED) |
| the existing index to diff against | ✅ local + S3, both hash-verified (B.2/B.2a) |

**So the "4 h" is not fiction for lack of a script.** It is a real, runnable, local CPU job.
**This removes a claimed FREEZE blocker.**

## B.4 The rebuild's real cost and output — DERIVED, with the two denominators kept apart

§6.2's fix: rebuild from **raw benchmark fields** (bare question; question + each choice; question +
correct answer) **in addition to** the rendered 5-shot form.

⚠️ **The two percentages have DIFFERENT DENOMINATORS. They are not one figure.** Preserving §6.2's
own table:

| grows | against what denominator | value |
|---|---|---|
| n-gram **count** | +1.1M new on **3,097,372 existing n-grams** | **+36%** |
| **resident bytes** | +18 MB on the **~250 MB (measured: 320 MB) resident index** | **+7%** |

They differ because **the exact-hash half does not grow in bytes proportionally** — n-grams are 16 B
each while exact hashes are 32 B, and only the n-gram set gains entries. Stating "+36% and +18 MB" as
a single claim (as an earlier draft did) is wrong; **+36% is a count, +7% is bytes.**

**Recomputed against my MEASURED numbers:**
- +1.1M n-grams × 16 B = **+17.6 MB** on the 54,350,848 B **on-disk** artifact = **+32% on disk**
  (a *third* denominator — disk 54 MB, not resident 320 MB). So: **+36% count / +32% on-disk bytes /
  +7% resident**. Three numbers, three denominators, all correct.
- Resident goes **320 MB → ~338 MB** (using the MEASURED 320 MB, not the documented 250 MB).

**Wall-clock for B5, DERIVED from measured components:**

| step | cost | basis |
|---|---|---|
| `git clone allenai/OLMo` @ pinned SHA | ~2–10 min | UNVERIFIED (repo size not measured) |
| read 184 source files, hash each | minutes | files are small (118 KB–460 KB each) |
| render + hash ~150k items × ~21 windows + ~1.1M new | **seconds of CPU** | at my MEASURED 1,174,020 windows/s/core, 4.2M windows = **3.6 s** |
| pack + write the `.bin` | seconds | 54 MB |
| **total compute** | **well under 1 h** | |

**So "4 h" is a safe envelope dominated by human/setup time, not compute.** The compute is minutes.
**The honest critical-path number for B5 is ~1 h wall-clock, with the 4 h as slack** — and it is
**local CPU work that needs no AWS Batch job at all**, so it can run fully in parallel with the
staging and build waves. **It should not sit on the critical path.**

⚠️ **The one real risk is not time, it is sequencing.** Changing the index changes **which documents
get dropped**. §6.2a and `corpus_filter.py:44` both stress that the normalization/index identity **is**
the dedup identity. So B5 must land **before** the build wave that consumes it, or the corpus is built
against an index that no longer exists. That, not the 4 h, is why it gates.

## B.5 The residual that NOTHING here fixes — rephrasing

Recorded as required, and unchanged by any rebuild:

- **n-gram decontamination is defeated by rephrasing by construction.** Our own measurement:
  13-gram detection scores **F1 0.926 on verbatim text and 0.000 on rephrased text** (§6.4).
- **FinePhrase is rephrased FineWeb-Edu, and FineWeb-Edu does zero upstream decontamination**
  (`corpus_filter.py:13-16` says so in the module docstring). A benchmark item that leaked into
  FineWeb-Edu survives in its FinePhrase rewrite **with zero shared 13-grams**.
- **Rebuilding the index does not touch this.** B5 restores the *exact-hash* half and adds raw-field
  n-grams; both are verbatim-matching mechanisms. **Against paraphrase, both score 0.000.**
- The eval bundle also **does not cover** MATH, HumanEval, MBPP, DROP, TriviaQA, BBH, MMLU-Pro, GPQA
  (§6.3) — so any of those reported by the program has **no contamination defense at all**, while the
  build still prints a healthy `DECON index …` line.
- **The honest action is disclosure in `limitations[]`**, per §6.4. There is no affordable technical
  fix at 1.23B documents.

⚠️ **Do not conflate:** the **−11.8 MMLU** figure is a **DEDUP** result (DCLM v4 Table 19, `min_ngram`
5 → MMLU 32.5 vs 13 → 44.3), **not a decontamination result**, and those two rows also differ in shard
count so it is not a clean single-variable ablation.


---

## Appendix: constraints honoured, and tooling notes

**Read-only discipline.** Nothing was written to `s3://edullm-landing` or any bucket. No Batch job was
submitted. Every AWS call was `list-buckets`, `list-objects-v2`, `s3 ls`, `head-object`, or
`cloudwatch get-metric-data`. **No `manifest.json` was created anywhere**, so the auto-promotion
EventBridge rule was never at risk.

**Bulk-listing note.** I did **not** need the `sb-aws-creds` credential_process + threaded boto3 path,
and therefore did not touch the permission classifier for it. `cloudwatch get-metric-data` answered
the "which buckets could even hold 470 GB" question for **all 44 buckets in one call**, and
`list-objects-v2` with a server-side JMESPath `sum(Contents[].Size)` returned exact prefix totals
without transferring object lists. That is strictly cheaper than threaded listing and avoided the
approval step entirely.

**Blocked / failed calls, recorded as instructed:**

| call | outcome |
|---|---|
| `cat ~/.aws/config` (checking for a credential_process profile) | **DENIED by the auto-mode classifier.** Routed around — the CloudWatch approach above made it unnecessary. Not retried. |
| a `python3` arithmetic one-liner (final bucket reconciliation) | **`claude-sonnet-5[1m] temporarily unavailable`** — the known **OUTAGE**, not a denial, exactly as the brief warned. I did the arithmetic in the following heredoc instead; no finding was lost. |

**Micro-benchmark reproducibility.** Scripts written to `/tmp/bench_decon.py`, `/tmp/bench2.py`,
`/tmp/bench3.py`, `/tmp/bench4.py`, `/tmp/bench_final.py` and `/tmp/crc64.py` (CRC-64/NVME, reflected
poly `0x9A6C9329AC4BC9B5`). `/tmp/bench_final.py` is the authoritative one — CPU-time based, 15
repetitions, 40-doc warmup. Machine: Apple M2 Pro, 10 cores (6 performance), CPython 3.11.9.

**Not verified (stated so rather than assumed):**
- GSM8K at pinned revisions `740312ad…` / `199530e9…` on HuggingFace — I verified the `ai2-olmo`
  side only.
- The `allenai/OLMo` clone size, hence the 2–10 min clone estimate.
- Whether the 78% decon-share anchor covers `SeenHashes`/sha256 as well as `contains()` — this is the
  crux of A.4.2 and is **not settleable read-only**.
- I did not re-run DATA-EXEC's `s3://edullm-landing/_src/` check; I rely on their exit-code-1 result.
