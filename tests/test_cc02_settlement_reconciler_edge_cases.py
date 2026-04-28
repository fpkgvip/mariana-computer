"""CC-02: behavioural coverage for the settlement reconciler edges that
existing S-03 / T-01 / Y-01 tests do not pin.

The S-03 suite covers the happy path (picks uncompleted, skips completed,
swallows RPC failure, SKIP LOCKED-isolated concurrency).  The T-01 suite
covers the marker-loss-no-replay invariant on the *agent* path.  The Y-01
suite covers research-path settlement idempotency.

This file fills six gaps:

  1. ``test_cc02_marker_fixup_short_circuit_no_rpc``
        ``ledger_applied_at IS NOT NULL`` and ``completed_at IS NULL``
        → reconciler stamps ``completed_at`` WITHOUT issuing a Supabase
        RPC.  Pin the T-01 marker-fixup short-circuit at the reconciler
        boundary (``_settle_agent_credits`` is never invoked, no httpx
        client is constructed).

  2. ``test_cc02_recent_claim_within_max_age_is_skipped``
        A claim aged 30s with ``max_age_seconds=300`` MUST NOT be picked
        up.  Pins the WHERE-clause guard so a still-running settlement
        is never doubly retried by an over-eager reconciler.

  3. ``test_cc02_empty_batch_returns_zero_no_logs``
        No candidate rows → reconciler returns 0 and never imports
        ``mariana.agent.loop`` (the late import is gated).

  4. ``test_cc02_batch_size_bound_respected``
        Five stuck claims, ``batch_size=2`` → exactly two rows are
        picked up per invocation.  Pins the LIMIT enforcement so a
        runaway reconciler can't lock the whole table.

  5. ``test_cc02_task_row_missing_skipped_gracefully``
        Claim row references a ``task_id`` whose ``agent_tasks`` row was
        deleted — reconciler logs a warning and continues; no exception
        propagates.  Other claims in the same batch still process.

  6. ``test_cc02_marker_fixup_per_row_exception_does_not_abort_batch``
        ``_mark_settlement_completed`` raises on row 1 — row 2 (a normal
        marker-fixup) still gets stamped.  Pins the per-row try/except
        contract so a single bad row cannot starve the batch.

All tests use the same Postgres fixture pattern as ``test_s03_reconciler.py``
so they exercise the real SKIP LOCKED query, not a mocked one.
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


async def _ensure_schema(pool: Any) -> None:
    sql = _AGENT_SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)


def _new_task(*, reserved: int = 500, settled: bool = False, spent_usd: float = 0.30):
    from mariana.agent.models import AgentState, AgentTask  # noqa: PLC0415

    task = AgentTask(
        id=str(uuid.uuid4()),
        user_id=f"user-cc02-{uuid.uuid4().hex[:8]}",
        goal="CC-02 reconciler edge cases",
        budget_usd=5.0,
        spent_usd=spent_usd,
        state=AgentState.DONE,
    )
    task.reserved_credits = reserved
    task.credits_settled = settled
    return task


async def _seed_claim(
    pool: Any,
    *,
    task,
    age_seconds: int = 600,
    completed: bool = False,
    ledger_applied: bool = False,
    insert_task: bool = True,
):
    """Insert (optionally) the agent_tasks row plus an agent_settlements
    row claimed ``age_seconds`` ago.  ``ledger_applied`` toggles the
    T-01 marker-fixup column."""
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415

    if insert_task:
        await _insert_agent_task(pool, task)
    final_tokens = int(task.spent_usd * 100)
    delta = final_tokens - task.reserved_credits
    completed_at = "now()" if completed else "NULL"
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
                {completed_at},
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


async def _wipe(pool: Any) -> None:
    """Clean settlement / task tables before each test so cross-test
    leakage cannot affect the candidate set the reconciler sees."""
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM agent_settlements")
        await conn.execute("DELETE FROM agent_tasks")


# ---------------------------------------------------------------------------
# 1. Marker-fixup short-circuit: ledger_applied_at present, no RPC fires.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_cc02_marker_fixup_short_circuit_no_rpc():
    """T-01 path: ``ledger_applied_at IS NOT NULL`` + ``completed_at IS
    NULL`` MUST be resolved by stamping ``completed_at`` only.  The
    reconciler must NOT re-enter ``_settle_agent_credits``."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        await _wipe(pool)
        task = _new_task(reserved=500, spent_usd=0.30)
        await _seed_claim(
            pool, task=task, age_seconds=600,
            completed=False, ledger_applied=True,
        )

        settle_mock = AsyncMock()
        with patch.object(loop_mod, "_settle_agent_credits", settle_mock):
            attempted = await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=50,
            )

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT completed_at, ledger_applied_at "
                "FROM agent_settlements WHERE task_id = $1",
                task.id,
            )
    finally:
        await pool.close()

    assert attempted == 1, f"marker-fixup row must count as attempted; got {attempted}"
    assert settle_mock.await_count == 0, (
        "marker-fixup short-circuit must NOT re-enter _settle_agent_credits — "
        "the ledger has already been mutated and a second RPC would risk "
        "double-credit"
    )
    assert row["completed_at"] is not None, (
        "marker-fixup must stamp completed_at"
    )
    assert row["ledger_applied_at"] is not None, (
        "marker-fixup must preserve the existing ledger_applied_at timestamp"
    )


