"""The `s3://` STAGED SOURCE path: enumeration, ordering, and document identity.

WHY THIS FILE EXISTS
--------------------
Seven of the sixteen failures on the 2026-08-09 build were `exit 2, HTTP 401` on a GATED HF repo.
The bytes had already been copied to `s3://edullm-landing/_src/nemotron-cc-math-v1/` and
byte-verified — **and no registry row could name them, because every reader resolved
`https://huggingface.co/datasets/{repo}/resolve/{revision}/{path}` unconditionally.** 61 B tokens,
the math pillar. The staging was done and never wired up.

WHAT EACH TEST HERE RECOMPUTES
------------------------------
Not "returns a list". Every test either reads real parquet/gzip bytes back out as documents and
compares them **element by element** against what was written, or asserts an ORDER against an
independently-computed expected order. The three properties that carry consequences:

1. **Enumeration is sorted by key, and the sort is OURS.** `_bundle_files` takes `files[i::K]`, a
   stride — which is a pure function of the list only if the list is ordered. `FakeS3.list` iterates
   a dict, so seeding the store out of order exercises the sort rather than a coincidence.
2. **The document stream is byte-identical to the HTTPS path's.** The same parquet bytes are read
   through a local file handle and through `S3RangeFile`, and the two `Document` lists are compared
   field by field. `source_doc` ordinals and the whole MTLD permutation are a counter over this
   stream, so a reorder or a single dropped document silently re-labels the curriculum.
3. **A prefix is a DIRECTORY, matched with a trailing `/`.** `4plus` must not sweep in `4plus_MIND`
   — that rewrite is a re-write of `4plus` and including both double-counts. The registry's own
   trap line says `startswith('4plus')` catches it; this asserts the code does not.
"""

from __future__ import annotations

import gzip
import io
import json

import pytest

from edullm_data.corpus import CorpusSpec, Document
from edullm_data.corpus_read import (
    S3_SCHEME,
    ReadError,
    S3RangeFile,
    is_s3_source,
    parse_s3_uri,
    read_documents,
    read_jsonl_gz_documents,
    read_parquet_documents,
    s3_files,
)
from edullm_data.s3 import FakeS3

BUCKET = "edullm-landing"
PREFIX = "_src/gated-corpus-v1"


def _spec(**over) -> CorpusSpec:
    base = dict(
        key="gated", category="math", source_label="gated", repo=f"{S3_SCHEME}{BUCKET}/{PREFIX}",
        file_format="parquet", text_column="text", id_column="id",
        target_tokens=1_000, revision="c" * 40,
    )
    base.update(over)
    return CorpusSpec(**base)


