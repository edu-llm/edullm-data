"""Family ``defaults`` must actually reach a profile check.

Two independent bugs made every family-declared bound dead code:

1. ``validate.py`` built its ``GroupContext`` without ``family_defaults``, so the field was
   always ``{}`` in production. Four profiles read ``ctx.family_defaults``; none of them ever
   saw a value outside the test suite, which supplied it directly to the fixture.
2. Even wired up, the vocabularies did not match: ``families/pretrain.json`` nests its bounds
   under ``decode_smoke_test`` as ``distinct_ids_min`` / ``eos_fraction_max`` /
   ``zero_run_max``, while the profile reads flat ``min_distinct_ids`` /
   ``max_eos_fraction`` / ``max_zero_run``. Names inverted AND nesting mismatched.

Net effect on the live corpus: it was validated against the profile's own fallbacks — 16
distinct ids, 50% EOS, 50% zeros — instead of the family's declared bounds. So a shard that was
half end-of-text padding, or half zeros from a crashed writer, would have passed.
"""

from __future__ import annotations

import json
from pathlib import Path

from edullm_data.profiles import pretrain_tokens_v1 as pretrain
from edullm_data.validate import FAMILIES_DIR, _family_defaults_for


def test_pretrain_family_bounds_resolve_to_the_declared_values():
    fd = _family_defaults_for("pretrain/olmo-mix-1124-31b")
    assert fd["min_distinct_ids"] == 128
    assert fd["max_eos_fraction"] == 0.05
    assert fd["max_zero_run"] == 256
    # window_bytes is deliberately NOT flattened: the decode window is a fixed constant, so
    # aliasing it would surface a bound nothing enforces.
    assert "window_bytes" not in fd


def test_the_declared_bounds_are_stricter_than_the_profile_fallbacks():
    """If this ever inverts, the family file has been loosened past the code's own floor."""
    fd = _family_defaults_for("pretrain/anything-10b")
    assert fd["min_distinct_ids"] > pretrain._DEFAULT_MIN_DISTINCT
    assert fd["max_eos_fraction"] < pretrain._DEFAULT_MAX_EOS_FRACTION
    assert fd["max_zero_run"] <= pretrain._DEFAULT_MAX_ZERO_RUN


def test_every_family_decode_key_is_either_mapped_or_explicitly_not_enforced():
    """Guards the key-drift bug directly, by SET MEMBERSHIP rather than a substring heuristic.

    The first version of this test used ``fam_key.split("_")[0] in k or ...``, which reports
    "mapped" for keys that map to nothing — it passed for invented names like
    ``max_repetition_ratio`` and ``tags_extra``. A guard that cannot fail is not a guard.

    Every ``decode_smoke_test`` key across every family must be in the alias map or in the
    explicitly-not-enforced list. Adding a new one to a family file without deciding which is a
    test failure, not a silent no-op.
    """
    from edullm_data.validate import _DECODE_BOUND_ALIASES, _DECODE_BOUNDS_NOT_ENFORCED

    accounted = set(_DECODE_BOUND_ALIASES) | set(_DECODE_BOUNDS_NOT_ENFORCED)
    for path in sorted(FAMILIES_DIR.glob("*.json")):
        smoke = json.loads(path.read_text(encoding="utf-8")).get("defaults", {}).get("decode_smoke_test", {})
        unaccounted = set(smoke) - accounted
        assert not unaccounted, (
            f"{path.stem} declares decode_smoke_test key(s) {sorted(unaccounted)} that are "
            f"neither aliased to a key a profile reads nor listed as deliberately unenforced"
        )


def test_the_alias_targets_are_keys_a_profile_actually_reads():
    """The other direction: an alias pointing at a key nobody reads is also decoration."""
    from edullm_data.validate import _DECODE_BOUND_ALIASES

    source = (
        Path(__file__).resolve().parent.parent
        / "src" / "edullm_data" / "profiles" / "pretrain_tokens_v1.py"
    ).read_text(encoding="utf-8")
    for target in _DECODE_BOUND_ALIASES.values():
        assert f'"{target}"' in source, f"nothing reads the alias target {target!r}"


def test_missing_families_dir_degrades_to_no_defaults_rather_than_raising():
    """The wheel ships only ``src/edullm_data``, so a Batch validator has no families dir.

    Falling back to the profile's own constants is correct there; crashing the gate is not.
    """
    assert _family_defaults_for("notafamily/whatever") == {}


