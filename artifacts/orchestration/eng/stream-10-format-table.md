# Stream 10 — the readable-format table (eng-10)

Worktree: `/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/eng-10--format-table`
Branch: `agent/eng-10/format-table` (base `5450f53`)
Date: 2026-08-08

## Baseline
- `python3 -m pytest -q` at `5450f53`: **1306 passed, 14 warnings in 46.29s** — MEASURED.

## The three sites, re-verified by me (MEASURED-IN-CODE, this checkout)

| # | table | site | contents |
|---|---|---|---|
| 1 | `READABLE_FORMATS` | `corpus_build.py:130` | `{"parquet","json.gz"}` |
| 2 | inline dict in `_reader_for` | `corpus_build.py:1250-1253` | `{"parquet","json.gz"}` |
| 3 | `_READERS` | `corpus_read.py:748-751` | `{"parquet","json.gz","jsonl.gz"}` |

Consumers of #1: `_assert_readable` (`:324`, message at `:329`) and the `--allow-unreadable`
filter (`:946`). Both live.
Consumers of #3: `read_documents` (`corpus_read.py:770`) **only**.

`python3 -c` on the real modules, MEASURED:
```
sorted(corpus_read._READERS)     -> ['json.gz', 'jsonl.gz', 'parquet']
sorted(corpus_build.READABLE_FORMATS) -> ['json.gz', 'parquet']
```
Confirms the divergence is real and is exactly `jsonl.gz`.

## `read_documents` is dead on the build path — CONFIRMED, and I found WHY it matters

`grep -rn "read_documents" src/` returns only its own definition, its `__all__` entry, and its
body. Zero `src/` callers. Only `tests/test_corpus_read.py:805-830` calls it (3 tests).
So ENG-EXEC's claim is correct as stated. The consequence, stated plainly: **the one table that
is right is the one nothing on the build path reads.** That is the whole defect, not a side note.

## Registry check — is any row newly admitted? NO (MEASURED)

`artifacts/reservoir/corpus-registry.json`, 17 rows:
- `Counter({'parquet': 10, 'json.gz': 7})` — **zero `jsonl.gz` rows**, zero `.zst` rows.
- drawn (`target_tokens > 0`): 9 parquet + 5 json.gz = 14. Reserve: 1 parquet + 2 json.gz.
- `plan_id` before any change: **`d5c9bcd38735e1f0`**, 27 bundles, 10,082 shards — MEASURED.

**Therefore admitting `jsonl.gz` admits NO new registry row and is NOT mix-visible today.** It is
a latent false negative: a future row (or a re-source, which is exactly what happened to
`dclm-baseline`) spelling `jsonl.gz` would be rejected at plan time although
`read_jsonl_gz_documents` handles it. I will re-measure `plan_id` after the change and assert it
is unchanged.

## Design constraint I found that the brief did not name (MEASURED-IN-CODE)

`_reader_for`'s import is **inside the function** — `corpus_build.py:1248`,
`from .corpus_read import read_jsonl_gz_documents, read_parquet_documents`. That is **late
binding**, and two Wave-0 tests depend on it: `tests/test_corpus_build.py:663-670` and
`:754-761` monkeypatch `corpus_read.read_parquet_documents` and expect `_reader_for` to pick the
patched object up.

`_READERS` as it stands holds **direct function objects**, so a table-driven dispatch through it
would bind the ORIGINAL function and silently ignore those patches — the budget and FinePhrase
partition tests would break, or worse, pass against the wrong reader. So the canonical table must
resolve **by name at call time**, not hold the callable. This is a real design constraint, not a
style choice.

No import cycle: `corpus_read` does not import `corpus_build`; a module-level
`from .corpus_read import ...` in `corpus_build` imports cleanly (MEASURED).

STATUS: design settled, implementing.

---

## WHAT LANDED

Canonical table = **`corpus_read._READERS`** (`corpus_read.py:768-772`). Everything derives.

1. **`_READERS` now maps format -> reader NAME (str), not the function object.** Load-bearing, see
   the constraint above. `corpus_read.py:768`.
2. **NEW `corpus_read.READABLE_FORMATS = frozenset(_READERS)`** (`:777`) — derived, one line.
3. **NEW `corpus_read.reader_for_format(fmt)`** (`:780`) — the one dispatch lookup, resolving the
   name out of module globals at CALL time.
