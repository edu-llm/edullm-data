# 03 — Code path recon: ingesting `allenai/OLMoE-mix-0924` as two pretrain releases

**Scope.** READ-ONLY recon of the worktree
`/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/final-dataset` (branch `final-dataset`,
tip `8e2524a`, package version `0.9.1` per `pyproject.toml:7`). Goal: the fastest existing code path
to publish two releases (~50B and ~100B tokens) from an external, already-processed corpus, with
dedup and decontamination deliberately skipped.

---

## 0. The headline finding, which reframes the whole question

**`allenai/OLMoE-mix-0924` is NOT pre-tokenized. It is raw text.** Verified live against
`https://huggingface.co/api/datasets/allenai/OLMoE-mix-0924`: every payload file is
`.json.gz`, `.jsonl.gz`, or `.json.zst`; the declared schema is `{id, text, added, created}` with
`text: string`; there is no tokenizer file and no `.npy`/`.bin` anywhere. The card reports token
*counts* only, and names no tokenizer.

So there is **no copy+rename fast path for this repo.** The task is not "ingest an already-processed
corpus" — it is a **full tokenization build**, which is the expensive path
(`corpus_build.py` + `corpus_pack.py`, measured at 72,615 tok/s/vCPU).

This is already recorded in the repo, precisely: `artifacts/impl-plan/source-encoding-audit.md:1214-1219`
draws exactly this distinction for the sibling dolma3 mixes —

> "that memory note is about AI2's **pre-tokenized `.npy`/uint32 shards** being byte-compatible with
> our `.u32le.bin`. **These `dolma3_*_mix` HF repos are the TEXT form.** Both exist. **If you want the
> pre-tokenized route (copy+rename, no re-tokenization), it is NOT these HF repos — it is AI2's S3
> shards.**"

**A second hard blocker on top of that.** The DCLM subset — 1,970 of the ~2,100+ files, i.e. the bulk
of the mix — is `.json.zst`. **This package cannot read zstd.** The reader registry is
`corpus_read.py:846-850` (`parquet`, `json.gz`, `jsonl.gz` only), and `corpus_read.py:896-902` refuses
it by name:

> "`.zst` is NOT among them — Common Pile ships some prefixes as `.json.zst`, which needs a
> zstandard dependency this package does not declare."

`corpus_build.py:361` repeats it. There is no `zstandard` in `pyproject.toml:27-82`. Reading DCLM
therefore needs a new dependency plus a new reader plus an entry in `corpus_build._PAYLOAD_EXT`
(`corpus_build.py:1549-1554`, enforced at import by `_assert_payload_extensions_cover_readers`,
`corpus_build.py:1556-1578`).

