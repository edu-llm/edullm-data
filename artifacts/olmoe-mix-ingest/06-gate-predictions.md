# Gate predictions for the OLMoE-mix-0824 pre-tokenized ingest

**Verified 2026-08-08, before any AWS spend.** Every value-domain verdict below is measured
against the **real bytes** on `olmo-data.org` via HTTP `Range` reads (no whole shard downloaded;
~7 MB total transferred), and every structural verdict is produced by **running the actual
`publish()` + `validate_dataset()` code** from
`/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/claude-20--olmoe-mix-ingest`
against a sparse S3 double whose `head()` returns the real `Content-Length` and whose
`get_range()` proxies a real HTTP range read.

Selection audited: the **regenerated** `olmoe_final_50b.json` / `olmoe_final_100b.json`
(20 objects / 51.0693 Gtok / web 74.57%; 37 objects / 98.1307 Gtok / web 75.00%).
`olmoe_selection.json` and `olmoe_selection_v2.json` are superseded and were not audited.

---

## THE HEADLINE: no blocker. Both releases pass Gate A clean.

End-to-end simulation, real bytes, real code:

```
pretrain/olmoe-mix-0824-51b   20 payload objects   Gate A: ok=True  violations=0
pretrain/olmoe-mix-0824-98b   37 payload objects   Gate A: ok=True  violations=0
```

There is **nothing that would reject the publish** given the plan as specified. The four
things that would reject it are all *plan choices you have already made correctly* (rename to
`.u32le.bin`, name the tokenizer, carve a val split from a disjoint source shard, do not
declare `seq_len`). They are enumerated in §"What WOULD reject" so they don't get undone.

Three items below are **advisories worth acting on before you spend money**, none of which is
a rejection:

- **A1 (highest value).** `publish(purpose=...)` needs **≥20 characters** or `publish()` raises
  before a byte moves. `purpose="pretraining"` fails. Cheap to hit, cheap to fix, but it aborts
  the run.
- **A2.** The EOS bound is **fail-open** on a tokenizer whose `eos_token_id` does not derive.
  Confirmed live: I mutated the published `tokenizer.json` so `derive_vocab` returned
  `eos_token_id=None`, republished, and Gate A returned **`ok=True, violations=0`** — the EOS
  check was silently skipped, not failed. `tokenizer/dolma2-bpe/v1` *does* derive 100257 today,
  so this does not fire for us; it is an ordering obligation (§9).
- **A3.** Gate A never re-reads payload to verify `sha256`, and `_resolve_tokenizer` never
  re-verifies the pinned parent `manifest_sha256`. Both are known gaps, restated because a
  copied external corpus is exactly the case where a producer digest is the only integrity claim.

---

## 1. Established facts: all four re-verified, none false

| Claim | Verdict | Evidence |
|---|---|---|
| Headerless raw uint32 LE, no `\x93NUMPY` | **TRUE** | First 16 B of all 44 sampled objects; `head[:4]` values like `3b000000`, `22730000`. Zero `\x93NUMPY`. Decoded head of arxiv/starcoder/wikipedia to fluent LaTeX / C++ / English from byte 0 (independent dtype confirmation). |
| Tokenizer `dolma2-tokenizer`: vocab 100278, eos 100257, pad 100277 | **TRUE** | `tokenizer_v1.derive_vocab()` run on the **published** `s3://edullm-data/tokenizer/dolma2-bpe/v1/tokenizer/tokenizer.json` → `{'vocab_size': 100278, 'eos_token_id': 100257}`. Base vocab len 100278, `added_tokens` 100256–100277, `<\|endoftext\|>`=100257, `<\|pad\|>`=100277. |
| All 1,122 shards a multiple of 4 bytes | **TRUE** | Independently recomputed from `olmoe_0824_sizes.json`: 0 violations in 1,122; 0 in the 38-object union; all `status == 200`. |
| Anonymous GET needs `User-Agent: curl/8.7.1` | **TRUE** | 616 + ~200 range reads, 0 errors, HTTP 206 throughout. |

---

## 2. The gate table

Bounds resolve correctly from `families/pretrain.json` via `validate._DECODE_BOUND_ALIASES`
(`validate.py:1100-1104`) — verified by calling `_family_defaults_for("pretrain/…")`, which
returns `{'min_distinct_ids': 128, 'max_eos_fraction': 0.05, 'max_zero_run': 256}`.

