"""Stage 1 of §5.6 phase 1: turn upstream files into :class:`~.corpus.Document` objects.

This is the READ stage and it does exactly one thing — yield `Document(id=, text=, source=,
domain=)` — because that is the whole contract `corpus.py` defines between the four stages. It
owns no shard geometry, no tokenizer, and no S3 write.

WHY THIS MODULE EXISTS SEPARATELY FROM `ingest_reservoir.py`
-----------------------------------------------------------
`ingest_reservoir._scan_ids` already reads these same files over the same transport, but it reads
**only the id column** — it never materialises payload text. This module does, which makes it a
different risk profile (memory per row group, and the column-resolution trap below) while sharing
every byte of the transport. So the transport is IMPORTED, never reimplemented: `_RangeFile`,
`_resolve_url`, `_leaf_index`, `_RATE_GATE`, `_hf_headers`. Each of those encodes a failure that
already cost this project real time (a SIGSEGV, a 429 storm, a wrong column), and a second HTTP
layer would re-earn all three.

THE ONE FAILURE THAT NO DOWNSTREAM CHECK CATCHES
------------------------------------------------
FinePhrase's synthetic rewrite is at `rollout_results.list.element.text`. Its **top-level `text`
holds the ORIGINAL, unrephrased FineWeb-Edu document** — the sibling `dataset` field literally
reads `HuggingFaceFW/fineweb-edu` in 34/34 sampled rows
(`artifacts/recount/synthetic.json:9`). The flat leaf list contains `text` TWICE, and
`.names.index("text")` returns the ORIGINAL: verified by execution on pyarrow 24.0.0, where a
FinePhrase-shaped footer yields

    md.schema.names        -> ['id', 'text', 'dataset', 'text', 'score']
    .index("text")         -> 1                      <- the ORIGINAL
    path_in_schema leaves  -> [..., 'rollout_results.list.element.text']   <- the REWRITE

Reading the wrong one substitutes ~1.39 *trillion* tokens of real web text for the synthetic pool
(`artifacts/recount/synthetic.json:11`), and **no hash, size, or decode check can see it**: the
bytes are real text, tokenized correctly, in valid ids, internally consistent with their own
digest. So this module resolves every column by exact `path_in_schema` through
:func:`ingest_reservoir._leaf_index`, which refuses a bare-name fallback by construction
(`ingest_reservoir.py:502`). That refusal is the single most important line here.

A second, quieter half of the same trap, also verified by execution: asking pyarrow for a
*plausible but wrong* nested selector does not raise. `columns=["rollout_results.text"]` and
`columns=["rollout_results.list.item.text"]` (the legacy spelling) both return a table with **zero
columns** and `to_pylist()` of `[{}]` — an empty corpus, silently. Only `path_in_schema` taken
from the file's own footer is safe, which is why the selectors here are never spelled by hand.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
No decontamination and no dedup — both are later steps in §5.6 phase 1's ordering, and neither
belongs in a per-file streaming reader. No `domain` classification: §1.2 was revised to *inherit*
a domain or publish flat, never to infer one.
"""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .corpus import MIN_DOC_TOKENS, MIN_MEAN_DOC_TOKENS, BuildError, CorpusSpec, Document
from .ingest_reservoir import (
    _RATE_GATE,
    IngestError,
    _hf_headers,
    _leaf_index,
    _RangeFile,
    _resolve_url,
)

__all__ = [
    "GZIP_WBITS",
    "JSONL_CHUNK_BYTES",
    "LIST_MARKER_SEGMENTS",
    "AttritionWarning",
    "READABLE_FORMATS",
    "FilterStats",
    "ReadError",
    "filter_documents",
    "read_documents",
    "read_jsonl_gz_documents",
    "surrogate_id",
    "read_parquet_documents",
    "reader_for_format",
]


class AttritionWarning(UserWarning):
    """A source delivered far fewer documents than the pool arithmetic assumed.

    Its own category rather than a bare ``RuntimeWarning`` for two reasons, both practical.
    ``corpus_pack._warn_if_parallelism_unset`` already emits a ``RuntimeWarning`` on this path, so a
    test asserting on the generic class would pass for the wrong reason; and an operator who wants
    to turn attrition into a hard stop can do it with one ``-W error::…AttritionWarning`` rather
    than a code change, which is the right shape for a POLICY that :meth:`FilterStats.problems`
    deliberately leaves to its caller.

    ``UserWarning``, not ``RuntimeWarning``: nothing is malfunctioning. The shards this bundle wrote
    passed every check ``corpus_pack._verify_shard`` makes. What is wrong is a NUMBER IN THE PLAN —
    §2.1/§3.2 budgeted tokens this source will not deliver — and that is a message to a human, which
    is what ``UserWarning`` means.
    """


class ReadError(BuildError):
    """An upstream file could not be read into documents *honestly*.

    A subclass of :class:`~.corpus.BuildError` rather than of ``IngestError`` because every
    condition it reports is a build-time invariant violation — a moved schema, a truncated
    download, a column that resolves to the wrong bytes — and §5.6 phase 1 fixes those before any
    byte reaches landing. Transport failures still surface as ``IngestError`` from the imported
    reader, and that split is intentional: one means "the network was bad", the other means "the
    data is not what we claimed".
    """


# --------------------------------------------------------------------------------------
# Parquet leaf-path resolution — the trap, and the machinery that closes it
# --------------------------------------------------------------------------------------

#: Path segments parquet inserts for a repeated field, which carry no data and must be stepped
#: over when walking a value. BOTH spellings are live: pyarrow writes `list.element` under
#: `use_compliant_nested_type=True` (its default since 13.0) and `list.item` under `False`.
#: Verified by execution — the same table written both ways yields
#: `rr.list.element.text` and `rr.list.item.text`. FinePhrase uses the compliant spelling
#: (`artifacts/recount/_fp_footer_leaf.py:68`), but a corpus written by an older writer will not,
#: and hard-coding one spelling would fail on the other with an EMPTY read rather than an error.
LIST_MARKER_SEGMENTS = frozenset({"list", "element", "item"})


