#!/usr/bin/env python3
"""Harvest a stratified sample of documents from each source needing a domain label.

Phase 0 task D of DATASET-DESIGN-reservoir.md §9.3. Feeds task E, the dual-judge smoke test
that gates the ~$595 full classification run.

WHAT THIS PRODUCES: one JSONL per source, each line `{source, doc_id, offset, text, n_chars}`.
`text` is a prefix, not the whole document (see below). Written locally as a few MB of JSON, then
uploaded to `s3://edullm-landing/_smoke/samples/` by the caller.

## Sampling design, and the three things it gets right on purpose

**1. Random offsets, never a contiguous head.** Parquet row order in these corpora frequently
correlates with content — crawl batch, date, repository name. Reading the first 500 rows would
sample one slice of the web and call it the corpus. This is the same positional-bias failure
`read.py` documents for whole-shard selection (§2.2), one level down.

**2. Small batches at many distinct offsets.** A row group is a contiguous block, so 20
consecutive rows are nowhere near 20 independent draws. Default `--batch 4` buys real
independence for the same row count; the cost is more HTTP round-trips.

**3. A 256-token prefix, and why that is defensible here.** §9.3 specifies 256-token prefixes.
A prefix is a *biased* view of a document — you see the intro, never the body — and for a
quality judgement that would be disqualifying. For a SUBJECT judgement it is much weaker: a
document about organic chemistry announces that in its first paragraph. It also matches what a
production classifier would see, and EAI-Distill-0.5b itself chunks anything over 30k chars
(head + random middle + tail), so full documents are not what the model reads either.

  ⚠ The honest cost: documents whose subject only becomes clear late — a personal blog post that
  turns into a math explainer — will be misjudged by ALL THREE models. That inflates the
  "all three differ" excluded bucket rather than producing false agreement, so it costs
  statistical power, not correctness. Recorded in the output as `n_excluded`.

We cut the prefix on TOKEN count using the dolma2 tokenizer, not characters, so every judge sees
the same budget regardless of how densely the source tokenizes.

## No dataset downloads

datasets-server `/rows` serves arbitrary offsets server-side. Harvesting 2,500 documents costs a
few MB against corpora in the hundreds of GB — metadata-scale, consistent with §5.7's "no dataset
byte touches a laptop." Never `load_dataset()` here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVER = "https://datasets-server.huggingface.co"


def _headers() -> dict:
    """Authenticate when a token is available -- the anonymous rate limit is low enough that a
    few parallel jobs trip HTTP 429 within minutes (measured)."""
    h = {"User-Agent": "edullm-data/harvest"}
    tok = os.environ.get("HF_TOKEN")
    if not tok:
        p = Path.home() / ".cache/huggingface/token"
        if p.exists():
            tok = p.read_text().strip()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


UA = _headers()

# The five sources that need a CLASSIFIED domain (§1.2). Sources that ship a subdomain upstream
# -- stackv2-edu/<language>, essential-web/<topic> -- are excluded: using an upstream label is
# free and needs no gate.
#
# `text_column` is pinned EXPLICITLY for every source rather than guessed. §3.3 trap 1 is the
# reason: FinePhrase's `text` column holds the ORIGINAL FineWeb-Edu document while the rewrite
# lives in `rollout_results[0].text`, and no size or hash check catches the mistake.
SOURCES = {
    "dclm": {
        "dataset": "mlfoundations/dclm-baseline-1.0",
        "config": None,          # resolved from /splits
        "text_column": "text",
        "note": "diverse web; /statistics is broken for this corpus (HTTP 501)",
    },
    "finemath": {
        "dataset": "HuggingFaceTB/finemath",
        "config": "finemath-3plus",
        "text_column": "text",
        "note": "math; the reservoir's math pool anchor",
    },
    "academic": {
        "dataset": "common-pile/peS2o_filtered",
        "config": None,
        "text_column": "text",
        "note": "academic papers; card token figure is whitespace WORDS, not tokens",
    },
    "reference": {
        "dataset": "HuggingFaceFW/finewiki",
        "config": "en",
        "text_column": "text",
        "note": "wiki; MUST be `text`, NOT `wikitext` (raw markup, ~1.6x longer)",
    },
    "qa-forum": {
        "dataset": "common-pile/stackexchange_filtered",
        "config": None,
        "text_column": "text",
        "note": "Q&A; CC-BY-SA, keep separable (§7 item 4)",
    },
}


def _get(path: str, params: dict, tries: int = 5, timeout: int = 90):
    url = f"{SERVER}/{path}?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode("utf8", "replace")
            if e.code in (400, 404, 500, 501):
                return {"_http_error": e.code, "_body": body}
            # 429 is a shared rate limit, not a broken corpus -- exponential backoff, and be
            # patient. Measured: parallel jobs exhaust the quota and a linear 6s retry cannot
            # outlast it, which mislabels a fine source as unmeasurable.
            last = f"HTTP {e.code}: {body[:120]}"
            time.sleep(min(120, 15 * 2 ** attempt) if e.code == 429 else 6 * (attempt + 1))
            continue
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(3 * (attempt + 1))
    return {"_error": last}


def resolve_config(dataset: str, want: str | None) -> tuple[str | None, str]:
    if want:
        return want, "train"
    d = _get("splits", {"dataset": dataset})
    for s in d.get("splits") or []:
        return s["config"], s["split"]
    return None, "train"


def n_rows(dataset: str, config: str) -> int | None:
    d = _get("size", {"dataset": dataset})
    for c in (d.get("size", {}).get("configs") or []):
        if c["config"] == config:
            return c["num_rows"]
    return None


def harvest_source(
    key: str, spec: dict, tokenizer, n_docs: int, prefix_tokens: int,
    batch: int, seed: int, min_chars: int,
) -> tuple[list[dict], dict]:
    dataset = spec["dataset"]
    config, split = resolve_config(dataset, spec["config"])
    total = n_rows(dataset, config) if config else None
    meta = {
        "source": key, "dataset": dataset, "config": config, "split": split,
        "n_rows_reported": total, "text_column": spec["text_column"], "note": spec["note"],
    }
    if not config or not total:
        meta["status"] = "FAILED"
        meta["error"] = f"could not resolve config/rows (config={config}, rows={total})"
        return [], meta

    rng = random.Random(seed)
    out: list[dict] = []
    errors: list[str] = []
    skipped_short = 0
    seen_hashes: set[str] = set()
    dup_dropped = 0

    # Oversample offsets: some rows come back empty, too short, or duplicated.
    target_batches = int(n_docs / batch * 1.6) + 4
    offsets = sorted({rng.randrange(0, max(1, total - batch)) for _ in range(target_batches)})
    rng.shuffle(offsets)

    for off in offsets:
        if len(out) >= n_docs:
            break
        got = _get("rows", {"dataset": dataset, "config": config, "split": split,
                            "offset": off, "length": batch})
        if "rows" not in got:
            errors.append(f"offset {off}: {got.get('_http_error') or got.get('_error')}")
            if len(errors) > 12:
                break
            continue
        for item in got["rows"]:
            if len(out) >= n_docs:
                break
            row = item.get("row", {})
            text = row.get(spec["text_column"])
            if not isinstance(text, str):
                errors.append(f"column {spec['text_column']!r} absent/not str; keys={sorted(row)[:10]}")
                break
            if len(text) < min_chars:
                skipped_short += 1
                continue
            # Exact-duplicate guard. A duplicate would be judged twice and double-counted, and
            # (per §1.4) this project has already shipped a corpus whose held-out shards were
            # byte-copies of train shards -- worth not repeating even in a 500-doc sample.
            h = hashlib.sha256(text.encode("utf8")).hexdigest()[:16]
            if h in seen_hashes:
                dup_dropped += 1
                continue
            seen_hashes.add(h)

            ids = tokenizer(text, add_special_tokens=False)["input_ids"][:prefix_tokens]
            out.append({
                "source": key,
                "doc_id": f"{key}-{h}",
                "offset": off,
                "n_chars_full": len(text),
                "n_tokens_prefix": len(ids),
                "text": tokenizer.decode(ids),
            })

    meta.update({
        "status": "ok" if out else "FAILED",
        "n_harvested": len(out),
        "n_distinct_offsets_used": len(offsets),
        "n_skipped_too_short": skipped_short,
        "n_exact_duplicates_dropped": dup_dropped,
        "errors": errors[:8],
    })
    return out, meta


def main() -> int:
    p = argparse.ArgumentParser(description="Harvest classification samples (Phase 0 task D).")
    p.add_argument("--out-dir", default="artifacts/smoke/samples")
    p.add_argument("--n-docs", type=int, default=500)
    p.add_argument("--prefix-tokens", type=int, default=256)
    p.add_argument("--batch", type=int, default=4)
    p.add_argument("--min-chars", type=int, default=200,
                   help="drop stubs -- too short to carry a subject at all")
    p.add_argument("--seed", type=int, default=20260731)
    p.add_argument("--only", default=None, help="comma-separated subset of source keys")
    args = p.parse_args()

    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained("allenai/dolma2-tokenizer")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    keys = args.only.split(",") if args.only else list(SOURCES)

    all_meta = []
    for i, key in enumerate(keys):
        if key not in SOURCES:
            print(f"[harvest] unknown source {key!r}, skipping", file=sys.stderr)
            continue
        print(f"[harvest] {key}: {SOURCES[key]['dataset']}", file=sys.stderr)
        # Distinct seed per source, derived not hand-picked, so sources don't share an offset pattern
        docs, meta = harvest_source(
            key, SOURCES[key], tokenizer, args.n_docs, args.prefix_tokens,
            args.batch, args.seed + i * 7919, args.min_chars,
        )
        path = out_dir / f"{key}.jsonl"
        with path.open("w") as f:
            for d in docs:
                f.write(json.dumps(d) + "\n")
        meta["path"] = str(path)
        all_meta.append(meta)
        print(f"[harvest]   -> {meta['n_harvested']} docs ({meta['status']}) {path}", file=sys.stderr)

    manifest = out_dir / "_harvest.json"
    manifest.write_text(json.dumps({
        "task": "Phase 0 task D -- classification samples",
        "prefix_tokens": args.prefix_tokens,
        "tokenizer": "allenai/dolma2-tokenizer",
        "seed": args.seed,
        "sources": all_meta,
    }, indent=2) + "\n")
    print(f"[harvest] wrote {manifest}", file=sys.stderr)
    total = sum(m.get("n_harvested", 0) for m in all_meta)
    print(f"[harvest] TOTAL {total} docs across {len(all_meta)} sources", file=sys.stderr)
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
