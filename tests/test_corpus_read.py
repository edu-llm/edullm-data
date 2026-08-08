"""Tests for the document-reader stage. Mirrors `src/edullm_data/corpus_read.py`.

NOTHING HERE TOUCHES THE NETWORK. Both readers take an injection seam — `fileobj=` for parquet,
`chunks=` for gzip — so the parquet tests build REAL parquet bytes with pyarrow in memory and the
gzip tests build real gzip members with `gzip.compress`. That is deliberate rather than
convenient: the duplicate-leaf trap is a property of how pyarrow resolves a column against a real
footer, and a mocked reader would test the mock's opinion of the schema instead of pyarrow's. The
transport itself is already covered by `test_ingest_reservoir.py`, which owns `_RangeFile`.

THE CENTREPIECE IS `test_duplicate_leaf_name_resolves_to_the_requested_path`. FinePhrase's
top-level `text` holds the ORIGINAL FineWeb-Edu document while the synthetic rewrite is at
`rollout_results.list.element.text`, the flat leaf list therefore contains `text` twice, and
reading the wrong one produces a corpus that is real web text labelled synthetic — internally
consistent with its own digest, its own size, and its own decode check. No check anywhere in this
package can catch it after the fact, so it has to be caught here.
"""

from __future__ import annotations

import gzip
import io
import json

import pytest

from edullm_data.corpus import MIN_DOC_TOKENS, MIN_MEAN_DOC_TOKENS, CorpusSpec, Document
from edullm_data.corpus_read import (
    FilterStats,
    ReadError,
    filter_documents,
    read_documents,
    read_jsonl_gz_documents,
    read_parquet_documents,
)

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")


# --------------------------------------------------------------------------------------
# Fixtures: real bytes, not mocks
# --------------------------------------------------------------------------------------

#: FinePhrase's measured shape, reduced to the fields that matter. `rollout_results` is a LIST of
#: STRUCT{finish_reason, text, usage{...}} and the top-level `text` is the ORIGINAL — both
#: confirmed by direct query against the live dataset, not inferred from a card
#: (`artifacts/recount/synthetic.json:9`).
_FINEPHRASE_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string()),
        pa.field("text", pa.string()),  # the ORIGINAL FineWeb-Edu document
        pa.field("dataset", pa.string()),
        pa.field("token_count", pa.int64()),
        pa.field(
            "rollout_results",
            pa.list_(
                pa.struct(
                    [
                        pa.field("finish_reason", pa.string()),
                        pa.field("text", pa.string()),  # the REWRITE — the payload we want
                        pa.field(
                            "usage",
                            pa.struct([pa.field("completion_tokens", pa.int64())]),
                        ),
                    ]
                )
            ),
        ),
    ]
)


def _finephrase_parquet(*, rows: int = 3, empty_rollout_at: int | None = 2, compliant: bool = True):
    """Parquet bytes shaped exactly like FinePhrase, with distinguishable original/rewrite text.

    `empty_rollout_at` gives one row an EMPTY `rollout_results` list, which is legal in parquet and
    means the row has no rewrite. `compliant` selects pyarrow's nested naming: `list.element` when
    True (its default, and what FinePhrase uses) vs the legacy `list.item` when False.
    """
    table = pa.table(
        {
            "id": [f"doc-{i}" for i in range(rows)],
            "text": [f"ORIGINAL-{i} " + "real web prose " * 20 for i in range(rows)],
            "dataset": ["HuggingFaceFW/fineweb-edu"] * rows,
            "token_count": [1100] * rows,
            "rollout_results": [
                []
                if i == empty_rollout_at
                else [
                    {
                        "finish_reason": "stop",
                        "text": f"REWRITE-{i} " + "rephrased prose " * 20,
                        "usage": {"completion_tokens": 60},
                    }
                ]
                for i in range(rows)
            ],
        },
        schema=_FINEPHRASE_SCHEMA,
    )
    buf = io.BytesIO()
    # Two rows per group so the row-group loop is genuinely exercised rather than degenerate.
    pq.write_table(table, buf, row_group_size=2, use_compliant_nested_type=compliant)
    buf.seek(0)
    return buf


def _spec(**overrides) -> CorpusSpec:
    base = {
        "key": "synthetic-finephrase-faq",
        "category": "synthetic",
        "source_label": "synthetic-finephrase-faq",
        "repo": "HuggingFaceFW/finephrase",
        "file_format": "parquet",
        "text_column": "rollout_results.list.element.text",
        "id_column": "id",
        "target_tokens": 15_000_000_000,
    }
    return CorpusSpec(**{**base, **overrides})


def _read(spec: CorpusSpec, buf, **kwargs) -> list[Document]:
    return list(read_parquet_documents("repo", "file.parquet", spec, {}, fileobj=buf, **kwargs))


# --------------------------------------------------------------------------------------
# THE POINT OF THE EXERCISE: the duplicate leaf name
# --------------------------------------------------------------------------------------


def test_duplicate_leaf_name_resolves_to_the_requested_path_not_the_first_match():
    """THE test. A schema with `text` at TWO leaf paths must resolve to the one asked for.

    The failure this prevents is not a crash — it is a corpus of unrephrased FineWeb-Edu labelled
    synthetic, whose bytes are real text, tokenize correctly, and agree with their own sha256.
    Nothing downstream can detect it. First the trap is shown to exist against these very bytes,
    then each path is shown to resolve independently.
    """
    buf = _finephrase_parquet()
    parquet_file = pq.ParquetFile(buf, pre_buffer=False)
    row_group = parquet_file.metadata.row_group(0)
    leaves = [row_group.column(c).path_in_schema for c in range(row_group.num_columns)]

    # The trap, recomputed rather than asserted from the docstring: `text` really is ambiguous in
    # the FLAT list, and the naive `.index("text")` really does select the original.
    assert leaves.count("text") == 1
    assert "rollout_results.list.element.text" in leaves
    flat_names = parquet_file.schema.names
    assert flat_names.count("text") == 2, "the flat leaf list must contain `text` twice"
    assert flat_names.index("text") == 1, "the FIRST match is the ORIGINAL, at index 1"
    assert parquet_file.schema.column(flat_names.index("text")).path == "text", (
        "so `.names.index('text')` selects the top-level ORIGINAL, which is the bug"
    )

    buf.seek(0)
    rewrites = _read(_spec(text_column="rollout_results.list.element.text"), buf)
    assert [d.text.split()[0] for d in rewrites] == ["REWRITE-0", "REWRITE-1"]
    assert all("ORIGINAL" not in d.text for d in rewrites), (
        "resolving the nested leaf must NOT return the top-level original"
    )

    # The complement: asking for the original returns the original. A reader that always returned
    # the nested leaf would pass the assertion above while being just as wrong.
    buf.seek(0)
    originals = _read(_spec(text_column="text"), buf)
    assert [d.text.split()[0] for d in originals] == ["ORIGINAL-0", "ORIGINAL-1", "ORIGINAL-2"]
    assert all("REWRITE" not in d.text for d in originals)


