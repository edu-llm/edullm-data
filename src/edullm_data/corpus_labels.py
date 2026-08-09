"""The per-bundle ``labels/`` sidecar — one record per document that reached the packer.

This is the artifact that makes a curriculum possible over a corpus that ships no text. Four
fields per document, and each one exists because the chunk-mapping in ``corpus_order`` cannot be
computed without it:

* ``source_doc`` — the document's position in its bundle's tokenization order, ``0..N-1``.
* ``n_tokens`` — CONTENT tokens, **excluding the EOS**. ``n_tokens + 1`` is the document's stride
  through the stream's token space, which is what turns a document list into token offsets.
* ``source_path`` — the upstream file the document was read from, via an interned table.
* ``mtld`` — the difficulty score (:mod:`corpus_mtld`).

WHY A PACKED BINARY RECORD AND NOT PARQUET OR JSONL
---------------------------------------------------
1.206 billion documents, so the per-document cost IS the design. MEASURED sizes for one record:

===============  =========  ==============  =================================================
form             per doc    1.206 B docs    note
===============  =========  ==============  =================================================
JSONL            ~120 B     ~145 GB         ``source_path`` repeated verbatim on every line
parquet          ~14-20 B   ~17-24 GB       needs pyarrow in the reader; a footer to corrupt
**this**         **14 B**   **16.9 GB**     ``np.frombuffer`` in one call, no dependency
===============  =========  ==============  =================================================

The record is a numpy structured dtype, explicitly ``align=False`` so there is **no padding** and
the on-disk width is the arithmetic sum of the fields — the same reason the token shards are raw
``<u4``: a width that a platform or a library version can change is a width nobody can verify.

``source_path`` is interned into a JSON header rather than stored per record because a bundle reads
at most ~100 files (``hf_files(spec)[i::K]``), so a per-record ``uint16`` id costs 2 bytes against
~60 for the string. That is the whole difference between 17 GB and 145 GB.

⚠️ **A uint16 path id caps a bundle at 65,536 distinct files.** MEASURED against the shipping
registry: the largest file count is ``stackv2-edu`` at 95 files over 7 parts. :func:`write_labels`
REFUSES past the cap rather than wrapping, because a wrapped id is a confident wrong provenance
claim on a document — the class of failure this repo exists to prevent.

WHAT IS RECOMPUTED, STATED PLAINLY (``fsck``'s disclosure discipline)
---------------------------------------------------------------------
:func:`verify_labels` recomputes, from the bytes:

* ``len(records) * ITEM_SIZE == len(payload)`` — the record count against the real byte length.
* ``source_doc == arange(N)`` — contiguity, elementwise. Stored rather than derived from the row
  index precisely so this can FAIL; deriving it would make the check a tautology, which is the
  ``ShardReceipt.bytes`` argument restated.
* ``sum(n_tokens + 1) == tokens_in`` — **the conservation identity, and see the correction below.**
* every ``path_id`` in range of the header's table.
* ``np.isfinite(mtld).all()`` — a NaN sorts unpredictably and an ``inf`` takes rank 0 or N-1 by
  accident of comparison order, so a non-finite score is a silently wrong curriculum.

🔴 **CORRECTION TO THE HANDOFF'S CONSERVATION CHECK.** The brief asks for
``sum(n_tokens + 1) == shard tokens``. That is **FALSE BY CONSTRUCTION** on this pipeline and would
fail every bundle. ``corpus_pack.PackResult``'s own identity is::

    tokens_in == tokens_out + tail_dropped + surplus_dropped

``tokens_out`` IS "shard tokens". ``tail_dropped`` is the sub-``SEQ_LEN`` remainder truncated off a
short final shard, and ``surplus_dropped`` under ``partial_source=True`` is ``pending_left`` — the
unconsumed remainder of the last document pack pulled, which is nonzero whenever the last document
straddles the end of the last ref, i.e. **normally**. So the checkable identity is against
``tokens_in``, and ``tokens_out`` is a PREFIX of the labelled token space. That prefix property is
what :mod:`corpus_order` relies on, and it is stated there too.

Checking against ``tokens_out`` instead would have been the ``_drain_surplus`` failure again: a
gate that fires on healthy bundles at end-of-run, after full billable work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from .corpus import BuildError
from .corpus_mtld import MTLD_SPEC_ID

__all__ = [
    "LABELS_SCHEMA",
    "LABEL_DTYPE",
    "ITEM_SIZE",
    "MAX_PATHS",
    "LabelSet",
    "DocumentLabel",
    "labels_key",
    "encode_labels",
    "decode_labels",
    "verify_labels",
]

#: Versioned for the reason ``RECEIPT_SCHEMA_VERSION`` is: the checks below are an interpretation
#: of these fields. A reader applying v1 rules to a v2 record would report confidently on a width
#: it does not know — and here the width is *literally* the parse, so a wrong interpretation
#: silently shifts every field of every document.
LABELS_SCHEMA = "edullm-doc-labels/v1"

#: The on-disk record. ``align=False`` is EXPLICIT, not the default being relied on: with
#: ``align=True`` numpy pads this to 16 bytes and every offset past the first field moves.
LABEL_DTYPE = np.dtype(
    [
        ("source_doc", "<u4"),
        ("n_tokens", "<u4"),
        ("mtld", "<f4"),
        ("path_id", "<u2"),
    ],
    align=False,
)

#: 14. Asserted at import rather than trusted, because the whole file's parse depends on it and a
#: numpy that padded differently would produce a plausible, wrongly-shifted read of 17 GB.
ITEM_SIZE = LABEL_DTYPE.itemsize
if ITEM_SIZE != 14:  # pragma: no cover - a broken numpy, not a code path
    raise BuildError(
        f"LABEL_DTYPE.itemsize is {ITEM_SIZE}, not 14 — numpy padded a dtype declared "
        f"align=False. Every field offset past source_doc has moved and every sidecar written by "
        f"this build would be unparseable by any other."
    )

#: ``uint16`` path ids. Refused past this rather than wrapped: id 0 for file 65,536 is a confident
#: wrong provenance claim, not a lost one.
MAX_PATHS = 1 << 16

#: ``float32`` for ``mtld``, deliberately. MTLD is used only to RANK, its cross-implementation
#: reproducibility is nowhere near 7 significant digits, and float64 would cost 4.8 GB more across
#: the corpus. ⚠️ The consequence is stated rather than hidden: float32 has ~7 decimal digits, so
#: two documents whose true scores differ by less than that collapse to a TIE — which
#: ``corpus_order`` resolves by ``global_chunk_idx``, a total order, so the permutation stays
#: deterministic. Ties are not a defect here; a nondeterministic tie-break would be.


@dataclass(frozen=True)
class DocumentLabel:
    """One document's label. The in-memory form; :data:`LABEL_DTYPE` is the on-disk one."""

    source_doc: int
    n_tokens: int
    source_path: str
    mtld: float


