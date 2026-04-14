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
    BROWSER_POOL_HOST   — bind address (default: 0.0.0.0)
    BROWSER_POOL_PORT   — bind port    (default: 8888)
    BROWSER_POOL_SIZE   — target pool size for future production use
                          (default: 5, currently unused in prototype)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

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

_STARTUP_TIME: datetime = datetime.now(tz=timezone.utc)

app = FastAPI(
    title="Mariana Browser Pool",
    description=(
        "Manages Playwright/Chromium browser contexts for JavaScript-heavy pages. "
        "This is the prototype placeholder — real browser automation is not yet active."
    ),
    version="0.1.0-prototype",
)


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
            ),
            "note": (
                "Prototype mode — using HTTP API connectors (httpx) instead of "
                "Playwright browser automation.  Browser pool will be activated "
                "in the production build."
            ),
        },
    )


@app.post(
    "/dispatch",
    summary="Dispatch a browser task",
    description=(
        "In prototype mode all dispatch requests are acknowledged but not executed. "
        "The orchestrator should fall back to the HTTP connectors for data retrieval."
    ),
    response_model=DispatchResponse,
)
async def dispatch_task(task: BrowserTask) -> DispatchResponse:
    """
    Accept and acknowledge a browser task without executing it.

    The orchestrator is designed to treat a ``status='skipped'`` response as a
    signal to fall through to the API connector layer.  This keeps the
    orchestrator logic consistent between prototype and production without
    requiring conditional branches.

    In production this endpoint will:
    1. Acquire a Playwright context from the pool.
    2. Navigate to ``task.url``.
    3. Wait for ``task.wait_for_selector`` if provided.
    4. Extract and return page text or HTML.
    5. Release the context back to the pool.
    """
    logger.info(
        "browser_dispatch_received (prototype — skipped): url=%s task_id=%s",
        task.url,
        task.task_id,
    )
    return DispatchResponse(
        status="skipped",
        url=task.url,
        reason=(
            "Prototype mode: browser pool is not active.  "
            "Caller should use HTTP connector (httpx) for this URL."
        ),
    )


@app.get(
    "/pool/status",
    summary="Pool status",
    description="Returns detailed pool metrics for monitoring dashboards.",
)
async def pool_status() -> JSONResponse:
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
            "failed_tasks": 0,
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
    logger.error("Unhandled error in browser pool server: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "error": type(exc).__name__,
            "detail": str(exc),
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the browser pool server using configuration from environment."""
    host = os.getenv("BROWSER_POOL_HOST", "0.0.0.0")
    port = int(os.getenv("BROWSER_POOL_PORT", "8888"))
    log_level = os.getenv("LOG_LEVEL", "info").lower()

    logger.info("Starting Mariana browser pool server (prototype): %s:%d", host, port)

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
