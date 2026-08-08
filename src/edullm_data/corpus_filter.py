"""Exact dedup and eval decontamination — the document-level pass before tokenizing.

§4.1 step 2 and §4.2, and the order matters: both run on DOCUMENTS, before `corpus_pack` sees a
token, because after tokenization there is no document to drop. A shard is a byte range; you cannot
remove a contaminated document from one without re-cutting every shard after it.

**Exact dedup is where the whole measured quality gain is.** §4.1 records DCLM measuring a Bloom
filter ALONE at +1.6 CORE — equal to the full Exact + MinHash + SuffixArray stack — and FineWeb
training on the ~31B kept vs 171B removed and finding *the removed data scored better*. So this
module deliberately does exact content-hash dedup and nothing fuzzier. MinHash is deferred by owner
decision, lands later as an annotate-only sidecar, and is not a gap here.

**Decontamination is load-bearing in a way it is not for most corpora**, because half this reservoir
is FinePhrase — rephrased FineWeb-Edu, and FineWeb-Edu does zero decontamination upstream.
arXiv:2311.04850 measured that paraphrasing defeats n-gram decontamination outright, and OLMo 3's
`decon` found GSM8K "complete leakage" and removed >60,000 DROP examples. This module closes the
n-gram half only; the synthetic half's paraphrase blind spot is genuinely still open.

WHAT IS STOLEN, AND FROM WHERE
------------------------------
The index format, the matching rule, and the normalization are `week1_corpus`'s, reimplemented here
with attribution rather than imported — that package is not a dependency and vendoring one module
would drag in `records`, `contracts`, and its own S3 layer. The pieces worth naming:

* `week1_corpus/decontamination.py:14-24` — word-level 13-grams over `\\w+`, casefolded, hashed with
  `blake2b(digest_size=16)` over `"\\x1f".join(words)`.
* `:115-125` — the two-test rule: exact content hash, OR **at least 2** distinct n-gram hits.
* `records.py:30-32` — the normalization every hash is taken over, pinned upstream as
  `week1-nfc-rstrip-v1`.

**The built index is reused, not rebuilt.** `s3://edullm-landing/_dist/eval-decontamination.bin`,
54,350,848 bytes, sha256 `04aa8fe5…50bfd7` — verified 2026-08-01 to equal the `index.sha256` its
own manifest claims. It covers 9 OE-eval families, MMLU all 57 subjects × {dev, validation, test}
rendered with the real 5-shot template, and GSM8K. Rebuilding it needs a pinned `ai2-olmo` checkout;
loading it needs this file.

THREE THINGS THAT LOOK LIKE DETAILS AND ARE NOT
------------------------------------------------
1. **`minimum_hits = 2`.** At 13-gram granularity over 3.1M benchmark n-grams, single-hit matching
   false-positives on boilerplate and throws away real training data.
2. **The window count is `len(words) - n + 1`.** The natural typo, `range(len(words) - n)`, silently
   skips the last window of every document — so a benchmark question sitting at the END of a
   document is never caught, on every document, forever.
3. **Normalization is fixed *before* hashing and is a compatibility surface.** Every dedup decision
   this module has ever made is a function of it; changing it invalidates them all. Hence
   :data:`NORMALIZATION_VERSION`, recorded in the stats so a corpus states which rule it was built
   under.
"""

from __future__ import annotations

import array
import hashlib
import re
import struct
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .corpus import BuildError, Document

__all__ = [
    "NORMALIZATION_VERSION",
    "DECON_INDEX_KEY",
    "DEFAULT_PARTITIONS",
    "KEEP_HASH_BITS",
    "KEEP_LIST_MAGIC",
    "KEEP_LIST_SET_SCHEMA",
    "DecontaminationIndex",
    "FilterStats",
    "HashScan",
    "KeepFilter",
    "KeepList",
    "SeenHashes",
    "content_hash",
    "dedup_and_decontaminate",
    "hash64",
    "keep_list_set_index",
    "normalize_text",
    "partition_of",
    "read_keep_list",
    "resolve_keep_lists",
]

#: The normalization rule, versioned because it IS the dedup identity.
#: Matches `week1_corpus`'s `week1-nfc-rstrip-v1` byte for byte so a hash computed by either side
#: means the same thing — which is what makes the prebuilt index's exact-hash half usable at all.
NORMALIZATION_VERSION = "week1-nfc-rstrip-v1"

#: Where the prebuilt index lives. `_dist/` deliberately: it is the one landing prefix with NO
#: expiry rule (verified against the live lifecycle config — the others expire at 14d, `_ingest/`
#: at 30d), and this artifact is expensive to rebuild and was briefly single-copy on a laptop.
DECON_INDEX_KEY = "_dist/eval-decontamination.bin"

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_BINARY_MAGIC = b"W1DCI001"
_BINARY_HEADER = struct.Struct("<8sIIQQ")


