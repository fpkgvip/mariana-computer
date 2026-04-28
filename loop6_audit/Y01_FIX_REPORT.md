# Y-01 Fix Report — research-task settlement idempotency

Status: **FIXED 2026-04-28**
Severity: P2 (financial — refund-twice / charge-twice on daemon resume)
Branch: `loop6/zero-bug`
Pattern: mirror of T-01 for the legacy investigation pipeline.

## 1. Bug

`mariana/main.py:_deduct_user_credits` settles legacy investigation
(`research_tasks`) credit reservations after the orchestrator returns. It
called the non-idempotent low-level RPCs `add_credits(p_user_id,
p_credits)` and `deduct_credits(target_user_id, amount)` directly — no
`(ref_type, ref_id)` keying, no claim row, no `credits_settled` flag on
`research_tasks`. The agent-side T-01 fix routed the symmetric path
through idempotent `grant_credits` / `refund_credits` plus
`agent_settlements.ledger_applied_at`; the legacy research-task path was
never migrated.

### Reproducer

1. Submit an investigation; reservation R is deducted at submission.
2. Investigation runs to completion; orchestrator persists `total_spent`
   into `ai_sessions`. `_run_single` calls
   `_deduct_user_credits(reserved=R, final=A1)` — RPC succeeds, applies
   delta1 = A1 − R.
3. Daemon process is SIGKILL'd (OOM, k8s pod replacement,
   `oom_score_adj`) BEFORE `task_file.rename(.done)` at `main.py:738`.
4. Daemon restarts. Resume path at `main.py:944-1024` picks up the
   `.running` file and calls `_run_single_guarded` again.
5. `_run_single` re-enters the orchestrator; checkpoint resume restores
   `task.current_state = HALT` and `cost_tracker.total_spent = A1` (from
   `ai_sessions`). Main loop body skipped because state is already
   terminal.
6. `_deduct_user_credits(reserved=R, final=A1)` is called again with the
   SAME inputs. Delta = A1 − R applied AGAIN by the non-idempotent RPC.

Net financial impact:
* If A1 < R: refund-twice → user under-billed by R − A1.
* If A1 > R: extra-deduct-twice → user over-billed by A1 − R.

## 2. Why prior fixes did not catch it

* **T-01** addressed the agent-mode settlement (`mariana/agent/loop.py`)
  with claim row, `ledger_applied_at`, and idempotent ledger primitives.
  Its commit message and fix report explicitly call out the agent path.
  The legacy `mariana/main.py` settlement path was overlooked because
  `add_credits` / `deduct_credits` calls remained scoped to it.
* **U-02** corrected the float→int truncation in
  `_deduct_user_credits` but kept the existing RPC choice — its scope
  was the cents-quantization, not the idempotency contract.
* **A29** and **A30** marked T-01 territory clean by spot-checking the
  agent path without enumerating the symmetric research path.

## 3. Fix

### 3.1 Schema — backend Postgres only

`research_tasks` and `research_settlements` live in backend Postgres
(initialised by `init_schema()` from
`mariana/data/db.py:_SCHEMA_SQL`); no Supabase migration is required.
Applied to local Postgres via the same `init_schema()` call.

```sql
ALTER TABLE research_tasks
    ADD COLUMN IF NOT EXISTS credits_settled BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS research_settlements (
    task_id            TEXT PRIMARY KEY REFERENCES research_tasks(id) ON DELETE RESTRICT,
    user_id            TEXT NOT NULL,
    reserved_credits   BIGINT NOT NULL CHECK (reserved_credits >= 0),
    final_credits      BIGINT NOT NULL CHECK (final_credits >= 0),
    delta_credits      BIGINT NOT NULL,
    ref_id             TEXT NOT NULL,
    claimed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    ledger_applied_at  TIMESTAMPTZ,
    completed_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_research_settlements_completed
    ON research_settlements(completed_at) WHERE completed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_research_settlements_ledger_applied_pending_complete
    ON research_settlements(ledger_applied_at)
    WHERE completed_at IS NULL AND ledger_applied_at IS NOT NULL;
```

The PRIMARY KEY `(task_id)` plus INSERT…ON CONFLICT DO NOTHING is the
once-only fence. ON DELETE RESTRICT keeps settlement history immutable
across task UUID reuse (mirrors S-04 for `agent_settlements`).

### 3.2 Code refactor — `mariana/main.py`

`_deduct_user_credits` gains optional `task_id` and `db` kwargs and is
restructured to mirror `_settle_agent_credits`:

1. **Existing-claim lookup.** If the row has `completed_at IS NOT NULL`,
   short-circuit. If `ledger_applied_at IS NOT NULL`, marker-fixup
   (`_mark_research_settlement_completed`) and return — no RPC.
2. **Atomic claim.** `_claim_research_settlement` does INSERT…ON
   CONFLICT DO NOTHING RETURNING task_id. Loser of the race exits.
3. **Idempotent ledger RPC.**
   * `delta < 0` → `POST /rest/v1/rpc/grant_credits` with
     `(p_source='refund', p_ref_type='research_task',
     p_ref_id=task_id)`.
   * `delta > 0` → `POST /rest/v1/rpc/refund_credits` with
     `(p_ref_type='research_task_overrun', p_ref_id=task_id)`.
   Both already deduplicate on `(type, ref_type, ref_id)` against
   `credit_transactions` per T-01.