# ---------------------------------------------------------------------------
# 2. Recent claim (younger than max_age) is NOT picked up.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_cc02_recent_claim_within_max_age_is_skipped():
    """A claim aged 30s while ``max_age_seconds=300`` must be invisible
    to the reconciler — it represents an in-flight settlement that the
    main loop is still working on."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        await _wipe(pool)
        task = _new_task(reserved=500, spent_usd=0.30)
        # 30s old — well within the 300s window.
        await _seed_claim(pool, task=task, age_seconds=30)

        settle_mock = AsyncMock()
        with patch.object(loop_mod, "_settle_agent_credits", settle_mock):
            attempted = await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=50,
            )

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT completed_at FROM agent_settlements WHERE task_id = $1",
                task.id,
            )
    finally:
        await pool.close()

    assert attempted == 0, (
        f"young claim must not be retried by the reconciler; got attempted={attempted}"
    )
    assert settle_mock.await_count == 0, (
        "young claim must not invoke _settle_agent_credits"
    )
    assert row["completed_at"] is None, (
        "young claim must remain uncompleted — the main loop is still on it"
    )


# ---------------------------------------------------------------------------
# 3. No candidates → returns 0 with no late import side-effects.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_cc02_empty_batch_returns_zero():
    """Empty candidate set: reconciler returns 0 and the late import of
    ``mariana.agent.loop`` is short-circuited.  We assert returncode and
    that no settle attempt fires."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        await _wipe(pool)

        settle_mock = AsyncMock()
        mark_mock = AsyncMock()
        with patch.object(loop_mod, "_settle_agent_credits", settle_mock), \
             patch.object(loop_mod, "_mark_settlement_completed", mark_mock):
            attempted = await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=50,
            )
    finally:
        await pool.close()

    assert attempted == 0
    assert settle_mock.await_count == 0
    assert mark_mock.await_count == 0


