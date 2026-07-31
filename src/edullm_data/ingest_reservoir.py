"""Batch ingest for `pretrain/reservoir-dolma2` — §5.6 phase 0 (HF ingest) with §9.7 item 4 folded in.

This is a BUILD driver, not part of the publishing library. It pulls upstream documents into S3
and applies the one transformation that has a build-time deadline. It writes no `dataset.json`,
no manifest, and nothing to `edullm-data`.

WHY THE PARTITION LIVES HERE AND NOT IN A LATER PASS
----------------------------------------------------
§9.7 item 4 must run before tokenization, because after tokenization there is no document→id
mapping left (§9.7 item 3 declined to emit one). The cheapest correct place is the pass that
already reads every row for the first time — this one. Doing it later means re-tokenizing the
synthetic half.

TWO LANDMINES IN THE DEPLOYED INFRASTRUCTURE, both verified live 2026-07-31
---------------------------------------------------------------------------
1. **`edullm-landing-manifest-created` matches key suffix `manifest.json` ANYWHERE in the
   bucket** — the EventBridge pattern is `{"object":{"key":[{"suffix":"manifest.json"}]}}`, with
   no prefix constraint. So a file named `manifest.json` written anywhere under landing fires the
   validator. This driver therefore never writes that basename; its per-run index is
   `_index.json`, and `_assert_safe_key` refuses the reserved names outright rather than relying
   on us to remember.

2. **`_ingest/` HAS NO LIFECYCLE RULE.** Landing's 14-day expiry is prefix-scoped to
   `pretrain/ curriculum/ sft/ eval/ probe/ vendor/ _pending/`. An `_ingest/` prefix is covered by
   NONE of them, so ~2.5 TB would persist indefinitely (~$59/month, forever) rather than expiring
   with the build. `infra/07-landing-ingest-lifecycle.json` closes this and MUST be deployed before
   a full run; `--require-lifecycle` (default on) makes the driver refuse to start until it is.

WHERE IT RUNS
-------------
Batch, in-region, per §5.7 — the constraint is that no dataset byte transits a laptop. Both
subcommands refuse to run without `AWS_BATCH_JOB_ID`, matching the prm800k ingest's guard. That
check is a fail-fast, not an authorization boundary: the authorization boundary is the ingest
role's IAM policy, which grants no write to `edullm-data`.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .reservoir_ids import (
    FINEPHRASE_FORMATS,
    IdPartitionError,
    audit_partition,
    keeps_id,
)

__all__ = [
    "FINEPHRASE_REPO",
    "FINEWEB_EDU_REPO",
    "REWRITE_LEAF",
    "IngestError",
    "IdSet",
    "hf_tree",
    "main",
]

# --------------------------------------------------------------------------------------
# Upstream identity — pinned, because "main" is mutable
# --------------------------------------------------------------------------------------

FINEPHRASE_REPO = "HuggingFaceFW/finephrase"
FINEWEB_EDU_REPO = "HuggingFaceFW/fineweb-edu"

#: THE COLUMN THAT MATTERS, and the one §3.3 trap 1 is about. FinePhrase's `text` column holds the
#: ORIGINAL FineWeb-Edu document — its `dataset` field literally reads `HuggingFaceFW/fineweb-edu`.
#: The synthetic rewrite is at `rollout_results[0].text`, whose parquet leaf path is below.
#:
#: Ingesting `text` builds a reservoir of UNREPHRASED FineWeb-Edu labelled synthetic, and no hash,
#: size, or token count catches it. `md.schema.names.index("text")` returns the original (index 0)
#: because the flat leaf list contains `text` twice — measured, and the reason
#: `artifacts/recount/_footer_chars.py` grew a `_resolve_leaf` guard.
REWRITE_LEAF = "rollout_results.list.element.text"

#: Basenames this driver must never write into landing. `manifest.json` fires the validator
#: (landmine 1); the two markers suppress validator discovery if they appear on a real dataset
#: prefix, and there is no reason a build driver should ever author one.
_RESERVED_BASENAMES = frozenset({"manifest.json", "_VALIDATED.json", "_REJECTED.json", "dataset.json"})

#: Landing prefixes that carry a 14-day expiry rule, verified live against
#: `get-bucket-lifecycle-configuration` on 2026-07-31.
_EXPIRING_LANDING_PREFIXES = ("pretrain/", "curriculum/", "sft/", "eval/", "probe/", "vendor/", "_pending/")


class IngestError(RuntimeError):
    """A precondition failed, or an upstream read produced something unusable."""


# --------------------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------------------


def _require_batch(allow_local: bool) -> None:
    """Refuse to move corpus bytes outside Batch (§5.7).

    `--allow-local` exists for the metadata-only `plan` subcommand and for tests; it is refused
    for anything that reads or writes payload, because the measured local throughput (0.8 MiB/s)
    turns this corpus into a multi-week transfer.
    """
    if allow_local:
        return
    if not os.environ.get("AWS_BATCH_JOB_ID"):
        raise IngestError(
            "AWS_BATCH_JOB_ID is not set, so this is not a Batch job. §5.7 forbids moving corpus "
            "bytes through anything but in-region compute — measured 0.8 MiB/s locally, which is "
            "weeks for this corpus. Use --allow-local only for metadata-only subcommands."
        )


def _assert_safe_key(key: str) -> str:
    """Refuse a destination key that would trip EventBridge or forge a validator marker.

    This is a mechanical guard rather than a convention because the failure is invisible at write
    time: uploading `.../manifest.json` to landing returns 200 and *then* fires the validator,
    which discovers a prefix with no `dataset.json` and reports on a build artifact.
    """
    base = key.rsplit("/", 1)[-1]
    if base in _RESERVED_BASENAMES:
        raise IngestError(
            f"refusing to write {key!r}: the basename {base!r} is reserved. "
            f"`edullm-landing-manifest-created` matches suffix `manifest.json` with NO prefix "
            f"constraint, so writing one here would fire the validator against a build artifact."
        )
    return key


def _assert_lifecycle_covers(s3_client, bucket: str, prefix: str) -> None:
    """Refuse to stage terabytes under a prefix nothing will ever expire.

    Landing's expiry rules are prefix-scoped. `_ingest/` matches none of them, so without this
    the build's working set becomes a permanent bill that nobody notices until it appears on an
    invoice — the class of defect this project's audit was created to find.
    """
    try:
        conf = s3_client.get_bucket_lifecycle_configuration(Bucket=bucket)
        rules = conf.get("Rules", [])
    except Exception as exc:  # botocore raises NoSuchLifecycleConfiguration as a ClientError
        if "NoSuchLifecycleConfiguration" not in str(exc):
            raise
        rules = []
    for rule in rules:
        if rule.get("Status") != "Enabled" or "Expiration" not in rule:
            continue
        rule_prefix = rule.get("Filter", {}).get("Prefix", rule.get("Prefix", ""))
        if rule_prefix and prefix.startswith(rule_prefix):
            return
    covered = ", ".join(_EXPIRING_LANDING_PREFIXES)
    raise IngestError(
        f"no enabled Expiration lifecycle rule covers s3://{bucket}/{prefix} — staging ~2.5 TB "
        f"there would persist indefinitely (~$59/month, forever). Landing's expiry rules cover "
        f"only: {covered}. Deploy infra/07-landing-ingest-lifecycle.json first, or pass "
        f"--no-require-lifecycle if you are deliberately accepting an unexpiring prefix."
    )


# --------------------------------------------------------------------------------------
# HF transport — footers and row groups over HTTP Range, never a full download
# --------------------------------------------------------------------------------------


def _hf_headers() -> dict:
    h = {"User-Agent": "edullm-data/ingest-reservoir"}
    tok = os.environ.get("HF_TOKEN")
    if not tok:
        cached = os.path.expanduser("~/.cache/huggingface/token")
        if os.path.exists(cached):
            tok = open(cached).read().strip()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


#: Retry budget for one Range read. Five was not enough: at 3*(n+1) seconds it gives up after 30 s,
#: and the Hub's 429 window outlasts that comfortably — measured, see `_RateGate`.
_MAX_ATTEMPTS = 8

#: Ceiling on a single backoff sleep. Exponential from 4 s reaches 256 s by attempt 7; capping at
#: 120 s keeps a stuck worker from idling for half the job's timeout.
_BACKOFF_CAP_S = 120.0


def _backoff_delay(attempt: int, retry_after: str | None = None) -> float:
    """Seconds to wait before retry `attempt` (0-indexed), honouring `Retry-After` when present.

    Exponential, not linear. `PLAN-CORRECTIONS.md` §6 recorded the same bug in `recount.py` — "a
    3 s linear retry that could never outlast the limit" — and this module reintroduced it, which
    is why the note is repeated here at the point of the mistake rather than only in an artifact.

    `Retry-After` may be seconds or an HTTP date; only the numeric form is honoured, because a date
    parse that silently fails would give the *wrong* delay rather than an obviously absent one.
    """
    if retry_after:
        try:
            return min(_BACKOFF_CAP_S, max(1.0, float(retry_after)))
        except ValueError:
            pass
    return min(_BACKOFF_CAP_S, 4.0 * (2**attempt))


class _RateGate:
    """A process-wide brake shared by every worker thread.

    WHY A PER-WORKER RETRY IS NOT ENOUGH, and this is the whole point. The Hugging Face rate limit
    is **per IP, not per account** — established in Phase 0 (`PLAN-CORRECTIONS.md` §6, where eight
    parallel agents starved each other and the failures looked exactly like broken corpora). Every
    thread in this process shares one IP, so a worker that backs off privately while fifteen others
    keep hammering has changed nothing: the limit is a property of the *fleet*, not the thread.

    So a 429 anywhere pauses everywhere. `penalise()` sets a shared deadline; `wait()` blocks any
    thread about to start new work until that deadline passes. The effect is that the whole scan
    slows down together and then recovers, instead of collapsing into a retry storm.

    This is also why the array-job plan changed: sharding across N machines multiplies the request
    rate against a limit that does not care how many machines you have, so more children make the
    429s *worse*. Shards must be run in small waves, not all at once.
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