def test_every_family_file_loads_and_flattens():
    for path in sorted(FAMILIES_DIR.glob("*.json")):
        family = path.stem
        out = _family_defaults_for(f"{family}/some-dataset")
        assert isinstance(out, dict), family
        assert "decode_smoke_test" not in out, f"{family}: nested block leaked through unflattened"


# ======================================================================================
# THE WIRING, not just the unit. TQ-1 from the Phase 1 review.
# ======================================================================================
#
# Every test above calls `_family_defaults_for` directly, so all of them pass even when the
# GroupContext is built WITHOUT family_defaults — which is precisely the bug this module
# exists to guard. Reverting the wiring broke zero of 410 tests. These two close that.


def test_the_family_bounds_actually_reject_a_corpus_the_fallbacks_would_accept():
    """End-to-end: publish a corpus that violates the FAMILY bound but clears the fallback.

    30% zeros is fine under the profile's own 0.5 constant and a violation under the family's
    0.01. If family_defaults stops reaching GroupContext, this test goes green again — which is
    what makes it a wiring test rather than another unit test.
    """
    import tempfile
    from pathlib import Path

    import numpy as np

    from edullm_data import publish as P
    from edullm_data import validate as V
    from edullm_data.s3 import FakeS3

    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir()
    ids = (np.arange(1, 60001) % 40000).astype(np.uint32) + 1
    # A real crashed writer leaves a CONTIGUOUS hole, not scattered zeros. The old fixture
    # used `ids[::3] = 0` — 33% zeros with a longest run of ONE, which is exactly what
    # healthy prose looks like when the tokenizer maps a common character to id 0 (dolma2
    # maps "!"). That shape is the false positive that rejected two good 150B shards.
    #
    # Zero the TAIL, which is the failure the four-window sampler was designed to catch
    # ("a zero-filled or truncated tail leaves a correctly-sized file with a valid head").
    # A small hole in the middle can genuinely be missed: for this 60,000-token shard the
    # seeded windows land at token offsets 6400/14392/24829/42227, so a 4,096-token hole at
    # 20000 is invisible to all four. Sampling is probabilistic by design; a tail is not.
    ids[40000:] = 0  # 20,000 consecutive zeros — a truncated write
    (d / "tokens" / "train-00000.u32le.bin").write_bytes(ids.tobytes())
    (d / "tokens" / "val-00000.u32le.bin").write_bytes(ids[:20000].tobytes())
    s3 = FakeS3()
    plan = P.publish(
        d,
        dataset_id="pretrain/zerofill-fixture-10b",
        purpose="corpus with a partial zero-fill, to prove the family bound reaches the profile",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at="2026-07-29T12:00:00Z",
        group_meta={"tokens": {"tokenizer": {
            "repo_id": "allenai/dolma2-tokenizer", "revision": "abc123",
            "fingerprint_sha256": "c" * 64, "vocab_size": 100278, "eos_token_id": 100257,
        }}},
        env={"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64},
    )
    res = V.validate_dataset(
        "edullm-landing", f"pretrain/zerofill-fixture-10b/{plan.version}", s3,
        data_bucket="edullm-data",
    )
    assert "zero-run-in-shard" in {v.code for v in res.violations}, [
        str(v) for v in res.violations
    ]


def test_families_are_found_from_an_installed_package_layout(tmp_path, monkeypatch):
    """BUG-1: FAMILIES_DIR was repo-root-relative, so an installed wheel found nothing.

    The wheel ships only ``src/edullm_data``, so ``parent.parent.parent / "families"`` resolved
    to ``<site-packages>/families`` — absent. Every family bound then silently fell back to the
    profile's laxer constant IN PRODUCTION ONLY, passing in every checkout and every test.
    pyproject force-includes families/ into the package; this asserts the lookup prefers it.
    """
    from edullm_data import validate as V

    packaged = tmp_path / "edullm_data" / "families"
    packaged.mkdir(parents=True)
    (packaged / "pretrain.json").write_text('{"defaults": {"min_distinct_ids": 999}}')
    monkeypatch.setattr(V, "FAMILIES_DIR", packaged)
    assert V._family_defaults_for("pretrain/x-10b")["min_distinct_ids"] == 999


def test_an_explicit_override_wins(tmp_path, monkeypatch):
    """EDULLM_FAMILIES_DIR mirrors the P.FAMILIES_DIR override the publish driver needs."""
    from edullm_data import validate as V

    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "pretrain.json").write_text('{"defaults": {"min_distinct_ids": 7}}')
    monkeypatch.setenv("EDULLM_FAMILIES_DIR", str(staged))
    assert V._resolve_families_dir() == staged
