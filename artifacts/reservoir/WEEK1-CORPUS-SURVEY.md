# What to take from `pipelines/week1_corpus`, and two claims about it that are wrong

Surveyed 2026-08-01, read-only. The sibling checkout at
`/Users/ericwu/Developer/Capstone_LLM/pipelines/week1_corpus` is 12,013 lines across 27 modules,
and `HANDOFF.md` describes it as holding working tokenize/pack/val-carve/dedup/decontam code with
"a complete, already-exercised S3 backend." Most of that is right. Two parts are not, and both
would cost real money if believed.

## 🛑 Correction 1: the packer named in HANDOFF is the WRONG one

`HANDOFF.md` cites `packing.py` approvingly, for `np.memmap(dtype=np.uint32)`. The bytes are
indeed correct — but `pack_category_partition` (`packing.py:68`) **silently discards its tail
remainder**:

```python
109:    dropped_remainder_tokens=len(buffer)
```

Nothing in the training path ever reads that field. Grepping `src/` and `tests/` for
`dropped_remainder_tokens` returns hits only in `validation_runtime.py:1099,2350` and one
assertion in `tests/test_validation.py:438` — i.e. only the *validation* program looks at it.

The scale of the loss comes from how it is called. `coordinator.py:167-181` creates one pack task
per **(hash-bucket, category)** pair, with 256 buckets (`tokenization.py:95`) × 8 categories = up
to **2,048 independent packers per tier**, each holding its own buffer and each dropping up to
`sequence_length - 1` tokens. Worst case ≈ **8.4 M tokens lost per tier, invisibly.** The tier
audit at `coordinator.py:233` does not catch it: it checks
`blocks × sequence_length == aligned_tokens`, which holds regardless of how much was discarded
upstream, and the shortfall is absorbed by over-provisioning the inventory 1.10× (`cli.py:721`).

**This is disqualifying for an exact-shard corpus** — 25,001,984 tokens per shard is not
reachable by a packer that drops an arbitrary remainder per partition.

**The two packers worth copying are elsewhere in the same repo:**

| where | what it does right |
|---|---|
| `validation.py:846` `pack_category_globally` | Computes the exact aligned size up front (`packed_tokens = token_plus_eos - token_plus_eos % sequence_length`), then splits mid-document: `count = min(len(values), packed_tokens - cursor)` (`:888`). One bounded remainder for the whole category, not one per partition. |
| `quick_validation.py:718` `_pack_category` | Pre-allocates the exact size, truncates mid-document, records per-document `boundary_truncated` provenance (`:756`), and **hard-fails if `cursor != quota`** (`:759-760`). |

⚠️ **BUT DO NOT COPY THE FIRST ONE LITERALLY — it cannot stream, and an earlier version of this
section recommended it without noticing.** `token_plus_eos = sum(len(item.tokens) + 1 for item in
ordered)` (`validation.py:864`) sums over a materialized list, so computing the aligned size up front
requires holding every document of a category in memory. At 255B tokens that is not available, and
adopting the shape as written would have forced a full pre-pass over the corpus before a single shard
could be written.

`corpus_pack.py` streams instead and truncates at exhaustion. **Verified equivalent, not assumed:**
across 5 randomized trials the streaming packer's `tokens_out` equalled
`total - (total % SEQ_LEN)` — what the materializing algorithm would have produced — in every case.
So the pre-pass buys nothing except the memory it costs.

What *is* worth taking is `quick_validation.py`'s `cursor != quota → raise`. `corpus_pack.py`
generalises it into the conservation identity `tokens_in == tokens_out + tail_dropped +
surplus_dropped`, which is strictly stronger: it also accounts for the tail and the surplus rather
than only detecting a shortfall. It is asserted at runtime in `PackResult.__post_init__`, not only in
tests, and it caught a real double-counting bug on its first run.

## 🛑 Correction 2: the S3 backend is neither complete nor exercised

`artifacts.py:173-283` `S3ArtifactStore` is 111 lines behind an `ArtifactStore` ABC. Three
problems:

1. **No multipart.** `put_file` is a single `put_object` (`:252`), hard-capped at 5 GB —
   `farmshare/upload-validation-to-s3.py:449` explicitly *refuses* objects ≥ 5 GiB. Our
   `s3.py:191-243` has `_multipart_copy` with `UploadPartCopy` and abort-on-failure. Relevant
   because the S3 audit found 8 of the 15 largest objects exceeded 5 GB.
2. **Untested.** Grepping `tests/` for `boto3`, `moto`, `S3ArtifactStore`: **zero hits.** All 16
   test files exercise `LocalArtifactStore` only.
