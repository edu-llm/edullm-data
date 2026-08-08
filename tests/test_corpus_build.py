"""The Batch build driver: plan determinism, ordinal uniqueness, and resume.

The resume tests are the ones that matter. A driver that skips work it did not do produces a corpus
that is short a shard, and nothing downstream notices until a training run's instance count comes up
wrong — so "receipt exists but its shards do not" is tested four ways, not one.

Everything here is offline: `FakeS3`, a whitespace tokenizer, and injected documents. No network, no
tokenizer download, no Batch.
"""

from __future__ import annotations

import dataclasses
import json
import random
import warnings

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


# --------------------------------------------------------------------------------------
# Duplicate identities — the silent-loss guard. RECOMPUTED, not asserted.
# --------------------------------------------------------------------------------------


def _plan_tokens(plan) -> int:
    return sum(b["tokens"] for b in plan["bundles"])


def test_two_rows_sharing_a_source_label_would_silently_LOSE_tokens_so_they_are_refused():
    """Not "a field is checked" — this measures the loss the check exists to prevent.

    `plan_document` keys targets by `(source_label, domain, split)` (`corpus_build.py:238`), so a
    second row with the same label OVERWRITES the first and its tokens are gone. The test proves
    the magnitude by arithmetic on the declared inputs, then proves the guard fires.

    It also pins the second, worse half of the failure: `spec_by_label` (`:251`) collapses the same
    way, so the surviving bundle would carry ONE row's `config`. Two rows meant to read two disjoint
    subdirectories would both read the SAME one — duplicate data that every token count agrees with.
    """
    a = _spec(key="dclm-a", source_label="dclm", target_tokens=SHARD_TOKENS * 100, config="dirA")
    b = _spec(key="dclm-b", source_label="dclm", target_tokens=SHARD_TOKENS * 200, config="dirB")

    with pytest.raises(BuildError, match="source_label"):
        B.plan_document([a, b])

    # What the guard bought, computed rather than claimed: build each row's plan alone, sum the
    # tokens the planner would really emit, and compare against what a colliding plan can hold.
    alone = _plan_tokens(B.plan_document([a])) + _plan_tokens(
        B.plan_document([dataclasses.replace(b, source_label="dclm-b")])
    )
    survivor = _plan_tokens(B.plan_document([dataclasses.replace(a, source_label="solo")]))
    # The larger row wins the dict slot, so the SMALLER row's tokens are what vanish.
    lost = survivor
    assert lost > 0
    assert lost / alone > 0.3, (
        f"expected the collision to destroy a third of the corpus; measured "
        f"{lost:,} of {alone:,} tokens ({lost / alone:.1%})"
    )


def test_the_colliding_config_is_what_makes_it_worse_than_a_hole():
    """The N-way-split failure, made concrete: one `config` would serve every child.

    Without the guard, `spec_by_label` keeps only the last row, so both bundles below would name
    `dirB` and two children would read identical input while the plan looked complete.
    """
    rows = [
        _spec(key="dclm-a", source_label="dclm", target_tokens=SHARD_TOKENS * 100, config="dirA"),
        _spec(key="dclm-b", source_label="dclm", target_tokens=SHARD_TOKENS * 200, config="dirB"),
    ]
    with pytest.raises(BuildError):
        B.plan_document(rows)

    # Distinct labels: both configs survive into the plan, which is the property the split needs.
    fixed = [dataclasses.replace(r, source_label=r.key) for r in rows]
    plan = B.plan_document(fixed)
    configs = {b["config"] for b in plan["bundles"]}
    assert configs == {"dirA", "dirB"}, "each split row must keep its own disjoint subdirectory"
    assert _plan_tokens(plan) >= SHARD_TOKENS * 299, "no row's tokens may disappear"


def test_two_rows_sharing_a_key_are_refused_because_run_resolves_specs_by_key():
    """`_cmd_run` does `{s.key: s for s in load_registry(...)[0]}` (`corpus_build.py:672`).

    A duplicate key there routes a build to the wrong upstream repo — the plan names one source and
    the run reads another — with nothing in between to notice.
    """
    rows = [
        _spec(key="dup", source_label="one", repo="acme/one"),
        _spec(key="dup", source_label="two", repo="acme/two"),
    ]
    with pytest.raises(BuildError, match="key"):
        B.plan_document(rows)
    # Proven, not assumed: the dict really does keep only one of them.
    assert len({s.key: s for s in rows}) == 1


def test_the_shipping_registry_has_unique_identities(tmp_path):
    """The guard must not be one the real registry trips, and the file must round-trip through it."""
    specs, _meta = B.load_registry()
    assert len({s.source_label for s in specs}) == len(specs)
    assert len({s.key for s in specs}) == len(specs)


def test_load_registry_refuses_a_registry_file_with_a_duplicated_label(tmp_path):
    """The check has to fire on the FILE path too — that is where a hand-edited split row lands."""
    specs, meta = B.load_registry()
    rows = [dataclasses.asdict(s) for s in specs[:2]]
    rows[1]["source_label"] = rows[0]["source_label"]
    rows[1]["key"] = rows[0]["key"] + "-clone"
    p = tmp_path / "registry.json"
    p.write_text(json.dumps({**meta, "corpora": rows}))
    with pytest.raises(BuildError, match="source_label"):
        B.load_registry(str(p))


# --------------------------------------------------------------------------------------
# Splitting a too-large source into N array children (`split_source_rows`)
# --------------------------------------------------------------------------------------


def _split_spec(**over) -> CorpusSpec:
    base = dict(
        key="dclm-baseline", source_label="dclm", target_tokens=SHARD_TOKENS * 1000,
        pool_tokens=SHARD_TOKENS * 8000, config="root",
    )
    base.update(over)
    return _spec(**base)


def test_a_split_source_becomes_N_bundles_each_reading_its_OWN_subdirectory():
    """The whole point: N children, N disjoint inputs, and no child larger than 1/N.

    Recomputes three separate properties rather than asserting the rows exist — the tokens are
    conserved, every subdirectory survives into the plan, and the per-child work really is 1/N.
    """
    parent = _split_spec()
    subs = [f"proc/global-shard_{i:02d}_of_10" for i in (1, 3, 5, 7, 9)]
    rows = B.split_source_rows(parent, subs)

    assert [r.source_label for r in rows] == [f"dclm-0{i}" for i in range(1, 6)]
    assert sum(r.target_tokens for r in rows) == parent.target_tokens, "tokens must be conserved"

    plan = B.plan_document(rows)
    # Each child names its OWN subdirectory. This is the property that fails silently when labels
    # collide — `spec_by_label` would hand every bundle the same config.
    assert {b["config"] for b in plan["bundles"]} == set(subs)
    assert len({b["config"] for b in plan["bundles"]}) == len(subs)

    trains = [b for b in plan["bundles"] if b["split"] == "train"]
    assert len(trains) == 5
    biggest = max(b["tokens"] for b in trains)
    unsplit = max(
        b["tokens"] for b in B.plan_document([parent])["bundles"] if b["split"] == "train"
    )
    # Wall clock is the slowest child, so this ratio IS the speedup. Recomputed, not assumed.
    assert biggest <= unsplit / 5 * 1.01, f"largest child {biggest:,} is not ~1/5 of {unsplit:,}"


def test_each_split_child_gets_its_own_DISJOINT_ordinal_block_at_plan_time():
    """Ordinals are the hard part the split route gets for free from `allocate_ordinals`.

    Two children that each counted from zero would write `tokens/dclm-01/train-00000` and
    `tokens/dclm-02/train-00000` — both parse, neither is rejected. The plan must hand out disjoint
    dense blocks up front, and this recomputes that from the emitted paths.
    """
    rows = B.split_source_rows(_split_spec(), [f"d{i}" for i in range(4)])
    plan = B.plan_document(rows)

    blocks = {}
    for b in plan["bundles"]:
        if b["split"] != "train":
            continue
        blocks[b["source"]] = sorted(
            int(p.rsplit("-", 1)[1].split(".")[0]) for p in b["shards"]
        )
    assert len(blocks) == 4
    for src, ords in blocks.items():
        assert ords == list(range(ords[0], ords[0] + len(ords))), f"{src} block is not contiguous"
    flat = [o for ords in blocks.values() for o in ords]
    assert len(set(flat)) == len(flat), "two children were handed the same ordinal"
    assert sorted(flat) == list(range(len(flat))), "the union must be dense across the whole plan"


def test_splitting_onto_a_REPEATED_subdirectory_is_refused():
    """The failure no gate downstream can see: two children reading identical files.

    The tokens are real, the digests differ (different ordinals), the counts add up. Only the
    plan can catch it, so it must.
    """
    with pytest.raises(BuildError, match="subdirs repeat"):
        B.split_source_rows(_split_spec(), ["a", "b", "a"])


def test_a_one_way_split_and_a_reserve_row_are_refused():
    with pytest.raises(BuildError, match="not a split"):
        B.split_source_rows(_split_spec(), ["only"])
    with pytest.raises(BuildError, match="nothing to split"):
        B.split_source_rows(_split_spec(target_tokens=0), ["a", "b"])


