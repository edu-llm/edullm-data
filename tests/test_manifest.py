"""Tests for edullm_data.manifest — §5 format/honesty/naming, §7 Gate A arithmetic."""

from __future__ import annotations

import hashlib

import pytest

from edullm_data.contracts import SCHEMA_VERSION, canonical_json, sha256_bytes
from edullm_data.manifest import (
    COUNT_UNITS,
    DTYPE_SIZES,
    EXTENSION_FORMAT,
    Format,
    ManifestEntry,
    build_manifest,
    check_extension_matches_format,
    check_shard_naming,
    diff_paths,
    is_cas_name,
    manifest_sha256,
    parse_shard_name,
    verify_arithmetic,
)

HEX = "a" * 64
HEX2 = "b" * 64
HEX3 = "c" * 64


def token_entry(
    path: str = "tokens/train-00000.u32le.bin",
    *,
    tokens: int | None = 134217728,
    nbytes: int | None = None,
    dtype: str = "uint32",
    sha: str = HEX,
    fmt: Format | None = None,
) -> ManifestEntry:
    """A §5 token-shard entry whose arithmetic is correct unless overridden."""
    fmt = fmt if fmt is not None else Format.for_tokens(dtype)
    if nbytes is None:
        nbytes = (tokens or 0) * DTYPE_SIZES[dtype]
    return ManifestEntry(
        path=path,
        sha256=sha,
        bytes=nbytes,
        count=None if tokens is None else {"unit": "tokens", "value": tokens},
        format=fmt,
    )


# ======================================================================================
# Format
# ======================================================================================


def test_format_for_tokens_is_the_headerless_raw_form():
    fmt = Format.for_tokens()
    assert fmt.to_dict() == {
        "container": "raw",
        "dtype": "uint32",
        "byte_order": "little",
        "header_bytes": 0,
        "codec": "none",
    }
    assert fmt.dtype_size == 4


def test_format_for_tokens_accepts_uint16():
    fmt = Format.for_tokens("uint16")
    assert fmt.dtype == "uint16"
    assert fmt.dtype_size == 2
    assert fmt.header_bytes == 0


def test_format_for_tokens_rejects_a_non_fixed_width_dtype():
    with pytest.raises(ValueError, match="fixed-width"):
        Format.for_tokens("bfloat16")


def test_format_matches_the_spec_example_block():
    """§5's worked example, byte for byte through canonical JSON."""
    fmt = Format(
        container="raw", dtype="uint32", byte_order="little", header_bytes=0, codec="none"
    )
    assert canonical_json(fmt.to_dict()) == (
        b'{"byte_order":"little","codec":"none","container":"raw",'
        b'"dtype":"uint32","header_bytes":0}'
    )


def test_format_roundtrip():
    d = {
        "container": "csv",
        "dtype": None,
        "byte_order": None,
        "header_bytes": 0,
        "codec": "gzip",
    }
    assert Format.from_dict(d).to_dict() == d


def test_format_from_dict_defaults():
    fmt = Format.from_dict({"container": "parquet"})
    assert (fmt.dtype, fmt.byte_order, fmt.header_bytes, fmt.codec) == (
        None,
        None,
        0,
        "none",
    )


@pytest.mark.parametrize(
    "bad",
    [
        {},  # no container
        {"container": ""},
        {"container": "raw", "byte_order": "middle"},
        {"container": "raw", "header_bytes": -1},
        {"container": "raw", "header_bytes": 1.5},
        {"container": "raw", "codec": ""},
        {"container": "raw", "endianness": "little"},  # unknown key
    ],
)
def test_format_rejects_malformed_blocks(bad):
    with pytest.raises(ValueError):
        Format.from_dict(bad)


def test_dtype_size_is_none_for_unknown_dtype():
    assert Format(container="raw", dtype="bfloat16").dtype_size is None
    assert Format(container="parquet").dtype_size is None


def test_dtype_sizes_table():
    assert DTYPE_SIZES == {
        "uint8": 1,
        "uint16": 2,
        "uint32": 4,
        "int32": 4,
        "float32": 4,
        "float16": 2,
        "int64": 8,
        "float64": 8,
    }


# ======================================================================================
# ManifestEntry
# ======================================================================================


