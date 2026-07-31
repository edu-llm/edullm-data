"""Tests for the four v1 dataset profiles (§4, §7).

Each profile check must RECOMPUTE something against the bytes (§0.4), so every fixture here
builds real byte payloads (numpy ``.tobytes()`` for token/order vectors, real jsonl/csv text
for eval/sft) and seeds them into a :class:`~edullm_data.s3.FakeS3`. The passing fixture must
be clean; the broken fixture must make exactly the target check fire.

Convention for the full object key: ``GroupContext.prefix`` is the group's landing prefix and
a manifest ``entry.path`` is the group-relative key that already carries the group name (§5:
``tokens/train-00000.u32le.bin``). The profile helpers join them as ``prefix.rstrip('/') +
'/' + path``; these tests mirror that so the decode paths read the bytes actually seeded.
"""

from __future__ import annotations

import gzip
import hashlib

import numpy as np
import pytest

from edullm_data.manifest import Format, ManifestEntry, build_manifest
from edullm_data.profiles import (
    eval_results_v1,
    pretrain_tokens_v1,
    sft_conversations_v1,
    token_order_v1,
)
from edullm_data.profiles.base import GroupContext, Violation
from edullm_data.s3 import FakeS3

BUCKET = "edullm-landing"
HEX = "a" * 64


# ======================================================================================
# shared helpers
# ======================================================================================


def _seed_and_ctx(
    *,
    prefix: str,
    group: dict,
    entries: list[ManifestEntry],
    bodies: dict[str, bytes],
    group_name: str = "g",
    family_defaults: dict | None = None,
) -> GroupContext:
    """Seed ``bodies`` (keyed by manifest-relative path) into a FakeS3 and build the
    GroupContext a profile check receives. The manifest is built by the real
    ``build_manifest`` so ``entries`` are dicts exactly as a check sees them in production."""
    s3 = FakeS3()
    for path, body in bodies.items():
        full = (prefix.rstrip("/") + "/" + path.lstrip("/")) if prefix else path
        s3.seed(BUCKET, full, body)
    manifest = build_manifest(entries, group_name=group_name)
    return GroupContext(
        dataset_id="pretrain/x",
        version="v1",
        landing_bucket=BUCKET,
        prefix=prefix,
        group=group,
        manifest=manifest,
        s3=s3,
        rng_seed=hashlib.sha256(b"seed").hexdigest()[:16],
        family_defaults=family_defaults or {},
    )


def _codes(violations: list[Violation]) -> set[str]:
    return {v.code for v in violations}


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


# ======================================================================================
# pretrain-tokens/v1
# ======================================================================================

VOCAB = 50000


def _tok_entry(path: str, body: bytes, *, dtype: str = "uint32", count_unit: str = "tokens") -> ManifestEntry:
    n = len(body) // Format.for_tokens(dtype).dtype_size
    return ManifestEntry(
        path=path,
        sha256=_sha(body),
        bytes=len(body),
        count={"unit": count_unit, "value": n},
        format=Format.for_tokens(dtype),
    )


def _tok_group(**overrides) -> dict:
    g = {
        "profile": pretrain_tokens_v1.NAME,
        "tokenizer": {
            "repo_id": "allenai/dolma2-tok",
            "revision": "main",
            "fingerprint_sha256": "f" * 64,
            "vocab_size": VOCAB,
            "eos_token_id": 1,
        },
    }
    g.update(overrides)
    return g


def test_pretrain_clean_uint32_shard_passes():
    # A realistic, varied uint32 token stream well inside the vocab.
    ids = (np.arange(200_000, dtype=np.uint32) % (VOCAB - 2)) + 2
    body = ids.tobytes()
    entry = _tok_entry("tokens/train-00000.u32le.bin", body)
    ctx = _seed_and_ctx(prefix="tokens", group=_tok_group(), entries=[entry],
                        bodies={"tokens/train-00000.u32le.bin": body})
    out: list[Violation] = []
    for check in pretrain_tokens_v1.CHECKS:
        out += check(ctx)
    assert out == [], f"clean shard should pass, got {[str(v) for v in out]}"


