"""Publish `pretrain/edu-mix-983b` — the exact call, runnable, inputs DERIVED from the receipts.

Run it in-region on Batch. `publish()` stream-hashes every object, so it PULLS every byte to
wherever it runs: 0.8 MiB/s from a laptop is ~9 days for this corpus. `hash_workers`/`copy_workers`
parallelize the two network-bound phases.

    python3 publish_driver.py --dry-run   # validates inputs, writes nothing, hashes nothing
    python3 publish_driver.py --go        # publishes to LANDING

NOTHING HERE CAN WRITE `s3://edullm-data`. `publish()` stages into `edullm-landing`; crossing the
airlock is `promote()`, which only the validator role can do, in a separate job definition.

Three properties this file enforces rather than documents, each because the alternative already
cost this project something:

1. **`SOURCE` MUST be the `s3://` form.** `publish()` at :1053 deletes every staged object when
   `source_kind == "local"`, and `source_kind` is decided at :945 purely by whether the string
   starts with `s3://`. The staged shards under `_ingest/` (30-day rule) ARE the owner's safety
   net — the reason no `_backup/` copy was made. A local path would silently delete that net as a
   side effect of an argument's FORM. Asserted, never remembered.

2. **`profile` is `pretrain-tokens/v1` — HYPHEN, not the module's underscore.** The module is
   `pretrain_tokens_v1.py`; the registered NAME is `pretrain-tokens/v1`
   (`profiles/registry.py`, `pretrain_tokens_v1.NAME`). `publish()` never validates the profile —
   it only string-matches it at :498 and copies it verbatim into the manifest — so the underscore
   form publishes 4 TB successfully and is then REJECTED by Gate A as `unknown-profile`.
   Asserted against the live registry below.

3. **Every number is DERIVED from the receipts, never typed.** Each hardcoded figure in this
   file's ancestors was wrong at least once; the reservoir driver says so in its own comments.
   `sources[]`, token totals and shard counts are read from the 185 build receipts, and the
   driver REFUSES to publish if they disagree with the plan.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys

#: 🔴 **MOVED for the 401 fix — was `29968a2b04008a8c`.** The registry's two Nemotron-CC-Math rows
#: were repointed from the GATED HF repo (HTTP 401, 7 of 16 bundle failures on 2026-08-09, 61 B
#: tokens of the math pillar) to the staged `s3://edullm-landing/_src/` copy. `repo` is inside the
#: plan document and `plan_id` is its content address, so the move is the fix working — not a
#: regression. Recomputed independently: 185 bundles, 39,205 train / 102 val shards,
#: 982,752,985,088 tokens — the SAME work under a new identity.
#:
#: ⚠️ This constant is a PREFIX. A stale value here reads `_ingest/final-dataset/<old>/` — an empty
#: or half-built prefix — and the driver would report "no labels" / "no receipts" rather than
#: anything that looks like a wrong-plan error. Verify against
#: `tests/test_curriculum_labels.FROZEN_PLAN_ID`, which is asserted against the checked-in registry.
PLAN_ID = "364cb4dd488a5761"
BUCKET = "edullm-landing"
PREFIX = "_ingest/final-dataset"
DATASET_ID = "pretrain/edu-mix-983b"
PROFILE = "pretrain-tokens/v1"

#: The `s3://` form is load-bearing — see property 1 in the module docstring.
SOURCE = f"s3://{BUCKET}/{PREFIX}/{PLAN_ID}/data/"

#: The plan's own totals, used as the cross-check the receipts must reproduce.
PLAN_SHARDS = 39_307
PLAN_TOKENS = 982_752_985_088


def assert_invariants() -> None:
    """Fail before any network call if either silent-failure mode is present."""
    if not SOURCE.startswith("s3://"):
        raise SystemExit(
            f"REFUSING: SOURCE={SOURCE!r} is not an s3:// URI. publish() would set "
            f"source_kind='local' and DELETE every staged shard after copying (publish.py:1053). "
            f"Those shards are the only backup of this build."
        )
    from edullm_data.profiles.registry import get_profile

    get_profile(PROFILE)  # raises ProfileError listing valid names


def load_receipts() -> list[dict]:
    """Read all 185 build receipts from S3. The receipts, not the plan, are what was BUILT."""
    from edullm_data.s3 import Boto3S3

    s3 = Boto3S3.default()
    receipts: list[dict] = []
    token = None
    rprefix = f"{PREFIX}/{PLAN_ID}/_receipts/"
    while True:
        kw = {"Bucket": BUCKET, "Prefix": rprefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.client.list_objects_v2(**kw)
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".json"):
                receipts.append(json.loads(s3.get(BUCKET, obj["Key"])))
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    return receipts


def derive(receipts: list[dict]) -> tuple[list[dict], dict]:
    """Turn receipts into `sources[]` plus realized totals. Refuses on any disagreement."""
    if not receipts:
        raise SystemExit(
            f"REFUSING: no receipts under {PREFIX}/{PLAN_ID}/_receipts/. The build has not "
            f"finished, or it wrote nothing. Publishing now would stage a partial corpus."
        )
    if len(receipts) != 185:
        raise SystemExit(
            f"REFUSING: {len(receipts)} receipts, expected 185 (one per bundle). A missing "
            f"receipt means a bundle did not complete; `corpus_build verify` is the check that "
            f"reports which."
        )

    by_source: dict[str, dict] = {}
    shards = tokens = documents = 0
    splits: dict[str, int] = {}
    for r in receipts:
        src = r["source"]
        n_shards = len(r["shards"])
        shards += n_shards
        tokens += r["tokens_out"]
        documents += r["documents"]
        splits[r["split"]] = splits.get(r["split"], 0) + r["tokens_out"]
        agg = by_source.setdefault(
            src, {"name": src, "tokens": 0, "documents": 0, "shards": 0}
        )
        agg["tokens"] += r["tokens_out"]
        agg["documents"] += r["documents"]
        agg["shards"] += n_shards

    if shards != PLAN_SHARDS:
        raise SystemExit(
            f"REFUSING: receipts declare {shards:,} shards, the plan says {PLAN_SHARDS:,}. "
            f"Regenerate from the receipts or run `corpus_build verify --plan-id {PLAN_ID}`."
        )
    if tokens != PLAN_TOKENS:
        raise SystemExit(
            f"REFUSING: receipts sum to {tokens:,} tokens, the plan says {PLAN_TOKENS:,}."
        )

    # Attach licence from the registry so the share-alike figure is derived, not asserted.
    reg = json.loads(open("/tmp/corpus-registry.json", encoding="utf-8").read())
    lic = {row["source_label"]: row.get("license", "") for row in reg["corpora"]}
    for name, agg in by_source.items():
        agg["license"] = lic.get(name, "UNKNOWN")

    sources = sorted(by_source.values(), key=lambda s: -s["tokens"])
    realized = {
        "shards": shards,
        "tokens_total": tokens,
        "documents": documents,
        "splits": splits,
    }
    return sources, realized


def build_kwargs(sources: list[dict], realized: dict) -> dict:
    sa = [s for s in sources if "SA" in (s["license"] or "")]
    sa_tokens = sum(s["tokens"] for s in sa)
    tot = realized["tokens_total"]
    val = realized["splits"].get("val", 0)
    train = realized["splits"].get("train", 0)
    return dict(
        dataset_id=DATASET_ID,
        purpose=(
            "983B-token educational pretraining corpus for the 96-expert flagship and 32-expert "
            "baseline MoEs, drawn from 39 sources across web, math, code, academic and synthetic "
            "categories"
        ),
        profile=PROFILE,
        tokenizer="tokenizer/dolma2-bpe",
        about=(
            f"{len(sources)} source streams tokenized with the published dolma2-bpe tokenizer "
            f"(vocab 100,278) into exact 25,001,984-token shards. Held-out documents are carved "
            f"BEFORE tokenizing by a hash of the document id alone, so val is drawn from "
            f"different DOCUMENTS than train rather than sampled from the same shuffled pool. "
            f"Exact-duplicate documents are removed within each bundle, and a 13-word-gram index "
            f"of eval benchmarks drops contaminated documents -- see limitations for where that "
            f"does not reach. Built as {len(sources)} sources over 185 bundles "
            f"(plan {PLAN_ID}, {realized['shards']:,} shards, {tot:,} tokens)."
        ),
        sources=sources,
        notes=(
            "Stage selection is a READER-side concern: this is one corpus, not a pre-split "
            "bulk/anneal pair. Each source was READ ONCE with a combined draw, so no byte set "
            "here corresponds to a separate anneal stage. OLMo-core takes a per-path token "
            "budget, so a training run draws its own stage mixture from these shards; `source` "
            "is a path segment the validator recomputes, so any re-weighting stays available."
        ),
        limitations=[
            {
                "kind": "contamination",
                "detail": (
                    "The synthetic portion is effectively UNDECONTAMINATED. FinePhrase is "
                    "rephrased FineWeb-Edu and rephrasing defeats n-gram matching, which is the "
                    "only decontamination applied. The 13-gram gate is verified effective on "
                    "verbatim text and should be assumed ineffective on rephrased text. No "
                    "LLM-judged tier was run."
                ),
            },
            {
                "kind": "identity",
                "detail": (
                    "Cosmopedia carries no document id, so a surrogate "
                    "(repo-relative path + row index, at a pinned revision) is used. A surrogate "
                    "id is NOT comparable across sources: any cross-source anti-join or dedup "
                    "keyed on document id silently excludes Cosmopedia."
                ),
            },
            {
                "kind": "licence",
                "detail": (
                    f"{len(sa)} of {len(sources)} source streams ({sa_tokens:,} tokens, "
                    f"{100 * sa_tokens / tot:.2f}% of the corpus) carry share-alike terms. "
                    f"Nemotron-CC-Math is used under the NVIDIA Data Agreement for Model "
                    f"Training, which limits use to internal training; its shards are "
                    f"enumerable by the `nemotron-cc-math` source prefix."
                ),
            },
            {
                "kind": "coverage",
                "detail": (
                    f"val is {val:,} tokens against {train:,} train "
                    f"({100 * val / tot:.3f}% of the corpus). Sources whose draw is below one "
                    f"val shard have no held-out split and all their documents are in train, "
                    f"because whole-shard selection cannot produce a partial shard."
                ),
            },
        ],
        hash_workers=16,
        copy_workers=16,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="validate inputs, write nothing")
    g.add_argument("--go", action="store_true", help="actually publish to LANDING")
    args = ap.parse_args()

    assert_invariants()
    print(f"INVARIANTS_OK source_kind=s3 profile={PROFILE}")

    receipts = load_receipts()
    sources, realized = derive(receipts)
    kwargs = build_kwargs(sources, realized)

    print(f"dataset_id : {kwargs['dataset_id']}")
    print(f"source     : {SOURCE}")
    print(f"profile    : {kwargs['profile']}   tokenizer: {kwargs['tokenizer']}")
    print(f"receipts   : {len(receipts)}")
    print(f"sources[]  : {len(sources)} streams")
    print(f"shards     : {realized['shards']:,}   tokens {realized['tokens_total']:,}")
    print(f"documents  : {realized['documents']:,}")
    print(f"splits     : {realized['splits']}")
    print(f"limitations: {[lim['kind'] for lim in kwargs['limitations']]}")
    print(f"workers    : hash={kwargs['hash_workers']} copy={kwargs['copy_workers']}")

    if args.dry_run:
        print(
            "\nDRY RUN -- nothing written, nothing hashed. NOTE: this validates the driver's "
            "own inputs (receipts, totals, profile, source form). It does NOT exercise "
            "enumeration, grouping or hashing, because publish() is never called."
        )
        return 0

    from edullm_data.publish import publish
    from edullm_data.s3 import Boto3S3

    created = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(f"\ncreated_at : {created}\nPUBLISHING to landing...", flush=True)
    plan = publish(SOURCE, s3=Boto3S3.default(), created_at=created, **kwargs)
    print(f"PUBLISHED {plan.dataset_id} {getattr(plan, 'version', '?')}")
    print("NOT PROMOTED: promotion is a separate job (edullm-promote), and the")
    print("edullm-landing-manifest-created rule is DISABLED. Nothing auto-promotes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
