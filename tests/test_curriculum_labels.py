"""The curriculum workstream's end-to-end contract: labels, the order vector, and ``plan_id``.

Three things are proved here and each one is load-bearing on an irreversible decision:

1. **``plan_id`` is still ``29968a2b04008a8c``.** If it moved, label emission touched the plan
   surface and the 6,750 shards already written by the pre-labels build became orphans. This is the
   single most consequential assertion in the file and it is an EQUALITY on the literal, not a
   comparison of two computed values — two runs of the same bug agree with each other.
2. **The labels sidecar's identities are recomputed, not asserted.** Contiguity elementwise, the
   conservation identity against the packer's own ``tokens_in``, path-id range, finiteness.
3. **The order vector is a complete permutation, and the failures fail CLOSED.** A bijection over
   garbage is still a bijection, so every way of producing a meaningless-but-valid permutation is
   given a test that shows it raises.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

from edullm_data.corpus import BuildError, SHARD_TOKENS
from edullm_data.corpus_build import load_registry, plan_document
from edullm_data.corpus_labels import (
    ITEM_SIZE,
    LABEL_DTYPE,
    LABELS_SCHEMA,
    DocumentLabel,
    LabelCollector,
    build_label_set,
    decode_labels,
    encode_labels,
    labels_key,
    verify_labels,
)
from edullm_data.corpus_mtld import MTLD_SPEC_ID
from edullm_data.corpus_order import (
    CHUNKS_FLOOR,
    CHUNKS_MINUS_ONE,
    COORDINATE_MODEL,
    METRIC_SORT,
    ORDER_DTYPE,
    SEQ_LEN_CHUNK,
    ChunkAxis,
    StreamLabels,
    build_order,
    chunk_axis_from_manifest,
    chunk_counts,
    identity_order,
)

REGISTRY = "artifacts/final-dataset/corpus-registry.json"

#: The plan id the in-flight build is running under. **A LITERAL, on purpose.** Comparing two
#: freshly-computed plans would pass even if both were wrong; comparing to a constant recorded
#: before this change is what makes the assertion evidence.
FROZEN_PLAN_ID = "29968a2b04008a8c"


# ======================================================================================
# 1. plan_id — the assertion the whole restart rests on
# ======================================================================================


def test_plan_id_is_unchanged_by_the_labels_work():
    """🔴 **THE ASSERTION THE OWNER'S RESTART DECISION RESTS ON.**

    ``plan_id`` is ``sha256`` of the plan DOCUMENT only (``corpus_build.py:540``) — sources, splits,
    file_shards, tokens, shard paths. Nothing code-derived enters it: no wheel version, no image
    digest, no output flag. So adding label emission must not move it, and if it does, the change
    touched the plan surface and is wrong.

    What that buys, stated so the test's purpose is not lost: the plan being unchanged is what makes
    the handoff's critical constraint — *"re-staging or re-tokenizing after building the order
    invalidates the permutation"* — SATISFIED rather than violated. Same bundles, same order, same
    shard geometry, same ordinals, so the permutation stays valid across the restart and the shards
    already written are byte-identical to what the labelled build writes.
    """
    specs, meta = load_registry(REGISTRY)
    drawn = [s for s in specs if s.target_tokens > 0]
    plan = plan_document(drawn, registry_meta=meta)
    assert plan["plan_id"] == FROZEN_PLAN_ID, (
        f"plan_id moved to {plan['plan_id']!r}. The curriculum change touched the PLAN SURFACE, "
        f"which orphans every shard the in-flight build has written and invalidates the "
        f"permutation. Revert whatever entered plan_document."
    )


def test_the_plan_shape_the_order_vector_depends_on_is_also_pinned():
    """The plan id is a digest, so it proves the document is unchanged but names nothing.

    These four numbers are what the order-vector arithmetic is built on, so they are pinned
    separately: a future change that legitimately re-cuts the plan should have to update BOTH, which
    makes the arithmetic's dependence on them visible rather than implicit.
    """
    specs, meta = load_registry(REGISTRY)
    plan = plan_document([s for s in specs if s.target_tokens > 0], registry_meta=meta)
    train = [b for b in plan["bundles"] if b["split"] == "train"]
    val = [b for b in plan["bundles"] if b["split"] == "val"]
    assert len(plan["bundles"]) == 185
    assert sum(len(b["shards"]) for b in train) == 39205
    assert sum(len(b["shards"]) for b in val) == 102
    assert plan["shard_tokens"] == SHARD_TOKENS == 25001984


# ======================================================================================
# 2. The labels sidecar
# ======================================================================================


def test_the_record_is_exactly_fourteen_bytes_with_no_padding():
    """``align=False`` is explicit and the width is asserted, because the whole file's parse depends
    on it: a numpy that padded to 16 would shift every field offset and produce a plausible,
    wrongly-shifted read of a 17 GB artifact with nothing to notice."""
    assert ITEM_SIZE == 14
    assert LABEL_DTYPE.itemsize == 14
    assert sum(LABEL_DTYPE[n].itemsize for n in LABEL_DTYPE.names) == 14


def test_the_measured_per_document_size_matches_the_design_budget():
    """The size claim in the module docstring, MEASURED rather than asserted.

    At scale the header (a JSON path table of ~100 entries) amortises to nothing, so the per-document
    cost converges on ``ITEM_SIZE``. Checked at 10,000 documents over 100 files, which is the real
    shape of a bundle's path table.
    """
    c = LabelCollector()
    for i in range(10_000):
        c.add(n_tokens=800 + (i % 97), source_path=f"data/train-{i % 100:05d}.parquet", mtld=42.5)
    ls = c.finish(
        plan_id="p", bundle_id="b", stream=("dclm", None, "train"),
        tokens_in=sum(800 + (i % 97) + 1 for i in range(10_000)),
    )
    body = encode_labels(ls)
    per_doc = len(body) / ls.documents
    assert 14.0 < per_doc < 14.9, f"{per_doc:.2f} B/doc — the path table stopped amortising"
    # And the corpus-scale projection the design rests on.
    projected_gb = 1_206_000_000 * per_doc / 1e9
    assert 16.0 < projected_gb < 18.5, f"{projected_gb:.1f} GB across 1.206 B documents"


def test_a_clean_sidecar_round_trips_and_verifies_with_no_violations():
    rows = [
        DocumentLabel(source_doc=i, n_tokens=100 + i, source_path=f"f{i % 3}.parquet", mtld=10.0 + i)
        for i in range(50)
    ]
    ls = build_label_set(
        rows, plan_id="p1", bundle_id="b1", stream=("dclm", None, "train"),
        tokens_in=sum(r.n_tokens + 1 for r in rows),
    )
    back = decode_labels(encode_labels(ls))
    assert back.schema == LABELS_SCHEMA
    assert back.mtld_spec == MTLD_SPEC_ID
    assert np.array_equal(back.records["n_tokens"], ls.records["n_tokens"])
    assert verify_labels(back, declared_documents=50) == []


def test_a_token_shard_fed_to_the_decoder_is_refused_by_the_magic():
    """Without the magic, a 100 MB token shard parses as ~7.1 M plausible records — every field in
    range, no checksum to contradict it. The sidecar and the shards live under the same plan prefix,
    so this is a mistake a resume path can actually make."""
    shard = np.arange(1000, dtype="<u4").tobytes()
    with pytest.raises(BuildError, match="EDULLBL1"):
        decode_labels(shard)


def test_a_broken_conservation_identity_is_a_violation():
    """``sum(n_tokens + 1) != tokens_in``. The identity is what proves the labels line up with the
    tokens; a sidecar that fails it describes a different document stream."""
    rows = [DocumentLabel(i, 100, "f.parquet", 5.0) for i in range(10)]
    ls = build_label_set(
        rows, plan_id="p", bundle_id="b", stream=("s", None, "train"),
        tokens_in=999,  # truth is 10 * 101 = 1010
    )
    codes = [v.code for v in verify_labels(ls)]
    assert "labels-token-conservation-broken" in codes


def test_conservation_is_checked_against_tokens_in_and_NOT_against_shard_tokens():
    """🔴 **THE BRIEF'S CHECK, CORRECTED.** The task asked for ``sum(n_tokens + 1) == shard tokens``.

    That is FALSE BY CONSTRUCTION here. ``PackResult``'s own identity is
    ``tokens_in == tokens_out + tail_dropped + surplus_dropped``, and ``tokens_out`` is "shard
    tokens". Under ``partial_source=True`` — which ``run_bundle`` always passes, because
    ``_reader_for`` deliberately over-delivers — ``surplus_dropped`` is the unconsumed remainder of
    the last document the packer pulled, which is nonzero whenever that document straddles the end of
    the last ref, i.e. NORMALLY.

    So a gate written against shard tokens fires on healthy bundles at end-of-run, after full
    billable work — the ``_drain_surplus`` failure shape that killed 25 of 27 bundles. This test
    encodes the corrected identity and demonstrates the gap it tolerates.
    """
    rows = [DocumentLabel(i, 1000, "f.parquet", 5.0) for i in range(10)]
    tokens_in = 10 * 1001  # 10,010
    tail_dropped, surplus_dropped = 7, 503
    tokens_out = tokens_in - tail_dropped - surplus_dropped
    assert tokens_out != tokens_in, "the premise: shard tokens are SHORT of labelled tokens"
    ls = build_label_set(
        rows, plan_id="p", bundle_id="b", stream=("s", None, "train"), tokens_in=tokens_in,
    )
    # Passes against tokens_in...
    assert verify_labels(ls) == []
    # ...and would have failed against tokens_out, on a perfectly healthy bundle.
    wrong = build_label_set(
        rows, plan_id="p", bundle_id="b", stream=("s", None, "train"), tokens_in=tokens_out,
    )
    assert "labels-token-conservation-broken" in [v.code for v in verify_labels(wrong)]


def test_non_contiguous_source_doc_is_a_violation():
    """The owner lookup is a ``searchsorted`` over cumulative strides, which assumes record order IS
    document order. A gap or a repeat shifts every document after it and silently reassigns their
    chunks — to real documents with real scores, so nothing else notices."""
    ls = build_label_set(
        [DocumentLabel(i, 100, "f.parquet", 1.0) for i in range(5)],
        plan_id="p", bundle_id="b", stream=("s", None, "train"), tokens_in=5 * 101,
    )
    broken = ls.records.copy()
    broken["source_doc"][3] = 99
    ls2 = type(ls)(**{**ls.__dict__, "records": broken})
    assert "labels-source-doc-not-contiguous" in [v.code for v in verify_labels(ls2)]


def test_a_non_finite_score_is_a_violation():
    """A NaN's every comparison is False so it lands at an arbitrary rank; an inf takes rank 0 or
    N-1 by accident. Either is a wrong curriculum that the permutation check cannot see."""
    ls = build_label_set(
        [DocumentLabel(i, 100, "f.parquet", 1.0) for i in range(5)],
        plan_id="p", bundle_id="b", stream=("s", None, "train"), tokens_in=5 * 101,
    )
    broken = ls.records.copy()
    broken["mtld"][2] = np.nan
    ls2 = type(ls)(**{**ls.__dict__, "records": broken})
    assert "labels-mtld-not-finite" in [v.code for v in verify_labels(ls2)]


def test_a_mismatched_mtld_spec_is_a_violation():
    """Two arms whose difficulty labels came from different functions are not comparable, and both
    produce bijective permutations over plausible floats. The spec id is the only thing that can
    contradict that."""
    ls = build_label_set(
        [DocumentLabel(i, 100, "f.parquet", 1.0) for i in range(5)],
        plan_id="p", bundle_id="b", stream=("s", None, "train"), tokens_in=5 * 101,
    )
    ls2 = type(ls)(**{**ls.__dict__, "mtld_spec": "mtld-something-else/ttr0.71"})
    assert "labels-mtld-spec-mismatch" in [v.code for v in verify_labels(ls2)]


def test_a_document_count_disagreeing_with_the_receipt_is_a_violation():
    """The cross-artifact check — the only one that can catch a labels file grafted onto the wrong
    bundle's receipt."""
    ls = build_label_set(
        [DocumentLabel(i, 100, "f.parquet", 1.0) for i in range(5)],
        plan_id="p", bundle_id="b", stream=("s", None, "train"), tokens_in=5 * 101,
    )
    assert "labels-document-count-mismatch" in [
        v.code for v in verify_labels(ls, declared_documents=7)
    ]