#: Module-level so every worker in the process shares it. Not a parameter: a per-call gate would be
#: a gate per thread, which is exactly the thing that does not work.
_RATE_GATE = _RateGate()


class _RangeFile(io.RawIOBase):
    """Seekable read-only file over HTTP Range, so pyarrow fetches footers and chosen column
    chunks and nothing else. Retries with exponential backoff behind a shared rate gate."""

    def __init__(self, url: str, size: int, headers: dict):
        self.url, self.size, self._h = url, size, headers
        self.pos = 0
        self.bytes_fetched = 0
        self.n_429 = 0

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, off: int, whence: int = 0) -> int:
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off)
        return self.pos

    def read(self, n: int = -1) -> bytes:
        """Return EXACTLY `n` bytes (or fewer only at true end-of-object).

        ⚠️ THE LOOP IS LOad-BEARING AND ITS ABSENCE SEGFAULTED THREE OF FOUR ARRAY CHILDREN.
        `RawIOBase.read` is *permitted* to return short, and a single `urlopen(...).read()` does
        exactly that when a 206 body arrives truncated — common under the rate limiting this
        module already fights, since a throttled connection can be cut mid-body.

        pyarrow does not re-request the remainder. It takes the short buffer, reads a page header
        at an offset that now lands inside the wrong bytes, and dereferences a garbage length:
        `Segmentation fault (core dumped)`, exit 139. Not exit 137 — this is a CRASH IN C++, not
        the container's memory cap, and that distinction is what separates "give it more RAM"
        (wrong, and I nearly did it) from "the buffer is short" (right).

        Deterministic rather than flaky: it hit whichever config followed the first successful
        one, on every child that ran long enough to be throttled. Shard 0 finished only because
        it happened not to be cut.

        So: loop until the request is satisfied. A read that returns 0 bytes before `n` is a real
        truncation and raises, because silently returning short is the whole failure mode.
        """
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        out = bytearray()
        while len(out) < n:
            chunk = self._read_once(n - len(out), self.pos + len(out))
            if not chunk:
                raise IngestError(
                    f"short read: got {len(out)} of {n} bytes at offset {self.pos} in {self.url}. "
                    f"Returning a short buffer here is what pyarrow turns into a SIGSEGV."
                )
            out += chunk
        self.pos += len(out)
        self.bytes_fetched += len(out)
        return bytes(out)

    def _read_once(self, n: int, start: int) -> bytes:
        """One ranged GET of at most `n` bytes from `start`, with 429-aware backoff."""
        req = urllib.request.Request(
            self.url, headers={**self._h, "Range": f"bytes={start}-{start + n - 1}"}
        )
        last: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    data = r.read()
                break
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code != 429 or attempt == _MAX_ATTEMPTS - 1:
                    raise IngestError(
                        f"range read failed after {attempt + 1} attempts: {self.url}: {exc}"
                    ) from last
                self.n_429 += 1
                # Honour Retry-After when the server sends one; it knows better than we do.
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = _backoff_delay(attempt, retry_after)
                _RATE_GATE.penalise(delay)
                time.sleep(delay)
            except Exception as exc:  # noqa: BLE001 - transport retry, re-raised below
                last = exc
                if attempt == _MAX_ATTEMPTS - 1:
                    raise IngestError(
                        f"range read failed after {_MAX_ATTEMPTS} attempts: {self.url}: {exc}"
                    ) from last
                time.sleep(min(_BACKOFF_CAP_S, 3 * (attempt + 1)))
        # `pos`/`bytes_fetched` are advanced by the CALLER once the full request is satisfied —
        # advancing here as well would double-count and desynchronise the next Range header.
        return data


