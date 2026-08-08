#!/usr/bin/env python3
"""Ingest selected OLMoE-mix-0824 pre-tokenized shards: olmo-data.org HTTP -> edullm-landing.

WHAT THIS DOES AND DOES NOT DO
------------------------------
Does: streams each selected upstream `.npy` object (headerless raw uint32 LE, up to 16.0 GiB)
straight into `s3://edullm-landing/_ingest/olmoe-mix-0824/<release>/tokens/<split>-<NNNNN>.u32le.bin`,
bounded to one multipart part in memory, recomputing byte count and sha256 on the way through.

Does NOT: tokenize (upstream is already tokenized with allenai/dolma2-tokenizer), deduplicate,
decontaminate (upstream paths are `v0_decontaminated` / `v1-decon-*`; we add nothing), or write
ANY `manifest.json` / `dataset.json`. See "THE MANIFEST IS NOT OURS TO WRITE" below.

    python3 olmoe_ingest_driver.py --release 50b  --dry-run      # full plan, zero writes
    python3 olmoe_ingest_driver.py --release 50b  --go --workers 8
    python3 olmoe_ingest_driver.py --release 100b --go --workers 8

Reuses, by file:line, from the branch at 0.9.1
(/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/claude-20--olmoe-mix-ingest):

* `src/edullm_data/ingest_reservoir.py:194-212` `_TRANSIENT_STATUSES` — retry 5xx, not just 429.
  Copied verbatim in spirit: a single 503 cost five bundles overnight there.
* `src/edullm_data/ingest_reservoir.py:229-246` `_backoff_delay` — EXPONENTIAL from 4 s, capped,
  honouring a numeric `Retry-After`. A linear 3 s retry cannot outlast a rate-limit window.
* `src/edullm_data/ingest_reservoir.py:250-296` `_RateGate` — process-wide brake: a 429 anywhere
  pauses every worker, because the quota is a property of the fleet, not the thread.
* `src/edullm_data/ingest_reservoir.py:299-434` `_RangeFile` — its `read()` LOOP (a short read
  segfaulted three of four array children) and `_cdn_url`'s resolve-ONCE-PER-FILE discipline.
  We need the loop and the retry, not the seek/Range machinery: see `_HttpBody` below.
* `src/edullm_data/ingest_prm800k.py:151-190` `_HashingReader` + `:616-641` the
  `put_stream` + byte/digest witness + assert + delete-on-mismatch shape. This is the closest
  existing precedent to this job and `_WitnessReader` here is a near-copy.
* `src/edullm_data/s3.py:469-542` `Boto3S3.put_stream` — bounded multipart, aborts on failure,
  returns the post-upload `head()`. `:186-193` `_read_stream_part` already loops on short reads.
* `src/edullm_data/s3.py:211-249` `Boto3S3.default(max_pool_connections=N)` — the sized pool.

THE MANIFEST IS NOT OURS TO WRITE  (and the brief's requirement #7 is unsatisfiable here)
-----------------------------------------------------------------------------------------
The brief says "write the group manifest.json LAST". This driver writes NO manifest at all, and
that is correct rather than a gap:

1. `publish()` writes the group manifests itself, LAST, as step 3 of its own ordering
   (`src/edullm_data/publish.py:1044-1051`, comment: "group manifests LAST — the commit point
   (§6)"). It derives every entry by re-streaming the staged bytes (`publish.py:418-425`), so a
   hand-written manifest here would be ignored at best and contradictory at worst.
2. An ingest role is DENIED the name. `infra/08-reservoir-ingest-policy.json` statement
   `NeverWriteValidatorTriggeringOrTerminalNames` is an explicit `Deny s3:PutObject` on
   `arn:aws:s3:::edullm-landing/*manifest.json` (and `*dataset.json`). A driver that tried would
   get AccessDenied. `infra/10-dataset-publish-policy.json` grants those names to the PUBLISH
   role only, and the jobdef doc says why the asymmetry exists: "A builder that could would fire
   the validator at a half-built prefix."
3. The trigger is real and unfiltered: `infra/04-event-wiring.yaml:176-185` matches
   `detail.object.key` by SUFFIX `manifest.json` with NO prefix filter — "Suffix matching
   deliberately over-matches slightly: `foo-manifest.json` matches too." So any manifest anywhere
   in landing fires `edullm-validator` (by UNVERSIONED name). It is currently DISABLED
   (`infra/DEPLOY.md:487`), but do not build on that: enabling it is a one-line change by someone
   who is not you.

So the commit point of THIS driver is `_INGEST_COMPLETE.json` — a name that is not `manifest.json`,
not `dataset.json`, and not `*_VALIDATED.json` / `*_REJECTED.json`, written LAST, after every
payload object is verified.

⚠️ AND IT IS WRITTEN OUTSIDE THE PREFIX `publish()` WILL READ, WHICH IS NOT WHAT I FIRST WROTE.
The obvious placement — `<release>/_INGEST_COMPLETE.json`, beside `tokens/` — is a BUG. Verified
against the real function rather than reasoned about:

    >>> _enumerate_s3(s3, "edullm-landing", "_ingest/olmoe-mix-0824/50b")
    [('_INGEST_COMPLETE.json', 2), ('tokens/train-00000.u32le.bin', 40)]

`publish()` filters enumerated source objects with `_is_control_source` (`publish.py:210-241`),
which is `basename in CONTROL_BASENAMES or is_control_prefix(rel)`. `CONTROL_BASENAMES` is exactly
`{dataset.json, manifest.json, _SUCCESS, _VALIDATED.json, _REJECTED.json, README.md}`
(`contracts.py:182-184`) and `CONTROL_PREFIXES` is `("_catalog/", "dependents/", "_dedup/",
"_licenses/")` (`contracts.py:213`). **A leading underscore is NOT sufficient** —
`_INGEST_COMPLETE.json` is in neither set, so it survives enumeration; then
`_group_of("_INGEST_COMPLETE.json")` returns `""` and `build_plan` raises `PublishError: payload file
'_INGEST_COMPLETE.json' is not under a group prefix` (`publish.py:346-353`). The publish would fail
at the very end of a 366 GiB transfer, for a 2 KB file.

So the marker goes in a SIBLING directory that is not under the release prefix at all:
`_ingest/olmoe-mix-0824/_receipts/<release>-INGEST_COMPLETE.json`. Verified the same way — with the
marker there, `_enumerate_s3` on the release prefix returns the payload objects only.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import BinaryIO

HERE = pathlib.Path(__file__).resolve().parent

ORIGIN = "https://olmo-data.org/"
LANDING_BUCKET = "edullm-landing"
INGEST_ROOT = "_ingest/olmoe-mix-0824"

#: Our dtype, from `families/pretrain.json` defaults.format: uint32 / little / header_bytes 0.
#: Upstream `.npy` files are headerless raw uint32 LE despite the extension (the ".npy lie"), so
#: ingest is copy + rename. VERIFIED 2026-08-08 by range read: first 16 bytes of arxiv part-04 are
#: `3b000000 3f0b0000 5a000000 6f950000` -> ids 59, 2879, 90, 38255. No `\x93NUMPY` magic.
BYTES_PER_TOKEN = 4
EXTENSION = ".u32le.bin"

#: The val split is READ from the selection JSON, never re-derived here. `make_final_selection.py`
#: owns that decision and encodes two constraints this driver only RE-CHECKS:
#:   * val > 1.0 Gtok. Several upstream shards are anomalously tiny — arxiv part-01 is 0.0624 Gtok,
#:     open-web-math part-01 is 0.0428, algebraic-stack part-03 is 0.0951. An earlier generator took
#:     "the next unused shard in sorted order" and handed the 50B release a 0.0624 Gtok val split:
#:     0.125% of the release, which validates clean and measures nothing.
#:   * the val object appears in NO train slice of EITHER release, so it is never a byte-copy of a
#:     train shard. A copied val is 100% leakage that every checksum gate blesses.
#: Re-checked rather than re-derived: a generator and a driver that both decide would drift, and
#: only one of them writes the bytes.
VAL_MIN_TOKENS = 1_000_000_000

#: `allenai/dolma2-tokenizer`: vocab 100278, EOS 100257. We do NOT declare these — the validator
#: DERIVES them from the published `tokenizer/dolma2-bpe` that publish() pins as a depends_on.
#: Recorded here only so the ingest receipt states what the bytes are supposed to be.
TOKENIZER_URI = "allenai/dolma2-tokenizer"
VOCAB_SIZE = 100278
EOS_TOKEN_ID = 100257

# --------------------------------------------------------------------------------------
# HTTP transport — lifted from ingest_reservoir.py, minus the seekable/Range machinery
# --------------------------------------------------------------------------------------

#: ⚠️ CLOUDFLARE UA GATING IS REAL BUT NARROWER THAN THE BRIEF SAYS. The brief asserts requests
#: "need header `User-Agent: curl/8.7.1` or Cloudflare returns 403". MEASURED live 2026-08-08
#: against arxiv part-04, ranged GET:
#:
#:     'curl/8.7.1'                    -> 206      'Python-urllib/3.11'  -> 403
#:     'python-urllib/3.11'            -> 206      (no UA header at all) -> 403
#:     'edullm-data/0.9.1 (+github…)'  -> 206      'urllib'              -> 206
#:     'python-requests/2.31.0'        -> 206      'Botocore/1.43.56'    -> 206
#:     'Mozilla/5.0'                  -> 206      ''  (empty UA)        -> 206
#:
#: So it is NOT "curl or nothing". What 403s is (a) the DEFAULT urllib UA string, which is exactly
#: capital-P `Python-urllib/<ver>` — the lowercase spelling passes, so the rule is a literal
#: case-sensitive match on that one token — and (b) sending no UA at all. Impersonating curl works
#: but is a lie in a log we will have to read later, so we send an HONEST identifying UA that is
#: measured to pass. If a future Cloudflare rule tightens to an allowlist, `_UA` is the one knob:
#: set it to `curl/8.7.1` and nothing else changes.
_UA = "edullm-data/0.9.1 (+https://github.com/edu-llm/edullm-data)"

#: `ingest_reservoir.py:194-212`, unchanged and for its stated reason: this set used to be just
#: {429}, so anything else raised on the FIRST attempt, and five wave-1 bundles died overnight on a
#: single 503 after hours of billable work each. Explicit statuses rather than `>= 500`, because a
#: blanket rule also retries 501/505, which are PERMANENT — burying a real error behind eight
#: backoffs. 403 is deliberately absent: here it means the UA was rejected, which no retry fixes.
_TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 509})
_MAX_ATTEMPTS = 8
_BACKOFF_CAP_S = 120.0


def _backoff_delay(attempt: int, retry_after: str | None = None) -> float:
    """`ingest_reservoir.py:229-246`. EXPONENTIAL from 4 s, capped, `Retry-After` when numeric.

    Exponential, not linear. A 3 s linear retry gives up after 30 s and can never outlast a
    rate-limit window — that bug has now been written twice in this repo, so it is restated at the
    point of the mistake rather than only in an artifact. Only the numeric form of `Retry-After` is
    honoured: a date parse that silently failed would give the WRONG delay rather than an obviously
    absent one.
    """
    if retry_after:
        try:
            return min(_BACKOFF_CAP_S, max(1.0, float(retry_after)))
        except ValueError:
            pass
    return min(_BACKOFF_CAP_S, 4.0 * (2**attempt))


class _RateGate:
    """`ingest_reservoir.py:250-296`, module-level so every worker shares ONE gate.

    WHY A PER-WORKER RETRY IS NOT ENOUGH. Every thread in this process hits the same origin from
    the same egress IP, so a worker that backs off privately while seven others keep hammering has
    changed nothing: the quota is a property of the FLEET, not the thread. `penalise()` sets a
    shared deadline; `wait()` blocks any thread about to start a NEW object until it passes. The
    whole ingest slows down together and recovers, instead of collapsing into a retry storm.

    Module-level, not a parameter: a per-call gate would be a gate per thread, which is exactly the
    thing that does not work.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until = 0.0
        self.total_penalties = 0

    def penalise(self, seconds: float) -> None:
        with self._lock:
            self._until = max(self._until, time.monotonic() + seconds)
            self.total_penalties += 1

    def wait(self) -> None:
        while True:
            with self._lock:
                remaining = self._until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 5.0))


