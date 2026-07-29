"""Primitives every other module depends on: canonical JSON, hashing, naming rules.

Implements DATASET-STANDARD.md §2 (location and naming), §3 (the invariant core's
``schema_version`` and ``version`` block), and the hashing used by §5's manifests.

Everything here is a pure function with no AWS dependency, per §13 step 3.

Design note (§0.1, §0.4): every rule in this module *recomputes* or *mechanically
decides* something. There is no "field is present" validation, because an unchecked
required field is worse than an absent one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Literal, get_args

__all__ = [
    "SCHEMA_VERSION",
    "FAMILIES",
    "RELATIONS",
    "NamingError",
    "canonical_json",
    "sha256_bytes",
    "sha256_file",
    "validate_dataset_id",
    "validate_name",
    "validate_purpose",
    "Version",
]

# --------------------------------------------------------------------------------------
# Schema identity
# --------------------------------------------------------------------------------------

#: §3 — the value of ``schema_version`` in every ``dataset.json`` and ``manifest.json``.
SCHEMA_VERSION = "edullm-dataset/v1"

# --------------------------------------------------------------------------------------
# Canonical JSON + hashing
# --------------------------------------------------------------------------------------

#: 8 MiB. Large enough that hashing a 633 GB corpus is bandwidth-bound (§11: ~21 min),
#: small enough to stay out of the way on a laptop.
_CHUNK_BYTES = 8 * 1024 * 1024


def canonical_json(value: Any) -> bytes:
    """Serialize ``value`` to the one byte-string the hash chain is defined over.

    This exact form is load-bearing: ``manifest_sha256`` and every
    ``groups[].manifest_sha256`` in §3 are digests of this output, so any change to
    the separators, key order, escaping, or NaN handling silently invalidates every
    published artifact. Do not "improve" it.

    * ``sort_keys=True``   — key order must not depend on dict insertion order.
    * ``separators``       — no incidental whitespace.
    * ``ensure_ascii=False`` — emit real UTF-8 rather than ``\\uXXXX`` escapes.
    * ``allow_nan=False``  — NaN/Infinity are not JSON; fail loudly instead of
      emitting a token no other parser accepts.
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_bytes(b: bytes) -> str:
    """Hex SHA-256 of ``b``."""
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> tuple[str, int]:
    """Stream ``path`` and return ``(hexdigest, size_in_bytes)``.

    Streamed in 8 MiB chunks so a 13 GiB shard never lands in memory. The size is
    returned from the same pass the digest came from, so the pair is self-consistent
    even if the file is being appended to concurrently (§7 wants the two facts to
    describe the same bytes).
    """
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


# --------------------------------------------------------------------------------------
# §2 — naming
# --------------------------------------------------------------------------------------


class NamingError(ValueError):
    """A ``dataset_id``, ``<name>``, ``purpose``, or ``version`` violates §2 / §3."""


#: §2 — a fixed enum, on purpose. Adding a family is a deliberate edit to the standard.
FAMILIES = frozenset({"pretrain", "curriculum", "sft", "eval", "probe", "vendor", "tokenizer"})

#: §2 — kebab-case, lowercase, and nothing else. Rejects ``dolma2_150B``.
_WORD_RE = re.compile(r"^[a-z0-9]+$")
_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

_MIN_WORDS = 2
_MAX_WORDS = 5

# --- date tokens (§2 "no dates") ------------------------------------------------------
#
# The hard part is rejecting dates *without* eating legitimate scale suffixes and
# upstream release codes. All of these must survive: 370m, 150b, 5b5, 10b, 4hop,
# 100students, 5x5, s5, tulu3, dolma2, and 1124 (the release code in
# `pretrain/olmo-mix-1124-30b`). So "contains four digits" is not usable as a rule.
# Three narrow shapes instead:

#: YYYYMMDD as a run of 8 digits with a 19xx/20xx year prefix, e.g. ``20260728``.
#: Checked before the 4-digit rule because the lookarounds there cannot see inside it.
_YYYYMMDD_RE = re.compile(r"(?<!\d)(?:19|20)\d{6}(?!\d)")

