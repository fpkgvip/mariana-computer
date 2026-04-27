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
from collections import defaultdict, deque
import hashlib
import hmac
import json
import json as _json
import os
import secrets
import re
import signal
import sys
import time
import uuid
import weakref
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, List, Literal
from urllib.parse import quote as _url_quote, urlsplit, urlunsplit

import asyncpg
import httpx
import structlog
import stripe as _stripe
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field, field_validator, model_validator
from sse_starlette.sse import EventSourceResponse

# BUG-API-039: Rate limiting via slowapi. Import is guarded so that the
# module still loads on environments where slowapi is not installed; in
# that case the limiter decorators become no-ops.
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded
    _SLOWAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - slowapi is an optional hardening dep
    _SLOWAPI_AVAILABLE = False

    class RateLimitExceeded(Exception):  # type: ignore[no-redef]
        """Fallback exception type when slowapi is not installed."""

    def get_remote_address(request: Any) -> str:  # type: ignore[no-redef]
        return getattr(getattr(request, "client", None), "host", "") or ""

    def _rate_limit_exceeded_handler(request: Any, exc: Exception):  # type: ignore[no-redef]
        return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})

    class _NoopLimiter:
        """No-op limiter used when slowapi is not installed."""

        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def limit(self, *_a: Any, **_kw: Any):
            def _decorator(func):
                return func
            return _decorator

    Limiter = _NoopLimiter  # type: ignore[misc,assignment]

from mariana.config import AppConfig, load_config
from mariana.data.db import create_pool, init_schema, insert_research_task as _db_insert_research_task
from mariana.data.models import ResearchTask as _ResearchTask, TaskStatus as _TaskStatus, State as _State

logger = structlog.get_logger(__name__)


def _jsonable(obj: Any) -> Any:
    """Recursively convert non-JSON-serializable types (datetime, UUID, etc.) to strings."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


# ---------------------------------------------------------------------------
# Application version
# ---------------------------------------------------------------------------

_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Admin constants
# ---------------------------------------------------------------------------

# BUG-0006 fix: Remove hardcoded default UUID.  When ADMIN_USER_ID is not set,
# admin features are disabled (empty string never matches any user_id).
ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID", "")


_ADMIN_ROLE_CACHE: dict[str, tuple[float, bool]] = {}
_ADMIN_ROLE_CACHE_TTL = 30.0  # seconds


def _is_admin_user(user_id: str) -> bool:
    """Return True if the given user_id is an admin.

    Two-path check:
    1. Fast path — matches env-configured ADMIN_USER_ID (bootstrap admin).
    2. DB path — profiles.role = 'admin' for this user_id. Cached 30s.

    BUG-0006 fix: When ADMIN_USER_ID is empty and the DB lookup fails or
    no admin row exists, no user is treated as admin.
    """
    if not user_id:
        return False
    if ADMIN_USER_ID and user_id == ADMIN_USER_ID:
        return True

    # DB-backed admin check with short TTL cache
    import time as _time
    now = _time.time()
    cached = _ADMIN_ROLE_CACHE.get(user_id)
    if cached and now - cached[0] < _ADMIN_ROLE_CACHE_TTL:
        return cached[1]

    is_admin = False
    try:
        cfg = _get_config()
        if cfg.SUPABASE_URL and cfg.SUPABASE_ANON_KEY:
            import httpx as _httpx  # noqa: PLC0415
            # Use SECURITY DEFINER RPC is_admin(user_id uuid) which reads
            # profiles.role='admin' irrespective of RLS.
            url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/is_admin"
            r = _httpx.post(
                url,
                headers={
                    "apikey": cfg.SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {cfg.SUPABASE_ANON_KEY}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={"user_id": user_id},
                timeout=3.0,
            )
            if r.status_code == 200:
                is_admin = bool(r.json())
    except Exception:  # noqa: BLE001 — never leak exceptions from auth gate
        is_admin = False

    _ADMIN_ROLE_CACHE[user_id] = (now, is_admin)
    return is_admin

# BUG-API-024: Allow-list of valid task status values. Clients that pass
# anything outside this set receive a 400 instead of an empty result.
_VALID_TASK_STATUSES: frozenset[str] = frozenset({
    "PENDING",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "HALTED",
    "CANCELLED",
})


def _normalize_bearer_auth_header(raw: str | None) -> str:
    """Normalize a raw Authorization header value for forwarding.

    BUG-API-030 / BUG-API-048: admin endpoints forward the caller's
    Authorization header to Supabase. Strip surrounding whitespace, verify a
    single ``Bearer <token>`` form, and raise 500 on empty/malformed values
    so we never relay nonsense credentials to Supabase.
    """
    if not raw:
        raise HTTPException(
            status_code=500,
            detail="Internal error: admin endpoint called without authorization header",
        )
    value = raw.strip()
    if not value:
        raise HTTPException(
            status_code=500,
            detail="Internal error: authorization header is empty",
        )
    if not value.lower().startswith("bearer "):
        raise HTTPException(
            status_code=500,
            detail="Internal error: expected Bearer authorization header",
        )
    token = value.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(
            status_code=500,
            detail="Internal error: empty Bearer token",
        )
    return f"Bearer {token}"

# ---------------------------------------------------------------------------
# Module-level singletons (populated during lifespan startup)
# ---------------------------------------------------------------------------

_config: AppConfig | None = None
_db_pool: asyncpg.Pool | None = None
_redis: Any | None = None  # aioredis.Redis


def _redact_url_for_logs(raw_url: str) -> str:
    """Return a log-safe representation of a connection URL."""
    if not raw_url:
        return ""
    try:
        parts = urlsplit(raw_url)
        hostname = parts.hostname or ""
        if parts.port is not None:
            hostname = f"{hostname}:{parts.port}"
        if parts.username:
            netloc = f"{parts.username}:***@{hostname}"
        else:
            netloc = hostname or parts.netloc
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except Exception:  # noqa: BLE001
        return "[redacted]"


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
        log.info("db_pool_ready", dsn=_redact_url_for_logs(_config.POSTGRES_DSN))
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
        log.info("redis_ready", url=_redact_url_for_logs(_config.REDIS_URL))
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

# BUG-API-039: Rate limiting via slowapi middleware.
# NOTE: Per-endpoint @limiter.limit() decorators are NOT used because they
# are incompatible with ``from __future__ import annotations`` (they wrap the
# function signature and break FastAPI's parameter introspection, causing 422
# on all decorated POST endpoints).  The global default_limits applies to
# all endpoints uniformly.  For tighter per-route limits, configure at the
# reverse-proxy / ingress layer (nginx, Cloudflare, etc.).
limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
if _SLOWAPI_AVAILABLE:
    from slowapi.middleware import SlowAPIMiddleware  # noqa: PLC0415
    app.add_middleware(SlowAPIMiddleware)

# BUG-027: CORS origins read from config so the hardcoded Vercel URL can be
# updated via environment variable without a code change.
_DEFAULT_PROD_CORS_ORIGINS = [
    "https://frontend-tau-navy-80.vercel.app",
    "https://app.mariana.computer",
]
_DEFAULT_DEV_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:3000",
]
# Preserve the old name for backwards compatibility with external imports.
_DEFAULT_CORS_ORIGINS = _DEFAULT_PROD_CORS_ORIGINS + _DEFAULT_DEV_CORS_ORIGINS

def _is_dev_environment() -> bool:
    """Return True when we should allow localhost-style CORS origins.

    BUG-API-027: Only permit localhost origins in development / debug
    deployments. Production deployments should never accept requests from
    ``http://localhost:*``.
    """
    env = (os.environ.get("APP_ENV") or os.environ.get("ENV") or "").lower()
    if env in {"dev", "development", "local", "test"}:
        return True
    debug = os.environ.get("DEBUG", "").lower()
    return debug in {"1", "true", "yes", "on"}

def _get_cors_origins() -> list[str]:
    """Return CORS allowed origins from env var, falling back to defaults.

    BUG-R3-04 fix: ``add_middleware`` is called at module load time, before
    the FastAPI lifespan context runs, so ``_config`` is always ``None`` at
    that point.  The ``_config.CORS_ALLOWED_ORIGINS`` branch was therefore
    dead code that silently dropped operator-configured origins.  Now we read
    directly from ``os.environ`` (which IS available at import time) so the
    env var is always honoured.

    BUG-API-027: Localhost entries in the default list are only returned
    when the service is running in a DEV/DEBUG environment.
    """
    extra = os.environ.get("CORS_ALLOWED_ORIGINS", "")
    if extra:
        return [o.strip() for o in extra.split(",") if o.strip()]
    if _is_dev_environment():
        return list(_DEFAULT_PROD_CORS_ORIGINS) + list(_DEFAULT_DEV_CORS_ORIGINS)
    return list(_DEFAULT_PROD_CORS_ORIGINS)

# BUG-0022 fix: Use explicit allow_origins list with allow_credentials=True.
# The CORS spec forbids allow_credentials=True with wildcard origin/methods/headers.
# Restrict methods and headers to what the frontend actually needs.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Requested-With"],
)


# ---------------------------------------------------------------------------
# Security headers middleware
#
# Sets a conservative baseline of security headers on every HTTP response.
# These mitigate common browser-level attacks (clickjacking, MIME sniffing,
# reflected XSS, protocol-downgrade) and are safe for an API that is only
# consumed by the trusted frontend. CSP is ``default-src 'self'`` because the
# API itself never renders HTML that loads third-party assets.
# ---------------------------------------------------------------------------


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach a baseline of browser security headers to every response.

    The /preview/* path is the deployed user app: it must be embeddable in
    the studio iframe (a different origin) and may load its own bundled
    JS/CSS, fonts, etc. We therefore skip the strict frame/CSP headers
    there and let the route handler set its own permissive set.
    """

    _HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-XSS-Protection": "1; mode=block",
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
        "Content-Security-Policy": "default-src 'self'",
    }

    # Headers that must NOT be applied to user-deployed preview content.
    _PREVIEW_SKIP = ("X-Frame-Options", "Content-Security-Policy")

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        is_preview = request.url.path.startswith("/preview/")
        for header, value in self._HEADERS.items():
            if is_preview and header in self._PREVIEW_SKIP:
                continue
            # Do not overwrite headers the route handler set deliberately.
            response.headers.setdefault(header, value)
        return response


app.add_middleware(SecurityHeadersMiddleware)


# ---------------------------------------------------------------------------
# BUG-0047 fix: Simple in-memory rate limiter
# ---------------------------------------------------------------------------

_rate_limit_store: dict[str, deque] = defaultdict(deque)

# Auth endpoints get stricter limits to slow credential stuffing
_AUTH_PATH_PREFIXES = ("/api/auth/", "/auth/")
_AUTH_RATE_LIMIT = 20       # requests per window
_DEFAULT_RATE_LIMIT = 60    # requests per window
_RATE_LIMIT_WINDOW = 60     # seconds


def _check_rate_limit(key: str, max_requests: int = _DEFAULT_RATE_LIMIT, window_seconds: int = _RATE_LIMIT_WINDOW) -> bool:
    """Return True if the request is within rate limits, False if exceeded."""
    now = time.monotonic()
    dq = _rate_limit_store[key]
    while dq and dq[0] < now - window_seconds:
        dq.popleft()
    if len(dq) >= max_requests:
        return False
    dq.append(now)
    return True


class RateLimitMiddleware(BaseHTTPMiddleware):
    """In-memory per-user rate limiting middleware."""

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for health/docs endpoints
        path = request.url.path
        if path in ("/health", "/docs", "/openapi.json", "/redoc"):
            return await call_next(request)

        # Extract user identity: prefer user_id from auth, fall back to IP
        # We can't run the full auth dependency here, so use a lightweight
        # token extraction for rate-limit keying.
        key = ""
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if token:
                # Hash the token to avoid storing raw tokens in memory
                key = f"user:{hashlib.sha256(token.encode()).hexdigest()[:16]}"
        if not key:
            key = f"ip:{request.client.host if request.client else 'unknown'}"

        # Choose rate limit based on path
        is_auth_path = any(path.startswith(p) for p in _AUTH_PATH_PREFIXES)
        max_req = _AUTH_RATE_LIMIT if is_auth_path else _DEFAULT_RATE_LIMIT

        if not _check_rate_limit(key, max_requests=max_req):
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please retry after a short wait."},
            )

        return await call_next(request)


app.add_middleware(RateLimitMiddleware)


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


# BUG-API-014: Max serialized size for free-form dict request fields. Prevents
# callers from shipping megabyte-scale JSON blobs in ``metadata`` / ``content``
# / ``user_directives`` — a memory-exhaustion and DB-bloat vector.
_REQUEST_DICT_MAX_BYTES = 32 * 1024  # 32 KB


def _validate_dict_size(value: dict | None, *, max_bytes: int = _REQUEST_DICT_MAX_BYTES) -> dict | None:
    """Reject dicts whose JSON serialization exceeds ``max_bytes``.

    Returns the dict unchanged on success. Raises ``ValueError`` (which
    Pydantic converts to a 422) if the serialization is too large or the
    dict cannot be serialized.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("metadata must be a JSON object")
    try:
        serialized = _json.dumps(value, default=str)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"metadata is not JSON-serializable: {exc}") from exc
    if len(serialized.encode("utf-8")) > max_bytes:
        raise ValueError(
            f"metadata exceeds maximum size of {max_bytes} bytes (serialized)"
        )
    return value


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


# ── Conversation models ─────────────────────────────────────────────────────

class ConversationSummary(BaseModel):
    """A conversation summary for the sidebar list."""
    id: str
    title: str
    created_at: str
    updated_at: str


class ConversationListResponse(BaseModel):
    items: list[ConversationSummary]


class ConversationMessageOut(BaseModel):
    """A single persisted message."""
    id: str
    role: str
    content: str
    type: str = "text"
    metadata: dict | None = None
    created_at: str


class ConversationDetailResponse(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    messages: list[ConversationMessageOut]
    investigations: list[str]  # task_ids linked to this conversation


class CreateConversationRequest(BaseModel):
    title: str = Field("New conversation", min_length=1, max_length=200)


class CreateConversationResponse(BaseModel):
    id: str
    title: str


class UpdateConversationRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


class SaveMessageRequest(BaseModel):
    conversation_id: str
    role: str = Field(..., pattern=r"^(user|assistant|system)$")
    content: str = Field(..., min_length=1, max_length=65536)
    type: str = Field("text", pattern=r"^(text|code|status|error|plan)$")
    metadata: dict | None = None

    @field_validator("metadata")
    @classmethod
    def _cap_metadata_size(cls, value: dict | None) -> dict | None:
        # BUG-API-014: reject oversize metadata blobs to avoid memory/DB bloat.
        return _validate_dict_size(value)


class SaveMessageResponse(BaseModel):
    id: str
    conversation_id: str


# ── Investigation models ───────────────────────────────────────────────────

class StartInvestigationRequest(BaseModel):
    topic: str = Field(..., min_length=1, max_length=4096, description="Research topic or question")
    conversation_id: str | None = Field(None, description="Conversation to link this investigation to")
    # All below are now optional — AI determines them if not provided

    @field_validator("topic")
    @classmethod
    def _normalize_topic(cls, value: str) -> str:
        """Trim whitespace and reject blank topics.

        Pydantic's ``min_length`` alone accepts strings like ``"   "``. That
        let whitespace-only submissions pass API validation, reserve credits,
        and write a task file that the daemon later rejected as empty.
        """
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Topic must not be empty or whitespace only")
        return trimmed

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
    quality_tier: str | None = Field(None, description="Model quality: maximum, high, balanced, economy")
    user_flow_instructions: str | None = Field(None, max_length=8192, description="User's custom instructions for how AI should conduct research")
    continuous_mode: bool = Field(False, description="If true, run in continuous loop until user manually stops")
    dont_kill_branches: bool = Field(False, description="If true, never auto-kill branches regardless of score")
    force_report_on_halt: bool = Field(False, description="If true, generate report instead of halting on critical failures")
    skip_skeptic: bool = Field(False, description="If true, skip the skeptic quality gate")
    skip_tribunal: bool = Field(False, description="If true, skip the adversarial tribunal review")
    user_directives: dict | None = Field(None, description="Freeform user directives dict for custom flow control")
    tier: str | None = Field(None, description="Override tier: instant, quick, standard, deep. If omitted, auto-classified.")
    selected_model: str | None = Field(
        None,
        description="Orchestrator model ID chosen by the user (e.g. 'claude-opus-4-7'). "
                    "Maps to a quality_tier internally.  Takes precedence over quality_tier if both set.",
    )

    @field_validator("user_directives")
    @classmethod
    def _cap_user_directives_size(cls, value: dict | None) -> dict | None:
        # BUG-API-014: user_directives can reach the task payload and DB; cap it.
        return _validate_dict_size(value)


class ClassifyRequest(BaseModel):
    """Request body for the /api/investigations/classify endpoint."""

    topic: str = Field(..., min_length=1, max_length=4096)

    @field_validator("topic")
    @classmethod
    def _normalize_topic(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Topic must not be empty or whitespace only")
        return trimmed


class ArchitectureHypothesis(BaseModel):
    """Lightweight hypothesis for the research architecture preview."""
    statement: str
    priority: int = Field(ge=1, le=10)
    test_strategy: str


class ArchitecturePhase(BaseModel):
    """A single phase in the research flow."""
    name: str
    description: str
    depends_on: list[str] = Field(default_factory=list)


class ResearchArchitecturePlan(BaseModel):
    """Lightweight research architecture returned in the classification
    response so the user can preview the investigation structure before
    approving.  This is NOT the same as the full ResearchArchitectureOutput
    generated by the orchestrator — it is a preview only."""
    hypotheses: list[ArchitectureHypothesis]
    data_sources: list[str]
    research_phases: list[ArchitecturePhase]
    estimated_branches: int
    risk_factors: list[str] = Field(default_factory=list)
    flow_description: str = Field(
        default="",
        description="Human-readable summary of how the research will flow",
    )


# Curated orchestrator model choices exposed to the frontend.
# Each maps to a quality_tier internally.
ORCHESTRATOR_MODELS: list[dict[str, str]] = [
    {
        "id": "claude-opus-4-7",
        "label": "Claude Opus 4.7",
        "description": "Most capable — deepest reasoning, highest accuracy",
        "tier": "maximum",
    },
    {
        "id": "gemini-3.1-pro-preview",
        "label": "Gemini 3.1 Pro",
        "description": "Fast, strong reasoning, large context window",
        "tier": "high",
    },
    {
        "id": "claude-sonnet-4-6",
        "label": "Claude Sonnet 4.6",
        "description": "Balanced quality and speed (default)",
        "tier": "balanced",
    },
    {
        "id": "deepseek-v3.2",
        "label": "DeepSeek V3.2",
        "description": "Budget-friendly, solid performance",
        "tier": "economy",
    },
]


class ClassifyResponse(BaseModel):
    """Classification of an investigation request into a research tier."""

    tier: str  # "instant" | "standard" | "deep"
    estimated_duration_hours: float
    estimated_credits: int
    plan_summary: str  # Brief description of what Mariana will do
    requires_approval: bool  # False for instant, True for standard/deep
    quality_tier: str = "balanced"
    is_conversational: bool = False  # True for greetings/casual messages — use /api/chat/respond instead
    research_architecture: ResearchArchitecturePlan | None = None  # Present for standard/deep tiers
    orchestrator_models: list[dict[str, str]] = Field(
        default_factory=lambda: ORCHESTRATOR_MODELS,
        description="Available orchestrator model choices for the user to select",
    )


class ChatRequest(BaseModel):
    """Request body for the /api/chat/respond endpoint."""
    message: str = Field(..., min_length=1, max_length=8192)
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    """Smart reply: either a conversational response, a research launch, or an agent task."""
    reply: str
    action: str = "chat"  # "chat" = just reply, "research" = launch investigation, "computer" = agent task
    mode: str = "chat"    # "chat" | "research" | "computer" — same signal as action, kept for legacy clients
    research_topic: str | None = None  # refined topic for investigation (when action=research)
    tier: str | None = None  # suggested tier (when action=research)
    user_instructions: str | None = None  # extracted user methodology / custom instructions (when action=research)
    agent_goal: str | None = None  # refined goal when action=computer


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
# Graph models
# ---------------------------------------------------------------------------


class GraphNode(BaseModel):
    """A single node in the investigation knowledge graph."""

    id: str
    label: str
    type: str = "entity"
    description: str = ""
    metadata: dict = {}
    x: float | None = None
    y: float | None = None
    source: str = "human"


class GraphEdge(BaseModel):
    """A directed edge connecting two nodes in the investigation graph.

    Uses D3 naming conventions: ``source`` and ``target`` refer to node IDs.
    The DB columns are ``source_node`` / ``target_node`` to avoid a name clash
    with the ``source`` provenance field; this mapping is handled transparently
    in the API layer.
    """

    id: str
    source: str  # source_node ID (D3 convention)
    target: str  # target_node ID (D3 convention)
    label: str = ""
    metadata: dict = {}
    source_origin: str = "human"  # renamed to avoid clash with `source` node-ID field


class GraphData(BaseModel):
    """Full graph payload for a single investigation."""

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []


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
    """Request body for POST /api/admin/users/{user_id}/credits.

    BUG-API-013: ``credits`` is now validated via a model_validator so it
    can be negative when ``delta=True`` (admin wants to subtract credits)
    but remains ≥ 0 when ``delta=False`` (setting an absolute balance).
    """

    credits: int = Field(..., description="New absolute credits balance, or delta when delta=True")
    delta: bool = Field(False, description="If True, treat credits as a delta to add/subtract")

    @model_validator(mode="after")
    def _validate_credits_sign(self) -> "AdminSetCreditsRequest":
        # Absolute balance must be non-negative; deltas may be negative.
        if not self.delta and self.credits < 0:
            raise ValueError(
                "credits must be >= 0 when delta=False (absolute set). "
                "Pass delta=True to subtract."
            )
        return self


class AdminStatsResponse(BaseModel):
    """System-wide statistics for the admin dashboard."""

    total_users: int
    # BUG-API-025: indicate when the total_users value could not be
    # retrieved (e.g. Supabase RPC failure) so the dashboard can display
    # "unknown" rather than a misleading 0.
    total_users_available: bool = True
    total_investigations: int
    running_investigations: int
    completed_investigations: int
    failed_investigations: int
    total_credits_consumed: int
    total_spent_usd: float
    active_users_30d: int


# --- v3.7 admin models -----------------------------------------------------


class AdminSetRoleRequest(BaseModel):
    role: Literal["user", "admin", "banned"]


class AdminSuspendRequest(BaseModel):
    suspend: bool
    reason: str | None = Field(None, max_length=500)


class AdminCreditsV2Request(BaseModel):
    mode: Literal["set", "delta"]
    amount: int
    reason: str | None = Field(None, max_length=500)


class AdminSystemFreezeRequest(BaseModel):
    frozen: bool
    reason: str | None = Field(None, max_length=500)
    message: str | None = Field(None, max_length=500)


class AdminFeatureFlagUpsert(BaseModel):
    key: str = Field(..., min_length=1, max_length=128)
    enabled: bool = True
    value: dict[str, Any] | None = None
    description: str | None = Field(None, max_length=500)


class AdminAdminTaskUpsert(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    category: str | None = Field(None, max_length=64)
    priority: str | None = Field("P2", max_length=8)
    status: str | None = Field("todo", max_length=32)
    assignee: str | None = Field(None, max_length=128)
    due_date: str | None = None


class AdminAdminTaskPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    category: str | None = None
    priority: str | None = None
    status: str | None = None
    assignee: str | None = None
    due_date: str | None = None


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


async def _authenticate_supabase_token(token: str) -> dict[str, str]:
    """Verify a Supabase access token with Supabase Auth and return user info.

    BUG-V2-01 fix: the previous implementation only base64-decoded the JWT
    payload and trusted attacker-controlled claims, so an unsigned forged token
    could impersonate any user. This helper now asks Supabase Auth to verify the
    token cryptographically via ``GET /auth/v1/user`` before accepting it.
    """
    cfg = _get_config()
    if not cfg.SUPABASE_URL:
        raise HTTPException(status_code=503, detail="Authentication service not configured")

    headers = {"Authorization": f"Bearer {token}"}
    if cfg.SUPABASE_ANON_KEY:
        headers["apikey"] = cfg.SUPABASE_ANON_KEY

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{cfg.SUPABASE_URL}/auth/v1/user", headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("supabase_auth_unreachable", error=str(exc))
        raise HTTPException(status_code=503, detail="Authentication service unavailable") from exc

    if resp.status_code != 200:
        logger.warning("supabase_auth_rejected_token", status=resp.status_code)
        raise HTTPException(status_code=401, detail="Invalid token")

    try:
        payload = resp.json()
    except ValueError as exc:
        logger.error("supabase_auth_invalid_json", error=str(exc))
        raise HTTPException(status_code=503, detail="Authentication service unavailable") from exc

    user_id: str | None = payload.get("id") or payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing user identifier")

    app_metadata = payload.get("app_metadata") or {}
    role: str = payload.get("role") or app_metadata.get("role") or "authenticated"
    return {"user_id": user_id, "role": role}


async def _get_current_user(
    authorization: str | None = Header(None),
) -> dict[str, str]:
    """Validate a bearer token and return basic user info."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return await _authenticate_supabase_token(token)


async def _get_current_user_from_header_or_query(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
) -> dict[str, str]:
    """Authenticate from Authorization header or ``?token=`` query param."""
    raw_token: str | None = None
    if authorization and authorization.startswith("Bearer "):
        raw_token = authorization.split(" ", 1)[1].strip()
    elif token:
        raw_token = token.strip()

    if not raw_token:
        raise HTTPException(status_code=401, detail="Missing or invalid authorization credentials")

    user = await _authenticate_supabase_token(raw_token)
    return {**user, "access_token": raw_token}


