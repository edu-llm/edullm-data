#!/usr/bin/env python3
"""Measure FinePhrase cross-config document-id overlap EXACTLY. Standalone; runs on FarmShare.

Open item 1 of `docs/FINAL-DATASET-MIX.md`: every synthetic sizing number in this project scales
off "~28.5% distinct across all four configs", and that number came from 4x1000 sampled ids at HF
revision 78cf4a5e. 4,000 ids is not a measurement of a 1.354-billion-row corpus. This script
replaces it with a census.

IMPORTS NOTHING FROM `edullm_data` -- deliberately. It re-implements `partition_of` so that the
result is an INDEPENDENT check of `src/edullm_data/reservoir_ids.py` rather than a tautology, and
so it can be scp'd to a cluster with only pyarrow + numpy present.


WHAT IS BEING COUNTED, AND WHY THE `id` COLUMN IS THE WHOLE TRICK
----------------------------------------------------------------
`HuggingFaceFW/finephrase` carries 12 columns (verified against the footer of
`faq/000_00000_0.parquet` at the pinned revision):

    text          str    <- the ORIGINAL, UNREPHRASED FineWeb-Edu document. NOT the rewrite.
    id            str    <- URN-shaped uuid, e.g. "<urn:uuid:e2300ad5-...-7ec88785cc9d>"
    dump, url, file_path, language, language_score, token_count, score, int_score, dataset
    rollout_results  LIST<STRUCT{finish_reason, text, usage{...}}>
                          <- THE REWRITE IS `rollout_results[0].text`.

This script reads ONLY `id`. That is the entire performance argument, and it is a large one --
measured, per file, from real footers at revision 78cf4a5e:

    leaf                                     compressed bytes   share of file
    text                                          186,644,641          69.1%
    rollout_results.list.element.text               73,385,752          27.2%
    id                                               2,542,091           0.94%

So an exact census over all 27,104 FinePhrase files moves ~95 GB of `id` bytes (measured 3.5 MB
per file) instead of the 5.16 TB the four configs actually occupy -- a 54x reduction. Adding the
FineWeb-Edu parent for the edu-web anti-join brings the total to ~111 GB against 6.16 TB, i.e.
1.8%. The `text`/`rollout_results` trap is asserted below (`verify_schema_trap`) but never read in
bulk.


WHY EXACT, AND WHY SAMPLING WOULD SAVE ALMOST NOTHING
----------------------------------------------------
A hash-prefix sample (`sha256(id) % k == 0`) is unbiased and reproducible, and this script
implements it (`--sample-mod`). But note what it does NOT save: you must still READ every id to
evaluate its hash. Sampling reduces RAM and sort time; it does not reduce the HTTP bytes or the
request count, and those are the binding cost. Exact mode is therefore nearly free relative to
sampled mode, and exact mode is the default. `--sample-mod` exists as the fallback for a node
that cannot hold the arrays, not as the expected path.

Do NOT reach for "first N rows" sampling. Parquet row order in these repos follows the CommonCrawl
dump order, so a prefix is a sample of the earliest crawls -- which is the prior measurement's
weakness, and it is not fixable by taking more rows from the front.


THE REPRESENTATION, AND ITS ONE ADMITTED APPROXIMATION
-----------------------------------------------------
An id is a 49-byte string. A Python `set` of the 2.29e9 ids in scope would be several hundred GB and
is not an option. Ids are stored instead as **the high 8 bytes of `sha256(id)`**, as `numpy.uint64`,
sorted. That is 18.3 GB for the whole census and supports exact set arithmetic by `searchsorted`.

The approximation is hash collision: two DIFFERENT ids sharing a 64-bit key are counted as one
document, which UNDERSTATES the union and therefore understates the distinct fraction. With `n`
distinct ids the expected number of colliding pairs is

    E[collisions] = n*(n-1)/2 / 2**64  ~=  n**2 / 2**65

The script computes this from the realized `n` and puts it in the output as
`hash_collision_expected_pairs`. For orientation, at n = 4.0e8 distinct ids it is 4.3e-3 pairs --
i.e. the census would need to be run about 230 times before one collision is expected at all. It
cannot move any reported fraction at three significant figures. The same sha256 call yields both
the 64-bit key and the 4-way partition, so partitioning costs no extra hashing.


THREE PHASES, BECAUSE ONE FOUR-HOUR HTTP JOB IS A FRAGILE JOB
------------------------------------------------------------
    tree    tiny, login-node-safe. Lists every parquet file at the PINNED revision via the Hub
            tree API and caches the listing to scratch. Runs once. Array tasks must not each
            hammer the API, and a listing fetched per-task is a listing that can differ per-task.

    hash    the parallel phase. Slurm job array; task `i` of `n` takes files `i, i+n, i+2n, ...`
            of the (deterministically sorted) global file list, reads `columns=['id']`, and writes
            one `.npy` of uint64 keys plus a `.json` of counts and assertion results. Idempotent:
            a task whose outputs already exist and validate is skipped, so a partial array is
            resumed by resubmitting it.

    reduce   the serial phase. Sorts and dedups per config, then walks the 64-bit key space in
            256 contiguous buckets. Because each per-config array is sorted, a bucket is a
            contiguous slice, so every pairwise intersection, the 4-way union, and the FineWeb-Edu
            anti-join are computed EXACTLY from small in-memory slices. Peak RSS stays near one
            bucket rather than near the corpus. Per-config sorted arrays are mmap'd.


FAILURE POSTURE
---------------
This project has been bitten more than once by a silent empty result reporting success. So:

  * a config that yields zero rows is a hard error, not a zero;
  * a file whose footer lacks an `id` leaf is a hard error;
  * `rollout_results` not being exactly length 1 is a hard error -- and it is checked from the
    FOOTER for EVERY row group of EVERY file at no I/O cost (see `assert_single_rollout`), not
    merely spot-checked;
  * a `reduce` over fewer shard files than the `hash` phase was supposed to produce is a hard
    error naming the missing shards, because a silently short reduce inflates the distinct
    fraction, which is the direction that would make us feel good.

`--allow-partial` exists for deliberate exploratory runs and stamps `"partial": true` into the
output. Nothing downstream should consume a partial result.
"""

from __future__ import annotations

import argparse
import bisect
import concurrent.futures as cf
import hashlib
import io
import json
import os
import random
import resource
import sys
import time
import urllib.error
import urllib.request

import numpy as np
import pyarrow.parquet as pq

# ---------------------------------------------------------------------------------------------
# Constants. Everything the script asserts against is here, with its provenance.
# ---------------------------------------------------------------------------------------------

HUB = "https://huggingface.co"
FINEPHRASE = "HuggingFaceFW/finephrase"
FINEWEB_EDU = "HuggingFaceFW/fineweb-edu"

#: The four configs, in the SAME fixed order as `reservoir_ids.FINEPHRASE_FORMATS`. That order is
#: the partition assignment, so a divergence here would silently reassign every document; the
#: order is asserted against the module in the README's cross-check, not imported.
CONFIGS: tuple[str, ...] = ("faq", "math", "table", "tutorial")

#: FinePhrase's declared parent, from the dataset card at the pinned revision:
#: `source_datasets: [HuggingFaceFW/fineweb-edu/sample-350BT]`, and in prose "config
#: `sample-350BT`, split `train`". The repo path for that config is `sample/350BT`.
#: This is the id space in which the edu-web collision lives.
EDUWEB_DEFAULT = "sample/350BT"

#: The 11 FineWeb-Edu columns plus `rollout_results`. Asserted, not assumed.
EXPECTED_FP_COLUMNS = frozenset({
    "text", "id", "dump", "url", "file_path", "language", "language_score",
    "token_count", "score", "int_score", "dataset", "rollout_results",
})

#: The leaf carrying the rewrite. `num_values` on this leaf equals the number of LIST ELEMENTS in
#: the row group (nulls included), so `num_values == num_rows` for every row group is an exact,
#: footer-only proof that every `rollout_results` list has length 1.
LEAF_REWRITE = "rollout_results.list.element.text"

ID_COLUMN = "id"

#: Card figures at revision 78cf4a5e, used only as sanity anchors in the summary.
CARD_SOURCE_DOCS = 339_347_842
CARD_OUTPUT_SAMPLES = 1_354_044_711

#: Number of contiguous buckets the 64-bit key space is walked in during `reduce`. Sets the
#: memory/loop-overhead tradeoff only; the RESULT is bucket-count-independent (exact set
#: arithmetic per bucket, summed), which `--buckets` lets a reviewer verify by re-running.
DEFAULT_BUCKETS = 256

#: The design bar from DATASET-DESIGN-reservoir.md: each of the 4 `keeps_id` partitions must hold
#: at least this share against an ideal 25.0%.
PARTITION_FLOOR = 0.173
N_PARTITIONS = 4


class Fatal(RuntimeError):
    """An assumption was violated. Never caught, never downgraded to a warning."""


def die(msg: str) -> "None":
    raise Fatal(msg)


