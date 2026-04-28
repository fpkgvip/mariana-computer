"""CC-05 regression: reconciler ``batch_size`` must never reach PostgreSQL
as a negative or zero ``LIMIT``.

Bug
---
``mariana/main.py`` parsed ``AGENT_SETTLEMENT_RECONCILE_BATCH_SIZE`` with a
bare ``int(os.getenv(...))`` and passed the result straight into
``LIMIT $2``.  PostgreSQL rejects negative LIMIT with
``InvalidRowCountInLimitClauseError``; one bad operator env value
(``-1``) would brick both settlement daemons forever — every loop
iteration raises before claiming rows, the outer ``except Exception``
logs and sleeps, and stuck settlements never reconcile.

This test file pins the fix at TWO layers:

1. ``mariana.main._parse_reconcile_batch_size`` — the env-helper clamps
   negative / zero / unparseable values to safe values.
2. ``reconcile_pending_settlements`` (agent) and
   ``reconcile_pending_research_settlements`` (research) — defensive
   ``if batch_size <= 0: batch_size = 1`` guards at function entry so
   tests / future internal callers cannot bypass the env clamp and
   trigger ``InvalidRowCountInLimitClauseError`` either.
"""

from __future__ import annotations

import os
import pathlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

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


async def _ensure_agent_schema(pool: Any) -> None:
    sql = _AGENT_SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)


async def _ensure_research_schema(pool: Any) -> None:
    from mariana.data.db import init_schema  # noqa: PLC0415

    await init_schema(pool)


# ---------------------------------------------------------------------------
# Layer 1: env helper unit tests (no DB required).
# ---------------------------------------------------------------------------


def _import_helper():
    from mariana.main import _parse_reconcile_batch_size  # noqa: PLC0415

    return _parse_reconcile_batch_size


def test_cc05_helper_unset_returns_default(monkeypatch):
    """Env var unset → returns default unchanged."""
    helper = _import_helper()
    monkeypatch.delenv("CC05_TEST_BATCH_SIZE", raising=False)
    assert helper("CC05_TEST_BATCH_SIZE", 50) == 50


def test_cc05_helper_unparseable_returns_default(monkeypatch):
    """Env var set to non-int garbage → returns default; never raises."""
    helper = _import_helper()
    monkeypatch.setenv("CC05_TEST_BATCH_SIZE", "notanint")
    assert helper("CC05_TEST_BATCH_SIZE", 50) == 50


def test_cc05_helper_negative_clamps_to_one(monkeypatch):
    """Env var = ``-1`` → clamped to 1, NOT passed straight to LIMIT."""
    helper = _import_helper()
    monkeypatch.setenv("CC05_TEST_BATCH_SIZE", "-1")
    assert helper("CC05_TEST_BATCH_SIZE", 50) == 1


def test_cc05_helper_zero_clamps_to_one(monkeypatch):
    """Env var = ``0`` → clamped to 1.  ``LIMIT 0`` is technically legal in
    PG but defeats the purpose of the daemon (it would never claim rows);
    treat as operator error and clamp."""
    helper = _import_helper()
    monkeypatch.setenv("CC05_TEST_BATCH_SIZE", "0")
    assert helper("CC05_TEST_BATCH_SIZE", 50) == 1


def test_cc05_helper_positive_passthrough(monkeypatch):
    """Env var = ``999999`` → returned unchanged.  No upper clamp; the
    daemon should not silently downsize a deliberately large batch."""
    helper = _import_helper()
    monkeypatch.setenv("CC05_TEST_BATCH_SIZE", "999999")
    assert helper("CC05_TEST_BATCH_SIZE", 50) == 999999


def test_cc05_helper_empty_string_falls_back(monkeypatch):
    """Edge case: env var explicitly set to empty string parses as falsy
    but ``int('')`` raises — must take the unparseable fallback branch."""
    helper = _import_helper()
    monkeypatch.setenv("CC05_TEST_BATCH_SIZE", "")
    assert helper("CC05_TEST_BATCH_SIZE", 7) == 7


# ---------------------------------------------------------------------------
# Layer 2: agent reconciler — defensive entry-point clamp.
# ---------------------------------------------------------------------------


