"""The distinct-ids floor scales with what was actually sampled.

``min_distinct_ids`` was an absolute count (256 from the family, 16 as the profile fallback),
which is unsatisfiable for a shard smaller than the bound: a 5-token shard cannot reach 256
distinct ids, or even 16, no matter how healthy it is.

Not hypothetical. The 150B corpus has 2 shards of 20 bytes / 5 tokens among 6,921. Under an
absolute floor they are guaranteed violations, and because ``promote()`` is all-or-nothing they
would block 630 GB / 157.5B tokens over 10 tokens — 6.3e-9 % of the corpus. The bug is the
bound's units, not the shards.

The scaling must not neuter the check, and the floor of 2 is what prevents that. A naive
``max(n // 4, 1)`` collapses to 1 for n <= 4, and a floor of 1 is vacuous — every non-empty
shard has at least one distinct id — so an all-one-token shard would sail through. Degeneracy is
the whole point of this check.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from edullm_data import publish as P
from edullm_data import validate as V
from edullm_data.s3 import FakeS3

CREATED = "2026-07-29T12:00:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}
DSID = "pretrain/tinyshard-fixture-10b"
TOK = {
    "repo_id": "allenai/dolma2-tokenizer",
    "revision": "abc123",
    "fingerprint_sha256": "c" * 64,
    "vocab_size": 100278,
    "eos_token_id": 100257,
}


def _validate_with_tiny(tiny_ids: list[int]):
    s3 = FakeS3()
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    big = (np.arange(1, 60001) % 40000).astype(np.uint32) + 1
    (d / "tokens" / "train-00000.u32le.bin").write_bytes(big.tobytes())
    (d / "tokens" / "train-00001.u32le.bin").write_bytes(np.array(tiny_ids, dtype=np.uint32).tobytes())
    (d / "tokens" / "val-00000.u32le.bin").write_bytes(big[:20000].tobytes())
    plan = P.publish(
        d, dataset_id=DSID,
        purpose="fixture for the sample-size-scaled distinct-ids floor on tiny shards",
        profile="pretrain-tokens/v1", s3=s3, created_at=CREATED,
        group_meta={"tokens": {"tokenizer": TOK}}, env=ENV,
    )
    return V.validate_dataset("edullm-landing", f"{DSID}/{plan.version}", s3, data_bucket="edullm-data")


def _distinct_violations(res) -> list:
    return [v for v in res.violations if v.code == "distinct-too-few"]


# ---- the blocker: a genuinely small but healthy shard ----

def test_a_healthy_five_token_shard_no_longer_blocks_the_corpus():
    """The exact 150B case: 20 bytes, 5 distinct ids, previously a guaranteed violation."""
    res = _validate_with_tiny([7, 42, 999, 12345, 88])
    assert _distinct_violations(res) == [], [str(v) for v in res.violations]
    assert res.ok, [str(v) for v in res.violations]


@pytest.mark.parametrize("n", [2, 3, 5, 9, 17])
def test_small_healthy_shards_of_various_sizes_pass(n):
    res = _validate_with_tiny(list(range(1, n + 1)))
    assert _distinct_violations(res) == [], [str(v) for v in _distinct_violations(res)]


# ---- the check must still bite ----

def test_an_all_one_token_tiny_shard_is_still_caught():
    """The failure a floor of 1 would have let through."""
    res = _validate_with_tiny([7] * 5)
    assert _distinct_violations(res), "a degenerate shard must not pass just because it is small"
    assert not res.ok


def test_an_all_zeros_tiny_shard_is_still_caught():
    """Partial zero-fill from a crashed writer — the reason the check exists."""
    res = _validate_with_tiny([0] * 5)
    assert _distinct_violations(res)


def test_a_degenerate_large_shard_is_still_caught_at_the_declared_bound():
    """Above ~1 KB of sampled tokens the family's 256 applies unchanged."""
    res = _validate_with_tiny([7, 8] * 5000)  # 10,000 tokens, only 2 distinct
    assert _distinct_violations(res)
    msg = str(_distinct_violations(res)[0])
    assert "256" in msg  # the declared bound, not a scaled one


def test_the_message_says_when_the_bound_was_scaled():
    """A reviewer must be able to tell a scaled bound from the declared one."""
    res = _validate_with_tiny([7] * 5)
    msg = str(_distinct_violations(res)[0])
    assert "scaled to this shard's sample size" in msg


# ---- the formula itself ----

@pytest.mark.parametrize(
    "n,expected",
    [(1, 1), (2, 2), (4, 2), (5, 2), (8, 2), (16, 4), (64, 16), (1024, 256), (16384, 256)],
)
def test_the_effective_floor_is_monotone_and_never_vacuous_above_one_token(n, expected):
    declared = 256
    effective = min(declared, max(n // 4, 2 if n > 1 else 1))
    assert effective == expected
    if n > 1:
        assert effective >= 2, "a floor of 1 would be vacuous — every shard has >= 1 distinct id"
    assert effective <= declared, "scaling must never RAISE the declared bound"
