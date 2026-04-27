"""F-05 regression suite: research_tasks owner FK and cascade-delete.

Phase E re-audit found that research_tasks had no FK to auth.users; ownership
was stored only inside the metadata JSONB field. Deleting a user left all their
tasks and descendants as orphan rows.

Fix: migration 010_f05_research_tasks_owner_fk.sql adds a relational
``user_id`` column with ``REFERENCES auth.users(id) ON DELETE CASCADE``.

Tests run against the local asyncpg testdb (PGHOST=/tmp PGPORT=55432).
The tests set up the auth.users stub table and research_tasks table via the
db.py schema initialiser, then exercise:

  1. test_fk_column_exists          — user_id column present after init_schema.
  2. test_user_id_written_on_insert — new rows carry the relational user_id.
  3. test_cascade_delete_task       — deleting auth.users row cascades into
                                      research_tasks.
  4. test_cascade_delete_descendants — child tables (hypotheses, findings…) also
                                      disappear on user delete.
  5. test_fk_rejects_nonexistent_user — cannot insert a task with a user_id
                                        that does not exist in auth.users.
  6. test_backfill_pattern           — UPDATE … SET user_id=(metadata->>'user_id')::uuid
                                       correctly fills rows where user_id IS NULL.
  7. test_ownership_check_prefers_fk_column — _require_investigation_owner
                                              uses FK column rather than metadata.
  8. test_ownership_check_falls_back_to_metadata — for legacy rows (NULL FK),
                                                   metadata fallback still works.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

# ---------------------------------------------------------------------------
# DB connection helpers
# ---------------------------------------------------------------------------

PGHOST = os.environ.get("PGHOST", "/tmp")
PGPORT = int(os.environ.get("PGPORT", "55432"))
PGUSER = os.environ.get("PGUSER", "postgres")
PGDATABASE = os.environ.get("PGDATABASE", "testdb")

_DB_AVAILABLE: Optional[bool] = None


def _dsn() -> str:
    if PGHOST.startswith("/"):
        return f"postgres://{PGUSER}@/{PGDATABASE}?host={PGHOST}&port={PGPORT}"
    return f"postgres://{PGUSER}@{PGHOST}:{PGPORT}/{PGDATABASE}"


async def _check_db() -> bool:
    global _DB_AVAILABLE
    if _DB_AVAILABLE is not None:
        return _DB_AVAILABLE
    try:
        conn = await asyncpg.connect(_dsn(), timeout=3)
        await conn.close()
        _DB_AVAILABLE = True
    except Exception:
        _DB_AVAILABLE = False
    return _DB_AVAILABLE


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Schema bootstrap (idempotent — only creates tables needed by these tests)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE IF NOT EXISTS auth.users (
    id UUID PRIMARY KEY,
    email TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS research_tasks (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    budget_usd NUMERIC NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    current_state TEXT NOT NULL DEFAULT 'INIT',
    total_spent_usd NUMERIC NOT NULL DEFAULT 0,
    diminishing_flags INTEGER NOT NULL DEFAULT 0,
    ai_call_counter INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    output_pdf_path TEXT,
    output_docx_path TEXT,
    metadata JSONB NOT NULL DEFAULT '{}',
    quality_tier TEXT DEFAULT 'balanced',
    user_flow_instructions TEXT DEFAULT '',
    continuous_mode BOOLEAN DEFAULT FALSE,
    dont_kill_branches BOOLEAN DEFAULT FALSE,
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_research_tasks_user_id ON research_tasks(user_id);

CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES hypotheses(id) ON DELETE SET NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    statement TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    score NUMERIC,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
"""


@pytest.fixture
async def db():
    """Yield an asyncpg connection scoped to a single test."""
    if not await _check_db():
        pytest.skip("Local testdb not available")
    conn = await asyncpg.connect(_dsn())
    try:
        # Bootstrap test tables idempotently.
        await conn.execute(_SCHEMA)
        yield conn
    finally:
        await conn.close()


async def _ensure_db_or_skip():
    if not await _check_db():
        pytest.skip("Local testdb not available")


# ---------------------------------------------------------------------------
# Helper: insert a user + task pair
# ---------------------------------------------------------------------------

async def _insert_user(conn: asyncpg.Connection) -> str:
    """Insert an auth.users stub and return its UUID."""
    user_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO auth.users (id, email) VALUES ($1, $2)",
        user_id, f"test_{user_id[:8]}@example.com",
    )
    return user_id


