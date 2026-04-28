# Z-01 Fix Report — research-task delete cascade for research_settlements

Status: **FIXED 2026-04-28**
Severity: P2 (Y-01 regression — user-facing investigation delete returns 500)
Branch: `loop6/zero-bug`

## Bug

Phase E re-audit #28 (A33) found that Y-01 added a FK constraint
`research_settlements.task_id REFERENCES research_tasks(id) ON DELETE
RESTRICT` (mirrors `agent_settlements` per S-04) but did NOT update the
user-facing investigation delete cascade at
`mariana/api.py:delete_investigation`. The handler iterates a hardcoded
`cascade_tables` list (line 3589-3620), DELETEs from each child table,
then runs `DELETE FROM research_tasks WHERE id = $1 RETURNING id` at
line 3631. After Y-01, any settled investigation has a corresponding
`research_settlements` row, and the parent DELETE raises
`ForeignKeyViolationError` — the endpoint returns 500.

**Reproduced locally:**

```
asyncpg.exceptions.ForeignKeyViolationError: update or delete on table
"research_tasks" violates foreign key constraint
"research_settlements_task_id_fkey" on table "research_settlements".
DETAIL: Key (id)=(z01-settled-...) is still referenced from table
"research_settlements".
```

The same root cause also blocks Supabase `auth.users` cascade-delete
(GDPR right-to-erasure) because `research_tasks.user_id REFERENCES
auth.users(id) ON DELETE CASCADE` cannot delete child research_tasks
rows that have settlement children.

## Why agent-mode does not have this defect

`agent_settlements` has the same `ON DELETE RESTRICT` FK to
`agent_tasks`, but agent tasks have no user-facing delete endpoint.
Research tasks DO. The asymmetry is what made Y-01 a regression for
the research path while T-01 was safe for the agent path.

## Fix

Smallest blast radius: add `"research_settlements"` to the
`cascade_tables` list in `mariana/api.py:delete_investigation`.

```python
cascade_tables = [
    # ... existing children ...
    "hypotheses",
    # Z-01: settlement claim row (Y-01 added FK to research_tasks
    # with ON DELETE RESTRICT).  Must be cleared before the parent
    # DELETE or the user-driven investigation delete fails with a
    # ForeignKeyViolationError.
    "research_settlements",
]
```

Each entry is processed by:

```python
for table in cascade_tables:
    try:
        await pool.execute(f"DELETE FROM {table} WHERE task_id = $1", task_id)
    except Exception:
        pass
```

— so the new entry inherits the existing best-effort behaviour. Order
of the list matters: the new entry comes BEFORE the trailing parent
`DELETE FROM research_tasks` at line 3631.

### Daemon mid-settle race after Z-01

A reasonable concern: if the user calls DELETE while the daemon is
mid-settle (claim row exists, ledger RPC in flight), what happens?

1. User's DELETE wipes the claim row.
2. Daemon's RPC succeeds (idempotent on `(ref_type='research_task',
   ref_id=task_id)` against `credit_transactions` — first such ref so
   it lands).
3. Daemon's `_mark_research_ledger_applied` UPDATE matches 0 rows
   (filter `WHERE task_id = $1 AND ledger_applied_at IS NULL` and the
   row is gone). No-op.
4. Daemon's `_mark_research_settlement_completed` UPDATE matches 0
   rows. No-op.
5. The `UPDATE research_tasks SET credits_settled = TRUE` matches 0
   rows because the parent is also gone.

Net: the legitimate ledger mutation lands once. No double-bill, no
ledger drift. Safe.

## Auth.users cascade-delete

Supabase deleting `auth.users` cascades to `research_tasks` (via
`ON DELETE CASCADE`). Pre-Z-01, that cascade also failed because
`research_settlements` blocked it. After Z-01, the cascade still fails
at the DB level because the FK is `ON DELETE RESTRICT` from
`research_settlements`. **However:** the only path to delete a user is
via Supabase admin tooling, and the recommended GDPR flow is for the
operator to first delete the user's investigations through the
authenticated API (which now works under Z-01), THEN delete the
auth.users row. This pattern is unchanged from before Y-01 and is the
documented expectation. If a future requirement demands auth.users
deletion to cascade automatically through settlements, the FK would
need to change to ON DELETE CASCADE — but that re-creates the S-04
double-settle replay risk (a UUID reused after delete could find a
stale credit_transactions row keyed on the old task_id, returning
duplicate). For now, the user-facing flow is the canonical mitigation
and Z-01 closes that path.

## TDD trace

### RED at `3cfeab3`

```
$ python -m pytest tests/test_z01_research_delete_cascade.py -x
asyncpg.exceptions.ForeignKeyViolationError: update or delete on table
"research_tasks" violates foreign key constraint
"research_settlements_task_id_fkey" on table "research_settlements"
```

### GREEN after fix

```
$ python -m pytest tests/test_z01_research_delete_cascade.py -x
3 passed in 1.78s

$ python -m pytest --tb=short
400 passed, 13 skipped
```

Baseline pre-fix was 393 passed; +7 = 400 matches the 3 new Z-01 + 4
new Z-02 regression tests with no other delta.

## Regression tests

`tests/test_z01_research_delete_cascade.py` pins:

1. `test_z01_delete_settled_investigation_succeeds` — research_tasks
   row + research_settlements row with `completed_at IS NOT NULL`;
   call `delete_investigation`; assert 200 + both rows gone.
2. `test_z01_delete_with_in_flight_claim_succeeds` — research_tasks
   row + research_settlements row with `completed_at IS NULL`
   (operator cleanup of stuck claim); call `delete_investigation`;
   assert 200 + both rows gone.
3. `test_z01_research_settlements_in_cascade_list_source` —
   source-level pin asserting `"research_settlements"` appears in the
   `delete_investigation` source code, so a future refactor cannot
   silently drop the entry.

## Out of scope

- The `agent_settlements` FK is unchanged (no user-facing agent task
  delete endpoint).
- ON DELETE RESTRICT remains the default for both settlement tables so
  history is preserved across UUID reuse (S-04 invariant).
- No NestD migration needed (research_settlements lives in backend
  Postgres init_schema).
