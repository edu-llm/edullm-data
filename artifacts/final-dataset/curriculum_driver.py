"""Build and publish `curriculum/edu-mix-983b` — the MTLD order vector over the published parent.

Runs AFTER `pretrain/edu-mix-983b` is promoted, and it cannot run before: the curriculum's
`depends_on` pins the parent by `manifest_sha256`, which does not exist until the parent's manifest
is written. That ordering is not a convenience — see REQUIRED-AND-UNFILLED below.

    python3 curriculum_driver.py --plan          # arithmetic only; no S3, no bytes
    python3 curriculum_driver.py --build          # read labels, build the vector, write it locally
    python3 curriculum_driver.py --go             # publish to LANDING

NOTHING HERE CAN WRITE `s3://edullm-data`. `publish()` stages into `edullm-landing`; crossing the
airlock is `promote()`, which only the validator role can do, in a separate job definition.

⚠️ REQUIRED-AND-UNFILLED: `PARENT_MANIFEST_SHA256` IS `None` AND THIS DRIVER REFUSES TO RUN
-------------------------------------------------------------------------------------------
`token_order_v1` requires `depends_on` with at least one entry (`REQUIRED_FIELDS`), and §7 pins a
derived dataset's parent by `manifest_sha256`. **The parent has not been promoted, so that digest
does not exist yet.** It is left `None` deliberately and :func:`assert_invariants` REFUSES rather
than defaulting, because every alternative ships something wrong:

* a placeholder string would publish a curriculum pinned to a parent that does not exist, and the
  pin is inside the curriculum's own `manifest_sha256` — so correcting it is a `v2`, not an edit;
* omitting `depends_on` makes `_block_count` return `None`, and `check_order_domain` then takes
  `n = order.size` and checks `bincount == 1` against the vector's OWN length. **That passes for any
  permutation of any length.** The check does not fail; it becomes vacuous. A 1.9 GB vector over the
  wrong parent would be accepted;
* inheriting a family default is impossible — `families/curriculum.json` has no parent to name.

So: fill it from the promoted parent's `dataset.json`, and until then this driver stops. **Fails
closed, and the failure is the point.**

THREE PROPERTIES ENFORCED RATHER THAN DOCUMENTED
------------------------------------------------
1. **`max_order_bytes` MUST be declared.** `token_order_v1._DEFAULT_MAX_ORDER_BYTES` is 512 MiB and
   the train vector is 1,914,301,740 B = **3.57x** that. Undeclared, `check_order_domain` returns
   `order-too-large` and never loads the vector — Gate A fails on the artifact it exists to check.
2. **`train` and `val` CANNOT share a group.** `check_order_domain` reads ONE group-level `ordering`
   and ONE `block_count` and applies both to EVERY manifest entry, so a train permutation of
   478,575,435 indices and a val identity of 51 in one group means the val object trips
   `permutation-wrong-length` with certainty. Two groups, each with its own `block_count`.
3. **The parent axis is read from the parent's MANIFEST, not from the plan.** The chunk numbering IS
   the order `read.dataset_paths` hands a trainer, which is manifest-entry order. Plan and manifest
   agree today; a republish or an excluded shard makes them disagree, and the resulting permutation
   stays bijective while pointing every chunk at the wrong tokens.
"""

from __future__ import annotations

import argparse
import json
import sys

#: 🔴 **MOVED TWICE — was `29968a2b04008a8c`, then `364cb4dd488a5761`.**
#: (1) the 401 fix: the two Nemotron-CC-Math rows repointed from the GATED HF repo to the staged
#:     `s3://edullm-landing/_src/` copy (7 of 16 bundle failures, 61 B tokens of the math pillar).
#: (2) the stackv2-edu pool/target correction, owner-ruled 2026-08-09 (ship 936 B): pool 707 B ->
#:     61,642,058,302 and target 108 B -> 58 B, the latter having been 3.74x the ENTIRE source.
#:     185 bundles unchanged, 37,215 train / 92 val shards, 932,749,017,088 tokens.
#: Both `repo` and `target_tokens` are inside the plan document, of which `plan_id` is the content
#: address, so each move was required.
#:
#: ⚠️ This constant is a PREFIX. A stale value here reads `_ingest/final-dataset/<old>/` — an empty
#: or half-built prefix — and the driver would report "no labels" / "no receipts" rather than
#: anything that looks like a wrong-plan error. Verify against
#: `tests/test_curriculum_labels.FROZEN_PLAN_ID`, which is asserted against the checked-in registry.
PLAN_ID = "79e53d1e5e131649"
BUCKET = "edullm-landing"
PREFIX = "_ingest/final-dataset"
PARENT_DATASET_ID = "pretrain/edu-mix-983b"
DATASET_ID = "curriculum/edu-mix-983b"
PROFILE = "token-order/v1"
GROUP = "mtld"

