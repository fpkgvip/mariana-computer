# M-01 fix report — agent billing 5x over-reservation + settlement

**Date:** 2026-04-28
**Branch:** `loop6/zero-bug`
**Severity:** P1 (money/correctness)
**Surface:** api / agent billing / agent runtime

## 1. Bug summary

`POST /api/agent` reserved credits with `max(200, int(body.budget_usd * 500))`
— **500 credits per USD** — while three other parts of the platform agreed on
**100 credits per USD**:

* `mariana/agent/loop.py:_budget_exceeded` (now line ~256) halts the runtime
  when `spent_usd >= budget_usd`, both stored in raw dollars. Maximum
  consumable cost is therefore `budget_usd` dollars, i.e. `budget_usd * 100`
  credits at the canonical rate.
* `frontend/src/components/deft/studio/stage.ts:90-95` ships
  `creditsFromUsd: 1 credit == $0.01` (100 credits/USD).
* `frontend/src/pages/Pricing.tsx:303` literally says "1c = $0.01".
* `mariana/main.py:_deduct_user_credits` (lines 406-490) settles research
  tasks via `int(total_with_markup * 100)` — same 100c/USD canonical.

Result: a $5 task reserved **2500 credits** but the runtime/UI ceiling was
**500 credits** — up to **2000 credits** over-collected per task.

Worse, the agent path had no completion-time settlement: the only refund
path was a narrow `except Exception as insert_exc:` guard around the
pre-enqueue Postgres `INSERT`. The inline comment "Refunded on error" was a
lie for every successfully enqueued task. Even a task that halted at $0 of
actual spend kept the entire 2500-credit reservation.

## 2. RED test results (before fix)

Created `tests/test_m01_agent_billing_unit.py` first and ran it against the
buggy code:

```
$ python -m pytest tests/test_m01_agent_billing_unit.py
FAILED tests/test_m01_agent_billing_unit.py::test_reserve_canonical_100_per_usd
    AssertionError: expected 500 credits for $5 budget under canonical 100c/USD; got 2500
FAILED tests/test_m01_agent_billing_unit.py::test_reserve_floor_at_100
FAILED tests/test_m01_agent_billing_unit.py::test_settlement_refunds_unused_on_done
    ImportError: cannot import name '_settle_agent_credits' from 'mariana.agent.loop'
FAILED tests/test_m01_agent_billing_unit.py::test_settlement_refunds_full_on_failed
FAILED tests/test_m01_agent_billing_unit.py::test_settlement_extra_deduct_on_overrun
FAILED tests/test_m01_agent_billing_unit.py::test_settlement_idempotent
============================== 6 failed in 1.80s ===============================
```

All 6 tests RED on the buggy code, as required.

## 3. Code changes

### 3a. `mariana/agent/api_routes.py` (~lines 401-464)

* Replaced the comment block on lines 401-413 with a multi-paragraph
  explanation that names the canonical 100c/USD conversion, points at the
  three platform sources of truth (`stage.ts`, `Pricing.tsx`,
  `_deduct_user_credits`), and explicitly states "Settled at task completion
  (refund unused, deduct overage) by `mariana/agent/loop.py:_settle_agent_credits`".
  Removed the misleading "Refunded on error" wording.
* `reserved_credits = max(200, int(body.budget_usd * 500))` →
  `reserved_credits = max(100, int(body.budget_usd * 100))` (line 428).
* HTTP 402 detail string updated to "(at 100 credits/$ canonical conversion)"
  so users can tie the number back to the published rate (line 435-437).
* Pass `reserved_credits=reserved_credits` into the `AgentTask` constructor
  so the runtime can settle it (line 463).

### 3b. `mariana/agent/models.py` (lines 142-155)

Added two fields to `AgentTask` immediately after the existing
`budget_usd` / `spent_usd` block:

```python
reserved_credits: int = 0
credits_settled: bool = False
```

The accompanying comment block documents that these are in-memory-only
(no Postgres column) for this release, and explains that the
`credits_settled` flag is the idempotency primitive that prevents a retried
orchestrator pass from double-charging or double-refunding.

### 3c. `mariana/agent/loop.py`

