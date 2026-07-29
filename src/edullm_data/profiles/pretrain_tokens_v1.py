"""Profile ``pretrain-tokens/v1`` — packed token shards (§4, §7).

The failure this profile prevents is the audit's headline: 7,557 headerless raw-uint32
objects wearing a ``.npy`` extension, plus the silent-death modes a checksum cannot see —
an all-zeros shard, an all-EOS shard, a shard written ``uint16`` but declared ``uint32``
(which halves the count and sends ids past the vocabulary), and a crashed writer that left
a correctly-sized file with a zero-filled tail.

Every check here **recomputes against the bytes** (§0.4): it reads ~64 KB per shard at
seeded offsets via :func:`~edullm_data.profiles.base.sample_offsets` and interprets those
bytes as the *declared* dtype/byte-order — the only way to catch a dtype/endianness lie is
to decode with the declared dtype and watch the ids fly past ``vocab_size``.

The arithmetic identity (``count.value x dtype_size == bytes``) and the metadata half of
the ``.npy`` honesty rule already live in :mod:`edullm_data.manifest`; these checks are the
value-domain additions §5 says are *also* needed, not a reimplementation of them.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

import numpy as np

from ..manifest import ManifestEntry
from .base import DECODE_SAMPLE_BYTES, GroupContext, Violation, sample_offsets

NAME = "pretrain-tokens/v1"

#: Fields the group's metadata block must carry (§4 "adds required fields"). Presence is
#: declarative; the VALUES are what CHECKS recompute (§0.1 — an unchecked required field is
#: worse than an absent one, so nothing here is trusted without a byte-level counterpart).
REQUIRED_FIELDS: Mapping[str, Any] = {
    # The tokenizer is resolved from a published tokenizer/v1 dataset this group
    # depends_on; the validator derives vocab_size/eos_token_id from its tokenizer.json and
    # the decode check reads them from ctx.resolved (NOT a hand-typed block — that would
    # reintroduce the guess §0.1 warns against). A group SHOULD declare:
    #   depends_on: [{role: "tokenizer", dataset_id: "tokenizer/…", version: "vN", manifest_sha256: …}]
    # A raw declared `tokenizer` block is still accepted as a migration fallback, but the
    # derived value wins when a tokenizer dependency is present.
    #
    # Every manifest entry must declare a token count so the manifest arithmetic identity
    # (edullm_data.manifest.verify_arithmetic) is falsifiable. Enforced by
    # check_entries_declare_token_counts below.
    "entries[].count": {"type": "object", "required": ["unit", "value"], "unit": "tokens"},
    # Optional, profile-tunable bounds (all have registry defaults; declaring an absurd one
    # to pass is visible in review, §7):
    #   min_distinct_ids (default 16), max_eos_fraction (0.5), max_zero_fraction (0.5),
    #   seq_len (optional; enables the mid-sequence truncation check).
}

# Read the decode budget as four spread-out windows rather than one, so a zero-filled or
# truncated *tail* is sampled, not just a valid head (§7 "random-not-head").
_N_WINDOWS = 4
_NPY_MAGIC = b"\x93NUMPY"
_DEFAULT_MIN_DISTINCT = 16
_DEFAULT_MAX_EOS_FRACTION = 0.5
_DEFAULT_MAX_ZERO_FRACTION = 0.5


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _object_key(prefix: str, path: str) -> str:
    """Full landing key for a manifest entry. ``prefix`` is the group's landing prefix
    (``GroupContext.prefix``) and ``path`` is the manifest-relative key that already
    carries the group name, per the §5 examples (``tokens/train-00000.u32le.bin``)."""
    if not prefix:
        return path
    return prefix.rstrip("/") + "/" + path.lstrip("/")


def _shard_seed(ctx: GroupContext, path: str) -> str:
    """Per-shard seed, ``sha256(group_seed | shard_path)`` (§7 derives the sample from the
    shard path so any auditor can re-run the identical offsets)."""
    return hashlib.sha256(f"{ctx.rng_seed}:{path}".encode()).hexdigest()[:16]


def _np_dtype(entry: ManifestEntry) -> "np.dtype | None":
    """The numpy dtype the manifest *declares* — never inferred (§5: OLMo-core defaults to
    ``uint16`` while these corpora are ``uint32``, which silently halves the count)."""
    fmt = entry.format
    if fmt.dtype is None:
        return None
    try:
        base = np.dtype(fmt.dtype)
    except TypeError:
        return None
    if fmt.byte_order == "big":
        return base.newbyteorder(">")
    if fmt.byte_order == "little":
        return base.newbyteorder("<")
    return base


def _bound(ctx: GroupContext, key: str, default: Any) -> Any:
    if key in ctx.group:
        return ctx.group[key]
    if key in ctx.family_defaults:
        return ctx.family_defaults[key]
    return default


def _tokenizer(ctx: GroupContext) -> Mapping[str, Any]:
    """Tokenizer facts for the vocab-range bound, in trust order:

    1. ``ctx.resolved["tokenizer"]`` — DERIVED by the validator from the published
       tokenizer.json this dataset ``depends_on``. This is the real, unfakeable source and
       is preferred whenever present (§0.4).
    2. a declared ``group.tokenizer`` block — a fallback for datasets that (legitimately or
       during migration) don't yet reference a published tokenizer; still checked against
       bytes, but the value itself is asserted, not derived.
    3. the family default.
    """
    resolved = ctx.resolved.get("tokenizer") if isinstance(ctx.resolved, Mapping) else None
    if isinstance(resolved, Mapping) and resolved.get("vocab_size"):
        return resolved
    tok = ctx.group.get("tokenizer")
    if isinstance(tok, Mapping):
        return tok
    tok = ctx.family_defaults.get("tokenizer")
    return tok if isinstance(tok, Mapping) else {}


def _entries(ctx: GroupContext):
    """Yield ``(raw_dict, ManifestEntry | None, Violation | None)`` per manifest entry."""
    for raw in ctx.manifest.get("entries", []):
        try:
            yield raw, ManifestEntry.from_dict(raw), None
        except Exception as e:  # a structurally broken entry — manifest.py owns the schema,
            path = raw.get("path") if isinstance(raw, Mapping) else None
            yield raw, None, Violation("bad-manifest-entry", f"unparseable manifest entry: {e}", path)


def _sampled_ids(ctx: GroupContext, entry: ManifestEntry, dtype: "np.dtype") -> "np.ndarray":
    """Read ~64 KB at seeded offsets and decode as ``dtype``. Uses the *actual* object size
    from HEAD (not the declared ``bytes``) so a truncated tail is sampled honestly."""
    key = _object_key(ctx.prefix, entry.path)
    size = int(ctx.s3.head(ctx.landing_bucket, key)["size"])
    itemsize = dtype.itemsize
    if size < itemsize:
        return np.empty(0, dtype=dtype)
    window = DECODE_SAMPLE_BYTES // _N_WINDOWS
    window -= window % itemsize
    window = max(window, itemsize)
    offsets = sample_offsets(
        _shard_seed(ctx, entry.path), size, window=window, n=_N_WINDOWS, align=itemsize
    )
    chunks = [ctx.s3.get_range(ctx.landing_bucket, key, off, window) for off in offsets]
    buf = b"".join(chunks)
    trim = len(buf) - (len(buf) % itemsize)
    if trim <= 0:
        return np.empty(0, dtype=dtype)
    return np.frombuffer(buf[:trim], dtype=dtype)


# --------------------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------------------


def check_entries_declare_token_counts(ctx: GroupContext) -> list[Violation]:
    """Every shard must declare ``count{unit: "tokens", value}``.

    Not a byte read, but the precondition that makes the manifest arithmetic identity
    falsifiable — without a token count, ``count.value x 4 == bytes`` cannot fire, so a
    ``uint16``-as-``uint32`` size lie would sail through the cheap gate.
    """
    out: list[Violation] = []
    for raw, entry, bad in _entries(ctx):
        if bad is not None:
            out.append(bad)
            continue
        count = entry.count
        if not count or count.get("unit") != "tokens":
            out.append(
                Violation(
                    "token-count-unit",
                    f"pretrain-tokens shard must declare count.unit == 'tokens'; "
                    f"got {None if not count else count.get('unit')!r}",
                    entry.path,
                )
            )
    return out


def check_decode_smoke(ctx: GroupContext) -> list[Violation]:
    """The §7 decode smoke test: decode sampled tokens as the declared dtype and assert
    every id is in ``[0, vocab_size)``, that enough distinct ids appear, and that the EOS
    and zero fractions stay within bounds.

    Catches (per §7's table): wrong dtype / wrong endianness (ids past vocab), all-zeros or
    all-one-token shards (distinct too few), all-EOS shards, and partial zero-fill from a
    crashed writer.
    """
    out: list[Violation] = []
    tok = _tokenizer(ctx)
    vocab_size = tok.get("vocab_size")
    eos_id = tok.get("eos_token_id")
    min_distinct = int(_bound(ctx, "min_distinct_ids", _DEFAULT_MIN_DISTINCT))
    max_eos = float(_bound(ctx, "max_eos_fraction", _DEFAULT_MAX_EOS_FRACTION))
    max_zero = float(_bound(ctx, "max_zero_fraction", _DEFAULT_MAX_ZERO_FRACTION))

    have_vocab = isinstance(vocab_size, int) and not isinstance(vocab_size, bool)
    if not have_vocab:
        out.append(
            Violation(
                "missing-tokenizer-field",
                "tokenizer.vocab_size is absent or non-integer; the vocab-range assertion "
                "cannot be recomputed (§7 decode smoke test)",
            )
        )

    for raw, entry, bad in _entries(ctx):
        if bad is not None:
            out.append(bad)
            continue
        dtype = _np_dtype(entry)
        if dtype is None or dtype.kind not in ("u", "i"):
            # Not an integer token array — nothing to decode as token ids here.
            continue
        ids = _sampled_ids(ctx, entry, dtype)
        n = int(ids.size)
        if n == 0:
            out.append(
                Violation("empty-shard", "no bytes sampled — shard is empty or shorter than one token", entry.path)
            )
            continue

        if have_vocab:
            n_bad = int(np.count_nonzero((ids < 0) | (ids >= vocab_size)))
            if n_bad:
                out.append(
                    Violation(
                        "vocab-out-of-range",
                        f"{n_bad}/{n} sampled ids fall outside [0, {vocab_size}); "
                        f"max sampled id = {int(ids.max())}. This is the signature of a wrong "
                        f"dtype or byte order (uint16 decoded as uint32 pushes ids past vocab)",
                        entry.path,
                    )
                )

        distinct = int(np.unique(ids).size)
        if distinct < min_distinct:
            out.append(
                Violation(
                    "distinct-too-few",
                    f"only {distinct} distinct ids across {n} sampled tokens (need >= "
                    f"{min_distinct}); signature of an all-zeros or all-one-token shard",
                    entry.path,
                )
            )

        if isinstance(eos_id, int) and not isinstance(eos_id, bool):
            eos_frac = float(np.count_nonzero(ids == eos_id)) / n
            if eos_frac > max_eos:
                out.append(
                    Violation(
                        "eos-fraction-out-of-bounds",
                        f"EOS (id {eos_id}) is {eos_frac:.3f} of sampled tokens, over the "
                        f"declared max {max_eos:.3f} — signature of an all-EOS shard",
                        entry.path,
                    )
                )

        zero_frac = float(np.count_nonzero(ids == 0)) / n
        if zero_frac > max_zero:
            out.append(
                Violation(
                    "zero-fraction-out-of-bounds",
                    f"zeros are {zero_frac:.3f} of sampled tokens, over the declared max "
                    f"{max_zero:.3f} — signature of a partial zero-fill from a crashed writer",
                    entry.path,
                )
            )
    return out


def check_first_bytes_not_npy(ctx: GroupContext) -> list[Violation]:
    """The magic-byte half of the §5 honesty rule: the first bytes must not be
    ``\\x93NUMPY``.

    ``manifest.check_extension_matches_format`` catches the *metadata* lie (a ``.npy`` name
    declared ``container: raw, header_bytes: 0``). It cannot catch a writer who names a file
    ``.npy`` *and* declares ``container: npy`` — that is internally consistent, so only
    reading the leading bytes and finding a NumPy header exposes it. §5 is explicit that
    BOTH checks are needed; this is the byte-reading one, and it is the audit's actual
    7,557-object case.
    """
    out: list[Violation] = []
    for raw, entry, bad in _entries(ctx):
        if bad is not None:
            out.append(bad)
            continue
        key = _object_key(ctx.prefix, entry.path)
        head = ctx.s3.get_range(ctx.landing_bucket, key, 0, 8)
        if head.startswith(_NPY_MAGIC):
            out.append(
                Violation(
                    "npy-magic-bytes",
                    "first bytes are '\\x93NUMPY' — the object carries a real NumPy header "
                    "where headerless raw tokens were declared. OLMo-core's np.memmap reader "
                    "would eat the header as leading tokens and mis-derive the count (§5)",
                    entry.path,
                )
            )
    return out


def check_seq_len_alignment(ctx: GroupContext) -> list[Violation]:
    """``bytes % (dtype_size x seq_len) == 0`` when the group declares ``seq_len``.

    Catches a shard truncated mid-sequence. If no ``seq_len`` is declared the group is not
    pre-packed into fixed sequences, so there is nothing to align — the check is skipped.
    """
    seq_len = ctx.group.get("seq_len")
    if not isinstance(seq_len, int) or isinstance(seq_len, bool) or seq_len <= 0:
        return []  # no seq_len declared: not fixed-length-packed, nothing to check.
    out: list[Violation] = []
    for raw, entry, bad in _entries(ctx):
        if bad is not None:
            out.append(bad)
            continue
        dtype = _np_dtype(entry)
        if dtype is None:
            continue
        key = _object_key(ctx.prefix, entry.path)
        size = int(ctx.s3.head(ctx.landing_bucket, key)["size"])
        stride = dtype.itemsize * seq_len
        if stride and size % stride != 0:
            out.append(
                Violation(
                    "seq-len-misalignment",
                    f"object is {size} bytes, not a whole multiple of dtype_size "
                    f"({dtype.itemsize}) x seq_len ({seq_len}) = {stride}; a sequence is "
                    f"truncated mid-way ({size % stride} bytes over the last full sequence)",
                    entry.path,
                )
            )
    return out


CHECKS = [
    check_entries_declare_token_counts,
    check_decode_smoke,
    check_first_bytes_not_npy,
    check_seq_len_alignment,
]


# --------------------------------------------------------------------------------------
# self-registration (guarded — registry.py may not exist yet, §"registration contract")
# --------------------------------------------------------------------------------------
import sys  # noqa: E402

try:
    from . import registry as _registry

    _registry.register(sys.modules[__name__])
except Exception:
    pass
