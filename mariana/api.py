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
import base64
import json
import json as _json
import os
import re
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import asyncpg
import httpx
import structlog
import stripe as _stripe
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
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
# Admin constants
# ---------------------------------------------------------------------------

#: Hardcoded admin user UUID — matches the Supabase profile with role='admin'.
ADMIN_USER_ID = "a34a319e-a046-4df2-8c98-9b83f6d512a0"

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

    # ── Stripe ──────────────────────────────────────────────────────────────
    if _config.STRIPE_SECRET_KEY:
        _stripe.api_key = _config.STRIPE_SECRET_KEY
        log.info("stripe_configured")
    else:
        log.warning("stripe_not_configured", message="STRIPE_SECRET_KEY is unset; billing endpoints will error")

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
    """Return CORS allowed origins from env var, falling back to defaults.

    BUG-R3-04 fix: ``add_middleware`` is called at module load time, before
    the FastAPI lifespan context runs, so ``_config`` is always ``None`` at
    that point.  The ``_config.CORS_ALLOWED_ORIGINS`` branch was therefore
    dead code that silently dropped operator-configured origins.  Now we read
    directly from ``os.environ`` (which IS available at import time) so the
    env var is always honoured.
    """
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
    topic: str = Field(..., min_length=1, max_length=4096, description="Research topic or question")
    # All below are now optional — AI determines them if not provided
    budget_usd: float | None = Field(
        None, gt=0.0, le=10000.0, description="Budget ceiling in USD (AI-determined if omitted)"
    )
    duration_hours: float | None = Field(
        None, gt=0.0, description="Max duration in hours (AI-determined if omitted)"
    )
    plan_approved: bool = Field(False, description="Whether user has approved the research plan")
    upload_session_uuid: str | None = Field(
        None, description="Session UUID from pre-submission file uploads (from POST /api/upload)",
    )


class ClassifyRequest(BaseModel):
    """Request body for the /api/investigations/classify endpoint."""

    topic: str = Field(..., min_length=1, max_length=4096)


class ClassifyResponse(BaseModel):
    """Classification of an investigation request into a research tier."""

    tier: str  # "instant" | "standard" | "deep"
    estimated_duration_hours: float
    estimated_credits: int
    plan_summary: str  # Brief description of what Mariana will do
    requires_approval: bool  # False for instant, True for standard/deep


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
# Billing models
# ---------------------------------------------------------------------------


class PlanInfo(BaseModel):
    """Public plan descriptor returned by GET /api/plans."""

    id: str
    name: str
    price_usd_monthly: float
    credits_per_month: int
    stripe_price_id: str
    description: str
    features: list[str]


class CreateCheckoutRequest(BaseModel):
    """Request body for POST /api/billing/create-checkout."""

    plan_id: str = Field(..., description="Plan ID (researcher | professional | enterprise)")
    success_url: str = Field(..., description="Redirect URL after successful checkout")
    cancel_url: str = Field(..., description="Redirect URL if checkout is cancelled")


class CreateCheckoutResponse(BaseModel):
    """Response from POST /api/billing/create-checkout."""

    checkout_url: str
    session_id: str


class BillingPortalResponse(BaseModel):
    """Response from GET /api/billing/portal."""

    portal_url: str


# ---------------------------------------------------------------------------
# Admin models
# ---------------------------------------------------------------------------


class AdminUserSummary(BaseModel):
    """Lightweight user record for admin listing."""

    user_id: str
    email: str | None
    role: str
    credits: int
    stripe_customer_id: str | None
    subscription_plan: str | None
    subscription_status: str | None
    created_at: datetime | None


class AdminSetCreditsRequest(BaseModel):
    """Request body for POST /api/admin/users/{user_id}/credits."""

    credits: int = Field(..., ge=0, description="New absolute credits balance (use delta for increments)")
    delta: bool = Field(False, description="If True, treat credits as a delta to add/subtract")


class AdminStatsResponse(BaseModel):
    """System-wide statistics for the admin dashboard."""

    total_users: int
    total_investigations: int
    running_investigations: int
    completed_investigations: int
    failed_investigations: int
    total_credits_consumed: int
    total_spent_usd: float
    active_users_30d: int


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
# Auth dependency — Supabase JWT validation
# ---------------------------------------------------------------------------