def test_the_split_divides_the_pool_too_so_the_epoch_guard_still_binds():
    """`CorpusSpec.__post_init__` checks pool >= target PER ROW.

    An undivided pool would leave each child claiming the parent's whole pool, so a row drawing
    more than its subdirectory holds would pass a check designed to catch exactly that.
    """
    rows = B.split_source_rows(_split_spec(), ["a", "b", "c", "d"])
    assert sum(r.pool_tokens for r in rows) <= _split_spec().pool_tokens
    for r in rows:
        assert r.pool_tokens >= r.target_tokens
    # And the guard really is live per row: ask for more than a quarter-pool and it raises.
    greedy = _split_spec(target_tokens=SHARD_TOKENS * 8000)
    with pytest.raises(BuildError, match="pool"):
        B.split_source_rows(greedy, ["a", "b"], total_tokens=SHARD_TOKENS * 8000 * 4)


def test_split_labels_are_safe_path_segments_and_survive_the_round_trip_to_labels():
    """`source_label` becomes the shard path's source segment and is inside `manifest_sha256`.

    An unsafe segment is caught nowhere downstream, so the generated labels must satisfy
    SAFE_SEGMENT_RE and must read back out of a real shard path unchanged.
    """
    from edullm_data.manifest import SAFE_SEGMENT_RE, labels_from_path

    rows = B.split_source_rows(_split_spec(), [f"d{i}" for i in range(12)], label_width=2)
    plan = B.plan_document(rows)
    for b in plan["bundles"]:
        assert SAFE_SEGMENT_RE.match(b["source"]), b["source"]
        assert labels_from_path(b["shards"][0]) == {"source": b["source"]}, (
            "the permanent, consumer-visible label must round-trip out of the path"
        )


def test_too_many_parts_for_the_label_width_is_refused_rather_than_truncated():
    """Truncating `dclm-100` to `dclm-10` collides two labels — i.e. silent token loss."""
    with pytest.raises(BuildError, match="label_width"):
        B.split_source_rows(_split_spec(), [f"d{i}" for i in range(11)], label_width=1)


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


def test_every_format_the_gate_admits_actually_dispatches_to_a_reader():
    """THE defect this pair of functions used to have, asserted through the LIVE dispatch path.

    `_reader_for` held its own dict literal — a third format table, and the one that actually ran.
    It omitted `jsonl.gz` while `corpus_read._READERS` had it, so a `jsonl.gz` row was refused at
    plan time although `read_jsonl_gz_documents` is registered for that exact spelling and reads
    it correctly.

    Driven through the real `_reader_for` for EVERY admitted format rather than by comparing two
    sets, because a set comparison is satisfied by a copy — and a copy is what the gate was. This
    fails if anyone reintroduces a literal inside `_reader_for`: a format the gate admits that the
    dispatch does not know raises `BuildDriverError("no reader for ...")` right here.

    The reader is faked (the two things needing a network are `hf_files` and the reader itself),
    so what is exercised is dispatch and nothing else.
    """
    from edullm_data import corpus_read

    for fmt in sorted(B.READABLE_FORMATS):
        spec = _spec(file_format=fmt, target_tokens=SHARD_TOKENS)
        B._assert_readable([spec])  # the gate admits it, by construction of the loop
        bundle = _small(B.bundles_of(B.plan_document([spec]))[0])

        reader_name = corpus_read._READERS[fmt]
        called = []

        def fake(repo, entry, sp, *a, _n=reader_name, **k):
            called.append(_n)
            yield Document(id=f"{_n}-0", text="lorem ipsum dolor sit " * 40, source="tiny")

        real_hf_files = B.hf_files
        real_reader = getattr(corpus_read, reader_name)
        B.hf_files = lambda sp, headers=None: [{"path": "data/00000", "size": 1000}]
        setattr(corpus_read, reader_name, fake)
        try:
            docs = list(B._reader_for(spec, bundle))
        finally:
            B.hf_files = real_hf_files
            setattr(corpus_read, reader_name, real_reader)

        assert called == [reader_name], (
            f"{fmt}: the gate admits it but `_reader_for` dispatched to {called!r}. Every "
            f"admitted format must reach its registered reader — a dispatch table separate from "
            f"`corpus_read._READERS` is exactly the defect this test exists to prevent."
        )
        assert [d.id for d in docs] == [f"{reader_name}-0"]


def test_every_admitted_format_can_also_be_LISTED_not_only_read():
    """A FOURTH format-keyed table exists — `_PAYLOAD_EXT`, which `hf_files` uses to filter the
    repo listing — and widening the gate without it moves a failure to a far worse place.

    Deriving `READABLE_FORMATS` from the reader registry means registering a reader silently
    admits registry rows. `hf_files` then raises `BuildDriverError` for a format `_PAYLOAD_EXT`
    does not name — **at run time, inside a Batch container, after the job is billing**, which is
    exactly the plan-time-vs-run-time trade `_assert_readable`'s docstring forbids. MEASURED
    before this was fixed: a `jsonl.gz` spec passed the gate and then died with
    "no payload extension known for 'jsonl.gz'".

    Recomputed as a set difference over the two live tables, so a reader added without its
    extensions fails here rather than on Batch.
    """
    missing = sorted(set(B.READABLE_FORMATS) - set(B._PAYLOAD_EXT))
    assert not missing, (
        f"{missing} are admitted by the plan-time gate but `hf_files` cannot list them. A format "
        f"that is admitted-but-unlistable is worse than one honestly refused."
    )
    # The reconciliation is enforced at import, not merely tested — force the failure to prove the
    # guard is real rather than a no-op that happens to be satisfied.
    real = dict(B._PAYLOAD_EXT)
    try:
        B._PAYLOAD_EXT.pop("jsonl.gz")
        with pytest.raises(BuildError, match="no entry in _PAYLOAD_EXT"):
            B._assert_payload_extensions_cover_readers()
    finally:
        B._PAYLOAD_EXT.clear()
        B._PAYLOAD_EXT.update(real)
    B._assert_payload_extensions_cover_readers()


def test_both_gzip_spellings_list_the_same_files():
    """`json.gz` and `jsonl.gz` name ONE reader, so they must also name one listing filter.

    Upstream mixes the spellings inside a single directory (the dolmino `math` prefix holds
    `*.jsonl.gz` and `*.json.gz` together), so a row declaring either must pick up both — a
    per-spelling filter would read half a source and report success.
    """
    assert B._PAYLOAD_EXT["jsonl.gz"] == B._PAYLOAD_EXT["json.gz"] == (".json.gz", ".jsonl.gz")


def test_jsonl_gz_is_admitted_because_a_working_reader_is_registered_for_it():
    """The measured false negative, named. `read_jsonl_gz_documents` serves BOTH gzip spellings —
    the same function object under two keys — so refusing `jsonl.gz` dropped a source a working
    reader could have read. Both spellings must reach the identical reader.

    Recomputed from the registry rather than asserted as a literal: the claim is "these two keys
    name one reader", which stays true under a rename.
    """
    from edullm_data import corpus_read

    assert corpus_read._READERS["jsonl.gz"] == corpus_read._READERS["json.gz"]
    assert (
        corpus_read.reader_for_format("jsonl.gz")
        is corpus_read.reader_for_format("json.gz")
        is corpus_read.read_jsonl_gz_documents
    )
    B._assert_readable([_spec(key="dolmino", source_label="dolmino", file_format="jsonl.gz")])


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

    # `*a, **k` absorbs the `headers` argument `corpus_read.read_documents` passes through. The
    # driver now reads via that seam rather than calling the reader directly, so a fake pinned to
    # exactly three positionals would fail on the signature instead of on the behaviour under test.
    def reader(repo, entry, spec, *a, **k):
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

    # `*a, **k` absorbs `headers`: the driver reads through `corpus_read.read_documents`, which
    # forwards it. Everything between `hf_files` and this fake is the production code path.
    def reader(repo, entry, sp, *a, **k):
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
    # This fixture is 200 three-token documents against 400 long ones and the packer stops early,
    # so it really does drop 64.6% — the attrition guard is CORRECT to fire on it. Caught rather
    # than left as log noise: an expected warning that is merely tolerated is one nobody notices
    # turning into an unexpected one.
    from edullm_data.corpus_read import AttritionWarning

    with pytest.warns(AttritionWarning, match="over the 40% threshold"):
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
    # The guard's verdict agrees with the counters it was computed from, on a fixture written
    # years-of-context ago for a different purpose — the cross-check that the wiring reads the
    # right stats object of the two named `FilterStats`.
    assert info["attrition"] and f"dropped {ln['dropped_short']}/{ln['seen']}" in info["attrition"][0]


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


# --------------------------------------------------------------------------------------
# The attrition guard — `corpus_read.FilterStats.problems()` wired into the build path
#
# Until this, `problems()` had ZERO callers in `src/` and five in `tests/`: a guard exercised only
# by its own tests, the same shape as the `families/` bug (CLAUDE.md gotcha 2) — a check that passes
# in a checkout and protects nothing in production.
#
# Every test below RECOMPUTES. The distributions are constructed so the drop rate is a property of
# the fixture that the test asserts independently, not a constant copied from the implementation.
# --------------------------------------------------------------------------------------


