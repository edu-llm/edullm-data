"""The dedup pre-pass — §5.2a / §5.3, task #22.

**Every test here RECOMPUTES.** The repo's golden rule is "recompute, never trust", and for a
keep-list the decorative version of these tests is easy to write and worthless: asserting that a
keep-list *has* a `keys` field, or that `to_bytes` round-trips, proves nothing about dedup. So the
load-bearing tests build a document set with a KNOWN duplicate structure, compute the unique set
INDEPENDENTLY with a plain Python `set` over the full 256-bit `content_hash`, and assert the
pre-pass's surviving documents equal it exactly.

The memory claims are MEASURED with `tracemalloc`, the same idiom `SeenHashes.__doc__` uses, not
asserted.
"""

from __future__ import annotations

import array
import random
import tracemalloc

import pytest

from edullm_data.corpus import BuildError, Document
from edullm_data.corpus_filter import (
    DEFAULT_PARTITIONS,
    KEEP_HASH_BITS,
    KEEP_LIST_MAGIC,
    KEEP_LIST_SET_SCHEMA,
    FilterStats,
    HashScan,
    KeepFilter,
    KeepList,
    SeenHashes,
    content_hash,
    dedup_and_decontaminate,
    hash64,
    keep_list_set_index,
    partition_of,
    read_keep_list,
    resolve_keep_lists,
)

# --------------------------------------------------------------------------------------
# A corpus with a duplicate structure we can state exactly
# --------------------------------------------------------------------------------------


def _corpus() -> dict[str, list[Document]]:
    """Three bundles, with every duplicate relationship the pre-pass must resolve.

    * ``alpha`` and ``beta`` share "shared-A" — a CROSS-SOURCE duplicate. Exactly one must survive.
    * ``beta`` and ``gamma`` share "shared-B" — a second cross-source pair, different partition.
    * ``alpha`` holds "dup-in-alpha" twice under two ids — an INTRA-bundle duplicate, which today's
      per-bundle `SeenHashes` already catches.
    * "shared-A" appears in ``alpha`` under a DIFFERENT id than in ``beta``, because dedup is on
      TEXT and must not be fooled by the id differing.
    * ``gamma`` holds a document whose text differs from another only by trailing whitespace —
      `normalize_text` rstrips, so these are the SAME document and must dedup.
    """
    return {
        "alpha": [
            Document(id="a1", text="alpha only one", source="alpha"),
            Document(id="a2", text="shared-A body", source="alpha"),
            Document(id="a3", text="dup-in-alpha", source="alpha"),
            Document(id="a4", text="dup-in-alpha", source="alpha"),
            Document(id="a5", text="alpha only two", source="alpha"),
        ],
        "beta": [
            Document(id="b1", text="shared-A body", source="beta"),
            Document(id="b2", text="beta only one", source="beta"),
            Document(id="b3", text="shared-B body", source="beta"),
        ],
        "gamma": [
            Document(id="g1", text="shared-B body", source="gamma"),
            Document(id="g2", text="gamma only one", source="gamma"),
            Document(id="g3", text="gamma only one   \n  ", source="gamma"),
        ],
    }


def _independent_unique(corpus: dict[str, list[Document]]) -> set[str]:
    """The ground truth, computed with NO pre-pass code: full 256-bit hashes in a plain set."""
    seen: set[str] = set()
    for docs in corpus.values():
        for d in docs:
            seen.add(content_hash(d.text))
    return seen


def _run_pass2(corpus, keep_lists) -> tuple[dict[str, list[Document]], dict[str, KeepFilter]]:
    """Pass 2 exactly as `run_bundle` will: `dedup_and_decontaminate(seen=KeepFilter(...))`."""
    out, filters = {}, {}
    for bundle_id, docs in corpus.items():
        f = KeepFilter(keep_lists[bundle_id])
        filters[bundle_id] = f
        out[bundle_id] = list(
            dedup_and_decontaminate(docs, seen=f, stats=FilterStats())
        )
    return out, filters