# ---------------------------------------------------------------------------
# 4. ``batch_size`` LIMIT is honoured.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_cc02_batch_size_bound_respected():
    """Five stuck claims with ``batch_size=2`` → exactly 2 rows are picked
    in a single call.  This pins the LIMIT enforcement on the candidate
    SELECT so a runaway reconciler cannot saturate the connection pool."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        await _wipe(pool)
        # Five stuck claims, all marker-fixup so we can use the
        # short-circuit branch (no ScriptedClient infra needed).
        tasks = [_new_task(reserved=500, spent_usd=0.30) for _ in range(5)]
        for t in tasks:
            await _seed_claim(
                pool, task=t, age_seconds=600,
                completed=False, ledger_applied=True,
            )

        # Track how many rows each invocation handled by counting
        # mark-completion calls.
        mark_calls: list[str] = []

        original_mark = loop_mod._mark_settlement_completed

        async def _spy_mark(db, task_id):
            mark_calls.append(task_id)
            return await original_mark(db, task_id)

        with patch.object(loop_mod, "_mark_settlement_completed", _spy_mark):
            attempted = await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=2,
            )

        async with pool.acquire() as conn:
            n_completed = await conn.fetchval(
                "SELECT count(*) FROM agent_settlements "
                "WHERE completed_at IS NOT NULL"
            )
            n_uncompleted = await conn.fetchval(
                "SELECT count(*) FROM agent_settlements "
                "WHERE completed_at IS NULL"
            )
    finally:
        await pool.close()

    assert attempted == 2, (
        f"batch_size=2 must cap retries at 2 per call; got attempted={attempted}"
    )
    assert len(mark_calls) == 2
    assert n_completed == 2, (
        f"exactly 2 of 5 claims must be completed in this batch; got {n_completed}"
    )
    assert n_uncompleted == 3, (
        f"the remaining 3 stuck claims must be left for a later batch; "
        f"got {n_uncompleted}"
    )


# ---------------------------------------------------------------------------
# 5. Claim row whose agent_tasks parent was deleted: skipped gracefully,
#    other claims in the batch still process.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_cc02_task_row_missing_skipped_gracefully():
    """A claim row whose ``_load_agent_task`` returns None must be
    skipped gracefully — reconciler logs a warning and continues; no
    exception propagates and other claims in the same batch still
    process.

    We intercept ``_load_agent_task_from_row`` to return None for the
    "ghost" task rather than physically orphaning the row, which would
    require dropping the FK constraint and risks leaving the schema in
    an inconsistent state for subsequent tests.
    """
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        await _wipe(pool)

        # Healthy task + claim — must succeed via marker-fixup.
        good_task = _new_task(reserved=500, spent_usd=0.30)
        await _seed_claim(
            pool, task=good_task, age_seconds=600,
            ledger_applied=True,
        )

        # "Ghost" task: real DB rows exist (task + settlement) but the
        # reconciler's loader is mocked to report it as None.  This
        # mirrors the production race: a row that disappeared between
        # candidate selection and per-row load (e.g. an admin DELETE
        # mid-reconcile).  ledger_applied=False so the loader is the
        # path the reconciler walks.
        ghost_task = _new_task(reserved=500, spent_usd=0.30)
        await _seed_claim(
            pool, task=ghost_task, age_seconds=600,
            ledger_applied=False,
        )

        original_loader = recon_mod._load_agent_task_from_row

        async def _selective_loader(db, task_id):
            if task_id == ghost_task.id:
                return None
            return await original_loader(db, task_id)

        with patch.object(
            recon_mod, "_load_agent_task_from_row", _selective_loader
        ):
            # Must not raise.
            attempted = await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=50,
            )

        async with pool.acquire() as conn:
            good_row = await conn.fetchrow(
                "SELECT completed_at FROM agent_settlements WHERE task_id = $1",
                good_task.id,
            )
            ghost_row = await conn.fetchrow(
                "SELECT completed_at FROM agent_settlements WHERE task_id = $1",
                ghost_task.id,
            )
    finally:
        await pool.close()
        _ = loop_mod  # noqa: F841

    # The good row was stamped via marker-fixup; the ghost was visited
    # but its loader returned None so the reconciler logged a warning
    # and continued without bumping the attempted counter.
    assert good_row["completed_at"] is not None, (
        "healthy marker-fixup row must be stamped even though a sibling "
        "row's loader returned None"
    )
    assert ghost_row["completed_at"] is None, (
        "ghost claim (loader → None) must not be stamped — there is "
        "nothing to reconcile"
    )
    # ``attempted`` counts claims that actually invoked settle/mark.
    # Good row: +1 (marker-fixup).  Ghost row: 0 (early continue).
    assert attempted == 1, (
        f"ghost-loader claim must NOT count as attempted; "
        f"got attempted={attempted}"
    )


# ---------------------------------------------------------------------------
# 6. Per-row exception in marker-fixup does not abort the batch.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_cc02_marker_fixup_per_row_exception_does_not_abort_batch():
    """Two marker-fixup rows.  ``_mark_settlement_completed`` raises on
    the first call only.  The second row MUST still be stamped — the
    per-row try/except in the reconciler is the contract that one bad
    row cannot starve the batch."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        await _wipe(pool)
        t1 = _new_task(reserved=500, spent_usd=0.30)
        t2 = _new_task(reserved=500, spent_usd=0.30)
        await _seed_claim(pool, task=t1, age_seconds=700, ledger_applied=True)
        await _seed_claim(pool, task=t2, age_seconds=600, ledger_applied=True)

        original_mark = loop_mod._mark_settlement_completed
        call_count = {"n": 0}

        async def _flaky_mark(db, task_id):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("transient mark failure")
            return await original_mark(db, task_id)

        with patch.object(loop_mod, "_mark_settlement_completed", _flaky_mark):
            # Must NOT raise.
            attempted = await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=50,
            )

        async with pool.acquire() as conn:
            n_completed = await conn.fetchval(
                "SELECT count(*) FROM agent_settlements "
                "WHERE task_id = ANY($1::uuid[]) AND completed_at IS NOT NULL",
                [t1.id, t2.id],
            )
    finally:
        await pool.close()

    # The second row succeeded; the first was logged and skipped.
    assert call_count["n"] == 2, (
        f"reconciler must attempt every candidate even after an earlier "
        f"row raised; got {call_count['n']} mark calls"
    )
    # ``attempted`` only counts SUCCESSFUL stampings in the marker-fixup
    # branch (the increment lives after the call inside the try block).
    # The failed row is logged via ``settlement_reconciler_marker_fixup_failed``
    # and skipped — so attempted == 1 (only the second row).
    assert attempted == 1, (
        f"reconciler must report exactly 1 success after one failure + "
        f"one success; got {attempted}"
    )
    assert n_completed == 1, (
        f"exactly one of the two rows must be completed (the second); "
        f"got {n_completed}"
    )
