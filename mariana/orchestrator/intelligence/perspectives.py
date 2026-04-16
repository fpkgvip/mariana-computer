"""
Mariana Intelligence Engine — Multi-Perspective Synthesis (System 11)

Instead of producing a single narrative, the system internally generates the
research from multiple analyst personas:
- Bull case (optimistic)
- Bear case (pessimistic)
- Skeptic (questioning everything)
- Domain Expert (deep technical analysis)

A meta-synthesizer then merges these into a balanced report with explicit
disagreement sections. This combats one-sidedness.
"""

from __future__ import annotations

import asyncio
import json
import structlog
from typing import Any

from pydantic import BaseModel, Field

from mariana.ai.session import spawn_model
from mariana.data.models import TaskType

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Perspective definitions
# ---------------------------------------------------------------------------

PERSPECTIVES: dict[str, dict[str, str]] = {
    "bull": {
        "name": "Bull Analyst",
        "system_instruction": (
            "You are a bull-case analyst. Your job is to find and articulate the most "
            "compelling positive narrative supported by the evidence. Look for growth "
            "catalysts, competitive advantages, positive momentum, and underappreciated "
            "strengths. Be specific and cite evidence. Do NOT fabricate — only argue from "
            "what the evidence supports. But find the strongest case."
        ),
    },
    "bear": {
        "name": "Bear Analyst",
        "system_instruction": (
            "You are a bear-case analyst. Your job is to find and articulate the most "
            "concerning risks and negative signals in the evidence. Look for headwinds, "
            "competitive threats, deteriorating fundamentals, regulatory risks, and "
            "overvaluation signals. Be specific and cite evidence. Do NOT fabricate — "
            "only argue from what the evidence supports. But find the strongest case for caution."
        ),
    },
    "skeptic": {
        "name": "Skeptic Analyst",
        "system_instruction": (
            "You are a skeptic analyst. Your job is to question every claim, identify "
            "assumptions that lack evidence, highlight data gaps, and flag where the "
            "evidence is thin or contradictory. You trust nothing at face value. Point out "
            "what we DON'T know, where sources conflict, and what alternative explanations "
            "exist. Be rigorous."
        ),
    },
    "domain_expert": {
        "name": "Domain Expert",
        "system_instruction": (
            "You are a deep domain expert analyzing this topic. Focus on technical nuance, "
            "structural factors, second-order effects, and industry-specific dynamics that "
            "a generalist would miss. Bring specialized knowledge and identify the most "
            "technically significant findings. Explain complex mechanisms clearly."
        ),
    },
}


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class PerspectiveSynthesis(BaseModel):
    """Output from a single perspective analysis."""
    perspective: str = Field(..., description="bull, bear, skeptic, or domain_expert")
    thesis_statement: str = Field(..., description="One-sentence thesis from this perspective")
    key_arguments: list[str] = Field(
        ..., min_length=1,
        description="Ranked list of key arguments from this perspective",
    )
    supporting_evidence: list[str] = Field(
        default_factory=list,
        description="Specific claim IDs or evidence snippets supporting the arguments",
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0,
        description="How confident this perspective is in its thesis",
    )
    synthesis_text: str = Field(
        ...,
        description="Full paragraph-length synthesis from this perspective",
    )


class MetaSynthesisOutput(BaseModel):
    """Output from the meta-synthesizer that merges all perspectives."""
    balanced_synthesis: str = Field(
        ...,
        description="Balanced synthesis incorporating all perspectives",
    )
    consensus_points: list[str] = Field(
        default_factory=list,
        description="Points where all perspectives agree",
    )
    disagreement_points: list[str] = Field(
        default_factory=list,
        description="Points where perspectives meaningfully disagree",
    )
    recommended_view: str = Field(
        ...,
        description="The recommended overall view with confidence",
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0,
        description="Overall confidence in the balanced synthesis",
    )


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

