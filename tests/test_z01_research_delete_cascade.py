"""Z-01 regression: investigation delete must cascade through research_settlements.

Bug
---
Phase E re-audit #28 (A33) found that ``mariana/api.py:delete_investigation``
cascades through a hardcoded list of child tables before
``DELETE FROM research_tasks WHERE id = $1``, but Y-01 added the FK
``research_settlements.task_id REFERENCES research_tasks(id) ON DELETE
RESTRICT`` without updating the cascade list.  Result: any settled
investigation cannot be deleted — the parent DELETE raises
``ForeignKeyViolationError`` and the endpoint returns 500.  Same root
cause also blocks Supabase ``auth.users`` cascade-delete (GDPR
right-to-erasure) because ``research_tasks.user_id ON DELETE CASCADE``
would try to delete child tasks that have settlement rows.

This test pins the fix:
  * a settled investigation can be deleted without raising
  * an investigation with an in-flight (uncompleted) claim row can be
    deleted — the cleanup unblocks operator-driven cleanup
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
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


async def _insert_research_task_row(pool: Any, task_id: str, user_id: str) -> None:
    """Insert a research_tasks row with the given task_id and user_id metadata.

    user_id is stored only inside metadata so the test does not depend on
    the auth.users FK target.
    """
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
                $5, $6::jsonb
            )
            ON CONFLICT (id) DO NOTHING
            """,
            task_id,
            "z01-test",
            5.0,
            0.0,
            datetime.now(tz=timezone.utc),
            f'{{"user_id": "{user_id}"}}',
        )


async def _insert_completed_settlement(pool: Any, task_id: str, user_id: str) -> None:
    """Insert a research_settlements row mimicking a successful settle."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO research_settlements (
                task_id, user_id, reserved_credits, final_credits,
                delta_credits, ref_id, ledger_applied_at, completed_at
            ) VALUES ($1, $2, 100, 50, -50, $3, now(), now())
            """,
            task_id,
            user_id,
            f"research_settle:{task_id}",
        )


async def _insert_in_flight_settlement(pool: Any, task_id: str, user_id: str) -> None:
    """Insert a research_settlements row that is still mid-settle."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO research_settlements (
                task_id, user_id, reserved_credits, final_credits,
                delta_credits, ref_id
            ) VALUES ($1, $2, 100, 50, -50, $3)
            """,
            task_id,
            user_id,
            f"research_settle:{task_id}",
        )


# ---------------------------------------------------------------------------
# (1) Settled investigation must be deletable end-to-end.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_z01_delete_settled_investigation_succeeds():
    """A research task with a completed settlement row must be deletable
    without a ForeignKeyViolationError."""
    from mariana import api as api_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task_id = "z01-settled-" + uuid.uuid4().hex[:12]
        # Use a UUID-shaped user_id so the F-05 backfill test
        # ``(metadata->>'user_id')::uuid`` cast does not trip on
        # leftover rows from this test.  Z-01 only cares that the
        # endpoint's metadata-based ownership check sees the same
        # string for caller and task, so the exact format is moot.
        user_id = str(uuid.uuid4())
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM research_settlements WHERE task_id = $1", task_id)
            await conn.execute("DELETE FROM research_tasks WHERE id = $1", task_id)
        await _insert_research_task_row(pool, task_id, user_id)
        await _insert_completed_settlement(pool, task_id, user_id)

        # Drive the endpoint with the same patched-deps pattern other
        # api tests use.
        with patch.object(api_mod, "_get_db", return_value=pool), \
             patch.object(api_mod, "_validate_task_id", side_effect=lambda x: x), \
             patch.object(api_mod, "_is_admin_user", return_value=False):
            result = await api_mod.delete_investigation(
                task_id=task_id,
                current_user={"user_id": user_id, "email": "z01@test.local"},
            )

        assert result.get("status") == "deleted"
        assert result.get("task_id") == task_id

        async with pool.acquire() as conn:
            remaining_task = await conn.fetchrow(
                "SELECT id FROM research_tasks WHERE id = $1", task_id
            )
            remaining_settle = await conn.fetchrow(
                "SELECT task_id FROM research_settlements WHERE task_id = $1", task_id
            )
        assert remaining_task is None, "research_tasks row must be gone"
        assert remaining_settle is None, "research_settlements row must be cascaded"
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# (2) Investigation with an in-flight (uncompleted) claim row must also delete.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_z01_delete_with_in_flight_claim_succeeds():
    """An investigation with a research_settlements row that has
    ``completed_at IS NULL`` must still be deletable."""
    from mariana import api as api_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task_id = "z01-inflight-" + uuid.uuid4().hex[:12]
        # UUID-shaped to keep the F-05 backfill test happy on shared DB.
        user_id = str(uuid.uuid4())
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM research_settlements WHERE task_id = $1", task_id)
            await conn.execute("DELETE FROM research_tasks WHERE id = $1", task_id)
        await _insert_research_task_row(pool, task_id, user_id)
        await _insert_in_flight_settlement(pool, task_id, user_id)

        with patch.object(api_mod, "_get_db", return_value=pool), \
             patch.object(api_mod, "_validate_task_id", side_effect=lambda x: x), \
             patch.object(api_mod, "_is_admin_user", return_value=False):
            result = await api_mod.delete_investigation(
                task_id=task_id,
                current_user={"user_id": user_id, "email": "z01@test.local"},
            )

        assert result.get("status") == "deleted"

        async with pool.acquire() as conn:
            remaining_settle = await conn.fetchrow(
                "SELECT task_id FROM research_settlements WHERE task_id = $1", task_id
            )
        assert remaining_settle is None, (
            "in-flight research_settlements row must be cleaned up so the "
            "operator can delete a stuck investigation"
        )
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# (3) Sanity: the cascade list must include research_settlements explicitly.
# ---------------------------------------------------------------------------


def test_z01_research_settlements_in_cascade_list_source():
    """Pin the source-level guarantee that the cascade list mentions
    ``research_settlements`` so a future refactor cannot silently drop it.
    """
    import inspect

    from mariana import api as api_mod  # noqa: PLC0415

    src = inspect.getsource(api_mod.delete_investigation)
    assert '"research_settlements"' in src or "'research_settlements'" in src, (
        "delete_investigation must explicitly reference research_settlements "
        "in its cascade list to prevent the Y-01 FK regression"
    )
