"""Tests for the reservoir ingest driver's guards and its id-set structure.

Mirrors `src/edullm_data/ingest_reservoir.py`. Nothing here touches the network or AWS: the
guards and the `IdSet` are the parts that can be wrong in a way that silently corrupts a build,
and they are all pure. The HF transport is exercised out of band by the `plan` subcommand.

The two guards under test both defend against a failure that returns SUCCESS at the time it
happens and only shows up later — an EventBridge rule firing on a build artifact, and a 2.5 TB
prefix that nothing expires. Those are exactly the shape this project's audit was created to find.
"""

from __future__ import annotations

import json

import pytest

from edullm_data.ingest_reservoir import (
    FINEPHRASE_REPO,
    REWRITE_LEAF,
    IdSet,
    IngestError,
    _assert_lifecycle_covers,
    _assert_safe_key,
    _require_batch,
    main,
)


# --------------------------------------------------------------------------------------
# Guard: the EventBridge landmine
# --------------------------------------------------------------------------------------


def test_reserved_basenames_are_refused():
    """`edullm-landing-manifest-created` matches suffix `manifest.json` with NO prefix
    constraint, so this basename anywhere under landing fires the validator."""
    for base in ("manifest.json", "dataset.json", "_VALIDATED.json", "_REJECTED.json"):
        with pytest.raises(IngestError, match="reserved"):
            _assert_safe_key(f"_ingest/reservoir-dolma2/_ids/{base}")


def test_the_keys_the_driver_actually_writes_are_accepted():
    """The complement of the check above — a guard that rejected everything would also pass a
    test that only checked rejection."""
    for key in (
        "_ingest/reservoir-dolma2/_ids/finephrase-faq.u64",
        "_ingest/reservoir-dolma2/_ids/_index.json",
    ):
        assert _assert_safe_key(key) == key


def test_a_manifest_json_nested_deeper_is_still_refused():
    """The EventBridge pattern is a SUFFIX match, so depth does not help."""
    with pytest.raises(IngestError, match="reserved"):
        _assert_safe_key("_ingest/a/b/c/d/manifest.json")


# --------------------------------------------------------------------------------------
# Guard: the unexpiring-prefix landmine
# --------------------------------------------------------------------------------------


class _FakeS3:
    def __init__(self, rules):
        self._rules = rules

    def get_bucket_lifecycle_configuration(self, Bucket):  # noqa: N803 - boto3 kwarg name
        if self._rules is None:
            raise RuntimeError("An error occurred (NoSuchLifecycleConfiguration) when calling ...")
        return {"Rules": self._rules}


