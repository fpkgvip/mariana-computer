"""
mariana/api.py

FastAPI REST backend for the Mariana Computer research engine.

This module exposes every front-end–facing operation:
  • Starting and listing investigations
  • Real-time SSE log streaming
  • Branch, finding, and cost breakdowns
  • PDF report download
  • Connector health status
  • Graceful shutdown

All endpoints are async.  CORS is fully open for local development;
tighten ``allow_origins`` before a production deployment.

Startup sequence
----------------
1. ``lifespan`` loads AppConfig from the environment.
2. Creates the asyncpg connection pool and runs ``init_schema`` if needed.
3. Initialises the Redis client.
4. On shutdown, closes both connections cleanly.

Daemon-mode task submission
---------------------------
``POST /api/investigations`` writes a ``.task.json`` file to
``config.inbox_dir`` so the offline orchestrator daemon picks it up without
requiring an in-process event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import asyncpg
import structlog
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from mariana.config import AppConfig, load_config
from mariana.data.db import create_pool, init_schema

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Application version
# ---------------------------------------------------------------------------

_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Module-level singletons (populated during lifespan startup)
# ---------------------------------------------------------------------------

_config: AppConfig | None = None
_db_pool: asyncpg.Pool | None = None
_redis: Any | None = None  # aioredis.Redis


# ---------------------------------------------------------------------------
# Lifespan — startup / shutdown
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan context: init infra on startup, close on shutdown."""
    global _config, _db_pool, _redis  # noqa: PLW0603

    log = logger.bind(component="api_lifespan")
    log.info("api_startup_begin", version=_VERSION)

    # ── Config ─────────────────────────────────────────────────────────────
    _config = load_config()

    # ── Database ────────────────────────────────────────────────────────────
    try:
        _db_pool = await create_pool(
            dsn=_config.POSTGRES_DSN,
            min_size=_config.POSTGRES_POOL_MIN,
            max_size=_config.POSTGRES_POOL_MAX,
        )
        await init_schema(_db_pool)
        log.info("db_pool_ready", dsn=_config.POSTGRES_DSN.split("@")[-1])
    except Exception as exc:  # noqa: BLE001
        log.error("db_pool_failed", error=str(exc))
        _db_pool = None
        # BUG-054: Clarify degraded mode for operators
        log.info("api_running_degraded_mode", missing="database",
                 message="API started without database; most endpoints will return 503")

    # ── Redis ───────────────────────────────────────────────────────────────
    try:
        import redis.asyncio as aioredis  # type: ignore[import-not-found]

        _redis = aioredis.from_url(
            _config.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
        await _redis.ping()
        log.info("redis_ready", url=_config.REDIS_URL)
    except Exception as exc:  # noqa: BLE001
        log.warning("redis_unavailable", error=str(exc))
        _redis = None

    log.info("api_startup_complete")

    yield  # ─── application runs ───────────────────────────────────────────

    # ── Teardown ────────────────────────────────────────────────────────────
    log.info("api_shutdown_begin")
    if _db_pool is not None:
        await _db_pool.close()
        log.info("db_pool_closed")
    if _redis is not None:
        await _redis.aclose()
        log.info("redis_closed")
    log.info("api_shutdown_complete")


# ---------------------------------------------------------------------------
# App construction
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Mariana Computer API",
    version=_VERSION,
    description=(
        "REST backend for the Mariana investigative research engine. "
        "Provides endpoints to start investigations, stream real-time logs, "
        "download reports, and inspect cost breakdowns."
    ),
    lifespan=lifespan,
)

# BUG-027: CORS origins read from config so the hardcoded Vercel URL can be
# updated via environment variable without a code change.
_DEFAULT_CORS_ORIGINS = [
    "https://frontend-tau-navy-80.vercel.app",
    "http://localhost:5173",
    "http://localhost:3000",
]

