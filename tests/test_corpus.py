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


#: MEASURED Gate A cost per object: 10,049 objects in ~85 min, 8 round trips each, recorded at
#: ``profiles/pretrain_tokens_v1.py:205-210``. Latency-bound (0.3% CPU), so it scales with objects.
_GATE_A_SECONDS_PER_OBJECT = 85 * 60 / 10_049

#: ``edullm-validator`` rev 14, MEASURED 2026-08-08 via ``batch describe-job-definitions``.
_VALIDATOR_TIMEOUT_S = 14_400


def test_gate_a_at_1T_does_NOT_fit_the_validator_timeout_at_this_shard_size():
    """The shard-size docstring's second constraint, RECOMPUTED — and it fails.

    This is deliberately a test that pins a *known shortfall* rather than a passing property. The
    constant's comment used to justify itself with "~10,400 objects fits the 7200 s timeout", and
    both premises silently moved: the live timeout is 14,400 s and 1.0T gives ~40,000 objects, not
    10,400. The arithmetic below is what makes that visible in CI instead of in a doc nobody
    re-derives.

    The value stays because task #10 (thread the profile checks) is the right lever, not a smaller
    shard. **If #10 lands, this test should start failing and be replaced by the threaded bound.**
    """
    objects_1t = round(1.0e12 / SHARD_TOKENS)
    assert 39_000 <= objects_1t <= 41_000, f"1.0T gives {objects_1t:,} objects, not ~40,000"

    serial_s = objects_1t * _GATE_A_SECONDS_PER_OBJECT
    assert serial_s > _VALIDATOR_TIMEOUT_S, (
        "if this now passes, Gate A got cheaper or the timeout rose — re-derive the constant's "
        "docstring rather than deleting the test"
    )
    # `--head-workers 16` threads exactly 1 of the 8 calls, so Amdahl caps the gain at 7/8.
    assert serial_s * (7 / 8 + (1 / 8) / 16) > _VALIDATOR_TIMEOUT_S, (
        "head_workers alone must not be mistaken for a fix: it threads 1 of 8 calls"
    )

    # The largest corpus that DOES fit at this shard size, so the shortfall has a number.
    break_even_objects = int(_VALIDATOR_TIMEOUT_S / _GATE_A_SECONDS_PER_OBJECT)
    assert 700e9 < break_even_objects * SHARD_TOKENS < 720e9

    # And the reservoir, which is what the original justification was actually measured on, fits.
    assert round(252.6e9 / SHARD_TOKENS) * _GATE_A_SECONDS_PER_OBJECT < _VALIDATOR_TIMEOUT_S


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


# --------------------------------------------------------------------------------------
# The source registry — a data file that must stay loadable by the code that consumes it
# --------------------------------------------------------------------------------------


def _registry():
    import json
    import pathlib

    p = pathlib.Path(__file__).resolve().parents[1] / "artifacts" / "reservoir" / "corpus-registry.json"
    if not p.exists():
        import pytest

        pytest.skip("corpus-registry.json not generated")
    return json.loads(p.read_text())


def test_every_registry_row_loads_into_a_corpus_spec():
    """The registry is a build INPUT, so a row the reader cannot construct is a broken build.

    Catching it here costs nothing; catching it on Batch costs a job. This also pins the field names
    against the dataclass, which is the thing most likely to drift when either side is edited.
    """
    rows = _registry()["corpora"]
    assert rows, "an empty registry would make this vacuous"
    for row in rows:
        CorpusSpec(**row)  # __post_init__ does the real checking


def test_registry_source_labels_are_unique_and_safe():
    """Two sources sharing a label would silently merge into one slice of the corpus."""
    from edullm_data.manifest import SAFE_SEGMENT_RE

    labels = [r["source_label"] for r in _registry()["corpora"]]
    assert len(set(labels)) == len(labels), "duplicate source_label merges two sources"
    for label in labels:
        assert SAFE_SEGMENT_RE.match(label), f"{label!r} is not a safe path segment"


def test_an_unverified_text_column_carries_the_command_to_settle_it():
    """`UNVERIFIED` must be actionable, not just honest.

    Getting the text column wrong is the one silent failure in the whole build — FinePhrase's
    top-level `text` is the ORIGINAL document — so a row admitting it is unverified has to say how to
    check.
    """
    for row in _registry()["corpora"]:
        if row["text_column"] == "UNVERIFIED":
            assert row["traps"], f"{row['key']}: unverified with no trap explaining how to settle it"
            assert "path_in_schema" in row["traps"][0], row["key"]


