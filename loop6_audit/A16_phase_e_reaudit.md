# A16 — Phase E re-audit #11

## 1. Header

- **Model:** claude_opus_4_7
- **Branch / commit:** `loop6/zero-bug` @ `2b3db0c`
- **Scope:** diff review for commits `ee6e329..2b3db0c` (the N-01 fix), plus
  full passes on `mariana/agent/api_routes.py`, `mariana/agent/loop.py`,
  `mariana/agent/models.py`, `mariana/agent/schema.sql`,
  `mariana/agent/planner.py`, `mariana/agent/dispatcher.py`,
  `mariana/api.py` (Stripe webhook surface, RPC wrappers, credit helpers),
  `mariana/main.py` (research consumer + agent queue daemon at lines
  406-502 and 738-827), Supabase migrations `004..021`, and the regression
  tests `tests/test_n01_settlement_persistence.py` /
  `tests/test_m01_agent_billing_unit.py`.

## 2. Surface walkthrough (with file:line refs)

### N-01 fix surface

- `mariana/agent/schema.sql:6-56` — `agent_tasks` adds `reserved_credits
  BIGINT NOT NULL DEFAULT 0` and `credits_settled BOOLEAN NOT NULL DEFAULT
  FALSE` inside the `CREATE TABLE IF NOT EXISTS`, then re-asserts both via
  `ALTER TABLE … ADD COLUMN IF NOT EXISTS` immediately after the table
  block. Both ALTERs run in the same `init_schema` transaction
  (`mariana/data/db.py:610-625`) **before** any agent INSERT can land
  because `init_schema` completes during FastAPI startup
  (`mariana/api.py:319-330`) and the queue daemon also calls `init_schema`
  before pulling the first task. Fresh-DB and in-place-upgrade paths both
  end with the columns present.
- `mariana/agent/api_routes.py:78-127` — `_insert_agent_task` declares 23
  columns and 23 placeholders, with `reserved_credits, credits_settled` as
  `$13, $14`; the param tuple at lines 104-126 lines up: `task.id …
  task.spent_usd, task.reserved_credits, task.credits_settled,
  task.max_fix_attempts_per_step …`. No off-by-one on placeholders.
- `mariana/agent/api_routes.py:130-192` — `_load_agent_task` SELECT lists
  the same 23 columns in the same order; the reconstruction dict reads each
  by name (`row["reserved_credits"]`, `row["credits_settled"]`) and casts
  `int()` / `bool()` before `AgentTask.model_validate(...)`.
- `mariana/agent/api_routes.py:396-505` — `start_agent_task` reservation:
  `reserved_credits = max(100, int(body.budget_usd * 100))` (canonical
  100c/USD, M-01); guarded behind `cfg.SUPABASE_URL and
  cfg.SUPABASE_ANON_KEY`; on `_supabase_deduct_credits` returning
  `"insufficient"` raises 402; on `"error"` falls back to
  `reserved_credits = 0` and proceeds; refund-on-DB-failure guard wraps
  `_insert_agent_task`. `AgentTask` is built with
  `reserved_credits=reserved_credits` and the implicit
  `credits_settled=False`.
- `mariana/agent/loop.py:80-147` — `_persist_task` UPSERT: 23 columns / 23
  placeholders match `_insert_agent_task`. The `ON CONFLICT (id) DO UPDATE
  SET` clause at lines 110-122 covers `state, steps, artifacts, spent_usd,
  reserved_credits, credits_settled, replan_count, total_failures,
  final_answer, stop_requested, error, updated_at`. `created_at,
  user_id, conversation_id, goal, …` are intentionally omitted from SET
  (immutable on update).
- `mariana/agent/loop.py:281-423` — `_settle_agent_credits`:
  short-circuits on `task.credits_settled or task.reserved_credits <= 0`;
  flips `credits_settled = True` before every RPC return path (lines 320,
  332, 362, 392, 416) so that any subsequent retry observes the flag. The
  RPC bodies use the correct param shapes: `deduct_credits` →
  `{"target_user_id", "amount"}`; `add_credits` → `{"p_user_id",
  "p_credits"}` — matches migrations 007 / 018.
