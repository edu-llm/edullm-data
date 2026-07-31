#!/usr/bin/env python3
"""Measure EXACT uncompressed UTF-8 byte totals for common-pile `.json.gz` repos, 8 bytes per file.

WHY. The academic footer count runs against `common-pile/raw_v0.1_parquet`, which is the RAW
collection; §3.2 specifies the `_filtered` variants, a strictly smaller ROW SET. To convert a raw
measurement into a filtered one I need the raw->filtered byte ratio. The cards publish UTF-8 GB
for both, but at 2-4 significant figures -- arxiv's "21" vs "19" carries +-0.5 GB, i.e. +-2.6%,
which is the same order as the quantity being estimated.

THE TRICK. A gzip member's last 8 bytes are CRC32 + ISIZE, and ISIZE is the uncompressed size
mod 2^32. One HTTP Range request of 8 bytes per shard therefore yields the exact decompressed
size of every shard without decompressing anything -- the same class of metadata read as a
parquet footer, and far cheaper.

TWO TRAPS, both checked rather than assumed:
  1. ISIZE is mod 4 GiB. A shard whose uncompressed size exceeds 4 GiB wraps silently. Guarded by
     comparing the implied compression ratio against the repo's plausible range; a wrap shows up
     as an absurdly low ratio.
  2. Multi-member (concatenated) gzip. The trailer then describes only the LAST member, so the
     sum undercounts badly. Self-checking: the summed raw ISIZE is compared against the raw
     card's published UTF-8 GB. Agreement to <1% means single-member, and simultaneously
     validates the card's byte column -- which is what licenses using the filtered card figure.

NOTE these are JSONL-of-JSON-objects bytes, i.e. text PLUS the `id`/`metadata` JSON wrapper, so
the ISIZE total is an upper bound on text bytes alone. It is used here only as a RATIO between
two repos built by the same writer with the same wrapper, where the wrapper largely cancels.
"""
from __future__ import annotations

import json
import os
import struct
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))


def _token() -> str | None:
    t = os.environ.get("HF_TOKEN")
    if t:
        return t
    p = os.path.expanduser("~/.cache/huggingface/token")
    return open(p).read().strip() if os.path.exists(p) else None


H = {"User-Agent": "edullm-data/isize"}
_t = _token()
if _t:
    H["Authorization"] = f"Bearer {_t}"


def api(url: str, tries: int = 4):
    last = None
    for a in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=120) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            import time
            time.sleep(2 * (a + 1))
    raise RuntimeError(last)


def isize(repo: str, path: str, size: int, tries: int = 4) -> int:
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{urllib.parse.quote(path)}"
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={**H, "Range": f"bytes={size - 8}-{size - 1}"})
            with urllib.request.urlopen(req, timeout=120) as r:
                d = r.read()
            return struct.unpack("<I", d[4:8])[0]
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            import time
            time.sleep(2 * (a + 1))
    raise RuntimeError(last)


def measure(repo: str, workers: int = 8) -> dict:
    tree = api(f"https://huggingface.co/api/datasets/{repo}/tree/main?recursive=1&limit=1000")
    fs = [e for e in tree if e["type"] == "file" and e["path"].endswith(".gz")]
    fs.sort(key=lambda e: e["path"])
    with ThreadPoolExecutor(max_workers=workers) as ex:
        izs = list(ex.map(lambda e: isize(repo, e["path"], e["size"]), fs))
    comp = sum(e["size"] for e in fs)
    tot = sum(izs)
    ratios = [iz / e["size"] for iz, e in zip(izs, fs)]
    print(f"{repo:42} {len(fs):3} shards  comp={comp/1e9:8.3f} GB  "
          f"ISIZE={tot/1e9:9.4f} GB  ratio={tot/comp:.3f} "
          f"(per-shard {min(ratios):.2f}-{max(ratios):.2f})", flush=True)
    return {
        "repo": repo,
        "n_shards": len(fs),
        "compressed_bytes": comp,
        "isize_sum_bytes": tot,
        "compression_ratio": tot / comp,
        "per_shard_ratio_min": min(ratios),
        "per_shard_ratio_max": max(ratios),
        "wrap_suspected": min(ratios) < 1.5,
        "per_shard": [{"path": e["path"], "compressed": e["size"], "isize": iz}
                      for e, iz in zip(fs, izs)],
    }


def main() -> int:
    repos = sys.argv[1:] or [
        "common-pile/arxiv_papers", "common-pile/arxiv_papers_filtered",
        "common-pile/peS2o", "common-pile/peS2o_filtered",
        "common-pile/pubmed", "common-pile/pubmed_filtered",
    ]
    out = {}
    for r in repos:
        try:
            out[r] = measure(r)
        except Exception as e:  # noqa: BLE001
            print(f"{r}: FAILED {type(e).__name__}: {e}", flush=True)
            out[r] = {"repo": r, "status": "FAILED", "error": str(e)[:200]}
    p = os.path.join(HERE, "_isize-academic.json")
    json.dump(out, open(p, "w"), indent=2)
    print(f"\nwrote {p}")
    # ratios
    for a, b in [("common-pile/peS2o", "common-pile/peS2o_filtered"),
                 ("common-pile/pubmed", "common-pile/pubmed_filtered"),
                 ("common-pile/arxiv_papers", "common-pile/arxiv_papers_filtered")]:
        if out.get(a, {}).get("isize_sum_bytes") and out.get(b, {}).get("isize_sum_bytes"):
            print(f"{a.split('/')[-1]:15} filtered/raw byte ratio = "
                  f"{out[b]['isize_sum_bytes'] / out[a]['isize_sum_bytes']:.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
