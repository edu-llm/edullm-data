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
from typing import Protocol, runtime_checkable


class S3Error(RuntimeError):
    """A read/write against S3 failed in a way the validator should surface, not swallow."""


class NotFound(S3Error):
    """The object or bucket does not exist (404 / NoSuchKey / NoSuchBucket)."""


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

    def put(self, bucket: str, key: str, body: bytes, *, content_type: str | None = None) -> None:
        """Write ``body`` at ``key``. Used for ``_REJECTED.json`` on landing and the
        catalog entry on the published bucket — small control objects only."""
        ...

    def put_file(self, bucket: str, key: str, local_path: str) -> None:
        """Upload a local file to ``key`` by STREAMING it (multipart, bounded memory) —
        never read the whole file into RAM. Used only to stage a local source directory
        into landing; payload bytes above laptop scale should originate in S3, not here."""
        ...

    def copy(self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> None:
        """Server-side copy — bytes never transit the client (§1 promotion, and publish()'s
        staged→final move). Implementations must use multipart copy above the 5 GB single-part
        ceiling (§9); 8 of the audit's 15 largest objects exceeded it, so this is not optional."""
        ...

    def delete(self, bucket: str, key: str) -> None:
        """Delete one object (current version). Used for best-effort staging cleanup after a
        server-side copy to the final prefix. Not for the airlock-locked read bucket."""
        ...


# --------------------------------------------------------------------------------------
# The boundary check
# --------------------------------------------------------------------------------------

#: The four methods every reader in :mod:`edullm_data.read` calls. Deliberately not the whole
#: protocol: a read-only adapter that implements these and none of ``put``/``copy``/``delete``
#: is a legitimate thing to hand a reader, and rejecting it would be this check inventing a
#: requirement the code does not have.
_READER_METHODS = ("get", "get_range", "head", "list")


def require_s3_adapter(candidate: object, *, called_from: str) -> None:
    """Refuse an object that is not an :class:`S3` adapter, before it is used.

    **THIS EXISTS BECAUSE OF THE MOST MISLEADING COINCIDENCE AVAILABLE.** boto3 names its
    generated client class after the service, so ``type(boto3.client("s3")).__name__`` is
    literally ``S3`` -- the same name as the protocol above. Passing a boto3 client where an
    adapter belongs therefore dies, deep inside a private helper on first use, with
    ``AttributeError: 'S3' object has no attribute 'head'``. That message describes this
    protocol perfectly and is about a different object entirely, so the obvious reading is
    that ``head`` is a typo for ``head_object`` and the fix is to change the call. It is not.
    ``head`` is on the protocol, ``Boto3S3`` implements it over ``head_object``, ``FakeS3``
    implements it in memory, and eleven call sites depend on it.

    That mistake cost four failed runs on the platform on 2026-08-01, and it shipped past
    ``mypy --strict``: boto3 carries no type information, so ``boto3.client("s3")`` is ``Any``
    and ``Any`` satisfies the ``s3: S3`` annotation. The annotation is right and buys nothing
    at the call site. Nor could a test catch it, and the reason is worth stating: every test
    runs against :class:`FakeS3`, which is a faithful implementation of the protocol, and a
    faithful fake is exactly what cannot stand in for a caller who passes something that is
    not an implementation at all.

    So the missing guard was never a unit test. It is an assertion at the boundary where an
    arbitrary object is accepted, which is here.

    Nothing is called on ``candidate`` and no network is touched; this reads attributes.
    """
    missing = [name for name in _READER_METHODS if not callable(getattr(candidate, name, None))]
    if not missing:
        return

    # A boto3 object, named rather than guessed at, because the one fact a person cannot
    # recover from the traceback is which of the two classes called ``S3`` they are holding.
    #
    # THE DISCRIMINATOR IS ``meta``'S OWN MODULE, AND THE FIRST VERSION OF THIS GOT IT WRONG
    # IN A WAY MUTATION TESTING CAUGHT. Requiring ``head_object`` alongside ``meta`` looks
    # tighter and quietly excludes ``boto3.resource("s3")``, which has no ``head_object`` at
    # all: a resource is at least as easy to pass here by mistake as a client, and it would
    # have fallen through to the generic message that names neither boto3 nor ``Boto3S3``.
    # Both a client and a resource carry ``meta``, and its type comes from ``botocore.client``
    # or ``boto3.resources.base`` respectively, so one check covers both and matches nothing
    # that merely happens to have an attribute called ``meta``.
    meta_module = type(getattr(candidate, "meta", None)).__module__.split(".")[0]
    if meta_module in {"boto3", "botocore"}:
        raise TypeError(
            f"{called_from} was given a boto3 object, and it needs an edullm_data S3 adapter. "
            f"These are two different things with the same class name: "
            f"type(boto3.client('s3')).__name__ is 'S3', exactly like the protocol in "
            f"edullm_data.s3, so without this check the failure is "
            f"\"AttributeError: 'S3' object has no attribute 'head'\" and reads like a missing "
            f"method on the protocol. It is not one; head() is on the protocol and Boto3S3 "
            f"implements it over head_object. "
            f"Pass edullm_data.s3.Boto3S3.default() instead, or Boto3S3(client) to reuse the "
            f"client you already hold."
        )

    raise TypeError(
        f"{called_from} needs an edullm_data.s3.S3 adapter and was given "
        f"{type(candidate).__module__}.{type(candidate).__name__}, which is missing "
        f"{', '.join(f'{name}()' for name in missing)}. "
        f"Use edullm_data.s3.Boto3S3 for real S3 or edullm_data.s3.FakeS3 for a test."
    )


# --------------------------------------------------------------------------------------
# Real client
# --------------------------------------------------------------------------------------

# CopyObject single-part ceiling (§9). Above this, UploadPartCopy is required.
_MULTIPART_COPY_THRESHOLD = 5 * 1024**3
_COPY_PART_BYTES = 256 * 1024**2  # 256 MiB parts; 10,000-part cap ⇒ up to ~2.4 TiB/object


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
        status = getattr(err, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"NoSuchKey", "NoSuchBucket", "404", "NotFound"} or status == 404:
            return NotFound(str(err))
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

    def put(self, bucket: str, key: str, body: bytes, *, content_type: str | None = None) -> None:
        kwargs: dict = {"Bucket": bucket, "Key": key, "Body": body}
        if content_type:
            kwargs["ContentType"] = content_type
        try:
            self._c.put_object(**kwargs)
        except Exception as e:  # noqa: BLE001
            raise S3Error(str(e)) from e

    def put_file(self, bucket: str, key: str, local_path: str) -> None:
        # upload_file streams and does multipart automatically — bounded memory
        try:
            self._c.upload_file(local_path, bucket, key)
        except Exception as e:  # noqa: BLE001
            raise S3Error(str(e)) from e

    def copy(self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> None:
        try:
            size = int(self._c.head_object(Bucket=src_bucket, Key=src_key)["ContentLength"])
            if size < _MULTIPART_COPY_THRESHOLD:
                self._c.copy_object(
                    Bucket=dst_bucket,
                    Key=dst_key,
                    CopySource={"Bucket": src_bucket, "Key": src_key},
                )
                return
            self._multipart_copy(src_bucket, src_key, dst_bucket, dst_key, size)
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
        self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str, size: int
    ) -> None:
        upload = self._c.create_multipart_upload(Bucket=dst_bucket, Key=dst_key)
        upload_id = upload["UploadId"]
        try:
            parts = []
            part_number = 1
            offset = 0
            while offset < size:
                last = min(offset + _COPY_PART_BYTES, size) - 1
                r = self._c.upload_part_copy(
                    Bucket=dst_bucket,
                    Key=dst_key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    CopySource={"Bucket": src_bucket, "Key": src_key},
                    CopySourceRange=f"bytes={offset}-{last}",
                )
                parts.append({"ETag": r["CopyPartResult"]["ETag"], "PartNumber": part_number})
                offset = last + 1
                part_number += 1
            self._c.complete_multipart_upload(
                Bucket=dst_bucket,
                Key=dst_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception:
            self._c.abort_multipart_upload(Bucket=dst_bucket, Key=dst_key, UploadId=upload_id)
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
            "etag": None,
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

    def put(self, bucket: str, key: str, body: bytes, *, content_type: str | None = None) -> None:
        self._store[(bucket, key)] = body

    def put_file(self, bucket: str, key: str, local_path: str) -> None:
        with open(local_path, "rb") as fh:
            self._store[(bucket, key)] = fh.read()

    def copy(self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> None:
        try:
            self._store[(dst_bucket, dst_key)] = self._store[(src_bucket, src_key)]
        except KeyError:
            raise NotFound(f"s3://{src_bucket}/{src_key}") from None

    def delete(self, bucket: str, key: str) -> None:
        self._store.pop((bucket, key), None)


# Re-export io so a caller can wrap bytes without a separate import when streaming.
__all__ = ["S3", "Boto3S3", "FakeS3", "S3Error", "NotFound", "io"]