def _lognormal_free_docs(n: int, mean_tokens: float, cv: float, seed: int) -> list[Document]:
    """`n` documents whose whitespace-token lengths are ~normal(mean_tokens, cv*mean_tokens).

    Normal rather than lognormal because the LEDGER's measurement of `reddit_to_flashcards` is a
    mean and a CV, and at CV 0.212 the two are indistinguishable for this purpose. Every word is
    distinct-ish so `_verify_shard`'s `distinct_ids_min` is not the thing that fires.
    """
    rng = random.Random(seed)
    out = []
    for i in range(n):
        length = max(1, int(round(rng.gauss(mean_tokens, mean_tokens * cv))))
        out.append(Document(
            id=f"d{i}", text=" ".join(f"w{rng.randrange(80000)}" for _ in range(length)),
            source="tiny",
        ))
    return out


def _measured_drop_fraction(docs, min_tokens: int) -> float:
    """The drop rate recomputed from the fixture itself, with no reference to any build code."""
    short = sum(1 for d in docs if len(d.text.split()) < min_tokens)
    return short / len(docs)


def test_a_source_that_drops_79_percent_fires_the_attrition_guard_with_the_real_numbers():
    """THE dolma3 QA row, reproduced through the whole driver.

    `reddit_to_flashcards`: mean 54.4 tokens, CV 0.212, 79.6% below the 64-token floor — 40.7% of
    the QA pool by bytes, and the row the 14B draw was dropped over. Before this wiring `run_bundle`
    built it, uploaded it, receipted it and returned 0 for it, silently.

    The numbers are recomputed three ways and cross-checked, because a test that only asserted "a
    string appeared" would be the decoration this change exists to remove: the fixture's own drop
    rate, the counters the build reported, and the percentage printed inside the message.
    """
    from edullm_data.corpus import MIN_MEAN_DOC_TOKENS
    from edullm_data.corpus_read import AttritionWarning

    plan = B.plan_document([_spec()])
    bundle = _small(B.bundles_of(plan)[0])
    docs = _lognormal_free_docs(4000, mean_tokens=54.4, cv=0.212, seed=42)

    # The fixture really has the shape the LEDGER measured — asserted before the build runs, so a
    # later edit to the generator cannot quietly turn this into a test of a healthy source.
    assert _measured_drop_fraction(docs, 64) == pytest.approx(0.79, abs=0.03)

    s3 = FakeS3()
    with pytest.warns(AttritionWarning) as caught:
        info = B.run_bundle(
            bundle, plan, _spec(), s3=s3, bucket=BUCKET, prefix=PREFIX,
            documents=lambda sp, bu: docs, tokenizer=WordTok(), eos_id=100257,
            vocab_size=100278, wheel_version="0.6.3",
        )

    L = info["length"]
    # 1. The build's own counters close, and reproduce the fixture's rate.
    assert L["seen"] == L["kept"] + L["dropped_short"] + L["dropped_empty"]
    assert L["dropped_short"] / L["seen"] == pytest.approx(0.79, abs=0.03)
    assert L["drop_fraction"] == pytest.approx(L["dropped_short"] / L["seen"], abs=0.0001)

    # 2. The guard fired, and the message carries the same numbers rather than a generic string.
    assert len(info["attrition"]) == 1, info["attrition"]
    message = info["attrition"][0]
    assert f"dropped {L['dropped_short']}/{L['seen']} documents" in message
    assert f"({L['drop_fraction']:.1%})" in message
    assert "over the 40% threshold" in message

    # 3. The warning carries the bundle id — with 27 array children, an unattributed warning is
    #    an alarm nobody can act on.
    assert any(bundle.bundle_id in str(w.message) for w in caught)

    # 4. THE HEART OF IT: the mean guard reports this bundle as SAFE, and the shards are valid.
    #    Trimming 79% of a distribution centred at 54.4 RAISES the survivors' mean, so the guard
    #    that looks like it covers this is anti-correlated with it. If the drop-rate clause is ever
    #    deleted as redundant, this assertion is the record of what is left: nothing.
    assert L["mean_kept_tokens"] > MIN_MEAN_DOC_TOKENS * 3, (
        "the mean guard cannot fire on this shape — that is WHY the drop-rate guard is needed"
    )
    # 5. And the bundle SUCCEEDED. Warn, not raise: the shards are already in S3 by now.
    assert info["shards"] > 0 and info["tokens_out"] > 0
    assert s3.get(BUCKET, info["receipt_key"]), "a warned bundle is still a completed bundle"


def test_a_drop_rate_just_under_the_threshold_stays_silent():
    """The complement, and it is not optional: a guard that always complained would pass the test
    above while telling an operator nothing. Constructed to straddle the 0.4 boundary, not to sit
    far from it — a check calibrated to fire at 40% must be tested near 40%."""
    from edullm_data.corpus_read import AttritionWarning

    plan = B.plan_document([_spec()])
    bundle = _small(B.bundles_of(plan)[0])
    # 38% short by construction: 380 documents of 10 tokens, 620 of 300.
    rng = random.Random(7)

    def _doc(i, words):
        return Document(id=f"d{i}", text=" ".join(
            f"w{rng.randrange(80000)}" for _ in range(words)), source="tiny")

    docs = [_doc(i, 10) for i in range(380)] + [_doc(i + 380, 300) for i in range(620)]
    rng.shuffle(docs)
    assert _measured_drop_fraction(docs, 64) == pytest.approx(0.38)

    s3 = FakeS3()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        info = B.run_bundle(
            bundle, plan, _spec(), s3=s3, bucket=BUCKET, prefix=PREFIX,
            documents=lambda sp, bu: docs, tokenizer=WordTok(), eos_id=100257,
            vocab_size=100278, wheel_version="0.6.3",
        )

    assert info["attrition"] == [], "0.38 is under 0.40 — the guard must not fire"
    assert not [w for w in caught if issubclass(w.category, AttritionWarning)]
    # The rate really was near the boundary rather than trivially clear of it, so this test would
    # notice a threshold that drifted to 0.3 as well as one that drifted to 0.5.
    assert 0.30 < info["length"]["drop_fraction"] < 0.40


def test_the_attrition_key_is_an_empty_list_not_a_missing_key_on_a_healthy_bundle():
    """`[]` means the guard RAN and found nothing; a missing key means nobody consulted it — which
    is what every caller did before this change. The distinction is the whole point of the wiring,
    so it is asserted rather than assumed."""
    s3 = FakeS3()
    plan = B.plan_document([_spec()])
    bundle = _small(B.bundles_of(plan)[0])
    info = _run(bundle, plan, _spec(), s3)
    assert info["attrition"] == []
    assert info["length"]["drop_fraction"] == 0.0


def test_an_operator_can_make_attrition_fatal_with_no_code_change():
    """Warn-not-raise is this driver's POLICY, and `problems()` explicitly leaves policy to its
    caller. A site that disagrees must not have to patch `run_bundle` — the standard `warnings`
    escalation is the seam, which is why `AttritionWarning` is its own category and not a bare
    `RuntimeWarning` shared with the TOKENIZERS_PARALLELISM notice."""
    from edullm_data.corpus_read import AttritionWarning

    plan = B.plan_document([_spec()])
    bundle = _small(B.bundles_of(plan)[0])
    docs = _lognormal_free_docs(4000, mean_tokens=54.4, cv=0.212, seed=42)

    with warnings.catch_warnings():
        warnings.simplefilter("error", AttritionWarning)
        with pytest.raises(AttritionWarning, match="over the 40% threshold"):
            B.run_bundle(bundle, plan, _spec(), s3=FakeS3(), bucket=BUCKET, prefix=PREFIX,
                         documents=lambda sp, bu: docs, tokenizer=WordTok(), eos_id=100257,
                         vocab_size=100278, wheel_version="0.6.3")