@dataclass(frozen=True)
class LabelSet:
    """One bundle's labels: the header facts plus the record array.

    ``records`` is a ``LABEL_DTYPE`` structured array, not a list of :class:`DocumentLabel` — at
    6.5 M documents per bundle a list of frozen dataclasses is ~1.5 GB of Python objects against
    91 MB of array. The dataclass form exists for tests and for a caller inspecting a handful.
    """

    schema: str
    plan_id: str
    bundle_id: str
    source: str
    domain: str | None
    split: str
    mtld_spec: str
    paths: tuple[str, ...]
    #: The bundle's ``PackResult.tokens_in`` — what ``sum(n_tokens + 1)`` must equal. Carried so
    #: the conservation check needs nothing but this object; see the module docstring's correction.
    tokens_in: int
    records: np.ndarray

    @property
    def documents(self) -> int:
        return int(self.records.size)


def labels_key(prefix: str, plan_id: str, bundle_id: str) -> str:
    """Where one bundle's labels live.

    Under the plan's own prefix, alongside ``_receipts/``, because a labels file is evidence about
    exactly one ``plan_id``'s document stream and is meaningless against another. Routed through
    ``_assert_safe_key`` for the same reason every other build artifact is: the basenames that
    would fire ``edullm-landing-manifest-created`` must never be writable by a build.
    """
    from .ingest_reservoir import _assert_safe_key

    return _assert_safe_key(f"{prefix.strip('/')}/{plan_id}/_labels/{bundle_id}.labels")


