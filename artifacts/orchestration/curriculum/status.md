# CURRICULUM-EXEC — status

Opened 2026-08-09. Task: implement MTLD curriculum labeling for `pretrain/edu-mix-983b`.

## Reading log
- LEDGER.md last three sections read (MTLD workstream assessment @3534, owner decision @3587,
  FINDING 1 `plan_id` @3593, FINDING 2 labels-not-text @3608, RULING @3622).
- `token_order_v1.py` read. Permutation recompute is at `check_order_domain` — `np.bincount(order,
  minlength=n) == 1` (the executable line is **:209-210**, not `:138`; `:138` is inside the
  docstring. Correcting the brief's citation in place: the behaviour is real, the line number is a
  docstring line).

## Status
- [ ] MTLD implementation
- [ ] labels sidecar
- [ ] plan_id assertion test
- [ ] order-vector builder
- [ ] publish surface

## 🔴 FIVE STRUCTURAL FINDINGS BEFORE ANY CODE (all recomputed, none inherited)

### F-C1 `plan_id` REPRODUCES — `29968a2b04008a8c`. MEASURED.
```
load_registry('artifacts/final-dataset/corpus-registry.json') -> 133 rows
plan_document(drawn, registry_meta=meta)['plan_id'] == 29968a2b04008a8c
185 bundles / 39,307 shards / 982,752,985,088 tokens
train 144 bundles / 39,205 shards / 980,202,782,720 tokens
val    41 bundles /    102 shards /   2,550,202,368 tokens
```

### F-C2 🔴 `max_order_bytes` DEFAULT REFUSES OUR ORDER VECTOR. The brief did not name this.
`token_order_v1._DEFAULT_MAX_ORDER_BYTES = 512*1024*1024 = 536,870,912`.
Our train vector is **1,914,301,740 B = 3.57x the cap** -> `check_order_domain` returns
`order-too-large` and REFUSES TO LOAD IT. Gate A fails on the artifact it was built to check.
`max_order_bytes` MUST be declared on the group. MEASURED-IN-CODE (`token_order_v1.py:158,168-177`).

### F-C3 🔴 RAISING THE CAP RE-OPENS THE OOM IT GUARDS. Validator is 8,192 MiB.
`np.bincount(order, minlength=n)` returns **int64** (measured), so at n=478,575,435:
```
s3.get() bytes      1.91 GB
np.frombuffer view  0     (a view, not a copy)
bincount int64      3.83 GB
peak               ~5.75 GB   in a 8,192 MiB container  (rev 14: vcpus 4 / memory 8192)
```
~2.2 GB headroom, but `s3.get` returns bytes that stay referenced by the frombuffer view for the
whole check. Tight, not impossible. **This needs a validator memory bump or a chunked check.**

### F-C4 🔴 ONE GROUP CANNOT HOLD BOTH A TRAIN PERMUTATION AND A VAL IDENTITY.
`check_order_domain` reads ONE group-level `ordering` and ONE `block_count`, then applies both to
EVERY manifest entry (`for entry, bad in _entries(ctx)`), and `permutation-wrong-length` fires when
`order.size != block_count`. Two objects of different lengths in one group is a guaranteed failure.

### F-C5 🔴 `source_doc` CONTIGUOUS PER *STREAM* IS UNACHIEVABLE BY A COUNTER.
A bundle is 1/K of a stream (file-sharding: stackv2-edu K=7, finepdfs-edu 4, nemotron-cc-math-3 3,
-4plus 2). K siblings are separate Batch children with no shared counter. Per-stream contiguity is
the ordinal-allocation problem, solved at PLAN time, and a run-time counter cannot solve it.
-> emit per-BUNDLE contiguous `source_doc`; global identity is `(bundle_id, source_doc)`.

### F-C6 ⚠️ `n_chunks` FORMULA CONFLICTS WITH OLMo-core's OWN ARITHMETIC.
brief/handoff: `(shard_tokens - 1) // 2048` -> **12,207** per full shard.
OLMo-core `numpy_dataset.py:679` MEASURED-IN-CODE: `file_size // (item_size * sequence_length)`
= 100,007,936 // 8,192 = **12,208** per full shard, remainder ZERO.
Difference is 1 chunk/shard x 39,205 shards = **39,205 chunks**, and it MISALIGNS EVERY CHUNK INDEX
PAST THE FIRST SHARD. Implemented as specified (12,207) but parameterised and reported.

## Progress
- [x] `corpus_mtld.py` — 6 hand-computed vectors verified (see below)
- [x] `corpus_labels.py` — 14 B/record packed dtype + interned path table
- [x] `on_document` hook in `tokenize_documents`, BEFORE the yield
- [x] `Document.source_path` (compare=False), populated at both reader sites
- [x] `Receipt.labels` + `_check_labels` (re-reads AND re-hashes the sidecar in the CHEAP tier)
- [x] `run_bundle(labels=True)` + `--labels` CLI flag (opt-in)
- [x] baseline restored: **1424 passed, 2 deselected**
- [ ] order-vector builder
- [ ] plan_id assertion test + MTLD tests
- [ ] publish surface

