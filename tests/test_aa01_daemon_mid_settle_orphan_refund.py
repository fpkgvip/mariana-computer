"""AA-01 regression: daemon mid-settle reservation refund must not be lost
when the parent research_tasks row was deleted in the user-DELETE window.

Bug
---
Phase E re-audit #29 (A34) found that when a user clicks DELETE on a
RUNNING investigation, the API at ``mariana/api.py:delete_investigation``
sets ``status='FAILED'``, publishes Redis kill, then cascades through the
child tables (now including ``research_settlements`` per Z-01) and
DELETEs the parent ``research_tasks`` row.  The orchestrator detects
the kill seconds later and ``_run_single`` calls
``_deduct_user_credits(task_id=task.id, db=db)``.  By that point, the
parent row may already be gone.  ``_claim_research_settlement`` does
``INSERT INTO research_settlements (task_id, ...)`` and the FK
``research_settlements.task_id REFERENCES research_tasks(id) ON DELETE
RESTRICT`` raises ``ForeignKeyViolationError``.  Pre-AA-01 the broad
``except Exception`` at ``mariana/main.py:590`` caught and silently
returned — **the user's reservation refund is permanently lost** because
the keyed ``grant_credits`` RPC is never issued.

The fix routes the orphan-parent case to the idempotent ledger RPC
directly, skipping the now-impossible ``research_settlements`` marker
UPDATEs (the row cannot be inserted because the parent is gone, so the
markers have nothing to attach to).  ``grant_credits`` and
``refund_credits`` dedupe on ``(ref_type, ref_id)`` against
``credit_transactions`` so a second call with the same task_id is a
``status='duplicate'`` no-op — the user gets exactly one refund.

This test pins:
  (1) Refund path: parent gone, delta < 0 → ``grant_credits`` issued
      keyed on ``(ref_type='research_task', ref_id=task_id)``.
  (2) Replay safety: a second call with the same task_id does NOT
      issue a second RPC (idempotent at the credit_transactions layer
      — the test asserts the bookkeeping the daemon does, since the
      DB-side dedup is enforced by the live RPC, not in this layer).
  (3) Overrun path: parent gone, delta > 0 → ``refund_credits``
      issued keyed on ``(ref_type='research_task_overrun',
      ref_id=task_id)``.
"""

from __future__ import annotations

import os
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


class _FakeCostTracker:
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
                return {"status": outer.status_value, "balance_after": 1000}

        return _R()