| # | Gate | Decided at | Verdict | Measurement |
|---|---|---|---|---|
| 1 | `eos_fraction_max: 0.05` | `pretrain_tokens_v1.py:604-614` | **PASS**, 21x margin | Worst observed **0.00241** (wikipedia). Per-label worst: wikipedia 0.0024, starcoder 0.0013, dclm 0.0011, owm 0.0007, stack 0.0007, arxiv 0.0006, pes2o 0.0002. |
| 2 | `distinct_ids_min: 128` | `:574-602` | **PASS**, 13x margin | Minimum over all sampled windows **1,656**; per-label minima 727–4,367. |
| 3 | `zero_run_max: 256` (contiguous) | `:629-640`, `_longest_run_of` `:644-655` | **PASS**, 256x margin | Longest contiguous zero run anywhere: **1**. Includes the **last 64 KB** of every selected shard: max tail zero-run **1**. No zero-padded tails. |
| 4 | Vocab bound `0 ≤ id < 100278` | `:561-572` | **PASS** | Max id observed **100,257** (= EOS itself). Out-of-range count **0** across ~1.9M sampled tokens. `pad`=100277 never appears. |
| 5 | `count.value × 4 == bytes` | `manifest.verify_arithmetic` `:561-618`; count derived at `publish.py:169-170` | **PASS**, tautologically | `_count_for` computes `tokens = size // 4`, so the identity holds by construction *and* independently: 0 of 1,122 sizes are non-multiples of 4. |
| 6 | Extension/magic-byte honesty | `pretrain_tokens_v1.check_first_bytes_not_npy:658-691` + `manifest.check_extension_matches_format:443-497` | **PASS after rename** | 0 of 44 objects carry `\x93NUMPY`. |
| 7 | `missing-required-split` | `validate._check_validation_present:1270-1333` | **PASS** | See §3 — one whole val shard is sufficient; no minimum size exists. |
| 8 | `shard-naming` | `manifest.SHARD_RE:660-663`, `check_shard_naming:1226` | **PASS after rename**; rename is **mandatory** | See §4. |
| 9 | Tokenizer dependency | `publish._resolve_tokenizer_dependency:776-822`; `validate._resolve_tokenizer:1868-1940` | **PASS** | Resolves `tokenizer/dolma2-bpe` → v1 via `_catalog/`, derives 100278/100257. But see A2. |
| 10 | Everything else in Gate A | §7 below | **PASS** | 68 validator codes + 10 profile codes enumerated; none fires. |

### Per-shard seeded decode, reproduced exactly

Sampling logic (`pretrain_tokens_v1._decode_plan:226-245` + `base.sample_offsets:130-159`):
**4 windows** (`_N_WINDOWS=4`) of `65536//4 = 16,384` bytes = 4,096 tokens each, so **16,384
tokens per shard**; offsets are `sha256(f"{shard_seed}:{counter}")[:8] % (size-window+1)`,
aligned down to 4, where `shard_seed = sha256(f"{rng_seed}:{path}")[:16]` and
`rng_seed = sha256(f"{dataset_id}|{version}|{group}")`. Pure function of identity + path + real
size — no clock, no PRNG state.

Worst case per release, from the **real seeded offsets under the real renamed paths**:

```
pretrain/olmoe-mix-0824-51b   eos=0.00201  min_distinct=1656  max_zero_run=1  max_id=100257
pretrain/olmoe-mix-0824-98b   eos=0.00195  min_distinct=1797  max_zero_run=1  max_id=100257
```

Note `eos_fraction ≈ 1/mean_doc_tokens`: 0.05 implies a ≥20-token mean document, and the
tightest source (wikipedia) implies a **~500-token** mean document. Even the *stubbiest* source
here is 25x clear of the bound. **The predicted-risky sources were not risky**: starcoder's code
snippets average ~780 tokens, wikipedia ~410-500. Neither is anywhere near breach.

---

## 3. `missing-required-split` — the val plan is sound

`families/pretrain.json` sets `validation_required: true` (`:52`) and declares `train`/`val`
path partitions (`:30-41`).

