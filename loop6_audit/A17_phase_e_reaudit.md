# A17 — Phase E re-audit #12

## 1. Header

- **Model:** gpt_5_4
- **Commit:** `e962765`
- **Scope:** re-audit of `/home/user/workspace/mariana` on branch `loop6/zero-bug` at commit `e962765`, with required re-read of `loop6_audit/REGISTRY.md`, `A15_phase_e_reaudit.md`, `A16_phase_e_reaudit.md`, and `N01_FIX_REPORT.md`; full walkthrough of `mariana/agent/*`, `mariana/api.py`, `mariana/main.py`, `frontend/src/{pages,components}/**/*`, and migrations `004..021`, with adversarial focus on task lifecycle, settlement, recovery, cancellation, SSE isolation, frontend/backend credit math, and migrations `020/021`.

## 2. Surface walkthrough / explicit challenges to A16

### Changed-files map

- `git diff --name-only 2b3db0c..e962765` shows no product-code delta after A16; only `loop6_audit/A16_phase_e_reaudit.md` changed. This re-audit therefore re-challenged A16’s zero-findings conclusions against the current tree rather than a new code diff.

### A16 claim 1 challenged: schema bootstrap ordering is safe

- Re-checked API startup: `mariana/api.py:320-327` creates the DB pool and runs `init_schema(_db_pool)` inside FastAPI lifespan before startup completes.
- Re-checked daemon startup: `mariana/main.py:1188-1190` creates the pool and calls `_ensure_db_modules`, and `_ensure_db_modules` immediately calls `init_schema(db)` at `mariana/main.py:113-123` before daemon tasks start.
- Re-checked queue consumer ordering: `_run_agent_queue_daemon` only starts later (`mariana/main.py:1207-1216`), so in the normal long-running daemon topology A16 was right that schema init precedes the first BLPOP.
- Re-checked `init_schema` itself: `mariana/data/db.py:620-625` executes both the shared schema SQL and `mariana/agent/schema.sql`; `mariana/agent/schema.sql:51-52` keeps the idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for `reserved_credits` / `credits_settled`.
- I did not find a concrete fresh defect in the standard API/daemon topology. I am not promoting a finding on speculative serverless cold-start topology alone.

### A16 claim 2 challenged: settle-then-persist / recovery / cancel paths are safe

- Verified the recovery filter exists exactly as A16 said: `mariana/main.py:758-761` requeues only rows with `state NOT IN ('done','failed','halted','cancelled','stopped')`.
- Verified settlement still happens only in the terminal-state finally path: `mariana/agent/loop.py:897-914` calls `_settle_agent_credits(task)` only if `is_terminal(task.state)`.
- However, the stop/cancel path is weaker than A16 claimed: `mariana/agent/api_routes.py:756-771` only sets `stop_requested = TRUE`; it does **not** transition the task to a terminal state or trigger settlement.
- Recovery ignores `stop_requested` entirely and will requeue such rows later because it filters only on `state` (`mariana/main.py:758-767`).
- `run_agent_task` does not check `stop_requested` before planning. It persists the task and executes `planner.build_initial_plan(task)` first (`mariana/agent/loop.py:760-765`), adds planner cost to `spent_usd` (`772`), and only checks `_check_stop_requested` inside the execute loop at `786-790`. This produced finding O-02.

### A16 claim 3 challenged: run_agent_task E2E gap is “coverage-only”

- Hand-tracing `run_agent_task` confirmed A16’s narrow point that the N-01 persistence bug is fixed: `_insert_agent_task`, `_load_agent_task`, `_persist_task`, schema columns, and final settlement ordering all round-trip the new fields (`mariana/agent/api_routes.py:78-192`, `mariana/agent/loop.py:80-147`, `281-423`, `897-914`, `mariana/agent/schema.sql:6-56`).
- I did not find a live negative-`spent_usd`, post-DONE spend bump, or swallowed-settle defect. `_estimate_cost` remains non-negative and returns `0.0` for unknown models (`mariana/agent/planner.py:561-574`), and all `spent_usd += cost` sites add those non-negative values.
- But the end-to-end hand trace did expose a different runtime bug in the lifecycle: cancelled queued tasks still pay the planner cost before the stop is honored (O-02).

### A16 claim 4 challenged: adjacent pricing / frontend / SSE / migrations had no regressions

