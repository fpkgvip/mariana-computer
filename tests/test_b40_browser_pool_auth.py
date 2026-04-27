"""B-40 regression suite: browser pool server requires auth on /dispatch and /pool/status.

Before the fix, /dispatch and /pool/status had no authentication.  Any process
that could reach the port (SSRF, misconfigured BROWSER_POOL_HOST) could dispatch
arbitrary browser tasks or read pool metrics.

After the fix, both endpoints require an X-Browser-Pool-Token header whose value
matches the BROWSER_POOL_SECRET env var.  /health remains unauthenticated.

Test IDs:
  1. test_dispatch_missing_token_returns_401
  2. test_dispatch_wrong_token_returns_401
  3. test_dispatch_correct_token_returns_503
  4. test_pool_status_missing_token_returns_401
  5. test_pool_status_wrong_token_returns_401
  6. test_pool_status_correct_token_returns_200
  7. test_health_no_auth_required
  8. test_no_secret_configured_allows_all (dev mode)
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from httpx import AsyncClient, ASGITransport


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------

from mariana.browser.pool_server import app as _pool_app

_VALID_DISPATCH_PAYLOAD = {
    "url": "https://example.com/page",
    "task_id": "task-aabbccdd",
}


# ---------------------------------------------------------------------------
# Test 1: /dispatch missing token → 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_missing_token_returns_401():
    """B-40: POST /dispatch without X-Browser-Pool-Token must return 401."""
    with patch.dict(os.environ, {"BROWSER_POOL_SECRET": "correct-secret-xyz"}):
        async with AsyncClient(transport=ASGITransport(app=_pool_app), base_url="http://test") as client:
            resp = await client.post("/dispatch", json=_VALID_DISPATCH_PAYLOAD)
    assert resp.status_code == 401, (
        f"B-40: missing token on /dispatch should yield 401, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 2: /dispatch wrong token → 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_wrong_token_returns_401():
    """B-40: POST /dispatch with wrong token must return 401."""
    with patch.dict(os.environ, {"BROWSER_POOL_SECRET": "correct-secret-xyz"}):
        async with AsyncClient(transport=ASGITransport(app=_pool_app), base_url="http://test") as client:
            resp = await client.post(
                "/dispatch",
                json=_VALID_DISPATCH_PAYLOAD,
                headers={"X-Browser-Pool-Token": "wrong-secret"},
            )
    assert resp.status_code == 401, (
        f"B-40: wrong token on /dispatch should yield 401, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 3: /dispatch correct token → 503 (prototype unavailable, but auth passed)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_correct_token_returns_503():
    """B-40: POST /dispatch with correct token passes auth and gets prototype 503."""
    secret = "correct-secret-xyz"
    with patch.dict(os.environ, {"BROWSER_POOL_SECRET": secret}):
        async with AsyncClient(transport=ASGITransport(app=_pool_app), base_url="http://test") as client:
            resp = await client.post(
                "/dispatch",
                json=_VALID_DISPATCH_PAYLOAD,
                headers={"X-Browser-Pool-Token": secret},
            )
    # Prototype always returns 503 for actual dispatch, but NOT 401
    assert resp.status_code == 503, (
        f"B-40: authenticated /dispatch should yield 503 (prototype), got {resp.status_code}"
    )
    assert resp.status_code != 401


# ---------------------------------------------------------------------------
# Test 4: /pool/status missing token → 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pool_status_missing_token_returns_401():
    """B-40: GET /pool/status without token must return 401."""
    with patch.dict(os.environ, {"BROWSER_POOL_SECRET": "pool-secret-abc"}):
        async with AsyncClient(transport=ASGITransport(app=_pool_app), base_url="http://test") as client:
            resp = await client.get("/pool/status")
    assert resp.status_code == 401, (
        f"B-40: missing token on /pool/status should yield 401, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 5: /pool/status wrong token → 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pool_status_wrong_token_returns_401():
    """B-40: GET /pool/status with wrong token must return 401."""
    with patch.dict(os.environ, {"BROWSER_POOL_SECRET": "pool-secret-abc"}):
        async with AsyncClient(transport=ASGITransport(app=_pool_app), base_url="http://test") as client:
            resp = await client.get(
                "/pool/status",
                headers={"X-Browser-Pool-Token": "totally-wrong"},
            )
    assert resp.status_code == 401, (
        f"B-40: wrong token on /pool/status should yield 401, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 6: /pool/status correct token → 200
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pool_status_correct_token_returns_200():
    """B-40: GET /pool/status with correct token must return 200."""
    secret = "pool-secret-abc"
    with patch.dict(os.environ, {"BROWSER_POOL_SECRET": secret}):
        async with AsyncClient(transport=ASGITransport(app=_pool_app), base_url="http://test") as client:
            resp = await client.get(
                "/pool/status",
                headers={"X-Browser-Pool-Token": secret},
            )
    assert resp.status_code == 200, (
        f"B-40: authenticated /pool/status should yield 200, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 7: /health requires no authentication
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_no_auth_required():
    """GET /health must return 200 without any authentication token."""
    with patch.dict(os.environ, {"BROWSER_POOL_SECRET": "any-secret-here"}):
        async with AsyncClient(transport=ASGITransport(app=_pool_app), base_url="http://test") as client:
            resp = await client.get("/health")
    assert resp.status_code == 200, (
        f"B-40: /health must be unauthenticated (200), got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 8: no BROWSER_POOL_SECRET configured → all requests allowed (dev mode)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_secret_configured_allows_all():
    """When BROWSER_POOL_SECRET is empty, all requests are allowed (dev/test mode)."""
    env_without_secret = {k: v for k, v in os.environ.items() if k != "BROWSER_POOL_SECRET"}
    env_without_secret["BROWSER_POOL_SECRET"] = ""

    with patch.dict(os.environ, env_without_secret, clear=True):
        async with AsyncClient(transport=ASGITransport(app=_pool_app), base_url="http://test") as client:
            resp = await client.get("/pool/status")
    # Without a configured secret, the guard is bypassed
    assert resp.status_code == 200, (
        f"B-40: no secret configured → dev mode allows all, got {resp.status_code}"
    )