def test_the_finephrase_rows_name_the_nested_rewrite_not_the_original():
    """The trap that no hash, size or decode check would catch.

    Verified against real bytes elsewhere; asserted here so a registry edit cannot quietly point the
    synthetic half at unrephrased FineWeb-Edu.
    """
    fp = [r for r in _registry()["corpora"] if r["key"].startswith("finephrase-")]
    assert len(fp) == 4, "four formats, separately weightable (§3.3)"
    for row in fp:
        assert row["text_column"] == "rollout_results.list.element.text", row["key"]


def test_no_row_targets_more_than_its_measured_pool():
    """Drawing more than a pool holds means repeating documents.

    `CorpusSpec.__post_init__` enforces this per row; this states it as a property of the registry so
    the failure names the registry rather than a constructor.
    """
    for row in _registry()["corpora"]:
        pool = row.get("pool_tokens")
        if pool is not None:
            assert row["target_tokens"] <= pool, row["key"]


def test_category_pools_report_overlap_adjusted_totals():
    """Where sources are not independent, the naive sum must not be the only number.

    §3.1's lineage trap: summing peS2o and pubmed double-counts peS2o's measured 49.7% PMC share. A
    registry reporting only the sum would overstate the academic pool by 20.1B.
    """
    cats = _registry()["categories"]
    for name, entry in cats.items():
        assert entry["non_overlapping_pool_tokens"] <= entry["naive_sum_pool_tokens"], name
        if entry["non_overlapping_pool_tokens"] < entry["naive_sum_pool_tokens"]:
            assert "_pool_note" in entry, f"{name} adjusts its pool without saying why"


def test_the_registry_covers_every_design_category():
    """A missing category is a silently absent slice of the corpus."""
    expected = {
        "edu-web", "web-diverse", "code", "synthetic",
        "math", "academic", "reference", "qa-forum",
    }
    assert set(_registry()["categories"]) == expected


def test_every_registry_row_pins_a_revision():
    """`resolve/main` follows a branch; a pin is what makes the build reproducible.

    Without this, a corpus re-downloaded a month later can return different bytes under the same
    name and NOTHING downstream notices: the manifest hashes whatever arrived, Gate A passes it, and
    "the dataset built from fineweb-edu" quietly means two different things across two runs.
    """
    import re

    for row in _registry()["corpora"]:
        rev = row.get("revision")
        assert rev, f"{row['key']}: no pinned revision"
        assert re.fullmatch(r"[0-9a-f]{40}", rev), f"{row['key']}: {rev!r} is not a 40-hex sha"


def test_rows_sharing_a_repo_share_its_revision():
    """One repo, one pin. The four FinePhrase configs and the three Common Pile subsets each live in
    a single repo, so two different shas for one repo would mean the build read two states of it."""
    by_repo: dict[str, set] = {}
    for row in _registry()["corpora"]:
        by_repo.setdefault(row["repo"], set()).add(row["revision"])
    for repo, revs in by_repo.items():
        assert len(revs) == 1, f"{repo} pinned at {len(revs)} different revisions: {sorted(revs)}"


def test_every_drawn_source_has_a_verified_text_column():
    """`UNVERIFIED` on a DRAWN row means the build would read the wrong bytes or none at all.

    All 14 drawn sources were confirmed by reading their first record at the pinned revision on
    2026-08-01. Reserve rows may still be unverified — they are not read.
    """
    for row in _registry()["corpora"]:
        if row["target_tokens"] > 0:
            assert row["text_column"] != "UNVERIFIED", f"{row['key']}: text_column unverified"
            assert row["id_column"] != "UNVERIFIED", f"{row['key']}: id_column unverified"


def test_finemath_joins_on_url_because_it_has_no_id_column():
    """Measured: 16 leaves and none of them an id.

    Recorded as a test because the id is the join key for the §9.7 item 4 partition and the
    FineWeb-Edu anti-join — silently substituting a row index would make both non-reproducible
    across a re-download.
    """
    fm = [r for r in _registry()["corpora"] if r["key"] == "finemath"][0]
    assert fm["id_column"] == "url"
