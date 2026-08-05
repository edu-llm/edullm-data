"""render_readme() tests. The README is a DERIVED artifact (§3): its content is a pure function
of dataset.json, so every test here is metadata-in / markdown-out, no AWS. The point of the
generator is: render the real facts, carry the one curated prose block, and — critically — omit a
section rather than fabricate one when its data is absent."""

from __future__ import annotations

from edullm_data.readme import render_readme

# A rich dataset.json shaped like the enriched olmo-mix corpus: sources with upstream scope,
# an about block, a tokenizer dependency, a real license, and a token partition.
RICH = {
    "schema_version": "edullm-dataset/v1",
    "dataset_id": "pretrain/olmo-mix-1124-31b",
    "version": {"id": "v1", "relation": "supersedes", "of": None},
    "created_at": "2026-07-29T04:00:00+00:00",
    "owner": "edullm-data@alphaaiengineering.com",
    "purpose": "OLMo-mix-1124 ~31B-token pretraining corpus for 370M/1B ladder runs",
    "mutability": "frozen",
    "inventory": {"objects": 218, "bytes": 125336003336},
    "about": "A document-trimmed ~31B-token subset of allenai/olmo-mix-1124, dolma2-tokenized.",
    "sources": [
        {"name": "DCLM-Baseline", "tokens": 3700000000000, "documents": 2950000000,
         "license": "CC-BY-4.0", "uri": "https://huggingface.co/datasets/allenai/olmo-mix-1124",
         "scope": "upstream-full-collection"},
        {"name": "pes2o", "tokens": 58600000000, "license": "ODC-By-1.0",
         "scope": "upstream-full-collection"},
        {"name": "wiki", "tokens": 3660000000, "license": "ODC-By-1.0",
         "scope": "upstream-full-collection"},
    ],
    "groups": [
        {"name": "tokens", "profile": "pretrain-tokens/v1", "prefix": "tokens/",
         "manifest": "tokens/manifest.json", "manifest_sha256": "f0" * 32,
         "depends_on": [{"role": "tokenizer", "dataset_id": "tokenizer/dolma2-bpe",
                         "version": "v1", "manifest_sha256": "b3" * 32}],
         "partitions": [{"name": "train", "by": "path", "glob": "train-*.u32le.bin",
                         "rows": 31334000834}]},
    ],
    "build": {"executor": {"kind": "aws-batch"}, "reproducibility": "logical"},
    "license": {"id": "ODC-By-1.0", "basis": "declared"},
    "limitations": [{"kind": "contamination", "benchmark": "gsm8k", "overlap_rate": 0.003}],
}

# A thin dataset.json like a freshly-published corpus before enrichment: no sources, no about,
# no license id. The generator must degrade gracefully, not emit empty tables.
THIN = {
    "schema_version": "edullm-dataset/v1",
    "dataset_id": "pretrain/fineweb-edu-10b",
    "version": {"id": "v1", "relation": "supersedes", "of": None},
    "created_at": "2026-07-29T04:00:00+00:00",
    "owner": "someone@example.com",
    "purpose": "10B-token FineWeb-Edu corpus for 150M smoke pretraining",
    "mutability": "frozen",
    "inventory": {"objects": 3, "bytes": 120000000},
    "sources": [],
    "groups": [
        {"name": "tokens", "profile": "pretrain-tokens/v1", "prefix": "tokens/",
         "manifest": "tokens/manifest.json", "manifest_sha256": "aa" * 32,
         "partitions": [{"name": "train", "by": "path", "glob": "train-*.u32le.bin", "rows": 30000000}]},
    ],
    "build": {"executor": {"kind": "external"}},
    "license": {"id": None, "basis": "unknown"},
}


def test_title_purpose_and_footer():
    md = render_readme(RICH, generator_version="0.1.0")
    assert md.startswith("# pretrain/olmo-mix-1124-31b — v1")
    assert "_OLMo-mix-1124 ~31B-token pretraining corpus" in md
    assert "Generated from `dataset.json` by edullm-data v0.1.0" in md
    assert "Do not edit by hand" in md
    assert md.endswith("\n") and not md.endswith("\n\n")


def test_about_block_rendered():
    md = render_readme(RICH)
    assert "## About" in md
    assert "document-trimmed ~31B-token subset" in md


def test_sources_table_and_upstream_caveat():
    md = render_readme(RICH)
    assert "## Data mix / sources" in md
    # a markdown table with the source names and upstream token counts (thousands-separated)
    assert "| Source |" in md
    assert "DCLM-Baseline" in md
    assert "3,700,000,000,000" in md  # upstream tokens, formatted
    assert "CC-BY-4.0" in md
    # the honesty caveat: these are upstream-collection figures, not this dataset's measured mix
    assert "upstream source collection" in md
    assert "not a measured" in md
    # uri rendered as a link
    assert "https://huggingface.co/datasets/allenai/olmo-mix-1124" in md


