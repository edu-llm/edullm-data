# 07 — Ingest + publish driver design: `pretrain/olmoe-mix-50b` and `-100b`

Design only. **Nothing was deployed, submitted, committed, or written to AWS.** Every AWS-shaped
claim below is read from repo files or from the prior artifacts in this directory; the only live
network calls I made were anonymous ranged `GET`s to `olmo-data.org` to falsify two claims about the
origin (§1.1) and one about the payload bytes (§1.2), plus one throughput sample.

Code worktree read for every `file:line`:
`/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/claude-20--olmoe-mix-ingest`
(branch `agent/claude-20/olmoe-mix-ingest`, `__init__.py:3` `__version__ = "0.9.1"`).

Deliverables, all under `/Users/ericwu/Developer/Capstone_LLM/edullm-data/artifacts/olmoe-mix-ingest/`:

| file | what it is | state |
|---|---|---|
| `olmoe_ingest_driver.py` | HTTP -> landing streamer, threaded, resumable | **exercised end-to-end against `FakeS3` + a fake origin; both `--dry-run`s produce the exact owner figures** |
| `olmoe_publish_driver.py` | the two `publish()` calls | **every kwarg checked against the real signature; a real `build_plan` + `render_readme` run on the planned key set** |
| `11-olmoe-ingest-policy.json` | the IAM policy that does not exist yet (§6.1) | authored, **not created** — needs `iam:CreateRole`, which no broker session has |

---

## 0. Corrections to the brief, up front

Five of the brief's assertions did not survive checking. Two are material.

### 0.1 MATERIAL — "the val shard is a source object that appears in NO train slice of either release (verified disjoint)" was FALSE for the selection I was handed at the time

The mid-task message asserted this. It was not true of the `olmoe_final_*.json` files as they then
stood: the 50B's val shard was
`preprocessed/proof-pile-2/v0_decontaminated/arxiv/train/.../part-02-00000.npy`, and that same
object was **also a 100B TRAIN shard**. Recomputed from the files themselves:

```
IS 50B val (part-02) in 100B TRAIN? True
IS 50B val in 50B train?           False
```

The final regenerated selection fixed it (50B val = `part-02`, 100B train arxiv = `part-00`,
`part-01` only; 100B val = `part-03`; all four disjoint — re-verified §2.2). But the class of bug
is invisible to every gate in this repo, so the driver now checks it explicitly and refuses:

* `duplicate-shard-digest` (`validate.py:748-757`) is scoped to **one group of one dataset**. Two
  independent releases share no group, so it cannot see a cross-release collision.
* `shared-sha-with-parent` (`validate.py:759-768`) fires only via `depends_on`, and these two
  releases deliberately do not declare each other.

I confirmed the guard fires by reconstructing the superseded selection and running the driver:

```
REFUSING: this release's val shard(s) [...part-02-00000.npy] are TRAIN shards of the 100b release.
Both releases go to the same consumers; that is leakage no gate in this repo can see, because the
two datasets share no group and no depends_on.
```

### 0.2 MATERIAL — a manifest written by this driver would be *denied*, and the brief's requirement #7 is unsatisfiable as phrased

The brief says "Write the group `manifest.json` LAST." The driver writes **no manifest at all**,
which is correct, for three independent reasons:

1. `publish()` writes the group manifests itself, last, as step 3 of its own ordering —
   `publish.py:1044-1051`, comment verbatim: *"group manifests LAST — the commit point (§6)"*. It
   derives every entry by re-streaming the staged bytes (`publish.py:418-425` -> `s3.hash_object`),
   so a hand-written manifest is ignored at best.
2. An ingest identity is **explicitly denied the name**.
   `infra/08-reservoir-ingest-policy.json` -> `NeverWriteValidatorTriggeringOrTerminalNames` is a
   `Deny s3:PutObject` on `arn:aws:s3:::edullm-landing/*manifest.json` **and** `*dataset.json`. The
   jobdef doc states the reason (`infra/10-dataset-publish-jobdef.md`): *"A builder that could would
   fire the validator at a half-built prefix."*
3. The brief's premise about the trigger is **right**, and stronger than it says.
   `infra/04-event-wiring.yaml:176-201` matches `detail.object.key` by suffix `manifest.json` with
   no prefix filter, and its own comment warns *"Suffix matching deliberately over-matches slightly:
   `foo-manifest.json` matches too. … Do not ever name a validator-authored landing object
   `*manifest.json` or this rule becomes recursive."* The rule is currently **DISABLED**
   (`infra/DEPLOY.md:487`, and `infra/09-reservoir-publish-jobdef.md:61`).

So the driver's commit point is a marker named `_INGEST_COMPLETE.json`, written last — and see §3.4,
where the obvious placement for it turned out to be a bug I had to find by running the real code.

### 0.3 The `User-Agent` claim is real but narrower than stated

The brief: *"Requests need header `User-Agent: curl/8.7.1` or Cloudflare returns 403."* Measured
live 2026-08-08, ranged `GET` on arxiv `part-04`:

| UA sent | status |
|---|---|
| `curl/8.7.1` | 206 |
| `Python-urllib/3.11` | **403** |
| *(no UA header at all)* | **403** |
| `python-urllib/3.11` (lowercase p) | 206 |
| `urllib` | 206 |
| `python-requests/2.31.0` | 206 |
| `Botocore/1.43.56` | 206 |
| `Mozilla/5.0` | 206 |
| `` (empty string) | 206 |
| `edullm-data/0.9.1 (+https://github.com/edu-llm/edullm-data)` | 206 |