def _new_agent_task(*, reserved: int = 500, spent_usd: float = 0.30):
    from mariana.agent.models import AgentState, AgentTask  # noqa: PLC0415

    task = AgentTask(
        id=str(uuid.uuid4()),
        user_id=f"user-cc05-{uuid.uuid4().hex[:8]}",
        goal="CC-05 batch_size validation",
        budget_usd=5.0,
        spent_usd=spent_usd,
        state=AgentState.DONE,
    )
    task.reserved_credits = reserved
    task.credits_settled = False
    return task


async def _seed_agent_claim(pool: Any, *, task, age_seconds: int = 600,
                            ledger_applied: bool = True):
    """Seed an agent_settlements claim row.  ``ledger_applied=True`` so
    the marker-fixup short-circuit handles it without trying to call
    out to the ledger RPC (which would need httpx mocking)."""
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415

    await _insert_agent_task(pool, task)
    final_tokens = int(task.spent_usd * 100)
    delta = final_tokens - task.reserved_credits
    ledger_at = "now()" if ledger_applied else "NULL"
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            INSERT INTO agent_settlements (
                task_id, user_id, reserved_credits, final_credits,
                delta_credits, ref_id, claimed_at, completed_at,
                ledger_applied_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                now() - ($7 || ' seconds')::interval,
                NULL,
                {ledger_at}
            )
            """,
            task.id,
            task.user_id,
            task.reserved_credits,
            final_tokens,
            delta,
            f"agent_settle:{task.id}",
            str(age_seconds),
        )


async def _wipe_agent(pool: Any) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM agent_settlements")
        await conn.execute("DELETE FROM agent_tasks")


@_pg_only
@pytest.mark.asyncio
@pytest.mark.parametrize("bad_batch_size", [-1, 0, -999])
async def test_cc05_agent_reconciler_clamps_non_positive_batch_size(bad_batch_size):
    """Calling ``reconcile_pending_settlements`` with a non-positive
    ``batch_size`` MUST NOT raise ``InvalidRowCountInLimitClauseError``.
    The function clamps to 1 and processes normally."""
    import asyncpg as _asyncpg  # noqa: PLC0415

    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_agent_schema(pool)
        await _wipe_agent(pool)
        task = _new_agent_task()
        await _seed_agent_claim(pool, task=task, age_seconds=600,
                                ledger_applied=True)

        # Must not raise InvalidRowCountInLimitClauseError.
        try:
            attempted = await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=bad_batch_size,
            )
        except _asyncpg.exceptions.InvalidRowCountInLimitClauseError:
            pytest.fail(
                f"reconcile_pending_settlements raised "
                f"InvalidRowCountInLimitClauseError for batch_size="
                f"{bad_batch_size}; the entry-point clamp must coerce to 1"
            )

        # Clamped to 1 → exactly one row attempted (marker-fixup path).
        assert attempted == 1, (
            f"clamped batch_size=1 must still attempt the one seeded row; "
            f"got attempted={attempted}"
        )
    finally:
        await pool.close()


@_pg_only
@pytest.mark.asyncio
async def test_cc05_agent_reconciler_huge_batch_size_no_overflow():
    """``batch_size=10**9`` must not cause integer overflow / driver
    coercion failure.  PG's ``LIMIT`` accepts ``bigint`` so this is
    purely a client-side smoke test."""
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_agent_schema(pool)
        await _wipe_agent(pool)
        task = _new_agent_task()
        await _seed_agent_claim(pool, task=task, age_seconds=600,
                                ledger_applied=True)

        attempted = await recon_mod.reconcile_pending_settlements(
            pool, max_age_seconds=300, batch_size=10**9,
        )
        assert attempted == 1, (
            f"huge batch_size must process the seeded row normally; got "
            f"attempted={attempted}"
        )
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Layer 2: research reconciler — same defensive clamp.
# ---------------------------------------------------------------------------


async def _insert_research_task_row(pool: Any, task_id: str) -> None:
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
            "cc05-test",
            5.0,
            0.0,
            datetime.now(tz=timezone.utc),
        )


async def _seed_research_claim(pool: Any, *, task_id: str,
                               age_seconds: int = 600,
                               ledger_applied: bool = True) -> None:
    """Seed a research_settlements claim row with ``ledger_applied_at``
    set so the marker-fixup branch handles it without ledger RPC."""
    user_id = "user-cc05-" + uuid.uuid4().hex[:8]
    ledger_at = "now()" if ledger_applied else "NULL"
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            INSERT INTO research_settlements (
                task_id, user_id, reserved_credits, final_credits,
                delta_credits, ref_id, claimed_at, completed_at,
                ledger_applied_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                now() - ($7 || ' seconds')::interval,
                NULL,
                {ledger_at}
            )
            """,
            task_id,
            user_id,
            500,
            30,
            -470,
            f"research_settle:{task_id}",
            str(age_seconds),
        )