**(a) What makes it pass.** `_check_validation_present` (`validate.py:1270-1333`) requires only
that *some* group declares a partition whose name is in `SPLITS` and not in `TRAINABLE_SPLITS`
(`:1315-1317`, `is_trainable`). `SPLITS = {train, val, test}`, `TRAINABLE_SPLITS = {train}`. A
single `val-00000.u32le.bin` plus the family's `val` partition satisfies it. **Confirmed live** —
that is exactly the shape both releases publish, `violations=0`.

Two other checks must also line up, and they do automatically because `publish()` resolves the
family templates:
- `partition-glob-empty` (`validate.py:1660-1668`) — the `val-*.u32le.bin` glob must match ≥1
  manifest path. It matches 1.
- `empty-split` (`_check_dataset_exhaustive_and_splits:1258-1265`) — a *declared* split with no
  matching object fires. Not the case.

**(b) Is there a minimum val size or fraction?** **No.** There is no size, token, or fraction
floor anywhere in the code. Grepped and tested: I published a variant whose val split is the
41 MB arxiv `part-08` shard — **0.083%** of the release, 0.041 Gtok — and it returned
`ok=True, violations=0`. So 2.757% (50B) and 1.346% (100B) are far more than the code asks for.
The only size-sensitive machinery is `distinct-too-few`'s sample-size scaling
(`pretrain_tokens_v1.py:592`), which only relaxes the bound for shards under ~512 tokens.

**(c) Does anything object to a whole-shard val split?** **No.** `by: path` partitioning is
whole-object by construction (`_matches_glob:1595-1598` matches basename then full path). There
is no document-level split concept in the manifest at all, so a whole-shard val is the only
thing expressible — not a compromise the code notices. `coverage: "partition"` is satisfied
because the two globs are disjoint and jointly cover all 20/37 paths
(`_check_coverage_is_a_partition:1780-1825`).

**(d) The 150B 100%-leakage mode — confirmed caught.** I built a variant where the val shard
points at the **same source object** as a train shard. Result:

```
[V5 val == train shard bytes]  ok=False  violations=1
  duplicate-shard-digest [tokens/proofpile-2-arxiv/val-00000.u32le.bin]:
    sha256 already used by another shard in this group
```

Fires at `validate.py:748-756` (set membership on `seen_sha` within the group).
`train-heldout-leakage` (`:1746-1776`) is the *path-level* backstop and would fire if the same
path were in both globs.

**Selecting a different source shard for val satisfies both**, and the margin is stronger than
"different key": among the 38 union objects there is **not one pair with the same byte size**,
so a digest collision is structurally impossible. arxiv `part-02` (50B val) and `part-03` (100B
val) each appear in **no** train slice of either release.

**Both val shards deep-checked directly** (24 spread 16 KB windows + head 64 KB + tail 64 KB =
131,072 tokens each, 8x the Gate A sample):

| | 50B val (arxiv `part-02`) | 100B val (arxiv `part-03`) |
|---|---|---|
| bytes / tokens | 5,631,356,692 / 1,407,839,173 | 5,284,426,788 / 1,321,106,697 |
| `bytes % 4` | 0 | 0 |
| EOS fraction | **0.000084** (bound 0.05) | **0.000069** |
| distinct ids | **8,647** (bound 128) | **8,211** |
| longest zero run | **1** (bound 256) | **1** |
| tail-64 KB zero run | **1** | **1** |
| max id | 100,257 (bound <100278) | 100,257 |
| NPY magic | no (`3b0000003f0b0000…`) | no (`3b0000003f0b0000…`) |
| last token | **100257 (EOS)** | **100257 (EOS)** |
| decodes to | `\section{Introduction}\n\label{intro}…` | `\section{ Introduction}\n\nAccording to the Zeldovich…` |

Both files **end on EOS** — a clean document boundary, not a mid-document truncation. A val
shard tripping a gate would fail the whole publish; neither is close.

---

## 4. `shard-naming` — the rename is mandatory, not cosmetic

Regex (`manifest.py:660-663`):
`^(?P<split>[a-z0-9]*[a-z][a-z0-9]*(?:-[a-z0-9]*[a-z][a-z0-9]*)*)-(?P<ordinal>\d{5})\.(?P<ext>[A-Za-z0-9.]+)$`

Every split word must contain a letter, which is what makes `-of-N` a non-match rather than an
absorbed suffix. Tested directly:

| name | parses | `check_shard_naming` |
|---|---|---|
| `train-00000.u32le.bin` | `('train', 0)` | 0 violations |
| `val-00000.u32le.bin` | `('val', 0)` | 0 violations |
| `tokens/starcoder/train-00003.u32le.bin` | `('train', 3)` | 0 violations |
| `part-000-00000.npy` (AI2 dclm/starcoder) | `None` | **1 violation** |
| `part-00-00000.npy` (AI2 pes2o/proofpile) | `None` | **1 violation** |
| `part-0-00000.npy` (AI2 wikipedia) | `None` | **1 violation** |
| `train-00000-of-00042.u32le.bin` | `None` | **1 violation** |

All three AI2 native forms fail — `part` parses as the split word and the trailing group is
ordinal-shaped, so the whole name misses. Confirmed end to end by keeping upstream names:

```
[V2 upstream .npy names kept]  ok=False  violations=22
  {missing-required-split: 1, extension-format-mismatch: 7, shard-naming: 7, token-count-unit: 7}
```

`shard-naming` is **not** exempt for us: the exemption at `validate.py:625-627` covers only
profiles starting `vendored/` or `tokenizer/`, and `pretrain-tokens/v1` does not.

**Renaming the split word alone is not enough** — the extension must change too:

```
[V3 renamed to train-NNNNN but ext left .npy]  ok=False  violations=43
  {extension-format-mismatch: 20, token-count-unit: 20, undeclared-split: 2, missing-required-split: 1}
```

`.npy` maps to `container: npy` in `EXTENSION_FORMAT`, contradicting the family's
`header_bytes: 0` / `container: raw`; and because `_count_for` (`publish.py:169`) only emits a
token count for `container == "raw"`, the `.npy` path also loses its count and trips
`token-count-unit` (`pretrain_tokens_v1.py:498-506`) and the `val-*.u32le.bin` glob, cascading
into `undeclared-split`. So `.npy` → `.u32le.bin` is load-bearing in three separate ways.

---

## 5. Tokenizer dependency — and the fail-open ordering fact (A2)

**Resolution path, confirmed by running it.** `publish(tokenizer="tokenizer/dolma2-bpe")` →
`_resolve_tokenizer_dependency` (`publish.py:776-822`): with no `/vN`, it picks the highest
`vN.json` under `s3://<data_bucket>/_catalog/tokenizer/dolma2-bpe/`, reads that version's
`dataset.json`, and pins `{role: "tokenizer", dataset_id, version, manifest_sha256: groups[0].manifest_sha256}`
onto every named group's `depends_on`. Then Gate A's `_resolve_tokenizer`
(`validate.py:1868-1940`) walks the parent's manifests, finds `tokenizer.json`, reads the bytes
from the **data** bucket, and calls `derive_vocab` — placing the result in
`ctx.resolved["tokenizer"]`, which `_tokenizer` (`pretrain_tokens_v1.py:140-158`) prefers over
any declared block.

**`tokenizer/dolma2-bpe/v1` supplies exactly 100278 / 100257.** Verified by reading the real
published object (4,237,178 B) and running the repo's own `derive_vocab` on it.

Both prerequisites exist in the real bucket today:
`s3://edullm-data/tokenizer/dolma2-bpe/v1/tokenizer/tokenizer.json` and
`s3://edullm-data/_catalog/tokenizer/dolma2-bpe/v1.json`. (My first simulation failed with
`no published version of tokenizer 'tokenizer/dolma2-bpe' found` purely because my fake omitted
the catalog key — the catalog entry, not `dataset.json`, is what the version scan reads.)

**A2 — CONFIRMED: the EOS check is fail-open, not fail-closed.** `pretrain_tokens_v1.py:604`
guards the entire EOS block on `isinstance(eos_id, int) and not isinstance(eos_id, bool)`. There
is **no else branch and no violation** for a missing `eos_token_id`. Contrast `vocab_size`,
which *does* have a fail-closed counterpart (`missing-tokenizer-field`, `:536-544`).

Tested both halves live:

```
[V1 tokenizer=None]                         ok=False  {missing-tokenizer-field: 1}
[V8 vocab derives, eos_token_id -> None]    ok=True   violations=0   <-- EOS SILENTLY SKIPPED
```

For V8 I swapped the published `tokenizer.json` for one whose `added_tokens` contents no longer
match `derive_vocab`'s `"endoftext"` substring test (`tokenizer_v1.py:66-70`). `vocab_size` still
derived 100278, so the vocab check ran and the corpus published **clean with the EOS bound never
evaluated**. This is the same shape as the known SuperBPE `added_tokens: []` defect.

