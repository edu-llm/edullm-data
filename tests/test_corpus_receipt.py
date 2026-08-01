"""Tests for the build receipt. Mirrors `src/edullm_data/corpus_receipt.py`.

The module under test is a verifier, so the tests that matter are the NEGATIVE ones: a receipt that
passes is only interesting once each specific lie has been shown to produce its own specific,
named violation. A test asserting `verify_receipt(...) == []` on a good bundle proves nothing about
whether any check actually runs — the same assertion passes against a function that returns `[]`
unconditionally. So every check below has a paired test that breaks exactly one fact and asserts on
the violation CODE, not on "some violation was returned".

Two properties of the fixtures are load-bearing and were chosen rather than defaulted:

* **Shard bodies are real multiples of `4 * SEQ_LEN`** (32,768 bytes), built by
  `_shard_body(sequences)`. Alignment is checked with `size % 32768`, so a fixture of, say, 100
  arbitrary bytes would make the alignment test pass by accident on a body that is not aligned to
  anything — the check would be exercised trivially instead of honestly. `_misaligned_body` breaks
  that on purpose, by one uint32.
* **Bodies are content-DISTINCT**, seeded per key. A fake corpus of identical shards would make the
  duplicate-digest checks fire everywhere, and would make the deep re-hash pass for the wrong reason
  (every digest equal to every other).

`FakeS3` only — no network, no AWS, matching `s3.py`'s own discipline. Its `hash_object` reads the
stored bytes directly, so a corrupted body really does produce a different digest; the deep tier is
tested against a genuine mismatch rather than a mock.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from edullm_data import __version__
from edullm_data.corpus import DTYPE_SIZE, SEQ_LEN, BuildError, ShardRef, shard_key
from edullm_data.corpus_receipt import (
    RECEIPT_SCHEMA_VERSION,
    SEQ_LEN_STRIDE,
    Receipt,
    ShardReceipt,
    SourcePin,
    bundle_id_for,
    read_receipt,
    verify_bundle_set,
    verify_receipt,
    write_receipt,
)
from edullm_data.s3 import FakeS3, NotFound

BUCKET = "edullm-landing"
PREFIX = "pretrain/reservoir-dolma2/v1"
PLAN = "plan-2026-08-01"

#: A real 40-char commit sha, copied from `artifacts/reservoir/corpus-registry.json` (the
#: `dclm-baseline` row). Real rather than invented so the pin check is exercised against the shape
#: the registry actually produces — all 14 (repo, sha) pairs there were resolved against the HF tree
#: API on 2026-08-01, and a receipt recording `main` instead would defeat that pinning entirely.
DCLM_SHA = "a3b142c183aebe5af344955ae20836eb34dcf69b"


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def _shard_body(sequences: int, *, seed: str) -> bytes:
    """`sequences * SEQ_LEN` uint32s of content-distinct bytes — a genuinely aligned payload.

    Length is `sequences * 32768`, so `% SEQ_LEN_STRIDE == 0` holds by construction and the
    alignment check has something real to confirm. The content is a SHA-256 keystream keyed by
    `seed`, so two shards are never byte-identical unless a test asks them to be.
    """
    want = sequences * SEQ_LEN * DTYPE_SIZE
    out = bytearray()
    counter = 0
    while len(out) < want:
        out += hashlib.sha256(f"{seed}|{counter}".encode()).digest()
        counter += 1
    return bytes(out[:want])


def _misaligned_body(seed: str) -> bytes:
    """One whole sequence plus a single uint32 — 32,772 bytes, four over the stride.

    Four bytes, not four hundred: the smallest possible break of the invariant is the one a fixture
    should use, because a large remainder could also be explained by a truncated upload while a
    single trailing token can only be a packer that failed to truncate to a whole sequence.
    """
    return _shard_body(1, seed=seed) + b"\x00\x00\x00\x00"


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _receipt(
    *,
    source: str = "dclm",
    domain: str | None = None,
    split: str = "train",
    bodies: dict[str, bytes] | None = None,
    prefix: str = PREFIX,
    plan_id: str = PLAN,
    **overrides,
) -> Receipt:
    """A receipt that is TRUE about `bodies`, before any test breaks one fact of it.

    Every derived number is computed from the bodies rather than typed, so the baseline cannot
    drift out of agreement with the fixture and quietly turn a negative test into a positive one.
    """
    bodies = bodies if bodies is not None else _default_bodies(source, domain, split)
    shards = tuple(
        ShardReceipt(path=path, sha256=_sha(body), tokens=len(body) // DTYPE_SIZE, bytes=len(body))
        for path, body in bodies.items()
    )
    tokens_out = sum(s.tokens for s in shards)
    fields = {
        "plan_id": plan_id,
        "bundle_id": bundle_id_for(plan_id, (source, domain, split)),
        "prefix": prefix,
        "source": source,
        "domain": domain,
        "split": split,
        "shards": shards,
        "documents": 1_000,
        "tokens_in": tokens_out,
        "tokens_out": tokens_out,
        "tail_dropped": 0,
        "surplus_dropped": 0,
        "max_eos_fraction": 0.012,
        "wheel_version": __version__,
        "sources": (SourcePin(key=source, repo=f"vendor/{source}", revision=DCLM_SHA),),
    }
    fields.update(overrides)
    return Receipt(**fields)


def _default_bodies(source: str, domain: str | None, split: str) -> dict[str, bytes]:
    """Two shards on the bundle's own stream, sizes 2 and 1 sequences (a full one and a short tail)."""
    return {
        shard_key(source, domain, split, 0): _shard_body(2, seed=f"{source}/{split}/0"),
        shard_key(source, domain, split, 1): _shard_body(1, seed=f"{source}/{split}/1"),
    }