def test_contents_tokenizer_license_limitations_provenance():
    md = render_readme(RICH)
    assert "## Contents" in md
    assert "218" in md and "GiB" in md  # inventory summary
    assert "`tokens` — pretrain-tokens/v1" in md
    assert "31,334,000,834 rows" in md  # partition rows, formatted
    assert "## Tokenizer" in md
    assert "tokenizer/dolma2-bpe/v1" in md
    assert "## License" in md and "ODC-By-1.0" in md and "declared" in md
    assert "## Limitations" in md and "contamination" in md and "gsm8k" in md
    assert "## Provenance" in md and "aws-batch" in md


def test_how_to_read_snippet_uses_real_ids():
    md = render_readme(RICH)
    assert "## How to read it" in md
    assert 'dataset_paths("pretrain/olmo-mix-1124-31b", "v1"' in md
    assert 'split="train"' in md
    assert "do NOT let the loader default to uint16" in md


def test_thin_dataset_omits_absent_sections_cleanly():
    md = render_readme(THIN)
    # no sources[] -> NO data-mix section at all (never an empty table, never a fake claim)
    assert "## Data mix / sources" not in md
    # no about -> no About section
    assert "## About" not in md
    # no tokenizer dependency -> no Tokenizer section
    assert "## Tokenizer" not in md
    # unknown license still renders honestly
    assert "## License" in md and "unknown" in md
    # but the always-present pieces are there
    assert md.startswith("# pretrain/fineweb-edu-10b — v1")
    assert "## Contents" in md
    assert "## How to read it" in md


def test_no_split_arg_when_no_partitions():
    ds = dict(THIN)
    ds["groups"] = [{"name": "g", "profile": "tabular/v1", "prefix": "g/",
                     "manifest": "g/manifest.json", "manifest_sha256": "cc" * 32}]
    md = render_readme(ds)
    assert "split=" not in md  # nothing to select, so don't suggest a split


def test_pipe_in_source_name_does_not_break_table():
    ds = dict(RICH)
    ds["sources"] = [{"name": "weird|name", "tokens": 5}]
    md = render_readme(ds)
    assert "weird\\|name" in md  # escaped, so the column count is preserved


# --------------------------------------------------------------------------------------
# the generated snippet must actually RUN
# --------------------------------------------------------------------------------------

# Two groups: exactly the shape where `dataset_paths(id, ver, s3=...)` RAISES.
MULTIGROUP = {
    "schema_version": "edullm-dataset/v1",
    "dataset_id": "eval/mmlu-suite",
    "version": {"id": "v2", "relation": "supersedes", "of": "v1"},
    "mutability": "frozen",
    "inventory": {"objects": 6, "bytes": 4096},
    "groups": [
        {"name": "results", "profile": "eval-results/v1", "prefix": "results/",
         "manifest": "results/manifest.json", "manifest_sha256": "11" * 32,
         "partitions": [{"name": "test", "by": "path", "glob": "test-*.jsonl"}]},
        {"name": "predictions", "profile": "eval-results/v1", "prefix": "predictions/",
         "manifest": "predictions/manifest.json", "manifest_sha256": "22" * 32,
         "partitions": [{"name": "test", "by": "path", "glob": "test-*.jsonl"}]},
    ],
}


def test_multigroup_snippet_passes_group_so_it_does_not_raise():
    """Without group=, this exact call raises ("pass group= to choose one" — read.py).

    A generated snippet that cannot execute is worse than none: it is the documented path, so
    the first thing a reader does is hit the error the README told them to.
    """
    md = render_readme(MULTIGROUP)
    assert 'dataset_paths("eval/mmlu-suite", "v2", group="results"' in md
    # and it names the alternatives rather than presenting the pick as authoritative
    assert '"predictions"' in md
    assert "group= is REQUIRED here" in md


def test_single_group_snippet_omits_group_arg():
    """One group needs no group= — dataset_paths defaults to it. Adding it would be noise."""
    md = render_readme(RICH)
    assert "group=" not in md
    assert 'dataset_paths("pretrain/olmo-mix-1124-31b", "v1", split="train"' in md


def test_multigroup_split_comes_from_the_group_the_snippet_reads():
    """A split declared only by a DIFFERENT group would resolve to an empty result in this call,
    so the split= must be taken from the chosen group, not from any group."""
    ds = dict(MULTIGROUP)
    ds["groups"] = [
        # chosen group declares NO partitions ...
        {"name": "vendored", "profile": "vendored/v1", "prefix": "vendored/",
         "manifest": "vendored/manifest.json", "manifest_sha256": "33" * 32},
        # ... while a later one does. split= must not be borrowed from it.
        {"name": "results", "profile": "eval-results/v1", "prefix": "results/",
         "manifest": "results/manifest.json", "manifest_sha256": "44" * 32,
         "partitions": [{"name": "test", "by": "path", "glob": "test-*.jsonl"}]},
    ]
    md = render_readme(ds)
    assert 'group="vendored"' in md
    assert "split=" not in md