async def _get_current_user(
    authorization: str | None = Header(None),
) -> dict[str, str]:
    """
    Validate a Supabase JWT and return basic user info.

    Decodes the JWT payload (base64url middle segment) to extract the
    ``sub`` (user_id) and ``role`` claims without performing a full
    cryptographic verification.  Full verification requires the Supabase
    JWT secret and can be added later; for now the caller is trusted
    to present a well-formed token issued by Supabase.

    Raises HTTP 401 for missing, malformed, or expired tokens.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
    token = authorization.split(" ", 1)[1]
    try:
        # JWT format: header.payload.signature  (all base64url-encoded)
        payload_b64 = token.split(".")[1]
        # Restore padding stripped during JWT encoding
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
        # BUG-S2-02 fix: check the "exp" claim so expired JWTs are rejected.
        # Without this, a stolen token works indefinitely.
        import time as _time  # noqa: PLC0415
        exp = payload.get("exp")
        if exp is not None and _time.time() > float(exp):
            raise HTTPException(status_code=401, detail="Token has expired")
        user_id: str | None = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Token missing sub claim")
        role: str = payload.get("role", "authenticated")
        return {"user_id": user_id, "role": role}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("jwt_decode_failed", error=str(exc))
        raise HTTPException(status_code=401, detail="Invalid token") from exc


async def _require_admin(
    current_user: dict[str, str] = Depends(_get_current_user),
) -> dict[str, str]:
    """Dependency that raises 403 unless the caller is the admin user."""
    if current_user["user_id"] != ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


# ---------------------------------------------------------------------------
# Billing — hardcoded plan catalogue (matches Supabase plans table)
# ---------------------------------------------------------------------------

# Stripe price IDs must be created in the Stripe dashboard and set here.
# These are placeholder IDs; replace with real ones from the Stripe dashboard.
_PLANS: list[dict[str, Any]] = [
    {
        "id": "individual",
        "name": "Individual",
        "price_usd_monthly": 299.0,
        "credits_per_month": 30_000,
        "stripe_price_id": os.environ.get("STRIPE_PRICE_INDIVIDUAL", "price_individual"),
        "description": "For individual analysts and researchers",
        "features": [
            "30,000 research credits/month",
            "Standard + Deep investigations",
            "PDF, DOCX, PPTX, XLSX report export",
            "Perplexity-powered web search",
            "Persistent memory across sessions",
            "Priority support",
        ],
    },
    {
        "id": "enterprise",
        "name": "Enterprise",
        "price_usd_monthly": 3999.0,
        "credits_per_month": 500_000,
        "stripe_price_id": os.environ.get("STRIPE_PRICE_ENTERPRISE", "price_enterprise"),
        "description": "For large organisations with heavy research workloads",
        "features": [
            "500,000 research credits/month",
            "All investigation tiers incl. Flagship",
            "Concurrent investigations (up to 4)",
            "Sub-agent delegation",
            "Custom skills",
            "Image & video generation",
            "Dedicated queue",
            "Custom integrations",
            "Dedicated account manager",
            "SLA-backed support",
        ],
    },
]

#: Tier-to-credit cost mapping used by the classification heuristic.
#: At $0.01/credit, these map to: instant=$0.10, standard=$5, deep=$20.
#: Minimum budgets: standard=$5, deep=$20 per the architecture spec.
_TIER_CREDITS: dict[str, int] = {
    "instant": 5,
    "quick": 50,
    "standard": 500,
    "deep": 2000,
}

#: Credits-to-USD ratio (1 credit = $0.01 USD)
_CREDIT_USD_RATE: float = 0.01


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
    "/api/investigations/classify",
    response_model=ClassifyResponse,
    tags=["Investigations"],
    summary="Classify a research request into a tier",
)
async def classify_request(body: ClassifyRequest) -> ClassifyResponse:
    """
    Classify a research topic into a tier (instant / standard / deep)
    and return estimated duration, credits, and a plan summary.

    Uses a deterministic heuristic — no LLM call required.
    The frontend should call this before submitting an investigation so
    the user can approve or adjust the plan.
    """
    return _classify_topic(body.topic)


@app.post(
    "/api/investigations",
    response_model=StartInvestigationResponse,
    status_code=202,
    tags=["Investigations"],
)
async def start_investigation(
    body: StartInvestigationRequest,
    current_user: dict[str, str] = Depends(_get_current_user),
) -> StartInvestigationResponse:
    """
    Submit a new investigation.

    Requires a valid Supabase JWT in the Authorization header.
    If budget_usd or duration_hours are omitted the endpoint classifies
    the topic automatically and fills in AI-determined values.

    Writes a ``.task.json`` file to the daemon inbox directory so the
    background orchestrator picks it up asynchronously.  Returns the
    generated ``task_id`` immediately with a 202 Accepted response.
    """
    cfg = _get_config()
    task_id = str(uuid.uuid4())
    created_at = datetime.now(tz=timezone.utc).isoformat()

    # ── Fill in AI-determined values when the caller omits them ─────────────
    classification = _classify_topic(body.topic)

    effective_duration_hours: float = (
        body.duration_hours
        if body.duration_hours is not None
        else classification.estimated_duration_hours
    )
    effective_budget_usd: float = (
        body.budget_usd
        if body.budget_usd is not None
        else float(classification.estimated_credits) * _CREDIT_USD_RATE
    )

    # ── Pre-flight credit balance check ───────────────────────────────────
    # estimated_credits_needed = budget_usd * 120 (real cost → tokens at
    # $0.01/token, with 20% markup: budget * 100 * 1.20 = budget * 120)
    estimated_credits_needed = int(effective_budget_usd * 120)
    user_tokens = await _supabase_get_user_tokens(current_user["user_id"], cfg)
    if user_tokens is not None and user_tokens < estimated_credits_needed:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Insufficient credits: you have {user_tokens} tokens but this "
                f"investigation requires an estimated {estimated_credits_needed} tokens "
                f"(budget ${effective_budget_usd:.2f} + 20% markup). "
                "Please add credits or reduce the budget."
            ),
        )

    # ── Move pending uploads to task directory ──────────────────────────────
    uploaded_file_names: list[str] = []
    if body.upload_session_uuid:
        pending_dir = Path(cfg.DATA_ROOT) / "uploads" / "pending" / body.upload_session_uuid
        if pending_dir.is_dir():
            task_upload_dir = Path(cfg.DATA_ROOT) / "uploads" / task_id
            task_upload_dir.mkdir(parents=True, exist_ok=True)
            import shutil
            for f in pending_dir.iterdir():
                if f.is_file():
                    dest = task_upload_dir / f.name
                    shutil.move(str(f), str(dest))
                    uploaded_file_names.append(f.name)
            # Clean up empty pending directory
            try:
                pending_dir.rmdir()
            except OSError:
                pass
            logger.info(
                "pending_uploads_moved",
                session_uuid=body.upload_session_uuid,
                task_id=task_id,
                files=uploaded_file_names,
            )

    task_payload: dict[str, Any] = {
        "id": task_id,
        "topic": body.topic,
        "budget_usd": effective_budget_usd,
        "duration_hours": effective_duration_hours,
        "max_duration_hours": None,  # null = unlimited; only set if user explicitly chooses a limit
        "status": "PENDING",
        "created_at": created_at,
        # Adaptive-mode metadata
        "tier": classification.tier,
        "plan_approved": body.plan_approved,
        "user_id": current_user["user_id"],
        "estimated_credits": classification.estimated_credits,
        "uploaded_files": uploaded_file_names,
    }

    inbox = Path(cfg.inbox_dir)
    try:
        inbox.mkdir(parents=True, exist_ok=True)
        task_file = inbox / f"{task_id}.task.json"
        task_file.write_text(_json.dumps(task_payload, indent=2), encoding="utf-8")
        logger.info(
            "task_submitted",
            task_id=task_id,
            topic=body.topic[:80],
            tier=classification.tier,
            user_id=current_user["user_id"],
        )
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
    current_user: dict[str, str] = Depends(_get_current_user),
) -> PaginatedTasksResponse:
    """List investigations owned by the authenticated user.

    BUG-S2-11 fix: Previously unauthenticated and returned ALL investigations.
    Now requires auth and filters by user_id from the JWT.
    Admin users see all investigations via /api/admin/investigations.
    """
    db = _get_db()
    offset = (page - 1) * page_size
    user_id = current_user["user_id"]

    if status:
        total: int = await db.fetchval(
            "SELECT COUNT(*) FROM research_tasks WHERE status = $1 AND metadata->>'user_id' = $2",
            status.upper(),
            user_id,
        )
        rows = await db.fetch(
            """
            SELECT id, topic, budget_usd, status, current_state,
                   total_spent_usd, ai_call_counter, created_at,
                   started_at, completed_at, output_pdf_path, output_docx_path
            FROM research_tasks
            WHERE status = $1 AND metadata->>'user_id' = $2
            ORDER BY created_at DESC
            LIMIT $3 OFFSET $4
            """,
            status.upper(),
            user_id,
            page_size,
            offset,
        )
    else:
        total = await db.fetchval(
            "SELECT COUNT(*) FROM research_tasks WHERE metadata->>'user_id' = $1",
            user_id,
        )
        rows = await db.fetch(
            """
            SELECT id, topic, budget_usd, status, current_state,
                   total_spent_usd, ai_call_counter, created_at,
                   started_at, completed_at, output_pdf_path, output_docx_path
            FROM research_tasks
            WHERE metadata->>'user_id' = $1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            """,
            user_id,
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
async def get_investigation(
    task_id: str,
    current_user: dict[str, str] = Depends(_get_current_user),
) -> TaskSummary:
    """Retrieve full detail for a single investigation by its task_id.

    BUG-S2-12 fix: Added auth — only the investigation owner or admin can view.
    """
    db = _get_db()
    row = await db.fetchrow(
        """
        SELECT id, topic, budget_usd, status, current_state,
               total_spent_usd, ai_call_counter, created_at,
               started_at, completed_at, output_pdf_path, output_docx_path,
               metadata
        FROM research_tasks
        WHERE id = $1
        """,
        task_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    # Verify ownership
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    task_user_id = metadata.get("user_id", "")
    if task_user_id and task_user_id != current_user["user_id"] and current_user["user_id"] != ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="You do not own this investigation")
    return _row_to_task_summary(row)


@app.post(
    "/api/investigations/{task_id}/kill",
    response_model=KillTaskResponse,
    tags=["Investigations"],
)
async def kill_investigation(
    task_id: str,
    current_user: dict[str, str] = Depends(_get_current_user),
) -> KillTaskResponse:
    """
    Request a running investigation to halt.

    Sets the task status to HALTED and publishes a ``kill:<task_id>``
    message on Redis so the orchestrator daemon can detect the signal
    on its next loop iteration.
    """
    db = _get_db()

    # BUG-S3-01 fix: Verify ownership before allowing kill.
    row = await db.fetchrow(
        "SELECT metadata FROM research_tasks WHERE id = $1",
        task_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    task_user_id = metadata.get("user_id", "")
    if task_user_id and task_user_id != current_user["user_id"] and current_user["user_id"] != ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="You do not own this investigation")

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
async def stream_logs(
    request: Request,
    task_id: str,
    format: str | None = Query(None, description="Set to 'legacy' for plain text events"),
) -> EventSourceResponse:
    """
    Subscribe to live log events for a running investigation.

    Uses Redis pub/sub on the channel ``logs:<task_id>``.  The orchestrator
    publishes structured JSON log lines there; this endpoint re-broadcasts
    them as SSE events.

    Falls back to polling the DB ``task_logs`` table if Redis is unavailable.
    """

    use_legacy = format == "legacy"

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
                        raw_data = message.get("data", "")
                        if use_legacy:
                            # Convert structured JSON events to plain text
                            try:
                                evt = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
                                if isinstance(evt, dict):
                                    evt_type = evt.get("type", "")
                                    if evt_type == "text":
                                        raw_data = evt.get("content", raw_data)
                                    elif evt_type == "status_change":
                                        raw_data = f"[{evt.get('state', '')}] {evt.get('message', '')}"
                                    elif evt_type == "cost_update":
                                        raw_data = f"Cost: ${evt.get('spent_usd', 0):.4f} / ${evt.get('budget_usd', 0):.2f}"
                                    elif evt_type == "file_attached":
                                        raw_data = f"File: {evt.get('filename', '')} ({evt.get('size', 0)} bytes)"
                            except (json.JSONDecodeError, ValueError):
                                pass  # Use raw_data as-is
                        yield {"data": raw_data, "event": "log"}
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
# Routes — File Attachments (AI → User)
# ---------------------------------------------------------------------------


class FileAttachmentInfo(BaseModel):
    """Metadata for an investigation artifact file."""

    filename: str
    size: int
    mime: str
    created_at: str


_MIME_MAP: dict[str, str] = {
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".html": "text/html",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".txt": "text/plain",
}


@app.get(
    "/api/investigations/{task_id}/files",
    response_model=list[FileAttachmentInfo],
    tags=["Files"],
    summary="List all artifact files for an investigation",
)
async def list_investigation_files(
    task_id: str,
    current_user: dict[str, str] = Depends(_get_current_user),
) -> list[FileAttachmentInfo]:
    """List all files (analysis, data, snapshots) produced by an investigation."""
    cfg = _get_config()
    db = _get_db()

    # Verify user owns the investigation
    row = await db.fetchrow(
        "SELECT metadata FROM research_tasks WHERE id = $1",
        task_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    task_user_id = metadata.get("user_id", "")
    if task_user_id and task_user_id != current_user["user_id"] and current_user["user_id"] != ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="You do not own this investigation")

    files_dir = Path(cfg.DATA_ROOT) / "files" / task_id
    if not files_dir.is_dir():
        return []

    result: list[FileAttachmentInfo] = []
    for f in sorted(files_dir.iterdir()):
        if f.is_file():
            stat = f.stat()
            suffix = f.suffix.lower()
            result.append(FileAttachmentInfo(
                filename=f.name,
                size=stat.st_size,
                mime=_MIME_MAP.get(suffix, "application/octet-stream"),
                created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            ))
    return result


@app.get(
    "/api/investigations/{task_id}/files/{filename:path}",
    tags=["Files"],
    summary="Download a specific artifact file",
)
async def download_investigation_file(
    task_id: str,
    filename: str,
    current_user: dict[str, str] = Depends(_get_current_user),
) -> FileResponse:
    """Download a specific file from an investigation's artifacts."""
    cfg = _get_config()
    db = _get_db()

    # Verify user owns the investigation
    row = await db.fetchrow(
        "SELECT metadata FROM research_tasks WHERE id = $1",
        task_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    task_user_id = metadata.get("user_id", "")
    if task_user_id and task_user_id != current_user["user_id"] and current_user["user_id"] != ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="You do not own this investigation")

    files_dir = Path(cfg.DATA_ROOT) / "files" / task_id
    file_path = (files_dir / filename).resolve()

    # Path traversal protection
    data_root = Path(cfg.DATA_ROOT).resolve()
    if not file_path.is_relative_to(data_root):
        raise HTTPException(status_code=403, detail="Access denied: path outside data root")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"File {filename!r} not found")

    suffix = file_path.suffix.lower()
    mime = _MIME_MAP.get(suffix, "application/octet-stream")
    return FileResponse(
        path=str(file_path),
        media_type=mime,
        filename=file_path.name,
        headers={"Content-Disposition": f'attachment; filename="{file_path.name}"'},
    )