def test_cmd_run_prints_the_length_block_and_the_attrition_verdict(monkeypatch, capsys):
    """`FilterRecord`'s docstring justifies keeping the length filter OUT of the receipt because
    "it is returned below and printable". That was true of the value and false of the program:
    `info["length"]` had no reader anywhere in `src/` — `_cmd_run` printed only the dedup block —
    so on the Batch path the short-doc attrition that killed the dolma3 QA row survived NOWHERE:
    not the receipt, not stdout, not CloudWatch.

    Driven through the REAL `_cmd_run` with its I/O stubbed, not through a copy of its format
    strings: a test that re-implemented the print would pass while the shipped function printed
    nothing, which is precisely the failure being fixed.
    """
    import argparse

    plan = B.plan_document([_spec()])
    info = {
        "bundle_id": "tiny--train", "receipt_key": "k", "receipt_sha256": "x",
        "shards": 2, "tokens_out": 32768, "unfilled": 0,
        "filter": {"seen": 3000, "kept": 3000, "duplicates": 0, "contaminated": 0,
                   "normalization": "n"},
        "keep": None,
        "length": {"min_tokens": 64, "seen": 2200, "kept": 462, "dropped_short": 1738,
                   "dropped_empty": 0, "kept_tokens": 32381, "mean_kept_tokens": 70.09,
                   "drop_fraction": 0.79},
        "attrition": ["dropped 1738/2200 documents (79.0%), over the 40% threshold."],
    }

    monkeypatch.setattr(B, "_require_batch", lambda **kw: None)
    monkeypatch.setattr(B, "_assert_tokenizers_parallelism", lambda: None)
    monkeypatch.setattr(B, "_s3", lambda **kw: FakeS3())
    monkeypatch.setattr(B, "_load_plan", lambda *a, **kw: plan)
    monkeypatch.setattr(B, "load_registry", lambda p: ([_spec()], {}))
    monkeypatch.setattr(B, "load_tokenizer", lambda d: (WordTok(), 100257, 100278))
    monkeypatch.setattr(B, "bundle_is_done", lambda *a, **kw: False)
    monkeypatch.setattr(B, "run_bundle", lambda *a, **kw: info)

    args = argparse.Namespace(
        allow_local=True, bucket=BUCKET, prefix=PREFIX, plan_id=plan["plan_id"],
        registry=None, shard=0, of=1, tokenizer_dir="/nonexistent",
        no_decontaminate=True, force=True,
    )
    assert B._cmd_run(args) == 0

    out = capsys.readouterr().out
    # The length block reaches stdout, with the counts AND the rate.
    assert "short=1,738" in out, out
    assert "kept=462/2,200" in out, out
    assert "drop=79.0%" in out, out
    assert "mean_tok=70.09" in out, out
    # The attrition verdict is printed on its own greppable line, attributed to a bundle — with 27
    # array children an unattributed alarm cannot be acted on.
    assert "ATTRITION tiny--train: dropped 1738/2200 documents (79.0%)" in out, out
    # And the two denominators stay on separate lines. `filter.seen` counts documents entering
    # dedup, `length.seen` counts those that survived it; one row carrying both is the
    # `category_attrition` mistake in miniature.
    done_line = next(ln for ln in out.splitlines() if ln.startswith("DONE "))
    assert "3,000" in done_line and "2,200" not in done_line


def test_cmd_run_prints_no_attrition_line_for_a_healthy_bundle(monkeypatch, capsys):
    """The complement: a driver that printed ATTRITION unconditionally would make the line
    worthless in a 27-child log."""
    import argparse

    plan = B.plan_document([_spec()])
    info = {
        "bundle_id": "tiny--train", "receipt_key": "k", "receipt_sha256": "x",
        "shards": 2, "tokens_out": 32768, "unfilled": 0,
        "filter": {"seen": 400, "kept": 400, "duplicates": 0, "contaminated": 0,
                   "normalization": "n"},
        "keep": None,
        "length": {"min_tokens": 64, "seen": 400, "kept": 400, "dropped_short": 0,
                   "dropped_empty": 0, "kept_tokens": 120000, "mean_kept_tokens": 300.0,
                   "drop_fraction": 0.0},
        "attrition": [],
    }
    monkeypatch.setattr(B, "_require_batch", lambda **kw: None)
    monkeypatch.setattr(B, "_assert_tokenizers_parallelism", lambda: None)
    monkeypatch.setattr(B, "_s3", lambda **kw: FakeS3())
    monkeypatch.setattr(B, "_load_plan", lambda *a, **kw: plan)
    monkeypatch.setattr(B, "load_registry", lambda p: ([_spec()], {}))
    monkeypatch.setattr(B, "load_tokenizer", lambda d: (WordTok(), 100257, 100278))
    monkeypatch.setattr(B, "bundle_is_done", lambda *a, **kw: False)
    monkeypatch.setattr(B, "run_bundle", lambda *a, **kw: info)

    args = argparse.Namespace(
        allow_local=True, bucket=BUCKET, prefix=PREFIX, plan_id=plan["plan_id"],
        registry=None, shard=0, of=1, tokenizer_dir="/nonexistent",
        no_decontaminate=True, force=True,
    )
    assert B._cmd_run(args) == 0
    out = capsys.readouterr().out
    assert "ATTRITION" not in out
    # The length line is UNCONDITIONAL, though — a healthy bundle's attrition rate is a number the
    # pool arithmetic wants too, not only a failing one's.
    assert "drop=0.0%" in out and "short=0" in out


# --------------------------------------------------------------------------------------
# FILE-SHARDING — §8A.5a. K children of ONE bundle, disjoint PLAN-ASSIGNED ordinal ranges
#
# The anti-pattern named for this feature: "a test asserting the function exists, or that K
# children ran, is decoration." So every test below RECOMPUTES the union of what the children
# produce — from `s3._store` where children really run, from the emitted paths otherwise — and
# compares it to the set the unsharded plan would have written. Never a count.
# --------------------------------------------------------------------------------------


def _fs_spec(**over) -> CorpusSpec:
    """40 planned shards, all train — 0.5% of 40 is 0.2 of a shard, so no val split is emitted.

    40 because ``40 % 7 == 5``: the K values under test include one that does NOT divide it
    evenly, which is the off-by-one case `_shard_slice`'s docstring warns about.
    """
    base = dict(key="stackv2-edu", source_label="stackv2-edu",
                target_tokens=SHARD_TOKENS * 40, pool_tokens=SHARD_TOKENS * 4000)
    base.update(over)
    return _spec(**base)


def _ordinals(paths) -> list[int]:
    """Ordinals recomputed from real shard KEYS, via the same parser the pipeline uses.

    Through `parse_shard_name` rather than a slice of the string, because that function IS the
    thing whose blindness makes reuse invisible — it happily returns `('train', 0)` for two
    different sources' `train-00000`. A test that parsed the path its own way would be checking a
    different function from the one production trusts.
    """
    from edullm_data.manifest import parse_shard_name

    out = []
    for p in paths:
        parsed = parse_shard_name(p)
        assert parsed is not None, f"{p} does not parse as a shard name"
        out.append(parsed[1])
    return out


@pytest.mark.parametrize("k", [1, 2, 3, 7])
def test_K_children_of_one_bundle_write_EXACTLY_the_unsharded_ordinal_set(k):
    """THE test. K children, one bundle: the union of the ordinals they really write must equal
    the set the unsharded plan would have written — no gaps, no overlaps, no reuse.

    Recomputed from `s3._store`, i.e. from objects that exist, after running every child end to
    end through `run_bundle`. Not from the plan, and not from a count: a count is satisfied by two
    children writing the same ordinal twice while a third writes none, which is exactly the
    failure — one `PutObject` overwrites the other and `parse_shard_name` cannot tell.

    K=7 does not divide 10 shards evenly, which is deliberate: `_shard_slice`'s own docstring warns
    that an off-by-one in a stride "silently drops files ... and a smaller-than-expected id set
    does not look like an error."

    Each child is given a DIFFERENT document set, the way a real file slice would — so a child that
    wrote into a sibling's range would be writing genuinely different bytes there, which is the
    live failure rather than a harmless duplicate.
    """
    spec = _fs_spec(target_tokens=SHARD_TOKENS * 10)
    flat = B.plan_document([spec])
    expected = sorted(_ordinals(p for b in flat["bundles"] for p in b["shards"]))
    assert len(expected) == 10

    plan = B.plan_document([spec], file_shards={spec.key: k})
    assert len(plan["bundles"]) == len(flat["bundles"]) * k

    s3 = FakeS3()
    for bundle in B.bundles_of(plan):
        _run(bundle, plan, spec, s3,
             docs=_docs(n=1200, words=200, seed=100 + bundle.file_shard_index))

    written = sorted(_ordinals(
        key for (b, key) in s3._store if b == BUCKET and key.endswith(".u32le.bin")
    ))
    assert written == expected, (
        f"K={k}: the union of what {k} children wrote is not the unsharded set. "
        f"missing={sorted(set(expected) - set(written))} "
        f"extra={sorted(set(written) - set(expected))} "
        f"reused={sorted(o for o in set(written) if written.count(o) > 1)}"
    )
    # Stated separately because equality of sorted lists already forbids duplicates, and the
    # duplicate is the one failure a reader of this test will want named.
    assert len(written) == len(set(written)), "two children wrote the same ordinal"


@pytest.mark.parametrize("k", [1, 2, 3, 7])
def test_the_PLAN_hands_out_disjoint_ranges_before_any_child_runs(k):
    """The same union property at PLAN time, which is where the CEO's condition actually binds:
    a child must never allocate an ordinal, so the disjointness has to exist in the artifact.

    Also asserts each part's block is CONTIGUOUS. Disjointness alone would be satisfied by a
    strided cut, which is just as correct and unreadable — `allocate_ordinals` promises
    "shards == max - min + 1" and a part should keep that property.
    """
    spec = _fs_spec()
    flat = B.plan_document([spec])
    expected = sorted(_ordinals(p for b in flat["bundles"] for p in b["shards"]))

    plan = B.plan_document([spec], file_shards={spec.key: k})
    got = sorted(_ordinals(p for b in plan["bundles"] for p in b["shards"]))
    assert got == expected, "file-sharding must not change WHICH shards the corpus contains"

    for b in plan["bundles"]:
        ords = _ordinals(b["shards"])
        assert ords == list(range(ords[0], ords[0] + len(ords))), (
            f"{b['bundle_id']} block is not contiguous: {ords}"
        )
        assert b["file_shard"]["of"] == k
        assert 0 <= b["file_shard"]["index"] < k


