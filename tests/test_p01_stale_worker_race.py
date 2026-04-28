"""P-01 regression suite: stale-worker race double-refund.

Bug fixed
---------
Before this commit, the queue worker loaded a task with a plain SELECT (no
row lock, no version check) and handed it straight to ``run_agent_task``.
If the user hit Stop in the window between worker load and worker start,
the stop endpoint would lock+settle the row (``state=CANCELLED,
credits_settled=True``) — but the worker's stale ``AgentTask`` snapshot
would then UPSERT the row back to ``state=PLAN, credits_settled=False``
via ``_persist_task``'s unconditional ``ON CONFLICT DO UPDATE``. The worker
would then proceed, eventually hit the stop check, halt, and the
``finally:`` block would run ``_settle_agent_credits`` AGAIN — second
``add_credits`` RPC = double refund = minted credits = direct financial
loss.

Fix
---
1. ``_persist_task`` UPSERT gains a CAS-style WHERE clause that REJECTS
   any UPDATE which would un-finalize a row already
   ``credits_settled=TRUE`` AND in a terminal state. The function now
   returns ``bool`` indicating whether the UPDATE landed.
2. ``run_agent_task`` re-reads the row at function entry BEFORE any
   ``_persist_task`` and aborts cleanly if the row has already been
   finalized by the stop endpoint.
3. The terminal ``finally:`` block re-reads ``credits_settled`` from DB
   before calling ``_settle_agent_credits``, defending against double-
   refund even if the in-memory snapshot lies.

Tests
-----
1. test_p01_stale_persist_does_not_clobber_settled
2. test_p01_stale_persist_does_not_clobber_done
3. test_p01_run_agent_task_aborts_when_terminal_settled
4. test_p01_normal_persist_still_works
5. test_p01_full_race_simulation
6. test_p01_persist_task_normal_concurrent_overlap
"""

from __future__ import annotations

import json
import os
import pathlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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
        max_size=4,
    )


async def _ensure_schema(pool: Any) -> None:
    sql = _AGENT_SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)


def _new_task(
    *,
    reserved: int = 500,
    settled: bool = False,
    spent_usd: float = 0.0,
    state=None,
):
    from mariana.agent.models import AgentState, AgentTask  # noqa: PLC0415

    task = AgentTask(
        id=str(uuid.uuid4()),
        user_id=f"user-p01-{uuid.uuid4().hex[:8]}",
        goal="P-01 stale-worker race",
        budget_usd=5.0,
        spent_usd=spent_usd,
        state=state or AgentState.PLAN,
    )
    task.reserved_credits = reserved
    task.credits_settled = settled
    return task


# ---------------------------------------------------------------------------
# 1. _persist_task must NOT clobber a finalized (CANCELLED + settled) row
#    with a stale non-terminal snapshot.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_p01_stale_persist_does_not_clobber_settled():
    """Stale worker UPSERT must be silently rejected by the CAS WHERE guard."""
    from mariana.agent.api_routes import _insert_agent_task, _load_agent_task  # noqa: PLC0415
    from mariana.agent.loop import _persist_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        # Step 1: persist the *finalized* row to DB (simulates the stop
        # endpoint having locked + settled the task).
        finalized = _new_task(reserved=500, settled=True, spent_usd=0.0,
                              state=AgentState.CANCELLED)
        finalized.error = "stop_requested"
        await _insert_agent_task(pool, finalized)

        # Step 2: build a stale snapshot of the SAME task (worker had
        # loaded it BEFORE the stop endpoint ran).  Identity matches by
        # id; state is the pre-stop snapshot (PLAN, not settled).
        stale = _new_task(reserved=500, settled=False, spent_usd=0.0,
                          state=AgentState.PLAN)
        stale.id = finalized.id  # same row, stale view
        stale.user_id = finalized.user_id
        stale.goal = finalized.goal

        # Step 3: simulate stale worker calling _persist_task.
        await _persist_task(pool, stale)

        # Step 4: assert the DB row was NOT clobbered.
        reloaded = await _load_agent_task(pool, finalized.id)
    finally:
        await pool.close()

    assert reloaded is not None
    assert reloaded.state == AgentState.CANCELLED, (
        f"stale persist must NOT downgrade state from CANCELLED; "
        f"got {reloaded.state.value!r}"
    )
    assert reloaded.credits_settled is True, (
        "stale persist must NOT flip credits_settled back to False — "
        "this is the P-01 double-refund vector"
    )