#: A bare 4-digit run that *looks like a year* — 19xx or 20xx. ``1124`` starts ``11``
#: and is therefore allowed; ``2026`` and ``1990s`` are not.
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")

_MONTH_ABBREVS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)

#: Month abbreviation immediately followed by digits — ``jul22``, ``sep10``, ``mar1``.
#: Anchored on digits so ``october``/``decoder``-shaped words are untouched.
_MONTH_DIGITS_RE = re.compile(r"(?:" + "|".join(_MONTH_ABBREVS) + r")\d")

#: A bare month word is also a date token (``pretrain/mix-jul`` is no better than
#: ``mix-jul22``). Whole-token match only, so ``mar`` inside a longer word is safe.
_BARE_MONTHS = frozenset(_MONTH_ABBREVS) | frozenset(_MONTH_NAMES)

# --- version tokens (§2 "no version tokens") -----------------------------------------

#: v1..v99. ``version`` is a separate path segment; it never belongs in ``<name>``.
_VERSION_NUM_RE = re.compile(r"^v[0-9]{1,2}$")

_VERSION_WORDS = frozenset({"final", "new", "latest", "fixed", "old", "current"})

# --- content-free and relative words -------------------------------------------------

#: §2 examples ``pretrain/data``, ``eval/results``, ``pretrain/eric-test``.
_BANNED_GENERIC = frozenset({"test", "tmp", "temp", "scratch", "data", "results", "misc", "stuff"})

#: §2 "no relative words". These are unfalsifiable and meaningless in six weeks.
_BANNED_RELATIVE = frozenset(
    {"big", "small", "improved", "better", "best", "good", "bad", "fast", "slow"}
)

#: A token of nothing but digits is a meaningless ordinal (§2 rejects
#: ``curriculum/experiment-3``). The single exception is a 4-digit run that already
#: survived the year check — that is the upstream-release-code shape (``1124``).
_ORDINAL_EXEMPT_DIGIT_LEN = 4


def _reject_token(token: str, position: int, name: str) -> None:
    """Raise ``NamingError`` if ``token`` breaks any §2 word-level rule."""
    where = f"word {position} ({token!r}) of name {name!r}"

    if not _WORD_RE.match(token):
        raise NamingError(
            f"{where} is not lowercase kebab-case: only [a-z0-9] is allowed "
            f"(§2: kebab-case, lowercase)"
        )

    # Dates first — they are the failure the standard opens with.
    if _YYYYMMDD_RE.search(token):
        raise NamingError(
            f"{where} contains a YYYYMMDD date; §2 forbids dates in names "
            f"(the version segment and created_at carry time, not the name)"
        )
    if _YEAR_RE.search(token):
        raise NamingError(
            f"{where} contains a year-shaped 4-digit token; §2 forbids dates in names. "
            f"(4-digit upstream release codes that are not 19xx/20xx, e.g. '1124', are fine)"
        )
    if _MONTH_DIGITS_RE.search(token) or token in _BARE_MONTHS:
        raise NamingError(
            f"{where} contains a month token; §2 forbids dates in names "
            f"(e.g. 'datamix1-jul22' held objects dated 2026-07-28)"
        )

    if _VERSION_NUM_RE.match(token) or token in _VERSION_WORDS:
        raise NamingError(
            f"{where} is a version token; §2 forbids these because <version> is a "
            f"separate path segment (and 'final' never is)"
        )

    if token in _BANNED_GENERIC:
        raise NamingError(
            f"{where} is content-free; §2 forbids it because it does not distinguish "
            f"this dataset from any sibling"
        )

    if token in _BANNED_RELATIVE:
        raise NamingError(
            f"{where} is a relative word; §2 forbids these because they are "
            f"unfalsifiable and meaningless in six weeks"
        )

    if token.isdigit() and len(token) != _ORDINAL_EXEMPT_DIGIT_LEN:
        raise NamingError(
            f"{where} is a bare ordinal with no semantics; §2 rejects "
            f"'curriculum/experiment-3'. Attach a unit or an axis "
            f"('370m', '5b5', '4hop') or use the upstream 4-digit release code"
        )


