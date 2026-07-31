"""The reservoir ingest role must not be able to reach the published bucket.

Mirrors `infra/08-reservoir-ingest-policy.json` and `-trust-policy.json`. These read the shipped
files; they cannot prove the LIVE role matches — that is the deploy runbook's job
(`infra/DEPLOY.md`) and a live smoke test, because `iam:simulate-principal-policy` is documented in
`CLAUDE.md` as returning false denials for this account.

WHY A BUILD ROLE GETS ITS OWN TEST. The tempting shortcut is to run ingest under the validator
role, which already exists and already works. That role is the ONLY principal that can write
`s3://edullm-data` — the airlock itself. Running a 5 TB bulk-download job under it puts a program
doing wide-open network fetches in possession of the one key to the published bucket, so a bug in
ingest becomes a bug that can write published data.

The whole value of this policy is what it does NOT contain, and absences are exactly what a reader
skims past. Hence tests: an absence nobody checks is an absence that comes back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parent.parent / "infra"
POLICY_PATH = INFRA / "08-reservoir-ingest-policy.json"
TRUST_PATH = INFRA / "08-reservoir-ingest-trust-policy.json"


@pytest.fixture(scope="module")
def policy() -> dict:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def trust() -> dict:
    return json.loads(TRUST_PATH.read_text(encoding="utf-8"))


def _as_list(v) -> list[str]:
    return list(v) if isinstance(v, list) else [v]


def _statements(policy: dict, effect: str | None = None) -> list[dict]:
    out = [s for s in policy["Statement"] if not isinstance(s, str)]
    return [s for s in out if effect is None or s["Effect"] == effect]


# --------------------------------------------------------------------------------------
# The airlock: this role cannot write published data
# --------------------------------------------------------------------------------------


def test_no_statement_allows_writing_the_published_bucket(policy):
    """THE test. Every Allow's resources are checked against `edullm-data` write actions."""
    write_actions = {
        "s3:PutObject",
        "s3:PutObjectTagging",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:AbortMultipartUpload",
        "s3:*",
        "*",
    }
    for st in _statements(policy, "Allow"):
        actions = set(_as_list(st["Action"]))
        resources = _as_list(st["Resource"])
        if actions & write_actions:
            for r in resources:
                assert "edullm-data" not in r, f"{st['Sid']} would write {r}"


def test_reads_of_the_published_bucket_are_narrow_and_read_only(policy):
    """Planning needs the catalog and the tokenizer it will declare a dependency on. Nothing else,
    and nothing writable."""
    for st in _statements(policy, "Allow"):
        for r in _as_list(st["Resource"]):
            if "edullm-data" in r:
                assert set(_as_list(st["Action"])) <= {"s3:GetObject", "s3:GetObjectAttributes"}
                assert r.startswith(
                    ("arn:aws:s3:::edullm-data/_catalog/", "arn:aws:s3:::edullm-data/tokenizer/")
                ), r


def test_no_delete_anywhere_at_all(policy):
    """Not even on its own staging prefix. A retry that can delete can destroy a different run's
    evidence; a fresh run id plus the 30-day lifecycle rule reclaims partials instead."""
    for st in _statements(policy, "Allow"):
        for a in _as_list(st["Action"]):
            assert "Delete" not in a, f"{st['Sid']} grants {a}"


def test_no_iam_or_policy_mutation(policy):
    """`iam:PutRolePolicy` is how an identity-policy grant gets widened from inside a job. The
    airlock's v1 weakness was exactly that shape."""
    for st in _statements(policy, "Allow"):
        for a in _as_list(st["Action"]):
            assert not a.startswith(("iam:", "sts:")), f"{st['Sid']} grants {a}"
            assert a != "*" and not a.startswith("s3:Put*"), f"{st['Sid']} grants {a}"


# --------------------------------------------------------------------------------------
# The EventBridge landmine, enforced where code cannot be edited around it
# --------------------------------------------------------------------------------------


def test_manifest_json_is_denied_by_iam_not_only_by_code(policy):
    """`edullm-landing-manifest-created` matches key SUFFIX `manifest.json` with no prefix
    constraint, so that basename anywhere in landing fires the validator against a build artifact.

    `ingest_reservoir._assert_safe_key` refuses it in code. This is the same rule in IAM: the code
    guard protects against a typo in this program, the IAM guard against a different program.
    """
    denied = {r for st in _statements(policy, "Deny") for r in _as_list(st["Resource"])}
    assert "arn:aws:s3:::edullm-landing/*manifest.json" in denied