# ---------------------------------------------------------------------------
# 2. Same as above but for state=DONE.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_p01_stale_persist_does_not_clobber_done():
    """Same CAS guard must protect a DONE+settled row from a stale PLAN snapshot."""
    from mariana.agent.api_routes import _insert_agent_task, _load_agent_task  # noqa: PLC0415
    from mariana.agent.loop import _persist_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        finalized = _new_task(reserved=500, settled=True, spent_usd=2.0,
                              state=AgentState.DONE)
        finalized.final_answer = "done"
        await _insert_agent_task(pool, finalized)

        stale = _new_task(reserved=500, settled=False, spent_usd=0.0,
                          state=AgentState.PLAN)
        stale.id = finalized.id
        stale.user_id = finalized.user_id
        stale.goal = finalized.goal

        await _persist_task(pool, stale)
        reloaded = await _load_agent_task(pool, finalized.id)
    finally:
        await pool.close()

    assert reloaded is not None
    assert reloaded.state == AgentState.DONE, (
        f"stale persist must NOT downgrade DONE to PLAN; got {reloaded.state.value!r}"
    )
    assert reloaded.credits_settled is True


# ---------------------------------------------------------------------------
# 3. run_agent_task must abort BEFORE any planner / persist work when the
#    DB row is already terminal+settled.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_p01_run_agent_task_aborts_when_terminal_settled():
    """run_agent_task with a stale snapshot must early-return without side effects."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import planner as planner_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        # DB row is CANCELLED + credits_settled=True (stop endpoint won).
        finalized = _new_task(reserved=500, settled=True, spent_usd=0.0,
                              state=AgentState.CANCELLED)
        finalized.error = "stop_requested"
        await _insert_agent_task(pool, finalized)

        # Worker still holds a stale PLAN snapshot.
        stale = _new_task(reserved=500, settled=False, spent_usd=0.0,
                          state=AgentState.PLAN)
        stale.id = finalized.id
        stale.user_id = finalized.user_id
        stale.goal = finalized.goal

        plan_mock = AsyncMock(return_value=([], 0.99))
        settle_mock = AsyncMock()
        record_mock = AsyncMock()

        with patch.object(planner_mod, "build_initial_plan", plan_mock), \
             patch.object(loop_mod, "_settle_agent_credits", settle_mock), \
             patch.object(loop_mod, "_record_event", record_mock):
            result = await loop_mod.run_agent_task(stale, db=pool, redis=None)
    finally:
        await pool.close()

    assert plan_mock.await_count == 0, (
        "planner.build_initial_plan must NOT be invoked when the canonical "
        "DB row is already terminal+settled"
    )
    assert settle_mock.await_count == 0, (
        "_settle_agent_credits must NOT be invoked a second time — the stop "
        "endpoint already settled this task"
    )
    # The in-memory task object should not have advanced past PLAN.
    assert result.state == AgentState.PLAN


# ---------------------------------------------------------------------------
# 4. Normal happy-path persist must still work after the CAS guard lands.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_p01_normal_persist_still_works():
    """Standard insert → mutate → persist round-trip is unaffected."""
    from mariana.agent.api_routes import _insert_agent_task, _load_agent_task  # noqa: PLC0415
    from mariana.agent.loop import _persist_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=500, settled=False)
        await _insert_agent_task(pool, task)

        # Legitimate runtime mutation: bump spent_usd, change state.
        task.spent_usd = 0.42
        task.state = AgentState.EXECUTE
        await _persist_task(pool, task)

        reloaded = await _load_agent_task(pool, task.id)
    finally:
        await pool.close()

    assert reloaded is not None
    assert reloaded.state == AgentState.EXECUTE
    assert abs(reloaded.spent_usd - 0.42) < 1e-9


# ---------------------------------------------------------------------------
# 5. Full P-01 race simulation: worker loads stale → stop endpoint settles
#    → worker calls run_agent_task → exactly ONE add_credits RPC ever fires.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_p01_full_race_simulation():
    """End-to-end race repro must yield exactly one refund RPC."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import planner as planner_mod  # noqa: PLC0415
    from mariana.agent.api_routes import (  # noqa: PLC0415
        _insert_agent_task,
        _load_agent_task,
    )
    from mariana.agent.models import AgentState  # noqa: PLC0415
    from mariana.config import AppConfig  # noqa: PLC0415

    cfg = AppConfig.__new__(AppConfig)
    object.__setattr__(cfg, "SUPABASE_URL", "https://supabase.test")
    object.__setattr__(cfg, "SUPABASE_KEY", "anon")
    object.__setattr__(cfg, "SUPABASE_SERVICE_ROLE_KEY", "service_role_xxx")
    object.__setattr__(cfg, "STRIPE_SECRET_KEY", "sk_test_xxx")
    object.__setattr__(cfg, "STRIPE_PUBLISHABLE_KEY", "pk_test_xxx")
    object.__setattr__(cfg, "STRIPE_WEBHOOK_SECRET", "whsec_xxx")

    rpc_calls: list[dict[str, Any]] = []

    class _ScriptedClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url, json=None, headers=None):
            rpc_calls.append({"url": url, "json": json})

            class _R:
                status_code = 200
                text = "{}"

                def json(self):
                    return True

            return _R()

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)

        # Step 1: POST /api/agent inserts task with reserved=500, settled=False.
        original = _new_task(reserved=500, settled=False, spent_usd=0.0,
                             state=AgentState.PLAN)
        await _insert_agent_task(pool, original)

        # Step 2: worker BLPOPs and loads task into memory (stale snapshot).
        stale_worker_view = await _load_agent_task(pool, original.id)
        assert stale_worker_view is not None
        assert stale_worker_view.state == AgentState.PLAN
        assert stale_worker_view.credits_settled is False

        # Step 3: stop endpoint runs in the meantime — lock + settle inline.
        # We replicate the exact pre-execution path from
        # mariana/agent/api_routes.py:stop_agent_task.
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT state, spent_usd, credits_settled, stop_requested "
                    "FROM agent_tasks WHERE id = $1 FOR UPDATE",
                    original.id,
                )
                assert row is not None
                await conn.execute(
                    "UPDATE agent_tasks SET stop_requested = TRUE, updated_at = now() "
                    "WHERE id = $1",
                    original.id,
                )
        terminal_task = await _load_agent_task(pool, original.id)
        assert terminal_task is not None
        terminal_task.state = AgentState.CANCELLED
        terminal_task.stop_requested = True
        terminal_task.error = "stop_requested"
        with patch.object(api_mod, "_get_config", lambda: cfg), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=_ScriptedClient()):
            await loop_mod._settle_agent_credits(terminal_task)
        # Persist the finalized row (this is what the stop endpoint does).
        await loop_mod._persist_task(pool, terminal_task)

        # Sanity: stop endpoint refund accounted for.
        assert len(rpc_calls) == 1, (
            f"setup: stop endpoint should have made 1 add_credits call; "
            f"got {len(rpc_calls)}"
        )
        assert "rpc/add_credits" in rpc_calls[0]["url"]

        # Step 4: stale worker now resumes and calls run_agent_task with its
        # pre-stop in-memory snapshot.  Without P-01 fix this would clobber
        # the row, eventually halt, and call _settle_agent_credits AGAIN.
        plan_mock = AsyncMock(return_value=([], 0.99))
        record_mock = AsyncMock()

        with patch.object(planner_mod, "build_initial_plan", plan_mock), \
             patch.object(loop_mod, "_record_event", record_mock), \
             patch.object(api_mod, "_get_config", lambda: cfg), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=_ScriptedClient()):
            result = await loop_mod.run_agent_task(stale_worker_view,
                                                   db=pool, redis=None)

        # Step 5: re-read DB; row must remain CANCELLED + settled.
        final_db = await _load_agent_task(pool, original.id)
    finally:
        await pool.close()

    assert final_db is not None
    assert final_db.state == AgentState.CANCELLED, (
        f"stale worker must not have downgraded the row from CANCELLED; "
        f"got {final_db.state.value!r}"
    )
    assert final_db.credits_settled is True

    # The critical invariant: only the stop endpoint's refund RPC happened.
    refund_calls = [c for c in rpc_calls if "rpc/add_credits" in c["url"]]
    assert len(refund_calls) == 1, (
        f"P-01: expected exactly ONE add_credits RPC (from stop endpoint); "
        f"got {len(refund_calls)} — stale worker double-refunded"
    )

    # Planner must not have run.
    assert plan_mock.await_count == 0