def normalize_text(text: str) -> str:
    """CRLF → LF, drop NULs, NFC, strip TRAILING whitespace only.

    Ported from `week1_corpus/records.py:30-32`. Each clause earns its place:

    * **CRLF → LF** — otherwise the same document crawled twice survives dedup as two documents.
    * **Drop `\\x00`** — some fast tokenizers truncate at a NUL, silently discarding the tail of a
      document while reporting success.
    * **NFC** — otherwise NFC and NFD spellings of identical text hash differently.
    * **`rstrip`, not `strip`** — leading whitespace is semantic in code, and `stackv2-edu` is 40B
      of the corpus.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return unicodedata.normalize("NFC", text).rstrip()


def content_hash(text: str) -> str:
    """sha256 of the NORMALIZED text. The dedup key, and the index's exact-match key."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(normalize_text(text).casefold())


def _ngram_hash(words: list[str]) -> bytes:
    # `\x1f` (unit separator) as the joiner rather than a space: a space would make the 2-gram
    # ["a b", "c"] collide with ["a", "b c"], and benchmark text contains both.
    return hashlib.blake2b("\x1f".join(words).encode("utf-8"), digest_size=16).digest()


@dataclass(frozen=True)
class DecontaminationIndex:
    """Benchmark text to keep OUT of the corpus, as exact hashes plus 13-gram hashes.

    Two `frozenset`s, ~250 MB resident for the shipped index — budget it on Batch, it is not free.
    """

    exact_hashes: frozenset[str]
    ngram_hashes: frozenset[bytes]
    ngram_size: int = 13
    minimum_hits: int = 2

    @classmethod
    def empty(cls) -> DecontaminationIndex:
        """An index that matches nothing.

        ⚠️ Use it only where "no decontamination" is a DELIBERATE choice. `week1_corpus` falls back
        to this when its index file is missing (`worker.py:102-106`), which turns a staging mistake
        into a silently uncontaminated-looking corpus. :func:`load_index` raises instead.
        """
        return cls(frozenset(), frozenset())

    @classmethod
    def from_bytes(cls, raw: bytes) -> DecontaminationIndex:
        """Parse the `W1DCI001` container, strictly.

        Every length is checked because a truncated download parses as a SMALLER index — which
        decontaminates less and reports success, the same shape of silent failure as a short
        `.json.gz` read.
        """
        if len(raw) < _BINARY_HEADER.size:
            raise BuildError("decontamination index header is truncated")
        magic, ngram_size, minimum_hits, n_exact, n_ngram = _BINARY_HEADER.unpack(
            raw[: _BINARY_HEADER.size]
        )
        if magic != _BINARY_MAGIC:
            raise BuildError(f"decontamination index magic is {magic!r}, expected {_BINARY_MAGIC!r}")
        want = _BINARY_HEADER.size + n_exact * 32 + n_ngram * 16
        if len(raw) != want:
            raise BuildError(
                f"decontamination index is {len(raw)} bytes but its header declares "
                f"{n_exact:,} exact + {n_ngram:,} ngram entries = {want} bytes. A truncated index "
                f"parses as a smaller one and decontaminates less while reporting success."
            )
        off = _BINARY_HEADER.size
        exact = frozenset(
            raw[off + i * 32: off + i * 32 + 32].hex() for i in range(n_exact)
        )
        off += n_exact * 32
        ngrams = frozenset(raw[off + i * 16: off + i * 16 + 16] for i in range(n_ngram))
        if len(exact) != n_exact or len(ngrams) != n_ngram:
            raise BuildError(
                f"decontamination index declares {n_exact:,}/{n_ngram:,} entries but holds "
                f"{len(exact):,}/{len(ngrams):,} distinct — duplicate or corrupt entries"
            )
        return cls(exact, ngrams, int(ngram_size), int(minimum_hits))

    def contains(self, text: str) -> bool:
        """True when this document should be REMOVED.

        Two independent tests, OR'd: the exact content hash, or ``minimum_hits`` distinct 13-gram
        hits. Early-returns on the second hit — without that, a multi-billion-document corpus hashes
        every window of every document to reach the same answer.
        """
        if not self.exact_hashes and not self.ngram_hashes:
            return False
        if content_hash(text) in self.exact_hashes:
            return True
        words = _words(text)
        hits = 0
        # `- self.ngram_size + 1`: a document of exactly `ngram_size` words yields ONE window.
        # `range(len(words) - n)` is the natural typo and skips the last window of every document.
        for i in range(max(0, len(words) - self.ngram_size + 1)):
            if _ngram_hash(words[i: i + self.ngram_size]) in self.ngram_hashes:
                hits += 1
                if hits >= self.minimum_hits:
                    return True
        return False


def load_index(s3, bucket: str = "edullm-landing", key: str = DECON_INDEX_KEY):
    """Fetch and parse the prebuilt index. RAISES when it is absent.

    Deliberately not `DecontaminationIndex.empty()` on failure. A build that quietly skips
    decontamination produces a corpus indistinguishable from a decontaminated one — you find out
    when a benchmark score looks too good, months later, and cannot tell which runs were affected.
    """
    try:
        raw = s3.get(bucket, key)
    except Exception as exc:  # noqa: BLE001
        raise BuildError(
            f"cannot read the decontamination index at s3://{bucket}/{key}: {exc}. Refusing to "
            f"build: skipping decontamination silently produces a corpus that looks "
            f"decontaminated. Pass --no-decontaminate to accept that deliberately."
        ) from exc
    return DecontaminationIndex.from_bytes(raw)