**If a pre-tokenized AI2 mirror can be located** (AI2's own S3, not HF), then a genuine fast path
exists and §1.3 below describes it. Everything else in this document is written for both cases.

---

## 1. Existing ingesters

Grep coverage: `ingest`, `vendor`, `hf_hub`, `huggingface`, `snapshot_download`,
`datasets.load_dataset`, `dolma`. There is **no** use of `huggingface_hub`, `snapshot_download`, or
`datasets.load_dataset` anywhere in `src/` — every upstream read is hand-rolled HTTP Range over
`urllib`.

### 1.1 `src/edullm_data/ingest_prm800k.py` (1,096 lines) — the closest working template

Two subcommands, CLI `edullm-prm800k-ingest` (`pyproject.toml:115`), `main()` at
`ingest_prm800k.py:1016`.

- `stage_prm800k(*, s3, run_id, landing_bucket="edullm-landing", retrieved_at=None, opener=None, part_size=16MiB, timeout_seconds=60, _spec=PRM800K_SOURCE) -> StageResult`
  — `ingest_prm800k.py:557-688`.
  **Streams HF → S3 with no local disk at all.** `_HashingReader` (`:151-189`) wraps the HTTP body,
  computes sha256 as bytes pass through, and hard-fails the moment the transport exceeds the pinned
  byte count (`:173-182`). Upload is `s3.put_stream` (`:617`, implementation `s3.py:469-540`) — one
  bounded multipart part in memory. **Not parallel** — a serial `for file in _spec.files` loop
  (`:613`). Writes to a non-triggering `_staging/<dataset_id>/<run_id>/payload/raw/...` prefix
  (`:297-311`) plus a `receipt.json` *beside* payload, never beneath it (`:652-655`).
- `publish_prm800k(*, s3, run_id, created_at=None, landing_bucket, data_bucket, env=None, _spec=PRM800K_SOURCE) -> PublishResult`
  — `ingest_prm800k.py:903-1006`. Re-verifies the receipt against S3, then calls `publish()` with
  `profile="vendored/v1"`, `expected_payload=`, `expected_version="v1"`.
- Batch-only: `_require_batch_environment()` (`:1009-1013`) refuses without `AWS_BATCH_JOB_ID`.

**Why it is only a partial template.** It is hard-wired to a single pinned four-file release with
code-embedded SHA-256 witnesses (`PRM800K_SOURCE`, `:102-133`), and the validator carries a
*second*, code-pinned contract check for it (`validate.py:894-1000`,
`_check_pinned_prm800k_contract`) that fires on `dataset_id == "vendor/openai-prm800k"`. The
*structure* — stage-with-witnesses, then publish — is reusable; the specifics are not.

### 1.2 `src/edullm_data/ingest_reservoir.py` (1,094 lines) — the HF transport machinery worth stealing

CLI `edullm-ingest-reservoir` (`pyproject.toml:107`), `main()` at `:1084`. Three subcommands: `plan`
(`:782`), `ids` (`:845`), `merge` (`:957`).

**This module does NOT ingest payload.** It scans FinePhrase `id` columns and writes a
`uint64` anti-join set. Its value here is the transport layer:

- `_RangeFile` (`:299-483`) — a seekable read-only file over HTTP Range, so pyarrow fetches footers
  and chosen column chunks only. Three load-bearing details: `_cdn_url()` (`:313-372`) resolves the
  metered `huggingface.co/.../resolve/...` control-plane URL to the **unmetered** signed CDN URL
  once per file (a 70x reduction in metered requests, `_CDN_TTL_S = 3000` at `:218`); `read()`
  (`:387-424`) **loops until `n` bytes are satisfied** because a short read segfaulted three of four
  array children; `_RateGate` (`:250-296`) is a process-wide 429 brake.
- `hf_tree(repo, path="", *, headers=None)` (`:486-536`) — paginated `.parquet` listing. **Do not add
  `expand=1`** (50x pessimisation, measured).
- Parallel via `concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)` (`:890`),
  default 8 (`:1049`).
- Batch-only: `_require_batch(allow_local)` (`:105-119`) — `--allow-local` exists for the
  metadata-only `plan` subcommand only.
- Refuses to stage under a prefix with no lifecycle rule: `_assert_lifecycle_covers` (`:139-165`),
  `--require-lifecycle` default on (`:1072`).

### 1.3 `src/edullm_data/profiles/vendored_v1.py` (464 lines) — the byte-preserving mirror profile

`NAME = "vendored/v1"` (`:23`). `REQUIRED_FIELDS` (`:25-35`): `vendor_root` (string), `upstream`
(object requiring `name`, `uri`, `revision`, `retrieved_at`), `sentinels` (array),
`upstream_files` (array of `{path, bytes, sha256}`).

Three checks (`CHECKS`, `:450-454`):
1. `check_vendor_metadata` (`:100-198`) — non-placeholder `vendor_root`, concrete upstream, every
   manifest path under the declared root, every declared sentinel really present.
2. `check_upstream_file_witnesses` (`:201-367`) — **the only profile in the repo that
   stream-hashes the full payload at Gate A** (`ctx.s3.hash_object`, `:312-314`), with an ETag
   before/after to catch mid-validation mutation (`:325-339`). Records the observed digest+ETag into
   `ctx.observations["vendored_payloads"]` (`:344-350`), which `promote()` then uses as
   `CopySourceIfMatch` (`validate.py:2059-2066`).
3. `check_jsonl_samples` (`:370-447`) — parses ≤64 KiB / ≤16 records of each `.jsonl`.

**Naming is exempt** for vendored trees: `validate.py:679-681` sets `profile_is_vendored` for
`vendored/` and `tokenizer/` prefixes, which suppresses `check_shard_naming`
(`validate.py:742-745`), `_check_split_matches_filename` (`:1354-1355`), and
`_check_labels_match_path`. `families/vendor.json` sets `validation_required: false`, so no
held-out split is required.

**Critical limitation for our use case:** a `vendored/v1` group is **not readable as training data**.
`read.dataset_paths` raises `VendoredReadRequiresOptIn` unless `allow_vendored=True`
(`read.py:465-470`), and that flag is documented for "an explicit transformation or
provenance-audit job." A vendored mirror is a provenance artifact, not a train-ready release.

**⚠️ And `vendored/v1` cannot be used to shortcut a token corpus.** Gate A's stream-hash of every
payload object means a 50B-token corpus (2,000 shards × 100 MB = 200 GB) gets **fully re-read** at
validation — vastly more expensive than `pretrain-tokens/v1`'s 64 KB-per-shard sample.

### 1.4 The one real copy+rename story, and it is not in this repo

Nothing in `src/` copies pre-tokenized shards. The nearest thing is `promote()`'s server-side copy
(`validate.py:2052-2110`, `s3.copy` at `s3.py:543-578`, which switches to `_multipart_copy` above
`_MULTIPART_COPY_THRESHOLD = 5 GiB`, `s3.py:175`) — but that is landing→data inside the airlock, not
an external ingest.

If pre-tokenized AI2 shards can be sourced, the fast path is:
`s3.copy` (or `s3.put_stream`) external → `edullm-landing/_ingest/...` with renamed keys →
`publish(source="s3://edullm-landing/_ingest/...", profile="pretrain-tokens/v1", tokenizer=...)`.
No `corpus_build`, no `corpus_pack`, no tokenizer run. `_count_for` (`publish.py:157-184`) derives
the token count from the object size arithmetically — **zero bytes read** for raw fixed-width.

### 1.5 `[project.scripts]` — the complete list (`pyproject.toml:87-115`)

| entry point | module | purpose |
|---|---|---|
| `edullm-data-validate` | `edullm_data.validate:main` | Gate A |
| `wu-fsck` | `edullm_data.fsck:main` | Gate B |
| `edullm-ingest-reservoir` | `edullm_data.ingest_reservoir:main` | FinePhrase id scan |
| `edullm-corpus-build` | `edullm_data.corpus_build:main` | plan → run → verify |
| `edullm-prm800k-ingest` | `edullm_data.ingest_prm800k:main` | PRM800K vendor mirror |

There is **no generic ingest CLI.** `edullm_data/cli.py` has never existed (`pyproject.toml:88-92`).

---

## 2. The tokenization path

`plan → run → verify`, driven by `edullm-corpus-build` (`corpus_build.py:main` at `:1868`ish;
subcommands `_cmd_plan:1367`, `_cmd_run:1397`, `_cmd_verify:1467`).

### 2.1 Pipeline, in order (`run_bundle`, `corpus_build.py:989-1293`)

| stage | call site | function |
|---|---|---|
| read | `:1082` via `documents=_reader_for` (`:1735`) | `corpus_read.read_documents` (`corpus_read.py:878-903`) |
| carve (train/val) | `:1082` | `corpus.carve` (`corpus.py:570-580`) → `is_held_out` (`corpus.py:533-567`) |
| **dedup + decontaminate** | `:1101-1103` | `corpus_filter.dedup_and_decontaminate` (`corpus_filter.py:299-327`) |
| tokenize + length filter | `:1121-1124` | `corpus_pack.tokenize_documents` (`corpus_pack.py:272`) |
| pack + shard write | `:1131-1132` | `corpus_pack.pack` (`corpus_pack.py:617`) |
| upload | `:1055-1075` (`sink`) | `s3.put_bytes_verified` — digest declared to S3 *before* upload |
| receipt | `:1220-1256` | `corpus_receipt.write_receipt`, gated by `verify_receipt` first |

Order is deliberate (`:1086-1091`): dedup/decon run on **documents, before tokenizing**, because
"after tokenization a document is a byte range inside a shard, and removing one means re-cutting
every shard after it."

### 2.2 Turning decontamination OFF — **yes, there is a flag**

**Exact parameter name: `--no-decontaminate`**, declared at `corpus_build.py:1899`. Consumed at
`corpus_build.py:1415-1417`:

```
if args.no_decontaminate:
    print("WARNING decontamination DISABLED by --no-decontaminate", flush=True)
else:
    index = load_index(s3, args.bucket)
```

`index=None` then flows to `run_bundle(..., index=index)` and `dedup_and_decontaminate` skips the
check (`corpus_filter.py:323`: `if index is not None and index.contains(...)`). The flag is the
*only* way to skip it: `load_index` **raises** rather than returning an empty index
(`corpus_filter.py:213-225`) — "skipping decontamination silently produces a corpus that looks
decontaminated. Pass `--no-decontaminate` to accept that deliberately."

At the library level the equivalent is `run_bundle(..., index=None)` (default, `:1002`).

### 2.3 Turning dedup OFF — **NO flag exists. This is a real gap.**

Grepped `no_dedup`, `no-dedup`, `skip_dedup`, `disable.*dedup` across `src/` — **zero hits.**

The mechanics: `dedup_and_decontaminate(docs, *, index=None, seen=None, stats=None)`
(`corpus_filter.py:299-305`). When `seen is None` it **default-constructs a fresh `SeenHashes`
per bundle** (`:315`), so exact-content dedup *always* runs. `corpus_build.py:1099-1103` passes
`seen=keep_filter`, where `keep_filter = _keep_filter_for(bundle, keep_list)` returns `None` when
`keep_list is None` (`:962-965`) — and `None` re-triggers the default `SeenHashes`.

Note what `keep_list` actually controls (`run_bundle` docstring, `:1019-1024`): **without it there is
no *cross-bundle* dedup at all** — every bundle gets its own `SeenHashes`, so cross-source duplicates
all survive. So the default regime is already "intra-bundle exact dedup only."

**Minimal change to disable dedup entirely.** Two options, both small:

1. **Library-only, zero source edits (recommended).** Pass a `seen=` object whose
   `add_if_new(hex) -> bool` always returns `True`. The seam is documented as duck-typed
   (`corpus_build.py:1093-1097`: "the seam is a `seen=` argument and nothing else because
   `KeepFilter` is duck-type compatible with `SeenHashes`"). **But** `_keep_filter_for`
   (`:962-986`) hard-rejects anything that is not a `corpus_filter.KeepList` — it raises on a
   `KeepFilter` (`:967-973`) and on any other type (`:974-978`). So this route needs either a
   direct `run_bundle` bypass, or a two-line relaxation in `_keep_filter_for`.
2. **Add the CLI flag**, mirroring `--no-decontaminate`: an `--no-dedup` arg on the `run` parser
   (beside `corpus_build.py:1899`) that makes `run_bundle` pass a pass-through `seen`. ~6 lines
   plus the warning print. This is the honest shape — it matches the existing precedent and leaves a
   log line in CloudWatch.

**⚠️ Skipping dedup does NOT skip the length filter, and the length filter is not optional.**
`tokenize_documents(..., min_tokens=plan["min_doc_tokens"])` (`corpus_build.py:1120-1124`) drops
documents under `corpus.MIN_DOC_TOKENS = 64` (`corpus.py:187`). It has no off-switch, and turning it
off would break the EOS gate — see §5.1.

---

## 3. `publish()` end to end

`publish(source, *, dataset_id, purpose, profile, s3, created_at, tokenizer=None, data_bucket="edullm-data", landing_bucket="edullm-landing", owner=None, group_meta=None, build_executor=None, env=None, max_version_attempts=8, hash_workers=1, copy_workers=1, sources=None, about=None, notes=None, limitations=None, license=None, expected_payload=None, expected_version=None) -> PublishPlan`
— `publish.py:832-857`.

Sequence:

1. **Validate the four typed things** (`:901-915`) — `validate_dataset_id`, `validate_purpose`,
   `expected_version` shape.
2. **Load the family** (`:917`) — `_load_family` reads `families/<family>.json` from
   `FAMILIES_DIR = _resolve_families_dir()` (`publish.py:67`, `contracts.py:580-604`). Three
   layouts: `EDULLM_FAMILIES_DIR` env override, `<package>/families/` (wheel, force-included at
   `pyproject.toml:130-131`), `<repo>/families/`.
3. **Resolve the tokenizer** (`:920-934`) — `_resolve_tokenizer_dependency` (`:776-823`) looks up
   the *published* `tokenizer/<name>[/vN]` dataset in `data_bucket`, pins it by `manifest_sha256`,
   attaches it as `depends_on` on every named group. **Required in practice** for a pretrain corpus
   (`:874-875`): "without a resolvable tokenizer the decode smoke test cannot recompute its bound
   and Gate A rejects the dataset."
4. **Resolve source** (`:940-950`). An `s3://` prefix is used in place. A local directory is first
   streamed to `_staging/<dataset_id>` via `_stage_local_to_landing` (`:244-277`), which skips
   control files and emits a `ControlFileSkipped` warning naming them.
5. **Enumerate** (`:952`) — `_enumerate_s3` (`:280-292`): `(path, size)` from the paginated LIST
   result only. **"Metadata only — NEVER the bytes."**
6. **Allocate the version** (`:962-964`) — `_next_version` (`:670-691`) takes one higher than every
   version in landing *and* in `_catalog/<dataset_id>/`.
7. **Build the plan** (`:965-987`) — `build_plan` (`:305-586`). Per group, per file:
   `s3.hash_object(source_bucket, src_key)` (`:425-427`).
8. **Reserve** (`:990-1008`) — create-only `dataset.json` via `If-None-Match: *`
   (`_put_create_only`, `:1067-1078`). Collision → bump and retry, unless `expected_version` is
   set, in which case canonical bytes must match exactly or `VersionConflict`.
9. **Copy payload** (`:1010-1029`) — `_copy_one` calls `s3.copy`, **server-side, S3→S3**.
   "Bytes move S3→S3 in-region; nothing transits the client."
10. **(Optional) re-verify final copies** (`:1034-1041`) — `_verify_expected_final_copies`
    (`:694-740`), only when `expected_payload` is passed.
11. **Group manifests LAST** (`:1043-1050`) — **this is the commit point.**
12. Clear the local staging area (`:1052-1058`), best-effort.

### 3.1 What is hashed, and where the bytes go — the in-region question

**`s3.hash_object` PULLS EVERY BYTE TO THE CALLER.** `s3.py:301-314`: `get_object`, then
`iter_chunks(chunk_size=8 MiB)` into `hashlib.sha256`. Bounded RAM, unbounded network.

`build_plan` calls it once per payload object (`publish.py:425`). So **`publish()` must run
in-region on AWS Batch**, never locally. `artifacts/reservoir/publish_driver.py:3-7` states the
measurement: "~88 MB/s" in-region single-stream vs "~2.9 MB/s from a laptop, which is ~96 h for this
corpus." `infra/10-dataset-publish-jobdef.md:70` sizes the job at ≥21,600 s (6 h) with 16 vCPU /
32 GB.

Copies (step 9) are server-side and cost the caller nothing.

### 3.2 Parallelism parameters and defaults

| parameter | default | what it threads | code |
|---|---|---|---|
| `publish(hash_workers=)` | **1** | `build_plan`'s per-object `hash_object`, and `_verify_expected_final_copies` | `publish.py:849`, `:467-475`, `:733-737` |
| `publish(copy_workers=)` | **1** | the server-side copy loop | `publish.py:849`, `:1022-1029` |
| `promote(copy_workers=)` | **1** | promote's copy loop + CRC HEADs | `validate.py:1958`, `:2136-2148` |
| `validate_dataset(head_workers=)` | **1** | per-entry HEAD prefetch | `validate.py:268`, `_prefetch_heads:545-593` |
| `validate_dataset(check_workers=)` | **`head_workers`** | profile-check S3 reads (7 of Gate A's 8 round trips) | `validate.py:269`, `:283-293`, `:424` |
| `corpus_build verify --hash-workers` | **1** | the `--deep` payload re-hash | `corpus_build.py:1907` |

Order-preserving in every case: `executor.map` yields in submission order, so the manifest and the
violation report are byte-identical at any worker count (`publish.py:467-473`,
`validate.py:711-716`).

The live example: `artifacts/reservoir/publish_driver.py:120-121` uses `hash_workers=16,
copy_workers=16`.

### 3.3 What triggers promotion

The `manifest.json` write in step 11 fires the `edullm-landing-manifest-created` EventBridge rule,
which wakes the validator. **Two traps:**

- The rule matches key **suffix** `manifest.json` **anywhere in the bucket, with no prefix
  constraint** (`ingest_reservoir.py:16-22`). Any build artifact named `manifest.json` fires Gate A.
  `_assert_safe_key` (`ingest_reservoir.py:122-136`) mechanically refuses the reserved basenames
  (`_RESERVED_BASENAMES`, `:89`).
- **The rule is currently DISABLED** (`infra/05-validator-jobdef.md:35-37`,
  `artifacts/reservoir/publish_driver.py:157-158`), so nothing auto-promotes right now. Submit
  `edullm-validator` by *unversioned* name, or re-enable the rule.

Promotion itself is `promote(result, s3, *, data_bucket, landing_bucket, now=None, copy_workers=1)`
(`validate.py:1951-2216`). It refuses a failed/incomplete result (`:1974`), refuses to re-promote a
sealed prefix (`:1985-1996`), requires the in-memory `promotion_snapshot` from Gate A (`:1998-2003`),
copies controls from the *snapshot bytes* not from mutable landing (`:2022-2040`), copies payload,
HEADs post-copy CRCs from the **destination** (`:2112-2118`), roots the hash chain with
`dataset_sha256` (`:2130-2140`), writes `_catalog/`, generates `README.md` best-effort
(`:2162-2179`), and writes `_VALIDATED.json` **last** (`:2205-2215`).

---

## 4. Subsetting and token budgets

### 4.1 The shard size constant

**`corpus.SHARD_TOKENS = 3052 * SEQ_LEN = 25,001,984` tokens** (`corpus.py:127`), where
`SEQ_LEN = 8192` (`corpus.py:80`). At uint32 that is **100.0 MB per shard**.

Two constraints in its comment (`corpus.py:82-127`):
- Hard: must be an exact multiple of `SEQ_LEN`, or `check_seq_len_alignment` rejects the object.
- ⚠️ Changing it **changes every `plan_id`** — it is serialized into the plan document whose sha256
  *is* the id.

Derived arithmetic for our two releases:

| release | tokens | shards | bytes |
|---|---|---|---|
| 50B | 50,000,000,000 | ~2,000 | ~200 GB |
| 100B | 100,000,000,000 | ~4,000 | ~400 GB |

### 4.2 How a token budget is expressed — build side

Per-source, in the registry: `CorpusSpec.target_tokens` (`corpus.py:269`). `0` means RESERVE — specced
but not drawn (`corpus.py:260-268`). `plan_document(specs, *, tokens_per_source=None, val_fraction=VAL_FRACTION, domain_map=None, registry_meta=None, file_shards=None)`
(`corpus_build.py:367-543`) turns those into a plan; `tokens_per_source` overrides per-key at
`:432`.

Chain: `target_tokens` → `val = int(want * val_fraction)`, zeroed if `< SHARD_TOKENS` (`:435-450`)
→ `train = want - val` → `shard_plan(targets)` rounds **down**, `tokens // SHARD_TOKENS`, and
**refuses** a stream yielding zero shards (`corpus_pack.py:467-503`) → `allocate_ordinals(plan)`
assigns globally-unique ordinals up front (`corpus.py:399-444`).

The reader's stopping rule is `_reader_for` (`corpus_build.py:1735-1830`+): a **character** budget,
because nothing knows a document's token length before the tokenizer sees it.
`_CHARS_PER_TOKEN = 6.0` (`corpus_build.py:1653`, chosen above the worst measured 5.58) times
`_FILTER_HEADROOM` (`:~1660`) to cover dedup/decon/length attrition.

### 4.3 How a token budget is expressed — read side

`build_mixture(dataset_id, version, *, sources, total, seed, s3, data_bucket, require_validated=True, group=None, split="train", warn_partial_labels=True) -> ResolvedMixture`
(`read.py:982-1155`). `total` is a budget in the group's own count unit; each `MixtureSource`
(`read.py:903-926`) has `ratio`, `max_repetition_ratio=1.0`, `max_source_fraction=1.0`.

Selection is **whole shards in a seeded order** (`read.py:1115-1123`), not the head of every file
(`:1001-1007`). Budget lands within one shard of target.

⚠️ **`build_mixture` is scoped to ONE group and ONE dataset** (`:1012-1015`, `_mixture_entries` at
`:1160-1199`). It cannot span datasets — so it is a *read-time* subsetting tool, not a way to build
the 50B release out of the 100B one as a published artifact.

### 4.4 **Is 50B a prefix of 100B? — NO, not as published datasets.**

Three independent mechanisms forbid it:

1. **`promote()` copies payload into each dataset's own prefix.** `_copy_payload`
   (`validate.py:2052-2110`) writes `data_bucket/<dataset_id>/<version>/<entry.path>`. Two
   dataset_ids = two full physical copies. There is no symlink, no reference, no shared-object
   layout anywhere in the schema.
2. **Gate A actively rejects the shared-bytes shortcut.** `duplicate-shard-digest`
   (`validate.py:748-756`) fires on a repeated sha256 *within* a group. And crucially,
   `shared-sha-with-parent` (`validate.py:759-766`) fires when a child shard's digest also appears
   in a `depends_on` parent: *"sha256 also appears in depends_on parent … — reference it, do not
   copy."* So declaring the 50B release as `depends_on` the 100B one and re-copying half its
   shards is **explicitly rejected**.
3. **`entry.path` is inside `manifest_sha256`**, and the ordinals differ between plans. Two plans
   with different `target_tokens` produce different `plan_id`s (`corpus_build.py:540-542`) and
   different ordinal allocations.

**Practical implications, cheapest first:**

- **(A) One dataset + two read-time mixtures — the actually cheap answer.** Publish ONE 100B
  release. The 50B "release" is a `build_mixture(total=50_000_000_000, seed=…)` call, which is
  deterministic and reproducible from its config (`read.py:1009-1010`). Costs: one build, one
  publish, one Gate A. **This is what the code is designed for** — it is exactly the reservoir
  pattern (`publish_driver.py:64-67`: "252B-token multi-source reservoir … to draw 20B-token
  training mixtures from").
- **(B) Two genuinely separate datasets.** Two builds (or one 100B build plus a second plan whose
  targets are halved), two publishes, two Gate A runs, ~600 GB of published payload. The 50B is
  *not* free and its shards are not the 100B's shards.
- **(C) One build, two publishes over disjoint key subsets.** Build 100B once into
  `_ingest/.../data/`, then `publish()` twice over two different `s3://` source prefixes. Requires
  the build to lay shards out under two separable prefixes from the start, and the 50B release is
  then *disjoint from* rather than *a prefix of* the 100B — a 150B build in total. Worse than (A)
  or (B).

### 4.5 Whole-shard granularity

`SHARD_TOKENS` = 25M means a 50B release has ~2,000 shards; mixture error is
`1 / shards_per_component` (`corpus.py:120-122`), so ~0.05% at that scale — irrelevant.

---

## 5. What gates would fire on an externally-produced tokenized corpus

Gate A entry point: `validate_dataset(landing_bucket, prefix, s3, *, data_bucket=None, head_workers=1, check_workers=None)`
(`validate.py:262-542`). Per-group: `_validate_group` (`:596-891`).

### 5.1 EOS-token fraction — **the bound that will bite**

| where | bound | code |
|---|---|---|
| profile constant (fallback) | `_DEFAULT_MAX_EOS_FRACTION = 0.5` | `pretrain_tokens_v1.py:58` |
| **family default (binding)** | **`eos_fraction_max: 0.05`** | `families/pretrain.json` → `defaults.decode_smoke_test` |
| mirrored build-side constant | `FAMILY_MAX_EOS_FRACTION = 0.05` | `corpus.py:158` |

The check: `check_decode_smoke` (`pretrain_tokens_v1.py:509-641`), specifically `:604-614`. It
samples 4 windows totalling `DECODE_SAMPLE_BYTES = 64 KiB` (`profiles/base.py:127`) = 16,384 uint32
tokens, at seeded offsets (`sample_offsets`, `base.py:130`; seed
`sha256(f"{dataset_id}|{version}|{group}")` at `validate.py:816-818`, then per-shard
`sha256(f"{rng_seed}:{path}")` at `pretrain_tokens_v1.py:78-81`).

Resolution order is `_bound` (`pretrain_tokens_v1.py:111-137`): group override, else family default,
else profile constant — and a group override may only **tighten** (`:126-134`). The family aliases
map at `validate.py:1100-1104`.

**⚠️ The families-dir trap.** `_family_defaults_for` (`validate.py:1117-1145`) returns `{}` when
`families/` is unresolvable, and the profile then falls back to **0.5, ten times laxer**. This is
how a live corpus shipped validated at 50% EOS while declaring 5% (`contracts.py:580-587`,
`CLAUDE.md` gotcha 2). Fixed by `pyproject.toml:130-131` force-including the directory into the
wheel — but verify it in whatever image runs Gate A.

**Would an AI2-produced mix pass?** The arithmetic is exact and stated at `corpus.py:160-177`:
one EOS per document means a packed shard's EOS fraction **IS** `1 / mean_doc_tokens`.

> `mean_doc_tokens < 20 ⟹ eos_fraction > 0.05 ⟹ eos-fraction-out-of-bounds`

**Inputs needed to predict it: the tokenizer's EOS id, and the mean document token length of each
subset.** For OLMoE-mix the subsets are DCLM web, peS2o papers, open-web-math, algebraic-stack, and
starcoder code — all long-document corpora, so a mean well above 20 is near-certain and this bound
should clear with a large margin. `starcoder` is the one to sample: many tiny source files would pull
the mean down.

Note the check is skipped entirely if `eos_id` is not an int (`pretrain_tokens_v1.py:604`). And there
is a live precedent for that silently happening: memory records `superbpe-tokenizer-has-no-eos` —
`added_tokens: []` → `eos_token_id: None` → the EOS check silently skipped in two published corpora.
`derive_vocab` (`profiles/tokenizer_v1.py:44-70`) resolves EOS only from `added_tokens` matching
`<|endoftext|>`, `</s>`, `<eos>`, `<|eot_id|>`, or containing `endoftext`.

Build-side there is a second, earlier gate: `assert_eos_fraction_publishable`
(`corpus_pack.py:525-565`) and `_verify_shard` (`corpus_pack.py:958-1063`), which checks the **worst
`DECODE_WINDOW_TOKENS` window** (`corpus_pack.py:193`), not the average — deliberately sufficient
rather than merely necessary for Gate A (`:975-981`).

### 5.2 `distinct_ids_min`

| where | value | code |
|---|---|---|
| profile constant | `_DEFAULT_MIN_DISTINCT = 16` | `pretrain_tokens_v1.py:57` |
| **family default (binding)** | **`distinct_ids_min: 128`** | `families/pretrain.json` |

Check at `pretrain_tokens_v1.py:574-602`. Two adjustments, both load-bearing:

1. **Vocab cap** — `_cap_min_distinct_by_vocab(min_distinct, vocab_size)`
   (`pretrain_tokens_v1.py:161-188`): returns `min(min_distinct, max(16, vocab_size // 16))`. For a
   ~100k BPE vocab this is `min(128, 6250) == 128` — **unchanged**. It only binds below vocab 4096.
   ⚠️ This function was recovered from the deployed `0.5.1` wheel and exists in no other commit.
2. **Sample scaling** — `effective_min = min(min_distinct, max(n // 4, 2 if n > 1 else 1))`
   (`:592`). At the full 16,384-token sample, `16384 // 4 = 4096`, so the declared 128 applies
   unchanged.

**Would an AI2 mix pass?** Yes, trivially. 128 distinct ids in a 16,384-token window of real text is
a floor no healthy shard approaches. **Input needed: `vocab_size`** (only matters if < 4096).

### 5.3 Duplicate-digest detection

- **Within a group:** `duplicate-shard-digest`, `validate.py:748-756`. Set membership on the
  *declared* sha256 over `seen_sha`. Fires on the second occurrence, so iteration order decides
  which shard is named — which is why the decision loop stays serial (`:705-709`).
- **Against a `depends_on` parent:** `shared-sha-with-parent`, `validate.py:759-766`, seeded by
  `_register_parent_shas` (`:1827-1865`) which walks every parent manifest.

**⚠️ Neither re-reads payload bytes.** `CLAUDE.md` is explicit and it holds on this branch: the
per-entry loop does `s3.head` for SIZE and set-membership on the *declared* digest. `hash_object` has
exactly one non-`vendored/` caller — `publish.py:425`, the PRODUCER. So a manifest `sha256` is a
producer assertion no `pretrain-tokens/v1` gate falsifies. (`vendored/v1` is the one exception,
§1.3.)

**Would an AI2 mix pass?** Only if no two shards are byte-identical. Real risk on a code corpus with
many near-empty files, and on any subset with duplicated upstream shards. The 150B corpus tripped
exactly this: memory records `150b-two-blockers-are-the-same-two-shards` — two 20-byte shards caused
both the duplicate-digest AND the eos-fraction failures.

### 5.4 `tokens × dtype_size == bytes`

`verify_arithmetic(entry)` (`manifest.py:561-618`), called at `validate.py:738-739` as
`count-arithmetic`. Identity: `count.value * dtype_size + header_bytes == bytes`. Applies only when
`count.unit ∈ FIXED_WIDTH_UNITS = {tokens, indices}` (`manifest.py:89`), `dtype` is in `DTYPE_SIZES`,
`codec == "none"`, and `container ∈ FIXED_WIDTH_CONTAINERS = {raw}` (`manifest.py:97`).

**⚠️ It is near-tautological on a `publish()`-built manifest**, and the code says so
(`manifest.py:612-617`): `publish._count_for` derives `count = size // dtype_size`
(`publish.py:170`), so the identity collapses to `bytes % dtype_size == 0`. It catches truncation and
a non-element-boundary size; it does **not** catch a dtype lie.

The real dtype check is separate: `_check_dtype_width_vs_vocab`
(`validate.py:1507-1592`) → `dtype-too-narrow-for-vocab`, using `_min_dtype_size_for_vocab`
(`:1492-1504`) against the **derived** vocab. One-sided: wider than necessary is legal, narrower is
not. Plus `dtype-not-checkable` (`:1542-1553`) for an alias like `u4` or `<u4`, and
`fixed-width-dtype-in-nonraw-container` (`:1557-1569`).

**Would an AI2 mix pass?** Yes if the shards are headerless raw uint32 LE and named `.u32le.bin`.
**Input needed: `vocab_size`** — dolma2 is 100,278, needing 4 bytes, so uint32 is exactly right and
uint16 would be rejected.

### 5.5 Extension vs magic bytes

Two halves, and §5 of the standard requires both:

- **Metadata half:** `check_extension_matches_format(path, fmt)` (`manifest.py:443-498`), called at
  `validate.py:740-741` as `extension-format-mismatch`. Longest-suffix match against
  `EXTENSION_FORMAT` (`manifest.py:395-427`). `.u32le.bin` claims
  `{container: raw, dtype: uint32, byte_order: little, header_bytes: 0, codec: none}`. A nonzero
  `header_bytes` under that extension is rejected (`:482-488`); a `.npy` with `header_bytes == 0` is
  rejected via `min_header_bytes: 1` (`:490-496`).
- **Bytes half:** `check_first_bytes_not_npy` (`pretrain_tokens_v1.py:658-691`) reads the leading
  8 bytes and rejects `\x93NUMPY` (`_NPY_MAGIC`, `:56`) → `npy-magic-bytes`. This is the check the
  metadata half cannot make: a file named `.npy` *and* declared `container: npy` is internally
  consistent.

**Would an AI2 mix pass?** **Only after a rename, and only if the bytes are truly headerless.** AI2
ships pre-tokenized shards as `.npy`. Two sub-cases:
- If they are headerless raw uint32 (the ".npy lie" — which memory records as *true* for AI2's dolma3
  shards, `ai2-dolma3-shards-are-byte-compatible`, verified by range-read): rename to `.u32le.bin`
  and both checks pass. This is the copy+rename path.
- If they carry a real `\x93NUMPY` header: `npy-magic-bytes` fires. Fix is to strip the header
  during the copy, which turns a server-side copy into a re-write — much more expensive.

**Test the first 8 bytes of a sample before committing to a plan.**

### 5.6 Minimum / maximum shard counts

**There is no minimum or maximum shard count anywhere in the validator.** Grepped — no contiguity,
gap, or count check on ordinals (confirmed by `corpus.py:409-414`: "nothing in `validate.py` compares
ordinals across sources … gaps are legal").

What exists instead:
- **Per-stream minimum of one shard**, build-side: `shard_plan` refuses a stream yielding zero shards
  (`corpus_pack.py:491-501`).
- **Ordinal ceiling**: `_ORDINAL_MAX = 99_999` (`corpus.py:374`), enforced by `shard_key`
  (`:389-394`). `SHARD_RE` requires **exactly five digits** (`manifest.py:660-663`), so a six-digit
  ordinal does not parse as a shard at all and silently stops being split-checked. At 25M
  tokens/shard, 100,000 ordinals = 2.5T tokens per split. Not a constraint for us.
- **A de facto ceiling from the Gate A job timeout** — see §7.2.

### 5.7 Required control files

`CONTROL_BASENAMES` (`contracts.py:182-184`) = `{dataset.json, manifest.json, _SUCCESS,
_VALIDATED.json, _REJECTED.json, README.md}`. `CONTROL_PREFIXES` (`:213`) = `("_catalog/",
"dependents/", "_dedup/", "_licenses/")`.

Which are *required*, and who writes them:

| file | required | written by |
|---|---|---|
| `dataset.json` | **yes** (step 8) | `publish()` |
| `<group>/manifest.json` | **yes** (commit point) | `publish()` |
| `README.md` | yes, but **derived and automatic** | `promote()`, `validate.py:2162-2179`, best-effort |
| `_VALIDATED.json` | written by promotion, last | `promote()`, `validate.py:2205-2215` |

**Never hand-write a README.** It is a control file, generated by `readme.render_readme(dataset.json)`
(`readme.py:331`), never a manifest entry, never in the hash chain. A hand-written one in the source
tree is **silently skipped** by `_is_control_source` (`publish.py:210-241`) — with a
`ControlFileSkipped` warning (`publish.py:78-95`). Feed it via
`publish(sources=/about=/notes=/limitations=/license=)`.

`REQUIRED_CORE_FIELDS` is at `validate.py:83`; `MUTABILITIES = {frozen, append-only, live}` at
`validate.py:94`; `READABLE_SCHEMA_VERSIONS = {edullm-dataset/v1, edullm-dataset/v2}` at
`contracts.py:61`.

### 5.8 Checks an external tokenized mix would trip that are easy to overlook

- **`shard-naming`** (`validate.py:742-745` → `manifest.check_shard_naming:1226-1246`). Names must
  match `<split>-<NNNNN>.<self-describing-ext>` with **exactly five digits and no `-of-NNNNN`**.
  Verified by execution in this session: `tokens/part-000-00000.npy`, `tokens/part_00000.u32le.bin`,
  and `tokens/train-000000.u32le.bin` (six digits) **all fail**; `tokens/train-00000.u32le.bin` and
  `tokens/dclm/train-00000.u32le.bin` pass. **So AI2's names must be rewritten, and the rewrite is
  the ordinal allocation.**
- **`missing-required-split`** (`_check_validation_present`, `validate.py:1270-1332`).
  `families/pretrain.json` sets `validation_required: true`, and this is **opt-out, not opt-in**:
  declaring no partitions at all is explicitly *not* an exemption (`:1298-1316`). **An externally
  drawn subset with no held-out shards is REJECTED.** The message even names the precedent:
  `pretrain/olmo-mix-1124-31b/v1` is expected to fail this rule.
  ⚠️ **This is a real constraint on the plan.** A val split must be carved from documents *before*
  tokenizing (`corpus.is_held_out`, `corpus.py:533-567`) — it cannot be added afterwards, and a
  frozen dataset cannot gain one in place.
- **`undeclared-split` / `empty-split`** (`_check_dataset_exhaustive_and_splits`,
  `validate.py:1246-1267`). Split words are recomputed from each object's own filename and compared
  in **both** directions against declared partitions.
- **`partition-rows-mismatch`** (`_check_partition_rows`, `validate.py:1686-1749`). `rows` is
  recomputed from the selected entries' counts. `publish()` fills it automatically
  (`_fill_missing_partition_rows`, `publish.py:619-639`) — including for caller-supplied partitions
  (`publish.py:527-539`, a fix for exactly this footgun).
- **`coverage-not-disjoint` / `coverage-incomplete`** (`_check_coverage_is_a_partition`,
  `validate.py:1780-1825`). `coverage: "partition"` is now enforced, not just spelled.
- **`labels-contradict-path`** (`_check_labels_match_path`, `validate.py:1400-1490`). Labels are
  recomputed from the key. **Only two levels are available** —
  `PATH_LABEL_KEYS = ("source", "domain")` (`manifest.py:693`) — and a third level must be
  FLATTENED into the `source` segment (`manifest.py:723-726`). `keys=` is not an escape hatch.
- **`unlisted-object` / `unlisted-object-dataset-level`** (`validate.py:793-800`, `:1227-1236`).
  The manifest must be exhaustive in both directions, at the group *and* dataset level.
- **`inventory-objects` / `inventory-bytes`** (`validate.py:452-468`). Recomputed and summed.
- **`manifest-sha256-mismatch`** (`validate.py:631-641`), **`manifest-objects`/`manifest-bytes`**
  (`:653-674`). The hash chain, recomputed.
- **`zero-run-in-shard`** (`pretrain_tokens_v1.py:629-640`). `max_zero_run` = 256 (both
  `_DEFAULT_MAX_ZERO_RUN` at `:61` and `zero_run_max` in `families/pretrain.json`). Comparison is
  `>=`, not `>`. Note this is a **RUN**, not a fraction — the density form was removed because
  dolma2 maps `!` to id 0 and it rejected two healthy prose shards.
- **`seq-len-misalignment`** (`check_seq_len_alignment`, `pretrain_tokens_v1.py:694-731`).
  **Skipped vacuously if the group declares no `seq_len`.** If declared,
  `bytes % (dtype_size * seq_len) == 0`. AI2's shard sizes will almost certainly not be multiples of
  `4 * 8192` — so **do not declare `seq_len`** on an externally-sourced group, or re-cut the shards.
- **`token-count-unit`** (`check_entries_declare_token_counts`, `pretrain_tokens_v1.py:484-506`).
  Every shard must declare `count{unit: "tokens", value}`. `publish()` supplies this automatically
  from the object size for raw fixed-width (`publish.py:169-170`).
- **`missing-tokenizer-field`** (`pretrain_tokens_v1.py:535-543`). No derivable `vocab_size` is
  itself a violation. Also `tokenizer-parent-missing` / `tokenizer-json-not-in-parent`
  (`_resolve_tokenizer`, `validate.py:1868-1949`).

### 5.9 Summary — inputs needed to predict the verdict

1. **The tokenizer's EOS id** and whether `derive_vocab` can find it — decides whether the EOS check
   runs at all, and at what threshold.
2. **`vocab_size`** — feeds `dtype-too-narrow-for-vocab` and the distinct-ids vocab cap.
3. **Mean document token length per subset** — the single number that predicts
   `eos-fraction-out-of-bounds` (must be ≥ 20; ≥ 64 gives the 3.2x margin the build targets).
4. **The first 8 bytes of a sample shard** — headerless, or a real `\x93NUMPY` header.
5. **Whether any two shards are byte-identical** — `duplicate-shard-digest`.
6. **Whether a held-out split exists** — `missing-required-split` is a hard reject.
7. **`bytes % (4 * 8192)`** on sample shards — only if you intend to declare `seq_len`.

---

## 6. The profile and family declaration to write

### 6.1 Profile schema (`profiles/base.py:104-131`)

A profile is **a module**, not a class. Three module-level attributes:

```
NAME: str                            # exact string appearing in a group's `profile`
REQUIRED_FIELDS: Mapping[str, Any]   # JSON-schema-ish fragment, presence only
CHECKS: list[Callable[[GroupContext], list[Violation]]]
```

Self-registration at import (`registry.py:9-18` documents the exact idiom;
`pretrain_tokens_v1.py:745-752` is the live example). Register the module name in
`registry._SHIPPED` (`registry.py:35-42`) — a literal list, not a directory scan.
`GroupContext` fields are at `base.py:88-101`.

### 6.2 **We almost certainly do NOT need a new profile.**

`pretrain-tokens/v1` (`profiles/pretrain_tokens_v1.py`) already covers packed token shards, and
`registry.available()` returns 6 profiles (`registry.py:35-42`): `pretrain-tokens/v1`,
`eval-results/v1`, `token-order/v1`, `sft-conversations/v1`, `tokenizer/v1`, `vendored/v1`.

**Write a new profile only if** the shards cannot be made to satisfy `pretrain-tokens/v1` — e.g. they
carry a real `.npy` header we choose not to strip. That would be a bad trade: it means writing a
profile whose whole purpose is to bless the exact lie the standard was written against.

### 6.3 Family JSON schema

`families/pretrain.json` is the one to use unchanged. Structure (all 7 families share it):

```
{
  "schema_version": "edullm-family/v1",
  "family": "pretrain",
  "owner": "...",
  "license": {"id": null, "basis": "unknown"},
  "sources": [],
  "defaults": {
    "profile": "pretrain-tokens/v1",
    "mutability": "frozen",
    "reproducibility": "logical",
    "tokenizer_dependency_optional": { ...OFF by default, leave it off... },
    "format": {"container": "raw", "dtype": "uint32", "byte_order": "little",
               "header_bytes": 0, "codec": "none"},
    "extension": ".u32le.bin",
    "shard_name_template": "<split>-<NNNNN>.u32le.bin",
    "partitions": [{"name": "train", "by": "path", "glob": "train-*.u32le.bin"},
                   {"name": "val",   "by": "path", "glob": "val-*.u32le.bin"}],
    "coverage": "partition",
    "decode_smoke_test": {"window_bytes": 65536, "distinct_ids_min": 128,
                          "eos_fraction_max": 0.05, "zero_run_max": 256},
    "tags": {"Project": "edullm"},
    "validation_required": true
  },
  "notes": "..."
}
```

**Do not edit `families/pretrain.json` to loosen a bound.** `_bound`
(`pretrain_tokens_v1.py:111-137`) only lets a group *tighten*; loosening requires editing the family
file, "where it applies to everyone and shows up as a change to the standard rather than to one
dataset."

### 6.4 What `publish()` must be given for a tokenized pretrain corpus

Required: `source`, `dataset_id`, `purpose`, `profile="pretrain-tokens/v1"`, `s3`, `created_at`,
`tokenizer="tokenizer/<name>"`. Everything else is derived or inherited.

`purpose` must clear `validate_purpose` (`contracts.py:447-490`): 20-300 chars, not in the
placeholder blocklist (`:419-431`), must contain a space.

`dataset_id` must clear `validate_dataset_id` (`contracts.py:383-407`): `<family>/<name>`, family
from the closed enum (`contracts.py:130`), name kebab-case with 2-5 words
(`_MIN_WORDS`/`_MAX_WORDS`, `contracts.py:230-231`), no dates, no version tokens, no content-free
words, no bare ordinals.

**Verified by execution this session** — all four candidates pass:
`pretrain/olmoe-mix-50b`, `pretrain/olmoe-mix-100b`, `pretrain/olmoe-mix-0924-50b`,
`pretrain/olmoe-mix-0924-100b`. (`0924` survives because the year rule only rejects `19xx`/`20xx`,
`contracts.py:247` — the same exemption that lets `olmo-mix-1124` through, `contracts.py:238`.)

### 6.5 Closest examples to copy

1. **`artifacts/reservoir/publish_driver.py`** (163 lines) — the real, runnable `publish()` call for
   a 252B-token pretrain corpus. Reads its numbers from generated JSON rather than retyping them,
   refuses to publish if they do not reconcile (`:43-51`), uses `hash_workers=16, copy_workers=16`.
   **This is the single best template for step 3.**
2. **`tests/test_mixture.py:52-77`** (`_publish_promote`) — the minimal end-to-end
   publish→validate→promote for a labelled, multi-source pretrain corpus with train and val shards.
   Shows the `group_meta={"tokens": {"tokenizer": TOKENIZER}}` shape.
3. **`src/edullm_data/ingest_prm800k.py`** — the stage-then-publish CLI structure, if a new ingest
   command is needed.

---

## 7. What hard-blocks a two-release publish

### 7.1 Ranked blockers

| # | blocker | severity | evidence |
|---|---|---|---|
| 1 | **The mix is raw text, not pre-tokenized** — no fast path exists | **HARD** | HF API: `.json.gz`/`.jsonl.gz`/`.json.zst`, schema `text: string`, no tokenizer file |
| 2 | **No zstd reader** — DCLM is 1,970 of ~2,100 files | **HARD** | `corpus_read.py:846-850`, `:896-902`; `corpus_build.py:361`; no `zstandard` in `pyproject.toml:27-82` |
| 3 | **50B is not a prefix of 100B** — two datasets = two full payload copies | **HARD (by design)** | `validate.py:2052-2110`; `shared-sha-with-parent` at `:759-766` |
| 4 | **`missing-required-split`** — a val split must be carved pre-tokenization | **HARD** | `validate.py:1270-1332`; `families/pretrain.json` `validation_required: true`; `corpus.is_held_out` |
| 5 | **Gate A object count vs job timeout** — see §7.2 | **SOFT, mitigated** | `corpus.py:90-111` |
| 6 | **Auto-promotion is DISABLED** | **operational** | `infra/05-validator-jobdef.md:35-37` |
| 7 | **No `--no-dedup` flag** | **SOFT** — ~6 lines | grep: zero hits |
| 8 | **`labels_from_path` allows only 2 levels** | **SCHEMA, unbackfillable** | `manifest.py:693`, `:723-726` |
| 9 | **AI2's shard names fail `check_shard_naming`** | **SOFT** — the rename IS the ordinal allocation | verified by execution |

### 7.2 The Gate A timeout arithmetic — worth doing before committing

`corpus.py:90-111` (re-derived 2026-08-08) gives the measured constants:

- Gate A is **objects × 8 round trips**, measured at **507.5 ms/object** (10,049 objects in ~85 min,
  `pretrain_tokens_v1.py:205-218`).
- Live `edullm-validator` timeout is **14,400 s** (rev 14), not the 7,200 s in older docs.
- The stated break-even is **28,373 objects = 709B tokens**.

For our releases: 50B ≈ 2,000 objects, 100B ≈ 4,000 objects. Both are **an order of magnitude under
the break-even** — Gate A is not a blocker at this scale. Serial estimate for 4,000 objects is
~34 min; with `--check-workers 16` it is minutes.

⚠️ But note `--check-workers` is where 7 of the 8 round trips live (`validate.py:283-293`), and it
**defaults to `head_workers`** deliberately, because the live job def passes only `--head-workers 16`
and a knob defaulting to 1 would have silently left the fan-out off in production.

### 7.3 What does NOT block us (checked, so it is not re-litigated)

- **Ordinal allocation does not forbid two datasets sharing a plan.** `allocate_ordinals`
  (`corpus.py:399-444`) is per-plan; two plans are independent. `_assert_plan_is_disjoint`
  (`corpus_build.py:546-593`) checks `bundle_id` and shard-path uniqueness *within* one plan only.
- **`build_mixture` cannot span groups** (`corpus.py:137-141`, `read.py:1012-1015`) — but this only
  matters for read-time mixtures, and the whole corpus lives in one group (`GROUP = "tokens"`).
- **No object-count limit within Gate A** other than the timeout — see §5.6.
- **Ordinal ceiling of 99,999** = 2.5T tokens/split. Not binding.
- **`promote()` refuses to re-promote a sealed prefix** (`validate.py:1985-1996`) — correct and not
  in our way, since our two releases have different `dataset_id`s.

---

## 8. Fast path vs slow path — the verdict

### The slow path (what OLMoE-mix-0924 actually requires)

```
[add a zstd reader + dependency]                             ← NEW CODE, blocker #2
  ↓
build a registry row per subset (CorpusSpec, corpus.py:234)
  ↓
edullm-corpus-build plan --upload                            ← corpus_build.py:1367
  ↓
edullm-corpus-build run --plan-id <id> --of N \               ← corpus_build.py:1397, Batch array
    --tokenizer-dir <dir> --no-decontaminate
  ↓  (read → carve → dedup → tokenize → pack → upload → receipt)
edullm-corpus-build verify --plan-id <id> --deep \            ← corpus_build.py:1467
    --hash-workers 16
  ↓
publish(source="s3://edullm-landing/_ingest/.../data/", ..., ← publish.py:832, IN-REGION Batch
        hash_workers=16, copy_workers=16)
  ↓
edullm-data-validate --prefix <id>/<v> --head-workers 16 \    ← validate.py:2457
    --promote --promote-workers 16
```

**Why it is slow.** Tokenization is filter-bound, measured at 72,615 tok/s/vCPU end-to-end
(`corpus_build.py:396`). 100B tokens is ~1.38M vCPU-seconds ≈ 383 vCPU-hours of pure tokenize, before
the read. Plus new zstd code, plus a full `publish()` stream-hash of ~400 GB.

### The fast path (only if pre-tokenized AI2 shards can be sourced)

```
copy external shards → s3://edullm-landing/_ingest/... , RENAMED to
    tokens/<source>/train-<NNNNN>.u32le.bin                  ← ordinals from corpus.allocate_ordinals
  ↓  (server-side copy where possible; s3.put_stream otherwise)
publish(source="s3://...", profile="pretrain-tokens/v1",     ← publish.py:832, IN-REGION
        tokenizer="tokenizer/dolma2-bpe", hash_workers=16, copy_workers=16)
  ↓  ← _count_for derives token counts from OBJECT SIZE, zero bytes read (publish.py:169-170)
edullm-data-validate --prefix ... --promote
```

**Why it is fast.** No tokenizer runs. No document is ever read. Token counts are pure arithmetic on
the object size. The only expensive step is `publish()`'s stream-hash — one full read of the payload,
in-region at ~88 MB/s × 16 workers.

**Its three preconditions, all testable cheaply:**
1. The bytes are headerless raw uint32 LE (range-read the first 8 bytes; memory records this as
   *verified true* for AI2's dolma3 shards).
2. The tokenizer is dolma2-compatible and already published as `tokenizer/dolma2-bpe`
   (`corpus.py:143-147` says it exists in `s3://edullm-data/tokenizer/` with a real 4.0 MiB
   `tokenizer.json`).
3. A val split exists or can be constructed — **this is the awkward one.** `is_held_out` carves at
   the *document* level pre-tokenization, and pre-tokenized shards have no documents left. A
   whole-shard val split (holding out N whole shards) is possible but is a *different* and weaker
   guarantee, and it is not what the code does today.

### Recommendation

1. **Settle blocker #1 first.** Find out whether a pre-tokenized AI2 mirror of this mix exists on
   AI2's S3. If it does, the fast path is ~10x cheaper and needs no new reader.
   `artifacts/impl-plan/source-encoding-audit.md:1216-1219` calls this "a genuine strategic fork."
2. **Publish ONE release, not two.** Per §4.4 option (A): one 100B dataset, and express the 50B as a
   deterministic `build_mixture(total=50_000_000_000, seed=…)`. This is the reservoir pattern the
   codebase was built for, it halves the build and publish cost, and it needs no new code. Two
   physically separate releases buy nothing the seed does not already give, and cost a second
   ~200 GB copy plus a second Gate A run.
3. **Decide the val split before anything else is built.** `missing-required-split` is a hard reject
   and the split is unbackfillable on a frozen dataset.
