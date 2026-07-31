"""Dependency classification shared by publisher and validator."""

from __future__ import annotations

from collections.abc import Mapping


def is_tokenizer_dependency(dependency: object) -> bool:
    """Whether a dependency identifies a tokenizer artifact.

    Historical manifests sometimes omitted ``role: "tokenizer"`` while still pinning a
    ``tokenizer/...`` dataset. Treat those exactly like role-tagged dependencies everywhere:
    accepting them during validation but ignoring them during publish creates ambiguity.
    """
    if not isinstance(dependency, Mapping):
        return False
    role = str(dependency.get("role", ""))
    dataset_id = str(dependency.get("dataset_id", ""))
    return (
        role == "tokenizer"
        or dataset_id.startswith("tokenizer/")
        or (dataset_id.startswith("vendor/") and "tokenizer" in dataset_id)
    )