async def _require_investigation_owner(
    task_id: str,
    current_user: dict[str, str] = Depends(_get_current_user),
) -> dict[str, str]:
    """Dependency that restricts a task-scoped endpoint to its owner or admin."""
    # ADV-FIX: Validate task_id is a proper UUID before hitting the database.
    # Null bytes, URL-encoded garbage, etc. would otherwise cause 500.
    try:
        uuid.UUID(task_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid task ID format")
    db = _get_db()
    row = await db.fetchrow("SELECT metadata FROM research_tasks WHERE id = $1", task_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    if _is_admin_user(current_user["user_id"]):
        return current_user

    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    if metadata.get("user_id") != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="You do not own this investigation")
    return current_user


# ---------------------------------------------------------------------------
# Stream token — short-lived HMAC-signed token for SSE (never expose full JWT)
# ---------------------------------------------------------------------------
_STREAM_TOKEN_SECRET: bytes | None = None
_STREAM_TOKEN_TTL_SECONDS = 120  # 2 minutes — client must refresh


def _get_stream_token_secret() -> bytes:
    """Return the HMAC key used for SSE stream tokens.

    Prefer the explicit ``STREAM_TOKEN_SECRET`` env var. When it is absent,
    derive a stable fallback from other deployment secrets so tokens remain
    valid across worker processes and routine restarts instead of breaking
    whenever a process generates a fresh random secret at import time.
    """
    global _STREAM_TOKEN_SECRET  # noqa: PLW0603

    if _STREAM_TOKEN_SECRET is not None:
        return _STREAM_TOKEN_SECRET

    configured = os.environ.get("STREAM_TOKEN_SECRET", "")
    if configured:
        _STREAM_TOKEN_SECRET = configured.encode()
        return _STREAM_TOKEN_SECRET

    stable_material = "|".join(
        value
        for value in (
            os.environ.get("ADMIN_SECRET_KEY", ""),
            os.environ.get("SUPABASE_SERVICE_KEY", ""),
            os.environ.get("POSTGRES_DSN", ""),
            os.environ.get("POSTGRES_PASSWORD", ""),
            os.environ.get("LLM_GATEWAY_API_KEY", ""),
        )
        if value
    )
    if stable_material:
        _STREAM_TOKEN_SECRET = hashlib.sha256(stable_material.encode()).digest()
        logger.warning("stream_token_secret_derived_fallback_in_use")
        return _STREAM_TOKEN_SECRET

    _STREAM_TOKEN_SECRET = secrets.token_bytes(32)
    logger.warning("stream_token_secret_ephemeral_fallback_in_use")
    return _STREAM_TOKEN_SECRET


def _mint_stream_token(user_id: str, task_id: str) -> str:
    """Create a short-lived HMAC-signed stream token for SSE.

    Payload: ``{user_id}|{task_id}|{exp_timestamp}``
    The token cannot be used for any other API endpoint.
    """
    exp = int(time.time()) + _STREAM_TOKEN_TTL_SECONDS
    payload = f"{user_id}|{task_id}|{exp}"
    sig = hmac.new(_get_stream_token_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def _verify_stream_token(token: str, task_id: str) -> str:
    """Verify a stream token and return the user_id.

    Raises HTTPException on any validation failure.
    """
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        parts = decoded.split("|")
        if len(parts) != 4:
            raise ValueError("malformed")
        user_id, tok_task_id, exp_str, sig = parts
        # Verify HMAC
        payload = f"{user_id}|{tok_task_id}|{exp_str}"
        expected_sig = hmac.new(_get_stream_token_secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            raise ValueError("bad signature")
        # Verify task_id matches
        if tok_task_id != task_id:
            raise ValueError("task mismatch")
        # Verify not expired. BUG-API-038: allow a small clock-skew grace so
        # tokens minted on node A don't fail on node B when B's wall-clock is
        # a few seconds ahead.
        _CLOCK_SKEW_GRACE_SECONDS = 5
        if int(exp_str) + _CLOCK_SKEW_GRACE_SECONDS < int(time.time()):
            raise ValueError("expired")
        return user_id
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired stream token")


async def _require_investigation_owner_header_or_query(
    task_id: str,
    current_user: dict[str, str] = Depends(_get_current_user_from_header_or_query),
) -> dict[str, str]:
    """SSE-friendly ownership dependency for task-scoped endpoints."""
    try:
        uuid.UUID(task_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid task ID format")
    db = _get_db()
    row = await db.fetchrow("SELECT metadata FROM research_tasks WHERE id = $1", task_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    if _is_admin_user(current_user["user_id"]):
        return current_user

    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    if metadata.get("user_id") != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="You do not own this investigation")
    return current_user


async def _authenticate_stream_token_or_header(
    task_id: str,
    authorization: str | None = Header(None),
    stream_token: str | None = Query(None, alias="token"),
) -> dict[str, str]:
    """Authenticate SSE requests via stream token (preferred) or Authorization header.

    Stream tokens are short-lived HMAC-signed tokens minted by
    ``POST /api/investigations/{task_id}/stream-token``.
    The full JWT is never sent in the query string.
    """
    # ADV-FIX: Validate UUID before any processing.
    try:
        uuid.UUID(task_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid task ID format")
    if stream_token:
        # Verify the stream token (short-lived, single-purpose)
        user_id = _verify_stream_token(stream_token, task_id)
        return {"user_id": user_id}
    elif authorization and authorization.startswith("Bearer "):
        raw_token = authorization.split(" ", 1)[1].strip()
        # BUG-API-015: reject explicitly empty Bearer tokens with a clear 400
        # instead of falling through to the generic 401 below.
        if not raw_token:
            raise HTTPException(
                status_code=400,
                detail="Authorization header has empty Bearer token",
            )
        user = await _authenticate_supabase_token(raw_token)
        # Verify ownership
        db = _get_db()
        row = await db.fetchrow("SELECT metadata FROM research_tasks WHERE id = $1", task_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
        if not _is_admin_user(user["user_id"]):
            metadata = row.get("metadata") or {}
            # BUG-API-016: wrap json.loads in try/except so that a malformed
            # metadata string (rare, but seen when ingesting legacy rows)
            # doesn't surface as a 500 inside this auth dependency.
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (json.JSONDecodeError, TypeError):
                    metadata = {}
            if metadata.get("user_id") != user["user_id"]:
                raise HTTPException(status_code=403, detail="You do not own this investigation")
        return user
    raise HTTPException(status_code=401, detail="Missing or invalid authorization credentials")



async def _require_admin(
    current_user: dict[str, str] = Depends(_get_current_user),
) -> dict[str, str]:
    """Dependency that raises 403 unless the caller is the admin user."""
    if not _is_admin_user(current_user["user_id"]):
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


# ---------------------------------------------------------------------------
# Agent (computer mode) routes — mounted from mariana.agent.api_routes
# ---------------------------------------------------------------------------

try:  # noqa: WPS229 — agent routes are optional at import time
    from mariana.agent.api_routes import make_routes as _make_agent_routes  # noqa: PLC0415

    def _agent_get_redis() -> Any:
        if _redis is None:
            raise HTTPException(status_code=503, detail="Redis unavailable")
        return _redis

    _agent_router = _make_agent_routes(
        get_current_user=_get_current_user,
        get_db=_get_db,
        get_redis=_agent_get_redis,
        get_stream_user=_get_current_user_from_header_or_query,
    )
    app.include_router(_agent_router)
    logger.info("agent_routes_registered", route_count=len(_agent_router.routes))
except Exception as _agent_exc:  # pragma: no cover — best effort
    logger.warning("agent_routes_registration_failed", error=str(_agent_exc))


# ---------------------------------------------------------------------------
# Deft billing routes — credit balance + pre-flight quote
# ---------------------------------------------------------------------------
try:
    from mariana.billing.router import build_billing_router as _build_billing_router

    _billing_router = _build_billing_router(
        get_current_user=_get_current_user,
        get_supabase_url=lambda: _get_config().SUPABASE_URL,
        get_service_key=lambda: (_supabase_api_key(_get_config()) or ""),
    )
    app.include_router(_billing_router)
    logger.info("billing_routes_registered", route_count=len(_billing_router.routes))
except Exception as _billing_exc:  # pragma: no cover — best effort
    logger.warning("billing_routes_registration_failed", error=str(_billing_exc))


# ---------------------------------------------------------------------------
# Deft vault routes — zero-knowledge secret storage
# ---------------------------------------------------------------------------
try:
    from mariana.vault.router import build_vault_router as _build_vault_router

    _vault_router = _build_vault_router(
        get_current_user=_get_current_user,
        get_supabase_url=lambda: _get_config().SUPABASE_URL,
        get_service_key=lambda: (_supabase_api_key(_get_config()) or ""),
    )
    app.include_router(_vault_router)
    logger.info("vault_routes_registered", route_count=len(_vault_router.routes))
except Exception as _vault_exc:  # pragma: no cover — best effort
    logger.warning("vault_routes_registration_failed", error=str(_vault_exc))


# ---------------------------------------------------------------------------
# Deft preview hosting — the deploy_preview tool snapshots a built static
# site to ${DEFT_PREVIEW_ROOT}/<task_id>/ and we serve it from /preview/
# directly via FastAPI (no nginx layer).  This is the magic moment:
# the right-side iframe in /build points here.
# ---------------------------------------------------------------------------
try:
    import mimetypes as _mt
    import re as _re_preview
    from pathlib import Path as _PathPv
    from fastapi import Path as _FPath  # noqa: PLC0415
    from fastapi.responses import FileResponse as _FileResponse  # noqa: PLC0415

    _PREVIEW_ROOT_PATH = _PathPv(os.environ.get("DEFT_PREVIEW_ROOT", "/var/lib/deft/preview"))
    _PREVIEW_ROOT_PATH.mkdir(parents=True, exist_ok=True)
    _SAFE_PREVIEW_TASK = _re_preview.compile(r"^[A-Za-z0-9_\-]{1,64}$")

    @app.get("/preview/{task_id}", include_in_schema=False)
    async def preview_root_redirect(task_id: str):  # noqa: ANN201
        # Redirect /preview/<id> -> /preview/<id>/index.html so iframe paths
        # resolve correctly relative to the entry document.
        from fastapi.responses import RedirectResponse  # noqa: PLC0415
        if not _SAFE_PREVIEW_TASK.match(task_id):
            raise HTTPException(404, "preview not found")
        return RedirectResponse(url=f"/preview/{task_id}/index.html", status_code=302)

    @app.get("/preview/{task_id}/{file_path:path}", include_in_schema=False)
    async def preview_static(task_id: str, file_path: str):  # noqa: ANN201
        """Serve files from the per-task preview snapshot.

        Security: task_id whitelist; resolved path must stay inside the
        task root.  Cache-Control: no-cache to keep the iframe live during
        rapid redeploys.  CORS: open (the frontend iframe lives on a
        different origin than the backend).
        """
        if not _SAFE_PREVIEW_TASK.match(task_id):
            raise HTTPException(404, "preview not found")
        if "\x00" in file_path or ".." in file_path.split("/"):
            raise HTTPException(400, "invalid path")
        root = (_PREVIEW_ROOT_PATH / task_id).resolve()
        if not root.is_dir():
            raise HTTPException(404, "preview not found")
        target = (root / file_path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise HTTPException(400, "path escapes preview root") from None
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            raise HTTPException(404, f"not found: {file_path}")
        ctype, _ = _mt.guess_type(str(target))
        ctype = ctype or "application/octet-stream"
        headers = {
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Access-Control-Allow-Origin": "*",
            "X-Deft-Preview-Task": task_id,
            # Preview must be embeddable in the studio iframe (cross-origin).
            # Override the global SecurityHeaders default of DENY.
            "Content-Security-Policy": "frame-ancestors *",
        }
        return _FileResponse(str(target), media_type=ctype, headers=headers)

    @app.get("/api/preview/{task_id}", tags=["Status"])
    async def preview_manifest(
        task_id: str,
        current_user: dict = Depends(_get_current_user),  # noqa: B008
    ) -> dict[str, Any]:
        """Return the manifest for a task's deployed preview, if any."""
        if not _SAFE_PREVIEW_TASK.match(task_id):
            raise HTTPException(404, "preview not found")
        manifest_file = _PREVIEW_ROOT_PATH / task_id / "_deft_manifest.json"
        if not manifest_file.is_file():
            return {"task_id": task_id, "deployed": False}
        try:
            data = json.loads(manifest_file.read_text("utf-8"))
        except Exception:
            return {"task_id": task_id, "deployed": False}
        # Owners only — sanity check user_id; admins always allowed.
        owner = data.get("user_id")
        if owner and owner != current_user.get("user_id") and not _is_admin_user(current_user.get("user_id", "")):
            raise HTTPException(403, "not your preview")
        rel_url = f"/preview/{task_id}/{data.get('entry') or 'index.html'}"
        return {
            "task_id": task_id,
            "deployed": True,
            "url": rel_url,
            "entry": data.get("entry"),
            "label": data.get("label"),
            "files": int(data.get("files") or 0),
            "total_bytes": int(data.get("total_bytes") or 0),
            "created_at": data.get("created_at"),
        }

    logger.info("preview_routes_registered", root=str(_PREVIEW_ROOT_PATH))
except Exception as _preview_exc:  # pragma: no cover
    logger.warning("preview_routes_registration_failed", error=str(_preview_exc))


# ---------------------------------------------------------------------------
# Billing — hardcoded plan catalogue (matches Supabase plans table)
# ---------------------------------------------------------------------------

# Deft v1 pricing tiers (F6).
# Money invariant: 1 credit = $0.01. Tiers chosen so gross margin >= 40% at p90
# user utilization. Stripe price IDs are looked up from env at startup.
_PLANS: list[dict[str, Any]] = [
    {
        "id": "starter",
        "name": "Starter",
        "price_usd_monthly": 20.0,
        "credits_per_month": 2_000,
        "stripe_price_id": os.environ.get("STRIPE_PRICE_STARTER", "price_starter"),
        "description": "For curious builders kicking the tires",
        "features": [
            "2,000 credits / month",
            "Instant + standard tasks",
            "All built-in skills (research, coding, docs)",
            "Vault for encrypted secrets",
            "Deploy apps to live URLs",
            "Community support",
        ],
    },
    {
        "id": "pro",
        "name": "Pro",
        "price_usd_monthly": 50.0,
        "credits_per_month": 5_500,
        "stripe_price_id": os.environ.get("STRIPE_PRICE_PRO", "price_pro"),
        "description": "For prosumers shipping daily",
        "features": [
            "5,500 credits / month",
            "Instant, standard, and deep tasks",
            "All flagship models",
            "Sub-agent delegation",
            "PDF, DOCX, PPTX, XLSX export",
            "Persistent memory + custom skills",
            "Priority support",
        ],
    },
    {
        "id": "max",
        "name": "Max",
        "price_usd_monthly": 200.0,
        "credits_per_month": 25_000,
        "stripe_price_id": os.environ.get("STRIPE_PRICE_MAX", "price_max"),
        "description": "For heavy autonomous workloads",
        "features": [
            "25,000 credits / month",
            "All task tiers incl. flagship models",
            "Up to 4 concurrent tasks",
            "Image + video generation",
            "Higher per-task budget caps",
            "Dedicated queue",
            "Priority support with SLA",
        ],
    },
]

# One-time top-up packs. Each tier corresponds to a Stripe price (one-time).
_TOPUPS: list[dict[str, Any]] = [
    {
        "id": "topup_starter",
        "name": "Starter top-up",
        "price_usd": 10.0,
        "credits": 1_000,
        "stripe_price_id": os.environ.get("STRIPE_PRICE_TOPUP_STARTER", "price_topup_starter"),
    },
    {
        "id": "topup_pro",
        "name": "Pro top-up",
        "price_usd": 30.0,
        "credits": 3_000,
        "stripe_price_id": os.environ.get("STRIPE_PRICE_TOPUP_PRO", "price_topup_pro"),
    },
    {
        "id": "topup_max",
        "name": "Max top-up",
        "price_usd": 150.0,
        "credits": 15_000,
        "stripe_price_id": os.environ.get("STRIPE_PRICE_TOPUP_MAX", "price_topup_max"),
    },
]

_PLAN_BY_ID: dict[str, dict[str, Any]] = {p["id"]: p for p in _PLANS}
_PLAN_BY_PRICE_ID: dict[str, dict[str, Any]] = {p["stripe_price_id"]: p for p in _PLANS}
_TOPUP_BY_ID: dict[str, dict[str, Any]] = {t["id"]: t for t in _TOPUPS}
_TOPUP_BY_PRICE_ID: dict[str, dict[str, Any]] = {t["stripe_price_id"]: t for t in _TOPUPS}

#: Tier-to-credit cost mapping used by the classification heuristic.
#: At $0.01/credit, these map to: instant=$0.10, standard=$5, deep=$20.
#: Minimum budgets: standard=$5, deep=$20 per the architecture spec.
_TIER_CREDITS: dict[str, int] = {
    "instant": 5,
    "quick": 20,       # ~$0.20 budget, ~30s, single search
    "standard": 100,   # ~$1.00 budget, 3-5 min, moderate analysis
    "deep": 500,       # ~$5.00 budget, 15-45 min, exhaustive research
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
async def get_config(
    current_user: dict[str, str] = Depends(_get_current_user),
) -> ConfigResponse:
    """Return sanitised runtime configuration (API keys are never exposed).

    VULN-C2-07 fix: Requires authentication to prevent information disclosure
    of internal paths and deployment details.
    """
    cfg = _get_config()
    return ConfigResponse(
        model_cheap=cfg.MODEL_CHEAP,
        model_medium=cfg.MODEL_MEDIUM,
        model_expensive=cfg.MODEL_EXPENSIVE,
        budget_branch_hard_cap=cfg.BUDGET_BRANCH_HARD_CAP,
        budget_task_hard_cap=cfg.BUDGET_TASK_HARD_CAP,
        score_kill_threshold=cfg.SCORE_KILL_THRESHOLD,
        score_deepen_threshold=cfg.SCORE_DEEPEN_THRESHOLD,
        data_root="[redacted]",
        log_level=cfg.LOG_LEVEL,
    )


@app.get("/api/orchestrator-models", tags=["Status"])
async def get_orchestrator_models() -> list[dict[str, str]]:
    """Return the curated list of orchestrator model choices.

    No auth required — the list is not sensitive and the frontend needs it
    before the user is authenticated (e.g. on the landing page).
    """
    return ORCHESTRATOR_MODELS


# ---------------------------------------------------------------------------
# Routes — Investigations (tasks)
# ---------------------------------------------------------------------------


@app.post(
    "/api/investigations/classify",
    response_model=ClassifyResponse,
    tags=["Investigations"],
    summary="Classify a research request into a tier",
)
async def classify_request(
    request: Request,
    body: ClassifyRequest,
    current_user: dict[str, str] = Depends(_get_current_user),
) -> ClassifyResponse:
    """
    Classify a research topic into a tier (instant / standard / deep)
    and return estimated duration, credits, and a plan summary.

    Uses a deterministic heuristic — no LLM call required.
    The frontend should call this before submitting an investigation so
    the user can approve or adjust the plan.
    """
    return _classify_topic(body.topic)


@app.post(
    "/api/chat/respond",
    response_model=ChatResponse,
    tags=["Chat"],
    summary="Smart chat: replies conversationally or signals a research launch",
)
async def chat_respond(
    request: Request,
    body: ChatRequest,
    authorization: str | None = Header(None),
    current_user: dict[str, str] = Depends(_get_current_user),
) -> ChatResponse:
    """
    Primary chat endpoint.  The LLM decides how to handle the message:

    - **Conversation** (greetings, meta questions, casual chat): returns a
      reply directly.  No investigation is created.
    - **Research request** (questions requiring deep investigation): returns
      ``action="research"`` with a refined research topic and suggested tier.
      The frontend should then call ``POST /api/investigations`` to launch it.

    Uses a single fast LLM call (~1-2s).  No credits consumed for
    conversational replies.
    """
    cfg = _get_config()
    import httpx  # noqa: PLC0415

    system_prompt = """You are Mariana, an elite AI research assistant built by Mariana Intelligence.
You help users with deep research on any topic with institutional-grade rigor.

Your job: decide how to handle the user's message.

RULE 1: If the message is casual conversation, a greeting, a meta-question about you
("what can you do?", "how does this work?", "who are you?"), or anything that does NOT
require looking up real-world facts or research — REPLY CONVERSATIONALLY.

RULE 2: If the message is a question that requires researching real-world facts, data,
news, analysis, or investigation — signal that you want to launch a research investigation.

RULE 2a: If the message is a REQUEST TO BUILD, CODE, EXECUTE, AUTOMATE, or OPERATE on files
— e.g. "write a Python script to X", "build an app that Y", "refactor this code", "run this
backtest", "fetch this URL and extract Z", "generate a PDF of Q", "compile a Rust binary",
"scrape a page", "download and process", or anything where the right answer is to DO WORK
in a sandbox/terminal/browser rather than write a prose research report — signal that you
want to launch a COMPUTER (agent) task.

RULE 2b: If the user's question refers to something said EARLIER in this conversation
(e.g. "what did I say?", "what was my X?", "summarize what we discussed", "what's my
favorite X?", or any recall/follow-up about prior messages) — REPLY CONVERSATIONALLY
using the conversation history provided. Do NOT launch a research investigation for
questions that can be answered from the chat history.

RULE 3 (CRITICAL): If the user provides ANY specific instructions about HOW to research,
what methodology to use, what to focus on, what to avoid, what tone to use, what format
to produce, or any other customization — you MUST extract those instructions into the
"user_instructions" field. The user is the boss. Whatever they say about how to do the
research, the AI must obey.

Examples of CONVERSATION (reply directly):
- "hello" / "hi" / "hey there"
- "what can you do?" / "how does this work?"
- "tell me about yourself"
- "thanks" / "cool" / "ok"
- "can you help me?" / "what are you good at?"
- "what did I just say?" / "what was my question?"
- "what is my favorite color?" (when they told you earlier in the conversation)
- "summarize our conversation" / "what have we discussed so far?"

Examples of RESEARCH (launch investigation):
- "What is the current state of AI regulation in the EU?"
- "Compare Tesla and BYD market share in 2025"
- "Analyze the impact of rate cuts on CMBS markets, focus on default rates and use only academic sources"
- "Research Bitcoin price prediction but use technical analysis methodology, not fundamental"

RESPOND IN THIS EXACT JSON FORMAT (nothing else):
{"action": "chat", "reply": "your conversational reply here"}
OR
{"action": "research", "reply": "brief message to the user", "research_topic": "clean research topic", "tier": "standard", "user_instructions": "extracted user instructions on HOW to research (methodology, focus, constraints, tone, etc.) — include EVERYTHING the user said about how to do it. If no special instructions, use null."}
OR
{"action": "computer", "reply": "brief message explaining what you will do", "agent_goal": "concise goal string describing the concrete build/code/execute task", "user_instructions": "extracted instructions on HOW (libraries, style, constraints). Null if none."}

For the tier field when action is "research":
- "quick" = simple factual lookup, takes ~30 seconds (e.g. "what is X?", "who is Y?")
- "standard" = moderate analysis, takes 3-5 minutes (e.g. "compare X and Y", "what happened with Z?")
- "deep" = exhaustive multi-angle investigation, takes 15-45 minutes (only when the user explicitly asks for deep research, thorough analysis, or a comprehensive report)

IMPORTANT: If the user specifies a time constraint, choose the tier that best fits:
- "keep it quick" / "1-2 minutes" / "just a quick look" → use "quick"
- "spend about 3-5 minutes" / "moderate" → use "standard"
- "take your time" / "thorough" / "deep dive" → use "deep"
The user's time preference overrides the default complexity-based tier selection.

Default to "standard" if unsure and no time preference is given. Only use "deep" when explicitly requested.

For user_instructions: Extract the user's FULL intent about HOW to research. Examples:
- "focus on emerging markets" → user_instructions: "Focus the research on emerging markets specifically"
- "use only peer-reviewed sources" → user_instructions: "Use only peer-reviewed academic sources"
- "I don't care if the hypothesis is bad just continue" → user_instructions: "Do not kill hypotheses even if they seem weak. Continue researching all angles regardless of initial quality."
- "make it a bear case analysis" → user_instructions: "Frame the research from a bear/pessimistic perspective"
- "research this but be contrarian" → user_instructions: "Take a contrarian stance. Challenge consensus views."
- "just 3-5 minutes" or "keep it short" → user_instructions: "Keep the research brief. Target 3-5 minutes of research time."
- "make me a pdf" or "write a report" → user_instructions: "Produce a PDF report with the findings."
If the user gives no special instructions, set user_instructions to null.

For conversational replies: be warm, concise (1-3 sentences), professional.
Introduce yourself briefly if it's a first greeting.
If they ask what you can do, explain you're an AI that can have normal conversations AND launch deep research investigations on any topic."""

    # ── BUG-D5-02: Fetch conversation history for context ──────────────
    # Without this, the LLM has no idea what was said earlier in the
    # conversation, causing generic/confused responses to follow-ups.
    # BUG-D7-01: Pass user_token so RLS allows the read (service key is
    # not configured, so anon-key queries return 0 rows).
    user_token = (
        authorization.split(" ", 1)[1].strip()
        if authorization and authorization.startswith("Bearer ")
        else None
    )
    history_messages: list[dict[str, str]] = []
    if body.conversation_id:
        try:
            hist_resp = await _supabase_rest(
                cfg, "GET", "/conversation_messages",
                params={
                    "conversation_id": f"eq.{body.conversation_id}",
                    "select": "role,content,type",
                    "order": "created_at.asc",
                    "limit": "50",
                },
                user_token=user_token,
            )
            if hist_resp.status_code == 200:
                for m in hist_resp.json():
                    role = m.get("role", "user")
                    content = m.get("content", "")
                    msg_type = m.get("type", "text")
                    # Only include text messages (skip status/system messages)
                    if msg_type in ("text", None) and role in ("user", "assistant") and content.strip():
                        history_messages.append({"role": role, "content": content})
        except Exception as hist_err:
            logger.warning("chat_history_fetch_error", error=str(hist_err))

    # Build the messages array: system prompt + history + current message
    llm_messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    # Include up to last 20 messages of history for context (avoid token overflow)
    if history_messages:
        llm_messages.extend(history_messages[-20:])
    llm_messages.append({"role": "user", "content": body.message})

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{cfg.LLM_GATEWAY_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.LLM_GATEWAY_API_KEY}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": llm_messages,
                    "max_tokens": 512,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            # BUG-API-046: Guard against empty/missing choices from LLM gateway
            choices = data.get("choices") or []
            if not choices or not choices[0].get("message", {}).get("content"):
                raise ValueError("LLM gateway returned empty choices")
            raw = choices[0]["message"]["content"].strip()

            # Parse the JSON response from the LLM
            import json as _json  # noqa: PLC0415
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            # BUG-D7-02: When conversation history is included, the LLM
            # sometimes responds as plain text instead of JSON (it "forgets"
            # the format instruction and just answers naturally). If JSON
            # parsing fails, treat the raw text as a conversational reply.
            try:
                parsed = _json.loads(raw)
            except (ValueError, TypeError):
                logger.info("chat_respond_plain_text_fallback", raw_preview=raw[:200])
                return ChatResponse(reply=raw, action="chat")

            action = parsed.get("action", "chat")
            reply = parsed.get("reply", "")

            if action == "research":
                return ChatResponse(
                    reply=reply,
                    action="research",
                    mode="research",
                    research_topic=parsed.get("research_topic", body.message),
                    tier=parsed.get("tier", "standard"),
                    user_instructions=parsed.get("user_instructions") or None,
                )
            elif action == "computer":
                return ChatResponse(
                    reply=reply,
                    action="computer",
                    mode="computer",
                    agent_goal=parsed.get("agent_goal") or body.message,
                    user_instructions=parsed.get("user_instructions") or None,
                )
            else:
                return ChatResponse(reply=reply, action="chat", mode="chat")

    except Exception as exc:
        logger.warning("chat_respond_error", error=str(exc))
        # Fallback: use the old pattern-matching classify for safety
        classification = _classify_topic(body.message)
        if classification.is_conversational:
            return ChatResponse(
                reply=(
                    "Hello! I'm Mariana, your AI research assistant. "
                    "I can chat with you normally, and when you have a topic "
                    "that needs deep research, just ask and I'll investigate it for you."
                ),
                action="chat",
            )
        else:
            return ChatResponse(
                reply=f"I'll research that for you: {body.message}",
                action="research",
                research_topic=body.message,
                tier=classification.tier,
            )


# ════════════════════════════════════════════════════════════════════════════
#  Conversations CRUD
# ════════════════════════════════════════════════════════════════════════════


# BUG-API-050: idempotent methods that are safe to retry on transient
# network / 5xx errors. PATCH and DELETE are included only when the caller
# targets a specific primary key (filtered via params) so the replay is
# still idempotent — callers that issue bulk PATCH/DELETE should pass
# ``allow_retry=False`` explicitly.
_SUPABASE_RETRY_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PATCH", "DELETE", "PUT"})
_SUPABASE_RETRY_MAX_ATTEMPTS = 3
_SUPABASE_RETRY_BASE_DELAY = 0.25  # seconds; exponential 0.25, 0.5, 1.0
_SUPABASE_RETRYABLE_STATUSES = frozenset({502, 503, 504})


async def _supabase_rest(
    cfg: AppConfig,
    method: str,
    path: str,
    *,
    json: dict | list | None = None,
    params: dict[str, str] | None = None,
    headers_extra: dict[str, str] | None = None,
    user_token: str | None = None,
    allow_retry: bool | None = None,
) -> httpx.Response:
    """Low-level Supabase REST helper for user-scoped operations.

    BUG-0004 fix: ``user_token`` is now required (must be a non-empty string).
    Callers performing system-level operations (webhook handlers, internal
    bookkeeping) must use ``_supabase_rest_system()`` instead to make the
    service-key usage explicit.

    BUG-API-050: for idempotent requests (GET/HEAD, or PATCH/DELETE/PUT with a
    PK-shaped filter), transparently retry on connection errors, read timeouts,
    and 502/503/504 upstream errors with exponential backoff.  Non-idempotent
    requests (POST) are never retried because they could produce duplicate
    writes.  Callers that know their PATCH/DELETE is non-idempotent (e.g. bulk
    updates without a PK filter) can opt out by passing ``allow_retry=False``.
    """
    if not user_token:
        raise ValueError(
            "_supabase_rest() requires a non-empty user_token for RLS-scoped "
            "requests. Use _supabase_rest_system() for service-key operations."
        )
    api_key = _supabase_api_key(cfg)
    if not api_key:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    auth_key = user_token
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {auth_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    if headers_extra:
        headers.update(headers_extra)
    url = f"{cfg.SUPABASE_URL}/rest/v1{path}"

    method_upper = method.upper()
    if allow_retry is None:
        # Auto-detect: PATCH/DELETE/PUT are only retried when a filter param
        # (e.g. ``id=eq.<uuid>``) is present, indicating a PK-targeted request.
        if method_upper in ("GET", "HEAD"):
            allow_retry = True
        elif method_upper in ("PATCH", "DELETE", "PUT") and params:
            allow_retry = any(
                isinstance(v, str) and v.startswith("eq.")
                for v in params.values()
            )
        else:
            allow_retry = False

    max_attempts = _SUPABASE_RETRY_MAX_ATTEMPTS if allow_retry and method_upper in _SUPABASE_RETRY_IDEMPOTENT_METHODS else 1
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=15.0) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await client.request(
                    method_upper, url, json=json, params=params, headers=headers
                )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt >= max_attempts:
                    logger.warning(
                        "supabase_rest_retry_exhausted",
                        method=method_upper,
                        path=path,
                        attempts=attempt,
                        error=str(exc),
                    )
                    raise
                delay = _SUPABASE_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.info(
                    "supabase_rest_retry",
                    method=method_upper,
                    path=path,
                    attempt=attempt,
                    delay=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code in _SUPABASE_RETRYABLE_STATUSES and attempt < max_attempts:
                delay = _SUPABASE_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.info(
                    "supabase_rest_retry_status",
                    method=method_upper,
                    path=path,
                    attempt=attempt,
                    status=resp.status_code,
                    delay=delay,
                )
                await asyncio.sleep(delay)
                continue
            return resp

    # Unreachable — either returned or raised above.
    assert last_exc is not None
    raise last_exc


async def _supabase_rest_system(
    cfg: AppConfig,
    method: str,
    path: str,
    *,
    json: dict | list | None = None,
    params: dict[str, str] | None = None,
    headers_extra: dict[str, str] | None = None,
    allow_retry: bool | None = None,
) -> httpx.Response:
    """Supabase REST helper for system/service-key operations.

    BUG-0004 fix: Explicitly uses the service-role API key for internal
    bookkeeping (webhook handlers, task linking) where no user JWT is
    available. This separation makes service-key usage auditable and prevents
    accidental privilege escalation in user-facing code paths.
    """
    api_key = _supabase_api_key(cfg)
    if not api_key:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    auth_key = api_key
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {auth_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    if headers_extra:
        headers.update(headers_extra)
    url = f"{cfg.SUPABASE_URL}/rest/v1{path}"

    method_upper = method.upper()
    if allow_retry is None:
        if method_upper in ("GET", "HEAD"):
            allow_retry = True
        elif method_upper in ("PATCH", "DELETE", "PUT") and params:
            allow_retry = any(
                isinstance(v, str) and v.startswith("eq.")
                for v in params.values()
            )
        else:
            allow_retry = False

    max_attempts = _SUPABASE_RETRY_MAX_ATTEMPTS if allow_retry and method_upper in _SUPABASE_RETRY_IDEMPOTENT_METHODS else 1
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=15.0) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await client.request(
                    method_upper, url, json=json, params=params, headers=headers
                )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt >= max_attempts:
                    raise
                delay = _SUPABASE_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                await asyncio.sleep(delay)
                continue

            if resp.status_code in _SUPABASE_RETRYABLE_STATUSES and attempt < max_attempts:
                delay = _SUPABASE_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                await asyncio.sleep(delay)
                continue
            return resp

    assert last_exc is not None
    raise last_exc


@app.post(
    "/api/conversations",
    response_model=CreateConversationResponse,
    status_code=201,
    tags=["Conversations"],
    summary="Create a new conversation",
)
async def create_conversation(
    body: CreateConversationRequest,
    authorization: str | None = Header(None),
    current_user: dict[str, str] = Depends(_get_current_user),
) -> CreateConversationResponse:
    cfg = _get_config()
    user_id = current_user["user_id"]
    user_token = authorization.split(" ", 1)[1].strip() if authorization and authorization.startswith("Bearer ") else None
    row = {"user_id": user_id, "title": body.title.strip() or "New conversation"}
    resp = await _supabase_rest(cfg, "POST", "/conversations", json=row, user_token=user_token)
    if resp.status_code not in (200, 201):
        logger.error("create_conversation_failed", status=resp.status_code, body=resp.text)
        raise HTTPException(status_code=500, detail="Failed to create conversation")
    data = resp.json()
    created = data[0] if isinstance(data, list) else data
    return CreateConversationResponse(id=created["id"], title=created["title"])


@app.get(
    "/api/conversations",
    response_model=ConversationListResponse,
    tags=["Conversations"],
    summary="List all conversations for the current user",
)
async def list_conversations(
    authorization: str | None = Header(None),
    current_user: dict[str, str] = Depends(_get_current_user),
) -> ConversationListResponse:
    cfg = _get_config()
    user_id = current_user["user_id"]
    user_token = authorization.split(" ", 1)[1].strip() if authorization and authorization.startswith("Bearer ") else None
    resp = await _supabase_rest(
        cfg, "GET", "/conversations",
        params={
            "user_id": f"eq.{user_id}",
            "select": "id,title,created_at,updated_at",
            "order": "updated_at.desc",
            "limit": "100",
        },
        user_token=user_token,
    )
    if resp.status_code != 200:
        logger.error("list_conversations_failed", status=resp.status_code, body=resp.text)
        raise HTTPException(status_code=500, detail="Failed to list conversations")
    rows = resp.json()
    items = [
        ConversationSummary(
            id=r["id"],
            title=r["title"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]
    return ConversationListResponse(items=items)


@app.get(
    "/api/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
    tags=["Conversations"],
    summary="Get a conversation with all messages and linked investigation IDs",
)
async def get_conversation(
    conversation_id: str,
    authorization: str | None = Header(None),
    current_user: dict[str, str] = Depends(_get_current_user),
) -> ConversationDetailResponse:
    # BUG-P2-04: Validate conversation_id is a valid UUID to avoid 500 from Supabase
    try:
        uuid.UUID(conversation_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid conversation ID format")

    cfg = _get_config()
    user_id = current_user["user_id"]
    user_token = authorization.split(" ", 1)[1].strip() if authorization and authorization.startswith("Bearer ") else None

    # Fetch conversation (RLS ensures ownership)
    conv_resp = await _supabase_rest(
        cfg, "GET", "/conversations",
        params={
            "id": f"eq.{conversation_id}",
            "user_id": f"eq.{user_id}",
            "select": "id,title,created_at,updated_at",
            "limit": "1",
        },
        user_token=user_token,
    )
    if conv_resp.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to fetch conversation")
    convs = conv_resp.json()
    if not convs:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv = convs[0]

    # Fetch messages
    msg_resp = await _supabase_rest(
        cfg, "GET", "/conversation_messages",
        params={
            "conversation_id": f"eq.{conversation_id}",
            "select": "id,role,content,type,metadata,created_at",
            "order": "created_at.asc",
            "limit": "1000",
        },
        user_token=user_token,
    )
    msgs: list[ConversationMessageOut] = []
    if msg_resp.status_code == 200:
        # BUG-API-049: use .get(...) with defensive defaults so that a
        # partially-populated row (e.g. a race where created_at is NULL, or
        # a schema drift) does not raise KeyError and 500 the entire
        # conversation fetch.  Rows missing required fields are skipped.
        for m in msg_resp.json():
            if not isinstance(m, dict):
                continue
            raw_meta = m.get("metadata")
            if isinstance(raw_meta, str):
                try:
                    raw_meta = _json.loads(raw_meta)
                except (ValueError, TypeError):
                    raw_meta = None
            msg_id = m.get("id")
            msg_role = m.get("role")
            msg_content = m.get("content", "")
            msg_created_at = m.get("created_at")
            # Skip rows missing any field that downstream consumers require.
            if not msg_id or not msg_role or not msg_created_at:
                logger.warning(
                    "conversation_message_skipped_missing_fields",
                    conversation_id=conversation_id,
                    have_id=bool(msg_id),
                    have_role=bool(msg_role),
                    have_created_at=bool(msg_created_at),
                )
                continue
            msgs.append(ConversationMessageOut(
                id=msg_id,
                role=msg_role,
                content=msg_content if isinstance(msg_content, str) else str(msg_content),
                type=m.get("type") or "text",
                metadata=raw_meta if isinstance(raw_meta, dict) else None,
                created_at=msg_created_at,
            ))

    # Fetch linked investigation task_ids
    inv_resp = await _supabase_rest(
        cfg, "GET", "/investigations",
        params={
            "conversation_id": f"eq.{conversation_id}",
            "user_id": f"eq.{user_id}",
            "select": "task_id",
        },
        user_token=user_token,
    )
    inv_ids: list[str] = []
    if inv_resp.status_code == 200:
        inv_ids = [r["task_id"] for r in inv_resp.json() if r.get("task_id")]

    return ConversationDetailResponse(
        id=conv["id"],
        title=conv["title"],
        created_at=conv["created_at"],
        updated_at=conv["updated_at"],
        messages=msgs,
        investigations=inv_ids,
    )


@app.patch(
    "/api/conversations/{conversation_id}",
    tags=["Conversations"],
    summary="Update conversation title",
)
async def update_conversation(
    conversation_id: str,
    body: UpdateConversationRequest,
    authorization: str | None = Header(None),
    current_user: dict[str, str] = Depends(_get_current_user),
) -> dict:
    try:
        uuid.UUID(conversation_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid conversation ID format")
    cfg = _get_config()
    user_id = current_user["user_id"]
    user_token = authorization.split(" ", 1)[1].strip() if authorization and authorization.startswith("Bearer ") else None
    resp = await _supabase_rest(
        cfg, "PATCH", "/conversations",
        json={"title": body.title.strip(), "updated_at": datetime.now(tz=timezone.utc).isoformat()},
        params={"id": f"eq.{conversation_id}", "user_id": f"eq.{user_id}"},
        user_token=user_token,
    )
    if resp.status_code not in (200, 204):
        raise HTTPException(status_code=500, detail="Failed to update conversation")
    # BUG-0053b fix: check if any rows were actually affected
    try:
        affected = resp.json()
        if isinstance(affected, list) and len(affected) == 0:
            raise HTTPException(status_code=404, detail="Conversation not found")
    except (json.JSONDecodeError, ValueError):
        pass  # 204 has no body; that's fine
    return {"ok": True}


@app.delete(
    "/api/conversations/{conversation_id}",
    tags=["Conversations"],
    summary="Delete a conversation and all its messages",
)
async def delete_conversation(
    conversation_id: str,
    authorization: str | None = Header(None),
    current_user: dict[str, str] = Depends(_get_current_user),
) -> dict:
    try:
        uuid.UUID(conversation_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid conversation ID format")
    cfg = _get_config()
    user_id = current_user["user_id"]
    user_token = authorization.split(" ", 1)[1].strip() if authorization and authorization.startswith("Bearer ") else None
    # Cascade delete handles messages. Unlink investigations (SET NULL).
    resp = await _supabase_rest(
        cfg, "DELETE", "/conversations",
        params={"id": f"eq.{conversation_id}", "user_id": f"eq.{user_id}"},
        user_token=user_token,
    )
    if resp.status_code not in (200, 204):
        raise HTTPException(status_code=500, detail="Failed to delete conversation")
    # BUG-0053b fix: check if any rows were actually affected
    try:
        affected = resp.json()
        if isinstance(affected, list) and len(affected) == 0:
            raise HTTPException(status_code=404, detail="Conversation not found")
    except (json.JSONDecodeError, ValueError):
        pass  # 204 has no body; that's fine
    return {"ok": True}


@app.post(
    "/api/conversations/messages",
    response_model=SaveMessageResponse,
    status_code=201,
    tags=["Conversations"],
    summary="Save a message to a conversation",
)
async def save_message(
    body: SaveMessageRequest,
    authorization: str | None = Header(None),
    current_user: dict[str, str] = Depends(_get_current_user),
) -> SaveMessageResponse:
    """Persist a single message to a conversation. The frontend calls this
    to save user, assistant, and system messages as they happen."""
    cfg = _get_config()
    user_id = current_user["user_id"]
    user_token = authorization.split(" ", 1)[1].strip() if authorization and authorization.startswith("Bearer ") else None

    # BUG-API-028: Validate conversation_id as a UUID to avoid confusing
    # PostgREST 400/500s when a client sends a malformed id.
    try:
        uuid.UUID(body.conversation_id)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid conversation_id format") from exc

    # Verify conversation ownership first
    check = await _supabase_rest(
        cfg, "GET", "/conversations",
        params={"id": f"eq.{body.conversation_id}", "user_id": f"eq.{user_id}", "select": "id", "limit": "1"},
        user_token=user_token,
    )
    if check.status_code != 200 or not check.json():
        raise HTTPException(status_code=404, detail="Conversation not found")

    import json as _json  # noqa: PLC0415
    row = {
        "conversation_id": body.conversation_id,
        "role": body.role,
        "content": body.content,
        "type": body.type,
        "metadata": _json.dumps(body.metadata) if body.metadata else "{}",
    }
    resp = await _supabase_rest(cfg, "POST", "/conversation_messages", json=row, user_token=user_token)
    if resp.status_code not in (200, 201):
        logger.error("save_message_failed", status=resp.status_code, body=resp.text)
        raise HTTPException(status_code=500, detail="Failed to save message")

    data = resp.json()
    created = data[0] if isinstance(data, list) else data

    # Update conversation's updated_at and auto-title from first user message
    patch: dict[str, str] = {"updated_at": datetime.now(tz=timezone.utc).isoformat()}
    if body.role == "user":
        # Auto-title: use first ~60 chars of the first user message
        title_candidate = body.content.strip()[:60]
        if title_candidate:
            # Only auto-title if current title is default
            conv_check = await _supabase_rest(
                cfg, "GET", "/conversations",
                params={"id": f"eq.{body.conversation_id}", "select": "title", "limit": "1"},
                user_token=user_token,
            )
            if conv_check.status_code == 200:
                conv_data = conv_check.json()
                if conv_data and conv_data[0].get("title") in ("New conversation", ""):
                    patch["title"] = title_candidate
    await _supabase_rest(
        cfg, "PATCH", "/conversations",
        json=patch,
        params={"id": f"eq.{body.conversation_id}"},
        user_token=user_token,
    )

    return SaveMessageResponse(id=created["id"], conversation_id=body.conversation_id)


@app.post(
    "/api/investigations",
    response_model=StartInvestigationResponse,
    status_code=202,
    tags=["Investigations"],
)
async def start_investigation(
    request: Request,
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

    # ── BUG-D2-04 fix: Validate quality_tier before any processing ─────────
    # Previously an invalid value like "ultra" was silently accepted and
    # written to .task.json, only caught much later via a warning in session.py.
    _VALID_QUALITY_TIERS: frozenset[str] = frozenset({"maximum", "high", "balanced", "economy"})
    if body.quality_tier and body.quality_tier not in _VALID_QUALITY_TIERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid quality_tier {body.quality_tier!r}. "
                f"Must be one of: {sorted(_VALID_QUALITY_TIERS)}"
            ),
        )

    # ── Map selected_model → quality_tier (takes precedence) ─────────────
    _MODEL_TO_TIER: dict[str, str] = {
        m["id"]: m["tier"] for m in ORCHESTRATOR_MODELS
    }
    if body.selected_model and body.selected_model in _MODEL_TO_TIER:
        body.quality_tier = _MODEL_TO_TIER[body.selected_model]

    # ── Fill in AI-determined values when the caller omits them ─────────────
    classification = _classify_topic(body.topic)

    # Allow the caller (or the chat LLM) to override the tier.
    if body.tier and body.tier in ("instant", "quick", "standard", "deep"):
        classification.tier = body.tier
        classification.estimated_credits = _TIER_CREDITS.get(body.tier, 100)

    # ── BUG-0049 fix: Validate tier/plan constraints server-side ───────────
    # Determine user plan (default to "free" if lookup fails)
    # BUG-S6-01 fix: Use the user's own JWT (via _supabase_rest) instead of
    # _supabase_rest_system.  The system call uses the anon key (service key
    # is not configured), which cannot read profiles through RLS — so every
    # user was silently downgraded to "free".
    user_plan = "free"
    _auth_header = request.headers.get("authorization", "")
    _user_jwt = (
        _auth_header.split(" ", 1)[1].strip()
        if _auth_header.lower().startswith("bearer ")
        else None
    )
    try:
        _plan_resp = await _supabase_rest(
            cfg, "GET", "/profiles",
            params={
                "id": f"eq.{current_user['user_id']}",
                "select": "plan",
                "limit": "1",
            },
            user_token=_user_jwt,
        )
        if _plan_resp.status_code == 200:
            _plan_data = _plan_resp.json()
            if _plan_data:
                user_plan = (_plan_data[0].get("plan") or "free") if isinstance(_plan_data, list) else (_plan_data.get("plan") or "free")
    except Exception:  # noqa: BLE001
        pass  # Default to "free" on lookup failure

    # Plan-based tier restrictions
    _PLAN_ALLOWED_TIERS: dict[str, set[str]] = {
        "free": {"instant", "quick"},
        "starter": {"instant", "quick", "standard"},
        "pro": {"instant", "quick", "standard", "deep"},
        "flagship": {"instant", "quick", "standard", "deep"},
    }
    allowed_tiers = _PLAN_ALLOWED_TIERS.get(user_plan, {"instant", "quick"})
    if classification.tier not in allowed_tiers:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Your {user_plan!r} plan does not support tier {classification.tier!r}. "
                f"Allowed tiers: {sorted(allowed_tiers)}"
            ),
        )

    # Cap duration_hours at 24 (or plan maximum)
    _PLAN_MAX_DURATION: dict[str, float] = {
        "free": 1.0,
        "starter": 6.0,
        "pro": 24.0,
        "flagship": 24.0,
    }
    max_duration = _PLAN_MAX_DURATION.get(user_plan, 1.0)

    # Continuous mode only for flagship plan
    if body.continuous_mode and user_plan != "flagship":
        raise HTTPException(
            status_code=403,
            detail=f"Continuous mode requires the flagship plan (your plan: {user_plan!r})",
        )

    effective_duration_hours: float = (
        body.duration_hours
        if body.duration_hours is not None
        else classification.estimated_duration_hours
    )
    if effective_duration_hours > max_duration:
        raise HTTPException(
            status_code=400,
            detail=f"duration_hours {effective_duration_hours} exceeds your plan maximum of {max_duration}h",
        )

    effective_budget_usd: float = (
        body.budget_usd
        if body.budget_usd is not None
        else float(classification.estimated_credits) * _CREDIT_USD_RATE
    )

    # ── BUG-0052 fix: Enforce budget bounds explicitly instead of silent clamping ──
    _BUDGET_MIN_USD = 0.10
    _PLAN_MAX_BUDGET: dict[str, float] = {
        "free": 5.0,
        "starter": 50.0,
        "pro": 500.0,
        "flagship": 10000.0,
    }
    _budget_max = _PLAN_MAX_BUDGET.get(user_plan, 5.0)
    if effective_budget_usd < _BUDGET_MIN_USD:
        raise HTTPException(
            status_code=400,
            detail=f"Budget ${effective_budget_usd:.2f} is below minimum ${_BUDGET_MIN_USD:.2f}",
        )
    if effective_budget_usd > _budget_max:
        raise HTTPException(
            status_code=400,
            detail=f"Budget ${effective_budget_usd:.2f} exceeds your plan maximum of ${_budget_max:.2f}",
        )

    # ── Reserve estimated credits up-front to prevent concurrent overspend ──
    # VULN-C2-01 fix: Enforce a minimum reservation equal to the tier's base
    # credit cost to prevent users from submitting with tiny budgets and
    # receiving underpaid model work.  The reservation is the greater of
    # (budget * 120) and the tier's base credit cost.
    tier_base_credits = _TIER_CREDITS.get(classification.tier, 50)
    estimated_credits_needed = max(
        int(effective_budget_usd * 120),
        tier_base_credits,
    )
    reserved_credits = 0
    if estimated_credits_needed > 0:
        # BUG-API-005: Three-state result. Distinguish insufficient-credits
        # (402) from transient RPC errors (503) so we never 402 a user whose
        # problem is actually service availability.
        reserved = await _supabase_deduct_credits(current_user["user_id"], estimated_credits_needed, cfg)
        if reserved == "insufficient":
            user_tokens = await _supabase_get_user_tokens(current_user["user_id"], cfg)
            available = user_tokens if user_tokens is not None else 0
            raise HTTPException(
                status_code=402,
                detail=(
                    f"Insufficient credits: you have {available} tokens but this "
                    f"investigation requires an estimated {estimated_credits_needed} tokens "
                    f"(budget ${effective_budget_usd:.2f} + 20% markup). "
                    "Please add credits or reduce the budget."
                ),
            )
        if reserved == "error":
            raise HTTPException(
                status_code=503,
                detail="Credit service unavailable; please retry shortly.",
            )
        reserved_credits = estimated_credits_needed

    try:
        # ── Move pending uploads to task directory ──────────────────────────────
        uploaded_file_names: list[str] = []
        if body.upload_session_uuid:
            session_uuid = _validate_upload_session_uuid(body.upload_session_uuid)
            pending_dir = Path(cfg.DATA_ROOT) / "uploads" / "pending" / session_uuid
            if pending_dir.is_dir():
                # Verify the upload session belongs to this user
                owner_meta = pending_dir / ".owner"
                if owner_meta.exists():
                    session_owner = owner_meta.read_text(encoding="utf-8").strip()
                    if session_owner != current_user["user_id"]:
                        raise HTTPException(
                            status_code=403,
                            detail="Upload session belongs to another user",
                        )
                # BUG-R13-02: Store in files/{task_id} so listing/download endpoints find them
                task_upload_dir = Path(cfg.DATA_ROOT) / "files" / task_id
                task_upload_dir.mkdir(parents=True, exist_ok=True)
                import shutil
                for f in pending_dir.iterdir():
                    if f.is_symlink():  # BUG-0008 fix: skip symlinks
                        continue
                    if f.is_file() and f.name != ".owner":
                        dest = task_upload_dir / f.name
                        shutil.move(str(f), str(dest))
                        uploaded_file_names.append(f.name)
                # Clean up pending directory (remove .owner metadata then dir)
                try:
                    owner_cleanup = pending_dir / ".owner"
                    if owner_cleanup.exists():
                        owner_cleanup.unlink()
                    pending_dir.rmdir()
                except OSError:
                    pass
                logger.info(
                    "pending_uploads_moved",
                    session_uuid=session_uuid,
                    task_id=task_id,
                    files=uploaded_file_names,
                )

        # M-09 fix: gate QA-bypass flags on admin role.  We read the role
        # off the authenticated user context (populated by Supabase auth
        # middleware) and also match against the configured ADMIN_USER_ID
        # for environments where the role claim is not yet provisioned.
        # BUG-0007 fix: admin-only QA-bypass flags are stripped for non-admin
        # users.  BUG-0006 fix: uses _is_admin_user() which handles empty
        # ADMIN_USER_ID safely.
        _is_admin: bool = (
            current_user.get("role") == "admin"
            or _is_admin_user(str(current_user.get("user_id", "")))
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
            "reserved_credits": reserved_credits,
            "uploaded_files": uploaded_file_names,
            # User flow control fields
            "quality_tier": body.quality_tier or "balanced",
            "user_flow_instructions": body.user_flow_instructions or "",
            "continuous_mode": body.continuous_mode,
            # M-09 fix: the QA-bypass flags (skip_tribunal / skip_skeptic /
            # dont_kill_branches / force_report_on_halt) skip important
            # critical-review gates.  Only honour them when the submitting
            # user is an admin; non-admin submissions get them forced False.
            "dont_kill_branches": bool(body.dont_kill_branches) if _is_admin else False,
            "force_report_on_halt": bool(body.force_report_on_halt) if _is_admin else False,
            "skip_skeptic": bool(body.skip_skeptic) if _is_admin else False,
            "skip_tribunal": bool(body.skip_tribunal) if _is_admin else False,
            "user_directives": body.user_directives or {},
        }

        # BUG-0051 fix: Write the task to DB synchronously BEFORE writing
        # the daemon file.  This ensures that a GET /api/investigations
        # immediately after POST sees the task (read-your-writes).
        db = _get_db()
        _parsed_created_at = datetime.fromisoformat(created_at)
        _task_model = _ResearchTask(
            id=task_id,
            topic=body.topic,
            budget_usd=effective_budget_usd,
            status=_TaskStatus.PENDING,
            current_state=_State.INIT,
            total_spent_usd=0.0,
            diminishing_flags=0,
            ai_call_counter=0,
            created_at=_parsed_created_at,
            metadata=task_payload,
        )
        await _db_insert_research_task(db, _task_model)

        inbox = Path(cfg.inbox_dir)
        inbox.mkdir(parents=True, exist_ok=True)
        task_file = inbox / f"{task_id}.task.json"
        # Atomic write: write to .tmp first, then rename to avoid daemon
        # reading a partially-written file.
        tmp_file = inbox / f"{task_id}.task.json.tmp"
        tmp_file.write_text(_json.dumps(task_payload, indent=2), encoding="utf-8")
        tmp_file.rename(task_file)
        logger.info(
            "task_submitted",
            task_id=task_id,
            topic=body.topic[:80],
            tier=classification.tier,
            user_id=current_user["user_id"],
            reserved_credits=reserved_credits,
        )

        # BUG-API-012: Verify the caller owns this conversation before linking
        # the task to it; otherwise a user could link their task to another
        # user's conversation, leaking graph/context into the wrong UI. We
        # query Supabase filtered by both id and user_id; ownership is
        # confirmed iff the query returns exactly one row.
        if body.conversation_id:
            conversation_owned = False
            try:
                # Validate UUID shape before hitting PostgREST (avoids confusing 400s).
                uuid.UUID(body.conversation_id)
                ownership_resp = await _supabase_rest_system(
                    cfg, "GET", "/conversations",
                    params={
                        "id": f"eq.{body.conversation_id}",
                        "user_id": f"eq.{current_user['user_id']}",
                        "select": "id",
                        "limit": "1",
                    },
                )
                if ownership_resp.status_code == 200:
                    rows = ownership_resp.json()
                    conversation_owned = bool(rows)
                else:
                    logger.warning(
                        "investigation_conversation_ownership_check_failed",
                        status=ownership_resp.status_code,
                    )
            except ValueError:
                logger.warning(
                    "investigation_conversation_invalid_uuid",
                    conversation_id=body.conversation_id,
                )
            except Exception as ownership_err:  # noqa: BLE001
                logger.warning(
                    "investigation_conversation_ownership_check_failed",
                    error=str(ownership_err),
                )

            if conversation_owned:
                try:
                    await _supabase_rest_system(
                        cfg, "PATCH", "/investigations",
                        json={"conversation_id": body.conversation_id},
                        params={"task_id": f"eq.{task_id}"},
                    )
                except Exception as link_err:  # noqa: BLE001
                    logger.warning("investigation_conversation_link_failed", error=str(link_err))
            else:
                logger.warning(
                    "investigation_conversation_link_rejected",
                    conversation_id=body.conversation_id,
                    user_id=current_user["user_id"],
                    reason="conversation_not_owned_by_user",
                )
    except HTTPException:
        # BUG-API-035: Wrap refund in inner try/except so a refund failure
        # never masks the original HTTPException the user was about to see.
        if reserved_credits > 0:
            try:
                await _supabase_add_credits(current_user["user_id"], reserved_credits, cfg)
            except Exception as refund_err:  # noqa: BLE001
                logger.error(
                    "refund_after_http_exception_failed",
                    user_id=current_user["user_id"],
                    amount=reserved_credits,
                    error=str(refund_err),
                )
        raise
    except OSError as exc:
        if reserved_credits > 0:
            try:
                await _supabase_add_credits(current_user["user_id"], reserved_credits, cfg)
            except Exception as refund_err:  # noqa: BLE001
                logger.error(
                    "refund_after_oserror_failed",
                    user_id=current_user["user_id"],
                    amount=reserved_credits,
                    error=str(refund_err),
                )
        # M-01 fix: do not leak raw OSError details (which can include file
        # paths, permission info, disk-space info) to the client.  The full
        # exception is logged server-side; the client gets a generic message.
        logger.error("task_write_failed", error=str(exc))
        raise HTTPException(
            status_code=500,
            detail="Failed to submit investigation. Please try again.",
        ) from exc
    except Exception:
        if reserved_credits > 0:
            try:
                await _supabase_add_credits(current_user["user_id"], reserved_credits, cfg)
            except Exception as refund_err:  # noqa: BLE001
                logger.error(
                    "refund_after_unexpected_exception_failed",
                    user_id=current_user["user_id"],
                    amount=reserved_credits,
                    error=str(refund_err),
                )
        raise

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

    # BUG-API-024: Validate the status filter against known values so callers
    # get a helpful 400 instead of a silent 0-result response.
    if status:
        normalized_status = status.upper()
        if normalized_status not in _VALID_TASK_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid status {status!r}. Must be one of: "
                    f"{sorted(_VALID_TASK_STATUSES)}"
                ),
            )
        status = normalized_status

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
    # ADV-FIX: Validate UUID format before DB query.
    try:
        uuid.UUID(task_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid task ID format")
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
        try:  # BUG-API-036: Guard against malformed JSON in metadata
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    task_user_id = metadata.get("user_id", "")
    if not _is_admin_user(current_user["user_id"]) and task_user_id != current_user["user_id"]:
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
    task_id = _validate_task_id(task_id)  # BUG-API-001: reject non-UUID before DB

    # BUG-S3-01 fix: Verify ownership before allowing kill.
    row = await db.fetchrow(
        "SELECT metadata FROM research_tasks WHERE id = $1",
        task_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    task_user_id = metadata.get("user_id", "")
    if not _is_admin_user(current_user["user_id"]) and task_user_id != current_user["user_id"]:
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


@app.post(
    "/api/investigations/{task_id}/stop",
    response_model=KillTaskResponse,
    tags=["Investigations"],
)
async def stop_investigation(
    task_id: str,
    current_user: dict[str, str] = Depends(_get_current_user),
) -> KillTaskResponse:
    """
    Manually stop a running investigation that is in continuous mode.

    Sets a Redis key ``stop:{task_id}`` (24 h TTL) that the event loop
    reads before each continuous-mode restart.  Also marks the task
    status as HALTED so it does not restart on a daemon reload.

    Unlike ``/kill``, this endpoint is designed for graceful termination
    of continuous-mode loops — it waits for the current research cycle
    to finish rather than interrupting mid-cycle.
    """
    db = _get_db()
    task_id = _validate_task_id(task_id)  # BUG-API-001: reject non-UUID before DB

    # Verify ownership
    row = await db.fetchrow(
        "SELECT metadata, status FROM research_tasks WHERE id = $1",
        task_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    task_user_id = metadata.get("user_id", "")
    if not _is_admin_user(current_user["user_id"]) and task_user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="You do not own this investigation")

    # BUG-API-011: Return 409 when task is already in a terminal state (matches kill_investigation behavior)
    current_status = row.get("status", "")
    if current_status not in ("RUNNING", "PENDING"):
        return KillTaskResponse(
            message=f"Investigation is already {current_status}; stop signal is a no-op",
            task_id=task_id,
        )

    # Set the Redis stop flag with a 24-hour TTL so the event loop
    # will not restart the continuous loop on its next HALT.
    if _redis is not None:
        try:
            await _redis.set(f"stop:{task_id}", "1", ex=86400)
            logger.info("stop_flag_set", task_id=task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("stop_flag_set_failed", task_id=task_id, error=str(exc))
    else:
        logger.warning("stop_flag_no_redis", task_id=task_id)

    # Also mark the task HALTED in DB so it won't be resumed on daemon restart.
    await db.execute(
        "UPDATE research_tasks SET status = 'HALTED', completed_at = NOW() "
        "WHERE id = $1 AND status IN ('RUNNING', 'PENDING')",
        task_id,
    )

    logger.info("task_stop_requested", task_id=task_id)
    return KillTaskResponse(task_id=task_id, message="Stop signal sent; investigation will halt after current cycle")


@app.delete(
    "/api/investigations/{task_id}",
    tags=["Investigations"],
    summary="Delete an investigation and all related data",
)
async def delete_investigation(
    task_id: str,
    current_user: dict[str, str] = Depends(_get_current_user),
) -> dict:
    """
    Permanently delete an investigation and all associated intelligence data.

    Only the owner can delete their investigations.  Running investigations
    are killed first before deletion.
    """
    pool = _get_db()
    user_id = current_user["user_id"]
    task_id = _validate_task_id(task_id)  # BUG-API-001: reject non-UUID before DB
    _log = logger.bind(task_id=task_id, user_id=user_id)

    # Verify the investigation exists and belongs to this user
    # P0-FIX-1: Use metadata->>'user_id' (canonical owner source) instead of
    # the top-level user_id column, which can be empty for legacy rows.
    row = await pool.fetchrow(
        "SELECT id, status, metadata FROM research_tasks WHERE id = $1",
        task_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Investigation not found")

    metadata = row["metadata"] or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    row_user_id = str(metadata.get("user_id", "")).strip()
    # BUG-API-017 fix: Treat missing owner as admin-only. Previously, empty
    # user_id let anyone delete legacy tasks.
    if not _is_admin_user(user_id) and row_user_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this investigation")

    # If still running, kill it first
    if row["status"] in ("RUNNING", "PENDING"):
        if _redis is not None:
            try:
                await _redis.publish(f"kill:{task_id}", "1")
                # Also set the stop key so continuous-mode checks pick it up
                await _redis.set(f"stop:{task_id}", "1", ex=3600)
            except Exception:  # noqa: BLE001
                pass
        await pool.execute(
            "UPDATE research_tasks SET status = 'FAILED', completed_at = NOW() "
            "WHERE id = $1 AND status IN ('RUNNING', 'PENDING')",
            task_id,
        )

    # BUG-0025 fix: Only delete from tables that actually exist in the schema.
    # Removed non-existent tables: replan_modifications, retrieval_coverage,
    # diversity_assessments, confidence_calibrations, temporal_tags,
    # source_credibility_scores, research_findings, research_branches, task_logs.
    # Fixed: research_findings → findings, research_branches → branches.
    # Order matters — delete children before parent.
    cascade_tables = [
        # Intelligence / analysis tables
        "executive_summaries",
        "audit_results",
        "perspective_syntheses",
        "gap_analyses",
        "hypothesis_priors",
        "contradiction_pairs",
        "claims",
        "source_scores",
        # Learning / outcome tables
        "learning_events",
        "investigation_outcomes",
        "learning_insights",
        # Session / report tables
        "ai_sessions",
        "evaluation_results",
        "report_generations",
        "tribunal_sessions",
        "skeptic_results",
        "orchestrator_handoffs",
        "research_plans",
        "checkpoints",
        # Graph data
        "graph_edges",
        "graph_nodes",
        # Core entities (children of research_tasks)
        "sources",
        "findings",
        "branches",
        "hypotheses",
    ]
    for table in cascade_tables:
        try:
            await pool.execute(f"DELETE FROM {table} WHERE task_id = $1", task_id)  # noqa: S608
        except Exception:  # noqa: BLE001
            pass

    # BUG-0033 fix: Use DELETE ... RETURNING to atomically claim the row.
    # If a concurrent DELETE already removed it, deleted_row is None and we
    # return 404 instead of a misleading 200.
    deleted_row = await pool.fetchrow(
        "DELETE FROM research_tasks WHERE id = $1 RETURNING id",
        task_id,
    )
    if not deleted_row:
        raise HTTPException(status_code=404, detail="Investigation already deleted")

    _log.info("investigation_deleted")
    return {"status": "deleted", "task_id": task_id}


# ---------------------------------------------------------------------------
# Routes — Branches
# ---------------------------------------------------------------------------


@app.get(
    "/api/investigations/{task_id}/branches",
    response_model=list[BranchSummary],
    tags=["Branches"],
)
async def list_branches(
    task_id: str,
    _: dict[str, str] = Depends(_require_investigation_owner),
) -> list[BranchSummary]:
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
    _: dict[str, str] = Depends(_require_investigation_owner),
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
async def get_cost_breakdown(
    task_id: str,
    _: dict[str, str] = Depends(_require_investigation_owner),
) -> CostBreakdown:
    """Return a detailed cost breakdown for an investigation."""
    db = _get_db()
    # BUG-API-018: The ``_require_investigation_owner`` dependency has already
    # confirmed the row exists and is owned by the caller. We fetch only the
    # columns we need here (budget + total spent) and do not repeat the 404
    # guard — doing so produced a race window where the dep passed 200 but
    # this endpoint returned 404 if the task vanished in between.
    task_row = await db.fetchrow(
        "SELECT budget_usd, total_spent_usd FROM research_tasks WHERE id = $1",
        task_id,
    )
    if task_row is None:
        # Extremely narrow race: row deleted between dep and this query.
        # Return zeroes rather than 404 so clients don't see inconsistent
        # status codes from the same request.
        return CostBreakdown(
            task_id=task_id,
            total_spent_usd=0.0,
            budget_usd=0.0,
            budget_remaining_usd=0.0,
            ai_call_count=0,
            per_model={},
            per_branch={},
        )

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
    # BUG-API-044: model_used may be NULL — use "unknown" as key to avoid JSON serialization error
    per_model = {(r["model_used"] or "unknown"): float(r["total_cost"] or 0.0) for r in model_rows}

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
# Routes — Investigation knowledge graph
# ---------------------------------------------------------------------------


def _row_to_graph_node(row: asyncpg.Record) -> GraphNode:
    """Convert a raw graph_nodes DB row to a GraphNode response model."""
    raw_meta = row["metadata"] or {}
    if isinstance(raw_meta, str):
        try:
            raw_meta = json.loads(raw_meta)
        except (json.JSONDecodeError, ValueError):
            raw_meta = {}
    return GraphNode(
        id=str(row["id"]),
        label=row["label"],
        type=row["type"],
        description=row["description"] or "",
        metadata=raw_meta,
        x=row["x"],
        y=row["y"],
        source=row["source"] or "ai",
    )


def _row_to_graph_edge(row: asyncpg.Record) -> GraphEdge:
    """Convert a raw graph_edges DB row to a GraphEdge response model.

    Maps DB columns ``source_node`` / ``target_node`` back to the D3-style
    ``source`` / ``target`` fields expected by the frontend.
    """
    raw_meta = row["metadata"] or {}
    if isinstance(raw_meta, str):
        try:
            raw_meta = json.loads(raw_meta)
        except (json.JSONDecodeError, ValueError):
            raw_meta = {}
    return GraphEdge(
        id=str(row["id"]),
        source=str(row["source_node"]),
        target=str(row["target_node"]),
        label=row["label"] or "",
        metadata=raw_meta,
        source_origin=row["source"] or "ai",
    )


@app.get(
    "/api/investigations/{task_id}/graph",
    response_model=GraphData,
    tags=["Graph"],
    summary="Retrieve the investigation knowledge graph",
)
async def get_investigation_graph(
    task_id: str,
    _: dict[str, str] = Depends(_require_investigation_owner),
) -> GraphData:
    """Return all graph nodes and edges recorded for a given investigation.

    Returns 404 if the task does not exist and 403 if the caller is not the
    task owner.  Both checks are handled by the ``_require_investigation_owner``
    dependency.
    """
    db = _get_db()

    node_rows = await db.fetch(
        """
        SELECT id, label, type, description, metadata, x, y, source
        FROM graph_nodes
        WHERE task_id = $1
        ORDER BY created_at ASC
        """,
        task_id,
    )

    edge_rows = await db.fetch(
        """
        SELECT id, source_node, target_node, label, metadata, source
        FROM graph_edges
        WHERE task_id = $1
        ORDER BY created_at ASC
        """,
        task_id,
    )

    return GraphData(
        nodes=[_row_to_graph_node(r) for r in node_rows],
        edges=[_row_to_graph_edge(r) for r in edge_rows],
    )


@app.post(
    "/api/investigations/{task_id}/graph",
    response_model=GraphData,
    tags=["Graph"],
    summary="Upsert graph nodes and edges for an investigation",
)
async def upsert_investigation_graph(
    task_id: str,
    body: GraphData,
    _: dict[str, str] = Depends(_require_investigation_owner),
) -> GraphData:
    """Upsert a batch of nodes and edges into the investigation graph.

    Both nodes and edges use ``ON CONFLICT (id) DO UPDATE`` semantics so
    callers can safely re-send the same payload without creating duplicates.
    The D3-style ``source`` / ``target`` fields on edges are mapped to the
    ``source_node`` / ``target_node`` DB columns transparently.

    Returns the full graph state after the upsert (all nodes + edges for
    the task, not just the ones in the request body).
    """
    db = _get_db()

    # BUG-API-042: cap batch sizes so a malicious or buggy client cannot
    # flood the graph tables in one request. 500 is generous for a UI batch
    # but well under Postgres statement / payload limits.
    _MAX_GRAPH_BATCH = 500
    if len(body.nodes) > _MAX_GRAPH_BATCH or len(body.edges) > _MAX_GRAPH_BATCH:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Graph upsert batch too large: max {_MAX_GRAPH_BATCH} nodes "
                f"and {_MAX_GRAPH_BATCH} edges per request "
                f"(got {len(body.nodes)} nodes, {len(body.edges)} edges)."
            ),
        )

    # BUG-API-034: before ON CONFLICT overwrites, verify that any existing
    # nodes/edges with the submitted IDs already belong to this task_id.
    # Otherwise a caller could supply an ID that collides with a node/edge
    # owned by a different investigation and clobber it, bypassing the
    # per-task ownership check enforced by ``_require_investigation_owner``.
    _node_ids = [n.id for n in body.nodes]
    _edge_ids = [e.id for e in body.edges]

    async with db.acquire() as conn:
        async with conn.transaction():
            # BUG-API-034: cross-task ownership preflight.
            if _node_ids:
                conflict_rows = await conn.fetch(
                    """
                    SELECT id FROM graph_nodes
                    WHERE id = ANY($1::text[]) AND task_id <> $2
                    """,
                    _node_ids,
                    task_id,
                )
                if conflict_rows:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "One or more node IDs already exist under a "
                            "different investigation and cannot be overwritten."
                        ),
                    )
            if _edge_ids:
                conflict_rows = await conn.fetch(
                    """
                    SELECT id FROM graph_edges
                    WHERE id = ANY($1::text[]) AND task_id <> $2
                    """,
                    _edge_ids,
                    task_id,
                )
                if conflict_rows:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "One or more edge IDs already exist under a "
                            "different investigation and cannot be overwritten."
                        ),
                    )

            # ── Upsert nodes ───────────────────────────────────────────────
            for node in body.nodes:
                await conn.execute(
                    """
                    INSERT INTO graph_nodes (
                        id, task_id, label, type, description,
                        metadata, x, y, source
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (id) DO UPDATE
                        SET label       = EXCLUDED.label,
                            type        = EXCLUDED.type,
                            description = EXCLUDED.description,
                            metadata    = EXCLUDED.metadata,
                            x           = EXCLUDED.x,
                            y           = EXCLUDED.y,
                            source      = EXCLUDED.source
                    """,
                    node.id,
                    task_id,
                    node.label,
                    node.type,
                    node.description,
                    json.dumps(node.metadata),
                    node.x,
                    node.y,
                    node.source,
                )

            # ── Upsert edges ───────────────────────────────────────────────
            # D3 ``source`` / ``target`` fields map to DB ``source_node`` /
            # ``target_node``; ``source_origin`` maps to the DB ``source``
            # provenance column.
            for edge in body.edges:
                await conn.execute(
                    """
                    INSERT INTO graph_edges (
                        id, task_id, source_node, target_node,
                        label, metadata, source
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (id) DO UPDATE
                        SET source_node = EXCLUDED.source_node,
                            target_node = EXCLUDED.target_node,
                            label       = EXCLUDED.label,
                            metadata    = EXCLUDED.metadata,
                            source      = EXCLUDED.source
                    """,
                    edge.id,
                    task_id,
                    edge.source,      # D3 source → source_node
                    edge.target,      # D3 target → target_node
                    edge.label,
                    json.dumps(edge.metadata),
                    edge.source_origin,  # provenance → source column
                )

    # Return the full current graph state for the task
    node_rows = await db.fetch(
        """
        SELECT id, label, type, description, metadata, x, y, source
        FROM graph_nodes
        WHERE task_id = $1
        ORDER BY created_at ASC
        """,
        task_id,
    )
    edge_rows = await db.fetch(
        """
        SELECT id, source_node, target_node, label, metadata, source
        FROM graph_edges
        WHERE task_id = $1
        ORDER BY created_at ASC
        """,
        task_id,
    )
    return GraphData(
        nodes=[_row_to_graph_node(r) for r in node_rows],
        edges=[_row_to_graph_edge(r) for r in edge_rows],
    )


# ---------------------------------------------------------------------------
# Routes — SSE log stream
# ---------------------------------------------------------------------------


@app.post(
    "/api/investigations/{task_id}/stream-token",
    tags=["Logs"],
    summary="Mint a short-lived stream token for SSE log streaming",
)
async def create_stream_token(
    task_id: str,
    current_user: dict[str, str] = Depends(_require_investigation_owner),
) -> dict[str, Any]:
    """Issue a short-lived HMAC-signed token for the SSE log endpoint.

    The token is bound to the specific task and user, expires in 2 minutes,
    and cannot be used for any other API endpoint. This avoids exposing the
    full JWT bearer token in the SSE query string.
    """
    token = _mint_stream_token(current_user["user_id"], task_id)
    return {"stream_token": token, "expires_in_seconds": _STREAM_TOKEN_TTL_SECONDS}


@app.get(
    "/api/investigations/{task_id}/logs",
    tags=["Logs"],
    summary="Stream real-time log events via Server-Sent Events",
)
async def stream_logs(
    request: Request,
    task_id: str,
    format: str | None = Query(None, description="Set to 'legacy' for plain text events"),
    auth_context: dict[str, str] = Depends(_authenticate_stream_token_or_header),
) -> EventSourceResponse:
    """
    Subscribe to live log events for a running investigation.

    Uses Redis pub/sub on the channel ``logs:<task_id>``.  The orchestrator
    publishes structured JSON log lines there; this endpoint re-broadcasts
    them as SSE events.

    Falls back to polling the DB ``task_logs`` table if Redis is unavailable.
    """

    use_legacy = format == "legacy"
    # Stream tokens are short-lived and single-purpose — no JWT to re-validate.
    # Periodic re-check verifies the task still exists and user still owns it.
    _sse_user_id = auth_context["user_id"]
    auth_recheck_interval_seconds = 30.0

    async def _event_generator() -> AsyncIterator[dict[str, str]]:
        last_auth_check = time.monotonic()
        # BUG-API-008: throttle DB status polls in the SSE pub/sub loop so that
        # each idle subscriber no longer produces ~1 query/second against the
        # pool. We still send heartbeats every ``timeout`` seconds.
        last_db_check = 0.0
        db_poll_interval_seconds = 10.0
        if _redis is not None:
            # ── Redis pub/sub path ──────────────────────────────────────
            pubsub = _redis.pubsub()
            await pubsub.subscribe(f"logs:{task_id}")
            try:
                # BUG-D6-01: Replay fast-path answer if the task completed before
                # the SSE subscription was established.  Quick-tier fast-path events
                # are emitted via pub/sub (transient) and may be lost if the frontend
                # connects after the orchestrator finishes.  The answer is persisted
                # in task.metadata["fast_path_answer"] by the orchestrator.
                _initial_replay_done = False
                if _db_pool is not None:
                    try:
                        _replay_row = await _db_pool.fetchrow(
                            "SELECT status, metadata FROM research_tasks WHERE id = $1",
                            task_id,
                        )
                        if _replay_row is not None and _replay_row["status"] in ("COMPLETED", "FAILED", "HALTED"):
                            _replay_meta = _replay_row.get("metadata") or {}
                            if isinstance(_replay_meta, str):
                                _replay_meta = json.loads(_replay_meta)
                            _fast_answer = _replay_meta.get("fast_path_answer")
                            if _fast_answer:
                                yield {"data": json.dumps({"type": "text", "content": _fast_answer}), "event": "log"}
                            yield {
                                "data": json.dumps({"task_id": task_id, "final_status": _replay_row["status"]}),
                                "event": "done",
                            }
                            _initial_replay_done = True
                    except Exception:
                        pass  # Non-fatal — fall through to normal pub/sub loop
                if _initial_replay_done:
                    return

                while True:
                    if await request.is_disconnected():
                        break
                    if time.monotonic() - last_auth_check >= auth_recheck_interval_seconds:
                        # BUG-API-009: distinguish permanent auth failures (task
                        # deleted / revoked owner) from transient DB errors; on
                        # transient errors we log and retry on the next cycle
                        # instead of swallowing silently.
                        try:
                            db = _get_db()
                            row = await db.fetchrow("SELECT metadata FROM research_tasks WHERE id = $1", task_id)
                            if row is None:
                                yield {"data": json.dumps({"error": "task_deleted"}), "event": "error"}
                                break
                            meta = row.get("metadata") or {}
                            if isinstance(meta, str):
                                try:
                                    meta = json.loads(meta)
                                except (json.JSONDecodeError, TypeError):
                                    meta = {}
                            if not _is_admin_user(_sse_user_id) and meta.get("user_id") != _sse_user_id:
                                yield {"data": json.dumps({"error": "authentication_revoked"}), "event": "error"}
                                break
                        except HTTPException as exc:
                            # _get_db() raises HTTPException(503) when the pool is
                            # missing — treat as transient and retry next cycle.
                            logger.warning(
                                "sse_auth_recheck_transient",
                                task_id=task_id,
                                user_id=_sse_user_id,
                                status=exc.status_code,
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "sse_auth_recheck_failed",
                                task_id=task_id,
                                user_id=_sse_user_id,
                                error=str(exc),
                            )
                        last_auth_check = time.monotonic()
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=1.0,
                    )
                    if message is not None:
                        # BUG-API-026: defensively coerce to str in case the Redis
                        # client was constructed without decode_responses=True.
                        raw_data = message.get("data", "")
                        if isinstance(raw_data, (bytes, bytearray)):
                            try:
                                raw_data = raw_data.decode("utf-8")
                            except UnicodeDecodeError:
                                raw_data = raw_data.decode("utf-8", errors="replace")
                        elif not isinstance(raw_data, str):
                            raw_data = str(raw_data)
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
                        # BUG-API-008: only poll the DB every ``db_poll_interval_seconds``
                        # to avoid hammering the pool with idle subscribers.
                        now_mono = time.monotonic()
                        if now_mono - last_db_check >= db_poll_interval_seconds:
                            last_db_check = now_mono
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
                if time.monotonic() - last_auth_check >= auth_recheck_interval_seconds:
                    # BUG-API-010 / BUG-API-009: log transient errors so they
                    # are not silently masked; retry on the next cycle.
                    try:
                        row = await db.fetchrow("SELECT metadata FROM research_tasks WHERE id = $1", task_id)
                        if row is None:
                            yield {"data": json.dumps({"error": "task_deleted"}), "event": "error"}
                            break
                        meta = row.get("metadata") or {}
                        if isinstance(meta, str):
                            try:
                                meta = json.loads(meta)
                            except (json.JSONDecodeError, TypeError):
                                meta = {}
                        if not _is_admin_user(_sse_user_id) and meta.get("user_id") != _sse_user_id:
                            yield {"data": json.dumps({"error": "authentication_revoked"}), "event": "error"}
                            break
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "sse_auth_recheck_failed",
                            task_id=task_id,
                            user_id=_sse_user_id,
                            error=str(exc),
                        )
                    last_auth_check = time.monotonic()
                row = await db.fetchrow(
                    "SELECT status, current_state, total_spent_usd, "
                    "output_pdf_path, output_docx_path "
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
                            "output_pdf_path": row.get("output_pdf_path"),
                            "output_docx_path": row.get("output_docx_path"),
                            "ts": datetime.now(tz=timezone.utc).isoformat(),
                        }),
                        "event": "state_change",
                    }
                if row["status"] in ("COMPLETED", "FAILED", "HALTED"):
                    yield {
                        "data": json.dumps({
                            "task_id": task_id,
                            "final_status": row["status"],
                            "output_pdf_path": row.get("output_pdf_path"),
                            "output_docx_path": row.get("output_docx_path"),
                        }),
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
async def download_report_pdf(
    task_id: str,
    _: dict[str, str] = Depends(_require_investigation_owner),
) -> FileResponse:
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
    # BUG-API-022: explicitly refuse to follow symlinks on the candidate
    # path. ``resolve()`` transparently follows symlinks, so a symlink
    # inside DATA_ROOT that points to ``/etc/passwd`` would resolve to
    # ``/etc/passwd`` — the ``is_relative_to`` check below will catch
    # that case, but we defend in depth by rejecting any symlink
    # component before the final resolve to avoid accidentally exposing
    # host filesystem state even for paths technically under DATA_ROOT
    # via a symlink that was planted by another process.
    cfg = _get_config()
    candidate = Path(pdf_path)
    if candidate.is_symlink() or any(p.is_symlink() for p in candidate.parents if p.exists()):
        logger.warning("pdf_path_is_symlink", task_id=task_id, path=str(candidate))
        raise HTTPException(
            status_code=403,
            detail="Access denied: report path contains a symlink",
        )
    resolved = candidate.resolve()
    data_root = Path(cfg.DATA_ROOT).resolve()
    if not resolved.is_relative_to(data_root):
        raise HTTPException(
            status_code=403,
            detail="Access denied: report path is outside the data root",
        )

    if not resolved.is_file():
        logger.warning("pdf_not_found", task_id=task_id, path=str(resolved))
        raise HTTPException(
            status_code=404,
            detail="Report PDF file is not available. It may still be generating or was removed.",
        )
    # BUG-0008 fix: reject symlinks in report download
    if resolved.is_symlink():
        raise HTTPException(status_code=403, detail="Access denied: symlinks are not allowed")

    # BUG-API-047: `FileResponse` already emits a correctly-encoded
    # Content-Disposition header when ``filename`` is passed (it uses
    # RFC 5987 / RFC 6266 filename* encoding for non-ASCII names).  The
    # previous explicit ``headers=`` override re-inserted the filename
    # unquoted, which breaks for names containing spaces, quotes, or
    # non-ASCII characters.  We drop the manual header and rely on
    # Starlette's built-in encoding instead.
    filename = resolved.name
    return FileResponse(
        path=str(resolved),
        media_type="application/pdf",
        filename=filename,
    )


@app.get(
    "/api/investigations/{task_id}/report/docx",
    tags=["Reports"],
    summary="Download the DOCX report (future capability)",
)
async def download_report_docx(
    task_id: str,
    _: dict[str, str] = Depends(_require_investigation_owner),
) -> FileResponse:
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
    # BUG-API-022: explicit symlink rejection for defense in depth — see
    # the same comment in ``download_report_pdf`` above.
    cfg = _get_config()
    candidate = Path(docx_path)
    if candidate.is_symlink() or any(p.is_symlink() for p in candidate.parents if p.exists()):
        logger.warning("docx_path_is_symlink", task_id=task_id, path=str(candidate))
        raise HTTPException(
            status_code=403,
            detail="Access denied: report path contains a symlink",
        )
    resolved = candidate.resolve()
    data_root = Path(cfg.DATA_ROOT).resolve()
    if not resolved.is_relative_to(data_root):
        raise HTTPException(
            status_code=403,
            detail="Access denied: report path is outside the data root",
        )

    if not resolved.is_file():
        logger.warning("docx_not_found", task_id=task_id, path=str(resolved))
        raise HTTPException(
            status_code=404,
            detail="Report DOCX file is not available. It may still be generating or was removed.",
        )

    # BUG-API-047: trust Starlette's FileResponse to emit the
    # Content-Disposition header. Removing the manual override avoids
    # duplicate headers and header-injection via filenames containing
    # quotes or CRLF.
    filename = resolved.name
    return FileResponse(
        path=str(resolved),
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        filename=filename,
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
    task_id = _validate_task_id(task_id)  # BUG-API-001: reject non-UUID before DB/Path

    # Verify user owns the investigation
    row = await db.fetchrow(
        "SELECT metadata FROM research_tasks WHERE id = $1",
        task_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    task_user_id = metadata.get("user_id", "")
    if not _is_admin_user(current_user["user_id"]) and task_user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="You do not own this investigation")

    files_dir = Path(cfg.DATA_ROOT) / "files" / task_id
    if not files_dir.is_dir():
        return []

    result: list[FileAttachmentInfo] = []
    for f in sorted(files_dir.iterdir()):
        try:  # BUG-API-037: guard against OSError if file vanishes between iterdir() and stat()
            if f.is_symlink():  # BUG-0008 fix: skip symlinks
                continue
            if f.is_file():
                stat = f.stat()
                suffix = f.suffix.lower()
                result.append(FileAttachmentInfo(
                    filename=f.name,
                    size=stat.st_size,
                    mime=_MIME_MAP.get(suffix, "application/octet-stream"),
                    created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                ))
        except OSError:
            continue  # file disappeared between iterdir() and stat()
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
    task_id = _validate_task_id(task_id)  # BUG-API-001: reject non-UUID before DB/Path

    # Verify user owns the investigation
    row = await db.fetchrow(
        "SELECT metadata FROM research_tasks WHERE id = $1",
        task_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    task_user_id = metadata.get("user_id", "")
    if not _is_admin_user(current_user["user_id"]) and task_user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="You do not own this investigation")

    files_dir = Path(cfg.DATA_ROOT) / "files" / task_id
    files_root = files_dir.resolve()
    file_path = (files_dir / filename).resolve()

    # Path traversal protection — the requested file must stay inside this task's
    # own artifact directory, not merely somewhere under DATA_ROOT.
    if not file_path.is_relative_to(files_root):
        raise HTTPException(status_code=403, detail="Access denied: path outside investigation files directory")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"File {filename!r} not found")
    # BUG-0008 fix: reject symlinks in file download
    if file_path.is_symlink():
        raise HTTPException(status_code=403, detail="Access denied: symlinks are not allowed")

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
# SEC-E3-R1-02: Per-target lock to serialize file-count-and-write, preventing
# parallel requests from bypassing the file cap via TOCTOU race.
_upload_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def _get_upload_lock(target_id: str) -> asyncio.Lock:
    """Return a per-target asyncio.Lock for serializing upload file-count checks.

    Uses a weak-value cache so one-off upload targets do not accumulate in a
    process-long dictionary after their requests finish.
    """
    lock = _upload_locks.get(target_id)
    if lock is None:
        lock = asyncio.Lock()
        _upload_locks[target_id] = lock
    return lock
_UPLOAD_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".txt", ".md", ".csv", ".json", ".html",
    ".png", ".jpg", ".jpeg", ".xlsx", ".docx",
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
    ".jpeg": "image/jpeg",
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


def _validate_task_id(task_id: str) -> str:
    """Validate that a task_id is a proper UUID.  Raises 400 otherwise.

    BUG-API-001 fix: Several task-scoped endpoints passed raw task_id to
    asyncpg without UUID validation, causing 500 on malformed inputs.

    BUG-API-002 fix: Also blocks path traversal via ``task_id`` in
    endpoints that build ``Path(DATA_ROOT) / "files" / task_id``. A
    malicious value like ``"../../etc"`` no longer escapes because the
    UUID parser rejects it here before any ``Path()`` composition.
    """
    try:
        return str(uuid.UUID(task_id))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid task ID format") from exc


def _validate_upload_session_uuid(session_uuid: str) -> str:
    """Validate and normalize a pending-upload session UUID."""
    try:
        return str(uuid.UUID(session_uuid))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid upload session UUID") from exc


@app.post(
    "/api/investigations/{task_id}/upload",
    response_model=UploadResponse,
    tags=["Uploads"],
    summary="Upload files to an existing investigation",
)
async def upload_investigation_files(
    request: Request,
    task_id: str,
    files: Annotated[list[UploadFile], File()],
    current_user: dict[str, str] = Depends(_get_current_user),
) -> UploadResponse:
    """Upload files to an existing investigation.

    Max 10MB per file, max 5 files per investigation.
    Supported types: .pdf, .txt, .md, .csv, .json, .html, .png, .jpg, .xlsx, .docx
    """
    cfg = _get_config()
    db = _get_db()
    task_id = _validate_task_id(task_id)  # BUG-API-001: reject non-UUID before DB/Path

    # Verify user owns the investigation
    row = await db.fetchrow(
        "SELECT metadata FROM research_tasks WHERE id = $1",
        task_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    task_user_id = metadata.get("user_id", "")
    if not _is_admin_user(current_user["user_id"]) and task_user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="You do not own this investigation")

    if len(files) > _UPLOAD_MAX_FILES_PER_INVESTIGATION:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {_UPLOAD_MAX_FILES_PER_INVESTIGATION} files per upload",
        )

    # BUG-R13-02: Store in files/{task_id} so listing/download endpoints find them
    upload_dir = Path(cfg.DATA_ROOT) / "files" / task_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    # SEC-E3-R1-02: Serialize count-check-and-write to prevent TOCTOU race
    async with _get_upload_lock(task_id):
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

            # BUG-API-007: Stream read in chunks; abort as soon as size exceeds
            # the cap to avoid memory exhaustion from large uploads.
            size = 0
            chunks: list[bytes] = []
            while True:
                chunk = await upload_file.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > _UPLOAD_MAX_FILE_SIZE:
                    raise HTTPException(
                        status_code=400,
                        detail=f"File {filename!r} exceeds {_UPLOAD_MAX_FILE_SIZE // (1024*1024)}MB limit",
                    )
                chunks.append(chunk)
            content = b"".join(chunks)

            # P1-FIX-46: Sanitize filename — strip path components first,
            # then reject dotfiles and traversal names.
            safe_name = os.path.basename(re.sub(r"[^\w\-.]", "_", filename))
            if not safe_name or safe_name.startswith(".") or safe_name in (".", ".."):
                raise HTTPException(status_code=400, detail=f"Invalid filename: {filename!r}")
            dest = upload_dir / safe_name
            # Ensure resolved path is within the upload directory
            if not str(dest.resolve()).startswith(str(upload_dir.resolve())):
                raise HTTPException(status_code=400, detail=f"Invalid filename: {filename!r}")
            # BUG-0034 fix: append counter suffix on duplicate filenames
            if dest.exists():
                stem = Path(safe_name).stem
                ext = Path(safe_name).suffix
                counter = 1
                while dest.exists():
                    safe_name = f"{stem}_{counter}{ext}"
                    dest = upload_dir / safe_name
                    counter += 1
            dest.write_bytes(content)
            # BUG-0008 fix: reject symlinks after write (race-safe check)
            if dest.is_symlink():
                dest.unlink()
                raise HTTPException(status_code=400, detail=f"Symlinks are not allowed: {safe_name!r}")

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
    request: Request,
    files: Annotated[list[UploadFile], File()],
    session_uuid: str | None = Form(None),
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

    normalized_session_uuid = (
        _validate_upload_session_uuid(session_uuid)
        if session_uuid else str(uuid.uuid4())
    )
    pending_dir = Path(cfg.DATA_ROOT) / "uploads" / "pending" / normalized_session_uuid
    pending_dir.mkdir(parents=True, exist_ok=True)

    # BUG-API-003 / BUG-API-040: Serialize the owner-binding check and file
    # writes inside a single lock so two concurrent requests cannot both pass
    # the ".exists()" check and stomp each other's ownership claim. We also
    # use os.open(O_CREAT|O_EXCL) so the ownership file creation is atomic
    # even across processes sharing the filesystem.
    owner_meta = pending_dir / ".owner"

    # SEC-E3-R1-02 / BUG-API-003: Serialize owner-binding, count-check and writes
    async with _get_upload_lock(f"pending-{normalized_session_uuid}"):
        # Atomic ownership binding: try to create the owner file exclusively;
        # if it already exists, read it and verify the caller is the owner.
        user_id_str = current_user["user_id"]
        try:
            fd = os.open(
                str(owner_meta),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(fd, user_id_str.encode("utf-8"))
            finally:
                os.close(fd)
        except FileExistsError:
            try:
                existing_owner = owner_meta.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise HTTPException(
                    status_code=500,
                    detail="Failed to verify upload session ownership",
                ) from exc
            if existing_owner != user_id_str:
                raise HTTPException(
                    status_code=403,
                    detail="Upload session belongs to another user",
                )

        existing_count = sum(1 for f in pending_dir.iterdir() if f.is_file() and f.name != ".owner")
        if existing_count + len(files) > _UPLOAD_MAX_FILES_PER_INVESTIGATION:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Upload session already has {existing_count} files; max is "
                    f"{_UPLOAD_MAX_FILES_PER_INVESTIGATION}"
                ),
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

            # BUG-API-007: Stream read in chunks; abort as soon as size exceeds
            # the cap to avoid memory exhaustion from large uploads.
            size = 0
            chunks: list[bytes] = []
            while True:
                chunk = await upload_file.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > _UPLOAD_MAX_FILE_SIZE:
                    raise HTTPException(
                        status_code=400,
                        detail=f"File {filename!r} exceeds {_UPLOAD_MAX_FILE_SIZE // (1024*1024)}MB limit",
                    )
                chunks.append(chunk)
            content = b"".join(chunks)

            # P1-FIX-46b: Same path-traversal protection as upload_investigation_files
            safe_name = os.path.basename(re.sub(r"[^\w\-.]", "_", filename))
            if not safe_name or safe_name.startswith(".") or safe_name in (".", ".."):
                raise HTTPException(status_code=400, detail=f"Invalid filename: {filename!r}")
            dest = pending_dir / safe_name
            if not str(dest.resolve()).startswith(str(pending_dir.resolve())):
                raise HTTPException(status_code=400, detail=f"Invalid filename: {filename!r}")
            # BUG-0034 fix: append counter suffix on duplicate filenames
            if dest.exists():
                stem = Path(safe_name).stem
                ext = Path(safe_name).suffix
                counter = 1
                while dest.exists():
                    safe_name = f"{stem}_{counter}{ext}"
                    dest = pending_dir / safe_name
                    counter += 1
            dest.write_bytes(content)
            # BUG-0008 fix: reject symlinks after write (race-safe check)
            if dest.is_symlink():
                dest.unlink()
                raise HTTPException(status_code=400, detail=f"Symlinks are not allowed: {safe_name!r}")

            uploaded.append(UploadedFileInfo(
                filename=safe_name,
                size=len(content),
                content_type=_UPLOAD_MIME_MAP.get(suffix, "application/octet-stream"),
            ))

    logger.info(
        "pending_files_uploaded",
        session_uuid=normalized_session_uuid,
        count=len(uploaded),
        user_id=current_user["user_id"],
    )
    return UploadResponse(files=uploaded, session_uuid=normalized_session_uuid)


# ---------------------------------------------------------------------------
# Routes — Connectors
# ---------------------------------------------------------------------------


@app.get(
    "/api/connectors",
    response_model=list[ConnectorStatus],
    tags=["Connectors"],
)
async def list_connectors(
    current_user: dict[str, str] = Depends(_get_current_user),
) -> list[ConnectorStatus]:
    """Report which external data connectors are configured and available.

    VULN-C2-07 fix: Requires authentication.

    ``available`` is True when the corresponding API key is non-empty.
    Full health checks (live HTTP probes) are not performed here to keep
    the endpoint fast; they run asynchronously in the orchestrator.
    """
    cfg = _get_config()

    # BUG-API-006: ``available`` for Redis only reflects whether the client
    # object was constructed at startup. If Redis becomes unreachable at
    # runtime, we would otherwise still report available=True. Do a
    # best-effort liveness probe and log a warning when the client is
    # present but unreachable.
    _redis_reachable = _redis is not None
    if _redis_reachable:
        try:
            await _redis.ping()  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "redis_ping_failed_but_client_present",
                error=str(exc),
            )
            _redis_reachable = False

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
            available=_redis_reachable,
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


def _build_architecture_preview(topic: str, tier: str) -> ResearchArchitecturePlan:
    """Build a lightweight research architecture preview for the plan card.

    This is a deterministic, fast approximation.  The full LLM-generated
    ``ResearchArchitectureOutput`` is produced later by the orchestrator's
    INIT phase and supersedes this preview.
    """
    topic_lower = topic.lower()

    # Derive plausible hypotheses from the topic
    hypotheses: list[ArchitectureHypothesis] = [
        ArchitectureHypothesis(
            statement=f"Primary claim analysis: verifying the core assertions about '{topic[:80]}'",
            priority=10,
            test_strategy="Multi-source evidence extraction with cross-referencing and contradiction detection",
        ),
        ArchitectureHypothesis(
            statement="Counter-hypothesis: exploring alternative explanations and opposing evidence",
            priority=8,
            test_strategy="Seek disconfirming evidence from independent sources to stress-test the primary claim",
        ),
    ]

    # Add domain-specific hypotheses
    if any(kw in topic_lower for kw in ("market", "stock", "invest", "financial", "price", "trade", "crypto")):
        hypotheses.append(ArchitectureHypothesis(
            statement="Quantitative validation: backtesting claims against historical market data",
            priority=9,
            test_strategy="Pull real price data from financial APIs, compute risk-adjusted returns and key metrics",
        ))
    if any(kw in topic_lower for kw in ("company", "competitor", "industry", "sector")):
        hypotheses.append(ArchitectureHypothesis(
            statement="Competitive landscape: mapping the competitive dynamics and market position",
            priority=7,
            test_strategy="Cross-reference company filings, industry reports, and analyst coverage",
        ))
    if any(kw in topic_lower for kw in ("technology", "ai", "software", "algorithm", "indicator")):
        hypotheses.append(ArchitectureHypothesis(
            statement="Technical viability: assessing the underlying methodology and its limitations",
            priority=8,
            test_strategy="Review technical documentation, academic literature, and independent evaluations",
        ))

    # Standard data sources
    data_sources = [
        "Academic papers & research databases",
        "Financial data APIs (Polygon, Bloomberg, SEC)",
        "News & media archives",
        "Industry reports & analyst coverage",
        "Official documentation & primary sources",
    ]

    # Research phases — these mirror the actual state machine
    phases: list[ArchitecturePhase] = [
        ArchitecturePhase(
            name="Architecture",
            description="Analyze the topic, design research plan, identify key hypotheses",
            depends_on=[],
        ),
        ArchitecturePhase(
            name="Hypothesis Generation",
            description=f"Generate {len(hypotheses)} testable hypotheses with specific test strategies",
            depends_on=["Architecture"],
        ),
        ArchitecturePhase(
            name="Evidence Search",
            description="Multi-source parallel search across branches, extract findings with confidence scores",
            depends_on=["Hypothesis Generation"],
        ),
        ArchitecturePhase(
            name="Analysis & Scoring",
            description="Extract claims, detect contradictions, run Bayesian hypothesis updates",
            depends_on=["Evidence Search"],
        ),
    ]

    if tier == "deep":
        phases.extend([
            ArchitecturePhase(
                name="Iterative Deepening",
                description="Gap detection → replan → additional search iterations until convergence",
                depends_on=["Analysis & Scoring"],
            ),
            ArchitecturePhase(
                name="Adversarial Review",
                description="Skeptic challenge + tribunal cross-examination of key findings",
                depends_on=["Iterative Deepening"],
            ),
        ])

    phases.append(ArchitecturePhase(
        name="Report Synthesis",
        description="Perspective synthesis, executive summary, and final report generation",
        depends_on=[phases[-1].name],
    ))

    estimated_branches = len(hypotheses)

    risk_factors = ["Source reliability varies across data providers"]
    if any(kw in topic_lower for kw in ("predict", "forecast", "future")):
        risk_factors.append("Forward-looking claims are inherently uncertain")
    if any(kw in topic_lower for kw in ("backtest", "strategy", "profitable")):
        risk_factors.append("Backtesting results may suffer from overfitting or lookahead bias")

    return ResearchArchitecturePlan(
        hypotheses=hypotheses,
        data_sources=data_sources,
        research_phases=phases,
        estimated_branches=estimated_branches,
        risk_factors=risk_factors,
        flow_description=(
            f"Architecture → {len(hypotheses)} hypotheses → "
            f"{estimated_branches} parallel branches → "
            "evidence extraction → Bayesian scoring → "
            + ("iterative deepening → adversarial review → " if tier == "deep" else "")
            + "report synthesis"
        ),
    )


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

    BUG-API-045: The ``explicit_hours`` branch chooses the tier purely from the
    duration keyword and uses the fixed ``_TIER_CREDITS`` entry for that tier.
    Credits are NOT linearly scaled by the requested hours — a user asking for
    "100 hours" pays the same ``deep`` credits as one asking for "3 hours".
    This is intentional: credit pricing reflects research depth/tier, not a
    clock-based budget.  If per-hour billing is ever desired, scale here.
    """
    # BUG-API-033: handle None / empty / whitespace-only topics so that callers
    # passing an empty message (e.g. an accidental empty webhook payload) get a
    # deterministic "instant" classification instead of falling through to the
    # default quick tier, which would incorrectly charge quick-tier credits.
    if not topic or not topic.strip():
        return ClassifyResponse(
            tier="instant",
            estimated_duration_hours=0.0,
            estimated_credits=_TIER_CREDITS["instant"],
            plan_summary="Empty topic — nothing to investigate.",
            requires_approval=False,
        )

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
            research_architecture=_build_architecture_preview(topic, tier),
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
            research_architecture=_build_architecture_preview(topic, "deep"),
        )

    # ── 3. Instant-tier: greetings, tests, trivial messages ────────────
    greeting_patterns = {
        "hello", "hi", "hey", "test", "ping", "yo", "sup",
        "hi there", "hey there", "hello there", "hey yo",
        "good morning", "good afternoon", "good evening", "good night",
        "are you there", "are you alive", "are you live",
        "are you working", "who are you", "what are you",
        "thanks", "thank you", "thanks a lot", "thx",
        "ok", "okay", "cool", "nice", "great", "awesome",
        "bye", "goodbye", "see you", "see ya",
        "how are you", "whats up", "what's up", "hows it going",
    }
    # Check if the whole message is basically a greeting/test
    # BUG-S5-03 fix: word_count <= 3 was too aggressive — "What is CATL" (3 words)
    # is a real query, not a greeting.  Only use word_count <= 2 for the auto-instant
    # bucket ("hello", "hi there", "ok"), and rely on the greeting_patterns set for
    # exact matches of 3-word greetings like "are you there".
    topic_stripped = topic_lower.rstrip(".!?,")
    if topic_stripped in greeting_patterns:
        # Pure greetings/tests — don’t create an investigation at all
        return ClassifyResponse(
            tier="instant",
            estimated_duration_hours=0.01,
            estimated_credits=_TIER_CREDITS["instant"],
            plan_summary="Quick response to your message.",
            requires_approval=False,
            is_conversational=True,
        )
    # Ultra-short messages (1-2 words) that aren’t greeting patterns:
    # Could be real queries like "Bitcoin price" or "market cap".
    # Route to quick tier (not conversational) so they get a real search.
    if word_count <= 2:
        return ClassifyResponse(
            tier="quick",
            estimated_duration_hours=0.08,
            estimated_credits=_TIER_CREDITS["quick"],
            plan_summary="Quick investigation: focused lookup with web search and a concise answer with sources.",
            requires_approval=False,
        )
    # Also catch "hello, let me test if you are live" style messages.
    # Use word-boundary-aware matching to avoid false positives like
    # "backtest" matching "test" or "highway" matching "hi ".
    _greeting_patterns_re = [r'\bhello\b', r'\bhi\b', r'\bhey\b', r'\btest\b']
    _has_greeting_word = any(re.search(p, topic_lower) for p in _greeting_patterns_re)
    if _has_greeting_word and word_count < 15:
        if not any(kw in topic_lower for kw in ("research", "analyze", "investigate", "report", "find", "backtest", "backtesting", "strategy", "performance", "competitive", "analysis", "compare", "evaluate")):
            return ClassifyResponse(
                tier="instant",
                estimated_duration_hours=0.01,
                estimated_credits=_TIER_CREDITS["instant"],
                plan_summary="Quick response to your message.",
                requires_approval=False,
                is_conversational=True,
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
        "competitive", "performance", "backtest", "backtesting",
        "strategy", "position", "deep dive", "landscape",
        "trend", "forecast", "prediction", "valuation",
        "portfolio", "sector", "industry",
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
        research_architecture=_build_architecture_preview(topic, "standard"),
    )


# ---------------------------------------------------------------------------
# Routes — Billing
# ---------------------------------------------------------------------------


@app.get("/api/plans", response_model=list[PlanInfo], tags=["Billing"])
async def list_plans() -> list[PlanInfo]:
    """Return all public subscription plans."""
    return [PlanInfo(**plan) for plan in _PLANS]


@app.get("/api/billing/usage", tags=["Billing"])
async def billing_usage(
    current_user: dict[str, str] = Depends(_get_current_user),
) -> dict[str, Any]:
    """Return the caller's subscription plan + credit usage.

    v3.5: single endpoint the frontend polls to show a usage meter and
    gate agent-task creation when the user is out of credits. Credits
    returned as the raw integer balance from Supabase; plan details come
    from the static ``_PLANS`` list so we don't need a round trip to Stripe.
    """
    cfg = _get_config()
    user_id = current_user["user_id"]

    balance: int | None = None
    if cfg.SUPABASE_URL and _supabase_api_key(cfg):
        try:
            balance = await _supabase_get_user_tokens(user_id, cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("billing_usage_balance_lookup_failed", user_id=user_id, error=str(exc))
            balance = None

    # Fetch plan details. Resolve from JWT claims when available, else
    # default to "free". We intentionally return plan details instead of
    # just the slug so the frontend can render the usage meter without a
    # second API call.
    plan_slug = (current_user.get("subscription_plan") or "free").lower()
    plan_status = current_user.get("subscription_status") or "none"
    matched = next((p for p in _PLANS if p["id"] == plan_slug), None)
    # If user has no active plan, synthesize a "free" tier so the UI has
    # something to render. Free tier has limited credits to encourage upgrade.
    if matched is None:
        credits_per_month = 500 if plan_slug == "free" else 0
        plan_info = {
            "id": plan_slug,
            "name": "Free" if plan_slug == "free" else plan_slug.title(),
            "price_usd_monthly": 0.0,
            "credits_per_month": credits_per_month,
        }
    else:
        plan_info = {
            "id": matched["id"],
            "name": matched["name"],
            "price_usd_monthly": matched["price_usd_monthly"],
            "credits_per_month": matched["credits_per_month"],
        }

    # Usage percentage for the meter — monthly bucket.
    total = int(plan_info.get("credits_per_month") or 0)
    used = max(0, total - (balance or 0)) if total > 0 else 0
    pct = round(100.0 * used / total, 1) if total > 0 else 0.0

    return {
        "plan": plan_info,
        "subscription_status": plan_status,
        "credits_remaining": balance,
        "credits_used_this_period": used,
        "credits_used_pct": pct,
    }


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

    # VULN-C2-03 fix: Validate redirect URLs to prevent open-redirect phishing.
    _ALLOWED_REDIRECT_HOSTS = {
        "frontend-tau-navy-80.vercel.app",
        "localhost",
        "127.0.0.1",
    }
    for url_field, url_value in [("success_url", body.success_url), ("cancel_url", body.cancel_url)]:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url_value)
            if parsed.hostname not in _ALLOWED_REDIRECT_HOSTS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid {url_field}: host {parsed.hostname!r} is not allowed",
                )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail=f"Invalid {url_field}: malformed URL")

    # Resolve plan or top-up. Subscriptions use mode=subscription; top-ups use
    # mode=payment so the Stripe Checkout session yields a one-shot
    # payment_intent.succeeded webhook event.
    plan = _PLAN_BY_ID.get(body.plan_id)
    topup = _TOPUP_BY_ID.get(body.plan_id) if plan is None else None
    if plan is None and topup is None:
        raise HTTPException(status_code=404, detail=f"Plan {body.plan_id!r} not found")

    line_items = [{"price": (plan or topup)["stripe_price_id"], "quantity": 1}]
    metadata: dict[str, str] = {
        "user_id": current_user["user_id"],
        "deft_kind": "subscription" if plan else "topup",
        "deft_plan_id": body.plan_id,
    }

    try:
        session = _stripe.checkout.Session.create(
            mode="subscription" if plan else "payment",
            line_items=line_items,
            success_url=body.success_url,
            cancel_url=body.cancel_url,
            metadata=metadata,
            client_reference_id=current_user["user_id"],
            payment_intent_data={"metadata": metadata} if topup else None,
            subscription_data={"metadata": metadata} if plan else None,
        )
    except _stripe.StripeError as exc:
        # M-02 fix: log the raw Stripe error server-side but return a
        # generic message to the client so we don't leak internal
        # configuration / price IDs / Stripe diagnostics.
        logger.error(
            "stripe_checkout_failed",
            user_id=current_user["user_id"],
            plan_id=body.plan_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=502,
            detail="Payment service error. Please try again.",
        ) from exc

    logger.info(
        "checkout_session_created",
        session_id=session.id,
        user_id=current_user["user_id"],
        plan_id=body.plan_id,
    )
    # BUG-API-004: Stripe can return null session.url in edge cases
    if not session.url:
        raise HTTPException(status_code=502, detail="Stripe did not return a checkout URL")
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

    event_id: str | None = event.get("id")
    event_type: str | None = event.get("type")  # BUG-API-029: use .get() to avoid KeyError on malformed webhooks
    if not event_type:
        raise HTTPException(status_code=400, detail="Webhook event missing type")
    log = logger.bind(event_type=event_type, event_id=event_id)

    if not event_id:
        raise HTTPException(status_code=400, detail="Webhook event missing id")

    # B-03 fix: two-phase idempotency.  We claim the event in 'pending' state
    # *before* running the handler.  Only after the handler returns
    # successfully do we mark the event 'completed'.  If the handler raises,
    # the row stays 'pending' so the next Stripe retry will re-execute the
    # handler instead of being silently skipped as a duplicate.  All grant
    # paths are independently idempotent on ``ref_id = event_id`` via the
    # ``uq_credit_tx_idem`` partial unique index, so re-execution cannot
    # double-credit.
    try:
        claim = await _claim_webhook_event(event_id, event_type)
    except Exception as exc:  # noqa: BLE001
        log.error("stripe_idempotency_check_failed", error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"status": "idempotency_error", "error": str(exc)},
        )

    if claim == _WebhookClaim.DUPLICATE:
        log.info("stripe_webhook_replay_ignored")
        return JSONResponse(content={"status": "duplicate", "event_id": event_id})

    log.info("stripe_webhook_received", claim=claim)

    try:
        if event_type == "checkout.session.completed":
            session_obj = event["data"]["object"]
            await _handle_checkout_completed(session_obj, cfg, event_id=event_id)

        elif event_type == "invoice.paid":
            invoice_obj = event["data"]["object"]
            await _handle_invoice_paid(invoice_obj, cfg, event_id=event_id)

        elif event_type == "payment_intent.succeeded":
            pi_obj = event["data"]["object"]
            await _handle_payment_intent_succeeded(pi_obj, cfg, event_id=event_id)

        elif event_type == "customer.subscription.updated":
            sub_obj = event["data"]["object"]
            await _handle_subscription_updated(sub_obj, cfg)

        elif event_type == "customer.subscription.deleted":
            sub_obj = event["data"]["object"]
            await _handle_subscription_deleted(sub_obj, cfg)

        else:
            log.info("stripe_webhook_unhandled_event")

    except HTTPException as exc:
        # BUG-C1-09 fix: Let 503 from _supabase_add_credits propagate as
        # 500 so Stripe retries when the credit RPC is down.  B-03: do NOT
        # finalize the event — leave it 'pending' so the retry re-runs.
        log.error("stripe_webhook_handler_failed_retriable")
        await _record_webhook_event_failure(event_id, f"http_{exc.status_code}: {exc.detail}")
        return JSONResponse(
            status_code=500,
            content={"status": "handler_error_retriable"},
        )
    except Exception as exc:  # noqa: BLE001
        log.error("stripe_webhook_handler_failed", error=str(exc), exc_info=True)
        # B-03 fix: Return 500 so Stripe retries.  The event row stays
        # 'pending' so ``_claim_webhook_event`` returns RETRY next time,
        # re-running the handler.  Per-grant idempotency guards prevent
        # double-credit.
        await _record_webhook_event_failure(event_id, str(exc))
        return JSONResponse(
            status_code=500,
            content={"status": "handler_error", "error": str(exc)},
        )

    # Handler succeeded — finalize the idempotency row.
    try:
        await _finalize_webhook_event(event_id)
    except Exception as exc:  # noqa: BLE001
        # The handler already mutated state (granted credits, etc.) but we
        # failed to mark the event 'completed'.  Return 500 so Stripe retries;
        # on the next attempt the claim will return RETRY and the handler
        # will run again — protected by per-grant ``ref_id`` idempotency.
        log.error("stripe_webhook_finalize_failed", error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"status": "finalize_error", "error": str(exc)},
        )

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
        # M-02 fix: generic client-facing detail, full error logged.
        logger.error("stripe_portal_failed", user_id=user_id, error=str(exc))
        raise HTTPException(
            status_code=502,
            detail="Payment service error. Please try again.",
        ) from exc

    logger.info("portal_session_created", user_id=user_id)
    # BUG-API-004: Stripe can return null portal_session.url
    if not portal_session.url:
        raise HTTPException(status_code=502, detail="Stripe did not return a portal URL")
    return BillingPortalResponse(portal_url=portal_session.url)


# ---------------------------------------------------------------------------
# Stripe webhook helpers
# ---------------------------------------------------------------------------


async def _handle_checkout_completed(
    session_obj: dict[str, Any],
    cfg: AppConfig,
    *,
    event_id: str,
) -> None:
    """Process checkout.session.completed.

    For subscription mode: link Stripe customer / subscription / plan, then
    immediately grant the first month's credits (idempotent on event_id).
    For one-time payment (top-up) mode: skip — handled by
    payment_intent.succeeded so refunds and metadata are consistent.
    """
    # BUG-API-043: Stripe may return metadata: null; guard with `or {}`
    _meta = session_obj.get("metadata") or {}
    user_id: str | None = (
        _meta.get("user_id")
        or session_obj.get("client_reference_id")
    )
    plan_id: str | None = _meta.get("deft_plan_id") or _meta.get("plan_id")
    kind: str = (_meta.get("deft_kind") or session_obj.get("mode") or "subscription").lower()
    stripe_customer_id: str | None = session_obj.get("customer")
    subscription_id: str | None = session_obj.get("subscription")

    if not user_id:
        logger.warning("checkout_completed_no_user_id", session_id=session_obj.get("id"))
        return

    # Top-ups are handled by payment_intent.succeeded — exit early to avoid
    # double-granting.
    if kind == "topup" or session_obj.get("mode") == "payment":
        logger.info("checkout_completed_topup_deferred", user_id=user_id, plan_id=plan_id)
        return

    plan = _PLAN_BY_ID.get(plan_id) if plan_id else None
    credits_to_add = int(plan["credits_per_month"]) if plan else 0

    # Retrieve full subscription to get current_period_end
    period_end: str | None = None
    if subscription_id:
        try:
            sub = _stripe.Subscription.retrieve(subscription_id)
            period_end_ts = sub.get("current_period_end")
            if period_end_ts:
                period_end = datetime.fromtimestamp(
                    period_end_ts, tz=timezone.utc
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

    if update_payload and cfg.SUPABASE_URL and _supabase_api_key(cfg):
        await _supabase_patch_profile(user_id, update_payload, cfg)

    if credits_to_add > 0:
        await _grant_credits_for_event(
            user_id=user_id,
            credits=credits_to_add,
            source="plan_renewal",
            ref_id=event_id,
            expires_at=period_end,
            cfg=cfg,
        )

    logger.info(
        "checkout_completed",
        user_id=user_id,
        plan_id=plan_id,
        credits_added=credits_to_add,
    )


async def _handle_invoice_paid(
    invoice_obj: dict[str, Any],
    cfg: AppConfig,
    *,
    event_id: str,
) -> None:
    """Process invoice.paid: monthly renewal grant for an active subscription.

    The invoice carries the customer + (line items -> price.id) which we map
    back to the Deft plan. We skip the very first invoice on subscription
    creation because checkout.session.completed already granted those credits;
    Stripe identifies that with billing_reason=='subscription_create'.
    """
    billing_reason: str = invoice_obj.get("billing_reason") or ""
    if billing_reason == "subscription_create":
        logger.info("invoice_paid_skip_first", invoice_id=invoice_obj.get("id"))
        return
    if invoice_obj.get("status") != "paid":
        logger.info("invoice_paid_not_paid", status=invoice_obj.get("status"))
        return

    customer_id: str | None = invoice_obj.get("customer")
    if not customer_id:
        logger.warning("invoice_paid_no_customer", invoice_id=invoice_obj.get("id"))
        return

    user_id = await _get_user_id_for_customer(customer_id, cfg)
    if not user_id:
        logger.warning("invoice_paid_unknown_customer", customer_id=customer_id)
        return

    # Resolve which plan was billed
    plan: dict[str, Any] | None = None
    for line in (invoice_obj.get("lines") or {}).get("data") or []:
        price_id = (line.get("price") or {}).get("id") or line.get("plan", {}).get("id")
        if price_id and price_id in _PLAN_BY_PRICE_ID:
            plan = _PLAN_BY_PRICE_ID[price_id]
            break

    if plan is None:
        logger.warning("invoice_paid_no_plan_match", invoice_id=invoice_obj.get("id"))
        return

    period_end_ts = invoice_obj.get("period_end") or invoice_obj.get("lines", {}).get(
        "data", [{}]
    )[0].get("period", {}).get("end")
    period_end_iso: str | None = None
    if period_end_ts:
        try:
            period_end_iso = datetime.fromtimestamp(
                int(period_end_ts), tz=timezone.utc
            ).isoformat()
        except (TypeError, ValueError):
            period_end_iso = None

    await _grant_credits_for_event(
        user_id=user_id,
        credits=int(plan["credits_per_month"]),
        source="plan_renewal",
        ref_id=event_id,
        expires_at=period_end_iso,
        cfg=cfg,
    )

    if cfg.SUPABASE_URL and _supabase_api_key(cfg):
        patch: dict[str, Any] = {
            "subscription_plan": plan["id"],
            "subscription_status": "active",
        }
        if period_end_iso:
            patch["subscription_current_period_end"] = period_end_iso
        await _supabase_patch_profile_by_customer(customer_id, patch, cfg)

    logger.info(
        "invoice_paid_granted",
        user_id=user_id,
        plan_id=plan["id"],
        credits=int(plan["credits_per_month"]),
    )


async def _handle_payment_intent_succeeded(
    pi_obj: dict[str, Any],
    cfg: AppConfig,
    *,
    event_id: str,
) -> None:
    """Process payment_intent.succeeded for one-time top-up purchases.

    We only grant credits when the payment_intent's metadata identifies it as
    a Deft top-up (set in create_checkout via payment_intent_data.metadata).
    Subscription invoices generate their own payment_intent.succeeded; those
    are ignored here because invoice.paid is the canonical signal.
    """
    metadata = pi_obj.get("metadata") or {}
    if metadata.get("deft_kind") != "topup":
        logger.info("payment_intent_not_topup_skipped", pi_id=pi_obj.get("id"))
        return

    user_id = metadata.get("user_id")
    plan_id = metadata.get("deft_plan_id")
    topup = _TOPUP_BY_ID.get(plan_id) if plan_id else None
    if not user_id or topup is None:
        logger.warning(
            "payment_intent_topup_unresolved",
            user_id=user_id,
            plan_id=plan_id,
        )
        return

    await _grant_credits_for_event(
        user_id=user_id,
        credits=int(topup["credits"]),
        source="topup",
        ref_id=event_id,
        expires_at=None,
        cfg=cfg,
    )
    logger.info(
        "topup_granted",
        user_id=user_id,
        plan_id=plan_id,
        credits=int(topup["credits"]),
    )


async def _grant_credits_for_event(
    *,
    user_id: str,
    credits: int,
    source: str,
    ref_id: str,
    expires_at: str | None,
    cfg: AppConfig,
) -> None:
    """Idempotent grant via the ledger RPC. ``ref_id`` should be the Stripe event id.

    Replays of the same Stripe event collapse to a single bucket because the
    grant_credits RPC is idempotent on (ref_type, ref_id).
    """
    from mariana.billing.ledger import grant_credits as _grant_rpc, LedgerError

    api_key = _supabase_api_key(cfg)
    if not cfg.SUPABASE_URL or not api_key:
        logger.error(
            "grant_credits_supabase_unconfigured",
            user_id=user_id,
            credits=credits,
            ref_id=ref_id,
        )
        raise HTTPException(status_code=503, detail="Credit ledger unavailable")
    try:
        await _grant_rpc(
            supabase_url=cfg.SUPABASE_URL,
            service_key=api_key,
            user_id=user_id,
            credits=int(credits),
            source=source,  # type: ignore[arg-type]
            ref_type="stripe_event",
            ref_id=ref_id,
            expires_at=expires_at,
        )
    except LedgerError as exc:
        logger.error(
            "grant_credits_failed",
            user_id=user_id,
            credits=credits,
            ref_id=ref_id,
            error=str(exc),
        )
        # Surface as 500 so Stripe retries via the outer handler
        raise HTTPException(status_code=503, detail="Credit grant failed") from exc


async def _get_user_id_for_customer(
    stripe_customer_id: str, cfg: AppConfig
) -> str | None:
    """Resolve a Stripe customer ID back to the Supabase user_id."""
    api_key = _supabase_api_key(cfg)
    if not cfg.SUPABASE_URL or not api_key:
        return None
    url = f"{cfg.SUPABASE_URL}/rest/v1/profiles"
    params = {"stripe_customer_id": f"eq.{stripe_customer_id}", "select": "id", "limit": "1"}
    headers = {"apikey": api_key, "Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            logger.error("customer_lookup_network_error", customer_id=stripe_customer_id, error=str(exc))
            return None
    if resp.status_code != 200:
        logger.error(
            "customer_lookup_failed",
            customer_id=stripe_customer_id,
            status=resp.status_code,
        )
        return None
    rows = resp.json() or []
    if not rows:
        return None
    return rows[0].get("id")


async def _handle_subscription_updated(
    sub_obj: dict[str, Any],
    cfg: AppConfig,
) -> None:
    """Process customer.subscription.updated: sync status to Supabase."""
    stripe_customer_id: str | None = sub_obj.get("customer")
    status: str = sub_obj.get("status", "unknown")

    if not stripe_customer_id:
        # BUG-API-032: log when event arrives without a customer ID so that
        # malformed / unexpected Stripe payloads are surfaced instead of
        # silently dropped.
        logger.warning(
            "subscription_updated_missing_customer_id",
            subscription_id=sub_obj.get("id"),
            status=status,
        )
        return

    if not cfg.SUPABASE_URL or not _supabase_api_key(cfg):
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
        # BUG-API-032: log when event arrives without a customer ID.
        logger.warning(
            "subscription_deleted_missing_customer_id",
            subscription_id=sub_obj.get("id"),
        )
        return

    if not cfg.SUPABASE_URL or not _supabase_api_key(cfg):
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


def _supabase_api_key(cfg: AppConfig) -> str | None:
    """Return the best available Supabase API key.

    Prefers ``SUPABASE_SERVICE_KEY`` (full admin access), but falls back to
    ``SUPABASE_ANON_KEY`` which works for RPC calls on ``SECURITY DEFINER``
    functions that have been explicitly granted to the ``anon`` role.
    """
    return cfg.SUPABASE_SERVICE_KEY or cfg.SUPABASE_ANON_KEY or None


async def _supabase_patch_profile(
    user_id: str,
    payload: dict[str, Any],
    cfg: AppConfig,
) -> None:
    """Update a single Supabase profile row identified by user_id via RPC."""
    api_key = _supabase_api_key(cfg)
    if not api_key:
        logger.warning("supabase_no_api_key_skip_patch_profile")
        return
    rpc_url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/update_profile_by_id"
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            rpc_url,
            json={"target_user_id": user_id, "payload": payload},
            headers=headers,
        )
        if resp.status_code not in (200, 204):
            logger.error(
                "supabase_patch_profile_failed",
                user_id=user_id,
                status=resp.status_code,
                body=resp.text[:200],
            )
            # BUG-API-020 fix: Raise so the webhook handler can return 500
            # and Stripe retries, instead of silently losing profile updates.
            raise HTTPException(
                status_code=502,
                detail=f"Supabase profile patch failed ({resp.status_code})",
            )


async def _supabase_patch_profile_by_customer(
    stripe_customer_id: str,
    payload: dict[str, Any],
    cfg: AppConfig,
) -> None:
    """Update Supabase profile rows matching a stripe_customer_id via RPC."""
    api_key = _supabase_api_key(cfg)
    if not api_key:
        logger.warning("supabase_no_api_key_skip_patch_by_customer")
        return
    rpc_url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/update_profile_by_stripe_customer"
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            rpc_url,
            json={"target_customer_id": stripe_customer_id, "payload": payload},
            headers=headers,
        )
        if resp.status_code not in (200, 204):
            logger.error(
                "supabase_patch_by_customer_failed",
                stripe_customer_id=stripe_customer_id,
                status=resp.status_code,
                body=resp.text[:200],
            )
            # BUG-API-020 fix: Raise so the webhook handler returns 500 and Stripe retries
            raise HTTPException(
                status_code=502,
                detail=f"Supabase patch by customer failed ({resp.status_code})",
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
    api_key = _supabase_api_key(cfg)
    if not api_key:
        logger.error("supabase_no_api_key_for_add_credits")
        raise HTTPException(status_code=503, detail="Credit service unavailable (no API key)")
    rpc_url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/add_credits"
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            rpc_url,
            json={"p_user_id": user_id, "p_credits": credits},
            headers=headers,
        )
        if resp.status_code in (200, 204):
            logger.info("credits_added_via_rpc", user_id=user_id, credits=credits)
            return

        # BUG-C1-09 fix: The previous read-modify-PATCH fallback was not
        # atomic and could corrupt credit balances on concurrent webhooks.
        # Now we use a raw SQL approach via PostgREST RPC or fail loudly
        # so Stripe can retry the webhook later.
        logger.error(
            "add_credits_rpc_unavailable",
            status=resp.status_code,
            user_id=user_id,
            credits=credits,
            message=(
                "The add_credits RPC is unavailable.  Credit grant was NOT "
                "applied.  Stripe webhook should retry.  Create the RPC: "
                "CREATE FUNCTION add_credits(p_user_id UUID, p_credits INT) "
                "RETURNS VOID AS $$ UPDATE profiles SET tokens = tokens + "
                "p_credits WHERE id = p_user_id; $$ LANGUAGE sql;"
            ),
        )
        raise HTTPException(
            status_code=503,
            detail="Credit service (add_credits RPC) unavailable",
        )


async def _supabase_get_user_tokens(
    user_id: str,
    cfg: AppConfig,
) -> int | None:
    """Fetch the current token balance for a user from Supabase profiles."""
    api_key = _supabase_api_key(cfg)
    if not cfg.SUPABASE_URL or not api_key:
        return None
    # Use RPC function (SECURITY DEFINER) — works with both service key and anon key
    rpc_url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/get_user_tokens"
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            rpc_url,
            json={"target_user_id": user_id},
            headers=headers,
        )
        if resp.status_code != 200:
            logger.error(
                "supabase_get_tokens_failed",
                user_id=user_id,
                status=resp.status_code,
                body=resp.text[:200],
            )
            return None
        result = resp.json()
        # RPC returns the integer directly
        if result is None:
            return None
        return int(result)


async def _supabase_deduct_credits(
    user_id: str,
    amount: int,
    cfg: AppConfig,
) -> Literal["ok", "insufficient", "error"]:
    """Deduct credits from a user's Supabase profile via RPC.

    Calls the ``deduct_credits`` RPC function.

    BUG-API-005: Returns a three-state result so callers can distinguish:
      - ``"ok"``          — credits were deducted successfully
      - ``"insufficient"`` — user did not have enough credits (RPC returned 402
                             or similar business-logic rejection)
      - ``"error"``       — RPC unavailable / configuration missing / network
                             failure; callers should surface 503 rather than 402

    If the RPC is unavailable we do not attempt a read-modify-write fallback,
    because that sequence is not atomic and would allow concurrent
    investigation submissions to overspend credits.
    """
    api_key = _supabase_api_key(cfg)
    if not cfg.SUPABASE_URL or not api_key:
        logger.warning("supabase_not_configured_skip_deduct")
        return "error"

    rpc_url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/deduct_credits"
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                rpc_url,
                json={"target_user_id": user_id, "amount": amount},
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.error(
            "deduct_credits_rpc_network_error",
            user_id=user_id,
            amount=amount,
            error=str(exc),
        )
        return "error"

    if resp.status_code == 200:
        # The RPC returns a JSON boolean / object; treat a truthy response
        # as success and a falsy one as insufficient funds.
        try:
            data = resp.json()
        except ValueError:
            data = True
        if data is False or (isinstance(data, dict) and data.get("success") is False):
            logger.info(
                "deduct_credits_insufficient",
                user_id=user_id,
                amount=amount,
            )
            return "insufficient"
        logger.info("credits_deducted_via_rpc", user_id=user_id, amount=amount)
        return "ok"

    # 402 / 400 from the RPC indicate insufficient balance (business-logic
    # error); everything else is a transient service error.
    if resp.status_code in (400, 402, 409):
        logger.info(
            "deduct_credits_insufficient",
            user_id=user_id,
            amount=amount,
            status=resp.status_code,
        )
        return "insufficient"

    logger.error(
        "deduct_credits_rpc_error",
        user_id=user_id,
        amount=amount,
        status=resp.status_code,
    )
    return "error"


# Sentinel returned by ``_claim_webhook_event`` to indicate the disposition of
# the claim attempt.  Using a small enum keeps the call-sites readable without
# pulling in a heavier type elsewhere.
class _WebhookClaim:
    NEW = "new"            # First time seen; caller must run the handler.
    RETRY = "retry"        # Previously crashed mid-handler; caller must run again.
    DUPLICATE = "duplicate"  # Already completed successfully; caller must skip.


async def _claim_webhook_event(event_id: str, event_type: str) -> str:
    """B-03 two-phase claim: atomically reserve a webhook event for handling.

    Returns one of ``_WebhookClaim.{NEW, RETRY, DUPLICATE}``.

    Semantics:
      - NEW       — First INSERT; caller proceeds and must call ``_finalize_webhook_event`` on success.
      - RETRY     — An earlier handler invocation crashed (status='pending');
                     caller proceeds and must call ``_finalize_webhook_event`` on success.
                     ``attempts`` is incremented for observability.
      - DUPLICATE — Already completed successfully (status='completed');
                     caller short-circuits and returns 200 to Stripe.

    The single round-trip uses ``INSERT ... ON CONFLICT DO UPDATE`` so the
    transition from pending→pending+1 happens atomically with the lookup.
    The ``RETURNING`` clause yields the *post-write* row state along with the
    pre-existing status (captured via the EXCLUDED ↔ stripe_webhook_events
    join in a subquery) so we can disambiguate NEW vs RETRY vs DUPLICATE.

    DB failures propagate to the caller (``stripe_webhook``) where they are
    translated into HTTP 500 so Stripe retries.
    """
    db = _get_db()
    # The CTE captures the pre-update status so we can return it alongside the
    # upserted row.  When the row didn't exist, prior_status is NULL.
    row = await db.fetchrow(
        """
        WITH prior AS (
            SELECT status FROM stripe_webhook_events WHERE event_id = $1
        ),
        upserted AS (
            INSERT INTO stripe_webhook_events
                (event_id, event_type, status, attempts, received_at, last_attempt_at, processed_at)
            VALUES ($1, $2, 'pending', 1, NOW(), NOW(), NOW())
            ON CONFLICT (event_id) DO UPDATE
                SET attempts        = stripe_webhook_events.attempts + 1,
                    last_attempt_at = NOW(),
                    -- Do not overwrite event_type or completed status.
                    event_type      = stripe_webhook_events.event_type
                WHERE stripe_webhook_events.status = 'pending'
            RETURNING status
        )
        SELECT (SELECT status FROM prior)     AS prior_status,
               (SELECT status FROM upserted)  AS post_status
        """,
        event_id,
        event_type,
    )
    prior_status = row["prior_status"] if row is not None else None
    post_status = row["post_status"] if row is not None else None

    if prior_status is None:
        # No prior row — this is a brand-new event.  upserted produced one row.
        return _WebhookClaim.NEW
    if prior_status == "completed":
        # A successful run already happened.  upserted's WHERE clause filtered
        # the UPDATE out, so post_status is NULL.  Stripe replay; skip.
        return _WebhookClaim.DUPLICATE
    # prior_status == 'pending' — the handler crashed before finalising.
    # The UPDATE matched (post_status='pending') and bumped attempts; rerun.
    assert post_status == "pending", post_status
    return _WebhookClaim.RETRY


async def _finalize_webhook_event(event_id: str) -> None:
    """B-03 second phase: mark a webhook event as fully processed.

    Called only after the business-logic handler returns successfully.  If
    this UPDATE itself fails the caller surfaces 500 to Stripe; on the next
    retry ``_claim_webhook_event`` returns RETRY (status is still 'pending'),
    so the handler runs again.  This is acceptable because every grant path
    is itself idempotent on ``ref_id=event_id`` via the
    ``uq_credit_tx_idem`` partial unique index.
    """
    db = _get_db()
    await db.execute(
        """
        UPDATE stripe_webhook_events
           SET status        = 'completed',
               completed_at  = NOW(),
               processed_at  = NOW(),
               last_error    = NULL
         WHERE event_id = $1
        """,
        event_id,
    )


async def _record_webhook_event_failure(event_id: str, error: str) -> None:
    """Record the most recent failure reason without finalising the event.

    The event remains ``status='pending'`` so Stripe's retry cycle can
    reattempt it.  Errors here are swallowed — if even the failure-recording
    UPDATE fails, the original handler error has already been logged.
    """
    try:
        db = _get_db()
        await db.execute(
            """
            UPDATE stripe_webhook_events
               SET last_error      = LEFT($2, 4000),
                   last_attempt_at = NOW()
             WHERE event_id = $1
            """,
            event_id,
            error,
        )
    except Exception:  # noqa: BLE001
        logger.warning("stripe_webhook_failure_record_failed", event_id=event_id)


# Backwards-compat shim — some tests and callers reference the old name.
# Returns True for both NEW and RETRY (i.e. "caller should proceed").
async def _record_webhook_event_once(event_id: str, event_type: str) -> bool:
    claim = await _claim_webhook_event(event_id, event_type)
    return claim != _WebhookClaim.DUPLICATE


async def _get_stripe_customer_id(
    user_id: str,
    cfg: AppConfig,
) -> str | None:
    """Fetch the stripe_customer_id for a user from the Supabase profiles table."""
    api_key = _supabase_api_key(cfg)
    if not cfg.SUPABASE_URL or not api_key:
        return None
    # Use RPC function (SECURITY DEFINER) — works with both service key and anon key
    rpc_url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/get_stripe_customer_id"
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            rpc_url,
            json={"target_user_id": user_id},
            headers=headers,
        )
        if resp.status_code != 200:
            logger.error(
                "supabase_get_customer_id_failed",
                user_id=user_id,
                status=resp.status_code,
            )
            return None
        result = resp.json()
        return result if isinstance(result, str) else None


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

    # Forward the caller's JWT so the RPC function can verify admin role.
    # BUG-API-030 / BUG-API-048: normalize the header so we never forward
    # whitespace-only or malformed values.
    # Loop6 / B-01: use the service_role key as the API-gateway credential
    # (the Authorization header still carries the caller's JWT, so the
    # effective Postgres role is still 'authenticated' and auth.uid() is
    # the caller; is_admin() gating inside the RPC still fires).
    auth_header = _normalize_bearer_auth_header(
        request.headers.get("authorization") or request.headers.get("Authorization")
    )
    api_key = _supabase_api_key(cfg) or ""

    url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/admin_list_profiles"
    headers = {
        "apikey": api_key,
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

    # BUG-API-024: validate status filter against known values.
    if status:
        normalized_status = status.upper()
        if normalized_status not in _VALID_TASK_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid status {status!r}. Must be one of: "
                    f"{sorted(_VALID_TASK_STATUSES)}"
                ),
            )
        status = normalized_status

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

    # BUG-API-030 / BUG-API-048: normalize auth header before forwarding.
    # Loop6 / B-01: api_key uses service_role so the API-gateway accepts
    # the request; the Authorization JWT still drives the Postgres role
    # and auth.uid() inside admin_set_credits.
    auth_header = _normalize_bearer_auth_header(
        request.headers.get("authorization") or request.headers.get("Authorization")
    )
    api_key = _supabase_api_key(cfg) or ""

    url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/admin_set_credits"
    headers = {
        "apikey": api_key,
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
    # BUG-API-025: track availability so the response can signal when
    # total_users is unreliable rather than defaulting to 0.
    total_users = 0
    total_users_available = True
    if cfg.SUPABASE_URL:
        try:
            # BUG-API-030 / BUG-API-048: normalize auth header before forwarding.
            # Loop6 / B-01: api_key uses service_role; JWT still carries caller.
            auth_header = _normalize_bearer_auth_header(
                request.headers.get("authorization") or request.headers.get("Authorization")
            )
            api_key = _supabase_api_key(cfg) or ""
            headers = {
                "apikey": api_key,
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
                else:
                    total_users_available = False
                    logger.warning(
                        "admin_stats_supabase_non_200",
                        status=resp.status_code,
                    )
        except Exception as e:
            total_users_available = False
            logger.warning("admin_stats_supabase_error", error=str(e))
    else:
        total_users_available = False

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
    # Credits consumed: apply 20% platform markup before converting to credits.
    # Formula: credits = raw_cost_usd * 1.20 / _CREDIT_USD_RATE = raw * 1.20 / 0.01 = raw * 120
    total_credits_consumed = int(total_spent * 120)
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
        total_users_available=total_users_available,
        total_investigations=total_investigations,
        running_investigations=running,
        completed_investigations=completed,
        failed_investigations=failed,
        total_credits_consumed=total_credits_consumed,
        total_spent_usd=total_spent,
        active_users_30d=active_users_30d,
    )


# ---------------------------------------------------------------------------
# Routes — Admin v3.7 (RPC-backed via SECURITY DEFINER functions)
# ---------------------------------------------------------------------------


def _admin_supabase_headers(request: Request, cfg: AppConfig) -> dict[str, str]:
    """Build headers for forwarding to Supabase PostgREST with caller JWT.

    Loop6 / B-01: The ``apikey`` header uses the service-role key (preferred)
    as the API-gateway credential. The ``Authorization`` header still carries
    the caller's user JWT, which is what PostgREST uses to resolve the
    Postgres role (``authenticated``) and ``auth.uid()`` inside SECURITY
    DEFINER functions. Using service_role as the gateway key avoids the
    anon-key rate limits and lets admin RPCs function under the partial
    revoke posture applied by migration 005 (anon+PUBLIC fully revoked
    on admin-gated RPCs; authenticated retained because is_admin(auth.uid())
    is enforced inline).
    """
    auth_header = _normalize_bearer_auth_header(
        request.headers.get("authorization") or request.headers.get("Authorization")
    )
    return {
        "apikey": _supabase_api_key(cfg) or "",
        "Authorization": auth_header,
        "Content-Type": "application/json",
    }


async def _admin_rpc_call(
    request: Request,
    fn: str,
    payload: dict[str, Any],
    *,
    timeout: float = 15.0,
) -> Any:
    """POST to a Supabase RPC function, returning parsed JSON.

    Raises HTTPException on non-200. Caller's JWT is forwarded so the
    SECURITY DEFINER fn can verify admin role.
    """
    cfg = _get_config()
    if not cfg.SUPABASE_URL:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/{fn}"
    headers = _admin_supabase_headers(request, cfg)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=payload)
    if resp.status_code != 200:
        body = resp.text[:400] if resp.text else ""
        logger.error("admin_rpc_failed", fn=fn, status=resp.status_code, body=body)
        # Map Postgres permission / validation errors to 4xx where possible
        code = 403 if resp.status_code in (401, 403) else 502
        raise HTTPException(status_code=code, detail=f"RPC {fn} failed: {body}")
    try:
        return resp.json()
    except ValueError:
        return None


async def _admin_rest_request(
    request: Request,
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    json_body: Any = None,
    prefer: str | None = None,
    timeout: float = 15.0,
) -> httpx.Response:
    """Proxy a PostgREST request with caller JWT.

    RLS ensures admin-only tables refuse non-admin callers.
    """
    cfg = _get_config()
    if not cfg.SUPABASE_URL:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    url = f"{cfg.SUPABASE_URL}/rest/v1{path}"
    headers = _admin_supabase_headers(request, cfg)
    if prefer:
        headers["Prefer"] = prefer
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(
            method.upper(), url, headers=headers, params=params, json=json_body
        )
    return resp


@app.get("/api/admin/overview", tags=["Admin"], summary="Admin dashboard overview (v2)")
async def admin_overview_v2(
    request: Request,
    caller: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    data = await _admin_rpc_call(
        request, "admin_overview_stats", {"p_caller": caller["user_id"]}
    )
    return JSONResponse(content=data or {})


@app.get("/api/admin/audit-log", tags=["Admin"], summary="List audit log entries")
async def admin_audit_log_list(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    action: str | None = Query(None, max_length=64),
    caller: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    data = await _admin_rpc_call(
        request,
        "admin_audit_list",
        {
            "p_caller": caller["user_id"],
            "p_limit": limit,
            "p_offset": offset,
            "p_action_filter": action,
        },
    )
    return JSONResponse(content=data or [])


@app.post("/api/admin/users/{user_id}/role", tags=["Admin"], summary="Set user role")
async def admin_user_set_role(
    user_id: str,
    body: AdminSetRoleRequest,
    request: Request,
    caller: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    await _admin_rpc_call(
        request,
        "admin_set_role",
        {
            "p_caller": caller["user_id"],
            "p_target": user_id,
            "p_new_role": body.role,
        },
    )
    logger.info("admin_role_set", actor=caller["user_id"], target=user_id, role=body.role)
    return JSONResponse(content={"user_id": user_id, "role": body.role})


@app.post("/api/admin/users/{user_id}/suspend", tags=["Admin"], summary="Suspend/unsuspend user")
async def admin_user_suspend(
    user_id: str,
    body: AdminSuspendRequest,
    request: Request,
    caller: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    await _admin_rpc_call(
        request,
        "admin_suspend",
        {
            "p_caller": caller["user_id"],
            "p_target": user_id,
            "p_suspend": body.suspend,
            "p_reason": body.reason,
        },
    )
    logger.info(
        "admin_suspend_toggled",
        actor=caller["user_id"],
        target=user_id,
        suspend=body.suspend,
    )
    return JSONResponse(content={"user_id": user_id, "suspended": body.suspend})


@app.post("/api/admin/users/{user_id}/credits-v2", tags=["Admin"], summary="Adjust credits (v2, audited)")
async def admin_user_credits_v2(
    user_id: str,
    body: AdminCreditsV2Request,
    request: Request,
    caller: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    if body.mode == "set" and body.amount < 0:
        raise HTTPException(status_code=422, detail="amount must be >= 0 when mode='set'")
    new_balance = await _admin_rpc_call(
        request,
        "admin_adjust_credits",
        {
            "p_caller": caller["user_id"],
            "p_target": user_id,
            "p_mode": body.mode,
            "p_amount": body.amount,
            "p_reason": body.reason,
        },
    )
    logger.info(
        "admin_credits_adjusted",
        actor=caller["user_id"],
        target=user_id,
        mode=body.mode,
        amount=body.amount,
        new_balance=new_balance,
    )
    return JSONResponse(content={"user_id": user_id, "new_balance": new_balance})


@app.post("/api/admin/system/freeze", tags=["Admin"], summary="Toggle global system freeze (kill-switch)")
async def admin_system_freeze(
    body: AdminSystemFreezeRequest,
    request: Request,
    caller: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    await _admin_rpc_call(
        request,
        "admin_system_freeze",
        {
            "p_caller": caller["user_id"],
            "p_frozen": body.frozen,
            "p_reason": body.reason,
            "p_message": body.message,
        },
    )
    logger.warning(
        "admin_system_freeze_toggled",
        actor=caller["user_id"],
        frozen=body.frozen,
        reason=body.reason,
    )
    return JSONResponse(content={"frozen": body.frozen, "message": body.message})


@app.get("/api/admin/tasks", tags=["Admin"], summary="List all user tasks (investigations)")
async def admin_list_all_tasks(
    request: Request,
    status: str | None = Query(None, max_length=32),
    user_id: str | None = Query(None, max_length=64),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    caller: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    data = await _admin_rpc_call(
        request,
        "admin_list_tasks",
        {
            "p_caller": caller["user_id"],
            "p_status": status,
            "p_user_id": user_id,
            "p_limit": limit,
            "p_offset": offset,
        },
    )
    return JSONResponse(content=data or [])


# --- Admin todo-list (admin_tasks table) ----------------------------------


@app.get("/api/admin/admin-tasks", tags=["Admin"], summary="List internal admin todo tasks")
async def admin_admintasks_list(
    request: Request,
    status: str | None = Query(None, max_length=32),
    category: str | None = Query(None, max_length=64),
    priority: str | None = Query(None, max_length=8),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    params: dict[str, str] = {
        "select": "*",
        "order": "priority.asc,created_at.desc",
        "limit": str(limit),
        "offset": str(offset),
    }
    if status:
        params["status"] = f"eq.{status}"
    if category:
        params["category"] = f"eq.{category}"
    if priority:
        params["priority"] = f"eq.{priority}"
    resp = await _admin_rest_request(request, "GET", "/admin_tasks", params=params)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"List admin_tasks failed: {resp.text[:200]}")
    return JSONResponse(content=resp.json())


@app.post("/api/admin/admin-tasks", tags=["Admin"], summary="Create an internal admin todo task")
async def admin_admintasks_create(
    body: AdminAdminTaskUpsert,
    request: Request,
    caller: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    payload = body.model_dump(exclude_none=True)
    payload["created_by"] = caller["user_id"]
    resp = await _admin_rest_request(
        request,
        "POST",
        "/admin_tasks",
        json_body=payload,
        prefer="return=representation",
    )
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Create admin_task failed: {resp.text[:200]}")
    data = resp.json()
    return JSONResponse(content=data[0] if isinstance(data, list) and data else data)


@app.patch("/api/admin/admin-tasks/{task_id}", tags=["Admin"], summary="Update an internal admin todo task")
async def admin_admintasks_patch(
    task_id: str,
    body: AdminAdminTaskPatch,
    request: Request,
    _: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    try:
        uuid.UUID(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid task_id (must be UUID)") from exc
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(status_code=422, detail="No fields provided")
    resp = await _admin_rest_request(
        request,
        "PATCH",
        "/admin_tasks",
        params={"id": f"eq.{task_id}"},
        json_body=payload,
        prefer="return=representation",
    )
    if resp.status_code not in (200, 204):
        raise HTTPException(status_code=502, detail=f"Patch admin_task failed: {resp.text[:200]}")
    data = resp.json() if resp.status_code == 200 else []
    return JSONResponse(content=data[0] if isinstance(data, list) and data else {})


@app.delete("/api/admin/admin-tasks/{task_id}", tags=["Admin"], summary="Delete an internal admin todo task")
async def admin_admintasks_delete(
    task_id: str,
    request: Request,
    _: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    try:
        uuid.UUID(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid task_id (must be UUID)") from exc
    resp = await _admin_rest_request(
        request,
        "DELETE",
        "/admin_tasks",
        params={"id": f"eq.{task_id}"},
    )
    if resp.status_code not in (200, 204):
        raise HTTPException(status_code=502, detail=f"Delete admin_task failed: {resp.text[:200]}")
    return JSONResponse(content={"id": task_id, "deleted": True})


# --- Feature flags ---------------------------------------------------------


@app.get("/api/admin/feature-flags", tags=["Admin"], summary="List feature flags")
async def admin_feature_flags_list(
    request: Request,
    _: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    resp = await _admin_rest_request(
        request,
        "GET",
        "/feature_flags",
        params={"select": "*", "order": "key.asc"},
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"List feature_flags failed: {resp.text[:200]}")
    return JSONResponse(content=resp.json())


@app.post("/api/admin/feature-flags", tags=["Admin"], summary="Upsert a feature flag")
async def admin_feature_flags_upsert(
    body: AdminFeatureFlagUpsert,
    request: Request,
    caller: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    payload = body.model_dump(exclude_none=True)
    payload["updated_by"] = caller["user_id"]
    resp = await _admin_rest_request(
        request,
        "POST",
        "/feature_flags",
        json_body=payload,
        prefer="resolution=merge-duplicates,return=representation",
    )
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Upsert feature_flag failed: {resp.text[:200]}")
    # Audit the change
    try:
        await _admin_rpc_call(
            request,
            "admin_audit_insert",
            {
                "p_actor_id": caller["user_id"],
                "p_action": "feature_flag.upsert",
                "p_target_type": "feature_flag",
                "p_target_id": body.key,
                "p_before": None,
                "p_after": payload,
                "p_metadata": None,
                "p_ip": request.client.host if request.client else None,
                "p_user_agent": request.headers.get("user-agent"),
            },
        )
    except HTTPException:
        pass
    data = resp.json()
    return JSONResponse(content=data[0] if isinstance(data, list) and data else data)


@app.delete("/api/admin/feature-flags/{key}", tags=["Admin"], summary="Delete a feature flag")
async def admin_feature_flags_delete(
    key: str,
    request: Request,
    caller: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    resp = await _admin_rest_request(
        request,
        "DELETE",
        "/feature_flags",
        params={"key": f"eq.{key}"},
    )
    if resp.status_code not in (200, 204):
        raise HTTPException(status_code=502, detail=f"Delete feature_flag failed: {resp.text[:200]}")
    try:
        await _admin_rpc_call(
            request,
            "admin_audit_insert",
            {
                "p_actor_id": caller["user_id"],
                "p_action": "feature_flag.delete",
                "p_target_type": "feature_flag",
                "p_target_id": key,
                "p_before": None,
                "p_after": None,
                "p_metadata": None,
                "p_ip": request.client.host if request.client else None,
                "p_user_agent": request.headers.get("user-agent"),
            },
        )
    except HTTPException:
        pass
    return JSONResponse(content={"key": key, "deleted": True})


# --- Usage rollup ----------------------------------------------------------


@app.get("/api/admin/usage", tags=["Admin"], summary="Daily usage rollup")
async def admin_usage_rollup(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(500, ge=1, le=5000),
    _: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    params = {
        "select": "*",
        "day": f"gte.{since}",
        "order": "day.desc",
        "limit": str(limit),
    }
    resp = await _admin_rest_request(request, "GET", "/usage_rollup_daily", params=params)
    if resp.status_code != 200:
        # Table may be empty or missing — return empty list rather than 502
        logger.warning("admin_usage_rollup_non_200", status=resp.status_code)
        return JSONResponse(content=[])
    return JSONResponse(content=resp.json())


# --- System health probe ---------------------------------------------------


@app.get("/api/admin/health-probe", tags=["Admin"], summary="Deep health probe of all dependencies")
async def admin_health_probe(
    request: Request,
    _: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    """Probe DB, Redis, Supabase, browser service, sandbox, LLM gateway.

    Returns a dict of {component: {ok: bool, detail: str, latency_ms: float}}.
    Each probe runs with a short timeout and is isolated so one failure does
    not mask others. Never raises — always returns 200 with per-component status.
    """
    cfg = _get_config()
    results: dict[str, dict[str, Any]] = {}

    async def _probe(name: str, coro) -> None:
        t0 = time.perf_counter()
        try:
            detail = await asyncio.wait_for(coro, timeout=5.0)
            results[name] = {
                "ok": True,
                "detail": str(detail)[:200] if detail else "ok",
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        except Exception as exc:  # noqa: BLE001
            results[name] = {
                "ok": False,
                "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            }

    async def _db_probe() -> str:
        db = _get_db()
        v = await db.fetchval("SELECT 1")
        return f"select=1 result={v}"

    async def _redis_probe() -> str:
        if _redis is None:
            raise RuntimeError("redis not initialized")
        pong = await _redis.ping()
        return f"ping={pong}"

    async def _supabase_probe() -> str:
        if not cfg.SUPABASE_URL:
            raise RuntimeError("SUPABASE_URL not configured")
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(
                f"{cfg.SUPABASE_URL}/auth/v1/health",
                headers={"apikey": cfg.SUPABASE_ANON_KEY or ""},
            )
        return f"status={r.status_code}"

    async def _browser_probe() -> str:
        url = (
            os.environ.get("BROWSER_BASE_URL")
            or os.environ.get("BROWSER_SERVICE_URL")
            or "http://mariana-browser:8000"
        ).rstrip("/")
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{url}/health")
        return f"status={r.status_code}"

    async def _sandbox_probe() -> str:
        url = (
            os.environ.get("SANDBOX_BASE_URL")
            or os.environ.get("SANDBOX_URL")
            or "http://mariana-sandbox:8000"
        ).rstrip("/")
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{url}/health")
        return f"status={r.status_code}"

    async def _gateway_probe() -> str:
        base = os.environ.get("LLM_GATEWAY_BASE_URL", "").rstrip("/")
        if not base:
            raise RuntimeError("LLM_GATEWAY_BASE_URL not configured")
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{base}/models", headers={
                "Authorization": f"Bearer {os.environ.get('LLM_GATEWAY_API_KEY','')}"
            })
        return f"status={r.status_code}"

    await asyncio.gather(
        _probe("database", _db_probe()),
        _probe("redis", _redis_probe()),
        _probe("supabase", _supabase_probe()),
        _probe("browser", _browser_probe()),
        _probe("sandbox", _sandbox_probe()),
        _probe("llm_gateway", _gateway_probe()),
        return_exceptions=True,
    )

    overall_ok = all(r["ok"] for r in results.values())
    return JSONResponse(
        content={
            "ok": overall_ok,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "components": results,
        }
    )


# --- Danger zone ops -------------------------------------------------------


class AdminDangerConfirm(BaseModel):
    confirm: str = Field(..., description="Must equal 'I UNDERSTAND' to proceed")


@app.post("/api/admin/danger/flush-redis", tags=["Admin"], summary="DANGER: flush Redis (cache + queues)")
async def admin_danger_flush_redis(
    body: AdminDangerConfirm,
    request: Request,
    caller: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    if body.confirm != "I UNDERSTAND":
        raise HTTPException(status_code=422, detail="Confirmation phrase required")
    if _redis is None:
        raise HTTPException(status_code=503, detail="Redis not initialized")
    try:
        await _redis.flushdb()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Flush failed: {exc}") from exc
    logger.warning("admin_danger_flush_redis", actor=caller["user_id"])
    try:
        await _admin_rpc_call(
            request,
            "admin_audit_insert",
            {
                "p_actor_id": caller["user_id"],
                "p_action": "danger.flush_redis",
                "p_target_type": "system",
                "p_target_id": "redis",
                "p_before": None,
                "p_after": None,
                "p_metadata": None,
                "p_ip": request.client.host if request.client else None,
                "p_user_agent": request.headers.get("user-agent"),
            },
        )
    except HTTPException:
        pass
    return JSONResponse(content={"flushed": True})


@app.post("/api/admin/danger/halt-running", tags=["Admin"], summary="DANGER: halt all RUNNING tasks")
async def admin_danger_halt_running(
    body: AdminDangerConfirm,
    request: Request,
    caller: dict[str, str] = Depends(_require_admin),
) -> JSONResponse:
    if body.confirm != "I UNDERSTAND":
        raise HTTPException(status_code=422, detail="Confirmation phrase required")
    db = _get_db()
    result = await db.execute(
        "UPDATE research_tasks SET status='HALTED' WHERE status='RUNNING'"
    )
    # asyncpg returns 'UPDATE N' string
    halted = 0
    try:
        halted = int(result.split()[-1])
    except Exception:
        pass
    logger.warning("admin_danger_halt_running", actor=caller["user_id"], halted=halted)
    try:
        await _admin_rpc_call(
            request,
            "admin_audit_insert",
            {
                "p_actor_id": caller["user_id"],
                "p_action": "danger.halt_running",
                "p_target_type": "system",
                "p_target_id": "research_tasks",
                "p_before": None,
                "p_after": {"halted": halted},
                "p_metadata": None,
                "p_ip": request.client.host if request.client else None,
                "p_user_agent": request.headers.get("user-agent"),
            },
        )
    except HTTPException:
        pass
    return JSONResponse(content={"halted": halted})


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
    # M-11 fix: constant-time comparison to prevent timing-attack recovery of
    # the admin key byte-by-byte.
    if not hmac.compare_digest((x_admin_key or "").encode("utf-8"), admin_key.encode("utf-8")):
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

    # Schedule OS-level exit after a brief delay to let the response flush.
    # BUG-API-031: increased from 1s to 3s to give downstream proxies/clients
    # more time to consume the final response body before the process exits.
    asyncio.get_running_loop().call_later(3.0, _exit_process)
    return ShutdownResponse(message="Graceful shutdown initiated")


def _exit_process() -> None:
    """Trigger process termination after the shutdown response is flushed.

    Prefer SIGTERM so the ASGI server can run its normal graceful-shutdown
    path (lifespan teardown, connection cleanup, task cancellation). Fall back
    to ``os._exit`` only if signalling the current process fails.
    """
    logger.info("api_process_exit_signal")
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception as exc:  # noqa: BLE001
        logger.error("api_process_exit_signal_failed", error=str(exc))
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
    # P0-FIX-6: Require explicit ownership match; don't allow deletion of
    # orphaned skills (owner_id=None) by any user — only admin can clean those up.
    if not _is_admin_user(current_user["user_id"]):
        if not skill.owner_id or skill.owner_id != current_user["user_id"]:
            raise HTTPException(status_code=403, detail="Not authorized to delete this skill")

    mgr.delete_skill(skill_id, owner_id=current_user["user_id"])
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


# ===========================================================================
# Learning Loop — Feedback & Insights endpoints
# ===========================================================================


class FeedbackRequest(BaseModel):
    """Request body for submitting investigation feedback."""
    task_id: str | None = Field(None, description="UUID of the related investigation")
    event_type: str = Field(..., description="One of: rating, feedback, correction, preference")
    category: str | None = Field(None, description="Category: report_quality, search_depth, branch_decision, general")
    content: dict = Field(..., description="Structured feedback payload")

    @field_validator("content")
    @classmethod
    def _cap_content_size(cls, value: dict) -> dict:
        # BUG-API-014: structured feedback can be very large; enforce a cap.
        validated = _validate_dict_size(value)
        # _validate_dict_size returns None only when input was None; content is required so that should never happen.
        assert validated is not None
        return validated


class FeedbackResponse(BaseModel):
    event_id: str
    status: str = "recorded"


class InsightItem(BaseModel):
    id: str
    insight_type: str
    insight_key: str
    insight_value: dict
    confidence: float
    sample_count: int
    last_updated: str | None


class InsightsResponse(BaseModel):
    insights: list[InsightItem]
    count: int


class LearningContextResponse(BaseModel):
    context: str
    has_insights: bool


class OutcomeResponse(BaseModel):
    task_id: str
    topic: str
    quality_tier: str | None
    total_cost_usd: float
    total_ai_calls: int
    duration_seconds: int
    final_state: str | None
    report_generated: bool
    user_rating: int | None
    user_feedback: str | None
    hypotheses_count: int
    findings_count: int
    killed_branches_count: int
    skeptic_pass: bool | None
    created_at: str | None


@app.post(
    "/api/feedback",
    response_model=FeedbackResponse,
    summary="Submit investigation feedback",
    tags=["learning"],
)
async def submit_feedback(
    body: FeedbackRequest,
    current_user: dict[str, str] = Depends(_get_current_user),
) -> FeedbackResponse:
    """Submit feedback for an investigation (rating, correction, preference)."""
    db = _get_db()
    from mariana.orchestrator.learning import record_feedback  # noqa: PLC0415

    if body.event_type not in ("rating", "feedback", "correction", "preference"):
        raise HTTPException(status_code=400, detail="event_type must be one of: rating, feedback, correction, preference")

    # P0-FIX-4: Verify the task belongs to the current user before accepting feedback.
    if body.task_id:
        validated_task_id = _validate_task_id(body.task_id)  # BUG-API-001
        row = await db.fetchrow("SELECT metadata FROM research_tasks WHERE id = $1", validated_task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Investigation not found")
        if not _is_admin_user(current_user["user_id"]):
            meta = row.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            if meta.get("user_id") != current_user["user_id"]:
                raise HTTPException(status_code=403, detail="Not authorized to submit feedback for this investigation")

    event_id = await record_feedback(
        user_id=current_user["user_id"],
        task_id=body.task_id,
        event_type=body.event_type,
        category=body.category,
        content=body.content,
        db=db,
    )
    if not event_id:
        raise HTTPException(status_code=500, detail="Failed to record feedback")

    return FeedbackResponse(event_id=event_id)


@app.get(
    "/api/feedback/{task_id}",
    summary="Get feedback for an investigation",
    tags=["learning"],
)
async def get_feedback(
    task_id: str,
    current_user: dict[str, str] = Depends(_require_investigation_owner),
) -> JSONResponse:
    """Fetch all feedback events for a specific investigation."""
    db = _get_db()
    from mariana.orchestrator.learning import get_investigation_feedback  # noqa: PLC0415

    events = await get_investigation_feedback(task_id, db)
    return JSONResponse(content={"feedback": events, "count": len(events)})


@app.get(
    "/api/learning/insights",
    response_model=InsightsResponse,
    summary="Get user learning insights",
    tags=["learning"],
)
async def get_insights(
    current_user: dict[str, str] = Depends(_get_current_user),
) -> InsightsResponse:
    """Fetch all learning insights extracted from the user's investigations."""
    db = _get_db()
    from mariana.orchestrator.learning import get_user_insights  # noqa: PLC0415

    insights = await get_user_insights(current_user["user_id"], db)
    return InsightsResponse(
        insights=[InsightItem(**i) for i in insights],
        count=len(insights),
    )


@app.get(
    "/api/learning/context",
    response_model=LearningContextResponse,
    summary="Get learning context for prompts",
    tags=["learning"],
)
async def get_learning_context(
    current_user: dict[str, str] = Depends(_get_current_user),
) -> LearningContextResponse:
    """Get the formatted learning context string used for prompt injection."""
    db = _get_db()
    from mariana.orchestrator.learning import build_learning_context  # noqa: PLC0415

    context = await build_learning_context(current_user["user_id"], db)
    return LearningContextResponse(
        context=context,
        has_insights=bool(context),
    )


@app.post(
    "/api/learning/extract",
    summary="Trigger full pattern extraction",
    tags=["learning"],
)
async def trigger_extraction(
    current_user: dict[str, str] = Depends(_get_current_user),
) -> JSONResponse:
    """Trigger full pattern extraction across all user's investigations."""
    db = _get_db()
    from mariana.orchestrator.learning import extract_patterns  # noqa: PLC0415

    count = await extract_patterns(current_user["user_id"], db)
    return JSONResponse(content={"insights_updated": count, "status": "complete"})


@app.get(
    "/api/learning/outcome/{task_id}",
    response_model=OutcomeResponse,
    summary="Get investigation outcome",
    tags=["learning"],
)
async def get_outcome(
    task_id: str,
    current_user: dict[str, str] = Depends(_require_investigation_owner),
) -> OutcomeResponse:
    """Get the automated outcome record for an investigation."""
    db = _get_db()
    row = await db.fetchrow(
        "SELECT * FROM investigation_outcomes WHERE task_id = $1",
        task_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"No outcome found for task {task_id}")

    return OutcomeResponse(
        task_id=row["task_id"],
        topic=row["topic"],
        quality_tier=row["quality_tier"],
        total_cost_usd=float(row["total_cost_usd"] or 0),
        total_ai_calls=int(row["total_ai_calls"] or 0),
        duration_seconds=int(row["duration_seconds"] or 0),
        final_state=row["final_state"],
        report_generated=bool(row["report_generated"]),
        user_rating=row["user_rating"],
        user_feedback=row["user_feedback"],
        hypotheses_count=int(row["hypotheses_count"] or 0),
        findings_count=int(row["findings_count"] or 0),
        killed_branches_count=int(row["killed_branches_count"] or 0),
        skeptic_pass=row["skeptic_pass"],
        created_at=row["created_at"].isoformat() if row["created_at"] else None,
    )


# ---------------------------------------------------------------------------
# Intelligence Engine API endpoints
# ---------------------------------------------------------------------------


@app.get(
    "/api/intelligence/{task_id}/claims",
    summary="Get evidence ledger (all extracted claims)",
    tags=["intelligence"],
)
async def get_claims(
    task_id: str,
    current_user: dict[str, str] = Depends(_require_investigation_owner),
) -> JSONResponse:
    """Fetch all atomic claims extracted from research findings."""
    db = _get_db()
    from mariana.orchestrator.intelligence.evidence_ledger import get_evidence_ledger  # noqa: PLC0415
    claims = await get_evidence_ledger(task_id, db)
    return JSONResponse(content=_jsonable({"claims": claims, "count": len(claims)}))


@app.get(
    "/api/intelligence/{task_id}/claims/summary",
    summary="Get evidence ledger summary",
    tags=["intelligence"],
)
async def get_claims_summary(
    task_id: str,
    current_user: dict[str, str] = Depends(_require_investigation_owner),
) -> JSONResponse:
    """Fetch summary statistics for the evidence ledger."""
    db = _get_db()
    from mariana.orchestrator.intelligence.evidence_ledger import get_ledger_summary  # noqa: PLC0415
    summary = await get_ledger_summary(task_id, db)
    return JSONResponse(content=summary)


@app.get(
    "/api/intelligence/{task_id}/source-scores",
    summary="Get source credibility scores",
    tags=["intelligence"],
)
async def get_source_scores(
    task_id: str,
    current_user: dict[str, str] = Depends(_require_investigation_owner),
) -> JSONResponse:
    """Fetch credibility scores for all sources in an investigation."""
    db = _get_db()
    from mariana.orchestrator.intelligence.credibility import get_source_scores, get_average_credibility  # noqa: PLC0415
    scores = await get_source_scores(task_id, db)
    avg = await get_average_credibility(task_id, db)
    return JSONResponse(content=_jsonable({"scores": scores, "count": len(scores), "average_credibility": avg}))


@app.get(
    "/api/intelligence/{task_id}/contradictions",
    summary="Get contradiction matrix",
    tags=["intelligence"],
)
async def get_contradictions(
    task_id: str,
    current_user: dict[str, str] = Depends(_require_investigation_owner),
) -> JSONResponse:
    """Fetch detected contradictions between claims."""
    db = _get_db()
    from mariana.orchestrator.intelligence.contradictions import get_contradiction_matrix  # noqa: PLC0415
    matrix = await get_contradiction_matrix(task_id, db)
    return JSONResponse(content=_jsonable(matrix))


@app.get(
    "/api/intelligence/{task_id}/hypotheses/rankings",
    summary="Get Bayesian hypothesis rankings",
    tags=["intelligence"],
)
async def get_hypothesis_rankings(
    task_id: str,
    current_user: dict[str, str] = Depends(_require_investigation_owner),
) -> JSONResponse:
    """Fetch Bayesian posterior rankings for all hypotheses."""
    db = _get_db()
    from mariana.orchestrator.intelligence.hypothesis_engine import get_hypothesis_rankings, get_winning_hypothesis  # noqa: PLC0415
    rankings = await get_hypothesis_rankings(task_id, db)
    winner = await get_winning_hypothesis(task_id, db)
    return JSONResponse(content=_jsonable({"rankings": rankings, "winner": winner}))


@app.get(
    "/api/intelligence/{task_id}/gaps",
    summary="Get gap analysis",
    tags=["intelligence"],
)
async def get_gaps(
    task_id: str,
    current_user: dict[str, str] = Depends(_require_investigation_owner),
) -> JSONResponse:
    """Fetch the latest gap analysis (missing evidence, completeness score)."""
    db = _get_db()
    from mariana.orchestrator.intelligence.gap_detector import get_latest_gap_analysis  # noqa: PLC0415
    gap = await get_latest_gap_analysis(task_id, db)
    if gap is None:
        return JSONResponse(content={"gap_analysis": None, "status": "not_yet_run"})
    return JSONResponse(content=_jsonable({"gap_analysis": gap}))


@app.get(
    "/api/intelligence/{task_id}/temporal",
    summary="Get temporal analysis",
    tags=["intelligence"],
)
async def get_temporal(
    task_id: str,
    current_user: dict[str, str] = Depends(_require_investigation_owner),
) -> JSONResponse:
    """Fetch temporal coverage and timeline of claims."""
    db = _get_db()
    from mariana.orchestrator.intelligence.temporal import get_temporal_coverage  # noqa: PLC0415
    coverage = await get_temporal_coverage(task_id, db)
    # Get a flat timeline of all temporally-tagged claims
    timeline_rows = await db.fetch(
        """
        SELECT id, subject, predicate, object, claim_text,
               confidence, temporal_start, temporal_end, temporal_type
        FROM claims
        WHERE task_id = $1 AND temporal_start IS NOT NULL
        ORDER BY temporal_start ASC
        LIMIT 100
        """,
        task_id,
    )
    from mariana.data.db import _row_to_dict  # noqa: PLC0415
    timeline = []
    for r in timeline_rows:
        d = _row_to_dict(r)
        for k in ("temporal_start", "temporal_end"):
            if d.get(k):
                d[k] = d[k].isoformat()
        timeline.append(d)
    return JSONResponse(content=_jsonable({"coverage": coverage, "timeline": timeline}))


@app.get(
    "/api/intelligence/{task_id}/perspectives",
    summary="Get multi-perspective synthesis",
    tags=["intelligence"],
)
async def get_perspectives(
    task_id: str,
    current_user: dict[str, str] = Depends(_require_investigation_owner),
) -> JSONResponse:
    """Fetch multi-perspective synthesis (bull/bear/skeptic/expert views)."""
    db = _get_db()
    rows = await db.fetch(
        """
        SELECT id, task_id, perspective, synthesis_text, confidence, key_arguments,
               cited_claim_ids, created_at
        FROM perspective_syntheses
        WHERE task_id = $1
        ORDER BY created_at DESC
        """,
        task_id,
    )
    from mariana.data.db import _row_to_dict  # noqa: PLC0415
    perspectives = [_row_to_dict(r) for r in rows]
    # Serialize datetimes
    for p in perspectives:
        if p.get("created_at"):
            p["created_at"] = p["created_at"].isoformat()
    return JSONResponse(content=_jsonable({"perspectives": perspectives, "count": len(perspectives)}))


@app.get(
    "/api/intelligence/{task_id}/audit",
    summary="Get reasoning chain audit",
    tags=["intelligence"],
)
async def get_audit(
    task_id: str,
    current_user: dict[str, str] = Depends(_require_investigation_owner),
) -> JSONResponse:
    """Fetch the latest reasoning chain audit results."""
    db = _get_db()
    from mariana.orchestrator.intelligence.auditor import get_latest_audit  # noqa: PLC0415
    audit = await get_latest_audit(task_id, db)
    if audit is None:
        return JSONResponse(content={"audit": None, "status": "not_yet_run"})
    return JSONResponse(content=_jsonable({"audit": audit}))


@app.get(
    "/api/intelligence/{task_id}/executive-summary",
    summary="Get executive summaries",
    tags=["intelligence"],
)
async def get_executive_summary(
    task_id: str,
    current_user: dict[str, str] = Depends(_require_investigation_owner),
) -> JSONResponse:
    """Fetch executive summaries at all compression levels."""
    db = _get_db()
    from mariana.orchestrator.intelligence.executive_summary import get_executive_summary as _get_exec_summary  # noqa: PLC0415
    summary = await _get_exec_summary(task_id, db)
    if summary is None:
        return JSONResponse(content={"summary": None, "status": "not_yet_generated"})
    return JSONResponse(content=_jsonable({"summary": summary}))


@app.get(
    "/api/intelligence/{task_id}/diversity",
    summary="Get source diversity assessment",
    tags=["intelligence"],
)
async def get_diversity(
    task_id: str,
    current_user: dict[str, str] = Depends(_require_investigation_owner),
) -> JSONResponse:
    """Fetch source diversity assessment for an investigation."""
    db = _get_db()
    from mariana.orchestrator.intelligence.diversity import assess_diversity  # noqa: PLC0415
    result = await assess_diversity(task_id, db)
    return JSONResponse(content=_jsonable(result))


@app.get(
    "/api/intelligence/{task_id}/overview",
    summary="Get full intelligence engine overview",
    tags=["intelligence"],
)
async def get_intelligence_overview(
    task_id: str,
    current_user: dict[str, str] = Depends(_require_investigation_owner),
) -> JSONResponse:
    """Comprehensive intelligence overview: claims, credibility, contradictions,
    Bayesian rankings, gaps, audit, perspectives, and executive summary — all in one call."""
    db = _get_db()

    overview: dict = {"task_id": task_id}

    # Claims summary
    try:
        from mariana.orchestrator.intelligence.evidence_ledger import get_ledger_summary  # noqa: PLC0415
        overview["claims"] = await get_ledger_summary(task_id, db)
    except Exception:
        overview["claims"] = None

    # Source credibility
    try:
        from mariana.orchestrator.intelligence.credibility import get_average_credibility  # noqa: PLC0415
        overview["average_credibility"] = await get_average_credibility(task_id, db)
    except Exception:
        overview["average_credibility"] = None

    # Contradictions count
    try:
        cnt = await db.fetchval(
            "SELECT COUNT(*) FROM contradiction_pairs WHERE task_id = $1",
            task_id,
        )
        overview["contradictions_count"] = cnt or 0
    except Exception:
        overview["contradictions_count"] = 0

    # Bayesian winner
    try:
        from mariana.orchestrator.intelligence.hypothesis_engine import get_winning_hypothesis  # noqa: PLC0415
        overview["bayesian_winner"] = await get_winning_hypothesis(task_id, db)
    except Exception:
        overview["bayesian_winner"] = None

    # Gap analysis
    try:
        from mariana.orchestrator.intelligence.gap_detector import get_latest_gap_analysis  # noqa: PLC0415
        gap = await get_latest_gap_analysis(task_id, db)
        overview["completeness_score"] = gap.get("completeness_score") if gap else None
        overview["gaps_found"] = len(gap.get("gaps", [])) if gap else 0
    except Exception:
        overview["completeness_score"] = None
        overview["gaps_found"] = 0

    # Audit
    try:
        from mariana.orchestrator.intelligence.auditor import get_latest_audit  # noqa: PLC0415
        audit = await get_latest_audit(task_id, db)
        overview["audit_passed"] = audit.get("passed") if audit else None
        overview["audit_score"] = audit.get("overall_score") if audit else None
    except Exception:
        overview["audit_passed"] = None
        overview["audit_score"] = None

    # Executive summary one-liner
    try:
        from mariana.orchestrator.intelligence.executive_summary import get_executive_summary as _get_es  # noqa: PLC0415
        es = await _get_es(task_id, db)
        overview["one_liner"] = es.get("one_liner", "") if es else ""
    except Exception:
        overview["one_liner"] = ""

    return JSONResponse(content=_jsonable(overview))


# ---------------------------------------------------------------------------
# Custom 422 handler — return structured JSON for validation errors
# ---------------------------------------------------------------------------


# BUG-037: Register on RequestValidationError (not integer 422) and return
# structured field-level errors via exc.errors() instead of str(exc).
@app.exception_handler(json.JSONDecodeError)
async def json_decode_error_handler(
    request: Request, exc: json.JSONDecodeError
) -> JSONResponse:
    """Handle malformed JSON (e.g. Infinity, NaN, trailing commas)."""
    return JSONResponse(
        status_code=400,
        content={"detail": "Invalid JSON in request body", "type": "json_parse_error"},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return a structured JSON body for Pydantic validation errors.

    ADV-FIX: exc.errors() can contain bytes or other non-serializable types
    (e.g. when body has null bytes or invalid encoding).  We sanitize the
    errors list so JSONResponse never crashes with TypeError.
    """
    import json as _json

    def _sanitize(obj: object) -> object:
        if isinstance(obj, bytes):
            return obj.decode("utf-8", errors="replace")
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_sanitize(i) for i in obj]
        # Fallback: force str for anything else non-serializable
        try:
            _json.dumps(obj)
            return obj
        except (TypeError, ValueError):
            return str(obj)

    try:
        errors = _sanitize(exc.errors())
    except Exception:
        errors = [{"msg": "Validation error", "type": "value_error"}]

    return JSONResponse(
        status_code=422,
        content={"detail": errors, "type": "validation_error"},
    )
