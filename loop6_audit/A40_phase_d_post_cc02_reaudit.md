# Re-audit #35 (gpt_5_4) — post-CC-02 Phase D coverage fill

**Range audited:** c108b1e..47af4fe (Phase D + coverage fill)
**Auditor:** gpt_5_4 (adversarial)
**Date:** 2026-04-28

## Findings

### CC-04 [Severity P2]: malformed vault Redis payload bypasses the fail-closed contract and runs secret-required tasks with an empty env
- File: mariana/vault/runtime.py:220-225; mariana/agent/loop.py:1179-1222
- Mechanism: `fetch_vault_env(..., requires_vault=True)` correctly raises `VaultUnavailableError` on Redis miss / transport failure, but it silently returns `{}` on malformed JSON (`json.loads` failure) and on non-object JSON. `run_agent_task()` treats that as a successful fetch, installs an empty task context, and continues into planning/execution. This reopens the exact fail-closed surface U-03 was meant to close: a task that explicitly required vaulted secrets can run as though secrets were honored when the Redis payload is corrupted, truncated, or poisoned.
- Reproduction: with a fake Redis client that returns `b'{'`, `b'[]'`, or `b'"just-a-string"'`, `fetch_vault_env(BadRedis(...), 'task-1', requires_vault=True, redis_url='redis://localhost:6379/0')` returns `{}` instead of raising. Because `run_agent_task()` only aborts on `VaultUnavailableError`, the task proceeds with no injected secrets.
- Recommended fix: in the `requires_vault=True` path, treat JSON parse failures, non-dict payloads, and invalid key/value shapes as `VaultUnavailableError` instead of degrading to `{}`. Add a regression test at the runtime boundary (`fetch_vault_env` / `run_agent_task`), not just in `vault/store.py`.

### CC-05 [Severity P3]: negative reconciler batch_size crashes each reconciliation tick instead of failing safe
- File: mariana/main.py:1180-1182, 1206-1209, 1241-1244; mariana/agent/settlement_reconciler.py:107-120; mariana/research_settlement_reconciler.py:60-74
- Mechanism: `_SETTLEMENT_RECONCILE_BATCH_SIZE` is parsed directly from `AGENT_SETTLEMENT_RECONCILE_BATCH_SIZE` with `int(...)` and passed unvalidated into SQL `LIMIT $2`. PostgreSQL rejects negative LIMITs with `InvalidRowCountInLimitClause`, so one bad env value bricks both settlement daemons: every loop iteration throws before claiming rows, and stuck settlements never reconcile.
- Reproduction: calling `reconcile_pending_settlements(pool, max_age_seconds=300, batch_size=-1)` raises `InvalidRowCountInLimitClauseError: LIMIT must not be negative`. The daemon loops in `mariana.main` catch and log the exception, sleep, and retry forever, so backlog recovery is permanently disabled until config is corrected.
- Recommended fix: validate `batch_size` at config-load and function-entry time (`>= 0`, and ideally `> 0` with an explicit no-op policy for zero). Mirror the guard in both reconcilers and add a regression test for zero / negative values.

## Verification of CC-02 fix

The CTE rewrite itself looks sound.

I checked both reconciler implementations and the surrounding settlement schema/logic. The candidate query is now:

```sql
WITH cands AS (
    SELECT task_id ...
    ORDER BY claimed_at
    LIMIT $2
    FOR UPDATE SKIP LOCKED
)
UPDATE ...
WHERE task_id IN (SELECT task_id FROM cands)
RETURNING ...
```

Why this appears correct:
- `FOR UPDATE SKIP LOCKED` still protects the selection phase, because the rows are locked inside the CTE before the outer `UPDATE` runs.
- The outer `UPDATE` can only target `task_id`s emitted by `cands`, so with the `task_id` primary key on both settlement tables it cannot update more rows than the CTE selected.
- Because `claimed_at = now()` happens in the same SQL statement/transaction that selected the rows, competing reconcilers see disjoint work: once one invocation claims rows, later invocations no longer match the age predicate, and locked rows are skipped during the selection itself.
- The "candidate set shrinks between CTE materialisation and UPDATE join" concern does not create an over-claim path here: the selected rows are row-locked for the duration of the statement, so another reconciler cannot concurrently modify those exact rows before the `UPDATE` finishes.

I also checked the new CC-02 tests against production code paths:
- the batch-size regression test does exercise the real SQL against Postgres, so it meaningfully verifies the CTE limit bound;
- the older S-03 concurrency test still exercises real concurrent reconciler calls against the same tables, and nothing in the CTE rewrite weakens the original `SKIP LOCKED` isolation model.

Net: I did not find a race or unbounded-claim regression in the CC-02 CTE rewrite itself. The only reconciler issue I found is the separate negative-`batch_size` validation hole above.

## Verdict

2 findings. Streak resets: CC-04, CC-05.