def test_pretrain_all_zeros_shard_fails_distinct_and_zero_run():
    body = np.zeros(200_000, dtype=np.uint32).tobytes()
    entry = _tok_entry("tokens/train-00000.u32le.bin", body)
    ctx = _seed_and_ctx(prefix="tokens", group=_tok_group(), entries=[entry],
                        bodies={"tokens/train-00000.u32le.bin": body})
    codes = _codes(pretrain_tokens_v1.check_decode_smoke(ctx))
    assert "distinct-too-few" in codes
    assert "zero-run-in-shard" in codes


def test_pretrain_string_bounds_are_rejected_and_cannot_weaken_decode_checks():
    """Numeric-looking JSON strings used to skip the clamp and disable all-zero detection."""
    body = np.zeros(200_000, dtype=np.uint32).tobytes()
    entry = _tok_entry("tokens/train-00000.u32le.bin", body)
    ctx = _seed_and_ctx(
        prefix="tokens",
        group=_tok_group(
            min_distinct_ids="0",
            max_eos_fraction="1.0",
            max_zero_run="100000000",
        ),
        entries=[entry],
        bodies={"tokens/train-00000.u32le.bin": body},
    )

    config = pretrain_tokens_v1.check_decode_bound_configuration(ctx)
    assert len(config) == 3
    assert _codes(config) == {"invalid-decode-bound"}

    codes = _codes(pretrain_tokens_v1.check_decode_smoke(ctx))
    assert {"distinct-too-few", "zero-run-in-shard"} <= codes


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("min_distinct_ids", True),
        ("max_eos_fraction", float("nan")),
        ("max_zero_run", 1.5),
    ],
)
def test_pretrain_invalid_bound_values_are_rejected_without_weakening_checks(key, value):
    body = np.zeros(200_000, dtype=np.uint32).tobytes()
    entry = _tok_entry("tokens/train-00000.u32le.bin", body)
    ctx = _seed_and_ctx(
        prefix="tokens",
        group=_tok_group(**{key: value}),
        entries=[entry],
        bodies={"tokens/train-00000.u32le.bin": body},
    )

    assert _codes(pretrain_tokens_v1.check_decode_bound_configuration(ctx)) == {
        "invalid-decode-bound"
    }
    assert {"distinct-too-few", "zero-run-in-shard"} <= _codes(
        pretrain_tokens_v1.check_decode_smoke(ctx)
    )


def test_pretrain_uint16_bytes_declared_uint32_fails_vocab_range():
    # Bytes are genuinely uint16 token ids (all valid < vocab as uint16), but the manifest
    # declares uint32 — decoding as uint32 packs pairs of uint16 into huge ids past vocab.
    u16 = ((np.arange(400_000, dtype=np.uint16) % (VOCAB - 2)) + 2)
    body = u16.tobytes()
    # Declare uint32 (the lie). Byte length is a multiple of 4, so it parses as uint32.
    entry = _tok_entry("tokens/train-00000.u32le.bin", body, dtype="uint32")
    ctx = _seed_and_ctx(prefix="tokens", group=_tok_group(), entries=[entry],
                        bodies={"tokens/train-00000.u32le.bin": body})
    codes = _codes(pretrain_tokens_v1.check_decode_smoke(ctx))
    assert "vocab-out-of-range" in codes, "uint16-as-uint32 must send ids past vocab_size"