_RATE_GATE = _RateGate()


class IngestError(RuntimeError):
    pass


class _HttpBody(io.RawIOBase):
    """A forward-only readable over ONE HTTP GET, opened ONCE per file.

    ⚠️ WHY THIS IS NOT `_RangeFile`. `_RangeFile` exists so pyarrow can SEEK — it issues ~70 ranged
    GETs to pull one column, which is what made resolve-per-range a 70x request amplification
    (`ingest_reservoir.py:313-360`). We are not seeking: we read one object start-to-finish exactly
    once. So the correct application of that hard-won lesson here is ONE request per file, full
    stop — not one resolve plus N ranges. There is nothing to resolve either: olmo-data.org serves
    the bytes directly (measured: `HTTP/2 200`, `server: cloudflare`, `accept-ranges: bytes`, no
    redirect), so there is no signed-CDN-URL TTL to expire mid-read.

    ⚠️ THE READ LOOP IS LOAD-BEARING, and its absence segfaulted three of four array children in
    the reservoir ingest (`ingest_reservoir.py:388-420`). `RawIOBase.read` is PERMITTED to return
    short, and a throttled connection cut mid-body does exactly that. Here the consumer is
    `s3.put_stream`, whose `_read_stream_part` (`s3.py:186-203`) already loops to fill a part — so
    a short read cannot produce an under-5-MiB non-final part. We keep the loop anyway, because
    "the layer above happens to compensate" is not an invariant, and a read that returns 0 bytes
    before the declared end is a real truncation that must RAISE rather than silently end the
    upload with a plausible short object.

    Resume-on-drop is deliberately NOT implemented. A mid-stream connection drop aborts the whole
    object; the caller retries it from byte 0. Ranged resume would let a retry stitch together two
    halves of two DIFFERENT server responses, and the sha256 would still come out "valid" because
    we computed it over whatever we stitched. Restarting is slower and honest. `--workers` and
    idempotent skip make a rerun cheap.
    """

    def __init__(self, url: str, expected_bytes: int, *, timeout: int = 300) -> None:
        self.url = url
        self.expected_bytes = expected_bytes
        self._timeout = timeout
        self._resp = None
        self.content_length: int | None = None
        self.bytes_read = 0
        self.n_429 = 0

    def _open(self) -> None:
        last: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            _RATE_GATE.wait()
            req = urllib.request.Request(self.url, headers={"User-Agent": _UA})
            try:
                self._resp = urllib.request.urlopen(req, timeout=self._timeout)
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code not in _TRANSIENT_STATUSES or attempt == _MAX_ATTEMPTS - 1:
                    hint = ""
                    if exc.code == 403:
                        hint = (
                            " — 403 from Cloudflare here is a User-Agent rejection, NOT a "
                            "permissions problem, and no retry fixes it. See _UA."
                        )
                    raise IngestError(
                        f"GET failed after {attempt + 1} attempts: {self.url}: {exc}{hint}"
                    ) from last
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = _backoff_delay(attempt, retry_after)
                if exc.code == 429:
                    # Only 429 penalises the SHARED gate: it means WE are sending too fast, so
                    # everyone must slow. A 503 is the server's own problem, and throttling the
                    # whole fleet over one bad object would idle seven workers for nothing.
                    self.n_429 += 1
                    _RATE_GATE.penalise(delay)
                time.sleep(delay)
                continue
            except Exception as exc:  # noqa: BLE001 - transport retry
                last = exc
                if attempt == _MAX_ATTEMPTS - 1:
                    raise IngestError(
                        f"GET failed after {_MAX_ATTEMPTS} attempts: {self.url}: {exc}"
                    ) from last
                time.sleep(min(_BACKOFF_CAP_S, 3 * (attempt + 1)))
                continue

            got = self._resp.headers.get("Content-Length")
            self.content_length = int(got) if got is not None else None
            # WITNESS 1 of 4, and the cheapest place to fail: refuse before a single byte is
            # uploaded if the origin does not agree with the pinned size. A `Content-Length` we
            # never compare is decoration.
            if self.content_length != self.expected_bytes:
                self.close()
                raise IngestError(
                    f"Content-Length mismatch for {self.url}: origin says "
                    f"{self.content_length}, pinned witness says {self.expected_bytes}. "
                    f"Upstream changed, or the sizes JSON is stale. Refusing to ingest."
                )
            return
        raise IngestError(f"unreachable: {self.url}") from last

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        if self._resp is None:
            self._open()
        if n is None or n < 0:
            raise IngestError(
                "unbounded read requested; the uploader must read in bounded parts or a 16 GiB "
                "shard lands in RAM — which is the single thing this driver exists to avoid"
            )
        remaining = self.expected_bytes - self.bytes_read
        if remaining <= 0:
            return b""
        n = min(n, remaining)
        out = bytearray()
        while len(out) < n:
            chunk = self._resp.read(n - len(out))
            if not chunk:
                # A real truncation. Returning short here is the whole failure mode: put_stream
                # would complete a plausible, smaller object and the assert would only catch it
                # after the bytes were already in landing.
                raise IngestError(
                    f"short read: got {self.bytes_read + len(out)} of {self.expected_bytes} bytes "
                    f"from {self.url}. Connection dropped mid-object; rerun (idempotent skip will "
                    f"not skip this key, because its size will not match)."
                )
            out += chunk
        self.bytes_read += len(out)
        return bytes(out)

    def close(self) -> None:
        if self._resp is not None:
            try:
                self._resp.close()
            except Exception:  # noqa: BLE001 - closing a dead socket is not load-bearing
                pass
            self._resp = None


