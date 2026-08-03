"""Test-session temp hygiene.

The suite calls `tempfile.mkdtemp()` in 33 places across 20 modules to build publish
fixtures (token shards, manifests). None of them clean up: `mkdtemp` — unlike
`TemporaryDirectory` — has no finaliser, so every call leaks its tree into `$TMPDIR`
for the OS to collect, which on macOS it effectively never does.

Left alone that is unbounded: a `tokens/train-*.u32le.bin` fixture is ~200 KB, the
suite is 380 tests, and repeated runs accumulated 38,997 directories / 31 GB in
`/var/folders/.../T` over four days and filled the disk.

Rather than touch all 33 call sites, redirect `TMPDIR` for the session into one
pytest-managed root and remove it at the end. `tempfile.mkdtemp()` with no `dir=`
resolves `TMPDIR` per call, so existing calls land inside the root unmodified and the
fixtures keep their real-filesystem semantics (`publish()` walks actual paths).
"""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture(scope="session", autouse=True)
def _contain_mkdtemp_leaks(tmp_path_factory):
    """Point TMPDIR at a session-scoped root that pytest deletes on exit."""
    root = tmp_path_factory.mktemp("edullm-session-tmp")

    # tempfile caches the resolved dir after first use; clear it so both the env var
    # and the cached value point at the new root, then restore both afterwards.
    saved_env = {k: os.environ.get(k) for k in ("TMPDIR", "TEMP", "TMP")}
    saved_tempdir = tempfile.tempdir

    os.environ["TMPDIR"] = str(root)
    os.environ.pop("TEMP", None)
    os.environ.pop("TMP", None)
    tempfile.tempdir = str(root)

    try:
        yield root
    finally:
        tempfile.tempdir = saved_tempdir
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
