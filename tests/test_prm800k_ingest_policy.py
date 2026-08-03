"""Least-privilege contract for the separate PRM800K ingestion Batch role."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
POLICY = json.loads(
    (ROOT / "infra" / "06-prm800k-ingest-policy.json").read_text(encoding="utf-8")
)
TRUST = json.loads(
    (ROOT / "infra" / "06-prm800k-ingest-trust-policy.json").read_text(encoding="utf-8")
)


def _statement(sid: str) -> dict:
    return next(
        statement for statement in POLICY["Statement"] if statement["Sid"] == sid
    )


def _actions(statement: dict) -> set[str]:
    actions = statement["Action"]
    return set(actions if isinstance(actions, list) else [actions])


def test_ingest_role_cannot_delete_or_abort_another_runs_staging_objects():
    staging = _statement("ReadWriteOnlyPrm800kStaging")
    assert (
        staging["Resource"]
        == "arn:aws:s3:::edullm-landing/_staging/vendor/openai-prm800k/*"
    )
    assert not _actions(staging) & {"s3:DeleteObject", "s3:AbortMultipartUpload"}


def test_ingest_role_never_receives_a_published_bucket_write_permission():
    for statement in POLICY["Statement"]:
        resources = statement["Resource"]
        resources = resources if isinstance(resources, list) else [resources]
        if any(
            resource.startswith("arn:aws:s3:::edullm-data") for resource in resources
        ):
            assert not _actions(statement) & {
                "s3:PutObject",
                "s3:DeleteObject",
                "s3:AbortMultipartUpload",
            }


def test_ingest_role_cannot_suppress_validator_discovery_with_terminal_markers():
    deny = _statement("NeverWriteValidatorTerminalMarkers")
    assert deny["Effect"] == "Deny"
    assert _actions(deny) == {"s3:PutObject"}
    assert set(deny["Resource"]) == {
        "arn:aws:s3:::edullm-landing/vendor/openai-prm800k/v1/_VALIDATED.json",
        "arn:aws:s3:::edullm-landing/vendor/openai-prm800k/v1/_REJECTED.json",
    }


def test_ingest_role_does_not_bootstrap_code_from_mutable_landing():
    assert all(
        "/_dist/" not in str(statement["Resource"]) for statement in POLICY["Statement"]
    )


def test_published_state_read_is_limited_to_retry_verification_controls():
    published = _statement("ReadOnlyPrm800kPublishedState")
    assert set(published["Resource"]) == {
        "arn:aws:s3:::edullm-data/_catalog/vendor/openai-prm800k/v1.json",
        "arn:aws:s3:::edullm-data/vendor/openai-prm800k/v1/_VALIDATED.json",
        "arn:aws:s3:::edullm-data/vendor/openai-prm800k/v1/dataset.json",
        "arn:aws:s3:::edullm-data/vendor/openai-prm800k/v1/raw/manifest.json",
    }


def test_published_state_listing_is_limited_to_three_exact_absence_checks():
    listed = _statement("ListOnlyPrm800kPublishedExistenceChecks")
    assert listed["Resource"] == "arn:aws:s3:::edullm-data"
    assert _actions(listed) == {"s3:ListBucket"}
    assert listed["Condition"] == {
        "StringEquals": {
            "s3:prefix": [
                "_catalog/vendor/openai-prm800k/v1.json",
                "vendor/openai-prm800k/v1/_VALIDATED.json",
                "vendor/openai-prm800k/v1/dataset.json",
            ]
        }
    }


def test_ingest_role_is_service_assumable_by_batch_tasks_not_a_human_principal():
    statement = TRUST["Statement"][0]
    assert statement["Principal"] == {"Service": "ecs-tasks.amazonaws.com"}
    assert statement["Action"] == "sts:AssumeRole"