#: The promoted parent's version and manifest digest. **BOTH REQUIRED, BOTH UNFILLED.** See the
#: module docstring: a placeholder here would publish a pin to a parent that does not exist, inside
#: an immutable `manifest_sha256`.
PARENT_VERSION: str | None = None
PARENT_MANIFEST_SHA256: str | None = None

#: 4 GiB. The real vector is 1.92 GB, so this is ~2.1x headroom over the actual size and 8x the
#: profile's default. Declared on BOTH groups — the cap is read per group (`_cfg`), so declaring it
#: on one leaves the other at 512 MiB.
MAX_ORDER_BYTES = 4 * 1024 * 1024 * 1024

#: Cross-checks from the plan. The driver refuses if the parent's manifest disagrees.
PLAN_TRAIN_SHARDS = 39_205
PLAN_VAL_SHARDS = 102
PLAN_SHARD_TOKENS = 25_001_984


def assert_invariants() -> None:
    """Fail before any network call on every silent-failure mode identified for this artifact."""
    from edullm_data.profiles.registry import get_profile

    get_profile(PROFILE)  # raises ProfileError listing valid names on a typo

    from edullm_data.profiles import token_order_v1

    # Property 1, checked against the profile's own constant rather than a remembered number.
    train_bytes = PLAN_TRAIN_SHARDS * 12_207 * 4
    if train_bytes <= token_order_v1._DEFAULT_MAX_ORDER_BYTES:
        raise SystemExit(
            f"the train vector is {train_bytes:,} B, at or under the profile default "
            f"({token_order_v1._DEFAULT_MAX_ORDER_BYTES:,}). This driver's max_order_bytes "
            f"reasoning assumed it exceeded the cap; re-derive it before publishing."
        )
    if MAX_ORDER_BYTES <= train_bytes:
        raise SystemExit(
            f"REFUSING: max_order_bytes={MAX_ORDER_BYTES:,} does not exceed the train vector's "
            f"{train_bytes:,} B. check_order_domain would return `order-too-large` and never load "
            f"the vector — Gate A would fail on the artifact it exists to check."
        )

    if PARENT_MANIFEST_SHA256 is None or PARENT_VERSION is None:
        raise SystemExit(
            "REFUSING: PARENT_VERSION / PARENT_MANIFEST_SHA256 are unfilled.\n"
            "\n"
            f"  The parent {PARENT_DATASET_ID} has NOT been promoted, so its manifest digest does\n"
            "  not exist. `token_order_v1` requires depends_on, and §7 pins a derived dataset's\n"
            "  parent by manifest_sha256 — which lands inside THIS dataset's own manifest_sha256,\n"
            "  so a wrong pin costs a v2 and cannot be edited.\n"
            "\n"
            "  Do NOT invent a placeholder. Omitting depends_on is worse: _block_count() returns\n"
            "  None, check_order_domain then takes n = order.size, and `bincount == 1` against the\n"
            "  vector's OWN length passes for ANY permutation of ANY length. The check becomes\n"
            "  VACUOUS rather than failing.\n"
            "\n"
            "  Fill both from the promoted parent's dataset.json:\n"
            f"    aws s3 cp s3://edullm-data/{PARENT_DATASET_ID}/<vN>/dataset.json - \\\n"
            "      | python3 -c 'import json,sys; d=json.load(sys.stdin); "
            "print(d[\"version\"], [g[\"manifest_sha256\"] for g in d[\"groups\"]])'"
        )