@_pg_only
@pytest.mark.asyncio
@pytest.mark.parametrize("bad_batch_size", [-1, 0, -42])
async def test_cc05_research_reconciler_clamps_non_positive_batch_size(
    bad_batch_size,
):
    """Calling ``reconcile_pending_research_settlements`` with a
    non-positive ``batch_size`` MUST NOT raise
    ``InvalidRowCountInLimitClauseError``.  Mirrors the agent test for
    the legacy investigation pipeline."""
    import asyncpg as _asyncpg  # noqa: PLC0415

    from mariana import research_settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_research_schema(pool)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM research_settlements")

        task_id = str(uuid.uuid4())
        await _insert_research_task_row(pool, task_id)
        await _seed_research_claim(pool, task_id=task_id, age_seconds=600,
                                   ledger_applied=True)

        try:
            attempted = await recon_mod.reconcile_pending_research_settlements(
                pool, max_age_seconds=300, batch_size=bad_batch_size,
            )
        except _asyncpg.exceptions.InvalidRowCountInLimitClauseError:
            pytest.fail(
                f"reconcile_pending_research_settlements raised "
                f"InvalidRowCountInLimitClauseError for batch_size="
                f"{bad_batch_size}; the entry-point clamp must coerce to 1"
            )

        assert attempted == 1, (
            f"clamped batch_size=1 must still attempt the one seeded row; "
            f"got attempted={attempted}"
        )

        # Confirm completed_at was stamped (marker-fixup short-circuit).
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT completed_at FROM research_settlements "
                "WHERE task_id = $1",
                task_id,
            )
            assert row is not None
            assert row["completed_at"] is not None, (
                "marker-fixup must stamp completed_at after clamp"
            )
    finally:
        await pool.close()


@_pg_only
@pytest.mark.asyncio
async def test_cc05_research_reconciler_huge_batch_size_no_overflow():
    """Same huge-value smoke test for the research path."""
    from mariana import research_settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_research_schema(pool)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM research_settlements")

        task_id = str(uuid.uuid4())
        await _insert_research_task_row(pool, task_id)
        await _seed_research_claim(pool, task_id=task_id, age_seconds=600,
                                   ledger_applied=True)

        attempted = await recon_mod.reconcile_pending_research_settlements(
            pool, max_age_seconds=300, batch_size=10**9,
        )
        assert attempted == 1
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Pure-unit form of the entry-point guard: assert it holds when the DB call
# itself is mocked away, so the defensive guard is verified independent of
# any DB behaviour.  Belt-and-braces against a future refactor that moves
# the clamp.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cc05_agent_clamp_is_function_entry_pure_unit():
    """Call the agent reconciler with a fully mocked db; verify the
    function does not raise for non-positive batch_size and short-cuts
    on empty fetch."""
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    class _FakeConn:
        async def fetch(self, query, *args):
            # Capture batch_size (last positional arg) and ensure it's >= 1.
            assert args[-1] >= 1, (
                f"reconciler must not pass non-positive LIMIT to PG; "
                f"got args={args!r}"
            )
            return []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    class _FakePool:
        def acquire(self):
            return _FakeConn()

    # All three non-positive values must short-circuit cleanly.
    for bad in (-1, 0, -10**6):
        attempted = await recon_mod.reconcile_pending_settlements(
            _FakePool(), max_age_seconds=300, batch_size=bad,
        )
        assert attempted == 0


@pytest.mark.asyncio
async def test_cc05_research_clamp_is_function_entry_pure_unit():
    """Pure-unit equivalent for the research reconciler."""
    from mariana import research_settlement_reconciler as recon_mod  # noqa: PLC0415

    class _FakeConn:
        async def fetch(self, query, *args):
            assert args[-1] >= 1, (
                f"research reconciler must not pass non-positive LIMIT to PG; "
                f"got args={args!r}"
            )
            return []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    class _FakePool:
        def acquire(self):
            return _FakeConn()

    for bad in (-1, 0, -10**6):
        attempted = await recon_mod.reconcile_pending_research_settlements(
            _FakePool(), max_age_seconds=300, batch_size=bad,
        )
        assert attempted == 0
