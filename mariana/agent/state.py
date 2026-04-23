"""State machine for the Mariana agent.

Keeps transition logic out of the (already large) event-loop module.  The
state machine itself is intentionally tiny — the interesting decisions are
data-driven and live in the loop.
"""

from __future__ import annotations

from typing import Iterable

from mariana.agent.models import AgentState


# Allowed transitions.  Anything not in this set is rejected.
_TRANSITIONS: dict[AgentState, frozenset[AgentState]] = {
    AgentState.PLAN:    frozenset({AgentState.EXECUTE, AgentState.FAILED, AgentState.HALTED}),
    AgentState.EXECUTE: frozenset({AgentState.TEST, AgentState.FIX, AgentState.REPLAN,
                                   AgentState.DELIVER, AgentState.FAILED, AgentState.HALTED,
                                   AgentState.EXECUTE}),
    AgentState.TEST:    frozenset({AgentState.EXECUTE, AgentState.FIX, AgentState.REPLAN,
                                   AgentState.DELIVER, AgentState.FAILED, AgentState.HALTED}),
    AgentState.FIX:     frozenset({AgentState.EXECUTE, AgentState.REPLAN, AgentState.FAILED,
                                   AgentState.HALTED}),
    AgentState.REPLAN:  frozenset({AgentState.EXECUTE, AgentState.FAILED, AgentState.HALTED}),
    AgentState.DELIVER: frozenset({AgentState.DONE, AgentState.FAILED, AgentState.HALTED}),
    AgentState.DONE:    frozenset(),
    AgentState.FAILED:  frozenset(),
    AgentState.HALTED:  frozenset(),
}


# States the loop considers terminal — execution stops when we hit one.
TERMINAL_STATES: frozenset[AgentState] = frozenset({
    AgentState.DONE, AgentState.FAILED, AgentState.HALTED,
})


def is_terminal(state: AgentState) -> bool:
    return state in TERMINAL_STATES


def can_transition(src: AgentState, dst: AgentState) -> bool:
    return dst in _TRANSITIONS.get(src, frozenset())


def assert_transition(src: AgentState, dst: AgentState) -> None:
    if not can_transition(src, dst):
        raise RuntimeError(
            f"illegal state transition: {src.value} -> {dst.value}.  "
            f"Legal: {sorted(s.value for s in _TRANSITIONS.get(src, frozenset()))}"
        )


def legal_next_states(src: AgentState) -> Iterable[AgentState]:
    return iter(_TRANSITIONS.get(src, frozenset()))
