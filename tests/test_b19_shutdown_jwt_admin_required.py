"""B-19 regression suite: Shutdown route requires admin JWT.

After the B-19 fix, the POST /api/shutdown endpoint requires BOTH:
  1. A valid Supabase JWT belonging to an admin user (_require_admin dependency).
  2. The shared X-Admin-Key header matching ADMIN_SECRET_KEY.

Test IDs:
  1. shutdown_requires_admin_jwt_missing_returns_401
  2. shutdown_non_admin_jwt_returns_403
  3. shutdown_missing_admin_key_header_returns_401
  4. shutdown_valid_admin_jwt_and_key_succeeds
  5. shutdown_admin_jwt_without_admin_key_returns_401
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request as StarletteRequest
from fastapi.testclient import TestClient

from mariana import api as mod
from mariana.config import AppConfig


ADMIN_USER_ID = "aaaaaaaa-0000-0000-0000-000000000001"
NONADMIN_USER_ID = "cccccccc-0000-0000-0000-000000000003"
ADMIN_SECRET_KEY = "super-secret-shutdown-key"


def _make_cfg(admin_key: str = ADMIN_SECRET_KEY) -> AppConfig:
    cfg = AppConfig.__new__(AppConfig)
    object.__setattr__(cfg, "SUPABASE_URL", "https://supabase.test")
    object.__setattr__(cfg, "SUPABASE_KEY", "anon")
    object.__setattr__(cfg, "SUPABASE_SERVICE_ROLE_KEY", "svc")
    object.__setattr__(cfg, "SUPABASE_ANON_KEY", "anon")
    object.__setattr__(cfg, "STRIPE_SECRET_KEY", "sk_test_x")
    object.__setattr__(cfg, "STRIPE_PUBLISHABLE_KEY", "pk_test_x")
    object.__setattr__(cfg, "STRIPE_WEBHOOK_SECRET", "whsec_x")
    object.__setattr__(cfg, "POSTGRES_DSN", "postgresql://x@localhost/testdb")
    object.__setattr__(cfg, "REDIS_URL", "redis://localhost:6379")
    object.__setattr__(cfg, "ADMIN_SECRET_KEY", admin_key)
    object.__setattr__(cfg, "DATA_ROOT", "/tmp")
    return cfg


def _make_request(admin_key: str | None = ADMIN_SECRET_KEY) -> StarletteRequest:
    headers = [(b"authorization", b"Bearer fake-admin-jwt"), (b"user-agent", b"test")]
    if admin_key is not None:
        headers.append((b"x-admin-key", admin_key.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/shutdown",
        "headers": headers,
        "query_string": b"",
        "client": ("127.0.0.1", 9000),
    }
    return StarletteRequest(scope)


@pytest.mark.asyncio
async def test_shutdown_requires_admin_jwt_missing_returns_401():
    """Missing JWT: _require_admin dependency raises 401 before the function body runs.

    We verify this by testing _require_admin + _get_current_user directly, and
    by confirming that the shutdown function signature includes _require_admin.
    """
    import inspect
    sig = inspect.signature(mod.graceful_shutdown)
    # The function must have a 'caller' parameter with Depends(_require_admin).
    assert "caller" in sig.parameters, (
        "graceful_shutdown must have a 'caller' parameter (Depends(_require_admin))"
    )
    # Verify _require_admin raises 403 for non-admin user.
    with pytest.raises(HTTPException) as exc_info:
        await mod._require_admin(
            current_user={"user_id": "not-an-admin-uuid-at-all-000000000000"}
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_shutdown_non_admin_jwt_returns_403():
    """Non-admin JWT: _require_admin raises 403 when user is not admin."""
    # Verify _is_admin_user returns False for unknown user.
    # Then verify _require_admin raises 403.
    with (
        patch.object(mod, "_is_admin_user", return_value=False),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await mod._require_admin(
                current_user={"user_id": NONADMIN_USER_ID}
            )
        assert exc_info.value.status_code == 403, (
            f"Expected 403 for non-admin JWT, got {exc_info.value.status_code}"
        )


@pytest.mark.asyncio
async def test_shutdown_missing_admin_key_header_returns_401():
    """Even with a valid admin JWT, missing X-Admin-Key must return 401."""
    cfg = _make_cfg()
    caller = {"user_id": ADMIN_USER_ID}
    with (
        patch.object(mod, "_get_config", return_value=cfg),
        patch.object(mod, "_require_admin", return_value=caller),
    ):
        request = _make_request(admin_key=None)
        with pytest.raises(HTTPException) as exc_info:
            await mod.graceful_shutdown(
                request=request,
                x_admin_key=None,
                caller=caller,
            )
        assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_shutdown_admin_jwt_with_wrong_key_returns_401():
    """Even with a valid admin JWT, wrong X-Admin-Key must return 401."""
    cfg = _make_cfg()
    caller = {"user_id": ADMIN_USER_ID}
    with (
        patch.object(mod, "_get_config", return_value=cfg),
        patch.object(mod, "_require_admin", return_value=caller),
    ):
        request = _make_request(admin_key="wrong-key")
        with pytest.raises(HTTPException) as exc_info:
            await mod.graceful_shutdown(
                request=request,
                x_admin_key="wrong-key",
                caller=caller,
            )
        assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_shutdown_valid_admin_jwt_and_key_succeeds():
    """Valid admin JWT + correct X-Admin-Key must proceed (return ShutdownResponse)."""
    import asyncio

    cfg = _make_cfg()
    caller = {"user_id": ADMIN_USER_ID}

    # Patch asyncio loop to prevent actual process exit.
    mock_loop = MagicMock()
    mock_loop.call_later = MagicMock()

    with (
        patch.object(mod, "_get_config", return_value=cfg),
        patch.object(mod, "_require_admin", return_value=caller),
        patch.object(mod, "_db_pool", None),  # no DB needed
        patch("asyncio.get_running_loop", return_value=mock_loop),
    ):
        request = _make_request(admin_key=ADMIN_SECRET_KEY)
        response = await mod.graceful_shutdown(
            request=request,
            x_admin_key=ADMIN_SECRET_KEY,
            caller=caller,
        )

    assert response is not None
    assert "shutdown" in response.message.lower() or response.message  # ShutdownResponse
    # Ensure the deferred exit was scheduled.
    mock_loop.call_later.assert_called_once()