async def generate_perspectives(
    task_id: str,
    research_topic: str,
    db: Any,
    cost_tracker: Any,
    config: Any,
    quality_tier: str | None = None,
) -> list[dict[str, Any]]:
    """
    Generate analysis from all 4 perspectives in parallel.

    Each perspective reads the full evidence ledger and produces its own synthesis.
    Results are stored in perspective_syntheses table.

    Returns list of perspective synthesis dicts.
    """
    log = logger.bind(component="generate_perspectives")

    # 1. Build evidence summary from claims
    claims_rows = await db.fetch(
        """
        SELECT claim_text, subject, predicate, object, confidence
        FROM claims
        WHERE task_id = $1
        ORDER BY confidence DESC
        LIMIT 60
        """,
        task_id,
    )
    evidence_text = "\n".join(
        f"[{r['confidence']:.2f}] {r['claim_text'][:300]}"
        for r in claims_rows
    ) or "(no evidence)"

    # 2. Get hypothesis rankings
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
        f"[P={r['posterior']:.3f}] {r['statement'][:200]}" if r["posterior"] else f"[no prior] {r['statement'][:200]}"
        for r in hyp_rows
    ) or "(no hypotheses)"

    # 3. Get contradiction summary
    contra_rows = await db.fetch(
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
    contradictions_text = "\n".join(
        f"[severity={r['severity']:.2f}] \"{r['claim_a'][:100]}\" vs \"{r['claim_b'][:100]}\""
        for r in contra_rows
    ) or "(no contradictions)"

    # 4. Run all 4 perspectives in parallel
    async def run_perspective(perspective_key: str) -> dict[str, Any] | None:
        perspective_info = PERSPECTIVES[perspective_key]
        try:
            output, _session = await spawn_model(
                task_type=TaskType.PERSPECTIVE_SYNTHESIS,
                context={
                    "task_id": task_id,
                    "research_topic": research_topic,
                    "perspective": perspective_key,
                    "perspective_instruction": perspective_info["system_instruction"],
                    "evidence": evidence_text,
                    "hypotheses": hypotheses_text,
                    "contradictions": contradictions_text,
                },
                output_schema=PerspectiveSynthesis,
                db=db,
                cost_tracker=cost_tracker,
                config=config,
                quality_tier=quality_tier,
            )
            parsed: PerspectiveSynthesis = output  # type: ignore[assignment]

            # Persist
            await db.execute(
                """
                INSERT INTO perspective_syntheses (
                    task_id, perspective, synthesis_text,
                    key_arguments, confidence, cited_claim_ids
                ) VALUES ($1, $2, $3, $4, $5, $6)
                """,
                task_id,
                perspective_key,
                parsed.synthesis_text,
                json.dumps(parsed.key_arguments),
                parsed.confidence,
                json.dumps(parsed.supporting_evidence),
            )

            return parsed.model_dump()
        except Exception as exc:
            log.warning("perspective_failed", perspective=perspective_key, error=str(exc))
            return None

    results = await asyncio.gather(
        *[run_perspective(k) for k in PERSPECTIVES],
        return_exceptions=False,
    )

    perspectives = [r for r in results if r is not None]

    log.info(
        "perspectives_generated",
        task_id=task_id,
        count=len(perspectives),
    )
    return perspectives


async def meta_synthesize(
    task_id: str,
    perspectives: list[dict[str, Any]],
    research_topic: str,
    db: Any,
    cost_tracker: Any,
    config: Any,
    quality_tier: str | None = None,
) -> dict[str, Any]:
    """
    Merge multiple perspective syntheses into a balanced view.

    This is the meta-synthesizer that identifies consensus, disagreements,
    and produces a recommended overall view.
    """
    log = logger.bind(component="meta_synthesize")

    if not perspectives:
        return {
            "balanced_synthesis": "",
            "consensus_points": [],
            "disagreement_points": [],
            "recommended_view": "Insufficient perspectives for synthesis",
            "confidence": 0.0,
        }

    perspectives_text = "\n\n".join(
        f"=== {p.get('perspective', 'unknown').upper()} ANALYST (confidence: {p.get('confidence', 0):.2f}) ===\n"
        f"Thesis: {p.get('thesis_statement', '')}\n"
        f"Synthesis: {p.get('synthesis_text', '')}\n"
        f"Key arguments: {', '.join(p.get('key_arguments', []))}"
        for p in perspectives
    )

    try:
        output, _session = await spawn_model(
            task_type=TaskType.META_SYNTHESIS,
            context={
                "task_id": task_id,
                "research_topic": research_topic,
                "perspectives": perspectives_text,
                "perspective_count": len(perspectives),
            },
            output_schema=MetaSynthesisOutput,
            db=db,
            cost_tracker=cost_tracker,
            config=config,
            quality_tier=quality_tier,
        )
    except Exception as exc:
        log.warning("meta_synthesis_failed", error=str(exc))
        return {
            "balanced_synthesis": "",
            "consensus_points": [],
            "disagreement_points": [],
            "recommended_view": "Meta-synthesis failed",
            "confidence": 0.0,
        }

    parsed: MetaSynthesisOutput = output  # type: ignore[assignment]

    log.info(
        "meta_synthesis_complete",
        task_id=task_id,
        consensus_count=len(parsed.consensus_points),
        disagreement_count=len(parsed.disagreement_points),
        confidence=f"{parsed.confidence:.2f}",
    )

    return parsed.model_dump()