@pytest.mark.parametrize(
    "wrong",
    [
        "rollout_results.text",  # the intuitive spelling; selects NOTHING
        "rollout_results.list.item.text",  # the legacy spelling, wrong for this file
        "rollout_results",  # the parent, not the leaf
        "rollout_results.list.element.tekst",  # a typo
    ],
)
def test_a_wrong_nested_selector_raises_instead_of_reading_an_empty_corpus(wrong):
    """Verified by execution: pyarrow does NOT raise on these.

    `read_row_group(columns=["rollout_results.text"])` returns a table with ZERO columns and
    `to_pylist()` of `[{}]` — so a reader that trusted pyarrow's silence would emit an empty
    corpus and look like a source that simply had no data. Exact-match resolution against
    `path_in_schema` is what converts that into an error.
    """
    with pytest.raises(ReadError, match="does not name a leaf"):
        _read(_spec(text_column=wrong), _finephrase_parquet())


def test_pyarrow_really_is_silent_about_the_wrong_selector():
    """The premise behind the test above, measured rather than asserted.

    If a future pyarrow starts raising on an unknown nested selector, this fails and the guard
    above can be relaxed — which is exactly the signal worth having, since the guard's cost is a
    schema walk on every file.
    """
    parquet_file = pq.ParquetFile(_finephrase_parquet(), pre_buffer=False)
    table = parquet_file.read_row_group(0, columns=["rollout_results.text"])
    assert table.num_columns == 0, "pyarrow silently selected nothing"
    assert table.to_pylist() == [{}, {}], "and produced rows with no fields, with no error"


def test_the_legacy_list_spelling_is_read_when_the_file_uses_it():
    """`list.item` vs `list.element` is a writer choice, and both exist upstream.

    Verified: the same table written with `use_compliant_nested_type=False` yields
    `rr.list.item.text`. Hard-coding one spelling would fail on the other with an EMPTY read
    rather than an error, so the reader takes the spelling from the file's own footer.
    """
    docs = _read(
        _spec(text_column="rollout_results.list.item.text"),
        _finephrase_parquet(compliant=False),
    )
    assert [d.text.split()[0] for d in docs] == ["REWRITE-0", "REWRITE-1"]


def test_a_duplicate_TOP_LEVEL_column_name_is_refused_rather_than_guessed():
    """pyarrow keeps the LAST field of a duplicated top-level name, silently.

    Measured: a file with `text: string` then `text: int64`, read with `columns=["text"]`, returns
    `[{'text': 7}]` — the int. There is no selector that expresses "the first one", so a reader
    cannot honour the request and must refuse.
    """
    schema = pa.schema([pa.field("id", pa.string()), pa.field("text", pa.string()),
                        pa.field("text", pa.int64())])
    table = pa.table([pa.array(["a"]), pa.array(["real text"]), pa.array([7])], schema=schema)
    buf = io.BytesIO()
    pq.write_table(table, buf)
    buf.seek(0)
    with pytest.raises(ReadError, match="appears 2 times"):
        _read(_spec(text_column="text"), buf)


# --------------------------------------------------------------------------------------
# Parquet reading: nesting, streaming, and the columns actually requested
# --------------------------------------------------------------------------------------


def test_only_the_needed_columns_are_requested():
    """A payload read must not drag `usage`, `dataset` or `token_count` over the wire.

    `rollout_results` carries SIX leaves; summing the subtree overcounts by 1.0-1.7%
    (`artifacts/recount/synthetic.json:16`) and pulling it transfers chunks nobody reads. This
    asserts on the real `columns=` argument by recording what the reader asked pyarrow for.
    """
    real = pq.ParquetFile(_finephrase_parquet(), pre_buffer=False)
    seen: list[list[str]] = []

    class _Recording:
        """Delegates everything to a real ParquetFile, recording only the `columns=` request.

        A wrapper rather than a mock: the schema, the footer and the decoded values all come from
        real parquet bytes, so this measures what the reader ASKS FOR without changing what it
        gets. `_open_parquet` accepts the object as-is on the `fileobj` seam, which is what makes
        the spy possible without a second code path.
        """

        metadata = real.metadata
        schema = real.schema
        schema_arrow = real.schema_arrow

        def read_row_group(self, i, columns=None, **kw):
            seen.append(list(columns or []))
            return real.read_row_group(i, columns=columns, **kw)

    docs = list(
        read_parquet_documents("repo", "f.parquet", _spec(), {}, parquet_file=_Recording())
    )
    assert docs, "the recording wrapper must still produce documents"
    assert seen, "read_row_group was never called"
    for columns in seen:
        assert columns == ["rollout_results.list.element.text", "id"]
        assert "usage" not in " ".join(columns), "the sibling usage struct must never be fetched"
        assert "token_count" not in columns and "dataset" not in columns


def test_the_projection_makes_a_fallback_to_the_original_text_UNREACHABLE():
    """Defence in depth, found by mutation testing: projecting the columns is a SECOND barrier.

    Injecting `text = _walk(row, walk) or row.get("text")` — a fallback to the top-level original,
    i.e. the duplicate-leaf trap arriving one row at a time — did not change a single test result.
    The reason is measured here: because only the requested leaves are projected, the row dict
    handed to the walker contains ONLY `rollout_results` and `id`, so `row.get("text")` is `None`
    and the original document is not reachable at all.

    That makes the projection load-bearing for correctness, not just for bytes transferred. This
    test pins it, so the two barriers cannot silently collapse into one: with `columns=None` the
    fallback WOULD find the original, which is why
    `test_only_the_needed_columns_are_requested` is also a correctness test.
    """
    projected = pq.ParquetFile(_finephrase_parquet(), pre_buffer=False).read_row_group(
        0, columns=["rollout_results.list.element.text", "id"]
    )
    assert projected.schema.names == ["rollout_results", "id"]
    for row in projected.to_pylist():
        assert "text" not in row, (
            "the projected row must not carry the top-level ORIGINAL text, or a fallback could "
            "silently substitute unrephrased FineWeb-Edu for the synthetic corpus"
        )
        assert "ORIGINAL" not in json.dumps(row)


