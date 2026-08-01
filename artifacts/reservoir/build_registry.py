"""Emit `corpus-registry.json` — one row per source the reservoir build will read.

WHY THIS IS A SCRIPT AND NOT A HAND-WRITTEN JSON FILE. Every number in the registry has to carry its
provenance (§3.1: card token counts are not comparable — most name no tokenizer, and every Common Pile
"token" figure is `Size(GB) x 0.25`, pure arithmetic). A hand-written file would let a card figure and
a footer-exact measurement sit in adjacent fields looking identical. Here the measured values are
*read from* `artifacts/recount/` and `artifacts/sizing-revised.md`, so a row that claims a measurement
either points at the artifact that made it or is explicitly `null`.

Run: `python3 artifacts/reservoir/build_registry.py`
Verifies: every row loads into `CorpusSpec`, so a registry that would crash the reader fails here.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from edullm_data.corpus import CorpusSpec  # noqa: E402

#: Text columns CONFIRMED against real bytes, with the artifact that confirmed each. Anything not in
#: here is `"UNVERIFIED"` in the output — the distinction is the point of the file.
#:
#: `finephrase` is the dangerous one and the only non-trivial path: the rewrite is at
#: `rollout_results.list.element.text` while top-level `text` holds the ORIGINAL FineWeb-Edu document
#: (`artifacts/reservoir/id-partition-verification.json` -> `rewrite_leaf`). Re-verified through the
#: finished reader: the correct selector returns REWRITE, `rollout_results.text` and the legacy
#: `list.item` spelling are REJECTED, and plain `text` returns ORIGINAL.
VERIFIED_TEXT_COLUMN = {
    "peS2o_filtered": ("text", "artifacts/recount/_footer-academic-peS2o.json"),
    "pubmed_filtered": ("text", "artifacts/recount/_footer-academic-pubmed.json"),
    "arxiv_papers_filtered": ("text", "artifacts/recount/_footer-academic-arxiv_papers.json"),
    "finepdfs-edu": ("text", "artifacts/recount/_footer-finepdfs-edu.json"),
    "essential-web": ("text", "artifacts/recount/edu-web-essential-web.json"),
    "finephrase": (
        "rollout_results.list.element.text",
        "artifacts/reservoir/id-partition-verification.json",
    ),
}

#: Measured pool sizes from `artifacts/sizing-revised.md`'s table, which states the basis per row.
#: These are dolma2 re-counts, NOT card figures.
MEASURED = {
    "finepdfs-edu": 161_100_000_000,
    "fineweb-edu": 100_240_000_000,
    "dclm-baseline": 114_690_000_000,
    "finemath": 34_690_000_000,
    "peS2o_filtered": 40_480_000_000,
    "pubmed_filtered": 37_540_000_000,
    "arxiv_papers_filtered": 6_230_000_000,
    "stackv2_edu_filtered": 74_810_000_000,
    "stackexchange_filtered": 24_050_000_000,
    "ubuntu_irc_filtered": 1_870_000_000,
    "github_archive_filtered": 11_510_000_000,
    "finewiki": 8_870_000_000,
    "finephrase-faq": 148_540_000_000,
    "finephrase-tutorial": 147_920_000_000,
    "finephrase-math": 94_740_000_000,
    "finephrase-table": 86_950_000_000,
}

_CP = "common-pile/raw_v0.1_parquet"


#: Category pools where summing the rows OVERSTATES what is available, because the sources are not
#: independent (§3.1's lineage trap). Values from `artifacts/sizing-revised.md`, which states the
#: basis per row. Absent categories are genuinely additive.
#:
#: This is the one place a registry built by summing rows would lie, so the naive sum is kept
#: alongside and labelled rather than silently replaced.
NON_OVERLAPPING = {
    # 64.12 B, not 84.26 B: peS2o's MEASURED 49.7% PMC byte share duplicates pubmed.
    "academic": 64_120_000_000,
    # finemath alone. swallow-math-v2 (32 B) is a rewrite of it and github_archive is not math.
    "math": 34_690_000_000,
}

#: Why the two numbers differ, carried into the JSON so a reader does not have to find this file.
POOL_NOTES = {
    "academic": (
        "naive sum 84.26 B overstates by 20.1 B: 49.7% of peS2o's BYTES are PubMedCentral-derived "
        "(measured from per-document metadata.pdf_src). The same article extracted by "
        "Grobid-over-PDF and by pandoc-over-nXML differs in >10% of its 20-grams and survives fuzzy "
        "dedup as DISTINCT, so a digest never catches it and neither does MinHash at the usual "
        "threshold. Drop peS2o's PMC share rather than dedup it."
    ),
    "math": (
        "the category has exactly ONE independent source. Anything that would raise the total is a "
        "rewrite of finemath with measured self-similarity too low for dedup to catch."
    ),
}


def _row(**kw) -> dict:
    """One registry row, with `text_column` resolved from the verified table."""
    key = kw.pop("_verify_key", kw["key"])
    col, src = VERIFIED_TEXT_COLUMN.get(key, ("UNVERIFIED", None))
    kw["text_column"] = kw.pop("text_column", col)
    traps = list(kw.pop("traps", ()))
    if kw["text_column"] == "UNVERIFIED":
        traps.insert(
            0,
            "text_column NOT confirmed from bytes. Settle it with: python3 -c \"import "
            f"pyarrow.parquet as pq; pf=pq.ParquetFile(<one file of {kw['repo']}>); "
            "rg=pf.metadata.row_group(0); print([rg.column(i).path_in_schema for i in "
            'range(rg.num_columns)])" — and match the EXACT path_in_schema, never a top-level name.',
        )
    elif src:
        traps.append(f"text_column verified in {src}")
    kw["traps"] = tuple(traps)
    return kw


ROWS = [
    # ---- edu-web: 48 B pool, measured 261.3 B (7.0x) ----
    _row(
        key="finepdfs-edu", category="edu-web", source_label="finepdfs-edu",
        repo="HuggingFaceFW/finepdfs-edu", config="eng_Latn", file_format="parquet",
        id_column="UNVERIFIED", target_tokens=28_000_000_000,
        pool_tokens=MEASURED["finepdfs-edu"], license="ODC-BY-1.0",
        traps=("PDF-extracted text; extraction artifacts differ in kind from CC-HTML sources.",),
    ),
    _row(
        key="fineweb-edu", category="edu-web", source_label="fineweb-edu",
        repo="HuggingFaceFW/fineweb-edu", config="sample-100BT", file_format="parquet",
        id_column="UNVERIFIED", target_tokens=20_000_000_000,
        pool_tokens=MEASURED["fineweb-edu"], license="ODC-BY-1.0",
        traps=(
            "🛑 100% of this source has a synthetic sibling: sample-100BT ⊂ sample-350BT, which is "
            "FinePhrase's exact parent. Draw synthetic from the ~242M sample-350BT ids NOT in "
            "sample-100BT, or every edu-web document appears twice in one run (HANDOFF 'three "
            "irreversible decisions', item 3).",
            "Zero decontamination upstream (§4.2).",
        ),
    ),
    _row(
        key="essential-web", category="edu-web", source_label="essential-web",
        repo="EssentialAI/essential-web-v1.0", config="default", file_format="parquet",
        id_column="UNVERIFIED", target_tokens=0, pool_tokens=None, license="ODC-BY-1.0",
        domain_column="eai_taxonomy.free_decimal_correspondence",
        traps=(
            "target_tokens 0 = RESERVE, not in the v1 draw: the pool is already met 7.0x. Listed "
            "because it is the one source that SHIPS the subject label the cancelled ~$920 "
            "classification run would have computed (§1.2).",
            "Shares 89 of 101 snapshots with DCLM-Pool (§3.1) — do not treat as independent.",
        ),
    ),
    # ---- web (diverse): 30 B pool, measured 114.69 B (5.5x) ----
    _row(
        key="dclm-baseline", category="web-diverse", source_label="dclm",
        repo="mlfoundations/dclm-baseline-1.0", config="default", file_format="parquet",
        id_column="UNVERIFIED", target_tokens=30_000_000_000,
        pool_tokens=MEASURED["dclm-baseline"], license="CC-BY-4.0",
        traps=(
            "The diversity counterweight to edu filtering — the point is that it is NOT filtered.",
            "olmo-mix-1124 is ~95% DCLM-baseline (§3.1), so this overlaps the published corpora.",
        ),
    ),
    # ---- math: 36 B pool, measured 34.69 B (pool short 3.6%, 3x peak still met) ----
    _row(
        key="finemath", category="math", source_label="finemath",
        repo="HuggingFaceTB/finemath", config="finemath-3plus", file_format="parquet",
        id_column="UNVERIFIED", target_tokens=34_000_000_000,
        pool_tokens=MEASURED["finemath"], license="ODC-BY-1.0",
        traps=(
            "⚠️ The ONLY math source: everything else in the category is a rewrite of it. "
            "swallow-math-v2 (32 B, Apache-2.0) is a FineMath rewrite; measured self-similarity "
            "means no digest or MinHash catches the duplication.",
            "Pool is 3.6% under the 36 B plan. 3x peak demand IS met (4.96x).",
            "NOT inside MegaMath — independent CC re-extractions (§3.1).",
        ),
    ),
    # ---- academic: 20 B pool, measured 64.12 B non-overlapping (3.2x) ----
    _row(
        key="peS2o_filtered", category="academic", source_label="pes2o",
        repo=_CP, config="peS2o_filtered", file_format="parquet",
        id_column="UNVERIFIED", target_tokens=14_000_000_000,
        pool_tokens=MEASURED["peS2o_filtered"], license="CC-BY / CC0 (mixed)", share_alike=True,
        traps=(
            "🛑 49.7% of its BYTES are PubMedCentral-derived (measured from per-document "
            "metadata.pdf_src, artifacts/recount/_overlap-pes2o-pmc.json). The same article via "
            "Grobid-over-PDF and pandoc-over-nXML differs in >10% of 20-grams and SURVIVES fuzzy "
            "dedup as distinct. Drop the PMC share rather than dedup it, or take pubmed instead.",
            "≈1.9% share-alike and INVISIBLE from repo metadata (Common Pile paper Table 3). "
            "Name-level SA exclusion therefore drops 100% of this source to remove 1.9%.",
            "Card says 43.3 B — 7% HIGH vs the footer-exact 40.48 B.",
        ),
    ),
    _row(
        key="pubmed_filtered", category="academic", source_label="pubmed",
        repo=_CP, config="pubmed_filtered", file_format="parquet",
        id_column="UNVERIFIED", target_tokens=6_000_000_000,
        pool_tokens=MEASURED["pubmed_filtered"], license="CC-BY / CC0 (mixed)",
        traps=("Overlaps peS2o's PMC share — see the peS2o row. Count one, not both.",),
    ),
    _row(
        key="arxiv_papers_filtered", category="academic", source_label="arxiv",
        repo=_CP, config="arxiv_papers_filtered", file_format="parquet",
        id_column="UNVERIFIED", target_tokens=0,
        pool_tokens=MEASURED["arxiv_papers_filtered"], license="CC-BY / CC0 (mixed)",
        traps=(
            "target_tokens 0 = reserve; the 20 B academic pool is met by peS2o + pubmed. DoReMi "
            "drove ArXiv 10.52% -> 0.36% and RegMix put it near zero (§2.1).",
        ),
    ),
    # ---- code: 40 B pool, measured 74.81 B from ONE source (1.87x) ----
    _row(
        key="stackv2_edu_filtered", category="code", source_label="stackv2-edu",
        repo=_CP, config="stackv2_edu_filtered", file_format="parquet",
        id_column="UNVERIFIED", target_tokens=40_000_000_000,
        pool_tokens=MEASURED["stackv2_edu_filtered"], license="Blue Oak (100% permissive per-doc)",
        domain_column="metadata.gha_language",
        traps=(
            "⚠️ 73 distinct languages in ONE shard. Cardinality is PERMANENT — each becomes a "
            "directory inside manifest_sha256. Fold to the top ~20 by token count, rest -> 'other' "
            "(§1.2), and pass the map to the reader: it is a reader ARGUMENT, not a spec field.",
            "🛑 SLUG THE VALUE. 'C#' publishes clean and passes Gate A, then urlparse puts "
            "everything after the '#' in `fragment` and THE SHARD NAME LEAVES THE PATH. Gate A now "
            "rejects '#' and brackets (validate._segment_breakage); other values still need "
            "slug_path_segment. 'C#'->'c-sharp', 'C++'->'c-plus-plus'.",
            "Use this, NOT the-stack-v2/stack-edu: those ship SWHIDs only and bulk access needs a "
            "Software Heritage agreement demanding open model release (§7 item 3).",
            "Excludes swallow-code-v2 (a Python-only rewrite of the same blobs, self-similarity "
            "0.064 so nothing catches it; 74% no_license upstream despite the apache-2.0 tag).",
        ),
    ),
    # ---- QA/forum: 12 B pool, measured 25.93 B (2.16x) — 92.8% share-alike ----
    _row(
        key="stackexchange_filtered", category="qa-forum", source_label="stackexchange",
        repo=_CP, config="stackexchange_filtered", file_format="parquet",
        id_column="UNVERIFIED", target_tokens=10_000_000_000,
        pool_tokens=MEASURED["stackexchange_filtered"], license="CC-BY-SA-4.0", share_alike=True,
        domain_column="metadata.site",
        traps=(
            "100% share-alike, and the license is visible ONLY in per-row metadata.all_licenses — "
            "cardData declares none. A declared license both over- and under-states what is inside.",
            "~180 sites; same permanent-cardinality fold as stackv2-edu. Slug: "
            "'3dprinting.stackexchange.com' -> '3dprinting'.",
        ),
    ),
    _row(
        key="ubuntu_irc_filtered", category="qa-forum", source_label="ubuntu-irc",
        repo=_CP, config="ubuntu_irc_filtered", file_format="parquet",
        id_column="UNVERIFIED", target_tokens=1_800_000_000,
        pool_tokens=MEASURED["ubuntu_irc_filtered"], license="Public Domain",
        traps=(
            "The ONLY non-share-alike source in this category. Without it the category is 1.87 B, "
            "which fails even peak demand once SA is excluded.",
            "IRC logs: short turns. Watch corpus.MIN_DOC_TOKENS — a mean under 20 tokens per "
            "document means EOS fraction > 0.05 and Gate A rejects the shard.",
        ),
    ),
    _row(
        key="github_archive_filtered", category="qa-forum", source_label="github-archive",
        repo=_CP, config="github_archive_filtered", file_format="parquet",
        id_column="UNVERIFIED", target_tokens=0,
        pool_tokens=MEASURED["github_archive_filtered"], license="permissive",
        traps=(
            "Moved here FROM code deliberately: it is issue/PR prose, not code (§3.2).",
            "target_tokens 0 = reserve; the 12 B pool is met without it.",
        ),
    ),
    # ---- reference: 9 B pool (owner-revised from 14 B), measured 8.87 B (3.70x) ----
    _row(
        key="finewiki", category="reference", source_label="finewiki",
        repo="HuggingFaceFW/finewiki", config="en", file_format="parquet",
        id_column="UNVERIFIED", target_tokens=8_800_000_000,
        pool_tokens=MEASURED["finewiki"], license="CC-BY-SA-4.0 AND GFDL", share_alike=True,
        traps=(
            "100% share-alike, and TWO different copyleft regimes (CC-BY-SA + GFDL). Do not model "
            "SA as a boolean — record the license string (§1.5).",
            "Pool was revised 14 B -> 9 B and max share 15% -> 12%: at 8.87 B, 15% gives only "
            "2.96x headroom and 15% would have been the one row violating §2.1's own 3x rule.",
            "The card names NO token count and NO tokenizer; 8.87 B is footer-exact measurement.",
        ),
    ),
]

# ---- synthetic: 60 B as 4 separately-weightable formats, measured 478.15 B (8.0x) ----
_FP_NEED = {"faq": 10.1, "tutorial": 10.1, "math": 15.8, "table": 17.3}
for i, fmt in enumerate(("faq", "math", "table", "tutorial")):
    ROWS.append(_row(
        key=f"finephrase-{fmt}", _verify_key="finephrase", category="synthetic",
        source_label=f"synthetic-finephrase-{fmt}", repo="HuggingFaceFW/finephrase", config=fmt,
        file_format="parquet", id_column="id", target_tokens=15_000_000_000,
        pool_tokens=MEASURED[f"finephrase-{fmt}"], license="ODC-BY-1.0",
        traps=(
            "🛑 THE REWRITE IS AT rollout_results.list.element.text. Top-level `text` holds the "
            "ORIGINAL FineWeb-Edu document (its `dataset` field literally reads "
            "HuggingFaceFW/fineweb-edu). A flat leaf list contains `text` TWICE and "
            ".names.index('text') returns the ORIGINAL — building from it yields a corpus of "
            "unrephrased FineWeb-Edu labelled synthetic, and NO hash, size or decode check catches "
            "it. Worse: rollout_results.text does not raise either, it returns ZERO columns.",
            f"🛑 The four formats are ~91-93% THE SAME DOCUMENTS. Apply the §9.7 item 4 partition "
            f"(sha256(id) % 4 == {i}) BEFORE tokenizing — after tokenization there is no "
            f"document->id mapping and it cannot be retrofitted. This format needs "
            f"{_FP_NEED[fmt]}% of its pool; a disjoint quarter gives 25.0% "
            f"(measured 24.86-25.26%).",
            "No upstream quality control: a sampled rewrite was the entire string 'Question: Can "
            "light accelerate to the speed of light?' (~12 tokens). corpus.MIN_DOC_TOKENS filtering "
            "is what makes this half publishable — a mean under 20 tokens fails the EOS bound.",
            "Rephrased FineWeb-Edu, which does zero decontamination, and rephrasing is exactly what "
            "defeats n-gram decontamination (§4.2). The eval bundle does NOT cover this.",
        ),
    ))


def _categories(by_cat: dict[str, list]) -> dict:
    """Per-category rollup, reporting the naive sum AND the non-overlapping figure side by side.

    Both, never one: the naive sum is what the rows literally say and is the number someone will
    recompute and expect to match, while the non-overlapping figure is what is actually available.
    Silently substituting the latter would make the arithmetic unreproducible from the rows.
    """
    out: dict[str, dict] = {}
    for cat, rows in sorted(by_cat.items()):
        naive = sum(s.pool_tokens or 0 for s in rows)
        entry = {
            "target_tokens": sum(s.target_tokens for s in rows),
            "naive_sum_pool_tokens": naive,
            "non_overlapping_pool_tokens": NON_OVERLAPPING.get(cat, naive),
            "sources": [s.key for s in rows],
        }
        if cat in POOL_NOTES:
            entry["_pool_note"] = POOL_NOTES[cat]
        out[cat] = entry
    return out


def main() -> int:
    specs = [CorpusSpec(**{k: v for k, v in r.items()}) for r in ROWS]  # the real validation
    by_cat: dict[str, list] = {}
    for s in specs:
        by_cat.setdefault(s.category, []).append(s)

    doc = {
        "_schema": "edullm-corpus-registry/v1",
        "_generated_by": "artifacts/reservoir/build_registry.py",
        "_tokenizer": "allenai/dolma2-tokenizer (published as tokenizer/dolma2-bpe/v1)",
        "_note": (
            "target_tokens 0 means RESERVE — listed deliberately, not drawn in v1. "
            "text_column 'UNVERIFIED' means not confirmed from real bytes; the row's first trap "
            "carries the command that settles it. pool_tokens are dolma2 re-counts from "
            "artifacts/recount + artifacts/sizing-revised.md, never card figures (§3.1)."
        ),
        "categories": _categories(by_cat),
        "corpora": ROWS,
    }
    out = ROOT / "artifacts" / "reservoir" / "corpus-registry.json"
    out.write_text(json.dumps(doc, indent=1) + "\n")

    ver = sum(1 for r in ROWS if r["text_column"] != "UNVERIFIED")
    drawn = [s for s in specs if s.target_tokens > 0]
    print(f"wrote {out.relative_to(ROOT)}")
    print(f"{len(ROWS)} corpora | text_column verified {ver}, UNVERIFIED {len(ROWS) - ver}")
    print(f"drawn in v1: {len(drawn)} | reserve: {len(ROWS) - len(drawn)}")
    print(f"total target {sum(s.target_tokens for s in specs) / 1e9:.1f}B "
          f"of measured pool {sum(s.pool_tokens or 0 for s in specs) / 1e9:.1f}B")
    cats = _categories(by_cat)
    print(f"{'category':14} {'target':>9} {'non-overlap':>12} {'x':>6}  note")
    tight = []
    for cat, e in cats.items():
        t, p = e["target_tokens"], e["non_overlapping_pool_tokens"]
        ratio = p / t if t else 0
        flag = "  <- overlap-adjusted" if cat in NON_OVERLAPPING else ""
        print(f"{cat:14} {t / 1e9:8.1f}B {p / 1e9:11.1f}B {ratio:5.2f}x{flag}")
        if t and ratio < 1.05:
            tight.append((cat, ratio))
    if tight:
        # Not a failure, and specifically NOT a decontamination problem — see the note below. §2.1
        # sizes pools for 3x PEAK demand and the default draw is far under peak; what these
        # categories cannot absorb is a *sourcing* surprise (a corpus that re-counts lower under
        # dolma2, or a source dropped on license grounds).
        print("\n⚠️  no slack (target within 5% of the available pool):")
        for cat, ratio in tight:
            print(f"      {cat}: {ratio:.3f}x")
        print("    Decontamination is NOT the threat here. The only real measurement we have "
              "(datamix1's leakage-summary.json) excluded 10,239 documents from a whole 20B build "
              "= ~0.026% of tokens. `category_attrition` in that file reads like a pool fraction "
              "(math 54.3%, code 79.7%) but is excluded/CANDIDATES within a category — 3,926 "
              "candidate documents for math, not 34.69B tokens. Reading it as a pool fraction "
              "overstates the loss by ~4 orders of magnitude.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
