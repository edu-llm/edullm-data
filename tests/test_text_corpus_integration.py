"""publish() → validate_dataset() regressions for text-corpus/v1 and the multi-group docs path.

These catch publisher/validator disagreement and the documented API — unit tests that call
profile checks with a constructed GroupContext do not.
"""

from __future__ import annotations

import gzip
import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from edullm_data import publish as P
from edullm_data import validate as V
from edullm_data.s3 import FakeS3

CREATED = "2026-07-30T18:00:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}
PURPOSE = "Example mix with companion raw documents for 370M ladder runs"


def _tokenizer_json(base_vocab: int = 100256, eos_id: int = 100257) -> bytes:
    doc = {
        "model": {"type": "BPE", "vocab": {str(i): i for i in range(base_vocab)}},
        "added_tokens": [{"id": eos_id, "content": "<|endoftext|>", "special": True}],
    }
    return json.dumps(doc).encode()


def _publish_tokenizer(s3: FakeS3) -> str:
    d = Path(tempfile.mkdtemp())
    (d / "files").mkdir()
    (d / "files" / "tokenizer.json").write_bytes(_tokenizer_json())
    plan = P.publish(
        d,
        dataset_id="tokenizer/dolma2-bpe",
        purpose="Published Dolma2 tokenizer so corpora own the tokenizer they were produced with",
        profile="tokenizer/v1",
        s3=s3,
        created_at=CREATED,
        env=ENV,
    )
    r = V.validate_dataset(
        "edullm-landing", f"tokenizer/dolma2-bpe/{plan.version}", s3, data_bucket="edullm-data"
    )
    assert r.ok, [str(v) for v in r.violations]
    V.promote(r, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")
    return plan.version


def _jsonl_bytes(rows: list[dict], *, terminal_newline: bool = True, blank_between: bool = False) -> bytes:
    parts: list[bytes] = []
    for i, row in enumerate(rows):
        parts.append(json.dumps(row, separators=(",", ":")).encode())
        if i < len(rows) - 1:
            parts.append(b"\n")
            if blank_between:
                parts.append(b"\n")
    if terminal_newline and rows:
        parts.append(b"\n")
    return b"".join(parts)


def _token_shard(n: int = 40000) -> bytes:
    return (np.arange(1, n + 1, dtype=np.uint32) % 90000).tobytes()


def _docs_example_tree(*, text_extra: dict[str, bytes] | None = None) -> Path:
    """Exact layout from USAGE.md multi-group example (train+val tokens and text)."""
    d = Path(tempfile.mkdtemp())
    (d / "tokens" / "src").mkdir(parents=True)
    (d / "text" / "src").mkdir(parents=True)
    (d / "tokens" / "src" / "train-00000.u32le.bin").write_bytes(_token_shard(40000))
    (d / "tokens" / "src" / "val-00000.u32le.bin").write_bytes(_token_shard(13332))
    train_rows = [{"id": f"t{i}", "text": f"train document {i} with prose"} for i in range(5)]
    val_rows = [{"id": f"v{i}", "text": f"val document {i} with prose"} for i in range(3)]
    (d / "text" / "src" / "train-00000.jsonl").write_bytes(_jsonl_bytes(train_rows))
    (d / "text" / "src" / "val-00000.jsonl").write_bytes(_jsonl_bytes(val_rows))
    if text_extra:
        for name, body in text_extra.items():
            (d / "text" / "src" / name).write_bytes(body)
    return d


def test_documented_multigroup_publish_attaches_tokenizer_to_tokens_not_text():
    """USAGE.md call: group_meta only for text; tokenizer= must land on pretrain-tokens/*."""
    s3 = FakeS3()
    _publish_tokenizer(s3)
    src = _docs_example_tree()
    plan = P.publish(
        src,
        dataset_id="pretrain/example-mix",
        purpose=PURPOSE,
        profile={
            "tokens": "pretrain-tokens/v1",
            "text": "text-corpus/v1",
        },
        tokenizer="tokenizer/dolma2-bpe",
        group_meta={
            "text": {"record_schema": {"text": "str", "id": "str"}},
        },
        s3=s3,
        created_at=CREATED,
        env=ENV,
    )
    ds = json.loads(s3.get("edullm-landing", f"{plan.dataset_id}/{plan.version}/dataset.json"))
    by_name = {g["name"]: g for g in ds["groups"]}
    assert "tokens" in by_name and "text" in by_name
    tok_deps = [d for d in by_name["tokens"].get("depends_on", []) if d.get("role") == "tokenizer"]
    assert tok_deps, "tokenizer must attach to the tokens group by resolved profile"
    assert tok_deps[0]["dataset_id"] == "tokenizer/dolma2-bpe"
    text_deps = [d for d in by_name["text"].get("depends_on", []) if d.get("role") == "tokenizer"]
    assert not text_deps, "tokenizer must not attach to the text group via group_meta keys"

    # Companion text group gets JSONL-compatible train/val partitions (not empty token globs).
    text_parts = {p["name"]: p for p in by_name["text"].get("partitions", [])}
    assert "train" in text_parts and "val" in text_parts
    assert text_parts["train"]["glob"].endswith("jsonl*")
    assert text_parts["train"]["rows"] == 5
    assert text_parts["val"]["rows"] == 3

    r = V.validate_dataset(
        "edullm-landing", f"{plan.dataset_id}/{plan.version}", s3, data_bucket="edullm-data"
    )
    assert r.ok, [str(v) for v in r.violations]


@pytest.mark.parametrize(
    "body_factory",
    [
        lambda: _jsonl_bytes(
            [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}],
            terminal_newline=False,
        ),
        lambda: _jsonl_bytes(
            [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}],
            blank_between=True,
        ),
        lambda: gzip.compress(
            _jsonl_bytes([{"id": "a", "text": "gzipped one"}, {"id": "b", "text": "gzipped two"}])
        ),
    ],
    ids=["no-final-newline", "blank-lines", "jsonl-gz"],
)
def test_publish_validate_agree_on_jsonl_row_counts(body_factory):
    s3 = FakeS3()
    _publish_tokenizer(s3)
    body = body_factory()
    name = "train-00000.jsonl.gz" if body[:2] == b"\x1f\x8b" else "train-00000.jsonl"
    d = Path(tempfile.mkdtemp())
    (d / "tokens" / "src").mkdir(parents=True)
    (d / "text" / "src").mkdir(parents=True)
    (d / "tokens" / "src" / "train-00000.u32le.bin").write_bytes(_token_shard(40000))
    (d / "tokens" / "src" / "val-00000.u32le.bin").write_bytes(_token_shard(13332))
    (d / "text" / "src" / name).write_bytes(body)
    (d / "text" / "src" / "val-00000.jsonl").write_bytes(
        _jsonl_bytes([{"id": "v0", "text": "held out document"}])
    )
    plan = P.publish(
        d,
        dataset_id="pretrain/jsonl-count-agree",
        purpose=PURPOSE,
        profile={"tokens": "pretrain-tokens/v1", "text": "text-corpus/v1"},
        tokenizer="tokenizer/dolma2-bpe",
        group_meta={"text": {"record_schema": {"text": "str", "id": "str"}}},
        s3=s3,
        created_at=CREATED,
        env=ENV,
    )
    man = json.loads(s3.get("edullm-landing", f"{plan.dataset_id}/{plan.version}/text/manifest.json"))
    entry = next(e for e in man["entries"] if e["path"].endswith(name))
    assert entry["count"]["unit"] == "rows"
    assert entry["count"]["value"] == 2  # not raw newline count
    r = V.validate_dataset(
        "edullm-landing", f"{plan.dataset_id}/{plan.version}", s3, data_bucket="edullm-data"
    )
    assert r.ok, [str(v) for v in r.violations]