def _prepass(corpus, *, priority=None, order=None):
    ids = order or list(corpus)
    scans = {b: HashScan().scan(corpus[b]) for b in ids}
    return scans, resolve_keep_lists(scans, priority=priority)


# --------------------------------------------------------------------------------------
# THE load-bearing test: survivors == independently computed unique set
# --------------------------------------------------------------------------------------


def test_survivors_equal_independently_computed_unique_set():
    corpus = _corpus()
    scans, keep = _prepass(corpus)
    kept, _ = _run_pass2(corpus, keep)

    survivors = [d for docs in kept.values() for d in docs]
    survivor_hashes = [content_hash(d.text) for d in survivors]

    # Recomputed ground truth, from a different code path (full-width hashes, plain set).
    assert set(survivor_hashes) == _independent_unique(corpus)
    # And EXACTLY ONE copy of each — a set comparison alone would pass if a duplicate survived.
    assert len(survivor_hashes) == len(set(survivor_hashes))
    # 11 documents in (5 + 3 + 3), 7 distinct texts out. Enumerated so the fixture's duplicate
    # structure is stated rather than trusted: alpha contributes 4 distinct (dup-in-alpha
    # collapses), beta adds 2 (shared-A is alpha's), gamma adds 1 (shared-B is beta's, and its two
    # "gamma only one" spellings differ only by trailing whitespace, which `normalize_text` rstrips).
    assert sum(len(v) for v in corpus.values()) == 11
    assert len(survivors) == 7
    assert sum(s.scanned for s in scans.values()) == 11


def test_cross_source_duplicate_survives_exactly_once_and_in_the_priority_winner():
    corpus = _corpus()
    _, keep = _prepass(corpus, priority=["beta", "gamma", "alpha"])
    kept, _ = _run_pass2(corpus, keep)

    where = {b: {d.text for d in docs} for b, docs in kept.items()}
    # beta outranks alpha, so beta keeps shared-A and alpha loses it.
    assert "shared-A body" in where["beta"]
    assert "shared-A body" not in where["alpha"]
    # beta outranks gamma, so beta keeps shared-B too.
    assert "shared-B body" in where["beta"]
    assert "shared-B body" not in where["gamma"]

    # Reverse the priority and the winner MOVES — proving priority is what decides it, not
    # iteration order or partition layout.
    _, keep2 = _prepass(corpus, priority=["alpha", "gamma", "beta"])
    kept2, _ = _run_pass2(corpus, keep2)
    where2 = {b: {d.text for d in docs} for b, docs in kept2.items()}
    assert "shared-A body" in where2["alpha"]
    assert "shared-A body" not in where2["beta"]
    assert "shared-B body" in where2["gamma"]
    assert "shared-B body" not in where2["beta"]

    # Either way the corpus-level survivor set is IDENTICAL — priority moves who owns a document,
    # never whether it exists.
    assert {content_hash(d.text) for docs in kept.values() for d in docs} == {
        content_hash(d.text) for docs in kept2.values() for d in docs
    }


def test_todays_per_bundle_dedup_does_NOT_catch_the_cross_source_duplicate():
    """The measurement that justifies the whole pass — recomputed, not cited.

    `run_bundle` passes no `seen=`, so each bundle gets a fresh `SeenHashes`. This asserts the
    consequence directly: cross-source duplicates survive TWICE today.
    """
    corpus = _corpus()
    kept = [
        d
        for docs in corpus.values()
        for d in dedup_and_decontaminate(docs, seen=SeenHashes(), stats=FilterStats())
    ]
    hashes = [content_hash(d.text) for d in kept]
    assert len(hashes) == 9  # 11 in, only the 2 INTRA-bundle duplicates removed
    assert len(set(hashes)) == 7  # 2 cross-source duplicates still present, twice each
    dupes = {h for h in hashes if hashes.count(h) > 1}
    assert dupes == {content_hash("shared-A body"), content_hash("shared-B body")}
    # 2 real documents leak through today that the pre-pass removes — recomputed as the gap
    # between per-bundle and global dedup on the same input.
    assert len(hashes) - len(set(hashes)) == 2


