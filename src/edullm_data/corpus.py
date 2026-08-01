"""The build-time contract shared by every stage that turns documents into token shards.

This module is the boundary between the four stages of §5.6 phase 1 — read, carve, tokenize,
pack — and it exists so those stages can be written and tested independently without agreeing
on anything at runtime. It is **pure**: no AWS, no HTTP, no tokenizer, no numpy at import time.
Everything here is a dataclass, a constant, or a pure function of its arguments.

WHY A CONTRACT MODULE AT ALL. The stages are naturally parallel work, and the two ways they can
silently disagree are both expensive:

* **Shard ordinals.** Ordinals are five digits inside the object key, the key is inside
  ``entry.path``, and ``entry.path`` is hashed into ``manifest_sha256``. Two workers that each
  count from zero produce ``tokens/dclm/train-00000`` and ``tokens/finewiki/train-00000``, which
  both parse fine (verified: ``parse_shard_name`` returns ``('train', 0)`` for each) — so nothing
  rejects them, and the collision is only visible to a human reading two keys side by side. The
  fix is to allocate from a **plan** computed before any worker starts, never from a runtime
  counter. :func:`allocate_ordinals` is that plan.
* **The token budget per shard.** A shard whose length is not a whole multiple of ``seq_len``
  is REJECTED by ``pretrain_tokens_v1.check_seq_len_alignment`` — but only when the group
  declares ``seq_len``, and omitting it makes the check pass vacuously. So the alignment has to
  be a build-time invariant, not something the validator is trusted to catch.

Everything in here is a number or rule that was checked against the code that enforces it, and
each one carries the citation. Where a bound comes from ``families/pretrain.json`` rather than a
profile default, the family value is used — ``profiles.base._bound`` only ever TIGHTENS, so the
family is the binding constraint and the profile constant is the laxer fallback that applies
when ``families/`` is missing (which is its own historical failure; see ``CLAUDE.md`` gotcha 2).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence

__all__ = [
    "SHARD_TOKENS",
    "SEQ_LEN",
    "SHARDS_PER_FILE_TOKENS",
    "DTYPE",
    "DTYPE_SIZE",
    "EXTENSION",
    "GROUP",
    "TOKENIZER_DATASET_ID",
    "FAMILY_MAX_EOS_FRACTION",
    "MIN_MEAN_DOC_TOKENS",
    "MIN_DOC_TOKENS",
    "VAL_FRACTION",
    "Document",
    "CorpusSpec",
    "ShardRef",
    "BuildError",
    "shard_key",
    "allocate_ordinals",
    "is_held_out",
    "carve",
    "epochs_for",
    "epoch_verdict",
]


class BuildError(RuntimeError):
    """A build-time invariant was violated.

    Deliberately NOT a ``PublishError``: these fire before any byte reaches landing, which is
    the entire point of putting them here. A shard that is wrong at pack time is free to fix; the
    same shard discovered at Gate A has already cost a copy and a hash of the whole corpus.
    """


# --------------------------------------------------------------------------------------
# Shard geometry — §2.2
# --------------------------------------------------------------------------------------

#: Sequence length the corpus is packed for. Not a free parameter: OLMo-core computes instance
#: count as ``file_size // (item_size * seq_len)``, which FLOORS, so a shard holding fewer than
#: one full sequence yields ZERO instances and is silently skipped rather than erroring.
SEQ_LEN = 8192

#: Tokens per shard: ``3052 * 8192``. Two independent constraints pick this number.
#:
#: 1. It must be an exact multiple of :data:`SEQ_LEN`, or ``check_seq_len_alignment``
#:    (``profiles/pretrain_tokens_v1.py:426``) rejects the object — it recomputes
#:    ``bytes % (dtype_size * seq_len)`` from a real ``head`` and requires zero.
#: 2. ~100 MB / ~10,400 objects is the finest granularity whose Gate A pass (~1.4 h) still fits
#:    the 7200 s validator timeout with margin. §2.2 measured p90 mixture error at 0.6% @20%
#:    weight and 2.4% @5%; halving to 10M tokens triples Gate A to 3.45 h and BREAKS the
#:    deployed validator, as well as doubling OLMo-core's per-sample linear path scan.
SHARD_TOKENS = 3052 * SEQ_LEN

#: Alias kept because "tokens per file" is what the packer's loop variable means locally, and
#: reading ``SHARD_TOKENS`` there invites the reader to wonder whether a shard is a file.
SHARDS_PER_FILE_TOKENS = SHARD_TOKENS

DTYPE = "uint32"
DTYPE_SIZE = 4
EXTENSION = ".u32le.bin"

#: One group holds the whole corpus. NOT a stylistic choice: ``read.build_mixture`` is scoped to
#: a single group, so real and synthetic sources split across two groups could never appear in
#: one mixture — which is the corpus's entire purpose. Realness is fused into the ``source``
#: segment instead (§1.1).
GROUP = "tokens"

#: The published tokenizer this corpus pins. Verified live in ``s3://edullm-data/tokenizer/``:
#: ``dolma2-bpe/v1`` exists with a real 4.0 MiB ``tokenizer.json``. ``publish()`` pins it by
#: ``manifest_sha256`` and the validator DERIVES ``vocab_size``/``eos_token_id`` from those
#: bytes — so nothing here types a vocab size, on purpose.
TOKENIZER_DATASET_ID = "tokenizer/dolma2-bpe"


# --------------------------------------------------------------------------------------
# The EOS-fraction floor — a build constraint nobody had written down
# --------------------------------------------------------------------------------------

#: ``families/pretrain.json`` → ``decode_smoke_test.eos_fraction_max``. **0.05, not the
#: profile's 0.5.** ``profiles.base._bound`` only tightens, so the family wins wherever
#: ``families/`` is resolvable — and where it is NOT, the corpus is validated at 0.5 while
#: claiming 0.05, which is exactly how the live corpus once shipped (``CLAUDE.md`` gotcha 2).
FAMILY_MAX_EOS_FRACTION = 0.05

#: **The consequence, which is load-bearing and was implicit until now.** One EOS per document
#: means the EOS fraction of a packed shard IS ``1 / mean_doc_tokens``. The decode smoke test
#: samples a 65,536-byte window = 16,384 uint32 tokens and rejects the shard when more than
#: 5% of them are EOS. So:
#:
#:     mean_doc_tokens < 20  ==>  eos_fraction > 0.05  ==>  eos-fraction-out-of-bounds
#:
#: Computed, not asserted: at a mean of 20 tokens the fraction is exactly 0.0500; at 16 it is
#: 0.0625 and the shard is REJECTED. This is the check that would have caught the two 20-byte
#: shards in the 150B corpus, and it is the check a corpus of very short documents trips.
#:
#: ⚠️ **This is why §3.3's FinePhrase quality-control note is a hard gate, not advice.** A
#: sampled FinePhrase rewrite was the entire string *"Question: Can light accelerate to the
#: speed of light?"* — about 12 tokens. A shard packed from documents like that averages well
#: under 20 tokens per document and is rejected AFTER the tokenize and the upload. Filtering
#: short documents at read time is therefore not a quality preference; it is what makes the
#: synthetic half publishable at all.
MIN_MEAN_DOC_TOKENS = 20

#: Per-document floor. Set well above :data:`MIN_MEAN_DOC_TOKENS` because a *mean* of 20 is the
#: failure threshold, not a safe operating point — a distribution whose mean sits exactly on the
#: limit fails half its shards. At a 64-token floor the worst possible shard mean is 64, giving
#: an EOS fraction of 0.0156, a 3.2x margin under the family bound.
#:
#: Documents shorter than this are DROPPED, not padded and not concatenated: padding invents
#: tokens the tokenizer never emitted, and concatenating two documents into one loses the
#: boundary that the EOS marks.
MIN_DOC_TOKENS = 64


# --------------------------------------------------------------------------------------
# Held-out — §1.4
# --------------------------------------------------------------------------------------

#: Share of each source's DOCUMENTS held out. Per-source rather than global so that a mixture
#: drawing 3% from one source still has held-out data for it; a global carve would leave small
#: sources with no val shards at all.
#:
#: 0.5% of ~255B is ~1.3B held-out tokens, ~51 val shards. Large enough that every source gets
#: at least one whole 25M-token shard: the smallest pool is reference at 9B, whose 0.5% is 45M
#: tokens = 1.8 shards.
VAL_FRACTION = 0.005


@dataclass(frozen=True)
class Document:
    """One document, as every reader must yield it.

    The reader's ONLY job is to produce these. Everything downstream — carve, tokenize, pack —
    consumes this shape and nothing else, which is what lets a new corpus be added by writing a
    reader and a registry row rather than by touching the pipeline.

    ``id`` is the upstream document id, kept as text because it is the join key for the §9.7
    item 4 partition and the FineWeb-Edu anti-join, both of which hash it
    (``reservoir_ids.partition_of``, ``ingest_reservoir.IdSet``). It must be stable upstream: a
    row index would make the partition non-reproducible across a re-download.

    ``domain`` is ``None`` for the majority of sources and that is CORRECT, not missing data —
    §1.2's rule is that a source gets a domain segment if and only if it SHIPS one upstream. A
    flat key legally yields ``{"source": ...}`` alone.
    """

    id: str
    text: str
    source: str
    domain: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise BuildError("Document.id is empty; it is the join key for the id partition")
        if not self.source:
            raise BuildError(f"Document {self.id!r} has no source; source is a path segment")


@dataclass(frozen=True)
class CorpusSpec:
    """One row of the source registry: everything a reader needs and nothing it doesn't.

    ``text_column`` is an exact parquet ``path_in_schema``, NOT a top-level column name, and
    this is the single most dangerous field in the module. FinePhrase's rewrite lives at
    ``rollout_results.list.element.text`` while the top-level ``text`` holds the ORIGINAL
    FineWeb-Edu document — so a flat leaf scan finds ``text`` twice and ``.names.index("text")``
    returns the wrong one. Ingesting it builds a corpus of unrephrased FineWeb-Edu labelled
    synthetic, and **no hash, size, or decode check catches that**: the bytes are real text,
    tokenized correctly, in valid ids. Only matching the exact ``path_in_schema`` prevents it.

    ``target_tokens`` is the pool target from §2.1/§3.2, not the upstream size. ``pool_tokens``
    is what upstream actually holds, MEASURED under dolma2 where we have a measurement — card
    figures are not comparable (§3.1: most name no tokenizer, and every Common Pile "token"
    figure is ``Size(GB) x 0.25``, pure arithmetic).
    """

    key: str
    category: str
    #: The ``source`` path segment, already slugged and already fused with realness (§1.1).
    source_label: str
    repo: str
    file_format: str
    text_column: str
    id_column: str
    target_tokens: int
    #: Upstream pool size. ``None`` where we have no measurement we trust — better than a card
    #: figure, which would look like evidence.
    pool_tokens: int | None = None
    config: str | None = None
    revision: str | None = None
    #: Upstream field to INHERIT as the ``domain`` segment, or ``None`` for a flat source.
    #: Never a classifier: an inherited value is the upstream publisher's own statement of fact
    #: (§1.2), which is the golden rule applied to labels — prefer the fact over the inference.
    domain_column: str | None = None
    license: str = "unknown"
    share_alike: bool = False
    traps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        from .manifest import SAFE_SEGMENT_RE

        if not SAFE_SEGMENT_RE.match(self.source_label):
            raise BuildError(
                f"{self.key}: source_label {self.source_label!r} is not a safe path segment "
                f"(must match {SAFE_SEGMENT_RE.pattern!r}). An unsafe segment is not caught "
                f"anywhere downstream — 'C#' in a key sends everything after the '#' into the "
                f"URI fragment, so the shard name vanishes from the path, and both "
                f"labels_from_path and fnmatch accept it happily."
            )
        if self.target_tokens <= 0:
            raise BuildError(f"{self.key}: target_tokens must be positive")
        if self.pool_tokens is not None and self.pool_tokens < self.target_tokens:
            raise BuildError(
                f"{self.key}: pool is {self.pool_tokens} tokens but the target asks for "
                f"{self.target_tokens}. Reduce the target or add a source; drawing more than "
                f"a pool holds means repeating documents, which the epoch guard exists to flag."
            )


@dataclass(frozen=True)
class ShardRef:
    """Where one shard goes, and what it must contain.

    Produced by :func:`allocate_ordinals` before any worker runs, so two workers can never pick
    the same key. ``tokens`` is the exact count the packer must write — not a maximum — because
    the alignment invariant is per-file.
    """

    source: str
    domain: str | None
    split: str
    ordinal: int
    tokens: int = SHARD_TOKENS

    @property
    def path(self) -> str:
        """The manifest-relative path, i.e. ``entry.path``."""
        return shard_key(self.source, self.domain, self.split, self.ordinal)


_ORDINAL_MAX = 99_999


def shard_key(source: str, domain: str | None, split: str, ordinal: int) -> str:
    """Build one shard's manifest-relative path.

    Centralised because the path IS the label: ``labels_from_path`` reads ``source`` and
    ``domain`` back out of exactly these segments, Gate A recomputes them and rejects a
    mismatch, and the result is hashed into ``manifest_sha256``. A path assembled ad hoc in two
    places is a label bug waiting to happen.

    Raises when the ordinal will not fit five digits — ``SHARD_RE`` requires exactly five, so a
    six-digit ordinal does not parse as a shard at all, which means ``parse_shard_name`` returns
    ``None`` and the object silently stops being split-checked.
    """
    if not 0 <= ordinal <= _ORDINAL_MAX:
        raise BuildError(
            f"ordinal {ordinal} does not fit the mandatory five digits (0..{_ORDINAL_MAX}). "
            f"A six-digit name fails SHARD_RE, so parse_shard_name returns None and the object "
            f"is no longer recognised as belonging to any split."
        )
    middle = f"{source}/{domain}/" if domain else f"{source}/"
    return f"{GROUP}/{middle}{split}-{ordinal:05d}{EXTENSION}"


def allocate_ordinals(
    plan: Sequence[tuple[str, str | None, str, int]],
) -> list[ShardRef]:
    """Assign globally-unique ordinals across the whole group, up front.

    ``plan`` is ``(source, domain, split, n_shards)`` tuples. The return is every shard the
    build will write, in a deterministic order.

    **Why global and not per-source.** Ordinals are unique per (split, source) only if you trust
    every writer to scope its counter the same way, and the failure is invisible: verified by
    execution, ``parse_shard_name`` returns ``('train', 0)`` for both
    ``tokens/dclm/train-00000.u32le.bin`` and ``tokens/finewiki/train-00000.u32le.bin``, and
    nothing in ``validate.py`` compares ordinals across sources (grepped: no contiguity, gap, or
    uniqueness check exists — gaps are legal). So reuse is not rejected; it just makes two
    distinct shards share a name in every log, report and error message a human will read.
    Allocating globally costs nothing and removes the class.

    Ordinals are dense and ascending in plan order. Gaps would be legal but are avoided so that
    the count of shards in a source is ``max - min + 1``, which is what a person checks by eye.

    Sorting is by the tuple, NOT by input order, so two callers that build the same plan in a
    different order get byte-identical keys — and therefore the same ``manifest_sha256``. Without
    that, a rerun that happened to enumerate sources differently would produce a different
    dataset identity from the same data.
    """
    seen: set[tuple[str, str | None, str]] = set()
    for source, domain, split, _n in plan:
        stream = (source, domain, split)
        if stream in seen:
            raise BuildError(
                f"plan names {stream} twice; merge the two entries. Two plan rows for one "
                f"stream would each get their own ordinal block, so the shard count for that "
                f"source could not be read off its ordinals."
            )
        seen.add(stream)

    refs: list[ShardRef] = []
    ordinal_by_split: dict[str, int] = {}
    for source, domain, split, n_shards in sorted(plan, key=lambda t: (t[0], t[1] or "", t[2])):
        if n_shards < 0:
            raise BuildError(f"{source}/{split}: n_shards is negative ({n_shards})")
        nxt = ordinal_by_split.get(split, 0)
        for i in range(n_shards):
            refs.append(ShardRef(source=source, domain=domain, split=split, ordinal=nxt + i))
        ordinal_by_split[split] = nxt + n_shards
    return refs


# --------------------------------------------------------------------------------------
# Held-out selection — §1.4, and the one bug this project has already shipped
# --------------------------------------------------------------------------------------


def is_held_out(doc_id: str, source: str, *, fraction: float = VAL_FRACTION) -> bool:
    """Decide held-out membership from the DOCUMENT ID alone.

    **This is the fix for a bug this project has already shipped.** A previously published
    corpus had six held-out shards that were byte-copies of train shards — 100% leakage — and
    Gate A caught only five of the six, by content digest, and only because it was one group.
    A digest check is the last line of defence, not the mechanism.

    Two properties make leakage structurally impossible rather than merely unlikely:

    * **It is a pure function of the id.** No shuffle, no RNG, no ordering, no state. The same
      document lands in the same split on every run, on every machine, in any order, at any
      concurrency — so a document cannot be drawn into both halves by two workers that disagree
      about a random seed. There is nothing to seed.
    * **It is decided BEFORE tokenizing** (§1.4). Sampling a val split out of the shuffled token
      pool afterwards is not a val split; the boundary has to exist while documents still do.

    ``source`` is mixed into the hash so that the held-out *documents* differ per source rather
    than being the same hash-slice of id space everywhere. If a document id appeared in two
    sources (it should not, but lineage overlap is real — §3.1), a source-blind function would
    hold it out of both, correlating the two val sets.

    Uses the counter-mode-SHA-256 house pattern (``read._shuffle_key``,
    ``profiles.base.sample_offsets``): no PRNG object, so the result is a pure function of its
    inputs across processes and Python versions.
    """
    if not 0.0 <= fraction < 1.0:
        raise BuildError(f"fraction must be in [0, 1); got {fraction}")
    if fraction == 0.0:
        return False
    digest = hashlib.sha256(f"heldout|{source}|{doc_id}".encode()).digest()
    # 53 bits: the full mantissa of a float64, so the comparison below is exact rather than
    # rounding a 256-bit integer through a float and skewing the boundary.
    bucket = int.from_bytes(digest[:7], "big") & ((1 << 53) - 1)
    return bucket < fraction * (1 << 53)


def carve(
    docs: Iterable[Document], *, fraction: float = VAL_FRACTION
) -> Iterator[tuple[str, Document]]:
    """Tag each document ``("train", doc)`` or ``("val", doc)``.

    A generator, so a 2.5 TB stream is never materialised. Callers route on the tag rather than
    building two lists — the whole point of carving at the document level is that neither half
    has to fit in memory.
    """
    for doc in docs:
        yield ("val" if is_held_out(doc.id, doc.source, fraction=fraction) else "train", doc)


# --------------------------------------------------------------------------------------
# The epoch guard — §4.3 / §6 item 2
# --------------------------------------------------------------------------------------

#: Green / amber / red / hard-fail boundaries on repetition, from Muennighoff et al.
#: (arXiv:2305.16264): R_D* ~= 15.39 is where the return on a repeated token has largely gone,
#: and past ~40 epochs "repeating is worthless".
_EPOCH_AMBER = 4.0
_EPOCH_RED = 16.0
_EPOCH_FAIL = 40.0


def epochs_for(total: int, ratio: float, available: int) -> float:
    """``N * w / S`` — how many times a component's data is seen in one run.

    Trivial arithmetic, named because the failure it catches is not obvious: **narrow selection,
    not cross-corpus overlap, is the real repetition risk.** At the §2.1 pool sizes every
    category lands at 0.33-0.50 epochs, deep green. The guard exists for the teammate who
    narrows a 20B run to one small source — a 5B source at ``w=1.0`` is exactly 4.0 epochs, and
    a 1B source is 20.
    """
    if available <= 0:
        raise BuildError("available must be positive to compute epochs")
    return (total * ratio) / available


def epoch_verdict(epochs: float) -> str:
    """``"green" | "amber" | "red" | "fail"`` for an epoch count."""
    if epochs > _EPOCH_FAIL:
        return "fail"
    if epochs > _EPOCH_RED:
        return "red"
    if epochs > _EPOCH_AMBER:
        return "amber"
    return "green"
