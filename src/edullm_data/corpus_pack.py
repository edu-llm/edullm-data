"""Tokenize → pack → shard: the stage whose output Gate A judges byte-for-byte (§5.6 phase 1).

Every earlier stage produces metadata a human can re-read and fix. This one produces the ~10,400
objects that ARE the corpus, and three of its failure modes are invisible until after the upload:

* **No EOS in the bytes.** OLMo-core adds no special tokens (``families/pretrain.json`` notes;
  §2.3 "EOS must be in your bytes"), and its local document-boundary path derives boundaries from
  ``(mmap == eos_token_id).nonzero()`` — so a corpus tokenized without an explicit EOS has no
  document boundary anywhere in it, at any point, and cannot be given one afterwards. Nothing
  rejects it either: the ids are real, in range, and decode to real text. :func:`tokenize_documents`
  appends EOS itself rather than trusting a library default, and :func:`pack` re-counts it in the
  bytes it is about to write.
* **A shard whose length is not a whole multiple of ``seq_len``.**
  ``pretrain_tokens_v1.check_seq_len_alignment`` (``profiles/pretrain_tokens_v1.py:426``)
  recomputes ``bytes % (4 * 8192)`` from a live ``head`` and REJECTS a non-zero remainder. Since
  ``promote()`` is all-or-nothing, one misaligned tail shard blocks the whole corpus.
* **An EOS fraction over the family bound.** ``families/pretrain.json`` sets
  ``decode_smoke_test.eos_fraction_max`` to **0.05**, and one EOS per document makes a shard's EOS
  fraction exactly ``1 / mean_doc_tokens`` — so a corpus of short documents is unpublishable and
  the packer is the last place that fact is cheap. §3.3's FinePhrase rewrites include a ~12-token
  document; at that mean the fraction is 0.083 and every shard fails.

WHY THE GATES RUN HERE AND NOT AT GATE A
----------------------------------------
``corpus.BuildError``'s docstring states the economics: a shard that is wrong at pack time is free
to fix, and the same shard discovered at Gate A has already cost a copy and a hash of the whole
corpus (~630 GB / ~1.4 h at this size). So :func:`pack` recomputes the family's own decode bounds
against the buffer *before* handing it to the sink — the golden rule ("recompute, never trust")
turned on our own writer rather than on somebody else's manifest.

The bounds are **read from ``families/pretrain.json``** and the two algorithms
(``_longest_run_of``, ``_cap_min_distinct_by_vocab``) are **imported from the profile that enforces
them**, never re-typed. Both choices are about the same failure: a copied ``0.05`` is a value that
can drift while both sides still look correct, and per ``CLAUDE.md`` gotcha 2 a *missing* families
directory does not raise — it silently falls back to each profile's laxer constant (0.5 EOS), which
is exactly how the live corpus came to be validated at 50% EOS while declaring 5%. So
:func:`_family_decode_bounds` RAISES on a missing family file instead of falling back. A build gate
that silently loosens is worse than no build gate, because it reports success.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
It does not touch S3, and it does not know what a manifest is. ``sink`` is a
``(ShardRef, bytes) -> None`` callable, which is what lets the whole stage be tested offline with a
list and lets the Batch driver stream each shard straight into ``put_object`` without ever holding
two shards at once. It also does not import ``tokenizers`` at module scope — ``pyproject.toml``
declares only ``boto3`` and ``numpy``, so a top-level import would break the installed wheel for
the validator, which needs none of this. The real tokenizer is duck-typed on ``encode_batch``.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import os
import warnings
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

import numpy as np

from .corpus import (
    DTYPE_SIZE,
    FAMILY_MAX_EOS_FRACTION,
    MIN_DOC_TOKENS,
    MIN_MEAN_DOC_TOKENS,
    SEQ_LEN,
    SHARD_TOKENS,
    BuildError,
    Document,
    ShardRef,
)
from .profiles.base import DECODE_SAMPLE_BYTES

# Imported, not reimplemented. `_longest_run_of` is the exact vectorised run scan Gate A applies,
# and `_cap_min_distinct_by_vocab` was RECOVERED from a deployed wheel that existed in no commit
# (see its docstring) — a second implementation here would be a second thing to lose. Private
# names on purpose: `test_corpus_pack.py` asserts they still resolve, so a rename in the profile
# fails this module loudly rather than quietly disabling a gate.
from .profiles.pretrain_tokens_v1 import _cap_min_distinct_by_vocab, _longest_run_of
from .profiles.pretrain_tokens_v1 import _N_WINDOWS as _DECODE_WINDOWS

__all__ = [
    "DTYPE_LE",
    "DECODE_WINDOW_TOKENS",
    "FAMILY_FILE",
    "PackResult",
    "Sink",
    "neutralize_boundary_markers",
    "tokenize_documents",
    "pack",
    "shard_plan",
    "estimate_eos_fraction",
    "assert_eos_fraction_publishable",
]

#: The explicit little-endian uint32 dtype, spelled out everywhere bytes are produced.
#: ``np.dtype("uint32").byteorder`` is ``'='`` (native) — verified by execution — so a buffer
#: allocated as plain ``uint32`` and written with ``.tobytes()`` emits BIG-endian bytes on a
#: big-endian host while the manifest declares ``byte_order: little``. Nothing downstream names
#: that correctly either: the decode smoke test would see ids far past ``vocab_size`` and report
#: ``vocab-out-of-range``, whose message blames a wrong dtype. Allocating as ``<u4`` makes the
#: on-disk order right by construction instead of by the host's accident.
DTYPE_LE = np.dtype("<u4")

#: Literal text that a tokenizer would parse back into a SPECIAL token id, mapped to an inert
#: rewrite. Applied to document text before encoding, by :func:`neutralize_boundary_markers`.
#:
#: **This is a real corpus property, not a hypothetical.** Five of the largest train bundles failed
#: live on it — `dclm`, `finemath`, `fineweb-edu`, `stackexchange`, `stackv2-edu` — each reporting
#: a handful more EOS occurrences than documents (measured: 1, 2 and 8 extra per ~20,000-document
#: shard, so roughly 1 document in 2,500). Web-scraped text simply contains the string
#: ``<|endoftext|>``, and `tokenizers` parses it as id 100257 wherever it appears.
#:
#: Why that is fatal rather than untidy: OLMo-core recovers document boundaries with
#: ``(mmap == eos_token_id).nonzero()``, so a marker inside a document becomes a FALSE boundary.
#: The model would train on fragments split at whatever point a scraped page happened to mention
#: the token. The EOS is the only document boundary this corpus ships (§2.3, no ``.csv.gz``
#: sidecars), so there is no second signal to disambiguate against.
#:
#: **Only the boundary id is rewritten.** The tokenizer defines 22 added tokens and all 22 parse
#: from raw text (verified by execution), but the other 21 are ordinary in-vocab ids — unusual,
#: not dangerous. Rewriting them too would modify documents to fix a problem that does not exist.
#:
#: The rewrite is a SPACE SPLIT rather than a deletion. It keeps the text human-legible and
#: honest about what was there, adds no invisible characters (a zero-width space would be an
#: encoding landmine for every later reader), and deletes no content — the alternative considered
#: was `s.replace(EOS, "")`, which silently drops what the document actually said.
_BOUNDARY_MARKER_REWRITES = (("<|endoftext|>", "<| endoftext |>"),)


def neutralize_boundary_markers(text: str) -> str:
    """Rewrite literal special-token text so it cannot tokenize to the EOS boundary id.

    Cheap on the common path: :meth:`str.replace` on a string with no match returns the same object,
    and the guard below skips the call entirely for the ~2,499 documents in 2,500 that never contain
    the marker. That matters at 340M documents.

    Idempotent, which resume depends on — a re-run of a partially built bundle must produce
    byte-identical shards, and ``"<| endoftext |>"`` contains no further match to rewrite.
    """
    if "<|" not in text:  # the only prefix any rewrite starts with; one substring scan
        return text
    for literal, replacement in _BOUNDARY_MARKER_REWRITES:
        text = text.replace(literal, replacement)
    return text

#: Tokens in ONE decode-test window, derived the way ``_sampled_ids`` derives it
#: (``profiles/pretrain_tokens_v1.py:209-211``) rather than hardcoded to 4096, so a change to
#: ``DECODE_SAMPLE_BYTES`` or the window count moves this with it. Gate A pools
#: ``_DECODE_WINDOWS`` of these into one 16,384-token sample.
DECODE_WINDOW_TOKENS = max(DECODE_SAMPLE_BYTES // _DECODE_WINDOWS, DTYPE_SIZE) // DTYPE_SIZE

#: The family file whose bounds this stage must satisfy. ``pretrain``, because ``corpus.GROUP``
#: lives in a ``pretrain/...`` dataset and ``families/pretrain.json`` supplies the defaults
#: ``profiles.base._bound`` resolves at Gate A.
FAMILY_FILE = "pretrain.json"

#: Documents per ``encode_batch`` call. The Rust batch encoder amortises the FFI crossing over the
#: batch, and 1,000 documents at a ~2 KB mean is ~2 MB of text in flight — small enough that peak
#: memory stays dominated by the 100 MB shard buffer rather than by the encode queue.
_ENCODE_BATCH = 1_000

#: Fires the fork-hazard warning at most once per process. The tokenize path runs per batch over
#: hundreds of millions of documents; a warning per batch would bury the Batch log.
_warned_parallelism = False

Sink = Callable[[ShardRef, bytes], None]


# --------------------------------------------------------------------------------------
# The family bounds — resolved from the file Gate A reads, never re-typed
# --------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _family_decode_bounds() -> tuple[float, int, int]:
    """``(eos_fraction_max, zero_run_max, distinct_ids_min)`` read from ``families/pretrain.json``.

    Raises when the family directory is unresolvable. That is the opposite of Gate A's behaviour,
    and deliberately so: at validation time a missing family file degrades to the profile's laxer
    constants, which is a bug the project has already paid for (``CLAUDE.md`` gotcha 2 — the live
    corpus validated at 50% EOS while declaring 5%, because the wheel shipped without
    ``families/``). Degrading *here* would mean a packer that reports every shard clean while
    writing shards Gate A will reject, which is strictly worse than refusing to start.

    The loaded EOS bound is cross-checked against ``corpus.FAMILY_MAX_EOS_FRACTION``. That
    constant is what ``corpus.MIN_MEAN_DOC_TOKENS`` (20 == 1/0.05) and every error message in this
    module are derived from, so if the family file were edited without updating it, the arithmetic
    those messages quote would be wrong while still looking authoritative.
    """
    from .contracts import _resolve_families_dir

    path = _resolve_families_dir() / FAMILY_FILE
    try:
        doc = json.loads(path.read_bytes())
    except OSError as exc:
        raise BuildError(
            f"cannot read the family bounds from {path}: {exc}. Refusing to pack: the decode "
            f"bounds Gate A enforces live in this file, and falling back to the profile constants "
            f"would silently gate at 0.5 EOS instead of 0.05 — the exact failure that put a "
            f"corpus live validated at 50% EOS while declaring 5% (CLAUDE.md gotcha 2). Set "
            f"EDULLM_FAMILIES_DIR to a staged copy, or run from a checkout."
        ) from exc
    smoke = doc.get("defaults", {}).get("decode_smoke_test", {})
    eos_max = smoke.get("eos_fraction_max")
    zero_run = smoke.get("zero_run_max")
    distinct = smoke.get("distinct_ids_min")
    if not isinstance(eos_max, (int, float)) or not isinstance(zero_run, int) or not isinstance(distinct, int):
        raise BuildError(
            f"{path} has no usable defaults.decode_smoke_test bounds "
            f"(eos_fraction_max={eos_max!r}, zero_run_max={zero_run!r}, "
            f"distinct_ids_min={distinct!r}); these are what the build gate recomputes against."
        )
    if float(eos_max) != FAMILY_MAX_EOS_FRACTION:
        raise BuildError(
            f"{path} declares eos_fraction_max={eos_max} but corpus.FAMILY_MAX_EOS_FRACTION is "
            f"{FAMILY_MAX_EOS_FRACTION}. corpus.MIN_MEAN_DOC_TOKENS "
            f"({MIN_MEAN_DOC_TOKENS} == 1/{FAMILY_MAX_EOS_FRACTION}) and MIN_DOC_TOKENS "
            f"({MIN_DOC_TOKENS}) are derived from that constant, so the two must move together — "
            f"otherwise the read-time filter is sized for a bound that no longer exists."
        )
    return float(eos_max), int(zero_run), int(distinct)


# --------------------------------------------------------------------------------------
# Tokenize
# --------------------------------------------------------------------------------------


def tokenize_documents(
    docs: Iterable[Document | str],
    tokenizer,
    *,
    eos_id: int,
    vocab_size: int | None = None,
    batch_size: int = _ENCODE_BATCH,
    min_tokens: int | None = None,
    stats: Any = None,
) -> Iterator[np.ndarray]:
    """Yield one ``<u4`` array per input document, in input order, with EOS appended.

    **``min_tokens`` applies the short-document filter HERE, from the ids this function already
    computed.** It is optional only for the callers that predate it; the build driver must pass it.
    ``corpus_read.filter_documents`` measures the same quantity by calling ``tokenizer.encode`` once
    per document, and a driver that used both encoded the entire corpus TWICE — once one-document-
    at-a-time, which gets no rayon parallelism at all. Measured on the pinned dolma2 tokenizer over
    real prose: 1.10 M tok/s single-document versus 10.5 M tok/s for ``encode_batch`` across 32
    vCPU, so the filter pass alone was ~91% of the build's compute on 1 of 32 cores. Filtering from
    the batch's own output makes the second encode unnecessary rather than merely faster.

    The `<u4` array is only built for documents that survive, so a dropped document costs the
    encode and nothing else. `stats` is duck-typed (a `corpus_read.FilterStats`) and updated with
    the same fields that function sets, so a receipt's accounting is unchanged by the move.

    ``tokenizer`` is duck-typed, and both accepted shapes are deliberate:

    * anything with ``encode_batch`` (i.e. ``tokenizers.Tokenizer``) is called with
      **``add_special_tokens=False``** and the EOS is appended here. The library default is
      ``True``, and what it then adds depends on the tokenizer's post-processor — so accepting the
      default makes the per-document token count a property of a 4 MB JSON file nobody in this
      repo owns. Appending it ourselves makes ``1 / mean_doc_tokens`` a number this module can
      assert, which is the entire basis of the EOS gate below.
    * a plain callable ``text -> Sequence[int]``, which is what lets this stage be tested with no
      network and no ``tokenizer.json``.

    ``vocab_size`` defaults to ``tokenizer.get_vocab_size()`` when available (with added tokens,
    matching ``tokenizer_v1.derive_vocab``, which counts them because they extend the id space —
    ``profiles/tokenizer_v1.py:56-68``). Every id is asserted into ``[0, vocab_size)`` **before**
    the uint32 cast, because the cast cannot fail: measured on numpy 2.4.4, assigning
    ``np.array([-1, 5], dtype=int64)`` into a ``<u4`` buffer yields ``[4294967295, 5]`` and
    ``2**33`` yields ``0``, both silently. Gate A recomputes the same range assertion against the
    published ``tokenizer.json`` (``profiles/pretrain_tokens_v1.py:300``); catching it here costs
    two comparisons per document, and catching it there costs a re-tokenize and a re-copy.

    **EOS lives in the tokenize step, not the packer, and that has a cost worth naming:**
    re-packing this corpus at a different ``seq_len`` requires re-TOKENIZING, because the arrays
    handed to :func:`pack` already carry their boundary token. Storing documents EOS-free and
    appending at pack time would make a re-pack free. The trade is deliberate — the EOS is the only
    document boundary this corpus will ever have (§2.3 ships no ``.csv.gz`` sidecars), so it
    belongs inside the unit whose contract guarantees it, and a re-pack is a rebuild that runs
    in-region anyway (§5.7). If a re-pack sweep ever becomes routine, move the append into
    :func:`pack` and delete this paragraph rather than doing both.
    """
    if not isinstance(eos_id, int) or isinstance(eos_id, bool):
        raise BuildError(f"eos_id must be an int; got {eos_id!r}")
    if batch_size < 1:
        raise BuildError(f"batch_size must be >= 1; got {batch_size}")
    if min_tokens is not None and min_tokens < 1:
        # Mirrors filter_documents' guard verbatim: a floor of 0 admits empty documents, which
        # contribute one EOS and no content — the shape that drives the EOS fraction to 1.0.
        raise BuildError(
            f"min_tokens must be at least 1; got {min_tokens}. A floor of 0 admits empty "
            f"documents, which contribute one EOS and no content."
        )
    if stats is not None and min_tokens is not None:
        stats.min_tokens = min_tokens

    encode_batch = getattr(tokenizer, "encode_batch", None)
    if vocab_size is None:
        get_vocab_size = getattr(tokenizer, "get_vocab_size", None)
        if callable(get_vocab_size):
            vocab_size = int(get_vocab_size())
    if vocab_size is not None and not 0 <= eos_id < vocab_size:
        raise BuildError(
            f"eos_id {eos_id} is outside [0, {vocab_size}) — every shard would fail Gate A's "
            f"vocab-range assertion on its document boundaries alone"
        )
    if encode_batch is not None:
        _warn_if_parallelism_unset()

    for batch in _batched(docs, batch_size):
        # Empty documents are dropped BEFORE the encode, not after: they cost the tokenizer nothing
        # to reject and `filter_documents` counted them under their own field, which the receipt's
        # accounting reads.
        if min_tokens is not None:
            kept_batch = []
            for doc in batch:
                text = doc.text if isinstance(doc, Document) else doc
                if not text:
                    # Counted as seen HERE, because an empty document is fully accounted for the
                    # moment it is rejected — it is never yielded, so no consumer can stop short
                    # of it.
                    if stats is not None:
                        stats.seen += 1
                        stats.dropped_empty += 1
                        if len(stats.sample_dropped) < stats.sample_limit:
                            stats.sample_dropped.append(getattr(doc, "id", ""))
                    continue
                kept_batch.append(doc)
            batch = kept_batch
            if not batch:
                continue
        # Neutralize BEFORE encoding. This is the only place it can go: after the encode the
        # marker is already an id indistinguishable from the boundary this function appends.
        texts = [neutralize_boundary_markers(doc.text if isinstance(doc, Document) else doc)
                 for doc in batch]
        if encode_batch is not None:
            id_lists = [enc.ids for enc in encode_batch(texts, add_special_tokens=False)]
        else:
            id_lists = [tokenizer(text) for text in texts]
        for doc, text_ids in zip(batch, id_lists):
            # `seen` is incremented HERE, per document, and NOT in the pre-pass above. The
            # difference only shows when the consumer stops early — which `corpus_build` now does
            # by design, since `_reader_for` over-delivers on purpose. A batch is encoded whole,
            # but the generator may be abandoned partway through yielding it, so counting `seen`
            # per batch reported documents the consumer never received and broke the receipt's
            # `seen == kept + dropped_short + dropped_empty` identity (measured: 596 vs 308).
            if min_tokens is not None and stats is not None:
                stats.seen += 1
            if min_tokens is not None and len(text_ids) < min_tokens:
                # Dropped from the ids this batch already produced — no second encode, and no
                # `<u4` allocation for a document that is not going into a shard.
                if stats is not None:
                    stats.dropped_short += 1
                    stats.dropped_tokens += len(text_ids)
                    if len(stats.sample_dropped) < stats.sample_limit:
                        stats.sample_dropped.append(getattr(doc, "id", ""))
                continue
            if min_tokens is not None and stats is not None:
                stats.kept += 1
                stats.kept_tokens += len(text_ids)
            # int64 first, so the range assertion sees the values the tokenizer actually emitted
            # rather than their already-wrapped uint32 shadows.
            wide = np.fromiter(text_ids, dtype=np.int64, count=len(text_ids))
            if wide.size and (
                int(wide.min()) < 0 or (vocab_size is not None and int(wide.max()) >= vocab_size)
            ):
                raise BuildError(
                    f"tokenizer emitted an id outside [0, {vocab_size}): min {int(wide.min())}, "
                    f"max {int(wide.max())}. The uint32 cast would have hidden this — a negative "
                    f"wraps to ~4.29e9 and an id past 2**32 wraps to a valid-looking small one — "
                    f"and Gate A would then report it as a dtype/byte-order fault on a corpus that "
                    f"has already been copied."
                )
            out = np.empty(wide.size + 1, dtype=DTYPE_LE)
            out[: wide.size] = wide
            out[wide.size] = eos_id  # the ONLY document boundary this corpus will ever have
            yield out


def _batched(items: Iterable, n: int) -> Iterator[list]:
    """Chunk an iterable without materialising it — the input is a ~2.5 TB document stream."""
    batch: list = []
    for item in items:
        batch.append(item)
        if len(batch) >= n:
            yield batch
            batch = []
    if batch:
        yield batch


def _warn_if_parallelism_unset() -> None:
    """Make the caller decide about ``TOKENIZERS_PARALLELISM`` instead of deciding for them.

    The hazard is real: ``encode_batch`` uses rayon, and a process that has encoded and then forks
    can deadlock in the child. The fix is not this module's to apply, though. Writing
    ``os.environ`` at import time changes behaviour for every importer, including the validator and
    this repo's own suite — and ``"false"`` is the WRONG value for the driver doing the work, since
    it disables the batch parallelism that makes a 255B-token tokenize affordable and is needed
    only in a process that forks AFTER encoding.

    So: warn once, name both values, and let the driver choose. A driver that fans out with
    ``ProcessPoolExecutor`` and tokenizes only inside the children needs neither.
    """
    global _warned_parallelism
    if _warned_parallelism or "TOKENIZERS_PARALLELISM" in os.environ:
        return
    _warned_parallelism = True
    warnings.warn(
        "TOKENIZERS_PARALLELISM is unset. Set it deliberately in the build driver: 'true' keeps "
        "encode_batch's rayon parallelism (what you want when tokenizing IS the workload); "
        "'false' is required only in a process that forks after encoding, which otherwise "
        "deadlocks in the child.",
        RuntimeWarning,
        stacklevel=3,
    )


# --------------------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------------------


def shard_plan(
    source_targets: Mapping[tuple[str, str | None, str], int],
) -> list[tuple[str, str | None, str, int]]:
    """Turn ``{(source, domain, split): tokens}`` into ``corpus.allocate_ordinals``' plan rows.

    Rounds **down**: ``tokens // SHARD_TOKENS``. A partial shard is never planned, because
    :func:`pack`'s tail rule writes a short final shard only when a stream *underruns* its last
    ref. Planning a partial shard up front would instead guarantee a short shard on every stream —
    turning the rare "< 8,192 tokens dropped" case into a routine one and making the tail the
    normal path rather than the exception.

    A stream that yields zero shards is REFUSED, not skipped, and the refusal names the shortfall.
    Skipping is the worse failure and it is silent: ``allocate_ordinals`` would emit no ordinals for
    that stream, :func:`pack` would find no destination for its documents, and the corpus would
    publish clean with a whole source missing while the README's mixture weights still named it.
    """
    rows: list[tuple[str, str | None, str, int]] = []
    for (source, domain, split), tokens in sorted(
        source_targets.items(), key=lambda kv: (kv[0][0], kv[0][1] or "", kv[0][2])
    ):
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
            raise BuildError(
                f"{source}/{split}: token count must be a non-negative int, got {tokens!r}"
            )
        n_shards = tokens // SHARD_TOKENS
        if n_shards == 0:
            raise BuildError(
                f"{source}/{domain or '-'}/{split}: {tokens:,} tokens yields zero shards — short "
                f"by {SHARD_TOKENS - tokens:,} of the {SHARD_TOKENS:,} one shard needs "
                f"({tokens / SHARD_TOKENS:.1%} of a shard). Raise this stream's target, merge it "
                f"into a sibling source, or drop it from the plan explicitly. It is refused rather "
                f"than skipped because a skipped stream gets no ordinals, so pack() would find no "
                f"destination for its documents and the corpus would publish clean with a whole "
                f"source missing."
            )
        rows.append((source, domain, split, n_shards))
    return rows


# --------------------------------------------------------------------------------------
# The EOS-fraction gate — §3.3, and the reason the synthetic half is publishable at all
# --------------------------------------------------------------------------------------


def estimate_eos_fraction(mean_doc_tokens: float) -> float:
    """``1 / mean_doc_tokens`` — a packed shard's EOS fraction, computable at design time.

    Exact rather than approximate, and that is the point: one EOS per document and no padding means
    the number of EOS ids in a shard IS the number of documents that end in it, so the fraction is
    fully determined by the mean document length. No tokenizer, corpus, or shard size appears in
    the formula, which is why it can be evaluated while sources are still being chosen —
    ``corpus.MIN_MEAN_DOC_TOKENS`` is precisely the inverse of the family bound (``1/20 == 0.05``).
    """
    if mean_doc_tokens <= 0:
        raise BuildError(f"mean_doc_tokens must be positive; got {mean_doc_tokens}")
    return 1.0 / mean_doc_tokens


def assert_eos_fraction_publishable(
    stream: str,
    documents: int,
    tokens: int,
    *,
    max_eos_fraction: float = FAMILY_MAX_EOS_FRACTION,
) -> float:
    """RAISE if a stream's realized mean document length puts its EOS fraction over the bound.

    The **realized** mean, not the planned one — a plan cannot be wrong about this, only a corpus
    can. §3.3's sampled FinePhrase rewrite (the whole document being *"Question: Can light
    accelerate to the speed of light?"*, ~12 tokens → 0.083) is the case this exists for. Filtering
    below ``corpus.MIN_DOC_TOKENS`` at read time is what makes the synthetic half publishable at
    all, and this is the check that proves the filter actually ran.
    """
    if documents <= 0:
        raise BuildError(
            f"{stream}: cannot compute a mean document length over {documents} documents"
        )
    mean = tokens / documents
    fraction = estimate_eos_fraction(mean)
    if fraction > max_eos_fraction:
        raise BuildError(
            f"{stream}: {documents:,} documents / {tokens:,} tokens = {mean:.2f} tokens per "
            f"document, so the EOS fraction is 1/{mean:.2f} = {fraction:.4f} — over the family "
            f"bound of {max_eos_fraction:.4f} (families/pretrain.json -> "
            f"decode_smoke_test.eos_fraction_max). Every shard of this stream would be rejected "
            f"with 'eos-fraction-out-of-bounds' AFTER the tokenize and the upload. The mean must "
            f"be at least {MIN_MEAN_DOC_TOKENS} tokens (1/{MIN_MEAN_DOC_TOKENS} = "
            f"{1 / MIN_MEAN_DOC_TOKENS:.4f}); drop documents under corpus.MIN_DOC_TOKENS "
            f"({MIN_DOC_TOKENS}) at read time, which leaves a {MIN_DOC_TOKENS / MIN_MEAN_DOC_TOKENS:.1f}x "
            f"margin."
        )
    return fraction


# --------------------------------------------------------------------------------------
# Pack
# --------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PackResult:
    """What one stream actually produced — the input to the manifest, replacing the plan.

    The driver **cannot** build a manifest without reading :attr:`written`, because a tail shard's
    realized token count differs from its planned one and ``manifest.verify_arithmetic`` recomputes
    ``count.value * 4 == bytes``. That is deliberate: it makes :attr:`unfilled` impossible to
    overlook, where a plan-shaped return value would let a stream that came in a whole shard short
    pass unnoticed.
    """

    stream: tuple[str, str | None, str]
    #: Refs actually written, with ``tokens`` set to the REALIZED count (the tail may be short).
    written: tuple[ShardRef, ...]
    #: Planned refs the stream had no data for. Data, not an error — see :func:`pack`.
    unfilled: tuple[ShardRef, ...]
    documents: int
    tokens_in: int
    tokens_out: int
    #: Sub-``SEQ_LEN`` remainder truncated off the final short shard. At most ``SEQ_LEN - 1``.
    tail_dropped: int
    #: Tokens still in the stream after every ref was filled — the ``shard_plan`` round-down residue.
    surplus_dropped: int
    #: Highest EOS fraction over any ``DECODE_WINDOW_TOKENS`` window of any shard written.
    max_eos_fraction: float

    def __post_init__(self) -> None:
        # THE conservation identity, asserted at runtime and not only in the suite. Every token read
        # is written, dropped by the documented tail rule, or dropped as round-down surplus; there
        # is no fourth channel. A packer loses tokens QUIETLY by construction — a `del buf[:n]` that
        # runs one iteration too many looks identical to one that does not — so the arithmetic has
        # to be checked rather than reviewed. This is the runtime counterpart of the suite's
        # token-conservation test; neither replaces the other, because the test proves the identity
        # on cases we thought of and this proves it on every case in production.
        expected = self.tokens_out + self.tail_dropped + self.surplus_dropped
        if expected != self.tokens_in:
            raise BuildError(
                f"{self.stream}: token conservation FAILED — {self.tokens_in:,} in, but "
                f"{self.tokens_out:,} out + {self.tail_dropped:,} tail + "
                f"{self.surplus_dropped:,} surplus = {expected:,} "
                f"({self.tokens_in - expected:+,} unaccounted). This is a packer bug, not a data "
                f"problem: the carry buffer dropped or duplicated tokens."
            )
        if self.tail_dropped >= SEQ_LEN:
            raise BuildError(
                f"{self.stream}: tail_dropped is {self.tail_dropped:,}, which is >= SEQ_LEN "
                f"({SEQ_LEN}). The tail rule truncates to the nearest whole sequence, so its "
                f"remainder cannot reach one — a whole sequence was dropped instead."
            )


def pack(
    streams: Mapping[tuple[str, str | None, str], Iterable[np.ndarray]],
    refs: Sequence[ShardRef],
    *,
    sink: Sink,
    eos_id: int | None = None,
    vocab_size: int | None = None,
    max_eos_fraction: float | None = None,
    partial_source: bool = False,
) -> list[PackResult]:
    """Concatenate tokenized documents into shards of exactly ``ref.tokens`` and hand them to ``sink``.

    ``streams`` maps a ``(source, domain, split)`` triple to that stream's documents in order;
    ``refs`` is the whole group's allocation from ``corpus.allocate_ordinals``. ``max_eos_fraction``
    defaults to the family file's value and may only TIGHTEN it, mirroring
    ``profiles.base._bound``'s clamp — a caller cannot loosen a family bound from a keyword
    argument, because then the bound would be decoration.

    **The carry buffer spans documents and input files.** A document straddling a shard boundary is
    split, and that is correct: FSL training concatenates and re-chunks regardless (§2.2), so the
    split costs one sequence's worth of cross-document attention and nothing else. The bug to avoid
    is dropping the remainder, so a partially-consumed document is held as an offset into the same
    array and resumed in the next shard. Note that a stream is a FLAT iterable and this function
    has no concept of an input file — the caller chains them
    (``itertools.chain.from_iterable``). That makes a per-file carry reset *unrepresentable* rather
    than merely avoided, which matters because the per-file reset is the natural shape of the bug:
    one packer per input file, each silently discarding its own sub-shard remainder.

    **The tail rule, and its arithmetic.** Only the last shard of a stream can be short, and only
    when the realized stream underruns its final ref. It is TRUNCATED DOWN to a whole multiple of
    ``SEQ_LEN``::

        aligned = cursor - (cursor % SEQ_LEN)         # SEQ_LEN 8192, stride 32,768 bytes
        worst-case loss = SEQ_LEN - 1 = 8,191 tokens = 32,764 bytes
                        = 0.0328 % of one 25,001,984-token shard
                        = 4.8e-5 % of a 255B corpus across ~15 streams

    Truncation, not padding, for a mechanical reason rather than an aesthetic one. Padding invents
    up to 8,191 tokens the tokenizer never emitted; zero-padding then leaves a zero run of up to
    8,191 and ``check_decode_smoke`` rejects a run ``>= 256``
    (``profiles/pretrain_tokens_v1.py:367``). EOS-padding stays inside every bound but writes
    thousands of fake document boundaries into the one signal OLMo-core uses to find real ones. A
    shard whose whole content is under one sequence is not written at all — ``aligned == 0`` there,
    and OLMo-core's instance count ``file_size // (item_size * seq_len)`` FLOORS to zero, so the
    object would hold data no reader can reach while an empty file passes every size and checksum
    gate before failing ``check_decode_smoke`` with ``empty-shard`` after the upload.

    **Why an unfilled ref is data and a surplus is fatal.** They look symmetric and are not. An
    unfilled ref costs nothing: ordinal gaps are legal (``allocate_ordinals``' docstring — nothing
    in ``validate.py`` checks contiguity), no data is lost, and the caller writes no manifest entry
    for it. A surplus DISCARDS tokens that were already read and tokenized, so
    :func:`_drain_surplus` refuses it once it reaches a whole shard's worth.

    ``eos_id`` upgrades the per-shard EOS gate from exact-under-contract to a recompute from the
    bytes. Without it the fraction is ``documents_ending_here / tokens``, exact only if every input
    array carries exactly one EOS as :func:`tokenize_documents` guarantees; with it, the two are
    cross-checked and a disagreement raises. That cross-check is what catches "the tokenizer never
    appended an EOS at all", which is otherwise undetectable in a finished corpus.
    """
    if not callable(sink):
        raise BuildError("sink must be a callable (ShardRef, bytes) -> None")

    family_eos, zero_run_max, distinct_min = _family_decode_bounds()
    if max_eos_fraction is None:
        max_eos_fraction = family_eos
    elif max_eos_fraction > family_eos:
        raise BuildError(
            f"max_eos_fraction={max_eos_fraction} is LOOSER than the family bound {family_eos} "
            f"(families/pretrain.json). A keyword argument may tighten a family bound and never "
            f"loosen it — the same clamp profiles.base._bound applies at Gate A, for the same "
            f"reason: a bound a caller can widen in one line is not a bound. Edit the family file "
            f"if this really should change for everyone."
        )

    by_stream: dict[tuple[str, str | None, str], list[ShardRef]] = {}
    for ref in refs:
        _assert_ref_alignable(ref)
        by_stream.setdefault((ref.source, ref.domain, ref.split), []).append(ref)
    for stream_refs in by_stream.values():
        # allocate_ordinals returns ascending order, but a caller may hand back a filtered or
        # concatenated list; ordinal order is what makes "shards == max - min + 1" true by eye.
        stream_refs.sort(key=lambda r: r.ordinal)

    orphan_refs = sorted(str(s) for s in set(by_stream) - set(streams))
    orphan_streams = sorted(str(s) for s in set(streams) - set(by_stream))
    if orphan_refs or orphan_streams:
        raise BuildError(
            f"plan and data disagree about which streams exist: refs with no documents "
            f"{orphan_refs}, documents with no refs {orphan_streams}. A stream with no refs has "
            f"nowhere to go and would be dropped in full; refs with no documents leave an empty "
            f"ordinal block. Both mean shard_plan() ran on a different set than the reader yielded."
        )

    results: list[PackResult] = []
    for stream in sorted(streams, key=lambda s: (s[0], s[1] or "", s[2])):
        results.append(
            _pack_stream(
                stream,
                streams[stream],
                by_stream[stream],
                sink=sink,
                eos_id=eos_id,
                vocab_size=vocab_size,
                max_eos_fraction=max_eos_fraction,
                zero_run_max=zero_run_max,
                distinct_min=distinct_min,
                partial_source=partial_source,
            )
        )
    return results


def _assert_ref_alignable(ref: ShardRef) -> None:
    """A ref whose ``tokens`` is not a positive multiple of ``SEQ_LEN`` cannot be written.

    ``allocate_ordinals`` always produces ``SHARD_TOKENS`` (3052 x 8192), so this only fires on a
    hand-built ref — but it fires HERE rather than at Gate A, where the same fact costs the copy.
    """
    if ref.tokens <= 0 or ref.tokens % SEQ_LEN != 0:
        stride = DTYPE_SIZE * SEQ_LEN
        raise BuildError(
            f"{ref.path}: ref.tokens is {ref.tokens}, not a positive multiple of SEQ_LEN "
            f"({SEQ_LEN}). check_seq_len_alignment recomputes bytes % {stride} from a live head "
            f"and would reject this object with {(ref.tokens * DTYPE_SIZE) % stride} bytes over "
            f"the last full sequence."
        )


def _pack_stream(
    stream: tuple[str, str | None, str],
    docs: Iterable[np.ndarray],
    stream_refs: Sequence[ShardRef],
    *,
    sink: Sink,
    eos_id: int | None,
    vocab_size: int | None,
    max_eos_fraction: float,
    zero_run_max: int,
    distinct_min: int,
    partial_source: bool = False,
) -> PackResult:
    """Fill one stream's refs in ordinal order from a single carry buffer."""
    label = "/".join(part for part in stream if part)
    doc_iter = iter(docs)
    pending: tuple[np.ndarray, int] | None = None  # (document, tokens already consumed)

    written: list[ShardRef] = []
    unfilled: list[ShardRef] = []
    documents = tokens_in = tokens_out = tail_dropped = 0
    worst_eos = 0.0
    exhausted = False

    for ref in stream_refs:
        if exhausted:
            unfilled.append(ref)
            continue  # before the allocation: never reserve 100 MB for a shard with no data
        # Exact-size preallocation, explicitly little-endian. The buffer IS the payload, so its
        # length cannot drift from ref.tokens and its byte order cannot depend on the host.
        buf = np.empty(ref.tokens, dtype=DTYPE_LE)
        cursor = 0
        docs_ending = 0
        while cursor < ref.tokens:
            if pending is None:
                doc = next(doc_iter, None)
                if doc is None:
                    exhausted = True
                    break
                _assert_document_dtype(doc, label)
                if doc.size == 0:
                    # A zero-length array carries no EOS, so it is not a document. Counting it
                    # would deflate the mean document length the EOS gate is computed from — i.e.
                    # inflate the apparent EOS fraction — and skipping it silently is right
                    # because tokenize_documents cannot produce one (it always appends EOS).
                    continue
                pending = (doc, 0)
                documents += 1
                tokens_in += int(doc.size)
            arr, offset = pending
            take = min(arr.size - offset, ref.tokens - cursor)
            buf[cursor : cursor + take] = arr[offset : offset + take]
            cursor += take
            if offset + take == arr.size:
                pending = None
                docs_ending += 1  # a document's EOS is its last token, so it landed in THIS shard
            else:
                pending = (arr, offset + take)  # resumes in the next shard; nothing dropped

        if cursor == 0:
            unfilled.append(ref)
            continue
        if cursor == ref.tokens:
            emit_ref, emit = ref, buf
        else:
            aligned = cursor - (cursor % SEQ_LEN)
            if aligned == 0:
                # Under one whole sequence. Emitting it would write an object no reader can reach
                # (instance count floors to 0) that Gate A then rejects as `empty-shard`.
                tail_dropped += cursor
                unfilled.append(ref)
                continue
            if eos_id is not None:
                # Truncation can discard a document's EOS along with its tail, so the
                # one-EOS-per-document identity below is stated over the EMITTED region only.
                docs_ending -= int(np.count_nonzero(buf[aligned:cursor] == eos_id))
            tail_dropped += cursor - aligned
            # `tokens` is the REALIZED count, not the planned one, so count.value * 4 == bytes holds.
            emit_ref, emit = dataclasses.replace(ref, tokens=aligned), buf[:aligned]

        worst_eos = max(
            worst_eos,
            _verify_shard(
                emit,
                path=emit_ref.path,
                docs_ending=docs_ending,
                eos_id=eos_id,
                vocab_size=vocab_size,
                max_eos_fraction=max_eos_fraction,
                zero_run_max=zero_run_max,
                distinct_min=distinct_min,
            ),
        )
        sink(emit_ref, emit.tobytes())
        written.append(emit_ref)
        tokens_out += int(emit.size)
        del buf, emit  # 100 MB each: hold at most one shard at a time (the streaming promise)

    # `pending_left` is already inside `tokens_in` (the whole document was counted when it was
    # pulled), while `unread` never entered it. Adding the total would double-count the remainder of
    # a half-consumed document — which is exactly the bug PackResult's conservation assertion caught
    # on its first run, over-reporting tokens_in by up to one document.
    pending_left, unread = _drain_surplus(
        label, doc_iter, pending, len(stream_refs), partial_source=partial_source
    )
    tokens_in += unread
    surplus = pending_left + unread

    # The stream-level gate, on top of the per-shard one. A stream that wrote a single shard can
    # satisfy that shard's windows and still be a short-document corpus overall, and the mean over
    # the whole stream is what §3.3 is about.
    if documents:
        assert_eos_fraction_publishable(
            label, documents, tokens_in, max_eos_fraction=max_eos_fraction
        )

    return PackResult(
        stream=stream,
        written=tuple(written),
        unfilled=tuple(unfilled),
        documents=documents,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tail_dropped=tail_dropped,
        surplus_dropped=surplus,
        max_eos_fraction=worst_eos,
    )