def test_a_K_that_does_not_divide_evenly_still_partitions_every_shard():
    """Off-by-one in a stride is the classic failure, so the uneven case is asserted by size.

    40 train shards over 7 parts is 6,6,6,6,6,5,5 — the first `n % K` parts take one extra. A
    floor-only cut would emit 7x5 = 35 and DROP five shards; a ceil-only cut would emit 7x6 = 42
    and reuse two ordinals. Both are checked here by the exact size vector, not by a total.
    """
    spec = _fs_spec()
    plan = B.plan_document([spec], file_shards={spec.key: 7})
    train = [b for b in plan["bundles"] if b["split"] == "train"]
    sizes = [len(b["shards"]) for b in sorted(train, key=lambda b: b["file_shard"]["index"])]
    assert sizes == [6, 6, 6, 6, 6, 5, 5], sizes
    assert sum(sizes) == len(B.plan_document([spec])["bundles"][0]["shards"])


def test_the_shard_path_set_is_UNCHANGED_by_file_sharding_on_the_real_registry():
    """Corpus-content neutrality, on the shipping 133-row registry rather than a fixture.

    This is what makes file-sharding safe to add before FREEZE: the set of object keys the build
    writes is byte-identical with and without it. Only the grouping into units of work changes,
    so no consumer-visible label, path, or token count moves. `source_label` is untouched — the
    difference from `split_source_rows`, which publishes `dclm-01 ... dclm-NN` permanently inside
    `manifest_sha256`.
    """
    import pathlib

    # The 1.0T `final-dataset` registry, not the reservoir one `REGISTRY_PATH` defaults to: these
    # four sources are the flat, un-fanned-out ones this feature exists for, and they only appear
    # in that file.
    reg = (pathlib.Path(__file__).resolve().parents[1]
           / "artifacts" / "final-dataset" / "corpus-registry.json")
    specs, meta = B.load_registry(str(reg))
    drawn = [s for s in specs if s.target_tokens > 0]

    flat = B.plan_document(drawn, registry_meta=meta)
    ways = {"stackv2-edu": 7, "finepdfs-edu": 4,
            "nemotron-cc-math-3": 3, "nemotron-cc-math-4plus": 2}
    sharded = B.plan_document(drawn, registry_meta=meta, file_shards=ways)

    def paths(p):
        return sorted(x for b in p["bundles"] for x in b["shards"])

    assert paths(sharded) == paths(flat), "file-sharding changed which objects the build writes"
    assert len(paths(sharded)) == len(set(paths(sharded)))
    assert len(sharded["bundles"]) > len(flat["bundles"])
    # And the biggest unit of work really did shrink — that is the entire point (51.38 h -> ~11 h).
    biggest = lambda p: max(len(b["shards"]) for b in p["bundles"])  # noqa: E731
    assert biggest(sharded) < biggest(flat) / 3


def test_every_part_gets_its_OWN_bundle_id_because_receipts_are_keyed_on_it():
    """Sharing a `bundle_id` is a K-1x silent data loss, not a naming preference.

    `receipt_key` is `.../_receipts/{bundle_id}.json`, so two parts with one id write ONE receipt —
    the second overwrites the first — and `bundle_is_done` then reports every part matching that
    receipt as DONE. K-1 children are skipped without ever running and the corpus is short their
    shards, discovered at training time. Asserted through `receipt_key` itself, not through the
    ids, because the key is what actually collides.
    """
    spec = _fs_spec()
    plan = B.plan_document([spec], file_shards={spec.key: 7})
    ids = [b["bundle_id"] for b in plan["bundles"]]
    assert len(ids) == len(set(ids)), "two parts share a bundle_id"
    keys = [B.receipt_key(PREFIX, plan["plan_id"], i) for i in ids]
    assert len(keys) == len(set(keys)), "two parts collide on one receipt key"

    # An unsharded stream's id is UNCHANGED, so the schema bump costs nothing on the ~157 streams
    # nobody is splitting — and every existing receipt key still resolves.
    plain = B.plan_document([spec])
    assert [b["bundle_id"] for b in plain["bundles"]] == ["stackv2-edu--train"]
    # Including a domain-bearing stream, whose id has a different shape.
    dom = B.plan_document([spec], domain_map={spec.key: {"a": "python"}})
    assert [b["bundle_id"] for b in dom["bundles"]] == ["stackv2-edu--python--train"]
    dom_split = B.plan_document([spec], domain_map={spec.key: {"a": "python"}},
                                file_shards={spec.key: 2})
    assert [b["bundle_id"] for b in dom_split["bundles"]] == [
        "stackv2-edu--python--train--p00of02", "stackv2-edu--python--train--p01of02"]


def test_a_part_id_survives_the_round_trip_into_a_real_receipt_key():
    """The part suffix must satisfy `_assert_safe_key`, which `receipt_key` enforces.

    A `bundle_id` that fails it raises at receipt-write time — i.e. after the bundle's full
    billable work, which is the end-of-run failure shape this package has already been bitten by
    twice (`_drain_surplus`, `_check_keep_accounting`).
    """
    spec = _fs_spec()
    plan = B.plan_document([spec], file_shards={spec.key: 12})
    for b in plan["bundles"]:
        key = B.receipt_key(PREFIX, plan["plan_id"], b["bundle_id"])
        assert key.endswith(f"{b['bundle_id']}.json")
        assert "of" in b["bundle_id"] and b["bundle_id"].split("--")[-1].startswith("p")


def test_a_part_reads_its_own_file_slice_and_the_union_of_files_is_the_whole_source():
    """The plan's half of the reader contract: `(index, of)` reaches each part as a 2-tuple.

    `Bundle.file_shard` is deliberately never `None`, so a reader can write
    `idx, of = bundle.file_shard` unconditionally — `(0, 1)` is `_shard_slice`'s identity
    (`items[0::1] == items`), i.e. exactly today's whole-source behaviour. Recomputed here by
    actually striding a file list with each part's own pair and unioning the result.
    """
    from edullm_data.ingest_reservoir import _shard_slice

    spec = _fs_spec()
    plan = B.plan_document([spec], file_shards={spec.key: 7})
    train = [b for b in B.bundles_of(plan) if b.split == "train"]

    files = [f"data/{i:05d}.parquet" for i in range(95)]   # stackv2-edu's real file count
    seen: list[str] = []
    for b in sorted(train, key=lambda b: b.file_shard_index):
        assert isinstance(b.file_shard, tuple) and len(b.file_shard) == 2
        assert b.is_file_sharded and b.file_shard_count == 7
        seen += _shard_slice(files, b.file_shard_index, b.file_shard_count)

    assert sorted(seen) == files, "the parts' file slices are not a partition of the source"
    assert len(seen) == len(set(seen)), "two parts read the same file — silent duplicate data"

    # An unsharded bundle's pair is the identity, so the same reader code path is today's.
    plain = B.bundles_of(B.plan_document([spec]))[0]
    assert plain.file_shard == (0, 1)
    assert not plain.is_file_sharded
    assert _shard_slice(files, *plain.file_shard) == files


def test_each_part_carries_its_OWN_token_budget_so_a_reader_must_not_divide_again():
    """`Bundle.tokens` is derived from the part's OWN refs, so it is already 1/K of the stream.

    The consumer-side trap: a reader that divides its budget by K a second time reads 1/K**2 of
    the text it needs, every part underfills, and the failure surfaces only as unfilled refs at
    the end of a multi-hour run. Asserted as conservation — the parts' tokens must sum to the
    unsharded bundle's, not each equal it.
    """
    # Big enough to earn a val split too, so BOTH streams are checked — a val part is the one most
    # likely to be mis-sized, since it is 0.5% of the source.
    spec = _fs_spec(target_tokens=SHARD_TOKENS * 1460, pool_tokens=SHARD_TOKENS * 20000)
    whole = B.bundles_of(B.plan_document([spec]))
    parts = B.bundles_of(B.plan_document([spec], file_shards={spec.key: 7}))
    assert {b.split for b in whole} == {"train", "val"}

    for split in ("train", "val"):
        w = next(b for b in whole if b.split == split)
        ps = [b for b in parts if b.split == split]
        assert sum(b.tokens for b in ps) == w.tokens, f"{split}: tokens are not conserved"
        assert max(b.tokens for b in ps) < w.tokens, f"{split}: no part is smaller than the whole"


def test_the_plan_stays_a_pure_deterministic_content_address_under_file_sharding():
    """No clock, no environment: same arguments, byte-identical document and `plan_id`.

    And a DIFFERENT K must give a different id — the plan is what tells a child which files to
    read, so two builds that disagree about K are different builds and must not share an address.
    """
    spec = _fs_spec()
    a = B.plan_document([spec], file_shards={spec.key: 7})
    b = B.plan_document([spec], file_shards={spec.key: 7})
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["plan_id"] == b["plan_id"]

    assert B.plan_document([spec], file_shards={spec.key: 4})["plan_id"] != a["plan_id"]
    assert B.plan_document([spec])["plan_id"] != a["plan_id"]
    # K=1 is not "sharded 1 way", it is unsharded — same document, same address.
    assert B.plan_document([spec], file_shards={spec.key: 1}) == B.plan_document([spec])


