"""The Batch build driver: plan determinism, ordinal uniqueness, and resume.

The resume tests are the ones that matter. A driver that skips work it did not do produces a corpus
that is short a shard, and nothing downstream notices until a training run's instance count comes up
wrong — so "receipt exists but its shards do not" is tested four ways, not one.

Everything here is offline: `FakeS3`, a whitespace tokenizer, and injected documents. No network, no
tokenizer download, no Batch.
"""

from __future__ import annotations

import random

import pytest

from edullm_data import corpus_build as B
from edullm_data.corpus import SHARD_TOKENS, BuildError, CorpusSpec, Document
from edullm_data.s3 import FakeS3

BUCKET = "edullm-landing"
PREFIX = "_ingest/test-build"


class WordTok:
    """Whitespace tokenizer with a wide id space.

    Wide on purpose: `corpus_pack._verify_shard` enforces the family's `distinct_ids_min` of 256 on
    a sampled window, so a toy tokenizer mapping a small alphabet produces shards that are REJECTED
    for looking degenerate. That check firing on a naive fixture is the check working; the fixture
    has to be realistic enough to clear it.
    """

    class E:
        def __init__(self, ids):
            self.ids = ids

    def _ids(self, text: str):
        return [(hash(w) % 99000) + 1 for w in text.split()]

    def encode(self, text, add_special_tokens=False):
        return WordTok.E(self._ids(text))

    def encode_batch(self, texts, add_special_tokens=False):
        return [self.encode(t) for t in texts]


def _spec(**over) -> CorpusSpec:
    base = dict(
        key="tiny", category="web-diverse", source_label="tiny", repo="acme/tiny",
        file_format="parquet", text_column="text", id_column="id",
        target_tokens=SHARD_TOKENS * 2, revision="a" * 40,
    )
    base.update(over)
    return CorpusSpec(**base)


#: Test shards are 2 sequences (16,384 tokens), not the real 3052 (25,001,984).
#:
#: The size is the only thing scaled down. `pack` takes the token count from each `ShardRef`, so
#: every code path — the carry buffer, the tail truncation, `_verify_shard`'s decode window, the
#: `bytes % (4*SEQ_LEN)` alignment — runs exactly as it does in production, on a shard small enough
#: that four tests do not spend 100 seconds pushing 60,000 documents through a Python tokenizer.
#: Testing at the real size measured 25s per test and proved nothing the small size does not.
TEST_SHARD_TOKENS = 2 * 8192


def _small(bundle):
    """The same bundle with production-sized refs swapped for `TEST_SHARD_TOKENS` ones."""
    import dataclasses

    return dataclasses.replace(
        bundle,
        shards=tuple(dataclasses.replace(r, tokens=TEST_SHARD_TOKENS) for r in bundle.shards),
    )


def _docs(n: int = 400, words: int = 300, seed: int = 3) -> list[Document]:
    rng = random.Random(seed)
    return [
        Document(id=f"d{i}", text=" ".join(f"w{rng.randrange(80000)}" for _ in range(words)),
                 source="tiny")
        for i in range(n)
    ]


def _run(bundle, plan, spec, s3, docs=None):
    return B.run_bundle(
        _small(bundle), plan, spec, s3=s3, bucket=BUCKET, prefix=PREFIX,
        documents=lambda sp, bu: docs if docs is not None else _docs(),
        tokenizer=WordTok(), eos_id=100257, vocab_size=100278, wheel_version="0.6.3",
    )


# --------------------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------------------


def test_the_plan_is_deterministic_and_its_id_is_a_content_address():
    """Same inputs, byte-identical plan. This is what makes `plan_id` mean anything.

    A timestamp in the document would change the id on every regeneration and destroy exactly the
    property the id exists to provide, so `plan_document` takes no clock.
    """
    specs = [_spec(), _spec(key="two", source_label="two", target_tokens=SHARD_TOKENS * 3)]
    a, b = B.plan_document(specs), B.plan_document(specs)
    assert a == b
    assert a["plan_id"] == b["plan_id"]
    assert B.plan_document(list(reversed(specs)))["plan_id"] == a["plan_id"], (
        "input ORDER must not change the plan, or two callers building the same plan differently "
        "would write different keys for the same data"
    )


def test_a_different_registry_gives_a_different_plan_id():
    a = B.plan_document([_spec()])
    b = B.plan_document([_spec(target_tokens=SHARD_TOKENS * 5)])
    assert a["plan_id"] != b["plan_id"]


