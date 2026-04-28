"""R-01 regression suite: DB-atomic settlement claim defeats finally fail-open.

Bug
---
The Q-01 finally block re-reads ``credits_settled`` from DB to decide whether
to skip ``_settle_agent_credits``.  If that read raises (transient pool
error, connection blip, mock failure in tests), the except clause logs and
falls through with ``already_settled_in_db=False``, leading to a SECOND
``_settle_agent_credits`` call that issues a duplicate ``add_credits`` RPC.
Q-01 CAS blocks the trailing ``_persist_task`` so the canonical row stays
clean while the ledger has been double-credited.

Fix
---
Settlement idempotency moves to a backend Postgres claim row in a new
``agent_settlements`` table.  ``_settle_agent_credits`` performs:

    INSERT INTO agent_settlements (...) VALUES (...)
    ON CONFLICT (task_id) DO NOTHING
    RETURNING task_id

If RETURNING is empty, another caller has already claimed settlement and
this call short-circuits before any ledger RPC.  Otherwise the caller has
won the claim and proceeds to issue the RPC, then marks completed_at on
success.  This eliminates the race even when process-local state (the
in-memory ``credits_settled`` flag) is wrong or the Q-01 fetchrow guard
fails.

Tests below construct the original repro in-process and assert exactly one
RPC fires under each adverse interleave.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

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
        user_id=f"user-r01-{uuid.uuid4().hex[:8]}",
        goal="R-01 settlement idempotency",
        budget_usd=5.0,
        spent_usd=spent_usd,
        state=state or AgentState.PLAN,
    )
    task.reserved_credits = reserved
    task.credits_settled = settled
    return task


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
    """httpx.AsyncClient stand-in that records every POST."""

    def __init__(self, calls: list[dict[str, Any]] | None = None,
                 status: int = 200):
        self.calls = calls if calls is not None else []
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json})

        class _R:
            status_code = self.status
            text = "{}"

            def json(self_inner):
                return True

        return _R()


# ---------------------------------------------------------------------------
# 1. Two concurrent _settle_agent_credits calls — exactly ONE add_credits RPC.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_r01_concurrent_settle_only_one_wins():
    """asyncio.gather two settle calls on same task → exactly 1 RPC."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        # Two AgentTask objects pointing at the same DB row, both with
        # in-memory credits_settled=False (worst case).
        base = _new_task(reserved=500, settled=False, spent_usd=0.0,
                         state=AgentState.CANCELLED)
        await _insert_agent_task(pool, base)

        from mariana.agent.models import AgentTask  # noqa: PLC0415
        # Two snapshots of the same task — same id but separate objects so
        # the in-memory ``credits_settled`` flag cannot link them.
        a = AgentTask(**base.model_dump())
        b = AgentTask(**base.model_dump())

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls)

        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            await asyncio.gather(
                loop_mod._settle_agent_credits(a, db=pool),
                loop_mod._settle_agent_credits(b, db=pool),
            )

        refund_calls = [c for c in rpc_calls if "rpc/grant_credits" in c["url"]]
    finally:
        await pool.close()

    assert len(refund_calls) == 1, (
        f"R-01: concurrent settle must yield exactly ONE add_credits RPC; "
        f"got {len(refund_calls)} — settlement claim is not atomic"
    )