def parent_manifest(s3) -> dict:
    """The parent's group manifest, from the PUBLISHED bucket. Property 3.

    Read from `edullm-data` and not from landing: the axis must be the order a trainer sees, and a
    trainer reads the promoted dataset. A landing copy is a staging artifact that promotion may have
    reordered or partially copied.
    """
    key = f"{PARENT_DATASET_ID}/{PARENT_VERSION}/tokens/manifest.json"
    return json.loads(s3.get("edullm-data", key))


def build_axis(manifest: dict, *, split: str = "train"):
    """The chunk axis for one split, DERIVED from the parent's manifest. **Never a constant.**

    🔴 **F2 — THE WORST DEFECT IN THIS FILE, AND IT WAS ALREADY WRONG AGAINST THE LIVE BUILD.**
    This function was called and its `axis.n_chunks` was used **only in the print below**, while the
    numbers that reached `group_meta_for` and `identity_order` came from a hard-coded
    `PLAN_*_SHARDS x 12,207`. That product assumes **every shard is exactly full**, and shards are
    not: a stream's last shard is short whenever its realized stream underruns its final ref
    (`corpus_pack.py:840-853`), and an `unfilled` ref is not written at all — a whole 12,207-chunk
    hole.

    **MEASURED against the live build at 88% done, ONE source (`stackv2-edu`):** 6 short train shards
    = 38,736 chunks short, and **7 short val shards = 29,880 chunks = 2.40% error on val already.**
    With 132 train streams each taking one short tail shard, the train count over-declares by up to
    ~1.6 M chunks.

    **The two outcomes were asymmetric and the smaller one was the dangerous one.** The train pair
    would have failed Gate A loudly (`permutation-wrong-length`, a 6-7 h publish thrown away, and
    recoverable). The **val pair was internally consistent** — `identity_order(n_val)` and the
    declared `block_count` came from the same wrong constant — so **it would have PASSED Gate A** and
    shipped a vector indexing 29,880+ chunks the parent does not have. A gate that catches the big
    error and waves the small one through is the worst available outcome, and it is the argument for
    deriving over asserting stated by the artifact itself.

    The constant product is retained as a printed UPPER BOUND with an explicit disagreement warning,
    because the disagreement is a real signal (a short tail shard is normal) and silence about it
    would be a second way to lose the same information.
    """
    from edullm_data.corpus_order import chunk_axis_from_manifest, chunk_counts

    axis = chunk_axis_from_manifest(manifest, split=split)
    want_shards = PLAN_TRAIN_SHARDS if split == "train" else PLAN_VAL_SHARDS
    if len(axis.keys) != want_shards:
        raise SystemExit(
            f"REFUSING: the parent manifest holds {len(axis.keys):,} {split} shards, the plan says "
            f"{want_shards:,}. A missing shard shifts every chunk index past it, and the "
            f"resulting permutation is still bijective."
        )
    per_shard = chunk_counts([PLAN_SHARD_TOKENS])[0]
    upper = want_shards * per_shard
    full = [t for t in axis.tokens if t == PLAN_SHARD_TOKENS]
    short = [(k, t) for k, t in zip(axis.keys, axis.tokens) if t != PLAN_SHARD_TOKENS]
    print(f"axis[{split}]: {len(axis.keys):,} shards, {len(full):,} at exactly "
          f"{PLAN_SHARD_TOKENS:,} tokens, **{axis.n_chunks:,} chunks DERIVED** "
          f"({axis.n_chunks * 4:,} B as uint32), rule={axis.rule}")
    if axis.n_chunks != upper:
        # NOT a failure. A short tail shard is the normal shape; what was a failure is declaring the
        # upper bound as if it were the count.
        print(f"  ⚠️ the constant product {want_shards:,} x {per_shard:,} = {upper:,} is "
              f"{upper - axis.n_chunks:+,} chunks ({(upper - axis.n_chunks) / upper:+.4%}) from the "
              f"derived count, across {len(short):,} short shard(s). THE DERIVED VALUE IS USED. "
              f"(F2: the constant was previously what got declared, and it is what made the val "
              f"pair internally consistent and therefore able to pass Gate A while wrong.)")
        for k, t in short[:5]:
            print(f"     short: {k} {t:,} tokens ({per_shard - max(0, (t - 1) // 2048):,} chunks short)")
    else:
        print(f"  the constant product {upper:,} agrees exactly — every shard is full.")
    return axis


