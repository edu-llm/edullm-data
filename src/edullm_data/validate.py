"""Gate A — the validator orchestrator (§7).

The one rule that matters: **recompute, never trust.** Every check here re-derives a value
from bytes or from the manifest and compares it to what ``dataset.json`` *claims*. A schema
that merely confirms a field is present would pass every failure in the audit; these checks
reproduce the number and catch the lie.

Flow (§1 airlock): a dataset is uploaded to ``s3://edullm-landing/<prefix>/``. This module
reads it there, runs Gate A, and — only on a clean pass — :func:`promote` server-side-copies
it into ``s3://edullm-data/<dataset_id>/<version>/`` and writes a catalog entry. Bytes never
transit the client; the validator role is the only principal that can write the data bucket.

Determinism: no ``datetime.now``/``time``/``random`` — timestamps are passed in, seeds are
hashed from identity. A validation run is reproducible from its inputs.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Mapping

from .contracts import (
    FAMILIES,
    _resolve_families_dir,
    SPLITS,
    TRAINABLE_SPLITS,
    is_trainable,
    READABLE_SCHEMA_VERSIONS,
    NamingError,
    SCHEMA_VERSION,
    Version,
    canonical_json,
    sha256_bytes,
    validate_dataset_id,
    validate_purpose,
)
from .manifest import (
    DTYPE_SIZES,
    FIXED_WIDTH_CONTAINERS,
    FIXED_WIDTH_UNITS,
    ManifestEntry,
    build_manifest,
    check_extension_matches_format,
    check_shard_naming,
    diff_paths,
    is_cas_name,
    labels_from_path,
    manifest_sha256,
    parse_shard_name,
    verify_arithmetic,
)
from .profiles import registry
from .profiles.base import GroupContext, Violation
from .s3 import S3, NotFound

# Control files that live under a dataset prefix but are not group payload — excluded from
# the manifest-exhaustiveness comparison so they never read as "extra".
CONTROL_BASENAMES = frozenset(
    {"dataset.json", "manifest.json", "_SUCCESS", "_VALIDATED.json", "_REJECTED.json", "README.md"}
)
CONTROL_PREFIXES = ("_catalog/", "dependents/")

FAMILIES_DIR = _resolve_families_dir()

# Core fields every dataset.json must carry (§3). Profile-specific fields live on the group.
REQUIRED_CORE_FIELDS = (
    "schema_version",
    "dataset_id",
    "version",
    "owner",
    "purpose",
    "mutability",
    "inventory",
    "groups",
    "build",
)
MUTABILITIES = frozenset({"frozen", "append-only", "live"})


@dataclass
class ValidationResult:
    """Outcome of Gate A. ``ok`` means promotable; ``incomplete`` means a frozen dataset is
    still missing a group's manifest (do not promote, but not *invalid* — a re-run after the
    last group seals may pass). ``violations`` is every problem found in one pass; checks do
    not short-circuit each other, so one run surfaces the whole list."""

    dataset_id: str
    version: str
    violations: list[Violation] = field(default_factory=list)
    incomplete: bool = False

    @property
    def ok(self) -> bool:
        return not self.violations and not self.incomplete

    def report(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "incomplete": self.incomplete,
            "dataset_id": self.dataset_id,
            "version": self.version,
            "violations": [
                {"code": v.code, "message": v.message, "path": v.path} for v in self.violations
            ],
        }

    def rejection_doc(self, *, now: str | None = None) -> dict[str, Any]:
        doc = {
            "schema_version": "edullm-rejection/v1",
            "dataset_id": self.dataset_id,
            "version": self.version,
            "incomplete": self.incomplete,
            "violations": [
                {"code": v.code, "message": v.message, "path": v.path} for v in self.violations
            ],
        }
        if now is not None:
            doc["rejected_at"] = now
        return doc


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _join(prefix: str, path: str) -> str:
    """Group-relative manifest path -> full landing key. ``entry.path`` already carries the
    group segment (``tokens/train-00000.u32le.bin``), so it joins onto the dataset prefix."""
    p = prefix.strip("/")
    return f"{p}/{path}" if p else path


def _is_control_key(rel: str) -> bool:
    """Whether a dataset-relative key is a control file rather than payload.

    Control BASENAMES are anchored to the dataset root (or one level down, where a group's
    manifest.json lives). Matching a basename anywhere in the tree let `sneaky/README.md` and
    `sneaky/dataset.json` hide from the exhaustiveness sweep entirely.
    """
    base = rel.rsplit("/", 1)[-1]
    depth = rel.count("/")
    if base in CONTROL_BASENAMES and depth == 0:
        return True
    if base == "manifest.json" and depth == 1:
        return True  # a group's own manifest, e.g. tokens/manifest.json
    return any(rel.startswith(cp) for cp in CONTROL_PREFIXES)


def _load_json(s3: S3, bucket: str, key: str) -> Any:
    return json.loads(s3.get(bucket, key).decode("utf-8"))


# --------------------------------------------------------------------------------------
# the orchestrator
# --------------------------------------------------------------------------------------


def validate_dataset(landing_bucket: str, prefix: str, s3: S3, *, data_bucket: str | None = None) -> ValidationResult:
    """Run Gate A against ``s3://landing_bucket/prefix/``. Reads only via ``s3``; never
    promotes. ``data_bucket`` is only needed if the dataset declares ``depends_on`` (the
    parent is read from the published bucket)."""
    prefix = prefix.strip("/")
    v: list[Violation] = []

    # -- load dataset.json --
    try:
        ds = _load_json(s3, landing_bucket, _join(prefix, "dataset.json"))
    except NotFound:
        return ValidationResult("?", "?", [Violation("no-dataset-json", f"no dataset.json under {prefix!r}")])
    except (ValueError, UnicodeDecodeError) as e:
        return ValidationResult("?", "?", [Violation("dataset-json-unparseable", str(e))])

    dataset_id = str(ds.get("dataset_id", "?"))
    version_id = "?"
    ver = ds.get("version")
    if isinstance(ver, dict):
        version_id = str(ver.get("id", "?"))

    # -- core fields present --
    for f_ in REQUIRED_CORE_FIELDS:
        if f_ not in ds:
            v.append(Violation("missing-core-field", f"dataset.json is missing required field {f_!r}"))

    # READABLE_SCHEMA_VERSIONS, not an equality check against the current one. Gate A re-runs
    # against already-published datasets (the in-place README backfill did exactly that), so an
    # exact match would make every v1 dataset fail the moment the writer moved to v2 — the
    # retroactive invalidation CONTRIBUTING forbids. New datasets are written at SCHEMA_VERSION;
    # older ones remain readable and validatable at the version they were sealed with.
    if ds.get("schema_version") not in READABLE_SCHEMA_VERSIONS:
        v.append(Violation(
            "schema-version",
            f"schema_version is {ds.get('schema_version')!r}, expected one of "
            f"{sorted(READABLE_SCHEMA_VERSIONS)} (new datasets are written at {SCHEMA_VERSION!r})",
        ))

    # -- identity: dataset_id/version valid AND equal to the prefix (§7) --
    try:
        validate_dataset_id(dataset_id)
    except NamingError as e:
        v.append(Violation("bad-dataset-id", str(e)))

    if isinstance(ver, dict):
        try:
            Version.from_dict(ver)
        except (ValueError, NamingError) as e:
            v.append(Violation("bad-version", str(e)))
    else:
        v.append(Violation("bad-version", "version must be an object {id, relation, of}"))

    if "purpose" in ds:
        try:
            validate_purpose(str(ds["purpose"]))
        except NamingError as e:
            v.append(Violation("bad-purpose", str(e)))

    # prefix must equal <dataset_id>/<version_id>
    expected_prefix = f"{dataset_id}/{version_id}"
    if prefix != expected_prefix:
        v.append(Violation(
            "prefix-mismatch",
            f"landing prefix {prefix!r} != dataset_id/version {expected_prefix!r}",
        ))

    mutability = ds.get("mutability")
    if mutability not in MUTABILITIES:
        v.append(Violation("bad-mutability", f"mutability {mutability!r} not in {sorted(MUTABILITIES)}"))

    groups = ds.get("groups")
    if not isinstance(groups, list) or not groups:
        v.append(Violation("no-groups", "dataset.json has no groups[]"))
        return ValidationResult(dataset_id, version_id, v)

    # -- dataset-level: is held-out data present when the family requires it? --
    # Checked across ALL groups, not per group: splits are a property of the dataset, and a
    # multi-group dataset may legitimately keep its val shards in one group.
    _check_validation_present(groups, _family_defaults_for(dataset_id), v, dataset_id)

    # -- per-group validation --
    total_objects = 0
    total_bytes = 0
    all_shas: dict[str, str] = {}  # sha256 -> first path that declared it (dup detection)
    group_manifest_paths: dict[str, set[str]] = {}  # group -> its manifest's paths
    my_shas: set[str] = set()
    incomplete = False

    for group in groups:
        gname = str(group.get("name", "?"))
        # NB: no gprefix here on purpose. The group-prefix LIST it looks like it was meant for
        # already lives inside _validate_group, which derives its own (stripped) gprefix and
        # LISTs `<prefix>/<gprefix>/` for the exhaustiveness diff. A second copy at this level
        # would only be a second source of truth for the same string.
        gres = _validate_group(
            s3, landing_bucket, prefix, dataset_id, version_id, group, v, all_shas, my_shas,
            data_bucket=data_bucket, collect_paths=group_manifest_paths,
        )
        if gres is None:
            # missing manifest: incomplete for frozen, fine otherwise
            if mutability == "frozen":
                incomplete = True
                v_incomplete = Violation(
                    "incomplete-group",
                    f"group {gname!r} has no manifest yet; frozen dataset not sealed",
                    path=gname,
                )
                # tracked separately from invalidity — surfaced but flagged incomplete
                v.append(v_incomplete)
            continue
        gobjs, gbytes = gres
        total_objects += gobjs
        total_bytes += gbytes

    # -- dataset-level sweep: orphan prefixes + observed-vs-declared splits --
    # After the group loop, so every group's manifest paths have been collected. A group-scoped
    # LIST cannot see an object under a top-level prefix that belongs to no declared group.
    if not incomplete:
        _check_dataset_exhaustive_and_splits(
            s3, landing_bucket, prefix, groups, group_manifest_paths, v
        )

    # -- inventory: declared totals equal recomputed sums (§7 — the inventory.json bug) --
    inv = ds.get("inventory") or {}
    if not incomplete:
        if int(inv.get("objects", -1)) != total_objects:
            v.append(Violation(
                "inventory-objects",
                f"inventory.objects={inv.get('objects')} but recomputed {total_objects} across groups",
            ))
        if int(inv.get("bytes", -1)) != total_bytes:
            v.append(Violation(
                "inventory-bytes",
                f"inventory.bytes={inv.get('bytes')} but recomputed {total_bytes} across groups",
            ))

    result = ValidationResult(dataset_id, version_id, v, incomplete=incomplete)
    # if the only violations are the incomplete markers, report incomplete (not invalid)
    if incomplete and all(x.code == "incomplete-group" for x in v):
        result.violations = [x for x in v if x.code != "incomplete-group"]
    return result


def _validate_group(
    s3: S3,
    landing_bucket: str,
    prefix: str,
    dataset_id: str,
    version_id: str,
    group: Mapping[str, Any],
    v: list[Violation],
    all_shas: dict[str, str],
    my_shas: set[str],
    *,
    data_bucket: str | None,
    collect_paths: dict[str, set[str]] | None = None,
) -> tuple[int, int] | None:
    """Validate one group. Returns (objects, bytes) or None if the group's manifest is
    absent (incomplete). Appends Violations to ``v`` in place."""
    gname = str(group.get("name", "?"))
    gprefix = str(group.get("prefix", "")).strip("/")
    profile_name = group.get("profile")

    manifest_rel = group.get("manifest") or (f"{gprefix}/manifest.json" if gprefix else "manifest.json")
    try:
        manifest = _load_json(s3, landing_bucket, _join(prefix, manifest_rel))
    except NotFound:
        return None
    except (ValueError, UnicodeDecodeError) as e:
        v.append(Violation("manifest-unparseable", f"group {gname!r}: {e}", path=gname))
        return (0, 0)

    # -- hash chain: recompute manifest_sha256, compare to the group's declared value (§7) --
    recomputed = manifest_sha256(manifest)
    declared = group.get("manifest_sha256")
    if declared != recomputed:
        v.append(Violation(
            "manifest-sha256-mismatch",
            f"group {gname!r}: declared manifest_sha256={declared!r} but recomputed {recomputed!r}",
            path=gname,
        ))

    raw_entries = manifest.get("entries", [])
    entries: list[ManifestEntry] = []
    for re_ in raw_entries:
        try:
            entries.append(ManifestEntry.from_dict(re_))
        except (ValueError, KeyError) as e:
            v.append(Violation("bad-manifest-entry", f"group {gname!r}: {e}", path=gname))

    # -- manifest bytes/objects self-consistency (recompute from entries) --
    rebuilt = build_manifest(entries, group_name=gname) if entries else {"objects": 0, "bytes": 0}
    if manifest.get("objects") != rebuilt["objects"]:
        v.append(Violation("manifest-objects", f"group {gname!r}: manifest objects={manifest.get('objects')} != {rebuilt['objects']}", path=gname))
    if manifest.get("bytes") != rebuilt["bytes"]:
        v.append(Violation("manifest-bytes", f"group {gname!r}: manifest bytes={manifest.get('bytes')} != {rebuilt['bytes']}", path=gname))

    # Shard-naming (<split>-<NNNNN>) is exempt for profiles whose files aren't shards:
    # vendored trees keep upstream names; a tokenizer's files have fixed meaningful names
    # (tokenizer.json, merges.txt, …); CAS objects are named by hash (handled per-entry).
    profile_is_vendored = isinstance(profile_name, str) and (
        profile_name.startswith("vendored/") or profile_name.startswith("tokenizer/")
    )

    # -- register depends_on parent shas FIRST, so the per-entry loop can catch a child
    #    shard that re-materializes a parent's bytes (the 37 GB duplication) --
    _register_parent_shas(s3, data_bucket, group, ds_depends=group.get("depends_on"), all_shas=all_shas, v=v, gname=gname)

    # -- per-entry: HEAD size, arithmetic, extension honesty, shard naming, dup digests --
    seen_sha: set[str] = set()
    for entry in entries:
        full_key = _join(prefix, entry.path)
        try:
            head = s3.head(landing_bucket, full_key)
        except NotFound:
            v.append(Violation("missing-object", f"manifest lists {entry.path!r} but it is absent", path=entry.path))
            continue
        if head["size"] != entry.bytes:
            v.append(Violation(
                "head-size-mismatch",
                f"{entry.path}: manifest bytes={entry.bytes} but S3 size={head['size']}",
                path=entry.path,
            ))
        for msg in verify_arithmetic(entry):
            v.append(Violation("count-arithmetic", msg, path=entry.path))
        for msg in check_extension_matches_format(entry.path, entry.format):
            v.append(Violation("extension-format-mismatch", msg, path=entry.path))
        for msg in check_shard_naming(entry.path, exempt=is_cas_name(entry.path) or profile_is_vendored):
            v.append(Violation("shard-naming", msg, path=entry.path))

        # pairwise-distinct digests within the group (byte-identical duplicated shard)
        if entry.sha256 in seen_sha:
            v.append(Violation("duplicate-shard-digest", f"{entry.path}: sha256 already used by another shard in this group", path=entry.path))
        seen_sha.add(entry.sha256)

        # cross-dataset dup vs depends_on parents (the 37 GB re-materialization)
        if entry.sha256 in all_shas and all_shas[entry.sha256].startswith("PARENT:"):
            v.append(Violation(
                "shared-sha-with-parent",
                f"{entry.path}: sha256 also appears in depends_on parent {all_shas[entry.sha256][7:]} — reference it, do not copy",
                path=entry.path,
            ))
        all_shas.setdefault(entry.sha256, entry.path)
        my_shas.add(entry.sha256)

    # -- manifest EXHAUSTIVE: LIST the group prefix, compare both directions (§5) --
    list_prefix = _join(prefix, gprefix + "/") if gprefix else prefix + "/"
    listed = s3.list(landing_bucket, list_prefix)
    actual_rel: set[str] = set()
    for obj in listed:
        rel_to_dataset = obj["key"][len(prefix) + 1 :] if obj["key"].startswith(prefix + "/") else obj["key"]
        if _is_control_key(rel_to_dataset):
            continue
        actual_rel.add(rel_to_dataset)
    manifest_paths = {e.path for e in entries}
    if collect_paths is not None:
        collect_paths[gname] = manifest_paths
    missing, extra = diff_paths(manifest_paths, actual_rel)
    for m in sorted(missing):
        v.append(Violation("missing-object", f"manifest lists {m!r} but it is not in S3", path=m))
    for x in sorted(extra):
        v.append(Violation("unlisted-object", f"{x!r} is in S3 but not in the manifest (a globbing reader would train on it)", path=x))

    # -- partitions (§7): every partition declares rows; structural check of the four forms --
    _validate_partitions(group, v, gname, manifest_paths, entries)

    # -- profile CHECKS (recompute against bytes) --
    if profile_name is None:
        v.append(Violation("no-profile", f"group {gname!r} declares no profile", path=gname))
    else:
        try:
            profile = registry.get_profile(profile_name)
        except registry.ProfileError as e:
            v.append(Violation("unknown-profile", str(e), path=gname))
        else:
            rng_seed = hashlib.sha256(f"{dataset_id}|{version_id}|{gname}".encode()).hexdigest()
            # Resolve facts the profile can't compute itself because they live in the data
            # bucket (which the profile deliberately can't see). The tokenizer is the case
            # that matters: derive vocab_size/eos_token_id from the tokenizer this group
            # depends_on, so the decode-smoke bound is computed from real bytes, not typed.
            resolved: dict[str, Any] = {}
            tok_derived = _resolve_tokenizer(s3, data_bucket, group, v, gname)
            if tok_derived is not None:
                resolved["tokenizer"] = tok_derived
            # The dtype width is DERIVED from the vocab, never trusted from the manifest.
            # This is the one check that catches a dtype NARROWING lie, and nothing else
            # can: verify_arithmetic is tautological on a manifest publish() built (count is
            # computed FROM size using the same dtype it is later checked against), the
            # extension check is self-consistent with the lie, and the decode smoke test only
            # ever sees ids that are in range. Narrowing is also the dangerous direction — it
            # INFLATES the declared token count, silently changing the training budget.
            #
            # Prefer the derived vocab, but fall back to a declared `tokenizer` block the same
            # way the pretrain profile does: a dataset that predates the depends_on convention
            # should still get the check rather than silently skip it.
            tok_for_width = tok_derived if tok_derived else group.get("tokenizer")
            if isinstance(tok_for_width, Mapping):
                _check_dtype_width_vs_vocab(entries, tok_for_width, v, gname)
            # A declared split must agree with the filename it was derived from.
            _check_split_matches_filename(
                entries, v, gname, profile_is_vendored=profile_is_vendored
            )
            _check_labels_match_path(
                entries, v, gname, profile_is_vendored=profile_is_vendored
            )
            # prefix is the DATASET prefix, not the group prefix: entry.path already carries
            # the group segment (tokens/train-00000...), so a profile joins prefix+entry.path.
            ctx = GroupContext(
                dataset_id=dataset_id,
                version=version_id,
                landing_bucket=landing_bucket,
                prefix=prefix,
                group=group,
                manifest=manifest,
                s3=s3,
                rng_seed=rng_seed,
                family_defaults=_family_defaults_for(dataset_id),
                resolved=resolved,
            )
            for check in profile.CHECKS:
                try:
                    v.extend(check(ctx))
                except Exception as e:  # noqa: BLE001 - a check bug must not crash the gate
                    v.append(Violation("profile-check-error", f"group {gname!r} check {getattr(check,'__name__','?')}: {e}", path=gname))

    return (rebuilt["objects"], rebuilt["bytes"])


#: Family ``defaults.decode_smoke_test.<key>`` -> the flat key a profile's ``_bound()`` reads.
#: These two vocabularies drifted apart: the family nests its bounds under
#: ``decode_smoke_test`` and names them ``<thing>_<bound>``, while every profile reads a
#: top-level ``<bound>_<thing>``. Both halves were wrong at once, so wiring family defaults
#: through without this mapping would still have resolved nothing.
#: ``window_bytes`` is deliberately ABSENT. The family files declare it, but the decode window
#: is the hard constant ``profiles.base.DECODE_SAMPLE_BYTES`` (64 KiB) and no profile reads a
#: configurable value — so aliasing it would flatten a key nobody enforces, which is the
#: decoration this module exists to remove. ``_check_alias_map_covers_family_keys`` asserts the
#: gap is deliberate rather than forgotten.
_DECODE_BOUND_ALIASES = {
    "distinct_ids_min": "min_distinct_ids",
    "eos_fraction_max": "max_eos_fraction",
    "zero_run_max": "max_zero_run",
}

#: Family ``decode_smoke_test`` keys that intentionally map to nothing, with the reason. Keeping
#: this explicit means a NEW unmapped key is a test failure rather than a silent no-op.
#: Units whose values may be summed into a partition row count. Superset of
#: manifest.FIXED_WIDTH_UNITS ({tokens, indices}) plus the line-oriented units.
_COUNTABLE_UNITS = FIXED_WIDTH_UNITS | {"rows", "items"}

_DECODE_BOUNDS_NOT_ENFORCED = {
    "window_bytes": "the decode window is the fixed DECODE_SAMPLE_BYTES constant, not tunable",
}


def _family_defaults_for(dataset_id: str) -> dict[str, Any]:
    """The family's ``defaults`` block, flattened into the keys profiles actually read.

    Returns ``{}`` rather than raising when ``families/`` is not on disk: the wheel ships
    only ``src/edullm_data``, so a Batch validator has no families directory (see CLAUDE.md).
    A profile falls back to its own conservative constant in that case, which is the same
    behaviour as before this was wired up — never a crash, and never a *laxer* bound than the
    family asks for.
    """
    family_name = str(dataset_id).split("/", 1)[0]
    if family_name not in FAMILIES:
        return {}
    path = FAMILIES_DIR / f"{family_name}.json"
    try:
        family = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    defaults = family.get("defaults")
    if not isinstance(defaults, Mapping):
        return {}
    flat: dict[str, Any] = {k: val for k, val in defaults.items() if k != "decode_smoke_test"}
    smoke = defaults.get("decode_smoke_test")
    if isinstance(smoke, Mapping):
        for fam_key, profile_key in _DECODE_BOUND_ALIASES.items():
            if fam_key in smoke:
                flat[profile_key] = smoke[fam_key]
    return flat


def _check_dataset_exhaustive_and_splits(
    s3: S3,
    landing_bucket: str,
    prefix: str,
    groups: list[Any],
    group_manifest_paths: dict[str, set[str]],
    v: list[Violation],
) -> None:
    """LIST the whole DATASET prefix and reconcile it, then recompute observed splits.

    Two holes closed at once, both of which needed a dataset-wide view:

    **V8 — orphan prefixes.** The per-group exhaustiveness check LISTs each group's own prefix,
    so an object under a top-level prefix belonging to NO declared group is listed by nobody and
    reconciled against nothing. An injected ``sneaky/val-00000.u32le.bin`` passed Gate A clean.
    ``promote()`` only copies manifest-listed keys, so such an object never reaches the data
    bucket — but "the manifest is exhaustive in both directions" held only *within* groups, not
    across the dataset, and that is a weaker claim than the standard makes.

    **The silent split hole.** A shard named ``val-00000.u32le.bin`` in a group with no declared
    ``val`` partition validated clean, was invisible to ``split="val"``, and — before the reader
    fix — was still returned by an unsplit read. So you could train on your validation data with
    nothing anywhere objecting. The split word is recomputed from each object's own filename via
    ``parse_shard_name`` and compared, in BOTH directions, against the declared partitions.

    ERROR rather than warning, for the same reason ``unlisted-object`` is an error: a warning
    does not stop ``promote()``, so it would publish a dataset nobody can trust. And in landing
    the fix is free — rename the shard or declare the partition, while the bytes are still
    mutable.
    """
    # A group whose profile EXEMPTS it from shard naming says nothing about splits through its
    # filenames. families/vendor.json grants that exemption on purpose — renaming an upstream
    # mirror destroys the byte-for-byte correspondence that makes it verifiable — and a
    # tokenizer's files (tokenizer.json, merges.txt) are not shards at all. Applying the split
    # sweep to them revokes an exemption the standard deliberately gives, so a vendored
    # `test-00000.parquet` was being rejected for keeping its own name.
    exempt_prefixes: list[str] = []
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        prof = str(group.get("profile", ""))
        if prof.startswith(("vendor/", "tokenizer/")):
            gpfx = str(group.get("prefix") or group.get("name") or "").strip("/")
            if gpfx:
                exempt_prefixes.append(gpfx + "/")

    claimed: set[str] = set()
    for paths in group_manifest_paths.values():
        claimed |= paths

    # If a group's manifest never parsed, `collect_paths` has no entry for it and its payload
    # would read as dataset-level orphans — reporting healthy objects from an unrelated group as
    # stray. Only reconcile groups we actually managed to read.
    unread = [str(g.get("name")) for g in groups
              if isinstance(g, Mapping) and str(g.get("name")) not in group_manifest_paths]

    listed = s3.list(landing_bucket, prefix + "/")
    observed: dict[str, list[str]] = {}
    orphans: list[str] = []
    for obj in listed:
        key = obj["key"]
        rel = key[len(prefix) + 1:] if key.startswith(prefix + "/") else key
        if _is_control_key(rel):
            continue
        if rel not in claimed:
            orphans.append(rel)
        if any(rel.startswith(px) for px in exempt_prefixes):
            continue  # vendored / tokenizer group: its filenames carry no split claim
        parsed = parse_shard_name(rel)
        # Only words in the SPLIT VOCABULARY are split claims. ``SHARD_RE`` matches any
        # ``<word>-NNNNN.<ext>`` name, so an eval dataset's ``results/eval-00000.jsonl`` parses
        # as a "split" called ``eval`` — a shard-naming convention, not a split declaration. The
        # vocabulary is closed precisely so this distinction is a lookup.
        if parsed is not None and parsed[0] in SPLITS and not is_cas_name(rel):
            observed.setdefault(parsed[0], []).append(rel)

    for rel in sorted(orphans) if not unread else []:
        v.append(Violation(
            "unlisted-object-dataset-level",
            f"{rel!r} is under the dataset prefix but is in no group's manifest. It belongs to "
            f"no declared group, so no group's exhaustiveness check ever looked at it — a "
            f"globbing reader would still find it.",
            path=rel,
        ))

    declared: set[str] = set()
    for group in groups:
        for part in (group.get("partitions") or []):
            if isinstance(part, Mapping) and part.get("name"):
                declared.add(str(part["name"]))
    # NOT an early return when nothing is declared. If objects on disk carry split-shaped names
    # and no partition claims them, that is exactly the undeclared-split condition — and
    # declaring nothing used to switch this whole sweep off (see _check_validation_present).
    for split_word, paths in sorted(observed.items()):
        if split_word not in declared:
            v.append(Violation(
                "undeclared-split",
                f"{len(paths)} object(s) are named {split_word!r}-NNNNN.* but no partition "
                f"declares a split called {split_word!r}. They are unreachable via "
                f"split={split_word!r} AND (before the reader's trainable-only default) were "
                f"returned by an unsplit read — trainable by accident. Declare the split or "
                f"rename the shards. First: {sorted(paths)[0]}",
                path=sorted(paths)[0],
            ))
    for split_word in sorted(declared - set(observed)):
        v.append(Violation(
            "empty-split",
            f"partition {split_word!r} is declared but no object is named "
            f"{split_word!r}-NNNNN.*; a reader asking for it gets silence",
            path=split_word,
        ))


def _check_validation_present(
    groups: list[Any], family_defaults: Mapping[str, Any], v: list[Violation], dataset_id: str
) -> None:
    """A dataset must carry held-out data unless its family explicitly opts out.

    Opt-OUT, not opt-in, and that polarity is the whole design. Under opt-in, a corpus with no
    validation split is indistinguishable from one where nobody thought about it — and the
    second is a mistake you find out about weeks later, from a suspiciously good eval. Under
    opt-out, "this dataset has no held-out data" becomes a claim some family file states with a
    reason attached, which a reviewer can disagree with.

    Four families legitimately have nothing to hold out and say so in their own file: ``eval``
    and ``probe`` are held out in their entirety (anything trained on stops being a probe),
    ``tokenizer`` is a model artifact with no rows, and ``vendor`` keeps upstream's structure
    byte-for-byte.
    """
    if family_defaults.get("validation_required") is not True:
        return
    declared: set[str] = set()
    for group in groups:
        parts = group.get("partitions")
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, Mapping) and part.get("name"):
                    declared.add(str(part["name"]))
    if not declared:
        # DECLARING NOTHING MUST NOT BE A WAY OUT. This used to return, deferring to a
        # "partitions-required rule" that was never written — so `partitions: null` (which is
        # families/curriculum.json's own default) disabled this check, disabled the
        # undeclared-split backstop, and then made the reader see no trainable split and return
        # EVERYTHING. An ordinary curriculum publish leaked its val shards as trainable with no
        # adversarial input at all, while `.val` reported None.
        v.append(Violation(
            "missing-required-split",
            f"{dataset_id} declares no partitions at all, so nothing marks which objects are "
            f"held out. Its family requires validation data. Declaring nothing is not an "
            f"exemption: a reader cannot distinguish 'no held-out data' from 'held-out data "
            f"nobody labelled', and it resolves that ambiguity by treating everything as "
            f"trainable. Declare train and {sorted(SPLITS - TRAINABLE_SPLITS)} partitions, or "
            f"set validation_required=false in families/<family>.json with a reason.",
        ))
        return
    held_out = {s for s in declared if not is_trainable(s)}
    if held_out:
        return
    v.append(Violation(
        "missing-required-split",
        f"{dataset_id} declares splits {sorted(declared)} but none of them is held out. Its "
        f"family requires validation data: a corpus you cannot measure held-out loss on cannot "
        f"support any claim about the model trained on it. Add a {sorted(SPLITS - TRAINABLE_SPLITS)} "
        f"split (name the shards e.g. 'val-00000.<ext>' and declare a matching partition), or — "
        f"if this family genuinely has nothing to hold out — set validation_required=false in "
        f"families/<family>.json with a reason. NOTE: pretrain/olmo-mix-1124-31b/v1 is EXPECTED "
        f"to fail this; it predates the rule, is frozen (so it cannot gain a split in place), and "
        f"is slated for replacement.",
    ))


def _check_split_matches_filename(
    entries: list[Any], v: list[Violation], gname: str, *, profile_is_vendored: bool = False
) -> None:
    """A declared ``split`` must equal the split RECOMPUTED from the object's own filename.

    This is what makes the field unfakeable, and it is the reason a profile may trust it.
    ``parse_shard_name`` has always returned the split word — the gate simply threw it away
    (it called ``check_shard_naming`` for the pattern and dropped the parse). So the fix is to
    stop discarding a value the code already computed.

    Silent on an entry with no declared split (a v1 manifest, a tokenizer file, a vendored
    blob) and on a name that does not parse as a shard: those are handled by
    ``check_shard_naming`` and by the missing-required-split check, not here. This check has
    exactly one job — catch a declaration that contradicts the bytes' own name.
    """
    if profile_is_vendored:
        return  # vendored trees keep upstream names, so a filename implies nothing about split
    for entry in entries:
        declared = getattr(entry, "split", None)
        if declared is None:
            continue
        if is_cas_name(entry.path):
            continue  # a content-addressed name carries no split by construction
        parsed = parse_shard_name(entry.path)
        if parsed is None:
            continue
        observed = parsed[0]
        if observed != declared:
            v.append(Violation(
                "split-contradicts-filename",
                f"{entry.path}: manifest declares split={declared!r} but the filename says "
                f"{observed!r}. One of the two is wrong, and a reader that trusts the manifest "
                f"would put this object in the wrong split — training on held-out data, or "
                f"evaluating on data it was trained on.",
                path=entry.path,
            ))


def _check_labels_match_path(
    entries: list[Any], v: list[Violation], gname: str, *, profile_is_vendored: bool = False
) -> None:
    """Declared ``labels`` must equal the labels RECOMPUTED from the object's own key.

    The same construction as :func:`_check_split_matches_filename`, for the same reason: a
    label nothing recomputes is a producer assertion, and a consumer slicing a corpus by
    ``source`` would silently train on the wrong mixture. Here the recompute is free — the
    directory segments between the group and the basename ARE the claim, so the check is a
    string comparison against a value the key already carries.

    Deliberately one-directional about absence. An entry with no labels is silent: a flat
    layout has no segments to describe, and v1 manifests predate the field. What is rejected
    is a label that CONTRADICTS the key, or a nested key whose labels were omitted — because
    then a reader partitioning on labels would drop the object from every slice.
    """
    if profile_is_vendored:
        return  # vendored trees keep upstream layout; segments imply nothing about slices
    for entry in entries:
        if is_cas_name(entry.path):
            continue  # a hash-named object sits in no meaningful subtree
        try:
            expected = labels_from_path(entry.path)
        except Exception as e:  # noqa: BLE001 - a deeper tree than we can name
            v.append(Violation("labels-unnameable-path", f"{entry.path}: {e}", path=entry.path))
            continue
        declared = getattr(entry, "labels", None) or {}
        if not expected and not declared:
            continue
        if declared != expected:
            v.append(Violation(
                "labels-contradict-path",
                f"{entry.path}: manifest declares labels={declared or {}} but the key's own "
                f"segments say {expected}. The path and the label disagree about which slice "
                f"this object belongs to, so any mixture computed from labels is wrong.",
                path=entry.path,
            ))


def _min_dtype_size_for_vocab(vocab_size: int) -> int:
    """Smallest unsigned width, in bytes, that can represent every id in [0, vocab_size).

    Mirrors ``numpy.min_scalar_type(vocab_size - 1).itemsize`` without importing numpy, so
    this module stays importable in a metadata-only environment (see ``read.py``'s docstring
    for why that constraint exists). dolma derives the write dtype the same way and refuses
    to let a caller override it — this restores that guarantee on the read side.
    """
    highest = max(int(vocab_size) - 1, 0)
    for size in (1, 2, 4, 8):
        if highest <= (1 << (8 * size)) - 1:
            return size
    return 8


def _check_dtype_width_vs_vocab(
    entries: list[ManifestEntry], tok_derived: Mapping[str, Any], v: list[Violation], gname: str
) -> None:
    """Every fixed-width shard's dtype must be wide enough for the tokenizer's vocab.

    One-sided on purpose. Declaring a WIDER dtype than strictly necessary is legal (it wastes
    space but reads correctly), so only too-narrow is a violation. Too-narrow is impossible to
    read correctly and is exactly the lie every other check misses.
    """
    vocab_size = tok_derived.get("vocab_size")
    # `isinstance(True, int)` is True in Python, so exclude bool explicitly — the same idiom
    # pretrain_tokens_v1 uses for its tokenizer fields. Without it, vocab_size=True yields
    # required=1 and the check silently passes everything.
    if isinstance(vocab_size, bool) or not isinstance(vocab_size, int) or vocab_size <= 0:
        return  # nothing derived to compare against; _resolve_tokenizer already flagged it
    required = _min_dtype_size_for_vocab(vocab_size)
    for entry in entries:
        fmt = entry.format
        declared = fmt.dtype_size
        unit_for_scope = entry.count.get("unit") if isinstance(entry.count, Mapping) else None
        # A dtype NAME the standard does not know sizes for is not "nothing to check" — it is a
        # width nobody can verify, and skipping silently is how the whole lie gets through.
        # `Format.dtype_size` is `DTYPE_SIZES.get(dtype)`, an 8-entry map, while numpy happily
        # accepts aliases like "u2" and "<u2" for uint16. Declaring one of those made this check
        # AND verify_arithmetic (which returns early on dtype_size=None) both skip, so a uint32
        # corpus could claim half-width and ship a 2x-inflated token count.
        if fmt.dtype is not None and declared is None:
            v.append(Violation(
                "dtype-not-checkable",
                f"{entry.path}: dtype {fmt.dtype!r} is not one of {sorted(DTYPE_SIZES)}, so its "
                f"width cannot be verified against the tokenizer's vocab — and the count "
                f"arithmetic cannot be checked either. Use the canonical name (e.g. 'uint32', "
                f"not 'u4' or '<u4'); an alias no gate can size is indistinguishable from a lie.",
                path=entry.path,
            ))
            continue
        # A fixed-width dtype declared inside a container that is NOT byte-addressable raw is a
        # contradiction, and it used to route around the width check entirely (e.g.
        # container: "memmap" or "raw " with a trailing space).
        if declared and fmt.container not in FIXED_WIDTH_CONTAINERS:
            if unit_for_scope in FIXED_WIDTH_UNITS:
                v.append(Violation(
                    "fixed-width-dtype-in-nonraw-container",
                    f"{entry.path}: declares a fixed-width dtype {fmt.dtype!r} and a "
                    f"{unit_for_scope} count, but container is {fmt.container!r}, not one of "
                    f"{sorted(FIXED_WIDTH_CONTAINERS)}. Token width is only meaningful for a raw "
                    f"byte-addressable array, so this combination cannot be verified.",
                    path=entry.path,
                ))
            continue
        if not declared:
            continue  # jsonl/tar and friends genuinely have no token width to check
        # Only entries whose count is in TOKEN units are token arrays. A group can legitimately
        # carry a fixed-width sidecar that is NOT tokens — float16 activations, a uint8 blob —
        # and a *vocab* bound says nothing about those. `verify_arithmetic` scopes itself the
        # same way (manifest.py: `unit not in FIXED_WIDTH_UNITS`), so this mirrors it rather
        # than inventing a second notion of "is this a token array".
        if unit_for_scope not in FIXED_WIDTH_UNITS:
            continue
        if declared < required:
            v.append(Violation(
                "dtype-too-narrow-for-vocab",
                f"{entry.path}: declared dtype {fmt.dtype!r} is {declared} bytes, but the "
                f"tokenizer this group depends_on has vocab_size={vocab_size}, which needs "
                f"at least {required} bytes per token. A {declared}-byte read of these bytes "
                f"cannot represent every id, and the declared count "
                f"({entry.count['value']:,}) "
                f"is inflated by {required // declared}x. Arithmetic and extension checks "
                f"CANNOT catch this — they are self-consistent with the wrong dtype.",
                path=entry.path,
            ))


def _matches_glob(path: str, glob: str) -> bool:
    """Basename first, then the full manifest-relative path — the same order the reader uses,
    so a partition that resolves at validation resolves identically at read time."""
    return fnmatch.fnmatch(path.rsplit("/", 1)[-1], glob) or fnmatch.fnmatch(path, glob)


def _validate_partitions(
    group: Mapping[str, Any],
    v: list[Violation],
    gname: str,
    manifest_paths: set[str],
    entries: list[Any] | None = None,
) -> None:
    parts = group.get("partitions")
    if parts is None:
        return
    if not isinstance(parts, list):
        v.append(Violation("bad-partitions", f"group {gname!r}: partitions must be a list", path=gname))
        return
    coverage = group.get("coverage")
    if coverage not in {"partition", "overlapping", "incomplete"}:
        v.append(Violation("bad-coverage", f"group {gname!r}: coverage {coverage!r} not in partition|overlapping|incomplete", path=gname))

    by_name: dict[str, set[str]] = {}  # partition name -> the paths it selects
    for p in parts:
        if not isinstance(p, Mapping):
            v.append(Violation("bad-partitions", f"group {gname!r}: a partition must be an object, got {type(p).__name__}", path=gname))
            continue
        pname = str(p.get("name", "?"))
        if "rows" not in p:
            v.append(Violation("partition-no-rows", f"group {gname!r} partition {pname!r} declares no rows", path=gname))
        by = p.get("by")
        if by not in {"path", "field", "range", "indices"}:
            v.append(Violation("bad-partition-form", f"partition {pname!r} has by={by!r}", path=gname))
        elif by == "path":
            glob = p.get("glob", "")
            matched = {mp for mp in manifest_paths if _matches_glob(mp, glob)}
            by_name[pname] = matched
            if not matched:
                v.append(Violation("partition-glob-empty", f"partition {pname!r} glob {glob!r} matches no manifest path", path=gname))
            elif entries is not None:
                _check_partition_rows(p, pname, matched, entries, v, gname)
        # field/range/indices scans are a v1 TODO — a profile check reads the bytes.

    if coverage == "partition" and by_name:
        _check_coverage_is_a_partition(by_name, manifest_paths, v, gname)
    # TRAIN/HELD-OUT LEAKAGE IS AN ERROR UNDER EVERY COVERAGE MODE. `overlapping` exists so
    # curriculum replay can revisit the same shards across trainable partitions — it is not a
    # licence for a trainable and a held-out partition to share objects. Declaring
    # `coverage: "overlapping"` with train and val both globbing `*` used to make the two
    # identical sets and validate clean, which is 100% leakage waived by one word.
    if by_name:
        _check_no_trainable_heldout_overlap(by_name, v, gname)


def _check_partition_rows(
    part: Mapping[str, Any],
    pname: str,
    matched: set[str],
    entries: list[Any],
    v: list[Violation],
    gname: str,
) -> None:
    """RECOMPUTE ``rows`` from the selected entries' own counts and compare to the claim.

    Previously ``rows`` was required to be present and never checked, so ``rows: 999999999`` on
    a 60,000-token group passed clean — and ``read.dataset_paths`` hands that number straight to
    a trainer as ``ResolvedSplit.rows``. Under "recompute, never trust", a required-but-unchecked
    field is exactly the decoration the standard warns about.

    Pure metadata arithmetic: the per-entry counts were themselves derived from object sizes, so
    this reads no payload bytes.
    """
    declared = part.get("rows")
    if declared is None or isinstance(declared, bool) or not isinstance(declared, int) or declared < 0:
        # A VIOLATION, not a silent return. `if "rows" not in p` is satisfied by an explicit
        # `rows: null`, and returning early here meant that shape passed Gate A and reached a
        # trainer as `ResolvedSplit.rows = None` — a presence check and a value check that each
        # assumed the other would catch it.
        if "rows" in part:
            v.append(Violation(
                "partition-bad-rows",
                f"group {gname!r} partition {pname!r} declares rows={declared!r}, which is not a "
                f"non-negative integer. An explicit null satisfies the presence check and then "
                f"reaches a trainer as an unknown split size.",
                path=gname,
            ))
        return
    actual = 0
    countable = False
    for e in entries:
        if e.path not in matched:
            continue
        # The same unit set manifest.verify_arithmetic uses, plus "rows"/"items" for
        # jsonl-shaped groups. Previously this list omitted "indices", so a legal
        # token-order partition skipped the recompute entirely.
        if e.count and e.count.get("unit") in _COUNTABLE_UNITS:
            actual += int(e.count["value"])
            countable = True
    if not countable:
        return  # nothing in this partition declares an honest count; §5 allows that
    if actual != declared:
        v.append(Violation(
            "partition-rows-mismatch",
            f"group {gname!r} partition {pname!r} declares rows={declared:,} but the "
            f"{len(matched)} object(s) it selects sum to {actual:,} "
            f"({declared - actual:+,}). A trainer reads this number as the split's size.",
            path=gname,
        ))


def _check_no_trainable_heldout_overlap(
    by_name: dict[str, set[str]], v: list[Violation], gname: str
) -> None:
    """No object may belong to both a trainable and a held-out partition, ever.

    Independent of ``coverage`` on purpose. ``coverage: "overlapping"`` is a statement about
    replay — the same shards legitimately appearing in several *trainable* orderings — and it
    must not double as permission for train and val to be the same bytes. That is the one
    overlap no research claim survives.
    """
    trainable = {n for n in by_name if is_trainable(n)}
    held_out = {n for n in by_name if n in SPLITS and not is_trainable(n)}
    for t in sorted(trainable):
        for h in sorted(held_out):
            shared = by_name[t] & by_name[h]
            if shared:
                v.append(Violation(
                    "train-heldout-leakage",
                    f"group {gname!r}: partition {t!r} (trainable) and {h!r} (held out) both "
                    f"select {len(shared)} object(s), e.g. {sorted(shared)[0]}. Every number "
                    f"produced from a model trained on this is meaningless — it was evaluated "
                    f"on data it trained on. This is an error under EVERY coverage mode; "
                    f"'overlapping' waives replay between trainable partitions, not this.",
                    path=gname,
                ))


def _check_coverage_is_a_partition(
    by_name: dict[str, set[str]], manifest_paths: set[str], v: list[Violation], gname: str
) -> None:
    """``coverage: "partition"`` claims the splits are disjoint AND cover everything. Check it.

    Only the word was validated before — that it was one of partition|overlapping|incomplete —
    so a group could declare ``"partition"`` with two identical globs and pass, which makes
    summing partition rows double-count. ``coverage`` exists precisely to tell a tool whether
    that sum is legitimate, so an unenforced value is worse than none.

    ``overlapping`` skips the disjointness half by design (curriculum replay legitimately
    revisits the same shards); ``incomplete`` skips both.
    """
    names = sorted(by_name)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = by_name[a] & by_name[b]
            if shared:
                v.append(Violation(
                    "coverage-not-disjoint",
                    f"group {gname!r} declares coverage='partition' but partitions {a!r} and "
                    f"{b!r} both select {len(shared)} object(s), e.g. {sorted(shared)[0]}. "
                    f"Summing partition rows would double-count. Use coverage='overlapping' if "
                    f"the overlap is intended — but if {a!r} is trainable and {b!r} is held out, "
                    f"this is train/test leakage.",
                    path=gname,
                ))
    selected = set().union(*by_name.values()) if by_name else set()
    unclaimed = manifest_paths - selected
    if unclaimed:
        v.append(Violation(
            "coverage-incomplete",
            f"group {gname!r} declares coverage='partition' but {len(unclaimed)} object(s) "
            f"belong to no partition, e.g. {sorted(unclaimed)[0]}. They are invisible to every "
            f"split= read yet still counted in the group's totals. Declare a partition that "
            f"covers them, or use coverage='incomplete' to say so on purpose.",
            path=gname,
        ))


def _register_parent_shas(s3, data_bucket, group, ds_depends, all_shas, v, gname) -> None:
    if not ds_depends:
        return
    if data_bucket is None:
        v.append(Violation("depends-on-no-data-bucket", f"group {gname!r} has depends_on but no data_bucket to resolve it", path=gname))
        return
    for dep in ds_depends:
        did = dep.get("dataset_id")
        dver = dep.get("version")
        dprefix = f"{did}/{dver}"
        try:
            pds = _load_json(s3, data_bucket, f"{dprefix}/dataset.json")
        except NotFound:
            v.append(Violation("dangling-parent", f"depends_on parent {dprefix!r} not found in data bucket", path=gname))
            continue
        for pg in pds.get("groups", []):
            pmanifest_rel = pg.get("manifest") or "manifest.json"
            try:
                pman = _load_json(s3, data_bucket, f"{dprefix}/{pmanifest_rel}")
            except NotFound:
                continue
            for e in pman.get("entries", []):
                sha = e.get("sha256")
                if sha:
                    all_shas[sha] = f"PARENT:{dprefix}"


def _resolve_tokenizer(s3, data_bucket, group, v, gname) -> dict[str, Any] | None:
    """If this group depends_on a published tokenizer dataset, load its tokenizer.json from
    the data bucket and DERIVE {vocab_size, eos_token_id} from the actual bytes. Returns the
    derived dict, or None if there's no tokenizer dependency (not every group has one).

    This is what makes the tokenizer an owned, first-class artifact rather than an HF
    reference, and what turns vocab_size from a typed guess into a computed, unfakeable value.
    A pretrain-tokens/curriculum-token group SHOULD carry a tokenizer dependency; if it
    doesn't, that's flagged so the decode bound can't silently fall back to a typed number.
    """
    from .profiles.tokenizer_v1 import derive_vocab

    deps = group.get("depends_on") or []
    tok_dep = next((d for d in deps if str(d.get("role", "")) == "tokenizer"
                    or str(d.get("dataset_id", "")).startswith(("tokenizer/", "vendor/"))
                    and "tokenizer" in str(d.get("dataset_id", ""))), None)
    if tok_dep is None:
        return None
    if data_bucket is None:
        v.append(Violation("tokenizer-no-data-bucket", f"group {gname!r} depends on a tokenizer but no data_bucket to resolve it", path=gname))
        return None
    dprefix = f"{tok_dep.get('dataset_id')}/{tok_dep.get('version')}"
    # find tokenizer.json in the parent's manifests
    try:
        pds = _load_json(s3, data_bucket, f"{dprefix}/dataset.json")
    except NotFound:
        v.append(Violation("tokenizer-parent-missing", f"tokenizer dependency {dprefix!r} not found in data bucket", path=gname))
        return None
    for pg in pds.get("groups", []):
        pman_rel = pg.get("manifest") or "manifest.json"
        try:
            pman = _load_json(s3, data_bucket, f"{dprefix}/{pman_rel}")
        except NotFound:
            continue
        for e in pman.get("entries", []):
            path = e.get("path", "")
            if path.rsplit("/", 1)[-1] == "tokenizer.json":
                try:
                    body = s3.get(data_bucket, f"{dprefix}/{path}")
                    return derive_vocab(body)
                except Exception as e2:  # noqa: BLE001
                    v.append(Violation("tokenizer-parent-unreadable", f"{dprefix}/{path}: {e2}", path=gname))
                    return None
    v.append(Violation("tokenizer-json-not-in-parent", f"tokenizer dependency {dprefix!r} has no tokenizer.json", path=gname))
    return None


# --------------------------------------------------------------------------------------
# promotion (§1) — only on a clean pass
# --------------------------------------------------------------------------------------


def promote(
    result: ValidationResult,
    s3: S3,
    *,
    data_bucket: str,
    landing_bucket: str,
    now: str | None = None,
    copy_workers: int = 1,
) -> None:
    """Server-side-copy a validated dataset from landing to the published bucket and write
    its catalog entry. Refuses a failed or incomplete result.

    ``copy_workers`` > 1 fans the copy loop and the CRC-reference HEADs out over a thread pool.
    Both are network-bound and each key is independent, so this scales near-linearly. It exists
    because promotion is ~2 S3 round-trips per object and was strictly sequential: a 6,913-object
    corpus is ~13,800 serial calls, which overruns the 60-minute Batch job-def limit — the same
    wall ``publish()`` already has ``hash_workers``/``copy_workers`` for. Default 1 preserves the
    original ordering exactly.

    Order still holds where it matters: every payload copy completes before any CRC is read
    (they are separate phases), and the seal is written last, after both.
    """
    if not result.ok:
        raise ValueError("refusing to promote a dataset that did not pass Gate A")

    prefix_check = f"{result.dataset_id}/{result.version}"
    # REFUSE TO RE-PROMOTE A SEALED PREFIX. Without this, "frozen means frozen" was defeated by
    # PutObject alone — no Delete call, so the new Delete Deny never fires. Landing expires after
    # 14 days, so the same vN prefix genuinely frees up; re-running publish+promote then
    # OVERWRITES the published payload, the manifest, and the seal together, leaving
    # verify_seal reporting INTACT on substituted data. Versioning keeps the old bytes
    # noncurrent, but nothing on any read path looks at a noncurrent version.
    #
    # The seal is the marker to check because it is written LAST, so its presence is exactly the
    # "this prefix is complete and published" claim. Republishing means a new version.
    try:
        s3.head(data_bucket, f"{prefix_check}/_VALIDATED.json")
    except NotFound:
        pass
    else:
        raise ValueError(
            f"refusing to re-promote {prefix_check!r}: it is already sealed in {data_bucket!r}. "
            f"Frozen means frozen — publish a new version rather than overwriting a published "
            f"one. (An overwrite needs no Delete call, so no policy would stop it.)"
        )

    prefix = f"{result.dataset_id}/{result.version}"
    # copy dataset.json + every group manifest + every payload object
    ds = _load_json(s3, landing_bucket, f"{prefix}/dataset.json")
    keys: list[str] = [f"{prefix}/dataset.json"]
    payload_paths: list[str] = []  # manifest-relative, for the CRC reference below
    for group in ds.get("groups", []):
        manifest_rel = group.get("manifest") or "manifest.json"
        keys.append(f"{prefix}/{manifest_rel}")
        man = _load_json(s3, landing_bucket, f"{prefix}/{manifest_rel}")
        for e in man.get("entries", []):
            keys.append(_join(prefix, e["path"]))
            payload_paths.append(e["path"])

    def _copy_one(key: str) -> None:
        s3.copy(landing_bucket, key, data_bucket, key)

    if copy_workers > 1 and len(keys) > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=copy_workers) as pool:
            list(pool.map(_copy_one, keys))  # raises if any copy failed
    else:
        for key in keys:
            _copy_one(key)

    # CAPTURE THE CRC REFERENCE POST-COPY, FROM THE DESTINATION. This is what lets wu-fsck
    # (Gate B) detect a same-length overwrite of a frozen object for the price of a HEAD, with
    # no payload GET — see fsck._check_crc64nvme.
    #
    # It MUST be HEADed here, on `data_bucket`, after the copy, and never inherited from the
    # landing object: `CopyObject` RECOMPUTES the checksum server-side, so the value the
    # promoted copy carries is a property of the copy. A CRC read from landing would describe
    # bytes that (a) may not be the ones that landed in the data bucket and (b) are gone in 14
    # days when landing expires — an unfalsifiable reference, which is the decoration the
    # standard exists to remove.
    #
    # Best-effort and only for paths where S3 actually returned a checksum: objects predating
    # additional-checksum support have none, and a missing reference must mean "not checkable"
    # (fsck skips it silently), never "changed".
    def _crc_for(rel: str) -> tuple[str, str | None]:
        try:
            return rel, s3.head(data_bucket, _join(prefix, rel)).get("crc64nvme")
        except (NotFound, OSError):
            return rel, None

    if copy_workers > 1 and len(payload_paths) > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=copy_workers) as pool:
            crc_pairs = list(pool.map(_crc_for, payload_paths))
    else:
        crc_pairs = [_crc_for(rel) for rel in payload_paths]

    # Built in submission order either way, so the reference map is identical regardless of
    # worker count — it feeds the seal, which must not depend on scheduling.
    crc_reference: dict[str, str] = {rel: crc for rel, crc in crc_pairs if crc}

    # ROOT the hash chain. Each group already carries a manifest_sha256, and dataset.json
    # carries all of them — but nothing hashed dataset.json itself, so the chain had no root
    # and neither the seal nor the catalog bound to any content. "Frozen means frozen" was
    # therefore unfalsifiable: you could not tell a tampered dataset.json from the sealed one.
    # With a root, a reader confirms the whole tree with one HEAD + one GET.
    # Hash what was PUBLISHED, not the landing copy. Landing is write-anything by design and
    # `_put_create_only` is documented as "not a lock", so a producer re-putting dataset.json
    # between the copy loop above and this read would make the seal bind to bytes that never
    # reached the data bucket — verify_seal would then report a never-tampered dataset as
    # tampered, permanently, on a frozen prefix. A verifier with false positives gets ignored.
    dataset_sha256 = sha256_bytes(s3.get(data_bucket, f"{prefix}/dataset.json"))

    catalog = {
        "schema_version": "edullm-catalog/v1",
        "dataset_id": result.dataset_id,
        "version": result.version,
        "uri": f"s3://{data_bucket}/{prefix}/",
        "objects": ds.get("inventory", {}).get("objects"),
        "bytes": ds.get("inventory", {}).get("bytes"),
        "dataset_sha256": dataset_sha256,
    }
    if now is not None:
        catalog["promoted_at"] = now
    s3.put(
        data_bucket,
        f"_catalog/{result.dataset_id}/{result.version}.json",
        canonical_json(catalog),
        content_type="application/json",
    )

    # Generate the per-dataset README from dataset.json and write it beside the payload (§3:
    # the README is a DERIVED artifact, generated from dataset.json — never hand-written, so it
    # can't drift from the manifest). It is a CONTROL file, not a manifest entry, so it never
    # enters the hash chain or the exhaustiveness check (README.md is in CONTROL_BASENAMES).
    # Written here, before the _VALIDATED seal, so the "seal implies complete" invariant covers
    # it too. A rendering bug must not fail an otherwise-valid promotion — the README is
    # documentation, the dataset is the bytes — so this is best-effort.
    try:
        from . import __version__ as _pkg_version
        from .readme import render_readme

        s3.put(
            data_bucket,
            f"{prefix}/README.md",
            render_readme(ds, generator_version=_pkg_version).encode("utf-8"),
            content_type="text/markdown",
        )
    except Exception:  # noqa: BLE001 - README generation is documentation, not a gate
        pass

    # Seal the promoted prefix with _VALIDATED.json IN THE DATA BUCKET. This is the marker
    # read.dataset_paths() looks for (§9): "only edullm-data holds validated data", so the
    # proof-of-validation must live beside the promoted copy, not only in landing. The landing
    # _VALIDATED.json main() writes is the self-discovery work-list signal (so the scanner skips
    # an already-done prefix) and expires with landing's 14-day lifecycle; this copy is durable
    # and is what makes a promoted dataset actually readable. Written LAST, after the payload +
    # catalog are in place, so the marker's presence always implies a complete, readable dataset.
    #
    # The seal carries dataset_sha256 + every group's manifest_sha256, which is what makes it
    # a claim about CONTENT rather than a bare "someone ran the validator". A reader (or fsck)
    # recomputes sha256(dataset.json) and compares; from there the per-group manifest_sha256
    # values in that file chain down to every payload digest.
    seal = {
        "dataset_id": result.dataset_id,
        "version": result.version,
        "objects": ds.get("inventory", {}).get("objects"),
        "bytes": ds.get("inventory", {}).get("bytes"),
        "dataset_sha256": dataset_sha256,
        "manifest_sha256": {
            str(g.get("name")): g.get("manifest_sha256")
            for g in ds.get("groups", [])
            if g.get("manifest_sha256")
        },
    }
    # Only emit the key when there is something in it, so an old seal and a new seal on a
    # bucket without checksums are the same document rather than differing by an empty dict.
    if crc_reference:
        seal["crc64nvme"] = crc_reference
    if now is not None:
        seal["validated_at"] = now
    s3.put(
        data_bucket,
        f"{prefix}/_VALIDATED.json",
        canonical_json(seal),
        content_type="application/json",
    )


# --------------------------------------------------------------------------------------
# self-discovery (§7 event wiring) — the wake-up-and-scan work list
# --------------------------------------------------------------------------------------


def discover_pending(landing_bucket: str, s3: S3) -> list[str]:
    """Prefixes with a dataset.json and every declared group manifest, but no
    _VALIDATED/_REJECTED marker. The self-discovering validator's work list — the event is
    a pure 'wake up' signal, so this, not the event payload, drives what gets processed."""
    everything = s3.list(landing_bucket, "")
    keys = {o["key"] for o in everything}
    pending: list[str] = []
    for key in sorted(keys):
        if not key.endswith("/dataset.json"):
            continue
        prefix = key[: -len("/dataset.json")]
        if f"{prefix}/_VALIDATED.json" in keys or f"{prefix}/_REJECTED.json" in keys:
            continue
        try:
            ds = _load_json(s3, landing_bucket, key)
        except (ValueError, NotFound):
            continue
        # every group must have its manifest present (partially-sealed → skip until complete)
        ok = True
        for g in ds.get("groups", []):
            man_rel = g.get("manifest") or "manifest.json"
            if _join(prefix, man_rel) not in keys:
                ok = False
                break
        if ok:
            pending.append(prefix)
    return pending


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="edullm-data-validate", description="Gate A validator")
    ap.add_argument("--landing-bucket", default="edullm-landing")
    ap.add_argument("--data-bucket", default="edullm-data")
    ap.add_argument("--prefix", help="dataset prefix (<dataset_id>/<version>); omit to self-discover")
    ap.add_argument("--promote", action="store_true")
    ap.add_argument(
        "--promote-workers",
        type=int,
        default=1,
        help="threads for promote()'s copy + CRC loops (default 1, sequential). Promotion is "
             "~2 S3 round-trips per object, so a several-thousand-object corpus needs this to "
             "finish inside the Batch job-def time limit.",
    )
    ap.add_argument("--now", default=None, help="ISO-8601 timestamp to stamp markers with")
    args = ap.parse_args(argv)

    from .s3 import Boto3S3

    s3 = Boto3S3.default()
    prefixes = [args.prefix] if args.prefix else discover_pending(args.landing_bucket, s3)
    if not prefixes:
        print("no pending datasets", file=sys.stderr)
        return 0

    exit_code = 0
    for prefix in prefixes:
        result = validate_dataset(args.landing_bucket, prefix, s3, data_bucket=args.data_bucket)
        if result.incomplete:
            print(f"{prefix}: INCOMPLETE (not sealed) — leaving for a later run", file=sys.stderr)
            continue
        if result.ok:
            if args.promote:
                promote(
                    result,
                    s3,
                    data_bucket=args.data_bucket,
                    landing_bucket=args.landing_bucket,
                    now=args.now,
                    copy_workers=args.promote_workers,
                )
            s3.put(args.landing_bucket, f"{prefix}/_VALIDATED.json",
                   canonical_json(result.report()), content_type="application/json")
            print(f"{prefix}: PASS" + (" + promoted" if args.promote else ""))
        else:
            s3.put(args.landing_bucket, f"{prefix}/_REJECTED.json",
                   canonical_json(result.rejection_doc(now=args.now)), content_type="application/json")
            print(f"{prefix}: REJECTED ({len(result.violations)} violations)", file=sys.stderr)
            for v in result.violations:
                print(f"  - {v}", file=sys.stderr)
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
