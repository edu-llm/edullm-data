"""The ARROW-NATIVE parquet extraction: identical documents, without `to_pylist()`.

WHY THIS EXISTS
---------------
FinePhrase is 36 B tokens across 4 registry rows and measured **4-10x slower than every other
source** (0.26-0.34x the throughput anchor, against flat controls at 0.78-1.04x). PLAT localized the
whole penalty to ONE call — `to_pylist()` on a nested `list<struct<text>>` column — and showed the
arrow READ is identical, i.e. column projection was never the problem:

        arrow READ      to_pylist()
    FLAT    text          0.0009 s        0.0021 s
    NESTED  rollout..text 0.0009 s        0.0076 s   <- 3.6x

Reproduced here on a 20,000-row fixture: nested `to_pylist` 54.29 ms vs `list_flatten` +
`.field('text')` **22.08 ms — 2.46x faster, and 0.94x the FLAT baseline**, so the nested penalty
essentially disappears. No schema change.

WHAT THIS FILE HAS TO PROVE, and correctness dominates speed here
-----------------------------------------------------------------
The reader is the one place a wrong answer is undetectable downstream: the tokens are real, the
counts add up, every shard decodes, and `sha256` is computed over whatever was read. So:

1. **Document order and content are BYTE-IDENTICAL to the old path, element by element** — not by
   count, not by an aggregate hash. Asserted for flat, nested, surrogate-id, and domain-bearing
   shapes, against a reference implementation of the OLD `to_pylist()` walk kept in this file.
2. **The FLAT path must not regress.** It goes through the same function, so it needs its own
   assertions rather than an argument that it is unaffected.
3. **`list_flatten` DROPS empty lists.** That is the one way this optimisation can be silently wrong:
   zipping flattened values back in order shifts every subsequent row by one, so each document would
   take the NEXT one's text. A fixture with an INTERIOR empty list is what catches it; a fixture
   without one cannot, and that is why several tests below carry one deliberately.
"""

from __future__ import annotations

import io

import pytest

from edullm_data.corpus import CorpusSpec
from edullm_data.corpus_read import (
    ReadError,
    _arrow_column_values,
    _compile_walk,
    _resolve_leaf,
    _walk,
    read_parquet_documents,
)


def _spec(**over) -> CorpusSpec:
    base = dict(
        key="k", category="math", source_label="src", repo="owner/name",
        file_format="parquet", text_column="text", id_column="id",
        target_tokens=1_000, revision="a" * 40,
    )
    base.update(over)
    return CorpusSpec(**base)


def _write(table) -> bytes:
    import pyarrow.parquet as pq

    buf = io.BytesIO()
    # Several small row groups, as real files have — a single-row-group fixture would not exercise
    # the per-row-group extraction at all.
    pq.write_table(table, buf, row_group_size=3)
    return buf.getvalue()


def _reference_docs(body: bytes, spec: CorpusSpec, *, domain_map=None) -> list[tuple]:
    """THE OLD PATH, reimplemented here: `table.to_pylist()` + the per-row `_walk`.

    Kept in the test rather than in the module, because the point is to compare the NEW code against
    what the OLD code did — and a reference that imported the new helper would compare the new code
    with itself. This is a transcription of the loop as it stood before the arrow change, using the
    same `_compile_walk`/`_walk`/`_resolve_leaf` the module still exports.
    """
    import pyarrow.parquet as pq

    from edullm_data.corpus_read import _domain_of, surrogate_id

    pf = pq.ParquetFile(io.BytesIO(body))
    md = pf.metadata
    text_leaf = _resolve_leaf(md, spec.text_column, what="text_column")
    id_leaf = None if spec.id_surrogate else _resolve_leaf(md, spec.id_column, what="id_column")
    _, text_walk = _compile_walk(pf.schema_arrow, text_leaf, what="text_column")
    id_walk = (None if id_leaf is None else
               _compile_walk(pf.schema_arrow, id_leaf, what="id_column", require_string=False)[1])
    wanted = [text_leaf] if id_leaf is None else [text_leaf, id_leaf]
    domain_walk = None
    if spec.domain_column is not None:
        dleaf = _resolve_leaf(md, spec.domain_column, what="domain_column")
        _, domain_walk = _compile_walk(pf.schema_arrow, dleaf, what="domain_column")
        wanted.append(dleaf)
    leaves = list(dict.fromkeys(wanted))

    out, row_index = [], 0
    for rg in range(md.num_row_groups):
        table = pf.read_row_group(rg, columns=leaves)
        for row in table.to_pylist():
            text = _walk(row, text_walk)
            if id_walk is None:
                doc_id = surrogate_id(spec.repo, "f.parquet", row_index)
                row_index += 1
            else:
                doc_id = _walk(row, id_walk)
            if not isinstance(text, str) or not text:
                continue
            out.append((
                str(doc_id), text,
                _domain_of(row, spec, domain_map=domain_map, walk=domain_walk),
            ))
    return out