def _seeded(bodies: dict[str, bytes], *, prefix: str = PREFIX, bucket: str = BUCKET) -> FakeS3:
    s3 = FakeS3()
    for path, body in bodies.items():
        s3.seed(bucket, f"{prefix}/{path}" if prefix else path, body)
    return s3


def _codes(violations) -> list[str]:
    return [v.code for v in violations]


# --------------------------------------------------------------------------------------
# baseline: the honest bundle
# --------------------------------------------------------------------------------------


def test_an_honest_bundle_is_clean_cheap_and_deep():
    bodies = _default_bodies("dclm", None, "train")
    receipt = _receipt(bodies=bodies)
    s3 = _seeded(bodies)

    assert verify_receipt(receipt, s3, BUCKET) == []
    assert verify_receipt(receipt, s3, BUCKET, deep=True) == []


def test_the_fixture_bodies_are_genuinely_seq_len_aligned_and_distinct():
    """Guards the fixtures themselves: the alignment test must not pass by accident.

    If `_shard_body` produced an arbitrary length, `receipt-seq-len-misalignment` would fire on the
    baseline and every "no violation" assertion above would be measuring the wrong thing.
    """
    bodies = _default_bodies("dclm", None, "train")
    for path, body in bodies.items():
        assert len(body) % SEQ_LEN_STRIDE == 0, path
        assert len(body) > 0
    assert len({_sha(b) for b in bodies.values()}) == len(bodies)
    assert SEQ_LEN_STRIDE == DTYPE_SIZE * SEQ_LEN == 32_768


# --------------------------------------------------------------------------------------
# cheap tier: the five checks a HEAD can settle
# --------------------------------------------------------------------------------------


def test_a_named_shard_that_does_not_exist_is_reported_as_missing():
    """Commit-then-die: the receipt exists, the object does not, every later run says 'done'."""
    bodies = _default_bodies("dclm", None, "train")
    receipt = _receipt(bodies=bodies)
    s3 = _seeded(bodies)
    s3.delete(BUCKET, f"{PREFIX}/{shard_key('dclm', None, 'train', 1)}")

    violations = verify_receipt(receipt, s3, BUCKET)

    assert "receipt-shard-missing" in _codes(violations)
    missing = [v for v in violations if v.code == "receipt-shard-missing"]
    assert len(missing) == 1
    assert missing[0].path == shard_key("dclm", None, "train", 1)


def test_a_real_size_that_disagrees_with_the_receipt_is_reported():
    """The receipt's `bytes` is compared to a real `head`, not to its own `tokens`."""
    bodies = _default_bodies("dclm", None, "train")
    receipt = _receipt(bodies=bodies)
    s3 = _seeded(bodies)
    # Truncate by one whole sequence: still aligned, still a plausible shard, wrong size. Chosen so
    # ONLY the size-derived checks fire and the alignment check stays quiet — otherwise the test
    # could not tell which check caught it.
    truncated = _shard_body(1, seed="dclm/train/0")
    s3.seed(BUCKET, f"{PREFIX}/{shard_key('dclm', None, 'train', 0)}", truncated)

    codes = _codes(verify_receipt(receipt, s3, BUCKET))

    assert "receipt-size-mismatch" in codes
    assert "receipt-seq-len-misalignment" not in codes


def test_a_truncated_shard_also_breaks_the_token_byte_identity():
    """`tokens * 4 == bytes` is evaluated against the OBSERVED size, so a truncation trips it too.

    Both codes firing on one lie is correct and deliberate: the receipt's `bytes` disagrees with S3
    (one true statement) and its `tokens` no longer describe the object (a second). Comparing
    `tokens * 4` to the declared `bytes` instead would leave the second silent — two producer
    numbers agreeing proves only that the producer is self-consistent.
    """
    bodies = _default_bodies("dclm", None, "train")
    receipt = _receipt(bodies=bodies)
    s3 = _seeded(bodies)
    s3.seed(BUCKET, f"{PREFIX}/{shard_key('dclm', None, 'train', 0)}", _shard_body(1, seed="short"))

    codes = _codes(verify_receipt(receipt, s3, BUCKET))

    assert "receipt-token-byte-mismatch" in codes
    assert "receipt-size-mismatch" in codes


