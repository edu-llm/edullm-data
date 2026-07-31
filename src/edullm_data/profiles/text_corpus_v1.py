"""Profile ``text-corpus/v1`` — raw untokenized documents (§4).

Companion to ``pretrain-tokens/v1``: the same corpus can ship a ``text/`` group of
document JSONL next to a ``tokens/`` group of packed shards. The failure this profile
prevents is publishing "text" that is empty, not JSONL, or missing the declared text
field — which would make a detokenize/compare or raw-document consumer silently wrong.

Rows come from ``.jsonl`` / ``.jsonl.gz`` per manifest entry (Dolma-style). Every
document object must carry a non-empty string at the group's text field (default
``text``). Row counts are **recomputed by parsing the payload**, never trusted from the
manifest alone (§0.4).
"""

from __future__ import annotations

import gzip
import json
from typing import Any, Mapping

from ..manifest import ManifestEntry
from .base import GroupContext, Violation

NAME = "text-corpus/v1"

REQUIRED_FIELDS: Mapping[str, Any] = {
    # Declares the document shape. Must name the text field (default key ``text``).
    # Presence is declarative; VALUES are enforced by CHECKS reading the bytes.
    "record_schema": {"type": "object"},
    # Optional:
    #   text_field (str, default "text"): which record key holds document text
    #   rows_sample (int): cap on rows checked for well-formedness (default: all)
    #   min_text_chars (int, default 1): minimum len(text) after strip
    #   max_identical_fraction (float, default 1.0): if < 1.0, refuse when this fraction
    #     of sampled texts are byte-identical (catches a stuck writer dumping one doc)
}

_DEFAULT_TEXT_FIELD = "text"
_DEFAULT_MIN_TEXT_CHARS = 1


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _object_key(prefix: str, path: str) -> str:
    if not prefix:
        return path
    return prefix.rstrip("/") + "/" + path.lstrip("/")


def _cfg(ctx: GroupContext, key: str, default: Any) -> Any:
    if key in ctx.group:
        return ctx.group[key]
    if key in ctx.family_defaults:
        return ctx.family_defaults[key]
    return default


def _entries(ctx: GroupContext):
    for raw in ctx.manifest.get("entries", []):
        try:
            yield ManifestEntry.from_dict(raw), None
        except Exception as e:
            path = raw.get("path") if isinstance(raw, Mapping) else None
            yield None, Violation("bad-manifest-entry", f"unparseable manifest entry: {e}", path)


def _decompress(path: str, body: bytes) -> bytes:
    if path.lower().endswith(".gz"):
        return gzip.decompress(body)
    return body


def _read_rows(ctx: GroupContext, entry: ManifestEntry) -> list[dict[str, Any]]:
    """Parse a jsonl(.gz) payload into document dicts. Blank lines are skipped.

    Raises ``ValueError`` for an unsupported container so the caller surfaces a
    Violation rather than silently passing an opaque blob as a text corpus.
    """
    key = _object_key(ctx.prefix, entry.path)
    raw = _decompress(entry.path, ctx.s3.get(ctx.landing_bucket, key))
    lower = entry.path.lower()
    if not (lower.endswith(".jsonl") or lower.endswith(".jsonl.gz")):
        raise ValueError(
            f"text-corpus payload {entry.path!r} is not jsonl(.gz); "
            f"cannot recompute document rows"
        )
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"line {line_no}: invalid JSON ({e})") from e
        if not isinstance(obj, dict):
            raise ValueError(f"line {line_no}: document must be a JSON object, got {type(obj).__name__}")
        rows.append(obj)
    return rows


def _text_field(ctx: GroupContext) -> str:
    field = _cfg(ctx, "text_field", _DEFAULT_TEXT_FIELD)
    if not isinstance(field, str) or not field:
        return _DEFAULT_TEXT_FIELD
    return field


def _schema_names_text(schema: Mapping[str, Any], text_field: str) -> bool:
    """True when ``record_schema`` mentions ``text_field`` as a key or in ``required``."""
    if text_field in schema:
        return True
    required = schema.get("required")
    if isinstance(required, list) and text_field in required:
        return True
    props = schema.get("properties")
    if isinstance(props, Mapping) and text_field in props:
        return True
    return False


# --------------------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------------------


def check_record_schema_names_text(ctx: GroupContext) -> list[Violation]:
    """Refuse a group whose ``record_schema`` does not name the text field.

    Schema presence alone is decoration (§0.4); this check only confirms the contract
    points at a field the byte-level checks will actually read.
    """
    schema = ctx.group.get("record_schema")
    if not isinstance(schema, Mapping) or not schema:
        return [
            Violation(
                "missing-record-schema",
                "text-corpus/v1 requires group.record_schema naming the document text "
                "field (default key 'text', or override via text_field)",
            )
        ]
    text_field = _text_field(ctx)
    if not _schema_names_text(schema, text_field):
        return [
            Violation(
                "record-schema-missing-text",
                f"record_schema does not name text field {text_field!r}; declare it as a "
                f"key, under properties, or in required[] so consumers know which field "
                f"holds document text",
            )
        ]
    return []


def check_entries_declare_row_counts(ctx: GroupContext) -> list[Violation]:
    """Every shard must declare ``count{unit: "rows", value}``.

    Precondition for a falsifiable row-count recompute — without a declared unit, a
    size-only lie cannot be caught by comparing against parsed documents.
    """
    out: list[Violation] = []
    for entry, bad in _entries(ctx):
        if bad is not None:
            out.append(bad)
            continue
        count = entry.count
        if not count or count.get("unit") != "rows":
            out.append(
                Violation(
                    "row-count-unit",
                    f"text-corpus shard must declare count.unit == 'rows'; "
                    f"got {None if not count else count.get('unit')!r}",
                    entry.path,
                )
            )
            continue
        value = count.get("value")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            out.append(
                Violation(
                    "row-count-value",
                    f"text-corpus shard count.value must be a non-negative int; got {value!r}",
                    entry.path,
                )
            )
    return out


