"""
mariana/main.py

Mariana Computer — autonomous financial research agent.

Entry point supporting three operational modes:

    single  (default)
        Run one investigation for the topic specified by ``--topic``.
        Blocks until the investigation completes or a budget cap is reached.
        Exits with code 0 on success, 1 on failure.

    daemon
        Poll an inbox directory (``<DATA_ROOT>/inbox/``) for ``*.task.json``
        files.  Each file must contain ``{"topic": "...", "budget": 50.0}``.
        Processed files are renamed to ``*.done``; failed files to ``*.error``.
        Gracefully shuts down on SIGINT / SIGTERM.

    dry-run
        Validate all infrastructure connections (PostgreSQL, Redis, LLM gateway)
        without running an actual investigation.  Exits 0 if everything is
        reachable, 1 otherwise.

Utility sub-commands (checked before mode dispatch):
    --status      Print the 10 most-recently created tasks and exit.
    --kill-task   Mark a task as HALTED and exit.

Usage examples:
    python -m mariana.main --topic "CATL battery margin compression 2024" --budget 30
    python -m mariana.main --mode daemon
    python -m mariana.main --dry-run
    python -m mariana.main --status
    python -m mariana.main --kill-task <task_uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from mariana.config import Config, load_config
from mariana.data.models import ResearchTask, State, TaskStatus
from mariana.util.redis_url import make_redis_client

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Structlog configuration
# ---------------------------------------------------------------------------


def _configure_logging(config: Config) -> None:
    """
    Configure structlog with the appropriate renderer.

    Uses JSON lines in production (``LOG_JSON=true``) and the pretty
    ConsoleRenderer in development.  Ties structlog into the stdlib ``logging``
    module so third-party libraries (asyncpg, httpx, uvicorn) go through the
    same pipeline.
    """
    log_level = getattr(logging, getattr(config, 'LOG_LEVEL', 'INFO').upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if getattr(config, 'LOG_JSON', True):
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Mirror to stdlib root logger so asyncpg / httpx / uvicorn logs are
    # captured in the same stream.
    logging.basicConfig(
        format="%(message)s",
        level=log_level,
        stream=sys.stdout,
    )
    logging.getLogger("asyncpg").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("weasyprint").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# DB operations (thin wrappers — real implementations in mariana.data.db)
# ---------------------------------------------------------------------------


async def _ensure_db_modules(db: Any, config: Config) -> None:
    """
    Lazily import and call ``init_schema``.

    Placed in a helper so the import error surfaces with a clear message if
    the data package has not been built yet.
    """
    try:
        from mariana.data.db import init_schema  # noqa: PLC0415
        await init_schema(db)
    except ImportError:
        logger.warning(
            "mariana.data.db not yet available — schema init skipped. "
            "Tables must exist before running investigations."
        )


async def _insert_task(db: Any, task: ResearchTask) -> None:
    """Insert a ResearchTask into the database."""
    try:
        from mariana.data.db import insert_research_task  # noqa: PLC0415
        await insert_research_task(db, task)
    except ImportError:
        # Fallback: raw asyncpg insert so the system works before data.db exists.
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO research_tasks (
                    id, topic, budget_usd, status, current_state,
                    total_spent_usd, diminishing_flags, ai_call_counter,
                    created_at, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW(), $9::jsonb)
                ON CONFLICT (id) DO NOTHING
                """,
                task.id,
                task.topic,
                task.budget_usd,
                task.status.value,
                task.current_state.value,
                task.total_spent_usd,
                task.diminishing_flags,
                task.ai_call_counter,
                json.dumps(task.metadata),
            )


async def _mark_task_failed(db: Any, task_id: str, error: str) -> None:
    """Set task status to FAILED and record the error message."""
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE research_tasks
               SET status        = $1,
                   error_message = $2,
                   completed_at  = NOW()
             WHERE id = $3
            """,
            TaskStatus.FAILED.value,
            error[:2048],
            task_id,
        )


# ---------------------------------------------------------------------------
# Graceful shutdown state
# ---------------------------------------------------------------------------


class _ShutdownFlag:
    """
    Thread-safe shutdown sentinel using threading.Event.

    Using threading.Event instead of asyncio.Event avoids the issue of
    creating an asyncio.Event at module import time (outside any running
    event loop), which raises DeprecationWarning in Python 3.10 and
    RuntimeError in Python 3.12+.

    Set by the SIGINT / SIGTERM handler.  Long-running loops check
    ``flag.is_set()`` after each iteration.
    """

    def __init__(self) -> None:
        self._flag = threading.Event()

    def set(self) -> None:
        self._flag.set()

    def is_set(self) -> bool:
        return self._flag.is_set()

    async def wait(self) -> None:
        # Poll in a non-blocking fashion to allow the event loop to yield.
        # BUG-018: Use a timeout to prevent permanent thread leaks when
        # shutdown is never signalled in long-running daemon mode.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self._flag.wait(timeout=10.0))


_SHUTDOWN = _ShutdownFlag()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Register SIGINT and SIGTERM handlers that set the shutdown flag."""

    def _handle_signal(sig: signal.Signals) -> None:
        logger.info("signal_received", signal=sig.name)
        _SHUTDOWN.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except (NotImplementedError, RuntimeError):
            # Windows and some non-asyncio runners don't support add_signal_handler.
            signal.signal(sig, lambda s, _: _handle_signal(signal.Signals(s)))


# ---------------------------------------------------------------------------
# Infrastructure creation helpers
# ---------------------------------------------------------------------------


async def _create_db_pool(config: Config) -> Any:
    """Create and return an asyncpg connection pool."""
    try:
        from mariana.data.db import create_pool  # noqa: PLC0415
        return await create_pool(
            dsn=config.POSTGRES_DSN,
            min_size=config.POSTGRES_POOL_MIN,
            max_size=config.POSTGRES_POOL_MAX,
        )
    except ImportError:
        # mariana.data.db not yet built — create pool directly.
        import asyncpg  # type: ignore[import]
        return await asyncpg.create_pool(
            dsn=config.POSTGRES_DSN,
            min_size=config.POSTGRES_POOL_MIN,
            max_size=config.POSTGRES_POOL_MAX,
            command_timeout=60.0,
        )


async def _create_redis(config: Config) -> Any:
    """Create and return a redis.asyncio client.

    W-01: routes through the validated factory so plaintext ``redis://``
    URLs to remote hosts are rejected the same way the vault and cache
    surfaces already do.
    """
    return make_redis_client(
        config.REDIS_URL,
        surface="agent_daemon",
        max_connections=20,
        socket_timeout=5.0,
        decode_responses=True,
    )


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