# ---------------------------------------------------------------------------
# Routes — File Uploads (User → Server)
# ---------------------------------------------------------------------------

_UPLOAD_MAX_FILE_SIZE: int = 10 * 1024 * 1024  # 10 MB
_UPLOAD_MAX_FILES_PER_INVESTIGATION: int = 5
_UPLOAD_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".txt", ".md", ".csv", ".json", ".html",
    ".png", ".jpg", ".xlsx", ".docx",
})

_UPLOAD_MIME_MAP: dict[str, str] = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".html": "text/html",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class UploadedFileInfo(BaseModel):
    """Metadata for an uploaded file."""

    filename: str
    size: int
    content_type: str


class UploadResponse(BaseModel):
    """Response from a file upload."""

    files: list[UploadedFileInfo]
    task_id: str | None = None
    session_uuid: str | None = None


@app.post(
    "/api/investigations/{task_id}/upload",
    response_model=UploadResponse,
    tags=["Uploads"],
    summary="Upload files to an existing investigation",
)
async def upload_investigation_files(
    task_id: str,
    files: list[UploadFile] = File(...),
    current_user: dict[str, str] = Depends(_get_current_user),
) -> UploadResponse:
    """Upload files to an existing investigation.

    Max 10MB per file, max 5 files per investigation.
    Supported types: .pdf, .txt, .md, .csv, .json, .html, .png, .jpg, .xlsx, .docx
    """
    cfg = _get_config()
    db = _get_db()

    # Verify user owns the investigation
    row = await db.fetchrow(
        "SELECT metadata FROM research_tasks WHERE id = $1",
        task_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    task_user_id = metadata.get("user_id", "")
    if task_user_id and task_user_id != current_user["user_id"] and current_user["user_id"] != ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="You do not own this investigation")

    if len(files) > _UPLOAD_MAX_FILES_PER_INVESTIGATION:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {_UPLOAD_MAX_FILES_PER_INVESTIGATION} files per upload",
        )

    upload_dir = Path(cfg.DATA_ROOT) / "uploads" / task_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    # Check existing file count
    existing_count = sum(1 for f in upload_dir.iterdir() if f.is_file())
    if existing_count + len(files) > _UPLOAD_MAX_FILES_PER_INVESTIGATION:
        raise HTTPException(
            status_code=400,
            detail=f"Investigation already has {existing_count} files; max is {_UPLOAD_MAX_FILES_PER_INVESTIGATION}",
        )

    uploaded: list[UploadedFileInfo] = []
    for upload_file in files:
        filename = upload_file.filename or "untitled"
        suffix = Path(filename).suffix.lower()

        if suffix not in _UPLOAD_ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"File type {suffix!r} not supported. Allowed: {', '.join(sorted(_UPLOAD_ALLOWED_EXTENSIONS))}",
            )

        content = await upload_file.read()
        if len(content) > _UPLOAD_MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File {filename!r} exceeds {_UPLOAD_MAX_FILE_SIZE // (1024*1024)}MB limit",
            )

        # Sanitize filename (keep only safe characters)
        safe_name = re.sub(r"[^\w\-.]", "_", filename)
        dest = upload_dir / safe_name
        dest.write_bytes(content)

        uploaded.append(UploadedFileInfo(
            filename=safe_name,
            size=len(content),
            content_type=_UPLOAD_MIME_MAP.get(suffix, "application/octet-stream"),
        ))

    logger.info(
        "files_uploaded",
        task_id=task_id,
        count=len(uploaded),
        user_id=current_user["user_id"],
    )
    return UploadResponse(files=uploaded, task_id=task_id)


