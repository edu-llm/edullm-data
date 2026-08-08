# OLMoE-mix-0924 — HF recon (RECON ONLY, no bulk download)

Date: 2026-08-08. All facts below are marked **VERIFIED** (I ran the request and read the bytes)
or **CARD SAYS** (unverified claim from the dataset card). Every number came from a live request.

---

## 0. HEADLINE ANSWER

**The HuggingFace repo `allenai/OLMoE-mix-0924` is RAW TEXT — dolma-format compressed JSON
documents. It is NOT tokenized.** 2,906 files, **7,504,048,280,704 bytes (6.825 TiB / 7.50 TB)**,
of which 96.64% is `.json.zst` DCLM text. There is no `.npy`, `.bin`, `.u32*`, or `preprocessed/`
path anywhere in the repo. VERIFIED by full paginated tree + magic-byte range reads.

**BUT — and this is the ballgame — a pre-tokenized version of the dominant component IS public
and anonymously downloadable**, at `https://olmo-data.org/`, as **headerless raw uint16
little-endian** shards with `.csv.gz` doc-offset sidecars. For a 50B/100B-token subset, which
will be overwhelmingly DCLM, **ingest is copy+rename with NO tokenization**.

The one surprise that changes the plan: the public shards are **uint16, not uint32**. Our
standard supports this (`DTYPE_SIZES["uint16"]=2`), but every size estimate halves and the
extension must be `.u16le.bin`, never `.u32le.bin`.

| question | answer |
|---|---|
| HF repo tokenized? | **No — raw text**, 6.825 TiB, `.json.zst` + `.json.gz` |
| Pre-tokenized public? | **Yes**, for DCLM (96.6% of mix) at `olmo-data.org`, anon-gettable |
| dtype | **uint16 LE, headerless** (NOT uint32 — verified by decode) |
| Tokenizer | `allenai/gpt-neox-olmo-dolma-v1_5`, vocab 50280, EOS **50279** |
| 100B-token subset size | **~200 GB** (uint16), not 400 GB and not 600 GB+ |
| License | ODC-BY v1.0, plus underlying per-component terms |

---

## 1. Full file tree (VERIFIED)

`GET https://huggingface.co/api/datasets/allenai/OLMoE-mix-0924/tree/main?recursive=true&expand=true`
→ HTTP 200. Paginated via the `Link` header: **59 pages**, 50 items each except the last (13).
Raw 2,913 items; **2,913 unique paths, 0 duplicates** (I checked for dupes explicitly, so
pagination genuinely advanced). 7 directories + **2,906 files**.

Sizes use `lfs.size` where present (2,904 of 2,906 files are LFS; only `.gitattributes` and
`README.md` are not).

**Total: 7,504,048,280,704 bytes = 6.825 TiB = 7.504 TB.**

Extensions present — note there is **no `.npy`, no `.bin`, no `.parquet`**:

| extension | count |
|---|---:|
| `.json.zst` | 1,970 |
| `.json.gz` | 920 |
| `.jsonl.gz` | 13 |
| `.md` | 1 |
| `.png` | 1 |
| (none: `.gitattributes`) | 1 |

Directory structure is flat and two-deep: `data/<component>/<shard>`.

```
.gitattributes, README.md, olmoe-mix.png
data/algebraic-stack/   data/dclm/   data/open-web-math/
data/pes2o/             data/starcoder/  data/wiki/
```

## 2. Components in the HF repo (VERIFIED from the tree, not memory)

| dir | files | bytes | TiB | % of repo |
|---|---:|---:|---:|---:|
| `dclm` | 1,970 | 7,251,709,266,488 | 6.595 | 96.64% |
| `pes2o` | 26 | 106,042,211,853 | 0.096 | 1.41% |
| `starcoder` | 863 | 102,890,462,637 | 0.094 | 1.37% |
| `open-web-math` | 26 | 25,913,663,774 | 0.024 | 0.35% |
| `algebraic-stack` | 16 | 11,011,501,789 | 0.010 | 0.15% |
| `wiki` | 2 | 6,481,127,992 | 0.006 | 0.09% |
| (repo root) | 3 | 46,171 | — | 0.00% |
| **total** | **2,906** | **7,504,048,280,704** | **6.825** | 100% |

