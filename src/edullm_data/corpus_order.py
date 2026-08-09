"""Documents + difficulty -> a chunk permutation. The ``curriculum/edu-mix-983b`` order vector.

The output is a ``uint32`` index vector that reorders the parent's training chunks from easiest to
hardest. Gate A recomputes it (``profiles/token_order_v1.check_order_domain``, the executable
``np.bincount(order, minlength=n) == 1`` at ``token_order_v1.py:209-210``), so a wrong permutation
is rejected rather than shipped — but only the BIJECTION is checked. Whether chunk *i* really is the
*i*-th easiest is a claim no gate can falsify, which is why everything here fails closed.

THE COORDINATE MODEL — ``parent_pool_flat_chunks_v1``
-----------------------------------------------------
A chunk is a fixed-length window of the parent's token space, and the parent's token space is the
concatenation of its TRAIN shards in a declared order:

1. Flatten the parent's train shards in ``read.dataset_paths()`` order into one global token axis.
2. Chunk each shard independently — a chunk NEVER straddles a shard boundary, because OLMo-core's
   instance index is per-file (``numpy_dataset.py:679``: ``file_size // (item_size * seq_len)``).
3. Number the chunks 0..n-1 in that same order.
4. A chunk's OWNER is the document containing its FIRST token. A chunk spanning three documents is
   owned by the first, and its rank is that document's rank.
5. Sort by ``(difficulty_rank, global_chunk_idx)``. The second key makes ties a total order, so the
   vector is a pure function of the inputs — no seed, no dict iteration order, no thread count.

⚠️ **STEP 1 IS THE WHOLE FRAGILITY, AND IT IS AN ORDER, NOT A SET.** ``dataset_paths`` builds each
split's URI list from ``manifest["entries"]`` **in manifest order** (``read.py:527``), and
``publish.build_plan`` writes entries sorted by key (``publish.py:286,419``). So the axis is
"the parent's train shard keys, ascending" — a property of the PUBLISHED manifest, not of the plan.
:func:`chunk_axis_from_manifest` therefore takes the manifest and RECOMPUTES the ordering rather
than reconstructing it from the plan, and :func:`build_order` refuses an axis whose keys are not
ascending. Reconstructing the axis from a different source is how a permutation stays bijective and
becomes meaningless.

🔴 **THE CHUNK-COUNT FORMULA IS SPECIFIED AS ``(tokens - 1) // seq_len`` AND OLMo-CORE USES
``tokens // seq_len``.** Both are implemented; ``CHUNKS_MINUS_ONE`` is the default because it is
what the handoff and the CEO brief specify, and the alternative is one line away. At our shard size
the two differ by exactly one chunk per shard (12,207 vs 12,208), which is **39,205 chunks** across
the corpus and — worse — **shifts every chunk index past the first shard**. See
``artifacts/orchestration/curriculum/status.md`` F-C6. This is a decision for the trainer's owner,
not for this module, so it is a parameter and it is reported.

WHAT FAILS CLOSED, AND WHY EACH ONE IS A SILENT DEFECT OTHERWISE
---------------------------------------------------------------
* **A chunk with no owning labelled document** — the document stream is short of the token space, so
  some chunks' difficulty is unknown. Assigning them rank 0 or rank N would be a plausible-looking
  curriculum over unlabelled data.
* **A labels file whose ``sum(n_tokens + 1)`` is short of its stream's token space** — the labels do
  not describe those tokens. This is the handoff's stream-length check and it is the one that proves
  the labels line up with the tokens.
* **Non-contiguous ``source_doc``** — the owner lookup is a ``searchsorted`` over cumulative strides,
  which assumes record order IS document order.
* **A result that is not a complete permutation of ``[0, n_chunks)``** — recomputed here, with the
  same ``bincount`` Gate A uses, so the failure is found before a 1.9 GB upload rather than after.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .corpus import BuildError

__all__ = [
    "COORDINATE_MODEL",
    "SEQ_LEN_CHUNK",
    "METRIC_SORT",
    "ORDER_DTYPE",
    "ChunkAxis",
    "StreamLabels",
    "chunk_axis_from_manifest",
    "chunk_counts",
    "build_order",
    "identity_order",
]

#: The name of the coordinate model, carried in the curriculum group's metadata. A consumer that
#: chunks differently produces a different axis, and the vector would still be a valid permutation
#: of the same length — so the model has to be NAMED in the artifact or the mismatch is undetectable.
COORDINATE_MODEL = "parent_pool_flat_chunks_v1"

#: The curriculum's chunk length. **2048, not the corpus's ``SEQ_LEN`` of 8192.**
#:
#: These are different quantities and conflating them is the trap. ``corpus.SEQ_LEN`` (8192) is the
#: PACKING stride: shard sizes are whole multiples of it, and ``check_seq_len_alignment`` recomputes
#: ``bytes % (4 * 8192)`` at Gate A. 2048 is the TRAINING sequence length the curriculum orders. A
#: shard aligned to 8192 is automatically a whole number of 2048-token chunks (8192 = 4 x 2048), so
#: the two are compatible — but the chunk count is set by THIS number, and using 8192 would produce
#: a 4x-too-short vector that is still a perfectly valid permutation of its own length.
SEQ_LEN_CHUNK = 2048

#: ``metric -> (field, descending)``. ``False`` = ASCENDING, i.e. rank 0 is the LOWEST value.
#:
#: For MTLD that is correct and load-bearing: higher MTLD = more lexically diverse = HARDER, so
#: ascending puts the easiest document first, which is what an easy-to-hard curriculum means. A
#: reversed sort produces a hard-to-easy schedule that is bijective, passes every gate, and trains
#: the opposite of what was intended — the single most consequential one-character error in this
#: module.
METRIC_SORT: Mapping[str, tuple[str, bool]] = {"mtld": ("mtld", False)}

#: ``<u4`` — the same little-endian uint32 the profile reads back (``token_order_v1._ORDER_DTYPE``).
#: Explicit, not the platform default: a big-endian host would write a vector whose every index is
#: byte-swapped into a different, in-range-looking chunk id.
ORDER_DTYPE = np.dtype("<u4")

#: How a shard's token count becomes a chunk count. See the module docstring's 🔴 note.
CHUNKS_MINUS_ONE = "minus_one"  # (tokens - 1) // seq_len   — the handoff's spec, the DEFAULT
CHUNKS_FLOOR = "floor"          # tokens // seq_len         — OLMo-core numpy_dataset.py:679


@dataclass(frozen=True)
class ChunkAxis:
    """The parent's train token space, as an ordered list of shards and their chunk counts.

    ``keys`` is the shard order the chunk indices are numbered in — the axis IS this order, so it is
    carried rather than recomputed by any consumer. ``tokens`` and ``chunks`` are parallel to it.
    """

    keys: tuple[str, ...]
    tokens: tuple[int, ...]
    chunks: tuple[int, ...]
    seq_len: int
    rule: str

    @property
    def n_chunks(self) -> int:
        return int(sum(self.chunks))

    @property
    def n_tokens(self) -> int:
        return int(sum(self.tokens))

    def offsets(self) -> np.ndarray:
        """Cumulative chunk offsets, length ``len(keys) + 1``. ``offsets[i]`` is shard *i*'s first
        global chunk index. int64 — ``n_chunks`` is ~4.8e8, which fits uint32 but leaves no room for
        the exclusive end of an arithmetic that a later change might widen."""
        out = np.zeros(len(self.chunks) + 1, dtype=np.int64)
        np.cumsum(np.asarray(self.chunks, dtype=np.int64), out=out[1:])
        return out


@dataclass(frozen=True)
class StreamLabels:
    """One (source, domain, split) stream's documents, in tokenization order, with their strides.

    ``strides`` is ``n_tokens + 1`` per document — the EOS included, because the EOS occupies a token
    slot in the packed stream. ``mtld`` is parallel to it.

    ⚠️ **A stream, not a bundle.** Under file-sharding one stream is K bundles built by K separate
    Batch children, and the packer fills the stream's ordinal block part by part, so the stream's
    token space is the CONCATENATION of its parts in ``file_shard`` index order. Assembling them in
    any other order silently reassigns every document past the first part.
    """

    source: str
    domain: str | None
    split: str
    strides: np.ndarray  # int64, per document, = n_tokens + 1
    mtld: np.ndarray  # float64 or float32, parallel to strides

    def __post_init__(self) -> None:
        if self.strides.shape != self.mtld.shape:
            raise BuildError(
                f"{self.source}/{self.split}: {self.strides.size} strides against "
                f"{self.mtld.size} scores — the two arrays must be parallel, since index i of one "
                f"is the same document as index i of the other."
            )

    @property
    def documents(self) -> int:
        return int(self.strides.size)

    @property
    def n_tokens(self) -> int:
        return int(self.strides.sum())


def chunk_counts(tokens: Sequence[int], *, seq_len: int = SEQ_LEN_CHUNK,
                 rule: str = CHUNKS_MINUS_ONE) -> list[int]:
    """Chunks per shard, per ``rule``.

    ``CHUNKS_MINUS_ONE`` is ``(tokens - 1) // seq_len`` — the handoff's and the brief's formula. The
    ``- 1`` is the next-token-prediction convention: an instance of length ``seq_len`` needs
    ``seq_len + 1`` tokens to produce ``seq_len`` labelled positions, so a shard of exactly
    ``k * seq_len`` tokens yields ``k - 1`` complete input/target pairs rather than ``k``.

    ``CHUNKS_FLOOR`` is ``tokens // seq_len`` — what OLMo-core's ``NumpyFSLDataset`` actually
    computes (``numpy_dataset.py:679``), which reads ``seq_len``-token windows and shifts inside the
    training step rather than reserving a token for it.

    **They differ by one chunk on every shard whose token count is a whole multiple of ``seq_len``,
    which is EVERY full shard in this corpus** (25,001,984 = 12,208 x 2048 exactly). That is not a
    rounding difference: it shifts every global chunk index past the first shard.
    """
    if seq_len < 1:
        raise BuildError(f"seq_len must be >= 1; got {seq_len}")
    if rule == CHUNKS_MINUS_ONE:
        return [max(0, (int(t) - 1) // seq_len) for t in tokens]
    if rule == CHUNKS_FLOOR:
        return [int(t) // seq_len for t in tokens]
    raise BuildError(
        f"unknown chunk rule {rule!r}; expected {CHUNKS_MINUS_ONE!r} or {CHUNKS_FLOOR!r}. There is "
        f"no default beyond these two on purpose — a third convention invented at a call site would "
        f"produce a vector that is a valid permutation of the wrong length."
    )


def chunk_axis_from_manifest(
    manifest: Mapping[str, Any],
    *,
    split: str = "train",
    seq_len: int = SEQ_LEN_CHUNK,
    rule: str = CHUNKS_MINUS_ONE,
    dtype_size: int = 4,
) -> ChunkAxis:
    """The chunk axis, RECOMPUTED from the parent's published manifest.

    **From the manifest and not from the plan, and that is the load-bearing decision.** The order the
    chunk indices are numbered in is the order ``read.dataset_paths`` hands a trainer, which is
    ``manifest["entries"]`` order (``read.py:527``) — and ``publish.build_plan`` writes entries
    sorted by key. The plan and the manifest agree today, but they are two artifacts: a republish,
    an excluded shard, or a partial promote makes them disagree, and the resulting permutation would
    still be bijective. So this reads the artifact the trainer reads.

    Token counts come from each entry's ``bytes // dtype_size``, NOT from its declared ``count``.
    ``bytes`` is what Gate A compared against a live ``head``; ``count`` is a producer number that
    ``verify_arithmetic`` checks only against ``bytes`` itself. Deriving from ``bytes`` means the
    axis rests on the one number an independent observation confirmed.

    Only ``split`` shards are included. A val shard in the train axis would shift every chunk index
    past it AND make the curriculum order held-out data.
    """
    from .manifest import parse_shard_name

    keys: list[str] = []
    tokens: list[int] = []
    for raw in manifest.get("entries", []) or []:
        path = str(raw.get("path", ""))
        parsed = parse_shard_name(path)
        if parsed is None or parsed[0] != split:
            continue
        nbytes = int(raw.get("bytes", 0))
        if nbytes % dtype_size != 0:
            raise BuildError(
                f"{path}: {nbytes} bytes is not a multiple of {dtype_size}, so its token count is "
                f"not an integer. The chunk axis cannot be derived from a ragged shard."
            )
        keys.append(path)
        tokens.append(nbytes // dtype_size)
    if not keys:
        raise BuildError(
            f"the parent manifest holds no {split!r} shards, so there is no token space to order. "
            f"An empty axis would make a zero-length 'permutation' that passes every length check."
        )
    return ChunkAxis(
        keys=tuple(keys),
        tokens=tuple(tokens),
        chunks=tuple(chunk_counts(tokens, seq_len=seq_len, rule=rule)),
        seq_len=seq_len,
        rule=rule,
    )


def _assert_axis_is_read_order(axis: ChunkAxis) -> None:
    """The axis must be the ascending-key order a trainer sees. Recomputed, not trusted.

    ``publish.build_plan`` sorts entries by key and ``read.dataset_paths`` preserves manifest order,
    so ascending keys IS the trainer's order. A manifest whose entries are in some other order means
    the axis this module numbers does not match the axis the trainer indexes — and every chunk index
    would point at the wrong tokens while remaining a valid permutation.
    """
    if list(axis.keys) != sorted(axis.keys):
        first = next(
            i for i, (a, b) in enumerate(zip(axis.keys, sorted(axis.keys))) if a != b
        )
        raise BuildError(
            f"the chunk axis is not in ascending key order — entry {first} is {axis.keys[first]!r} "
            f"where the sorted order has {sorted(axis.keys)[first]!r}. `publish.build_plan` sorts "
            f"manifest entries by key and `read.dataset_paths` preserves manifest order, so "
            f"ascending keys is what a trainer indexes. A differently-ordered axis produces a "
            f"permutation that is bijective and points every chunk at the wrong tokens."
        )


def _owner_of_each_chunk(
    stream_strides: np.ndarray, chunk_starts: np.ndarray
) -> np.ndarray:
    """For each chunk start (a token offset into the stream), the index of the owning document.

    ``searchsorted(cumulative_ends, offset, side="right")`` — the document whose half-open token
    range ``[start, end)`` contains the chunk's FIRST token. ``side="right"`` is the correct half of
    the boundary: a chunk starting exactly at a document's first token is owned by THAT document, not
    by the one that ended there. The off-by-one is invisible (both answers are real documents with
    real scores) and would misattribute one chunk per document boundary — ~1 in 2.5 across a corpus
    whose mean document is 815 tokens against a 2,048-token chunk.
    """
    ends = np.cumsum(stream_strides, dtype=np.int64)
    return np.searchsorted(ends, chunk_starts, side="right")


def build_order(
    axis: ChunkAxis,
    streams: Iterable[StreamLabels],
    *,
    shard_stream: Mapping[str, tuple[str, str | None, str]],
    metric: str = "mtld",
) -> np.ndarray:
    """The permutation. ``order[i]`` is the global chunk index that trains *i*-th.

    ``shard_stream`` maps each axis key to the ``(source, domain, split)`` stream that wrote it —
    recoverable from the key itself (``manifest.labels_from_path``) but passed in so this function
    does no path parsing of its own and a caller can be explicit about a nested layout.

    **Fails closed on every one of these**, each of which otherwise yields a bijective, meaningless
    vector:

    * an axis key whose stream has no labels;
    * a stream whose labelled token space is SHORTER than the tokens its shards hold — the handoff's
      stream-length check;
    * a chunk whose first token falls past the last labelled document;
    * a result that is not a complete permutation of ``[0, n_chunks)``.

    Sorting is ``np.lexsort((global_chunk_idx, rank))`` — a STABLE sort on the primary key with the
    chunk index as the explicit secondary, so the output is a pure function of the inputs. ``rank``
    is the document's position in the difficulty order, not its raw score: ranks are small integers
    that sort exactly, where float32 scores would make the tie set depend on rounding.
    """
    if metric not in METRIC_SORT:
        raise BuildError(
            f"unknown metric {metric!r}; known: {sorted(METRIC_SORT)}. The sort DIRECTION lives in "
            f"METRIC_SORT, so an unregistered metric has no declared direction — and guessing it "
            f"produces a hard-to-easy curriculum that passes every gate."
        )
    _field, descending = METRIC_SORT[metric]
    _assert_axis_is_read_order(axis)

    by_stream = {(s.source, s.domain, s.split): s for s in streams}
    if not by_stream:
        raise BuildError("no labelled streams supplied; there is nothing to rank")

    # 1. GLOBAL DOCUMENT RANKS, over every stream at once. Ranking per stream and interleaving would
    #    make a document's rank depend on which stream it came from — i.e. a curriculum that is
    #    easy-to-hard WITHIN a source and arbitrary across sources, which is not the schedule.
    stream_keys = sorted(by_stream, key=lambda s: (s[0], s[1] or "", s[2]))
    doc_base: dict[tuple[str, str | None, str], int] = {}
    at = 0
    for key in stream_keys:
        doc_base[key] = at
        at += by_stream[key].documents
    total_docs = at
    scores = np.empty(total_docs, dtype=np.float64)
    for key in stream_keys:
        s = by_stream[key]
        scores[doc_base[key] : doc_base[key] + s.documents] = s.mtld
    if not bool(np.isfinite(scores).all()):
        n_bad = int(np.count_nonzero(~np.isfinite(scores)))
        raise BuildError(
            f"{n_bad:,} of {total_docs:,} difficulty scores are NaN or inf. A NaN's every "
            f"comparison is False so it lands at an arbitrary rank, and an inf takes rank 0 or "
            f"rank N-1 by accident — the curriculum would be wrong in a way the permutation check "
            f"cannot see, because a bijection over garbage is still a bijection."
        )
    # Ties broken by global document index, so the rank vector is a total order. `argsort` with
    # kind="stable" on an ascending score gives exactly that, and negating for the descending case
    # keeps the SAME tie-break direction (index-ascending) rather than reversing it.
    ordering = np.argsort(-scores if descending else scores, kind="stable")
    rank_of_doc = np.empty(total_docs, dtype=np.int64)
    rank_of_doc[ordering] = np.arange(total_docs, dtype=np.int64)

    # 2. PER SHARD: which stream, which documents, and the token offset the shard begins at.
    #    A shard's chunks are numbered from the shard's own start, so a per-shard cursor into the
    #    stream's token space is what connects the two coordinate systems.
    cursor: dict[tuple[str, str | None, str], int] = {k: 0 for k in stream_keys}
    ends_cache: dict[tuple[str, str | None, str], np.ndarray] = {}
    for key in stream_keys:
        ends_cache[key] = np.cumsum(by_stream[key].strides, dtype=np.int64)

    # The stream-length check, per stream, BEFORE any chunk is mapped: the labelled token space must
    # cover the tokens the stream's shards hold. It may legitimately EXCEED them — tokens_out is a
    # prefix of tokens_in, short by tail_dropped + surplus_dropped — but it must never be shorter.
    shard_tokens_by_stream: dict[tuple[str, str | None, str], int] = {k: 0 for k in stream_keys}
    for k, t in zip(axis.keys, axis.tokens):
        st = shard_stream.get(k)
        if st is None:
            raise BuildError(
                f"axis key {k!r} maps to no stream. Every shard was written by exactly one "
                f"(source, domain, split) stream; a shard with no stream has no documents and so "
                f"its chunks have no difficulty."
            )
        if st not in by_stream:
            raise BuildError(
                f"axis key {k!r} belongs to stream {st!r}, which supplied NO labels. Its chunks "
                f"would have no owning document, so ordering them would mean inventing a "
                f"difficulty for real training tokens. Refusing: a partial curriculum that silently "
                f"omits a source is worse than none."
            )
        shard_tokens_by_stream[st] += int(t)
    for key in stream_keys:
        labelled = by_stream[key].n_tokens
        held = shard_tokens_by_stream[key]
        if labelled < held:
            raise BuildError(
                f"{key}: the labels describe {labelled:,} tokens but this stream's shards hold "
                f"{held:,} ({held - labelled:,} unlabelled). The labelled space must COVER the "
                f"shard space — it may exceed it, since tokens_out is a prefix of tokens_in short "
                f"by tail_dropped + surplus_dropped, but it must not fall short. This is the "
                f"handoff's stream-length check and it is what proves the labels line up with the "
                f"tokens."
            )

    # 3. RANK EVERY CHUNK.
    offsets = axis.offsets()
    n_chunks = int(offsets[-1])
    if n_chunks <= 0:
        raise BuildError(
            f"the axis yields {n_chunks} chunks across {len(axis.keys)} shard(s) at seq_len "
            f"{axis.seq_len}. A zero-length vector is a valid permutation of nothing."
        )
    chunk_rank = np.empty(n_chunks, dtype=np.int64)
    for i, key in enumerate(axis.keys):
        n = int(axis.chunks[i])
        st = shard_stream[key]
        start_tok = cursor[st]
        # The shard consumes its FULL token count from the stream's space even when the last
        # partial chunk is dropped by the `- 1` rule, so the next shard's cursor advances by
        # `axis.tokens[i]` and not by `n * seq_len`. Advancing by the chunked amount would drift
        # the cursor by one chunk per shard and misattribute every document downstream.
        cursor[st] = start_tok + int(axis.tokens[i])
        if n == 0:
            continue
        chunk_starts = start_tok + np.arange(n, dtype=np.int64) * axis.seq_len
        owners = np.searchsorted(ends_cache[st], chunk_starts, side="right")
        n_docs = by_stream[st].documents
        if int(owners.max()) >= n_docs:
            past = int(np.count_nonzero(owners >= n_docs))
            raise BuildError(
                f"{key}: {past} of {n} chunks start past the last labelled document of stream "
                f"{st!r} ({n_docs:,} documents, {by_stream[st].n_tokens:,} labelled tokens). Those "
                f"chunks hold real training tokens whose difficulty is unknown, and assigning them "
                f"rank 0 or rank N would be a plausible-looking curriculum over unlabelled data."
            )
        chunk_rank[offsets[i] : offsets[i] + n] = rank_of_doc[doc_base[st] + owners]

    # 4. SORT. `lexsort` takes keys LAST-is-primary, so `(idx, rank)` sorts by rank then by index.
    idx = np.arange(n_chunks, dtype=np.int64)
    order = np.lexsort((idx, chunk_rank))

    if n_chunks > np.iinfo(np.uint32).max:
        raise BuildError(
            f"{n_chunks:,} chunks exceeds uint32 ({np.iinfo(np.uint32).max:,}); the order vector's "
            f"dtype cannot address them and every index past the limit would WRAP to a small, "
            f"valid-looking chunk id."
        )
    out = order.astype(ORDER_DTYPE)

    # 5. RECOMPUTE THE PERMUTATION, with the same bincount Gate A uses — so the failure is found
    #    here, before a 1.9 GB upload and a 4 h Gate A, rather than after.
    counts = np.bincount(out, minlength=n_chunks)
    if not bool(np.all(counts == 1)):
        n_missing = int(np.count_nonzero(counts == 0))
        n_dup = int(np.count_nonzero(counts > 1))
        raise BuildError(
            f"the assembled order is not a permutation of [0, {n_chunks:,}): {n_missing:,} chunks "
            f"never appear and {n_dup:,} appear more than once. token_order_v1.check_order_domain "
            f"recomputes exactly this at Gate A (np.bincount(order, minlength=n) == 1)."
        )
    return out


def identity_order(n: int) -> np.ndarray:
    """``[0, 1, ..., n-1]`` as ``<u4`` — the val split's order object.

    Held-out data is not curriculum-ordered: the point of a val set is that every evaluation sees
    the same data in the same order, so a shuffled val split would make two checkpoints'
    validation losses incomparable. The identity is nonetheless a real permutation, so it satisfies
    ``check_order_domain`` honestly rather than by exemption.
    """
    if n <= 0:
        raise BuildError(
            f"identity_order({n}) — an empty order vector is a valid permutation of nothing and "
            f"`check_order_domain` reports it as `order-empty`."
        )
    return np.arange(n, dtype=ORDER_DTYPE)