def test_an_empty_rollout_list_is_skipped_not_crashed_and_not_mislabelled():
    """A row with `rollout_results == []` has no rewrite. Legal in parquet, verified.

    It must not raise, and it must not silently fall back to the top-level `text` — falling back
    is the duplicate-leaf bug arriving through the back door, one row at a time.
    """
    docs = _read(_spec(), _finephrase_parquet(rows=3, empty_rollout_at=2))
    assert [d.id for d in docs] == ["doc-0", "doc-1"], "the rewrite-less row is dropped"
    assert all("ORIGINAL" not in d.text for d in docs)


def test_element_zero_is_taken_from_the_list():
    """One rollout per document, always — min = max = 1 over 842,000 rows and in every one of 160
    row groups sampled across all four configs (`artifacts/recount/synthetic.json:9,11`). So
    element 0 is the entire payload. If a file ever carries two, this pins the behaviour that is
    actually implemented rather than leaving it to be discovered."""
    schema = pa.schema(
        [pa.field("id", pa.string()),
         pa.field("rr", pa.list_(pa.struct([pa.field("text", pa.string())])))]
    )
    table = pa.table({"id": ["x"], "rr": [[{"text": "FIRST"}, {"text": "SECOND"}]]}, schema=schema)
    buf = io.BytesIO()
    pq.write_table(table, buf)
    buf.seek(0)
    docs = _read(_spec(text_column="rr.list.element.text"), buf)
    assert [d.text for d in docs] == ["FIRST"]


def test_a_field_genuinely_named_element_below_the_list_marker_still_resolves():
    """The marker segments must be consumed positionally, not filtered out by name.

    Measured: `list<struct<element: struct<text>>>` has the leaf
    `rr.list.element.element.text`. Stripping every marker-named segment eats both hops and then
    reads a sibling or raises — a real bug this test caught during implementation.
    """
    inner = pa.struct([pa.field("text", pa.string())])
    schema = pa.schema(
        [pa.field("id", pa.string()),
         pa.field("rr", pa.list_(pa.struct([pa.field("element", inner)])))]
    )
    table = pa.table({"id": ["x"], "rr": [[{"element": {"text": "DEEP"}}]]}, schema=schema)
    buf = io.BytesIO()
    pq.write_table(table, buf)
    buf.seek(0)
    assert [d.text for d in _read(_spec(text_column="rr.list.element.element.text"), buf)] == ["DEEP"]


def test_every_row_group_is_read():
    """Streaming must not stop after the first group. With `row_group_size=2` and 5 rows there are
    three groups, and a loop that read only group 0 would return a plausible 2-document corpus."""
    buf = _finephrase_parquet(rows=5, empty_rollout_at=None)
    assert pq.ParquetFile(buf, pre_buffer=False).metadata.num_row_groups == 3
    buf.seek(0)
    assert len(_read(_spec(), buf)) == 5


def test_an_integer_id_column_is_accepted_and_stringified():
    """A deliberate asymmetry with the text/domain columns.

    An integer post id is legitimate upstream (StackExchange), and `Document.id` needs exactly one
    property from it: stability across a re-download (`corpus.py:176-179`). `str(int)` has that.
    The text and domain columns are held to real strings because a coerced `repr` would be
    tokenized in one case and become a permanent path segment inside `manifest_sha256` in the other.
    """
    schema = pa.schema([pa.field("id", pa.int64()), pa.field("text", pa.string())])
    table = pa.table({"id": [101, 102], "text": ["body " * 40] * 2}, schema=schema)
    buf = io.BytesIO()
    pq.write_table(table, buf)
    buf.seek(0)
    docs = _read(_spec(text_column="text"), buf)
    assert [d.id for d in docs] == ["101", "102"]
    assert all(isinstance(d.id, str) for d in docs)


def test_a_non_string_text_column_is_refused_up_front():
    """Refused at open time, not per row: a numeric column would tokenize its `repr`."""
    with pytest.raises(ReadError, match="not a string"):
        _read(_spec(text_column="token_count"), _finephrase_parquet())


def test_a_null_id_raises_because_the_id_is_a_join_key():
    """`Document.id` is the join key for the §9.7 item 4 partition and the FineWeb-Edu anti-join
    (`corpus.py:176-179`). Substituting a row index would make the partition non-reproducible
    across a re-download, so a null id must stop the build rather than be invented."""
    schema = pa.schema([pa.field("id", pa.string()), pa.field("text", pa.string())])
    table = pa.table({"id": ["ok", None], "text": ["a" * 50, "b" * 50]}, schema=schema)
    buf = io.BytesIO()
    pq.write_table(table, buf)
    buf.seek(0)
    with pytest.raises(ReadError, match="join key"):
        _read(_spec(text_column="text"), buf)


def test_reading_over_http_range_requires_a_size():
    """`_RangeFile` cannot work without it, and `hf_tree` guarantees the field is present
    (`ingest_reservoir.py:483-489`) — so its absence is a caller bug, caught before any request."""
    with pytest.raises(ReadError, match="needs the object size"):
        list(read_parquet_documents("repo", "f.parquet", _spec(), {}))


def test_an_hf_tree_entry_dict_supplies_the_size():
    """Callers hold `hf_tree` entries, not bare paths; accepting the dict avoids a lossy unpack at
    every call site. Exercised without network by asserting on the failure that comes AFTER the
    size is accepted."""
    with pytest.raises(Exception) as excinfo:
        list(read_parquet_documents(
            "repo", {"path": "f.parquet", "size": 1234}, _spec(), {"User-Agent": "t"},
        ))
    assert "needs the object size" not in str(excinfo.value), (
        "the size must have been taken from the entry dict"
    )


def test_pre_buffer_false_is_passed_at_this_modules_call_site():
    """`pre_buffer=False` is the array-SIGSEGV fix and nothing else in THIS module asserts it.

    `pre_buffer=True` dispatches range reads onto Arrow's native C++ IO thread pool, where each
    becomes a full `urlopen` inside a C++ callback: exit 139 in 3 of 4 array children, vs 0 of 4
    with it disabled (A/B on Batch, 2026-07-31; `ingest_reservoir.py:641-664`).

    A source-level assertion because the keyword is INVISIBLE otherwise — and because the default
    is a per-API, per-version accident. Measured on the installed pyarrow 24.0.0:
    `ParquetFile.__init__` defaults to False while `pq.read_table` and `pq.ParquetDataset` default
    to True, and `_scan_ids`'s docstring records the ParquetFile default as True on 25.0.0, which
    is what production resolves unpinned. So an edit that drops the keyword may pass locally and
    segfault on Batch.
    """
    import inspect
    import re

    from edullm_data import corpus_read

    source = inspect.getsource(corpus_read._open_parquet)
    # Every real `pq.ParquetFile(...)` construction, ignoring prose in the docstring.
    constructions = re.findall(r"pq\.ParquetFile\(([^)]*)\)", source)
    assert constructions, "no pq.ParquetFile call site found — did the function move?"
    for args in constructions:
        assert "pre_buffer=False" in args, (
            f"pq.ParquetFile({args}) must pass pre_buffer=False explicitly. The default is a "
            f"per-API, per-version accident, and True SIGSEGVs every array child."
        )
    # Both the HTTP path and the bytes seam must construct it the same way, or the behavioural
    # tests below exercise a configuration production never runs.
    assert len(constructions) == 2, (
        f"expected the _RangeFile path and the fileobj seam, found {len(constructions)} call sites"
    )