def test_mandatory_text_field_checks_every_row_not_just_first():
    """Former rows_sample=1 only checked the first row of the first shard — later rows must fail."""
    s3 = FakeS3()
    _publish_tokenizer(s3)
    bad_later = _jsonl_bytes(
        [
            {"id": "ok", "text": "first row is fine"},
            {"id": "bad", "text": "   "},  # empty after strip
        ]
    )
    src = _docs_example_tree(text_extra={"train-00001.jsonl": bad_later})
    # overwrite the primary train shard so the bad rows are not only in an extra file —
    # use a single train shard whose second row is blank.
    (src / "text" / "src" / "train-00000.jsonl").write_bytes(bad_later)
    (src / "text" / "src" / "train-00001.jsonl").unlink(missing_ok=True)
    plan = P.publish(
        src,
        dataset_id="pretrain/text-later-rows",
        purpose=PURPOSE,
        profile={"tokens": "pretrain-tokens/v1", "text": "text-corpus/v1"},
        tokenizer="tokenizer/dolma2-bpe",
        group_meta={"text": {"record_schema": {"text": "str", "id": "str"}}},
        s3=s3,
        created_at=CREATED,
        env=ENV,
    )
    r = V.validate_dataset(
        "edullm-landing", f"{plan.dataset_id}/{plan.version}", s3, data_bucket="edullm-data"
    )
    assert not r.ok
    assert "text-field-empty" in {v.code for v in r.violations}