def test_manifest_entry_matches_the_spec_example():
    """§5's example row, reproduced exactly."""
    entry = ManifestEntry(
        path="tokens/train-00000.u32le.bin",
        sha256=HEX,
        bytes=536870912,
        count={"unit": "tokens", "value": 134217728},
        format=Format.for_tokens(),
    )
    d = entry.to_dict()
    assert d["path"] == "tokens/train-00000.u32le.bin"
    assert d["bytes"] == 536870912
    assert d["count"] == {"unit": "tokens", "value": 134217728}
    assert d["format"]["header_bytes"] == 0
    assert ManifestEntry.from_dict(d) == entry
    # And the identity holds: 134217728 x 4 == 536870912
    assert verify_arithmetic(entry) == []


def test_count_is_omissible_and_omitted_not_nulled():
    """§5: a tar part or a .done sentinel has no honest count."""
    entry = ManifestEntry(
        path="dist/bundle.tar.part-0",
        sha256=HEX,
        bytes=1024,
        count=None,
        format=Format(container="raw"),
    )
    assert "count" not in entry.to_dict()
    assert ManifestEntry.from_dict(entry.to_dict()).count is None


@pytest.mark.parametrize("unit", sorted(COUNT_UNITS))
def test_all_count_units_accepted(unit):
    entry = ManifestEntry(
        path="g/rows-00000.jsonl",
        sha256=HEX,
        bytes=10,
        count={"unit": unit, "value": 1},
        format=Format(container="jsonl"),
    )
    assert entry.count == {"unit": unit, "value": 1}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sha256": "notahash"},
        {"sha256": HEX.upper()},  # must be lowercase hex
        {"sha256": "a" * 63},
        {"bytes": -1},
        {"bytes": 1.0},
        {"bytes": True},
        {"path": ""},
        {"path": "/abs/train-00000.u32le.bin"},
        {"path": "../escape.bin"},
        {"path": "tokens\\train.bin"},
        {"count": {"unit": "blocks", "value": 1}},
        {"count": {"unit": "tokens", "value": -1}},
        {"count": {"unit": "tokens"}},
        {"count": {"unit": "tokens", "value": 1, "extra": 2}},
        {"count": 5},
    ],
)
def test_manifest_entry_rejects_malformed_rows(kwargs):
    base = {
        "path": "tokens/train-00000.u32le.bin",
        "sha256": HEX,
        "bytes": 4,
        "count": {"unit": "tokens", "value": 1},
        "format": Format.for_tokens(),
    }
    base.update(kwargs)
    with pytest.raises(ValueError):
        ManifestEntry(**base)  # type: ignore[arg-type]


def test_manifest_entry_from_dict_requires_the_always_required_keys():
    """§5: bytes and sha256 always required."""
    full = {
        "path": "tokens/train-00000.u32le.bin",
        "sha256": HEX,
        "bytes": 4,
        "format": {"container": "raw"},
    }
    for missing in ("path", "sha256", "bytes", "format"):
        partial = {k: v for k, v in full.items() if k != missing}
        with pytest.raises(ValueError, match=missing):
            ManifestEntry.from_dict(partial)


# ======================================================================================
# §5 the honesty rule — the .npy lie
# ======================================================================================


def test_npy_extension_with_zero_header_bytes_is_a_lie():
    """The audit's real bug: 7,557 headerless raw-uint32 objects named .npy."""
    entry = token_entry(path="tokens/train-00000.npy")
    violations = check_extension_matches_format(entry.path, entry.format)
    assert violations, "a .npy name over headerless raw bytes must be rejected"
    blob = " ".join(violations)
    assert "train-00000.npy" in blob
    assert "header_bytes=0" in blob
    assert ".u32le.bin" in blob  # the message must say what to do instead


def test_a_real_npy_passes_the_extension_check():
    """The extension is only a lie when it contradicts the bytes."""
    fmt = Format(
        container="npy", dtype="uint32", byte_order="little", header_bytes=128, codec="none"
    )
    assert check_extension_matches_format("arrays/embeddings.npy", fmt) == []


def test_npy_with_a_header_but_raw_container_is_still_flagged():
    fmt = Format(
        container="raw", dtype="uint32", byte_order="little", header_bytes=128, codec="none"
    )
    violations = check_extension_matches_format("tokens/train-00000.npy", fmt)
    assert any("container" in v for v in violations)


