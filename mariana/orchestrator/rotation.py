"""Orchestrator rotation — handoff context for seamless orchestrator restarts.

Stores and retrieves structured handoff context so that a freshly started
orchestrator process has full situational awareness of work already completed
on a given research task.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class OrchestratorContext:
    """Structured snapshot of orchestrator state at a given phase boundary."""

    task_id: str
    phase: str
    key_findings: list[str] = field(default_factory=list)
    active_hypotheses: list[str] = field(default_factory=list)
    killed_hypotheses: list[str] = field(default_factory=list)
    sources_found: list[str] = field(default_factory=list)
    quality_tier: str = "balanced"
    user_instructions: str = ""
    loop_config: dict[str, Any] = field(default_factory=dict)


async def write_handoff(db: Any, ctx: OrchestratorContext) -> None:
    """Persist a handoff snapshot to the ``orchestrator_handoffs`` table.

    The context is serialised as JSONB so that ``read_handoff`` can
    reconstruct the full ``OrchestratorContext`` on the next process start.
    """
    if db is None:
        logger.warning("write_handoff_skipped_no_db", extra={"task_id": ctx.task_id})
        return

    try:
        await db.execute(
            """
            INSERT INTO orchestrator_handoffs (task_id, phase, context)
            VALUES ($1, $2, $3::jsonb)
            """,
            ctx.task_id,
            ctx.phase,
            json.dumps(asdict(ctx), default=str),
        )
        logger.debug(
            "handoff_written",
            extra={"task_id": ctx.task_id, "phase": ctx.phase},
        )
    except Exception:
        logger.exception(
            "write_handoff_failed",
            extra={"task_id": ctx.task_id, "phase": ctx.phase},
        )


async def read_handoff(
    db: Any,
    task_id: str,
    current_phase: str,  # noqa: ARG001 — reserved for future phase filtering
) -> OrchestratorContext | None:
    """Read the most recent handoff for *task_id* from the database.

    Returns ``None`` when no prior handoff exists (first run).
    """
    if db is None:
        return None

    try:
        row = await db.fetchrow(
            """
            SELECT context FROM orchestrator_handoffs
            WHERE task_id = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            task_id,
        )
        if row is None:
            return None

        data: dict[str, Any] = (
            json.loads(row["context"])
            if isinstance(row["context"], str)
            else row["context"]
        )
        return OrchestratorContext(
            task_id=data.get("task_id", task_id),
            phase=data.get("phase", "UNKNOWN"),
            key_findings=data.get("key_findings", []),
            active_hypotheses=data.get("active_hypotheses", []),
            killed_hypotheses=data.get("killed_hypotheses", []),
            sources_found=data.get("sources_found", []),
            quality_tier=data.get("quality_tier", "balanced"),
            user_instructions=data.get("user_instructions", ""),
            loop_config=data.get("loop_config", {}),
        )
    except Exception:
        logger.exception(
            "read_handoff_failed",
            extra={"task_id": task_id},
        )
        return None


def build_rotation_prompt(ctx: OrchestratorContext) -> str:
    """Build a natural-language summary of prior orchestrator state.

    Injected into the research-architecture prompt so a fresh context window
    has full awareness of work already completed.
    """

    def _fmt(items: list[str], fallback: str = "(none)") -> str:
        if not items:
            return fallback
        return "\n".join(f"  • {item[:300]}" for item in items[:20])

    sections = [
        f"## Prior Orchestrator Handoff (phase: {ctx.phase})",
        "",
        "### Key Findings So Far",
        _fmt(ctx.key_findings),
        "",
        "### Active Hypotheses",
        _fmt(ctx.active_hypotheses),
        "",
        "### Killed Hypotheses",
        _fmt(ctx.killed_hypotheses),
        "",
        "### Sources Found",
        _fmt(ctx.sources_found),
        "",
        f"Quality tier: {ctx.quality_tier}",
    ]

    if ctx.user_instructions:
        sections.append(f"\nUser instructions: {ctx.user_instructions[:500]}")

    if ctx.loop_config:
        sections.append(f"\nLoop config: {json.dumps(ctx.loop_config)}")

    return "\n".join(sections)