It is **not** "curl or nothing" — it is a case-sensitive block on the literal default urllib token
`Python-urllib/*`, plus a block on sending no UA. The driver therefore sends an **honest**
identifying UA that is measured to pass, rather than impersonating curl in a log someone will have
to read later. `_UA` is a single module constant if a future Cloudflare rule tightens to an
allowlist. The brief is right that this is not a permissions problem and no retry fixes it — so 403
is deliberately absent from the retry set, with that reason recorded at the raise site.

### 0.4 The reservoir's `_cdn_url` fix does not transfer as stated; the *lesson* does

The brief asks for "resolve once per FILE not once per range (a 70x request reduction)". That fix
(`ingest_reservoir.py:313-360`) exists because **pyarrow seeks**: it issues ~70 ranged reads to pull
one column, and pointing each at HF's metered control plane spent 70 metered requests per file.

We do not seek. We read each object once, start to finish. The correct application here is **one
HTTP request per file, full stop** — not one resolve plus N ranges. There is also nothing to
resolve: `olmo-data.org` serves the bytes directly (measured `HTTP/2 200`, `server: cloudflare`,
`accept-ranges: bytes`, no redirect), so there is no signed-URL TTL to expire mid-read and
`_CDN_TTL_S` has no analogue. Claiming otherwise would be cargo-culting the mechanism instead of
the lesson.

### 0.5 Minor: sizes/counts differ slightly from the brief's headline numbers

The brief's "18 shards / 56.06 Gtok / 208.9 GiB" and "32 / 105.17 / 391.8" match the *superseded*
proportional `olmoe_selection.json`, which I recomputed and confirmed before it was withdrawn. The
final figures are in §2. Also: the largest selected object is **exactly 16.0 GiB**
(17,179,868,800 B), not "up to 17.2 GiB" — 17.2 is the same number in decimal GB. The part-count
arithmetic in §3.1 uses the measured value.

---

## 1. Established facts, re-verified

### 1.1 Origin and key list

* Key list is `OLMo-core/src/olmo_core/data/mixes/OLMoE-mix-0824.txt`, 1,135 lines,
  `<label>,<path-with-{TOKENIZER}>` plus `#` comments. Substituting `{TOKENIZER}` ->
  `allenai/dolma2-tokenizer` yields the 1,122 keys in `olmoe_0824_sizes.json`, each with a live
  HEAD **status 200** (I asserted this over all 1,122 rows).
* Listing is disabled — `04-aws-platform.md` records `GET https://olmo-data.org/?list-type=2` ->
  404 Cloudflare. So the `.txt` is the only enumeration and the driver never lists.

### 1.2 Payload is headerless raw uint32 LE — copy + rename, and it decodes correctly

Range-read the first 16 bytes of arxiv `part-04`: `3b000000 3f0b0000 5a000000 6f950000`. No
`\x93NUMPY` magic; as `<u4` that is ids 59, 2879, 90, 38255 — all inside vocab 100278.

I then ran the **profile's own decode smoke test** against real bytes for one shard of each of the
seven components plus a val candidate — 3 windows x ~21.8 KB per shard, decoded `<u4`, against the
`families/pretrain.json` bounds (`window_bytes` 65536, `distinct_ids_min` 128, `eos_fraction_max`
0.05, `zero_run_max` 256):

| component | n ids | distinct | max id | in vocab | EOS frac | zero-run OK |
|---|---:|---:|---:|:--:|---:|:--:|
| dclm | 16,374 | 3,827 | 100,257 | yes | 0.00067 | yes |
| starcoder | 16,374 | 2,948 | 100,257 | yes | 0.00092 | yes |
| pes2o | 16,374 | 3,091 | 100,257 | yes | 0.00018 | yes |
| proofpile-2-arxiv | 16,374 | 2,780 | 100,257 | yes | 0.00006 | yes |
| proofpile-2-open-web-math | 16,374 | 2,706 | 100,257 | yes | 0.00079 | yes |
| proofpile-2-stack | 16,374 | 2,658 | 100,257 | yes | 0.00006 | yes |
| wikipedia | 16,374 | 4,326 | 100,257 | yes | 0.00189 | yes |
| val candidate (arxiv part-02) | 16,374 | 2,583 | 100,257 | yes | 0.00006 | yes |

Max id observed is exactly EOS (100257) and every id is `< 100278`, which is independent
confirmation of both the dtype and the tokenizer. All seven components clear `distinct_ids_min` by
20x and `eos_fraction_max` by ~26x. **Gate A's decode check should pass** — subject to the caveat
that the real check samples seeded offsets across all 37 objects, not the 3 windows I sampled.

One genuinely reassuring consequence: the EOS ids are *present*, so this corpus does not hit the
`superbpe-tokenizer-has-no-eos` trap where a missing EOS silently skips the check entirely.

### 1.3 Every selected object is a whole number of tokens

All 1,122 upstream sizes satisfy `bytes % 4 == 0`, and so do all 38 selected objects. So
`tokens * dtype_size == file bytes` holds exactly and `tokens = bytes / 4` needs no rounding. The
driver asserts this per object anyway (free, and a future reselection is not required to be careful).

### 1.4 Tokenizer

