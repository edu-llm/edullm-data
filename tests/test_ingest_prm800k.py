"""PRM800K raw-vendor staging and fixed-version publication tests."""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from typing import Any

import pytest

from edullm_data import ingest_prm800k as I
from edullm_data import publish as P
from edullm_data import read as R
from edullm_data import validate as V
from edullm_data.contracts import canonical_json
from edullm_data.manifest import manifest_sha256
from edullm_data.s3 import Boto3S3, MIN_MULTIPART_PART_BYTES, FakeS3, S3Error

CREATED = "2026-08-01T12:00:00Z"
RETRIEVED = "2026-08-01T11:59:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}
PART_SIZE = MIN_MULTIPART_PART_BYTES


class RecordingResponse(io.BytesIO):
    """A fake HTTP response that proves the uploader never requests an unbounded read."""

    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


class NoPayloadGetFakeS3(FakeS3):
    """Fail if publish tries to whole-load a staged raw JSONL payload."""

    def __init__(self) -> None:
        super().__init__()
        self.forbidden_prefixes: list[str] = []

    def get(self, bucket: str, key: str) -> bytes:
        if any(key.startswith(prefix) for prefix in self.forbidden_prefixes):
            raise AssertionError(f"whole-object get() forbidden for payload {key}")
        return super().get(bucket, key)


class OpaqueMissingControlS3(FakeS3):
    """Model S3's deliberately ambiguous missing-object behavior for a narrow role.

    A role which can read an exact object but cannot list its bucket gets a 403 from HEAD when
    that object is absent.  The live producer must instead use an exact-prefix ListObjectsV2
    check.  Landing controls exercise the same no-guessing behavior before an absent GET.
    """

    def __init__(
        self,
        *,
        published_controls: set[str],
        absent_landing_controls: set[str],
    ) -> None:
        super().__init__()
        self.published_controls = published_controls
        self.absent_landing_controls = absent_landing_controls
        self.published_head_attempts: list[str] = []
        self.absent_landing_get_attempts: list[str] = []
        self.published_list_prefixes: list[str] = []
        self.landing_control_list_prefixes: list[str] = []

    def head(self, bucket: str, key: str) -> dict:
        if bucket == "edullm-data" and key in self.published_controls:
            self.published_head_attempts.append(key)
            raise S3Error("simulated S3 403: missing object is opaque without ListBucket")
        return super().head(bucket, key)

    def get(self, bucket: str, key: str) -> bytes:
        if (
            bucket == "edullm-landing"
            and key in self.absent_landing_controls
            and (bucket, key) not in self._store
        ):
            self.absent_landing_get_attempts.append(key)
            raise S3Error("simulated S3 403: missing object is opaque without ListBucket")
        return super().get(bucket, key)

    def list(self, bucket: str, prefix: str) -> list[dict]:
        if bucket == "edullm-data":
            self.published_list_prefixes.append(prefix)
        if bucket == "edullm-landing" and prefix in self.absent_landing_controls:
            self.landing_control_list_prefixes.append(prefix)
        return super().list(bucket, prefix)


class ReceiptPutFailureS3(FakeS3):
    """Fail a receipt PUT before or after storing it, as an HTTP client can observe."""

    def __init__(self, *, commit_before_failure: bool) -> None:
        super().__init__()
        self.commit_before_failure = commit_before_failure

    def put(
        self, bucket: str, key: str, body: bytes, *, content_type: str | None = None
    ) -> None:
        if key.endswith("/receipt.json"):
            if self.commit_before_failure:
                super().put(bucket, key, body, content_type=content_type)
            raise OSError("simulated lost receipt PUT response")
        super().put(bucket, key, body, content_type=content_type)


class SwapAfterHashS3(FakeS3):
    """Simulate a same-size staging overwrite after hash and before server-side copy."""

    def arm_swap(self, bucket: str, key: str, replacement: bytes) -> None:
        self._swap = (bucket, key, replacement)

    def hash_object(self, bucket: str, key: str) -> tuple[str, int]:
        digest, size = super().hash_object(bucket, key)
        armed = getattr(self, "_swap", None)
        if armed is not None and (bucket, key) == armed[:2]:
            self.seed(bucket, key, armed[2])
            self._swap = None
        return digest, size


class SwapDestinationAfterCopyS3(FakeS3):
    """Corrupt one destination object after a conditionally valid copy for race coverage."""

    def arm_destination_swap(self, bucket: str, key: str, replacement: bytes) -> None:
        self._destination_swap = (bucket, key, replacement)

    def copy(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
        *,
        source_etag: str | None = None,
    ) -> None:
        super().copy(
            src_bucket,
            src_key,
            dst_bucket,
            dst_key,
            source_etag=source_etag,
        )
        armed = getattr(self, "_destination_swap", None)
        if armed is not None and (dst_bucket, dst_key) == armed[:2]:
            self.seed(dst_bucket, dst_key, armed[2])
            self._destination_swap = None


