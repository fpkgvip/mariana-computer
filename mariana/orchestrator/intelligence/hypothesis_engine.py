"""
Mariana Intelligence Engine — Hypothesis Generation & Testing (System 8)

Before diving into research, the system generates competing hypotheses.
Then it actively seeks evidence for and against each hypothesis using
Bayesian updating:

1. Generate 3-5 candidate hypotheses.
2. Assign prior probabilities (uniform 1/n by default).
3. Update posteriors as evidence arrives: P(H|E) ∝ P(E|H) * P(H)
4. Report the winning hypothesis with its evidence chain.
"""

from __future__ import annotations

import json
import structlog
import math
from typing import Any

from pydantic import BaseModel, Field

from mariana.ai.session import spawn_model
from mariana.data.models import TaskType

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class LikelihoodEstimate(BaseModel):
    """LLM's estimate of how likely evidence is under a hypothesis."""
    hypothesis_id: str = Field(..., description="Hypothesis being evaluated")
    likelihood_given_h: float = Field(
        ..., ge=0.01, le=0.99,
        description="P(evidence | hypothesis is true) — how expected is this evidence if H is true?",
    )
    likelihood_given_not_h: float = Field(
        ..., ge=0.01, le=0.99,
        description="P(evidence | hypothesis is false) — how expected is this evidence if H is false?",
    )
    reasoning: str = Field(..., description="Brief explanation for the likelihood estimates")


class BayesianUpdateOutput(BaseModel):
    """Output from the Bayesian likelihood estimation call."""
    estimates: list[LikelihoodEstimate] = Field(
        default_factory=list,
        description="Likelihood estimates for each hypothesis",
    )


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

async def initialize_priors(
    task_id: str,
    hypothesis_ids: list[str],
    db: Any,
) -> dict[str, float]:
    """
    Initialize uniform priors for all hypotheses.

    Each hypothesis starts with P(H) = 1/n where n is the number of hypotheses.

    Returns dict of hypothesis_id → prior probability.
    """
    log = logger.bind(component="init_priors")

    if not hypothesis_ids:
        return {}

    n = len(hypothesis_ids)
    uniform_prior = 1.0 / n

    priors: dict[str, float] = {}
    for hid in hypothesis_ids:
        try:
            await db.execute(
                """
                INSERT INTO hypothesis_priors (task_id, hypothesis_id, prior, posterior)
                VALUES ($1, $2, $3, $3)
                ON CONFLICT (task_id, hypothesis_id) DO NOTHING
                """,
                task_id,
                hid,
                uniform_prior,
            )
            priors[hid] = uniform_prior
        except Exception as exc:
            log.warning("prior_init_failed", hypothesis_id=hid, error=str(exc))

    log.info("priors_initialized", task_id=task_id, hypotheses=n, prior=f"{uniform_prior:.4f}")
    return priors


