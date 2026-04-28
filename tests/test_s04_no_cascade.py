"""S-04 regression: agent_settlements.task_id FK is ON DELETE RESTRICT.

Bug
---
``agent_settlements.task_id REFERENCES agent_tasks(id) ON DELETE CASCADE``
allowed a deleted+re-inserted task UUID to be settled twice.  Settlement
history must be permanent — operators must explicitly acknowledge a
settlement before deleting the task.

Fix
---
Schema change: ``ON DELETE RESTRICT`` plus an idempotent ALTER for
existing deployments.
"""

from __future__ import annotations

import os
import pathlib
import uuid
from typing import Any

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
        host=PGHOST, port=PGPORT, user=PGUSER, database=PGDATABASE,
        min_size=1, max_size=4,
    )


async def _ensure_schema(pool: Any) -> None:
    sql = _AGENT_SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)


@_pg_only
@pytest.mark.asyncio
async def test_s04_agent_tasks_delete_does_not_cascade():
    """Inserting an agent_task + agent_settlement, then deleting the task,
    must either (a) raise asyncpg.ForeignKeyViolationError because of the
    RESTRICT, or (b) leave the agent_settlements row intact.  Either way
    the settlement history must NOT disappear."""
    import asyncpg  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task_id = str(uuid.uuid4())
        user_id = f"user-s04-{uuid.uuid4().hex[:8]}"

        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_tasks (id, user_id, goal, state) "
                "VALUES ($1, $2, 'cascade-test', 'plan')",
                task_id, user_id,
            )
            await conn.execute(
                "INSERT INTO agent_settlements (task_id, user_id, "
                "reserved_credits, final_credits, delta_credits, ref_id) "
                "VALUES ($1, $2, 500, 30, -470, $3)",
                task_id, user_id, f"agent_settle:{task_id}",
            )

            # Attempt to delete the task — must be REJECTED by RESTRICT.
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    "DELETE FROM agent_tasks WHERE id = $1", task_id
                )

            # Settlement row still exists.
            still = await conn.fetchrow(
                "SELECT task_id FROM agent_settlements WHERE task_id = $1",
                task_id,
            )
            assert still is not None, (
                "agent_settlements row must persist — settlement history is "
                "immutable across task UUID reuse"
            )
    finally:
        await pool.close()
