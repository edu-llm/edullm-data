# gigatoken audit — can it replace HF `tokenizers` for a 1T-token dolma2 build?

**Date:** 2026-08-07
**Auditor:** subagent, read-only (nothing was installed or executed; per Capstone_LLM/CLAUDE.md this laptop runs nothing computational)
**Target of audit:** https://github.com/marcelroed/gigatoken @ `main`, pushed 2026-08-06
**Grading legend:** MEASURED (I ran it, or the repo's own harness measured it) / CARD (claimed in README or docs, unverified) / DERIVED (arithmetic on MEASURED or CARD inputs) / PROJECTED (reasoned estimate)

## Repo facts (MEASURED — GitHub API, 2026-08-07)

```
full_name        = marcelroed/gigatoken
description      = Language model tokenization at GB/s
stargazers_count = 3936
forks_count      = 204
open_issues_count= 16
created_at       = 2025-11-10
pushed_at        = 2026-08-06
license          = MIT
```

Not a toy: ~4k stars, active as of yesterday. Layout is a real Rust crate (`src/` ~700 KB of
Rust) plus a Python wrapper (`gigatoken/`), 30 Python test files, 9 Rust benches, and an unusual
amount of adversarial-looking engineering documentation (`profiling/`, `pretokenizer_optimization_log.md`,
`design_doc.md`).

Files that matter to this audit (paths are repo-relative):

| Path | Why |
|---|---|
| `src/pretokenize/fast/olmo3.rs` (18 KB) | a dedicated fast pretokenizer scheme named for OLMo 3 |
| `src/pretokenize/fast/mod.rs` | shared byte predicates + invalid-UTF-8 decode contract |
| `src/pretokenize/mod.rs` (47 KB) | the generic/reference pretokenizer |
| `src/load_tokenizer/hf.rs` (40 KB) | how a `tokenizer.json` is parsed and dispatched |
| `gigatoken/_hf_compat.py` (27 KB) | the `as_hf()` compatibility wrapper |
| `gigatoken/_tokenizer.py` (13 KB) | the `gt.Tokenizer` constructor / selection logic |
| `tests/tokenizers/test_hf_parity.py` | the differential test against HF |
| `tests/test_config_dispatch.py` | asserts which scheme a given config dispatches to |
| `tests/test_encode_dclm.py` | parity over DCLM (web-crawl) documents |

---

## Q1 + Q4 (merged): is `allenai/dolma2-tokenizer` supported, and is the pretokenizer the same one?

These two questions collapse into one check, and the answer is **yes, byte-for-byte, and it is
not a lookalike**.

### The dolma2 pretokenizer we must match (MEASURED — fetched the live config)

`https://huggingface.co/allenai/dolma2-tokenizer/raw/main/tokenizer.json`:

```json
"normalizer": null,
"pre_tokenizer": {
  "type": "Sequence",
  "pretokenizers": [
    { "type": "Split",
      "pattern": { "Regex": "(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\\r\\n\\p{L}\\p{N}]?\\p{L}+|\\p{N}{1,3}| ?[^\\s\\p{L}\\p{N}]+[\\r\\n]*|\\s*[\\r\\n]+|\\s+(?!\\S)|\\s+" },
      "behavior": "Removed", "invert": true },
    { "type": "ByteLevel", "add_prefix_space": false, "trim_offsets": true, "use_regex": false }
  ]
},
"post_processor": null,
"model": { "type": "BPE", "dropout": null, "unk_token": null,
           "continuing_subword_prefix": "", "end_of_word_suffix": "",
           "fuse_unk": false, "byte_fallback": false, "ignore_merges": false }
```

Four facts that make this the **easy** case, not the hard one:

1. `normalizer: null` — no NFC/NFKC. Unnormalized Unicode is passed through untouched. There is
   no normalization step to get wrong.
2. `ByteLevel` with `add_prefix_space: false` and **`use_regex: false`** — the ByteLevel stage is
   a pure 256→unicode byte map; it does no splitting of its own and adds no leading space. All
   splitting authority lives in the single `Split`/`Regex`/`invert: true` stage.
3. `post_processor: null` — **there is no post-processor to add specials.** This is important for
   Q5: `add_special_tokens=False` has nothing to suppress on this tokenizer. HF's own
   `add_special_tokens` is a no-op here.
4. `ignore_merges: false`, `byte_fallback: false`, `dropout: null`, `unk_token: null` — plain
   deterministic byte-level BPE, no exotic model flags.

The regex is the original **cl100k / GPT-4** pattern (`\p{N}{1,3}`, the `(?!\S)` whitespace
lookahead, the `[^\r\n\p{L}\p{N}]?` letter prefix).

### What gigatoken implements for it (MEASURED — read the source)

`src/pretokenize/fast/olmo3.rs` opens with a doc comment naming the target explicitly:

```rust
//! Fast pretokenizer for the Olmo 2/3 (dolma2) regex — on aarch64 (NEON)
//! and x86_64 with AVX-512 (runtime-detected) a mask scanner via the shared
//! `cl100k_family::batch_masks` boundary algebra, with the scalar `advance_pos`
//! below as reference, no-SIMD fallback, and bad-zone/tail executor:
//! `(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}{1,3}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+`
```

and at `olmo3.rs:261-262` defines the reference regex its own unit tests differ against:

```rust
/// The Olmo3/dolma2 pattern verbatim — no possessive quantifiers, so it
/// runs directly under fancy-regex.
const OLMO3_REF_REGEX: &str =
    r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}{1,3}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+";
```

**MEASURED (I ran the comparison, string-level, not a tokenizer):** I unescaped the `Regex`
string out of the live dolma2 `tokenizer.json` and compared it to `OLMO3_REF_REGEX` from
`olmo3.rs`. They are **byte-identical, 115 characters, `IDENTICAL: True`**. The internal name
"olmo3" covers OLMo **2 and 3**, and dolma2 is exactly that scheme — the README's "OLMo 2 / 3"
row is not a lookalike, and this is the tokenizer whose regex the file is written against.

Verdict on Q1's "hardcoded table" worry: it is **half true and it does not hurt us**, see the
selection mechanism below.

### Selection mechanism (MEASURED — read the loader)

Selection is a two-tier thing, and dolma2 lands on the good tier. `gigatoken/_load/hf.py`
exposes `TOKENIZER_LINES` / `_match_tokenizer_line` / `try_load_from_config` — a hardcoded
table keyed off `tokenizer_config.json`'s `tokenizer_class` + `auto_map` module, used only for
tokenizers that are **not** expressible as a `tokenizer.json` (Kimi's `TikTokenTokenizer`
remote-code path). `tests/test_config_dispatch.py:64-68` documents the fall-through:

```python
def test_dispatch_returns_none_for_unregistered_dir(tmp_path):
    """A directory whose config identifies no registered line falls through
    to the tokenizer.json path (None), without touching any vocab file."""
```

So the normal path is: parse `tokenizer.json` (`src/load_tokenizer/hf.rs`, 40 KB) and pick a
pretokenizer scheme from the parsed `pre_tokenizer` — **not** from the model name. dolma2 is a
plain `tokenizer.json`, so it takes the parsed path. That also answers "can it load an arbitrary
`tokenizer.json`": yes, and the scheme is chosen by matching the *parsed pretokenizer*, with
`BPETokenizer.from_tiktoken` raising `ValueError: unknown pretokenizer scheme` on an unrecognized
one (`tests/test_config_dispatch.py:76-80`) — i.e. the failure mode on an unrecognized regex is
an **exception**, not a silent wrong split. (I verify the regex→scheme matching code, and
therefore whether an *unmatched* regex errors or silently degrades, in Q4b below.)

Vocab 100,278 real / 100,352 padded and the 22 `added_tokens` (ids 100256-100277, including
`<|endoftext|>` = 100257) are ordinary data for a byte-level BPE; nothing about the padded vocab
is special to gigatoken since it never emits an id it did not read from the vocab.

### The repo's parity fixture IS our tokenizer (MEASURED — I diffed the two configs)

`tests/conftest.py:144-146`:

```python
def olmo3_tokenizer_path() -> Path:
    """Path to Olmo3 (dolma2) tokenizer.json in the HF cache."""
    return _hf_tokenizer_json("allenai/Olmo-3-1025-7B")
```

and `tests/tokenizers/conftest.py:32`: `TokenizerSpec(name="olmo3", eot_text="<|endoftext|>",
eot_id=100257)` — the same EOS id dolma2 declares. `benchmarks/families.json:87-89` groups
`allenai/OLMo-2-0425-1B` under `allenai/Olmo-3-1025-7B`, confirming the family claim.

The repo tests `allenai/Olmo-3-1025-7B`, not `allenai/dolma2-tokenizer`, and the two
`tokenizer.json` files are **different files** (4,237,178 vs 7,137,177 bytes; git oids
`887c9f43…` vs `5fe17212…`). So I downloaded both and diffed them structurally:

```
top-level keys equal: True
version/truncation/padding/normalizer/pre_tokenizer/post_processor/decoder/added_tokens: ALL equal
model.{type,dropout,unk_token,continuing_subword_prefix,end_of_word_suffix,
       fuse_unk,byte_fallback,ignore_merges}: ALL equal
vocab  len 100278 == 100278, dicts equal: True
merges len 100000 == 100000, equal: False   <-- see below
max vocab id: 100277 == 100277
n added_tokens: 22 == 22
```

The only difference is the **merges serialization format**, not the merges:

```
dolma2 merges[0:3] : ['Ġ Ġ', 'ĠĠ ĠĠ', 'i n']            <class 'str'>   (legacy space-joined)
olmo3  merges[0:3] : [['Ġ','Ġ'], ['ĠĠ','ĠĠ'], ['i','n']] <class 'list'>  (tokenizers >=0.20 pairs)
after normalizing both to tuples -> equal: True
```

**This is the single most useful finding in the audit.** The repo's `olmo3` parity fixture is,
semantically, *our exact tokenizer* — same vocab dict, same 100,000 merges, same pretokenizer,
same added tokens, same EOS id. Every ID-parity assertion in `tests/tokenizers/test_hf_parity.py`
and `tests/test_encode_dclm.py` therefore already covers the dolma2 tokenizer's behaviour.

It also creates one **new, concrete, adoptable risk** the repo does not test: gigatoken has never
been exercised on the **legacy space-joined merges form** that `allenai/dolma2-tokenizer` ships,
only on the list-of-pairs form. If `src/load_tokenizer/hf.rs` parses only one form the outcome is
either a load error (safe) or a silently-empty/partial merge table (catastrophic — it would still
emit in-range ids that decode). See Q4b.

Also worth noting: `allenai/dolma2-tokenizer/tokenizer_config.json` declares
`tokenizer_class: "GPT2Tokenizer"` — which is **not** in gigatoken's `TOKENIZER_LINES` table, so
`try_load_from_config` returns `None` and it correctly falls through to the `tokenizer.json`
path. Good, but confirm the code path with a real load.

### Q4b: the "silent fallback to a wrong split" fear does not apply — unknown regex is a hard error

This was the scariest hypothesis in the brief and the source refutes it cleanly. Scheme selection
is an **exact string match on the regex text**, with `None` on no match, and `None` is converted
to an `Err`.

`src/pretokenize/options.rs:106-142` — `from_split_regexes` / `from_split_regex`:

```rust
pub fn from_split_regexes(patterns: &[&str]) -> Option<Self> {
    match patterns {
        [p] => Self::from_split_regex(p),
        _ if patterns == DEEPSEEK_V3_SPLIT_REGEXES => Some(PretokenizerType::DeepSeekV3),
        _ => None,
    }
}
pub fn from_split_regex(pattern: &str) -> Option<Self> {
    match pattern {
        ... 
        r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}{1,3}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+" => {
            Some(PretokenizerType::Olmo3)
        }
        ...
        _ => None,
    }
}
```

`src/load_tokenizer/hf.rs:632-634`:

```rust
PretokenizerType::from_split_regexes(&regexes).ok_or_else(|| {
    eyre::eyre!("Unknown pre_tokenizer Split regexes, no fast pretokenizer for: {regexes:?}")
})
```

**MEASURED (I ran the string match):** taking the `Regex` string straight out of the live
`allenai/dolma2-tokenizer/tokenizer.json` and comparing it against every arm:

```
GPT2     match: False
GPT4-a   match: False
GPT4-b   match: False
Qwen2    match: False
Olmo3    match: True      <-- unique hit
Split regexes in the Sequence: 1  -> takes the [p] arm -> from_split_regex(p) -> Olmo3
ByteLevel add_prefix_space: False -> detect_add_prefix_space() == False   (correct, no prefix space)
```

Note how narrow the escape is, and why exact matching is the right design: the `GPT4-b` arm
differs from `Olmo3` by **one character** — `\s*[\r\n]` vs `\s*[\r\n]+` (114 vs 115 chars). Under
any fuzzy/prefix matching dolma2 could have landed on cl100k's scheme; under exact matching it
cannot. There is **no generic regex-engine fallback** in the load path at all: a pretokenizer
gigatoken does not have a hand-written SIMD scheme for **fails to load**. That is the fail-closed
behaviour you want, and it is the opposite of the dangerous answer.

The one residual sensitivity this creates is a **maintenance** risk, not a correctness risk: if
AI2 ever republishes `dolma2-tokenizer` with a cosmetically re-serialized regex (e.g. adding
possessive quantifiers as HF did between the two GPT-4 spellings above), gigatoken will refuse to
load rather than mis-split. Pin the `tokenizer.json` you validate against (which our gate already
does by re-deriving from the published `tokenizer.json`).

### Q4c: what the SIMD path does on non-covered input

There is no "input the SIMD path does not cover" in the mis-split sense. Three layers, all
converging on one answer:

- SIMD availability is a **runtime** decision with a scalar reference as fallback, per scheme:
  `src/pretokenize/fast/mod.rs:53-71`
  ```rust
  #[cfg(any(target_arch = "aarch64", target_arch = "x86_64"))]
  if mask::simd_scanner_available() {
      return state.fill_spans_two_phase::<S>(bytes, batch, prefetch);
  }
  crate::pretokenize::fill_spans_keyed_with_buf(bytes, || state.next_span::<S>(bytes), batch, prefetch)
  ```
  `olmo3.rs:53-56` says so in prose: "With SIMD support (aarch64 NEON, or x86_64 AVX-512 detected
  at runtime), iteration runs the shared cl100k-family mask scanner; **elsewhere every token takes
  the scalar `advance_pos`**." Both paths are asserted equal by
  `src/pretokenize/mod.rs:985` (`check_source(FastOlmo3Pretokenizer::new(b), …, "olmo3")`).