# --------------------------------------------------------------------------------------
# Determinism — the second constraint of §5.3, and not a nice-to-have
# --------------------------------------------------------------------------------------


def test_keep_lists_are_identical_under_shuffled_input_order():
    corpus = _corpus()
    _, baseline = _prepass(corpus)
    baseline_bytes = {b: kl.to_bytes() for b, kl in baseline.items()}

    rng = random.Random(1234)
    for trial in range(12):
        shuffled = {}
        bundle_order = list(corpus)
        rng.shuffle(bundle_order)
        for b in bundle_order:
            docs = list(corpus[b])
            rng.shuffle(docs)  # documents WITHIN a bundle reordered too
            shuffled[b] = docs
        _, keep = _prepass(shuffled, order=bundle_order)
        assert {b: kl.to_bytes() for b, kl in keep.items()} == baseline_bytes, (
            f"trial {trial}: keep-list changed under a different input order"
        )


def test_partition_subsets_reassemble_into_the_same_keep_lists():
    """256 independent workers must produce exactly what one process produces.

    This is the "zero shared state" claim, checked rather than argued: each partition is resolved
    alone and the union is compared byte-for-byte against the single-process answer.
    """
    corpus = _corpus()
    scans, whole = _prepass(corpus)

    merged: dict[str, array.array] = {b: array.array("Q") for b in corpus}
    for p in range(DEFAULT_PARTITIONS):
        part = resolve_keep_lists(scans, partitions_subset=[p])
        for b, kl in part.items():
            merged[b].extend(kl.keys)

    for b in corpus:
        assert KeepList(b, merged[b]).to_bytes() == whole[b].to_bytes()


def test_every_key_lands_in_the_partition_that_owns_it_and_partitions_are_order_preserving():
    keys = [hash64(content_hash(f"doc-{i}")) for i in range(4000)]
    for k in keys:
        p = partition_of(k)
        assert 0 <= p < DEFAULT_PARTITIONS
        assert p == k >> 56  # recomputed independently of `partition_of`
    # Order-preserving: a key in a lower partition is strictly smaller than one in a higher.
    by_part: dict[int, list[int]] = {}
    for k in keys:
        by_part.setdefault(partition_of(k), []).append(k)
    for lo in sorted(by_part):
        for hi in sorted(by_part):
            if lo < hi:
                assert max(by_part[lo]) < min(by_part[hi])


# --------------------------------------------------------------------------------------
# A bigger randomised corpus, with a duplicate rate we control
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("n_bundles,n_docs,dup_rate", [(4, 700, 0.35), (7, 400, 0.6)])
def test_randomised_corpus_matches_the_independent_unique_set(n_bundles, n_docs, dup_rate):
    rng = random.Random(20260808 + n_docs)
    pool = [f"body number {i} " + "x" * (i % 17) for i in range(n_docs)]
    corpus: dict[str, list[Document]] = {}
    for b in range(n_bundles):
        bid = f"src{b:02d}"
        docs = []
        for j in range(n_docs):
            text = rng.choice(pool) if rng.random() < dup_rate else pool[j]
            docs.append(Document(id=f"{bid}-{j}", text=text, source=bid))
        corpus[bid] = docs

    scans, keep = _prepass(corpus)
    kept, filters = _run_pass2(corpus, keep)

    survivors = [content_hash(d.text) for docs in kept.values() for d in docs]
    assert set(survivors) == _independent_unique(corpus)
    assert len(survivors) == len(set(survivors))

    # The set index's own invariant: awarded keys sum to the global distinct count.
    idx = keep_list_set_index(keep, scans)
    assert sum(r["keys"] for r in idx["bundles"]) == idx["distinct_keys"]
    assert idx["distinct_keys"] == len(_independent_unique(corpus))
    assert idx["documents_scanned"] == n_bundles * n_docs

    # Nothing was awarded and then left on the floor: `unused` is the pass-1/pass-2 agreement check.
    for bid, f in filters.items():
        assert f.unused == 0
        # Every document this bundle presented is accounted for as exactly one of the three.
        assert f.hits + f.repeats + f.misses == len(corpus[bid])
        assert f.hits == len(kept[bid])
    assert sum(f.hits for f in filters.values()) == len(survivors)
    assert sum(f.hits + f.repeats + f.misses for f in filters.values()) == n_bundles * n_docs