def test_a_token_count_that_does_not_match_the_bytes_is_reported_on_its_own():
    """A receipt whose `tokens` is wrong while `bytes` is right: only the identity fires."""
    bodies = {shard_key("dclm", None, "train", 0): _shard_body(2, seed="only")}
    receipt = _receipt(bodies=bodies)
    body = next(iter(bodies.values()))
    bad = ShardReceipt(
        path=receipt.shards[0].path,
        sha256=receipt.shards[0].sha256,
        tokens=receipt.shards[0].tokens - 1,  # off by one token: 4 bytes of lie
        bytes=len(body),
    )
    receipt = _receipt(bodies=bodies, shards=(bad,), tokens_in=bad.tokens, tokens_out=bad.tokens)
    s3 = _seeded(bodies)

    codes = _codes(verify_receipt(receipt, s3, BUCKET))

    assert "receipt-token-byte-mismatch" in codes
    assert "receipt-size-mismatch" not in codes
    # `tokens_out` was made consistent with the lie, so the S3-derived cross-check catches it too —
    # that is the check surviving a receipt whose internal arithmetic was made to agree with itself.
    assert "receipt-tokens-out-mismatch" in codes


def test_a_byte_length_not_aligned_to_the_stride_is_reported():
    """Four bytes over one sequence. Gate A rejects this AFTER the whole corpus has been copied."""
    path = shard_key("dclm", None, "train", 0)
    body = _misaligned_body("tail")
    bodies = {path: body}
    receipt = _receipt(bodies=bodies)
    s3 = _seeded(bodies)

    violations = verify_receipt(receipt, s3, BUCKET)
    codes = _codes(violations)

    assert "receipt-seq-len-misalignment" in codes
    # The receipt is otherwise entirely truthful — the size matches, the identity holds, the digest
    # is right. Nothing but the alignment check can see this.
    assert "receipt-size-mismatch" not in codes
    assert "receipt-token-byte-mismatch" not in codes
    assert len(body) % SEQ_LEN_STRIDE == DTYPE_SIZE
    assert str(SEQ_LEN_STRIDE) in violations[0].message


def test_tokens_out_is_re_derived_from_the_sizes_in_s3():
    """The conservation cross-check ignores the receipt's own per-shard counts.

    `sum(real bytes) / 4` is compared to the recorded `tokens_out`; summing `shard.tokens` instead
    would be two producer numbers agreeing. Here every per-shard row is honest and only the total
    is wrong, which is precisely the case a per-shard loop cannot see.
    """
    bodies = _default_bodies("dclm", None, "train")
    honest = _receipt(bodies=bodies)
    receipt = _receipt(
        bodies=bodies,
        tokens_out=honest.tokens_out - SEQ_LEN,
        tokens_in=honest.tokens_in - SEQ_LEN,
    )
    s3 = _seeded(bodies)

    codes = _codes(verify_receipt(receipt, s3, BUCKET))

    assert "receipt-tokens-out-mismatch" in codes
    assert "receipt-size-mismatch" not in codes
    assert "receipt-token-byte-mismatch" not in codes


def test_the_tokens_out_cross_check_is_skipped_when_a_shard_is_missing():
    """A missing object makes the derived sum incomplete by construction.

    Reporting `receipt-tokens-out-mismatch` on top of `receipt-shard-missing` would be a second,
    misleading finding about the same fact — the total is short because an object is gone, not
    because the packer miscounted.
    """
    bodies = _default_bodies("dclm", None, "train")
    receipt = _receipt(bodies=bodies)
    s3 = _seeded(bodies)
    s3.delete(BUCKET, f"{PREFIX}/{shard_key('dclm', None, 'train', 0)}")

    codes = _codes(verify_receipt(receipt, s3, BUCKET))

    assert codes == ["receipt-shard-missing"]


def test_the_cheap_tier_never_reads_a_payload_byte():
    """`deep=False` issues HEADs only — the property that makes it affordable per bundle.

    Asserted by execution rather than by docstring: a subclass that raises on `get`/`get_range`/
    `hash_object` proves the claim the way `fsck.py`'s "never a payload byte" would be proved.
    """

    class HeadOnly(FakeS3):
        def get(self, bucket, key):  # pragma: no cover - the assertion is that it is not called
            raise AssertionError(f"cheap tier read the payload of {key}")

        def get_range(self, bucket, key, start, length):  # pragma: no cover
            raise AssertionError(f"cheap tier ranged-read {key}")

        def hash_object(self, bucket, key):  # pragma: no cover
            raise AssertionError(f"cheap tier re-hashed {key}")

    bodies = _default_bodies("dclm", None, "train")
    s3 = HeadOnly()
    for path, body in bodies.items():
        s3.seed(BUCKET, f"{PREFIX}/{path}", body)

    assert verify_receipt(_receipt(bodies=bodies), s3, BUCKET) == []


# --------------------------------------------------------------------------------------
# deep tier: the one re-hash in the pipeline
# --------------------------------------------------------------------------------------


def test_deep_catches_a_corrupted_payload_that_passes_every_cheap_check():
    """Length-preserving corruption. THE case for `deep=True`.

    The replacement body is the same length, so the size matches, `tokens * 4 == bytes` holds, the
    alignment holds, and the S3-derived `tokens_out` holds. Gate A would also pass it: per
    `CLAUDE.md`'s KNOWN GAP its per-entry loop HEADs for the size and does set-membership on the
    DECLARED digest, never re-reading payload. Only the re-hash sees it.
    """
    path = shard_key("dclm", None, "train", 0)
    bodies = {path: _shard_body(2, seed="original")}
    receipt = _receipt(bodies=bodies)
    s3 = _seeded(bodies)

    corrupted = _shard_body(2, seed="replaced")
    assert len(corrupted) == len(bodies[path])  # same length, different bytes
    s3.seed(BUCKET, f"{PREFIX}/{path}", corrupted)

    assert verify_receipt(receipt, s3, BUCKET) == []  # every cheap check passes

    deep = verify_receipt(receipt, s3, BUCKET, deep=True)
    assert _codes(deep) == ["receipt-payload-digest-mismatch"]
    assert deep[0].path == path
    assert _sha(corrupted) in deep[0].message
    assert receipt.shards[0].sha256 in deep[0].message