`allenai/dolma2-tokenizer`, vocab 100278, EOS 100257 — consistent with §1.2. We publish naming the
already-published `tokenizer/dolma2-bpe`; `publish()` resolves it, pins it by `manifest_sha256`, and
attaches it as `depends_on` on the token group (`publish.py:918-932`), and the validator **derives**
vocab/EOS from that tokenizer.json rather than trusting anything we declare. This is also why the
driver declares no `vocab_size`/`eos_token_id` in the dataset metadata: `families/pretrain.json`'s
notes call hand-typed pins "unverifiable typed values".

---

## 2. The selection, recomputed

Read from `olmoe_final_50b.json` / `olmoe_final_100b.json` (regenerated by the committed
`make_final_selection.py`), and independently re-summed from `olmoe_0824_sizes.json`.

### 2.1 Realized figures — every field reconciles

Both drivers refuse to run if the `realized` block disagrees with the re-derivation. Confirmed by
mutating one field by +1: `REFUSING: realized.train_tokens = 49,661,507,306 but the selection sums
to 49,661,507,305.`

| | 50B | 100B |
|---|---:|---:|
| dataset_id | `pretrain/olmoe-mix-50b` | `pretrain/olmoe-mix-100b` |
| train shards | 19 | 36 |
| val shards | 1 | 1 |
| **objects** | **20** | **37** |
| train tokens | 49,661,507,305 | 96,809,585,725 |
| val tokens | 1,407,839,173 | 1,321,106,697 |
| **total tokens** | **51,069,346,478** | **98,130,692,422** |
| total bytes | 204,277,385,912 | 392,522,769,688 |
| **GiB** | **190.25** | **365.57** |
| **web (dclm) share of train** | **74.57%** | **75.00%** |
| val fraction of release | 2.757% | 1.346% |

Realized per-component shares of train, and the upstream share each departs from:

| component | 50B shards | 50B share | 100B shards | 100B share | upstream share |
|---|---:|---:|---:|---:|---:|
| dclm (web) | 9 | 74.57% | 18 | 75.00% | **95.12%** |
| pes2o | 1 | 6.06% | 4 | 12.27% | 1.50% |
| wikipedia | 2 | 7.37% | 2 | 3.78% | 0.09% |
| starcoder | 3 | 4.48% | 6 | 4.98% | 2.13% |
| proofpile-2-stack | 2 | 3.65% | 2 | 1.87% | 0.30% |
| proofpile-2-arxiv | 1 | 2.81% | 2 | 1.51% | 0.53% |
| proofpile-2-open-web-math | 1 | 1.05% | 2 | 0.58% | 0.31% |

Upstream shares are computed from the whole mix as listed: 1,122 shards, 15,580,114,781,464 bytes =
3.895 Ttok, dclm = 3,704,993,775,401 tok = **95.1211%**. Confirms the owner's 95.12%.

### 2.2 Structural checks, all recomputed

| claim | result |
|---|---|
| 50B train is a strict subset of 100B train | **True** |
| 50B val in either train set | **False** |
| 100B val in either train set | **False** |
| the two val shards differ | **True** (part-02 vs part-03) |
| every selected object `bytes % 4 == 0` | **True** (38/38) |
| both val splits > 1.0 Gtok | **True** (1.408, 1.321) |
| all 38 objects have *distinct byte sizes* | **True** — so no two are byte-identical, i.e. `duplicate-shard-digest` cannot fire on a size-collision basis |
| distinct objects across both releases | **38**, 398,154,126,380 B = **370.81 GiB** fetched once |
| largest object | 17,179,868,800 B = exactly 16.0 GiB |

### 2.3 The val carve

Read from the JSON, not re-derived — but the two constraints are **re-checked** in the driver,
because a generator and a driver that both decide will drift and only one of them writes bytes.

* **50B val**: arxiv `part-02-00000` — 1,407,839,173 tok, **2.757%** of the release.
* **100B val**: arxiv `part-03-00000` — 1,321,106,697 tok, **1.346%** of the release.

Why a whole shard: `families/pretrain.json` sets `validation_required: true` and globs
`val-*.u32le.bin`, and a packed token shard carries **no document boundaries** — the `.csv.gz`
doc-offset sidecars are a separate upstream artifact we are not ingesting — so a file is the
smallest carvable unit. Neither is a byte-copy of anything in train (§2.2), so
`duplicate-shard-digest` cannot fire and val is not 100% leakage.

The **tiny-shard trap is real and the floor is load-bearing.** Several upstream shards are
anomalously small: arxiv `part-01` is 0.0624 Gtok, open-web-math `part-01` 0.0428, algebraic-stack
`part-03` 0.0951 — against a 16.0 GiB (4.29 Gtok) dclm shard, a 100x spread. An earlier generator
took "the next unused shard in sorted order" and produced a 0.0624 Gtok val split. The driver
re-checks `> 1.0 Gtok` and refuses:

```
REFUSING: val shard ...part-01-00000.npy is 62,390,879 tokens, at or below the 1,000,000,000
floor. See VAL_MIN_TOKENS: the tiny-shard trap already produced a 0.0624 Gtok val split once.
```

One honest limitation, recorded in `limitations[]`: the val split is **arXiv only**, so it measures
held-out loss on academic prose and says nothing directly about web, code or encyclopedic text. A
stratified per-source val split would require re-tokenizing from source documents, which this
ingest deliberately does not do.

---

## 3. `olmoe_ingest_driver.py`