def test_u32le_bin_with_nonzero_header_bytes_is_flagged():
    """The mirror image of the .npy lie: a header where headerless was promised."""
    fmt = Format(
        container="raw", dtype="uint32", byte_order="little", header_bytes=128, codec="none"
    )
    violations = check_extension_matches_format("tokens/train-00000.u32le.bin", fmt)
    assert violations
    assert any("header_bytes=128" in v for v in violations)
    assert any("headerless" in v for v in violations)


def test_u32le_bin_agrees_with_for_tokens():
    assert check_extension_matches_format(
        "tokens/train-00000.u32le.bin", Format.for_tokens("uint32")
    ) == []


def test_u32le_bin_declared_uint16_is_flagged():
    """§5: OLMo-core defaults to uint16 while these corpora are uint32."""
    violations = check_extension_matches_format(
        "tokens/train-00000.u32le.bin", Format.for_tokens("uint16")
    )
    assert any("dtype" in v for v in violations)


def test_u16le_bin_maps_to_uint16_little_headerless():
    expected = EXTENSION_FORMAT[".u16le.bin"]
    assert expected["dtype"] == "uint16"
    assert expected["byte_order"] == "little"
    assert expected["container"] == "raw"
    assert expected["header_bytes"] == 0
    assert check_extension_matches_format(
        "tokens/val-00000.u16le.bin", Format.for_tokens("uint16")
    ) == []


def test_byte_order_in_the_extension_is_enforced():
    violations = check_extension_matches_format(
        "tokens/train-00000.u32be.bin", Format.for_tokens("uint32")
    )
    assert any("byte_order" in v for v in violations)


@pytest.mark.parametrize(
    "path,fmt,ok",
    [
        ("sidecars/train-00000.csv.gz", Format(container="csv", codec="gzip"), True),
        ("sidecars/train-00000.csv.gz", Format(container="csv", codec="none"), False),
        ("sidecars/train-00000.csv.gz", Format(container="parquet", codec="gzip"), False),
        ("rows/train-00000.jsonl", Format(container="jsonl", codec="none"), True),
        ("rows/train-00000.jsonl.gz", Format(container="jsonl", codec="gzip"), True),
        ("rows/train-00000.jsonl.gz", Format(container="jsonl", codec="none"), False),
        ("rows/train-00000.parquet", Format(container="parquet"), True),
        ("rows/train-00000.parquet", Format(container="jsonl"), False),
        ("dataset.json", Format(container="json"), True),
        ("dataset.json", Format(container="jsonl"), False),
    ],
)
def test_container_and_codec_honesty_for_tabular_and_text(path, fmt, ok):
    violations = check_extension_matches_format(path, fmt)
    assert (violations == []) is ok


def test_jsonl_gz_beats_a_shorter_suffix_match():
    """Longest-suffix matching: .jsonl.gz must not be read as .jsonl."""
    assert check_extension_matches_format(
        "rows/train-00000.jsonl.gz", Format(container="jsonl", codec="gzip")
    ) == []


def test_unknown_extension_makes_no_claim_and_no_violation():
    """An extension that claims nothing cannot contradict anything."""
    assert check_extension_matches_format("blobs/part-00000.bin", Format(container="raw")) == []
    assert check_extension_matches_format("_SUCCESS", Format(container="raw")) == []


def test_extension_match_is_case_insensitive():
    violations = check_extension_matches_format("tokens/TRAIN-00000.NPY", Format.for_tokens())
    assert violations


# ======================================================================================
# build_manifest / manifest_sha256
# ======================================================================================


def test_build_manifest_shape_and_recomputed_totals():
    entries = [
        token_entry("tokens/train-00001.u32le.bin", tokens=4, sha=HEX2),
        token_entry("tokens/train-00000.u32le.bin", tokens=8, sha=HEX),
    ]
    m = build_manifest(entries, group_name="tokens")
    assert m["schema_version"] == SCHEMA_VERSION
    assert m["group"] == "tokens"
    assert m["objects"] == 2
    assert m["bytes"] == (4 + 8) * 4
    assert set(m) == {"schema_version", "group", "entries", "objects", "bytes"}