def _new_docs(body: bytes, spec: CorpusSpec, *, domain_map=None) -> list[tuple]:
    docs = list(read_parquet_documents(
        spec.repo, "f.parquet", spec, fileobj=io.BytesIO(body), domain_map=domain_map))
    return [(d.id, d.text, d.domain) for d in docs]


def _assert_identical(body: bytes, spec: CorpusSpec, *, domain_map=None, expect_min=1):
    """Element by element, in order. The whole contract of this change."""
    want = _reference_docs(body, spec, domain_map=domain_map)
    got = _new_docs(body, spec, domain_map=domain_map)
    assert len(got) == len(want) >= expect_min, (
        f"document COUNT differs: {len(got)} vs the old path's {len(want)}"
    )
    for i, (a, b) in enumerate(zip(want, got)):
        assert a[0] == b[0], f"document {i}: id moved ({a[0]!r} -> {b[0]!r})"
        assert a[1] == b[1], f"document {i}: TEXT moved ({a[1][:40]!r} -> {b[1][:40]!r})"
        assert a[2] == b[2], f"document {i}: domain moved ({a[2]!r} -> {b[2]!r})"
    return got


# ======================================================================================
# 1. Byte identity, the four shapes that matter
# ======================================================================================


def test_the_FLAT_path_is_byte_identical_and_must_not_regress():
    """The flat column is 96% of the corpus. It goes through the SAME function now, so "unaffected"
    is a claim needing a test rather than an argument."""
    import pyarrow as pa

    rows = [(f"d{i:04d}", f"flat document {i} body " * 5) for i in range(11)]
    body = _write(pa.table({"id": pa.array([r[0] for r in rows]),
                            "text": pa.array([r[1] for r in rows])}))
    got = _assert_identical(body, _spec(), expect_min=11)
    assert [g[0] for g in got] == [r[0] for r in rows], "upstream row order must survive"
    assert [g[1] for g in got] == [r[1] for r in rows]


def test_the_NESTED_finephrase_shape_is_byte_identical_and_reads_the_REWRITE():
    """🔴 The trap this module exists to close, re-asserted through the new extraction.

    FinePhrase's payload is `rollout_results[0].text`; the FLAT `text` is the SOURCE document.
    Reading the wrong one substitutes real web text for the synthetic pool and NO hash, size, or
    decode check can see it — so the arrow path must resolve the same leaf the old one did.
    """
    import pyarrow as pa

    n = 9
    rewrites = [f"REWRITE {i} synthetic body " * 4 for i in range(n)]
    sources = [f"ORIGINAL {i} web body " * 4 for i in range(n)]
    body = _write(pa.table({
        "id": pa.array([f"n{i}" for i in range(n)]),
        "text": pa.array(sources),
        "rollout_results": pa.array([[{"text": t}] for t in rewrites],
                                    type=pa.list_(pa.struct([("text", pa.string())]))),
    }))
    spec = _spec(text_column="rollout_results.list.element.text")
    got = _assert_identical(body, spec, expect_min=n)
    assert [g[1] for g in got] == rewrites
    assert not any("ORIGINAL" in g[1] for g in got), "the SOURCE document was read, not the rewrite"


def test_a_SURROGATE_id_is_byte_identical_including_its_row_numbering():
    """`surrogate_id` is `(file path, row index)` and the index is per FILE, not per row group. A
    bulk extraction that restarted numbering at each row group would produce colliding ids that look
    entirely plausible."""
    import pyarrow as pa

    n = 10
    body = _write(pa.table({"text": pa.array([f"doc {i} text here " * 3 for i in range(n)])}))
    spec = _spec(id_column="", id_surrogate=True)
    got = _assert_identical(body, spec, expect_min=n)
    # Numbering is continuous across the 4 row groups (row_group_size=3), not restarted.
    suffixes = [int(g[0].rsplit("#", 1)[1]) for g in got]
    assert suffixes == list(range(n)), suffixes


