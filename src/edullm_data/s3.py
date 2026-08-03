"""S3 access layer for the validator.

Two implementations behind one Protocol:

* :class:`Boto3S3` — the real client used in the Batch job.
* :class:`FakeS3` — an in-memory store so the validator (and every profile check) is
  testable with no AWS, the same discipline ``contracts.py`` / ``manifest.py`` follow.

The surface is deliberately tiny: exactly the operations Gate A and promotion need, no
more. Everything returns plain Python so a check never has to know which implementation
it holds.

Range reads matter: §7's decode smoke test reads ~64 KB per shard at seeded offsets, and
loading a 512 MiB shard whole to inspect 64 KB of it would make the gate cost scale with
corpus size. ``get_range`` keeps it flat.
"""

from __future__ import annotations

import hashlib
import io
from typing import BinaryIO, Protocol, runtime_checkable


class S3Error(RuntimeError):
    """A read/write against S3 failed in a way the validator should surface, not swallow."""


class NotFound(S3Error):
    """The object or bucket does not exist (404 / NoSuchKey / NoSuchBucket)."""


class PreconditionFailed(S3Error):
    """A conditional object operation did not match the object's current state."""


@runtime_checkable
class S3(Protocol):
    """The operations Gate A and promotion require. Implementations must be safe to call
    concurrently across keys (the validator may fan out HEADs)."""

    def get(self, bucket: str, key: str) -> bytes:
        """Full object body. Raises :class:`NotFound` if absent."""
        ...

    def get_range(self, bucket: str, key: str, start: int, length: int) -> bytes:
        """``length`` bytes starting at ``start`` (0-indexed, inclusive). May return
        fewer bytes than requested if the object ends first. Raises :class:`NotFound`."""
        ...

    def head(self, bucket: str, key: str) -> dict:
        """Object metadata. Guaranteed keys: ``size`` (int). Best-effort keys:
        ``crc64nvme`` (str|None), ``etag`` (str), ``content_type`` (str|None).
        Raises :class:`NotFound` if absent.

        Note the deliberate absence of a ``sha256`` key: for a multipart object S3 stores
        no whole-object SHA-256 (§7), so the validator must recompute or read it from the
        manifest, never trust a server value that may not exist."""
        ...

    def list(self, bucket: str, prefix: str) -> list[dict]:
        """Every object under ``prefix``, paginated internally. Each dict has ``key``
        (str) and ``size`` (int). Order is unspecified — callers compare as sets (§5)."""
        ...

    def hash_object(self, bucket: str, key: str) -> tuple[str, int]:
        """Stream the object and return ``(sha256_hex, size)`` WITHOUT ever holding the
        whole object in memory. This is the primitive that keeps publishing byte-count-
        agnostic: a 633 GB shard is hashed in bounded RAM, and — when this runs on Batch
        in-region — the bytes never leave AWS. Publishing must never load a payload whole
        (the old ``get`` + ``len`` path pulled TB to the caller). Raises :class:`NotFound`."""
        ...

    def put(
        self,
        bucket: str,
        key: str,
        body: bytes,
        *,
        content_type: str | None = None,
        if_none_match: bool = False,
    ) -> None:
        """Write ``body`` at ``key``. Used for ``_REJECTED.json`` on landing and the
        catalog entry on the published bucket — small control objects only.

        ``if_none_match=True`` maps to S3's atomic ``If-None-Match: *`` precondition.  It
        is deliberately part of the small adapter rather than a caller-side ``HEAD`` + PUT
        convention: a landing reservation is a concurrency boundary, and a HEAD cannot make
        a subsequent write create-only.
        """
        ...

    def put_file(self, bucket: str, key: str, local_path: str) -> None:
        """Upload a local file to ``key`` by STREAMING it (multipart, bounded memory) —
        never read the whole file into RAM. Used only to stage a local source directory
        into landing; payload bytes above laptop scale should originate in S3, not here."""
        ...

    def put_stream(
        self,
        bucket: str,
        key: str,
        source: BinaryIO,
        *,
        part_size: int = ...,
        content_type: str | None = None,
    ) -> dict:
        """Stream ``source`` into one object and return its post-upload ``head()`` result.

        This is the source-to-S3 counterpart of :meth:`hash_object`: it bounds buffering to
        one multipart part, never writes a temporary local payload file, and aborts an
        incomplete multipart upload on failure.  Callers that need a source digest wrap the
        readable object and update their hash from ``read()``; no opaque SDK upload helper is
        allowed to consume an unobserved byte stream.
        """
        ...

    def copy(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
        *,
        source_etag: str | None = None,
    ) -> None:
        """Server-side copy — bytes never transit the client (§1 promotion, and publish()'s
        staged→final move). Implementations must use multipart copy above the 5 GB single-part
        ceiling (§9); 8 of the audit's 15 largest objects exceeded it, so this is not optional.

        A non-``None`` ``source_etag`` is an atomic source precondition.  The promotion gate
        uses it for vendored payloads after hashing them in landing: a producer can still mutate
        landing, but cannot race a changed object through the server-side copy.
        """
        ...

    def delete(self, bucket: str, key: str) -> None:
        """Delete one object (current version). Used for best-effort staging cleanup after a
        server-side copy to the final prefix. Not for the airlock-locked read bucket."""
        ...


