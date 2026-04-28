"""Combined regressions for V-01 and V-02 vault hardening.

V-01 pins the Redis URL validator to hostname parsing rather than substring
matching, so userinfo and hostile subdomains cannot bypass the TLS policy.

V-02 pins the worker fail-closed path so a task that requires vault secrets
still runs the normal settlement helper when vault fetch fails, refunding the
full reservation through the existing idempotent ledger RPC path.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from mariana.vault.runtime import VaultUnavailableError, _validate_redis_url_for_vault


# ---------------------------------------------------------------------------
# V-01 — URL validator must parse hostname, not substring-match the raw URL.
# ---------------------------------------------------------------------------


def test_substring_bypass_localhost_subdomain_rejected():
    with pytest.raises(ValueError):
        _validate_redis_url_for_vault("redis://localhost.attacker.com:6379")


def test_substring_bypass_userinfo_localhost_rejected():
    with pytest.raises(ValueError):
        _validate_redis_url_for_vault("redis://localhost@evil.com:6379")


def test_substring_bypass_127_subdomain_rejected():
    with pytest.raises(ValueError):
        _validate_redis_url_for_vault("redis://127.0.attacker.com:6379")


def test_substring_bypass_redis_userinfo_rejected():
    with pytest.raises(ValueError):
        _validate_redis_url_for_vault("redis://redis:secret@evil.com:6379")


def test_legitimate_local_still_allowed():
    _validate_redis_url_for_vault("redis://localhost:6379")
    _validate_redis_url_for_vault("redis://127.0.0.1:6379")
    _validate_redis_url_for_vault("redis://[::1]:6379")
    _validate_redis_url_for_vault("redis://redis:6379")


def test_legitimate_remote_with_tls_allowed():
    _validate_redis_url_for_vault("rediss://example.com:6379")


def test_malformed_url_rejected():
    with pytest.raises(ValueError):
        _validate_redis_url_for_vault("not-a-url")


def test_data_cache_uses_same_validator():
    from mariana.data import cache as cache_mod

    async def _run() -> None:
        for url in (
            "redis://localhost.attacker.com:6379",
            "redis://localhost@evil.com:6379",
            "redis://127.0.attacker.com:6379",
            "redis://redis:secret@evil.com:6379",
        ):
            with pytest.raises(ValueError):
                await cache_mod.create_redis_client(url)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# V-02 — worker vault fail-closed path must still settle/refund reservation.
# ---------------------------------------------------------------------------


PGHOST = os.environ.get("PGHOST", "/tmp")
PGPORT = int(os.environ.get("PGPORT", "55432"))
PGUSER = os.environ.get("PGUSER", "postgres")
PGDATABASE = os.environ.get("PGDATABASE", "testdb")

try:
    import asyncpg  # type: ignore  # noqa: F401
    import psycopg2  # type: ignore

    _conn = psycopg2.connect(host=PGHOST, port=PGPORT, user=PGUSER, dbname=PGDATABASE)
    _conn.close()
    _PG_AVAILABLE = True
except Exception:
    _PG_AVAILABLE = False

_pg_only = pytest.mark.skipif(not _PG_AVAILABLE, reason="Local PG not available")

_AGENT_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "mariana" / "agent" / "schema.sql"
)


async def _open_pool():
    import asyncpg as _asyncpg  # noqa: PLC0415

    return await _asyncpg.create_pool(
        host=PGHOST,
        port=PGPORT,
        user=PGUSER,
        database=PGDATABASE,
        min_size=2,
        max_size=8,
    )


async def _ensure_schema(pool: Any) -> None:
    sql = _AGENT_SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)


def _cfg():
    from mariana.config import AppConfig  # noqa: PLC0415

    cfg = AppConfig.__new__(AppConfig)
    object.__setattr__(cfg, "SUPABASE_URL", "https://supabase.test")
    object.__setattr__(cfg, "SUPABASE_KEY", "anon")
    object.__setattr__(cfg, "SUPABASE_SERVICE_ROLE_KEY", "service_role_xxx")
    object.__setattr__(cfg, "STRIPE_SECRET_KEY", "sk_test_xxx")
    object.__setattr__(cfg, "STRIPE_PUBLISHABLE_KEY", "pk_test_xxx")
    object.__setattr__(cfg, "STRIPE_WEBHOOK_SECRET", "whsec_xxx")
    return cfg


class _ScriptedClient:
    def __init__(self, calls: list[dict[str, Any]] | None = None, status: int = 200):
        self.calls = calls if calls is not None else []
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        outer = self

        class _R:
            status_code = outer.status
            text = "{}"

            def json(self_inner):
                return {"status": "granted", "balance_after": 1000}

        return _R()


def _new_task():
    from mariana.agent.models import AgentTask

    task = AgentTask(
        id=str(uuid.uuid4()),
        user_id=f"user-v02-{uuid.uuid4().hex[:8]}",
        goal="V-02 vault fail refund",
        budget_usd=5.0,
        spent_usd=0.0,
        requires_vault=True,
    )
    task.reserved_credits = 100
    task.credits_settled = False
    return task


@_pg_only
@pytest.mark.asyncio
async def test_worker_vault_fail_refunds_reservation():
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM agent_settlements")
            await conn.execute("DELETE FROM agent_tasks")

        task = _new_task()
        await _insert_agent_task(pool, task)

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls, status=200)
        real_settle = loop_mod._settle_agent_credits

        async def _settle_passthrough(*a, **kw):
            return await real_settle(*a, **kw)

        settle_spy = AsyncMock(side_effect=_settle_passthrough)

        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client), \
             patch.object(loop_mod, "fetch_vault_env", AsyncMock(side_effect=VaultUnavailableError("redis down"))), \
             patch.object(loop_mod, "_settle_agent_credits", settle_spy):
            out = await loop_mod.run_agent_task(task, db=pool, redis=None)

        assert out.state == AgentState.FAILED
        assert settle_spy.await_count == 1
        assert any(call[0][0].id == task.id for call in settle_spy.await_args_list)
        refund_calls = [c for c in rpc_calls if c["url"].endswith("/rest/v1/rpc/grant_credits")]
        assert len(refund_calls) == 1, f"expected one refund RPC, got {rpc_calls}"
        assert refund_calls[0]["json"]["p_credits"] == 100
        assert refund_calls[0]["json"]["p_source"] == "refund"
        assert refund_calls[0]["json"]["p_ref_type"] == "agent_task"
        assert refund_calls[0]["json"]["p_ref_id"] == task.id
    finally:
        await pool.close()


@_pg_only
@pytest.mark.asyncio
async def test_worker_vault_unexpected_exception_refunds_reservation():
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM agent_settlements")
            await conn.execute("DELETE FROM agent_tasks")

        task = _new_task()
        await _insert_agent_task(pool, task)

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls, status=200)
        real_settle = loop_mod._settle_agent_credits

        async def _settle_passthrough(*a, **kw):
            return await real_settle(*a, **kw)

        settle_spy = AsyncMock(side_effect=_settle_passthrough)

        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client), \
             patch.object(loop_mod, "fetch_vault_env", AsyncMock(side_effect=RuntimeError("boom"))), \
             patch.object(loop_mod, "_settle_agent_credits", settle_spy):
            out = await loop_mod.run_agent_task(task, db=pool, redis=None)

        assert out.state == AgentState.FAILED
        assert settle_spy.await_count == 1
        refund_calls = [c for c in rpc_calls if c["url"].endswith("/rest/v1/rpc/grant_credits")]
        assert len(refund_calls) == 1, f"expected one refund RPC, got {rpc_calls}"
        assert refund_calls[0]["json"]["p_credits"] == 100
        assert refund_calls[0]["json"]["p_ref_id"] == task.id
    finally:
        await pool.close()
