"""O-01 regression: AgentStartRequest.budget_usd Pydantic floor.

The frontend PreflightCard previously allowed sub-100-credit ceilings, which
the backend silently rounded up to the canonical 100-credit floor. The fix
clamps the UI to CREDITS_MIN_RESERVATION=100 and tightens the Pydantic field
to ``ge=1.0`` so direct-API callers (those bypassing the UI) get a clear
422 ValidationError rather than a silent over-reservation.

This file covers the backend half of the O-01 fix.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_o01_pydantic_rejects_below_1_dollar():
    """budget_usd < 1.0 must raise pydantic.ValidationError."""
    from mariana.agent.api_routes import AgentStartRequest

    # The previous floor was 0.1 — accepted in M-01. After O-01 the floor is
    # 1.0 to match the backend's ``max(100, int(budget_usd*100))`` reservation.
    with pytest.raises(ValidationError):
        AgentStartRequest(goal="tiny", budget_usd=0.5)
    with pytest.raises(ValidationError):
        AgentStartRequest(goal="tiny", budget_usd=0.1)
    with pytest.raises(ValidationError):
        AgentStartRequest(goal="tiny", budget_usd=0.99)


def test_o01_pydantic_accepts_at_floor():
    """budget_usd == 1.0 must be accepted (the canonical 100-credit floor)."""
    from mariana.agent.api_routes import AgentStartRequest

    body = AgentStartRequest(goal="tiny", budget_usd=1.0)
    assert body.budget_usd == 1.0
    # And reasonable values still pass.
    body5 = AgentStartRequest(goal="five", budget_usd=5.0)
    assert body5.budget_usd == 5.0
    body_max = AgentStartRequest(goal="max", budget_usd=100.0)
    assert body_max.budget_usd == 100.0


def test_o01_pydantic_rejects_above_max():
    """budget_usd > 100.0 still rejected (existing upper bound preserved)."""
    from mariana.agent.api_routes import AgentStartRequest

    with pytest.raises(ValidationError):
        AgentStartRequest(goal="too big", budget_usd=200.0)