def test_ordinals_are_unique_across_the_whole_plan():
    """The collision the plan exists to prevent.

    Two children each counting from zero produce `tokens/a/train-00000` and `tokens/b/train-00000`,
    which both parse fine and which no gate rejects.
    """
    specs = [
        _spec(key=k, source_label=k, target_tokens=SHARD_TOKENS * n)
        for k, n in (("alpha", 2), ("beta", 3), ("gamma", 2))
    ]
    plan = B.plan_document(specs)
    paths = [p for b in plan["bundles"] for p in b["shards"]]
    assert len(set(paths)) == len(paths)
    trains = sorted(int(p.rsplit("-", 1)[1].split(".")[0]) for p in paths if "/train-" in p)
    assert trains == list(range(len(trains))), "train ordinals must be dense and unique"


def test_a_source_too_small_for_one_val_shard_gets_no_val_split():
    """Stated in the plan, not hidden.

    Whole-shard selection means a partial val shard cannot be written, so a source under
    `SHARD_TOKENS / VAL_FRACTION` (5,000,396,800 tokens) has no held-out set at all. Nothing is
    lost and nothing leaks — its documents all go to train — but "which sources have no val data"
    must be answerable from the artifact afterwards.
    """
    small = _spec(key="small", source_label="small", target_tokens=SHARD_TOKENS * 4)
    plan = B.plan_document([small])
    assert plan["no_val_split"] == ["small"]
    assert not [b for b in plan["bundles"] if b["split"] == "val"]


def test_a_large_source_does_get_a_val_split():
    big = _spec(key="big", source_label="big", target_tokens=SHARD_TOKENS * 400)
    plan = B.plan_document([big])
    assert plan["no_val_split"] == []
    assert [b for b in plan["bundles"] if b["split"] == "val"]


def test_an_all_reserve_registry_is_refused():
    with pytest.raises(BuildError, match="reserve"):
        B.plan_document([_spec(target_tokens=0)])


def test_bundles_round_trip_through_the_plan():
    """A child reconstructs refs from the plan and never allocates one itself."""
    plan = B.plan_document([_spec()])
    bundle = B.bundles_of(plan)[0]
    assert bundle.stream == ("tiny", None, "train")
    assert [r.path for r in bundle.shards] == plan["bundles"][0]["shards"]


# --------------------------------------------------------------------------------------
# Unreadable sources are a PLAN-time failure
# --------------------------------------------------------------------------------------


def test_a_source_with_no_reader_fails_at_plan_time():
    """`dclm-baseline` is `jsonl.zst` and `corpus_read` refuses zstd.

    Plan time, not run time: discovering it mid-run means other bundles have already been built and
    paid for, and the corpus quietly lacks a whole category.
    """
    with pytest.raises(BuildError, match="no reader"):
        B._assert_readable([_spec(key="dclm", source_label="dclm", file_format="jsonl.zst")])


def test_a_readable_source_passes():
    B._assert_readable([_spec(), _spec(key="cp", source_label="cp", file_format="json.gz")])


# --------------------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------------------


def test_keys_never_use_a_reserved_basename():
    """`edullm-landing-manifest-created` matches the suffix `manifest.json` ANYWHERE in the bucket,
    so a build artifact under that name fires the validator against a prefix with no dataset.json."""
    from edullm_data.ingest_reservoir import IngestError

    assert B.receipt_key(PREFIX, "p1", "b1").endswith("_receipts/b1.json")
    with pytest.raises(IngestError):
        B.receipt_key(PREFIX, "p1", "../manifest")


# --------------------------------------------------------------------------------------
# Run + resume — the tests that matter
# --------------------------------------------------------------------------------------


def test_a_bundle_runs_end_to_end_and_writes_a_verified_receipt():
    s3 = FakeS3()
    plan = B.plan_document([_spec()])
    bundle = _small(B.bundles_of(plan)[0])
    info = _run(bundle, plan, _spec(), s3)
    assert info["shards"] == len(bundle.shards)
    assert info["tokens_out"] == len(bundle.shards) * TEST_SHARD_TOKENS
    written = [k for (b, k) in s3._store if b == BUCKET and k.endswith(".u32le.bin")]
    assert len(written) == len(bundle.shards)


def test_resume_is_true_only_when_the_receipt_AND_every_shard_survive():
    """THE trap this driver is built around, in four states.

    A worker that uploads some shards, writes its receipt, then dies leaves a bundle every later
    run declares finished. The receipt is not evidence; the objects are.
    """
    s3 = FakeS3()
    plan = B.plan_document([_spec()])
    pid = plan["plan_id"]
    bundle = _small(B.bundles_of(plan)[0])
    _run(bundle, plan, _spec(), s3)

    assert B.bundle_is_done(bundle, pid, s3, BUCKET, PREFIX) is True

    keys = sorted(k for (b, k) in s3._store if k.endswith(".u32le.bin"))
    body = s3.get(BUCKET, keys[0])

    s3._store.pop((BUCKET, keys[0]))
    assert B.bundle_is_done(bundle, pid, s3, BUCKET, PREFIX) is False, "a deleted shard is not done"

    # Truncated: the key EXISTS, so a presence-only check would pass it. This is why the check
    # compares sizes — `head` returns the length anyway, so it is strictly stronger for free.
    s3.seed(BUCKET, keys[0], body[:-4])
    assert B.bundle_is_done(bundle, pid, s3, BUCKET, PREFIX) is False, "a truncated shard is not done"

    s3.seed(BUCKET, keys[0], body)
    assert B.bundle_is_done(bundle, pid, s3, BUCKET, PREFIX) is True


