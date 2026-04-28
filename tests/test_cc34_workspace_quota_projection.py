"""CC-34 regression — workspace quota must enforce *projected* post-write size.

CC-34 (A50 Finding 1, Medium) found that ``_enforce_workspace_quota`` only
refused workspaces that were already over cap.  A workspace at ``cap - 1 KiB``
could still be pushed over the limit by a single ``/fs/write`` payload or
the source file written by ``/exec``, defeating the production-safety goal of
CC-28.

This module pins the route-level fix:

* ``_enforce_workspace_quota(workspace_root, additional_bytes=0)`` raises
  HTTP 507 ``workspace_full`` when ``current + additional_bytes`` would exceed
  the cap.
* ``/fs/write`` measures the incoming payload BEFORE the quota check so a
  text/binary write whose projection trips the cap is refused.
* ``/exec`` projects ``len(req.code.encode("utf-8"))`` for the source file
  the sandbox writes immediately; runtime artifacts are bounded by
  ``MAX_STDOUT_BYTES`` / ``MAX_STDERR_BYTES`` and the wall-clock timeout.
* The cached workspace size is refreshed after a successful write so two
  rapid writes inside the cache TTL cannot each see the same pre-write
  total and both pass.
"""

from __future__ import annotations

import base64
import importlib
import os
import tempfile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


# Point the sandbox app at a tempdir before import.  CC-28's existing test
# follows the same pattern, so test isolation is consistent.
os.environ.setdefault("WORKSPACE_ROOT", tempfile.mkdtemp(prefix="cc34-sandbox-"))
# Force a tight, deterministic cap for the suite so we don't have to allocate
# 2 GiB worth of test fixtures.  4096 bytes is well above any header/source
# overhead we project from below.
os.environ["SANDBOX_MAX_WORKSPACE_BYTES"] = "4096"
os.environ.setdefault("SANDBOX_SHARED_SECRET", "cc34-test-secret")

_AUTH_HEADERS = {"x-sandbox-secret": os.environ["SANDBOX_SHARED_SECRET"]}


@pytest.fixture
def sandbox_app(monkeypatch):
    """Reload sandbox_server.app under a tight 4096-byte quota."""
    monkeypatch.setenv("SANDBOX_MAX_WORKSPACE_BYTES", "4096")
    workspace_root = tempfile.mkdtemp(prefix="cc34-sandbox-ws-")
    monkeypatch.setenv("WORKSPACE_ROOT", workspace_root)
    monkeypatch.setenv("SANDBOX_SHARED_SECRET", "cc34-test-secret")
    from sandbox_server import app as mod

    reloaded = importlib.reload(mod)
    reloaded._WORKSPACE_SIZE_CACHE.clear()
    yield reloaded
    # Restore default so other suites are unaffected.
    monkeypatch.delenv("SANDBOX_MAX_WORKSPACE_BYTES", raising=False)
    importlib.reload(mod)


# ---------------------------------------------------------------------------
# (1) /fs/write text payload that would push the workspace over → 507
# ---------------------------------------------------------------------------


def test_fs_write_text_projection_pushes_over_returns_507(sandbox_app):
    """A text payload that would push the workspace past the cap is refused."""
    user_id = "alice"
    workspace = (sandbox_app.WORKSPACE_ROOT / user_id).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    # Pre-fill with 4 KiB - 8 bytes — under the 4096-byte cap.
    (workspace / "existing.txt").write_bytes(b"a" * (4096 - 8))
    sandbox_app._WORKSPACE_SIZE_CACHE.clear()

    client = TestClient(sandbox_app.app)
    # 64 bytes of text would push us to 4152 bytes > 4096.
    payload = "x" * 64
    resp = client.post(
        "/fs/write",
        headers=_AUTH_HEADERS,
        json={"user_id": user_id, "path": "new.txt", "content": payload},
    )
    assert resp.status_code == 507
    assert resp.json()["detail"] == "workspace_full"
    # The new file must NOT have been written.
    assert not (workspace / "new.txt").exists()


# ---------------------------------------------------------------------------
# (2) /fs/write binary base64 payload whose decoded size pushes over → 507
# ---------------------------------------------------------------------------


def test_fs_write_binary_projection_pushes_over_returns_507(sandbox_app):
    user_id = "bob"
    workspace = (sandbox_app.WORKSPACE_ROOT / user_id).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    # Pre-fill close to cap.
    (workspace / "padding.bin").write_bytes(b"\xff" * (4096 - 16))
    sandbox_app._WORKSPACE_SIZE_CACHE.clear()

    raw = b"\x00" * 64  # 64 decoded bytes; 4080 + 64 > 4096.
    encoded = base64.b64encode(raw).decode("ascii")

    client = TestClient(sandbox_app.app)
    resp = client.post(
        "/fs/write",
        headers=_AUTH_HEADERS,
        json={
            "user_id": user_id,
            "path": "blob.bin",
            "content": encoded,
            "binary": True,
        },
    )
    assert resp.status_code == 507
    assert resp.json()["detail"] == "workspace_full"
    assert not (workspace / "blob.bin").exists()