- Within the SIMD path there is an explicit **"bad-zone/tail executor"** (`olmo3.rs:3-4`) — the
  scalar `advance_pos` is the designated executor for chunk edges and regions the mask scanner
  declines. So the scanner is a *boundary-finder that can abstain*, not a lossy approximation.
- **Note for our deployment:** AWS Batch CPU instances are x86_64. On x86_64 the mask scanner is
  gated on **AVX-512** (`batch_masks_x86::<AVX512>`, runtime-detected). AVX-512 is present on
  Intel Sapphire/Emerald Rapids (`m7i`, `c7i`, `r7i`) and AMD Genoa/Zen4+ (`m7a`, `c7a`) but
  **absent on Graviton-adjacent and on older Intel Skylake-SP-era and on Zen3 (`m6a`/`c6a`)**.
  On an AVX-512-less x86 instance gigatoken still runs (scalar `advance_pos`) and is still
  correct, but a meaningful chunk of the headline speed is SIMD. This is a **compute-profile
  selection** item, not a correctness item — and note the README's own third benchmark table
  (Ryzen 7 9800X3D, Zen 5, 16 threads) still shows 6.27 GB/s, so the scalar/narrow-SIMD path is
  not slow in absolute terms. GRADE: instance-family AVX-512 availability is DERIVED from public
  ISA support, not measured on our fleet — verify with `lscpu` on the chosen profile.

---

## Q2 + Q7 (merged): is exact parity opt-in, and does the fast path apply to us?

**The brief's framing contains a false premise, and correcting it is the most consequential result
of this audit.** The README's "compatibility mode vs Gigatoken API" split is NOT a
lossy-vs-exact split. It is an **API-shape** split. And we do not need either wrapper.

### The three-tier reality (MEASURED — read the wrapper source)

```
tier 1  gt.Tokenizer(path).encode_batch(list_of_docs)   <- NATIVE, takes a Python list
tier 2  gt.Tokenizer(path).encode_files(TextFileSource) <- NATIVE, Rust reads the files
tier 3  gt.Tokenizer(path).as_hf().encode_batch(...)    <- COMPAT wrapper over tier 1
```

`gigatoken/gigatoken_rs/__init__.pyi:97-105` — the native Rust `encode_batch` signature:

```python
def encode_batch(
    self,
    inputs: list[str] | list[bytes] | BytesSource | ak.Array,
    *, parallel: bool = True,
) -> ak.Array:
```

**`encode_files` is not the only fast API.** The native `encode_batch` accepts
`list[str] | list[bytes]` directly and fans it out over rayon inside Rust. Our documents arrive as
an in-memory stream of individual already-filtered, already-deduplicated documents — which is
*exactly* `list[bytes]`. So **the answer to Q7's core worry is: we are not forced onto `as_hf()`,
and there is no separator-splitting requirement.** The `TextFileSource(..., separator=...)` API is
one *input source* among several (`TextFileSource`, `JsonlFileSource`, `ParquetFileSource`,
`BytesSource`), not the gate on the fast path.

Also present and directly useful to a shard packer: `encode_batch_list` (returns
`list[list[int]]`, rows assembled in Rust) and `encode_batch_padded` (returns a `(rows × width)`
uint32 matrix plus true lengths, assembled in Rust in one pass).

### What `as_hf()` actually costs, and why we don't want it anyway

`gigatoken/_hf_compat.py` is a thin Python adapter. Its batch path is
`_hf_compat.py:285`/`:397`:

```python
rows = self._tokenizer._encode_batch_list_compat(_as_list(text), options)   # line 285
...
rows = self._tokenizer.encode_batch(texts)                                  # line 397
```

i.e. it **calls the same native backend** and then does Python-level list assembly, attention
masks, `BatchEncoding` dict construction, padding/truncation, and a "forbidden specials" scan
(`_WrapTruncate`'s `forbid`, stub line 287). The README's "non-negligible cost to performance"
is therefore the cost of **materializing transformers-shaped Python objects**, not a different or
degraded tokenization algorithm. Both tiers route through one Rust encoder.

That is decisive for the audit question: **there is no "fast mode" that is lossy w.r.t. token
ids.** The parity risk is identical in both tiers because the ids come from the same code. What
differs is the returned object (`ak.Array` vs `BatchEncoding` with `attention_mask`), offsets, and
the padding/special-token machinery.

### `add_special_tokens=False` — what the equivalent is

Our build calls `encode_batch(texts, add_special_tokens=False)` and appends EOS itself. Mapping:

- **Native tier (recommended):** `Tokenizer.encode_batch(docs)` takes **no** `add_special_tokens`
  parameter *because it never adds any*. Raw text in, ids out. This is the semantics we want, by
  construction, with no flag to get wrong. Appending `100257` ourselves is unchanged.
- **Compat tier:** `HFCompat.__call__(..., add_special_tokens: bool = True)` — note the default is
  `True`, matching transformers. `_hf_compat.py:219-224`:
  ```python
  def _specials(self, add_special_tokens: bool) -> tuple[list[int], list[int]]:
      """(prefix_ids, suffix_ids) the post-processor would add, or empty."""
      if not add_special_tokens:
          return [], []
      return self._prefix_ids, self._suffix_ids
  ```
  and `_hf_compat.py:152` derives `_prefix_ids/_suffix_ids` from `post_processor` via
  `_template_special_ids`, which returns `([], [])` for `ByteLevel` and — importantly — **raises**
  for post-processor types it cannot reproduce rather than silently diverging
  (`_hf_compat.py:69`: `raise ValueError(f"unsupported post_processor type: {kind}")`).

For dolma2 specifically this is all moot in the best way: **`post_processor` is `null`**, so
`_template_special_ids(None)` returns `([], [])` and even `add_special_tokens=True` would add
nothing. Both tiers inject zero special tokens for our tokenizer. The repo's own parity helper
documents exactly this reasoning (`tests/tokenizers/test_hf_parity.py:36-41`):

```python
# add_special_tokens=False: gigatoken encodes raw text without template
# wrapping, so HF post-processors that inject tokens (ModernBERT's
# TemplateProcessing adds [CLS]/[SEP]) must be disabled. A no-op for the
# other tokenizers, whose ByteLevel post-processors add nothing.
hf_ids = hf_tok.encode(text, add_special_tokens=False).ids
gigatoken_ids = gigatoken_tok.encode(text.encode("utf-8")).tolist()
```

### One operational detail that matters for a Batch job

`Tokenizer.encode_batch(..., parallel=None)` auto-detects and **turns parallelism off inside a
multiprocessing worker or forked child** (`_tokenizer.py:211-219`):

> "batches encode in parallel on the process-global thread pool, except inside a multiprocessing
> worker (or forked child), where everything runs on the calling thread so worker processes
> compose instead of oversubscribing — or, after a fork, deadlocking. Pass True/False to override.
> **Output is identical either way.**"

So if our tokenizer driver already forks worker processes, we would silently get single-threaded
gigatoken per worker (still correct, and probably what we want — but it means "1 process ×
N vCPU" and "N processes × 1 thread" are different performance regimes and we should pick one
deliberately). Also note the ABI3 Python-iteration overhead in the README's Known Issues, and
that `encode_batch` returns an `ak.Array` — so **`awkward` becomes a build dependency** unless we
use `encode_batch_list`/`encode_batch_padded`.

---

## Q3: correctness testing in the repo

**Answer: yes, a real differential test suite exists, and it is unusually good for a
performance-first project — but CI does not run it.** Both halves matter.

### The differential tests that exist (MEASURED — read the test files)

**`tests/tokenizers/test_hf_parity.py`** — module docstring states the intent exactly:

> "Test that `BPETokenizer.from_hf` produces token IDs identical to HuggingFace `tokenizers` for
> every tokenizer in TOKENIZER_SPECS … Covers the fast pretokenizers, added-token extraction, NFC
> normalization (Qwen2), and the BPE merge itself."

- 8 tokenizers via `TOKENIZER_SPECS` (`tests/tokenizers/conftest.py:28-55`): gpt2, **olmo3**,
  qwen2, qwen3_5, modernbert, glm5_2, deepseek_v3, deepseek_v4.
- 36 hand-written `TEXTS` (`:51-90`) incl. emoji, CJK, Cyrillic, Arabic, Arabic-Indic digits,
  code, JSON, URLs, `"a"*500`, `\r\n\r\n`, and explicitly **non-NFC decomposed** text.
- 28 `SPECIAL_TEXTS` (`:95-126`) — and this list is directly on point for our Q5 concern. It
  includes the literal `"<|endoftext|>"`, `"a<|endoftext|>b"`, `"<|endoftext|><|endoftext|>"`,
  `"á<|endoftext|>é"`, `|||PHONE_NUMBER|||`, `|||EMAIL_ADDRESS|||`, `|||IP_ADDRESS|||`,
  `<|im_start|>`, `<|pad|>`, `<|endofprompt|>` — i.e. **the exact dolma2 added-token set** — plus a
  deliberate lookalike battery that must NOT match: `"<|endoftext"`, `"endoftext|>"`,
  `"<|endoftext |>"`, `"<|ENDOFTEXT|>"`, `"||PHONE_NUMBER|||"`, `"|||UNKNOWN_TAG|||"`,
  `"<<|endoftext|>>"`, `"||||||PHONE_NUMBER||||||"`.
- `test_decode_roundtrip` over all 64 strings; `test_encode_batch_matches_encode` (batch == single).
- `test_owt_matches_hf` — streams OWT and compares **id arrays** with
  `np.array_equal(hf_ids, jt)`, defaulting to 100 MB, `OWT_MAX_BYTES=0` for the whole ~12 GB.
  HF side is `encode_batch_fast(texts, add_special_tokens=False)`.

**`tests/test_encode_dclm.py`** — the most relevant test in the repo for a web-corpus build.
Docstring:

> "The corpus (~20 MB) is DCLM-baseline documents selected for tokenizer-hostile content —
> CJK/RTL scripts, NFC-divergent text, emoji, control whitespace, code, unbroken 80+ char tokens,
> 128 KB+ documents"

and `test_dclm_sample_is_diverse` *asserts the fixture actually contains* each of those
(`:26-39`) — an anti-decoration check in the same spirit as our own golden rule. Then
`test_encode_dclm_matches_hf` runs exact ID parity for three backends, **including
`olmo3_tokenizer_path`**, against `HFTokenizer.encode_batch(..., add_special_tokens=False)`.
Plus `test_encode_batch_matches_single_docs`, `test_encode_files_dclm_fixture` (encode_files ==
encode_batch), and `test_decode_roundtrip_dclm`.

**Rust-level differential tests** — `src/pretokenize/fast/olmo3.rs:254-504`, a `#[cfg(test)]` module
that differs the SIMD/scalar pretokenizer against `fancy_regex` running the **verbatim dolma2
pattern**: `olmo3_small_cases` (78 adversarial cases incl. `"'ſ"`/`"it'ſ fine"` for the U+017F
long-s case-fold, `"a\u{2028}b"`, `"\x0bword"`, `"١٢٣٤٥"`, `"1٢3x"`, `"\u{a0}word"`),
`olmo3_matches_regex_owt` (5 MB), and `olmo3_matches_regex_owt_full` (**the full ~12 GB OWT,
token-by-token vs the regex**, `#[ignore]`d, rayon over 32 MB chunks).

There is also a shipped `--validate` mode in the CLI (README FAQ: `gigatoken bench <repo> <file>
--validate --doc-separator "<|endoftext|>"` → `validation OK: 20401 documents match`), which is
how issue #35 was found *by a user* — evidence the validation tooling works.

So: **the answer to "a repo with a 1000x claim and no differential test over adversarial Unicode"
is that this repo has one, at three levels (Rust pretokenizer vs regex, Python ids vs HF, and a
user-runnable validator), over both OWT and a purpose-built tokenizer-hostile DCLM sample.**

### The gap: CI runs no tests (MEASURED — read `.github/workflows/CI.yml`)

`.github/workflows/CI.yml` is the stock maturin-generated file. Its header says so:

```yaml
# This file is autogenerated by maturin v1.9.5
# To update, run
#    maturin generate-ci github
```

Its jobs are `linux`/`musllinux`/`windows`/`macos`/`sdist`/`release` and every one of them only
runs `maturin build --release` and uploads wheels. **There is no `pytest` step and no `cargo test`
step anywhere in either workflow.** So the excellent differential suite is **developer-run, not
gate-enforced**: nothing prevents a regression from being published to PyPI. Combined with the
release job auto-publishing on tag, this means *a version bump is not evidence the parity tests
passed*. This is the strongest structural argument for our own gate.

Secondary note: the heavy tests are also **opt-in by environment** — `test_owt_matches_hf` is
`skipif(not OWT_PATH.exists())` on `~/data/owt_train.txt`, and `olmo3_matches_regex_owt_full` is
`#[ignore]`d. On a fresh clone with no OWT file, the strongest tests silently skip.

---

## Q5: adversarial-input behavior

Taking the brief's list one at a time. GRADE on all of these: source-read (so DERIVED about
behaviour, MEASURED about what the code says) — **I ran none of it.**