def test_a_DOMAIN_column_is_byte_identical_through_the_shared_fold():
    """`_domain_of` and the arrow path now both call `_fold_domain`, so the `other` rule, the
    collision-checked map, and the non-string refusal cannot diverge between them."""
    import pyarrow as pa

    body = _write(pa.table({
        "id": pa.array(["a", "b", "c", "d"]),
        "text": pa.array(["alpha text", "beta text", "gamma text", "delta text"]),
        "lang": pa.array(["Python", "Rust", "Python", "Zig"]),
    }))
    spec = _spec(domain_column="lang")
    got = _assert_identical(body, spec, expect_min=4)
    assert [g[2] for g in got] == ["python", "rust", "python", "zig"]
    # And with a map, the tail folds to `other` identically on both paths.
    got2 = _assert_identical(body, spec, domain_map={"Python": "python", "Rust": "rust"})
    assert [g[2] for g in got2] == ["python", "rust", "python", "other"]


# ======================================================================================
# 2. THE EMPTY LIST — the one way this optimisation is silently wrong
# ======================================================================================


def test_an_INTERIOR_empty_list_does_not_shift_every_subsequent_document():
    """🔴🔴 **THE ASSERTION THAT MAKES THIS CHANGE SAFE.**

    `pc.list_flatten` DROPS empty lists — it does not emit a null for them. So zipping the flattened
    values back against row order shifts every row after an empty list by one, and **each document
    would take the NEXT document's text**. Nothing downstream could see it: the ids are real, the
    texts are real, the counts add up, and only the PAIRING is wrong.

    An empty `rollout_results` is verified-legal in parquet and means the row has no rewrite. The
    empty list here is INTERIOR (row 3 of 8), because a trailing one cannot detect the shift.
    """
    import pyarrow as pa

    texts = [f"REWRITE-{i} body text here " * 3 for i in range(8)]
    lists = [[{"text": t}] for t in texts]
    lists[3] = []              # interior: rows 4..7 would shift by one under a naive zip
    ids = [f"n{i}" for i in range(8)]
    body = _write(pa.table({
        "id": pa.array(ids),
        "rollout_results": pa.array(lists, type=pa.list_(pa.struct([("text", pa.string())]))),
    }))
    spec = _spec(text_column="rollout_results.list.element.text")
    got = _assert_identical(body, spec, expect_min=7)

    # The payload-less row is SKIPPED, and every survivor keeps ITS OWN text.
    assert len(got) == 7
    assert [g[0] for g in got] == [i for k, i in enumerate(ids) if k != 3]
    for doc_id, text, _dom in got:
        want_index = int(doc_id[1:])
        assert f"REWRITE-{want_index} " in text, (
            f"{doc_id} carries {text[:30]!r} — the row alignment shifted, which is exactly what "
            f"list_flatten's dropping of empty lists causes if the values are zipped in order"
        )


def test_several_empty_lists_including_the_first_row():
    """The first row being empty is the case where an off-by-one is most likely to look correct."""
    import pyarrow as pa

    lists = [[] , [{"text": "one text body here"}], [], [{"text": "three text body here"}],
             [{"text": "four text body here"}], []]
    body = _write(pa.table({
        "id": pa.array([f"r{i}" for i in range(6)]),
        "rollout_results": pa.array(lists, type=pa.list_(pa.struct([("text", pa.string())]))),
    }))
    spec = _spec(text_column="rollout_results.list.element.text")
    got = _assert_identical(body, spec, expect_min=3)
    assert [g[0] for g in got] == ["r1", "r3", "r4"]
    assert [g[1] for g in got] == ["one text body here", "three text body here",
                                   "four text body here"]


def test_an_all_empty_column_yields_no_documents_rather_than_raising():
    """Legal parquet, and it means "no rewrites in this file". A row to skip and count, not a crash —
    `filter_documents` is where losses are reported."""
    import pyarrow as pa

    body = _write(pa.table({
        "id": pa.array(["a", "b", "c"]),
        "rollout_results": pa.array([[], [], []],
                                    type=pa.list_(pa.struct([("text", pa.string())]))),
    }))
    spec = _spec(text_column="rollout_results.list.element.text")
    assert _new_docs(body, spec) == []
    assert _reference_docs(body, spec) == []