@app.post(
    "/api/upload",
    response_model=UploadResponse,
    tags=["Uploads"],
    summary="Upload files before creating an investigation",
)
async def upload_pending_files(
    files: list[UploadFile] = File(...),
    current_user: dict[str, str] = Depends(_get_current_user),
) -> UploadResponse:
    """Upload files before an investigation exists (pre-submission).

    Files are saved to a temporary pending directory keyed by a session UUID.
    When the investigation is created, these files can be moved to the
    investigation's upload directory.
    """
    cfg = _get_config()

    if len(files) > _UPLOAD_MAX_FILES_PER_INVESTIGATION:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {_UPLOAD_MAX_FILES_PER_INVESTIGATION} files per upload",
        )

    session_uuid = str(uuid.uuid4())
    pending_dir = Path(cfg.DATA_ROOT) / "uploads" / "pending" / session_uuid
    pending_dir.mkdir(parents=True, exist_ok=True)

    uploaded: list[UploadedFileInfo] = []
    for upload_file in files:
        filename = upload_file.filename or "untitled"
        suffix = Path(filename).suffix.lower()

        if suffix not in _UPLOAD_ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"File type {suffix!r} not supported. Allowed: {', '.join(sorted(_UPLOAD_ALLOWED_EXTENSIONS))}",
            )

        content = await upload_file.read()
        if len(content) > _UPLOAD_MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File {filename!r} exceeds {_UPLOAD_MAX_FILE_SIZE // (1024*1024)}MB limit",
            )

        safe_name = re.sub(r"[^\w\-.]", "_", filename)
        dest = pending_dir / safe_name
        dest.write_bytes(content)

        uploaded.append(UploadedFileInfo(
            filename=safe_name,
            size=len(content),
            content_type=_UPLOAD_MIME_MAP.get(suffix, "application/octet-stream"),
        ))

    logger.info(
        "pending_files_uploaded",
        session_uuid=session_uuid,
        count=len(uploaded),
        user_id=current_user["user_id"],
    )
    return UploadResponse(files=uploaded, session_uuid=session_uuid)


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
# Classification heuristic — pure function, no I/O
# ---------------------------------------------------------------------------


def _classify_topic(topic: str) -> ClassifyResponse:
    """
    Deterministic tier classification for a research topic.

    Rules (evaluated in order, first match wins):
    1. Explicit duration keywords ("3 hours", "2 days") — honour the stated
       duration; tier is deep if > 2 h, standard otherwise.
    2. Deep-tier signals: "flagship", "exhaustive", "multi-day", etc.
    3. Instant-tier: greetings, simple tests, trivial messages.
    4. Quick-tier: short questions, simple lookups, single-fact queries.
    5. Standard-tier: moderate research requiring structured analysis.
    6. Default: quick (safe default — avoids overkill on simple tasks).
    """
    topic_lower = topic.lower().strip()
    word_count = len(topic_lower.split())

    # ── 1. Parse explicit duration mentions ──────────────────────────────────
    explicit_hours: float | None = None
    duration_match = re.search(
        r"(\d+(?:\.\d+)?)\s*(hour|hr|day|days|hours|hrs)",
        topic_lower,
    )
    if duration_match:
        value = float(duration_match.group(1))
        unit = duration_match.group(2)
        explicit_hours = value * 24.0 if unit.startswith("day") else value

    if explicit_hours is not None:
        if explicit_hours > 2.0:
            tier = "deep"
            credits = _TIER_CREDITS["deep"]
            summary = (
                f"Deep investigation over {explicit_hours:.1f} hours: multi-angle analysis, "
                "exhaustive source coverage, tribunal review, and a full written report."
            )
        else:
            tier = "standard"
            credits = _TIER_CREDITS["standard"]
            summary = (
                f"Standard investigation ({explicit_hours:.1f} h): structured evidence "
                "gathering, hypothesis testing, and a concise report."
            )
        return ClassifyResponse(
            tier=tier,
            estimated_duration_hours=explicit_hours,
            estimated_credits=credits,
            plan_summary=summary,
            requires_approval=True,
        )

    # ── 2. Deep-tier signal words ───────────────────────────────────────
    deep_signals = {"flagship", "exhaustive", "multi-day", "full analysis",
                    "deep dive", "deep research", "thorough investigation"}
    if any(signal in topic_lower for signal in deep_signals):
        return ClassifyResponse(
            tier="deep",
            estimated_duration_hours=24.0,
            estimated_credits=_TIER_CREDITS["deep"],
            plan_summary=(
                "Flagship deep investigation: exhaustive multi-day research spanning all "
                "available data sources, tribunal adversarial review, and a publication-grade report."
            ),
            requires_approval=True,
        )

    # ── 3. Instant-tier: greetings, tests, trivial messages ────────────
    greeting_patterns = {
        "hello", "hi", "hey", "test", "ping", "yo", "sup",
        "good morning", "good afternoon", "good evening",
        "are you there", "are you alive", "are you live",
        "are you working", "who are you", "what are you",
        "thanks", "thank you", "ok", "okay", "cool", "nice",
    }
    # Check if the whole message is basically a greeting/test
    # BUG-S5-03 fix: word_count <= 3 was too aggressive — "What is CATL" (3 words)
    # is a real query, not a greeting.  Only use word_count <= 2 for the auto-instant
    # bucket ("hello", "hi there", "ok"), and rely on the greeting_patterns set for
    # exact matches of 3-word greetings like "are you there".
    topic_stripped = topic_lower.rstrip(".!?,")
    if topic_stripped in greeting_patterns or word_count <= 2:
        return ClassifyResponse(
            tier="instant",
            estimated_duration_hours=0.01,
            estimated_credits=_TIER_CREDITS["instant"],
            plan_summary="Quick response to your message.",
            requires_approval=False,
        )
    # Also catch "hello, let me test if you are live" style messages
    if any(g in topic_lower for g in ("hello", "hi ", "hey ", "test")) and word_count < 15:
        if not any(kw in topic_lower for kw in ("research", "analyze", "investigate", "report", "find")):
            return ClassifyResponse(
                tier="instant",
                estimated_duration_hours=0.01,
                estimated_credits=_TIER_CREDITS["instant"],
                plan_summary="Quick response to your message.",
                requires_approval=False,
            )

    # ── 4. Quick-tier: short questions, simple lookups ──────────────────
    is_short = len(topic) < 200
    has_question = "?" in topic
    # Research-demanding keywords that push toward standard tier
    research_keywords = {
        "investigate", "research", "analyze", "analysis", "report",
        "compare", "evaluate", "comprehensive", "in-depth", "detailed",
        "thesis", "paper", "study", "survey", "assessment",
        "market analysis", "due diligence", "competitive analysis",
    }
    has_research_keyword = any(kw in topic_lower for kw in research_keywords)

    if is_short and not has_research_keyword:
        return ClassifyResponse(
            tier="quick",
            estimated_duration_hours=0.08,
            estimated_credits=_TIER_CREDITS["quick"],
            plan_summary=(
                "Quick investigation: focused lookup with web search and "
                "a concise answer with sources."
            ),
            requires_approval=False,
        )

    # ── 5. Standard-tier: moderate research ──────────────────────────────
    return ClassifyResponse(
        tier="standard",
        estimated_duration_hours=1.5,
        estimated_credits=_TIER_CREDITS["standard"],
        plan_summary=(
            "Standard investigation: structured hypothesis generation, multi-source evidence "
            "gathering, scoring, and a written summary report."
        ),
        requires_approval=True,
    )


# ---------------------------------------------------------------------------
# Routes — Billing
# ---------------------------------------------------------------------------


