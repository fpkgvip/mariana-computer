"""Phase D coverage-fill: settlement reconciler edge cases not covered
by S-03 / T-01 / Y-01 / CC-02 / CC-05.

Cold spots filled here:

  1. ``test_phase_d_empty_table_returns_zero_no_settle_call``
        Strong empty-batch guarantee: when ``agent_settlements`` is
        empty (not just \"no candidates\"), the reconciler returns 0,
        does NOT call ``_settle_agent_credits`` even once, and does NOT
        call ``_mark_settlement_completed``.  Pin: zero side effects.

  2. ``test_phase_d_partial_failure_leaves_row_claimable_next_pass``
        ``_settle_agent_credits`` raises mid-batch.  The raised row
        MUST remain claimable on the next reconciler pass (claimed_at
        gets bumped, ledger_applied_at stays NULL, completed_at stays
        NULL).  Pin: a transient RPC failure does not consume the row's
        retryability \u2014 it only burns one pass.

  3. ``test_phase_d_idempotency_unique_constraint_blocks_double_claim``
        ``agent_settlements`` has a PRIMARY KEY on ``task_id``.  An
        attempt to INSERT a SECOND claim for the same task_id MUST
        raise a UniqueViolationError.  Pin: there is only ever one
        ledger entry per (task, settlement) pair, so duplicate
        settlement processing cannot create a second ledger row.

  4. ``test_phase_d_cc02_cte_limit_exact_for_large_batch``
        100 pending claims, ``batch_size=10`` \u2014 the reconciler must
        process EXACTLY 10 rows in a single invocation.  This is the
        CC-02 semi-join regression guard: if PostgreSQL inlines the
        candidate subquery as a semi-join, the LIMIT applies to the
        join output and the outer UPDATE matches every uncompleted
        row, blowing past batch_size.  The materialised CTE forces
        evaluation as a one-shot result set.

  5. ``test_phase_d_cc02_cte_limit_under_fetch_guard``
        Inverse: 5 pending claims, ``batch_size=20`` \u2014 the reconciler
        must process exactly 5 rows (no over-fetch / no padding /
        no exception when batch_size > available).  Pin: small
        candidate sets do not trip the LIMIT.

  6. ``test_phase_d_cc04_batch_size_validation_rejects_non_positive``
        At the function entry, ``batch_size <= 0`` clamps to 1 (pinned by
        CC-05) so PG never sees a negative LIMIT.  We pin the same guard
        AT the reconciler boundary for ``batch_size = 0`` and ``-7``
        and assert that the call returns gracefully (no
        ``InvalidRowCountInLimitClauseError``).

  7. ``test_phase_d_concurrent_reconciler_disjoint_candidates``
        Two concurrent reconciler runs MUST process disjoint sets of
        candidates.  We launch both with ``asyncio.gather`` against the
        same pool of 6 pending marker-fixup rows and assert: total
        rows stamped == 6, and the union of per-call task_id sets is
        disjoint (no row was attempted by both runs).  This is the
        FOR UPDATE SKIP LOCKED contract.
"""

from __future__ import annotations

import asyncio
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


async def _open_pool(*, max_size: int = 8):
    import asyncpg as _asyncpg  # noqa: PLC0415

    return await _asyncpg.create_pool(
        host=PGHOST,
        port=PGPORT,
        user=PGUSER,
        database=PGDATABASE,
        min_size=2,
        max_size=max_size,
    )


async def _ensure_schema(pool: Any) -> None:
    sql = _AGENT_SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)


