"""The general-purpose publisher role: broad across datasets, still walled off from the airlock.

Mirrors `infra/10-dataset-publish-policy.json` and `-trust-policy.json`. These read the shipped files
and cannot prove the LIVE role matches — that is the deploy runbook's job, because
`iam:simulate-principal-policy` is documented in `CLAUDE.md` as returning false denials for this
account (11 known). Smoke-test a permission live; never trust the simulator.

WHY THIS ROLE IS DELIBERATELY BROAD, which is the thing to be suspicious of. Every prior producer
role is pinned to one dataset: `edullm-prm800k-producer` names `vendor/openai-prm800k/*` in five of
six statements. That means corpus number two needs role number two, and the per-corpus role becomes
the path of least resistance — each one a fresh chance to fat-finger a Deny that nobody reviews. One
reviewed role is less surface, not more.

So the grant is bounded by the **§2 family enum** rather than by a dataset name:
`contracts.validate_dataset_id` refuses any dataset_id whose first segment is not one of the seven
families, which is asserted here against the real function rather than assumed. That is what keeps a
family-scoped grant from reaching `_ingest/`, `_dist/` (bootstrap wheels, and that prefix has NO
lifecycle expiry), `_staging/`, or `_catalog/`.

The tests below are mostly about absences, and absences are exactly what a reader skims past.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from edullm_data.contracts import NamingError, validate_dataset_id

INFRA = Path(__file__).resolve().parent.parent / "infra"
POLICY_PATH = INFRA / "10-dataset-publish-policy.json"
TRUST_PATH = INFRA / "10-dataset-publish-trust-policy.json"

FAMILIES = ("curriculum", "eval", "pretrain", "probe", "sft", "tokenizer", "vendor")

WRITE_ACTIONS = {
    "s3:PutObject",
    "s3:PutObjectTagging",
    "s3:DeleteObject",
    "s3:DeleteObjectVersion",
    "s3:AbortMultipartUpload",
    "s3:*",
    "*",
}


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


def _write_resources(policy: dict) -> list[str]:
    return [
        r
        for st in _statements(policy, "Allow")
        if set(_as_list(st["Action"])) & WRITE_ACTIONS
        for r in _as_list(st["Resource"])
    ]


# --------------------------------------------------------------------------------------
# The airlock
# --------------------------------------------------------------------------------------


def test_no_allow_writes_the_published_bucket(policy):
    """THE test. publish() stages into landing; crossing into edullm-data is promote(), which belongs
    to the validator role alone."""
    for r in _write_resources(policy):
        assert "edullm-data" not in r, f"a write grant reaches {r}"


def test_the_published_bucket_deny_is_explicit_and_covers_deletes(policy):
    """Not absent-by-omission. An explicit Deny survives someone attaching a broad managed policy to
    this role later; an absence does not."""
    denies = [
        st
        for st in _statements(policy, "Deny")
        if any("edullm-data" in r for r in _as_list(st["Resource"]))
    ]
    assert denies, "no explicit Deny covers edullm-data"
    actions = {a for st in denies for a in _as_list(st["Action"])}
    for needed in ("s3:PutObject", "s3:DeleteObject"):
        assert needed in actions, f"{needed} is not denied on edullm-data"


def test_validator_verdicts_cannot_be_forged_anywhere_in_landing(policy):
    """`_VALIDATED.json` / `_REJECTED.json` are the validator's terminal markers. Forging one would
    make a REJECTED upload look promoted — the single forgery that defeats the gate outright. Denied
    bucket-wide via a suffix wildcard, not per-dataset, so a new family cannot miss the guard."""
    denied = {
        r
        for st in _statements(policy, "Deny")
        if "s3:PutObject" in _as_list(st["Action"])
        for r in _as_list(st["Resource"])
    }
    for marker in ("_VALIDATED.json", "_REJECTED.json"):
        assert any(
            r.startswith("arn:aws:s3:::edullm-landing/*") and r.endswith(marker) for r in denied
        ), f"{marker} is not denied bucket-wide"


# --------------------------------------------------------------------------------------
# What bounds a role that is not bounded by a dataset name
# --------------------------------------------------------------------------------------


def test_the_seven_families_are_exactly_the_section_2_enum(policy):
    """The grant's shape is only safe if the code refuses anything outside this list — so assert that
    against the real validator, not against a comment."""
    for fam in FAMILIES:
        assert validate_dataset_id(f"{fam}/some-real-name") == (fam, "some-real-name")
    for bad in ("_ingest", "_dist", "_staging", "_catalog", "nosuchfamily"):
        with pytest.raises(NamingError):
            validate_dataset_id(f"{bad}/some-real-name")


def test_every_family_can_be_published_and_nothing_else_can(policy):
    """Each of the seven has a write grant, and no write grant exists outside them."""
    granted = {
        r for r in _write_resources(policy) if r.startswith("arn:aws:s3:::edullm-landing/")
    }
    for fam in FAMILIES:
        assert f"arn:aws:s3:::edullm-landing/{fam}/*" in granted, f"{fam} cannot be published"
    allowed = {f"arn:aws:s3:::edullm-landing/{f}/*" for f in FAMILIES}
    assert granted == allowed, f"unexpected write grants: {sorted(granted - allowed)}"


def test_the_control_prefixes_are_denied_not_merely_unmentioned(policy):
    """`_dist/` holds bootstrap wheels and has NO lifecycle expiry, so a write there persists
    indefinitely and would be executed by a later job. `_ingest/` holds build output this role reads
    to hash — writable, it could alter payload after `verify --deep` had already passed, which is the
    pipeline's only payload re-hash and runs BEFORE publishing."""
    denied: dict[str, set[str]] = {}
    for st in _statements(policy, "Deny"):
        for r in _as_list(st["Resource"]):
            denied.setdefault(r, set()).update(_as_list(st["Action"]))
    for prefix in ("_dist", "_ingest", "_staging", "_catalog"):
        arn = f"arn:aws:s3:::edullm-landing/{prefix}/*"
        assert arn in denied, f"{prefix}/ is not explicitly denied"
        assert "s3:PutObject" in denied[arn], f"{prefix}/ PutObject is not denied"


