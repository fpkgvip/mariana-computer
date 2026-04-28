# R-01 fix report — fail-open finally guard double-refund

Header: model=gpt_5_4, base_commit=fa6cd55, branch=loop6/zero-bug.

## Bug

P1, agent/billing/finally-fail-open.

`run_agent_task`'s `finally` block re-read `credits_settled, state` from
`agent_tasks` to gate the settlement helper. If that read raised on any
transient cause (pool error, connection blip, mocked failure), the
`except` clause logged `agent_finally_settle_check_failed` and continued
with `already_settled_in_db=False`. The worker then called
`_settle_agent_credits` from its stale in-memory snapshot, issuing a
second `add_credits` RPC for the same reservation. The Q-01 CAS on the
trailing `_persist_task` blocked the row state from being clobbered, so
the canonical `agent_tasks` row stayed `state=cancelled,
credits_settled=True, spent_usd=0` while the credit ledger was
double-credited.

The repro in `repro_r01_finally_fetch_failure.py` (described in
`A20_phase_e_reaudit.md`) showed two `add_credits` RPCs (500 + 420)
against the same reservation while the final DB row appeared clean.

## Strategy

Move settlement idempotency off process-local state and onto a backend
Postgres claim row. `agent_tasks` lives in backend Postgres (asyncpg)
while the credit ledger lives in NestD Supabase, so a single SQL
function cannot atomically settle both. Instead a new
`agent_settlements` table colocated with `agent_tasks` carries an atomic
`(task_id) PRIMARY KEY` claim that pre-gates the ledger RPC.

```
INSERT INTO agent_settlements (...)
VALUES (...)
ON CONFLICT (task_id) DO NOTHING
RETURNING task_id
```

A second concurrent caller — stop endpoint racing the worker's finally,
or the finally fetch failure falling through — observes the empty
`RETURNING` and short-circuits before any RPC fires.

## RED tests first

Six regression tests added in `tests/test_r01_settlement_idempotency.py`
covering the audit's required matrix:

1. `test_r01_concurrent_settle_only_one_wins`
2. `test_r01_finally_fetch_failure_does_not_double_refund`
3. `test_r01_settlement_table_records_outcome`
4. `test_r01_settle_idempotent_after_completion`
5. `test_r01_full_race_repro`
6. `test_r01_in_memory_credits_settled_no_longer_authoritative`

RED on `fa6cd55`:

```
FAILED tests/test_r01_settlement_idempotency.py::test_r01_concurrent_settle_only_one_wins
FAILED tests/test_r01_settlement_idempotency.py::test_r01_finally_fetch_failure_does_not_double_refund
FAILED tests/test_r01_settlement_idempotency.py::test_r01_settlement_table_records_outcome
FAILED tests/test_r01_settlement_idempotency.py::test_r01_settle_idempotent_after_completion
FAILED tests/test_r01_settlement_idempotency.py::test_r01_full_race_repro
FAILED tests/test_r01_settlement_idempotency.py::test_r01_in_memory_credits_settled_no_longer_authoritative
============================== 6 failed in 1.80s ===============================
```

(Initial failure mode was a `TypeError` on the new `db=` kwarg, since the
old `_settle_agent_credits(task)` signature did not accept it. After
adding the parameter the assertions exercise the actual claim-row
behaviour.)

## Schema diff (`mariana/agent/schema.sql`)

```sql
CREATE TABLE IF NOT EXISTS agent_settlements (
    task_id           UUID PRIMARY KEY REFERENCES agent_tasks(id) ON DELETE CASCADE,
    user_id           TEXT NOT NULL,
    reserved_credits  BIGINT NOT NULL,
    final_credits     BIGINT NOT NULL,
    delta_credits     BIGINT NOT NULL,
    ref_id            TEXT NOT NULL,
    claimed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_agent_settlements_completed
    ON agent_settlements(completed_at) WHERE completed_at IS NULL;
```

The schema bootstrap (`init_schema` running this file) is idempotent.
Existing deployments pick the table up on next restart with no
migration step.

