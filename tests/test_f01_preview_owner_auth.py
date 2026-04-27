"""F-01 regression suite: preview asset routes enforce ownership.

Phase E re-audit found that ``/preview/{task_id}/{file_path}`` served any
file under ``${DEFT_PREVIEW_ROOT}/{task_id}`` without authentication, even
though the manifest endpoint at ``/api/preview/{task_id}`` was owner-gated.
A user who learned a peer's task id could read every file in their preview.

This suite verifies:

  1. ``_mint_preview_token`` produces a different token than ``_mint_stream_token``
     for the same (user_id, task_id) pair, and is signed with the same secret.
  2. ``_verify_preview_token`` accepts a valid token and returns the user_id.
  3. Stream tokens cannot be replayed against the preview verifier (scope marker).
  4. Preview tokens cannot be replayed against the stream verifier.
  5. Task-id mismatch is rejected.
  6. Tampered signatures are rejected.
  7. Expired tokens are rejected.
  8. ``GET /preview/{task_id}/index.html`` with no credentials returns 401
     (not 200) when a manifest exists.
  9. ``GET /preview/{task_id}/index.html`` with another user's JWT returns 403.
 10. ``GET /preview/{task_id}/index.html`` with the owner's preview cookie
     returns the file body.
 11. ``GET /api/preview/{task_id}`` sets the ``deft_preview_<task_id>`` cookie
     for the owner and the cookie's Path attribute is scoped to the preview.

No live Postgres or Redis is required. The Supabase token verifier is patched.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STREAM_TOKEN_SECRET", "f01-test-secret-do-not-rotate")
    import mariana.api as api_mod
    api_mod._STREAM_TOKEN_SECRET = None  # type: ignore[attr-defined]


@pytest.fixture
def preview_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "preview"
    root.mkdir()
    monkeypatch.setenv("DEFT_PREVIEW_ROOT", str(root))
    return root


@pytest.fixture
def deployed_preview(preview_root: Path) -> dict[str, Any]:
    """Lay down a deployed preview owned by ``owner-uuid`` with index.html."""
    task_id = "task01"
    task_dir = preview_root / task_id
    task_dir.mkdir()
    (task_dir / "index.html").write_text("<html>secret payload</html>")
    (task_dir / "_deft_manifest.json").write_text(
        json.dumps({"task_id": task_id, "user_id": "owner-uuid", "entry": "index.html"})
    )
    return {"task_id": task_id, "owner_id": "owner-uuid", "dir": task_dir}


@pytest.fixture
def client(preview_root: Path):  # noqa: ARG001
    """A fresh TestClient with the preview routes registered against the tmp root.

    The preview routes are mounted at module-import time using the
    ``DEFT_PREVIEW_ROOT`` env var that is in scope at that moment, so we need
    to reload the module after monkey-patching the env var.
    """
    import importlib
    import mariana.api as api_mod
    api_mod = importlib.reload(api_mod)
    return TestClient(api_mod.app), api_mod


# ---------------------------------------------------------------------------
# 1-7: token primitives
# ---------------------------------------------------------------------------


def test_mint_preview_token_differs_from_stream_token() -> None:
    from mariana.api import _mint_preview_token, _mint_stream_token

    p = _mint_preview_token("u1", "t1")
    s = _mint_stream_token("u1", "t1")
    assert p != s


def test_verify_preview_token_round_trip() -> None:
    from mariana.api import _mint_preview_token, _verify_preview_token

    token = _mint_preview_token("u1", "t1")
    assert _verify_preview_token(token, "t1") == "u1"


def test_stream_token_cannot_pass_preview_verifier() -> None:
    from mariana.api import _mint_stream_token, _verify_preview_token

    s = _mint_stream_token("u1", "t1")
    assert _verify_preview_token(s, "t1") is None


def test_preview_token_cannot_pass_stream_verifier() -> None:
    from fastapi import HTTPException

    from mariana.api import _mint_preview_token, _verify_stream_token

    p = _mint_preview_token("u1", "t1")
    with pytest.raises(HTTPException):
        _verify_stream_token(p, "t1")


def test_preview_token_task_mismatch_rejected() -> None:
    from mariana.api import _mint_preview_token, _verify_preview_token

    token = _mint_preview_token("u1", "t1")
    assert _verify_preview_token(token, "t2") is None


def test_preview_token_tampered_signature_rejected() -> None:
    import base64

    from mariana.api import _mint_preview_token, _verify_preview_token

    token = _mint_preview_token("u1", "t1")
    raw = base64.urlsafe_b64decode(token.encode()).decode()
    # Flip the final character of the signature.
    flipped = raw[:-1] + ("0" if raw[-1] != "0" else "1")
    bad = base64.urlsafe_b64encode(flipped.encode()).decode()
    assert _verify_preview_token(bad, "t1") is None


def test_preview_token_expired_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from mariana import api as api_mod

    monkeypatch.setattr(api_mod, "_PREVIEW_TOKEN_TTL_SECONDS", -10)
    token = api_mod._mint_preview_token("u1", "t1")
    assert api_mod._verify_preview_token(token, "t1") is None


# ---------------------------------------------------------------------------
# 8-11: HTTP route enforcement
# ---------------------------------------------------------------------------


def test_preview_static_unauthenticated_returns_401(client, deployed_preview) -> None:
    test_client, _ = client
    resp = test_client.get(f"/preview/{deployed_preview['task_id']}/index.html")
    assert resp.status_code == 401, resp.text


def test_preview_static_other_user_jwt_returns_403(client, deployed_preview) -> None:
    test_client, api_mod = client

    async def _fake_auth(_token: str) -> dict[str, str]:
        return {"user_id": "stranger-uuid", "role": "authenticated"}

    with patch.object(api_mod, "_authenticate_supabase_token", side_effect=_fake_auth):
        resp = test_client.get(
            f"/preview/{deployed_preview['task_id']}/index.html",
            headers={"Authorization": "Bearer stranger-jwt"},
        )
    assert resp.status_code == 403, resp.text


def test_preview_static_owner_cookie_returns_file(client, deployed_preview) -> None:
    test_client, api_mod = client
    token = api_mod._mint_preview_token(deployed_preview["owner_id"], deployed_preview["task_id"])
    cookie_name = f"{api_mod._PREVIEW_COOKIE_PREFIX}{deployed_preview['task_id']}"
    test_client.cookies.set(cookie_name, token)
    resp = test_client.get(f"/preview/{deployed_preview['task_id']}/index.html")
    assert resp.status_code == 200, resp.text
    assert "secret payload" in resp.text


def test_api_preview_manifest_sets_owner_cookie(client, deployed_preview) -> None:
    test_client, api_mod = client

    async def _fake_auth(_token: str) -> dict[str, str]:
        return {"user_id": deployed_preview["owner_id"], "role": "authenticated"}

    with patch.object(api_mod, "_authenticate_supabase_token", side_effect=_fake_auth):
        resp = test_client.get(
            f"/api/preview/{deployed_preview['task_id']}",
            headers={"Authorization": "Bearer owner-jwt"},
        )
    assert resp.status_code == 200, resp.text
    set_cookies = resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else [resp.headers.get("set-cookie", "")]
    cookie_name = f"{api_mod._PREVIEW_COOKIE_PREFIX}{deployed_preview['task_id']}"
    matching = [c for c in set_cookies if cookie_name in c]
    assert matching, f"expected {cookie_name} in {set_cookies}"
    # Path attribute must be scoped to the preview path so the cookie is only
    # sent for that task's assets, not for unrelated routes.
    assert f"Path=/preview/{deployed_preview['task_id']}" in matching[0]


def test_api_preview_manifest_other_user_returns_403(client, deployed_preview) -> None:
    test_client, api_mod = client

    async def _fake_auth(_token: str) -> dict[str, str]:
        return {"user_id": "stranger-uuid", "role": "authenticated"}

    with patch.object(api_mod, "_authenticate_supabase_token", side_effect=_fake_auth):
        resp = test_client.get(
            f"/api/preview/{deployed_preview['task_id']}",
            headers={"Authorization": "Bearer stranger-jwt"},
        )
    assert resp.status_code == 403


def test_preview_root_redirect_unauthenticated_returns_401(client, deployed_preview) -> None:
    test_client, _ = client
    resp = test_client.get(f"/preview/{deployed_preview['task_id']}", follow_redirects=False)
    assert resp.status_code == 401, resp.text


def test_preview_static_query_token_works(client, deployed_preview) -> None:
    test_client, api_mod = client
    token = api_mod._mint_preview_token(deployed_preview["owner_id"], deployed_preview["task_id"])
    resp = test_client.get(
        f"/preview/{deployed_preview['task_id']}/index.html",
        params={"preview_token": token},
    )
    assert resp.status_code == 200
    assert "secret payload" in resp.text


def test_preview_static_query_token_wrong_task_rejected(client, deployed_preview) -> None:
    test_client, api_mod = client
    # Token minted for a *different* task id must not unlock this preview.
    other_token = api_mod._mint_preview_token("stranger-uuid", "different-task-id")
    resp = test_client.get(
        f"/preview/{deployed_preview['task_id']}/index.html",
        params={"preview_token": other_token},
    )
    assert resp.status_code == 401