# ---------------------------------------------------------------------------
# 2. Finally fetch failure must not produce a second RPC.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_r01_finally_fetch_failure_does_not_double_refund():
    """Replicates repro_r01_finally_fetch_failure.py:

    Stop endpoint settles 500 → worker finally fetchrow raises → worker
    falls through to _settle_agent_credits.  With the DB-atomic claim,
    the second settle observes the existing claim row and short-circuits.
    Net: exactly ONE add_credits RPC."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent.api_routes import (  # noqa: PLC0415
        _insert_agent_task,
        _load_agent_task,
    )
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)

        # Step 1: task starts with reserved=500, settled=False.
        original = _new_task(reserved=500, settled=False, spent_usd=0.0,
                             state=AgentState.PLAN)
        await _insert_agent_task(pool, original)

        # Step 2: stop endpoint runs → settle(500), persist CANCELLED+settled.
        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls)
        terminal_task = await _load_agent_task(pool, original.id)
        assert terminal_task is not None
        terminal_task.state = AgentState.CANCELLED
        terminal_task.stop_requested = True
        terminal_task.error = "stop_requested"
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            await loop_mod._settle_agent_credits(terminal_task, db=pool)
        await loop_mod._persist_task(pool, terminal_task)

        # Setup sanity: stop endpoint has done its single refund.
        first_round = [c for c in rpc_calls if "rpc/grant_credits" in c["url"]]
        assert len(first_round) == 1

        # Step 3: stale worker snapshot — pretends finally guard fetch
        # failed (in-memory ``credits_settled`` still False, ``spent_usd``
        # at the moment of stale load was $0.80 → 80 final tokens).
        stale = _new_task(reserved=500, settled=False, spent_usd=0.80,
                          state=AgentState.HALTED)
        stale.id = original.id
        stale.user_id = original.user_id
        stale.goal = original.goal

        # Stale worker now calls _settle_agent_credits — without R-01 fix
        # this would issue a SECOND add_credits RPC.  With the fix, the
        # agent_settlements ON CONFLICT DO NOTHING returns 0 rows and the
        # call short-circuits.
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            await loop_mod._settle_agent_credits(stale, db=pool)

        all_refunds = [c for c in rpc_calls if "rpc/grant_credits" in c["url"]]
    finally:
        await pool.close()

    assert len(all_refunds) == 1, (
        f"R-01: finally fail-open must NOT double-refund — expected exactly "
        f"ONE add_credits RPC across stop endpoint + worker finally; got "
        f"{len(all_refunds)}"
    )


# ---------------------------------------------------------------------------
# 3. agent_settlements row records outcome on success and on RPC failure.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_r01_settlement_table_records_outcome():
    """After successful settle, the claim row has completed_at NOT NULL,
    delta_credits, ref_id.  After RPC failure, completed_at IS NULL but the
    row still locks the claim.

    S-01 contract update (2026-04-28): on RPC failure, ``credits_settled``
    now stays False so the S-03 reconciler can retry the same code path.
    The R-01-original assertion that ``credits_settled is True`` after a
    500 was the root cause of S-01: it permanently stranded uncompleted
    claims by short-circuiting every retry on the in-memory flag.
    Updated to assert ``credits_settled is False`` per the S-01 fix
    (loop.py: only set the flag together with the completed_at stamp)."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)

        # Case A: successful RPC → completed_at populated.
        ok_task = _new_task(reserved=500, settled=False, spent_usd=0.30,
                            state=AgentState.DONE)
        await _insert_agent_task(pool, ok_task)
        client_ok = _ScriptedClient(status=200)
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client_ok):
            await loop_mod._settle_agent_credits(ok_task, db=pool)

        async with pool.acquire() as conn:
            row_ok = await conn.fetchrow(
                "SELECT task_id, reserved_credits, final_credits, "
                "delta_credits, ref_id, completed_at "
                "FROM agent_settlements WHERE task_id = $1",
                ok_task.id,
            )
        assert row_ok is not None, "claim row must be inserted on first settle"
        assert row_ok["completed_at"] is not None, (
            "completed_at must be set after the RPC succeeded"
        )
        assert row_ok["reserved_credits"] == 500
        assert row_ok["final_credits"] == 30  # int($0.30 * 100) = 30
        assert row_ok["delta_credits"] == -470  # spent < reserved → refund
        assert row_ok["ref_id"] == f"agent_settle:{ok_task.id}"

        # Case B: RPC fails → claim row exists, completed_at IS NULL.
        fail_task = _new_task(reserved=500, settled=False, spent_usd=0.30,
                              state=AgentState.DONE)
        await _insert_agent_task(pool, fail_task)
        client_fail = _ScriptedClient(status=500)
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client_fail):
            await loop_mod._settle_agent_credits(fail_task, db=pool)

        async with pool.acquire() as conn:
            row_fail = await conn.fetchrow(
                "SELECT completed_at FROM agent_settlements WHERE task_id = $1",
                fail_task.id,
            )
        assert row_fail is not None, (
            "claim row must persist even when RPC fails — operator "
            "reconciliation depends on it"
        )
        assert row_fail["completed_at"] is None, (
            "completed_at must remain NULL when the RPC did not succeed"
        )
        # S-01 fix: on RPC failure, credits_settled MUST stay False so the
        # reconciler (S-03) can retry the settlement.  The DB-level claim
        # row + ref_id is the canonical idempotency anchor; the in-memory
        # flag is a co-witness that only flips together with completed_at.
        assert fail_task.credits_settled is False
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# 4. Calling _settle_agent_credits twice → second call short-circuits.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_r01_settle_idempotent_after_completion():
    """Two sequential settle calls on the same task → exactly ONE RPC."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=500, settled=False, spent_usd=0.30,
                         state=AgentState.DONE)
        await _insert_agent_task(pool, task)
        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls)
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            await loop_mod._settle_agent_credits(task, db=pool)
            # Force-pretend the in-memory flag is still False so the
            # function cannot short-circuit on it alone.
            task.credits_settled = False
            await loop_mod._settle_agent_credits(task, db=pool)
        refunds = [c for c in rpc_calls if "rpc/grant_credits" in c["url"]]
    finally:
        await pool.close()

    assert len(refunds) == 1, (
        f"second settle call must short-circuit on the existing claim row; "
        f"got {len(refunds)} RPC calls"
    )


# ---------------------------------------------------------------------------
# 5. Full repro: stop settles + worker finally fetchrow raises + worker
#    settles → exactly ONE add_credits RPC, final DB row clean.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_r01_full_race_repro():
    """End-to-end run_agent_task race with finally fetch failure."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import planner as planner_mod  # noqa: PLC0415
    from mariana.agent.api_routes import (  # noqa: PLC0415
        _insert_agent_task,
        _load_agent_task,
    )
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)

        # POST /api/agent: row inserted reserved=500, settled=False.
        original = _new_task(reserved=500, settled=False, spent_usd=0.0,
                             state=AgentState.PLAN)
        await _insert_agent_task(pool, original)

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls)

        # Worker BLPOPs and loads stale snapshot.
        stale_worker_view = await _load_agent_task(pool, original.id)
        assert stale_worker_view is not None

        # Stop endpoint races in: settle 500 + persist CANCELLED row.
        terminal_task = await _load_agent_task(pool, original.id)
        assert terminal_task is not None
        terminal_task.state = AgentState.CANCELLED
        terminal_task.stop_requested = True
        terminal_task.error = "stop_requested"
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            await loop_mod._settle_agent_credits(terminal_task, db=pool)
        await loop_mod._persist_task(pool, terminal_task)
        assert len([c for c in rpc_calls if "rpc/grant_credits" in c["url"]]) == 1

        # Now the worker resumes.  We patch its run_agent_task path so the
        # finally-block fetchrow raises (simulating a transient pool
        # error).  We patch loop_mod._FETCH_FINAL_ROW_FAIL = True via a
        # wrapper around db.acquire; simpler: monkeypatch db.acquire to
        # raise from the finally fetchrow path.  Since the finally re-read
        # has been simplified post-fix, we instead simulate the
        # repro_r01_finally_fetch_failure scenario by:
        #   1. mutating stale snapshot to simulate the worker's accumulated
        #      in-memory state at the moment of finally;
        #   2. directly calling _settle_agent_credits — which is what the
        #      fail-open finally would have done before the fix.
        stale_worker_view.spent_usd = 0.80
        stale_worker_view.state = AgentState.HALTED
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            await loop_mod._settle_agent_credits(stale_worker_view, db=pool)

        all_refunds = [c for c in rpc_calls if "rpc/grant_credits" in c["url"]]
        final_row = await _load_agent_task(pool, original.id)
    finally:
        await pool.close()

    assert len(all_refunds) == 1, (
        f"R-01 full repro: expected exactly ONE add_credits RPC; got "
        f"{len(all_refunds)} — double-refund was minted"
    )
    assert final_row is not None
    assert final_row.state == AgentState.CANCELLED
    assert final_row.credits_settled is True
    # (spent_usd may be 0.0 from the stop-endpoint settled value — the
    # critical invariant is that no second RPC fired.)