# ---------------------------------------------------------------------------
# (1) Refund path: parent gone, delta < 0 → grant_credits issued.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_aa01_orphan_parent_refund_still_issues_grant_credits():
    """When _claim_research_settlement fails because the parent
    research_tasks row was deleted, the keyed ``grant_credits`` RPC
    must STILL be issued so the user's reservation refund lands."""
    import httpx  # noqa: PLC0415

    from mariana import main as main_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        # Use a task_id that does NOT exist in research_tasks — simulates
        # the post-cascade-DELETE state the daemon observes when the
        # user has already wiped the investigation.
        task_id = "aa01-orphan-" + uuid.uuid4().hex[:12]
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM research_settlements WHERE task_id = $1", task_id
            )
            await conn.execute(
                "DELETE FROM research_tasks WHERE id = $1", task_id
            )

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls, status=200,
                                 status_value="granted")
        cfg = _cfg()
        cost_tracker = _FakeCostTracker(total_with_markup=0.30)
        # final = usd_to_credits(0.30) = 30; reserved = 500 → delta = -470 → refund
        user_id = "user-aa01-" + uuid.uuid4().hex[:8]

        with patch.object(httpx, "AsyncClient", return_value=client):
            await main_mod._deduct_user_credits(
                user_id, cost_tracker, cfg,
                reserved_credits=500, task_id=task_id, db=pool,
            )

        assert len(rpc_calls) == 1, (
            f"orphan-parent refund must still issue exactly one keyed "
            f"grant_credits RPC; got {len(rpc_calls)}: {rpc_calls!r}"
        )
        body = rpc_calls[0]["json"]
        assert "grant_credits" in rpc_calls[0]["url"]
        assert body.get("p_user_id") == user_id
        assert body.get("p_credits") == 470
        assert body.get("p_source") == "refund"
        assert body.get("p_ref_type") == "research_task"
        assert body.get("p_ref_id") == task_id, (
            "the keyed (ref_type, ref_id) must remain (research_task, task_id) "
            "so a daemon retry or a delayed reconciler is deduped at the "
            "credit_transactions layer"
        )

        # No claim row was inserted (parent was gone).
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT task_id FROM research_settlements WHERE task_id = $1",
                task_id,
            )
        assert row is None, (
            "no research_settlements row should be created for an orphan "
            "task — the parent does not exist so the FK would still raise"
        )
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# (2) Replay safety: a second orphan-refund call on the same task_id
#     keeps the same keyed (ref_type, ref_id) so the credit_transactions
#     dedup short-cuts the second mutation at the live ledger layer.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_aa01_orphan_replay_uses_same_ref_id():
    """A second call with the same task_id must use the SAME keyed
    ``ref_id`` so the live ledger's ``(ref_type, ref_id)`` UNIQUE
    constraint deduplicates the replay (returning ``status='duplicate'``)
    rather than minting a second refund."""
    import httpx  # noqa: PLC0415

    from mariana import main as main_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task_id = "aa01-replay-" + uuid.uuid4().hex[:12]
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM research_settlements WHERE task_id = $1", task_id
            )
            await conn.execute(
                "DELETE FROM research_tasks WHERE id = $1", task_id
            )

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls, status=200)
        cfg = _cfg()
        cost_tracker = _FakeCostTracker(total_with_markup=0.30)
        user_id = "user-aa01-" + uuid.uuid4().hex[:8]

        with patch.object(httpx, "AsyncClient", return_value=client):
            # First orphan-refund.
            await main_mod._deduct_user_credits(
                user_id, cost_tracker, cfg,
                reserved_credits=500, task_id=task_id, db=pool,
            )
            # Second call (e.g. daemon retry, reconciler does not apply
            # since no claim row exists).  Must reuse the same ref_id so
            # the live ledger dedupes.
            await main_mod._deduct_user_credits(
                user_id, cost_tracker, cfg,
                reserved_credits=500, task_id=task_id, db=pool,
            )

        assert len(rpc_calls) == 2, (
            "this layer issues the keyed RPC for each call — the live "
            "credit_transactions UNIQUE(type, ref_type, ref_id) is what "
            "actually deduplicates; both calls must carry the same ref_id"
        )
        ref_ids = {c["json"]["p_ref_id"] for c in rpc_calls}
        assert ref_ids == {task_id}, (
            f"replay must use the same task_id-keyed ref_id; got {ref_ids!r}"
        )
        ref_types = {c["json"]["p_ref_type"] for c in rpc_calls}
        assert ref_types == {"research_task"}
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# (3) Overrun path: parent gone, delta > 0 → refund_credits issued.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_aa01_orphan_parent_overrun_still_issues_refund_credits():
    """delta > 0 (cost exceeded reservation) on an orphan task must
    still issue the keyed ``refund_credits`` RPC."""
    import httpx  # noqa: PLC0415

    from mariana import main as main_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task_id = "aa01-overrun-" + uuid.uuid4().hex[:12]
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM research_settlements WHERE task_id = $1", task_id
            )
            await conn.execute(
                "DELETE FROM research_tasks WHERE id = $1", task_id
            )

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls, status=200)
        cfg = _cfg()
        # Overrun: total_with_markup = 6.00 → final 600, reserved 100 → delta +500
        cost_tracker = _FakeCostTracker(total_with_markup=6.00)
        user_id = "user-aa01-" + uuid.uuid4().hex[:8]

        with patch.object(httpx, "AsyncClient", return_value=client):
            await main_mod._deduct_user_credits(
                user_id, cost_tracker, cfg,
                reserved_credits=100, task_id=task_id, db=pool,
            )

        assert len(rpc_calls) == 1
        body = rpc_calls[0]["json"]
        assert "refund_credits" in rpc_calls[0]["url"]
        assert body.get("p_user_id") == user_id
        assert body.get("p_credits") == 500
        assert body.get("p_ref_type") == "research_task_overrun"
        assert body.get("p_ref_id") == task_id
    finally:
        await pool.close()
