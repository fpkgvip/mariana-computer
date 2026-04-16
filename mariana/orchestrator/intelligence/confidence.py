"""
Mariana Intelligence Engine — Confidence Calibration Layer (System 7)

Each claim in the evidence ledger gets a calibrated confidence score.
This isn't just LLM logprobs — it's a composite of:
    - Source credibility (from credibility engine)
    - Corroboration count (how many independent sources confirm)
    - Recency (how fresh the data is)
    - Internal consistency (1 - contradiction ratio)

An analyst who says "I'm 60% sure" is more useful than one who says
everything with equal conviction.
"""

from __future__ import annotations

import structlog
from typing import Any

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Calibration weights
# ---------------------------------------------------------------------------

# These weights sum to 1.0 and can be tuned per domain.
W_CREDIBILITY = 0.30      # Source authority
W_CORROBORATION = 0.30    # Independent confirmation
W_RECENCY = 0.20          # Freshness of data
W_CONSISTENCY = 0.20      # Absence of contradictions


# ---------------------------------------------------------------------------
# Core calibration function
# ---------------------------------------------------------------------------

def compute_calibrated_confidence(
    base_confidence: float,
    source_credibility: float,
    corroboration_count: int,
    total_related_claims: int,
    contradiction_count: int,
    recency_score: float,
    max_corroboration: int = 5,
) -> float:
    """
    Compute a calibrated confidence score for a claim.

    Pure algorithmic — no LLM call needed.

    Args:
        base_confidence: Original extraction confidence [0, 1].
        source_credibility: Composite source score from credibility engine [0, 1].
        corroboration_count: Number of independent sources confirming this claim.
        total_related_claims: Total claims about the same subject.
        contradiction_count: Number of contradicting claims.
        recency_score: Recency decay score from credibility engine [0, 1].
        max_corroboration: Cap for corroboration normalization.

    Returns:
        Calibrated confidence score [0, 1].
    """
    # Corroboration ratio: normalized to [0, 1] with diminishing returns
    corroboration_ratio = min(corroboration_count / max_corroboration, 1.0)

    # Consistency: 1 - (contradictions / total_related), floored at 0
    if total_related_claims > 0:
        consistency = max(0.0, 1.0 - (contradiction_count / total_related_claims))
    else:
        consistency = 0.8  # neutral when no other claims exist

    # Weighted composite
    calibrated = (
        W_CREDIBILITY * source_credibility
        + W_CORROBORATION * corroboration_ratio
        + W_RECENCY * recency_score
        + W_CONSISTENCY * consistency
    )

    # Blend with base confidence (50/50) to avoid completely overriding LLM judgment
    final = 0.5 * base_confidence + 0.5 * calibrated

    # Clamp to [0, 1]
    return max(0.0, min(1.0, final))


async def calibrate_claim(
    claim_id: str,
    task_id: str,
    db: Any,
) -> float:
    """
    Recalibrate a single claim's confidence using all available signals.

    Fetches credibility scores, corroboration data, and contradiction counts
    from the database, then computes and persists the calibrated confidence.

    Returns the new confidence score.
    """
    log = logger.bind(component="calibrate_claim")

    # Fetch claim
    claim = await db.fetchrow("SELECT * FROM claims WHERE id = $1", claim_id)
    if not claim:
        return 0.5

    # Get source credibility (average of all sources backing this claim)
    import json as _json
    source_ids = claim["source_ids"]
    if isinstance(source_ids, str):
        try:
            source_ids = _json.loads(source_ids)
        except Exception:
            source_ids = []

    source_credibility = 0.5
    if source_ids:
        cred_row = await db.fetchrow(
            """
            SELECT AVG(composite_score) as avg_cred
            FROM source_scores
            WHERE source_id = ANY($1::text[]) AND task_id = $2
            """,
            source_ids,
            task_id,
        )
        if cred_row and cred_row["avg_cred"] is not None:
            source_credibility = float(cred_row["avg_cred"])

    # Count corroborations (other claims with same subject+predicate)
    corr_row = await db.fetchrow(
        """
        SELECT COUNT(*) as cnt
        FROM claims
        WHERE task_id = $1
          AND LOWER(subject) = LOWER($2)
          AND LOWER(predicate) = LOWER($3)
          AND id != $4
        """,
        task_id,
        claim["subject"],
        claim["predicate"],
        claim_id,
    )
    corroboration_count = int(corr_row["cnt"]) if corr_row else 0

    # Count contradictions
    contradiction_ids = claim["contradiction_ids"]
    if isinstance(contradiction_ids, str):
        try:
            contradiction_ids = _json.loads(contradiction_ids)
        except Exception:
            contradiction_ids = []
    contradiction_count = len(contradiction_ids) if isinstance(contradiction_ids, list) else 0

    # Total related claims (same subject)
    total_row = await db.fetchrow(
        """
        SELECT COUNT(*) as cnt
        FROM claims
        WHERE task_id = $1 AND LOWER(subject) = LOWER($2)
        """,
        task_id,
        claim["subject"],
    )
    total_related = int(total_row["cnt"]) if total_row else 1

    # Recency score (from source_scores)
    recency = 0.7  # default
    if source_ids:
        rec_row = await db.fetchrow(
            """
            SELECT AVG(recency) as avg_rec
            FROM source_scores
            WHERE source_id = ANY($1::text[]) AND task_id = $2
            """,
            source_ids,
            task_id,
        )
        if rec_row and rec_row["avg_rec"] is not None:
            recency = float(rec_row["avg_rec"])

    # Compute
    new_confidence = compute_calibrated_confidence(
        base_confidence=float(claim["confidence"]),
        source_credibility=source_credibility,
        corroboration_count=corroboration_count,
        total_related_claims=total_related,
        contradiction_count=contradiction_count,
        recency_score=recency,
    )

    # Persist
    try:
        await db.execute(
            """
            UPDATE claims
            SET confidence = $1, credibility_score = $2, corroboration_count = $3
            WHERE id = $4
            """,
            new_confidence,
            source_credibility,
            corroboration_count,
            claim_id,
        )
    except Exception as exc:
        log.warning("calibration_persist_failed", error=str(exc), claim_id=claim_id)

    return new_confidence


async def calibrate_all_claims(task_id: str, db: Any) -> dict[str, Any]:
    """
    Recalibrate all claims for a task.

    Returns summary statistics.
    """
    log = logger.bind(component="calibrate_all")

    rows = await db.fetch(
        "SELECT id FROM claims WHERE task_id = $1",
        task_id,
    )

    if not rows:
        return {"calibrated": 0, "avg_confidence": 0.0}

    total = 0.0
    count = 0
    for row in rows:
        conf = await calibrate_claim(row["id"], task_id, db)
        total += conf
        count += 1

    avg = total / count if count > 0 else 0.0
    log.info("calibration_complete", task_id=task_id, claims=count, avg_confidence=f"{avg:.3f}")

    return {
        "calibrated": count,
        "avg_confidence": avg,
    }