**Invalid / lone-surrogate / malformed UTF-8.** This is handled deliberately and documented at
length, `src/pretokenize/fast/mod.rs:129-155`:

> "Invalid input is garbage-in/**defined**-garbage-out, with two hard guarantees the walkers rely
> on: [1] Never reads past `bytes.len()`… a truncated tail consumes exactly the bytes that remain
> and yields `CP_INVALID`. (Pre-fix this read up to 3 bytes past the slice and returned an end
> past `len` — walker panic on the Iterator path, out-of-bounds span on the SpanBatch path.)
> [2] The codepoint is always `<= 0x10FFFF`… invalid leads 0xF5..=0xFF … can assemble
> 'codepoints' up to 0x1FFFFF, which are clamped to `CP_INVALID`. (Pre-fix the table lookup read
> up to ~246 KB past the table — heap memory whose contents depend on other threads' allocations,
> **which is what made >65 KB invalid-UTF-8 pretokens split nondeterministically between the
> walker paths**.)"

Read that parenthetical carefully. It is a **confessed, fixed, previously-shipped
nondeterminism bug on invalid UTF-8 in long pretokens** — i.e. exactly our failure mode (silently
different ids), on exactly our kind of input (web crawl), at exactly our scale (long blobs). It is
fixed now, and the fix is principled (`CP_INVALID = 0x10FFFF`, an unassigned noncharacter that
classifies as `Other` in every scheme so all paths agree). But its existence is the empirical proof
that this class of bug is reachable here, and it is the reason a pre-adoption differential test on
*our own* corpus is not optional.

Note also **open issue #45 (label: `security`)**, filed by `GhimBoon`, second half:

> "Malformed UTF-8 reaches `from_utf8_unchecked` … src/lib.rs:401-409, src/batch.rs:1000-1050,
> src/pretokenize/fast/mod.rs:120-125 … Malformed input such as `b"\xff"` can therefore reach
> `std::str::from_utf8_unchecked`. Rust documents invalid UTF-8 passed to this function as
> undefined behavior."

**Open, unlabelled as wontfix, unresolved as of 2026-08-07.** `decode_non_ascii` at
`fast/mod.rs:120-127` is indeed `unsafe fn` wrapping `from_utf8_unchecked`. For us this is a
**must-mitigate**: our documents are bytes off a web crawl. Mitigation is cheap and we should do it
regardless of tokenizer: validate UTF-8 (or `errors="replace"`) at the reader boundary before
handing bytes to any tokenizer. If we feed only known-valid UTF-8, the issue cannot fire. Worth
noting HF `tokenizers` sidesteps this by only accepting `str`.

**Unnormalized Unicode.** dolma2 has `normalizer: null`, so there is nothing to normalize and
nothing to diverge on — the byte-level BPE consumes bytes as given. `test_decode_roundtrip_dclm`
asserts byte-exact roundtrip for every DCLM doc precisely because "byte-level BPE without
normalizer must roundtrip every document." **This removes the entire NFC/NFKC risk class for our
tokenizer** (it is a live risk for Qwen2/DeepSeek, which is why the repo tracks
`normalizes_nfc`).

**Extremely long single "words" (base64, minified JS).** Covered by design and by fixture: the
DCLM fixture asserts `re.search(r"\S{80,}", d)` and `len(d.encode()) >= 131_072` are both present.
The pretoken cache is explicitly built for long-tailed pretoken distributions (README FAQ).
Caveat: issue **#36 "Unbounded cache growth"** (closed) and PR **#46 "Bound the encode caches by
default (512 MiB per worker)"** (closed) — memory, not correctness, but a 1T-token job with many
workers should set that bound consciously.

**NUL bytes.** No specific test. `\0` is a `Cc` control character, not `White_Space`, so it
classifies as `CharClass::Other` and joins a punctuation run identically on both sides — the
byte-level table maps all 256 bytes including `0x00`. GRADE: **PROJECTED from the classification
code, untested in the repo.** Put a NUL case in our own gate.

**The literal `<|endoftext|>` in document text.** This is the one I would have most expected to
break, and it is directly tested — `SPECIAL_TEXTS` contains `"<|endoftext|>"`,
`"a<|endoftext|>b"`, `"hello <|endoftext|> world"`, `"<|endoftext|><|endoftext|>"`,
`"text<|endoftext|>\nmore text"`, `"á<|endoftext|>é"`, and `test_endoftext_id` asserts
`spec.eot_id in ids` for `f"a{eot}b"` with `eot_id=100257`. Note what this means semantically:
gigatoken matches HF, and **HF `tokenizers` with `add_special_tokens=False` still tokenizes a
literal `<|endoftext|>` in the text to id 100257**, because it is an `added_token` and
`add_special_tokens` only governs the *post-processor*, not added-token matching. So both agree —
and if our build wants document text containing `<|endoftext|>` to NOT become 100257, **that is a
pre-existing property of our current HF pipeline, not something gigatoken changes.** Worth
knowing, out of scope for this audit.

**`add_special_tokens=False` equivalent that will not inject a special token.** Answered in Q2:
the native `encode_batch` has no such parameter because it never injects; and dolma2's
`post_processor` is `null` so there is nothing to inject in either tier.

### The str/bytes encoding boundary, and whether lone surrogates can reach the tokenizer

Our pipeline holds Python `str`. gigatoken's `encode` accepts `str | bytes` and `encode_batch`
accepts `list[str] | list[bytes]` (`gigatoken_rs/__init__.pyi:96-102`), so both are legal inputs.
This determines the whole lone-surrogate question:

- **If we pass `list[str]`** (the drop-in change), PyO3 converts `str` → Rust `String`/`&str` at the
  boundary, and that conversion **cannot represent a lone surrogate**. A Python `str` containing
  `'\ud800'` fails to convert (PyO3 uses the UTF-8 view of the object, which CPython refuses to
  produce for surrogates) and surfaces as a **Python exception**, not silent corruption. So the
  entire lone-surrogate / invalid-UTF-8 class — including issue #45's `from_utf8_unchecked`
  concern — is **unreachable on the `str` path**. This is the same guarantee HF `tokenizers` gives,
  for the same reason.
