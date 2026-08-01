"""Tests for the build-time contract.

The interesting tests here are the ones that assert a *cross-module* property — that a key built
by ``shard_key`` reads back through ``labels_from_path`` to the labels Gate A will recompute, and
that ordinal allocation cannot collide. Those are the two ways the build can be wrong in a manner
no single module's own tests would catch.
"""

from __future__ import annotations

import pytest

from edullm_data.corpus import (
    DTYPE_SIZE,
    FAMILY_MAX_EOS_FRACTION,
    GROUP,
    MIN_MEAN_DOC_TOKENS,
    SEQ_LEN,
    SHARD_TOKENS,
    VAL_FRACTION,
    BuildError,
    CorpusSpec,
    Document,
    ShardRef,
    allocate_ordinals,
    carve,
    epoch_verdict,
    epochs_for,
    is_held_out,
    shard_key,
)
from edullm_data.manifest import PATH_LABEL_KEYS, labels_from_path, parse_shard_name


# --------------------------------------------------------------------------------------
# Shard geometry
# --------------------------------------------------------------------------------------


def test_shard_tokens_is_a_whole_number_of_sequences():
    """The invariant ``check_seq_len_alignment`` enforces, asserted at build time.

    ``profiles/pretrain_tokens_v1.py:426`` recomputes ``bytes % (dtype_size * seq_len)`` from a
    real ``head`` and rejects a non-zero remainder. Asserting it here means a change to either
    constant fails in CI rather than after a corpus has been tokenized and uploaded.
    """
    assert SHARD_TOKENS % SEQ_LEN == 0
    assert SHARD_TOKENS // SEQ_LEN == 3052
    assert (SHARD_TOKENS * DTYPE_SIZE) % (DTYPE_SIZE * SEQ_LEN) == 0


def test_shard_bytes_is_a_single_part_copy():
    """~100 MB, far under the 5 GiB single-part copy threshold (``s3.py:98``).

    Above it, ``promote()`` needs multipart copies for every object; keeping shards small means
    the copy path stays the simple one for all ~10,400 of them.
    """
    assert SHARD_TOKENS * DTYPE_SIZE < 5 * 1024**3


# --------------------------------------------------------------------------------------
# The path IS the label
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,domain,expected",
    [
        ("dclm", None, {"source": "dclm"}),
        ("stackv2-edu", "python", {"source": "stackv2-edu", "domain": "python"}),
        ("synthetic-finephrase-faq", None, {"source": "synthetic-finephrase-faq"}),
    ],
)
def test_shard_key_round_trips_through_labels_from_path(source, domain, expected):
    """The labels Gate A recomputes must be the labels we intended.

    ``validate._check_labels_match_path`` derives labels from the key and compares by FULL DICT
    EQUALITY against what the manifest declares, so a key whose segments do not mean what the
    producer thought is rejected as ``labels-contradict-path``. This test is the producer half of
    that contract: it asserts the key we generate says what we mean.
    """
    assert labels_from_path(shard_key(source, domain, "train", 0)) == expected


def test_shard_key_parses_as_a_shard():
    """``parse_shard_name`` must recognise the name, or the split claim disappears.

    A name that fails ``SHARD_RE`` returns ``None``, and ``read.py:492`` then KEEPS the entry in
    a trainable read regardless of what it is called — so an unparseable ``val`` shard is
    silently trainable. The five-digit ordinal is what makes it parse.
    """
    assert parse_shard_name(shard_key("dclm", None, "val", 42)) == ("val", 42)


def test_domain_uses_the_second_label_key_not_a_third():
    """Two levels is the whole budget; a third RAISES rather than inventing ``level_3``."""
    assert PATH_LABEL_KEYS == ("source", "domain")
    deep = f"{GROUP}/a/b/c/train-00000.u32le.bin"
    with pytest.raises(ValueError, match="FLATTEN"):
        labels_from_path(deep)


def test_six_digit_ordinal_is_refused():
    """Refused at build time, because the failure downstream is silent.

    A six-digit name does not match ``SHARD_RE``, so ``parse_shard_name`` returns ``None`` and
    the object stops being recognised as belonging to any split — it is not an error anywhere,
    it just quietly leaves the split system.
    """
    with pytest.raises(BuildError, match="five digits"):
        shard_key("dclm", None, "train", 100_000)


# --------------------------------------------------------------------------------------
# Ordinal allocation
# --------------------------------------------------------------------------------------


def test_ordinals_are_unique_across_sources():
    """The collision this function exists to prevent.

    Verified by execution that the collision is NOT caught downstream: ``parse_shard_name``
    returns ``('train', 0)`` for both ``tokens/dclm/train-00000.u32le.bin`` and
    ``tokens/finewiki/train-00000.u32le.bin``, and no check in ``validate.py`` compares ordinals
    across sources. So per-source counters produce two distinct shards that share a name in
    every log and error message, and nothing objects.
    """
    refs = allocate_ordinals(
        [
            ("dclm", None, "train", 3),
            ("finewiki", None, "train", 2),
            ("stackv2-edu", "python", "train", 2),
        ]
    )
    train = [r.ordinal for r in refs if r.split == "train"]
    assert sorted(train) == list(range(7))
    assert len({r.path for r in refs}) == len(refs)