async def _run_dry_run(config: Config) -> int:
    """
    Test all infrastructure connections and return exit code.

    Checks:
    1. PostgreSQL  — connect and execute ``SELECT 1``.
    2. Redis       — PING.
    3. LLM gateway — GET /models with API key.

    Returns 0 if all checks pass, 1 if any fail.
    """
    all_ok = True

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    try:
        db = await _create_db_pool(config)
        async with db.acquire() as conn:
            result = await conn.fetchval("SELECT 1")
        await db.close()
        logger.info("dry_run_check", service="postgresql", status="ok", result=result)
    except Exception as exc:
        logger.error("dry_run_check", service="postgresql", status="fail", error=str(exc))
        all_ok = False

    # ── Redis ─────────────────────────────────────────────────────────────────
    try:
        redis_client = await _create_redis(config)
        pong = await redis_client.ping()
        await redis_client.aclose()
        logger.info("dry_run_check", service="redis", status="ok", pong=pong)
    except Exception as exc:
        logger.error("dry_run_check", service="redis", status="fail", error=str(exc))
        all_ok = False

    # ── LLM Gateway ───────────────────────────────────────────────────────────
    try:
        import httpx  # type: ignore[import]
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{config.LLM_GATEWAY_BASE_URL}/models",
                headers={"Authorization": f"Bearer {config.LLM_GATEWAY_API_KEY}"},
            )
        logger.info(
            "dry_run_check",
            service="llm_gateway",
            status="ok" if resp.status_code < 500 else "degraded",
            http_status=resp.status_code,
            url=config.LLM_GATEWAY_BASE_URL,
        )
    except Exception as exc:
        logger.error("dry_run_check", service="llm_gateway", status="fail", error=str(exc))
        all_ok = False

    status_str = "all_checks_passed" if all_ok else "some_checks_failed"
    logger.info("dry_run_complete", status=status_str)
    return 0 if all_ok else 1


# ---------------------------------------------------------------------------
# Status sub-command
# ---------------------------------------------------------------------------


async def _run_status(db: Any) -> None:
    """Print the 10 most-recently created research tasks."""
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, topic, status, current_state,
                   total_spent_usd, budget_usd,
                   created_at, completed_at
              FROM research_tasks
             ORDER BY created_at DESC
             LIMIT 10
            """
        )

    if not rows:
        logger.info("status_no_tasks", message="No tasks found in database.")
        return

    logger.info("status_task_list", count=len(rows))
    for row in rows:
        duration_str = ""
        if row["completed_at"] and row["created_at"]:
            secs = int((row["completed_at"] - row["created_at"]).total_seconds())
            duration_str = f"{secs // 60}m{secs % 60:02d}s"

        logger.info(
            "task",
            id=str(row["id"])[:8],
            topic=(row["topic"] or "")[:60],
            status=row["status"],
            state=row["current_state"],
            spent_usd=f"${row['total_spent_usd']:.2f}",
            budget_usd=f"${row['budget_usd']:.2f}",
            created=row["created_at"].strftime("%Y-%m-%d %H:%M") if row["created_at"] else "",
            duration=duration_str,
        )


# ---------------------------------------------------------------------------
# Kill-task sub-command
# ---------------------------------------------------------------------------


async def _run_kill_task(db: Any, task_id: str) -> None:
    """Mark a running task as HALTED."""
    async with db.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE research_tasks
               SET status = $1,
                   completed_at = NOW()
             WHERE id = $2
               AND status NOT IN ('COMPLETED', 'FAILED', 'HALTED')
            """,
            TaskStatus.HALTED.value,
            task_id,
        )
    rows_affected = int(result.split()[-1])
    if rows_affected == 0:
        logger.warning(
            "kill_task_noop",
            task_id=task_id,
            message="Task not found or already in a terminal state.",
        )
    else:
        logger.info("task_killed", task_id=task_id)


# ---------------------------------------------------------------------------
# Single investigation mode
# ---------------------------------------------------------------------------


