"""Every hole two adversarial reviews found, pinned so it cannot reopen.

Both reviews attacked the same seam from different angles and converged on one root cause: the
validator and the reader BOTH reasoned from *declared partition names*, and both treated
"nothing declared" as "nothing to check". So the two halves of the defence failed together
rather than covering for each other. Declaring no partitions turned off the validation
requirement, turned off the undeclared-split backstop, and made the reader hand back everything.

The worst case needed no adversarial input at all: ``families/curriculum.json`` ships
``validation_required: true`` with no partition template, so an ordinary publish declared no
splits and leaked its val shards as trainable while ``.val`` reported ``None``.

The fix is the standard's own rule applied to the read path too: RECOMPUTE from the bytes' own
names, never trust the declaration.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from edullm_data import publish as P
from edullm_data import read as R
from edullm_data import validate as V
from edullm_data.contracts import canonical_json, sha256_bytes
from edullm_data.manifest import ManifestEntry
from edullm_data.s3 import FakeS3
from edullm_data.validate import _validate_partitions

CREATED = "2026-07-29T12:00:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}
DSID = "pretrain/adversarial-fixture-10b"
TOK = {
    "repo_id": "allenai/dolma2-tokenizer", "revision": "abc123",
    "fingerprint_sha256": "c" * 64, "vocab_size": 100278, "eos_token_id": 100257,
}
FMT = {"container": "raw", "dtype": "uint32", "byte_order": "little", "header_bytes": 0, "codec": "none"}


def _publish(s3: FakeS3, *, group_meta=None, with_val=True):
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    ids = (np.arange(1, 60001) % 40000).astype(np.uint32) + 1
    (d / "tokens" / "train-00000.u32le.bin").write_bytes(ids.tobytes())
    if with_val:
        (d / "tokens" / "val-00000.u32le.bin").write_bytes(ids[:20000].tobytes())
    meta = {"tokenizer": TOK}
    meta.update(group_meta or {})
    plan = P.publish(
        d, dataset_id=DSID,
        purpose="fixture pinning the adversarial holes both reviews found in the split defence",
        profile="pretrain-tokens/v1", s3=s3, created_at=CREATED,
        group_meta={"tokens": meta}, env=ENV,
    )
    return plan.version, V.validate_dataset(
        "edullm-landing", f"{DSID}/{plan.version}", s3, data_bucket="edullm-data"
    )


def _force_promote_with_partitions(s3: FakeS3, parts):
    """Publish, then rewrite the PUBLISHED partitions and re-seal.

    Deliberately bypasses Gate A so the READER is what is under test — the point is that the
    reader must not depend on the validator having caught the shape.
    """
    ver, res = _publish(s3)
    res.violations = []
    V.promote(res, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")
    prefix = f"{DSID}/{ver}"
    key = f"{prefix}/dataset.json"
    ds = json.loads(s3.get("edullm-data", key).decode())
    ds["groups"][0]["partitions"] = parts
    s3.put("edullm-data", key, canonical_json(ds))
    seal = json.loads(s3.get("edullm-data", f"{prefix}/_VALIDATED.json").decode())
    seal["dataset_sha256"] = sha256_bytes(s3.get("edullm-data", key))
    s3.put("edullm-data", f"{prefix}/_VALIDATED.json", canonical_json(seal))
    return ver


# ======================================================================================
# The reader must never hand back held-out data, whatever dataset.json claims.
# ======================================================================================

@pytest.mark.parametrize(
    "parts,label",
    [
        ([{"name": "val", "by": "path", "glob": "val-*.u32le.bin", "rows": 20000}], "val-only partition"),
        ([], "empty partitions list"),
        ([{"by": "path", "glob": "*.u32le.bin", "rows": 80000}], "partition with no name"),
        ([{"name": "", "by": "path", "glob": "*.u32le.bin", "rows": 80000}], "empty-string name"),
        ([{"name": "train", "by": "path", "glob": "*.u32le.bin", "rows": 80000}], "train glob matches everything"),
        (
            [{"name": "train", "by": "field", "field": "x", "rows": 60000},
             {"name": "val", "by": "field", "field": "y", "rows": 20000}],
            "both by:field (each selects every shard)",
        ),
        ([{"name": "test", "by": "path", "glob": "*.u32le.bin", "rows": 80000}], "test-only partition"),
    ],
)
def test_no_declared_shape_can_make_an_unsplit_read_return_held_out_data(parts, label):
    """Seven shapes, each of which used to leak. The filename is the authority, not the claim."""
    s3 = FakeS3()
    ver = _force_promote_with_partitions(s3, parts)
    res = R.dataset_paths(DSID, ver, s3=s3)
    leaked = [p for p in res.paths if "val-" in p]
    assert leaked == [], f"{label}: leaked {leaked}"


def test_include_held_out_still_returns_everything():
    """The escape hatch must remain usable, or callers will reach for require_validated=False."""
    s3 = FakeS3()
    ver = _force_promote_with_partitions(
        s3, [{"name": "train", "by": "path", "glob": "train-*.u32le.bin", "rows": 60000},
             {"name": "val", "by": "path", "glob": "val-*.u32le.bin", "rows": 20000}]
    )
    res = R.dataset_paths(DSID, ver, s3=s3, include_held_out=True)
    assert any("val-" in p for p in res.paths)


def test_a_dataset_with_no_shard_names_still_returns_its_payload():
    """The filename filter must not empty out a tokenizer or vendored tree."""
    s3 = FakeS3()
    d = Path(tempfile.mkdtemp())
    (d / "files").mkdir()
    doc = {"model": {"type": "BPE", "vocab": {str(i): i for i in range(100256)}},
           "added_tokens": [{"id": 100257, "content": "<|endoftext|>", "special": True}]}
    (d / "files" / "tokenizer.json").write_bytes(json.dumps(doc).encode())
    plan = P.publish(
        d, dataset_id="tokenizer/dolma2-bpe",
        purpose="Published Dolma2 tokenizer so corpora own the tokenizer they were produced with",
        profile="tokenizer/v1", s3=s3, created_at=CREATED, env=ENV,
    )
    res = V.validate_dataset("edullm-landing", f"tokenizer/dolma2-bpe/{plan.version}", s3, data_bucket="edullm-data")
    assert res.ok, [str(v) for v in res.violations]
    V.promote(res, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")
    r = R.dataset_paths("tokenizer/dolma2-bpe", plan.version, s3=s3)
    assert r.paths, "a dataset whose files are not shards must still be readable"


# ======================================================================================
# Declaring nothing is not an exemption.
# ======================================================================================

def test_declaring_an_empty_partitions_list_is_a_violation_not_a_bypass():
    """`partitions: []` used to disable the validation requirement AND the split sweep."""
    _, res = _publish(FakeS3(), group_meta={"partitions": [], "coverage": "incomplete"})
    assert not res.ok
    assert "missing-required-split" in {v.code for v in res.violations}


def test_omitting_partitions_inherits_the_family_template_and_is_safe():
    """Distinct from the above, and worth pinning: supplying NO partitions is not the hole.

    publish() fills in the family's train+val template, so an ordinary pretrain publish is
    already covered. Only an explicit empty list (or a nameless partition) reaches the gap.
    """
    ver, res = _publish(FakeS3(), group_meta={})
    assert res.ok, [str(v) for v in res.violations]


def test_the_curriculum_family_declares_a_position_on_validation():
    """It required validation while shipping no partition template — the no-input leak."""
    from edullm_data.validate import _family_defaults_for

    fd = _family_defaults_for("curriculum/easy-first-10b")
    assert "validation_required" in fd


# ======================================================================================
# Train/held-out leakage is an error under EVERY coverage mode.
# ======================================================================================

@pytest.mark.parametrize("coverage", ["partition", "overlapping", "incomplete"])
def test_train_heldout_overlap_is_rejected_under_every_coverage_mode(coverage):
    """`overlapping` waives replay between trainable partitions, never train-vs-held-out."""
    entries = [
        ManifestEntry.from_dict({"path": "tokens/train-00000.u32le.bin", "sha256": "a" * 64,
                                 "bytes": 240000, "count": {"unit": "tokens", "value": 60000}, "format": FMT}),
        ManifestEntry.from_dict({"path": "tokens/val-00000.u32le.bin", "sha256": "b" * 64,
                                 "bytes": 80000, "count": {"unit": "tokens", "value": 20000}, "format": FMT}),
    ]
    v: list = []
    _validate_partitions(
        {"coverage": coverage, "partitions": [
            {"name": "train", "by": "path", "glob": "*.u32le.bin", "rows": 80000},
            {"name": "val", "by": "path", "glob": "*.u32le.bin", "rows": 80000},
        ]}, v, "tokens", {e.path for e in entries}, entries,
    )
    assert "train-heldout-leakage" in {x.code for x in v}


def test_replay_between_trainable_partitions_is_still_allowed():
    entries = [ManifestEntry.from_dict({"path": "tokens/train-00000.u32le.bin", "sha256": "a" * 64,
                                        "bytes": 240000, "count": {"unit": "tokens", "value": 60000},
                                        "format": FMT})]
    v: list = []
    _validate_partitions(
        {"coverage": "overlapping", "partitions": [
            {"name": "train", "by": "path", "glob": "*.u32le.bin", "rows": 60000},
        ]}, v, "tokens", {e.path for e in entries}, entries,
    )
    assert "train-heldout-leakage" not in {x.code for x in v}


# ======================================================================================
# Exemptions the standard grants must not be revoked by a new check.
# ======================================================================================

def test_a_vendored_tree_keeps_its_upstream_shard_names():
    """families/vendor.json exempts vendored trees from naming BECAUSE renaming destroys the
    byte-for-byte correspondence that makes a mirror verifiable. The split sweep was revoking it,
    rejecting an upstream `test-00000.parquet` for keeping its own name."""
    s3 = FakeS3()
    d = Path(tempfile.mkdtemp())
    (d / "mirror").mkdir()
    (d / "mirror" / "test-00000.parquet").write_bytes(b"PAR1" + b"\x00" * 400)
    plan = P.publish(
        d, dataset_id="vendor/dclm-hero-fasttext",
        purpose="Mirror of an upstream release kept byte-for-byte so its provenance stays verifiable",
        profile="vendor/v1", s3=s3, created_at=CREATED, env=ENV,
    )
    res = V.validate_dataset(
        "edullm-landing", f"vendor/dclm-hero-fasttext/{plan.version}", s3, data_bucket="edullm-data"
    )
    assert "undeclared-split" not in {v.code for v in res.violations}


# ======================================================================================
# dtype aliases, bad rows, re-promotion.
# ======================================================================================

@pytest.mark.parametrize(
    "dtype,container,expected",
    [
        ("u2", "raw", "dtype-not-checkable"),          # a numpy alias the size map does not know
        ("<u2", "raw", "dtype-not-checkable"),
        ("uint16", "memmap", "fixed-width-dtype-in-nonraw-container"),
        ("uint16", "raw ", "fixed-width-dtype-in-nonraw-container"),  # trailing space
    ],
)
def test_a_dtype_the_gate_cannot_size_is_a_violation_not_a_skip(dtype, container, expected):
    """Each of these made BOTH this check and verify_arithmetic skip, so a uint32 corpus could
    claim half-width and ship a 2x-inflated token count."""
    from edullm_data.validate import _check_dtype_width_vs_vocab

    entry = ManifestEntry.from_dict({
        "path": "tokens/train-00000.bin", "sha256": "a" * 64, "bytes": 240000,
        "count": {"unit": "tokens", "value": 120000},
        "format": {"container": container, "dtype": dtype, "byte_order": "little",
                   "header_bytes": 0, "codec": "none"},
    })
    v: list = []
    _check_dtype_width_vs_vocab([entry], {"vocab_size": 100278}, v, "tokens")
    assert expected in {x.code for x in v}


@pytest.mark.parametrize("rows", [None, "a lot", -5, True, 1.5])
def test_a_rows_value_that_is_not_a_count_is_rejected(rows):
    """`rows: null` satisfied the presence check and then skipped the value check."""
    entries = [ManifestEntry.from_dict({"path": "tokens/train-00000.u32le.bin", "sha256": "a" * 64,
                                        "bytes": 240000, "count": {"unit": "tokens", "value": 60000},
                                        "format": FMT})]
    v: list = []
    _validate_partitions(
        {"coverage": "incomplete", "partitions": [
            {"name": "train", "by": "path", "glob": "train-*.u32le.bin", "rows": rows},
        ]}, v, "tokens", {e.path for e in entries}, entries,
    )
    assert {"partition-bad-rows", "partition-rows-mismatch"} & {x.code for x in v}


def test_a_group_override_cannot_loosen_a_family_bound():
    """An all-zeros corpus published clean by declaring min_distinct_ids=1 — re-enabling by hand
    the exact failure the family bounds exist to forbid."""
    s3 = FakeS3()
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    zeros = np.zeros(60000, dtype=np.uint32)
    (d / "tokens" / "train-00000.u32le.bin").write_bytes(zeros.tobytes())
    (d / "tokens" / "val-00000.u32le.bin").write_bytes(zeros[:20000].tobytes())
    plan = P.publish(
        d, dataset_id="pretrain/allzeros-fixture-10b",
        purpose="all-zeros corpus probing whether a group override can loosen a family bound",
        profile="pretrain-tokens/v1", s3=s3, created_at=CREATED,
        group_meta={"tokens": {"tokenizer": TOK, "min_distinct_ids": 1, "max_zero_fraction": 1.0}},
        env=ENV,
    )
    res = V.validate_dataset(
        "edullm-landing", f"pretrain/allzeros-fixture-10b/{plan.version}", s3, data_bucket="edullm-data"
    )
    assert not res.ok
    assert {"distinct-too-few", "zero-fraction-out-of-bounds"} & {v.code for v in res.violations}


def test_re_promoting_a_sealed_prefix_is_refused():
    """An overwrite needs no Delete call, so no bucket policy would stop it. "Frozen means
    frozen" has to be enforced by the publisher too."""
    s3 = FakeS3()
    ver, res = _publish(s3)
    assert res.ok, [str(v) for v in res.violations]
    V.promote(res, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")
    with pytest.raises(ValueError, match="already sealed"):
        V.promote(res, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")


def test_a_tampered_dataset_json_is_refused_on_the_read_path():
    """Rooting the hash chain buys nothing if no read path verifies it. The tampering that
    matters most: swap the train and val globs — the marker is present, the manifests are
    intact, and `split="train"` returns the val shards."""
    s3 = FakeS3()
    ver, res = _publish(s3)
    assert res.ok, [str(v) for v in res.violations]
    V.promote(res, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")
    key = f"{DSID}/{ver}/dataset.json"
    ds = json.loads(s3.get("edullm-data", key).decode())
    for part in ds["groups"][0]["partitions"]:
        part["glob"] = "val-*.u32le.bin" if part["name"] == "train" else "train-*.u32le.bin"
    s3.put("edullm-data", key, canonical_json(ds))
    with pytest.raises(R.SealMismatch):
        R.dataset_paths(DSID, ver, split="train", s3=s3)


def test_a_control_basename_in_a_subdirectory_does_not_hide_an_object():
    """`_is_control_key` matched a basename anywhere in the tree, so `sneaky/README.md` was
    exempt from the exhaustiveness sweep."""
    from edullm_data.validate import _is_control_key

    assert _is_control_key("README.md")
    assert _is_control_key("tokens/manifest.json")
    assert not _is_control_key("sneaky/README.md")
    assert not _is_control_key("sneaky/dataset.json")
