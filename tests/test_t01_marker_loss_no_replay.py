"""T-01 regression: settlement marker-loss must not cause reconciler replay.

Bug
---
After S-01..S-04, ``agent_settlements.completed_at`` is the SOLE once-only
fence for the non-idempotent low-level RPCs ``add_credits(p_user_id,
p_credits)`` and ``deduct_credits(target_user_id, amount)``.  In
``mariana/agent/loop.py:_settle_agent_credits``::

    if rpc_succeeded:
        task.credits_settled = True
        if db is not None:
            try:
                await _mark_settlement_completed(db, task.id)
            except Exception as exc:
                logger.warning("agent_settlement_mark_completed_failed", ...)

If the ledger RPC returns 200 but the marker UPDATE then fails (transient
DB hiccup, statement timeout, pool reset), the worker's in-memory
``credits_settled`` is True yet ``agent_settlements.completed_at`` stays
NULL.  The S-03 reconciler later forces ``credits_settled = False`` and
re-invokes ``_settle_agent_credits`` for any uncompleted claim older
than 5 minutes — driving a SECOND real ledger mutation against
``add_credits`` / ``deduct_credits``.  Net: refund-twice (delta<0) or
charge-twice (delta>0).

This test:
  1. Stands up the local Postgres ``agent_settlements`` table (from
     ``mariana/agent/schema.sql``).
  2. Patches ``_mark_settlement_completed`` to raise once on first call.
  3. Patches httpx so RPCs return 200 and counts POSTs.
  4. Calls ``_settle_agent_credits(task, db=db)`` for a refund task.
     Expects EXACTLY 1 RPC POST so far.  ``task.credits_settled`` must
     be False (durable marker write failed → not safe to claim settled).
  5. Ages the claim's ``claimed_at`` past the reconciler threshold.
  6. Calls ``reconcile_pending_settlements(db, max_age_seconds=300)``.
  7. Asserts STILL exactly 1 RPC POST total — no replay.
  8. Asserts the row is in a clean terminal state — either
     ``completed_at IS NOT NULL`` or ``ledger_applied_at IS NOT NULL``.

The fix routes settlement through idempotent ledger primitives
(``grant_credits`` / ``refund_credits``) AND adds an explicit
``agent_settlements.ledger_applied_at`` column so the reconciler can
distinguish "ledger mutation already applied, only marker bookkeeping is
stale" from "RPC genuinely failed, must retry".
"""

from __future__ import annotations

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


def _new_task(*, reserved: int = 500, spent_usd: float = 0.30):
    from mariana.agent.models import AgentState, AgentTask  # noqa: PLC0415

    task = AgentTask(
        id=str(uuid.uuid4()),
        user_id=f"user-t01-{uuid.uuid4().hex[:8]}",
        goal="T-01 marker loss",
        budget_usd=5.0,
        spent_usd=spent_usd,
        state=AgentState.DONE,
    )
    task.reserved_credits = reserved
    task.credits_settled = False
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
    """Minimal httpx.AsyncClient stand-in.  Records every RPC POST and
    returns ``status`` for every call.  Returns ``status_value`` for
    the JSON body so grant_credits / refund_credits look successful."""

    def __init__(self, calls: list[dict[str, Any]] | None = None,
                 status: int = 200,
                 status_value: str = "granted"):
        self.calls = calls if calls is not None else []
        self.status = status
        self.status_value = status_value

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
                # Both the legacy add_credits/deduct_credits and the
                # idempotent grant_credits/refund_credits accept a
                # truthy/dict body — return a flexible shape.
                return {
                    "status": outer.status_value,
                    "balance_after": 1000,
                }

        return _R()