def encode_labels(labels: LabelSet) -> bytes:
    """``LabelSet`` -> the object body: a length-prefixed JSON header, then the packed records.

    Layout, chosen so the header can be read without the 91 MB body (a ranged GET of the first
    64 KiB is enough to recover the path table and the counts)::

        bytes 0..7    b"EDULLBL1"                     magic, so a wrong object fails loudly
        bytes 8..11   uint32 LE header_len
        bytes 12..    header_len bytes of UTF-8 JSON
        then          documents * 14 bytes of LABEL_DTYPE records

    A magic rather than trusting the key: the sidecar and the token shards live under the same
    plan prefix, and a token shard fed to :func:`decode_labels` would otherwise parse as ~7.1 M
    plausible records instead of raising.
    """
    if len(labels.paths) >= MAX_PATHS:
        raise BuildError(
            f"{labels.bundle_id}: {len(labels.paths)} distinct source files exceeds MAX_PATHS "
            f"({MAX_PATHS}) — the per-record path id is uint16 and would WRAP, silently attributing "
            f"documents to the wrong upstream file. The shipping registry's largest source is 100 "
            f"files, so this means the reader changed, not that the cap is too low."
        )
    if labels.records.dtype != LABEL_DTYPE:
        raise BuildError(
            f"{labels.bundle_id}: records dtype is {labels.records.dtype!r}, not LABEL_DTYPE. A "
            f"different width writes a file no reader of this schema can parse — and it parses as "
            f"garbage rather than failing, because there is no per-record checksum."
        )
    header = {
        "schema": labels.schema,
        "plan_id": labels.plan_id,
        "bundle_id": labels.bundle_id,
        "stream": {"source": labels.source, "domain": labels.domain, "split": labels.split},
        "mtld_spec": labels.mtld_spec,
        "item_size": ITEM_SIZE,
        "documents": labels.documents,
        "tokens_in": labels.tokens_in,
        "paths": list(labels.paths),
    }
    body = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    return b"EDULLBL1" + len(body).to_bytes(4, "little") + body + labels.records.tobytes()


def decode_labels(raw: bytes) -> LabelSet:
    """The inverse of :func:`encode_labels`. Raises on anything it cannot parse exactly.

    Structural failures RAISE rather than returning violations, the same line
    ``corpus_receipt.Receipt.from_dict`` draws: there is nothing useful to report about fields that
    could not be read. Semantic failures — a broken conservation identity, a non-contiguous
    ``source_doc`` — are :func:`verify_labels`' business and come back as Violations.
    """
    if len(raw) < 12 or raw[:8] != b"EDULLBL1":
        raise BuildError(
            f"not a labels object: expected magic b'EDULLBL1', got {raw[:8]!r}. A token shard has "
            f"no magic and would otherwise parse as millions of plausible records."
        )
    header_len = int.from_bytes(raw[8:12], "little")
    if 12 + header_len > len(raw):
        raise BuildError(
            f"labels header claims {header_len} bytes but the object holds only {len(raw) - 12} "
            f"past the prefix — truncated."
        )
    header = json.loads(raw[12 : 12 + header_len].decode())
    if header.get("schema") != LABELS_SCHEMA:
        raise BuildError(
            f"labels object declares schema {header.get('schema')!r}; this reader understands "
            f"{LABELS_SCHEMA!r}. Refusing to interpret an unknown record width rather than "
            f"reporting confidently on a wrongly-shifted parse."
        )
    if int(header.get("item_size", -1)) != ITEM_SIZE:
        raise BuildError(
            f"labels object declares item_size {header.get('item_size')!r} but this reader's "
            f"LABEL_DTYPE is {ITEM_SIZE} bytes. Every field offset would be wrong."
        )
    payload = raw[12 + header_len :]
    if len(payload) % ITEM_SIZE != 0:
        raise BuildError(
            f"labels payload is {len(payload)} bytes, not a multiple of {ITEM_SIZE}; the record "
            f"array is ragged, so the last record is truncated."
        )
    records = np.frombuffer(payload, dtype=LABEL_DTYPE)
    stream = header.get("stream") or {}
    return LabelSet(
        schema=str(header["schema"]),
        plan_id=str(header.get("plan_id", "")),
        bundle_id=str(header.get("bundle_id", "")),
        source=str(stream.get("source", "")),
        domain=stream.get("domain"),
        split=str(stream.get("split", "")),
        mtld_spec=str(header.get("mtld_spec", "")),
        paths=tuple(str(p) for p in header.get("paths", []) or []),
        tokens_in=int(header.get("tokens_in", 0)),
        records=records,
    )