class _WitnessReader:
    """`ingest_prm800k.py:151-190` `_HashingReader`, with the pinned-size tripwire kept.

    Records EXACTLY the bytes `put_stream` consumed and hashes them on the way past. This is the
    golden rule on the write path: the digest is computed from the same bytes handed to S3, so the
    two cannot disagree — unlike a caller-supplied digest, which can describe bytes other than the
    ones sent and would read as verification while proving nothing (`s3.py:404-431`).
    """

    def __init__(self, source: BinaryIO, *, expected_bytes: int) -> None:
        self._source = source
        self._hash = hashlib.sha256()
        self.bytes_read = 0
        self._expected_bytes = expected_bytes

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            raise IngestError("stream uploader requested an unbounded source read")
        chunk = self._source.read(size)
        if not isinstance(chunk, bytes):
            raise IngestError("origin returned a non-bytes body chunk")
        if self.bytes_read + len(chunk) > self._expected_bytes:
            # Fail BEFORE returning this chunk to the multipart uploader. An oversized transport
            # response must not become an accepted (or even completed) S3 payload merely because
            # its eventual digest would fail after the whole transfer.
            raise IngestError(
                f"origin returned more bytes than the pinned witness permits "
                f"({self.bytes_read + len(chunk)} > {self._expected_bytes})"
            )
        self._hash.update(chunk)
        self.bytes_read += len(chunk)
        return chunk

    @property
    def sha256(self) -> str:
        return self._hash.hexdigest()


