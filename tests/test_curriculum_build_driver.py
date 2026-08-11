"""End-to-end test of the curriculum ORDER GENERATOR on a synthetic corpus.

``artifacts/final-dataset/curriculum/build_curriculum.py`` is a driver, not library code, so it
is exercised here against a fixture small enough to check by hand but shaped like the real
corpus: two sources with DISJOINT MTLD ranges (the defect-2 shape), a file-sharded stream with
two parts whose ``tokens_in`` exceeds ``tokens_out`` by DIFFERENT amounts (the F3 shape), and a
runaway upper tail (the defect-1 shape).

Every assertion here was MUTATION-PROVEN — see each test's docstring for the mutation used and
what failed. The fixture asserts its own shape first (vacuity guards): a fixture that cannot
exercise a defect makes every downstream assertion decoration.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

import numpy as np
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DRIVER = os.path.join(ROOT, "artifacts", "final-dataset", "curriculum", "build_curriculum.py")

sys.path.insert(0, os.path.join(ROOT, "src"))
from edullm_data.corpus_labels import LABEL_DTYPE  # noqa: E402
from edullm_data.corpus_mtld import MTLD_SPEC_ID  # noqa: E402

SEQ = 2048
#: 512 chunks' worth of tokens per shard -> 511 chunks under (t-1)//seq, 512 under t//seq.
#:
#: Sized so a 1% head is ~40 chunks. Two properties pull in opposite directions and both must
#: hold at once:
#:
#: * TIES need documents LONGER than one 2,048-token chunk, because a chunk inherits its owner
#:   document's key and only a multi-chunk document makes several chunks share one. That is how
#:   the real corpus ties (mean document length 263..8,301 tokens).
#: * PROPORTIONALITY needs the 1% head to hold enough chunks to span several sources; if the head
#:   is one chunk it can only ever come from one source, and the assertion fails on arithmetic
#:   rather than on the algorithm.
#:
#: At 64 chunks/shard the head was 5 chunks and documents were 98 tokens — neither held. 512
#: chunks/shard with ~150 documents per part gives ~2.5-chunk documents AND a ~40-chunk head.
SHARD_TOKENS = 512 * SEQ  # 1,048,576


def _load_driver():
    spec = importlib.util.spec_from_file_location("build_curriculum", DRIVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_labels(path, bundle_id, source, split, part, n_tokens, mtld):
    """A labels object in the real on-disk form: magic + u32 header_len + JSON + N*14."""
    n = len(n_tokens)
    rec = np.zeros(n, dtype=LABEL_DTYPE)
    rec["source_doc"] = np.arange(n, dtype=np.uint32)
    rec["n_tokens"] = np.asarray(n_tokens, dtype=np.uint32)
    rec["mtld"] = np.asarray(mtld, dtype=np.float32)
    rec["path_id"] = 0
    hdr = {
        "schema": "edullm-doc-labels/v1",
        "plan_id": "79e53d1e5e131649",
        "bundle_id": bundle_id,
        "stream": {"source": source, "domain": None, "split": split},
        "mtld_spec": MTLD_SPEC_ID,
        "item_size": 14,
        "documents": n,
        "tokens_in": int((rec["n_tokens"].astype(np.int64) + 1).sum()),
        "paths": ["f0"],
    }
    body = json.dumps(hdr, sort_keys=True, separators=(",", ":")).encode()
    with open(path, "wb") as fh:
        fh.write(b"EDULLBL1" + len(body).to_bytes(4, "little") + body + rec.tobytes())
    return hdr["tokens_in"]


def _build_fixture(tmp_path, *, tail=True, parts=2, doc_len_ratio=1):
    """Two sources; `code` is file-sharded into `parts` parts, `prose` is one part.

    Each part's labelled tokens deliberately EXCEED its shards' tokens by a different amount,
    which is the file-shard drift shape that made `StreamLabels.part` necessary.
    """
    art = tmp_path / "artifacts" / "final-dataset"
    (art / "index").mkdir(parents=True)
    (art / "receipts_all").mkdir()
    (art / "labels_raw").mkdir()
    (art / "curriculum").mkdir()

    shards, receipts = [], []
    ordinal = 0

    def add_part(source, part, n_parts, shard_count, scores, surplus):
        nonlocal ordinal
        paths = []
        for _ in range(shard_count):
            rp = f"data/tokens/{source}/train-{ordinal:05d}.u32le.bin"
            paths.append(rp)
            bid = (
                f"{source}--train--p{part:02d}of{n_parts:02d}" if n_parts > 1 else f"{source}--train"
            )
            shards.append(
                {
                    "s3": "s3://b/" + rp,
                    "relative_path": rp,
                    "tokens": SHARD_TOKENS,
                    "bytes": SHARD_TOKENS * 4,
                    "source": source,
                    "bundle_id": bid,
                    "split": "train",
                    "sha256": "0" * 64,
                }
            )
            ordinal += 1
        held = SHARD_TOKENS * shard_count
        # documents summing (n_tokens+1) to held + surplus
        n_docs = len(scores)
        base = (held + surplus) // n_docs
        nt = [base - 1] * n_docs
        short = (held + surplus) - sum(x + 1 for x in nt)
        nt[-1] += short
        assert sum(x + 1 for x in nt) == held + surplus
        bid = f"{source}--train--p{part:02d}of{n_parts:02d}" if n_parts > 1 else f"{source}--train"
        ti = _write_labels(
            art / "labels_raw" / f"{bid}.labels", bid, source, "train", part, nt, scores
        )
        receipts.append(
            {
                "schema_version": "edullm-corpus-receipt/v2",
                "plan_id": "79e53d1e5e131649",
                "bundle_id": bid,
                "stream": {"source": source, "domain": None, "split": "train"},
                "file_shard": {"index": part, "of": n_parts},
                "labels": {"documents": n_docs, "tokens_in": ti, "bytes": 0},
                "pack": {
                    "documents": n_docs,
                    "tokens_in": ti,
                    "tokens_out": held,
                    "tail_dropped": 0,
                    "surplus_dropped": surplus,
                },
                "shards": [{"path": p[len("data/") :], "bytes": SHARD_TOKENS * 4} for p in paths],
            }
        )

    rng = np.random.default_rng(11)
    # `code`: LOW scores (the low-register source), split into parts with DIFFERENT surpluses.
    #
    # ⚠️ `tie_mass` is not decoration. With continuous random scores the resulting chunk keys are
    # almost all distinct (MEASURED: 501 distinct of 504), and 3 tied pairs is far too sparse for
    # numpy's introsort to reorder — so a `kind="quicksort"` mutation survived every test. Giving
    # a large block of documents ONE repeated score forces many chunks to share a key, which is
    # what makes sort stability observable at all. The real corpus has this shape for free: the
    # clamp alone ties 825,415 documents at the threshold.
    #
    # ⚠️ **Document count also controls whether chunk keys tie at all, and that is the second
    # thing this fixture must get right.** A chunk inherits its OWNER document's key, so a
    # document LONGER than one 2,048-token chunk owns several chunks that all share one key —
    # which is how the real corpus ties (mean document length runs 263..8,301 tokens against a
    # 2,048-token chunk). With 4,000 documents over 3 shards each document is ~98 tokens, far
    # SHORTER than a chunk, so every chunk had a distinct owner and a distinct key: MEASURED 504
    # distinct keys of 504 chunks (at the older, smaller shard size), and a `kind="quicksort"` mutation survived. 150 documents makes
    # each ~2,621 tokens — longer than a chunk — so ties appear the same way they do in production.
    n_code = 800
    for p in range(parts):
        sc = rng.uniform(10.0, 45.0, n_code)
        sc[n_code // 4 : (3 * n_code) // 4] = 22.5  # tie_mass at one exact score
        if tail and p == 0:
            sc[:5] = [5e3, 5e4, 5e6, 3.15e8, 1e7]  # the runaway tail
        add_part("code", p, parts, 3, sc, surplus=17 + 100 * p)
    # `prose`: HIGH scores, one part, disjoint from code. `doc_len_ratio` shrinks the DOCUMENT
    # count while the shard count stays fixed, so prose documents become that many times longer
    # than code's — which is what makes the token- vs document-denominator difference visible.
    n_prose = max(4, 1200 // doc_len_ratio)
    pr = rng.uniform(150.0, 260.0, n_prose)
    pr[n_prose // 6 : (2 * n_prose) // 3] = 200.0  # tie_mass in the second source too
    add_part("prose", 0, 1, 2, pr, surplus=41)

    shards.sort(key=lambda s: s["relative_path"])
    json.dump(
        {
            "shards": shards,
            "totals": {"shards": len(shards)},
        },
        open(art / "index" / "INDEX-FINAL.json", "w"),
    )
    for r in receipts:
        json.dump(r, open(art / "receipts_all" / f"{r['bundle_id']}.json", "w"))
    return art


def _run(art, tmp_path):
    """Run the driver with its module-level paths repointed at the fixture."""
    mod = _load_driver()
    cur = str(art / "curriculum")
    mod.ART = str(art)
    mod.INDEX = str(art / "index" / "INDEX-FINAL.json")
    mod.RECEIPTS = str(art / "receipts_all")
    mod.LABELS = str(art / "labels_raw")
    mod.OUT = cur
    mod.SCRATCH = str(art / "curriculum" / "_scratch")
    rc = mod.main()
    assert rc == 0
    spec = json.load(open(os.path.join(cur, "CURRICULUM-SPEC.json")))
    stats = json.load(open(os.path.join(cur, "CURRICULUM-STATS.json")))
    order = np.fromfile(os.path.join(cur, "edu-mix-983b-train.u32le.bin"), dtype="<u4")
    return mod, spec, stats, order


def test_the_order_is_a_complete_permutation_of_the_chunk_pool(tmp_path):
    """The one check Gate A also makes, on a fixture whose chunk count is hand-computable.

    MUTATION: writing `order[:-1]` (a truncated vector) -> bincount has a zero; FAILS.
    MUTATION: `np.argsort(...)[::-1]` still permutes, so this test alone cannot catch a reversed
    curriculum — that is what test_the_head_is_easier_than_the_tail is for."""
    art = _build_fixture(tmp_path)
    _mod, spec, _stats, order = _run(art, tmp_path)

    n_shards = 3 * 2 + 2  # code: 2 parts x 3 shards; prose: 2
    per_shard = (SHARD_TOKENS - 1) // SEQ
    expected = n_shards * per_shard
    # VACUITY GUARD: many chunks per shard, or a chunk/shard confusion would be invisible;
    # and a 1% head must be more than one chunk, or proportionality is inexpressible.
    assert per_shard == 511
    assert expected == 4088
    assert expected * 0.01 > 1

    assert spec["block_count"] == expected
    assert order.size == expected
    assert order.dtype == np.dtype("<u4")
    bc = np.bincount(order, minlength=expected)
    assert np.all(bc == 1), (bc.min(), bc.max())
    assert spec["permutation_check"]["valid"] is True
    assert spec["permutation_check"]["chunks_never_appearing"] == 0
    assert spec["permutation_check"]["chunks_appearing_more_than_once"] == 0


def test_the_head_is_easier_than_the_tail_so_the_sort_direction_is_right(tmp_path):
    """A reversed sort is bijective and passes every structural check. This is the direction test.

    MUTATION: sorting on `-ckey` (hard-to-easy) -> the head's mean percentile exceeds the
    tail's; FAILS. This is the 'single most consequential one-character error' corpus_order
    names, so it gets its own assertion."""
    art = _build_fixture(tmp_path)
    mod, _spec, _stats, order = _run(art, tmp_path)
    key = np.fromfile(
        os.path.join(str(art / "curriculum" / "_scratch"), "chunk_key.u32"), dtype=np.uint32
    )
    # VACUITY GUARD: the keys must not be constant, or direction is unobservable.
    assert np.unique(key).size > 2
    k = key[order]
    assert np.all(np.diff(k.astype(np.int64)) >= 0), "order is not ascending in the key"
    assert k[: len(k) // 4].mean() < k[-len(k) // 4 :].mean()


def test_stratification_puts_both_sources_in_the_head(tmp_path):
    """The defect-2 fix, end to end on disjoint per-source ranges.

    MUTATION: ranking on the clamped RAW score instead of the within-source percentile (i.e.
    `ckey = raw_key`) -> the head becomes 100% `code`; FAILS."""
    art = _build_fixture(tmp_path)
    _mod, _spec, stats, _order = _run(art, tmp_path)

    before = stats["head_before_stratification_chunks"]["easiest_5pct"]["composition"]
    after = stats["head_after_stratification_chunks"]["easiest_5pct"]["composition"]
    # VACUITY GUARD: the BEFORE view must actually show the defect, or the AFTER view proves
    # nothing. With disjoint ranges a global sort's 5% head is pure `code`.
    assert before[0]["source"] == "code"
    assert before[0]["share_of_head"] == 1.0, before

    names = {r["source"] for r in after}
    assert names == {"code", "prose"}, after
    assert stats["verdict"]["passed"] is True
    assert stats["verdict"]["max_over_representation"] <= 1.5


def test_the_clamp_is_applied_and_counted(tmp_path):
    """Defect 1, end to end. The fixture's 5 runaway values must be clamped and reported.

    MUTATION: `np.maximum` in the driver's clamp line -> records_modified and the after-max
    both go wrong; FAILS."""
    art = _build_fixture(tmp_path, tail=True)
    _mod, spec, stats, _order = _run(art, tmp_path)
    # VACUITY GUARD: the fixture's raw max must be the runaway value, not a plausible score.
    assert stats["mtld_before_clamp"]["max"] == pytest.approx(3.15e8, rel=1e-6)

    thr = spec["fix_defect_1_clamp"]["threshold"]
    assert 0 < thr < 1e4
    assert spec["fix_defect_1_clamp"]["records_modified"] >= 1
    assert stats["mtld_after_clamp"]["max"] == thr
    # The std must collapse while the robust std does not -- the defect-1 evidence.
    assert stats["clamp_effect"]["std_before"] > 100 * stats["clamp_effect"]["std_after"]


def test_a_file_sharded_stream_maps_each_part_to_its_own_token_space(tmp_path):
    """The F3 shape: two parts whose labelled-vs-held gaps DIFFER.

    A cursor shared across parts would accrue one gap per boundary and misattribute every
    post-boundary chunk while staying a perfect permutation.

    MUTATION: keying the cursor on `(source, domain, split)` instead of including the part ->
    the driver's `cursor != held` assertion fires; FAILS (loudly, which is the point)."""
    art = _build_fixture(tmp_path, parts=2)
    # VACUITY GUARD: the two parts' surpluses must differ, or one shared cursor would work.
    rs = [
        json.load(open(art / "receipts_all" / f))
        for f in sorted(os.listdir(art / "receipts_all"))
        if f.startswith("code--")
    ]
    surpluses = {r["pack"]["surplus_dropped"] for r in rs}
    assert len(surpluses) == 2, surpluses
    _mod, spec, _stats, order = _run(art, tmp_path)
    assert spec["permutation_check"]["valid"] is True
    bc = np.bincount(order, minlength=spec["block_count"])
    assert np.all(bc == 1)