4. **`corpus_build.READABLE_FORMATS` is now an IMPORT** (`corpus_build.py:87`,
   `from .corpus_read import READABLE_FORMATS`), not a literal. `is`-identical to the reader
   module's object, so it cannot be a stale copy. The old definition site (`:124-142`) is now a
   comment explaining why nothing is defined there.
5. **The inline dict inside `_reader_for` is GONE** (`corpus_build.py:1267-1284`). It now calls
   `corpus_read.read_documents` — see the `read_documents` verdict below.
6. `_assert_readable`'s message (`:341-348`) no longer implies `zstandard` is the way to add a
   format; it names `_READERS` as the single place, and keeps the `.zst` note as an aside.
   `read_documents`'s message now quotes `sorted(READABLE_FORMATS)`, recomputed.

**`plan_id` is UNCHANGED: `d5c9bcd38735e1f0`, 27 bundles, 10,082 shards** — MEASURED before and
after. No mix-visible change.

## TESTS — 1306 -> 1312 (MEASURED, `1312 passed, 14 warnings in 41.65s`)

6 new. None spells the admitted set out — that would be a fourth table.

`tests/test_corpus_read.py` (4): gate==registry bidirectionally + every entry resolves to a
callable; gate `is` the registry's key set and MUTATING `_READERS` moves it; reader resolved by
name at call time (late binding); `.zst` absent and both error messages quote the recomputed set.
`tests/test_corpus_build.py` (2): every admitted format driven through the REAL `_reader_for` to
its registered reader; and `jsonl.gz` admitted BECAUSE `_READERS` gives it the same reader object
as `json.gz`.

### The tests were validated by REINTRODUCING the defect, 4 ways (MEASURED)

| regression injected | result |
|---|---|
| A. gate re-hardcoded as a literal that AGREES today | **1 failed** |
| B. the original defect — gate omits `jsonl.gz` | **3 failed** |
| C. inline dispatch dict restored inside `_reader_for` | **1 failed** |
| E. `READABLE_FORMATS` hand-written instead of `frozenset(_READERS)` | **2 failed** |

A is the one that matters: three lists that AGREE is the state this defect grew out of, and it is
caught. I also tried D — reverting `_READERS` to hold function objects — which failed **8** tests,
5 of them pre-existing Wave-0 tests. That confirms the late-binding constraint empirically.

## ⚠️ A FOURTH TABLE, NOT IN THE BRIEF — `_PAYLOAD_EXT` (`corpus_build.py:1111`)

MEASURED-IN-CODE. `_PAYLOAD_EXT = {"parquet": (".parquet",), "json.gz": (".json.gz", ".jsonl.gz")}`,
consumed by `hf_files` (`:1132`), which raises `BuildDriverError` on an unknown key.

`sorted(set(READABLE_FORMATS) - set(_PAYLOAD_EXT))` == **`['jsonl.gz']`** — MEASURED.

**This is a real consequence of my change and I nearly shipped it.** Driving a `jsonl.gz` spec
through the widened gate:
```
gate: admitted
hf_files: BuildDriverError dolmino: no payload extension known for 'jsonl.gz'
```
So before my change a `jsonl.gz` row died at PLAN time; after it, the gate admits the row and it
dies in `hf_files` at RUN time, inside a Batch container, after the job is billing. That is
exactly the trade `_assert_readable`'s docstring exists to forbid. **Not hypothetical for a future
row — it is the shape of the `dclm-baseline` re-sourcing that already happened once.**
Note `_PAYLOAD_EXT["json.gz"]` ALREADY lists `.jsonl.gz` as a file extension, so this is a missing
KEY, not missing capability. Fixing next.

### `_PAYLOAD_EXT` — FIXED

`jsonl.gz` added, mapping to the SAME extension pair as `json.gz` (`(".json.gz", ".jsonl.gz")`) —
one reader, one listing filter. Plus **`_assert_payload_extensions_cover_readers()`**, called at
IMPORT (`corpus_build.py:1140`): registering a reader without payload extensions now fails when
the module loads, not on Batch. `set(READABLE_FORMATS) - set(_PAYLOAD_EXT)` == `set()` — MEASURED.
2 more tests, one of which POPS the key to force the guard and prove it is not a no-op.

## `read_documents` VERDICT: **made live, not deleted**

