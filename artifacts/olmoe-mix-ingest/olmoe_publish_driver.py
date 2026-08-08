#!/usr/bin/env python3
"""Publish `pretrain/olmoe-mix-50b` and `pretrain/olmoe-mix-100b` — the exact calls, runnable.

Run INSIDE a Batch job in us-east-1 (`infra/10-dataset-publish-jobdef.md`). `publish()`
stream-hashes every object to compute its manifest entry (`publish.py:418-425` -> `s3.hash_object`),
so it PULLS EVERY BYTE to wherever it runs. Measured single-stream in-region throughput is ~88 MB/s;
from a laptop it was ~2.9 MB/s, which for 366 GiB is ~36 days. Locality is what makes this possible
at all, and `hash_workers`/`copy_workers` are what make it fast.

    python3 olmoe_publish_driver.py --release 50b  --dry-run    # prints the call, writes nothing
    python3 olmoe_publish_driver.py --release 50b  --go         # publishes to LANDING
    python3 olmoe_publish_driver.py --release 100b --go

Modelled on `artifacts/reservoir/publish_driver.py`, which is the proven shape. Same discipline for
the same reason: every number that could be retyped has already been retyped wrong once in this
repo, so every figure below is READ from `olmoe_final_<release>.json`'s `realized` block (written by
the committed, deterministic `make_final_selection.py`) and re-derived from the measured
`olmoe_0824_sizes.json`. `load_inputs()` refuses to publish if the two disagree.

TWO INDEPENDENT DATASETS. Neither declares the other. The 50B train key set is a strict subset of
the 100B train key set, so 19 of the 50B's objects are byte-identical to 100B objects — declaring
`depends_on` between them would trip `shared-sha-with-parent` (`validate.py:759-768`) on every one.
They are siblings distinguished by token budget, exactly as `families/pretrain.json`'s notes
describe ("Names carry corpus plus token budget, because the budget is the axis that distinguishes
siblings").

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

# Names carry the UPSTREAM RELEASE CODE plus the REALIZED token budget, per §2 and
# `families/pretrain.json` ("Names carry corpus plus token budget, because the budget is the axis
# that distinguishes siblings").
#
# `-51b` / `-98b`, not `-50b` / `-100b`: whole-shard selection cannot land on a round number, and
# the realized counts are 51.069 Gtok and 98.131 Gtok. Naming these `-50b`/`-100b` would put a
# figure in the name that the manifest contradicts, which defeats the entire reason the budget is
# in the name — it is meant to be falsifiable against the manifest. Owner confirmed the
# realized-count form.
#
# `0824` survives `validate_name` as an upstream release code: `_reject_token` bans year-shaped
# 4-digit tokens (19xx/20xx) but exempts other 4-digit runs, the documented `1124` shape
# (`contracts.py:290-292, 310-313`). Keeping it matters because `0924` is a DIFFERENT upstream
# artifact — raw text, not these tokenized shards — so dropping the code would conflate them.
RELEASES = {
    "50b": {
        "dataset_id": "pretrain/olmoe-mix-0824-51b",
        "source": "s3://edullm-landing/_ingest/olmoe-mix-0824/50b/",
    },
    "100b": {
        "dataset_id": "pretrain/olmoe-mix-0824-98b",
        "source": "s3://edullm-landing/_ingest/olmoe-mix-0824/100b/",
    },
}

#: Upstream per-component totals for the WHOLE OLMoE-mix-0824 mix as listed in
#: `OLMo-core/src/olmo_core/data/mixes/OLMoE-mix-0824.txt` (1,122 shards, 15,580,114,781,464 bytes
#: = 3.895 Ttok). Every figure is `exact bytes / 4`, because the payload is uint32 and
#: `tokens * dtype_size == file bytes` holds exactly — these are MEASURED sizes (1,122 live HEADs,
#: all status 200, in `olmoe_0824_sizes.json`), not card arithmetic.
#:
#: ⚠️ These describe UPSTREAM, not our subset, which is why every `sources[]` row carries
#: `scope="upstream-full-collection"`. `readme.py:79-87` keys off that: any upstream-scoped row
#: relabels the table's column to "Upstream tokens" and prints the caveat paragraph. Omitting
#: `scope` here would print upstream figures under a "Tokens" heading, telling a consumer the
#: opposite of the truth — the exact confusion `scope` exists to resolve.
UPSTREAM_TOKENS = {
    "dclm": 3_704_993_775_401,
    "starcoder": 83_042_493_916,
    "pes2o": 58_552_154_223,
    "proofpile-2-arxiv": 20_774_296_636,
    "proofpile-2-open-web-math": 12_185_293_921,
    "proofpile-2-stack": 11_818_866_398,
    "wikipedia": 3_662_042_078,
}
UPSTREAM_TOTAL_TOKENS = 3_895_028_695_366
UPSTREAM_WEB_SHARE = 0.951210  # dclm / total. The number our composition deliberately departs from.

#: Human-facing names + upstream URIs. `uri` renders as the "Upstream" column (`readme.py:99-101`).
SOURCE_META = {
    "dclm": ("DCLM-baseline (web)", "https://huggingface.co/datasets/mlfoundations/dclm-baseline-1.0"),
    "starcoder": ("StarCoder (code)", "https://huggingface.co/datasets/bigcode/starcoderdata"),
    "pes2o": ("peS2o (scientific papers)", "https://huggingface.co/datasets/allenai/peS2o"),
    "proofpile-2-arxiv": ("Proof-Pile-2 arXiv", "https://huggingface.co/datasets/EleutherAI/proof-pile-2"),
    "proofpile-2-open-web-math": ("Proof-Pile-2 OpenWebMath", "https://huggingface.co/datasets/EleutherAI/proof-pile-2"),
    "proofpile-2-stack": ("Proof-Pile-2 AlgebraicStack", "https://huggingface.co/datasets/EleutherAI/proof-pile-2"),
    "wikipedia": ("Wikipedia + Wikibooks", "https://huggingface.co/datasets/allenai/dolma"),
}


def load_inputs(release: str) -> tuple[dict, dict]:
    """`(selection, realized)`, with the realized block RE-DERIVED from the measured sizes.

    Refuses to publish numbers that do not reconcile. Both `realized` and this re-derivation come
    from `olmoe_0824_sizes.json`, so a mismatch means one file is stale — which is exactly the
    failure that put a PLANNED figure in a table labelled "realized" twice in this repo.
    """
    sel = json.loads((HERE / f"olmoe_final_{release}.json").read_text(encoding="utf-8"))
    realized = sel["realized"]
    sizes = {k: b for _l, k, _s, b in json.loads((HERE / "olmoe_0824_sizes.json").read_text())}

    train_bytes = sum(sizes[k] for keys in sel["train"].values() for k in keys)
    val_bytes = sum(sizes[k] for keys in sel["val"].values() for k in keys)
    checks = {
        "train_shards": sum(len(v) for v in sel["train"].values()),
        "train_bytes": train_bytes,
        "train_tokens": train_bytes // 4,
        "val_bytes": val_bytes,
        "val_tokens": val_bytes // 4,
        "total_bytes": train_bytes + val_bytes,
        "total_tokens": (train_bytes + val_bytes) // 4,
    }
    for field, computed in checks.items():
        if realized[field] != computed:
            raise SystemExit(
                f"REFUSING: realized.{field} = {realized[field]:,} but the selection sums to "
                f"{computed:,}. Re-run make_final_selection.py."
            )

    # The web share is the headline claim in `limitations`; re-derive it rather than trust it.
    web = sum(sizes[k] for k in sel["train"].get("dclm", []))
    if abs(realized["web_fraction_of_train"] - web / train_bytes) > 1e-12:
        raise SystemExit("REFUSING: realized.web_fraction_of_train does not match the selection")

    # Every source key must be a real upstream key we measured, and no train key may also be the
    # val key. The ingest driver checks this too; a publish that trusted the ingest to have checked
    # would be trusting a claim rather than recomputing it.
    train_keys = {k for keys in sel["train"].values() for k in keys}
    val_keys = {k for keys in sel["val"].values() for k in keys}
    if train_keys & val_keys:
        raise SystemExit(f"REFUSING: val shard is also a train shard: {sorted(train_keys & val_keys)}")
    if not (train_keys | val_keys) <= set(sizes):
        raise SystemExit("REFUSING: selection names a key with no measured size")
    return sel, realized


def build_sources(sel: dict, realized: dict) -> list[dict]:
    """One `sources[]` row per upstream component present in this release's TRAIN split.

    `share` is OUR realized share of train (measured, per-component); `tokens` is the UPSTREAM
    component total, hence `scope`. Both are useful and they are different questions, which is
    precisely why `scope` has to be on the row: `readme.py:79-87` uses it to label the token column
    honestly. The val split is arXiv and is reported separately in `notes` rather than folded into
    a share, because a share of train that silently included val would not sum to the mix.
    """
    rows = []
    for label in sorted(sel["train"], key=lambda l: -realized["shares_of_train"][l]):
        name, uri = SOURCE_META[label]
        rows.append(
            {
                "name": name,
                "share": f"{100 * realized['shares_of_train'][label]:.2f}%",
                "tokens": UPSTREAM_TOKENS[label],
                "license": "ODC-By-1.0 (mix); per-component upstream terms also apply",
                "uri": uri,
                # `readme.py:79` matches `str(scope).startswith("upstream")`.
                "scope": "upstream-full-collection",
            }
        )
    return rows


def build_kwargs(release: str, sel: dict, realized: dict) -> dict:
    """Every kwarg below is verified present in `publish()`'s signature at `publish.py:832-857`:

        def publish(source, *, dataset_id, purpose, profile, s3, created_at,
                    tokenizer=None, data_bucket="edullm-data", landing_bucket=LANDING_BUCKET,
                    owner=None, group_meta=None, build_executor=None, env=None,
                    max_version_attempts=8, hash_workers=1, copy_workers=1,
                    sources=None, about=None, notes=None, limitations=None, license=None,
                    expected_payload=None, expected_version=None) -> PublishPlan

    We pass: dataset_id, purpose, profile, tokenizer, sources, about, notes, limitations, license,
    hash_workers, copy_workers. `source`, `s3` and `created_at` are supplied positionally/at the
    call site in `main()`. Nothing else is invented.

    NOT passed, deliberately:
      * `group_meta` — `publish()` builds the tokenizer `depends_on` itself from `tokenizer=`
        (`publish.py:918-932`), attaching it to the conventional `tokens` group, which is the group
        our single `tokens/` prefix produces (`publish.py:295-297`: the group is the first path
        segment).
      * `expected_payload` — it is a per-path `{bytes, sha256}` witness table
        (`publish.py:362-397`). We have real digests only from the ingest run's
        `_INGEST_COMPLETE.json`, and for a resumed/skipped object that file records `sha256: null`.
        A partially-populated table cannot be passed (`publish.py:377-386` requires a 64-hex digest
        for every path) and a table filled with digests we did not observe would be decoration.
        The honest witness chain is: ingest asserts received==pinned==written==sha256 at write
        time, then `publish()` independently re-streams and hashes every object anyway.
      * `expected_version` — we want the ordinary create-only reservation. A fixed version is for
        an artifact that must never be retried under a new version; that is not this.
    """
    cfg = RELEASES[release]
    r = realized
    web_pct = 100 * r["web_fraction_of_train"]
    tok_b = r["total_tokens"] / 1e9
    budget = "50B" if release == "50b" else "100B"

    # `validate_purpose` (`contracts.py:447-482`): 20-300 chars, must contain a space, and must not
    # normalize (lowercase, strip non-alphanumerics) into the blocklist — "", todo, tbd, data,
    # training data, the dataset, dataset, experiments, see readme, data from the run,
    # corpus for the project. §2's shape: "<what it is> for <what consumes it> to <what it decides>".
    purpose = (
        f"{tok_b:.1f}B-token dolma2-tokenized subset of AI2's OLMoE-mix-0824 for eduLLM "
        f"pretraining runs to establish a published-mix baseline at the {budget} budget"
    )

    return dict(
        dataset_id=cfg["dataset_id"],
        purpose=purpose,
        profile="pretrain-tokens/v1",
        tokenizer="tokenizer/dolma2-bpe",
        about=(
            f"{r['train_shards']} train shards plus one held-out validation shard, copied "
            f"byte-for-byte from AI2's public pre-tokenized OLMoE-mix-0824 at olmo-data.org and "
            f"renamed to this standard's layout. The upstream payload is headerless raw uint32 "
            f"little-endian despite its .npy extension, so ingest was a copy and a rename to "
            f".u32le.bin — NO re-tokenization, and no byte was altered. The tokenizer is the "
            f"published dolma2-bpe (vocab 100278, EOS 100257), pinned as a dependency, so token "
            f"counts are exact: every shard's byte count is a whole multiple of 4 and "
            f"tokens = bytes / 4. Shard ordinals are allocated globally and contiguously from "
            f"00000 across all seven source components in a flat tokens/ group; the per-component "
            f"composition is in the sources table, not in the keys. Validation is one whole shard "
            f"({r['val_tokens'] / 1e9:.3f}B tokens, {100 * r['val_tokens'] / r['total_tokens']:.2f}% "
            f"of the release) drawn from a source object used in NO train slice of either the 50B "
            f"or the 100B release, because a packed token shard carries no document boundaries and "
            f"so cannot be split any finer than a file."
        ),
        sources=build_sources(sel, realized),
        license={"id": "ODC-By-1.0", "basis": "declared"},
        limitations=[
            {
                "kind": "deduplication",
                "detail": (
                    "NOT deduplicated by us. No dedup of any kind was applied during ingest — "
                    "this is a byte-for-byte copy of upstream shards. Whatever duplication AI2's "
                    "own pipeline left in OLMoE-mix-0824 is present here unchanged, and we have "
                    "not measured it."
                ),
            },
            {
                "kind": "contamination",
                "detail": (
                    "NOT decontaminated by us, and we added nothing. Upstream applied its own "
                    "decontamination — the source paths say so: the Proof-Pile-2 components are "
                    "under 'v0_decontaminated' and StarCoder under 'v1-decon-100_to_20k-2star-"
                    "top_token_030'. We did not verify that work, did not extend it to the DCLM, "
                    "peS2o or Wikipedia components (whose upstream paths make no decontamination "
                    "claim), and ran no n-gram eval-overlap gate of our own. Treat eval-set "
                    "cleanliness as an UPSTREAM claim, not one this dataset establishes."
                ),
            },
            {
                "kind": "composition",
                "detail": (
                    f"Web is DELIBERATELY UNDERWEIGHTED. DCLM is {web_pct:.2f}% of train here "
                    f"against {100 * UPSTREAM_WEB_SHARE:.2f}% in the full upstream mix. This is an "
                    f"intentional composition choice, NOT sampling drift: holding web to ~75% is "
                    f"what buys whole shards of the smaller, higher-quality sources (peS2o, "
                    f"Wikipedia, arXiv, OpenWebMath, AlgebraicStack), which at upstream "
                    f"proportions would round to a fraction of a shard at this budget. Selection "
                    f"is whole-shard because that is the smallest unit a packed token file "
                    f"supports, so no share is hit exactly; the realized shares in the sources "
                    f"table are measured from the shipped bytes."
                ),
            },
            {
                "kind": "coverage",
                "detail": (
                    "Wikipedia is fully consumed at 2 of 2 upstream shards — a POOL LIMIT, not a "
                    "composition choice. OLMoE-mix-0824 contains only two Wikipedia shards in "
                    "total (3.662B tokens), so its 7.37%/3.78% share is the entire upstream "
                    "component and cannot be increased without a different source."
                ),
            },
            {
                "kind": "coverage",
                "detail": (
                    "The validation split is one whole shard of arXiv, so it is NOT a "
                    "representative sample of the training mix — it measures held-out loss on "
                    "academic prose only, and says nothing directly about web, code or "
                    "encyclopedic text. Packed token shards carry no document boundaries, so a "
                    "stratified per-source val split would require re-tokenizing from source "
                    "documents, which this ingest deliberately does not do."
                ),
            },
        ],
        notes=(
            f"Realized totals: {r['train_tokens']:,} train + {r['val_tokens']:,} val = "
            f"{r['total_tokens']:,} tokens in {r['train_shards'] + 1} shards "
            f"({r['total_bytes']:,} bytes = {r['total_bytes'] / 1024**3:.2f} GiB). "
            f"Shards are large and UNEVEN — from 0.17 GB to 17.18 GB (16.0 GiB exactly, several "
            f"DCLM shards) — because they are upstream files, not repacked to a fixed token count. "
            f"A mixture drawn from this corpus therefore has coarse weight granularity: with 1-18 "
            f"shards per component, per-source weights cannot be hit closely, and single-shard "
            f"components are all-or-nothing. Sibling releases pretrain/olmoe-mix-50b and "
            f"pretrain/olmoe-mix-100b are INDEPENDENT datasets that intentionally do not declare "
            f"each other; the 50B train set is a strict subset of the 100B train set, so do not "
            f"train on one and evaluate on the other. The two releases also use DIFFERENT val "
            f"shards, each held out of both releases' train splits."
        ),
        # `publish.py:836-838` documents both: they parallelize the stream-hash in `build_plan` and
        # the server-side copy to the final prefix. 16/16 is what the live reservoir driver used.
        # ⚠️ Requires a matching `max_pool_connections` on the client — see main().
        hash_workers=16,
        copy_workers=16,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", required=True, choices=tuple(RELEASES))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="print the call, write nothing")
    g.add_argument("--go", action="store_true", help="actually publish to LANDING")
    args = ap.parse_args(argv)

    sel, realized = load_inputs(args.release)
    kwargs = build_kwargs(args.release, sel, realized)
    source = RELEASES[args.release]["source"]

    print(f"dataset_id : {kwargs['dataset_id']}")
    print(f"source     : {source}")
    print(f"profile    : {kwargs['profile']}   tokenizer: {kwargs['tokenizer']}")
    print(f"purpose    : {kwargs['purpose']}")
    print(f"             ({len(kwargs['purpose'])} chars; validate_purpose needs 20-300)")
    print(f"license    : {kwargs['license']}")
    print(f"sources[]  : {len(kwargs['sources'])} entries, all scope="
          f"{kwargs['sources'][0]['scope']!r}")
    for s in kwargs["sources"]:
        print(f"   {s['share']:>7s} of train  {s['name']:32s} upstream {s['tokens']:>17,} tok")
    print(f"limitations: {[lim['kind'] for lim in kwargs['limitations']]}")
    print(f"shards     : {realized['train_shards']} train + 1 val, "
          f"{realized['total_tokens']:,} tokens, "
          f"{realized['total_bytes'] / 1024**3:.2f} GiB")
    print(f"web share  : {100 * realized['web_fraction_of_train']:.2f}% of train "
          f"(upstream {100 * UPSTREAM_WEB_SHARE:.2f}%)")
    print(f"workers    : hash={kwargs['hash_workers']} copy={kwargs['copy_workers']}")

    if args.dry_run:
        print("\nDRY RUN — nothing written. Re-run with --go inside an in-region Batch job.")
        return 0

    from edullm_data.publish import publish
    from edullm_data.s3 import Boto3S3

    # ⚠️ SIZED POOL, OR THE 16 WORKERS SILENTLY BECOME ~10. `Boto3S3.default()` with no argument
    # passes no `botocore.config.Config`, so `max_pool_connections` is botocore's default 10, and
    # botocore does not pass `block=True` to urllib3 — the surplus connection is DISCARDED with a
    # "Connection pool is full" log line and no error. `s3.py:211-249`, and
    # `infra/10-dataset-publish-jobdef.md` calls out this exact trap for a publish driver calling
    # `Boto3S3.default()` directly with `hash_workers=16`. 40 covers hash + copy fan-out together.
    s3 = Boto3S3.default(max_pool_connections=40)

    created = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(f"\ncreated_at : {created}\nPUBLISHING to landing...", flush=True)
    plan = publish(source, s3=s3, created_at=created, **kwargs)
    print(f"PUBLISHED {plan.dataset_id} {getattr(plan, 'version', '?')}")
    print("`edullm-landing-manifest-created` is DISABLED, so nothing auto-promotes. Submit")
    print("`edullm-validator` by UNVERSIONED name with the landing prefix in the command override.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