* New helper `_settle_agent_credits(task: AgentTask) -> None` at lines
  264-411. Mirrors `mariana/main.py:_deduct_user_credits`:
  * Returns immediately if `task.credits_settled` or `task.reserved_credits <= 0`.
  * Late-imports `_get_config` and `_supabase_api_key` from `mariana.api`
    to dodge the api.py ↔ agent.loop circular import.
  * `final_tokens = int(task.spent_usd * 100)`,
    `delta = final_tokens - task.reserved_credits`.
  * `delta == 0` → log `agent_credits_settle_noop`, set flag.
  * `delta > 0` → POST `/rest/v1/rpc/deduct_credits` with
    `{target_user_id, amount: delta}`; flag set regardless of HTTP outcome
    (so retry can't double-charge); success/failure logged with
    `agent_credits_settle_extra_deduct_{ok,failed}`.
  * `delta < 0` → POST `/rest/v1/rpc/add_credits` with
    `{p_user_id, p_credits: abs(delta)}`; flag set regardless;
    `agent_credits_settle_refund_{ok,failed}`.
  * Outer `try/except` swallows any exception, sets flag, logs
    `agent_credits_settle_exception`. Settlement errors NEVER bubble out.
* Wired into the `run_task` `finally:` block (lines 885-902): when
  `is_terminal(task.state)` is true (DONE / FAILED / HALTED), call
  `_settle_agent_credits(task)` first so the `credits_settled = True` flag
  lands in the same `_persist_task` UPSERT that records the terminal state.
  Wrapped in its own `try/except` that logs `agent_credits_settle_finally_error`
  but does not crash the finally block.

## 4. Settlement helper design notes

* **Conversion:** canonical 100 credits/USD (matches frontend +
  research-task settlement).
* **Idempotency:** single in-memory boolean (`credits_settled`). Sufficient
  because settlement only runs in the `run_task` `finally:` block of a
  single orchestrator process — there is no cross-process retry path that
  could observe a stale flag.
* **Failure policy:** flag is set unconditionally once an RPC has been
  *attempted*. A failed RPC is logged and reconciled offline. The
  alternative (leaving the flag clear on failure) would expose the user to
  double-charge / double-refund the next time the function is called.
* **Why not persist to Postgres:** the task explicitly forbids a Supabase
  migration, and settlement is single-process by construction. If a
  multi-process retry path is ever added, persisting the flag becomes
  necessary.
* **RPC names:** `deduct_credits` and `add_credits` already exist
  (mariana/api.py lines 6905 and 7042). Reused exactly the same JSON
  shapes those helpers use, so the existing Supabase RPC functions handle
  this caller without changes.

## 5. Regression test count

6 new tests in `tests/test_m01_agent_billing_unit.py`:

1. `test_reserve_canonical_100_per_usd` — $5 budget reserves 500 credits
   (NOT 2500); $0.50 budget reserves 100 credits (floor wins over 50).
2. `test_reserve_floor_at_100` — $0.10 budget still reserves 100 (floor).
3. `test_settlement_refunds_unused_on_done` — reserved=500, spent=$0.30
   (=30 tokens) → `add_credits` called with 470, `credits_settled` True.
4. `test_settlement_refunds_full_on_failed` — reserved=500, spent=$0,
   terminal=FAILED → full 500 refunded.
5. `test_settlement_extra_deduct_on_overrun` — at-budget (5.0/500) is
   noop; over-budget ($5.40 with reserved=500) → `deduct_credits` extra 40.
6. `test_settlement_idempotent` — second `_settle_agent_credits` call is a
   no-op (only one RPC observed).

## 6. Full pytest results (after fix)

```
$ PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb \
    python -m pytest tests/
================= 309 passed, 13 skipped, 2 warnings in 6.40s ==================
```

309 passed, 13 skipped, 0 failed. M-01 file alone:

```
tests/test_m01_agent_billing_unit.py::test_reserve_canonical_100_per_usd PASSED
tests/test_m01_agent_billing_unit.py::test_reserve_floor_at_100 PASSED
tests/test_m01_agent_billing_unit.py::test_settlement_refunds_unused_on_done PASSED
tests/test_m01_agent_billing_unit.py::test_settlement_refunds_full_on_failed PASSED
tests/test_m01_agent_billing_unit.py::test_settlement_extra_deduct_on_overrun PASSED
tests/test_m01_agent_billing_unit.py::test_settlement_idempotent PASSED
============================== 6 passed in 1.94s ===============================
```
