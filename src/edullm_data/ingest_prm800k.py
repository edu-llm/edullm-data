"""Batch-only, byte-preserving ingestion for the OpenAI PRM800K release.

The Hugging Face repository is a transport mirror, not the provenance authority.  This module
therefore embeds the OpenAI Git commit and its four Git-LFS file witnesses, streams the pinned
HF revision directly to ``edullm-landing`` and only then asks the ordinary publisher to seal a
``vendored/v1`` artifact.  It never writes to ``edullm-data`` or calls ``promote()``.

There are deliberately two commands:

``stage``
    Fetch and verify the four source files under a non-triggering staging prefix.  It writes a
    receipt *outside* the payload only after every streamed source hash matches the fixed table.

``publish``
    Revalidates that receipt and the staged object checksums, then invokes ``publish()`` with a
    fixed ``v1`` reservation.  ``publish()`` writes the final group manifest last; EventBridge
    and the Batch validator handle the only permitted promotion path.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import sys
from typing import Any, BinaryIO, Callable, Mapping
from urllib.parse import quote
from urllib.request import Request, urlopen

from .contracts import canonical_json
from .publish import PublishError, PublishPlan, _build_executor_from_env, publish
from .s3 import Boto3S3, MIN_MULTIPART_PART_BYTES, NotFound, S3, S3Error


LANDING_BUCKET = "edullm-landing"
DATA_BUCKET = "edullm-data"
_STAGE_SCHEMA_VERSION = "edullm-prm800k-stage/v1"
_RUN_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_JOB_DEFINITION_ARN_RE = re.compile(
    r"arn:aws(?:-[a-z]+)?:batch:[a-z0-9-]+:\d{12}:job-definition/"
    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}:\d+"
)
_STREAM_PART_BYTES = 16 * 1024**2


class Prm800kIngestError(RuntimeError):
    """A PRM800K staging or publication invariant failed."""


class ReceiptError(Prm800kIngestError):
    """The operational staging receipt is absent, malformed, or inconsistent with S3."""


class SourceVerificationError(Prm800kIngestError):
    """Bytes fetched from the pinned Hugging Face transport do not match OpenAI's witness."""


@dataclass(frozen=True)
class UpstreamFile:
    """One immutable upstream file witness, as recorded by OpenAI's Git-LFS pointer."""

    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class Prm800kSource:
    """The complete immutable identity of the source release and its transport mirror."""

    dataset_id: str
    version: str
    hf_repo: str
    hf_revision: str
    canonical_repo: str
    canonical_uri: str
    canonical_revision: str
    license_id: str
    files: tuple[UpstreamFile, ...]

    @property
    def hf_uri(self) -> str:
        return f"https://huggingface.co/datasets/{self.hf_repo}"

    def transport_url(self, file: UpstreamFile) -> str:
        # Pin a full commit in the URL itself.  `urlopen` follows the Hub's CDN redirect while
        # keeping the immutable revision in the request we record in the receipt.
        path = quote(file.path, safe="/")
        return f"{self.hf_uri}/resolve/{self.hf_revision}/{path}?download=true"


# Canonical source: github.com/openai/prm800k commit 00811d6 (2023-04-13), whose four LFS
# pointers carry exactly these SHA-256/byte witnesses.  The Hugging Face commit is used only to
# fetch the bytes, not as a replacement for the OpenAI identity or license basis.
PRM800K_SOURCE = Prm800kSource(
    dataset_id="vendor/openai-prm800k",
    version="v1",
    hf_repo="tasksource/PRM800K",
    hf_revision="547b19506677a59037ee888838834b65e9b1ddd4",
    canonical_repo="openai/prm800k",
    canonical_uri="https://github.com/openai/prm800k",
    canonical_revision="00811d6de065642a6967b9017d4cee59550c0ef4",
    license_id="MIT",
    files=(
        UpstreamFile(
            "phase1_test.jsonl",
            829_105,
            "f4b3bc5b095e45c816453dc4d748b755c680d61d55f9895d929a335b487c727d",
        ),
        UpstreamFile(
            "phase1_train.jsonl",
            7_900_236,
            "e9da6a73f827ffb9a8c0dc644c541d34ed76b3d4d1e4896ff5f7b37ddf5ae34d",
        ),
        UpstreamFile(
            "phase2_test.jsonl",
            12_240_719,
            "6b172efa884ac8341a946dd82e06947c135b7254109fb3f7aa907c715d98aaad",
        ),
        UpstreamFile(
            "phase2_train.jsonl",
            456_135_365,
            "1110237feeb51d1bc200cb37b8f965cfdc1036eac7d506094049366fe7dc1089",
        ),
    ),
)


@dataclass(frozen=True)
class StageResult:
    run_id: str
    staging_prefix: str
    receipt: Mapping[str, Any]
    reused: bool