def test_a_receipt_from_another_plan_does_not_count():
    """A receipt names its plan; one from a different plan is evidence about a different build."""
    s3 = FakeS3()
    plan = B.plan_document([_spec()])
    bundle = _small(B.bundles_of(plan)[0])
    _run(bundle, plan, _spec(), s3)
    assert B.bundle_is_done(bundle, "deadbeefdeadbeef", s3, BUCKET, PREFIX) is False


def test_a_missing_receipt_is_not_done():
    s3 = FakeS3()
    plan = B.plan_document([_spec()])
    assert B.bundle_is_done(_small(B.bundles_of(plan)[0]), plan["plan_id"], s3, BUCKET, PREFIX) is False


def test_the_receipt_is_written_only_after_its_shards_verify():
    """Order matters: a receipt over broken shards is how a later run skips broken work.

    Simulated with a sink-level failure — an S3 whose put silently drops one object, which is the
    shape of a partial upload.
    """
    class DroppingS3(FakeS3):
        def put(self, bucket, key, body, *, content_type=None):
            if key.endswith("train-00001.u32le.bin"):
                return  # silently dropped, exactly like an interrupted PUT
            super().put(bucket, key, body, content_type=content_type)

    s3 = DroppingS3()
    plan = B.plan_document([_spec()])
    bundle = B.bundles_of(plan)[0]
    with pytest.raises(BuildError, match="refusing to write a receipt"):
        _run(bundle, plan, _spec(), s3)
    assert not [k for (b, k) in s3._store if "_receipts" in k], "no receipt over failed shards"


# --------------------------------------------------------------------------------------
# The three driver obligations
# --------------------------------------------------------------------------------------


def test_the_driver_refuses_to_run_until_tokenizers_parallelism_is_decided(monkeypatch):
    """Neither default is safe, so the operator chooses.

    A library setting it would affect every importer including the validator; `"false"` throws away
    the rayon parallelism that makes a 255B-token tokenize affordable; leaving it unset in a forking
    driver is the documented HF deadlock.
    """
    monkeypatch.delenv("TOKENIZERS_PARALLELISM", raising=False)
    with pytest.raises(BuildError, match="TOKENIZERS_PARALLELISM"):
        B._assert_tokenizers_parallelism()
    monkeypatch.setenv("TOKENIZERS_PARALLELISM", "true")
    B._assert_tokenizers_parallelism()


def test_a_source_without_a_pinned_revision_is_refused_before_any_listing():
    """Reading `main` makes the build unreproducible, and the failure is silent."""
    with pytest.raises(BuildError, match="pinned revision"):
        B.hf_files(_spec(revision=None))


def test_the_pinned_resolve_url_never_says_main():
    url = B._resolve_pinned("acme/tiny", "b" * 40, "data/x.parquet")
    assert f"/resolve/{'b' * 40}/" in url
    assert "/resolve/main/" not in url


# --------------------------------------------------------------------------------------
# The registry the driver actually ships against
# --------------------------------------------------------------------------------------


def test_the_real_registry_plans_cleanly_and_reports_its_gaps():
    """An integration check against the committed registry, not a fixture.

    Asserts what a reader of the plan needs to trust: every drawn source HAS a reader, ordinals are
    globally unique across all of them, and the sources that silently get no val split are named.

    The readable assertion is the interesting one. It was `pytest.raises(match="no reader")` while
    `dclm-baseline` pointed at `mlfoundations/dclm-baseline-1.0` (`.jsonl.zst`, which `corpus_read`
    refuses) — a 30B hole. Now that the row points at `HuggingFaceFW/dclm_100BT`, where the pool
    measurement came from anyway, the whole registry is readable and this test says so.
    """
    specs, meta = B.load_registry()
    drawn = [s for s in specs if s.target_tokens > 0]
    B._assert_readable(drawn)  # every drawn source must have a reader — no exclusions

    plan = B.plan_document(drawn, registry_meta=meta)
    paths = [p for b in plan["bundles"] for p in b["shards"]]
    assert len(set(paths)) == len(paths), "ordinal collision across sources"
    assert plan["no_val_split"] == ["ubuntu-irc"], plan["no_val_split"]
    assert plan["registry_revisions_pinned_at"], "the plan must record which pin set it used"
    # ~10,400 objects at 25,001,984 tokens is what §2.2 sized Gate A (~1.4 h) against.
    assert 9_000 < len(paths) < 11_000, f"{len(paths)} shards is off the §2.2 sizing"
