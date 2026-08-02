"""Exact dedup and eval decontamination.

The tests that matter are the ones that would catch a filter which quietly does nothing: a
decontamination index that parses but matches no benchmark text, or a dedup that misses a duplicate
differing only in line endings. Both failures look exactly like success.

Offline. The one test that touches the real 54 MB index skips when it is not on disk.
"""

from __future__ import annotations

import hashlib
import pathlib
import struct

import pytest

from edullm_data.corpus import BuildError, Document
from edullm_data.corpus_filter import (
    NORMALIZATION_VERSION,
    DecontaminationIndex,
    FilterStats,
    SeenHashes,
    content_hash,
    dedup_and_decontaminate,
    load_index,
    normalize_text,
)
from edullm_data.s3 import FakeS3

#: The real index, if this is a checkout that has the sibling repo. Not a fixture — the point of the
#: test that uses it is that it runs against the ACTUAL artifact the build will load.
REAL_INDEX = pathlib.Path(
    "/Users/ericwu/Developer/Capstone_LLM/pipelines/week1_corpus/config/eval-decontamination.bin"
)


def _doc(text: str, i: int = 0) -> Document:
    return Document(id=f"d{i}", text=text, source="src")


def _index(texts: list[str], ngram_size: int = 3, minimum_hits: int = 2) -> DecontaminationIndex:
    """Build a small index the same way the real one was built."""
    from edullm_data.corpus_filter import _ngram_hash, _words

    exact = {content_hash(t) for t in texts}
    ngrams: set[bytes] = set()
    for t in texts:
        w = _words(t)
        for i in range(max(0, len(w) - ngram_size + 1)):
            ngrams.add(_ngram_hash(w[i: i + ngram_size]))
    return DecontaminationIndex(frozenset(exact), frozenset(ngrams), ngram_size, minimum_hits)


# --------------------------------------------------------------------------------------
# Normalization — the compatibility surface every hash depends on
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b",
    [
        ("line\r\none", "line\none"),      # CRLF: the same doc crawled twice
        ("café", "café"),        # NFC vs NFD
        ("text  ", "text"),                 # trailing whitespace
        ("a\x00b", "ab"),                   # a NUL some tokenizers truncate at
    ],
)
def test_normalization_collapses_the_differences_that_are_not_differences(a, b):
    assert normalize_text(a) == normalize_text(b)
    assert content_hash(a) == content_hash(b)


def test_leading_whitespace_is_PRESERVED():
    """`rstrip`, not `strip`. Indentation is semantic in code, and stackv2-edu is 40B of this corpus."""
    assert normalize_text("    indented") == "    indented"
    assert content_hash("    x") != content_hash("x")


def test_the_normalization_rule_is_versioned():
    """Changing it invalidates every hash ever computed, so a corpus records which rule it used."""
    assert NORMALIZATION_VERSION == "week1-nfc-rstrip-v1"
    assert FilterStats().normalization == NORMALIZATION_VERSION


# --------------------------------------------------------------------------------------
# Dedup
# --------------------------------------------------------------------------------------


def test_an_exact_duplicate_is_dropped_and_counted():
    stats = FilterStats()
    docs = [_doc("hello world", 0), _doc("hello world", 1), _doc("different", 2)]
    kept = list(dedup_and_decontaminate(docs, stats=stats))
    assert [d.id for d in kept] == ["d0", "d2"]
    assert (stats.seen, stats.kept, stats.duplicates) == (3, 2, 1)


def test_a_duplicate_differing_only_in_line_endings_is_still_a_duplicate():
    """The case a naive `text ==` dedup misses, and the reason hashing is over NORMALIZED text."""
    kept = list(dedup_and_decontaminate([_doc("a\r\nb", 0), _doc("a\nb", 1)]))
    assert len(kept) == 1


def test_the_first_occurrence_wins():
    kept = list(dedup_and_decontaminate([_doc("same", 0), _doc("same", 1)]))
    assert kept[0].id == "d0"