def test_build_manifest_sorts_entries_by_path():
    entries = [
        token_entry("tokens/train-00002.u32le.bin", tokens=1, sha=HEX3),
        token_entry("tokens/train-00000.u32le.bin", tokens=1, sha=HEX),
        token_entry("tokens/train-00001.u32le.bin", tokens=1, sha=HEX2),
    ]
    m = build_manifest(entries, group_name="tokens")
    assert [e["path"] for e in m["entries"]] == [
        "tokens/train-00000.u32le.bin",
        "tokens/train-00001.u32le.bin",
        "tokens/train-00002.u32le.bin",
    ]


def test_manifest_hash_is_independent_of_discovery_order():
    """Two publishers listing the same prefix must agree on manifest_sha256."""
    a = [
        token_entry("tokens/train-00000.u32le.bin", tokens=1, sha=HEX),
        token_entry("tokens/train-00001.u32le.bin", tokens=1, sha=HEX2),
    ]
    b = list(reversed(a))
    assert manifest_sha256(build_manifest(a, group_name="tokens")) == manifest_sha256(
        build_manifest(b, group_name="tokens")
    )


def test_manifest_sha256_is_sha256_of_canonical_json():
    m = build_manifest([token_entry(tokens=1)], group_name="tokens")
    assert manifest_sha256(m) == hashlib.sha256(canonical_json(m)).hexdigest()
    assert manifest_sha256(m) == sha256_bytes(canonical_json(m))


def test_manifest_sha256_changes_when_any_byte_of_a_claim_changes():
    base = build_manifest([token_entry(tokens=8)], group_name="tokens")
    bumped = build_manifest([token_entry(tokens=9, nbytes=36)], group_name="tokens")
    assert manifest_sha256(base) != manifest_sha256(bumped)


def test_build_manifest_rejects_duplicate_paths():
    dupes = [token_entry(tokens=1, sha=HEX), token_entry(tokens=1, sha=HEX2)]
    with pytest.raises(ValueError, match="duplicate path"):
        build_manifest(dupes, group_name="tokens")


def test_build_manifest_on_an_empty_group():
    m = build_manifest([], group_name="sidecars")
    assert (m["objects"], m["bytes"], m["entries"]) == (0, 0, [])


def test_build_manifest_requires_a_group_name():
    with pytest.raises(ValueError, match="group_name"):
        build_manifest([], group_name="")


def test_build_manifest_accepts_a_generator():
    m = build_manifest((token_entry(tokens=1),), group_name="tokens")
    assert m["objects"] == 1


# ======================================================================================
# §7 Gate A — the arithmetic identity
# ======================================================================================


def test_the_identity_from_the_spec_holds():
    """§7: 86,096,509 x 4 = 344,386,036 = exact object size."""
    entry = token_entry(tokens=86_096_509, nbytes=344_386_036)
    assert 86_096_509 * 4 == 344_386_036
    assert verify_arithmetic(entry) == []


def test_arithmetic_catches_a_truncated_shard():
    """A crashed writer leaves a correctly-hashed file that is a page short."""
    entry = token_entry(tokens=86_096_509, nbytes=344_386_036 - 4096)
    violations = verify_arithmetic(entry)
    assert len(violations) == 1
    assert "344386036" in violations[0]
    assert "-4096" in violations[0]


def test_arithmetic_catches_the_uint16_vs_uint32_trap():
    """Declaring uint16 over uint32 bytes silently doubles the count."""
    entry = ManifestEntry(
        path="tokens/train-00000.u32le.bin",
        sha256=HEX,
        bytes=344_386_036,
        count={"unit": "tokens", "value": 86_096_509},
        format=Format.for_tokens("uint16"),
    )
    assert verify_arithmetic(entry)


def test_arithmetic_applies_to_indices():
    ok = ManifestEntry(
        path="order/train-00000.u32le.bin",
        sha256=HEX,
        bytes=40,
        count={"unit": "indices", "value": 10},
        format=Format.for_tokens("uint32"),
    )
    assert verify_arithmetic(ok) == []
    bad = ManifestEntry(
        path="order/train-00000.u32le.bin",
        sha256=HEX,
        bytes=41,
        count={"unit": "indices", "value": 10},
        format=Format.for_tokens("uint32"),
    )
    assert verify_arithmetic(bad)


