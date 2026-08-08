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

    Simulated with a sink-level failure — an S3 whose write silently drops one object, which is the
    shape of a partial upload.

    Intercepts `put_bytes_verified`, which is what the sink calls since the shard upload started
    declaring its `ChecksumSHA256` (§8.3a step 1). It used to intercept `put`, and that override
    kept "passing" against the new sink while dropping NOTHING — a fixture that no longer reaches
    the code it was written to break, reported as a green test.
    """
    class DroppingS3(FakeS3):
        def put_bytes_verified(self, bucket, key, body, *, content_type=None):
            if key.endswith("train-00001.u32le.bin"):
                # Silently dropped, exactly like an interrupted PUT: the caller still gets the
                # digest it would have got, so nothing at the call site can tell.
                import hashlib

                return hashlib.sha256(body).hexdigest()
            return super().put_bytes_verified(bucket, key, body, content_type=content_type)

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


# --------------------------------------------------------------------------------------
# B7 — the shard upload declares its ChecksumSHA256 (§8.3a step 1)
# --------------------------------------------------------------------------------------


def test_every_shard_upload_declares_a_checksum_recomputed_from_its_own_payload():
    """The digest S3 is asked to verify must be a digest of the bytes S3 received.

    Recomputed, never trusted: this reads the payload back out of the store and re-derives the
    sha256 from those bytes, then compares it to the base64 `ChecksumSHA256` the write DECLARED.
    Asserting merely that a checksum was declared would pass against a constant.

    Why it matters: before this, the sink called plain `s3.put` and computed the sha256 one line
    afterwards for the receipt only (§8.3a). A 200 from an unverified PUT was the only evidence the
    bytes arrived intact, so `verify --deep`'s full second read of the corpus was the sole thing
    standing behind them.
    """
    import base64
    import hashlib

    s3 = FakeS3()
    plan = B.plan_document([_spec()])
    bundle = _small(B.bundles_of(plan)[0])
    _run(bundle, plan, _spec(), s3)

    shard_keys = sorted(k for (b, k) in s3._store if b == BUCKET and k.endswith(".u32le.bin"))
    assert shard_keys, "fixture wrote no shards"
    for key in shard_keys:
        declared = s3.declared_checksum(BUCKET, key)
        assert declared is not None, (
            f"{key} was uploaded with no ChecksumSHA256 declared — S3 verified nothing and a "
            f"corrupted body would have become an object"
        )
        payload = s3.get(BUCKET, key)
        recomputed = base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")
        assert declared == recomputed, (
            f"{key} declared {declared} but its stored bytes hash to {recomputed}"
        )


def test_the_receipt_digest_is_the_same_digest_that_was_declared_to_s3():
    """One hash, two uses. A receipt claiming a digest S3 never verified would be a provenance
    hole exactly where the deep tier is being retired from.

    Recomputed from the payload on both sides rather than compared field-to-field, so the test
    fails if either the receipt or the declaration drifts off the real bytes.
    """
    import base64
    import hashlib
    import json

    s3 = FakeS3()
    plan = B.plan_document([_spec()])
    bundle = _small(B.bundles_of(plan)[0])
    info = _run(bundle, plan, _spec(), s3)

    receipt = json.loads(s3.get(BUCKET, info["receipt_key"]))
    assert receipt["shards"], "fixture wrote no shards"
    for shard in receipt["shards"]:
        key = f"{receipt['prefix']}/{shard['path']}"
        payload = s3.get(BUCKET, key)
        assert shard["sha256"] == hashlib.sha256(payload).hexdigest()
        assert shard["bytes"] == len(payload)
        assert s3.declared_checksum(BUCKET, key) == base64.b64encode(
            hashlib.sha256(payload).digest()
        ).decode("ascii")


def test_a_corrupted_body_is_rejected_by_the_server_and_never_becomes_an_object():
    """The mechanism the checksum buys, asserted rather than assumed.

    Models the behaviour VERIFIED LIVE against S3 on 2026-08-01 for the file-shaped path
    (`s3.put_file_verified`): a declared digest that does not match the body returns `BadDigest`
    and **no object is created**. Driven here through `FakeS3._accept_verified_put`, the fake's
    server side, because that is the only seam at which a wrong digest can be declared at all —
    `put_bytes_verified` derives the digest from the body and so cannot produce a mismatch.

    ⚠️ This is a model of the server, not a live measurement of the bytes-shaped call. The
    bytes-shaped path issues the same `put_object` with the same header, but a deliberate
    corruption must still be asserted against real S3 in the Phase 2 smoke test.
    """
    import base64
    import hashlib

    from edullm_data.s3 import S3Error

    s3 = FakeS3()
    body = b"\x01\x02\x03\x04" * 64
    wrong = base64.b64encode(hashlib.sha256(b"different bytes").digest()).decode("ascii")

    with pytest.raises(S3Error, match="BadDigest"):
        s3._accept_verified_put(BUCKET, "corrupt.u32le.bin", body, wrong)

    assert (BUCKET, "corrupt.u32le.bin") not in s3._store, "no object may exist after a BadDigest"
    assert s3.declared_checksum(BUCKET, "corrupt.u32le.bin") is None


def test_a_plain_put_declares_nothing_so_the_test_above_cannot_pass_vacuously():
    """The control. If `declared_checksum` returned something for every write, the checksum
    assertions would hold whether or not the sink was ever changed."""
    s3 = FakeS3()
    s3.put(BUCKET, "unverified.bin", b"payload")
    assert s3.declared_checksum(BUCKET, "unverified.bin") is None


# --------------------------------------------------------------------------------------
# A2b / #22 — run_bundle consumes the global dedup keep-list
# --------------------------------------------------------------------------------------


def _keep_list_for(bundle_id, texts):
    """A real keep-list awarding `texts` to `bundle_id`, built through the producer's own API.

    Constructed via `HashScan` + `resolve_keep_lists` rather than hand-assembled, so these tests
    exercise the contract eng-05 froze rather than my reading of it.
    """
    from edullm_data.corpus_filter import HashScan, resolve_keep_lists

    scan = HashScan()
    for t in texts:
        scan.add_text(t)
    return resolve_keep_lists({bundle_id: scan})[bundle_id]


def test_no_keep_list_is_byte_for_byte_todays_behaviour():
    """`keep_list=None` must change nothing. It is what lets global dedup land without touching
    any bundle that is not built against a pre-pass.

    Compared on the RECEIPT DIGEST — a content address over every shard path, digest, byte count
    and conservation number — not on a summary field, so a difference anywhere in the artifact
    fails this.
    """
    plan = B.plan_document([_spec()])
    bundle = _small(B.bundles_of(plan)[0])
    docs = _docs(n=400)

    a, b = FakeS3(), FakeS3()
    ref = B.run_bundle(bundle, plan, _spec(), s3=a, bucket=BUCKET, prefix=PREFIX,
                       documents=lambda sp, bu: docs, tokenizer=WordTok(), eos_id=100257,
                       vocab_size=100278, wheel_version="0.6.3")
    explicit = B.run_bundle(bundle, plan, _spec(), s3=b, bucket=BUCKET, prefix=PREFIX,
                            documents=lambda sp, bu: docs, tokenizer=WordTok(), eos_id=100257,
                            vocab_size=100278, wheel_version="0.6.3", keep_list=None)

    assert ref["receipt_sha256"] == explicit["receipt_sha256"]
    assert ref["filter"] == explicit["filter"]
    assert ref["keep"] is None, "no keep-list means no keep block, not a zeroed one"
    assert a.dump(BUCKET) == b.dump(BUCKET), "every uploaded byte must be identical"


def test_a_keep_list_drops_exactly_the_documents_it_does_not_award():
    """The consumer half of #22, recomputed against an independently-derived expected set.

    The keep-list is built over HALF the documents, so the expected survivor set is known without
    consulting any counter the code under test maintains — `filter.kept` is then compared to a
    number this test derived itself.
    """
    docs = _docs(n=400)
    plan = B.plan_document([_spec()])
    bundle = _small(B.bundles_of(plan)[0])

    # Which documents actually reach the filter is decided by `carve`, upstream of dedup, so the
    # expected count has to be taken from the same routing rather than from len(docs).
    from edullm_data.corpus import carve

    reaching = [d for split, d in carve(docs, fraction=plan["val_fraction"]) if split == "train"]
    awarded = reaching[: len(reaching) // 2]

    keep = _keep_list_for(bundle.bundle_id, [d.text for d in awarded])
    s3 = FakeS3()
    info = B.run_bundle(bundle, plan, _spec(), s3=s3, bucket=BUCKET, prefix=PREFIX,
                        documents=lambda sp, bu: docs, tokenizer=WordTok(), eos_id=100257,
                        vocab_size=100278, wheel_version="0.6.3", keep_list=keep)

    f, k = info["filter"], info["keep"]
    assert f["seen"] == len(reaching)
    assert f["kept"] == len(awarded), (
        f"exactly the awarded documents must survive: expected {len(awarded)}, got {f['kept']}"
    )
    assert f["duplicates"] == len(reaching) - len(awarded)
    assert f["seen"] == f["kept"] + f["duplicates"] + f["contaminated"]
    # The keep block's own numbers, against the same independently-derived counts.
    assert k["keys"] == len(awarded)
    assert k["hits"] == len(awarded)
    assert k["misses"] == len(reaching) - len(awarded)
    assert k["repeats"] == 0
    # `unused` is RE-DERIVED, not asserted to be zero. `pack` stops as soon as its planned shards
    # are full and does not drain the document iterator (MEASURED 2026-08-08: 50,264 of 200,015
    # documents pulled), so a healthy bundle routinely leaves awarded keys unpresented. An earlier
    # draft asserted zero here and the matching verifier check failed a legitimate two-bundle run.
    assert k["unused"] == k["keys"] - k["hits"]


def test_the_output_does_not_depend_on_which_bundle_ran_first():
    """THE determinism property, and the reason a shared mutable filter was rejected (§5.3).

    Two bundles are run in both orders against fresh stores and every uploaded byte is compared.
    A filter carrying state between them would make the second bundle's output depend on the
    first's and the two stores would differ.

    **The fixture is built so both bundles actually write.** The obvious construction — the same
    texts offered to both bundles — is degenerate: global dedup awards every hash to one winner, so
    the loser writes nothing and there is nothing left for an ordering to change. (MEASURED on this
    fixture: `resolve_keep_lists` gave `tiny--train` 150 keys and `two--train` 0.) So each bundle
    gets its own texts PLUS a shared overlap that only one of them can win — which is the case an
    order-dependent filter would actually get wrong.
    """
    from edullm_data.corpus_filter import HashScan, resolve_keep_lists

    specs = [_spec(), _spec(key="two", source_label="two")]
    plan = B.plan_document(specs)
    by_key = {s.key: s for s in specs}
    bundles = [_small(b) for b in B.bundles_of(plan)]
    assert len(bundles) == 2

    overlap = _docs(n=60, seed=9)
    own = {bundles[0].bundle_id: _docs(n=200, seed=31),
           bundles[1].bundle_id: _docs(n=200, seed=32)}

    def docs_for(bundle):
        return [
            Document(id=f"{bundle.source}-{i}", text=d.text, source=bundle.source)
            for i, d in enumerate(own[bundle.bundle_id] + overlap)
        ]

    scans = {}
    for bu in bundles:
        scan = HashScan()
        for d in docs_for(bu):
            scan.add_text(d.text)
        scans[bu.bundle_id] = scan
    keeps = resolve_keep_lists(scans)

    # The fixture must be non-degenerate in BOTH directions or this test proves nothing: each
    # bundle must win keys, and the overlap must be genuinely contested.
    assert all(len(v) > 0 for v in keeps.values()), f"degenerate fixture: {keeps}"
    total_distinct = len({d.text for bu in bundles for d in docs_for(bu)})
    assert sum(len(v) for v in keeps.values()) == total_distinct
    assert sum(len(v) for v in keeps.values()) < sum(len(docs_for(bu)) for bu in bundles), (
        "the overlap must actually be deduplicated away, or ordering has nothing to affect"
    )

    def run_in(order):
        s3 = FakeS3()
        infos = {}
        for bu in order:
            infos[bu.bundle_id] = B.run_bundle(
                bu, plan, by_key[bu.spec_key], s3=s3, bucket=BUCKET, prefix=PREFIX,
                documents=lambda sp, b, _b=bu: docs_for(_b), tokenizer=WordTok(),
                eos_id=100257, vocab_size=100278, wheel_version="0.6.3",
                keep_list=keeps[bu.bundle_id],
            )
        return s3.dump(BUCKET), infos

    forward, info_f = run_in(bundles)
    backward, info_b = run_in(list(reversed(bundles)))

    assert forward == backward, (
        "bundle execution order changed the bytes written — the keep-list is not immutable and "
        "the byte-identical-rerun property is gone"
    )
    for bid in own:
        assert info_f[bid]["receipt_sha256"] == info_b[bid]["receipt_sha256"]
        assert info_f[bid]["filter"] == info_b[bid]["filter"]
        assert info_f[bid]["keep"] == info_b[bid]["keep"]


def test_the_same_text_in_two_bundles_survives_exactly_once_globally():
    """What the pre-pass BUYS, asserted rather than cited.

    Today there is no cross-bundle dedup at all — `dedup_and_decontaminate` default-constructs a
    `SeenHashes` per bundle, so every copy of a cross-source duplicate survives. This runs the same
    corpus both ways and shows the difference, so the test fails if the keep-list is ever wired up
    as a no-op.
    """
    from edullm_data.corpus_filter import HashScan, resolve_keep_lists

    specs = [_spec(), _spec(key="two", source_label="two")]
    plan = B.plan_document(specs)
    by_key = {s.key: s for s in specs}
    bundles = [_small(b) for b in B.bundles_of(plan)]
    shared = _docs(n=150, seed=9)

    def docs_for(bundle):
        return [Document(id=f"{bundle.source}-{i}", text=d.text, source=bundle.source)
                for i, d in enumerate(shared)]

    # Without a keep-list: BOTH bundles keep the same texts. That is the leak.
    without = {}
    s3 = FakeS3()
    for bu in bundles:
        try:
            without[bu.bundle_id] = B.run_bundle(
                bu, plan, by_key[bu.spec_key], s3=s3, bucket=BUCKET, prefix=PREFIX,
                documents=lambda sp, b, _b=bu: docs_for(_b), tokenizer=WordTok(),
                eos_id=100257, vocab_size=100278, wheel_version="0.6.3",
            )["filter"]["kept"]
        except BuildError:
            pytest.fail("the no-keep-list arm must succeed; it is today's shipping behaviour")
    assert all(v > 0 for v in without.values())
    assert sum(without.values()) > max(without.values()), (
        "per-bundle dedup must let the SAME text survive in both bundles — that is the defect"
    )

    # With a keep-list: the total kept across both bundles is the DISTINCT text count.
    scans = {}
    for bu in bundles:
        scan = HashScan()
        for d in docs_for(bu):
            scan.add_text(d.text)
        scans[bu.bundle_id] = scan
    keeps = resolve_keep_lists(scans)
    distinct = len({d.text for d in shared})
    assert sum(len(v) for v in keeps.values()) == distinct, (
        "global dedup must award each distinct text exactly once across all bundles"
    )
    assert sum(without.values()) > distinct, "the fixture must actually exhibit the leak"


def test_another_bundles_keep_list_is_refused_rather_than_silently_starving_the_stream():
    """The mis-wiring that produces an almost-empty bundle with no error.

    Pass 1 awards each hash to exactly ONE bundle, so the wrong list rejects nearly everything as
    won-by-another — indistinguishable from a source that was genuinely all duplicates.
    """
    plan = B.plan_document([_spec()])
    bundle = _small(B.bundles_of(plan)[0])
    wrong = _keep_list_for("some-other-bundle", [d.text for d in _docs(n=10)])

    with pytest.raises(BuildError, match="keep-list for 'some-other-bundle'"):
        B.run_bundle(bundle, plan, _spec(), s3=FakeS3(), bucket=BUCKET, prefix=PREFIX,
                     documents=lambda sp, bu: _docs(), tokenizer=WordTok(), eos_id=100257,
                     vocab_size=100278, wheel_version="0.6.3", keep_list=wrong)


def test_a_shared_keepfilter_is_refused_because_its_bitmap_is_mutable():
    """The plausible mistake that defeats the whole design.

    `KeepFilter` duck-types as a `SeenHashes` and the parameter is named `keep_list`, so passing one
    directly looks right. It is the one error every other check misses: a caller-owned filter is
    shared mutable state whose bitmap depends on how many bundles ran before, which is precisely
    the order-dependence the immutable keep-list exists to prevent.
    """
    from edullm_data.corpus_filter import KeepFilter

    plan = B.plan_document([_spec()])
    bundle = _small(B.bundles_of(plan)[0])
    kl = _keep_list_for(bundle.bundle_id, [d.text for d in _docs(n=10)])

    with pytest.raises(BuildError, match="KeepFilter, not a KeepList"):
        B.run_bundle(bundle, plan, _spec(), s3=FakeS3(), bucket=BUCKET, prefix=PREFIX,
                     documents=lambda sp, bu: _docs(), tokenizer=WordTok(), eos_id=100257,
                     vocab_size=100278, wheel_version="0.6.3", keep_list=KeepFilter(kl))


def test_a_keep_list_that_mutates_mid_build_is_caught():
    """The immutability guard, driven by a keep-list that actually grows while the build runs.

    An earlier draft of this test built a mutating object and then asserted nothing about it —
    it passed without ever invoking the guard. Recording that here because a test that constructs
    an elaborate fixture and forgets to use it is indistinguishable from a passing test.

    Necessary, not sufficient: the guard compares the key COUNT, so it catches an append or a
    truncation, not a swap of two entries. `run_bundle`'s own comment says so.
    """
    import array as _array

    from edullm_data.corpus_filter import KeepList

    plan = B.plan_document([_spec()])
    bundle = _small(B.bundles_of(plan)[0])
    docs = _docs(n=400)
    real = _keep_list_for(bundle.bundle_id, [d.text for d in docs])

    class GrowingKeepList(KeepList):
        """Passes `_keep_filter_for`'s isinstance check, then grows behind the filter's back —
        which is exactly the shared-mutable-structure case the pre-pass design forbids."""

        def __len__(self):
            # `object.__setattr__` because `KeepList` is a frozen dataclass — which is the point:
            # frozen stops an honest mistake, not a determined one, so the count guard still earns
            # its place.
            object.__setattr__(
                self, "keys", _array.array("Q", list(self.keys) + [max(self.keys) + 1])
            )
            return len(self.keys)

    grown = GrowingKeepList(real.bundle_id, _array.array("Q", real.keys))

    with pytest.raises(BuildError, match="keep-list changed during the build"):
        B.run_bundle(bundle, plan, _spec(), s3=FakeS3(), bucket=BUCKET, prefix=PREFIX,
                     documents=lambda sp, bu: docs, tokenizer=WordTok(), eos_id=100257,
                     vocab_size=100278, wheel_version="0.6.3", keep_list=grown)


def test_the_keep_block_is_absent_not_zeroed_when_no_keep_list_is_used():
    """A zeroed keep block is a positive claim ("the keep-list matched nothing"); an absent one
    says "this bundle ran under per-bundle dedup". Different build regimes, and a reader must be
    able to tell them apart."""
    import json

    s3 = FakeS3()
    plan = B.plan_document([_spec()])
    bundle = _small(B.bundles_of(plan)[0])
    info = _run(bundle, plan, _spec(), s3)

    doc = json.loads(s3.get(BUCKET, info["receipt_key"]))
    assert "keep" not in doc, "no keep-list means no keep key at all"
    assert "filter" in doc, "the filter block is written regardless"