@app.get("/api/plans", response_model=list[PlanInfo], tags=["Billing"])
async def list_plans() -> list[PlanInfo]:
    """Return all public subscription plans."""
    return [PlanInfo(**plan) for plan in _PLANS]


@app.post(
    "/api/billing/create-checkout",
    response_model=CreateCheckoutResponse,
    tags=["Billing"],
    summary="Create a Stripe Checkout session for a subscription plan",
)
async def create_checkout(
    body: CreateCheckoutRequest,
    current_user: dict[str, str] = Depends(_get_current_user),
) -> CreateCheckoutResponse:
    """
    Create a Stripe Checkout session for the given plan.

    Looks up the plan by ID, resolves its Stripe price ID, and returns
    a Checkout URL the frontend can redirect the user to.
    """
    cfg = _get_config()
    if not cfg.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Billing service not configured")

    # Resolve plan
    plan = next((p for p in _PLANS if p["id"] == body.plan_id), None)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {body.plan_id!r} not found")

    try:
        session = _stripe.checkout.Session.create(
            mode="subscription",
            line_items=[
                {
                    "price": plan["stripe_price_id"],
                    "quantity": 1,
                }
            ],
            success_url=body.success_url,
            cancel_url=body.cancel_url,
            metadata={
                "user_id": current_user["user_id"],
                "plan_id": body.plan_id,
            },
            client_reference_id=current_user["user_id"],
        )
    except _stripe.StripeError as exc:
        logger.error(
            "stripe_checkout_failed",
            user_id=current_user["user_id"],
            plan_id=body.plan_id,
            error=str(exc),
        )
        raise HTTPException(status_code=502, detail=f"Stripe error: {exc}") from exc

    logger.info(
        "checkout_session_created",
        session_id=session.id,
        user_id=current_user["user_id"],
        plan_id=body.plan_id,
    )
    return CreateCheckoutResponse(
        checkout_url=session.url,
        session_id=session.id,
    )


@app.post(
    "/api/billing/webhook",
    tags=["Billing"],
    summary="Stripe webhook receiver",
    status_code=200,
)
async def stripe_webhook(request: Request) -> JSONResponse:
    """
    Handle Stripe webhook events.

    Supported events:
    - ``checkout.session.completed``: record the Stripe customer ID and
      subscription details, then credit the user's Supabase profile.
    - ``customer.subscription.updated``: update subscription status.
    - ``customer.subscription.deleted``: mark subscription as cancelled.

    The Supabase profile is updated via a direct HTTP call to the
    Supabase REST API using the service role key.
    """
    cfg = _get_config()
    if not cfg.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Billing service not configured")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # BUG-S2-06 fix: Reject webhooks entirely when STRIPE_WEBHOOK_SECRET is
    # not configured, instead of silently accepting unverified payloads.
    # An attacker could forge webhook events to credit arbitrary accounts.
    if not cfg.STRIPE_WEBHOOK_SECRET:
        logger.error("stripe_webhook_secret_not_configured")
        raise HTTPException(status_code=503, detail="Webhook signature verification not configured")

    try:
        event = _stripe.Webhook.construct_event(
            payload, sig_header, cfg.STRIPE_WEBHOOK_SECRET
        )
    except _stripe.SignatureVerificationError as exc:
        logger.warning("stripe_webhook_signature_invalid", error=str(exc))
        raise HTTPException(status_code=400, detail="Invalid webhook signature") from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("stripe_webhook_parse_failed", error=str(exc))
        raise HTTPException(status_code=400, detail="Webhook parse error") from exc

    event_type: str = event["type"]
    log = logger.bind(event_type=event_type, event_id=event.get("id"))
    log.info("stripe_webhook_received")

    try:
        if event_type == "checkout.session.completed":
            session_obj = event["data"]["object"]
            await _handle_checkout_completed(session_obj, cfg)

        elif event_type == "customer.subscription.updated":
            sub_obj = event["data"]["object"]
            await _handle_subscription_updated(sub_obj, cfg)

        elif event_type == "customer.subscription.deleted":
            sub_obj = event["data"]["object"]
            await _handle_subscription_deleted(sub_obj, cfg)

        else:
            log.info("stripe_webhook_unhandled_event")

    except Exception as exc:  # noqa: BLE001
        log.error("stripe_webhook_handler_failed", error=str(exc))
        # Return 200 to prevent Stripe from retrying a handler bug
        return JSONResponse(content={"status": "handler_error", "error": str(exc)})

    return JSONResponse(content={"status": "ok"})


@app.get(
    "/api/billing/portal",
    response_model=BillingPortalResponse,
    tags=["Billing"],
    summary="Create a Stripe Customer Portal session",
)
async def billing_portal(
    current_user: dict[str, str] = Depends(_get_current_user),
) -> BillingPortalResponse:
    """
    Generate a Stripe Customer Portal URL for the authenticated user.

    Requires the user's Stripe customer ID to be stored in the Supabase
    profile.  Fetches it via the Supabase REST API.
    """
    cfg = _get_config()
    if not cfg.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Billing service not configured")

    user_id = current_user["user_id"]

    # Fetch the Stripe customer ID from Supabase
    stripe_customer_id = await _get_stripe_customer_id(user_id, cfg)
    if not stripe_customer_id:
        raise HTTPException(
            status_code=404,
            detail="No Stripe customer found for this user. Complete a checkout first.",
        )

    try:
        portal_session = _stripe.billing_portal.Session.create(
            customer=stripe_customer_id,
        )
    except _stripe.StripeError as exc:
        logger.error("stripe_portal_failed", user_id=user_id, error=str(exc))
        raise HTTPException(status_code=502, detail=f"Stripe error: {exc}") from exc

    logger.info("portal_session_created", user_id=user_id)
    return BillingPortalResponse(portal_url=portal_session.url)


# ---------------------------------------------------------------------------
# Stripe webhook helpers
# ---------------------------------------------------------------------------


async def _handle_checkout_completed(
    session_obj: dict[str, Any],
    cfg: AppConfig,
) -> None:
    """Process checkout.session.completed: link Stripe customer, add credits."""
    user_id: str | None = (
        session_obj.get("metadata", {}).get("user_id")
        or session_obj.get("client_reference_id")
    )
    plan_id: str | None = session_obj.get("metadata", {}).get("plan_id")
    stripe_customer_id: str | None = session_obj.get("customer")
    subscription_id: str | None = session_obj.get("subscription")

    if not user_id:
        logger.warning("checkout_completed_no_user_id", session_id=session_obj.get("id"))
        return

    # Resolve credits for this plan
    plan = next((p for p in _PLANS if p["id"] == plan_id), None)
    credits_to_add = plan["credits_per_month"] if plan else 0

    # Retrieve full subscription to get current_period_end
    period_end: str | None = None
    if subscription_id:
        try:
            sub = _stripe.Subscription.retrieve(subscription_id)
            period_end = datetime.fromtimestamp(
                sub["current_period_end"], tz=timezone.utc
            ).isoformat()
        except Exception as exc:  # noqa: BLE001
            logger.warning("subscription_retrieve_failed", error=str(exc))

    update_payload: dict[str, Any] = {}
    if stripe_customer_id:
        update_payload["stripe_customer_id"] = stripe_customer_id
    if subscription_id:
        update_payload["stripe_subscription_id"] = subscription_id
    if plan_id:
        update_payload["subscription_plan"] = plan_id
    if period_end:
        update_payload["subscription_current_period_end"] = period_end
    update_payload["subscription_status"] = "active"

    if update_payload and cfg.SUPABASE_URL and cfg.SUPABASE_SERVICE_KEY:
        await _supabase_patch_profile(user_id, update_payload, cfg)

    # Increment credits — use supabase RPC or a direct PATCH with increment
    if credits_to_add > 0 and cfg.SUPABASE_URL and cfg.SUPABASE_SERVICE_KEY:
        await _supabase_add_credits(user_id, credits_to_add, cfg)

    logger.info(
        "checkout_completed",
        user_id=user_id,
        plan_id=plan_id,
        credits_added=credits_to_add,
    )


