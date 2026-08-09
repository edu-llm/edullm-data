"""`artifacts/final-dataset/curriculum_driver.py` — THE FILE THAT HAD ZERO TESTS.

WHY THIS FILE EXISTS, AND IT IS THE AUDIT'S OWN ARGUMENT
--------------------------------------------------------
The 2026-08-09 red-team of the curriculum work found every severe defect in ONE file, and it was the
one file no test touched. Its ranked summary:

    F2  🔴🔴 driver declares block_count from a hard-coded `shards x 12,207` — ALREADY 2.40% wrong
            against the live build, on VAL, where it PASSES Gate A
    F3  🔴🔴 chunk-owner cursor drifts at every file-shard part boundary — 23.5% of the corpus
    F12 🔴   `profile={GROUP: PROFILE}` keys `mtld`; the groups are `mtld-train`/`mtld-val`
    F11 ⚠️   `curriculum_driver.py` has zero test coverage; F2/F3/F12 all live there

while the library modules it calls (`corpus_mtld`, `corpus_labels`, `corpus_order`) came out of the
same audit with their constants verified bit-for-bit. **That distribution is not a coincidence — it
is the argument for the coverage rule, stated by the defect distribution itself.**

TWO FIXTURE PROPERTIES THAT ARE THE WHOLE POINT
------------------------------------------------
1. **Shards are NOT uniform length.** Every pre-existing axis fixture in the suite used a uniform
   `tokens_each`, which is precisely the shape F2 turns on: with every shard exactly full, the
   hard-coded product and the derived count AGREE and the defect is invisible. The manifests here
   carry short tail shards, so the two numbers differ and the test can tell which one was used.
2. **Streams are MULTI-PART.** No pre-existing fixture built one, which is the shape F3 turns on.

The driver is loaded by path (it lives in `artifacts/`, not in the package) and every S3 call is
faked — `parent_manifest`, `load_streams` and `load_receipts` are the three seams.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from edullm_data.corpus import BuildError
from edullm_data.corpus_order import SEQ_LEN_CHUNK, StreamLabels, chunk_counts

DRIVER = Path(__file__).resolve().parents[1] / "artifacts/final-dataset/curriculum_driver.py"


def _load():
    """Fresh module per test, so a test that rebinds a module global cannot leak into another."""
    spec = importlib.util.spec_from_file_location("curriculum_driver_under_test", DRIVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


D = _load()
PER_SHARD = chunk_counts([D.PLAN_SHARD_TOKENS])[0]
FULL_BYTES = D.PLAN_SHARD_TOKENS * 4


def _manifest(train_bytes: list[int], val_bytes: list[int]) -> dict:
    """A parent manifest with EXPLICIT per-shard byte counts, so short shards are expressible."""
    entries = [
        {"path": f"tokens/src/train-{i:05d}.u32le.bin", "bytes": b}
        for i, b in enumerate(train_bytes)
    ] + [
        {"path": f"tokens/src/val-{i:05d}.u32le.bin", "bytes": b}
        for i, b in enumerate(val_bytes)
    ]
    return {"entries": entries}


# ======================================================================================
# F2 — the count must be DERIVED. This is the test that would have caught it.
# ======================================================================================


def test_build_axis_derives_the_chunk_count_and_does_not_return_the_constant_product(monkeypatch):
    """🔴 **F2. The assertion the whole finding reduces to.**

    `build_axis` always computed the right answer and the driver then used
    `PLAN_TRAIN_SHARDS x 12,207` instead — a product that assumes every shard is exactly full.
    MEASURED against the live build, ONE source (`stackv2-edu`): 6 short train shards = 38,736 chunks
    short, and **7 short val shards = 29,880 = 2.40% error on val already, at 88% done.**

    The fixture has one short shard, so the derived count and the product DIFFER — which is what
    makes this test able to tell them apart. A uniform fixture cannot.
    """
    mod = _load()
    monkeypatch.setattr(mod, "PLAN_TRAIN_SHARDS", 3)
    monkeypatch.setattr(mod, "PLAN_VAL_SHARDS", 1)
    short_tokens = 5_000_000
    man = _manifest([FULL_BYTES, FULL_BYTES, short_tokens * 4], [FULL_BYTES])

    axis = mod.build_axis(man, split="train")

    product = 3 * PER_SHARD
    derived = 2 * PER_SHARD + (short_tokens - 1) // SEQ_LEN_CHUNK
    assert axis.n_chunks == derived
    assert axis.n_chunks != product, "the fixture must discriminate, or this proves nothing"
    assert product - axis.n_chunks > 0, "a short shard makes the product an OVER-declaration"


def test_a_short_shard_is_REPORTED_and_the_derived_value_is_the_one_used(monkeypatch, capsys):
    """The disagreement is a real signal — a short tail shard is the normal shape — so it must be
    printed rather than silently absorbed. What was a defect is declaring the bound as the count."""
    mod = _load()
    monkeypatch.setattr(mod, "PLAN_TRAIN_SHARDS", 2)
    monkeypatch.setattr(mod, "PLAN_VAL_SHARDS", 1)
    man = _manifest([FULL_BYTES, 5_000_000 * 4], [FULL_BYTES])

    mod.build_axis(man, split="train")
    out = capsys.readouterr().out
    assert "DERIVED" in out
    assert "UPPER BOUND" not in out  # that phrasing belongs to main()'s pre-S3 print
    assert "THE DERIVED VALUE IS USED" in out
    assert "short:" in out and "train-00001" in out


def test_an_all_full_manifest_reports_exact_agreement(monkeypatch, capsys):
    """The complement. A warning that always fires tells an operator nothing, so the agreeing case
    must be visibly distinct — and it is the case where the old constant happened to be right."""
    mod = _load()
    monkeypatch.setattr(mod, "PLAN_TRAIN_SHARDS", 2)
    monkeypatch.setattr(mod, "PLAN_VAL_SHARDS", 1)
    axis = mod.build_axis(_manifest([FULL_BYTES, FULL_BYTES], [FULL_BYTES]), split="train")
    assert axis.n_chunks == 2 * PER_SHARD
    assert "agrees exactly" in capsys.readouterr().out


def test_build_axis_derives_the_VAL_count_too_and_val_is_the_dangerous_half(monkeypatch):
    """🔴 **VAL is where F2 SHIPS.**

    The train pair fails Gate A loudly (`permutation-wrong-length`): a 6-7 h publish thrown away,
    recoverable. The val pair was **internally consistent** — `identity_order(constant)` and a
    declared `block_count` from the same constant — so **it PASSED Gate A** while indexing 29,880+
    chunks the parent does not have, which crashes the trainer's launch or reads past the end.

    So `build_axis` must accept `split="val"`. Before the fix there was no val axis at all.
    """
    mod = _load()
    monkeypatch.setattr(mod, "PLAN_TRAIN_SHARDS", 1)
    monkeypatch.setattr(mod, "PLAN_VAL_SHARDS", 3)
    short = 1_000_000
    man = _manifest([FULL_BYTES], [FULL_BYTES, FULL_BYTES, short * 4])

    val_axis = mod.build_axis(man, split="val")
    assert val_axis.n_chunks == 2 * PER_SHARD + (short - 1) // SEQ_LEN_CHUNK
    assert val_axis.n_chunks != 3 * PER_SHARD
    # And it really is the VAL shards, not the train ones.
    assert all("val-" in k for k in val_axis.keys)
    assert len(val_axis.keys) == 3


def test_a_shard_count_mismatch_against_the_plan_is_refused_per_split(monkeypatch):
    """A missing shard shifts every chunk index past it and the result is still bijective. The check
    has to use the split's OWN expected count — using the train count for val would refuse every
    healthy val axis."""
    mod = _load()
    monkeypatch.setattr(mod, "PLAN_TRAIN_SHARDS", 5)
    monkeypatch.setattr(mod, "PLAN_VAL_SHARDS", 2)
    man = _manifest([FULL_BYTES, FULL_BYTES], [FULL_BYTES, FULL_BYTES])
    with pytest.raises(SystemExit, match="holds 2 train shards, the plan says 5"):
        mod.build_axis(man, split="train")
    # val has exactly its 2, so it must pass — the counts are not interchangeable.
    assert len(mod.build_axis(man, split="val").keys) == 2


# ======================================================================================
# F12 — the profile mapping key. `--go` could never have succeeded.
# ======================================================================================


def test_the_profile_mapping_is_keyed_by_the_SUFFIXED_group_names():
    """🔴 **F12, and it is proof `--go` had never been run.**

    `publish.build_plan` groups files by FIRST PATH SEGMENT, and this driver stages
    `{GROUP}-train/` and `{GROUP}-val/` — so the groups are `mtld-train` and `mtld-val`. The mapping
    was `{GROUP: PROFILE}`, keying the bare `mtld`, which no group is called; `profile_for` raises
    `PublishError` on a group with no entry. `group_meta_for` already had the suffixes right, so the
    author knew and missed the one line that also needed them.

    Asserted against the driver's OWN source, because the call is inside `main()`'s publish branch
    which cannot be reached without S3 and a promoted parent.
    """
    mod = _load()
    code = _code_text(mod.main)
    # Executable text, comments and strings stripped — see `_code_text`. The mapping keys are
    # f-strings, so what survives tokenisation is the dict shape and the PROFILE references.
    assert 'profile = { "" : PROFILE , "" : PROFILE }' in code, code[-400:]
    # And the resolution really succeeds for both group names, replicating publish()'s own closure
    # (`publish.py:401-409`) rather than trusting the shape.
    from edullm_data.publish import PublishError

    def profile_for(g, mapping):
        if g in mapping:
            return mapping[g]
        raise PublishError(f"group {g!r} has no profile in the profile mapping {dict(mapping)!r}")

    good = {f"{mod.GROUP}-train": mod.PROFILE, f"{mod.GROUP}-val": mod.PROFILE}
    for g in (f"{mod.GROUP}-train", f"{mod.GROUP}-val"):
        assert profile_for(g, good) == mod.PROFILE
        # The OLD mapping raises on both — the defect, reproduced so the fix is known to matter.
        with pytest.raises(PublishError, match="has no profile in the profile mapping"):
            profile_for(g, {mod.GROUP: mod.PROFILE})


def test_the_group_names_the_driver_stages_are_exactly_the_ones_it_declares():
    """The invariant behind F12, checked structurally rather than by string match.

    Three places must agree: the directories staged, the `group_meta_for` keys, and the profile
    mapping keys. F12 was two of three agreeing.
    """
    import inspect

    mod = _load()
    src = inspect.getsource(mod.main)
    staged = {f"{mod.GROUP}-train", f"{mod.GROUP}-val"}
    assert "{GROUP}-train" in src and "{GROUP}-val" in src, "the staged directory names moved"

    mod.PARENT_VERSION, mod.PARENT_MANIFEST_SHA256 = "v1", "a" * 64
    meta = mod.group_meta_for(100, 10)
    assert set(meta) == staged, "group_meta_for's keys must be the staged directory names"
    # `publish.build_plan` groups by FIRST PATH SEGMENT, so the group name IS the directory name.
    # That is the whole chain F12 broke, and all three links are now asserted together.
    assert {k.split("/")[0] for k in (f"{mod.GROUP}-train/x", f"{mod.GROUP}-val/y")} == staged


# ======================================================================================
# F3 — one StreamLabels PER PART, and the mapping from RECEIPTS
# ======================================================================================


class _FakeS3:
    """The three seams `load_streams`/`load_receipts` use: `client.list_objects_v2` and `get`."""

    def __init__(self, objects: dict[str, bytes]):
        self._o = dict(objects)
        outer = self

        class _C:
            def list_objects_v2(self, **kw):
                pre = kw["Prefix"]
                keys = sorted(k for k in outer._o if k.startswith(pre))
                return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

        self.client = _C()

    def get(self, bucket, key):
        return self._o[key]


def _labels_object(mod, *, source, split, part, of, strides, scores):
    from edullm_data.corpus_labels import DocumentLabel, build_label_set, encode_labels

    bundle = f"{source}--{split}" + (f"--p{part:02d}of{of:02d}" if of > 1 else "")
    rows = [
        DocumentLabel(source_doc=i, n_tokens=int(s) - 1, source_path="data/p0.parquet",
                      mtld=float(m))
        for i, (s, m) in enumerate(zip(strides, scores))
    ]
    ls = build_label_set(rows, plan_id=mod.PLAN_ID, bundle_id=bundle,
                         stream=(source, None, split), tokens_in=int(sum(strides)))
    return bundle, encode_labels(ls)


def test_load_streams_returns_ONE_StreamLabels_PER_PART_and_never_concatenates(monkeypatch):
    """🔴 **F3 on the driver side.**

    `load_streams` used to `np.concatenate` a stream's K parts into one token space. Each part is its
    own `pack()` call with its own tail truncation, so `tokens_in - tokens_out` is a PER-PART
    quantity — and `build_order`'s cursor, advancing by `tokens_out`, accumulated one gap per
    boundary. Every chunk after the first boundary was attributed to the wrong document, with
    `build_order` returning a perfect permutation and raising nothing.

    So: two parts in, TWO `StreamLabels` out, each carrying its own `part`.
    """
    mod = _load()
    objs = {}
    for part, (n, base) in enumerate([(4, 100.0), (3, 50.0)]):
        bundle, body = _labels_object(
            mod, source="src", split="train", part=part, of=2,
            strides=[801] * n, scores=[base + i for i in range(n)],
        )
        objs[f"{mod.PREFIX}/{mod.PLAN_ID}/_labels/{bundle}.labels"] = body

    streams = mod.load_streams(_FakeS3(objs), split="train")
    assert len(streams) == 2, "the two parts were concatenated — that IS the F3 defect"
    assert sorted(s.part for s in streams) == [0, 1]
    assert {s.key for s in streams} == {("src", None, "train", 0), ("src", None, "train", 1)}
    # Each part keeps its OWN documents, not the union.
    by_part = {s.part: s for s in streams}
    assert by_part[0].documents == 4 and by_part[1].documents == 3
    # And the scores did not get merged: part 1 is the lower-scoring one.
    assert float(by_part[1].mtld.max()) < float(by_part[0].mtld.min())


def test_load_streams_filters_by_split_so_val_labels_never_enter_the_train_axis(monkeypatch):
    """The previous code hard-coded `"train"` into the mapping it built, so a val label set would
    have been ranked into the train curriculum. A val document in the train order both orders
    held-out data and shifts every index past it."""
    mod = _load()
    objs = {}
    for split in ("train", "val"):
        bundle, body = _labels_object(mod, source="src", split=split, part=0, of=1,
                                      strides=[801, 801], scores=[1.0, 2.0])
        objs[f"{mod.PREFIX}/{mod.PLAN_ID}/_labels/{bundle}.labels"] = body
    s3 = _FakeS3(objs)
    assert [s.split for s in mod.load_streams(s3, split="train")] == ["train"]
    assert [s.split for s in mod.load_streams(s3, split="val")] == ["val"]


def test_a_missing_part_is_refused(monkeypatch):
    """A gap means those documents were never labelled, so their chunks have no difficulty."""
    mod = _load()
    bundle, body = _labels_object(mod, source="src", split="train", part=1, of=2,
                                  strides=[801], scores=[1.0])
    objs = {f"{mod.PREFIX}/{mod.PLAN_ID}/_labels/{bundle}.labels": body}
    with pytest.raises(SystemExit, match="file_shard indices"):
        mod.load_streams(_FakeS3(objs), split="train")


def test_labels_from_another_plan_are_refused(monkeypatch):
    mod = _load()
    from edullm_data.corpus_labels import DocumentLabel, build_label_set, encode_labels

    ls = build_label_set(
        [DocumentLabel(source_doc=0, n_tokens=800, source_path="p", mtld=1.0)],
        plan_id="another-plan-id", bundle_id="src--train", stream=("src", None, "train"),
        tokens_in=801,
    )
    objs = {f"{mod.PREFIX}/{mod.PLAN_ID}/_labels/src--train.labels": encode_labels(ls)}
    with pytest.raises(SystemExit, match="declares plan_id"):
        mod.load_streams(_FakeS3(objs), split="train")


def test_no_labels_at_all_is_refused_naming_the_missing_flag(monkeypatch):
    """The build ran without `--labels`. This is the message an operator will actually hit if F13's
    job-def half is missed, so it must name the cause."""
    mod = _load()
    with pytest.raises(SystemExit, match="ran without --labels"):
        mod.load_streams(_FakeS3({}), split="train")


def test_load_receipts_refuses_an_empty_set_because_the_part_would_be_unrecoverable(monkeypatch):
    """🔴 Without receipts the shard -> part mapping cannot be built at all, and the tempting
    fallback (`labels_from_path`) collapses every shard onto part 0 — which is F3."""
    mod = _load()
    with pytest.raises(SystemExit, match="no receipts under"):
        mod.load_receipts(_FakeS3({}))


def _code_text(fn) -> str:
    """Executable text of `fn`: comments removed, string literals blanked."""
    import inspect
    import io
    import textwrap
    import tokenize

    src = textwrap.dedent(inspect.getsource(fn))
    keep = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING:
            keep.append('""')  # a name inside a docstring or message is not a call
            continue
        keep.append(tok.string)
    return " ".join(keep)


def test_the_driver_no_longer_builds_its_mapping_from_labels_from_path():
    """The mapping SOURCE, asserted on the driver's EXECUTABLE source.

    `labels_from_path` cannot name a part — MEASURED, it returns `{'source': ...}` for a real shard
    key — so a mapping built from it is the F3 collapse with a plausible-looking implementation.

    Comments and strings are stripped first; see `_code_text`. The naive text assertion failed on the
    comment explaining the fix, which is the same trap the audit's F13 turned on.
    """
    mod = _load()
    code = _code_text(mod.main)
    assert "shard_stream_from_receipts" in code
    assert "labels_from_path" not in code, (
        "the mapping is being derived from the shard KEY again; it structurally cannot carry the "
        "file-shard part, so every shard would collapse onto part 0 of its stream"
    )


# ======================================================================================
# assert_invariants — the refusals that must survive
# ======================================================================================


def test_the_driver_refuses_to_run_with_an_unfilled_parent_pin():
    """`token_order_v1` requires `depends_on`; omitting it makes `_block_count` return None and
    `check_order_domain` then checks `bincount == 1` against the vector's OWN length — VACUOUS, not
    failing. So an unfilled pin must stop the driver rather than default."""
    mod = _load()
    assert mod.PARENT_MANIFEST_SHA256 is None and mod.PARENT_VERSION is None
    with pytest.raises(SystemExit, match="PARENT_VERSION / PARENT_MANIFEST_SHA256 are unfilled"):
        mod.assert_invariants()


def test_max_order_bytes_must_exceed_the_train_vector_or_gate_a_never_loads_it():
    """`check_order_domain` returns `order-too-large` and never reads the vector, so Gate A fails on
    the artifact it exists to check. Asserted by breaking it, not by reading the constant."""
    mod = _load()
    mod.PARENT_VERSION, mod.PARENT_MANIFEST_SHA256 = "v1", "a" * 64
    mod.MAX_ORDER_BYTES = 1024
    with pytest.raises(SystemExit, match="does not exceed the train vector"):
        mod.assert_invariants()


def test_group_meta_declares_two_groups_each_with_its_own_block_count():
    """`check_order_domain` reads ONE group-level `block_count` and applies it to every entry, so a
    478 M-index train permutation and a 51-index val identity in one group means the val object trips
    `permutation-wrong-length` with certainty."""
    mod = _load()
    mod.PARENT_VERSION, mod.PARENT_MANIFEST_SHA256 = "v1", "b" * 64
    meta = mod.group_meta_for(478_575_435, 1_245_114)
    assert meta["mtld-train"]["block_count"] == 478_575_435
    assert meta["mtld-val"]["block_count"] == 1_245_114
    # The pin is the SAME parent on both, and the block_count inside depends_on matches the group's.
    for g, n in (("mtld-train", 478_575_435), ("mtld-val", 1_245_114)):
        dep = meta[g]["depends_on"][0]
        assert dep["manifest_sha256"] == "b" * 64
        assert dep["block_count"] == n
    # max_order_bytes on BOTH — `_cfg` reads per group, so one leaves the other at 512 MiB.
    assert meta["mtld-train"]["max_order_bytes"] == meta["mtld-val"]["max_order_bytes"]
    # val is the identity and says so.
    assert meta["mtld-val"]["easy_to_hard"] is False
    assert meta["mtld-train"]["easy_to_hard"] is True


def test_main_DECLARES_the_derived_count_and_not_the_constant_product():
    """🔴 **The gap my first pass left, and it is exactly the F2 shape one level up.**

    `test_build_axis_derives_...` proves `build_axis` computes the right number. It does NOT prove
    `main()` USES it — and "computes the right answer, then declares a constant instead" is precisely
    what F2 was. MEASURED: mutating `main()` to `n_train = upper_train` left all 17 other tests
    GREEN. So the binding must be asserted on `main()`'s own executable text.

    A test that verifies a helper while the caller ignores it is the same defect as the code it
    tests, and I wrote one before catching it.
    """
    mod = _load()
    code = _code_text(mod.main)
    assert "n_train = axis . n_chunks" in code, (
        "main() is not declaring the DERIVED train count — this is F2 restored"
    )
    assert "n_val = val_axis . n_chunks" in code, (
        "main() is not declaring the DERIVED val count. Val is the half that PASSES Gate A while "
        "wrong, because identity_order() and block_count come from the same constant."
    )
    # The constant products must survive only as printed bounds, never as the declared values.
    assert "n_train = upper_train" not in code and "n_val = upper_val" not in code
    assert "upper_train" in code and "upper_val" in code, "the bounds should still be reported"
    # And a second axis really is built for val — before the fix there was none.
    assert 'val_axis = build_axis ( manifest , split = "" )' in code


def test_main_cross_checks_build_order_against_the_derived_axis():
    """`order.size` and the declared `block_count` cannot disagree: one would be right and the other
    wrong, and Gate A compares the vector against the declaration, not against the parent."""
    mod = _load()
    code = _code_text(mod.main)
    assert "order . size != n_train" in code, (
        "nothing checks the built vector's length against the number that gets declared"
    )


def test_BOTH_drivers_pin_the_SAME_plan_id_as_the_checked_in_registry():
    """🔴 **A stale `PLAN_ID` in a driver is SILENT, and it is a prefix.**

    `PLAN_ID` is a path component: `_ingest/final-dataset/<PLAN_ID>/`. A stale value points at an
    empty or half-built prefix, so `load_streams` reports *"no labels"* and `load_receipts` reports
    *"no receipts"* — diagnostics that read like "the build has not finished", not like
    "you are looking in the wrong place". `publish_driver`'s `SOURCE` is built from it too, so a
    stale value there publishes a prefix that is not this corpus.

    Three places must agree and there is no mechanism that makes them: the registry (which DERIVES
    the id), and the two drivers (which hard-code it). So it is asserted, against the value
    recomputed from the registry rather than against a second literal.
    """
    from edullm_data.corpus_build import load_registry, plan_document

    specs, meta = load_registry("artifacts/final-dataset/corpus-registry.json")
    derived = plan_document([s for s in specs if s.target_tokens > 0],
                            registry_meta=meta)["plan_id"]

    from tests.test_curriculum_labels import FROZEN_PLAN_ID

    assert derived == FROZEN_PLAN_ID, "the registry and its own test literal disagree"
    assert _load().PLAN_ID == derived, (
        f"curriculum_driver.PLAN_ID is {_load().PLAN_ID!r}, the registry derives {derived!r}. This "
        f"is a PREFIX: the driver would read an empty _labels/ and report 'no labels'."
    )

    pub_spec = importlib.util.spec_from_file_location(
        "publish_driver_under_test",
        Path(__file__).resolve().parents[1] / "artifacts/final-dataset/publish_driver.py",
    )
    pub = importlib.util.module_from_spec(pub_spec)
    pub_spec.loader.exec_module(pub)
    assert pub.PLAN_ID == derived, (
        f"publish_driver.PLAN_ID is {pub.PLAN_ID!r}, the registry derives {derived!r}. Its SOURCE "
        f"is built from it, so it would publish a prefix that is not this corpus."
    )
    assert derived in pub.SOURCE, "SOURCE must be derived from PLAN_ID, not written out separately"
