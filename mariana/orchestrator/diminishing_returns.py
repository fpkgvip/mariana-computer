"""
mariana/orchestrator/diminishing_returns.py

Deterministic diminishing-returns detection.

This module contains NO AI calls.  All logic is pure arithmetic over
already-computed values from the branch and task state.

The algorithm:

1. Compute novelty: fraction of findings that are *new* vs. total.
2. Count new sources discovered this cycle.
3. Compute score delta from the two most recent branch scores.
4. If all three are below their respective thresholds, increment the
   task's ``diminishing_flags`` counter; otherwise reset it to zero.
5. Map the counter to a :class:`DiminishingRecommendation`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from mariana.data.models import (
    Branch,
    DiminishingRecommendation,
    ResearchTask,
)

if TYPE_CHECKING:
    # AppConfig is built in a parallel subagent; import only for type hints.
    pass

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Thresholds (mirrors AppConfig defaults)
# ---------------------------------------------------------------------------

_NOVELTY_THRESHOLD: float = 0.10
"""Below this novelty fraction the dimension is considered stale."""

_NEW_SOURCES_THRESHOLD: int = 3
"""Fewer new sources than this across a cycle signals staleness."""

_SCORE_DELTA_THRESHOLD: float = 1.0
"""Score improvement below this value signals stagnation."""

_FLAG_PIVOT_THRESHOLD: int = 2
"""Flags at or above this level → PIVOT recommendation."""

_FLAG_HALT_THRESHOLD: int = 3
"""Flags at or above this level → HALT recommendation."""


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------


@dataclass
class DiminishingResult:
    """Output of a single diminishing-returns check.

    Attributes
    ----------
    novelty:
        Fraction of findings that are new relative to total findings
        (0.0–1.0).
    new_sources:
        Count of source records added during the most recent cycle.
    score_delta:
        Absolute difference between the two most recent branch scores.
        Defaults to 10.0 when fewer than two data points exist (so the
        first cycle never trips the threshold).
    flag_triggered:
        True if all three metrics fell below their thresholds in this
        cycle, causing ``task.diminishing_flags`` to be incremented.
    recommendation:
        Recommended next action for the orchestrator.
    """

    novelty: float
    new_sources: int
    score_delta: float
    flag_triggered: bool
    recommendation: DiminishingRecommendation


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_diminishing_returns(
    branch: Branch,
    findings_before: int,
    findings_after: int,
    sources_before: int,
    sources_after: int,
    task: ResearchTask,
    config: Any,  # AppConfig — typed as Any to avoid import-time dependency
) -> DiminishingResult:
    """Evaluate whether research output is showing diminishing returns.

    This function is *pure* with the exception of mutating
    ``task.diminishing_flags``, which the caller is expected to persist.

    Parameters
    ----------
    branch:
        The branch that just completed a research cycle.
    findings_before:
        Total finding count *before* this cycle ran.
    findings_after:
        Total finding count *after* this cycle ran.
    sources_before:
        Total source count *before* this cycle ran.
    sources_after:
        Total source count *after* this cycle ran.
    task:
        The parent ResearchTask; ``diminishing_flags`` is mutated in-place.
    config:
        Application configuration (unused currently — thresholds are
        module-level constants; reserved for future override support).

    Returns
    -------
    DiminishingResult
        Computed metrics and the recommended next action.
    """
    # ------------------------------------------------------------------
    # 1. Compute novelty
    # ------------------------------------------------------------------
    # novelty = new findings / total findings (avoids ZeroDivisionError)
    new_findings = findings_after - findings_before
    novelty: float = new_findings / max(findings_after, 1)

    # ------------------------------------------------------------------
    # 2. New sources
    # ------------------------------------------------------------------
    new_sources: int = sources_after - sources_before

    # ------------------------------------------------------------------
    # 3. Score delta
    # ------------------------------------------------------------------
    if len(branch.score_history) >= 2:
        score_delta = abs(branch.score_history[-1] - branch.score_history[-2])
    else:
        # Not enough history — never trip the flag on the first cycle
        score_delta = 10.0

    # ------------------------------------------------------------------
    # 4. Flag logic
    # ------------------------------------------------------------------
    all_stale = (
        novelty < _NOVELTY_THRESHOLD
        and new_sources < _NEW_SOURCES_THRESHOLD
        and score_delta < _SCORE_DELTA_THRESHOLD
    )

    if all_stale:
        task.diminishing_flags += 1
        flag_triggered = True
    else:
        task.diminishing_flags = 0
        flag_triggered = False

    flags = task.diminishing_flags

    # ------------------------------------------------------------------
    # 5. Recommendation
    # ------------------------------------------------------------------
    if flags >= _FLAG_HALT_THRESHOLD:
        recommendation = DiminishingRecommendation.HALT
    elif flags == _FLAG_PIVOT_THRESHOLD:
        recommendation = DiminishingRecommendation.PIVOT
    elif flags == 1:
        recommendation = DiminishingRecommendation.SEARCH_DIFFERENT_SOURCES
    else:
        recommendation = DiminishingRecommendation.CONTINUE

    result = DiminishingResult(
        novelty=novelty,
        new_sources=new_sources,
        score_delta=score_delta,
        flag_triggered=flag_triggered,
        recommendation=recommendation,
    )

    logger.info(
        "diminishing_returns_check",
        branch_id=branch.id,
        task_id=task.id,
        novelty=round(novelty, 4),
        new_sources=new_sources,
        score_delta=round(score_delta, 4),
        flag_triggered=flag_triggered,
        flags_total=flags,
        recommendation=recommendation.value,
    )

    return result
