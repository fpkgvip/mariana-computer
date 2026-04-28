"""S-03 regression: background reconciler retries stuck agent_settlements rows.

Bug
---
The R-01 fix shipped ``agent_settlements`` with a partial index on
``completed_at IS NULL`` for "operator reconciliation surface", but no
cron / asyncio task actually runs the reconciliation.  Combined with the
S-01 RPC payload bug, every settlement attempt produced a stuck row
with no automated rescue.

Fix
---
``mariana/agent/settlement_reconciler.py`` exports
``reconcile_pending_settlements(db, max_age_seconds=300, batch_size=50)``
which SELECTs uncompleted claims older than 5 minutes via
``FOR UPDATE SKIP LOCKED`` and re-invokes ``_settle_agent_credits`` for
each.  The daemon in ``mariana/main.py`` runs this every 60 seconds
alongside the agent-queue daemon.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import uuid
from typing import Any
from unittest.mock import patch

import pytest


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


def _new_task(*, reserved: int = 500, settled: bool = False, spent_usd: float = 0.30):
    from mariana.agent.models import AgentState, AgentTask  # noqa: PLC0415

    task = AgentTask(
        id=str(uuid.uuid4()),
        user_id=f"user-s03-{uuid.uuid4().hex[:8]}",
        goal="S-03 reconciler",
        budget_usd=5.0,
        spent_usd=spent_usd,
        state=AgentState.DONE,
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
        outer = self

        class _R:
            status_code = outer.status
            text = "{}"

            def json(self_inner):
                return True

        return _R()


async def _seed_stuck_settlement(
    pool: Any,
    *,
    task,
    age_seconds: int = 600,
    completed: bool = False,
):
    """Insert an agent_tasks + agent_settlements pair where the
    settlement was claimed ``age_seconds`` ago and remains uncompleted."""
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415

    await _insert_agent_task(pool, task)
    final_tokens = int(task.spent_usd * 100)
    delta = final_tokens - task.reserved_credits
    completed_at = "now()" if completed else "NULL"
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            INSERT INTO agent_settlements (
                task_id, user_id, reserved_credits, final_credits,
                delta_credits, ref_id, claimed_at, completed_at
            ) VALUES ($1, $2, $3, $4, $5, $6, now() - ($7 || ' seconds')::interval, {completed_at})
            """,
            task.id,
            task.user_id,
            task.reserved_credits,
            final_tokens,
            delta,
            f"agent_settle:{task.id}",
            str(age_seconds),
        )


# ---------------------------------------------------------------------------
# 1. Reconciler picks up an old uncompleted claim and stamps completed_at.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_s03_reconciler_picks_uncompleted():
    """Insert a stuck claim claimed 10 min ago.  Run reconciler.  Expect
    one RPC POST and completed_at populated."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=500, spent_usd=0.30)
        await _seed_stuck_settlement(pool, task=task, age_seconds=600)

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls, status=200)
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=50,
            )

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT completed_at FROM agent_settlements WHERE task_id = $1",
                task.id,
            )
    finally:
        await pool.close()

    assert len(rpc_calls) == 1, f"reconciler must issue exactly 1 RPC, got {len(rpc_calls)}"
    assert row is not None and row["completed_at"] is not None, (
        "reconciler must stamp completed_at after a successful retry"
    )


# ---------------------------------------------------------------------------
# 2. Already-completed rows are skipped.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_s03_reconciler_skips_completed():
    """A row with completed_at NOT NULL must be ignored — no RPC fires."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=500, spent_usd=0.30)
        await _seed_stuck_settlement(pool, task=task, age_seconds=600,
                                     completed=True)

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls, status=200)
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=50,
            )
    finally:
        await pool.close()

    assert len(rpc_calls) == 0, (
        f"reconciler must not touch completed rows; got {len(rpc_calls)} RPC POSTs"
    )


# ---------------------------------------------------------------------------
# 3. Reconciler swallows RPC failure and leaves row for next attempt.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_s03_reconciler_handles_rpc_failure_gracefully():
    """RPC returns 503.  Reconciler must not raise; row stays uncompleted."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=500, spent_usd=0.30)
        await _seed_stuck_settlement(pool, task=task, age_seconds=600)

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls, status=503)
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            # Must not raise even though every RPC fails.
            await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=50,
            )

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT completed_at FROM agent_settlements WHERE task_id = $1",
                task.id,
            )
    finally:
        await pool.close()

    assert len(rpc_calls) >= 1, "reconciler should have attempted the RPC at least once"
    assert row is not None and row["completed_at"] is None, (
        "row must remain uncompleted after RPC failure — next reconciler "
        "iteration retries"
    )


# ---------------------------------------------------------------------------
# 4. Concurrent reconciler runs are SKIP LOCKED-safe.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_s03_reconciler_idempotent_concurrent_runs():
    """Two concurrent reconciler invocations must between them issue
    exactly one RPC per stuck claim — SELECT ... FOR UPDATE SKIP LOCKED
    prevents both runs from racing on the same row."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        # Wipe any stragglers from prior tests so we observe only the
        # rows we're seeding here.  Settlements first (RESTRICT FK), then
        # tasks.
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM agent_settlements")
            await conn.execute("DELETE FROM agent_tasks")
        # Three stuck claims.
        tasks = [_new_task(reserved=500, spent_usd=0.30) for _ in range(3)]
        task_user_ids = {t.user_id for t in tasks}
        for t in tasks:
            await _seed_stuck_settlement(pool, task=t, age_seconds=600)

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls, status=200)
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            await asyncio.gather(
                recon_mod.reconcile_pending_settlements(
                    pool, max_age_seconds=300, batch_size=50,
                ),
                recon_mod.reconcile_pending_settlements(
                    pool, max_age_seconds=300, batch_size=50,
                ),
            )

        async with pool.acquire() as conn:
            completed = await conn.fetchval(
                "SELECT count(*) FROM agent_settlements "
                "WHERE task_id = ANY($1::uuid[]) AND completed_at IS NOT NULL",
                [t.id for t in tasks],
            )
    finally:
        await pool.close()

    # Each task was retried at most once across both concurrent runs.
    relevant_rpcs = [
        c for c in rpc_calls
        if c["json"].get("p_user_id") in task_user_ids
        or c["json"].get("target_user_id") in task_user_ids
    ]
    assert len(relevant_rpcs) == 3, (
        f"two concurrent reconcilers over 3 claims must yield exactly 3 RPCs "
        f"(one per claim); got {len(relevant_rpcs)} — SKIP LOCKED is not isolating"
    )
    assert completed == 3, f"all three claims must be completed, got {completed}"
