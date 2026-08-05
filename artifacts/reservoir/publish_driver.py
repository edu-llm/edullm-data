"""Publish `pretrain/reservoir-dolma2` — the exact call, runnable, with its inputs read from disk.

Run it INSIDE a Batch job in us-east-1 (see `infra/10-dataset-publish-jobdef.md`). `publish()`
stream-hashes every object, so it PULLS every byte to wherever it runs: measured single-stream
in-region throughput is ~88 MB/s (from the `verify --deep` run), and ~2.9 MB/s from a laptop, which
is ~96 h for this corpus. `hash_workers`/`copy_workers` parallelize it; locality is what makes it
possible at all.

This is a script rather than a documented snippet because every number in it that could be retyped
has already been retyped wrong once. `sources[]`, the token totals, and the share-alike figure are
READ from `sources.json` / `realized-tokens.json`, both generated from the 27 receipts. The two
things a human must confirm are at the top: DRY_RUN and the image/plan identity.

    python3 artifacts/reservoir/publish_driver.py --dry-run     # prints the plan, writes nothing
    python3 artifacts/reservoir/publish_driver.py --go          # publishes to LANDING

Nothing here can write `s3://edullm-data`. `publish()` stages into `edullm-landing`; crossing the
airlock is `promote()`, which only the validator role can do.
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
DATASET_ID = "pretrain/reservoir-dolma2"
PLAN_ID = "d5c9bcd38735e1f0"
SOURCE = f"s3://edullm-landing/_ingest/reservoir-dolma2/build/{PLAN_ID}/data/"

# Gate 1 of PUBLISH-SPEC.md, satisfied 2026-08-05 by job 507356db (`rsv-verify-deep-2`):
#   OK 27 bundles, 10049 shards (payload re-hashed) / VERIFY_DONE_RC=0
# Re-run `corpus_build verify --plan-id <PLAN_ID> --deep` if ANY shard is rebuilt after this.
VERIFY_PASSED = True


def load_inputs() -> tuple[list[dict], dict]:
    sources = json.loads((HERE / "sources.json").read_text(encoding="utf-8"))
    realized = json.loads((HERE / "realized-tokens.json").read_text(encoding="utf-8"))
    # Refuse to publish numbers that do not reconcile. Both files are generated from the receipts,
    # so a mismatch means one is stale — which is exactly the failure that put a PLANNED figure in a
    # table labelled "realized" twice already.
    total = sum(s["tokens"] for s in sources)
    if total != realized["splits"]["train"]:
        raise SystemExit(
            f"REFUSING: sources.json sums to {total:,} but realized-tokens.json says train is "
            f"{realized['splits']['train']:,}. Regenerate both from the receipts."
        )
    if realized["wheel_version"] != "0.7.4" or realized["bundles"] != 27:
        raise SystemExit(f"REFUSING: unexpected realized provenance: {realized['wheel_version']}, "
                         f"{realized['bundles']} bundles")
    return sources, realized


def build_kwargs(sources: list[dict], realized: dict) -> dict:
    share_alike = [s for s in sources if "SA" in s["license"]]
    sa_tokens = sum(s["tokens"] for s in share_alike)
    tr = realized["splits"]["train"]
    return dict(
        dataset_id=DATASET_ID,
        purpose=(
            "252B-token multi-source reservoir for the eduLLM team to draw 20B-token training "
            "mixtures from, to compare data mixes at fixed compute"
        ),
        profile="pretrain-tokens/v1",
        tokenizer="tokenizer/dolma2-bpe",
        about=(
            "Eight source categories tokenized with the published dolma2-bpe tokenizer into exact "
            "25,001,984-token shards. Held-out documents are carved BEFORE tokenizing by a hash of "
            "(source, document id), so val is drawn from different documents than train rather "
            "than sampled from the same shuffled pool. Exact-duplicate documents are removed "
            "within each bundle, and a 13-word-gram index of eval benchmarks is used to drop "
            "contaminated documents -- see limitations for where that does not reach. Every "
            "shard's payload bytes were re-hashed against its build receipt before publishing "
            f"(plan {PLAN_ID}, 10,049 shards, 1.005 TB)."
        ),
        sources=sources,
        limitations=[
            {"kind": "contamination",
             "detail": (
                 "The 59.6B-token synthetic portion (23.82% of train, the four FinePhrase configs) "
                 "is effectively UNDECONTAMINATED. FinePhrase is rephrased FineWeb-Edu, and "
                 "rephrasing defeats n-gram matching, which is the only decontamination this "
                 "corpus applies. The 13-gram gate is verified effective on verbatim text (40/40 "
                 "GSM8K test questions caught, 0/2 false positives) and should be assumed "
                 "ineffective on this portion. A second, LLM-judged tier was scoped at ~$200 and "
                 "NOT run."
             )},
            {"kind": "coverage",
             "detail": (
                 "ubuntu-irc has no val split: one val shard requires 5,000,396,800 source tokens "
                 "at val_fraction 0.005 and the source holds 1.87B, so whole-shard selection "
                 "cannot produce one. Its documents are all in train."
             )},
            # Derived, not typed: every hardcoded figure in this file's ancestors was wrong at least
            # once. `partial` comes from sources.json, which comes from the receipts.
            *[
                {"kind": "coverage",
                 "detail": (
                     f"{p['name']} is PARTIAL and its note records why: {p['note']}. It is "
                     f"{p['share']} of train, not the share a plan-derived figure would suggest."
                 )}
                for p in sources if "note" in p
            ],
        ],
        notes=(
            f"Licensing is MIXED and includes share-alike: "
            f"{', '.join(s['name'] for s in share_alike)} ({sa_tokens:,} tokens, "
            f"{100 * sa_tokens / tr:.2f}% of train) are CC-BY-SA-4.0, finewiki additionally GFDL. "
            f"Per-source licenses and pinned upstream revisions are in sources[]; there is no "
            f"single dataset-level license id. Shard granularity is 25,001,984 tokens, so a "
            f"mixture drawn from this reservoir has ~0.1-0.4% weight granularity per source "
            f"(>=238 shards each, except ubuntu-irc at 71). Realized totals: "
            f"{tr:,} train + {realized['splits']['val']:,} val = "
            f"{realized['tokens_total']:,} tokens in {realized['shards']:,} shards."
        ),
        hash_workers=16,
        copy_workers=16,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="print the call, write nothing")
    g.add_argument("--go", action="store_true", help="actually publish to LANDING")
    args = ap.parse_args()

    if not VERIFY_PASSED:
        raise SystemExit("REFUSING: verify --deep has not passed for this plan.")

    sources, realized = load_inputs()
    kwargs = build_kwargs(sources, realized)

    print(f"dataset_id : {kwargs['dataset_id']}")
    print(f"source     : {SOURCE}")
    print(f"profile    : {kwargs['profile']}   tokenizer: {kwargs['tokenizer']}")
    print(f"sources[]  : {len(sources)} entries, {sum(s['tokens'] for s in sources):,} train tokens")
    print(f"shards     : {realized['shards']:,}   total tokens {realized['tokens_total']:,}")
    print(f"limitations: {[lim['kind'] for lim in kwargs['limitations']]}")
    print(f"workers    : hash={kwargs['hash_workers']} copy={kwargs['copy_workers']}")

    if args.dry_run:
        print("\nDRY RUN — nothing written. Re-run with --go inside an in-region Batch job.")
        return 0

    import boto3
    from botocore.config import Config

    from edullm_data.publish import publish
    from edullm_data.s3 import Boto3S3

    # NOT `Boto3S3.default()`. It passes no botocore Config, so `max_pool_connections` is the
    # default 10 — and botocore does not pass `block=True` to urllib3, so exceeding the pool
    # neither raises nor waits: urllib3's `_put_conn` DISCARDS the surplus connection and logs
    # "Connection pool is full". With hash_workers=16 that means workers 11..16 pay a fresh TLS
    # handshake on every one of 10,049 objects, silently capping the speedup at ~10 workers with no
    # error anywhere. Sized to the larger of the two worker counts so every thread keeps a socket.
    # (Same ceiling `corpus_build._s3(max_pool_connections=...)` handles for the threaded verify.)
    pool = max(kwargs["hash_workers"], kwargs["copy_workers"]) + 2  # +2: headroom for control calls
    s3 = Boto3S3(
        boto3.client("s3", region_name="us-east-1", config=Config(max_pool_connections=pool))
    )
    created = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(f"\ncreated_at : {created}\nmax_pool_connections: {pool}")
    print("PUBLISHING to landing...", flush=True)
    plan = publish(SOURCE, s3=s3, created_at=created, **kwargs)
    print(f"PUBLISHED {plan.dataset_id} {getattr(plan, 'version', '?')}")
    # `edullm-landing-manifest-created` is ENABLED (verified live 2026-08-05, and demonstrated
    # twice by this driver: writing the manifest above fired `edullm-validator` within seconds on
    # both the first attempt and the retry). An earlier version of these lines said the rule was
    # disabled and that nothing auto-promotes -- copied from three docs that were all wrong -- so
    # it printed a reassurance that the same run immediately falsified. Do not restore it; read the
    # rule state instead of claiming it.
    print("AUTO-PROMOTION IS LIVE: the manifest write fires EventBridge -> edullm-validator")
    print("  -> Gate A -> promote() into edullm-data, where frozen means frozen.")
    print("  Watch the validator job; a pass SEALS the prefix with _VALIDATED.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
