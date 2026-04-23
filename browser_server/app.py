"""Mariana browser pool — a small HTTP surface over a Playwright browser pool.

Endpoints
---------
POST /fetch           — navigate and extract HTML/text/title/links
POST /screenshot      — full-page or viewport PNG (returned as base64)
POST /pdf             — render page to PDF (base64)
POST /click_and_fetch — navigate, click a selector, return resulting page
POST /eval            — evaluate arbitrary JS expression on the page, return result
GET  /health

All endpoints require an `x-sandbox-secret` header matching BROWSER_SHARED_SECRET.

Design notes
------------
* A single Chromium instance is launched at startup and reused for every
  request.  Each request gets its own *context* (incognito-equivalent) which
  is destroyed when the request finishes.  This is cheap (~30ms) and isolates
  cookies / storage between calls.
* A semaphore caps concurrent contexts at BROWSER_POOL_SIZE to protect RAM.
* Default navigation waits for `networkidle` with a sane wall-clock cap.
* Error responses include the full diagnostic text so the orchestrator can
  feed it back into the LLM for self-correction.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)
from pydantic import BaseModel, Field, field_validator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("browser")

BROWSER_SHARED_SECRET = os.getenv("BROWSER_SHARED_SECRET", "")
POOL_SIZE = int(os.getenv("BROWSER_POOL_SIZE", "4"))
DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 120_000
MAX_URL_LEN = 4096
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 MarianaBot/1.0"
)


# -----------------------------------------------------------------------------
# Playwright lifecycle
# -----------------------------------------------------------------------------

_pw: Playwright | None = None
_browser: Browser | None = None
_pool_sema: asyncio.Semaphore | None = None


@asynccontextmanager
async def _lifespan(app: FastAPI):  # type: ignore[override]
    global _pw, _browser, _pool_sema
    log.info("starting playwright")
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",  # safe: container already drops caps and runs non-root user
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-zygote",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-breakpad",
            "--disable-client-side-phishing-detection",
            "--disable-features=TranslateUI",
            "--disable-hang-monitor",
            "--disable-ipc-flooding-protection",
            "--disable-popup-blocking",
            "--disable-renderer-backgrounding",
            "--disable-sync",
            "--force-color-profile=srgb",
            "--metrics-recording-only",
            "--mute-audio",
        ],
    )
    _pool_sema = asyncio.Semaphore(POOL_SIZE)
    log.info("browser ready pool_size=%d", POOL_SIZE)
    try:
        yield
    finally:
        log.info("shutting down browser")
        if _browser:
            try:
                await _browser.close()
            except Exception:  # noqa: BLE001
                pass
        if _pw:
            try:
                await _pw.stop()
            except Exception:  # noqa: BLE001
                pass


app = FastAPI(
    title="Mariana Browser",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):  # type: ignore[override]
    if request.url.path == "/health":
        return await call_next(request)
    if not BROWSER_SHARED_SECRET:
        return JSONResponse({"detail": "browser misconfigured"}, status_code=503)
    provided = request.headers.get("x-sandbox-secret", "")
    if not secrets.compare_digest(provided, BROWSER_SHARED_SECRET):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok" if _browser and _browser.is_connected() else "unavailable",
        "pool_size": POOL_SIZE,
        "ts": time.time(),
    }


# -----------------------------------------------------------------------------
# Pool helper — get a fresh context with a fresh page
# -----------------------------------------------------------------------------


@asynccontextmanager
async def _acquire_page(
    *,
    user_agent: str = DEFAULT_UA,
    viewport: dict[str, int] | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
):
    if _pool_sema is None or _browser is None:
        raise HTTPException(503, "browser not ready")
    await _pool_sema.acquire()
    ctx: BrowserContext | None = None
    try:
        ctx = await _browser.new_context(
            user_agent=user_agent,
            viewport=viewport or {"width": 1440, "height": 900},
            java_script_enabled=True,
            ignore_https_errors=False,
            bypass_csp=False,
            locale="en-US",
        )
        ctx.set_default_timeout(timeout_ms)
        page = await ctx.new_page()
        try:
            yield page
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
    finally:
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass
        _pool_sema.release()


# -----------------------------------------------------------------------------
# Request models
# -----------------------------------------------------------------------------


class FetchRequest(BaseModel):
    url: str = Field(..., max_length=MAX_URL_LEN)
    wait_for: str = Field(default="networkidle", pattern=r"^(load|domcontentloaded|networkidle)$")
    wait_for_selector: str | None = None
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, ge=1000, le=MAX_TIMEOUT_MS)
    # True → return text content (stripped HTML); False → full HTML.
    extract_text: bool = True
    # Max characters returned (prevents multi-MB tokens).
    max_chars: int = Field(default=200_000, ge=100, le=2_000_000)
    user_agent: str = DEFAULT_UA

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class ScreenshotRequest(BaseModel):
    url: str = Field(..., max_length=MAX_URL_LEN)
    wait_for: str = Field(default="networkidle", pattern=r"^(load|domcontentloaded|networkidle)$")
    wait_for_selector: str | None = None
    full_page: bool = True
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, ge=1000, le=MAX_TIMEOUT_MS)
    viewport_width: int = Field(default=1440, ge=320, le=3840)
    viewport_height: int = Field(default=900, ge=240, le=2160)
    user_agent: str = DEFAULT_UA

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class PdfRequest(BaseModel):
    url: str = Field(..., max_length=MAX_URL_LEN)
    wait_for: str = Field(default="networkidle", pattern=r"^(load|domcontentloaded|networkidle)$")
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, ge=1000, le=MAX_TIMEOUT_MS)
    format: Literal["A4", "Letter", "Legal"] = "A4"
    user_agent: str = DEFAULT_UA

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class ClickFetchRequest(BaseModel):
    url: str = Field(..., max_length=MAX_URL_LEN)
    click_selector: str
    wait_for: str = Field(default="networkidle", pattern=r"^(load|domcontentloaded|networkidle)$")
    wait_after_click_ms: int = Field(default=2000, ge=0, le=30_000)
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, ge=1000, le=MAX_TIMEOUT_MS)
    extract_text: bool = True
    max_chars: int = Field(default=200_000, ge=100, le=2_000_000)
    user_agent: str = DEFAULT_UA


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


async def _goto_and_settle(page: Page, url: str, wait_for: str, timeout_ms: int) -> dict[str, Any]:
    """Navigate and return the response status / final URL.  Raises HTTPException on failure."""
    try:
        resp = await page.goto(url, wait_until=wait_for, timeout=timeout_ms)
    except PlaywrightTimeout as exc:
        raise HTTPException(504, f"navigation timeout: {url}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"navigation failed: {exc}") from exc
    if resp is None:
        # Same-document navigation or about:blank — fine but odd.
        return {"status": 0, "final_url": page.url}
    return {"status": resp.status, "final_url": page.url}


async def _extract(page: Page, *, as_text: bool, max_chars: int) -> str:
    if as_text:
        text = (await page.inner_text("body")).strip()
    else:
        text = await page.content()
    if len(text) > max_chars:
        return text[: max_chars] + f"\n\n…[truncated at {max_chars} chars]"
    return text


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------


@app.post("/fetch")
async def fetch(req: FetchRequest) -> dict[str, Any]:
    start = time.monotonic()
    async with _acquire_page(user_agent=req.user_agent, timeout_ms=req.timeout_ms) as page:
        nav = await _goto_and_settle(page, req.url, req.wait_for, req.timeout_ms)
        if req.wait_for_selector:
            try:
                await page.wait_for_selector(req.wait_for_selector, timeout=req.timeout_ms)
            except PlaywrightTimeout:
                # not fatal — still return what we have
                pass
        title = await page.title()
        body = await _extract(page, as_text=req.extract_text, max_chars=req.max_chars)
        # Best-effort outlinks (unique)
        try:
            hrefs = await page.eval_on_selector_all(
                "a[href]",
                "els => Array.from(new Set(els.map(e => e.href))).slice(0, 200)",
            )
        except Exception:  # noqa: BLE001
            hrefs = []
        return {
            "status": nav["status"],
            "final_url": nav["final_url"],
            "title": title,
            "body": body,
            "links": hrefs,
            "duration_ms": int((time.monotonic() - start) * 1000),
        }


@app.post("/screenshot")
async def screenshot(req: ScreenshotRequest) -> dict[str, Any]:
    start = time.monotonic()
    async with _acquire_page(
        user_agent=req.user_agent,
        viewport={"width": req.viewport_width, "height": req.viewport_height},
        timeout_ms=req.timeout_ms,
    ) as page:
        await _goto_and_settle(page, req.url, req.wait_for, req.timeout_ms)
        if req.wait_for_selector:
            try:
                await page.wait_for_selector(req.wait_for_selector, timeout=req.timeout_ms)
            except PlaywrightTimeout:
                pass
        png = await page.screenshot(full_page=req.full_page, type="png")
        return {
            "final_url": page.url,
            "title": await page.title(),
            "format": "png",
            "image_b64": base64.b64encode(png).decode("ascii"),
            "size": len(png),
            "duration_ms": int((time.monotonic() - start) * 1000),
        }


@app.post("/pdf")
async def pdf(req: PdfRequest) -> dict[str, Any]:
    start = time.monotonic()
    async with _acquire_page(user_agent=req.user_agent, timeout_ms=req.timeout_ms) as page:
        await _goto_and_settle(page, req.url, req.wait_for, req.timeout_ms)
        data = await page.pdf(format=req.format, print_background=True)
        return {
            "final_url": page.url,
            "title": await page.title(),
            "format": "pdf",
            "pdf_b64": base64.b64encode(data).decode("ascii"),
            "size": len(data),
            "duration_ms": int((time.monotonic() - start) * 1000),
        }


@app.post("/click_and_fetch")
async def click_and_fetch(req: ClickFetchRequest) -> dict[str, Any]:
    start = time.monotonic()
    async with _acquire_page(user_agent=req.user_agent, timeout_ms=req.timeout_ms) as page:
        await _goto_and_settle(page, req.url, req.wait_for, req.timeout_ms)
        try:
            await page.click(req.click_selector, timeout=req.timeout_ms)
        except PlaywrightTimeout as exc:
            raise HTTPException(400, f"click selector not found/clickable: {req.click_selector}") from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"click failed: {exc}") from exc
        if req.wait_after_click_ms:
            try:
                await page.wait_for_load_state("networkidle", timeout=req.timeout_ms)
            except PlaywrightTimeout:
                await page.wait_for_timeout(req.wait_after_click_ms)
        body = await _extract(page, as_text=req.extract_text, max_chars=req.max_chars)
        return {
            "final_url": page.url,
            "title": await page.title(),
            "body": body,
            "duration_ms": int((time.monotonic() - start) * 1000),
        }