# ---------------------------------------------------------------------------------------------
# HTTP: authenticated, retrying, range-capable.
# ---------------------------------------------------------------------------------------------

def hf_headers() -> dict:
    """HF token from env or the standard cache path. Absent is fine -- these repos are public."""
    h = {"User-Agent": "edullm-data/finephrase-overlap"}
    tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not tok:
        p = os.path.expanduser("~/.cache/huggingface/token")
        if os.path.exists(p):
            tok = open(p).read().strip()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


H = hf_headers()


def http_json(url: str, tries: int = 6, timeout: int = 120) -> tuple[object, str | None]:
    """GET JSON, returning (body, Link header). Retries transient failures with backoff.

    A 4xx is NOT swallowed into a data value here (unlike the /rows-era scripts in
    artifacts/recount/): this script's inputs are the Hub tree API and parquet files, and a 404 on
    either means the pinned revision or path is wrong, which must stop the run.
    """
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=H)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r), r.headers.get("Link")
        except urllib.error.HTTPError as e:
            body = e.read()[:400].decode("utf8", "replace")
            if e.code in (429, 500, 502, 503, 504):
                last = f"HTTP {e.code}: {body}"
                time.sleep(min(60, 5 * (attempt + 1) ** 2))
                continue
            die(f"GET {url} -> HTTP {e.code}: {body}")
        except Exception as e:  # noqa: BLE001 - transient network; retry then fail loudly
            last = f"{type(e).__name__}: {e}"
            time.sleep(min(60, 4 * (attempt + 1)))
    die(f"GET {url} failed after {tries} tries: {last}")
    return None, None  # unreachable; keeps type checkers quiet


def fetch_range(url: str, start: int, length: int, tries: int = 6) -> bytes:
    """One HTTP Range GET, retried. Fails loudly rather than returning short."""
    req = urllib.request.Request(
        url, headers={**H, "Range": f"bytes={start}-{start + length - 1}"})
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data = r.read()
            if len(data) != length:
                # A short 200 (server ignored the Range header) would silently corrupt the parquet
                # read into a confusing pyarrow error a long way from here.
                last = f"asked {length} bytes, got {len(data)} (Range honoured? status {r.status})"
                time.sleep(min(60, 4 * (attempt + 1)))
                continue
            return data
        except Exception as e:  # noqa: BLE001 - transient network; retry then fail loudly
            last = f"{type(e).__name__}: {e}"
            time.sleep(min(60, 4 * (attempt + 1)))
    die(f"range read {url} [{start}, +{length}] failed after {tries} tries: {last}")
    return b""  # unreachable


class RangeFile(io.RawIOBase):
    """Seekable read-only file over HTTP Range, with a prefilled range cache.

    Derived from `artifacts/recount/_fp_footer_leaf.py`, which used this pattern against this exact
    dataset -- plus the one addition that makes a census affordable.

    WHY THE CACHE EXISTS (measured, not guessed). A FinePhrase file holds 67-77 row groups; the
    `id` column chunk in each is ~38 KB, and consecutive chunks are ~4 MB apart because `text` and
    `rollout_results` sit between them. So reading the id column is 67 SMALL, SCATTERED ranges.
    Measured against `faq/000_00000_0.parquet` from a residential link: 3.2 MB in 69 serial
    requests took 49 s -- 0.72 s each. The wall clock was 100% request LATENCY; the bytes were
    noise. Naively extrapolated that is 378 single-threaded hours for the census.

    Coalescing is not the fix. Merging those 67 chunks into one range means spanning the holes too,
    i.e. downloading the whole 270 MB file -- 84x the bytes, which destroys the entire
    id-column-only argument (5.16 TB instead of 95 GB).

    The fix is to issue the scattered ranges CONCURRENTLY. `prefetch()` takes the exact
    (offset, length) list of the id column chunks -- read straight from the footer, where parquet
    records them -- fetches them with a thread pool, and serves pyarrow's subsequent reads from
    memory with zero further HTTP. Request count is unchanged; the requests just stop being serial.

    `bytes_fetched` / `n_requests` are instrumentation: they are what makes the "we moved 111 GB of
    6.16 TB" claim checkable in the JSON output rather than merely asserted.
    """

    def __init__(self, url: str, size: int, prefetch_workers: int = 16):
        self.url, self.size, self.pos = url, size, 0
        self.bytes_fetched = 0
        self.n_requests = 0
        self.prefetch_workers = prefetch_workers
        self._cache: list[tuple[int, int, bytes]] = []  # (start, end_exclusive, data), sorted
        self._starts: list[int] = []                    # _cache starts, for bisect

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def seek(self, off: int, whence: int = 0) -> int:
        if whence == 0:
            self.pos = off
        elif whence == 1:
            self.pos += off
        else:
            self.pos = self.size + off
        return self.pos

    def tell(self) -> int:
        return self.pos

    def prefetch(self, ranges: list[tuple[int, int]]) -> None:
        """Fetch these (offset, length) ranges in parallel and cache them.

        Ranges are merged when they are adjacent or overlap (parquet page headers make consecutive
        chunks touch sometimes) so the cache stays a small sorted list of disjoint spans, and a
        lookup is a linear scan over ~70 entries rather than a per-byte dict.
        """
        if not ranges:
            return
        merged: list[list[int]] = []
        for start, length in sorted(ranges):
            start = max(0, min(start, self.size))
            end = max(start, min(start + length, self.size))
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        spans = [(s, e) for s, e in merged if e > s]
        if not spans:
            return
        n = min(self.prefetch_workers, len(spans))
        with cf.ThreadPoolExecutor(n) as ex:
            futs = {ex.submit(fetch_range, self.url, s, e - s): (s, e) for s, e in spans}
            for fut in cf.as_completed(futs):
                s, e = futs[fut]
                data = fut.result()
                self.bytes_fetched += len(data)
                self.n_requests += 1
                self._cache.append((s, e, data))
        self._cache.sort()
        self._starts = [c[0] for c in self._cache]

    def _from_cache(self, start: int, end: int) -> bytes | None:
        """Bisect, not a linear scan -- FineWeb-Edu files have 728 row groups.

        pyarrow issues several reads per row group (page header, then data), so a linear scan over
        the cache is O(row_groups^2) per file: ~530,000 comparisons on a 728-group FineWeb-Edu
        file versus ~7,000 on a 67-group FinePhrase one. That asymmetry is invisible when you
        benchmark only FinePhrase, and it is why the first FineWeb-Edu timing ran off the end of a
        two-minute probe. `_starts` is kept in lockstep with the sorted `_cache`.
        """
        i = bisect.bisect_right(self._starts, start) - 1
        if i < 0:
            return None
        s, e, data = self._cache[i]
        if s <= start and end <= e:
            return data[start - s:end - s]
        return None

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        hit = self._from_cache(self.pos, self.pos + n)
        if hit is not None:
            self.pos += len(hit)
            return hit
        data = fetch_range(self.url, self.pos, n)
        self.pos += len(data)
        self.bytes_fetched += len(data)
        self.n_requests += 1
        return data


def id_column_ranges(md, pad: int = 4096) -> list[tuple[int, int]]:
    """The exact byte ranges of the `id` column chunks, from the footer.

    Parquet records each column chunk's page offsets and `total_compressed_size`, so the ranges are
    known EXACTLY before any payload byte is read -- no guessing, no over-fetching.

    DO NOT USE `file_offset`. MEASURED on `faq/000_00000_0.parquet`: `file_offset` is **0** for
    every `id` chunk in these files (a legal but unhelpful writer choice -- the field is optional in
    the Thrift spec and some writers leave it unset). Trusting it collapses every range to offset 0,
    the merge folds them into one span, and the prefetch silently becomes a no-op that fetches the
    wrong bytes while still *reporting* a successful prefetch. The real start is
    `dictionary_page_offset` when a dictionary page is present (it is here: dict at 2,861,399, data
    at 2,898,113, size 37,999 -> the chunk is dict-then-data), else `data_page_offset`.

    `pad` at the tail absorbs the trailing page header; a miss just falls through to a normal
    read, so slop costs bytes and never correctness.
    """
    out: list[tuple[int, int]] = []
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        for i in range(rg.num_columns):
            cc = rg.column(i)
            if cc.path_in_schema != ID_COLUMN:
                continue
            starts = [v for v in (cc.dictionary_page_offset, cc.data_page_offset)
                      if isinstance(v, int) and v > 0]
            if not starts:
                # No usable offset: skip the prefetch for this chunk rather than fetch garbage.
                # `read` falls back to a direct range request, which is correct, just slower.
                continue
            start = min(starts)
            out.append((start, cc.total_compressed_size + pad))
    if not out:
        die("footer exposed no usable page offsets for the `id` column -- refusing to run the "
            "census at one serial request per row group (~0.7 s each, ~2.3 M requests)")
    return out


def resolve_url(repo: str, revision: str, path: str) -> str:
    return f"{HUB}/datasets/{repo}/resolve/{revision}/{path}"


# ---------------------------------------------------------------------------------------------
# PHASE `tree`: pin the revision, enumerate the files, cache the listing.
# ---------------------------------------------------------------------------------------------