## ⏱️ LABELING OVERHEAD — MEASURED, and it produced a THIRD reproduction of the 16.4x gap

**MTLD cost, MEASURED on this laptop** (300 synthetic documents at the corpus's MEASURED 815 tok/doc
-> 627 words at 0.769 words/token; median of 5-7 reps of `time.process_time`):
```
MTLD per document            314 us     (words_of() is 49% of it — 155 us)
decon contains() per document 475 us
build per document           11,224 us  = 815 tok / 72,615 tok/s/vCPU  (the MEASURED anchor)
```

| measure | value | grade |
|---|---|---|
| **MTLD as a share of build time** | **2.80%** | MEASURED, laptop |
| MTLD / decon `contains()` ratio | **0.662** | MEASURED, laptop, ratio so hardware-independent |
| **added wall clock on a 13.3 h build** | **+0.27 h -> 13.57 h (+2.1%)** | DERIVED from the above |
| total MTLD CPU across 1,206 M docs | 105 core-hours | DERIVED |

**Within the CEO's expected single-digit percent. Confirmed, not assumed.**

### 🔴 But the two routes to that number DISAGREE 18x, and the reason is the 78% anchor
Route A (absolute): MTLD 314 us / build 11,224 us = **2.80%**.
Route B (ratio x the ledger's serial share): 0.662 x 78% = **51.1%** -> +6.8 h.

**Route B is wrong, and it is wrong for the reason the ledger already flagged as OPEN.** My decon
measurement independently reproduces DATA's and PLAT's:
```
decon 615 windows/doc / 474.9 us = 1,294,919 windows/s/core
DATA  1,174,020   (1.103x)      PLAT  1,266,551   (1.022x)
```
**Three analysts, three machines, same rate.** But at that rate decon is **4.23% of the 11,224 us
build budget, not 78%.** That is the ledger's unexplained 16.4x gap, reproduced a third time from a
completely different direction — and it means **the "78% serial" anchor cannot be multiplied by any
measured per-document cost.** Route A uses only MEASURED end-to-end numbers on both sides, so it is
the one I report. **I am NOT claiming the 78% is wrong** (nobody has explained the gap); I am
claiming it is the wrong multiplier for this question, which is the cap-x-rate error class.

⚠️ **UNVERIFIED: the absolute rate on c7i hardware.** The RATIO is hardware-independent; the 2.80%
divides a laptop measurement by a c7i-measured build rate, so it is a mixed denominator. It is
conservative in the direction that matters (a laptop core is *faster* single-threaded than a c7i
vCPU, which would make MTLD's share *smaller* on Batch, not larger) but it has not been run there.

### One optimisation NOT taken, and why
`words_of()` is 49% of MTLD, and `corpus_filter._words()` already tokenizes every document. Sharing
the word list would nearly halve the cost — **and it is refused**: decon uses `\w+` UNICODE over
NFC-normalised text, MTLD uses `[A-Za-z]+(?:'[A-Za-z]+)?|[A-Za-z]*\d+[A-Za-z0-9]*` over raw
lowercase. Different words -> a different score -> incomparable arms. The regex is a compatibility
surface, so 155 us/doc is the price of comparability and it is paid deliberately.

## 🔴 F-C3 CONFIRMED BY MEASUREMENT — Gate A needs ~7.13 GB in an 8.00 GB container

Ran `check_order_domain`'s exact arithmetic at full scale (N = 478,575,435):
```
order vector       (<u4, N)              1.78 GiB
np.bincount(order, minlength=N)  int64   3.57 GiB   (measured dtype, not assumed)
s3.get() bytes kept alive by frombuffer  1.78 GiB   (a VIEW does not copy — it PINS)
                                        ---------
                                         7.13 GiB   against rev 14's 8192 MiB = 8.00 GiB
```
`np.all(counts == 1)` returned True on a real 478 M-element permutation, so **the check works** —
but at **89% of the container**, before the Python interpreter, boto3, and the rest of Gate A's
state. `validate.py` runs this inside a job whose container is `vcpus: 4 / memory: 8192`.

**Three fixes, in preference order, none of which I can apply (PLAT-EXEC-2 owns AWS):**
1. **Register a validator revision with more memory** (16,384 MiB). One `register-job-definition`.
2. **`del` the payload before the bincount** in `check_order_domain` — a two-line change that drops
   the 1.78 GiB pinned bytes and takes the peak to 5.35 GiB. Requires `np.frombuffer(...).copy()`
   or reading via a temp file. Cheap, but it changes a profile every dataset shares.
3. A chunked bijection check (sort-based, or a uint8 bitmap at 0.48 GiB instead of int64's 3.57).
**I have NOT changed `token_order_v1` — it is shared by every curriculum dataset and this is a
platform decision, not a curriculum one. Reporting, not acting.**