@dataclass
class SeenHashes:
    """Content hashes already emitted, for exact dedup within a build.

    Dedup here is **within a bundle**, which is where duplicates actually cluster (one source, one
    crawl, adjacent shards); cross-bundle dedup needs the shared Bloom filter §4.1 budgets at ~$3
    and is a different stage.

    **Stores a 128-bit int, not the 64-char hex string, and that is a memory fix rather than a
    style one.** MEASURED with `tracemalloc` over a 200,000-entry set:

        set[str]  (64-char hex)  154.9 B/entry   ->  120M documents = 18.6 GB
        set[int]  (128-bit)       85.9 B/entry   ->  120M documents = 10.3 GB

    The docstring this replaces claimed ~113 B/entry, which is `sys.getsizeof` of the string alone
    and ignores the set's own slot overhead — it understated the real cost by 37%. That mattered:
    `stackv2-edu--train` is ~120M documents, so its dedup set alone wanted 18.6 GB inside a 20 GiB
    container, which is why only 3 children fit per 64 GiB host and the whole cluster sat at 97%
    memory with 25% of its CPU idle.

    128 bits of sha256 is the right truncation. Birthday collision probability over 120M documents
    is ~2e-20 — a false "duplicate" would silently drop one document, and at that rate it will not
    happen. Full 256-bit would cost 65 B/entry more for no reachable benefit.

    :func:`content_hash` still returns hex and is UNCHANGED, because it is also the key format of
    the shipped decontamination index (`DecontaminationIndex.exact_hashes`); changing it would
    invalidate a 54 MB artifact already staged in S3. The narrowing happens here, at the one call
    site that keeps hashes resident.
    """

    hashes: set[int] = field(default_factory=set)

    def add_if_new(self, digest: str) -> bool:
        """`digest` is hex, as :func:`content_hash` returns; the int conversion is internal."""
        key = int(digest[:32], 16)  # top 128 bits
        if key in self.hashes:
            return False
        self.hashes.add(key)
        return True

    def __len__(self) -> int:
        return len(self.hashes)


@dataclass
class FilterStats:
    """What this pass removed, and why. Reported per bundle so attrition is auditable.

    Counts, never a ratio, because a ratio invites exactly the mistake made against
    `leakage-summary.json`'s `category_attrition`: read as a pool fraction it overstated
    decontamination loss by four orders of magnitude, when it was really excluded ÷ candidates.
    A denominator you have to guess is a denominator someone will guess wrong.
    """

    seen: int = 0
    kept: int = 0
    duplicates: int = 0
    contaminated: int = 0
    normalization: str = NORMALIZATION_VERSION

    def as_dict(self) -> dict:
        return {
            "seen": self.seen,
            "kept": self.kept,
            "duplicates": self.duplicates,
            "contaminated": self.contaminated,
            "normalization": self.normalization,
        }


def dedup_and_decontaminate(
    docs: Iterable[Document],
    *,
    index: DecontaminationIndex | None = None,
    seen: SeenHashes | None = None,
    stats: FilterStats | None = None,
) -> Iterator[Document]:
    """Drop exact duplicates and contaminated documents, in that order.

    **Dedup first, and the order is not arbitrary.** Contamination checking is the expensive half —
    up to `len(words) - 12` blake2b hashes per document against a 3.1M-entry set — while dedup is
    one sha256. Testing the cheap predicate first means a duplicate is never contamination-checked,
    and duplicates are common in web crawls.

    A generator: the corpus does not fit in memory and neither does one source of it.
    """
    seen = seen if seen is not None else SeenHashes()
    stats = stats if stats is not None else FilterStats()
    for doc in docs:
        stats.seen += 1
        digest = content_hash(doc.text)
        if not seen.add_if_new(digest):
            stats.duplicates += 1
            continue
        if index is not None and index.contains(doc.text):
            stats.contaminated += 1
            continue
        stats.kept += 1
        yield doc


