"""Every third-party package this code imports must be DECLARED in `pyproject.toml`.

The failure this catches has already happened: `tokenizers` decides every token id in the corpus,
is imported by `corpus_build.py`, and appeared nowhere in `pyproject.toml` — so the version that ran
in the Batch build container was whatever PyPI served that morning. A numpy mismatch crashes; a
tokenizer change silently emits DIFFERENT ids that stay inside the vocabulary and decode cleanly, so
no gate in this package can see it.

The check RECOMPUTES the import set by parsing every module's AST, rather than asserting that some
list of names is present. A hardcoded expected-list is decoration: it goes stale the moment someone
adds an import, which is precisely the moment it needed to fire.
"""

from __future__ import annotations

import ast
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src" / "edullm_data"

#: Imported by the package but deliberately NOT declared. Each entry needs a reason, because an
#: exemption list with no reasons is how the original `tokenizers` gap survived review.
_UNDECLARED_BY_DECISION = {
    # Ships inside boto3's own dependency tree, so it is pinned transitively and cannot skew
    # independently of the client it configures.
    "botocore": "arrives with boto3",
}


def _imported_top_level_packages() -> dict[str, set[str]]:
    """`{package: {module.py, ...}}` for every non-stdlib import in the package, at any scope.

    Function-local imports count. `corpus_build.py:631` imports `tokenizers` inside a function, and
    that is exactly the import that was undeclared — a scan that only looked at module-level
    imports would have reported the package clean.
    """
    stdlib = set(sys.stdlib_module_names)
    found: dict[str, set[str]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in stdlib or top == "edullm_data":
                    continue
                found.setdefault(top, set()).add(path.name)
    return found


def _declared_requirement_names() -> dict[str, str]:
    """`{normalised name: raw requirement string}` from `pyproject.toml`'s `dependencies`.

    Parsed with a small reader rather than `tomllib`, which is 3.11+ while this package's floor is
    3.10 (and that floor is load-bearing — see the comment in `pyproject.toml`).
    """
    import re

    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"^dependencies\s*=\s*\[(.*?)^\]", text, re.S | re.M)
    assert block, "pyproject.toml has no `dependencies` array"
    out: dict[str, str] = {}
    for raw in re.findall(r'"([^"]+)"', block.group(1)):
        name = re.split(r"[<>=!~\[; ]", raw, 1)[0].strip()
        out[name.lower().replace("_", "-")] = raw
    return out


def test_every_imported_package_is_declared():
    imported = _imported_top_level_packages()
    declared = _declared_requirement_names()
    missing = {
        pkg: sorted(mods)
        for pkg, mods in imported.items()
        if pkg.lower().replace("_", "-") not in declared and pkg not in _UNDECLARED_BY_DECISION
    }
    assert not missing, (
        "third-party packages are imported but not declared in pyproject.toml, so their version "
        "in the Batch container is whatever PyPI served that morning:\n"
        + "\n".join(f"  {pkg}: imported by {mods}" for pkg, mods in sorted(missing.items()))
    )


def test_tokenizers_is_declared_and_bounded_on_both_sides():
    """The specific gap this file was written for.

    Both bounds are asserted because each one guards a different failure. Without a FLOOR the
    resolver may serve 0.20.x, whose Unicode-14 `\\p{L}`/`\\p{N}` tables diverge from later versions
    by upstream's own WONTFIX. Without a CEILING an untested major/minor lands silently in a build
    whose output is content-addressed and frozen once published.
    """
    req = _declared_requirement_names().get("tokenizers")
    assert req, "`tokenizers` is not declared; corpus_build.py:631 imports it"
    assert ">=" in req, f"no lower bound in {req!r}: 0.20.x's Unicode 14 tables are reachable"
    assert "<" in req.replace("<=", ""), f"no upper bound in {req!r}"


def test_the_declared_bound_actually_admits_the_version_the_suite_runs_against():
    """A pin nobody can satisfy is worse than no pin: the container build fails, or somebody
    'fixes' it by widening the bound without reading why it was there.

    RECOMPUTED against the INSTALLED distribution, not against a version string typed into this
    test — that is the whole point. If this environment's tokenizers falls outside the declared
    range, either the range or the environment is wrong and both deserve a look.
    """
    import importlib.metadata as md

    try:
        installed = md.version("tokenizers")
    except md.PackageNotFoundError:  # pragma: no cover - tokenizers absent in a minimal venv
        import pytest

        pytest.skip("tokenizers not installed in this environment")

    parts = tuple(int(x) for x in installed.split(".")[:2])
    assert (0, 21) <= parts < (0, 23), (
        f"installed tokenizers {installed} is outside the declared >=0.21,<0.23. The suite is "
        f"therefore not testing what production would install."
    )