## Code diff (`mariana/agent/loop.py`)

`_settle_agent_credits` gains an optional `db` parameter (default `None`
for legacy unit-test callers that already mock the in-memory flag). New
helpers:

- `_claim_settlement(...)` — atomic INSERT ON CONFLICT DO NOTHING
  RETURNING task_id. Returns `True` if this caller won the claim.
- `_mark_settlement_completed(db, task_id)` — stamps `completed_at=now()`
  after a successful RPC.

The settlement flow is now:

1. Compute `final_tokens, delta, ref_id=f"agent_settle:{task.id}"`.
2. Attempt `_claim_settlement`. If it returns `False`, log
   `agent_settlement_already_claimed` and short-circuit before any RPC.
3. If `delta == 0`, mark `completed_at` and return.
4. Otherwise issue the appropriate `add_credits` / `deduct_credits` RPC,
   threading `ref_id` through the JSON body for ledger-level
   defense-in-depth.
5. On success, `_mark_settlement_completed` stamps the claim row. On
   failure the row stays uncompleted — operator reconciliation surface.

The `finally` block in `run_agent_task` is simplified:

```python
finally:
    if is_terminal(task.state):
        already_settled_in_db = False
        try:
            async with db.acquire() as conn:
                fast_row = await conn.fetchrow(
                    "SELECT credits_settled FROM agent_tasks WHERE id = $1",
                    task.id,
                )
            if fast_row is not None and fast_row["credits_settled"] is True:
                already_settled_in_db = True
        except Exception:
            logger.exception("agent_finally_fast_path_read_failed", task_id=task.id)

        if not already_settled_in_db:
            try:
                await _settle_agent_credits(task, db=db)
            except Exception as exc:
                logger.error("agent_credits_settle_finally_error", task_id=task.id, error=str(exc))

        try:
            await _persist_task(db, task)
        except Exception:
            logger.exception("agent_finally_persist_failed", task_id=task.id)
```

The fast-path `SELECT credits_settled` skips the helper invocation when
we already know the row is settled. Its failure mode is no longer
dangerous — the claim-row INSERT will short-circuit any duplicate
settle attempt regardless. The trailing `_persist_task` runs
unconditionally and is protected by the Q-01 CAS guard.

## API call site (`mariana/agent/api_routes.py`)

The stop-endpoint inline settlement now passes `db`:

```python
await _settle_agent_credits(terminal_task, db=db)
```

Any racing worker call observing the same DB will see the existing
claim row and short-circuit.

## Test results

`tests/test_m01_agent_billing_unit.py`,
`tests/test_n01_settlement_persistence.py`,
`tests/test_o02_cancel_settlement.py`,
`tests/test_p01_stale_worker_race.py`,
`tests/test_q01_cas_state_clobber.py` all still green — they call the
helper without `db` and continue to exercise the legacy in-memory path.

Full python pytest tail:

```
tests/test_quote.py ........                                             [ 84%]
tests/test_r01_settlement_idempotency.py ......                          [ 85%]
tests/test_vault_integration.py .......                                  [ 87%]
tests/test_vault_live.py s                                               [ 88%]
tests/test_vault_no_leak_live.py ss                                      [ 88%]
tests/test_vault_redaction.py ..........                                 [ 91%]
tests/test_vault_runtime.py ................                             [ 96%]
tests/tools/test_reconcile_ledger.py ..............                      [100%]

================= 340 passed, 13 skipped, 2 warnings in 6.44s ==================
```

Frontend vitest tail:

```
 Test Files  15 passed (15)
      Tests  144 passed (144)
   Duration  10.75s
```

## Summary

- Settlement is now idempotent at the database level via an
  `agent_settlements` claim row.
- The fragile Q-01 finally fetchrow guard is no longer the
  authoritative gate; its failure no longer causes duplicate refunds.
- The ledger RPC carries `ref_id="agent_settle:<task_id>"` for
  ledger-side idempotency as defense-in-depth.
- 6 regression tests added; 340 python + 144 vitest tests green.