**Two discrepancies against the card, both real:**

1. **There is NO `arxiv` directory in the HF repo.** The card lists Arxiv at 21.1 B tokens /
   88.8 B bytes, and the training config references 100 `arxiv` tokenized shards, but the HF
   repo ships no arxiv text. The mix as published on HF is **missing a component the card
   claims**. (Actual dirs are exactly the six above — verified from the deduped tree.)
2. **Card total bytes 17.4 T vs repo 7.50 T.** Consistent: card bytes are *uncompressed* UTF-8;
   repo bytes are zstd/gzip *compressed*. ~2.3x ratio is plausible for text. So the raw-text
   path would mean **~7.5 TB to download and ~17.4 TB to decompress and tokenize.**

Naming is not uniform inside a component — `open-web-math` mixes two conventions
(`041.jsonl.gz` … `053.jsonl.gz` alongside `open-web-math-train-0012.json.gz`), so any ingest
script must enumerate from the API, not construct filenames.

Shard size stats (bytes): dclm min 2,934,122,728 / median 3,686,603,509 / max 3,818,368,274;
pes2o max 4,330,774,377; starcoder min 30,222 (tiny per-language files, 863 of them).

## 3. Payload format — VERIFIED by range read

`curl -r 0-63 -L .../resolve/main/<path>`, HTTP 206 on each:

| file | first bytes | verdict |
|---|---|---|
| `data/dclm/dclm-0000.json.zst` | `28b5 2ffd 0058 c4be` | **zstd** (magic `28 b5 2f fd`) |
| `data/wiki/wiki-0000.json.gz` | `1f8b 0800 0000 0000` | **gzip** |
| `data/open-web-math/041.jsonl.gz` | `1f8b 0808 2797 d265 02ff 3034 312e 6a73 6f6e 6c00` | **gzip**, embeds original name `041.jsonl` |
| `data/starcoder/alloy-0000.json.gz` | `1f8b 0800 0000 0000` | **gzip** |

No `\x93NUMPY`, no `PAR1`. **This is compressed text, definitively.**

Decompressed `data/wiki/wiki-0000.json.gz` (first 512 KB range → 1,562,677 bytes out) and
parsed the first line. It is **standard dolma JSONL**, one JSON object per line:

```
keys: ['added', 'created', 'id', 'metadata', 'source', 'text', 'version']
  added:    '2023-04-25T09:58:21.172Z'
  created:  '2023-04-25T09:58:21.172Z'
  id:       '5'
  metadata: {'length': 541, 'provenance': 'en_simple_wiki_v0-0000.json.gz:1',
             'revid': '3382164', 'url': 'https://en.wikibooks.org/wiki?curid=5'}
  source:   'wikipedia'
  text:     'Organic Chemistry/Cover\n\nWelcome to the world\'s foremost open content...'
  version:  'v0'
```

Note the card's `dataset_info.features` declares only `id/text/added/created` — the actual
payload also carries `metadata`, `source`, `version`. The card understates the schema.

## 4. Declared token counts + tokenizer

**CARD SAYS** (verbatim table from `README.md`):

| Subset | Tokens | Words | Bytes | Docs |
|---|---|---|---|---|
| DCLM Baseline 1.0 | 3.86 T | 3.38 T | 16.7 T | 2.95 B |
| Starcoder | 101 B | 63.9 B | 325 B | 78.7 M |
| peS2o (Dolma) | 57.2 B | 51.3 B | 268 B | 38.8 M |
| Arxiv (RedPajama v1 via Proof Pile II) | 21.1 B | 23.5 B | 88.8 B | 1.55 M |
| OpenWebMath (Proof Pile II) | 12.7 B | 10.2 B | 42.4 B | 2.91 M |
| Algebraic Stack (Proof Pile II) | 12.6 B | 9.6 B | 39.3 B | 2.83 M |
| En Wikipedia + Wikibooks (Dolma) | 3.69 B | 3.16 B | 16.2 B | 6.17 M |
| **Total** | **4.07 T** | **3.53 T** | **17.4 T** | **3.08 B** |