def _assert_document_dtype(doc: np.ndarray, label: str) -> None:
    """Refuse anything but a 4-byte unsigned array, because the alternative is a silent wrap.

    ``buf[a:b] = int64_array`` does not raise on values outside uint32 — measured on numpy 2.4.4,
    ``-1`` becomes ``4294967295`` and ``2**33`` becomes ``0``. Checking the dtype once per document
    is O(1) and closes the channel entirely, where checking values would redo per token what
    :func:`tokenize_documents` already did.
    """
    if not isinstance(doc, np.ndarray) or doc.dtype.kind != "u" or doc.dtype.itemsize != DTYPE_SIZE:
        got = getattr(doc, "dtype", type(doc).__name__)
        raise BuildError(
            f"{label}: documents must be {DTYPE_SIZE}-byte unsigned numpy arrays (uint32); got "
            f"{got}. A signed or wider array assigned into the shard buffer WRAPS silently, so an "
            f"out-of-vocab id would reach S3 looking like a valid small id."
        )


def _drain_surplus(
    label: str,
    doc_iter: Iterator[np.ndarray],
    pending: tuple[np.ndarray, int] | None,
    n_refs: int,
    *,
    partial_source: bool = False,
) -> tuple[int, int]:
    """``(pending_left, unread)`` — the tokens left over after every ref was filled.

    The split is not cosmetic, and getting it wrong is a conservation bug rather than a reporting
    one. ``pending_left`` is the unconsumed remainder of a document the caller has ALREADY added to
    ``tokens_in`` (it counts a whole document when it pulls one), whereas ``unread`` is documents
    never pulled at all. Returning only the sum makes the caller either double-count the remainder
    or miss the unread documents — there is no single number that is correct for both.

    **``partial_source`` decides whether leftover data is an ERROR or the POINT**, and the two
    cases are genuinely different builds rather than a strictness preference:

    * ``False`` (default) — the stream is meant to be consumed WHOLE, so tokens left after the last
      ref mean the plan allocated too few refs for the data that exists. Discarding them silently
      would ship a corpus short of what its own source contains, so this raises.
    * ``True`` — the plan deliberately draws a SUBSET. The reservoir registry takes 252 B tokens
      from a 1,094 B-token pool, and `corpus_build._reader_for` over-delivers on purpose
      (``_CHARS_PER_TOKEN`` 6.0 against a measured ~4.4, times ``_FILTER_HEADROOM`` 1.5) so that
      filter attrition cannot leave the final shard unfilled. That overshoot IS surplus. Raising on
      it turns a working build into a guaranteed end-of-run failure — measured: 25 of 27 bundles,
      each after its full billable work, and only ``ubuntu-irc`` survived because its pool is 1.04x
      its target so the reader ran out of FILES before the budget bound.

    When ``partial_source`` is set the iterator is NOT drained: unread documents are left unpulled
    and reported as ``unread == 0``. That keeps ``PackResult``'s conservation identity exact, since
    ``tokens_in`` only ever counted documents actually pulled — and it avoids walking the remaining
    terabytes just to produce a number nobody acts on.

    Raising after shards have already reached the sink is safe: the sink writes to
    ``edullm-landing``, which is scratch with a 14-day expiry, and nothing has been promoted.
    """
    pending_left = 0
    if pending is not None:
        arr, offset = pending
        pending_left = int(arr.size - offset)
    if partial_source:
        # Stop pulling. The remaining stream is the part of the pool this bundle was never meant
        # to take, and counting it would cost a full read of data that is about to be ignored.
        return pending_left, 0
    unread = 0
    for doc in doc_iter:
        unread += int(doc.size)
        surplus = pending_left + unread
        if surplus >= SHARD_TOKENS:
            raise BuildError(
                f"{label}: at least {surplus:,} tokens remain after all {n_refs} planned shards "
                f"were filled — a whole {SHARD_TOKENS:,}-token shard's worth would be discarded. "
                f"The plan under-allocated: recompute shard_plan() from the realized token count "
                f"and re-run. (A surplus under one shard is normal — shard_plan rounds down.) "
                f"If this stream is a deliberate SUBSET of a larger pool, the caller should pass "
                f"partial_source=True instead of shrinking the plan."
            )
    return pending_left, unread


