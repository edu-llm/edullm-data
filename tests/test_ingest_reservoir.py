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
# The tree query — a performance defect that presents as a hang, so it needs a test
# --------------------------------------------------------------------------------------


def test_tree_query_does_not_use_expand(monkeypatch):
    """`expand=1` caps pages at 50 instead of 1000 and takes 26 s per page — measured ~1 hour per
    config vs 2.5 s. It is the obvious flag to add (the Phase 0c footer tool used it) and it
    presents as a hang, not an error, so nothing else in this suite would catch a regression.

    Asserts on the URL actually requested rather than on the source text.
    """
    seen: list[str] = []

    class _Resp:
        headers = {"Link": ""}

        def read(self):
            return json.dumps([{"path": "faq/000_0.parquet", "size": 123}]).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return _Resp()

    import edullm_data.ingest_reservoir as mod

    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen)
    out = mod.hf_tree("HuggingFaceFW/finephrase", "faq", headers={})
    assert out == [{"path": "faq/000_0.parquet", "size": 123}]
    assert len(seen) == 1
    assert "recursive=1" in seen[0]
    assert "expand" not in seen[0], f"expand=1 is a 50x pessimisation here: {seen[0]}"


def test_tree_follows_the_cursor_rather_than_trusting_one_page(monkeypatch):
    """Each config holds ~6,800 files across 7 pages; stopping at page 1 would silently report a
    corpus 7x smaller and every downstream fraction would still look self-consistent."""
    pages = [
        (json.dumps([{"path": f"faq/{i}.parquet", "size": 1} for i in range(1000)]).encode(),
         '<https://x?cursor=AAA>; rel="next"'),
        (json.dumps([{"path": "faq/last.parquet", "size": 1}]).encode(), ""),
    ]
    calls = {"n": 0}

    class _Resp:
        def __init__(self, body, link):
            self._b, self.headers = body, {"Link": link}

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        body, link = pages[calls["n"]]
        calls["n"] += 1
        return _Resp(body, link)

    import edullm_data.ingest_reservoir as mod

    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen)
    out = mod.hf_tree("HuggingFaceFW/finephrase", "faq", headers={})
    assert calls["n"] == 2
    assert len(out) == 1001


def test_tree_refuses_entries_without_size(monkeypatch):
    """`size` drives the Range reads. If the compact form ever stops carrying it, the fix is a
    HEAD per file — the guard exists so nobody reaches for expand=1 instead."""

    class _Resp:
        headers = {"Link": ""}

        def read(self):
            return json.dumps([{"path": "faq/a.parquet"}]).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import edullm_data.ingest_reservoir as mod

    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    with pytest.raises(IngestError, match="NOT expand=1"):
        mod.hf_tree("HuggingFaceFW/finephrase", "faq", headers={})


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# Sharding — an array job's file split
# --------------------------------------------------------------------------------------


def test_shards_partition_the_file_list_exactly():
    """Union of all shards == the whole list, with no item in two shards.

    An off-by-one here silently drops files from the anti-join set, and a smaller-than-expected
    id set does not look like an error — the ingest succeeds and edu-web quietly keeps documents
    that should have been removed.
    """
    from edullm_data.ingest_reservoir import _shard_slice

    for n in (1, 2, 3, 7, 20, 64):
        items = list(range(1_000))
        shards = [_shard_slice(items, i, n) for i in range(n)]
        flat = [x for s in shards for x in s]
        assert sorted(flat) == items
        assert len(flat) == len(set(flat))


def test_shards_are_striped_not_contiguous():
    """Contiguous blocks are the obvious split and the wrong one: FinePhrase files are name-ordered
    with sizes varying by an order of magnitude, so a contiguous slice can be all-large and the
    array's wall clock becomes the unluckiest child's."""
    from edullm_data.ingest_reservoir import _shard_slice

    assert _shard_slice(list(range(10)), 0, 3) == [0, 3, 6, 9]
    assert _shard_slice(list(range(10)), 1, 3) == [1, 4, 7]


def test_shards_are_balanced_within_one_item():
    from edullm_data.ingest_reservoir import _shard_slice

    sizes = [len(_shard_slice(list(range(1_000)), i, 7)) for i in range(7)]
    assert max(sizes) - min(sizes) <= 1


def test_out_of_range_shard_is_refused():
    from edullm_data.ingest_reservoir import _shard_slice

    for bad in (-1, 4, 99):
        with pytest.raises(IngestError, match="out of range"):
            _shard_slice([1, 2, 3], bad, 4)


