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
from edullm_data.manifest import (
    DEFAULT_DOMAIN_KEEP,
    MAX_SEGMENT_CHARS,
    OTHER_SEGMENT,
    SAFE_SEGMENT_RE,
    SlugError,
    build_domain_slug_map,
    labels_from_path,
    slug_path_segment,
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
        ("tokenizer/merges.txt", Format(container="text"), True),
        ("tokenizer/merges.txt", Format(container="json"), False),
    ],
)
def test_container_and_codec_honesty_for_tabular_and_text(path, fmt, ok):
    violations = check_extension_matches_format(path, fmt)
    assert (violations == []) is ok


def test_txt_sidecar_publishes_as_honest_text_not_an_error():
    """A tokenizer's merges.txt must resolve to an honest 'text' format (no dtype, no
    arithmetic), so a published tokenizer keeps every file that loads it. Regression for
    the first tokenizer publish, where '.txt' was an unknown extension and publish()
    raised 'cannot determine format' rather than shipping the merges file."""
    from edullm_data.publish import _format_for
    from edullm_data.manifest import verify_arithmetic, ManifestEntry

    fmt = _format_for("tokenizer/merges.txt", {})
    assert fmt.container == "text"
    assert fmt.dtype is None and fmt.byte_order is None
    # honest declaration, no contradiction
    assert check_extension_matches_format("tokenizer/merges.txt", fmt) == []
    # text is not fixed-width, so the arithmetic identity must decline (never fire)
    entry = ManifestEntry(path="tokenizer/merges.txt", sha256="a" * 64, bytes=916646, format=fmt)
    assert verify_arithmetic(entry) == []


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


# --------------------------------------------------------------------------------------
# §5 — slugging an INHERITED domain value into a path segment
# --------------------------------------------------------------------------------------
#
# The hazard, verified by execution before any of this was written:
#
#   s3://edullm-data/pretrain/x/v1/tokens/stackv2-edu/C#/train-00000.u32le.bin
#     urlparse -> path='/pretrain/x/v1/tokens/stackv2-edu/C'  fragment='/train-00000.u32le.bin'
#
# The shard name leaves the path entirely. And NOTHING in the pipeline catches it:
# `labels_from_path` returns {'source': 'stackv2-edu', 'domain': 'C#'} without complaint and
# `fnmatch` matches 'tokens/stackv2-edu/*/train-*.u32le.bin'. It surfaces at read time, in a
# consumer, on a segment that is already inside `manifest_sha256` and therefore unfixable
# without republishing every byte. So these tests are the gate, and the collision tests below
# are the most load-bearing ones in the file: a collision is the failure that costs a re-copy
# while every count still adds up.


def test_the_four_verified_upstream_values_slug_as_specified():
    """The exact values read out of real records: gha_language, metadata.site."""
    assert slug_path_segment("C#") == "c-sharp"
    assert slug_path_segment("C++") == "c-plus-plus"
    assert slug_path_segment("Jupyter Notebook") == "jupyter-notebook"
    assert slug_path_segment("3dprinting.stackexchange.com") == "3dprinting"


def test_c_sharp_and_c_plus_plus_do_not_collapse_to_the_same_thing():
    """THE check. Generic character-stripping maps both to 'c' — one directory, two languages,
    permanently, with every token count still adding up."""
    assert slug_path_segment("C#") != slug_path_segment("C++")
    assert "c" not in (slug_path_segment("C#"), slug_path_segment("C++"))


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Python", "python"),
        ("Objective-C++", "objective-c-plus-plus"),
        ("F*", "f-star"),
        ("Emacs Lisp", "emacs-lisp"),
        ("mathoverflow.stackexchange.com", "mathoverflow"),
        ("Café", "cafe"),  # an accent renders a letter; it does not name a different thing
        ("science & technology", "science-technology"),
        ("500 - Natural sciences and mathematics", "500-natural-sciences-and-mathematics"),
        ("  Rust  ", "rust"),
    ],
)
def test_slugging_covers_the_shapes_the_real_vocabularies_contain(value, expected):
    assert slug_path_segment(value) == expected


