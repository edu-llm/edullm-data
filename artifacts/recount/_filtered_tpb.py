#!/usr/bin/env python3
"""Measure tokens/byte on the ACTUAL `_filtered` corpora, not on `raw` as a proxy.

WHY THIS IS A SEPARATE MEASUREMENT. The footer count runs against `common-pile/raw_v0.1_parquet`
(RAW), but §3.2 specifies the `_filtered` variants. Scaling raw tokens by a byte ratio implicitly
assumes tokens/byte is IDENTICAL in both collections. That assumption is exactly what filtering
attacks: the filters drop short documents, non-English text, and OCR garbage, and OCR garbage
tokenizes far WORSE than clean prose (fragmenting into many short subwords). Removing it should
LOWER tokens/byte. So the raw-derived ratio is not automatically the filtered one, and the
difference has to be measured rather than asserted.

HOW, without downloading a corpus. The `_filtered` repos are `.json.gz`. A single HTTP Range
request for the first N bytes of a shard, fed to `zlib.decompressobj(16+MAX_WBITS)`, yields a
decodable prefix of that shard -- a gzip stream decompresses from its start, so a head range is
enough and no full shard is transferred. Sampling the head of EVERY shard (not many docs from
one shard) spreads the sample across the whole corpus: shards are the corpus's own partitioning,
so one prefix per shard covers all 91 / 17 / 8 of them.

ACKNOWLEDGED BIAS, and why it is acceptable here: within a shard this reads the FIRST few
documents rather than random ones, so it is a head sample at shard granularity. That is a real
limitation for any LENGTH-dependent quantity. It is tolerable for tokens/byte specifically
because tokens/byte is a property of script and domain and is near-constant across row groups --
independently measured on the raw parquet, where random-row-group sampling IS possible: per-group
CV 0.025 (peS2o), 0.032 (pubmed), 0.062 (arxiv). A quantity that stable across random groups
cannot be badly biased by which documents within a shard are read.

The number this produces is used ONLY as tokens/byte. Byte totals come from the gzip ISIZE
census and the cards; document counts come from the cards. NO DATASET DOWNLOAD (§5.7): a few
hundred KB per shard.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import urllib.parse
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))


def _token() -> str | None:
    t = os.environ.get("HF_TOKEN")
    if t:
        return t
    p = os.path.expanduser("~/.cache/huggingface/token")
    return open(p).read().strip() if os.path.exists(p) else None


H = {"User-Agent": "edullm-data/filtered-tpb"}
_t = _token()
if _t:
    H["Authorization"] = f"Bearer {_t}"


def head_docs(repo: str, path: str, nbytes: int, max_docs: int) -> list[str]:
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{urllib.parse.quote(path)}"
    req = urllib.request.Request(url, headers={**H, "Range": f"bytes=0-{nbytes - 1}"})
    last = None
    for a in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                comp = r.read()
            break
        except Exception as e:  # noqa: BLE001
            last = e
            if a == 3:
                raise
            import time
            time.sleep(3 * (a + 1))
    do = zlib.decompressobj(16 + zlib.MAX_WBITS)
    txt = do.decompress(comp).decode("utf8", "replace")
    out = []
    for line in txt.split("\n"):
        if len(out) >= max_docs:
            break
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)  # a truncated final line just fails and is skipped
        except Exception:  # noqa: BLE001
            continue
        t = o.get("text")
        if isinstance(t, str) and t:
            out.append(t)
    return out


def run(repo: str, nbytes: int, docs_per_shard: int) -> dict:
    from transformers import AutoTokenizer
    tk = AutoTokenizer.from_pretrained("allenai/dolma2-tokenizer")

    census = json.load(open(os.path.join(HERE, "_isize-academic.json")))
    shards = [s["path"] for s in census[repo]["per_shard"]]

    tb = tt = 0
    per_shard = []
    ndocs = 0
    errs = []
    for i, p in enumerate(shards):
        try:
            docs = head_docs(repo, p, nbytes, docs_per_shard)
            gb = gt = 0
            for s in docs:
                gb += len(s.encode("utf8"))
                gt += len(tk(s, add_special_tokens=False)["input_ids"])
            if gb:
                per_shard.append(gt / gb)
                tb += gb
                tt += gt
                ndocs += len(docs)
        except Exception as e:  # noqa: BLE001
            errs.append(f"{p}: {type(e).__name__}: {e}"[:200])
        print(f"  [{repo.split('/')[-1]}] {i+1}/{len(shards)} shards, {ndocs} docs, "
              f"tpb={tt/tb if tb else 0:.5f}", file=sys.stderr, flush=True)

    return {
        "repo": repo,
        "status": "ok" if tb else "FAILED",
        "tokenizer": "allenai/dolma2-tokenizer",
        "n_shards_sampled": len(per_shard),
        "n_shards_total": len(shards),
        "n_docs_sampled": ndocs,
        "sample_text_bytes": tb,
        "sample_tokens": tt,
        "tokens_per_byte": tt / tb if tb else None,
        "tokens_per_byte_cv_per_shard": (
            statistics.stdev(per_shard) / statistics.fmean(per_shard) if len(per_shard) > 1 else None
        ),
        "tokens_per_byte_min_shard": min(per_shard) if per_shard else None,
        "tokens_per_byte_max_shard": max(per_shard) if per_shard else None,
        "bytes_per_token": tb / tt if tt else None,
        "mean_doc_bytes_in_sample": tb / ndocs if ndocs else None,
        "sampling": "head prefix of EVERY shard (see module docstring for the bias note)",
        "errors": errs[:8],
    }


def main() -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("repo")
    p.add_argument("--bytes", type=int, default=600_000)
    p.add_argument("--docs-per-shard", type=int, default=8)
    p.add_argument("--out", default=None)
    a = p.parse_args()
    r = run(a.repo, a.bytes, a.docs_per_shard)
    out = a.out or os.path.join(HERE, f"_ftpb-{a.repo.split('/')[-1]}.json")
    json.dump(r, open(out, "w"), indent=2)
    print(json.dumps(r, indent=2))
    return 0 if r["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
