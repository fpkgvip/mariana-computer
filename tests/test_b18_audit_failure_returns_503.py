"""B-18 regression suite: Admin mutation routes return 503 when audit log fails.

After the B-18 fix, admin routes that call _audit_or_503() will raise
HTTPException(503) when admin_audit_insert fails, instead of silently
swallowing the exception.

Test IDs:
  1. audit_or_503_raises_503_on_http_exception
  2. audit_or_503_raises_503_on_unexpected_exception
  3. audit_or_503_succeeds_when_rpc_succeeds
  4. feature_flag_upsert_returns_503_when_audit_fails
  5. feature_flag_delete_returns_503_when_audit_fails
  6. danger_flush_redis_returns_503_when_audit_fails
  7. danger_halt_running_returns_503_when_audit_fails
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request as StarletteRequest

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
    object.__setattr__(cfg, "POSTGRES_DSN", "postgresql://x@localhost/testdb")
    object.__setattr__(cfg, "REDIS_URL", "redis://localhost:6379")
    object.__setattr__(cfg, "DATA_ROOT", "/tmp")
    return cfg


def _make_request() -> StarletteRequest:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/admin/test",
        "headers": [(b"authorization", b"Bearer fake-jwt"), (b"user-agent", b"test")],
        "query_string": b"",
        "client": ("127.0.0.1", 9000),
    }
    return StarletteRequest(scope)


# ---------------------------------------------------------------------------
# _audit_or_503 unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_or_503_raises_503_on_http_exception():
    """_audit_or_503 must raise HTTPException(503) when admin_audit_insert fails."""
    async def fail_rpc(request, fn, payload, **kwargs):
        raise HTTPException(status_code=502, detail="DB down")

    with patch.object(mod, "_admin_rpc_call", side_effect=fail_rpc):
        request = _make_request()
        with pytest.raises(HTTPException) as exc_info:
            await mod._audit_or_503(
                request,
                actor="actor-uuid",
                action="test.action",
                target_type="test",
                target_id="target-id",
            )
        assert exc_info.value.status_code == 503, (
            f"Expected 503, got {exc_info.value.status_code}"
        )


@pytest.mark.asyncio
async def test_audit_or_503_raises_503_on_unexpected_exception():
    """_audit_or_503 must raise 503 on any unexpected exception from the RPC."""
    async def broken_rpc(request, fn, payload, **kwargs):
        raise RuntimeError("network timeout")

    with patch.object(mod, "_admin_rpc_call", side_effect=broken_rpc):
        request = _make_request()
        with pytest.raises(HTTPException) as exc_info:
            await mod._audit_or_503(
                request,
                actor="actor",
                action="test.action",
                target_type="system",
                target_id="redis",
            )
        assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_audit_or_503_succeeds_when_rpc_succeeds():
    """_audit_or_503 must not raise when admin_audit_insert succeeds."""
    async def ok_rpc(request, fn, payload, **kwargs):
        return {"id": "audit-row-id"}

    with patch.object(mod, "_admin_rpc_call", side_effect=ok_rpc):
        request = _make_request()
        # Should not raise.
        await mod._audit_or_503(
            request,
            actor="actor",
            action="test.action",
            target_type="system",
            target_id="something",
        )


# ---------------------------------------------------------------------------
# Route-level tests (admin_feature_flags_upsert, _delete, danger routes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_flag_upsert_returns_503_when_audit_fails():
    """POST /api/admin/feature-flags must return 503 if audit write fails."""
    import httpx

    cfg = _make_cfg()
    caller = {"user_id": "admin-uuid"}

    # Simulate successful flag upsert but failing audit.
    mock_rest_resp = MagicMock()
    mock_rest_resp.status_code = 201
    mock_rest_resp.json.return_value = [{"key": "my_flag", "enabled": True}]

    async def fail_audit(request, fn, payload, **kwargs):
        if fn == "admin_audit_insert":
            raise HTTPException(status_code=502, detail="audit DB down")
        return None

    with (
        patch.object(mod, "_get_config", return_value=cfg),
        patch.object(mod, "_require_admin", return_value=caller),
        patch.object(mod, "_admin_rest_request", return_value=mock_rest_resp),
        patch.object(mod, "_admin_rpc_call", side_effect=fail_audit),
    ):
        request = _make_request()
        body = mod.AdminFeatureFlagUpsert(key="my_flag", enabled=True)
        with pytest.raises(HTTPException) as exc_info:
            await mod.admin_feature_flags_upsert(
                body=body, request=request, caller=caller
            )
        assert exc_info.value.status_code == 503, (
            f"Expected 503 on audit failure, got {exc_info.value.status_code}"
        )


@pytest.mark.asyncio
async def test_feature_flag_delete_returns_503_when_audit_fails():
    """DELETE /api/admin/feature-flags/{key} must return 503 if audit write fails."""
    cfg = _make_cfg()
    caller = {"user_id": "admin-uuid"}

    mock_rest_resp = MagicMock()
    mock_rest_resp.status_code = 204

    async def fail_audit(request, fn, payload, **kwargs):
        if fn == "admin_audit_insert":
            raise HTTPException(status_code=500, detail="audit DB down")
        return None

    with (
        patch.object(mod, "_get_config", return_value=cfg),
        patch.object(mod, "_require_admin", return_value=caller),
        patch.object(mod, "_admin_rest_request", return_value=mock_rest_resp),
        patch.object(mod, "_admin_rpc_call", side_effect=fail_audit),
    ):
        request = _make_request()
        with pytest.raises(HTTPException) as exc_info:
            await mod.admin_feature_flags_delete(
                key="my_flag", request=request, caller=caller
            )
        assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_danger_flush_redis_returns_503_when_audit_fails():
    """POST /api/admin/danger/flush-redis must return 503 if audit write fails."""
    cfg = _make_cfg()
    caller = {"user_id": "admin-uuid"}

    mock_redis = AsyncMock()
    mock_redis.flushdb = AsyncMock(return_value=None)

    async def fail_audit(request, fn, payload, **kwargs):
        if fn == "admin_audit_insert":
            raise HTTPException(status_code=502, detail="audit down")
        return None

    with (
        patch.object(mod, "_get_config", return_value=cfg),
        patch.object(mod, "_require_admin", return_value=caller),
        patch.object(mod, "_redis", mock_redis),
        patch.object(mod, "_admin_rpc_call", side_effect=fail_audit),
    ):
        request = _make_request()
        body = mod.AdminDangerConfirm(confirm="I UNDERSTAND")
        with pytest.raises(HTTPException) as exc_info:
            await mod.admin_danger_flush_redis(
                body=body, request=request, caller=caller
            )
        assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_danger_halt_running_returns_503_when_audit_fails():
    """POST /api/admin/danger/halt-running must return 503 if audit write fails."""
    cfg = _make_cfg()
    caller = {"user_id": "admin-uuid"}

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value="UPDATE 3")

    async def fail_audit(request, fn, payload, **kwargs):
        if fn == "admin_audit_insert":
            raise HTTPException(status_code=503, detail="audit DB unreachable")
        return None

    with (
        patch.object(mod, "_get_config", return_value=cfg),
        patch.object(mod, "_require_admin", return_value=caller),
        patch.object(mod, "_db_pool", mock_db),
        patch.object(mod, "_admin_rpc_call", side_effect=fail_audit),
    ):
        request = _make_request()
        body = mod.AdminDangerConfirm(confirm="I UNDERSTAND")
        with pytest.raises(HTTPException) as exc_info:
            await mod.admin_danger_halt_running(
                body=body, request=request, caller=caller
            )
        assert exc_info.value.status_code == 503