def test_every_slug_matches_the_safe_segment_shape():
    for value in ("C#", "C++", "Jupyter Notebook", "3dprinting.stackexchange.com", "F*"):
        assert SAFE_SEGMENT_RE.match(slug_path_segment(value))


# ---- the three consumers that actually matter ----


@pytest.mark.parametrize("value", ["C#", "C++", "Jupyter Notebook", "3dprinting.stackexchange.com"])
def test_a_slug_round_trips_through_urlparse_fnmatch_and_labels_from_path(value):
    """The regex is the cheap gate; these three are the real ones. Recompute, never trust."""
    import fnmatch
    from urllib.parse import urlparse

    segment = slug_path_segment(value)
    rel = f"tokens/stackv2-edu/{segment}/train-00000.u32le.bin"
    key = f"pretrain/reservoir-260b/v1/{rel}"

    parsed = urlparse(f"s3://edullm-data/{key}")
    assert parsed.path == f"/{key}"
    assert parsed.fragment == "" and parsed.query == ""

    assert fnmatch.fnmatch(rel, rel), "a literal glob built from the key must match the key"
    assert fnmatch.fnmatch(rel, "tokens/stackv2-edu/*/train-*.u32le.bin")

    assert labels_from_path(rel) == {"source": "stackv2-edu", "domain": segment}


def test_the_raw_values_fail_the_same_round_trip_that_the_slugs_pass():
    """The failing half: proves the round-trip check above is not vacuous."""
    import fnmatch
    from urllib.parse import urlparse

    # `#` truncates the URI: the shard name lands in the fragment.
    parsed = urlparse("s3://edullm-data/pretrain/x/v1/tokens/stackv2-edu/C#/train-00000.u32le.bin")
    assert parsed.path.endswith("/stackv2-edu/C")
    assert parsed.fragment == "/train-00000.u32le.bin"

    # …while the pipeline's own checks are perfectly happy with it.
    raw = "tokens/stackv2-edu/C#/train-00000.u32le.bin"
    assert labels_from_path(raw) == {"source": "stackv2-edu", "domain": "C#"}
    assert fnmatch.fnmatch(raw, "tokens/stackv2-edu/*/train-*.u32le.bin")

    # And a bracketed value is not even fnmatch-inert against itself.
    bracketed = "tokens/stackv2-edu/a[b]/train-00000.u32le.bin"
    assert not fnmatch.fnmatch(bracketed, bracketed)


def test_a_segment_that_fails_its_consumers_is_refused_even_via_an_override():
    """An override is the one path the generic rules cannot clean up, so verification still runs."""
    with pytest.raises(SlugError) as e:
        slug_path_segment("Whatever", overrides={"whatever": "not#safe"})
    assert "not a safe segment" in str(e.value) or "does not survive" in str(e.value)


# ---- what it refuses, and why refusing beats guessing ----


def test_an_unknown_meaningful_character_is_refused_rather_than_dropped():
    with pytest.raises(SlugError) as e:
        slug_path_segment("x\x01y")
    msg = str(e.value)
    assert "neither punctuation nor" in msg
    assert "'C#' and 'C++' both become 'c'" in msg, "the message must name the real hazard"


def test_a_value_that_slugs_to_nothing_is_refused():
    """An empty segment collapses the key by a level, so the SHARD NAME becomes the domain."""
    with pytest.raises(SlugError) as e:
        slug_path_segment("日本語")
    assert "nothing survives" in str(e.value)


def test_an_over_long_value_is_refused_not_truncated():
    """Truncation is how two distinct values silently become one directory."""
    with pytest.raises(SlugError) as e:
        slug_path_segment("z" * (MAX_SEGMENT_CHARS + 1))
    assert "Refused rather than truncated" in str(e.value)
    assert slug_path_segment("z" * MAX_SEGMENT_CHARS) == "z" * MAX_SEGMENT_CHARS


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_an_empty_value_is_refused(value):
    with pytest.raises(SlugError):
        slug_path_segment(value)


