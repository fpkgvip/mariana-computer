# S-01..S-04 Fix Report — 2026-04-28

Branch: `loop6/zero-bug`
Base commit: `14cbabd` (re-audit #16)

## Summary

Four findings closed in a single change-set:

| ID | Severity | Surface | Outcome |
| --- | --- | --- | --- |
| S-01 | **P0** | agent/billing/regression-rpc-404 | FIXED |
| S-02 | P3 | agent_settlements/defense-in-depth | FIXED |
| S-03 | P2 | agent_settlements/reconciler | FIXED |
| S-04 | P4 | agent_settlements/cascade-replay | FIXED |

## RED → GREEN

### Tests added (12 new tests)

* `tests/test_s01_rpc_signature_match.py` — 5 tests
  - `test_s01_add_credits_payload_no_ref_id`
  - `test_s01_deduct_credits_payload_no_ref_id`
  - `test_s01_rpc_404_marks_claim_uncompleted`
  - `test_s01_rpc_failure_does_not_short_circuit_retry`
  - `test_s01_rpc_success_sets_completed_at`
* `tests/test_s02_check_constraints.py` — 2 tests
  - `test_s02_negative_reserved_credits_rejected`
  - `test_s02_negative_final_credits_rejected`
* `tests/test_s03_reconciler.py` — 4 tests
  - `test_s03_reconciler_picks_uncompleted`
  - `test_s03_reconciler_skips_completed`
  - `test_s03_reconciler_handles_rpc_failure_gracefully`
  - `test_s03_reconciler_idempotent_concurrent_runs`
* `tests/test_s04_no_cascade.py` — 1 test
  - `test_s04_agent_tasks_delete_does_not_cascade`

### RED at HEAD (14cbabd) — pre-fix

Initial run on HEAD with the new tests applied but **no code/schema changes**:

```
FAILED tests/test_s01_rpc_signature_match.py::test_s01_add_credits_payload_no_ref_id
FAILED tests/test_s01_rpc_signature_match.py::test_s01_deduct_credits_payload_no_ref_id
FAILED tests/test_s01_rpc_signature_match.py::test_s01_rpc_404_marks_claim_uncompleted
FAILED tests/test_s01_rpc_signature_match.py::test_s01_rpc_failure_does_not_short_circuit_retry
FAILED tests/test_s02_check_constraints.py::test_s02_negative_reserved_credits_rejected
FAILED tests/test_s02_check_constraints.py::test_s02_negative_final_credits_rejected
FAILED tests/test_s03_reconciler.py::test_s03_reconciler_picks_uncompleted
FAILED tests/test_s03_reconciler.py::test_s03_reconciler_skips_completed
FAILED tests/test_s03_reconciler.py::test_s03_reconciler_handles_rpc_failure_gracefully
FAILED tests/test_s03_reconciler.py::test_s03_reconciler_idempotent_concurrent_runs
FAILED tests/test_s04_no_cascade.py::test_s04_agent_tasks_delete_does_not_cascade
========================= 11 failed, 1 passed in 1.87s =========================
```

(`test_s01_rpc_success_sets_completed_at` passed at HEAD because the
broken code coincidentally set `credits_settled=True` and stamped
`completed_at` on RPC 200 — that path remains correct post-fix.)

### GREEN post-fix

Final full-suite run after all four fixes applied:

```
================= 352 passed, 13 skipped, 2 warnings in 6.99s ==================
```

Frontend vitest unchanged: **144 passed (15 files)**.

## Code & schema changes

### S-01 — `mariana/agent/loop.py`

`_settle_agent_credits` rewritten end-to-end (lines ~420–700):

1. Drops the `ref_id` JSON key from both POST bodies. The live PostgREST
   functions accept exactly `(p_user_id, p_credits)` and
   `(target_user_id, amount)`; sending `ref_id` triggered PGRST202 / HTTP
   404 on every agent settlement.
2. New flow uses the existing `agent_settlements` claim row as the
   single source of truth:
   * SELECT existing claim first.
   * `completed_at IS NOT NULL` → flip in-memory flag, return
     (already settled).
   * `completed_at IS NULL` → fall through to RPC retry without
     re-inserting.
   * No row → race-safe INSERT via ON CONFLICT DO NOTHING; on lost
     race, re-fetch and decide.
3. `task.credits_settled` is set to `True` **only** when the RPC
   actually succeeds (or a pre-completed claim was observed).  RPC
   failure leaves both the in-memory flag False and `completed_at`
   NULL — the row is now "in flight" and the S-03 reconciler retries
   it on the next iteration via the same code path.
4. The δ=0 branch still stamps `completed_at` inline so the reconciler
   doesn't pick up rows that need no RPC.

`ref_id = "agent_settle:<task.id>"` is still computed and stored in the
`agent_settlements` row for offline correlation; it just no longer
reaches PostgREST.

### S-02 — `mariana/agent/schema.sql`

`agent_settlements` gains inline `CHECK (reserved_credits >= 0)` and
`CHECK (final_credits >= 0)`.  Added idempotent
`ALTER TABLE ... DROP CONSTRAINT IF EXISTS / ADD CONSTRAINT` pairs so
existing deployments pick the constraints up at the next
`init_schema` run.  `delta_credits` stays unconstrained (signed —
positive overrun, negative refund).

### S-03 — new `mariana/agent/settlement_reconciler.py`

`reconcile_pending_settlements(db, max_age_seconds=300, batch_size=50)`
atomically claims candidate rows by bumping `claimed_at = now()` in a
single `UPDATE ... WHERE task_id IN (SELECT ... FOR UPDATE SKIP LOCKED)`
RETURNING.  This is the SKIP-LOCKED-equivalent without holding a row
lock across the slow Supabase RPC (which would deadlock against the
inner `_settle_agent_credits` `_mark_settlement_completed` UPDATE).

For each claimed task_id it loads the AgentTask, resets the in-memory
`credits_settled` flag (a stale True would otherwise short-circuit the
retry), and calls `_settle_agent_credits` — which already knows how to
treat an existing-but-uncompleted claim as a retry.

`mariana/main.py` adds `_run_settlement_reconciler_loop(db)` running
every 60 seconds (env-tunable via `AGENT_SETTLEMENT_RECONCILE_*`)
alongside the existing agent-queue daemon under
`asyncio.wait(..., FIRST_EXCEPTION)`.

### S-04 — `mariana/agent/schema.sql`

`agent_settlements.task_id` FK changed from `ON DELETE CASCADE` to
`ON DELETE RESTRICT`.  Added idempotent FK drop+add for in-place
upgrades.  Settlement history is now immutable: deleting an
`agent_tasks` row with a settlement claim raises
`asyncpg.ForeignKeyViolationError`, forcing operators to acknowledge
(and explicitly delete) the settlement first.

## R-01 test contract update

`tests/test_r01_settlement_idempotency.py::test_r01_settlement_table_records_outcome`
asserted `fail_task.credits_settled is True` after an RPC 500 — this
encoded the very behaviour S-01's audit identified as the root cause
(in-memory flag flipped True before HTTP status check, permanently
stranding uncompleted claims).  Updated to
`assert fail_task.credits_settled is False` with a docstring explaining
the corrected contract.  The 5 other R-01 tests pass unchanged.

## Files touched

```
mariana/agent/loop.py                       (rewrite of _settle_agent_credits)
mariana/agent/schema.sql                    (CHECK constraints + RESTRICT)
mariana/agent/settlement_reconciler.py      (new)
mariana/main.py                             (reconciler loop wiring)
tests/test_s01_rpc_signature_match.py       (new — 5 tests)
tests/test_s02_check_constraints.py         (new — 2 tests)
tests/test_s03_reconciler.py                (new — 4 tests)
tests/test_s04_no_cascade.py                (new — 1 test)
tests/test_r01_settlement_idempotency.py    (1 test contract update)
loop6_audit/REGISTRY.md                     (S-01..S-04 → FIXED)
loop6_audit/S01_S02_S03_S04_FIX_REPORT.md   (this file)
```

## Full pytest tail

```
tests/test_r01_settlement_idempotency.py ......                          [ 83%]
tests/test_s01_rpc_signature_match.py .....                              [ 84%]
tests/test_s02_check_constraints.py ..                                   [ 84%]
tests/test_s03_reconciler.py ....                                        [ 86%]
tests/test_s04_no_cascade.py .                                           [ 86%]
tests/test_vault_integration.py .......                                  [ 88%]
tests/test_vault_live.py s                                               [ 88%]
tests/test_vault_no_leak_live.py ss                                      [ 89%]
tests/test_vault_redaction.py ..........                                 [ 91%]
tests/test_vault_runtime.py ................                             [ 96%]
tests/tools/test_reconcile_ledger.py ..............                      [100%]

================= 352 passed, 13 skipped, 2 warnings in 6.99s ==================
```

Frontend:

```
Test Files  15 passed (15)
     Tests  144 passed (144)
```
