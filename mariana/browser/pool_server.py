"""
mariana/browser/pool_server.py

Placeholder browser pool server for the Mariana prototype.

In production this module will manage a pool of Playwright/Chromium browser
contexts for JavaScript-heavy pages (e.g., exchange data feeds, captcha-gated
portals, dynamic SPA-driven news sites).

For the current prototype all web access is handled by the HTTP-level
connectors in ``mariana.connectors``, which use ``httpx`` directly and do not
require a real browser.  This server exists so the orchestrator's health
check and dispatch calls have a valid endpoint and so the production upgrade
path is already wired in.

Usage (standalone):
    python -m mariana.browser.pool_server          # defaults
    BROWSER_POOL_PORT=9090 python -m mariana.browser.pool_server

Environment variables:
    BROWSER_POOL_HOST   — bind address (default: 127.0.0.1; set explicitly to
                          0.0.0.0 only for network-accessible deployments)
    BROWSER_POOL_PORT   — bind port    (default: 8888)
    BROWSER_POOL_SIZE   — target pool size for future production use
                          (default: 5, currently unused in prototype)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import structlog
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# B-40: shared-secret authentication for /dispatch and /pool/status
# ---------------------------------------------------------------------------

# The orchestrator and pool server share BROWSER_POOL_SECRET at deploy time.
# /health remains unauthenticated for load balancer liveness checks.


def _get_pool_secret() -> str:
    """Return the configured BROWSER_POOL_SECRET (may be empty in dev)."""
    return os.getenv("BROWSER_POOL_SECRET", "")


async def _require_pool_auth(
    x_browser_pool_token: str | None = Header(default=None),
) -> None:
    """FastAPI dependency that enforces the BROWSER_POOL_SECRET header check.

    Returns immediately when BROWSER_POOL_SECRET is not configured (dev mode).
    In production, rejects any request that does not supply the correct
    X-Browser-Pool-Token header value.
    """
    secret = _get_pool_secret()
    if not secret:
        # Secret not configured — allow all (development / test).
        return
    if x_browser_pool_token != secret:
        logger.warning(
            "browser_pool_auth_rejected",
            reason="missing or incorrect X-Browser-Pool-Token header",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or incorrect X-Browser-Pool-Token header",
        )


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class BrowserTask(BaseModel):
    """A browser dispatch request sent by the orchestrator."""

    url: str = Field(..., min_length=7, description="Target URL to render")
    task_id: str = Field(..., min_length=1, description="Parent ResearchTask UUID")
    branch_id: str | None = Field(default=None, description="Branch that requested this task")
    wait_for_selector: str | None = Field(
        default=None,
        description="CSS selector to wait for before capturing content",
    )
    extract_text: bool = Field(
        default=True,
        description="Whether to return inner text (True) or full HTML (False)",
    )
    timeout_ms: int = Field(
        default=30_000,
        ge=1_000,
        le=120_000,
        description="Maximum wait time in milliseconds",
    )


class DispatchResponse(BaseModel):
    """Response returned for a browser dispatch request."""

    status: str
    url: str | None = None
    reason: str | None = None
    content: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

_STARTUP_TIME: datetime | None = None

# Module-level counters for monitoring.
_SKIPPED_COUNT: int = 0
_ERROR_COUNT: int = 0

app = FastAPI(
    title="Mariana Browser Pool",
    description=(
        "Manages Playwright/Chromium browser contexts for JavaScript-heavy page rendering."
    ),
    version="0.1.0",
)


@app.on_event("startup")
async def _set_startup_time() -> None:
    global _STARTUP_TIME
    _STARTUP_TIME = datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get(
    "/health",
    summary="Health check",
    description="Returns pool status.  Always 200 OK in prototype mode.",
)
async def health() -> JSONResponse:
    """
    Lightweight health probe used by the orchestrator before dispatching tasks.

    Returns the pool size (0 in prototype mode) and an informational note
    explaining that the prototype uses API connectors rather than browser
    automation.
    """
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "ok",
            "mode": "prototype",
            "pool_size": 0,
            "pool_size_target": int(os.getenv("BROWSER_POOL_SIZE", "5")),
            "uptime_seconds": int(
                (datetime.now(tz=timezone.utc) - _STARTUP_TIME).total_seconds()
            ) if _STARTUP_TIME else 0,
            "note": (
                "Prototype mode — using HTTP API connectors (httpx) instead of "
                "Playwright browser automation.  Browser pool will be activated "
                "in the production build."
            ),
        },
    )


@app.post(
    "/dispatch",
    response_model=DispatchResponse,
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    summary="Dispatch a browser task",
    description=(
        "Browser pool is not yet configured. Returns HTTP 503 for every request. "
        "The orchestrator should fall back to the HTTP connectors for data retrieval."
    ),
)
async def dispatch_task(
    task: BrowserTask,
    _auth: None = Depends(_require_pool_auth),  # B-40: shared-secret auth
) -> DispatchResponse:
    """
    Return HTTP 503 for every dispatch request — the browser pool is not active.

    The orchestrator must handle this 503 by falling through to the API connector
    layer (httpx).  A 503 is used instead of a 200/skipped so that callers that
    do not explicitly handle the skipped status still surface a visible error.

    In production this endpoint will:
    1. Acquire a Playwright context from the pool.
    2. Navigate to ``task.url``.
    3. Wait for ``task.wait_for_selector`` if provided.
    4. Extract and return page text or HTML.
    5. Release the context back to the pool.
    """
    global _SKIPPED_COUNT
    _SKIPPED_COUNT += 1
    logger.warning(
        "browser_dispatch_unavailable",
        url=task.url,
        task_id=task.task_id,
        skipped_total=_SKIPPED_COUNT,
    )
    return DispatchResponse(
        status="unavailable",
        error="Browser pool not configured",
    )


@app.get(
    "/pool/status",
    summary="Pool status",
    description="Returns detailed pool metrics for monitoring dashboards.",
)
async def pool_status(
    _auth: None = Depends(_require_pool_auth),  # B-40: shared-secret auth
) -> JSONResponse:
    """
    Return detailed pool metrics.

    In prototype mode all worker counts are zero.  In production this will
    return per-worker health, current page counts, memory usage, and crash
    history.
    """
    return JSONResponse(
        content={
            "total_workers": 0,
            "idle_workers": 0,
            "busy_workers": 0,
            "queued_tasks": 0,
            "completed_tasks": 0,
            "failed_tasks": _ERROR_COUNT,
            "skipped_tasks": _SKIPPED_COUNT,
            "mode": "prototype",
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Catch-all exception handler so the pool server never returns a raw 500
    stack trace to the orchestrator.
    """
    global _ERROR_COUNT
    _ERROR_COUNT += 1
    logger.error("unhandled_browser_pool_error", error=str(exc), error_type=type(exc).__name__)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "error": "internal_server_error",
            "detail": "An unexpected error occurred. Check server logs for details.",
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the browser pool server using configuration from environment."""
    host = os.getenv("BROWSER_POOL_HOST", "127.0.0.1")
    port = int(os.getenv("BROWSER_POOL_PORT", "8888"))
    log_level = os.getenv("LOG_LEVEL", "info").lower()

    logger.info("browser_pool_server_starting", host=host, port=port)

    # BUG-A08 note: _SKIPPED_COUNT and _ERROR_COUNT are plain module-level ints
    # mutated with += 1 in route handlers.  This is safe under the asyncio event
    # loop (single-threaded), but becomes a read-modify-write race if uvicorn is
    # ever started with workers > 1 (multi-process).  This server MUST be run
    # with a single worker (the default below).  Do NOT set workers > 1.
    uvicorn.run(
        "mariana.browser.pool_server:app",
        host=host,
        port=port,
        log_level=log_level,
        access_log=True,
        reload=False,
    )


if __name__ == "__main__":
    main()