_LIVE_RULES = [
    {"ID": "expire-pretrain-14d", "Filter": {"Prefix": "pretrain/"}, "Status": "Enabled",
     "Expiration": {"Days": 14}},
    {"ID": "expire-vendor-14d", "Filter": {"Prefix": "vendor/"}, "Status": "Enabled",
     "Expiration": {"Days": 14}},
    {"ID": "abort-incomplete-multipart-uploads-1d", "Filter": {"Prefix": ""}, "Status": "Enabled",
     "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}},
]


def test_ingest_prefix_is_refused_under_the_live_rule_set():
    """This is the ACTUAL deployed configuration as of 2026-07-31: seven prefix-scoped expiry
    rules, none covering `_ingest/`. Staging 2.5 TB there would never expire."""
    with pytest.raises(IngestError, match="no enabled Expiration lifecycle rule"):
        _assert_lifecycle_covers(_FakeS3(_LIVE_RULES), "edullm-landing", "_ingest/reservoir-dolma2/")


def test_a_covered_prefix_passes():
    assert _assert_lifecycle_covers(_FakeS3(_LIVE_RULES), "edullm-landing", "pretrain/x/") is None


def test_the_multipart_abort_rule_does_not_count_as_expiry():
    """Its Filter.Prefix is "" so it matches every key, but it has no Expiration — it aborts
    incomplete uploads. Treating it as coverage would defeat the whole check."""
    only_abort = [r for r in _LIVE_RULES if "Expiration" not in r]
    with pytest.raises(IngestError, match="no enabled Expiration"):
        _assert_lifecycle_covers(_FakeS3(only_abort), "edullm-landing", "_ingest/x/")


def test_a_disabled_rule_does_not_count():
    disabled = [{**_LIVE_RULES[0], "ID": "off", "Filter": {"Prefix": "_ingest/"},
                 "Status": "Disabled"}]
    with pytest.raises(IngestError, match="no enabled Expiration"):
        _assert_lifecycle_covers(_FakeS3(disabled), "edullm-landing", "_ingest/x/")


def test_a_bucket_with_no_lifecycle_at_all_is_refused_not_crashed():
    """`NoSuchLifecycleConfiguration` is an error from botocore, not an empty result."""
    with pytest.raises(IngestError, match="no enabled Expiration"):
        _assert_lifecycle_covers(_FakeS3(None), "edullm-landing", "_ingest/x/")


def test_the_shipped_lifecycle_json_covers_the_ingest_prefix():
    """The fix file must actually satisfy the guard it exists to satisfy."""
    with open("infra/07-landing-ingest-lifecycle.json") as fh:
        conf = json.load(fh)
    assert _assert_lifecycle_covers(
        _FakeS3(conf["Rules"]), "edullm-landing", "_ingest/reservoir-dolma2/"
    ) is None


# --------------------------------------------------------------------------------------
# Guard: no corpus bytes off Batch (§5.7)
# --------------------------------------------------------------------------------------


def test_payload_work_refuses_to_run_outside_batch(monkeypatch):
    monkeypatch.delenv("AWS_BATCH_JOB_ID", raising=False)
    with pytest.raises(IngestError, match="not a Batch job"):
        _require_batch(allow_local=False)


def test_batch_guard_passes_inside_batch(monkeypatch):
    monkeypatch.setenv("AWS_BATCH_JOB_ID", "abc-123")
    assert _require_batch(allow_local=False) is None


def test_allow_local_bypasses_the_guard_for_metadata_only_work(monkeypatch):
    monkeypatch.delenv("AWS_BATCH_JOB_ID", raising=False)
    assert _require_batch(allow_local=True) is None


# --------------------------------------------------------------------------------------
# IdSet — the anti-join structure
# --------------------------------------------------------------------------------------


def _ids(n, start=0):
    return [f"<urn:uuid:0000000{i:08d}-0000-4000-8000-000000000000>" for i in range(start, start + n)]


def test_membership_is_exact_for_present_and_absent_ids():
    s = IdSet.from_ids(_ids(5_000))
    for i in _ids(5_000):
        assert s.contains(i)
    for i in _ids(500, start=90_000):
        assert not s.contains(i)


def test_duplicate_ids_collapse():
    ids = _ids(1_000)
    assert len(IdSet.from_ids(ids + ids)) == len(IdSet.from_ids(ids)) == 1_000


def test_round_trips_through_bytes():
    s = IdSet.from_ids(_ids(2_000))
    back = IdSet.from_bytes(s.to_bytes())
    assert len(back) == len(s)
    assert all(back.contains(i) for i in _ids(2_000))


def test_truncated_id_set_is_refused():
    """A short multipart upload would otherwise silently produce a smaller anti-join set."""
    raw = IdSet.from_ids(_ids(100)).to_bytes()
    with pytest.raises(IngestError, match="not a multiple of 8"):
        IdSet.from_bytes(raw[:-3])


def test_unsorted_id_set_is_refused():
    """`contains` uses searchsorted, so an unsorted array returns wrong answers silently."""
    import numpy as np

    bad = np.array([5, 3, 1], dtype="<u8").tobytes()
    with pytest.raises(IngestError, match="not sorted"):
        IdSet.from_bytes(bad)


# --------------------------------------------------------------------------------------
# The wrong-column trap (§3.3 trap 1)
# --------------------------------------------------------------------------------------


def test_rewrite_leaf_is_the_full_nested_path_not_a_bare_name():
    """A bare `text` resolves to the ORIGINAL FineWeb-Edu document, and no hash or token count
    catches the substitution. The constant must carry the full path_in_schema."""
    assert REWRITE_LEAF == "rollout_results.list.element.text"
    assert REWRITE_LEAF != "text"
    assert REWRITE_LEAF.count(".") == 3


def test_leaf_resolution_refuses_a_missing_leaf_rather_than_falling_back():
    """Exact-match only: a bare-name fallback is precisely how the original gets ingested."""
    from edullm_data.ingest_reservoir import _leaf_index

    class _RG:
        num_columns = 2

        def column(self, i):
            class _C:
                path_in_schema = ["text", "id"][i]
            return _C()

    class _MD:
        def row_group(self, _i):
            return _RG()

    with pytest.raises(IngestError, match="Refusing a bare-name fallback"):
        _leaf_index(_MD(), REWRITE_LEAF)


def test_upstream_repo_is_pinned_to_the_measured_one():
    assert FINEPHRASE_REPO == "HuggingFaceFW/finephrase"


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def test_errors_exit_nonzero_rather_than_raising(monkeypatch, capsys):
    """A Batch job must fail with a status, not a traceback that logs as a crash."""
    monkeypatch.delenv("AWS_BATCH_JOB_ID", raising=False)
    rc = main(["ids", "--run-id", "test", "--bucket", "edullm-landing"])
    assert rc == 2
    assert "not a Batch job" in capsys.readouterr().err
