"""The 1.0T `final-dataset` registry, re-derived from its own bytes.

**Why these tests recompute rather than assert.** This registry is a DATA artifact, and a test that
only proves it parses is the decoration this repo's golden rule exists to reject: a corrupted mix
parses perfectly. Every check here re-derives a quantity from the rows and compares it to the number
the plan committed to, so a hand-edit that changes the corpus silently fails the suite.

The prefix-collision check is the one with a live incident behind it. `SAFE_SEGMENT_RE`
(`manifest.py`, called from `CorpusSpec.__post_init__`) validates characters WITHIN one segment and
is **structurally incapable** of comparing two labels — so `--prefix tokens/nemotron-` would sweep in
`math-textbooks`, a CC-BY-4.0 source explicitly carved OUT of the NVIDIA Data Agreement. The regex
cannot catch that class; this test is the only thing that can.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from edullm_data.corpus import CorpusSpec

REGISTRY = Path(__file__).resolve().parents[1] / "artifacts" / "final-dataset" / "corpus-registry.json"

#: The report's 1,000B nominal less the 14B dolma3-QA row struck by CEO ruling 2026-08-08.
EXPECTED_TOTAL_TOKENS = 986_000_000_000

#: Under-1-epoch is the corpus's whole no-repetition claim. FinePDFs-Edu at 0.90 is the max.
MAX_EPOCHS = 0.99


@pytest.fixture(scope="module")
def doc() -> dict:
    return json.loads(REGISTRY.read_text())


@pytest.fixture(scope="module")
def rows(doc) -> list[dict]:
    return doc["corpora"]


def test_every_row_constructs_a_real_CorpusSpec(rows):
    """The registry is loaded straight into ``CorpusSpec(**row)``, so its validation is ours."""
    specs = [CorpusSpec(**row) for row in rows]
    assert len(specs) == len(rows)


def test_the_summed_target_recomputes_to_the_committed_total(rows):
    """Sum the rows; do not trust the header.

    A per-row edit that changes the corpus size is invisible in review and fatal at FREEZE — the
    mix has to be final before the first token is written, because inserting a source renames 98%
    of shards.
    """
    total = sum(r["target_tokens"] for r in rows)
    assert total == EXPECTED_TOTAL_TOKENS, (
        f"summed target_tokens is {total:,}, expected {EXPECTED_TOTAL_TOKENS:,}. "
        f"The registry IS the mix; a changed sum is a changed corpus."
    )
    # And the header must agree with the rows it claims to describe.
    assert json.loads(REGISTRY.read_text())["_total_target_tokens"] == total


def test_no_source_label_is_a_prefix_of_another(rows):
    """A prefix collision is a LICENCE-BOUNDARY failure, not a naming preference.

    ``--prefix tokens/nemotron-`` must not reach ``math-textbooks``: it is CC-BY-4.0 and carved out
    of the NVIDIA Data Agreement that binds ``nemotron-cc-math-*``. Recomputed over every ordered
    pair, because the regex that validates a label cannot compare two of them.
    """
    labels = [r["source_label"] for r in rows]
    collisions = [
        (a, b) for a in labels for b in labels if a != b and b.startswith(a)
    ]
    assert not collisions, (
        f"prefix collisions: {collisions}. A prefix operation over one label would sweep in the "
        f"other, and source_label is inside manifest_sha256 — unbackfillable."
    )


def test_labels_and_keys_are_unique(rows):
    """Two rows sharing a label silently LOSE the first row's tokens (measured at 33.3%)."""
    for field in ("source_label", "key"):
        values = [r[field] for r in rows]
        assert len(values) == len(set(values)), f"duplicate {field}"


def test_every_pinned_revision_is_a_40_char_sha(rows):
    """An unpinned revision resolves to whatever ``main`` holds on the morning of the build."""
    for r in rows:
        rev = r["revision"]
        assert rev is not None and len(rev) == 40 and all(
            c in "0123456789abcdef" for c in rev
        ), f"{r['key']}: revision {rev!r} is not a 40-char hex sha"


def test_no_row_draws_more_than_its_pool_holds(rows):
    """Drawing past the pool means repeating documents, which the epoch guard exists to flag."""
    for r in rows:
        pool = r["pool_tokens"]
        if pool is not None:
            assert pool >= r["target_tokens"], (
                f"{r['key']}: draws {r['target_tokens']:,} from a {pool:,} pool"
            )