# --------------------------------------------------------------------------------------
# Part size — justified against S3's 10,000-part limit
# --------------------------------------------------------------------------------------

#: 64 MiB parts. The arithmetic, against the three real S3 limits:
#:
#:   * 10,000 parts/upload  -> ceiling 64 MiB x 10,000 = 640 GiB per object.
#:     Our largest selected shard is 17,179,868,800 B = EXACTLY 16.0 GiB (dclm part-000-00000 and
#:     several siblings; measured, not estimated) = 256 parts. 39x headroom.
#:   * 5 MiB minimum non-final part (`s3.py:MIN_MULTIPART_PART_BYTES`) -> satisfied 12.8x over.
#:     `_read_stream_part` (`s3.py:186-203`) fills a part before uploading, so a short HTTP read
#:     cannot produce an undersized non-final part.
#:   * Memory: ONE part is buffered at a time, per worker. 64 MiB x 8 workers = 512 MiB peak
#:     payload buffer, comfortable inside a 32 GiB Batch container and independent of the 16 GiB
#:     shard size — which is the requirement.
#:
#: Why not the inherited 16 MiB default (`s3.py:_STREAM_PART_BYTES`): a 16 GiB shard would be 1,024
#: parts, i.e. 1,024 sequential `upload_part` round trips per object. Still legal, but 4x the
#: per-part latency for no benefit. Why not 128 MiB: it doubles peak RAM per worker to buy nothing
#: — 256 parts is already nowhere near 10,000.
#:
#: ⚠️ Do NOT raise this to chase throughput without re-doing the 10,000-part arithmetic against the
#: LARGEST object, not the median. At 5 MiB (the minimum) the ceiling is 48.8 GiB, which our 16.0
#: GiB shard fits — so the part limit is not what binds here; latency is.
PART_BYTES = 64 * 1024**2
MIN_PART_BYTES = 5 * 1024**2  # s3.py:MIN_MULTIPART_PART_BYTES, restated for the guard


# --------------------------------------------------------------------------------------
# Plan: source key -> our ordinal, allocated GLOBALLY and CONTIGUOUSLY from 00000
# --------------------------------------------------------------------------------------


def load_sizes() -> dict[str, tuple[str, int]]:
    """`{source_key: (label, bytes)}` from the measured sizes JSON.

    Each row is `[label, key, http_status, bytes]` and every row's status is 200 — these are HEAD
    observations of the real origin, not card figures.
    """
    rows = json.loads((HERE / "olmoe_0824_sizes.json").read_text(encoding="utf-8"))
    out: dict[str, tuple[str, int]] = {}
    for label, key, status, size in rows:
        if status != 200:
            raise SystemExit(f"REFUSING: sizes JSON has a non-200 row for {key} ({status})")
        out[key] = (label, int(size))
    return out