def surrogate_id(repo: str, file_path: str, row_index: int) -> str:
    """A document id for a source that ships none: ``<repo-tail>/<file path>#<row index>``.

    Dossier §B12's design, and the alternatives it REJECTED are the reason this shape and not a
    simpler one:

    * ``sha256(text)`` — rejected because *"the required ``lstrip()`` fix CHANGES the text, so the
      id would depend on whether normalization ran before or after hashing."* An id that moves with
      a normalization pass is not reproducible, and reproducibility is the single property
      :func:`~.corpus.is_held_out` needs — it decides the train/val carve from the id ALONE.
    * ``(config, row_index)`` — rejected because row order is a property of the upstream files, not
      of the dataset: a re-conversion that repacks rows keeps the config and moves every id.
    * the ``prompt`` column — rejected because Cosmopedia generates MULTIPLE documents per seed
      prompt, so it is not unique; colliding ids would put unrelated documents on the same side of
      the carve and read as duplicates to the dedup pass.

    **Stability comes from the filename, deliberately.** Upstream names encode their own cardinality
    (``train-00000-of-00118.parquet``), so any change to the file count changes EVERY filename —
    the id breaks loudly instead of silently re-partitioning the corpus. §B12: *"loud, not silent."*

    ⚠️ **Uses the file's FULL repo-relative path, not its basename — a CORRECTION to §B12 forced by a
    later change.** §B12 wrote ``(config, file_basename, row_index)`` when the cosmopedia row named
    ONE config. That row now reads ``config: "data"``, the parent of all 8 configs, and **basenames
    COLLIDE across them — MEASURED: ``train-00000-of-00002.parquet`` exists under BOTH
    ``data/openstax/`` and ``data/wikihow/``.** Basename + row index would hand two different
    documents the same id. The full path carries the config segment that §B12 relied on, so this
    honours the design rather than replacing it; uniqueness within a revision is then structural.

    ⚠️ **NOT comparable across sources.** This id is derived from our read of the file tree, not
    from anything upstream published, so a cross-source join keyed on ``id`` silently excludes a
    surrogate source. :attr:`~.corpus.CorpusSpec.id_surrogate` is the flag to check first.

    Only meaningful against a PINNED revision; ``CorpusSpec.__post_init__`` enforces that.
    """
    if row_index < 0:
        raise ReadError(f"surrogate row_index must be >= 0, got {row_index}")
    if not file_path:
        raise ReadError(
            "surrogate id needs the file path — it is the component that makes the id unique "
            "across configs and that breaks loudly when upstream re-shards"
        )
    # The repo tail keeps the id readable and namespaced without embedding the owner, which is
    # noise here: the revision pin is what fixes provenance, not the org name.
    return f"{repo.rsplit('/', 1)[-1]}/{file_path}#{row_index}"


def _resolve_leaf(parquet_md: Any, want: str, *, what: str) -> str:
    """Confirm `want` is an exact leaf path, and return it unchanged.

    A thin wrapper over :func:`ingest_reservoir._leaf_index` — used for its *refusal*, not its
    index. That function is already the exact-match-or-raise gate this module needs
    (`ingest_reservoir.py:502-517`); wrapping it rather than reimplementing keeps one definition
    of "which column is the right column", and re-raises as :class:`ReadError` with the field name
    that was wrong so the message names `text_column` or `domain_column` instead of a bare leaf.
    """
    try:
        _leaf_index(parquet_md, want)
    except IngestError as exc:
        raise ReadError(f"{what}={want!r} does not name a leaf in this file. {exc}") from exc
    return want


def _compile_walk(
    schema_arrow: Any, leaf: str, *, what: str, require_string: bool = True
) -> tuple[str, tuple[str, ...]]:
    """Split a leaf path into `(top_level_column, walk)`, checking it against the arrow schema.

    The returned walk INCLUDES the top-level name as its first step and has the
    :data:`LIST_MARKER_SEGMENTS` dropped, so it applies to a whole projected row rather than to
    an already-extracted value. That is deliberate: `to_pylist()` hands back
    ``{'rollout_results': [{'text': ...}]}``, i.e. a Mapping whose first hop is the same
    `Mapping.get` as every later struct hop, so one walker covers the entire path with no special
    case for the top level — and no chance of the two disagreeing about who consumed it.

    Traversing the arrow schema here is not decoration — it is what turns three silent failures
    into loud ones *before* the first row group is read:

    * a `domain_column` that names a struct instead of a string, which would otherwise yield a
      dict to :func:`slug_path_segment` and raise per-row, mid-build;
    * a duplicate TOP-LEVEL column name, where pyarrow keeps the LAST field of that name.
      Measured: a file with `text: string` then `text: int64` read with `columns=["text"]`
      returns ``[{'text': 7}]`` — the int, silently. There is no way to say which one was meant,
      so this refuses;
    * a path that does not descend (a `.` into a primitive), which selects nothing.
    """
    import pyarrow as pa

    segments = leaf.split(".")
    top = segments[0]
    indices = schema_arrow.get_all_field_indices(top)
    if len(indices) != 1:
        raise ReadError(
            f"{what}={leaf!r}: the top-level column {top!r} appears {len(indices)} times in this "
            f"file's schema. pyarrow keeps the LAST field of a duplicated name (measured: a "
            f"string `text` followed by an int64 `text` read back as the int), so there is no way "
            f"to express which one is meant. Refusing rather than guessing."
        )

    arrow_type = schema_arrow.field(indices[0]).type
    walk: list[str] = [top]
    rest = segments[1:]
    while rest:
        if (
            pa.types.is_list(arrow_type)
            or pa.types.is_large_list(arrow_type)
            or pa.types.is_fixed_size_list(arrow_type)
        ):
            # Parquet spells a repeated field as `<name>.list.<element-name>`, so consume EXACTLY
            # those two marker segments — never every marker-named segment in the tail. Measured:
            # a `list<struct<element: struct<text>>>` (a field genuinely named `element` under the
            # marker) has the leaf `rr.list.element.element.text`, and a greedy strip eats both
            # hops and then reads a sibling or raises. The element's real spelling is read off the
            # file rather than assumed, because `value_field.name` tracks the writer's choice
            # exactly: `element` under `use_compliant_nested_type=True`, `item` under False —
            # verified on both.
            if rest[0] == "list":
                rest = rest[1:]
            if rest and rest[0] == arrow_type.value_field.name:
                rest = rest[1:]
            walk.append("[0]")
            arrow_type = arrow_type.value_type
            # `rest` is legitimately empty for a list of PRIMITIVES, whose whole leaf path is
            # `<name>.list.<element>` (verified: `tags.list.element` for a `list<string>`). The
            # loop ends and the string check below applies to the element type, which is right.
            continue
        if pa.types.is_struct(arrow_type):
            name = rest[0]
            index = arrow_type.get_field_index(name)
            if index < 0:
                raise ReadError(
                    f"{what}={leaf!r}: struct {arrow_type} has no field {name!r}. The footer "
                    f"listed this leaf, so the two disagree — which means the file mixes schemas."
                )
            walk.append(name)
            arrow_type = arrow_type.field(index).type
            rest = rest[1:]
            continue
        raise ReadError(
            f"{what}={leaf!r}: cannot descend into {arrow_type} at segment {rest[0]!r}. A path "
            f"that does not descend selects NOTHING in pyarrow — verified: "
            f"columns=['rollout_results.text'] returns a table with zero columns and no error."
        )

    if require_string and not (
        pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type)
    ):
        raise ReadError(
            f"{what}={leaf!r} has arrow type {arrow_type}, not a string. Refused here rather "
            f"than per-row: a non-string text column would tokenize its `repr`, and a non-string "
            f"domain column would reach slug_path_segment as a dict and raise mid-build."
        )
    return top, tuple(walk)


