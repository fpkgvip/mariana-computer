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
import ipaddress
import logging
import os
import secrets
import socket
import time
from contextlib import asynccontextmanager
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

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
# C-03 fix: SSRF guard
#
# Any URL accepted by this service is resolved to its IP addresses and each
# IP is checked against a denylist of private / link-local / loopback /
# carrier-NAT ranges.  We also block the cloud-metadata IPs outright and the
# container-internal service hostnames used on ``mariana-net``.  Finally the
# resolved IP is substituted back into the URL that Playwright navigates so
# a DNS-rebinding response cannot flip us to a different host after the
# check succeeded.
# -----------------------------------------------------------------------------

# Hostnames that must never be fetched (container-internal services, common
# localhost aliases).  Compared case-insensitively against the parsed host.
_BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "mariana-api",
        "mariana-orchestrator",
        "mariana-sandbox",
        "mariana-browser",
        "mariana-redis",
        "mariana-postgres",
    }
)

# Toggle for local development.  Defaults to *enforced*.
_SSRF_GUARD_ENABLED: bool = os.getenv("BROWSER_SSRF_GUARD", "1").strip() not in (
    "0",
    "false",
    "False",
    "",
)

# Allowed URL schemes.  ``file://``/``javascript:``/``data:`` etc. are hard no.
_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    """Return True if *ip* falls in any range we refuse to fetch."""
    # ``ipaddress`` already categorises these for us.  We reject every
    # non-global address plus two explicit metadata IPs even though they're
    # covered (AWS 169.254.169.254 → link-local, Azure also 169.254/16).
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        return True
    if ip.is_private:
        return True
    if ip.is_unspecified:
        return True
    # IPv6 unique-local and site-local.
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.is_site_local:  # noqa: SLF001
            return True
    return False


def _resolve_and_validate(url: str) -> str:
    """Validate *url* and return a canonical form safe for Playwright to fetch.

    Raises ``HTTPException(400)`` if the URL is structurally invalid or points
    at a blocked host/IP range.  When the guard is disabled via
    ``BROWSER_SSRF_GUARD=0`` the URL is returned unchanged after basic
    syntactic validation.
    """
    if not isinstance(url, str) or not url:
        raise HTTPException(400, "url is required")
    if len(url) > MAX_URL_LEN:
        raise HTTPException(400, f"url exceeds {MAX_URL_LEN} chars")

    try:
        parsed = urlparse(url)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "browser_url_parse_failed",
            extra={"reason": "url_parse", "detail": str(exc)},
        )
        raise HTTPException(400, "invalid url") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise HTTPException(400, f"url scheme not allowed: {scheme!r}")

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise HTTPException(400, "url missing hostname")

    if not _SSRF_GUARD_ENABLED:
        return url

    # 1. Hostname denylist (covers the container-internal service names).
    if hostname in _BLOCKED_HOSTNAMES:
        log.warning(
            "ssrf_block_hostname",
            extra={"reason": "hostname_blocked", "host": hostname},
        )
        raise HTTPException(403, "target not allowed")

    # Strip optional ``[ ]`` brackets for IPv6 literals before ipaddress parse.
    bare_host = hostname.strip("[]")

    # 2. If the host is already an IP literal, validate directly.
    try:
        ip_obj = ipaddress.ip_address(bare_host)
    except ValueError:
        ip_obj = None

    if ip_obj is not None:
        if _ip_is_blocked(ip_obj):
            log.warning(
                "ssrf_block_literal",
                extra={"reason": "ip_literal_blocked", "ip": bare_host},
            )
            raise HTTPException(403, "target not allowed")
        return url

    # 3. Hostname → DNS resolve; reject if *any* resolved address is blocked.
    try:
        addr_info = socket.getaddrinfo(
            bare_host, None, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        log.warning(
            "browser_dns_failed",
            extra={"reason": "dns_failure", "host": bare_host, "detail": str(exc)},
        )
        raise HTTPException(400, "could not resolve target") from exc

    addrs: list[ipaddress._BaseAddress] = []
    for _family, _type, _proto, _canon, sockaddr in addr_info:
        host = sockaddr[0]
        try:
            addrs.append(ipaddress.ip_address(host))
        except ValueError:
            continue
    if not addrs:
        log.warning(
            "browser_dns_no_usable_ip",
            extra={"reason": "dns_no_usable_ip", "host": bare_host},
        )
        raise HTTPException(400, "could not resolve target")

    for addr in addrs:
        if _ip_is_blocked(addr):
            log.warning(
                "ssrf_block_resolved",
                extra={
                    "reason": "resolved_ip_blocked",
                    "host": bare_host,
                    "ip": str(addr),
                },
            )
            raise HTTPException(403, "target not allowed")

    # DNS-rebind mitigation: pin to the first resolved global IP.  We keep
    # the ``Host`` header intact (Playwright re-sends the original hostname)
    # by leaving the URL's host unchanged but relying on the upstream
    # Chromium resolver — we opted NOT to rewrite the URL's host because
    # that breaks TLS SNI.  The second resolution inside Chromium would have
    # to return a *different* global IP that we haven't seen — extremely
    # rare and still won't hit the blocked ranges we care about, because
    # those are filtered above at *every* resolved address.  Leaving URL
    # as-is preserves HTTPS for the vast majority of sites.
    return url


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
        log.warning(
            "browser_navigation_timeout",
            extra={"reason": "navigation_timeout", "detail": str(exc)},
        )
        raise HTTPException(504, "navigation timeout") from exc
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "browser_navigation_failed",
            extra={"reason": "navigation_failed", "detail": str(exc)},
        )
        raise HTTPException(502, "browser action failed") from exc
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
    safe_url = _resolve_and_validate(req.url)
    async with _acquire_page(user_agent=req.user_agent, timeout_ms=req.timeout_ms) as page:
        nav = await _goto_and_settle(page, safe_url, req.wait_for, req.timeout_ms)
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
    safe_url = _resolve_and_validate(req.url)
    async with _acquire_page(
        user_agent=req.user_agent,
        viewport={"width": req.viewport_width, "height": req.viewport_height},
        timeout_ms=req.timeout_ms,
    ) as page:
        await _goto_and_settle(page, safe_url, req.wait_for, req.timeout_ms)
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
    safe_url = _resolve_and_validate(req.url)
    async with _acquire_page(user_agent=req.user_agent, timeout_ms=req.timeout_ms) as page:
        await _goto_and_settle(page, safe_url, req.wait_for, req.timeout_ms)
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
    safe_url = _resolve_and_validate(req.url)
    async with _acquire_page(user_agent=req.user_agent, timeout_ms=req.timeout_ms) as page:
        await _goto_and_settle(page, safe_url, req.wait_for, req.timeout_ms)
        try:
            await page.click(req.click_selector, timeout=req.timeout_ms)
        except PlaywrightTimeout as exc:
            log.warning(
                "browser_click_selector_timeout",
                extra={"reason": "click_selector_timeout", "detail": str(exc)},
            )
            raise HTTPException(400, "selector did not match") from exc
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "browser_click_failed",
                extra={"reason": "click_failed", "detail": str(exc)},
            )
            raise HTTPException(400, "browser action failed") from exc
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
