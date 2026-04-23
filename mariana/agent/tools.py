"""HTTP clients for the Mariana sandbox and browser services.

These are async, use a shared ``httpx.AsyncClient`` per-process, and return
plain Python dicts.  Errors from the remote service are surfaced as
:class:`SandboxError` / :class:`BrowserError` with enough context for the
agent loop to feed back into its self-correction prompt.
"""

from __future__ import annotations

import base64
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

import httpx
import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration (read from env at call time, not import time, so tests and
# the ``deploy.sh`` flow can override without restarts in dev).
# ---------------------------------------------------------------------------


def _sandbox_base() -> str:
    return os.getenv("SANDBOX_BASE_URL", "http://mariana-sandbox:8000").rstrip("/")


def _browser_base() -> str:
    return os.getenv("BROWSER_BASE_URL", "http://mariana-browser:8000").rstrip("/")


def _shared_secret() -> str:
    # Same secret is used for sandbox and browser so the orchestrator only
    # needs one env var.
    v = os.getenv("SANDBOX_SHARED_SECRET", "")
    if not v:
        raise RuntimeError("SANDBOX_SHARED_SECRET is not set")
    return v


# ---------------------------------------------------------------------------
# HTTP client (module-level singleton, lazy)
# ---------------------------------------------------------------------------

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0, read=1800.0, write=60.0, pool=10.0
            ),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            headers={"user-agent": "mariana-orchestrator/1.0"},
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class SandboxError(RuntimeError):
    """Raised for any non-success response from the sandbox service."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class BrowserError(RuntimeError):
    """Raised for any non-success response from the browser service."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


# ---------------------------------------------------------------------------
# Sandbox helpers
# ---------------------------------------------------------------------------


Language = Literal["python", "bash", "typescript", "javascript", "rust"]


async def sandbox_health() -> dict[str, Any]:
    r = await _get_client().get(f"{_sandbox_base()}/health")
    r.raise_for_status()
    return r.json()


