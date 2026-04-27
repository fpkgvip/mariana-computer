"""B-17 regression suite: Legacy admin credits endpoint aliased to v2 path.

After the B-17 fix, the v1 endpoint POST /api/admin/users/{uid}/credits
delegates to admin_adjust_credits (the same RPC used by v2).

Test IDs:
  1. v1_endpoint_exists_returns_success_shape
  2. v1_calls_admin_adjust_credits_not_admin_set_credits
  3. v1_set_mode_passes_correct_mode_param
  4. v1_delta_mode_passes_delta_mode_param
  5. v1_response_shape_matches_v2
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from mariana import api as mod
from mariana.config import AppConfig


def _make_cfg() -> AppConfig:
    cfg = AppConfig.__new__(AppConfig)
    object.__setattr__(cfg, "SUPABASE_URL", "https://supabase.test")
    object.__setattr__(cfg, "SUPABASE_KEY", "anon-key")
    object.__setattr__(cfg, "SUPABASE_SERVICE_ROLE_KEY", "service-role-key")
    object.__setattr__(cfg, "SUPABASE_ANON_KEY", "anon-key")
    object.__setattr__(cfg, "STRIPE_SECRET_KEY", "sk_test_x")
    object.__setattr__(cfg, "STRIPE_PUBLISHABLE_KEY", "pk_test_x")
    object.__setattr__(cfg, "STRIPE_WEBHOOK_SECRET", "whsec_x")
    object.__setattr__(cfg, "POSTGRES_DSN", "postgresql://postgres@/tmp:55432/testdb")
    object.__setattr__(cfg, "REDIS_URL", "redis://localhost:6379")
    object.__setattr__(cfg, "ADMIN_SECRET_KEY", "secret-key")
    object.__setattr__(cfg, "DATA_ROOT", "/tmp")
    return cfg


ADMIN_USER_ID = "aaaaaaaa-0000-0000-0000-000000000001"
TARGET_USER_ID = "bbbbbbbb-0000-0000-0000-000000000002"


def _make_caller() -> dict:
    return {"user_id": ADMIN_USER_ID, "email": "admin@test.local"}


@pytest.mark.asyncio
async def test_v1_calls_admin_adjust_credits_not_admin_set_credits():
    """v1 endpoint must call admin_adjust_credits, not admin_set_credits."""
    cfg = _make_cfg()
    captured_fn: list[str] = []

    async def mock_admin_rpc_call(request, fn, payload, **kwargs):
        captured_fn.append(fn)
        return 500  # new_balance

    with (
        patch.object(mod, "_get_config", return_value=cfg),
        patch.object(mod, "_require_admin", return_value=_make_caller()),
        patch.object(mod, "_admin_rpc_call", side_effect=mock_admin_rpc_call),
    ):
        # Build a fake request.
        from starlette.datastructures import Headers
        from starlette.requests import Request as StarletteRequest

        scope = {
            "type": "http",
            "method": "POST",
            "path": f"/api/admin/users/{TARGET_USER_ID}/credits",
            "headers": [(b"authorization", b"Bearer fake-jwt"), (b"content-type", b"application/json")],
            "query_string": b"",
        }
        request = StarletteRequest(scope)

        body = mod.AdminSetCreditsRequest(credits=500, delta=False)
        response = await mod.admin_set_credits(
            user_id=TARGET_USER_ID,
            body=body,
            request=request,
            caller=_make_caller(),
        )

    assert "admin_adjust_credits" in captured_fn, (
        f"v1 endpoint must call admin_adjust_credits; called: {captured_fn}"
    )
    assert "admin_set_credits" not in captured_fn, (
        "v1 endpoint must NOT call admin_set_credits RPC directly"
    )


@pytest.mark.asyncio
async def test_v1_set_mode_passes_correct_mode_param():
    """When delta=False, v1 must pass p_mode='set' to admin_adjust_credits."""
    captured_payload: list[dict] = []

    async def mock_rpc(request, fn, payload, **kwargs):
        if fn == "admin_adjust_credits":
            captured_payload.append(payload)
        return 300

    cfg = _make_cfg()

    with (
        patch.object(mod, "_get_config", return_value=cfg),
        patch.object(mod, "_require_admin", return_value=_make_caller()),
        patch.object(mod, "_admin_rpc_call", side_effect=mock_rpc),
    ):
        from starlette.requests import Request as StarletteRequest
        scope = {
            "type": "http", "method": "POST",
            "path": "/api/admin/users/x/credits",
            "headers": [(b"authorization", b"Bearer fake")],
            "query_string": b"",
        }
        request = StarletteRequest(scope)
        body = mod.AdminSetCreditsRequest(credits=300, delta=False)
        await mod.admin_set_credits(
            user_id=TARGET_USER_ID, body=body, request=request, caller=_make_caller()
        )

    assert captured_payload, "admin_adjust_credits was not called"
    assert captured_payload[0]["p_mode"] == "set"
    assert captured_payload[0]["p_amount"] == 300


@pytest.mark.asyncio
async def test_v1_delta_mode_passes_delta_mode_param():
    """When delta=True, v1 must pass p_mode='delta' to admin_adjust_credits."""
    captured_payload: list[dict] = []

    async def mock_rpc(request, fn, payload, **kwargs):
        if fn == "admin_adjust_credits":
            captured_payload.append(payload)
        return 150

    cfg = _make_cfg()
    with (
        patch.object(mod, "_get_config", return_value=cfg),
        patch.object(mod, "_require_admin", return_value=_make_caller()),
        patch.object(mod, "_admin_rpc_call", side_effect=mock_rpc),
    ):
        from starlette.requests import Request as StarletteRequest
        scope = {
            "type": "http", "method": "POST",
            "path": "/api/admin/users/x/credits",
            "headers": [(b"authorization", b"Bearer fake")],
            "query_string": b"",
        }
        request = StarletteRequest(scope)
        body = mod.AdminSetCreditsRequest(credits=50, delta=True)
        await mod.admin_set_credits(
            user_id=TARGET_USER_ID, body=body, request=request, caller=_make_caller()
        )

    assert captured_payload, "admin_adjust_credits was not called"
    assert captured_payload[0]["p_mode"] == "delta"
    assert captured_payload[0]["p_amount"] == 50


@pytest.mark.asyncio
async def test_v1_response_shape_matches_v2():
    """v1 endpoint response must include user_id and new_balance keys."""
    async def mock_rpc(request, fn, payload, **kwargs):
        return 999

    cfg = _make_cfg()
    with (
        patch.object(mod, "_get_config", return_value=cfg),
        patch.object(mod, "_require_admin", return_value=_make_caller()),
        patch.object(mod, "_admin_rpc_call", side_effect=mock_rpc),
    ):
        from starlette.requests import Request as StarletteRequest
        scope = {
            "type": "http", "method": "POST",
            "path": "/api/admin/users/x/credits",
            "headers": [(b"authorization", b"Bearer fake")],
            "query_string": b"",
        }
        request = StarletteRequest(scope)
        body = mod.AdminSetCreditsRequest(credits=999, delta=False)
        response = await mod.admin_set_credits(
            user_id=TARGET_USER_ID, body=body, request=request, caller=_make_caller()
        )

    import json
    data = json.loads(response.body)
    assert "user_id" in data
    assert "new_balance" in data
    assert data["user_id"] == TARGET_USER_ID
    assert data["new_balance"] == 999