# --------------------------------------------------------------------------------------
# The inherited `domain` segment — §1.2
# --------------------------------------------------------------------------------------


def _stackexchange_parquet(sites):
    schema = pa.schema(
        [
            pa.field("id", pa.string()),
            pa.field("text", pa.string()),
            pa.field(
                "metadata",
                pa.struct([pa.field("site", pa.string()), pa.field("license", pa.string())]),
            ),
        ]
    )
    table = pa.table(
        {
            "id": [f"q-{i}" for i in range(len(sites))],
            "text": ["question body " * 20] * len(sites),
            "metadata": [{"site": s, "license": "CC-BY-SA"} for s in sites],
        },
        schema=schema,
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    buf.seek(0)
    return buf


def _se_spec(**kw) -> CorpusSpec:
    return _spec(
        **{
            "key": "stackexchange",
            "source_label": "stackexchange",
            "text_column": "text",
            "domain_column": "metadata.site",
            **kw,
        }
    )


def test_a_flat_source_yields_domain_none():
    """`None` is CORRECT for most sources, not missing data: §1.2 grants a domain segment if and
    only if the source SHIPS one upstream (`corpus.py:180-182`)."""
    assert all(d.domain is None for d in _read(_spec(), _finephrase_parquet()))


def test_an_inherited_domain_is_slugged_before_it_becomes_a_path_segment():
    """`C#` in a key silently truncates any `s3://` URI at the fragment delimiter, and
    `labels_from_path` plus `fnmatch` both accept it — so nothing in the pipeline catches it and it
    breaks at read time in a consumer (§1.2 landmine 1)."""
    docs = _read(_se_spec(), _stackexchange_parquet(["C#", "mathoverflow"]))
    assert [d.domain for d in docs] == ["c-sharp", "mathoverflow"]


def test_the_cardinality_fold_maps_the_tail_to_other():
    """§1.2 landmine 2: every distinct value is a permanent directory inside `manifest_sha256`.
    73 `gha_language` values and ~180 StackExchange sites are the verified tails, so the top ~20
    keep their names and the rest fold. The map must come from `build_domain_slug_map`, which is
    where the COLLISION check lives (`manifest.py:1149`)."""
    from edullm_data.manifest import build_domain_slug_map

    slug_map = build_domain_slug_map(
        {"mathoverflow": 900, "physics": 800, "tex": 3, "ru": 2}, keep=2
    )
    # `folded` is highest-ranked first, so tex (3) precedes ru (2).
    assert slug_map.folded == ("tex", "ru")
    docs = _read(
        _se_spec(), _stackexchange_parquet(["mathoverflow", "tex", "ru"]),
        domain_map=slug_map.slug_of,
    )
    assert [d.domain for d in docs] == ["mathoverflow", "other", "other"]


def test_a_value_absent_from_the_map_folds_to_other_rather_than_creating_a_directory():
    """A value first seen AFTER the counting pass is by construction outside the top `keep`. It
    must land in `other`, not mint a 21st permanent directory that nothing planned for."""
    from edullm_data.manifest import build_domain_slug_map

    slug_map = build_domain_slug_map({"mathoverflow": 900, "physics": 800}, keep=2)
    docs = _read(
        _se_spec(), _stackexchange_parquet(["mathoverflow", "brand-new-site"]),
        domain_map=slug_map.slug_of,
    )
    assert [d.domain for d in docs] == ["mathoverflow", "other"]


def test_a_missing_domain_value_folds_rather_than_going_flat_mid_source():
    """A flat key and a nested key are DIFFERENT label sets to `labels_from_path`, so mixing depths
    inside ONE source would make `labels={'domain': ...}` skip those rows invisibly — §1.2's
    "a `domain=` query silently drops flat sources", arriving within a single source."""
    from edullm_data.manifest import build_domain_slug_map

    slug_map = build_domain_slug_map({"mathoverflow": 900, "physics": 800}, keep=2)
    docs = _read(
        _se_spec(), _stackexchange_parquet(["mathoverflow", ""]), domain_map=slug_map.slug_of
    )
    assert [d.domain for d in docs] == ["mathoverflow", "other"]
    assert all(d.domain is not None for d in docs), "depth must stay uniform within a source"


def test_a_dns_suffix_is_stripped_from_an_inherited_site():
    """`3dprinting.stackexchange.com` -> `3dprinting`, per §1.2. Delegated to
    `slug_path_segment`, asserted here because the reader is what feeds it."""
    docs = _read(_se_spec(), _stackexchange_parquet(["3dprinting.stackexchange.com"]))
    assert [d.domain for d in docs] == ["3dprinting"]


def test_an_unsluggable_domain_value_raises_rather_than_dropping_characters():
    """Dropping an unknown character is how `C#` and `C++` both become `c` and two languages merge
    into one permanent directory. `slug_path_segment` refuses; the reader must not swallow that."""
    from edullm_data.manifest import SlugError

    with pytest.raises((SlugError, ReadError)):
        _read(_se_spec(), _stackexchange_parquet(["中文"]))


def test_a_non_string_domain_leaf_is_refused_before_any_row_is_read():
    """An int reaching `slug_path_segment` raises per-row, mid-build. Refused up front, where the
    message can name the field that is wrong."""
    schema = pa.schema(
        [pa.field("id", pa.string()), pa.field("text", pa.string()), pa.field("rank", pa.int64())]
    )
    table = pa.table({"id": ["a"], "text": ["body " * 40], "rank": [7]}, schema=schema)
    buf = io.BytesIO()
    pq.write_table(table, buf)
    buf.seek(0)
    with pytest.raises(ReadError, match="not a string"):
        _read(_se_spec(domain_column="rank"), buf)


def test_naming_a_struct_PARENT_as_the_domain_column_is_refused():
    """`metadata` is not a leaf — only `metadata.site` and `metadata.license` are. Caught by leaf
    resolution rather than by the type check, which is the earlier and better place: the message
    lists the real leaves so the registry row can be corrected."""
    with pytest.raises(ReadError, match="does not name a leaf"):
        _read(_se_spec(domain_column="metadata"), _stackexchange_parquet(["x"]))


def test_the_domain_column_is_also_resolved_by_exact_leaf_path():
    """The duplicate-leaf discipline is not text-column-specific. `metadata.site` and a top-level
    `site` would be two different columns, and the domain lands inside `manifest_sha256`."""
    with pytest.raises(ReadError, match="does not name a leaf"):
        _read(_se_spec(domain_column="site"), _stackexchange_parquet(["x"]))


# --------------------------------------------------------------------------------------
# `.json.gz` — the path that did not exist before, and the flagged risk
# --------------------------------------------------------------------------------------


def _records(n: int = 4, *, site: str | None = None, words: int = 100) -> list[dict]:
    out = []
    for i in range(n):
        record = {"id": f"pes2o-{i}", "text": " ".join(["word"] * words), "added": "2026-01-01"}
        if site is not None:
            record["metadata"] = {"site": site}
        out.append(record)
    return out


def _jsonl_bytes(records) -> bytes:
    return ("\n".join(json.dumps(r) for r in records) + "\n").encode("utf-8")


def _jspec(**kw) -> CorpusSpec:
    return _spec(
        **{
            "key": "pes2o",
            "source_label": "pes2o",
            "file_format": "json.gz",
            "text_column": "text",
            "id_column": "id",
            **kw,
        }
    )


def _read_gz(chunks, spec=None) -> list[Document]:
    return list(
        read_jsonl_gz_documents("repo", "pes2o-0000.json.gz", spec or _jspec(), {}, chunks=chunks)
    )


def test_a_single_gzip_member_reads_every_document():
    """Every Common Pile source ships `.json.gz` and NO parquet — verified, `peS2o_filtered` has
    93 files and 0 parquet (`artifacts/smoke/harvest_parquet.py:125`). That is the academic, code
    and QA/forum categories, so this path is not optional."""
    docs = _read_gz([gzip.compress(_jsonl_bytes(_records(4)))])
    assert [d.id for d in docs] == [f"pes2o-{i}" for i in range(4)]
    assert all(d.source == "pes2o" and d.domain is None for d in docs)


def test_a_partial_trailing_line_is_carried_across_chunks_never_parsed():
    """A range boundary lands mid-line with near-certainty.

    Prior art in this repo swallows the resulting `json.loads` failure and moves on
    (`artifacts/recount/_filtered_tpb.py:83`), which is right for a sampler and would silently
    drop one real document per boundary in a build. Feeding the member in 7-byte chunks puts a
    boundary inside nearly every line; all documents must still arrive, in order.
    """
    # Distinct per-record text so gzip cannot collapse the fixture into a handful of chunks.
    records = [
        {"id": f"pes2o-{i}", "text": f"unique-body-{i} " + f"{i} filler words here " * 30}
        for i in range(6)
    ]
    body = gzip.compress(_jsonl_bytes(records))
    tiny = [body[i : i + 7] for i in range(0, len(body), 7)]
    assert len(tiny) > 20, "the fixture must actually be split into many chunks"
    docs = _read_gz(tiny)
    assert [d.id for d in docs] == [f"pes2o-{i}" for i in range(6)]
    assert [d.text.split()[0] for d in docs] == [f"unique-body-{i}" for i in range(6)], (
        "each document's own bytes must be reassembled, not just the right count"
    )


def test_a_truncated_download_raises_instead_of_yielding_a_short_corpus():
    """THE silent-data-loss check, and the reason `decompressobj.eof` is inspected.

    Verified by execution: on a truncated gzip stream `decompressobj` does NOT raise — it returns
    every byte it could decode and sets `eof = False`, where `gzip.decompress` raises `EOFError`.
    So without this check a cut-off download decompresses cleanly into a short corpus that reads
    as a small-but-valid source, and the loss is invisible forever.
    """
    whole = gzip.compress(_jsonl_bytes(_records(6)))
    truncated = whole[: len(whole) - 6]
    # The premise: the bytes really do decode without error.
    import zlib

    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    decompressor.decompress(truncated)
    assert decompressor.eof is False, "a truncated stream must leave eof False"

    with pytest.raises(ReadError, match="TRUNCATED"):
        _read_gz([truncated])


def test_truncation_is_caught_even_when_it_lands_mid_document():
    """Cutting deeper loses part of a line too. The error must still be truncation, not a JSON
    parse failure — a `json.loads` complaint would send the reader hunting a schema bug."""
    whole = gzip.compress(_jsonl_bytes(_records(6)))
    with pytest.raises(ReadError, match="TRUNCATED"):
        _read_gz([whole[: len(whole) // 2]])


def test_concatenated_gzip_members_are_all_read():
    """Real `.json.gz` files are sometimes concatenated members, and ONE `decompressobj` stops at
    the first: verified, it returns member 1's data with `eof=True` and leaves member 2 in
    `unused_data`. Reading only the first member is a partial corpus with no error."""
    first = gzip.compress(_jsonl_bytes(_records(2)))
    second = gzip.compress(_jsonl_bytes([{"id": "pes2o-9", "text": "w " * 60}]))
    docs = _read_gz([first + second])
    assert [d.id for d in docs] == ["pes2o-0", "pes2o-1", "pes2o-9"]


def test_concatenated_members_survive_a_chunk_boundary_on_the_member_seam():
    """The member switch happens inside the chunk loop, so a boundary exactly at the seam is the
    case most likely to lose the second member."""
    first = gzip.compress(_jsonl_bytes(_records(2)))
    second = gzip.compress(_jsonl_bytes([{"id": "pes2o-9", "text": "w " * 60}]))
    docs = _read_gz([first, second])
    assert [d.id for d in docs] == ["pes2o-0", "pes2o-1", "pes2o-9"]


def test_trailing_nul_padding_is_tolerated():
    """Measured: `gzip.decompress` accepts NUL-padded files, and starting a fresh decompressor on
    that padding raises `Error -3 ... incorrect header check`. So padding must end the stream
    rather than be mistaken for a member."""
    padded = gzip.compress(_jsonl_bytes(_records(2))) + b"\x00" * 16
    assert [d.id for d in _read_gz([padded])] == ["pes2o-0", "pes2o-1"]


def test_unexplained_trailing_bytes_are_refused():
    """Not padding and not a member (no `1f 8b` magic) means the file is not the concatenation it
    appears to be. Ignoring the tail would hide a corrupt or wrongly-assembled object."""
    with pytest.raises(ReadError, match="trailing bytes"):
        _read_gz([gzip.compress(_jsonl_bytes(_records(2))) + b"NOTGZIP!"])


def test_a_final_line_without_a_newline_is_still_a_document():
    """A member whose last line lacks `\\n` is real, and `eof` proves the stream complete — so the
    remainder is a WHOLE record, not a cut-off one. Dropping it loses one document per shard."""
    body = _jsonl_bytes(_records(3))[:-1]  # strip the trailing newline
    assert not body.endswith(b"\n")
    assert [d.id for d in _read_gz([gzip.compress(body)])] == ["pes2o-0", "pes2o-1", "pes2o-2"]


def test_a_multibyte_character_split_across_a_chunk_boundary_is_not_corrupted():
    """The carry must be BYTES, not text. Decoding per chunk would mangle a codepoint at every
    boundary — or, with `errors="replace"`, silently substitute U+FFFD into the corpus."""
    text = "éèê" * 40  # 2 bytes per char in UTF-8
    body = gzip.compress(_jsonl_bytes([{"id": "acc", "text": text}]))
    docs = _read_gz([body[i : i + 3] for i in range(0, len(body), 3)])
    assert len(docs) == 1
    assert docs[0].text == text, "the text must round-trip byte-exactly"
    assert "�" not in docs[0].text


def test_blank_lines_are_skipped_without_counting_as_documents():
    body = b'{"id":"a","text":"' + b"w " * 60 + b'"}\n\n   \n'
    assert [d.id for d in _read_gz([gzip.compress(body)])] == ["a"]


def test_malformed_json_on_a_complete_line_raises():
    """The gzip layer already proved the line complete, so this is malformed upstream rather than
    a range-boundary artifact — the two cases prior art conflated. A build must not skip it."""
    body = b'{"id":"a","text":"ok"}\n{not json at all}\n'
    with pytest.raises(ReadError, match="not valid JSON"):
        _read_gz([gzip.compress(body)])


def test_a_record_missing_its_id_raises():
    body = _jsonl_bytes([{"text": "w " * 60}])
    with pytest.raises(ReadError, match="join key"):
        _read_gz([gzip.compress(body)])


def test_a_record_missing_its_text_is_skipped():
    """A row with no payload is a row to skip and count, not a build failure."""
    body = _jsonl_bytes([{"id": "a", "text": "w " * 60}, {"id": "b"}])
    assert [d.id for d in _read_gz([gzip.compress(body)])] == ["a"]


def test_an_empty_object_is_refused_rather_than_read_as_an_empty_corpus():
    with pytest.raises(ReadError, match="empty"):
        _read_gz([])


def test_a_nested_json_domain_is_inherited_and_slugged():
    """The registry names ONE path per field, and the same logical field is spelled differently in
    the two containers. A marker segment is skipped only when the value is actually a list, so the
    parquet spelling also works against a JSON record."""
    body = gzip.compress(_jsonl_bytes(_records(2, site="C#")))
    docs = _read_gz([body], _jspec(domain_column="metadata.site"))
    assert [d.domain for d in docs] == ["c-sharp", "c-sharp"]


def test_a_json_list_valued_path_takes_element_zero():
    """`rollout_results.list.element.text` must resolve against `{"rollout_results":[{"text":...}]}`
    without the registry carrying a second spelling per format."""
    body = gzip.compress(
        _jsonl_bytes([{"id": "a", "rollout_results": [{"text": "REWRITE " * 30}]}])
    )
    docs = _read_gz([body], _jspec(text_column="rollout_results.list.element.text"))
    assert len(docs) == 1 and docs[0].text.startswith("REWRITE")


# --------------------------------------------------------------------------------------
# Format dispatch
# --------------------------------------------------------------------------------------


def test_read_documents_dispatches_on_the_declared_format():
    docs = list(
        read_documents("repo", "f.parquet", _spec(), {}, fileobj=_finephrase_parquet())
    )
    assert [d.text.split()[0] for d in docs] == ["REWRITE-0", "REWRITE-1"]


def test_an_unknown_format_is_refused_rather_than_sniffed():
    """Upstream filenames LIE about compression — the dolmino `math` prefix mixes `.jsonl`,
    `.jsonl.gz`, `.json.gz` and `.json.zst` in one directory
    (`artifacts/reservoir/WEEK1-CORPUS-SURVEY.md:111`), and suffix dispatch throws mid-stream,
    hours in, on a subset of shards. An unknown format is a registry bug to fix."""
    with pytest.raises(ReadError, match="no reader"):
        list(read_documents("repo", "f.zst", _spec(file_format="json.zst"), {}))


def test_both_gzip_format_spellings_resolve():
    for fmt in ("json.gz", "jsonl.gz"):
        docs = list(
            read_documents(
                "repo", "f.gz", _jspec(file_format=fmt), {},
                chunks=[gzip.compress(_jsonl_bytes(_records(1)))],
            )
        )
        assert [d.id for d in docs] == ["pes2o-0"]


# --------------------------------------------------------------------------------------
# ONE format table — the gate and the readers cannot diverge
# --------------------------------------------------------------------------------------
#
# There used to be THREE lists of readable formats: `corpus_build.READABLE_FORMATS` (which gates
# the plan), a dict literal inside `corpus_build._reader_for` (which actually dispatched), and
# `_READERS` here. Only `_READERS` carried `jsonl.gz`, so a `jsonl.gz` registry row was refused at
# plan time by `_assert_readable` **although `read_jsonl_gz_documents` is registered for exactly
# that spelling and reads it correctly** (the test above proves the reader works). A whole source
# was droppable by a check that looked entirely legitimate.
#
# These tests deliberately do NOT spell the admitted set out. A test asserting
# `{"parquet", "json.gz", "jsonl.gz"}` would be a FOURTH table — it would pass the day someone
# re-hardcodes the gate, which is the failure being removed. They recompute the correspondence
# from the live objects instead, so registering a reader without widening the gate, or widening
# the gate past the readers, fails here.


def test_the_gate_admits_exactly_the_formats_that_have_a_reader():
    """The bidirectional check, recomputed from the two live structures.

    Forward: every format the plan-time gate admits resolves to a callable, so the gate cannot
    admit a row that then dies at dispatch. Backward: every registered reader's format is admitted,
    so a working reader cannot be locked out — the `jsonl.gz` defect, stated as an assertion.
    """
    from edullm_data import corpus_build, corpus_read

    gate = set(corpus_build.READABLE_FORMATS)
    registered = set(corpus_read._READERS)

    assert gate == registered, (
        f"the plan-time gate and the reader registry disagree: gate-only={sorted(gate - registered)}, "
        f"reader-only={sorted(registered - gate)}. Both must come from `_READERS`; a second list "
        f"is what let `jsonl.gz` be refused while its reader worked."
    )
    for fmt in registered:
        assert callable(corpus_read.reader_for_format(fmt)), f"{fmt} resolves to no callable"


def test_the_gate_is_the_reader_registry_not_a_copy_of_it():
    """Equal-today is not the property being defended — three lists were equal until they were not.

    Mutating the registry must move the gate, in `corpus_build` as well as here. A `frozenset`
    literal that happens to match would pass the test above and fail this one, which is the whole
    point: the gate has to be DERIVED, not synchronised.
    """
    from edullm_data import corpus_build, corpus_read

    assert corpus_build.READABLE_FORMATS is corpus_read.READABLE_FORMATS, (
        "corpus_build must re-export the reader registry's key set, not define its own"
    )

    original = dict(corpus_read._READERS)
    try:
        corpus_read._READERS["mp3"] = "read_parquet_documents"
        assert frozenset(corpus_read._READERS) == frozenset(original) | {"mp3"}
        # Recomputed from the registry at call time, so the gate has already moved.
        assert corpus_read.reader_for_format("mp3") is corpus_read.read_parquet_documents
    finally:
        corpus_read._READERS.clear()
        corpus_read._READERS.update(original)

    assert corpus_read.reader_for_format("mp3") is None


def test_a_reader_is_resolved_by_name_at_call_time():
    """`_READERS` stores NAMES, and that is load-bearing rather than incidental.

    The offline build tests install a fake reader by assigning `corpus_read.read_parquet_documents`
    (`test_corpus_build.py:663,756`). A table holding the function OBJECT would have bound the
    original at import and driven the real reader while the test reported success — the
    mock-that-does-nothing failure. Asserted by doing exactly what those tests do.
    """
    from edullm_data import corpus_read

    def fake(*a, **k):
        yield Document(id="x", text="y", source="s")

    real = corpus_read.read_parquet_documents
    corpus_read.read_parquet_documents = fake
    try:
        assert corpus_read.reader_for_format("parquet") is fake
    finally:
        corpus_read.read_parquet_documents = real
    assert corpus_read.reader_for_format("parquet") is real


def test_zstd_is_absent_from_the_registry_and_the_message_says_so():
    """`.zst` has no reader and no consumer — the registry is 11 parquet + 6 json.gz, and `zst`
    appears in `src/` only in comments and error strings, never in a code path. So the gap closes
    by removal, not by adding a `zstandard` dependency nothing reads. This pins the error text to
    the registry's real contents rather than to a remembered list."""
    from edullm_data import corpus_build, corpus_read

    assert "zst" not in " ".join(corpus_read._READERS)
    with pytest.raises(ReadError) as exc:
        list(read_documents("repo", "f.zst", _spec(file_format="json.zst"), {}))
    # The message must quote what IS readable, recomputed — not a stale hand-written list.
    assert str(sorted(corpus_read.READABLE_FORMATS)) in str(exc.value)

    with pytest.raises(corpus_build.BuildDriverError) as build_exc:
        corpus_build._assert_readable(
            [
                CorpusSpec(
                    key="dclm", category="web-diverse", source_label="dclm",
                    repo="mlfoundations/dclm-baseline-1.0", file_format="jsonl.zst",
                    text_column="text", id_column="id", target_tokens=1, revision="a" * 40,
                )
            ]
        )
    assert str(sorted(corpus_build.READABLE_FORMATS)) in str(build_exc.value)


# --------------------------------------------------------------------------------------
# The short-document filter — the EOS-fraction floor made mechanical
# --------------------------------------------------------------------------------------


def _docs(lengths) -> list[Document]:
    return [
        Document(id=f"d{i}", text=" ".join(["w"] * n), source="synthetic-finephrase-faq")
        for i, n in enumerate(lengths)
    ]


def _words(text: str) -> int:
    """A stand-in for the pinned dolma2 tokenizer. The callable seam is why these tests need no
    tokenizer download and no network."""
    return len(text.split())


def test_documents_under_the_floor_are_dropped():
    """Not a quality preference. One EOS per document makes a shard's EOS fraction exactly
    `1 / mean_doc_tokens`, and the family bound of 0.05 rejects any shard whose mean document is
    under 20 tokens (`corpus.py:114-140`) — AFTER the tokenize and the upload are paid for."""
    stats = FilterStats()
    kept = list(filter_documents(_docs([12, 63, 64, 65, 5000]), _words, stats=stats))
    assert [d.id for d in kept] == ["d2", "d3", "d4"]
    assert stats.seen == 5 and stats.kept == 3 and stats.dropped == 2
    assert stats.dropped_short == 2 and stats.dropped_empty == 0


def test_the_default_floor_is_the_contracts_floor_not_the_failure_threshold():
    """64, not 20. A *mean* of 20 is where shards start failing, so a distribution centred there
    fails half of them; at a 64-token floor the worst possible shard mean is 64
    (`corpus.py:142-150`)."""
    assert FilterStats().min_tokens == MIN_DOC_TOKENS == 64
    assert MIN_DOC_TOKENS > MIN_MEAN_DOC_TOKENS
    stats = FilterStats()
    list(filter_documents(_docs([MIN_DOC_TOKENS - 1, MIN_DOC_TOKENS]), _words, stats=stats))
    assert stats.kept == 1 and stats.dropped_short == 1


def test_the_finephrase_quality_failure_is_what_this_drops():
    """A sampled FinePhrase rewrite was the entire string *"Question: Can light accelerate to the
    speed of light?"* — about 12 tokens (§3.3 trap 2). Shards packed from documents like that are
    rejected by the decode smoke test."""
    short = Document(
        id="q",
        text="Question: Can light accelerate to the speed of light?",
        source="synthetic-finephrase-faq",
    )
    stats = FilterStats()
    assert list(filter_documents([short], _words, stats=stats)) == []
    assert stats.dropped_short == 1


def test_the_survivors_predicted_eos_fraction_clears_the_family_bound():
    """Recomputed from the real kept lengths rather than assumed from the floor — the golden rule
    applied to the one number that decides whether the shards validate."""
    from edullm_data.corpus import FAMILY_MAX_EOS_FRACTION

    stats = FilterStats()
    list(filter_documents(_docs([10, 64, 128, 256]), _words, stats=stats))
    assert stats.mean_kept_tokens == pytest.approx((64 + 128 + 256) / 3)
    assert stats.predicted_eos_fraction < FAMILY_MAX_EOS_FRACTION
    assert stats.problems() == []


def test_counts_are_reported_so_a_silent_40_percent_loss_cannot_happen():
    """A build that discards 40% of a source and reports only its output size is
    indistinguishable from a build whose source was small. §3.3 measures a 3.4-12.6% document loss
    at a 50-token minimum (`artifacts/recount/synthetic.json:173`), so the rate is expected to be
    nonzero and must therefore be visible."""
    stats = FilterStats()
    list(filter_documents(_docs([1] * 6 + [100] * 4), _words, stats=stats))
    assert stats.kept == 4 and stats.dropped == 6
    assert stats.drop_fraction == pytest.approx(0.6)
    problems = stats.problems()
    assert problems and "60.0%" in problems[0]
    assert stats.sample_dropped[:2] == ["d0", "d1"], "dropped ids must be inspectable, not just counted"


def test_a_source_that_loses_everything_says_so_and_names_the_likely_cause():
    """All-dropped is the signature of a wrong text column, which is the duplicate-leaf trap's
    quiet twin: an empty column looks exactly like a corpus of short documents."""
    stats = FilterStats()
    assert list(filter_documents(_docs([1, 2, 3]), _words, stats=stats)) == []
    assert any("check the text column" in p for p in stats.problems())


def test_an_acceptable_drop_rate_reports_no_problems():
    """The complement — a check that always complained would also pass the tests above."""
    stats = FilterStats()
    list(filter_documents(_docs([1] + [100] * 99), _words, stats=stats))
    assert stats.drop_fraction == pytest.approx(0.01)
    assert stats.problems() == []


def test_empty_text_is_counted_separately_from_short_text():
    """Different causes: an empty string means the column resolved to nothing for that row, a
    short one means the document really is short. Merging them hides the schema bug."""
    stats = FilterStats()
    docs = _docs([100]) + [Document(id="empty", text="", source="s")]
    assert [d.id for d in filter_documents(docs, _words, stats=stats)] == ["d0"]
    assert stats.dropped_empty == 1 and stats.dropped_short == 0


def test_the_filter_streams_rather_than_materialising():
    """A 2.5 TB stream must never be listed. The generator must not consume its input until
    iterated, or the drop counts are complete only after memory has already been spent."""
    consumed: list[str] = []

    def _source():
        for doc in _docs([100, 100, 100]):
            consumed.append(doc.id)
            yield doc

    stats = FilterStats()
    iterator = filter_documents(_source(), _words, stats=stats)
    assert consumed == [], "nothing may be read before the first next()"
    next(iterator)
    assert consumed == ["d0"], "exactly one document per pull"


def test_a_zero_floor_is_refused():
    """A floor of 0 admits empty documents, which contribute one EOS and no content — the exact
    shape that drives the EOS fraction to 1.0."""
    with pytest.raises(ReadError, match="at least 1"):
        list(filter_documents(_docs([10]), _words, min_tokens=0))


def test_a_custom_floor_is_honoured_and_recorded():
    """§3.3's measured recommendation is a >=50-token minimum, and it warns against going above
    ~200: at >=200 `table` loses 23% of its tokens because a markdown table IS legitimately a
    short document, which would silently reshape the format mix."""
    stats = FilterStats()
    kept = list(filter_documents(_docs([49, 50, 51]), _words, min_tokens=50, stats=stats))
    assert [d.id for d in kept] == ["d1", "d2"]
    assert stats.min_tokens == 50


# ---- the json.gz twin of the zero-columns parquet trap ----


def test_a_wrong_text_column_on_json_gz_RAISES_instead_of_yielding_nothing():
    """A missing text key must not read as an empty corpus.

    JSON has no footer, so unlike parquet there is nothing to validate the selector against up
    front — and `continue`-ing past a missing key means a typo (or the literal string
    "UNVERIFIED") skips EVERY record and yields zero documents while reporting success. Measured
    against real bytes before this guard existed: `ubuntu_irc_filtered` with
    `text_column="UNVERIFIED"` returned 0 documents and raised nothing.
    """
    import gzip
    import json as _json

    from edullm_data.corpus import CorpusSpec
    from edullm_data.corpus_read import ReadError, read_jsonl_gz_documents

    body = gzip.compress(
        b"\n".join(_json.dumps({"id": f"d{i}", "text": f"real text {i}"}).encode() for i in range(3))
    )
    spec = CorpusSpec(
        key="cp", category="academic", source_label="cp", repo="common-pile/x",
        file_format="json.gz", text_column="NOPE", id_column="id", target_tokens=1,
        revision="a" * 40,
    )
    with pytest.raises(ReadError, match="EMPTY corpus"):
        list(read_jsonl_gz_documents("common-pile/x", "f.json.gz", spec, chunks=[body]))

    ok = CorpusSpec(**{**spec.__dict__, "text_column": "text"})
    got = list(read_jsonl_gz_documents("common-pile/x", "f.json.gz", ok, chunks=[body]))
    assert [d.id for d in got] == ["d0", "d1", "d2"]


def test_a_later_record_missing_text_is_skipped_not_fatal():
    """Only the FIRST record is treated as a schema signal; upstream filtering leaves a few
    documents with an empty body and those are legitimately unusable, not an error."""
    import gzip
    import json as _json

    from edullm_data.corpus import CorpusSpec
    from edullm_data.corpus_read import read_jsonl_gz_documents

    body = gzip.compress(
        _json.dumps({"id": "a", "text": "present"}).encode() + b"\n"
        + _json.dumps({"id": "b"}).encode() + b"\n"
        + _json.dumps({"id": "c", "text": "also present"}).encode() + b"\n"
    )
    spec = CorpusSpec(
        key="cp", category="academic", source_label="cp", repo="common-pile/x",
        file_format="json.gz", text_column="text", id_column="id", target_tokens=1,
        revision="a" * 40,
    )
    got = list(read_jsonl_gz_documents("common-pile/x", "f.json.gz", spec, chunks=[body]))
    assert [d.id for d in got] == ["a", "c"]


def test_the_pinned_url_uses_the_revision_not_main():
    """`ingest_reservoir._resolve_url` hardcodes `resolve/main`, and calling it here silently
    defeated the registry pins: files listed at the pinned sha, bytes fetched from HEAD."""
    from edullm_data.corpus_read import _pinned_url

    url = _pinned_url("acme/x", "data/f.parquet", "b" * 40)
    assert f"/resolve/{'b' * 40}/" in url and "/resolve/main/" not in url
    # Nothing pinned -> the old behaviour, so an unpinned caller is unchanged rather than broken.
    assert "/resolve/main/" in _pinned_url("acme/x", "data/f.parquet", None)