def verify_labels(labels: LabelSet, *, declared_documents: int | None = None) -> list[Any]:
    """Falsify a labels sidecar's claims from its own bytes. Returns ``profiles.base.Violation``\\ s.

    Every check here recomputes; none asserts presence. ``declared_documents`` is the receipt's
    ``PackResult.documents``, when the caller holds it — a cross-artifact comparison, which is the
    only kind that can catch a labels file grafted onto the wrong bundle's receipt.
    """
    from .profiles.base import Violation

    v: list[Violation] = []
    r = labels.records
    n = int(r.size)

    if labels.mtld_spec != MTLD_SPEC_ID:
        v.append(
            Violation(
                "labels-mtld-spec-mismatch",
                f"{labels.bundle_id}: labels were computed under mtld_spec "
                f"{labels.mtld_spec!r} but this build's spec is {MTLD_SPEC_ID!r}. Two arms whose "
                f"difficulty labels came from different functions are not comparable, and nothing "
                f"downstream can see it: both are floats in a plausible range and both produce a "
                f"bijective permutation.",
            )
        )

    if n == 0:
        v.append(
            Violation(
                "labels-empty",
                f"{labels.bundle_id}: the labels sidecar holds no records. Every document that "
                f"reached the packer has a label, so an empty sidecar for a bundle that wrote "
                f"shards means the labels were not collected — and a curriculum built over it "
                f"would silently omit this bundle's chunks from the ordering.",
            )
        )
        return v

    # CONTIGUITY, elementwise. Stored rather than derived exactly so this can fail.
    expected = np.arange(n, dtype=r["source_doc"].dtype)
    if not np.array_equal(r["source_doc"], expected):
        bad = int(np.flatnonzero(r["source_doc"] != expected)[0])
        v.append(
            Violation(
                "labels-source-doc-not-contiguous",
                f"{labels.bundle_id}: source_doc is not 0..{n - 1}; record {bad} holds "
                f"{int(r['source_doc'][bad])}. The chunk-owner lookup is a searchsorted over "
                f"CUMULATIVE (n_tokens + 1), which assumes record order IS document order — a gap "
                f"or a repeat shifts every document after it and silently reassigns their chunks.",
            )
        )

    # THE CONSERVATION IDENTITY. int64 accumulation: sum over 6.5 M uint32 strides overflows
    # uint32 at ~4.3 B tokens, and a bundle holds up to 107 B.
    total = int(np.add(r["n_tokens"].astype(np.int64), 1).sum())
    if total != labels.tokens_in:
        v.append(
            Violation(
                "labels-token-conservation-broken",
                f"{labels.bundle_id}: sum(n_tokens + 1) over {n:,} labels is {total:,} but the "
                f"bundle's PackResult.tokens_in is {labels.tokens_in:,} "
                f"({total - labels.tokens_in:+,}). Every document the packer pulled contributes "
                f"exactly its content tokens plus one EOS; there is no fourth channel. Note this "
                f"is checked against tokens_in and NOT against shard tokens — tokens_out is a "
                f"PREFIX of this space, short by tail_dropped + surplus_dropped.",
            )
        )

    if labels.paths:
        worst = int(r["path_id"].max())
        if worst >= len(labels.paths):
            v.append(
                Violation(
                    "labels-path-id-out-of-range",
                    f"{labels.bundle_id}: a record claims path_id {worst} against a table of only "
                    f"{len(labels.paths)} paths. The provenance of those documents is unrecoverable, "
                    f"and a uint16 that WRAPPED would instead point confidently at the wrong file.",
                )
            )
    else:
        v.append(
            Violation(
                "labels-no-path-table",
                f"{labels.bundle_id}: the header carries no path table, so every record's "
                f"source_path is unresolvable. source_path is one of the four fields the chunk "
                f"mapping's provenance rests on.",
            )
        )

    finite = np.isfinite(r["mtld"])
    if not bool(finite.all()):
        n_bad = int(np.count_nonzero(~finite))
        v.append(
            Violation(
                "labels-mtld-not-finite",
                f"{labels.bundle_id}: {n_bad:,} of {n:,} mtld scores are NaN or inf. A NaN sorts "
                f"unpredictably (every comparison is False) and an inf takes rank 0 or rank N-1 by "
                f"accident of comparison order, so the curriculum would be wrong in a way the "
                f"permutation check cannot see — a bijection over garbage is still a bijection.",
            )
        )
    if bool((r["mtld"] < 0).any()):
        v.append(
            Violation(
                "labels-mtld-negative",
                f"{labels.bundle_id}: at least one mtld score is negative. MTLD is a ratio of a "
                f"non-negative length to a positive factor count, so a negative value means the "
                f"field was misread — most likely a record-width or byte-order error, which "
                f"shifts every other field too.",
            )
        )
    if bool((r["n_tokens"] == 0).any()):
        n_zero = int(np.count_nonzero(r["n_tokens"] == 0))
        v.append(
            Violation(
                "labels-zero-token-document",
                f"{labels.bundle_id}: {n_zero:,} labels claim 0 content tokens. "
                f"tokenize_documents drops empty documents before the encode and the length floor "
                f"is min_doc_tokens (64 on this corpus), so a zero here means the label was "
                f"recorded for a document the packer never received.",
            )
        )

    if declared_documents is not None and n != declared_documents:
        v.append(
            Violation(
                "labels-document-count-mismatch",
                f"{labels.bundle_id}: {n:,} labels against the receipt's "
                f"PackResult.documents={declared_documents:,}. The label is appended when the "
                f"packer PULLS a document, so these count the same event — a difference means the "
                f"two artifacts describe different runs.",
            )
        )
    return v


