"""Shared JSONL document parsing for publish() and text-corpus/v1.

Publisher and validator must agree on what a "row" is: a non-blank line that parses as a
JSON object. Raw newline counts disagree with that definition (no terminal newline, blank
lines), so every call site goes through this module.

Streaming: payloads are read in bounded chunks via ``s3.get_range`` (and gunzipped via
``gzip.GzipFile`` when needed). Callers that only need a count never materialize the row
list; profile checks that need per-row fields iterate once.
"""

from __future__ import annotations

import gzip
import io
import json
from collections.abc import Iterator
from typing import Any

from .s3 import S3

_CHUNK = 8 * 1024 * 1024
# A record must fit in memory because json.loads consumes a complete JSON value. This is
# deliberately a generous document limit, while ensuring an attacker cannot turn a missing
# newline into unbounded publisher / Gate A memory use.
MAX_JSONL_RECORD_BYTES = 16 * 1024 * 1024


class _ChunkReader(io.RawIOBase):
    """File-like over successive ``get_range`` chunks — never holds the whole object.

    Implements ``readinto`` (not only ``read``) so ``io.BufferedReader`` / ``gzip.GzipFile``
    work: those call ``readinto`` on the raw stream, and RawIOBase's default raises
    ``NotImplementedError``.
    """

    def __init__(self, s3: S3, bucket: str, key: str) -> None:
        super().__init__()
        self._s3 = s3
        self._bucket = bucket
        self._key = key
        self._size = int(s3.head(bucket, key)["size"])
        self._pos = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:  # noqa: ANN001 - io API buffer
        view = memoryview(b).cast("B")
        if not view:
            return 0
        if self._pos >= self._size:
            return 0
        data = self._s3.get_range(
            self._bucket,
            self._key,
            self._pos,
            min(len(view), self._size - self._pos),
        )
        n = len(data)
        view[:n] = data
        self._pos += n
        return n

    def read(self, size: int = -1) -> bytes:  # noqa: A003 - io API
        # RawIOBase.read() routes through readinto(), so this does not keep and slice a
        # retained tail. An explicit unbounded read remains the caller's opt-in API choice.
        return super().read(size)


def _line_iter_from_binary(
    stream: io.BufferedIOBase, *, max_record_bytes: int = MAX_JSONL_RECORD_BYTES
) -> Iterator[tuple[int, bytes]]:
    """Yield ``(1-based line_no, raw_line_without_newline)`` in linear time.

    ``cursor`` advances through the bytearray without copying its unconsumed suffix per
    record. The only tail copy happens once per incoming chunk, after all complete records
    in that chunk were yielded.
    """
    line_no = 0
    buf = bytearray()
    cursor = 0
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        while True:
            i = buf.find(b"\n", cursor)
            if i < 0:
                break
            record_bytes = i - cursor
            if record_bytes > max_record_bytes:
                raise ValueError(
                    f"line {line_no + 1}: JSONL record exceeds "
                    f"{max_record_bytes} byte limit"
                )
            line_no += 1
            yield line_no, bytes(buf[cursor:i])
            cursor = i + 1
        # Do not retain a consumed prefix. This is one copy per chunk, rather than one
        # immutable-buffer slice per short JSONL document.
        if cursor:
            del buf[:cursor]
            cursor = 0
        if len(buf) > max_record_bytes:
            raise ValueError(
                f"line {line_no + 1}: JSONL record exceeds {max_record_bytes} byte limit"
            )
    if buf:
        line_no += 1
        yield line_no, bytes(buf)


def iter_jsonl_objects_from_stream(stream: io.BufferedIOBase) -> Iterator[dict[str, Any]]:
    """Parse JSONL from a binary stream: skip blank lines; require JSON objects."""
    for line_no, raw in _line_iter_from_binary(stream):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"line {line_no}: invalid JSON ({e})") from e
        if not isinstance(obj, dict):
            raise ValueError(
                f"line {line_no}: document must be a JSON object, got {type(obj).__name__}"
            )
        yield obj


def iter_jsonl_objects_from_bytes(body: bytes, *, gzipped: bool = False) -> Iterator[dict[str, Any]]:
    """Parse JSONL from an in-memory body (tests / small objects)."""
    if gzipped:
        body = gzip.decompress(body)
    yield from iter_jsonl_objects_from_stream(io.BytesIO(body))


def count_jsonl_objects_from_bytes(body: bytes, *, gzipped: bool = False) -> int:
    return sum(1 for _ in iter_jsonl_objects_from_bytes(body, gzipped=gzipped))


def open_jsonl_s3(s3: S3, bucket: str, key: str, *, gzipped: bool) -> io.BufferedIOBase:
    """Return a binary stream of decompressed JSONL bytes for ``key``."""
    raw = _ChunkReader(s3, bucket, key)
    if gzipped:
        # gzip.GzipFile asks for tiny header / trailer reads. Coalesce those onto the same
        # 8 MiB transfer granularity as the JSONL parser; a default 8 KiB buffer would turn a
        # GiB compressed corpus into roughly 100k S3 range GETs.
        return gzip.GzipFile(  # type: ignore[return-value]
            fileobj=io.BufferedReader(raw, buffer_size=_CHUNK),
            mode="rb",
        )
    return io.BufferedReader(raw, buffer_size=_CHUNK)


def iter_jsonl_objects_s3(s3: S3, bucket: str, key: str, *, gzipped: bool) -> Iterator[dict[str, Any]]:
    """Stream-parse JSONL documents from S3 without holding the whole object decoded."""
    with open_jsonl_s3(s3, bucket, key, gzipped=gzipped) as stream:
        yield from iter_jsonl_objects_from_stream(stream)


def count_jsonl_objects_s3(s3: S3, bucket: str, key: str, *, gzipped: bool) -> int:
    """Count JSONL documents via a streaming parse (bounded memory)."""
    return sum(1 for _ in iter_jsonl_objects_s3(s3, bucket, key, gzipped=gzipped))


def path_is_jsonl(path: str) -> bool:
    lower = path.lower()
    return lower.endswith(".jsonl") or lower.endswith(".jsonl.gz")


def path_is_jsonl_gz(path: str) -> bool:
    return path.lower().endswith(".jsonl.gz")


def is_jsonl_path(path: str) -> tuple[bool, bool]:
    """Return ``(is_jsonl, gzipped)`` for a payload path."""
    lower = path.lower()
    if lower.endswith(".jsonl.gz"):
        return True, True
    if lower.endswith(".jsonl"):
        return True, False
    return False, False