def test_two_runs_produce_byte_identical_vectors(tmp_path):
    """Determinism, run to run. A re-run must reproduce the digest exactly.

    ⚠️ **This test alone CANNOT catch a non-stable sort, and saying otherwise was wrong.**
    MEASURED: `kind='quicksort'` left all 10 tests passing. numpy's introsort is a deterministic
    function of its input, so two runs of a quicksort build agree with each other perfectly —
    they simply agree on the WRONG order. Run-to-run equality is necessary and not sufficient;
    the sufficient property is asserted in
    `test_tied_keys_resolve_by_ascending_chunk_index` below.

    MUTATION: seeding any tie-break with `random`, a hash, or dict iteration order -> digests
    diverge; FAILS."""
    art = _build_fixture(tmp_path)
    _m1, spec1, _s1, order1 = _run(art, tmp_path)
    sha1 = spec1["order_file"]["sha256"]
    _m2, spec2, _s2, order2 = _run(art, tmp_path)
    assert spec2["order_file"]["sha256"] == sha1
    assert order1.tobytes() == order2.tobytes()


def test_tied_keys_resolve_by_ascending_chunk_index(tmp_path):
    """The REAL determinism property: within one key value, chunk indices must ASCEND.

    That is what makes the vector a pure function of the inputs rather than of numpy's pivot
    choices — and it is the property `kind="stable"` provides and `kind="quicksort"` does not.
    A two-run comparison cannot see the difference because introsort is itself deterministic.

    MUTATION: `kind='quicksort'` -> tied chunk indices come back out of order; FAILS.
    MEASURED at this fixture's size: quicksort reorders ties on a 504-element key with 50
    distinct values, so the fixture is large enough for the property to be observable."""
    art = _build_fixture(tmp_path)
    _mod, _spec, _stats, order = _run(art, tmp_path)
    key = np.fromfile(
        os.path.join(str(art / "curriculum" / "_scratch"), "chunk_key.u32"), dtype=np.uint32
    )
    # ⚠️ **The DRIVER's own key must supply the ties, or this test cannot see the driver's sort.**
    # An earlier version manufactured ties on a local copy and re-sorted it here — which tested
    # `np.argsort`, not `build_curriculum`, and MEASURED: a `kind="quicksort"` mutation in the
    # driver survived it. The token-weighted key ties much less often than the document-weighted
    # one (two documents collide only if the same number of their source's tokens are easier), so
    # the FIXTURE has to create the collisions: `_build_fixture` gives every document in a part an
    # identical stride, and its `tie_mass` block gives 2,000 of them an identical score — so their
    # token-ranks collide exactly.
    n_tied = int(key.size - np.unique(key).size)
    assert n_tied > 0, (
        "the driver's own chunk_key has no ties, so sort stability is unobservable here. Fix the "
        "FIXTURE (equal strides + a tie_mass score block), never by re-sorting a doctored copy — "
        "that tests numpy instead of the driver."
    )

    ko = key[order]
    same = ko[1:] == ko[:-1]
    assert int(same.sum()) > 0
    steps = np.diff(order.astype(np.int64))
    assert np.all(steps[same] > 0), (
        f"{int((steps[same] <= 0).sum())} tied pairs are not in ascending chunk-index order — "
        f"the sort is not stable, so the vector depends on numpy internals"
    )