def test_the_file_shard_field_is_on_EVERY_bundle_including_unsharded_ones():
    """Mandatory, never optional. A reader that has to supply a default reads the wrong files
    whenever its default disagrees with the plan, and nothing downstream can see that: the
    documents are real, the tokens are real, and the counts still add up."""
    spec = _fs_spec()
    plan = B.plan_document([spec, _spec()], file_shards={spec.key: 3})
    assert plan["schema"] == B.PLAN_SCHEMA
    for b in plan["bundles"]:
        assert "file_shard" in b, b["bundle_id"]
        assert set(b["file_shard"]) == {"index", "of"}
    tiny = [b for b in plan["bundles"] if b["source"] == "tiny"]
    assert tiny and all(b["file_shard"] == {"index": 0, "of": 1} for b in tiny)


def test_a_v2_plan_missing_the_field_is_REFUSED_rather_than_defaulted():
    """The one place a default would be catastrophic, so it is the one place that raises.

    `from_plan_entry` must tolerate an absent field — a v1 plan could not express a part, and
    "not sharded" is the correct reading of one. But a v2 plan that dropped the field on a bundle
    that IS a part would hand that child the WHOLE file list against 1/K of the ordinals: a K-fold
    over-read whose only symptom is surplus, and `partial_source=True` ignores surplus by design.
    """
    spec = _fs_spec()
    plan = B.plan_document([spec], file_shards={spec.key: 3})
    del plan["bundles"][1]["file_shard"]
    with pytest.raises(BuildError, match="file_shard"):
        B.bundles_of(plan)

    # A v1 plan (no such schema tag) still parses, and reads as unsharded.
    legacy = B.plan_document([spec])
    legacy["schema"] = "edullm-build-plan/v1"
    for b in legacy["bundles"]:
        b.pop("file_shard")
    assert all(b.file_shard == (0, 1) for b in B.bundles_of(legacy))


def test_more_parts_than_shards_is_refused_because_an_empty_part_has_nowhere_to_write():
    """A part with no ordinals is the `shard_plan` zero-shard failure in miniature: `pack` finds
    no refs for the stream and drops its documents in full, while every token count still adds up.

    The VAL stream is what binds in practice — it is 0.5% of the source, so a source with 40 train
    shards has 0 or a handful of val shards and a 7-way split asks for more parts than exist.
    """
    spec = _fs_spec(target_tokens=SHARD_TOKENS * 3)
    with pytest.raises(BuildError, match="only 3 shard"):
        B.plan_document([spec], file_shards={spec.key: 4})

    from edullm_data.corpus import partition_ordinals

    refs = B.bundles_of(B.plan_document([spec]))[0].shards
    with pytest.raises(BuildError, match="would get no refs"):
        partition_ordinals(refs, 4)


def test_a_bad_file_shards_map_is_refused_at_plan_time_not_discovered_hours_in():
    """An unknown key is the dangerous one: a typo leaves the 51 h bundle unsplit while the
    operator believes it was split seven ways, and the only symptom is a build that takes K times
    as long."""
    spec = _fs_spec()
    with pytest.raises(BuildError, match="not a drawn registry row"):
        B.plan_document([spec], file_shards={"stackv2-edy": 7})
    with pytest.raises(BuildError, match="must be an int"):
        B.plan_document([spec], file_shards={spec.key: 0})
    with pytest.raises(BuildError, match="must be an int"):
        B.plan_document([spec], file_shards={spec.key: True})
    with pytest.raises(BuildError, match="MAX_FILE_SHARDS"):
        B.plan_document([spec], file_shards={spec.key: 100})


def test_the_map_can_come_from_the_registry_metadata_rather_than_the_call():
    """`_file_shards` is TOP-LEVEL registry metadata, not a row field, so `CorpusSpec` and the row
    schema are untouched and an old registry plans exactly as it did before."""
    spec = _fs_spec()
    from_meta = B.plan_document([spec], registry_meta={"_file_shards": {spec.key: 3}})
    from_arg = B.plan_document([spec], file_shards={spec.key: 3})
    assert [b["bundle_id"] for b in from_meta["bundles"]] == \
           [b["bundle_id"] for b in from_arg["bundles"]]
    # The explicit argument WINS, so an operator can override the registry without editing it.
    override = B.plan_document([spec], registry_meta={"_file_shards": {spec.key: 3}},
                               file_shards={})
    assert len(override["bundles"]) == len(B.plan_document([spec])["bundles"])


def test_the_disjointness_guard_catches_a_plan_that_reuses_an_ordinal():
    """The guard is not a restatement of `partition_ordinals`' contract — it checks the ASSEMBLED
    document, which is where string-formatted ids and paths can still collide.

    Both halves are exercised by mutating a correct plan the way a real bug would.
    """
    spec = _fs_spec()
    plan = B.plan_document([spec], file_shards={spec.key: 3})

    reused = json.loads(json.dumps(plan))
    reused["bundles"][1]["shards"][0] = reused["bundles"][0]["shards"][0]
    with pytest.raises(BuildError, match="claimed by both"):
        B._assert_plan_is_disjoint(reused)

    collided = json.loads(json.dumps(plan))
    collided["bundles"][1]["bundle_id"] = collided["bundles"][0]["bundle_id"]
    with pytest.raises(BuildError, match="bundle_id"):
        B._assert_plan_is_disjoint(collided)


def test_partition_ordinals_refuses_refs_from_two_different_streams():
    """A part is a slice of ONE stream's work. Mixed refs would produce "ranges" that are not a
    range of anything, and silently regrouping them would hide the caller's mistake."""
    from edullm_data.corpus import ShardRef, partition_ordinals

    mixed = [ShardRef(source="a", domain=None, split="train", ordinal=0),
             ShardRef(source="b", domain=None, split="train", ordinal=1)]
    with pytest.raises(BuildError, match="ONE stream"):
        partition_ordinals(mixed, 2)


def test_an_out_of_range_file_shard_pair_is_refused_when_the_bundle_is_built():
    """`items[index::of]` with a bad index returns the WRONG files or none, and a shorter file
    list does not look like an error — `_shard_slice`'s own docstring says so."""
    plan = B.plan_document([_fs_spec()], file_shards={"stackv2-edu": 3})
    entry = json.loads(json.dumps(plan["bundles"][0]))
    # Asserts on "out of range", the wording the merged guard uses. Two agents wrote this guard
    # independently with different messages; the surviving text is eng-12's, which names the
    # consequence (a silently EMPTY bundle) rather than just the invalidity.
    entry["file_shard"] = {"index": 3, "of": 3}
    with pytest.raises(BuildError, match="out of range"):
        B.Bundle.from_plan_entry(entry)
    entry["file_shard"] = {"index": -1, "of": 3}
    with pytest.raises(BuildError, match="out of range"):
        B.Bundle.from_plan_entry(entry)


def test_K_children_are_distributed_across_array_children_by_the_EXISTING_stride():
    """The mechanism `--shard/--of` already has: parts are bundles, so `_shard_slice` spreads them
    with no new machinery. This is why file-sharding is a plan change and not a driver change.

    Recomputed as a partition of the bundle ids, because `_shard_slice`'s failure mode is dropping
    items, and a smaller-than-expected set does not look like an error.
    """
    from edullm_data.ingest_reservoir import _shard_slice

    spec = _fs_spec()
    plan = B.plan_document([spec], file_shards={spec.key: 7})
    everything = B.bundles_of(plan)

    for of in (1, 3, 5, 16):
        got = [b.bundle_id for c in range(of) for b in _shard_slice(everything, c, of)]
        assert sorted(got) == sorted(b.bundle_id for b in everything)
        assert len(got) == len(set(got)), "one part landed on two array children"


def test_verify_bundle_set_REJECTS_a_correct_file_sharded_build___KNOWN_GAP():
    """⚠️ **BLOCKER, PINNED HERE SO IT CANNOT BE FORGOTTEN. Not this stream's surface to fix.**

    `corpus_receipt._check_set_shards` and the duplicate-stream check group receipts by
    `(source, domain, split)` and raise `bundle-set-duplicate-stream` when more than one claims a
    stream — and K parts share one stream BY CONSTRUCTION. So a perfectly correct file-sharded
    build runs to completion and then FAILS its own `verify`, which exits non-zero.

    Reproduced free (no `s3=`), which is the cheap tier `verify_bundle_set`'s docstring advertises.
    This test asserts the CURRENT WRONG BEHAVIOUR so that the fix has a failing test to flip: when
    `Receipt` learns `file_shard` and the check becomes file-shard-aware, this test must be
    inverted, not deleted.

    Do NOT "fix" it by grouping on `bundle_id` — that would weaken the genuine
    retry-that-did-not-replace case, which is the real defect the check exists to catch.
    """
    from edullm_data.corpus_receipt import Receipt, ShardReceipt, verify_bundle_set

    spec = _fs_spec()
    plan = B.plan_document([spec], file_shards={spec.key: 3})
    parts = [b for b in B.bundles_of(plan) if b.split == "train"]

    receipts = [
        Receipt(
            plan_id=plan["plan_id"], bundle_id=b.bundle_id, prefix=PREFIX,
            source=b.source, domain=b.domain, split=b.split,
            shards=tuple(
                ShardReceipt(path=r.path, sha256=f"{r.ordinal:064x}", bytes=4 * r.tokens,
                             tokens=r.tokens)
                for r in b.shards
            ),
            documents=1, tokens_in=b.tokens, tokens_out=b.tokens, tail_dropped=0,
            surplus_dropped=0, max_eos_fraction=0.01, wheel_version="0.9.1",
        )
        for b in parts
    ]
    # Every shard path is distinct and every digest is distinct — this build is CORRECT.
    paths = [s.path for r in receipts for s in r.shards]
    assert len(paths) == len(set(paths))

    codes = [v.code for v in verify_bundle_set(receipts, [b.stream for b in parts])]
    assert "bundle-set-duplicate-stream" in codes, (
        "if this no longer fires, the file-shard-aware fix has landed — INVERT this test rather "
        "than deleting it, and assert that a correct file-sharded set verifies clean."
    )
    assert "bundle-set-shard-path-collision" not in codes, (
        "the ordinal ranges really are disjoint; only the STREAM grouping is confused"
    )