def _get_cors_origins() -> list[str]:
    """Return CORS allowed origins from config, falling back to defaults."""
    if _config is not None and hasattr(_config, "CORS_ALLOWED_ORIGINS"):
        return _config.CORS_ALLOWED_ORIGINS
    extra = os.environ.get("CORS_ALLOWED_ORIGINS", "")
    if extra:
        return [o.strip() for o in extra.split(",") if o.strip()]
    return _DEFAULT_CORS_ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def _get_db() -> asyncpg.Pool:
    """Return the live DB pool or raise 503 if unavailable."""
    if _db_pool is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return _db_pool


def _get_config() -> AppConfig:
    """Return the loaded config or raise 503 if startup failed."""
    if _config is None:
        raise HTTPException(status_code=503, detail="Configuration not loaded")
    return _config


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str
    version: str


class ConfigResponse(BaseModel):
    """Sanitised config — API keys are redacted."""

    model_cheap: str
    model_medium: str
    model_expensive: str
    budget_branch_hard_cap: float
    budget_task_hard_cap: float
    score_kill_threshold: float
    score_deepen_threshold: float
    data_root: str
    log_level: str


class StartInvestigationRequest(BaseModel):
    topic: str = Field(..., min_length=3, max_length=1024, description="Research topic")
    budget_usd: float = Field(..., gt=0.0, le=500.0, description="Budget ceiling in USD")
    duration_hours: float = Field(2.0, gt=0.0, description="Maximum investigation duration in hours")


class StartInvestigationResponse(BaseModel):
    task_id: str
    status: str
    message: str


class TaskSummary(BaseModel):
    id: str
    topic: str
    budget_usd: float
    status: str
    current_state: str
    total_spent_usd: float
    ai_call_counter: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    output_pdf_path: str | None
    output_docx_path: str | None


class PaginatedTasksResponse(BaseModel):
    items: list[TaskSummary]
    total: int
    page: int
    page_size: int


class BranchSummary(BaseModel):
    id: str
    hypothesis_id: str
    status: str
    budget_allocated: float
    budget_spent: float
    cycles_completed: int
    latest_score: float | None
    created_at: datetime


class FindingSummary(BaseModel):
    id: str
    hypothesis_id: str
    content: str
    evidence_type: str
    confidence: float
    content_language: str
    is_compressed: bool
    created_at: datetime


class CostBreakdown(BaseModel):
    task_id: str
    total_spent_usd: float
    budget_usd: float
    budget_remaining_usd: float
    ai_call_count: int
    per_model: dict[str, float]
    per_branch: dict[str, float]


class ConnectorStatus(BaseModel):
    name: str
    available: bool
    api_key_set: bool
    note: str


class KillTaskResponse(BaseModel):
    task_id: str
    message: str