- `mariana/agent/loop.py:884-925` — `run_agent_task` `finally:` only
  triggers settle + persist when `is_terminal(task.state)`. The terminal
  state was already set in the `try:` (lines 768, 819, 880, 889 set
  `FAILED`; lines 705 / 789 / 796 / 843 transition to `DONE` / `HALTED`
  via `_transition` which already calls `_persist_task`). Settlement runs
  before the second `_persist_task` so `credits_settled=True` lands in the
  same UPSERT as the terminal-state record.
- `mariana/agent/models.py:116-176` — `AgentTask` has
  `reserved_credits: int = 0` and `credits_settled: bool = False` defaults
  (matching the SQL DEFAULTs). `model_config = ConfigDict(extra="forbid")`
  rejects unknown fields, so any future SELECT drift would crash on
  `model_validate` rather than silently drop columns.

### Queue consumer + concurrency surface

- `mariana/main.py:738-827` — `_run_agent_queue_daemon`:
  - lines 752-771: v3.6 stuck-task recovery — at startup, `SELECT id,
    state FROM agent_tasks WHERE state NOT IN ('done','failed','halted',
    'cancelled','stopped') AND updated_at < NOW() - INTERVAL '60 seconds'
    LIMIT 500` followed by `redis_client.rpush("agent:queue", tid)`. The
    recovery filter explicitly excludes terminal states, so a task whose
    `finally:` already set `state=DONE/FAILED/HALTED` and persisted that
    state will not be re-queued even if its `_persist_task`-of-credits
    crashed afterwards.
  - lines 773-786: `_run_one(task_id)` re-loads the task from Postgres
    via `_load_agent_task`. Because of N-01, `reserved_credits` and
    `credits_settled` round-trip through DB so `_settle_agent_credits`
    sees the correct values on a requeue.
- The `agent:queue` Redis list has no per-task claim primitive
  (no `SETNX agent:claim:{task_id}` in `_run_one` or the BLPOP path).
  This is a pre-existing architectural property, not an N-01 regression
  (and any concrete double-credit risk it might enable is bounded by
  `credits_settled` being read from the freshly-loaded row, which the
  current architecture's single-orchestrator deployment makes effectively
  serial).

### Adjacent surfaces re-walked with no new issue

- `mariana/agent/planner.py:561-678` — `_estimate_cost` is bounded
  non-negative (Opus/Sonnet/Gemini/DeepSeek price tables, falls back to
  `0.0`). `task.spent_usd += cost` therefore never goes negative; the
  `int(spent_usd * 100)` call in `_settle_agent_credits` cannot produce a
  negative `final_tokens` to invert delta-sign.
- `mariana/agent/dispatcher.py:81-235` — dispatch table validates tool
  name, `vault_env` redaction wraps tool results, no new arg surface.
- `mariana/api.py:6905-7123` — `_supabase_add_credits` /
  `_supabase_deduct_credits` still use `_supabase_api_key(cfg)` with
  service-role priority and three-state results (`ok` / `insufficient` /
  `error`). No regression.
- `mariana/api.py:6084-6839` — Stripe webhook grant + reversal flows are
  unchanged from L-01 / K-01 / K-02 baselines (atomic
  `process_charge_reversal` RPC + `stripe_payment_grants` linkage).
- `mariana/main.py:406-502` — research-task settlement helper still uses
  the same 100c/USD canonical conversion as the agent path, providing the
  consistency baseline M-01 aligned to.
- Migrations `004..021` re-spot-checked: REVOKE/GRANT posture on
  `add_credits` / `deduct_credits` (mig 005) is intact; advisory-lock
  hygiene in `add_credits` (mig 018), `refund_credits` (mig 009),
  `process_charge_reversal` (mig 021) intact; `stripe_payment_grants`
  (mig 017) and `stripe_dispute_reversals` (mig 017 / 020) intact.