def _walk(row: Mapping[str, Any] | None, walk: Sequence[str]) -> Any:
    """Follow a compiled walk through one projected row, or a parsed JSON record.

    ONE walker serves both readers, and that is not a coincidence: `to_pylist()` reproduces the
    parquet nesting as plain dicts and lists (verified —
    ``{'meta': {'site': 'phys'}, 'rollout_results': [{'text': 'REW'}]}``), which is the same shape
    `json.loads` produces for a Common Pile record. So the *projection* is what makes this safe,
    not the walk: because the columns were selected by exact `path_in_schema`, the dict handed
    here contains only the leaves that were asked for, and a bare `"text"` key cannot be the
    original document unless the original document is what was requested.

    `[0]` takes the FIRST list element. For FinePhrase that is not a lossy choice: `/statistics`
    reports `rollout_results` with mean = median = min = max = 1.0 over 842,000 rows, and
    rollouts-per-row was min = max = 1 in every one of 160 row groups across all four configs
    (`artifacts/recount/synthetic.json:9,11`). Element 0 IS the entire payload, so there is no
    multi-rollout aggregation to perform and no second rewrite being discarded. A row whose list
    is EMPTY yields ``None`` here rather than raising — an empty list is verified-legal in
    parquet (``{'rollout_results': []}``) and means the row has no rewrite, which is a row to skip
    and count, not a build failure.
    """
    value: Any = row
    for step in walk:
        if value is None:
            return None
        if step == "[0]":
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                return None
            if not value:
                return None
            value = value[0]
            continue
        if not isinstance(value, Mapping):
            return None
        value = value.get(step)
    return value


def _json_walk(record: Mapping[str, Any], dotted: str) -> Any:
    """Walk a dotted path through a parsed JSON record, tolerating parquet's list markers.

    The registry names ONE path per field (`CorpusSpec.text_column`), but the same logical field
    is spelled differently in the two containers: parquet's footer says
    `rollout_results.list.element.text` while the JSON record is `{"rollout_results": [{"text":
    ...}]}`. Rather than duplicate every registry row per format, a marker segment is skipped
    **only when the current value is actually a list** — so a JSON object that legitimately owns a
    key named `list` is still read correctly, and the ambiguity is closed by the data rather than
    by a naming convention.

    UNVERIFIED: that no target `.json.gz` corpus has a record key literally named `list`,
    `element`, or `item` at a position where it holds a list. The guard above makes it harmless,
    but to confirm the vocabulary:
        python3 -c "import json,urllib.request,zlib;u='https://huggingface.co/datasets/common-pile/peS2o_filtered/resolve/main/v0/documents/peS2o-0000.json.gz';r=urllib.request.Request(u,headers={'Range':'bytes=0-2000000'});d=zlib.decompressobj(16+zlib.MAX_WBITS);print(sorted(json.loads(d.decompress(urllib.request.urlopen(r).read()).split(b'\\n')[0])))"
    """
    value: Any = record
    for segment in dotted.split("."):
        if value is None:
            return None
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if segment in LIST_MARKER_SEGMENTS:
                # A marker, not a key: step over it and stay on the list.
                continue
            if not value:
                return None
            value = value[0]
        if not isinstance(value, Mapping):
            return None
        value = value.get(segment)
    return value


# --------------------------------------------------------------------------------------
# The inherited `domain` segment — §1.2
# --------------------------------------------------------------------------------------


def _domain_of(
    row: Mapping[str, Any],
    spec: CorpusSpec,
    *,
    domain_map: Mapping[str, str] | None = None,
    walk: Sequence[str] | None = None,
) -> str | None:
    """The `domain` path segment for one row, slugged and folded — or ``None`` for a flat source.

    ``None`` is the answer for most sources and it is CORRECT, not missing data: §1.2's rule is
    that a source gets a domain segment if and only if it SHIPS one upstream
    (`corpus.py:180-182`).

    **`domain_map` is how §1.2's cardinality fold is applied, and it must come from
    :func:`manifest.build_domain_slug_map`.** Two properties make that non-optional:

    * *A single streaming pass cannot know the top 20.* "Keep the top ~20 values by count" is a
      statement about the whole corpus, and this function sees one row. So the fold needs a prior
      counting pass, and `build_domain_slug_map` is the only sanctioned way to turn those counts
      into segments — it is where the COLLISION check lives (`manifest.py:1149`), and running
      :func:`slug_path_segment` over a vocabulary yourself skips exactly that check. `C#` and
      `C++` both stripping to `c` would merge two languages into one permanent directory inside
      `manifest_sha256` with every token count still adding up.
    * *A value absent from the map folds to `other`.* Not a fallback — a definition. The map was
      built from the counting pass's vocabulary, so a value missing from it is by construction
      outside the top `keep`, which is precisely the tail §1.2 folds. This is also what makes the
      reader robust to a value that appears for the first time after the counting pass: it lands
      in `other` rather than silently creating a 21st permanent directory.

    With NO map, the value is slugged and returned unfolded. That path exists for a single-source
    smoke read and for the counting pass itself; publishing from it would commit one directory per
    distinct upstream value, which for `stackv2-edu` is 73 and for StackExchange ~180.
    """
    if spec.domain_column is None:
        return None

    raw = _walk(row, walk) if walk is not None else _json_walk(row, spec.domain_column)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        # A row missing its own domain value. Not fatal and not silently flat: a flat key and a
        # nested key are DIFFERENT label sets to `labels_from_path`, so mixing them inside one
        # source would make `labels={'domain': ...}` skip these rows invisibly (§1.2's
        # "silently drops flat sources"). Folding to the tail segment keeps the depth uniform.
        from .manifest import OTHER_SEGMENT

        return OTHER_SEGMENT if domain_map is not None else None
    if not isinstance(raw, str):
        raise ReadError(
            f"{spec.key}: domain_column {spec.domain_column!r} yielded {type(raw).__name__} "
            f"{raw!r}, not a string. A non-string cannot be a path segment, and coercing it here "
            f"would put `repr` output inside manifest_sha256 permanently."
        )

    value = raw.strip()
    if domain_map is not None:
        from .manifest import OTHER_SEGMENT

        return domain_map.get(value, OTHER_SEGMENT)

    from .manifest import slug_path_segment

    return slug_path_segment(value)