def test_the_epoch_table_recomputes_and_nothing_reaches_one_epoch(rows):
    """Re-derive epochs per row. The under-1-epoch property is the no-repetition claim.

    Recomputed rather than quoted because the report's two epoch columns are PER STAGE and are
    never summed there — a source drawn in both stages has a true exposure neither column shows.
    These rows carry the combined draw, so this division is the real number.
    """
    worst, worst_key = 0.0, None
    for r in rows:
        pool, target = r["pool_tokens"], r["target_tokens"]
        if pool and target:
            epochs = target / pool
            if epochs > worst:
                worst, worst_key = epochs, r["key"]
    assert worst < MAX_EPOCHS, f"{worst_key} at {worst:.3f} epochs breaks the under-1-epoch claim"


def test_the_two_split_families_have_the_right_child_count_and_even_targets(rows):
    """A split family's children must be evenly targeted: ``_shard_slice`` strides, it does not balance.

    Uneven children leave the largest one as the wall clock, which is the entire reason the split
    exists.
    """
    for prefix, n, per_child in (("dclm-", 10, 41_000_000_000),
                                 ("fineweb-edu-", 16, 15_750_000_000)):
        family = [r for r in rows if r["source_label"].startswith(prefix)]
        assert len(family) == n, f"{prefix}: {len(family)} children, expected {n}"
        assert {r["target_tokens"] for r in family} == {per_child}
        # Each child must name a DISTINCT subdirectory, or N children read the same files and the
        # duplicate documents are real text that hashes and decodes fine.
        configs = [r["config"] for r in family]
        assert len(set(configs)) == n, f"{prefix}: children share a config"


def test_the_dclm_children_name_the_four_level_prefix(rows):
    """A bare ``global-shard_NN_of_10`` config is a hard HTTP 404 (dossier B7)."""
    for r in (r for r in rows if r["source_label"].startswith("dclm-")):
        assert r["config"].startswith("filtered/OH_eli5"), r["config"]
        assert "/processed_data/global-shard_" in r["config"], r["config"]


def test_fineweb_edu_draws_from_data_not_a_sample(rows):
    """``sample/350BT`` satisfies the size requirement while SILENTLY breaking the anti-join.

    MEASURED in dossier B19: the same FinePhrase file scores 0 hits across three ``sample/350BT``
    files and 2,085 in ONE ``data/CC-MAIN-2013-20`` file.
    """
    for r in (r for r in rows if r["source_label"].startswith("fineweb-edu-")):
        assert r["config"].startswith("data/CC-MAIN-"), r["config"]
        assert "sample" not in r["config"]


def test_the_source_priority_list_covers_every_row_exactly_once(doc, rows):
    """The priority list decides which source WINS a cross-source duplicate.

    A row missing from it falls back to an undefined order, which is the alphabetical accident this
    list exists to replace.
    """
    priority = doc["_source_priority"]
    assert len(priority) == len(set(priority)), "duplicate entries in _source_priority"
    assert set(priority) == {r["source_label"] for r in rows}, (
        "the priority list and the rows disagree"
    )
    # Bulk web must lose to everything: it is the least curated source in the mix.
    assert priority[-1].startswith("dclm-"), "bulk web should rank last"


def test_every_format_declared_has_a_reader(rows):
    """Recomputed against the live reader registry, not a hardcoded set.

    A fourth copy of the format list here would be exactly the divergence stream 10 removed.
    """
    from edullm_data.corpus_read import _READERS

    for r in rows:
        assert r["file_format"] in _READERS, (
            f"{r['key']}: no reader for {r['file_format']!r}"
        )


def test_the_restricted_licence_is_confined_to_the_labels_that_carry_it(rows):
    """Only the two CC-Math tiers may carry the NVIDIA Data Agreement.

    If a third row ever acquires it, an operator enumerating restricted objects by prefix would
    miss it — the address shape IS the enumeration.
    """
    restricted = {r["source_label"] for r in rows if r["license"].startswith("NVIDIA Data Agreement")}
    assert restricted == {"nemotron-cc-math-3", "nemotron-cc-math-4plus"}
    for label in restricted:
        assert label.startswith("nemotron-cc-math")


def test_rows_that_cannot_build_yet_are_visible_as_such(rows):
    """Cosmopedia has NO id column in any config; the reader raises on a null id.

    Recorded as an empty ``id_column`` with a trap rather than a plausible guess, so the blocker is
    discoverable from the artifact instead of at run time inside a billing container.
    """
    cosmo = next(r for r in rows if r["key"] == "cosmopedia")
    assert cosmo["id_column"] == ""
    assert any("NO ID COLUMN" in t for t in cosmo["traps"])


def test_every_row_carries_its_traps(rows):
    """A trap is how a measured hazard survives into the next session."""
    for r in rows:
        if r["target_tokens"] > 0 and not r["source_label"].startswith(("dclm-", "fineweb-edu-")):
            assert r["traps"], f"{r['key']} records no traps"
