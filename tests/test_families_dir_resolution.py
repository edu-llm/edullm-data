"""``families/`` must resolve from an INSTALLED WHEEL, in the producer as well as the validator.

This is the bug class that has now bitten twice, and a normal test cannot see it: a source
checkout always finds `families/` at the repo root, so every test passes while production dies.

* First time — the validator. `FAMILIES_DIR` was repo-root-relative, so inside a wheel it
  pointed at nothing, `_family_defaults_for` silently returned `{}`, and every bound fell back
  to the profile's laxer constant. The live corpus was validated at 50% EOS / 50% zeros against
  a declared 5% / 1%. SILENT.
* Second time — the producer, 2026-07-30. `validate` was fixed; `publish` was left with the old
  hardcoded path. The 150B publish reached AWS Batch, spent the run getting there, and died with
  `no family.json for 'pretrain' (looked in /usr/local/lib/python3.12/families)`. LOUD, and
  still a wasted run.

Both modules now share ONE resolver in `contracts`. These tests assert that sharing, and
simulate the installed-wheel layout so the failure is reproducible without building a wheel.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from edullm_data import contracts as C
from edullm_data import publish as P
from edullm_data import validate as V

REPO = Path(__file__).resolve().parent.parent


def test_producer_and_validator_resolve_the_same_directory():
    """A split here means one module enforces bounds the other never applied."""
    assert P.FAMILIES_DIR == V.FAMILIES_DIR
    assert P.FAMILIES_DIR.is_dir()


def test_there_is_exactly_one_resolver():
    """Both must call the shared implementation, not keep private copies that can drift."""
    assert P.FAMILIES_DIR == C._resolve_families_dir()
    assert V.FAMILIES_DIR == C._resolve_families_dir()

    # and neither module may reintroduce a hardcoded parent-walk
    for mod in ("publish.py", "validate.py"):
        src = (REPO / "src" / "edullm_data" / mod).read_text(encoding="utf-8")
        assert 'parent.parent.parent / "families"' not in src, (
            f"{mod} hardcodes a repo-relative families path again; inside an installed wheel "
            f"that directory does not exist"
        )


def test_env_override_wins():
    """An operator must always be able to point at a staged copy without a rebuild."""
    prev = os.environ.get("EDULLM_FAMILIES_DIR")
    os.environ["EDULLM_FAMILIES_DIR"] = "/tmp/somewhere-else"
    try:
        assert C._resolve_families_dir() == Path("/tmp/somewhere-else")
    finally:
        if prev is None:
            os.environ.pop("EDULLM_FAMILIES_DIR", None)
        else:
            os.environ["EDULLM_FAMILIES_DIR"] = prev


def test_the_packaged_layout_resolves_without_a_repo_root(tmp_path: Path):
    """THE test: simulate an installed wheel and prove BOTH modules find families/.

    Copies the package into a directory with no repo root above it — exactly what
    `site-packages/edullm_data/` looks like — and imports it in a subprocess. Under the old
    code `publish.FAMILIES_DIR` pointed outside the tree and `_load_family` raised.
    """
    import shutil

    site = tmp_path / "site"
    pkg = site / "edullm_data"
    shutil.copytree(REPO / "src" / "edullm_data", pkg)
    shutil.copytree(REPO / "families", pkg / "families")  # what force-include does

    probe = (
        "import edullm_data.publish as P, edullm_data.validate as V, json;"
        "print(json.dumps({"
        "'publish': str(P.FAMILIES_DIR), 'validate': str(V.FAMILIES_DIR),"
        "'p_ok': P.FAMILIES_DIR.is_dir(), 'v_ok': V.FAMILIES_DIR.is_dir(),"
        "'n': len(list(P.FAMILIES_DIR.glob('*.json'))),"
        "'pretrain': P._load_family('pretrain')['family'],"
        "}))"
    )
    env = {**os.environ, "PYTHONPATH": str(site)}
    env.pop("EDULLM_FAMILIES_DIR", None)  # exercise layout 2, not the override
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         env=env, cwd=str(tmp_path))
    assert out.returncode == 0, f"packaged import failed:\n{out.stderr}"
    got = json.loads(out.stdout)

    assert got["p_ok"], f"publish cannot find families/ in a wheel layout: {got['publish']}"
    assert got["v_ok"], f"validate cannot find families/ in a wheel layout: {got['validate']}"
    assert got["publish"] == got["validate"]
    assert got["publish"].endswith("edullm_data/families")
    assert got["n"] == 7
    assert got["pretrain"] == "pretrain"


def test_publish_fails_loudly_when_families_is_genuinely_absent(tmp_path: Path, monkeypatch):
    """The error must name the directory it looked in — that is what made the Batch failure
    diagnosable in one log line instead of a bisect."""
    monkeypatch.setattr(P, "FAMILIES_DIR", tmp_path / "nope")
    with pytest.raises(P.PublishError) as e:
        P._load_family("pretrain")
    msg = str(e.value)
    assert "no family.json for 'pretrain'" in msg
    assert str(tmp_path / "nope") in msg, "the message must say WHERE it looked"