class PersistentDestinationSwapS3(FakeS3):
    """Corrupt the same destination after every copy to exercise retry exhaustion."""

    def arm_destination_swap(self, bucket: str, key: str, replacement: bytes) -> None:
        self._destination_swap = (bucket, key, replacement)

    def copy(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
        *,
        source_etag: str | None = None,
    ) -> None:
        super().copy(
            src_bucket,
            src_key,
            dst_bucket,
            dst_key,
            source_etag=source_etag,
        )
        armed = getattr(self, "_destination_swap", None)
        if armed is not None and (dst_bucket, dst_key) == armed[:2]:
            self.seed(dst_bucket, dst_key, armed[2])


class SwapSourceBeforeCopyS3(FakeS3):
    """Change a source object immediately before its conditional promotion copy."""

    def arm_source_swap(self, bucket: str, key: str, replacement: bytes) -> None:
        self._source_swap = (bucket, key, replacement)

    def copy(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
        *,
        source_etag: str | None = None,
    ) -> None:
        armed = getattr(self, "_source_swap", None)
        if armed is not None and (src_bucket, src_key) == armed[:2]:
            self.seed(src_bucket, src_key, armed[2])
            self._source_swap = None
        super().copy(
            src_bucket,
            src_key,
            dst_bucket,
            dst_key,
            source_etag=source_etag,
        )


class LostLandingMarkerResponseS3(FakeS3):
    """Fail exactly one landing marker PUT after the data promotion has succeeded."""

    def __init__(self) -> None:
        super().__init__()
        self.copy_calls = 0
        self._lost_marker: tuple[str, str] | None = None

    def arm_lost_marker_response(self, bucket: str, key: str) -> None:
        self._lost_marker = (bucket, key)

    def copy(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
        *,
        source_etag: str | None = None,
    ) -> None:
        self.copy_calls += 1
        super().copy(
            src_bucket,
            src_key,
            dst_bucket,
            dst_key,
            source_etag=source_etag,
        )

    def put(
        self,
        bucket: str,
        key: str,
        body: bytes,
        *,
        content_type: str | None = None,
        if_none_match: bool = False,
    ) -> None:
        if self._lost_marker == (bucket, key):
            self._lost_marker = None
            raise OSError("simulated lost landing marker PUT response")
        super().put(
            bucket,
            key,
            body,
            content_type=content_type,
            if_none_match=if_none_match,
        )


def _test_spec(bodies: dict[str, bytes]) -> I.Prm800kSource:
    return I.Prm800kSource(
        # Fixture-sized bytes must not masquerade as the code-pinned production identity.
        dataset_id="vendor/mini-prm800k",
        version="v1",
        hf_repo="example/prm800k-mirror",
        hf_revision="a" * 40,
        canonical_repo="openai/prm800k",
        canonical_uri="https://github.com/openai/prm800k",
        canonical_revision="b" * 40,
        license_id="MIT",
        files=tuple(
            I.UpstreamFile(path, len(body), hashlib.sha256(body).hexdigest())
            for path, body in sorted(bodies.items())
        ),
    )


def _opener(
    spec: I.Prm800kSource, bodies: dict[str, bytes]
) -> tuple[Any, list[RecordingResponse]]:
    urls = {spec.transport_url(file): bodies[file.path] for file in spec.files}
    responses: list[RecordingResponse] = []

    def open_source(url: str) -> RecordingResponse:
        response = RecordingResponse(urls[url])
        responses.append(response)
        return response

    return open_source, responses


def _bodies() -> dict[str, bytes]:
    return {
        "phase1_test.jsonl": b'{"question":{"problem":"one"},"label":{"steps":[]}}\n',
        "phase2_train.jsonl": b'{"question":{"problem":"two"},"label":{"steps":[]}}\n',
    }


def _stage_and_publish_pinned_fixture(
    monkeypatch: pytest.MonkeyPatch, s3: FakeS3
) -> tuple[I.Prm800kSource, dict[str, bytes]]:
    """Create a small stand-in for the code-pinned production identity.

    The validator imports ``PRM800K_SOURCE`` from its installed code, so replacing that one
    module constant lets this unit test prove the trusted-contract mechanism without uploading
    the real 477 MB release.
    """
    bodies = _bodies()
    spec = replace(_test_spec(bodies), dataset_id="vendor/openai-prm800k")
    monkeypatch.setattr(I, "PRM800K_SOURCE", spec)
    opener, _ = _opener(spec, bodies)
    I.stage_prm800k(
        s3=s3,
        run_id="pinned-fixture",
        retrieved_at=RETRIEVED,
        opener=opener,
        part_size=PART_SIZE,
        _spec=spec,
    )
    I.publish_prm800k(
        s3=s3,
        run_id="pinned-fixture",
        created_at=CREATED,
        env=ENV,
        _spec=spec,
    )
    return spec, bodies