async def _claim_research_settlement(
    db: Any,
    *,
    task_id: str,
    user_id: str,
    reserved_credits: int,
    final_credits: int,
    delta_credits: int,
    ref_id: str,
) -> str:
    """Atomically claim the right to settle this research task.

    Y-01: mirrors the agent-side ``_claim_settlement`` (R-01).
    AA-01: returns a 3-state string so the caller can distinguish the
    parent-row-gone case from a normal lost-race:

      ``"won"``    — INSERT landed, this caller owns settlement.
      ``"lost"``   — INSERT no-op'd via ``ON CONFLICT DO NOTHING``;
                     another caller already owns the claim.
      ``"orphan"`` — FK violation because the parent ``research_tasks``
                     row was deleted (typically by the user-driven
                     ``DELETE /api/investigations/{task_id}`` between
                     this daemon picking up the kill signal and reaching
                     settlement).  Caller must fall through to the
                     keyed ledger RPC directly so the user's reservation
                     refund still lands.

    Other DB errors propagate to the caller.
    """
    import asyncpg as _asyncpg  # noqa: PLC0415

    try:
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO research_settlements (
                    task_id, user_id, reserved_credits, final_credits,
                    delta_credits, ref_id
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (task_id) DO NOTHING
                RETURNING task_id
                """,
                task_id,
                user_id,
                reserved_credits,
                final_credits,
                delta_credits,
                ref_id,
            )
    except _asyncpg.exceptions.ForeignKeyViolationError:
        # AA-01: parent ``research_tasks`` row is gone — the user
        # cascaded the investigation away during the daemon's settlement
        # window.  Caller must still issue the keyed ledger RPC so the
        # reservation refund (or overrun debit) actually lands.
        return "orphan"
    return "won" if row is not None else "lost"


async def _mark_research_ledger_applied(db: Any, task_id: str) -> None:
    """Stamp ``ledger_applied_at`` on the claim row immediately after a 2xx
    ledger RPC, BEFORE attempting to stamp ``completed_at``.

    Y-01 / T-01: this is the durable proof that the ledger mutation has
    already happened.  The reconciler treats any row with
    ``ledger_applied_at IS NOT NULL`` and ``completed_at IS NULL`` as a
    marker-fixup case and does NOT re-issue the ledger RPC.
    """
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE research_settlements SET ledger_applied_at = now() "
            "WHERE task_id = $1 AND ledger_applied_at IS NULL",
            task_id,
        )


async def _mark_research_settlement_completed(db: Any, task_id: str) -> None:
    """Stamp BOTH ``ledger_applied_at`` (if NULL) and ``completed_at`` on
    the claim row.  Idempotent under the ``completed_at IS NULL`` filter.

    Also flips ``research_tasks.credits_settled`` to TRUE so a stale
    daemon resume short-circuits on the in-row flag before even touching
    the claim row.
    """
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE research_settlements "
                "SET ledger_applied_at = COALESCE(ledger_applied_at, now()), "
                "    completed_at = now() "
                "WHERE task_id = $1 AND completed_at IS NULL",
                task_id,
            )
            await conn.execute(
                "UPDATE research_tasks SET credits_settled = TRUE "
                "WHERE id = $1 AND credits_settled = FALSE",
                task_id,
            )


async def _deduct_user_credits(
    user_id: str,
    cost_tracker: Any,
    config: Config,
    reserved_credits: int = 0,
    *,
    task_id: str | None = None,
    db: Any = None,
) -> None:
    """Settle the user's final credit balance after investigation.

    If credits were reserved at submission time, only the delta versus the
    final actual cost is applied here: refund unused credits or deduct any
    overage.

    Y-01 fix: when ``task_id`` and ``db`` are provided, this function
    routes through the idempotent ``research_settlements`` claim row +
    ``grant_credits`` / ``refund_credits`` keyed on
    ``(ref_type, ref_id=task_id)``.  Same once-only fence agent-mode tasks
    enjoy under T-01.  Calls without ``task_id`` / ``db`` (legacy
    callers, including older single-mode CLI invocations) skip the claim
    row but still use the keyed idempotent RPCs so worst-case replay
    returns ``status='duplicate'`` from the ledger rather than a real
    second mutation.
    """
    if not user_id:
        return
    _api_key = getattr(config, "SUPABASE_SERVICE_KEY", "") or getattr(config, "SUPABASE_ANON_KEY", "")
    if not getattr(config, "SUPABASE_URL", "") or not _api_key:
        logger.warning("supabase_not_configured_skip_credit_deduction")
        return

    # U-02 fix: quantize to cents with ROUND_HALF_UP via the central helper
    # (`mariana.billing.precision.usd_to_credits`).  The prior code computed
    # ``int(total_with_markup * 100)`` which truncated toward zero so a
    # markup-inclusive $0.305 produced 30 credits instead of 31, and
    # inherited IEEE-754 float drift from the ``cost_tracker.total_spent``
    # accumulator.  See ``loop6_audit/U02_FIX_REPORT.md``.
    from mariana.billing.precision import usd_to_credits  # noqa: PLC0415

    total_with_markup = getattr(cost_tracker, "total_with_markup", cost_tracker.total_spent * 1.20)
    final_tokens = usd_to_credits(total_with_markup)
    delta_tokens = final_tokens - reserved_credits
    refund_tokens = abs(delta_tokens)

    # Y-01: existing-claim lookup is the entry point when we have a
    # ``task_id`` + ``db``.  Mirrors T-01's structure exactly.
    if task_id and db is not None:
        try:
            async with db.acquire() as conn:
                existing = await conn.fetchrow(
                    "SELECT completed_at, ledger_applied_at "
                    "FROM research_settlements WHERE task_id = $1",
                    task_id,
                )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.error(
                "credit_settlement_claim_lookup_failed",
                user_id=user_id,
                task_id=task_id,
                error=str(exc),
            )
            return

        if existing is not None and existing["completed_at"] is not None:
            logger.info(
                "credit_settlement_already_completed",
                user_id=user_id,
                task_id=task_id,
            )
            return

        if existing is not None and existing["ledger_applied_at"] is not None:
            try:
                await _mark_research_settlement_completed(db, task_id)
                logger.info(
                    "credit_settlement_marker_fixup",
                    user_id=user_id,
                    task_id=task_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "credit_settlement_mark_completed_failed",
                    user_id=user_id,
                    task_id=task_id,
                    error=str(exc),
                    phase="marker_fixup",
                )
            return

        if existing is None:
            try:
                claim_state = await _claim_research_settlement(
                    db,
                    task_id=task_id,
                    user_id=user_id,
                    reserved_credits=max(0, reserved_credits),
                    final_credits=max(0, final_tokens),
                    delta_credits=delta_tokens,
                    ref_id=f"research_settle:{task_id}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "credit_settlement_claim_error",
                    user_id=user_id,
                    task_id=task_id,
                    error=str(exc),
                )
                return
            if claim_state == "lost":
                # Lost the race; another caller is settling.  Re-fetch
                # and exit gracefully — they will succeed or the
                # reconciler picks up the row.
                logger.info(
                    "credit_settlement_claim_lost",
                    user_id=user_id,
                    task_id=task_id,
                )
                return
            if claim_state == "orphan":
                # AA-01: parent research_tasks row was deleted while the
                # daemon was still mid-settle (user clicked DELETE on a
                # RUNNING task).  No claim row was inserted (FK
                # violation).  We MUST still issue the keyed ledger RPC
                # so the reservation refund / overrun debit lands —
                # otherwise the user's reservation is silently lost.
                # The keyed ``(ref_type, ref_id=task_id)`` is dedupliced
                # by the live ``credit_transactions`` UNIQUE constraint
                # so a subsequent daemon retry returns ``status='duplicate'``
                # rather than minting a second mutation.  The marker
                # UPDATEs at the end of the RPC branch are skipped via
                # the ``orphan_parent`` flag because there is no claim
                # row to attach them to.
                logger.warning(
                    "credit_settlement_orphan_parent",
                    user_id=user_id,
                    task_id=task_id,
                    reserved_credits=reserved_credits,
                    final_tokens=final_tokens,
                    delta_tokens=delta_tokens,
                )
                orphan_parent = True
            else:
                orphan_parent = False
        else:
            orphan_parent = False
    else:
        # No db / task_id — legacy CLI path; treat as orphan-style for
        # marker-skip purposes (there is no row to mark anyway).
        orphan_parent = False

    if delta_tokens == 0:
        # No RPC needed.  Stamp completed_at inline so the reconciler
        # does not pick this up.  AA-01: skip the marker UPDATE on the
        # orphan-parent path — there is no research_settlements row to
        # mark.
        if task_id and db is not None and not orphan_parent:
            try:
                await _mark_research_settlement_completed(db, task_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "credit_settlement_mark_completed_failed",
                    user_id=user_id,
                    task_id=task_id,
                    error=str(exc),
                    phase="noop",
                )
        logger.info(
            "credit_settlement_noop",
            user_id=user_id,
            task_id=task_id,
            reserved_credits=reserved_credits,
            final_tokens=final_tokens,
        )
        return

    import httpx  # type: ignore[import]  # noqa: PLC0415

    headers = {
        "apikey": _api_key,
        "Authorization": f"Bearer {_api_key}",
        "Content-Type": "application/json",
    }
    rpc_succeeded = False
    # Y-01: when no task_id is available (legacy CLI / unit-test calls
    # without DB wiring), fall back to a synthetic ref_id derived from
    # user_id + final cost.  Worst-case replay would still be deduped by
    # ``credit_transactions.(ref_type, ref_id)`` but the synthetic ref
    # is unique per call so this path provides best-effort idempotency
    # only.  The DB-wired path (task_id + db) is the canonical fix.
    _ref_id = task_id or f"research_settle:{user_id}:{final_tokens}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if delta_tokens > 0:
                # Overrun — claw back via idempotent ``refund_credits``
                # keyed on (ref_type='research_task_overrun', ref_id=task_id).
                rpc_url = f"{config.SUPABASE_URL}/rest/v1/rpc/refund_credits"
                resp = await client.post(
                    rpc_url,
                    json={
                        "p_user_id": user_id,
                        "p_credits": delta_tokens,
                        "p_ref_type": "research_task_overrun",
                        "p_ref_id": _ref_id,
                    },
                    headers=headers,
                )
                if resp.status_code in (200, 204):
                    rpc_succeeded = True
                    logger.info(
                        "credit_settlement_overrun_ok",
                        user_id=user_id,
                        task_id=task_id,
                        reserved_credits=reserved_credits,
                        final_tokens=final_tokens,
                        extra_deducted=delta_tokens,
                    )
                else:
                    logger.error(
                        "credit_settlement_overrun_failed",
                        user_id=user_id,
                        task_id=task_id,
                        status=resp.status_code,
                        reserved_credits=reserved_credits,
                        final_tokens=final_tokens,
                    )
            else:
                # Refund unused reservation via idempotent ``grant_credits``
                # with source='refund' keyed on
                # (ref_type='research_task', ref_id=task_id).
                rpc_url = f"{config.SUPABASE_URL}/rest/v1/rpc/grant_credits"
                resp = await client.post(
                    rpc_url,
                    json={
                        "p_user_id": user_id,
                        "p_credits": refund_tokens,
                        "p_source": "refund",
                        "p_ref_type": "research_task",
                        "p_ref_id": _ref_id,
                    },
                    headers=headers,
                )
                if resp.status_code in (200, 204):
                    rpc_succeeded = True
                    logger.info(
                        "credit_settlement_refund_ok",
                        user_id=user_id,
                        task_id=task_id,
                        reserved_credits=reserved_credits,
                        final_tokens=final_tokens,
                        refunded=refund_tokens,
                    )
                else:
                    logger.error(
                        "credit_settlement_refund_failed",
                        user_id=user_id,
                        task_id=task_id,
                        status=resp.status_code,
                        reserved_credits=reserved_credits,
                        final_tokens=final_tokens,
                    )
    except Exception as exc:
        logger.error(
            "credit_settlement_failed",
            user_id=user_id,
            task_id=task_id,
            error=str(exc),
            reserved_credits=reserved_credits,
            final_tokens=final_tokens,
        )

    # Y-01: durable two-step finalisation, mirroring T-01.
    # 1. Stamp ``ledger_applied_at`` first so the reconciler can tell
    #    that the ledger mutation already happened even if step 2 fails.
    # 2. Stamp ``completed_at`` (and re-stamp ``ledger_applied_at`` via
    #    COALESCE) and flip ``research_tasks.credits_settled``.
    # AA-01: skip both UPDATEs on the orphan-parent path — there is no
    # research_settlements row (FK was violated) and no
    # research_tasks row (user deleted the parent).  The keyed ledger
    # mutation has already landed via the idempotent RPC; a successful
    # ``status='duplicate'`` on a future retry is the canonical
    # idempotency anchor.
    if rpc_succeeded and task_id and db is not None and not orphan_parent:
        try:
            await _mark_research_ledger_applied(db, task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "credit_settlement_mark_ledger_applied_failed",
                user_id=user_id,
                task_id=task_id,
                error=str(exc),
            )
            return
        try:
            await _mark_research_settlement_completed(db, task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "credit_settlement_mark_completed_failed",
                user_id=user_id,
                task_id=task_id,
                error=str(exc),
            )
            return
    elif rpc_succeeded and orphan_parent:
        logger.info(
            "credit_settlement_orphan_refund_ok",
            user_id=user_id,
            task_id=task_id,
            reserved_credits=reserved_credits,
            final_tokens=final_tokens,
            delta_tokens=delta_tokens,
        )


async def _run_single(
    config: Config,
    db: Any,
    redis_client: Any,
    topic: str,
    budget: float,
    user_id: str = "",
    task_id: str | None = None,
    tier: str = "standard",
    reserved_credits: int = 0,
    quality_tier: str = "balanced",
    user_flow_instructions: str = "",
    continuous_mode: bool = False,
    dont_kill_branches: bool = False,
) -> int:
    """
    Run a single investigation and return exit code.

    Creates the ResearchTask record, inserts it into the DB, runs the
    orchestrator event loop, and handles errors / final status logging.
    """
    from mariana.orchestrator.cost_tracker import CostTracker  # noqa: PLC0415

    task_metadata: dict = {"tier": tier, "reserved_credits": reserved_credits}
    if user_id:
        task_metadata["user_id"] = user_id
    if quality_tier:
        task_metadata["quality_tier"] = quality_tier
    if user_flow_instructions:
        task_metadata["user_flow_instructions"] = user_flow_instructions
    task_metadata["continuous_mode"] = continuous_mode
    task_metadata["dont_kill_branches"] = dont_kill_branches

    task = ResearchTask(
        id=task_id or str(uuid.uuid4()),
        topic=topic,
        budget_usd=budget,
        status=TaskStatus.RUNNING,
        current_state=State.INIT,
        started_at=datetime.now(tz=timezone.utc),
        metadata=task_metadata,
    )

    await _insert_task(db, task)
    logger.info(
        "investigation_start",
        task_id=task.id,
        topic=topic,
        budget_usd=budget,
        user_id=user_id or "none",
    )

    cost_tracker = CostTracker(
        task_id=task.id,
        task_budget=task.budget_usd,
        branch_hard_cap=getattr(config, "BUDGET_BRANCH_HARD_CAP", 75.0),
    )

    try:
        from mariana.orchestrator.event_loop import run as run_investigation  # noqa: PLC0415
        await run_investigation(
            task=task,
            db=db,
            redis_client=redis_client,
            config=config,
            cost_tracker=cost_tracker,
            shutdown_flag=_SHUTDOWN,
        )
        logger.info(
            "investigation_complete",
            task_id=task.id,
            total_spent_usd=cost_tracker.total_spent,
            total_with_markup_usd=cost_tracker.total_with_markup,
            total_calls=cost_tracker.call_count,
        )
        # Settle credits against the amount reserved at submission time
        await _deduct_user_credits(
            user_id, cost_tracker, config,
            reserved_credits=reserved_credits,
            task_id=task.id, db=db,
        )
        return 0

    except KeyboardInterrupt:
        logger.info("investigation_interrupted", task_id=task.id)
        # Still settle for work done before interruption
        await _deduct_user_credits(
            user_id, cost_tracker, config,
            reserved_credits=reserved_credits,
            task_id=task.id, db=db,
        )
        return 1

    except Exception as exc:
        logger.error(
            "investigation_failed",
            task_id=task.id,
            error=type(exc).__name__,
            detail=str(exc),
            exc_info=True,
        )
        await _mark_task_failed(db, task.id, f"{type(exc).__name__}: {exc}")
        # Settle credits even on failure — the cost was incurred
        await _deduct_user_credits(
            user_id, cost_tracker, config,
            reserved_credits=reserved_credits,
            task_id=task.id, db=db,
        )
        return 1


# ---------------------------------------------------------------------------
# Daemon mode
# ---------------------------------------------------------------------------


_MAX_CONCURRENT_INVESTIGATIONS: int = 4
"""Maximum number of investigations that can run concurrently in daemon mode."""


def _normalize_daemon_task_payload(task_data: Any) -> dict[str, Any]:
    """Validate and normalize daemon task-file payloads.

    Raises ``ValueError`` when the payload is malformed so callers can rename the
    offending file to ``.error`` without crashing the daemon loop.
    """
    if not isinstance(task_data, dict):
        raise ValueError("Task payload must be a JSON object")

    raw_topic = task_data.get("topic", "")
    if not isinstance(raw_topic, str):
        raise ValueError("Task payload field 'topic' must be a string")
    topic = raw_topic.strip()
    if not topic:
        raise ValueError("Task payload field 'topic' must not be empty")

    raw_budget = task_data.get("budget_usd", task_data.get("budget", 50.0))
    try:
        budget = float(raw_budget)
    except (TypeError, ValueError) as exc:
        raise ValueError("Task payload field 'budget_usd' must be numeric") from exc

    raw_reserved_credits = task_data.get("reserved_credits", 0) or 0
    try:
        reserved_credits = int(raw_reserved_credits)
    except (TypeError, ValueError) as exc:
        raise ValueError("Task payload field 'reserved_credits' must be an integer") from exc

    user_id = task_data.get("user_id", "")
    if user_id is None:
        user_id = ""
    elif not isinstance(user_id, str):
        raise ValueError("Task payload field 'user_id' must be a string")

    task_id = task_data.get("id", "")
    if task_id is None:
        task_id = ""
    elif not isinstance(task_id, str):
        raise ValueError("Task payload field 'id' must be a string")

    tier = task_data.get("tier", "standard") or "standard"
    if not isinstance(tier, str):
        raise ValueError("Task payload field 'tier' must be a string")

    quality_tier = task_data.get("quality_tier", "balanced") or "balanced"
    if not isinstance(quality_tier, str):
        raise ValueError("Task payload field 'quality_tier' must be a string")

    user_flow_instructions = task_data.get("user_flow_instructions", "") or ""
    if not isinstance(user_flow_instructions, str):
        raise ValueError("Task payload field 'user_flow_instructions' must be a string")

    return {
        "topic": topic,
        "budget": budget,
        "user_id": user_id,
        "task_id": task_id,
        "tier": tier,
        "reserved_credits": reserved_credits,
        "quality_tier": quality_tier,
        "user_flow_instructions": user_flow_instructions,
        "continuous_mode": bool(task_data.get("continuous_mode", False)),
        "dont_kill_branches": bool(task_data.get("dont_kill_branches", False)),
        "max_duration_hours": task_data.get("max_duration_hours"),
    }


async def _run_single_guarded(
    semaphore: asyncio.Semaphore,
    config: Config,
    db: Any,
    redis_client: Any,
    topic: str,
    budget: float,
    user_id: str,
    task_id: str | None,
    task_file: Path,
    tier: str = "standard",
    reserved_credits: int = 0,
    quality_tier: str = "balanced",
    user_flow_instructions: str = "",
    continuous_mode: bool = False,
    dont_kill_branches: bool = False,
) -> None:
    """Run a single investigation guarded by a concurrency semaphore.

    Renames the task file to ``.done`` or ``.error`` based on result.
    """
    async with semaphore:
        logger.info(
            "daemon_task_started",
            file=task_file.name,
            topic=topic[:60],
            budget_usd=budget,
            user_id=user_id or "none",
        )
        exit_code = await _run_single(
            config=config,
            db=db,
            redis_client=redis_client,
            topic=topic,
            budget=budget,
            user_id=user_id,
            task_id=task_id,
            tier=tier,
            reserved_credits=reserved_credits,
            quality_tier=quality_tier,
            user_flow_instructions=user_flow_instructions,
            continuous_mode=continuous_mode,
            dont_kill_branches=dont_kill_branches,
        )
        if exit_code == 0:
            task_file.rename(task_file.with_suffix(".done"))
            logger.info("daemon_task_done", file=task_file.name)
        else:
            task_file.rename(task_file.with_suffix(".error"))
            logger.warning("daemon_task_failed", file=task_file.name)


# ---------------------------------------------------------------------------
# Agent-mode queue consumer (computer tasks)
# ---------------------------------------------------------------------------

_AGENT_MAX_CONCURRENT = int(os.getenv("AGENT_MAX_CONCURRENT", "4"))


async def _run_agent_queue_daemon(db: Any, redis_client: Any) -> None:
    """Block on ``agent:queue`` and run the agent loop for each popped task id.

    Up to ``AGENT_MAX_CONCURRENT`` tasks run in parallel.  Exits when the
    shutdown flag is set.
    """
    # Local import keeps the research path independent if agent pkg missing.
    from mariana.agent.api_routes import _load_agent_task  # noqa: PLC0415
    from mariana.agent.loop import run_agent_task  # noqa: PLC0415

    logger.info("agent_queue_start", max_concurrent=_AGENT_MAX_CONCURRENT)
    sem = asyncio.Semaphore(_AGENT_MAX_CONCURRENT)
    active: set[asyncio.Task[None]] = set()

    # v3.6 recovery: re-queue any task stuck in a non-terminal state with no
    # heartbeat for >= 60 seconds.  This fixes the failure mode where the
    # orchestrator crashes / is redeployed mid-task and the task is abandoned.
    try:
        async with db.acquire() as conn:
            stuck = await conn.fetch(
                # O-02: ``stop_requested = FALSE`` excludes cancelled-but-non-
                # terminal legacy rows.  After the O-02 fix, the stop endpoint
                # finalises queued/pending tasks in-transaction (transitioning
                # to CANCELLED + settling), so this state should not exist on
                # fresh data.  The filter is belt-and-suspenders for rows
                # written by older versions of the stop path.
                "SELECT id, state FROM agent_tasks "
                "WHERE state NOT IN ('done', 'failed', 'halted', 'cancelled', 'stopped') "
                "AND stop_requested = FALSE "
                "AND updated_at < NOW() - INTERVAL '60 seconds' "
                "ORDER BY created_at ASC LIMIT 500"
            )
        for row in stuck:
            tid = str(row["id"])
            logger.warning("agent_queue_requeue_stuck", task_id=tid, state=row["state"])
            try:
                await redis_client.rpush("agent:queue", tid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent_queue_requeue_failed", task_id=tid, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_queue_recovery_failed", error=str(exc))

    async def _run_one(task_id: str) -> None:
        async with sem:
            try:
                task = await _load_agent_task(db, task_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("agent_queue_load_failed", task_id=task_id, error=str(exc))
                return
            if task is None:
                logger.warning("agent_queue_task_missing", task_id=task_id)
                return
            try:
                await run_agent_task(task, db=db, redis=redis_client)
            except Exception as exc:  # noqa: BLE001
                logger.exception("agent_queue_task_crashed", task_id=task_id)

    while not _SHUTDOWN.is_set():
        # Clean up completed futures.
        for t in {t for t in active if t.done()}:
            active.discard(t)
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent_queue_task_exception", error=str(exc))

        try:
            # BLPOP blocks for up to 5s; shutdown flag is checked between pops.
            popped = await redis_client.blpop("agent:queue", timeout=5)
        except asyncio.TimeoutError:
            # Socket-level timeout when queue is idle — expected, just retry.
            continue
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "Timeout" in msg or "timeout" in msg:
                # Redis client raises TimeoutError on idle BLPOP — silent retry.
                continue
            logger.warning("agent_queue_blpop_error", error=msg)
            await asyncio.sleep(2)
            continue
        if popped is None:
            continue
        _key, task_id = popped
        if isinstance(task_id, bytes):
            task_id = task_id.decode("utf-8")
        logger.info("agent_queue_pop", task_id=task_id)
        active.add(asyncio.create_task(_run_one(task_id), name=f"agent-{task_id[:8]}"))

    # Shutdown: wait up to 60s for in-flight tasks.
    if active:
        logger.info("agent_queue_waiting", count=len(active))
        done, pending = await asyncio.wait(active, timeout=60.0)
        for p in pending:
            p.cancel()
    logger.info("agent_queue_stopped")


# ---------------------------------------------------------------------------
# S-03: agent_settlements reconciler loop
# ---------------------------------------------------------------------------

_SETTLEMENT_RECONCILE_INTERVAL_S = int(
    os.getenv("AGENT_SETTLEMENT_RECONCILE_INTERVAL_S", "60")
)
_SETTLEMENT_RECONCILE_MAX_AGE_S = int(
    os.getenv("AGENT_SETTLEMENT_RECONCILE_MAX_AGE_S", "300")
)
_SETTLEMENT_RECONCILE_BATCH_SIZE = int(
    os.getenv("AGENT_SETTLEMENT_RECONCILE_BATCH_SIZE", "50")
)


async def _run_settlement_reconciler_loop(db: Any) -> None:
    """Periodically run :func:`reconcile_pending_settlements` so any
    ``agent_settlements`` claim with ``completed_at IS NULL`` older than
    ``AGENT_SETTLEMENT_RECONCILE_MAX_AGE_S`` (default 5 min) gets retried.

    Runs on a fixed cadence regardless of whether the previous iteration
    found work; the underlying SELECT is cheap because of the partial
    index on ``completed_at IS NULL``.
    """
    from mariana.agent.settlement_reconciler import (  # noqa: PLC0415
        reconcile_pending_settlements,
    )

    logger.info(
        "settlement_reconciler_start",
        interval_s=_SETTLEMENT_RECONCILE_INTERVAL_S,
        max_age_s=_SETTLEMENT_RECONCILE_MAX_AGE_S,
        batch_size=_SETTLEMENT_RECONCILE_BATCH_SIZE,
    )
    while True:
        try:
            await reconcile_pending_settlements(
                db,
                max_age_seconds=_SETTLEMENT_RECONCILE_MAX_AGE_S,
                batch_size=_SETTLEMENT_RECONCILE_BATCH_SIZE,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "settlement_reconciler_iteration_failed",
                error=str(exc),
            )
        await asyncio.sleep(_SETTLEMENT_RECONCILE_INTERVAL_S)


async def _run_research_settlement_reconciler_loop(db: Any) -> None:
    """Y-01: periodically reconcile stuck ``research_settlements`` rows.

    Mirrors :func:`_run_settlement_reconciler_loop` (agent settlements)
    for the legacy investigation pipeline.  Cadence and parameters are
    deliberately shared with the agent loop so operators only manage
    one knob set.
    """
    from mariana.research_settlement_reconciler import (  # noqa: PLC0415
        reconcile_pending_research_settlements,
    )

    logger.info(
        "research_settlement_reconciler_start",
        interval_s=_SETTLEMENT_RECONCILE_INTERVAL_S,
        max_age_s=_SETTLEMENT_RECONCILE_MAX_AGE_S,
        batch_size=_SETTLEMENT_RECONCILE_BATCH_SIZE,
    )
    while True:
        try:
            await reconcile_pending_research_settlements(
                db,
                max_age_seconds=_SETTLEMENT_RECONCILE_MAX_AGE_S,
                batch_size=_SETTLEMENT_RECONCILE_BATCH_SIZE,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "research_settlement_reconciler_iteration_failed",
                error=str(exc),
            )
        await asyncio.sleep(_SETTLEMENT_RECONCILE_INTERVAL_S)


async def _run_daemon(config: Config, db: Any, redis_client: Any) -> None:
    """
    Poll an inbox directory for ``.task.json`` files and run investigations.

    Supports up to ``_MAX_CONCURRENT_INVESTIGATIONS`` investigations running
    in parallel via an ``asyncio.Semaphore``. The inbox is polled every
    10 seconds; new tasks are spawned as ``asyncio.Task`` instances while
    the polling loop continues.

    File format:
    {
        "topic":        "Research topic string",
        "budget_usd":   50.0,
        "duration_hours": 2.0,
        "user_id":      "uuid",
        "id":           "task-uuid"
    }

    Processing:
    - On success: rename ``foo.task.json`` → ``foo.done``
    - On failure: rename ``foo.task.json`` → ``foo.error``

    The loop checks ``_SHUTDOWN.is_set()`` after every poll cycle.
    """
    inbox = Path(config.DATA_ROOT) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_INVESTIGATIONS)
    active_tasks: set[asyncio.Task[None]] = set()

    logger.info(
        "daemon_start",
        inbox=str(inbox),
        poll_interval_s=10,
        max_concurrent=_MAX_CONCURRENT_INVESTIGATIONS,
    )

    # ── Resume interrupted investigations from .running files ────────────
    # On container restart, any .running files represent investigations that
    # were in progress when the container was stopped.  Resume them.
    # C-04 / M-13: enforce size limits and reject symlinks that escape the inbox.
    _DAEMON_FILE_SIZE_CAP = 1_048_576  # 1 MB
    inbox_resolved = inbox.resolve()
    running_files = sorted(inbox.glob("*.running"))
    for rf in running_files:
        try:
            rf_resolved = rf.resolve()
            if not rf_resolved.is_relative_to(inbox_resolved):
                logger.warning("daemon_resume_symlink_escape", file=rf.name)
                try:
                    rf.rename(rf.with_suffix(".error"))
                except FileNotFoundError:
                    pass
                continue
            try:
                if rf_resolved.stat().st_size > _DAEMON_FILE_SIZE_CAP:
                    logger.warning("daemon_resume_oversized", file=rf.name)
                    rf.rename(rf.with_suffix(".error"))
                    continue
            except FileNotFoundError:
                continue
            resume_claim = rf.with_suffix(".resuming")
            try:
                rf.rename(resume_claim)
            except FileNotFoundError:
                logger.warning("daemon_resume_already_claimed", file=rf.name)
                continue

            # Re-check after rename (TOCTOU-safe).
            rc_resolved = resume_claim.resolve()
            if not rc_resolved.is_relative_to(inbox_resolved):
                logger.warning("daemon_resume_claim_symlink_escape", file=resume_claim.name)
                try:
                    resume_claim.rename(resume_claim.with_suffix(".error"))
                except FileNotFoundError:
                    pass
                continue
            raw_resume = rc_resolved.read_text(encoding="utf-8")
            resume_data = json.loads(raw_resume)
            normalized_resume = _normalize_daemon_task_payload(resume_data)
            topic_r = normalized_resume["topic"]
            budget_r = max(1.0, min(normalized_resume["budget"], config.BUDGET_TASK_HARD_CAP))
            user_id_r = normalized_resume["user_id"]
            task_id_r = normalized_resume["task_id"]
            tier_r = normalized_resume["tier"]
            reserved_credits_r = normalized_resume["reserved_credits"]
            quality_tier_r = normalized_resume["quality_tier"]
            user_flow_instructions_r = normalized_resume["user_flow_instructions"]
            continuous_mode_r = normalized_resume["continuous_mode"]
            dont_kill_branches_r = normalized_resume["dont_kill_branches"]
            logger.info(
                "daemon_resuming_interrupted",
                file=rf.name,
                task_id=task_id_r,
                topic=topic_r[:60],
            )
            task = asyncio.create_task(
                _run_single_guarded(
                    semaphore=semaphore,
                    config=config,
                    db=db,
                    redis_client=redis_client,
                    topic=topic_r,
                    budget=budget_r,
                    user_id=user_id_r,
                    task_id=task_id_r or None,
                    task_file=resume_claim,
                    tier=tier_r,
                    reserved_credits=reserved_credits_r,
                    quality_tier=quality_tier_r,
                    user_flow_instructions=user_flow_instructions_r,
                    continuous_mode=continuous_mode_r,
                    dont_kill_branches=dont_kill_branches_r,
                ),
                name=f"resume-{task_id_r or rf.stem}",
            )
            active_tasks.add(task)
        except Exception as exc:
            logger.error("daemon_resume_failed", file=rf.name, error=str(exc))
            failed_resume_path = locals().get("resume_claim", rf)
            try:
                failed_resume_path.rename(failed_resume_path.with_suffix(".error"))
            except FileNotFoundError:
                logger.warning("daemon_resume_error_file_missing", file=rf.name)

    if running_files:
        logger.info("daemon_resumed_tasks", count=len(active_tasks))

    while not _SHUTDOWN.is_set():
        # Clean up completed tasks
        done_tasks = {t for t in active_tasks if t.done()}
        for t in done_tasks:
            # BUG-S2-10 fix: t.exception() raises CancelledError if the task
            # was cancelled, crashing the cleanup loop.  Guard with try/except.
            try:
                exc = t.exception()
                if exc is not None:
                    logger.error(
                        "daemon_task_exception",
                        error=str(exc),
                    )
            except asyncio.CancelledError:
                logger.info("daemon_task_cancelled", task_name=t.get_name())
            active_tasks.discard(t)

        task_files = sorted(inbox.glob("*.task.json"))

        for tf in task_files:
            if _SHUTDOWN.is_set():
                break

            # C-04 fix: block symlinks that escape the inbox, and reject
            # oversized JSON (M-13) before reading. Use resolve() to fully
            # dereference any symlinks before comparing and reading.
            try:
                tf_resolved = tf.resolve()
            except OSError as exc:
                logger.error("daemon_resolve_failed", file=str(tf), error=str(exc))
                try:
                    tf.rename(tf.with_suffix(".error"))
                except FileNotFoundError:
                    pass
                continue

            if not tf_resolved.is_relative_to(inbox_resolved):
                logger.warning("daemon_symlink_escape", file=str(tf))
                try:
                    tf.rename(tf.with_suffix(".error"))
                except FileNotFoundError:
                    pass
                continue

            try:
                if tf_resolved.stat().st_size > _DAEMON_FILE_SIZE_CAP:
                    logger.warning(
                        "daemon_task_file_too_large",
                        file=str(tf),
                        size=tf_resolved.stat().st_size,
                    )
                    tf.rename(tf.with_suffix(".error"))
                    continue
            except FileNotFoundError:
                continue

            try:
                raw = tf_resolved.read_text(encoding="utf-8")
                task_data = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                logger.error(
                    "daemon_bad_task_file",
                    file=str(tf),
                    error=str(exc),
                )
                tf.rename(tf.with_suffix(".error"))
                continue

            try:
                normalized_task = _normalize_daemon_task_payload(task_data)
            except ValueError as exc:
                logger.warning(
                    "daemon_invalid_task_payload",
                    file=str(tf),
                    error=str(exc),
                )
                tf.rename(tf.with_suffix(".error"))
                continue

            topic = normalized_task["topic"]
            budget = max(1.0, min(normalized_task["budget"], config.BUDGET_TASK_HARD_CAP))
            user_id = normalized_task["user_id"]
            file_task_id = normalized_task["task_id"]
            file_tier = normalized_task["tier"]
            reserved_credits = normalized_task["reserved_credits"]
            # max_duration_hours: null/missing = unlimited (never kill prematurely)
            _max_dur = normalized_task["max_duration_hours"]  # noqa: F841  (reserved for future use)
            # User flow control fields
            file_quality_tier = normalized_task["quality_tier"]
            file_user_flow_instructions = normalized_task["user_flow_instructions"]
            file_continuous_mode = normalized_task["continuous_mode"]
            file_dont_kill_branches = normalized_task["dont_kill_branches"]
            logger.info(
                "daemon_picked_task",
                file=tf.name,
                topic=topic[:60],
                budget_usd=budget,
                user_id=user_id or "none",
                active_tasks=len(active_tasks),
            )

            # Rename to .running to prevent re-pickup on next poll
            # BUG-C1-06 fix: wrap in try/except to handle concurrent daemon
            # instances that may have already claimed this file.
            running_file = tf.with_suffix(".running")
            try:
                tf.rename(running_file)
            except FileNotFoundError:
                logger.warning("daemon_task_already_claimed", file=tf.name)
                continue

            # Spawn as a concurrent task
            task = asyncio.create_task(
                _run_single_guarded(
                    semaphore=semaphore,
                    config=config,
                    db=db,
                    redis_client=redis_client,
                    topic=topic,
                    budget=budget,
                    user_id=user_id,
                    task_id=file_task_id or None,
                    task_file=running_file,
                    tier=file_tier,
                    reserved_credits=reserved_credits,
                    quality_tier=file_quality_tier,
                    user_flow_instructions=file_user_flow_instructions,
                    continuous_mode=file_continuous_mode,
                    dont_kill_branches=file_dont_kill_branches,
                ),
                name=f"investigation-{file_task_id or tf.stem}",
            )
            active_tasks.add(task)

        # Wait 10 seconds before next poll, waking early on shutdown.
        try:
            await asyncio.wait_for(_SHUTDOWN.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            pass  # Normal — no shutdown signal yet.

    # On shutdown: wait for active tasks to checkpoint + complete (120s grace)
    # Investigations use periodic checkpointing so they can resume after restart.
    if active_tasks:
        logger.info("daemon_waiting_for_active_tasks", count=len(active_tasks))
        done, pending = await asyncio.wait(active_tasks, timeout=120.0)
        for t in pending:
            logger.warning("daemon_cancelling_task", task_name=t.get_name())
            t.cancel()

    logger.info("daemon_stopped")


# ---------------------------------------------------------------------------
# Main async entrypoint
# ---------------------------------------------------------------------------


async def _async_main() -> int:  # noqa: PLR0912  (many branches by design)
    """
    Parse arguments, configure logging, create infrastructure, and dispatch
    to the appropriate operating mode.

    Returns the process exit code.
    """
    parser = argparse.ArgumentParser(
        description="Mariana Computer — Autonomous Financial Research Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--topic",
        type=str,
        default=None,
        help="Research topic to investigate (required for single mode)",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=50.0,
        metavar="USD",
        help="Budget ceiling in USD (default: 50.0)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate infrastructure connections without running an investigation",
    )
    parser.add_argument(
        "--mode",
        choices=["single", "daemon"],
        default="single",
        help="Operational mode: single (default) or daemon",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print last 10 tasks and exit",
    )
    parser.add_argument(
        "--kill-task",
        type=str,
        default=None,
        metavar="TASK_ID",
        help="Mark task TASK_ID as HALTED and exit",
    )
    parser.add_argument(
        "--env-file",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to .env file (default: search CWD and parents)",
    )

    args = parser.parse_args()

    # Load config before structlog (config sets log_level and log_json).
    config = load_config(env_file=args.env_file)
    _configure_logging(config)

    log = logger.bind(pid=os.getpid())
    log.info("mariana_startup", mode=args.mode, dry_run=args.dry_run)

    # ── Dry-run: no DB needed ─────────────────────────────────────────────────
    if args.dry_run:
        return await _run_dry_run(config)

    # ── Infrastructure for all other modes ────────────────────────────────────
    # BUG-025: Initialize to None before try/finally to avoid UnboundLocalError
    # if _create_db_pool raises before redis_client is assigned.
    db = None
    redis_client = None
    db = await _create_db_pool(config)
    await _ensure_db_modules(db, config)
    redis_client = await _create_redis(config)

    # Install signal handlers after the event loop is running.
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop)

    exit_code = 0
    try:
        # ── Status sub-command ────────────────────────────────────────────────
        if args.status:
            await _run_status(db)

        # ── Kill-task sub-command ─────────────────────────────────────────────
        elif args.kill_task:
            await _run_kill_task(db, args.kill_task)

        # ── Daemon mode ───────────────────────────────────────────────────────
        elif args.mode == "daemon":
            # Research daemon + agent-mode queue consumer run concurrently.
            research_task = asyncio.create_task(
                _run_daemon(config=config, db=db, redis_client=redis_client),
                name="research-daemon",
            )
            agent_task = asyncio.create_task(
                _run_agent_queue_daemon(db=db, redis_client=redis_client),
                name="agent-queue",
            )
            settlement_reconciler_task = asyncio.create_task(
                _run_settlement_reconciler_loop(db=db),
                name="settlement-reconciler",
            )
            research_settlement_reconciler_task = asyncio.create_task(
                _run_research_settlement_reconciler_loop(db=db),
                name="research-settlement-reconciler",
            )
            done, pending = await asyncio.wait(
                {
                    research_task,
                    agent_task,
                    settlement_reconciler_task,
                    research_settlement_reconciler_task,
                },
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for p in pending:
                p.cancel()
            for d in done:
                try:
                    d.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    log.error("daemon_task_failed", name=d.get_name(), error=str(exc))

        # ── Single mode ───────────────────────────────────────────────────────
        elif args.topic:
            exit_code = await _run_single(
                config=config,
                db=db,
                redis_client=redis_client,
                topic=args.topic,
                budget=args.budget,
            )

        else:
            parser.print_help()
            log.warning("no_command", message="No topic or sub-command specified.")
            exit_code = 1

    finally:
        # Always clean up connections.
        # BUG-025: Guard with is not None to handle partial initialization
        if redis_client is not None:
            try:
                await redis_client.aclose()
            except Exception as exc:
                log.warning("cleanup_error", component="redis", error=str(exc))
        if db is not None:
            try:
                await db.close()
            except Exception as exc:
                log.warning("cleanup_error", component="db", error=str(exc))
        log.info("mariana_shutdown", exit_code=exit_code)

    return exit_code


# ---------------------------------------------------------------------------
# Sync entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """
    Synchronous wrapper that starts the asyncio event loop.

    Installed as the ``mariana`` console script entry point in pyproject.toml.
    """
    try:
        exit_code = asyncio.run(_async_main())
    except KeyboardInterrupt:
        # Pressed Ctrl+C before the event loop caught it.
        sys.exit(1)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