**What it means for our publish order:**

1. **Resolve the tokenizer or the run is worthless as evidence.** A `tokenizer=None` publish
   fails loudly (good). A publish where the tokenizer resolves a vocab but no EOS passes
   **quietly** — a green Gate A would then be no evidence at all about EOS. `tokenizer/dolma2-bpe`
   is already published and derives EOS correctly, so **name it explicitly** and treat any
   `missing-tokenizer-field` as a hard stop rather than a retry-with-defaults.
2. **A green Gate A is only meaningful if you confirm EOS was actually checked.** Because a skip
   is indistinguishable from a pass in the output, do not infer "EOS is fine" from
   `violations=0`. The measurements in §2 are the independent evidence: worst 0.00241 against
   0.05, established here without relying on the gate firing.
3. **Prefer pinning the exact version** (`tokenizer="tokenizer/dolma2-bpe/v1"`) over the bare id.
   The bare form resolves "highest `vN` in the catalog at publish time", so a future
   `tokenizer/dolma2-bpe/v2` would silently retarget this corpus's bound.

---

## 6. Gate A cost at 20 and 37 objects — nowhere near the timeout

Measured by wrapping the S3 double in a call counter and running the real Gate A:

| release | objects | `head` | `get_range` | `get` | `list` | total | per object |
|---|---|---|---|---|---|---|---|
| 51b | 20 | 40 | 100 | 7 | 2 | **149** | 7.45 |
| 98b | 37 | 74 | 185 | 7 | 2 | **268** | 7.24 |