# --------------------------------------------------------------------------------------
# The dedup PRE-PASS — §5.2a / §5.3, task #22
# --------------------------------------------------------------------------------------
#
# WHY THIS EXISTS, IN ONE PARAGRAPH
# ---------------------------------
# :class:`SeenHashes` is a `set[int]` at a MEASURED 85.9 B/entry. The worst bundle of this corpus
# is `dclm--train` at 325M documents (§5.2a, MEASURED tokens/doc) = **27.92 GB** inside a 15.03 GB
# container. It OOMs. A flat array holds the same 325M in **2.74 GB** — but it cannot answer
# "seen?" incrementally, which is exactly WHY this is a separate pass rather than a drop-in.
#
# Re-MEASURED here, `tracemalloc` over 200,000 entries on this branch, and the plan's arithmetic
# needs one correction:
#
#     SeenHashes  set[int]        85.95 B/entry   ->  DCLM 27.93 GB, global 960M  82.53 GB
#     HashScan    array('Q')       8.43 B/entry   ->  DCLM  2.74 GB, global 960M   8.09 GB
#     KeepFilter  array + bitmap   8.13 B/key     ->  DCLM  2.64 GB, global 960M   7.80 GB
#
# 85.95 reproduces `SeenHashes`'s docstring to 0.06%, and 27.93 reproduces §5.2a's 27.92 GB.
# ⚠️ §5.3's "**2.60 GB for DCLM**, 7.68 GB global" is the IDEAL 8.000 B/key. The real accumulator
# measures 8.43 because CPython over-allocates ~1/16 on `array.append`, so the true figures are
# 2.74 and 8.09 GB — 5.4% above the plan. Both still fit; the ratio is **10.2x**, inside the 5-11x
# §5.3 predicts. Recorded because the plan's number is a floor, not a measurement, and sizing a
# container to 2.60 GB would be sizing to something no run will ever hit.
#
# ⚠️ §5.2's table names `finephrase-table` (225.6M / 19.37 GB) as the worst bundle. That table is
# the RESERVOIR's mix scaled 3.981x, and the reservoir is 23.82% synthetic against this corpus's
# 4.3% — so it over-weights precisely the bundles that dominate it. §5.2a redoes it and DCLM is
# **1.44x worse** than the bundle the plan called the blocker, and does not appear in the 5.2 table
# at all. Size a container for DCLM.
#
# DETERMINISM IS THE SECOND CONSTRAINT, AND IT IS NOT A NICE-TO-HAVE
# ------------------------------------------------------------------
# A shared mutable filter threaded through the build makes the output depend on bundle EXECUTION
# ORDER, which destroys the byte-identical-rerun property this repo has verified live (9 bundles /
# 4,137 shards re-run on a new wheel gave byte-identical digests). A pre-pass emitting an
# IMMUTABLE keep-list preserves it: pass 2 reads a frozen artifact and no bundle can observe
# another bundle's progress.
#
# WHY 256 PARTITIONS GIVES EXACT GLOBAL DEDUP WITH ZERO SHARED STATE
# -------------------------------------------------------------------
# The partition is the TOP 8 BITS OF THE HASH, not the source. So worker `p` sees documents from
# EVERY source whose hash lands in `p` — and two byte-identical documents have the same hash by
# construction, hence the same partition. Cross-source duplicates are therefore always co-located,
# and a worker needs to talk to nobody. That is what makes this exact rather than probabilistic.
#
# WHY NOT A BLOOM FILTER. `orchestrator-findings.md` F4 calls Bloom "the only affordable global
# option" and is SUPERSEDED: it sized n=2.28B (the *synthetic* 438.5 tok/doc applied corpus-wide,
# corrected inside its own file) at fp=1e-6, and it was comparing against a global `set` at
# 195.9 GB because the flat-array pre-pass had not been considered. A Bloom false positive
# DISCARDS A REAL DOCUMENT. Bloom is the fallback, not the plan.
#
# WHY 64 BITS IS ENOUGH. Over 1.23B documents the probability that ANY 64-bit collision exists is
# 4.08% — but the expected number of colliding PAIRS is 0.04, i.e. expected loss under one
# document. :class:`SeenHashes`'s 128 bits is over-provisioned by 64.
#
# WHAT THIS PASS CANNOT DO. Near-duplicates survive (§5.4): boilerplate-differing pages, one
# interior whitespace character. That is deliberate — FineWeb MEASURED global cross-dump MinHash as
# actively harmful (removed data outperformed kept data), and our upstream sources have each
# already deduped themselves, so the remaining job is cross-source only.

#: Top 8 bits of the hash. Over-provisioned on purpose: §5.3 sizes the table at 1.23B documents
#: even though §5.2a measures the real mix at 0.96B, because raising a partition count is free.
DEFAULT_PARTITIONS = 256

#: Bits of sha256 retained in a keep-list key. See the birthday argument above.
KEEP_HASH_BITS = 64

#: `\0`-padded to 8 bytes so the header is a clean `<8sIIQQ`, mirroring `W1DCI001`.
KEEP_LIST_MAGIC = b"EDKL001\x00"
KEEP_LIST_SET_SCHEMA = "edullm-keeplist-set/v1"

_KEEP_HEADER = struct.Struct("<8sIIQQ")
#: `array.array('Q')` is NATIVE byte order; the on-disk payload is defined as little-endian, the
#: same choice `.u32le.bin` makes. On a big-endian host both directions byteswap.
_NEEDS_SWAP = struct.pack("=Q", 1) != struct.pack("<Q", 1)