# --------------------------------------------------------------------------------------
# Parquet
# --------------------------------------------------------------------------------------


def _pinned_url(repo: str, path: str, revision: str | None) -> str:
    """`resolve/<revision>/<path>`, falling back to `main` ONLY when nothing is pinned.

    ⚠️ `ingest_reservoir._resolve_url` hardcodes `resolve/main`, and calling it here silently
    defeated the registry's revision pins: the build would list files at the pinned sha and then
    fetch their bytes from whatever `main` pointed at that morning. Nothing downstream would
    notice — the manifest hashes whatever arrived and Gate A passes it — so "the corpus built from
    fineweb-edu@87f09149" could quietly contain different bytes on two runs.

    That module keeps its own version because its one caller (the FinePhrase id scan) genuinely
    wants HEAD-of-branch; this one is for a pinned build.
    """
    if not revision:
        return _resolve_url(repo, path)
    return f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{path}"


def _open_parquet(
    repo: str,
    path: str,
    size: int,
    headers: Mapping[str, str],
    fileobj: Any | None,
    parquet_file: Any | None = None,
    revision: str | None = None,
) -> Any:
    """`(ParquetFile, underlying_file)` over HTTP Range, or over an injected object.

    ⚠️ **`pre_buffer=False` IS LOAD-BEARING. IT IS THE ARRAY SEGFAULT FIX. DO NOT REMOVE IT.**

    The full argument is in `ingest_reservoir._scan_ids` (`ingest_reservoir.py:641-664`) and is not
    repeated here, but the mechanism is: `pre_buffer=True` dispatches this file's coalesced range
    reads onto **Arrow's native C++ IO thread pool**, where each one becomes a complete `urlopen`
    — TLS handshake, redirect chain, socket read — inside a C++ thread-pool callback. Arrow sizes
    that pool from the HOST cpu count, not the cgroup (logged live: `io_threads=8` inside a 2-vCPU
    container). A/B on Batch, 4-child array, everything else byte-identical: `pre_buffer=True` →
    exit 139 `Segmentation fault (core dumped)` in 3 of 4 children; `pre_buffer=False` → exit 0 in
    4 of 4.

    Do NOT "fix" this with a lock on `_RangeFile`. That hypothesis was tested and refuted —
    instrumented runs measured max concurrency **1** per object and **0** interleavings, and a
    per-object `RLock` did not change the outcome. The problem is the native callback context, not
    a data race.

    ⚠️ And the keyword is mandatory even though the *installed* default is currently harmless:
    measured on pyarrow 24.0.0, `ParquetFile.__init__` defaults `pre_buffer=False` while
    `pq.read_table` and `pq.ParquetDataset` default `True`. So the default is a per-API,
    per-version accident — production resolved pyarrow 25.0.0 unpinned (`pyproject.toml:29`),
    where `_scan_ids`'s docstring records the ParquetFile default as `True`. Writing it
    explicitly is what makes this call site independent of which of those is true today.
    """
    import pyarrow.parquet as pq

    if parquet_file is not None:
        # An ALREADY-OPEN reader, for a caller that needs to observe or wrap the requests this
        # module makes. Bypasses the keyword below by construction, which is why the `fileobj`
        # seam exists separately and is what the behavioural tests use.
        return parquet_file, None

    if fileobj is not None:
        # Test/local seam over real bytes. Same call site, same keyword — a test that bypassed
        # this line would not be exercising the configuration that segfaulted.
        return pq.ParquetFile(fileobj, pre_buffer=False), fileobj

    # Block before opening a new file if any worker was recently 429'd: every thread in this
    # process presents one credential to one metered endpoint, so the brake belongs to the
    # process (`ingest_reservoir.py:228-251`).
    _RATE_GATE.wait()
    rf = _RangeFile(_pinned_url(repo, path, revision), size, dict(headers))
    return pq.ParquetFile(rf, pre_buffer=False), rf


