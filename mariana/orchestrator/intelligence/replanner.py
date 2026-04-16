"""
Mariana Intelligence Engine — Adaptive Query Decomposition & Replanning (System 5)

The initial research plan is a hypothesis. As evidence arrives, the system
replans. If a sub-query returns nothing useful, the decomposer reformulates.
If an unexpected angle surfaces, it spawns a new branch.

This is the planner agent that re-evaluates the investigation graph every
N evaluation cycles (configurable, default 3).
"""

from __future__ import annotations

import json
import structlog
from typing import Any

from pydantic import BaseModel, Field

from mariana.ai.session import spawn_model
from mariana.data.models import TaskType

logger = structlog.get_logger(__name__)

# Default: replan every 3 evaluation cycles
REPLAN_INTERVAL = 3


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class PlanModification(BaseModel):
    """A proposed modification to the research plan."""
    action: str = Field(
        ...,
        description="One of: spawn_branch, kill_branch, modify_query, redirect_focus, add_source_type",
    )
    target: str = Field(
        ..., description="What to modify (branch_id, hypothesis_id, or new query text)"
    )
    rationale: str = Field(..., description="Why this modification is needed")
    new_query: str | None = Field(
        default=None,
        description="New search query if action is spawn_branch or modify_query",
    )
    priority: int = Field(default=5, ge=1, le=10, description="Priority of this modification")


class ReplanOutput(BaseModel):
    """Output from the replanning LLM call."""
    should_replan: bool = Field(
        ..., description="Whether modifications to the research plan are needed"
    )
    modifications: list[PlanModification] = Field(
        default_factory=list,
        description="Proposed modifications to the plan",
    )
    plan_effectiveness_score: float = Field(
        ..., ge=0.0, le=1.0,
        description="How effective the current plan is (0=useless, 1=perfect)",
    )
    overall_assessment: str = Field(
        ..., description="Brief assessment of research progress and plan quality"
    )


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

async def should_replan(
    task_id: str,
    evaluation_cycle: int,
    db: Any,
    interval: int = REPLAN_INTERVAL,
) -> bool:
    """
    Determine if it's time to replan based on the evaluation cycle count.

    Returns True every `interval` cycles, or if there are critical gaps.
    """
    if evaluation_cycle > 0 and evaluation_cycle % interval == 0:
        return True

    # Also check if there are critical unresolved gaps
    gap_row = await db.fetchrow(
        """
        SELECT gaps FROM gap_analyses
        WHERE task_id = $1
        ORDER BY analysis_round DESC
        LIMIT 1
        """,
        task_id,
    )
    if gap_row and gap_row["gaps"]:
        gaps = gap_row["gaps"]
        if isinstance(gaps, str):
            try:
                gaps = json.loads(gaps)
            except Exception:
                gaps = []
        critical = [g for g in gaps if isinstance(g, dict) and g.get("priority") == "critical"]
        if len(critical) >= 2:
            return True

    return False


