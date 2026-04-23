"""Tool dispatcher — maps agent tool names to real async callables.

The agent LLM emits step plans like ``{"tool": "code_exec", "params": {...}}``.
This module validates the params and calls the right backend.  Errors are
returned as structured dicts the loop can feed back into the fix prompt.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import structlog

from mariana.agent import tools
from mariana.agent.models import TOOL_NAMES

logger = structlog.get_logger(__name__)


class ToolError(RuntimeError):
    """Raised when a tool call fails.  Includes a structured diagnostic."""

    def __init__(self, tool: str, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.tool = tool
        self.detail = detail


# ---------------------------------------------------------------------------
# Per-tool wrappers
# ---------------------------------------------------------------------------


def _require(params: dict[str, Any], key: str, typ: type | tuple[type, ...]) -> Any:
    if key not in params:
        raise ToolError("", f"missing required param: {key}")
    val = params[key]
    if not isinstance(val, typ):
        raise ToolError("", f"param {key!r} has wrong type: expected {typ}, got {type(val).__name__}")
    return val


def _opt(params: dict[str, Any], key: str, default: Any, typ: type | tuple[type, ...]) -> Any:
    val = params.get(key, default)
    if val is None:
        return default
    if not isinstance(val, typ):
        raise ToolError("", f"param {key!r} has wrong type: expected {typ}, got {type(val).__name__}")
    return val


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def dispatch(
    tool: str,
    params: dict[str, Any],
    *,
    user_id: str,
    task_id: str,
) -> dict[str, Any]:
    """Execute a single tool call.  Returns the tool's result dict.

    On validation error, raises :class:`ToolError` with a diagnostic that the
    caller can feed into the fix-loop prompt.
    """
    try:
        return await _DISPATCH_TABLE[tool](params, user_id=user_id, task_id=task_id)
    except KeyError:
        raise ToolError(tool, f"unknown tool: {tool!r}") from None
    except ToolError as exc:
        exc.tool = tool
        raise
    except (tools.SandboxError, tools.BrowserError) as exc:
        # Surface a structured error from the remote service.
        raise ToolError(
            tool,
            f"{tool} failed: {exc}",
            detail={"status": getattr(exc, "status", None), "body": getattr(exc, "body", None)},
        ) from exc
    except httpx.HTTPError as exc:
        raise ToolError(tool, f"{tool} transport error: {exc}") from exc


# ----- individual tool handlers --------------------------------------------


async def _h_code_exec(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    return await tools.exec_code(
        user_id=user_id,
        code=_require(p, "code", str),
        language=_opt(p, "language", "python", str) or "python",
        stdin=_opt(p, "stdin", "", str),
        cwd=_opt(p, "cwd", "", str),
        wall_timeout_sec=int(_opt(p, "wall_timeout_sec", 60, (int, float))),
        mem_mb=int(_opt(p, "mem_mb", 1024, (int, float))),
        cpu_sec=int(_opt(p, "cpu_sec", 60, (int, float))),
        env=_opt(p, "env", {}, dict),
    )


async def _h_bash_exec(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    # Convenience wrapper so the LLM can pick a shorter tool name.
    p2 = dict(p)
    p2["language"] = "bash"
    return await _h_code_exec(p2, user_id=user_id, task_id=task_id)


async def _h_typescript_exec(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    p2 = dict(p)
    p2["language"] = "typescript"
    return await _h_code_exec(p2, user_id=user_id, task_id=task_id)


async def _h_rust_exec(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    p2 = dict(p)
    p2["language"] = "rust"
    return await _h_code_exec(p2, user_id=user_id, task_id=task_id)


async def _h_fs_read(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    return await tools.fs_read(
        user_id=user_id,
        path=_require(p, "path", str),
        binary=bool(_opt(p, "binary", False, bool)),
        max_bytes=int(_opt(p, "max_bytes", 1_048_576, (int, float))),
    )


async def _h_fs_write(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    return await tools.fs_write(
        user_id=user_id,
        path=_require(p, "path", str),
        content=_require(p, "content", str),
        binary=bool(_opt(p, "binary", False, bool)),
        overwrite=bool(_opt(p, "overwrite", True, bool)),
    )


async def _h_fs_list(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    return await tools.fs_list(
        user_id=user_id,
        path=_opt(p, "path", "", str),
        recursive=bool(_opt(p, "recursive", True, bool)),
        max_entries=int(_opt(p, "max_entries", 1000, (int, float))),
    )


async def _h_fs_delete(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    return await tools.fs_delete(
        user_id=user_id,
        path=_require(p, "path", str),
    )


async def _h_browser_fetch(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    return await tools.browser_fetch(
        url=_require(p, "url", str),
        wait_for=_opt(p, "wait_for", "networkidle", str),
        wait_for_selector=_opt(p, "wait_for_selector", None, str),
        timeout_ms=int(_opt(p, "timeout_ms", 30_000, (int, float))),
        extract_text=bool(_opt(p, "extract_text", True, bool)),
        max_chars=int(_opt(p, "max_chars", 200_000, (int, float))),
    )


async def _h_browser_screenshot(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    result = await tools.browser_screenshot(
        url=_require(p, "url", str),
        wait_for=_opt(p, "wait_for", "networkidle", str),
        wait_for_selector=_opt(p, "wait_for_selector", None, str),
        full_page=bool(_opt(p, "full_page", True, bool)),
        timeout_ms=int(_opt(p, "timeout_ms", 30_000, (int, float))),
        viewport_width=int(_opt(p, "viewport_width", 1440, (int, float))),
        viewport_height=int(_opt(p, "viewport_height", 900, (int, float))),
    )
    # Persist the PNG into the user workspace so it shows up as an artifact.
    save_path = _opt(p, "save_to", "", str)
    if save_path:
        await tools.fs_write(
            user_id=user_id,
            path=save_path,
            content=result["image_b64"],
            binary=True,
            overwrite=True,
        )
        result["saved_to"] = save_path
    # Drop the b64 payload from the returned dict so it doesn't pollute the LLM
    # context — the image is accessible via the workspace path.
    summary = {k: v for k, v in result.items() if k != "image_b64"}
    return summary


async def _h_browser_pdf(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    fmt = _opt(p, "format", "A4", str)
    if fmt not in ("A4", "Letter", "Legal"):
        raise ToolError("", f"invalid pdf format: {fmt!r}")
    result = await tools.browser_pdf(
        url=_require(p, "url", str),
        wait_for=_opt(p, "wait_for", "networkidle", str),
        timeout_ms=int(_opt(p, "timeout_ms", 30_000, (int, float))),
        fmt=fmt,
    )
    save_path = _opt(p, "save_to", "", str)
    if save_path:
        await tools.fs_write(
            user_id=user_id,
            path=save_path,
            content=result["pdf_b64"],
            binary=True,
            overwrite=True,
        )
        result["saved_to"] = save_path
    return {k: v for k, v in result.items() if k != "pdf_b64"}


async def _h_browser_click_fetch(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    return await tools.browser_click_fetch(
        url=_require(p, "url", str),
        click_selector=_require(p, "click_selector", str),
        wait_for=_opt(p, "wait_for", "networkidle", str),
        wait_after_click_ms=int(_opt(p, "wait_after_click_ms", 2000, (int, float))),
        timeout_ms=int(_opt(p, "timeout_ms", 30_000, (int, float))),
        extract_text=bool(_opt(p, "extract_text", True, bool)),
        max_chars=int(_opt(p, "max_chars", 200_000, (int, float))),
    )


async def _h_web_search(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    """Web search via Perplexity Sonar (reuses existing tool)."""
    # Import lazily so orchestrator start-up doesn't depend on Perplexity config.
    import os as _os
    from mariana.tools.perplexity_search import search_perplexity

    api_key = _os.getenv("PERPLEXITY_API_KEY", "")
    if not api_key:
        raise ToolError("web_search", "PERPLEXITY_API_KEY not set")
    query = _require(p, "query", str)
    result = await search_perplexity(query=query, api_key=api_key)
    return {
        "query": result.query,
        "answer": result.answer,
        "citations": result.citations,
    }


async def _h_think(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    """No-op tool: lets the LLM insert an explicit reasoning step in the plan.

    The `thought` field is shown in the UI as a "thinking" event; no sandbox
    call is made.
    """
    thought = _require(p, "thought", str)
    return {"thought": thought[:8000]}


async def _h_deliver(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    """Marks the task as ready for delivery.  The loop handles the final render."""
    return {"final_answer": _opt(p, "final_answer", "", str)}


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_DISPATCH_TABLE: dict[str, Any] = {
    "code_exec": _h_code_exec,
    "bash_exec": _h_bash_exec,
    "typescript_exec": _h_typescript_exec,
    "rust_exec": _h_rust_exec,
    "fs_read": _h_fs_read,
    "fs_write": _h_fs_write,
    "fs_list": _h_fs_list,
    "fs_delete": _h_fs_delete,
    "browser_fetch": _h_browser_fetch,
    "browser_screenshot": _h_browser_screenshot,
    "browser_pdf": _h_browser_pdf,
    "browser_click_fetch": _h_browser_click_fetch,
    "web_search": _h_web_search,
    "think": _h_think,
    "deliver": _h_deliver,
}

VALID_TOOLS: frozenset[str] = frozenset(_DISPATCH_TABLE.keys())


def is_valid_tool(name: str) -> bool:
    return name in VALID_TOOLS