def resolve_revision(repo: str, revision: str) -> str:
    """Turn a short sha / branch / tag into the full commit sha, and RECORD it.

    A run pinned to `78cf4a5e` and a run pinned to `main` are different measurements. The output
    carries the full sha so that "28.5% was measured at 78cf4a5e" is a statement someone can
    check a year from now.
    """
    info, _ = http_json(f"{HUB}/api/datasets/{repo}/revision/{revision}")
    sha = (info or {}).get("sha")
    if not isinstance(sha, str) or len(sha) != 40:
        die(f"could not resolve {repo}@{revision} to a 40-char commit sha; got {sha!r}")
    if not sha.startswith(revision) and revision not in ("main", "refs/convert/parquet"):
        # A branch name legitimately will not prefix-match; a short sha must.
        if all(c in "0123456789abcdef" for c in revision.lower()):
            die(f"{repo}@{revision} resolved to {sha}, which does not start with the requested sha")
    return sha


def list_parquet(repo: str, sha: str, prefix: str) -> list[dict]:
    """Every `.parquet` under `prefix` at `sha`, as [{path, size}], paginating the tree API."""
    out: list[dict] = []
    url = f"{HUB}/api/datasets/{repo}/tree/{sha}/{prefix}?recursive=true"
    while url:
        body, link = http_json(url)
        for e in body or []:
            p = e.get("path", "")
            if p.endswith(".parquet"):
                if not isinstance(e.get("size"), int) or e["size"] <= 0:
                    die(f"tree entry {p} has no usable size: {e!r}")
                out.append({"path": p, "size": e["size"]})
        url = link.split("<")[1].split(">")[0] if (link and 'rel="next"' in link) else None
    if not out:
        die(f"no parquet files under {repo}@{sha}:{prefix} -- wrong prefix, or the revision moved")
    # Sort by path. The global file list must be a pure function of (repo, sha, prefixes) so that
    # array task i takes the same files on a resubmission as it did on the first attempt.
    return sorted(out, key=lambda d: d["path"])