def test_the_labels_key_cannot_be_a_reserved_control_basename():
    """``_assert_safe_key`` refuses the basenames that fire ``edullm-landing-manifest-created``.
    Routed through it for the same reason every other build artifact is."""
    key = labels_key("_ingest/final-dataset/build", "29968a2b04008a8c", "dclm-001--train")
    assert key.endswith("/_labels/dclm-001--train.labels")
    assert "manifest.json" not in key


def test_the_collector_grows_without_losing_or_reordering_records():
    """The collector starts at 65,536 capacity and doubles. A resize bug would silently truncate or
    zero-fill, and ``source_doc`` is materialised as ``arange(n)`` at ``finish()`` so contiguity
    would still pass — the loss would show only as a broken conservation identity."""
    c = LabelCollector(capacity=4)
    n = 1000
    for i in range(n):
        c.add(n_tokens=i + 1, source_path="f.parquet", mtld=float(i))
    ls = c.finish(
        plan_id="p", bundle_id="b", stream=("s", None, "train"),
        tokens_in=sum(i + 1 + 1 for i in range(n)),
    )
    assert ls.documents == n
    assert np.array_equal(ls.records["n_tokens"], np.arange(1, n + 1, dtype="<u4"))
    assert np.array_equal(ls.records["mtld"], np.arange(n, dtype="<f4"))
    assert verify_labels(ls, declared_documents=n) == []


# ======================================================================================
# 3. The order vector
# ======================================================================================


def test_the_sort_direction_is_ascending_so_rank_zero_is_the_easiest():
    """``METRIC_SORT["mtld"] == ("mtld", False)``.

    The single most consequential one-character decision in the workstream: higher MTLD = more
    diverse = harder, so ASCENDING puts the easiest first. A reversed sort produces a hard-to-easy
    schedule that is bijective, passes every gate, and trains the opposite of what was intended.
    """
    assert METRIC_SORT["mtld"] == ("mtld", False)


def test_the_chunk_length_is_2048_and_not_the_corpus_seq_len():
    """2048 is the TRAINING sequence length; ``corpus.SEQ_LEN`` (8192) is the PACKING stride.

    Conflating them yields a 4x-too-short vector that is a perfectly valid permutation of its own
    length. A shard aligned to 8192 is automatically a whole number of 2048-token chunks, so the two
    are compatible and the mistake is invisible.
    """
    from edullm_data.corpus import SEQ_LEN

    assert SEQ_LEN_CHUNK == 2048
    assert SEQ_LEN == 8192
    assert SEQ_LEN % SEQ_LEN_CHUNK == 0


def test_the_n_chunks_arithmetic_at_the_real_shard_size():
    """The brief's arithmetic, recomputed. ``(25,001,984 - 1) // 2048 == 12,207``.

    And the full-corpus projection: 39,205 train shards x 12,207 = 478,575,435 chunks -> a
    1,914,301,740-byte uint32 vector, which is the ~1.92 GB the brief predicts.
    """
    assert chunk_counts([SHARD_TOKENS])[0] == 12_207
    n = 39_205 * 12_207
    assert n == 478_575_435
    assert n * 4 == 1_914_301_740
    assert 1.91e9 < n * 4 < 1.93e9


