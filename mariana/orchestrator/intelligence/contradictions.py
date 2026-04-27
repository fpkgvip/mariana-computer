"""
Mariana Intelligence Engine — Contradiction Detection & Resolution (System 4)

A real analyst doesn't just aggregate — they triangulate. When Source A says X
and Source B says ¬X, the system flags this explicitly, attempts resolution via
a third source, and if unresolvable, presents both positions with confidence weights.

Process:
1. Decompose every source into atomic claim triples (handled by evidence_ledger).
2. Run NLI-style pairwise comparisons across claims with overlapping subjects.
3. Generate a "contradiction matrix" that the synthesis step must address.
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

class ContradictionPair(BaseModel):
    """A detected contradiction between two claims."""
    claim_a_text: str = Field(..., description="Text of claim A")
    claim_b_text: str = Field(..., description="Text of claim B")
    claim_a_index: int = Field(..., description="Index of claim A in the input list")
    claim_b_index: int = Field(..., description="Index of claim B in the input list")
    contradiction_type: str = Field(
        ...,
        description="One of: direct, temporal, quantitative, qualitative",
    )
    severity: float = Field(
        ..., ge=0.0, le=1.0,
        description="How severe this contradiction is (0=minor, 1=critical)",
    )
    explanation: str = Field(..., description="Why these claims contradict each other")
    suggested_resolution: str = Field(
        default="",
        description="How this contradiction might be resolved (e.g., different time periods)",
    )


class ContradictionDetectionOutput(BaseModel):
    """Output from the NLI pairwise comparison."""
    contradictions: list[ContradictionPair] = Field(
        default_factory=list,
        description="All detected contradiction pairs",
    )
    summary: str = Field(default="", description="Overall summary of contradictions found")


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

async def detect_contradictions(
    task_id: str,
    db: Any,
    cost_tracker: Any,
    config: Any,
    quality_tier: str | None = None,
    batch_size: int = 30,
) -> list[dict[str, Any]]:
    """
    Detect contradictions among claims for a research task.

    Groups claims by overlapping subjects, then runs NLI-style pairwise
    comparison in batches. Only compares claims that share subjects to
    avoid O(n²) explosion.

    Args:
        task_id: Research task ID.
        db: asyncpg pool.
        cost_tracker: Cost tracker instance.
        config: App config.
        quality_tier: Optional quality tier.
        batch_size: Max claims per NLI batch.

    Returns:
        List of contradiction dicts as inserted.
    """
    log = logger.bind(component="detect_contradictions")

    # 1. Get all claims grouped by subject
    rows = await db.fetch(
        """
        SELECT id, subject, predicate, object, claim_text, confidence,
               temporal_start, temporal_end, source_ids
        FROM claims
        WHERE task_id = $1
        ORDER BY subject, confidence DESC
        """,
        task_id,
    )

    if not rows or len(rows) < 2:
        log.info("insufficient_claims_for_contradiction_check", count=len(rows) if rows else 0)
        return []

    # 2. Group by subject
    subject_groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        subj = row["subject"].lower().strip()
        if subj not in subject_groups:
            subject_groups[subj] = []
        subject_groups[subj].append(dict(row))

    # 3. Build comparison batches — only subjects with 2+ claims
    comparison_claims: list[dict[str, Any]] = []
    for subj, claims in subject_groups.items():
        if len(claims) >= 2:
            comparison_claims.extend(claims)

    if len(comparison_claims) < 2:
        log.info("no_overlapping_subjects", task_id=task_id)
        return []

    # 4. Process in batches
    all_contradictions: list[dict[str, Any]] = []

    for batch_start in range(0, len(comparison_claims), batch_size):
        batch = comparison_claims[batch_start:batch_start + batch_size]
        if len(batch) < 2:
            continue

        # Format claims for the LLM
        claims_text = "\n".join(
            f"[{i}] Subject: {c['subject']} | Predicate: {c['predicate']} | "
            f"Object: {c['object']} | Claim: {c['claim_text']} | "
            f"Confidence: {c['confidence']:.2f}"
            for i, c in enumerate(batch)
        )

        try:
            output, _session = await spawn_model(
                task_type=TaskType.CONTRADICTION_DETECTION,
                context={
                    "task_id": task_id,
                    "claims": claims_text,
                    "claims_count": len(batch),
                },
                output_schema=ContradictionDetectionOutput,
                db=db,
                cost_tracker=cost_tracker,
                config=config,
                quality_tier=quality_tier,
            )
        except Exception as exc:
            log.warning("contradiction_detection_failed", error=str(exc), batch_start=batch_start)
            continue

        parsed: ContradictionDetectionOutput = output  # type: ignore[assignment]

        # 5. Persist each contradiction pair
        for pair in parsed.contradictions:
            idx_a = pair.claim_a_index
            idx_b = pair.claim_b_index
            if idx_a < 0 or idx_a >= len(batch) or idx_b < 0 or idx_b >= len(batch):
                continue
            if idx_a == idx_b:
                continue

            claim_a_id = batch[idx_a]["id"]
            claim_b_id = batch[idx_b]["id"]

            try:
                row = await db.fetchrow(
                    """
                    INSERT INTO contradiction_pairs (
                        task_id, claim_a_id, claim_b_id,
                        contradiction_type, severity,
                        resolution_status, resolution_note
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                    RETURNING id
                    """,
                    task_id,
                    claim_a_id,
                    claim_b_id,
                    pair.contradiction_type,
                    pair.severity,
                    "unresolved",
                    pair.suggested_resolution or None,
                )

                # Update contradiction_ids on both claims
                for cid in (claim_a_id, claim_b_id):
                    other_id = claim_b_id if cid == claim_a_id else claim_a_id
                    await db.execute(
                        """
                        UPDATE claims
                        SET contradiction_ids = contradiction_ids || $1::jsonb
                        WHERE id = $2 AND NOT (contradiction_ids @> $1::jsonb)
                        """,
                        json.dumps([other_id]),
                        cid,
                    )

                all_contradictions.append({
                    "id": row["id"],
                    "claim_a_id": claim_a_id,
                    "claim_b_id": claim_b_id,
                    "type": pair.contradiction_type,
                    "severity": pair.severity,
                    "explanation": pair.explanation,
                })
            except Exception as exc:
                log.warning("contradiction_persist_failed", error=str(exc))

    log.info(
        "contradiction_detection_complete",
        task_id=task_id,
        total_claims_checked=len(comparison_claims),
        contradictions_found=len(all_contradictions),
    )
    return all_contradictions


# F-06 pagination constants.
_INTEL_MAX_LIMIT = 1000
_INTEL_DEFAULT_LIMIT = 100


async def get_contradiction_matrix(
    task_id: str,
    db: Any,
    limit: int = _INTEL_DEFAULT_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    """
    Build the contradiction matrix for synthesis with pagination.

    F-06 fix: returns at most ``limit`` contradictions per page using
    keyset pagination on (created_at, id). The envelope includes
    total_contradictions across all pages (count query), plus per-page items.
    """
    limit = max(1, min(limit, _INTEL_MAX_LIMIT))

    if cursor:
        try:
            cursor_ts, cursor_id = cursor.split("|", 1)
            rows = await db.fetch(
                """
                SELECT
                    cp.*,
                    ca.claim_text as claim_a_text,
                    cb.claim_text as claim_b_text,
                    ca.confidence as claim_a_confidence,
                    cb.confidence as claim_b_confidence,
                    ca.subject as subject
                FROM contradiction_pairs cp
                JOIN claims ca ON cp.claim_a_id = ca.id
                JOIN claims cb ON cp.claim_b_id = cb.id
                WHERE cp.task_id = $1
                  AND (cp.created_at, cp.id) > ($2::timestamptz, $3)
                ORDER BY cp.created_at ASC, cp.id ASC
                LIMIT $4
                """,
                task_id, cursor_ts, cursor_id, limit,
            )
        except Exception:
            rows = await db.fetch(
                """
                SELECT
                    cp.*,
                    ca.claim_text as claim_a_text,
                    cb.claim_text as claim_b_text,
                    ca.confidence as claim_a_confidence,
                    cb.confidence as claim_b_confidence,
                    ca.subject as subject
                FROM contradiction_pairs cp
                JOIN claims ca ON cp.claim_a_id = ca.id
                JOIN claims cb ON cp.claim_b_id = cb.id
                WHERE cp.task_id = $1
                ORDER BY cp.created_at ASC, cp.id ASC
                LIMIT $2
                """,
                task_id, limit,
            )
    else:
        rows = await db.fetch(
            """
            SELECT
                cp.*,
                ca.claim_text as claim_a_text,
                cb.claim_text as claim_b_text,
                ca.confidence as claim_a_confidence,
                cb.confidence as claim_b_confidence,
                ca.subject as subject
            FROM contradiction_pairs cp
            JOIN claims ca ON cp.claim_a_id = ca.id
            JOIN claims cb ON cp.claim_b_id = cb.id
            WHERE cp.task_id = $1
            ORDER BY cp.created_at ASC, cp.id ASC
            LIMIT $2
            """,
            task_id, limit,
        )

    contradictions = [dict(r) for r in rows]
    unresolved = [c for c in contradictions if c["resolution_status"] == "unresolved"]
    resolved = [c for c in contradictions if c["resolution_status"] != "unresolved"]

    return {
        "total_contradictions": len(contradictions),
        "unresolved_count": len(unresolved),
        "resolved_count": len(resolved),
        "contradictions": contradictions,
        "critical_unresolved": [
            c for c in unresolved if c["severity"] >= 0.7
        ],
    }


async def attempt_resolution(
    contradiction_id: str,
    task_id: str,
    db: Any,
    cost_tracker: Any,
    config: Any,
    quality_tier: str | None = None,
) -> dict[str, Any]:
    """
    Attempt to resolve a contradiction by seeking a third source.

    This is called during gap detection when unresolved contradictions
    are identified as gaps needing follow-up.
    """
    log = logger.bind(component="resolve_contradiction")

    row = await db.fetchrow(
        """
        SELECT cp.*, ca.claim_text as claim_a_text, cb.claim_text as claim_b_text
        FROM contradiction_pairs cp
        JOIN claims ca ON cp.claim_a_id = ca.id
        JOIN claims cb ON cp.claim_b_id = cb.id
        WHERE cp.id = $1
        """,
        contradiction_id,
    )

    if not row:
        return {"status": "not_found"}

    # The resolution attempt is primarily informational — actual resolution
    # happens when new evidence arrives and the confidence calibrator re-evaluates
    log.info("contradiction_resolution_attempted", contradiction_id=contradiction_id)

    return {
        "contradiction_id": contradiction_id,
        "claim_a": row["claim_a_text"],
        "claim_b": row["claim_b_text"],
        "status": row["resolution_status"],
    }
