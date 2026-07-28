"""Profile ``sft-conversations/v1`` — instruction / conversation data (§4).

Two failures this profile prevents:

* a malformed conversation record — a row whose ``messages`` is missing, not a list, or
  carries entries without a ``role``/``content`` — which a training loader would silently
  drop or crash on;
* **train/heldout leakage** — the same conversation present in both the training split and
  the held-out evaluation split, which inflates every heldout metric. §4 requires a
  ``dedup + leakage`` report block; per §0.4 this profile does not *trust* that report, it
  **recomputes** the leakage from the actual rows.

Rows come from ``.jsonl`` / ``.jsonl.gz`` per manifest entry. Partitions (§7) declare which
entries are train vs heldout by path glob (``by: "path"`` is enough for v1).
"""

from __future__ import annotations

import fnmatch
import gzip
import hashlib
import json
from typing import Any, Iterable, Mapping

from ..manifest import ManifestEntry
from .base import GroupContext, Violation

NAME = "sft-conversations/v1"

REQUIRED_FIELDS: Mapping[str, Any] = {
    # The record schema: every row has a messages[] array of {role, content}.
    "record_schema": {"type": "object", "required": ["messages"]},
    # Splits, one of which must be the held-out set (§7 sft requires a heldout).
    "partitions": {"type": "array", "min_items": 2},
    # The dedup + leakage report block §4 mandates — declared, but recomputed by the check.
    "dedup": {"type": "object"},
    "leakage": {"type": "object"},
    # Optional:
    #   dedup_key: list[str] of message fields to hash (default: concat of contents)
    #   heldout_glob / train_glob: override partition detection
    #   max_leakage (int): allowed intersection size, default 0
    #   messages_sample (int): cap on rows checked for well-formedness, default all
}

_ROLES_HINT = frozenset({"system", "user", "assistant", "tool"})
_DEFAULT_MAX_LEAKAGE = 0


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


def _read_rows(ctx: GroupContext, entry: ManifestEntry) -> list[dict[str, Any]]:
    key = _object_key(ctx.prefix, entry.path)
    body = ctx.s3.get(ctx.landing_bucket, key)
    if entry.path.lower().endswith(".gz"):
        body = gzip.decompress(body)
    rows: list[dict[str, Any]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        rows.append(obj if isinstance(obj, dict) else {"_value": obj})
    return rows


def _partition_globs(ctx: GroupContext) -> tuple[list[str], list[str]]:
    """Return ``(train_globs, heldout_globs)`` from ``partitions[]`` (``by: "path"``).

    A partition whose name contains 'heldout'/'held-out'/'holdout'/'test'/'val'/'eval' is
    the held-out side; anything else (typically 'train') is the training side. Explicit
    ``train_glob`` / ``heldout_glob`` on the group override the auto-detection.
    """
    train_glob = _cfg(ctx, "train_glob", None)
    heldout_glob = _cfg(ctx, "heldout_glob", None)
    if train_glob or heldout_glob:
        return ([train_glob] if train_glob else []), ([heldout_glob] if heldout_glob else [])

    train: list[str] = []
    held: list[str] = []
    for part in ctx.group.get("partitions", []) or []:
        if not isinstance(part, Mapping) or part.get("by") != "path":
            continue
        glob = part.get("glob")
        if not glob:
            continue
        name = str(part.get("name", "")).lower()
        if any(tok in name for tok in ("heldout", "held-out", "holdout", "test", "val", "eval")):
            held.append(glob)
        else:
            train.append(glob)
    return train, held


def _matches_any(path: str, globs: Iterable[str]) -> bool:
    base = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(base, g) for g in globs)


def _dedup_key(row: Mapping[str, Any], fields: list[str] | None) -> str:
    """Stable hash of a row's identity. Default is the concatenation of every message's
    ``content`` (the conversation text) — two rows with the same conversation collide even
    if surrounding metadata differs, which is what makes the leakage check meaningful."""
    messages = row.get("messages")
    if fields:
        parts = [str(row.get(f, "")) for f in fields]
    elif isinstance(messages, list):
        parts = []
        for m in messages:
            if isinstance(m, Mapping):
                parts.append(f"{m.get('role', '')}\x1f{m.get('content', '')}")
    else:
        parts = [json.dumps(row, sort_keys=True, ensure_ascii=False)]
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()