def read_parquet_documents(
    repo: str,
    path: str | Mapping[str, Any],
    spec: CorpusSpec,
    headers: Mapping[str, str] | None = None,
    *,
    size: int | None = None,
    domain_map: Mapping[str, str] | None = None,
    fileobj: Any | None = None,
    parquet_file: Any | None = None,
) -> Iterator[Document]:
    """Stream one parquet file as :class:`~.corpus.Document` objects, a row group at a time.

    `path` accepts either a key string or an `hf_tree` entry dict (`{"path": ..., "size": ...}`),
    because `size` is required for the Range reader and the entry is what callers already hold —
    `hf_tree` guarantees the field is present and raises if the API stops returning it
    (`ingest_reservoir.py:483-489`).

    ROW GROUP AT A TIME, VIA `read_row_group`, DELIBERATELY. `iter_batches(batch_size=...)` would
    bound memory tighter and does accept nested leaf paths (verified). It is NOT used, because
    `read_row_group(rg, columns=[...])` is the exact call the array-segfault A/B was run against
    (`ingest_reservoir.py:680`) and the crash it was fixing was an IO-dispatch bug several layers
    below the reading API. Swapping in an unproven IO path here to save memory that FinePhrase's
    row groups do not need — the measured payload leaf is ~1.77 MB per row group
    (`artifacts/recount/_fp_footer_leaf.py:19`) — would be trading a known-good configuration for
    an untested one at the one call site that has already cost days.

    Only the needed leaves are requested, so the Range reader fetches the footer plus those column
    chunks and nothing else. For FinePhrase that matters twice over: `rollout_results` carries SIX
    leaves, and summing the whole subtree instead of the one payload leaf overcounts by 1.0-1.7%
    (`artifacts/recount/synthetic.json:16`) while also pulling `usage` chunks nobody reads.
    """
    entry_size = size
    if isinstance(path, Mapping):
        entry_size = path.get("size", size)
        path = str(path["path"])
    if fileobj is None and parquet_file is None and entry_size is None:
        raise ReadError(
            f"{spec.key}: reading {path!r} over HTTP Range needs the object size; pass size= or "
            f"the hf_tree entry dict, which always carries it."
        )

    pf, _handle = _open_parquet(
        repo, str(path), int(entry_size or 0), headers or _hf_headers(), fileobj, parquet_file,
        revision=spec.revision,
    )
    md = pf.metadata

    # Resolve EVERY column by exact `path_in_schema` before reading a single row. This is the
    # trap from the module docstring, and it is checked here rather than per-row so that a moved
    # schema fails on file 1 of 6,800 instead of producing a plausible corpus.
    text_leaf = _resolve_leaf(md, spec.text_column, what="text_column")
    # A surrogate source has NO id column to resolve — that is the whole point of the flag, and
    # `_resolve_leaf` would (correctly) refuse an empty name. The id is built per row below from
    # `(file path, row index)`, dossier §B12.
    id_leaf = None if spec.id_surrogate else _resolve_leaf(md, spec.id_column, what="id_column")
    _, text_walk = _compile_walk(pf.schema_arrow, text_leaf, what="text_column")
    # The id need NOT be a string: an integer post id is legitimate upstream (StackExchange), and
    # `str(int)` is stable across a re-download, which is the only property `Document.id` requires
    # of it. The text and domain columns are held to strings because a coerced `repr` would be
    # tokenized in one case and become a permanent path segment in the other.
    id_walk = (
        None if id_leaf is None
        else _compile_walk(pf.schema_arrow, id_leaf, what="id_column", require_string=False)[1]
    )

    wanted = [text_leaf] if id_leaf is None else [text_leaf, id_leaf]
    domain_walk: tuple[str, ...] | None = None
    if spec.domain_column is not None:
        domain_leaf = _resolve_leaf(md, spec.domain_column, what="domain_column")
        _, domain_walk = _compile_walk(pf.schema_arrow, domain_leaf, what="domain_column")
        wanted.append(domain_leaf)

    # Deduplicated because a spec may legally point two fields at one leaf. pyarrow accepts the
    # repeat and returns the column once (verified: `columns=["id","id"]` -> names `['id']`), so
    # this is for the reader of the request list, not for pyarrow.
    leaves = list(dict.fromkeys(wanted))

    # Row index within the FILE, not within the row group: the surrogate must be stable against a
    # re-read, and row-group boundaries are a writer's choice that a re-conversion can move.
    row_index = 0
    for rg in range(md.num_row_groups):
        table = pf.read_row_group(rg, columns=leaves)
        for row in table.to_pylist():
            text = _walk(row, text_walk)
            if id_walk is None:
                doc_id = surrogate_id(spec.repo, str(path), row_index)
                row_index += 1
            else:
                doc_id = _walk(row, id_walk)
            if not isinstance(text, str) or not text:
                # No rewrite for this row (an empty `rollout_results` list is legal), or a null.
                # Skipped rather than raised; `filter_documents` is where losses get counted.
                continue
            if doc_id is None:
                raise ReadError(
                    f"{spec.key}: a row in {path!r} has a null {spec.id_column!r}. The id is the "
                    f"join key for the §9.7 item 4 partition and the FineWeb-Edu anti-join, and "
                    f"a row index would make the partition non-reproducible across a "
                    f"re-download (corpus.py:176-179)."
                )
            yield Document(
                id=str(doc_id),
                text=text,
                source=spec.source_label,
                domain=_domain_of(row, spec, domain_map=domain_map, walk=domain_walk),
            )
        del table


# --------------------------------------------------------------------------------------
# JSON Lines over gzip — every Common Pile source
# --------------------------------------------------------------------------------------

#: `16 + MAX_WBITS` selects gzip framing (not raw deflate, not zlib) for
#: :func:`zlib.decompressobj`. Used instead of :mod:`gzip` because `GzipFile` needs a seekable
#: stream it can re-read and, more importantly, because `decompressobj` exposes
#: :attr:`~zlib.Decompress.eof` — the flag that distinguishes a complete stream from a truncated
#: one. See :func:`_gunzip_lines`.
GZIP_WBITS = 16 + zlib.MAX_WBITS

#: Compressed bytes per Range read. A gzip member is not seekable — it must be decompressed from
#: byte 0 — so unlike the parquet path there is no column projection and no random access: the
#: whole object transfers. 8 MiB amortises request overhead without holding a large buffer.
JSONL_CHUNK_BYTES = 8 * 1024 * 1024


def _gunzip_lines(chunks: Iterable[bytes], *, where: str) -> Iterator[bytes]:
    """Decompress a stream of gzip chunks into complete lines, as raw bytes.

    Four separate correctness properties, each of which fails SILENTLY if dropped:

    1. **The partial trailing line is carried, never parsed.** A range boundary lands mid-line
       with near-certainty. Prior art in this repo swallows the resulting error
       (`artifacts/recount/_filtered_tpb.py:83` — "a truncated final line just fails and is
       skipped"), which is right for a sampler and wrong for a build: it would drop one real
       document per chunk boundary, invisibly. Here the remainder is held and prepended to the
       next chunk.

    2. **The carry is BYTES, not text.** A chunk boundary can split a multi-byte UTF-8 codepoint
       just as easily as a line, so decoding per chunk would corrupt a character at every
       boundary (or, with `errors="replace"`, silently substitute U+FFFD). Only complete lines are
       decoded.

    3. **`decompressobj.eof` is checked at end of stream.** This is the one that loses data
       quietly. Verified by execution: on a truncated gzip stream `decompressobj` returns every
       byte it could decode and sets `eof = False` — it does NOT raise, where `gzip.decompress`
       raises `EOFError`. So a cut-off download decompresses cleanly to a *short* corpus that
       reads as a small-but-valid source. Cutting 5 bytes off a 2-member fixture still yielded
       both of member 1's documents with `eof=False`; cutting to half yielded a partial document.

    4. **Multi-member gzip is followed.** Real `.json.gz` files are sometimes concatenated
       members, and one `decompressobj` stops at the first member's end — verified: it returns
       member 1's data with `eof=True` and member 2 sitting untouched in `unused_data`. Feeding
       that to a fresh decompressor recovers it, looping for however many members there are.

    A single trailing detail, also measured: gzip files padded with trailing NUL bytes are real
    (`gzip.decompress` tolerates them), and starting a fresh decompressor on that padding raises
    `Error -3 ... incorrect header check`. So a remainder is only treated as a new member when it
    actually begins with the gzip magic `1f 8b`; anything else is padding and ends the stream.
    """
    decompressor = zlib.decompressobj(GZIP_WBITS)
    pending = b""
    saw_any = False

    for chunk in chunks:
        if not chunk:
            continue
        saw_any = True
        remainder: bytes | None = chunk
        while remainder:
            data = decompressor.decompress(remainder)
            remainder = None
            if data:
                pending += data
                # `keepends=False` via split: the last element is the partial line, or b"" when
                # the chunk happened to end exactly on a newline.
                *complete, pending = pending.split(b"\n")
                for line in complete:
                    yield line
            if decompressor.eof:
                tail = decompressor.unused_data
                if tail[:2] == b"\x1f\x8b":
                    decompressor = zlib.decompressobj(GZIP_WBITS)
                    remainder = tail
                elif tail.strip(b"\x00"):
                    raise ReadError(
                        f"{where}: {len(tail)} trailing bytes after the last gzip member are "
                        f"neither a new member (magic 1f 8b) nor NUL padding: {tail[:16]!r}. "
                        f"Refusing to ignore them — unexplained trailing bytes mean the file is "
                        f"not the concatenation of members it appears to be."
                    )

    if not saw_any:
        raise ReadError(f"{where}: the object is empty; no gzip member to decompress.")

    if not decompressor.eof:
        raise ReadError(
            f"{where}: the gzip stream ended without its end-of-stream marker (decompressobj.eof "
            f"is False), so the download was TRUNCATED. This check is the whole defence: "
            f"decompressobj does not raise on a truncated stream — it returns the bytes it could "
            f"decode, which reads downstream as a small-but-valid source. Silent data loss."
        )

    # Only now is the tail safe to emit: `eof` proved the stream complete, so a remainder with no
    # trailing newline is a WHOLE final record, not a cut-off one. Verified that a member whose
    # last line lacks "\n" is real, so dropping this would lose one document per shard.
    if pending.strip():
        yield pending


