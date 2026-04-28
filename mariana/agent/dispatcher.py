"""Tool dispatcher — maps agent tool names to real async callables.

The agent LLM emits step plans like ``{"tool": "code_exec", "params": {...}}``.
This module validates the params and calls the right backend.  Errors are
returned as structured dicts the loop can feed back into the fix prompt.
"""

from __future__ import annotations

import base64
import hashlib
import os
import posixpath
import re
import uuid
from typing import Any

import httpx
import structlog

from mariana.agent import tools
from mariana.agent.models import TOOL_NAMES
from mariana.vault.runtime import get_task_env

logger = structlog.get_logger(__name__)


class ToolError(RuntimeError):
    """Raised when a tool call fails.  Includes a structured diagnostic.

    The ``message`` and ``detail`` attributes are **server-log-only** as of
    CC-25.  The agent loop catches this exception and persists / emits a
    stable ``tool_error`` code on the user-visible step record; only the
    structured server log keeps the raw message + detail.  Construct messages
    with as much diagnostic context as you need (workspace paths, file lists,
    upstream response bodies) — they will not leak to the API surface.
    """

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


def _artifact_record(path: str, payload: bytes | str) -> dict[str, Any]:
    """Build an artifact dict for the agent loop to register.

    The loop appends any entry in result['artifacts'] to ``task.artifacts``,
    which is what ``/api/agent/{id}/artifacts`` surfaces and what the frontend
    shows in the artifact gallery.
    """
    data = payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode("utf-8")
    return {
        "name": posixpath.basename(path.strip("/\\")) or path,
        "workspace_path": path,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


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
        # CC-25: ToolError message + detail are server-log-only.  The agent
        # loop persists/emits only a stable ``tool_error`` code; never assume
        # this message string is rendered to the end user.
        raise ToolError(
            tool,
            f"{tool} failed: {exc}",
            detail={"status": getattr(exc, "status", None), "body": getattr(exc, "body", None)},
        ) from exc
    except httpx.HTTPError as exc:
        raise ToolError(tool, f"{tool} transport error: {exc}") from exc


# ----- individual tool handlers --------------------------------------------


async def _h_code_exec(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    # Vault injection: merge per-task secrets BEHIND any explicit env supplied
    # by the LLM plan.  Plan values win on conflict so the LLM can shadow a
    # vault var if it really needs to (rare; typically used for testing).
    plan_env = _opt(p, "env", {}, dict)
    vault_env = get_task_env()
    if vault_env:
        merged: dict[str, Any] = dict(vault_env)
        merged.update(plan_env or {})
        env_for_exec = merged
    else:
        env_for_exec = plan_env or {}
    return await tools.exec_code(
        user_id=user_id,
        code=_require(p, "code", str),
        language=_opt(p, "language", "python", str) or "python",
        stdin=_opt(p, "stdin", "", str),
        cwd=_opt(p, "cwd", "", str),
        wall_timeout_sec=int(_opt(p, "wall_timeout_sec", 60, (int, float))),
        mem_mb=int(_opt(p, "mem_mb", 1024, (int, float))),
        cpu_sec=int(_opt(p, "cpu_sec", 60, (int, float))),
        env=env_for_exec,
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
    path = _require(p, "path", str)
    content = _require(p, "content", str)
    binary = bool(_opt(p, "binary", False, bool))
    result = await tools.fs_write(
        user_id=user_id,
        path=path,
        content=content,
        binary=binary,
        overwrite=bool(_opt(p, "overwrite", True, bool)),
    )
    # v3.6: surface every fs_write as an artifact so the UI gallery + the
    # /api/agent/{id}/artifacts endpoint see the file.  Binary writes send
    # base64 — decode for an accurate size/hash.
    try:
        if binary:
            payload = base64.b64decode(content)
        else:
            payload = content.encode("utf-8")
        result = dict(result) if isinstance(result, dict) else {}
        result.setdefault("artifacts", []).append(_artifact_record(path, payload))
    except Exception as exc:  # noqa: BLE001
        logger.warning("fs_write_artifact_register_failed", path=path, error=str(exc))
    return result


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
    result = await tools.browser_fetch(
        url=_require(p, "url", str),
        wait_for=_opt(p, "wait_for", "networkidle", str),
        wait_for_selector=_opt(p, "wait_for_selector", None, str),
        timeout_ms=int(_opt(p, "timeout_ms", 30_000, (int, float))),
        extract_text=bool(_opt(p, "extract_text", True, bool)),
        max_chars=int(_opt(p, "max_chars", 200_000, (int, float))),
    )
    # v3 injection defence: every fetched body is UNTRUSTED data.  Mark it
    # explicitly so the LLM's fix / reasoning loop is re-reminded when it
    # looks at the tool result.  Also scan for obvious injection giveaways
    # and log them without altering the body.
    if isinstance(result, dict) and "body" in result and isinstance(result["body"], str):
        body = result["body"]
        result["__untrusted"] = True
        hits = _injection_signals(body)
        if hits:
            result["__suspected_injection_signals"] = hits
            logger.warning(
                "browser_fetch_injection_signals",
                task_id=task_id,
                url=result.get("final_url") or result.get("url"),
                signals=hits[:5],
            )
    return result


_INJECTION_PATTERNS = (
    # Permissive: allow 0-3 filler words between "ignore" and the noun
    re.compile(r"ignore\s+(?:\w+\s+){0,3}(instructions|prompt|rules|system|directives)", re.I),
    re.compile(r"disregard\s+(?:\w+\s+){0,3}(instructions|prompt|rules|system|directives)", re.I),
    re.compile(r"forget\s+(?:\w+\s+){0,3}(instructions|prompt|rules|system|directives)", re.I),
    re.compile(r"override\s+(?:\w+\s+){0,3}(instructions|prompt|rules|system|directives)", re.I),
    re.compile(r"you are now\b", re.I),
    re.compile(r"new\s+(system\s+)?(prompt|instructions|role|persona)\s*:", re.I),
    re.compile(r"^\s*system\s*:\s*", re.I | re.M),
    re.compile(r"send\s+(?:the|your|me|us)?\s*(?:\w+\s+){0,3}(api[_\s\-]?key|token|secret|password|credentials|bearer)", re.I),
    re.compile(r"exfiltrate|leak\s+(?:the|your)?\s*(prompt|system|secret|key|token)", re.I),
    re.compile(r"curl\s+[^\s]+\s+--data\s+.+api[_\-]?key", re.I),
    re.compile(r"</?(system|user|assistant)>", re.I),
    re.compile(r"\[\s*(INST|/INST|SYSTEM|USER|ASSISTANT)\s*\]", re.I),
)


def _injection_signals(text: str) -> list[str]:
    """Return a list of injection-pattern hits found in the text (up to 10)."""
    out: list[str] = []
    snippet = text[: 50_000]  # cap pattern scan work
    for pat in _INJECTION_PATTERNS:
        m = pat.search(snippet)
        if m:
            out.append(m.group(0)[:120])
            if len(out) >= 10:
                break
    return out


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
    artifacts: list[dict[str, Any]] = []
    if save_path:
        png_b64 = result["image_b64"]
        await tools.fs_write(
            user_id=user_id,
            path=save_path,
            content=png_b64,
            binary=True,
            overwrite=True,
        )
        result["saved_to"] = save_path
        try:
            artifacts.append(_artifact_record(save_path, base64.b64decode(png_b64)))
        except Exception:
            pass
    # Drop the b64 payload from the returned dict so it doesn't pollute the LLM
    # context — the image is accessible via the workspace path.
    summary = {k: v for k, v in result.items() if k != "image_b64"}
    if artifacts:
        summary["artifacts"] = artifacts
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
    artifacts: list[dict[str, Any]] = []
    if save_path:
        pdf_b64 = result["pdf_b64"]
        await tools.fs_write(
            user_id=user_id,
            path=save_path,
            content=pdf_b64,
            binary=True,
            overwrite=True,
        )
        result["saved_to"] = save_path
        try:
            artifacts.append(_artifact_record(save_path, base64.b64decode(pdf_b64)))
        except Exception:
            pass
    out = {k: v for k, v in result.items() if k != "pdf_b64"}
    if artifacts:
        out["artifacts"] = artifacts
    return out


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
# Media generation (v3) — image + video gen.
#
# Both tools run in the orchestrator process: the provider APIs need network
# egress and api keys which are intentionally NOT exposed to the user sandbox.
# We fetch the bytes server-side, then persist into the user's sandbox
# workspace via the sandbox fs_write endpoint (same pattern as
# browser_screenshot).  Resulting file becomes an agent artifact automatically
# by virtue of being under /workspace/<user_id>/.
# ---------------------------------------------------------------------------


async def _h_generate_image(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    from pathlib import Path as _P
    from mariana.tools.image_gen import generate_image

    prompt = _require(p, "prompt", str)
    size = _opt(p, "size", "1024x1024", str)
    save_to = _require(p, "save_to", str)
    # C-02 fix: image generation routes through LLM Gateway, using the same
    # LLM_GATEWAY_API_KEY that powers inference.  NANOBANANA_API_KEY retired.
    api_key = os.getenv("LLM_GATEWAY_API_KEY") or ""
    if not api_key:
        raise ToolError("generate_image", "LLM_GATEWAY_API_KEY not configured")

    # Write to a /tmp holding file then read back as bytes.
    tmp = _P(f"/tmp/agent_img_{task_id}_{uuid.uuid4().hex[:8]}.png")
    try:
        await generate_image(
            prompt=prompt,
            api_key=api_key,
            output_path=tmp,
            size=size,
            timeout=180.0,
            data_root=None,
        )
        data = tmp.read_bytes()
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass

    await tools.fs_write(
        user_id=user_id,
        path=save_to,
        content=base64.b64encode(data).decode("ascii"),
        binary=True,
        overwrite=True,
    )
    return {
        "saved_to": save_to,
        "prompt": prompt[:200],
        "size": size,
        "bytes": len(data),
        "artifacts": [_artifact_record(save_to, data)],
    }


async def _h_generate_video(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    from pathlib import Path as _P
    from mariana.tools.video_gen import generate_video

    prompt = _require(p, "prompt", str)
    duration = int(_opt(p, "duration_seconds", 10, (int, float)))
    duration = max(1, min(duration, 60))  # clamp 1..60s; further snapped to {4,6,8,10} in tool
    save_to = _require(p, "save_to", str)
    # C-01 fix: video generation routes through LLM Gateway long-running
    # videos API (POST /videos → poll → GET /videos/{id}/content).
    # VEO_API_KEY retired; we reuse LLM_GATEWAY_API_KEY.
    api_key = os.getenv("LLM_GATEWAY_API_KEY") or ""
    if not api_key:
        raise ToolError("generate_video", "LLM_GATEWAY_API_KEY not configured")

    tmp = _P(f"/tmp/agent_vid_{task_id}_{uuid.uuid4().hex[:8]}.mp4")
    try:
        await generate_video(
            prompt=prompt,
            api_key=api_key,
            output_path=tmp,
            duration_seconds=duration,
            timeout=900.0,
            data_root=None,
        )
        data = tmp.read_bytes()
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass

    await tools.fs_write(
        user_id=user_id,
        path=save_to,
        content=base64.b64encode(data).decode("ascii"),
        binary=True,
        overwrite=True,
    )
    return {
        "saved_to": save_to,
        "prompt": prompt[:200],
        "duration_seconds": duration,
        "bytes": len(data),
        "artifacts": [_artifact_record(save_to, data)],
    }


async def _h_describe_self(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    """Return a compact self-description so the LLM can answer "what can you do?".

    Useful when a user asks about the agent's capabilities mid-task.  Keeps the
    facts in one place so prompt + user-facing answers stay in sync.
    """
    from mariana.agent.self_knowledge import describe_self_payload
    return describe_self_payload()


# ---------------------------------------------------------------------------
# Deft v2 — deploy_preview
# ---------------------------------------------------------------------------

_PREVIEW_ROOT = os.environ.get("DEFT_PREVIEW_ROOT", "/var/lib/deft/preview")
_PREVIEW_PUBLIC_BASE = os.environ.get("DEFT_PREVIEW_PUBLIC_BASE", "")  # e.g. https://api.deft.ai
_PREVIEW_MAX_FILES = 2000
_PREVIEW_MAX_TOTAL = 100 * 1024 * 1024  # 100 MB / preview


def _preview_dir(task_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_\-]", "", task_id)[:64] or "task"
    return os.path.join(_PREVIEW_ROOT, safe)


async def _h_deploy_preview(p: dict[str, Any], *, user_id: str, task_id: str) -> dict[str, Any]:
    """Snapshot a built directory in the sandbox workspace to a local public dir.

    Files are read via the sandbox `/fs/read` endpoint (binary mode), then
    written to ``$DEFT_PREVIEW_ROOT/<task_id>/`` which the FastAPI app mounts
    at ``/preview/<task_id>/`` for the frontend iframe.  Returns the public
    URL pointing at the entry file (default ``index.html``).
    """
    src_dir = _require(p, "source_dir", str).strip().lstrip("/")
    if not src_dir or src_dir in (".", "./"):
        raise ToolError("", "source_dir must be a non-empty workspace path")
    entry = _opt(p, "entry", "index.html", str) or "index.html"
    label = _opt(p, "label", "", str) or ""

    # 1) walk the directory in the sandbox
    walk = await tools.fs_walk(user_id=user_id, path=src_dir, max_entries=_PREVIEW_MAX_FILES + 1)
    if not walk:
        raise ToolError("", f"source_dir {src_dir!r} is empty or missing")
    if len(walk) > _PREVIEW_MAX_FILES:
        raise ToolError("", f"too many files ({len(walk)}); cap is {_PREVIEW_MAX_FILES}")

    # 2) prepare the local public dir; nuke any prior preview for this task.
    target_root = _preview_dir(task_id)
    try:
        if os.path.isdir(target_root):
            import shutil
            shutil.rmtree(target_root, ignore_errors=True)
        os.makedirs(target_root, exist_ok=True)
    except OSError as exc:
        raise ToolError("", f"could not initialise preview dir: {exc}") from exc

    # 3) verify the entry file exists in the walk.
    rel_paths = [e["path"] for e in walk]
    src_prefix = src_dir.rstrip("/") + "/"
    src_relative = [p[len(src_prefix):] if p.startswith(src_prefix) else p for p in rel_paths]
    if entry not in src_relative:
        raise ToolError(
            "",
            f"entry {entry!r} not found in {src_dir!r}. Available: {src_relative[:10]}{'...' if len(src_relative) > 10 else ''}",
        )

    # 4) copy each file via fs_read in binary mode.  Cap total bytes.
    total_bytes = 0
    files_written = 0
    for entry_meta in walk:
        sb_path = entry_meta["path"]
        rel = sb_path[len(src_prefix):] if sb_path.startswith(src_prefix) else sb_path
        if rel.startswith(".."):
            continue  # paranoia
        local_path = os.path.join(target_root, rel)
        os.makedirs(os.path.dirname(local_path) or target_root, exist_ok=True)
        sz = int(entry_meta.get("size", 0))
        if total_bytes + sz > _PREVIEW_MAX_TOTAL:
            raise ToolError("", f"preview exceeds {_PREVIEW_MAX_TOTAL // (1024*1024)} MB cap")
        try:
            file_blob = await tools.fs_read(
                user_id=user_id, path=sb_path, binary=True, max_bytes=max(sz + 16, 65536)
            )
        except tools.SandboxError as exc:
            raise ToolError("", f"failed to read {sb_path}: {exc}") from exc
        b64 = file_blob.get("content_b64")
        if b64 is None:
            txt = file_blob.get("content", "")
            data = txt.encode("utf-8")
        else:
            data = base64.b64decode(b64)
        with open(local_path, "wb") as fh:
            fh.write(data)
        total_bytes += len(data)
        files_written += 1

    # 5) write a tiny manifest for the API.
    manifest = {
        "task_id": task_id,
        "user_id": user_id,
        "entry": entry,
        "label": label[:120],
        "source_dir": src_dir,
        "files": files_written,
        "total_bytes": total_bytes,
        "created_at": __import__("time").time(),
    }
    with open(os.path.join(target_root, "_deft_manifest.json"), "w", encoding="utf-8") as fh:
        import json as _json
        _json.dump(manifest, fh)

    # 6) build the public URL.  If a base URL is configured, return absolute;
    # otherwise return a path the frontend will resolve against the api host.
    base = _PREVIEW_PUBLIC_BASE.rstrip("/")
    rel_url = f"/preview/{os.path.basename(target_root)}/{entry}"
    public_url = f"{base}{rel_url}" if base else rel_url
    logger.info(
        "deploy_preview",
        task_id=task_id,
        files=files_written,
        total_bytes=total_bytes,
        url=public_url,
    )
    return {
        "url": public_url,
        "entry": entry,
        "files": files_written,
        "total_bytes": total_bytes,
        "label": manifest["label"],
    }


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
    # v3 additions — additive only, existing tools unchanged.
    "generate_image": _h_generate_image,
    "generate_video": _h_generate_video,
    "describe_self": _h_describe_self,
    # Deft v2.
    "deploy_preview": _h_deploy_preview,
}

VALID_TOOLS: frozenset[str] = frozenset(_DISPATCH_TABLE.keys())


def is_valid_tool(name: str) -> bool:
    return name in VALID_TOOLS