def test_no_write_grant_uses_a_bare_landing_wildcard(policy):
    """`edullm-landing/*` would silently include every control prefix. The Denies above would still
    win, but a grant that relies on a Deny to be correct is one edit away from being wrong."""
    for r in _write_resources(policy):
        assert r != "arn:aws:s3:::edullm-landing/*", "bare landing wildcard in a write grant"
        assert r != "arn:aws:s3:::edullm-landing", "bucket-level write grant"


def test_build_output_is_readable_because_publish_stream_hashes_it(policy):
    """publish() hashes every source object to build the manifest — that read is not redundant with
    the server-side copy, and it is why publish must run in-region."""
    reads = {
        r
        for st in _statements(policy, "Allow")
        if "s3:GetObject" in _as_list(st["Action"])
        for r in _as_list(st["Resource"])
    }
    assert "arn:aws:s3:::edullm-landing/_ingest/*" in reads
    assert "arn:aws:s3:::edullm-landing/_staging/*" in reads


def test_published_tokenizers_and_catalog_are_readable(policy):
    """publish(tokenizer=...) binds a published tokenizer, and the next version is resolved from the
    catalog. Both live in edullm-data and both must be READ-only from here."""
    reads = {
        r
        for st in _statements(policy, "Allow")
        if "s3:GetObject" in _as_list(st["Action"])
        for r in _as_list(st["Resource"])
    }
    assert "arn:aws:s3:::edullm-data/_catalog/*" in reads
    assert "arn:aws:s3:::edullm-data/tokenizer/*" in reads


# --------------------------------------------------------------------------------------
# Trust
# --------------------------------------------------------------------------------------


def test_only_ecs_tasks_may_assume_it(trust):
    """This role can reserve a version and fire the validator for ANY family, so the trust boundary
    is what makes its breadth acceptable. The intern role here carries AdministratorAccess; IAM does
    not keep a human out of this identity, this policy does."""
    statements = _statements(trust)
    assert len(statements) == 1, statements
    st = statements[0]
    assert st["Effect"] == "Allow"
    assert st["Action"] == "sts:AssumeRole"
    assert st["Principal"] == {"Service": "ecs-tasks.amazonaws.com"}


def test_no_aws_principal_is_trusted(trust):
    """An `AWS` principal is the shape that would let a session — including an admin session —
    assume this role directly."""
    for st in _statements(trust):
        assert set(st["Principal"]) == {"Service"}, st["Principal"]


def test_trust_carries_no_condition_block(trust):
    """Deliberate and documented: Batch does not populate `aws:SourceArn` on the ECS task-assume
    call, so a SourceArn condition evaluates false and every job fails to start with an unhelpful
    credential error. Pinned so nobody 'hardens' it back into an outage."""
    for st in _statements(trust):
        assert "Condition" not in st, st
