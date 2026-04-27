# O-01 + O-02 Fix Report

**Date:** 2026-04-28
**Branch:** `loop6/zero-bug`
**Source audit:** `loop6_audit/A17_phase_e_reaudit.md`

## Summary

Two P2 findings from re-audit #12 closed in a single commit:

- **O-01:** frontend/backend ceiling-floor mismatch.  PreflightCard
  exposed sub-100-credit ceilings while the backend always reserved
  ``max(100, int(budget_usd * 100))``.  Result: silent over-reservation
  or false 402 rejection.
- **O-02:** ``POST /api/agent/{task_id}/stop`` only set
  ``stop_requested=TRUE`` without terminalising or settling.  Recovery
  requeued such rows; ``run_agent_task`` invoked the planner before the
  first stop check, so cancelled queued tasks still paid the planner
  cost.

## O-01 — diff summary

**Frontend canonical constant.**
- `frontend/src/components/deft/studio/stage.ts`: export
  ``CREDITS_MIN_RESERVATION = 100``.

**Frontend PreflightCard.**
- `frontend/src/components/deft/PreflightCard.tsx`:
  - Imports the constant.
  - ``ceilingMin`` is now ``Math.max(CREDITS_MIN_RESERVATION,
    Math.max(1, Math.floor(quote.credits_min * 0.5)))``.
  - ``ceilingFloor`` and the no-quote default both use the constant
    instead of a literal `100`.
  - Numeric input ``onChange`` hard-clamps typed values up to the
    canonical floor.
  - Caption ``"Minimum reservation 100 credits"`` rendered next to
    the “Credit ceiling” label.

**Backend defense-in-depth.**
- `mariana/agent/api_routes.py`: ``AgentStartRequest.budget_usd``
  Pydantic ``Field`` tightened from ``ge=0.1`` to ``ge=1.0`` so direct-API
  callers receive a 422 ValidationError instead of a silent reservation
  bump.

**Tests added/updated.**
- `frontend/src/components/deft/PreflightCard.test.tsx` (new) — 3 vitest
  cases: floor enforcement, sub-floor input clamping, caption presence.
- `tests/test_o01_budget_min_floor.py` (new) — 3 pytest cases covering
  reject below 1.0, accept at floor / 5.0 / 100.0, reject above 100.
- `tests/test_m01_agent_billing_unit.py` — adjusted two existing cases
  from ``budget_usd=0.5`` and ``0.1`` to the new ``1.0`` floor.  Behaviour
  unchanged: floor still resolves to 100 credits.

## O-02 — diff summary

**New terminal state.**
- `mariana/agent/models.py`: ``AgentState.CANCELLED = "cancelled"`` added.
- `mariana/agent/state.py`: ``CANCELLED`` legal as a target from ``PLAN``;
  ``TERMINAL_STATES`` and ``is_terminal`` extended to include it.

**Stop endpoint reworked.**
- `mariana/agent/api_routes.py` (``stop_agent_task``):
  - ``SELECT ... FOR UPDATE`` inside an ``async with conn.transaction()``.
  - Idempotent fast path for already-terminal-and-settled rows.
  - For pre-execution rows (``state == PLAN`` and ``spent_usd <= 0`` and
    ``credits_settled == False``) the endpoint sets
    ``stop_requested = TRUE``, then after the transaction reloads the
    task, sets ``state = CANCELLED``, calls
    ``_settle_agent_credits`` inline, and persists the terminal row.
  - For all other states the legacy behaviour is preserved (set
    ``stop_requested=TRUE`` only; the worker terminalises + settles in
    its ``finally:``).
- SSE EOF detection in the same module now treats ``CANCELLED`` as a
  terminal state alongside ``DONE``/``FAILED``/``HALTED``.

**Worker pre-planner gate.**
- `mariana/agent/loop.py` (``run_agent_task``): an extra
  ``_check_stop_requested`` is invoked immediately after the initial
  ``_persist_task`` and **before** ``planner.build_initial_plan``.  If
  set, the task transitions to ``HALTED`` (legal from PLAN), emits a
  ``halted`` event, and the ``finally:`` settlement block refunds the
  reservation.  ``HALTED`` (rather than ``CANCELLED``) keeps the
  transition map intact and signals "the worker honoured the stop".

**Recovery filter.**
- `mariana/main.py`: ``WHERE`` clause adds ``AND stop_requested = FALSE``
  so legacy cancelled-but-non-terminal rows are not blindly requeued.

## Tests added

- `tests/test_o02_cancel_settlement.py` (new) — 4 cases:
  1. `test_o02_stop_terminal_for_queued`: PLAN/no-spend → row reloads as
     ``CANCELLED`` + ``credits_settled=True`` + add_credits refund of
     500 fired.
  2. `test_o02_stop_running_still_signals_only`: EXECUTE/spend>0 stays
     non-terminal with ``stop_requested=TRUE``.
  3. `test_o02_recovery_skips_stop_requested`: live recovery query
     skips a stale row whose ``stop_requested=TRUE``.
  4. `test_o02_run_agent_task_early_stop_check`: planner mock has
     ``await_count == 0`` for a recovered ``stop_requested=TRUE`` task;
     final ``spent_usd == 0.0`` and state ``HALTED``.

## Test results

- Python: `python -m pytest tests/ --timeout=60` → **322 passed,
  13 skipped** (vault-live and ledger SSL cases skipped as usual).
- Vitest: `npx vitest run` → **144 passed** across 15 files including
  the new PreflightCard suite.

## Voice / style notes

UI caption uses calm, factual phrasing ("Minimum reservation 100
credits") with no exclamation, no hero verbs, no forbidden adjectives.
Comments describe the bug and the fix without marketing tone.

## Files changed

- `frontend/src/components/deft/studio/stage.ts`
- `frontend/src/components/deft/PreflightCard.tsx`
- `frontend/src/components/deft/PreflightCard.test.tsx` (new)
- `mariana/agent/api_routes.py`
- `mariana/agent/loop.py`
- `mariana/agent/models.py`
- `mariana/agent/state.py`
- `mariana/main.py`
- `tests/test_m01_agent_billing_unit.py`
- `tests/test_o01_budget_min_floor.py` (new)
- `tests/test_o02_cancel_settlement.py` (new)
- `loop6_audit/REGISTRY.md`
- `loop6_audit/O01_O02_FIX_REPORT.md` (this file)