@dataclass(frozen=True)
class PublishResult:
    version: str
    status: str  # "submitted" | "already-published"
    plan: PublishPlan | None


class _HashingReader:
    """Readable wrapper that records exactly the bytes consumed by ``S3.put_stream``."""

    def __init__(self, source: BinaryIO, *, expected_bytes: int | None = None) -> None:
        self._source = source
        self._hash = hashlib.sha256()
        self.bytes_read = 0
        self._expected_bytes = expected_bytes

    def read(self, size: int = -1) -> bytes:
        # A negative read would let an upload implementation buffer a whole source object.  The
        # S3 protocol promises bounded reads, and enforcing that at this seam makes the claim
        # testable rather than documentary.
        if size is None or size < 0:
            raise Prm800kIngestError(
                "stream uploader requested an unbounded source read"
            )
        chunk = self._source.read(size)
        if not isinstance(chunk, bytes):
            raise Prm800kIngestError(
                "Hugging Face response returned a non-bytes body chunk"
            )
        if (
            self._expected_bytes is not None
            and self.bytes_read + len(chunk) > self._expected_bytes
        ):
            # Fail before returning this chunk to the multipart uploader: an oversized
            # transport response must not become an accepted (or even completed) S3 payload
            # merely because its eventual digest would fail after the transfer.
            raise SourceVerificationError(
                "HF transport returned more bytes than the pinned source witness permits"
            )
        self._hash.update(chunk)
        self.bytes_read += len(chunk)
        return chunk

    @property
    def sha256(self) -> str:
        return self._hash.hexdigest()


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _validate_spec(spec: Prm800kSource) -> None:
    if re.fullmatch(r"v[1-9][0-9]*", spec.version) is None:
        raise Prm800kIngestError(
            f"source version {spec.version!r} is not a canonical vN value"
        )
    seen: set[str] = set()
    for file in spec.files:
        if (
            not file.path
            or file.path.startswith("/")
            or ".." in file.path.split("/")
            or file.path in seen
            or file.bytes < 0
            or _SHA256_RE.fullmatch(file.sha256) is None
        ):
            raise Prm800kIngestError(
                f"invalid immutable source witness for {file.path!r}"
            )
        seen.add(file.path)
    if not spec.files:
        raise Prm800kIngestError("PRM800K source must declare at least one witness")


def _validate_run_id(run_id: str) -> None:
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise Prm800kIngestError(
            "run_id must be 1–80 lowercase letters/digits/hyphens and must not contain a path separator"
        )


def _require_batch_provenance(env: Mapping[str, str]) -> dict[str, Any] | None:
    """Return a Batch executor record only when its immutable deployment identity is present.

    This is intentionally enforced in the PRM800K live path rather than merely documented:
    the raw mirror is a fixed v1 provenance artifact, so a queue name and a mutable wheel URL
    are not enough to say which code/image admitted its bytes.  Unit callers without a Batch
    job id remain able to use a FakeS3 fixture.
    """
    if not env.get("AWS_BATCH_JOB_ID"):
        return None
    executor = _build_executor_from_env(env)
    job_definition_arn = executor.get("job_definition_arn")
    image_digest = executor.get("image_digest")
    image_repo = executor.get("image_repo")
    if not isinstance(job_definition_arn, str) or not _JOB_DEFINITION_ARN_RE.fullmatch(
        job_definition_arn
    ):
        raise Prm800kIngestError(
            "Batch PRM800K publication requires EDULLM_BATCH_JOB_DEFINITION_ARN with a "
            "revisioned AWS Batch job-definition ARN"
        )
    if not isinstance(image_digest, str) or not _IMAGE_DIGEST_RE.fullmatch(
        image_digest
    ):
        raise Prm800kIngestError(
            "Batch PRM800K publication requires EDULLM_IMAGE_DIGEST=sha256:<64 lowercase hex>"
        )
    if (
        not isinstance(image_repo, str)
        or not image_repo.strip()
        or "todo" in image_repo.lower()
    ):
        raise Prm800kIngestError(
            "Batch PRM800K publication requires a concrete EDULLM_IMAGE_REPO"
        )
    return executor


def _require_recorded_batch_provenance(executor: Mapping[str, Any]) -> None:
    """Reject an old fixed-v1 reservation that was created without immutable identity."""
    job_definition_arn = executor.get("job_definition_arn")
    image_digest = executor.get("image_digest")
    image_repo = executor.get("image_repo")
    if (
        not isinstance(job_definition_arn, str)
        or not _JOB_DEFINITION_ARN_RE.fullmatch(job_definition_arn)
        or not isinstance(image_digest, str)
        or not _IMAGE_DIGEST_RE.fullmatch(image_digest)
        or not isinstance(image_repo, str)
        or not image_repo.strip()
    ):
        raise Prm800kIngestError(
            "fixed v1 reservation lacks the required immutable Batch job-definition and image-digest provenance"
        )