def _range_chunks(rf: Any, *, chunk_bytes: int = JSONL_CHUNK_BYTES) -> Iterator[bytes]:
    """Sequential `chunk_bytes` reads off a `_RangeFile` until the object is exhausted.

    `_RangeFile.read` returns EXACTLY the requested count or raises, looping internally over
    short 206 bodies (`ingest_reservoir.py:365-402`) — which is what keeps a throttled,
    mid-body-cut response from silently shortening a member and making `eof` fail for the wrong
    reason.
    """
    while True:
        block = rf.read(chunk_bytes)
        if not block:
            return
        yield block


def read_jsonl_gz_documents(
    repo: str,
    path: str | Mapping[str, Any],
    spec: CorpusSpec,
    headers: Mapping[str, str] | None = None,
    *,
    size: int | None = None,
    domain_map: Mapping[str, str] | None = None,
    chunks: Iterable[bytes] | None = None,
) -> Iterator[Document]:
    """Stream one `.json.gz` shard as :class:`~.corpus.Document` objects.

    **This is the flagged-risk path: nothing in this package read `.json.gz` before.** It matters
    because EVERY Common Pile source ships it and ships no parquet at all — verified,
    `peS2o_filtered` has 93 files and 0 of them parquet
    (`artifacts/smoke/harvest_parquet.py:125`), and the `_filtered` repos that §3.2 actually
    specifies were unmeasurable in Phase 0 for exactly this reason
    (`artifacts/recount/code.json:23`). That covers peS2o, pubmed, arxiv, stackexchange,
    stackv2-edu, github-archive and ubuntu-irc — the academic, code and QA/forum categories.

    Column resolution is by dotted path as in the parquet reader, but there is NO schema to check
    it against: JSON has no footer. So a `text_column` typo cannot be caught up front and instead
    surfaces as "0 documents decoded", which is why :func:`filter_documents` counts what it drops
    rather than only what it keeps.

    UNVERIFIED: the exact field names of each `_filtered` repo's records. `text` is used by prior
    art in this repo against real peS2o bytes (`artifacts/recount/_filtered_tpb.py:85`), so the
    payload key is confirmed for that source; the `id` and metadata keys are not. To settle one:
        python3 artifacts/recount/_filtered_tpb.py  # reads real shard heads over HTTP Range
    """
    if isinstance(path, Mapping):
        size = path.get("size", size)
        path = str(path["path"])

    if chunks is None:
        if size is None:
            raise ReadError(
                f"{spec.key}: reading {path!r} over HTTP Range needs the object size; pass size= "
                f"or the hf_tree entry dict."
            )
        _RATE_GATE.wait()
        chunks = _range_chunks(
            _RangeFile(_pinned_url(repo, str(path), spec.revision), int(size),
                       dict(headers or _hf_headers()))
        )

    where = f"{repo}/{path}"
    for lineno, line in enumerate(_gunzip_lines(chunks, where=where), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            # Reached only for a line the gzip layer proved COMPLETE, so this is malformed JSON
            # rather than a chunk boundary — the case prior art conflated with the truncated tail
            # and skipped. A build must not skip it silently.
            raise ReadError(
                f"{where}:{lineno}: line is not valid JSON ({exc}). The gzip layer already "
                f"proved this line complete, so it is malformed upstream, not a range-boundary "
                f"artifact."
            ) from exc
        if not isinstance(record, Mapping):
            raise ReadError(
                f"{where}:{lineno}: expected a JSON object per line, got {type(record).__name__}."
            )

        text = _json_walk(record, spec.text_column)
        if text is None and lineno == 1:
            # ⚠️ A WRONG `text_column` MUST NOT READ AS AN EMPTY CORPUS. JSON has no footer, so
            # unlike the parquet path there is nothing to validate the selector against up front —
            # and `continue`-ing on a missing key (which is what this did) means a typo, or the
            # literal string "UNVERIFIED", skips EVERY record and yields zero documents while
            # reporting success. Measured: `ubuntu_irc_filtered` with `text_column="UNVERIFIED"`
            # returned 0 documents and raised nothing.
            #
            # The first record is the cheapest place to catch it, and a real corpus whose very
            # first line legitimately lacks the text field does not exist in this registry.
            raise ReadError(
                f"{where}:1: no {spec.text_column!r} in the first record; keys are "
                f"{sorted(record)[:12]}. A missing text column would otherwise skip every record "
                f"and yield an EMPTY corpus with no error — the json.gz twin of the "
                f"zero-columns parquet trap in this module's docstring. Fix the registry row."
            )
        if not isinstance(text, str) or not text:
            # Past the first record an absent or empty text is a legitimately unusable document
            # (upstream filtering leaves a few), not a schema error.
            continue
        doc_id = _json_walk(record, spec.id_column)
        if doc_id is None:
            raise ReadError(
                f"{where}:{lineno}: no {spec.id_column!r} in this record. The id is the join key "
                f"for the id partition and the anti-join (corpus.py:176-179); a line number "
                f"would not survive a re-download."
            )
        yield Document(
            id=str(doc_id),
            text=text,
            source=spec.source_label,
            domain=_domain_of(record, spec, domain_map=domain_map),
        )


#: Which reader handles which `CorpusSpec.file_format`. **THE SINGLE SOURCE OF TRUTH for what
#: this package can read** — `READABLE_FORMATS` below is derived from it, and
#: `corpus_build` re-exports that rather than keeping a list of its own.
#:
#: It used to be one of THREE tables. `corpus_build.READABLE_FORMATS` gated the plan, a private
#: dict inside `corpus_build._reader_for` did the live dispatch, and this one served
#: `read_documents`; the first two omitted `jsonl.gz` while this one had it, so a `jsonl.gz`
#: registry row was refused at plan time **although the reader for it is right here and works** —
#: `read_jsonl_gz_documents` serves both gzip keys. The rejection looked exactly like a
#: legitimate format check, which is what made it dangerous. Three lists that agree today diverge
#: again on the next edit, so the fix is derivation, not synchronisation.
#:
#: Both spellings of the gzip form are live upstream — the dolmino `math` prefix mixes `*.jsonl`,
#: `*.jsonl.gz`, `*.json.gz` and `*.json.zst` in ONE directory
#: (`artifacts/reservoir/WEEK1-CORPUS-SURVEY.md:111`), so a registry row naming either spelling
#: must resolve.
#:
#: **Values are function NAMES, not function objects, and that is load-bearing.** Holding the
#: callable would bind whatever object existed at import time, so the offline tests that swap in a
#: fake by assigning `corpus_read.read_parquet_documents` (`test_corpus_build.py:663,756`) would
#: drive the ORIGINAL reader while reporting success — the mock-that-does-nothing failure this
#: module's tests are written to avoid. :func:`reader_for_format` resolves the name on every call.
_READERS: dict[str, str] = {
    "parquet": "read_parquet_documents",
    "json.gz": "read_jsonl_gz_documents",
    "jsonl.gz": "read_jsonl_gz_documents",
}

#: Every `CorpusSpec.file_format` a reader exists for. DERIVED from :data:`_READERS`, never
#: written out again: registering a reader is what widens the plan-time gate, so the gate
#: structurally cannot lag behind the readers. `corpus_build` imports this object itself.
READABLE_FORMATS: frozenset[str] = frozenset(_READERS)


def reader_for_format(file_format: str) -> Callable[..., Iterator[Document]] | None:
    """The reader registered for `file_format`, or ``None`` if there is none.

    The one dispatch lookup in the package — :func:`read_documents` and the build driver's
    `_reader_for` both come through here, so "which formats can be read" has exactly one answer.

    Resolved by NAME out of this module's namespace on every call, so a test that replaces
    `corpus_read.read_parquet_documents` is honoured. See :data:`_READERS`.
    """
    name = _READERS.get(file_format)
    if name is None:
        return None
    try:
        return globals()[name]
    except KeyError:  # pragma: no cover - a typo in _READERS, caught by the registry test
        raise ReadError(
            f"_READERS maps {file_format!r} to {name!r}, which is not a function in "
            f"corpus_read. The reader table names its readers, so a rename must update it."
        ) from None


def read_documents(
    repo: str,
    path: str | Mapping[str, Any],
    spec: CorpusSpec,
    headers: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> Iterator[Document]:
    """Dispatch to the reader for `spec.file_format`.

    The seam the build driver reads through, so the per-source loop does not grow a format `if` —
    and so there is no second dispatch table to drift from this one. It refuses an unknown format
    rather than defaulting: upstream filenames lie about compression (`WEEK1-CORPUS-SURVEY.md`'s
    trap 1 — suffix dispatch throws `BadGzipFile` mid-stream, hours in, on a subset of shards), so
    a format this module has not been taught is a registry bug to fix, not something to sniff at
    read time.
    """
    reader = reader_for_format(spec.file_format)
    if reader is None:
        raise ReadError(
            f"{spec.key}: file_format {spec.file_format!r} has no reader. Known: "
            f"{sorted(READABLE_FORMATS)}. `.zst` is NOT among them — Common Pile ships some "
            f"prefixes as `.json.zst`, which needs a zstandard dependency this package does not "
            f"declare."
        )
    return reader(repo, path, spec, headers, **kwargs)


# --------------------------------------------------------------------------------------
# The short-document filter — the EOS-fraction floor made mechanical
# --------------------------------------------------------------------------------------


@dataclass
class FilterStats:
    """What :func:`filter_documents` kept and threw away, per source.

    Exists because the failure it guards is a *quiet* one. §3.3's FinePhrase quality note is a
    hard gate, not advice: a measured `>=50`-token minimum removes **3.4-12.6% of documents**
    depending on config (`artifacts/recount/synthetic.json:173`), and `math` at a higher
    threshold loses far more. A build that discards 40% of a source and reports only its output
    size looks identical to a build whose source was small. So the drop counts are part of the
    output, not a log line.

    Token totals are tracked as well as document counts, because the constraint being defended is
    a MEAN: one EOS per document makes a packed shard's EOS fraction exactly
    ``1 / mean_doc_tokens``, and the family bound of 0.05 puts the failure threshold at a mean of
    20 tokens (`corpus.py:121-140`). :attr:`mean_kept_tokens` is therefore the number that
    predicts whether the shards this source produces will pass the decode smoke test — recomputed
    from the real lengths rather than assumed from the floor.
    """

    min_tokens: int = MIN_DOC_TOKENS
    seen: int = 0
    kept: int = 0
    dropped_short: int = 0
    dropped_empty: int = 0
    kept_tokens: int = 0
    dropped_tokens: int = 0
    #: A bounded sample of dropped documents' ids, so a surprising drop rate can be inspected
    #: instead of only counted. Bounded because a 40% drop over 339 M documents is 135 M ids.
    sample_dropped: list[str] = field(default_factory=list)
    sample_limit: int = 20

    @property
    def dropped(self) -> int:
        return self.dropped_short + self.dropped_empty

    @property
    def accounted(self) -> int:
        """``kept + dropped_short + dropped_empty`` — what :attr:`seen` must equal.

        Named, rather than left implicit, because these counters are no longer maintained by
        :func:`filter_documents`'s single three-way branch. The length filter moved into
        ``corpus_pack.tokenize_documents`` (measured: the separate pass was ~91% of the build's
        compute on 1 of 32 cores), where ``seen`` is incremented in one place and the three
        outcomes in three others, across a batched loop the consumer can abandon mid-batch. That
        arrangement has ALREADY produced a miscount once — ``corpus_pack.py:383-388`` records
        596 against 308 when ``seen`` was counted per batch instead of per document. A closure
        that used to hold by construction now holds by maintenance, so it is worth recomputing.
        """
        return self.kept + self.dropped_short + self.dropped_empty

    @property
    def drop_fraction(self) -> float:
        return (self.dropped / self.seen) if self.seen else 0.0

    @property
    def mean_kept_tokens(self) -> float:
        """Mean tokens per SURVIVING document — i.e. ``1 / eos_fraction`` for the packed shards."""
        return (self.kept_tokens / self.kept) if self.kept else 0.0

    @property
    def predicted_eos_fraction(self) -> float:
        """The EOS fraction the packer will produce, recomputed from real document lengths.

        One EOS per document, so this is ``1 / mean_kept_tokens`` — the exact quantity the decode
        smoke test samples and bounds at 0.05 (`corpus.py:117-140`).
        """
        mean = self.mean_kept_tokens
        return (1.0 / mean) if mean else 0.0

    def problems(self, *, max_drop_fraction: float = 0.4) -> list[str]:
        """Recomputed complaints about this source — empty means it is publishable as-is.

        Deliberately returns strings instead of raising: a build reads several sources and the
        useful output is "which of these is wrong", not the first exception. The caller decides
        whether a 40% drop is acceptable for a given source; this only refuses to let it pass
        unmentioned.

        ⚠️ **Read :attr:`mean_kept_tokens`'s clause below knowing it is ANTI-CORRELATED with the
        failure it looks like it guards, and that :attr:`drop_fraction`'s clause is the only one
        that catches that shape.** MEASURED 2026-08-08 on the `reddit_to_flashcards` distribution
        (mean 54.4 tokens, CV 0.212) at the default ``min_tokens=64``: **79.0%** of documents are
        dropped, and the survivors' mean is **70.09** tokens — **3.5x CLEAR** of the 20-token floor.
        Trimming the bottom of a distribution RAISES the mean of what is left, so the harder a
        source fails this way, the safer the mean clause reports it. Only the drop-rate clause
        fires (0.790 against 0.4). Deleting the drop-rate clause as redundant would leave nothing.
        """
        out: list[str] = []
        # First, because every count below is read off these fields and a broken closure makes all
        # of them meaningless. `accounted`'s docstring has the measured miscount this catches.
        if self.seen != self.accounted:
            out.append(
                f"the counters do not close: seen {self.seen} but {self.kept} kept + "
                f"{self.dropped_short} short + {self.dropped_empty} empty = {self.accounted} "
                f"({self.seen - self.accounted:+d} unaccounted). Every document is kept, dropped "
                f"short, or dropped empty; there is no fourth channel, so the drop rate below is "
                f"not a rate of anything and must not be read as one."
            )
        if self.seen and not self.kept:
            out.append(
                f"every one of {self.seen} documents was dropped. If the drop reason is "
                f"'short', check the text column: a wrong or empty column yields no text and "
                f"looks exactly like a corpus of short documents."
            )
        if self.drop_fraction > max_drop_fraction:
            out.append(
                f"dropped {self.dropped}/{self.seen} documents ({self.drop_fraction:.1%}), over "
                f"the {max_drop_fraction:.0%} threshold. Not automatically wrong — §3.3 expects "
                f"3.4-12.6% on FinePhrase — but at this rate the pool arithmetic in §2.1/§3.2 "
                f"was computed on tokens this source will not deliver."
            )
        if self.kept and self.mean_kept_tokens < MIN_MEAN_DOC_TOKENS:
            out.append(
                f"mean kept length is {self.mean_kept_tokens:.1f} tokens, under the "
                f"{MIN_MEAN_DOC_TOKENS}-token floor, so the packed shards land at EOS fraction "
                f"{self.predicted_eos_fraction:.4f} and the decode smoke test REJECTS them "
                f"after the tokenize and the upload. This is unreachable at the default "
                f"min_tokens={MIN_DOC_TOKENS} and means the floor was lowered."
            )
        return out


def filter_documents(
    docs: Iterable[Document],
    tokenizer_len: Callable[[str], int],
    *,
    min_tokens: int = MIN_DOC_TOKENS,
    stats: FilterStats | None = None,
) -> Iterator[Document]:
    """Drop documents shorter than `min_tokens`, counting the losses into `stats`.

    **Not a quality preference — it is what makes the synthetic half publishable at all.** One EOS
    per document means a packed shard's EOS fraction IS ``1 / mean_doc_tokens``, and
    `families/pretrain.json`'s `eos_fraction_max` of 0.05 therefore rejects any shard whose mean
    document is under 20 tokens (`corpus.py:114-140`). A sampled FinePhrase rewrite was the entire
    string *"Question: Can light accelerate to the speed of light?"* — about 12 tokens. Shards
    packed from documents like that are rejected AFTER the tokenize and the upload have been paid
    for.

    The default floor is 64, not 20, because a *mean* of 20 is the failure threshold rather than a
    safe operating point — a distribution centred on the limit fails half its shards. At 64 the
    worst possible shard mean is 64, an EOS fraction of 0.0156 and a 3.2x margin
    (`corpus.py:142-150`).

    Documents are DROPPED, never padded or concatenated: padding invents tokens the tokenizer
    never emitted, and merging two documents destroys the boundary the EOS marks.

    `tokenizer_len` is a callable so this needs no tokenizer to test and so the real build can
    pass the pinned dolma2 tokenizer's own length. Pass `stats=FilterStats()` and read it after
    the iterator is exhausted — a generator cannot return a value, and the counts are only
    complete once the stream is.
    """
    if min_tokens < 1:
        raise ReadError(
            f"min_tokens must be at least 1; got {min_tokens}. A floor of 0 admits empty "
            f"documents, which contribute one EOS and no content — the exact shape that drives "
            f"the EOS fraction to 1.0."
        )
    if stats is None:
        stats = FilterStats(min_tokens=min_tokens)
    stats.min_tokens = min_tokens

    for doc in docs:
        stats.seen += 1
        if not doc.text:
            stats.dropped_empty += 1
            if len(stats.sample_dropped) < stats.sample_limit:
                stats.sample_dropped.append(doc.id)
            continue
        n = tokenizer_len(doc.text)
        if n < min_tokens:
            stats.dropped_short += 1
            stats.dropped_tokens += n
            if len(stats.sample_dropped) < stats.sample_limit:
                stats.sample_dropped.append(doc.id)
            continue
        stats.kept += 1
        stats.kept_tokens += n
        yield doc