def test_a_non_string_is_refused():
    with pytest.raises(SlugError):
        slug_path_segment(None)  # type: ignore[arg-type]


# ---- the fold, and the collision check that makes the fold safe ----


def test_the_top_n_keep_their_names_and_the_tail_folds_to_other():
    weights = {"Python": 100, "C#": 90, "C++": 80, "Rust": 70, "Go": 6, "Zig": 5, "Nim": 4}
    m = build_domain_slug_map(weights, keep=4, unit="tokens")
    assert m.kept == ("Python", "C#", "C++", "Rust")
    assert m.folded == ("Go", "Zig", "Nim")
    assert m.apply("C#") == "c-sharp"
    assert m.apply("Go") == m.apply("Zig") == m.apply("Nim") == OTHER_SEGMENT
    assert m.segments == ("c-plus-plus", "c-sharp", "other", "python", "rust")


def test_the_default_keep_is_twenty_because_cardinality_is_permanent():
    """73 gha_language values and ~180 StackExchange sites are the verified tails; every
    distinct value is a directory inside manifest_sha256 forever."""
    assert DEFAULT_DOMAIN_KEEP == 20
    m = build_domain_slug_map({f"lang{i:03d}": 1000 - i for i in range(73)})
    assert len(m.kept) == 20
    assert len(m.folded) == 53
    assert len(m.segments) == 21  # 20 named + `other`


def test_a_collision_between_two_upstream_values_RAISES():
    """The single most important property. Two values in one directory is unfixable after
    publish, and nothing downstream can see it: the counts still add up."""
    with pytest.raises(SlugError) as e:
        build_domain_slug_map({"C#": 10, "C sharp": 9}, keep=5)
    msg = str(e.value)
    assert "COLLISION" in msg
    assert "'C#'" in msg and "'C sharp'" in msg, "both raw values must be named"
    assert "overrides" in msg, "the message must state the fix"


def test_case_only_and_host_only_differences_also_collide_loudly():
    for weights in ({"Python": 9, "python": 8}, {"foo.com": 9, "foo.org": 8}):
        with pytest.raises(SlugError) as e:
            build_domain_slug_map(weights, keep=5)
        assert "COLLISION" in str(e.value)


def test_an_override_resolves_a_collision():
    """The documented escape hatch has to actually work, or the error is a dead end."""
    m = build_domain_slug_map(
        {"C#": 10, "C sharp": 9}, keep=5, overrides={"C sharp": "c-sharp-prose"}
    )
    assert m.apply("C#") == "c-sharp"
    assert m.apply("C sharp") == "c-sharp-prose"


def test_a_real_value_slugging_to_the_fold_target_is_refused():
    """One directory meaning both 'the value literally named other' and 'the tail'."""
    with pytest.raises(SlugError) as e:
        build_domain_slug_map({"Other": 10, "Python": 9, "Rust": 8}, keep=2)
    assert "fold-target segment" in str(e.value)


def test_the_fold_is_the_only_many_to_one_and_it_is_itemised():
    """`other` merges, which is why every folded value is listed rather than summarised."""
    m = build_domain_slug_map({"a": 5, "b": 4, "c": 3, "d": 2}, keep=1)
    assert m.folded == ("b", "c", "d")
    assert [v for v in m.folded if v in m.readme_table()] == ["b", "c", "d"]


def test_a_tail_of_exactly_one_is_kept_rather_than_folded():
    """`other` holding one value costs the same directory and destroys the value's name."""
    m = build_domain_slug_map({"a": 3, "b": 2, "c": 1}, keep=2)
    assert m.folded == ()
    assert m.kept == ("a", "b", "c")
    assert OTHER_SEGMENT not in m.segments


# ---- determinism, and the published mapping ----


def test_the_map_is_deterministic_under_reordered_input_and_ties():
    """A map that depends on dict order cannot be published: re-running the build would move
    segments that are already inside manifest_sha256."""
    values = {f"lang{i}": 10 for i in range(30)}  # ALL tied, so only the tiebreak orders them
    forward = build_domain_slug_map(dict(sorted(values.items())), keep=5)
    reverse = build_domain_slug_map(dict(sorted(values.items(), reverse=True)), keep=5)
    assert forward.kept == reverse.kept
    assert forward.slug_of == reverse.slug_of
    assert forward.segments == reverse.segments


