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
from collections.abc import Iterator, Mapping
from typing import Any

from .s3 import S3

_CHUNK = 8 * 1024 * 1024


class _ChunkReader(io.RawIOBase):
    """File-like over successive ``get_range`` chunks — never holds the whole object.

    Implements ``readinto`` (not only ``read``) so ``io.BufferedReader`` / ``gzip.GzipFile``
    work: those call ``readinto`` on the raw stream, and RawIOBase's default raises
    ``NotImplementedError``.
    """

    def __init__(self, s3: S3, bucket: str, key: str, *, chunk_size: int = _CHUNK) -> None:
        super().__init__()
        self._s3 = s3
        self._bucket = bucket
        self._key = key
        self._chunk_size = chunk_size
        self._size = int(s3.head(bucket, key)["size"])
        self._pos = 0
        self._buf = b""

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:  # noqa: ANN001 - io API buffer
        view = memoryview(b).cast("B")
        if not view:
            return 0
        data = self.read(len(view))
        n = len(data)
        view[:n] = data
        return n

    def read(self, size: int = -1) -> bytes:  # noqa: A003 - io API
        if size == 0:
            return b""
        if size is None or size < 0:
            parts = [self._buf]
            self._buf = b""
            while self._pos < self._size:
                n = min(self._chunk_size, self._size - self._pos)
                parts.append(self._s3.get_range(self._bucket, self._key, self._pos, n))
                self._pos += n
            return b"".join(parts)
        while len(self._buf) < size and self._pos < self._size:
            n = min(self._chunk_size, self._size - self._pos)
            self._buf += self._s3.get_range(self._bucket, self._key, self._pos, n)
            self._pos += n
        out, self._buf = self._buf[:size], self._buf[size:]
        return out


def _line_iter_from_binary(stream: io.BufferedIOBase) -> Iterator[tuple[int, bytes]]:
    """Yield ``(1-based line_no, raw_line_without_newline)`` from a binary stream."""
    line_no = 0
    buf = b""
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        buf += chunk
        while True:
            i = buf.find(b"\n")
            if i < 0:
                break
            line_no += 1
            yield line_no, buf[:i]
            buf = buf[i + 1 :]
    if buf:
        line_no += 1
        yield line_no, buf


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
        # GzipFile needs a buffered file-like; wrap the raw chunk reader.
        return gzip.GzipFile(fileobj=io.BufferedReader(raw), mode="rb")  # type: ignore[return-value]
    return io.BufferedReader(raw)


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