def test_generated_snippet_is_executable_python():
    """Parse the fenced block. Catches an unbalanced quote or a stray comma in the call."""
    import ast

    for ds in (RICH, THIN, MULTIGROUP):
        md = render_readme(ds)
        block = md.split("```python", 1)[1].split("```", 1)[0]
        ast.parse(block)  # raises SyntaxError if the generator emitted broken code


def test_snippet_mentions_numpy_dtype_for_byte_order():
    """dtype alone is not enough to read the bytes on an arbitrary host."""
    md = render_readme(RICH)
    assert "r.numpy_dtype" in md


# --------------------------------------------------------------------------------------
# The token/doc column headers must agree with `scope`
# --------------------------------------------------------------------------------------


def test_measured_sources_are_not_labelled_upstream():
    """A source with NO `scope` carries counts measured for THIS dataset, so the header must not say
    "Upstream tokens" — that tells the consumer the opposite of the truth.

    Found while publishing `pretrain/reservoir-dolma2`, whose per-source counts come from build
    receipts: the numbers were right and the column heading contradicted them.
    """
    md = render_readme(
        {
            "dataset_id": "pretrain/x",
            "version": "v1",
            "purpose": "p",
            "sources": [{"name": "a", "tokens": 123, "documents": 7}],
        }
    )
    assert "| Tokens |" in md
    assert "| Documents |" in md
    assert "Upstream tokens" not in md
    assert "Upstream docs" not in md


def test_upstream_scoped_sources_keep_the_upstream_headers():
    """The other half of the control: when `scope` says the figures describe the upstream
    collection, the headers must say so, and the caveat paragraph must appear."""
    md = render_readme(
        {
            "dataset_id": "pretrain/x",
            "version": "v1",
            "purpose": "p",
            "sources": [
                {
                    "name": "a",
                    "tokens": 123,
                    "documents": 7,
                    "scope": "upstream-full-collection",
                }
            ],
        }
    )
    assert "| Upstream tokens |" in md
    assert "| Upstream docs |" in md
    assert "provenance, not as this dataset's mix" in md


def test_the_upstream_link_column_is_always_called_upstream():
    """`uri` points at where the data came from, which is upstream whether or not the counts were
    measured locally. Only the count columns move."""
    md = render_readme(
        {
            "dataset_id": "pretrain/x",
            "version": "v1",
            "purpose": "p",
            "sources": [{"name": "a", "tokens": 1, "uri": "https://example.invalid/d"}],
        }
    )
    assert "| Upstream |" in md
    assert "| Tokens |" in md


# --------------------------------------------------------------------------------------
# `notes` must actually reach the README
# --------------------------------------------------------------------------------------


def test_notes_are_rendered_not_silently_dropped():
    """`notes` was accepted by publish(), stored in dataset.json, and never rendered — so nothing a
    producer wrote there reached a consumer.

    Found while publishing `pretrain/reservoir-dolma2`, whose notes carry the mixed-license
    disclosure (7.13% of train is CC-BY-SA-4.0 share-alike, which constrains redistribution). A
    consumer cannot recompute that from the shards.
    """
    md = render_readme(
        {
            "dataset_id": "pretrain/x",
            "version": "v1",
            "purpose": "p",
            "notes": "Licensing is MIXED and includes share-alike.",
        }
    )
    assert "## Notes" in md
    assert "Licensing is MIXED and includes share-alike." in md


def test_absent_notes_render_no_section():
    """The module's first design rule: never fabricate a section. An empty Notes heading would read
    as 'there are no caveats', which is a stronger claim than 'none recorded'."""
    md = render_readme({"dataset_id": "pretrain/x", "version": "v1", "purpose": "p"})
    assert "## Notes" not in md
    for blank in ("", "   ", "\n"):
        md = render_readme(
            {"dataset_id": "pretrain/x", "version": "v1", "purpose": "p", "notes": blank}
        )
        assert "## Notes" not in md, f"blank notes {blank!r} produced a section"


def test_notes_sit_between_license_and_limitations():
    """Ordering is deliberate: notes usually qualify the license (as they do for the reservoir, where
    the single license id would be a lie), so they must not appear after the limitations."""
    md = render_readme(
        {
            "dataset_id": "pretrain/x",
            "version": "v1",
            "purpose": "p",
            "license": {"id": "unknown", "basis": "mixed"},
            "notes": "NOTE_BODY",
            "limitations": [{"kind": "k", "detail": "LIMITATION_BODY"}],
        }
    )
    assert md.index("## License") < md.index("## Notes") < md.index("## Limitations")