def hash64(digest_hex: str) -> int:
    """Top 64 bits of a :func:`content_hash` hex digest, as an int.

    Deliberately a slice of the SAME digest :class:`SeenHashes` slices to 128 bits, so a keep-list
    key and a `SeenHashes` key are derived from one sha256 of one normalization. :func:`content_hash`
    itself is UNCHANGED — it is also the key format of the 54 MB shipped decontamination index, and
    narrowing it there would invalidate an artifact already staged in S3.
    """
    if len(digest_hex) < 16:
        raise BuildError(
            f"digest {digest_hex!r} is {len(digest_hex)} hex chars; hash64 needs at least 16. "
            f"Pass a content_hash() digest, not an already-truncated key."
        )
    return int(digest_hex[:16], 16)


def partition_of(key: int, partitions: int = DEFAULT_PARTITIONS) -> int:
    """Which of ``partitions`` slices owns ``key`` — the TOP bits, never a modulus.

    Top bits rather than ``key % partitions`` for one reason that matters downstream: it makes the
    partitions **order-preserving**, so concatenating them in ascending partition order yields a
    globally ascending key sequence for free. :meth:`KeepList.to_bytes` requires ascending, and a
    modulus would force a second global sort to get it.
    """
    if partitions <= 0 or partitions & (partitions - 1):
        raise BuildError(f"partitions must be a positive power of two, got {partitions}")
    shift = KEEP_HASH_BITS - partitions.bit_length() + 1
    return key >> shift


def _numpy():
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - numpy is a hard dependency of the package
        raise BuildError(
            "the dedup pre-pass needs numpy for its sort; the flat-array design is the whole "
            "memory fix and a Python fallback would reinstate the 85.9 B/entry set"
        ) from exc
    return np


@dataclass
class HashScan:
    """Pass 1's accumulator: every content hash this bundle produced, partitioned, at ~8 B/key.

    **`array.array('Q')`, not a `set` and not a growing numpy array**, and both halves of that are
    deliberate:

    * against `set[int]` at a MEASURED 85.9 B/entry it is ~8.5 B/key including CPython's ~1/16
      over-allocation on append — an **~10x** reduction, and the 5-11x the plan predicts;
    * against a manually doubled numpy array it avoids the 2x peak at every resize, and — the
      reason worth stating — `array.array` allocates through `PyMem_Malloc`, so **`tracemalloc`
      SEES IT**. numpy allocates outside the Python allocator and is invisible to `tracemalloc`,
      which would make the repo's own measurement idiom silently report ~0 for the very structure
      whose size is the point.

    Narrowing the key INSIDE a `set` does not help and this is worth knowing before someone tries
    it: `sys.getsizeof` is 44 B for a 128-bit int and 36 B for a 64-bit one, so against the measured
    85.9 B/entry the set's own slot overhead is 41.9 B/entry and width-independent — CPython stores
    a pointer plus a cached hash, not the value. 128->64 inside a set buys 9.3%, not 50%. The whole
    win comes from leaving the set.
    """

    partitions: int = DEFAULT_PARTITIONS
    #: partition index -> raw keys, in arrival order, WITH duplicates. Deduped in `finalize`.
    buckets: dict[int, array.array] = field(default_factory=dict)
    #: Documents presented. Not the key count — duplicates are still in `buckets` at this stage.
    scanned: int = 0

    def __post_init__(self) -> None:
        if self.partitions <= 0 or self.partitions & (self.partitions - 1):
            raise BuildError(f"partitions must be a positive power of two, got {self.partitions}")

    def add_key(self, key: int) -> None:
        p = partition_of(key, self.partitions)
        buf = self.buckets.get(p)
        if buf is None:
            buf = self.buckets[p] = array.array("Q")
        buf.append(key)
        self.scanned += 1

    def add_digest(self, digest_hex: str) -> None:
        self.add_key(hash64(digest_hex))

    def add_text(self, text: str) -> None:
        self.add_digest(content_hash(text))

    def scan(self, docs: Iterable[Document]) -> "HashScan":
        """Consume documents, return self. Pass 1's whole loop.

        No decontamination here on purpose: the keep-list answers "who OWNS this hash", and
        contamination is a property of the text that pass 2 re-tests per document anyway. Running
        the 3.1M-entry n-gram index in pass 1 too would double the expensive half of the filter to
        change nothing — a contaminated document is dropped in pass 2 whether or not it won its
        hash.
        """
        for doc in docs:
            self.add_text(doc.text)
        return self

    def finalize(self) -> dict[int, Any]:
        """partition -> **sorted, unique** ``np.uint64`` array. Intra-bundle dedup happens here."""
        np = _numpy()
        out: dict[int, Any] = {}
        for p in sorted(self.buckets):
            buf = self.buckets[p]
            arr = np.frombuffer(memoryview(buf), dtype=np.uint64)
            out[p] = np.unique(arr)
        return out

    @property
    def distinct(self) -> int:
        """Distinct keys, recomputed from the buckets rather than tracked incrementally."""
        return sum(int(len(v)) for v in self.finalize().values())

    def nbytes(self) -> int:
        """Bytes the accumulator's payload occupies. Exact, not an estimate."""
        return sum(buf.buffer_info()[1] * buf.itemsize for buf in self.buckets.values())