def test_the_head_is_token_proportional_not_document_proportional(tmp_path):
    """The rank's denominator must be TOKENS, because the head is measured in chunks.

    This is the defect the first full run exposed: ranking by DOCUMENT percentile while verifying
    in CHUNKS gave `max_over_representation` 3.357 on the real corpus, because mean document
    length spans 31.6x across its sources. A fixture whose sources have EQUAL document lengths
    cannot see it — both denominators agree there — so this one gives `prose` documents 8x the
    length of `code` documents.

    MUTATION: `/ float(docs_src[si])` instead of `/ float(toks_src[si])` -> the long-document
    source is over-represented in the chunk head and this FAILS."""
    art = _build_fixture(tmp_path, doc_len_ratio=6)
    _mod, _spec, stats, _order = _run(art, tmp_path)

    # VACUITY GUARD: the two sources' mean document lengths must genuinely differ, or the
    # document- and token-weighted denominators coincide and the mutation is invisible.
    per = {r["source"]: r for r in stats["per_source"]}
    mean_len = {s: r["labelled_tokens"] / r["documents"] for s, r in per.items()}
    spread = max(mean_len.values()) / min(mean_len.values())
    # MEASURED at doc_len_ratio=6: spread 2.7x (code 3,932 tok/doc vs prose 10,485). That is well
    # short of the real corpus's 31.6x, and it is the most this fixture can express — pushing the
    # ratio higher shrinks prose to ~25 documents of ~41 chunks each, at which point ONE document
    # fills most of a 41-chunk head and the head is dominated by granularity rather than by the
    # ranking. So the bar is 2x here, and the mutation is still caught: the document-denominator
    # mutation over-represents prose even at this spread.
    assert spread > 2, mean_len

    for key in ("easiest_1pct", "easiest_5pct"):
        v = stats["head_after_stratification_chunks"][key]
        assert v["max_over_representation"] <= 1.5, (key, v["composition"])
    assert stats["verdict"]["passed"] is True