def test_the_mapping_is_returned_so_a_slug_is_reversible():
    """Without this table `c-sharp` is unreadable — nobody can show it means C# and not CSharp."""
    m = build_domain_slug_map({"C#": 10, "C++": 9, "Jupyter Notebook": 8}, keep=3, unit="tokens")
    assert {v: k for k, v in m.slug_of.items()}["c-sharp"] == "C#"
    assert m.to_dict()["slug_of"]["Jupyter Notebook"] == "jupyter-notebook"


def test_the_readme_table_carries_the_reverse_mapping_and_the_fold_cost():
    m = build_domain_slug_map({"C#": 900, "C++": 80, "Go": 10, "Zig": 10}, keep=2, unit="tokens")
    table = m.readme_table()
    assert "`c-sharp` | `C#`" in table
    assert "`other`" in table and "`Go`" in table and "`Zig`" in table
    assert f"{m.folded_fraction * 100:.2f}%" in table
    assert "tokens" in table, "the ranking unit must be stated; docs and tokens rank differently"


def test_folded_fraction_is_what_says_whether_keep_was_set_well():
    m = build_domain_slug_map({"big": 980, "a": 10, "b": 5, "c": 5}, keep=1, unit="tokens")
    assert m.kept_weight == 980 and m.folded_weight == 20
    assert m.folded_fraction == pytest.approx(0.02)


def test_a_value_the_map_never_saw_raises_instead_of_folding_silently():
    """It means the counts and the data disagree; hiding that under `other` writes a permanent
    directory to cover it up."""
    m = build_domain_slug_map({"a": 3, "b": 2, "c": 1}, keep=1)
    with pytest.raises(SlugError) as e:
        m.apply("surprise")
    assert "not in this slug map" in str(e.value)


@pytest.mark.parametrize("bad", [{}, {"a": -1}, {"a": "lots"}, {"": 5}])
def test_a_malformed_weight_table_is_refused(bad):
    with pytest.raises(SlugError):
        build_domain_slug_map(bad, keep=3)


@pytest.mark.parametrize("keep", [0, -1, True, 1.5])
def test_a_malformed_keep_is_refused(keep):
    with pytest.raises(SlugError):
        build_domain_slug_map({"a": 1, "b": 2}, keep=keep)


def test_every_segment_a_real_reservoir_map_produces_is_publishable():
    """End to end on the two real vocabularies: 73 languages folded, ~180 SE sites folded, and
    every surviving segment round-trips through all three consumers."""
    import fnmatch
    from urllib.parse import urlparse

    languages = {
        "Python": 900, "C#": 800, "C++": 700, "Jupyter Notebook": 600, "Java": 500,
        "JavaScript": 400, "Go": 300, "Rust": 200, "Objective-C++": 100, "F*": 90,
        "Emacs Lisp": 80, "Vim script": 70, "Shell": 60, "TypeScript": 50, "Ruby": 40,
        **{f"Lang{i}": 10 - (i % 5) for i in range(58)},
    }
    sites = {
        "mathoverflow.stackexchange.com": 900, "physics.stackexchange.com": 800,
        "3dprinting.stackexchange.com": 700,
        **{f"site{i:03d}.stackexchange.com": 100 - (i % 7) for i in range(177)},
    }
    for weights in (languages, sites):
        m = build_domain_slug_map(weights, unit="tokens")
        assert len(m.segments) == DEFAULT_DOMAIN_KEEP + 1
        assert len(set(m.slug_of.values())) == len(m.segments)
        for segment in m.segments:
            rel = f"tokens/stackv2-edu/{segment}/train-00000.u32le.bin"
            key = f"pretrain/reservoir-260b/v1/{rel}"
            assert urlparse(f"s3://edullm-data/{key}").path == f"/{key}"
            assert fnmatch.fnmatch(rel, rel)
            assert labels_from_path(rel)["domain"] == segment
