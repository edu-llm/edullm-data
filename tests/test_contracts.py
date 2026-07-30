"""Tests for edullm_data.contracts — canonical JSON, hashing, §2 naming, §3 version."""

from __future__ import annotations

import hashlib
import json

import pytest

from edullm_data.contracts import (
    FAMILIES,
    RELATIONS,
    READABLE_SCHEMA_VERSIONS,
    SCHEMA_VERSION,
    NamingError,
    Version,
    canonical_json,
    sha256_bytes,
    sha256_file,
    validate_dataset_id,
    validate_name,
    validate_purpose,
)

# ======================================================================================
# canonical_json
# ======================================================================================


def test_canonical_json_is_exactly_the_documented_form():
    """The hash chain is defined over this byte string; pin it literally."""
    value = {"b": 1, "a": 2}
    assert canonical_json(value) == b'{"a":2,"b":1}'
    assert canonical_json(value) == json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def test_canonical_json_is_insertion_order_independent():
    """Two dicts with the same content must hash identically regardless of build order."""
    a = {"z": 1, "m": {"y": 2, "x": 3}, "a": [1, 2, 3]}
    b = {"a": [1, 2, 3], "m": {"x": 3, "y": 2}, "z": 1}
    assert a != list(b)  # sanity: different insertion order
    assert list(a.keys()) != list(b.keys())
    assert canonical_json(a) == canonical_json(b)
    assert sha256_bytes(canonical_json(a)) == sha256_bytes(canonical_json(b))


def test_canonical_json_sorts_keys_recursively():
    out = canonical_json({"outer": {"b": 1, "a": {"d": 4, "c": 3}}})
    assert out == b'{"outer":{"a":{"c":3,"d":4},"b":1}}'


def test_canonical_json_has_no_incidental_whitespace():
    out = canonical_json({"a": [1, 2], "b": {"c": 3}})
    assert b" " not in out
    assert out == b'{"a":[1,2],"b":{"c":3}}'


def test_canonical_json_emits_real_utf8_not_escapes():
    """ensure_ascii=False: bytes are UTF-8, not \\uXXXX."""
    out = canonical_json({"note": "café — naïve"})
    assert "café — naïve".encode("utf-8") in out
    assert b"\\u" not in out
    assert out.decode("utf-8") == '{"note":"café — naïve"}'


