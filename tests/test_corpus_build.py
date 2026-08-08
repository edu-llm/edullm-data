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

    Wide on purpose: `corpus_pack._verify_shard` enforces the family's `distinct_ids_min` on
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


def test_contaminated_and_duplicate_documents_never_reach_a_shard():
    """The filter is wired in, not merely importable.

    Runs the real pipeline over documents that are one-third duplicates and one-third benchmark
    text, and asserts both are gone from the receipt's own accounting. A driver that imported
    `corpus_filter` and forgot to call it would pass every test in `test_corpus_filter.py`.
    """
    from edullm_data.corpus_filter import DecontaminationIndex, _ngram_hash, _words, content_hash

    leak = "the mitochondrion is the powerhouse of the cell and everyone knows it"
    ngrams = set()
    w = _words(leak)
    for i in range(len(w) - 12):
        ngrams.add(_ngram_hash(w[i:i + 13]))
    index = DecontaminationIndex(frozenset({content_hash(leak)}), frozenset(ngrams), 13, 2)

    rng = random.Random(11)
    uniq = [
        Document(id=f"u{i}", text=" ".join(f"w{rng.randrange(80000)}" for _ in range(300)),
                 source="tiny")
        for i in range(400)
    ]
    docs = uniq + [Document(id=f"dup{i}", text=d.text, source="tiny") for i, d in enumerate(uniq)]
    docs += [Document(id=f"leak{i}", text=leak, source="tiny") for i in range(50)]

    s3 = FakeS3()
    plan = B.plan_document([_spec()])
    bundle = _small(B.bundles_of(plan)[0])
    info = B.run_bundle(
        bundle, plan, _spec(), s3=s3, bucket=BUCKET, prefix=PREFIX,
        documents=lambda sp, bu: docs, tokenizer=WordTok(), eos_id=100257,
        vocab_size=100278, wheel_version="0.6.3", index=index,
    )
    f = info["filter"]
    # `seen` is what reached the FILTER, which is less than len(docs): the val carve runs first and
    # routes ~0.5% elsewhere (measured exactly 3 of these 850, since carve is a pure function of the
    # id). Asserting len(docs) here would be asserting the wrong stage's count.
    assert f["seen"] < len(docs)
    assert f["seen"] == f["kept"] + f["duplicates"] + f["contaminated"], "every document accounted"
    assert f["duplicates"] > 350, f"repeated documents must be dropped, got {f}"
    # The 50 identical leaks: the FIRST is contamination, the other 49 are duplicates of it. Dedup
    # runs first precisely so the expensive n-gram check sees each distinct text once.
    assert f["contaminated"] == 1, f
    assert not any(d.text == leak for d in uniq), "the fixture must not leak into the kept set"
    assert f["normalization"] == "week1-nfc-rstrip-v1"


# --------------------------------------------------------------------------------------
# The read budget — why all 14 sources used to raise before writing a shard
# --------------------------------------------------------------------------------------