async def _handle_subscription_updated(
    sub_obj: dict[str, Any],
    cfg: AppConfig,
) -> None:
    """Process customer.subscription.updated: sync status to Supabase."""
    stripe_customer_id: str | None = sub_obj.get("customer")
    status: str = sub_obj.get("status", "unknown")

    if not stripe_customer_id:
        return

    if not cfg.SUPABASE_URL or not cfg.SUPABASE_SERVICE_KEY:
        logger.warning("supabase_not_configured_skip_subscription_update")
        return

    # Patch all profiles with this Stripe customer ID
    update_payload: dict[str, Any] = {"subscription_status": status}
    period_end_ts = sub_obj.get("current_period_end")
    if period_end_ts:
        update_payload["subscription_current_period_end"] = datetime.fromtimestamp(
            period_end_ts, tz=timezone.utc
        ).isoformat()

    await _supabase_patch_profile_by_customer(
        stripe_customer_id, update_payload, cfg
    )
    logger.info(
        "subscription_updated",
        stripe_customer_id=stripe_customer_id,
        status=status,
    )


async def _handle_subscription_deleted(
    sub_obj: dict[str, Any],
    cfg: AppConfig,
) -> None:
    """Process customer.subscription.deleted: mark subscription cancelled."""
    stripe_customer_id: str | None = sub_obj.get("customer")

    if not stripe_customer_id:
        return

    if not cfg.SUPABASE_URL or not cfg.SUPABASE_SERVICE_KEY:
        logger.warning("supabase_not_configured_skip_subscription_delete")
        return

    await _supabase_patch_profile_by_customer(
        stripe_customer_id,
        {"subscription_status": "canceled"},
        cfg,
    )
    logger.info("subscription_canceled", stripe_customer_id=stripe_customer_id)


# ---------------------------------------------------------------------------
# Supabase REST API helpers
# ---------------------------------------------------------------------------


