"""N-01 regression suite: agent settlement metadata persistence.

Bug fixed
---------
M-01 added ``reserved_credits`` and ``credits_settled`` to the in-memory
:class:`mariana.agent.models.AgentTask` Pydantic model and populated
``reserved_credits`` during ``POST /api/agent``. But the Postgres-backed
``agent_tasks`` table, the ``_insert_agent_task`` INSERT, the
``_load_agent_task`` SELECT/reconstruction, and the ``_persist_task`` UPSERT
all lacked the two columns. The real worker path (``mariana/main.py:738-790``)
reloads each task via ``_load_agent_task`` before invoking ``run_agent_task``,
so the reloaded task always defaulted ``reserved_credits=0`` and
``credits_settled=False`` — which makes ``_settle_agent_credits`` return
immediately on the ``task.reserved_credits <= 0`` guard.

The fix (this file's regression coverage) persists both columns end-to-end:

* ``mariana/agent/schema.sql``           — adds the columns + idempotent ALTER.
* ``mariana/agent/api_routes.py``        — INSERT and SELECT cover both columns.
* ``mariana/agent/loop.py``              — UPSERT INSERT + ON CONFLICT SET cover both.
* ``mariana/agent/loop.py``              — settle runs BEFORE the final
                                           ``_persist_task`` so ``credits_settled=True``
                                           survives requeue.

Tests
-----
1. test_n01_schema_columns_present
2. test_n01_insert_persists_reserved_credits
3. test_n01_persist_task_upserts_credits_settled
4. test_n01_persist_task_upserts_reserved_credits_change
5. test_n01_round_trip_through_settlement
6. test_n01_round_trip_no_double_settle

These tests use a real local Postgres (PGHOST=/tmp PGPORT=55432) and bootstrap
the agent schema directly from ``mariana/agent/schema.sql`` if not already
loaded — mirroring how ``mariana.data.db.init_schema`` runs at startup.
"""

from __future__ import annotations

import json
import os
import pathlib
import uuid
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Local PG availability gate (mirrors test_i01_add_credits_lock pattern).
# ---------------------------------------------------------------------------

PGHOST = os.environ.get("PGHOST", "/tmp")
PGPORT = int(os.environ.get("PGPORT", "55432"))
PGUSER = os.environ.get("PGUSER", "postgres")
PGDATABASE = os.environ.get("PGDATABASE", "testdb")

try:
    import asyncpg  # type: ignore  # noqa: F401
    import psycopg2  # type: ignore

    _conn = psycopg2.connect(
        host=PGHOST, port=PGPORT, user=PGUSER, dbname=PGDATABASE
    )
    _conn.close()
    _PG_AVAILABLE = True
except Exception:
    _PG_AVAILABLE = False

_pg_only = pytest.mark.skipif(not _PG_AVAILABLE, reason="Local PG not available")


_AGENT_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "mariana"
    / "agent"
    / "schema.sql"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Pool:
    """Minimal asyncpg-like pool wrapper exposing acquire() as async ctx mgr.

    The production code calls ``async with db.acquire() as conn`` — both
    ``asyncpg.Pool`` and this shim satisfy that contract.
    """

    def __init__(self, real_pool: Any) -> None:
        self._pool = real_pool

    def acquire(self):
        return self._pool.acquire()


async def _open_pool():
    import asyncpg as _asyncpg  # noqa: PLC0415

    pool = await _asyncpg.create_pool(
        host=PGHOST,
        port=PGPORT,
        user=PGUSER,
        database=PGDATABASE,
        min_size=1,
        max_size=2,
    )
    return pool


async def _ensure_schema(pool: Any) -> None:
    """Apply mariana/agent/schema.sql so agent_tasks/agent_events exist.

    Idempotent: schema.sql uses CREATE TABLE / INDEX IF NOT EXISTS.
    """
    sql = _AGENT_SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)


def _new_task(
    *,
    reserved: int = 0,
    settled: bool = False,
    spent_usd: float = 0.0,
    state=None,
):
    from mariana.agent.models import AgentState, AgentTask  # noqa: PLC0415

    task = AgentTask(
        id=str(uuid.uuid4()),
        user_id=f"user-n01-{uuid.uuid4().hex[:8]}",
        goal="N-01 round-trip",
        budget_usd=5.0,
        spent_usd=spent_usd,
        state=state or AgentState.PLAN,
    )
    task.reserved_credits = reserved
    task.credits_settled = settled
    return task