def test_a_NULL_struct_field_matches_the_old_path_too():
    """A present list whose struct's `text` is null. `_walk` returned None and the row was skipped;
    the arrow path must agree rather than yielding an empty string."""
    import pyarrow as pa

    body = _write(pa.table({
        "id": pa.array(["a", "b", "c"]),
        "rollout_results": pa.array(
            [[{"text": "real body text here"}], [{"text": None}], [{"text": "third body text"}]],
            type=pa.list_(pa.struct([("text", pa.string())]))),
    }))
    spec = _spec(text_column="rollout_results.list.element.text")
    got = _assert_identical(body, spec, expect_min=2)
    assert [g[0] for g in got] == ["a", "c"]


def test_a_MULTI_element_list_takes_only_the_first_exactly_as_the_walk_did():
    """`_walk`'s `[0]` takes the first element. FinePhrase's lists are all length 1 (min == max == 1
    over 160 row groups, all four configs) so this discards nothing there — but a 2-element list must
    contribute ONE document, not two, or the corpus silently gains rows."""
    import pyarrow as pa

    body = _write(pa.table({
        "id": pa.array(["a", "b"]),
        "rollout_results": pa.array(
            [[{"text": "FIRST body text here"}, {"text": "SECOND body text here"}],
             [{"text": "only body text here"}]],
            type=pa.list_(pa.struct([("text", pa.string())]))),
    }))
    spec = _spec(text_column="rollout_results.list.element.text")
    got = _assert_identical(body, spec, expect_min=2)
    assert len(got) == 2, "a 2-element list must yield ONE document"
    assert got[0][1] == "FIRST body text here"
    assert "SECOND" not in got[0][1]


# ======================================================================================
# 3. GENERALITY — nothing is FinePhrase-specific
# ======================================================================================


def test_a_DEEPER_nesting_than_finephrase_works_with_no_new_code():
    """The extraction is driven by the walk `_compile_walk` derives from the file's own footer, so
    depth is data. A hardcoded `rollout_results` would have to be re-hardcoded for the next nested
    source and the next session would not know to look."""
    import pyarrow as pa

    inner = pa.struct([("payload", pa.struct([("body", pa.string())]))])
    body = _write(pa.table({
        "id": pa.array(["a", "b", "c"]),
        "wrap": pa.array([[{"payload": {"body": f"deep body {i} here"}}] for i in range(3)],
                         type=pa.list_(inner)),
    }))
    spec = _spec(text_column="wrap.list.element.payload.body")
    got = _assert_identical(body, spec, expect_min=3)
    assert [g[1] for g in got] == [f"deep body {i} here" for i in range(3)]


def test_a_struct_with_no_list_at_all_works():
    """Nested but not repeated — `meta.body`. No `[0]` step, so the extraction is a plain field walk
    and there is no flatten to get wrong."""
    import pyarrow as pa

    body = _write(pa.table({
        "id": pa.array(["a", "b"]),
        "meta": pa.array([{"body": "first body text"}, {"body": "second body text"}],
                         type=pa.struct([("body", pa.string())])),
    }))
    spec = _spec(text_column="meta.body")
    got = _assert_identical(body, spec, expect_min=2)
    assert [g[1] for g in got] == ["first body text", "second body text"]


def test_large_string_columns_work_because_two_sources_ship_them():
    """`Nemotron-Pretraining-Specialized-v1` uses arrow's 64-bit-offset `large_string`, MEASURED by
    DATA — "any reader that asserts pa.string() fails here"."""
    import pyarrow as pa

    body = _write(pa.table({
        "id": pa.array(["a", "b"], type=pa.large_string()),
        "text": pa.array(["first large body", "second large body"], type=pa.large_string()),
    }))
    got = _assert_identical(body, _spec(), expect_min=2)
    assert [g[1] for g in got] == ["first large body", "second large body"]


def test_a_large_list_of_struct_works_too():
    """`large_list` is the 64-bit sibling of `list`, and `_compile_walk` already accepts it — so the
    extraction must too, or a source compiles and then fails at read time."""
    import pyarrow as pa

    body = _write(pa.table({
        "id": pa.array(["a", "b"]),
        "rr": pa.array([[{"text": "one body text"}], [{"text": "two body text"}]],
                       type=pa.large_list(pa.struct([("text", pa.string())]))),
    }))
    spec = _spec(text_column="rr.list.element.text")
    got = _assert_identical(body, spec, expect_min=2)
    assert [g[1] for g in got] == ["one body text", "two body text"]