def _parquet_bytes(rows: list[dict], *, nested: bool = False) -> bytes:
    """Real parquet bytes, written by pyarrow, so the footer this reads is a real footer."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    if nested:
        table = pa.table({
            "id": pa.array([r["id"] for r in rows]),
            # The FinePhrase shape: the payload is nested and the FLAT `text` is the SOURCE.
            "text": pa.array([r.get("source_text", "SOURCE-DOCUMENT-DO-NOT-READ") for r in rows]),
            "rollout_results": pa.array(
                [[{"text": r["text"]}] for r in rows],
                type=pa.list_(pa.struct([("text", pa.string())])),
            ),
        })
    else:
        table = pa.table({
            "id": pa.array([r["id"] for r in rows]),
            "text": pa.array([r["text"] for r in rows]),
        })
    buf = io.BytesIO()
    # Small row groups so multi-row-group iteration is exercised, as it is on real files.
    pq.write_table(table, buf, row_group_size=2)
    return buf.getvalue()


def _rows(n: int, *, start: int = 0) -> list[dict]:
    return [{"id": f"doc-{i:05d}", "text": f"document {i} body text " * 8} for i in range(start, start + n)]


# ======================================================================================
# 1. The URI, and the two ways a bad one is silent
# ======================================================================================


def test_a_bucket_only_uri_is_refused_because_it_would_enumerate_the_whole_bucket():
    """`s3://edullm-landing` with no prefix lists every staged source AND every in-flight build
    shard. The failure would not look like a bad row — it would look like an enormous source that
    happens to contain a lot of parquet, which is why this must raise rather than default."""
    with pytest.raises(ReadError, match="no prefix"):
        parse_s3_uri(f"{S3_SCHEME}{BUCKET}")
    with pytest.raises(ReadError, match="no bucket"):
        parse_s3_uri(S3_SCHEME)


def test_the_predicate_and_the_parse_agree_and_a_trailing_slash_is_normalised():
    assert is_s3_source(f"{S3_SCHEME}{BUCKET}/{PREFIX}")
    assert not is_s3_source("nvidia/Nemotron-CC-Math-v1")
    assert parse_s3_uri(f"{S3_SCHEME}{BUCKET}/{PREFIX}") == (BUCKET, PREFIX)
    assert parse_s3_uri(f"{S3_SCHEME}{BUCKET}/{PREFIX}/") == (BUCKET, PREFIX)


# ======================================================================================
# 2. Enumeration and ORDERING — the property the permutation rests on
# ======================================================================================


def test_enumeration_is_sorted_by_key_even_when_the_listing_is_not():
    """🔴 **THE ORDERING GUARANTEE, exercised against an out-of-order listing.**

    `_bundle_files` strides `files[i::K]`, so an unordered list gives each of the K children a
    different slice on every re-run with NO error — the token counts still add up and every shard
    still decodes. `FakeS3.list` iterates its dict in insertion order, so seeding the objects
    backwards means a reader that trusted the listing would return them backwards.
    """
    s3 = FakeS3()
    want = [f"{PREFIX}/part_{i:03d}.parquet" for i in range(6)]
    for key in reversed(want):  # insertion order is the REVERSE of the answer
        s3.seed(BUCKET, key, _parquet_bytes(_rows(2)))
    got = s3_files(_spec(), s3=s3, extensions=(".parquet",))
    assert [e["path"] for e in got] == want
    # Sorted, not merely "a permutation of" — the whole point is a total order.
    assert [e["path"] for e in got] == sorted(e["path"] for e in got)
    # And the entry shape matches `hf_files`': path + size, so `_bundle_files` needs no adapter.
    assert all(e["size"] == len(s3.get(BUCKET, e["path"])) for e in got)


def test_the_ordering_is_stable_across_repeated_listings():
    """The stride is a pure function of `(spec, i, K)` only if two listings agree. Asserted by
    running the enumeration twice over a store whose iteration order the first call did not
    change."""
    s3 = FakeS3()
    for i in (4, 0, 2, 5, 1, 3):
        s3.seed(BUCKET, f"{PREFIX}/part_{i:03d}.parquet", _parquet_bytes(_rows(2)))
    a = [e["path"] for e in s3_files(_spec(), s3=s3, extensions=(".parquet",))]
    b = [e["path"] for e in s3_files(_spec(), s3=s3, extensions=(".parquet",))]
    assert a == b == sorted(a)


def test_a_config_scopes_the_prefix_and_does_not_sweep_a_sibling_that_shares_it():
    """🔴 **`4plus` MUST NOT match `4plus_MIND`.** The MIND set is a REWRITE of `4plus`; including
    both double-counts the same documents. The registry's trap line names `startswith` as the
    hazard — this asserts the code appends a `/` and therefore matches a DIRECTORY."""
    s3 = FakeS3()
    s3.seed(BUCKET, f"{PREFIX}/4plus/part_000.parquet", _parquet_bytes(_rows(2)))
    s3.seed(BUCKET, f"{PREFIX}/4plus/part_001.parquet", _parquet_bytes(_rows(2)))
    s3.seed(BUCKET, f"{PREFIX}/4plus_MIND/part_000.parquet", _parquet_bytes(_rows(2)))
    s3.seed(BUCKET, f"{PREFIX}/3/part_000.parquet", _parquet_bytes(_rows(2)))
    got = [e["path"] for e in s3_files(_spec(config="4plus"), s3=s3, extensions=(".parquet",))]
    assert got == [f"{PREFIX}/4plus/part_000.parquet", f"{PREFIX}/4plus/part_001.parquet"]
    assert not any("MIND" in p for p in got)
    # And the sibling config resolves to its own files, so one staged repo serves two rows.
    assert [e["path"] for e in s3_files(_spec(config="3"), s3=s3, extensions=(".parquet",))] == [
        f"{PREFIX}/3/part_000.parquet"
    ]


def test_an_empty_prefix_raises_rather_than_yielding_an_empty_corpus():
    """A missing category is not a smaller corpus, it is a wrong one — the same argument
    `verify` is built on."""
    s3 = FakeS3()
    s3.seed(BUCKET, f"{PREFIX}/README.md", b"not payload")
    with pytest.raises(ReadError, match="no .* objects under"):
        s3_files(_spec(), s3=s3, extensions=(".parquet",))


def test_a_zero_byte_object_is_not_offered_as_a_payload_file():
    """A 0-byte key has no footer; pyarrow raises deep inside the C++ layer on it. Filtered here so
    the diagnostic names the prefix instead."""
    s3 = FakeS3()
    s3.seed(BUCKET, f"{PREFIX}/good.parquet", _parquet_bytes(_rows(2)))
    s3.seed(BUCKET, f"{PREFIX}/empty.parquet", b"")
    got = [e["path"] for e in s3_files(_spec(), s3=s3, extensions=(".parquet",))]
    assert got == [f"{PREFIX}/good.parquet"]


# ======================================================================================
# 3. The RANGE FILE — the short-read loop is what stands between us and a SIGSEGV
# ======================================================================================


def test_the_range_file_returns_exactly_what_was_asked_for_over_a_short_reading_client():
    """⚠️ A `read(n)` that comes back short is what pyarrow turns into `Segmentation fault` — it
    reads a page header at an offset now inside the wrong bytes. `Boto3S3.get_range` `.read()`s a
    body that is PERMITTED to be short, so the loop is not optional just because the transport is
    S3 rather than HTTP. Modelled with a client that deliberately returns one byte at a time."""

    class DribbleS3(FakeS3):
        def get_range(self, bucket, key, start, length):
            return super().get_range(bucket, key, start, min(1, length))

    body = bytes(range(256)) * 4
    s3 = DribbleS3()
    s3.seed(BUCKET, "k", body)
    rf = S3RangeFile(s3, BUCKET, "k", len(body))
    assert rf.read(700) == body[:700]        # 700 one-byte responses, reassembled
    assert rf.tell() == 700
    rf.seek(10)
    assert rf.read(20) == body[10:30]
    rf.seek(-5, 2)
    assert rf.read() == body[-5:]


def test_the_range_file_raises_on_a_truncation_rather_than_returning_short():
    """A client that returns nothing mid-request is a real truncation. Silently returning short is
    the whole failure mode, so it raises."""

    class TruncatingS3(FakeS3):
        def get_range(self, bucket, key, start, length):
            return b"" if start > 0 else super().get_range(bucket, key, start, 4)

    s3 = TruncatingS3()
    s3.seed(BUCKET, "k", b"x" * 100)
    with pytest.raises(ReadError, match="short read"):
        S3RangeFile(s3, BUCKET, "k", 100).read(100)


# ======================================================================================
# 4. DOCUMENT IDENTITY — the same bytes through both transports, compared element by element
# ======================================================================================


def _docs_via_local(body: bytes, spec: CorpusSpec) -> list[Document]:
    """The HTTPS path's own code, driven through its `fileobj=` seam over the same bytes."""
    return list(read_parquet_documents("owner/name", "f.parquet", spec, fileobj=io.BytesIO(body)))