def hf_tree(repo: str, path: str = "", *, headers: dict | None = None) -> list[dict]:
    """Every `.parquet` entry under `path` in `repo`, following the Link-header cursor.

    Paginated deliberately rather than trusting one page: each FinePhrase config holds ~6,800
    files, and an unpaginated read silently returns the first page — which would look like a small
    corpus rather than an error.

    ⚠️ **DO NOT ADD `expand=1`.** It is the obvious flag to reach for (it is what the Phase 0c
    footer tool used) and it is a 50x pessimisation here. Measured live 2026-07-31, per config:

        recursive=1            ~6,790 files in 7 pages of 1000, ~2.5 s
        recursive=1&expand=1   ~6,790 files in ~136 pages of 50, 26 s PER PAGE -> ~1 hour

    `expand=1` caps a page at 50 entries and does a per-entry lookup server-side; without it the
    page limit is 1000. The only reason to pay that would be a field the compact form omits, and
    the field this driver needs — `size`, for the Range reads — is present in BOTH (verified:
    `all_have_size=True` across all 27,104 files of all four configs). It also intermittently
    returns HTTP 500 under the slow path, which is how this was found: two background jobs sat
    silent for twenty minutes and looked like a slow network rather than a bad query string.
    """
    h = headers or _hf_headers()
    base = f"https://huggingface.co/api/datasets/{repo}/tree/main"
    url = f"{base}/{path}" if path else base
    url += "?recursive=1"
    out: list[dict] = []
    cursor: str | None = None
    while True:
        req = urllib.request.Request(url + (f"&cursor={cursor}" if cursor else ""), headers=h)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                page = json.loads(r.read())
                link = r.headers.get("Link", "")
        except urllib.error.HTTPError as exc:
            raise IngestError(f"HF tree read failed for {repo}/{path}: HTTP {exc.code}") from exc
        out += [e for e in page if e.get("path", "").endswith(".parquet")]
        if 'rel="next"' not in link:
            break
        cursor = link.split("cursor=")[1].split(">")[0].split("&")[0]
    missing_size = [e["path"] for e in out if "size" not in e][:3]
    if missing_size:
        raise IngestError(
            f"tree entries lack `size`, which the Range reader needs: {missing_size}. If the API "
            f"stopped returning it in the compact form, the fix is a HEAD per file — NOT expand=1, "
            f"which is 50x slower (see this function's docstring)."
        )
    if not out:
        raise IngestError(
            f"no parquet files under {repo}/{path!r}. FinePhrase's configs are TOP-LEVEL "
            f"directories (faq/, math/, table/, tutorial/), not under data/ — verified live."
        )
    return out


