"""BOUNDED ROW READS: the reader's peak no longer depends on the writer's row-group size.

WHY THIS EXISTS
---------------
Three bundles of the `edu-mix-983b` build died exit 137 with an explicit `OutOfMemoryError` at
14,336 MiB / 8 vCPU: `finepdfs-edu--train--p02of04` (after 364 shards), `pre-1929-books--train`
(58 shards), `reasoning-traces--train` (30 shards). The reader was the largest single term: a
`reasoning-traces` row group declares 1,041.6 MiB of text and `read_row_group` materialises ALL of
it, measured at 2.03-4.01x the declared text (RSS) with an Arrow pool high-water of 1,928.2 MiB.

**A row group is a writer's choice, not a memory budget.** `iter_batches(batch_size=...,
row_groups=[rg], use_threads=False)` reads the same row group in bounded slices: the same pool
high-water drops to **598.2 MiB — a 3.22x reduction, identical across 3 runs**.

WHAT THIS FILE HAS TO PROVE
---------------------------
The reader is the one place a wrong answer is undetectable downstream — the tokens are real, the
counts add up, every shard decodes, and `sha256` is computed over whatever was read. And 171 bundles
/ 3.47 TB were ALREADY BUILT by the old code, so anything but byte-identity makes the corpus
internally inconsistent. So:

1. **Documents are identical to the `read_row_group` path, element by element** — id, text, domain,
   source_path, and ORDER — including at batch sizes that do not divide the row group evenly.
   Asserted against the incumbent, never against a hand-written literal: whatever the old path
   produced is by definition correct, because that is what is already on disk.
2. **An interior empty list keeps its row's alignment.** `list_flatten` drops empty lists, so
   zipping flattened values back in order shifts every subsequent row by one and each document takes
   the NEXT one's text. A fixture with an interior empty list is what catches it.
3. **The IO stays on the calling thread.** `iter_batches` defaults `use_threads=True`, which on the
   whole-file iterator relocates reads onto pyarrow's own threads. `_RangeFile.pos` is an
   unsynchronized read-modify-write, so concurrent callers would read from the WRONG OFFSET — the
   same corrupt-buffer mechanism as the array SIGSEGV, by a different route. This is a TEST and not
   a comment so that a future pyarrow default change fails the suite instead of the corpus.
"""

from __future__ import annotations

import io
import threading

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from edullm_data.corpus import CorpusSpec
from edullm_data.corpus_read import _READ_BATCH_ROWS, read_parquet_documents


def _spec(**over) -> CorpusSpec:
    base = dict(
        key="k", category="math", source_label="src", repo="owner/name",
        file_format="parquet", text_column="text", id_column="id",
        target_tokens=1_000, revision="a" * 40,
    )
    base.update(over)
    return CorpusSpec(**base)


def _flat_parquet(nrows: int = 2_500, row_group_size: int = 700) -> bytes:
    """A flat `large_string` text column — the `reasoning-traces`/`finepdfs-edu` shape.

    `large_string` deliberately: the real sources use 64-bit offsets, and the row-group sizes here do
    not divide `nrows` evenly (2,500 / 700 = 3 full + 400) so the final short batch is exercised.
    """
    tbl = pa.table({
        "text": pa.array([f"document {i} " + "word " * (i % 40) for i in range(nrows)],
                         type=pa.large_string()),
        "id": pa.array([f"doc-{i:05d}" for i in range(nrows)]),
        "dom": pa.array([f"d{i % 3}" for i in range(nrows)]),
    })
    buf = io.BytesIO()
    pq.write_table(tbl, buf, row_group_size=row_group_size)
    return buf.getvalue()


def _nested_parquet() -> bytes:
    """`list<struct<text>>` with an INTERIOR EMPTY LIST at row 3 — the FinePhrase shape.

    Row groups of 3 over 7 rows (3/3/1), so the empty list falls INSIDE the second group rather than
    at a boundary, and the last group is short.
    """
    rollouts = [
        [{"text": "rew-A", "usage": 1}], [{"text": "rew-B", "usage": 2}],
        [{"text": "rew-C", "usage": 3}],
        [],  # <- the alignment trap: no payload for this row
        [{"text": "rew-E", "usage": 5}], [{"text": "rew-F", "usage": 6}],
        [{"text": "rew-G", "usage": 7}],
    ]
    tbl = pa.table({
        "id": pa.array([f"id{i}" for i in range(7)]),
        "text": pa.array([f"ORIGINAL-{i}" for i in range(7)]),  # must never be read
        "rollout_results": pa.array(
            rollouts, type=pa.list_(pa.struct([("text", pa.string()), ("usage", pa.int64())]))
        ),
    })
    buf = io.BytesIO()
    pq.write_table(tbl, buf, row_group_size=3)
    return buf.getvalue()