def test_a_shard_whose_part_supplied_no_labels_is_refused(tmp_path):
    """An unlabelled part's chunks would have no difficulty; ordering them means inventing one.

    MUTATION: `continue`ing past a missing part instead of raising -> the run succeeds and emits
    a shorter vector, which is a valid permutation of the WRONG axis; FAILS."""
    art = _build_fixture(tmp_path)
    os.remove(art / "labels_raw" / "prose--train.labels")
    mod = _load_driver()
    mod.ART, mod.INDEX = str(art), str(art / "index" / "INDEX-FINAL.json")
    mod.RECEIPTS, mod.LABELS = str(art / "receipts_all"), str(art / "labels_raw")
    mod.OUT, mod.SCRATCH = str(art / "curriculum"), str(art / "curriculum" / "_scratch")
    with pytest.raises(SystemExit, match="supplied NO labels"):
        mod.main()


def test_labels_shorter_than_the_shards_they_describe_are_refused(tmp_path):
    """`labelled >= held`, PER PART. Labels that do not cover the tokens cannot rank them.

    MUTATION: dropping the check -> chunks past the last document get owner index n and the
    searchsorted guard fires later with a less useful message; without both, the run would
    attribute them to the final document. FAILS on the assertion below."""
    art = _build_fixture(tmp_path)
    # Rewrite prose's labels with far too few tokens.
    _write_labels(
        art / "labels_raw" / "prose--train.labels",
        "prose--train",
        "prose",
        "train",
        0,
        [10] * 3000,
        list(np.linspace(150, 260, 3000)),
    )
    mod = _load_driver()
    mod.ART, mod.INDEX = str(art), str(art / "index" / "INDEX-FINAL.json")
    mod.RECEIPTS, mod.LABELS = str(art / "receipts_all"), str(art / "labels_raw")
    mod.OUT, mod.SCRATCH = str(art / "curriculum"), str(art / "curriculum" / "_scratch")
    with pytest.raises(SystemExit, match="shards hold"):
        mod.main()


