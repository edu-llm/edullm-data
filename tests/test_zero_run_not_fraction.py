"""Zero-fill detection is a RUN test, not a density test.

The check exists to catch a crashed writer that left a correctly-sized file with a hole in it.
It used to do that by counting what fraction of the sampled tokens were id 0 — which silently
assumes id 0 means "nothing". **In the dolma2 vocabulary id 0 is ``!``**, verified against the
published ``tokenizer.json``, so the density form was measuring punctuation.

That is not hypothetical. Gate A rejected two healthy shards of the 150B corpus at zero
fractions of 0.0106 and 0.0108 against a 0.010 bound:

    tokens/all-dressed-snazzy2/entertainment/train-00721.u32le.bin
    tokens/all-dressed-snazzy2/sports_and_fitness/train-02766.u32le.bin

Their zeros were **30 scattered singletons, longest run 1** — e.g.
``[43096, 512, 1937, 38, 0, 2209, 430, 889, 358]``, a "!" mid-sentence. The violation message
claimed "signature of a partial zero-fill from a crashed writer", and the data flatly
contradicted it.

A run test is tokenizer-independent, which is the whole point: no vocabulary makes 256
consecutive identical ids meaningful, whereas *any* id can be a frequent token.
"""

from __future__ import annotations

import numpy as np
import pytest

from edullm_data.profiles import pretrain_tokens_v1 as P

MAX_RUN = P._DEFAULT_MAX_ZERO_RUN


def _prose(n: int = 16384, seed: int = 1) -> np.ndarray:
    return np.random.default_rng(seed).integers(1, 100278, n, dtype=np.uint32)


# ---- the primitive ----

@pytest.mark.parametrize(
    "arr,expected",
    [
        (np.array([1, 2, 3], dtype=np.uint32), 0),
        (np.array([0], dtype=np.uint32), 1),
        (np.array([0, 1, 0], dtype=np.uint32), 1),
        (np.array([1, 0, 0, 0, 1], dtype=np.uint32), 3),
        (np.array([0, 0, 1, 0, 0, 0], dtype=np.uint32), 3),
        (np.zeros(5, dtype=np.uint32), 5),
        (np.array([0, 0, 1], dtype=np.uint32), 2),  # run at the very start
        (np.array([1, 0, 0], dtype=np.uint32), 2),  # run at the very end
    ],
)
def test_longest_run_is_computed_correctly(arr, expected):
    assert P._longest_run_of(arr, 0) == expected


def test_longest_run_on_an_empty_array():
    assert P._longest_run_of(np.empty(0, dtype=np.uint32), 0) == 0


# ---- the false positive this replaces ----

def test_scattered_zeros_at_the_density_that_rejected_two_good_shards_now_pass():
    """1.06% scattered zeros — the exact shape of the two rejected 150B shards."""
    ids = _prose()
    ids[::94] = 0  # ~1.06%, never adjacent
    frac = float((ids == 0).sum()) / len(ids)
    assert frac > 0.010, "fixture must exceed the OLD 0.010 bound or it proves nothing"
    assert P._longest_run_of(ids, 0) == 1
    assert P._longest_run_of(ids, 0) < MAX_RUN


def test_even_heavy_punctuation_passes_when_never_contiguous():
    """A '!'-dense corpus (chat, marketing copy) must not read as corruption."""
    ids = _prose()
    ids[::10] = 0  # 10% zeros — would have been a 10x bound violation
    assert P._longest_run_of(ids, 0) == 1


# ---- what it must still catch ----

@pytest.mark.parametrize(
    "name,make",
    [
        ("all zeros", lambda: np.zeros(16384, dtype=np.uint32)),
        ("zero-filled tail", lambda: np.concatenate(
            [_prose(12288), np.zeros(4096, dtype=np.uint32)])),
        ("zero-filled head", lambda: np.concatenate(
            [np.zeros(4096, dtype=np.uint32), _prose(12288)])),
        ("hole in the middle", lambda: np.concatenate(
            [_prose(6000), np.zeros(4096, dtype=np.uint32), _prose(6288)])),
    ],
)
def test_real_zero_fill_shapes_are_still_caught(name, make):
    assert P._longest_run_of(make(), 0) >= MAX_RUN, f"{name} slipped through"


def test_the_run_test_is_STRICTLY_more_sensitive_than_the_old_density_test():
    """A 256-token hole is 1.56% of a 16K sample.

    Under a lax density bound (the profile fallback was 0.5) that is invisible; the run test
    catches it exactly at the limit. So switching to runs does not trade sensitivity for the
    false-positive fix — it improves both.
    """
    ids = _prose()
    ids[8000:8256] = 0
    frac = float((ids == 0).sum()) / len(ids)
    assert frac < 0.5, "a 256-token hole is far under the old profile-default density bound"
    assert P._longest_run_of(ids, 0) >= MAX_RUN, "but the run test catches it"


# ---- the bound is declared, resolvable, and clamped ----

def test_the_family_declares_a_run_bound_and_no_stale_fraction_bound():
    import json
    from edullm_data.validate import FAMILIES_DIR

    raw = json.loads((FAMILIES_DIR / "pretrain.json").read_text(encoding="utf-8"))
    smoke = raw["defaults"]["decode_smoke_test"]
    assert "zero_run_max" in smoke
    assert "zero_fraction_max" not in smoke, "the density bound must be gone, not merely unused"
    assert isinstance(smoke["zero_run_max"], int)


def test_the_alias_map_still_covers_the_renamed_key():
    """`_DECODE_BOUND_ALIASES` is how a family bound reaches the profile at all.

    Renaming the family key without renaming its alias would silently drop the bound back to
    the profile fallback — the exact silent-laxness failure that let the live corpus validate
    at 50% EOS.
    """
    from edullm_data.validate import _family_defaults_for

    fd = _family_defaults_for("pretrain/x-10b")
    assert fd["max_zero_run"] == 256
    assert "max_zero_fraction" not in fd
