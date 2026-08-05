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
        # A train AND a val shard: the pretrain family requires held-out data
        # (families/pretrain.json validation_required=true), and declaring no partitions is
        # itself a violation now — declaring nothing used to disable the check entirely.
        vbody = body[: (len(body) // 3 // 4) * 4]
        entries = [
            _entry("tokens/train-00000.u32le.bin", body),
            _entry("tokens/val-00000.u32le.bin", vbody),
        ]
        s3.seed(LANDING, f"{dataset_id}/{version}/tokens/train-00000.u32le.bin", body)
        s3.seed(LANDING, f"{dataset_id}/{version}/tokens/val-00000.u32le.bin", vbody)
    man = build_manifest(entries, group_name="tokens")
    s3.seed(LANDING, f"{dataset_id}/{version}/tokens/manifest.json", canonical_json(man))
    group = {
        "name": "tokens",
        "profile": "pretrain-tokens/v1",
        "prefix": "tokens/",
        "manifest": "tokens/manifest.json",
        "manifest_sha256": manifest_sha256(man),
        "tokenizer": _tokenizer(),
        "coverage": "partition",
        "partitions": [
            {"name": "train", "by": "path", "glob": "train-*.u32le.bin",
             "rows": len(_tokens_body()) // 4},
            {"name": "val", "by": "path", "glob": "val-*.u32le.bin",
             "rows": ((len(_tokens_body()) // 3 // 4) * 4) // 4},
        ],
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


def test_promote_writes_generated_readme():
    """promote() emits a README.md into the data bucket, generated from dataset.json (§3). It
    renders the real dataset id + purpose, so it is documentation of THIS dataset, not a stub."""
    s3 = FakeS3()
    _build(s3, ds_over={"about": "A tiny Dolma2 slice.",
                        "sources": [{"name": "Dolma2", "tokens": 150000000000, "license": "ODC-By-1.0"}]})
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert r.ok, [str(v) for v in r.violations]
    V.promote(r, s3, data_bucket=DATA, landing_bucket=LANDING)
    body = s3._store.get((DATA, f"{DID}/{VER}/README.md"))
    assert body is not None
    text = body.decode("utf-8")
    assert text.startswith(f"# {DID} — {VER}")
    assert "## About" in text and "A tiny Dolma2 slice." in text
    assert "## Data mix / sources" in text and "Dolma2" in text
    assert "## How to read it" in text


def test_readme_is_control_not_flagged_on_revalidate():
    """The README is a CONTROL file, never a manifest entry — so re-running Gate A over the
    PROMOTED prefix (which now contains README.md) must not flag it as an unlisted/extra object.
    This is what lets the golden-rule 'recompute in place against edullm-data' check pass after a
    promotion writes the README."""
    s3 = FakeS3()
    _build(s3)
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert r.ok, [str(v) for v in r.violations]
    V.promote(r, s3, data_bucket=DATA, landing_bucket=LANDING)
    assert s3._store.get((DATA, f"{DID}/{VER}/README.md")) is not None
    # re-validate the promoted copy in the DATA bucket (both landing_bucket and data_bucket = DATA)
    r2 = V.validate_dataset(DATA, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert r2.ok, [str(v) for v in r2.violations]
    assert "unlisted-object" not in _codes(r2)


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


# --------------------------------------------------------------------------------------
# sidecar control files (_dedup/, _licenses/) — the PASSING and FAILING pair
# --------------------------------------------------------------------------------------


def test_sidecar_prefixes_are_control_files_at_any_depth():
    """PASSING fixture. The two sidecars the design depends on — `_dedup/clusters.parquet`
    (MinHash cluster IDs) and `_licenses/licenses.parquet` (per-source license strings) — are
    control files, so a dataset carrying them passes Gate A unchanged. Both were rejected as
    `unlisted-object-dataset-level` before.

    Depth is checked too, because a PREFIX that only matched depth 1 would be a basename rule
    wearing a prefix's clothes: adding a partitioned `_dedup/2026/part-00000.parquet` later must
    not need another code change.
    """
    s3 = FakeS3()
    _build(s3)
    s3.seed(LANDING, f"{DID}/{VER}/_dedup/clusters.parquet", b"PAR1-cluster-table")
    s3.seed(LANDING, f"{DID}/{VER}/_dedup/2026/part-00000.parquet", b"PAR1-nested")
    s3.seed(LANDING, f"{DID}/{VER}/_licenses/licenses.parquet", b"PAR1-licenses")
    s3.seed(LANDING, f"{DID}/{VER}/_licenses/spdx/map.json", b"{}")
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert r.ok, [str(v) for v in r.violations]
    assert not {c for c in _codes(r) if c.startswith("unlisted-object")}


def test_sidecar_allowlist_does_not_disable_the_exhaustiveness_sweep():
    """FAILING fixture, and the one that matters. An allowlist too broad silently switches off
    the check it lives in, so each of these must STILL be rejected:

    * a payload-shaped object under a group prefix that no manifest lists,
    * an object under a top-level prefix belonging to no declared group,
    * a near-miss on the sidecar names — `_dedup` / `_licenses` WITHOUT the trailing slash are
      not the control prefixes, and a bare `_licenses.parquet` at depth 0 is not one either
      (`_licenses/` is; the prefix form is the sanctioned shape).
    """
    s3 = FakeS3()
    _build(s3)
    s3.seed(LANDING, f"{DID}/{VER}/tokens/random.parquet", b"PAR1-not-in-any-manifest")
    s3.seed(LANDING, f"{DID}/{VER}/sneaky/val-00000.u32le.bin", _tokens_body(100))
    s3.seed(LANDING, f"{DID}/{VER}/_dedupe-notes/clusters.parquet", b"PAR1-near-miss")
    s3.seed(LANDING, f"{DID}/{VER}/_licenses.parquet", b"PAR1-depth-zero")
    r = V.validate_dataset(LANDING, f"{DID}/{VER}", s3, data_bucket=DATA)
    assert not r.ok
    flagged = {v.path for v in r.violations if v.code.startswith("unlisted-object")}
    assert "tokens/random.parquet" in flagged
    assert "sneaky/val-00000.u32le.bin" in flagged
    assert "_dedupe-notes/clusters.parquet" in flagged
    assert "_licenses.parquet" in flagged


def test_control_prefixes_are_all_unambiguously_not_group_names():
    """A control prefix that could also be a group name would exempt real payload from the
    sweep. Group names are the first path segment and are kebab-case `[a-z0-9-]`, so a leading
    underscore is what makes a prefix unmistakable. `dependents/` is the grandfathered
    exception; every other entry must lead with `_`, and none may be `_catalog/`-adjacent
    enough to collide with the version resolver's namespace.
    """
    from edullm_data.contracts import CONTROL_PREFIXES

    for cp in CONTROL_PREFIXES:
        assert cp.endswith("/"), f"{cp!r} must end with / or it prefix-matches a sibling name"
        assert cp.startswith("_") or cp == "dependents/", f"{cp!r} could be mistaken for a group"
    assert "_dedup/" in CONTROL_PREFIXES
    assert "_licenses/" in CONTROL_PREFIXES


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


# --------------------------------------------------------------------------------------
# Threaded HEAD prefetch: faster, and provably the same verdict
# --------------------------------------------------------------------------------------


class _InvertedLatencyS3:
    """A fake whose HEADs complete in REVERSE submission order.

    This is the point of the fixture. On a plain in-memory fake, an as-completed implementation
    usually passes an order assertion by luck, because everything finishes instantly in submission
    order anyway. Here later entries finish FIRST by construction, so any implementation that
    collects results as they arrive rather than by submission order fails visibly.
    """

    def __init__(self, n, missing=()):
        import threading

        self.n = n
        self.missing = set(missing)
        self.completion = []
        self.peak = 0
        self._cur = 0
        self._lock = threading.Lock()

    def head(self, bucket, key):
        import time

        from edullm_data.s3 import NotFound

        with self._lock:
            self._cur += 1
            self.peak = max(self.peak, self._cur)
        idx = int(key.rsplit("-", 1)[-1])
        time.sleep(0.01 * (self.n - idx))
        with self._lock:
            self._cur -= 1
            self.completion.append(idx)
        if idx in self.missing:
            raise NotFound(key)
        return {"size": 100 + idx}


class _Entry:
    def __init__(self, path):
        self.path = path


def _entries(n):
    return [_Entry(f"tokens/train-{i:05d}") for i in range(n)]


def test_prefetched_heads_keep_manifest_order_at_every_worker_count():
    """Gate A's `duplicate-shard-digest` fires on the SECOND occurrence of a digest, so which of
    two identical shards gets named is decided by iteration order. The prefetch must therefore
    return results in manifest order, not completion order."""
    from edullm_data.validate import _prefetch_heads

    ents = _entries(10)
    baseline = None
    for workers in (1, 4, 16):
        fake = _InvertedLatencyS3(10, missing={3, 7})
        out = _prefetch_heads(fake, "bkt", "pfx", ents, workers)
        assert list(out.keys()) == [e.path for e in ents], workers
        got = [(p, None if h is None else h["size"]) for p, h in out.items()]
        if baseline is None:
            baseline = got
        else:
            assert got == baseline, f"workers={workers} diverged from sequential"
        if workers > 1:
            assert fake.peak > 1, "the pool never overlapped; this test proved nothing"
            # Completion order must differ from submission order — that is what makes the
            # manifest-order assertion above meaningful. (Not a strict reversal: once the pool is
            # as wide as the work, every HEAD starts at once and the sleeps interleave.)
            assert fake.completion != sorted(fake.completion), (
                f"workers={workers} completed in submission order; the fixture proved nothing"
            )


def test_a_missing_object_is_reported_for_its_own_entry():
    """`None` must mean "absent" for exactly the entry that is absent — the caller turns that into
    `missing-object`, and a shifted mapping would blame the wrong shard."""
    from edullm_data.validate import _prefetch_heads

    ents = _entries(8)

    class _Missing:
        """Deterministic: no sleeps, so this cannot flake under a loaded test run.

        The earlier version of this test used the latency fixture and failed intermittently in the
        full suite while passing alone — a flaky test guarding a correctness property is worse than
        no test, because the next person learns to re-run it.
        """

        def head(self, bucket, key):
            from edullm_data.s3 import NotFound

            idx = int(key.rsplit("-", 1)[-1])
            if idx in (0, 5):
                raise NotFound(key)
            return {"size": 100 + idx}

    for workers in (1, 8):
        out = _prefetch_heads(_Missing(), "b", "p", ents, workers)
        absent = [i for i, e in enumerate(ents) if out[e.path] is None]
        assert absent == [0, 5], (workers, absent)
        for i, e in enumerate(ents):
            if i not in (0, 5):
                assert out[e.path]["size"] == 100 + i, (workers, i)


def test_a_duplicated_path_costs_one_head():
    """Two manifest rows for one object describe ONE object. Gate A reports the duplication
    separately; the prefetch must not double the most expensive call here."""
    from edullm_data.validate import _prefetch_heads

    class Counting:
        def __init__(self):
            self.calls = 0

        def head(self, bucket, key):
            self.calls += 1
            return {"size": 1}

    ents = [_Entry("a"), _Entry("a"), _Entry("b")]
    for workers in (1, 4):
        c = Counting()
        out = _prefetch_heads(c, "b", "p", ents, workers)
        assert c.calls == 2, (workers, c.calls)
        assert list(out.keys()) == ["a", "b"]


def test_the_default_is_one_worker_and_stays_sequential():
    """A previously written `_VALIDATED.json` stands on the sequential path, so the default must
    remain it — same calls, same order, no pool."""
    import inspect

    from edullm_data.validate import validate_dataset

    assert inspect.signature(validate_dataset).parameters["head_workers"].default == 1

    from edullm_data.validate import _prefetch_heads

    fake = _InvertedLatencyS3(6)
    _prefetch_heads(fake, "b", "p", _entries(6), 1)
    assert fake.peak == 1, "head_workers=1 used concurrency"
    assert fake.completion == sorted(fake.completion), "sequential run completed out of order"

    # And prove it never even CONSTRUCTS a pool at 1. peak==1 alone is not enough: a
    # ThreadPoolExecutor(max_workers=1) also yields peak 1 while running on a worker thread, and
    # then "the default is the original path" would be false in a way no assertion above catches.
    # (Found by mutation: replacing the `head_workers <= 1` guard with `if False:` passed every
    # other test in this file.)
    import threading

    seen_threads = set()

    class _RecordThread:
        def head(self, bucket, key):
            seen_threads.add(threading.current_thread().name)
            return {"size": 1}

    _prefetch_heads(_RecordThread(), "b", "p", _entries(4), 1)
    assert seen_threads == {threading.current_thread().name}, (
        f"head_workers=1 ran off the calling thread: {seen_threads}"
    )


def test_the_cli_sizes_the_http_pool_to_the_worker_count():
    """`--head-workers 16` against botocore's default 10-connection pool would silently cap itself.

    botocore does not pass `block=True` to urllib3, so exceeding the pool neither raises nor waits --
    urllib3 discards the surplus connection and logs "Connection pool is full", and the 11th..Nth
    worker pays a fresh TLS handshake per object. The speedup the operator asked for just does not
    happen, with no error anywhere. Verified against botocore rather than assumed: the default really
    is 10.
    """
    import boto3
    from botocore.config import Config

    assert boto3.client("s3", region_name="us-east-1").meta.config.max_pool_connections == 10
    sized = boto3.client(
        "s3", region_name="us-east-1", config=Config(max_pool_connections=18)
    )
    assert sized.meta.config.max_pool_connections == 18

    # And the CLI wires it: the source must size the pool off the worker counts, not hardcode it.
    import inspect

    from edullm_data import validate as V

    src = inspect.getsource(V.main)
    assert "max_pool_connections" in src, "main() does not size the HTTP pool"
    assert "max(args.head_workers, args.promote_workers)" in src, (
        "the pool is not sized from the worker counts"
    )
