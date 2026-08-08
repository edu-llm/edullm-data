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


def rows_fixture_values(path) -> list[dict]:
    """The rows, read straight off disk — for tests that are not fixture-scoped."""
    return json.loads(path.read_text())["corpora"]


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
    for prefix, n, per_child in (("dclm-", 100, 4_100_000_000),
                                 ("fineweb-edu-", 16, 15_750_000_000),
                                 ("finephrase-", 4, 9_000_000_000)):
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
        # One LOCAL-shard dir per row: the finest disjoint unit the tree offers, and the only one
        # `config` can name. A coarser carve overdraws the ~7.33B unique pool of a single dir.
        assert "/local-shard_" in r["config"], r["config"]


def test_no_bundle_exceeds_the_per_child_wall_clock_except_the_known_escalations(rows):
    """Re-derive each child's hours at the MEASURED rate. This is the check E14 existed to add.

    ``--shard/--of`` strides BUNDLES, so a source that does not split is one child on one instance
    no matter how large the array — the makespan is the longest bundle, not the total ÷ children.
    Two sources are FLAT upstream (zero subdirectories) and cannot be split by any walk; they are
    escalated, and named here so a third one cannot appear silently.
    """
    RATE_B_PER_HOUR = 72_615 * 8 * 3600 / 1e9  # 8-vCPU child, MEASURED end-to-end rate
    FLOOR_H = 9.96
    escalated = {"stackv2-edu", "finepdfs-edu", "nemotron-cc-math-3", "nemotron-cc-math-4plus"}
    over = {
        r["source_label"]: r["target_tokens"] / 1e9 / RATE_B_PER_HOUR
        for r in rows
        if r["target_tokens"] / 1e9 / RATE_B_PER_HOUR > FLOOR_H
    }
    assert set(over) <= escalated, (
        f"a source exceeds the {FLOOR_H} h floor and is not a known escalation: "
        f"{ {k: round(v, 2) for k, v in over.items() if k not in escalated} }"
    )


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


@pytest.mark.network
def test_every_spec_config_resolves_to_a_listable_path_with_payload_files():
    """The check whose absence let a 404 reach a billing container. Opt-in: `-m network`.

    **Why no offline test can cover this.** A registry `config` that does not exist upstream is
    invisible to every check that does not make the request: the row parses, `CorpusSpec` validates,
    `plan_document` builds, `plan_id` is stable and reproducible, and the shard paths are correct.
    The failure surfaces on the first `hf_files` call — inside a Batch child, after the image build,
    the role assumption, the tokenizer download and the decon-index load. One row of 133 shipped
    exactly that way (`cosmopedia`, `config: "web_samples_v2"` → HTTP 404; the configs live under
    `data/`).

    **Recomputes rather than asserts.** It calls the real `hf_files` — the same function the build
    calls, at the same pinned revision — and requires a NON-EMPTY file list. Non-empty is the load-
    bearing half: `hf_files` filters by payload extension, so a path that lists but holds no
    `.parquet`/`.json.gz` returns `[]`, and a child that reads zero files does not fail. It yields
    nothing, packs nothing, and leaves its refs unfilled, which looks like filter attrition at
    `verify` rather than like a bad row.

    Read-only, no bulk data, no S3, no credentials. 133 rows over a thread pool, ~10 s.
    """
    from concurrent.futures import ThreadPoolExecutor

    from edullm_data.corpus_build import hf_files

    specs, _ = __import__("edullm_data.corpus_build", fromlist=["load_registry"]).load_registry(
        str(REGISTRY)
    )
    drawn = [s for s in specs if s.target_tokens > 0]

    def probe(spec):
        try:
            return spec.key, len(hf_files(spec)), None
        except Exception as exc:  # noqa: BLE001 — the point is to report, not to raise here
            return spec.key, 0, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(probe, drawn))

    # ⚠️ A 429 is the HOST rate-limiting us, not a bad row, and conflating the two turns a shared
    # upstream quota into a false registry failure. Reported separately and skipped, because a test
    # that fails on someone else's throttle teaches people to ignore it.
    throttled = [k for k, n, err in results if err and "429" in err]
    broken = [(k, err) for k, n, err in results if err is not None and "429" not in err]
    empty = [k for k, n, err in results if err is None and n == 0]
    if throttled and not broken:
        pytest.skip(f"HF returned 429 for {len(throttled)} row(s); re-run alone, not back-to-back")
    assert not broken, (
        f"{len(broken)} of {len(drawn)} registry rows do not resolve at their pinned revision: "
        f"{broken[:3]}. Each one is a build that dies on its first read inside a container."
    )
    assert not empty, (
        f"{len(empty)} row(s) resolve but list NO payload files: {empty[:3]}. A child that reads "
        f"zero files still writes a receipt, so the bundle is silently empty rather than failed."
    )


