"""O-02 regression suite: cancel-time settlement + early stop gate.

Bug fixed
---------
Before this commit:
- ``POST /api/agent/{task_id}/stop`` only set ``stop_requested=TRUE``;
  it never transitioned a queued/PLAN task to a terminal state, so the
  reservation stayed locked until some worker eventually picked it up.
- The stuck-task recovery filter in ``mariana/main.py`` requeued any
  non-terminal row regardless of ``stop_requested``, so a cancelled
  queued task was blindly requeued.
- ``run_agent_task`` did not check ``stop_requested`` before invoking
  ``planner.build_initial_plan``, so a recovered cancelled task still
  paid the planner cost.

Fix
---
- ``stop_agent_task`` SELECTs FOR UPDATE, decides pre-vs-post-execution,
  and for pre-execution (PLAN, no spend) transitions to
  ``AgentState.CANCELLED`` and settles credits inline.
- ``run_agent_task`` adds an early ``_check_stop_requested`` gate
  immediately after the initial ``_persist_task`` and BEFORE the planner.
- ``mariana/main.py`` recovery WHERE clause adds
  ``AND stop_requested = FALSE``.

Tests
-----
1. test_o02_stop_terminal_for_queued
2. test_o02_stop_running_still_signals_only
3. test_o02_recovery_skips_stop_requested
4. test_o02_run_agent_task_early_stop_check
"""

from __future__ import annotations

import json
import os
import pathlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.routing import APIRoute


# ---------------------------------------------------------------------------
# Local PG availability gate.
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
# Helpers.
# ---------------------------------------------------------------------------


async def _open_pool():
    import asyncpg as _asyncpg  # noqa: PLC0415

    return await _asyncpg.create_pool(
        host=PGHOST,
        port=PGPORT,
        user=PGUSER,
        database=PGDATABASE,
        min_size=1,
        max_size=2,
    )


async def _ensure_schema(pool: Any) -> None:
    sql = _AGENT_SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)


def _new_task(state, *, reserved: int = 500, spent: float = 0.0):
    from mariana.agent.models import AgentTask  # noqa: PLC0415

    task = AgentTask(
        id=str(uuid.uuid4()),
        user_id=f"user-o02-{uuid.uuid4().hex[:8]}",
        goal="o02 cancel settlement",
        budget_usd=5.0,
        spent_usd=spent,
        state=state,
    )
    task.reserved_credits = reserved
    task.credits_settled = False
    return task


