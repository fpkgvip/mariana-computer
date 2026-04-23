"""Self-knowledge for the agent: the authoritative description of what
Mariana can do, which tools it exposes, and what its guarantees are.

Consumed by:
- `describe_self` tool (so the LLM can answer "what can you do?" mid-task
  without hallucinating capabilities)
- `/api/agent/about` endpoint (so the frontend About panel stays in sync)
- System prompt v3 (loaded at plan time so the agent knows its own limits)

Single source of truth.  When we add/remove a tool or skill, update here
and the three surfaces stay aligned automatically.
"""
from __future__ import annotations

from typing import Any

from mariana.agent.dispatcher import VALID_TOOLS
from mariana.agent.skills import ALL_SKILLS

# Version bumped whenever the capability surface changes.  Used by the
# frontend to invalidate any cached About copy.
VERSION = "3.0.0"

PRODUCT_NAME = "Mariana Computer"
TAGLINE = (
    "Autonomous agent for financial firms, social media agencies, and power "
    "users.  Plans, executes, self-corrects, and delivers."
)

# Short capability bullets.  Keep punchy \u2014 these show up in About panels and
# bot replies when a user asks 'what can you do?'.
CAPABILITIES = [
    "Run multi-step plans that include code (Python, Rust, Bash, TypeScript), "
    "browsing the web, reading files, and generating deliverables.",
    "Self-correct: if a step fails, the agent inspects the error, fixes the "
    "code, and retries (up to 5 attempts per step, 3 replans per task).",
    "Deliver files to your workspace: Markdown reports, PDFs, PowerPoint, "
    "Excel with live formulas, generated images, generated videos.",
    "Operate in a hardened sandbox: every code step runs in a containerised "
    "environment with no internet and strict memory/CPU caps.",
    "Stream live: every plan, step, and terminal output is pushed to the UI "
    "over SSE as it happens \u2014 no polling, no blank spinners.",
    "Respect budgets: the task halts automatically when the wall-clock or "
    "dollar budget is reached.",
]

# Tool categories mapped to the tool names that back them.  Used both to
# render the About page and to run an internal self-check that the dispatcher
# hasn't drifted from the stated capability surface.
TOOL_CATEGORIES: dict[str, list[str]] = {
    "Code execution": [
        "code_exec", "bash_exec", "typescript_exec", "rust_exec",
    ],
    "Filesystem": ["fs_read", "fs_write", "fs_list", "fs_delete"],
    "Web & research": [
        "browser_fetch", "browser_screenshot", "browser_pdf",
        "browser_click_fetch", "web_search",
    ],
    "Media generation": ["generate_image", "generate_video"],
    "Planning & delivery": ["think", "deliver", "describe_self"],
}


LIMITS = {
    "max_steps_per_task": 25,
    "max_fix_attempts_per_step": 5,
    "max_replans_per_task": 3,
    "sandbox_no_internet": True,
    "sandbox_default_memory_mb": 1024,
    "sandbox_max_memory_mb": 4096,
    "sandbox_default_cpu_sec": 60,
    "sandbox_max_cpu_sec": 1800,
}


def _all_tools_documented() -> list[str]:
    """Return any tool in the dispatcher that isn't in a category.  Used by
    self-tests to catch drift."""
    documented = {t for tools in TOOL_CATEGORIES.values() for t in tools}
    return sorted(set(VALID_TOOLS) - documented)


def describe_self_payload() -> dict[str, Any]:
    """Canonical self-description returned by the ``describe_self`` tool."""
    return {
        "version": VERSION,
        "name": PRODUCT_NAME,
        "tagline": TAGLINE,
        "capabilities": CAPABILITIES,
        "tools": {
            "count": len(VALID_TOOLS),
            "categories": TOOL_CATEGORIES,
            "all": sorted(VALID_TOOLS),
        },
        "skills": [s.name for s in ALL_SKILLS],
        "limits": LIMITS,
        "undocumented_tools": _all_tools_documented(),
    }