def test_pretrain_real_npy_header_fails_first_bytes():
    ids = (np.arange(200_000, dtype=np.uint32) % (VOCAB - 2)) + 2
    import io as _io

    buf = _io.BytesIO()
    np.save(buf, ids)  # writes a real \x93NUMPY header
    body = buf.getvalue()
    assert body[:6] == b"\x93NUMPY"
    entry = ManifestEntry(
        path="tokens/train-00000.u32le.bin",
        sha256=_sha(body),
        bytes=len(body),
        count={"unit": "tokens", "value": len(body) // 4},
        format=Format.for_tokens("uint32"),
    )
    ctx = _seed_and_ctx(prefix="tokens", group=_tok_group(), entries=[entry],
                        bodies={"tokens/train-00000.u32le.bin": body})
    codes = _codes(pretrain_tokens_v1.check_first_bytes_not_npy(ctx))
    assert "npy-magic-bytes" in codes


def test_pretrain_all_eos_shard_fails_eos_fraction():
    body = np.ones(200_000, dtype=np.uint32).tobytes()  # every token is eos id 1
    entry = _tok_entry("tokens/train-00000.u32le.bin", body)
    ctx = _seed_and_ctx(prefix="tokens", group=_tok_group(), entries=[entry],
                        bodies={"tokens/train-00000.u32le.bin": body})
    codes = _codes(pretrain_tokens_v1.check_decode_smoke(ctx))
    assert "eos-fraction-out-of-bounds" in codes
    assert "distinct-too-few" in codes  # only one id present


def test_pretrain_missing_token_count_unit_fails():
    body = ((np.arange(200_000, dtype=np.uint32) % (VOCAB - 2)) + 2).tobytes()
    entry = _tok_entry("tokens/train-00000.u32le.bin", body, count_unit="items")
    ctx = _seed_and_ctx(prefix="tokens", group=_tok_group(), entries=[entry],
                        bodies={"tokens/train-00000.u32le.bin": body})
    codes = _codes(pretrain_tokens_v1.check_entries_declare_token_counts(ctx))
    assert "token-count-unit" in codes


def test_pretrain_seq_len_misalignment_fails():
    # 200_001 tokens is not a multiple of seq_len=1024.
    ids = ((np.arange(200_001, dtype=np.uint32) % (VOCAB - 2)) + 2)
    body = ids.tobytes()
    entry = _tok_entry("tokens/train-00000.u32le.bin", body)
    ctx = _seed_and_ctx(prefix="tokens", group=_tok_group(seq_len=1024), entries=[entry],
                        bodies={"tokens/train-00000.u32le.bin": body})
    codes = _codes(pretrain_tokens_v1.check_seq_len_alignment(ctx))
    assert "seq-len-misalignment" in codes


def test_pretrain_seq_len_aligned_passes():
    ids = ((np.arange(1024 * 100, dtype=np.uint32) % (VOCAB - 2)) + 2)
    body = ids.tobytes()
    entry = _tok_entry("tokens/train-00000.u32le.bin", body)
    ctx = _seed_and_ctx(prefix="tokens", group=_tok_group(seq_len=1024), entries=[entry],
                        bodies={"tokens/train-00000.u32le.bin": body})
    assert pretrain_tokens_v1.check_seq_len_alignment(ctx) == []


def test_pretrain_tokenizer_can_come_from_family_defaults():
    ids = ((np.arange(200_000, dtype=np.uint32) % (VOCAB - 2)) + 2)
    body = ids.tobytes()
    entry = _tok_entry("tokens/train-00000.u32le.bin", body)
    group = {"profile": pretrain_tokens_v1.NAME}  # no tokenizer on the group
    ctx = _seed_and_ctx(
        prefix="tokens", group=group, entries=[entry],
        bodies={"tokens/train-00000.u32le.bin": body},
        family_defaults={"tokenizer": {"vocab_size": VOCAB, "eos_token_id": 1}},
    )
    assert pretrain_tokens_v1.check_decode_smoke(ctx) == []


# ======================================================================================
# eval-results/v1
# ======================================================================================


def _jsonl(rows: list[dict]) -> bytes:
    import json as _json

    return ("\n".join(_json.dumps(r) for r in rows) + "\n").encode("utf-8")


def _eval_entry(path: str, body: bytes) -> ManifestEntry:
    fmt = Format(
        container="jsonl" if "jsonl" in path else "csv",
        codec="gzip" if path.endswith(".gz") else "none",
    )
    return ManifestEntry(path=path, sha256=_sha(body), bytes=len(body), count=None, format=fmt)


def _eval_group(status_counts: dict, **overrides) -> dict:
    g = {
        "profile": eval_results_v1.NAME,
        "model": {"id": "m", "revision": "r"},
        "task": "arc",
        "decode": {"temperature": 0.0, "top_p": 1.0, "max_tokens": 128},
        "status_counts": status_counts,
    }
    g.update(overrides)
    return g


def test_eval_healthy_file_passes():
    rows = [{"status": "ok", "score": i % 2} for i in range(10)] + [{"status": "error", "score": 0}]
    body = _jsonl(rows)
    entry = _eval_entry("rows-00000.jsonl", body)
    group = _eval_group({"ok": 10, "error": 1, "filtered": 0}, metric_field="score")
    ctx = _seed_and_ctx(prefix="", group=group, entries=[entry], bodies={"rows-00000.jsonl": body})
    out: list[Violation] = []
    for check in eval_results_v1.CHECKS:
        out += check(ctx)
    assert out == [], f"healthy eval should pass, got {[str(v) for v in out]}"


def test_eval_header_only_csv_fails_row_count():
    # 66-byte header-only CSV: header row only, zero data rows. status_counts claims rows.
    body = b"id,status,score\n"
    entry = _eval_entry("rows-00000.csv", body)
    group = _eval_group({"ok": 12, "error": 0, "filtered": 0})
    ctx = _seed_and_ctx(prefix="", group=group, entries=[entry], bodies={"rows-00000.csv": body})
    codes = _codes(eval_results_v1.check_row_count_accounting(ctx))
    assert "row-count-mismatch" in codes
    # The header-only file has ZERO rows, so the all-error refusal (which requires
    # total_rows > 0) correctly does NOT fire — row-count is the check that catches it.
    # This is the exact division of labor §7 describes between the two checks.
    assert eval_results_v1.check_refuse_all_error(ctx) == []


def test_eval_all_error_file_fails_refusal_but_passes_row_count():
    # The audit's 3 all-error files: honest nonzero n_rows, every row an error.
    rows = [{"status": "error", "score": 0} for _ in range(500)]
    body = _jsonl(rows)
    entry = _eval_entry("rows-00000.jsonl", body)
    group = _eval_group({"ok": 0, "error": 500, "filtered": 0})
    ctx = _seed_and_ctx(prefix="", group=group, entries=[entry], bodies={"rows-00000.jsonl": body})
    # Row-count accounting is HONEST here (500 == 0+500+0), so it must NOT fire.
    assert eval_results_v1.check_row_count_accounting(ctx) == []
    # The all-error refusal is the check that catches this file.
    assert "all-error-no-success" in _codes(eval_results_v1.check_refuse_all_error(ctx))


def test_eval_constant_metric_fails():
    rows = [{"status": "ok", "score": 1.0} for _ in range(20)]
    body = _jsonl(rows)
    entry = _eval_entry("rows-00000.jsonl", body)
    group = _eval_group({"ok": 20, "error": 0, "filtered": 0}, metric_field="score")
    ctx = _seed_and_ctx(prefix="", group=group, entries=[entry], bodies={"rows-00000.jsonl": body})
    assert "metric-all-identical" in _codes(eval_results_v1.check_metric_not_constant(ctx))


def test_eval_gzip_jsonl_is_read():
    rows = [{"status": "ok", "score": i} for i in range(5)]
    body = gzip.compress(_jsonl(rows))
    entry = _eval_entry("rows-00000.jsonl.gz", body)
    group = _eval_group({"ok": 5, "error": 0, "filtered": 0})
    ctx = _seed_and_ctx(prefix="", group=group, entries=[entry], bodies={"rows-00000.jsonl.gz": body})
    assert eval_results_v1.check_row_count_accounting(ctx) == []
    assert eval_results_v1.check_refuse_all_error(ctx) == []


def test_eval_custom_status_field_and_value():
    rows = [{"finish_reason": "stop"} for _ in range(4)]
    body = _jsonl(rows)
    entry = _eval_entry("rows-00000.jsonl", body)
    group = _eval_group(
        {"stop": 4, "error": 0, "filtered": 0},
        status_field="finish_reason",
        status_ok_value="stop",
    )
    ctx = _seed_and_ctx(prefix="", group=group, entries=[entry], bodies={"rows-00000.jsonl": body})
    assert eval_results_v1.check_refuse_all_error(ctx) == []
    assert eval_results_v1.check_row_count_accounting(ctx) == []


# ======================================================================================
# token-order/v1
# ======================================================================================

N_BLOCKS = 4096


def _order_entry(path: str, body: bytes) -> ManifestEntry:
    return ManifestEntry(
        path=path,
        sha256=_sha(body),
        bytes=len(body),
        count={"unit": "indices", "value": len(body) // 4},
        format=Format(container="raw", dtype="uint32", byte_order="little"),
    )


def _order_group(**overrides) -> dict:
    g = {
        "profile": token_order_v1.NAME,
        "depends_on": [{"dataset_id": "pretrain/pool", "version": "v1", "block_count": N_BLOCKS}],
    }
    g.update(overrides)
    return g


def test_token_order_permutation_passes():
    rng = np.random.default_rng(0)
    perm = rng.permutation(N_BLOCKS).astype("<u4")
    body = perm.tobytes()
    entry = _order_entry("order-00000.u32le.bin", body)
    ctx = _seed_and_ctx(prefix="order", group=_order_group(ordering="permutation"), entries=[entry],
                        bodies={"order-00000.u32le.bin": body})
    out: list[Violation] = []
    for check in token_order_v1.CHECKS:
        out += check(ctx)
    assert out == [], f"a real permutation should pass, got {[str(v) for v in out]}"


def test_token_order_all_zeros_fails_bincount():
    body = np.zeros(N_BLOCKS, dtype="<u4").tobytes()  # 4096 copies of block 0
    entry = _order_entry("order-00000.u32le.bin", body)
    ctx = _seed_and_ctx(prefix="order", group=_order_group(ordering="permutation"), entries=[entry],
                        bodies={"order-00000.u32le.bin": body})
    assert "permutation-not-bijective" in _codes(token_order_v1.check_order_domain(ctx))


def test_token_order_out_of_range_index_fails():
    perm = np.arange(N_BLOCKS, dtype="<u4")
    perm[10] = N_BLOCKS + 5  # one index past the parent's block_count
    body = perm.tobytes()
    entry = _order_entry("order-00000.u32le.bin", body)
    ctx = _seed_and_ctx(prefix="order", group=_order_group(ordering="permutation"), entries=[entry],
                        bodies={"order-00000.u32le.bin": body})
    assert "order-index-out-of-range" in _codes(token_order_v1.check_order_domain(ctx))


def test_token_order_length_arithmetic_fails_on_ragged_bytes():
    body = b"\x00\x00\x00\x00\x01\x00\x00"  # 7 bytes, not a multiple of 4
    entry = ManifestEntry(
        path="order-00000.u32le.bin",
        sha256=_sha(body),
        bytes=len(body),
        count=None,
        format=Format(container="raw", dtype="uint32", byte_order="little"),
    )
    ctx = _seed_and_ctx(prefix="order", group=_order_group(), entries=[entry],
                        bodies={"order-00000.u32le.bin": body})
    assert "order-bytes-not-multiple" in _codes(token_order_v1.check_order_length_arithmetic(ctx))


def test_token_order_subset_unique_passes_but_dup_fails():
    # subset: unique in-range indices pass.
    subset = np.array([5, 10, 200, 3000], dtype="<u4")
    body = subset.tobytes()
    entry = _order_entry("order-00000.u32le.bin", body)
    ctx = _seed_and_ctx(prefix="order", group=_order_group(ordering="subset"), entries=[entry],
                        bodies={"order-00000.u32le.bin": body})
    assert token_order_v1.check_order_domain(ctx) == []

    dup = np.array([5, 5, 200, 3000], dtype="<u4")
    body2 = dup.tobytes()
    entry2 = _order_entry("order-00000.u32le.bin", body2)
    ctx2 = _seed_and_ctx(prefix="order", group=_order_group(ordering="subset"), entries=[entry2],
                         bodies={"order-00000.u32le.bin": body2})
    assert "subset-not-unique" in _codes(token_order_v1.check_order_domain(ctx2))


def test_token_order_repeating_allows_duplicates():
    # repeating: duplicates are fine, only range matters.
    rep = np.array([0, 0, 1, 1, 2, 2] * 100, dtype="<u4")
    body = rep.tobytes()
    entry = _order_entry("order-00000.u32le.bin", body)
    ctx = _seed_and_ctx(prefix="order", group=_order_group(ordering="repeating"), entries=[entry],
                        bodies={"order-00000.u32le.bin": body})
    assert token_order_v1.check_order_domain(ctx) == []


def test_token_order_too_large_is_refused_not_oomed():
    # A tiny real body, but a HEAD override reporting an implausible size trips the cap.
    body = np.arange(10, dtype="<u4").tobytes()
    entry = ManifestEntry(
        path="order-00000.u32le.bin",
        sha256=_sha(body),
        bytes=len(body),
        count=None,
        format=Format(container="raw", dtype="uint32", byte_order="little"),
    )
    ctx = _seed_and_ctx(prefix="order", group=_order_group(max_order_bytes=1024), entries=[entry],
                        bodies={"order-00000.u32le.bin": body})
    ctx.s3.override_head(BUCKET, "order/order-00000.u32le.bin", size=2 * 1024 * 1024 * 1024)
    assert "order-too-large" in _codes(token_order_v1.check_order_domain(ctx))


# ======================================================================================
# sft-conversations/v1
# ======================================================================================


def _conv(a: str, b: str) -> dict:
    return {"messages": [{"role": "user", "content": a}, {"role": "assistant", "content": b}]}


def _sft_entry(path: str, body: bytes) -> ManifestEntry:
    fmt = Format(container="jsonl", codec="gzip" if path.endswith(".gz") else "none")
    return ManifestEntry(path=path, sha256=_sha(body), bytes=len(body), count=None, format=fmt)


def _sft_group(**overrides) -> dict:
    g = {
        "profile": sft_conversations_v1.NAME,
        "record_schema": {"messages": [{"role": "str", "content": "str"}]},
        "partitions": [
            {"name": "train", "by": "path", "glob": "train-*.jsonl", "rows": 3},
            {"name": "heldout", "by": "path", "glob": "heldout-*.jsonl", "rows": 2},
        ],
        "dedup": {"method": "sha256-content"},
        "leakage": {"reported_overlap": 0},
    }
    g.update(overrides)
    return g


def test_sft_disjoint_train_heldout_passes():
    train = _jsonl([_conv("q1", "a1"), _conv("q2", "a2"), _conv("q3", "a3")])
    held = _jsonl([_conv("h1", "b1"), _conv("h2", "b2")])
    entries = [_sft_entry("train-00000.jsonl", train), _sft_entry("heldout-00000.jsonl", held)]
    ctx = _seed_and_ctx(prefix="", group=_sft_group(), entries=entries,
                        bodies={"train-00000.jsonl": train, "heldout-00000.jsonl": held})
    out: list[Violation] = []
    for check in sft_conversations_v1.CHECKS:
        out += check(ctx)
    assert out == [], f"disjoint splits should pass, got {[str(v) for v in out]}"


def test_sft_overlapping_train_heldout_fails_leakage():
    shared = _conv("shared-q", "shared-a")
    train = _jsonl([_conv("q1", "a1"), shared, _conv("q3", "a3")])
    held = _jsonl([shared, _conv("h2", "b2")])  # the shared conversation is in both splits
    entries = [_sft_entry("train-00000.jsonl", train), _sft_entry("heldout-00000.jsonl", held)]
    ctx = _seed_and_ctx(prefix="", group=_sft_group(), entries=entries,
                        bodies={"train-00000.jsonl": train, "heldout-00000.jsonl": held})
    assert "train-heldout-leakage" in _codes(sft_conversations_v1.check_train_heldout_leakage(ctx))


def test_sft_malformed_messages_record_fails():
    bad = _jsonl([{"messages": "not-a-list"}])  # messages must be a list of {role, content}
    entries = [_sft_entry("train-00000.jsonl", bad), _sft_entry("heldout-00000.jsonl", _jsonl([_conv("h", "b")]))]
    ctx = _seed_and_ctx(prefix="", group=_sft_group(), entries=entries,
                        bodies={"train-00000.jsonl": bad, "heldout-00000.jsonl": _jsonl([_conv("h", "b")])})
    assert "malformed-messages" in _codes(sft_conversations_v1.check_messages_wellformed(ctx))


def test_sft_message_missing_role_fails():
    bad = _jsonl([{"messages": [{"content": "hi"}]}])  # no role
    entries = [_sft_entry("train-00000.jsonl", bad), _sft_entry("heldout-00000.jsonl", _jsonl([_conv("h", "b")]))]
    ctx = _seed_and_ctx(prefix="", group=_sft_group(), entries=entries,
                        bodies={"train-00000.jsonl": bad, "heldout-00000.jsonl": _jsonl([_conv("h", "b")])})
    assert "malformed-messages" in _codes(sft_conversations_v1.check_messages_wellformed(ctx))


def test_sft_leakage_respects_declared_dedup_key():
    # Same 'id' field in both splits, different conversation text; dedup_key=['id'] catches it.
    train = _jsonl([{"id": "x1", **_conv("q1", "a1")}])
    held = _jsonl([{"id": "x1", **_conv("different", "text")}])
    entries = [_sft_entry("train-00000.jsonl", train), _sft_entry("heldout-00000.jsonl", held)]
    ctx = _seed_and_ctx(prefix="", group=_sft_group(dedup_key=["id"]), entries=entries,
                        bodies={"train-00000.jsonl": train, "heldout-00000.jsonl": held})
    assert "train-heldout-leakage" in _codes(sft_conversations_v1.check_train_heldout_leakage(ctx))


def test_sft_max_leakage_tolerance_allows_bounded_overlap():
    shared = _conv("shared-q", "shared-a")
    train = _jsonl([_conv("q1", "a1"), shared])
    held = _jsonl([shared, _conv("h2", "b2")])
    entries = [_sft_entry("train-00000.jsonl", train), _sft_entry("heldout-00000.jsonl", held)]
    ctx = _seed_and_ctx(prefix="", group=_sft_group(max_leakage=1), entries=entries,
                        bodies={"train-00000.jsonl": train, "heldout-00000.jsonl": held})
    assert sft_conversations_v1.check_train_heldout_leakage(ctx) == []


def test_sft_gzip_rows_are_read():
    train = gzip.compress(_jsonl([_conv("q1", "a1")]))
    held = gzip.compress(_jsonl([_conv("h1", "b1")]))
    group = _sft_group(partitions=[
        {"name": "train", "by": "path", "glob": "train-*.jsonl.gz", "rows": 1},
        {"name": "heldout", "by": "path", "glob": "heldout-*.jsonl.gz", "rows": 1},
    ])
    entries = [_sft_entry("train-00000.jsonl.gz", train), _sft_entry("heldout-00000.jsonl.gz", held)]
    ctx = _seed_and_ctx(prefix="", group=group, entries=entries,
                        bodies={"train-00000.jsonl.gz": train, "heldout-00000.jsonl.gz": held})
    assert sft_conversations_v1.check_messages_wellformed(ctx) == []
    assert sft_conversations_v1.check_train_heldout_leakage(ctx) == []


# ======================================================================================
# registration contract (§ "registry.py may not exist yet")
# ======================================================================================


def test_profiles_expose_the_module_contract():
    for mod in (pretrain_tokens_v1, eval_results_v1, token_order_v1, sft_conversations_v1):
        assert isinstance(mod.NAME, str) and mod.NAME
        assert isinstance(mod.REQUIRED_FIELDS, dict)
        assert isinstance(mod.CHECKS, list) and mod.CHECKS
        for check in mod.CHECKS:
            assert callable(check)


def test_profiles_self_register_if_registry_present():
    # If a concurrent agent has landed registry.py, importing a profile must register it.
    try:
        from edullm_data.profiles import registry
    except Exception:
        pytest.skip("registry.py not present yet — profiles import standalone (guarded)")
    for mod in (pretrain_tokens_v1, eval_results_v1, token_order_v1, sft_conversations_v1):
        assert registry.get_profile(mod.NAME) is mod