# File-sharding: K children, one bundle, disjoint slices of the SOURCE FILES (§8A.5a)
# --------------------------------------------------------------------------------------
#
# `--shard/--of` strides BUNDLES, so before this a bundle was always one child on one instance and
# `stackv2-edu--train` was 107.46B = 51.38 h setting the whole makespan while 47 children idled.
#
# The union test below is the one that matters, and it is deliberately not a count. `_shard_slice`
# is `items[shard::of]`; the classic failure is an off-by-one that drops files, and a
# smaller-than-expected corpus does not look like an error — it looks like filter attrition. So the
# assertion recomputes the union of the ACTUAL paths read and compares it to the whole file list.


def _files_read_by(spec, bundle, tree):
    """The paths `_reader_for` actually opens for one bundle, over a fake HF tree.

    Drives the REAL `_reader_for` — not `_bundle_files` in isolation — so the test covers the wiring
    as well as the slice. The budget is made unreachable (one tiny document per file) so the read
    is bounded by the file list, which is what is under test; the budget's own stop is tested
    separately by `test_the_reader_stops_instead_of_walking_a_pool_far_larger_than_the_plan`.
    """
    import edullm_data.corpus_build as mod
    from edullm_data import corpus_read

    read = []

    def reader(repo, entry, sp, *a, **k):
        read.append(entry["path"])
        yield Document(id=f"{entry['path']}-0", text="tiny", source="tiny")

    real_hf, real_reader = mod.hf_files, corpus_read.read_parquet_documents
    mod.hf_files = lambda sp, headers=None: tree
    corpus_read.read_parquet_documents = reader
    try:
        list(mod._reader_for(spec, bundle))
    finally:
        mod.hf_files = real_hf
        corpus_read.read_parquet_documents = real_reader
    return read


def _sharded(bundle, shard: int, of: int):
    """One sibling of a K-way file split, with its own 1/K slice of the refs.

    The ref split mirrors what the plan does (`allocate_ordinals` at plan time): siblings hold
    DISJOINT refs, which is exactly the premise `_reader_for` relies on when it does not divide the
    read budget by K.
    """
    return dataclasses.replace(
        bundle, file_shard=(shard, of), shards=bundle.shards[shard::of]
    )


@pytest.mark.parametrize("n_files,k", [
    (57, 4),    # nemotron-cc-math-3 — 57/4 = 14,14,14,15. THE uneven case from the brief.
    (95, 7),    # stackv2-edu at its carve K — 95/7 = 13 or 14. Also uneven.
    (100, 4),   # finepdfs-edu — divides evenly.
    (57, 3),    # nemotron-cc-math-3 at its carve K — divides evenly (19 each).
    (46, 2),    # nemotron-cc-math-4plus — divides evenly.
    (10, 3),    # small and uneven, so an off-by-one is visible by eye in a failure.
    (5, 5),     # K == file count: every child gets exactly one file.
    (7, 1),     # K == 1: the whole source, i.e. the pre-file-sharding behaviour.
])
def test_the_union_of_files_read_across_k_children_is_exactly_the_file_list(n_files, k):
    """No file read twice, no file dropped — RECOMPUTED as a set union, never as a count.

    A count assertion would pass on a stride that read file 3 twice and never read file 7. The real
    failure mode is silent: files that no child reads are simply never in the corpus, and the
    shortfall surfaces at `verify` as unfilled refs that look identical to filter attrition.

    Both halves are asserted because they fail differently. Duplicates mean two children write the
    same documents (and, in the plan, the same ordinals — see `_assert_file_shard_family`); drops
    mean a silently short stream.
    """
    tree = [{"path": f"data/{i:05d}.parquet", "size": 1000} for i in range(n_files)]
    spec = _spec(target_tokens=SHARD_TOKENS * 8)
    whole = B.bundles_of(B.plan_document([spec]))[0]

    per_child = [_files_read_by(spec, _sharded(whole, i, k), tree) for i in range(k)]
    flat = [p for child in per_child for p in child]
    expected = [e["path"] for e in tree]

    assert sorted(flat) == sorted(expected), "the union of the K slices is not the file list"
    assert len(flat) == len(set(flat)), "a file was read by more than one child"
    assert set(flat) == set(expected), "a file was read by no child at all"
    # Uneven division must still cover: the child sizes may differ by ONE and no more, which is
    # what striding guarantees and what a contiguous or truncating split would not.
    sizes = sorted(len(c) for c in per_child)
    assert sizes[-1] - sizes[0] <= 1, f"stride left children unbalanced: {sizes}"


def test_striding_not_contiguous_blocks_on_a_realistically_skewed_source():
    """The stride must INTERLEAVE, because on the real sources the big files cluster.

    MEASURED (ENG-EXEC, 2026-08-08, HF tree API at the pinned revisions): `stackv2-edu`'s 95 files
    have CV 0.211 and 2.09x max/min, and the large ones are not spread evenly by name. Contiguous
    blocks give a 1.132x worst-child byte imbalance against striding's 1.026x — 8.35 h versus
    7.57 h on that one bundle. `_shard_slice`'s docstring asserts this; here it is measured.

    Asserted on BYTES, not on file counts: equal counts of unequal files is exactly the trap. The
    fixture puts the big files in one contiguous run, which is the shape the measurement found.
    """
    # 40 files; the last 12 are 4x the size of the rest, i.e. the "big ones cluster" shape.
    # 12 big files over K=4 is 3 apiece under a stride, so striding balances EXACTLY here — the
    # fixture is sized so the assertion can be a clean equality rather than an unjustifiable
    # tolerance. Contiguous blocks of 10 put ALL 12 big files in the last two children.
    tree = [{"path": f"data/{i:05d}.parquet", "size": 4000 if i >= 28 else 1000}
            for i in range(40)]
    by_path = {e["path"]: e["size"] for e in tree}
    spec = _spec(target_tokens=SHARD_TOKENS * 8)
    whole = B.bundles_of(B.plan_document([spec]))[0]
    k = 4

    strided = [sum(by_path[p] for p in _files_read_by(spec, _sharded(whole, i, k), tree))
               for i in range(k)]
    contiguous = [sum(e["size"] for e in tree[i * 10:(i + 1) * 10]) for i in range(k)]

    # Striding is PERFECTLY balanced on this fixture; contiguous is 3.4x imbalanced.
    assert max(strided) == min(strided), f"stride did not balance: {strided}"
    assert max(contiguous) / min(contiguous) > 3.0, "fixture does not model the clustering"
    assert max(strided) < max(contiguous), (
        "the worst child under striding must be lighter than under contiguous blocks — this is "
        "the whole reason _shard_slice strides"
    )


def test_the_slice_a_child_reads_does_not_depend_on_k_ordering_or_which_child_ran_first():
    """Determinism: the file set is a pure function of (spec, file_shard, file_shards).

    9 bundles / 4,137 shards previously re-ran byte-identical, and file-sharding must not cost that.
    Running the children in reverse order, and twice, must give each child the same slice.
    """
    tree = [{"path": f"data/{i:05d}.parquet", "size": 1000} for i in range(57)]
    spec = _spec(target_tokens=SHARD_TOKENS * 8)
    whole = B.bundles_of(B.plan_document([spec]))[0]

    forward = {i: _files_read_by(spec, _sharded(whole, i, 4), tree) for i in range(4)}
    backward = {i: _files_read_by(spec, _sharded(whole, i, 4), tree) for i in reversed(range(4))}
    assert forward == backward

    # And the list order within a child is stable, not just the set — `pack` concatenates in read
    # order, so a reordered slice is a different byte stream under the same plan_id.
    again = {i: _files_read_by(spec, _sharded(whole, i, 4), tree) for i in range(4)}
    assert forward == again
    for i in range(4):
        assert forward[i] == sorted(forward[i]), "hf_files is sorted; the slice must stay ordered"


def test_k_equals_one_reads_every_file_exactly_as_before_file_sharding():
    """The default must be byte-identical to the pre-file-sharding behaviour.

    Every plan written before this schema change omits `file_shard`/`file_shards`, so the defaults
    are not a convenience — they are what keeps an old plan readable.
    """
    tree = [{"path": f"data/{i:05d}.parquet", "size": 1000} for i in range(12)]
    spec = _spec(target_tokens=SHARD_TOKENS * 8)
    bundle = B.bundles_of(B.plan_document([spec]))[0]
    assert bundle.file_shard == (0, 1)
    assert _files_read_by(spec, bundle, tree) == [e["path"] for e in tree]

    # A plan entry with no file-shard keys at all reads as the whole source.
    entry = dict(B.plan_document([spec])["bundles"][0])
    assert "file_shards" not in entry, "plan_document is eng-11's; this test only reads it"
    assert B.Bundle.from_plan_entry(entry).file_shard_count == 1


