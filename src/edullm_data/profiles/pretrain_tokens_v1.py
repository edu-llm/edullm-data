"""Profile ``pretrain-tokens/v1`` — packed token shards (§4, §7).

The failure this profile prevents is the audit's headline: 7,557 headerless raw-uint32
objects wearing a ``.npy`` extension, plus the silent-death modes a checksum cannot see —
an all-zeros shard, an all-EOS shard, a shard written ``uint16`` but declared ``uint32``
(which halves the count and sends ids past the vocabulary), and a crashed writer that left
a correctly-sized file with a zero-filled tail.

Every check here **recomputes against the bytes** (§0.4): it reads ~64 KB per shard at
seeded offsets via :func:`~edullm_data.profiles.base.sample_offsets` and interprets those
bytes as the *declared* dtype/byte-order — the only way to catch a dtype/endianness lie is
to decode with the declared dtype and watch the ids fly past ``vocab_size``.

The arithmetic identity (``count.value x dtype_size == bytes``) and the metadata half of
the ``.npy`` honesty rule already live in :mod:`edullm_data.manifest`; these checks are the
value-domain additions §5 says are *also* needed, not a reimplementation of them.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

import numpy as np

from ..manifest import ManifestEntry
from .base import DECODE_SAMPLE_BYTES, GroupContext, Violation, sample_offsets

NAME = "pretrain-tokens/v1"

#: Fields the group's metadata block must carry (§4 "adds required fields"). Presence is
#: declarative; the VALUES are what CHECKS recompute (§0.1 — an unchecked required field is
#: worse than an absent one, so nothing here is trusted without a byte-level counterpart).
REQUIRED_FIELDS: Mapping[str, Any] = {
    # The tokenizer is resolved from a published tokenizer/v1 dataset this group
    # depends_on; the validator derives vocab_size/eos_token_id from its tokenizer.json and
    # the decode check reads them from ctx.resolved (NOT a hand-typed block — that would
    # reintroduce the guess §0.1 warns against). A group SHOULD declare:
    #   depends_on: [{role: "tokenizer", dataset_id: "tokenizer/…", version: "vN", manifest_sha256: …}]
    # A raw declared `tokenizer` block is still accepted as a migration fallback, but the
    # derived value wins when a tokenizer dependency is present.
    #
    # Every manifest entry must declare a token count so the manifest arithmetic identity
    # (edullm_data.manifest.verify_arithmetic) is falsifiable. Enforced by
    # check_entries_declare_token_counts below.
    "entries[].count": {"type": "object", "required": ["unit", "value"], "unit": "tokens"},
    # Optional, profile-tunable bounds (all have registry defaults; declaring an absurd one
    # to pass is visible in review, §7):
    #   min_distinct_ids (default 16), max_eos_fraction (0.5), max_zero_run (256),
    #   seq_len (optional; enables the mid-sequence truncation check).
}

# Read the decode budget as four spread-out windows rather than one, so a zero-filled or
# truncated *tail* is sampled, not just a valid head (§7 "random-not-head").
_N_WINDOWS = 4
_NPY_MAGIC = b"\x93NUMPY"
_DEFAULT_MIN_DISTINCT = 16
_DEFAULT_MAX_EOS_FRACTION = 0.5
#: A run this long cannot be prose in any tokenizer; the old density default (0.5) was
#: meaningless for a vocabulary whose id 0 is a common character.
_DEFAULT_MAX_ZERO_RUN = 256


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _object_key(prefix: str, path: str) -> str:
    """Full landing key for a manifest entry. ``prefix`` is the group's landing prefix
    (``GroupContext.prefix``) and ``path`` is the manifest-relative key that already
    carries the group name, per the §5 examples (``tokens/train-00000.u32le.bin``)."""
    if not prefix:
        return path
    return prefix.rstrip("/") + "/" + path.lstrip("/")


def _shard_seed(ctx: GroupContext, path: str) -> str:
    """Per-shard seed, ``sha256(group_seed | shard_path)`` (§7 derives the sample from the
    shard path so any auditor can re-run the identical offsets)."""
    return hashlib.sha256(f"{ctx.rng_seed}:{path}".encode()).hexdigest()[:16]


def _np_dtype(entry: ManifestEntry) -> "np.dtype | None":
    """The numpy dtype the manifest *declares* — never inferred (§5: OLMo-core defaults to
    ``uint16`` while these corpora are ``uint32``, which silently halves the count)."""
    fmt = entry.format
    if fmt.dtype is None:
        return None
    try:
        base = np.dtype(fmt.dtype)
    except TypeError:
        return None
    if fmt.byte_order == "big":
        return base.newbyteorder(">")
    if fmt.byte_order == "little":
        return base.newbyteorder("<")
    return base


#: Which direction makes a bound LAXER, so a group override can be clamped against the family.
#: ``max`` means a bigger number is more permissive (``max_eos_fraction``); ``min`` means a
#: smaller one is (``min_distinct_ids``).
_BOUND_LAXER_DIRECTION = {
    "min_distinct_ids": "smaller",
    "max_eos_fraction": "larger",
    "max_zero_run": "larger",
}


def _bound(ctx: GroupContext, key: str, default: Any) -> Any:
    """Resolve a tunable bound: group override, else family default, else the profile constant.

    A group override may TIGHTEN a family bound freely but cannot LOOSEN it silently. Without
    that clamp the family bounds are decoration: a group declaring
    ``{"min_distinct_ids": 1, "max_zero_run": 10**9}`` publishes an all-zeros corpus clean —
    re-enabling by hand the exact failure the family bounds exist to forbid, and which this
    profile's own docstring claims is "visible in review" (it is one line in a group_meta block).

    Loosening is still possible, but it must be a deliberate edit to the FAMILY file, where it
    applies to everyone and shows up as a change to the standard rather than to one dataset.
    """
    fam = ctx.family_defaults.get(key)
    if key in ctx.group:
        val = ctx.group[key]
        direction = _BOUND_LAXER_DIRECTION.get(key)
        if fam is not None and direction is not None:
            try:
                if direction == "smaller":
                    return max(val, fam)  # a floor may be raised, never lowered
                return min(val, fam)  # a ceiling may be lowered, never raised
            except TypeError:
                return val  # non-comparable; schema validation owns the type error
        return val
    if fam is not None:
        return fam
    return default


def _tokenizer(ctx: GroupContext) -> Mapping[str, Any]:
    """Tokenizer facts for the vocab-range bound, in trust order:

    1. ``ctx.resolved["tokenizer"]`` — DERIVED by the validator from the published
       tokenizer.json this dataset ``depends_on``. This is the real, unfakeable source and
       is preferred whenever present (§0.4).
    2. a declared ``group.tokenizer`` block — a fallback for datasets that (legitimately or
       during migration) don't yet reference a published tokenizer; still checked against
       bytes, but the value itself is asserted, not derived.
    3. the family default.
    """
    resolved = ctx.resolved.get("tokenizer") if isinstance(ctx.resolved, Mapping) else None
    if isinstance(resolved, Mapping) and resolved.get("vocab_size"):
        return resolved
    tok = ctx.group.get("tokenizer")
    if isinstance(tok, Mapping):
        return tok
    tok = ctx.family_defaults.get("tokenizer")
    return tok if isinstance(tok, Mapping) else {}


def _cap_min_distinct_by_vocab(min_distinct: int, vocab_size: Any) -> int:
    """Never require more ID diversity than a sensible fraction of the vocabulary.

    The pretrain family floor of 256 is calibrated for ~100k BPE vocabs, where a healthy
    English shard easily exceeds 256 distinct ids in a 64 KB sample. A raw UTF-8 byte
    tokenizer has ``vocab_size=256``, so the same floor demands every byte value appear —
    including control bytes ASCII-heavy formal text (e.g. Lean) never uses. Publishers then
    interleaved full ``0..255`` alphabet markers into training shards to pass Gate A,
    contaminating the corpus.

    Cap at ``max(profile_default, vocab_size // 16)`` when vocab is known: byte LMs get 16
    (still catches all-zeros / all-one-token); large BPE vocabs keep the family floor of 256
    unchanged (``min(256, max(16, 100000 // 16)) == 256``).

    ⚠️ **RECOVERED FROM THE DEPLOYED `0.5.1` WHEEL, WHICH EXISTS IN NO COMMIT.** This function
    was live in production and present in no branch — found by installing the deployed artifact
    into a clean venv and running the live job definition's own assertion against a rebuild,
    which failed on `ImportError`. Reshipping without it would have silently REGRESSED a Gate A
    behaviour that real published datasets depend on (`pretrain/lean4-mathlib-bytes`,
    `tokenizer/bytes-utf8`), reopening the contamination it exists to prevent.

    The lesson is the reason `--version` alone is not a deployment check: `0.5.1`'s
    `__version__` string was the only *other* difference from `main`, so a version comparison
    would have called the wheel stale-but-equivalent. Diff the artifact, not the number.
    """
    if not isinstance(vocab_size, int) or isinstance(vocab_size, bool) or vocab_size <= 0:
        return min_distinct
    return min(min_distinct, max(_DEFAULT_MIN_DISTINCT, vocab_size // 16))


def _entries(ctx: GroupContext):
    """Yield ``(raw_dict, ManifestEntry | None, Violation | None)`` per manifest entry."""
    for raw in ctx.manifest.get("entries", []):
        try:
            yield raw, ManifestEntry.from_dict(raw), None
        except Exception as e:  # a structurally broken entry — manifest.py owns the schema,
            path = raw.get("path") if isinstance(raw, Mapping) else None
            yield raw, None, Violation("bad-manifest-entry", f"unparseable manifest entry: {e}", path)


def _observed_size(ctx: GroupContext, key: str) -> int:
    """The object's REAL size from S3, HEADed once per key per validation run.

    Every caller here wants the same fact about the same key, and each was issuing its own HEAD:
    ``_sampled_ids`` (decode smoke) and ``check_seq_len_alignment`` both did, on top of the
    per-entry HEAD in ``validate._validate_group``. Three HEADs per entry, so a 10,049-object
    corpus spent ~30,000 round trips discovering 10,049 sizes. Measured live on
    ``pretrain/reservoir-dolma2``: Gate A ran ~85 min at 0.3% CPU and ~15.8 round trips/s -- purely
    latency-bound, which is what pushed the first promotion attempt past its 2 h timeout.

    Cached in ``ctx.observations``, which exists for exactly this ("ephemeral, validator-owned facts
    computed while checks run") and is never serialized into dataset metadata.

    **Still recomputed, not trusted.** This caches an OBSERVATION of S3, never the producer's
    declared ``entry.bytes`` -- the whole point of HEADing is that a truncated tail gets sampled and
    checked against reality. Caching one observation per key changes the number of round trips, not
    what is compared: both callers previously saw whatever S3 returned for that key, and one HEAD
    within a single run returns the same size to both.
    """
    sizes = ctx.observations.setdefault("object_sizes", {})
    if key not in sizes:
        sizes[key] = int(ctx.s3.head(ctx.landing_bucket, key)["size"])
    return int(sizes[key])


def _decode_plan(ctx: GroupContext, entry: ManifestEntry, dtype: "np.dtype") -> tuple[str, int, list[int]]:
    """``(key, window_bytes, offsets)`` for one entry's decode sample.

    Split out of :func:`_sampled_ids` so the *offsets* can be computed on the calling thread and
    only the network reads fan out. The offsets are a pure function of ``(rng_seed, path, observed
    size, itemsize)`` — no clock, no PRNG state — so a prefetch worker and a later sequential read
    derive the identical window list, which is what makes the cache below safe to share.
    """
    key = _object_key(ctx.prefix, entry.path)
    size = _observed_size(ctx, key)
    itemsize = dtype.itemsize
    if size < itemsize:
        return key, 0, []
    window = DECODE_SAMPLE_BYTES // _N_WINDOWS
    window -= window % itemsize
    window = max(window, itemsize)
    offsets = sample_offsets(
        _shard_seed(ctx, entry.path), size, window=window, n=_N_WINDOWS, align=itemsize
    )
    return key, window, offsets


def _decode_bytes(
    ctx: GroupContext, entry: ManifestEntry, dtype: "np.dtype", prefetched: Mapping | None = None
) -> bytes:
    """The sampled bytes for one entry.

    ``prefetched`` is the current batch's ``{(key, offset): bytes}`` window map, filled
    concurrently by :func:`_prefetch_windows`. A miss falls back to reading the window here —
    which is what makes the concurrency an optimisation rather than a dependency: with
    ``prefetched=None`` this issues exactly the calls it issued before threading existed.

    **Still an observation of S3, never the producer's claim.** Whether a window arrives from a
    worker thread or from this line, it is bytes that came back from ``get_range`` against the
    real object, so a truncated or zero-filled tail is still sampled and still fails.
    """
    key, window, offsets = _decode_plan(ctx, entry, dtype)
    if not offsets:
        return b""
    out = []
    for off in offsets:
        chunk = None if prefetched is None else prefetched.get((key, off))
        if chunk is None:
            chunk = ctx.s3.get_range(ctx.landing_bucket, key, off, window)
        out.append(chunk)
    return b"".join(out)


def _sampled_ids(
    ctx: GroupContext, entry: ManifestEntry, dtype: "np.dtype", prefetched: Mapping | None = None
) -> "np.ndarray":
    """Read ~64 KB at seeded offsets and decode as ``dtype``. Uses the *actual* object size
    from HEAD (not the declared ``bytes``) so a truncated tail is sampled honestly."""
    buf = _decode_bytes(ctx, entry, dtype, prefetched)
    itemsize = dtype.itemsize
    trim = len(buf) - (len(buf) % itemsize)
    if trim <= 0:
        return np.empty(0, dtype=dtype)
    return np.frombuffer(buf[:trim], dtype=dtype)


# --------------------------------------------------------------------------------------
# Concurrent prefetch — the only place this profile touches threads
# --------------------------------------------------------------------------------------

#: Objects per prefetch batch, as a multiple of the worker count. Batching rather than warming the
#: whole manifest at once is a MEMORY bound, and it is load-bearing: the decode sample is 64 KB per
#: object, so a whole-corpus prefetch of the 40,001-object 1.0T build would hold **~2.6 GB** — a
#: third of the validator container's 8 GB (``vcpus: 4 / memory: 8192``, MEASURED on
#: ``edullm-validator:14``) — to save latency the batched form saves anyway. At 16 workers a batch
#: is 64 objects ≈ 4 MB in flight. 4 is enough to keep every worker fed across the batch boundary
#: without the tail of one batch idling the pool for long.
_PREFETCH_BATCH_MULTIPLE = 4


def _workers(ctx: GroupContext) -> int:
    """``ctx.check_workers``, defensively. ``getattr`` because a GroupContext built by an older
    caller (or a test double that mimics the dataclass) may not carry the field at all, and a
    profile must degrade to sequential rather than raise."""
    try:
        n = int(getattr(ctx, "check_workers", 1) or 1)
    except (TypeError, ValueError):
        return 1
    return max(n, 1)


def _batches(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _fan_out(ctx: GroupContext, jobs: list, fn) -> dict:
    """Run ``fn(job)`` for each job on ``check_workers`` threads; return ``{job: result}``.

    Failures are recorded as absent, never raised. A prefetch that raised would convert one
    unreadable object into a group-level ``profile-check-error`` and lose the precise per-entry
    violation the sequential path produces; instead the miss falls through to the sequential read,
    which re-issues the call and raises exactly as it does today. One extra round trip, on the
    error path only.
    """
    from concurrent.futures import ThreadPoolExecutor

    # De-duplicated, order preserved. A manifest may list the same object twice, and a duplicated
    # path must not cost two reads just because the fan-out did not look — the same guarantee
    # ``validate._prefetch_heads`` gives for HEADs, and one Gate A relies on: `duplicate-shard-digest`
    # exists precisely because duplicated entries occur in real manifests.
    jobs = list(dict.fromkeys(jobs))

    def one(job):
        try:
            return fn(job)
        except Exception:  # noqa: BLE001 - see the docstring; a miss is the designed fallback
            return None

    with ThreadPoolExecutor(max_workers=_workers(ctx)) as pool:
        results = list(pool.map(one, jobs))  # map() yields in SUBMISSION order
    return {job: r for job, r in zip(jobs, results) if r is not None}


def _prefetch_sizes(ctx: GroupContext, keys: list[str]) -> None:
    """Warm ``ctx.observations["object_sizes"]`` for ``keys``, concurrently.

    **Why this is where the win is.** Gate A is ``objects x round trips x latency`` with the CPU
    idle: MEASURED live on ``pretrain/reservoir-dolma2`` at ~85 min, **0.3% CPU**, ~15.8 round
    trips/s over 10,049 objects (recorded at :func:`_observed_size`). This profile had **zero**
    threading, so it waited for one round trip at a time for all of them. Latency per call does not
    shrink; the number you wait for serially does.

    The cache dict is created on the CALLING thread and workers only assign distinct keys into it,
    so no two threads race to build it. ``dict.setdefault`` from several threads can otherwise
    construct two dicts and silently drop one's writes.
    """
    sizes = ctx.observations.setdefault("object_sizes", {})
    todo = [k for k in dict.fromkeys(keys) if k not in sizes]
    if len(todo) <= 1 or _workers(ctx) <= 1:
        return
    got = _fan_out(ctx, todo, lambda k: int(ctx.s3.head(ctx.landing_bucket, k)["size"]))
    for k, size in got.items():
        sizes.setdefault(k, size)


def _prefetch_windows(ctx: GroupContext, plans: list[tuple[str, int, list[int]]]) -> dict:
    """Read every decode window in this batch concurrently; return ``{(key, offset): bytes}``.

    Fanned out per WINDOW rather than per object: each object needs 4 spread-out reads
    (``_N_WINDOWS``) that are independent of one another, so window granularity keeps the pool
    busy with a quarter as many objects in flight — which is what bounds memory.

    Returned rather than cached in ``ctx.observations`` on purpose. The map dies with the batch, so
    peak resident sample bytes is ``batch x 64 KB`` instead of ``corpus x 64 KB``; nothing here
    needs to outlive the loop that consumes it.
    """
    if _workers(ctx) <= 1:
        return {}
    jobs = [(key, off, window) for key, window, offsets in plans for off in offsets]
    if len(jobs) <= 1:
        return {}
    got = _fan_out(ctx, jobs, lambda j: ctx.s3.get_range(ctx.landing_bucket, j[0], j[1], j[2]))
    return {(key, off): body for (key, off, _w), body in got.items()}


def _prefetch_first_bytes(ctx: GroupContext, keys: list[str]) -> dict:
    """Read the leading 8 bytes (the ``\\x93NUMPY`` sniff) for this batch concurrently."""
    todo = list(dict.fromkeys(keys))
    if len(todo) <= 1 or _workers(ctx) <= 1:
        return {}
    return _fan_out(ctx, todo, lambda k: ctx.s3.get_range(ctx.landing_bucket, k, 0, 8))


def _batch_size(ctx: GroupContext) -> int:
    return max(_workers(ctx) * _PREFETCH_BATCH_MULTIPLE, 1)


def _entries_with_decode_windows(ctx: GroupContext):
    """``_entries`` plus, per entry, the batch window map its decode sample was prefetched into.

    A GENERATOR over batches rather than a whole-manifest warm-up, because the sample is 64 KB per
    object: warming all 40,001 objects of the 1.0T build at once would hold ~2.6 GB of a 8,192 MB
    container to save latency that batching saves anyway (see ``_PREFETCH_BATCH_MULTIPLE``).

    Order is exactly ``_entries``'s — manifest order — at every worker count. Only the *timing* of
    the reads changes; a consumer sees the identical sequence of triples it saw when this profile
    had no threading, plus a dict that is at worst empty.
    """
    it = _entries(ctx)
    size = _batch_size(ctx)
    while True:
        batch = []
        for _ in range(size):
            try:
                batch.append(next(it))
            except StopIteration:
                break
        if not batch:
            return
        windows = _warm_decode_batch(ctx, batch)
        for raw, entry, bad in batch:
            yield raw, entry, bad, windows
        if len(batch) < size:
            return


def _entries_with_first_bytes(ctx: GroupContext):
    """``_entries`` plus, per entry, the batch's ``{key: first 8 bytes}`` map."""
    it = _entries(ctx)
    size = _batch_size(ctx)
    while True:
        batch = []
        for _ in range(size):
            try:
                batch.append(next(it))
            except StopIteration:
                break
        if not batch:
            return
        heads = _prefetch_first_bytes(
            ctx, [_object_key(ctx.prefix, e.path) for _r, e, bad in batch if bad is None]
        )
        for raw, entry, bad in batch:
            yield raw, entry, bad, heads
        if len(batch) < size:
            return


def _warm_decode_batch(ctx: GroupContext, batch: list) -> dict:
    """Sizes then windows for one batch of ``(raw, entry, bad)`` triples.

    Two phases because the second depends on the first: the seeded offsets are derived from the
    object's REAL size, so every HEAD in the batch must land before any window read can be planned.
    Both phases are concurrent within themselves; the barrier between them is one batch deep, not
    one corpus deep, which is the whole reason for batching.
    """
    if _workers(ctx) <= 1:
        return {}
    decodable = [
        (entry, dt)
        for _raw, entry, bad in batch
        if bad is None
        for dt in (_np_dtype(entry),)
        if dt is not None and dt.kind in ("u", "i")
    ]
    if not decodable:
        return {}
    _prefetch_sizes(ctx, [_object_key(ctx.prefix, e.path) for e, _ in decodable])
    plans = []
    for entry, dt in decodable:
        try:
            plans.append(_decode_plan(ctx, entry, dt))
        except Exception:  # noqa: BLE001 - an unreadable size is the sequential path's to report
            continue
    return _prefetch_windows(ctx, plans)


# --------------------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------------------


def check_entries_declare_token_counts(ctx: GroupContext) -> list[Violation]:
    """Every shard must declare ``count{unit: "tokens", value}``.

    Not a byte read, but the precondition that makes the manifest arithmetic identity
    falsifiable — without a token count, ``count.value x 4 == bytes`` cannot fire, so a
    ``uint16``-as-``uint32`` size lie would sail through the cheap gate.
    """
    out: list[Violation] = []
    for raw, entry, bad in _entries(ctx):
        if bad is not None:
            out.append(bad)
            continue
        count = entry.count
        if not count or count.get("unit") != "tokens":
            out.append(
                Violation(
                    "token-count-unit",
                    f"pretrain-tokens shard must declare count.unit == 'tokens'; "
                    f"got {None if not count else count.get('unit')!r}",
                    entry.path,
                )
            )
    return out


def check_decode_smoke(ctx: GroupContext) -> list[Violation]:
    """The §7 decode smoke test: decode sampled tokens as the declared dtype and assert
    every id is in ``[0, vocab_size)``, that enough distinct ids appear, and that the EOS
    and zero fractions stay within bounds.

    Catches (per §7's table): wrong dtype / wrong endianness (ids past vocab), all-zeros or
    all-one-token shards (distinct too few), all-EOS shards, and partial zero-fill from a
    crashed writer.

    **Concurrency.** The loop below is UNCHANGED and still walks entries one at a time in manifest
    order; only the reads it waits on are batched ahead of it (``ctx.check_workers``). Every
    violation is therefore appended in manifest order at any worker count, and each decision is
    made from the same bytes for the same key — the same "prefetch facts, decide sequentially"
    split ``validate._prefetch_heads`` uses for the decision loop.
    """
    out: list[Violation] = []
    tok = _tokenizer(ctx)
    vocab_size = tok.get("vocab_size")
    eos_id = tok.get("eos_token_id")
    min_distinct = _cap_min_distinct_by_vocab(
        int(_bound(ctx, "min_distinct_ids", _DEFAULT_MIN_DISTINCT)),
        vocab_size,
    )
    max_eos = float(_bound(ctx, "max_eos_fraction", _DEFAULT_MAX_EOS_FRACTION))
    max_zero_run = int(_bound(ctx, "max_zero_run", _DEFAULT_MAX_ZERO_RUN))

    have_vocab = isinstance(vocab_size, int) and not isinstance(vocab_size, bool)
    if not have_vocab:
        out.append(
            Violation(
                "missing-tokenizer-field",
                "tokenizer.vocab_size is absent or non-integer; the vocab-range assertion "
                "cannot be recomputed (§7 decode smoke test)",
            )
        )

    for raw, entry, bad, windows in _entries_with_decode_windows(ctx):
        if bad is not None:
            out.append(bad)
            continue
        dtype = _np_dtype(entry)
        if dtype is None or dtype.kind not in ("u", "i"):
            # Not an integer token array — nothing to decode as token ids here.
            continue
        ids = _sampled_ids(ctx, entry, dtype, windows)
        n = int(ids.size)
        if n == 0:
            out.append(
                Violation("empty-shard", "no bytes sampled — shard is empty or shorter than one token", entry.path)
            )
            continue

        if have_vocab:
            n_bad = int(np.count_nonzero((ids < 0) | (ids >= vocab_size)))
            if n_bad:
                out.append(
                    Violation(
                        "vocab-out-of-range",
                        f"{n_bad}/{n} sampled ids fall outside [0, {vocab_size}); "
                        f"max sampled id = {int(ids.max())}. This is the signature of a wrong "
                        f"dtype or byte order (uint16 decoded as uint32 pushes ids past vocab)",
                        entry.path,
                    )
                )

        distinct = int(np.unique(ids).size)
        # The floor SCALES with how much was actually sampled. A bound expressed as an absolute
        # count is unsatisfiable for a shard smaller than the bound: a 5-token shard can never
        # reach 128 distinct ids, or even 16, no matter how healthy it is.
        #
        # That is not hypothetical. In the 150B corpus, 2 of 6,921 shards are 20 bytes / 5
        # tokens. Under an absolute floor they are guaranteed violations, and because promote()
        # is all-or-nothing they would block 630 GB / 157.5B tokens over 10 tokens —
        # 6.3e-9 % of the corpus. The root cause is the bound's units, not the shards.
        #
        # The floor of 2 is load-bearing. A naive ``max(n // 4, 1)`` collapses to 1 for n <= 4,
        # and a floor of 1 is vacuous — every non-empty shard has at least one distinct id, so
        # an all-one-token 5-token shard would pass. Degeneracy is precisely what this check
        # exists to catch, so it must stay catchable at every size where "degenerate" is even
        # meaningful. At n == 1 there is nothing to compare, so 1 is the honest bound there.
        #
        # Once the sample is at least 4x the declared bound the declared bound applies unchanged
        # (>= 512 sampled tokens at the family's 128).
        effective_min = min(min_distinct, max(n // 4, 2 if n > 1 else 1))
        if distinct < effective_min:
            scaled = " (scaled to this shard's sample size)" if effective_min != min_distinct else ""
            out.append(
                Violation(
                    "distinct-too-few",
                    f"only {distinct} distinct ids across {n} sampled tokens (need >= "
                    f"{effective_min}{scaled}); signature of an all-zeros or all-one-token shard",
                    entry.path,
                )
            )

        if isinstance(eos_id, int) and not isinstance(eos_id, bool):
            eos_frac = float(np.count_nonzero(ids == eos_id)) / n
            if eos_frac > max_eos:
                out.append(
                    Violation(
                        "eos-fraction-out-of-bounds",
                        f"EOS (id {eos_id}) is {eos_frac:.3f} of sampled tokens, over the "
                        f"declared max {max_eos:.3f} — signature of an all-EOS shard",
                        entry.path,
                    )
                )

        # A crashed writer leaves a CONTIGUOUS RUN of 0x00000000, so that is what to look for.
        # This used to be a density test — `count(ids == 0) / n > max_zero_fraction` — which
        # conflates "the file has a hole in it" with "the tokenizer maps a common character to
        # id 0". For dolma2, id 0 is "!", so the density form was measuring punctuation and
        # rejected two healthy prose shards of the 150B corpus at 0.0106 and 0.0108 against a
        # 0.010 bound. Their zeros were 30 scattered singletons, longest run 1: a "!" mid
        # sentence, e.g. [43096, 512, 1937, 38, 0, 2209, 430, 889, 358].
        #
        # The run form is tokenizer-independent, which is the point: no vocabulary makes a
        # thousand consecutive identical ids meaningful, whereas ANY id can be a frequent
        # token. It is also strictly more sensitive to the real failure — a 4 KiB hole inside
        # a 64 KiB sample is 6% of tokens and slipped under a 10% density bound, but its run
        # length is 1,024.
        longest_zero_run = _longest_run_of(ids, 0)
        if longest_zero_run >= max_zero_run:
            out.append(
                Violation(
                    "zero-run-in-shard",
                    f"{longest_zero_run} consecutive zero ids in the sampled window (limit "
                    f"{max_zero_run}) — the signature of a partial zero-fill from a crashed "
                    f"writer. Note this is a RUN, not a fraction: a tokenizer that maps a "
                    f"common character to id 0 (dolma2 maps '!') makes scattered zeros normal.",
                    entry.path,
                )
            )
    return out


def _longest_run_of(ids: "np.ndarray", value: int) -> int:
    """Length of the longest contiguous run of ``value`` in ``ids``.

    Vectorised because the decode sample is 16 K ids per shard across thousands of shards: a
    Python loop here would dominate Gate A's runtime for a large corpus.
    """
    hits = ids == value
    if not hits.any():
        return 0
    # Boundaries where the run state flips; the diff of their positions is each run's length.
    edges = np.flatnonzero(np.diff(np.concatenate(([0], hits.view(np.int8), [0]))))
    return int((edges[1::2] - edges[::2]).max())


def check_first_bytes_not_npy(ctx: GroupContext) -> list[Violation]:
    """The magic-byte half of the §5 honesty rule: the first bytes must not be
    ``\\x93NUMPY``.

    ``manifest.check_extension_matches_format`` catches the *metadata* lie (a ``.npy`` name
    declared ``container: raw, header_bytes: 0``). It cannot catch a writer who names a file
    ``.npy`` *and* declares ``container: npy`` — that is internally consistent, so only
    reading the leading bytes and finding a NumPy header exposes it. §5 is explicit that
    BOTH checks are needed; this is the byte-reading one, and it is the audit's actual
    7,557-object case.

    Reads are batched ahead of the loop at ``ctx.check_workers``; the loop itself is unchanged and
    still walks entries in manifest order, so the violation list is identical at any worker count.
    """
    out: list[Violation] = []
    for raw, entry, bad, heads in _entries_with_first_bytes(ctx):
        if bad is not None:
            out.append(bad)
            continue
        key = _object_key(ctx.prefix, entry.path)
        head = heads.get(key)
        if head is None:
            head = ctx.s3.get_range(ctx.landing_bucket, key, 0, 8)
        if head.startswith(_NPY_MAGIC):
            out.append(
                Violation(
                    "npy-magic-bytes",
                    "first bytes are '\\x93NUMPY' — the object carries a real NumPy header "
                    "where headerless raw tokens were declared. OLMo-core's np.memmap reader "
                    "would eat the header as leading tokens and mis-derive the count (§5)",
                    entry.path,
                )
            )
    return out


def check_seq_len_alignment(ctx: GroupContext) -> list[Violation]:
    """``bytes % (dtype_size x seq_len) == 0`` when the group declares ``seq_len``.

    Catches a shard truncated mid-sequence. If no ``seq_len`` is declared the group is not
    pre-packed into fixed sequences, so there is nothing to align — the check is skipped.
    """
    seq_len = ctx.group.get("seq_len")
    if not isinstance(seq_len, int) or isinstance(seq_len, bool) or seq_len <= 0:
        return []  # no seq_len declared: not fixed-length-packed, nothing to check.
    out: list[Violation] = []
    entries = list(_entries(ctx))
    # Sizes only — this check needs no payload bytes, so there is nothing to bound and the whole
    # group can be warmed in one fan-out. In the ordinary case (`check_decode_smoke` ran first)
    # every one of these is already cached and this issues zero calls.
    _prefetch_sizes(
        ctx, [_object_key(ctx.prefix, e.path) for _r, e, bad in entries if bad is None]
    )
    for raw, entry, bad in entries:
        if bad is not None:
            out.append(bad)
            continue
        dtype = _np_dtype(entry)
        if dtype is None:
            continue
        key = _object_key(ctx.prefix, entry.path)
        size = _observed_size(ctx, key)
        stride = dtype.itemsize * seq_len
        if stride and size % stride != 0:
            out.append(
                Violation(
                    "seq-len-misalignment",
                    f"object is {size} bytes, not a whole multiple of dtype_size "
                    f"({dtype.itemsize}) x seq_len ({seq_len}) = {stride}; a sequence is "
                    f"truncated mid-way ({size % stride} bytes over the last full sequence)",
                    entry.path,
                )
            )
    return out


CHECKS = [
    check_entries_declare_token_counts,
    check_decode_smoke,
    check_first_bytes_not_npy,
    check_seq_len_alignment,
]


# --------------------------------------------------------------------------------------
# self-registration (guarded — registry.py may not exist yet, §"registration contract")
# --------------------------------------------------------------------------------------
import sys  # noqa: E402

try:
    from . import registry as _registry

    _registry.register(sys.modules[__name__])
except Exception:
    pass