def test_unsharded_is_the_identity():
    """`--of 1` must behave exactly as before sharding existed."""
    from edullm_data.ingest_reservoir import _shard_slice

    items = list(range(50))
    assert _shard_slice(items, 0, 1) == items


# --------------------------------------------------------------------------------------
# Rate limiting — the failure that actually happened
# --------------------------------------------------------------------------------------


def test_backoff_is_exponential_not_linear():
    """A 3*(n+1) linear retry gives up after 30 s and cannot outlast the Hub's 429 window.

    `PLAN-CORRECTIONS.md` §6 recorded exactly this bug in `recount.py` during Phase 0, and this
    module reintroduced it — the first real ingest job died with 8 of 20 files at HTTP 429.
    """
    from edullm_data.ingest_reservoir import _backoff_delay

    delays = [_backoff_delay(i) for i in range(6)]
    assert delays == [4.0, 8.0, 16.0, 32.0, 64.0, 120.0]  # doubling, then capped
    assert sum(delays) > 30.0, "must outlast the linear retry that already failed"


def test_backoff_honours_retry_after_when_the_server_sends_one():
    from edullm_data.ingest_reservoir import _backoff_delay

    assert _backoff_delay(0, "45") == 45.0
    assert _backoff_delay(5, "7") == 7.0  # server's answer wins over our schedule


def test_backoff_ignores_an_unparseable_retry_after():
    """`Retry-After` may be an HTTP date. Rather than mis-parse it into a wrong delay, fall back to
    the exponential schedule."""
    from edullm_data.ingest_reservoir import _backoff_delay

    assert _backoff_delay(2, "Wed, 21 Oct 2026 07:28:00 GMT") == 16.0


def test_backoff_is_capped():
    from edullm_data.ingest_reservoir import _BACKOFF_CAP_S, _backoff_delay

    assert _backoff_delay(30) == _BACKOFF_CAP_S


def test_rate_gate_pauses_every_thread_not_just_the_one_that_was_limited():
    """THE point of the gate. The HF limit is per-IP, so a worker backing off privately while
    fifteen others keep hammering has changed nothing — the limit belongs to the fleet."""
    import time as _t

    from edullm_data.ingest_reservoir import _RateGate

    gate = _RateGate()
    gate.penalise(0.3)
    t0 = _t.monotonic()
    gate.wait()  # a DIFFERENT caller than the one that was penalised
    assert _t.monotonic() - t0 >= 0.25
    assert gate.total_penalties == 1


def test_rate_gate_does_not_block_when_nothing_is_penalised():
    import time as _t

    from edullm_data.ingest_reservoir import _RateGate

    t0 = _t.monotonic()
    _RateGate().wait()
    assert _t.monotonic() - t0 < 0.1


def test_rate_gate_keeps_the_latest_deadline():
    """Two 429s should not shorten the pause to the second one's."""
    from edullm_data.ingest_reservoir import _RateGate

    gate = _RateGate()
    gate.penalise(60.0)
    gate.penalise(0.01)
    assert gate._until > 0 and gate.total_penalties == 2


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def test_merge_requires_of(capsys):
    with pytest.raises(SystemExit):
        main(["merge", "--run-id", "x"])


# --------------------------------------------------------------------------------------
# Merge — the refusal matters more than the concatenation
# --------------------------------------------------------------------------------------


class _MergeS3:
    """Minimal S3 double holding shard parts in memory."""

    def __init__(self, parts: dict):
        self.parts = dict(parts)
        self.written: dict = {}

    def list_objects_v2(self, **kw):
        keys = [k for k in self.parts if k.startswith(kw["Prefix"])]
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def get_object(self, Bucket, Key):  # noqa: N803
        import io as _io

        return {"Body": _io.BytesIO(self.parts[Key])}

    def put_object(self, Bucket, Key, Body, **kw):  # noqa: N803
        self.written[Key] = Body


def _mk_parts(of: int, per_shard: int = 100, overlap: int = 0) -> dict:
    """Build `of` shard parts per config; `overlap` ids are repeated across every shard."""
    from edullm_data.ingest_reservoir import FINEPHRASE_FORMATS, IdSet

    out = {}
    for cfg in FINEPHRASE_FORMATS:
        for i in range(of):
            ids = [f"<urn:uuid:{cfg}-{i}-{j}>" for j in range(per_shard)]
            ids += [f"<urn:uuid:{cfg}-shared-{j}>" for j in range(overlap)]
            key = f"_ingest/reservoir-dolma2/_ids/parts/finephrase-{cfg}.{i:05d}-of-{of:05d}.u64"
            out[key] = IdSet.from_ids(ids).to_bytes()
    return out