def test_splits_get_independent_ordinal_blocks():
    """``train`` and ``val`` each start at 0 — they are different name spaces."""
    refs = allocate_ordinals([("dclm", None, "train", 2), ("dclm", None, "val", 2)])
    assert sorted(r.ordinal for r in refs if r.split == "train") == [0, 1]
    assert sorted(r.ordinal for r in refs if r.split == "val") == [0, 1]


def test_allocation_is_independent_of_plan_order():
    """Same plan, any order, byte-identical keys — therefore the same ``manifest_sha256``.

    Without this, a rerun that enumerated sources in a different order would produce a different
    dataset IDENTITY from the same data, and the two would be indistinguishable except by
    comparing every key.
    """
    plan = [
        ("finewiki", None, "train", 2),
        ("dclm", None, "train", 3),
        ("stackv2-edu", "python", "train", 1),
    ]
    assert [r.path for r in allocate_ordinals(plan)] == [
        r.path for r in allocate_ordinals(list(reversed(plan)))
    ]


def test_duplicate_stream_in_plan_is_refused():
    with pytest.raises(BuildError, match="twice"):
        allocate_ordinals([("dclm", None, "train", 1), ("dclm", None, "train", 2)])


def test_shard_ref_path_matches_shard_key():
    ref = ShardRef(source="dclm", domain=None, split="train", ordinal=5)
    assert ref.path == shard_key("dclm", None, "train", 5)
    assert ref.tokens == SHARD_TOKENS


# --------------------------------------------------------------------------------------
# Held-out
# --------------------------------------------------------------------------------------


def test_held_out_is_deterministic():
    """No seed, no state, no ordering — so two workers cannot disagree.

    This is the structural property that makes the shipped leakage bug (six val shards that were
    byte-copies of train shards) impossible rather than merely unlikely.
    """
    assert is_held_out("doc-1", "dclm") == is_held_out("doc-1", "dclm")


def test_held_out_rate_is_close_to_the_fraction():
    n = 200_000
    got = sum(is_held_out(f"d{i}", "dclm") for i in range(n)) / n
    assert abs(got - VAL_FRACTION) < 0.0005


def test_held_out_differs_per_source():
    """A source-blind function would hold the same id out of every source, correlating val sets.

    Real lineage overlap makes a shared id possible (§3.1: FineWeb-Edu ⊂ score-2 ⊂ FineWeb), so
    the source is mixed into the hash.
    """
    ids = [f"d{i}" for i in range(20_000)]
    a = {i for i in ids if is_held_out(i, "dclm")}
    b = {i for i in ids if is_held_out(i, "finewiki")}
    assert a and b and a != b
    # Near-disjoint rather than merely unequal: overlap should be ~VAL_FRACTION of either set.
    assert len(a & b) < 0.2 * min(len(a), len(b))


def test_carve_tags_every_document_exactly_once():
    docs = [Document(id=f"d{i}", text="x", source="dclm") for i in range(5_000)]
    tagged = list(carve(docs))
    assert len(tagged) == len(docs)
    assert {t for t, _ in tagged} <= {"train", "val"}
    assert [d.id for _, d in tagged] == [d.id for d in docs]


def test_carve_train_and_val_are_disjoint_by_id():
    """The property the shipped bug violated, asserted directly."""
    docs = [Document(id=f"d{i}", text="x", source="dclm") for i in range(20_000)]
    tagged = list(carve(docs))
    train = {d.id for t, d in tagged if t == "train"}
    val = {d.id for t, d in tagged if t == "val"}
    assert not (train & val)
    assert train | val == {d.id for d in docs}


def test_zero_fraction_holds_nothing_out():
    assert not any(t == "val" for t, _ in carve(
        [Document(id=f"d{i}", text="x", source="s") for i in range(1000)], fraction=0.0
    ))


def test_smallest_pool_still_yields_a_whole_val_shard():
    """Reference is the smallest pool at 9B; 0.5% of it must exceed one shard.

    A source whose val carve is smaller than one shard produces NO val shard for it, so a
    per-source held-out set silently does not exist for that source.
    """
    assert (9_000_000_000 * VAL_FRACTION) / SHARD_TOKENS > 1.0


# --------------------------------------------------------------------------------------
# The EOS-fraction floor
# --------------------------------------------------------------------------------------