class _RecordingHTTP:
    """Minimal httpx.AsyncClient stand-in that records calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url: str, json=None, headers=None):
        self.calls.append({"url": url, "json": json})

        class _R:
            status_code = 200
            text = "{}"

            def json(self):
                return True

        return _R()


def _cfg():
    from mariana.config import AppConfig  # noqa: PLC0415

    cfg = AppConfig.__new__(AppConfig)
    object.__setattr__(cfg, "SUPABASE_URL", "https://supabase.test")
    object.__setattr__(cfg, "SUPABASE_KEY", "anon")
    object.__setattr__(cfg, "SUPABASE_ANON_KEY", "anon_xxx")
    object.__setattr__(cfg, "SUPABASE_SERVICE_ROLE_KEY", "service_role_xxx")
    object.__setattr__(cfg, "STRIPE_SECRET_KEY", "sk_test_xxx")
    object.__setattr__(cfg, "STRIPE_PUBLISHABLE_KEY", "pk_test_xxx")
    object.__setattr__(cfg, "STRIPE_WEBHOOK_SECRET", "whsec_xxx")
    return cfg


async def _stop_endpoint(user_id: str):
    """Build the stop_agent_task closure with mocked auth/redis."""
    from mariana.agent.api_routes import make_routes  # noqa: PLC0415

    router = make_routes(
        get_current_user=AsyncMock(return_value={"user_id": user_id}),
        get_db=MagicMock(return_value=None),  # injected per test
        get_redis=MagicMock(return_value=None),
        get_stream_user=AsyncMock(return_value={"user_id": user_id}),
    )
    for route in router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == "/api/agent/{task_id}/stop"
            and route.methods == {"POST"}
        ):
            return route.endpoint, router
    raise AssertionError("could not locate stop endpoint")


# ---------------------------------------------------------------------------
# 1. Stop on a queued (PLAN, no spend) task → terminal CANCELLED + settled.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_o02_stop_terminal_for_queued():
    """POST /stop on a PLAN task (no spend) terminalises and refunds credits."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent.api_routes import (  # noqa: PLC0415
        _insert_agent_task,
        _load_agent_task,
        make_routes,
    )
    from mariana.agent.models import AgentState  # noqa: PLC0415
    from mariana.agent.state import is_terminal  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(AgentState.PLAN, reserved=500, spent=0.0)
        await _insert_agent_task(pool, task)

        # Build the stop endpoint with the live pool.
        router = make_routes(
            get_current_user=AsyncMock(return_value={"user_id": task.user_id}),
            get_db=MagicMock(return_value=pool),
            get_redis=MagicMock(return_value=None),
            get_stream_user=AsyncMock(return_value={"user_id": task.user_id}),
        )
        endpoint = next(
            r.endpoint
            for r in router.routes
            if isinstance(r, APIRoute)
            and r.path == "/api/agent/{task_id}/stop"
            and r.methods == {"POST"}
        )

        client = _RecordingHTTP()
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            resp = await endpoint(
                task_id=task.id,
                current_user={"user_id": task.user_id},
            )

        # Endpoint signals success and routes through the cancel-before-execution branch.
        assert resp.stopped is True
        assert "cancel" in resp.message.lower()

        # Reload from Postgres: row must be terminal CANCELLED and settled.
        reloaded = await _load_agent_task(pool, task.id)
        assert reloaded is not None
        assert reloaded.state == AgentState.CANCELLED
        assert is_terminal(reloaded.state)
        assert reloaded.stop_requested is True
        assert reloaded.credits_settled is True

        # And the settlement helper triggered an add_credits refund for the
        # full reservation (500 reserved, 0 spent → 500 credit refund).
        refund_calls = [c for c in client.calls if "rpc/add_credits" in c["url"]]
        assert len(refund_calls) == 1
        body = refund_calls[0]["json"]
        refund = body.get("p_credits") or body.get("credits") or body.get("amount")
        assert refund == 500
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# 2. Stop on a running (post-PLAN with spend) task: stop_requested only.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_o02_stop_running_still_signals_only():
    """A task already past PLAN with recorded spend keeps the legacy path:
    stop_requested=TRUE only; the worker terminalises + settles in finally.
    """
    from mariana.agent.api_routes import (  # noqa: PLC0415
        _insert_agent_task,
        _load_agent_task,
        make_routes,
    )
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        # EXECUTE state with non-zero spend models a task whose worker has
        # already begun running.  The stop endpoint must not finalise it.
        task = _new_task(AgentState.EXECUTE, reserved=500, spent=0.50)
        await _insert_agent_task(pool, task)

        router = make_routes(
            get_current_user=AsyncMock(return_value={"user_id": task.user_id}),
            get_db=MagicMock(return_value=pool),
            get_redis=MagicMock(return_value=None),
            get_stream_user=AsyncMock(return_value={"user_id": task.user_id}),
        )
        endpoint = next(
            r.endpoint
            for r in router.routes
            if isinstance(r, APIRoute)
            and r.path == "/api/agent/{task_id}/stop"
            and r.methods == {"POST"}
        )

        resp = await endpoint(
            task_id=task.id,
            current_user={"user_id": task.user_id},
        )
        assert resp.stopped is True
        # Message stays the legacy "stop requested" — no inline settlement.
        assert "cancel" not in resp.message.lower()

        reloaded = await _load_agent_task(pool, task.id)
        assert reloaded is not None
        # Task remains non-terminal; only stop_requested flipped.
        assert reloaded.state == AgentState.EXECUTE
        assert reloaded.stop_requested is True
        assert reloaded.credits_settled is False
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# 3. Recovery query excludes stop_requested rows.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_o02_recovery_skips_stop_requested():
    """The stuck-task recovery WHERE clause now skips rows with stop_requested=TRUE."""
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)

        legacy = _new_task(AgentState.PLAN, reserved=500, spent=0.0)
        normal = _new_task(AgentState.PLAN, reserved=500, spent=0.0)
        await _insert_agent_task(pool, legacy)
        await _insert_agent_task(pool, normal)

        # Mark the legacy row as stopped + push its updated_at backwards so
        # both rows match the "stale" condition.
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_tasks SET stop_requested = TRUE, "
                "updated_at = now() - INTERVAL '5 minutes' WHERE id = $1",
                legacy.id,
            )
            await conn.execute(
                "UPDATE agent_tasks SET updated_at = now() - INTERVAL '5 minutes' "
                "WHERE id = $1",
                normal.id,
            )

            # Mirror the EXACT recovery query from mariana/main.py (after the fix).
            rows = await conn.fetch(
                "SELECT id FROM agent_tasks "
                "WHERE state NOT IN ('done', 'failed', 'halted', 'cancelled', 'stopped') "
                "AND stop_requested = FALSE "
                "AND updated_at < NOW() - INTERVAL '60 seconds' "
                "ORDER BY created_at ASC LIMIT 500"
            )
        ids = {str(r["id"]) for r in rows}
        assert legacy.id not in ids, (
            "stop_requested=TRUE row must NOT be requeued by recovery"
        )
        assert normal.id in ids, (
            "normal stale non-terminal row must still be requeued"
        )
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# 4. run_agent_task fires _check_stop_requested BEFORE planner.build_initial_plan.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_o02_run_agent_task_early_stop_check():
    """A task arriving with stop_requested=TRUE must NOT call the planner."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import planner as planner_mod  # noqa: PLC0415
    from mariana.agent.models import AgentState, AgentTask  # noqa: PLC0415

    task = AgentTask(
        id=str(uuid.uuid4()),
        user_id="user-o02-early",
        goal="early stop",
        budget_usd=5.0,
        spent_usd=0.0,
        state=AgentState.PLAN,
    )
    task.reserved_credits = 500
    task.credits_settled = False
    task.stop_requested = True  # arrives stopped (legacy recovered row).

    plan_mock = AsyncMock(return_value=([], 0.99))
    persist_mock = AsyncMock()
    record_mock = AsyncMock()
    settle_mock = AsyncMock()

    # _persist_task is called multiple times; all should succeed silently.
    with patch.object(loop_mod, "_persist_task", persist_mock), \
         patch.object(loop_mod, "_record_event", record_mock), \
         patch.object(loop_mod, "_settle_agent_credits", settle_mock), \
         patch.object(planner_mod, "build_initial_plan", plan_mock):
        result = await loop_mod.run_agent_task(task, db=MagicMock(), redis=None)

    # Planner must NOT have been invoked.
    assert plan_mock.await_count == 0, (
        "planner.build_initial_plan called despite stop_requested=TRUE — "
        "early stop gate is missing"
    )
    # And spend stayed at zero.
    assert result.spent_usd == 0.0
    # The task ended up in a terminal state (HALTED).
    assert result.state == AgentState.HALTED
    # Settlement ran from the finally block.
    assert settle_mock.await_count >= 1
