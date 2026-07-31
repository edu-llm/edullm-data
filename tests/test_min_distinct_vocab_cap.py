"""The distinct-ids floor is capped by vocabulary size.

RECOVERED FROM PRODUCTION, NOT WRITTEN FRESH. `_cap_min_distinct_by_vocab` was live in the
deployed `edullm_data-0.5.1` wheel and present in **no commit on any branch**. It was found while
reshipping: the live job definition's own bootstrap assertion (`assert
_cap_min_distinct_by_vocab(256, 256) == 16`) failed with `ImportError` against a wheel rebuilt from
git. Reshipping without it would have silently regressed a Gate A behaviour that published
datasets depend on.

It shipped with no tests, which is how it came to exist only as a build artifact. These are them.

WHAT IT DEFENDS. The pretrain family's `min_distinct_ids` floor of 256 is calibrated for ~100k BPE
vocabularies. A raw UTF-8 byte tokenizer has `vocab_size=256`, so the same floor demands that
EVERY byte value appear in a sampled window — including control bytes that ASCII-heavy formal text
(Lean, for instance) never emits. Publishers responded by interleaving full `0..255` alphabet
markers into training shards to pass Gate A, contaminating the corpus with synthetic bytes.

That makes this the same class of defect as `test_tiny_shards.py`: a bound whose UNITS are wrong
for the data, where the workaround is worse than the check.
"""

from __future__ import annotations

import pytest

from edullm_data.profiles.pretrain_tokens_v1 import (
    _DEFAULT_MIN_DISTINCT,
    _cap_min_distinct_by_vocab,
)


# --------------------------------------------------------------------------------------
# The exact assertion the live job definition makes
# --------------------------------------------------------------------------------------


def test_the_live_job_definitions_bootstrap_assertion():
    """`edullm-validator:7`'s container command runs precisely this. It is the closest thing to a
    production contract this function has, so it is pinned first and by itself."""
    assert _cap_min_distinct_by_vocab(256, 256) == 16


# --------------------------------------------------------------------------------------
# Byte tokenizers get a usable floor; BPE tokenizers keep the family floor
# --------------------------------------------------------------------------------------


def test_byte_tokenizer_floor_drops_to_the_profile_default():
    """256 // 16 == 16, which is `_DEFAULT_MIN_DISTINCT` — a floor a real byte corpus can meet
    without fabricating an alphabet."""
    assert _cap_min_distinct_by_vocab(256, 256) == _DEFAULT_MIN_DISTINCT == 16


def test_large_bpe_vocab_keeps_the_family_floor_unchanged():
    """The cap must not weaken the check where it was calibrated. `100000 // 16 == 6250`, so
    `min(256, max(16, 6250)) == 256` — unchanged."""
    for vocab in (32_000, 50_257, 100_278, 128_000, 200_000):
        assert _cap_min_distinct_by_vocab(256, vocab) == 256, vocab


def test_the_cap_never_raises_the_floor():
    """It is a cap, so it may only lower or preserve. A version that RAISED the floor for some
    vocab would reject healthy shards, and the direction is easy to invert by accident."""
    for vocab in (1, 16, 256, 1_000, 65_536, 10**6):
        for declared in (16, 64, 256, 1_024):
            assert _cap_min_distinct_by_vocab(declared, vocab) <= declared, (declared, vocab)


def test_it_still_catches_degeneracy_on_a_byte_vocab():
    """The floor drops to 16, NOT to 1. A floor of 1 is vacuous — every non-empty shard has one
    distinct id — so an all-one-token or all-zeros shard would pass. Degeneracy is the whole
    point of the check, and this is the same argument `test_tiny_shards.py` makes for its floor
    of 2."""
    assert _cap_min_distinct_by_vocab(256, 256) > 1
    assert _cap_min_distinct_by_vocab(256, 16) >= _DEFAULT_MIN_DISTINCT


# --------------------------------------------------------------------------------------
# Unknown / malformed vocab_size falls back to the declared floor
# --------------------------------------------------------------------------------------


def test_unknown_vocab_size_leaves_the_floor_alone():
    """`vocab_size` is absent when no tokenizer is resolvable. Weakening the check on missing
    metadata would let an unverifiable corpus through on a laxer bound — the "absent field is
    worse than an unchecked one" failure the golden rule is about."""
    for bad in (None, 0, -1, "256", 256.0, [], {}):
        assert _cap_min_distinct_by_vocab(256, bad) == 256, bad


def test_bool_is_rejected_even_though_it_is_an_int():
    """`isinstance(True, int)` is True in Python, and `True // 16 == 0`, so an unguarded version
    would compute `max(16, 0) == 16` and silently weaken the floor on a garbage value."""
    assert _cap_min_distinct_by_vocab(256, True) == 256
    assert _cap_min_distinct_by_vocab(256, False) == 256


@pytest.mark.parametrize("vocab,expected", [(256, 16), (512, 32), (4_096, 256), (100_000, 256)])
def test_the_boundary_is_vocab_over_sixteen(vocab, expected):
    """Pins the divisor. `4096 // 16 == 256` is where the cap stops binding, so a corpus with a
    4k vocab is the last one held to the full family floor."""
    assert _cap_min_distinct_by_vocab(256, vocab) == expected