def test_the_validator_terminal_markers_are_denied(policy):
    """Either marker suppresses validator discovery of a real dataset prefix."""
    denied = {r for st in _statements(policy, "Deny") for r in _as_list(st["Resource"])}
    for name in ("_VALIDATED.json", "_REJECTED.json", "dataset.json"):
        assert f"arn:aws:s3:::edullm-landing/*{name}" in denied, name


def test_the_deny_covers_putobject(policy):
    for st in _statements(policy, "Deny"):
        assert "s3:PutObject" in _as_list(st["Action"])


def test_code_and_iam_agree_on_the_reserved_names(policy):
    """The two guards must not drift: a name refused in code but allowed in IAM (or the reverse)
    means one of them is decoration."""
    from edullm_data.ingest_reservoir import _RESERVED_BASENAMES

    denied = {r for st in _statements(policy, "Deny") for r in _as_list(st["Resource"])}
    for base in _RESERVED_BASENAMES:
        assert f"arn:aws:s3:::edullm-landing/*{base}" in denied, base


# --------------------------------------------------------------------------------------
# Staging scope
# --------------------------------------------------------------------------------------


def test_writes_are_confined_to_one_landing_prefix(policy):
    """`_ingest/reservoir-dolma2/` is the only writable location, and it is the prefix the
    `expire-ingest-30d` lifecycle rule covers — otherwise 5 TB would persist indefinitely."""
    writable = [
        r
        for st in _statements(policy, "Allow")
        if "s3:PutObject" in _as_list(st["Action"])
        for r in _as_list(st["Resource"])
    ]
    assert writable == ["arn:aws:s3:::edullm-landing/_ingest/reservoir-dolma2/*"]


def test_the_writable_prefix_is_covered_by_the_shipped_lifecycle_rule():
    """Binds this policy to `07-landing-ingest-lifecycle.json`. If someone moves the staging prefix
    without moving the expiry rule, the bytes become permanent and nothing else notices."""
    from edullm_data.ingest_reservoir import _assert_lifecycle_covers

    rules = json.loads((INFRA / "07-landing-ingest-lifecycle.json").read_text())["Rules"]

    class _S3:
        def get_bucket_lifecycle_configuration(self, Bucket):  # noqa: N803
            return {"Rules": rules}

    assert _assert_lifecycle_covers(_S3(), "edullm-landing", "_ingest/reservoir-dolma2/") is None


def test_list_is_scoped_by_prefix_condition(policy):
    """`s3:ListBucket` takes the BUCKET as its resource, so without a prefix condition it lists
    everything in landing — a common way least-privilege policies leak more than intended."""
    for st in _statements(policy, "Allow"):
        if "s3:ListBucket" in _as_list(st["Action"]):
            prefixes = st["Condition"]["StringLike"]["s3:prefix"]
            assert all(p.startswith("_ingest/reservoir-dolma2/") for p in prefixes), prefixes


# --------------------------------------------------------------------------------------
# Trust policy
# --------------------------------------------------------------------------------------


def test_only_ecs_tasks_may_assume_the_role(trust):
    """No human session can become this role — the same property that made the airlock's delete
    Deny unprobeable as the validator. It matters more than usual here: the intern role in this
    account carries AdministratorAccess, so IAM is not what keeps a person out of pipeline
    identities; this trust policy is."""
    assert len(trust["Statement"]) == 1
    st = trust["Statement"][0]
    assert st["Effect"] == "Allow"
    assert st["Principal"] == {"Service": "ecs-tasks.amazonaws.com"}
    assert _as_list(st["Action"]) == ["sts:AssumeRole"]


def test_trust_policy_names_no_aws_principal(trust):
    """An `AWS` principal would let a role or user assume it directly."""
    assert "AWS" not in json.dumps(trust["Statement"][0]["Principal"])


# --------------------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [POLICY_PATH, TRUST_PATH])
def test_documents_are_valid_and_versioned(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["Version"] == "2012-10-17"
    assert doc["Statement"]


@pytest.mark.parametrize("path", [POLICY_PATH, TRUST_PATH])
def test_no_account_id_leaks_into_a_public_repo(path):
    """Public repo. Bucket names are functional constants and stay; account digits do not."""
    import re

    assert not re.search(r"\b\d{12}\b", path.read_text(encoding="utf-8"))