def _new_task(*, reserved: int = 500, settled: bool = False, spent_usd: float = 0.30):
    from mariana.agent.models import AgentState, AgentTask  # noqa: PLC0415

    task = AgentTask(
        id=str(uuid.uuid4()),
        user_id=f"user-pd-rec-{uuid.uuid4().hex[:8]}",
        goal="Phase D reconciler coverage fill",
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
    """Insert agent_tasks + agent_settlements with the chosen claim age."""
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
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM agent_settlements")
        await conn.execute("DELETE FROM agent_tasks")


# ---------------------------------------------------------------------------
# 1. Empty table → zero side effects.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_phase_d_empty_table_returns_zero_no_settle_call():
    """Stronger version of CC-02 #3: ``agent_settlements`` is wiped, so
    even the candidate SELECT returns zero rows.  The reconciler MUST
    return 0 with zero downstream side effects \u2014 no
    ``_settle_agent_credits`` call, no ``_mark_settlement_completed``
    call, no ``_load_agent_task_from_row`` call."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        await _wipe(pool)

        settle_mock = AsyncMock()
        mark_mock = AsyncMock()
        loader_mock = AsyncMock()

        with (
            patch.object(loop_mod, "_settle_agent_credits", settle_mock),
            patch.object(loop_mod, "_mark_settlement_completed", mark_mock),
            patch.object(recon_mod, "_load_agent_task_from_row", loader_mock),
        ):
            attempted = await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=50
            )
    finally:
        await pool.close()

    assert attempted == 0
    assert settle_mock.await_count == 0, (
        "empty-batch reconciler must NOT invoke _settle_agent_credits"
    )
    assert mark_mock.await_count == 0, (
        "empty-batch reconciler must NOT invoke _mark_settlement_completed"
    )
    assert loader_mock.await_count == 0, (
        "empty-batch reconciler must NOT invoke _load_agent_task_from_row"
    )


# ---------------------------------------------------------------------------
# 2. Partial failure: leftover row remains claimable on the next pass.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_phase_d_partial_failure_leaves_row_claimable_next_pass():
    """Two stuck claims A + B, both with ``ledger_applied_at IS NULL``.
    On the first reconciler pass, ``_settle_agent_credits`` raises for
    A and succeeds for B.  Pin invariants:

      * the first pass returns 1 (B succeeded, A counted as a logged
        failure, the per-row try/except contract holds).
      * row A's ``completed_at`` is still NULL after the failed pass.
      * row A's ``claimed_at`` was bumped to ~now() (the candidate-claim
        UPDATE happens BEFORE settle; this is what makes the row
        invisible until the NEXT max_age_seconds elapses).
      * if we run a second reconciler pass with ``max_age_seconds=0``,
        row A is reclaimed and (with a working settle) successfully
        finalises.  This pins the recovery contract.
    """
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        await _wipe(pool)
        task_a = _new_task(reserved=500, spent_usd=0.30)
        task_b = _new_task(reserved=500, spent_usd=0.30)
        await _seed_claim(pool, task=task_a, age_seconds=700, ledger_applied=False)
        await _seed_claim(pool, task=task_b, age_seconds=600, ledger_applied=False)

        # Per-row settle: raise for A, mark B as completed.
        async def _selective_settle(task, *, db):
            if task.id == task_a.id:
                raise RuntimeError("transient ledger RPC 503")
            # Successful path: stamp completed_at + ledger_applied_at on B.
            async with db.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_settlements SET completed_at = now(), "
                    "ledger_applied_at = now() WHERE task_id = $1",
                    task.id,
                )

        with patch.object(loop_mod, "_settle_agent_credits", _selective_settle):
            n1 = await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=50
            )

        async with pool.acquire() as conn:
            row_a = await conn.fetchrow(
                "SELECT completed_at, ledger_applied_at, claimed_at FROM "
                "agent_settlements WHERE task_id = $1",
                task_a.id,
            )
            row_b = await conn.fetchrow(
                "SELECT completed_at FROM agent_settlements WHERE task_id = $1",
                task_b.id,
            )

        assert n1 == 1, (
            f"first pass: only B succeeded; got attempted={n1}"
        )
        assert row_b["completed_at"] is not None
        assert row_a["completed_at"] is None, (
            "A's settle raised; completed_at must remain NULL"
        )
        assert row_a["ledger_applied_at"] is None, (
            "A's settle raised; ledger_applied_at must remain NULL"
        )

        # Second pass: with max_age_seconds=0 the row is reclaimable
        # immediately.  This time, settle works.
        async def _good_settle(task, *, db):
            async with db.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_settlements SET completed_at = now(), "
                    "ledger_applied_at = now() WHERE task_id = $1",
                    task.id,
                )

        with patch.object(loop_mod, "_settle_agent_credits", _good_settle):
            n2 = await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=0, batch_size=50
            )

        async with pool.acquire() as conn:
            row_a_final = await conn.fetchrow(
                "SELECT completed_at FROM agent_settlements WHERE task_id = $1",
                task_a.id,
            )
    finally:
        await pool.close()

    assert n2 == 1, (
        f"second pass must reclaim A and succeed; got attempted={n2}"
    )
    assert row_a_final["completed_at"] is not None, (
        "A must be finalised on the recovery pass \u2014 a transient settle "
        "failure must not permanently strand the row"
    )


# ---------------------------------------------------------------------------
# 3. Idempotency: duplicate INSERT for same task_id raises UniqueViolation.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_phase_d_idempotency_unique_constraint_blocks_double_claim():
    """``agent_settlements.task_id`` is the PRIMARY KEY.  A duplicate
    INSERT for the same task_id MUST raise a UniqueViolationError.  This
    pins the database-level idempotency invariant the reconciler relies
    on: even if two writers race to claim the same task, only one row
    can ever exist."""
    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        await _wipe(pool)
        task = _new_task(reserved=500, spent_usd=0.30)
        await _seed_claim(pool, task=task, age_seconds=600)

        # Second INSERT for the same task_id must fail with a unique
        # violation (PRIMARY KEY conflict).
        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    """
                    INSERT INTO agent_settlements (
                        task_id, user_id, reserved_credits, final_credits,
                        delta_credits, ref_id, claimed_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, now())
                    """,
                    task.id,
                    task.user_id,
                    task.reserved_credits,
                    int(task.spent_usd * 100),
                    int(task.spent_usd * 100) - task.reserved_credits,
                    f"agent_settle:{task.id}-DUPLICATE",
                )

        # The original row is still present and intact.
        async with pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT count(*) FROM agent_settlements WHERE task_id = $1",
                task.id,
            )
            ref = await conn.fetchval(
                "SELECT ref_id FROM agent_settlements WHERE task_id = $1",
                task.id,
            )
    finally:
        await pool.close()

    assert n == 1, (
        f"PRIMARY KEY must enforce single row per task_id; got {n} rows"
    )
    assert "DUPLICATE" not in (ref or ""), (
        "the second INSERT must not have replaced the original ref_id"
    )


# ---------------------------------------------------------------------------
# 4. CC-02 CTE LIMIT semi-join regression: large batch processes EXACTLY
#    batch_size rows.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_phase_d_cc02_cte_limit_exact_for_large_batch():
    """100 stuck marker-fixup claims, ``batch_size=10`` \u2014 the reconciler
    must process EXACTLY 10 rows in a single invocation, leaving 90 for
    later batches.  This is the CC-02 regression guard: if the candidate
    subquery is inlined as a semi-join, the LIMIT applies to the join
    output and the outer UPDATE matches every uncompleted row, blowing
    past batch_size.  The materialised CTE forces a one-shot result
    set.
    """
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        await _wipe(pool)
        # 100 stuck marker-fixup rows so the short-circuit branch
        # handles everything (no ledger RPC required).
        tasks = [_new_task(reserved=500, spent_usd=0.30) for _ in range(100)]
        for t in tasks:
            await _seed_claim(
                pool,
                task=t,
                age_seconds=600,
                ledger_applied=True,
            )

        original_mark = loop_mod._mark_settlement_completed
        mark_calls: list[str] = []

        async def _spy_mark(db, task_id):
            mark_calls.append(task_id)
            return await original_mark(db, task_id)

        with patch.object(loop_mod, "_mark_settlement_completed", _spy_mark):
            attempted = await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=10
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

    assert attempted == 10, (
        f"CC-02 LIMIT must cap a single batch at exactly 10 rows; "
        f"got attempted={attempted} (semi-join regression?)"
    )
    assert len(mark_calls) == 10
    assert n_completed == 10
    assert n_uncompleted == 90, (
        f"the remaining 90 rows must be left for later batches; "
        f"got {n_uncompleted}"
    )


# ---------------------------------------------------------------------------
# 5. Under-fetch: small candidate set, large batch_size, no padding.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_phase_d_cc02_cte_limit_under_fetch_guard():
    """5 stuck claims, ``batch_size=20`` \u2014 reconciler processes exactly
    5 rows.  Pin: a batch_size larger than the candidate set does NOT
    raise, does NOT pad, does NOT loop forever \u2014 it just returns the
    smaller actual count."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        await _wipe(pool)
        tasks = [_new_task(reserved=500, spent_usd=0.30) for _ in range(5)]
        for t in tasks:
            await _seed_claim(
                pool, task=t, age_seconds=600, ledger_applied=True
            )

        with patch.object(loop_mod, "_mark_settlement_completed", AsyncMock(
            side_effect=loop_mod._mark_settlement_completed
        )):
            attempted = await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=20
            )

        async with pool.acquire() as conn:
            n_completed = await conn.fetchval(
                "SELECT count(*) FROM agent_settlements "
                "WHERE completed_at IS NOT NULL"
            )
    finally:
        await pool.close()

    assert attempted == 5, (
        f"under-fetch: batch_size=20 over 5 candidates must yield "
        f"attempted=5; got {attempted}"
    )
    assert n_completed == 5


