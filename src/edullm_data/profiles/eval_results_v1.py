"""Profile ``eval-results/v1`` — model outputs / scores (§4, §7).

Names the failure it prevents, per §4: the audit found **12 header-only CSVs** (66 bytes
each, zero data rows) and **3 byte-identical 713 KB files** where every row's finish reason
was ``"error"``. Listing could not tell either apart from real results. Two checks kill
both, and they are not redundant:

* ``n_rows == n_ok + n_error + n_filtered``, with ``n_rows`` **recomputed by counting the
  payload rows** (never trusting a declared count) — this is what catches the header-only
  CSVs, whose real row count is 0.
* **refuse if ``n_ok == 0``** — the three all-error files had honest, nonzero ``n_rows``, so
  the row-count identity passed them; only the all-error refusal catches them.

A third check refuses a degenerate eval whose per-row metric is a single constant.

Rows are read from ``.jsonl`` / ``.jsonl.gz`` (and ``.csv`` / ``.csv.gz``) per manifest
entry. These files are small (§0.2 — the eval case is the high-volume small case), so a
whole read via ``s3.get`` is the right tool.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
from typing import Any, Mapping

from ..manifest import ManifestEntry
from .base import GroupContext, Violation

NAME = "eval-results/v1"

REQUIRED_FIELDS: Mapping[str, Any] = {
    "model": {"type": "object", "required": ["id", "revision"]},
    "task": {"type": "string"},
    "decode": {"type": "object", "required": ["temperature", "top_p", "max_tokens"]},
    "status_counts": {"type": "object"},
    # Optional, profile-tunable (with defaults):
    #   status_field (default "status"), status_ok_value ("ok"),
    #   status_error_value ("error"), status_filtered_value ("filtered"),
    #   metric_field (default None; if given, the all-identical check runs).
}

_DEFAULT_STATUS_FIELD = "status"
_DEFAULT_OK = "ok"
_DEFAULT_ERROR = "error"
_DEFAULT_FILTERED = "filtered"


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
    """Parse a payload object into a list of dict rows.

    Supports jsonl / jsonl.gz (one JSON object per non-blank line) and csv / csv.gz
    (``DictReader``, so the header row is not counted as data — which is exactly why a
    header-only CSV recomputes to zero rows). Raises ``ValueError`` for a container we do
    not know how to count, so the caller surfaces it rather than silently passing.
    """
    key = _object_key(ctx.prefix, entry.path)
    raw = _decompress(entry.path, ctx.s3.get(ctx.landing_bucket, key))
    lower = entry.path.lower()
    if lower.endswith(".jsonl") or lower.endswith(".jsonl.gz"):
        rows: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            rows.append(obj if isinstance(obj, dict) else {"_value": obj})
        return rows
    if lower.endswith(".csv") or lower.endswith(".csv.gz"):
        text = raw.decode("utf-8")
        return list(csv.DictReader(io.StringIO(text)))
    raise ValueError(
        f"eval-results payload {entry.path!r} is neither jsonl(.gz) nor csv(.gz); "
        f"cannot recompute a row count"
    )


def _status_of(row: Mapping[str, Any], field: str) -> Any:
    return row.get(field)


# --------------------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------------------


def check_row_count_accounting(ctx: GroupContext) -> list[Violation]:
    """Recompute ``n_rows`` by counting payload rows and assert it equals
    ``n_ok + n_error + n_filtered`` from the *declared* ``status_counts``.

    The header-only CSV dies here: its declared ``status_counts`` sum to some positive
    number, but the recomputed row count is 0. Do not trust a declared ``n_rows`` — §0.4:
    validators recompute, they do not read.
    """
    out: list[Violation] = []
    sc = ctx.group.get("status_counts")
    if not isinstance(sc, Mapping):
        return [Violation("missing-status-counts", "group declares no status_counts object (§4)")]

    ok_key = _cfg(ctx, "status_ok_value", _DEFAULT_OK)
    err_key = _cfg(ctx, "status_error_value", _DEFAULT_ERROR)
    filt_key = _cfg(ctx, "status_filtered_value", _DEFAULT_FILTERED)
    n_ok = int(sc.get(ok_key, 0) or 0)
    n_err = int(sc.get(err_key, 0) or 0)
    n_filtered = int(sc.get(filt_key, 0) or 0)
    declared_total = n_ok + n_err + n_filtered

    recomputed = 0
    for entry, bad in _entries(ctx):
        if bad is not None:
            out.append(bad)
            continue
        try:
            recomputed += len(_read_rows(ctx, entry))
        except Exception as e:
            out.append(Violation("row-read-failed", f"could not read rows: {e}", entry.path))

    if declared_total != recomputed:
        out.append(
            Violation(
                "row-count-mismatch",
                f"recomputed {recomputed} data rows across the group, but declared "
                f"status_counts sum to {declared_total} "
                f"(ok={n_ok}, error={n_err}, filtered={n_filtered}). A header-only file "
                f"recomputes to 0 rows while declaring a nonzero count (§7)",
            )
        )
    return out


def check_refuse_all_error(ctx: GroupContext) -> list[Violation]:
    """Refuse the whole group when it contains **zero** successful rows.

    This is the check that catches the three byte-identical all-error files: they had
    honest, nonzero ``n_rows`` (so row-count accounting passed), but not one row succeeded.
    A results file with no successes is not a result. Recomputed from the rows, not read
    from ``status_counts`` — a writer that mislabels every error as ``ok`` still has to
    produce a row whose status field equals ``status_ok_value``, and it won't.
    """
    ok_value = _cfg(ctx, "status_ok_value", _DEFAULT_OK)
    status_field = _cfg(ctx, "status_field", _DEFAULT_STATUS_FIELD)

    out: list[Violation] = []
    total_rows = 0
    total_ok = 0
    for entry, bad in _entries(ctx):
        if bad is not None:
            out.append(bad)
            continue
        try:
            rows = _read_rows(ctx, entry)
        except Exception as e:
            out.append(Violation("row-read-failed", f"could not read rows: {e}", entry.path))
            continue
        total_rows += len(rows)
        total_ok += sum(1 for r in rows if _status_of(r, status_field) == ok_value)

    if total_rows > 0 and total_ok == 0:
        out.append(
            Violation(
                "all-error-no-success",
                f"all {total_rows} rows are non-'{ok_value}' in field '{status_field}' — "
                f"zero successes. An all-error results file has honest row counts, so only "
                f"this refusal catches it (§7); it is not a usable result",
            )
        )
    return out


def check_metric_not_constant(ctx: GroupContext) -> list[Violation]:
    """If rows carry a metric/score field, refuse when every value is a single constant.

    A degenerate eval — a model that emits the same answer for every item, or a scorer stuck
    at one value — produces a metric column with a single distinct value. Only runs when the
    group declares a ``metric_field`` (adding a required field no check reads would be
    decoration, §4). Recomputes the distinct set from the rows.
    """
    metric_field = _cfg(ctx, "metric_field", None)
    if not metric_field:
        return []  # no metric declared — nothing to recompute.

    values: list[Any] = []
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
        for r in rows:
            if metric_field in r and r[metric_field] is not None:
                values.append(r[metric_field])

    distinct = {repr(v) for v in values}
    if len(values) >= 2 and len(distinct) == 1:
        out.append(
            Violation(
                "metric-all-identical",
                f"every one of {len(values)} '{metric_field}' values is identical "
                f"({values[0]!r}); a constant metric is a degenerate eval (§7)",
            )
        )
    return out


CHECKS = [
    check_row_count_accounting,
    check_refuse_all_error,
    check_metric_not_constant,
]


import sys  # noqa: E402

try:
    from . import registry as _registry

    _registry.register(sys.modules[__name__])
except Exception:
    pass