def test_the_two_chunk_rules_differ_by_one_per_full_shard_and_that_is_reported():
    """🔴 **THE CONFLICT WITH OLMo-core, MEASURED-IN-CODE, pinned so it cannot be forgotten.**

    ``SHARD_TOKENS`` is 12,208 x 2048 EXACTLY, so:

    * the handoff's ``(tokens - 1) // seq_len`` gives **12,207**;
    * OLMo-core's own ``file_size // (item_size * sequence_length)``
      (``numpy_dataset.py:679``) gives **12,208**.

    One chunk per shard, 39,205 shards = 39,205 chunks — and, far worse, every chunk index past the
    first shard SHIFTS. The default here is the handoff's rule because that is what was specified;
    the alternative is one parameter away. **Which one the trainer actually uses is an open question
    for its owner, not something this module may decide silently.**
    """
    assert chunk_counts([SHARD_TOKENS], rule=CHUNKS_MINUS_ONE)[0] == 12_207
    assert chunk_counts([SHARD_TOKENS], rule=CHUNKS_FLOOR)[0] == 12_208
    assert SHARD_TOKENS % SEQ_LEN_CHUNK == 0, "the premise: full shards are exact multiples"
    assert SHARD_TOKENS // SEQ_LEN_CHUNK == 12_208


def _axis(n_shards: int, tokens_each: int, *, source: str = "s") -> tuple:
    keys = tuple(f"tokens/{source}/train-{i:05d}.u32le.bin" for i in range(n_shards))
    axis = ChunkAxis(
        keys=keys,
        tokens=tuple([tokens_each] * n_shards),
        chunks=tuple(chunk_counts([tokens_each] * n_shards)),
        seq_len=SEQ_LEN_CHUNK,
        rule=CHUNKS_MINUS_ONE,
    )
    return axis, {k: (source, None, "train") for k in keys}


def _stream(n_docs: int, tokens_each: int, scores, *, source: str = "s") -> StreamLabels:
    return StreamLabels(
        source=source, domain=None, split="train",
        strides=np.full(n_docs, tokens_each + 1, dtype=np.int64),
        mtld=np.asarray(scores, dtype=np.float64),
    )