# --------------------------------------------------------------------------------------
# MEASURED memory — tracemalloc, the same idiom `SeenHashes.__doc__` uses
# --------------------------------------------------------------------------------------


def _bytes_per_entry(build, n: int) -> float:
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    obj = build(n)
    peak = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    assert obj is not None
    return (peak - base) / n


def test_measured_bytes_per_entry_beats_the_set_by_at_least_5x():
    """RECOMPUTES the 85.9 B/entry claim and the flat-array claim in the same process.

    Both are measured here rather than quoted, because the whole design rests on the ratio and a
    quoted ratio measured on another machine is not evidence about this one.
    """
    n = 200_000
    digests = [content_hash(f"m-{i}") for i in range(n)]

    def build_set(k):
        s = SeenHashes()
        for d in digests[:k]:
            s.add_if_new(d)
        return s

    def build_scan(k):
        sc = HashScan()
        for d in digests[:k]:
            sc.add_digest(d)
        return sc

    set_bpe = _bytes_per_entry(build_set, n)
    scan_bpe = _bytes_per_entry(build_scan, n)

    # The docstring's 85.9 B/entry, re-measured. Loose bounds: this is an allocator measurement,
    # not a constant of nature, and a CPython build could move it.
    assert 60.0 <= set_bpe <= 130.0, f"set[int] measured {set_bpe:.1f} B/entry"
    # `array.array('Q')` is 8 B/key plus CPython's ~1/16 append over-allocation.
    assert scan_bpe <= 12.0, f"HashScan measured {scan_bpe:.1f} B/entry"
    assert set_bpe / scan_bpe >= 5.0, f"only {set_bpe / scan_bpe:.1f}x better, need >=5x"

    # Exact, not sampled: the accumulator's payload is n keys x 8 bytes.
    sc = build_scan(n)
    assert sc.nbytes() >= n * 8
    assert sc.scanned == n


def test_keepfilter_resident_cost_is_about_8_bytes_per_key():
    n = 200_000
    keys = sorted({hash64(content_hash(f"k-{i}")) for i in range(n)})
    kl = KeepList("bulk", array.array("Q", keys))

    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    f = KeepFilter(kl)
    peak = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    # The keep-list array is pre-existing; what KeepFilter ADDS is the 1-bit/key used-bitmap.
    added = (peak - base) / len(keys)
    assert f.as_dict()["keys"] == len(keys)
    assert added <= 0.5, f"KeepFilter added {added:.3f} B/key, expected ~0.125 (1 bit)"


# --------------------------------------------------------------------------------------
# The container: truncation must RAISE, never parse smaller
# --------------------------------------------------------------------------------------


def test_keep_list_round_trips_and_recomputes_its_own_length():
    keys = sorted({hash64(content_hash(f"rt-{i}")) for i in range(500)})
    kl = KeepList("rt", array.array("Q", keys))
    raw = kl.to_bytes()
    assert raw[:8] == KEEP_LIST_MAGIC
    assert len(raw) == 32 + 8 * len(keys)
    back = read_keep_list(raw, "rt")
    assert list(back.keys) == keys
    assert back.sha256() == kl.sha256()


@pytest.mark.parametrize("cut", [1, 8, 33, 137])
def test_truncated_keep_list_raises_instead_of_parsing_as_a_smaller_one(cut):
    keys = sorted({hash64(content_hash(f"tr-{i}")) for i in range(64)})
    raw = KeepList("tr", array.array("Q", keys)).to_bytes()
    with pytest.raises(BuildError, match="keep-list"):
        read_keep_list(raw[:-cut], "tr")