def test_deep_reports_a_shard_that_vanishes_between_the_head_and_the_get():
    """The window between the two calls is real; it must not surface as a traceback."""
    path = shard_key("dclm", None, "train", 0)
    bodies = {path: _shard_body(1, seed="racy")}
    receipt = _receipt(bodies=bodies)
    key = f"{PREFIX}/{path}"

    class VanishesAfterHead(FakeS3):
        def hash_object(self, bucket, k):
            raise NotFound(f"s3://{bucket}/{k}")

    s3 = VanishesAfterHead()
    s3.seed(BUCKET, key, bodies[path])

    assert _codes(verify_receipt(receipt, s3, BUCKET, deep=True)) == ["receipt-shard-missing"]


def test_deep_does_not_re_report_a_size_the_head_already_settled():
    """`hash_object` returns `(digest, size)` and only the digest is compared.

    The size it returns describes the same bytes the digest does, and the HEAD already checked the
    size against the receipt — comparing it twice would emit two findings for one fact.
    """
    path = shard_key("dclm", None, "train", 0)
    bodies = {path: _shard_body(2, seed="sized")}
    receipt = _receipt(bodies=bodies)
    s3 = _seeded(bodies)
    s3.seed(BUCKET, f"{PREFIX}/{path}", _shard_body(1, seed="sized-short"))

    codes = _codes(verify_receipt(receipt, s3, BUCKET, deep=True))

    assert codes.count("receipt-payload-digest-mismatch") == 1
    assert codes.count("receipt-size-mismatch") == 1


# --------------------------------------------------------------------------------------
# pure checks: bundle shape, stream membership, provenance, conservation
# --------------------------------------------------------------------------------------


def test_a_bundle_claiming_completion_with_no_shards_is_refused():
    """Same failure class as `shard_plan`'s zero-shard refusal: skipping is silent and permanent."""
    receipt = _receipt(bodies={}, tokens_in=0, tokens_out=0)

    codes = _codes(verify_receipt(receipt, FakeS3(), BUCKET))

    assert codes == ["receipt-empty-bundle"]


def test_two_byte_identical_shards_inside_one_bundle_are_reported():
    """The leakage signature, caught before the copy rather than by Gate A after it."""
    body = _shard_body(1, seed="same")
    bodies = {
        shard_key("dclm", None, "train", 0): body,
        shard_key("dclm", None, "train", 1): body,
    }
    receipt = _receipt(bodies=bodies)
    s3 = _seeded(bodies)

    codes = _codes(verify_receipt(receipt, s3, BUCKET))

    assert codes == ["receipt-duplicate-shard-digest"]


