# A15 — Phase E re-audit #10

## 1. Header

- **Model:** GPT-5 API subagent
- **Commit:** `ee6e329`
- **Scope:** diff review for commits `cc3183b..ee6e329`; full passes on `mariana/agent/api_routes.py`, `mariana/agent/loop.py`, `mariana/agent/models.py`, `mariana/agent/planner.py`, `mariana/agent/dispatcher.py`, `mariana/api.py`, `mariana/main.py`, `frontend/src/components/deft/studio/stage.ts`, `frontend/src/pages/Build.tsx`, `frontend/src/pages/Pricing.tsx`, `frontend/src/pages/Account.tsx`, and Supabase migrations `004..021` with explicit attention to RLS / grant posture / `SECURITY DEFINER` routines.

## 2. Surface walkthrough

### Backend / agent billing path

- `mariana/agent/api_routes.py:78-120` — reviewed `agent_tasks` insert shape.
- `mariana/agent/api_routes.py:123-177` — reviewed task reload / model reconstruction path.
- `mariana/agent/api_routes.py:381-518` — reviewed `POST /api/agent` reservation, insert-failure refund, vault staging, and enqueue behavior.
- `mariana/agent/loop.py:80-135` — reviewed task UPSERT/checkpoint persistence.
- `mariana/agent/loop.py:255-411` — reviewed budget enforcement and new `_settle_agent_credits` helper.
- `mariana/agent/loop.py:714-902` — reviewed `run_agent_task`, especially terminal-state `finally:` settlement ordering.
- `mariana/agent/models.py:116-155` — reviewed `AgentTask` additions `reserved_credits` / `credits_settled`.
- `mariana/main.py:738-827` — reviewed the real queue-consumer path that loads tasks from Postgres and invokes `run_agent_task`.
- `tests/test_m01_agent_billing_unit.py:147-367` — reviewed new M-01 regression coverage and looked for missing path coverage.

### Adjacent agent/runtime surfaces reviewed with no new issue

- `mariana/agent/planner.py:1-260` — reviewed model normalization, prompt/tool manifest, and planner output constraints.
- `mariana/agent/dispatcher.py:81-235` — reviewed dispatch table entry points, parameter validation, and fetched-content injection markers.

### Billing / webhook / research settlement baselines reviewed with no new issue

- `mariana/api.py:6084-6839` — re-checked `_grant_credits_for_event`, `stripe_payment_grants` linkage, charge-reversal flow, and Supabase key selection.
- `mariana/main.py:406-502` — re-checked research-task settlement helper as the comparison baseline for agent billing.

### Frontend consistency surfaces reviewed with no new issue

- `frontend/src/components/deft/studio/stage.ts:89-96` — confirmed canonical `100 credits = $1` conversion.
- `frontend/src/pages/Build.tsx:365-399` — confirmed build header uses canonical conversion for spend/ceiling display.
- `frontend/src/pages/Pricing.tsx:298-304` — confirmed pricing copy still states `1c = $0.01`.
- `frontend/src/pages/Account.tsx:175-257` — re-checked checkout / billing-portal redirect guards and usage-fetch path.

### Database / migration / privilege surfaces reviewed with no new issue beyond N-01

- Full pass across migrations `004..021`, with focused re-checks on:
  - `frontend/supabase/migrations/018_i01_add_credits_lock.sql:9-78`
  - `frontend/supabase/migrations/019_i03_marker_tables_rls.sql:9-24`
  - `frontend/supabase/migrations/020_k01_charge_amount.sql:24-43`
  - `frontend/supabase/migrations/021_k02_atomic_charge_reversal.sql:37-191`
- `mariana/agent/schema.sql:6-54` — reviewed persisted `agent_tasks` / `agent_events` schema against the new M-01 in-memory fields.

## 3. Findings

### N-01 — P1 — agent billing settlement state is never persisted, so normal queued tasks skip settlement entirely

- **Severity:** P1
- **Surface:** agent billing / queue consumer / task persistence
- **Root cause:**
  - The M-01 fix added `reserved_credits` and `credits_settled` only to the Pydantic model (`mariana/agent/models.py:142-155`) and populated `reserved_credits` on the in-memory task during `POST /api/agent` (`mariana/agent/api_routes.py:450-464`).
  - But both DB writers still persist only the legacy `agent_tasks` columns: the initial insert in `_insert_agent_task` (`mariana/agent/api_routes.py:78-120`) and the runtime UPSERT in `_persist_task` (`mariana/agent/loop.py:80-135`) have no columns/values for `reserved_credits` or `credits_settled`.
  - The backing table also has no such columns (`mariana/agent/schema.sql:6-33`).
  - The reload path `_load_agent_task` selects only the legacy columns and reconstructs `AgentTask` without the new billing fields (`mariana/agent/api_routes.py:123-177`), so every reloaded task silently falls back to the model defaults `reserved_credits=0` and `credits_settled=False`.
  - The real worker path always reloads a fresh task from Postgres before execution (`mariana/main.py:745-746, 776-784`). When that reloaded task reaches the terminal-state `finally:` block, `_settle_agent_credits` returns immediately on `task.reserved_credits <= 0` (`mariana/agent/loop.py:292-293, 885-900`).
  - The new unit tests miss this because they call `_settle_agent_credits` directly on an in-memory task object and never exercise the insert → reload → queue-consumer path (`tests/test_m01_agent_billing_unit.py:147-367`).
- **Exploit / impact:**
  1. A user starts a normal agent run with a large budget; `POST /api/agent` deducts the up-front reservation using the new canonical formula.
  2. The queue consumer then reloads the task from Postgres, but the reservation metadata has already been dropped by the persistence layer.
  3. On DONE / FAILED / HALTED, settlement is skipped because the worker sees `reserved_credits == 0`.

  This means the common asynchronous path still fails to reconcile reserved credits at all. Low-spend, failed, or immediately halted runs can keep nearly the entire reservation with no refund, while overruns are never additionally deducted either. Because `credits_settled` is also not persisted, crash/restart/requeue paths have the same defect. In practice, M-01's 5x multiplier is fixed, but the promised completion-time settlement is not actually wired into the persisted worker path.
- **Suggested fix sketch:**
  1. Persist `reserved_credits` and `credits_settled` in `agent_tasks` with a migration, and thread them through every insert/select/upsert path (`_insert_agent_task`, `_load_agent_task`, `_persist_task`, and any schema bootstrap).
  2. If a schema change is truly off-limits, serialize both fields into an already-persisted JSON shape that survives `_load_agent_task`; the current in-memory-only approach is incompatible with the queue consumer.
  3. Add an integration regression test that covers the real path: reserve credits in `POST /api/agent` → write task row → reload via `_load_agent_task` / queue daemon → finish task → assert refund/extra-deduct occurs exactly once.
  4. Add a restart/requeue test proving `credits_settled` survives a persisted terminal-state reload.

## 4. Additional rationale / no-second-finding notes

I specifically looked for the edge cases called out in the task brief around the M-01 fix: re-entrancy, HALTED-state settlement, settlement-before-persist ordering, RPC-failure behavior, `int(budget_usd * 100)` truncation, negative / oversized budgets, direct `task.spent_usd` manipulation, and orphan deductions on pre-enqueue failures. The only high-confidence issue I found was the persistence gap above.

I did **not** promote a second finding in the Stripe / research-task / frontend / migration surfaces. The billing/RLS work from migrations `018..021` still appears correctly locked down, and the frontend still consistently presents the canonical `100 credits = $1` conversion.

RE-AUDIT #10 COMPLETE findings=1 file=loop6_audit/A15_phase_e_reaudit.md