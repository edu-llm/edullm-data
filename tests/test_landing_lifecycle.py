"""No lifecycle rule may reach the two landing prefixes nothing else holds a copy of.

``s3://edullm-landing`` is the write-anything side of the airlock and everything in it
expires, which is the whole point of it. Two prefixes are exceptions, and both are
exceptions because losing them costs something no re-run recovers:

``_dist/``
    the bootstrap wheel every Batch job ``pip install``s before it runs. An expiry here
    breaks the validator, the publisher and fsck simultaneously, and the failure looks like
    a pip error rather than like a lifecycle rule.

``_preserved/``
    operator rescue copies of corpora that Gate A refused and that were therefore never
    promoted, so ``edullm-data`` does not hold them and nothing else does either. On
    2026-08-06 that is ``_preserved/pretrain/olmo-127b/v2/`` — 364 objects and
    122,828,050,823 bytes.

Neither was protected. They survived because the deployed configuration had been
hand-edited into per-prefix rules and no rule happened to name them, while the template
still carried one unfiltered fourteen-day expiry over the whole bucket. Surviving because
nothing points at you is not the same as being protected, and the gap between the two is
one ``cloudformation deploy`` of a file that was already in the repository.

WHAT THIS CANNOT SEE. It reads ``infra/01-buckets.yaml`` and nothing else. A lifecycle
configuration written straight onto the live bucket with ``s3api
put-bucket-lifecycle-configuration`` is invisible here — which is not a hypothetical, it is
how the currently deployed configuration came to exist. Closing that needs something that
reads the bucket; ``infra/DEPLOY.md`` is where the read belongs until it does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "infra" / "01-buckets.yaml"
FAMILIES_DIR = REPO_ROOT / "families"

LANDING_BUCKET = "edullm-landing"

#: Prefixes no lifecycle rule may delete from, and why each one is here. The reason is part
#: of the constant so that removing an entry means deleting a sentence somebody has to
#: disagree with in a review, rather than deleting a string.
PROTECTED_PREFIXES = {
    "_dist/": "the bootstrap wheel every Batch job installs before it can run",
    "_preserved/": "rescue copies of refused corpora that exist in no other bucket",
}

#: Lifecycle actions that can remove or relocate an object that finished uploading.
#: ``AbortIncompleteMultipartUpload`` is deliberately absent: it acts only on an upload that
#: never completed, so it cannot cost a stored byte, and landing's copy of it is unfiltered
#: on purpose (§10 calls it load-bearing).
DESTRUCTIVE_ACTIONS = frozenset(
    {
        "ExpirationInDays",
        "ExpirationDate",
        "Expiration",
        "ExpiredObjectDeleteMarker",
        "NoncurrentVersionExpiration",
        "NoncurrentVersionExpirationInDays",
        "Transition",
        "Transitions",
        "NoncurrentVersionTransition",
        "NoncurrentVersionTransitions",
    }
)


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    return yaml.safe_load(TEMPLATE_PATH.read_text(encoding="utf-8"))


def _landing_rules(template: dict[str, Any]) -> list[dict[str, Any]]:
    for resource in template["Resources"].values():
        properties = resource.get("Properties", {})
        if properties.get("BucketName") != LANDING_BUCKET:
            continue
        return list(properties.get("LifecycleConfiguration", {}).get("Rules", []))
    raise AssertionError(f"{TEMPLATE_PATH.name} declares no bucket named {LANDING_BUCKET!r}")


def _rule_prefixes(rule: dict[str, Any]) -> list[str] | None:
    """Every prefix the rule is scoped to, or ``None`` if it is scoped to no prefix.

    ``None`` means "matches every key", and it is returned for a tag-only or size-only
    filter as well as for no filter at all — a rule selected by tag can land on an object
    under any prefix, so it constrains nothing about which prefixes it reaches.
    """
    if "Prefix" in rule:  # the pre-Filter spelling, which S3 still accepts
        return [str(rule["Prefix"])]
    fltr = rule.get("Filter")
    if not isinstance(fltr, dict):
        return None
    if "Prefix" in fltr:
        return [str(fltr["Prefix"])]
    conjunction = fltr.get("And")
    if isinstance(conjunction, dict) and "Prefix" in conjunction:
        return [str(conjunction["Prefix"])]
    return None


def _reaches(rule_prefix: str, protected: str) -> bool:
    """Whether a rule scoped to ``rule_prefix`` can select a key under ``protected``.

    Lifecycle matches a prefix against the start of the key, so the two overlap whenever
    either string is a prefix of the other: ``""`` and ``_pre`` both reach ``_preserved/``,
    and so does ``_preserved/pretrain/``.
    """
    return protected.startswith(rule_prefix) or rule_prefix.startswith(protected)


def _destructive(rule: dict[str, Any]) -> list[str]:
    return sorted(set(rule) & DESTRUCTIVE_ACTIONS)


def test_no_rule_can_delete_from_a_protected_prefix(template):
    """THE invariant. If this fails, deploying the template loses bytes nothing else holds."""
    failures = []
    for rule in _landing_rules(template):
        actions = _destructive(rule)
        if not actions:
            continue
        prefixes = _rule_prefixes(rule)
        for protected, reason in PROTECTED_PREFIXES.items():
            if prefixes is None:
                failures.append(
                    f"{rule.get('Id')!r} carries {actions} and is scoped to no prefix, so it "
                    f"selects every key in the bucket including {protected!r} — {reason}"
                )
                continue
            for prefix in prefixes:
                if _reaches(prefix, protected):
                    failures.append(
                        f"{rule.get('Id')!r} carries {actions} under prefix {prefix!r}, which "
                        f"overlaps {protected!r} — {reason}"
                    )
    assert not failures, "\n".join(failures)


def test_a_disabled_rule_is_not_an_exemption(template):
    """Status is not a scope, and the check above must not be dodged by flipping one word.

    A disabled rule that names a protected prefix is a rule somebody enables on a morning
    when the bucket is expensive, and the reason it was disabled will not be in front of
    them. The invariant is about what a rule can reach, not about whether it is running
    today, so the check above reads every rule and this asserts that it does.
    """
    statuses = {str(rule.get("Status")) for rule in _landing_rules(template)}
    assert statuses <= {"Enabled", "Disabled"}, statuses
    disabled = [r for r in _landing_rules(template) if r.get("Status") != "Enabled"]
    for rule in disabled:
        prefixes = _rule_prefixes(rule)
        assert prefixes is not None, (
            f"{rule.get('Id')!r} is disabled and unscoped. Delete it rather than disabling "
            f"it: a rule that is one word from matching every key is not a protection."
        )


def test_every_family_prefix_expires(template):
    """The other direction: a family whose landing prefix no rule reaches never expires.

    The enumeration exists because lifecycle has no way to say "everything except", so the
    exclusion of ``_dist/`` and ``_preserved/`` had to be written as a list of what IS
    expired. A list is only as good as the thing holding it to its source, and the source is
    ``families/``. Adding ``families/foo.json`` without a rule here would otherwise leave
    landing accumulating a family forever, which is the same class of mistake pointing the
    other way.
    """
    families = sorted(p.stem for p in FAMILIES_DIR.glob("*.json"))
    assert families, f"no family files under {FAMILIES_DIR}"

    expiring: set[str] = set()
    for rule in _landing_rules(template):
        if "ExpirationInDays" not in rule and "ExpirationDate" not in rule:
            continue
        for prefix in _rule_prefixes(rule) or []:
            expiring.add(prefix)

    missing = [f for f in families if f"{f}/" not in expiring]
    assert not missing, (
        f"families {missing} have no landing expiry rule, so their landing copies are kept "
        f"forever. Every family in families/ needs one: {sorted(expiring)}"
    )


def test_the_landing_expiry_is_still_the_number_the_standard_names(template):
    """Splitting one rule into seven must not have changed what any of them does."""
    days = {
        rule["ExpirationInDays"]
        for rule in _landing_rules(template)
        if "ExpirationInDays" in rule
    }
    assert days == {14}, f"§10 says landing expires at 14 days; found {sorted(days)}"


def test_the_family_list_is_the_one_the_package_enforces():
    """``families/`` is the source this reads, so it must be the source the code reads too."""
    from edullm_data.contracts import FAMILIES

    assert {p.stem for p in FAMILIES_DIR.glob("*.json")} == set(FAMILIES)


def test_families_are_readable_json():
    """A family file that does not parse would silently shrink the list above to nothing."""
    for path in FAMILIES_DIR.glob("*.json"):
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict), path