### 3.1 Streaming, and the part-size arithmetic

`S3.put_stream` (`s3.py:469-542`) with **64 MiB parts**. The three real limits:

| limit | value | our worst case |
|---|---|---|
| 10,000 parts / upload | 64 MiB x 10,000 = **640 GiB** ceiling | 16.0 GiB = **256 parts**, 39x headroom |
| 5 MiB minimum non-final part (`s3.py:MIN_MULTIPART_PART_BYTES`) | | satisfied 12.8x over |
| memory | one part per worker | 64 MiB x 8 = **512 MiB** peak, independent of the 16 GiB shard |

Not the inherited 16 MiB default (`s3.py:_STREAM_PART_BYTES`): a 16 GiB shard would be 1,024
sequential `upload_part` round trips. Not 128 MiB: doubles peak RAM to buy nothing when 256 parts is
already nowhere near 10,000. Note the part limit is not actually what binds — even at the 5 MiB
minimum the ceiling is 48.8 GiB, above our largest object. Latency is what binds.

`_read_stream_part` (`s3.py:186-203`) already accumulates until a part is full, so a short HTTP read
cannot produce an undersized non-final part. The driver never holds a whole shard: bounded by one
part, guaranteed by `_WitnessReader.read` raising on any `size < 0` request.

### 3.2 Ordinals: global, contiguous, flat, deterministic

`tokens/train-00000.u32le.bin` … `train-00018` + `tokens/val-00000.u32le.bin` (50B);
`train-00000` … `train-00035` + `val-00000` (100B). One flat `tokens/` group, **no** `tokens/<source>/`
nesting. Two reasons, in order of severity:

1. **Ordinal reuse is the one real contradiction.** `parse_shard_name` operates on the **basename**
   (`manifest.py:675-687`), so nested per-source numbering makes `tokens/dclm/train-00000` and
   `tokens/pes2o/train-00000` both parse to `("train", 0)`, and any consumer keyed on
   `(split, ordinal)` silently collapses them.
2. Nesting would put the source in the key, which `labels_from_path` (`manifest.py:698-720`) turns
   into `entry.labels` — **inside `manifest_sha256`, therefore unbackfillable**. A reasonable future
   choice, not free, and not needed to publish. Composition lives in `sources[]` and the mapping
   file instead.

Ordering is `sorted((label, source_key))` — a total order over two strings. No clock, no PRNG, no
dict iteration order. `--emit-mapping <path>` writes the full plan (source key -> ordinal, sizes,
token counts, totals, the `realized` block) so the assignment is auditable and reproducible.

### 3.3 Four witnesses, compared, on every object

"Recompute, never trust" on the write path. Per object:

1. **pinned** — bytes from `olmoe_0824_sizes.json` (a live HEAD, status 200)
2. **received** — `Content-Length` the origin actually sent, checked in `_HttpBody._open` **before a
   single byte is uploaded**
3. **consumed** — bytes `_WitnessReader` actually handed to `put_stream`, hashing as they pass
4. **written** — `ContentLength` from a fresh `head()` after `complete_multipart_upload`

`assert pinned == received == consumed == written`, plus `written % 4 == 0`, then best-effort delete
of a known-bad object. Modelled on `ingest_prm800k.py:616-641`, including its note that the ingest
role holds no `s3:DeleteObject`, so the delete normally fails and the object stays receipt-less and
distinguishable until lifecycle expiry.

The sha256 is computed **from the same bytes handed to S3**, so the two cannot disagree — unlike a
caller-supplied digest, which `s3.py:404-431` calls out as decoration that "would read as
verification and prove nothing."

I verified all four tripwires against a fake origin:

| injected fault | result |
|---|---|
| origin serves 4 bytes fewer than pinned | `Content-Length mismatch … origin says 36, pinned witness says 40`, rc=1, **no object created** |
| origin serves 4 bytes more | same tripwire, rc=1 |
| `Content-Length: 40` but body truncated to 20 | `short read: got 20 of 40 bytes`, rc=1 |
| any failure | **no completion marker written**, rerun skips the completed objects |

### 3.4 A BUG I FOUND IN MY OWN DRIVER: where the completion marker goes

My first version wrote `<release>/_INGEST_COMPLETE.json`, beside `tokens/`, and the docstring
asserted `publish()` would skip it as a control file. **That is false**, and I only found it by
running the real function:

```
>>> _enumerate_s3(s3, "edullm-landing", "_ingest/olmoe-mix-0824/50b")
[('_INGEST_COMPLETE.json', 2), ('tokens/train-00000.u32le.bin', 40)]
```

`_is_control_source` (`publish.py:210-241`) is `basename in CONTROL_BASENAMES or
is_control_prefix(rel)`. `CONTROL_BASENAMES` is exactly `{dataset.json, manifest.json, _SUCCESS,
_VALIDATED.json, _REJECTED.json, README.md}` (`contracts.py:182-184`) and `CONTROL_PREFIXES` is
`("_catalog/", "dependents/", "_dedup/", "_licenses/")` (`contracts.py:213`). **A leading underscore
is not sufficient.** The marker survives enumeration, `_group_of` returns `""`, and `build_plan`
raises `PublishError: payload file '_INGEST_COMPLETE.json' is not under a group prefix`
(`publish.py:346-353`) — i.e. the publish fails at the very end of a 366 GiB transfer, for a 2 KB
file.

