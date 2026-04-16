"""
Mariana Intelligence Engine — Claim Extraction & Evidence Ledger (System 6)

Every piece of text the system reads is decomposed into discrete, atomic claims.
Claims are stored as structured (Subject, Predicate, Object) triples in the evidence
ledger. The final report is synthesized from this ledger, not from raw text.

This forces the system to reason over structured knowledge rather than vibes.
"""

from __future__ import annotations

import json
import structlog
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from mariana.ai.session import spawn_model
from mariana.data.models import AISession, TaskType

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Pydantic output schemas for LLM extraction
# ---------------------------------------------------------------------------

class ExtractedClaim(BaseModel):
    """A single atomic claim extracted from a finding."""
    subject: str = Field(..., description="Entity or concept (e.g., 'Apple Inc.')")
    predicate: str = Field(..., description="Relationship or attribute (e.g., 'revenue_was')")
    object: str = Field(..., description="Value or target (e.g., '$394B in FY2023')")
    claim_text: str = Field(..., description="Human-readable statement of the claim")
    confidence: float = Field(default=0.7, ge=0.0, le=1.0, description="Extraction confidence")
    temporal_start: str | None = Field(default=None, description="ISO timestamp when claim became true, or null")
    temporal_end: str | None = Field(default=None, description="ISO timestamp when claim stopped being true, or null")
    temporal_type: str = Field(default="point", description="point, range, or ongoing")


