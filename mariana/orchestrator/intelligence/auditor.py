"""
Mariana Intelligence Engine — Reasoning Chain Auditor (System 14)

Before the final report is emitted, an independent auditor agent reviews the
full reasoning chain from evidence → claims → synthesis → conclusions.

It checks for:
- Logical fallacies (hasty generalization, false cause, straw man, etc.)
- Unsupported jumps (conclusions that don't follow from evidence)
- Circular reasoning (claim A supports B which supports A)
- Cherry-picking (selective use of evidence)
- Overconfidence (high confidence with thin evidence)

This is the internal "quality gate" — a senior analyst reviewing a junior's
work before it goes to the client.
"""

from __future__ import annotations

import json
import structlog
from typing import Any

from pydantic import BaseModel, Field

from mariana.ai.session import spawn_model
from mariana.ai.prompt_builder import _sanitize_untrusted_text
from mariana.data.models import TaskType

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class AuditIssue(BaseModel):
    """A single issue found during the reasoning chain audit."""
    issue_type: str = Field(
        ...,
        description="One of: logical_fallacy, unsupported_jump, circular_reasoning, "
        "cherry_picking, overconfidence, missing_context, temporal_error, "
        "source_quality, contradicts_evidence, ambiguous_claim",
    )
    severity: str = Field(
        ..., description="critical, major, minor"
    )
    description: str = Field(
        ..., description="Clear description of the issue"
    )
    location: str = Field(
        ..., description="Where in the reasoning chain this issue occurs"
    )
    suggestion: str = Field(
        ..., description="How to fix or mitigate this issue"
    )
    affected_claims: list[str] = Field(
        default_factory=list,
        description="Claim texts or IDs affected by this issue",
    )


class AuditOutput(BaseModel):
    """Full audit result."""
    issues: list[AuditIssue] = Field(
        default_factory=list,
        description="All issues found in the reasoning chain",
    )
    passed: bool = Field(
        ...,
        description="True if no critical issues and total issues are acceptable",
    )
    overall_score: float = Field(
        ..., ge=0.0, le=1.0,
        description="Overall quality score (0=terrible, 1=publication-ready)",
    )
    auditor_notes: str = Field(
        ...,
        description="Overall assessment and recommendations from the auditor",
    )
    critical_count: int = Field(default=0, description="Number of critical issues")
    major_count: int = Field(default=0, description="Number of major issues")
    minor_count: int = Field(default=0, description="Number of minor issues")


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