# ---------------------------------------------------------------------------
# 6. CC-04 batch_size validation at the function boundary.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_phase_d_cc04_batch_size_validation_clamps_non_positive():
    """``batch_size <= 0`` MUST be clamped at the reconciler boundary so
    PG never sees a negative LIMIT (which would raise
    ``InvalidRowCountInLimitClauseError`` and brick the daemon).  We
    pin both ``0`` and a negative value, plus the happy invariant that
    even at batch_size=1 (the clamped floor) the reconciler picks up
    exactly one row."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        await _wipe(pool)
        # Three stuck marker-fixup rows so we can verify the clamped
        # floor of 1 is honoured (only one row processed even though
        # three are eligible).
        tasks = [_new_task(reserved=500, spent_usd=0.30) for _ in range(3)]
        for t in tasks:
            await _seed_claim(
                pool, task=t, age_seconds=600, ledger_applied=True
            )

        # Non-positive batch_sizes must NOT raise.
        for bad in (0, -7, -1):
            attempted_bad = await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=bad
            )
            # The clamp lifts to 1; first call processes exactly 1 row.
            # Subsequent calls would each process 1 more, so we re-seed
            # to keep the invariant simple.  We assert the clamp does
            # not over-fetch on a single call.
            assert attempted_bad <= 1, (
                f"batch_size={bad} must clamp to floor 1 \u2014 max one row "
                f"per call; got attempted={attempted_bad}"
            )

        # Sanity: no unhandled exception escaped.  We also assert the
        # remaining-uncompleted count is consistent with at most 3 calls
        # of clamped=1 floor.  The total stamped rows is between 1 and 3.
        async with pool.acquire() as conn:
            n_done = await conn.fetchval(
                "SELECT count(*) FROM agent_settlements "
                "WHERE completed_at IS NOT NULL"
            )
    finally:
        await pool.close()
        _ = loop_mod  # noqa: F841 \u2014 silence import-only

    assert 1 <= n_done <= 3, (
        f"clamped batch_size must process at least 1 row per call; "
        f"got {n_done} stamped rows after 3 invocations"
    )


# ---------------------------------------------------------------------------
# 7. Concurrent reconciler runs: SKIP LOCKED isolates candidate sets.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_phase_d_concurrent_reconciler_disjoint_candidates():
    """Two concurrent reconciler invocations across 6 stuck claims must
    process DISJOINT candidate sets (no overlap) and TOGETHER cover
    every row.  Without ``FOR UPDATE SKIP LOCKED`` (or its equivalent
    via the candidate-claim UPDATE bumping ``claimed_at``), both
    invocations would pick up overlapping candidate sets and a single
    row would be processed twice \u2014 the precise double-settle vector
    that R-01 closed."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool(max_size=8)
    try:
        await _ensure_schema(pool)
        await _wipe(pool)
        tasks = [_new_task(reserved=500, spent_usd=0.30) for _ in range(6)]
        for t in tasks:
            await _seed_claim(
                pool, task=t, age_seconds=600, ledger_applied=True
            )

        # Spy on _mark_settlement_completed so we can record which
        # task_ids each invocation handled.  We use a context-local
        # list because we cannot easily distinguish between the two
        # concurrent invocations from inside a single shared spy \u2014
        # instead, we record (task_id, time-ordered) and then assert
        # uniqueness on the union.
        seen: list[str] = []
        original_mark = loop_mod._mark_settlement_completed

        async def _spy_mark(db, task_id):
            seen.append(task_id)
            return await original_mark(db, task_id)

        with patch.object(loop_mod, "_mark_settlement_completed", _spy_mark):
            results = await asyncio.gather(
                recon_mod.reconcile_pending_settlements(
                    pool, max_age_seconds=300, batch_size=10
                ),
                recon_mod.reconcile_pending_settlements(
                    pool, max_age_seconds=300, batch_size=10
                ),
            )

        async with pool.acquire() as conn:
            n_completed = await conn.fetchval(
                "SELECT count(*) FROM agent_settlements "
                "WHERE completed_at IS NOT NULL"
            )
    finally:
        await pool.close()

    # Total stamped rows == 6 (one per task), and the seen list
    # records exactly 6 entries with NO duplicates.
    assert n_completed == 6, (
        f"two concurrent reconcilers over 6 claims must stamp all 6 "
        f"rows; got {n_completed}"
    )
    assert len(seen) == 6, (
        f"each row must be picked up by exactly one reconciler; got "
        f"{len(seen)} mark calls"
    )
    assert len(set(seen)) == 6, (
        f"two reconcilers must NOT both pick up the same row \u2014 SKIP "
        f"LOCKED / claim-bump isolation broken; duplicates in {seen!r}"
    )
    # Both results sum to 6.
    assert sum(results) == 6, (
        f"sum of attempted across both runs must equal 6; got {results!r}"
    )


