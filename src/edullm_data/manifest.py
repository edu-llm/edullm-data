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
from typing import Any, Iterable, Mapping, Sequence

from .contracts import SCHEMA_VERSION, SPLITS, canonical_json, sha256_bytes

__all__ = [
    "DTYPE_SIZES",
    "COUNT_UNITS",
    "FIXED_WIDTH_UNITS",
    "FIXED_WIDTH_CONTAINERS",
    "EXTENSION_FORMAT",
    "PATH_LABEL_KEYS",
    "labels_from_path",
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
    "SAFE_SEGMENT_RE",
    "SEGMENT_SLUG_OVERRIDES",
    "SEGMENT_SEPARATORS",
    "SEGMENT_SEMANTIC_CHARS",
    "DNS_SUFFIXES",
    "MAX_SEGMENT_CHARS",
    "DEFAULT_DOMAIN_KEEP",
    "OTHER_SEGMENT",
    "SlugError",
    "slug_path_segment",
    "DomainSlugMap",
    "build_domain_slug_map",
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
    #: Which split this object belongs to, from the closed vocabulary in ``contracts.SPLITS``.
    #: ``None`` on a v1 manifest (the field did not exist) and on an object that has no split —
    #: a tokenizer file or a vendored blob. A v2 manifest for a split-bearing group declares it
    #: on every entry, and Gate A recomputes it from the filename, so it cannot be faked.
    split: str | None = None
    #: Free-form slice labels, e.g. ``{"source": "arxiv", "domain": "science"}``. Flat and
    #: string-valued ONLY, deliberately: a partition selects by exact label match, so a nested
    #: or richly-typed value would need a query language and a validator nobody has written.
    labels: dict[str, str] | None = None

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

        if self.split is not None:
            if not isinstance(self.split, str):
                raise ValueError(f"entry.split for {self.path!r} must be a string or null")
            if self.split not in SPLITS:
                raise ValueError(
                    f"entry.split for {self.path!r} must be one of {sorted(SPLITS)}; "
                    f"got {self.split!r}. The vocabulary is closed so that 'is this trainable?' "
                    f"is a lookup rather than a guess — a substring test over free-form names "
                    f"misreads 'trainval' as held-out and rejects 'dev' outright."
                )

        if self.labels is not None:
            if not isinstance(self.labels, Mapping):
                raise ValueError(f"entry.labels for {self.path!r} must be an object or null")
            for key, val in self.labels.items():
                if not isinstance(key, str) or not key:
                    raise ValueError(
                        f"entry.labels for {self.path!r} has a non-string or empty key {key!r}"
                    )
                if key == "split":
                    raise ValueError(
                        f"entry.labels for {self.path!r} must not carry a 'split' key — split is "
                        f"a reserved top-level field, and two places to state it is two places "
                        f"to disagree"
                    )
                if not isinstance(val, str):
                    raise ValueError(
                        f"entry.labels[{key!r}] for {self.path!r} must be a string; got "
                        f"{type(val).__name__}. Labels are flat strings so a partition can "
                        f"select by exact match without a query language."
                    )

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
        # Same reasoning for the v2 fields, and one more: omitting them keeps a v1-shaped
        # dataset's manifest BYTE-IDENTICAL after the schema bump, so `manifest_sha256` does
        # not move and an already-published dataset is not retroactively invalidated.
        if self.split is not None:
            d["split"] = self.split
        if self.labels:
            d["labels"] = {k: self.labels[k] for k in sorted(self.labels)}
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ManifestEntry":
        if not isinstance(d, Mapping):
            raise ValueError(f"manifest entry must be an object; got {type(d).__name__}")
        unknown = set(d) - {"path", "sha256", "bytes", "count", "format", "split", "labels"}
        if unknown:
            raise ValueError(f"manifest entry has unknown key(s) {sorted(unknown)}")
        for required in ("path", "sha256", "bytes", "format"):
            if required not in d:
                raise ValueError(f"manifest entry is missing required key {required!r}")
        count = d.get("count")
        labels = d.get("labels")
        return cls(
            path=d["path"],
            sha256=d["sha256"],
            bytes=d["bytes"],
            count=dict(count) if isinstance(count, Mapping) else count,
            format=Format.from_dict(d["format"]),
            # A v1 manifest has neither field. Reading it as "split unknown, no labels" rather
            # than rejecting it is the compatibility rule CONTRIBUTING states: a field added in
            # v2 must not retroactively invalidate every v1 dataset.
            split=d.get("split"),
            labels=dict(labels) if isinstance(labels, Mapping) else labels,
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
    # Opaque UTF-8 text. Not fixed-width (no dtype/byte_order), so the arithmetic
    # identity never applies; it just lets an honest text sidecar declare a truthful
    # container instead of erroring. The concrete case is a tokenizer's merges.txt
    # riding alongside tokenizer.json (families/tokenizer.json says these "may ride
    # along") — a real BPE merges file is text, and dropping it to dodge a missing
    # extension would reduce the published tokenizer to less than what loads it.
    ".txt": {"container": "text", "codec": "none"},
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
            f"({delta:+d}). The object is truncated (or grew) relative to the declared "
            f"count, or its size is not a whole multiple of the item width — a raw "
            f"fixed-width array must end on an element boundary. This is NOT a dtype "
            f"check: publish() derives count = bytes // dtype_size (publish.py:134), so "
            f"the identity collapses to bytes % dtype_size == 0, which uint16 and uint32 "
            f"satisfy equally for the same bytes. A too-narrow dtype is caught separately, "
            f"by 'dtype-too-narrow-for-vocab' against the tokenizer's derived vocab_size"
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


#: Names for the path segments BETWEEN the group and the basename, outermost first.
#: ``tokens/<source>/<domain>/train-00000.u32le.bin`` -> ``{"source": …, "domain": …}``.
#: Two levels is the depth this standard describes; a third is a producer mistake, not a
#: dimension nobody named, so :func:`labels_from_path` refuses rather than inventing a key.
PATH_LABEL_KEYS: tuple[str, ...] = ("source", "domain")


def labels_from_path(rel_path: str, *, keys: Sequence[str] = PATH_LABEL_KEYS) -> dict[str, str]:
    """Derive ``entry.labels`` from the directory segments between the group and the basename.

    A corpus that keeps its sources in the key already states the slice a shard belongs to;
    this reads that statement into the manifest so the claim is machine-readable rather than
    a string convention a consumer has to re-parse. Gate A then recomputes it
    (``_check_labels_match_path``), which is what makes the label trustworthy — an entry whose
    ``labels`` disagree with its own key is rejected. A hand-typed label would be a producer
    assertion no gate falsifies, i.e. decoration under CONTRIBUTING's golden rule.

    Flat layouts return ``{}``: there are no segments, so there is nothing to say, and an
    empty dict is omitted from the manifest entirely (``ManifestEntry.to_dict``) so a flat
    dataset's bytes are unchanged.

    Raises ``PublishError`` when the tree is deeper than ``keys`` can name. Silently dropping
    the extra segment would publish a label that is true but incomplete, and silently
    inventing ``level_3`` would put an unnamed dimension in the hash chain forever — labels
    live inside ``manifest_sha256`` and cannot be corrected without republishing.
    """
    parts = rel_path.split("/")
    middle = parts[1:-1]  # drop the group segment and the basename
    if not middle:
        return {}
    if len(middle) > len(keys):
        raise ValueError(
            f"payload key {rel_path!r} nests {len(middle)} levels under its group "
            f"({'/'.join(middle)}), but only {len(keys)} are named {tuple(keys)!r}. Name the "
            f"extra level explicitly via labels_from_path(keys=…) rather than shipping an "
            f"unlabelled dimension — entry.labels is inside manifest_sha256, so a label that "
            f"is wrong today cannot be fixed without republishing the payload."
        )
    return {key: seg for key, seg in zip(keys, middle)}


# --------------------------------------------------------------------------------------
# §5 — turning an INHERITED upstream value into a path segment
# --------------------------------------------------------------------------------------
#
# This lives next to `labels_from_path` on purpose: it is the inverse half of the same
# one-way door. `labels_from_path` reads a segment back OUT of a key; this decides what goes
# IN. Both sit inside `manifest_sha256`, so a mistake in either costs a republish and a full
# re-copy of the payload — and the round-trip check below calls `labels_from_path` directly,
# which it can only do from here without an import cycle. A reviewer sees both halves of the
# path<->labels contract on one screen. The module stays pure (no AWS, no I/O), as its own
# docstring promises.
#
# The concrete driver: a source whose upstream metadata already names a subdomain
# (`metadata.gha_language`, `metadata.site`, an FDC subject) can have that value INHERITED
# into the key as `tokens/<source>/<domain>/…`. An inherited value is better evidence than a
# classified one — it is the upstream publisher's own statement of fact rather than a model's
# guess about it. But the raw value is not a path segment, and the gap is dangerous rather
# than cosmetic.


class SlugError(ValueError):
    """A value cannot become a path segment safely, or a slug map is ambiguous.

    A ``ValueError`` subclass so `publish`'s existing handling around `labels_from_path` keeps
    working: this is the same class of failure — a key that cannot be labelled honestly —
    caught one step earlier, before any byte is copied.
    """


#: The only shape a derived segment may take: lowercase alphanumerics joined by single
#: dashes, no leading/trailing dash. Deliberately NARROWER than what S3 or `labels_from_path`
#: accept, because both accept far too much. All four verified by execution:
#:
#:   * ``tokens/stackv2-edu/C#/train-00000.u32le.bin`` — `urlparse` of the ``s3://`` URI puts
#:     everything from the ``#`` onward into ``fragment``, so ``path`` ends at ``…/C`` and THE
#:     SHARD NAME IS GONE FROM THE PATH. `labels_from_path` returns
#:     ``{'source': 'stackv2-edu', 'domain': 'C#'}`` happily and `fnmatch` matches
#:     ``tokens/stackv2-edu/*/train-*.u32le.bin`` — so nothing in this pipeline catches it. It
#:     breaks later, in a consumer, on data that is by then frozen.
#:   * ``Jupyter Notebook`` (space) and ``C++`` (plus) survive `urlparse` but need escaping in
#:     every URL, shell line and CLI argument that ever touches the key.
#:   * ``a[b]`` is not even fnmatch-inert: ``fnmatch(key, key)`` is FALSE, so a literal glob
#:     built from the key fails to match the key it was built from.
SAFE_SEGMENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Characters whose removal does not change WHICH THING the value names — pure punctuation
#: and word separators. These collapse to a dash. A character outside this set and outside
#: :data:`SEGMENT_SEMANTIC_CHARS` is REFUSED, never dropped; see :func:`slug_path_segment`.
SEGMENT_SEPARATORS = " \t\n_-./()[]{}&,'\"“”‘’:;!?"

#: Characters that CARRY MEANING and are therefore spelled out, never dropped. This is the
#: heart of the problem: generic character-stripping maps both ``C#`` and ``C++`` to ``c`` —
#: the SAME value — silently merging two languages into one permanent directory.
SEGMENT_SEMANTIC_CHARS: dict[str, str] = {"#": "sharp", "+": "plus"}

#: Explicitly verified upstream names, consulted BEFORE any generic rule. Two jobs:
#:
#: 1. It is the authority for the names that actually appear in the data, so the mapping a
#:    teammate reads in the README is reviewable rather than inferred from code.
#: 2. It PINS the answers that matter. The generic expansion of
#:    :data:`SEGMENT_SEMANTIC_CHARS` already produces ``c-sharp`` and ``c-plus-plus``, and
#:    these entries mean a later edit to that expansion cannot silently move them — which
#:    would move `manifest_sha256` and every ``depends_on`` pin written against it.
#:
#: Keys match case-insensitively against the stripped raw value.
SEGMENT_SLUG_OVERRIDES: dict[str, str] = {
    # gha_language values whose identity IS punctuation (all present in stackv2-edu)
    "c#": "c-sharp",
    "c++": "c-plus-plus",
    "f#": "f-sharp",
    "f*": "f-star",
    "q#": "q-sharp",
    "objective-c": "objective-c",
    "objective-c++": "objective-c-plus-plus",
    "jupyter notebook": "jupyter-notebook",
}

#: DNS suffixes stripped before slugging, longest first, so a StackExchange site becomes the
#: site NAME rather than a hostname: ``3dprinting.stackexchange.com`` -> ``3dprinting``,
#: ``mathoverflow.stackexchange.com`` -> ``mathoverflow``. Without this, ~180 segments would
#: each end in the same ``-stackexchange-com`` — 19 characters of identical noise in every
#: key, inside the hash chain, forever.
DNS_SUFFIXES: tuple[str, ...] = (
    ".stackexchange.com",
    ".stackoverflow.com",
    ".wikipedia.org",
    ".com",
    ".org",
    ".net",
    ".io",
    ".edu",
    ".gov",
)

#: Hard cap on a derived segment. 63 is the DNS label limit — the most widely implemented
#: "one path segment" bound there is, and comfortably inside S3's 1024-byte whole-key budget
#: even with a long ``<family>/<name>/<version>/`` prefix plus two label levels. A longer
#: value is REFUSED, never truncated: truncation is exactly how two distinct values silently
#: become one directory.
MAX_SEGMENT_CHARS = 63

#: How many distinct domain values keep their own segment; the rest fold into
#: :data:`OTHER_SEGMENT`. **20**, because cardinality is permanent:
#:
#:   * Every distinct value is a directory that exists inside `manifest_sha256` forever. The
#:     verified tails are 73 ``gha_language`` values in ONE stackv2-edu shard and ~180
#:     StackExchange sites, so keeping everything commits ~250 permanent directories; keeping
#:     the top 20 of each commits ~60.
#:   * A domain is worth naming only if it is a usable SLICE, and a slice's usefulness is set
#:     by how many shards it holds. On a Zipf-ish vocabulary the head holds hundreds of shards
#:     per value while the 20th holds a handful and the 50th holds one or two. A domain with
#:     three shards in it cannot be re-weighted toward a target ratio with any precision, so
#:     it buys a permanent commitment and returns nothing.
#:   * 20 is a default, not a law. :func:`build_domain_slug_map` reports
#:     :attr:`DomainSlugMap.folded_fraction` so an operator can see what the choice cost and
#:     raise ``keep`` — before the publish, which is the only moment it is still free.
DEFAULT_DOMAIN_KEEP = 20

#: Where the folded tail lands. A real value that slugs to this is an error, not a merge —
#: see :func:`build_domain_slug_map`.
OTHER_SEGMENT = "other"

_DASH_RUN_RE = re.compile(r"-{2,}")

#: A probe key shaped exactly like a real one, for checking a candidate segment against the
#: three consumers that actually matter instead of trusting the regex. CONTRIBUTING's golden
#: rule in miniature: recompute, do not assert.
_PROBE_PREFIX = "pretrain/slug-probe/v1"
_PROBE_GROUP = "tokens"
_PROBE_SOURCE = "probe-source"
_PROBE_BASENAME = "train-00000.u32le.bin"


def _verify_segment_survives_its_consumers(segment: str) -> list[str]:
    """Run a candidate segment through `urlparse`, `fnmatch` and `labels_from_path`.

    Those three are the entire consumer surface of a path segment in this package: the reader
    hands out ``s3://`` URIs, partitions resolve by `fnmatch` glob, and Gate A recomputes
    labels off the key. The regex above is the cheap gate; this is the one that would have
    caught ``C#``, because ``C#`` passes every metadata check in the pipeline and fails HERE.

    Returns problem strings; empty means the segment round-trips.
    """
    import fnmatch as _fnmatch
    from urllib.parse import urlparse as _urlparse

    problems: list[str] = []
    rel = f"{_PROBE_GROUP}/{_PROBE_SOURCE}/{segment}/{_PROBE_BASENAME}"
    key = f"{_PROBE_PREFIX}/{rel}"
    parsed = _urlparse(f"s3://edullm-data/{key}")

    if parsed.fragment or parsed.query:
        problems.append(
            f"urlparse of the s3:// URI SPLITS the key: path ends at {parsed.path!r} with "
            f"fragment={parsed.fragment!r} query={parsed.query!r} — the shard name is no "
            f"longer part of the path"
        )
    if parsed.path != f"/{key}":
        problems.append(
            f"urlparse of the s3:// URI does not round-trip: path is {parsed.path!r}, "
            f"expected {'/' + key!r}"
        )
    if not _fnmatch.fnmatch(rel, rel):
        problems.append(
            "the key does not match ITSELF as an fnmatch pattern — a glob metacharacter in "
            "the segment makes a literal partition glob built from this key fail to match it"
        )
    if not _fnmatch.fnmatch(rel, f"{_PROBE_GROUP}/*/*/train-*.u32le.bin"):
        problems.append("the key is not matched by the standard two-level partition glob")
    try:
        labels = labels_from_path(rel)
    except ValueError as exc:  # pragma: no cover — a one-segment probe cannot nest too deep
        problems.append(f"labels_from_path refuses the key: {exc}")
    else:
        if labels.get("domain") != segment:
            problems.append(
                f"labels_from_path reads the domain back as {labels.get('domain')!r}, "
                f"not {segment!r}"
            )
    return problems


def slug_path_segment(
    value: str,
    *,
    overrides: Mapping[str, str] | None = None,
    verify: bool = True,
) -> str:
    """One inherited upstream value -> one safe path segment. Deterministic and pure.

    ``"C#"`` -> ``"c-sharp"``, ``"C++"`` -> ``"c-plus-plus"``,
    ``"Jupyter Notebook"`` -> ``"jupyter-notebook"``,
    ``"3dprinting.stackexchange.com"`` -> ``"3dprinting"``.

    **This function refuses more than it rewrites, and that is the design.** A segment lives
    inside `manifest_sha256`; a wrong one cannot be corrected without republishing and
    re-copying every byte. So every case where the honest answer is unknowable raises
    :class:`SlugError` naming the offending value rather than emitting a guess:

    * a character that is neither punctuation (:data:`SEGMENT_SEPARATORS`) nor a spelled-out
      semantic character (:data:`SEGMENT_SEMANTIC_CHARS`) — dropping such a character is what
      turns ``C#`` and ``C++`` into the same ``c``;
    * a value that slugs to nothing, or to more than :data:`MAX_SEGMENT_CHARS` (refused, never
      truncated);
    * a candidate that fails the round-trip through `urlparse`, `fnmatch` and
      `labels_from_path`. ``verify=False`` skips only that last stage, and only for a caller
      that wants to inspect a raw candidate.

    A collision between two DIFFERENT values is not detectable here — this sees one value at a
    time. That check lives in :func:`build_domain_slug_map`, which is the only sanctioned way
    to derive segments for a real corpus: running this over an upstream vocabulary yourself
    skips the one check that matters most.
    """
    if not isinstance(value, str):
        raise SlugError(f"cannot slug {value!r}: a domain value must be a string")
    raw = value.strip()
    if not raw:
        raise SlugError("cannot slug an empty (or whitespace-only) domain value")

    extra = {str(k).strip().lower(): str(v) for k, v in (overrides or {}).items()}
    pinned = {**SEGMENT_SLUG_OVERRIDES, **extra}.get(raw.lower())
    if pinned is not None:
        candidate = pinned
    else:
        import unicodedata

        work = raw.lower()
        for suffix in sorted(DNS_SUFFIXES, key=len, reverse=True):
            if work.endswith(suffix) and len(work) > len(suffix):
                work = work[: -len(suffix)]
                break
        # Spell the semantic characters BEFORE anything can strip them, and with surrounding
        # dashes so `c++` becomes `c-plus-plus` rather than `cplusplus`.
        for char, word in SEGMENT_SEMANTIC_CHARS.items():
            work = work.replace(char, f"-{word}-")
        # NFKD -> ASCII so `Café` becomes `cafe` instead of raising: an accent is a rendering
        # of a letter, not a different letter, so losing it does not change what is named.
        work = unicodedata.normalize("NFKD", work).encode("ascii", "ignore").decode("ascii")

        unknown = sorted(
            {
                ch
                for ch in work
                if not (ch.isalnum() and ch.isascii()) and ch not in SEGMENT_SEPARATORS
            }
        )
        if unknown:
            raise SlugError(
                f"cannot slug {value!r}: character(s) {unknown!r} are neither punctuation nor a "
                f"known semantic character, so this function cannot tell whether dropping them "
                f"changes what the value NAMES. Dropping them quietly is exactly how 'C#' and "
                f"'C++' both become 'c' and two languages merge into one permanent directory. "
                f"Either add an explicit entry to SEGMENT_SLUG_OVERRIDES (or pass overrides=) "
                f"spelling out the intended segment, or add the character to "
                f"SEGMENT_SEMANTIC_CHARS with the word it stands for."
            )
        candidate = "".join(ch if ch.isalnum() else "-" for ch in work)

    candidate = _DASH_RUN_RE.sub("-", candidate).strip("-")

    if not candidate:
        raise SlugError(
            f"cannot slug {value!r}: nothing survives as a segment. An empty segment would "
            f"collapse the key by one level, so `labels_from_path` would read the SHARD NAME "
            f"as the domain."
        )
    if len(candidate) > MAX_SEGMENT_CHARS:
        raise SlugError(
            f"cannot slug {value!r}: the segment {candidate!r} is {len(candidate)} chars, over "
            f"the {MAX_SEGMENT_CHARS}-char limit. Refused rather than truncated — truncation "
            f"is how two distinct values silently become one directory. Give it an explicit "
            f"short name via overrides=."
        )
    if not SAFE_SEGMENT_RE.match(candidate):
        raise SlugError(
            f"cannot slug {value!r}: the candidate {candidate!r} is still not a safe segment "
            f"(must match {SAFE_SEGMENT_RE.pattern!r}). This is reachable from an override "
            f"that is itself unsafe — the one path the generic rules cannot clean up."
        )
    if verify:
        problems = _verify_segment_survives_its_consumers(candidate)
        if problems:
            raise SlugError(
                f"the segment {candidate!r} derived from {value!r} does not survive its own "
                f"consumers:\n  " + "\n  ".join(problems)
            )
    return candidate


@dataclass(frozen=True)
class DomainSlugMap:
    """The published, reversible record of how upstream values became path segments.

    Returned by :func:`build_domain_slug_map` rather than left implicit, because the segment
    is inside `manifest_sha256` and the raw value is stored nowhere else in the dataset.
    Without this table ``c-sharp`` is unreadable — nobody can show it means ``C#`` rather than
    ``CSharp``, and nobody can tell whether ``other`` swallowed 0.4% of the tokens or 40%.
    :meth:`readme_table` is what goes in the generated README, which is a control file outside
    the hash chain and therefore still revisable on a frozen dataset.
    """

    #: EVERY input value -> the segment it publishes under, folded values included.
    slug_of: dict[str, str]
    #: Values that kept their own segment, highest-ranked first.
    kept: tuple[str, ...]
    #: Values folded into :attr:`other_segment`, highest-ranked first.
    folded: tuple[str, ...]
    #: The distinct segments that will exist as directories, sorted. This is the permanent
    #: commitment, and the number to look at before publishing.
    segments: tuple[str, ...]
    #: The counts used to rank, echoed back so the ranking is auditable rather than asserted.
    weight_of: dict[str, int]
    other_segment: str
    keep: int
    #: What the counts counted — ``"tokens"``, ``"documents"``, … Recorded because ranking by
    #: documents and ranking by tokens give different answers, and a reader of the README
    #: cannot otherwise tell which one produced the table.
    unit: str | None = None

    @property
    def kept_weight(self) -> int:
        return sum(self.weight_of[v] for v in self.kept)

    @property
    def folded_weight(self) -> int:
        return sum(self.weight_of[v] for v in self.folded)

    @property
    def folded_fraction(self) -> float:
        """Share of the ranking weight that lands in ``other`` — the number that says whether
        ``keep`` was set well, and the only moment it can still be changed for free is before
        the publish."""
        total = self.kept_weight + self.folded_weight
        return (self.folded_weight / total) if total else 0.0

    def apply(self, value: str) -> str:
        """The segment for one value. Raises on a value the map has never seen.

        Deliberately not ``.get(value, other)``: a value absent from the ranking table means
        the counts the map was built from and the data being published DISAGREE, and filing
        the surprise under ``other`` would hide that while writing a permanent directory.
        """
        try:
            return self.slug_of[value]
        except KeyError:
            raise SlugError(
                f"{value!r} is not in this slug map, which covers {len(self.slug_of)} value(s). "
                f"A value the map has not seen means the counts it was built from do not cover "
                f"the data being published; folding it into {self.other_segment!r} silently "
                f"would write a permanent directory to hide that disagreement. Rebuild the map "
                f"from counts that cover the whole corpus."
            ) from None

    def to_dict(self) -> dict[str, Any]:
        return {
            "keep": self.keep,
            "unit": self.unit,
            "other_segment": self.other_segment,
            "segments": list(self.segments),
            "kept": list(self.kept),
            "folded": list(self.folded),
            "slug_of": {k: self.slug_of[k] for k in sorted(self.slug_of)},
            "weight_of": {k: self.weight_of[k] for k in sorted(self.weight_of)},
            "folded_fraction": round(self.folded_fraction, 6),
        }

    def readme_table(self, *, title: str = "Domain slug map") -> str:
        """Markdown for the generated README, so ``c-sharp`` -> ``C#`` stays recoverable."""
        unit = self.unit or "weight"
        lines = [
            f"### {title}",
            "",
            f"`domain` segments are slugged from the upstream value, keeping the top "
            f"{self.keep} by {unit} and folding the rest into `{self.other_segment}`. "
            f"{len(self.segments)} segment(s) exist; the folded tail is "
            f"{self.folded_fraction * 100:.2f}% of {unit}.",
            "",
            f"| `domain` segment | upstream value | {unit} |",
            "|---|---|---|",
        ]
        for value in self.kept:
            lines.append(f"| `{self.slug_of[value]}` | `{value}` | {self.weight_of[value]:,} |")
        if self.folded:
            folded = ", ".join(f"`{v}`" for v in self.folded)
            lines.append(f"| `{self.other_segment}` | {folded} | {self.folded_weight:,} |")
        lines.append("")
        return "\n".join(lines)


def build_domain_slug_map(
    weights: Mapping[str, Any],
    *,
    keep: int = DEFAULT_DOMAIN_KEEP,
    other_segment: str = OTHER_SEGMENT,
    overrides: Mapping[str, str] | None = None,
    unit: str | None = None,
) -> DomainSlugMap:
    """Slug + fold an inherited domain vocabulary into a deterministic, publishable mapping.

    ``weights`` is value -> count (tokens, or documents — say which via ``unit``). The top
    ``keep`` values by count keep their own segment; the rest fold into ``other_segment``. The
    same input always yields the same map: ties break on the raw value, so nothing depends on
    dict insertion order or on which shard happened to be read first.

    **A COLLISION RAISES.** Two distinct upstream values slugging to one segment is the
    failure this whole function exists to prevent: ``C#`` and ``C++`` both stripping to ``c``
    would merge two languages into one directory, inside `manifest_sha256`, permanently, with
    every token count still adding up and nothing anywhere to notice. So a collision is
    refused with both raw values named and the fix stated. The fold into ``other`` is the ONE
    sanctioned many-to-one, and it is recorded value-by-value in
    :attr:`DomainSlugMap.folded` — a listed merge, not a silent one.

    A tail of exactly ONE value is kept rather than folded: ``other`` holding a single value
    costs the same one directory while destroying that value's name for nothing.
    """
    if not isinstance(weights, Mapping) or not weights:
        raise SlugError("build_domain_slug_map needs a non-empty {value: count} mapping")
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise SlugError(f"keep must be an int >= 1; got {keep!r}")

    cleaned: dict[str, int] = {}
    for value, count in weights.items():
        if not isinstance(value, str) or not value.strip():
            raise SlugError(f"domain value {value!r} is not a non-empty string")
        if isinstance(count, bool) or not isinstance(count, (int, float)):
            raise SlugError(f"count for {value!r} must be a number; got {count!r}")
        if count < 0:
            raise SlugError(f"count for {value!r} must be >= 0; got {count!r}")
        cleaned[value] = int(count)

    # A TOTAL order: weight descending, then the value itself. Without the tiebreak the map is
    # a function of dict order, and a map that is not deterministic cannot be published —
    # re-running the build would silently move segments that are already in the hash chain.
    ranked = sorted(cleaned, key=lambda v: (-cleaned[v], v))
    kept = ranked[:keep]
    tail = ranked[keep:]
    if len(tail) == 1:
        kept, tail = ranked, []

    slug_of: dict[str, str] = {}
    owner: dict[str, str] = {}
    for value in kept:
        segment = slug_path_segment(value, overrides=overrides)
        if segment == other_segment:
            raise SlugError(
                f"{value!r} slugs to {segment!r}, which is the fold-target segment — it would "
                f"be indistinguishable from the folded tail, one directory meaning two "
                f"different things. Give it an explicit name via overrides=, or pass a "
                f"different other_segment=."
            )
        clash = owner.get(segment)
        if clash is not None:
            raise SlugError(
                f"COLLISION: {clash!r} and {value!r} both slug to {segment!r}. Publishing this "
                f"would merge two distinct upstream values into one permanent directory inside "
                f"manifest_sha256, with every token count still adding up and nothing "
                f"downstream able to tell them apart. Add an explicit entry for at least one of "
                f"them to SEGMENT_SLUG_OVERRIDES, or pass overrides={{…}}."
            )
        owner[segment] = value
        slug_of[value] = segment
    for value in tail:
        slug_of[value] = other_segment

    return DomainSlugMap(
        slug_of=slug_of,
        kept=tuple(kept),
        folded=tuple(tail),
        segments=tuple(sorted(set(slug_of.values()))),
        weight_of=cleaned,
        other_segment=other_segment,
        keep=keep,
        unit=unit,
    )


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