def load_streams(s3, *, split: str = "train"):
    """Every bundle's labels, as ONE ``StreamLabels`` PER PART. **Never concatenated across parts.**

    🔴 **F3 — THIS FUNCTION USED TO CONCATENATE THE K PARTS AND THAT WAS THE DEFECT.** Under
    file-sharding a stream is K bundles (stackv2-edu 7, finepdfs-edu 4, nemotron-cc-math-3 3,
    -4plus 2), and **each is its own `pack()` call with its own tail truncation and surplus drop** —
    so each part's `tokens_in` exceeds its shards' `tokens_out` by a DIFFERENT amount. Concatenating
    them into one token space while `build_order` advanced its cursor by `tokens_out` made the gap
    accrue once per boundary, and **every chunk after the first boundary was attributed to the wrong
    document** — MEASURED, with `build_order` returning a perfect permutation and raising nothing.
    23.5% of this corpus (232 B of 986 B tokens) is in a file-sharded stream.

    Each part now carries its own `part` index, so `build_order` gives it a cursor starting at 0 in
    its own labelled space. The ordering-of-parts concern the previous docstring was about does not
    disappear, it becomes **unnecessary**: nothing is concatenated, so nothing can be concatenated in
    the wrong order. The completeness check is kept — a MISSING part still means unlabelled chunks.
    """
    import numpy as np

    from edullm_data.corpus_labels import decode_labels
    from edullm_data.corpus_order import StreamLabels

    parts: dict[tuple, list[tuple[int, object]]] = {}
    token = None
    lprefix = f"{PREFIX}/{PLAN_ID}/_labels/"
    while True:
        kw = {"Bucket": BUCKET, "Prefix": lprefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.client.list_objects_v2(**kw)
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".labels"):
                continue
            ls = decode_labels(s3.get(BUCKET, obj["Key"]))
            if ls.plan_id != PLAN_ID:
                raise SystemExit(
                    f"REFUSING: {obj['Key']} declares plan_id {ls.plan_id!r}, not {PLAN_ID!r}. "
                    f"Labels from another plan describe a different document stream."
                )
            if ls.split != split:
                continue
            # The part index is the `--pNNofNN` suffix of the bundle id (`_bundle_id`); its absence
            # means an unsharded stream, i.e. part 0 of 1.
            idx = 0
            if "--p" in ls.bundle_id:
                idx = int(ls.bundle_id.rsplit("--p", 1)[1].split("of")[0])
            parts.setdefault((ls.source, ls.domain, ls.split), []).append((idx, ls))
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")

    streams = []
    for key, members in sorted(parts.items(), key=lambda kv: (kv[0][0], kv[0][1] or "", kv[0][2])):
        members.sort(key=lambda t: t[0])
        seen = [i for i, _ in members]
        if seen != list(range(len(seen))):
            raise SystemExit(
                f"REFUSING: stream {key} has file_shard indices {seen}, not "
                f"{list(range(len(seen)))}. A missing part means those documents were never "
                f"labelled and their chunks have no difficulty; a repeated one means two parts "
                f"claim the same slice."
            )
        # ONE StreamLabels PER PART. No np.concatenate across members — that WAS the F3 bug.
        for idx, ls in members:
            streams.append(StreamLabels(
                source=key[0], domain=key[1], split=key[2],
                strides=np.add(ls.records["n_tokens"].astype(np.int64), 1),
                mtld=ls.records["mtld"].astype(np.float64),
                part=idx,
            ))
    if not streams:
        raise SystemExit(
            f"REFUSING: no {split!r} labels under {lprefix}. The build ran without --labels, or has "
            f"not finished. Publishing an order vector over unlabelled tokens is not possible and "
            f"inventing difficulties for them would be worse."
        )
    return streams


