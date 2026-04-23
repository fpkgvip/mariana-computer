"""Agent planner — converts a user goal into a structured step list.

The planner makes one LLM call to turn the user's goal + instructions into a
`plan` (list of tool invocations) that the event loop then executes.  The
result is deterministic-shape JSON validated against :class:`AgentStep`.

Later in the loop, the same function is reused for REPLAN (with additional
context from the failed attempts) and for FIX (single-step replacement).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

import httpx
import structlog

from mariana.agent.dispatcher import VALID_TOOLS
from mariana.agent.models import AgentStep, AgentTask, StepStatus

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

# User-selected agent models.  The planner always uses the strongest
# available model.  Executor/fix steps may downgrade for cost.
AGENT_MODEL_ALIASES: dict[str, str] = {
    # Canonical IDs exposed by the LLM gateway (no date suffix required).
    "claude-opus-4-7": "claude-opus-4-7",
    "claude-opus-4-6": "claude-opus-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-sonnet-4-5": "claude-sonnet-4-5",
    "claude-haiku-4-5": "claude-haiku-4-5",
    "gemini-3-1-pro": "gemini-3-1-pro",
    "deepseek-v3.2": "deepseek-v3.2",
    # Legacy dated IDs from earlier drafts — map back to canonical.
    "claude-opus-4-7-20260208": "claude-opus-4-7",
    "claude-sonnet-4-6-20260117": "claude-sonnet-4-6",
}


def _normalise_model(name: str) -> str:
    return AGENT_MODEL_ALIASES.get(name, "claude-opus-4-7")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_TOOL_MANIFEST = """
Available tools (JSON schema for params shown after each):

- code_exec: Execute Python in a sandboxed container (no internet, /workspace RW, 1GB RAM, 60s default).
    params: { code: str, language?: "python"|"bash"|"typescript"|"javascript"|"rust",
              stdin?: str, cwd?: str (workspace-relative),
              wall_timeout_sec?: int (<=1800), mem_mb?: int (<=4096),
              cpu_sec?: int (<=1800), env?: {STR: STR} }
    result: { stdout, stderr, exit_code, duration_ms, timed_out, killed, artifacts: [{name,workspace_path,size,sha256}] }

- bash_exec / typescript_exec / rust_exec: aliases of code_exec with language fixed.
    For TS, Bun is the runtime — `console.log(...)` works. Rust compiles with rustc -O then runs.

- fs_read:  params: { path: str, binary?: bool, max_bytes?: int }
            result: { path, size, binary, content | content_b64 }

- fs_write: params: { path: str, content: str, binary?: bool, overwrite?: bool }
            For binary files (images, PDFs) pass base64 in content and set binary=true.
            result: { path, size, sha256 }

- fs_list:  params: { path?: str, recursive?: bool, max_entries?: int }
            result: { root, entries: [{path,type,size,mtime}], truncated }

- fs_delete: params: { path: str }

- browser_fetch: Navigate to a URL and extract body text.
    params: { url: str, wait_for?: "load"|"domcontentloaded"|"networkidle",
              wait_for_selector?: str, timeout_ms?: int,
              extract_text?: bool (default true), max_chars?: int }
    result: { status, final_url, title, body, links: [str], duration_ms }

- browser_screenshot: Screenshot a URL.  Pass save_to="img/foo.png" to persist in workspace.
    params: { url, full_page?: bool, save_to?: str (workspace-relative), viewport_width?, viewport_height? }
    result: { final_url, title, size, saved_to?, duration_ms }

- browser_pdf: Render a URL to PDF. Pass save_to="reports/foo.pdf".
    params: { url, format?: "A4"|"Letter"|"Legal", save_to?: str }

- browser_click_fetch: Navigate, click a CSS selector, return resulting page.
    params: { url, click_selector, wait_after_click_ms?, extract_text?, max_chars? }

- web_search: Perplexity Sonar search for current, cited information.
    params: { query: str }
    result: { query, answer, citations: [{url,title,snippet}] }

- think: Insert an explicit reasoning step (no side effect).
    params: { thought: str }
    result: { thought }