- **If we pass `list[bytes]`** (the faster path — skips a re-encode, and what `encode_files`/
  `JsonlFileSource` do internally), arbitrary bytes reach Rust and issue #45 becomes live.

**Recommendation, and it is a cheap one: pass `str`, or validate before passing `bytes`.** Our
documents come from JSON, so they are already `str` by the time we hold them; passing `str` costs
one UTF-8 encode inside Rust and buys immunity to the entire malformed-input class. If profiling
later says that encode matters, switch to `bytes` **only** behind an explicit
`b.decode("utf-8")`-succeeded check (or `errors="replace"` at the reader). Note that lone
surrogates cannot survive a JSON round-trip through `json.loads` into a `str` that then
`.encode("utf-8")`s successfully anyway — `str.encode` raises `UnicodeEncodeError` on surrogates —
so a `bytes` path built by encoding our own `str`s is also safe. The dangerous shape is only
"raw bytes straight off the wire, never decoded."

### Is there fuzzing? (MEASURED — searched the tree)

**No.** Grepping the full file tree for `fuzz`/`proptest`/`quickcheck`/`hypothesis` returns
**NONE**, and `Cargo.toml`'s `[dev-dependencies]` are `criterion`, `fancy-regex`, `rand`,
`simdutf`, `tempfile` — `fancy-regex` is the differential oracle, not a fuzzer. So the correctness
story is **differential-over-fixed-corpora, not generative fuzzing**: 78 hand-written Rust cases +
64 hand-written Python strings + ~20 MB curated-adversarial DCLM + up to 11.9 GB of OWT. That is a
lot of real bytes and a well-chosen adversarial fixture, but it is **not** randomized exploration
of the input space, and there is no coverage-guided fuzz target. For our purposes the OWT/DCLM
corpora are the more relevant evidence anyway (they are the same *kind* of data we tokenize), but
"nobody has fuzzed this" is an accurate statement and worth recording plainly.

---

## RESOLVED: the legacy space-joined merges risk is a non-issue (and provably so)

I raised this myself in Q1 as the one new risk the repo does not test, and flagged the catastrophic
branch as "silently-partial merge table." **It is neither a load error nor a partial table — the
legacy form is explicitly, deliberately supported, and our specific file parses unambiguously.**

`src/load_tokenizer/hf.rs:98-99` and `:113-137` (MEASURED — read the source):

```rust
#[serde(deserialize_with = "deserialize_merges")]
merges: Vec<[String; 2]>,
...
/// Merges appear as `["a", "b"]` arrays in current tokenizer.json files and
/// as `"a b"` strings in older ones; accept both.
fn deserialize_merges<'de, D>(deserializer: D) -> Result<Vec<[String; 2]>, D::Error>
{
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum Merge { Pair([String; 2]), Legacy(String) }
    let raw = Vec::<Merge>::deserialize(deserializer)?;
    raw.into_iter()
        .map(|m| match m {
            Merge::Pair(pair) => Ok(pair),
            Merge::Legacy(s) => {
                let (a, b) = s.split_once(' ').ok_or_else(|| {
                    serde::de::Error::custom(format!("invalid merge entry: {s:?}"))
                })?;
                Ok([a.to_string(), b.to_string()])
            }
        })
        .collect()
}
```

Three things make this safe rather than merely present:

1. It is a `serde(untagged)` enum over the **whole list**, so a partial parse is not
   representable — either every entry deserializes into `Vec<[String; 2]>` or the whole load
   `Err`s. There is no "skip the ones I don't understand" path, which is the shape that would have
   produced a silently-partial table.
