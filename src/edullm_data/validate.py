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
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Mapping

from .contracts import (
    FAMILIES,
    NamingError,
    SCHEMA_VERSION,
    Version,
    canonical_json,
    sha256_bytes,
    validate_dataset_id,
    validate_purpose,
)
from .manifest import (
    FIXED_WIDTH_CONTAINERS,
    ManifestEntry,
    build_manifest,
    check_extension_matches_format,
    check_shard_naming,
    diff_paths,
    is_cas_name,
    manifest_sha256,
    verify_arithmetic,
)
from .profiles import registry
from .profiles.base import GroupContext, Violation
from .s3 import S3, NotFound, S3Error

# Control files that live under a dataset prefix but are not group payload — excluded from
# the manifest-exhaustiveness comparison so they never read as "extra".
CONTROL_BASENAMES = frozenset(
    {"dataset.json", "manifest.json", "_SUCCESS", "_VALIDATED.json", "_REJECTED.json", "README.md"}
)
CONTROL_PREFIXES = ("_catalog/", "dependents/")

#: Where ``families/*.json`` lives when running from a checkout. Deliberately resolved the
#: same way ``publish.py`` does rather than imported from it, so the gate does not pull the
#: publisher in. Absent inside the Batch image (the wheel ships only ``src/edullm_data``),
#: which ``_family_defaults_for`` handles by returning no defaults instead of raising.
FAMILIES_DIR = Path(__file__).resolve().parent.parent.parent / "families"

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
    base = rel.rsplit("/", 1)[-1]
    if base in CONTROL_BASENAMES:
        return True
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

    if ds.get("schema_version") != SCHEMA_VERSION:
        v.append(Violation("schema-version", f"schema_version is {ds.get('schema_version')!r}, expected {SCHEMA_VERSION!r}"))

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

    # -- per-group validation --
    total_objects = 0
    total_bytes = 0
    all_shas: dict[str, str] = {}  # sha256 -> first path that declared it (dup detection)
    my_shas: set[str] = set()
    incomplete = False

    for group in groups:
        gname = str(group.get("name", "?"))
        gprefix = group.get("prefix", "")
        gres = _validate_group(
            s3, landing_bucket, prefix, dataset_id, version_id, group, v, all_shas, my_shas,
            data_bucket=data_bucket,
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
    missing, extra = diff_paths(manifest_paths, actual_rel)
    for m in sorted(missing):
        v.append(Violation("missing-object", f"manifest lists {m!r} but it is not in S3", path=m))
    for x in sorted(extra):
        v.append(Violation("unlisted-object", f"{x!r} is in S3 but not in the manifest (a globbing reader would train on it)", path=x))

    # -- partitions (§7): every partition declares rows; structural check of the four forms --
    _validate_partitions(group, v, gname, manifest_paths)

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
_DECODE_BOUND_ALIASES = {
    "distinct_ids_min": "min_distinct_ids",
    "eos_fraction_max": "max_eos_fraction",
    "zero_fraction_max": "max_zero_fraction",
    "window_bytes": "window_bytes",
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
    entries: list[Any], tok_derived: Mapping[str, Any], v: list[Violation], gname: str
) -> None:
    """Every fixed-width shard's dtype must be wide enough for the tokenizer's vocab.

    One-sided on purpose. Declaring a WIDER dtype than strictly necessary is legal (it wastes
    space but reads correctly), so only too-narrow is a violation. Too-narrow is impossible to
    read correctly and is exactly the lie every other check misses.
    """
    vocab_size = tok_derived.get("vocab_size")
    if not isinstance(vocab_size, int) or vocab_size <= 0:
        return  # nothing derived to compare against; _resolve_tokenizer already flagged it
    required = _min_dtype_size_for_vocab(vocab_size)
    for entry in entries:
        fmt = entry.format
        declared = fmt.dtype_size
        if not declared or fmt.container not in FIXED_WIDTH_CONTAINERS:
            continue  # jsonl/tar and friends have no token width to check
        if declared < required:
            v.append(Violation(
                "dtype-too-narrow-for-vocab",
                f"{entry.path}: declared dtype {fmt.dtype!r} is {declared} bytes, but the "
                f"tokenizer this group depends_on has vocab_size={vocab_size}, which needs "
                f"at least {required} bytes per token. A {declared}-byte read of these bytes "
                f"cannot represent every id, and the declared count "
                f"({entry.count.get('value') if isinstance(entry.count, Mapping) else '?'}) "
                f"is inflated by {required // declared}x. Arithmetic and extension checks "
                f"CANNOT catch this — they are self-consistent with the wrong dtype.",
                path=entry.path,
            ))


def _validate_partitions(group: Mapping[str, Any], v: list[Violation], gname: str, manifest_paths: set[str]) -> None:
    parts = group.get("partitions")
    if parts is None:
        return
    if not isinstance(parts, list):
        v.append(Violation("bad-partitions", f"group {gname!r}: partitions must be a list", path=gname))
        return
    coverage = group.get("coverage")
    if coverage not in {"partition", "overlapping", "incomplete"}:
        v.append(Violation("bad-coverage", f"group {gname!r}: coverage {coverage!r} not in partition|overlapping|incomplete", path=gname))
    for p in parts:
        pname = p.get("name", "?")
        if "rows" not in p:
            v.append(Violation("partition-no-rows", f"group {gname!r} partition {pname!r} declares no rows", path=gname))
        by = p.get("by")
        if by not in {"path", "field", "range", "indices"}:
            v.append(Violation("bad-partition-form", f"partition {pname!r} has by={by!r}", path=gname))
        elif by == "path":
            glob = p.get("glob", "")
            if not any(fnmatch.fnmatch(mp.rsplit("/", 1)[-1], glob) or fnmatch.fnmatch(mp, glob) for mp in manifest_paths):
                v.append(Violation("partition-glob-empty", f"partition {pname!r} glob {glob!r} matches no manifest path", path=gname))
        # field/range/indices scans are a v1 TODO — a profile check reads the bytes.


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


def promote(result: ValidationResult, s3: S3, *, data_bucket: str, landing_bucket: str, now: str | None = None) -> None:
    """Server-side-copy a validated dataset from landing to the published bucket and write
    its catalog entry. Refuses a failed or incomplete result."""
    if not result.ok:
        raise ValueError("refusing to promote a dataset that did not pass Gate A")

    prefix = f"{result.dataset_id}/{result.version}"
    # copy dataset.json + every group manifest + every payload object
    ds = _load_json(s3, landing_bucket, f"{prefix}/dataset.json")
    keys: list[str] = [f"{prefix}/dataset.json"]
    for group in ds.get("groups", []):
        manifest_rel = group.get("manifest") or "manifest.json"
        keys.append(f"{prefix}/{manifest_rel}")
        man = _load_json(s3, landing_bucket, f"{prefix}/{manifest_rel}")
        for e in man.get("entries", []):
            keys.append(_join(prefix, e["path"]))

    for key in keys:
        s3.copy(landing_bucket, key, data_bucket, key)

    # ROOT the hash chain. Each group already carries a manifest_sha256, and dataset.json
    # carries all of them — but nothing hashed dataset.json itself, so the chain had no root
    # and neither the seal nor the catalog bound to any content. "Frozen means frozen" was
    # therefore unfalsifiable: you could not tell a tampered dataset.json from the sealed one.
    # With a root, a reader confirms the whole tree with one HEAD + one GET.
    dataset_sha256 = sha256_bytes(s3.get(landing_bucket, f"{prefix}/dataset.json"))

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
                promote(result, s3, data_bucket=args.data_bucket, landing_bucket=args.landing_bucket, now=args.now)
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