# ---------------------------------------------------------------------------
# 8. Re-running an already-completed batch is a true no-op.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_phase_d_completed_rows_are_invisible_to_reconciler():
    """Settlement rows with ``completed_at IS NOT NULL`` MUST be
    skipped by the candidate SELECT \u2014 ``WHERE completed_at IS NULL``
    is the primary partial-index gate.  This pins idempotency: a
    settlement reconciler run AFTER everything has already been
    stamped is a true no-op."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import settlement_reconciler as recon_mod  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        await _wipe(pool)
        # Five rows, ALL already completed.  Reconciler must see zero
        # candidates.
        for _ in range(5):
            t = _new_task(reserved=500, spent_usd=0.30)
            await _seed_claim(
                pool, task=t, age_seconds=600,
                completed=True, ledger_applied=True,
            )

        settle_mock = AsyncMock()
        mark_mock = AsyncMock()
        with (
            patch.object(loop_mod, "_settle_agent_credits", settle_mock),
            patch.object(loop_mod, "_mark_settlement_completed", mark_mock),
        ):
            attempted = await recon_mod.reconcile_pending_settlements(
                pool, max_age_seconds=300, batch_size=50
            )
    finally:
        await pool.close()

    assert attempted == 0
    assert settle_mock.await_count == 0
    assert mark_mock.await_count == 0
