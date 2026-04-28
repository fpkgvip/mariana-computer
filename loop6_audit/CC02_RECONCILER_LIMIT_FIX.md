# CC-02: settlement reconciler `batch_size` LIMIT silently ignored

## Severity

**HIGH** — production bug.  The reconciler was advertised to retry at most
`batch_size` (default 50) stuck claims per invocation; in practice it
retried *every* uncompleted claim older than `max_age_seconds`, regardless
of `batch_size`.  Under a backlog of thousands of stuck rows (e.g. after a
Supabase outage) a single reconciler tick would issue thousands of RPCs
serially, blocking the event loop and starving the agent-queue daemon.

## Bug

The candidate-selection SQL in both
`mariana/agent/settlement_reconciler.py::reconcile_pending_settlements` and
`mariana/research_settlement_reconciler.py::reconcile_pending_research_settlements`
used the form:

```sql
UPDATE agent_settlements SET claimed_at = now()
WHERE task_id IN (
    SELECT task_id FROM agent_settlements
    WHERE completed_at IS NULL
      AND claimed_at < now() - ($1 || ' seconds')::interval
    ORDER BY claimed_at
    LIMIT $2
    FOR UPDATE SKIP LOCKED
)
RETURNING task_id, ledger_applied_at
```

PostgreSQL is free to rewrite `WHERE x IN (SELECT ...)` as a semi-join when
the subquery's selection columns can be expressed as a join key.  For this
query the planner inlines the subquery into the outer UPDATE's predicate
and applies `LIMIT $2` to the *join output* rather than to the candidate
set.  Because the outer UPDATE re-evaluates the same `completed_at IS NULL
AND claimed_at < ...` predicate against the same table, the `LIMIT` becomes
a no-op: every matching row is updated.

Reproduction (PG 14, fresh testdb):

```python
# 5 stuck claims, max_age=300s, batch_size=2
await conn.fetch("""
    UPDATE agent_settlements SET claimed_at = now()
    WHERE task_id IN (
        SELECT task_id FROM agent_settlements
        WHERE completed_at IS NULL
          AND claimed_at < now() - ('300' || ' seconds')::interval
        ORDER BY claimed_at
        LIMIT 2
        FOR UPDATE SKIP LOCKED
    )
    RETURNING task_id
""")
# Expected: 2 rows.  Observed: 5 rows.
```

## Fix

Wrap the candidate query in a CTE.  PostgreSQL treats CTEs that contain
data-modifying or row-locking operations (here `FOR UPDATE`) as
materialised by default — the LIMIT is therefore applied to the candidate
set itself, exactly as intended.

```sql
WITH cands AS (
    SELECT task_id FROM agent_settlements
    WHERE completed_at IS NULL
      AND claimed_at < now() - ($1 || ' seconds')::interval
    ORDER BY claimed_at
    LIMIT $2
    FOR UPDATE SKIP LOCKED
)
UPDATE agent_settlements SET claimed_at = now()
WHERE task_id IN (SELECT task_id FROM cands)
RETURNING task_id, ledger_applied_at
```

Both reconcilers (agent + research) were patched identically.

## Test

`tests/test_cc02_settlement_reconciler_edge_cases.py::test_cc02_batch_size_bound_respected`
seeds 5 stuck claims and asserts that `batch_size=2` retires exactly 2
rows; the remaining 3 stay uncompleted for a later batch.  This test fails
with `attempted == 5` against the IN-subquery form and passes with the CTE
form.

## Compatibility

The CTE rewrite is semantically identical to the intended behaviour of the
original code; no migration is required.  Concurrent reconciler invocations
remain SKIP LOCKED-isolated because the lock is held inside the CTE and
the subsequent UPDATE only matches the locked rows.