def test_a_clean_order_is_a_complete_permutation_of_the_right_length():
    axis, mapping = _axis(3, 20 * SEQ_LEN_CHUNK)
    # 3 shards x 19 chunks = 57 chunks over 3 x 40,960 = 122,880 tokens. The labelled space must
    # COVER that, so the document count is DERIVED from it rather than guessed — my first version
    # used 40 documents of stride 2,049 (81,960 tokens) and failed its own coverage precondition,
    # which is the check working.
    total_tokens = sum(axis.tokens)
    n_docs = -(-total_tokens // 2049) + 1  # ceil, plus one so coverage is strict
    rng = np.random.default_rng(0)
    stream = _stream(n_docs, 2048, rng.random(n_docs) * 100)
    assert stream.n_tokens >= total_tokens
    order = build_order(axis, [stream], shard_stream=mapping)
    assert order.dtype == ORDER_DTYPE
    assert order.size == axis.n_chunks == 57
    assert np.array_equal(np.sort(order), np.arange(57))


def test_the_easiest_document_s_chunks_come_first():
    """The ORDERING claim, not just the bijection. Gate A cannot check this — a permutation is a
    permutation — so it has to be checked here or nowhere."""
    axis, mapping = _axis(1, 8 * SEQ_LEN_CHUNK)  # 7 chunks
    # 8 documents of exactly one chunk each, so document i owns chunk i.
    scores = [50.0, 10.0, 90.0, 20.0, 70.0, 30.0, 60.0, 40.0]
    stream = _stream(8, SEQ_LEN_CHUNK - 1, scores)  # stride 2048 exactly
    order = build_order(axis, [stream], shard_stream=mapping)
    # Chunk 1 (score 10.0, the easiest) must train first.
    assert int(order[0]) == 1
    # And the whole sequence is the score order restricted to the 7 chunks that exist.
    ranked = sorted(range(7), key=lambda i: (scores[i], i))
    assert list(order) == ranked


def test_a_tie_is_broken_by_the_global_chunk_index_so_the_vector_is_deterministic():
    """float32 collapses near-equal scores into ties, so ties are the NORMAL case at 1.2 B
    documents. A nondeterministic tie-break would make a re-run produce a different order over the
    same corpus — indistinguishable from a corrupted one."""
    axis, mapping = _axis(1, 8 * SEQ_LEN_CHUNK)
    stream = _stream(8, SEQ_LEN_CHUNK - 1, [42.0] * 8)  # every score identical
    a = build_order(axis, [stream], shard_stream=mapping)
    b = build_order(axis, [stream], shard_stream=mapping)
    assert np.array_equal(a, b)
    assert list(a) == list(range(7)), "an all-tie input must fall back to chunk order exactly"


def test_a_stream_with_no_labels_fails_closed():
    """Its chunks would have no owning document, so ordering them means inventing a difficulty for
    real training tokens. A partial curriculum that silently omits a source is worse than none."""
    axis, mapping = _axis(2, 20 * SEQ_LEN_CHUNK)
    other = _stream(40, 2048, [1.0] * 40, source="different")
    with pytest.raises(BuildError, match="supplied NO labels"):
        build_order(axis, [other], shard_stream=mapping)


def test_labels_shorter_than_the_shard_space_fail_closed():
    """**The handoff's stream-length check.** The labelled token space must COVER the shard space; it
    may exceed it (``tokens_out`` is a prefix of ``tokens_in``) but must not fall short."""
    axis, mapping = _axis(2, 20 * SEQ_LEN_CHUNK)
    thin = _stream(5, 2048, [1.0] * 5)  # nowhere near 2 x 40,960 tokens
    with pytest.raises(BuildError, match="stream-length check"):
        build_order(axis, [thin], shard_stream=mapping)


def test_an_axis_that_is_not_in_ascending_key_order_fails_closed():
    """``publish.build_plan`` sorts entries by key and ``read.dataset_paths`` preserves manifest
    order, so ascending keys IS what a trainer indexes. A differently-ordered axis gives a bijective
    permutation that points every chunk at the wrong tokens."""
    axis, mapping = _axis(3, 20 * SEQ_LEN_CHUNK)
    scrambled = ChunkAxis(
        keys=(axis.keys[2], axis.keys[0], axis.keys[1]),
        tokens=axis.tokens, chunks=axis.chunks, seq_len=axis.seq_len, rule=axis.rule,
    )
    stream = _stream(40, 2048, [1.0] * 40)
    with pytest.raises(BuildError, match="ascending key order"):
        build_order(scrambled, [stream], shard_stream=mapping)


def test_a_non_finite_score_reaching_the_ranker_fails_closed():
    axis, mapping = _axis(1, 8 * SEQ_LEN_CHUNK)
    stream = _stream(8, SEQ_LEN_CHUNK - 1, [1.0, np.inf, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
    with pytest.raises(BuildError, match="NaN or inf"):
        build_order(axis, [stream], shard_stream=mapping)


def test_the_axis_is_recomputed_from_bytes_and_excludes_val_shards():
    """Token counts come from ``bytes // 4``, not from the declared ``count``: ``bytes`` is what Gate
    A compared against a live ``head``, where ``count`` is a producer number checked only against
    ``bytes`` itself. And a val shard in the train axis would shift every chunk index past it AND
    make the curriculum order held-out data."""
    manifest = {
        "entries": [
            {"path": "tokens/s/train-00000.u32le.bin", "bytes": 8 * SEQ_LEN_CHUNK * 4},
            {"path": "tokens/s/train-00001.u32le.bin", "bytes": 8 * SEQ_LEN_CHUNK * 4},
            {"path": "tokens/s/val-00000.u32le.bin", "bytes": 8 * SEQ_LEN_CHUNK * 4},
        ]
    }
    axis = chunk_axis_from_manifest(manifest)
    assert len(axis.keys) == 2, "the val shard must not enter the train axis"
    assert all("train-" in k for k in axis.keys)
    assert axis.tokens == (8 * SEQ_LEN_CHUNK, 8 * SEQ_LEN_CHUNK)
    assert axis.n_chunks == 14  # 2 x (16384 - 1)//2048 = 2 x 7


def test_a_manifest_with_no_train_shards_fails_closed():
    """An empty axis makes a zero-length 'permutation' that passes every length check."""
    with pytest.raises(BuildError, match="no 'train' shards|holds no"):
        chunk_axis_from_manifest({"entries": [
            {"path": "tokens/s/val-00000.u32le.bin", "bytes": 4096},
        ]})


def test_the_identity_order_is_a_real_permutation():
    """The val split's order object. Held-out data is not curriculum-ordered — every evaluation must
    see the same data in the same order, or two checkpoints' validation losses are incomparable — but
    the identity satisfies ``check_order_domain`` honestly rather than by exemption."""
    n = 102
    ident = identity_order(n)
    assert ident.dtype == ORDER_DTYPE
    counts = np.bincount(ident, minlength=n)
    assert bool(np.all(counts == 1))


def test_the_coordinate_model_is_named_in_the_artifact():
    """A consumer that chunks differently produces a different axis, and the vector would still be a
    valid permutation of the same length. The model has to be NAMED or the mismatch is undetectable.
    """
    assert COORDINATE_MODEL == "parent_pool_flat_chunks_v1"


def test_gate_a_would_accept_the_vector_this_module_produces():
    """The real end-to-end check: run ``token_order_v1``'s OWN checks over a staged order object.

    This is the assertion that matters, because it exercises the code that will actually adjudicate
    the artifact rather than a restatement of what it does.
    """
    from edullm_data.manifest import Format, ManifestEntry
    from edullm_data.profiles import token_order_v1
    from edullm_data.profiles.base import GroupContext

    axis, mapping = _axis(2, 8 * SEQ_LEN_CHUNK)
    rng = np.random.default_rng(3)
    stream = _stream(20, 2048, rng.random(20) * 10)
    order = build_order(axis, [stream], shard_stream=mapping)
    body = order.tobytes()
    n = order.size

    class _S3:
        def head(self, bucket, key):
            return {"size": len(body)}

        def get(self, bucket, key):
            return body

    entry = ManifestEntry(
        path="mtld/train-00000.u32le.bin",
        sha256=hashlib.sha256(body).hexdigest(),
        bytes=len(body),
        count={"unit": "indices", "value": n},
        format=Format(container="raw", dtype="uint32", byte_order="little"),
    )
    ctx = GroupContext(
        dataset_id="curriculum/edu-mix-983b",
        version="v1",
        landing_bucket="edullm-landing",
        prefix="curriculum/edu-mix-983b/v1",
        group={
            "profile": token_order_v1.NAME,
            "ordering": "permutation",
            "block_count": n,
            # ⚠️ REQUIRED. The default cap is 512 MiB and the real vector is 1.92 GB, so without
            # this the check returns `order-too-large` and never loads it. See F-C2.
            "max_order_bytes": 4 * 1024 * 1024 * 1024,
            "depends_on": [
                {"dataset_id": "pretrain/edu-mix-983b", "version": "v1", "block_count": n}
            ],
        },
        manifest={"entries": [entry.to_dict()]},
        s3=_S3(),
        rng_seed="seed",
    )
    out = []
    for check in token_order_v1.CHECKS:
        out += check(ctx)
    assert out == [], f"Gate A rejected our own vector: {[str(v) for v in out]}"


def test_gate_a_REFUSES_the_real_vector_at_the_default_cap():
    """🔴 **F-C2, proved by execution rather than argued.**

    ``_DEFAULT_MAX_ORDER_BYTES`` is 512 MiB. The real train vector is 1,914,301,740 B = 3.57x that,
    so a group that does NOT declare ``max_order_bytes`` gets ``order-too-large`` and the permutation
    is never loaded — Gate A fails on the artifact it exists to check. Demonstrated here with a small
    object and a small cap, so the test costs nothing but exercises the same branch.
    """
    from edullm_data.profiles import token_order_v1

    assert token_order_v1._DEFAULT_MAX_ORDER_BYTES == 512 * 1024 * 1024
    assert 39_205 * 12_207 * 4 > token_order_v1._DEFAULT_MAX_ORDER_BYTES

    from edullm_data.manifest import Format, ManifestEntry
    from edullm_data.profiles.base import GroupContext

    order = np.arange(64, dtype=ORDER_DTYPE)
    body = order.tobytes()

    class _S3:
        def head(self, bucket, key):
            return {"size": len(body)}

        def get(self, bucket, key):
            return body

    entry = ManifestEntry(
        path="mtld/train-00000.u32le.bin",
        sha256=hashlib.sha256(body).hexdigest(),
        bytes=len(body),
        count={"unit": "indices", "value": order.size},
        format=Format(container="raw", dtype="uint32", byte_order="little"),
    )
    ctx = GroupContext(
        dataset_id="curriculum/edu-mix-983b", version="v1", landing_bucket="b",
        prefix="p",
        group={
            "profile": token_order_v1.NAME, "ordering": "permutation",
            "block_count": order.size,
            "max_order_bytes": 16,  # smaller than the object, as 512 MiB is to the real one
            "depends_on": [{"dataset_id": "pretrain/edu-mix-983b", "version": "v1"}],
        },
        manifest={"entries": [entry.to_dict()]}, s3=_S3(), rng_seed="s",
    )
    codes = [v.code for v in token_order_v1.check_order_domain(ctx)]
    assert "order-too-large" in codes


# ======================================================================================
# 4. End to end through `run_bundle`, on FakeS3 — the wiring, not the pieces
# ======================================================================================


def _e2e(labels: bool, *, n_docs: int = 400, words: int = 300):
    """One bundle through the real ``run_bundle``, with and without labels.

    Imports the existing driver test's fixtures rather than re-inventing them, so this exercises the
    same tokenizer, shard size, and document shape the driver's own suite does. Re-writing them here
    would let the two drift and this test would stop covering the real path.
    """
    from edullm_data.s3 import FakeS3

    from tests.test_corpus_build import TEST_SHARD_TOKENS, WordTok, _docs, _small, _spec

    from edullm_data import corpus_build as B

    spec = _spec()
    plan = B.plan_document([spec])
    bundle = next(b for b in B.bundles_of(plan) if b.split == "train")
    docs = _docs(n=n_docs, words=words)
    # `source_path` travels WITH the document — the whole point of the field. Two files so the
    # interned path table has more than one entry and a mislabelling would be visible.
    docs = [
        type(d)(id=d.id, text=d.text, source=d.source, domain=d.domain,
                source_path=f"data/part-{i % 2:05d}.parquet")
        for i, d in enumerate(docs)
    ]
    s3 = FakeS3()
    info = B.run_bundle(
        _small(bundle), plan, spec, s3=s3, bucket="edullm-landing", prefix="_ingest/test-curriculum",
        documents=lambda sp, bu: docs, tokenizer=WordTok(), eos_id=100257, vocab_size=100278,
        wheel_version="0.9.0", labels=labels,
    )
    return s3, info, plan, bundle


def test_run_bundle_emits_a_labels_sidecar_that_the_receipt_verifies():
    """The wiring, end to end: sidecar written, receipt carries its digest, ``verify_receipt``
    re-reads and re-hashes it, zero violations."""
    from edullm_data.corpus_receipt import read_receipt, verify_receipt

    s3, info, plan, bundle = _e2e(True)
    receipt = read_receipt(s3, "edullm-landing", info["receipt_key"])
    assert receipt.labels is not None, "run_bundle(labels=True) wrote no LabelRecord"
    assert receipt.labels.documents > 0
    assert verify_receipt(receipt, s3, "edullm-landing") == []
    # And the sidecar's own recomputes pass against the object actually in S3.
    ls = decode_labels(s3.get("edullm-landing", receipt.labels.key))
    assert ls.plan_id == plan["plan_id"]
    assert ls.bundle_id == bundle.bundle_id
    assert verify_labels(ls, declared_documents=receipt.documents) == []


def test_the_sidecar_holds_one_label_per_document_the_packer_pulled():
    """**The ``on_document``-before-the-yield contract.**

    ``pack`` stops as soon as its refs are full and never drains the iterator, so a hook placed AFTER
    the yield would miss the final document of every bundle. The count must equal
    ``PackResult.documents`` exactly — one short is the off-by-one that looks like a checker bug.
    """
    from edullm_data.corpus_receipt import read_receipt

    s3, info, _plan, _b = _e2e(True)
    receipt = read_receipt(s3, "edullm-landing", info["receipt_key"])
    ls = decode_labels(s3.get("edullm-landing", receipt.labels.key))
    assert ls.documents == receipt.documents
    assert ls.records["source_doc"][0] == 0
    assert ls.records["source_doc"][-1] == ls.documents - 1


def test_the_conservation_identity_closes_on_a_real_bundle():
    """``sum(n_tokens + 1) == PackResult.tokens_in``, on numbers the packer produced rather than on a
    fixture. And the gap to ``tokens_out`` is shown, since that gap is exactly why the check is
    against ``tokens_in``."""
    from edullm_data.corpus_receipt import read_receipt

    s3, info, _plan, _b = _e2e(True)
    receipt = read_receipt(s3, "edullm-landing", info["receipt_key"])
    ls = decode_labels(s3.get("edullm-landing", receipt.labels.key))
    total = int(np.add(ls.records["n_tokens"].astype(np.int64), 1).sum())
    assert total == receipt.tokens_in
    # The prefix property, demonstrated: shard tokens are SHORT of labelled tokens on a healthy run.
    assert receipt.tokens_out <= receipt.tokens_in
    assert receipt.tokens_out + receipt.tail_dropped + receipt.surplus_dropped == receipt.tokens_in


def test_source_path_provenance_survives_the_batching():
    """``tokenize_documents`` batches 1,000 documents and ``pack`` pulls lazily, so a shared "current
    file" variable would mislabel provenance at every file boundary — silently, since both values are
    real files of the same source. Documents alternate between two files, so a shared-variable bug
    collapses the table or skews the split badly."""
    from edullm_data.corpus_receipt import read_receipt

    s3, info, _plan, _b = _e2e(True)
    receipt = read_receipt(s3, "edullm-landing", info["receipt_key"])
    ls = decode_labels(s3.get("edullm-landing", receipt.labels.key))
    assert set(ls.paths) == {"data/part-00000.parquet", "data/part-00001.parquet"}
    # Alternating input, so the two ids must be near-balanced and both present.
    ids, counts = np.unique(ls.records["path_id"], return_counts=True)
    assert set(int(i) for i in ids) == {0, 1}
    assert abs(int(counts[0]) - int(counts[1])) <= 1


def test_labels_off_writes_no_sidecar_and_no_receipt_field():
    """The default must be byte-for-byte the previous behaviour, since the flag exists to be opt-in.
    A receipt that gained a key would change its own canonical bytes — hence ``receipt_sha256`` — on
    work that is bit-for-bit identical."""
    from edullm_data.corpus_receipt import read_receipt

    s3, info, _plan, _b = _e2e(False)
    receipt = read_receipt(s3, "edullm-landing", info["receipt_key"])
    assert receipt.labels is None
    assert "labels" not in json.loads(s3.get("edullm-landing", info["receipt_key"]).decode())
    assert not [k for k in s3.dump("edullm-landing") if "_labels/" in k]


def test_the_shard_bytes_are_IDENTICAL_with_and_without_labels():
    """🔴 **THE PROPERTY THAT MAKES THIS SAFE TO ADD MID-BUILD.**

    The 6,750 shards the pre-labels build already wrote must be byte-identical to what the labelled
    build produces, or the restart orphans them. ``plan_id`` being unchanged is necessary but not
    sufficient — the OUTPUT must also be unchanged, which is what this asserts directly.
    """
    a, info_a, _p, _b = _e2e(False)
    b, info_b, _p2, _b2 = _e2e(True)
    shards_a = {k: v for k, v in a.dump("edullm-landing").items() if k.endswith(".u32le.bin")}
    shards_b = {k: v for k, v in b.dump("edullm-landing").items() if k.endswith(".u32le.bin")}
    assert set(shards_a) == set(shards_b), "labels changed which shards were written"
    for k in shards_a:
        assert shards_a[k] == shards_b[k], f"labels changed the BYTES of {k}"
    assert info_a["tokens_out"] == info_b["tokens_out"]


def test_a_missing_sidecar_is_caught_even_though_every_shard_is_present():
    """The commit-then-die case for labels. Every shard is there at the right size, the conservation
    identity closes, and ``verify`` would report OK — while the curriculum built over the corpus
    silently omits this bundle's chunks. Nothing but the receipt's own claim can see it."""
    from edullm_data.corpus_receipt import read_receipt, verify_receipt

    s3, info, _plan, _b = _e2e(True)
    receipt = read_receipt(s3, "edullm-landing", info["receipt_key"])
    del s3._store[("edullm-landing", receipt.labels.key)]
    codes = [v.code for v in verify_receipt(receipt, s3, "edullm-landing")]
    assert codes == ["receipt-labels-missing"], codes


def test_a_tampered_sidecar_is_caught_by_the_cheap_tier_rehash():
    """Unlike a shard, the labels object IS re-read and re-hashed without ``--deep`` — it is ~14 B
    per document, so there is no reason to defer it. This is a strictly stronger guarantee than the
    one the shard payloads get."""
    from edullm_data.corpus_receipt import read_receipt, verify_receipt

    s3, info, _plan, _b = _e2e(True)
    receipt = read_receipt(s3, "edullm-landing", info["receipt_key"])
    raw = bytearray(s3.get("edullm-landing", receipt.labels.key))
    raw[-1] ^= 0xFF  # flip one bit of the last record, preserving length
    s3.seed("edullm-landing", receipt.labels.key, bytes(raw))
    codes = [v.code for v in verify_receipt(receipt, s3, "edullm-landing")]
    assert "receipt-labels-digest-mismatch" in codes


def test_a_sidecar_from_another_bundle_is_caught():
    """A labels file from another bundle maps this bundle's chunks onto another bundle's documents —
    a permutation that is still bijective and still passes Gate A."""
    from edullm_data.corpus_receipt import read_receipt, verify_receipt

    s3, info, _plan, _b = _e2e(True)
    receipt = read_receipt(s3, "edullm-landing", info["receipt_key"])
    ls = decode_labels(s3.get("edullm-landing", receipt.labels.key))
    forged = type(ls)(**{**ls.__dict__, "bundle_id": "some-other-bundle"})
    body = encode_labels(forged)
    s3.seed("edullm-landing", receipt.labels.key, body)
    import dataclasses as _dc

    receipt2 = _dc.replace(
        receipt,
        labels=_dc.replace(
            receipt.labels, sha256=hashlib.sha256(body).hexdigest(), bytes=len(body)
        ),
    )
    codes = [v.code for v in verify_receipt(receipt2, s3, "edullm-landing")]
    assert "receipt-labels-identity-mismatch" in codes


# ======================================================================================
# 5. The publish surface — two ways Gate A becomes VACUOUS, both proved by execution
# ======================================================================================


def test_without_a_parent_block_count_the_permutation_check_IS_VACUOUS():
    """🔴 **PROVED BY EXECUTION, and it is why the driver refuses to run unfilled.**

    ``_block_count`` returns ``None`` when neither the group nor any ``depends_on`` entry carries
    one. ``check_order_domain`` then takes ``n = order.size`` and checks ``bincount == 1`` against
    the vector's OWN length — which **passes for any permutation of any length**. The check does not
    fail; it stops being a check.

    MEASURED here: a 64-index permutation offered as the order over a 478,575,435-chunk parent
    passes with no violations when ``block_count`` is absent, and produces
    ``permutation-wrong-length`` + ``permutation-not-bijective`` when it is present.

    So an unfilled ``depends_on`` is not a smaller version of the right thing — it is the removal of
    the only gate standing between a 1.9 GB vector and the wrong parent. This is what makes
    ``curriculum_driver.assert_invariants``' refusal correct rather than pedantic.
    """
    import hashlib as _h

    from edullm_data.manifest import Format, ManifestEntry
    from edullm_data.profiles import token_order_v1
    from edullm_data.profiles.base import GroupContext

    order = np.random.default_rng(0).permutation(64).astype(ORDER_DTYPE)
    body = order.tobytes()

    class _S3:
        def head(self, bucket, key):
            return {"size": len(body)}

        def get(self, bucket, key):
            return body

    entry = ManifestEntry(
        path="mtld/train-00000.u32le.bin", sha256=_h.sha256(body).hexdigest(), bytes=len(body),
        count={"unit": "indices", "value": order.size},
        format=Format(container="raw", dtype="uint32", byte_order="little"),
    )

    def _codes(dep):
        ctx = GroupContext(
            dataset_id="curriculum/edu-mix-983b", version="v1", landing_bucket="b", prefix="p",
            group={"profile": token_order_v1.NAME, "ordering": "permutation", "depends_on": [dep]},
            manifest={"entries": [entry.to_dict()]}, s3=_S3(), rng_seed="s",
        )
        return [v.code for v in token_order_v1.check_order_domain(ctx)]

    unpinned = {"dataset_id": "pretrain/edu-mix-983b", "version": "v1"}
    assert _codes(unpinned) == [], "premise: without block_count the check passes — VACUOUSLY"
    pinned = unpinned | {"block_count": 39_205 * 12_207}
    assert "permutation-wrong-length" in _codes(pinned)


def test_a_train_permutation_and_a_val_identity_CANNOT_share_one_group():
    """🔴 **F-C4, proved by execution.** The brief stages both objects in the group ``mtld``.

    ``check_order_domain`` reads ONE group-level ``ordering`` and ONE ``block_count``, then applies
    both to EVERY manifest entry. Two objects of different lengths in one group therefore guarantees
    ``permutation-wrong-length`` on whichever one does not match — so the corpus's train permutation
    and its val identity must be TWO GROUPS, each with its own ``block_count``.

    Demonstrated at small scale; the branch is identical at 478,575,435 vs 1,245,114.
    """
    import hashlib as _h

    from edullm_data.manifest import Format, ManifestEntry
    from edullm_data.profiles import token_order_v1
    from edullm_data.profiles.base import GroupContext

    train = np.random.default_rng(1).permutation(64).astype(ORDER_DTYPE)
    val = identity_order(8)
    bodies = {
        "mtld/train-00000.u32le.bin": train.tobytes(),
        "mtld/val-00000.u32le.bin": val.tobytes(),
    }

    def _rel(key: str) -> str:
        # The profile joins prefix + entry.path, so strip the one-segment prefix explicitly rather
        # than by string search — a `rsplit("p/")` would break on any path containing "p/".
        return key[len("p/"):] if key.startswith("p/") else key

    class _S3:
        def head(self, bucket, key):
            return {"size": len(bodies[_rel(key)])}

        def get(self, bucket, key):
            return bodies[_rel(key)]

    entries = [
        ManifestEntry(
            path=p, sha256=_h.sha256(b).hexdigest(), bytes=len(b),
            count={"unit": "indices", "value": len(b) // 4},
            format=Format(container="raw", dtype="uint32", byte_order="little"),
        ).to_dict()
        for p, b in bodies.items()
    ]
    ctx = GroupContext(
        dataset_id="curriculum/edu-mix-983b", version="v1", landing_bucket="b", prefix="p",
        group={
            "profile": token_order_v1.NAME, "ordering": "permutation", "block_count": 64,
            "depends_on": [{"dataset_id": "pretrain/edu-mix-983b", "version": "v1",
                            "block_count": 64}],
        },
        manifest={"entries": entries}, s3=_S3(), rng_seed="s",
    )
    out = token_order_v1.check_order_domain(ctx)
    codes = [v.code for v in out]
    assert "permutation-wrong-length" in codes, (
        "one group cannot hold two order vectors of different lengths — if this ever passes, "
        "re-read check_order_domain before merging train and val into one group"
    )
    # And it is the VAL object that is rejected, not the train one: the group's single block_count
    # (64) matches train and not val. Asserting the path stops this passing for a different reason,
    # e.g. a stub that fed the wrong bytes to both entries.
    assert {v.path for v in out} == {"mtld/val-00000.u32le.bin"}


def test_the_curriculum_family_default_does_not_supply_a_max_order_bytes():
    """``_cfg`` falls back to ``family_defaults`` before the profile default, so a family-level
    ``max_order_bytes`` would make the driver's declaration redundant. It does not exist — checked
    against the shipped file rather than assumed, since "the family probably handles it" is exactly
    the kind of belief that leaves the cap at 512 MiB."""
    from pathlib import Path

    fam = json.loads(Path("families/curriculum.json").read_text())
    assert "max_order_bytes" not in fam.get("defaults", {}), (
        "the family now sets max_order_bytes — reconcile it with curriculum_driver.MAX_ORDER_BYTES "
        "before publishing, because _cfg prefers the GROUP value and the two could disagree"
    )


# ======================================================================================
# 6. F3 — THE FILE-SHARD PART BOUNDARY. Owner IDENTITY, not permutation validity.
# ======================================================================================
#
# 🔴 THE FINDING THESE TESTS EXIST FOR, in one sentence: `build_order` returned a PERFECT
# PERMUTATION with every chunk after a file-shard part boundary attributed to the WRONG DOCUMENT,
# and nothing caught it. 23.5% of this corpus (232 B of 986 B tokens) is in a file-sharded stream —
# stackv2-edu 7 parts, finepdfs-edu 4, nemotron-cc-math-3 3, -4plus 2.
#
# ⚠️ WHY EVERY PRE-EXISTING TEST MISSED IT, and this is the lesson rather than the bug:
#   * every `_axis()` fixture above uses UNIFORM `tokens_each`, and
#   * no fixture built a MULTI-PART stream, and
#   * `assert np.array_equal(np.sort(order), np.arange(n))` — permutation validity — is satisfied
#     by the drift. So is Gate A's `bincount`. So is `labelled >= held`, which the drift makes MORE
#     satisfied, not less.
# The only assertion that can see this failure is one on WHICH DOCUMENT owns a given chunk. That is
# what these tests assert, and it is why they construct documents that are distinguishable by score.


def _two_part_fixture():
    """Two parts of ONE stream, each with its own tail drop — the real shape, minimally.

    part 0: `tokens_in` 8,692, its 2 shards hold `tokens_out` 8,192 (500 dropped)
    part 1: `tokens_in` 8,492, its 2 shards hold `tokens_out` 8,192 (300 dropped)

    The two gaps DIFFER, which matters: equal gaps would let a single averaged correction look right.

    🔴 **THE SCORES ARE CHOSEN SO THE DRIFT CHANGES THE ANSWER, and that took a correction.** My
    first version scored part 0's documents 1..n and part 1's 1000..1000+n — ascending overall. Under
    those scores the correct owners (0, 5, 11, 16) and the DRIFTED owners (0, 5, 10, 15) are both
    ascending, so **both produce the identical permutation [0,1,2,3]** and the test passed on the
    broken code. That is the audit's finding restated as a fixture property: *a monotone score
    assignment cannot distinguish a drifted owner from a correct one.*

    So part 1 scores BELOW part 0 (50+i against 100+i). Now the correct owners give order [2,3,0,1]
    and the drifted ones give [3,0,1,2]. `test_the_fixture_scores_actually_discriminate_the_drift`
    asserts that difference, so this reasoning is checked rather than trusted.
    """
    keys = tuple(f"tokens/src/train-{i:05d}.u32le.bin" for i in range(4))
    tokens = (4096, 4096, 4096, 4096)
    axis = ChunkAxis(
        keys=keys, tokens=tokens, chunks=tuple(chunk_counts(list(tokens))),
        seq_len=SEQ_LEN_CHUNK, rule=CHUNKS_MINUS_ONE,
    )

    def strides_for(total: int) -> np.ndarray:
        out, at = [], 0
        while at + 801 <= total:
            out.append(801)
            at += 801
        if total - at:
            out.append(total - at)
        return np.array(out, dtype=np.int64)

    s0, s1 = strides_for(8692), strides_for(8492)
    # Part 1 scores BELOW part 0 — see the docstring. Distinct within each part so ranks are a
    # bijection and a chunk's owner is recoverable from its rank.
    m0 = 100.0 + np.arange(s0.size, dtype=np.float64)
    m1 = 50.0 + np.arange(s1.size, dtype=np.float64)
    p0 = StreamLabels(source="src", domain=None, split="train", strides=s0, mtld=m0, part=0)
    p1 = StreamLabels(source="src", domain=None, split="train", strides=s1, mtld=m1, part=1)
    # Shards 0,1 were written by part 0; shards 2,3 by part 1. NOT recoverable from the keys — all
    # four live under `tokens/src/` with globally-allocated ordinals, which is the whole reason
    # `shard_stream` must come from receipts.
    mapping = {keys[0]: ("src", None, "train", 0), keys[1]: ("src", None, "train", 0),
               keys[2]: ("src", None, "train", 1), keys[3]: ("src", None, "train", 1)}
    return axis, [p0, p1], mapping, (s0, s1)


def _expected_order_for(owners, s0, s1) -> list[int]:
    """The permutation implied by a given owner-per-chunk list. Independently computed.

    Global document ids are part 0's documents then part 1's; scores match `_two_part_fixture`.
    `lexsort((idx, rank))` sorts by rank then chunk index, which is `sorted(key=(rank, idx))`.
    """
    scores = np.concatenate([100.0 + np.arange(s0.size), 50.0 + np.arange(s1.size)])
    rank_of_doc = np.empty(scores.size, dtype=np.int64)
    rank_of_doc[np.argsort(scores, kind="stable")] = np.arange(scores.size)
    return sorted(range(len(owners)), key=lambda c: (int(rank_of_doc[owners[c]]), c))


def _owners_correct(axis, s0, s1) -> list[int]:
    """The TRUE owner of each chunk: each part's chunk offsets restart at 0 in ITS OWN labelled
    space, and part 1's documents are numbered after part 0's."""
    ends0, ends1 = np.cumsum(s0), np.cumsum(s1)
    out = []
    for i in range(len(axis.keys)):
        within = (i % 2) * axis.tokens[i]
        ends, base = (ends0, 0) if i < 2 else (ends1, int(s0.size))
        for c in range(axis.chunks[i]):
            out.append(base + int(np.searchsorted(ends, within + c * SEQ_LEN_CHUNK, "right")))
    return out


def _owners_drifted(axis, s0, s1) -> list[int]:
    """What the PRE-F3 code computed: ONE cursor across both parts, advanced by `tokens_out`."""
    ends = np.cumsum(np.concatenate([s0, s1]))
    out, cursor = [], 0
    for i in range(len(axis.keys)):
        for c in range(axis.chunks[i]):
            out.append(int(np.searchsorted(ends, cursor + c * SEQ_LEN_CHUNK, "right")))
        cursor += axis.tokens[i]
    return out


def test_the_owner_of_every_chunk_after_a_part_boundary_is_the_RIGHT_DOCUMENT():
    """🔴 **THE F3 REGRESSION TEST. Owner IDENTITY across the boundary, not permutation validity.**

    Before the fix, `build_order` advanced one cursor across both parts by `axis.tokens[i]`
    (`tokens_out`) while the labelled space advanced by `tokens_in`, so the gap accrued once per part.
    MEASURED at the time: shard 2's chunk was attributed to document 10 when the true owner is
    document 11, and shard 3's to 15 instead of 16 — **and `build_order` returned a perfect
    permutation and raised nothing.**

    The expected permutation is derived from INDEPENDENTLY recomputed owners (`_owners_correct`,
    which restarts each part's offsets in its own space), never from `build_order`'s own output.
    """
    axis, parts, mapping, (s0, s1) = _two_part_fixture()
    order = build_order(axis, parts, shard_stream=mapping)

    # Still a valid permutation. Stated so the test is known not to be passing on a raise, AND to
    # record that this is precisely the assertion that could NOT see the defect.
    assert np.array_equal(np.sort(order), np.arange(axis.n_chunks))

    want = _expected_order_for(_owners_correct(axis, s0, s1), s0, s1)
    assert list(order) == want, (
        f"order {list(order)} != {want} implied by the true owners "
        f"{_owners_correct(axis, s0, s1)}. This is the F3 drift: the cursor crossed a pack() "
        f"boundary and the tokens_in/tokens_out gap accrued."
    )
    # And it is NOT what the drifted cursor would have produced.
    assert list(order) != _expected_order_for(_owners_drifted(axis, s0, s1), s0, s1)


def test_the_fixture_scores_actually_discriminate_the_drift():
    """Guards the fixture. **A monotone score assignment cannot see this defect at all** — correct
    and drifted owners are both ascending, so both yield the identical permutation. My first version
    of the test above did exactly that and passed on broken code. This asserts the current scores
    give two DIFFERENT answers, which is what makes the regression test a test."""
    axis, _p, _m, (s0, s1) = _two_part_fixture()
    correct, drifted = _owners_correct(axis, s0, s1), _owners_drifted(axis, s0, s1)
    assert correct != drifted, "the fixture must straddle a boundary where the owners differ"
    assert correct == [0, 5, 11, 16] and drifted == [0, 5, 10, 15], (correct, drifted)
    assert _expected_order_for(correct, s0, s1) != _expected_order_for(drifted, s0, s1), (
        "the two owner lists imply the SAME permutation, so this fixture cannot detect the drift"
    )
    # The specific values, recorded: [2,3,0,1] correct vs [3,0,1,2] drifted.
    assert _expected_order_for(correct, s0, s1) == [2, 3, 0, 1]
    assert _expected_order_for(drifted, s0, s1) == [3, 0, 1, 2]


def test_the_two_parts_have_DIFFERENT_tokens_in_minus_out_gaps():
    """Guards the fixture, not the code.

    Equal per-part gaps would let a single averaged correction pass, so the fixture must have unequal
    ones for the test above to discriminate. 500 and 300.
    """
    _axis_, parts, _m, (s0, s1) = _two_part_fixture()
    assert int(s0.sum()) - 8192 == 500
    assert int(s1.sum()) - 8192 == 300
    assert parts[0].part == 0 and parts[1].part == 1
    assert parts[0].key == ("src", None, "train", 0)
    assert parts[0].stream == parts[1].stream, "same stream, different parts — the whole point"
    assert float(parts[1].mtld.min()) < float(parts[0].mtld.min()), (
        "part 1 must score BELOW part 0 or the drift is invisible — see _two_part_fixture"
    )


def test_merging_two_parts_into_one_stream_is_now_REFUSED_rather_than_silently_drifting():
    """The pre-F3 representation must not be expressible.

    Concatenating the two parts into ONE `StreamLabels` is exactly what the driver used to do. With
    a part-keyed mapping it no longer type-checks as the same thing: the concatenated part-0 label
    set does not cover part 1's shards, and the per-part `labelled >= held` check fires NAMING the
    shard — where before it was satisfied and the drift went through.
    """
    axis, parts, mapping, (s0, s1) = _two_part_fixture()
    merged = StreamLabels(
        source="src", domain=None, split="train",
        strides=np.concatenate([s0, s1]),
        mtld=np.concatenate([parts[0].mtld, parts[1].mtld]),
        part=0,
    )
    with pytest.raises(BuildError, match="supplied NO labels"):
        build_order(axis, [merged], shard_stream=mapping)


def test_a_three_tuple_mapping_still_works_and_means_part_zero():
    """Backwards compatibility, asserted rather than hoped.

    Every fixture and caller written before F3 passes a 3-tuple. An unsharded stream IS part 0, so
    the 3-tuple must keep working — but it must not become a way to merge K parts again, which the
    test above covers.
    """
    axis, mapping = _axis(2, 20 * SEQ_LEN_CHUNK)
    total = sum(axis.tokens)
    n_docs = -(-total // 2049) + 1
    rng = np.random.default_rng(11)
    stream = _stream(n_docs, 2048, rng.random(n_docs) * 100)
    assert stream.part == 0, "the default must be part 0, or every old fixture silently re-keys"
    three = build_order(axis, [stream], shard_stream=mapping)
    four = build_order(axis, [stream],
                       shard_stream={k: (*v, 0) for k, v in mapping.items()})
    assert np.array_equal(three, four), "a 3-tuple and (…, 0) must be the same mapping"


def test_shard_stream_from_receipts_recovers_the_part_the_KEY_cannot_name():
    """🔴 The mapping must come from RECEIPTS. A shard key structurally cannot name its writer.

    Asserted both ways: `labels_from_path` really does lose the part (so the alternative is not
    merely inconvenient, it is impossible), and the receipt-derived mapping really does recover it.
    """
    from dataclasses import dataclass

    from edullm_data.corpus_order import shard_stream_from_receipts
    from edullm_data.manifest import labels_from_path

    # The measurement that forces this design: 7 children, one prefix, global ordinals.
    got = labels_from_path("tokens/stackv2-edu/train-35260.u32le.bin")
    assert "part" not in got and "file_shard" not in got, (
        "if a shard key ever carries its part, this whole function becomes redundant — but today it "
        f"yields {got!r} and the part is unrecoverable"
    )

    @dataclass
    class _Shard:
        path: str

    @dataclass
    class _Receipt:
        bundle_id: str
        source: str
        domain: object
        split: str
        file_shard: int
        shards: tuple

    receipts = [
        _Receipt("stackv2-edu--train--p00of02", "stackv2-edu", None, "train", 0,
                 (_Shard("tokens/stackv2-edu/train-00000.u32le.bin"),
                  _Shard("tokens/stackv2-edu/train-00001.u32le.bin"))),
        _Receipt("stackv2-edu--train--p01of02", "stackv2-edu", None, "train", 1,
                 (_Shard("tokens/stackv2-edu/train-00002.u32le.bin"),)),
    ]
    mapping = shard_stream_from_receipts(receipts)
    assert mapping["tokens/stackv2-edu/train-00000.u32le.bin"] == ("stackv2-edu", None, "train", 0)
    assert mapping["tokens/stackv2-edu/train-00002.u32le.bin"] == ("stackv2-edu", None, "train", 1)
    # Two ordinals apart in ONE prefix, different parts — the case `labels_from_path` collapses.
    assert mapping["tokens/stackv2-edu/train-00001.u32le.bin"][3] == 0


def test_two_receipts_claiming_one_shard_are_REFUSED_by_the_mapping_builder():
    """Keeping the last writer would map a shard's chunks onto the wrong part's documents — the F3
    failure arriving by a different route, so it is refused here as well as in `verify_bundle_set`."""
    from dataclasses import dataclass

    from edullm_data.corpus_order import shard_stream_from_receipts

    @dataclass
    class _S:
        path: str

    @dataclass
    class _R:
        bundle_id: str
        source: str
        domain: object
        split: str
        file_shard: int
        shards: tuple

    dup = "tokens/src/train-00000.u32le.bin"
    with pytest.raises(BuildError, match="claimed by two different bundles"):
        shard_stream_from_receipts([
            _R("a--p00of02", "src", None, "train", 0, (_S(dup),)),
            _R("a--p01of02", "src", None, "train", 1, (_S(dup),)),
        ])


def test_two_label_sets_for_the_SAME_part_are_refused():
    """A retry that wrote a second sidecar without removing the first. Their documents would be
    ranked twice and one set would own every chunk of the part — silently, since both are real."""
    axis, parts, mapping, _s = _two_part_fixture()
    with pytest.raises(BuildError, match="two label sets claim part"):
        build_order(axis, [parts[0], parts[0], parts[1]], shard_stream=mapping)


# ======================================================================================
# 7. F9 — the degenerate zero-chunk shard. UNREACHABLE from our packer, now ASSERTED.
# ======================================================================================


def test_a_zero_chunk_shard_in_the_parent_manifest_is_REFUSED():
    """🔴 The consumer SKIPS a zero-chunk file SILENTLY, which renumbers everything after it.

    `curriculum_loader.py:67-68` does a bare `continue` when a file's chunk count is `<= 0`. So a
    vector built over an axis that INCLUDED that file is a perfect permutation of the right length
    indexing a different axis than the trainer walks — and Gate A's `bincount` structurally cannot
    see it, because the corruption is in the correspondence between paths and indices, not in the
    vector.

    MEASURED before this guard: a 20-byte shard gave `chunks=(9, 0, 9)` and `build_order` SUCCEEDED.
    """
    manifest = {"entries": [
        {"path": "tokens/s/train-00000.u32le.bin", "bytes": 20 * SEQ_LEN_CHUNK * 4},
        {"path": "tokens/s/train-00001.u32le.bin", "bytes": 20},   # 5 tokens -> 0 chunks
        {"path": "tokens/s/train-00002.u32le.bin", "bytes": 20 * SEQ_LEN_CHUNK * 4},
    ]}
    with pytest.raises(BuildError, match="yield ZERO chunks"):
        chunk_axis_from_manifest(manifest)


def test_the_zero_chunk_refusal_names_the_loader_and_every_offending_shard():
    """A diagnostic naming one shard when three are broken sends the reader back to find the rest."""
    manifest = {"entries": (
        [{"path": f"tokens/s/train-{i:05d}.u32le.bin", "bytes": 20} for i in range(3)]
        + [{"path": "tokens/s/train-00003.u32le.bin", "bytes": 20 * SEQ_LEN_CHUNK * 4}]
    )}
    with pytest.raises(BuildError) as exc:
        chunk_axis_from_manifest(manifest)
    msg = str(exc.value)
    assert "3 of 4" in msg
    assert "curriculum_loader.py:67-68" in msg, "the refusal must name WHY, not just refuse"
    for i in range(3):
        assert f"train-{i:05d}" in msg
    assert "train-00003" not in msg, "the healthy shard must not be blamed"


def test_the_guard_does_NOT_fire_on_the_smallest_shard_our_packer_can_write():
    """The complement, and it is what stops this guard from being a false alarm.

    `corpus_pack.py:840-846` refuses a shard under one whole `SEQ_LEN` (8,192 tokens), which is 4
    chunks at 2,048 — so the smallest REAL shard clears `chunks <= 0` with margin. A guard that also
    rejected that would block every legitimate short tail shard, of which this corpus has one per
    stream.
    """
    from edullm_data.corpus import SEQ_LEN

    smallest = SEQ_LEN * 4  # bytes of the smallest shard corpus_pack will emit
    axis = chunk_axis_from_manifest(
        {"entries": [{"path": "tokens/s/train-00000.u32le.bin", "bytes": smallest}]}
    )
    assert axis.chunks == (3,), "8,192 tokens is 3 chunks under (t-1)//2048"
    assert min(axis.chunks) > 0
    # And the two formulas agree that this is NOT skippable, which is the property being relied on.
    assert chunk_counts([SEQ_LEN], rule=CHUNKS_FLOOR)[0] == 4
