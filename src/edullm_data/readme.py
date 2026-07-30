"""render_readme() — the per-dataset README, generated from dataset.json (§3).

The standard is explicit that the README is a *derived* artifact: §3 says the free-text
``notes`` and ``limitations[]`` fields "exist because the README is generated", and §2 rejects
a ``purpose`` of ``"see README"`` precisely because "the README is generated *from* this field".
So this module takes a ``dataset.json`` dict and returns markdown — one source of truth, no file
a human edits, nothing that can drift from the manifest.

Two design rules this file exists to honor:

* **Never fabricate a section.** Every block is omitted when its data is absent. A dataset with
  ``sources: []`` gets no data-mix table rather than an empty one — an empty table reads as "no
  sources", which is a stronger (and false) claim than "not recorded". §0.1: inventing a claim to
  render is exactly the unearned confidence the standard is against.
* **Honest scope on the mix.** A corpus that is a trimmed subset of a larger upstream collection
  must not present the upstream's full-collection proportions as its own measured mix. A source may
  carry ``scope`` (e.g. ``"upstream-full-collection"``); when any source is upstream-scoped, the
  section prints a one-line caveat so the numbers are read as provenance, not as this dataset's
  measured breakdown.

Pure: no I/O, no AWS, no ``datetime`` — it is called from ``promote()`` (the validator, the only
writer to the data bucket) and from a backfill driver, and must be testable with neither.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = ["render_readme"]


def _fmt_int(n: Any) -> str:
    """Thousands-separated int, or the value unchanged if it isn't one."""
    if isinstance(n, bool) or not isinstance(n, int):
        return str(n)
    return f"{n:,}"


def _fmt_bytes(n: Any) -> str:
    """Human byte size (GiB/MiB) alongside the exact count, so the README is both scannable
    and exact. Not a claim the validator checks — the manifest's ``bytes`` is authoritative;
    this is presentation only."""
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        return str(n)
    for unit, scale in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024)):
        if n >= scale:
            return f"{n / scale:.1f} {unit} ({n:,} bytes)"
    return f"{n:,} bytes"


def _count_str(count: Mapping[str, Any] | None) -> str | None:
    if not isinstance(count, Mapping):
        return None
    unit = count.get("unit")
    value = count.get("value")
    if unit is None or value is None:
        return None
    return f"{_fmt_int(value)} {unit}"


def _md_escape_cell(s: Any) -> str:
    """Escape a value for a markdown table cell (pipes would break the column)."""
    return str(s).replace("|", "\\|").replace("\n", " ")


def _sources_section(sources: Sequence[Mapping[str, Any]]) -> list[str]:
    """Data-mix / sources table from ``sources[]``. Columns are rendered only when at least one
    source carries them, so a sparse ``sources[]`` doesn't produce a wall of empty cells."""
    rows = [s for s in sources if isinstance(s, Mapping)]
    if not rows:
        return []

    # Decide which optional columns to show based on what the data actually has.
    show_share = any(r.get("share") is not None for r in rows)
    show_tokens = any(r.get("tokens") is not None for r in rows)
    show_docs = any(r.get("documents") is not None for r in rows)
    show_license = any(r.get("license") for r in rows)
    show_uri = any(r.get("uri") for r in rows)
    upstream_scoped = any(str(r.get("scope", "")).startswith("upstream") for r in rows)

    header = ["Source"]
    if show_share:
        header.append("Share")
    if show_tokens:
        header.append("Upstream tokens")
    if show_docs:
        header.append("Upstream docs")
    if show_license:
        header.append("License")
    if show_uri:
        header.append("Upstream")

    lines = ["## Data mix / sources", ""]
    if upstream_scoped:
        lines += [
            "> These figures describe the **upstream source collection**, not a measured "
            "per-source breakdown of *this* dataset. This dataset is a subset/derivation of "
            "the collection below; the proportions of each source within it were not "
            "separately measured. Treat the table as provenance, not as this dataset's mix.",
            "",
        ]

    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for r in rows:
        cells = [_md_escape_cell(r.get("name") or r.get("id") or "?")]
        if show_share:
            share = r.get("share")
            cells.append("" if share is None else _md_escape_cell(share))
        if show_tokens:
            tok = r.get("tokens")
            cells.append("" if tok is None else _fmt_int(tok))
        if show_docs:
            docs = r.get("documents")
            cells.append("" if docs is None else _fmt_int(docs))
        if show_license:
            cells.append(_md_escape_cell(r.get("license") or ""))
        if show_uri:
            uri = r.get("uri")
            cells.append(f"[link]({uri})" if uri else "")
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def _contents_section(ds: Mapping[str, Any]) -> list[str]:
    groups = ds.get("groups") or []
    if not groups:
        return []
    inv = ds.get("inventory") or {}
    lines = ["## Contents", ""]
    tot_obj = inv.get("objects")
    tot_bytes = inv.get("bytes")
    if tot_obj is not None or tot_bytes is not None:
        summary = []
        if tot_obj is not None:
            summary.append(f"**{_fmt_int(tot_obj)}** objects")
        if tot_bytes is not None:
            summary.append(_fmt_bytes(tot_bytes))
        lines += [" · ".join(summary), ""]

    for g in groups:
        if not isinstance(g, Mapping):
            continue
        gname = g.get("name", "?")
        profile = g.get("profile", "?")
        lines.append(f"### `{gname}` — {profile}")
        lines.append("")
        parts = g.get("partitions") or []
        rendered_partition = False
        for p in parts:
            if not isinstance(p, Mapping):
                continue
            pname = p.get("name", "?")
            rows = p.get("rows")
            by = p.get("by")
            detail = f"`{pname}`"
            if rows is not None:
                detail += f" — {_fmt_int(rows)} rows"
            if by:
                detail += f" (by {by})"
            lines.append(f"- split {detail}")
            rendered_partition = True
        if rendered_partition:
            lines.append("")
    return lines