def _docs_via_s3(body: bytes, spec: CorpusSpec, key: str) -> list[Document]:
    s3 = FakeS3()
    s3.seed(BUCKET, key, body)
    return list(read_parquet_documents(spec.repo, key, spec, s3=s3))


def test_the_staged_path_yields_the_IDENTICAL_document_stream_element_by_element():
    """🔴 **THE ASSERTION THE CURRICULUM RESTS ON.**

    `source_doc` is a COUNTER over the documents the packer pulled, so document ORDER is document
    IDENTITY. A staged read that reordered, dropped, or duplicated even one document would map every
    subsequent chunk of the permutation to the wrong text — and nothing downstream could see it: the
    tokens are real, the counts add up, and every shard decodes.

    Compared FIELD BY FIELD in order, not by count and not by a hash of the aggregate.
    """
    rows = _rows(9)
    body = _parquet_bytes(rows)
    want = _docs_via_local(body, _spec(repo="owner/name"))
    got = _docs_via_s3(body, _spec(), f"{PREFIX}/part_000.parquet")

    assert len(got) == len(want) == 9
    for i, (a, b) in enumerate(zip(want, got)):
        assert a.id == b.id, f"document {i}: id moved"
        assert a.text == b.text, f"document {i}: TEXT moved"
        assert a.source == b.source
        assert a.domain == b.domain
    # And the ids are the upstream ones, in upstream row order.
    assert [d.id for d in got] == [r["id"] for r in rows]