# ---------------------------------------------------------------------------
# 1. Schema columns must exist with the right types.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_n01_schema_columns_present():
    """information_schema must show both columns on agent_tasks."""
    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'agent_tasks'
                  AND column_name IN ('reserved_credits', 'credits_settled')
                ORDER BY column_name
                """
            )
    finally:
        await pool.close()

    found = {r["column_name"]: r["data_type"] for r in rows}
    assert "reserved_credits" in found, (
        "agent_tasks.reserved_credits must exist after schema bootstrap "
        "(N-01: required for queue-consumer settlement)"
    )
    assert "credits_settled" in found
    # Postgres reports BIGINT as 'bigint' and BOOLEAN as 'boolean'.
    assert found["reserved_credits"] == "bigint", (
        f"reserved_credits must be bigint; got {found['reserved_credits']!r}"
    )
    assert found["credits_settled"] == "boolean", (
        f"credits_settled must be boolean; got {found['credits_settled']!r}"
    )


# ---------------------------------------------------------------------------
# 2. Insert must persist reserved_credits and credits_settled.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_n01_insert_persists_reserved_credits():
    """_insert_agent_task → _load_agent_task round-trip preserves the two columns."""
    from mariana.agent.api_routes import _insert_agent_task, _load_agent_task  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=500, settled=False)
        await _insert_agent_task(pool, task)

        reloaded = await _load_agent_task(pool, task.id)
    finally:
        await pool.close()

    assert reloaded is not None, "task should reload from Postgres"
    assert reloaded.reserved_credits == 500, (
        f"reserved_credits must round-trip through DB; got {reloaded.reserved_credits}"
    )
    assert reloaded.credits_settled is False, (
        f"credits_settled must round-trip; got {reloaded.credits_settled!r}"
    )


# ---------------------------------------------------------------------------
# 3. UPSERT must update credits_settled (post-settlement persistence).
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_n01_persist_task_upserts_credits_settled():
    """After settlement flips credits_settled=True, _persist_task must write it."""
    from mariana.agent.api_routes import _insert_agent_task, _load_agent_task  # noqa: PLC0415
    from mariana.agent.loop import _persist_task  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=500, settled=False)
        await _insert_agent_task(pool, task)

        # Simulate post-settlement mutation.
        task.credits_settled = True
        await _persist_task(pool, task)

        reloaded = await _load_agent_task(pool, task.id)
    finally:
        await pool.close()

    assert reloaded is not None
    assert reloaded.credits_settled is True, (
        "credits_settled=True must survive _persist_task UPSERT — required for "
        "requeue idempotency after a worker crash post-settlement"
    )
    # And reserved_credits is preserved across the UPSERT.
    assert reloaded.reserved_credits == 500


# ---------------------------------------------------------------------------
# 4. UPSERT must allow reserved_credits to change (covers the SET clause).
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_n01_persist_task_upserts_reserved_credits_change():
    """If the runtime mutates reserved_credits mid-flight, the UPSERT must reflect it."""
    from mariana.agent.api_routes import _insert_agent_task, _load_agent_task  # noqa: PLC0415
    from mariana.agent.loop import _persist_task  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=500, settled=False)
        await _insert_agent_task(pool, task)

        # Hypothetical partial-refund mid-flight reducing the reservation.
        task.reserved_credits = 300
        await _persist_task(pool, task)

        reloaded = await _load_agent_task(pool, task.id)
    finally:
        await pool.close()

    assert reloaded is not None
    assert reloaded.reserved_credits == 300, (
        "reserved_credits change must be persisted by UPSERT SET clause; "
        f"got {reloaded.reserved_credits}"
    )


# ---------------------------------------------------------------------------
# 5. Full round-trip: insert → reload → settle → persist → reload reflects settlement.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_n01_round_trip_through_settlement():
    """Mirror the queue-consumer path end-to-end.

    POST /api/agent inserts; worker reloads; terminal-state finally calls
    ``_settle_agent_credits`` then ``_persist_task``. After persistence, a
    second reload must observe ``credits_settled=True`` so a requeue or crash
    cannot trigger a second refund.
    """
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task, _load_agent_task  # noqa: PLC0415
    from mariana.agent.loop import _persist_task, _settle_agent_credits  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415
    from mariana.config import AppConfig  # noqa: PLC0415

    cfg = AppConfig.__new__(AppConfig)
    object.__setattr__(cfg, "SUPABASE_URL", "https://supabase.test")
    object.__setattr__(cfg, "SUPABASE_KEY", "anon")
    object.__setattr__(cfg, "SUPABASE_SERVICE_ROLE_KEY", "service_role_xxx")
    object.__setattr__(cfg, "STRIPE_SECRET_KEY", "sk_test_xxx")
    object.__setattr__(cfg, "STRIPE_PUBLISHABLE_KEY", "pk_test_xxx")
    object.__setattr__(cfg, "STRIPE_WEBHOOK_SECRET", "whsec_xxx")

    class _FakeResp:
        def __init__(self, status_code: int = 200, body: Any = None) -> None:
            self.status_code = status_code
            self._body = body if body is not None else {}
            self.text = json.dumps(self._body) if not isinstance(self._body, str) else self._body

        def json(self):
            return self._body

    class _ScriptedClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url: str, json=None, headers=None):
            self.calls.append({"method": "POST", "url": url, "json": json})
            return _FakeResp(200, True)

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        # POST /api/agent persists with reserved_credits=500.
        task = _new_task(reserved=500, settled=False, spent_usd=0.30)
        await _insert_agent_task(pool, task)

        # Worker (mariana/main.py) reloads via _load_agent_task — which is the
        # exact path that previously stripped the reservation metadata.
        reloaded = await _load_agent_task(pool, task.id)
        assert reloaded is not None
        assert reloaded.reserved_credits == 500, (
            "reload BEFORE settlement must preserve reservation; this is the "
            "core N-01 regression"
        )

        # Mark task as DONE and run the settlement helper exactly as the
        # ``finally:`` block in run_agent_task does.
        reloaded.state = AgentState.DONE
        client = _ScriptedClient()
        with patch.object(api_mod, "_get_config", lambda: cfg), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            await _settle_agent_credits(reloaded)

        refund_calls = [c for c in client.calls if "rpc/add_credits" in c["url"]]
        assert len(refund_calls) == 1, "expected exactly one add_credits refund"
        body = refund_calls[0]["json"]
        refund = body.get("p_credits") or body.get("credits") or body.get("amount")
        assert refund == 470, (
            f"expected refund of 470 (500 reserved - 30 spent_credits); got {refund}"
        )
        assert reloaded.credits_settled is True

        # Persist the settled state — this matches the order in
        # ``run_agent_task``'s ``finally:`` block (settle BEFORE persist).
        await _persist_task(pool, reloaded)

        # Second reload (simulates requeue / restart) must observe settled=True.
        reloaded_again = await _load_agent_task(pool, task.id)
    finally:
        await pool.close()

    assert reloaded_again is not None
    assert reloaded_again.credits_settled is True, (
        "credits_settled=True must survive a full reload from Postgres so a "
        "requeued worker does not double-refund"
    )
    assert reloaded_again.reserved_credits == 500


# ---------------------------------------------------------------------------
# 6. Already-settled task must short-circuit on the next worker pass.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_n01_round_trip_no_double_settle():
    """If credits_settled=True is persisted, the next reload's settle is a noop."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task, _load_agent_task  # noqa: PLC0415
    from mariana.agent.loop import _settle_agent_credits  # noqa: PLC0415
    from mariana.config import AppConfig  # noqa: PLC0415

    cfg = AppConfig.__new__(AppConfig)
    object.__setattr__(cfg, "SUPABASE_URL", "https://supabase.test")
    object.__setattr__(cfg, "SUPABASE_KEY", "anon")
    object.__setattr__(cfg, "SUPABASE_SERVICE_ROLE_KEY", "service_role_xxx")
    object.__setattr__(cfg, "STRIPE_SECRET_KEY", "sk_test_xxx")
    object.__setattr__(cfg, "STRIPE_PUBLISHABLE_KEY", "pk_test_xxx")
    object.__setattr__(cfg, "STRIPE_WEBHOOK_SECRET", "whsec_xxx")

    class _SpyClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url: str, json=None, headers=None):  # pragma: no cover
            self.calls.append({"method": "POST", "url": url, "json": json})

            class _R:
                status_code = 200
                text = "{}"

                def json(self):
                    return True

            return _R()

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        # Simulate a task previously settled; persist with credits_settled=True.
        task = _new_task(reserved=500, settled=True, spent_usd=0.30)
        await _insert_agent_task(pool, task)

        # Worker requeue path reloads the task.
        reloaded = await _load_agent_task(pool, task.id)
        assert reloaded is not None
        assert reloaded.credits_settled is True, (
            "round-trip must preserve credits_settled=True"
        )

        client = _SpyClient()
        with patch.object(api_mod, "_get_config", lambda: cfg), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            await _settle_agent_credits(reloaded)
    finally:
        await pool.close()

    assert client.calls == [], (
        "_settle_agent_credits must short-circuit when credits_settled is "
        "already True — no httpx call, no refund, no extra deduct"
    )