def check_row_count_recomputed(ctx: GroupContext) -> list[Violation]:
    """Recompute document rows by parsing jsonl and assert equality with declared counts.

    Catches an empty or truncated file whose manifest still claims rows, and a writer that
    stuffed non-JSON or blank padding to inflate ``count`` via newline arithmetic alone.
    """
    out: list[Violation] = []
    total = 0
    for entry, bad in _entries(ctx):
        if bad is not None:
            out.append(bad)
            continue
        try:
            rows = _read_rows(ctx, entry)
        except Exception as e:
            out.append(Violation("row-read-failed", f"could not read rows: {e}", entry.path))
            continue
        recomputed = len(rows)
        total += recomputed
        declared = entry.count.get("value") if entry.count else None
        if isinstance(declared, int) and not isinstance(declared, bool) and declared != recomputed:
            out.append(
                Violation(
                    "row-count-mismatch",
                    f"recomputed {recomputed} JSONL document(s) but manifest declares "
                    f"count.value={declared}. Empty, truncated, or newline-padded files "
                    f"fail here (§0.4)",
                    entry.path,
                )
            )
        if recomputed == 0:
            out.append(
                Violation(
                    "empty-shard",
                    "text-corpus shard parses to zero documents — refuse empty payload objects",
                    entry.path,
                )
            )
    if total == 0 and not out:
        out.append(
            Violation(
                "empty-group",
                "text-corpus group has zero documents across all shards",
            )
        )
    return out


def check_text_field_wellformed(ctx: GroupContext) -> list[Violation]:
    """Recompute over rows that every document has a non-empty string at the text field.

    This is the byte-level counterpart of ``record_schema``: a schema that claims ``text``
    while every row omits it (or sets it to ``\"\"`` / null) is exactly the plausible
    garbage schema-only validation would accept.
    """
    text_field = _text_field(ctx)
    min_chars = int(_cfg(ctx, "min_text_chars", _DEFAULT_MIN_TEXT_CHARS))
    cap = _cfg(ctx, "rows_sample", None)
    out: list[Violation] = []
    seen = 0
    for entry, bad in _entries(ctx):
        if bad is not None:
            out.append(bad)
            continue
        try:
            rows = _read_rows(ctx, entry)
        except Exception as e:
            out.append(Violation("row-read-failed", f"could not read rows: {e}", entry.path))
            continue
        for idx, row in enumerate(rows):
            val = row.get(text_field, _MISSING)
            if val is _MISSING:
                out.append(
                    Violation(
                        "missing-text-field",
                        f"row {idx}: document has no {text_field!r} field",
                        entry.path,
                    )
                )
            elif not isinstance(val, str):
                out.append(
                    Violation(
                        "text-field-type",
                        f"row {idx}: {text_field!r} must be a string, got {type(val).__name__}",
                        entry.path,
                    )
                )
            elif len(val.strip()) < min_chars:
                out.append(
                    Violation(
                        "text-field-empty",
                        f"row {idx}: {text_field!r} is empty/whitespace "
                        f"(need >= {min_chars} non-whitespace chars)",
                        entry.path,
                    )
                )
            seen += 1
            if isinstance(cap, int) and not isinstance(cap, bool) and seen >= cap:
                return out
    return out


_MISSING = object()


def check_text_not_all_identical(ctx: GroupContext) -> list[Violation]:
    """Refuse when (almost) every sampled document text is byte-identical.

    Catches a stuck writer that repeats one document across the shard. Off by default
    (``max_identical_fraction`` defaults to 1.0 = disabled); set e.g. 0.99 to enable.
    """
    max_frac = _cfg(ctx, "max_identical_fraction", 1.0)
    try:
        max_frac_f = float(max_frac)
    except (TypeError, ValueError):
        return []
    if max_frac_f >= 1.0:
        return []  # disabled

    text_field = _text_field(ctx)
    texts: list[str] = []
    out: list[Violation] = []
    for entry, bad in _entries(ctx):
        if bad is not None:
            out.append(bad)
            continue
        try:
            rows = _read_rows(ctx, entry)
        except Exception as e:
            out.append(Violation("row-read-failed", f"could not read rows: {e}", entry.path))
            continue
        for row in rows:
            val = row.get(text_field)
            if isinstance(val, str):
                texts.append(val)
    n = len(texts)
    if n < 2:
        return out
    # Dominant text frequency.
    counts: dict[str, int] = {}
    for t in texts:
        counts[t] = counts.get(t, 0) + 1
    top = max(counts.values())
    frac = top / n
    if frac > max_frac_f:
        out.append(
            Violation(
                "text-all-identical",
                f"{top}/{n} documents ({frac:.3f}) share identical {text_field!r} text, "
                f"over max_identical_fraction={max_frac_f:.3f} — signature of a stuck "
                f"writer dumping one document",
            )
        )
    return out


CHECKS = [
    check_record_schema_names_text,
    check_entries_declare_row_counts,
    check_row_count_recomputed,
    check_text_field_wellformed,
    check_text_not_all_identical,
]


# --------------------------------------------------------------------------------------
# self-registration
# --------------------------------------------------------------------------------------
import sys  # noqa: E402

try:
    from . import registry as _registry

    _registry.register(sys.modules[__name__])
except Exception:
    pass
