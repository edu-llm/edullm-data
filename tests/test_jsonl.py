"""Shared JSONL row definition: publish and text-corpus/v1 must count the same way."""

from __future__ import annotations

import gzip
import io
import json

import pytest

from edullm_data import jsonl as J
from edullm_data import publish as P
from edullm_data.s3 import FakeS3


def _obj(i: int) -> dict:
    return {"id": f"d{i}", "text": f"document {i}"}


def test_count_skips_blank_lines():
    body = b'{"id":"a","text":"one"}\n\n\n{"id":"b","text":"two"}\n\n'
    assert J.count_jsonl_objects_from_bytes(body) == 2


def test_count_no_terminal_newline():
    body = b'{"id":"a","text":"one"}'  # one line, no trailing \n
    assert J.count_jsonl_objects_from_bytes(body) == 1
    assert body.count(b"\n") == 0  # raw newline count would be 0 — the old publish bug


def test_count_gzip_roundtrip():
    raw = b"".join(json.dumps(_obj(i)).encode() + b"\n" for i in range(5))
    gz = gzip.compress(raw)
    assert J.count_jsonl_objects_from_bytes(gz, gzipped=True) == 5


def test_stream_count_matches_bytes_via_fakes3():
    """Blank lines + no final newline must agree between bytes and S3 streaming paths."""
    body = b'{"id":"a","text":"one"}\n\n{"id":"b","text":"two"}'  # no final \n
    s3 = FakeS3()
    s3.seed("b", "shard.jsonl", body)
    assert J.count_jsonl_objects_s3(s3, "b", "shard.jsonl", gzipped=False) == 2
    assert J.count_jsonl_objects_from_bytes(body) == 2


def test_stream_gzip_via_fakes3():
    raw = b"".join(json.dumps(_obj(i)).encode() + b"\n" for i in range(3))
    gz = gzip.compress(raw)
    s3 = FakeS3()
    s3.seed("b", "shard.jsonl.gz", gz)
    assert J.count_jsonl_objects_s3(s3, "b", "shard.jsonl.gz", gzipped=True) == 3
    objs = list(J.iter_jsonl_objects_s3(s3, "b", "shard.jsonl.gz", gzipped=True))
    assert [o["id"] for o in objs] == ["d0", "d1", "d2"]


def test_chunked_many_short_records_parse_without_tail_slicing():
    """Short rows spanning many chunks retain only each chunk's unfinished tail."""
    body = b"".join(json.dumps(_obj(i)).encode() + b"\n" for i in range(10_000))

    class SmallChunkReader(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            return super().read(min(size, 127))

    assert sum(1 for _ in J.iter_jsonl_objects_from_stream(SmallChunkReader(body))) == 10_000


def test_record_size_limit_rejects_newline_free_input():
    with pytest.raises(ValueError, match="record exceeds"):
        list(J._line_iter_from_binary(io.BytesIO(b"x" * 33), max_record_bytes=32))


def test_uppercase_jsonl_uses_the_jsonl_format_not_family_raw_fallback():
    fmt = P._format_for(  # noqa: SLF001 - regression for publish's extension inference
        "text/train-00000.JSONL",
        {"format": {"container": "raw", "dtype": "uint32", "byte_order": "little"}},
    )
    assert fmt.container == "jsonl"
