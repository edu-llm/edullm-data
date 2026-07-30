"""Family ``defaults`` must actually reach a profile check.

Two independent bugs made every family-declared bound dead code:

1. ``validate.py`` built its ``GroupContext`` without ``family_defaults``, so the field was
   always ``{}`` in production. Four profiles read ``ctx.family_defaults``; none of them ever
   saw a value outside the test suite, which supplied it directly to the fixture.
2. Even wired up, the vocabularies did not match: ``families/pretrain.json`` nests its bounds
   under ``decode_smoke_test`` as ``distinct_ids_min`` / ``eos_fraction_max`` /
   ``zero_fraction_max``, while the profile reads flat ``min_distinct_ids`` /
   ``max_eos_fraction`` / ``max_zero_fraction``. Names inverted AND nesting mismatched.

Net effect on the live corpus: it was validated against the profile's own fallbacks — 16
distinct ids, 50% EOS, 50% zeros — instead of the family's 256 / 5% / 1%. So a shard that was
half end-of-text padding, or half zeros from a crashed writer, would have passed.
"""

from __future__ import annotations

import json

from edullm_data.profiles import pretrain_tokens_v1 as pretrain
from edullm_data.validate import FAMILIES_DIR, _family_defaults_for


def test_pretrain_family_bounds_resolve_to_the_declared_values():
    fd = _family_defaults_for("pretrain/olmo-mix-1124-31b")
    assert fd["min_distinct_ids"] == 256
    assert fd["max_eos_fraction"] == 0.05
    assert fd["max_zero_fraction"] == 0.01
    assert fd["window_bytes"] == 65536


def test_the_declared_bounds_are_stricter_than_the_profile_fallbacks():
    """If this ever inverts, the family file has been loosened past the code's own floor."""
    fd = _family_defaults_for("pretrain/anything-10b")
    assert fd["min_distinct_ids"] > pretrain._DEFAULT_MIN_DISTINCT
    assert fd["max_eos_fraction"] < pretrain._DEFAULT_MAX_EOS_FRACTION
    assert fd["max_zero_fraction"] < pretrain._DEFAULT_MAX_ZERO_FRACTION


def test_family_file_keys_map_onto_keys_a_profile_actually_reads():
    """Guards bug #2 directly: the two vocabularies must not drift apart again.

    Every key under ``decode_smoke_test`` in the shipped family file has to translate to a
    key some profile reads, or it is a bound nobody enforces.
    """
    raw = json.loads((FAMILIES_DIR / "pretrain.json").read_text(encoding="utf-8"))
    smoke = raw["defaults"]["decode_smoke_test"]
    flat = _family_defaults_for("pretrain/x")
    for fam_key in smoke:
        assert any(fam_key.split("_")[0] in k or k.split("_")[-1] in fam_key for k in flat), (
            f"family declares decode_smoke_test.{fam_key} but nothing in the flattened "
            f"defaults corresponds to it: {sorted(flat)}"
        )


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
