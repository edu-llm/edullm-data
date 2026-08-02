"""Batch build driver for `pretrain/reservoir-dolma2` — §5.6 phase 1, stages 1-4 wired together.

`corpus.py` defines the contract, `corpus_read.py` yields documents, `corpus_pack.py` emits shards.
None of them touches S3, knows what a Batch array is, or decides anything an operator should own.
This module is the one that does: it turns the 17-row registry into a *plan*, splits that plan
across ~420 array children, runs read → carve → filter → tokenize → pack per child, uploads each
shard to landing, and writes a receipt that a later run can trust enough to skip work.

Everything here exists because of one of four failures.

WHY A PLAN ARTIFACT, WRITTEN ONCE, BEFORE ANY CHILD STARTS
----------------------------------------------------------
Shard ordinals are five digits inside the object key, the key is inside `entry.path`, and
`entry.path` is hashed into `manifest_sha256` (`corpus.py:9-17`). Two children that each count from
zero produce `tokens/dclm/train-00000` and `tokens/finewiki/train-00000`, both of which parse fine
and neither of which any gate rejects. So ordinals come from `corpus.allocate_ordinals` over the
WHOLE plan, computed once, serialized, and read back by every child. A child never allocates.

The plan is also the unit of reproducibility: `plan_document()` is a pure function of the registry
plus a handful of scalars, so the same registry in gives byte-identical JSON out, and its
`plan_id` is the sha256 of those bytes. A receipt that names a different `plan_id` is not evidence
about this build.

WHY RESUME IS AT BUNDLE GRANULARITY, AND WHY THE RECEIPT ALONE IS NOT ENOUGH
----------------------------------------------------------------------------
A stream's shards are cut from one carry buffer that spans documents (`corpus_pack.pack`), so shard
*k* cannot be reproduced without re-reading every document before it. There is no cheaper unit than
"the whole bundle", and pretending otherwise would mean re-reading the input to skip the output.

⚠️ **The trap is checking the receipt and stopping there.** A worker that uploads three of four
shards, writes its receipt, and then dies leaves a bundle that every later run declares finished —
and the corpus is short one shard, discovered at training time when OLMo-core's instance count comes
up wrong. `week1_corpus/task_runtime.py:42-48` guards the same thing the same way
(`any(... not store.exists(value) ...)`), and it is the only part of that module's resume logic
worth copying. So :func:`bundle_is_done` re-HEADs every key the receipt names and compares the size
S3 reports against the size the receipt claims. A receipt without its shards is not done.

Sizes, not just existence, because a truncated multipart or an interrupted PUT leaves a key that
exists at the wrong length; `head` costs the same either way, so checking only presence would be
strictly weaker for free. It is NOT a re-hash: `s3.hash_object` re-reads the whole payload, which at
~100 MB per shard × ~10,400 shards is a second full pass over the corpus on every resume. See
`verify --deep` for the opt-in that does pay that cost.

WHY `verify` REFUSES RATHER THAN REPORTS
-----------------------------------------
A missing bundle is not a smaller corpus, it is a *wrong* one: the mixture weights in the README
still name the source, the token counts still add up within each bundle, and nothing objects. This
is the same shape as `ingest_reservoir._cmd_merge`'s incomplete-part refusal
(`ingest_reservoir.py:916-923`) and as the anti-join it protects. So `verify` is a gate, not a
report, and it exits non-zero on the first missing bundle it can name.

WHERE IT RUNS, AND WHAT IT MAY WRITE
-------------------------------------
Batch, in-region (§5.7): `publish()` and this driver both PULL bytes to wherever they run, and the
measured laptop throughput of 0.8 MiB/s makes a 255B-token tokenize a multi-week transfer. `run`
refuses to start without `AWS_BATCH_JOB_ID`. That guard is a fail-fast, not an authorization
boundary — the boundary is IAM, and this driver's role cannot write `s3://edullm-data` at all. It
writes shards and receipts to `s3://edullm-landing` and nothing else, and `_assert_safe_key` refuses
the basenames that would fire `edullm-landing-manifest-created` against a build artifact
(`ingest_reservoir.py:16-21`).
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sys
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .corpus import (
    GROUP,
    MIN_DOC_TOKENS,
    SHARD_TOKENS,
    TOKENIZER_DATASET_ID,
    VAL_FRACTION,
    BuildError,
    CorpusSpec,
    Document,
    ShardRef,
    allocate_ordinals,
    carve,
)
from .ingest_reservoir import (
    IngestError,
    _assert_lifecycle_covers,
    _assert_safe_key,
    _require_batch,
    _shard_slice,
)

__all__ = [
    "DEFAULT_BUCKET",
    "DEFAULT_PREFIX",
    "REGISTRY_PATH",
    "BuildDriverError",
    "Bundle",
    "bundle_is_done",
    "load_registry",
    "main",
    "plan_document",
    "receipt_key",
    "run_bundle",
]


class BuildDriverError(BuildError):
    """A driver-level precondition failed, or a receipt did not survive verification."""


DEFAULT_BUCKET = "edullm-landing"
DEFAULT_PREFIX = "_ingest/reservoir-dolma2/build"
REGISTRY_PATH = "artifacts/reservoir/corpus-registry.json"


#: Formats `corpus_read` can actually consume. A registry row naming anything else is a
#: PLAN-TIME error, not a run-time one: discovering it mid-run means other bundles have already
#: been built and paid for, and the corpus quietly lacks a whole category.
#:
#: This fired for real. `dclm-baseline` pointed at `mlfoundations/dclm-baseline-1.0`, which ships
#: `.jsonl.zst` while the row claimed `parquet` — a 30B hole. It was resolved by re-sourcing to
#: `HuggingFaceFW/dclm_100BT` (parquet, and where the pool measurement came from anyway), not by
#: adding a zstd dependency, so every drawn source is readable today.
READABLE_FORMATS = frozenset({"parquet", "json.gz"})


def load_registry(path: str | None = None) -> tuple[list[CorpusSpec], dict[str, Any]]:
    """Registry rows as :class:`~.corpus.CorpusSpec`, plus the file's non-row metadata.

    Reserve rows (``target_tokens == 0``) are returned like any other: dropping them here would
    hide them from ``plan --show-reserve``, and the caller that builds the plan is the one that
    should decide what to skip.
    """
    p = path or str(_repo_root() / REGISTRY_PATH)
    try:
        with open(p, "rb") as fh:
            doc = json.load(fh)
    except OSError as exc:
        raise BuildDriverError(
            f"cannot read the source registry at {p}: {exc}. Generate it with "
            f"`python3 artifacts/reservoir/build_registry.py`, and stage it into the job — a "
            f"build with no registry has nothing to read."
        ) from exc
    rows = doc.get("corpora") or []
    if not rows:
        raise BuildDriverError(f"{p} declares no corpora")
    specs = [CorpusSpec(**row) for row in rows]  # CorpusSpec.__post_init__ does the real checking
    meta = {k: v for k, v in doc.items() if k != "corpora"}
    return specs, meta


def _repo_root():
    from pathlib import Path

    # `src/edullm_data/corpus_build.py` -> repo root. Only used for the default registry path in a
    # checkout; on Batch the caller passes --registry explicitly because the wheel has no
    # `artifacts/` directory.
    return Path(__file__).resolve().parents[2]


def _assert_readable(specs: Sequence[CorpusSpec]) -> None:
    """Refuse a plan containing a source no reader can open.

    Fails at PLAN time by design. The alternative — skipping the row, or letting `run` discover it
    — produces a corpus that is quietly missing a whole category while every bundle that did run
    reports success, which is the failure mode this driver's `verify` exists to prevent.
    """
    bad = [(s.key, s.file_format) for s in specs if s.file_format not in READABLE_FORMATS]
    if bad:
        listing = ", ".join(f"{k} ({f})" for k, f in bad)
        raise BuildDriverError(
            f"{len(bad)} source(s) have no reader: {listing}. `corpus_read` handles "
            f"{sorted(READABLE_FORMATS)}. Either add support (zstd needs a `zstandard` dependency "
            f"this package does not declare) or drop the row from the plan deliberately with "
            f"--allow-unreadable, which EXCLUDES it from the corpus rather than failing."
        )


def plan_document(
    specs: Sequence[CorpusSpec],
    *,
    tokens_per_source: Mapping[str, int] | None = None,
    val_fraction: float = VAL_FRACTION,
    domain_map: Mapping[str, Mapping[str, str]] | None = None,
    registry_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The plan artifact: every bundle, every shard ref, and the ordinals, decided once.

    A PURE function of its arguments — no clock, no S3, no environment — so the same registry in
    gives byte-identical JSON out. ``plan_id`` is the sha256 of that JSON, which makes it a content
    address: two runs that agree on the plan agree on every key they will write, and a receipt
    naming a different ``plan_id`` is evidence about a different build.

    That purity is why there is no timestamp in the document. A `created_at` would change the
    `plan_id` on every regeneration and destroy exactly the property the id exists to provide.
    """
    drawn = [s for s in specs if s.target_tokens > 0]
    if not drawn:
        raise BuildDriverError("every registry row is reserve (target_tokens 0); nothing to build")

    targets: dict[tuple[str, str | None, str], int] = {}
    for spec in drawn:
        want = (tokens_per_source or {}).get(spec.key, spec.target_tokens)
        # Val is carved from DOCUMENTS before tokenizing (§1.4), so its token budget is the same
        # fraction of the source's target. Both splits read the same stream; `carve` routes them.
        val = int(want * val_fraction)
        # ⚠️ A source too small to yield ONE whole val shard gets no val split at all, and this is
        # the honest outcome rather than a workaround. Whole-shard selection means a partial shard
        # cannot be written (`shard_plan` refuses, correctly — a stream with no ordinals has no
        # destination), so the choices are: no held-out data for this source, or a val fraction
        # large enough to reach 25,001,984 tokens, which for a 1.8B source is 1.4%.
        #
        # At VAL_FRACTION 0.005 the break-even target is 5,000,396,800 tokens. Exactly one drawn
        # source is under it: ubuntu-irc at 1.8B (0.36 of a shard). Everything else clears it,
        # pubmed next-lowest at 1.20 shards.
        #
        # The consequence is stated rather than hidden: that source's documents ALL go to train,
        # so nothing is lost and nothing leaks — there is simply no per-source held-out set for it,
        # and a category-level val split has to come from its siblings.
        if val < SHARD_TOKENS:
            val = 0
        train = want - val
        # A domain-bearing source fans out into one stream per domain value only once the counting
        # pass has produced a map. Absent one, the source is flat — which is legal and is what
        # `labels_from_path` returns for a one-level key.
        doms = sorted((domain_map or {}).get(spec.key, {}).values()) or [None]
        uniq: list[str | None] = []
        for d in doms:
            if d not in uniq:
                uniq.append(d)
        for dom in uniq:
            share_t = train // len(uniq)
            share_v = val // len(uniq)
            if share_t:
                targets[(spec.source_label, dom, "train")] = share_t
            if share_v:
                targets[(spec.source_label, dom, "val")] = share_v

    from .corpus_pack import shard_plan

    plan = shard_plan(targets)
    refs = allocate_ordinals(plan)

    bundles: dict[tuple[str, str | None, str], list[ShardRef]] = {}
    for ref in refs:
        bundles.setdefault((ref.source, ref.domain, ref.split), []).append(ref)

    spec_by_label = {s.source_label: s for s in drawn}
    doc = {
        "schema": "edullm-build-plan/v1",
        "group": GROUP,
        "shard_tokens": SHARD_TOKENS,
        "min_doc_tokens": MIN_DOC_TOKENS,
        "val_fraction": val_fraction,
        "tokenizer": TOKENIZER_DATASET_ID,
        "registry_revisions_pinned_at": (registry_meta or {}).get("_revisions_pinned_at"),
        #: Sources that get NO held-out split because 0.5% of their target is under one shard.
        #: Recorded in the plan, not just warned about, so the omission is auditable afterwards —
        #: "which sources have no val data" must be answerable from the artifact.
        "no_val_split": sorted(
            s.source_label for s in drawn
            if int((tokens_per_source or {}).get(s.key, s.target_tokens) * val_fraction)
            < SHARD_TOKENS
        ),
        "bundles": [
            {
                "bundle_id": _bundle_id(src, dom, split),
                "source": src,
                "domain": dom,
                "split": split,
                "spec_key": spec_by_label[src].key,
                "repo": spec_by_label[src].repo,
                "revision": spec_by_label[src].revision,
                "config": spec_by_label[src].config,
                "file_format": spec_by_label[src].file_format,
                "text_column": spec_by_label[src].text_column,
                "id_column": spec_by_label[src].id_column,
                "tokens": sum(r.tokens for r in shards),
                "shards": [r.path for r in shards],
            }
            for (src, dom, split), shards in sorted(
                bundles.items(), key=lambda kv: (kv[0][0], kv[0][1] or "", kv[0][2])
            )
        ],
    }
    doc["plan_id"] = hashlib.sha256(
        json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return doc


def _bundle_id(source: str, domain: str | None, split: str) -> str:
    """A filesystem- and key-safe id for one stream."""
    return f"{source}--{domain}--{split}" if domain else f"{source}--{split}"


@dataclasses.dataclass(frozen=True)
class Bundle:
    """One unit of resumable work: one (source, domain, split) stream and its shard refs.

    Reconstructed from the plan rather than recomputed, so a child never allocates an ordinal.
    """

    bundle_id: str
    source: str
    domain: str | None
    split: str
    spec_key: str
    shards: tuple[ShardRef, ...]
    #: The plan's `val_fraction`, carried so `keep_rate` needs no second argument. Defaults to
    #: `VAL_FRACTION` for hand-built bundles in tests; the plan always supplies the real value.
    val_fraction: float = VAL_FRACTION

    @property
    def stream(self) -> tuple[str, str | None, str]:
        return (self.source, self.domain, self.split)

    @property
    def tokens(self) -> int:
        """Planned tokens for this bundle — the sum of its refs, not a stored field.

        Derived rather than carried so it cannot disagree with `shards`. `test_corpus_build`
        rescales refs to `TEST_SHARD_TOKENS`, and a stored total would then describe a different
        bundle than the one being packed.
        """
        return sum(r.tokens for r in self.shards)

    @property
    def keep_rate(self) -> float:
        """Fraction of the documents read that this bundle KEEPS, per the val carve.

        A val bundle keeps `val_fraction` (0.005) and discards the rest; a train bundle keeps the
        complement. This is what makes a val bundle's read ~200x its own token count — there is no
        way to reach a held-out document except to read the train documents interleaved with it,
        because `is_held_out` is a hash of the document id and is not knowable per file.
        """
        return self.val_fraction if self.split == "val" else (1.0 - self.val_fraction)

    @classmethod
    def from_plan_entry(cls, entry: Mapping[str, Any],
                        val_fraction: float = VAL_FRACTION) -> Bundle:
        from .manifest import parse_shard_name

        refs = []
        for path in entry["shards"]:
            parsed = parse_shard_name(path)
            if parsed is None:
                raise BuildDriverError(f"plan shard path {path!r} does not parse as a shard name")
            refs.append(
                ShardRef(
                    source=entry["source"],
                    domain=entry.get("domain"),
                    split=parsed[0],
                    ordinal=parsed[1],
                )
            )
        return cls(
            bundle_id=entry["bundle_id"],
            source=entry["source"],
            domain=entry.get("domain"),
            split=entry["split"],
            spec_key=entry["spec_key"],
            shards=tuple(refs),
            val_fraction=val_fraction,
        )


def bundles_of(plan: Mapping[str, Any]) -> list[Bundle]:
    # The plan's val_fraction reaches each Bundle here, because `_reader_for` sizes a val bundle's
    # read budget by its inverse and a wrong value there silently starves or over-reads.
    vf = float(plan.get("val_fraction", VAL_FRACTION))
    return [Bundle.from_plan_entry(e, vf) for e in plan["bundles"]]


def plan_key(prefix: str, plan_id: str) -> str:
    return _assert_safe_key(f"{prefix.strip('/')}/{plan_id}/plan.json")


def receipt_key(prefix: str, plan_id: str, bundle_id: str) -> str:
    """Where one bundle's receipt lives."""
    return _assert_safe_key(f"{prefix.strip('/')}/{plan_id}/_receipts/{bundle_id}.json")


def bundle_is_done(
    bundle: Bundle,
    plan_id: str,
    s3: Any,
    bucket: str,
    prefix: str,
) -> bool:
    """True only when the receipt exists AND every shard it names is in S3 at the right size.

    ⚠️ **The receipt alone is not evidence.** A worker that uploads three of four shards, writes its
    receipt, then dies leaves a bundle every later run declares finished — and the corpus is short
    one shard, discovered at training time. So this re-``head``s every key.

    Sizes, not just presence: an interrupted PUT leaves a key that exists at the wrong length, and
    ``head`` returns the size anyway, so checking only existence would be strictly weaker for free.

    Not a re-hash — that is `verify --deep`. Re-hashing here would make every resume a second full
    read of the corpus.
    """
    from .corpus_receipt import read_receipt

    key = receipt_key(prefix, plan_id, bundle.bundle_id)
    try:
        receipt = read_receipt(s3, bucket, key)
    except Exception:  # noqa: BLE001 - absent, unreadable, or malformed all mean "not done"
        return False
    if receipt.plan_id != plan_id:
        return False  # a receipt from another plan says nothing about this one
    declared = {r.path for r in receipt.shards}
    if declared != {r.path for r in bundle.shards}:
        return False
    root = f"{receipt.prefix.strip('/')}/" if receipt.prefix else ""
    for shard in receipt.shards:
        try:
            head = s3.head(bucket, f"{root}{shard.path}")
        except Exception:  # noqa: BLE001 - NotFound, or anything else: treat as not done
            return False
        if int(head.get("size", -1)) != shard.bytes:
            return False
    return True


def run_bundle(
    bundle: Bundle,
    plan: Mapping[str, Any],
    spec: CorpusSpec,
    *,
    s3: Any,
    bucket: str,
    prefix: str,
    documents: Callable[[CorpusSpec, Bundle], Iterable[Document]],
    tokenizer: Any,
    eos_id: int,
    vocab_size: int | None = None,
    wheel_version: str = "0.0.0",
    index: Any = None,
) -> dict[str, Any]:
    """Read → carve → filter → tokenize → pack → upload → receipt, for one bundle.

    ``documents`` is injected rather than constructed here so the whole pipeline is testable with no
    network: the Batch path passes a reader bound to `corpus_read`, a test passes a list.

    The upload happens inside `pack`'s sink, so a shard's bytes are written to S3 and dropped from
    memory before the next one is cut — `corpus_pack` holds at most one 100 MB shard at a time and
    routing it through a buffer here would undo that.
    """
    from .corpus_filter import FilterStats, dedup_and_decontaminate
    from .corpus_pack import pack, tokenize_documents
    from .corpus_receipt import Receipt, SourcePin, write_receipt

    plan_id = plan["plan_id"]
    root = f"{prefix.strip('/')}/{plan_id}/data"
    digests: dict[str, tuple[str, int]] = {}

    def sink(ref: ShardRef, payload: bytes) -> None:
        key = _assert_safe_key(f"{root}/{ref.path}")
        s3.put(bucket, key, payload)
        digests[ref.path] = (hashlib.sha256(payload).hexdigest(), len(payload))

    # Carve routes documents by a pure function of (source, doc_id), so BOTH splits are decided
    # from one read of the source. This bundle keeps only its own side.
    want_split = bundle.split

    def _selected() -> Iterator[Document]:
        for split, doc in carve(documents(spec, bundle), fraction=plan["val_fraction"]):
            if split == want_split:
                yield doc

    # §4.1 step 2 + §4.2, and this order is the one the design specifies: dedup and decontaminate
    # DOCUMENTS, before anything is tokenized. After tokenization a document is a byte range inside
    # a shard, and removing one means re-cutting every shard after it.
    #
    # Ahead of the length filter as well, because both are cheaper than tokenizing and there is no
    # point measuring the token length of a document that is about to be dropped.
    filter_stats = FilterStats()
    surviving = dedup_and_decontaminate(_selected(), index=index, stats=filter_stats)

    # The length filter runs INSIDE tokenize_documents, from the ids encode_batch already produced.
    # It used to be a separate `corpus_read.filter_documents` pass calling `tokenizer.encode` once
    # per document, which encoded the whole corpus twice — and that pass got no rayon parallelism,
    # measured at 1.10 M tok/s against encode_batch's 10.5 M across 32 vCPU, making it ~91% of the
    # build's compute on 1 of 32 cores. `filter_documents` remains the right tool for a caller with
    # a cheap length proxy; this driver has none, because the tokenizer IS the length.
    #
    # TWO stats objects, not one, and they are not interchangeable. `corpus_filter.FilterStats`
    # closes as `seen == kept + duplicates + contaminated` — an identity a test asserts and the
    # receipt reports. Letting the length filter decrement its `kept` would break that identity
    # silently. `corpus_read.FilterStats` is the one with `dropped_short`/`dropped_tokens` and the
    # `mean_kept_tokens` that predicts the EOS fraction, so the length pass reports into its own.
    from .corpus_read import FilterStats as LengthStats

    length_stats = LengthStats(min_tokens=plan["min_doc_tokens"])
    arrays = tokenize_documents(
        surviving, tokenizer, eos_id=eos_id, vocab_size=vocab_size,
        min_tokens=plan["min_doc_tokens"], stats=length_stats,
    )
    # partial_source=True because `_reader_for` DELIBERATELY over-delivers: the registry draws
    # 252B tokens from a 1,094B pool, and the budget carries `_FILTER_HEADROOM` slack so filter
    # attrition cannot leave the last shard unfilled. Whatever that slack does not consume is
    # surplus by design. Without this flag `_drain_surplus` raised on it — measured live as 25 of
    # 27 bundles failing at end-of-run, each after its full billable work, with only ubuntu-irc
    # passing because its pool is 1.04x its target so the reader hit end-of-files first.
    results = pack({bundle.stream: arrays}, list(bundle.shards), sink=sink, eos_id=eos_id,
                   vocab_size=vocab_size, partial_source=True)
    if not results:
        raise BuildDriverError(f"{bundle.bundle_id}: pack returned no result")
    result = results[0]

    receipt = Receipt.from_pack_result(
        result,
        plan_id=plan_id,
        prefix=root,
        digests=digests,
        bundle_id=bundle.bundle_id,
        wheel_version=wheel_version,
        sources=(
            SourcePin(
                key=spec.key, repo=spec.repo, revision=spec.revision or "", config=spec.config
            ),
        ),
    )
    # Verify BEFORE writing the receipt. A receipt is a claim that work is done; writing one for
    # shards that failed their own checks is how a later run skips broken work.
    from .corpus_receipt import verify_receipt

    violations = verify_receipt(receipt, s3, bucket)
    if violations:
        raise BuildDriverError(
            f"{bundle.bundle_id}: refusing to write a receipt over "
            f"{len(violations)} violation(s): {'; '.join(str(v) for v in violations[:3])}"
        )
    key = receipt_key(prefix, plan_id, bundle.bundle_id)
    # `write_receipt` returns the receipt's sha256, NOT the key — it is a content address for the
    # receipt itself, usable as an idempotency token.
    digest = write_receipt(receipt, s3, bucket, key)
    return {
        "bundle_id": bundle.bundle_id,
        "receipt_key": key,
        "receipt_sha256": digest,
        "shards": len(result.written),
        "tokens_out": result.tokens_out,
        "unfilled": len(result.unfilled),
        "filter": filter_stats.as_dict(),
        # Reported separately from `filter` because it is a different denominator: `filter.seen`
        # counts documents entering dedup, `length.seen` counts those that survived it. Merging
        # them would recreate the `category_attrition` mistake — a numerator whose denominator a
        # reader has to guess.
        "length": {
            "min_tokens": length_stats.min_tokens,
            "seen": length_stats.seen,
            "kept": length_stats.kept,
            "dropped_short": length_stats.dropped_short,
            "dropped_empty": length_stats.dropped_empty,
            "kept_tokens": length_stats.kept_tokens,
            "mean_kept_tokens": round(length_stats.mean_kept_tokens, 2),
        },
    }


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _s3():
    from .s3 import Boto3S3

    return Boto3S3.default()


def load_tokenizer(directory: str) -> tuple[Any, int, int]:
    """``(tokenizer, eos_id, vocab_size)`` from a local ``tokenizer.json``.

    **`vocab_size` and `eos_id` are DERIVED from the file, never typed.** `profiles.tokenizer_v1.
    derive_vocab` is the same function the validator uses, so the ids this build asserts against
    are the ids Gate A will assert against — reusing it is what makes the two agree by
    construction rather than by two people typing 100278.

    A mismatched tokenizer is close to undetectable downstream: vocab sizes cluster near 100k, so
    wrong ids stay in range and pass every decode check while being semantically wrong
    (``families/pretrain.json`` notes). Deriving removes the one place a typo could enter.
    """
    from pathlib import Path

    from .profiles.tokenizer_v1 import derive_vocab

    path = Path(directory) / "tokenizer.json"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BuildDriverError(
            f"cannot read {path}: {exc}. Stage the published tokenizer "
            f"({TOKENIZER_DATASET_ID}) into the job and pass --tokenizer-dir."
        ) from exc
    derived = derive_vocab(raw)
    eos = derived.get("eos_token_id")
    if eos is None:
        raise BuildDriverError(
            f"{path} declares no discoverable EOS token. EOS must be in the bytes — OLMo-core adds "
            f"no special tokens, so without it the corpus has no document boundaries at all."
        )
    try:
        from tokenizers import Tokenizer
    except ImportError as exc:  # pragma: no cover - environment problem, not logic
        raise BuildDriverError(f"the `tokenizers` package is required to tokenize: {exc}") from exc
    return Tokenizer.from_file(str(path)), int(eos), int(derived["vocab_size"])


def _cmd_plan(args) -> int:
    specs, meta = load_registry(args.registry)
    drawn = [s for s in specs if s.target_tokens > 0]
    if not args.allow_unreadable:
        _assert_readable(drawn)
    else:
        drawn = [s for s in drawn if s.file_format in READABLE_FORMATS]
    plan = plan_document(drawn, registry_meta=meta)
    body = json.dumps(plan, indent=1).encode()
    print(f"plan_id={plan['plan_id']} bundles={len(plan['bundles'])} "
          f"shards={sum(len(b['shards']) for b in plan['bundles'])} "
          f"tokens={sum(b['tokens'] for b in plan['bundles']):,}")
    if args.out:
        with open(args.out, "wb") as fh:
            fh.write(body)
        print(f"wrote {args.out}")
    if args.upload:
        _require_batch(allow_local=args.allow_local)
        s3 = _s3()
        _assert_lifecycle_covers(s3._c, args.bucket, args.prefix.strip("/") + "/")
        key = plan_key(args.prefix, plan["plan_id"])
        s3.put(args.bucket, key, body, content_type="application/json")
        print(f"wrote s3://{args.bucket}/{key}")
    return 0


def _load_plan(s3, bucket: str, prefix: str, plan_id: str) -> dict[str, Any]:
    return json.loads(s3.get(bucket, plan_key(prefix, plan_id)))


def _cmd_run(args) -> int:
    _require_batch(allow_local=args.allow_local)
    _assert_tokenizers_parallelism()
    s3 = _s3()
    plan = _load_plan(s3, args.bucket, args.prefix, args.plan_id)
    specs = {s.key: s for s in load_registry(args.registry)[0]}
    shard = args.shard if args.shard is not None else int(
        os.environ.get("AWS_BATCH_JOB_ARRAY_INDEX", "0")
    )
    mine = _shard_slice(bundles_of(plan), shard, args.of)
    print(f"RUN_START plan={args.plan_id} shard={shard}/{args.of} bundles={len(mine)}", flush=True)

    tok, eos, vocab = load_tokenizer(args.tokenizer_dir)

    # §4.2. Loading RAISES when the index is absent, and `--no-decontaminate` is the only way to
    # skip it — a build that silently skipped would produce a corpus indistinguishable from a
    # decontaminated one, discovered when a benchmark score looks too good months later.
    index = None
    if args.no_decontaminate:
        print("WARNING decontamination DISABLED by --no-decontaminate", flush=True)
    else:
        from .corpus_filter import load_index

        index = load_index(s3, args.bucket)
        print(f"DECON index {len(index.exact_hashes):,} exact + "
              f"{len(index.ngram_hashes):,} ngrams", flush=True)

    done = skipped = 0
    for bundle in mine:
        if not args.force and bundle_is_done(bundle, args.plan_id, s3, args.bucket, args.prefix):
            skipped += 1
            print(f"SKIP {bundle.bundle_id} (receipt + all shards present)", flush=True)
            continue
        t0 = time.monotonic()
        info = run_bundle(
            bundle, plan, specs[bundle.spec_key], s3=s3, bucket=args.bucket, prefix=args.prefix,
            documents=_reader_for, tokenizer=tok, eos_id=eos, vocab_size=vocab,
            wheel_version=_wheel_version(), index=index,
        )
        done += 1
        f = info["filter"]
        print(f"DONE {bundle.bundle_id} shards={info['shards']} "
              f"tokens={info['tokens_out']:,} docs={f['kept']:,}/{f['seen']:,} "
              f"dup={f['duplicates']:,} decon={f['contaminated']:,} "
              f"{time.monotonic() - t0:.0f}s", flush=True)
    print(f"RUN_END built={done} skipped={skipped}", flush=True)
    return 0


def _cmd_verify(args) -> int:
    from .corpus_receipt import read_receipt, verify_bundle_set

    s3 = _s3()
    plan = _load_plan(s3, args.bucket, args.prefix, args.plan_id)
    bundles = bundles_of(plan)
    receipts = []
    missing = []
    for b in bundles:
        try:
            receipts.append(read_receipt(s3, args.bucket, receipt_key(
                args.prefix, args.plan_id, b.bundle_id)))
        except Exception:  # noqa: BLE001
            missing.append(b.bundle_id)
    violations = verify_bundle_set(
        receipts, [b.stream for b in bundles], s3=s3, bucket=args.bucket, deep=args.deep,
    )
    for m in missing:
        print(f"MISSING RECEIPT {m}")
    for v in violations:
        print(f"VIOLATION {v}")
    if missing or violations:
        print(f"\nFAILED: {len(missing)} missing receipt(s), {len(violations)} violation(s). "
              f"A missing bundle is not a smaller corpus, it is a wrong one — the mixture still "
              f"names the source and nothing else objects.")
        return 1
    print(f"OK {len(receipts)} bundles, {sum(len(r.shards) for r in receipts)} shards"
          f"{' (payload re-hashed)' if args.deep else ''}")
    return 0


def _assert_tokenizers_parallelism() -> None:
    """Refuse to run until the operator has decided, because neither default is safe here.

    `corpus_pack` deliberately does not set this — a library that mutates it affects every importer
    including the validator. But leaving it unset in a forking driver is the documented HF
    deadlock, and blindly setting `"false"` throws away the rayon parallelism that makes a
    255B-token tokenize affordable. So the driver requires an explicit choice rather than picking
    one silently. `week1_corpus` never sets it anywhere, which is how this became a known trap.
    """
    if "TOKENIZERS_PARALLELISM" not in os.environ:
        raise BuildDriverError(
            "TOKENIZERS_PARALLELISM is unset. Set it explicitly: 'true' for a single-process "
            "tokenize (keeps rayon parallelism, which is what makes 255B tokens affordable), or "
            "'false' if this process forks AFTER encoding (avoids the HF fork deadlock). There is "
            "no safe default, which is why neither this driver nor corpus_pack picks one."
        )


def _wheel_version() -> str:
    from . import __version__

    return __version__


#: Payload extensions per registry `file_format`. `ingest_reservoir.hf_tree` filters to `.parquet`
#: only, so the Common Pile `.json.gz` sources need their own listing.
_PAYLOAD_EXT = {"parquet": (".parquet",), "json.gz": (".json.gz", ".jsonl.gz")}


def hf_files(spec: CorpusSpec, *, headers: Mapping[str, str] | None = None) -> list[dict]:
    """Every payload file for one source, listed AT ITS PINNED REVISION.

    Not `ingest_reservoir.hf_tree`, for two reasons that both matter here. It filters to
    `.parquet`, so it returns nothing for the seven Common Pile `.json.gz` sources; and
    `ingest_reservoir._resolve_url` hardcodes `resolve/main`, which would silently defeat the
    revision pinning — the whole point of which is that a re-run reads the same bytes.

    Paginated via the `Link` header rather than trusting one page: `fineweb-edu` and
    `essential-web` hold thousands of files, and a truncated listing is a silently short corpus.
    """
    import urllib.request

    if not spec.revision:
        raise BuildDriverError(
            f"{spec.key}: no pinned revision. Reading `main` makes the build unreproducible — a "
            f"re-download can return different bytes under the same name."
        )
    exts = _PAYLOAD_EXT.get(spec.file_format)
    if exts is None:
        raise BuildDriverError(f"{spec.key}: no payload extension known for {spec.file_format!r}")
    base = f"https://huggingface.co/api/datasets/{spec.repo}/tree/{spec.revision}"
    url = f"{base}/{spec.config}?recursive=1&limit=1000" if spec.config \
        else f"{base}?recursive=1&limit=1000"
    hdrs = dict(headers or {"User-Agent": "edullm-data/corpus-build"})
    out: list[dict] = []
    while url:
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=60) as resp:
            page = json.load(resp)
            link = resp.headers.get("Link", "")
        out += [e for e in page
                if e.get("type") == "file" and e["path"].endswith(tuple(exts))]
        url = ""
        for part in link.split(","):
            if 'rel="next"' in part and "<" in part:
                url = part[part.index("<") + 1: part.index(">")]
                break
    if not out:
        raise BuildDriverError(
            f"{spec.key}: no {exts} files at {spec.repo}@{spec.revision[:10]}"
            f"{'/' + spec.config if spec.config else ''}. Check the layout with "
            f"`python3 artifacts/reservoir/verify_pins.py --deep`."
        )
    return sorted(out, key=lambda e: e["path"])