def build_plan(release: str) -> dict:
    """The deterministic source->dest mapping for one release. Pure function of two JSON files.

    ORDINALS ARE GLOBAL AND CONTIGUOUS FROM 00000 ACROSS ALL SOURCES. Not per-source. The layout
    is FLAT — `tokens/train-NNNNN.u32le.bin`, no `tokens/<source>/…` nesting — for two reasons, in
    order of how badly each would bite:

      1. Ordinal REUSE across a nested layout is the one real contradiction in the standard. A
         nested tree with per-source numbering produces `tokens/dclm/train-00000.u32le.bin` AND
         `tokens/pes2o/train-00000.u32le.bin`. `parse_shard_name` operates on the BASENAME
         (`manifest.py:675-687`) so both parse to `("train", 0)`, and any consumer that keys on
         (split, ordinal) silently collapses them.
      2. Nesting would put the source name in the KEY, which `labels_from_path`
         (`manifest.py:698-720`) turns into `entry.labels = {"source": …}` — inside
         `manifest_sha256`, therefore unbackfillable. That is a real feature and a reasonable
         future choice; it is not needed to publish, and it is not free. We keep it out of v1 and
         record the per-source composition in `sources[]` and in the mapping file instead.

    Ordering is `sorted((label, key))` — a total order over two strings, so the assignment is
    reproducible from the inputs alone with no clock, no dict iteration order, and no PRNG. Rerun
    this function on the same two JSON files a year from now and every ordinal is identical.
    """
    sizes = load_sizes()
    sel = json.loads((HERE / f"olmoe_final_{release}.json").read_text(encoding="utf-8"))

    # Every key must be a key we actually measured, or its "witness" is a guess.
    for split in ("train", "val"):
        for label, keys in sel[split].items():
            for k in keys:
                if k not in sizes:
                    raise SystemExit(f"REFUSING: selected key has no measured size: {k}")
                if sizes[k][0] != label:
                    raise SystemExit(
                        f"REFUSING: {k} is labelled {label!r} in the selection but "
                        f"{sizes[k][0]!r} in the sizes JSON"
                    )

    # The val split is exactly one whole shard. We cannot carve by DOCUMENT — a packed token shard
    # has no document boundaries in it, and the `.csv.gz` doc-offset sidecars are a separate
    # upstream artifact we are not ingesting — so the smallest carvable unit is a file.
    val_keys_flat = [k for keys in sel["val"].values() for k in keys]
    if len(val_keys_flat) != 1:
        raise SystemExit(f"REFUSING: expected exactly 1 val shard, selection has "
                         f"{len(val_keys_flat)}. `families/pretrain.json` sets "
                         f"validation_required: true and globs val-*.u32le.bin.")
    val_tokens = sizes[val_keys_flat[0]][1] // BYTES_PER_TOKEN
    if val_tokens <= VAL_MIN_TOKENS:
        raise SystemExit(
            f"REFUSING: val shard {val_keys_flat[0]} is {val_tokens:,} tokens, at or below the "
            f"{VAL_MIN_TOKENS:,} floor. See VAL_MIN_TOKENS: the tiny-shard trap already produced a "
            f"0.0624 Gtok val split once."
        )

    entries: list[dict] = []
    for split in ("train", "val"):
        ordered = sorted((label, k) for label, keys in sel[split].items() for k in keys)
        for ordinal, (label, key) in enumerate(ordered):
            _, size = sizes[key]
            # A shard whose byte count is not a whole number of uint32 tokens cannot be a token
            # shard: the standard's invariant is `tokens * dtype_size == file bytes` EXACTLY.
            # Verified for all 44 selected objects; asserted anyway, because the check is free and
            # a future reselection is not required to be as careful.
            if size % BYTES_PER_TOKEN:
                raise SystemExit(
                    f"REFUSING: {key} is {size} bytes, not a multiple of {BYTES_PER_TOKEN}; "
                    f"tokens * dtype_size == file bytes cannot hold"
                )
            entries.append(
                {
                    "source_key": key,
                    "source_url": ORIGIN + key,
                    "label": label,
                    "split": split,
                    "ordinal": ordinal,
                    "dest_rel": f"tokens/{split}-{ordinal:05d}{EXTENSION}",
                    "expected_bytes": size,
                    "tokens": size // BYTES_PER_TOKEN,
                }
            )

    # A duplicated source key would produce two byte-identical destinations, which Gate A rejects
    # as `duplicate-shard-digest` (`validate.py:748-757`) — after the transfer. Catch it here.
    keys = [e["source_key"] for e in entries]
    if len(set(keys)) != len(keys):
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        raise SystemExit(f"REFUSING: source key appears twice in the selection: {dupes}")

    # THE LEAKAGE CHECK, and it is the one that matters. A val shard that is a byte-copy of a train
    # shard is 100% leakage. Gate A would catch it (`duplicate-shard-digest`, same group), but only
    # after ~200-390 GiB has moved, and only because train and val share ONE group here.
    train_keys = {e["source_key"] for e in entries if e["split"] == "train"}
    val_keys = {e["source_key"] for e in entries if e["split"] == "val"}
    shared = train_keys & val_keys
    if shared:
        raise SystemExit(
            f"REFUSING: {len(shared)} source object(s) are in BOTH train and val within this "
            f"release: {sorted(shared)}. A val shard that is a byte-copy of a train shard is 100% "
            f"leakage validated clean by every checksum gate ever written."
        )

    # ...and across BOTH releases. The 50B train set is a strict subset of the 100B train set, so a
    # val shard that is clean within its own release can still be a 100B train shard — which would
    # make the 100B release leak through a corpus the same team also trains on. This exact mistake
    # was present in an earlier selection: the 50B val was arxiv part-02, which was simultaneously
    # a 100B TRAIN shard. Gate A cannot see it: `duplicate-shard-digest` is scoped to one group of
    # one dataset (`validate.py:748-757`), and two independent releases share no group. Nothing
    # downstream catches it either, since the releases deliberately do not declare each other via
    # depends_on, so `shared-sha-with-parent` (`validate.py:759-768`) never fires.
    other = "100b" if release == "50b" else "50b"
    other_sel = json.loads((HERE / f"olmoe_final_{other}.json").read_text(encoding="utf-8"))
    other_train = {k for keys in other_sel["train"].values() for k in keys}
    cross = val_keys & other_train
    if cross:
        raise SystemExit(
            f"REFUSING: this release's val shard(s) {sorted(cross)} are TRAIN shards of the "
            f"{other} release. Both releases go to the same consumers; that is leakage no gate in "
            f"this repo can see, because the two datasets share no group and no depends_on."
        )

    prefix = f"{INGEST_ROOT}/{release}"
    total = sum(e["expected_bytes"] for e in entries)

    # Reconcile against the generator's own `realized` block. Both this function and
    # `make_final_selection.py` compute totals from the same sizes JSON, so a disagreement means
    # one of the two files is stale — which is exactly the failure that published a PLANNED figure
    # under a "realized" heading twice in this repo. The publish driver reads `realized` verbatim
    # for the README, so it must be the block this driver just proved.
    realized = sel.get("realized")
    if realized is None:
        raise SystemExit(f"REFUSING: olmoe_final_{release}.json has no `realized` block")
    train_bytes = sum(e["expected_bytes"] for e in entries if e["split"] == "train")
    val_bytes = sum(e["expected_bytes"] for e in entries if e["split"] == "val")
    for field, computed in (
        ("train_shards", len(train_keys)),
        ("train_bytes", train_bytes),
        ("train_tokens", train_bytes // BYTES_PER_TOKEN),
        ("val_bytes", val_bytes),
        ("val_tokens", val_bytes // BYTES_PER_TOKEN),
        ("total_bytes", total),
        ("total_tokens", total // BYTES_PER_TOKEN),
    ):
        if realized[field] != computed:
            raise SystemExit(
                f"REFUSING: olmoe_final_{release}.json realized.{field} = {realized[field]:,} but "
                f"the selection sums to {computed:,}. Re-run make_final_selection.py."
            )
    return {
        "release": release,
        "landing_bucket": LANDING_BUCKET,
        "dest_prefix": prefix,
        # SIBLING of the release prefix, never inside it — see the module docstring. A marker at
        # `<release>/_INGEST_COMPLETE.json` is enumerated as payload by publish() and kills it.
        "marker_key": f"{INGEST_ROOT}/_receipts/{release}-INGEST_COMPLETE.json",
        "publish_source_uri": f"s3://{LANDING_BUCKET}/{prefix}/",
        "origin": ORIGIN,
        "tokenizer": TOKENIZER_URI,
        "vocab_size": VOCAB_SIZE,
        "eos_token_id": EOS_TOKEN_ID,
        "format": {
            "container": "raw",
            "dtype": "uint32",
            "byte_order": "little",
            "header_bytes": 0,
            "codec": "none",
        },
        "ordinal_allocation": "global-contiguous-from-00000, sorted by (label, source_key)",
        "entries": entries,
        "realized": realized,
        "totals": {
            "objects": len(entries),
            "bytes": total,
            "tokens": total // BYTES_PER_TOKEN,
            "train_objects": len(train_keys),
            "val_objects": len(val_keys),
            "train_tokens": train_bytes // BYTES_PER_TOKEN,
            "val_tokens": val_bytes // BYTES_PER_TOKEN,
        },
    }


# --------------------------------------------------------------------------------------
# Transfer
# --------------------------------------------------------------------------------------


def _already_done(s3, entry: dict, prefix: str) -> bool:
    """Idempotent skip: destination exists at EXACTLY the pinned size.

    Size, not just existence. A previous run killed mid-`put_stream` leaves no object (multipart is
    aborted on failure, `s3.py:534-541`), but a run killed between `complete_multipart_upload` and
    our own assert leaves a complete object — and the only cheap way to tell a good one from a
    truncated one is its length against the pinned witness. Size-only is a weaker check than a
    re-hash; the re-hash happens anyway inside `publish()`, which stream-hashes every object it
    enumerates (`publish.py:418-425`), so paying for it twice here buys nothing.
    """
    from edullm_data.s3 import NotFound

    try:
        head = s3.head(LANDING_BUCKET, f"{prefix}/{entry['dest_rel']}")
    except NotFound:
        return False
    return int(head["size"]) == entry["expected_bytes"]


def transfer_one(s3, entry: dict, prefix: str, part_bytes: int) -> dict:
    """Stream one object HTTP -> S3 and prove the four witnesses agree."""
    dest_key = f"{prefix}/{entry['dest_rel']}"
    expected = entry["expected_bytes"]

    body = _HttpBody(entry["source_url"], expected)
    reader = _WitnessReader(body, expected_bytes=expected)
    try:
        head = s3.put_stream(
            LANDING_BUCKET,
            dest_key,
            reader,
            part_size=part_bytes,
            content_type="application/octet-stream",
        )
    finally:
        body.close()

    # RECOMPUTE, NEVER TRUST — all four witnesses, compared, and a loud failure on any mismatch.
    #   1. expected : the pinned size from olmoe_0824_sizes.json (a live HEAD, status 200)
    #   2. received : Content-Length the origin actually sent  (checked in _HttpBody._open)
    #   3. consumed : bytes _WitnessReader actually handed to put_stream
    #   4. written  : ContentLength S3 reports for the completed object (a fresh head())
    written = int(head.get("size", -1))
    if not (expected == body.content_length == reader.bytes_read == written):
        # Best-effort removal of a known-bad object. The dedicated ingest role holds no
        # s3:DeleteObject, so this normally fails and the object stays — receipt-less, and clearly
        # distinguishable as a partial run until the 14-day lifecycle expires
        # (`ingest_prm800k.py:625-631` takes the same position for the same reason).
        try:
            s3.delete(LANDING_BUCKET, dest_key)
        except Exception:  # noqa: BLE001
            pass
        raise IngestError(
            f"WITNESS MISMATCH for {entry['source_key']} -> {dest_key}: "
            f"pinned={expected} content_length={body.content_length} "
            f"consumed={reader.bytes_read} written={written}"
        )
    if written % BYTES_PER_TOKEN:
        raise IngestError(f"{dest_key}: {written} bytes is not a whole number of uint32 tokens")

    return {
        "source_key": entry["source_key"],
        "label": entry["label"],
        "split": entry["split"],
        "ordinal": entry["ordinal"],
        "dest_key": dest_key,
        "bytes": written,
        "tokens": written // BYTES_PER_TOKEN,
        "sha256": reader.sha256,
        "crc64nvme": head.get("crc64nvme"),
        "content_length_received": body.content_length,
        "n_429": body.n_429,
    }


def run(plan: dict, *, workers: int, part_bytes: int) -> int:
    from edullm_data.s3 import Boto3S3

    # ⚠️ SIZED CONNECTION POOL. `Boto3S3.default()` with no argument passes no
    # `botocore.config.Config`, so `max_pool_connections` is botocore's default 10 — and botocore
    # does NOT pass `block=True` to urllib3, so exceeding the pool neither raises nor waits:
    # urllib3 DISCARDS the surplus connection and logs "Connection pool is full". Workers 11..N
    # pay a fresh TLS handshake per request and the fan-out silently caps itself. The failure mode
    # is a NUMBER THAT DOES NOT IMPROVE, with no error anywhere. See `s3.py:211-249`.
    # `workers + 4` leaves room for the head()/delete() calls that share the client.
    s3 = Boto3S3.default(max_pool_connections=workers + 4)
    prefix = plan["dest_prefix"]

    todo, skipped = [], []
    for entry in plan["entries"]:
        (skipped if _already_done(s3, entry, prefix) else todo).append(entry)
    print(f"{len(skipped)} object(s) already present at the right size — skipping")
    print(f"{len(todo)} object(s) to transfer, "
          f"{sum(e['expected_bytes'] for e in todo) / 1024**3:.1f} GiB", flush=True)

    receipts: list[dict] = []
    failures: list[str] = []
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(transfer_one, s3, e, prefix, part_bytes): e for e in todo}
            for i, fut in enumerate(as_completed(futures), 1):
                entry = futures[fut]
                try:
                    receipt = fut.result()
                    receipts.append(receipt)
                    print(f"  [{i}/{len(todo)}] OK {receipt['dest_key']} "
                          f"{receipt['bytes']:,} B  sha256={receipt['sha256'][:16]}…", flush=True)
                except Exception as exc:  # noqa: BLE001 - reported, then re-raised in aggregate
                    failures.append(f"{entry['source_key']}: {exc}")
                    print(f"  [{i}/{len(todo)}] FAIL {entry['dest_rel']}: {exc}", flush=True)

    if failures:
        print(f"\n{len(failures)} object(s) FAILED; writing NO completion marker. "
              f"Rerun — completed objects are skipped by size.", file=sys.stderr)
        for f in failures:
            print("  " + f, file=sys.stderr)
        return 1

    # Re-derive the receipt set for skipped objects too, so the marker describes the WHOLE prefix
    # rather than only this attempt. Sizes come from a fresh head(); sha256 is unavailable for an
    # object we did not stream, and is left null rather than guessed — publish() computes the real
    # one by streaming every object it enumerates (`publish.py:418-425`), so nothing downstream
    # depends on this field.
    for entry in skipped:
        head = s3.head(LANDING_BUCKET, f"{prefix}/{entry['dest_rel']}")
        receipts.append(
            {
                "source_key": entry["source_key"],
                "label": entry["label"],
                "split": entry["split"],
                "ordinal": entry["ordinal"],
                "dest_key": f"{prefix}/{entry['dest_rel']}",
                "bytes": int(head["size"]),
                "tokens": int(head["size"]) // BYTES_PER_TOKEN,
                "sha256": None,
                "crc64nvme": head.get("crc64nvme"),
                "content_length_received": None,
                "n_429": 0,
                "reused_from_earlier_attempt": True,
            }
        )

    if len(receipts) != plan["totals"]["objects"]:
        print(f"REFUSING to mark complete: {len(receipts)} receipts for "
              f"{plan['totals']['objects']} planned objects", file=sys.stderr)
        return 1
    got = sum(r["bytes"] for r in receipts)
    if got != plan["totals"]["bytes"]:
        print(f"REFUSING to mark complete: {got} bytes present, {plan['totals']['bytes']} planned",
              file=sys.stderr)
        return 1

    # ⚠️ THE COMPLETION MARKER IS WRITTEN LAST, AND IT IS DELIBERATELY NOT NAMED `manifest.json`.
    # The live EventBridge rule `edullm-landing-manifest-created` matches `detail.object.key` by
    # SUFFIX `manifest.json` with NO prefix filter (`infra/04-event-wiring.yaml:176-201`), so ANY
    # object anywhere in `edullm-landing` whose key ends in `manifest.json` fires the validator
    # against a prefix — immediately, and by unversioned job-def name. The rule is DISABLED today
    # (`infra/DEPLOY.md:487`) and that is not something to build on.
    #
    # An ingest identity is also explicitly DENIED the name: see
    # `infra/08-reservoir-ingest-policy.json` -> `NeverWriteValidatorTriggeringOrTerminalNames`,
    # a Deny on `edullm-landing/*manifest.json` and `*dataset.json`.
    #
    # The group manifest is `publish()`'s to write, LAST, as step 3 of its own ordering
    # (`publish.py:1044-1051`). This marker only says "the payload prefix is complete; publish may
    # read it". It lives under `_ingest/`, which `publish()` skips as a control prefix
    # (`publish.py:210-241`), so it can never become a manifest entry.
    marker = {
        "schema": "edullm-olmoe-ingest/v1",
        "release": plan["release"],
        "origin": plan["origin"],
        "upstream_mix": "OLMoE-mix-0824",
        "key_list_source": "OLMo-core/src/olmo_core/data/mixes/OLMoE-mix-0824.txt "
                           "with {TOKENIZER} -> allenai/dolma2-tokenizer",
        "tokenizer": plan["tokenizer"],
        "format": plan["format"],
        "ordinal_allocation": plan["ordinal_allocation"],
        "transform": "copy + rename .npy -> .u32le.bin; no tokenization, no dedup, no decontam",
        "totals": plan["totals"],
        "objects": sorted(receipts, key=lambda r: r["dest_key"]),
    }
    body = json.dumps(marker, indent=2, sort_keys=True).encode("utf-8")
    s3.put_bytes_verified(
        LANDING_BUCKET, plan["marker_key"], body, content_type="application/json"
    )
    print(f"\nWROTE {plan['marker_key']} ({len(receipts)} objects, {got:,} bytes)")
    print(f"publish() source: {plan['publish_source_uri']}")
    if _RATE_GATE.total_penalties:
        print(f"NOTE: {_RATE_GATE.total_penalties} rate-limit penalties. If large, lower --workers.")
    return 0


def print_plan(plan: dict, part_bytes: int) -> None:
    t = plan["totals"]
    print(f"release        : {plan['release']}")
    print(f"origin         : {plan['origin']}  (UA {_UA!r})")
    print(f"destination    : s3://{LANDING_BUCKET}/{plan['dest_prefix']}/")
    print(f"publish source : {plan['publish_source_uri']}")
    print(f"completion     : s3://{LANDING_BUCKET}/{plan['marker_key']}  (OUTSIDE the source "
          f"prefix, so publish() cannot enumerate it as payload)")
    print(f"format         : {plan['format']}   tokenizer {plan['tokenizer']}")
    print(f"ordinals       : {plan['ordinal_allocation']}")
    print(f"part size      : {part_bytes / 1024**2:.0f} MiB")
    biggest = max(e["expected_bytes"] for e in plan["entries"])
    print(f"largest object : {biggest:,} B = {biggest / 1024**3:.2f} GiB "
          f"= {-(-biggest // part_bytes)} parts (limit 10,000)")
    print()
    print(f"{'source key':92s} {'->':2s} {'dest':30s} {'bytes':>15s} {'tokens':>15s}")
    for e in plan["entries"]:
        print(f"{e['source_key']:92s} -> {e['dest_rel']:30s} "
              f"{e['expected_bytes']:15,} {e['tokens']:15,}   [{e['label']}]")
    print()
    print(f"train : {t['train_objects']:3d} objects  {t['train_tokens']:,} tokens")
    print(f"val   : {t['val_objects']:3d} objects  {t['val_tokens']:,} tokens  "
          f"({100 * t['val_tokens'] / t['tokens']:.3f}% of release)")
    print(f"TOTAL : {t['objects']:3d} objects  {t['bytes']:,} bytes = "
          f"{t['bytes'] / 1024**3:.2f} GiB  {t['tokens']:,} tokens")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--release", required=True, choices=("50b", "100b"))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="print the full plan, write nothing")
    g.add_argument("--go", action="store_true", help="transfer into edullm-landing")
    ap.add_argument("--workers", type=int, default=8,
                    help="concurrent objects (default 8). The pool is sized to match.")
    ap.add_argument("--part-bytes", type=int, default=PART_BYTES)
    ap.add_argument("--emit-mapping", type=str, default=None,
                    help="write the deterministic source->ordinal mapping to this JSON path")
    args = ap.parse_args(argv)

    if args.part_bytes < MIN_PART_BYTES:
        raise SystemExit(f"--part-bytes must be >= {MIN_PART_BYTES} (S3's non-final part minimum)")
    if args.workers < 1:
        raise SystemExit("--workers must be >= 1")

    plan = build_plan(args.release)
    print_plan(plan, args.part_bytes)

    if args.emit_mapping:
        pathlib.Path(args.emit_mapping).write_text(
            json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(f"\nwrote mapping -> {args.emit_mapping}")

    if args.dry_run:
        print("\nDRY RUN — nothing written. Run with --go inside an in-region Batch job.")
        return 0
    return run(plan, workers=args.workers, part_bytes=args.part_bytes)


if __name__ == "__main__":
    sys.exit(main())
