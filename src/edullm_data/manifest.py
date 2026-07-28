"""Manifest construction and the checks that make a manifest a *checkable* claim.

Implements DATASET-STANDARD.md §5 (manifests, per-file format, the honesty rule,
shard naming without ``-of-N``, exhaustiveness) and the arithmetic identity from §7's
Gate A.

Two failures from §0's table are the reason this module exists:

* ``.npy`` extension on 7,557 headerless raw-uint32 objects — an extension that
  contradicts the bytes. :func:`check_extension_matches_format` refuses it.
* a count that does not match the object size —
  ``86,096,509 x 4 = 344,386,036`` is the identity that exposed it.
  :func:`verify_arithmetic` recomputes it.

Every function here is pure: it takes metadata (and, for the caller's own diffing,
path sets) and returns a list of human-readable violation strings. No AWS, no I/O.
An empty list means "nothing detected"; violations are strings rather than
exceptions because §7 wants *all* failing assertions written into
``_REJECTED.json``, not just the first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .contracts import SCHEMA_VERSION, canonical_json, sha256_bytes

__all__ = [
    "DTYPE_SIZES",
    "COUNT_UNITS",
    "FIXED_WIDTH_UNITS",
    "FIXED_WIDTH_CONTAINERS",
    "EXTENSION_FORMAT",
    "SHARD_RE",
    "CAS_RE",
    "Format",
    "ManifestEntry",
    "check_extension_matches_format",
    "build_manifest",
    "manifest_sha256",
    "verify_arithmetic",
    "diff_paths",
    "parse_shard_name",
    "is_cas_name",
    "check_shard_naming",
]

import re

# --------------------------------------------------------------------------------------
# dtypes and count units
# --------------------------------------------------------------------------------------

#: Width in bytes of every dtype the standard's arithmetic identity can be applied to.
#: A dtype outside this map is not "invalid" — it is simply not fixed-width-checkable,
#: so :func:`verify_arithmetic` declines to assert rather than guessing.
DTYPE_SIZES: dict[str, int] = {
    "uint8": 1,
    "uint16": 2,
    "uint32": 4,
    "int32": 4,
    "float32": 4,
    "float16": 2,
    "int64": 8,
    "float64": 8,
}

#: §5: ``count{unit, value}`` with ``unit in {rows, tokens, items, indices, bytes}``.
COUNT_UNITS = frozenset({"rows", "tokens", "items", "indices", "bytes"})

#: The units whose value has a fixed byte width per element, so
#: ``value x dtype_size`` is a meaningful prediction of the object size.
#: ``rows``/``items`` are variable-width; ``bytes`` is not a multiple of a dtype.
FIXED_WIDTH_UNITS = frozenset({"tokens", "indices"})

_BYTE_ORDERS = frozenset({"little", "big", "n/a"})

#: §7 Gate A scopes the arithmetic identity to "fixed-width containers". A raw,
#: uncompressed container is the only one where ``bytes`` is a plain function of the
#: element count: parquet has a footer, and any codec makes ``bytes`` the *encoded*
#: size. See :func:`verify_arithmetic`.
FIXED_WIDTH_CONTAINERS = frozenset({"raw"})


# --------------------------------------------------------------------------------------
# §5 — per-file format
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Format:
    """§5's per-file ``format`` block.

    ``{"container": "raw", "dtype": "uint32", "byte_order": "little",
       "header_bytes": 0, "codec": "none"}``

    ``dtype`` and ``byte_order`` are ``None`` for containers that carry their own
    per-column typing (parquet, csv, jsonl). ``header_bytes`` is the count of leading
    bytes that are *not* payload — 0 for headerless raw, nonzero for a real ``.npy``.
    §5 is explicit that ``dtype`` "must be declared and read, never inferred", because
    OLMo-core defaults to ``uint16`` while these corpora are ``uint32``.
    """

    container: str
    dtype: str | None = None
    byte_order: str | None = None
    header_bytes: int = 0
    codec: str = "none"

    def __post_init__(self) -> None:
        if not isinstance(self.container, str) or not self.container:
            raise ValueError("format.container must be a non-empty string")
        if self.dtype is not None and not isinstance(self.dtype, str):
            raise ValueError("format.dtype must be a string or null")
        if self.byte_order is not None and self.byte_order not in _BYTE_ORDERS:
            raise ValueError(
                f"format.byte_order must be one of {sorted(_BYTE_ORDERS)} or null; "
                f"got {self.byte_order!r}"
            )
        if isinstance(self.header_bytes, bool) or not isinstance(self.header_bytes, int):
            raise ValueError("format.header_bytes must be an int")
        if self.header_bytes < 0:
            raise ValueError("format.header_bytes must be >= 0")
        if not isinstance(self.codec, str) or not self.codec:
            raise ValueError("format.codec must be a non-empty string ('none' if uncompressed)")

    @classmethod
    def for_tokens(cls, dtype: str = "uint32") -> "Format":
        """The headerless-raw form §5 mandates for packed token shards.

        ``container="raw"``, ``byte_order="little"``, ``header_bytes=0``,
        ``codec="none"`` — the shape ``np.memmap(path, mode="r", dtype=dtype)``
        actually reads, and the reason the extension is ``.u32le.bin`` and never
        ``.npy``.
        """
        if dtype not in DTYPE_SIZES:
            raise ValueError(
                f"token dtype {dtype!r} is not fixed-width-checkable; "
                f"expected one of {sorted(DTYPE_SIZES)}"
            )
        return cls(
            container="raw",
            dtype=dtype,
            byte_order="little",
            header_bytes=0,
            codec="none",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "container": self.container,
            "dtype": self.dtype,
            "byte_order": self.byte_order,
            "header_bytes": self.header_bytes,
            "codec": self.codec,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Format":
        if not isinstance(d, Mapping):
            raise ValueError(f"format must be an object; got {type(d).__name__}")
        known = {"container", "dtype", "byte_order", "header_bytes", "codec"}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"format has unknown key(s) {sorted(unknown)}")
        if "container" not in d:
            raise ValueError("format is missing required key 'container'")
        return cls(
            container=d["container"],
            dtype=d.get("dtype"),
            byte_order=d.get("byte_order"),
            header_bytes=d.get("header_bytes", 0),
            codec=d.get("codec", "none"),
        )

    @property
    def dtype_size(self) -> int | None:
        """Byte width of one element, or ``None`` if the dtype is not fixed-width."""
        if self.dtype is None:
            return None
        return DTYPE_SIZES.get(self.dtype)


# --------------------------------------------------------------------------------------
# §5 — manifest entries
# --------------------------------------------------------------------------------------

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ManifestEntry:
    """One row of a group manifest (§5).

    ``{"path": ..., "sha256": ..., "bytes": ..., "count": {...}, "format": {...}}``

    ``count`` is omissible (``None``) — §5: "a tar part or a ``.done`` sentinel has no
    honest count." ``bytes`` and ``sha256`` are always required.

    ``sha256`` is a **client assertion** used for content addressing (§7 "Checksum
    reality"); S3 does not verify it for multipart objects. Nothing here should be read
    as claiming otherwise.
    """

    path: str
    sha256: str
    bytes: int
    count: dict[str, Any] | None = None
    format: Format = field(default_factory=lambda: Format(container="raw"))

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("entry.path must be a non-empty string")
        if self.path.startswith("/") or "\\" in self.path:
            raise ValueError(
                f"entry.path {self.path!r} must be a relative, forward-slashed key "
                f"under the group prefix (e.g. 'tokens/train-00000.u32le.bin')"
            )
        if ".." in self.path.split("/"):
            raise ValueError(f"entry.path {self.path!r} must not contain '..'")

        if not isinstance(self.sha256, str) or not _SHA256_HEX_RE.match(self.sha256):
            raise ValueError(
                f"entry.sha256 for {self.path!r} must be 64 lowercase hex chars; "
                f"got {self.sha256!r}"
            )

        if isinstance(self.bytes, bool) or not isinstance(self.bytes, int):
            raise ValueError(f"entry.bytes for {self.path!r} must be an int")
        if self.bytes < 0:
            raise ValueError(f"entry.bytes for {self.path!r} must be >= 0")

        if self.count is not None:
            if not isinstance(self.count, Mapping):
                raise ValueError(f"entry.count for {self.path!r} must be an object or null")
            unknown = set(self.count) - {"unit", "value"}
            if unknown:
                raise ValueError(
                    f"entry.count for {self.path!r} has unknown key(s) {sorted(unknown)}; "
                    f"the block is {{unit, value}}"
                )
            unit = self.count.get("unit")
            if unit not in COUNT_UNITS:
                raise ValueError(
                    f"entry.count.unit for {self.path!r} must be one of "
                    f"{sorted(COUNT_UNITS)}; got {unit!r}"
                )
            value = self.count.get("value")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"entry.count.value for {self.path!r} must be an int >= 0; got {value!r}"
                )

        if not isinstance(self.format, Format):
            raise ValueError(f"entry.format for {self.path!r} must be a Format")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "format": self.format.to_dict(),
        }
        # Omit rather than emit null: §5 calls count "omissible", and an explicit null
        # invites a reader to treat "no honest count" as "count of nothing".
        if self.count is not None:
            d["count"] = {"unit": self.count["unit"], "value": self.count["value"]}
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ManifestEntry":
        if not isinstance(d, Mapping):
            raise ValueError(f"manifest entry must be an object; got {type(d).__name__}")
        unknown = set(d) - {"path", "sha256", "bytes", "count", "format"}
        if unknown:
            raise ValueError(f"manifest entry has unknown key(s) {sorted(unknown)}")
        for required in ("path", "sha256", "bytes", "format"):
            if required not in d:
                raise ValueError(f"manifest entry is missing required key {required!r}")
        count = d.get("count")
        return cls(
            path=d["path"],
            sha256=d["sha256"],
            bytes=d["bytes"],
            count=dict(count) if isinstance(count, Mapping) else count,
            format=Format.from_dict(d["format"]),
        )


# --------------------------------------------------------------------------------------
# §5 — the honesty rule: extension must not contradict format
# --------------------------------------------------------------------------------------


def _raw_ext(code: str, dtype: str) -> dict[str, Any]:
    """Expectations for a self-describing raw extension like ``.u32le.bin``."""
    order = {"le": "little", "be": "big"}.get(code[-2:], None)
    return {
        "container": "raw",
        "dtype": dtype,
        "byte_order": order,
        "header_bytes": 0,
        "codec": "none",
    }


_RAW_DTYPE_CODES = {
    "u16": "uint16",
    "u32": "uint32",
    "i32": "int32",
    "i64": "int64",
    "f16": "float16",
    "f32": "float32",
    "f64": "float64",
}

#: Extension -> the format facts that extension *claims*. A key absent from a value
#: dict means the extension makes no claim about it (parquet does not constrain dtype;
#: gzip-wrapped text does not constrain byte order).
#:
#: Matching is longest-suffix and case-insensitive, so ``.jsonl.gz`` wins over
#: ``.gz`` and ``.u32le.bin`` wins over ``.bin``. A bare ``.bin`` is deliberately
#: *not* in the map: it is not self-describing, so it cannot contradict anything —
#: §5 asks for self-describing extensions, and the profile-level naming rule is where
#: "use a self-describing extension" belongs.
EXTENSION_FORMAT: dict[str, dict[str, Any]] = {
    # --- raw, headerless, self-describing (the token-shard case) ---
    ".u8.bin": {
        "container": "raw",
        "dtype": "uint8",
        "header_bytes": 0,
        "codec": "none",
    },
    **{
        f".{code}{end}.bin": _raw_ext(f"{code}{end}", dtype)
        for code, dtype in _RAW_DTYPE_CODES.items()
        for end in ("le", "be")
    },
    # --- containers that carry their own typing ---
    ".parquet": {"container": "parquet"},
    ".jsonl": {"container": "jsonl", "codec": "none"},
    ".jsonl.gz": {"container": "jsonl", "codec": "gzip"},
    ".csv": {"container": "csv", "codec": "none"},
    ".csv.gz": {"container": "csv", "codec": "gzip"},
    ".json": {"container": "json", "codec": "none"},
    # --- the lie the standard was written to stop ---
    # A real .npy always has a header (magic \x93NUMPY + version + dict), so
    # header_bytes must be > 0 and the container is not "raw". `min_header_bytes`
    # is the only non-equality expectation in the map; see _check_expectations.
    ".npy": {"container": "npy", "min_header_bytes": 1, "codec": "none"},
}

_EXT_KEYS_BY_LENGTH: tuple[str, ...] = tuple(
    sorted(EXTENSION_FORMAT, key=len, reverse=True)
)


def _match_extension(path: str) -> str | None:
    """Longest known extension that ``path`` ends with, or ``None``."""
    lowered = path.lower()
    for ext in _EXT_KEYS_BY_LENGTH:
        if lowered.endswith(ext):
            return ext
    return None


def check_extension_matches_format(path: str, fmt: Format) -> list[str]:
    """§5's honesty rule, as far as metadata alone can prove it.

    Returns a list of violation strings; empty means no contradiction found. The
    validator pairs this with a magic-byte sniff (§7 Gate A) — this function catches
    the claim that is self-contradictory *before* any byte is read, which is the
    cheap half.

    Two cases are called out explicitly because they are the audit's real bug:

    * ``.npy`` with ``header_bytes == 0`` — 7,557 such objects exist. ``np.load()``
      fails on them, and OLMo-core's ``np.memmap`` reader would eat a real header as
      leading tokens. Either the extension is wrong or the header count is.
    * ``.u32le.bin`` with ``header_bytes != 0`` — the mirror image: the extension
      promises headerless raw, so a nonzero header silently shifts every token and
      corrupts the size-derived count.

    An unknown extension yields no violations: an extension that claims nothing
    cannot contradict anything (§0.1 — inventing a claim to check would manufacture
    exactly the unearned confidence the standard is against).
    """
    violations: list[str] = []
    ext = _match_extension(path)
    if ext is None:
        return violations

    expected = EXTENSION_FORMAT[ext]

    for key in ("container", "dtype", "byte_order", "codec"):
        if key not in expected:
            continue
        want = expected[key]
        got = getattr(fmt, key)
        if got != want:
            violations.append(
                f"{path}: extension '{ext}' claims format.{key}={want!r} "
                f"but the manifest declares {got!r}"
            )

    if "header_bytes" in expected and fmt.header_bytes != expected["header_bytes"]:
        violations.append(
            f"{path}: extension '{ext}' claims headerless raw bytes "
            f"(header_bytes={expected['header_bytes']}) but the manifest declares "
            f"header_bytes={fmt.header_bytes}; a nonzero header shifts every element "
            f"and corrupts the size-derived count (§5)"
        )

    if "min_header_bytes" in expected and fmt.header_bytes < expected["min_header_bytes"]:
        violations.append(
            f"{path}: extension '{ext}' claims a NumPy container but the manifest "
            f"declares header_bytes={fmt.header_bytes}; a real .npy always has a "
            f"'\\x93NUMPY' header. Headerless raw token shards must be named "
            f"'.u32le.bin', never '.npy' (§5)"
        )

    return violations


# --------------------------------------------------------------------------------------
# §5 — building the manifest
# --------------------------------------------------------------------------------------


def build_manifest(entries: Iterable[ManifestEntry], *, group_name: str) -> dict[str, Any]:
    """Assemble a group manifest.

    Returns ``{"schema_version", "group", "entries", "objects", "bytes"}``.
    ``objects`` and ``bytes`` are recomputed from ``entries`` and never accepted from
    a caller — §0.4, and the direct answer to the ``inventory.json`` that claimed 98
    objects / 172 GB in a bucket holding 10 / 31.7 GB.

    Entries are emitted sorted by path so ``manifest_sha256`` is a function of the
    *content* of the group, not of the order a walk happened to discover it in. Two
    publishers listing the same prefix must produce the same hash.

    Duplicate paths raise: §5 proves completeness by path-set equality, and a repeated
    path makes that comparison meaningless (the set would silently absorb it while
    ``objects`` double-counted).
    """
    if not isinstance(group_name, str) or not group_name:
        raise ValueError("group_name must be a non-empty string")

    ordered = sorted(entries, key=lambda e: e.path)

    seen: set[str] = set()
    for entry in ordered:
        if entry.path in seen:
            raise ValueError(
                f"duplicate path {entry.path!r} in group {group_name!r}: a manifest is a "
                f"set of paths (§5 proves completeness by path-set equality)"
            )
        seen.add(entry.path)

    return {
        "schema_version": SCHEMA_VERSION,
        "group": group_name,
        "entries": [entry.to_dict() for entry in ordered],
        "objects": len(ordered),
        "bytes": sum(entry.bytes for entry in ordered),
    }


def manifest_sha256(manifest_dict: Mapping[str, Any]) -> str:
    """SHA-256 of the manifest's canonical JSON.

    This is the value that goes in ``groups[].manifest_sha256`` in ``dataset.json``
    (§3) and in ``depends_on[].manifest_sha256`` (§7). It is defined over
    :func:`~edullm_data.contracts.canonical_json`, so it is stable across dict
    insertion order and Python versions.
    """
    return sha256_bytes(canonical_json(manifest_dict))


# --------------------------------------------------------------------------------------
# §7 Gate A — the arithmetic identity
# --------------------------------------------------------------------------------------


def verify_arithmetic(entry: ManifestEntry) -> list[str]:
    """Recompute ``count.value x dtype_size (+ header_bytes) == bytes``.

    §7: "the arithmetic identity that exposed the fake ``.npy`` files
    (``86,096,509 x 4 = 344,386,036`` = exact object size)". It also catches a
    truncated shard, which no checksum can: a crashed writer leaves a
    correctly-hashed file whose declared token count no longer fits.

    Applies only when the claim is actually falsifiable:

    * ``count.unit`` is fixed-width (``tokens``, ``indices``) — ``rows`` and ``items``
      are variable-width, and ``bytes`` is not a multiple of a dtype;
    * ``format.dtype`` is in :data:`DTYPE_SIZES`;
    * the payload is stored raw and uncompressed — for ``codec != "none"`` (or a
      self-typed container like parquet) ``bytes`` is the *encoded* size, so the
      identity is not merely violated, it is meaningless. Declining to assert is the
      honest option; the extension/magic-byte checks are what stop a publisher from
      declaring a bogus codec to dodge this gate.

    ``header_bytes`` is added because it is by definition not payload. For every case
    §5 describes it is 0, so this reduces to the identity as written — but stating it
    generally means the check stays correct for a group that legitimately carries a
    header instead of quietly going wrong.
    """
    violations: list[str] = []
    count = entry.count
    if count is None:
        return violations

    unit = count.get("unit")
    if unit not in FIXED_WIDTH_UNITS:
        return violations

    dtype_size = entry.format.dtype_size
    if dtype_size is None:
        return violations

    if entry.format.codec != "none" or entry.format.container not in FIXED_WIDTH_CONTAINERS:
        return violations

    value = count["value"]
    expected_bytes = value * dtype_size + entry.format.header_bytes
    if expected_bytes != entry.bytes:
        delta = entry.bytes - expected_bytes
        violations.append(
            f"{entry.path}: count arithmetic fails — {value} {unit} x "
            f"{dtype_size} bytes/{entry.format.dtype}"
            + (f" + {entry.format.header_bytes} header bytes" if entry.format.header_bytes else "")
            + f" = {expected_bytes}, but bytes={entry.bytes} "
            f"({delta:+d}). Either the count is wrong, the dtype is wrong "
            f"(uint16-vs-uint32 halves the count), or the object is truncated"
        )
    return violations


# --------------------------------------------------------------------------------------
# §5 — exhaustiveness by path-set equality
# --------------------------------------------------------------------------------------


def diff_paths(
    manifest_paths: set[str], actual_paths: set[str]
) -> tuple[set[str], set[str]]:
    """Return ``(missing, extra)``.

    * ``missing`` — listed in the manifest, absent from the store. An incomplete
      upload, or a member still in an in-flight multipart upload (invisible to
      ``LIST``, which is why landing needs the 1-day MPU-abort rule, §10).
    * ``extra`` — present under the group prefix, absent from the manifest. This is
      the hole where "a stray shard gets silently trained on by a globbing reader"
      (§5).

    Checked in both directions on purpose. This is what replaces ``-of-NNNNN`` in
    shard names, which §5 rejects as unknowable at write time: the surviving shard
    count depends on filtering that has not run yet, so completeness has to be proven
    against the manifest rather than encoded in a filename.
    """
    return set(manifest_paths) - set(actual_paths), set(actual_paths) - set(manifest_paths)


# --------------------------------------------------------------------------------------
# §5 — shard naming
# --------------------------------------------------------------------------------------

#: ``<split>-<NNNNN>.<self-describing-ext>``, e.g. ``train-00000.u32le.bin``.
#: The split is kebab-case (its vocabulary is profile-level, not core-level, §7), the
#: ordinal is exactly five digits, and there is deliberately no ``-of-NNNNN`` group.
#:
#: Every word of the split must contain a letter. That is what makes
#: ``train-00000-of-00042.u32le.bin`` a non-match rather than a match with split
#: ``train-00000-of`` — §5 excludes ``-of-NNNNN`` outright, so the pattern must not
#: silently absorb it. No real split name (train, val, test, held-out, holdout) is a
#: bare number, so nothing legitimate is lost.
_SPLIT_WORD = r"[a-z0-9]*[a-z][a-z0-9]*"
SHARD_RE = re.compile(
    rf"^(?P<split>{_SPLIT_WORD}(?:-{_SPLIT_WORD})*)-(?P<ordinal>\d{{5}})\."
    r"(?P<ext>[A-Za-z0-9.]+)$"
)

#: Content-addressed names: ``objects/<64-hex>.bin``. §5 exempts CAS groups because
#: the hash *is* the dedup mechanism — mandating ordinals over a CAS would force the
#: same block to exist under two names, making copying the compliant path (§0.3).
CAS_RE = re.compile(r"^(?P<sha256>[0-9a-f]{64})\.(?P<ext>[A-Za-z0-9.]+)$")


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def parse_shard_name(path: str) -> tuple[str, int] | None:
    """Return ``(split, ordinal)`` for a §5 shard name, else ``None``.

    Operates on the basename, so a group-prefixed key works.
    ``"tokens/train-00007.u32le.bin"`` -> ``("train", 7)``. A CAS name, a manifest, or
    a sentinel returns ``None`` — the caller decides whether that is an exemption or a
    violation (see :func:`check_shard_naming`).
    """
    match = SHARD_RE.match(_basename(path))
    if match is None:
        return None
    return match.group("split"), int(match.group("ordinal"))


def is_cas_name(path: str) -> bool:
    """True if the basename is ``<64-hex>.<ext>`` — a content-addressed object."""
    return CAS_RE.match(_basename(path)) is not None


def check_shard_naming(path: str, *, exempt: bool = False) -> list[str]:
    """Violations for a payload path under a shard-named group.

    ``exempt=True`` for vendored trees (renaming destroys upstream verifiability) and
    for any group whose profile declares CAS naming. CAS names are recognized as
    exempt automatically and unconditionally, so a content-addressed group never has to
    remember to pass the flag.

    Naming is a **profile-level** rule (§5), so the caller — a profile check — decides
    whether to run this at all. It lives here because the regexes do.
    """
    if exempt or is_cas_name(path):
        return []
    if parse_shard_name(path) is not None:
        return []
    return [
        f"{path}: shard name does not match '<split>-<NNNNN>.<self-describing-ext>' "
        f"(§5). Note there is no '-of-NNNNN' segment: it is unknowable at write time, "
        f"so completeness is proven by path-set equality against the manifest"
    ]