@pytest.mark.parametrize("unit", ["rows", "items", "bytes"])
def test_arithmetic_declines_on_variable_width_units(unit):
    """rows/items are variable-width; asserting would manufacture a false failure."""
    entry = ManifestEntry(
        path="rows/train-00000.jsonl",
        sha256=HEX,
        bytes=12345,
        count={"unit": unit, "value": 7},
        format=Format(container="jsonl"),
    )
    assert verify_arithmetic(entry) == []


def test_arithmetic_declines_when_there_is_no_count():
    assert verify_arithmetic(token_entry(tokens=None, nbytes=999)) == []


def test_arithmetic_declines_on_an_unknown_dtype():
    entry = ManifestEntry(
        path="tokens/train-00000.bin",
        sha256=HEX,
        bytes=999,
        count={"unit": "tokens", "value": 7},
        format=Format(container="raw", dtype="bfloat16", byte_order="little"),
    )
    assert verify_arithmetic(entry) == []


def test_arithmetic_declines_when_bytes_are_encoded():
    """With a codec, `bytes` is the compressed size — the identity is meaningless."""
    entry = ManifestEntry(
        path="tokens/train-00000.u32le.bin.gz",
        sha256=HEX,
        bytes=17,
        count={"unit": "tokens", "value": 1000},
        format=Format(container="raw", dtype="uint32", byte_order="little", codec="gzip"),
    )
    assert verify_arithmetic(entry) == []


def test_arithmetic_accounts_for_a_declared_header():
    entry = ManifestEntry(
        path="arrays/embeddings.raw",
        sha256=HEX,
        bytes=128 + 10 * 4,
        count={"unit": "indices", "value": 10},
        format=Format(container="raw", dtype="uint32", byte_order="little", header_bytes=128),
    )
    assert verify_arithmetic(entry) == []


def test_arithmetic_on_a_zero_length_shard():
    assert verify_arithmetic(token_entry(tokens=0, nbytes=0)) == []
    assert verify_arithmetic(token_entry(tokens=0, nbytes=4))


# ======================================================================================
# §5 exhaustiveness — path-set equality
# ======================================================================================


def test_diff_paths_reports_both_directions():
    manifest = {"tokens/train-00000.u32le.bin", "tokens/train-00001.u32le.bin"}
    actual = {"tokens/train-00001.u32le.bin", "tokens/train-00002.u32le.bin"}
    missing, extra = diff_paths(manifest, actual)
    assert missing == {"tokens/train-00000.u32le.bin"}
    assert extra == {"tokens/train-00002.u32le.bin"}


def test_diff_paths_clean_when_sets_are_equal():
    paths = {"tokens/train-00000.u32le.bin"}
    assert diff_paths(paths, set(paths)) == (set(), set())


def test_diff_paths_catches_the_stray_shard_a_globbing_reader_would_train_on():
    manifest = {"tokens/train-00000.u32le.bin"}
    actual = manifest | {"tokens/train-00001.u32le.bin"}
    missing, extra = diff_paths(manifest, actual)
    assert missing == set()
    assert extra == {"tokens/train-00001.u32le.bin"}


def test_diff_paths_catches_a_member_still_uploading():
    """In-flight multipart uploads are invisible to LIST (§6)."""
    manifest = {"tokens/train-00000.u32le.bin", "tokens/train-00001.u32le.bin"}
    missing, extra = diff_paths(manifest, {"tokens/train-00000.u32le.bin"})
    assert missing == {"tokens/train-00001.u32le.bin"}
    assert extra == set()


def test_diff_paths_does_not_mutate_its_inputs():
    manifest = {"a"}
    actual = {"b"}
    diff_paths(manifest, actual)
    assert manifest == {"a"} and actual == {"b"}


def test_diff_paths_against_a_built_manifest():
    m = build_manifest(
        [
            token_entry("tokens/train-00000.u32le.bin", tokens=1, sha=HEX),
            token_entry("tokens/train-00001.u32le.bin", tokens=1, sha=HEX2),
        ],
        group_name="tokens",
    )
    listed = {e["path"] for e in m["entries"]}
    missing, extra = diff_paths(listed, {"tokens/train-00000.u32le.bin", "tokens/stray.u32le.bin"})
    assert missing == {"tokens/train-00001.u32le.bin"}
    assert extra == {"tokens/stray.u32le.bin"}


# ======================================================================================
# §5 shard naming
# ======================================================================================