def test_the_read_budget_is_not_divided_by_k_because_the_refs_already_are():
    """THE budget decision, asserted rather than asserted-in-a-comment.

    `budget = bundle.tokens * chars_per_token * headroom / keep_rate`, and `bundle.tokens` sums
    THIS bundle's refs. The plan gives each of K siblings its own disjoint refs, so `bundle.tokens`
    is already 1/K of the stream and dividing again would read 1/K of what the child needs — a
    `verify` failure on unfilled refs AFTER the full billable run.

    Measured through the reader: a K-way child must read ~1/K of the files the whole bundle reads,
    because it needs 1/K of the tokens from 1/K of the pool. Both halves scale, so the FRACTION of
    its own slice that it consumes is unchanged — which is why a bundle that could be filled before
    the split can still be filled after it.
    """
    tree = [{"path": f"data/{i:05d}.parquet", "size": 1000} for i in range(400)]
    spec = _spec(target_tokens=SHARD_TOKENS * 8)
    whole = B.bundles_of(B.plan_document([spec]))[0]
    k = 4

    def with_fat_files(bundle):
        """Each file holds a fixed slab of text, so 'files read' is a proxy for 'chars read'."""
        import edullm_data.corpus_build as mod
        from edullm_data import corpus_read

        read = []

        def reader(repo, entry, sp, *a, **k_):
            read.append(entry["path"])
            for i in range(20):
                yield Document(id=f"{entry['path']}-{i}",
                               text="x" * (SHARD_TOKENS * 4 // 20), source="tiny")

        real_hf, real_reader = mod.hf_files, corpus_read.read_parquet_documents
        mod.hf_files = lambda sp, headers=None: tree
        corpus_read.read_parquet_documents = reader
        try:
            list(mod._reader_for(spec, bundle))
        finally:
            mod.hf_files = real_hf
            corpus_read.read_parquet_documents = real_reader
        return len(read)

    whole_files = with_fat_files(whole)
    child_files = [with_fat_files(_sharded(whole, i, k)) for i in range(k)]

    # Each child stops after ~1/K of the whole bundle's read, because its refs are 1/K.
    assert whole_files > k, "fixture too small to distinguish"
    for i, n in enumerate(child_files):
        assert n <= -(-whole_files // k) + 1, (
            f"child {i} read {n} files against the whole bundle's {whole_files} over K={k}: the "
            f"budget is being applied as if the child owed the WHOLE stream's tokens"
        )
    # And it is not the opposite error either — a budget divided by K again would read ~1/K^2.
    assert sum(child_files) >= whole_files - k, (
        f"K children read {sum(child_files)} files against the whole bundle's {whole_files}: the "
        f"budget looks divided by K a second time, which starves every child"
    )


def test_a_bundle_whose_source_has_fewer_files_than_k_is_refused_before_reading():
    """`items[4::5]` on a 5-file source is `[]`, and an empty read does NOT fail on its own.

    The child yields nothing, packs nothing, writes a receipt, and leaves its refs unfilled — which
    at `verify` is indistinguishable from filter attrition. Caught here, before the first byte.
    """
    tree = [{"path": f"data/{i:05d}.parquet", "size": 1000} for i in range(3)]
    spec = _spec(target_tokens=SHARD_TOKENS * 8)
    whole = B.bundles_of(B.plan_document([spec]))[0]
    import edullm_data.corpus_build as mod

    real_hf = mod.hf_files
    mod.hf_files = lambda sp, headers=None: tree
    try:
        with pytest.raises(BuildError, match="only 3 payload files"):
            list(mod._reader_for(spec, _sharded(whole, 2, 5)))
    finally:
        mod.hf_files = real_hf


def test_an_out_of_range_file_shard_is_refused_when_the_bundle_is_built():
    """Out of range is not a no-op — it is an empty slice, i.e. a silently empty bundle."""
    with pytest.raises(BuildError, match="out of range"):
        B.Bundle(bundle_id="b", source="s", domain=None, split="train", spec_key="k",
                 shards=(), file_shard=(3, 3))
    with pytest.raises(BuildError, match="must be >= 1"):
        B.Bundle(bundle_id="b", source="s", domain=None, split="train", spec_key="k",
                 shards=(), file_shard=(0, 0))


def test_a_plan_that_gives_every_sibling_the_whole_shard_list_is_refused():
    """The failure the no-division decision would turn into a silent K-fold duplication.

    If all K siblings carry the full ref list, each reads K× what it needs and they all write the
    SAME ordinals. Nothing downstream sees it: token counts add up, ordinals are dense, shards
    decode, and in S3 the last writer wins. Only a whole-plan check can catch it.
    """
    spec = _spec(target_tokens=SHARD_TOKENS * 8)
    plan = B.plan_document([spec])
    train = next(e for e in plan["bundles"] if e["split"] == "train")
    plan["bundles"] = [
        {**train, "bundle_id": f"{train['bundle_id']}--f{i}", "file_shard": {"index": i, "of": 3}}
        for i in range(3)
    ]
    with pytest.raises(BuildError, match="claimed by BOTH file_shard"):
        B.bundles_of(plan)


def test_a_missing_sibling_is_refused_so_files_are_never_silently_unread():
    """file_shards=3 with only shards 0 and 1 present means every third file is read by nobody."""
    spec = _spec(target_tokens=SHARD_TOKENS * 8)
    plan = B.plan_document([spec])
    train = next(e for e in plan["bundles"] if e["split"] == "train")
    refs = train["shards"]
    plan["bundles"] = [
        {**train, "bundle_id": f"{train['bundle_id']}--f{i}", "file_shard": {"index": i, "of": 3},
         "shards": refs[i::3]}
        for i in range(2)          # sibling 2 is missing
    ]
    with pytest.raises(BuildError, match=r"file_shard \[0, 1\], not \[0, 1, 2\]"):
        B.bundles_of(plan)

    # The same three siblings, complete and disjoint, are accepted.
    plan["bundles"].append(
        {**train, "bundle_id": f"{train['bundle_id']}--f2", "file_shard": {"index": 2, "of": 3},
         "shards": refs[2::3]}
    )
    got = B.bundles_of(plan)
    assert sorted(b.file_shard_index for b in got) == [0, 1, 2]
    # Disjoint AND complete over the ordinals, recomputed.
    seen = [r.path for b in got for r in b.shards]
    assert sorted(seen) == sorted(refs) and len(seen) == len(set(seen))


def test_file_sharding_composes_with_the_finephrase_id_partition():
    """The two partitions are orthogonal and BOTH must still apply.

    FinePhrase keeps `sha256(id) % 4`; file-sharding keeps `files[k::K]`. A child of a sharded
    FinePhrase bundle must yield exactly the intersection — its own files, filtered to its own ids.
    Asserted against `format_for_id` recomputed on the spot, never via a spy on `keeps_id`,
    because the partition has a history of being green and uncalled.
    """
    from edullm_data.reservoir_ids import format_for_id

    ids = [f"<urn:uuid:0000-{i:06d}>" for i in range(600)]
    text = "lorem ipsum dolor " * 10
    tree = [{"path": f"data/{i:05d}.parquet", "size": 1000} for i in range(12)]
    spec = _fp_spec("faq", target_tokens=SHARD_TOKENS * 8)
    whole = B.bundles_of(B.plan_document([spec]))[0]

    def run(bundle):
        import edullm_data.corpus_build as mod
        from edullm_data import corpus_read

        files = []

        def reader(repo, entry, sp, *a, **k):
            files.append(entry["path"])
            # Each file holds a distinct 50-id slab, so a dropped file is a dropped id block.
            i = int(entry["path"][5:10])
            for doc_id in ids[i * 50:(i + 1) * 50]:
                yield Document(id=doc_id, text=text, source="synthetic-finephrase")

        real_hf, real_reader = mod.hf_files, corpus_read.read_parquet_documents
        mod.hf_files = lambda sp, headers=None: tree
        corpus_read.read_parquet_documents = reader
        try:
            out = list(mod._reader_for(spec, bundle))
        finally:
            mod.hf_files = real_hf
            corpus_read.read_parquet_documents = real_reader
        return {d.id for d in out}, files

    k = 5                                   # 12 files over 5 children: uneven
    per_child, all_files = [], []
    for i in range(k):
        got, files = run(_sharded(whole, i, k))
        per_child.append(got)
        all_files += files

    # The files still tile exactly.
    assert sorted(all_files) == sorted(e["path"] for e in tree)
    # The id partition still applies within every child — recomputed, not spied.
    for got in per_child:
        assert all(format_for_id(i) == "faq" for i in got)
    # And the union is exactly the faq partition of the whole id set: file-sharding must not
    # change WHICH documents the corpus contains, only which child reads them.
    union = set().union(*per_child)
    assert union == {i for i in ids if format_for_id(i) == "faq"}
    assert sum(len(c) for c in per_child) == len(union), "an id was yielded by two children"