def build_label_set(
    rows: Sequence[DocumentLabel],
    *,
    plan_id: str,
    bundle_id: str,
    stream: tuple[str, str | None, str],
    tokens_in: int,
    paths: Sequence[str] | None = None,
) -> LabelSet:
    """Assemble a :class:`LabelSet` from :class:`DocumentLabel` rows. **Tests and small callers.**

    ``run_bundle`` does NOT use this — it accumulates straight into typed arrays, because
    materialising 6.5 M frozen dataclasses per bundle is ~1.5 GB of Python objects against 91 MB
    of records, inside a container the ledger sizes at 14,336 MiB with a dedup set already in it.
    """
    table: list[str] = list(paths) if paths is not None else []
    index = {p: i for i, p in enumerate(table)}
    recs = np.zeros(len(rows), dtype=LABEL_DTYPE)
    for i, row in enumerate(rows):
        pid = index.get(row.source_path)
        if pid is None:
            pid = len(table)
            index[row.source_path] = pid
            table.append(row.source_path)
        recs[i] = (row.source_doc, row.n_tokens, row.mtld, pid)
    return LabelSet(
        schema=LABELS_SCHEMA,
        plan_id=plan_id,
        bundle_id=bundle_id,
        source=stream[0],
        domain=stream[1],
        split=stream[2],
        mtld_spec=MTLD_SPEC_ID,
        paths=tuple(table),
        tokens_in=tokens_in,
        records=recs,
    )