@dataclass(frozen=True)
class KeepList:
    """The immutable pass-1 output for ONE bundle: the hashes it WON, ascending.

    Frozen, and the ascending invariant is checked on construction rather than assumed, because
    membership in :class:`KeepFilter` is a binary search — an unsorted payload would not error, it
    would silently report "not in the list" for real keys and **discard real documents**, which is
    the exact failure mode a Bloom filter was rejected for.
    """

    bundle_id: str
    keys: array.array

    def __post_init__(self) -> None:
        if not self.bundle_id:
            raise BuildError("KeepList.bundle_id is empty; it is the artifact's filename")
        if self.keys.typecode != "Q":
            raise BuildError(f"KeepList.keys must be array('Q'), got {self.keys.typecode!r}")
        prev = -1
        for k in self.keys:
            if k <= prev:
                raise BuildError(
                    f"{self.bundle_id}: keep-list keys are not strictly ascending at {k}. "
                    f"Membership is a binary search, so an unsorted list silently reports "
                    f"'absent' for present keys and discards real documents."
                )
            prev = k

    def __len__(self) -> int:
        return len(self.keys)

    def __contains__(self, key: int) -> bool:
        import bisect

        i = bisect.bisect_left(self.keys, key)
        return i < len(self.keys) and self.keys[i] == key

    def to_bytes(self) -> bytes:
        """The `EDKL001` container. Header declares the payload length so truncation is CATCHABLE."""
        payload = self.keys
        if _NEEDS_SWAP:  # pragma: no cover - every host this runs on is little-endian
            payload = array.array("Q", self.keys)
            payload.byteswap()
        raw = payload.tobytes()
        return (
            _KEEP_HEADER.pack(
                KEEP_LIST_MAGIC, KEEP_HASH_BITS, DEFAULT_PARTITIONS, len(self.keys), len(raw)
            )
            + raw
        )

    def sha256(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


def read_keep_list(raw: bytes, bundle_id: str = "keep-list") -> KeepList:
    """Parse `EDKL001`, strictly — every length RECOMPUTED, never taken on the header's word.

    A truncated download must not parse as a SMALLER keep-list. A smaller keep-list is not a
    smaller error: every key missing from it turns a real document into a "cross-bundle loser" and
    drops it, silently, while the build reports success. Same shape of failure as a short
    `.json.gz` read, and the same treatment `DecontaminationIndex.from_bytes` already gives it.
    """
    if len(raw) < _KEEP_HEADER.size:
        raise BuildError(
            f"{bundle_id}: keep-list is {len(raw)} bytes, shorter than its {_KEEP_HEADER.size}-byte "
            f"header"
        )
    magic, hash_bits, partitions, n_keys, payload_bytes = _KEEP_HEADER.unpack(
        raw[: _KEEP_HEADER.size]
    )
    if magic != KEEP_LIST_MAGIC:
        raise BuildError(
            f"{bundle_id}: keep-list magic is {magic!r}, expected {KEEP_LIST_MAGIC!r}"
        )
    if hash_bits != KEEP_HASH_BITS:
        raise BuildError(
            f"{bundle_id}: keep-list declares {hash_bits}-bit keys, this build uses "
            f"{KEEP_HASH_BITS}. A width mismatch makes every lookup miss and drops the corpus."
        )
    if payload_bytes != n_keys * 8:
        raise BuildError(
            f"{bundle_id}: keep-list header declares {n_keys:,} keys but {payload_bytes:,} payload "
            f"bytes; {n_keys:,} x 8 = {n_keys * 8:,}"
        )
    want = _KEEP_HEADER.size + payload_bytes
    if len(raw) != want:
        raise BuildError(
            f"{bundle_id}: keep-list is {len(raw):,} bytes but its header declares {n_keys:,} keys "
            f"= {want:,} bytes. A truncated keep-list parses as a smaller one and DISCARDS real "
            f"documents while reporting success."
        )
    keys = array.array("Q")
    keys.frombytes(raw[_KEEP_HEADER.size:])
    if _NEEDS_SWAP:  # pragma: no cover - every host this runs on is little-endian
        keys.byteswap()
    # KeepList.__post_init__ recomputes the ascending invariant; `partitions` is carried for the
    # reader's information only, which is why it is not asserted against DEFAULT_PARTITIONS —
    # a keep-list built 128-way is still a correct keep-list.
    del partitions
    return KeepList(bundle_id, keys)


def resolve_keep_lists(
    contributions: Mapping[str, Any],
    *,
    priority: Sequence[str] | None = None,
    partitions: int = DEFAULT_PARTITIONS,
    partitions_subset: Iterable[int] | None = None,
) -> dict[str, KeepList]:
    """Pass 1's reduce: decide which bundle WINS each globally-duplicated hash.

    ``contributions`` maps ``bundle_id`` -> a :class:`HashScan`, a ``{partition: array}`` mapping as
    :meth:`HashScan.finalize` returns, or any iterable of int keys.

    ``priority`` is the ORDERED list of bundle ids, **highest quality FIRST**. It is a parameter
    rather than a constant because who wins a cross-source duplicate is a corpus-design decision,
    not an implementation detail — see the module docstring block above and this function's
    ``_default_priority``.

    ``partitions_subset`` runs one slice of the hash space, which is how this is distributed: 256
    workers each pass their own ``p``, no worker talks to another, and the union of their outputs is
    the same answer a single process computes. Determinism does not depend on how it is split.
    """
    np = _numpy()
    ranked = _default_priority(contributions) if priority is None else list(priority)
    rank_of = {}
    for i, b in enumerate(ranked):
        if b in rank_of:
            raise BuildError(f"priority lists {b!r} twice; the winner would be ambiguous")
        rank_of[b] = i
    missing = [b for b in contributions if b not in rank_of]
    if missing:
        raise BuildError(
            f"priority does not rank {len(missing)} contributing bundle(s): {sorted(missing)[:5]}. "
            f"An unranked bundle has no defined precedence, and defaulting it would decide a "
            f"corpus-design question by accident."
        )
    if len(ranked) > 65535:
        raise BuildError(f"{len(ranked)} bundles exceeds the uint16 rank width")

    per_bundle = {b: _as_partitioned(v, partitions, np) for b, v in contributions.items()}
    wanted = (
        sorted(set(partitions_subset))
        if partitions_subset is not None
        else list(range(partitions))
    )
    won: dict[str, list] = {b: [] for b in contributions}

    # Ascending partition order matters: `partition_of` is a right-shift, so partitions are
    # order-preserving and concatenating them ascending yields a globally ascending key sequence
    # with no second sort. `KeepList` re-checks that rather than trusting it.
    for p in wanted:
        if p < 0 or p >= partitions:
            raise BuildError(f"partition {p} is outside 0..{partitions - 1}")
        present = [(b, per_bundle[b][p]) for b in contributions if p in per_bundle[b]]
        present = [(b, a) for b, a in present if len(a)]
        if not present:
            continue
        if len(present) == 1:
            b, arr = present[0]
            won[b].append(arr)
            continue
        keys = np.concatenate([a for _, a in present])
        ranks = np.concatenate(
            [np.full(len(a), rank_of[b], dtype=np.uint16) for b, a in present]
        )
        # Primary key ascending, secondary rank ascending -> the first row of each key group is the
        # highest-priority claimant. `lexsort`'s LAST key is the primary one.
        order = np.lexsort((ranks, keys))
        skeys = keys[order]
        sranks = ranks[order]
        first = np.empty(len(skeys), dtype=bool)
        first[0] = True
        np.not_equal(skeys[1:], skeys[:-1], out=first[1:])
        wkeys = skeys[first]
        wranks = sranks[first]
        for b, _ in present:
            sel = wkeys[wranks == rank_of[b]]
            if len(sel):
                won[b].append(sel)

    out: dict[str, KeepList] = {}
    for b in contributions:
        chunks = won[b]
        keys = array.array("Q")
        if chunks:
            joined = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
            keys.frombytes(np.ascontiguousarray(joined, dtype="<u8").tobytes())
            if _NEEDS_SWAP:  # pragma: no cover
                keys.byteswap()
        out[b] = KeepList(b, keys)
    return out


def _default_priority(contributions: Mapping[str, Any]) -> list[str]:
    """`sorted(bundle_ids)` — and this default is a RECORDED UNMADE DECISION, not a recommendation.

    §5.5 says the winner of a cross-source duplicate is today "decided by alphabetical accident."
    That describes the PROPOSED state. Today it is worse and simpler: `run_bundle` passes no
    ``seen=``, so `dedup_and_decontaminate` builds a fresh `SeenHashes` per bundle and **there is no
    cross-bundle dedup at all** — every copy of a cross-source duplicate survives. So "default to
    today's behaviour" has no literal meaning here, and the nearest honest default is the order the
    plan already emits bundles in, which `plan_document` sorts as
    ``(source, domain or "", split)`` — reproduced by `sorted(bundle_id)` for every source in the
    current 17-row registry, because each source is either wholly flat or wholly domain-bearing.

    **This makes `dclm--train` the top-priority bundle purely because "d" is an early letter.** That
    is not a quality judgement and must not be mistaken for one. An owner decision is needed; pass
    ``priority=`` to encode it. The mechanism is here and costs nothing to redirect.
    """
    return sorted(contributions)


def _as_partitioned(value: Any, partitions: int, np) -> dict[int, Any]:
    if isinstance(value, HashScan):
        if value.partitions != partitions:
            raise BuildError(
                f"scan is partitioned {value.partitions} ways, resolve was asked for {partitions}"
            )
        return value.finalize()
    if isinstance(value, Mapping):
        return {int(p): np.asarray(a, dtype=np.uint64) for p, a in value.items()}
    arr = np.asarray(list(value) if not hasattr(value, "dtype") else value, dtype=np.uint64)
    out: dict[int, Any] = {}
    if not len(arr):
        return out
    shift = KEEP_HASH_BITS - partitions.bit_length() + 1
    parts = (arr >> np.uint64(shift)).astype(np.int64)
    for p in np.unique(parts):
        out[int(p)] = np.unique(arr[parts == p])
    return out


@dataclass
class KeepFilter:
    """Pass 2's replacement for :class:`SeenHashes` — duck-type compatible, and IMMUTABLE.

    Same `add_if_new(hex) -> bool` surface, so `dedup_and_decontaminate` is unchanged and the wiring
    is a `seen=` argument. What differs is that this filter **decides nothing**: pass 1 already did,
    and this only reports the frozen verdict. That is what preserves byte-identical reruns — no
    bundle can observe another bundle's progress, so execution order cannot reach the output.

    Resident cost: the sorted key array at exactly 8 B/key (no over-allocation — it is built once,
    at its final size) plus a used-bitmap MEASURED at 0.127 B/key = **8.13 B/key**, against
    `SeenHashes`'s MEASURED 85.95 — a **10.6x** reduction. DCLM's 325M documents go from
    **27.93 GB to 2.64 GB**, which is what takes the worst bundle from OOM inside a 15.03 GB
    container to 18% of it.

    Membership is `bisect` over an `array.array`, not a `set` and not numpy: a `set` would reinstate
    the 85.9 B/entry this class exists to remove, and requiring numpy in pass 2 would put a hard
    dependency on the consumer side for ~30 integer comparisons per document — comparisons that are
    invisible next to the sha256 the caller already paid for.
    """

    keep: KeepList
    _used: bytearray = field(init=False, repr=False)
    hits: int = 0
    repeats: int = 0
    misses: int = 0

    def __post_init__(self) -> None:
        self._used = bytearray((len(self.keep) + 7) // 8)

    def _index(self, key: int) -> int:
        import bisect

        keys = self.keep.keys
        i = bisect.bisect_left(keys, key)
        return i if i < len(keys) and keys[i] == key else -1

    def add_if_new(self, digest: str) -> bool:
        """True only when this bundle OWNS the hash and has not yet emitted it."""
        i = self._index(hash64(digest))
        if i < 0:
            self.misses += 1
            return False
        byte, bit = divmod(i, 8)
        if self._used[byte] >> bit & 1:
            self.repeats += 1
            return False
        self._used[byte] |= 1 << bit
        self.hits += 1
        return True

    def __len__(self) -> int:
        return self.hits

    @property
    def unused(self) -> int:
        """Keys pass 1 awarded that pass 2 never presented.

        ⚠️ **Non-zero means the two passes disagreed about the input**, which is the only signal
        that the staged read changed between them. Surface it; do not swallow it. Zero is the
        expected value and the cheap end-to-end check that pass 1 and pass 2 saw one corpus.
        """
        return len(self.keep) - self.hits

    def as_dict(self) -> dict:
        return {
            "keys": len(self.keep),
            "hits": self.hits,
            "repeats": self.repeats,
            "misses": self.misses,
            "unused": self.unused,
            "hash_bits": KEEP_HASH_BITS,
        }


def keep_list_set_index(
    keep_lists: Mapping[str, KeepList],
    scans: Mapping[str, Any] | None = None,
    *,
    priority: Sequence[str] | None = None,
    plan_id: str = "",
) -> dict:
    """The `keeplists.json` body — the set-level index over every bundle's `.keep64`.

    ⚠️ **Named `keeplists.json` and NEVER `manifest.json`.** A `manifest.json` landing anywhere in
    `s3://edullm-landing` fires the `edullm-landing-manifest-created` EventBridge rule, which
    submits a validator job with `--promote` and irreversibly freezes a version. A pre-pass
    artifact must not be able to do that by being named badly.

    ``scans`` is optional and supplies the per-bundle ``scanned``/``distinct`` denominators. Without
    it those read 0 rather than being guessed — a denominator you have to guess is the
    `category_attrition` mistake, which overstated decontamination loss by four orders of magnitude.
    """
    ranked = list(priority) if priority is not None else _default_priority(keep_lists)
    rows = []
    for b in sorted(keep_lists):
        kl = keep_lists[b]
        s = (scans or {}).get(b)
        scanned = int(getattr(s, "scanned", 0) or 0)
        distinct = int(s.distinct) if isinstance(s, HashScan) else 0
        rows.append(
            {
                "bundle_id": b,
                "path": f"{b}.keep64",
                "scanned": scanned,
                "distinct": distinct,
                # `lost` is only meaningful when `distinct` is known; -1 marks "not measured"
                # rather than 0, which would read as "lost nothing".
                "lost": (distinct - len(kl)) if distinct else -1,
                "keys": len(kl),
                "sha256": kl.sha256(),
            }
        )
    return {
        "schema": KEEP_LIST_SET_SCHEMA,
        "plan_id": plan_id,
        "normalization": NORMALIZATION_VERSION,
        "hash_bits": KEEP_HASH_BITS,
        "partitions": DEFAULT_PARTITIONS,
        "priority": ranked,
        "priority_basis": "explicit" if priority is not None else "plan-order",
        "documents_scanned": sum(r["scanned"] for r in rows),
        "distinct_keys": sum(r["keys"] for r in rows),
        "bundles": rows,
    }
