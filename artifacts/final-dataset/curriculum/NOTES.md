# `edu-mix-983b` curriculum ordering — what is here and what it is not

**Not** a published dataset. Not validated by Gate A. Not promoted. Nothing was written to S3 by
the work in this directory; the `_labels/` objects in both buckets are byte-for-byte untouched.

> The file is deliberately named `NOTES.md`, not `README.md`. `README.md` is a **reserved control
> basename** in the eduLLM dataset standard — generated only by `promote()` from `dataset.json` —
> so writing one by hand anywhere near corpus artifacts is exactly the confusion the standard
> exists to prevent.

## Files

| file | what |
|---|---|
| `build_curriculum.py` | the generator. Reads the index, the 182 receipts and the 141 train label objects; writes the three artifacts below. Local files only. |
| `edu-mix-983b-train.u32le.bin` | the permutation over the **train** chunk pool. `uint32` little-endian. **Not committed** (1.68 GB) — its `sha256` is in the spec and the generator is deterministic. |
| `CURRICULUM-SPEC.json` | everything a trainer needs: `block_count`, `max_order_bytes`, dtype, chunk rule, the axis digest, the S3 prefix, both fixes and their measured effect. |
| `CURRICULUM-STATS.json` | before/after MTLD moments, head composition before and after stratification, per-source chunk counts. |

## Why this is a file and not a `token-order/v1` dataset

`token-order/v1` requires `depends_on` naming the **parent's `manifest_sha256`**. The parent corpus
was never published — no `manifest.json`, no `dataset.json`, no hash chain (the mirror's own
`NOTE.txt` says so). `artifacts/final-dataset/curriculum_driver.py` encodes this with
`PARENT_MANIFEST_SHA256 = None` and exits 1 **by design**. That check was not worked around.

The spec therefore carries, explicitly, the things a publish would otherwise have to reconstruct
— the coordinate model, the chunk rule, and a digest of the axis itself. Rebuilding the axis from
a different source (the plan, a prefix listing) is the step that leaves a permutation bijective
and meaningless.

## The two fixes

**1. The runaway upper tail is clamped, not dropped.** `corpus_mtld.mtld_one_direction` returns
`float(len(words))` when no factor closes (`corpus_mtld.py:146-147`) — a document *length*. A drop
is not available: a dropped document's tokens are already packed into shards, so its chunks still
exist in the axis and still need a rank, and `build_order` refuses a chunk with no owning labelled
document. "Drop" would mean a sentinel rank, i.e. a clamp with an undeclared threshold. The clamp
is monotone non-decreasing, so **no pairwise comparison below the threshold changes**.

**2. The global sort is replaced by a within-source percentile rank.** MTLD tracks *register*, not
difficulty, so an ascending global sort opens the curriculum on near-pure code. Ranking each
document against its own source makes the head easy-*for-its-domain*, and every training window
draws from all sources in proportion to their size. This matters because domain-pure micro-batches
suppress MoE expert specialization, and the target is a 96-expert MoE.

## Reading it

The order is over **chunks**, not shards or documents. Every train shard holds exactly 25,001,984
tokens, so the mapping is exact integer arithmetic:

```python
order = np.fromfile("edu-mix-983b-train.u32le.bin", dtype="<u4")
paths = [l.strip() for l in open("../index/paths-train.txt")]   # 36,111, ascending
CHUNKS_PER_SHARD = 12207          # (25_001_984 - 1) // 2048
for c in order:
    shard      = paths[c // CHUNKS_PER_SHARD]
    tok_offset = (c % CHUNKS_PER_SHARD) * 2048
```

Consuming it shard-by-shard rather than chunk-by-chunk discards the curriculum entirely while
still reading every token — and nothing downstream can detect that.

## The chunk-count rule is a decision, and it is reported

This vector uses `(tokens - 1) // 2048` (`corpus_order.CHUNKS_MINUS_ONE`, the handoff's rule).
OLMo-core's `NumpyFSLDataset` uses `tokens // 2048`. Every train shard here is a whole multiple of
2048, so the two differ by one chunk **per shard** — 36,111 chunks total — and every global chunk
index past the first shard shifts. **A trainer on the floor rule must regenerate, not
reinterpret.** Both totals are in the spec.