- **Planner cost non-negativity / NaN / Infinity:** no fresh bug found. `_estimate_cost` casts token counts with `int(...)` and uses fixed positive constants, then falls back to `0.0` (`mariana/agent/planner.py:561-574`).
- **`budget_usd` bounds / overflow:** no fresh bug found. `AgentStartRequest.budget_usd` is constrained to `Field(ge=0.1, le=100.0)` at `mariana/agent/api_routes.py:45-58`, so the backend reservation math cannot overflow the `BIGINT` reservation column.
- **`reserved_credits` negative via app path:** no fresh bug found. I found no RPC or route that lets untrusted callers set `reserved_credits` directly; the backend computes it from bounded `budget_usd`.
- **Migrations 020/021 backward compatibility:** no fresh bug found. Migration `020_k01_charge_amount.sql:18-21, 26-41` explicitly allows `charge_amount IS NULL` on legacy rows, and `mariana/api.py:6628-6639` falls back with a warning rather than treating NULL as zero. `021_k02_atomic_charge_reversal.sql:37-176` still serializes per charge and uses `COALESCE(SUM(credits), 0)`.
- **SSE user isolation:** no fresh cross-user leak found. Stream auth is task-scoped (`mariana/api.py:1378-1457`, `mariana/agent/api_routes.py:624-650`), and `/api/agent/{task_id}/stream` verifies `task.user_id == current_user['user_id']` before replaying DB events or reading the Redis stream (`mariana/agent/api_routes.py:640-730`). The Redis stream key is `agent:{task_id}:events` (`mariana/agent/loop.py:58-63, 187-190`), not user-scoped, but ownership checks still gate access.
- **Frontend/backend pricing consistency:** A16’s “frontend consistent” conclusion does not hold for the agent run ceiling. The frontend allows and displays sub-100-credit ceilings, while the backend reserves with a hard 100-credit floor. This produced finding O-01.

### Other surfaces scrutinized with no new finding

- `mariana/agent/api_routes.py:130-192, 535-587, 640-771` — task load, event replay, SSE, stop endpoint.
- `mariana/agent/loop.py:150-225, 252-423, 726-925` — event emission/redaction, stop checks, settlement, main task lifecycle.
- `mariana/main.py:738-827` — queue consumer, stuck-task recovery, BLPOP loop.
- `frontend/src/components/deft/studio/stage.ts:89-96`, `frontend/src/pages/Build.tsx:365-399`, `frontend/src/components/deft/LiveCanvas.tsx:116-118`, `frontend/src/components/deft/studio/StudioHeader.tsx:126-135` — frontend credit display.
- `frontend/src/pages/Pricing.tsx:298-347`, `frontend/src/components/deft/account/AccountView.tsx:133-196`, `frontend/src/lib/agentApi.ts:75-80` — canonical `1 credit = $0.01` copy remains consistent.
- `frontend/src/lib/streamAuth.ts:1-102`, `mariana/api.py:1378-1457` — stream-token mint/verify path.
- `mariana/billing/quote.py:23-123`, `frontend/src/components/deft/PreflightCard.tsx:118-153, 257-315, 347-404`, `frontend/src/lib/agentRunApi.ts:11-53` — preflight quote / ceiling path.
- `mariana/api.py:6905-7123` — credit add/deduct RPC wrappers.
- `frontend/supabase/migrations/020_k01_charge_amount.sql`, `021_k02_atomic_charge_reversal.sql` — dispute/refund accounting path.

## 3. Findings

### O-01 — P2 — frontend allows and displays sub-100-credit agent ceilings, but backend always reserves at least 100 credits

- **Severity:** P2
- **Surface:** frontend / agent billing / credit reservation math
- **Root cause:**
  - The frontend preflight allows ceilings well below 100 credits. `PreflightCard` derives `ceilingMin = max(1, floor(quote.credits_min * 0.5))` and lets the user enter or slide to any integer at or above that value (`frontend/src/components/deft/PreflightCard.tsx:145-153, 257-315`).
  - The quote engine can legitimately return sub-100-credit runs: lite baseline is only 60 credits and `credits_min = round(baseline * 0.6 * complexity)` with complexity bounded as low as 0.5 (`mariana/billing/quote.py:23-29, 60-77, 99-105`). So the UI will commonly present ceilings in the tens of credits for simple lite prompts.
  - `startAgentRun` converts the chosen ceiling directly to `budget_usd = ceilingCredits / 100`, clamped only to `0.1..100.0` dollars (`frontend/src/lib/agentRunApi.ts:40-52`).
  - The backend then reserves `max(100, int(body.budget_usd * 100))` regardless of the user-selected ceiling (`mariana/agent/api_routes.py:441-458`). Any ceiling from 1 to 99 credits still reserves 100.
  - After start, the frontend continues to display the task ceiling as `creditsFromUsd(task.budget_usd)` / `round(task.budget_usd * 100)` (`frontend/src/pages/Build.tsx:365-399`, `frontend/src/components/deft/studio/stage.ts:89-96`, `frontend/src/components/deft/LiveCanvas.tsx:116-118`, `frontend/src/components/deft/studio/StudioHeader.tsx:126-135`), which shows the user’s selected sub-100 value rather than the actual reservation hold.