def _tokenizer_section(ds: Mapping[str, Any]) -> list[str]:
    """The pinned tokenizer, read from any group's ``depends_on`` with role ``tokenizer``.
    This is the corpus's most load-bearing dependency (§ family notes: the shards are
    meaningless integers without it), so it gets its own section."""
    for g in ds.get("groups") or []:
        if not isinstance(g, Mapping):
            continue
        for dep in g.get("depends_on") or []:
            if not isinstance(dep, Mapping):
                continue
            if str(dep.get("role", "")) == "tokenizer" or "tokenizer/" in str(dep.get("dataset_id", "")):
                did = dep.get("dataset_id", "?")
                dver = dep.get("version", "?")
                return [
                    "## Tokenizer",
                    "",
                    f"Tokenized with **`{did}/{dver}`** (a published tokenizer dataset; "
                    f"`vocab_size`/`eos_token_id` are derived from its `tokenizer.json`, not "
                    f"typed here). Read the token dtype from the manifest — these are `uint32`, "
                    f"never the `uint16` default.",
                    "",
                ]
    return []


def _license_section(ds: Mapping[str, Any]) -> list[str]:
    lic = ds.get("license")
    if not isinstance(lic, Mapping):
        return []
    lid = lic.get("id")
    basis = lic.get("basis")
    if lid is None and basis is None:
        return []
    if lid is None:
        body = f"`unknown` (basis: {basis})" if basis else "`unknown`"
    else:
        body = f"**{lid}**" + (f" (basis: {basis})" if basis else "")
    return ["## License", "", body, ""]


def _limitations_section(ds: Mapping[str, Any]) -> list[str]:
    lims = ds.get("limitations") or []
    rows = [lim for lim in lims if isinstance(lim, Mapping)]
    if not rows:
        return []
    lines = ["## Limitations", ""]
    for lim in rows:
        kind = lim.get("kind", "note")
        rest = {k: v for k, v in lim.items() if k != "kind"}
        detail = ", ".join(f"{k}={v}" for k, v in rest.items())
        lines.append(f"- **{kind}**" + (f": {detail}" if detail else ""))
    lines.append("")
    return lines


def _provenance_section(ds: Mapping[str, Any]) -> list[str]:
    build = ds.get("build") or {}
    ex = build.get("executor") or {}
    created = ds.get("created_at")
    kind = ex.get("kind")
    if not kind and not created:
        return []
    lines = ["## Provenance", ""]
    if created:
        lines.append(f"- Created: `{created}`")
    if kind:
        lines.append(f"- Built by: `{kind}`")
    if ds.get("mutability"):
        lines.append(f"- Mutability: `{ds['mutability']}`")
    lines.append("")
    return lines