def _merge_args(of: int):
    import argparse

    return argparse.Namespace(
        bucket="edullm-landing",
        prefix="_ingest/reservoir-dolma2",
        run_id="test",
        of=of,
        allow_local=True,
    )


def test_merge_refuses_an_incomplete_part_set(monkeypatch):
    """THE reason merge exists as a separate step with a check.

    A missing part yields a SMALLER anti-join set, and nothing about that looks like an error: the
    merge succeeds, the counts are plausible, and edu-web silently keeps documents that should
    have been dropped. Missing parts must be loud.
    """
    import edullm_data.ingest_reservoir as mod

    parts = _mk_parts(of=4)
    dropped = next(k for k in parts if "faq" in k and "00002-of" in k)
    del parts[dropped]
    fake = _MergeS3(parts)
    monkeypatch.setattr(mod, "_require_batch", lambda **_: None)
    monkeypatch.setitem(__import__("sys").modules, "boto3", type("B", (), {"client": staticmethod(lambda *_a, **_k: fake)}))

    with pytest.raises(IngestError, match="shard parts are missing"):
        mod._cmd_merge(_merge_args(4))
    assert not fake.written, "nothing may be written when the set is incomplete"


def test_merge_combines_and_deduplicates_across_shards(monkeypatch):
    import edullm_data.ingest_reservoir as mod
    from edullm_data.ingest_reservoir import FINEPHRASE_FORMATS, IdSet

    fake = _MergeS3(_mk_parts(of=4, per_shard=100, overlap=10))
    monkeypatch.setattr(mod, "_require_batch", lambda **_: None)
    monkeypatch.setitem(__import__("sys").modules, "boto3", type("B", (), {"client": staticmethod(lambda *_a, **_k: fake)}))

    assert mod._cmd_merge(_merge_args(4)) == 0
    for cfg in FINEPHRASE_FORMATS:
        key = f"_ingest/reservoir-dolma2/_ids/finephrase-{cfg}.u64"
        merged = IdSet.from_bytes(fake.written[key])
        # 4 shards x 100 unique + 10 shared (counted once) = 410
        assert len(merged) == 410, cfg


def test_merge_output_is_sorted_so_membership_works(monkeypatch):
    """`contains` uses searchsorted; an unsorted merge silently returns wrong answers."""
    import edullm_data.ingest_reservoir as mod
    from edullm_data.ingest_reservoir import IdSet

    fake = _MergeS3(_mk_parts(of=3))
    monkeypatch.setattr(mod, "_require_batch", lambda **_: None)
    monkeypatch.setitem(__import__("sys").modules, "boto3", type("B", (), {"client": staticmethod(lambda *_a, **_k: fake)}))
    mod._cmd_merge(_merge_args(3))

    raw = fake.written["_ingest/reservoir-dolma2/_ids/finephrase-faq.u64"]
    IdSet.from_bytes(raw)  # raises if unsorted
    ids = IdSet.from_bytes(raw)
    assert ids.contains("<urn:uuid:faq-2-7>")
    assert not ids.contains("<urn:uuid:faq-99-99>")


def test_merge_writes_a_summary_not_a_manifest(monkeypatch):
    """`_merge-summary.json`, never `manifest.json` — the EventBridge suffix rule."""
    import edullm_data.ingest_reservoir as mod

    fake = _MergeS3(_mk_parts(of=2))
    monkeypatch.setattr(mod, "_require_batch", lambda **_: None)
    monkeypatch.setitem(__import__("sys").modules, "boto3", type("B", (), {"client": staticmethod(lambda *_a, **_k: fake)}))
    mod._cmd_merge(_merge_args(2))

    assert "_ingest/reservoir-dolma2/_ids/_merge-summary.json" in fake.written
    assert not any(k.endswith("manifest.json") for k in fake.written)


def test_default_workers_is_conservative():
    """The limit is per-IP: the default must not encourage a retry storm. 16 produced 8/20 failures
    on the first real run."""
    import argparse

    from edullm_data.ingest_reservoir import _build_parser

    ap: argparse.ArgumentParser = _build_parser()
    ns = ap.parse_args(["ids", "--run-id", "x"])
    assert ns.workers <= 8
    assert ns.of == 1 and ns.shard == 0


def test_errors_exit_nonzero_rather_than_raising(monkeypatch, capsys):
    """A Batch job must fail with a status, not a traceback that logs as a crash."""
    monkeypatch.delenv("AWS_BATCH_JOB_ID", raising=False)
    rc = main(["ids", "--run-id", "test", "--bucket", "edullm-landing"])
    assert rc == 2
    assert "not a Batch job" in capsys.readouterr().err