The measured ~8 round trips/object cost model holds (7.45 / 7.24). Breakdown per object: 2
`head` (one prefetched for the entry loop, one for the profile's `_observed_size` cache) +
4 `get_range` decode windows + 1 `get_range` for the 8-byte NPY sniff.

At the **measured serial rate of 15.8 round trips/s** (recorded at
`pretrain_tokens_v1._observed_size:201-223` from the live `reservoir-dolma2` run):

- 51b: 149 / 15.8 = **9.4 s** serial; **~1 s** at `check_workers=16`.
- 98b: 268 / 15.8 = **17.0 s** serial; **~2 s** at `check_workers=16`.

Against a 14,400 s (4 h) or 7,200 s (2 h) attempt limit that is a margin of **~850x even
single-threaded**. Gate A is not remotely a timeout risk here; the earlier ~4,000-object /
~34-min estimate is obsolete by two orders of magnitude. Note `--check-workers` defaults to
`head_workers` (`validate.py:422`), so passing only `--head-workers 16` already fans out the
seven-of-eight round trips that live in the profile.

The expensive step is **`publish()`'s stream-hash**, not Gate A: one full read of 190 GiB (51b)
or 366 GiB (98b). Per project memory that **must run in-region** — locally it measured
0.8 MiB/s. Server-side copies are fine anywhere; hashing is not.

---

## 7. Item 10 — everything else in Gate A that fires on a copied-and-renamed corpus

Enumerated all 68 `Violation` codes in `validate.py` and all 10 in `pretrain_tokens_v1.py`, and
adversarially tested the ones a *copy-and-rename* pipeline can plausibly trip.

### Not named in the brief and worth knowing

| Check | file:line | Fires for us? | Note |
|---|---|---|---|
| **`purpose` ≥20 chars** (`bad-purpose` / `PublishError`) | `contracts.py:467` | **Would ABORT publish** | See A1. `purpose="pretraining"` (11 chars) raises `PublishError` before any bytes move. Use a sentence naming artifact, consumer, and question. |
| **`dataset_id` bare-ordinal rejection** | `contracts.py:342,378,406` | No | `pretrain/olmoe-mix-0824-51b`, `-98b`, `-50b`, `-100b`, `-48b` all pass. A name ending in a bare number with no unit (e.g. `olmoe-rt-50`) is **rejected** — I hit this live. Keep the `b` suffix. |
| `seq-len-misalignment` | `pretrain_tokens_v1.py:694-731` | **Only if you declare `seq_len`** | Vacuous when absent. I declared `seq_len=4096` and got **20/20 violations** — AI2's shards are not multiples of `4×4096`. **Do not declare `seq_len`.** |
| `unlisted-object-dataset-level` | `validate.py:1148-1235` | No, but is the real risk in a copy pipeline | An orphan under the dataset prefix that no manifest claims. Tested by injecting `sneaky/val-00099.u32le.bin` → fires. A retried/partial copy leaving a stray key is exactly how this happens. **Copy into a clean prefix.** |
| `unlisted-object` / `missing-object` | `validate.py:770-786`, `:716-723` | No | Per-group path-set equality both directions. Same "clean prefix" discipline covers it. |
| `labels-contradict-path` / `labels-unnameable-path` | `validate.py:1400-1505` | No | `labels_from_path` allows exactly **2** middle levels. `tokens/<label>/<split>-NNNNN` → `{source: <label>}`, recomputed and compared by full dict equality. A **third** level raises at publish time (`tokens/a/b/c/…` → `ValueError`). Keep the layout at one middle segment. |
| `label-segment-unsafe` | `validate.py:1467-1476` | No | Rejects `#` and `[`/`]` in a label segment. All 7 labels (`dclm`, `pes2o`, `proofpile-2-arxiv`, `proofpile-2-open-web-math`, `proofpile-2-stack`, `starcoder`, `wikipedia`) are clean kebab-case. |
| `split-contradicts-filename` | `validate.py:1335-1376` | No | `publish()` derives `entry.split` from the filename, so they agree by construction. |
| `dtype-too-narrow-for-vocab` / `dtype-not-checkable` | `validate.py:1507-1588` | No | vocab 100278 needs ≥4 bytes; we declare `uint32` (4). Note the canonical-name requirement: `"u4"` or `"<u4"` would trip `dtype-not-checkable`. The family supplies `"uint32"`. |
| `partition-rows-mismatch` / `partition-bad-rows` | `validate.py:1684-1740` | No | `publish._fill_missing_partition_rows` (`:619-640`) sums entry counts per glob, so `rows` matches by construction. Verified: 51b train 49,661,507,305 + val 1,407,839,173 = 51,069,346,478 = total. |
| `coverage-incomplete` / `coverage-not-disjoint` | `validate.py:1780-1825` | No | `coverage: "partition"`; the two globs are disjoint and cover all paths. |
| `inventory-objects` / `inventory-bytes` | `validate.py:451-467` | No | Recomputed from entries; `publish()` fills them. Verified 20/204,277,385,912 and 37/392,522,769,688. |
| `manifest-sha256-mismatch`, `manifest-objects`, `manifest-bytes` | `validate.py:631-672` | No | `publish()` writes both sides. |
| `head-size-mismatch` | `validate.py:728-735` | No, **if the copy is complete** | Compares real S3 size to declared `bytes`. A truncated copy fires here. This is the check that catches a botched transfer, so let it. |
| `shared-sha-with-parent` | `validate.py:758-767` | No | Our only `depends_on` parent is the tokenizer; no digest overlap. |
| `empty-shard` | `pretrain_tokens_v1.py:556-559` | No | Smallest selected object is 171 MB. |
| `_check_pinned_prm800k_contract` | `validate.py:891+` | No | Keyed on `dataset_id == "vendor/openai-prm800k"`. |
| `prefix-mismatch` | `validate.py:365-373` | No | Validate the prefix as `<dataset_id>/<version>` exactly. |

### Flat layout and ordinal reuse — both accepted (informational)

- **Flat** (`tokens/train-NNNNN.u32le.bin`, no per-source subdir): `ok=True`, and entries carry
  **no `labels`**. That silently forfeits per-source mixture selection. Keep the
  `tokens/<label>/` layout.
- **Per-label ordinal restart** (each label numbering from `00000`): `ok=True, violations=0` —
  Gate A does **not** check ordinal uniqueness or contiguity. So ordinal reuse across labels is
  *unenforced*, matching the "ordinal REUSE is the one real contradiction" note. It costs nothing
  to number globally, and the simulation above does; recommend keeping that.

### Restated, per CLAUDE.md: what Gate A does NOT verify

- **`sha256` is never recomputed from payload bytes.** The per-entry loop
  (`validate.py:717-768`) does `head` for SIZE and set-membership on the *declared* digest. So
  `duplicate-shard-digest` compares producer assertions. For us the assertions come from
  `publish()`'s own `hash_object` stream (`publish.py:280`), which does read every byte — so the
  digests are real. But Gate A is not what makes them real.
- **The pinned parent `manifest_sha256` is not re-verified by the child's Gate A.**
  `_resolve_tokenizer` reads `tokenizer.json` directly; the pin in `depends_on` is never
  recomputed. This is how V8 could swap the tokenizer bytes and still validate clean.

---

## 8. What WOULD reject the publish — the do-not-undo list

Every one of these was produced by running the real code. The plan as specified avoids all of
them; this list exists so a later "simplification" doesn't reintroduce one.

| Would-be mistake | Result | Fix |
|---|---|---|
| `purpose` under 20 chars | `PublishError` **before any bytes move** | ≥20 chars naming artifact + consumer + question |
| Keep AI2's `part-NNN-NNNNN.npy` names | 22 violations (`shard-naming`, `extension-format-mismatch`, `token-count-unit`, `missing-required-split`) | Rename to `<split>-<NNNNN>.u32le.bin` |
| Rename the split but keep `.npy` | 43 violations | Extension must be `.u32le.bin` |
| No val split | `missing-required-split` | One whole val shard + the family `val` partition |
| Val = a byte copy of a train shard | `duplicate-shard-digest` | Use a source shard in no train slice (already done) |
| Declare `seq_len` | 20/20 `seq-len-misalignment` | Do not declare `seq_len` |
| Omit `tokenizer=` | `missing-tokenizer-field` | `tokenizer="tokenizer/dolma2-bpe/v1"` |
| A third middle path level | `ValueError` at publish | Exactly one middle segment: `tokens/<label>/` |
| Stray object under the dataset prefix | `unlisted-object-dataset-level` | Copy into a clean, empty prefix |
| `dataset_id` ending in a bare number | `PublishError` (bare ordinal) | Keep the `b` unit: `…-51b`, `…-98b` |

---

## 9. Upstream health of the anomalously tiny shards (asked, answered)

None are in the final selection, but the question was whether upstream ships degenerate shards.
Sampled the **8 smallest of all 1,122** (8 windows + head 64 KB + tail 64 KB each):

| shard | Gtok | EOS | distinct | zero run | tail zero run | max id | NPY |
|---|---|---|---|---|---|---|---|
| arxiv `part-08` | 0.0410 | 0.0004 | 4,086 | 1 | 0 | 100257 | no |
| owm `part-01` | 0.0428 | 0.0004 | 4,573 | 1 | 1 | 100257 | no |
| arxiv `part-01` | 0.0624 | 0.0007 | 5,969 | 1 | 1 | 100257 | no |
| stack `part-03` | 0.0951 | 0.0005 | 3,731 | 1 | 0 | 100257 | no |
| stack `part-08` | 0.1460 | 0.0001 | 3,192 | 1 | 1 | 100257 | no |
| stack `part-09` | 0.1486 | 0.0000 | 3,495 | 1 | 1 | 100257 | no |
| pes2o `part-05` | 0.3328 | 0.0002 | 4,792 | 0 | 0 | 100257 | no |
| stack `part-14` | 0.3435 | 0.0009 | 3,845 | 1 | 1 | 100257 | no |

**All healthy — legitimately small, not degenerate.** No zero padding, no truncation signature,
no all-EOS, no out-of-range ids, thousands of distinct ids in every window. These are just
uneven upstream shard sizes (proof-pile-2 subsets partitioned by document count, not bytes).
They would each pass Gate A on their own; excluding them from the selection is a
mixture-precision choice, not a correctness one.

---

## 10. Reproduction

- Sampling reproduction of `sample_offsets`: `/tmp/olmoe-gate/plan.py`, `sample.py`, `sample2.py`
- Per-shard measurement table: `/tmp/olmoe-gate/rows.json`, `analyze.py`
- Val-shard deep check: `/tmp/olmoe-gate/valcheck.py`
- End-to-end `publish()` + Gate A simulation: `/tmp/olmoe-gate/sim.py`
- Adversarial variants V1-V14: `/tmp/olmoe-gate/adv.py`, `adv2.py`, `adv3.py`

Total network cost of this entire verification: ~7 MB of HTTP range reads plus one 4 MB
`tokenizer.json`. No AWS mutation; the only AWS calls were two read-only `s3 ls` and one
`s3 cp` of the published tokenizer.
