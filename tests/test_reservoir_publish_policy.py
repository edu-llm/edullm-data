"""The reservoir PUBLISH role: permitted the two names the build role is denied, and nothing more.

Mirrors `infra/09-reservoir-publish-policy.json` and `-trust-policy.json`. Like
`test_reservoir_ingest_policy.py`, these read the shipped files and cannot prove the LIVE role
matches — that is the deploy runbook's job, because `iam:simulate-principal-policy` is documented in
`CLAUDE.md` as returning false denials for this account.

WHY A SECOND ROLE EXISTS, which is the thing a reader will want to delete. The obvious move is to
publish under `edullm-reservoir-ingest`, which already builds the corpus into the same bucket. It
cannot: that role explicitly DENIES `PutObject` on `*manifest.json` and `*dataset.json` anywhere in
landing, and those are precisely the two objects `publish()` writes (`publish.py:785` reserves the
version with a create-only `dataset.json`; `publish.py:813` writes each group's `manifest.json`).
The Deny is deliberate — a builder that could write a manifest could fire
`edullm-landing-manifest-created` against a half-built prefix — so the fix is a second role, not a
hole in the first.

That makes THIS role the only one in the reservoir line that can trigger the validator, so the
absences here are load-bearing: it must still be unable to write `edullm-data` (the airlock), unable
to forge a `_VALIDATED.json`/`_REJECTED.json` verdict, and unable to write outside its own dataset
prefix. An absence nobody checks is an absence that comes back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parent.parent / "infra"
POLICY_PATH = INFRA / "09-reservoir-publish-policy.json"
TRUST_PATH = INFRA / "09-reservoir-publish-trust-policy.json"
INGEST_POLICY_PATH = INFRA / "08-reservoir-ingest-policy.json"

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


@pytest.fixture(scope="module")
def ingest_policy() -> dict:
    return json.loads(INGEST_POLICY_PATH.read_text(encoding="utf-8"))


def _as_list(v) -> list[str]:
    return list(v) if isinstance(v, list) else [v]


def _statements(policy: dict, effect: str | None = None) -> list[dict]:
    out = [s for s in policy["Statement"] if not isinstance(s, str)]
    return [s for s in out if effect is None or s["Effect"] == effect]


# --------------------------------------------------------------------------------------
# The airlock: this role cannot write published data either
# --------------------------------------------------------------------------------------


def test_no_statement_allows_writing_the_published_bucket(policy):
    """THE test, same as the ingest role's. publish() stages into landing; promote() is the
    validator's job, so a write grant on edullm-data here would dissolve the airlock."""
    for st in _statements(policy, "Allow"):
        if set(_as_list(st["Action"])) & WRITE_ACTIONS:
            for r in _as_list(st["Resource"]):
                assert "edullm-data" not in r, f"{st['Sid']} would write {r}"


def test_the_data_bucket_write_deny_is_explicit(policy):
    """Not just absent-by-omission. An explicit Deny survives someone later attaching a broad
    managed policy to this role, which an absence does not."""
    denies = [
        st
        for st in _statements(policy, "Deny")
        if any("edullm-data" in r for r in _as_list(st["Resource"]))
    ]
    assert denies, "no explicit Deny covers edullm-data"
    actions = {a for st in denies for a in _as_list(st["Action"])}
    assert "s3:PutObject" in actions
    assert "s3:DeleteObject" in actions


def test_the_role_cannot_forge_a_validator_verdict(policy):
    """`_VALIDATED.json` / `_REJECTED.json` are the validator's terminal markers. A producer that
    could write one could make a rejected upload look promoted."""
    denied: set[str] = set()
    for st in _statements(policy, "Deny"):
        if "s3:PutObject" in _as_list(st["Action"]):
            denied.update(_as_list(st["Resource"]))
    for name in ("_VALIDATED.json", "_REJECTED.json"):
        assert any(name in r for r in denied), f"nothing denies writing {name}"


# --------------------------------------------------------------------------------------
# The reason this role exists at all
# --------------------------------------------------------------------------------------