def _resolve_pinned(repo: str, revision: str, path: str) -> str:
    """`resolve/<revision>/`, never `resolve/main`."""
    return f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{path}"


#: Characters of source text read per token the bundle needs, before the reader stops.
#:
#: The reader has to stop somewhere, and it cannot stop on a token count: it yields TEXT, and
#: nothing knows a document's token length until the tokenizer has seen it (which is the whole
#: point of the previous fix — there is no cheap pre-count). So the budget is denominated in
#: characters and this is the conversion.
#:
#: MEASURED per source on the first file at each pinned revision, 400 documents each
#: (`artifacts/reservoir/chars-per-token.json`). The spread is wide and the direction matters:
#:
#:     finephrase-table 5.58   finephrase-faq 4.94   fineweb-edu 4.62   peS2o 4.61
#:     finephrase-tutorial 4.85  finephrase-math 4.67  finepdfs-edu 4.47  stackexchange 4.41
#:     finewiki 4.38   dclm-baseline 4.39   pubmed 4.20   stackv2-edu 3.66
#:     ubuntu-irc 3.02   finemath 2.56
#:
#: 6.0, i.e. above the worst observed, because the two errors are not symmetric. Reading too much
#: costs time and `pack` discards the overshoot; reading too little leaves the bundle's last shard
#: unfilled, which `verify` refuses and which costs a re-run of the whole bundle. A first draft
#: used 4.0 (measured on general prose) and 10 of 14 sources exceeded it — the build only worked
#: because `_FILTER_HEADROOM` happened to absorb the gap, which is the wrong constant doing the
#: wrong job by luck.
_CHARS_PER_TOKEN = 6.0