# ---------------------------------------------------------------------------
# T-01: marker-loss must not cause a second ledger RPC.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_t01_marker_loss_no_replay_refund():
    """RPC succeeds; ``_mark_settlement_completed`` raises ONCE; reconciler
    must NOT issue a second RPC for the same task."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        # Wipe stragglers so the assertion on RPC count is unambiguous.
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM agent_settlements")
            await conn.execute("DELETE FROM agent_tasks")

        task = _new_task(reserved=500, spent_usd=0.30)
        # Refund path: delta = 30 - 500 = -470.
        await _insert_agent_task(pool, task)

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls, status=200,
                                 status_value="granted")

        # Patch _mark_settlement_completed to raise on the first call,
        # then succeed (delegate to the real implementation) afterwards.
        real_mark = loop_mod._mark_settlement_completed
        call_state = {"n": 0}

        async def flaky_mark(db, task_id):
            call_state["n"] += 1
            if call_state["n"] == 1:
                raise RuntimeError("transient: marker write failed after RPC")
            return await real_mark(db, task_id)

        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client), \
             patch.object(loop_mod, "_mark_settlement_completed", flaky_mark):

            # First settle attempt — RPC goes through, marker write fails.
            await loop_mod._settle_agent_credits(task, db=pool)

            # Exactly one ledger RPC POST so far.
            assert len(rpc_calls) == 1, (
                f"first settle must issue exactly 1 ledger RPC, got "
                f"{len(rpc_calls)}: {rpc_calls}"
            )
            # Because the durable marker write failed, the in-memory
            # flag must NOT report the task as settled yet — otherwise
            # a same-process retry would skip.  (Under the fix, the
            # durable ``ledger_applied_at`` is what authorises any
            # short-circuit on subsequent calls.)
            assert task.credits_settled is False, (
                "with the durable marker write failing, "
                "task.credits_settled must remain False so future "
                "callers consult the DB row instead of trusting "
                "stale in-memory state"
            )

            # Age the claim past the reconciler threshold.
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_settlements "
                    "SET claimed_at = now() - interval '10 minutes' "
                    "WHERE task_id = $1",
                    task.id,
                )

            # Reconciler runs — must NOT replay the ledger RPC.
            await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=50,
            )

        # *** The critical assertion: still exactly one RPC POST total. ***
        assert len(rpc_calls) == 1, (
            f"reconciler must NOT replay a successful ledger RPC after "
            f"a transient marker-write failure; got {len(rpc_calls)} "
            f"RPC POSTs total: {rpc_calls}"
        )

        # The row must end in a clean terminal state.  Under the fix the
        # reconciler stamps completed_at (and the original RPC stamped
        # ledger_applied_at).  We accept either column being non-NULL
        # so this test does not over-couple to the chosen column.
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT completed_at, "
                "       (CASE WHEN EXISTS ("
                "           SELECT 1 FROM information_schema.columns "
                "            WHERE table_name='agent_settlements' "
                "              AND column_name='ledger_applied_at'"
                "       ) THEN (SELECT ledger_applied_at "
                "                 FROM agent_settlements "
                "                WHERE task_id = $1) "
                "         ELSE NULL END) AS ledger_applied_at "
                "FROM agent_settlements WHERE task_id = $1",
                task.id,
            )
        assert row is not None, "claim row must still exist"
        terminal = (row["completed_at"] is not None
                    or row["ledger_applied_at"] is not None)
        assert terminal, (
            "after reconciler the row must be in a terminal state "
            "(completed_at IS NOT NULL or ledger_applied_at IS NOT NULL); "
            f"got completed_at={row['completed_at']}, "
            f"ledger_applied_at={row['ledger_applied_at']}"
        )
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# T-01: same property for the overrun (delta > 0) path.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_t01_marker_loss_no_replay_overrun():
    """Mirror of the refund test, but for delta > 0 — the deduct/spend
    path must equally not double-charge after a marker-write failure."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM agent_settlements")
            await conn.execute("DELETE FROM agent_tasks")

        # Overrun: reserved 500, spent_usd 6.00 → final_tokens 600 →
        # delta = +100 → user must lose 100 more credits.
        task = _new_task(reserved=500, spent_usd=6.00)
        await _insert_agent_task(pool, task)

        rpc_calls: list[dict[str, Any]] = []
        # status_value 'reversed' matches refund_credits success shape;
        # legacy deduct_credits is also content with any 200 body.
        client = _ScriptedClient(calls=rpc_calls, status=200,
                                 status_value="reversed")

        real_mark = loop_mod._mark_settlement_completed
        call_state = {"n": 0}

        async def flaky_mark(db, task_id):
            call_state["n"] += 1
            if call_state["n"] == 1:
                raise RuntimeError("transient: marker write failed after RPC")
            return await real_mark(db, task_id)

        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client), \
             patch.object(loop_mod, "_mark_settlement_completed", flaky_mark):

            await loop_mod._settle_agent_credits(task, db=pool)
            assert len(rpc_calls) == 1, (
                f"first settle must issue exactly 1 ledger RPC, got "
                f"{len(rpc_calls)}: {rpc_calls}"
            )
            assert task.credits_settled is False

            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_settlements "
                    "SET claimed_at = now() - interval '10 minutes' "
                    "WHERE task_id = $1",
                    task.id,
                )

            await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=50,
            )

        assert len(rpc_calls) == 1, (
            f"reconciler must NOT replay a successful overrun RPC; "
            f"got {len(rpc_calls)} POSTs total: {rpc_calls}"
        )

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT completed_at, "
                "       (CASE WHEN EXISTS ("
                "           SELECT 1 FROM information_schema.columns "
                "            WHERE table_name='agent_settlements' "
                "              AND column_name='ledger_applied_at'"
                "       ) THEN (SELECT ledger_applied_at "
                "                 FROM agent_settlements "
                "                WHERE task_id = $1) "
                "         ELSE NULL END) AS ledger_applied_at "
                "FROM agent_settlements WHERE task_id = $1",
                task.id,
            )
        assert row is not None
        terminal = (row["completed_at"] is not None
                    or row["ledger_applied_at"] is not None)
        assert terminal, (
            "overrun row must end in a terminal state after reconciler"
        )
    finally:
        await pool.close()
