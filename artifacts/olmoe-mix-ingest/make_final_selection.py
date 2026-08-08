#!/usr/bin/env python3
"""Deterministically select the shards for pretrain/olmoe-mix-50b and -100b.

Owner decisions encoded here:
  * source is OLMoE-mix-0824 (pre-tokenized, uint32 LE, dolma2) -- copy+rename, no tokenization
  * web (dclm) is deliberately held to ~75%, which UNDERWEIGHTS it against upstream's 95.12%,
    to admit whole shards of the smaller, higher-quality sources
  * the 50B train set is a strict SUBSET of the 100B train set (nested, so the only difference
    between the two releases is the added tokens)
  * two independent releases; neither declares the other via depends_on, so the
    `shared-sha-with-parent` gate (validate.py:759) never fires

The val carve is DELIBERATE, not "the next unused shard". Several upstream shards are
anomalously tiny (arxiv part-01 is 0.0624 Gtok, owm part-01 is 0.0428, stack part-03 is 0.0951).
An earlier version of this script took the next unused shard in sorted order and handed the 50B
release a 0.0624 Gtok val split -- 0.125% of the release, too thin to validate anything. So we
require a val shard > 1.0 Gtok, drawn from a source object used in NO train slice of EITHER
release (never a byte-copy of a train shard: `duplicate-shard-digest`, and the 150B corpus's
100%-leakage incident).
"""

import json
import pathlib
from collections import OrderedDict, defaultdict

HERE = pathlib.Path(__file__).parent
GIB = 1024**3

# non-web shard counts per release; 100B must be >= 50B elementwise or nesting breaks
NW_50 = {
    "starcoder": 3,
    "pes2o": 1,
    "proofpile-2-arxiv": 1,
    "proofpile-2-open-web-math": 1,
    "proofpile-2-stack": 2,
    "wikipedia": 2,
}
NW_100 = {
    "starcoder": 6,
    "pes2o": 4,
    "proofpile-2-arxiv": 2,
    "proofpile-2-open-web-math": 2,
    "proofpile-2-stack": 2,
    "wikipedia": 2,
}
VAL_MIN_TOKENS = 1.0e9


def load():
    rows = json.loads((HERE / "olmoe_0824_sizes.json").read_text())
    by = defaultdict(list)
    for label, key, status, size in rows:
        assert status == 200, (status, key)
        by[label].append((key, size))
    for label in by:
        by[label].sort()  # deterministic order = sorted by key
    return by


def build(by, target_tokens, nonweb):
    """Whole shards only. Non-web counts are fixed; dclm fills the remaining budget."""
    sel = OrderedDict()
    used = 0.0
    nonweb_labels = sorted(
        (l for l in by if l != "dclm"), key=lambda l: -sum(s for _, s in by[l])
    )
    for label in nonweb_labels:
        n = min(nonweb[label], len(by[label]))
        picked = [(k, s) for k, s in by[label][:n]]
        sel[label] = picked
        used += sum(s for _, s in picked) / 4
    per_dclm = by["dclm"][0][1] / 4
    n_dclm = max(1, round((target_tokens - used) / per_dclm))
    sel["dclm"] = [(k, s) for k, s in by["dclm"][:n_dclm]]
    return sel


def main():
    by = load()
    assert all(NW_100[k] >= NW_50[k] for k in NW_50), "nesting violated by the counts"

    sel = {"50b": build(by, 50e9, NW_50), "100b": build(by, 100e9, NW_100)}
    train_all = {k for s in sel.values() for v in s.values() for k, _ in v}

    # val candidates: arxiv, comfortably sized, outside EVERY train slice of BOTH releases
    val_pool = [
        (k, s)
        for k, s in by["proofpile-2-arxiv"]
        if k not in train_all and s / 4 > VAL_MIN_TOKENS
    ]
    assert len(val_pool) >= 2, f"need 2 val shards, found {len(val_pool)}"
    val = {"50b": val_pool[0], "100b": val_pool[1]}

    out = {}
    for name in ("50b", "100b"):
        groups = sel[name]
        vkey, vsize = val[name]
        tb = sum(s for v in groups.values() for _, s in v)
        tt = tb / 4
        web = sum(s for _, s in groups["dclm"]) / 4
        n = sum(len(v) for v in groups.values())
        print(f"=== {name.upper()}: {n} train + 1 val, WEB {100 * web / tt:.2f}% ===")
        for label in sorted(groups, key=lambda l: -sum(s for _, s in groups[l])):
            st = sum(s for _, s in groups[label]) / 4
            print(
                f"    {label:26s} {len(groups[label]):3d}/{len(by[label]):<4d} "
                f"{st / 1e9:8.3f} Gtok {100 * st / tt:6.2f}%"
            )
        print(
            f"    val: arxiv {vkey.split('/')[-1]} = {vsize / 4 / 1e9:.4f} Gtok "
            f"= {100 * (vsize / 4) / (tt + vsize / 4):.3f}% of release"
        )
        print(
            f"    TOTAL {n + 1} objects, {(tb + vsize) / GIB:.1f} GiB, "
            f"{(tt + vsize / 4) / 1e9:.3f} Gtok"
        )
        print()
        out[name] = {
            "train": {l: [k for k, _ in v] for l, v in groups.items()},
            "val": {"proofpile-2-arxiv": [vkey]},
            "realized": {
                "train_shards": n,
                "train_tokens": int(tt),
                "train_bytes": tb,
                "val_tokens": int(vsize / 4),
                "val_bytes": vsize,
                "total_tokens": int(tt + vsize / 4),
                "total_bytes": tb + vsize,
                "web_fraction_of_train": web / tt,
                "shares_of_train": {
                    l: sum(s for _, s in v) / 4 / tt for l, v in groups.items()
                },
            },
        }
        (HERE / f"olmoe_final_{name}.json").write_text(json.dumps(out[name], indent=1))

    ka = {k for v in out["50b"]["train"].values() for k in v}
    kb = {k for v in out["100b"]["train"].values() for k in v}
    va = out["50b"]["val"]["proofpile-2-arxiv"][0]
    vb = out["100b"]["val"]["proofpile-2-arxiv"][0]
    size = {k: s for v in by.values() for k, s in v}
    everything = ka | kb | {va, vb}

    print("CHECKS")
    checks = {
        "50B train is a strict subset of 100B train": ka < kb,
        "50B val appears in no train slice": va not in (ka | kb),
        "100B val appears in no train slice": vb not in (ka | kb),
        "the two val shards differ": va != vb,
        "every selected shard is a whole multiple of 4 bytes": all(
            size[k] % 4 == 0 for k in everything
        ),
        "both val splits exceed 1 Gtok": min(size[va], size[vb]) / 4 > VAL_MIN_TOKENS,
    }
    for label, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    assert all(checks.values()), "a structural check failed"
    print(f"\n  distinct objects to fetch: {len(everything)}")
    print(f"  bytes to transfer once   : {sum(size[k] for k in everything) / GIB:.1f} GiB")


if __name__ == "__main__":
    main()