def _staging_prefix(spec: Prm800kSource, run_id: str) -> str:
    return f"_staging/{spec.dataset_id}/{run_id}"


def _payload_prefix(spec: Prm800kSource, run_id: str) -> str:
    return f"{_staging_prefix(spec, run_id)}/payload"


def _payload_key(spec: Prm800kSource, run_id: str, file: UpstreamFile) -> str:
    return f"{_payload_prefix(spec, run_id)}/raw/{file.path}"


def _receipt_key(spec: Prm800kSource, run_id: str) -> str:
    return f"{_staging_prefix(spec, run_id)}/receipt.json"


def _source_doc(spec: Prm800kSource) -> dict[str, Any]:
    return {
        "canonical": {
            "name": spec.canonical_repo,
            "uri": spec.canonical_uri,
            "revision": spec.canonical_revision,
            "license": {"id": spec.license_id, "basis": "declared"},
        },
        "transport": {
            "name": spec.hf_repo,
            "uri": spec.hf_uri,
            "revision": spec.hf_revision,
        },
    }


def _expected_payload(spec: Prm800kSource) -> dict[str, dict[str, Any]]:
    return {
        f"raw/{file.path}": {"bytes": file.bytes, "sha256": file.sha256}
        for file in spec.files
    }


def _read_json(s3: S3, bucket: str, key: str) -> dict[str, Any]:
    try:
        value = json.loads(s3.get(bucket, key).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"{key} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ReceiptError(f"{key} must contain a JSON object")
    return value


def _read_receipt(s3: S3, bucket: str, key: str) -> dict[str, Any]:
    """Load a staging receipt while translating an absent commit marker for operators.

    ``_read_json`` is also used for data-bucket metadata, where callers intentionally need a
    raw ``NotFound`` to distinguish an absent object from malformed JSON.  A receipt is the
    stage's commit marker, so a missing one is a meaningful ingest-state error instead.
    """
    try:
        return _read_json(s3, bucket, key)
    except NotFound as exc:
        raise ReceiptError(f"staging receipt is absent: {key}") from exc


def _expected_staging_keys(spec: Prm800kSource, run_id: str) -> set[str]:
    return {_payload_key(spec, run_id, file) for file in spec.files} | {
        _receipt_key(spec, run_id)
    }


def _check_staging_layout(
    s3: S3,
    *,
    landing_bucket: str,
    run_id: str,
    spec: Prm800kSource,
) -> set[str]:
    prefix = _staging_prefix(spec, run_id) + "/"
    observed = {obj["key"] for obj in s3.list(landing_bucket, prefix)}
    if not observed:
        return observed
    expected = _expected_staging_keys(spec, run_id)
    unexpected = sorted(observed - expected)
    if unexpected:
        raise ReceiptError(
            f"staging prefix {_staging_prefix(spec, run_id)!r} contains unexpected objects: {unexpected}"
        )
    # This explicit wording makes the anti-trigger property obvious even if an operator sees a
    # partial prefix in S3.  An ingress stage never writes these commit/control names.
    forbidden = [
        key for key in observed if key.endswith(("/dataset.json", "/manifest.json"))
    ]
    if forbidden:
        raise ReceiptError(
            f"staging prefix contains forbidden dataset control files: {forbidden}"
        )
    return observed


def _verify_receipt(
    receipt: Mapping[str, Any],
    *,
    s3: S3,
    landing_bucket: str,
    run_id: str,
    spec: Prm800kSource,
    check_s3: bool,
) -> None:
    """Validate mutable receipt data against code-pinned source facts and S3 observations."""
    staging_prefix = _staging_prefix(spec, run_id)
    if receipt.get("schema_version") != _STAGE_SCHEMA_VERSION:
        raise ReceiptError("receipt has an unexpected schema_version")
    if (
        receipt.get("dataset_id") != spec.dataset_id
        or receipt.get("version") != spec.version
    ):
        raise ReceiptError(
            "receipt does not identify the fixed PRM800K target artifact"
        )
    if (
        receipt.get("run_id") != run_id
        or receipt.get("staging_prefix") != staging_prefix
    ):
        raise ReceiptError(
            "receipt run_id/staging_prefix does not match the requested staging run"
        )
    if not _valid_timestamp(receipt.get("retrieved_at")):
        raise ReceiptError(
            "receipt retrieved_at must be an ISO-8601 timestamp with timezone"
        )
    if receipt.get("source") != _source_doc(spec):
        raise ReceiptError(
            "receipt source pins do not match the immutable PRM800K source table"
        )

    raw_files = receipt.get("files")
    if not isinstance(raw_files, list) or len(raw_files) != len(spec.files):
        raise ReceiptError(
            "receipt file list does not match the immutable PRM800K source table"
        )
    by_path: dict[str, Mapping[str, Any]] = {}
    for item in raw_files:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            raise ReceiptError("receipt files must be objects with a string path")
        path = str(item["path"])
        if path in by_path:
            raise ReceiptError(f"receipt repeats source path {path!r}")
        by_path[path] = item

    for file in spec.files:
        item = by_path.get(file.path)
        if item is None:
            raise ReceiptError(f"receipt omits source file {file.path!r}")
        expected_key = _payload_key(spec, run_id, file)
        if (
            item.get("payload_path") != f"raw/{file.path}"
            or item.get("bytes") != file.bytes
            or item.get("sha256") != file.sha256
            or item.get("transport_url") != spec.transport_url(file)
        ):
            raise ReceiptError(
                f"receipt witness for {file.path!r} differs from the pinned source"
            )
        s3_doc = item.get("s3")
        if (
            not isinstance(s3_doc, Mapping)
            or s3_doc.get("key") != expected_key
            or s3_doc.get("bytes") != file.bytes
            or not (
                isinstance(s3_doc.get("crc64nvme"), str)
                or s3_doc.get("crc64nvme") is None
            )
        ):
            raise ReceiptError(f"receipt S3 record for {file.path!r} is malformed")
        if check_s3:
            try:
                head = s3.head(landing_bucket, expected_key)
            except NotFound as exc:
                raise ReceiptError(
                    f"staged payload object is absent: {expected_key}"
                ) from exc
            if head.get("size") != file.bytes:
                raise ReceiptError(
                    f"staged payload size for {file.path!r} is {head.get('size')}, expected {file.bytes}"
                )
            if head.get("crc64nvme") != s3_doc.get("crc64nvme"):
                raise ReceiptError(
                    f"staged payload server checksum for {file.path!r} differs from its receipt"
                )


def _build_receipt(
    *,
    spec: Prm800kSource,
    run_id: str,
    retrieved_at: str,
    uploaded: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": _STAGE_SCHEMA_VERSION,
        "dataset_id": spec.dataset_id,
        "version": spec.version,
        "run_id": run_id,
        "staging_prefix": _staging_prefix(spec, run_id),
        "retrieved_at": retrieved_at,
        "source": _source_doc(spec),
        "files": [
            {
                "path": file.path,
                "payload_path": f"raw/{file.path}",
                "bytes": file.bytes,
                "sha256": file.sha256,
                "transport_url": spec.transport_url(file),
                "s3": dict(uploaded[file.path]),
            }
            for file in spec.files
        ],
    }


def _default_open(url: str, *, timeout_seconds: int) -> BinaryIO:
    request = Request(url, headers={"User-Agent": "edullm-data-prm800k-ingest/1"})
    return urlopen(request, timeout=timeout_seconds)  # noqa: S310 - URL is fixed by PRM800K_SOURCE


def _cleanup_payload(
    s3: S3,
    *,
    landing_bucket: str,
    run_id: str,
    spec: Prm800kSource,
) -> None:
    """Best-effort cleanup after a transfer failure.

    The dedicated live role deliberately has no DeleteObject permission, so a real failed run
    normally remains as receipt-less evidence until landing's lifecycle expires.  The next
    attempt must use a fresh run id.  Keeping this helper makes local/FakeS3 tests tidy and is
    harmless for a broader manually approved remediation identity.
    """
    for file in spec.files:
        try:
            s3.delete(landing_bucket, _payload_key(spec, run_id, file))
        except Exception:
            # A failed cleanup deliberately leaves no receipt.  The next invocation refuses the
            # partial prefix rather than mistaking it for a completed/verified stage.
            pass


def _cleanup_receipt(
    s3: S3,
    *,
    landing_bucket: str,
    run_id: str,
    spec: Prm800kSource,
) -> None:
    """Best-effort removal of an ambiguous failed receipt commit (normally lifecycle-owned)."""
    try:
        s3.delete(landing_bucket, _receipt_key(spec, run_id))
    except Exception:
        pass


def stage_prm800k(
    *,
    s3: S3,
    run_id: str,
    landing_bucket: str = LANDING_BUCKET,
    retrieved_at: str | None = None,
    opener: Callable[[str], BinaryIO] | None = None,
    part_size: int = _STREAM_PART_BYTES,
    timeout_seconds: int = 60,
    _spec: Prm800kSource = PRM800K_SOURCE,
) -> StageResult:
    """Stream the pinned HF mirror into a non-triggering landing staging prefix.

    The source digest is computed while bytes are uploaded.  A receipt appears only after the
    complete four-file set matches the code-pinned OpenAI Git-LFS witnesses.  A complete valid
    receipt makes a retry a no-op; a partial prefix is refused, never resumed blindly.
    """
    _validate_spec(_spec)
    _validate_run_id(run_id)
    if part_size < MIN_MULTIPART_PART_BYTES:
        raise Prm800kIngestError(
            f"part_size must be at least {MIN_MULTIPART_PART_BYTES} bytes for an S3 multipart upload"
        )
    retrieved_at = retrieved_at or _utc_now()
    if not _valid_timestamp(retrieved_at):
        raise Prm800kIngestError(
            "retrieved_at must be an ISO-8601 timestamp with timezone"
        )

    observed = _check_staging_layout(
        s3, landing_bucket=landing_bucket, run_id=run_id, spec=_spec
    )
    receipt_key = _receipt_key(_spec, run_id)
    if observed:
        if receipt_key not in observed:
            raise ReceiptError(
                f"incomplete staging run {_staging_prefix(_spec, run_id)!r}; refusing to overwrite it"
            )
        if observed != _expected_staging_keys(_spec, run_id):
            raise ReceiptError("completed staging run has an incomplete payload set")
        receipt = _read_receipt(s3, landing_bucket, receipt_key)
        _verify_receipt(
            receipt,
            s3=s3,
            landing_bucket=landing_bucket,
            run_id=run_id,
            spec=_spec,
            check_s3=True,
        )
        return StageResult(run_id, _staging_prefix(_spec, run_id), receipt, reused=True)

    open_source = opener or (
        lambda url: _default_open(url, timeout_seconds=timeout_seconds)
    )
    uploaded: dict[str, Mapping[str, Any]] = {}
    try:
        for file in _spec.files:
            url = _spec.transport_url(file)
            with closing(open_source(url)) as response:
                reader = _HashingReader(response, expected_bytes=file.bytes)
                head = s3.put_stream(
                    landing_bucket,
                    _payload_key(_spec, run_id, file),
                    reader,
                    part_size=part_size,
                    content_type="application/x-ndjson",
                )
            if reader.bytes_read != file.bytes or reader.sha256 != file.sha256:
                # A local/remediation identity may delete the known-bad object.  The dedicated
                # ingest role cannot, so it remains receipt-less and clearly distinguishable as a
                # partial run until lifecycle expiry.
                try:
                    s3.delete(landing_bucket, _payload_key(_spec, run_id, file))
                except Exception:
                    pass
                raise SourceVerificationError(
                    f"HF transport witness mismatch for {file.path!r}: got bytes={reader.bytes_read}, "
                    f"sha256={reader.sha256}; expected bytes={file.bytes}, sha256={file.sha256}"
                )
            if head.get("size") != file.bytes:
                raise SourceVerificationError(
                    f"S3 reported bytes={head.get('size')} after uploading {file.path!r}; expected {file.bytes}"
                )
            uploaded[file.path] = {
                "key": _payload_key(_spec, run_id, file),
                "bytes": head["size"],
                "crc64nvme": head.get("crc64nvme"),
            }
    except Exception:
        _cleanup_payload(s3, landing_bucket=landing_bucket, run_id=run_id, spec=_spec)
        raise

    receipt = _build_receipt(
        spec=_spec, run_id=run_id, retrieved_at=retrieved_at, uploaded=uploaded
    )
    # Receipt lives beside payload/, never beneath it, so it can neither be mistaken for a
    # payload file nor trigger the manifest-created EventBridge rule.  A client can lose the
    # response after S3 stored this small commit object, so re-read and verify before deciding
    # whether to clean a failed attempt up.
    try:
        s3.put(
            landing_bucket,
            receipt_key,
            canonical_json(receipt),
            content_type="application/json",
        )
    except Exception as put_error:
        try:
            committed = _read_receipt(s3, landing_bucket, receipt_key)
            _verify_receipt(
                committed,
                s3=s3,
                landing_bucket=landing_bucket,
                run_id=run_id,
                spec=_spec,
                check_s3=True,
            )
        except Exception:
            _cleanup_payload(
                s3, landing_bucket=landing_bucket, run_id=run_id, spec=_spec
            )
            _cleanup_receipt(
                s3, landing_bucket=landing_bucket, run_id=run_id, spec=_spec
            )
            raise Prm800kIngestError(
                "could not commit the staging receipt; the incomplete run remains isolated "
                "until lifecycle expiry, so retry with a fresh run id"
            ) from put_error
        return StageResult(
            run_id, _staging_prefix(_spec, run_id), committed, reused=False
        )
    return StageResult(run_id, _staging_prefix(_spec, run_id), receipt, reused=False)


def _group_meta(spec: Prm800kSource, retrieved_at: str) -> dict[str, Any]:
    return {
        "vendor_root": "raw",
        "upstream": {
            "name": spec.canonical_repo,
            "uri": spec.canonical_uri,
            "revision": spec.canonical_revision,
            "retrieved_at": retrieved_at,
            "transport": {
                "name": spec.hf_repo,
                "uri": spec.hf_uri,
                "revision": spec.hf_revision,
            },
        },
        "sentinels": [],
        "upstream_files": [
            {"path": file.path, "bytes": file.bytes, "sha256": file.sha256}
            for file in spec.files
        ],
    }


def _assert_published_prm800k(
    *,
    s3: S3,
    data_bucket: str,
    spec: Prm800kSource,
) -> None:
    """Verify that an already-catalogued v1 is exactly the source-pinned raw mirror."""
    prefix = f"{spec.dataset_id}/{spec.version}"
    if not _exact_key_exists(s3, bucket=data_bucket, key=f"{prefix}/_VALIDATED.json"):
        raise Prm800kIngestError(f"catalogued {prefix} has no validation seal")
    dataset = _read_json(s3, data_bucket, f"{prefix}/dataset.json")
    groups = dataset.get("groups")
    if (
        not isinstance(groups, list)
        or len(groups) != 1
        or not isinstance(groups[0], Mapping)
    ):
        raise Prm800kIngestError(f"catalogued {prefix} has an unexpected group layout")
    group = groups[0]
    upstream = group.get("upstream")
    if not isinstance(upstream, Mapping) or not _valid_timestamp(
        upstream.get("retrieved_at")
    ):
        raise Prm800kIngestError(
            f"catalogued {prefix} has invalid upstream retrieval provenance"
        )
    expected_meta = _group_meta(spec, str(upstream["retrieved_at"]))
    for key in (
        "name",
        "profile",
        "prefix",
        "manifest",
        "vendor_root",
        "sentinels",
        "upstream_files",
    ):
        expected = "raw" if key == "name" else expected_meta.get(key)
        if key in {"profile", "prefix", "manifest"}:
            expected = {
                "profile": "vendored/v1",
                "prefix": "raw/",
                "manifest": "raw/manifest.json",
            }[key]
        if group.get(key) != expected:
            raise Prm800kIngestError(
                f"catalogued {prefix} has unexpected raw group {key!r}"
            )
    expected_upstream = expected_meta["upstream"]
    for key in ("name", "uri", "revision", "transport"):
        if upstream.get(key) != expected_upstream[key]:
            raise Prm800kIngestError(
                f"catalogued {prefix} has unexpected upstream {key!r}"
            )
    manifest = _read_json(s3, data_bucket, f"{prefix}/raw/manifest.json")
    actual = {
        entry.get("path"): (entry.get("bytes"), entry.get("sha256"))
        for entry in manifest.get("entries", [])
        if isinstance(entry, Mapping)
    }
    expected = {f"raw/{file.path}": (file.bytes, file.sha256) for file in spec.files}
    if actual != expected:
        raise Prm800kIngestError(
            f"catalogued {prefix} payload no longer matches the pinned witnesses"
        )

    # Recompute the published hash chain as well as examining its metadata.  A catalog entry is
    # only a discovery pointer; the validation seal is the authoritative integrity boundary.
    from .read import verify_seal

    problems = verify_seal(
        spec.dataset_id, spec.version, s3=s3, data_bucket=data_bucket
    )
    if problems:
        raise Prm800kIngestError(
            f"catalogued {prefix} fails its validation seal: {'; '.join(problems)}"
        )


def _exact_key_exists(s3: S3, *, bucket: str, key: str) -> bool:
    """Return whether exactly ``key`` exists without relying on ``HeadObject``.

    S3 intentionally hides a missing object's existence behind a 403 response when a caller
    has object access but lacks ``ListBucket``.  That makes a failed ``HeadObject`` ambiguous:
    the producer must never treat it as an absent dataset and risk an overwrite.  Its role is
    therefore allowed to list only each exact PRM800K control key checked here.  Compare exact
    names after the prefix-list because S3's Prefix parameter also returns lookalike suffixes.
    Any denied or malformed list call still propagates as an S3 error rather than becoming a
    guessed absence.
    """
    return any(obj.get("key") == key for obj in s3.list(bucket, key))


def _published_catalog_exists(s3: S3, *, data_bucket: str, spec: Prm800kSource) -> bool:
    return _exact_key_exists(
        s3,
        bucket=data_bucket,
        key=f"_catalog/{spec.dataset_id}/{spec.version}.json",
    )


def _assert_no_uncatalogued_published_prefix(
    s3: S3, *, data_bucket: str, spec: Prm800kSource
) -> None:
    prefix = f"{spec.dataset_id}/{spec.version}"
    for key in (f"{prefix}/_VALIDATED.json", f"{prefix}/dataset.json"):
        if _exact_key_exists(s3, bucket=data_bucket, key=key):
            raise Prm800kIngestError(
                f"{data_bucket}/{prefix} exists without its catalog entry; refusing to overwrite or republish it"
            )


def _assert_landing_version_is_not_terminal(
    s3: S3,
    *,
    landing_bucket: str,
    spec: Prm800kSource,
) -> None:
    """Refuse a fixed-v1 retry once the validator has made a terminal decision.

    A byte-identical manifest is idempotent and therefore does not emit a fresh Object Created
    event.  More importantly, the validator deliberately skips ``_VALIDATED`` and ``_REJECTED``
    landing prefixes on its self-discovery sweep.  Returning ``submitted`` in either state would
    be a false success: a human needs to inspect the terminal result rather than re-run v1.
    """
    prefix = f"{spec.dataset_id}/{spec.version}"
    for marker_name in ("_REJECTED.json", "_VALIDATED.json"):
        key = f"{prefix}/{marker_name}"
        if not _exact_key_exists(s3, bucket=landing_bucket, key=key):
            continue
        marker = _read_json(s3, landing_bucket, key)
        if marker_name == "_REJECTED.json":
            raw_violations = marker.get("violations")
            codes = (
                [
                    item.get("code")
                    for item in raw_violations
                    if isinstance(item, Mapping) and isinstance(item.get("code"), str)
                ]
                if isinstance(raw_violations, list)
                else []
            )
            detail = f" (violations: {', '.join(codes[:3])})" if codes else ""
            raise Prm800kIngestError(
                f"landing {prefix} is terminally rejected{detail}; do not retry fixed v1 without resolving "
                "the rejection and an explicitly approved recovery plan"
            )
        raise Prm800kIngestError(
            f"landing {prefix} is already marked validated but has no published catalog entry; "
            "inspect the validator/promotion outcome instead of retrying fixed v1"
        )


def _landing_reservation(
    s3: S3,
    *,
    landing_bucket: str,
    spec: Prm800kSource,
) -> tuple[str, Mapping[str, Any]] | None:
    """Return immutable creation facts from a partially published fixed version, if any.

    A Batch retry is a new execution attempt and therefore has a different job ID.  Replacing
    that provenance or the creation timestamp would make the otherwise-identical v1 metadata
    differ and force a collision.  Reuse the first reservation's facts instead.
    """
    key = f"{spec.dataset_id}/{spec.version}/dataset.json"
    if not _exact_key_exists(s3, bucket=landing_bucket, key=key):
        return None
    existing = _read_json(s3, landing_bucket, key)
    version_doc = existing.get("version")
    if (
        existing.get("dataset_id") != spec.dataset_id
        or not isinstance(version_doc, Mapping)
        or version_doc.get("id") != spec.version
    ):
        raise Prm800kIngestError(
            f"landing reservation {key} identifies a different dataset or version"
        )
    created_at = existing.get("created_at")
    executor = (
        existing.get("build", {}).get("executor")
        if isinstance(existing.get("build"), Mapping)
        else None
    )
    if not _valid_timestamp(created_at) or not isinstance(executor, Mapping):
        raise Prm800kIngestError(
            f"landing reservation {key} lacks immutable creation provenance"
        )
    return str(created_at), executor


def publish_prm800k(
    *,
    s3: S3,
    run_id: str,
    created_at: str | None = None,
    landing_bucket: str = LANDING_BUCKET,
    data_bucket: str = DATA_BUCKET,
    env: Mapping[str, str] | None = None,
    _spec: Prm800kSource = PRM800K_SOURCE,
) -> PublishResult:
    """Seal the completed stage as fixed ``vendor/openai-prm800k/v1`` landing data.

    This function intentionally ends after the final landing manifest is written.  The enabled
    EventBridge rule is the wake-up signal for validation/promotion; a client never calls
    ``promote()`` and never writes the read bucket.
    """
    _validate_spec(_spec)
    _validate_run_id(run_id)
    effective_env: Mapping[str, str] = env if env is not None else os.environ
    if created_at is not None and not _valid_timestamp(created_at):
        raise Prm800kIngestError(
            "created_at must be an ISO-8601 timestamp with timezone"
        )

    if _published_catalog_exists(s3, data_bucket=data_bucket, spec=_spec):
        _assert_published_prm800k(s3=s3, data_bucket=data_bucket, spec=_spec)
        return PublishResult(_spec.version, "already-published", None)
    _assert_no_uncatalogued_published_prefix(s3, data_bucket=data_bucket, spec=_spec)
    _assert_landing_version_is_not_terminal(
        s3, landing_bucket=landing_bucket, spec=_spec
    )

    reservation = _landing_reservation(s3, landing_bucket=landing_bucket, spec=_spec)
    if reservation is not None:
        reserved_created_at, reserved_executor = reservation
        if created_at is not None and created_at != reserved_created_at:
            raise Prm800kIngestError(
                "fixed v1 is already reserved with a different created_at; retry without --created-at"
            )
        created_at = reserved_created_at
    else:
        reserved_executor = None
        created_at = created_at or _utc_now()

    receipt = _read_receipt(s3, landing_bucket, _receipt_key(_spec, run_id))
    _verify_receipt(
        receipt,
        s3=s3,
        landing_bucket=landing_bucket,
        run_id=run_id,
        spec=_spec,
        check_s3=True,
    )
    # Do this only after all no-write receipt checks have passed.  An interrupted Batch retry
    # with no staging receipt should still report that concrete operational state rather than an
    # unrelated image-provenance configuration error.
    current_executor = _require_batch_provenance(effective_env)
    if reserved_executor is not None and current_executor is not None:
        _require_recorded_batch_provenance(reserved_executor)
    plan = publish(
        f"s3://{landing_bucket}/{_payload_prefix(_spec, run_id)}",
        dataset_id=_spec.dataset_id,
        purpose="Byte-preserving OpenAI PRM800K mirror for provenance and controlled derived datasets",
        profile="vendored/v1",
        s3=s3,
        created_at=created_at,
        data_bucket=data_bucket,
        landing_bucket=landing_bucket,
        group_meta={"raw": _group_meta(_spec, str(receipt["retrieved_at"]))},
        expected_payload=_expected_payload(_spec),
        expected_version=_spec.version,
        build_executor=(
            dict(reserved_executor)
            if reserved_executor is not None
            else current_executor
        ),
        env=effective_env,
        license={"id": _spec.license_id, "basis": "declared"},
        sources=[
            {
                "name": "OpenAI PRM800K",
                "uri": _spec.canonical_uri,
                "license": _spec.license_id,
            }
        ],
        about=(
            "Byte-preserving mirror of the four-file OpenAI PRM800K release. It is retained "
            "for provenance and controlled derivation, not as a train-ready SFT or process-reward dataset."
        ),
        notes=(
            f"Transport is pinned to Hugging Face {_spec.hf_repo}@{_spec.hf_revision}; "
            f"canonical upstream is {_spec.canonical_repo}@{_spec.canonical_revision}."
        ),
        limitations=[
            {
                "kind": "raw-vendor-mirror",
                "detail": (
                    "Records are preserved verbatim, including opaque labeler identifiers and timestamps. "
                    "Any train-ready representation must be a separately validated derived artifact."
                ),
            }
        ],
    )
    return PublishResult(_spec.version, "submitted", plan)


def _require_batch_environment() -> None:
    if not os.environ.get("AWS_BATCH_JOB_ID"):
        raise Prm800kIngestError(
            "PRM800K stage/publish commands are Batch-only; run them inside the approved AWS Batch job"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="edullm-prm800k-ingest",
        description="Stage and publish the pinned PRM800K raw vendor mirror from AWS Batch",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser(
        "stage", help="stream and verify the four pinned HF files into landing staging"
    )
    stage.add_argument("--run-id", required=True)
    stage.add_argument("--landing-bucket", default=LANDING_BUCKET)
    stage.add_argument("--retrieved-at", default=None)
    stage.add_argument("--part-size", type=int, default=_STREAM_PART_BYTES)
    stage.add_argument("--timeout-seconds", type=int, default=60)

    publish_cmd = sub.add_parser(
        "publish", help="seal a completed stage as the fixed v1 landing artifact"
    )
    publish_cmd.add_argument("--run-id", required=True)
    publish_cmd.add_argument("--landing-bucket", default=LANDING_BUCKET)
    publish_cmd.add_argument("--data-bucket", default=DATA_BUCKET)
    publish_cmd.add_argument("--created-at", default=None)

    args = parser.parse_args(argv)
    try:
        _require_batch_environment()
        s3 = Boto3S3.default()
        if args.command == "stage":
            result = stage_prm800k(
                s3=s3,
                run_id=args.run_id,
                landing_bucket=args.landing_bucket,
                retrieved_at=args.retrieved_at,
                part_size=args.part_size,
                timeout_seconds=args.timeout_seconds,
            )
            print(
                json.dumps(
                    {
                        "status": "reused" if result.reused else "staged",
                        "run_id": result.run_id,
                        "staging_prefix": result.staging_prefix,
                        "retrieved_at": result.receipt["retrieved_at"],
                    },
                    sort_keys=True,
                )
            )
        else:
            result = publish_prm800k(
                s3=s3,
                run_id=args.run_id,
                landing_bucket=args.landing_bucket,
                data_bucket=args.data_bucket,
                created_at=args.created_at,
            )
            print(
                json.dumps(
                    {"status": result.status, "version": result.version}, sort_keys=True
                )
            )
        return 0
    except (Prm800kIngestError, PublishError, S3Error, OSError, ValueError) as exc:
        print(f"PRM800K ingest failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PRM800K_SOURCE",
    "Prm800kIngestError",
    "ReceiptError",
    "SourceVerificationError",
    "StageResult",
    "PublishResult",
    "stage_prm800k",
    "publish_prm800k",
    "main",
]
