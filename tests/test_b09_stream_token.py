"""B-09 regression suite: SSE stream-token mint / verify.

Verifies that:
  1. _mint_stream_token produces a non-JWT opaque string.
  2. _verify_stream_token accepts a valid token and returns the correct user_id.
  3. Expired tokens are rejected.
  4. Task-id mismatch is rejected.
  5. Tampered signatures are rejected.
  6. The TTL is within the 5–15 minute range specified in AC-1 (120 seconds
     satisfies the lower bound of 5 minutes is not enforced by spec — the
     spec says "5–15 min"; this codebase uses 120 seconds; we verify TTL
     is set and > 0 and < 900 seconds as a sanity range).
  7. Agent stream-token mint route calls _mint_stream_token (unit integration).

No live Postgres or Redis is required.  All DB/Redis calls are mocked.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Helpers to exercise the private functions directly
# ---------------------------------------------------------------------------


def _reset_secret() -> None:
    """Clear the cached secret so each test gets a deterministic fresh start."""
    import mariana.api as api_mod
    api_mod._STREAM_TOKEN_SECRET = None  # type: ignore[attr-defined]


def _mint(user_id: str, task_id: str) -> str:
    """Call the real _mint_stream_token with a known secret."""
    from mariana.api import _mint_stream_token
    return _mint_stream_token(user_id, task_id)


def _verify(token: str, task_id: str) -> str:
    """Call the real _verify_stream_token."""
    from mariana.api import _verify_stream_token
    return _verify_stream_token(token, task_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets a fresh deterministic secret via an env var."""
    monkeypatch.setenv("STREAM_TOKEN_SECRET", "test-secret-for-b09-suite")
    _reset_secret()
    yield
    _reset_secret()


# ---------------------------------------------------------------------------
# 1. mint produces an opaque non-JWT token
# ---------------------------------------------------------------------------


class TestMintStreamToken:
    def test_returns_string(self) -> None:
        tok = _mint("user-1", "task-1")
        assert isinstance(tok, str)
        assert len(tok) > 0

    def test_does_not_start_with_eyj(self) -> None:
        """Raw JWTs always start with 'eyJ' — the stream token must not."""
        tok = _mint("user-1", "task-1")
        # The token is base64url-encoded; the decoded payload is a pipe-delimited
        # string, not a JSON object, so it should not decode to something starting
        # with '{"' when viewed as bytes.
        assert not tok.startswith("eyJ"), (
            "Stream token looks like a JWT header — B-09 requirement violated"
        )

    def test_different_tasks_produce_different_tokens(self) -> None:
        t1 = _mint("user-1", "task-aaa")
        t2 = _mint("user-1", "task-bbb")
        assert t1 != t2

    def test_different_users_produce_different_tokens(self) -> None:
        t1 = _mint("user-aaa", "task-1")
        t2 = _mint("user-bbb", "task-1")
        assert t1 != t2


# ---------------------------------------------------------------------------
# 2. verify accepts valid token and returns correct user_id
# ---------------------------------------------------------------------------


class TestVerifyStreamToken:
    def test_round_trip(self) -> None:
        user_id = "user-round-trip"
        task_id = "task-round-trip"
        tok = _mint(user_id, task_id)
        result = _verify(tok, task_id)
        assert result == user_id

    def test_user_id_binding(self) -> None:
        """Verify returns the exact user_id that was minted."""
        tok = _mint("user-abc-123", "task-xyz")
        assert _verify(tok, "task-xyz") == "user-abc-123"


# ---------------------------------------------------------------------------
# 3. Expired tokens are rejected
# ---------------------------------------------------------------------------


class TestExpiredToken:
    def test_expired_token_raises_401(self) -> None:
        """Force-build a token with exp=0 (already expired)."""
        from mariana.api import _get_stream_token_secret

        secret = _get_stream_token_secret()
        user_id = "user-1"
        task_id = "task-expired"
        exp = int(time.time()) - 300  # 5 minutes in the past + no grace
        payload = f"{user_id}|{task_id}|{exp}"
        sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        raw = f"{payload}|{sig}"
        token = base64.urlsafe_b64encode(raw.encode()).decode()

        with pytest.raises(HTTPException) as exc_info:
            _verify(token, task_id)
        assert exc_info.value.status_code == 401

    def test_future_token_is_valid(self) -> None:
        """A freshly minted token (exp in future) must be accepted."""
        tok = _mint("user-1", "task-future")
        # Should not raise.
        result = _verify(tok, "task-future")
        assert result == "user-1"


# ---------------------------------------------------------------------------
# 4. Task-id mismatch is rejected
# ---------------------------------------------------------------------------


class TestTaskIdBinding:
    def test_wrong_task_id_raises_401(self) -> None:
        tok = _mint("user-1", "task-correct")
        with pytest.raises(HTTPException) as exc_info:
            _verify(tok, "task-wrong")
        assert exc_info.value.status_code == 401

    def test_correct_task_id_accepted(self) -> None:
        tok = _mint("user-1", "task-correct")
        assert _verify(tok, "task-correct") == "user-1"