def test_the_spec_declares_block_count_and_max_order_bytes(tmp_path):
    """Both are MANDATORY: without block_count the permutation check is vacuous, and without
    max_order_bytes the real 1.68 GB vector is refused unread by check_order_domain.

    MUTATION: omitting either key from the spec dict -> FAILS."""
    art = _build_fixture(tmp_path)
    _mod, spec, _stats, order = _run(art, tmp_path)
    assert spec["block_count"] == order.size
    assert spec["max_order_bytes"] == order.size * 4
    assert spec["coordinate_model"] == "parent_pool_flat_chunks_v1"
    assert spec["chunk"]["seq_len"] == 2048
    assert spec["chunk"]["rule"] == "minus_one"
    # The alternative rule must be reported, since it shifts every index past the first shard.
    assert spec["chunk"]["alternative_rule"]["total_chunks"] > spec["block_count"]
    assert spec["not_published"]["gate_a_run"] is False
    assert spec["parent"]["axis_sha256"]


def test_the_drivers_streaming_stratification_agrees_with_the_library_function():
    """The driver reimplements ``stratify_within_source`` as a histogram walk for memory reasons.
    Nothing else asserts the two agree, so their green test suites are otherwise evidence about
    two different implementations of one idea.

    This pins the equivalence: same inputs, same ORDER out. The values differ (the driver's key
    is a uint32-scaled percentile, the library's is a float64 percentile), so the comparison is
    on the induced ordering, which is the only thing either is used for.

    MUTATION: dividing by the global count in either implementation -> the orderings diverge and
    this FAILS. MUTATION: `side='left'` vs the cursor walk -> ties land differently; FAILS."""
    mod = _load_driver()
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from edullm_data.corpus_curriculum import stratify_within_source

    rng = np.random.default_rng(3)
    n, k = 20_000, 7
    # ⚠️ Scores on a 0.05 grid, not 0.01, and the reason is a MEASURED precision limit rather
    # than convenience. The driver bins at width 0.01 via `floor(score * 100)`, and in binary
    # floating point `floor(1.13 * 100) == 112`, the same bin as 1.12 — MEASURED: 252 of 13,206
    # distinct scores on a 0.01 grid collide into a shared bin. Inside one bin the driver orders
    # by DOCUMENT index while the library orders by VALUE, so on colliding pairs the two
    # genuinely disagree and no equivalence holds. That is a real (tiny) resolution limit of the
    # histogram route, documented here rather than hidden by a looser assertion: the ordering of
    # two documents whose MTLD differs by <0.01 is not preserved. A 0.05 grid is coarser than the
    # bin, so every distinct score gets its own bin and the equivalence is exact.
    scores = np.round(rng.uniform(1.0, 400.0, n) * 20) / 20
    scores[5000:9000] = 88.85  # a real tie block, or stability is unexercised
    sid = rng.integers(0, k, n)
    # VACUITY GUARD: ties present, every source non-empty, and — the load-bearing one — no two
    # distinct scores may share a bin, or the equivalence being asserted does not hold.
    assert np.unique(scores).size < n
    assert np.bincount(sid, minlength=k).min() > 0
    _u = np.unique(scores)
    assert np.unique(np.floor(_u * 100).astype(np.int64)).size == _u.size, (
        "fixture scores collide into shared 0.01 bins — the two implementations are not "
        "equivalent there, so this test would be asserting something false"
    )

    lib = stratify_within_source(scores, sid, n_sources=k)

    # --- the driver's histogram route, on the same inputs ---
    NB, INV = mod.NBINS, mod.INV
    b = np.clip(np.floor(scores * INV).astype(np.int64), 0, NB - 1)
    hist = np.zeros((k, NB), dtype=np.int64)
    for i in range(k):
        m = sid == i
        hist[i] += np.bincount(b[m], minlength=NB)
    cum = np.zeros((k, NB), dtype=np.int64)
    np.cumsum(hist[:, :-1], axis=1, out=cum[:, 1:])
    sizes = np.bincount(sid, minlength=k)
    drv = np.empty(n, dtype=np.float64)
    for i in range(k):
        m = np.flatnonzero(sid == i)
        o, off = mod.within_bin_offsets(b[m])
        rank = np.empty(m.size, dtype=np.int64)
        rank[o] = cum[i, b[m][o]] + off
        drv[m] = rank / float(sizes[i])

    # Same induced order, and the same per-source rank set.
    np.testing.assert_array_equal(
        np.lexsort((np.arange(n), lib)), np.lexsort((np.arange(n), drv))
    )
    for i in range(k):
        m = sid == i
        np.testing.assert_allclose(np.sort(lib[m]), np.sort(drv[m]), atol=1e-12)


