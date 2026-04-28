# A18 — Phase E re-audit #13

## 1. Header

- **Model:** gpt_5_4
- **Commit:** `29b897f`
- **Scope:** re-audit of `/home/user/workspace/mariana` on branch `loop6/zero-bug` at commit `29b897f`, with required re-read of `loop6_audit/REGISTRY.md`, `loop6_audit/O01_O02_FIX_REPORT.md`, and `loop6_audit/A17_phase_e_reaudit.md`; adversarial re-check of the O-01 and O-02 fixes; walkthrough of the changed files in `b3b5d57..29b897f`; targeted review of `mariana/agent/*`, `mariana/api.py`, `mariana/main.py`, `mariana/billing/*`, `frontend/src/{pages,components,lib}/`, and migrations / webhook / vault / credit-accounting surfaces.

## 2. Surface walkthrough / explicit O-01 and O-02 fix probes

### O-01 re-check: minimum reservation floor and frontend/backend alignment

- `frontend/src/components/deft/studio/stage.ts:89-108` now exports `CREDITS_MIN_RESERVATION = 100` and keeps the canonical `creditsFromUsd()` conversion at `usd * 100`.
- `frontend/src/components/deft/PreflightCard.tsx:150-162, 263-307` imports that constant, clamps the numeric input + slider minimum to 100 credits, and renders the new “Minimum reservation 100 credits” caption.
- `mariana/agent/api_routes.py:45-64` tightens `AgentStartRequest.budget_usd` to `ge=1.0`, so direct API callers can no longer submit sub-$1 budgets.
- `frontend/src/pages/Build.tsx:365-399`, `frontend/src/components/deft/studio/StudioHeader.tsx:125-140`, and `frontend/src/components/deft/LiveCanvas.tsx:116-118` still derive displayed ceiling/spend from `budget_usd * 100`, which now matches the backend reservation floor once `budget_usd >= 1.0`.
- `frontend/src/lib/agentRunApi.ts:40-52` still clamps helper output to `Math.max(0.1, ...)`, but the live Build path feeds it the clamped `PreflightCard` ceiling (`Build.tsx:254-258`), and the only other caller found was the dev-only preview route (`frontend/src/pages/DevStudio.tsx:103-125`). I did not find a new live O-01 regression from this helper alone.
- `rg -n "budget_usd" mariana frontend/src tests` showed the expected O-01 test updates in `tests/test_o01_budget_min_floor.py` and `tests/test_m01_agent_billing_unit.py`; I did not find a surviving production caller that still intentionally constructs `< 1.0` agent budgets.

### O-02 re-check: cancel terminalisation, settlement, recovery, and stale-worker races

- `mariana/agent/models.py:22-36` adds `AgentState.CANCELLED`.
- `mariana/agent/state.py:17-43` makes `CANCELLED` terminal and legal from `PLAN`, and `is_terminal()` now includes it.
- `mariana/agent/api_routes.py:756-869` reworks `POST /api/agent/{task_id}/stop` to lock the row with `FOR UPDATE`, fast-return when already terminal+settled, and inline-settle the narrow pre-execution case.
- `mariana/main.py:752-769` now excludes `stop_requested = TRUE` rows from stale-task recovery.
- `mariana/agent/loop.py:764-776` adds the new pre-planner `_check_stop_requested()` gate before `planner.build_initial_plan()`.
- The explicit stale-worker race from the brief is still real: the queue worker loads tasks through `_load_agent_task()` with a plain `SELECT` (`mariana/agent/api_routes.py:136-198`), `_run_agent_queue_daemon()` immediately hands that stale in-memory object to `run_agent_task()` (`mariana/main.py:780-792`), and `run_agent_task()` performs `_persist_task()` **before** the first stop check (`mariana/agent/loop.py:758-776`). `_persist_task()` blindly overwrites `state`, `reserved_credits`, `credits_settled`, `stop_requested`, and `error` on conflict (`mariana/agent/loop.py:80-147`). That produced the new finding below.

### Fresh surfaces checked beyond O-* fixes

- Stripe webhook dispatch / renewal / reversal paths in `mariana/api.py:5675-5748, 5830-6075, 6329-6756` were re-read. `invoice.paid` grants use the Stripe `event_id` as `ref_id`, skip `billing_reason == 'subscription_create'`, and charge/dispute reversals now go through the atomic `process_charge_reversal` RPC. I did not find a distinct new webhook idempotency or partial-refund regression beyond prior J/K/L fixes.
- Vault runtime and storage in `mariana/vault/runtime.py:59-238`, `mariana/vault/store.py:136-260`, and `mariana/vault/router.py:70-264` were checked for server-side validation and cleanup. I did not find a key-rotation selector bug because this tree does not currently implement a `select_active_key` / rotating-key chooser surface.
- Retry-loop / replanning caps in `mariana/agent/loop.py:655-686, 740-742, 849-890` still clamp to hard maxima and did not expose a fresh infinite-loop bypass in this pass.
- I also re-checked the O-02 regression tests in `tests/test_o02_cancel_settlement.py:183-410`. They cover queued cancel settlement, running-task signal-only behavior, recovery filtering, and the explicit `task.stop_requested=True` early-stop gate, but they do **not** simulate the stale-worker-loaded-before-stop race.

