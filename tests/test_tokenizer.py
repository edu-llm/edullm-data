"""Tokenizer-as-published-artifact tests.

Prove: (1) a tokenizer/v1 dataset validates from its real tokenizer.json; (2) vocab_size/
eos_token_id are DERIVED from that file, not typed; (3) a pretrain dataset that depends_on a
published tokenizer gets its decode-smoke vocab bound from the derived value; (4) the derived
bound actually bites — a token id >= the real vocab is rejected even though no vocab_size was
hand-typed anywhere.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from edullm_data import publish as P
from edullm_data import validate as V
from edullm_data.profiles.tokenizer_v1 import derive_vocab
from edullm_data.s3 import FakeS3

CREATED = "2026-07-29T02:00:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}


def _tokenizer_json(base_vocab: int = 100256, eos_id: int = 100257) -> bytes:
    """A minimal but real-shaped HF tokenizer.json: base vocab of N tokens + an EOS added
    token at id eos_id, so derive_vocab computes vocab_size = eos_id+1."""
    doc = {
        "model": {"type": "BPE", "vocab": {str(i): i for i in range(base_vocab)}},
        "added_tokens": [{"id": eos_id, "content": "<|endoftext|>", "special": True}],
    }
    return json.dumps(doc).encode()


# --------------------------------------------------------------------------------------
# derive_vocab unit
# --------------------------------------------------------------------------------------


def test_derive_vocab_from_tokenizer_json():
    d = derive_vocab(_tokenizer_json(base_vocab=100256, eos_id=100257))
    assert d["vocab_size"] == 100258  # max id 100257 + 1
    assert d["eos_token_id"] == 100257


# --------------------------------------------------------------------------------------
# publish + validate a tokenizer dataset
# --------------------------------------------------------------------------------------


def _publish_tokenizer(s3: FakeS3) -> str:
    d = Path(tempfile.mkdtemp())
    (d / "files").mkdir()
    (d / "files" / "tokenizer.json").write_bytes(_tokenizer_json())
    plan = P.publish(
        d,
        dataset_id="tokenizer/dolma2-bpe",
        purpose="Published Dolma2 tokenizer so pretrain corpora own their tokenizer instead of referencing HuggingFace",
        profile="tokenizer/v1",
        s3=s3,
        created_at=CREATED,
        env=ENV,
    )
    r = V.validate_dataset("edullm-landing", f"tokenizer/dolma2-bpe/{plan.version}", s3, data_bucket="edullm-data")
    assert r.ok, [str(v) for v in r.violations]
    V.promote(r, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")
    return plan.version


def test_tokenizer_dataset_validates_and_promotes():
    s3 = FakeS3()
    ver = _publish_tokenizer(s3)
    assert ("edullm-data", f"tokenizer/dolma2-bpe/{ver}/files/tokenizer.json") in s3._store


def test_tokenizer_dataset_rejects_non_json():
    s3 = FakeS3()
    d = Path(tempfile.mkdtemp())
    (d / "files").mkdir()
    (d / "files" / "tokenizer.json").write_bytes(b"not json at all {{{")
    plan = P.publish(
        d,
        dataset_id="tokenizer/broken-json",
        purpose="A deliberately broken tokenizer to prove the validator rejects an unparseable tokenizer.json",
        profile="tokenizer/v1",
        s3=s3,
        created_at=CREATED,
        env=ENV,
    )
    r = V.validate_dataset("edullm-landing", f"tokenizer/broken-json/{plan.version}", s3, data_bucket="edullm-data")
    assert not r.ok
    assert "tokenizer-json-invalid" in {v.code for v in r.violations}


# --------------------------------------------------------------------------------------
# a pretrain dataset derives its vocab bound from the published tokenizer
# --------------------------------------------------------------------------------------


def _publish_pretrain_depending_on_tokenizer(s3: FakeS3, tok_ver: str, *, bad_token: bool):
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    if bad_token:
        # one id (200000) far above the derived vocab (100258) — must be caught WITHOUT any
        # hand-typed vocab_size on the pretrain side
        arr = np.array([200000] * 40000, dtype=np.uint32)
    else:
        arr = (np.arange(1, 40001, dtype=np.uint32) % 90000)
    (d / "tokens" / "train-00000.u32le.bin").write_bytes(arr.tobytes())
    tok_manifest_sha = json.loads(s3.get("edullm-data", f"tokenizer/dolma2-bpe/{tok_ver}/dataset.json"))["groups"][0]["manifest_sha256"]
    plan = P.publish(
        d,
        dataset_id="pretrain/probe-tok-derive",
        purpose="Pretrain probe that derives its vocab bound from the published tokenizer it depends on",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at=CREATED,
        group_meta={"tokens": {
            "depends_on": [{"role": "tokenizer", "dataset_id": "tokenizer/dolma2-bpe", "version": tok_ver, "manifest_sha256": tok_manifest_sha}],
        }},
        env=ENV,
    )
    return V.validate_dataset("edullm-landing", f"pretrain/probe-tok-derive/{plan.version}", s3, data_bucket="edullm-data")


def test_pretrain_derives_vocab_and_passes_clean():
    s3 = FakeS3()
    tok_ver = _publish_tokenizer(s3)
    r = _publish_pretrain_depending_on_tokenizer(s3, tok_ver, bad_token=False)
    assert r.ok, [str(v) for v in r.violations]


def test_derived_vocab_bound_bites_with_no_typed_vocab():
    s3 = FakeS3()
    tok_ver = _publish_tokenizer(s3)
    r = _publish_pretrain_depending_on_tokenizer(s3, tok_ver, bad_token=True)
    # rejected because sampled ids exceed the DERIVED vocab_size — and nowhere did we type one
    assert not r.ok
    codes = {v.code for v in r.violations}
    assert "vocab-out-of-range" in codes or any("vocab" in c for c in codes), codes


# --------------------------------------------------------------------------------------
# per-dataset tokenizers: each corpus names its own; there is no single canonical one
# --------------------------------------------------------------------------------------


def _publish_named_tokenizer(s3: FakeS3, name: str, *, base_vocab: int, eos_id: int) -> str:
    d = Path(tempfile.mkdtemp())
    (d / "files").mkdir()
    (d / "files" / "tokenizer.json").write_bytes(_tokenizer_json(base_vocab=base_vocab, eos_id=eos_id))
    plan = P.publish(
        d, dataset_id=name,
        purpose=f"Published tokenizer {name} for corpora that were tokenized with it, not with any other",
        profile="tokenizer/v1", s3=s3, created_at=CREATED, env=ENV,
    )
    r = V.validate_dataset("edullm-landing", f"{name}/{plan.version}", s3, data_bucket="edullm-data")
    assert r.ok, [str(v) for v in r.violations]
    V.promote(r, s3, data_bucket="edullm-data", landing_bucket="edullm-landing")
    return plan.version


def _publish_corpus_with_tokenizer(s3: FakeS3, dsid: str, tokenizer: str, max_id: int):
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    # Varied ids in [1, max_id] with max_id present — so the ONLY thing under test is the
    # vocab-range bound derived from the named tokenizer. Fine under a big-vocab tokenizer,
    # out-of-range under a small one.
    #
    # Two properties this fixture must hold so unrelated checks stay quiet, both of which are
    # the FAMILY's bounds (families/pretrain.json defaults.decode_smoke_test), not the
    # profile's laxer fallbacks: >= 256 distinct ids in any 64 KB window, and a zero fraction
    # under 0.01. Before family defaults were wired into the gate, the profile fell back to
    # 16 distinct / 0.5 zeros and this fixture passed while violating both.
    span = max(max_id, 1)
    arr = (np.arange(40000, dtype=np.uint64) % span).astype(np.uint32) + 1  # 1..max_id, no zeros
    arr[0] = max_id  # ensure the top id appears
    (d / "tokens" / "train-00000.u32le.bin").write_bytes(arr.tobytes())
    plan = P.publish(
        d, dataset_id=dsid,
        purpose="Corpus tokenized with a specific published tokenizer, deriving its vocab bound from that one",
        profile="pretrain-tokens/v1", s3=s3, created_at=CREATED,
        tokenizer=tokenizer,  # the first-class per-dataset arg
        env=ENV,
    )
    return V.validate_dataset("edullm-landing", f"{dsid}/{plan.version}", s3, data_bucket="edullm-data")


def test_two_datasets_two_different_tokenizers_each_derive_own_vocab():
    s3 = FakeS3()
    # big tokenizer: vocab 50001; small tokenizer: vocab 5001
    big = _publish_named_tokenizer(s3, "tokenizer/dolma2-bpe", base_vocab=50000, eos_id=50000)
    small = _publish_named_tokenizer(s3, "tokenizer/gpt2-bpe", base_vocab=5000, eos_id=5000)

    # a corpus with ids up to 40000: valid under big-bpe (vocab 50001), INVALID under small-bpe (5001)
    ok = _publish_corpus_with_tokenizer(s3, "pretrain/dolma2-corpus-40k", "tokenizer/dolma2-bpe", max_id=40000)
    assert ok.ok, [str(v) for v in ok.violations]

    bad = _publish_corpus_with_tokenizer(s3, "pretrain/gpt2-corpus-40k", "tokenizer/gpt2-bpe", max_id=40000)
    assert not bad.ok  # 40000 >= derived vocab 5001 — caught against THIS tokenizer, not a shared default
    assert any("vocab" in v.code for v in bad.violations), [str(v) for v in bad.violations]


def test_tokenizer_arg_resolves_latest_version():
    s3 = FakeS3()
    _publish_named_tokenizer(s3, "tokenizer/dolma2-bpe", base_vocab=50000, eos_id=50000)
    # reference without a version → resolves the published latest
    # max_id must clear the family's distinct-ids floor (256) while staying under the
    # tokenizer's derived vocab (50001) — the bound actually under test here is vocab-range.
    ok = _publish_corpus_with_tokenizer(s3, "pretrain/dolma2-corpus-10b", "tokenizer/dolma2-bpe", max_id=1000)
    assert ok.ok, [str(v) for v in ok.violations]


def test_unpublished_tokenizer_rejected_at_publish():
    s3 = FakeS3()
    with pytest.raises(P.PublishError):
        _publish_corpus_with_tokenizer(s3, "pretrain/no-tok-10b", "tokenizer/never-published", max_id=100)