class _RowGroupReader:
    """THE OLD PATH: a ParquetFile whose `iter_batches` yields ONE batch per whole row group.

    This is how the incumbent is reproduced for comparison — by neutering the bound at the pyarrow
    seam rather than by keeping a second copy of the reader loop in this file. A reference that
    reimplemented the loop would drift from the module and eventually compare the new code with a
    stale transcription of itself; this way BOTH sides run the module's real code and the only
    difference is how many rows arrive per batch.
    """

    def __init__(self, body: bytes):
        self._real = pq.ParquetFile(io.BytesIO(body), pre_buffer=False)
        self.metadata = self._real.metadata
        self.schema = self._real.schema
        self.schema_arrow = self._real.schema_arrow

    def iter_batches(self, batch_size=65536, row_groups=None, columns=None, **kw):
        for rg in (row_groups if row_groups is not None else range(self.metadata.num_row_groups)):
            table = self._real.read_row_group(rg, columns=columns)
            for batch in table.to_batches():
                yield batch


class _BatchSizeSpy:
    """Records the `batch_size` and `use_threads` the reader actually passes to pyarrow.

    Necessary because `_READ_BATCH_ROWS` is read at the CALL, but `use_threads=False` is a literal at
    the call site — and a test that read the constant would say nothing about either. This observes
    the real arguments.
    """

    def __init__(self, body: bytes):
        self._real = pq.ParquetFile(io.BytesIO(body), pre_buffer=False)
        self.metadata = self._real.metadata
        self.schema = self._real.schema
        self.schema_arrow = self._real.schema_arrow
        self.calls: list[dict] = []

    def iter_batches(self, batch_size=65536, row_groups=None, columns=None, **kw):
        self.calls.append({"batch_size": batch_size, "row_groups": row_groups, **kw})
        return self._real.iter_batches(
            batch_size=batch_size, row_groups=row_groups, columns=columns, **kw
        )

    def read_row_group(self, i, columns=None, **kw):  # pragma: no cover - must NOT be reached
        raise AssertionError(
            "read_row_group was called: the whole-row-group read is what OOMed and the reader must "
            "not fall back to it"
        )


def _docs(pf, spec, **kw) -> list[tuple]:
    return [
        (d.id, d.text, d.domain, d.source_path)
        for d in read_parquet_documents("owner/name", "f.parquet", spec, {}, parquet_file=pf, **kw)
    ]


# --------------------------------------------------------------------------------------
# 1. identity with the incumbent
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("batch_size", [1, 2, 7, 333, 700, 1_000, 65_536])
def test_documents_are_identical_to_the_whole_row_group_path(batch_size, monkeypatch):
    """Element-by-element identity against the OLD path, at batch sizes that straddle boundaries.

    `700` divides the row group exactly, `333` and `7` do not, `1` is one batch per row, and `65536`
    exceeds the whole file. The bound is a resource decision; if ANY of these changed a document the
    3.47 TB already on disk would no longer match what a resubmitted bundle produces.
    """
    body = _flat_parquet()
    spec = _spec(domain_column="dom")

    reference = _docs(_RowGroupReader(body), spec)
    monkeypatch.setattr("edullm_data.corpus_read._READ_BATCH_ROWS", batch_size)
    got = _docs(pq.ParquetFile(io.BytesIO(body), pre_buffer=False), spec)

    assert len(reference) == 2_500, "the fixture must produce every row"
    assert got == reference, (
        f"batch_size={batch_size} changed the documents. The reader bound must be invisible in the "
        f"output — 171 bundles are already built by the old path."
    )