class LabelCollector:
    """Accumulates one bundle's labels into typed arrays as the packer pulls documents.

    **Why this is a class with a callback and not a generator wrapper.** The label must be recorded
    for exactly the documents ``corpus_pack.pack`` actually PULLED — pack stops as soon as its
    planned shards are full and does not drain the iterator (``partial_source=True``; MEASURED:
    offered 200,015 documents it pulled 50,264). A generator that appended *after* its yield would
    miss the last document; one that appended *before* records exactly the pulled set, because a
    generator's body runs only when ``next()`` is called. So the hook goes immediately before
    ``tokenize_documents``' ``yield``, and this object is what it writes into.

    ⚠️ **``source_path`` must travel WITH the document, never be read from a shared "current file"
    variable.** ``tokenize_documents`` batches 1,000 documents, so by the time pack pulls record 1
    of a batch the reader has advanced ~1,000 documents ahead and may be in the NEXT file. A shared
    mutable path would mislabel provenance at every file boundary — silently, since both paths are
    real files of the same source.

    Growth is amortised doubling on ``numpy`` arrays rather than Python lists: at 6.5 M documents
    per bundle a list of ints is ~250 MB against 26 MB for the same values as ``uint32``.
    """

    __slots__ = ("_n", "_cap", "_n_tokens", "_mtld", "_path_id", "_paths", "_index")

    def __init__(self, *, capacity: int = 1 << 16) -> None:
        self._n = 0
        self._cap = max(1, capacity)
        self._n_tokens = np.zeros(self._cap, dtype="<u4")
        self._mtld = np.zeros(self._cap, dtype="<f4")
        self._path_id = np.zeros(self._cap, dtype="<u2")
        self._paths: list[str] = []
        self._index: dict[str, int] = {}

    def __len__(self) -> int:
        return self._n

    def add(self, *, n_tokens: int, source_path: str, mtld: float) -> None:
        """Record one document. ``source_doc`` is this call's index — a counter, by construction.

        The counter is the whole reason ``source_doc`` needs no reconstruction pass: we are already
        in tokenization order, so position IS identity. It is still WRITTEN to the record so
        :func:`verify_labels` can contradict it.
        """
        pid = self._index.get(source_path)
        if pid is None:
            pid = len(self._paths)
            if pid >= MAX_PATHS:
                raise BuildError(
                    f"more than {MAX_PATHS} distinct source files in one bundle; the per-record "
                    f"path id is uint16 and the next one would WRAP onto file 0, attributing "
                    f"documents to the wrong upstream file with no error."
                )
            self._index[source_path] = pid
            self._paths.append(source_path)
        if self._n == self._cap:
            self._grow()
        i = self._n
        self._n_tokens[i] = n_tokens
        self._mtld[i] = mtld
        self._path_id[i] = pid
        self._n = i + 1

    def _grow(self) -> None:
        self._cap *= 2
        self._n_tokens = np.resize(self._n_tokens, self._cap)
        self._mtld = np.resize(self._mtld, self._cap)
        self._path_id = np.resize(self._path_id, self._cap)

    def finish(
        self,
        *,
        plan_id: str,
        bundle_id: str,
        stream: tuple[str, str | None, str],
        tokens_in: int,
    ) -> LabelSet:
        """The immutable :class:`LabelSet`. ``source_doc`` is materialised as ``arange(n)`` here.

        Materialising it (rather than leaving it implicit) is what gives
        ``labels-source-doc-not-contiguous`` something to falsify — and it costs 26 MB per bundle
        against a check that would otherwise be decoration.
        """
        n = self._n
        recs = np.zeros(n, dtype=LABEL_DTYPE)
        recs["source_doc"] = np.arange(n, dtype="<u4")
        recs["n_tokens"] = self._n_tokens[:n]
        recs["mtld"] = self._mtld[:n]
        recs["path_id"] = self._path_id[:n]
        return LabelSet(
            schema=LABELS_SCHEMA,
            plan_id=plan_id,
            bundle_id=bundle_id,
            source=stream[0],
            domain=stream[1],
            split=stream[2],
            mtld_spec=MTLD_SPEC_ID,
            paths=tuple(self._paths),
            tokens_in=tokens_in,
            records=recs,
        )


@dataclass(frozen=True)
class LabelRecord:
    """The receipt's copy of the labels sidecar's identity — see ``Receipt.labels``.

    Four numbers and a digest, each checkable against the sidecar's own bytes. This is not a
    duplicate of the sidecar; it is what makes the sidecar's presence and content a claim the
    RECEIPT is accountable for, so a build that wrote shards and lost its labels cannot pass
    ``verify`` looking complete.
    """

    key: str
    sha256: str
    bytes: int
    documents: int
    tokens_in: int
    mtld_spec: str = MTLD_SPEC_ID

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "documents": self.documents,
            "tokens_in": self.tokens_in,
            "mtld_spec": self.mtld_spec,
        }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> "LabelRecord":
        return cls(
            key=str(doc.get("key", "")),
            sha256=str(doc.get("sha256", "")),
            bytes=int(doc.get("bytes", 0)),
            documents=int(doc.get("documents", 0)),
            tokens_in=int(doc.get("tokens_in", 0)),
            mtld_spec=str(doc.get("mtld_spec", "")),
        )