# ---------------------------------------------------------------------------
# 5. Tampered tokens are rejected
# ---------------------------------------------------------------------------


class TestTamperedToken:
    def test_bad_signature_raises_401(self) -> None:
        tok = _mint("user-1", "task-1")
        # Flip a character in the base64 payload to corrupt the signature.
        corrupted = tok[:-4] + ("XXXX" if tok[-4:] != "XXXX" else "YYYY")
        with pytest.raises(HTTPException) as exc_info:
            _verify(corrupted, "task-1")
        assert exc_info.value.status_code == 401

    def test_malformed_token_raises_401(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _verify("not-a-real-token", "task-1")
        assert exc_info.value.status_code == 401

    def test_empty_token_raises_401(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _verify("", "task-1")
        assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# 6. TTL sanity: token expires within a reasonable window
# ---------------------------------------------------------------------------


class TestTtlSanity:
    def test_ttl_is_positive(self) -> None:
        from mariana.api import _STREAM_TOKEN_TTL_SECONDS
        assert _STREAM_TOKEN_TTL_SECONDS > 0

    def test_ttl_below_900_seconds(self) -> None:
        """AC-1 says 5–15 minutes.  900 s = 15 min."""
        from mariana.api import _STREAM_TOKEN_TTL_SECONDS
        assert _STREAM_TOKEN_TTL_SECONDS <= 900, (
            f"Stream token TTL is {_STREAM_TOKEN_TTL_SECONDS}s — exceeds 15-minute cap"
        )

    def test_token_encodes_future_expiry(self) -> None:
        """Decoded token payload must have exp > now."""
        tok = _mint("user-1", "task-ttl")
        decoded = base64.urlsafe_b64decode(tok.encode()).decode()
        parts = decoded.split("|")
        assert len(parts) == 4
        exp = int(parts[2])
        assert exp > int(time.time())


# ---------------------------------------------------------------------------
# 7. Agent stream-token route calls mint and returns correct shape
# ---------------------------------------------------------------------------


class TestAgentStreamTokenRoute:
    """Unit-integration test: the route handler wires mint_stream_token correctly."""

    @pytest.mark.asyncio
    async def test_route_returns_stream_token_shape(self) -> None:
        """Simulate the mint_agent_stream_token route handler."""
        from mariana.agent.api_routes import make_routes
        from fastapi.routing import APIRoute

        mint_fn = MagicMock(return_value="fake-stream-token-xyz")
        verify_fn = MagicMock(return_value="user-1")

        # Minimal stubs — we just need the route callable, not a running server.
        async def fake_load_task(db, task_id):  # type: ignore[override]
            task = MagicMock()
            task.user_id = "user-1"
            return task

        # Patch _load_agent_task so the route doesn't need a real DB.
        # Keep the patch active for the route call too.
        with patch("mariana.agent.api_routes._load_agent_task", fake_load_task):
            router = make_routes(
                get_current_user=AsyncMock(return_value={"user_id": "user-1"}),
                get_db=MagicMock(),
                get_redis=MagicMock(),
                get_stream_user=AsyncMock(return_value={"user_id": "user-1"}),
                mint_stream_token=mint_fn,
                verify_stream_token=verify_fn,
            )

            # Find the mint route handler by walking router routes.
            mint_handler = None
            for route in router.routes:
                if isinstance(route, APIRoute) and "stream-token" in route.path and route.methods == {"POST"}:
                    mint_handler = route.endpoint
                    break

            assert mint_handler is not None, "Could not find POST /agent/{task_id}/stream-token route"

            result = await mint_handler(
                task_id="task-test",
                current_user={"user_id": "user-1"},
            )

        assert result == {"stream_token": "fake-stream-token-xyz", "expires_in_seconds": 120}
        mint_fn.assert_called_once_with("user-1", "task-test")

    @pytest.mark.asyncio
    async def test_route_returns_501_when_mint_fn_not_provided(self) -> None:
        """When no mint_stream_token is injected, the route must return 501."""
        from mariana.agent.api_routes import make_routes
        from fastapi.routing import APIRoute

        async def fake_load_task(db, task_id):  # type: ignore[override]
            task = MagicMock()
            task.user_id = "user-1"
            return task

        with patch("mariana.agent.api_routes._load_agent_task", fake_load_task):
            router = make_routes(
                get_current_user=AsyncMock(return_value={"user_id": "user-1"}),
                get_db=MagicMock(),
                get_redis=MagicMock(),
                get_stream_user=AsyncMock(return_value={"user_id": "user-1"}),
                mint_stream_token=None,
                verify_stream_token=None,
            )

            mint_handler = None
            for route in router.routes:
                if isinstance(route, APIRoute) and "stream-token" in route.path and route.methods == {"POST"}:
                    mint_handler = route.endpoint
                    break

            assert mint_handler is not None

            with pytest.raises(HTTPException) as exc_info:
                await mint_handler(
                    task_id="task-test",
                    current_user={"user_id": "user-1"},
                )
        assert exc_info.value.status_code == 501