async def bayesian_update(
    task_id: str,
    claim_id: str,
    claim_text: str,
    db: Any,
    cost_tracker: Any,
    config: Any,
    quality_tier: str | None = None,
) -> dict[str, float]:
    """
    Update hypothesis posteriors given new evidence (a claim).

    Uses an LLM to estimate likelihood ratios P(E|H) / P(E|¬H) for each
    hypothesis, then applies Bayes' theorem.

    Args:
        task_id: Research task ID.
        claim_id: ID of the new claim (evidence).
        claim_text: Text of the claim.
        db: asyncpg pool.
        cost_tracker: Cost tracker.
        config: App config.
        quality_tier: Optional quality tier.

    Returns:
        Dict of hypothesis_id → updated posterior.
    """
    log = logger.bind(component="bayesian_update")

    # Get all hypotheses with their current posteriors
    rows = await db.fetch(
        """
        SELECT hp.hypothesis_id, hp.posterior, h.statement
        FROM hypothesis_priors hp
        JOIN hypotheses h ON hp.hypothesis_id = h.id
        WHERE hp.task_id = $1
        """,
        task_id,
    )

    if not rows:
        return {}

    hypotheses = [
        {
            "id": r["hypothesis_id"],
            "posterior": float(r["posterior"]),
            "statement": r["statement"],
        }
        for r in rows
    ]

    # Build context for LLM
    hyp_text = "\n".join(
        f"[{h['id']}] {h['statement']} (current P={h['posterior']:.4f})"
        for h in hypotheses
    )

    try:
        output, _session = await spawn_model(
            task_type=TaskType.BAYESIAN_UPDATE,
            context={
                "task_id": task_id,
                "claim_text": claim_text,
                "hypotheses": hyp_text,
                "hypotheses_count": len(hypotheses),
            },
            output_schema=BayesianUpdateOutput,
            db=db,
            cost_tracker=cost_tracker,
            config=config,
            quality_tier=quality_tier,
        )
    except Exception as exc:
        log.warning("bayesian_update_llm_failed", error=str(exc))
        return {h["id"]: h["posterior"] for h in hypotheses}

    parsed: BayesianUpdateOutput = output  # type: ignore[assignment]

    # Build lookup for LLM estimates
    estimates_by_id: dict[str, LikelihoodEstimate] = {
        e.hypothesis_id: e for e in parsed.estimates
    }

    # Apply Bayes' theorem
    updated_posteriors: dict[str, float] = {}
    unnormalized: dict[str, float] = {}

    for h in hypotheses:
        est = estimates_by_id.get(h["id"])
        if est is None:
            # No estimate for this hypothesis — keep current posterior
            unnormalized[h["id"]] = h["posterior"]
            continue

        # Bayes: P(H|E) ∝ P(E|H) * P(H)
        likelihood_ratio = est.likelihood_given_h / max(est.likelihood_given_not_h, 0.01)
        new_unnorm = h["posterior"] * likelihood_ratio
        unnormalized[h["id"]] = new_unnorm

    # Normalize so posteriors sum to 1
    total = sum(unnormalized.values())
    if total > 0:
        for hid, val in unnormalized.items():
            updated_posteriors[hid] = val / total
    else:
        # Fallback to uniform
        n = len(hypotheses)
        for h in hypotheses:
            updated_posteriors[h["id"]] = 1.0 / n

    # Persist updated posteriors
    for hid, posterior in updated_posteriors.items():
        est = estimates_by_id.get(hid)
        update_entry = {
            "claim_id": claim_id,
            "posterior_after": posterior,
            "likelihood_ratio": (est.likelihood_given_h / max(est.likelihood_given_not_h, 0.01))
            if est else 1.0,
        }
        try:
            await db.execute(
                """
                UPDATE hypothesis_priors
                SET posterior = $1,
                    evidence_updates = evidence_updates || $2::jsonb,
                    last_updated = now()
                WHERE task_id = $3 AND hypothesis_id = $4
                """,
                posterior,
                json.dumps([update_entry]),
                task_id,
                hid,
            )
        except Exception as exc:
            log.warning("posterior_persist_failed", hypothesis_id=hid, error=str(exc))

    log.info(
        "bayesian_update_complete",
        task_id=task_id,
        claim_id=claim_id,
        posteriors={hid: f"{p:.4f}" for hid, p in updated_posteriors.items()},
    )
    return updated_posteriors


async def get_hypothesis_rankings(task_id: str, db: Any) -> list[dict[str, Any]]:
    """
    Get all hypotheses ranked by posterior probability.

    Returns list ordered by posterior descending, with evidence chain summaries.
    """
    rows = await db.fetch(
        """
        SELECT
            hp.hypothesis_id, hp.prior, hp.posterior, hp.evidence_updates,
            h.statement, h.status, h.score
        FROM hypothesis_priors hp
        JOIN hypotheses h ON hp.hypothesis_id = h.id
        WHERE hp.task_id = $1
        ORDER BY hp.posterior DESC
        """,
        task_id,
    )

    rankings = []
    for r in rows:
        evidence_updates = r["evidence_updates"]
        if isinstance(evidence_updates, str):
            try:
                evidence_updates = json.loads(evidence_updates)
            except Exception:
                evidence_updates = []

        rankings.append({
            "hypothesis_id": r["hypothesis_id"],
            "statement": r["statement"],
            "prior": float(r["prior"]),
            "posterior": float(r["posterior"]),
            "status": r["status"],
            "branch_score": float(r["score"]) if r["score"] else None,
            "evidence_count": len(evidence_updates) if isinstance(evidence_updates, list) else 0,
            "posterior_change": float(r["posterior"]) - float(r["prior"]),
        })

    return rankings


async def get_winning_hypothesis(task_id: str, db: Any) -> dict[str, Any] | None:
    """Get the hypothesis with the highest posterior probability."""
    rankings = await get_hypothesis_rankings(task_id, db)
    return rankings[0] if rankings else None