3. **Never ran.** Its only callers are `week1-corpus worker` / `coordinator` / `mark-smoke-gate`
   (`worker.py:270`, `cli.py:290,540`), and no `farmshare/*.sbatch` or `*.sh` invokes any of them
   — everything that ran used `farmshare-worker` → `LocalArtifactStore` + `FileTaskQueue`. The
   96-object release on `s3://edullm-datasets/datamix1-jul22/` was written by
   `farmshare/upload-validation-to-s3.py`, a standalone 639-line script with its own inline boto3
   that never imports `artifacts.py`.

So "port the S3 layer" is really "cherry-pick two behaviours." Both are genuine and both are
things `s3.py` lacks:

- **`IfNoneMatch="*"` conditional create**, with 412 → compare-existing-body → decide
  conflict-vs-idempotent-retry (`artifacts.py:205-217`). A real idempotent-write primitive.
- **Client-side SHA-256 sent as `ChecksumSHA256` and verified against the value S3 echoes back**
  (`:238-267`). End-to-end upload integrity in ~8 lines. Note this is exactly the kind of
  recompute the golden rule asks for, on the write path.

## The decontamination bundle: reusable, and verified authentic

`decontamination.py` is 125 lines with one dependency (`records.normalize_text`), and the built
index **already exists** — §4.2's "our pipeline has none" is a gap that can close today rather
than in a porting exercise.

Verified 2026-08-01 by recompute, not by reading the manifest:

```
local sha256 04aa8fe5c87f438a648c74d2c97197411ab8448ef01b61cd3efc0a556750bfd7
manifest     04aa8fe5c87f438a648c74d2c97197411ab8448ef01b61cd3efc0a556750bfd7   <- equal
header       struct '<8sIIQQ' -> W1DCI001, ngram 13, min_hits 2,
                                 exact 149,777, ngrams 3,097,372
size         32 + 149777*32 + 3097372*16 == 54,350,848 == actual file size
```

Contents: 9 OE-eval families × `_rc_5shot`; MMLU all 57 subjects × {dev, validation, test},
rendered with the real 5-shot prompt template including demonstrations
(`eval_bundle.py:145-171`); GSM8K main/test. Counts are asserted against a pinned `ai2-olmo`
checkout (`6c3373fa182af2d57fe3c390ffc8420d5c5b325a`), so a drifted checkout fails loudly
(`eval_bundle.py:251-262`).

⚠️ **It was on the laptop only.** The `datamix1-jul22/validation/audits/` prefix holds the
*manifest* describing the `.bin`, not the `.bin` — a full recursive listing confirms it is absent.
Now copied to **`s3://edullm-landing/_dist/eval-decontamination.bin`**, which is the right place
because `_dist/` carries **no expiry rule** (the live lifecycle covers `pretrain/ curriculum/
sft/ eval/ probe/ vendor/ _pending/` at 14 d and `_ingest/` at 30 d, nothing else).

Two limits before leaning on it: the matching is **word-level 13-gram, casefolded**, which is
precisely what rephrasing defeats — so it does nothing for the FinePhrase half (§4.2 says the same
and it remains true) — and it holds two `frozenset`s resident, **~250 MB**, which needs a line in
any Batch memory budget.

## The traps worth stealing, with citations

Nine pieces of non-obvious correctness, ~111 lines total; the irreplaceable core is ~75. Ranked
by what breaks without them.

**1. Compression sniffed from magic bytes, not the filename** — `records.py:72-89`, plus the
load-bearing `response.raw.decode_content = False` at `records.py:50`. The dolmino `math` prefix
mixes `*.jsonl`, `*.jsonl.gz`, `*.json.gz` and `*.json.zst` in one directory
(`config/corpus.yaml:52-56`) — upstream filenames lie. Suffix dispatch throws `BadGzipFile`
mid-stream, hours in, on a subset of shards. `peek()` is non-consuming so the sniff is free. And
without `decode_content = False`, urllib3 transparently applies `Content-Encoding`, so the magic
bytes reflect the *transport* encoding rather than the file's — same file, different answer
depending on the CDN.

**2. Resume that enumerates all three states** — `source_cache.py:43-50, 60-61`. A `.part` at
exactly the expected size is renamed, not re-fetched; a `.part` *larger* than expected is deleted
rather than trusted; and `append = offset > 0 and response.status_code == 206` is the critical
line — **if you send `Range` and the server answers `200`, appending gives you
`prefix + whole_file`.** Opening `"ab"` unconditionally is the natural thing to write and it is
wrong.

