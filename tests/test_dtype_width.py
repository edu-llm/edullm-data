"""dtype width must be DERIVED from the tokenizer's vocab, never trusted from the manifest.

This is the check that closes the one dtype lie every other gate misses. The others cannot:

* ``verify_arithmetic`` is tautological on any manifest ``publish()`` built — ``publish.py``
  computes ``count = size // dtype_size`` and ``manifest.py`` then asserts
  ``count * dtype_size == bytes``, which reduces to ``size % dtype_size == 0``. Both uint16
  and uint32 satisfy it for the same bytes.
* the extension check is *self-consistent* with the lie (``.u16le.bin`` + ``dtype: uint16``
  agree with each other).
* the ``\\x93NUMPY`` sniff sees no header either way.
* the decode smoke test only ever sees ids that are in range — a uint32 stream read as uint16
  yields small numbers, all comfortably under vocab_size.

Narrowing is also the *dangerous* direction: it INFLATES the declared token count (2x for
uint32-read-as-uint16), which silently changes the training budget rather than crashing.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from edullm_data import publish as P
from edullm_data import validate as V
from edullm_data.manifest import ManifestEntry
from edullm_data.s3 import FakeS3
from edullm_data.validate import _check_dtype_width_vs_vocab

CREATED = "2026-07-29T12:00:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}
DOLMA2_VOCAB = 100278  # needs 4 bytes; 2 bytes tops out at 65535


def _tokenizer_meta(vocab_size: int = DOLMA2_VOCAB) -> dict:
    return {
        "tokenizer": {
            "repo_id": "allenai/dolma2-tokenizer",
            "revision": "abc123",
            "fingerprint_sha256": "c" * 64,
            "vocab_size": vocab_size,
            "eos_token_id": 100257,
        }
    }


def _publish(s3: FakeS3, *, ext: str, dtype: np.dtype, vocab_size: int = DOLMA2_VOCAB):
    """Publish a one-shard corpus whose shards really are ``dtype``, named honestly."""
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    # Ids must be in range for the declared dtype AND under vocab_size, so the decode smoke
    # test has no independent reason to complain — the width check must be the only objection.
    ceiling = min(int(vocab_size) - 1, int(np.iinfo(dtype).max))
    ids = (np.arange(1, 60001) % ceiling).astype(dtype) + 1
    (d / "tokens" / f"train-00000.{ext}.bin").write_bytes(ids.tobytes())
    plan = P.publish(
        d,
        dataset_id="pretrain/width-fixture",
        purpose="fixture corpus for the dtype-width-vs-vocab check",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at=CREATED,
        group_meta={"tokens": _tokenizer_meta(vocab_size)},
        env=ENV,
    )
    return V.validate_dataset(
        "edullm-landing", f"pretrain/width-fixture/{plan.version}", s3, data_bucket="edullm-data"
    )


# ---- PASSING fixture: uint32 shards, dolma2 vocab -> 4 bytes is exactly right ----

def test_uint32_shards_with_dolma2_vocab_pass():
    r = _publish(FakeS3(), ext="u32le", dtype=np.uint32)
    narrow = [v for v in r.violations if v.code == "dtype-too-narrow-for-vocab"]
    assert narrow == [], [str(v) for v in narrow]


def test_a_wider_dtype_than_needed_is_legal():
    """One-sided on purpose: too-wide wastes space but reads correctly, so it is not a lie."""
    s3 = FakeS3()
    r = _publish(s3, ext="u32le", dtype=np.uint32, vocab_size=300)  # 300 needs only 2 bytes
    narrow = [v for v in r.violations if v.code == "dtype-too-narrow-for-vocab"]
    assert narrow == [], [str(v) for v in narrow]


# ---- FAILING fixture: uint16 declared against a vocab that cannot fit in 16 bits ----

def test_uint16_shards_with_dolma2_vocab_are_rejected():
    r = _publish(FakeS3(), ext="u16le", dtype=np.uint16)
    codes = [v.code for v in r.violations]
    assert "dtype-too-narrow-for-vocab" in codes, codes
    assert not r.ok


def test_the_narrowing_lie_passes_every_other_check():
    """The point of the new check: nothing else in Gate A objects to this dataset.

    If this test ever fails because another check started catching it too, that is good news
    — but it should be a deliberate change, not a silent one.
    """
    r = _publish(FakeS3(), ext="u16le", dtype=np.uint16)
    other = sorted({v.code for v in r.violations} - {"dtype-too-narrow-for-vocab"})
    assert other == [], f"expected the width check to be the ONLY objection, also got: {other}"


def test_the_violation_explains_the_inflated_count():
    """A reviewer must be able to tell what went wrong without reading the validator."""
    r = _publish(FakeS3(), ext="u16le", dtype=np.uint16)
    msg = next(str(v) for v in r.violations if v.code == "dtype-too-narrow-for-vocab")
    assert "100278" in msg
    assert "2 bytes" in msg and "4 bytes" in msg
    assert "inflated" in msg


# ======================================================================================
# Scoping: the check is about TOKEN arrays, not every fixed-width payload.
# ======================================================================================
#
# BUG-2 from the Phase 1 review. The check filtered only on `container in
# FIXED_WIDTH_CONTAINERS` ({"raw"}) and a truthy dtype_size, so ANY raw fixed-width entry in a
# group carrying a tokenizer dependency got a *vocab* bound applied to it — including float16
# activations and uint8 blobs, which have nothing to do with token ids. `verify_arithmetic`
# already scopes itself with FIXED_WIDTH_UNITS; this now mirrors it.

_DOLMA2 = {"vocab_size": DOLMA2_VOCAB}


def _check_one(path: str, dtype: str, size: int, unit: str | None, value: int = 1000):
    entry = ManifestEntry.from_dict({
        "path": path, "sha256": "a" * 64, "bytes": value * size,
        "count": {"unit": unit, "value": value} if unit else None,
        "format": {"container": "raw", "dtype": dtype, "byte_order": "little",
                   "header_bytes": 0, "codec": "none"},
    })
    v: list = []
    _check_dtype_width_vs_vocab([entry], _DOLMA2, v, "g")
    return [x.code for x in v]


@pytest.mark.parametrize(
    "path,dtype,size,unit",
    [
        ("probe/act-00000.f16le.bin", "float16", 2, "items"),
        ("raw/blob-00000.u8.bin", "uint8", 1, "bytes"),
        ("tokens/train-00000.u16le.bin", "uint16", 2, None),  # a sentinel with no honest count
    ],
)
def test_a_non_token_payload_does_not_get_a_token_vocab_bound(path, dtype, size, unit):
    """A group may legitimately carry a fixed-width sidecar that is not tokens."""
    assert _check_one(path, dtype, size, unit) == []


def test_a_real_uint16_token_array_is_still_rejected():
    """Scoping must not have disarmed the check for the case it exists to catch."""
    assert _check_one("tokens/train-00000.u16le.bin", "uint16", 2, "tokens") == [
        "dtype-too-narrow-for-vocab"
    ]


def test_indices_count_as_a_fixed_width_unit_too():
    """token-order/v1 stores index vectors; the same width logic applies."""
    assert _check_one("order/train-00000.u16le.bin", "uint16", 2, "indices") == [
        "dtype-too-narrow-for-vocab"
    ]


def test_a_bool_vocab_size_does_not_silently_disable_the_check():
    """BUG-3: isinstance(True, int) is True in Python, so True would give required=1."""
    entry = ManifestEntry.from_dict({
        "path": "tokens/train-00000.u16le.bin", "sha256": "a" * 64, "bytes": 2000,
        "count": {"unit": "tokens", "value": 1000},
        "format": {"container": "raw", "dtype": "uint16", "byte_order": "little",
                   "header_bytes": 0, "codec": "none"},
    })
    v: list = []
    _check_dtype_width_vs_vocab([entry], {"vocab_size": True}, v, "g")
    # treated as absent (no derived vocab to compare against), NOT as vocab_size=1
    assert v == []