def test_min_mean_doc_tokens_is_the_eos_bound_inverted():
    """``MIN_MEAN_DOC_TOKENS`` is derived, not chosen: it is ``1 / eos_fraction_max``.

    One EOS per document means a packed shard's EOS fraction is ``1 / mean_doc_tokens``, so the
    family's 0.05 ceiling IS a 20-token floor on mean document length. Asserting the identity
    keeps the two numbers from drifting apart.
    """
    assert MIN_MEAN_DOC_TOKENS == pytest.approx(1.0 / FAMILY_MAX_EOS_FRACTION)
    assert 1.0 / MIN_MEAN_DOC_TOKENS == pytest.approx(FAMILY_MAX_EOS_FRACTION)
    # And the direction of the failure: shorter documents mean MORE EOS.
    assert 1.0 / 16 > FAMILY_MAX_EOS_FRACTION  # a 16-token mean is rejected
    assert 1.0 / 64 < FAMILY_MAX_EOS_FRACTION  # the per-document floor has margin


def test_family_bound_is_tighter_than_the_profile_default():
    """``families/pretrain.json`` binds; the profile constant is the laxer fallback.

    ``profiles.base._bound`` only ever tightens, so the family's 0.05 wins wherever
    ``families/`` resolves — and where it does NOT, the corpus is silently validated at the
    profile's 0.5, which is how the live corpus once shipped (``CLAUDE.md`` gotcha 2).
    """
    from edullm_data.profiles.pretrain_tokens_v1 import _DEFAULT_MAX_EOS_FRACTION

    assert FAMILY_MAX_EOS_FRACTION < _DEFAULT_MAX_EOS_FRACTION


# --------------------------------------------------------------------------------------
# Document / CorpusSpec validation
# --------------------------------------------------------------------------------------


def test_document_requires_an_id():
    with pytest.raises(BuildError, match="join key"):
        Document(id="", text="x", source="dclm")


def test_document_requires_a_source():
    with pytest.raises(BuildError, match="path segment"):
        Document(id="d1", text="x", source="")


def _spec(**over):
    base = dict(
        key="dclm",
        category="web-diverse",
        source_label="dclm",
        repo="mlfoundations/dclm-baseline-1.0",
        file_format="parquet",
        text_column="text",
        id_column="id",
        target_tokens=30_000_000_000,
    )
    base.update(over)
    return CorpusSpec(**base)


def test_spec_rejects_an_unsafe_source_label():
    """``C#`` in a key silently truncates the URI at the ``#``.

    Nothing downstream catches it: ``labels_from_path`` accepts it and ``fnmatch`` matches it,
    so it breaks in a consumer, on data that is frozen by then.
    """
    with pytest.raises(BuildError, match="safe path segment"):
        _spec(source_label="C#")
    with pytest.raises(BuildError, match="safe path segment"):
        _spec(source_label="Jupyter Notebook")


def test_spec_rejects_a_target_larger_than_the_pool():
    with pytest.raises(BuildError, match="epoch guard"):
        _spec(pool_tokens=1_000_000_000, target_tokens=30_000_000_000)


def test_spec_accepts_an_unmeasured_pool():
    """``pool_tokens=None`` is legal — better than a card figure, which would look like evidence."""
    assert _spec(pool_tokens=None).pool_tokens is None


# --------------------------------------------------------------------------------------
# The epoch guard
# --------------------------------------------------------------------------------------


def test_a_five_billion_token_source_at_full_weight_is_exactly_four_epochs():
    """§4.3's worked example, which is the boundary of green."""
    assert epochs_for(20_000_000_000, 1.0, 5_000_000_000) == 4.0
    assert epoch_verdict(4.0) == "green"
    assert epoch_verdict(4.01) == "amber"


def test_design_pool_sizes_are_all_green():
    """Every §2.1 category at its default weight lands deep green.

    The guard is not for the default mix — it is for the teammate who narrows a run to one small
    source. This test records that the default is nowhere near the boundary.
    """
    pools = {
        "edu-web": (0.40, 48e9),
        "web-diverse": (0.15, 30e9),
        "code": (0.12, 40e9),
        "synthetic": (0.10, 60e9),
        "math": (0.08, 36e9),
        "academic": (0.07, 20e9),
        "reference": (0.05, 9e9),
        "qa-forum": (0.03, 12e9),
    }
    for name, (ratio, pool) in pools.items():
        e = epochs_for(20_000_000_000, ratio, pool)
        assert epoch_verdict(e) == "green", f"{name} at {e:.2f} epochs"
        assert e < 1.0, f"{name} repeats data at the default mix: {e:.2f}"


@pytest.mark.parametrize(
    "epochs,verdict",
    [(0.5, "green"), (4.0, "green"), (10.0, "amber"), (20.0, "red"), (41.0, "fail")],
)
def test_epoch_verdict_boundaries(epochs, verdict):
    assert epoch_verdict(epochs) == verdict


def test_epochs_refuses_an_empty_pool():
    with pytest.raises(BuildError):
        epochs_for(20_000_000_000, 1.0, 0)
