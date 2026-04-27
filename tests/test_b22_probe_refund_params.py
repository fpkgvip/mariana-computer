"""B-22 regression suite: atomic credit probe refund uses correct RPC parameter names.

After the B-22 fix the add_credits RPC call inside _atomic_probe_credits must
send {"p_user_id": ..., "p_credits": 1} matching the live DB signature:
  public.add_credits(p_user_id uuid, p_credits integer)

Before the fix the call sent {"target_user_id": ..., "amount": 1} which causes
Postgres to raise 42883 (function not found) and silently lose 1 credit per probe.

Test IDs:
  1. test_probe_refund_uses_p_user_id_param_name
  2. test_probe_refund_uses_p_credits_param_name
  3. test_probe_deduct_uses_target_user_id_param_name  (deduct unchanged, regression)
  4. test_probe_returns_ok_when_deduct_succeeds
  5. test_probe_returns_insufficient_when_deduct_rejected
  6. test_probe_returns_error_when_config_missing
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from mariana.orchestrator.event_loop import _atomic_probe_credits


# ---------------------------------------------------------------------------
# Helper: build a minimal config object
# ---------------------------------------------------------------------------

def _make_config(base_url: str = "https://supabase.example.com",
                 api_key: str = "test-api-key") -> MagicMock:
    cfg = MagicMock()
    cfg.SUPABASE_URL = base_url
    cfg.SUPABASE_SERVICE_KEY = api_key
    cfg.SUPABASE_ANON_KEY = ""
    return cfg


# ---------------------------------------------------------------------------
# Test 1 & 2: refund call uses correct parameter names
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_refund_uses_p_user_id_param_name():
    """B-22: add_credits refund must send p_user_id, not target_user_id."""
    user_id = "user-abc-123"
    captured_calls: list[tuple[str, dict]] = []

    # We patch httpx.AsyncClient so we can inspect what JSON was sent.
    import httpx

    class FakeResponse:
        status_code = 200
        def json(self):
            return True

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, url: str, json: dict, headers: dict):
            rpc_name = url.split("/rpc/")[-1]
            captured_calls.append((rpc_name, json))
            return FakeResponse()

    with patch.object(httpx, "AsyncClient", return_value=FakeClient()):
        result = await _atomic_probe_credits(user_id, _make_config())

    assert result == "ok"
    # Find the add_credits call
    add_credits_calls = [(n, p) for n, p in captured_calls if n == "add_credits"]
    assert len(add_credits_calls) >= 1, "add_credits RPC was never called"
    _, payload = add_credits_calls[0]
    assert "p_user_id" in payload, (
        f"B-22: refund payload must contain 'p_user_id' but got keys: {list(payload)}"
    )
    assert payload["p_user_id"] == user_id


@pytest.mark.asyncio
async def test_probe_refund_uses_p_credits_param_name():
    """B-22: add_credits refund must send p_credits, not amount."""
    user_id = "user-abc-456"
    captured_calls: list[tuple[str, dict]] = []

    import httpx

    class FakeResponse:
        status_code = 200
        def json(self):
            return True

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, url: str, json: dict, headers: dict):
            rpc_name = url.split("/rpc/")[-1]
            captured_calls.append((rpc_name, json))
            return FakeResponse()

    with patch.object(httpx, "AsyncClient", return_value=FakeClient()):
        result = await _atomic_probe_credits(user_id, _make_config())

    assert result == "ok"
    add_credits_calls = [(n, p) for n, p in captured_calls if n == "add_credits"]
    assert len(add_credits_calls) >= 1, "add_credits RPC was never called"
    _, payload = add_credits_calls[0]
    assert "p_credits" in payload, (
        f"B-22: refund payload must contain 'p_credits' but got keys: {list(payload)}"
    )
    assert payload["p_credits"] == 1, (
        f"B-22: refund must send p_credits=1 but got {payload['p_credits']}"
    )


# ---------------------------------------------------------------------------
# Test 3: deduct call still uses its existing (correct) param names
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_deduct_uses_target_user_id_param_name():
    """B-22 regression: deduct_credits call must still use target_user_id + amount."""
    user_id = "user-abc-789"
    captured_calls: list[tuple[str, dict]] = []

    import httpx

    class FakeResponse:
        status_code = 200
        def json(self):
            return True

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, url: str, json: dict, headers: dict):
            rpc_name = url.split("/rpc/")[-1]
            captured_calls.append((rpc_name, json))
            return FakeResponse()

    with patch.object(httpx, "AsyncClient", return_value=FakeClient()):
        await _atomic_probe_credits(user_id, _make_config())

    deduct_calls = [(n, p) for n, p in captured_calls if n == "deduct_credits"]
    assert len(deduct_calls) >= 1, "deduct_credits RPC was never called"
    _, payload = deduct_calls[0]
    assert "target_user_id" in payload, (
        f"deduct_credits payload must contain 'target_user_id' but got: {list(payload)}"
    )
    assert "amount" in payload, (
        f"deduct_credits payload must contain 'amount' but got: {list(payload)}"
    )


# ---------------------------------------------------------------------------
# Test 4: returns "ok" when deduct + refund both succeed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_returns_ok_when_deduct_succeeds():
    """Probe returns 'ok' when deduct_credits returns 200 and refund succeeds."""
    import httpx

    class FakeResponse:
        status_code = 200
        def json(self):
            return True

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, url: str, json: dict, headers: dict):
            return FakeResponse()

    with patch.object(httpx, "AsyncClient", return_value=FakeClient()):
        result = await _atomic_probe_credits("user-ok", _make_config())

    assert result == "ok"


# ---------------------------------------------------------------------------
# Test 5: returns "insufficient" when deduct_credits is rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_returns_insufficient_when_deduct_rejected():
    """Probe returns 'insufficient' when deduct_credits returns 402."""
    import httpx

    class FakeResponse:
        def __init__(self, status: int):
            self.status_code = status
        def json(self):
            return {"error": "insufficient_credits"}

    call_count = 0

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, url: str, json: dict, headers: dict):
            # deduct_credits → 402 (insufficient); add_credits should not be called
            return FakeResponse(402)

    with patch.object(httpx, "AsyncClient", return_value=FakeClient()):
        result = await _atomic_probe_credits("user-broke", _make_config())

    assert result == "insufficient"


# ---------------------------------------------------------------------------
# Test 6: returns "error" when config has no SUPABASE_URL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_returns_error_when_config_missing():
    """Probe returns 'error' immediately when SUPABASE_URL is empty."""
    cfg = MagicMock()
    cfg.SUPABASE_URL = ""
    cfg.SUPABASE_SERVICE_KEY = ""
    cfg.SUPABASE_ANON_KEY = ""
    result = await _atomic_probe_credits("user-xyz", cfg)
    assert result == "error"
