"""The profile contract.

A profile is a named contract attached to a payload *group* (§4). It does exactly two
things: declares the extra fields that group's metadata must carry, and supplies checks
that RECOMPUTE something against the group's bytes. Nothing else — not a folder, not a
class hierarchy.

The golden rule (CONTRIBUTING.md, §0.4): **a check must recompute, never merely assert a
field is present.** A JSON-schema shape check invites plausible garbage, especially from
coding agents. So the interface is built around handing each check the bytes (via ``s3``)
and the declared claims (via ``group``/``manifest``), and asking it to find the lie.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from ..s3 import S3


@dataclass(frozen=True)
class Violation:
    """One failed claim. ``code`` is a stable kebab-case slug for programmatic handling
    (``count-arithmetic``, ``vocab-out-of-range``); ``message`` explains it to a human and
    should name the concrete failure, not just the rule — the audit's lesson is that a
    finding people can act on beats a finding they have to investigate."""

    code: str
    message: str
    path: str | None = None  # the offending object key, when a single one is implicated

    def __str__(self) -> str:
        loc = f" [{self.path}]" if self.path else ""
        return f"{self.code}{loc}: {self.message}"


@dataclass
class GroupContext:
    """Everything a profile check is handed. Read-only in spirit; a check returns
    Violations, it does not mutate.

    * ``dataset_id`` / ``version`` — identity, already validated by contracts.py.
    * ``landing_bucket`` / ``prefix`` — where the group's bytes live *right now* (landing,
      pre-promotion). All reads go here.
    * ``group`` — the group's entry from ``dataset.json`` (name, profile, prefix, the
      profile-specific metadata block).
    * ``manifest`` — the parsed group ``manifest.json`` (schema_version, entries, objects,
      bytes). Entries are dicts; use ``ManifestEntry.from_dict`` if a check wants the typed
      form.
    * ``s3`` — the access layer. Prefer ``get_range`` for sampling; never load a whole
      shard to inspect a slice of it.
    * ``rng_seed`` — the deterministic seed a check must use for any sampling, so a result
      is reproducible and auditable (§7 decode smoke test). Derived once, per group, by the
      orchestrator: ``sha256(dataset_id|version|group_name)``.
    * ``resolved`` — facts the ORCHESTRATOR derived that a profile cannot compute itself
      because they live outside landing. The load-bearing case: the tokenizer. A profile
      must not typed-trust a tokenizer block; instead the validator loads the tokenizer that
      the dataset ``depends_on`` (from the published data bucket, which the profile can't
      see), computes ``vocab_size``/``eos_token_id`` from its ``tokenizer.json``, and places
      them here as ``resolved["tokenizer"]``. A check reads the bound from here, not from a
      hand-typed field — so the bound is unfakeable.
    * ``observations`` — ephemeral, validator-owned facts computed while checks run.  Unlike
      ``resolved``, these are about landing objects themselves.  A profile may record a
      content hash plus stable ETag here so promotion can condition its later server-side copy
      on the exact object Gate A inspected.  This is never serialized into dataset metadata.
    """

    dataset_id: str
    version: str
    landing_bucket: str
    prefix: str
    group: Mapping[str, Any]
    manifest: Mapping[str, Any]
    s3: S3
    rng_seed: str
    family_defaults: Mapping[str, Any] = field(default_factory=dict)
    resolved: Mapping[str, Any] = field(default_factory=dict)
    observations: dict[str, Any] = field(default_factory=dict)


# A check recomputes something and returns any violations it finds. Empty list = clean.
Check = Callable[[GroupContext], list[Violation]]


class Profile(Protocol):
    """What every ``profiles/<name>_vN.py`` module must expose.

    Kept as a Protocol (not a base class to subclass) so a profile is just a module with
    the right attributes — the lowest-ceremony way to add one, matching CONTRIBUTING.md.
    """

    # e.g. "pretrain-tokens/v1" — the exact string that appears in a group's `profile`.
    NAME: str

    # JSON-schema-ish fragment describing the fields this profile requires in the group's
    # metadata block. Presence/type is checked structurally; VALUES are checked by CHECKS.
    REQUIRED_FIELDS: Mapping[str, Any]

    # The recompute-something checks. Run in order; all run (a check does not short-circuit
    # the rest) so one dataset surfaces all its problems in a single pass.
    CHECKS: list[Check]


# --------------------------------------------------------------------------------------
# Helpers shared by profile checks — sampling and decoding
# --------------------------------------------------------------------------------------

# §7: ~64 KB per shard at seeded offsets. A module-level default so every profile that
# samples uses the same budget unless it has a reason not to.
DECODE_SAMPLE_BYTES = 64 * 1024


def sample_offsets(
    seed_hex: str, object_size: int, *, window: int, n: int, align: int
) -> list[int]:
    """Deterministic byte offsets for a decode smoke test.

    Reproducible from ``seed_hex`` alone (§7: "any auditor can re-run the identical
    sample"). Offsets are aligned down to an ``align``-byte boundary so a token decode
    never straddles an element, and never within ``window`` of EOF so the read returns a
    full window. Random-not-head because a zero-filled or truncated tail leaves a
    correctly-sized file with a valid head — head-only sampling misses it entirely.
    """
    if object_size <= 0 or window <= 0 or n <= 0:
        return []
    hi = object_size - window
    if hi <= 0:
        return [0]  # object smaller than one window: read from the top
    # Deterministic PRNG seeded purely from seed_hex — no Math.random/os.urandom, so the
    # result is a pure function of (dataset, version, group, shard).
    import hashlib

    offsets: list[int] = []
    counter = 0
    while len(offsets) < n:
        h = hashlib.sha256(f"{seed_hex}:{counter}".encode()).digest()
        raw = int.from_bytes(h[:8], "big")
        off = raw % (hi + 1)
        off -= off % align
        offsets.append(off)
        counter += 1
    return sorted(set(offsets)) or [0]


__all__ = [
    "Violation",
    "GroupContext",
    "Check",
    "Profile",
    "DECODE_SAMPLE_BYTES",
    "sample_offsets",
]