class ShutdownResponse(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _row_to_task_summary(row: asyncpg.Record) -> TaskSummary:
    """Convert a raw DB row to a TaskSummary Pydantic model."""
    return TaskSummary(
        id=str(row["id"]),
        topic=row["topic"],
        budget_usd=float(row["budget_usd"]),
        status=row["status"],
        current_state=row["current_state"],
        total_spent_usd=float(row["total_spent_usd"] or 0.0),
        ai_call_counter=int(row["ai_call_counter"] or 0),
        created_at=row["created_at"],
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
        output_pdf_path=row.get("output_pdf_path"),
        output_docx_path=row.get("output_docx_path"),
    )


def _row_to_branch_summary(row: asyncpg.Record) -> BranchSummary:
    """Convert a raw branches row to BranchSummary."""
    score_history = row.get("score_history") or []
    if isinstance(score_history, str):
        try:
            score_history = json.loads(score_history)
        except (json.JSONDecodeError, ValueError):
            score_history = []

    latest_score: float | None = score_history[-1] if score_history else None

    return BranchSummary(
        id=str(row["id"]),
        hypothesis_id=str(row["hypothesis_id"]),
        status=row["status"],
        budget_allocated=float(row["budget_allocated"] or 0.0),
        budget_spent=float(row["budget_spent"] or 0.0),
        cycles_completed=int(row["cycles_completed"] or 0),
        latest_score=latest_score,
        created_at=row["created_at"],
    )


def _row_to_finding_summary(row: asyncpg.Record) -> FindingSummary:
    """Convert a raw findings row to FindingSummary."""
    return FindingSummary(
        id=str(row["id"]),
        hypothesis_id=str(row["hypothesis_id"]),
        content=row["content"],
        evidence_type=row["evidence_type"],
        confidence=float(row["confidence"] or 0.5),
        content_language=row.get("content_language") or "en",
        is_compressed=bool(row.get("is_compressed", False)),
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Routes — Health / Status
# ---------------------------------------------------------------------------


@app.get("/api/health", response_model=HealthResponse, tags=["Status"])
async def health_check() -> HealthResponse:
    """Liveness probe — always returns 200 if the process is running."""
    return HealthResponse(status="ok", version=_VERSION)


@app.get("/api/config", response_model=ConfigResponse, tags=["Status"])
async def get_config() -> ConfigResponse:
    """Return sanitised runtime configuration (API keys are never exposed)."""
    cfg = _get_config()
    return ConfigResponse(
        model_cheap=cfg.MODEL_CHEAP,
        model_medium=cfg.MODEL_MEDIUM,
        model_expensive=cfg.MODEL_EXPENSIVE,
        budget_branch_hard_cap=cfg.BUDGET_BRANCH_HARD_CAP,
        budget_task_hard_cap=cfg.BUDGET_TASK_HARD_CAP,
        score_kill_threshold=cfg.SCORE_KILL_THRESHOLD,
        score_deepen_threshold=cfg.SCORE_DEEPEN_THRESHOLD,
        data_root=cfg.DATA_ROOT,
        log_level=cfg.LOG_LEVEL,
    )


# ---------------------------------------------------------------------------
# Routes — Investigations (tasks)
# ---------------------------------------------------------------------------


@app.post(
    "/api/investigations",
    response_model=StartInvestigationResponse,
    status_code=202,
    tags=["Investigations"],
)
async def start_investigation(body: StartInvestigationRequest) -> StartInvestigationResponse:
    """
    Submit a new investigation.

    Writes a ``.task.json`` file to the daemon inbox directory so the
    background orchestrator picks it up asynchronously.  Returns the
    generated ``task_id`` immediately with a 202 Accepted response.
    """
    cfg = _get_config()
    task_id = str(uuid.uuid4())
    created_at = datetime.now(tz=timezone.utc).isoformat()

    task_payload: dict[str, Any] = {
        "id": task_id,
        "topic": body.topic,
        "budget_usd": body.budget_usd,
        "duration_hours": body.duration_hours,
        "status": "PENDING",
        "created_at": created_at,
    }

    inbox = Path(cfg.inbox_dir)
    try:
        inbox.mkdir(parents=True, exist_ok=True)
        task_file = inbox / f"{task_id}.task.json"
        task_file.write_text(json.dumps(task_payload, indent=2), encoding="utf-8")
        logger.info("task_submitted", task_id=task_id, topic=body.topic[:80])
    except OSError as exc:
        logger.error("task_write_failed", error=str(exc))
        raise HTTPException(
            status_code=500,
            detail=f"Failed to write task to inbox: {exc}",
        ) from exc

    return StartInvestigationResponse(
        task_id=task_id,
        status="PENDING",
        message=f"Investigation queued. task_id={task_id}",
    )


@app.get(
    "/api/investigations",
    response_model=PaginatedTasksResponse,
    tags=["Investigations"],
)
async def list_investigations(
    page: int = Query(1, ge=1, description="1-based page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    status: str | None = Query(None, description="Filter by status (e.g. RUNNING)"),
) -> PaginatedTasksResponse:
    """List all investigations, newest first, with optional status filter."""
    db = _get_db()
    offset = (page - 1) * page_size

    if status:
        total: int = await db.fetchval(
            "SELECT COUNT(*) FROM research_tasks WHERE status = $1",
            status.upper(),
        )
        rows = await db.fetch(
            """
            SELECT id, topic, budget_usd, status, current_state,
                   total_spent_usd, ai_call_counter, created_at,
                   started_at, completed_at, output_pdf_path, output_docx_path
            FROM research_tasks
            WHERE status = $1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            """,
            status.upper(),
            page_size,
            offset,
        )
    else:
        total = await db.fetchval("SELECT COUNT(*) FROM research_tasks")
        rows = await db.fetch(
            """
            SELECT id, topic, budget_usd, status, current_state,
                   total_spent_usd, ai_call_counter, created_at,
                   started_at, completed_at, output_pdf_path, output_docx_path
            FROM research_tasks
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
            """,
            page_size,
            offset,
        )

    return PaginatedTasksResponse(
        items=[_row_to_task_summary(r) for r in rows],
        total=total or 0,
        page=page,
        page_size=page_size,
    )


@app.get(
    "/api/investigations/{task_id}",
    response_model=TaskSummary,
    tags=["Investigations"],
)
async def get_investigation(task_id: str) -> TaskSummary:
    """Retrieve full detail for a single investigation by its task_id."""
    db = _get_db()
    row = await db.fetchrow(
        """
        SELECT id, topic, budget_usd, status, current_state,
               total_spent_usd, ai_call_counter, created_at,
               started_at, completed_at, output_pdf_path, output_docx_path
        FROM research_tasks
        WHERE id = $1
        """,
        task_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    return _row_to_task_summary(row)


@app.post(
    "/api/investigations/{task_id}/kill",
    response_model=KillTaskResponse,
    tags=["Investigations"],
)
async def kill_investigation(task_id: str) -> KillTaskResponse:
    """
    Request a running investigation to halt.

    Sets the task status to HALTED and publishes a ``kill:<task_id>``
    message on Redis so the orchestrator daemon can detect the signal
    on its next loop iteration.
    """
    db = _get_db()

    # BUG-021: Atomic conditional UPDATE to avoid race condition
    result = await db.execute(
        "UPDATE research_tasks SET status = 'HALTED', completed_at = NOW() "
        "WHERE id = $1 AND status IN ('RUNNING', 'PENDING')",
        task_id,
    )
    rows_affected = int(result.split()[-1])
    if rows_affected == 0:
        exists = await db.fetchval("SELECT 1 FROM research_tasks WHERE id = $1", task_id)
        if not exists:
            raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
        raise HTTPException(status_code=409, detail="Task is already in terminal state")

    if _redis is not None:
        try:
            await _redis.publish(f"kill:{task_id}", "1")
        except Exception as exc:  # noqa: BLE001
            logger.warning("kill_publish_failed", task_id=task_id, error=str(exc))

    logger.info("task_killed", task_id=task_id)
    return KillTaskResponse(task_id=task_id, message="Kill signal sent")


# ---------------------------------------------------------------------------
# Routes — Branches
# ---------------------------------------------------------------------------


@app.get(
    "/api/investigations/{task_id}/branches",
    response_model=list[BranchSummary],
    tags=["Branches"],
)
async def list_branches(task_id: str) -> list[BranchSummary]:
    """List all research branches for a given investigation."""
    db = _get_db()
    _ensure_task_exists(await db.fetchrow(
        "SELECT id FROM research_tasks WHERE id = $1", task_id
    ), task_id)

    rows = await db.fetch(
        """
        SELECT id, hypothesis_id, task_id, status, score_history,
               budget_allocated, budget_spent, cycles_completed,
               kill_reason, created_at, updated_at
        FROM branches
        WHERE task_id = $1
        ORDER BY created_at ASC
        """,
        task_id,
    )
    return [_row_to_branch_summary(r) for r in rows]


# ---------------------------------------------------------------------------
# Routes — Findings
# ---------------------------------------------------------------------------


@app.get(
    "/api/investigations/{task_id}/findings",
    response_model=list[FindingSummary],
    tags=["Findings"],
)
async def list_findings(
    task_id: str,
    limit: int = Query(50, ge=1, le=500, description="Max findings to return"),
    evidence_type: str | None = Query(None, description="Filter by FOR / AGAINST / NEUTRAL"),
) -> list[FindingSummary]:
    """List findings (evidence items) collected for an investigation."""
    db = _get_db()
    _ensure_task_exists(await db.fetchrow(
        "SELECT id FROM research_tasks WHERE id = $1", task_id
    ), task_id)

    if evidence_type:
        rows = await db.fetch(
            """
            SELECT id, task_id, hypothesis_id, content, evidence_type,
                   confidence, content_language, is_compressed, created_at
            FROM findings
            WHERE task_id = $1 AND evidence_type = $2
            ORDER BY confidence DESC, created_at DESC
            LIMIT $3
            """,
            task_id,
            evidence_type.upper(),
            limit,
        )
    else:
        rows = await db.fetch(
            """
            SELECT id, task_id, hypothesis_id, content, evidence_type,
                   confidence, content_language, is_compressed, created_at
            FROM findings
            WHERE task_id = $1
            ORDER BY confidence DESC, created_at DESC
            LIMIT $2
            """,
            task_id,
            limit,
        )
    return [_row_to_finding_summary(r) for r in rows]


# ---------------------------------------------------------------------------
# Routes — Cost tracking
# ---------------------------------------------------------------------------


@app.get(
    "/api/investigations/{task_id}/cost",
    response_model=CostBreakdown,
    tags=["Cost"],
)
async def get_cost_breakdown(task_id: str) -> CostBreakdown:
    """Return a detailed cost breakdown for an investigation."""
    db = _get_db()
    task_row = await db.fetchrow(
        "SELECT id, budget_usd, total_spent_usd FROM research_tasks WHERE id = $1",
        task_id,
    )
    if task_row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    # Per-model breakdown
    model_rows = await db.fetch(
        """
        SELECT model_used, SUM(cost_usd) AS total_cost
        FROM ai_sessions
        WHERE task_id = $1
        GROUP BY model_used
        """,
        task_id,
    )
    per_model = {r["model_used"]: float(r["total_cost"] or 0.0) for r in model_rows}

    # Per-branch breakdown
    branch_rows = await db.fetch(
        """
        SELECT COALESCE(branch_id::text, 'none') AS branch_id,
               SUM(cost_usd) AS total_cost
        FROM ai_sessions
        WHERE task_id = $1
        GROUP BY branch_id
        """,
        task_id,
    )
    per_branch = {r["branch_id"]: float(r["total_cost"] or 0.0) for r in branch_rows}

    # Total AI call count
    call_count: int = await db.fetchval(
        "SELECT COUNT(*) FROM ai_sessions WHERE task_id = $1",
        task_id,
    ) or 0

    budget_usd = float(task_row["budget_usd"])
    total_spent = float(task_row["total_spent_usd"] or 0.0)

    return CostBreakdown(
        task_id=task_id,
        total_spent_usd=total_spent,
        budget_usd=budget_usd,
        budget_remaining_usd=max(0.0, budget_usd - total_spent),
        ai_call_count=call_count,
        per_model=per_model,
        per_branch=per_branch,
    )


# ---------------------------------------------------------------------------
# Routes — SSE log stream
# ---------------------------------------------------------------------------


@app.get(
    "/api/investigations/{task_id}/logs",
    tags=["Logs"],
    summary="Stream real-time log events via Server-Sent Events",
)
async def stream_logs(request: Request, task_id: str) -> EventSourceResponse:
    """
    Subscribe to live log events for a running investigation.

    Uses Redis pub/sub on the channel ``logs:<task_id>``.  The orchestrator
    publishes structured JSON log lines there; this endpoint re-broadcasts
    them as SSE events.

    Falls back to polling the DB ``task_logs`` table if Redis is unavailable.
    """

    async def _event_generator() -> AsyncIterator[dict[str, str]]:
        if _redis is not None:
            # ── Redis pub/sub path ──────────────────────────────────────
            pubsub = _redis.pubsub()
            await pubsub.subscribe(f"logs:{task_id}")
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=1.0,
                    )
                    if message is not None:
                        yield {"data": message.get("data", ""), "event": "log"}
                    else:
                        # BUG-006: Use _db_pool directly to avoid HTTPException inside generator
                        if _db_pool is None:
                            yield {"data": json.dumps({"error": "database_unavailable"}), "event": "error"}
                            break
                        # Periodically check if the task has reached a terminal state.
                        status_row = await _db_pool.fetchrow(
                            "SELECT status FROM research_tasks WHERE id = $1",
                            task_id,
                        )
                        if status_row is not None and status_row["status"] in (
                            "COMPLETED", "FAILED", "HALTED"
                        ):
                            yield {
                                "data": json.dumps({"task_id": task_id, "final_status": status_row["status"]}),
                                "event": "done",
                            }
                            break
                        yield {"data": json.dumps({"heartbeat": True}), "event": "ping"}
                    await asyncio.sleep(0.1)
            finally:
                await pubsub.unsubscribe(f"logs:{task_id}")
                await pubsub.aclose()
        else:
            # ── DB fallback: poll task status changes ───────────────────
            # BUG-006: Use _db_pool directly to avoid HTTPException inside generator
            if _db_pool is None:
                yield {"data": json.dumps({"error": "database_unavailable"}), "event": "error"}
                return
            db = _db_pool
            last_state: str | None = None
            while True:
                if await request.is_disconnected():
                    break
                row = await db.fetchrow(
                    "SELECT status, current_state, total_spent_usd "
                    "FROM research_tasks WHERE id = $1",
                    task_id,
                )
                if row is None:
                    yield {
                        "data": json.dumps({"error": "task_not_found"}),
                        "event": "error",
                    }
                    break
                current_state = row["current_state"]
                if current_state != last_state:
                    last_state = current_state
                    yield {
                        "data": json.dumps({
                            "task_id": task_id,
                            "status": row["status"],
                            "state": current_state,
                            "total_spent_usd": float(row["total_spent_usd"] or 0.0),
                            "ts": datetime.now(tz=timezone.utc).isoformat(),
                        }),
                        "event": "state_change",
                    }
                if row["status"] in ("COMPLETED", "FAILED", "HALTED"):
                    yield {
                        "data": json.dumps({"task_id": task_id, "final_status": row["status"]}),
                        "event": "done",
                    }
                    break
                await asyncio.sleep(2.0)

    return EventSourceResponse(_event_generator())


# ---------------------------------------------------------------------------
# Routes — Reports
# ---------------------------------------------------------------------------


@app.get(
    "/api/investigations/{task_id}/report",
    tags=["Reports"],
    summary="Download the PDF report for a completed investigation",
)
async def download_report_pdf(task_id: str) -> FileResponse:
    """
    Stream the generated PDF report for a completed investigation.

    Returns 404 if the investigation does not exist or no PDF has been
    generated yet.
    """
    db = _get_db()
    row = await db.fetchrow(
        "SELECT output_pdf_path FROM research_tasks WHERE id = $1",
        task_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    pdf_path: str | None = row["output_pdf_path"]
    if not pdf_path:
        raise HTTPException(
            status_code=404,
            detail="PDF report has not been generated yet for this task",
        )

    # Protect against path traversal: ensure the resolved path is under DATA_ROOT.
    cfg = _get_config()
    resolved = Path(pdf_path).resolve()
    data_root = Path(cfg.DATA_ROOT).resolve()
    if not resolved.is_relative_to(data_root):
        raise HTTPException(
            status_code=403,
            detail="Access denied: report path is outside the data root",
        )

    if not resolved.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"PDF file not found on disk: {pdf_path}",
        )

    filename = resolved.name
    return FileResponse(
        path=str(resolved),
        media_type="application/pdf",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get(
    "/api/investigations/{task_id}/report/docx",
    tags=["Reports"],
    summary="Download the DOCX report (future capability)",
)
async def download_report_docx(task_id: str) -> FileResponse:
    """
    Stream the generated DOCX report for a completed investigation.

    The DOCX export is not yet implemented in the report generator;
    this endpoint is reserved for future use and currently returns 404
    unless a DOCX path has been set on the task.
    """
    db = _get_db()
    row = await db.fetchrow(
        "SELECT output_docx_path FROM research_tasks WHERE id = $1",
        task_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    docx_path: str | None = row["output_docx_path"]
    if not docx_path:
        raise HTTPException(
            status_code=404,
            detail="DOCX report not available. The generator currently produces PDF only.",
        )

    # Protect against path traversal: ensure the resolved path is under DATA_ROOT.
    cfg = _get_config()
    resolved = Path(docx_path).resolve()
    data_root = Path(cfg.DATA_ROOT).resolve()
    if not resolved.is_relative_to(data_root):
        raise HTTPException(
            status_code=403,
            detail="Access denied: report path is outside the data root",
        )

    if not resolved.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"DOCX file not found on disk: {docx_path}",
        )

    filename = resolved.name
    return FileResponse(
        path=str(resolved),
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Routes — Connectors
# ---------------------------------------------------------------------------


@app.get(
    "/api/connectors",
    response_model=list[ConnectorStatus],
    tags=["Connectors"],
)
async def list_connectors() -> list[ConnectorStatus]:
    """
    Report which external data connectors are configured and available.

    ``available`` is True when the corresponding API key is non-empty.
    Full health checks (live HTTP probes) are not performed here to keep
    the endpoint fast; they run asynchronously in the orchestrator.
    """
    cfg = _get_config()

    connectors: list[ConnectorStatus] = [
        ConnectorStatus(
            name="polygon_io",
            available=bool(cfg.POLYGON_API_KEY),
            api_key_set=bool(cfg.POLYGON_API_KEY),
            note="Market data — equities, options, forex",
        ),
        ConnectorStatus(
            name="unusual_whales",
            available=bool(cfg.UNUSUAL_WHALES_API_KEY),
            api_key_set=bool(cfg.UNUSUAL_WHALES_API_KEY),
            note="Dark pool and options flow data",
        ),
        ConnectorStatus(
            name="fred",
            available=bool(cfg.FRED_API_KEY),
            api_key_set=bool(cfg.FRED_API_KEY),
            note="Federal Reserve Economic Data",
        ),
        ConnectorStatus(
            name="sec_edgar",
            available=True,  # EDGAR is a public API — no key required
            api_key_set=True,
            note="SEC EDGAR filings — no API key required",
        ),
        ConnectorStatus(
            name="llm_gateway",
            available=bool(cfg.LLM_GATEWAY_API_KEY),
            api_key_set=bool(cfg.LLM_GATEWAY_API_KEY),
            note=f"LLM inference gateway ({cfg.LLM_GATEWAY_BASE_URL})",
        ),
        ConnectorStatus(
            name="redis",
            available=_redis is not None,
            api_key_set=True,
            note="Redis pub/sub and caching layer",
        ),
        ConnectorStatus(
            name="postgresql",
            available=_db_pool is not None,
            api_key_set=True,
            note="Primary relational database (asyncpg pool)",
        ),
    ]
    return connectors


# ---------------------------------------------------------------------------
# Routes — Kill switch
# ---------------------------------------------------------------------------


@app.post(
    "/api/shutdown",
    response_model=ShutdownResponse,
    tags=["Admin"],
    summary="Gracefully shut down the API server",
)
async def graceful_shutdown(
    x_admin_key: str | None = Header(None),
) -> ShutdownResponse:
    """
    Initiate a graceful server shutdown.

    Marks all RUNNING tasks as HALTED in the DB and schedules process
    termination via ``asyncio``.  Requires X-Admin-Key header matching
    ADMIN_SECRET_KEY config value.  Use with care in production.
    """
    # BUG-009: Require admin key to prevent unauthenticated shutdown
    cfg = _get_config()
    admin_key = getattr(cfg, "ADMIN_SECRET_KEY", "")
    if admin_key and x_admin_key != admin_key:
        raise HTTPException(status_code=401, detail="Unauthorized")
    db: asyncpg.Pool | None = _db_pool
    if db is not None:
        try:
            await db.execute(
                "UPDATE research_tasks SET status = 'HALTED' WHERE status = 'RUNNING'"
            )
            logger.info("running_tasks_halted_for_shutdown")
        except Exception as exc:  # noqa: BLE001
            logger.error("shutdown_halt_failed", error=str(exc))

    # Schedule OS-level exit after a brief delay to let the response flush
    asyncio.get_running_loop().call_later(1.0, _exit_process)
    return ShutdownResponse(message="Graceful shutdown initiated")


def _exit_process() -> None:
    """Force-exit the process. Called from the event loop after shutdown.

    Uses os._exit to avoid SystemExit propagation through asyncio tasks
    (BUG-044). sys is imported at module level (BUG-060).
    """
    logger.info("api_process_exit")
    os._exit(0)  # noqa: SLF001


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SQL injection protection — column-name allowlists
# BUG-039: These API-layer allowlists overlap with _ALLOWED_TASK_COLUMNS and
# _ALLOWED_BRANCH_COLUMNS in data/db.py. The canonical source of truth is db.py;
# these API-layer sets are kept for the api.py update paths specifically.
# ---------------------------------------------------------------------------

#: Columns that may legally appear in UPDATE research_tasks SET ... queries.
_RESEARCH_TASK_UPDATABLE_COLUMNS: frozenset[str] = frozenset({
    "status",
    "current_state",
    "error_message",
    "total_spent_usd",
    "ai_call_counter",
    "diminishing_flags",
    "started_at",
    "completed_at",
    "output_pdf_path",
    "output_docx_path",
    "metadata",
})

#: Columns that may legally appear in UPDATE branches SET ... queries.
_BRANCH_UPDATABLE_COLUMNS: frozenset[str] = frozenset({
    "status",
    "budget_allocated",
    "budget_spent",
    "cycles_completed",
    "score_history",
    "kill_reason",
    "updated_at",
})


def _validate_update_columns(columns: set[str], allowlist: frozenset[str], table: str) -> None:
    """
    Raise ValueError if any column name is not in the allowlist.

    This prevents SQL injection via dynamic column-name interpolation in
    UPDATE queries built from **kwargs-style field mappings.
    """
    unknown = columns - allowlist
    if unknown:
        raise ValueError(
            f"SQL injection protection: disallowed column(s) for table '{table}': "
            + ", ".join(sorted(unknown))
        )


def _ensure_task_exists(row: asyncpg.Record | None, task_id: str) -> None:
    """Raise HTTP 404 if the task lookup returned None."""
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")


# ---------------------------------------------------------------------------
# Custom 422 handler — return structured JSON for validation errors
# ---------------------------------------------------------------------------


# BUG-037: Register on RequestValidationError (not integer 422) and return
# structured field-level errors via exc.errors() instead of str(exc).
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return a structured JSON body for Pydantic validation errors."""
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "type": "validation_error"},
    )