def validate_name(name: str) -> str:
    """Validate a ``<name>`` segment against §2. Returns it unchanged.

    Mechanically enforced: charset, word count, dates, version tokens, content-free
    words, relative words, bare ordinals.

    Deliberately *not* enforced: "no person names, no ticket ids". There is no
    mechanical test for those, and per §0.1 a rule that cannot be checked is worse
    than no rule — it would be a schema field nobody verifies. ``pretrain/eric-test``
    is caught by the ``test`` ban instead; review catches the rest.
    """
    if not isinstance(name, str) or not name:
        raise NamingError("name must be a non-empty string")

    if not _NAME_RE.match(name):
        raise NamingError(
            f"name {name!r} is not kebab-case: it must match "
            f"[a-z0-9]+(-[a-z0-9]+)* (§2 rejects 'dolma2_150B' for snake_case + capitals)"
        )

    words = name.split("-")
    if not (_MIN_WORDS <= len(words) <= _MAX_WORDS):
        raise NamingError(
            f"name {name!r} has {len(words)} word(s); §2 requires "
            f"{_MIN_WORDS}-{_MAX_WORDS}. A name must state what the data is *plus* the "
            f"one axis that distinguishes it from its siblings"
        )

    for position, word in enumerate(words, start=1):
        _reject_token(word, position, name)

    return name


def validate_dataset_id(dataset_id: str) -> tuple[str, str]:
    """Validate ``<family>/<name>`` per §2 and return ``(family, name)``.

    Raises :class:`NamingError` with a message naming the specific broken rule --
    §0's point is that a rejection must teach, not just refuse.
    """
    if not isinstance(dataset_id, str) or not dataset_id:
        raise NamingError("dataset_id must be a non-empty string")

    if dataset_id.count("/") != 1:
        raise NamingError(
            f"dataset_id {dataset_id!r} must be exactly '<family>/<name>' with one '/'; "
            f"found {dataset_id.count('/')}. The <version> segment is allocated, never typed"
        )

    family, name = dataset_id.split("/", 1)

    if family not in FAMILIES:
        raise NamingError(
            f"family {family!r} is not one of the §2 enum "
            f"{sorted(FAMILIES)}; a free-text family segment becomes a second naming problem"
        )

    validate_name(name)
    return family, name


# --------------------------------------------------------------------------------------
# §2 — purpose
# --------------------------------------------------------------------------------------

_PURPOSE_MIN_CHARS = 20
_PURPOSE_MAX_CHARS = 300

#: §2's reject table, plus the empty string. Compared after normalization, so
#: "TODO", " todo ", and "To-Do!" all collapse to the same key.
_PURPOSE_BLOCKLIST_RAW = (
    "",
    "todo",
    "tbd",
    "data",
    "training data",
    "the dataset",
    "dataset",
    "experiments",
    "see readme",
    "data from the run",
    "corpus for the project",
)


def _normalize_purpose(purpose: str) -> str:
    """Lowercase and strip everything that is not alphanumeric.

    "case/space-insensitive" is read as broadly as possible: punctuation is dropped
    too, so ``"TODO."``, ``"to do"``, and ``"To-Do"`` cannot route around the
    blocklist. Per §0.1, a check that a one-character edit defeats is decoration.
    """
    return re.sub(r"[^a-z0-9]+", "", purpose.lower())


_PURPOSE_BLOCKLIST = frozenset(_normalize_purpose(p) for p in _PURPOSE_BLOCKLIST_RAW)


