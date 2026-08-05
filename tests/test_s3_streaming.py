"""Real-client multipart behavior for :meth:`Boto3S3.put_stream`.

The ingest flow relies on this lower-level path rather than boto3's opaque transfer helper so
the producer can hash exactly the bytes it uploads.  These tests use a tiny SDK-shaped client,
not credentials or a network connection.
"""

from __future__ import annotations

import io

import pytest

from edullm_data.s3 import Boto3S3, MIN_MULTIPART_PART_BYTES, S3Error


class StubMultipartClient:
    def __init__(self, *, fail_upload_part: int | None = None) -> None:
        self.fail_upload_part = fail_upload_part
        self.created: dict | None = None
        self.uploaded: list[tuple[int, bytes]] = []
        self.completed: dict | None = None
        self.aborted: list[dict] = []
        self.head_calls: list[dict] = []
        self.body = b""

    def create_multipart_upload(self, **kwargs) -> dict:
        self.created = kwargs
        return {"UploadId": "upload-1"}

    def upload_part(self, **kwargs) -> dict:
        if kwargs["PartNumber"] == self.fail_upload_part:
            raise RuntimeError("simulated upload-part failure")
        self.uploaded.append((kwargs["PartNumber"], kwargs["Body"]))
        return {"ETag": f'"part-{kwargs["PartNumber"]}"'}

    def complete_multipart_upload(self, **kwargs) -> None:
        self.completed = kwargs
        self.body = b"".join(body for _, body in self.uploaded)

    def abort_multipart_upload(self, **kwargs) -> None:
        self.aborted.append(kwargs)

    def head_object(self, **kwargs) -> dict:
        self.head_calls.append(kwargs)
        return {
            "ContentLength": len(self.body),
            "ChecksumCRC64NVME": "stub-crc64",
            "ETag": "stub-etag",
            "ContentType": "application/x-ndjson",
        }


class ShortReader(io.BytesIO):
    """A network-like response that may return short reads before EOF."""

    def __init__(self, body: bytes, *, maximum_chunk: int) -> None:
        super().__init__(body)
        self.maximum_chunk = maximum_chunk
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(min(size, self.maximum_chunk))


class FailingReader:
    def __init__(self) -> None:
        self.calls = 0

    def read(self, size: int = -1) -> bytes:
        self.calls += 1
        if self.calls == 1:
            return b"x" * size
        raise OSError("simulated source read failure")


class StubConditionalCopyClient:
    """Small SDK-shaped client for conditional PUT/Copy argument coverage."""

    def __init__(self) -> None:
        self.put_calls: list[dict] = []
        self.copy_calls: list[dict] = []
        self.head_calls: list[dict] = []

    def put_object(self, **kwargs) -> None:
        self.put_calls.append(kwargs)

    def head_object(self, **kwargs) -> dict:
        self.head_calls.append(kwargs)
        return {"ContentLength": 123}

    def copy_object(self, **kwargs) -> None:
        self.copy_calls.append(kwargs)


def test_put_stream_assembles_short_reads_in_valid_parts_and_heads_completed_object():
    payload = b"x" * (MIN_MULTIPART_PART_BYTES + 3)
    client = StubMultipartClient()
    source = ShortReader(payload, maximum_chunk=1024 * 1024)

    head = Boto3S3(client).put_stream(
        "landing",
        "payload.jsonl",
        source,
        part_size=MIN_MULTIPART_PART_BYTES,
        content_type="application/x-ndjson",
    )

    assert client.created == {
        "Bucket": "landing",
        "Key": "payload.jsonl",
        "ContentType": "application/x-ndjson",
    }
    assert [len(body) for _, body in client.uploaded] == [MIN_MULTIPART_PART_BYTES, 3]
    assert [number for number, _ in client.uploaded] == [1, 2]
    assert client.completed is not None
    assert client.completed["MultipartUpload"]["Parts"] == [
        {"ETag": '"part-1"', "PartNumber": 1},
        {"ETag": '"part-2"', "PartNumber": 2},
    ]
    assert client.body == payload
    assert client.aborted == []
    assert all(0 < size <= MIN_MULTIPART_PART_BYTES for size in source.read_sizes)
    assert client.head_calls == [
        {"Bucket": "landing", "Key": "payload.jsonl", "ChecksumMode": "ENABLED"}
    ]
    assert head == {
        "size": len(payload),
        "crc64nvme": "stub-crc64",
        "etag": "stub-etag",
        "content_type": "application/x-ndjson",
    }


def test_conditional_put_and_copy_are_passed_to_s3_not_emulated_by_a_head():
    client = StubConditionalCopyClient()
    s3 = Boto3S3(client)

    s3.put("landing", "reserved/dataset.json", b"{}", if_none_match=True)
    s3.copy(
        "landing",
        "raw/file.jsonl",
        "data",
        "vendor/openai-prm800k/v1/raw/file.jsonl",
        source_etag='"validated-etag"',
    )

    assert client.put_calls == [
        {
            "Bucket": "landing",
            "Key": "reserved/dataset.json",
            "Body": b"{}",
            "IfNoneMatch": "*",
        }
    ]
    assert client.copy_calls == [
        {
            "Bucket": "data",
            "Key": "vendor/openai-prm800k/v1/raw/file.jsonl",
            "CopySource": {"Bucket": "landing", "Key": "raw/file.jsonl"},
            "CopySourceIfMatch": '"validated-etag"',
        }
    ]


@pytest.mark.parametrize("failure", ["upload", "read"])
def test_put_stream_aborts_an_incomplete_multipart_upload(failure: str):
    client = StubMultipartClient(fail_upload_part=2 if failure == "upload" else None)
    source = (
        io.BytesIO(b"x" * (MIN_MULTIPART_PART_BYTES + 1))
        if failure == "upload"
        else FailingReader()
    )

    with pytest.raises(S3Error):
        Boto3S3(client).put_stream(
            "landing", "payload.jsonl", source, part_size=MIN_MULTIPART_PART_BYTES
        )

    assert client.completed is None
    assert client.aborted == [
        {"Bucket": "landing", "Key": "payload.jsonl", "UploadId": "upload-1"}
    ]