def test_seen_hashes_can_be_shared_across_calls():
    """A bundle is read in pieces; the dedup set has to outlive one call to be worth anything."""
    seen = SeenHashes()
    list(dedup_and_decontaminate([_doc("x", 0)], seen=seen))
    kept = list(dedup_and_decontaminate([_doc("x", 1)], seen=seen))
    assert kept == []
    assert len(seen) == 1


# --------------------------------------------------------------------------------------
# Decontamination
# --------------------------------------------------------------------------------------


def test_an_exact_benchmark_document_is_removed():
    idx = _index(["what is the capital of france"])
    stats = FilterStats()
    kept = list(dedup_and_decontaminate(
        [_doc("what is the capital of france", 0), _doc("unrelated prose here", 1)],
        index=idx, stats=stats,
    ))
    assert [d.id for d in kept] == ["d1"]
    assert stats.contaminated == 1


def test_two_ngram_hits_are_required():
    """`minimum_hits=2`. One hit false-positives on boilerplate and discards real training data."""
    idx = _index(["alpha beta gamma delta epsilon"], ngram_size=3, minimum_hits=2)
    one_hit = "alpha beta gamma zzz qqq www"       # exactly one 3-gram in common
    assert not idx.contains(one_hit)
    two_hits = "alpha beta gamma delta zzz qqq"     # two overlapping 3-grams
    assert idx.contains(two_hits)


def test_the_last_window_of_a_document_is_checked():
    """The off-by-one that would let a benchmark question at the END of a document through.

    `range(len(words) - n)` — the natural typo — skips the final window of EVERY document, silently
    and forever. A document of exactly `ngram_size` words must yield one window, not zero.
    """
    idx = _index(["red green blue"], ngram_size=3, minimum_hits=1)
    assert idx.contains("red green blue"), "a doc of exactly ngram_size words must be checked"
    assert idx.contains("filler filler filler red green blue"), "the trailing window must be checked"


def test_an_empty_index_matches_nothing():
    assert not DecontaminationIndex.empty().contains("anything at all")


def test_clean_prose_is_not_flagged():
    idx = _index(["what is the capital of france"], ngram_size=3, minimum_hits=2)
    assert not idx.contains("sourdough starter needs regular feeding with flour and water")


# --------------------------------------------------------------------------------------
# The binary container — a truncated index decontaminates LESS and reports success
# --------------------------------------------------------------------------------------


def _pack(exact: list[bytes], ngrams: list[bytes], n=13, hits=2) -> bytes:
    head = struct.Struct("<8sIIQQ").pack(b"W1DCI001", n, hits, len(exact), len(ngrams))
    return head + b"".join(exact) + b"".join(ngrams)


def test_a_valid_container_round_trips():
    e = [hashlib.sha256(b"x").digest()]
    g = [hashlib.blake2b(b"y", digest_size=16).digest()]
    idx = DecontaminationIndex.from_bytes(_pack(e, g))
    assert idx.exact_hashes == {e[0].hex()}
    assert idx.ngram_hashes == {g[0]}
    assert (idx.ngram_size, idx.minimum_hits) == (13, 2)


def test_a_truncated_container_is_REFUSED_not_silently_smaller():
    """The failure this check exists for: fewer entries parse cleanly and decontaminate less."""
    raw = _pack([hashlib.sha256(bytes([i])).digest() for i in range(4)], [])
    with pytest.raises(BuildError, match="truncated index parses as a smaller one"):
        DecontaminationIndex.from_bytes(raw[:-32])


def test_a_bad_magic_is_refused():
    with pytest.raises(BuildError, match="magic"):
        DecontaminationIndex.from_bytes(b"NOTMAGIC" + bytes(24))


def test_a_short_header_is_refused():
    with pytest.raises(BuildError, match="header is truncated"):
        DecontaminationIndex.from_bytes(b"W1DCI")