# --------------------------------------------------------------------------------------
# Real client
# --------------------------------------------------------------------------------------

# CopyObject single-part ceiling (§9). Above this, UploadPartCopy is required.
_MULTIPART_COPY_THRESHOLD = 5 * 1024**3
_COPY_PART_BYTES = (
    256 * 1024**2
)  # 256 MiB parts; 10,000-part cap ⇒ up to ~2.4 TiB/object
_STREAM_PART_BYTES = 16 * 1024**2
# S3 requires every multipart part except the final one to be at least 5 MiB.  Export this
# boundary so callers can reject an invalid configuration before opening a large HTTP response.
MIN_MULTIPART_PART_BYTES = 5 * 1024**2


def _read_stream_part(source: BinaryIO, part_size: int) -> bytes:
    """Read up to one multipart part without assuming a network stream fills ``read(n)``.

    HTTP response objects are allowed to return short reads before EOF.  Accumulating until a
    part is full (or EOF) keeps S3's non-final multipart parts above its 5 MiB minimum while
    holding at most one bounded part in memory.
    """
    chunks = bytearray()
    while len(chunks) < part_size:
        chunk = source.read(part_size - len(chunks))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise TypeError("stream upload source.read() must return bytes")
        chunks.extend(chunk)
    return bytes(chunks)