**The card does NOT name a tokenizer anywhere.** The only mention of tokenization is in
Preprocessing, verbatim:

> All subsets were pre-processed to remove documents with a *sequence* of 32 or more repeated
> *ngrams*.
> - a *ngram* is a span of 1 to 13 tokens, included;
> - *tokens* are obtained using the model tokenizer;
> - a *sequence* is a contiguous span of repeated ngrams.

"the model tokenizer" is an unresolved reference on the card. **I resolved it from the training
config instead** (§6). Is the Tokens column a count or an estimate? **It is a real count, at 3
significant figures.** My independent measurement of the tokenized DCLM shards gives
3,857,122,337,647 tokens = 3.857 T against the card's 3.86 T — **ratio 0.9993**. That agreement
is far too tight to be arithmetic from bytes, so the column is a genuine tokenizer count,
rounded. Note it is only reliable *for the tokenizer AI2 used*; it does not transfer to a
different tokenizer.

## 5. License (verbatim)

Card frontmatter: `license: odc-by`. Body, verbatim:

> ## Licensing Information
>
> This mix is licensed under [Open Data Commons Attribution License (ODC-By) v1.0](https://opendatacommons.org/licenses/by/1-0/). By using this dataset, you are bound to licenses and Terms of Services of underlying datasets, which you can access by clicking on the links in the table above.

HF API confirms `"tags": [..., "license:odc-by", ...]`, `"gated": false`, `"private": false` —
**no gate, no click-through**. Heterogeneous downstream terms apply per component and the card
binds us to each: DCLM-baseline-1.0, bigcode/starcoderdata, allenai/peS2o, RedPajama-Data-1T,
EleutherAI/proof-pile-2. **Starcoder is the one to check before publishing** — BigCode carries
its own agreement and opt-out regime; I did not fetch those component licenses (out of scope for
this recon). ODC-BY requires attribution, which our generated README can carry.

## 6. IS THERE A PRE-TOKENIZED VERSION? — YES (the decisive finding)

### 6a. Not in the HF repo
Zero `.npy`, `.bin`, `.u32*`, `.u16*`, or `preprocessed/` paths in all 2,906 files. VERIFIED
from the deduped tree.

### 6b. The training config names the exact shard paths
`https://raw.githubusercontent.com/allenai/OLMoE/main/configs/OLMoE-1B-7B-0924.yml` → HTTP 200,
1,494 lines, and it **inlines the full path list**: **1,285 `.npy` paths** (1,271 train + 14
eval).

**Tokenizer, verbatim from the config (line 68-70):**
```yaml
tokenizer:
  identifier: tokenizers/allenai_gpt-neox-olmo-dolma-v1_5.json
  truncate_direction: right
```
and (lines 31-35): `max_sequence_length: 4096`, `vocab_size: 50280`, `embedding_size: 50304`,
`eos_token_id: 0`, `pad_token_id: 1`. **`max_duration: 2ep`** — AI2 trained 2 epochs on this mix.

Train paths group into exactly two roots (this is where the missing-arxiv puzzle resolves — the
tokenized mix HAS arxiv, the HF text repo does not):

| root | shards | component |
|---|---:|---|
| `preprocessed/fastdclm/text_openhermes_reddit_eli5_vs_rw_v2_bigram_200k_train/allenai/` | **941** | DCLM |
| `preprocessed/olmo-mix/danyh-compiled-v1_7/starcoder/allenai/gpt-neox-olmo-dolma-v1_5/` | 155 | starcoder |
| `…/arxiv/allenai/gpt-neox-olmo-dolma-v1_5/` | 100 | arxiv |
| `…/pes2o/…` | 43 | peS2o |
| `…/algebraic-stack/…` | 16 | algebraic-stack |
| `…/open-web-math/…` | 13 | open-web-math |
| `…/wiki/…` | 6 (3 distinct) | wiki |

The `-s3.yml` sibling config gives the same list as `s3://ai2-llm/...` URLs.

### 6c. `s3://ai2-llm` is PRIVATE
Unauthenticated attempts, all recorded:
- `https://ai2-llm.s3.us-west-2.amazonaws.com/?list-type=2&prefix=preprocessed/...` → **HTTP 301**
  `PermanentRedirect` (wrong endpoint; bucket is not in us-west-2 for path purposes).
- `https://ai2-llm.s3.amazonaws.com/?list-type=2&prefix=...` (4 prefixes incl. empty) → **HTTP 403
  `AccessDenied`** every time. Not listable.
- `GET https://ai2-llm.s3.amazonaws.com/preprocessed/olmo-mix/danyh-compiled-v1_7/wiki/allenai/gpt-neox-olmo-dolma-v1_5/part-0-00000.npy`
  → **HTTP 403**; `HEAD` → **HTTP 403 Forbidden**.

**So the S3 bucket is a dead end.** Do not plan against it.

### 6d. `https://olmo-data.org/` — PUBLIC, and it has the DCLM shards
Not listable (`GET /` → 404; `?list-type=2&prefix=…` returns a Cloudflare "Not Found" HTML page,
so it is a CDN in front of the bucket, not an S3 REST endpoint). **But individual objects GET
anonymously.** The key is that the mirror path is the S3 key with the bucket stripped.

**VERIFIED public and gettable — DCLM (941 shards, the 96.6% component):**

```
https://olmo-data.org/preprocessed/fastdclm/text_openhermes_reddit_eli5_vs_rw_v2_bigram_200k_train/allenai/part-<NNN>-<IIIII>.npy
```
`HEAD part-000-00000.npy` → **HTTP 200**, `content-length: 8589919036`,
`last-modified: Fri, 16 Aug 2024 21:35:34 GMT`, `accept-ranges: bytes`, `server: cloudflare`,
`etag: "7d5a6d500c6fee4f26fdb5bd017402bb-1024"` (1,024-part multipart upload).

**I swept all 941 config-named DCLM paths with HEAD. 941/941 returned HTTP 200 with a
content-length** (27 came back empty on the first 40-way pass and all 27 resolved to 200 on
retry — transient concurrency, not missing objects).

- **Total: 7,714,244,675,294 bytes = 7.0161 TiB = 7.714 TB**
- min 466,770,182 / median 8,589,931,314 / max 8,589,934,584 bytes
- 753 of 941 shards are at the ~8.59 GB cap (8,589,934,592 = 8 GiB)
- **All 941 sizes are even**, consistent with a 2-byte dtype
- Numbering: parts `000`–`187` (188 distinct), each with 5 sub-shards except one with 6
- Extent probes: `part-199` → 404, `part-200` → 404, `part-000-00005` → 404. So the config list
  is the complete set, not a subset.

**Other components: NOT mirrored (except wiki).** I probed each component under both
`…/danyh-compiled-v1_7/<comp>/…` and `…/danyh-compiled-v1_7/documents/<comp>/…`, for both
`gpt-neox-olmo-dolma-v1_5` and `dolma2-tokenizer`, across three part-name conventions:

- **HIT:** `preprocessed/olmo-mix/danyh-compiled-v1_7/documents/wiki/allenai/dolma2-tokenizer/part-0-00000.npy`
  → HTTP 200, `content-length: 4989292812`. Sidecar `part-0-00000.csv.gz` → HTTP 200.
  **Caveat: this is the dolma2-tokenizer (vocab ~100278) build, NOT the v1_5 build OLMoE trained
  on.** Verified: `<u4` max 100257 on a 200 KB sample, all < 100278 — so it is uint32/dolma2, a
  *different tokenizer* from the DCLM shards. **Do not mix it with the DCLM uint16 shards.**
- **404 for everything under `gpt-neox-olmo-dolma-v1_5`** for all six components (12 probes, all
  404) — the v1_5 tokenizer directory is not on the mirror for the `olmo-mix` root.
- **404 for starcoder / arxiv / pes2o / algebraic-stack / open-web-math** under both layouts and
  both tokenizers.

**Consequence:** the only OLMoE-0924-consistent tokenized data that is public is **DCLM**. That
is fine for our purpose — see §8.

## 7. The tokenized shards: naming, dtype, shard size, sidecars (VERIFIED)

**Naming.** `part-<NNN>-<IIIII>.npy` — 3-digit part, 5-digit sub-shard. The `.npy` extension is a
**lie**, exactly the ".npy lie" this repo already knows: these are headerless raw integers.

**NO `.npy` header — verified on two independent shards:**
```
part-000-00000.npy  first 16B: d817 3605 ac5c 0d00 2254 1101 4201 0f00
part-125-00001.npy  first  8B: 235e a70d 0d00 9d12
```
Neither begins with `\x93NUMPY`. Headerless raw. ✅ This is the form we want.

**dtype is uint16 LE — NOT uint32.** This is the one place I expected uint32 and the bytes say
otherwise, so I tested it three ways.

1. Divisibility: 8,589,919,036 is divisible by 2 and by 4, not by 8 — inconclusive alone.
2. In-vocab test on a 200 KB mid-file sample (byte offset 4,000,000,000):
   | interpretation | n | max | fraction < 50280 |
   |---|---:|---:|---:|
   | `<u2` (uint16) | 100,000 | **50,279** | **1.000000** |
   | `<u4` (uint32) | 50,000 | 3,295,126,437 | 0.000000 |
   `max == 50279 == vocab_size - 1` exactly, with 100% of ids in range. uint32 gives garbage.
3. **Decode test** — the proof. Reading `<u2` and decoding with the config's tokenizer yields
   fluent English:
   > `' many of the direct sound clips from the film work in a musical context as well, though it can feel a little distracting in the context of a song on its own. Like Goblin before him, Yorke's true test comes in the more pop-rock tracks like "Suspirium" where balances stirring riffs with his devastating vocals...'`

   Second, independent shard (`part-125-00001.npy` at offset 1,000,000,000): n=16,384, max=50,279,
   all < 50280, decodes to
   > `' for short, medium and long distances are set at 150 degrees, 54 degrees and 28 degrees respectively.\n\nIn addition, the car is equipped with 77GHz millimeter wave radar for the front, 24GHz millimeter wave radar at the four corners of the car, four cameras for the Around View'`

**So: dtype `uint16`, byte order `little`, headerless. Our extension must be `.u16le.bin`.**

**Tokenizer confirmed by decode.** `https://huggingface.co/allenai/gpt-neox-olmo-dolma-v1_5/resolve/main/tokenizer.json`
→ HTTP 200 (2,115,113 bytes; the un-`-L` request returns 307, follow it).
`get_vocab_size() == 50280`, matching the config exactly. `id 253 = 'Ġthe'`, `id 273 = 'Ġof'` —
GPT-NeoX BPE. This is a real HF repo we can pin.

**EOS is 50279, NOT 0 — the config's `eos_token_id: 0` is wrong for the data.** Verified:
`added_tokens` are `(0, '|||IP_ADDRESS|||')`, `(1, '<|padding|>')`, and `id 50279` decodes to
`'<|endoftext|>'`. In a 1 MB (524,288-token) mid-file sample:

| id | count | fraction |
|---|---:|---:|
| 0 | 0 | 0.000000 |
| 1 | 0 | 0.000000 |
| **50279** | **404** | **0.000771** → mean doc length **1,297.7 tokens** |

Cross-checked against the sidecar at the file head: the sidecar says doc 1 spans tokens [0,249),
and **token index 248 is exactly 50279 `<|endoftext|>`** while 249 starts new text (`'<<'`,
`'Up'`, `' Contents'`). Doc boundaries are real, and EOS-terminated. Use **50279** as the EOS in
our tokenizer metadata; passing 0 would be a silent lie.

**Sidecars EXIST — `.csv.gz` next to every shard.** `part-000-00000.csv.gz` → HTTP 200,
`content-length: 121670466` (~122 MB); `part-125-00001.csv.gz` → HTTP 200, 119,760,026 bytes.
`part-000-00000.npy.csv.gz` → 404 and `.csv` → 404, so the naming is **basename + `.csv.gz`**
(replace the extension, don't append). Note memory's warning that `.u32le.bin`-style renaming
breaks remote sidecar lookup applies here — renaming to `.u16le.bin` will desynchronize the
sidecar basename unless we rename the sidecar too.

Decoded the first 200 KB of the sidecar (gzip, 1,253,400 bytes out, 5,527 lines). **Schema is
4-column, headerless CSV: `start_token_offset,end_token_offset,sha256_hex_docid,source_path`:**
```
0,249,edb2863252c8f3262a85c9c8a8fc6bff1b0d8078,/home/ubuntu/fasttext_openhermes_reddit_eli5_vs_rw_v2_bigram_200k_train/processed_data/global-shard_01_of_10/local-shard_9_of_10/shard_00000007_processed
249,1150,850113d9ad24efd511c32394e4738f1817384fa8,.../global-shard_03_of_10/local-shard_0_of_10/shard_00000054_processed
1150,2279,579c4fff56ce9631a0a94b557fa6336085731089,.../global-shard_01_of_10/local-shard_8_of_10/shard_00000243_proce
```
Offsets are **token** offsets (not bytes) and are contiguous/monotonic — so we get exact doc
boundaries and a stable per-doc id for free. The `source_path` leaks AI2's build host paths and
shows the DCLM fasttext-filter provenance (`openhermes_reddit_eli5_vs_rw_v2_bigram_200k`).

## 8. What this means for the 50B / 100B subsets

**Token accounting.** Public DCLM pool = 7,714,244,675,294 bytes / 2 = **3,857,122,337,647 tokens
(3.857 T)**. This independently reproduces the card's 3.86 T for DCLM (ratio 0.9993) — strong
mutual confirmation that both the card's count and my uint16 reading are right.

**Size of what we actually need (uint16 → 2 bytes/token):**

| subset | bytes | size | full 8 GiB shards |
|---|---:|---|---:|
| 50 B tokens | 100,000,000,000 | **~100 GB (0.09 TiB)** | ~11.6 |
| 100 B tokens | 200,000,000,000 | **~200 GB (0.18 TiB)** | ~23.3 |

So to answer the sizing question directly: **a 100B-token subset is ~200 GB, not ~400 GB
(uint32) and not ~600 GB+ (compressed text).** The uint16 dtype halves the naive estimate. We
need ~24 of 941 shards — **2.5% of the pool** — so we can take whole shards and never split one.

**Ingest is copy + rename, no tokenization**, for a DCLM-only subset. Cost is an in-region
S3 copy of ~200 GB from an anonymous HTTPS origin (Cloudflare, `accept-ranges: bytes`, so
parallel ranged GETs work).

**Caveats that will bite if ignored:**
1. **`.u16le.bin`, not `.u32le.bin`.** `tokens × 2 == file bytes`. Our `DTYPE_SIZES["uint16"]=2`
   handles it; `_min_dtype_size_for_vocab(50280)` returns exactly 2, so the dtype-vs-vocab gate
   passes (it is one-sided — too-narrow fails, and 2 is precisely wide enough for max id 50279).
2. **EOS = 50279.** EOS fraction measured **0.000771**, comfortably under our 0.05 bound, so the
   eos-fraction gate passes with ~65x margin. But declare 50279, not the config's 0.
3. **A DCLM-only subset is 100% web text** — no code, no math, no wiki, because only DCLM is
   mirrored under the v1_5 tokenizer. If the 50B/100B subsets must be *mixtures* mirroring the
   card's proportions, the non-DCLM components are **not available pre-tokenized** and would
   have to be tokenized from HF text (~0.24 TiB compressed for all five non-DCLM dirs — small,
   but it needs the v1_5 tokenizer to stay consistent, and arxiv text is absent from HF entirely).
4. **Rename the sidecar with the shard** or remote doc-offset lookup silently breaks.
5. **Two tokenizers are in play on the mirror.** DCLM = v1_5/uint16/vocab 50280; the mirrored
   `wiki` = dolma2-tokenizer/uint32/vocab 100278. Mixing them in one group is unreadable — our
   reader takes one explicit dtype per read.
6. **This is not decontaminated for our evals**, and DCLM's fasttext filter is tuned toward
   OpenHermes/Reddit/ELI5-style text (per the path name), which is a known contamination-adjacent
   signal. Our pipeline has no decontam.

## 9. Request log (status codes, for auditability)

| request | status |
|---|---|
| `api/datasets/allenai/OLMoE-mix-0924` | 200 |
| `api/datasets/.../tree/main?recursive=true&expand=true` ×59 pages | 200 (2,913 items, 0 dupes) |
| `datasets/.../raw/main/README.md` | 200 (4,220 bytes) |
| range reads of 4 payload files | 206 each |
| `api/models/allenai/OLMoE-1B-7B-0924/tree/main` | **429 rate-limited** (IP-limited, needs HF_TOKEN) — worked around via the GitHub config, which is strictly better evidence anyway |
| `api.github.com/repos/allenai/OLMoE/git/trees/main?recursive=1` | 200 |
| `raw.githubusercontent.com/.../configs/OLMoE-1B-7B-0924.yml` | 200 |
| `raw.githubusercontent.com/.../configs/ablations/olmoe-8x1b-newhp-newds-final-s3.yml` | 200 |
| `github.com/allenai/OLMoE/.../tokenizers/allenai_gpt-neox-olmo-dolma-v1_5.json` | 404 (config path is Beaker-internal, not in the repo) |
| `hf allenai/gpt-neox-olmo-dolma-v1_5/tokenizer.json` | 307 → 200 with `-L` |
| `ai2-llm.s3.us-west-2.amazonaws.com` list ×4 | 301 PermanentRedirect |
| `ai2-llm.s3.amazonaws.com` list ×4 prefixes | 403 AccessDenied |
| `ai2-llm.s3.amazonaws.com` GET/HEAD wiki npy | 403 |
| `olmo-data.org/` root; `?list-type=2` | 404 (CDN, not listable) |
| `olmo-data.org` HEAD ×941 DCLM shards | **200 ×941** |
| `olmo-data.org` DCLM `.csv.gz` sidecars ×2 | 200, 200 |
| `olmo-data.org` non-DCLM component probes (12 v1_5 + 18 dolma2 + 3 no-`documents/`) | 404 except wiki/dolma2 → 200 |
| `olmo-data.org` extent probes `part-199`, `part-200`, `part-000-00005` | 404 (confirms complete set) |

**Not verified / open:** per-component upstream licenses (esp. BigCode/starcoderdata) were not
fetched; whether a v1_5-tokenized starcoder/arxiv/pes2o exists at some other unguessed mirror
prefix (I probed the config-named layouts and two tokenizer dirs, not an exhaustive search — the
mirror is not listable, so absence of a HIT is not proof of absence); and no payload byte was
re-hashed against any published digest (AI2 publishes none).