2. An entry with no space is a **hard error** (`invalid merge entry: {s:?}`), not a skip.
3. The same file goes on to handle untyped legacy files too (`:93-96`: "tokenizer.json files
   written before tokenizers 0.9 (e.g. the original GPT-2 upload) omit `model.type`; those are
   always BPE"), so legacy-format support is a considered feature, not an accident.

And **MEASURED on our actual file** (`allenai/dolma2-tokenizer/tokenizer.json`), because the
`split_once(' ')` on the FIRST space is only correct if no merge half contains a space:

```
total merges: 100000 | all entries are str (legacy form): True
entries whose space-count != 1:                  0
entries with NO space (would hard-error):        0
ambiguous (>1 space, split_once would mis-split):0
vocab strings containing a literal space:         0
merge entries whose halves/join are NOT all in vocab: 0
```

That last line is the strong one: for all 100,000 entries, both halves **and** their concatenation
resolve in the vocab. So the legacy parse reconstructs the merge table exactly. (It cannot be
otherwise for a ByteLevel BPE — a literal space is encoded as `Ġ`, never as `0x20` — but it is
better to have checked than to have reasoned.) **Risk closed.**

---

## Q6: open issues and PRs bearing on output equality

Searched the tracker for `mismatch`, `wrong tokens`, `differs`, `incorrect`, `panic`, `parity`, and
listed all 54 issues/PRs. Only these bear on output equality:

| # | State | What it means for us |
|---|---|---|
| **#37** Unicode property version mismatch with HF | **closed WONTFIX** (label `hf-bug`) | **The one real live risk.** See below. |
| **#43** Better report issues due to Unicode version differences | **open** (label `hf-bug`) | Maintainer's own follow-up to #37/#35: wants validation to *label* these as HF bugs. Confirms the divergence is unfixed by design. |
| **#35** Token-order mismatch on Qwen2.5-7B, Unicode combining marks | closed (`hf-bug`) | Same root cause. Notable: found by a *user* via the shipped `--validate`, and the symptom was **same token count, different order at index 98** — a mismatch no length or size check would ever catch. Qwen2.5-specific (combining marks); our scheme has no `\p{M}` rule. |
| **#45** unsafe Hub cache paths + unchecked UTF-8 | **open** (`security`) | Malformed UTF-8 → `from_utf8_unchecked` (UB). Neutralized for us by passing `str`; see Q5. |
| #31 o200k special-token handling in tiktoken loader | open | `<|endoftext|>` gets the wrong id — but only in the **`.tiktoken` rank-file loader for o200k**. We load a `tokenizer.json` for olmo3. Not our path. |
| #40 `from_tiktoken` always selects r50k | closed (fixed by #42) | Same story — `.tiktoken` path only, and now fixed with `_ => raise`. |
| #44 `HFCompat` uses `_token_to_id` not `token_to_id` | open | API-surface papercut in the compat wrapper. Irrelevant on the native path. |
| #20 Match HF drop semantics for byte-gap BPE vocabs | closed PR | Historical parity fix, already in. |
| #18 fairseq-ordered vocabs, added-token lstrip/rstrip, ByteLevel `add_prefix_space` | closed PR | Historical parity fix, already in. |
| #36 / #46 unbounded cache growth / bound caches 512 MiB per worker | closed | Memory, not correctness. Set the bound for a 1T job. |

**Zero results for `panic`.** No open issue alleges a wrong-id result for a `tokenizer.json`-loaded
byte-level BPE, and none at all for the olmo3/dolma2 scheme.

### The Unicode-version divergence, stated precisely

This is the only finding that could actually corrupt our corpus, so it deserves exact framing.

Gigatoken resolves `\p{L}`/`\p{N}`/`\s` from **ICU4X**, not from a regex engine —
`src/pretokenize/unicode.rs:1-2`:

```rust
use icu::properties::props::{EnumeratedProperty, GeneralCategory, GeneralCategoryGroup, WhiteSpace};
use icu::properties::CodePointSetData;
```

with `Cargo.toml`: `icu = { version = "2.2", features = ["compiled_data", "datagen"] }`. HF
`tokenizers` resolves the same classes through **Oniguruma**. The two ship different Unicode
tables, so any codepoint whose General_Category changed between versions **pretokenizes
differently, hence gets different ids, with no error**.

Version facts (MEASURED where cited):
- `icu_properties_data 2.2.0` docs.rs: *"This data was generated with CLDR version 48.2.0, ICU
  version release-78.1rc"* — the page states no Unicode number, so **Unicode 17 is DERIVED** from
  ICU 78 (and from issue #37's reporter asserting "ICU Unicode 17 properties"). ICU4X 2.0's notes
  say Unicode 16.0; 2.2 is newer.
- Issue #37 reporter, on HF: *"HuggingFace `tokenizers==0.20.3` uses Oniguruma Unicode 14."*
  **CARD** (reporter's claim, and the maintainer did not dispute it).

**This directly implicates our tree.** Our repo pins no `tokenizers` at all (`pyproject.toml`
dependencies are just `boto3`, `numpy`), and the version actually installed in this workspace is
**`tokenizers-0.20.1`** (MEASURED: `pipelines/week1_curriculum/.venv/.../tokenizers-0.20.1.dist-info`)
— i.e. **the exact Unicode-14 generation named in issue #37**, one patch below the reporter's
0.20.3. So a naive swap would compare Unicode 14 (old HF) against Unicode 17 (gigatoken): the
worst-case pairing, ~3 Unicode releases apart.

The maintainer's position (issue #37, 2026-07-24), verbatim:

> "I still consider matching errors of old versions of `tokenizers` to be out of scope. In part
> this is because `gigatoken` doesn't depend on `tokenizers`, so allowing users to specify which
> `tokenizers` version number to match against would add a lot of complexity … Again, the old
> version of `tokenizers` you're using is doing the wrong thing in your example, and newer versions
> of it will not act the same."

He is technically right that gigatoken is the *more correct* implementation — and his last point is
even a substantive argument for us: characters whose properties changed after Unicode 14 postdate
dolma2's training data, so their tokenization is out-of-distribution for the model either way. But
"more correct" is not the property we need. **We need "identical to whatever produced the rest of
the corpus,"** because a corpus tokenized half by HF and half by gigatoken is internally
inconsistent, and that inconsistency is invisible to sha256 (different bytes → different hash,
both valid), to size checks, and to id-range checks.

Mitigation is straightforward and turns this from a blocker into a gate condition: **pin a modern
`tokenizers` and measure the divergence on our own corpus.** A contributor (`CompilationFail`) built
per-version Unicode tables and reported *"Exhaustive validation against both HF versions covered
28,913,664 cases each with zero mismatches"* (branch
`CompilationFail/gigatoken:fix/hf-unicode-version-parity`) — **CARD, unmerged, and rejected by the
maintainer**, so do not count on it, but it exists as a fork if we ever need Unicode-14 fidelity.

---

## Q8: benchmark honesty, and the real speedup for our workload

### Are the README's 989x/1299x apples-to-apples for us? No — and the README says so

To the author's credit the disclosure is right there (`README.md:157-161`, MEASURED — I read it):

> "Gigatoken encodes the whole file un-split, and is thus doing more work than the other tokenizers
> to find the split boundaries and automatically parallelize. HuggingFace tokenizers
> (`encode_batch_fast`) gets the first 100 MB and tiktoken (`encode_ordinary_batch`) the first 1 GB,
> both presplit on `<|endoftext|>`. This is fair because neither of the compared tokenizers do
> caching, meaning the speed is roughly uniform throughout processing."

The "fair because neither caches" defense is **sound for the HF side** (constant throughput, so a
100 MB sample estimates the rate) but it **understates gigatoken's own advantage**, because
gigatoken *does* cache and its cache warms over 11.9 GB. So the ratio credits gigatoken a warm
cache measured over 11.9 GB against HF measured cold-ish over 100 MB. That is a real asymmetry, and
it inflates the headline for any workload that does not present a big homogeneous stream.

Four reasons the headline number is not ours:

1. **Hardware.** 989x is a 144-core dual-socket EPYC 9565 (Zen 5, AVX-512). We plan ~32-64 vCPU.
   The README's own 16-thread rows are the honest comparator: OLMo 2/3 is **6.06 GB/s / 109x** on a
   Ryzen 9800X3D and **7.56 GB/s / 1,299x** on an M4 Max. The spread between 109x and 1,299x on
   two 16-core machines is entirely the *HF denominator* (55.4 vs 5.8 MB/s), not gigatoken —
   which is the tell that these ratios measure the baseline's environment as much as the subject.
2. **The denominator must be our own.** The README's HF numbers (5.8-55.4 MB/s for OLMo 2/3) are
   single-config measurements on someone else's box. We have our own.
3. **Already-split documents.** Removes gigatoken's extra boundary-finding work (helps it) and
   removes HF's pre-split advantage (neutral). Roughly a wash, slightly favoring gigatoken.
4. **Cache warmth is real but data-dependent.** Our corpus is heterogeneous (web + synthetic +
   code + math), and the README's Known Issues flag **"CJK-heavy data is much slower"** — the
   pretoken cache is the mechanism, and heterogeneity is what degrades it.

### Calibrated against our MEASURED baseline

Using the coordinator's measured numbers as the denominator (**MEASURED, our build**):
- HF `encode_batch`, 32 vCPU: **10.5 M tok/s** → 1.0T tokens = **26.5 h**
- HF single-document: 1.10 M tok/s (→ 32 vCPU gives 9.5x scaling efficiency, ~30%… actually
  10.5/1.10 = 9.5x on 32 vCPU = 30% parallel efficiency, itself a hint that our HF path is
  Python/GIL-bound rather than tokenizer-bound)

Converting gigatoken's rate to tokens: the README's own OLMo 2/3 rows give a bytes→tokens ratio of
2701.65 Mtok / 11920.51 MB = **4.41 bytes/token** (MEASURED by the repo's CLI output, README:213).

| Scenario | gigatoken rate | 1.0T tokens | vs our 26.5 h | Grade |
|---|---|---|---|---|
| Ryzen 9800X3D, 16 threads (README) | 6.06 GB/s = 1,374 Mtok/s | **0.20 h** | 130x | CARD (theirs), DERIVED (conversion) |
| M4 Max, 16 cores (README) | 7.56 GB/s = 1,714 Mtok/s | 0.16 h | 165x | CARD / DERIVED |
| EPYC 9565, 144 cores (README) | 23.06 GB/s = 5,229 Mtok/s | 0.05 h | 495x | CARD / DERIVED |
| **Our 32 vCPU, discounted 4x for heterogeneity + no AVX-512 + Python overhead** | ~340 Mtok/s | **~0.8 h** | **~33x** | **PROJECTED** |

**Defensible expected speedup for our case: 20-60x (PROJECTED), i.e. tokenization drops from
~26.5 h to roughly 0.5-1.5 h at 32 vCPU.** I deliberately discount hard: even a 4x haircut on the
weakest README configuration lands above 30x, and the reason I still believe a large multiple is
that our own 30% parallel efficiency (10.5 vs 1.10 × 32) says our HF path is losing most of its
theoretical throughput to Python-level batching — precisely the overhead gigatoken's Rust-side
`encode_batch_list`/`encode_batch_padded` eliminate. **Even at a pessimistic 10x, tokenization
stops being a scheduling consideration.** I would not quote any number above 60x without measuring.

### The number that actually decides this

**Download dominates, and by a lot.** ~4.3 TB kept / ~6.1 TB read. Even at a generous sustained
1 Gb/s (125 MB/s) aggregate, 6.1 TB is **~13.5 h**; at 500 Mb/s it is ~27 h. So:

| Stage | Now | With gigatoken | Grade |
|---|---|---|---|
| Download/read 6.1 TB | ~13.5-27 h | **unchanged** | DERIVED from 6.1 TB @ 125 MB/s |
| Tokenize 1.0T | 26.5 h | ~0.5-1.5 h | MEASURED / PROJECTED |
| **Total (serial)** | **~40-53 h** | **~14-28 h** | DERIVED |
| **Total (overlapped)** | **~26.5 h** (tokenize-bound) | **~13.5-27 h** (download-bound) | DERIVED |

Two readings, and the difference between them is the crux:

- **If the stages run serially**, gigatoken removes ~25 h from a ~40-53 h build — a **~1.9x
  end-to-end win**. Material.
- **If they overlap** (stream-and-tokenize, which is the natural design), tokenization at 26.5 h and
  download at 13.5-27 h are *the same order*, so today tokenization is plausibly the binding
  constraint and removing it makes the build purely download-bound — a win of **0 to ~13 h**
  depending on real bandwidth. Possibly nothing.

**So the honest answer to "is it worth it" depends on a number we have not measured: our actual
sustained S3 read bandwidth.** If we read at 125 MB/s+, gigatoken buys little wall-clock in an
overlapped design. If we read faster (in-region S3 to a 32-64 vCPU instance can far exceed that —
and note our own memory says publish pulled at 0.8 MiB/s *out of region*, so in-region rates need
measuring, not assuming), tokenization is the wall and gigatoken removes it.

There is one asymmetry worth naming: gigatoken's win is not only wall-clock but **cost and retry
latency**. A 26.5 h tokenize is a job that can time out (our own job-def limit history: the
218-shard/125 GB olmo publish hit the 60-min ceiling; a 26.5 h stage needs
`attemptDurationSeconds` well past the default) and that is expensive to re-run after a bug. A
1 h tokenize is cheap to re-run, which materially changes how freely we can iterate on the mixture
— and given our own history of discovering corpus defects *after* publishing, cheap re-tokenization
has option value beyond the single build.

---

## VERDICT: **ADOPT-WITH-GATE**

The gate is one specific differential test, defined below. Until it passes, do not tokenize a
single published shard with gigatoken.

### Why not DO-NOT-ADOPT

Every structural fear in the brief was checked and came back clean, and several came back better
than clean:

- The dolma2 pretokenizer regex is **byte-identical** (115 chars, verified) to the constant
  `olmo3.rs` is written against and tested against.
- The repo's `olmo3` parity fixture (`allenai/Olmo-3-1025-7B`) is **semantically our exact
  tokenizer** — same vocab dict, same 100,000 merges, same pretokenizer, same 22 added tokens, same
  EOS 100257 — so existing ID-parity assertions already cover dolma2's behaviour.
- There is **no lossy fast mode**. Both tiers call one Rust encoder; the README's "cost to
  performance" is Python object materialization. The brief's central premise was wrong in our favor.
- Unknown pretokenizer → **hard error**, never a silent wrong split. Exact string match, `_ => None`
  → `Err`. dolma2 hits the `Olmo3` arm uniquely, despite a one-character-different cl100k arm
  sitting next to it.
- The legacy space-joined merges risk I raised **resolves safe and provably so** (untagged enum over
  the whole list, no-space entries hard-error, and all 100,000 of our entries verified unambiguous
  with every half and join present in vocab).
- `normalizer: null` and `post_processor: null` delete the two largest divergence classes
  (NFC, special-token injection) for our tokenizer specifically.
- The `str` input path makes the open `from_utf8_unchecked` issue (#45) unreachable.
- Real differential tests exist at three levels, including full-12 GB OWT and a
  deliberately tokenizer-hostile DCLM fixture whose adversarial content is itself asserted.

### Why not plain ADOPT — the three things that require a gate

1. **CI runs no tests.** `.github/workflows/CI.yml` is stock maturin: build wheels, publish on tag.
   No `pytest`, no `cargo test`. The good suite is developer-discipline, not enforcement, and the
   release job publishes to PyPI on tag regardless. **A version number is not evidence of parity.**
   Pin an exact version + hash, and re-run our gate on every bump.
2. **The Unicode-version divergence is real, unfixed by policy, and our installed HF is the bad
   generation.** gigatoken = ICU4X 2.2 (Unicode 17, DERIVED); HF 0.20.x = Oniguruma Unicode 14
   (CARD). Our workspace has `tokenizers-0.20.1`. The maintainer closed #37 WONTFIX. This is the one
   mechanism that can silently produce different-but-valid ids on real web text.
3. **Confessed prior nondeterminism in exactly our risk shape.** `fast/mod.rs:129-155` documents a
   fixed bug where >65 KB invalid-UTF-8 pretokens "split **nondeterministically** between the walker
   paths" because a table lookup read up to ~246 KB past the table into other threads' heap. Fixed,
   principled, and disclosed — but it proves the class is reachable, on web-crawl input, at our
   scale. Also: issue #35's symptom was *same token count, different order* — invisible to every
   check we have.

### THE GATE — the exact differential test that must pass

Run on AWS Batch (CPU) via the `edullm-platform-runs` skill. Not on this laptop.

1. **Sample 1,000,000 documents from our actual staged corpus**, stratified across every pool that
   will appear in the 1T mix (edu-web, synthetic, code, math, QA/forum, PDF, …) — not OWT, not
   DCLM. gigatoken is already tested on those; the untested corpus is *ours*. Include the longest
   documents present, not a length-capped sample.
2. **Both sides, same process, same documents:**
   `HFTokenizer.from_file(dolma2/tokenizer.json).encode_batch(docs, add_special_tokens=False)`
   versus `gigatoken.Tokenizer(dolma2/tokenizer.json).encode_batch_list(docs)`.
   **Load both from the identical `allenai/dolma2-tokenizer/tokenizer.json` file** — the one whose
   sha256 the corpus will record — not from a model repo, and not one from each.
3. **Assert full sequence equality per document**, `list == list`. Not lengths, not counts, not
   set-equality, not a sampled prefix — issue #35's failure was a same-length reordering. Report
   the first mismatching document with byte offset and a ±5-token window.
4. **Pin `tokenizers` to a modern version** (>= 0.21.2, the generation issue #37 associates with
   Unicode 16) **and record it in the corpus provenance.** Run step 3 on that version. Additionally
   run it against `tokenizers==0.20.1` and **report the delta between the two HF versions** — that
   difference is the true size of the Unicode-version exposure on our data, and it is worth knowing
   as a number rather than a worry. If the modern-HF mismatch count is not **exactly zero**, stop.
5. **Explicit adversarial cases appended to the sample**, asserted individually: a NUL byte
   mid-document; a >1 MB single unbroken base64/minified-JS token; the literal `<|endoftext|>` and
   each of the other 21 added tokens inline in text; text with unnormalized/decomposed combining
   marks; a CJK-heavy document; and a document ending mid-multi-byte-sequence-adjacent whitespace.
6. **Determinism:** encode the same 1M docs twice in one process and once with `parallel=False`;
   assert all three id streams are byte-identical. This is the direct check against the
   walker-path nondeterminism class in finding (3), and it is nearly free.
7. **Verify the loaded tokenizer identity** before trusting any of the above:
   `gt.Tokenizer(path).vocab_size == 100278`, its `merges` list has 100,000 entries, and
   `encode("<|endoftext|>") == [100257]`. This catches a mis-parsed legacy merges table directly —
   the failure mode my Q1 analysis flagged and Q-resolved — rather than relying on my static proof.

**Pass condition: zero mismatching documents in steps 3, 5, and 6, on the pinned modern
`tokenizers`.** Cost: one CPU Batch job, well under an hour, negligible against a 26.5 h stage.

If the gate passes, use the **native** `encode_batch_list`/`encode_batch_padded` on `list[str]` —
not `as_hf()` (no benefit, adds issue #44's API papercut and Python object churn) and not
`encode_files` (our documents are in memory, already filtered and deduplicated). Set an explicit
cache bound (per PR #46), choose an AVX-512-capable instance family (`m7i`/`c7i`/`m7a`) since the
x86 mask scanner is AVX-512-gated, and pick deliberately between "1 process × N threads" and
"N processes × `parallel=False`" — output is identical either way, performance is not.

### Addressing the framing directly: is any correctness risk worth it for a non-bottleneck stage?

This is the right question and it deserves a direct answer rather than a hedge.

**The risk is not "small," it is *gated to zero* — and that changes the calculus.** The failure mode
we fear is silent id divergence. But id divergence is exactly the thing a differential test detects
perfectly: it is a deterministic, total, cheap-to-check property. Run 1M of our own documents
through both tokenizers and compare; either they agree everywhere or we learn precisely where they
don't. This is unlike most adoption risks (performance cliffs, memory leaks, rare races) where
testing samples a space it cannot cover. Here the gate is not evidence *about* correctness, it very
nearly *is* correctness for the corpus we are about to build — with the residual being only
"documents unlike the 1M sampled," which stratified sampling from the real corpus makes small.

So the trade is not "speed versus risk." It is **"~25 h and much cheaper re-runs, versus running a
1-hour differential job first."** Framed that way, declining is hard to justify.

**But the coordinator's framing also lands, and it changes the *priority*, not the verdict.** If
tokenization overlaps download, the end-to-end saving may be near zero, and then gigatoken is not
worth *sequencing work around*: it should not block the build, and nobody should be waiting on it.
The right order is therefore:

1. **Measure sustained in-region S3 read bandwidth first.** It is one cheap job, it decides whether
   tokenization is even on the critical path, and we currently do not know it (our only recorded
   figure, 0.8 MiB/s, was out-of-region and is not informative here).
2. If download dominates decisively, **keep HF for this build** and treat gigatoken as an
   optimization for the *next* one. Correctness risk taken for zero wall-clock is a bad trade no
   matter how well-gated.
3. If tokenization binds (or if we expect to re-tokenize — different tokenizer, different mixture,
   a bug found late), **run the gate and adopt.** The option value of a 1 h re-tokenize is
   substantial given our history of finding corpus defects post-publish, and it is the strongest
   non-wall-clock argument for adoption.

### The single strongest argument against my own verdict

**My gate cannot detect the failure it is most needed for, because the divergence is
input-dependent and its trigger set is vanishingly rare.**

The Unicode-version mechanism only fires on codepoints whose General_Category changed between
Unicode 14 and 17 — a set of a few thousand codepoints out of 1.1M, concentrated in recently-added
emoji and scripts. In a 1M-document sample from a web corpus, many of those codepoints will appear
**zero times**. So my gate can return a clean "zero mismatches" and I would call it a pass — and
then the full 1T-token run, being 1000x larger, encounters the rare codepoint a few thousand times
and silently emits different ids in a few thousand documents. **A clean gate is evidence of
low divergence *rate*, not proof of zero divergence.** Worse, the affected documents would be
exactly the multilingual/emoji-heavy tail that nobody eyeballs.

The honest version of my verdict must concede that the only *complete* check is to tokenize the
entire 1T corpus with both implementations and compare — which costs the full 26.5 h HF run and
therefore **destroys the entire reason to adopt gigatoken for this build.** That is a genuine
catch-22, and it is the strongest case for the conservative path: for a corpus this expensive to
rebuild and this hard to audit after the fact, "use the same tokenizer everyone else's ids came
from" has a value that no speedup on a non-bottleneck stage can match.

Two things blunt this, which is why I still land on ADOPT-WITH-GATE rather than DO-NOT-ADOPT — but
neither dissolves it:

- Step 4 of the gate measures the **HF-14-vs-HF-16 delta on our own data**, which bounds the
  exposure empirically rather than leaving it as a worry. If that delta is zero on 1M real
  documents, the trigger set is genuinely absent from our corpus and the concern is quantified, not
  assumed away.
- The maintainer's own argument has real force here: codepoints added after Unicode 14 postdate
  dolma2's training data, so the model has never seen coherent tokenizations of them regardless.
  A divergence confined to that set is closer to noise than corruption.

But if the reviewer's tolerance for silent, unauditable, sub-percent corpus inconsistency is zero —
which for a content-addressed frozen corpus is a defensible position and arguably the position our
own "recompute, never trust" rule implies — then **DO-NOT-ADOPT-FOR-THIS-BUILD is the correct call,
and my verdict is wrong.** The deciding question is not technical: it is how much unverifiable tail
risk a frozen 1T corpus is allowed to carry in exchange for a stage that may not be the bottleneck.

---

## Addendum: does adoption require running THEIR suite against `allenai/dolma2-tokenizer`?

**Yes, and it is the cheaper half of the gate — run it first.** The reasoning is specific:

Their suite is parametrized over `TOKENIZER_SPECS` by *fixture name*, and the `olmo3` fixture
hardcodes a different repo (`tests/conftest.py:144-146` → `allenai/Olmo-3-1025-7B`). I proved that
file is semantically identical to `allenai/dolma2-tokenizer` **except for the merges
serialization** — and the merges format is precisely the one axis where the two differ. So their
green suite tests the list-of-pairs parse; **nothing in the repo has ever exercised the
space-joined parse that our file uses.** My static proof (untagged enum, no-space entries
hard-error, all 100,000 of our entries unambiguous with halves and joins in vocab) says it must
work. That proof deserves to be confirmed by execution, not trusted — which is our own house rule.

Redirecting their suite at our file is a two-line change and turns their entire test corpus into
coverage for our exact bytes:

```python
# tests/conftest.py — point the olmo3 fixture at the tokenizer OUR corpus records
def olmo3_tokenizer_path() -> Path:
    return _hf_tokenizer_json("allenai/dolma2-tokenizer")   # was allenai/Olmo-3-1025-7B
```

Then run, in order of increasing cost:

| Step | Command | What it buys |
|---|---|---|
| A | `pytest tests/tokenizers/test_hf_parity.py -k olmo3` | 64 hand-written strings incl. all 22 added tokens + lookalikes, decode roundtrip, batch==single — **on our merges format** |
| B | `pytest tests/test_encode_dclm.py -k olmo3` | ~20 MB tokenizer-hostile DCLM: CJK/RTL, NFC-divergent, emoji, `\S{80,}`, 128 KB+ docs |
| C | `cargo test --release olmo3` | 78 Rust pretokenizer cases vs `fancy_regex` on the verbatim dolma2 pattern |
| D | `OWT_MAX_BYTES=0 pytest tests/tokenizers/test_hf_parity.py -k "olmo3 and owt"` | full 11.9 GB OWT, id-array equality (needs `~/data/owt_train.txt`) |
| E | `cargo test --release olmo3_matches_regex_owt_full -- --ignored --nocapture` | full 12 GB pretokenizer-vs-regex, token by token |

**Venue: FarmShare or AWS Batch, never this laptop** — per `Capstone_LLM/CLAUDE.md`, and steps D/E
read 12 GB and want many cores regardless. Steps A-C are minutes; D/E are the ones worth a Batch
job. Note steps D and E **silently skip** without the OWT file and the `--ignored` flag
respectively, so check for "PASSED", never for absence of failure — that skip-as-green trap is
exactly the decoration our golden rule warns about.

Their suite (A-E) and my gate (the 7 numbered steps above) are complementary and neither
substitutes for the other: **theirs proves gigatoken agrees with HF on the merges format and
tokenizer file we will actually load; mine proves it agrees on the documents we will actually
tokenize.** Run theirs first because it is cheap and it can fail fast on the one untested axis.

One acceptance condition on top: pin the exact gigatoken version and record it in corpus
provenance, and **re-run A-C plus my step 6 (determinism) on every version bump.** Since their CI
publishes wheels to PyPI on tag without running a single test, our own re-run is the only thing
standing between a regression and our corpus. Treat "gigatoken version" the way our memory says to
treat a wheel version: not a code identity, so pin the artifact, not the number.

### The framing question, answered directly

**Is there any correctness risk that justifies replacing a component that is not the bottleneck?**

For *this* build, on the numbers we have: **no, not yet — and my ADOPT-WITH-GATE is conditional on
a measurement nobody has taken.** Let me be precise rather than split the difference. The gate makes
the risk *acceptable*, not merely small, for one specific reason: **tokenizer parity is a
run-once-then-frozen property.** Unlike a performance regression or a race, the artifact a parity
test certifies never changes afterward — the corpus is content-addressed and immutable, so if the
ids were right at build time they are right forever, and one job's evidence covers the whole life of
the dataset. That is a genuinely unusual and favorable risk shape, and it is why a gate is
sufficient here where it would not be for, say, a training-loop change.

But "acceptable risk" still has to buy something, and **the wall-clock benefit is currently
unmeasured and might be zero.** If download runs 13.5-27 h and tokenization overlaps it, removing a
26.5 h stage saves somewhere between 13 h and nothing. Taking *any* unverifiable tail risk — and the
Unicode-14-vs-17 exposure is genuinely unverifiable short of a full dual run, as I argued above — for
a benefit that could round to zero is a bad trade. So the sequencing is not optional:

1. **Measure sustained in-region S3 read bandwidth.** One cheap job. It decides whether
   tokenization is on the critical path at all. We do not know this number; our only recorded
   figure (0.8 MiB/s) was out-of-region and tells us nothing about it.
2. **If download dominates: keep HF for this build.** Ship the 1T corpus on the tokenizer whose ids
   the rest of the ecosystem produced. Zero new risk, zero wall-clock lost.
3. **If tokenization binds: run the gate (theirs A-C, then mine) and adopt.**

**Does it change for the NEXT build? Yes, and this is where gigatoken clearly wins.** What would
have to be true is already largely true:

- **We expect to re-tokenize.** Our own memory records that ~$600 settles the SuperBPE-vs-dolma2
  tokenizer question via byte-matched A/B corpora, and that two published corpora already have a
  broken EOS check. A tokenizer A/B *requires* tokenizing the same bytes twice or more. At 26.5 h
  per pass that is a scheduling problem; at ~1 h it is a coffee break. Tokenization stops being a
  stage you plan around.
- **The gate is amortized.** Its cost is paid once; every subsequent re-tokenization is free of it
  (modulo a version-bump re-run of A-C).
- **Download is amortized too.** A second build over already-staged bytes has no 6.1 TB download at
  all — at which point tokenization is *unambiguously* the bottleneck and the 20-60x is the whole
  wall-clock story.

So: **not for the corpus we are about to freeze, unless bandwidth says tokenization is the wall.
Yes for the iteration cycle that follows it, where re-tokenization is the point and download is
already paid.** The gate should be run either way, because it is cheap, it is reusable, and the
one thing worse than not adopting gigatoken is adopting it on the assumption that "OLMo 2/3 is in
the README" meant somebody had checked our file.

