"""Y-01 regression: legacy investigation settlement must be idempotent.

Bug
---
Phase E re-audit #26 (A31) found that ``mariana/main.py:_deduct_user_credits``
calls the non-idempotent low-level RPCs ``add_credits(p_user_id, p_credits)``
and ``deduct_credits(target_user_id, amount)`` directly with no claim row,
no ``(ref_type, ref_id)`` keying, and no ``research_tasks.credits_settled``
flag.  T-01 fixed exactly this defect class for the AGENT settlement path
(``mariana/agent/loop.py:_settle_agent_credits``) by routing through
idempotent ``grant_credits`` / ``refund_credits`` and adding
``agent_settlements.ledger_applied_at``; the symmetric LEGACY INVESTIGATION
path was overlooked.

Reproducer:
  1. Investigation runs to completion and ``_deduct_user_credits`` issues
     the ledger RPC successfully.
  2. The daemon process is SIGKILL'd (OOM, k8s pod replacement) BEFORE the
     ``.running``->``.done`` rename at ``main.py:738``.
  3. On restart, the resume path at ``main.py:944-1024`` re-invokes
     ``_run_single_guarded`` for the ``.running`` file.
  4. The orchestrator restores ``cost_tracker.total_spent`` from
     ``ai_sessions`` and short-circuits because ``current_state == HALT``.
  5. ``_deduct_user_credits`` is called AGAIN with the SAME reserved /
     final amounts — applying the same delta a SECOND time.

Net financial impact: refund-twice (delta<0) under-bills the user;
extra-deduct-twice (delta>0) over-bills the user.

This test pins the fix:
  * ``research_settlements`` claim row + ``ledger_applied_at`` mirror T-01.
  * ``_deduct_user_credits`` is now keyed on ``task_id`` and routes
    through idempotent ``grant_credits`` / ``refund_credits``.
  * ``mariana/research_settlement_reconciler.py`` retries any uncompleted
    claim older than 5 minutes, short-circuiting any
    ``ledger_applied_at IS NOT NULL`` row to a marker fix-up.
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
    from mariana.data.db import init_schema  # noqa: PLC0415

    await init_schema(pool)


async def _insert_research_task_row(pool: Any, task_id: str) -> None:
    """Minimal research_tasks row so the FK from research_settlements holds.

    user_id stays NULL so the test does not depend on the auth.users table.
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO research_tasks (
                id, topic, budget_usd, status, current_state,
                total_spent_usd, diminishing_flags, ai_call_counter,
                created_at, metadata
            ) VALUES (
                $1, $2, $3, 'COMPLETED', 'HALT',
                $4, 0, 0,
                $5, '{}'::jsonb
            )
            ON CONFLICT (id) DO NOTHING
            """,
            task_id,
            "y01-test",
            5.0,
            0.0,
            datetime.now(tz=timezone.utc),
        )


class _FakeCostTracker:
    """Drop-in stand-in for ``mariana.orchestrator.cost_tracker.CostTracker``.

    Only ``total_spent`` and ``total_with_markup`` are read by
    ``_deduct_user_credits`` so this minimal shape suffices.
    """

    def __init__(self, total_with_markup: float) -> None:
        self.total_spent = total_with_markup / 1.20
        self._markup = total_with_markup

    @property
    def total_with_markup(self) -> float:
        return self._markup


def _cfg():
    from mariana.config import AppConfig  # noqa: PLC0415

    cfg = AppConfig.__new__(AppConfig)
    object.__setattr__(cfg, "SUPABASE_URL", "https://supabase.test")
    object.__setattr__(cfg, "SUPABASE_KEY", "anon")
    object.__setattr__(cfg, "SUPABASE_SERVICE_KEY", "service_role_xxx")
    object.__setattr__(cfg, "SUPABASE_ANON_KEY", "anon_xxx")
    object.__setattr__(cfg, "SUPABASE_SERVICE_ROLE_KEY", "service_role_xxx")
    object.__setattr__(cfg, "STRIPE_SECRET_KEY", "sk_test_xxx")
    object.__setattr__(cfg, "STRIPE_PUBLISHABLE_KEY", "pk_test_xxx")
    object.__setattr__(cfg, "STRIPE_WEBHOOK_SECRET", "whsec_xxx")
    return cfg


class _ScriptedClient:
    """Minimal httpx.AsyncClient stand-in mirroring the T-01 test pattern."""

    def __init__(self, calls: list[dict[str, Any]] | None = None,
                 status: int = 200,
                 status_value: str = "granted") -> None:
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
                return {
                    "status": outer.status_value,
                    "balance_after": 1000,
                }

        return _R()


# ---------------------------------------------------------------------------
# (1) First settle issues exactly one ledger RPC and stamps the durable
#     ``ledger_applied_at`` + ``completed_at`` markers.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_y01_first_settle_keys_on_task_id():
    """Refund path issues a single keyed ``grant_credits`` RPC and the
    claim row ends with both ``ledger_applied_at`` and ``completed_at``
    stamped, ``research_tasks.credits_settled = TRUE``."""
    import httpx  # noqa: PLC0415

    from mariana import main as main_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM research_settlements")

        task_id = str(uuid.uuid4())
        await _insert_research_task_row(pool, task_id)

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls, status=200,
                                 status_value="granted")
        cfg = _cfg()
        cost_tracker = _FakeCostTracker(total_with_markup=0.30)
        # final_tokens = usd_to_credits(0.30) = 30; reserved = 500 → delta -470
        user_id = "user-y01-" + uuid.uuid4().hex[:8]

        with patch.object(httpx, "AsyncClient", return_value=client):
            await main_mod._deduct_user_credits(
                user_id,
                cost_tracker,
                cfg,
                reserved_credits=500,
                task_id=task_id,
                db=pool,
            )

        # Exactly one keyed RPC POST.
        assert len(rpc_calls) == 1, f"expected 1 keyed RPC, got {rpc_calls!r}"
        body = rpc_calls[0]["json"]
        assert body.get("p_ref_type") in {"research_task", "research_task_overrun"}
        assert body.get("p_ref_id") == task_id
        # delta = 30 - 500 = -470 → refund path → grant_credits
        assert "grant_credits" in rpc_calls[0]["url"]
        assert body.get("p_source") == "refund"
        assert body.get("p_credits") == 470

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT ledger_applied_at, completed_at, delta_credits "
                "FROM research_settlements WHERE task_id = $1",
                task_id,
            )
            assert row is not None, "claim row must exist after settle"
            assert row["ledger_applied_at"] is not None, (
                "ledger_applied_at must be stamped after RPC 2xx"
            )
            assert row["completed_at"] is not None, (
                "completed_at must be stamped after marker write"
            )

            tcs = await conn.fetchval(
                "SELECT credits_settled FROM research_tasks WHERE id = $1",
                task_id,
            )
            assert tcs is True, "credits_settled must flip True only after marker write"
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# (2) A second call with the same task_id MUST NOT issue a second RPC.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_y01_second_settle_same_task_no_replay():
    """Re-invoking ``_deduct_user_credits`` for the same task_id is a
    no-op — the already-completed claim row short-circuits before any RPC."""
    import httpx  # noqa: PLC0415

    from mariana import main as main_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM research_settlements")

        task_id = str(uuid.uuid4())
        await _insert_research_task_row(pool, task_id)

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls, status=200,
                                 status_value="granted")
        cfg = _cfg()
        cost_tracker = _FakeCostTracker(total_with_markup=0.30)
        user_id = "user-y01-" + uuid.uuid4().hex[:8]

        with patch.object(httpx, "AsyncClient", return_value=client):
            await main_mod._deduct_user_credits(
                user_id, cost_tracker, cfg,
                reserved_credits=500, task_id=task_id, db=pool,
            )
            assert len(rpc_calls) == 1

            # Second call with same task_id — must not issue another RPC.
            await main_mod._deduct_user_credits(
                user_id, cost_tracker, cfg,
                reserved_credits=500, task_id=task_id, db=pool,
            )

        assert len(rpc_calls) == 1, (
            f"second settle for the same task_id must short-circuit; "
            f"got {len(rpc_calls)} RPCs total: {rpc_calls!r}"
        )
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# (3) RPC succeeds but ``_mark_settlement_completed`` fails: the reconciler
#     must NOT replay the ledger RPC — only stamp the marker.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_y01_marker_loss_no_replay():
    """Marker-write failure between RPC 2xx and ``completed_at`` stamp
    must leave the row reconciler-eligible.  The reconciler short-cuts
    via ``ledger_applied_at IS NOT NULL`` and stamps ``completed_at``
    WITHOUT re-issuing the ledger RPC."""
    import httpx  # noqa: PLC0415

    from mariana import main as main_mod  # noqa: PLC0415
    from mariana import research_settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM research_settlements")

        task_id = str(uuid.uuid4())
        await _insert_research_task_row(pool, task_id)

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls, status=200,
                                 status_value="granted")
        cfg = _cfg()
        cost_tracker = _FakeCostTracker(total_with_markup=0.30)
        user_id = "user-y01-" + uuid.uuid4().hex[:8]

        # Make the FIRST marker-completion call raise; subsequent calls
        # delegate to the real implementation.
        real_mark = main_mod._mark_research_settlement_completed
        call_state = {"n": 0}

        async def flaky_mark(db, task_id_):
            call_state["n"] += 1
            if call_state["n"] == 1:
                raise RuntimeError("transient marker write failure")
            return await real_mark(db, task_id_)

        with patch.object(httpx, "AsyncClient", return_value=client), \
             patch.object(main_mod, "_mark_research_settlement_completed", flaky_mark):
            await main_mod._deduct_user_credits(
                user_id, cost_tracker, cfg,
                reserved_credits=500, task_id=task_id, db=pool,
            )

            # RPC went through, ledger_applied_at stamped, completed_at NULL.
            assert len(rpc_calls) == 1
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT ledger_applied_at, completed_at "
                    "FROM research_settlements WHERE task_id = $1",
                    task_id,
                )
                assert row["ledger_applied_at"] is not None
                assert row["completed_at"] is None

            # Age the claim past the reconciler threshold.
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE research_settlements "
                    "SET claimed_at = now() - interval '10 minutes' "
                    "WHERE task_id = $1",
                    task_id,
                )

            # Reconciler runs.  Must NOT issue another RPC because
            # ledger_applied_at IS NOT NULL.
            await recon_mod.reconcile_pending_research_settlements(
                pool, max_age_seconds=300, batch_size=50,
            )

        assert len(rpc_calls) == 1, (
            f"reconciler must NOT replay ledger RPC after a transient "
            f"marker-write failure; got {len(rpc_calls)} RPCs: {rpc_calls!r}"
        )

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT completed_at FROM research_settlements WHERE task_id = $1",
                task_id,
            )
            assert row["completed_at"] is not None, (
                "reconciler must stamp completed_at when ledger_applied_at "
                "was already set"
            )
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# (4) Daemon resume scenario: resume re-enters _deduct_user_credits with
#     the same task_id; the ledger must mutate at most once.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_y01_resume_does_not_double_settle():
    """Simulate the A31 reproducer: settle once (success), then a second
    settle for the same task_id (as if the daemon was SIGKILL'd between
    settle and ``.running``->``.done`` rename, then resumed). Must NOT
    issue a second ledger RPC."""
    import httpx  # noqa: PLC0415

    from mariana import main as main_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM research_settlements")

        task_id = str(uuid.uuid4())
        await _insert_research_task_row(pool, task_id)

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls, status=200,
                                 status_value="granted")
        cfg = _cfg()
        # Overrun path: total = 6.00, reserved = 100 → delta = +600 → refund_credits
        cost_tracker = _FakeCostTracker(total_with_markup=6.00)
        user_id = "user-y01-" + uuid.uuid4().hex[:8]

        with patch.object(httpx, "AsyncClient", return_value=client):
            # Run 1: original settlement.
            await main_mod._deduct_user_credits(
                user_id, cost_tracker, cfg,
                reserved_credits=100, task_id=task_id, db=pool,
            )
            # Run 2 (resume): same task_id, same cost_tracker — daemon
            # picked up the .running file after a SIGKILL'd run-1 between
            # RPC return and file rename.
            await main_mod._deduct_user_credits(
                user_id, cost_tracker, cfg,
                reserved_credits=100, task_id=task_id, db=pool,
            )

        assert len(rpc_calls) == 1, (
            f"daemon resume must NOT trigger a second ledger RPC; got "
            f"{len(rpc_calls)} RPCs: {rpc_calls!r}"
        )
        body = rpc_calls[0]["json"]
        assert body.get("p_ref_type") == "research_task_overrun"
        assert body.get("p_ref_id") == task_id
        assert "refund_credits" in rpc_calls[0]["url"]
        # final = usd_to_credits(6.00) = 600; reserved = 100 → delta = +500
        assert body.get("p_credits") == 500
    finally:
        await pool.close()
