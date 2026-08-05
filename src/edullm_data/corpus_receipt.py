"""The build receipt: one bundle's claim that it is finished, and the checks that falsify it.

A corpus build is ~420 Batch array children (``HANDOFF.md`` item 3). Each child owns a **bundle** —
one ``(source, domain, split)`` stream's shards — and when it finishes it writes a receipt. The
receipt exists because resumability needs a way to answer "is bundle *k* done?" without re-reading
the documents that produced it, and because the alternative answer (a commit marker) is the failure
mode the survey already recorded: *a worker that commits then dies leaves missing shards that every
later run declares done*.

WHY THIS MODULE IS MOSTLY VERIFIER
----------------------------------
A receipt is a producer assertion. Under this project's golden rule ("recompute, never trust",
``CONTRIBUTING.md`` §0.4) a claim nothing recomputes is decoration — so the schema below is designed
backwards from the checks: **every field that could be wrong is stored next to something it can be
checked against.** ``bytes`` is checkable against a real ``head``; ``tokens`` is checkable against
those bytes through ``tokens * 4 == bytes``; ``path`` is checkable against the bundle's own stream
identity through :func:`corpus.shard_key`; ``sha256`` is checkable only by re-reading the payload,
which is why it is the one thing behind ``deep=True``.

Nothing here raises on a bad dataset. :func:`verify_receipt` returns a list of
``profiles.base.Violation``, the same shape ``validate.py`` accumulates, because one pass should
surface every problem rather than the first one — a build that fails 400 bundles for one reason
should say so once, not 400 times over 400 re-runs.

WHAT IS RECOMPUTED, AND WHAT IS NOT — read this the way you read ``fsck.py``'s docstring
-----------------------------------------------------------------------------------------
``fsck`` states outright that it never reads a payload byte. This module is the build-time analogue
and owes the same disclosure:

* **Cheap tier (always, metadata only).** One ``head`` per shard: presence, ``ContentLength``, the
  ``tokens * 4 == bytes`` identity, the ``bytes % (4 * SEQ_LEN) == 0`` alignment, and a conservation
  cross-check that re-derives ``tokens_out`` from the sizes S3 actually reports. Plus the pure
  checks that need no S3 at all (duplicate digests inside a bundle, shard keys that do not belong to
  the bundle's stream, unpinned upstream revisions, the ``PackResult`` identity).
* **Deep tier (``deep=True``, opt-in).** ``s3.hash_object`` re-reads every shard and compares the
  digest to the receipt's.

  **This is the only re-hash anywhere in the pipeline.** ``CLAUDE.md``'s KNOWN GAP records the
  situation precisely: ``s3.hash_object`` has exactly one non-definition caller, ``publish.py:371``,
  which is the PRODUCER; Gate A's per-entry loop (``validate.py:391-426``) issues a ``head`` for the
  SIZE and then does set-membership on the *declared* digest, and never re-reads payload bytes. So a
  manifest ``sha256`` is a producer assertion no gate falsifies. ``deep=True`` falsifies it — at the
  price of a full GET of every shard in the bundle (~100 MB each, ~1 TB across a 10,400-shard
  corpus, in-region). That is why it is opt-in and why it is worth having at all: run it once on a
  sampled subset of bundles, or on any bundle whose cheap tier looked suspicious.

  A corrupted payload that preserves length passes **every** cheap check here and every check at
  Gate A. Only the deep tier sees it before promotion; after promotion, ``fsck``'s CRC64NVME
  comparison is what notices (and only because ``promote()`` captured a post-copy reference).

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
It does not decode a shard. The decode bounds (EOS fraction, zero runs, distinct ids) are recomputed
by ``corpus_pack._verify_shard`` against the in-memory buffer *before* the sink ever sees it, which
is strictly cheaper and strictly stronger than sampling the same shard back out of S3. Re-sampling
here would be a weaker copy of a check that already ran. ``max_eos_fraction`` is carried in the
receipt as a *record* of that check, not as a claim this module re-derives — and it is labelled as
such below.

It also writes no manifest and no ``dataset.json``. A receipt is a build artifact under landing's
scratch prefixes; Gate A never reads one.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from . import __version__
from .contracts import CONTROL_BASENAMES, canonical_json
from .corpus import DTYPE_SIZE, SEQ_LEN, BuildError, shard_key
from .manifest import parse_shard_name
from .profiles.base import Violation
from .s3 import S3, NotFound

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Imported for annotation only so this module stays importable without numpy. `corpus_pack`
    # pulls numpy at module scope; the verifier may run anywhere (a laptop, the validator image)
    # and has no numeric work of its own. `from_pack_result` duck-types the fields it reads.
    from .corpus_pack import PackResult

__all__ = [
    "RECEIPT_SCHEMA_VERSION",
    "READABLE_RECEIPT_SCHEMAS",
    "SEQ_LEN_STRIDE",
    "SourcePin",
    "ShardReceipt",
    "Receipt",
    "bundle_id_for",
    "verify_receipt",
    "verify_bundle_set",
    "write_receipt",
    "read_receipt",
]

#: Versioned because the checks below are an interpretation of these fields, not of a shape. A
#: verifier that applied v1 rules to a v2 document would report confidently on fields it does not
#: understand, which is worse than refusing.
RECEIPT_SCHEMA_VERSION = "edullm-corpus-receipt/v1"
READABLE_RECEIPT_SCHEMAS = frozenset({RECEIPT_SCHEMA_VERSION})

#: ``4 * 8192`` = 32,768. The exact stride ``pretrain_tokens_v1.check_seq_len_alignment``
#: (``profiles/pretrain_tokens_v1.py:445``) recomputes ``size % stride`` against at Gate A. Checking
#: it here is one HEAD; checking it there has already cost the copy of the whole corpus, and
#: ``promote()`` is all-or-nothing so one misaligned tail shard blocks everything.
SEQ_LEN_STRIDE = DTYPE_SIZE * SEQ_LEN

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

#: Ref names that LOOK pinned in a JSON field and are not. ``ingest_reservoir`` pins its repos
#: "because ``main`` is mutable": a re-download six weeks later returns different bytes under the
#: same name, and the receipt would still read as provenance.
_MUTABLE_REFS = frozenset(
    {"main", "master", "head", "latest", "refs/heads/main", "refs/heads/master"}
)


# --------------------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SourcePin:
    """One upstream snapshot a bundle was built from.

    ``revision`` is the field that matters. ``artifacts/reservoir/corpus-registry.json`` pins a
    real commit sha on all 17 rows (14 distinct repos, each resolved against the HF tree API on
    2026-08-01 — which is what caught seven Common Pile rows pointing at the wrong repo entirely).
    The receipt copies that pin forward so the check survives into the artifact: an unpinned source
    means a re-download returns different bytes for the same corpus, and the registry cannot enforce
    it at build time because a registry row is written long before the build that consumes it.

    :func:`verify_receipt` therefore refuses a ``revision`` that is absent or is a branch name. A
    receipt recording ``main`` would defeat the pinning entirely while still reading as provenance.
    """

    key: str
    repo: str
    revision: str | None = None
    config: str | None = None

    def __post_init__(self) -> None:
        if not self.key or not self.repo:
            raise BuildError(f"SourcePin needs both a key and a repo; got {self!r}")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key, "repo": self.repo, "revision": self.revision}
        if self.config is not None:
            out["config"] = self.config
        return out

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> "SourcePin":
        return cls(
            key=str(doc.get("key", "")),
            repo=str(doc.get("repo", "")),
            revision=doc.get("revision"),
            config=doc.get("config"),
        )


@dataclass(frozen=True)
class ShardReceipt:
    """One shard the bundle wrote: the key, and three facts about the bytes at that key.

    All three are redundant *with the object itself*, and that redundancy is the design. ``bytes``
    is comparable to a ``head``; ``tokens`` is comparable to those bytes through the fixed-width
    identity; ``sha256`` is comparable to a re-hash. A field that could not be compared to anything
    would be a field the receipt could lie about for free.

    ``bytes`` is the length of the payload handed to the sink, NOT ``ref.tokens * 4`` recomputed —
    ``from_pack_result`` takes it from the ``(sha256, size)`` pair the writer produced, matching
    ``s3.hash_object``'s return shape. Deriving it would make ``tokens * 4 == bytes`` an identity
    that cannot fail, i.e. decoration.
    """

    path: str
    sha256: str
    tokens: int
    bytes: int

    def __post_init__(self) -> None:
        # Structural only. A value that is not even a candidate for the checks below fails to LOAD
        # rather than producing a violation, mirroring `ManifestEntry.__post_init__` — `validate.py`
        # reports that class as `bad-manifest-entry` at parse time for the same reason.
        if not isinstance(self.path, str) or not self.path or self.path.startswith("/"):
            raise BuildError(f"ShardReceipt.path must be a relative non-empty key; got {self.path!r}")
        if not isinstance(self.sha256, str) or not _SHA256_HEX_RE.match(self.sha256):
            raise BuildError(
                f"ShardReceipt.sha256 for {self.path!r} must be 64 lowercase hex chars; "
                f"got {self.sha256!r}"
            )
        for name in ("tokens", "bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BuildError(
                    f"ShardReceipt.{name} for {self.path!r} must be a non-negative int; got {value!r}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "tokens": self.tokens,
            "bytes": self.bytes,
        }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> "ShardReceipt":
        return cls(
            path=doc.get("path"),  # type: ignore[arg-type]  # __post_init__ types it
            sha256=doc.get("sha256"),  # type: ignore[arg-type]
            tokens=doc.get("tokens"),  # type: ignore[arg-type]
            bytes=doc.get("bytes"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class Receipt:
    """One bundle's completion claim.

    Frozen, and JSON-serializable through :meth:`to_dict` / :meth:`from_dict`. It carries four
    things that a later stage cannot recover on its own:

    * **identity** — ``plan_id`` (which build), ``bundle_id`` (which unit of work), and the
      ``(source, domain, split)`` stream. Resumability is at bundle granularity because you cannot
      skip shard *k* of a stream without re-reading the documents that produced it.
    * **the shards** — see :class:`ShardReceipt`.
    * **the conservation numbers** from ``corpus_pack.PackResult``. That dataclass asserts
      ``tokens_out + tail_dropped + surplus_dropped == tokens_in`` at runtime
      (``corpus_pack.py:447``), and the assertion dies with the process. Carrying the four numbers
      into the artifact is what lets the identity be re-checked afterwards, by a different program,
      against sizes read back out of S3.
    * **provenance** — ``wheel_version`` and the pinned :class:`SourcePin` rows. ``wheel_version``
      is not decoration: ``CLAUDE.md`` gotcha 2/3 records a live corpus validated at 50% EOS while
      declaring 5% because the deployed wheel shipped without ``families/``, and the job defs
      bootstrap a wheel *by exact filename*. A resumed build whose bundles were written by two
      different wheels is half-gated by whichever one was older, and
      :func:`verify_bundle_set` is the only place that can see it.

    ``max_eos_fraction`` is a **record**, not a claim this module re-derives — the per-shard decode
    bounds were recomputed against the buffer in ``corpus_pack._verify_shard`` before the bytes were
    written, which is cheaper and stronger than sampling them back. It is here so a human reading
    receipts can see the margin, and so a sweep can rank bundles by how close they ran to the family
    bound.
    """

    plan_id: str
    bundle_id: str
    #: Key prefix the shard paths are relative to, e.g. ``pretrain/reservoir-dolma2/v1``. Part of
    #: the claim: "these keys, under this prefix, exist". Empty means the paths are whole keys.
    prefix: str
    source: str
    domain: str | None
    split: str
    shards: tuple[ShardReceipt, ...]
    documents: int
    tokens_in: int
    tokens_out: int
    tail_dropped: int
    surplus_dropped: int
    max_eos_fraction: float
    wheel_version: str
    sources: tuple[SourcePin, ...] = ()
    #: Planned refs the stream had no data for. Data, not an error (``corpus_pack.pack``: ordinal
    #: gaps are legal). Recorded so a reader of the receipt can tell "this bundle underran its plan"
    #: from "this bundle wrote every shard it was asked for" — otherwise the two are the same
    #: shorter-than-expected shard list.
    unfilled: tuple[str, ...] = ()
    schema_version: str = RECEIPT_SCHEMA_VERSION

    @property
    def stream(self) -> tuple[str, str | None, str]:
        """The ``(source, domain, split)`` triple, i.e. ``PackResult.stream``."""
        return (self.source, self.domain, self.split)

    @property
    def label(self) -> str:
        """Human-readable stream name, matching ``corpus_pack._pack_stream``'s ``label``."""
        return "/".join(part for part in self.stream if part)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "bundle_id": self.bundle_id,
            "prefix": self.prefix,
            "stream": {"source": self.source, "domain": self.domain, "split": self.split},
            "shards": [s.to_dict() for s in self.shards],
            "unfilled": list(self.unfilled),
            "pack": {
                "documents": self.documents,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "tail_dropped": self.tail_dropped,
                "surplus_dropped": self.surplus_dropped,
                "max_eos_fraction": self.max_eos_fraction,
            },
            "build": {
                "wheel_version": self.wheel_version,
                "sources": [p.to_dict() for p in self.sources],
            },
        }

    def to_json_bytes(self) -> bytes:
        """The canonical serialization, via ``contracts.canonical_json``.

        The same function the hash chain is defined over, so two runs that produce the same receipt
        produce the same bytes and the same digest — which is what makes :meth:`receipt_sha256`
        usable as an idempotency key by a resumable driver.
        """
        return canonical_json(self.to_dict())

    def receipt_sha256(self) -> str:
        return hashlib.sha256(self.to_json_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> "Receipt":
        """Parse a receipt document. Raises :class:`~.corpus.BuildError` on an unusable one.

        A document that will not load is a LOAD failure, not a violation — the same split
        ``validate.py`` draws between ``manifest-unparseable`` and the per-entry checks. There is
        nothing useful to report about fields that could not be read.
        """
        if not isinstance(doc, Mapping):
            raise BuildError(f"a receipt must be a JSON object; got {type(doc).__name__}")
        stream = doc.get("stream")
        if not isinstance(stream, Mapping):
            raise BuildError("receipt has no stream {source, domain, split} block")
        pack = doc.get("pack") if isinstance(doc.get("pack"), Mapping) else {}
        build = doc.get("build") if isinstance(doc.get("build"), Mapping) else {}
        raw_shards = doc.get("shards") or []
        if not isinstance(raw_shards, Sequence) or isinstance(raw_shards, (str, bytes)):
            raise BuildError("receipt.shards must be a list")
        return cls(
            plan_id=str(doc.get("plan_id", "")),
            bundle_id=str(doc.get("bundle_id", "")),
            prefix=str(doc.get("prefix", "")),
            source=str(stream.get("source", "")),
            domain=stream.get("domain"),
            split=str(stream.get("split", "")),
            shards=tuple(ShardReceipt.from_dict(s) for s in raw_shards),
            documents=int(pack.get("documents", 0)),
            tokens_in=int(pack.get("tokens_in", 0)),
            tokens_out=int(pack.get("tokens_out", 0)),
            tail_dropped=int(pack.get("tail_dropped", 0)),
            surplus_dropped=int(pack.get("surplus_dropped", 0)),
            max_eos_fraction=float(pack.get("max_eos_fraction", 0.0)),
            wheel_version=str(build.get("wheel_version", "")),
            sources=tuple(SourcePin.from_dict(p) for p in build.get("sources", []) or []),
            unfilled=tuple(str(p) for p in doc.get("unfilled", []) or []),
            schema_version=str(doc.get("schema_version", "")),
        )

    @classmethod
    def from_pack_result(
        cls,
        result: "PackResult",
        *,
        plan_id: str,
        prefix: str,
        digests: Mapping[str, tuple[str, int]],
        sources: Sequence[SourcePin] = (),
        bundle_id: str | None = None,
        wheel_version: str = __version__,
    ) -> "Receipt":
        """Build a receipt from what the packer actually produced.

        ``digests`` maps a shard's manifest-relative path to ``(sha256_hex, size_bytes)`` — the
        exact shape ``s3.hash_object`` returns, so a driver that streams each shard into S3 and
        hashes it on the way past can hand this straight over. The size comes from the same pass as
        the digest, so the pair describes the same bytes.

        **The one thing this refuses.** A written shard with no digest raises: a receipt whose
        ``sha256`` is absent or invented cannot be checked by ``deep=True``, which would make the
        artifact unverifiable in exactly the tier that exists to verify it. Everything else is left
        to :func:`verify_receipt`, deliberately — the constructor only sees producer numbers, and
        two producer numbers agreeing proves nothing. The verifier is the single place that compares
        them to S3.
        """
        shards: list[ShardReceipt] = []
        for ref in result.written:
            pair = digests.get(ref.path)
            if pair is None:
                raise BuildError(
                    f"no (sha256, size) recorded for {ref.path!r}, which pack() reports as written. "
                    f"A receipt with a missing digest is unverifiable by the deep tier — the only "
                    f"check in this pipeline that ever re-reads payload bytes — so it is refused "
                    f"rather than written with a hole in it."
                )
            digest, size = pair
            shards.append(
                ShardReceipt(path=ref.path, sha256=digest, tokens=int(ref.tokens), bytes=int(size))
            )
        stream = tuple(result.stream)
        return cls(
            plan_id=plan_id,
            bundle_id=bundle_id or bundle_id_for(plan_id, stream),  # type: ignore[arg-type]
            prefix=prefix.strip("/"),
            source=stream[0],
            domain=stream[1],
            split=stream[2],
            shards=tuple(shards),
            documents=int(result.documents),
            tokens_in=int(result.tokens_in),
            tokens_out=int(result.tokens_out),
            tail_dropped=int(result.tail_dropped),
            surplus_dropped=int(result.surplus_dropped),
            max_eos_fraction=float(result.max_eos_fraction),
            wheel_version=wheel_version,
            sources=tuple(sources),
            unfilled=tuple(ref.path for ref in result.unfilled),
        )


def bundle_id_for(plan_id: str, stream: tuple[str, str | None, str]) -> str:
    """A deterministic id for one bundle of work.

    Derived rather than allocated, for the same reason ``corpus.is_held_out`` is a pure function of
    the document id: a re-run of the same bundle must produce the *same* id, on any machine, at any
    concurrency, with nothing to seed. A counter would give the retry of child 37 a different id
    from child 37, and the whole point of the receipt is that a later run can tell those apart.

    Uses the counter-mode-SHA-256 house pattern (``corpus.is_held_out``, ``read._shuffle_key``).
    """
    source, domain, split = stream
    material = f"bundle|{plan_id}|{source}|{domain or ''}|{split}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------------------
# Verify one receipt
# --------------------------------------------------------------------------------------


def _join(prefix: str, path: str) -> str:
    """Receipt-relative path -> full key. Mirrors ``validate._join``."""
    p = prefix.strip("/")
    return f"{p}/{path}" if p else path


def verify_receipt(
    receipt: Receipt,
    s3: S3,
    bucket: str,
    *,
    deep: bool = False,
    hash_workers: int = 1,
) -> list[Violation]:
    """Falsify a bundle's completion claim against S3. Returns every violation found, in one pass.

    **CHEAP TIER — always, one ``head`` per shard, no payload byte read.**

    * ``receipt-shard-missing`` — the named object is not there.
    * ``receipt-size-mismatch`` — the real ``ContentLength`` is not the receipt's ``bytes``.
    * ``receipt-token-byte-mismatch`` — ``tokens * 4`` is not the size S3 reports.
    * ``receipt-seq-len-misalignment`` — the real size is not a whole multiple of ``4 * SEQ_LEN``.
    * ``receipt-tokens-out-mismatch`` — ``sum(real sizes) / 4`` is not the recorded ``tokens_out``.

    Plus the pure checks, which need no S3 at all and so are free: schema version, an empty bundle,
    duplicate paths or digests inside the bundle, shard keys that do not belong to the bundle's own
    stream, unpinned upstream revisions, and the ``PackResult`` conservation identity.

    **DEEP TIER — ``deep=True`` only.**

    * ``receipt-payload-digest-mismatch`` — ``s3.hash_object`` re-reads the object and the digest
      differs from the receipt's.

    Nothing else in this pipeline does that. ``CLAUDE.md``'s KNOWN GAP: ``s3.hash_object`` has one
    non-definition caller, ``publish.py:371`` — the producer — and Gate A's per-entry loop
    (``validate.py:391-426``) heads for the SIZE and then does set-membership on the *declared*
    digest, never re-reading payload bytes. A manifest ``sha256`` is therefore a producer assertion
    no gate falsifies. This is the place that falsifies it, and the price is a full GET of every
    shard in the bundle: ~100 MB per shard, ~1 TB across a full 10,400-shard corpus. Streamed in
    8 MiB chunks, so RAM is bounded regardless. Run it in-region, and sample bundles rather than
    sweeping all of them unless something already looks wrong.

    A corrupted payload of unchanged length passes every cheap check here AND every check at Gate A.
    Length-preserving corruption is exactly what the deep tier is for.

    **``hash_workers`` > 1 runs the deep re-hashes on a thread pool** (ignored entirely when
    ``deep`` is False — the cheap tier is one HEAD per shard and is not what costs 3 hours).
    Re-hashing is network-bound, exactly like ``publish.build_plan``'s hashing, so threads scale it
    near-linearly despite the GIL: measured live 2026-08-05 at 87.8 MB/s sustained, 3.27 h for
    10,049 shards / 1.005 TB on a 16-vCPU box with 15 cores idle. Default 1 keeps the original
    strictly-sequential path — the same code, in the same order, issuing the same calls — because a
    ``verify --deep`` verdict already stands on it (job ``507356db``: *OK 27 bundles, 10049 shards
    (payload re-hashed)*) and a performance change must not be able to invalidate it.

    Violations are collected in **submission order**, so the returned list is element-for-element
    identical at any worker count. Tests assert on ``deep[0].path`` and ``violations[0].message``;
    an as-completed collection would make those flaky and would silently reorder a report a human
    reads top-down.

    RAM stays bounded but the bound scales: ``s3.hash_object`` streams in 8 MiB chunks, so N workers
    means at most N concurrent streams ⇒ ~``8 MiB * hash_workers`` of payload buffer, plus botocore's
    per-connection buffers. At 16 workers that is ~128 MiB — trivial next to a 100 MB shard held
    whole, which is the thing the streaming exists to avoid.

    **Connection-pool ceiling (verified, not assumed).** ``Boto3S3.default()`` builds
    ``boto3.client("s3")`` with no ``botocore.config.Config``, so ``max_pool_connections`` is
    botocore's default **10** (confirmed: ``botocore.config.Config().max_pool_connections == 10`` on
    botocore 1.43.56). botocore does not pass ``block=True`` to urllib3, so exceeding it does not
    raise or block — urllib3's ``_put_conn`` *discards* the surplus connection and logs "Connection
    pool is full", meaning workers 11..N silently pay a fresh TLS handshake per shard. That is a
    throttle on the speedup, not a correctness bug, and it is why ``hash_workers`` above ~10 wants a
    client built with a matching ``max_pool_connections``; see ``corpus_build._s3``.

    **Why size and the identity are both checked, and why they can co-fire.** ``manifest.py``'s
    ``verify_arithmetic`` compares ``count.value * dtype_size`` to the *declared* ``bytes`` — two
    producer numbers agreeing. Here the identity is evaluated against the size S3 reports, so it is
    a comparison with a fact. A receipt whose ``bytes`` field is simply wrong therefore trips both
    ``receipt-size-mismatch`` and ``receipt-token-byte-mismatch``; that is two true statements about
    one lie, not double-reporting.
    """
    v: list[Violation] = []

    if receipt.schema_version not in READABLE_RECEIPT_SCHEMAS:
        # Short-circuit on purpose. Applying v1 rules to fields written under some other schema
        # produces confident findings about semantics this code does not know, which is worse than
        # declining. `read.py` refuses unknown dataset schema versions the same way.
        return [
            Violation(
                "receipt-schema-unknown",
                f"receipt declares schema_version {receipt.schema_version!r}; this verifier "
                f"understands {sorted(READABLE_RECEIPT_SCHEMAS)}. Refusing to interpret unknown "
                f"fields rather than reporting on semantics it does not know.",
            )
        ]

    v += _check_bundle_shape(receipt)
    v += _check_source_pins(receipt)
    v += _check_recorded_conservation(receipt)
    v += _check_objects(receipt, s3, bucket, deep=deep, hash_workers=hash_workers)
    return v


def _check_bundle_shape(receipt: Receipt) -> list[Violation]:
    """Pure checks over the shard list: emptiness, duplicates, and keys off the bundle's stream."""
    v: list[Violation] = []

    if not receipt.shards:
        # A bundle that claims completion having written nothing. Same failure class as
        # `corpus_pack.shard_plan`'s zero-shard refusal: skipping is silent, and a resumable driver
        # would mark this bundle done forever.
        v.append(
            Violation(
                "receipt-empty-bundle",
                f"bundle {receipt.bundle_id} ({receipt.label}) claims completion but names no "
                f"shards. A driver that treats a receipt as 'done' would never rebuild this "
                f"stream, and the corpus would publish clean with it missing.",
            )
        )

    seen_paths: set[str] = set()
    seen_sha: dict[str, str] = {}
    for shard in receipt.shards:
        if shard.path in seen_paths:
            v.append(
                Violation(
                    "receipt-duplicate-path",
                    f"{shard.path} is listed twice in this bundle; the second row's tokens and "
                    f"bytes are counted into the totals for an object that exists once.",
                    path=shard.path,
                )
            )
        seen_paths.add(shard.path)

        if shard.sha256 in seen_sha:
            # The 150B corpus shipped six held-out shards that were byte-copies of train shards —
            # 100% leakage — and Gate A caught five of six by digest, only because it was ONE group
            # (`corpus.is_held_out`). Catching it per bundle is free and happens before the copy.
            v.append(
                Violation(
                    "receipt-duplicate-shard-digest",
                    f"{shard.path} has the same sha256 as {seen_sha[shard.sha256]} — two shards of "
                    f"one bundle are byte-identical. Gate A rejects this as duplicate-shard-digest "
                    f"after the whole corpus has been copied and hashed.",
                    path=shard.path,
                )
            )
        else:
            seen_sha[shard.sha256] = shard.path

        v += _check_shard_belongs_to_stream(receipt, shard)

    return v


def _check_shard_belongs_to_stream(receipt: Receipt, shard: ShardReceipt) -> list[Violation]:
    """The shard key is REBUILT from the bundle's stream identity and compared to what is written.

    The path is not a name, it is the label: ``manifest.labels_from_path`` reads ``source`` and
    ``domain`` back out of exactly these segments, Gate A recomputes them and rejects a mismatch
    (``labels-contradict-path``), and the result is hashed into ``manifest_sha256`` — where it
    cannot be corrected without republishing the payload. So a shard filed under the wrong stream is
    not a cosmetic error; it is a permanently mislabelled slice of the mixture.
    """
    v: list[Violation] = []
    parsed = parse_shard_name(shard.path)
    if parsed is None:
        # `SHARD_RE` requires exactly five digits and no `-of-NNNNN`. A name that does not parse is
        # not recognised as belonging to any split at all (`corpus.shard_key`), so `entry.split`
        # goes unset and the object stops being split-checked — a val shard that reads as trainable.
        v.append(
            Violation(
                "receipt-shard-unparseable-name",
                f"{shard.path} does not parse as a shard name (SHARD_RE wants "
                f"'<split>-<5 digits><ext>'). parse_shard_name returns None, so nothing downstream "
                f"recognises which split it belongs to.",
                path=shard.path,
            )
        )
        return v

    split, ordinal = parsed
    try:
        expected = shard_key(receipt.source, receipt.domain, split, ordinal)
    except BuildError as exc:  # six-digit ordinal, etc.
        v.append(Violation("receipt-shard-unparseable-name", f"{shard.path}: {exc}", path=shard.path))
        return v

    if expected != shard.path or split != receipt.split:
        v.append(
            Violation(
                "receipt-shard-not-in-stream",
                f"{shard.path} does not belong to this bundle's stream {receipt.stream!r}: "
                f"shard_key() rebuilds that stream's ordinal {ordinal} as {expected!r} (split "
                f"{receipt.split!r}, got {split!r}). The path IS the label — labels_from_path reads "
                f"source/domain back out of these segments and Gate A rejects a mismatch — and it "
                f"is inside manifest_sha256, so it cannot be fixed without republishing.",
                path=shard.path,
            )
        )
    return v


def _check_source_pins(receipt: Receipt) -> list[Violation]:
    """Every upstream this bundle read must be pinned to an immutable revision.

    ``artifacts/reservoir/corpus-registry.json`` now pins a sha on every row, so the receipt has a
    real value to carry; this is what stops one from being dropped or downgraded on the way into the
    artifact. A branch name here is worse than a null, because it reads as provenance — the same
    name resolves to different bytes next month and nothing downstream sees the substitution.
    """
    v: list[Violation] = []
    for pin in receipt.sources:
        rev = (pin.revision or "").strip()
        if not rev:
            v.append(
                Violation(
                    "receipt-unpinned-source",
                    f"source {pin.key!r} ({pin.repo}) records no revision. The bundle cannot be "
                    f"reproduced: a re-download returns whatever upstream holds that day, and "
                    f"nothing downstream would notice the substitution.",
                )
            )
        elif rev.lower() in _MUTABLE_REFS or not _COMMIT_SHA_RE.match(rev.lower()):
            v.append(
                Violation(
                    "receipt-unpinned-source",
                    f"source {pin.key!r} ({pin.repo}) is pinned to {rev!r}, which is a mutable ref, "
                    f"not a commit sha. It looks like provenance and is not — the same name "
                    f"resolves to different bytes next month.",
                )
            )
    return v


def _check_recorded_conservation(receipt: Receipt) -> list[Violation]:
    """Re-assert ``PackResult``'s runtime identity on the artifact.

    ``PackResult.__post_init__`` (``corpus_pack.py:447``) raises when
    ``tokens_out + tail_dropped + surplus_dropped != tokens_in``, and that assertion dies with the
    process that ran it. A receipt loaded from JSON has never been through it — it may have been
    written by an older wheel, hand-edited, or merged from parts — so the identity is re-checked
    here on the numbers as stored. Cheap, and it is the only thing standing between a
    silently-lossy packer and a corpus that is quietly short.
    """
    v: list[Violation] = []
    accounted = receipt.tokens_out + receipt.tail_dropped + receipt.surplus_dropped
    if accounted != receipt.tokens_in:
        v.append(
            Violation(
                "receipt-conservation-broken",
                f"{receipt.label}: {receipt.tokens_in:,} tokens in, but "
                f"{receipt.tokens_out:,} out + {receipt.tail_dropped:,} tail + "
                f"{receipt.surplus_dropped:,} surplus = {accounted:,} "
                f"({receipt.tokens_in - accounted:+,} unaccounted). Every token read is written, "
                f"dropped by the tail rule, or dropped as round-down surplus; there is no fourth "
                f"channel.",
            )
        )
    if receipt.tail_dropped >= SEQ_LEN:
        v.append(
            Violation(
                "receipt-tail-over-seq-len",
                f"{receipt.label}: tail_dropped is {receipt.tail_dropped:,}, at or over SEQ_LEN "
                f"({SEQ_LEN}). The tail rule truncates to the nearest whole sequence, so its "
                f"remainder cannot reach one — a whole sequence was dropped instead.",
            )
        )
    return v


def _check_objects(
    receipt: Receipt, s3: S3, bucket: str, *, deep: bool, hash_workers: int = 1
) -> list[Violation]:
    """The S3 tier: one HEAD per shard always, one full GET per shard when ``deep``.

    **Structure, and why it is segments rather than one flat list.** The cheap tier stays strictly
    sequential — it is one HEAD per shard, it is not what costs hours, and it mutates the two
    dedupe structures below, so keeping it single-threaded means those structures are never touched
    by more than one thread and there is no race to reason about. Only the deep re-hashes fan out,
    and they are pure: ``_deep_rehash`` reads S3 and returns violations, sharing nothing.

    Output order is preserved *structurally*, not by index arithmetic. Each shard's cheap violations
    become one segment, and each deep re-hash RESERVES its segment in the same pass — at exactly the
    position the sequential code would have appended it. The segments are filled afterwards and
    flattened, so the returned list is element-for-element identical to the sequential path at any
    ``hash_workers``. That matters because callers index it (``deep[0].path``,
    ``violations[0].message``) and because a human reads the report top-down.
    """
    # Keyed by full key, NOT accumulated per row, and that distinction is load-bearing. Two receipt
    # rows for one path describe ONE object in S3, so summing per row would inflate the derived
    # total by exactly the amount a duplicated row inflates `tokens_out` — the two lies would cancel
    # and `receipt-tokens-out-mismatch` would go quiet on the case it exists to catch. (Found by
    # `test_a_path_listed_twice_is_reported`, which failed against the per-row version.) Caching
    # also means a duplicated path costs one HEAD, not two.
    observed: dict[str, int] = {}
    hashed: set[str] = set()
    all_present = True

    # Ordered output segments. `segments[i]` is one contiguous run of violations; deep slots start
    # empty and are filled below. Both are appended to only by this sequential loop.
    segments: list[list[Violation]] = []
    deep_tasks: list[tuple[int, ShardReceipt, str]] = []

    for shard in receipt.shards:
        v: list[Violation] = []
        segments.append(v)
        key = _join(receipt.prefix, shard.path)
        if key in observed:
            size = observed[key]
        else:
            try:
                size = int(s3.head(bucket, key)["size"])
            except NotFound:
                all_present = False
                v.append(
                    Violation(
                        "receipt-shard-missing",
                        f"the receipt names {shard.path} but s3://{bucket}/{key} does not exist. "
                        f"This is the commit-then-die case: a worker that wrote its receipt and "
                        f"lost its objects looks 'done' to every later run.",
                        path=shard.path,
                    )
                )
                continue
            observed[key] = size

        if size != shard.bytes:
            v.append(
                Violation(
                    "receipt-size-mismatch",
                    f"{shard.path}: receipt claims {shard.bytes:,} bytes but S3 reports {size:,} "
                    f"({size - shard.bytes:+,}). A truncated upload is the common cause and it is "
                    f"invisible to everything else — the object exists and its digest was never "
                    f"re-derived.",
                    path=shard.path,
                )
            )

        # Against the OBSERVED size, not the declared one: `bytes` and `tokens` both come from the
        # producer, so comparing them to each other proves only that the producer is self-consistent.
        if shard.tokens * DTYPE_SIZE != size:
            v.append(
                Violation(
                    "receipt-token-byte-mismatch",
                    f"{shard.path}: {shard.tokens:,} tokens x {DTYPE_SIZE} bytes = "
                    f"{shard.tokens * DTYPE_SIZE:,}, but the object is {size:,} bytes. "
                    f"manifest.verify_arithmetic recomputes exactly this identity at Gate A, and "
                    f"the manifest's count comes from this number.",
                    path=shard.path,
                )
            )

        if size % SEQ_LEN_STRIDE != 0:
            v.append(
                Violation(
                    "receipt-seq-len-misalignment",
                    f"{shard.path}: {size:,} bytes is not a whole multiple of dtype_size "
                    f"({DTYPE_SIZE}) x seq_len ({SEQ_LEN}) = {SEQ_LEN_STRIDE} — "
                    f"{size % SEQ_LEN_STRIDE} bytes over the last full sequence. "
                    f"check_seq_len_alignment rejects this at Gate A, and promote() is "
                    f"all-or-nothing, so one misaligned tail shard blocks the whole corpus.",
                    path=shard.path,
                )
            )

        # `hashed` guards the same duplicate-row case as `observed`, and here it also guards the
        # COST: the deep tier is a full GET, so re-reading a repeated path would double the most
        # expensive thing this module does to report a fact it already reported. The guard lives in
        # this sequential loop, so it still admits each key exactly once no matter how many workers
        # drain the queue afterwards — threading cannot reintroduce a double hash or a double report.
        if deep and key not in hashed:
            hashed.add(key)
            # Reserve this re-hash's place in the output NOW, so its violations land where the
            # sequential code would have put them: after this shard's cheap findings, before the
            # next shard's.
            deep_tasks.append((len(segments), shard, key))
            segments.append([])

    _run_deep_rehashes(deep_tasks, segments, s3, bucket, hash_workers=hash_workers)

    v = [item for segment in segments for item in segment]

    # The conservation cross-check, re-derived from what S3 actually holds rather than from the
    # receipt's own per-shard token counts (summing those and comparing to the recorded total would
    # be two producer numbers agreeing). Skipped when a shard is missing: the sum is then
    # incomplete by construction and would fire a second, misleading violation on top of the first.
    if all_present and receipt.shards:
        observed_total = sum(observed.values())
        derived = observed_total // DTYPE_SIZE
        if derived != receipt.tokens_out:
            v.append(
                Violation(
                    "receipt-tokens-out-mismatch",
                    f"{receipt.label}: the {len(observed)} distinct objects in S3 hold "
                    f"{observed_total:,} bytes = {derived:,} tokens, but the receipt records "
                    f"tokens_out={receipt.tokens_out:,} ({derived - receipt.tokens_out:+,}). The "
                    f"packer's conservation identity is stated over tokens_out, so a wrong "
                    f"tokens_out makes that identity meaningless.",
                )
            )
    return v


def _run_deep_rehashes(
    tasks: Sequence[tuple[int, ShardReceipt, str]],
    segments: list[list[Violation]],
    s3: S3,
    bucket: str,
    *,
    hash_workers: int,
) -> None:
    """Fill each reserved segment with its shard's re-hash result, concurrently when asked.

    Writes ``segments[slot]`` in place. Each task owns a distinct slot, allocated by the sequential
    caller, so no two workers ever touch the same list — the only shared mutable object is
    ``segments`` itself, and assigning to distinct pre-existing indices of a Python list is safe
    without a lock (the list never resizes here).

    ``hash_workers <= 1``, or a single task, takes the plain sequential path: same calls, same order,
    no pool constructed at all. That is the default and it is what the 2026-08-05 ``verify --deep``
    verdict rests on.
    """
    if not tasks:
        return
    if hash_workers > 1 and len(tasks) > 1:
        from concurrent.futures import ThreadPoolExecutor

        # Never more threads than there is work — 16 workers for 3 shards is 13 idle threads and a
        # pool the GC has to clean up.
        with ThreadPoolExecutor(max_workers=min(hash_workers, len(tasks))) as pool:
            futures = [
                (slot, pool.submit(_deep_rehash, shard, s3, bucket, key))
                for slot, shard, key in tasks
            ]
            for slot, fut in futures:
                # `.result()` re-raises in the caller's thread, so an S3Error still propagates
                # exactly as it does sequentially rather than being swallowed by the pool.
                segments[slot] = fut.result()
        return
    for slot, shard, key in tasks:
        segments[slot] = _deep_rehash(shard, s3, bucket, key)


def _deep_rehash(shard: ShardReceipt, s3: S3, bucket: str, key: str) -> list[Violation]:
    """Re-read the payload and compare the digest. THE expensive check; see :func:`verify_receipt`.

    ``s3.hash_object`` streams in 8 MiB chunks and returns ``(sha256_hex, size)`` from one pass, so
    RAM is bounded and the two facts describe the same bytes. Only the digest is compared here — the
    size it returns has already been checked against the ``head``, and a disagreement between the
    two would be an S3 consistency anomaly, not a producer lie.
    """
    try:
        actual, _size = s3.hash_object(bucket, key)
    except NotFound:  # deleted between the head and the get; already reported if it was absent
        return [
            Violation(
                "receipt-shard-missing",
                f"{shard.path} vanished between the HEAD and the deep re-read of "
                f"s3://{bucket}/{key}",
                path=shard.path,
            )
        ]
    if actual != shard.sha256:
        return [
            Violation(
                "receipt-payload-digest-mismatch",
                f"{shard.path}: re-hashed the payload and got {actual}, but the receipt claims "
                f"{shard.sha256}. The bytes are not the bytes that were written. Note that this "
                f"object passed every cheap check — length-preserving corruption is invisible to "
                f"HEAD, to the arithmetic identity, and to Gate A, which never re-reads payload.",
                path=shard.path,
            )
        ]
    return []


# --------------------------------------------------------------------------------------
# Verify a whole set of bundles — the refusal that IS the feature
# --------------------------------------------------------------------------------------


def verify_bundle_set(
    receipts: Iterable[Receipt],
    expected_streams: Iterable[tuple[str, str | None, str]],
    *,
    s3: S3 | None = None,
    bucket: str | None = None,
    deep: bool = False,
    hash_workers: int = 1,
) -> list[Violation]:
    """Refuse an incomplete or inconsistent set of bundles.

    **The refusal is the point**, and it is the same failure class as
    ``ingest_reservoir._cmd_merge``'s: a missing part yields a *smaller* result that is not an error
    anyone would notice. There, a missing shard part gives a smaller anti-join set and edu-web
    silently keeps documents that should have been removed. Here, a missing bundle gives a smaller
    corpus — the remaining shards are all valid, every count is internally consistent, Gate A passes,
    and the mixture the README names is quietly not the mixture that was built. Nothing objects
    unless something checks the *set*.

    Set-level violations, none of which any single receipt can see:

    * ``bundle-set-incomplete`` — an expected stream has no receipt.
    * ``bundle-set-unexpected-stream`` — a receipt for a stream nobody planned.
    * ``bundle-set-duplicate-stream`` — two receipts claim one stream (a retry that did not replace).
    * ``bundle-set-plan-mismatch`` — receipts from two different ``plan_id``s merged into one corpus.
    * ``bundle-set-shard-path-collision`` — two bundles claim the same key; one overwrote the other.
    * ``bundle-set-duplicate-shard-digest`` — two bundles hold byte-identical shards. **This is the
      cross-stream case, which no per-bundle check can reach**: the 150B corpus's six val shards were
      byte-copies of *train* shards, i.e. a different stream, and that is 100% leakage.
    * ``bundle-set-mixed-wheel-versions`` — the corpus was built by two different wheels.
    * ``bundle-set-source-revision-conflict`` — one upstream key pinned to two revisions.

    ``s3``/``bucket`` are optional. Given, every receipt is also run through :func:`verify_receipt`
    (honouring ``deep``) and the results are concatenated; omitted, only the set-level checks run —
    which are pure and free, so a driver can check completeness before spending a single HEAD.

    ``hash_workers`` is forwarded to each receipt's deep tier. **The pool is per-receipt, i.e. the
    fan-out is across shards WITHIN a bundle, not across bundles** — a deliberate choice, and the
    alternatives were rejected for concrete reasons:

    * *Across bundles only* would strand the speedup on this corpus's actual shape. The 27 bundles
      hold between 1 and 1,591 shards, so a 16-way per-bundle pool spends its last hours running one
      worker on the 1,591-shard tail while 15 sit idle — Amdahl on the largest bundle, which is most
      of the 3.27 h.
    * *Both levels* (a pool of bundles each spawning a pool of shards) would multiply out to
      ``workers ** 2`` concurrent streams from one flag, blowing the RAM bound and the connection
      pool at once, and would need the per-bundle results interleaved to keep order. More moving
      parts for no additional throughput: one flat 16-way fan-out already saturates the network.

    Per-receipt is therefore the simplest thing that gets the full speedup, and it keeps the
    concurrency inside the one function that owns the dedupe structures. Receipts are still verified
    in list order and their violations concatenated in that order, so the returned list is
    element-for-element identical at any worker count.
    """
    receipts = list(receipts)
    expected = list(expected_streams)
    v: list[Violation] = []

    by_stream: dict[tuple[str, str | None, str], list[Receipt]] = {}
    for r in receipts:
        by_stream.setdefault(r.stream, []).append(r)

    expected_set = set(expected)
    for stream in sorted(expected_set, key=lambda s: (s[0], s[1] or "", s[2])):
        if stream not in by_stream:
            v.append(
                Violation(
                    "bundle-set-incomplete",
                    f"no receipt for planned stream {stream!r}. Merging now yields a SMALLER corpus "
                    f"that looks entirely healthy: every remaining shard is valid, the counts are "
                    f"plausible, and Gate A passes. Re-run the failed array child first, and check "
                    f"its OUTPUTS — a worker that commits then dies leaves missing shards that "
                    f"every later run declares done.",
                )
            )
    for stream in sorted(set(by_stream) - expected_set, key=lambda s: (s[0], s[1] or "", s[2])):
        v.append(
            Violation(
                "bundle-set-unexpected-stream",
                f"a receipt claims stream {stream!r}, which is not in the expected set. Either the "
                f"plan and the driver disagree about which streams exist, or a receipt from another "
                f"build leaked into this prefix — and its shards would be published as part of this "
                f"corpus.",
            )
        )
    for stream, group in sorted(by_stream.items(), key=lambda kv: (kv[0][0], kv[0][1] or "", kv[0][2])):
        if len(group) > 1:
            v.append(
                Violation(
                    "bundle-set-duplicate-stream",
                    f"{len(group)} receipts claim stream {stream!r} "
                    f"(bundle ids {sorted(r.bundle_id for r in group)}). Nothing says which is "
                    f"authoritative, and their shard lists may differ — a retry that wrote a new "
                    f"receipt without removing the old one produces exactly this.",
                )
            )

    v += _check_set_provenance(receipts)
    v += _check_set_shards(receipts)

    if s3 is not None and bucket is not None:
        for r in receipts:
            v += verify_receipt(r, s3, bucket, deep=deep, hash_workers=hash_workers)
    return v


def _check_set_provenance(receipts: Sequence[Receipt]) -> list[Violation]:
    """One plan, one wheel, one revision per upstream — across the whole set."""
    v: list[Violation] = []

    plans = sorted({r.plan_id for r in receipts})
    if len(plans) > 1:
        v.append(
            Violation(
                "bundle-set-plan-mismatch",
                f"receipts come from {len(plans)} different plans {plans}. Ordinals are allocated "
                f"per plan (corpus.allocate_ordinals), so two plans' shards can share a key while "
                f"holding different data — and the collision is only visible to a human reading two "
                f"keys side by side.",
            )
        )

    wheels = sorted({r.wheel_version for r in receipts})
    if len(wheels) > 1:
        # Not cosmetic. CLAUDE.md gotcha 2: a wheel shipped without `families/` does not raise, it
        # falls back to each profile's laxer constant — which is how the live corpus came to be
        # validated at 50% EOS while declaring 5%. A resumed build split across two wheels has half
        # its shards gated by whichever one was older, and no other artifact records which half.
        v.append(
            Violation(
                "bundle-set-mixed-wheel-versions",
                f"bundles were built by {len(wheels)} different wheels {wheels}. The gates a bundle "
                f"passed are a property of the wheel that packed it: a wheel without families/ "
                f"silently falls back to the profile's 0.5 EOS bound instead of the family's 0.05, "
                f"and reports every shard clean. Re-run the bundles built by the older wheel.",
            )
        )

    revisions: dict[str, dict[str | None, list[str]]] = {}
    for r in receipts:
        for pin in r.sources:
            revisions.setdefault(pin.key, {}).setdefault(pin.revision, []).append(r.bundle_id)
    for key, seen in sorted(revisions.items()):
        if len(seen) > 1:
            # Name the bundles per revision, not just the count: the actionable question is "which
            # half do I rebuild", and a bare "2 revisions" makes the reader go and find out.
            detail = "; ".join(
                f"{rev or 'unpinned'} -> {sorted(ids)}"
                for rev, ids in sorted(seen.items(), key=lambda kv: str(kv[0]))
            )
            v.append(
                Violation(
                    "bundle-set-source-revision-conflict",
                    f"upstream {key!r} is pinned to {len(seen)} different revisions across bundles "
                    f"({detail}). Half the corpus was read from a different snapshot than the "
                    f"other half, and the finished shards carry no trace of which.",
                )
            )
    return v


def _check_set_shards(receipts: Sequence[Receipt]) -> list[Violation]:
    """Key collisions and cross-stream byte-identical shards."""
    v: list[Violation] = []
    owner_by_key: dict[str, str] = {}
    owner_by_sha: dict[str, str] = {}

    for r in receipts:
        for shard in r.shards:
            full = _join(r.prefix, shard.path)
            where = f"{r.bundle_id} ({r.label})"
            if full in owner_by_key:
                v.append(
                    Violation(
                        "bundle-set-shard-path-collision",
                        f"{full} is claimed by both {owner_by_key[full]} and {where}. One bundle's "
                        f"PutObject overwrote the other's, so one stream is silently short while "
                        f"both receipts read as complete.",
                        path=shard.path,
                    )
                )
            else:
                owner_by_key[full] = where

            if shard.sha256 in owner_by_sha:
                v.append(
                    Violation(
                        "bundle-set-duplicate-shard-digest",
                        f"{full} is byte-identical to {owner_by_sha[shard.sha256]}. Across streams "
                        f"this is the leakage case: a previously published corpus had six held-out "
                        f"shards that were byte-copies of train shards, 100% leakage, and Gate A "
                        f"caught five of six only because everything was in ONE group "
                        f"(corpus.is_held_out). Per-bundle checks cannot see this at all.",
                        path=shard.path,
                    )
                )
            else:
                owner_by_sha[shard.sha256] = full
    return v


# --------------------------------------------------------------------------------------
# Persist
# --------------------------------------------------------------------------------------


def write_receipt(receipt: Receipt, s3: S3, bucket: str, key: str) -> str:
    """Put a receipt at ``key``. Returns its sha256.

    Refuses a reserved basename. ``edullm-landing-manifest-created`` matches the key suffix
    ``manifest.json`` **anywhere in the bucket** with no prefix constraint (verified live
    2026-07-31, ``ingest_reservoir.py`` landmine 1), so a build artifact written under that name
    fires the validator against a prefix that has no ``dataset.json``. The guard is mechanical
    rather than a convention because the failure is invisible at write time: the PUT returns 200 and
    the damage happens afterwards.

    Checked against ``contracts.CONTROL_BASENAMES``, which is the single definition of what a
    control file is — ``ingest_reservoir`` keeps its own narrower ``_RESERVED_BASENAMES``, and a
    third copy here would be a third thing to drift.
    """
    base = key.rsplit("/", 1)[-1]
    if base in CONTROL_BASENAMES:
        raise BuildError(
            f"refusing to write a receipt at {key!r}: the basename {base!r} is a reserved control "
            f"name. `edullm-landing-manifest-created` matches the suffix `manifest.json` with NO "
            f"prefix constraint, so writing one anywhere under landing fires Gate A against a build "
            f"artifact — and the other names forge a validator marker on a real dataset prefix."
        )
    body = receipt.to_json_bytes()
    s3.put(bucket, key, body, content_type="application/json")
    return hashlib.sha256(body).hexdigest()


def read_receipt(s3: S3, bucket: str, key: str) -> Receipt:
    """Load one receipt. Raises :class:`~.corpus.BuildError` if it is unparseable or unusable."""
    try:
        doc = json.loads(s3.get(bucket, key).decode("utf-8"))
    except NotFound:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise BuildError(f"s3://{bucket}/{key} is not a readable receipt: {exc}") from exc
    return Receipt.from_dict(doc)
