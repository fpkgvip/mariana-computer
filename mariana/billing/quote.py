"""Pre-flight quote estimator.

Given a user prompt + chosen tier, returns a credit range and ETA. The
estimator is *not* a contract — the contract is the credit ceiling the user
sets in the Pre-flight card. This is purely advisory.

Heuristics (v1.0):
  - tier multipliers calibrated against historical run averages (placeholder
    constants until we backfill from `events` rollups).
  - prompt complexity = log-scaled token count + tool-presence boost.
  - response is always a *range* (min/max) because LLM cost has variance.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, asdict
from typing import Any, Literal

ModelTier = Literal["lite", "standard", "max"]

# Calibrated baseline credits per tier (1 credit == $0.01).
# Source: tech-landscape.md margin model, p50 historical run.
_TIER_BASELINE: dict[ModelTier, int] = {
    "lite": 60,        # ~$0.60 baseline
    "standard": 220,   # ~$2.20 baseline
    "max": 700,        # ~$7.00 baseline
}

# Variance multiplier (max = baseline * (1 + variance)).
_TIER_VARIANCE: dict[ModelTier, float] = {
    "lite": 0.6,
    "standard": 0.7,
    "max": 0.85,
}

# ETA in seconds for a baseline-cost run.
_TIER_ETA_S: dict[ModelTier, tuple[int, int]] = {
    "lite": (60, 180),
    "standard": (180, 420),
    "max": (300, 900),
}


@dataclass(frozen=True)
class Quote:
    tier: ModelTier
    credits_min: int
    credits_max: int
    eta_seconds_min: int
    eta_seconds_max: int
    complexity_score: float
    breakdown: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _complexity(prompt: str) -> float:
    """Cheap, deterministic complexity estimate in [0.5, 3.0]."""
    if not prompt:
        return 1.0
    # Rough token count: 1 token ~= 4 chars.
    token_count = max(1, len(prompt) // 4)
    base = 0.5 + math.log10(token_count + 1)  # 1 token -> 0.8, 1000 tokens -> 3.5
    # Boost for tool / build / research keywords
    boost = 0.0
    if re.search(r"\b(build|create|implement|write code|deploy)\b", prompt, re.I):
        boost += 0.4
    if re.search(r"\b(research|analyze|compare|investigate|deep dive)\b", prompt, re.I):
        boost += 0.3
    if re.search(r"\b(test|qa|fix bugs?|debug)\b", prompt, re.I):
        boost += 0.2
    if re.search(r"https?://", prompt):
        boost += 0.1
    return max(0.5, min(3.0, base + boost))


def estimate_quote(
    *,
    prompt: str,
    tier: ModelTier = "standard",
    max_credits: int | None = None,
) -> Quote:
    """Return a credit + ETA quote for ``prompt`` at ``tier``.

    ``max_credits`` is the user-specified ceiling; we return the smaller of
    the heuristic max and the ceiling so the UI can display "capped at $X".
    """
    if tier not in _TIER_BASELINE:
        raise ValueError(f"invalid tier: {tier!r}")

    cx = _complexity(prompt)
    baseline = _TIER_BASELINE[tier]
    variance = _TIER_VARIANCE[tier]
    eta_lo, eta_hi = _TIER_ETA_S[tier]

    # Min: 0.6 * baseline * complexity-discounted; Max: baseline * (1+variance) * complexity.
    credits_min = max(1, int(round(baseline * 0.6 * cx)))
    credits_max = int(round(baseline * (1 + variance) * cx))

    if max_credits is not None and max_credits > 0:
        credits_max = min(credits_max, int(max_credits))
        credits_min = min(credits_min, credits_max)

    eta_min = int(round(eta_lo * cx))
    eta_max = int(round(eta_hi * cx))

    return Quote(
        tier=tier,
        credits_min=credits_min,
        credits_max=credits_max,
        eta_seconds_min=eta_min,
        eta_seconds_max=eta_max,
        complexity_score=round(cx, 3),
        breakdown={
            "tier_baseline_credits": baseline,
            "tier_variance": variance,
            "complexity_score": round(cx, 3),
            "ceiling_applied": max_credits,
        },
    )
