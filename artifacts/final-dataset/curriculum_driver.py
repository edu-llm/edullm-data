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

PLAN_ID = "29968a2b04008a8c"
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


def build_axis(manifest: dict):
    """The chunk axis, plus the arithmetic cross-checks."""
    from edullm_data.corpus_order import chunk_axis_from_manifest

    axis = chunk_axis_from_manifest(manifest, split="train")
    if len(axis.keys) != PLAN_TRAIN_SHARDS:
        raise SystemExit(
            f"REFUSING: the parent manifest holds {len(axis.keys):,} train shards, the plan says "
            f"{PLAN_TRAIN_SHARDS:,}. A missing shard shifts every chunk index past it, and the "
            f"resulting permutation is still bijective."
        )
    full = [t for t in axis.tokens if t == PLAN_SHARD_TOKENS]
    print(f"axis: {len(axis.keys):,} train shards, {len(full):,} at exactly "
          f"{PLAN_SHARD_TOKENS:,} tokens, {axis.n_chunks:,} chunks "
          f"({axis.n_chunks * 4:,} B as uint32), rule={axis.rule}")
    return axis


def load_streams(s3):
    """Every bundle's labels, assembled into per-STREAM arrays in file_shard order.

    ⚠️ **A stream is K bundles under file-sharding** (stackv2-edu 7, finepdfs-edu 4,
    nemotron-cc-math-3 3, -4plus 2), built by K separate Batch children. The packer fills the
    stream's ordinal block part by part in index order, so the stream's token space is the
    CONCATENATION of its parts in `file_shard` index order. Assembling them in any other order —
    S3 listing order, receipt order, dict order — silently reassigns every document past the first
    part to the wrong difficulty.
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
        strides = np.concatenate(
            [np.add(ls.records["n_tokens"].astype(np.int64), 1) for _, ls in members]
        )
        scores = np.concatenate([ls.records["mtld"].astype(np.float64) for _, ls in members])
        streams.append(
            StreamLabels(source=key[0], domain=key[1], split=key[2], strides=strides, mtld=scores)
        )
    if not streams:
        raise SystemExit(
            f"REFUSING: no labels under {lprefix}. The build ran without --labels, or has not "
            f"finished. Publishing an order vector over unlabelled tokens is not possible and "
            f"inventing difficulties for them would be worse."
        )
    return streams


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
    n_train = PLAN_TRAIN_SHARDS * per_shard
    n_val = PLAN_VAL_SHARDS * per_shard
    print(f"chunk rule      : (tokens - 1) // {SEQ_LEN_CHUNK} = {per_shard:,} per full shard")
    print(f"                  ⚠️ OLMo-core's own numpy_dataset.py:679 gives "
          f"{PLAN_SHARD_TOKENS // SEQ_LEN_CHUNK:,} — see the F-C6 finding")
    print(f"train chunks    : {PLAN_TRAIN_SHARDS:,} x {per_shard:,} = {n_train:,} "
          f"({n_train * 4:,} B = {n_train * 4 / 1e9:.2f} GB)")
    print(f"val chunks      : {PLAN_VAL_SHARDS:,} x {per_shard:,} = {n_val:,}")
    print(f"max_order_bytes : {MAX_ORDER_BYTES:,} (profile default is 536,870,912)")

    if args.plan:
        print("\nPLAN ONLY — no S3 call made. The publish path additionally requires "
              "PARENT_VERSION and PARENT_MANIFEST_SHA256, which are UNFILLED by design.")
        return 0

    assert_invariants()
    print(f"INVARIANTS_OK profile={PROFILE} parent={PARENT_DATASET_ID}/{PARENT_VERSION}")

    from edullm_data.corpus_order import build_order, identity_order
    from edullm_data.manifest import labels_from_path
    from edullm_data.s3 import Boto3S3

    s3 = Boto3S3.default()
    axis = build_axis(parent_manifest(s3))
    streams = load_streams(s3)
    print(f"labels: {len(streams)} streams, "
          f"{sum(s.documents for s in streams):,} documents, "
          f"{sum(s.n_tokens for s in streams):,} labelled tokens")

    mapping = {}
    for key in axis.keys:
        lab = labels_from_path(key)
        mapping[key] = (lab.get("source", ""), lab.get("domain"), "train")

    order = build_order(axis, streams, shard_stream=mapping, metric="mtld")
    ident = identity_order(n_val)
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
        profile={GROUP: PROFILE},
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