def validate_purpose(purpose: str) -> None:
    """Validate ``purpose`` per §2. Raises :class:`NamingError`; returns ``None``.

    §2's shape to aim for: ``<what it is> for <what consumes it> to <what it decides>``.
    """
    if not isinstance(purpose, str):
        raise NamingError("purpose must be a string")

    stripped = purpose.strip()

    # Blocklist first, so the error names the real problem ("says nothing") rather
    # than an incidental symptom ("too short").
    if _normalize_purpose(stripped) in _PURPOSE_BLOCKLIST:
        raise NamingError(
            f"purpose {purpose!r} is one of §2's rejected placeholders. State the "
            f"decision it supports: '<what it is> for <what consumes it> to "
            f"<what it decides>'"
        )

    if len(stripped) < _PURPOSE_MIN_CHARS:
        raise NamingError(
            f"purpose is {len(stripped)} chars; §2 needs at least {_PURPOSE_MIN_CHARS}. "
            f"Name the artifact, the consumer, and the question it answers"
        )

    if len(stripped) > _PURPOSE_MAX_CHARS:
        raise NamingError(
            f"purpose is {len(stripped)} chars; the limit is {_PURPOSE_MAX_CHARS}. "
            f"§2 wants one line — put footguns and caveats in 'notes' or 'limitations[]'"
        )

    if " " not in stripped:
        raise NamingError(
            f"purpose {purpose!r} is a single token; §2 wants one descriptive line, "
            f"not a slug"
        )


# --------------------------------------------------------------------------------------
# §2 / §3 — the version block
# --------------------------------------------------------------------------------------

Relation = Literal["supersedes", "extends", "sibling"]

#: §2: ``relation ∈ {supersedes, extends, sibling}``.
RELATIONS: frozenset[str] = frozenset(get_args(Relation))

_VERSION_ID_RE = re.compile(r"^v[1-9][0-9]*$")

#: The first version. §3's example is ``{"id": "v1", "relation": "supersedes",
#: "of": null}`` — a v1 has nothing to point at.
_FIRST_VERSION_ID = "v1"


@dataclass(frozen=True)
class Version:
    """§2 / §3 ``version`` block: ``{"id": "v3", "relation": "supersedes", "of": "v2"}``.

    ``relation`` carries meaning monotonic ordering cannot: ``extends`` for a
    generation consumed *alongside* its base, ``sibling`` for two snapshots whose
    order is genuinely unknown (better than fabricating one).

    Frozen because §1's landing writes are create-only — a version block is reserved
    with ``IfNoneMatch:*`` and then immutable.
    """

    id: str
    relation: Relation = "supersedes"
    of: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _VERSION_ID_RE.match(self.id):
            raise NamingError(
                f"version id {self.id!r} must be 'v<N>' with N >= 1 (§2: v1, v2, ... "
                f"monotonic per name, auto-allocated)"
            )

        if self.relation not in RELATIONS:
            raise NamingError(
                f"version relation {self.relation!r} is not one of {sorted(RELATIONS)}"
            )

        is_first = self.id == _FIRST_VERSION_ID

        # "`of` must be None iff relation is the first version" — read as: the first
        # version (v1) has no antecedent, and every later version must name the one it
        # supersedes / extends / sits beside. A dangling vN>=1 with of=None would make
        # the relation unfalsifiable, which §0.1 forbids.
        if is_first and self.of is not None:
            raise NamingError(
                f"version {self.id!r} is the first version, so 'of' must be null; "
                f"got {self.of!r}"
            )
        if not is_first and self.of is None:
            raise NamingError(
                f"version {self.id!r} is not the first version, so 'of' must name the "
                f"version it {self.relation}; got null"
            )

        if self.of is not None:
            if not isinstance(self.of, str) or not _VERSION_ID_RE.match(self.of):
                raise NamingError(
                    f"version 'of' must be 'v<N>' with N >= 1 or null; got {self.of!r}"
                )
            if self.of == self.id:
                raise NamingError(
                    f"version {self.id!r} cannot {self.relation} itself"
                )

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "relation": self.relation, "of": self.of}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Version":
        if not isinstance(d, dict):
            raise NamingError(f"version must be an object; got {type(d).__name__}")
        unknown = set(d) - {"id", "relation", "of"}
        if unknown:
            raise NamingError(
                f"version has unknown key(s) {sorted(unknown)}; the block is "
                f"{{id, relation, of}}"
            )
        if "id" not in d:
            raise NamingError("version is missing required key 'id'")
        if "relation" not in d:
            raise NamingError("version is missing required key 'relation'")
        return cls(id=d["id"], relation=d["relation"], of=d.get("of"))
