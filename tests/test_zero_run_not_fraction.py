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


# --------------------------------------------------------------------------------------
# The int8 sentinels — a MEMORY fix, and the correctness claim attached to it is REFUTED
# --------------------------------------------------------------------------------------


def test_the_sentinels_are_int8_so_the_hit_mask_is_not_promoted_to_int64():
    """🔴 **A whole-buffer copy on every shard of every bundle, invisible to every other test.**

    `np.concatenate` with a Python list literal `[0]` promotes the WHOLE body to int64 — an 8x copy
    of the hit mask. MEASURED with `tracemalloc` at n = 50,000,000: **762.9 MiB with `[0]` against
    302.0 MiB with `np.zeros(1, np.int8)`.** `_verify_shard` calls this for every shard of every
    bundle, so it is paid thousands of times.

    Same shape as the `combine_chunks` defect found the same session: a wider copy computes the
    IDENTICAL answer, so byte-identity tests cannot see it. Only an allocation counter can.

    🔧 **CORRECTED IN PLACE — my first version of this test did not test the shipped code.** It
    measured two local reimplementations and compared them to each other, so restoring the `[0]`
    promotion in `pretrain_tokens_v1` left it GREEN. That is the same defect as F2 (verify a helper
    while the caller ignores it) and the same one I already caught myself making on the driver.
    It now allocates through `P._longest_run_of` itself and reads `tracemalloc`'s peak for THAT call.
    """
    import tracemalloc

    n = 4_000_000
    # 1 in 3 zeros, never adjacent, so the run length is 1 and the work is all in the edge scan.
    ids = np.full(n, 7, dtype=np.uint32)
    ids[::3] = 0

    tracemalloc.start()
    got = P._longest_run_of(ids, 0)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert got == 1

    # MEASURED, and the bound is set FROM the measurement rather than reasoned to:
    #   fixed (int8 body)      34.33 MiB =  9.00 B/element
    #   promoted (int64 body)  64.85 MiB = 17.00 B/element   -> 1.89x
    # The 9 B/element floor is the boolean mask plus the int64 EDGES array, which at this 1-in-3
    # density dominates and is common to both forms — which is why the ratio is 1.89x and not 8x,
    # and why a naive "<4 B/element" bound (my first attempt) fails on correct code.
    per_elem = peak / n
    assert per_elem < 13.0, (
        f"_longest_run_of peaked at {peak/2**20:.1f} MiB = {per_elem:.2f} B/element on a "
        f"{n:,}-element input. The int8 form measures 9.00; a Python list sentinel promotes the hit "
        f"mask to int64 and measures 17.00. Anything past 13 means the promotion is back."
    )
    assert P._RUN_SENTINEL.dtype == np.int8, "the sentinel must not be re-widened"
    # And the premise, so the bound above is known to be discriminating rather than merely loose.
    hits = ids == 0
    assert np.concatenate(([0], hits.view(np.int8), [0])).dtype == np.int64
    assert np.concatenate(
        (P._RUN_SENTINEL, hits.view(np.int8), P._RUN_SENTINEL)
    ).dtype == np.int8


def test_uint8_sentinels_would_ALSO_be_correct_the_wrap_hazard_is_REFUTED():
    """🔧 **A reported correctness hazard that does not exist. Recorded so nobody re-adds it.**

    The claim: `uint8` sentinels are unsafe because `np.diff` on uint8 WRAPS — a 1 -> 0 transition
    gives `255`, not `-1`. **The wrap is real; the hazard is not.** `np.flatnonzero` asks only
    whether a value is nonzero, and 255 is as nonzero as -1. The diff VALUES are then discarded
    entirely — only the edge POSITIONS are used, via `edges[1::2] - edges[::2]`. The sign never
    enters the arithmetic.

    Proven EXHAUSTIVELY rather than argued, because "I cannot think of a case" is not a proof: over
    **all 32,766 bit patterns of length 1..14**, the int8 form, the uint8 form and the original
    int64 form each agree with a brute-force scan on every single one.

    So `int8` is chosen for MEMORY alone. It is kept over `uint8` only because a signed intermediate
    reads as -1/+1 when someone debugs this — a legibility preference, not a guard. This test exists
    so the next reader does not attach a correctness claim to it that the data refutes.
    """
    import itertools

    def brute(bits) -> int:
        best = run = 0
        for b in bits:
            run = run + 1 if b else 0
            best = max(best, run)
        return best

    def via(bits, dt) -> int:
        hits = np.array(bits, dtype=bool)
        if not hits.any():
            return 0
        z = np.zeros(1, dtype=dt)
        e = np.flatnonzero(np.diff(np.concatenate((z, hits.view(np.int8).astype(dt), z))))
        return int((e[1::2] - e[::2]).max())

    checked = 0
    for length in range(1, 15):
        for bits in itertools.product([0, 1], repeat=length):
            want = brute(bits)
            checked += 1
            assert via(bits, np.int8) == want, (bits, "int8")
            assert via(bits, np.uint8) == want, (bits, "uint8 — the wrap DOES break something")
            # And the shipped function agrees, driven through its real entry point.
            ids = np.where(np.array(bits, dtype=bool), 0, 7).astype(np.uint32)
            assert P._longest_run_of(ids, 0) == want, (bits, "the shipped function")
    assert checked == 32_766, checked