Fix: the marker goes in a **sibling** directory outside the release prefix —
`_ingest/olmoe-mix-0824/_receipts/<release>-INGEST_COMPLETE.json`. Re-verified: `_enumerate_s3` on
the release prefix then returns exactly the 20 payload objects, all under group `tokens`.

### 3.5 Threading and the sized pool

`--workers N` (default 8) over `ThreadPoolExecutor`, and the client is built
`Boto3S3.default(max_pool_connections=workers + 4)`. Without it, botocore's default is **10** and
botocore does not pass `block=True` to urllib3, so urllib3 **discards** the surplus connection and
logs "Connection pool is full" — workers 11..N pay a fresh TLS handshake per request and the fan-out
silently caps itself. `s3.py:211-249` calls the failure mode "a number that does not improve, which
is the hardest kind to notice", and `infra/10-dataset-publish-jobdef.md` flags this exact trap for a
publish driver. The harness asserts the parameter is passed.

The shared `_RateGate` (`ingest_reservoir.py:250-296`) is module-level so a 429 anywhere pauses
everywhere — the quota is a property of the fleet, not the thread. `_TRANSIENT_STATUSES` is
`{408, 425, 429, 500, 502, 503, 504, 509}` verbatim from `ingest_reservoir.py:194-212`: explicit
statuses rather than `>= 500`, because a blanket rule also retries permanent 501/505 and buries a
real error behind eight backoffs. Backoff is exponential from 4 s capped at 120 s, honouring a
numeric `Retry-After` (`ingest_reservoir.py:229-246`) — a linear 3 s retry cannot outlast a
rate-limit window, a bug this repo has now written twice.

**Resume-on-drop is deliberately not implemented.** A mid-stream drop aborts the object; the caller
retries from byte 0. Ranged resume would let a retry stitch two halves of two *different* server
responses and the sha256 would still come out "valid" because we computed it over what we stitched.

### 3.6 Idempotent / resumable

Skip an object already at the destination **at exactly the pinned size**. Size rather than mere
existence: a run killed inside `put_stream` leaves no object (multipart aborts, `s3.py:534-541`), but
one killed between `complete_multipart_upload` and the assert leaves a complete object, and length
against the pinned witness is the cheap way to tell it from a truncated one. Not a re-hash, because
`publish()` stream-hashes every object anyway (`publish.py:418-425`) — paying twice buys nothing.
Verified: a second `run()` reports `20 object(s) already present at the right size — skipping`.

### 3.7 What the harness actually proved

Ran `transfer_one`/`run` against `FakeS3` plus a fake HTTP origin — no AWS, no network:

* 20 objects written, then the marker **last** in write order
* **no** `manifest.json` or `dataset.json` written, ever
* sha256 in the marker matches an independently computed digest
* `publish()` enumerates exactly 20 payload objects, all under group `tokens`; marker invisible
* idempotent rerun clean; all four fault injections fail loudly with rc=1 and no marker
* both `--dry-run`s reproduce the owner's figures to the byte (190.25 GiB / 51,069,346,478 tok;
  365.57 GiB / 98,130,692,422 tok)

---

## 4. `olmoe_publish_driver.py`

### 4.1 The signature, quoted (`publish.py:832-857`)

```python
def publish(source, *, dataset_id, purpose, profile, s3, created_at,
            tokenizer=None, data_bucket="edullm-data", landing_bucket=LANDING_BUCKET,
            owner=None, group_meta=None, build_executor=None, env=None,
            max_version_attempts=8, hash_workers=1, copy_workers=1,
            sources=None, about=None, notes=None, limitations=None, license=None,
            expected_payload=None, expected_version=None) -> PublishPlan
```

I checked programmatically that `set(kwargs) - set(signature.parameters)` is **empty** for both
releases, and that `validate_purpose` / `validate_dataset_id` accept the real values. Nothing is
invented. `hash_workers`/`copy_workers` both default to **1** (`publish.py:848-849`); we pass 16/16,
matching the live reservoir driver. `publish.py:836-838` documents what they parallelize.

### 4.2 Deliberately NOT passed

* **`group_meta`** — `publish()` builds the tokenizer `depends_on` itself from `tokenizer=`
  (`publish.py:918-932`) and attaches it to the conventional `tokens` group, which is exactly the
  group our flat `tokens/` prefix produces (`publish.py:295-297`).
* **`expected_payload`** — a per-path `{bytes, sha256}` table requiring a 64-hex digest for *every*
  path (`publish.py:362-397`). For a resumed/skipped object the ingest marker records
  `sha256: null`, so the table cannot be completed, and filling it with digests we did not observe
  would be decoration. The honest chain is: ingest asserts four witnesses at write time, then
  `publish()` independently re-streams and hashes everything.
* **`expected_version`** — that is for an artifact that must never be retried under a new version.
  We want the ordinary create-only reservation.

### 4.3 `purpose` (20-300 chars, blocklist-safe)

50B (143 chars) / 100B (144 chars):

> `51.1B-token dolma2-tokenized subset of AI2's OLMoE-mix-0824 for eduLLM pretraining runs to establish a published-mix baseline at the 50B budget`

`validate_purpose` (`contracts.py:447-482`) checks 20-300 chars, at least one space, and that the
normalized form (lowercased, non-alphanumerics stripped) is not in `_PURPOSE_BLOCKLIST_RAW`
(`""`, `todo`, `tbd`, `data`, `training data`, `the dataset`, `dataset`, `experiments`,
`see readme`, `data from the run`, `corpus for the project`). Both pass — asserted against the real
function, not eyeballed.