def _messages_wellformed(messages: Any) -> str | None:
    """Return a human reason string if ``messages`` is malformed, else ``None``."""
    if not isinstance(messages, list):
        return f"'messages' must be a list, got {type(messages).__name__}"
    if len(messages) == 0:
        return "'messages' is empty"
    for i, m in enumerate(messages):
        if not isinstance(m, Mapping):
            return f"messages[{i}] is not an object"
        if "role" not in m or not isinstance(m["role"], str) or not m["role"]:
            return f"messages[{i}] has no non-empty 'role'"
        if "content" not in m:
            return f"messages[{i}] has no 'content'"
    return None


# --------------------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------------------


def check_messages_wellformed(ctx: GroupContext) -> list[Violation]:
    """Recompute over a sample of rows that every record has a well-formed ``messages``
    array (a list of ``{role, content}``). A malformed record is a real defect a loader
    would drop or die on."""
    cap = _cfg(ctx, "messages_sample", None)
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
            reason = _messages_wellformed(row.get("messages"))
            if reason is not None:
                out.append(
                    Violation(
                        "malformed-messages",
                        f"row {idx}: {reason}. Every sft record must be a list of "
                        f"{{role, content}} messages (§4)",
                        entry.path,
                    )
                )
            seen += 1
            if isinstance(cap, int) and not isinstance(cap, bool) and seen >= cap:
                return out
    return out


def check_train_heldout_leakage(ctx: GroupContext) -> list[Violation]:
    """Recompute train/heldout leakage: hash a dedup key per row, and assert the
    intersection of the train key-set and the heldout key-set is empty (or <= a declared
    ``max_leakage``).

    This is the real leakage check §4 asks for — it does **not** trust the declared
    ``leakage`` report. v1 reads the rows fully; if a group were huge this would be sampled
    and noted, but the sft sets in scope are small enough to hash whole.
    """
    train_globs, held_globs = _partition_globs(ctx)
    if not train_globs or not held_globs:
        return [
            Violation(
                "missing-partitions",
                "sft-conversations requires both a train and a heldout partition declared "
                "by path glob (§7); could not resolve both from partitions[]",
            )
        ]

    fields = _cfg(ctx, "dedup_key", None)
    if fields is not None and not isinstance(fields, list):
        fields = [fields]
    max_leakage = int(_cfg(ctx, "max_leakage", _DEFAULT_MAX_LEAKAGE))

    train_keys: set[str] = set()
    held_keys: set[str] = set()
    out: list[Violation] = []

    for entry, bad in _entries(ctx):
        if bad is not None:
            out.append(bad)
            continue
        is_train = _matches_any(entry.path, train_globs)
        is_held = _matches_any(entry.path, held_globs)
        if not (is_train or is_held):
            continue  # an entry in neither split (e.g. a report sidecar) is not leakage-relevant.
        try:
            rows = _read_rows(ctx, entry)
        except Exception as e:
            out.append(Violation("row-read-failed", f"could not read rows: {e}", entry.path))
            continue
        target = train_keys if is_train else held_keys
        for row in rows:
            target.add(_dedup_key(row, fields))

    overlap = train_keys & held_keys
    if len(overlap) > max_leakage:
        out.append(
            Violation(
                "train-heldout-leakage",
                f"{len(overlap)} conversation key(s) appear in BOTH train and heldout "
                f"(allowed <= {max_leakage}). Recomputed from row contents — the declared "
                f"leakage report was not trusted (§0.4). Heldout metrics on these rows are "
                f"memorized, not generalized",
            )
        )
    return out


CHECKS = [
    check_messages_wellformed,
    check_train_heldout_leakage,
]


import sys  # noqa: E402

try:
    from . import registry as _registry

    _registry.register(sys.modules[__name__])
except Exception:
    pass