# ======================================================================================
# 4. The helper's own refusals
# ======================================================================================


def test_the_extractor_refuses_a_walk_that_disagrees_with_the_table():
    """A walk compiled against a different schema than the table was projected from. Refused rather
    than returning nulls, which would read as a file full of payload-less rows."""
    import pyarrow as pa

    table = pa.table({"text": pa.array(["a body", "b body"])})
    with pytest.raises(ReadError, match="not a list"):
        _arrow_column_values(table, ("text", "[0]"))
    with pytest.raises(ReadError, match="not a struct"):
        _arrow_column_values(table, ("text", "field"))
    with pytest.raises(ReadError, match="empty walk"):
        _arrow_column_values(table, ())


def test_the_extractor_returns_one_value_per_TABLE_row_always():
    """Length is the invariant the caller's `enumerate` depends on: the returned list is indexed by
    row position, so it must be exactly as long as the table even when values are missing."""
    import pyarrow as pa

    lists = [[{"text": "x body"}], [], [{"text": "y body"}], []]
    table = pa.table({"rr": pa.array(lists, type=pa.list_(pa.struct([("text", pa.string())])))})
    vals = _arrow_column_values(table, ("rr", "[0]", "text"))
    assert len(vals) == 4
    assert vals == ["x body", None, "y body", None]


# ======================================================================================
# 5. The speed claim — asserted as a DIRECTION, not as a threshold
# ======================================================================================


@pytest.mark.parametrize("n", [4000])
def test_the_nested_read_is_no_longer_dramatically_slower_than_the_flat_one(n):
    """The reason this change exists, measured — but asserted LOOSELY on purpose.

    A CI machine's absolute timings are not ours, so a threshold like "2.4x" would be a flake
    generator. What is asserted is the DIRECTION and a wide bound: the arrow-native nested read must
    not be more than 2x the flat baseline, where `to_pylist()` measured 2.3-3.6x. Locally this run
    gives ~0.94x flat and 2.46x faster than the old nested path.

    The correctness tests above are what actually guard this change; this one guards the MOTIVE.
    """
    import time

    import pyarrow as pa
    import pyarrow.parquet as pq

    texts = [f"rewrite document {i} " * 40 for i in range(n)]
    nested = pa.table({
        "id": pa.array([f"d{i}" for i in range(n)]),
        "text": pa.array([f"ORIGINAL source {i} " * 40 for i in range(n)]),
        "rollout_results": pa.array([[{"text": t}] for t in texts],
                                    type=pa.list_(pa.struct([("text", pa.string())]))),
    })
    flat = pa.table({"id": pa.array([f"d{i}" for i in range(n)]), "text": pa.array(texts)})
    bn, bf = io.BytesIO(), io.BytesIO()
    pq.write_table(nested, bn, row_group_size=n)
    pq.write_table(flat, bf, row_group_size=n)

    def best(fn, reps=3):
        return min(_timed(fn) for _ in range(reps))

    def _timed(fn):
        t = time.perf_counter()
        fn()
        return time.perf_counter() - t

    nspec = _spec(text_column="rollout_results.list.element.text")
    t_flat = best(lambda: list(read_parquet_documents(
        "owner/name", "f.parquet", _spec(), fileobj=io.BytesIO(bf.getvalue()))))
    t_nested = best(lambda: list(read_parquet_documents(
        "owner/name", "f.parquet", nspec, fileobj=io.BytesIO(bn.getvalue()))))
    t_old = best(lambda: _reference_docs(bn.getvalue(), nspec))

    assert t_nested < t_flat * 2.0, (
        f"nested {t_nested*1000:.1f} ms vs flat {t_flat*1000:.1f} ms = {t_nested/t_flat:.2f}x. The "
        f"whole point of the arrow-native extraction is that this ratio is ~1x; to_pylist gave "
        f"2.3-3.6x."
    )
    assert t_nested < t_old, (
        f"the arrow path ({t_nested*1000:.1f} ms) is not faster than the to_pylist path "
        f"({t_old*1000:.1f} ms) — the change has no motive left"
    )
