"""
Mariana Intelligence Engine — Executive Summary Generator (System 15)

Generates summaries at multiple compression levels from the evidence ledger:
- One-liner: Single most important insight
- Paragraph: Top 3-5 insights with context
- Page: Comprehensive summary with citations
- Full: Complete analysis (delegates to report generator)

Each level requires different synthesis strategies. The one-liner needs the
single most impactful finding; the paragraph needs narrative flow; the page
needs structure and citations.
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

class OneLinerOutput(BaseModel):
    """The single most important insight."""
    one_liner: str = Field(
        ..., min_length=10, max_length=1000,
        description="Single sentence capturing THE key insight",
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0,
        description="Confidence in this as the most important finding",
    )


class ParagraphSummaryOutput(BaseModel):
    """Top 3-5 insights in paragraph form."""
    summary: str = Field(
        ..., min_length=50, max_length=5000,
        description="Paragraph-length summary of the top insights",
    )
    key_points: list[str] = Field(
        ..., min_length=1, max_length=5,
        description="The top 3-5 most important points",
    )


class PageSummaryOutput(BaseModel):
    """Full page summary with citations."""
    summary: str = Field(
        ..., min_length=100, max_length=50000,
        description="Page-length summary with structured sections and citations",
    )
    sections: list[str] = Field(
        default_factory=list,
        description="Section titles in the summary",
    )
    citations: list[str] = Field(
        default_factory=list,
        description="Source citations referenced in the summary",
    )


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

async def generate_executive_summaries(
    task_id: str,
    research_topic: str,
    db: Any,
    cost_tracker: Any,
    config: Any,
    quality_tier: str | None = None,
) -> dict[str, Any]:
    """
    Generate all compression levels of executive summary.

    Reads the full evidence ledger, hypothesis rankings, perspectives,
    and audit results to produce summaries at each level.

    Returns dict with one_liner, paragraph, and page_summary.
    """
    log = logger.bind(component="executive_summary")

    # 1. Build comprehensive evidence context
    claims_rows = await db.fetch(
        """
        SELECT claim_text, confidence, subject, predicate, object
        FROM claims WHERE task_id = $1
        ORDER BY confidence DESC
        LIMIT 80
        """,
        task_id,
    )
    evidence = "\n".join(
        f"[{r['confidence']:.2f}] {r['claim_text'][:400]}"
        for r in claims_rows
    ) or "(no evidence)"

    # 2. Get hypothesis rankings
    hyp_rows = await db.fetch(
        """
        SELECT h.statement, COALESCE(hp.posterior, 0.5) as posterior
        FROM hypotheses h
        LEFT JOIN hypothesis_priors hp ON h.id = hp.hypothesis_id AND hp.task_id = h.task_id
        WHERE h.task_id = $1
        ORDER BY COALESCE(hp.posterior, 0.5) DESC
        """,
        task_id,
    )
    hypotheses = "\n".join(
        f"[P={r['posterior']:.3f}] {r['statement'][:200]}"
        for r in hyp_rows
    ) or "(no hypotheses)"

    # 3. Get perspective syntheses
    persp_rows = await db.fetch(
        "SELECT perspective, synthesis_text FROM perspective_syntheses WHERE task_id = $1",
        task_id,
    )
    perspectives = "\n\n".join(
        f"[{r['perspective']}] {r['synthesis_text'][:500]}"
        for r in persp_rows
    ) or "(no perspectives)"

    # 4. Get contradiction summary
    contra_count = await db.fetchval(
        "SELECT COUNT(*) FROM contradiction_pairs WHERE task_id = $1 AND resolution_status = 'unresolved'",
        task_id,
    )

    # 5. Get source info
    source_row = await db.fetchrow(
        """
        SELECT COUNT(*) as cnt, AVG(composite_score) as avg_cred
        FROM source_scores WHERE task_id = $1
        """,
        task_id,
    )
    source_info = (
        f"Sources: {source_row['cnt']}, Avg credibility: {float(source_row['avg_cred'] or 0):.2f}"
    ) if source_row else ""

    # === Generate One-Liner ===
    one_liner = ""
    try:
        out1, _ = await spawn_model(
            task_type=TaskType.EXECUTIVE_SUMMARY,
            context={
                "task_id": task_id,
                "research_topic": research_topic,
                "compression_level": "one_liner",
                "evidence": evidence[:2000],
                "hypotheses": hypotheses,
            },
            output_schema=OneLinerOutput,
            db=db,
            cost_tracker=cost_tracker,
            config=config,
            quality_tier=quality_tier,
        )
        one_liner = out1.one_liner  # type: ignore[attr-defined]
    except Exception as exc:
        log.warning("one_liner_generation_failed", error=str(exc))

    # === Generate Paragraph Summary ===
    paragraph = ""
    try:
        out2, _ = await spawn_model(
            task_type=TaskType.EXECUTIVE_SUMMARY,
            context={
                "task_id": task_id,
                "research_topic": research_topic,
                "compression_level": "paragraph",
                "evidence": evidence[:4000],
                "hypotheses": hypotheses,
                "perspectives": perspectives[:2000],
                "unresolved_contradictions": contra_count or 0,
            },
            output_schema=ParagraphSummaryOutput,
            db=db,
            cost_tracker=cost_tracker,
            config=config,
            quality_tier=quality_tier,
        )
        paragraph = out2.summary  # type: ignore[attr-defined]
    except Exception as exc:
        log.warning("paragraph_generation_failed", error=str(exc))

    # === Generate Page Summary ===
    page_summary = ""
    try:
        out3, _ = await spawn_model(
            task_type=TaskType.EXECUTIVE_SUMMARY,
            context={
                "task_id": task_id,
                "research_topic": research_topic,
                "compression_level": "page",
                "evidence": evidence,
                "hypotheses": hypotheses,
                "perspectives": perspectives,
                "source_info": source_info,
                "unresolved_contradictions": contra_count or 0,
            },
            output_schema=PageSummaryOutput,
            db=db,
            cost_tracker=cost_tracker,
            config=config,
            quality_tier=quality_tier,
        )
        page_summary = out3.summary  # type: ignore[attr-defined]
    except Exception as exc:
        log.warning("page_summary_generation_failed", error=str(exc))

    # 6. Persist
    try:
        await db.execute(
            """
            INSERT INTO executive_summaries (
                task_id, one_liner, paragraph, page_summary,
                compression_metadata
            ) VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (task_id) DO UPDATE SET
                one_liner = EXCLUDED.one_liner,
                paragraph = EXCLUDED.paragraph,
                page_summary = EXCLUDED.page_summary,
                compression_metadata = EXCLUDED.compression_metadata,
                created_at = now()
            """,
            task_id,
            one_liner,
            paragraph,
            page_summary,
            json.dumps({
                "claims_count": len(claims_rows),
                "sources_count": source_row["cnt"] if source_row else 0,
                "unresolved_contradictions": contra_count or 0,
            }),
        )
    except Exception as exc:
        log.warning("executive_summary_persist_failed", error=str(exc))

    result = {
        "one_liner": one_liner,
        "paragraph": paragraph,
        "page_summary": page_summary,
        "claims_used": len(claims_rows),
    }

    log.info(
        "executive_summaries_generated",
        task_id=task_id,
        one_liner_len=len(one_liner),
        paragraph_len=len(paragraph),
        page_len=len(page_summary),
    )
    return result


async def get_executive_summary(task_id: str, db: Any) -> dict[str, Any] | None:
    """Get the executive summary for a task."""
    row = await db.fetchrow(
        "SELECT * FROM executive_summaries WHERE task_id = $1 ORDER BY created_at DESC LIMIT 1",
        task_id,
    )
    return dict(row) if row else None
