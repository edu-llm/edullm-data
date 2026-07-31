"""The synthetic `id` partition and the edu-web anti-join — `DATASET-DESIGN-reservoir.md` §9.7 item 4.

This module exists because of a defect that no check in this pipeline can see. It is pure
(no AWS, no HTTP) so the arithmetic that decides the reservoir's synthetic half is testable
without a network, and so the ingest driver has nothing in it worth unit-testing separately.

WHAT GOES WRONG WITHOUT IT
--------------------------
`HuggingFaceFW/finephrase` publishes four configs — faq, math, table, tutorial — and the design
draws 15 B tokens from each as four independently-weightable `source` values. They are not four
corpora. They are ONE corpus rephrased four ways over the same ~339 M FineWeb-Edu documents,
measured at 91.0–92.9% pairwise id overlap (§3.3). Drawing 15 B from each yields ~15 B of
distinct documents wearing four hats.

Nothing downstream catches it:

- a content digest sees four DIFFERENT strings (four rephrasings), so exact dedup passes;
- MinHash at the usual threshold sees four differently-worded texts, so fuzzy dedup passes;
- every token count still adds up, so no sizing check fires.

That is the same mechanism §4.2 documents as "rephrasing is exactly what defeats n-gram
decontamination," turned inward on our own pool.

And there is a second, worse collision: FinePhrase is rephrased **FineWeb-Edu**, which §3.2 also
draws for edu-web, and the two share one id space. Untreated, one document can appear as real
edu-web text AND as its own rephrasing inside a single 20 B run.

THE BUILD-TIME DEADLINE
-----------------------
`id` never enters `manifest_sha256`, so this is not irreversible in the manifest sense. It is
irreversible in the BUILD sense: after tokenization there is no document→id mapping left (§9.7
item 3 deliberately declined to emit one), so redoing it means re-tokenizing the synthetic half.
Hence this runs at ingest, in the same pass that first reads the rows.

WHY `sha256(id) % 4` AND NOT `hash(id) % 4`
-------------------------------------------
Python's builtin `hash()` is salted per process (PYTHONHASHSEED), so it would assign the same
document to different formats on different workers and across retries — silently, since each run
looks internally consistent. `sha256` is stable across processes, machines, and Python versions,
which is what makes a partition computed on one Batch worker mean the same thing on another.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

__all__ = [
    "FINEPHRASE_FORMATS",
    "N_PARTITIONS",
    "IdPartitionError",
    "partition_of",
    "format_for_id",
    "keeps_id",
    "PartitionAudit",
    "audit_partition",
]

#: The four FinePhrase configs, in a FIXED order. This order IS the partition assignment —
#: `FINEPHRASE_FORMATS[partition_of(id)]` is the format that keeps that document — so reordering
#: this tuple silently reassigns every document in the corpus. It is not alphabetical by accident;
#: it is alphabetical so that there is one obvious canonical order and no temptation to "tidy" it.
#:
#: Changing this tuple after a build invalidates that build's synthetic half.
FINEPHRASE_FORMATS: tuple[str, ...] = ("faq", "math", "table", "tutorial")

#: Number of disjoint partitions. Deliberately equal to `len(FINEPHRASE_FORMATS)` rather than a
#: free parameter: the whole point is a bijection between partition and format, so a mismatch is
#: a bug rather than a configuration.
N_PARTITIONS = len(FINEPHRASE_FORMATS)


class IdPartitionError(ValueError):
    """A document id is unusable for partitioning, or an audit fails its own bar.

    Raised rather than returning a sentinel because every caller of this module is deciding
    permanent shard membership; a silently-skipped document is a silently-smaller pool.
    """


def _require_id(doc_id: str) -> str:
    """Reject what cannot be partitioned, instead of hashing it anyway.

    An empty or non-string id would hash fine (`sha256(b"")` is a perfectly good digest) and land
    every such document in one partition, which is exactly the kind of plausible-garbage outcome
    the golden rule exists to stop. FinePhrase ids are URN-shaped UUID strings; anything falsy is
    a read bug upstream of here, and it should surface as one.
    """
    if not isinstance(doc_id, str):
        raise IdPartitionError(
            f"document id must be str, got {type(doc_id).__name__} — a non-string id means the "
            f"`id` column was read wrong, not that this document is unusual"
        )
    if not doc_id:
        raise IdPartitionError(
            "document id is empty; every FinePhrase / FineWeb-Edu row carries a URN-shaped uuid, "
            "so an empty id means the column selection is wrong"
        )
    return doc_id


def partition_of(doc_id: str, n: int = N_PARTITIONS) -> int:
    """Which of `n` disjoint partitions `doc_id` belongs to. Stable across processes and hosts.

    Uses the FULL 256-bit digest as an integer, not a prefix. Taking `% n` of the low bytes only
    would also be uniform, but reading the whole digest costs nothing and removes a question a
    reader would otherwise have to reason about.
    """
    _require_id(doc_id)
    if n < 1:
        raise IdPartitionError(f"n must be >= 1, got {n}")
    return int.from_bytes(hashlib.sha256(doc_id.encode("utf-8")).digest(), "big") % n


def format_for_id(doc_id: str) -> str:
    """The one FinePhrase format that keeps `doc_id`. Every other format must drop it."""
    return FINEPHRASE_FORMATS[partition_of(doc_id)]


def keeps_id(fmt: str, doc_id: str) -> bool:
    """Whether config `fmt` keeps `doc_id` under the partition.

    This is the predicate the ingest pass applies per row. It rejects an unknown format rather
    than returning False, because a typo'd config name would otherwise drop 100% of its rows and
    report a successful ingest of an empty source.
    """
    if fmt not in FINEPHRASE_FORMATS:
        raise IdPartitionError(
            f"unknown FinePhrase format {fmt!r}; expected one of {FINEPHRASE_FORMATS}. A silent "
            f"False here would ingest zero rows and call it success"
        )
    return format_for_id(doc_id) == fmt


@dataclass
class PartitionAudit:
    """What a partition actually did to a real id sample, recomputed rather than assumed.

    `worst_deviation_pp` is the headline: the largest absolute deviation of any partition's share
    from the ideal `100/n` percent, in percentage points. The design's arithmetic needs each
    partition to hold >= 17.3% of its config (table, the worst case), against an ideal 25.0%, so
    the audit's job is to prove the realised split is nowhere near that floor.
    """

    n_ids: int
    n_partitions: int
    counts: dict[int, int] = field(default_factory=dict)

    @property
    def shares_pct(self) -> dict[int, float]:
        if not self.n_ids:
            return {}
        return {k: 100.0 * v / self.n_ids for k, v in sorted(self.counts.items())}

    @property
    def ideal_pct(self) -> float:
        return 100.0 / self.n_partitions

    @property
    def worst_deviation_pp(self) -> float:
        if not self.n_ids:
            return 0.0
        return max(abs(s - self.ideal_pct) for s in self.shares_pct.values())

    @property
    def min_share_pct(self) -> float:
        if not self.n_ids:
            return 0.0
        return min(self.shares_pct.values())

    def to_dict(self) -> dict:
        return {
            "n_ids": self.n_ids,
            "n_partitions": self.n_partitions,
            "counts": dict(sorted(self.counts.items())),
            "shares_pct": {k: round(v, 4) for k, v in self.shares_pct.items()},
            "ideal_pct": round(self.ideal_pct, 4),
            "worst_deviation_pp": round(self.worst_deviation_pp, 4),
            "min_share_pct": round(self.min_share_pct, 4),
        }


def audit_partition(
    ids: list[str],
    *,
    n: int = N_PARTITIONS,
    required_min_share_pct: float | None = None,
) -> PartitionAudit:
    """Recompute the realised partition split over `ids`, and optionally enforce a floor.

    `required_min_share_pct` is the design's real requirement, not a stylistic one: the table
    config needs 17.3% of its rows to reach 15 B tokens (§9.7 item 4), so an ingest run should
    pass its own worst-case floor here and fail loudly if the realised split cannot deliver it.

    Deduplicates before counting. An id appearing twice in the sample (the `raw_v0.1_parquet`
    two-document-tree trap in `artifacts/sizing-revised.md` is exactly this shape) would
    otherwise weight one partition by a multiplicity that says nothing about the corpus.
    """
    distinct = sorted(set(ids))
    counts: dict[int, int] = {k: 0 for k in range(n)}
    for doc_id in distinct:
        counts[partition_of(doc_id, n)] += 1
    audit = PartitionAudit(n_ids=len(distinct), n_partitions=n, counts=counts)
    if required_min_share_pct is not None and audit.n_ids:
        if audit.min_share_pct < required_min_share_pct:
            raise IdPartitionError(
                f"realised partition floor {audit.min_share_pct:.3f}% is below the required "
                f"{required_min_share_pct:.3f}% over {audit.n_ids:,} distinct ids "
                f"(shares: {audit.to_dict()['shares_pct']}). The design's 15 B-per-format draw "
                f"does not fit — do not tokenize against this partition"
            )
    return audit
