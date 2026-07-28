"""Profile ``token-order/v1`` — index vectors: curriculum orderings, views (§4).

An order object is a ``uint32`` index vector that reorders a parent token pool. The failure
this profile prevents is a **degenerate curriculum**: an order of all zeros is not a
reordering, it is 2M copies of block 0, and it trains to nothing while passing every
integrity check (correct size, valid checksum). Listing cannot tell it from a real
permutation.

The checks recompute against the bytes (§0.4):

* read the ``uint32`` order vector and prove it is what the group claims — a permutation
  (``np.bincount == 1`` everywhere), a subset (unique, in range), or a repeating order (in
  range) — with ``max(index) < parent.block_count``;
* ``len(order) * 4 == bytes`` for every order object.

Reading a whole order vector is acceptable (§ the real case is ~8 MB), but a hard cap
guards against an implausibly large object: over the cap the check returns a Violation
rather than attempting the read and risking OOM.
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from ..manifest import ManifestEntry
from .base import GroupContext, Violation

NAME = "token-order/v1"

REQUIRED_FIELDS: Mapping[str, Any] = {
    # The parent this order reorders, pinned by content (§7 "derived datasets"). A list so a
    # view can order more than one parent group; block_count may live here or on the group.
    "depends_on": {"type": "array", "min_items": 1},
    # Optional:
    #   ordering (default "permutation"): one of permutation | subset | repeating
    #   block_count (int): the parent's block count, if not carried on depends_on[]
    #   max_order_bytes (int): OOM guard, default 512 MiB
}

_ORDER_DTYPE = np.dtype("<u4")  # uint32 little-endian; the .u32 / .u32le.bin index form.
_DEFAULT_MAX_ORDER_BYTES = 512 * 1024 * 1024
_VALID_ORDERINGS = frozenset({"permutation", "subset", "repeating"})


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


def _block_count(ctx: GroupContext) -> int | None:
    """Parent block count: prefer a value on the group, else the first ``depends_on`` entry
    that carries one (§7 pins the parent's ``block_count`` in ``depends_on[]``)."""
    bc = ctx.group.get("block_count")
    if isinstance(bc, int) and not isinstance(bc, bool) and bc >= 0:
        return bc
    for dep in ctx.group.get("depends_on", []) or []:
        if isinstance(dep, Mapping):
            v = dep.get("block_count")
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                return v
    return None


def _entries(ctx: GroupContext):
    for raw in ctx.manifest.get("entries", []):
        try:
            yield ManifestEntry.from_dict(raw), None
        except Exception as e:
            path = raw.get("path") if isinstance(raw, Mapping) else None
            yield None, Violation("bad-manifest-entry", f"unparseable manifest entry: {e}", path)


# --------------------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------------------


def check_order_length_arithmetic(ctx: GroupContext) -> list[Violation]:
    """``len(order) * 4 == bytes`` for every order object.

    Order vectors are ``uint32``, so the object size must be a whole multiple of 4 and equal
    the declared index count times 4. Catches a truncated order or a wrong dtype before any
    value-domain claim is made. Uses the actual object size from HEAD.
    """
    out: list[Violation] = []
    for entry, bad in _entries(ctx):
        if bad is not None:
            out.append(bad)
            continue
        key = _object_key(ctx.prefix, entry.path)
        size = int(ctx.s3.head(ctx.landing_bucket, key)["size"])
        if size % _ORDER_DTYPE.itemsize != 0:
            out.append(
                Violation(
                    "order-bytes-not-multiple",
                    f"object is {size} bytes, not a multiple of 4 — an order vector is "
                    f"uint32, so this cannot be a whole index array",
                    entry.path,
                )
            )
            continue
        n_indices = size // _ORDER_DTYPE.itemsize
        count = entry.count
        if count and count.get("unit") == "indices":
            declared = int(count["value"])
            if declared != n_indices:
                out.append(
                    Violation(
                        "order-length-mismatch",
                        f"declared {declared} indices but object holds "
                        f"{n_indices} (= {size} bytes / 4); either the count is wrong or the "
                        f"object is truncated",
                        entry.path,
                    )
                )
    return out


def check_order_domain(ctx: GroupContext) -> list[Violation]:
    """Recompute that each order vector is what the group claims.

    * ``permutation`` (default): ``np.bincount(order, minlength=n) == 1`` everywhere — every
      parent block appears exactly once. This is the degenerate-curriculum check: an
      all-zeros order makes ``bincount[0] == n`` and every other count 0.
    * ``subset``: indices are unique and in range (a proper sub-selection).
    * ``repeating``: indices need only be in range (a view may repeat blocks by design; its
      partition ``coverage`` is ``overlapping``, §7).

    All variants require ``max(index) < parent.block_count``. Reads the whole vector (the
    real case is ~8 MB) but refuses over ``max_order_bytes`` rather than risking OOM.
    """
    ordering = _cfg(ctx, "ordering", "permutation")
    if ordering not in _VALID_ORDERINGS:
        return [
            Violation(
                "bad-ordering-kind",
                f"group declares ordering={ordering!r}; expected one of "
                f"{sorted(_VALID_ORDERINGS)}",
            )
        ]
    block_count = _block_count(ctx)
    max_bytes = int(_cfg(ctx, "max_order_bytes", _DEFAULT_MAX_ORDER_BYTES))

    out: list[Violation] = []
    for entry, bad in _entries(ctx):
        if bad is not None:
            out.append(bad)
            continue
        key = _object_key(ctx.prefix, entry.path)
        size = int(ctx.s3.head(ctx.landing_bucket, key)["size"])
        if size > max_bytes:
            out.append(
                Violation(
                    "order-too-large",
                    f"order object is {size} bytes, over the {max_bytes}-byte cap; refusing "
                    f"to load it whole rather than risk OOM. Raise max_order_bytes if this is "
                    f"genuinely intended",
                    entry.path,
                )
            )
            continue
        if size % _ORDER_DTYPE.itemsize != 0:
            # Already reported by the arithmetic check; skip domain analysis on ragged bytes.
            continue
        order = np.frombuffer(ctx.s3.get(ctx.landing_bucket, key), dtype=_ORDER_DTYPE)
        if order.size == 0:
            out.append(Violation("order-empty", "order vector is empty", entry.path))
            continue

        max_idx = int(order.max())
        if block_count is not None and max_idx >= block_count:
            out.append(
                Violation(
                    "order-index-out-of-range",
                    f"max index {max_idx} >= parent block_count {block_count}; the order "
                    f"references a block the parent does not have",
                    entry.path,
                )
            )

        if ordering == "permutation":
            n = block_count if block_count is not None else int(order.size)
            if block_count is not None and int(order.size) != block_count:
                out.append(
                    Violation(
                        "permutation-wrong-length",
                        f"a permutation of {block_count} blocks must have {block_count} "
                        f"indices; got {int(order.size)}",
                        entry.path,
                    )
                )
            if max_idx < n:
                counts = np.bincount(order, minlength=n)
                if not np.all(counts == 1):
                    n_missing = int(np.count_nonzero(counts == 0))
                    n_dup = int(np.count_nonzero(counts > 1))
                    worst = int(counts.max())
                    out.append(
                        Violation(
                            "permutation-not-bijective",
                            f"not a permutation: {n_missing} blocks never appear and "
                            f"{n_dup} appear more than once (one block appears {worst} "
                            f"times). An all-zeros order is 2M copies of block 0 — trains to "
                            f"nothing (§7 degenerate-curriculum check)",
                            entry.path,
                        )
                    )
        elif ordering == "subset":
            if int(np.unique(order).size) != int(order.size):
                n_dup = int(order.size - np.unique(order).size)
                out.append(
                    Violation(
                        "subset-not-unique",
                        f"a subset ordering must have unique indices; {n_dup} are repeated",
                        entry.path,
                    )
                )
        # 'repeating': range check above is the whole contract.
    return out


CHECKS = [
    check_order_length_arithmetic,
    check_order_domain,
]


import sys  # noqa: E402

try:
    from . import registry as _registry

    _registry.register(sys.modules[__name__])
except Exception:
    pass