def _how_to_read_section(ds: Mapping[str, Any]) -> list[str]:
    dataset_id = ds.get("dataset_id", "<family>/<name>")
    ver = ds.get("version") or {}
    version_id = ver.get("id", "vN") if isinstance(ver, Mapping) else "vN"
    groups = [g for g in (ds.get("groups") or []) if isinstance(g, Mapping)]

    # THE SNIPPET MUST RUN. `dataset_paths` RAISES on a dataset with >1 groups unless `group=`
    # is passed ("pass group= to choose one" — read.py), so a multi-group README was publishing
    # a copy-pasteable example whose only outcome is a traceback. A generated snippet that
    # cannot execute is worse than no snippet: it is the documented path, so the first thing a
    # new reader does is hit an error the README told them to hit.
    #
    # Pick the FIRST group deterministically (dataset.json group order is stable, and the file
    # is frozen) rather than inventing a "primary" notion the standard does not have. It is an
    # example, and the comment below points at the alternatives so the choice is visible rather
    # than silently authoritative.
    group_name = groups[0].get("name") if len(groups) > 1 else None
    other_groups = [g.get("name") for g in groups[1:]] if len(groups) > 1 else []

    # Only offer a split= arg if a group actually declares path partitions — and take it from
    # the group the snippet actually reads, since a split declared by a DIFFERENT group would
    # resolve to an empty result in this call.
    split = None
    for g in ([groups[0]] if group_name is not None else groups):
        for p in g.get("partitions") or []:
            if isinstance(p, Mapping) and p.get("name"):
                split = p["name"]
                break
        if split:
            break

    call = (
        f'dataset_paths("{dataset_id}", "{version_id}", '
        + (f'group="{group_name}", ' if group_name else "")
        + (f'split="{split}", ' if split else "")
        + "s3=Boto3S3.default())"
    )
    lines = [
        "## How to read it",
        "",
        "```python",
        "from edullm_data.read import dataset_paths",
        "from edullm_data.s3 import Boto3S3",
        "",
        f"r = {call}",
        "# r.paths  -> the object URIs to feed the loader",
        "# r.dtype  -> read this; do NOT let the loader default to uint16",
        "# r.numpy_dtype -> byte-order-qualified (e.g. \"<u4\"); correct on any host",
    ]
    if other_groups:
        listed = ", ".join(f'"{n}"' for n in other_groups if n)
        lines.append(
            f"# group= is REQUIRED here: this dataset has {len(groups)} groups. Others: {listed}"
        )
    lines += ["```", ""]
    return lines


def render_readme(ds: Mapping[str, Any], *, generator_version: str | None = None) -> str:
    """Render a dataset's ``dataset.json`` dict to a README markdown string.

    Sections appear in a fixed order and each is omitted when its data is absent (see the module
    docstring). ``ds`` is the parsed ``dataset.json``; nothing is read from S3, so this is safe to
    call inside the validator and in tests with no AWS. ``generator_version`` stamps the footer
    (the caller passes ``edullm_data.__version__``); ``None`` leaves it unversioned.
    """
    dataset_id = ds.get("dataset_id", "?")
    ver = ds.get("version") or {}
    version_id = ver.get("id", "?") if isinstance(ver, Mapping) else "?"

    lines: list[str] = [f"# {dataset_id} — {version_id}", ""]

    purpose = ds.get("purpose")
    if purpose:
        lines += [f"_{purpose}_", ""]

    about = ds.get("about")
    if isinstance(about, str) and about.strip():
        # The one curated, free-text block — narrative the derived sections can't carry.
        lines += ["## About", "", about.strip(), ""]

    lines += _sources_section(ds.get("sources") or [])
    lines += _contents_section(ds)
    lines += _tokenizer_section(ds)
    lines += _license_section(ds)
    lines += _limitations_section(ds)
    lines += _provenance_section(ds)
    lines += _how_to_read_section(ds)

    stamp = f"edullm-data{f' v{generator_version}' if generator_version else ''}"
    lines += [
        "---",
        "",
        f"_Generated from `dataset.json` by {stamp}. Do not edit by hand — "
        f"edit the dataset's metadata and re-generate._",
        "",
    ]
    # single trailing newline, no double blank at EOF
    return "\n".join(lines).rstrip("\n") + "\n"