# ---------------------------------------------------------------------------
# 6. The CAS guard must NOT impede legitimate concurrent updates between
#    two non-terminal writers (e.g. two near-simultaneous spent_usd bumps).
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_p01_persist_task_normal_concurrent_overlap():
    """Two non-terminal writers must both succeed; the CAS guard only blocks
    un-finalize attempts, not normal writes."""
    from mariana.agent.api_routes import _insert_agent_task, _load_agent_task  # noqa: PLC0415
    from mariana.agent.loop import _persist_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=500, settled=False)
        await _insert_agent_task(pool, task)

        # Writer A: bumps spent_usd to 0.10.
        a = await _load_agent_task(pool, task.id)
        assert a is not None
        a.spent_usd = 0.10
        a.state = AgentState.EXECUTE

        # Writer B: bumps spent_usd to 0.20 (loaded fresh after A's load).
        b = await _load_agent_task(pool, task.id)
        assert b is not None
        b.spent_usd = 0.20
        b.state = AgentState.EXECUTE

        # Both writes happen back-to-back; both must land.
        await _persist_task(pool, a)
        await _persist_task(pool, b)

        reloaded = await _load_agent_task(pool, task.id)
    finally:
        await pool.close()

    assert reloaded is not None
    assert reloaded.state == AgentState.EXECUTE
    # Last writer wins — concurrent normal-path writes are NOT rejected.
    assert abs(reloaded.spent_usd - 0.20) < 1e-9, (
        f"second non-terminal writer must succeed; got spent_usd="
        f"{reloaded.spent_usd}"
    )