def test_a_path_listed_twice_is_reported():
    """A duplicated row counts one object's tokens and bytes twice into the totals."""
    path = shard_key("dclm", None, "train", 0)
    body = _shard_body(1, seed="dup")
    row = ShardReceipt(path=path, sha256=_sha(body), tokens=len(body) // DTYPE_SIZE, bytes=len(body))
    receipt = _receipt(
        bodies={path: body},
        shards=(row, row),
        tokens_in=row.tokens * 2,
        tokens_out=row.tokens * 2,
    )
    s3 = _seeded({path: body})

    codes = _codes(verify_receipt(receipt, s3, BUCKET))

    assert "receipt-duplicate-path" in codes
    assert "receipt-duplicate-shard-digest" in codes
    # And the S3-derived total catches the inflation: one object, two rows' worth of tokens claimed.
    assert "receipt-tokens-out-mismatch" in codes


def test_a_shard_filed_under_another_source_is_reported():
    """The path IS the label. `shard_key` is rebuilt from the bundle's stream and compared."""
    bodies = {
        shard_key("dclm", None, "train", 0): _shard_body(1, seed="ok"),
        shard_key("finemath", None, "train", 1): _shard_body(1, seed="wrong-source"),
    }
    receipt = _receipt(source="dclm", bodies=bodies)
    s3 = _seeded(bodies)

    violations = verify_receipt(receipt, s3, BUCKET)

    offenders = [v for v in violations if v.code == "receipt-shard-not-in-stream"]
    assert len(offenders) == 1
    assert offenders[0].path == shard_key("finemath", None, "train", 1)
    assert "labels_from_path" in offenders[0].message


def test_a_val_shard_inside_a_train_bundle_is_reported():
    """Split is compared too — a val shard in a train bundle is a trainable-looking held-out shard."""
    bodies = {
        shard_key("dclm", None, "train", 0): _shard_body(1, seed="t"),
        shard_key("dclm", None, "val", 1): _shard_body(1, seed="v"),
    }
    receipt = _receipt(source="dclm", split="train", bodies=bodies)
    s3 = _seeded(bodies)

    offenders = [
        v for v in verify_receipt(receipt, s3, BUCKET) if v.code == "receipt-shard-not-in-stream"
    ]

    assert [v.path for v in offenders] == [shard_key("dclm", None, "val", 1)]


def test_a_domain_bearing_stream_round_trips_through_shard_key():
    """A two-level key (`tokens/<source>/<domain>/...`) is accepted, not mistaken for a foreign one."""
    bodies = _default_bodies("stackexchange", "physics", "train")
    receipt = _receipt(source="stackexchange", domain="physics", bodies=bodies)
    s3 = _seeded(bodies)

    assert verify_receipt(receipt, s3, BUCKET) == []
    assert all("/physics/" in path for path in bodies)


def test_a_name_that_is_not_a_shard_name_is_reported():
    """`-of-NNNNN` is excluded by SHARD_RE, so `parse_shard_name` returns None and split is unset."""
    path = "tokens/dclm/train-00000-of-00042.u32le.bin"
    body = _shard_body(1, seed="of")
    receipt = _receipt(
        bodies={path: body},
        shards=(
            ShardReceipt(
                path=path, sha256=_sha(body), tokens=len(body) // DTYPE_SIZE, bytes=len(body)
            ),
        ),
        tokens_in=len(body) // DTYPE_SIZE,
        tokens_out=len(body) // DTYPE_SIZE,
    )
    s3 = _seeded({path: body})

    codes = _codes(verify_receipt(receipt, s3, BUCKET))

    assert codes == ["receipt-shard-unparseable-name"]


@pytest.mark.parametrize("revision", [None, "", "main", "master", "refs/heads/main", "v1.0"])
def test_an_unpinned_upstream_revision_is_reported(revision):
    """A branch name reads as provenance and is not: the same name resolves to different bytes.

    `artifacts/reservoir/corpus-registry.json` pins a real 40-char sha on every row precisely so
    this cannot happen; the check is what keeps a receipt from quietly undoing that.
    """
    bodies = _default_bodies("dclm", None, "train")
    receipt = _receipt(
        bodies=bodies,
        sources=(SourcePin(key="dclm", repo="mlfoundations/dclm-baseline-1.0", revision=revision),),
    )
    s3 = _seeded(bodies)

    codes = _codes(verify_receipt(receipt, s3, BUCKET))

    assert codes == ["receipt-unpinned-source"]


def test_a_real_registry_sha_is_accepted():
    """The positive half of the pin check, against the exact string the registry holds."""
    bodies = _default_bodies("dclm", None, "train")
    receipt = _receipt(
        bodies=bodies,
        sources=(
            SourcePin(key="dclm", repo="mlfoundations/dclm-baseline-1.0", revision=DCLM_SHA),
            SourcePin(
                key="fineweb-edu",
                repo="HuggingFaceFW/fineweb-edu",
                revision="87f09149ef4734204d70ed1d046ddc9ca3f2b8f9",
                config="sample-100BT",
            ),
        ),
    )

    assert verify_receipt(receipt, _seeded(bodies), BUCKET) == []


def test_the_pack_conservation_identity_is_re_asserted_on_the_artifact():
    """`PackResult.__post_init__` dies with its process; a loaded receipt has never been through it."""
    bodies = _default_bodies("dclm", None, "train")
    honest = _receipt(bodies=bodies)
    doc = honest.to_dict()
    doc["pack"]["tokens_in"] = honest.tokens_out + 4_096  # 4,096 tokens vanish into no channel
    receipt = Receipt.from_dict(doc)

    codes = _codes(verify_receipt(receipt, _seeded(bodies), BUCKET))

    assert "receipt-conservation-broken" in codes


def test_a_tail_drop_of_a_whole_sequence_is_reported():
    """The tail rule truncates to the nearest whole sequence, so its remainder cannot reach one."""
    bodies = _default_bodies("dclm", None, "train")
    honest = _receipt(bodies=bodies)
    receipt = _receipt(
        bodies=bodies,
        tail_dropped=SEQ_LEN,
        tokens_in=honest.tokens_out + SEQ_LEN,  # keeps conservation true, isolating the tail check
    )

    codes = _codes(verify_receipt(receipt, _seeded(bodies), BUCKET))

    assert codes == ["receipt-tail-over-seq-len"]


def test_an_unknown_schema_version_short_circuits():
    """v1 rules applied to unknown fields would produce confident findings about unknown semantics."""
    bodies = _default_bodies("dclm", None, "train")
    receipt = _receipt(bodies=bodies, schema_version="edullm-corpus-receipt/v99")

    violations = verify_receipt(receipt, FakeS3(), BUCKET)  # empty S3: nothing else could pass

    assert _codes(violations) == ["receipt-schema-unknown"]


# --------------------------------------------------------------------------------------
# the set: refusing an incomplete corpus
# --------------------------------------------------------------------------------------

STREAMS = [
    ("dclm", None, "train"),
    ("dclm", None, "val"),
    ("finemath", None, "train"),
]


def _bundle_set(streams=STREAMS):
    """One honest receipt per stream, plus the S3 that backs them."""
    receipts = []
    all_bodies: dict[str, bytes] = {}
    for source, domain, split in streams:
        bodies = {
            shard_key(source, domain, split, 0): _shard_body(1, seed=f"{source}/{domain}/{split}"),
        }
        all_bodies.update(bodies)
        receipts.append(_receipt(source=source, domain=domain, split=split, bodies=bodies))
    return receipts, _seeded(all_bodies)


def test_a_complete_bundle_set_is_clean():
    receipts, s3 = _bundle_set()

    assert verify_bundle_set(receipts, STREAMS, s3=s3, bucket=BUCKET, deep=True) == []


def test_a_missing_bundle_is_refused_and_that_refusal_is_the_feature():
    """Every remaining shard is valid and every count is consistent — only the SET check objects.

    Same failure class as `ingest_reservoir._cmd_merge`'s: a missing part yields a smaller result
    that looks entirely healthy. Asserted literally here — the surviving receipts are individually
    clean, so nothing per-bundle could ever have caught this.
    """
    receipts, s3 = _bundle_set()
    dropped = receipts.pop(1)

    for r in receipts:
        assert verify_receipt(r, s3, BUCKET) == []  # each survivor is spotless

    violations = verify_bundle_set(receipts, STREAMS, s3=s3, bucket=BUCKET)

    assert _codes(violations) == ["bundle-set-incomplete"]
    assert str(dropped.stream) in violations[0].message


def test_a_receipt_for_a_stream_nobody_planned_is_reported():
    """A receipt leaked in from another build would publish its shards as part of this corpus."""
    receipts, s3 = _bundle_set()
    # `pes2o`, lowercase: `manifest.SAFE_SEGMENT_RE` is `[a-z0-9-]`, and a fixture that used the
    # upstream's mixed-case `peS2o` would be modelling a source label `CorpusSpec` already refuses.
    stray_bodies = {shard_key("pes2o", None, "train", 0): _shard_body(1, seed="stray")}
    receipts.append(_receipt(source="pes2o", bodies=stray_bodies))

    codes = _codes(verify_bundle_set(receipts, STREAMS))

    assert codes == ["bundle-set-unexpected-stream"]


def test_two_receipts_for_one_stream_are_reported():
    """A retry that wrote a new receipt without removing the old one. Nothing says which wins."""
    receipts, s3 = _bundle_set()
    receipts.append(receipts[0])

    codes = _codes(verify_bundle_set(receipts, STREAMS))

    assert "bundle-set-duplicate-stream" in codes


def test_receipts_from_two_plans_are_reported():
    """Ordinals are allocated per plan, so two plans' shards can share a key holding different data."""
    receipts, s3 = _bundle_set()
    doc = receipts[0].to_dict()
    doc["plan_id"] = "plan-from-last-week"
    receipts[0] = Receipt.from_dict(doc)

    codes = _codes(verify_bundle_set(receipts, STREAMS))

    assert "bundle-set-plan-mismatch" in codes


def test_a_corpus_built_by_two_wheels_is_reported():
    """CLAUDE.md gotcha 2: a wheel without families/ gates at 0.5 EOS and reports every shard clean."""
    receipts, s3 = _bundle_set()
    doc = receipts[0].to_dict()
    doc["build"]["wheel_version"] = "0.2.0"
    receipts[0] = Receipt.from_dict(doc)

    violations = verify_bundle_set(receipts, STREAMS)

    assert _codes(violations) == ["bundle-set-mixed-wheel-versions"]
    assert "0.2.0" in violations[0].message
    assert "families/" in violations[0].message


def test_one_upstream_pinned_to_two_revisions_is_reported():
    """Half the corpus read from a different snapshot; the finished shards carry no trace of which."""
    receipts, s3 = _bundle_set()
    doc = receipts[0].to_dict()
    doc["build"]["sources"] = [
        {"key": "shared", "repo": "vendor/shared", "revision": DCLM_SHA},
    ]
    receipts[0] = Receipt.from_dict(doc)
    other = receipts[1].to_dict()
    other["build"]["sources"] = [
        {
            "key": "shared",
            "repo": "vendor/shared",
            "revision": "87f09149ef4734204d70ed1d046ddc9ca3f2b8f9",
        },
    ]
    receipts[1] = Receipt.from_dict(other)

    codes = _codes(verify_bundle_set(receipts, STREAMS))

    assert "bundle-set-source-revision-conflict" in codes


def test_two_bundles_claiming_one_key_are_reported():
    """One bundle's PutObject overwrote the other's; both receipts still read as complete."""
    receipts, s3 = _bundle_set()
    collided = shard_key("dclm", None, "train", 0)
    body = _shard_body(1, seed="collide")
    doc = receipts[2].to_dict()  # the finemath bundle, re-pointed at dclm's key
    doc["shards"] = [
        {
            "path": collided,
            "sha256": _sha(body),
            "tokens": len(body) // DTYPE_SIZE,
            "bytes": len(body),
        }
    ]
    receipts[2] = Receipt.from_dict(doc)

    codes = _codes(verify_bundle_set(receipts, STREAMS))

    assert "bundle-set-shard-path-collision" in codes


def test_byte_identical_shards_across_two_bundles_are_reported():
    """THE cross-stream leakage case, which no per-bundle check can reach.

    The 150B corpus's six val shards were byte-copies of TRAIN shards — a different stream. Each
    bundle here is individually spotless; only comparing digests across bundles sees it.
    """
    shared = _shard_body(1, seed="leaked")
    train_bodies = {shard_key("dclm", None, "train", 0): shared}
    val_bodies = {shard_key("dclm", None, "val", 1): shared}
    receipts = [
        _receipt(source="dclm", split="train", bodies=train_bodies),
        _receipt(source="dclm", split="val", bodies=val_bodies),
    ]
    s3 = _seeded({**train_bodies, **val_bodies})
    streams = [("dclm", None, "train"), ("dclm", None, "val")]

    for r in receipts:
        assert verify_receipt(r, s3, BUCKET) == []  # each bundle is clean on its own

    codes = _codes(verify_bundle_set(receipts, streams, s3=s3, bucket=BUCKET))

    assert codes == ["bundle-set-duplicate-shard-digest"]


def test_the_set_checks_run_without_s3_at_all():
    """Completeness is pure, so a driver can check it before spending a single HEAD."""
    receipts, _ = _bundle_set()
    receipts.pop()

    codes = _codes(verify_bundle_set(receipts, STREAMS))

    assert codes == ["bundle-set-incomplete"]


def test_per_receipt_violations_are_concatenated_when_s3_is_supplied():
    receipts, s3 = _bundle_set()
    s3.delete(BUCKET, f"{PREFIX}/{shard_key('dclm', None, 'train', 0)}")

    codes = _codes(verify_bundle_set(receipts, STREAMS, s3=s3, bucket=BUCKET))

    assert codes == ["receipt-shard-missing"]


# --------------------------------------------------------------------------------------
# schema: round-trip, construction from a PackResult, persistence
# --------------------------------------------------------------------------------------


def test_a_receipt_round_trips_through_json_unchanged():
    receipt = _receipt(source="stackexchange", domain="physics")

    revived = Receipt.from_dict(json.loads(receipt.to_json_bytes().decode("utf-8")))

    assert revived == receipt
    assert revived.receipt_sha256() == receipt.receipt_sha256()


def test_the_canonical_bytes_do_not_depend_on_field_insertion_order():
    """`canonical_json` sorts keys, so the digest is usable as an idempotency key by a resumed run."""
    receipt = _receipt()
    shuffled = dict(reversed(list(receipt.to_dict().items())))

    assert Receipt.from_dict(shuffled).receipt_sha256() == receipt.receipt_sha256()


def test_a_bundle_id_is_derived_not_allocated():
    """A retry of child 37 must get child 37's id — a counter would give it a new one."""
    a = bundle_id_for(PLAN, ("dclm", None, "train"))
    b = bundle_id_for(PLAN, ("dclm", None, "train"))

    assert a == b
    assert a != bundle_id_for(PLAN, ("dclm", None, "val"))
    assert a != bundle_id_for("another-plan", ("dclm", None, "train"))
    assert a != bundle_id_for(PLAN, ("dclm", "physics", "train"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha256", "not-hex"),
        ("sha256", "ABCD" * 16),  # uppercase: `_SHA256_HEX_RE` is lowercase-only, like ManifestEntry
        ("tokens", -1),
        ("bytes", -1),
        ("tokens", True),  # bool is an int subclass; ManifestEntry rejects it for the same reason
        ("path", ""),
        ("path", "/absolute/key"),
    ],
)
def test_a_structurally_unusable_shard_row_fails_to_load(field, value):
    """A LOAD failure, not a violation — there is nothing useful to report about unreadable fields."""
    good = {
        "path": "tokens/dclm/train-00000.u32le.bin",
        "sha256": "a" * 64,
        "tokens": SEQ_LEN,
        "bytes": SEQ_LEN * DTYPE_SIZE,
    }
    good[field] = value

    with pytest.raises(BuildError):
        ShardReceipt.from_dict(good)


def test_a_receipt_document_with_no_stream_block_fails_to_load():
    with pytest.raises(BuildError, match="stream"):
        Receipt.from_dict({"schema_version": RECEIPT_SCHEMA_VERSION, "plan_id": "p"})


class _FakePackResult:
    """Duck-typed `PackResult`: same fields, no numpy import, no family-file resolution.

    `corpus_pack` reads `families/pretrain.json` at pack time and imports numpy at module scope;
    neither is needed to test that a receipt is assembled correctly from a result's fields, and
    requiring them would make this test depend on the packer's environment rather than its shape.
    The field names are asserted against the real dataclass below, so the double cannot drift.
    """

    def __init__(self, stream, written, unfilled=(), **numbers):
        self.stream = stream
        self.written = tuple(written)
        self.unfilled = tuple(unfilled)
        for name, value in numbers.items():
            setattr(self, name, value)


def test_the_fake_pack_result_carries_every_field_the_real_one_does():
    """Guards the double: a field added to `PackResult` must be considered here, not silently missed."""
    import dataclasses

    from edullm_data.corpus_pack import PackResult

    expected = {f.name for f in dataclasses.fields(PackResult)}
    assert expected == {
        "stream",
        "written",
        "unfilled",
        "documents",
        "tokens_in",
        "tokens_out",
        "tail_dropped",
        "surplus_dropped",
        "max_eos_fraction",
    }


def test_a_receipt_is_built_from_a_pack_result_and_verifies_against_s3():
    """The producer path end to end: pack result + digests -> receipt -> a clean verify."""
    stream = ("dclm", None, "train")
    bodies = {
        shard_key(*stream, 0): _shard_body(2, seed="pr0"),
        shard_key(*stream, 1): _shard_body(1, seed="pr1"),  # a short tail, realized not planned
    }
    written = [
        ShardRef(source="dclm", domain=None, split="train", ordinal=0, tokens=2 * SEQ_LEN),
        ShardRef(source="dclm", domain=None, split="train", ordinal=1, tokens=SEQ_LEN),
    ]
    total = 3 * SEQ_LEN
    result = _FakePackResult(
        stream,
        written,
        unfilled=[ShardRef(source="dclm", domain=None, split="train", ordinal=2)],
        documents=42,
        tokens_in=total + 100,
        tokens_out=total,
        tail_dropped=100,
        surplus_dropped=0,
        max_eos_fraction=0.031,
    )
    digests = {path: (_sha(body), len(body)) for path, body in bodies.items()}

    receipt = Receipt.from_pack_result(
        result,
        plan_id=PLAN,
        prefix=PREFIX,
        digests=digests,
        sources=[SourcePin(key="dclm", repo="mlfoundations/dclm-baseline-1.0", revision=DCLM_SHA)],
    )

    assert receipt.bundle_id == bundle_id_for(PLAN, stream)
    assert receipt.wheel_version == __version__
    assert receipt.unfilled == (shard_key(*stream, 2),)
    assert verify_receipt(receipt, _seeded(bodies), BUCKET, deep=True) == []


def test_a_written_shard_with_no_digest_is_refused_at_construction():
    """A receipt with a hole where a sha256 should be is unverifiable by the only tier that re-hashes."""
    stream = ("dclm", None, "train")
    result = _FakePackResult(
        stream,
        [ShardRef(source="dclm", domain=None, split="train", ordinal=0, tokens=SEQ_LEN)],
        documents=1,
        tokens_in=SEQ_LEN,
        tokens_out=SEQ_LEN,
        tail_dropped=0,
        surplus_dropped=0,
        max_eos_fraction=0.01,
    )

    with pytest.raises(BuildError, match="deep tier"):
        Receipt.from_pack_result(result, plan_id=PLAN, prefix=PREFIX, digests={})


def test_the_bytes_in_a_receipt_come_from_the_writer_not_from_tokens():
    """If `bytes` were derived as `tokens * 4`, the identity check could never fail. It must not be.

    Here the writer reports a size that disagrees with the ref's token count — the truncated-upload
    case — and the receipt must carry the writer's number so `verify_receipt` has something to catch.
    """
    stream = ("dclm", None, "train")
    path = shard_key(*stream, 0)
    short = _shard_body(1, seed="writer-short")
    result = _FakePackResult(
        stream,
        [ShardRef(source="dclm", domain=None, split="train", ordinal=0, tokens=2 * SEQ_LEN)],
        documents=1,
        tokens_in=2 * SEQ_LEN,
        tokens_out=2 * SEQ_LEN,
        tail_dropped=0,
        surplus_dropped=0,
        max_eos_fraction=0.01,
    )

    receipt = Receipt.from_pack_result(
        result, plan_id=PLAN, prefix=PREFIX, digests={path: (_sha(short), len(short))}
    )

    assert receipt.shards[0].bytes == len(short)
    assert receipt.shards[0].tokens * DTYPE_SIZE != receipt.shards[0].bytes
    codes = _codes(verify_receipt(receipt, _seeded({path: short}), BUCKET))
    assert "receipt-token-byte-mismatch" in codes


def test_a_receipt_round_trips_through_s3():
    s3 = FakeS3()
    receipt = _receipt()
    key = f"_receipts/{PLAN}/{receipt.bundle_id}.json"

    digest = write_receipt(receipt, s3, BUCKET, key)

    assert digest == receipt.receipt_sha256()
    assert read_receipt(s3, BUCKET, key) == receipt


@pytest.mark.parametrize(
    "basename", ["manifest.json", "dataset.json", "_VALIDATED.json", "_REJECTED.json"]
)
def test_writing_a_receipt_under_a_reserved_basename_is_refused(basename):
    """`edullm-landing-manifest-created` matches the suffix ANYWHERE in the bucket, no prefix bound.

    The PUT would return 200 and then fire Gate A against a build artifact with no dataset.json.
    """
    s3 = FakeS3()

    with pytest.raises(BuildError, match="reserved"):
        write_receipt(_receipt(), s3, BUCKET, f"_receipts/{PLAN}/{basename}")

    assert s3.dump(BUCKET) == {}  # nothing was written


def test_reading_an_unparseable_receipt_raises_rather_than_returning_a_hollow_one():
    s3 = FakeS3()
    s3.seed(BUCKET, "_receipts/broken.json", b"{not json")

    with pytest.raises(BuildError, match="readable receipt"):
        read_receipt(s3, BUCKET, "_receipts/broken.json")


def test_reading_an_absent_receipt_raises_not_found():
    """`NotFound` passes through: "no receipt yet" is how a resumable driver learns to run a bundle."""
    with pytest.raises(NotFound):
        read_receipt(FakeS3(), BUCKET, "_receipts/nope.json")
