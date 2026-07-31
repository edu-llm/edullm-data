"""Profile ``text-corpus/v1`` — raw untokenized documents (§4).

Companion to ``pretrain-tokens/v1``: the same corpus can ship a ``text/`` group of
document JSONL next to a ``tokens/`` group of packed shards. The failure this profile
prevents is publishing "text" that is empty, not JSONL, or missing the declared text
field — which would make a detokenize/compare or raw-document consumer silently wrong.

Rows come from ``.jsonl`` / ``.jsonl.gz`` per manifest entry (Dolma-style). Every
document object must carry a non-empty string at the group's text field (default
``text``). Row counts are **recomputed by streaming-parsing the payload** with the same
helper ``publish()`` uses (:mod:`edullm_data.jsonl`), never trusted from the manifest
alone (§0.4). Each shard is fetched/parsed **once** for count + text + optional
identical-text checks.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .. import jsonl as jsonl_mod
from ..manifest import ManifestEntry
from .base import GroupContext, Violation

NAME = "text-corpus/v1"

REQUIRED_FIELDS: Mapping[str, Any] = {
    # Declares the document shape. Must name the text field (default key ``text``).
    # Presence is declarative; VALUES are enforced by CHECKS reading the bytes.
    "record_schema": {"type": "object"},
    # Optional (validated explicitly — invalid values are violations, not silent defaults):
    #   text_field (nonempty str, default "text")
    #   min_text_chars (int >= 1, default 1)
    #   max_identical_fraction (finite float in [0, 1], default 1.0 = disabled)
}

_DEFAULT_TEXT_FIELD = "text"
_DEFAULT_MIN_TEXT_CHARS = 1
_DEFAULT_MAX_IDENTICAL = 1.0
_MISSING = object()


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _object_key(prefix: str, path: str) -> str:
    if not prefix:
        return path
    return prefix.rstrip("/") + "/" + path.lstrip("/")


def _cfg_raw(ctx: GroupContext, key: str, default: Any) -> Any:
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


def _schema_names_text(schema: Mapping[str, Any], text_field: str) -> bool:
    if text_field in schema:
        return True
    required = schema.get("required")
    if isinstance(required, list) and text_field in required:
        return True
    props = schema.get("properties")
    if isinstance(props, Mapping) and text_field in props:
        return True
    return False


def _resolve_settings(ctx: GroupContext) -> tuple[str, int, float] | list[Violation]:
    """Return ``(text_field, min_text_chars, max_identical_fraction)`` or violations."""
    out: list[Violation] = []

    text_field = _cfg_raw(ctx, "text_field", _DEFAULT_TEXT_FIELD)
    if not isinstance(text_field, str) or not text_field.strip():
        out.append(
            Violation(
                "invalid-text-field",
                f"text_field must be a nonempty string; got {text_field!r}",
            )
        )
        text_field = _DEFAULT_TEXT_FIELD  # unused if we return violations

    min_chars = _cfg_raw(ctx, "min_text_chars", _DEFAULT_MIN_TEXT_CHARS)
    if not isinstance(min_chars, int) or isinstance(min_chars, bool) or min_chars < 1:
        out.append(
            Violation(
                "invalid-min-text-chars",
                f"min_text_chars must be an integer >= 1; got {min_chars!r}",
            )
        )

    max_frac = _cfg_raw(ctx, "max_identical_fraction", _DEFAULT_MAX_IDENTICAL)
    try:
        max_frac_f = float(max_frac)
    except (TypeError, ValueError):
        out.append(
            Violation(
                "invalid-max-identical-fraction",
                f"max_identical_fraction must be a finite float in [0, 1]; got {max_frac!r}",
            )
        )
        max_frac_f = _DEFAULT_MAX_IDENTICAL
    else:
        if not math.isfinite(max_frac_f) or max_frac_f < 0.0 or max_frac_f > 1.0:
            out.append(
                Violation(
                    "invalid-max-identical-fraction",
                    f"max_identical_fraction must be a finite float in [0, 1]; got {max_frac!r}",
                )
            )

    if out:
        return out
    assert isinstance(text_field, str)
    assert isinstance(min_chars, int)
    return text_field, min_chars, max_frac_f


# --------------------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------------------


def check_group_config(ctx: GroupContext) -> list[Violation]:
    """Validate record_schema + tunable settings before any payload bytes are read."""
    out: list[Violation] = []
    schema = ctx.group.get("record_schema")
    if not isinstance(schema, Mapping) or not schema:
        out.append(
            Violation(
                "missing-record-schema",
                "text-corpus/v1 requires group.record_schema naming the document text "
                "field (default key 'text', or override via text_field)",
            )
        )
        return out

    settings = _resolve_settings(ctx)
    if isinstance(settings, list):
        return settings

    text_field, _min_chars, _max_frac = settings
    if not _schema_names_text(schema, text_field):
        out.append(
            Violation(
                "record-schema-missing-text",
                f"record_schema does not name text field {text_field!r}; declare it as a "
                f"key, under properties, or in required[] so consumers know which field "
                f"holds document text",
            )
        )
    return out


def check_entries_declare_row_counts(ctx: GroupContext) -> list[Violation]:
    """Every shard must declare ``count{unit: "rows", value}``."""
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


def check_documents(ctx: GroupContext) -> list[Violation]:
    """Single streaming pass per shard: row count, text field, optional identical-text.

    Each shard is opened once via :func:`edullm_data.jsonl.iter_jsonl_objects_s3`. The
    identical-text heuristic is **per shard** (a 100% repeated shard is not diluted by
    healthy siblings). ``max_identical_fraction`` of 1.0 disables that heuristic.
    """
    settings = _resolve_settings(ctx)
    if isinstance(settings, list):
        return []  # check_group_config already reported

    text_field, min_chars, max_frac = settings
    out: list[Violation] = []
    total = 0

    for entry, bad in _entries(ctx):
        if bad is not None:
            out.append(bad)
            continue
        is_jsonl, gzipped = jsonl_mod.is_jsonl_path(entry.path)
        if not is_jsonl:
            out.append(
                Violation(
                    "row-read-failed",
                    f"text-corpus payload {entry.path!r} is not jsonl(.gz)",
                    entry.path,
                )
            )
            continue

        key = _object_key(ctx.prefix, entry.path)
        recomputed = 0
        identical_counts: dict[str, int] = {}
        try:
            for idx, row in enumerate(
                jsonl_mod.iter_jsonl_objects_s3(
                    ctx.s3, ctx.landing_bucket, key, gzipped=gzipped
                )
            ):
                recomputed += 1
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
                            f"row {idx}: {text_field!r} must be a string, "
                            f"got {type(val).__name__}",
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
                else:
                    if max_frac < 1.0:
                        identical_counts[val] = identical_counts.get(val, 0) + 1
        except Exception as e:
            out.append(Violation("row-read-failed", f"could not read rows: {e}", entry.path))
            continue

        total += recomputed
        declared = entry.count.get("value") if entry.count else None
        if isinstance(declared, int) and not isinstance(declared, bool) and declared != recomputed:
            out.append(
                Violation(
                    "row-count-mismatch",
                    f"recomputed {recomputed} JSONL document(s) but manifest declares "
                    f"count.value={declared}. Publisher and validator share "
                    f"edullm_data.jsonl counting (§0.4)",
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
        elif max_frac < 1.0 and recomputed >= 2 and identical_counts:
            top = max(identical_counts.values())
            frac = top / recomputed
            if frac > max_frac:
                out.append(
                    Violation(
                        "text-all-identical",
                        f"{top}/{recomputed} documents ({frac:.3f}) in this shard share "
                        f"identical {text_field!r} text, over max_identical_fraction="
                        f"{max_frac:.3f} — signature of a stuck writer dumping one document",
                        entry.path,
                    )
                )

    if total == 0 and not any(v.code == "empty-shard" for v in out):
        # No successful parses and no empty-shard yet (e.g. all read failures).
        if not out:
            out.append(
                Violation(
                    "empty-group",
                    "text-corpus group has zero documents across all shards",
                )
            )
    return out


CHECKS = [
    check_group_config,
    check_entries_declare_row_counts,
    check_documents,
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