async def execute_replan(
    task_id: str,
    research_topic: str,
    evaluation_cycle: int,
    db: Any,
    cost_tracker: Any,
    config: Any,
    quality_tier: str | None = None,
) -> dict[str, Any]:
    """
    Execute a replanning cycle: assess plan effectiveness and propose modifications.

    Args:
        task_id: Research task ID.
        research_topic: The research topic.
        evaluation_cycle: Current evaluation cycle number.
        db: asyncpg pool.
        cost_tracker: Cost tracker.
        config: App config.
        quality_tier: Optional quality tier.

    Returns:
        Replan results including modifications to execute.
    """
    log = logger.bind(component="replan")

    # 1. Get current plan (or create initial)
    plan_row = await db.fetchrow(
        "SELECT * FROM research_plans WHERE task_id = $1 AND is_active = TRUE ORDER BY version DESC LIMIT 1",
        task_id,
    )
    current_plan = dict(plan_row) if plan_row else {"version": 0, "plan_data": {}}

    # 2. Get branch status summary
    branch_rows = await db.fetch(
        """
        SELECT b.id, b.status, b.cycles_completed, b.budget_spent,
               h.statement as hypothesis, h.score
        FROM branches b
        JOIN hypotheses h ON b.hypothesis_id = h.id
        WHERE b.task_id = $1
        ORDER BY h.score DESC NULLS LAST
        """,
        task_id,
    )
    branch_summary = "\n".join(
        f"- Branch {r['id'][:8]}: {r['status']} | cycles={r['cycles_completed']} | "
        f"score={r['score']} | ${r['budget_spent']:.2f} spent | {r['hypothesis'][:150]}"
        for r in branch_rows
    ) or "(no branches)"

    # 3. Get latest gap analysis
    gap_row = await db.fetchrow(
        "SELECT gaps, follow_ups_launched FROM gap_analyses WHERE task_id = $1 ORDER BY analysis_round DESC LIMIT 1",
        task_id,
    )
    gaps_summary = ""
    if gap_row and gap_row["gaps"]:
        gaps = gap_row["gaps"]
        if isinstance(gaps, str):
            try:
                gaps = json.loads(gaps)
            except Exception:
                gaps = []
        if isinstance(gaps, list):
            gaps_summary = "\n".join(
                f"- [{g.get('priority', 'medium')}] {g.get('description', '')[:200]}"
                for g in gaps
            )
    gaps_summary = gaps_summary or "(no gap analysis yet)"

    # 4. Get evidence coverage
    claims_row = await db.fetchrow(
        """
        SELECT
            COUNT(*) as total_claims,
            COUNT(DISTINCT subject) as unique_subjects,
            AVG(confidence) as avg_confidence
        FROM claims WHERE task_id = $1
        """,
        task_id,
    )
    evidence_info = (
        f"Claims: {claims_row['total_claims']}, "
        f"Subjects: {claims_row['unique_subjects']}, "
        f"Avg confidence: {float(claims_row['avg_confidence'] or 0):.2f}"
    ) if claims_row else "No evidence yet"

    # 5. LLM replan
    try:
        output, _session = await spawn_model(
            task_type=TaskType.REPLAN,
            context={
                "task_id": task_id,
                "research_topic": research_topic,
                "current_plan_version": current_plan.get("version", 0),
                "evaluation_cycle": evaluation_cycle,
                "branch_summary": branch_summary,
                "gaps_summary": gaps_summary,
                "evidence_info": evidence_info,
            },
            output_schema=ReplanOutput,
            db=db,
            cost_tracker=cost_tracker,
            config=config,
            quality_tier=quality_tier,
        )
    except Exception as exc:
        log.warning("replan_llm_failed", error=str(exc))
        return {"replanned": False, "reason": str(exc)}

    parsed: ReplanOutput = output  # type: ignore[assignment]

    # 6. Persist new plan version
    new_version = current_plan.get("version", 0) + 1
    modifications_json = [m.model_dump() for m in parsed.modifications]

    try:
        # Deactivate old plan
        await db.execute(
            "UPDATE research_plans SET is_active = FALSE WHERE task_id = $1",
            task_id,
        )
        # Insert new plan
        await db.execute(
            """
            INSERT INTO research_plans (
                task_id, version, plan_data, trigger_reason, is_active
            ) VALUES ($1, $2, $3, $4, TRUE)
            """,
            task_id,
            new_version,
            json.dumps({
                "modifications": modifications_json,
                "effectiveness_score": parsed.plan_effectiveness_score,
                "assessment": parsed.overall_assessment,
            }),
            f"Cycle {evaluation_cycle} replan",
        )
    except Exception as exc:
        log.warning("replan_persist_failed", error=str(exc))

    result = {
        "replanned": parsed.should_replan,
        "version": new_version,
        "modifications": modifications_json,
        "effectiveness_score": parsed.plan_effectiveness_score,
        "assessment": parsed.overall_assessment,
        "spawn_branch_queries": [
            m["new_query"] for m in modifications_json
            if m.get("action") == "spawn_branch" and m.get("new_query")
        ],
    }

    log.info(
        "replan_complete",
        task_id=task_id,
        version=new_version,
        modifications=len(parsed.modifications),
        effectiveness=f"{parsed.plan_effectiveness_score:.2f}",
    )
    return result