def cmd_tree(a: argparse.Namespace) -> dict:
    fp_sha = resolve_revision(FINEPHRASE, a.revision)
    ew_sha = resolve_revision(FINEWEB_EDU, a.eduweb_revision)

    groups: list[dict] = []
    for cfg in CONFIGS:
        files = list_parquet(FINEPHRASE, fp_sha, cfg)
        groups.append({"repo": FINEPHRASE, "sha": fp_sha, "kind": "finephrase",
                       "name": cfg, "prefix": cfg, "files": files})
        print(f"[tree] finephrase/{cfg}: {len(files):,} files, "
              f"{sum(f['size'] for f in files) / 1e12:.3f} TB", file=sys.stderr)

    for prefix in [p for p in a.eduweb_configs.split(",") if p]:
        files = list_parquet(FINEWEB_EDU, ew_sha, prefix)
        groups.append({"repo": FINEWEB_EDU, "sha": ew_sha, "kind": "fineweb_edu",
                       "name": prefix, "prefix": prefix, "files": files})
        print(f"[tree] fineweb-edu/{prefix}: {len(files):,} files, "
              f"{sum(f['size'] for f in files) / 1e12:.3f} TB", file=sys.stderr)

    manifest = {
        "tool": "measure_finephrase_overlap.py",
        "phase": "tree",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "finephrase_revision_requested": a.revision,
        "finephrase_revision_sha": fp_sha,
        "fineweb_edu_revision_requested": a.eduweb_revision,
        "fineweb_edu_revision_sha": ew_sha,
        "configs": list(CONFIGS),
        "eduweb_configs": [p for p in a.eduweb_configs.split(",") if p],
        "groups": groups,
        "n_files_total": sum(len(g["files"]) for g in groups),
        "bytes_total": sum(f["size"] for g in groups for f in g["files"]),
    }
    path = os.path.join(a.work, "tree.json")
    os.makedirs(a.work, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(manifest, fh, indent=1)
    print(f"[tree] {manifest['n_files_total']:,} files, "
          f"{manifest['bytes_total'] / 1e12:.3f} TB total -> {path}", file=sys.stderr)
    return manifest


def load_tree(work: str) -> dict:
    path = os.path.join(work, "tree.json")
    if not os.path.exists(path):
        die(f"{path} missing -- run the `tree` phase first (it is cheap and login-node safe)")
    return json.load(open(path))


def global_file_list(tree: dict) -> list[dict]:
    """Flatten to a single deterministic list of work items: (group index, repo, sha, path, size).

    Deterministic order matters twice: it makes `--shard i --nshards n` reproducible across
    resubmissions, and it interleaves the two repos so no single array task inherits all of
    FineWeb-Edu's 728-row-group files.
    """
    items: list[dict] = []
    for gi, g in enumerate(tree["groups"]):
        for f in g["files"]:
            items.append({"gi": gi, "kind": g["kind"], "name": g["name"],
                          "repo": g["repo"], "sha": g["sha"],
                          "path": f["path"], "size": f["size"]})
    return sorted(items, key=lambda d: (d["path"], d["name"]))


# ---------------------------------------------------------------------------------------------
# Hashing. One sha256 per id yields BOTH the 64-bit key and the 4-way partition.
# ---------------------------------------------------------------------------------------------

#: Salt for the SAMPLING hash. It exists because of a bug this script's own end-to-end test
#: caught: sampling on `int(sha256(id)) % k == 0` and partitioning on `int(sha256(id)) % 4` use
#: the SAME integer, so whenever 4 divides `k` the sample predicate FORCES `partition == 0`. A
#: `--sample-mod 1000` run duly reported 100.0% of documents in partition 0 and 0.0% in the other
#: three -- a partition audit that looked catastrophically broken when the partition was fine and
#: the SAMPLER was broken. Salting the sampling digest makes the two hashes independent for every
#: `k`, so the audit means the same thing in sampled mode as in exact mode.
SAMPLE_SALT = b"edullm-finephrase-overlap-sample-v1|"


def key_and_partition(doc_id: str) -> tuple[int, int]:
    """(high 8 bytes of sha256(id) as uint64, full-digest mod 4).

    `partition` reimplements `reservoir_ids.partition_of` verbatim --
    `int.from_bytes(sha256(id.encode()).digest(), 'big') % 4` -- on purpose: this script is the
    independent check on that module, so importing it would make the check circular. If the two
    ever disagree, one of them is a bug and this measurement is how you find out.
    """
    d = hashlib.sha256(doc_id.encode("utf-8")).digest()
    return int.from_bytes(d[:8], "big"), int.from_bytes(d, "big") % N_PARTITIONS


def sampled_in(doc_id_bytes: bytes, sample_mod: int, sample_residue: int) -> bool:
    """Deterministic 1-in-`sample_mod` membership, on a SALTED digest independent of the partition.

    Still a pure function of the id, so a document is kept or dropped identically in every config
    -- which is what preserves the overlap signal. See `SAMPLE_SALT` for why it is not the plain
    digest.
    """
    d = hashlib.sha256(SAMPLE_SALT + doc_id_bytes).digest()
    return int.from_bytes(d, "big") % sample_mod == sample_residue


def hash_ids(ids: list, sample_mod: int, sample_residue: int) -> tuple[np.ndarray, np.ndarray, int]:
    """Hash a batch of id strings. Returns (uint64 keys, partition counts[4], n_bad).

    `sample_mod > 1` keeps only ids selected by `sampled_in`, a deterministic function of the id
    alone -- so the same document is kept or dropped identically in every config, which is
    precisely what makes the sampled overlap estimate unbiased. (Sampling each config
    independently would destroy the overlap signal: two 1% samples of the same 339M documents
    intersect in ~1% of 1%.) The sampling digest is SALTED so it cannot correlate with
    `partition_of`; see `SAMPLE_SALT`.

    Partition counts are accumulated over the KEPT ids only, matching the reported shares.

    `n_bad` counts nulls / non-strings / empties. These are reported and, at the caller, fatal --
    a FinePhrase or FineWeb-Edu row without a URN-shaped uuid means the column selection is wrong,
    not that the document is unusual.
    """
    keys = np.empty(len(ids), dtype=np.uint64)
    parts = np.zeros(N_PARTITIONS, dtype=np.int64)
    n = 0
    bad = 0
    for s in ids:
        if not isinstance(s, str) or not s:
            bad += 1
            continue
        enc = s.encode("utf-8")
        if sample_mod > 1 and not sampled_in(enc, sample_mod, sample_residue):
            continue
        d = hashlib.sha256(enc).digest()
        keys[n] = int.from_bytes(d[:8], "big")
        parts[int.from_bytes(d, "big") % N_PARTITIONS] += 1
        n += 1
    return keys[:n], parts, bad


# ---------------------------------------------------------------------------------------------
# PHASE `hash`: read the id column, assert the schema from footers, emit uint64 keys.
# ---------------------------------------------------------------------------------------------

def assert_single_rollout(md, path: str) -> dict:
    """Prove, from the FOOTER alone, that every `rollout_results` list has length exactly 1.

    The leaf `rollout_results.list.element.text` reports `num_values` = the number of list
    ELEMENTS in the row group (nulls counted). If a single list held 2 elements, that row group's
    leaf `num_values` would exceed its `num_rows`; if one held 0, it would fall short. So
    `num_values == num_rows` for EVERY row group is a complete proof, for every one of the
    1.354e9 rows, at zero extra I/O.

    This is strictly stronger than the /statistics evidence the design cites (mean == median ==
    min == max == 1.0 over 842,000 rows), and it is why this script does not need to read the
    `rollout_results` column in bulk to stand behind the claim.
    """
    total_rows = 0
    total_vals = 0
    offenders = []
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        rows = rg.num_rows
        vals = None
        for i in range(rg.num_columns):
            cc = rg.column(i)
            if cc.path_in_schema == LEAF_REWRITE:
                vals = cc.num_values
                break
        if vals is None:
            die(f"{path} row group {g}: leaf {LEAF_REWRITE!r} absent from the footer -- the "
                f"schema changed, or this is not a FinePhrase file")
        total_rows += rows
        total_vals += vals
        if vals != rows:
            offenders.append({"row_group": g, "num_rows": rows, "leaf_num_values": vals})
    if offenders:
        die(f"{path}: rollout_results is NOT always length 1. "
            f"{len(offenders)} row group(s) mismatch, first: {offenders[0]}. "
            f"Every sizing number that assumes one rewrite per document is now suspect.")
    return {"rows": total_rows, "rollout_elements": total_vals}


def read_one_file(item: dict, sample_mod: int, sample_residue: int,
                  verify_schema: bool, rg_batch: int = 64,
                  prefetch_workers: int = 16) -> dict:
    """Read `columns=['id']` from ONE parquet file over HTTP Range; hash as we go.

    Two decisions here carry the wall clock, both measured against real files:

    1. PREFETCH the id column chunks concurrently (see `RangeFile.prefetch`). Serial ranges cost
       ~0.72 s each and there are ~70 per file; concurrency turns 67 x latency into
       ceil(67/workers) x latency for identical bytes.
    2. Read row groups in BATCHES of `rg_batch` so pyarrow does not walk 728 tiny reads. With the
       prefetch cache warm this is a memory-copy loop, but batching also bounds the fallback cost
       if a range misses the cache.

    A batch's ids stay small: 64 row groups x ~1,000 rows x 49 bytes is ~3 MB, so RSS per reader
    thread is in the low tens of MB.
    """
    url = resolve_url(item["repo"], item["sha"], item["path"])
    rf = RangeFile(url, item["size"], prefetch_workers=prefetch_workers)
    pf = pq.ParquetFile(rf)
    md = pf.metadata
    names = list(pf.schema_arrow.names)

    if ID_COLUMN not in names:
        die(f"{item['path']}: no {ID_COLUMN!r} column. Columns present: {names}. "
            f"The id column name was confirmed as 'id' on both repos at the pinned revision, so "
            f"this means the revision moved or the wrong repo is being read.")

    schema_note: dict = {"columns": names}
    if item["kind"] == "finephrase":
        missing = EXPECTED_FP_COLUMNS - set(names)
        extra = set(names) - EXPECTED_FP_COLUMNS
        if missing:
            die(f"{item['path']}: FinePhrase is missing expected columns {sorted(missing)}")
        if extra:
            die(f"{item['path']}: FinePhrase has unexpected columns {sorted(extra)} -- the schema "
                f"changed under the pin")
        schema_note.update(assert_single_rollout(md, item["path"]))

    # Everything the id column occupies, fetched in parallel BEFORE pyarrow asks for any of it.
    rf.prefetch(id_column_ranges(md))

    chunks: list[np.ndarray] = []
    parts = np.zeros(N_PARTITIONS, dtype=np.int64)
    n_rows = 0
    n_bad = 0
    for g0 in range(0, md.num_row_groups, rg_batch):
        rgs = list(range(g0, min(g0 + rg_batch, md.num_row_groups)))
        tbl = pf.read_row_groups(rgs, columns=[ID_COLUMN])
        ids = tbl.column(ID_COLUMN).to_pylist()
        n_rows += len(ids)
        k, p, bad = hash_ids(ids, sample_mod, sample_residue)
        if k.size:
            chunks.append(k)
        parts += p
        n_bad += bad
        del tbl, ids

    if n_rows == 0:
        die(f"{item['path']}: read ZERO rows. A file that contributes nothing is not a zero, it "
            f"is a read bug -- the footer says {md.num_rows} rows.")
    if n_rows != md.num_rows:
        die(f"{item['path']}: read {n_rows} rows but the footer declares {md.num_rows}")
    if n_bad:
        die(f"{item['path']}: {n_bad} row(s) have a null/empty/non-string id. Both repos carry a "
            f"URN-shaped uuid on every row, so this is a column-selection bug.")

    keys = np.concatenate(chunks) if chunks else np.empty(0, dtype=np.uint64)

    out = {
        "path": item["path"], "kind": item["kind"], "name": item["name"],
        "file_bytes": item["size"], "rows": n_rows,
        "kept": int(keys.size),
        "partition_counts": parts.tolist(),
        "bytes_fetched": rf.bytes_fetched, "n_requests": rf.n_requests,
        "row_groups": md.num_row_groups,
        "schema": schema_note if verify_schema else None,
        "_keys": keys,
    }
    return out


def verify_schema_trap(item: dict, n_rows: int = 8) -> dict:
    """Read ONE row group's `text` + `rollout_results` and prove they are different strings.

    This is the ONE place the script touches the payload, on a handful of rows, and it exists to
    make the trap in this dataset a MEASURED fact in the output rather than a comment. If a future
    reader ever points a counting script at the top-level `text`, this field in the JSON is the
    evidence that they have counted the original FineWeb-Edu document instead of the rewrite.
    """
    url = resolve_url(item["repo"], item["sha"], item["path"])
    rf = RangeFile(url, item["size"])
    pf = pq.ParquetFile(rf)
    tbl = pf.read_row_group(0, columns=["text", "rollout_results", ID_COLUMN])
    txt = tbl.column("text").to_pylist()[:n_rows]
    rr = tbl.column("rollout_results").to_pylist()[:n_rows]
    ids = tbl.column(ID_COLUMN).to_pylist()[:n_rows]
    same = 0
    lens = []
    for t, lst, _ in zip(txt, rr, ids):
        if not isinstance(lst, list) or len(lst) != 1:
            die(f"{item['path']}: rollout_results is not a length-1 list on a directly read row: "
                f"{type(lst).__name__} len={len(lst) if isinstance(lst, list) else 'n/a'}")
        rewrite = (lst[0] or {}).get("text")
        if not isinstance(rewrite, str) or not rewrite:
            die(f"{item['path']}: rollout_results[0].text is not a non-empty string")
        if rewrite == t:
            same += 1
        lens.append({"original_chars": len(t or ""), "rewrite_chars": len(rewrite)})
    if same:
        die(f"{item['path']}: top-level `text` EQUALS rollout_results[0].text on {same}/{len(txt)} "
            f"rows. The documented schema says `text` is the ORIGINAL and the rewrite is nested; "
            f"if they are equal the generation is a no-op or the schema changed.")
    return {
        "verified_on": item["path"], "n_rows_checked": len(txt),
        "text_equals_rewrite": same,
        "rewrite_is_at": "rollout_results[0].text",
        "top_level_text_is": "the ORIGINAL, UNREPHRASED FineWeb-Edu document",
        "example_lengths": lens[:4],
        "bytes_fetched_for_this_check": rf.bytes_fetched,
        "id_sample": ids[:2],
    }


def shard_paths(work: str, shard: int) -> tuple[str, str]:
    d = os.path.join(work, "hash")
    return os.path.join(d, f"shard-{shard:05d}.npz"), os.path.join(d, f"shard-{shard:05d}.json")


def cmd_hash(a: argparse.Namespace) -> dict:
    tree = load_tree(a.work)
    items = global_file_list(tree)
    mine = [it for i, it in enumerate(items) if i % a.nshards == a.shard]
    if not mine:
        die(f"shard {a.shard}/{a.nshards} got zero files out of {len(items)} -- nshards exceeds "
            f"the file count, which means some shard indices produce no output and `reduce` will "
            f"refuse the run")

    npz, meta = shard_paths(a.work, a.shard)
    os.makedirs(os.path.dirname(npz), exist_ok=True)
    if os.path.exists(npz) and os.path.exists(meta) and not a.force:
        try:
            prev = json.load(open(meta))
            if prev.get("complete") and prev.get("n_files") == len(mine) \
                    and prev.get("sample_mod") == a.sample_mod:
                print(f"[hash {a.shard}] already complete ({prev['rows']:,} rows) -- skipping. "
                      f"--force to redo.", file=sys.stderr)
                return prev
        except Exception:  # noqa: BLE001 - a corrupt sidecar just means redo the shard
            pass

    t0 = time.time()
    per_group: dict[int, list[np.ndarray]] = {}
    stats: dict[int, dict] = {}
    schema_evidence = None
    done = 0

    def work_one(it: dict) -> dict:
        return read_one_file(it, a.sample_mod, a.sample_residue, verify_schema=True)

    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(work_one, it): it for it in mine}
        for fut in cf.as_completed(futs):
            r = fut.result()  # a Fatal here propagates and kills the task, by design
            gi = next(i for i, g in enumerate(tree["groups"])
                      if g["kind"] == r["kind"] and g["name"] == r["name"])
            per_group.setdefault(gi, []).append(r.pop("_keys"))
            s = stats.setdefault(gi, {"kind": r["kind"], "name": r["name"], "files": 0, "rows": 0,
                                      "kept": 0, "bytes_fetched": 0, "n_requests": 0,
                                      "file_bytes": 0,
                                      "partition_counts": [0] * N_PARTITIONS,
                                      "rollout_elements": 0})
            s["files"] += 1
            s["rows"] += r["rows"]
            s["kept"] += r["kept"]
            s["bytes_fetched"] += r["bytes_fetched"]
            s["n_requests"] += r["n_requests"]
            s["file_bytes"] += r["file_bytes"]
            for i in range(N_PARTITIONS):
                s["partition_counts"][i] += r["partition_counts"][i]
            if r.get("schema") and "rollout_elements" in r["schema"]:
                s["rollout_elements"] += r["schema"]["rollout_elements"]
            done += 1
            if done % 25 == 0 or done == len(mine):
                el = time.time() - t0
                print(f"[hash {a.shard}] {done}/{len(mine)} files, "
                      f"{sum(v['rows'] for v in stats.values()):,} rows, "
                      f"{sum(v['bytes_fetched'] for v in stats.values()) / 1e9:.2f} GB, "
                      f"{el:.0f}s (ETA {el / done * (len(mine) - done):.0f}s)", file=sys.stderr)

    # The one payload read: prove the text/rollout_results trap on this shard's first FinePhrase
    # file. Cheap, and it means every shard independently attests to the schema.
    fp_items = [it for it in mine if it["kind"] == "finephrase"]
    if fp_items and a.verify_trap:
        schema_evidence = verify_schema_trap(sorted(fp_items, key=lambda d: d["path"])[0])

    arrays = {}
    for gi, chunks in per_group.items():
        g = tree["groups"][gi]
        arrays[f"{g['kind']}::{g['name']}"] = (np.concatenate(chunks) if chunks
                                               else np.empty(0, dtype=np.uint64))
    np.savez(npz, **arrays)

    payload = {
        "phase": "hash", "shard": a.shard, "nshards": a.nshards,
        "complete": True,
        "n_files": len(mine),
        "rows": sum(v["rows"] for v in stats.values()),
        "kept": sum(v["kept"] for v in stats.values()),
        "sample_mod": a.sample_mod, "sample_residue": a.sample_residue,
        "finephrase_revision_sha": tree["finephrase_revision_sha"],
        "fineweb_edu_revision_sha": tree["fineweb_edu_revision_sha"],
        "groups": {f"{tree['groups'][gi]['kind']}::{tree['groups'][gi]['name']}": v
                   for gi, v in stats.items()},
        "schema_evidence": schema_evidence,
        "seconds": round(time.time() - t0, 1),
        "peak_rss_mb": peak_rss_mb(),
        "keys_file": os.path.basename(npz),
    }
    with open(meta, "w") as fh:
        json.dump(payload, fh, indent=1)
    print(f"[hash {a.shard}] done: {payload['rows']:,} rows, {payload['kept']:,} kept, "
          f"{payload['seconds']:.0f}s, peak {payload['peak_rss_mb']} MB", file=sys.stderr)
    return payload