def test_invalid_config_values_rejected():
    s3 = FakeS3()
    _publish_tokenizer(s3)
    src = _docs_example_tree()
    plan = P.publish(
        src,
        dataset_id="pretrain/text-cfg-reject",
        purpose=PURPOSE,
        profile={"tokens": "pretrain-tokens/v1", "text": "text-corpus/v1"},
        tokenizer="tokenizer/dolma2-bpe",
        group_meta={
            "text": {
                "record_schema": {"text": "str", "id": "str"},
                "min_text_chars": 0,
                "text_field": "",
                # out of [0,1] (NaN cannot round-trip canonical_json allow_nan=False)
                "max_identical_fraction": 1.5,
            }
        },
        s3=s3,
        created_at=CREATED,
        env=ENV,
    )
    r = V.validate_dataset(
        "edullm-landing", f"{plan.dataset_id}/{plan.version}", s3, data_bucket="edullm-data"
    )
    codes = {v.code for v in r.violations}
    assert "invalid-min-text-chars" in codes
    assert "invalid-text-field" in codes
    assert "invalid-max-identical-fraction" in codes


def test_repeated_shard_not_diluted_by_healthy_siblings():
    s3 = FakeS3()
    _publish_tokenizer(s3)
    healthy = _jsonl_bytes([{"id": f"h{i}", "text": f"unique {i}"} for i in range(10)])
    stuck = _jsonl_bytes([{"id": f"s{i}", "text": "same"} for i in range(10)])
    d = Path(tempfile.mkdtemp())
    (d / "tokens" / "src").mkdir(parents=True)
    (d / "text" / "src").mkdir(parents=True)
    (d / "tokens" / "src" / "train-00000.u32le.bin").write_bytes(_token_shard(40000))
    (d / "tokens" / "src" / "val-00000.u32le.bin").write_bytes(_token_shard(13332))
    (d / "text" / "src" / "train-00000.jsonl").write_bytes(healthy)
    (d / "text" / "src" / "train-00001.jsonl").write_bytes(stuck)
    (d / "text" / "src" / "val-00000.jsonl").write_bytes(
        _jsonl_bytes([{"id": "v0", "text": "held out"}])
    )
    plan = P.publish(
        d,
        dataset_id="pretrain/text-identical-shard",
        purpose=PURPOSE,
        profile={"tokens": "pretrain-tokens/v1", "text": "text-corpus/v1"},
        tokenizer="tokenizer/dolma2-bpe",
        group_meta={
            "text": {
                "record_schema": {"text": "str", "id": "str"},
                "max_identical_fraction": 0.5,
            }
        },
        s3=s3,
        created_at=CREATED,
        env=ENV,
    )
    r = V.validate_dataset(
        "edullm-landing", f"{plan.dataset_id}/{plan.version}", s3, data_bucket="edullm-data"
    )
    assert not r.ok
    viols = [v for v in r.violations if v.code == "text-all-identical"]
    assert viols
    assert any(v.path and v.path.endswith("train-00001.jsonl") for v in viols)
    assert not any(v.path and v.path.endswith("train-00000.jsonl") for v in viols)