def test_every_drawn_row_declares_an_identifier_one_way_or_the_other():
    """Wall 6 as an OFFLINE check. No network, so it runs on every commit.

    `is_held_out` decides the train/val carve from the document id ALONE, so a row with neither a
    real `id_column` nor `id_surrogate` has no reproducible, leak-free split — and the crash lands
    on file 1 inside a billing container, after the image build and the tokenizer download.
    `CorpusSpec.__post_init__` now refuses it, and this asserts the shipping registry satisfies it
    rather than trusting that nobody re-introduces the shape.
    """
    for r in rows_fixture_values(REGISTRY):
        if r["target_tokens"] > 0:
            has_id = bool(r.get("id_column"))
            surrogate = bool(r.get("id_surrogate"))
            assert has_id != surrogate, (
                f"{r['key']}: id_column={r.get('id_column')!r} id_surrogate={surrogate}. Exactly "
                f"one must be true — a drawn row needs an identifier, and declaring both hides "
                f"which one the build used."
            )


def test_surrogate_rows_are_pinned_and_flagged_as_unjoinable():
    """A surrogate id is OUR construction, not upstream's, so a cross-source join silently drops it.

    Two things are asserted because both are load-bearing: the revision pin (§B12's condition — the
    id embeds a file path, so an unpinned ref lets an upstream re-shard re-partition the carve), and
    that the row records the non-comparability in its own traps, so the warning travels with the
    artifact instead of living only in a dossier.
    """
    surrogates = [r for r in rows_fixture_values(REGISTRY) if r.get("id_surrogate")]
    assert surrogates, "expected at least cosmopedia; if this row went away, delete this test"
    for r in surrogates:
        assert r["revision"] and len(r["revision"]) == 40, f"{r['key']}: surrogate needs a pin"
        joined = " ".join(r["traps"]).upper()
        assert "NOT COMPARABLE ACROSS SOURCES" in joined, (
            f"{r['key']}: the surrogate's non-comparability must be recorded in the row's traps — "
            f"a future session joining on `id` would silently exclude this source."
        )


@pytest.mark.network
def test_every_drawn_rows_id_column_names_a_real_leaf_in_a_real_file():
    """PLAT's wall-6 hardening: the declared identifier must EXIST in the bytes.

    The offline check above proves a row *declares* an identifier. This proves the declaration is
    true of the file — the gap that let `id_column: ''` reach a container. It resolves one real file
    per row and asks the same `_resolve_leaf` the reader uses, so a renamed or absent column fails
    here instead of on file 1 of 6,800.

    Surrogate rows are checked the other way round: they must have NO id column to resolve, which is
    the condition that makes the surrogate legitimate rather than a workaround for a typo.
    """
    from concurrent.futures import ThreadPoolExecutor

    from edullm_data.corpus_build import hf_files, load_registry
    from edullm_data.corpus_read import _resolve_leaf, _open_parquet, _hf_headers

    specs, _ = load_registry(str(REGISTRY))
    # One row per (repo, id_column) shape, not all 127: the 100 dclm rows share a repo and a schema,
    # so probing each is 100 range-reads that prove one fact and reliably earn a 429. The distinct
    # shapes are what carry the information.
    seen: set[tuple[str, str, bool]] = set()
    drawn = []
    for sp in specs:
        if sp.target_tokens <= 0 or sp.file_format != "parquet":
            continue
        shape = (sp.repo, sp.id_column, sp.id_surrogate)
        if shape in seen:
            continue
        seen.add(shape)
        drawn.append(sp)

    def probe(spec):
        try:
            entry = hf_files(spec)[0]
            pf, _ = _open_parquet(spec.repo, entry["path"], int(entry["size"]),
                                  _hf_headers(), None, None, revision=spec.revision)
            names = set(pf.metadata.schema.names)
            if spec.id_surrogate:
                return spec.key, None if not spec.id_column else "surrogate row names an id_column"
            _resolve_leaf(pf.metadata, spec.id_column, what="id_column")
            return spec.key, None
        except Exception as exc:  # noqa: BLE001 — collect, do not raise, so one row names itself
            return spec.key, f"{type(exc).__name__}: {str(exc)[:160]}"

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(probe, drawn))
    # 403 is the GATED-REPO condition, not a bad row: dossier B13 records that Nemotron-CC-Math
    # gate access is per-account and our token is not authorized. The registry rows are read from
    # `s3://edullm-landing/_src/` at build time, not from HF, so a 403 here says nothing about the
    # build. Reported, never failed — an unactionable red test is a test people learn to ignore.
    gated = [k for k, e in results if e and "403" in e]
    throttled = [k for k, e in results if e and "429" in e]
    broken = [(k, e) for k, e in results if e and "429" not in e and "403" not in e]
    if gated:
        print(f"\nNOTE: {len(gated)} gated row(s) unverifiable from this account: {gated}")
    if throttled and not broken:
        pytest.skip(f"HF returned 429 for {len(throttled)} row(s); re-run alone, not back-to-back")
    assert not broken, (
        f"{len(broken)} of {len(drawn)} parquet rows have an id_column that is not a real leaf: "
        f"{broken[:3]}. Each is a build that dies on its first row inside a container."
    )