# ---------------------------------------------------------------------------
# 6. In-memory credits_settled flag is no longer authoritative — a stale
#    False flag must NOT cause a duplicate RPC if the claim row exists.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_r01_in_memory_credits_settled_no_longer_authoritative():
    """credits_settled=False on a fresh AgentTask object pointing at a
    task that already has a completed claim row → settle short-circuits."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=500, settled=False, spent_usd=0.10,
                         state=AgentState.DONE)
        await _insert_agent_task(pool, task)

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls)
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            # First call wins the claim.
            await loop_mod._settle_agent_credits(task, db=pool)

            # Build an entirely fresh AgentTask pointing at the same DB
            # row.  Its in-memory flag is False (lying to us, but we don't
            # trust it any more).
            fresh = _new_task(reserved=500, settled=False, spent_usd=0.10,
                              state=AgentState.DONE)
            fresh.id = task.id
            fresh.user_id = task.user_id
            fresh.goal = task.goal

            await loop_mod._settle_agent_credits(fresh, db=pool)

        refunds = [c for c in rpc_calls if "rpc/grant_credits" in c["url"]]
    finally:
        await pool.close()

    assert len(refunds) == 1, (
        f"In-memory credits_settled=False must NOT bypass the DB claim row; "
        f"got {len(refunds)} RPC calls — claim-row idempotency leaked"
    )