async def audit_reasoning_chain(
    task_id: str,
    research_topic: str,
    db: Any,
    cost_tracker: Any,
    config: Any,
    quality_tier: str | None = None,
    audit_type: str = "full",
) -> dict[str, Any]:
    """
    Audit the full reasoning chain for a research task.

    Reads claims, hypotheses, perspectives, contradictions, and syntheses
    to check for logical issues.

    Args:
        task_id: Research task ID.
        research_topic: The research topic.
        db: asyncpg pool.
        cost_tracker: Cost tracker.
        config: App config.
        quality_tier: Optional quality tier.
        audit_type: "full" or "incremental".

    Returns:
        Audit results including pass/fail and issues list.
    """
    log = logger.bind(component="audit")

    # 1. Gather all evidence
    claims_rows = await db.fetch(
        """
        SELECT claim_text, subject, predicate, object, confidence,
               corroboration_count, credibility_score
        FROM claims WHERE task_id = $1
        ORDER BY confidence DESC
        """,
        task_id,
    )
    claims_text = "\n".join(
        f"[conf={r['confidence']:.2f}, corr={r['corroboration_count']}, "
        f"cred={r['credibility_score'] or 'N/A'}] {r['claim_text'][:300]}"
        for r in claims_rows
    ) or "(no claims)"

    # 2. Hypothesis rankings
    hyp_rows = await db.fetch(
        """
        SELECT h.statement, hp.posterior, h.score
        FROM hypotheses h
        LEFT JOIN hypothesis_priors hp ON h.id = hp.hypothesis_id AND hp.task_id = h.task_id
        WHERE h.task_id = $1
        ORDER BY COALESCE(hp.posterior, 0.5) DESC
        """,
        task_id,
    )
    hypotheses_text = "\n".join(
        f"[P={r['posterior']:.3f}, score={r['score']}] {r['statement'][:200]}"
        if r.get("posterior") else f"[no Bayesian prior] {r['statement'][:200]}"
        for r in hyp_rows
    ) or "(no hypotheses)"

    # 3. Contradictions
    contra_rows = await db.fetch(
        """
        SELECT ca.claim_text as claim_a, cb.claim_text as claim_b,
               cp.severity, cp.resolution_status
        FROM contradiction_pairs cp
        JOIN claims ca ON cp.claim_a_id = ca.id
        JOIN claims cb ON cp.claim_b_id = cb.id
        WHERE cp.task_id = $1
        ORDER BY cp.severity DESC
        """,
        task_id,
    )
    contradictions_text = "\n".join(
        f"[{r['resolution_status']}, severity={r['severity']:.2f}] "
        f"\"{r['claim_a'][:100]}\" vs \"{r['claim_b'][:100]}\""
        for r in contra_rows
    ) or "(no contradictions)"

    # 4. Perspective syntheses
    perspective_rows = await db.fetch(
        """
        SELECT perspective, synthesis_text, confidence, key_arguments
        FROM perspective_syntheses
        WHERE task_id = $1
        """,
        task_id,
    )
    perspectives_text = "\n\n".join(
        f"=== {r['perspective'].upper()} (conf={r['confidence']:.2f}) ===\n{r['synthesis_text'][:500]}"
        for r in perspective_rows
    ) or "(no perspectives generated)"

    # 5. Source diversity
    diversity_row = await db.fetchrow(
        """
        SELECT
            COUNT(*) as total,
            COUNT(DISTINCT domain_authority) as types,
            AVG(composite_score) as avg_score
        FROM source_scores WHERE task_id = $1
        """,
        task_id,
    )
    source_info = (
        f"Sources: {diversity_row['total']}, Types: {diversity_row['types']}, "
        f"Avg credibility: {float(diversity_row['avg_score'] or 0):.2f}"
    ) if diversity_row else "No source data"

    # 6. LLM audit
    # BUG-0056 fix: sanitize all text fields derived from prior LLM outputs
    # (claims, hypotheses, contradictions, perspectives all originate from LLM calls)
    try:
        output, _session = await spawn_model(
            task_type=TaskType.REASONING_AUDIT,
            context={
                "task_id": task_id,
                "research_topic": _sanitize_untrusted_text(research_topic, max_chars=2000),
                "claims": _sanitize_untrusted_text(claims_text, max_chars=10000),
                "claims_count": len(claims_rows),
                "hypotheses": _sanitize_untrusted_text(hypotheses_text, max_chars=6000),
                "contradictions": _sanitize_untrusted_text(contradictions_text, max_chars=6000),
                "perspectives": _sanitize_untrusted_text(perspectives_text, max_chars=8000),
                "source_info": source_info,
                "audit_type": audit_type,
            },
            output_schema=AuditOutput,
            db=db,
            cost_tracker=cost_tracker,
            config=config,
            quality_tier=quality_tier,
        )
    except Exception as exc:
        log.warning("audit_llm_failed", error=str(exc))
        return {
            "passed": False,
            "overall_score": 0.0,
            "issues": [],
            "auditor_notes": f"Audit failed: {exc}",
        }

    parsed: AuditOutput = output  # type: ignore[assignment]

    # Count severities
    issues_json = [i.model_dump() for i in parsed.issues]
    critical_count = sum(1 for i in parsed.issues if i.severity == "critical")
    major_count = sum(1 for i in parsed.issues if i.severity == "major")
    minor_count = sum(1 for i in parsed.issues if i.severity == "minor")

    # BUG-0021 fix: do NOT trust the LLM's self-reported `passed` field.
    # Instead, apply deterministic server-side criteria:
    #   passed IFF: LLM said passed AND zero critical issues AND score >= 0.6
    server_passed = (
        parsed.passed
        and critical_count == 0
        and parsed.overall_score >= 0.6
    )
    if server_passed != parsed.passed:
        log.warning(
            "audit_pass_overridden",
            llm_passed=parsed.passed,
            server_passed=server_passed,
            critical_count=critical_count,
            overall_score=parsed.overall_score,
        )

    # 7. Persist audit results
    try:
        await db.execute(
            """
            INSERT INTO audit_results (
                task_id, audit_type, issues, passed,
                overall_score, auditor_notes
            ) VALUES ($1, $2, $3, $4, $5, $6)
            """,
            task_id,
            audit_type,
            json.dumps(issues_json),
            server_passed,
            parsed.overall_score,
            parsed.auditor_notes,
        )
    except Exception as exc:
        log.warning("audit_persist_failed", error=str(exc))

    result = {
        "passed": server_passed,
        "overall_score": parsed.overall_score,
        "issues": issues_json,
        "auditor_notes": parsed.auditor_notes,
        "critical_count": critical_count,
        "major_count": major_count,
        "minor_count": minor_count,
        "total_issues": len(parsed.issues),
    }

    log.info(
        "audit_complete",
        task_id=task_id,
        passed=server_passed,
        score=f"{parsed.overall_score:.2f}",
        critical=critical_count,
        major=major_count,
        minor=minor_count,
    )
    return result


async def get_latest_audit(task_id: str, db: Any) -> dict[str, Any] | None:
    """Get the most recent audit result for a task."""
    row = await db.fetchrow(
        """
        SELECT * FROM audit_results
        WHERE task_id = $1
        ORDER BY created_at DESC
        LIMIT 1
        """,
        task_id,
    )
    return dict(row) if row else None
