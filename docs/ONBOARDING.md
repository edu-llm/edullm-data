# eduLLM datasets — the 2-minute version

For someone brand new to the data pipeline. The full spec is
[`dataset-creation/DATASET-STANDARD.md`](dataset-creation/DATASET-STANDARD.md);
this is just the shape and the rules.

```
TWO BUCKETS, A ONE-WAY DOOR
  you ─write─►  edullm-landing  ─►  [VALIDATOR]  ─►  edullm-data  ─read─►  training
                (scratch inbox,      re-checks         (the library;
                 14-day expiry)      every byte         only the validator
                                                        can write here)

  You drop a dataset in `landing`. A robot re-checks it and, only if it passes,
  copies it into `edullm-data`. You physically cannot write to `edullm-data`
  (blocked at the AWS level). So anything in there is already trustworthy.


WHAT edullm-data LOOKS LIKE NOW            (refreshed 2026-08-01)
  s3://edullm-data/
  ├── pretrain/olmo-150b-dolma2/v1/      ← 157.5B tokens, 6,911 shards, 586.6 GiB
  │   ├── dataset.json      the metadata "index card"
  │   ├── README.md         human-readable, auto-generated
  │   ├── _VALIDATED.json    "passed the validator" seal
  │   └── tokens/           the payload — NESTED: tokens/<source>/<domain>/
  │       ├── manifest.json  lists every file + size + checksum
  │       └── dclm/.../train-00000.u32le.bin ...   (6,851 train + 60 val)
  ├── pretrain/regmix-10b/v1/            ← 7 sources; the corpus a real training run read
  ├── tokenizer/dolma2-bpe/v1/           ← the tokenizer those tokens were made with
  └── _catalog/             one tiny JSON per dataset (the card catalog)

  ~~pretrain/olmo-mix-1124-31b/v1 — 31.3B tokens, 218 files, ~125 GB, flat tokens/~~
  DELETED 2026-07-29 (no val split, and frozen means it could never gain one). It was this
  file's running example; both the id and the flat layout are gone. More than two datasets
  are published now — `_catalog/` is the list, never this diagram.


EVERY DATASET HAS THE SAME ADDRESS SHAPE
  s3://edullm-data/ <family> / <name> / <version> /
     family   fixed list: pretrain, curriculum, sft, eval, probe, vendor, tokenizer
     name     kebab-case, 2–5 words (olmo-mix-1124-31b).  no dates/versions/vague words
     version  v1, v2 ...  assigned automatically, immutable once published

  Inside: dataset.json (the index card) + one or more groups (e.g. tokens/),
  each group with a manifest.json listing every file's size + checksum.


WHAT THE PIPELINE FORCES (can't publish otherwise)
  • Naming: family from the fixed 7; name kebab-case 2–5 words; no dates,
    version tokens, names, or vague words. Version is auto-assigned.
  • Integrity ("recompute, never trust"): validator HEADs every file and compares
    the real object size to the manifest's `bytes`; profiles then read ~64 KB per
    shard and decode it. It does NOT re-hash payload bytes — `sha256` is written
    once by the producer, and its job is content addressing (duplicate-shard and
    shared-with-parent detection) plus the manifest hash chain, which IS recomputed.
    Manifest and bucket must match both directions — no missing files, no stray
    unlisted ones.
  • Token files: (tokens × 4 bytes) must equal file size exactly. Extension must
    match real bytes → token shards are `.u32le.bin`, never `.npy`.
  • Tokenizer: a corpus must point at a PUBLISHED tokenizer, pinned by checksum;
    the validator derives vocab size itself and checks the token IDs against it.
  • Immutable: never edit v1, publish v2. Only the validator writes edullm-data.


USING IT
  # list what exists
  aws s3 ls s3://edullm-data/_catalog/ --recursive

  # read into training (gives file URIs AND the correct dtype)
  from edullm_data.read import dataset_paths, resolve_latest
  ver = resolve_latest("pretrain/olmo-150b-dolma2", s3=s3)
  r   = dataset_paths("pretrain/olmo-150b-dolma2", ver, split="train", s3=s3)
  # r.paths -> the 6,851 train URIs
  # r.numpy_dtype -> "<u4"  ← FEED THIS ONE to the loader
  # r.dtype -> "uint32"  (the declared NAME; np.dtype("uint32") is host byte order)

  # publish: you type 4 things, the validator does the rest
  publish(source, dataset_id="pretrain/my-corpus-10b",
          purpose="what it is, what uses it, what it decides",
          profile="pretrain-tokens/v1", ...)
```

**Why so strict:** every rule maps to a real bug that shipped once — an
`inventory.json` claiming 98 files / 172 GB in a folder holding 10 files / 31 GB,
and `.npy` files that were secretly raw bytes (silently corrupted training). The
validator never believes a claimed number; it re-derives it from the bytes.
