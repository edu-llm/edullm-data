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

import hashlib
import re
import struct
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from .corpus import BuildError, Document

__all__ = [
    "NORMALIZATION_VERSION",
    "DECON_INDEX_KEY",
    "DecontaminationIndex",
    "FilterStats",
    "SeenHashes",
    "content_hash",
    "dedup_and_decontaminate",
    "normalize_text",
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

    A plain `set[str]` of 64-char hex. At ~1.5B documents that is far too large for one worker, and
    this is honest about the limit rather than pretending otherwise: dedup here is **within a
    bundle**, which is where the duplicates actually cluster (one source, one crawl, adjacent
    shards). Cross-bundle dedup needs the shared Bloom filter §4.1 budgets at ~$3, and is a
    different stage.

    Sized in the report so the limit is visible rather than assumed: 64-char hex strings cost ~113 B
    each in CPython, so a 10M-document bundle is ~1.1 GB.
    """

    hashes: set[str] = field(default_factory=set)

    def add_if_new(self, digest: str) -> bool:
        if digest in self.hashes:
            return False
        self.hashes.add(digest)
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