class ClaimExtractionOutput(BaseModel):
    """Structured output from the claim extraction LLM call."""
    claims: list[ExtractedClaim] = Field(default_factory=list, description="Extracted atomic claims")
    extraction_notes: str = Field(default="", description="Notes about extraction quality or ambiguity")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_temporal(value: str | None) -> datetime | None:
    """Convert an LLM-returned temporal string to a datetime object for asyncpg."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value if isinstance(value, datetime) else datetime(value.year, value.month, value.day)
    s = str(value).strip()
    if not s:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%Y-%m",
        "%Y",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

async def extract_claims_from_finding(
    finding_id: str,
    finding_content: str,
    hypothesis_statement: str,
    task_id: str,
    source_ids: list[str],
    db: Any,
    cost_tracker: Any,
    config: Any,
    quality_tier: str | None = None,
) -> list[dict[str, Any]]:
    """
    Decompose a finding into atomic claims using an LLM.

    Each claim is stored as a (Subject, Predicate, Object) triple in the claims
    table and returned as a list of dicts.

    Args:
        finding_id: ID of the finding to decompose.
        finding_content: Raw text content of the finding.
        hypothesis_statement: The hypothesis this finding relates to.
        task_id: Parent research task ID.
        source_ids: Source IDs backing this finding.
        db: asyncpg connection pool.
        cost_tracker: CostTracker instance.
        config: AppConfig instance.
        quality_tier: Optional quality tier override.

    Returns:
        List of claim dicts as inserted into the DB.
    """
    log = logger.bind(component="extract_claims")

    context: dict[str, Any] = {
        "task_id": task_id,
        "finding_id": finding_id,
        "finding_content": finding_content[:4000],  # Cap to avoid context overflow
        "hypothesis_statement": hypothesis_statement,
    }

    try:
        output, _session = await spawn_model(
            task_type=TaskType.CLAIM_EXTRACTION,
            context=context,
            output_schema=ClaimExtractionOutput,
            db=db,
            cost_tracker=cost_tracker,
            config=config,
            quality_tier=quality_tier,
        )
    except Exception as exc:
        log.warning("claim_extraction_llm_failed", error=str(exc), finding_id=finding_id)
        return []

    parsed: ClaimExtractionOutput = output  # type: ignore[assignment]
    inserted: list[dict[str, Any]] = []

    for claim in parsed.claims:
        try:
            # Look up hypothesis_id from finding
            hyp_row = await db.fetchrow(
                "SELECT hypothesis_id FROM findings WHERE id = $1",
                finding_id,
            )
            hypothesis_id = hyp_row["hypothesis_id"] if hyp_row else None

            row = await db.fetchrow(
                """
                INSERT INTO claims (
                    task_id, finding_id, hypothesis_id,
                    subject, predicate, object, claim_text,
                    source_ids, confidence,
                    temporal_start, temporal_end, temporal_type
                ) VALUES (
                    $1, $2, $3,
                    $4, $5, $6, $7,
                    $8, $9,
                    $10::timestamptz, $11::timestamptz, $12
                )
                RETURNING id
                """,
                task_id,
                finding_id,
                hypothesis_id,
                claim.subject,
                claim.predicate,
                claim.object,
                claim.claim_text,
                json.dumps(source_ids),
                claim.confidence,
                _parse_temporal(claim.temporal_start),
                _parse_temporal(claim.temporal_end),
                claim.temporal_type,
            )
            inserted.append({
                "id": row["id"],
                "subject": claim.subject,
                "predicate": claim.predicate,
                "object": claim.object,
                "claim_text": claim.claim_text,
                "confidence": claim.confidence,
                "temporal_type": claim.temporal_type,
            })
        except Exception as exc:
            log.warning("claim_insert_failed", error=str(exc), claim_text=claim.claim_text[:100])

    log.info(
        "claims_extracted",
        finding_id=finding_id,
        claims_count=len(inserted),
        task_id=task_id,
    )
    return inserted


async def get_evidence_ledger(task_id: str, db: Any) -> list[dict[str, Any]]:
    """
    Retrieve the full evidence ledger (all claims) for a research task.

    Returns claims ordered by confidence descending.
    """
    rows = await db.fetch(
        """
        SELECT c.*, f.content as finding_content, h.statement as hypothesis_statement
        FROM claims c
        LEFT JOIN findings f ON c.finding_id = f.id
        LEFT JOIN hypotheses h ON c.hypothesis_id = h.id
        WHERE c.task_id = $1
        ORDER BY c.confidence DESC
        """,
        task_id,
    )
    return [dict(r) for r in rows]


async def get_claims_by_subject(task_id: str, subject: str, db: Any) -> list[dict[str, Any]]:
    """Retrieve all claims about a specific subject entity."""
    rows = await db.fetch(
        """
        SELECT * FROM claims
        WHERE task_id = $1 AND LOWER(subject) = LOWER($2)
        ORDER BY confidence DESC
        """,
        task_id,
        subject,
    )
    return [dict(r) for r in rows]


async def get_claims_for_hypothesis(task_id: str, hypothesis_id: str, db: Any) -> list[dict[str, Any]]:
    """Retrieve all claims linked to a specific hypothesis."""
    rows = await db.fetch(
        """
        SELECT * FROM claims
        WHERE task_id = $1 AND hypothesis_id = $2
        ORDER BY confidence DESC
        """,
        task_id,
        hypothesis_id,
    )
    return [dict(r) for r in rows]


async def get_ledger_summary(task_id: str, db: Any) -> dict[str, Any]:
    """Get aggregate statistics about the evidence ledger."""
    row = await db.fetchrow(
        """
        SELECT
            COUNT(*) as total_claims,
            COUNT(DISTINCT subject) as unique_subjects,
            AVG(confidence) as avg_confidence,
            COUNT(*) FILTER (WHERE corroboration_count > 0) as corroborated_claims,
            COUNT(*) FILTER (WHERE jsonb_array_length(COALESCE(contradiction_ids, '[]'::jsonb)) > 0) as contradicted_claims,
            COUNT(*) FILTER (WHERE temporal_start IS NOT NULL) as temporally_tagged
        FROM claims
        WHERE task_id = $1
        """,
        task_id,
    )
    if not row:
        return {"total_claims": 0, "unique_subjects": 0, "avg_confidence": 0.0}
    result = dict(row)
    # Ensure numeric values are JSON-serializable
    if result.get("avg_confidence") is not None:
        result["avg_confidence"] = float(result["avg_confidence"])
    return result
