"""S-02 regression: agent_settlements must enforce non-negative credit columns.

Bug
---
``agent_settlements`` declared ``reserved_credits BIGINT NOT NULL`` and
``final_credits BIGINT NOT NULL`` *without* CHECK constraints.  A buggy
caller passing -1 would persist silently.

Fix
---
Add inline ``CHECK (reserved_credits >= 0)`` and
``CHECK (final_credits >= 0)`` to the CREATE TABLE plus an idempotent
ALTER ADD CONSTRAINT for in-place upgrades.  delta_credits stays signed
(it's deliberately positive for overruns and negative for refunds).
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


async def _seed_task(pool: Any, task_id: str, user_id: str) -> None:
    """Insert a minimal agent_tasks row so the FK target exists."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_tasks (id, user_id, goal, state) "
            "VALUES ($1, $2, 'check-test', 'plan') "
            "ON CONFLICT (id) DO NOTHING",
            task_id,
            user_id,
        )


@_pg_only
@pytest.mark.asyncio
async def test_s02_negative_reserved_credits_rejected():
    """INSERT with reserved_credits=-1 must raise asyncpg.CheckViolationError."""
    import asyncpg  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task_id = str(uuid.uuid4())
        await _seed_task(pool, task_id, f"user-s02-{uuid.uuid4().hex[:6]}")

        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO agent_settlements (task_id, user_id, "
                    "reserved_credits, final_credits, delta_credits, ref_id) "
                    "VALUES ($1, 'u', -1, 0, 0, 'agent_settle:x')",
                    task_id,
                )
    finally:
        await pool.close()


@_pg_only
@pytest.mark.asyncio
async def test_s02_negative_final_credits_rejected():
    """INSERT with final_credits=-1 must raise asyncpg.CheckViolationError."""
    import asyncpg  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task_id = str(uuid.uuid4())
        await _seed_task(pool, task_id, f"user-s02-{uuid.uuid4().hex[:6]}")

        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO agent_settlements (task_id, user_id, "
                    "reserved_credits, final_credits, delta_credits, ref_id) "
                    "VALUES ($1, 'u', 0, -1, 0, 'agent_settle:x')",
                    task_id,
                )
    finally:
        await pool.close()
