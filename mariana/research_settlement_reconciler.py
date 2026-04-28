"""Y-01: background reconciler for stuck ``research_settlements`` rows.

Mirrors :mod:`mariana.agent.settlement_reconciler` (S-03 / T-01) for the
legacy investigation pipeline.  The same once-only fence applies: any
claim row with ``completed_at IS NULL`` and ``claimed_at`` older than
``max_age_seconds`` is retried.  Rows where ``ledger_applied_at IS NOT
NULL`` short-cut to a marker fix-up that stamps ``completed_at`` without
re-issuing the ledger RPC — Y-01's defense in depth against a successful
RPC followed by a transient marker write failure.

The reconciler runs once a minute alongside ``_run_daemon`` so a stuck
claim's worst-case dwell time is ``max_age_seconds + interval``.
"""

from __future__ import annotations

from typing import Any

import structlog


logger = structlog.get_logger(__name__)


async def reconcile_pending_research_settlements(
    db: Any,
    *,
    max_age_seconds: int = 300,
    batch_size: int = 50,
) -> int:
    """Retry every ``research_settlements`` claim where
    ``completed_at IS NULL`` and ``claimed_at`` is older than
    ``max_age_seconds``.

    Returns the number of rows the reconciler attempted to retry.

    Concurrency model mirrors T-01:
      * Atomic claim via ``UPDATE ... SET claimed_at = now() WHERE
        claimed_at < now() - interval`` so concurrent reconcilers see
        disjoint candidate sets.
      * Inner SELECT uses ``FOR UPDATE SKIP LOCKED``.
      * Per-row exceptions are logged and swallowed — one bad task must
        not abort the rest of the batch.

    Failure model:
      * If the candidate SELECT itself raises (DB outage), the exception
        propagates so the caller's loop log can tag the iteration as
        failed.
    """
    # CC-02: candidate selection MUST be a materialised CTE rather than an
    # inline ``WHERE task_id IN (SELECT ... LIMIT $2 ...)``.  PostgreSQL is
    # free to inline the IN-subquery as a semi-join, in which case the
    # ``LIMIT`` of the subquery applies to the *join* output rather than to
    # the candidate set — the outer UPDATE then matches every uncompleted
    # row, blowing past ``batch_size``.  See the agent-side reconciler for
    # the full diagnosis (loop6_audit/CC02_RECONCILER_LIMIT_FIX.md).
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH cands AS (
                SELECT task_id FROM research_settlements
                WHERE completed_at IS NULL
                  AND claimed_at < now() - ($1 || ' seconds')::interval
                ORDER BY claimed_at
                LIMIT $2
                FOR UPDATE SKIP LOCKED
            )
            UPDATE research_settlements SET claimed_at = now()
            WHERE task_id IN (SELECT task_id FROM cands)
            RETURNING task_id, ledger_applied_at, user_id, reserved_credits,
                      final_credits, delta_credits
            """,
            str(max_age_seconds),
            batch_size,
        )
    candidates = [
        {
            "task_id": str(r["task_id"]),
            "ledger_applied_at": r["ledger_applied_at"],
            "user_id": r["user_id"],
            "reserved_credits": int(r["reserved_credits"]),
            "final_credits": int(r["final_credits"]),
            "delta_credits": int(r["delta_credits"]),
        }
        for r in rows
    ]

    if not candidates:
        return 0

    logger.info(
        "research_settlement_reconciler_batch",
        count=len(candidates),
        max_age_seconds=max_age_seconds,
    )

    # Late import to avoid cycles between mariana.main and this module
    # at import time (mariana.main pulls in a lot of orchestrator code).
    from mariana import main as main_mod  # noqa: PLC0415

    attempted = 0
    for cand in candidates:
        task_id = cand["task_id"]

        # Y-01: marker-fixup short-circuit.  ``ledger_applied_at`` is
        # the durable proof that the ledger RPC has already succeeded
        # for this claim; the only outstanding work is stamping
        # ``completed_at``.  Re-issuing the ledger RPC here is wasteful
        # (idempotent on (ref_type, ref_id) so a worst-case replay
        # returns ``status='duplicate'`` but still consumes a round-trip
        # and emits a confusing log line).
        if cand["ledger_applied_at"] is not None:
            try:
                await main_mod._mark_research_settlement_completed(db, task_id)
                attempted += 1
                logger.info(
                    "research_settlement_reconciler_marker_fixup",
                    task_id=task_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "research_settlement_reconciler_marker_fixup_failed",
                    task_id=task_id,
                    error=str(exc),
                )
            continue

        # ledger_applied_at IS NULL → genuine ledger retry needed.
        # Reconstruct the minimal arguments required by
        # ``_deduct_user_credits``: a fake cost-tracker shaped to yield
        # the previously-recorded ``final_credits``, the original
        # ``reserved_credits``, and the user_id from the claim row.
        # ``_deduct_user_credits`` will re-issue the keyed ledger RPC
        # (idempotent on ref_id) and stamp the markers.
        try:
            from mariana.api import _get_config as _get_cfg  # noqa: PLC0415

            cfg = _get_cfg()
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "research_settlement_reconciler_config_load_failed",
                task_id=task_id,
                error=str(exc),
            )
            continue

        # Reconstruct a CostTracker-shaped object whose
        # ``total_with_markup`` divides back to the recorded final_credits.
        # ``usd_to_credits`` is what ``_deduct_user_credits`` calls;
        # passing ``final_credits / 100`` as USD reproduces the exact
        # final_tokens the original call computed.
        from decimal import Decimal  # noqa: PLC0415

        recorded_final = cand["final_credits"]

        class _ReplayCostTracker:
            total_spent = float(Decimal(recorded_final) / Decimal(120))
            total_with_markup = float(Decimal(recorded_final) / Decimal(100))

        try:
            await main_mod._deduct_user_credits(
                cand["user_id"],
                _ReplayCostTracker(),
                cfg,
                reserved_credits=cand["reserved_credits"],
                task_id=task_id,
                db=db,
            )
            attempted += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "research_settlement_reconciler_settle_failed",
                task_id=task_id,
                error=str(exc),
            )

    return attempted