**3. Length-prefixed digest composition** — `determinism.py:9-16`. Each part is preceded by its
8-byte length, so `("ab","c")` and `("a","bc")` cannot collide. Without it, two distinct documents
can hash to the same val/train bucket deterministically-wrongly, and both answers look valid.

**4. The 13-gram window off-by-one and the 2-hit minimum** — `decontamination.py:115-125`.
`range(max(0, len(words) - ngram_size + 1))`: a document of exactly 13 words must yield **one**
window. The natural typo `range(len(words) - 13)` silently skips the last window of every
document, so a benchmark question at the *end* of a document is never caught. And `min_hits=2` is
not a rounding detail — single-hit matching at 13-gram granularity false-positives on boilerplate
and discards real training data.

**5. Normalization is fixed before hashing, and versioned** — `records.py:30-32`, pinned as
`NORMALIZATION_VERSION = "week1-nfc-rstrip-v1"` (`validation.py:41`). `exact_content_hash` hashes
the *normalized* text, so the whole dedup identity depends on this function: get it wrong and
CRLF-vs-LF copies both survive, NFC-vs-NFD variants both survive, and a bare `\x00` reaches a
fast tokenizer that may truncate at it and silently drop the document tail. `.rstrip()` not
`.strip()` — leading whitespace is semantic in code. The version string is the real lesson: this
is a compatibility surface, and changing it invalidates every hash ever computed.

**6. Resumability checks OUTPUTS, not the commit marker** — `task_runtime.py:42-48`. A worker that
writes `commit.json` and is preempted before uploading its objects makes every later run declare
the task done and proceed with **missing shards**; you find out at training time. Paired with
`LocalArtifactStore.put_bytes` (`artifacts.py:105-124`): tempfile → `fsync` → `os.link`, so a
partial file cannot appear at the final path even under `KeyboardInterrupt`.

**7. The schedule is computable at any offset** — `determinism.py:70-74`, with the cycle reduced
by `math.gcd` (`:40`) so any window is a modular index rather than a materialised list. This is
what makes shard *N* independently and idempotently rebuildable. Take it if we ever interleave
weighted sources.

**8. Composite ordinal packs file+row into one sortable int** — `records.py:168`,
`(file_ordinal << 40) | row_index`. Dedup winner selection (`reduction.py:51`) needs a global
order. ⚠️ There is **no overflow assertion**; if copied, add one — silent aliasing of two
documents into one ordinal is the failure.

**9. `mkdir -p` must trust EEXIST over a following stat** — `artifacts.py:29-46`. On NFS/Lustre
with N workers racing, `Path.mkdir(parents=True, exist_ok=True)` intermittently raises
`FileNotFoundError` on an intermediate component because another client just created the parent
and the attribute cache is stale. Rare enough to look like a flake, common enough to kill a 12-hour
job. **Moot for pure S3** — flagged because it is the least obvious thing in the repo.

## Verdicts

| component | verdict | why |
|---|---|---|
| reader `records.py` | **STEAL** (~90-line core) | Traps 1, 5, 8 are hard-won against upstream files that lie about their format. Drop the HF-URL half — our `ingest_reservoir._RangeFile` is more advanced. |
| tokenizer `tokenization.py` | **STEAL THE IDEA** | The encode call is 9 obvious lines. Worth taking: the length-prefixed snapshot fingerprint checked against a pin (`:67-79`), the vocab/EOS/PAD triple-assert (`:81-90`), and **not appending EOS at tokenize time** so re-packing at a different `seq_len` costs nothing. |
| packer `packing.py` | **IGNORE** | Correction 1. Use the `validation.py:846` / `quick_validation.py:718` shape. |
| val carve `determinism.py:23` | **STEAL** (6 lines) | Correct by construction: document-level, pre-tokenization, keyed on a pinned-revision id, independent of ordering and worker count. Take `stable_digest` with it. ⚠️ One gap: `reduction.py:51` picks the surviving duplicate by `(ordinal, document_id)` **ignoring the validation flag**, so dedup can move a document across the split. `corpus.is_held_out` keys on `(source, doc_id)` only, so it does not inherit this. |
| dedup `reduction.py` | **IGNORE** | 88 lines, obvious algorithm, and its 256-partition architecture serves a nested tier structure we do not have. |
| decontam | **STEAL, and reuse the built artifact** | Above. |
| s3 `artifacts.py` | **IGNORE the class, port two behaviours** | Correction 2. |

One more, unrelated to any verdict: **`TOKENIZERS_PARALLELISM` is set nowhere in that repo**
(grepped `.py`, `.sbatch`, `.sh`, `Dockerfile`) while it forks workers via `multiprocessing`
(`worker.py:304`, `autotune.py:50`). Set it explicitly or HF warns and can deadlock.
