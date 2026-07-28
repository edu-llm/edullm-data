"""Dataset profiles: one typed contract per payload-group kind (§4).

Each ``<name>_vN.py`` module exposes module-level ``NAME``, ``REQUIRED_FIELDS``, and
``CHECKS``, and self-registers with :mod:`edullm_data.profiles.registry` at import time.
The registry is the discovery surface; import a profile module for its side effect of
registering, or look it up by ``NAME`` via ``registry.get_profile``.

An explicit ``__init__`` (rather than relying on namespace-package behaviour) guarantees
the subpackage ships in the built wheel, not only under an editable install.
"""
