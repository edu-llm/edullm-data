# stream-05 — hash pre-pass (A2a / task #22)

**Agent:** eng-05 | **Branch:** `agent/eng-05/hash-prepass` | **Date:** 2026-08-08
**Owns:** `src/edullm_data/corpus_filter.py` + `tests/test_corpus_keeplist.py`

Grades: MEASURED / MEASURED-IN-CODE / DERIVED / CARD / UNVERIFIED.

Baseline: `python3 -m pytest -q` = **1214 passed** on `f5a4017` (MEASURED, 61.84 s).

---

## ⚠️ SECTION 1 — THE KEEP-LIST CONTRACT (eng-06 read this first)

**Status: FROZEN as of this write. Any later change is announced at the top of this file.**

### 1.1 The one-line integration

`KeepFilter` is **duck-type compatible with `SeenHashes`**. It exposes the same
`add_if_new(digest: str) -> bool`. So `dedup_and_decontaminate` is **UNCHANGED**, and eng-06's
change in `run_bundle` is to pass a `seen=`:

```python
from .corpus_filter import KeepFilter, read_keep_list

keep = KeepFilter(read_keep_list(raw_bytes)) if raw_bytes is not None else None
filter_stats = FilterStats()
surviving = dedup_and_decontaminate(_selected(), index=index, seen=keep, stats=filter_stats)
```

`seen=None` keeps **today's behaviour exactly** (a fresh per-bundle `SeenHashes`). Nothing in the
existing pipeline changes until a keep-list is actually supplied.

### 1.2 `add_if_new` semantics for `KeepFilter`

| case | returns | meaning |
|---|---|---|
| key in list, first time seen | `True` | this bundle owns this document — emit it |
| key in list, already returned True | `False` | intra-bundle repeat |
| key NOT in list | `False` | another bundle won this hash (or it was never scanned) |

Both `False` cases increment `FilterStats.duplicates`, which keeps the
`seen == kept + duplicates + contaminated` identity that `test_corpus_filter` asserts. The
breakdown eng-06 should report separately lives on the filter itself:

```python
keep.hits      # int — documents emitted
keep.repeats   # int — in-list but already used (intra-bundle duplicate)
keep.misses    # int — not in this bundle's list (cross-bundle loser, or unscanned)
keep.unused    # int — keys in the list never presented in pass 2   ⚠️ see below
keep.as_dict() # {"keys", "hits", "repeats", "misses", "unused", "hash_bits"}
```

⚠️ **`keep.unused > 0` is a pass-1/pass-2 disagreement and eng-06 should surface it, not swallow
it.** It means the reader delivered a different document set than the scan saw. It is the only
signal that the two passes diverged, and it is free.

### 1.3 The key

```python
key = int(content_hash(text)[:16], 16)          # top 64 bits of the sha256 hex
partition = key >> 56                            # 0..255, top 8 bits
```

`content_hash` is UNCHANGED (still hex sha256 of `normalize_text`) — it is also the key format of
the 54 MB shipped decontamination index and narrowing it would invalidate that artifact.
`SeenHashes` takes `digest[:32]` (128 bits); the keep-list takes `digest[:16]` (64 bits).
Helper: `corpus_filter.hash64(digest_hex) -> int`.

### 1.4 The on-disk artifact — ONE FILE PER BUNDLE, `<bundle_id>.keep64`

Binary container, deliberately shaped like the existing `W1DCI001` decontamination container so the
same strictness applies:

```
offset  size  field
0       8     magic          b"EDKL001\0"
8       4     hash_bits      uint32 LE, == 64
12      4     partitions     uint32 LE, == 256
16      8     n_keys         uint64 LE
24      8     payload_bytes  uint64 LE, MUST == n_keys * 8
32      8*n   payload        uint64 LE, STRICTLY ASCENDING, no duplicates
```

Header is 32 bytes. `read_keep_list(raw)` **recomputes and rejects**: wrong magic, wrong
`hash_bits`, `payload_bytes != n_keys*8`, `len(raw) != 32 + payload_bytes`, and a payload that is
not strictly ascending. A truncated download therefore raises instead of parsing as a *smaller*
keep-list that would silently discard real documents — the same failure shape
`DecontaminationIndex.from_bytes` guards.

Sorted-ascending is not cosmetic: it is what makes `np.searchsorted` membership legal, and
checking it is a recompute rather than a declaration.