# ---------------------------------------------------------------------------
# (3) /exec source file whose size pushes over → 507
# ---------------------------------------------------------------------------


def test_exec_source_projection_pushes_over_returns_507(sandbox_app):
    user_id = "carol"
    workspace = (sandbox_app.WORKSPACE_ROOT / user_id).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    # Pre-fill very close to cap.
    (workspace / "fill.dat").write_bytes(b"q" * (4096 - 32))
    sandbox_app._WORKSPACE_SIZE_CACHE.clear()

    # 128-byte source file would push 4064 + 128 = 4192 > 4096.
    code = "x = 1\n" + "# pad\n" * 30
    assert len(code.encode("utf-8")) > 32

    client = TestClient(sandbox_app.app)
    resp = client.post(
        "/exec",
        headers=_AUTH_HEADERS,
        json={"user_id": user_id, "language": "python", "code": code},
    )
    assert resp.status_code == 507
    assert resp.json()["detail"] == "workspace_full"


# ---------------------------------------------------------------------------
# (4) Boundary — projection exactly at cap succeeds
# ---------------------------------------------------------------------------


def test_fs_write_projection_exactly_at_cap_succeeds(sandbox_app):
    """``size + additional == cap`` is allowed; the trip condition is ``>``."""
    user_id = "dave"
    workspace = (sandbox_app.WORKSPACE_ROOT / user_id).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    # Pre-fill with 3 KiB.
    (workspace / "a.bin").write_bytes(b"r" * 3072)
    sandbox_app._WORKSPACE_SIZE_CACHE.clear()

    # Add exactly the remaining 1024 bytes — total lands on cap exactly.
    payload = "y" * 1024
    client = TestClient(sandbox_app.app)
    resp = client.post(
        "/fs/write",
        headers=_AUTH_HEADERS,
        json={"user_id": user_id, "path": "filler.txt", "content": payload},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["size"] == 1024
    # Cache must reflect the post-write total (3072 + 1024 = 4096).
    cached = sandbox_app._WORKSPACE_SIZE_CACHE[str(workspace)]
    assert cached[1] == 4096

    # An additional 1-byte write must now trip the cap.
    resp2 = client.post(
        "/fs/write",
        headers=_AUTH_HEADERS,
        json={"user_id": user_id, "path": "extra.txt", "content": "z"},
    )
    assert resp2.status_code == 507
    assert resp2.json()["detail"] == "workspace_full"


# ---------------------------------------------------------------------------
# (5) Helper unit test — additional_bytes parameter is honoured
# ---------------------------------------------------------------------------


def test_enforce_workspace_quota_additional_bytes_param(sandbox_app, tmp_path):
    """Direct unit coverage of the helper signature change."""
    cap = sandbox_app._MAX_WORKSPACE_BYTES
    # Mock current size at cap - 100.
    from unittest.mock import patch

    with patch.object(sandbox_app, "_workspace_size_bytes", return_value=cap - 100):
        # additional_bytes=100 → projected==cap → must pass
        sandbox_app._enforce_workspace_quota(tmp_path, additional_bytes=100)
        # additional_bytes=101 → projected>cap → must raise 507
        with pytest.raises(HTTPException) as exc:
            sandbox_app._enforce_workspace_quota(tmp_path, additional_bytes=101)
        assert exc.value.status_code == 507
        assert exc.value.detail == "workspace_full"


# ---------------------------------------------------------------------------
# (6) Cache invalidation — successive writes inside the TTL window account
# ---------------------------------------------------------------------------


def test_successive_writes_account_for_cached_size(sandbox_app):
    """Two rapid writes inside the size cache TTL must both account.

    Without the post-write cache refresh, the second write would read the
    same pre-write total as the first and both would pass.
    """
    user_id = "erin"
    workspace = (sandbox_app.WORKSPACE_ROOT / user_id).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    sandbox_app._WORKSPACE_SIZE_CACHE.clear()

    # Two 2 KiB writes; cap is 4 KiB.  First should succeed, second should
    # land at exactly cap (allowed), third 1-byte write should trip 507.
    client = TestClient(sandbox_app.app)
    big = "p" * 2048
    r1 = client.post(
        "/fs/write",
        headers=_AUTH_HEADERS,
        json={"user_id": user_id, "path": "p1.txt", "content": big},
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/fs/write",
        headers=_AUTH_HEADERS,
        json={"user_id": user_id, "path": "p2.txt", "content": big},
    )
    assert r2.status_code == 200
    r3 = client.post(
        "/fs/write",
        headers=_AUTH_HEADERS,
        json={"user_id": user_id, "path": "p3.txt", "content": "x"},
    )
    assert r3.status_code == 507
    assert r3.json()["detail"] == "workspace_full"