async def exec_code(
    *,
    user_id: str,
    code: str,
    language: Language = "python",
    stdin: str = "",
    cwd: str = "",
    wall_timeout_sec: int = 60,
    mem_mb: int = 1024,
    cpu_sec: int = 60,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute code in the sandbox.  Returns the raw response dict.

    Schema::

        {
          "stdout": str,
          "stderr": str,
          "exit_code": int,
          "duration_ms": int,
          "timed_out": bool,
          "killed": bool,
          "stdout_truncated": bool,
          "stderr_truncated": bool,
          "artifacts": [{"name": str, "workspace_path": str, "size": int, "sha256": str}, ...]
        }
    """
    payload = {
        "user_id": user_id,
        "language": language,
        "code": code,
        "stdin": stdin,
        "cwd": cwd,
        "wall_timeout_sec": wall_timeout_sec,
        "mem_mb": mem_mb,
        "cpu_sec": cpu_sec,
        "env": env or {},
    }
    return await _sandbox_post("/exec", payload, read_timeout=wall_timeout_sec + 30)


async def fs_read(
    *,
    user_id: str,
    path: str,
    binary: bool = False,
    max_bytes: int = 1_048_576,
) -> dict[str, Any]:
    return await _sandbox_post(
        "/fs/read",
        {"user_id": user_id, "path": path, "binary": binary, "max_bytes": max_bytes},
    )


async def fs_write(
    *,
    user_id: str,
    path: str,
    content: str | bytes,
    binary: bool = False,
    overwrite: bool = True,
) -> dict[str, Any]:
    if isinstance(content, bytes) or binary:
        body_content = (
            base64.b64encode(content).decode("ascii")
            if isinstance(content, bytes)
            else content
        )
        binary = True
    else:
        body_content = content
    return await _sandbox_post(
        "/fs/write",
        {
            "user_id": user_id,
            "path": path,
            "content": body_content,
            "binary": binary,
            "overwrite": overwrite,
        },
    )


async def fs_list(
    *,
    user_id: str,
    path: str = "",
    recursive: bool = True,
    max_entries: int = 1000,
) -> dict[str, Any]:
    return await _sandbox_post(
        "/fs/list",
        {"user_id": user_id, "path": path, "recursive": recursive, "max_entries": max_entries},
    )


async def fs_delete(*, user_id: str, path: str) -> dict[str, Any]:
    return await _sandbox_post("/fs/delete", {"user_id": user_id, "path": path})


async def _sandbox_post(path: str, payload: dict, *, read_timeout: float | None = None) -> dict[str, Any]:
    url = f"{_sandbox_base()}{path}"
    client = _get_client()
    timeout = (
        httpx.Timeout(connect=10.0, read=read_timeout, write=60.0, pool=10.0)
        if read_timeout is not None
        else None
    )
    try:
        r = await client.post(
            url,
            json=payload,
            headers={"x-sandbox-secret": _shared_secret()},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise SandboxError(f"sandbox transport error calling {path}: {exc}") from exc
    if r.status_code >= 400:
        try:
            body = r.json()
        except ValueError:
            body = {"detail": r.text}
        raise SandboxError(
            f"sandbox {path} -> {r.status_code}: {body.get('detail') if isinstance(body, dict) else body}",
            status=r.status_code,
            body=body,
        )
    return r.json()


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------


async def browser_health() -> dict[str, Any]:
    r = await _get_client().get(f"{_browser_base()}/health")
    r.raise_for_status()
    return r.json()


async def browser_fetch(
    *,
    url: str,
    wait_for: str = "networkidle",
    wait_for_selector: str | None = None,
    timeout_ms: int = 30_000,
    extract_text: bool = True,
    max_chars: int = 200_000,
) -> dict[str, Any]:
    return await _browser_post(
        "/fetch",
        {
            "url": url,
            "wait_for": wait_for,
            "wait_for_selector": wait_for_selector,
            "timeout_ms": timeout_ms,
            "extract_text": extract_text,
            "max_chars": max_chars,
        },
        read_timeout=timeout_ms / 1000 + 30,
    )


async def browser_screenshot(
    *,
    url: str,
    wait_for: str = "networkidle",
    wait_for_selector: str | None = None,
    full_page: bool = True,
    timeout_ms: int = 30_000,
    viewport_width: int = 1440,
    viewport_height: int = 900,
) -> dict[str, Any]:
    """Return {..., image_b64: str, size: int}."""
    return await _browser_post(
        "/screenshot",
        {
            "url": url,
            "wait_for": wait_for,
            "wait_for_selector": wait_for_selector,
            "full_page": full_page,
            "timeout_ms": timeout_ms,
            "viewport_width": viewport_width,
            "viewport_height": viewport_height,
        },
        read_timeout=timeout_ms / 1000 + 30,
    )


async def browser_pdf(
    *,
    url: str,
    wait_for: str = "networkidle",
    timeout_ms: int = 30_000,
    fmt: Literal["A4", "Letter", "Legal"] = "A4",
) -> dict[str, Any]:
    return await _browser_post(
        "/pdf",
        {"url": url, "wait_for": wait_for, "timeout_ms": timeout_ms, "format": fmt},
        read_timeout=timeout_ms / 1000 + 30,
    )


async def browser_click_fetch(
    *,
    url: str,
    click_selector: str,
    wait_for: str = "networkidle",
    wait_after_click_ms: int = 2000,
    timeout_ms: int = 30_000,
    extract_text: bool = True,
    max_chars: int = 200_000,
) -> dict[str, Any]:
    return await _browser_post(
        "/click_and_fetch",
        {
            "url": url,
            "click_selector": click_selector,
            "wait_for": wait_for,
            "wait_after_click_ms": wait_after_click_ms,
            "timeout_ms": timeout_ms,
            "extract_text": extract_text,
            "max_chars": max_chars,
        },
        read_timeout=timeout_ms / 1000 + 30,
    )


async def _browser_post(path: str, payload: dict, *, read_timeout: float | None = None) -> dict[str, Any]:
    url = f"{_browser_base()}{path}"
    client = _get_client()
    timeout = (
        httpx.Timeout(connect=10.0, read=read_timeout, write=60.0, pool=10.0)
        if read_timeout is not None
        else None
    )
    try:
        r = await client.post(
            url,
            json=payload,
            headers={"x-sandbox-secret": _shared_secret()},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise BrowserError(f"browser transport error calling {path}: {exc}") from exc
    if r.status_code >= 400:
        try:
            body = r.json()
        except ValueError:
            body = {"detail": r.text}
        raise BrowserError(
            f"browser {path} -> {r.status_code}: {body.get('detail') if isinstance(body, dict) else body}",
            status=r.status_code,
            body=body,
        )
    return r.json()
