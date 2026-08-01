"""Tests for the tokenize/pack/shard stage. Mirrors `src/edullm_data/corpus_pack.py`.

Every test here recomputes something from the bytes the packer produced, rather than asserting that
a returned field is present. The distinction matters more in this module than anywhere else in the
suite, because the packer's output IS the corpus: a test that trusted `PackResult.tokens_out` would
be testing the same integer the packer used to convince itself, and a packer that loses tokens
computes a self-consistent wrong number. So the conservation test below sums the SINK's bytes.

Two shard geometries appear:

* `SHARD_TOKENS` (25,001,984) wherever the real geometry is what is under test — the alignment
  invariant, the tail rule, the 100 MB buffer path.
* small `SEQ_LEN`-multiple refs (8,192 / 16,384) everywhere else, so ~40 tests do not each allocate
  100 MB. A hand-built ref is legal input to `pack` (it validates `tokens % SEQ_LEN == 0` itself),
  and every claim proved at 8,192 tokens holds at 25,001,984 because neither the carry loop nor the
  tail arithmetic references the shard size except through `ref.tokens`.

The tokenizer is a callable, never `tokenizers.Tokenizer`: offline, no `tokenizer.json`, and it lets
a test emit an id the real tokenizer never would (a negative, or one past vocab) which is precisely
what the range assertion exists to catch.
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest

from edullm_data.corpus import (
    DTYPE_SIZE,
    FAMILY_MAX_EOS_FRACTION,
    MIN_DOC_TOKENS,
    MIN_MEAN_DOC_TOKENS,
    SEQ_LEN,
    SHARD_TOKENS,
    BuildError,
    Document,
    ShardRef,
    allocate_ordinals,
)
from edullm_data.corpus_pack import (
    DECODE_WINDOW_TOKENS,
    DTYPE_LE,
    PackResult,
    assert_eos_fraction_publishable,
    estimate_eos_fraction,
    pack,
    shard_plan,
    tokenize_documents,
)

EOS = 100_277  # dolma2-shaped: a large id well clear of the 0 the zero-run check watches
VOCAB = 100_352


# --------------------------------------------------------------------------------------
# fixtures: a list-appending sink and a fake tokenizer
# --------------------------------------------------------------------------------------


class Sink:
    """Collects `(ShardRef, bytes)` — the whole reason `pack` takes a callable.

    Keeps the raw bytes, not a decoded array, so tests recompute the token stream through
    `np.frombuffer` exactly as OLMo-core's memmap would. Anything less would let a byte-order or
    dtype bug pass, since a numpy array compared to a numpy array agrees with itself.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[ShardRef, bytes]] = []

    def __call__(self, ref: ShardRef, payload: bytes) -> None:
        self.calls.append((ref, payload))

    @property
    def total_tokens(self) -> int:
        """Recomputed from byte lengths, never from a declared count."""
        return sum(len(p) for _r, p in self.calls) // DTYPE_SIZE

    def stream_back(self) -> np.ndarray:
        """Every shard concatenated, decoded from the bytes as a reader would."""
        if not self.calls:
            return np.empty(0, dtype=DTYPE_LE)
        return np.concatenate([np.frombuffer(p, dtype=DTYPE_LE) for _r, p in self.calls])


def fake_tokenizer(vocab: int = VOCAB, *, ids_per_char: int = 1):
    """`text -> [ids]`, deterministic and reversible-ish, with ids spread across the vocabulary.

    Spread matters: a tokenizer emitting a handful of distinct ids would trip the distinct-ids floor
    for reasons that have nothing to do with the packer, so the mapping walks a large stride through
    the vocab. Id 0 and the EOS id are both avoided so the zero-run and EOS-count checks measure
    only what the packer did.
    """

    def encode(text: str) -> list[int]:
        out = []
        for i, ch in enumerate(text):
            for k in range(ids_per_char):
                # Bounded below EOS for the same reason docs_of is: a body id equal to the boundary
                # id would make the packer's one-EOS-per-document cross-check fire on a healthy
                # fixture.
                out.append(1 + ((ord(ch) * 7919 + i * 4099 + k * 31) % (min(vocab, EOS) - 2)))
        return out

    return encode


def docs_of(lengths, *, start: int = 0, eos: int = EOS) -> list[np.ndarray]:
    """Tokenized documents of the given token counts (EOS included in each count).

    Ids are distinct and ascending across the whole set, so a token can be traced to the document it
    came from — which is what makes the carry-boundary tests able to assert ORDER, not just totals.

    Bodies are confined to `[1, eos)`, which is load-bearing rather than tidy. A body id colliding
    with the EOS id makes the packer's one-EOS-per-document cross-check fire correctly on a fixture
    that meant to be healthy, and a body id of 0 feeds the zero-run check — so a wider range makes
    tests fail for reasons that are about the fixture instead of about the packer. (Caught exactly
    that way: a `% (VOCAB - 2)` body range let ~1 in 100,000 tokens land on EOS.)
    """
    out = []
    next_id = start + 1
    for length in lengths:
        assert length >= 1, "a document must have room for its EOS"
        body = np.arange(next_id, next_id + length - 1, dtype=np.int64)
        next_id += length - 1
        arr = np.empty(length, dtype=DTYPE_LE)
        arr[: length - 1] = 1 + (body % (eos - 1))
        arr[length - 1] = eos
        assert not np.any(arr[: length - 1] == eos), "fixture leaked an EOS into a document body"
        out.append(arr)
    return out


def refs_for(n: int, *, tokens: int = SEQ_LEN, source: str = "web", split: str = "train"):
    return [
        ShardRef(source=source, domain=None, split=split, ordinal=i, tokens=tokens)
        for i in range(n)
    ]


def pack_one(docs, refs, **kw):
    """`pack` over a single stream, returning `(sink, result)`."""
    sink = Sink()
    stream = (refs[0].source, refs[0].domain, refs[0].split)
    results = pack({stream: iter(docs)}, refs, sink=sink, **kw)
    assert len(results) == 1
    return sink, results[0]


# --------------------------------------------------------------------------------------
# The geometry the whole stage rests on
# --------------------------------------------------------------------------------------


def test_shard_tokens_is_a_whole_number_of_sequences():
    """3052 x 8192. The reason the constant is not round, and the check Gate A recomputes."""
    assert SHARD_TOKENS == 3052 * SEQ_LEN == 25_001_984
    assert SHARD_TOKENS % SEQ_LEN == 0
    assert (SHARD_TOKENS * DTYPE_SIZE) % (DTYPE_SIZE * SEQ_LEN) == 0