def test_bad_magic_and_wrong_hash_width_are_rejected():
    keys = array.array("Q", [1, 2, 3])
    raw = KeepList("bm", keys).to_bytes()
    with pytest.raises(BuildError, match="magic"):
        read_keep_list(b"XXXXXXXX" + raw[8:], "bm")
    with pytest.raises(BuildError, match="32-bit keys"):
        read_keep_list(raw[:8] + (32).to_bytes(4, "little") + raw[12:], "bm")


def test_unsorted_keep_list_is_refused_at_construction():
    with pytest.raises(BuildError, match="ascending"):
        KeepList("bad", array.array("Q", [5, 3, 9]))
    with pytest.raises(BuildError, match="ascending"):
        KeepList("bad", array.array("Q", [5, 5]))  # a repeat is not strictly ascending either


def test_keep_list_rejects_an_empty_bundle_id_and_a_wrong_typecode():
    with pytest.raises(BuildError, match="bundle_id"):
        KeepList("", array.array("Q", [1]))
    with pytest.raises(BuildError, match="array"):
        KeepList("x", array.array("I", [1]))


# --------------------------------------------------------------------------------------
# KeepFilter semantics — the contract eng-06 codes against
# --------------------------------------------------------------------------------------


def test_keepfilter_reports_hits_repeats_misses_and_unused_separately():
    owned = [content_hash("owned-one"), content_hash("owned-two")]
    keys = sorted(hash64(d) for d in owned)
    f = KeepFilter(KeepList("b", array.array("Q", keys)))

    assert f.add_if_new(owned[0]) is True      # hit
    assert f.add_if_new(owned[0]) is False     # repeat
    assert f.add_if_new(content_hash("not-mine")) is False  # miss
    assert f.as_dict() == {
        "keys": 2, "hits": 1, "repeats": 1, "misses": 1, "unused": 1,
        "hash_bits": KEEP_HASH_BITS,
    }
    assert f.add_if_new(owned[1]) is True
    assert f.unused == 0


def test_filter_stats_identity_survives_the_keepfilter():
    """`seen == kept + duplicates + contaminated` must still close, or the receipt lies."""
    corpus = _corpus()
    _, keep = _prepass(corpus)
    for bundle_id, docs in corpus.items():
        stats = FilterStats()
        list(dedup_and_decontaminate(docs, seen=KeepFilter(keep[bundle_id]), stats=stats))
        assert stats.seen == stats.kept + stats.duplicates + stats.contaminated
        assert stats.seen == len(docs)


def test_an_empty_keep_list_keeps_nothing_and_says_so():
    f = KeepFilter(KeepList("empty", array.array("Q")))
    assert f.add_if_new(content_hash("anything")) is False
    assert f.as_dict()["misses"] == 1
    assert f.unused == 0


# --------------------------------------------------------------------------------------
# Priority: the mechanism, and the guard on an unranked bundle
# --------------------------------------------------------------------------------------


def test_unranked_bundle_raises_rather_than_being_defaulted():
    corpus = _corpus()
    scans = {b: HashScan().scan(corpus[b]) for b in corpus}
    with pytest.raises(BuildError, match="does not rank"):
        resolve_keep_lists(scans, priority=["alpha", "beta"])  # gamma missing


def test_duplicate_priority_entry_raises():
    scans = {"a": HashScan().scan([Document(id="1", text="t", source="a")])}
    with pytest.raises(BuildError, match="twice"):
        resolve_keep_lists(scans, priority=["a", "a"])


def test_default_priority_is_plan_order_and_the_index_labels_it_as_such():
    corpus = _corpus()
    scans, keep = _prepass(corpus)
    idx = keep_list_set_index(keep, scans)
    assert idx["priority"] == sorted(corpus)
    assert idx["priority_basis"] == "plan-order"
    idx2 = keep_list_set_index(keep, scans, priority=["beta", "alpha", "gamma"])
    assert idx2["priority_basis"] == "explicit"
    assert idx2["priority"] == ["beta", "alpha", "gamma"]