def test_duplicate_entries_are_refused():
    """Declaring N entries and holding fewer means the file disagrees with itself."""
    same = hashlib.sha256(b"dup").digest()
    with pytest.raises(BuildError, match="distinct"):
        DecontaminationIndex.from_bytes(_pack([same, same], []))


def test_a_missing_index_RAISES_rather_than_disabling_decontamination():
    """Silently skipping produces a corpus indistinguishable from a decontaminated one.

    `week1_corpus/worker.py:102-106` falls back to an empty index here, which turns a staging
    mistake into a clean-looking corpus discovered months later.
    """
    with pytest.raises(BuildError, match="Refusing to build"):
        load_index(FakeS3())


def test_load_index_reads_a_seeded_object():
    s3 = FakeS3()
    s3.seed("edullm-landing", "_dist/eval-decontamination.bin",
            _pack([hashlib.sha256(b"z").digest()], []))
    assert len(load_index(s3).exact_hashes) == 1


# --------------------------------------------------------------------------------------
# Order, stats, and the real artifact
# --------------------------------------------------------------------------------------


def test_dedup_runs_before_the_expensive_contamination_check():
    """A duplicate must never be n-gram checked: dedup is one sha256, contamination is thousands
    of blake2b hashes against a 3.1M-entry set."""
    calls = []

    class Counting(DecontaminationIndex):
        def contains(self, text):  # type: ignore[override]
            calls.append(text)
            return False

    idx = Counting(frozenset(), frozenset({b"x" * 16}))
    list(dedup_and_decontaminate([_doc("same", 0), _doc("same", 1)], index=idx))
    assert len(calls) == 1, "the duplicate should not have been contamination-checked"


def test_stats_report_counts_not_ratios():
    """A ratio invites the mistake made against leakage-summary.json's category_attrition, which
    reads like a pool fraction and is excluded/candidates."""
    d = FilterStats(seen=10, kept=7, duplicates=2, contaminated=1).as_dict()
    assert d["seen"] == 10 and d["kept"] == 7
    assert not any(isinstance(v, float) for v in d.values())


@pytest.mark.skipif(not REAL_INDEX.exists(), reason="the prebuilt index is not on this machine")
def test_the_real_index_parses_and_agrees_with_its_manifest():
    """Against the ACTUAL 54 MB artifact the build loads, not a fixture.

    The counts are the manifest's own declared values, so this catches both a parser bug and a
    corrupted artifact.
    """
    idx = DecontaminationIndex.from_bytes(REAL_INDEX.read_bytes())
    assert len(idx.exact_hashes) == 149_777
    assert len(idx.ngram_hashes) == 3_097_372
    assert (idx.ngram_size, idx.minimum_hits) == (13, 2)


@pytest.mark.skipif(not REAL_INDEX.exists(), reason="the prebuilt index is not on this machine")
def test_the_real_index_catches_real_benchmark_text():
    """The test that proves the filter is not decoration.

    A parser that produced an index matching NOTHING would pass every other test in this file.
    These are verbatim GSM8K test questions — measured 40/40 caught against the live dataset — and
    ordinary prose, which must not be.
    """
    idx = DecontaminationIndex.from_bytes(REAL_INDEX.read_bytes())
    gsm8k = (
        "Janet’s ducks lay 16 eggs per day. She eats three for breakfast every morning and bakes "
        "muffins for her friends every day with four. She sells the remainder at the farmers' "
        "market daily for $2 per fresh duck egg. How much in dollars does she make every day at "
        "the farmers' market?"
    )
    assert idx.contains(gsm8k), "a verbatim GSM8K test question must be caught"
    assert not idx.contains(
        "Sourdough starter needs regular feeding with equal parts flour and water, kept at room "
        "temperature until it doubles reliably within four to six hours."
    ), "ordinary prose must not be flagged"