def test_dtype_is_explicitly_little_endian_not_native():
    """The manifest declares `byte_order: little`, so the buffer must be `<u4` and not native `u4`.

    Note what CANNOT be asserted here, because it cost a debugging round: on a little-endian host
    `np.dtype("<u4").byteorder` is `'='`, not `'<'` — numpy normalises an explicit byte order to
    "native" when they coincide, so `.byteorder` cannot distinguish the two and `<u4 == uint32` is
    True. `.str` preserves the request, which is why it is what this asserts. The distinction is
    invisible on this machine either way; it only manifests on a big-endian host, where
    `np.empty(n, "uint32").tobytes()` would emit big-endian bytes under a manifest claiming little
    and Gate A would report `vocab-out-of-range`, blaming the dtype.
    """
    assert DTYPE_LE.str == "<u4"
    assert DTYPE_LE.itemsize == DTYPE_SIZE == 4
    assert np.arange(2, dtype=DTYPE_LE).tobytes() == b"\x00\x00\x00\x00\x01\x00\x00\x00"


def test_decode_window_matches_the_profiles_own_derivation():
    """If the profile's window arithmetic changes, the build gate must follow it, not a stale 4096."""
    from edullm_data.profiles.base import DECODE_SAMPLE_BYTES
    from edullm_data.profiles.pretrain_tokens_v1 import _N_WINDOWS

    assert DECODE_WINDOW_TOKENS == DECODE_SAMPLE_BYTES // _N_WINDOWS // DTYPE_SIZE == 4096


def test_the_imported_profile_helpers_still_exist():
    """The build gate reuses Gate A's own run scan and vocab cap. A rename in the profile must fail
    HERE, loudly, rather than leaving a second copy of the logic to drift — `_cap_min_distinct_by_vocab`
    was already lost once (it lived only in a deployed wheel)."""
    from edullm_data.profiles.pretrain_tokens_v1 import (
        _cap_min_distinct_by_vocab,
        _longest_run_of,
    )

    assert _longest_run_of(np.array([0, 0, 1, 0, 0, 0], dtype=DTYPE_LE), 0) == 3
    assert _cap_min_distinct_by_vocab(256, 256) == 16
    assert _cap_min_distinct_by_vocab(256, VOCAB) == 256


def test_family_bounds_are_read_from_the_family_file():
    """Not re-typed. The 0.05 that governs this stage must be the same byte the validator resolves."""
    import json

    from edullm_data.contracts import _resolve_families_dir
    from edullm_data.corpus_pack import _family_decode_bounds

    on_disk = json.loads((_resolve_families_dir() / "pretrain.json").read_bytes())
    smoke = on_disk["defaults"]["decode_smoke_test"]
    eos_max, zero_run, distinct = _family_decode_bounds()
    assert (eos_max, zero_run, distinct) == (
        smoke["eos_fraction_max"],
        smoke["zero_run_max"],
        smoke["distinct_ids_min"],
    )
    assert eos_max == FAMILY_MAX_EOS_FRACTION == 0.05


# --------------------------------------------------------------------------------------
# tokenize_documents
# --------------------------------------------------------------------------------------


def test_eos_is_in_the_bytes_and_is_the_last_token():
    """OLMo-core adds no special tokens and recovers boundaries with `(mmap == eos).nonzero()`, so an
    EOS that is not physically present does not exist at all."""
    enc = fake_tokenizer()
    docs = list(tokenize_documents(["hello world", "second doc"], enc, eos_id=EOS, vocab_size=VOCAB))
    assert len(docs) == 2
    for text, arr in zip(["hello world", "second doc"], docs):
        assert arr.dtype == DTYPE_LE
        assert int(arr[-1]) == EOS
        assert int(np.count_nonzero(arr == EOS)) == 1
        # The body is the tokenizer's ids, unmodified: EOS is appended, not substituted.
        assert list(arr[:-1]) == enc(text)
        assert arr.size == len(enc(text)) + 1


def test_add_special_tokens_is_false_so_the_count_is_ours():
    """A `tokenizers.Tokenizer` default of True adds whatever its post-processor says, making the
    per-document token count — and therefore the EOS fraction — a property of a JSON file this repo
    does not own."""
    seen = {}

    class Enc:
        ids = [5, 6, 7]

    class FakeHF:
        def encode_batch(self, texts, add_special_tokens=True):
            seen["add_special_tokens"] = add_special_tokens
            return [Enc() for _ in texts]

        def get_vocab_size(self):
            return VOCAB

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        out = list(tokenize_documents(["a", "b"], FakeHF(), eos_id=EOS))
    assert seen["add_special_tokens"] is False
    assert [list(a) for a in out] == [[5, 6, 7, EOS], [5, 6, 7, EOS]]


def test_documents_accepts_Document_or_str():
    """The reader yields `corpus.Document`; a raw string is the convenience the tests use."""
    enc = fake_tokenizer()
    doc = Document(id="d1", text="hello", source="web")
    from_doc = list(tokenize_documents([doc], enc, eos_id=EOS, vocab_size=VOCAB))[0]
    from_str = list(tokenize_documents(["hello"], enc, eos_id=EOS, vocab_size=VOCAB))[0]
    assert np.array_equal(from_doc, from_str)


def test_batching_does_not_change_the_output_or_its_order():
    """`encode_batch` amortises the FFI crossing; a batch boundary must be invisible."""
    enc = fake_tokenizer()
    texts = [f"document number {i} with some words" for i in range(23)]
    one = [a.tobytes() for a in tokenize_documents(texts, enc, eos_id=EOS, vocab_size=VOCAB)]
    many = [
        a.tobytes()
        for a in tokenize_documents(texts, enc, eos_id=EOS, vocab_size=VOCAB, batch_size=4)
    ]
    assert one == many
    assert len(one) == 23


def test_an_id_at_or_past_vocab_raises_instead_of_wrapping():
    """The uint32 cast CANNOT fail: `2**33` assigned into a `<u4` buffer becomes 0, silently. Gate A
    would then recompute the same range assertion after a full copy."""
    with pytest.raises(BuildError, match="outside"):
        list(tokenize_documents(["x"], lambda t: [VOCAB], eos_id=EOS, vocab_size=VOCAB))
    with pytest.raises(BuildError, match="outside"):
        list(tokenize_documents(["x"], lambda t: [1, 2, 2**33], eos_id=EOS, vocab_size=VOCAB))


def test_a_negative_id_raises_instead_of_wrapping_to_4_billion():
    """Measured on numpy 2.4.4: `-1` into a `<u4` buffer yields 4294967295 with no error."""
    buf = np.empty(1, dtype=DTYPE_LE)
    buf[0:1] = np.array([-1], dtype=np.int64)
    assert int(buf[0]) == 4294967295  # the silence this check exists to break

    with pytest.raises(BuildError, match="outside"):
        list(tokenize_documents(["x"], lambda t: [-1, 5], eos_id=EOS, vocab_size=VOCAB))