4. **Two-step durable finalisation.** After 2xx,
   `_mark_research_ledger_applied` stamps `ledger_applied_at`, then
   `_mark_research_settlement_completed` stamps `completed_at` and
   flips `research_tasks.credits_settled`. `task.credits_settled` is
   never set on RPC success alone.

The three call sites in `_run_single` (success, KeyboardInterrupt,
exception) thread `task.id` and `db` through.

### 3.3 Reconciler — `mariana/research_settlement_reconciler.py`

Atomic claim via `UPDATE … SET claimed_at = now() WHERE
claimed_at < now() - interval` plus inner SELECT with
`FOR UPDATE SKIP LOCKED`. Per-row branch:

* `ledger_applied_at IS NOT NULL` → marker fix-up via
  `_mark_research_settlement_completed`. NO ledger RPC.
* `ledger_applied_at IS NULL` → reconstruct a CostTracker-shaped object
  whose `total_with_markup` divides back to the recorded
  `final_credits`, then call `_deduct_user_credits` (which routes
  through the same claim row + idempotent RPC + marker writes).

Wired into `_run_daemon` alongside the agent reconciler:

```python
research_settlement_reconciler_task = asyncio.create_task(
    _run_research_settlement_reconciler_loop(db=db),
    name="research-settlement-reconciler",
)
```

Cadence shared with the agent reconciler via the existing
`AGENT_SETTLEMENT_RECONCILE_INTERVAL_S` / `_MAX_AGE_S` /
`_BATCH_SIZE` env knobs (60 s / 300 s / 50 by default).

## 4. TDD trace

### RED at `aaf79e0`

```
$ python -m pytest tests/test_y01_research_settlement_idempotency.py -x
test_y01_first_settle_keys_on_task_id FAILED
TypeError: _deduct_user_credits() got an unexpected keyword argument 'task_id'
```

### GREEN after fix

```
$ python -m pytest tests/test_y01_research_settlement_idempotency.py -x
4 passed in 0.36s

$ python -m pytest --tb=short
393 passed, 13 skipped, 0 failed
```

Baseline pre-fix was 389 passed, 13 skipped; +4 = 393 matches the four
new Y-01 regression tests with one collateral edit
(`tests/test_u02_decimal_billing.py::test_legacy_investigation_quantize`
updated to the new RPC surface — same quantization contract, new
`refund_credits` URL).

## 5. Regression tests

`tests/test_y01_research_settlement_idempotency.py` pins:

1. `test_y01_first_settle_keys_on_task_id` — refund path issues a single
   keyed `grant_credits` RPC; both `ledger_applied_at` and
   `completed_at` stamped; `research_tasks.credits_settled = TRUE`.
2. `test_y01_second_settle_same_task_no_replay` — calling
   `_deduct_user_credits` again with the same `task_id` does NOT issue
   a second RPC. Short-circuits via the completed claim row.
3. `test_y01_marker_loss_no_replay` — RPC 2xx + transient
   `_mark_research_settlement_completed` failure leaves the row
   reconciler-eligible. Reconciler runs, sees `ledger_applied_at IS NOT
   NULL`, stamps `completed_at` WITHOUT issuing another RPC.
4. `test_y01_resume_does_not_double_settle` — direct A31 reproducer.
   Two consecutive `_deduct_user_credits` calls with the same task_id
   (simulating SIGKILL between RPC and file rename, then daemon resume)
   issue exactly one ledger RPC. Overrun path verified — `p_ref_type =
   'research_task_overrun'`, `p_ref_id = task_id`, `p_credits = 500`.

## 6. Out of scope / non-goals

* `_supabase_add_credits` / `_supabase_deduct_credits` in `api.py:7279,
  7416` — these are admin-only and request-time reservation helpers,
  symmetric and bounded to a single HTTP request. Not subject to
  daemon-resume double-settle.
* No NestD migration was issued (table lives in backend Postgres). The
  `init_schema()` startup path applies the new SQL idempotently on
  production deploys, same pattern as M-01 / N-01 / T-01 for
  `agent_settlements`.
* `grant_credits` / `refund_credits` already exist live with the
  required `(ref_type, ref_id)` deduplication per T-01. No live
  function changes.

## 7. Residual risk

* `research_settlements.task_id` is `TEXT` (not UUID) because
  `research_tasks.id` is also `TEXT` (per the long-standing schema in
  `mariana/data/db.py`). asyncpg passes Python `str` cleanly to a
  `TEXT` column.
* The reconciler's marker-fix-up path uses
  `_mark_research_settlement_completed`, which is a single transaction
  combining `UPDATE research_settlements` + `UPDATE
  research_tasks.credits_settled`. If the row already has
  `completed_at` non-NULL (race against another reconciler), the
  `WHERE … completed_at IS NULL` filter makes it a no-op — safe under
  concurrency.
* `research_tasks.credits_settled` defaults FALSE for legacy rows. If a
  pre-Y-01 task is still running at upgrade time, the first settlement
  attempt creates a fresh claim row and proceeds normally; no migration
  data backfill is required.