async def _supabase_patch_profile(
    user_id: str,
    payload: dict[str, Any],
    cfg: AppConfig,
) -> None:
    """PATCH a single Supabase profile row identified by user_id."""
    url = f"{cfg.SUPABASE_URL}/rest/v1/profiles?id=eq.{user_id}"
    headers = {
        "apikey": cfg.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {cfg.SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.patch(url, json=payload, headers=headers)
        if resp.status_code not in (200, 204):
            logger.error(
                "supabase_patch_profile_failed",
                user_id=user_id,
                status=resp.status_code,
                body=resp.text[:200],
            )


async def _supabase_patch_profile_by_customer(
    stripe_customer_id: str,
    payload: dict[str, Any],
    cfg: AppConfig,
) -> None:
    """PATCH Supabase profile rows matching a stripe_customer_id."""
    # BUG-S2-05 fix: URL-encode the customer ID to prevent query injection
    # via crafted Stripe webhook payloads containing special characters.
    from urllib.parse import quote as _url_quote  # noqa: PLC0415
    url = (
        f"{cfg.SUPABASE_URL}/rest/v1/profiles"
        f"?stripe_customer_id=eq.{_url_quote(stripe_customer_id, safe='')}"
    )
    headers = {
        "apikey": cfg.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {cfg.SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.patch(url, json=payload, headers=headers)
        if resp.status_code not in (200, 204):
            logger.error(
                "supabase_patch_by_customer_failed",
                stripe_customer_id=stripe_customer_id,
                status=resp.status_code,
                body=resp.text[:200],
            )


async def _supabase_add_credits(
    user_id: str,
    credits: int,
    cfg: AppConfig,
) -> None:
    """
    Increment the ``tokens`` column in the Supabase profiles table.

    Uses a Postgres RPC function (add_credits) if available; falls back
    to a read-modify-PATCH approach if the function is not present.
    """
    rpc_url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/add_credits"
    headers = {
        "apikey": cfg.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {cfg.SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            rpc_url,
            json={"p_user_id": user_id, "p_credits": credits},
            headers=headers,
        )
        if resp.status_code == 200:
            logger.info("credits_added_via_rpc", user_id=user_id, credits=credits)
            return

        # Fallback: read current balance and PATCH
        logger.warning(
            "add_credits_rpc_unavailable",
            status=resp.status_code,
            fallback="read_modify_patch",
        )
        profile_url = f"{cfg.SUPABASE_URL}/rest/v1/profiles?id=eq.{user_id}&select=tokens"
        get_resp = await client.get(profile_url, headers=headers)
        if get_resp.status_code != 200:
            logger.error("supabase_get_profile_failed", user_id=user_id)
            return
        rows = get_resp.json()
        if not rows:
            logger.error("supabase_profile_not_found", user_id=user_id)
            return
        current_tokens: int = rows[0].get("tokens", 0) or 0
        patch_url = f"{cfg.SUPABASE_URL}/rest/v1/profiles?id=eq.{user_id}"
        patch_resp = await client.patch(
            patch_url,
            json={"tokens": current_tokens + credits},
            headers={**headers, "Prefer": "return=minimal"},
        )
        if patch_resp.status_code not in (200, 204):
            logger.error(
                "supabase_add_credits_fallback_failed",
                user_id=user_id,
                status=patch_resp.status_code,
            )
        else:
            logger.info(
                "credits_added_via_patch",
                user_id=user_id,
                old=current_tokens,
                added=credits,
                new=current_tokens + credits,
            )


async def _supabase_get_user_tokens(
    user_id: str,
    cfg: AppConfig,
) -> int | None:
    """Fetch the current token balance for a user from Supabase profiles."""
    if not cfg.SUPABASE_URL or not cfg.SUPABASE_SERVICE_KEY:
        return None
    url = (
        f"{cfg.SUPABASE_URL}/rest/v1/profiles"
        f"?id=eq.{user_id}&select=tokens"
    )
    headers = {
        "apikey": cfg.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {cfg.SUPABASE_SERVICE_KEY}",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.error(
                "supabase_get_tokens_failed",
                user_id=user_id,
                status=resp.status_code,
            )
            return None
        rows = resp.json()
        if not rows:
            return None
        return int(rows[0].get("tokens", 0) or 0)


async def _supabase_deduct_credits(
    user_id: str,
    amount: int,
    cfg: AppConfig,
) -> bool:
    """Deduct credits from a user's Supabase profile via RPC.

    Calls the ``deduct_credits`` RPC function. Falls back to
    read-modify-PATCH if the function is unavailable.

    Returns True on success, False on failure.
    """
    if not cfg.SUPABASE_URL or not cfg.SUPABASE_SERVICE_KEY:
        logger.warning("supabase_not_configured_skip_deduct")
        return False

    rpc_url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/deduct_credits"
    headers = {
        "apikey": cfg.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {cfg.SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            rpc_url,
            json={"target_user_id": user_id, "amount": amount},
            headers=headers,
        )
        if resp.status_code == 200:
            logger.info("credits_deducted_via_rpc", user_id=user_id, amount=amount)
            return True

        # Fallback: read current balance and PATCH
        logger.warning(
            "deduct_credits_rpc_unavailable",
            status=resp.status_code,
            fallback="read_modify_patch",
        )
        profile_url = f"{cfg.SUPABASE_URL}/rest/v1/profiles?id=eq.{user_id}&select=tokens"
        get_resp = await client.get(profile_url, headers=headers)
        if get_resp.status_code != 200:
            logger.error("supabase_get_profile_failed_for_deduct", user_id=user_id)
            return False
        rows = get_resp.json()
        if not rows:
            logger.error("supabase_profile_not_found_for_deduct", user_id=user_id)
            return False
        current_tokens: int = int(rows[0].get("tokens", 0) or 0)
        new_tokens = max(0, current_tokens - amount)
        patch_url = f"{cfg.SUPABASE_URL}/rest/v1/profiles?id=eq.{user_id}"
        patch_resp = await client.patch(
            patch_url,
            json={"tokens": new_tokens},
            headers={**headers, "Prefer": "return=minimal"},
        )
        if patch_resp.status_code not in (200, 204):
            logger.error(
                "supabase_deduct_credits_fallback_failed",
                user_id=user_id,
                status=patch_resp.status_code,
            )
            return False
        logger.info(
            "credits_deducted_via_patch",
            user_id=user_id,
            old=current_tokens,
            deducted=amount,
            new=new_tokens,
        )
        return True


async def _get_stripe_customer_id(
    user_id: str,
    cfg: AppConfig,
) -> str | None:
    """Fetch the stripe_customer_id for a user from the Supabase profiles table."""
    if not cfg.SUPABASE_URL or not cfg.SUPABASE_SERVICE_KEY:
        return None
    url = (
        f"{cfg.SUPABASE_URL}/rest/v1/profiles"
        f"?id=eq.{user_id}&select=stripe_customer_id"
    )
    headers = {
        "apikey": cfg.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {cfg.SUPABASE_SERVICE_KEY}",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.error(
                "supabase_get_customer_id_failed",
                user_id=user_id,
                status=resp.status_code,
            )
            return None
        rows = resp.json()
        if not rows:
            return None
        return rows[0].get("stripe_customer_id")


# ---------------------------------------------------------------------------
# Routes — Admin
# ---------------------------------------------------------------------------


@app.get(
    "/api/admin/users",
    response_model=list[AdminUserSummary],
    tags=["Admin"],
    summary="List all users (admin only)",
)
async def admin_list_users(
    request: Request,
    _: dict[str, str] = Depends(_require_admin),
) -> list[AdminUserSummary]:
    """
    Return all user profiles via the Supabase ``admin_list_profiles()``
    SECURITY DEFINER function.  The function itself checks that the caller
    has role='admin' in the profiles table, so it's double-gated.
    """
    cfg = _get_config()
    if not cfg.SUPABASE_URL:
        raise HTTPException(status_code=503, detail="Supabase not configured")

    # Forward the caller's JWT so the RPC function can verify admin role
    auth_header = request.headers.get("authorization", "")
    anon_key = cfg.SUPABASE_ANON_KEY or ""

    url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/admin_list_profiles"
    headers = {
        "apikey": anon_key,
        "Authorization": auth_header,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, json={})

    if resp.status_code != 200:
        logger.error("admin_list_users_failed", status=resp.status_code, body=resp.text[:200])
        raise HTTPException(status_code=502, detail="Failed to fetch users from Supabase")

    rows: list[dict[str, Any]] = resp.json()
    return [
        AdminUserSummary(
            user_id=str(r["id"]),
            email=r.get("email"),
            role=r.get("role") or "authenticated",
            credits=r.get("tokens") or 0,
            stripe_customer_id=r.get("stripe_customer_id"),
            subscription_plan=r.get("subscription_plan"),
            subscription_status=r.get("subscription_status"),
            created_at=r.get("created_at"),
        )
        for r in rows
    ]


@app.get(
    "/api/admin/investigations",
    response_model=PaginatedTasksResponse,
    tags=["Admin"],
    summary="List all investigations across all users (admin only)",
)
async def admin_list_investigations(
    page: int = Query(1, ge=1, description="1-based page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    status: str | None = Query(None, description="Filter by status"),
    _: dict[str, str] = Depends(_require_admin),
) -> PaginatedTasksResponse:
    """List every investigation in the system regardless of owner. Admin only."""
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


@app.post(
    "/api/admin/users/{user_id}/credits",
    tags=["Admin"],
    summary="Set or adjust credits for a user (admin only)",
)
async def admin_set_credits(
    user_id: str,
    body: AdminSetCreditsRequest,
    request: Request,
    _: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    """
    Set the absolute credit balance for a user, or add/subtract a delta.

    Uses the ``admin_set_credits`` SECURITY DEFINER function in Supabase
    so no service-role key is needed.
    """
    cfg = _get_config()
    if not cfg.SUPABASE_URL:
        raise HTTPException(status_code=503, detail="Supabase not configured")

    auth_header = request.headers.get("authorization", "")
    anon_key = cfg.SUPABASE_ANON_KEY or ""

    url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/admin_set_credits"
    headers = {
        "apikey": anon_key,
        "Authorization": auth_header,
        "Content-Type": "application/json",
    }
    payload = {
        "target_user_id": user_id,
        "new_credits": body.credits,
        "is_delta": body.delta,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, json=payload)

    if resp.status_code != 200:
        detail = resp.text[:200] if resp.text else "Unknown error"
        logger.error(
            "admin_set_credits_failed",
            user_id=user_id,
            status=resp.status_code,
            detail=detail,
        )
        raise HTTPException(status_code=502, detail=f"Failed to update credits: {detail}")

    new_balance = resp.json()
    logger.info(
        "admin_credits_updated",
        user_id=user_id,
        new_balance=new_balance,
        delta=body.delta,
    )
    return JSONResponse(content={"user_id": user_id, "new_balance": new_balance})


@app.get(
    "/api/admin/stats",
    response_model=AdminStatsResponse,
    tags=["Admin"],
    summary="System-wide statistics (admin only)",
)
async def admin_stats(
    request: Request,
    _: dict[str, str] = Depends(_require_admin),
) -> AdminStatsResponse:
    """Return aggregated system stats for the admin overview."""
    db = _get_db()
    cfg = _get_config()

    # --- Total users via SECURITY DEFINER RPC (no service key needed) ---
    total_users = 0
    if cfg.SUPABASE_URL:
        try:
            auth_header = request.headers.get("authorization", "")
            anon_key = cfg.SUPABASE_ANON_KEY or ""
            headers = {
                "apikey": anon_key,
                "Authorization": auth_header,
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{cfg.SUPABASE_URL}/rest/v1/rpc/admin_count_profiles",
                    headers=headers,
                    json={},
                )
                if resp.status_code == 200:
                    total_users = int(resp.json())
        except Exception as e:
            logger.warning("admin_stats_supabase_error", error=str(e))

    total_investigations: int = await db.fetchval("SELECT COUNT(*) FROM research_tasks") or 0
    running: int = await db.fetchval(
        "SELECT COUNT(*) FROM research_tasks WHERE status = 'RUNNING'"
    ) or 0
    completed: int = await db.fetchval(
        "SELECT COUNT(*) FROM research_tasks WHERE status = 'COMPLETED'"
    ) or 0
    failed: int = await db.fetchval(
        "SELECT COUNT(*) FROM research_tasks WHERE status IN ('FAILED', 'HALTED')"
    ) or 0
    total_spent: float = float(
        await db.fetchval(
            "SELECT COALESCE(SUM(total_spent_usd), 0.0) FROM research_tasks"
        ) or 0.0
    )
    # Approximate credits consumed: $1 = 100 credits, round to integer
    total_credits_consumed = int(total_spent * 100)
    active_users_30d: int = await db.fetchval(
        """
        SELECT COUNT(DISTINCT metadata->>'user_id')
        FROM research_tasks
        WHERE created_at >= NOW() - INTERVAL '30 days'
          AND metadata->>'user_id' IS NOT NULL
        """
    ) or 0

    return AdminStatsResponse(
        total_users=total_users,
        total_investigations=total_investigations,
        running_investigations=running,
        completed_investigations=completed,
        failed_investigations=failed,
        total_credits_consumed=total_credits_consumed,
        total_spent_usd=total_spent,
        active_users_30d=active_users_30d,
    )


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
    # BUG-009 + BUG-S2-03: Require admin key to prevent unauthenticated shutdown.
    # When ADMIN_SECRET_KEY is not configured, ALL shutdown requests are rejected
    # (previously an empty key allowed anyone to shut down the server).
    cfg = _get_config()
    admin_key = getattr(cfg, "ADMIN_SECRET_KEY", "")
    if not admin_key:
        raise HTTPException(status_code=403, detail="Shutdown endpoint disabled (ADMIN_SECRET_KEY not configured)")
    if x_admin_key != admin_key:
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
# Routes — Memory
# ---------------------------------------------------------------------------


class MemoryFactRequest(BaseModel):
    """Request body for storing a user fact."""
    fact: str = Field(..., min_length=1, max_length=2000)
    category: str = Field(default="general", max_length=100)


class MemoryPreferenceRequest(BaseModel):
    """Request body for storing a user preference."""
    key: str = Field(..., min_length=1, max_length=200)
    value: str = Field(..., min_length=1, max_length=2000)


class MemoryResponse(BaseModel):
    """Response containing user memory data."""
    facts: list[str]
    preferences: dict[str, str]
    history: list[dict[str, str]]


@app.get("/api/memory", response_model=MemoryResponse, tags=["Memory"])
async def get_memory(
    current_user: dict[str, str] = Depends(_get_current_user),
) -> MemoryResponse:
    """Retrieve the current user's persistent memory."""
    from pathlib import Path as _MemPath  # noqa: PLC0415
    from mariana.tools.memory import UserMemory  # noqa: PLC0415

    cfg = _get_config()
    mem = UserMemory(user_id=current_user["user_id"], data_root=_MemPath(cfg.DATA_ROOT))
    return MemoryResponse(
        facts=mem.get_facts(),
        preferences=mem.get_preferences(),
        history=mem.get_history(limit=20),
    )


@app.post("/api/memory/facts", tags=["Memory"], status_code=201)
async def store_fact(
    body: MemoryFactRequest,
    current_user: dict[str, str] = Depends(_get_current_user),
) -> dict[str, str]:
    """Store a durable fact for the current user."""
    from pathlib import Path as _MemPath  # noqa: PLC0415
    from mariana.tools.memory import UserMemory  # noqa: PLC0415

    cfg = _get_config()
    mem = UserMemory(user_id=current_user["user_id"], data_root=_MemPath(cfg.DATA_ROOT))
    mem.store_fact(body.fact, body.category)
    return {"status": "ok"}


@app.post("/api/memory/preferences", tags=["Memory"], status_code=201)
async def store_preference(
    body: MemoryPreferenceRequest,
    current_user: dict[str, str] = Depends(_get_current_user),
) -> dict[str, str]:
    """Store a preference for the current user."""
    from pathlib import Path as _MemPath  # noqa: PLC0415
    from mariana.tools.memory import UserMemory  # noqa: PLC0415

    cfg = _get_config()
    mem = UserMemory(user_id=current_user["user_id"], data_root=_MemPath(cfg.DATA_ROOT))
    mem.store_preference(body.key, body.value)
    return {"status": "ok"}


class DeleteFactRequest(BaseModel):
    """Request body for DELETE /api/memory/facts."""
    fact: str = Field(..., min_length=1)


class DeletePreferenceRequest(BaseModel):
    """Request body for DELETE /api/memory/preferences."""
    key: str = Field(..., min_length=1)


@app.delete("/api/memory/facts", tags=["Memory"])
async def delete_fact(
    body: DeleteFactRequest,
    current_user: dict[str, str] = Depends(_get_current_user),
) -> dict[str, str]:
    """Delete a stored fact for the current user."""
    from pathlib import Path as _MemPath  # noqa: PLC0415
    from mariana.tools.memory import UserMemory  # noqa: PLC0415

    cfg = _get_config()
    mem = UserMemory(user_id=current_user["user_id"], data_root=_MemPath(cfg.DATA_ROOT))
    found = mem.delete_fact(body.fact)
    if not found:
        raise HTTPException(status_code=404, detail="Fact not found")
    return {"status": "ok"}


@app.delete("/api/memory/preferences", tags=["Memory"])
async def delete_preference(
    body: DeletePreferenceRequest,
    current_user: dict[str, str] = Depends(_get_current_user),
) -> dict[str, str]:
    """Delete a stored preference for the current user."""
    from pathlib import Path as _MemPath  # noqa: PLC0415
    from mariana.tools.memory import UserMemory  # noqa: PLC0415

    cfg = _get_config()
    mem = UserMemory(user_id=current_user["user_id"], data_root=_MemPath(cfg.DATA_ROOT))
    found = mem.delete_preference(body.key)
    if not found:
        raise HTTPException(status_code=404, detail="Preference not found")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Routes — Skills
# ---------------------------------------------------------------------------


class CreateSkillRequest(BaseModel):
    """Request body for creating a custom skill."""
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1, max_length=2000)
    system_prompt: str = Field(..., min_length=1, max_length=10000)
    trigger_keywords: list[str] = Field(..., min_length=1, max_length=20)


class SkillResponse(BaseModel):
    """Public representation of a skill."""
    id: str
    name: str
    description: str
    trigger_keywords: list[str]
    category: str
    owner_id: str | None = None


@app.get("/api/skills", response_model=list[SkillResponse], tags=["Skills"])
async def list_skills(
    current_user: dict[str, str] = Depends(_get_current_user),
) -> list[SkillResponse]:
    """List all available skills (built-in + custom)."""
    from pathlib import Path as _SkPath  # noqa: PLC0415
    from mariana.tools.skills import SkillManager  # noqa: PLC0415

    cfg = _get_config()
    mgr = SkillManager(data_root=_SkPath(cfg.DATA_ROOT))
    skills = mgr.list_skills(owner_id=current_user["user_id"])
    return [
        SkillResponse(
            id=s.id,
            name=s.name,
            description=s.description,
            trigger_keywords=s.trigger_keywords,
            category=s.category,
            owner_id=s.owner_id,
        )
        for s in skills
    ]


@app.post("/api/skills", response_model=SkillResponse, tags=["Skills"], status_code=201)
async def create_skill(
    body: CreateSkillRequest,
    current_user: dict[str, str] = Depends(_get_current_user),
) -> SkillResponse:
    """Create a custom skill."""
    from pathlib import Path as _SkPath  # noqa: PLC0415
    from mariana.tools.skills import SkillManager  # noqa: PLC0415

    cfg = _get_config()
    mgr = SkillManager(data_root=_SkPath(cfg.DATA_ROOT))
    skill = mgr.create_skill(
        name=body.name,
        description=body.description,
        system_prompt=body.system_prompt,
        trigger_keywords=body.trigger_keywords,
        owner_id=current_user["user_id"],
    )
    return SkillResponse(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        trigger_keywords=skill.trigger_keywords,
        category=skill.category,
        owner_id=skill.owner_id,
    )


@app.delete("/api/skills/{skill_id}", tags=["Skills"])
async def delete_skill(
    skill_id: str,
    current_user: dict[str, str] = Depends(_get_current_user),
) -> dict[str, str]:
    """Delete a custom skill by ID."""
    from pathlib import Path as _SkPath  # noqa: PLC0415
    from mariana.tools.skills import SkillManager  # noqa: PLC0415

    cfg = _get_config()
    mgr = SkillManager(data_root=_SkPath(cfg.DATA_ROOT))

    # Verify ownership: only custom skills owned by the user can be deleted
    skill = mgr.get_skill(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill {skill_id!r} not found")
    if skill.category == "built-in":
        raise HTTPException(status_code=403, detail="Cannot delete built-in skills")
    if skill.owner_id and skill.owner_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Not authorized to delete this skill")

    mgr.delete_skill(skill_id)
    return {"status": "deleted"}


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