def test_eos_outside_vocab_is_refused_up_front():
    """Every shard would fail Gate A's vocab-range assertion on its boundaries alone."""
    with pytest.raises(BuildError, match="eos_id"):
        list(tokenize_documents(["x"], fake_tokenizer(), eos_id=VOCAB + 1, vocab_size=VOCAB))


def test_vocab_size_is_derived_from_the_tokenizer_when_not_passed():
    """`get_vocab_size()` includes added tokens, matching `tokenizer_v1.derive_vocab`."""

    class Enc:
        ids = [VOCAB - 1]

    class FakeHF:
        def encode_batch(self, texts, add_special_tokens=True):
            return [Enc() for _ in texts]

        def get_vocab_size(self):
            return VOCAB - 5  # tighter than the id above, so the assertion must fire

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        with pytest.raises(BuildError, match=f"outside \\[0, {VOCAB - 5}\\)"):
            list(tokenize_documents(["a"], FakeHF(), eos_id=EOS % (VOCAB - 5)))


def test_an_empty_document_still_carries_its_eos():
    """A document that tokenizes to nothing is still a boundary; length 1, not 0."""
    out = list(tokenize_documents([""], lambda t: [], eos_id=EOS, vocab_size=VOCAB))
    assert [list(a) for a in out] == [[EOS]]


def test_tokenizers_parallelism_warns_once_for_the_batch_api(monkeypatch):
    """rayon + fork deadlocks in the child. The library warns and names both values rather than
    setting the variable, because 'false' is the wrong value for the driver doing the tokenizing."""
    import edullm_data.corpus_pack as cp

    monkeypatch.delenv("TOKENIZERS_PARALLELISM", raising=False)
    monkeypatch.setattr(cp, "_warned_parallelism", False)

    class Enc:
        ids = [7]

    class FakeHF:
        def encode_batch(self, texts, add_special_tokens=True):
            return [Enc() for _ in texts]

    with pytest.warns(RuntimeWarning, match="TOKENIZERS_PARALLELISM"):
        list(cp.tokenize_documents(["a"], FakeHF(), eos_id=EOS, vocab_size=VOCAB))
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a second warning would now be an error
        list(cp.tokenize_documents(["b"], FakeHF(), eos_id=EOS, vocab_size=VOCAB))


def test_no_parallelism_warning_when_the_caller_already_decided(monkeypatch):
    import edullm_data.corpus_pack as cp

    monkeypatch.setenv("TOKENIZERS_PARALLELISM", "true")
    monkeypatch.setattr(cp, "_warned_parallelism", False)

    class Enc:
        ids = [7]

    class FakeHF:
        def encode_batch(self, texts, add_special_tokens=True):
            return [Enc() for _ in texts]

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        list(cp.tokenize_documents(["a"], FakeHF(), eos_id=EOS, vocab_size=VOCAB))


# --------------------------------------------------------------------------------------
# THE conservation identity — the strongest test in this stage
# --------------------------------------------------------------------------------------


def test_token_conservation_tokens_in_equals_out_plus_tail():
    """tokens in == tokens out + tail remainder, recomputed from the SINK's bytes.

    The equality is asserted against the bytes rather than against `PackResult.tokens_out`, because
    a packer that loses tokens computes a self-consistent wrong total. `tail` is the only sanctioned
    loss channel, and this stream is sized to exercise it: 3 refs of 8,192 with documents summing to
    a non-multiple, so the last shard truncates.
    """
    lengths = [1000, 3000, 777, 5000, 2500, 4321, 6000, 1500]
    total_in = sum(lengths)
    assert total_in % SEQ_LEN != 0, "the fixture must actually produce a tail"
    docs = docs_of(lengths)

    sink, result = pack_one(docs, refs_for(3), eos_id=EOS, vocab_size=VOCAB)

    assert result.tokens_in == total_in
    assert sink.total_tokens == result.tokens_out
    # THE identity, over bytes on one side and the input on the other.
    assert total_in == sink.total_tokens + result.tail_dropped + result.surplus_dropped
    assert 0 < result.tail_dropped < SEQ_LEN
    assert result.surplus_dropped == 0


def test_conservation_holds_across_many_random_shapes():
    """The identity is a property, so it is tested as one. Random document lengths, random ref
    counts, random ref sizes — the arithmetic must close every time or the packer leaks."""
    rng = np.random.default_rng(20260801)
    for trial in range(60):
        n_refs = int(rng.integers(1, 5))
        ref_tokens = SEQ_LEN * int(rng.integers(1, 4))
        n_docs = int(rng.integers(1, 40))
        lengths = [int(x) for x in rng.integers(MIN_DOC_TOKENS, 4000, size=n_docs)]
        docs = docs_of(lengths)
        refs = refs_for(n_refs, tokens=ref_tokens)
        try:
            sink, result = pack_one(docs, refs, eos_id=EOS, vocab_size=VOCAB)
        except BuildError as exc:
            # The only legitimate refusal at these shapes is an under-allocated plan.
            assert "remain after all" in str(exc), f"trial {trial}: {exc}"
            continue
        assert sum(lengths) == sink.total_tokens + result.tail_dropped + result.surplus_dropped, trial
        assert result.tokens_out == sink.total_tokens, trial
        assert result.tail_dropped < SEQ_LEN, trial


def test_pack_result_rejects_a_conservation_violation():
    """The runtime counterpart of the test above. A `del buf[:n]` off by one iteration looks exactly
    like a correct one, so the arithmetic is checked at construction, not reviewed."""
    with pytest.raises(BuildError, match="conservation FAILED"):
        PackResult(
            stream=("web", None, "train"),
            written=(),
            unfilled=(),
            documents=1,
            tokens_in=1000,
            tokens_out=900,  # 100 unaccounted
            tail_dropped=0,
            surplus_dropped=0,
            max_eos_fraction=0.0,
        )


def test_pack_result_rejects_a_tail_of_a_whole_sequence():
    """A tail >= SEQ_LEN means a whole sequence was dropped, not a sub-sequence remainder."""
    with pytest.raises(BuildError, match="tail_dropped"):
        PackResult(
            stream=("web", None, "train"),
            written=(),
            unfilled=(),
            documents=1,
            tokens_in=SEQ_LEN,
            tokens_out=0,
            tail_dropped=SEQ_LEN,
            surplus_dropped=0,
            max_eos_fraction=0.0,
        )


# --------------------------------------------------------------------------------------
# Shard size and the seq_len alignment invariant
# --------------------------------------------------------------------------------------


