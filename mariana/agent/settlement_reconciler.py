"""S-03: background reconciler for stuck ``agent_settlements`` rows.

The R-01 fix shipped ``agent_settlements`` with a partial index on
``completed_at IS NULL`` for "operator reconciliation surface", but no
background job ran to actually retry stalled claims.  Combined with
S-01's RPC payload bug, every settlement attempt produced a stuck row
with no automated rescue.

This module exposes :func:`reconcile_pending_settlements`, which
SELECTs uncompleted claims older than ``max_age_seconds`` via
``FOR UPDATE SKIP LOCKED`` (so concurrent reconciler runs across
processes/replicas don't double-issue RPCs for the same row), then calls
back into :func:`mariana.agent.loop._settle_agent_credits` for each
candidate.  ``_settle_agent_credits`` already knows how to treat an
existing claim with ``completed_at IS NULL`` as a retry: it re-issues
the appropriate ``add_credits`` / ``deduct_credits`` RPC and stamps
``completed_at`` on success.

Wiring: :mod:`mariana.main` schedules
:func:`reconcile_pending_settlements` once a minute alongside
``_run_agent_queue_daemon`` so a stuck claim's worst-case dwell time is
``max_age_seconds + 60s``.
"""

from __future__ import annotations

from typing import Any

import structlog


logger = structlog.get_logger(__name__)


async def _load_agent_task_from_row(db: Any, task_id: str) -> Any | None:
    """Late-import wrapper around the canonical
    :func:`mariana.agent.api_routes._load_agent_task`.

    Kept inline so the reconciler can be imported by ``mariana.main``
    without dragging in api_routes' Pydantic / FastAPI dependency tree
    during start-up tests.
    """
    from mariana.agent.api_routes import _load_agent_task  # noqa: PLC0415

    return await _load_agent_task(db, task_id)


async def reconcile_pending_settlements(
    db: Any,
    *,
    max_age_seconds: int = 300,
    batch_size: int = 50,
) -> int:
    """Retry every ``agent_settlements`` claim where ``completed_at IS NULL``
    and ``claimed_at`` is older than ``max_age_seconds``.

    Returns the number of rows the reconciler attempted to retry — useful
    for metrics / smoke tests.

    Concurrency model:
    * The candidate SELECT uses ``FOR UPDATE SKIP LOCKED`` so two
      simultaneous reconciler invocations cannot both select the same
      row.  The lock is held for the duration of the surrounding
      ``async with`` block.
    * We deliberately fetch task_ids inside the lock and drop the
      transaction *before* calling ``_settle_agent_credits`` per row.
      ``_settle_agent_credits`` opens its own short-lived connections
      for the claim re-fetch, the optional re-INSERT (which is a no-op
      under ``ON CONFLICT DO NOTHING``), and the final
      ``UPDATE ... SET completed_at = now()``.  This keeps the row
      lock window tight (microseconds) and prevents a slow Supabase
      RPC from blocking other reconciler iterations.

    Failure model:
    * Per-row exceptions are logged and swallowed — one bad task must
      not abort the rest of the batch.
    * If the entire SELECT query raises (DB outage), the exception
      propagates so the caller's ``except Exception`` log loop can
      tag the iteration as failed.
    """
    # Atomically claim the candidate rows by bumping ``claimed_at`` to now()
    # in a single UPDATE...RETURNING.  Concurrent reconcilers see disjoint
    # candidate sets because the WHERE clause filters by
    # ``claimed_at < now() - interval`` — once one process bumps the
    # timestamp, the other process's WHERE no longer matches.  This is the
    # SKIP LOCKED equivalent without the deadlock risk of holding a
    # FOR UPDATE lock across the slow ledger RPC.
    #
    # ctid is included so the UPDATE only touches rows we explicitly select
    # via the LIMIT subquery (avoids scanning the whole index).
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE agent_settlements SET claimed_at = now()
            WHERE task_id IN (
                SELECT task_id FROM agent_settlements
                WHERE completed_at IS NULL
                  AND claimed_at < now() - ($1 || ' seconds')::interval
                ORDER BY claimed_at
                LIMIT $2
                FOR UPDATE SKIP LOCKED
            )
            RETURNING task_id
            """,
            str(max_age_seconds),
            batch_size,
        )
    task_ids = [str(r["task_id"]) for r in rows]

    if not task_ids:
        return 0

    logger.info(
        "settlement_reconciler_batch",
        count=len(task_ids),
        max_age_seconds=max_age_seconds,
    )

    # Late import to dodge the agent.loop ↔ agent.settlement_reconciler
    # cycle some test importers can introduce.
    from mariana.agent import loop as loop_mod  # noqa: PLC0415

    attempted = 0
    for task_id in task_ids:
        try:
            task = await _load_agent_task_from_row(db, task_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "settlement_reconciler_load_failed",
                task_id=task_id,
                error=str(exc),
            )
            continue
        if task is None:
            logger.warning(
                "settlement_reconciler_task_missing",
                task_id=task_id,
            )
            continue
        # Force the in-memory flag back to False — the load may have read
        # it as True from a stale write while the claim row says otherwise.
        # _settle_agent_credits will set it correctly once the RPC
        # succeeds.  Without this reset the early-exit guard
        # (``if task.credits_settled: return``) would prevent the retry.
        task.credits_settled = False
        try:
            await loop_mod._settle_agent_credits(task, db=db)
            attempted += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "settlement_reconciler_settle_failed",
                task_id=task_id,
                error=str(exc),
            )

    return attempted
