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

import base64
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

    def put_bytes_verified(
        self,
        bucket: str,
        key: str,
        body: bytes,
        *,
        content_type: str | None = None,
    ) -> str:
        """Write ``body`` and make S3 PROVE it received those bytes. Returns the sha256 hex.

        The golden rule applied to a write of an in-memory payload: the implementation computes
        the digest from ``body`` itself, declares it as ``ChecksumSHA256``, and S3 recomputes it
        server-side, so a corrupted body is rejected with ``BadDigest`` and never becomes an
        object. Implementations MUST refuse a body at or over the 5 GiB single-PUT limit rather
        than degrading to an unverified :meth:`put`.
        """
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

    # NOTE: ``put_file_verified`` is deliberately NOT a member of this Protocol, though both
    # ``Boto3S3`` and ``FakeS3`` implement it. A Protocol member is REQUIRED of every
    # implementation, and several test doubles implement this interface structurally (e.g.
    # ``tests/test_ingest_reservoir.py:64``, ``tests/test_publish_streaming.py:25``) — adding a
    # member would make each of them silently incomplete under a type checker for the sake of a
    # method only the build tooling calls. It is an available capability, not part of the
    # publish/validate contract; callers that want it should take a concrete ``Boto3S3``.


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
    def default(
        cls, region: str = "us-east-1", *, max_pool_connections: int | None = None
    ) -> "Boto3S3":
        """The ordinary client, optionally sized for a threaded caller.

        ``max_pool_connections=None`` (the default) builds exactly the client this classmethod
        always built — no ``botocore.config.Config`` at all — so every existing single-threaded
        caller is byte-for-byte unchanged.

        **Why the parameter has to exist here rather than at each call site.** Without a
        ``Config``, botocore's ``max_pool_connections`` is **10** (recomputed, not assumed:
        ``botocore.config.Config().max_pool_connections == 10`` on botocore 1.43.56, and
        ``tests/test_s3_pool.py`` re-asserts it so a botocore change is a failing test rather than
        a silent regression). botocore does **not** pass ``block=True`` to urllib3, so exceeding
        the pool neither raises nor waits: urllib3's ``_put_conn`` DISCARDS the surplus connection
        and logs "Connection pool is full". Workers 11..N therefore pay a fresh TLS handshake on
        every request and the fan-out silently caps itself, with no error anywhere — the failure
        mode is a *number that does not improve*, which is the hardest kind to notice.

        This mattered enough that the same six lines of ``Config`` construction were written twice
        already, at ``validate.py:main`` and ``corpus_build._s3``, each with its own copy of the
        explanation. Any threaded caller that forgets is not wrong, just slow — which is why the
        knob belongs on the constructor everyone already uses.
        """
        import boto3  # local import so the package imports without boto3 for pure-logic tests

        if max_pool_connections is None:
            return cls(boto3.client("s3", region_name=region))

        from botocore.config import Config

        return cls(
            boto3.client(
                "s3",
                region_name=region,
                config=Config(max_pool_connections=max_pool_connections),
            )
        )

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

    def put_file_verified(self, bucket: str, key: str, local_path: str) -> str:
        """Upload, and make S3 prove it received the bytes we hashed. Returns the sha256 hex.

        The golden rule applied to the WRITE path. ``put_file`` above sends bytes and trusts a 200;
        this computes the digest locally, declares it in the request, and lets S3 recompute it
        server-side — a mismatch is rejected by S3 rather than discovered later by ``fsck``. That
        turns "the upload returned success" into "the object holds exactly these bytes."

        **Single-part on purpose, and this is the whole subtlety.** ``ChecksumSHA256`` means
        different things depending on how the object was uploaded: for a single PUT it is a
        ``FULL_OBJECT`` digest of the whole object, but for a multipart upload it is a *composite*
        of per-part digests, which is NOT the sha256 of the file and cannot be compared to one.
        ``boto3``'s default ``multipart_threshold`` is 8 MiB — measured — so ``upload_file`` would
        silently take the multipart path for a 100 MB token shard and the checksum would stop
        meaning what a caller assumes. A single ``put_object`` is legal up to 5 GiB, which every
        shard is comfortably under, so this refuses anything larger rather than quietly degrading
        to a composite digest.

        Ported from a sibling repo's ``S3ArtifactStore.put_file`` (``artifacts.py:238-267``),
        which is the one thing that implementation did better than this module. Its own version had
        no size guard, because nothing it uploaded approached the limit.

        **Verified live against S3, 2026-08-01**, because a checksum header the service ignores
        would be exactly the decoration the golden rule forbids:

        * correct digest -> 200, and the response echoes ``ChecksumType: FULL_OBJECT``, confirming
          the digest covers the whole object rather than a part;
        * digest deliberately corrupted -> ``BadDigest``, *"The SHA256 you specified did not match
          the calculated checksum"*, and a subsequent listing showed **no object was created**.

        So the rejection is real server-side enforcement: a corrupted body cannot become an object
        that later passes ``fsck``.
        """
        import os

        size = os.path.getsize(local_path)
        if size >= _MULTIPART_COPY_THRESHOLD:
            raise S3Error(
                f"{local_path} is {size} bytes, at or over the {_MULTIPART_COPY_THRESHOLD}-byte "
                f"single-PUT limit. A multipart ChecksumSHA256 is a COMPOSITE of per-part digests, "
                f"not the object's sha256, so it cannot be compared against a local digest — "
                f"use put_file() and verify with hash_object() instead of getting a checksum that "
                f"looks authoritative and is not."
            )
        h = hashlib.sha256()
        with open(local_path, "rb") as f:
            while chunk := f.read(8 * 1024 * 1024):  # bounded RAM, matching hash_object
                h.update(chunk)
        digest = h.digest()
        try:
            with open(local_path, "rb") as f:
                self._c.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=f,
                    ChecksumAlgorithm="SHA256",
                    ChecksumSHA256=base64.b64encode(digest).decode("ascii"),
                )
        except Exception as e:  # noqa: BLE001
            raise S3Error(f"verified upload of {key} failed: {e}") from e
        return h.hexdigest()

    def put_bytes_verified(
        self,
        bucket: str,
        key: str,
        body: bytes,
        *,
        content_type: str | None = None,
    ) -> str:
        """:meth:`put_file_verified` for a payload already in memory. Returns the sha256 hex.

        **Why this exists rather than a call to `put_file_verified`.** That method takes a local
        path, and the one caller that needs this — `corpus_build.run_bundle`'s pack sink — holds a
        ~100 MB shard as `bytes` that `corpus_pack` deliberately never spills to disk. Routing it
        through a temporary file to reach a verified upload would add a full write and read of every
        shard to buy a checksum, i.e. ~4 TB of local I/O across a 1.0T build, to avoid five lines.

        **Why not `put(..., checksum_sha256=...)`.** A caller-supplied digest can describe bytes
        other than the ones being sent, which is precisely the decoration the golden rule forbids:
        it would read as verification and prove nothing. This computes the digest from the same
        `body` object it hands to `put_object`, so the two cannot disagree.

        **Single-part, and the reasoning is `put_file_verified`'s verbatim** — with one difference
        worth stating because it is the opposite of the trap there. `put_object` has no multipart
        path at all (boto3's 8 MiB `multipart_threshold` belongs to `upload_file`), so
        `ChecksumSHA256` here is always a `FULL_OBJECT` digest and can never silently become a
        composite of per-part digests. The 5 GiB guard is therefore about the hard single-PUT limit
        rather than about checksum semantics — but it still **raises** instead of falling back to an
        unverified `put`, because a caller who oversteps it wants to know, not to be quietly
        downgraded to the write path this method exists to replace.

        **The live evidence is `put_file_verified`'s, and it is the same API call.** 2026-08-01,
        against real S3: a correct digest returned 200 with `ChecksumType: FULL_OBJECT`; a
        deliberately corrupted one returned `BadDigest` — *"The SHA256 you specified did not match
        the calculated checksum"* — and no object was created. The request this method issues
        differs from that one only in whether `Body` is a file object or a `bytes`; boto3 sends the
        same header either way. ⚠️ That is an argument by identity of the API call, NOT a second
        live measurement. **The bytes-shaped path must have its own deliberate-corruption assertion
        in the Phase 2 smoke test** before anything downstream is retired on the strength of it.
        """
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise TypeError(f"put_bytes_verified body must be bytes-like, got {type(body).__name__}")
        body = bytes(body)
        if len(body) >= _MULTIPART_COPY_THRESHOLD:
            raise S3Error(
                f"{key} is {len(body)} bytes, at or over the {_MULTIPART_COPY_THRESHOLD}-byte "
                f"single-PUT limit, so it cannot be uploaded with a whole-object ChecksumSHA256. "
                f"Refusing rather than falling back to an unverified put(): the caller asked for a "
                f"server-verified write and would otherwise get an unverified one that returns 200."
            )
        digest = hashlib.sha256(body).digest()
        kwargs: dict = {
            "Bucket": bucket,
            "Key": key,
            "Body": body,
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": base64.b64encode(digest).decode("ascii"),
        }
        if content_type:
            kwargs["ContentType"] = content_type
        try:
            self._c.put_object(**kwargs)
        except Exception as e:  # noqa: BLE001
            raise S3Error(f"verified upload of {key} failed: {e}") from e
        return digest.hex()

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
        # The ``ChecksumSHA256`` each write DECLARED, if any. Kept apart from ``head``'s
        # content-derived stand-in on purpose: `crc64nvme` there is recomputed from whatever bytes
        # are stored and so can never disagree with them, which makes it useless for the question
        # this records — did the caller ask S3 to verify, or did it just PUT and trust a 200?
        self._declared_checksums: dict[tuple[str, str], str] = {}

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

    def put_file_verified(self, bucket: str, key: str, local_path: str) -> str:
        """Mirror the real client, INCLUDING its size refusal.

        A fake that accepts an upload the real client rejects makes the guard untestable, and the
        guard is the interesting part — a caller who oversteps the single-PUT limit would get a
        composite checksum that looks authoritative and is not. So the limit is enforced here too,
        against the same constant.
        """
        import os

        size = os.path.getsize(local_path)
        if size >= _MULTIPART_COPY_THRESHOLD:
            raise S3Error(
                f"{local_path} is {size} bytes, at or over the {_MULTIPART_COPY_THRESHOLD}-byte "
                f"single-PUT limit; a multipart ChecksumSHA256 is a composite, not the object's "
                f"sha256"
            )
        with open(local_path, "rb") as fh:
            body = fh.read()
        self._store[(bucket, key)] = body
        return hashlib.sha256(body).hexdigest()

    def put_bytes_verified(
        self,
        bucket: str,
        key: str,
        body: bytes,
        *,
        content_type: str | None = None,
    ) -> str:
        """Mirror the real client, INCLUDING its size refusal and S3's server-side recompute.

        The size refusal is here for :meth:`put_file_verified`'s reason: a fake that accepts an
        upload the real client rejects makes the guard untestable.

        The **recompute** is here for a stronger reason. This method's entire value is that S3
        re-derives the digest from the bytes it received and rejects a mismatch — a fake that just
        stored the body would model an unverified `put` wearing a checksum's name, and every test
        written against it would pass whether or not the header was ever sent. So the declared
        digest goes through :meth:`_accept_verified_put`, which recomputes and raises. That method
        is also the seam a test uses to declare a deliberately WRONG digest and observe the
        rejection, which is the only way to prove the check is not decoration.
        """
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise TypeError(f"put_bytes_verified body must be bytes-like, got {type(body).__name__}")
        body = bytes(body)
        if len(body) >= _MULTIPART_COPY_THRESHOLD:
            raise S3Error(
                f"{key} is {len(body)} bytes, at or over the {_MULTIPART_COPY_THRESHOLD}-byte "
                f"single-PUT limit, so it cannot be uploaded with a whole-object ChecksumSHA256"
            )
        declared = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
        self._accept_verified_put(bucket, key, body, declared, content_type=content_type)
        return hashlib.sha256(body).hexdigest()

    def _accept_verified_put(
        self,
        bucket: str,
        key: str,
        body: bytes,
        declared_sha256_b64: str,
        *,
        content_type: str | None = None,
    ) -> None:
        """S3's server side of a checksum-declaring PUT: recompute, compare, store or refuse.

        Models the behaviour verified live on 2026-08-01 (`Boto3S3.put_file_verified`): a mismatch
        returns ``BadDigest`` and **no object is created**, so the store is left untouched.
        """
        actual = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
        if declared_sha256_b64 != actual:
            raise S3Error(
                f"BadDigest: the SHA256 you specified for s3://{bucket}/{key} did not match the "
                f"calculated checksum (declared {declared_sha256_b64}, calculated {actual}); no "
                f"object was created"
            )
        self._declared_checksums[(bucket, key)] = declared_sha256_b64
        self._store[(bucket, key)] = body

    def declared_checksum(self, bucket: str, key: str) -> str | None:
        """Test helper (not part of the S3 protocol): the ``ChecksumSHA256`` a write declared.

        ``None`` for an object written by plain :meth:`put`, which declares nothing — so a test can
        tell "uploaded with a server-verified checksum" from "uploaded and hoped", a distinction
        the stored bytes are identical across.
        """
        return self._declared_checksums.get((bucket, key))

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