def test_canonical_json_rejects_nan_and_infinity():
    """allow_nan=False: NaN/Infinity are not JSON, so fail loudly."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            canonical_json({"x": bad})


def test_canonical_json_byte_stability_across_repeated_calls():
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "group": "tokens",
        "entries": [{"path": "tokens/train-00000.u32le.bin", "bytes": 4}],
        "objects": 1,
        "bytes": 4,
    }
    digests = {sha256_bytes(canonical_json(manifest)) for _ in range(50)}
    assert len(digests) == 1


def test_canonical_json_list_order_is_significant():
    """Lists are ordered data; only *keys* get sorted."""
    assert canonical_json([1, 2]) != canonical_json([2, 1])


def test_schema_version_constant():
    """New artifacts are written at v2 (adds optional entry.split / entry.labels)."""
    assert SCHEMA_VERSION == "edullm-dataset/v2"


def test_v1_stays_readable_after_the_bump():
    """A published dataset is frozen at the version it was sealed with.

    Gate A re-runs against published datasets (the in-place README backfill did exactly that),
    so dropping v1 would retroactively invalidate every existing dataset. Removing a version
    from this set is a breaking change to the standard, and this test is where that decision
    has to be made on purpose.
    """
    assert READABLE_SCHEMA_VERSIONS == {"edullm-dataset/v1", "edullm-dataset/v2"}
    assert SCHEMA_VERSION in READABLE_SCHEMA_VERSIONS


# ======================================================================================
# hashing
# ======================================================================================


def test_sha256_bytes_matches_hashlib():
    assert sha256_bytes(b"") == hashlib.sha256(b"").hexdigest()
    assert sha256_bytes(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_sha256_file_returns_digest_and_size(tmp_path):
    payload = b"the .npy suffix does not imply a NumPy header\n" * 1000
    target = tmp_path / "notes.txt"
    target.write_bytes(payload)

    digest, size = sha256_file(target)
    assert digest == hashlib.sha256(payload).hexdigest()
    assert size == len(payload)
    assert size == target.stat().st_size


def test_sha256_file_on_empty_file(tmp_path):
    target = tmp_path / "empty.bin"
    target.write_bytes(b"")
    assert sha256_file(target) == (hashlib.sha256(b"").hexdigest(), 0)


def test_sha256_file_streams_multiple_chunks(tmp_path):
    """Cross the 8 MiB chunk boundary so the loop is actually exercised."""
    payload = bytes(range(256)) * ((8 * 1024 * 1024 * 2) // 256 + 7)
    target = tmp_path / "big.u32le.bin"
    target.write_bytes(payload)

    digest, size = sha256_file(target)
    assert size > 8 * 1024 * 1024
    assert digest == hashlib.sha256(payload).hexdigest()
    assert size == len(payload)


def test_sha256_file_accepts_str_path(tmp_path):
    target = tmp_path / "x.bin"
    target.write_bytes(b"x")
    assert sha256_file(str(target)) == (hashlib.sha256(b"x").hexdigest(), 1)


# ======================================================================================
# §2 naming — the doc's own tables
# ======================================================================================

# Verbatim from DATASET-STANDARD.md §2 "Examples — good".
GOOD_DATASET_IDS = [
    "pretrain/dolma2-150b",
    "pretrain/olmo-mix-1124-30b",
    "pretrain/refhq-regmix-5b5",
    "pretrain/fineweb-edu-10b",
    "curriculum/flesch-linear-370m",
    "curriculum/zlib-strict-370m",
    "sft/pedagogical-tutoring-100students",
    "sft/tulu3-mix",
    "eval/mcq-arc-openbookqa-sciq",
    "eval/tutorbench-responses",
    "eval/judge-blinded-5x5",
    "probe/s5-parity-solvable",
    "probe/multihop-wikidata-4hop",
    "vendor/dclm-hero-run-fasttext",
]

# Verbatim from DATASET-STANDARD.md §2 "Examples — reject these".
BAD_DATASET_IDS = [
    "pretrain/datamix1-jul22",
    "pretrain/new-corpus",
    "pretrain/final-v2",
    "pretrain/eric-test",
    "pretrain/data",
    "eval/results",
    "curriculum/experiment-3",
    "sft/good-data",
    "pretrain/dolma2_150B",
    "eval/mcq-v2-fixed-final",
]


@pytest.mark.parametrize("dataset_id", GOOD_DATASET_IDS)
def test_good_dataset_ids_from_the_spec_table_all_pass(dataset_id):
    family, name = validate_dataset_id(dataset_id)
    assert dataset_id == f"{family}/{name}"
    assert family in FAMILIES


@pytest.mark.parametrize("dataset_id", BAD_DATASET_IDS)
def test_bad_dataset_ids_from_the_spec_table_all_raise(dataset_id):
    with pytest.raises(NamingError):
        validate_dataset_id(dataset_id)


def test_all_families_accepted():
    for family in FAMILIES:
        assert validate_dataset_id(f"{family}/some-corpus-10b") == (family, "some-corpus-10b")


@pytest.mark.parametrize(
    "dataset_id",
    [
        "dolma2-150b",  # no family
        "pretrain/dolma2-150b/v1",  # version segment does not belong here
        "pretrain//dolma2-150b",
        "/dolma2-150b",
        "pretraining/dolma2-150b",  # not in the enum
        "Pretrain/dolma2-150b",  # enum is lowercase
        "datasets/dolma2-150b",
        "",
    ],
)
def test_dataset_id_structure_and_family_enum(dataset_id):
    with pytest.raises(NamingError):
        validate_dataset_id(dataset_id)


def test_dataset_id_rejects_non_string():
    with pytest.raises(NamingError):
        validate_dataset_id(None)  # type: ignore[arg-type]


# --- word count -----------------------------------------------------------------------


@pytest.mark.parametrize("name", ["dolma2", "corpus", "a"])
def test_name_needs_at_least_two_words(name):
    with pytest.raises(NamingError, match="word"):
        validate_name(name)


def test_name_allows_exactly_five_words():
    assert validate_name("mcq-arc-openbookqa-sciq-10b") == "mcq-arc-openbookqa-sciq-10b"


def test_name_rejects_six_words():
    with pytest.raises(NamingError, match="2-5"):
        validate_name("mcq-arc-openbookqa-sciq-piqa-10b")


# --- charset --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "dolma2_150b",  # snake_case
        "Dolma2-150b",  # capitals
        "dolma2-150B",
        "dolma2--150b",  # empty word
        "-dolma2-150b",
        "dolma2-150b-",
        "dolma2.150b",
        "dolma2 150b",
        "dolma2/150b",
        "dölma2-150b",
    ],
)
def test_name_charset_is_kebab_case_lowercase(name):
    with pytest.raises(NamingError):
        validate_name(name)


# --- dates (§2 "no dates") ------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "datamix1-jul22",  # the doc's example
        "corpus-jul22",
        "corpus-jan1",
        "corpus-dec2026",
        "mix-20260728",  # YYYYMMDD
        "mix-2026",  # bare year
        "mix-1999",
        "corpus-2026-30b",
        "corpus-jul",  # bare month word
        "corpus-july",
        "mix-20260728-30b",
    ],
)
def test_name_rejects_date_tokens(name):
    with pytest.raises(NamingError, match="date|month|year"):
        validate_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "olmo-mix-1124-30b",  # 1124 is an upstream release code, NOT a date
        "dolma2-150b",
        "refhq-regmix-5b5",
        "fineweb-edu-10b",
        "flesch-linear-370m",
        "pedagogical-tutoring-100students",
        "judge-blinded-5x5",
        "multihop-wikidata-4hop",
        "s5-parity-solvable",
        "tulu3-mix",
        "corpus-1124",
        "mix-3000",  # not 19xx/20xx, so not year-shaped
        "mix-1800",
        "decoder-probe",  # contains 'dec' but is not a month token
        "marker-probe",  # contains 'mar'
        "mayhem-corpus-10b",  # starts with 'may' but is not a month token
    ],
)
def test_name_allows_scale_suffixes_and_release_codes(name):
    """Model sizes, token budgets, hop counts, and 4-digit release codes are legitimate."""
    assert validate_name(name) == name


def test_bare_month_word_is_still_a_date_token():
    with pytest.raises(NamingError, match="month"):
        validate_name("january-effect-probe")


def test_the_release_code_1124_survives_the_date_rule():
    """Regression guard: the date rule must not eat pretrain/olmo-mix-1124-30b."""
    assert validate_dataset_id("pretrain/olmo-mix-1124-30b") == (
        "pretrain",
        "olmo-mix-1124-30b",
    )


# --- version tokens -------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "corpus-v1",
        "corpus-v2",
        "corpus-v99",
        "final-v2",
        "corpus-final",
        "new-corpus",
        "corpus-latest",
        "corpus-fixed",
        "corpus-old",
        "corpus-current",
        "mcq-v2-fixed-final",
    ],
)
def test_name_rejects_version_tokens(name):
    with pytest.raises(NamingError):
        validate_name(name)


def test_v100_is_not_a_version_token_but_v99_is():
    """The rule is v1..v99; 'v100' is out of that range and not otherwise banned."""
    with pytest.raises(NamingError):
        validate_name("corpus-v99")
    assert validate_name("corpus-v100") == "corpus-v100"


# --- content-free and relative words --------------------------------------------------


@pytest.mark.parametrize(
    "word",
    ["test", "tmp", "temp", "scratch", "data", "results", "misc", "stuff"],
)
def test_name_rejects_content_free_words(word):
    with pytest.raises(NamingError, match="content-free"):
        validate_name(f"corpus-{word}")


@pytest.mark.parametrize(
    "word",
    ["big", "small", "improved", "better", "best", "good", "bad", "fast", "slow"],
)
def test_name_rejects_relative_words(word):
    with pytest.raises(NamingError, match="relative"):
        validate_name(f"{word}-corpus")


def test_name_rejects_bare_ordinals():
    with pytest.raises(NamingError, match="ordinal"):
        validate_name("experiment-3")
    with pytest.raises(NamingError, match="ordinal"):
        validate_name("corpus-12")


def test_ordinal_rule_does_not_eat_units_or_release_codes():
    for name in ("corpus-370m", "corpus-150b", "corpus-5b5", "corpus-1124", "corpus-4hop"):
        assert validate_name(name) == name


# ======================================================================================
# §2 purpose
# ======================================================================================

# Verbatim from DATASET-STANDARD.md §2 "Good:".
GOOD_PURPOSES = [
    "150B-token Dolma2 mix for 370M ladder pretraining at 1.25xC",
    "Curriculum ordering by Flesch reading ease for the 370M difficulty-ordering ablation",
    "ARC/OpenBookQA/SciQ loglikelihood scores for 23 baseline models, for the "
    "adaptive-inference baseline table",
    "Blinded judge verdicts, 5 judges x 5 prompt variants x 3 replicates, to measure "
    "judge reliability",
    "Held-out synthetic reasoning items, 14 tasks x 512, for the memory-split "
    "dense-vs-sparse comparison",
    "Retention corpus of general text to measure forgetting during distillation",
]

# Verbatim from DATASET-STANDARD.md §2 "Reject:".
BAD_PURPOSES = [
    "training data",
    "the dataset",
    "data from the run",
    "TODO",
    "tbd",
    "corpus for the project",
    "experiments",
    "see README",
]


@pytest.mark.parametrize("purpose", GOOD_PURPOSES)
def test_good_purposes_from_the_spec_pass(purpose):
    assert validate_purpose(purpose) is None


@pytest.mark.parametrize("purpose", BAD_PURPOSES)
def test_bad_purposes_from_the_spec_raise(purpose):
    with pytest.raises(NamingError):
        validate_purpose(purpose)


@pytest.mark.parametrize(
    "purpose",
    ["", "   ", "todo", "ToDo", " TODO. ", "T B D", "Dataset", "  the   dataset  ", "See Readme!"],
)
def test_purpose_blocklist_is_case_space_and_punctuation_insensitive(purpose):
    with pytest.raises(NamingError):
        validate_purpose(purpose)


def test_purpose_min_length():
    with pytest.raises(NamingError, match="at least 20"):
        validate_purpose("short thing here")  # 16 chars, has a space


def test_purpose_at_exactly_min_length_passes():
    purpose = "a" * 18 + " b"  # 20 chars including the space
    assert len(purpose) == 20
    assert validate_purpose(purpose) is None


def test_purpose_max_length():
    with pytest.raises(NamingError, match="300"):
        validate_purpose("x " + "y" * 299)


def test_purpose_at_exactly_max_length_passes():
    purpose = "x " + "y" * 298
    assert len(purpose) == 300
    assert validate_purpose(purpose) is None


def test_purpose_must_contain_a_space():
    with pytest.raises(NamingError, match="single token"):
        validate_purpose("dolma2-150b-tokens-for-370m-ladder-pretraining")


def test_purpose_rejects_non_string():
    with pytest.raises(NamingError):
        validate_purpose(None)  # type: ignore[arg-type]


# ======================================================================================
# §2 / §3 version block
# ======================================================================================


def test_version_v1_has_no_antecedent():
    v = Version(id="v1", relation="supersedes", of=None)
    assert v.to_dict() == {"id": "v1", "relation": "supersedes", "of": None}


def test_version_roundtrip_matches_the_spec_example():
    d = {"id": "v3", "relation": "supersedes", "of": "v2"}
    assert Version.from_dict(d).to_dict() == d


@pytest.mark.parametrize("relation", sorted(RELATIONS))
def test_all_relations_accepted(relation):
    assert Version(id="v2", relation=relation, of="v1").relation == relation


def test_version_rejects_unknown_relation():
    with pytest.raises(NamingError, match="relation"):
        Version(id="v2", relation="replaces", of="v1")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_id", ["v0", "1", "v", "V1", "v01", "v1.1", "latest", "", "v-1"])
def test_version_id_must_be_vN_with_N_at_least_1(bad_id):
    with pytest.raises(NamingError, match="version id"):
        Version(id=bad_id, relation="supersedes", of=None)


def test_version_v1_with_an_of_is_rejected():
    with pytest.raises(NamingError, match="first version"):
        Version(id="v1", relation="supersedes", of="v0")


def test_version_beyond_v1_requires_an_of():
    with pytest.raises(NamingError, match="must name the version"):
        Version(id="v2", relation="supersedes", of=None)


def test_version_cannot_reference_itself():
    with pytest.raises(NamingError, match="itself"):
        Version(id="v2", relation="supersedes", of="v2")


def test_version_of_must_be_vN():
    with pytest.raises(NamingError, match="'of'"):
        Version(id="v2", relation="extends", of="previous")


def test_version_from_dict_requires_id_and_relation():
    with pytest.raises(NamingError, match="'id'"):
        Version.from_dict({"relation": "supersedes", "of": None})
    with pytest.raises(NamingError, match="'relation'"):
        Version.from_dict({"id": "v1", "of": None})


def test_version_from_dict_rejects_unknown_keys():
    with pytest.raises(NamingError, match="unknown key"):
        Version.from_dict({"id": "v1", "relation": "supersedes", "of": None, "date": "2026"})


def test_version_from_dict_rejects_non_dict():
    with pytest.raises(NamingError, match="object"):
        Version.from_dict("v1")  # type: ignore[arg-type]


def test_version_is_hashable_and_frozen():
    v = Version(id="v1")
    assert {v, Version(id="v1")} == {v}
    with pytest.raises(Exception):
        v.id = "v2"  # type: ignore[misc]


def test_version_survives_canonical_json():
    assert canonical_json(Version(id="v2", relation="extends", of="v1").to_dict()) == (
        b'{"id":"v2","of":"v1","relation":"extends"}'
    )