### 4.4 `sources[]` and `scope`

One row per component in the release's train split: `share` = **our measured** share of train,
`tokens` = the **upstream component total**, hence `scope="upstream-full-collection"` on every row.
`readme.py:79-87` matches `str(scope).startswith("upstream")` and uses it to relabel the column
"Upstream tokens" and print a caveat paragraph. Omitting `scope` would print upstream figures under
a plain "Tokens" heading — "which tells a consumer the opposite of the truth, and is the exact
confusion `scope` exists to resolve." Upstream totals are `exact bytes / 4` from the 1,122 measured
sizes, not card arithmetic.

`license={"id": "ODC-By-1.0", "basis": "declared"}` — "declared", not "verified": ODC-By is the
mix-level term AI2 states, and per-component upstream terms also apply. `families/pretrain.json`
carries `license.basis: "unknown"` at family level precisely so each dataset overrides with its own
checked terms.

### 4.5 `limitations[]` — five entries

| kind | records |
|---|---|
| `deduplication` | **Not deduplicated by us.** Byte-for-byte copy; whatever duplication AI2 left is here unchanged and **we have not measured it**. |
| `contamination` | **Not decontaminated by us, and we added nothing.** Upstream did its own — the paths say so (`v0_decontaminated`, `v1-decon-100_to_20k-2star-top_token_030`). We did not verify that work, it does not extend to dclm/peS2o/wikipedia (whose paths make no such claim), and we ran no gate of our own. Eval cleanliness is an **upstream** claim. |
| `composition` | Web is **DELIBERATELY UNDERWEIGHTED**: 74.57%/75.00% vs upstream **95.12%**. Explicitly *"an intentional composition choice, NOT sampling drift"* — holding web to ~75% is what buys whole shards of the smaller sources. |
| `coverage` | Wikipedia fully consumed at **2 of 2** upstream shards — a **pool limit, not a choice**. |
| `coverage` | Val is one whole arXiv shard, so **not representative** of the mix. |

`notes` carries realized totals, the 0.17 GB - 17.18 GB shard-size spread and its consequence for
mixture weight granularity, and the sibling warning: the 50B train set is a strict subset of the
100B train set, so **do not train on one and evaluate on the other**.

### 4.6 A real `build_plan` + README render on the planned key set

Not a dry-run print — I ran `build_plan` with `FakeS3` seeded at the 20 planned keys (distinct
bodies) and the real `families/pretrain.json`, then `render_readme(plan.dataset_json)`:

```
groups:            ['tokens']
partitions:        [('train', 'train-*.u32le.bin', 209), ('val', 'val-*.u32le.bin', 11)]
coverage:          partition
manifest entries:  20   |   distinct digests: 20 of 20
labels:            (none — flat layout, as intended)
README headers:    ['## About', '## Data mix / sources', '## Contents',
                    '### `tokens` — pretrain-tokens/v1', '## License', '## Notes',
                    '## Limitations', '## Provenance', '## How to read it']
upstream caveat rendered: True     'Upstream tokens' column label: True
```

Both partitions resolve non-empty (so no `empty-split` / `partition-glob-empty`), `coverage:
partition` is satisfiable (disjoint and complete — the val glob and train glob cannot overlap), and
all four limitation phrasings survive into the README.

---

## 5. Two `publish()` facts that constrain operations

* **`publish()` pulls every byte to wherever it runs.** It stream-hashes each object to build the
  manifest (`publish.py:418-425`). Measured in-region single-stream is ~88 MB/s; I measured **2.3
  MB/s** from this laptop against the origin, and a prior artifact measured 2.9 MB/s for S3 — at
  that rate 366 GiB is ~36 days. **It must run in-region on Batch.**
* **Publishing to landing auto-promotes when the rule is enabled**, because writing a
  `manifest.json` is the trigger. The rule is DISABLED today, so nothing auto-promotes; the
  validator must be submitted by unversioned name with the landing prefix in a command override.

---

## 6. Batch job specifics

### 6.1 THE BLOCKER: no role can currently write `_ingest/olmoe-mix-0824/*`

I enumerated the `Resource` lists of every `infra/*policy.json`. No existing role grants
`s3:PutObject` on our ingest prefix, and one **actively denies** it:

| role | grant on `_ingest/…` |
|---|---|
| `edullm-reservoir-ingest` (`08`) | `PutObject` on `_ingest/reservoir-dolma2/*` **only** |
| `edullm-prm800k-producer` (`06`) | `_staging/vendor/openai-prm800k/*` only |
| `edullm-dataset-publish` (`10`) | `GetObject` on `_ingest/*` — and an explicit **`Deny PutObject` on `_ingest/*`** |
| `dataset-validator` (`03`) | `Put` on all of landing, but assumable only by `ecs-tasks` as the validator |

So the ingest step needs a new role. Minimal addition authored as
**`11-olmoe-ingest-policy.json`** — modelled statement-for-statement on `08`: `Put/Get/
GetObjectAttributes/AbortMultipartUpload` scoped to `_ingest/olmoe-mix-0824/*`, the same
`NeverWriteValidatorTriggeringOrTerminalNames` Deny on `*manifest.json` / `*dataset.json` /
`*_VALIDATED.json` / `*_REJECTED.json`, prefix-conditioned `ListBucket`, and
`GetLifecycleConfiguration`. I verified it is valid JSON, **pure ASCII** with no `_comment` keys
(commit `d8398a2`: "IAM rejects non-ASCII and `_comment` in a policy document"), and that its Deny
matches `…/tokens/manifest.json` while permitting both our payload keys and the `_receipts/` marker.