## 3. Findings

### P-01 — P1 — queued-task cancel race can double-refund the same reservation and mint credits

- **Severity:** P1
- **Surface:** agent lifecycle / cancel / credit settlement / stale in-memory task race
- **Root cause:**
  - The queue worker loads the task through `_load_agent_task()` using a plain `SELECT`, with no row lock or version check (`mariana/agent/api_routes.py:136-198`; `mariana/main.py:780-792`).
  - The stop endpoint then takes `SELECT ... FOR UPDATE`, flips `stop_requested`, reloads the task, marks it `CANCELLED`, calls `_settle_agent_credits()`, and persists `credits_settled=True` (`mariana/agent/api_routes.py:784-857`).
  - But the already-loaded worker keeps its stale in-memory `AgentTask` object. `run_agent_task()` begins by calling `_persist_task()` **before** the first `_check_stop_requested()` (`mariana/agent/loop.py:758-776`). `_persist_task()` has an unconditional UPSERT that overwrites `state`, `reserved_credits`, `credits_settled`, `stop_requested`, and `error` from the stale object (`mariana/agent/loop.py:80-147`).
  - That means a worker which loaded the task before the stop can clobber the freshly persisted `CANCELLED + credits_settled=True` row back to `PLAN/HALTED + credits_settled=False`, then run its own terminal settlement in `finally:` (`mariana/agent/loop.py:912-929`).
- **Exploit / impact:**
  1. User starts an agent run, reserving e.g. 500 credits.
  2. Worker BLPOP’s the task and loads it into memory.
  3. Before the worker’s first stop check, the user hits Stop.
  4. `POST /stop` refunds the 500-credit reservation and persists `CANCELLED + credits_settled=True`.
  5. The stale worker then `_persist_task()`s its old snapshot, erasing the settled flag and terminal state.
  6. The worker sees the Redis stop key, halts, and the `finally:` block settles **again**.

  I reproduced this locally against the provided Postgres test DB by loading a stale task object, invoking the stop endpoint, then running `run_agent_task()` with the stale object. The observed result was two separate `add_credits` RPC calls for the same 500-credit reservation, and the final row ended as `HALTED` rather than `CANCELLED`.

  There is also a nearby variant where the stop key is observed slightly later: the worker can spend planner cost first, then still refund a second time from the stale reservation snapshot. Either way, the O-02 fix is not actually idempotent under the stale-worker race the brief called out.
- **Why this is new / not O-02 duplication:**
  - O-02 was about cancelled queued tasks remaining non-terminal, being requeued, and paying planner cost before stop was honored.
  - This issue survives **after** that fix: it is a new double-refund / state-clobber race caused by the interaction between the new inline cancel settlement and the unchanged stale-worker UPSERT path.
- **Fix sketch:**
  1. Do not let `run_agent_task()` write the stale snapshot before it re-validates the current DB row. The first stop/terminal check needs to happen before the initial `_persist_task()`.
  2. Add optimistic concurrency or compare-and-swap protection to `_persist_task()` (for example a version / updated_at guard), and never allow a stale non-terminal snapshot to overwrite a row that is already terminal or already `credits_settled=True`.
  3. Alternatively, reload the task under lock at worker start and abort immediately if the DB row is already terminal / settled.
  4. Add a regression test that exactly simulates: worker loads task → stop endpoint cancels+settles → worker resumes with stale object → assert exactly one refund RPC and final row remains `CANCELLED`.

## 4. Additional rationale / no-second-finding notes

I deliberately re-challenged the fix surfaces requested in the brief and did not promote separate findings for the following:

- O-01 constant export/import path: the new floor is wired into `stage.ts` and `PreflightCard.tsx`, and the main Build display path now matches the canonical backend floor.
- Direct API `< $1` budgets: the Pydantic `ge=1.0` guard is live in `AgentStartRequest`, and I did not find a production caller still constructing such requests.
- Stop endpoint idempotent fast path: `is_terminal(cur_state) and already_settled` now short-circuits correctly, but that protection only applies to the fresh DB row, not the stale worker object described in P-01.
- Recovery filtering: `AND stop_requested = FALSE` is present and working for the intended stale-row class.
- Webhook renewal / refund / dispute ordering: `invoice.paid`, `payment_intent.succeeded`, `charge.refunded`, and dispute handlers now look structurally consistent with the prior J/K/L fixes; I did not find a distinct new defect worth reporting.
- Vault runtime: server-side validation / TTL / delete-on-terminal behavior looked sane in this pass, and I found no separate cross-user leak or rotation bug in the current implementation.

RE-AUDIT #13 COMPLETE findings=1 file=loop6_audit/A18_phase_e_reaudit.md