- deliver: Mark the task ready for delivery with a final_answer string.
    params: { final_answer: str }

RULES:
- Every step's "tool" MUST be exactly one of the names above.  Anything else fails.
- The workspace at /workspace/<user_id>/ persists between steps.  Write files with fs_write,
  or let code_exec write to ./ (auto-persisted under _runs/).
- The sandbox has NO internet.  Use browser_fetch or web_search to get remote data, then
  save to the workspace, then process with code_exec.
- For long-running jobs, set wall_timeout_sec explicitly (up to 1800).
- Prefer one larger code_exec block over many small ones when logic is related.
- Always end the plan with a "deliver" step whose final_answer concisely summarises results
  and references any artifacts (files, URLs) by their workspace paths.
""".strip()


_PLAN_SYSTEM_PROMPT = """You are Mariana, an autonomous computer agent.

The user has given you a goal.  Produce a concrete plan — an ordered list of
tool calls — that will accomplish the goal.  You MUST respond with a single
JSON object of the form:

{
  "reasoning": "<short explanation of your plan in 1-3 sentences>",
  "steps": [
    { "id": "s1", "title": "<short verb phrase>", "description": "<1-line>",
      "tool": "<tool name>", "params": { ... } },
    ...
    { "id": "sN", "title": "Deliver result", "tool": "deliver",
      "params": { "final_answer": "<user-facing summary>" } }
  ]
}

Rules:
- Output JSON ONLY.  No markdown fences, no prose outside the JSON object.
- 1-12 steps.  Be thorough but efficient.
- Step IDs must be unique, s1, s2, ... format.
- The LAST step MUST be "deliver".
- If a step depends on a previous step's output, reference it by narrative in
  "description" — the executor has full access to prior results.
- For code tasks: produce production-grade code with type hints, docstrings,
  and self-tests (asserts or pytest).  Run your own tests before delivery.
- Never emit a tool name not in the manifest.

""" + _TOOL_MANIFEST


_REPLAN_SYSTEM_PROMPT = """You are Mariana, an autonomous computer agent.

Your previous plan failed.  Produce a REVISED plan that works around the
failures.  You have the same tool manifest.  Same JSON output format.

Common failure modes and their fixes:
- "sandbox has no internet": prefix with browser_fetch or web_search, save to
  a workspace file, then read it from code_exec.
- "ModuleNotFoundError": the module isn't pre-installed.  Either use a
  different approach, or write pure-stdlib code.
- Non-zero exit code: inspect stderr in the failed step's error field.
  Patch the code or change the approach entirely.
- Playwright timeout: increase timeout_ms, or change wait_for to "domcontentloaded",
  or wait_for_selector on a known element.

""" + _TOOL_MANIFEST


_FIX_SYSTEM_PROMPT = """You are Mariana's fix loop.

A specific step in the plan just failed.  Produce a REVISED single step that
fixes the problem.  Respond with JSON of the form:

{
  "reasoning": "<1-line>",
  "step": { "id": "<same id as failed step>", "title": "...", "tool": "...", "params": {...} }
}

The NEW step replaces the failed one.  Keep the same `id`.  If the right fix
is "don't do this step at all", set tool to "think" and put the reason in
params.thought — the loop will treat that as a skip.