**It cannot be created from any session**: `infra/10-dataset-publish-jobdef.md` records
`iam:CreateRole` denied even for a lead, because the broker mints an `Intern-*` session regardless
of org role. This is an admin ask. `edullm-dataset-publish` itself was authored and, per
`04-aws-platform.md`, does exist as a role now — the same route applies here.

Alternative that needs no new IAM, if the admin ask is unwelcome: **widen `08`'s first statement**
to `_ingest/*` or add a second resource ARN. Smaller ask, but it broadens an existing identity;
I would state the trade-off rather than choose it silently.

### 6.2 Publish step

| field | value | why |
|---|---|---|
| job definition | **`edullm-dataset-publish`** | the general-purpose publisher (`infra/10-dataset-publish-jobdef.md`); the only def whose role may write `*manifest.json` / `*dataset.json` |
| `jobRoleArn` | `role/edullm-dataset-publish` | **not** a build role — those Deny those two names |
| `executionRoleArn` | `sbsandbox-intern-edullm-batch-execution` | **required**; omitting it yields a container that never starts and no readable logs, mimicking a missing log group |
| `vcpus` / `memory` | **16 / 32768**, as submit-time overrides | `publish()` threads its hashing, so cores are used |
| `attemptDurationSeconds` | **`--timeout attemptDurationSeconds=21600`** (6 h) | §6.4 |
| `retryStrategy` | **`attempts: 1`** | §6.5 |
| image | rev 1 pins `sha256:055ff803…` = `d8398a2` (0.7.5), **3 days / many commits behind** | §6.6 |
| env | **must override `PUBLISH_MODE`** | rev 1 carries `environment: [{PUBLISH_MODE: "--dry-run"}]` — it defaults to a dry run |

**Every `edullm-*` job def has empty `resourceRequirements`**, so vCPU/memory are not set in the def
and **must** be supplied at submit time via `containerOverrides` — verified in
`infra/DEPLOY.md:115-117` ("Expect `vcpus: null` and `memory: null` … every submission must supply
them in the override or the job fails to place") and re-confirmed in `04-aws-platform.md`. A job
stuck `RUNNABLE` forever means the override did not take.

### 6.3 Ingest step

Same queue; needs the §6.1 role and a def pointing at an image containing the driver. `--workers 8`,
64 MiB parts. Sizing 8 vCPU / 16384 MiB is ample — the work is network-bound and peak payload buffer
is 512 MiB. Timeout: §6.4.

### 6.4 Timeouts, justified from throughput

**Ingest** (HTTP -> S3, bounded by the origin, `--workers 8`). Unknown aggregate origin bandwidth is
the honest gap — my 2.3 MB/s laptop sample is not an in-region number, and Cloudflare-to-AWS should
be far better. At a conservative aggregate 200 MB/s: 190 GiB ~ 17 min, 366 GiB ~ 33 min. At a
pessimistic 50 MB/s: ~1.1 h and ~2.2 h. Ask for **`attemptDurationSeconds=21600` (6 h)** and treat
the first run as the measurement.

**Publish** (`hash_object` on every object, then server-side copy). At the measured in-region
single-stream **~88 MB/s**: 190 GiB sequential ~ 40 min, 366 GiB ~ 77 min. `hash_workers=16` cuts
that substantially, but only ~20 (50B) / 37 (100B) objects exist, so per-object latency dominates
tail behaviour and the fan-out cannot exceed the object count. Copies are server-side and cheap.
Gate A is *not* in this job. **`attemptDurationSeconds=21600` (6 h)** — which is already
`edullm-dataset-publish:1`'s default, so no override is strictly needed; pass it explicitly anyway.

Context for why headroom matters: 7200 s SIGKILLed the reservoir promotion at 6,324 of 10,051
objects. Our object counts are 270-500x smaller, so this is a comfortable margin, not a tight one.

**Validation** (a separate `edullm-validator` submission) is the one to watch — Gate A is
latency-bound serial-ish I/O per entry, but at 20/37 entries it is minutes, not the ~85 min the
10k-object reservoir needed. `edullm-validator` is at rev 16 / 28800 s (8 h).

### 6.5 `retryStrategy attempts: 1` — why it is load-bearing for `publish()`

`publish()` reserves its version by writing a **create-only** `dataset.json` first
(`publish.py:1050-1060` -> `_put_create_only`, which uses S3's `If-None-Match: *` precondition,
`publish.py:1069-1082`). On attempt 2, that key exists:

* with no `expected_version` (our case), `FileExistsError` is caught and the loop **bumps the
  version and retries** (`publish.py:996-999`) — so a retried attempt silently lands on **`v2`**
  while `v1` sits half-written in landing;
* with `expected_version` set, it compares canonical bytes and raises `VersionConflict` if they
  differ.

Either way attempt 2 is wrong: you get a spurious `v2` or a confusing failure, and 366 GiB of copies
already happened. `infra/10-dataset-publish-jobdef.md` states it flatly: *"No retry. … attempt 2
finds `v1` taken and either fails confusingly or lands on `v2`. Diagnose by hand."*

### 6.6 Image identity — do not trust the version string

`edullm-dataset-publish:1` runs `d8398a2` (0.7.5), which **predates** this branch's 0.9.1. Both
drivers live in `artifacts/`, so they are not in the wheel or the image at all and must be shipped
in whatever image the job runs, or fetched at start. Two standing traps:

* Identify an image by its **ECR tag (a commit sha)** plus `git merge-base --is-ancestor`, never by
  `__version__` — two commits declared `0.7.4` and two more `0.8.0`, so every job def's
  `assert __version__` passes on materially different trees.
* Container images build **only** from `edullm/**` branches, not `main` and not
  `agent/**`. `agent/claude-20/olmoe-mix-ingest` will build nothing; dispatch explicitly:
  `gh workflow run edullm-platform-build.yml --repo edu-llm/edullm-data --ref <branch>`.

Also note `04-aws-platform.md`'s finding that the newest image `8e2524a` (= this worktree's HEAD, and
`0.9.1`) is **referenced by no job def** — built but unwired.

