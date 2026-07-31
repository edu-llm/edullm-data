"""Tests for the §9.7 item 4 synthetic id partition.

Mirrors `src/edullm_data/reservoir_ids.py`. Every test here recomputes something rather than
asserting a field is present — the partition's whole value is arithmetic that must hold on real
ids, so a test that only checked "returns an int in range" would be the decoration the golden
rule forbids.

The uuids below are generated deterministically from a fixed seed, NOT sampled from FinePhrase:
committing real corpus ids would pin this suite to a mutable upstream. The balance claim is
verified against real FinePhrase ids separately, out of band, and recorded in the design doc.
"""

from __future__ import annotations

import hashlib
import random
import uuid

import pytest

from edullm_data.reservoir_ids import (
    FINEPHRASE_FORMATS,
    N_PARTITIONS,
    IdPartitionError,
    audit_partition,
    format_for_id,
    keeps_id,
    partition_of,
)


def _urn_ids(n: int, seed: int = 20260731) -> list[str]:
    """`n` FinePhrase-shaped ids: `<urn:uuid:...>`, the exact form the corpus ships."""
    rng = random.Random(seed)
    return [f"<urn:uuid:{uuid.UUID(int=rng.getrandbits(128), version=4)}>" for _ in range(n)]


# --------------------------------------------------------------------------------------
# The bijection: partition <-> format
# --------------------------------------------------------------------------------------


def test_partition_count_matches_format_count():
    """A free `n` would let the two drift; the design needs one format per partition."""
    assert N_PARTITIONS == len(FINEPHRASE_FORMATS) == 4


def test_every_id_is_kept_by_exactly_one_format():
    """THE load-bearing property. If a document were kept by two formats the collision this
    module exists to remove would still be present, just smaller."""
    for doc_id in _urn_ids(2_000):
        keepers = [f for f in FINEPHRASE_FORMATS if keeps_id(f, doc_id)]
        assert keepers == [format_for_id(doc_id)], doc_id
        assert len(keepers) == 1


def test_formats_share_zero_documents():
    """The pairwise overlap the plan measured at 91-93% must become exactly 0."""
    ids = _urn_ids(3_000)
    kept = {f: {i for i in ids if keeps_id(f, i)} for f in FINEPHRASE_FORMATS}
    assert sum(len(v) for v in kept.values()) == len(ids)  # partition covers everything
    for a_idx, a in enumerate(FINEPHRASE_FORMATS):
        for b in FINEPHRASE_FORMATS[a_idx + 1 :]:
            assert not (kept[a] & kept[b]), f"{a} and {b} still share documents"


# --------------------------------------------------------------------------------------
# Stability — the reason this is sha256 and not hash()
# --------------------------------------------------------------------------------------


def test_partition_is_stable_against_a_pinned_digest():
    """Recomputed from first principles, so this fails if the hash construction ever changes.

    A worker that partitioned differently from its peers would split the corpus inconsistently
    and every run would still look internally coherent.
    """
    for doc_id in _urn_ids(200):
        expected = int.from_bytes(hashlib.sha256(doc_id.encode("utf-8")).digest(), "big") % 4
        assert partition_of(doc_id) == expected


def test_partition_does_not_depend_on_python_hash_randomisation():
    """`hash()` is salted per process; this asserts we are not using it.

    Verified by value, not by reading the source: a pinned literal for a known id is what would
    actually break if someone swapped the construction for `hash()`.
    """
    doc_id = "<urn:uuid:e2300ad5-01dd-4e80-92b3-7ec88785cc9d>"  # a real shape, from §9.7 item 4
    digest = hashlib.sha256(doc_id.encode("utf-8")).digest()
    assert partition_of(doc_id) == int.from_bytes(digest, "big") % 4
    assert partition_of(doc_id) == partition_of(doc_id)  # idempotent within a process


# --------------------------------------------------------------------------------------
# Balance — the arithmetic the 15B-per-format draw rests on
# --------------------------------------------------------------------------------------


def test_partition_is_near_uniform_and_clears_the_worst_case_floor():
    """The design needs >= 17.3% per partition (the `table` config's requirement). Ideal is 25%.

    This is the check that would catch a construction that is stable but skewed.
    """
    audit = audit_partition(_urn_ids(40_000))
    assert audit.n_ids == 40_000
    assert audit.worst_deviation_pp < 1.0, audit.to_dict()
    assert audit.min_share_pct > 17.3, audit.to_dict()


def test_audit_enforces_its_floor_and_reports_the_shares():
    """A passing floor returns; a floor above the ideal share must raise, not warn."""
    ids = _urn_ids(5_000)
    audit_partition(ids, required_min_share_pct=17.3)  # passes
    with pytest.raises(IdPartitionError, match="does not fit"):
        audit_partition(ids, required_min_share_pct=30.0)  # impossible: ideal is 25%


def test_audit_deduplicates_before_counting():
    """The `raw_v0.1_parquet` two-document-tree trap has exactly this shape: an id read twice.

    Multiplicity says nothing about the corpus, so it must not weight a partition.
    """
    ids = _urn_ids(500)
    assert audit_partition(ids + ids).to_dict() == audit_partition(ids).to_dict()


# --------------------------------------------------------------------------------------
# Failing fixtures (CONTRIBUTING.md: every profile ships a passing AND a failing one)
# --------------------------------------------------------------------------------------


def test_empty_id_is_refused_rather_than_hashed():
    """`sha256(b"")` is a valid digest, so an empty id would silently land in one partition."""
    with pytest.raises(IdPartitionError, match="empty"):
        partition_of("")


def test_non_string_id_is_refused():
    with pytest.raises(IdPartitionError, match="must be str"):
        partition_of(12345)  # type: ignore[arg-type]


def test_unknown_format_raises_instead_of_dropping_every_row():
    """A typo'd config name returning False would ingest zero rows and report success."""
    with pytest.raises(IdPartitionError, match="unknown FinePhrase format"):
        keeps_id("tables", "<urn:uuid:e2300ad5-01dd-4e80-92b3-7ec88785cc9d>")


def test_reordering_the_format_tuple_would_reassign_documents():
    """Documents the hazard the FINEPHRASE_FORMATS comment warns about, by demonstrating it.

    Not a guard — nothing can stop an edit to a constant. This test exists so the consequence is
    visible in the suite rather than only in a comment.
    """
    doc_id = _urn_ids(1)[0]
    p = partition_of(doc_id)
    assert format_for_id(doc_id) == FINEPHRASE_FORMATS[p]
    rotated = FINEPHRASE_FORMATS[1:] + FINEPHRASE_FORMATS[:1]
    assert rotated[p] != FINEPHRASE_FORMATS[p]  # same document, different format
