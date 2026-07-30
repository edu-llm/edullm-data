"""The airlock's Delete Deny must bind every principal, including the validator.

v1 had ONE Deny covering PutObject, DeleteObject and DeleteObjectVersion, exempting
``<BATCH_JOB_ROLE>`` and ``<INFRA_DEPLOYER_ROLE>`` from all five actions. So the *bucket*
policy permitted the validator to delete published data, and the only thing preventing it was
that ``03-validator-policy.json`` happens to grant ``PutObject`` and not ``Delete*``.

That is an identity policy on a role whose inline policies are editable with
``iam:PutRolePolicy`` — an action the intern session has. One call away from a principal that
can erase a frozen, published dataset. "Frozen means frozen" was resting on the wrong kind of
control: a grant somebody can widen, rather than a Deny nobody can escape.

An explicit Deny with no principal exemption always wins, so v2 gets Object Lock's guarantee
without Object Lock's failure modes (a version-not-path scope, non-WORM delete markers, a
bypassable GOVERNANCE mode, and no way to turn it off).

These tests read the shipped policy file. They cannot prove the LIVE bucket matches — that is
the deploy runbook's job (``infra/DEPLOY.md``, "Deploying the split Delete Deny").
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

POLICY_PATH = Path(__file__).resolve().parent.parent / "infra" / "02-bucket-policy.json"


@pytest.fixture(scope="module")
def policy() -> dict:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def _actions(statement: dict) -> list[str]:
    act = statement["Action"]
    return list(act) if isinstance(act, list) else [act]


def _denies(policy: dict) -> list[dict]:
    return [s for s in policy["Statement"] if s["Effect"] == "Deny"]


def test_delete_is_denied_to_everyone_with_no_principal_exemption():
    """THE invariant. If this fails, a published dataset is deletable by some principal."""
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    delete_denies = [
        s for s in _denies(policy) if any("Delete" in a for a in _actions(s))
    ]
    assert delete_denies, "no Deny statement covers Delete* on edullm-data"
    for s in delete_denies:
        cond = s.get("Condition", {})
        assert "ArnNotEqualsIfExists" not in cond, (
            f"{s['Sid']} exempts a principal from Delete*: "
            f"{cond['ArnNotEqualsIfExists']}. An identity policy can then be widened to "
            f"permit deleting frozen data."
        )
        assert "ArnNotEquals" not in cond, f"{s['Sid']} exempts a principal from Delete*"


def test_delete_and_write_are_separate_statements(policy):
    """They must not share a statement, or one exemption list governs both."""
    for s in _denies(policy):
        acts = _actions(s)
        has_delete = any("Delete" in a for a in acts)
        has_write = any(a in {"s3:PutObject", "s3:PutObjectTagging"} for a in acts)
        assert not (has_delete and has_write), (
            f"{s['Sid']} mixes delete and write actions, so exempting a writer also exempts "
            f"a deleter: {acts}"
        )


def test_writes_are_still_restricted_to_the_validator(policy):
    """Splitting the Deny must not have widened write access."""
    write_denies = [
        s for s in _denies(policy) if "s3:PutObject" in _actions(s)
    ]
    assert write_denies, "nothing denies PutObject — the airlock is open"
    exempt = set()
    for s in write_denies:
        exempt |= set(s.get("Condition", {}).get("ArnNotEqualsIfExists", {}).get("aws:PrincipalArn", []))
    assert any("BATCH_JOB_ROLE" in a for a in exempt), exempt
    assert len(exempt) <= 2, f"more principals can write than the validator + deployer: {exempt}"


def test_no_wildcard_action_could_lock_out_policy_management(policy):
    """Footgun 2 in DEPLOY.md: s3:* or s3:Put* would catch PutBucketPolicy itself."""
    for s in _denies(policy):
        for a in _actions(s):
            assert a != "s3:*", f"{s['Sid']}: s3:* in a Deny is a hard lockout"
            assert not a.endswith("*"), f"{s['Sid']}: wildcard action {a!r} in a Deny"


def test_every_deny_lets_aws_services_through(policy):
    """Lifecycle and inventory run as the service; without this they would be denied."""
    for s in _denies(policy):
        assert s.get("Condition", {}).get("BoolIfExists", {}).get(
            "aws:PrincipalIsAWSService"
        ) == "false", f"{s['Sid']} has no service escape hatch"


def test_role_arns_use_the_iam_role_form_not_an_assumed_role_session(policy):
    """Footgun 1: an sts assumed-role ARN makes the condition always true, denying everyone."""
    for s in _denies(policy):
        for arn in s.get("Condition", {}).get("ArnNotEqualsIfExists", {}).get("aws:PrincipalArn", []):
            assert arn.startswith("arn:aws:iam::"), arn
            assert ":role/" in arn and "assumed-role" not in arn, arn


def test_policy_id_records_the_version(policy):
    assert policy["Id"] == "edullm-data-airlock-v2"