def test_the_ingest_role_really_does_deny_what_publish_needs(ingest_policy):
    """The premise of this whole file, asserted against the OTHER policy rather than trusted.

    If someone later relaxes the build role's Deny, this test fails and says so — at which point the
    two roles could be merged. Until then the second role is not duplication.
    """
    denied: set[str] = set()
    for st in _statements(ingest_policy, "Deny"):
        if "s3:PutObject" in _as_list(st["Action"]):
            denied.update(_as_list(st["Resource"]))
    assert any("manifest.json" in r for r in denied), "ingest role no longer denies manifest.json"
    assert any("dataset.json" in r for r in denied), "ingest role no longer denies dataset.json"


def test_publish_may_write_its_own_dataset_prefix(policy):
    """The grant that the build role lacks: PutObject under `pretrain/reservoir-dolma2/`.

    Scoped to the dataset prefix, NOT to basenames. publish() writes dataset.json and manifest.json
    there, so a basename-level Deny like the build role's would break the thing this role is for.
    """
    grants = [
        st
        for st in _statements(policy, "Allow")
        if "s3:PutObject" in _as_list(st["Action"])
    ]
    assert grants, "this role cannot publish anything"
    resources = [r for st in grants for r in _as_list(st["Resource"])]
    assert all(r.startswith("arn:aws:s3:::edullm-landing/") for r in resources), resources
    assert any("pretrain/reservoir-dolma2/" in r for r in resources), resources


def test_publish_cannot_write_the_build_prefix_it_reads(policy):
    """One-way: it reads the built corpus to hash and copy it, and must not be able to rewrite it.

    A publish that could overwrite `_ingest/.../data/` could silently change payload bytes after the
    receipts were written, and `verify --deep` had already run — the one payload re-hash in the
    pipeline happens BEFORE this job, so nothing would catch it.
    """
    for st in _statements(policy, "Allow"):
        if set(_as_list(st["Action"])) & WRITE_ACTIONS:
            for r in _as_list(st["Resource"]):
                assert "_ingest/" not in r, f"{st['Sid']} could rewrite the built corpus: {r}"


def test_it_can_read_the_built_corpus_because_publish_stream_hashes_it(policy):
    """GetObject on the build prefix is not redundant with the server-side copy: publish() hashes
    every source object to build the manifest, which is why it must run in-region."""
    reads = [
        r
        for st in _statements(policy, "Allow")
        if "s3:GetObject" in _as_list(st["Action"])
        for r in _as_list(st["Resource"])
    ]
    assert any("_ingest/reservoir-dolma2/" in r for r in reads), reads


def test_it_can_read_the_published_tokenizer(policy):
    """publish(tokenizer="tokenizer/dolma2-bpe") binds a published tokenizer, so the job must be
    able to read it out of the data bucket — read only, never write."""
    reads = [
        r
        for st in _statements(policy, "Allow")
        if "s3:GetObject" in _as_list(st["Action"])
        for r in _as_list(st["Resource"])
    ]
    assert any("tokenizer/dolma2-bpe" in r for r in reads), reads


# --------------------------------------------------------------------------------------
# Trust: no human session can become this role
# --------------------------------------------------------------------------------------


def test_only_ecs_tasks_may_assume_it(trust):
    """The intern role here carries AdministratorAccess, so IAM is not what keeps a human out of
    this identity — the trust policy is. This is the one reservoir role that can fire the validator.
    """
    statements = _statements(trust)
    assert len(statements) == 1, statements
    st = statements[0]
    assert st["Effect"] == "Allow"
    assert st["Action"] == "sts:AssumeRole"
    assert st["Principal"] == {"Service": "ecs-tasks.amazonaws.com"}


def test_no_principal_other_than_a_service_is_trusted(trust):
    """No `AWS` principal at all — that is the shape that would let a session assume it."""
    for st in _statements(trust):
        assert set(st["Principal"]) == {"Service"}, st["Principal"]


def test_trust_carries_no_condition_block(trust):
    """Deliberate, and documented in the file: Batch does not populate `aws:SourceArn` on the ECS
    task-assume call, so a SourceArn condition evaluates false and every job fails to start with an
    unhelpful credential error. Pinned so nobody 'hardens' it back into an outage."""
    for st in _statements(trust):
        assert "Condition" not in st, st