Now called by `_reader_for` (`corpus_build.py:1277`, and the read loop at `:1320`). Reasons, in
order of weight:

1. **Deleting it would have left the dispatch logic somewhere.** `_reader_for` still has to pick a
   reader. Delete the seam and that choice stays inline in the driver — which is the third table,
   the exact thing being removed. Deletion recreates the defect.
2. **It is the correct table's only consumer.** Deleting it makes `_READERS` unreachable except
   through a new accessor, i.e. the same code under a worse name.
3. **Precedent, from this repo's own history:** `reservoir_ids.keeps_id` was tested, green, and
   uncalled for weeks while the 4x FinePhrase over-exposure it prevents went unprevented. Correct
   uncalled code is a known failure mode here. The seam is now on the hot path — every file of
   every bundle — so it cannot rot unnoticed.
4. `artifacts/impl-plan/dedup-decontam-audit.md:682,701` writes the planned stage-0 dedup pre-pass
   in terms of `read_documents(...)`. A future consumer is already designed around it.

**One real behaviour change from routing through the seam**, and I checked it rather than assumed:
`read_documents` forwards `headers` as a 4th POSITIONAL. Production is unaffected — it passes
`None`, and both readers do `headers or _hf_headers()`, so explicit-None and omitted are identical
(MEASURED-IN-CODE). But two Wave-0 test fakes were pinned to exactly 3 positionals and raised
`TypeError`. Widened to `(repo, entry, sp, *a, **k)` at `tests/test_corpus_build.py:713,751`.
**These are test-fake signatures, not production behaviour** — I want that on the record since
those tests belong to the Wave-0 budget/partition work, not to me. Nothing about the budget
arithmetic, `keeps_id` filtering, or `_CHARS_PER_TOKEN` was touched.

## Registry rows newly admitted: **NONE** (MEASURED, asked for explicitly)

17 rows: 10 parquet, 7 json.gz, **zero `jsonl.gz`**, zero `.zst`. `plan_id` `d5c9bcd38735e1f0`,
27 bundles, 10,082 shards — identical before and after. **Not a mix-visible change.** The fix is
prospective: it protects the next row or re-sourcing, which is not hypothetical — `dclm-baseline`
was re-sourced once already for exactly this class of reason.

## `zstandard` NOT added, as instructed — and I confirmed the premise
Absent from `pyproject.toml`; no `import zstandard` anywhere in `src/`; `zst` in `src/` is
comments and error strings only. The two error messages that implied "add zstandard" now name
`corpus_read._READERS` as the one place to register a format, with `.zst` as an aside.

## Stale docs corrected (both cited the two-element set as VERIFIED)
- `docs/IMPLEMENTATION-PLAN.md:292-295` — cited `corpus_build.py:127` / `:171`, both stale in line
  number AND wrong in content. Replaced, with the correction called out.
- `artifacts/reservoir/CORPUS-REGISTRY.md:149-154` — "every drawn source is parquet or `.json.gz`"
  is still true of the rows; added the correction that the ADMITTED set is wider and derived.

## Final state
- **`python3 -m pytest -q` -> 1314 passed, 14 warnings, 41.4 s** (MEASURED; baseline 1306).
- Committed `2bb1a36` on `agent/eng-10/format-table`. **NOT pushed.** Tree clean.
- No S3 write, no Batch submission, no `manifest.json`. No Maple config read.

## For ENG-EXEC — decisions needed
1. **eng-09 shares both files.** My hunks: `corpus_read.py` 67-79 / 743-825; `corpus_build.py`
   87/101/124-142/329-348/1111-1148/1267-1284/1320; plus 2 test-fake signature widenings at
   `tests/test_corpus_build.py:713,751`. I did not touch `run_bundle`, `problems()`, or
   `FilterStats`. The `corpus_read.py` `__all__` edit is the one likely textual conflict.
2. **`_PAYLOAD_EXT` was a fourth table the brief did not name.** Worth asking whether other
   `spec.file_format`-keyed dicts exist in streams I cannot see. My grep of `src/` found no fifth.
3. `_reader_for` remains **UNVERIFIED against live HF from a Batch container** — its own docstring
   says so, and my change does not alter that. The suggested settling run (single bundle against
   `ubuntu-irc`, 1.87B) now also exercises the `read_documents` seam.