- **Exploit / impact:**
  1. User gets a lite quote in the tens of credits and sets a ceiling such as 40 or 50 credits.
  2. Frontend presents that as the run ceiling and sends `budget_usd = 0.40` / `0.50`.
  3. Backend reserves 100 credits anyway.

  Two concrete failures follow:
  - **False insufficient-credit rejection:** a user with e.g. 80 credits can be shown a valid 40-credit ceiling in preflight, yet `POST /api/agent` still returns 402 because the backend tries to deduct 100.
  - **Hidden over-reservation:** a user with enough balance to pass start sees a 40-credit ceiling in the UI, but 100 credits are held up front until terminal settlement. That is exactly the frontend/backend mismatch the brief asked to re-check.
- **Fix sketch:**
  1. Pick one canonical rule and enforce it everywhere. Either:
     - backend keeps the 100-credit minimum and the frontend enforces/displays a minimum ceiling of 100 credits, **or**
     - backend removes the 100-credit floor so reservation equals the chosen ceiling exactly.
  2. If the 100-credit minimum is intentional, expose it explicitly in preflight and post-start UI by showing both `selected ceiling` and `initial reservation` rather than silently mapping one to the other.
  3. Add an integration test covering a sub-100 ceiling (e.g. 40 credits) and asserting that the displayed amount, start payload, backend deduction, and task header all agree.

### O-02 — P2 — cancelling a queued agent task does not settle it and still allows planner charges after stop is requested

- **Severity:** P2
- **Surface:** agent lifecycle / cancel / recovery / billing settlement
- **Root cause:**
  - `POST /api/agent/{task_id}/stop` only does `UPDATE agent_tasks SET stop_requested = TRUE, updated_at = now()` and optionally writes a Redis stop key (`mariana/agent/api_routes.py:743-771`). It does **not** transition the task to a terminal state, does not requeue it immediately, and does not call settlement.
  - Settlement still occurs only in the terminal-state `finally:` branch (`mariana/agent/loop.py:897-914`), so a merely-stopped queued task keeps its reservation until some worker eventually runs it to `HALTED`.
  - Startup recovery requeues every stale non-terminal row regardless of `stop_requested` (`mariana/main.py:752-767`). A stopped task in `plan` or `execute` state is therefore requeued later.
  - When the worker finally loads that task, `run_agent_task` does **not** check `stop_requested` before planning. It persists the task, emits `state_change`, then calls `planner.build_initial_plan(task)` (`mariana/agent/loop.py:760-765`), increments `task.spent_usd += cost` (`772`), and only then reaches the first `_check_stop_requested` at `786-790`.
  - `start_agent_task` explicitly accepts enqueue failure / no-Redis degraded mode and still returns 202 after reservation (`mariana/agent/api_routes.py:507-531`), which makes this stop-path weakness user-reachable without exotic races.
- **Exploit / impact:**
  1. User starts a task; credits are reserved up front.
  2. Before a worker starts it, user presses Cancel / Stop.
  3. The row is left non-terminal with `stop_requested = TRUE`, so no settlement happens yet.
  4. On later recovery, the queue daemon requeues it anyway because recovery filters only on `state`, not `stop_requested`.
  5. The worker honors the stop **after** the planner call, so the cancelled task still incurs planner spend and only then halts.

  This breaks the expected “cancel before work starts” contract in two ways:
  - queued cancelled tasks can keep their reservation locked until a worker eventually touches them;
  - once a worker does touch them, they still burn at least one planner round-trip before the stop is honored, so cancellation can directly consume credits.
- **Fix sketch:**
  1. In `stop_agent_task`, transition queued/non-running tasks directly to a terminal halted/cancelled state and settle immediately instead of leaving them as non-terminal `stop_requested=TRUE` rows.
  2. In `run_agent_task`, add an early `_check_stop_requested` gate before any planning work (`before build_initial_plan`) so a recovered cancelled task exits without additional spend.
  3. In stuck-task recovery, exclude `stop_requested = TRUE` rows from blind requeue, or requeue them only into a lightweight settlement path that does not execute planning.
  4. Add integration coverage for: reserve → stop before worker start → recovery requeue → assert zero planner spend and exactly-once refund/settlement.

## 4. Additional rationale / what else I scrutinized

Beyond the two findings above, I specifically re-checked all adversarial prompts from the brief and did **not** find defensible fresh issues in:

- schema bootstrap ordering (`init_schema` before normal API/daemon request handling);
- N-01 persistence of `reserved_credits` / `credits_settled` across insert, reload, UPSERT, and final settlement;
- settle-then-persist idempotency once a task is already terminal;
- `spent_usd` negativity / NaN / Infinity paths;
- Pydantic `budget_usd` bounds and integer-overflow risk;
- RPC param-name drift versus migrations 007/018/020/021;
- cross-user SSE leakage or stream-token auth bypass;
- migration 020 legacy-NULL `charge_amount` handling;
- migration 021 atomic reversal math / role grants;
- vault redaction of event payloads (`mariana/agent/loop.py:150-167, 747-748`).

RE-AUDIT #12 COMPLETE findings=2 file=loop6_audit/A17_phase_e_reaudit.md