def test_set_index_is_not_named_manifest_and_carries_the_normalization_version():
    corpus = _corpus()
    scans, keep = _prepass(corpus)
    idx = keep_list_set_index(keep, scans, plan_id="deadbeef")
    assert idx["schema"] == KEEP_LIST_SET_SCHEMA
    assert "manifest" not in idx["schema"]
    assert all(not r["path"].endswith("manifest.json") for r in idx["bundles"])
    assert idx["normalization"] == "week1-nfc-rstrip-v1"
    assert idx["plan_id"] == "deadbeef"
    # sha256 in the index is the sha256 of the bytes that would be written, recomputed here.
    import hashlib

    for r in idx["bundles"]:
        assert r["sha256"] == hashlib.sha256(keep[r["bundle_id"]].to_bytes()).hexdigest()
    # `lost` is MEASURED per bundle, and under plan order (`sorted` -> alpha, beta, gamma) it falls
    # out exactly as alphabetical precedence dictates: alpha ranks first and loses nothing, beta
    # loses shared-A to alpha, gamma loses shared-B to beta. This is §5.5's "alphabetical accident"
    # made visible — it is not a quality judgement and an owner decision should replace it.
    lost = {r["bundle_id"]: r["lost"] for r in idx["bundles"]}
    assert lost == {"alpha": 0, "beta": 1, "gamma": 1}
    assert sum(lost.values()) == sum(
        scans[b].distinct for b in corpus
    ) - idx["distinct_keys"]


# --------------------------------------------------------------------------------------
# Interop with the existing pieces
# --------------------------------------------------------------------------------------


def test_hash64_is_the_top_64_bits_of_the_same_digest_seenhashes_slices():
    d = content_hash("interop")
    assert hash64(d) == int(d[:16], 16)
    assert hash64(d) == int(d[:32], 16) >> 64  # the top half of SeenHashes' 128-bit key
    with pytest.raises(BuildError, match="hex chars"):
        hash64("abc")


def test_normalization_is_shared_so_a_keeplist_key_means_what_the_index_key_means():
    """Trailing whitespace and CRLF must collapse in the pre-pass exactly as in `content_hash`."""
    sc = HashScan()
    for t in ["one\r\ntwo", "one\ntwo", "one\ntwo   ", "one\ntwo\x00"]:
        sc.add_text(t)
    assert sc.scanned == 4
    assert sc.distinct == 1


def test_partitions_are_balanced_because_sha256_top_bits_are_uniform():
    """The 256-way split is only safe if no partition is much bigger than the mean.

    Sizing a worker at "total/256" assumes uniformity. sha256's top bits are uniform, but that is
    an assumption about the hash reaching the partition function intact — a bug in `partition_of`
    (a modulus on a low-entropy field, say) would show up here as skew and nowhere else, since
    every correctness test would still pass with a lopsided split.
    """
    import statistics

    n = 120_000
    sc = HashScan()
    for i in range(n):
        sc.add_digest(content_hash(f"bal-{i}"))
    sizes = [len(sc.finalize().get(p, ())) for p in range(DEFAULT_PARTITIONS)]

    assert sum(sizes) == n  # no key lost or double-counted by the partitioning
    assert all(s > 0 for s in sizes)  # every partition occupied
    mean = statistics.mean(sizes)
    # MEASURED at 2,000,000 keys: max/mean = 1.0383. At this smaller n the tail is wider, so the
    # bound is loose; it is here to catch skew, not to pin a constant.
    assert max(sizes) / mean < 1.35, f"partition skew {max(sizes) / mean:.3f} — check partition_of"


def test_partition_count_must_be_a_power_of_two():
    with pytest.raises(BuildError, match="power of two"):
        HashScan(partitions=100)
    with pytest.raises(BuildError, match="power of two"):
        partition_of(1, partitions=0)


def test_resolve_accepts_raw_key_iterables_not_just_scans():
    a = [hash64(content_hash(t)) for t in ("p", "q")]
    b = [hash64(content_hash(t)) for t in ("q", "r")]
    keep = resolve_keep_lists({"a": a, "b": b}, priority=["b", "a"])
    assert len(keep["a"]) == 1 and len(keep["b"]) == 2
    assert hash64(content_hash("q")) in keep["b"]
    assert hash64(content_hash("q")) not in keep["a"]