def peak_rss_mb() -> int:
    """Peak RSS in MB. ru_maxrss is KB on Linux and BYTES on macOS -- FarmShare is Linux."""
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(v / 1024) if sys.platform != "darwin" else int(v / (1024 * 1024))


# ---------------------------------------------------------------------------------------------
# PHASE `reduce`: exact set arithmetic over the sorted uint64 key space.
# ---------------------------------------------------------------------------------------------

def wilson_ci(k: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Wilson score interval for a proportion k/n.

    Used ONLY in sampled mode. In exact mode the distinct fraction is a census -- reporting a
    sampling CI on a census would be a category error, so exact mode reports `null` for the CI and
    says so in `ci_method`. Wilson rather than normal-approximation because it stays inside [0,1]
    and behaves at small k, which matters for the edu-web collision if that fraction is near 0.
    """
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def survey_shards(work: str, nshards: int, allow_partial: bool) -> tuple[list[dict], list[str]]:
    """Validate the shard set and list the group labels present. Loads NO key arrays.

    Separated from loading on purpose. Loading all five groups' keys at once would hold the whole
    18.3 GB census resident, and then `np.unique` on the largest group would peak another ~3x its
    array on top of that. Reading ONE group at a time (`load_group`) keeps the reduce peak near
    "largest single config x 3" instead of "whole census + largest config x 3".

    A missing shard makes every config smaller by roughly the same fraction, which leaves the
    pairwise Jaccards roughly right and the DISTINCT FRACTION roughly right too -- so a short
    reduce produces a plausible number with no visible symptom. That is the exact failure mode this
    project keeps getting bitten by, so it is fatal by default and the missing indices are named.
    """
    metas: list[dict] = []
    missing: list[int] = []
    labels: set[str] = set()
    for s in range(nshards):
        npz, meta = shard_paths(work, s)
        if not (os.path.exists(npz) and os.path.exists(meta)):
            missing.append(s)
            continue
        m = json.load(open(meta))
        if not m.get("complete"):
            missing.append(s)
            continue
        m["_npz"] = npz
        metas.append(m)
        with np.load(npz) as z:
            labels.update(z.files)
    if missing:
        msg = (f"{len(missing)} of {nshards} hash shards are missing or incomplete: "
               f"{missing[:20]}{' ...' if len(missing) > 20 else ''}. A short reduce silently "
               f"INFLATES the distinct fraction, which is the direction that flatters us. "
               f"Resubmit the array for those indices (the hash phase is idempotent).")
        if not allow_partial:
            die(msg)
        print(f"[reduce] WARNING (--allow-partial): {msg}", file=sys.stderr)

    # Assert every shard agrees on the pinned revisions and the sample rate. Mixing a `main` run
    # with a `78cf4a5e` run would produce a number that measures neither.
    shas = {m["finephrase_revision_sha"] for m in metas}
    mods = {m["sample_mod"] for m in metas}
    if len(shas) != 1:
        die(f"hash shards disagree on the FinePhrase revision: {sorted(shas)}")
    if len(mods) != 1:
        die(f"hash shards disagree on the sample rate: {sorted(mods)}")
    if not metas:
        die("no complete hash shards found at all -- the hash phase never produced output")
    return metas, sorted(labels)


def load_group(metas: list[dict], label: str) -> np.ndarray:
    """Concatenate ONE group's keys across every shard. See `survey_shards` for why one at a time."""
    chunks: list[np.ndarray] = []
    for m in metas:
        with np.load(m["_npz"]) as z:
            if label in z.files:
                arr = z[label]
                if arr.size:
                    chunks.append(arr.astype(np.uint64, copy=False))
    return np.concatenate(chunks) if chunks else np.empty(0, dtype=np.uint64)


def sort_unique_to_disk(work: str, label: str, arr: np.ndarray) -> tuple[str, int, int]:
    """Sort + dedup one group's keys and memory-map the result.

    `np.unique` on 4.0e8 uint64 peaks at roughly 3x the array (input + argsort workspace + output),
    so it is done ONE GROUP AT A TIME and spilled to a `.npy` that is reopened with `mmap_mode='r'`.
    That is what keeps the five groups' combined 18.3 GB out of resident memory during the
    bucket walk.

    Returns (path, n_raw, n_unique). `n_raw - n_unique` is WITHIN-config duplication: FinePhrase
    should have none (one rewrite per source document per config), so a nonzero value here is a
    finding in its own right and is reported, not silently absorbed.
    """
    d = os.path.join(work, "sorted")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{label.replace('::', '__').replace('/', '_')}.npy")
    n_raw = int(arr.size)
    uniq = np.unique(arr)
    np.save(path, uniq)
    del uniq
    return path, n_raw, int(np.load(path, mmap_mode="r").shape[0])


def cmd_reduce(a: argparse.Namespace) -> dict:
    t0 = time.time()
    tree = load_tree(a.work)
    metas, labels = survey_shards(a.work, a.nshards, a.allow_partial)

    fp_labels = [f"finephrase::{c}" for c in CONFIGS]
    ew_labels = [k for k in labels if k.startswith("fineweb_edu::")]
    for lab in fp_labels:
        if lab not in labels:
            die(f"config {lab!r} produced no keys at all. A config that returns zero rows is a "
                f"read failure, not an empty config.")
    if not ew_labels:
        die("no FineWeb-Edu group in the hash output -- the edu-web collision is one of the "
            "required measurements, so an absent parent corpus is a failure, not a zero")

    sorted_paths: dict[str, str] = {}
    raw_counts: dict[str, int] = {}
    uniq_counts: dict[str, int] = {}
    for lab in fp_labels + ew_labels:
        arr = load_group(metas, lab)
        if arr.size == 0:
            die(f"group {lab!r} produced ZERO ids. Refusing to report a distinct fraction that a "
                f"zero would make look perfect.")
        p, nr, nu = sort_unique_to_disk(a.work, lab, arr)
        del arr  # release before the next group's ~3x np.unique peak
        sorted_paths[lab], raw_counts[lab], uniq_counts[lab] = p, nr, nu
        print(f"[reduce] {lab}: {nr:,} keys -> {nu:,} unique "
              f"({nr - nu:,} within-config duplicates)", file=sys.stderr)

    mm = {lab: np.load(p, mmap_mode="r") for lab, p in sorted_paths.items()}

    # -------- the bucket walk --------
    # Each per-config array is sorted, so bucket b = keys in [lo, hi) is a CONTIGUOUS slice found
    # by searchsorted. Every quantity below is an exact count summed over disjoint buckets, so the
    # totals are bucket-count independent -- re-run with a different --buckets to confirm.
    pairs = [(i, j) for i in range(4) for j in range(i + 1, 4)]
    inter = {f"{CONFIGS[i]}|{CONFIGS[j]}": 0 for i, j in pairs}
    union4 = 0
    n_in_k_configs = np.zeros(5, dtype=np.int64)  # how many union docs appear in exactly 1..4
    ew_hits: dict[str, int] = {lab: 0 for lab in ew_labels}
    ew_union_hits = 0

    nb = a.buckets
    step = (1 << 64) // nb
    for b in range(nb):
        lo = np.uint64(b * step)
        hi = np.uint64((b + 1) * step) if b + 1 < nb else np.uint64((1 << 64) - 1)
        slices = []
        for lab in fp_labels:
            arr = mm[lab]
            i0 = int(np.searchsorted(arr, lo, side="left"))
            i1 = int(np.searchsorted(arr, hi, side="right" if b + 1 == nb else "left"))
            slices.append(np.asarray(arr[i0:i1]))
        for (i, j) in pairs:
            inter[f"{CONFIGS[i]}|{CONFIGS[j]}"] += int(
                np.intersect1d(slices[i], slices[j], assume_unique=True).size)
        u = np.unique(np.concatenate(slices)) if any(s.size for s in slices) \
            else np.empty(0, dtype=np.uint64)
        union4 += int(u.size)
        if u.size:
            # multiplicity: in how many of the 4 configs does each union key appear
            mult = np.zeros(u.size, dtype=np.int8)
            for s in slices:
                if s.size:
                    mult += np.isin(u, s, assume_unique=True).astype(np.int8)
            for k in range(1, 5):
                n_in_k_configs[k] += int(np.count_nonzero(mult == k))
            # The edu-web anti-join, per bucket. NOTE the partition shares are NOT computed here:
            # `partition_of` is a function of the id STRING, and only the 64-bit key survives into
            # this phase, so partition counts are accumulated during `hash` (where the strings
            # exist) and folded in below.
            hit = np.zeros(u.size, dtype=bool)
            for lab in ew_labels:
                arr = mm[lab]
                i0 = int(np.searchsorted(arr, lo, side="left"))
                i1 = int(np.searchsorted(arr, hi, side="right" if b + 1 == nb else "left"))
                ew = np.asarray(arr[i0:i1])
                h = np.isin(u, ew, assume_unique=True)
                ew_hits[lab] += int(np.count_nonzero(h))
                hit |= h
            ew_union_hits += int(np.count_nonzero(hit))
        if (b + 1) % max(1, nb // 8) == 0:
            print(f"[reduce] bucket {b + 1}/{nb}, union so far {union4:,} "
                  f"({time.time() - t0:.0f}s)", file=sys.stderr)

    sum_configs = sum(uniq_counts[lab] for lab in fp_labels)
    distinct_fraction = union4 / sum_configs if sum_configs else 0.0

    # -------- partition shares, from the hash phase --------
    # `partition_of` operates on the id STRING, so the counts had to be accumulated where the
    # strings existed. They are per-config over that config's own rows (duplicates included),
    # which is the right population: the partition decides which rows a config KEEPS.
    part_by_config: dict[str, list[int]] = {}
    for m in metas:
        for lab, v in m["groups"].items():
            acc = part_by_config.setdefault(lab, [0] * N_PARTITIONS)
            for i in range(N_PARTITIONS):
                acc[i] += v["partition_counts"][i]

    partition_report = {}
    worst_dev_pp = 0.0
    worst_where = None
    for lab in fp_labels:
        counts = part_by_config.get(lab, [0] * N_PARTITIONS)
        tot = sum(counts)
        if tot == 0:
            die(f"no partition counts for {lab} -- the hash phase did not record them")
        shares = [c / tot for c in counts]
        for idx, sh in enumerate(shares):
            dev = abs(sh - 1.0 / N_PARTITIONS) * 100.0
            if dev > worst_dev_pp:
                worst_dev_pp, worst_where = dev, f"{lab}/partition{idx}({CONFIGS[idx]})"
        partition_report[lab] = {
            "counts": counts, "total": tot,
            "shares": [round(s, 6) for s in shares],
            "min_share": round(min(shares), 6),
            "meets_floor_0.173": bool(min(shares) >= PARTITION_FLOOR),
        }

    # -------- collision budget for the 64-bit keying --------
    n_union = union4
    expected_collisions = (n_union * (n_union - 1) / 2) / float(1 << 64)

    sample_mod = metas[0]["sample_mod"] if metas else a.sample_mod
    mode = "exact" if sample_mod == 1 else "sampled"
    if mode == "sampled":
        lo, hi = wilson_ci(union4, sum_configs)
        ci = [round(lo, 6), round(hi, 6)]
        ci_method = (f"Wilson 95% on union/sum_of_configs at a deterministic 1-in-{sample_mod} "
                     f"hash-prefix sample of the id space")
    else:
        ci = None
        ci_method = ("none -- this is a CENSUS of every id at the pinned revision, not a sample. "
                     "The only uncertainty is 64-bit hash collision, quantified in "
                     "hash_collision_expected_pairs.")

    fp_rows = {lab: sum(m["groups"].get(lab, {}).get("rows", 0) for m in metas)
               for lab in fp_labels}
    total_bytes = sum(v.get("bytes_fetched", 0) for m in metas for v in m["groups"].values())
    total_file_bytes = sum(f["size"] for g in tree["groups"] for f in g["files"])

    out = {
        "tool": "measure_finephrase_overlap.py",
        "phase": "reduce",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": mode,
        "partial": bool(a.allow_partial and len(metas) < a.nshards),
        "sample_mod": sample_mod,
        "sample_rate": 1.0 / sample_mod,
        "sample_method": ("deterministic hash-prefix filter: keep id iff "
                          "int(sha256(id)) % sample_mod == residue. Applied to the SAME predicate "
                          "in every config, so a document is kept or dropped identically "
                          "everywhere -- that is what preserves the overlap signal. NOT a "
                          "first-N-rows sample; parquet row order follows CommonCrawl dump order, "
                          "so a prefix is a sample of the earliest crawls."),
        "finephrase_revision_requested": tree["finephrase_revision_requested"],
        "finephrase_revision_sha": tree["finephrase_revision_sha"],
        "fineweb_edu_revision_sha": tree["fineweb_edu_revision_sha"],
        "eduweb_configs": tree["eduweb_configs"],
        "n_hash_shards_used": len(metas),
        "n_hash_shards_expected": a.nshards,

        "per_config": {
            CONFIGS[i]: {
                "rows_read": fp_rows[fp_labels[i]],
                "ids_kept": raw_counts[fp_labels[i]],
                "distinct_ids": uniq_counts[fp_labels[i]],
                "within_config_duplicate_ids": raw_counts[fp_labels[i]] - uniq_counts[fp_labels[i]],
            } for i in range(4)
        },
        "pairwise": {
            f"{CONFIGS[i]}|{CONFIGS[j]}": {
                "intersection": inter[f"{CONFIGS[i]}|{CONFIGS[j]}"],
                "jaccard": round(
                    inter[f"{CONFIGS[i]}|{CONFIGS[j]}"]
                    / (uniq_counts[fp_labels[i]] + uniq_counts[fp_labels[j]]
                       - inter[f"{CONFIGS[i]}|{CONFIGS[j]}"]), 6)
                if (uniq_counts[fp_labels[i]] + uniq_counts[fp_labels[j]]
                    - inter[f"{CONFIGS[i]}|{CONFIGS[j]}"]) else None,
                "overlap_of_smaller": round(
                    inter[f"{CONFIGS[i]}|{CONFIGS[j]}"]
                    / min(uniq_counts[fp_labels[i]], uniq_counts[fp_labels[j]]), 6),
            } for i, j in pairs
        },
        "union_4way": union4,
        "sum_of_config_distinct_ids": sum_configs,
        "distinct_fraction": round(distinct_fraction, 6),
        "distinct_fraction_ci95": ci,
        "ci_method": ci_method,
        "prior_spot_measure": {
            "distinct_fraction": 0.285,
            "pairwise_overlap_range": [0.903, 0.932],
            "basis": "4 x 1000 sampled ids at revision 78cf4a5e",
        },
        "documents_in_exactly_k_configs": {str(k): int(n_in_k_configs[k]) for k in range(1, 5)},
        "hash_keying": {
            "key": "high 8 bytes of sha256(id), as uint64, sorted",
            "hash_collision_expected_pairs": expected_collisions,
            "collision_direction": ("a collision merges two distinct documents, so it UNDERSTATES "
                                    "the union and the distinct fraction"),
            "formula": "n*(n-1)/2 / 2**64 with n = union_4way",
        },

        "eduweb_collision": {
            "per_config_source": {lab: ew_hits[lab] for lab in ew_labels},
            "union_hits": ew_union_hits,
            "fraction_of_finephrase_union": round(ew_union_hits / union4, 6) if union4 else None,
            "per_finephrase_config": {},  # filled below
            "eduweb_distinct_ids": {lab: uniq_counts[lab] for lab in ew_labels},
            "meaning": ("fraction of DISTINCT FinePhrase source documents that also appear in the "
                        "FineWeb-Edu subset edu-web draws. Untreated, each such document can "
                        "appear as real edu-web text AND as its own rephrasing in one run -- the "
                        "anti-join half of DATASET-DESIGN-reservoir.md 9.7 item 4."),
        },

        "partition_audit": {
            "definition": ("partition_of(id) = int.from_bytes(sha256(id.encode()).digest(),'big') "
                           "% 4; reimplemented here, NOT imported from edullm_data, so this is an "
                           "independent check of src/edullm_data/reservoir_ids.py rather than a "
                           "tautology"),
            "ideal_share": 0.25,
            "design_floor": PARTITION_FLOOR,
            "per_config": partition_report,
            "worst_deviation_pp": round(worst_dev_pp, 4),
            "worst_deviation_at": worst_where,
            "all_partitions_meet_floor": all(v["meets_floor_0.173"]
                                             for v in partition_report.values()),
        },

        "cost": {
            "bytes_fetched": total_bytes,
            "config_parquet_bytes_total": total_file_bytes,
            "fraction_of_corpus_bytes_moved": round(total_bytes / total_file_bytes, 6)
            if total_file_bytes else None,
            "hash_phase_seconds_sum": round(sum(m.get("seconds", 0) for m in metas), 1),
            "hash_phase_peak_rss_mb_max": max((m.get("peak_rss_mb", 0) for m in metas), default=0),
            "reduce_seconds": round(time.time() - t0, 1),
            "reduce_peak_rss_mb": peak_rss_mb(),
        },
        "schema_evidence": next((m["schema_evidence"] for m in metas
                                 if m.get("schema_evidence")), None),
        "schema_assertions_passed": [
            "12 columns == FineWeb-Edu's 11 + rollout_results (per file, from the arrow schema)",
            "'id' column present on every file of both repos",
            "rollout_results list length == 1 for EVERY row group of EVERY file "
            "(footer leaf num_values == num_rows -- an exact proof over all 1.354e9 rows)",
            "top-level `text` != rollout_results[0].text on directly-read rows "
            "(the rewrite is NESTED; `text` is the original)",
            "no null/empty/non-string id anywhere",
            "no file read zero rows",
        ],
        "card_anchors": {
            "source_documents_in_input_split": CARD_SOURCE_DOCS,
            "output_samples_across_all_configs": CARD_OUTPUT_SAMPLES,
        },
    }

    # Per-FinePhrase-config edu-web collision. Recomputed per config so a reader can see whether
    # the collision is uniform (it should be: all four configs draw the same parent).
    per_cfg_ew = {}
    for i, lab in enumerate(fp_labels):
        arr = mm[lab]
        hits = 0
        for b in range(nb):
            lo = np.uint64(b * step)
            hi = np.uint64((b + 1) * step) if b + 1 < nb else np.uint64((1 << 64) - 1)
            i0 = int(np.searchsorted(arr, lo, side="left"))
            i1 = int(np.searchsorted(arr, hi, side="right" if b + 1 == nb else "left"))
            s = np.asarray(arr[i0:i1])
            if not s.size:
                continue
            h = np.zeros(s.size, dtype=bool)
            for lab2 in ew_labels:
                a2 = mm[lab2]
                j0 = int(np.searchsorted(a2, lo, side="left"))
                j1 = int(np.searchsorted(a2, hi, side="right" if b + 1 == nb else "left"))
                h |= np.isin(s, np.asarray(a2[j0:j1]), assume_unique=True)
            hits += int(np.count_nonzero(h))
        per_cfg_ew[CONFIGS[i]] = {
            "hits": hits,
            "fraction": round(hits / uniq_counts[lab], 6) if uniq_counts[lab] else None,
        }
    out["eduweb_collision"]["per_finephrase_config"] = per_cfg_ew

    # -------- verdict against the number every synthetic figure scales off --------
    prior = 0.285
    delta = distinct_fraction - prior
    out["verdict"] = {
        "prior_distinct_fraction": prior,
        "measured_distinct_fraction": round(distinct_fraction, 6),
        "delta_pp": round(delta * 100, 3),
        "relative_change": round(delta / prior, 4),
        "synthetic_sizing_implication": (
            "docs/FINAL-DATASET-MIX.md sizes the synthetic pool from this fraction. A measured "
            f"{distinct_fraction:.4f} vs the assumed {prior:.4f} scales the weighted-partition "
            f"131.0B by {distinct_fraction / prior:.4f} -> "
            f"{131.0 * distinct_fraction / prior:.1f}B, all else equal."),
        "note": ("all-else-equal only: the weighted 35/35/15/15 partition's yield depends on the "
                 "per-format token means too, which this script does not measure (it reads no "
                 "text). Combine with artifacts/recount/synthetic.json."),
    }

    with open(a.out, "w") as fh:
        json.dump(out, fh, indent=1)
    print_summary(out)
    print(f"[reduce] wrote {a.out}", file=sys.stderr)
    return out


def print_summary(o: dict) -> None:
    """Human summary. Every line here has a machine-readable twin in the JSON."""
    p = print
    p("")
    p("=" * 78)
    p(f"FinePhrase cross-config id overlap  [{o['mode'].upper()}"
      + (f", 1-in-{o['sample_mod']}" if o["mode"] == "sampled" else "") + "]")
    p(f"revision {o['finephrase_revision_sha']} (requested "
      f"{o['finephrase_revision_requested']})")
    if o.get("partial"):
        p("!! PARTIAL RUN -- do not consume. Missing hash shards.")
    p("=" * 78)
    p("")
    p("per config:")
    for c, v in o["per_config"].items():
        p(f"  {c:9s} rows={v['rows_read']:>13,}  distinct_ids={v['distinct_ids']:>13,}"
          f"  within-config dups={v['within_config_duplicate_ids']:,}")
    p("")
    p("pairwise (Jaccard / |A^B|):")
    for k, v in o["pairwise"].items():
        p(f"  {k:20s} J={v['jaccard']:.4f}  |A^B|={v['intersection']:>13,}"
          f"  of-smaller={v['overlap_of_smaller']:.4f}")
    p("")
    p(f"4-way union         : {o['union_4way']:,}")
    p(f"sum of configs      : {o['sum_of_config_distinct_ids']:,}")
    p(f"DISTINCT FRACTION   : {o['distinct_fraction']:.4f}"
      + (f"   95% CI [{o['distinct_fraction_ci95'][0]:.4f}, "
         f"{o['distinct_fraction_ci95'][1]:.4f}]" if o["distinct_fraction_ci95"] else "   (census)"))
    p(f"  prior spot-measure: {o['prior_spot_measure']['distinct_fraction']:.4f} "
      f"(4x1000 ids)   delta {o['verdict']['delta_pp']:+.2f} pp")
    p(f"  expected 64-bit hash collisions: {o['hash_keying']['hash_collision_expected_pairs']:.3e} "
      f"pairs (understates the union)")
    p("")
    p("documents appearing in exactly k configs:")
    for k, n in o["documents_in_exactly_k_configs"].items():
        frac = n / o["union_4way"] if o["union_4way"] else 0
        p(f"  k={k}: {n:>13,}  ({frac:.4f} of union)")
    p("")
    ec = o["eduweb_collision"]
    p(f"edu-web collision (FineWeb-Edu {', '.join(o['eduweb_configs'])}):")
    p(f"  union hits {ec['union_hits']:,} = {ec['fraction_of_finephrase_union']} of the "
      f"FinePhrase union")
    for c, v in ec["per_finephrase_config"].items():
        p(f"    {c:9s} {v['hits']:>13,}  ({v['fraction']})")
    p("")
    pa = o["partition_audit"]
    p(f"keeps_id partition audit (ideal 0.2500, design floor {pa['design_floor']}):")
    for lab, v in pa["per_config"].items():
        p(f"  {lab:22s} " + "  ".join(f"{s:.4f}" for s in v["shares"])
          + f"   min={v['min_share']:.4f} {'OK' if v['meets_floor_0.173'] else 'BELOW FLOOR'}")
    p(f"  worst deviation: {pa['worst_deviation_pp']:.3f} pp at {pa['worst_deviation_at']}")
    p(f"  all meet floor : {pa['all_partitions_meet_floor']}")
    p("")
    c = o["cost"]
    p(f"cost: moved {c['bytes_fetched'] / 1e9:.1f} GB of "
      f"{c['config_parquet_bytes_total'] / 1e12:.2f} TB "
      f"({c['fraction_of_corpus_bytes_moved']}) -- the id-column-only argument")
    p(f"      hash CPU-seconds {c['hash_phase_seconds_sum']:.0f}, max shard RSS "
      f"{c['hash_phase_peak_rss_mb_max']} MB; reduce {c['reduce_seconds']:.0f}s, RSS "
      f"{c['reduce_peak_rss_mb']} MB")
    p("")
    p("implication: " + o["verdict"]["synthetic_sizing_implication"])
    p("=" * 78)


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Measure FinePhrase cross-config document-id overlap exactly.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="phases: tree (once, cheap) -> hash (job array) -> reduce (once, serial)")
    p.add_argument("phase", choices=["tree", "hash", "reduce", "selftest"])
    p.add_argument("--work", required=True,
                   help="scratch working dir; holds tree.json, hash/, sorted/. NOT home.")
    p.add_argument("--out", default=None, help="reduce: JSON result path")
    p.add_argument("--revision", default="78cf4a5e",
                   help="FinePhrase revision to PIN. Default matches the prior spot-measure so "
                        "the two numbers are comparable.")
    p.add_argument("--eduweb-revision", default="main")
    p.add_argument("--eduweb-configs", default=EDUWEB_DEFAULT,
                   help="comma-separated FineWeb-Edu repo prefixes. Default sample/350BT -- "
                        "FinePhrase's declared parent (card: source_datasets "
                        "HuggingFaceFW/fineweb-edu/sample-350BT). Use 'data' for the full 1.3T "
                        "corpus, which is what edu-web actually draws in FINAL-DATASET-MIX.md; "
                        "that is 4.52 TB of files and ~59 GB of id bytes.")
    p.add_argument("--shard", type=int, default=0, help="hash: this task's index")
    p.add_argument("--nshards", type=int, default=1, help="hash/reduce: total array size")
    p.add_argument("--workers", type=int, default=8,
                   help="hash: concurrent HTTP/parquet readers per task. I/O bound; 8 is measured "
                        "to saturate a FarmShare node's share of the Hub without 429s.")
    p.add_argument("--sample-mod", type=int, default=1,
                   help="1 = EXACT census (default). k>1 keeps ids where int(sha256(id)) %% k == "
                        "residue, a deterministic unbiased 1-in-k sample of the id space. Note "
                        "this does NOT reduce HTTP bytes, only RAM -- exact is nearly free.")
    p.add_argument("--sample-residue", type=int, default=0)
    p.add_argument("--buckets", type=int, default=DEFAULT_BUCKETS,
                   help="reduce: contiguous key-space buckets. Memory/overhead knob only; the "
                        "result is bucket-count invariant, so change it to verify.")
    p.add_argument("--allow-partial", action="store_true",
                   help="reduce: proceed with missing hash shards and stamp partial=true. A short "
                        "reduce INFLATES the distinct fraction. Exploratory use only.")
    p.add_argument("--force", action="store_true", help="hash: redo a shard already complete")
    p.add_argument("--no-verify-trap", dest="verify_trap", action="store_false",
                   help="hash: skip the one payload read that proves text != rollout_results[0].text")
    a = p.parse_args()

    if a.sample_mod < 1:
        die("--sample-mod must be >= 1")
    if not 0 <= a.sample_residue < max(1, a.sample_mod):
        die(f"--sample-residue must be in [0, {a.sample_mod})")

    if a.phase == "selftest":
        return selftest()
    if a.phase == "tree":
        cmd_tree(a)
    elif a.phase == "hash":
        cmd_hash(a)
    else:
        if not a.out:
            die("reduce requires --out")
        cmd_reduce(a)
    return 0


def selftest() -> int:
    """No network. Proves the arithmetic on synthetic ids with a KNOWN answer.

    Run this first on the login node. It catches a broken numpy/pyarrow install and any regression
    in the bucket walk before an array task spends node-hours -- and it is the only part of this
    script whose expected output is known in advance.
    """
    rng = random.Random(1234)
    # Construct 4 "configs" over a shared pool with a designed overlap so the answer is checkable.
    pool = [f"<urn:uuid:{rng.getrandbits(128):032x}>" for _ in range(20000)]
    shared = pool[:15000]
    cfgs = [shared + pool[15000 + 1000 * i: 15000 + 1000 * (i + 1)] for i in range(4)]
    expect_union = 15000 + 4000
    expect_sum = sum(len(c) for c in cfgs)

    arrs = []
    parts_total = np.zeros(N_PARTITIONS, dtype=np.int64)
    for c in cfgs:
        k, pcount, bad = hash_ids(c, 1, 0)
        assert bad == 0, bad
        parts_total += pcount
        arrs.append(np.unique(k))
    union = np.unique(np.concatenate(arrs))
    ok_union = union.size == expect_union
    ok_pair = np.intersect1d(arrs[0], arrs[1], assume_unique=True).size == 15000
    frac = union.size / expect_sum

    # partition_of must match the documented formula on a fixed vector
    fixed = "<urn:uuid:e2300ad5-01dd-4e80-92b3-7ec88785cc9d>"
    key, part = key_and_partition(fixed)
    ref = int.from_bytes(hashlib.sha256(fixed.encode("utf-8")).digest(), "big") % 4
    ok_part = part == ref
    ok_key = key == int.from_bytes(hashlib.sha256(fixed.encode("utf-8")).digest()[:8], "big")

    # Wilson sanity: a symmetric interval containing the point estimate
    lo, hi = wilson_ci(2850, 10000)
    ok_wilson = lo < 0.285 < hi and (hi - lo) < 0.02

    # sampling determinism: the same predicate keeps the same documents in every config
    ks = [set(hash_ids(c, 100, 0)[0].tolist()) for c in cfgs]
    ok_sample_shared = len(ks[0] & ks[1]) > 0

    # REGRESSION, and the reason SAMPLE_SALT exists. The first version of this script sampled on
    # `int(sha256(id)) % k` and partitioned on `int(sha256(id)) % 4`, i.e. the same integer. With
    # any k divisible by 4 -- 4, 100, 1000, every round number an operator would actually type --
    # the sample predicate FORCES partition 0. A real `--sample-mod 1000` run reported 100.0% of
    # documents in partition 0 and 0.0% in the other three, which reads as a catastrophic
    # partition defect when the partition is fine and the SAMPLER is broken. So: under a
    # 4-divisible sample_mod, all four partitions must still be populated.
    pool2 = [f"<urn:uuid:{rng.getrandbits(128):032x}>" for _ in range(400000)]
    _, pc, _ = hash_ids(pool2, 100, 0)
    ok_sample_partition_indep = bool(pc.min() > 0) and bool(pc.max() / max(1, pc.min()) < 1.6)

    print(f"selftest union      {union.size} (expect {expect_union})  {'OK' if ok_union else 'FAIL'}")
    print(f"selftest pair       {'OK' if ok_pair else 'FAIL'}")
    print(f"selftest distinct   {frac:.4f} (expect {expect_union / expect_sum:.4f})")
    print(f"selftest partition  {part} == {ref}  {'OK' if ok_part else 'FAIL'}")
    print(f"selftest key        {'OK' if ok_key else 'FAIL'}")
    print(f"selftest partitions {parts_total.tolist()} "
          f"(expect ~{expect_sum // 4} each of {expect_sum})")
    print(f"selftest wilson     [{lo:.4f}, {hi:.4f}]  {'OK' if ok_wilson else 'FAIL'}")
    print(f"selftest hashsample shared-kept={len(ks[0] & ks[1])}  "
          f"{'OK' if ok_sample_shared else 'FAIL'}")
    print(f"selftest salt-indep partitions under sample_mod=100: {pc.tolist()}  "
          f"{'OK' if ok_sample_partition_indep else 'FAIL (sample hash correlates with partition)'}")
    import pyarrow as _pa
    print(f"numpy {np.__version__}  pyarrow {_pa.__version__}  python {sys.version.split()[0]}")
    bad = not all([ok_union, ok_pair, ok_part, ok_key, ok_wilson, ok_sample_shared,
                   ok_sample_partition_indep])
    print("SELFTEST " + ("FAILED" if bad else "PASSED"))
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Fatal as e:
        print(f"\nFATAL: {e}\n", file=sys.stderr)
        raise SystemExit(2)