def test_the_nested_column_trap_is_closed_on_the_staged_path_too():
    """The single most dangerous field in the module is `text_column`, and its trap is a property of
    the FOOTER, not of how the footer arrived. A staged FinePhrase-shaped file must still resolve
    `rollout_results.list.element.text` — the REWRITE — and never the flat `text`, which is the
    SOURCE document. Reading the wrong one substitutes real web text for the synthetic pool and no
    hash, size, or decode check can see it."""
    rows = [{"id": f"n{i}", "text": f"REWRITE {i} " * 6, "source_text": f"ORIGINAL {i} " * 6}
            for i in range(6)]
    body = _parquet_bytes(rows, nested=True)
    spec = _spec(text_column="rollout_results.list.element.text")
    got = _docs_via_s3(body, spec, f"{PREFIX}/nested.parquet")
    assert [d.text for d in got] == [r["text"] for r in rows]
    assert not any("ORIGINAL" in d.text for d in got)
    # The flat spelling reads the SOURCE — asserted so the two are known to be distinguishable
    # here, i.e. the fixture really does discriminate.
    flat = _docs_via_s3(body, _spec(text_column="text"), f"{PREFIX}/nested.parquet")
    assert all("ORIGINAL" in d.text for d in flat)


def test_a_surrogate_id_on_a_staged_row_embeds_the_S3_KEY_and_is_stable():
    """`surrogate_id` is `(file path, row index)`. On a staged row the "file path" is the object
    KEY — the only name the row has — so the id is stable across re-reads of the same prefix and
    changes loudly if the copy is re-sharded."""
    body = _parquet_bytes(_rows(4))
    spec = _spec(id_column="", id_surrogate=True)
    key = f"{PREFIX}/part_007.parquet"
    got = _docs_via_s3(body, spec, key)
    assert [d.id for d in got] == [f"{PREFIX.rsplit('/', 1)[-1]}/{key}#{i}" for i in range(4)]
    # Deterministic: a second read of the same object gives the same ids.
    assert [d.id for d in _docs_via_s3(body, spec, key)] == [d.id for d in got]


def test_the_domain_column_is_inherited_on_a_staged_row_exactly_as_on_an_HF_row():
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table({
        "id": pa.array(["a", "b", "c"]),
        "text": pa.array(["alpha text here", "beta text here", "gamma text here"]),
        "lang": pa.array(["Python", "Rust", "Python"]),
    })
    buf = io.BytesIO()
    pq.write_table(table, buf)
    spec = _spec(domain_column="lang")
    got = _docs_via_s3(buf.getvalue(), spec, f"{PREFIX}/d.parquet")
    assert [d.domain for d in got] == ["python", "rust", "python"]


# ======================================================================================
# 5. The json.gz half — every Common Pile source, and the gzip checks are transport-independent
# ======================================================================================


