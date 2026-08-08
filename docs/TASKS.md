# Task register — `final-dataset`

**Written 2026-08-07.** The `#NN` ids are referenced throughout `IMPLEMENTATION-PLAN.md`,
`BUILD-DEPENDENCY-GRAPH.md` and `HANDOFF-FINAL-DATASET.md`. They previously lived **only in a session
task tool**, so a fresh agent could read `#21` in three documents and find no definition anywhere. This
file is the definition. **It is the crosswalk between all three numbering schemes** — task id, graph node,
and Phase 0 item.

**Nothing here has been submitted to AWS.** Every job needs a platform submission and a human release.

## Wave 0 — the critical-path code

| # | task | graph | Phase 0 | changes the plan? | est |
|---|---|---|---|---|---|
| **#28** | **File-shard the BIG bundles.** `--shard/--of` strides *bundles* (`corpus_build.py:676`), so DCLM's 410B lands in one child = **10.85 h even on a whole 32-vCPU instance**. Needs plan-time ordinal ranges. **The longest item and the new critical path** | **C3b** | **3b** | **YES** | 1–2 d |
| #22 | **Replace the dedup set with a flat `np.uint64` pre-pass.** The `set` needs **27.92 GB** for DCLM in a 15.03 GB container (§5.2a) | A2a + A2b | 4 | no | 1 d |
| #21 | **Wire the FinePhrase id partition** into `_reader_for`. Cannot be retrofitted after tokenization. **Ships with the budget correction or not at all** | C1 | 1 | **YES** | ~5 lines |
| #9 | **Decide `SHARD_TOKENS`.** Code says 25,001,984; the report now agrees. Confirm and stop carrying two values | B6 | 11 | **YES** | 1 h |
| #24 | **Rebuild the decon index from raw benchmark fields**, not 5-shot renders. Gates `FREEZE` | B5 | 12 | no | 4 h |

## Wave 0 — parallel, distinct files, no coordination

| # | task | graph | Phase 0 | est |
|---|---|---|---|---|
| #23 | **Pin `tokenizers`** in `pyproject.toml`. It is imported at `corpus_build.py:631` and declared nowhere | B1 | 5 | 1 line |
| #10 | **Thread the Gate A profile checks + raise `max_pool_connections`.** ~100 lines, not the ~20 an earlier draft said | B3 | 6 | 1 d |
| #16 | **Drop `data_provenance_initiative`** — ships GSM8K in Flan CoT format at 6 repeats. 0.51% of tokens | B4 | 9 | registry edit |
| — | **Fix the boundary-marker prefix guard** in `corpus_pack.py`, and the test asserting the table length is 1 | B2 | 8 | ~5 lines |
| — | **Record `FilterStats` in the receipt.** Until then no dedup claim is auditable | — | 7 | ~10 lines |
| #11 | **Query the live validator timeout.** `edullm-validator:12`'s is recorded nowhere. Read-only | — | 10 | 1 call |

## Wave 0 — measurements

| # | task | graph | est |
|---|---|---|---|
| **#26** | **Measure in-region S3 + HF CDN bandwidth. RUN THIS FIRST** — every read estimate borrows ~85 MB/s from an S3 measurement, and one reconciliation implies the CDN is ~8.4 MB/s | M1 | 10 min |
| #14 | **Dolma 3 adult-content prevalence**, sampled at **random offsets** — a prior attempt could not separate signal from HuggingFace preview ordering. **Blocking for that source** | M2 | 1 h |
| #17 | ~~Nemotron-CC-Math's real dolma2 count~~ ✅ **DONE 2026-08-07** — **134.0B** (472,213,218,716 bytes × 0.283686 tok/byte, 1,920 random-offset docs, seed 42; `3` ≈ 83.6B + `4plus` ≈ 50.4B). Measured by a teammate with gate access | M3 | done |
| — | **Mean doc length for 5 unmeasured stage-2 sources.** The dolma3 QA source is the one plausibly near the 20-token EOS floor | M4 | 1 h |

## Deferrable — do NOT put in the first image

| # | task | graph | why deferred |
|---|---|---|---|
| #25 | **File-shard the VAL bundles.** 43% of bytes moved serves 0.39% of tokens. **NOT the same item as #28** — this one is a read-volume saving, #28 is a wall-clock prerequisite | C3 | ∞ slack; contends with #21 on `_reader_for` |
| — | **Wire `bytes_fetched`** through from `corpus_read` to the build | C11 | instrumentation only |

## Then, in order

| # | task | note |
|---|---|---|
| #20 | **Freeze the full two-stage plan.** THE REAL GATE. Its duration is a decision, not a computation | after every "changes the plan" item lands |
| #6 | **Ingest.** Stage → smoke test → build in waves → publish two datasets → Gate A → `verify --deep` → promote | |
| #1 | Register `reservoir-dolma2` with the platform | independent of this corpus |

## Downstream of the corpus — training-side, not build-side

| # | task |
|---|---|
| #19 | **Fix `MoELoadBalancingLossGranularity`** before any training run. Cannot be annealed in — switching at 10% recovers ~55% |
| #7 | Run probe-fitted mixture selection before the real run |
| #18 | Use BPB, not accuracy, for any ablation at this scale — MCF is random below ~400B tokens |
| #8 | Store MTLD as a per-document label **at ingest** — labels are inside `manifest_sha256`, so unbackfillable |
| #13 | Settle SuperBPE empirically with the existing byte-matched A/B corpora (~$600) |
| #12 | Fix the missing EOS in the SuperBPE tokenizer. **Affects two already-published corpora, NOT this one** — dolma2's `eos_token_id` is 100257 and its check runs |

## Closed

| # | task | outcome |
|---|---|---|
| #5 | Measure the real FinePhrase overlap | **0.2683 distinct** on a complete-column read of 287,000 ids |
| #15 | Interleave sources within shards | **Not expressible in data.** `labels` is one dict per shard path and Gate A compares by full dict equality. Trainer-side (#19) |
| #4 | Wire `keeps_id` into the build path | superseded by #21, which is the same fix scoped to the reader |
| #27 | Fix 23 verified doc inconsistencies | this pass |