### 1.5 The set-level index — `keeplists.json`, **NOT `manifest.json`**

⚠️ **Never name this file `manifest.json`.** A `manifest.json` landing in `s3://edullm-landing`
fires the EventBridge rule and irreversibly promotes a version.

```json
{
  "schema": "edullm-keeplist-set/v1",
  "plan_id": "<plan_id or ''>",
  "normalization": "week1-nfc-rstrip-v1",
  "hash_bits": 64,
  "partitions": 256,
  "priority": ["dclm--train", "fineweb-edu--train", "..."],
  "priority_basis": "plan-order" | "explicit",
  "documents_scanned": 0,
  "distinct_keys": 0,
  "bundles": [
    {"bundle_id": "dclm--train", "path": "dclm--train.keep64",
     "scanned": 0, "distinct": 0, "keys": 0, "lost": 0, "sha256": "<of the .keep64 bytes>"}
  ]
}
```

`distinct` = unique keys this bundle contributed (after intra-bundle dedup).
`keys` = how many it **won** globally. `lost = distinct - keys`. Invariant, asserted by a test:
`sum(b["keys"]) == distinct_keys`.

### 1.6 Producer API (all in `corpus_filter.py`, all network-free)

```python
hash64(digest_hex) -> int
partition_of(key, partitions=256) -> int

HashScan(partitions=256)              # the flat np.uint64 accumulator — the memory fix
  .add_digest(hex) / .add_text(text) / .scan(documents) -> HashScan
  .finalize() -> dict[int, np.ndarray]   # partition -> sorted unique uint64
  .scanned : int

resolve_keep_lists(contributions, *, priority=None, partitions=256) -> dict[str, KeepList]
    # contributions: Mapping[bundle_id, HashScan | dict[int, ndarray] | ndarray | Iterable[int]]
    # priority: ordered list of bundle_ids, highest quality FIRST. Absent -> sorted(bundle_ids),
    #           which reproduces plan order (see §3).

KeepList(bundle_id, keys)             # frozen; .to_bytes(), __len__, __contains__
read_keep_list(raw) -> KeepList
keep_list_set_index(keep_lists, scans, *, priority, plan_id="") -> dict   # the keeplists.json body
```

Resolution is per partition: winners are decided inside `resolve_keep_lists` by a
`np.lexsort((bundle_rank, key))` and taking the first row of each key group — so a worker only ever
holds one partition. Splitting the loop across 256 real workers is a driver concern, not a code
change: `resolve_keep_lists` accepts a `partitions_subset=` to run one slice.

---

## 2. Numbers, with grades

### 2.1 CONFIRMED — today there is no cross-bundle dedup at all