def _resolve_url(repo: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def _leaf_index(parquet_md, want: str) -> int:
    """Column-chunk index for an exact `path_in_schema`, refusing to guess.

    Deliberately exact-match only, with no bare-name fallback: this module's whole reason for
    naming `REWRITE_LEAF` in full is that a bare `"text"` resolves to the ORIGINAL document.
    A fallback would reintroduce exactly that.
    """
    rg0 = parquet_md.row_group(0)
    paths = [rg0.column(c).path_in_schema for c in range(rg0.num_columns)]
    if want not in paths:
        raise IngestError(
            f"leaf {want!r} not found; leaves are {paths}. Refusing a bare-name fallback: "
            f"`text` appears twice in FinePhrase's flat leaf list and index 0 is the ORIGINAL "
            f"FineWeb-Edu document, so guessing here silently ingests the wrong corpus."
        )
    return paths.index(want)


# --------------------------------------------------------------------------------------
# The id set that edu-web anti-joins against
# --------------------------------------------------------------------------------------


@dataclass
class IdSet:
    """A membership set over document ids, held as sorted 64-bit digest prefixes.

    WHY 64-BIT PREFIXES AND NOT A BLOOM FILTER. The design budgeted a Bloom filter (~400 MB at
    1% FPR over ~339 M ids). A sorted `uint64` array is the same order of memory (2.7 GB, fine on
    the `c7g.16xlarge` §5.7 already allocates) and has NO false-positive rate to reason about:
    the expected number of 64-bit collisions over 339 M ids is n²/2^65 ≈ 0.003, i.e. almost
    certainly zero, and a collision costs one wrongly-dropped edu-web document out of 261 B
    tokens. A Bloom filter's 1% FPR would drop ~1% of edu-web silently, which is 2.6 B tokens.

    Stored as raw little-endian `uint64` so a worker can mmap it without parsing.
    """

    values: object  # numpy.ndarray[uint64], sorted and unique

    @staticmethod
    def _digest64(doc_id: str) -> int:
        return int.from_bytes(hashlib.sha256(doc_id.encode("utf-8")).digest()[:8], "big")

    @classmethod
    def from_ids(cls, ids) -> IdSet:
        import numpy as np

        arr = np.fromiter((cls._digest64(i) for i in ids), dtype=np.uint64)
        return cls(values=np.unique(arr))

    @classmethod
    def from_digest_chunks(cls, chunks) -> IdSet:
        """Build from an iterable of already-digested `uint64` arrays.

        THIS IS THE PATH THE FULL RUN MUST USE, and the reason is a factor of 12. Holding one
        config's ids as Python strings costs `sys.getsizeof` 96 B each — at 339 M ids that is
        **32.5 GB per config, 130 GB for all four**, against **2.71 GB / 10.85 GB** for the same
        information as digests. The string list is pure overhead: nothing downstream needs the id
        text, only membership.

        So a worker digests each file's ids immediately and discards the strings, and this
        concatenates the small arrays. `from_ids` is kept for tests and small samples, where the
        difference is irrelevant and taking a list is more readable.
        """
        import numpy as np

        parts = [c for c in chunks if c is not None and len(c)]
        if not parts:
            return cls(values=np.empty(0, dtype=np.uint64))
        return cls(values=np.unique(np.concatenate(parts)))

    def __len__(self) -> int:
        return int(self.values.shape[0])

    def contains(self, doc_id: str) -> bool:
        import numpy as np

        v = np.uint64(self._digest64(doc_id))
        i = int(np.searchsorted(self.values, v))
        return i < len(self) and bool(self.values[i] == v)

    def to_bytes(self) -> bytes:
        return self.values.astype("<u8").tobytes()

    @classmethod
    def from_bytes(cls, raw: bytes) -> IdSet:
        import numpy as np

        if len(raw) % 8:
            raise IngestError(f"id set is {len(raw)} bytes, not a multiple of 8 — truncated upload")
        arr = np.frombuffer(raw, dtype="<u8")
        if arr.size > 1 and not bool((arr[1:] >= arr[:-1]).all()):
            raise IngestError("id set is not sorted; searchsorted membership would be wrong")
        return cls(values=arr)


# --------------------------------------------------------------------------------------
# Work units
# --------------------------------------------------------------------------------------


@dataclass
class FileResult:
    """One file's contribution.

    `digests` is a `uint64` numpy array, NOT the id strings, and `sample_ids` keeps only a bounded
    handful of the strings for the partition audit. That split is the difference between 2.71 GB
    and 32.5 GB per config (see `IdSet.from_digest_chunks`) — the id text has no downstream
    consumer, only membership does.
    """

    path: str
    rows_read: int = 0
    rows_kept: int = 0
    bytes_fetched: int = 0
    n_429: int = 0
    digests: object = None  # numpy.ndarray[uint64]
    sample_ids: list = field(default_factory=list)
    error: str | None = None

    @property
    def keep_fraction(self) -> float:
        return (self.rows_kept / self.rows_read) if self.rows_read else 0.0


def _scan_ids(
    repo: str,
    entry: dict,
    headers: dict,
    *,
    id_column: str = "id",
    sample_per_file: int = 2_000,
) -> FileResult:
    """Read ONLY the id column of one parquet file, digesting as it goes.

    Payload columns are never requested — the Range reader fetches the footer plus the `id` column
    chunks and nothing else. Ids are digested to `uint64` per row group and the strings dropped
    immediately; keeping them would cost 12x the memory for information nothing downstream reads.
    """
    import numpy as np
    import pyarrow.parquet as pq

    res = FileResult(path=entry["path"])
    try:
        # Block here, before opening a new file, if any worker has recently been 429'd. The limit
        # is per-IP so it belongs to the whole process, not to this thread.
        _RATE_GATE.wait()
        rf = _RangeFile(_resolve_url(repo, entry["path"]), entry["size"], headers)
        pf = pq.ParquetFile(rf)
        md = pf.metadata
        _leaf_index(md, id_column)  # fail loudly if the schema moved
        chunks: list = []
        for rg in range(md.num_row_groups):
            tbl = pf.read_row_group(rg, columns=[id_column])
            ids = tbl.column(id_column).to_pylist()
            chunks.append(np.fromiter((IdSet._digest64(i) for i in ids), dtype=np.uint64, count=len(ids)))
            if len(res.sample_ids) < sample_per_file:
                res.sample_ids.extend(ids[: sample_per_file - len(res.sample_ids)])
            del ids, tbl
        res.digests = np.concatenate(chunks) if chunks else np.empty(0, dtype=np.uint64)
        res.rows_read = md.num_rows
        res.rows_kept = int(res.digests.shape[0])
        res.bytes_fetched = rf.bytes_fetched
        res.n_429 = rf.n_429
    except Exception as exc:  # noqa: BLE001 - recorded per file, surfaced in the run index
        res.error = f"{type(exc).__name__}: {exc}"[:300]
    return res


def _partition_report(config: str, ids: list[str]) -> dict:
    """Recompute what the partition does to THIS config's real ids, per the golden rule.

    Reports `keep_fraction` against the config's requirement rather than only the global balance,
    because the design's margin is per-format: `table` needs 17.3% and the others ~10%.
    """
    kept = [i for i in ids if keeps_id(config, i)]
    audit = audit_partition(ids)
    return {
        "config": config,
        "ids_sampled": len(ids),
        "ids_distinct": audit.n_ids,
        "kept_by_this_format": len(kept),
        "keep_fraction_pct": round(100.0 * len(kept) / len(ids), 4) if ids else 0.0,
        "global_partition_audit": audit.to_dict(),
    }


# --------------------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------------------

#: Per-format token requirement from §3.3 / §9.7 item 4: 15 B each against the Phase 0c measured
#: totals. The fraction each format must retain is what the partition has to clear.
_REQUIRED_FRACTION_PCT = {"faq": 10.1, "tutorial": 10.1, "math": 15.8, "table": 17.3}


def _shard_slice(items: list, shard: int, of: int) -> list:
    """`items` interleaved by shard, NOT cut into contiguous blocks.

    Contiguous blocks would be the obvious choice and are wrong here: FinePhrase's files are
    ordered by name, sizes vary by an order of magnitude, and the big ones cluster. A contiguous
    split hands one child a slice of mostly-large files, so the array's wall clock is set by the
    unluckiest child while others idle. Striding (`items[shard::of]`) spreads any size ordering
    evenly across children by construction.

    Every item lands in exactly one shard, so the union across `of` shards is the whole list —
    asserted in the tests, because an off-by-one here silently drops files from the anti-join set
    and a smaller-than-expected id set does not look like an error.
    """
    if not 0 <= shard < of:
        raise IngestError(f"shard {shard} out of range for --of {of}")
    return items[shard::of]


def _cmd_plan(args) -> int:
    """Metadata only: enumerate upstream files, measure the partition on a real id sample, and
    print what a full run would do. Safe to run locally — it moves footers, not payload."""
    _require_batch(allow_local=True)
    headers = _hf_headers()
    out: dict = {"repo": FINEPHRASE_REPO, "rewrite_leaf": REWRITE_LEAF, "configs": []}
    all_ids: list[str] = []
    for config in FINEPHRASE_FORMATS:
        tree = sorted(hf_tree(FINEPHRASE_REPO, config, headers=headers), key=lambda e: e["path"])
        total_bytes = sum(e["size"] for e in tree)
        sample = tree[: args.sample_files]
        ids: list[str] = []
        fetched = 0
        n_rows = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for res in pool.map(lambda e: _scan_ids(FINEPHRASE_REPO, e, headers), sample):
                if res.error:
                    print(f"  ! {res.path}: {res.error}", file=sys.stderr)
                    continue
                ids += res.sample_ids
                fetched += res.bytes_fetched
                n_rows += res.rows_read
        all_ids += ids
        rec = {
            "config": config,
            "n_files": len(tree),
            "parquet_bytes": total_bytes,
            "files_sampled": len(sample),
            "rows_in_sampled_files": n_rows,
            "bytes_fetched": fetched,
            "required_keep_fraction_pct": _REQUIRED_FRACTION_PCT.get(config),
            **_partition_report(config, ids),
        }
        out["configs"].append(rec)
        print(
            f"{config:9s} files={len(tree):5,} parquet={total_bytes/1e9:7.1f} GB  "
            f"sampled={len(sample)}  ids={len(ids):7,}  "
            f"keeps={rec['keep_fraction_pct']:.2f}% (needs {rec['required_keep_fraction_pct']}%)",
            flush=True,
        )
    if all_ids:
        combined = audit_partition(all_ids)
        out["combined_partition_audit"] = combined.to_dict()
        print(
            f"\ncombined: {combined.n_ids:,} distinct ids, worst deviation "
            f"{combined.worst_deviation_pp:.3f}pp, min share {combined.min_share_pct:.3f}%"
        )
        for config in FINEPHRASE_FORMATS:
            need = _REQUIRED_FRACTION_PCT[config]
            if combined.min_share_pct < need:
                print(
                    f"  ✗ {config}: partition floor {combined.min_share_pct:.2f}% < required {need}%",
                    file=sys.stderr,
                )
                out["verdict"] = "INSUFFICIENT"
        out.setdefault("verdict", "OK")
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(out, fh, indent=1)
        print(f"\nwrote {args.out}")
    return 0 if out.get("verdict") == "OK" else 1


def _cmd_ids(args) -> int:
    """Scan FinePhrase id columns and upload the anti-join set (or this shard's part of it).

    Unsharded (`--of 1`, the default) writes `<prefix>/_ids/finephrase-<config>.u64` and
    `_ids/_index.json`, which is what a small run wants.

    Sharded (`--of N`) writes `<prefix>/_ids/parts/finephrase-<config>.<shard>-of-<N>.u64` and a
    per-shard `_index.<shard>-of-<N>.json`, then `merge` combines them. Never `manifest.json`.
    """
    _require_batch(allow_local=args.allow_local)
    import boto3

    s3 = boto3.client("s3")
    prefix = args.prefix.strip("/")
    if args.require_lifecycle:
        _assert_lifecycle_covers(s3, args.bucket, prefix + "/")
    headers = _hf_headers()
    sharded = args.of > 1
    index: dict = {
        "run_id": args.run_id,
        "repo": FINEPHRASE_REPO,
        "shard": args.shard,
        "of": args.of,
        "configs": {},
    }
    for config in FINEPHRASE_FORMATS:
        tree = sorted(hf_tree(FINEPHRASE_REPO, config, headers=headers), key=lambda e: e["path"])
        if args.limit_files:
            tree = tree[: args.limit_files]
        n_all = len(tree)
        tree = _shard_slice(tree, args.shard, args.of)
        if sharded:
            print(
                f"{config}: shard {args.shard}/{args.of} -> {len(tree):,} of {n_all:,} files",
                flush=True,
            )
        # Digest chunks, NOT id strings: 2.71 GB vs 32.5 GB per config. `audit_ids` keeps a bounded
        # sample of the strings so the partition can still be audited on real ids.
        chunks: list = []
        audit_ids: list[str] = []
        errors: list[str] = []
        fetched = 0
        rows = 0
        done = 0
        n_429 = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for res in pool.map(lambda e: _scan_ids(FINEPHRASE_REPO, e, headers), tree):
                done += 1
                n_429 += res.n_429
                if res.error:
                    errors.append(f"{res.path}: {res.error}")
                    continue
                chunks.append(res.digests)
                rows += res.rows_kept
                fetched += res.bytes_fetched
                if len(audit_ids) < args.audit_sample:
                    audit_ids.extend(res.sample_ids[: args.audit_sample - len(audit_ids)])
                if done % 250 == 0:
                    print(
                        f"  {config}: {done}/{len(tree)} files, {rows:,} ids, {n_429} 429s",
                        flush=True,
                    )
        if errors and not args.tolerate_errors:
            raise IngestError(
                f"{len(errors)} of {len(tree)} {config} files failed and --tolerate-errors was "
                f"not set. An incomplete id set makes the anti-join silently incomplete, which is "
                f"worse than a failed job. {n_429} requests were rate-limited — if that number is "
                f"large, LOWER --workers rather than raising it; the HF limit is per-IP, so more "
                f"parallelism makes this worse. First error: {errors[0]}"
            )
        id_set = IdSet.from_digest_chunks(chunks)
        del chunks
        if sharded:
            key = _assert_safe_key(
                f"{prefix}/_ids/parts/finephrase-{config}.{args.shard:05d}-of-{args.of:05d}.u64"
            )
        else:
            key = _assert_safe_key(f"{prefix}/_ids/finephrase-{config}.u64")
        s3.put_object(Bucket=args.bucket, Key=key, Body=id_set.to_bytes())
        index["configs"][config] = {
            "n_files_in_config": n_all,
            "n_files_this_shard": len(tree),
            "ids_read": rows,
            "ids_distinct": len(id_set),
            "duplicate_ids_within_shard": rows - len(id_set),
            "bytes_fetched": fetched,
            "n_429": n_429,
            "key": key,
            "errors": errors[:10],
            "n_errors": len(errors),
            **_partition_report(config, audit_ids),
        }
        print(f"{config}: {len(id_set):,} distinct ids -> s3://{args.bucket}/{key}", flush=True)
    suffix = f".{args.shard:05d}-of-{args.of:05d}" if sharded else ""
    ikey = _assert_safe_key(f"{prefix}/_ids/_index{suffix}.json")
    s3.put_object(
        Bucket=args.bucket,
        Key=ikey,
        Body=json.dumps(index, indent=1).encode(),
        ContentType="application/json",
    )
    print(f"wrote s3://{args.bucket}/{ikey}")
    if _RATE_GATE.total_penalties:
        print(
            f"NOTE: {_RATE_GATE.total_penalties} rate-limit pauses. The HF limit is per-IP, so if "
            f"this run was slow, use FEWER concurrent shards — not more.",
            flush=True,
        )
    return 0


def _cmd_merge(args) -> int:
    """Combine shard parts into the per-config id sets, refusing to merge an incomplete set.

    The refusal is the point. A missing part yields a *smaller* anti-join set, which is not an
    error anyone would notice: the ingest succeeds, the counts look plausible, and edu-web silently
    keeps documents that should have been removed. So this verifies that every expected
    `<shard>-of-<N>` part is present before it writes anything.
    """
    _require_batch(allow_local=args.allow_local)
    import boto3
    import numpy as np

    s3 = boto3.client("s3")
    prefix = args.prefix.strip("/")
    parts_prefix = f"{prefix}/_ids/parts/"
    listed: list[str] = []
    token: str | None = None
    while True:
        kw = {"Bucket": args.bucket, "Prefix": parts_prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        listed += [o["Key"] for o in page.get("Contents", [])]
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated"):
            break

    summary: dict = {"run_id": args.run_id, "of": args.of, "configs": {}}
    for config in FINEPHRASE_FORMATS:
        expected = [
            f"{parts_prefix}finephrase-{config}.{i:05d}-of-{args.of:05d}.u64" for i in range(args.of)
        ]
        missing = [k for k in expected if k not in listed]
        if missing:
            raise IngestError(
                f"{config}: {len(missing)} of {args.of} shard parts are missing, e.g. "
                f"{missing[0]}. Merging now would produce a SMALLER anti-join set that looks "
                f"entirely healthy — re-run the failed array children first. "
                f"(Array child i writes shard i; check the job's failed indices.)"
            )
        arrays = []
        total_rows = 0
        for key in expected:
            raw = s3.get_object(Bucket=args.bucket, Key=key)["Body"].read()
            part = IdSet.from_bytes(raw)
            total_rows += len(part)
            arrays.append(part.values)
        merged = IdSet(values=np.unique(np.concatenate(arrays))) if arrays else IdSet.from_ids([])
        del arrays
        out_key = _assert_safe_key(f"{prefix}/_ids/finephrase-{config}.u64")
        s3.put_object(Bucket=args.bucket, Key=out_key, Body=merged.to_bytes())
        summary["configs"][config] = {
            "parts": args.of,
            "ids_across_parts": total_rows,
            "ids_distinct": len(merged),
            "cross_shard_duplicates": total_rows - len(merged),
            "key": out_key,
        }
        print(
            f"{config}: {args.of} parts, {total_rows:,} ids -> {len(merged):,} distinct "
            f"-> s3://{args.bucket}/{out_key}",
            flush=True,
        )
    skey = _assert_safe_key(f"{prefix}/_ids/_merge-summary.json")
    s3.put_object(
        Bucket=args.bucket,
        Key=skey,
        Body=json.dumps(summary, indent=1).encode(),
        ContentType="application/json",
    )
    print(f"wrote s3://{args.bucket}/{skey}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="edullm-ingest-reservoir",
        description="Ingest FinePhrase / FineWeb-Edu for pretrain/reservoir-dolma2, applying the "
        "§9.7 item 4 id partition at read time.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="metadata-only dry run: enumerate files, audit the partition")
    p.add_argument("--sample-files", type=int, default=2, help="files per config to read ids from")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--out", default=None)
    p.set_defaults(func=_cmd_plan)

    p = sub.add_parser("ids", help="scan all FinePhrase ids and upload the anti-join set")
    p.add_argument("--bucket", default="edullm-landing")
    p.add_argument("--prefix", default="_ingest/reservoir-dolma2")
    p.add_argument("--run-id", required=True)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument(
        "--shard",
        type=int,
        default=0,
        help="this shard's index; set from AWS_BATCH_JOB_ARRAY_INDEX in an array job",
    )
    p.add_argument(
        "--of",
        type=int,
        default=1,
        help="total shards. >1 writes _ids/parts/ and requires a later `merge`",
    )
    p.add_argument("--limit-files", type=int, default=0, help="0 = all")
    p.add_argument("--audit-sample", type=int, default=200_000)
    p.add_argument("--tolerate-errors", action="store_true")
    p.add_argument("--allow-local", action="store_true", help="metadata-scale testing only")
    p.add_argument(
        "--no-require-lifecycle",
        dest="require_lifecycle",
        action="store_false",
        help="accept an unexpiring destination prefix (see module docstring landmine 2)",
    )
    p.set_defaults(func=_cmd_ids, require_lifecycle=True)

    p = sub.add_parser("merge", help="combine shard parts written by `ids --of N`")
    p.add_argument("--bucket", default="edullm-landing")
    p.add_argument("--prefix", default="_ingest/reservoir-dolma2")
    p.add_argument("--run-id", required=True)
    p.add_argument("--of", type=int, required=True, help="the same --of the shards used")
    p.add_argument("--allow-local", action="store_true")
    p.set_defaults(func=_cmd_merge)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (IngestError, IdPartitionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
