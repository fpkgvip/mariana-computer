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
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from mariana.config import Config, load_config
from mariana.data.models import ResearchTask, State, TaskStatus

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
    Simple, asyncio-safe shutdown sentinel.

    Set by the SIGINT / SIGTERM handler.  Long-running loops check
    ``flag.is_set()`` after each iteration.
    """

    def __init__(self) -> None:
        self._flag = asyncio.Event()

    def set(self) -> None:
        self._flag.set()

    def is_set(self) -> bool:
        return self._flag.is_set()

    async def wait(self) -> None:
        await self._flag.wait()


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
    """Create and return a redis.asyncio client."""
    import redis.asyncio as aioredis  # type: ignore[import]
    return aioredis.from_url(
        config.REDIS_URL,
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


async def _run_single(
    config: Config,
    db: Any,
    redis_client: Any,
    topic: str,
    budget: float,
) -> int:
    """
    Run a single investigation and return exit code.

    Creates the ResearchTask record, inserts it into the DB, runs the
    orchestrator event loop, and handles errors / final status logging.
    """
    from mariana.orchestrator.cost_tracker import CostTracker  # noqa: PLC0415

    task = ResearchTask(
        id=str(uuid.uuid4()),
        topic=topic,
        budget_usd=budget,
        status=TaskStatus.RUNNING,
        current_state=State.INIT,
        started_at=datetime.utcnow(),
    )

    await _insert_task(db, task)
    logger.info(
        "investigation_start",
        task_id=task.id,
        topic=topic,
        budget_usd=budget,
    )

    cost_tracker = CostTracker(task_id=task.id, task_budget=task.budget_usd)

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
            total_calls=cost_tracker.call_count,
        )
        return 0

    except KeyboardInterrupt:
        logger.info("investigation_interrupted", task_id=task.id)
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
        return 1


# ---------------------------------------------------------------------------
# Daemon mode
# ---------------------------------------------------------------------------


async def _run_daemon(config: Config, db: Any, redis_client: Any) -> None:
    """
    Poll an inbox directory for ``.task.json`` files and run investigations.

    File format:
    {
        "topic":  "Research topic string",
        "budget": 50.0
    }

    Processing:
    - On success: rename ``foo.task.json`` → ``foo.done``
    - On failure: rename ``foo.task.json`` → ``foo.error``

    The loop checks ``_SHUTDOWN.is_set()`` after every poll cycle.
    """
    inbox = Path(config.DATA_ROOT) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    logger.info("daemon_start", inbox=str(inbox), poll_interval_s=10)

    while not _SHUTDOWN.is_set():
        task_files = sorted(inbox.glob("*.task.json"))

        for tf in task_files:
            if _SHUTDOWN.is_set():
                break

            try:
                raw = tf.read_text(encoding="utf-8")
                task_data = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                logger.error(
                    "daemon_bad_task_file",
                    file=str(tf),
                    error=str(exc),
                )
                tf.rename(tf.with_suffix(".error"))
                continue

            topic = task_data.get("topic", "").strip()
            budget = float(task_data.get("budget", 50.0))

            if not topic:
                logger.warning(
                    "daemon_empty_topic",
                    file=str(tf),
                    message="Task file has no 'topic' field; skipping.",
                )
                tf.rename(tf.with_suffix(".error"))
                continue

            budget = max(1.0, min(budget, config.BUDGET_TASK_HARD_CAP))
            logger.info(
                "daemon_picked_task",
                file=tf.name,
                topic=topic[:60],
                budget_usd=budget,
            )

            exit_code = await _run_single(
                config=config,
                db=db,
                redis_client=redis_client,
                topic=topic,
                budget=budget,
            )

            if exit_code == 0:
                tf.rename(tf.with_suffix(".done"))
                logger.info("daemon_task_done", file=tf.name)
            else:
                tf.rename(tf.with_suffix(".error"))
                logger.warning("daemon_task_failed", file=tf.name)

        # Wait 10 seconds before next poll, waking early on shutdown.
        try:
            await asyncio.wait_for(_SHUTDOWN.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            pass  # Normal — no shutdown signal yet.

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
            await _run_daemon(config=config, db=db, redis_client=redis_client)

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
        try:
            await redis_client.aclose()
        except Exception:
            pass
        try:
            await db.close()
        except Exception:
            pass
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