### 6.7 Order of operations

1. **Admin**: create the §6.1 ingest role (or widen `08`). Then re-verify the airlock live — intern
   `PutObject` to `edullm-data` must return `AccessDenied` (explicit deny). The simulator lies for
   this account (11 known false denials); probe, do not simulate.
2. Build an image from an `edullm/**` branch carrying both drivers; register the ingest def.
3. `--dry-run` both releases; keep the `--emit-mapping` output as the audit record.
4. Ingest **50B** (`--go --workers 8`). Confirm the `_receipts/50b-INGEST_COMPLETE.json` marker.
5. Ingest **100B**. It re-fetches nothing already present — but note the prefixes are separate, so
   the 19 shared objects **are** transferred twice unless you stage once and copy server-side.
   Summed prefixes 555.81 GiB vs 370.81 GiB distinct = **185.00 GiB** of avoidable re-transfer; the
   saving is real but the added moving part may not be worth it for a one-off.
6. Publish 50B (`edullm-dataset-publish`, overrides per §6.2, `PUBLISH_MODE` overridden).
7. Submit `edullm-validator` by **unversioned** name with the landing prefix in the command
   override. Repeat 6-7 for 100B.
8. After anything that touched permissions, re-verify the airlock Deny again.

---

## 6.8 Independent corroboration from `06-gate-predictions.md`

A parallel agent ran the real `publish()` + `validate_dataset()` against a sparse S3 double whose
`get_range()` proxies real HTTP range reads, on the same regenerated selection. Its findings agree
with everything above and add live Gate A verdicts I did not run:

* **`Gate A: ok=True, violations=0`** for both releases (20 and 37 payload objects).
* Decode sample over the full object set: `eos=0.00201 / 0.00195`, `min_distinct=1656 / 1797`,
  `max_zero_run=1`, `max_id=100257` — same conclusion as my 8-shard sample (§1.2), with wider
  coverage.
* The `.u32le.bin` rename is **mandatory, not cosmetic**: keeping AI2's `part-NNN-NNNNN.npy` names
  produces 22 violations (`shard-naming`, `extension-format-mismatch`, `token-count-unit`,
  `missing-required-split`), and the vendored exemption at `validate.py:625-627` does not cover us.
* `dataset_id` naming: `pretrain/olmoe-mix-50b` and `-100b` both pass; a name ending in a bare
  number with no unit is rejected as a bare ordinal, so **keep the `b` suffix**.
* Two advisories worth carrying: the **EOS bound is fail-open** if a tokenizer's `eos_token_id` does
  not derive (it does derive 100257 for `tokenizer/dolma2-bpe/v1`, so it does not fire for us — but
  it is an ordering obligation), and Gate A never re-reads payload to verify `sha256`, which is my
  open item 4.

One naming divergence to settle before submitting: that artifact simulated
`pretrain/olmoe-mix-0824-51b` / `-98b`; the brief and my drivers use `pretrain/olmoe-mix-50b` /
`-100b`. Both pass validation, so this is an owner naming call, not a correctness question — but the
two must not diverge in what is actually published.

---

## 7. Open items I could not close

1. **The ingest IAM role does not exist and cannot be created from a session** (§6.1). Hard blocker,
   admin ask, policy authored.
2. **Aggregate origin bandwidth from AWS is unmeasured.** My 2.3 MB/s is a laptop number. The 6 h
   ingest timeout is sized so this does not matter, but the first run is the measurement.
3. **The 19 shared objects are transferred twice** under the two-prefix layout (§6.7 step 5).
   Deliberate simplicity; flagging the cost rather than hiding it.
4. **Gate A's `sha256` is still a producer assertion no gate falsifies.** The known repo-wide gap,
   re-confirmed in this branch: the per-entry loop in `_validate_group` (`validate.py:596+`;
   the loop body at `:700-757`) prefetches `s3.head` — *"the sole network call"*, compared as
   `head["size"] != entry.bytes` — then does set-membership on the **declared** digest
   (`entry.sha256 in seen_sha`). No payload byte is re-read. Our ingest computes a real digest over
   the real bytes and `publish()` re-streams every object, so this corpus is *better* witnessed than
   the gap implies — but nothing downstream re-verifies it, so do not describe the published
   `sha256` as validator-checked.
5. **Not run**: `pytest` (out of scope per the brief), and no AWS write of any kind.