def load_receipts(s3):
    """Every receipt for this plan. **The only correct source of the shard -> part mapping.**

    🔴 A shard KEY cannot name which of a stream's K children wrote it — MEASURED:
    `labels_from_path("tokens/stackv2-edu/train-35260.u32le.bin")` yields `{'source': 'stackv2-edu'}`.
    All 7 children write into `tokens/stackv2-edu/` with globally-allocated ordinals, because parts
    deliberately share the `source` path segment (that is the whole difference from
    `split_source_rows`). A receipt names both: its `bundle_id` carries `--pNNofNN` and its `shards`
    list names exactly the keys that child wrote.

    **The previous code built the mapping with `labels_from_path` and hard-coded `"train"` as the
    split** — so every shard resolved to part 0 of its stream, which is precisely the collapse F3 is
    about, and every val shard would have been mislabelled as train had the val axis existed.
    """
    from edullm_data.corpus_receipt import read_receipt

    out, token = [], None
    rprefix = f"{PREFIX}/{PLAN_ID}/_receipts/"
    while True:
        kw = {"Bucket": BUCKET, "Prefix": rprefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.client.list_objects_v2(**kw)
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".json"):
                out.append(read_receipt(s3, BUCKET, obj["Key"]))
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    if not out:
        raise SystemExit(
            f"REFUSING: no receipts under {rprefix}. Without them a shard cannot be attributed to "
            f"the file-shard PART that wrote it, and `labels_from_path` structurally cannot supply "
            f"it — every shard would collapse onto part 0 and 23.5% of the corpus would be "
            f"mis-ranked, silently (F3)."
        )
    return out


