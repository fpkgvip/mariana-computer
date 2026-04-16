"""
Mariana Intelligence Engine — Gap Detection & Proactive Follow-Up (System 9)

After each research pass, a dedicated "gap detector" reviews the evidence ledger
and identifies what's missing. If the user asked about market size and the system
found revenue data but no unit economics, it autonomously launches a follow-up
search.

This is the equivalent of an analyst saying "I noticed we don't have data on X —
let me dig into that."
"""

from __future__ import annotations

import json
import structlog
from typing import Any

from pydantic import BaseModel, Field

from mariana.ai.session import spawn_model
from mariana.data.models import TaskType

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class IdentifiedGap(BaseModel):
    """A single gap identified in the evidence."""
    description: str = Field(..., description="What evidence is missing")
    priority: str = Field(
        ..., description="critical, high, medium, low"
    )
    category: str = Field(
        ...,
        description="data_missing, perspective_missing, temporal_gap, "
        "source_type_missing, contradiction_unresolved, methodology_unclear",
    )
    follow_up_query: str = Field(
        ..., description="Specific search query to fill this gap"
    )
    expected_source_types: list[str] = Field(
        default_factory=list,
        description="Where this data is likely found (e.g., 'sec_filing', 'government', 'academic')",
    )


class GapDetectionOutput(BaseModel):
    """Output from the gap detection LLM call."""
    gaps: list[IdentifiedGap] = Field(default_factory=list, description="Identified evidence gaps")
    completeness_score: float = Field(
        ..., ge=0.0, le=1.0,
        description="Overall completeness of the evidence (0=empty, 1=thorough)",
    )
    analysis_notes: str = Field(default="", description="Notes about the overall evidence quality")


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

async def detect_gaps(
    task_id: str,
    research_topic: str,
    db: Any,
    cost_tracker: Any,
    config: Any,
    quality_tier: str | None = None,
) -> dict[str, Any]:
    """
    Detect gaps in the evidence ledger for a research task.

    Reads the full evidence ledger, hypothesis rankings, contradiction matrix,
    and source diversity data to identify what's missing.

    Returns gap analysis results including auto-follow-up recommendations.
    """
    log = logger.bind(component="detect_gaps")

    # 1. Get evidence ledger summary
    ledger_rows = await db.fetch(
        """
        SELECT claim_text, subject, predicate, confidence, temporal_type
        FROM claims
        WHERE task_id = $1
        ORDER BY confidence DESC
        LIMIT 50
        """,
        task_id,
    )
    claims_summary = "\n".join(
        f"- [{r['confidence']:.2f}] {r['claim_text'][:200]}"
        for r in ledger_rows
    ) or "(no claims extracted yet)"

    # 2. Get hypothesis status
    hyp_rows = await db.fetch(
        """
        SELECT h.statement, h.status, h.score,
               COALESCE(hp.posterior, 0.5) as posterior
        FROM hypotheses h
        LEFT JOIN hypothesis_priors hp ON h.id = hp.hypothesis_id AND hp.task_id = h.task_id
        WHERE h.task_id = $1
        """,
        task_id,
    )
    hypotheses_summary = "\n".join(
        f"- [{r['status']}] (score={r['score']}, P={r['posterior']:.3f}) {r['statement'][:200]}"
        for r in hyp_rows
    ) or "(no hypotheses)"

    # 3. Get unresolved contradictions
    contradiction_rows = await db.fetch(
        """
        SELECT ca.claim_text as claim_a, cb.claim_text as claim_b, cp.severity
        FROM contradiction_pairs cp
        JOIN claims ca ON cp.claim_a_id = ca.id
        JOIN claims cb ON cp.claim_b_id = cb.id
        WHERE cp.task_id = $1 AND cp.resolution_status = 'unresolved'
        ORDER BY cp.severity DESC
        LIMIT 10
        """,
        task_id,
    )
    contradictions_summary = "\n".join(
        f"- [severity={r['severity']:.2f}] \"{r['claim_a'][:100]}\" vs \"{r['claim_b'][:100]}\""
        for r in contradiction_rows
    ) or "(no contradictions)"

    # 4. Get source diversity info
    diversity_row = await db.fetchrow(
        """
        SELECT
            COUNT(DISTINCT domain_authority) as authority_types,
            COUNT(*) as total_sources,
            MODE() WITHIN GROUP (ORDER BY domain_authority) as dominant_authority
        FROM source_scores
        WHERE task_id = $1
        """,
        task_id,
    )
    diversity_info = (
        f"Source types: {diversity_row['authority_types']}, "
        f"Total: {diversity_row['total_sources']}, "
        f"Dominant: {diversity_row['dominant_authority']}"
    ) if diversity_row and diversity_row["total_sources"] else "No source diversity data"

    # 5. LLM gap detection
    try:
        output, _session = await spawn_model(
            task_type=TaskType.GAP_DETECTION,
            context={
                "task_id": task_id,
                "research_topic": research_topic,
                "claims_summary": claims_summary,
                "hypotheses_summary": hypotheses_summary,
                "contradictions_summary": contradictions_summary,
                "diversity_info": diversity_info,
                "claims_count": len(ledger_rows),
            },
            output_schema=GapDetectionOutput,
            db=db,
            cost_tracker=cost_tracker,
            config=config,
            quality_tier=quality_tier,
        )
    except Exception as exc:
        log.warning("gap_detection_llm_failed", error=str(exc))
        return {"gaps": [], "completeness_score": 0.5}

    parsed: GapDetectionOutput = output  # type: ignore[assignment]

    # 6. Determine current analysis round
    round_row = await db.fetchrow(
        "SELECT MAX(analysis_round) as max_round FROM gap_analyses WHERE task_id = $1",
        task_id,
    )
    current_round = (int(round_row["max_round"]) + 1) if round_row and round_row["max_round"] else 1

    # 7. Persist gap analysis
    gaps_json = [g.model_dump() for g in parsed.gaps]
    try:
        await db.execute(
            """
            INSERT INTO gap_analyses (task_id, gaps, analysis_round)
            VALUES ($1, $2, $3)
            """,
            task_id,
            json.dumps(gaps_json),
            current_round,
        )
    except Exception as exc:
        log.warning("gap_analysis_persist_failed", error=str(exc))

    result = {
        "gaps": gaps_json,
        "completeness_score": parsed.completeness_score,
        "analysis_notes": parsed.analysis_notes,
        "round": current_round,
        "critical_gaps": [g for g in gaps_json if g.get("priority") == "critical"],
        "follow_up_queries": [g["follow_up_query"] for g in gaps_json if g.get("priority") in ("critical", "high")],
    }

    log.info(
        "gap_detection_complete",
        task_id=task_id,
        gaps_found=len(parsed.gaps),
        critical=len(result["critical_gaps"]),
        completeness=f"{parsed.completeness_score:.2f}",
    )
    return result


async def get_latest_gap_analysis(task_id: str, db: Any) -> dict[str, Any] | None:
    """Get the most recent gap analysis for a task."""
    row = await db.fetchrow(
        """
        SELECT * FROM gap_analyses
        WHERE task_id = $1
        ORDER BY analysis_round DESC
        LIMIT 1
        """,
        task_id,
    )
    return dict(row) if row else None