@pytest.mark.parametrize("batch_size", [1, 2, 3, 5, 1_000])
def test_an_interior_empty_list_keeps_every_later_row_aligned(batch_size, monkeypatch):
    """THE ALIGNMENT TRAP, under the new bound, at sizes that split the empty row off on its own.

    `list_flatten` DROPS empty lists. If the flattened values were zipped back in order, row 3's
    missing payload would hand `rew-E` to `id3`, `rew-F` to `id4` and so on — every document
    thereafter carrying the NEXT one's text, with no error and no count mismatch.

    Asserted against the incumbent rather than against a literal `[]` or `None`: the REPRESENTATION
    of an absent payload is incidental, and whatever the old path did with it is what the already-built
    shards contain. The property that matters is that ids stay married to their own text.
    """
    body = _nested_parquet()
    spec = _spec(text_column="rollout_results.list.element.text")

    reference = _docs(_RowGroupReader(body), spec)
    monkeypatch.setattr("edullm_data.corpus_read._READ_BATCH_ROWS", batch_size)
    got = _docs(pq.ParquetFile(io.BytesIO(body), pre_buffer=False), spec)

    assert got == reference, f"batch_size={batch_size} shifted a nested row's payload"
    # The empty row must be ABSENT (skipped, not yielded with someone else's text), and every
    # surviving row must still carry ITS OWN payload. Spelled out as explicit pairs rather than
    # computed from the id, because a formula derived from the same off-by-one being tested for would
    # shift with it and assert nothing.
    assert [(d[0], d[1]) for d in got] == [
        ("id0", "rew-A"), ("id1", "rew-B"), ("id2", "rew-C"),
        # id3's rollout list is empty -> skipped entirely. The trap is id4 arriving as "rew-E"
        # (correct) rather than "rew-F" (shifted by the dropped empty).
        ("id4", "rew-E"), ("id5", "rew-F"), ("id6", "rew-G"),
    ], "the empty-rollout row must be skipped and no later row may take the next one's text"
    for _doc_id, text, _dom, _p in got:
        assert "ORIGINAL" not in text, "the top-level original text must never be substituted"


def test_document_order_is_preserved_across_row_group_boundaries(monkeypatch):
    """Order is a CONTRACT, not an incidental: surrogate ids are assigned by position.

    A reader that returned the right SET in the wrong ORDER would renumber every surrogate document
    under the same `plan_id`, and `--shard/--of` would hand different children different documents on
    a re-run. Asserted with a batch size that divides neither the file nor the row group.
    """
    body = _flat_parquet(nrows=2_500, row_group_size=700)
    spec = _spec()
    monkeypatch.setattr("edullm_data.corpus_read._READ_BATCH_ROWS", 333)
    got = _docs(pq.ParquetFile(io.BytesIO(body), pre_buffer=False), spec)
    assert [d[0] for d in got] == [f"doc-{i:05d}" for i in range(2_500)]


def test_the_surrogate_id_sequence_is_unchanged_by_the_bound(monkeypatch):
    """A surrogate id is `(repo, path, row_index)` — a POSITION. The bound must not renumber it.

    `pre-1929-books` is not the surrogate source, but `finepdfs-edu` shares this code path, and a
    renumbering would be invisible: the ids would still be unique, still deterministic, and still
    wrong relative to the 3.47 TB already written.
    """
    body = _flat_parquet(nrows=1_000, row_group_size=300)
    # `id_column` must be EMPTY with a surrogate — `CorpusSpec` refuses both, because naming a column
    # and asking for a surrogate hides which one the build used.
    spec = _spec(id_surrogate=True, id_column="")

    reference = _docs(_RowGroupReader(body), spec)
    monkeypatch.setattr("edullm_data.corpus_read._READ_BATCH_ROWS", 97)
    got = _docs(pq.ParquetFile(io.BytesIO(body), pre_buffer=False), spec)

    assert got == reference, "the surrogate row_index sequence moved"
    assert len({d[0] for d in got}) == 1_000, "surrogate ids must stay unique"


# --------------------------------------------------------------------------------------
# 2. the bound is real, and the whole-row-group read is gone
# --------------------------------------------------------------------------------------


def test_the_reader_asks_pyarrow_for_bounded_batches_and_never_a_whole_row_group():
    """OBSERVES THE REAL CALL, because reading the constant would prove nothing.

    `_batched`'s cap is a DEFAULT ARGUMENT bound at def time — proven on this project by setting the
    module global to 1 and still getting batches of [83, 83, 34]. The same class of mistake here
    would be asserting `_READ_BATCH_ROWS == 1000`, which says nothing about what the reader passes.
    So the spy records the arguments, and `read_row_group` raises if it is reached at all.
    """
    spy = _BatchSizeSpy(_flat_parquet(nrows=2_500, row_group_size=700))
    docs = _docs(spy, _spec())

    assert len(docs) == 2_500
    assert spy.calls, "iter_batches was never called — the reader is not using the bounded path"
    for call in spy.calls:
        assert call["batch_size"] == _READ_BATCH_ROWS == 1_000
        assert call["row_groups"] is not None and len(call["row_groups"]) == 1, (
            "one row group per call keeps the outer loop the unit of iteration"
        )
    assert len(spy.calls) == spy.metadata.num_row_groups == 4


