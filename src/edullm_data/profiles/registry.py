"""Profile registry — the discovery surface for profiles (§4).

**Registration contract.** A profile module must expose module-level ``NAME`` (the exact
string that appears in a group's ``profile``), ``REQUIRED_FIELDS`` (a mapping), and
``CHECKS`` (a list of ``(GroupContext) -> list[Violation]``), and self-register at import
time with::

    import sys
    try:
        from . import registry as _registry
        _registry.register(sys.modules[__name__])
    except Exception:
        pass

Look a profile up by ``NAME`` with :func:`get_profile`. Importing this module eagerly
imports the four v1 profile modules so they register as a side effect — a caller that has
only imported ``registry`` still sees every shipped profile. The imports are guarded so a
half-built tree (one profile missing) never makes the registry unimportable; a genuinely
unknown ``NAME`` surfaces at :func:`get_profile` time as a clear error, which the
orchestrator turns into a Violation rather than a crash (§7).
"""

from __future__ import annotations

import importlib
from types import ModuleType

# name -> profile module
_REGISTRY: dict[str, ModuleType] = {}

# The profile modules shipped in this package. Kept as a literal list (not a directory
# scan) so what is loaded is explicit and reviewable — adding a profile is a one-line
# edit here plus the module, matching CONTRIBUTING.md.
_SHIPPED = (
    "pretrain_tokens_v1",
    "eval_results_v1",
    "token_order_v1",
    "sft_conversations_v1",
)


class ProfileError(KeyError):
    """Requested a profile NAME that is not registered."""


def register(module: ModuleType) -> None:
    """Register a profile module. Idempotent per NAME (re-import is safe). Validates the
    module actually satisfies the contract, so a malformed profile fails loudly at
    registration rather than silently at check time."""
    name = getattr(module, "NAME", None)
    if not isinstance(name, str) or not name:
        raise ProfileError(f"profile module {module!r} has no valid module-level NAME")
    if not hasattr(module, "REQUIRED_FIELDS"):
        raise ProfileError(f"profile {name!r} is missing REQUIRED_FIELDS")
    checks = getattr(module, "CHECKS", None)
    if not isinstance(checks, list):
        raise ProfileError(f"profile {name!r} must expose CHECKS as a list")
    _REGISTRY[name] = module


def get_profile(name: str) -> ModuleType:
    """Return the registered profile module for ``name``.

    Raises :class:`ProfileError` listing the known names if unknown — the orchestrator
    converts that into an ``unknown-profile`` Violation so a typo in ``dataset.json`` is a
    rejection, never a stack trace."""
    _ensure_loaded()
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none loaded)"
        raise ProfileError(f"unknown profile {name!r}; known: {known}") from None


def available() -> list[str]:
    """Sorted list of registered profile NAMEs."""
    _ensure_loaded()
    return sorted(_REGISTRY)


_loaded = False


def _ensure_loaded() -> None:
    """Import the shipped profile modules once so their self-registration runs. Guarded so
    one broken/absent profile does not take down discovery of the others."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for mod in _SHIPPED:
        try:
            importlib.import_module(f"{__package__}.{mod}")
        except Exception:  # noqa: BLE001 - a missing/broken profile must not break the rest
            continue


__all__ = ["register", "get_profile", "available", "ProfileError"]
