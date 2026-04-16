"""
Mariana Intelligence Engine — Temporal Reasoning Engine (System 10)

Many research questions have a time dimension. The system:
1. Tracks when each claim was true (temporal_start, temporal_end).
2. Detects temporal conflicts (same subject, different values, overlapping times).
3. Prefers recent data when recency matters, historical data when trends matter.

Temporal metadata is extracted during claim extraction (evidence_ledger module).
This module handles temporal conflict detection and reasoning.
"""

from __future__ import annotations

import structlog
from datetime import datetime, timezone
from typing import Any

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Temporal conflict detection
# ---------------------------------------------------------------------------

async def detect_temporal_conflicts(
    task_id: str,
    db: Any,
) -> list[dict[str, Any]]:
    """
    Detect temporal conflicts: same subject + predicate but different objects
    at overlapping time periods.

    Examples of temporal conflicts:
    - "Company X had 100 employees" (2023) vs "Company X had 100 employees" (2025)
      → NOT a conflict if different time periods
    - "Company X revenue was $10B" (Q1 2025) vs "Company X revenue was $15B" (Q1 2025)
      → IS a conflict (same time period, different values)

    Returns list of conflict dicts with the conflicting claim pairs.
    """
    log = logger.bind(component="temporal_conflicts")

    # Find claims that share subject+predicate but have different objects
    # and overlapping temporal ranges
    rows = await db.fetch(
        """
        SELECT
            a.id as claim_a_id,
            b.id as claim_b_id,
            a.subject,
            a.predicate,
            a.object as object_a,
            b.object as object_b,
            a.claim_text as claim_a_text,
            b.claim_text as claim_b_text,
            a.temporal_start as a_start,
            a.temporal_end as a_end,
            b.temporal_start as b_start,
            b.temporal_end as b_end,
            a.confidence as conf_a,
            b.confidence as conf_b
        FROM claims a
        JOIN claims b ON
            a.task_id = b.task_id
            AND LOWER(a.subject) = LOWER(b.subject)
            AND LOWER(a.predicate) = LOWER(b.predicate)
            AND LOWER(a.object) != LOWER(b.object)
            AND a.id < b.id  -- avoid duplicates
        WHERE a.task_id = $1
          AND a.temporal_start IS NOT NULL
          AND b.temporal_start IS NOT NULL
        """,
        task_id,
    )

    conflicts: list[dict[str, Any]] = []
    for row in rows:
        # Check temporal overlap
        a_start = row["a_start"]
        a_end = row["a_end"] or datetime.now(timezone.utc)
        b_start = row["b_start"]
        b_end = row["b_end"] or datetime.now(timezone.utc)

        # Two intervals overlap if a_start < b_end AND b_start < a_end
        if a_start < b_end and b_start < a_end:
            conflict = {
                "claim_a_id": row["claim_a_id"],
                "claim_b_id": row["claim_b_id"],
                "subject": row["subject"],
                "predicate": row["predicate"],
                "object_a": row["object_a"],
                "object_b": row["object_b"],
                "claim_a_text": row["claim_a_text"],
                "claim_b_text": row["claim_b_text"],
                "overlap_start": max(a_start, b_start).isoformat(),
                "overlap_end": min(a_end, b_end).isoformat(),
                "confidence_a": float(row["conf_a"]),
                "confidence_b": float(row["conf_b"]),
                "preferred_claim": row["claim_a_id"] if row["conf_a"] >= row["conf_b"] else row["claim_b_id"],
            }
            conflicts.append(conflict)

    log.info("temporal_conflicts_detected", task_id=task_id, count=len(conflicts))
    return conflicts


async def get_temporal_timeline(
    task_id: str,
    subject: str,
    db: Any,
) -> list[dict[str, Any]]:
    """
    Build a chronological timeline for a specific subject.

    Returns claims ordered by temporal_start, useful for trend analysis.
    """
    rows = await db.fetch(
        """
        SELECT id, subject, predicate, object, claim_text,
               confidence, temporal_start, temporal_end, temporal_type
        FROM claims
        WHERE task_id = $1 AND LOWER(subject) = LOWER($2)
          AND temporal_start IS NOT NULL
        ORDER BY temporal_start ASC
        """,
        task_id,
        subject,
    )
    return [dict(r) for r in rows]


async def get_temporal_coverage(task_id: str, db: Any) -> dict[str, Any]:
    """
    Assess temporal coverage of the evidence ledger.

    Returns statistics about how well the evidence covers different time periods.
    """
    row = await db.fetchrow(
        """
        SELECT
            COUNT(*) as total_claims,
            COUNT(*) FILTER (WHERE temporal_start IS NOT NULL) as temporally_tagged,
            MIN(temporal_start) as earliest,
            MAX(temporal_start) as latest,
            COUNT(DISTINCT subject) as subjects_with_temporal
        FROM claims
        WHERE task_id = $1
        """,
        task_id,
    )

    if not row or row["total_claims"] == 0:
        return {"coverage_ratio": 0.0, "total": 0, "tagged": 0}

    return {
        "total_claims": row["total_claims"],
        "temporally_tagged": row["temporally_tagged"],
        "coverage_ratio": row["temporally_tagged"] / row["total_claims"] if row["total_claims"] > 0 else 0,
        "earliest": row["earliest"].isoformat() if row["earliest"] else None,
        "latest": row["latest"].isoformat() if row["latest"] else None,
        "subjects_with_temporal": row["subjects_with_temporal"],
    }


def select_preferred_claims(
    claims: list[dict[str, Any]],
    prefer_recent: bool = True,
) -> list[dict[str, Any]]:
    """
    When multiple claims conflict, select the preferred ones based on temporal strategy.

    Args:
        claims: List of claim dicts with temporal_start and confidence.
        prefer_recent: If True, prefer more recent claims. If False, prefer for trends.

    Returns:
        Filtered list with duplicates resolved.
    """
    if not claims:
        return []

    # Group by subject+predicate
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in claims:
        key = f"{c.get('subject', '').lower()}|{c.get('predicate', '').lower()}"
        if key not in groups:
            groups[key] = []
        groups[key].append(c)

    preferred: list[dict[str, Any]] = []
    for key, group in groups.items():
        if len(group) == 1:
            preferred.append(group[0])
            continue

        # Sort: by temporal_start descending (most recent first) if prefer_recent,
        # or ascending (oldest first) for trends
        sorted_group = sorted(
            group,
            key=lambda x: (x.get("temporal_start") or datetime.min.replace(tzinfo=timezone.utc)),
            reverse=prefer_recent,
        )

        # Take the one with highest confidence among the top temporal candidates
        best = max(sorted_group[:3], key=lambda x: x.get("confidence", 0))
        preferred.append(best)

    return preferred