""" + _TOOL_MANIFEST


# ---------------------------------------------------------------------------
# LLM call helpers
# ---------------------------------------------------------------------------


def _llm_gateway_url() -> str:
    return os.getenv("LLM_GATEWAY_BASE_URL", "").rstrip("/")


def _llm_gateway_key() -> str:
    return os.getenv("LLM_GATEWAY_API_KEY", "")


async def _llm_json(
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 4096,
    temperature: float = 0.3,
    timeout_sec: float = 180.0,
) -> tuple[dict[str, Any], float]:
    """Call the LLM gateway and parse a JSON-only response.  Returns (parsed, cost_usd).

    Cost is estimated as 0.0 until the gateway returns a ``usage`` field we
    can map to pricing.  We pass it back as 0.0 and track it in the loop.
    """
    base = _llm_gateway_url()
    key = _llm_gateway_key()
    if not base or not key:
        raise RuntimeError("LLM_GATEWAY_BASE_URL / LLM_GATEWAY_API_KEY not configured")
    # Anthropic models via the gateway don't support the OpenAI-style
    # ``response_format: json_object`` flag.  Add JSON coercion via prompt
    # instead, and rely on ``_extract_json`` to strip fences / extract the
    # outermost object.  OpenAI / Gemini / DeepSeek still benefit from the
    # native json_object mode.
    supports_json_mode = not (
        "claude" in model.lower() or "anthropic" in model.lower()
    )
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
    }
    # Opus 4.7 and newer Anthropic reasoning models reject the ``temperature``
    # parameter outright.  Only include it for models known to accept it.
    rejects_temperature = (
        "opus-4-7" in model.lower()
        or "opus-4-6" in model.lower()
        or "sonnet-4-6" in model.lower()
        or "sonnet-4-5" in model.lower()
    )
    if not rejects_temperature:
        payload["temperature"] = temperature
    if supports_json_mode:
        payload["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        resp = await client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"LLM gateway error: {resp.status_code} {resp.text[:500]}")
    body = resp.json()
    text = body["choices"][0]["message"]["content"]
    parsed = _extract_json(text)
    usage = body.get("usage") or {}
    # Rough cost: prompt 3$/M, completion 15$/M for Opus; 0 for others we track separately.
    cost = _estimate_cost(model, usage)
    return parsed, cost


def _extract_json(text: str) -> dict[str, Any]:
    """Extract a JSON object from LLM output that may have surrounding text."""
    text = text.strip()
    # Strip markdown fences if present.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    # First, try full parse.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: extract the outermost {...} block.
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"LLM returned invalid JSON: {exc}\n---\n{text[:800]}") from exc
    raise RuntimeError(f"LLM returned no JSON object\n---\n{text[:800]}")


def _estimate_cost(model: str, usage: dict[str, Any]) -> float:
    # Very rough; gateway exposes per-model unit costs via a different endpoint
    # we'd need to query.  For now, use conservative fixed rates.
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    if "opus" in model:
        return (pt * 3.0 + ct * 15.0) / 1_000_000.0
    if "sonnet" in model:
        return (pt * 3.0 + ct * 15.0) / 1_000_000.0
    if "gemini" in model:
        return (pt * 1.25 + ct * 10.0) / 1_000_000.0
    if "deepseek" in model:
        return (pt * 0.14 + ct * 0.28) / 1_000_000.0
    return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def build_initial_plan(task: AgentTask) -> tuple[list[AgentStep], float]:
    """Produce the first plan for a task."""
    user = _format_goal(task)
    parsed, cost = await _llm_json(
        model=_normalise_model(task.selected_model),
        system=_PLAN_SYSTEM_PROMPT,
        user=user,
        max_tokens=6000,
        temperature=0.3,
    )
    steps = _validate_plan(parsed)
    return steps, cost


async def replan(task: AgentTask, *, reason: str) -> tuple[list[AgentStep], float]:
    """Produce a revised plan after a failure or dead end."""
    failed_steps = [s for s in task.steps if s.status == StepStatus.FAILED]
    summary = {
        "goal": task.goal,
        "user_instructions": task.user_instructions,
        "replan_count": task.replan_count,
        "replan_reason": reason,
        "prior_plan": [
            {
                "id": s.id,
                "title": s.title,
                "tool": s.tool,
                "params": _truncate_params(s.params),
                "status": s.status.value,
                "error": s.error,
                "stderr_tail": _tail(s.result.get("stderr", "") if s.result else "", 2000),
            }
            for s in task.steps
        ],
    }
    user = (
        "Previous attempt failed.  Review the history and produce a revised plan.\n\n"
        "HISTORY (JSON):\n" + json.dumps(summary, indent=2, default=str)
    )
    parsed, cost = await _llm_json(
        model=_normalise_model(task.selected_model),
        system=_REPLAN_SYSTEM_PROMPT,
        user=user,
        max_tokens=6000,
        temperature=0.3,
    )
    steps = _validate_plan(parsed)
    return steps, cost


async def fix_step(task: AgentTask, failed_step: AgentStep) -> tuple[AgentStep, float]:
    """Produce a single revised step to replace a failed one."""
    ctx = {
        "goal": task.goal,
        "failed_step": {
            "id": failed_step.id,
            "title": failed_step.title,
            "tool": failed_step.tool,
            "params": _truncate_params(failed_step.params),
            "attempts": failed_step.attempts,
            "error": failed_step.error,
            "result": _truncate_result(failed_step.result) if failed_step.result else None,
        },
        "prior_steps": [
            {"id": s.id, "title": s.title, "tool": s.tool, "status": s.status.value}
            for s in task.steps if s.id != failed_step.id
        ],
        "workspace_note": "Files written by earlier steps persist in the workspace.",
    }
    user = (
        "Step failed.  Produce a fixed replacement step.\n\n"
        "CONTEXT:\n" + json.dumps(ctx, indent=2, default=str)
    )
    # Sonnet is fine for single-step fixes; cheaper and faster than Opus.
    model = os.getenv("AGENT_FIX_MODEL") or "claude-sonnet-4-6"
    parsed, cost = await _llm_json(
        model=model,
        system=_FIX_SYSTEM_PROMPT,
        user=user,
        max_tokens=3000,
        temperature=0.2,
    )
    step_data = parsed.get("step") or {}
    # Preserve the failed step's id; LLM might emit a different one by mistake.
    step_data["id"] = failed_step.id
    validated = _validate_single_step(step_data)
    return validated, cost


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_plan(parsed: dict[str, Any]) -> list[AgentStep]:
    raw_steps = parsed.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise RuntimeError(f"plan missing 'steps' list: {parsed!r}")
    if len(raw_steps) > 25:
        raise RuntimeError(f"plan too long: {len(raw_steps)} steps (max 25)")
    out: list[AgentStep] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise RuntimeError(f"step {i} is not an object: {raw!r}")
        if "id" not in raw or not raw["id"]:
            raw["id"] = f"s{i+1}"
        if raw["id"] in seen_ids:
            raise RuntimeError(f"duplicate step id: {raw['id']!r}")
        seen_ids.add(raw["id"])
        step = _validate_single_step(raw)
        out.append(step)
    if out[-1].tool != "deliver":
        # Auto-append a deliver step if the LLM forgot.
        out.append(AgentStep(
            id="deliver",
            title="Deliver",
            tool="deliver",
            params={"final_answer": "Task complete."},
        ))
    return out


def _validate_single_step(raw: dict[str, Any]) -> AgentStep:
    tool = raw.get("tool")
    if tool not in VALID_TOOLS:
        raise RuntimeError(
            f"invalid tool {tool!r} (step id={raw.get('id')}).  Valid tools: {sorted(VALID_TOOLS)}"
        )
    # Let Pydantic do the rest.
    try:
        return AgentStep(
            id=str(raw.get("id") or uuid.uuid4().hex[:6]),
            title=str(raw.get("title") or tool),
            description=str(raw.get("description") or ""),
            tool=tool,
            params=raw.get("params") or {},
        )
    except Exception as exc:  # pydantic ValidationError
        raise RuntimeError(f"step validation failed: {exc}") from exc


def _truncate_params(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in params.items():
        if isinstance(v, str) and len(v) > 2000:
            out[k] = v[:2000] + f"…[truncated, {len(v)-2000} chars hidden]"
        else:
            out[k] = v
    return out


def _truncate_result(result: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in result.items():
        if isinstance(v, str) and len(v) > 4000:
            out[k] = v[:4000] + f"…[truncated]"
        else:
            out[k] = v
    return out


def _tail(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return "…" + text[-max_chars:]


def _format_goal(task: AgentTask) -> str:
    parts = [f"GOAL:\n{task.goal}"]
    if task.user_instructions:
        parts.append(f"\nUSER INSTRUCTIONS:\n{task.user_instructions}")
    parts.append(f"\nUSER_ID: {task.user_id} (use this for all workspace operations)")
    parts.append(f"WORKSPACE: /workspace/{task.user_id}/ (files persist across steps)")
    return "\n".join(parts)