class Boto3S3:
    """Real S3 via boto3. Constructed with a client so the region/credentials come from
    the Batch task environment (the validator runs as ``<BATCH_JOB_ROLE>``)."""

    def __init__(self, client) -> None:
        self._c = client

    @classmethod
    def default(cls, region: str = "us-east-1") -> "Boto3S3":
        import boto3  # local import so the package imports without boto3 for pure-logic tests

        return cls(boto3.client("s3", region_name=region))

    def _wrap_not_found(self, err: Exception) -> Exception:
        code = getattr(err, "response", {}).get("Error", {}).get("Code")
        status = (
            getattr(err, "response", {})
            .get("ResponseMetadata", {})
            .get("HTTPStatusCode")
        )
        if code in {"NoSuchKey", "NoSuchBucket", "404", "NotFound"} or status == 404:
            return NotFound(str(err))
        if code in {"PreconditionFailed", "412"} or status == 412:
            return PreconditionFailed(str(err))
        return S3Error(str(err))

    def get(self, bucket: str, key: str) -> bytes:
        try:
            return self._c.get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception as e:  # noqa: BLE001 - re-raised as our typed error
            raise self._wrap_not_found(e) from e

    def get_range(self, bucket: str, key: str, start: int, length: int) -> bytes:
        if length <= 0:
            return b""
        rng = f"bytes={start}-{start + length - 1}"
        try:
            return self._c.get_object(Bucket=bucket, Key=key, Range=rng)["Body"].read()
        except Exception as e:  # noqa: BLE001
            raise self._wrap_not_found(e) from e

    def head(self, bucket: str, key: str) -> dict:
        try:
            r = self._c.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
        except Exception as e:  # noqa: BLE001
            raise self._wrap_not_found(e) from e
        return {
            "size": int(r["ContentLength"]),
            "crc64nvme": r.get("ChecksumCRC64NVME"),
            "etag": r.get("ETag"),
            "content_type": r.get("ContentType"),
        }

    def list(self, bucket: str, prefix: str) -> list[dict]:
        out: list[dict] = []
        try:
            paginator = self._c.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for item in page.get("Contents", []):
                    out.append({"key": item["Key"], "size": int(item["Size"])})
        except Exception as e:  # noqa: BLE001
            raise self._wrap_not_found(e) from e
        return out

    def hash_object(self, bucket: str, key: str) -> tuple[str, int]:
        import hashlib

        h = hashlib.sha256()
        size = 0
        try:
            body = self._c.get_object(Bucket=bucket, Key=key)["Body"]
            # stream in 8 MiB chunks — bounded RAM regardless of object size
            for chunk in body.iter_chunks(chunk_size=8 * 1024 * 1024):
                h.update(chunk)
                size += len(chunk)
        except Exception as e:  # noqa: BLE001
            raise self._wrap_not_found(e) from e
        return h.hexdigest(), size

    def put(
        self,
        bucket: str,
        key: str,
        body: bytes,
        *,
        content_type: str | None = None,
        if_none_match: bool = False,
    ) -> None:
        kwargs: dict = {"Bucket": bucket, "Key": key, "Body": body}
        if content_type:
            kwargs["ContentType"] = content_type
        if if_none_match:
            kwargs["IfNoneMatch"] = "*"
        try:
            self._c.put_object(**kwargs)
        except Exception as e:  # noqa: BLE001
            raise self._wrap_not_found(e) from e

    def put_file(self, bucket: str, key: str, local_path: str) -> None:
        # upload_file streams and does multipart automatically — bounded memory
        try:
            self._c.upload_file(local_path, bucket, key)
        except Exception as e:  # noqa: BLE001
            raise S3Error(str(e)) from e

    def put_stream(
        self,
        bucket: str,
        key: str,
        source: BinaryIO,
        *,
        part_size: int = _STREAM_PART_BYTES,
        content_type: str | None = None,
    ) -> dict:
        """Upload a readable stream with one bounded multipart buffer.

        ``upload_fileobj`` would also stream, but it obscures the source reads from the caller
        that is simultaneously computing a provenance hash.  Owning the loop here makes that
        relationship explicit, keeps memory bounded to one part, and lets us abort a failed
        multipart upload instead of relying on a lifecycle rule to clean it up later.
        """
        if part_size < MIN_MULTIPART_PART_BYTES:
            raise ValueError(
                f"part_size must be at least {MIN_MULTIPART_PART_BYTES} bytes for multipart upload"
            )
        create: dict = {"Bucket": bucket, "Key": key}
        if content_type:
            create["ContentType"] = content_type
        try:
            upload = self._c.create_multipart_upload(**create)
            upload_id = upload["UploadId"]
        except Exception as e:  # noqa: BLE001
            raise self._wrap_not_found(e) from e

        parts: list[dict] = []
        try:
            part_number = 1
            while True:
                body = _read_stream_part(source, part_size)
                if not body:
                    break
                response = self._c.upload_part(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=body,
                )
                parts.append({"ETag": response["ETag"], "PartNumber": part_number})
                part_number += 1
            if not parts:
                # An empty stream is valid, but an empty multipart upload is not useful.  Abort
                # it before the small, non-streaming control-object write.
                self._c.abort_multipart_upload(
                    Bucket=bucket, Key=key, UploadId=upload_id
                )
                self._c.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=b"",
                    **({"ContentType": content_type} if content_type else {}),
                )
            else:
                self._c.complete_multipart_upload(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
        except Exception as e:  # noqa: BLE001
            try:
                self._c.abort_multipart_upload(
                    Bucket=bucket, Key=key, UploadId=upload_id
                )
            except Exception:
                pass
            raise self._wrap_not_found(e) from e
        return self.head(bucket, key)

    def copy(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
        *,
        source_etag: str | None = None,
    ) -> None:
        try:
            size = int(
                self._c.head_object(Bucket=src_bucket, Key=src_key)["ContentLength"]
            )
            if size < _MULTIPART_COPY_THRESHOLD:
                kwargs: dict = {
                    "Bucket": dst_bucket,
                    "Key": dst_key,
                    "CopySource": {"Bucket": src_bucket, "Key": src_key},
                }
                if source_etag is not None:
                    kwargs["CopySourceIfMatch"] = source_etag
                self._c.copy_object(
                    **kwargs,
                )
                return
            self._multipart_copy(
                src_bucket,
                src_key,
                dst_bucket,
                dst_key,
                size,
                source_etag=source_etag,
            )
        except S3Error:
            raise
        except Exception as e:  # noqa: BLE001
            raise self._wrap_not_found(e) from e

    def delete(self, bucket: str, key: str) -> None:
        try:
            self._c.delete_object(Bucket=bucket, Key=key)
        except Exception as e:  # noqa: BLE001
            raise S3Error(str(e)) from e

    def _multipart_copy(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
        size: int,
        *,
        source_etag: str | None = None,
    ) -> None:
        upload = self._c.create_multipart_upload(Bucket=dst_bucket, Key=dst_key)
        upload_id = upload["UploadId"]
        try:
            parts = []
            part_number = 1
            offset = 0
            while offset < size:
                last = min(offset + _COPY_PART_BYTES, size) - 1
                kwargs: dict = {
                    "Bucket": dst_bucket,
                    "Key": dst_key,
                    "UploadId": upload_id,
                    "PartNumber": part_number,
                    "CopySource": {"Bucket": src_bucket, "Key": src_key},
                    "CopySourceRange": f"bytes={offset}-{last}",
                }
                if source_etag is not None:
                    kwargs["CopySourceIfMatch"] = source_etag
                r = self._c.upload_part_copy(
                    **kwargs,
                )
                parts.append(
                    {"ETag": r["CopyPartResult"]["ETag"], "PartNumber": part_number}
                )
                offset = last + 1
                part_number += 1
            self._c.complete_multipart_upload(
                Bucket=dst_bucket,
                Key=dst_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception:
            self._c.abort_multipart_upload(
                Bucket=dst_bucket, Key=dst_key, UploadId=upload_id
            )
            raise


# --------------------------------------------------------------------------------------
# In-memory fake for tests
# --------------------------------------------------------------------------------------


class FakeS3:
    """In-memory S3 for tests. Stores ``{(bucket, key): bytes}``. No IAM, no policy — the
    airlock's Deny is an AWS-side property proven by the live smoke test, not something a
    unit test can or should re-check. What tests *do* exercise is that the validator makes
    the right allow/reject decision given the bytes."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], bytes] = {}
        # Optional per-key overrides so a test can simulate a HEAD/actual-size mismatch
        # or a missing server checksum without corrupting the stored body.
        self._head_overrides: dict[tuple[str, str], dict] = {}

    # -- test helpers (not part of the S3 protocol) --
    def seed(self, bucket: str, key: str, body: bytes) -> None:
        self._store[(bucket, key)] = body

    def override_head(self, bucket: str, key: str, **fields) -> None:
        self._head_overrides[(bucket, key)] = fields

    def dump(self, bucket: str) -> dict[str, bytes]:
        return {k[1]: v for k, v in self._store.items() if k[0] == bucket}

    # -- S3 protocol --
    def get(self, bucket: str, key: str) -> bytes:
        try:
            return self._store[(bucket, key)]
        except KeyError:
            raise NotFound(f"s3://{bucket}/{key}") from None

    def get_range(self, bucket: str, key: str, start: int, length: int) -> bytes:
        # Read the store directly, NOT via self.get — Boto3S3.get_range issues a ranged
        # request (Range header) and never fetches the whole object, so the fake models that
        # separation (a test subclass guarding get() must not see a ranged read as a whole get).
        try:
            body = self._store[(bucket, key)]
        except KeyError:
            raise NotFound(f"s3://{bucket}/{key}") from None
        if length <= 0:
            return b""
        return body[start : start + length]

    def head(self, bucket: str, key: str) -> dict:
        if (bucket, key) not in self._store:
            raise NotFound(f"s3://{bucket}/{key}")
        body = self._store[(bucket, key)]
        base = {
            "size": len(body),
            # A CONTENT-DERIVED stand-in for S3's stored CRC64NVME, not the real polynomial.
            # It has to be derived from the bytes for the fake to model the property that
            # matters: real S3 RECOMPUTES the checksum on CopyObject and on any overwrite, so a
            # same-length replacement changes it. A hardcoded None made
            # fsck._check_crc64nvme untestable and a constant would have made it vacuous.
            # Truncated + labelled so nobody mistakes it for a real CRC64NVME value.
            "crc64nvme": "fake64:" + hashlib.sha256(body).hexdigest()[:16],
            # A deterministic identity for the Fake's conditional-copy contract.  It is not
            # intended to model S3's MD5/multipart ETag algorithm; it only has to change when
            # a stored object changes, exactly as the real CopySourceIfMatch guard requires.
            "etag": "fake-etag:" + hashlib.sha256(body).hexdigest(),
            "content_type": None,
        }
        base.update(self._head_overrides.get((bucket, key), {}))
        return base

    def list(self, bucket: str, prefix: str) -> list[dict]:
        return [
            {"key": k, "size": len(v)}
            for (b, k), v in self._store.items()
            if b == bucket and k.startswith(prefix)
        ]

    def hash_object(self, bucket: str, key: str) -> tuple[str, int]:
        import hashlib

        # Read the store directly, NOT via self.get — Boto3S3.hash_object streams with
        # iter_chunks and never touches the whole-object get() path, so the fake must model
        # that separation faithfully (else a test subclass that guards get() sees a false hit).
        try:
            body = self._store[(bucket, key)]
        except KeyError:
            raise NotFound(f"s3://{bucket}/{key}") from None
        return hashlib.sha256(body).hexdigest(), len(body)

    def put(
        self,
        bucket: str,
        key: str,
        body: bytes,
        *,
        content_type: str | None = None,
        if_none_match: bool = False,
    ) -> None:
        if if_none_match and (bucket, key) in self._store:
            raise PreconditionFailed(f"s3://{bucket}/{key} already exists")
        self._store[(bucket, key)] = body

    def put_file(self, bucket: str, key: str, local_path: str) -> None:
        with open(local_path, "rb") as fh:
            self._store[(bucket, key)] = fh.read()

    def put_stream(
        self,
        bucket: str,
        key: str,
        source: BinaryIO,
        *,
        part_size: int = _STREAM_PART_BYTES,
        content_type: str | None = None,
    ) -> dict:
        # The fake models the public contract (bounded reads and an object only after a
        # successful complete upload), not S3's multipart implementation details.  Tests can
        # instrument ``source.read`` to prove no unbounded read occurred.
        body = bytearray()
        while True:
            chunk = source.read(part_size)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise TypeError("stream upload source.read() must return bytes")
            body.extend(chunk)
        self.put(bucket, key, bytes(body), content_type=content_type)
        return self.head(bucket, key)

    def copy(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
        *,
        source_etag: str | None = None,
    ) -> None:
        try:
            if (
                source_etag is not None
                and self.head(src_bucket, src_key).get("etag") != source_etag
            ):
                raise PreconditionFailed(
                    f"source s3://{src_bucket}/{src_key} no longer has the validated ETag"
                )
            self._store[(dst_bucket, dst_key)] = self._store[(src_bucket, src_key)]
        except KeyError:
            raise NotFound(f"s3://{src_bucket}/{src_key}") from None

    def delete(self, bucket: str, key: str) -> None:
        self._store.pop((bucket, key), None)


# Re-export io so a caller can wrap bytes without a separate import when streaming.
__all__ = [
    "S3",
    "Boto3S3",
    "FakeS3",
    "S3Error",
    "NotFound",
    "PreconditionFailed",
    "MIN_MULTIPART_PART_BYTES",
    "io",
]