def test_the_reader_stops_instead_of_walking_a_pool_far_larger_than_the_plan():
    """THE blocker that made every bundle unrunnable, and it is not a performance concern.

    The registry draws 252B tokens from a 1,094B pool. `_reader_for` used to iterate every file in
    the repo, so `pack` was handed thousands of shards' worth of documents it had no refs for —
    and `corpus_pack._drain_surplus` REFUSES a surplus of one whole shard, because discarding
    already-tokenized tokens means the plan disagrees with reality. Measured before the budget
    existed: all 14 drawn sources raised, 11 of them needing an impossible 46-90% filter loss to
    come under the threshold.

    Asserted through file COUNT rather than a token total, because the stop is between files by
    design — a mid-file cut would make the document set depend on where the budget ran out.
    """
    files_read = []

    def reader(repo, entry, spec):
        files_read.append(entry["path"])
        # Each "file" carries a whole shard's worth of characters at the assumed 4.0 chars/token.
        for i in range(200):
            yield Document(id=f"{entry['path']}-{i}",
                           text="x" * (SHARD_TOKENS * 4 // 200), source="tiny")

    spec = _spec(target_tokens=SHARD_TOKENS)          # ONE shard planned
    plan = B.plan_document([spec])
    bundle = B.bundles_of(plan)[0]

    # 500 files available; the budget must stop long before the end.
    tree = [{"path": f"data/{i:05d}.parquet", "size": 1000} for i in range(500)]
    import edullm_data.corpus_build as mod
    real_hf_files, real_readers = mod.hf_files, None
    mod.hf_files = lambda sp, headers=None: tree
    try:
        from edullm_data import corpus_read
        real_readers = corpus_read.read_parquet_documents
        corpus_read.read_parquet_documents = reader
        docs = list(mod._reader_for(spec, bundle))
    finally:
        mod.hf_files = real_hf_files
        if real_readers is not None:
            corpus_read.read_parquet_documents = real_readers

    assert len(files_read) < 500, "the reader must stop, not walk the whole pool"
    # 1 shard x 4.0 chars/token x 1.5 headroom / 0.995 keep = ~1.5 files' worth.
    assert len(files_read) <= 4, f"read {len(files_read)} files for a 1-shard bundle"
    assert docs, "it must still yield something"


def test_a_val_bundle_reads_far_more_than_it_keeps():
    """A val bundle's budget is scaled by 1/val_fraction, and without that it reads ~0.5% of what
    it needs and every one of its shards comes up empty.

    There is no cheaper way: `corpus.is_held_out` hashes the document id, so a held-out document
    cannot be located without reading the train documents interleaved with it.
    """
    big = _spec(key="big", source_label="big", target_tokens=SHARD_TOKENS * 400)
    plan = B.plan_document([big])
    train = next(b for b in B.bundles_of(plan) if b.split == "train")
    val = next(b for b in B.bundles_of(plan) if b.split == "val")

    assert val.keep_rate == plan["val_fraction"]
    assert train.keep_rate == 1.0 - plan["val_fraction"]
    # Per PLANNED token, a val bundle must read ~200x what a train bundle does.
    train_per_tok = 1.0 / train.keep_rate
    val_per_tok = 1.0 / val.keep_rate
    assert val_per_tok / train_per_tok > 190


def test_bundle_tokens_is_derived_from_its_refs_not_stored():
    """`_reader_for` sizes its budget from `bundle.tokens`, and the driver tests rescale refs to
    TEST_SHARD_TOKENS. A stored total would describe a different bundle than the one packed."""
    plan = B.plan_document([_spec()])
    bundle = B.bundles_of(plan)[0]
    assert bundle.tokens == sum(r.tokens for r in bundle.shards) == SHARD_TOKENS * 2
    assert _small(bundle).tokens == TEST_SHARD_TOKENS * 2


# --------------------------------------------------------------------------------------
# The FinePhrase id partition — the one thing here that cannot be retrofitted
# --------------------------------------------------------------------------------------
#
# The four FinePhrase configs are ONE corpus rephrased four ways over the same ~339M FineWeb-Edu
# documents (MEASURED 91.0-92.9% pairwise id overlap; 26.83% distinct over a 287,000-id read).
# Nothing downstream can catch it: four rephrasings are four different strings, so exact dedup,
# MinHash and every token count all pass. After tokenization there is no document -> id mapping
# left, so `_reader_for` is the last place it can be fixed.
#
# `reservoir_ids.keeps_id` was tested and verified balanced to 0.27pp for weeks while having exactly
# one caller, a JSON reporting function. So these tests deliberately assert on the DOCUMENTS THAT
# COME OUT of the real `_reader_for`, recomputed against `partition_of` — never that `keeps_id` was
# called.

FINEPHRASE_REPO = "HuggingFaceFW/finephrase"


def _fp_spec(config: str = "faq", **over) -> CorpusSpec:
    """A registry-shaped FinePhrase row: the real repo, the real nested leaf, one config."""
    base = dict(
        key=f"finephrase-{config}", category="synthetic",
        source_label=f"synthetic-finephrase-{config}", repo=FINEPHRASE_REPO,
        file_format="parquet", text_column="rollout_results.list.element.text", id_column="id",
        config=config, target_tokens=SHARD_TOKENS, revision="b" * 40,
    )
    base.update(over)
    return CorpusSpec(**base)


def _run_reader(spec, bundle, docs_per_file, n_files=400):
    """Drive the REAL `_reader_for` over a fake HF tree, returning (docs_out, files_read).

    `docs_per_file(path)` yields the documents one "file" holds. Patches `hf_files` and the parquet
    reader, i.e. exactly the two things that need a network — everything between them is the
    production code path.
    """
    import edullm_data.corpus_build as mod
    from edullm_data import corpus_read

    files_read = []

    def reader(repo, entry, sp):
        files_read.append(entry["path"])
        yield from docs_per_file(entry["path"])

    tree = [{"path": f"data/{i:05d}.parquet", "size": 1000} for i in range(n_files)]
    real_hf_files, real_reader = mod.hf_files, corpus_read.read_parquet_documents
    mod.hf_files = lambda sp, headers=None: tree
    corpus_read.read_parquet_documents = reader
    try:
        out = list(mod._reader_for(spec, bundle))
    finally:
        mod.hf_files = real_hf_files
        corpus_read.read_parquet_documents = real_reader
    return out, files_read


def test_the_reader_keeps_exactly_the_ids_this_finephrase_config_owns():
    """THE blocker. Feed one known id set through the reader four times, once per config, and
    assert the survivors are EXACTLY the ids `partition_of` assigns to that config.

    Recomputed from `partition_of` on the spot, not compared against a stored expectation and not
    asserted via a spy on `keeps_id`. A mock that records the call would pass even if the reader
    threw the result away, which is precisely the failure this test exists to rule out: the
    partition code has been green and uncalled since it was written.
    """
    from edullm_data.reservoir_ids import FINEPHRASE_FORMATS, partition_of

    ids = [f"<urn:uuid:0000-{i:06d}>" for i in range(4_000)]
    text = "lorem ipsum dolor " * 40

    def files(_path):
        for doc_id in ids:
            yield Document(id=doc_id, text=text, source="synthetic-finephrase")

    seen_by_format = {}
    for fmt in FINEPHRASE_FORMATS:
        spec = _fp_spec(fmt)
        bundle = _small(B.bundles_of(B.plan_document([spec]))[0])
        out, _ = _run_reader(spec, bundle, files, n_files=1)
        got = {d.id for d in out}
        want = {i for i in ids if FINEPHRASE_FORMATS[partition_of(i)] == fmt}
        assert got == want, f"{fmt}: kept {len(got)} ids, partition assigns {len(want)}"
        assert got, f"{fmt} kept nothing — a partition that drops everything reports success"
        seen_by_format[fmt] = got

    # DISJOINT AND COMPLETE. This is the property the whole blocker is about: drawing all four
    # configs must yield each parent document ONCE, not four times.
    union = set().union(*seen_by_format.values())
    assert sum(len(v) for v in seen_by_format.values()) == len(union) == len(ids)
    for a in FINEPHRASE_FORMATS:
        for b in FINEPHRASE_FORMATS:
            if a != b:
                assert not (seen_by_format[a] & seen_by_format[b]), f"{a} and {b} overlap"


def test_the_realised_partition_shares_are_balanced_on_a_sample():
    """The design's arithmetic needs each partition to hold >= 17.3% of its config (`table`, the
    worst case) against an ideal 25.0%. An unbalanced split is not a cosmetic problem — a starved
    partition leaves the bundle's last shard unfilled, which `verify` refuses.

    Recomputed here from what the READER actually emitted, so it measures the wired-in path rather
    than re-testing `audit_partition` (which `test_reservoir_ids.py` already covers). The reference
    measurement on real ids is 24.86-25.26% over 287,000 ids
    (`artifacts/reservoir/id-partition-verification.json`, worst deviation 0.27pp).
    """
    from edullm_data.reservoir_ids import FINEPHRASE_FORMATS

    ids = [f"<urn:uuid:aa17-{i:07d}>" for i in range(12_000)]
    text = "balanced sample text " * 30

    def files(_path):
        for doc_id in ids:
            yield Document(id=doc_id, text=text, source="synthetic-finephrase")

    shares = {}
    for fmt in FINEPHRASE_FORMATS:
        spec = _fp_spec(fmt)
        bundle = _small(B.bundles_of(B.plan_document([spec]))[0])
        out, _ = _run_reader(spec, bundle, files, n_files=1)
        shares[fmt] = 100.0 * len(out) / len(ids)

    assert abs(sum(shares.values()) - 100.0) < 1e-9, shares
    # 17.3% is the design floor (`table`); 2pp of the 25.0% ideal is a far tighter bar and still
    # ~8x looser than the 0.27pp measured on real ids, so this fails on a broken partition and not
    # on sampling noise.
    for fmt, pct in shares.items():
        assert 17.3 < pct, f"{fmt} at {pct:.3f}% is under the design floor: {shares}"
        assert abs(pct - 25.0) < 2.0, f"{fmt} at {pct:.3f}% deviates too far: {shares}"


def test_a_finephrase_bundle_reads_four_times_the_text_to_deliver_its_tokens():
    """The budget correction, which ships with the partition or not at all.

    A partition keeping ~1 document in 4 quarters the effective keep rate. Without dividing the
    budget the reader stops after ~1/4 of the text the plan allocated, the bundle runs to
    completion, and it fails `verify` on unfilled refs AFTER its full billable work.

    Asserted through the DELIVERED characters — what reaches `pack` — rather than through the
    budget expression, so it still holds if the constants move. Both bundles plan the same tokens,
    so both must deliver comparable text despite one of them discarding 3 documents in 4.
    """
    per_file = 100
    text = "x" * 500

    def files(path):
        start = int(path.split("/")[1].split(".")[0]) * per_file
        for i in range(start, start + per_file):
            yield Document(id=f"<urn:uuid:bud1-{i:07d}>", text=text, source="s")

    fp = _fp_spec("math")
    plain = _spec(key="plain", source_label="plain", target_tokens=SHARD_TOKENS)

    delivered, read = {}, {}
    for spec in (fp, plain):
        bundle = _small(B.bundles_of(B.plan_document([spec]))[0])
        out, files_read = _run_reader(spec, bundle, files, n_files=200)
        assert len(files_read) < 200, f"{spec.key} exhausted the tree; the budget never bound"
        delivered[spec.key] = sum(len(d.text) for d in out)
        read[spec.key] = len(files_read)

    ratio = delivered["finephrase-math"] / delivered["plain"]
    assert 0.75 < ratio < 1.35, (
        f"FinePhrase delivered {delivered['finephrase-math']:,} chars vs "
        f"{delivered['plain']:,} for the same planned tokens (ratio {ratio:.3f}) — the budget was "
        f"not divided by the keep fraction"
    )
    # And it got there by READING ~4x as much, which is the cost the correction knowingly accepts.
    read_ratio = read["finephrase-math"] / read["plain"]
    assert 3.0 < read_ratio < 5.0, f"read {read} — expected ~4x the files, got {read_ratio:.2f}x"


def test_a_non_finephrase_source_is_not_partitioned():
    """The partition must be scoped to FinePhrase and nothing else. Applying it to a source whose
    documents have no synthetic siblings would silently discard 75% of a legitimate pool — the same
    magnitude of error as not applying it, in the other direction.
    """
    ids = [f"doc-{i}" for i in range(1_500)]
    text = "unrelated corpus text " * 25

    def files(_path):
        for doc_id in ids:
            yield Document(id=doc_id, text=text, source="tiny")

    spec = _spec()  # repo="acme/tiny"
    bundle = _small(B.bundles_of(B.plan_document([spec]))[0])
    out, files_read = _run_reader(spec, bundle, files, n_files=1)
    assert [d.id for d in out] == ids, "a non-FinePhrase source must be passed through untouched"
    assert files_read == ["data/00000.parquet"]


def test_a_finephrase_row_with_an_unnameable_config_is_refused_not_skipped():
    """`keeps_id` raises on an unknown format rather than returning False, deliberately: a typo'd
    config name would otherwise drop 100% of its rows and report a successful ingest of an empty
    source. That behaviour is PRESERVED here, and moved earlier — the refusal happens before the
    first HTTP request, so a bad row fails locally instead of 6,800 files into a billable job.

    The mirror-image failure is just as bad and is also covered: silently treating an unrecognised
    config as "not FinePhrase" would skip the partition entirely and restore the 4x over-exposure.
    """
    import edullm_data.corpus_build as mod

    def boom(*a, **k):
        raise AssertionError("hf_files must not be reached — the refusal precedes any listing")

    bundle = _small(B.bundles_of(B.plan_document([_fp_spec("faq")]))[0])
    real_hf_files = mod.hf_files
    try:
        mod.hf_files = boom
        for bad in ("tables", "FAQ", None, ""):
            # key/source_label pinned to a valid pair, so `config` is the only thing wrong.
            spec = _fp_spec(key="fp-bad", source_label="synthetic-fp-bad", config=bad)
            with pytest.raises(B.BuildDriverError, match="not one of"):
                mod._finephrase_format(spec)
            with pytest.raises(B.BuildDriverError, match="not one of"):
                list(mod._reader_for(spec, bundle))
    finally:
        mod.hf_files = real_hf_files


def test_the_partition_is_keyed_on_the_upstream_repo_not_the_label():
    """`source_label` is a NAMING decision (§1.1 fuses realness into it), so keying the partition on
    it means a later mix edit that renames the label silently turns the partition off — and a 4x
    over-exposure is invisible to every check downstream. `repo` is upstream identity.
    """
    import edullm_data.corpus_build as mod

    assert mod._finephrase_format(_fp_spec("table", source_label="anything-else")) == "table"
    assert mod._finephrase_format(_fp_spec("table", key="renamed-key")) == "table"
    assert mod._finephrase_format(_spec()) is None


def test_the_shipped_registry_carries_the_nested_rewrite_leaf_and_a_nameable_config():
    """§4.2's column trap, recomputed against the COMMITTED registry rather than a fixture.

    FinePhrase's top-level `text` holds the ORIGINAL FineWeb-Edu document — its `dataset` field
    literally reads `HuggingFaceFW/fineweb-edu` — so a row pointing at `text` would build a corpus
    of unrephrased web text labelled synthetic, and no hash, size or decode check catches it. The
    reader's own tests pin what it does GIVEN a spec; this pins what the shipped rows say.

    Also asserts every FinePhrase row is one `_finephrase_format` can name, which is what makes the
    partition apply to all of them rather than raising mid-build.
    """
    import edullm_data.corpus_build as mod
    from edullm_data.reservoir_ids import FINEPHRASE_FORMATS

    specs, _ = B.load_registry()
    fp = [s for s in specs if s.repo == FINEPHRASE_REPO]
    assert len(fp) == len(FINEPHRASE_FORMATS), f"expected 4 FinePhrase rows, got {len(fp)}"
    assert sorted(s.config for s in fp) == sorted(FINEPHRASE_FORMATS)
    for s in fp:
        assert s.text_column == "rollout_results.list.element.text", (
            f"{s.key}: text_column is {s.text_column!r}. Top-level `text` is the ORIGINAL "
            f"FineWeb-Edu document; only the exact path_in_schema selects the rewrite."
        )
        assert mod._finephrase_format(s) == s.config


# --------------------------------------------------------------------------------------
# The double encode
# --------------------------------------------------------------------------------------


def test_the_corpus_is_encoded_exactly_once_per_document():
    """91% of the build's compute was a second, single-threaded encode.

    `run_bundle` called `filter_documents(lambda t: len(tokenizer.encode(t).ids))` and then
    `tokenize_documents`, so every document was encoded twice — and the filter pass handed the
    tokenizer one string at a time, which gets no rayon parallelism. Measured on the pinned dolma2
    tokenizer: 1.10 M tok/s that way against 10.5 M for encode_batch across 32 vCPU.

    Counted per document rather than by wall clock, so it holds on any machine.
    """
    # Counted at `_ids`, BELOW both entry points, because WordTok.encode_batch is itself
    # implemented over encode — so counting `encode` calls would measure the fixture's internals
    # rather than how many times the driver tokenizes each document.
    calls = {"n": 0}

    class CountingTok(WordTok):
        def _ids(self, text):
            calls["n"] += 1
            return super()._ids(text)

    docs = _docs(n=300)
    s3 = FakeS3()
    plan = B.plan_document([_spec()])
    bundle = _small(B.bundles_of(plan)[0])
    B.run_bundle(bundle, plan, _spec(), s3=s3, bucket=BUCKET, prefix=PREFIX,
                 documents=lambda sp, bu: docs, tokenizer=CountingTok(), eos_id=100257,
                 vocab_size=100278, wheel_version="0.6.3")

    # Every document tokenized ONCE. The old shape tokenized each one twice (filter, then pack),
    # so this asserted ~2x before the fuse. `<=` rather than `==` because the val carve drops ~0.5%
    # before the tokenizer ever sees them.
    assert calls["n"] <= len(docs), (
        f"{calls['n']} tokenizations for {len(docs)} documents — the length filter is encoding "
        f"again instead of reading the batch's own ids"
    )
    assert calls["n"] > len(docs) * 0.9, f"only {calls['n']} tokenizations; fixture is not exercising the path"


def test_the_length_filter_still_drops_short_documents_and_reports_them():
    """Fusing the filter into the tokenizer must not weaken it — the >=64-token floor is what keeps
    the EOS fraction under the family's 0.05 bound, and it is reported under its own denominator."""
    # DISTINCT short texts, deliberately. 200 copies of one string are exact duplicates and dedup
    # removes 199 of them before the length filter runs — measured, the first draft of this test
    # asserted 150 drops and got 1.
    short = [Document(id=f"s{i}", text=f"w{i} w{i+1} w{i+2}", source="tiny") for i in range(200)]
    long = _docs(n=400)
    s3 = FakeS3()
    plan = B.plan_document([_spec()])
    bundle = _small(B.bundles_of(plan)[0])
    info = B.run_bundle(bundle, plan, _spec(), s3=s3, bucket=BUCKET, prefix=PREFIX,
                        documents=lambda sp, bu: short + long, tokenizer=WordTok(),
                        eos_id=100257, vocab_size=100278, wheel_version="0.6.3")
    ln = info["length"]
    assert ln["min_tokens"] == plan["min_doc_tokens"]
    assert ln["dropped_short"] > 150, f"3-token documents must be dropped: {ln}"
    assert ln["seen"] == ln["kept"] + ln["dropped_short"] + ln["dropped_empty"]
    assert ln["mean_kept_tokens"] >= plan["min_doc_tokens"]
    # The two stats blocks have DIFFERENT denominators and must not be conflated.
    assert ln["seen"] <= info["filter"]["kept"]


def test_an_empty_document_is_dropped_without_being_encoded():
    """Empty text costs the tokenizer nothing to reject, and it is counted under its own field
    because an empty document contributes one EOS and no content — the shape that drives the EOS
    fraction to 1.0."""
    from edullm_data.corpus_pack import tokenize_documents
    from edullm_data.corpus_read import FilterStats

    stats = FilterStats(min_tokens=4)
    seen = []

    class Tok(WordTok):
        def encode_batch(self, texts, add_special_tokens=False):
            seen.extend(texts)
            return super().encode_batch(texts, add_special_tokens=add_special_tokens)

    docs = [Document(id="a", text="", source="s"),
            Document(id="b", text="w1 w2 w3 w4 w5", source="s")]
    out = list(tokenize_documents(docs, Tok(), eos_id=1, vocab_size=100278,
                                  min_tokens=4, stats=stats))
    assert len(out) == 1
    assert stats.dropped_empty == 1 and stats.kept == 1
    assert "" not in seen, "the empty document must never reach the tokenizer"


def test_a_bundle_drawing_a_SUBSET_of_its_pool_does_not_raise_on_surplus():
    """The bug that killed the first array, 25 of 27 bundles, at end-of-run.

    `_reader_for` over-delivers ON PURPOSE — `_CHARS_PER_TOKEN` 6.0 against a measured ~4.4, times
    `_FILTER_HEADROOM` 1.5 — so that filter attrition cannot leave a bundle's last shard unfilled.
    Whatever that slack does not consume arrives at `pack` as surplus, and `_drain_surplus` used to
    raise on one shard's worth of it. Every bundle whose pool exceeds its target therefore did its
    full billable work and then threw the result away.

    Only `ubuntu-irc` survived, which is exactly why the single-bundle proof run passed and told us
    nothing: its pool is 1.04x its target, so the reader hit end-of-FILES before the budget bound
    and the surplus was 0. A proof case that cannot exhibit the bug is not a proof.

    Asserted at PRODUCTION shard size deliberately: `_drain_surplus` compares against the module
    constant `SHARD_TOKENS`, which `_small`'s rescaling does not touch. At TEST_SHARD_TOKENS the
    surplus never reaches the threshold and this test would pass against the broken code.
    """
    rng = random.Random(21)
    WORDS = 500
    # Four shards' worth of documents offered against a ONE shard plan — the registry's real shape
    # (252B drawn from a 1,094B pool).
    n = (SHARD_TOKENS * 4) // WORDS

    def documents(spec, bundle):
        for i in range(n):
            yield Document(id=f"d{i}",
                           text=" ".join(f"w{rng.randrange(80000)}" for _ in range(WORDS)),
                           source="tiny")

    spec = _spec(target_tokens=SHARD_TOKENS)
    plan = B.plan_document([spec])
    bundle = B.bundles_of(plan)[0]
    assert bundle.tokens == SHARD_TOKENS, "must run at production size or the bug is invisible"

    s3 = FakeS3()
    info = B.run_bundle(bundle, plan, spec, s3=s3, bucket=BUCKET, prefix=PREFIX,
                        documents=documents, tokenizer=WordTok(), eos_id=100257,
                        vocab_size=100278, wheel_version="0.7.0")
    assert info["shards"] == 1
    assert info["unfilled"] == 0, "the shard must be FULL — that is what the headroom buys"
    # The receipt's own accounting must still close, which is what `partial_source` protects: it
    # returns unread=0 rather than draining, so tokens_in only ever counts documents pulled.
    ln = info["length"]
    assert ln["seen"] == ln["kept"] + ln["dropped_short"] + ln["dropped_empty"], ln


def test_a_stream_meant_to_be_consumed_WHOLE_still_raises_on_surplus():
    """The other half: `partial_source` must not become a blanket suppression.

    A caller packing a complete stream still needs the under-allocation gate — leftover tokens
    there mean the plan has too few refs for data that exists, and discarding them ships a corpus
    short of its own source. Only `corpus_build` passes partial_source=True, and only because its
    reader deliberately over-reads.
    """
    import numpy as np

    from edullm_data.corpus_pack import pack

    refs = B.bundles_of(B.plan_document([_spec(target_tokens=SHARD_TOKENS)]))[0].shards
    # RANDOM ids, not a constant fill: `_verify_shard` enforces the family's distinct-ids floor
    # on a sampled window and runs BEFORE the surplus gate, so np.full() fails the wrong check
    # and the test would pass for the wrong reason.
    # RANDOM ids with a trailing EOS, because `pack` consumes `tokenize_documents` output and
    # `_verify_shard` checks BOTH the distinct-ids floor (on a sampled window) and that each
    # document contributes exactly one EOS. Both run before the surplus gate, so a lazier fixture
    # fails an unrelated check and the test passes for the wrong reason.
    rng = np.random.default_rng(9)

    def _doc(n):
        a = rng.integers(1, 100_000, n, dtype="<u4")
        a[-1] = 100257
        return a

    docs = [_doc(SHARD_TOKENS // 2) for _ in range(6)]
    with pytest.raises(BuildError, match="remain after all"):
        pack({("tiny", None, "train"): iter(docs)}, list(refs),
             sink=lambda ref, payload: None, eos_id=100257, vocab_size=100278)


# --------------------------------------------------------------------------------------
# `verify --hash-workers`: the flag exists, defaults to sequential, and refuses to mislead
# --------------------------------------------------------------------------------------


def test_verify_defaults_to_one_hash_worker():
    """The default is the flag's most important property.

    A `verify --deep` run on 2026-08-05 (job `507356db`) returned `OK 27 bundles, 10049 shards
    (payload re-hashed)`. That verdict stands on the strictly-sequential path, so the default must
    keep selecting it — a default of "however many cores" would silently re-characterize the run
    that produced the verdict.
    """
    args = B._build_parser().parse_args(["verify", "--plan-id", "p"])

    assert args.hash_workers == 1
    assert args.deep is False


def test_verify_accepts_hash_workers_with_deep():
    args = B._build_parser().parse_args(
        ["verify", "--plan-id", "p", "--deep", "--hash-workers", "16"]
    )

    assert (args.deep, args.hash_workers) == (True, 16)


def test_hash_workers_without_deep_is_refused_rather_than_silently_ignored():
    """The flag only parallelizes the re-hash. Accepting it without `--deep` would report a run as
    sped up when it ran exactly as before — a misleading success, which is worse than a refusal.

    Refused BEFORE any client is built, so the check cannot depend on AWS being reachable.
    """
    args = B._build_parser().parse_args(["verify", "--plan-id", "p", "--hash-workers", "8"])

    with pytest.raises(BuildError, match="no effect without --deep"):
        B._cmd_verify(args)


def test_a_nonsense_worker_count_is_refused():
    """`ThreadPoolExecutor(max_workers=0)` raises deep inside the pool; refuse at the edge instead."""
    args = B._build_parser().parse_args(
        ["verify", "--plan-id", "p", "--deep", "--hash-workers", "0"]
    )

    with pytest.raises(BuildError, match="at least 1"):
        B._cmd_verify(args)


def test_the_threaded_verify_raises_the_botocore_connection_pool_ceiling():
    """botocore's `max_pool_connections` defaults to 10 and does NOT block when exceeded.

    urllib3's `_put_conn` discards the surplus connection and logs "Connection pool is full", so a
    16-way fan-out on a default client silently pays a fresh TLS handshake per shard past the 10th —
    the speedup gets capped and nothing reports it. Asserted against botocore's real default so this
    test starts failing if that default ever changes underneath us.
    """
    from botocore.config import Config

    assert Config().max_pool_connections == 10, "botocore default changed; revisit _s3()"

    sized = B._s3(max_pool_connections=20)
    assert sized._c.meta.config.max_pool_connections == 20

    # The default path is unchanged: no Config is passed, so it keeps botocore's own default.
    assert B._s3()._c.meta.config.max_pool_connections == 10