def test_the_bounded_read_holds_fewer_rows_than_the_row_group():
    """The POINT of the change, measured in rows resident rather than in bytes.

    Bytes would be an RSS assertion, and RSS on this path is not reproducible (2,456-4,005 MiB across
    three runs of identical input in the session that wrote this). Rows-per-table is exact,
    deterministic, and is the quantity the peak is proportional to.
    """
    body = _flat_parquet(nrows=2_500, row_group_size=2_500)  # ONE row group, as reasoning-traces has
    pf = pq.ParquetFile(io.BytesIO(body), pre_buffer=False)
    assert pf.metadata.num_row_groups == 1
    assert pf.metadata.row_group(0).num_rows == 2_500

    sizes = [
        b.num_rows
        for b in pf.iter_batches(batch_size=_READ_BATCH_ROWS, row_groups=[0],
                                 columns=["text", "id"], use_threads=False)
    ]
    assert max(sizes) <= _READ_BATCH_ROWS, (
        f"a batch held {max(sizes)} rows against a bound of {_READ_BATCH_ROWS} — the read is not "
        f"bounded and the reader's peak is still the writer's choice"
    )
    assert sum(sizes) == 2_500, "rows were lost or duplicated by the bound"
    assert len(sizes) > 1, "a single batch means the bound did not engage on a 2,500-row group"


# --------------------------------------------------------------------------------------
# 3. the IO must stay on the calling thread
# --------------------------------------------------------------------------------------


def test_the_reader_passes_use_threads_false():
    """`use_threads=True` (pyarrow's DEFAULT) relocates the reads. This pins the keyword.

    MEASURED with an instrumented file object: the whole-file `iter_batches` at the default put 2 of
    3 read/seek calls on pyarrow's own threads (`{MainThread: 8, Dummy-1: 8, Dummy-2: 8}`), where
    `read_row_group` kept 100% on the caller. `pre_buffer=False` does not suppress it — different
    mechanism.
    """
    spy = _BatchSizeSpy(_flat_parquet(nrows=100, row_group_size=50))
    _docs(spy, _spec())
    assert spy.calls
    for call in spy.calls:
        assert call.get("use_threads") is False, (
            "iter_batches must be called with use_threads=False. Its default is True, which moves "
            "reads onto pyarrow's threads; _RangeFile.pos is an unsynchronized read-modify-write, "
            "so two interleaved callers read from the WRONG OFFSET — the same corrupt-buffer "
            "mechanism as the array SIGSEGV."
        )


def test_no_read_reaches_a_thread_this_module_did_not_create():
    """THE PROVENANCE CHECK, measured on real bytes rather than argued from the keyword.

    A keyword assertion says what we ASKED for; this says what pyarrow DID. It is the check that
    still fails if a future pyarrow version relocates IO for some other reason — which is exactly the
    failure that would otherwise reach production, because a capacity- or corruption-induced crash
    mid-bundle looks identical to the OOM this whole change is fixing.
    """
    body = _flat_parquet(nrows=2_500, row_group_size=700)
    seen: dict[str, int] = {}
    main = threading.current_thread().name

    class _ThreadSpyFile(io.BytesIO):
        def _note(self):
            n = threading.current_thread().name
            seen[n] = seen.get(n, 0) + 1

        def read(self, *a, **kw):
            self._note()
            return super().read(*a, **kw)

        def seek(self, *a, **kw):
            self._note()
            return super().seek(*a, **kw)

    # `fileobj=` is the seam that reaches `_open_parquet`, i.e. the REAL `pq.ParquetFile(...,
    # pre_buffer=False)` call — going through `parquet_file=` would bypass the very keyword whose
    # effect is under test.
    spy = _ThreadSpyFile(body)
    docs = [
        d.id
        for d in read_parquet_documents("owner/name", "f.parquet", _spec(), {}, fileobj=spy)
    ]
    assert len(docs) == 2_500, "the fixture must be fully read for the thread evidence to mean anything"
    assert seen, "no read was observed — the spy is not on the IO path and proves nothing"
    assert set(seen) == {main}, (
        f"reads landed on threads this module never created: {sorted(seen)}. `_RangeFile` keeps an "
        f"unsynchronized `pos`, so concurrent reads return bytes from the wrong offset and pyarrow "
        f"turns a corrupt buffer into a SIGSEGV. Reads must stay on the calling thread."
    )