#: Multiplier on the character budget, to cover what the filters remove downstream.
#:
#: Between this reader and `pack` sit exact dedup, decontamination and the >=64-token floor, and
#: every one of them DELETES documents. A budget sized for exactly `bundle.tokens` therefore
#: under-delivers by whatever those three remove — measured at 3.4-12.6% for FinePhrase's short
#: rewrites (`artifacts/recount/synthetic.json:173`) and unmeasured for the rest.
#:
#: 1.5 is slack, not a measurement, and it is deliberately generous in the direction that costs
#: only time. Too high wastes reads that `_drain_surplus` then absorbs; too low leaves the final
#: shard of a bundle unfilled, which is a `verify` failure and a re-run of the whole bundle.
#:
#: It covers ONLY filter attrition. The chars-per-token conversion is `_CHARS_PER_TOKEN`'s job and
#: is measured separately — conflating the two is how a wrong conversion hides inside a generous
#: headroom until the one source that needs both fails.
_FILTER_HEADROOM = 1.5


def _reader_for(spec: CorpusSpec, bundle: Bundle) -> Iterable[Document]:
    """Documents for one bundle, dispatched on the registry's `file_format`.

    **Stops once the bundle's character budget is met, and that bound is what makes the build
    runnable at all.** The registry draws 252 B tokens from a 1,094 B-token pool, so a reader that
    walks every file arrives at `pack` with thousands of shards' worth of documents the plan has no
    refs for — and `corpus_pack._drain_surplus` REFUSES a surplus of one whole shard, because
    discarding already-tokenized tokens means the plan and reality disagree. Measured before this
    bound existed: all 14 drawn sources raised, 11 of them needing an impossible 46-90% filter loss
    to come under the threshold. The raise is correct; what was missing is that nothing told the
    reader when to stop.

    Deliberately NOT a hard cap on documents handed over: the loop breaks between FILES, so the
    last file is always read to its end. Truncating mid-file would make the document set depend on
    where the budget happened to run out, and a re-run with a different `min_doc_tokens` would then
    select a different corpus under the same `plan_id`. Whole files keep the read deterministic.

    ⚠️ UNVERIFIED against live HF from inside a Batch container — every offline test injects
    `documents=` instead, so this dispatch is exercised only by its own unit test. Settle it with a
    single-bundle `run --of <n_bundles>` against the smallest source (`ubuntu-irc`, 1.87B) before
    committing a full array.
    """
    from .corpus_read import read_jsonl_gz_documents, read_parquet_documents

    reader = {
        "parquet": read_parquet_documents,
        "json.gz": read_jsonl_gz_documents,
    }.get(spec.file_format)
    if reader is None:
        raise BuildDriverError(
            f"{spec.key}: no reader for {spec.file_format!r} — this should have been caught at "
            f"plan time by _assert_readable"
        )

    # A val bundle keeps only `val_fraction` of what it reads, so its budget is scaled by the
    # inverse. Without this a val bundle stops after ~0.5% of the text it needs and every one of
    # its shards comes up empty. The carve is a pure function of the document id (corpus.is_held_out)
    # and cannot be predicted per file, so there is no cheaper way to reach a val document than
    # reading the train ones alongside it and discarding them.
    keep_rate = bundle.keep_rate
    budget = int(bundle.tokens * _CHARS_PER_TOKEN * _FILTER_HEADROOM / keep_rate)
    seen_chars = 0
    for entry in hf_files(spec):
        for doc in reader(spec.repo, entry, spec):
            seen_chars += len(doc.text)
            yield doc
        if seen_chars >= budget:
            return


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="edullm-corpus-build",
        description="Plan, run, and verify the reservoir corpus build (§5.6 phase 1).",
    )
    ap.add_argument("--bucket", default=DEFAULT_BUCKET)
    ap.add_argument("--prefix", default=DEFAULT_PREFIX)
    ap.add_argument("--registry", default=None, help="path to corpus-registry.json")
    ap.add_argument("--allow-local", action="store_true",
                    help="skip the AWS_BATCH_JOB_ID guard (dev only)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="compute the plan; ordinals are allocated ONCE, here")
    p.add_argument("--out", default=None, help="also write the plan JSON locally")
    p.add_argument("--upload", action="store_true", help="write the plan to S3")
    p.add_argument("--allow-unreadable", action="store_true",
                   help="EXCLUDE sources with no reader instead of failing")
    p.set_defaults(func=_cmd_plan)

    r = sub.add_parser("run", help="build this array child's slice of the bundles")
    r.add_argument("--plan-id", required=True)
    r.add_argument("--shard", type=int, default=None,
                   help="defaults to AWS_BATCH_JOB_ARRAY_INDEX")
    r.add_argument("--of", type=int, required=True)
    r.add_argument("--tokenizer-dir", required=True)
    r.add_argument("--force", action="store_true", help="rebuild even if a receipt verifies")
    r.add_argument("--no-decontaminate", action="store_true",
                   help="DELIBERATELY skip the eval decontamination pass (§4.2)")
    r.set_defaults(func=_cmd_run)

    v = sub.add_parser("verify", help="refuse an incomplete build")
    v.add_argument("--plan-id", required=True)
    v.add_argument("--deep", action="store_true",
                   help="re-hash every payload byte (a full GET per shard)")
    v.set_defaults(func=_cmd_verify)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (BuildDriverError, BuildError, IngestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