# --------------------------------------------------------------------------------------
# The per-shard recompute — the family's own bounds, on the buffer, before the sink
# --------------------------------------------------------------------------------------


def _verify_shard(
    ids: np.ndarray,
    *,
    path: str,
    docs_ending: int,
    eos_id: int | None,
    vocab_size: int | None,
    max_eos_fraction: float,
    zero_run_max: int,
    distinct_min: int,
) -> float:
    """Recompute Gate A's decode bounds on the bytes about to be written. Returns the EOS fraction.

    Three of ``check_decode_smoke``'s four bounds are recomputed here, and the fourth is
    deliberately not:

    * **EOS fraction** — over the worst ``DECODE_WINDOW_TOKENS`` window, not over the whole shard.
      Gate A pools ``_N_WINDOWS`` windows at seeded offsets into one 16,384-token sample and
      divides, and a shard averaging 0.049 can still contain a 4,096-token window at 0.08. The seed
      is ``sha256(group.rng_seed : path)`` and ``rng_seed`` belongs to a dataset that does not exist
      yet at pack time, so the offsets cannot be reproduced — but the MAXIMUM over all windows of
      that size upper-bounds any pool of them, which makes passing here *sufficient* for Gate A
      rather than merely necessary.
    * **Zero run** — the whole-shard longest run, via the profile's own ``_longest_run_of``. Any run
      a window sees is part of a whole-shard run, so the whole-shard figure bounds every window.
    * **Distinct ids** — over one pooled sample of ``_N_WINDOWS`` evenly-spaced windows, with the
      identical ``effective_min`` formula and vocab cap Gate A applies. This is the one bound
      sampled rather than computed exhaustively: ``np.unique`` over a whole 25M-token shard measures
      0.33 s, i.e. ~57 min over 10,400 shards. Fixed offsets differ from Gate A's seeded ones, so
      this is a strong smoke test, not a proof — hence the error message says so.
    * **Vocab range** — NOT rechecked, and that is not an omission. :func:`tokenize_documents`
      already asserts every id against the same ``vocab_size`` before the uint32 cast, and nothing
      between there and here can change a value: the shard buffer is ``<u4`` and
      :func:`_assert_document_dtype` refuses anything that is not already ``<u4``, which is exactly
      what makes that claim true. Re-scanning 25M ids per shard to confirm a property already
      enforced upstream is the decoration the golden rule warns about — the check that earns its
      cost is the dtype guard.
    """
    n = int(ids.size)
    if n == 0:  # unreachable through _pack_stream, which never emits an empty shard
        raise BuildError(f"{path}: refusing to write a zero-token shard")

    if eos_id is not None:
        counted = int(np.count_nonzero(ids == eos_id))
        # THE cross-check. `docs_ending` is exact only under tokenize_documents' one-EOS-per-
        # document guarantee; `counted` is a fact about the bytes. A mismatch means the EOS was
        # never appended (counted == 0 over thousands of documents) or that a real token collides
        # with the boundary id — and a corpus with no EOS has NO document boundary anywhere, cannot
        # be given one after tokenization, and passes every other check ever written.
        if counted != docs_ending:
            raise BuildError(
                f"{path}: {docs_ending} documents end in this shard but id {eos_id} appears "
                f"{counted} times in its bytes. One EOS per document is what makes the EOS "
                f"fraction — and the document boundary OLMo-core recovers with "
                f"`(mmap == eos_token_id).nonzero()` — mean anything. "
                + (
                    "The tokenizer emitted no EOS at all: this corpus would have no document "
                    "boundaries, and that is unrecoverable after tokenization."
                    if counted == 0
                    else "Either a document carries more than one EOS, or a real token collides "
                    "with the boundary id."
                )
            )
        whole_shard = counted / n
        if whole_shard > max_eos_fraction:
            # Checked before the sliding-window pass, which is also what BOUNDS that pass's memory:
            # it allocates per EOS hit, so a degenerate all-EOS shard is rejected here rather than
            # 800 MB later.
            _raise_eos(path, whole_shard, max_eos_fraction, f"whole shard ({n:,} tokens)")
        eos_fraction = _max_window_fraction(ids == eos_id, DECODE_WINDOW_TOKENS)
        scope = f"worst {DECODE_WINDOW_TOKENS}-token window"
    else:
        # Exact under contract (one EOS per document, none invented by padding) but a whole-shard
        # average, so it cannot bound a window. Pass eos_id to close that gap.
        eos_fraction = docs_ending / n
        scope = "whole shard — pass eos_id for the per-window bound Gate A actually applies"
    if eos_fraction > max_eos_fraction:
        _raise_eos(path, eos_fraction, max_eos_fraction, scope)

    zero_run = _longest_run_of(ids, 0)
    # `>=`, matching profiles/pretrain_tokens_v1.py:367 exactly. A run of exactly zero_run_max is a
    # violation there, so a build gate on `>` would pass shards Gate A rejects.
    if zero_run >= zero_run_max:
        raise BuildError(
            f"{path}: {zero_run} consecutive zero ids (limit {zero_run_max}, and Gate A's "
            f"comparison is >=, not >). This is the signature of a partial zero-fill — an "
            f"uninitialised buffer region, or an array allocated but never written. Note it is a "
            f"RUN, not a fraction: dolma2 maps '!' to id 0, so scattered zeros are normal prose."
        )

    sample = _pooled_sample(ids, DECODE_WINDOW_TOKENS, _DECODE_WINDOWS)
    distinct = int(np.unique(sample).size)
    # The same size-scaling Gate A applies (profiles/pretrain_tokens_v1.py:329): an absolute floor
    # is unsatisfiable for a sample smaller than the floor.
    capped = _cap_min_distinct_by_vocab(distinct_min, vocab_size)
    effective_min = min(capped, max(sample.size // 4, 2 if sample.size > 1 else 1))
    if distinct < effective_min:
        raise BuildError(
            f"{path}: only {distinct} distinct ids across {sample.size} sampled tokens (need >= "
            f"{effective_min}) — the signature of an all-zeros or all-one-token shard. Sampled at "
            f"fixed evenly-spaced offsets, where Gate A samples seeded ones, so it may land "
            f"elsewhere and still reject. If this is a small-vocab corpus, pass vocab_size so the "
            f"floor is capped the way Gate A caps it (_cap_min_distinct_by_vocab)."
        )
    return eos_fraction


def _raise_eos(path: str, fraction: float, bound: float, scope: str) -> None:
    raise BuildError(
        f"{path}: EOS fraction {fraction:.4f} over the {scope} exceeds the family bound "
        f"{bound:.4f} (families/pretrain.json -> decode_smoke_test.eos_fraction_max). That implies "
        f"a mean document length of 1/{fraction:.4f} = {1 / fraction:.1f} tokens; it must be at "
        f"least {MIN_MEAN_DOC_TOKENS}. Drop documents under corpus.MIN_DOC_TOKENS "
        f"({MIN_DOC_TOKENS}) at read time (§3.3)."
    )


def _max_window_fraction(hits: np.ndarray, window: int) -> float:
    """Highest fraction of ``True`` over any contiguous ``window`` of ``hits``.

    Sliding, not tiled: non-overlapping windows can miss a spike straddling a boundary by up to 2x,
    and the spike IS the failure — a run of very short documents inside an otherwise healthy shard.

    Computed over the hit POSITIONS rather than a cumulative sum, which is a memory decision. A
    ``cumsum`` over a 25M-token shard costs two ~100 MB int32 temporaries on top of the 100 MB
    buffer, times ``copy_workers``. The positions cost 8 bytes per EOS, and the caller has already
    rejected any shard whose whole-shard fraction exceeds the bound, so ``m <= 0.05n`` here — ~1.25M
    hits, ~40 MB of temporaries. The maximum is always attained by a window whose left edge sits on
    a hit (sliding right until it does can only add hits), so scanning hit-aligned windows is exact,
    not an approximation.
    """
    n = int(hits.size)
    positions = np.flatnonzero(hits)
    m = int(positions.size)
    if m == 0:
        return 0.0
    if n <= window:
        return m / n
    # For each hit k at position p: how many hits lie in [p, p + window).
    ends = np.searchsorted(positions, positions + window, side="left")
    counts = ends - np.arange(m, dtype=ends.dtype)
    return float(counts.max()) / window


def _pooled_sample(ids: np.ndarray, window: int, n_windows: int) -> np.ndarray:
    """``n_windows`` evenly-spaced windows of ``window`` tokens, concatenated.

    Mirrors the SHAPE of ``_sampled_ids``' pool (``profiles/pretrain_tokens_v1.py:212-220``) so the
    distinct-ids floor is applied to a sample of the size Gate A will use — a floor of 256 against a
    16,384-token sample is a different test than against a 25M-token one. The offsets are fixed
    rather than seeded because the seed depends on a ``rng_seed`` that does not exist yet.
    """
    n = int(ids.size)
    if n <= window * n_windows:
        return ids
    stride = (n - window) // max(n_windows - 1, 1)
    return np.concatenate([ids[i * stride : i * stride + window] for i in range(n_windows)])