def test_a_staged_json_gz_object_reads_the_same_documents_as_the_https_path():
    recs = [{"id": f"j{i}", "text": f"line {i} of the staged corpus " * 4} for i in range(7)]
    raw = b"\n".join(json.dumps(r).encode() for r in recs)
    body = gzip.compress(raw)
    spec = _spec(file_format="json.gz")

    want = list(read_jsonl_gz_documents("owner/name", "f.json.gz", _spec(
        file_format="json.gz", repo="owner/name"), chunks=[body]))
    s3 = FakeS3()
    key = f"{PREFIX}/shard-0000.json.gz"
    s3.seed(BUCKET, key, body)
    got = list(read_jsonl_gz_documents(spec.repo, key, spec, s3=s3))

    assert len(got) == len(want) == 7
    for a, b in zip(want, got):
        assert (a.id, a.text) == (b.id, b.text)
    assert [d.id for d in got] == [r["id"] for r in recs]


def test_a_TRUNCATED_staged_gzip_object_still_raises_the_eof_check():
    """⚠️ `decompressobj` does NOT raise on a truncated stream — it returns the bytes it could
    decode and leaves `eof` False, which reads downstream as a small-but-valid source. That check is
    the whole defence and it must not be bypassed by the staged transport."""
    recs = [{"id": f"j{i}", "text": f"record {i} " * 30} for i in range(20)]
    body = gzip.compress(b"\n".join(json.dumps(r).encode() for r in recs))
    s3 = FakeS3()
    key = f"{PREFIX}/cut.json.gz"
    s3.seed(BUCKET, key, body[: len(body) - 20])
    spec = _spec(file_format="json.gz")
    with pytest.raises(ReadError, match="TRUNCATED"):
        list(read_jsonl_gz_documents(spec.repo, key, spec, s3=s3))


def test_a_multi_member_staged_gzip_object_recovers_every_member():
    """Real `.json.gz` files are sometimes CONCATENATED members, and one `decompressobj` stops at the
    first member's end with the rest sitting in `unused_data`."""
    a = [{"id": f"m{i}", "text": f"member one doc {i} " * 5} for i in range(3)]
    b = [{"id": f"m{i}", "text": f"member two doc {i} " * 5} for i in range(3, 6)]
    # Each member ends with a newline. Without it member 1's last line and member 2's first line
    # are ONE line across the member boundary — which is a real property of the format, not a
    # fixture detail: a producer that omits the final newline makes the join lossy, and the reader
    # correctly reports it as malformed JSON rather than guessing where to split.
    body = (gzip.compress(b"\n".join(json.dumps(r).encode() for r in a) + b"\n")
            + gzip.compress(b"\n".join(json.dumps(r).encode() for r in b) + b"\n"))
    s3 = FakeS3()
    key = f"{PREFIX}/multi.json.gz"
    s3.seed(BUCKET, key, body)
    got = list(read_jsonl_gz_documents(_spec(file_format="json.gz").repo, key,
                                       _spec(file_format="json.gz"), s3=s3))
    assert [d.id for d in got] == [r["id"] for r in a + b]


# ======================================================================================
# 6. The refusals — a staged row with no client must not fall back to a 401
# ======================================================================================


def test_a_staged_row_without_a_client_refuses_instead_of_silently_using_the_hf_url():
    """The fallback is the bug this whole path exists to fix: resolving
    `https://huggingface.co/datasets/s3://…` would 404, and resolving the row's UPSTREAM repo would
    401 — which is exactly the failure that cost 7 bundles. Refuse, naming the missing client."""
    spec = _spec()
    with pytest.raises(ReadError, match="no `s3=` client"):
        list(read_parquet_documents(spec.repo, f"{PREFIX}/x.parquet", spec))
    jspec = _spec(file_format="json.gz")
    with pytest.raises(ReadError, match="no `s3=` client"):
        list(read_jsonl_gz_documents(jspec.repo, f"{PREFIX}/x.json.gz", jspec))