@pytest.mark.parametrize(
    "path,expected",
    [
        ("tokens/train-00000.u32le.bin", ("train", 0)),
        ("train-00000.u32le.bin", ("train", 0)),
        ("tokens/train-00007.u32le.bin", ("train", 7)),
        ("tokens/val-01234.u16le.bin", ("val", 1234)),
        ("sidecars/train-00000.csv.gz", ("train", 0)),
        ("rows/held-out-00012.jsonl", ("held-out", 12)),
    ],
)
def test_parse_shard_name(path, expected):
    assert parse_shard_name(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "tokens/train-0.u32le.bin",  # not 5 digits
        "tokens/train-000000.u32le.bin",  # 6 digits
        "tokens/train.u32le.bin",  # no ordinal
        "tokens/manifest.json",
        "tokens/train-00000-of-00042.u32le.bin",  # §5 rejects -of-N outright
        "tokens/Train-00000.u32le.bin",
        "tokens/_SUCCESS",
    ],
)
def test_parse_shard_name_returns_none_for_non_shards(path):
    assert parse_shard_name(path) is None


def test_of_n_shard_names_are_rejected():
    """§5 excludes -of-NNNNN: unknowable at write time."""
    path = "tokens/train-00000-of-00042.u32le.bin"
    assert parse_shard_name(path) is None
    violations = check_shard_naming(path)
    assert violations
    assert "-of-NNNNN" in violations[0]


def test_cas_names_are_recognised_as_exempt_not_violations():
    """§5: objects/<sha256>.bin *is* the dedup mechanism."""
    cas = "objects/" + "0" * 64 + ".bin"
    assert is_cas_name(cas)
    assert parse_shard_name(cas) is None
    assert check_shard_naming(cas) == []


def test_cas_detection_requires_lowercase_64_hex():
    assert not is_cas_name("objects/" + "0" * 63 + ".bin")
    assert not is_cas_name("objects/" + "A" * 64 + ".bin")
    assert not is_cas_name("objects/" + "g" * 64 + ".bin")


def test_shard_naming_ok_for_a_conforming_shard():
    assert check_shard_naming("tokens/train-00000.u32le.bin") == []


def test_vendored_trees_are_exempt():
    """§5: renaming a vendored tree destroys upstream verifiability."""
    path = "vendor_root/dclm/hero-run/fasttext/model.bin"
    assert check_shard_naming(path)  # not exempt by default
    assert check_shard_naming(path, exempt=True) == []


# ======================================================================================
# integration: one dataset, two groups (§4's worked layout)
# ======================================================================================


def test_two_group_dataset_passes_every_check_in_this_module():
    tokens = [
        token_entry("tokens/train-00000.u32le.bin", tokens=1024, sha=HEX),
        token_entry("tokens/val-00000.u32le.bin", tokens=16, sha=HEX2),
    ]
    sidecars = [
        ManifestEntry(
            path="sidecars/train-00000.csv.gz",
            sha256=HEX3,
            bytes=2048,
            count={"unit": "rows", "value": 1024},
            format=Format(container="csv", codec="gzip"),
        )
    ]

    for entries, group in ((tokens, "tokens"), (sidecars, "sidecars")):
        m = build_manifest(entries, group_name=group)
        assert m["objects"] == len(entries)
        assert m["bytes"] == sum(e.bytes for e in entries)
        assert len(manifest_sha256(m)) == 64
        for entry in entries:
            assert check_extension_matches_format(entry.path, entry.format) == []
            assert verify_arithmetic(entry) == []
            assert check_shard_naming(entry.path) == []
        assert diff_paths({e["path"] for e in m["entries"]}, {e.path for e in entries}) == (
            set(),
            set(),
        )


def test_the_audits_fake_npy_group_fails_two_independent_gates():
    """Both the honesty rule and the arithmetic must fire on the audit's real bug."""
    entry = ManifestEntry(
        path="tokens/part-00000.npy",
        sha256=HEX,
        bytes=344_386_036,
        count={"unit": "tokens", "value": 86_096_509 // 2},  # halved by a uint16 guess
        format=Format(container="raw", dtype="uint32", byte_order="little", header_bytes=0),
    )
    assert check_extension_matches_format(entry.path, entry.format)
    assert verify_arithmetic(entry)
