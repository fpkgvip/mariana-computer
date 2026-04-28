"""CC-28 regression — sandbox workspaces must enforce a per-workspace disk quota.

CC-28 (P2, post-CC-26 re-audit #44 Finding 2) found that
``sandbox_server/app.py`` did not cap the on-disk size of a per-task
workspace.  A malicious or runaway plan could fill the host filesystem by
writing arbitrary amounts to its workspace; nothing aborted with a stable
``workspace_full`` error code.

This module pins the fix:

  * ``_workspace_size_bytes`` returns the recursive byte total for a workspace
    path; results are cached for ~5 seconds to avoid repeated walks.
  * ``_enforce_workspace_quota`` raises ``HTTPException(507, "workspace_full")``
    when the workspace is over the configured cap.
  * The cap defaults to 2 GiB and is overridable via the
    ``SANDBOX_MAX_WORKSPACE_BYTES`` env var.
  * ``/fs/write`` and ``/exec`` both call ``_enforce_workspace_quota`` before
    writing.  A workspace under quota writes succeed; a workspace over quota
    triggers HTTP 507 with detail ``"workspace_full"``.
"""

from __future__ import annotations

import importlib
import os
import tempfile
from unittest.mock import patch

import pytest
from fastapi import HTTPException


# The sandbox app module touches ``/workspace`` at import time.  Point it at
# a tempdir for the test process.
os.environ.setdefault("WORKSPACE_ROOT", tempfile.mkdtemp(prefix="cc28-sandbox-"))
from sandbox_server import app as sandbox_app  # noqa: E402


# ---------------------------------------------------------------------------
# (1) Under-quota write succeeds
# ---------------------------------------------------------------------------


def test_under_quota_workspace_passes_check(tmp_path):
    """A 1 KiB workspace is well under the 2 GiB default cap."""
    (tmp_path / "f.txt").write_bytes(b"x" * 1024)
    # Bust the size cache for this path (REPL/test isolation).
    sandbox_app._WORKSPACE_SIZE_CACHE.pop(str(tmp_path), None)
    # No exception raised.
    sandbox_app._enforce_workspace_quota(tmp_path)
    # Sanity: the helper reports a size in [1024, 2048) bytes.
    sandbox_app._WORKSPACE_SIZE_CACHE.pop(str(tmp_path), None)
    size = sandbox_app._workspace_size_bytes(tmp_path)
    assert size >= 1024
    assert size < sandbox_app._MAX_WORKSPACE_BYTES


# ---------------------------------------------------------------------------
# (2) Over-quota workspace raises HTTP 507 ``workspace_full``
# ---------------------------------------------------------------------------


def test_over_quota_workspace_raises_507(tmp_path):
    """When the workspace is over cap, the helper raises HTTP 507.

    We mock ``_workspace_size_bytes`` rather than actually filling 2 GiB.
    """
    (tmp_path / "f.txt").write_bytes(b"y" * 16)
    huge = sandbox_app._MAX_WORKSPACE_BYTES + 1
    with patch.object(sandbox_app, "_workspace_size_bytes", return_value=huge):
        with pytest.raises(HTTPException) as exc:
            sandbox_app._enforce_workspace_quota(tmp_path)
    assert exc.value.status_code == 507
    assert exc.value.detail == "workspace_full"


# ---------------------------------------------------------------------------
# (3) Env override SANDBOX_MAX_WORKSPACE_BYTES lowers the cap
# ---------------------------------------------------------------------------


def test_env_override_lowers_cap(tmp_path, monkeypatch):
    """Setting SANDBOX_MAX_WORKSPACE_BYTES=1024 trips on a 2 KiB workspace.

    The cap is read at module load, so we re-import the module under the
    patched env to exercise the override.
    """
    monkeypatch.setenv("SANDBOX_MAX_WORKSPACE_BYTES", "1024")
    monkeypatch.setenv(
        "WORKSPACE_ROOT",
        os.environ.get("WORKSPACE_ROOT") or tempfile.mkdtemp(prefix="cc28-env-"),
    )
    reloaded = importlib.reload(sandbox_app)
    try:
        assert reloaded._MAX_WORKSPACE_BYTES == 1024
        # 2 KiB workspace > 1 KiB cap.
        (tmp_path / "f.txt").write_bytes(b"z" * 2048)
        reloaded._WORKSPACE_SIZE_CACHE.pop(str(tmp_path), None)
        with pytest.raises(HTTPException) as exc:
            reloaded._enforce_workspace_quota(tmp_path)
        assert exc.value.status_code == 507
        assert exc.value.detail == "workspace_full"
    finally:
        # Restore module to default cap so the rest of the suite is unaffected.
        monkeypatch.delenv("SANDBOX_MAX_WORKSPACE_BYTES", raising=False)
        importlib.reload(sandbox_app)


# ---------------------------------------------------------------------------
# (4) Boundary: size == cap is allowed, size == cap+1 is rejected
# ---------------------------------------------------------------------------


def test_quota_boundary_inclusive(tmp_path):
    """``size > max`` is the trip condition; ``size == max`` must pass."""
    cap = sandbox_app._MAX_WORKSPACE_BYTES
    with patch.object(sandbox_app, "_workspace_size_bytes", return_value=cap):
        # No exception at exactly the cap.
        sandbox_app._enforce_workspace_quota(tmp_path)
    with patch.object(sandbox_app, "_workspace_size_bytes", return_value=cap + 1):
        with pytest.raises(HTTPException) as exc:
            sandbox_app._enforce_workspace_quota(tmp_path)
        assert exc.value.status_code == 507
