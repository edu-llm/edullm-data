"""Gate A orchestrator tests — one test per gate, each tripping exactly the failure it
targets. Every fixture is built in FakeS3 so the whole validator is exercised without AWS;
the airlock's IAM Deny is a live-verified AWS property, not something a unit test re-checks.
"""

from __future__ import annotations

import numpy as np
import pytest

from edullm_data import validate as V
from edullm_data.contracts import canonical_json, sha256_bytes
from edullm_data.manifest import (
    Format,
    ManifestEntry,
    build_manifest,
    manifest_sha256,
)
from edullm_data.s3 import FakeS3

LANDING = "edullm-landing"
DATA = "edullm-data"
DID = "pretrain/dolma2-150b"
VER = "v1"


def _tokens_body(n: int = 50000, *, dtype=np.uint32, vocab: int = 100000) -> bytes:
    return (np.arange(1, n + 1, dtype=dtype) % vocab).tobytes()


def _entry(path: str, body: bytes, *, dtype: str = "uint32", value: int | None = None) -> ManifestEntry:
    size_ = {"uint32": 4, "uint16": 2}[dtype]
    return ManifestEntry(
        path=path,
        sha256=sha256_bytes(body),
        bytes=len(body),
        count={"unit": "tokens", "value": value if value is not None else len(body) // size_},
        format=Format(container="raw", dtype=dtype, byte_order="little", header_bytes=0, codec="none"),
    )


def _tokenizer() -> dict:
    return {
        "repo_id": "allenai/dolma2-tokenizer",
        "revision": "main",
        "fingerprint_sha256": "c" * 64,
        "vocab_size": 100278,
        "eos_token_id": 100257,
    }


def _build(s3: FakeS3, *, entries=None, group_over=None, ds_over=None, dataset_id=DID, version=VER):
    """Seed a valid single-group pretrain-tokens dataset, return (dataset_dict, manifest)."""
    if entries is None:
        body = _tokens_body()
        entries = [_entry("tokens/train-00000.u32le.bin", body)]
        s3.seed(LANDING, f"{dataset_id}/{version}/tokens/train-00000.u32le.bin", body)
    man = build_manifest(entries, group_name="tokens")
    s3.seed(LANDING, f"{dataset_id}/{version}/tokens/manifest.json", canonical_json(man))
    group = {
        "name": "tokens",
        "profile": "pretrain-tokens/v1",
        "prefix": "tokens/",
        "manifest": "tokens/manifest.json",
        "manifest_sha256": manifest_sha256(man),
        "tokenizer": _tokenizer(),
    }
    if group_over:
        group.update(group_over)
    ds = {
        "schema_version": "edullm-dataset/v1",
        "dataset_id": dataset_id,
        "version": {"id": version, "relation": "supersedes", "of": None},
        "owner": "edullm-data@alphaaiengineering.com",
        "purpose": "150B-token Dolma2 mix for 370M ladder pretraining at 1.25xC",
        "mutability": "frozen",
        "inventory": {"objects": man["objects"], "bytes": man["bytes"]},
        "build": {
            "executor": {"kind": "external", "code_sha256": "a" * 64, "packages_lock_sha256": "b" * 64},
            "seed": 6198,
            "reproducibility": "logical",
        },
        "groups": [group],
    }
    if ds_over:
        ds.update(ds_over)
    s3.seed(LANDING, f"{dataset_id}/{version}/dataset.json", canonical_json(ds))
    return ds, man


def _codes(result) -> set[str]:
    return {v.code for v in result.violations}


# --------------------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------------------


def test_clean_dataset_passes_and_promotes():
    s3 = FakeS3()
    _build(s3)
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert r.ok, [str(v) for v in r.violations]
    V.promote(r, s3, data_bucket=DATA, landing_bucket=LANDING)
    data_keys = set(s3.dump(DATA))
    assert f"{DID}/{VER}/dataset.json" in data_keys
    assert f"{DID}/{VER}/tokens/train-00000.u32le.bin" in data_keys
    assert f"_catalog/{DID}/{VER}.json" in data_keys


def test_promote_refuses_failed_result():
    s3 = FakeS3()
    _build(s3, ds_over={"dataset_id": "pretrain/final-v2"})  # bad name → fails
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert not r.ok
    with pytest.raises(ValueError):
        V.promote(r, s3, data_bucket=DATA, landing_bucket=LANDING)


# --------------------------------------------------------------------------------------
# one test per gate
# --------------------------------------------------------------------------------------


def test_bad_dataset_id():
    s3 = FakeS3()
    # prefix stays valid but the id inside dataset.json is a banned name
    _build(s3, ds_over={"dataset_id": "pretrain/final-v2"})
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert "bad-dataset-id" in _codes(r)


def test_manifest_sha256_mismatch():
    s3 = FakeS3()
    _build(s3, group_over={"manifest_sha256": "0" * 64})
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert "manifest-sha256-mismatch" in _codes(r)


def test_missing_shard():
    s3 = FakeS3()
    body = _tokens_body()
    e = _entry("tokens/train-00000.u32le.bin", body)
    # manifest lists it but we never seed the object
    _build(s3, entries=[e])
    # remove the object the default helper would have seeded (helper only seeds when entries is None)
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert "missing-object" in _codes(r)


def test_extra_unlisted_shard():
    s3 = FakeS3()
    _build(s3)
    # drop an object into the group prefix that the manifest does not mention
    stray = _tokens_body(100)
    s3.seed(LANDING, f"{DID}/{VER}/tokens/train-99999.u32le.bin", stray)
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert "unlisted-object" in _codes(r)


def test_head_size_mismatch():
    s3 = FakeS3()
    _build(s3)
    # override the HEAD size so it disagrees with the manifest's declared bytes
    s3.override_head(LANDING, f"{DID}/{VER}/tokens/train-00000.u32le.bin", size=123)
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert "head-size-mismatch" in _codes(r)


def test_truncated_shard_arithmetic():
    s3 = FakeS3()
    body = _tokens_body()
    # claim one more token than the bytes support
    e = _entry("tokens/train-00000.u32le.bin", body, value=len(body) // 4 + 1)
    s3.seed(LANDING, f"{DID}/{VER}/tokens/train-00000.u32le.bin", body)
    _build(s3, entries=[e])
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert "count-arithmetic" in _codes(r)


def test_npy_lie():
    s3 = FakeS3()
    body = _tokens_body()
    # same headerless bytes, but named .npy while declaring header_bytes=0
    e = ManifestEntry(
        path="tokens/train-00000.npy",
        sha256=sha256_bytes(body),
        bytes=len(body),
        count={"unit": "tokens", "value": len(body) // 4},
        format=Format(container="raw", dtype="uint32", byte_order="little", header_bytes=0, codec="none"),
    )
    s3.seed(LANDING, f"{DID}/{VER}/tokens/train-00000.npy", body)
    _build(s3, entries=[e])
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert "extension-format-mismatch" in _codes(r)


def test_duplicate_shard_digest():
    s3 = FakeS3()
    body = _tokens_body()
    e0 = _entry("tokens/train-00000.u32le.bin", body)
    # second shard, different name, byte-identical → same sha256
    e1 = ManifestEntry(
        path="tokens/train-00001.u32le.bin",
        sha256=sha256_bytes(body),
        bytes=len(body),
        count={"unit": "tokens", "value": len(body) // 4},
        format=Format(container="raw", dtype="uint32", byte_order="little", header_bytes=0, codec="none"),
    )
    s3.seed(LANDING, f"{DID}/{VER}/tokens/train-00000.u32le.bin", body)
    s3.seed(LANDING, f"{DID}/{VER}/tokens/train-00001.u32le.bin", body)
    _build(s3, entries=[e0, e1])
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert "duplicate-shard-digest" in _codes(r)


def test_inventory_mismatch():
    s3 = FakeS3()
    _build(s3, ds_over={"inventory": {"objects": 99, "bytes": 1}})
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert "inventory-objects" in _codes(r) or "inventory-bytes" in _codes(r)


def test_shared_sha_with_parent():
    s3 = FakeS3()
    body = _tokens_body()
    sha = sha256_bytes(body)
    # publish a parent in the DATA bucket whose manifest contains this sha
    parent_prefix = "pretrain/datamix1/v1"
    pe = _entry("objects/x.u32le.bin", body)
    # force the parent entry to carry the same sha as our child shard
    pe = ManifestEntry(path="objects/x.u32le.bin", sha256=sha, bytes=len(body),
                       count={"unit": "tokens", "value": len(body) // 4},
                       format=Format(container="raw", dtype="uint32", byte_order="little", header_bytes=0, codec="none"))
    pman = build_manifest([pe], group_name="objects")
    s3.seed(DATA, f"{parent_prefix}/objects/manifest.json", canonical_json(pman))
    pds = {"groups": [{"name": "objects", "manifest": "objects/manifest.json"}]}
    s3.seed(DATA, f"{parent_prefix}/dataset.json", canonical_json(pds))
    # child re-materializes the same bytes and declares depends_on the parent
    e = _entry("tokens/train-00000.u32le.bin", body)
    s3.seed(LANDING, f"{DID}/{VER}/tokens/train-00000.u32le.bin", body)
    _build(s3, entries=[e], group_over={
        "depends_on": [{"dataset_id": "pretrain/datamix1", "version": "v1", "manifest_sha256": manifest_sha256(pman)}],
    })
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert "shared-sha-with-parent" in _codes(r)


def test_unknown_profile():
    s3 = FakeS3()
    _build(s3, group_over={"profile": "does-not-exist/v1"})
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert "unknown-profile" in _codes(r)


def test_frozen_incomplete_not_invalid():
    s3 = FakeS3()
    _build(s3)
    # delete the group manifest so the frozen dataset is unsealed
    del s3._store[(LANDING, f"{DID}/{VER}/tokens/manifest.json")]
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert r.incomplete
    assert not r.ok
    with pytest.raises(ValueError):
        V.promote(r, s3, data_bucket=DATA, landing_bucket=LANDING)


def test_prefix_mismatch():
    s3 = FakeS3()
    _build(s3)
    # validate against a prefix that doesn't match dataset_id/version
    # re-seed the same dataset under a wrong prefix
    ds, man = _build(s3, dataset_id="pretrain/dolma2-150b", version="v1")
    r = V.validate_dataset(LANDING, "pretrain/wrong-place/v1", s3, data_bucket=DATA)
    assert "no-dataset-json" in _codes(r) or "prefix-mismatch" in _codes(r)


# --------------------------------------------------------------------------------------
# self-discovery
# --------------------------------------------------------------------------------------


def test_discover_pending_skips_marked_and_incomplete():
    s3 = FakeS3()
    _build(s3)  # complete, unmarked → pending
    pending = V.discover_pending(LANDING, s3)
    assert f"{DID}/{VER}" in pending

    # mark it validated → no longer pending
    s3.seed(LANDING, f"{DID}/{VER}/_VALIDATED.json", b"{}")
    assert f"{DID}/{VER}" not in V.discover_pending(LANDING, s3)


def test_discover_skips_unsealed():
    s3 = FakeS3()
    _build(s3)
    del s3._store[(LANDING, f"{DID}/{VER}/tokens/manifest.json")]  # unsealed
    assert f"{DID}/{VER}" not in V.discover_pending(LANDING, s3)