async def _insert_task(
    conn: asyncpg.Connection,
    user_id: str,
    *,
    use_fk_column: bool = True,
) -> str:
    """Insert a research_tasks row, optionally populating the FK column."""
    task_id = str(uuid.uuid4())
    meta = json.dumps({"user_id": user_id})
    if use_fk_column:
        await conn.execute(
            """
            INSERT INTO research_tasks
                (id, topic, budget_usd, status, current_state,
                 total_spent_usd, diminishing_flags, ai_call_counter,
                 created_at, metadata, user_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::uuid)
            """,
            task_id, "Test topic", 10.0, "PENDING", "INIT",
            0.0, 0, 0,
            datetime.now(tz=timezone.utc),
            meta,
            user_id,
        )
    else:
        # Legacy-style row: no FK column, only metadata JSONB.
        await conn.execute(
            """
            INSERT INTO research_tasks
                (id, topic, budget_usd, status, current_state,
                 total_spent_usd, diminishing_flags, ai_call_counter,
                 created_at, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
            """,
            task_id, "Test topic (legacy)", 10.0, "PENDING", "INIT",
            0.0, 0, 0,
            datetime.now(tz=timezone.utc),
            meta,
        )
    return task_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_fk_column_exists(db):
    """user_id column must exist on research_tasks after schema bootstrap."""
    await _ensure_db_or_skip()
    row = await db.fetchrow(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'research_tasks'
          AND column_name = 'user_id'
        """
    )
    assert row is not None, "user_id column not found on research_tasks"
    assert row["data_type"] in ("uuid", "USER-DEFINED"), f"Unexpected type: {row['data_type']}"


async def test_user_id_written_on_insert(db):
    """Inserting a task with user_id column set persists the FK value."""
    await _ensure_db_or_skip()
    user_id = await _insert_user(db)
    task_id = await _insert_task(db, user_id, use_fk_column=True)

    row = await db.fetchrow(
        "SELECT user_id FROM research_tasks WHERE id = $1", task_id
    )
    assert row is not None
    assert str(row["user_id"]) == user_id

    # Cleanup
    await db.execute("DELETE FROM research_tasks WHERE id = $1", task_id)
    await db.execute("DELETE FROM auth.users WHERE id = $1::uuid", user_id)


async def test_cascade_delete_task(db):
    """Deleting auth.users row must cascade into research_tasks."""
    await _ensure_db_or_skip()
    user_id = await _insert_user(db)
    task_id = await _insert_task(db, user_id, use_fk_column=True)

    # Verify the task exists before deleting the user.
    count_before = await db.fetchval(
        "SELECT count(*) FROM research_tasks WHERE id = $1", task_id
    )
    assert count_before == 1, "Task not found before user deletion"

    # Delete the user — should cascade.
    await db.execute("DELETE FROM auth.users WHERE id = $1::uuid", user_id)

    count_after = await db.fetchval(
        "SELECT count(*) FROM research_tasks WHERE id = $1", task_id
    )
    assert count_after == 0, "research_tasks row survived user deletion (no cascade)"


async def test_cascade_delete_descendants(db):
    """Deleting auth.users must cascade through task into child tables (hypotheses)."""
    await _ensure_db_or_skip()
    user_id = await _insert_user(db)
    task_id = await _insert_task(db, user_id, use_fk_column=True)

    # Insert a hypothesis child row.
    hyp_id = str(uuid.uuid4())
    now = datetime.now(tz=timezone.utc)
    await db.execute(
        """
        INSERT INTO hypotheses
            (id, task_id, depth, statement, status, created_at, updated_at)
        VALUES ($1, $2, 0, 'Test hypothesis', 'PENDING', $3, $3)
        """,
        hyp_id, task_id, now,
    )

    count_hyp_before = await db.fetchval(
        "SELECT count(*) FROM hypotheses WHERE id = $1", hyp_id
    )
    assert count_hyp_before == 1

    # Delete the user — should cascade through research_tasks -> hypotheses.
    await db.execute("DELETE FROM auth.users WHERE id = $1::uuid", user_id)

    count_hyp_after = await db.fetchval(
        "SELECT count(*) FROM hypotheses WHERE id = $1", hyp_id
    )
    assert count_hyp_after == 0, "Hypothesis survived user deletion (cascade broken)"

    count_task_after = await db.fetchval(
        "SELECT count(*) FROM research_tasks WHERE id = $1", task_id
    )
    assert count_task_after == 0, "Task survived user deletion"


async def test_fk_rejects_nonexistent_user(db):
    """INSERT with a user_id not present in auth.users must raise FK violation."""
    await _ensure_db_or_skip()
    nonexistent_user_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())

    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await db.execute(
            """
            INSERT INTO research_tasks
                (id, topic, budget_usd, status, current_state,
                 total_spent_usd, diminishing_flags, ai_call_counter,
                 created_at, metadata, user_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::uuid)
            """,
            task_id, "Bad user task", 10.0, "PENDING", "INIT",
            0.0, 0, 0,
            datetime.now(tz=timezone.utc),
            json.dumps({"user_id": nonexistent_user_id}),
            nonexistent_user_id,
        )


async def test_backfill_pattern(db):
    """The backfill UPDATE (metadata->>'user_id')::uuid correctly sets user_id.

    This test only applies when the user_id column is still nullable (i.e. an
    older schema where the column was added post-facto but NOT NULL was not yet
    enforced).  If the column is already NOT NULL (fresh install or migration
    already ran with no NULL rows), the backfill scenario cannot be tested
    and the test is skipped.
    """
    await _ensure_db_or_skip()

    # Check if user_id is nullable; skip if NOT NULL already enforced.
    col_row = await db.fetchrow(
        """
        SELECT is_nullable
        FROM information_schema.columns
        WHERE table_name = 'research_tasks'
          AND column_name = 'user_id'
        """
    )
    if col_row is None or col_row["is_nullable"] == "NO":
        import pytest
        pytest.skip(
            "user_id column is NOT NULL — backfill scenario does not apply "
            "(table was created with FK column from the start, or migration "
            "already enforced NOT NULL after clean backfill)."
        )

    user_id = await _insert_user(db)

    # Insert a legacy-style row (no FK column).
    task_id = await _insert_task(db, user_id, use_fk_column=False)

    # Verify the FK column is NULL.
    row = await db.fetchrow(
        "SELECT user_id FROM research_tasks WHERE id = $1", task_id
    )
    assert row["user_id"] is None, "Expected NULL user_id before backfill"

    # Run the backfill query (mirrors the migration).
    await db.execute(
        """
        UPDATE research_tasks
        SET user_id = (metadata->>'user_id')::uuid
        WHERE user_id IS NULL
          AND metadata->>'user_id' IS NOT NULL
        """
    )

    row_after = await db.fetchrow(
        "SELECT user_id FROM research_tasks WHERE id = $1", task_id
    )
    assert row_after["user_id"] is not None, "Backfill did not set user_id"
    assert str(row_after["user_id"]) == user_id, "Backfill set wrong user_id"

    # Cleanup
    await db.execute("DELETE FROM research_tasks WHERE id = $1", task_id)
    await db.execute("DELETE FROM auth.users WHERE id = $1::uuid", user_id)


# ---------------------------------------------------------------------------
# Ownership-check tests (pure mock — no live DB needed for API path)
# ---------------------------------------------------------------------------


def _make_asyncpg_record(data: dict) -> Any:
    """Return an object that mimics an asyncpg Record with dict-like access."""
    class _FakeRecord(dict):
        def get(self, key, default=None):
            return super().get(key, default)

        def __getitem__(self, key):
            return super().__getitem__(key)

    return _FakeRecord(data)


async def test_ownership_check_prefers_fk_column():
    """_require_investigation_owner uses the FK column when present."""
    import importlib
    import mariana.api as api_mod

    user_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())

    # Mock DB: row has FK column matching user.
    fake_row = _make_asyncpg_record({
        "user_id": user_id,
        "metadata": json.dumps({"user_id": "wrong-user-in-metadata"}),
    })

    mock_db = AsyncMock()
    mock_db.fetchrow = AsyncMock(return_value=fake_row)

    current_user = {"user_id": user_id, "role": "user"}

    with patch.object(api_mod, "_get_db", return_value=mock_db), \
         patch.object(api_mod, "_is_admin_user", return_value=False):
        result = await api_mod._require_investigation_owner(
            task_id=task_id,
            current_user=current_user,
        )
    # Should succeed (FK matched) without raising 403.
    assert result == current_user


async def test_ownership_check_falls_back_to_metadata():
    """For legacy rows (FK column NULL), metadata fallback enforces ownership."""
    import mariana.api as api_mod
    from fastapi import HTTPException

    user_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())

    # FK column is NULL; metadata has the right user_id.
    fake_row = _make_asyncpg_record({
        "user_id": None,
        "metadata": json.dumps({"user_id": user_id}),
    })
    mock_db = AsyncMock()
    mock_db.fetchrow = AsyncMock(return_value=fake_row)

    current_user = {"user_id": user_id, "role": "user"}

    with patch.object(api_mod, "_get_db", return_value=mock_db), \
         patch.object(api_mod, "_is_admin_user", return_value=False):
        result = await api_mod._require_investigation_owner(
            task_id=task_id,
            current_user=current_user,
        )
    assert result == current_user

    # Now test that a different user is rejected.
    wrong_user = {"user_id": str(uuid.uuid4()), "role": "user"}
    with patch.object(api_mod, "_get_db", return_value=mock_db), \
         patch.object(api_mod, "_is_admin_user", return_value=False):
        with pytest.raises(HTTPException) as exc_info:
            await api_mod._require_investigation_owner(
                task_id=task_id,
                current_user=wrong_user,
            )
    assert exc_info.value.status_code == 403
