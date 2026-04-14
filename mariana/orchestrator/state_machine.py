"""
mariana/orchestrator/state_machine.py

The state machine that drives the Mariana Computer research process.

This module defines:
  - :class:`TransitionTrigger` — every event that can cause a state change.
  - :class:`InvalidTransitionError` — raised for undefined (state, trigger) pairs.
  - :class:`Action` — a structured command for the event loop to execute.
  - :class:`ResearchSessionData` — all live runtime data passed to transition().
  - :data:`TRANSITION_TABLE` — the complete FSM transition map.
  - :func:`transition` — the single entry-point for advancing the state machine.

Design notes
------------
* All transitions are deterministic given (state, trigger, session_data).
* Budget cap is checked first in *every* transition — it always overrides.
* No AI calls are made here; this layer issues Action instructions which
  the event loop executes.
* Guard conditions (budget remaining, branch counts, etc.) are evaluated
  inside transition() after the table lookup, producing the final
  (next_state, actions) pair.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

import structlog

from mariana.data.models import (
    Branch,
    Finding,
    ResearchTask,
    State,
)

if TYPE_CHECKING:
    from mariana.orchestrator.cost_tracker import CostTracker

logger = structlog.get_logger(__name__)


# ===========================================================================
# Enumerations
# ===========================================================================


class TransitionTrigger(str, Enum):
    """Every event that can trigger a state-machine transition."""

    HYPOTHESES_READY = "HYPOTHESES_READY"
    BATCH_COMPLETE = "BATCH_COMPLETE"
    BRANCH_SCORE_HIGH = "BRANCH_SCORE_HIGH"
    BRANCH_SCORE_MEDIUM = "BRANCH_SCORE_MEDIUM"
    BRANCH_SCORE_LOW = "BRANCH_SCORE_LOW"
    ALL_BRANCHES_EXHAUSTED = "ALL_BRANCHES_EXHAUSTED"
    STRONG_FINDINGS_EXIST = "STRONG_FINDINGS_EXIST"
    DIMINISHING_RETURNS = "DIMINISHING_RETURNS"
    CONSECUTIVE_DR_FLAGS_3 = "CONSECUTIVE_DR_FLAGS_3"
    TRIBUNAL_CONFIRMED = "TRIBUNAL_CONFIRMED"
    TRIBUNAL_WEAKENED = "TRIBUNAL_WEAKENED"
    TRIBUNAL_DESTROYED = "TRIBUNAL_DESTROYED"
    SKEPTIC_QUESTIONS_RESOLVED = "SKEPTIC_QUESTIONS_RESOLVED"
    SKEPTIC_RESEARCHABLE_EXIST = "SKEPTIC_RESEARCHABLE_EXIST"
    SKEPTIC_CRITICAL_OPEN = "SKEPTIC_CRITICAL_OPEN"
    BUDGET_HARD_CAP = "BUDGET_HARD_CAP"
    MANUAL_HALT = "MANUAL_HALT"


# ===========================================================================
# Exceptions
# ===========================================================================


class InvalidTransitionError(Exception):
    """Raised when a (state, trigger) pair has no defined transition.

    Attributes
    ----------
    state:
        The current state where the undefined trigger arrived.
    trigger:
        The trigger that was not handled.
    """

    def __init__(self, state: State, trigger: TransitionTrigger) -> None:
        self.state = state
        self.trigger = trigger
        super().__init__(
            f"No transition defined for state={state.value!r} + trigger={trigger.value!r}"
        )


# ===========================================================================
# Action type
# ===========================================================================


@dataclass
class Action:
    """A structured instruction emitted by the state machine for the event loop.

    The event loop pattern-matches on ``action_type`` and executes the
    corresponding side-effecting operation.

    Attributes
    ----------
    action_type:
        One of the defined action literals.
    params:
        Arbitrary key-value parameters for the action handler.
    """

    action_type: Literal[
        "SPAWN_AI",
        "DISPATCH_BROWSER",
        "KILL_BRANCH",
        "GRANT_BUDGET",
        "SAVE_CHECKPOINT",
        "GENERATE_REPORT",
        "HALT",
    ]
    params: dict[str, Any] = field(default_factory=dict)


# ===========================================================================
# Session data container
# ===========================================================================


@dataclass
class ResearchSessionData:
    """All runtime state passed into :func:`transition`.

    This dataclass is rebuilt by the event loop before each call to
    ``transition()`` so that it always reflects the freshest in-memory view
    of the task.

    Attributes
    ----------
    task:
        The current ResearchTask (mutable reference).
    active_branches:
        Branches currently being explored.
    dead_branches:
        Branches that have been killed or exhausted.
    recent_findings:
        Findings from the most recent cycle (not the full history).
    all_source_ids:
        Set of all source UUIDs fetched across the entire task, used for
        deduplication.
    ai_call_counter:
        Total number of AI calls made so far.
    recent_action_summaries:
        Short text summaries of the last N actions taken; used as context
        for AI prompts.
    """

    task: ResearchTask
    active_branches: list[Branch]
    dead_branches: list[Branch]
    recent_findings: list[Finding]
    all_source_ids: set[str]
    ai_call_counter: int
    recent_action_summaries: list[str]


# ===========================================================================
# Transition table
# ===========================================================================

# Each entry maps (State, TransitionTrigger) -> State
# Guard conditions that modify the destination or produce extra actions
# are handled in transition() after the table lookup.

_RawTransition = tuple[State, list[str]]  # (next_state, [action_types_hint])

TRANSITION_TABLE: dict[tuple[State, TransitionTrigger], State] = {
    # ------------------------------------------------------------------
    # INIT
    # ------------------------------------------------------------------
    (State.INIT, TransitionTrigger.HYPOTHESES_READY): State.SEARCH,
    (State.INIT, TransitionTrigger.BUDGET_HARD_CAP): State.HALT,

    # ------------------------------------------------------------------
    # SEARCH
    # ------------------------------------------------------------------
    (State.SEARCH, TransitionTrigger.BATCH_COMPLETE): State.EVALUATE,
    (State.SEARCH, TransitionTrigger.BUDGET_HARD_CAP): State.HALT,

    # ------------------------------------------------------------------
    # EVALUATE
    # ------------------------------------------------------------------
    (State.EVALUATE, TransitionTrigger.BRANCH_SCORE_HIGH): State.DEEPEN,
    (State.EVALUATE, TransitionTrigger.BRANCH_SCORE_MEDIUM): State.SEARCH,
    (State.EVALUATE, TransitionTrigger.BRANCH_SCORE_LOW): State.CHECKPOINT,  # after kill
    (State.EVALUATE, TransitionTrigger.STRONG_FINDINGS_EXIST): State.CHECKPOINT,
    (State.EVALUATE, TransitionTrigger.ALL_BRANCHES_EXHAUSTED): State.CHECKPOINT,
    # BUG-040: No scores yet — do another search cycle
    (State.EVALUATE, TransitionTrigger.BATCH_COMPLETE): State.SEARCH,
    (State.EVALUATE, TransitionTrigger.BUDGET_HARD_CAP): State.HALT,

    # ------------------------------------------------------------------
    # DEEPEN
    # ------------------------------------------------------------------
    (State.DEEPEN, TransitionTrigger.BRANCH_SCORE_HIGH): State.DEEPEN,
    (State.DEEPEN, TransitionTrigger.BRANCH_SCORE_MEDIUM): State.SEARCH,
    (State.DEEPEN, TransitionTrigger.BRANCH_SCORE_LOW): State.CHECKPOINT,  # after kill
    (State.DEEPEN, TransitionTrigger.ALL_BRANCHES_EXHAUSTED): State.CHECKPOINT,
    (State.DEEPEN, TransitionTrigger.BUDGET_HARD_CAP): State.HALT,

    # ------------------------------------------------------------------
    # CHECKPOINT
    # ------------------------------------------------------------------
    (State.CHECKPOINT, TransitionTrigger.STRONG_FINDINGS_EXIST): State.TRIBUNAL,
    (State.CHECKPOINT, TransitionTrigger.ALL_BRANCHES_EXHAUSTED): State.PIVOT,  # if budget
    (State.CHECKPOINT, TransitionTrigger.CONSECUTIVE_DR_FLAGS_3): State.HALT,
    (State.CHECKPOINT, TransitionTrigger.DIMINISHING_RETURNS): State.PIVOT,  # flags==2 (guard handles flags==1 → SEARCH)
    # BUG-026: BATCH_COMPLETE fallthrough → continue normal research loop
    (State.CHECKPOINT, TransitionTrigger.BATCH_COMPLETE): State.SEARCH,
    (State.CHECKPOINT, TransitionTrigger.BUDGET_HARD_CAP): State.HALT,

    # ------------------------------------------------------------------
    # PIVOT
    # ------------------------------------------------------------------
    (State.PIVOT, TransitionTrigger.HYPOTHESES_READY): State.SEARCH,
    (State.PIVOT, TransitionTrigger.BUDGET_HARD_CAP): State.HALT,

    # ------------------------------------------------------------------
    # TRIBUNAL
    # ------------------------------------------------------------------
    (State.TRIBUNAL, TransitionTrigger.TRIBUNAL_CONFIRMED): State.SKEPTIC,
    (State.TRIBUNAL, TransitionTrigger.TRIBUNAL_WEAKENED): State.SEARCH,  # or HALT (guard)
    (State.TRIBUNAL, TransitionTrigger.TRIBUNAL_DESTROYED): State.PIVOT,  # or HALT (guard)
    (State.TRIBUNAL, TransitionTrigger.BUDGET_HARD_CAP): State.HALT,

    # ------------------------------------------------------------------
    # SKEPTIC
    # ------------------------------------------------------------------
    (State.SKEPTIC, TransitionTrigger.SKEPTIC_QUESTIONS_RESOLVED): State.REPORT,
    (State.SKEPTIC, TransitionTrigger.SKEPTIC_RESEARCHABLE_EXIST): State.SEARCH,  # if budget
    (State.SKEPTIC, TransitionTrigger.SKEPTIC_CRITICAL_OPEN): State.HALT,

    # ------------------------------------------------------------------
    # REPORT
    # ------------------------------------------------------------------
    (State.REPORT, TransitionTrigger.BATCH_COMPLETE): State.HALT,

    # ------------------------------------------------------------------
    # Universal halts (manual override)
    # ------------------------------------------------------------------
    (State.INIT, TransitionTrigger.MANUAL_HALT): State.HALT,
    (State.SEARCH, TransitionTrigger.MANUAL_HALT): State.HALT,
    (State.EVALUATE, TransitionTrigger.MANUAL_HALT): State.HALT,
    (State.DEEPEN, TransitionTrigger.MANUAL_HALT): State.HALT,
    (State.CHECKPOINT, TransitionTrigger.MANUAL_HALT): State.HALT,
    (State.PIVOT, TransitionTrigger.MANUAL_HALT): State.HALT,
    (State.TRIBUNAL, TransitionTrigger.MANUAL_HALT): State.HALT,
    (State.SKEPTIC, TransitionTrigger.MANUAL_HALT): State.HALT,
    (State.REPORT, TransitionTrigger.MANUAL_HALT): State.HALT,
}

# Strength score used for "strong findings" check
_STRONG_FINDING_CONFIDENCE_THRESHOLD: float = 0.75
_STRONG_FINDING_MIN_COUNT: int = 3

# Minimum budget required to continue after WEAKENED / DESTROYED verdicts
_MIN_BUDGET_TO_CONTINUE: float = 2.00


# ===========================================================================
# Guard helpers
# ===========================================================================


def _has_strong_findings(findings: list[Finding]) -> bool:
    """True if there are at least 3 high-confidence findings."""
    high_conf = [
        f for f in findings if f.confidence >= _STRONG_FINDING_CONFIDENCE_THRESHOLD
    ]
    return len(high_conf) >= _STRONG_FINDING_MIN_COUNT


def _all_branches_exhausted(
    active_branches: list[Branch],
    dead_branches: list[Branch],
) -> bool:
    """True when there are no active branches left."""
    return len(active_branches) == 0


def _budget_is_available(cost_tracker: CostTracker, minimum: float) -> bool:
    """True when remaining budget is above *minimum*."""
    return cost_tracker.budget_remaining >= minimum


# ===========================================================================
# Public transition function
# ===========================================================================


def transition(
    current_state: State,
    trigger: TransitionTrigger,
    session_data: ResearchSessionData,
    db: Any,  # asyncpg.Pool
    cost_tracker: CostTracker,
) -> tuple[State, list[Action]]:
    """Advance the state machine by one step.

    This is the single public entry-point for all state transitions.
    The event loop calls this after computing the appropriate trigger.

    Parameters
    ----------
    current_state:
        The state the machine is currently in.
    trigger:
        The event that occurred.
    session_data:
        Current snapshot of the research session.
    db:
        asyncpg connection pool (for any DB writes triggered by guards).
    cost_tracker:
        Live cost tracker for budget checks.

    Returns
    -------
    tuple[State, list[Action]]
        The new state and the list of actions to execute.

    Raises
    ------
    InvalidTransitionError
        If no row in :data:`TRANSITION_TABLE` matches (current_state,
        trigger) and no budget-cap override applies.
    """
    actions: list[Action] = []

    # ------------------------------------------------------------------
    # 1. Budget hard cap always overrides everything
    # ------------------------------------------------------------------
    if (
        trigger == TransitionTrigger.BUDGET_HARD_CAP
        or cost_tracker.is_exhausted
    ):
        logger.warning(
            "budget_hard_cap_override",
            state=current_state.value,
            trigger=trigger.value,
            total_spent=cost_tracker.total_spent,
            budget=cost_tracker.task_budget,
        )
        actions.append(Action("SAVE_CHECKPOINT", {"reason": "budget_cap"}))
        actions.append(Action("HALT", {"reason": "budget_exhausted"}))
        return State.HALT, actions

    # ------------------------------------------------------------------
    # 2. Manual halt override
    # ------------------------------------------------------------------
    if trigger == TransitionTrigger.MANUAL_HALT:
        actions.append(Action("SAVE_CHECKPOINT", {"reason": "manual_halt"}))
        actions.append(Action("HALT", {"reason": "manual_halt"}))
        return State.HALT, actions

    # ------------------------------------------------------------------
    # 3. Table lookup
    # ------------------------------------------------------------------
    raw_next = TRANSITION_TABLE.get((current_state, trigger))
    if raw_next is None:
        raise InvalidTransitionError(current_state, trigger)

    next_state = raw_next

    # ------------------------------------------------------------------
    # 4. Apply guard conditions and produce actions
    # ------------------------------------------------------------------
    next_state, actions = _apply_guards(
        current_state=current_state,
        trigger=trigger,
        raw_next=next_state,
        session_data=session_data,
        cost_tracker=cost_tracker,
        actions=actions,
    )

    logger.info(
        "state_transition",
        from_state=current_state.value,
        trigger=trigger.value,
        to_state=next_state.value,
        actions=[a.action_type for a in actions],
    )

    return next_state, actions


# ===========================================================================
# Guard condition application
# ===========================================================================


def _apply_guards(
    current_state: State,
    trigger: TransitionTrigger,
    raw_next: State,
    session_data: ResearchSessionData,
    cost_tracker: CostTracker,
    actions: list[Action],
) -> tuple[State, list[Action]]:
    """Apply guard conditions that may modify the table-suggested transition.

    Returns the (possibly modified) next state and updated actions list.
    """
    task = session_data.task
    active = session_data.active_branches
    findings = session_data.recent_findings

    # ------------------------------------------------------------------
    # INIT → SEARCH (HYPOTHESES_READY)
    # ------------------------------------------------------------------
    if current_state == State.INIT and trigger == TransitionTrigger.HYPOTHESES_READY:
        actions.append(Action("SPAWN_AI", {"task_type": "HYPOTHESIS_GENERATION"}))
        return State.SEARCH, actions

    # ------------------------------------------------------------------
    # SEARCH → EVALUATE (BATCH_COMPLETE)
    # ------------------------------------------------------------------
    if current_state == State.SEARCH and trigger == TransitionTrigger.BATCH_COMPLETE:
        actions.append(Action("SPAWN_AI", {"task_type": "EVALUATION"}))
        return State.EVALUATE, actions

    # ------------------------------------------------------------------
    # EVALUATE
    # ------------------------------------------------------------------
    if current_state == State.EVALUATE:
        if trigger == TransitionTrigger.BATCH_COMPLETE:
            # BUG-040: No scores yet — do another search cycle
            actions.append(Action("SPAWN_AI", {"task_type": "EVIDENCE_EXTRACTION"}))
            return State.SEARCH, actions

        if trigger == TransitionTrigger.BRANCH_SCORE_LOW:
            # Kill the lowest-scoring branch first
            actions.append(Action("KILL_BRANCH", {"reason": "low_score"}))
            # BUG-015: Account for the branch about to be killed when checking exhaustion
            remaining_active = len(active) - 1
            if remaining_active <= 0:
                actions.append(Action("SAVE_CHECKPOINT", {"reason": "all_branches_dead"}))
                return State.CHECKPOINT, actions
            # Still branches left → loop back to search
            return State.SEARCH, actions

        if trigger == TransitionTrigger.STRONG_FINDINGS_EXIST:
            actions.append(Action("SAVE_CHECKPOINT", {"reason": "strong_findings"}))
            return State.CHECKPOINT, actions

        if trigger == TransitionTrigger.ALL_BRANCHES_EXHAUSTED:
            actions.append(Action("SAVE_CHECKPOINT", {"reason": "all_exhausted"}))
            return State.CHECKPOINT, actions

        if trigger == TransitionTrigger.BRANCH_SCORE_HIGH:
            # BUG-020: differentiate score7 ($20 grant) vs score8 ($50 grant)
            _best_score = max(
                (b.latest_score for b in active if b.latest_score is not None),
                default=0.0,
            )
            _score_band = "score8" if _best_score >= 0.8 else "score7"
            actions.append(Action("GRANT_BUDGET", {"score_band": _score_band}))
            return State.DEEPEN, actions

        if trigger == TransitionTrigger.BRANCH_SCORE_MEDIUM:
            return State.SEARCH, actions

    # ------------------------------------------------------------------
    # DEEPEN
    # ------------------------------------------------------------------
    if current_state == State.DEEPEN:
        if trigger == TransitionTrigger.BRANCH_SCORE_HIGH:
            # BUG-020: differentiate score7 vs score8
            _best_score_d = max(
                (b.latest_score for b in active if b.latest_score is not None),
                default=0.0,
            )
            _score_band_d = "score8" if _best_score_d >= 0.8 else "score7"
            actions.append(Action("GRANT_BUDGET", {"score_band": _score_band_d}))
            return State.DEEPEN, actions

        if trigger == TransitionTrigger.BRANCH_SCORE_MEDIUM:
            return State.SEARCH, actions

        if trigger == TransitionTrigger.BRANCH_SCORE_LOW:
            actions.append(Action("KILL_BRANCH", {"reason": "low_score_in_deepen"}))
            # BUG-015: Account for the branch about to be killed when checking exhaustion
            remaining_active = len(active) - 1
            if remaining_active <= 0:
                actions.append(Action("SAVE_CHECKPOINT", {"reason": "all_branches_dead"}))
                return State.CHECKPOINT, actions
            return State.SEARCH, actions

        if trigger == TransitionTrigger.ALL_BRANCHES_EXHAUSTED:
            actions.append(Action("SAVE_CHECKPOINT", {"reason": "all_exhausted_deepen"}))
            return State.CHECKPOINT, actions

    # ------------------------------------------------------------------
    # CHECKPOINT
    # ------------------------------------------------------------------
    if current_state == State.CHECKPOINT:
        # Always save on entry to CHECKPOINT state
        actions.append(Action("SAVE_CHECKPOINT", {"reason": trigger.value}))

        if trigger == TransitionTrigger.BATCH_COMPLETE:
            # BUG-026: Normal fallthrough — continue research loop
            return State.SEARCH, actions

        if trigger == TransitionTrigger.STRONG_FINDINGS_EXIST:
            return State.TRIBUNAL, actions

        if trigger == TransitionTrigger.CONSECUTIVE_DR_FLAGS_3:
            actions.append(Action("HALT", {"reason": "3_consecutive_dr_flags"}))
            return State.HALT, actions

        if trigger == TransitionTrigger.DIMINISHING_RETURNS:
            flags = task.diminishing_flags
            # BUG-016: flags >= 3 should have triggered CONSECUTIVE_DR_FLAGS_3 instead;
            # this path is defensive only.
            assert flags > 0, "DIMINISHING_RETURNS trigger should only fire with flags > 0"
            if flags >= 3:
                actions.append(Action("HALT", {"reason": "dr_flags_ge_3"}))
                return State.HALT, actions
            if flags == 2:
                if _budget_is_available(cost_tracker, _MIN_BUDGET_TO_CONTINUE):
                    return State.PIVOT, actions
                else:
                    actions.append(Action("HALT", {"reason": "no_budget_for_pivot"}))
                    return State.HALT, actions
            # flags == 1 → search different sources
            return State.SEARCH, actions

        if trigger == TransitionTrigger.ALL_BRANCHES_EXHAUSTED:
            if _budget_is_available(cost_tracker, _MIN_BUDGET_TO_CONTINUE):
                return State.PIVOT, actions
            else:
                actions.append(Action("HALT", {"reason": "no_budget_for_pivot"}))
                return State.HALT, actions

    # ------------------------------------------------------------------
    # PIVOT → SEARCH (HYPOTHESES_READY)
    # ------------------------------------------------------------------
    if current_state == State.PIVOT and trigger == TransitionTrigger.HYPOTHESES_READY:
        actions.append(Action("SPAWN_AI", {"task_type": "HYPOTHESIS_GENERATION", "pivot": True}))
        return State.SEARCH, actions

    # ------------------------------------------------------------------
    # TRIBUNAL
    # ------------------------------------------------------------------
    if current_state == State.TRIBUNAL:
        if trigger == TransitionTrigger.TRIBUNAL_CONFIRMED:
            actions.append(Action("SPAWN_AI", {"task_type": "SKEPTIC_QUESTIONS"}))
            return State.SKEPTIC, actions

        if trigger == TransitionTrigger.TRIBUNAL_WEAKENED:
            if _budget_is_available(cost_tracker, _MIN_BUDGET_TO_CONTINUE):
                return State.SEARCH, actions
            else:
                actions.append(Action("HALT", {"reason": "weakened_no_budget"}))
                return State.HALT, actions

        if trigger == TransitionTrigger.TRIBUNAL_DESTROYED:
            if _budget_is_available(cost_tracker, _MIN_BUDGET_TO_CONTINUE):
                return State.PIVOT, actions
            else:
                actions.append(Action("HALT", {"reason": "destroyed_no_budget"}))
                return State.HALT, actions

    # ------------------------------------------------------------------
    # SKEPTIC
    # ------------------------------------------------------------------
    if current_state == State.SKEPTIC:
        if trigger == TransitionTrigger.SKEPTIC_QUESTIONS_RESOLVED:
            actions.append(Action("GENERATE_REPORT", {}))
            return State.REPORT, actions

        if trigger == TransitionTrigger.SKEPTIC_RESEARCHABLE_EXIST:
            if _budget_is_available(cost_tracker, _MIN_BUDGET_TO_CONTINUE):
                return State.SEARCH, actions
            else:
                # No budget for more research; generate whatever we have
                actions.append(Action("GENERATE_REPORT", {"partial": True}))
                return State.REPORT, actions

        if trigger == TransitionTrigger.SKEPTIC_CRITICAL_OPEN:
            actions.append(Action("HALT", {"reason": "critical_open_questions"}))
            return State.HALT, actions

    # ------------------------------------------------------------------
    # REPORT → HALT
    # ------------------------------------------------------------------
    if current_state == State.REPORT:
        actions.append(Action("HALT", {"reason": "report_complete"}))
        return State.HALT, actions

    # ------------------------------------------------------------------
    # Fallback: return the table's raw suggestion unchanged
    # ------------------------------------------------------------------
    return raw_next, actions