MEASURED-IN-CODE: `corpus_build.py:475` calls
`dedup_and_decontaminate(_selected(), index=index, stats=filter_stats)` with **no `seen=`**, so
`dedup_and_decontaminate` constructs a fresh `SeenHashes()` per bundle
(`corpus_filter.py:302`). The `SeenHashes` docstring says so outright ("Dedup here is **within a
bundle**").

**This sharpens §5.5.** The plan says a cross-source duplicate's winner "is decided by alphabetical
accident." That is the description of the *proposed* state. **Today a cross-source duplicate is not
detected at all — every copy survives.** So the pre-pass is not a re-ordering of an existing
behaviour; it introduces cross-bundle dedup for the first time. That also means "default to today's
behaviour" is not literally implementable, and the honest default is **plan order** (§3).

**A second, unremarked win:** because `SeenHashes` is per *bundle* and bundles are per
`(source, domain, split)`, the same text appearing twice in one source under two doc ids — one
carved to `train`, one to `val` — is **not deduped today**. That is train/val leakage inside a
single source, invisible to every current check. Global dedup removes it. UNVERIFIED at what rate;
pass 1 will measure it for free (it is `lost` on the val bundles).

### 2.2 CORRECTION — §5.3's partition table is at 85.9 B/key, not the 16 B/key its own caveat claims

(a) **Plan claim**, `docs/IMPLEMENTATION-PLAN.md` §5.3, the partition table and the ⚠️ directly
under it: *"Those columns are 16 B/key — a `(hash, ref)` pair held during the sort."*

(b) **Countervailing evidence** — recompute the rows (DERIVED, arithmetic only):

| partitions | table says @1.23B | at 16 B/key | at 85.9 B/key |
|---|---|---|---|
| 64 | 1.65 GB | 0.31 GB | **1.65 GB** ✓ |
| 128 | 0.82 GB | 0.154 GB | **0.83 GB** ✓ |
| 256 | 0.41 GB | 0.077 GB | **0.41 GB** ✓ |

All three rows reproduce at **85.9 B/key** — the `set[int]` rate — and none at 16. The same holds
for the 0.96B column (0.96e9 × 85.9 / 256 = 0.322 GB = the table's 0.32).

(c) **Numbers it moves:** the true 256-way resident is **0.077 GB/worker** (1.23B, 16 B/key sort
pair) or **0.060 GB** (0.96B) — **5.3× smaller than the plan's own table**. My brief's "0.030 GB at
960M" is the 8 B/key accumulator figure and is correct for the accumulator.

(d) **Blast radius: none, and that is why this is a footnote not a blocker.** The table is used to
argue 256 partitions fit a worker. They fit by a wider margin than claimed. No decision reverses.
The reason to record it is that the table is *labelled* with the wrong unit, so the next person
sizing a worker from it will over-provision by 5×, and the caveat that was written to prevent
exactly that confusion is itself the confused part.

### 2.3 Memory — MEASURED with `tracemalloc`, 200,000 entries, this branch, 2026-08-08

| structure | B/entry | DCLM 325M | fineweb-edu 250.2M | global 960.2M |
|---|---|---|---|---|
| `SeenHashes` `set[int]` (today) | **85.95** | **27.93 GB** | 21.50 GB | 82.53 GB |
| `HashScan` `array('Q')` (pass 1) | **8.43** | **2.74 GB** | 2.11 GB | 8.09 GB |
| `KeepFilter` array + bitmap (pass 2) | **8.13** | **2.64 GB** | 2.03 GB | 7.80 GB |

Ratio **10.20×** — inside the 5–11× §5.3 predicts.

**Two independent confirmations that the measurement is sound**, both recomputed rather than
quoted: 85.95 reproduces `SeenHashes.__doc__`'s 85.9 to **0.06%**, and 27.93 reproduces §5.2a's
**27.92 GB** for DCLM. The measurement idiom and the plan's arithmetic agree.

**Verdict on the blocker:** DCLM goes from **186% of a 15.03 GB container (OOM)** to **18% of it**.

### 2.4 CORRECTION — §5.3's "2.60 GB for DCLM, 7.68 GB global" is a floor, not a measurement

(a) **Plan claim**, §5.3 Constraint 1 and §5.2a's closing paragraph: *"A flat `np.uint64` at 8 B/key
holds DCLM's 325M documents in **2.60 GB** and the global 960M in **7.68 GB**."*

(b) **MEASURED: 2.74 GB and 8.09 GB.** The plan's figures are `n × 8.000` exactly. The real
accumulator measures **8.43 B/entry** because CPython over-allocates ~1/16 on `array.append`.
(`KeepFilter`, built once at final size, does hit ~8.13 including its bitmap — so the plan's number
is nearly right for pass 2 and 5.4% low for pass 1.)

(c) **Numbers moved:** DCLM 2.60 → **2.74 GB** (+5.4%); global 7.68 → **8.09 GB** (+5.3%).

(d) **Blast radius: none. Both still fit comfortably** — this does not reopen any decision. Recorded
only because 2.60 GB is a number no run will hit, and a container sized to it has no headroom by
construction. Both figures are now in the module docstring.

### 2.5 MEASURED — partition balance, the assumption nothing else would catch

At 2,000,000 keys: **256/256 partitions occupied**, min 7,585, max 8,112, mean 7,812.5, stdev 86.7,
**max/mean = 1.0383**. Sizing a worker at `total/256` is safe to within ~4%.

Worth stating why this is tested: every correctness test in this file passes with a *lopsided*
split. Skew is invisible to all of them and shows up only as one worker OOMing in production.

### 2.6 MEASURED — pass 1 throughput, and it is sha256-bound

**102,478 documents/s single core** (2M documents, `content_hash` + partition + append).

DERIVED from that: DCLM's 325M documents = **0.88 core-hours**; the whole 960.2M corpus =
**2.60 core-hours**, i.e. **~24 seconds of wall clock across 384 vCPU** if perfectly parallel.

⚠️ **That is the HASHING only and is not the pre-pass's cost.** Pass 1 must still *read* the staged
text. §5.3 budgets that second read at **~0.2 h from S3** (and notes it is only affordable because
of §3's staging — from HuggingFace it would be 2.7–5.4 h). So the pre-pass is read-bound, the
CPU side is free, and the honest figure for the critical path is §5.3's ~0.2 h, not my 24 s.
**I have not independently verified the 0.2 h** — UNVERIFIED, inherited from §5.3.

---

## 3. The source-priority decision — UNMADE, and it needs an owner

**Implemented as a parameter, defaulted, and flagged. This is the finding my brief anticipated.**

`resolve_keep_lists(..., priority=[...])` takes an ordered list of bundle ids, **highest quality
first**. Absent one it uses `sorted(bundle_ids)`, which reproduces the order `plan_document` already
emits bundles in (`corpus_build.py:284` sorts by `(source, domain or "", split)`).

**What that default actually does, MEASURED on the 17-row registry's source labels:** the winner of
every cross-source duplicate is the alphabetically-first source label. In order:

`arxiv, dclm, essential-web, finemath, finepdfs-edu, finephrase-*, finewiki, fineweb-edu,
github-archive, pes2o, pubmed, stackexchange, stackv2-edu, ubuntu-irc`

So **`dclm` beats every source except `arxiv` (reserve, `target_tokens=0`)** — the 410B web-diverse
pillar wins duplicates against `fineweb-edu`, `pes2o`, `pubmed`, `finewiki` and all four synthetic
partitions, **purely because "d" precedes "f", "p", "s" and "u"**. That is not a quality judgement
and must not be read as one.

**Why this matters more than it looks.** A cross-source duplicate between `dclm` and `fineweb-edu`
is a near-certainty at scale — both are Common Crawl derivatives. Under the default, the copy that
survives is labelled `dclm`, so it lands in the `web-diverse` category rather than `edu-web`. The
priority list therefore **silently shifts the realised category mix away from the design**, and
nothing downstream would flag it. It is a mix decision wearing an implementation detail's clothes.

**DECISION NEEDED (owner):** the ordered priority list. A defensible starting point, from the
report's own quality reasoning — *this is my suggestion, not an owner decision, and it is
UNVERIFIED against any measurement*: curated/academic first (`pes2o`, `pubmed`, `finewiki`),
then targeted-quality web (`finepdfs-edu`, `fineweb-edu`, `finemath`), then code and forum
(`stackv2-edu`, `stackexchange`, `ubuntu-irc`), then bulk web (`dclm`), and **synthetic last**
(FinePhrase is a *rephrasing of FineWeb-Edu* — where a FinePhrase document collides exactly with
its own source, the real document is the one to keep).

Until that list exists, `keeplists.json` records `"priority_basis": "plan-order"` so the artifact
states which regime produced it.

---

## 4. Two things the pre-pass turns up that were not in the brief

### 4.1 There is train/val leakage today, inside a single source, and nothing checks for it

MEASURED-IN-CODE. `SeenHashes` is per **bundle**, and a bundle is
`(source, domain, split)` — so `dclm--train` and `dclm--val` have **separate** dedup sets. The same
text under two upstream doc ids, one carved to train and one to val, survives in both.
`carve` routes on `is_held_out(doc.id, ...)` — a hash of the **id**, not the text — so identical
text with different ids lands on both sides of the split routinely.

This is real leakage, it is invisible to every current check, and the global pre-pass removes it as
a side effect. **Rate UNVERIFIED** — pass 1 measures it for free as `lost` on the val bundles.

Precedent that this class of defect ships: the memory index records *"150B heldout-val is duplicated
train — all 6 val shards are copies of train shards; publishing as-is = 100% leakage."*

### 4.2 `Receipt` still has no filter block (§5.6), and the keep-list makes that worse

MEASURED-IN-CODE, confirming §5.6: `run_bundle` returns `filter_stats.as_dict()` and nothing writes
it to the `Receipt`. With a keep-list there is now a **second** set of numbers with the same
problem — `KeepFilter.as_dict()`'s hits/repeats/misses/unused. **`unused > 0` is the only signal
that pass 1 and pass 2 disagreed about the input**, and if it is not persisted it exists solely in
CloudWatch.

Not mine to fix (`corpus_receipt.py` is not my file, and `run_bundle` is eng-06's). Flagged for
ENG-EXEC: **whoever owns §5.6 should add the `keep` block at the same time as the `filter` block.**

---

## 5. What landed

`src/edullm_data/corpus_filter.py` (+~560 lines; `dedup_and_decontaminate`, `SeenHashes`,
`content_hash`, `DecontaminationIndex` all **UNCHANGED**)
`tests/test_corpus_keeplist.py` (new, 30 tests)

Commit **`d1d9c8f`** on `agent/eng-05/hash-prepass`, parent `f5a4017`. **Not pushed.**

**Tests: 1214 → 1244** (MEASURED; 30 new, zero regressions, zero existing tests modified).

### The tests that carry the argument

| test | what it RECOMPUTES |
|---|---|
| `test_survivors_equal_independently_computed_unique_set` | survivors == unique set computed by a **different code path** (full 256-bit hashes, plain Python `set`), and exactly one copy each |
| `test_randomised_corpus_matches_the_independent_unique_set` | same, at 2,800 and 2,800 docs with 35%/60% duplicate rates |
| `test_keep_lists_are_identical_under_shuffled_input_order` | **12 shuffles** of both bundle order and within-bundle document order → byte-identical `to_bytes()` |
| `test_partition_subsets_reassemble_into_the_same_keep_lists` | all 256 partitions resolved **independently**, union compared byte-for-byte to the single-process answer |
| `test_todays_per_bundle_dedup_does_NOT_catch_the_cross_source_duplicate` | the §2.1 claim, asserted directly rather than cited |
| `test_measured_bytes_per_entry_beats_the_set_by_at_least_5x` | `tracemalloc` on **both** structures in one process |
| `test_partitions_are_balanced_...` | the uniformity assumption every other test is blind to |
| `test_truncated_keep_list_raises_...` (×4) | truncation at 4 offsets must **raise**, never parse smaller |

### What I did NOT do

- **No S3 writes, no Batch submissions, no `manifest.json` written anywhere.** Nothing promotes.
- Did not touch `run_bundle`, `load_registry`, `plan_document`, `_reader_for` (eng-04/06/07).
- Did not write the pass-1 **driver** (the thing that reads staged text and fans out 256 workers).
  Scope was the producer + data structure; the driver is a CLI concern and needs `_reader_for`
  (eng-07) to exist. `resolve_keep_lists(partitions_subset=)` is the seam it plugs into.
- Did not verify §5.3's **~0.2 h** second-read cost. UNVERIFIED, inherited.

---

## 6. Summary of every number, graded

| number | value | grade |
|---|---|---|
| test baseline / final | 1214 / **1244** | MEASURED |
| `SeenHashes` cost | **85.95 B/entry** | MEASURED (`tracemalloc`, 200k) |
| `HashScan` cost | **8.43 B/entry** | MEASURED |
| `KeepFilter` cost | **8.13 B/key** | MEASURED |
| density ratio | **10.20×** | MEASURED |
| DCLM set / scan / filter | **27.93 / 2.74 / 2.64 GB** | DERIVED from MEASURED B/entry × 325M |
| global 960.2M | **82.53 / 8.09 / 7.80 GB** | DERIVED |
| §5.3's 2.60 / 7.68 GB | **superseded → 2.74 / 8.09** | MEASURED (§2.4) |
| partition max/mean @2M | **1.0383** | MEASURED |
| pass-1 hashing | **102,478 docs/s/core** → 2.60 core-h global | MEASURED / DERIVED |
| pass-1 read cost | ~0.2 h from S3 | **UNVERIFIED** (§5.3) |
| §5.3 partition table unit | is 85.9 B/key, labelled 16 | DERIVED (§2.2) |
| true 256-way sort resident | **0.077 GB** @1.23B, **0.060** @0.96B | DERIVED |
| cross-bundle dedup today | **none exists** | MEASURED-IN-CODE (§2.1) |
| 64-bit collision loss | 0.04 expected pairs @1.23B | DERIVED (plan §5.3, re-read) |

## 7. Decisions needed from ENG-EXEC / owner

1. **The source-priority list** (§3). Blocking nothing today — the mechanism defaults and the
   artifact labels itself — but the default makes `dclm` beat almost everything alphabetically and
   that shifts the realised category mix. **This is a mix decision, not an implementation detail.**
2. **Does §5.6's receipt fix include the `keep` block?** (§4.2) `unused > 0` is the only
   pass-1/pass-2 disagreement signal and is currently unpersisted.
3. **Confirm the keep-list contract with eng-06** (§1) before either of us is far along. It is
   frozen from my side.