def test_a_chunk_starting_exactly_on_a_document_boundary_is_owned_by_that_document():
    """``searchsorted(ends, offset, side="right")`` — and ``side="left"`` is an invisible off-by-one.

    A chunk whose first token is exactly a document's first token belongs to THAT document, not to
    the one that ended there. Both answers are real documents with real scores, so the error is
    undetectable downstream: the permutation stays bijective and every chunk still has a rank.
    ``corpus_order._owner_of_each_chunk`` puts the frequency at ~1 chunk per document boundary.

    MEASURED: a `side="left"` mutation in the driver left all 13 other tests passing, so this is
    tested directly on the arithmetic rather than through the end-to-end fixture.

    MUTATION: `side="left"` -> the boundary chunk is attributed to the PREVIOUS document; FAILS."""
    # Three documents of 10 tokens each: token spans [0,10), [10,20), [20,30).
    strides = np.array([10, 10, 10], dtype=np.int64)
    ends = np.cumsum(strides)  # [10, 20, 30]
    starts = np.array([0, 9, 10, 19, 20, 29], dtype=np.int64)
    # VACUITY GUARD: the probe must include offsets EXACTLY on a boundary (10 and 20), or the two
    # `side=` spellings agree and the assertion is decoration.
    assert 10 in starts.tolist() and 20 in starts.tolist()

    right = np.searchsorted(ends, starts, side="right")
    left = np.searchsorted(ends, starts, side="left")
    # The correct answer: offset 10 is document 1's first token, so document 1 owns it.
    np.testing.assert_array_equal(right, [0, 0, 1, 1, 2, 2])
    # `side="left"` attributes the boundary chunk to the document that ENDED there.
    np.testing.assert_array_equal(left, [0, 0, 0, 1, 1, 2])
    assert not np.array_equal(right, left), "fixture cannot distinguish the two spellings"

    # And the driver must use the right one. Read it out of the source so the test tracks the code.
    src = open(DRIVER).read()
    assert 'np.searchsorted(ends, st, side="right")' in src, (
        "the driver's owner lookup is not side='right' — a chunk starting on a document boundary "
        "would be attributed to the previous document, invisibly"
    )


def test_within_bin_offsets_gives_consecutive_ranks_to_a_tie_block():
    """The helper that stops a clamped tail collapsing to one shared percentile.

    MUTATION: returning `np.zeros_like` for the offsets -> the whole tie block shares a rank and
    the assertion on distinctness FAILS."""
    mod = _load_driver()
    bins = np.array([5, 5, 5, 1, 1, 9], dtype=np.int64)
    # VACUITY GUARD: there must be a repeated bin, or offsets are trivially all zero.
    assert np.unique(bins).size < bins.size
    order, off = mod.within_bin_offsets(bins)
    # Within each bin the offsets must be 0..k-1 exactly once.
    bs = bins[order]
    for b in np.unique(bs):
        got = np.sort(off[bs == b])
        assert np.array_equal(got, np.arange(got.size)), (b, got)