def test_source_table_is_the_pinned_openai_lfs_release():
    source = I.PRM800K_SOURCE
    assert source.hf_repo == "tasksource/PRM800K"
    assert source.hf_revision == "547b19506677a59037ee888838834b65e9b1ddd4"
    assert source.canonical_revision == "00811d6de065642a6967b9017d4cee59550c0ef4"
    assert source.license_id == "MIT"
    assert sum(file.bytes for file in source.files) == 477_105_425
    assert {file.path: (file.bytes, file.sha256) for file in source.files} == {
        "phase1_test.jsonl": (
            829_105,
            "f4b3bc5b095e45c816453dc4d748b755c680d61d55f9895d929a335b487c727d",
        ),
        "phase1_train.jsonl": (
            7_900_236,
            "e9da6a73f827ffb9a8c0dc644c541d34ed76b3d4d1e4896ff5f7b37ddf5ae34d",
        ),
        "phase2_test.jsonl": (
            12_240_719,
            "6b172efa884ac8341a946dd82e06947c135b7254109fb3f7aa907c715d98aaad",
        ),
        "phase2_train.jsonl": (
            456_135_365,
            "1110237feeb51d1bc200cb37b8f965cfdc1036eac7d506094049366fe7dc1089",
        ),
    }


def test_stage_publish_validate_promote_round_trip_is_streamed_and_fixed_to_v1():
    bodies = _bodies()
    spec = _test_spec(bodies)
    s3 = NoPayloadGetFakeS3()
    opener, responses = _opener(spec, bodies)
    run_id = "prm800k-test-run"

    stage = I.stage_prm800k(
        s3=s3,
        run_id=run_id,
        retrieved_at=RETRIEVED,
        opener=opener,
        part_size=PART_SIZE,
        _spec=spec,
    )
    assert not stage.reused
    assert all(
        0 < size <= PART_SIZE for response in responses for size in response.read_sizes
    )
    staged = s3.dump("edullm-landing")
    assert set(staged) == {
        I._payload_key(spec, run_id, file) for file in spec.files
    } | {I._receipt_key(spec, run_id)}
    assert not any(key.endswith(("dataset.json", "manifest.json")) for key in staged)

    calls_before_reuse = len(responses)
    reused = I.stage_prm800k(
        s3=s3,
        run_id=run_id,
        retrieved_at=RETRIEVED,
        opener=opener,
        part_size=PART_SIZE,
        _spec=spec,
    )
    assert reused.reused
    assert (
        len(responses) == calls_before_reuse
    )  # receipt+head checks only; no transport re-fetch

    s3.forbidden_prefixes.append(I._payload_prefix(spec, run_id) + "/raw/")
    first = I.publish_prm800k(
        s3=s3, run_id=run_id, created_at=CREATED, env=ENV, _spec=spec
    )
    assert first.status == "submitted"
    assert first.version == "v1"
    assert first.plan is not None
    raw_manifest = first.plan.manifests["raw"]
    assert {entry["path"] for entry in raw_manifest["entries"]} == {
        f"raw/{file.path}" for file in spec.files
    }
    assert all("count" not in entry for entry in raw_manifest["entries"])
    assert all("receipt.json" not in entry["path"] for entry in raw_manifest["entries"])

    # A retry after the fixed v1 reservation is still v1 even though this invocation has a
    # different execution environment and no copied --created-at argument.
    retry = I.publish_prm800k(
        s3=s3,
        run_id=run_id,
        env={"EDULLM_CODE_SHA256": "c" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "d" * 64},
        _spec=spec,
    )
    assert retry.status == "submitted" and retry.version == "v1"
    assert not any(
        key.startswith(f"{spec.dataset_id}/v2/") for key in s3.dump("edullm-landing")
    )

    result = V.validate_dataset(
        "edullm-landing", f"{spec.dataset_id}/v1", s3, data_bucket="edullm-data"
    )
    assert result.ok, [str(violation) for violation in result.violations]
    V.promote(result, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")

    with pytest.raises(R.VendoredReadRequiresOptIn):
        R.dataset_paths(spec.dataset_id, "v1", s3=s3)
    raw = R.dataset_paths(spec.dataset_id, "v1", s3=s3, allow_vendored=True)
    assert len(raw.paths) == len(spec.files)

    already = I.publish_prm800k(s3=s3, run_id=run_id, _spec=spec)
    assert already.status == "already-published" and already.plan is None


def test_publish_uses_exact_lists_for_opaque_absent_control_objects():
    """A 403 from an absent HEAD/GET must not be misread as a safe absence."""
    bodies = _bodies()
    spec = _test_spec(bodies)
    prefix = f"{spec.dataset_id}/{spec.version}"
    published_controls = {
        f"_catalog/{spec.dataset_id}/{spec.version}.json",
        f"{prefix}/_VALIDATED.json",
        f"{prefix}/dataset.json",
    }
    absent_landing_controls = {
        f"{prefix}/_REJECTED.json",
        f"{prefix}/_VALIDATED.json",
        f"{prefix}/dataset.json",
    }
    s3 = OpaqueMissingControlS3(
        published_controls=published_controls,
        absent_landing_controls=absent_landing_controls,
    )
    opener, _ = _opener(spec, bodies)
    run_id = "opaque-control-absence"
    I.stage_prm800k(
        s3=s3,
        run_id=run_id,
        retrieved_at=RETRIEVED,
        opener=opener,
        part_size=PART_SIZE,
        _spec=spec,
    )

    # Prefix search can return this lookalike, but an exact-key check must ignore it.
    catalog_key = f"_catalog/{spec.dataset_id}/{spec.version}.json"
    s3.seed("edullm-data", catalog_key + ".stale", b"not a catalog entry")
    s3.seed("edullm-landing", f"{prefix}/dataset.json.stale", b"not a reservation")

    result = I.publish_prm800k(
        s3=s3, run_id=run_id, created_at=CREATED, env=ENV, _spec=spec
    )

    assert result.status == "submitted"
    assert s3.published_head_attempts == []
    assert s3.absent_landing_get_attempts == []
    assert s3.published_list_prefixes == [
        catalog_key,
        f"{prefix}/_VALIDATED.json",
        f"{prefix}/dataset.json",
    ]
    assert s3.landing_control_list_prefixes == [
        f"{prefix}/_REJECTED.json",
        f"{prefix}/_VALIDATED.json",
        f"{prefix}/dataset.json",
    ]


def test_stage_hash_mismatch_cleans_payload_and_never_writes_receipt():
    bodies = _bodies()
    spec = _test_spec(bodies)
    bad = dict(bodies)
    bad["phase1_test.jsonl"] = b'{"question":{"problem":"bad"},"label":{"steps":[]}}\n'
    s3 = FakeS3()
    opener, _ = _opener(spec, bad)

    with pytest.raises(I.SourceVerificationError):
        I.stage_prm800k(
            s3=s3,
            run_id="bad-source",
            retrieved_at=RETRIEVED,
            opener=opener,
            part_size=PART_SIZE,
            _spec=spec,
        )
    assert s3.dump("edullm-landing") == {}


def test_stage_rejects_an_oversized_transport_before_excess_bytes_reach_landing():
    bodies = _bodies()
    spec = _test_spec(bodies)
    oversized = dict(bodies)
    first = spec.files[0]
    oversized[first.path] = bodies[first.path] + b"x"
    s3 = FakeS3()
    opener, _ = _opener(spec, oversized)

    with pytest.raises(I.SourceVerificationError, match="more bytes"):
        I.stage_prm800k(
            s3=s3,
            run_id="oversized-source",
            retrieved_at=RETRIEVED,
            opener=opener,
            part_size=PART_SIZE,
            _spec=spec,
        )
    assert s3.dump("edullm-landing") == {}


def test_partial_or_tampered_staging_cannot_write_a_final_dataset_manifest():
    bodies = _bodies()
    spec = _test_spec(bodies)
    s3 = FakeS3()
    run_id = "partial-stage"
    s3.seed("edullm-landing", I._payload_key(spec, run_id, spec.files[0]), b"partial")
    with pytest.raises(I.ReceiptError, match="incomplete staging"):
        I.stage_prm800k(
            s3=s3, run_id=run_id, opener=lambda _: io.BytesIO(b""), _spec=spec
        )

    run_id = "tampered-receipt"
    opener, _ = _opener(spec, bodies)
    I.stage_prm800k(
        s3=s3,
        run_id=run_id,
        retrieved_at=RETRIEVED,
        opener=opener,
        part_size=PART_SIZE,
        _spec=spec,
    )
    receipt = json.loads(s3.get("edullm-landing", I._receipt_key(spec, run_id)))
    receipt["source"]["transport"]["revision"] = "f" * 40
    s3.seed(
        "edullm-landing", I._receipt_key(spec, run_id), json.dumps(receipt).encode()
    )
    with pytest.raises(I.ReceiptError, match="source pins"):
        I.publish_prm800k(s3=s3, run_id=run_id, created_at=CREATED, env=ENV, _spec=spec)
    assert f"{spec.dataset_id}/v1/dataset.json" not in s3.dump("edullm-landing")


def test_same_size_payload_tampering_fails_pinned_stream_hash_before_reservation():
    bodies = _bodies()
    spec = _test_spec(bodies)
    s3 = FakeS3()
    run_id = "tampered-payload"
    opener, _ = _opener(spec, bodies)
    I.stage_prm800k(
        s3=s3,
        run_id=run_id,
        retrieved_at=RETRIEVED,
        opener=opener,
        part_size=PART_SIZE,
        _spec=spec,
    )
    file = spec.files[0]
    key = I._payload_key(spec, run_id, file)
    receipt = json.loads(s3.get("edullm-landing", I._receipt_key(spec, run_id)))
    recorded_crc = receipt["files"][0]["s3"]["crc64nvme"]
    replacement = bodies[file.path].replace(b"one", b"two")
    assert len(replacement) == file.bytes
    s3.seed("edullm-landing", key, replacement)
    # Simulate a provider whose HEAD checksum is unavailable/stale: the publish-time streamed
    # expected_payload witness is the second, independent defence and must still fail before
    # dataset.json is reserved.
    s3.override_head("edullm-landing", key, crc64nvme=recorded_crc)
    with pytest.raises(P.PublishError, match="staged payload witness mismatch"):
        I.publish_prm800k(s3=s3, run_id=run_id, created_at=CREATED, env=ENV, _spec=spec)
    assert f"{spec.dataset_id}/v1/dataset.json" not in s3.dump("edullm-landing")


def test_source_swap_between_hash_and_copy_never_commits_a_manifest():
    bodies = _bodies()
    spec = _test_spec(bodies)
    s3 = SwapAfterHashS3()
    run_id = "hash-copy-race"
    opener, _ = _opener(spec, bodies)
    I.stage_prm800k(
        s3=s3,
        run_id=run_id,
        retrieved_at=RETRIEVED,
        opener=opener,
        part_size=PART_SIZE,
        _spec=spec,
    )
    file = spec.files[0]
    replacement = bodies[file.path].replace(b"one", b"two")
    assert len(replacement) == file.bytes
    s3.arm_swap("edullm-landing", I._payload_key(spec, run_id, file), replacement)

    with pytest.raises(P.PublishError, match="final payload witness mismatch"):
        I.publish_prm800k(s3=s3, run_id=run_id, created_at=CREATED, env=ENV, _spec=spec)
    landing = s3.dump("edullm-landing")
    assert (
        f"{spec.dataset_id}/v1/dataset.json" in landing
    )  # fixed v1 remains reserved for recovery
    assert f"{spec.dataset_id}/v1/raw/manifest.json" not in landing


def test_gate_a_rejects_a_self_consistent_mutation_after_final_publish_hash(
    monkeypatch: pytest.MonkeyPatch,
):
    """Mutable landing metadata cannot replace the code-pinned PRM800K source contract."""
    s3 = FakeS3()
    spec, bodies = _stage_and_publish_pinned_fixture(monkeypatch, s3)
    target = spec.files[0]
    prefix = f"{spec.dataset_id}/{spec.version}"
    replacement = bodies[target.path].replace(b"one", b"two")
    assert len(replacement) == target.bytes

    # Make the producer-controlled payload, manifest, dataset group hash, and upstream_files
    # all agree with the altered bytes.  The generic vendored checks would accept this tree;
    # the validator's code-pinned PRM800K witness table must not.
    s3.seed("edullm-landing", f"{prefix}/raw/{target.path}", replacement)
    manifest_key = f"{prefix}/raw/manifest.json"
    manifest = json.loads(s3.get("edullm-landing", manifest_key))
    entry = next(
        item for item in manifest["entries"] if item["path"] == f"raw/{target.path}"
    )
    entry["sha256"] = hashlib.sha256(replacement).hexdigest()
    dataset_key = f"{prefix}/dataset.json"
    dataset = json.loads(s3.get("edullm-landing", dataset_key))
    dataset["groups"][0]["manifest_sha256"] = manifest_sha256(manifest)
    for witness in dataset["groups"][0]["upstream_files"]:
        if witness["path"] == target.path:
            witness["sha256"] = entry["sha256"]
    s3.seed("edullm-landing", manifest_key, canonical_json(manifest))
    s3.seed("edullm-landing", dataset_key, canonical_json(dataset))

    result = V.validate_dataset("edullm-landing", prefix, s3, data_bucket="edullm-data")
    codes = {violation.code for violation in result.violations}
    assert not result.ok
    assert {
        "pinned-vendor-witness-mismatch",
        "pinned-vendor-payload-witness-mismatch",
    } & codes


def test_promotion_rejects_landing_payload_changed_after_gate_a_without_sealing(
    monkeypatch: pytest.MonkeyPatch,
):
    s3 = FakeS3()
    spec, bodies = _stage_and_publish_pinned_fixture(monkeypatch, s3)
    prefix = f"{spec.dataset_id}/{spec.version}"
    result = V.validate_dataset("edullm-landing", prefix, s3, data_bucket="edullm-data")
    assert result.ok, [str(violation) for violation in result.violations]

    target = spec.files[0]
    replacement = bodies[target.path].replace(b"one", b"two")
    assert len(replacement) == target.bytes
    s3.seed("edullm-landing", f"{prefix}/raw/{target.path}", replacement)

    with pytest.raises(V.PromotionIntegrityError, match="changed after Gate A"):
        V.promote(
            result, s3, data_bucket="edullm-data", landing_bucket="edullm-landing"
        )
    assert f"{prefix}/_VALIDATED.json" not in s3.dump("edullm-data")
    # CopySourceIfMatch fails before the corrupted source becomes a data-bucket object.
    assert f"{prefix}/raw/{target.path}" not in s3.dump("edullm-data")


def test_promotion_rehashes_destination_and_recovers_an_unsealed_partial_prefix(
    monkeypatch: pytest.MonkeyPatch,
):
    s3 = SwapDestinationAfterCopyS3()
    spec, bodies = _stage_and_publish_pinned_fixture(monkeypatch, s3)
    prefix = f"{spec.dataset_id}/{spec.version}"
    result = V.validate_dataset("edullm-landing", prefix, s3, data_bucket="edullm-data")
    assert result.ok, [str(violation) for violation in result.violations]

    target = spec.files[0]
    replacement = bodies[target.path].replace(b"one", b"two")
    assert len(replacement) == target.bytes
    s3.arm_destination_swap("edullm-data", f"{prefix}/raw/{target.path}", replacement)

    with pytest.raises(
        V.PromotionIntegrityError, match="differs from its validation snapshot"
    ):
        V.promote(
            result, s3, data_bucket="edullm-data", landing_bucket="edullm-landing"
        )
    assert f"{prefix}/_VALIDATED.json" not in s3.dump("edullm-data")

    # A retry hashes the existing unsealed object, refuses to reuse its wrong bytes, copies the
    # still ETag-bound source again, and seals only after it matches the original Gate-A plan.
    V.promote(result, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")
    assert f"{prefix}/_VALIDATED.json" in s3.dump("edullm-data")


def test_validator_cli_retries_a_transient_destination_copy_mismatch(
    monkeypatch: pytest.MonkeyPatch,
):
    """The EventBridge wake-up has one Batch attempt, so a safe retry must happen in-process."""
    s3 = SwapDestinationAfterCopyS3()
    spec, bodies = _stage_and_publish_pinned_fixture(monkeypatch, s3)
    prefix = f"{spec.dataset_id}/{spec.version}"
    target = spec.files[0]
    replacement = bodies[target.path].replace(b"one", b"two")
    assert len(replacement) == target.bytes
    s3.arm_destination_swap("edullm-data", f"{prefix}/raw/{target.path}", replacement)
    monkeypatch.setattr(Boto3S3, "default", staticmethod(lambda: s3))

    assert (
        V.main(
            [
                "--landing-bucket",
                "edullm-landing",
                "--data-bucket",
                "edullm-data",
                "--prefix",
                prefix,
                "--promote",
            ]
        )
        == 0
    )
    assert f"{prefix}/_VALIDATED.json" in s3.dump("edullm-data")
    assert f"{prefix}/_VALIDATED.json" in s3.dump("edullm-landing")
    assert f"{prefix}/_REJECTED.json" not in s3.dump("edullm-landing")


def test_validator_cli_terminalizes_a_persistent_destination_copy_mismatch(
    monkeypatch: pytest.MonkeyPatch,
):
    """A persistent bad copy never gets a seal or a false PASS after the bounded retry."""
    s3 = PersistentDestinationSwapS3()
    spec, bodies = _stage_and_publish_pinned_fixture(monkeypatch, s3)
    prefix = f"{spec.dataset_id}/{spec.version}"
    target = spec.files[0]
    replacement = bodies[target.path].replace(b"one", b"two")
    assert len(replacement) == target.bytes
    s3.arm_destination_swap("edullm-data", f"{prefix}/raw/{target.path}", replacement)
    monkeypatch.setattr(Boto3S3, "default", staticmethod(lambda: s3))

    assert (
        V.main(
            [
                "--landing-bucket",
                "edullm-landing",
                "--data-bucket",
                "edullm-data",
                "--prefix",
                prefix,
                "--promote",
            ]
        )
        == 1
    )
    assert f"{prefix}/_VALIDATED.json" not in s3.dump("edullm-data")
    assert f"{prefix}/_VALIDATED.json" not in s3.dump("edullm-landing")
    rejection = json.loads(s3.get("edullm-landing", f"{prefix}/_REJECTED.json"))
    assert rejection["violations"][0]["code"] == "vendored-final-copy-mismatch"
    assert V.discover_pending("edullm-landing", s3) == []


def test_validator_cli_terminalizes_a_landing_source_change_after_gate_a(
    monkeypatch: pytest.MonkeyPatch,
):
    """CopySourceIfMatch drift is evidence the approved source no longer exists."""
    s3 = SwapSourceBeforeCopyS3()
    spec, bodies = _stage_and_publish_pinned_fixture(monkeypatch, s3)
    prefix = f"{spec.dataset_id}/{spec.version}"
    target = spec.files[0]
    replacement = bodies[target.path].replace(b"one", b"two")
    assert len(replacement) == target.bytes
    s3.arm_source_swap("edullm-landing", f"{prefix}/raw/{target.path}", replacement)
    monkeypatch.setattr(Boto3S3, "default", staticmethod(lambda: s3))

    assert (
        V.main(
            [
                "--landing-bucket",
                "edullm-landing",
                "--data-bucket",
                "edullm-data",
                "--prefix",
                prefix,
                "--promote",
            ]
        )
        == 1
    )
    assert f"{prefix}/_VALIDATED.json" not in s3.dump("edullm-data")
    rejection = json.loads(s3.get("edullm-landing", f"{prefix}/_REJECTED.json"))
    assert rejection["violations"][0]["code"] == "vendored-landing-source-changed"
    assert V.discover_pending("edullm-landing", s3) == []


def test_validator_cli_reconciles_a_lost_landing_marker_without_repromotion(
    monkeypatch: pytest.MonkeyPatch,
):
    """A later sweep can mark landing complete only after proving the sealed data snapshot."""
    s3 = LostLandingMarkerResponseS3()
    spec, _ = _stage_and_publish_pinned_fixture(monkeypatch, s3)
    prefix = f"{spec.dataset_id}/{spec.version}"
    monkeypatch.setattr(Boto3S3, "default", staticmethod(lambda: s3))
    s3.arm_lost_marker_response("edullm-landing", f"{prefix}/_VALIDATED.json")
    copies_before_promotion = s3.copy_calls
    args = [
        "--landing-bucket",
        "edullm-landing",
        "--data-bucket",
        "edullm-data",
        "--prefix",
        prefix,
        "--promote",
    ]

    with pytest.raises(OSError, match="lost landing marker"):
        V.main(args)
    copies_after_promotion = s3.copy_calls
    assert copies_after_promotion > copies_before_promotion
    assert f"{prefix}/_VALIDATED.json" in s3.dump("edullm-data")
    assert f"{prefix}/_VALIDATED.json" not in s3.dump("edullm-landing")
    assert f"{prefix}/_REJECTED.json" not in s3.dump("edullm-landing")

    assert V.main(args) == 0
    assert s3.copy_calls == copies_after_promotion
    landing_marker = json.loads(
        s3.get("edullm-landing", f"{prefix}/_VALIDATED.json")
    )
    assert landing_marker["ok"] is True


def test_validator_cli_refuses_to_reconcile_a_seal_for_different_controls(
    monkeypatch: pytest.MonkeyPatch,
):
    """A data seal is not permission to mark a later mutable landing revision validated."""
    s3 = FakeS3()
    spec, _ = _stage_and_publish_pinned_fixture(monkeypatch, s3)
    prefix = f"{spec.dataset_id}/{spec.version}"
    first = V.validate_dataset(
        "edullm-landing", prefix, s3, data_bucket="edullm-data"
    )
    assert first.ok, [str(violation) for violation in first.violations]
    V.promote(first, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")

    # This remains a valid PRM800K landing artifact, but Gate A must retain the changed control
    # body and refuse to treat the prior data-bucket seal as a match for it.
    dataset_key = f"{prefix}/dataset.json"
    revised = json.loads(s3.get("edullm-landing", dataset_key))
    revised["build"]["executor"]["code_sha256"] = "e" * 64
    s3.seed("edullm-landing", dataset_key, canonical_json(revised))
    monkeypatch.setattr(Boto3S3, "default", staticmethod(lambda: s3))

    assert (
        V.main(
            [
                "--landing-bucket",
                "edullm-landing",
                "--data-bucket",
                "edullm-data",
                "--prefix",
                prefix,
                "--promote",
            ]
        )
        == 1
    )
    assert f"{prefix}/_VALIDATED.json" not in s3.dump("edullm-landing")
    rejection = json.loads(s3.get("edullm-landing", f"{prefix}/_REJECTED.json"))
    assert rejection["violations"][0]["code"] == "sealed-data-snapshot-mismatch"
    assert V.discover_pending("edullm-landing", s3) == []


def test_prm800k_namespace_rejects_a_spoofed_v2(
    monkeypatch: pytest.MonkeyPatch,
):
    s3 = FakeS3()
    spec, _ = _stage_and_publish_pinned_fixture(monkeypatch, s3)
    original_prefix = f"{spec.dataset_id}/{spec.version}"
    v2_prefix = f"{spec.dataset_id}/v2"
    for key, body in s3.dump("edullm-landing").items():
        if key.startswith(original_prefix + "/"):
            s3.seed("edullm-landing", v2_prefix + key[len(original_prefix) :], body)
    dataset_key = f"{v2_prefix}/dataset.json"
    dataset = json.loads(s3.get("edullm-landing", dataset_key))
    dataset["version"]["id"] = "v2"
    s3.seed("edullm-landing", dataset_key, canonical_json(dataset))

    result = V.validate_dataset(
        "edullm-landing", v2_prefix, s3, data_bucket="edullm-data"
    )
    assert "pinned-vendor-version-reserved" in {
        violation.code for violation in result.violations
    }


@pytest.mark.parametrize("commit_before_failure", [False, True])
def test_receipt_put_failure_is_either_recovered_or_left_as_an_isolated_partial_run(
    commit_before_failure: bool,
):
    bodies = _bodies()
    spec = _test_spec(bodies)
    s3 = ReceiptPutFailureS3(commit_before_failure=commit_before_failure)
    opener, _ = _opener(spec, bodies)

    if commit_before_failure:
        result = I.stage_prm800k(
            s3=s3,
            run_id="receipt-lost-response",
            retrieved_at=RETRIEVED,
            opener=opener,
            part_size=PART_SIZE,
            _spec=spec,
        )
        assert not result.reused
        assert I._receipt_key(spec, "receipt-lost-response") in s3.dump(
            "edullm-landing"
        )
    else:
        with pytest.raises(I.Prm800kIngestError, match="fresh run id"):
            I.stage_prm800k(
                s3=s3,
                run_id="receipt-not-stored",
                retrieved_at=RETRIEVED,
                opener=opener,
                part_size=PART_SIZE,
                _spec=spec,
            )
        assert s3.dump("edullm-landing") == {}


@pytest.mark.parametrize(
    ("marker", "document", "message"),
    [
        (
            "_REJECTED.json",
            {"violations": [{"code": "upstream-sha256-mismatch"}]},
            "terminally rejected.*upstream-sha256-mismatch",
        ),
        ("_VALIDATED.json", {"ok": True}, "already marked validated"),
    ],
)
def test_terminal_landing_marker_refuses_false_submit(
    marker: str, document: dict[str, Any], message: str
):
    bodies = _bodies()
    spec = _test_spec(bodies)
    s3 = FakeS3()
    run_id = "terminal-marker"
    opener, _ = _opener(spec, bodies)
    I.stage_prm800k(
        s3=s3,
        run_id=run_id,
        retrieved_at=RETRIEVED,
        opener=opener,
        part_size=PART_SIZE,
        _spec=spec,
    )
    s3.seed(
        "edullm-landing",
        f"{spec.dataset_id}/v1/{marker}",
        json.dumps(document).encode(),
    )
    with pytest.raises(I.Prm800kIngestError, match=message):
        I.publish_prm800k(s3=s3, run_id=run_id, created_at=CREATED, env=ENV, _spec=spec)


def test_stage_rejects_a_part_smaller_than_s3_accepts_before_opening_transport():
    with pytest.raises(I.Prm800kIngestError, match="part_size must be at least"):
        I.stage_prm800k(
            s3=FakeS3(),
            run_id="invalid-part-size",
            part_size=MIN_MULTIPART_PART_BYTES - 1,
            opener=lambda _: pytest.fail(
                "source must not be opened for an invalid part size"
            ),
            _spec=_test_spec(_bodies()),
        )


def test_cli_refuses_to_run_outside_batch(monkeypatch):
    monkeypatch.delenv("AWS_BATCH_JOB_ID", raising=False)
    assert I.main(["stage", "--run-id", "outside-batch"]) == 1


def test_publish_cli_reports_a_missing_receipt_without_a_traceback(monkeypatch, capsys):
    """An interrupted/misordered Batch retry should be an actionable CLI failure."""
    monkeypatch.setenv("AWS_BATCH_JOB_ID", "batch-test-job")
    monkeypatch.setattr(I.Boto3S3, "default", staticmethod(FakeS3))

    assert I.main(["publish", "--run-id", "missing-receipt"]) == 1

    captured = capsys.readouterr()
    assert "staging receipt is absent" in captured.err
    assert "Traceback" not in captured.err


def test_batch_publish_requires_digest_pinned_execution_provenance():
    bodies = _bodies()
    spec = _test_spec(bodies)
    s3 = FakeS3()
    opener, _ = _opener(spec, bodies)
    I.stage_prm800k(
        s3=s3,
        run_id="missing-batch-provenance",
        retrieved_at=RETRIEVED,
        opener=opener,
        part_size=PART_SIZE,
        _spec=spec,
    )

    with pytest.raises(I.Prm800kIngestError, match="EDULLM_BATCH_JOB_DEFINITION_ARN"):
        I.publish_prm800k(
            s3=s3,
            run_id="missing-batch-provenance",
            created_at=CREATED,
            env={"AWS_BATCH_JOB_ID": "batch-test-job"},
            _spec=spec,
        )
    assert f"{spec.dataset_id}/v1/dataset.json" not in s3.dump("edullm-landing")