def test_read_documents_forwards_the_client_through_the_format_seam():
    """The dispatch table stays about FORMATS. `s3=` rides `**kwargs` into whichever reader the
    format names, so there is no second place that decides which rows are staged."""
    body = _parquet_bytes(_rows(3))
    s3 = FakeS3()
    key = f"{PREFIX}/via-seam.parquet"
    s3.seed(BUCKET, key, body)
    spec = _spec()
    got = list(read_documents(spec.repo, key, spec, s3=s3))
    assert [d.id for d in got] == ["doc-00000", "doc-00001", "doc-00002"]
    # And the HTTPS branch still ignores an unused `s3=`, so the driver passes it unconditionally.
    local = list(read_documents("owner/name", "f.parquet", _spec(repo="owner/name"),
                                fileobj=io.BytesIO(body), s3=s3))
    assert [d.id for d in local] == [d.id for d in got]


def test_the_entry_dict_carries_its_own_bucket_so_no_caller_reparses_the_uri():
    """`s3_files` returns `bucket` on every entry; a reader handed the entry must use it rather than
    re-deriving one, which is how a two-bucket staging would silently read the wrong one."""
    s3 = FakeS3()
    key = f"{PREFIX}/e.parquet"
    body = _parquet_bytes(_rows(2))
    s3.seed(BUCKET, key, body)
    entry = s3_files(_spec(), s3=s3, extensions=(".parquet",))[0]
    assert entry["bucket"] == BUCKET
    got = list(read_parquet_documents(_spec().repo, entry, _spec(), s3=s3))
    assert [d.id for d in got] == ["doc-00000", "doc-00001"]


# ======================================================================================
# 7. THE COMPOSITION — a STAGED file with a NESTED column, through the arrow extraction
# ======================================================================================


def test_a_staged_nested_file_reads_correctly_through_the_arrow_native_extraction():
    """🔴 **The two changes of this branch, composed — and this IS the production path.**

    The `s3://` transport (this file) and the arrow-native nested extraction
    (`test_corpus_read_arrow_native.py`) were built for different reasons and each is tested alone.
    Nothing tested them TOGETHER, and a staged nested source is exactly what
    `nemotron-cc-math-*` will be if its schema ever nests — and what a future gated FinePhrase-shaped
    source would be on day one.

    The fixture carries an INTERIOR empty list (row 2 of 7), because that is the case where the
    flatten-based extraction shifts every later row by one and each document takes the NEXT one's
    text. Combined with the staged transport there is no reference to fall back on: the tokens are
    real, the counts add up, and only the pairing is wrong.
    """
    rows = [{"id": f"n{i}", "text": f"REWRITE {i} synthetic body ",
             "source_text": f"ORIGINAL {i} web body "} for i in range(7)]
    import pyarrow as pa
    import pyarrow.parquet as pq

    lists = [[{"text": r["text"]}] for r in rows]
    lists[2] = []  # interior: rows 3..6 shift under a naive zip
    table = pa.table({
        "id": pa.array([r["id"] for r in rows]),
        "text": pa.array([r["source_text"] for r in rows]),
        "rollout_results": pa.array(lists, type=pa.list_(pa.struct([("text", pa.string())]))),
    })
    buf = io.BytesIO()
    pq.write_table(table, buf, row_group_size=3)  # several row groups, as real files have

    s3 = FakeS3()
    key = f"{PREFIX}/nested-staged.parquet"
    s3.seed(BUCKET, key, buf.getvalue())
    spec = _spec(text_column="rollout_results.list.element.text")
    got = list(read_documents(spec.repo, key, spec, s3=s3))

    assert [d.id for d in got] == ["n0", "n1", "n3", "n4", "n5", "n6"], (
        "the payload-less row must be skipped and every survivor must keep its own id"
    )
    for d in got:
        assert f"REWRITE {d.id[1:]} " in d.text, (
            f"{d.id} carries {d.text[:32]!r} — the row alignment shifted across the staged read"
        )
    assert not any("ORIGINAL" in d.text for d in got), "read the SOURCE document, not the rewrite"
    # And the transport really was S3: the same spec with no client refuses.
    with pytest.raises(ReadError, match="no `s3=` client"):
        list(read_documents(spec.repo, key, spec))