def test_every_shard_is_exactly_ref_tokens_except_a_truncated_tail():
    """Refs of 16,384 (2 sequences) so the final shard can be genuinely SHORT rather than dropped.

    The distinction is easy to get wrong and worth stating: a final shard holding less than one
    sequence is not written at all, so a stream must overrun a sequence boundary in its last shard
    to produce a short one. 8,192 + 10,000 does; 8,192 + 4,616 does not.
    """
    lengths = [8000, 8384, 10000]  # 26,384 = one full 16,384 shard + 10,000 -> aligns to 8,192
    sink, result = pack_one(
        docs_of(lengths), refs_for(2, tokens=SEQ_LEN * 2), eos_id=EOS, vocab_size=VOCAB
    )

    sizes = [len(p) // DTYPE_SIZE for _r, p in sink.calls]
    assert sizes == [SEQ_LEN * 2, SEQ_LEN], "one full shard, then a tail truncated to one sequence"
    assert sizes[0] == result.written[0].tokens, "a full shard is exactly ref.tokens"
    assert sizes[-1] % SEQ_LEN == 0, "the tail is a whole number of sequences"
    assert sum(sizes) + result.tail_dropped == sum(lengths)
    assert result.tail_dropped == 10_000 - SEQ_LEN == 1808


def test_every_shard_byte_length_is_a_multiple_of_the_alignment_stride():
    """`check_seq_len_alignment` recomputes `bytes % (4 * 8192) == 0` from a live head and rejects a
    remainder (`profiles/pretrain_tokens_v1.py:426`). promote() is all-or-nothing, so one misaligned
    tail blocks the corpus."""
    stride = DTYPE_SIZE * SEQ_LEN
    rng = np.random.default_rng(11)
    for trial in range(40):
        lengths = [int(x) for x in rng.integers(MIN_DOC_TOKENS, 9000, size=int(rng.integers(1, 25)))]
        sink, _result = pack_one(
            docs_of(lengths), refs_for(4, tokens=SEQ_LEN * 2), eos_id=EOS, vocab_size=VOCAB
        )
        for ref, payload in sink.calls:
            assert len(payload) % stride == 0, (trial, ref.path, len(payload))
            # And the manifest's own identity: count.value * 4 == bytes.
            assert ref.tokens * DTYPE_SIZE == len(payload), (trial, ref.path)


def test_the_tail_ref_reports_its_realized_token_count_not_the_planned_one():
    """`manifest.verify_arithmetic` recomputes `count.value * 4 == bytes`, so a truncated shard still
    declaring its planned token count would be caught — after the copy."""
    planned = SEQ_LEN * 3
    sink, result = pack_one(
        docs_of([planned, 9000]), refs_for(2, tokens=planned), eos_id=EOS, vocab_size=VOCAB
    )
    tail_ref, tail_payload = sink.calls[-1]
    assert tail_ref.tokens == len(tail_payload) // DTYPE_SIZE == SEQ_LEN
    assert tail_ref.tokens < planned, "the tail declares its realized count, not the plan's"
    assert tail_ref.tokens % SEQ_LEN == 0
    assert result.written[-1] == tail_ref
    # The ref is otherwise untouched: same key, so the same manifest path and the same hash chain.
    assert tail_ref.path == "tokens/web/train-00001.u32le.bin"


def test_the_tail_is_truncated_never_padded():
    """Padding invents tokens the tokenizer never emitted. Zero-padding also trips `zero_run_max`
    256, and EOS-padding writes fake document boundaries into the only boundary signal there is."""
    lengths = [SEQ_LEN + 100]  # one shard's worth plus 100 -> second shard holds 100, under a seq
    sink, result = pack_one(docs_of(lengths), refs_for(2), eos_id=EOS, vocab_size=VOCAB)

    assert len(sink.calls) == 1, "the 100-token remainder is not written as a shard"
    stream = sink.stream_back()
    assert stream.size == SEQ_LEN
    assert result.tail_dropped == 100
    # No invented tokens of either flavour: the emitted stream is a prefix of the input.
    assert np.array_equal(stream, docs_of(lengths)[0][:SEQ_LEN])


def test_worst_case_tail_loss_is_one_token_under_a_sequence():
    """The measured bound the design comment quotes: SEQ_LEN - 1 = 8,191 tokens = 32,764 bytes,
    0.0328 % of one 25,001,984-token shard."""
    lengths = [SEQ_LEN, SEQ_LEN - 1]
    sink, result = pack_one(docs_of(lengths), refs_for(2), eos_id=EOS, vocab_size=VOCAB)
    assert result.tail_dropped == SEQ_LEN - 1 == 8191
    assert result.tail_dropped * DTYPE_SIZE == 32_764
    assert result.tail_dropped / SHARD_TOKENS < 0.00033
    assert sink.total_tokens == SEQ_LEN


def test_a_shard_shorter_than_one_sequence_is_never_written():
    """OLMo-core's instance count is `file_size // (item_size * seq_len)`, which FLOORS — so such an
    object holds data no reader can reach, and an empty file passes every checksum and size gate
    before failing `check_decode_smoke` with `empty-shard` after the upload."""
    sink, result = pack_one(docs_of([500]), refs_for(1), eos_id=EOS, vocab_size=VOCAB)
    assert sink.calls == []
    assert result.written == ()
    assert len(result.unfilled) == 1
    assert result.tail_dropped == 500
    assert result.tokens_in == 500 == result.tail_dropped + result.tokens_out


def test_no_zero_token_shard_is_ever_emitted():
    """An empty object is the one failure that passes every cheap gate."""
    rng = np.random.default_rng(3)
    for _ in range(30):
        lengths = [int(x) for x in rng.integers(1, 3000, size=int(rng.integers(1, 10)))]
        try:
            sink, _r = pack_one(docs_of(lengths), refs_for(3), eos_id=None, vocab_size=VOCAB)
        except BuildError:
            continue  # short docs may legitimately fail the EOS gate; that is another test
        for ref, payload in sink.calls:
            assert len(payload) > 0
            assert ref.tokens > 0


def test_a_ref_not_a_multiple_of_seq_len_is_refused_before_any_write():
    sink = Sink()
    bad = [ShardRef(source="web", domain=None, split="train", ordinal=0, tokens=SEQ_LEN + 1)]
    with pytest.raises(BuildError, match="multiple of SEQ_LEN"):
        pack({("web", None, "train"): iter(docs_of([100]))}, bad, sink=sink)
    assert sink.calls == []


def test_the_real_shard_geometry_round_trips():
    """One full-size shard at the production geometry: 25,001,984 tokens, 100,007,936 bytes."""
    docs = docs_of([SHARD_TOKENS // 4] * 4)
    sink, result = pack_one(
        docs, refs_for(1, tokens=SHARD_TOKENS), eos_id=EOS, vocab_size=VOCAB
    )
    ref, payload = sink.calls[0]
    assert ref.tokens == SHARD_TOKENS
    assert len(payload) == SHARD_TOKENS * DTYPE_SIZE == 100_007_936
    assert len(payload) % (DTYPE_SIZE * SEQ_LEN) == 0
    assert result.tail_dropped == 0
    assert ref.path == "tokens/web/train-00000.u32le.bin"


# --------------------------------------------------------------------------------------
# The carry buffer: across documents, and across files
# --------------------------------------------------------------------------------------


def test_a_document_straddling_a_shard_boundary_is_split_not_dropped():
    """The split itself is correct — FSL training re-chunks anyway (§2.2). The bug is losing the
    remainder, so this asserts the second half appears at the head of the NEXT shard, in order."""
    docs = docs_of([SEQ_LEN + 3000, SEQ_LEN - 3000])
    original = docs[0].copy()
    sink, result = pack_one(docs, refs_for(2), eos_id=EOS, vocab_size=VOCAB)

    first = np.frombuffer(sink.calls[0][1], dtype=DTYPE_LE)
    second = np.frombuffer(sink.calls[1][1], dtype=DTYPE_LE)
    assert np.array_equal(first, original[:SEQ_LEN]), "shard 0 is the document's first SEQ_LEN"
    assert np.array_equal(second[:3000], original[SEQ_LEN:]), "the remainder resumes at shard 1's head"
    assert result.tokens_in == sink.total_tokens == SEQ_LEN * 2
    assert result.tail_dropped == result.surplus_dropped == 0, "this stream fits its plan exactly"


def test_carry_survives_a_file_boundary_with_zero_loss():
    """`pack` takes a FLAT iterable and has no concept of a file — the caller chains them. That makes
    a per-file carry reset unrepresentable, which matters because it is the natural shape of the bug:
    one packer per input file, each dropping its own sub-shard remainder (up to 8.4M tokens across
    2,048 partitions, in the implementation this one was written against)."""
    import itertools

    files = [docs_of([1111, 2222], start=0), docs_of([3333, 4444], start=9000), docs_of([5555], start=90000)]
    total = sum(int(d.size) for f in files for d in f)
    flat = itertools.chain.from_iterable(files)

    sink, result = pack_one(list(flat), refs_for(3), eos_id=EOS, vocab_size=VOCAB)

    assert result.tokens_in == total
    assert sink.total_tokens + result.tail_dropped == total
    # And the ORDER is the concatenation of the files, unbroken across every boundary.
    expected = np.concatenate([d for f in files for d in f])
    got = sink.stream_back()
    assert np.array_equal(got, expected[: got.size])


def test_the_packed_stream_is_the_exact_concatenation_of_its_documents():
    """The whole point of a carry buffer: no gaps, no reordering, no duplication. Recomputed by
    round-tripping every shard's BYTES back through np.frombuffer, as OLMo-core's memmap would."""
    lengths = [64, 100, 8192, 1, 4096, 3, 20000, 7]
    docs = docs_of(lengths)
    expected = np.concatenate(docs)
    sink, result = pack_one(docs, refs_for(5, tokens=SEQ_LEN), eos_id=EOS, vocab_size=VOCAB)

    got = sink.stream_back()
    assert got.dtype == DTYPE_LE
    assert got.size == result.tokens_out
    assert np.array_equal(got, expected[: got.size])
    assert expected.size - got.size == result.tail_dropped + result.surplus_dropped


def test_a_document_spanning_three_shards_is_reassembled_intact():
    """One document longer than two whole shards exercises the resume path twice."""
    docs = docs_of([SEQ_LEN * 2 + 500])
    original = docs[0].copy()
    sink, result = pack_one(docs, refs_for(3), eos_id=EOS, vocab_size=VOCAB)
    assert len(sink.calls) == 2, "the 500-token remainder is under one sequence, so it is dropped"
    assert np.array_equal(sink.stream_back(), original[: SEQ_LEN * 2])
    assert result.tail_dropped == 500


def test_an_empty_document_array_is_not_counted_as_a_document():
    """It carries no EOS, so counting it would deflate the mean document length the EOS gate divides
    by — i.e. inflate the apparent EOS fraction. tokenize_documents cannot produce one."""
    docs = docs_of([4000, 4000, 4000])
    docs.insert(1, np.empty(0, dtype=DTYPE_LE))
    sink, result = pack_one(docs, refs_for(2), eos_id=EOS, vocab_size=VOCAB)
    assert result.documents == 3
    assert result.tokens_in == 12000
    assert sink.total_tokens + result.tail_dropped == 12000


def test_a_signed_or_wide_document_array_is_refused():
    """`buf[a:b] = int64_array` wraps silently, so an out-of-vocab id would reach S3 looking like a
    valid small one. The dtype guard is what lets _verify_shard skip a per-token vocab rescan."""
    # uint16 uses a small EOS because 100,277 does not fit — which is itself the dtype confusion the
    # standard forbids: a uint16 shard declared uint32 halves the count and sends ids past vocab.
    for bad in (
        np.array([1, 2, EOS], dtype=np.int64),
        np.array([1, 2, 65_535], dtype=np.uint16),
        np.array([1, 2, EOS], dtype=np.uint64),
    ):
        with pytest.raises(BuildError, match="unsigned numpy arrays"):
            pack_one([bad], refs_for(1), eos_id=EOS, vocab_size=VOCAB)


# --------------------------------------------------------------------------------------
# The EOS gate — the check that makes the FinePhrase synthetic half publishable
# --------------------------------------------------------------------------------------


def test_estimate_eos_fraction_is_the_inverse_of_mean_doc_length():
    assert estimate_eos_fraction(20) == 0.05 == FAMILY_MAX_EOS_FRACTION
    assert estimate_eos_fraction(16) == 0.0625  # rejected
    assert estimate_eos_fraction(MIN_DOC_TOKENS) == 0.015625  # the 3.2x margin corpus.py cites
    assert estimate_eos_fraction(MIN_MEAN_DOC_TOKENS) == FAMILY_MAX_EOS_FRACTION


def test_min_mean_doc_tokens_is_exactly_the_family_bound_boundary():
    """corpus.MIN_MEAN_DOC_TOKENS is not a preference; it is 1/0.05 solved for the mean."""
    assert 1 / MIN_MEAN_DOC_TOKENS == FAMILY_MAX_EOS_FRACTION
    assert estimate_eos_fraction(MIN_MEAN_DOC_TOKENS - 1) > FAMILY_MAX_EOS_FRACTION


def test_a_twelve_token_finephrase_rewrite_stream_raises_with_the_arithmetic():
    """§3.3's real sample: the whole document being "Question: Can light accelerate to the speed of
    light?" — ~12 tokens, EOS fraction 0.083. Filtering below MIN_DOC_TOKENS at read time is what
    makes the synthetic half publishable, and this is the check that proves the filter ran."""
    with pytest.raises(BuildError) as exc:
        assert_eos_fraction_publishable("synthetic-finephrase", documents=1000, tokens=12_000)
    msg = str(exc.value)
    assert "12.00 tokens per document" in msg
    assert "0.0833" in msg
    assert "0.0500" in msg
    assert "eos_fraction_max" in msg
    assert str(MIN_DOC_TOKENS) in msg


def test_a_short_document_stream_raises_during_pack():
    """Not just at design time — the realized mean over the shards actually written."""
    docs = docs_of([12] * 3000)  # 36,000 tokens of 12-token documents
    with pytest.raises(BuildError, match="EOS fraction"):
        pack_one(docs, refs_for(4), eos_id=EOS, vocab_size=VOCAB)


def test_a_stream_at_min_doc_tokens_packs_cleanly():
    """The floor has to be usable, not merely safe: 64-token documents give 0.0156, a 3.2x margin."""
    docs = docs_of([MIN_DOC_TOKENS] * 400)  # 25,600 tokens into 3 x 8,192 = 24,576 of capacity
    sink, result = pack_one(docs, refs_for(3), eos_id=EOS, vocab_size=VOCAB)
    assert result.max_eos_fraction == 1 / MIN_DOC_TOKENS == 0.015625
    assert result.max_eos_fraction < FAMILY_MAX_EOS_FRACTION
    assert len(sink.calls) == 3 and all(len(p) // DTYPE_SIZE == SEQ_LEN for _r, p in sink.calls)
    # 1,024 tokens over the plan's capacity: a surplus, not a tail. Every ref filled exactly.
    assert result.tail_dropped == 0
    assert result.surplus_dropped == 25_600 - SEQ_LEN * 3 == 1024
    assert sink.total_tokens + result.surplus_dropped == MIN_DOC_TOKENS * 400


def test_the_eos_gate_is_measured_per_window_not_per_shard():
    """Gate A divides over a 16,384-token pooled sample, so a shard averaging under the bound can
    still contain a window over it. A packer gating only on the shard average would ship it."""
    # A long healthy document, then a burst of 12-token documents filling one window.
    burst = [12] * (DECODE_WINDOW_TOKENS // 12)
    lengths = [SEQ_LEN * 6] + burst + [SEQ_LEN * 6]
    docs = docs_of(lengths)
    whole_shard_fraction = len(lengths) / sum(lengths)
    assert whole_shard_fraction < FAMILY_MAX_EOS_FRACTION, "the shard average must be INSIDE the bound"

    with pytest.raises(BuildError, match="window"):
        pack_one(docs, refs_for(3, tokens=SEQ_LEN * 13), eos_id=EOS, vocab_size=VOCAB)


def test_a_caller_cannot_loosen_the_family_eos_bound():
    """`profiles.base._bound` clamps a group override for the same reason: a bound a caller widens in
    one keyword is decoration. Tightening is free."""
    docs = docs_of([MIN_DOC_TOKENS] * 400)
    with pytest.raises(BuildError, match="LOOSER than the family bound"):
        pack_one(docs, refs_for(3), eos_id=EOS, vocab_size=VOCAB, max_eos_fraction=0.5)

    with pytest.raises(BuildError, match="EOS fraction"):  # tightening applies
        pack_one(docs, refs_for(3), eos_id=EOS, vocab_size=VOCAB, max_eos_fraction=0.001)


def test_a_corpus_tokenized_without_eos_is_caught_by_the_cross_check():
    """The failure with no other detector. `docs_ending` is the contract's count; `counted` is a fact
    about the bytes, and a corpus with no EOS has no document boundary anywhere, forever."""
    docs = [np.arange(1, 4001, dtype=DTYPE_LE) for _ in range(3)]  # no EOS anywhere
    with pytest.raises(BuildError, match="no EOS at all"):
        pack_one(docs, refs_for(2), eos_id=EOS, vocab_size=VOCAB)


def test_a_document_carrying_two_eos_is_caught_by_the_cross_check():
    """Two boundaries in one document makes `1/mean_doc_tokens` — and therefore the EOS gate —
    measure something other than the document count. The shard must be full, so the mismatch is
    observed on a shard the packer actually emits."""
    docs = docs_of([SEQ_LEN, SEQ_LEN])
    docs[0][17] = EOS  # a second boundary inside document 0
    with pytest.raises(BuildError, match="more than one EOS"):
        pack_one(docs, refs_for(2), eos_id=EOS, vocab_size=VOCAB)


def test_the_cross_check_survives_tail_truncation():
    """Truncation can discard a document's EOS along with its tail, so the identity is stated over
    the EMITTED region only. Without that adjustment every truncated tail would false-positive on
    the one-EOS-per-document check — a build gate that fails on its own correct output.

    One ref of 16,384 fed 8,000 + 300, so the stream EXHAUSTS mid-shard at 8,300 and truncates to
    8,192 — document 2's EOS (index 8,299) falls in the discarded 108. Exhausting mid-shard is what
    routes the remainder to the TAIL channel: a ref the data exactly fills has no truncation at all,
    and a ref the data overruns carries the rest forward into the next shard or the surplus.
    """
    sink, result = pack_one(
        docs_of([8000, 300]), refs_for(1, tokens=SEQ_LEN * 2), eos_id=EOS, vocab_size=VOCAB
    )
    emitted = sink.stream_back()
    assert emitted.size == SEQ_LEN
    assert result.tail_dropped == 108, "8,300 realized - 8,192 aligned"
    assert result.surplus_dropped == 0
    # Document 1's EOS (index 7,999) survived; document 2's (index 8,299) was truncated away. Had
    # _pack_stream not subtracted it from docs_ending, the cross-check would have raised here.
    assert int(np.count_nonzero(emitted == EOS)) == 1
    assert result.documents == 2
    assert result.tokens_in == 8300 == emitted.size + result.tail_dropped


def test_pack_without_eos_id_still_gates_on_the_contract_count():
    """`eos_id` is optional; omitting it downgrades the gate from a byte recompute to the
    one-EOS-per-document contract, and the error says so."""
    docs = docs_of([12] * 3000)
    with pytest.raises(BuildError, match="EOS fraction"):
        pack_one(docs, refs_for(4), eos_id=None, vocab_size=VOCAB)


# --------------------------------------------------------------------------------------
# The other decode bounds, recomputed before the sink
# --------------------------------------------------------------------------------------


def test_a_zero_run_at_the_family_limit_is_refused():
    """Gate A's comparison is `>=` (`profiles/pretrain_tokens_v1.py:367`), so a build gate on `>`
    would pass shards Gate A rejects. 256 zeros is a violation; 255 is not."""
    doc = np.full(SEQ_LEN, 5, dtype=DTYPE_LE)
    doc[1000:1256] = 0  # exactly 256
    doc[-1] = EOS
    with pytest.raises(BuildError, match="consecutive zero ids"):
        pack_one([doc], refs_for(1), eos_id=EOS, vocab_size=VOCAB)


def test_scattered_zeros_are_normal_prose_and_pass():
    """dolma2 maps '!' to id 0. The run form is what makes this a tokenizer-independent check — the
    old density form rejected two healthy 150B shards at 0.0106 against a 0.010 bound."""
    rng = np.random.default_rng(5)
    doc = rng.integers(1, EOS - 1, size=SEQ_LEN * 2).astype(DTYPE_LE)
    # Every other index, so 400 zeros are guaranteed isolated: longest run is exactly 1.
    doc[np.arange(0, 800, 2)] = 0
    doc[-1] = EOS
    sink, _result = pack_one([doc], refs_for(2), eos_id=EOS, vocab_size=VOCAB)
    ids = sink.stream_back()
    assert int(np.count_nonzero(ids == 0)) == 400, "a high zero FRACTION, and still fine"
    assert ids.size == SEQ_LEN * 2, "both shards written; nothing rejected"
    assert len(sink.calls) == 2


def test_an_all_one_token_shard_is_refused_as_degenerate():
    doc = np.full(SEQ_LEN * 2, 7, dtype=DTYPE_LE)
    doc[-1] = EOS
    with pytest.raises(BuildError, match="distinct ids"):
        pack_one([doc], refs_for(2), eos_id=EOS, vocab_size=VOCAB)


def test_the_distinct_floor_is_capped_by_vocab_the_way_gate_a_caps_it():
    """A byte tokenizer has vocab 256; the family floor of 256 would demand every byte value,
    including control bytes formal text never uses. That contaminated real corpora once."""
    rng = np.random.default_rng(9)
    doc = rng.integers(1, 200, size=SEQ_LEN * 2).astype(DTYPE_LE)  # ~199 distinct, under 256
    doc[-1] = 255
    sink, _result = pack_one([doc], refs_for(2), eos_id=255, vocab_size=256)
    assert len(sink.calls) == 2, "capped to vocab // 16 == 16, so ~199 distinct passes"

    # Uncapped (vocab unknown), the family floor of 256 applies and the SAME shard is rejected —
    # which is the contamination this cap exists to prevent: publishers interleaved 0..255 alphabet
    # markers into real training shards to satisfy the floor.
    with pytest.raises(BuildError, match="distinct ids"):
        pack_one([doc.copy()], refs_for(2), eos_id=255, vocab_size=None)


# --------------------------------------------------------------------------------------
# shard_plan
# --------------------------------------------------------------------------------------


def test_shard_plan_rounds_down():
    """A partial shard is dropped by the tail rule, so planning one would guarantee a short shard on
    every stream — making the exception the normal path."""
    plan = shard_plan({("web", None, "train"): SHARD_TOKENS * 3 + SHARD_TOKENS - 1})
    assert plan == [("web", None, "train", 3)]


def test_shard_plan_refuses_a_stream_that_yields_zero_shards_and_says_the_shortfall():
    """Refused, not skipped: a skipped stream gets no ordinals, so pack() finds no destination for
    its documents and the corpus publishes clean with a whole source missing."""
    short = SHARD_TOKENS - 1_000_000
    with pytest.raises(BuildError) as exc:
        shard_plan({("tiny", None, "train"): short})
    msg = str(exc.value)
    assert f"{short:,}" in msg
    assert f"{1_000_000:,}" in msg, "the message must name how many tokens it was short"
    assert "96.0% of a shard" in msg


def test_shard_plan_is_deterministic_regardless_of_input_order():
    """The plan feeds allocate_ordinals, whose keys land inside manifest_sha256 — so an enumeration
    order that leaked through would change the dataset's identity from the same data."""
    a = {("b", None, "train"): SHARD_TOKENS, ("a", "x", "val"): SHARD_TOKENS}
    b = {("a", "x", "val"): SHARD_TOKENS, ("b", None, "train"): SHARD_TOKENS}
    assert shard_plan(a) == shard_plan(b)
    assert [r[0] for r in shard_plan(a)] == ["a", "b"]


def test_shard_plan_output_feeds_allocate_ordinals_directly():
    """The contract's shape, end to end: no adapter between the two, by design."""
    plan = shard_plan(
        {
            ("web", None, "train"): SHARD_TOKENS * 2,
            ("code", "python", "train"): SHARD_TOKENS * 3,
            ("web", None, "val"): SHARD_TOKENS,
        }
    )
    refs = allocate_ordinals(plan)
    assert len(refs) == 6
    assert [r.path for r in refs if r.split == "val"] == ["tokens/web/val-00000.u32le.bin"]
    # Ordinals are global per split, so no two sources collide on a name.
    train = [r.ordinal for r in refs if r.split == "train"]
    assert sorted(train) == list(range(5))
    assert all(r.tokens == SHARD_TOKENS for r in refs)


def test_shard_plan_rejects_a_negative_or_non_integer_token_count():
    with pytest.raises(BuildError, match="non-negative int"):
        shard_plan({("web", None, "train"): -1})
    with pytest.raises(BuildError, match="non-negative int"):
        shard_plan({("web", None, "train"): 1.5 * SHARD_TOKENS})


# --------------------------------------------------------------------------------------
# Multi-stream behaviour and the plan/data agreement
# --------------------------------------------------------------------------------------


def test_multiple_streams_write_to_their_own_ordinals_only():
    refs = allocate_ordinals([("code", "python", "train", 1), ("web", None, "train", 1)])
    refs = [ShardRef(r.source, r.domain, r.split, r.ordinal, tokens=SEQ_LEN) for r in refs]
    sink = Sink()
    results = pack(
        {
            ("web", None, "train"): iter(docs_of([SEQ_LEN])),
            ("code", "python", "train"): iter(docs_of([SEQ_LEN], start=50_000)),
        },
        refs,
        sink=sink,
        eos_id=EOS,
        vocab_size=VOCAB,
    )
    paths = sorted(ref.path for ref, _p in sink.calls)
    assert paths == [
        "tokens/code/python/train-00000.u32le.bin",
        "tokens/web/train-00001.u32le.bin",
    ]
    assert len(results) == 2
    assert all(r.tail_dropped == 0 for r in results)


def test_results_are_returned_in_a_deterministic_order():
    refs = [
        *refs_for(1, source="web"),
        ShardRef(source="code", domain="python", split="train", ordinal=1, tokens=SEQ_LEN),
    ]
    sink = Sink()
    results = pack(
        {
            ("web", None, "train"): iter(docs_of([SEQ_LEN])),
            ("code", "python", "train"): iter(docs_of([SEQ_LEN], start=50_000)),
        },
        refs,
        sink=sink,
        eos_id=EOS,
        vocab_size=VOCAB,
    )
    assert [r.stream for r in results] == [("code", "python", "train"), ("web", None, "train")]


def test_a_stream_with_no_refs_is_refused_rather_than_silently_dropped():
    with pytest.raises(BuildError, match="documents with no refs"):
        pack(
            {
                ("web", None, "train"): iter(docs_of([SEQ_LEN])),
                ("orphan", None, "train"): iter(docs_of([SEQ_LEN])),
            },
            refs_for(1),
            sink=Sink(),
            eos_id=EOS,
        )


def test_refs_with_no_stream_are_refused():
    with pytest.raises(BuildError, match="refs with no documents"):
        pack(
            {("web", None, "train"): iter(docs_of([SEQ_LEN]))},
            [*refs_for(1), ShardRef("ghost", None, "train", 9, tokens=SEQ_LEN)],
            sink=Sink(),
            eos_id=EOS,
        )


def test_unfilled_refs_are_reported_not_raised():
    """Ordinal gaps are legal — nothing in validate.py checks contiguity (allocate_ordinals'
    docstring). No data is lost, so the caller simply writes no manifest entry."""
    sink, result = pack_one(docs_of([SEQ_LEN]), refs_for(4), eos_id=EOS, vocab_size=VOCAB)
    assert len(result.written) == 1
    assert [r.ordinal for r in result.unfilled] == [1, 2, 3]
    assert len(sink.calls) == 1


def test_a_whole_shard_of_surplus_is_refused_because_it_discards_real_tokens():
    """An unfilled ref costs nothing; a surplus DISCARDS tokens already read and tokenized. The
    asymmetry is the point — the honest fix is to re-plan from realized counts."""
    docs = docs_of([SEQ_LEN] * 2) + [np.concatenate(docs_of([SHARD_TOKENS], start=99_999))]
    with pytest.raises(BuildError, match="remain after all"):
        pack_one(docs, refs_for(1), eos_id=EOS, vocab_size=VOCAB)


def test_a_surplus_under_one_shard_is_normal_and_reported():
    """shard_plan rounds down, so a surplus is expected by construction."""
    docs = docs_of([SEQ_LEN, 5000])
    sink, result = pack_one(docs, refs_for(1), eos_id=EOS, vocab_size=VOCAB)
    assert result.surplus_dropped == 5000
    assert result.tokens_in == SEQ_LEN + 5000
    assert sink.total_tokens + result.tail_dropped + result.surplus_dropped == result.tokens_in


def test_the_sink_holds_one_shard_at_a_time():
    """The streaming promise: the Batch driver pipes each shard straight into put_object, so `pack`
    must not accumulate. Asserted by observing that a shard's bytes arrive before the next ref is
    filled — a batching implementation would deliver all of them at the end."""
    order: list[str] = []

    def watching_sink(ref, payload):
        order.append(ref.path)

    stream = ("web", None, "train")
    docs = docs_of([SEQ_LEN] * 3)

    def watched_docs():
        for i, doc in enumerate(docs):
            order.append(f"read-{i}")
            yield doc

    pack({stream: watched_docs()}, refs_for(3), sink=watching_sink, eos_id=EOS, vocab_size=VOCAB)
    # Each shard is delivered before the document after it is read.
    assert order[:4] == [
        "read-0",
        "tokens/web/train-00000.u32le.bin",
        "read-1",
        "tokens/web/train-00001.u32le.bin",
    ]


def test_pack_refuses_a_non_callable_sink():
    with pytest.raises(BuildError, match="callable"):
        pack({("web", None, "train"): iter([])}, refs_for(1), sink=[])  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# End to end: text in, bytes out, tokens recovered
# --------------------------------------------------------------------------------------


def test_round_trip_text_to_shards_to_recovered_token_stream():
    """The whole stage: real-ish text through tokenize_documents into pack, then the bytes read back
    with np.frombuffer exactly as OLMo-core's memmap would, and the documents recovered by splitting
    on EOS — the only boundary signal this corpus ships (§2.3 has no .csv.gz sidecars)."""
    enc = fake_tokenizer()
    texts = [f"Document {i}: " + ("some educational prose here. " * 40) for i in range(12)]
    tokenized = list(tokenize_documents(texts, enc, eos_id=EOS, vocab_size=VOCAB))
    expected = np.concatenate(tokenized)

    sink, result = pack_one(tokenized, refs_for(4, tokens=SEQ_LEN), eos_id=EOS, vocab_size=VOCAB)

    recovered = sink.stream_back()
    assert recovered.dtype == DTYPE_LE
    assert np.array_equal(recovered, expected[: recovered.size])
    assert expected.size == recovered.size + result.tail_dropped + result.surplus_dropped

    # Document boundaries survive: split the recovered stream on EOS, exactly as OLMo-core's local
    # path does with `(mmap == eos_token_id).nonzero()`. Only the documents that END inside the
    # recovered bytes can be compared — the last one is cut mid-document by the shard boundary,
    # which is normal (§2.2) and is why this zips against a truncated prefix of the inputs.
    cuts = np.flatnonzero(recovered == EOS)
    assert cuts.size >= 5, "the fixture must span several documents"
    rebuilt = [
        recovered[start:end]
        for start, end in zip(np.concatenate(([0], cuts[:-1] + 1)), cuts + 1)
    ]
    assert len(rebuilt) == cuts.size
    for original, got in zip(tokenized, rebuilt):
        assert np.array_equal(original, got), "a whole document round-tripped, EOS included"
        assert int(got[-1]) == EOS


def test_the_full_pipeline_plan_allocate_pack_agrees_on_every_key():
    """shard_plan -> allocate_ordinals -> pack, with the shard size scaled down. The keys the sink
    receives must be exactly the plan's, because those keys land inside manifest_sha256."""
    import edullm_data.corpus_pack as cp

    per_shard = SEQ_LEN * 2
    targets = {("web", None, "train"): per_shard * 2, ("web", None, "val"): per_shard}
    plan = shard_plan({k: v * (SHARD_TOKENS // per_shard) for k, v in targets.items()})
    refs = [
        ShardRef(r.source, r.domain, r.split, r.ordinal, tokens=per_shard)
        for r in allocate_ordinals(plan)
    ]
    sink = Sink()
    results = pack(
        {
            ("web", None, "train"): iter(docs_of([per_shard * 2])),
            ("web", None, "val"): iter(docs_of([per_shard], start=70_000)),
        },
        refs,
        sink=sink,
        eos_id=EOS,
        vocab_size=VOCAB,
    )
    assert sorted(ref.path for ref, _p in sink.calls) == [
        "tokens/web/train-00000.u32le.bin",
        "tokens/web/train-00001.u32le.bin",
        "tokens/web/val-00000.u32le.bin",
    ]
    assert sum(r.tokens_out for r in results) == sink.total_tokens == per_shard * 3
    assert all(len(p) % (DTYPE_SIZE * SEQ_LEN) == 0 for _r, p in sink.calls)
    assert cp.DECODE_WINDOW_TOKENS == 4096
