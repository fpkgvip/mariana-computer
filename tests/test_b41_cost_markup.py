"""B-41 regression suite: DB total_spent_usd stores 1.20× markup, not raw cost.

Before the fix, _sync_cost set task.total_spent_usd = cost_tracker.total_spent
(raw model cost).  The frontend WebSocket sent total_with_markup (raw × 1.20)
and the credit ledger deducted the marked-up amount, but the DB recorded the
raw cost.  This caused a permanent 20% reconciliation gap.

After the fix, _sync_cost sets task.total_spent_usd = raw_cost × 1.20 so that
the DB column matches the user-facing charge amount.

Test IDs:
  1. test_sync_cost_applies_1_20_markup
  2. test_sync_cost_zero_cost_stays_zero
  3. test_sync_cost_preserves_call_count
  4. test_cost_markup_multiplier_constant_is_1_20
  5. test_sync_cost_matches_total_with_markup
  6. test_total_spent_usd_not_raw_cost
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task() -> MagicMock:
    task = MagicMock()
    task.total_spent_usd = 0.0
    task.ai_call_counter = 0
    return task


def _make_cost_tracker(total_spent: float, call_count: int = 5) -> MagicMock:
    tracker = MagicMock()
    tracker.total_spent = total_spent
    tracker.call_count = call_count
    tracker.total_with_markup = total_spent * 1.20
    return tracker


# ---------------------------------------------------------------------------
# Test 1: raw cost of 1.0 → persisted as 1.20
# ---------------------------------------------------------------------------

def test_sync_cost_applies_1_20_markup():
    """B-41: _sync_cost must store raw_cost × 1.20 in task.total_spent_usd."""
    from mariana.orchestrator.event_loop import _sync_cost

    task = _make_task()
    tracker = _make_cost_tracker(total_spent=1.0)

    _sync_cost(task, tracker)

    assert task.total_spent_usd == pytest.approx(1.20, rel=1e-9), (
        f"B-41: $1.00 raw cost must be stored as $1.20 (1.20× markup), "
        f"got {task.total_spent_usd}"
    )


# ---------------------------------------------------------------------------
# Test 2: zero cost stays zero after markup
# ---------------------------------------------------------------------------

def test_sync_cost_zero_cost_stays_zero():
    """Zero raw cost should remain zero after the markup factor."""
    from mariana.orchestrator.event_loop import _sync_cost

    task = _make_task()
    tracker = _make_cost_tracker(total_spent=0.0)

    _sync_cost(task, tracker)

    assert task.total_spent_usd == 0.0


# ---------------------------------------------------------------------------
# Test 3: call count is still propagated correctly
# ---------------------------------------------------------------------------

def test_sync_cost_preserves_call_count():
    """_sync_cost must still update ai_call_counter correctly."""
    from mariana.orchestrator.event_loop import _sync_cost

    task = _make_task()
    tracker = _make_cost_tracker(total_spent=5.0, call_count=42)

    _sync_cost(task, tracker)

    assert task.ai_call_counter == 42


# ---------------------------------------------------------------------------
# Test 4: the multiplier constant is exactly 1.20
# ---------------------------------------------------------------------------

def test_cost_markup_multiplier_constant_is_1_20():
    """The _COST_MARKUP_MULTIPLIER constant must be exactly 1.20."""
    from mariana.orchestrator.event_loop import _COST_MARKUP_MULTIPLIER

    assert _COST_MARKUP_MULTIPLIER == pytest.approx(1.20), (
        f"B-41: _COST_MARKUP_MULTIPLIER must be 1.20, got {_COST_MARKUP_MULTIPLIER}"
    )


# ---------------------------------------------------------------------------
# Test 5: _sync_cost result matches CostTracker.total_with_markup
# ---------------------------------------------------------------------------

def test_sync_cost_matches_total_with_markup():
    """task.total_spent_usd after _sync_cost must equal tracker.total_with_markup."""
    from mariana.orchestrator.event_loop import _sync_cost

    raw = 3.75
    task = _make_task()
    tracker = _make_cost_tracker(total_spent=raw)

    _sync_cost(task, tracker)

    assert task.total_spent_usd == pytest.approx(tracker.total_with_markup, rel=1e-9), (
        f"B-41: persisted value must match total_with_markup, "
        f"expected {tracker.total_with_markup}, got {task.total_spent_usd}"
    )


# ---------------------------------------------------------------------------
# Test 6: verify that total_spent_usd is NOT the raw cost
# ---------------------------------------------------------------------------

def test_total_spent_usd_not_raw_cost():
    """After the fix, total_spent_usd must differ from the raw cost (assuming raw > 0)."""
    from mariana.orchestrator.event_loop import _sync_cost

    raw = 2.50
    task = _make_task()
    tracker = _make_cost_tracker(total_spent=raw)

    _sync_cost(task, tracker)

    assert task.total_spent_usd != raw, (
        f"B-41: total_spent_usd must NOT equal raw cost {raw}; "
        f"it must be {raw * 1.20} (with markup). Got {task.total_spent_usd}."
    )
    assert task.total_spent_usd == pytest.approx(raw * 1.20, rel=1e-9)