### Concurrency + idempotency probes around the N-01 fix

I specifically chased the four classes of failure called out in the brief:

1. **`_persist_task` UPSERT clobbering `credits_settled=True` back to
   False under a concurrent writer.** The SET clause does include
   `credits_settled = EXCLUDED.credits_settled`, so a concurrent in-flight
   writer with `task.credits_settled=False` in memory would in principle
   overwrite a previously-persisted `True`. In practice this requires two
   `run_agent_task` invocations for the same task ID to be live
   simultaneously, which is gated by:
   (a) the v3.6 recovery only re-queues tasks whose state is **not**
       terminal, and the only path that sets `credits_settled=True` is
       `_settle_agent_credits`, which is itself only reached from the
       terminal-state branch of the `finally:` block — by which point
       `task.state` is already `DONE` / `FAILED` / `HALTED` and that
       state was persisted by the calling code path (`_transition`
       and/or the `try:` block setters at lines 768, 819, 880, 889).
   (b) BLPOP on a single Redis queue key cannot deliver the same item
       twice from a single push; double-execution requires a double
       `rpush`. The recovery query is the only `rpush` source other
       than `start_agent_task`'s `_enqueue_agent_task`, and (a) shows
       it cannot fire on a task whose `credits_settled` is True.
   So the documented clobber path is unreachable on the persisted state
   machine. I considered the multi-orchestrator deployment scenario
   noted by B-21 (`In-process rate limiter not shared across
   workers/instances`) but that is a previously-known architectural
   constraint, not a fresh defect of N-01.

2. **RPC-succeeds-but-persist-fails → flag never persisted → requeue
   double-refunds.** Verified the `finally:` ordering at
   `mariana/agent/loop.py:898-914`: settle (sets in-memory
   `credits_settled=True`), then `_persist_task`. Even if the second
   persist raises (caught silently by the `except Exception: pass` at
   913-914), the row's terminal state was already persisted by the
   triggering code path, so the recovery filter (`state NOT IN
   ('done','failed','halted',…)`) excludes it — no requeue, no
   double-refund. Documented tradeoff: a manual operator requeue would
   re-settle, but that is operator action, not a vulnerability.

3. **RPC fails but flag set → permanent loss but no double-charge.**
   Confirmed: every RPC path in `_settle_agent_credits` flips
   `task.credits_settled = True` *before* checking
   `resp.status_code` (lines 362, 392). The catch-all
   `except Exception` at 412-423 also sets the flag. So a failed RPC
   never opens a retry window. The chosen tradeoff (no double-charge >
   maybe-stuck-refund) is documented in the function docstring at
   281-302.

4. **Two writers race the `credits_settled` flag.** Per (1) above,
   reaching `_settle_agent_credits` requires terminal `task.state`,
   which is persisted before the finally block runs. The recovery filter
   prevents requeue once that persisted state is terminal. No claim/lock
   in `_run_one` is currently necessary because the persisted-state
   gate is already serializing.

### Test-coverage observation (not a runtime defect)

`tests/test_n01_settlement_persistence.py` validates the four pieces N-01
actually changed: schema columns, INSERT round-trip, UPSERT round-trip,
and the documented settle → persist sequence. It deliberately uses the
real local Postgres on `PGHOST=/tmp PGPORT=55432` (consistent with the
test brief). Tests #5 and #6 re-implement the queue-consumer order
manually — they do not call `run_agent_task` end-to-end, so a future
refactor that drops the `_settle_agent_credits` invocation from the
finally block would not be caught here. That is a coverage gap rather
than a live bug; it does not constitute a P1-P4 finding under the brief's
rubric.

## 3. Findings

ZERO findings.

## 4. Rationale for ZERO findings

I scrutinized the following high-risk areas, with the brief's specific
prompts in mind, and could not promote any of them to a defensible new
finding:

- **Schema-bootstrap order.** `init_schema` runs both `CREATE TABLE IF NOT
  EXISTS` and `ALTER TABLE … ADD COLUMN IF NOT EXISTS` for both new
  columns before any HTTP request can be served, and before
  `_run_agent_queue_daemon` issues its first BLPOP. Cannot construct a
  window where an INSERT precedes the ALTER on a fresh deploy.
- **INSERT / SELECT / UPSERT placeholder renumbering.** Re-counted twice
  on each of `_insert_agent_task`, `_load_agent_task`, and
  `_persist_task`. 23-column / 23-placeholder shape matches
  `task.*` argument order exactly, with `reserved_credits` /
  `credits_settled` consistently in slots 13/14.
- **`_persist_task` UPSERT SET coverage.** Includes both new columns;
  the omitted columns (`created_at`, identity / immutables) are correctly
  not updated.
- **Settlement ordering / failure modes.** Validated under all four
  failure-mode classes from the brief. The chosen RPC-failure tradeoff
  (mark settled, log error, never retry) is internally consistent with
  the function docstring and consistent with how
  `mariana/main.py:_deduct_user_credits` handles the same sequence for
  research tasks.
- **Concurrency between queue consumer / API stop / SSE.** The stop
  endpoint only writes `stop_requested = TRUE` (it does not touch
  `credits_settled`); SSE is read-only; there is no delete path. The
  only concurrent writer of `credits_settled` is `_persist_task` via
  `run_agent_task`, and the v3.6 recovery filter guarantees only one
  such invocation can be alive once the row's `state` reaches a
  terminal value.
- **`int(budget_usd*100)` boundaries.** `budget_usd` is bounded by
  `Field(ge=0.1, le=100.0)`, so reserved credits stay in `[100, 10000]`
  — no integer overflow, no negative reservation. `int(spent_usd*100)`
  is non-negative because `task.spent_usd` only accumulates non-negative
  costs from `_estimate_cost`.
- **Settlement RPC params.** `deduct_credits` keys
  (`target_user_id`, `amount`) and `add_credits` keys (`p_user_id`,
  `p_credits`) match migrations 007 and 018. No B-22-style param-name
  drift.
- **Migration retroactivity.** The `ALTER TABLE … ADD COLUMN IF NOT
  EXISTS … DEFAULT 0 / FALSE` will backfill any pre-N-01 rows with
  `reserved_credits=0`, so historical rows from the M-01-only window
  whose users were already deducted credits will quietly skip
  settlement. This is a pre-existing M-01 data-migration consequence
  (those tasks pre-dated the persistence layer altogether), not a
  new defect introduced by 2b3db0c.
- **Stripe webhook surface, F03/F04/F05/H01/H02/I01-03/J01-02/K01-02/
  L01 baselines** — no new code paths in the diff range; spot checks
  confirm those families are still locked down.
- **Frontend / IDOR / SSE auth / vault** — diff range
  `ee6e329..2b3db0c` contains no frontend or vault changes.
- **Prompt injection / SSRF / SQLi / XSS / CSRF / RLS / IDOR / secret
  exposure** — no new attack surface introduced by N-01; the schema
  ALTER and Python-side INSERT/SELECT/UPSERT use parameterised queries
  with no string concatenation, no JSON-from-untrusted-input parsing
  changed, no new endpoints added, no auth dependencies relaxed.

The N-01 fix is tight: it correctly persists the M-01 settlement metadata
across the queue-consumer reload path; the placeholder renumbering is
clean; the UPSERT SET clause is complete; the settle-then-persist
ordering inside `run_agent_task`'s `finally:` block locks settlement to
the same UPSERT that records the terminal state; and the test suite at
`tests/test_n01_settlement_persistence.py` exercises the actual schema
bootstrap and DB round-trip rather than mocking the database layer.

RE-AUDIT #11 COMPLETE findings=0 file=loop6_audit/A16_phase_e_reaudit.md