def group_meta_for(n_train: int, n_val: int) -> dict:
    """The two groups' metadata. Property 2 — `train` and `val` cannot share one.

    `depends_on` carries the parent's `block_count` per group, because that is what
    `check_order_domain` compares `order.size` against. Both entries pin the SAME parent by the same
    `manifest_sha256`; only the block count differs, because the two vectors index different
    partitions of it.
    """
    dep = {
        "dataset_id": PARENT_DATASET_ID,
        "version": PARENT_VERSION,
        "manifest_sha256": PARENT_MANIFEST_SHA256,
    }
    common = {
        "profile": PROFILE,
        "ordering": "permutation",
        "sort": "ascending",
        "easy_to_hard": True,
        "metric": "mtld",
        "max_order_bytes": MAX_ORDER_BYTES,
    }
    from edullm_data.corpus_mtld import MTLD_SPEC_ID
    from edullm_data.corpus_order import COORDINATE_MODEL, SEQ_LEN_CHUNK

    common |= {
        "coordinate_model": COORDINATE_MODEL,
        "sequence_length": SEQ_LEN_CHUNK,
        "mtld_spec": MTLD_SPEC_ID,
    }
    return {
        f"{GROUP}-train": common | {
            "block_count": n_train,
            "depends_on": [dep | {"block_count": n_train, "split": "train"}],
        },
        f"{GROUP}-val": common | {
            "block_count": n_val,
            # The val vector is the IDENTITY, not a curriculum. Held-out data is not reordered —
            # two checkpoints' validation losses are incomparable if the val order moves — but the
            # identity is a real permutation, so it satisfies check_order_domain honestly.
            "ordering": "permutation",
            "easy_to_hard": False,
            "note": "identity — held-out data is deliberately NOT curriculum-ordered",
            "depends_on": [dep | {"block_count": n_val, "split": "val"}],
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true", help="arithmetic only; no S3, no bytes")
    g.add_argument("--build", action="store_true", help="build the vectors, write them locally")
    g.add_argument("--go", action="store_true", help="publish to LANDING")
    ap.add_argument("--out", default="/tmp/curriculum-edu-mix-983b")
    args = ap.parse_args()

    from edullm_data.corpus_order import SEQ_LEN_CHUNK, chunk_counts

    per_shard = chunk_counts([PLAN_SHARD_TOKENS])[0]
    # ⚠️ UPPER BOUNDS, printed only. These are NOT what gets declared — see build_axis' F2 note. The
    # products assume every shard is exactly full, and MEASURED against the live build one source
    # alone put val 2.40% out. `n_train`/`n_val` are DERIVED from the manifest below.
    upper_train = PLAN_TRAIN_SHARDS * per_shard
    upper_val = PLAN_VAL_SHARDS * per_shard
    print(f"chunk rule      : (tokens - 1) // {SEQ_LEN_CHUNK} = {per_shard:,} per full shard")
    print(f"                  ⚠️ OLMo-core's own numpy_dataset.py:679 gives "
          f"{PLAN_SHARD_TOKENS // SEQ_LEN_CHUNK:,} — see the F-C6 finding")
    print(f"train chunks    : <= {PLAN_TRAIN_SHARDS:,} x {per_shard:,} = {upper_train:,} "
          f"({upper_train * 4:,} B = {upper_train * 4 / 1e9:.2f} GB)  ⚠️ UPPER BOUND, not the count")
    print(f"val chunks      : <= {PLAN_VAL_SHARDS:,} x {per_shard:,} = {upper_val:,}"
          f"  ⚠️ UPPER BOUND, not the count")
    print(f"max_order_bytes : {MAX_ORDER_BYTES:,} (profile default is 536,870,912)")

    if args.plan:
        print("\nPLAN ONLY — no S3 call made. Both figures above are UPPER BOUNDS from the constant "
              "product; the real counts are DERIVED from the parent's manifest and are only "
              "available with S3 (--build). The publish path additionally requires PARENT_VERSION "
              "and PARENT_MANIFEST_SHA256, which are UNFILLED by design.")
        return 0

    assert_invariants()
    print(f"INVARIANTS_OK profile={PROFILE} parent={PARENT_DATASET_ID}/{PARENT_VERSION}")

    from edullm_data.corpus_order import (
        build_order,
        identity_order,
        shard_stream_from_receipts,
    )
    from edullm_data.s3 import Boto3S3

    s3 = Boto3S3.default()
    manifest = parent_manifest(s3)

    # F2. BOTH axes derived from the manifest, and `n_val` from the VAL axis rather than a constant.
    # The val pair was the dangerous half: `identity_order(constant)` and a declared `block_count`
    # from the same constant are internally consistent, so Gate A passed them while the vector
    # indexed chunks the parent does not have.
    axis = build_axis(manifest, split="train")
    val_axis = build_axis(manifest, split="val")
    n_train = axis.n_chunks
    n_val = val_axis.n_chunks
    print(f"DERIVED n_train={n_train:,} n_val={n_val:,} "
          f"(constant products were {upper_train:,} / {upper_val:,})")

    # F3. The shard -> PART mapping comes from RECEIPTS. `labels_from_path` cannot name the part —
    # every shard would collapse onto part 0 of its stream and 23.5% of the corpus would be
    # mis-ranked while the vector stayed a perfect permutation.
    receipts = load_receipts(s3)
    mapping = shard_stream_from_receipts(receipts)
    print(f"receipts: {len(receipts):,}, mapping {len(mapping):,} shard keys to parts")
    missing = [k for k in axis.keys if k not in mapping]
    if missing:
        raise SystemExit(
            f"REFUSING: {len(missing):,} train shards in the parent manifest are named by NO "
            f"receipt (e.g. {missing[:3]}). Their chunks cannot be attributed to a part, and "
            f"guessing part 0 is the F3 collapse."
        )

    streams = load_streams(s3, split="train")
    val_streams = load_streams(s3, split="val")
    print(f"labels: {len(streams)} train parts / {len(val_streams)} val parts, "
          f"{sum(s.documents for s in streams):,} train documents, "
          f"{sum(s.n_tokens for s in streams):,} labelled train tokens")

    order = build_order(axis, streams, shard_stream=mapping, metric="mtld")
    ident = identity_order(n_val)
    if order.size != n_train:
        raise SystemExit(
            f"REFUSING: build_order returned {order.size:,} indices against the derived axis's "
            f"{n_train:,}. These cannot disagree; if they do, the declared block_count would be "
            f"right for one of them and wrong for the other."
        )
    print(f"ORDER_OK train {order.size:,} indices, val {ident.size:,} indices")

    import os

    os.makedirs(f"{args.out}/{GROUP}-train", exist_ok=True)
    os.makedirs(f"{args.out}/{GROUP}-val", exist_ok=True)
    with open(f"{args.out}/{GROUP}-train/train-00000.u32le.bin", "wb") as fh:
        fh.write(order.tobytes())
    with open(f"{args.out}/{GROUP}-val/val-00000.u32le.bin", "wb") as fh:
        fh.write(ident.tobytes())
    print(f"wrote {args.out}/")

    if args.build:
        print("\nBUILD ONLY — vectors written locally, nothing published.")
        return 0

    # ⚠️ `source` must be the s3:// form for a shard corpus (publish() DELETES a local source tree
    # after copying, publish.py:1053). Here the source IS local and small (1.92 GB, two objects), so
    # deletion is acceptable — but it is stated rather than assumed, because the parent driver's
    # property 1 exists for the opposite reason and the two must not be confused.
    import datetime

    from edullm_data.publish import publish

    plan = publish(
        args.out,
        dataset_id=DATASET_ID,
        purpose=(
            "MTLD easy-to-hard curriculum ordering over pretrain/edu-mix-983b: a uint32 index "
            "vector reordering the parent's 2048-token training chunks by ascending bidirectional "
            "MTLD, lowest (least lexically diverse, easiest) first"
        ),
        # F12. The GROUPS are `mtld-train` and `mtld-val` — `publish.build_plan` groups by FIRST PATH
        # SEGMENT (`publish.py:346-354`) and this driver stages `{GROUP}-train/` and `{GROUP}-val/`.
        # `{GROUP: PROFILE}` keyed the bare `mtld`, which no group is called, and `profile_for`
        # (`publish.py:400-408`) raises `PublishError` on a group with no mapping entry. Reproduced:
        # "group 'mtld-train' has no profile in the profile mapping {'mtld': 'token-order/v1'}".
        # `group_meta_for` already used the suffixed names — so the author knew, and this was the one
        # line that also needed them. It raises before any byte moves, which is why it is cheap; but
        # it is also proof that `--go` had never been run.
        profile={f"{GROUP}-train": PROFILE, f"{GROUP}-val": PROFILE},
        s3=s3,
        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        group_meta=group_meta_for(n_train, n_val),
        about=(
            f"Bidirectional MTLD (McCarthy & Jarvis 2010, TTR factor threshold 0.72) computed "
            f"per document AT TOKENIZE TIME from the text in memory — this corpus persists no "
            f"text, so the score could not be recovered afterwards. Chunk ownership is by FIRST "
            f"token; ties break on global chunk index, so the vector is a pure function of the "
            f"parent and the labels with no seed. Coordinate model "
            f"parent_pool_flat_chunks_v1 at sequence length {SEQ_LEN_CHUNK}."
        ),
        notes=(
            "The val object is the IDENTITY, not a curriculum: held-out data is deliberately not "
            "reordered, because two checkpoints' validation losses are incomparable if the val "
            "order moves. It is nonetheless a real permutation and satisfies the profile's "
            "bijection check honestly rather than by exemption."
        ),
        limitations=[
            {
                "kind": "metric-coverage",
                "detail": (
                    "MTLD's word regex is ASCII-only, so a non-English document scores on its "
                    "ASCII content alone and a purely non-ASCII one yields zero words and the "
                    "minimum score — ranking as maximally easy. The regex is fixed by "
                    "cross-corpus comparability and is not adjusted for this."
                ),
            },
            {
                "kind": "chunk-arithmetic",
                "detail": (
                    "Chunk counts use (shard_tokens - 1) // 2048 = 12,207 per full shard, per the "
                    "curriculum handoff. OLMo-core's NumpyFSLDataset computes "
                    "file_size // (item_size * sequence_length) = 12,208. A trainer using the "
                    "second convention must rebuild this vector: the two disagree on every chunk "
                    "index past the first shard."
                ),
            },
            {
                "kind": "difficulty-granularity",
                "detail": (
                    "A chunk is owned by the document holding its FIRST token, so a chunk spanning "
                    "several documents is ranked by the first alone. At a mean document length of "
                    "~815 tokens against a 2,048-token chunk most chunks span more than one "
                    "document, so the ordering is a document-level signal applied at chunk "
                    "granularity, not a chunk-level measurement."
                ),
            },
        ],
        hash_workers=8,
        copy_workers=8,
    )
    print(f"PUBLISHED to landing: {plan.dataset_id}/{plan.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
