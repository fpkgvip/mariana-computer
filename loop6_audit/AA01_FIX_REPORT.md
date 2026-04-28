# AA-01 Fix Report — Daemon mid-settle reservation loss after parent delete

Status: **FIXED 2026-04-28**
Severity: P2 (financial — silent reservation loss when user deletes a running investigation)
Branch: `loop6/zero-bug`

## 1. Bug

Phase E re-audit #29 (A34) found that the Y-01 / Z-01 interaction
created a daemon-mid-settle reservation-loss path:

1. User submits an investigation. The API reserves R credits up front.
2. Investigation enters RUNNING state and the daemon picks it up.
3. User clicks DELETE on the running investigation.
4. The API at `mariana/api.py:delete_investigation` (lines 3577-3642):
   - sets `status='FAILED'`,
   - publishes `kill:<task_id>` via Redis,
   - iterates `cascade_tables` (which includes `research_settlements`
     per Z-01) and DELETEs from each,
   - DELETEs the parent `research_tasks` row.
5. Seconds later, the orchestrator detects the kill and `_run_single`
   calls `_deduct_user_credits(task_id, db)`.
6. `_deduct_user_credits` looks up the claim row in
   `research_settlements` — none. It calls
   `_claim_research_settlement` to INSERT a fresh claim.
7. The INSERT raises `asyncpg.exceptions.ForeignKeyViolationError`
   because `research_settlements.task_id REFERENCES research_tasks(id)
   ON DELETE RESTRICT` and the parent row is gone.
8. Pre-AA-01: the broad `except Exception` at `mariana/main.py:590`
   caught and silently returned. **The user's reservation refund was
   permanently lost** — the keyed `grant_credits` RPC was never
   issued.

Concrete example:
- User reserves R = 200 credits at submission. Profile balance:
  `original − 200`.
- User clicks DELETE before any work completes (cost_tracker spend
  near zero). Expected refund: 200 credits.
- API cascades + DELETEs parent.
- Daemon settle: claim INSERT FK-violates → swallowed → no RPC.
- Profile balance remains `original − 200`. 200 credits silently
  lost.

## 2. Why prior fixes did not catch it

* **Y-01** introduced the `research_settlements` claim-row as a
  prerequisite for settlement, mirroring T-01 for the agent path. T-01
  did not have this regression because agent tasks are not
  user-deletable.
* **Z-01** added `research_settlements` to the user-DELETE cascade so
  that a steady-state delete of an already-settled investigation no
  longer FK-violates. Z-01 did NOT address the case where the daemon
  is mid-settle when the user DELETE arrives — the parent disappears
  before the daemon's claim INSERT fires.

## 3. Fix

Smallest blast radius: turn `_claim_research_settlement` into a
3-state return (`"won"` / `"lost"` / `"orphan"`) that distinguishes
the parent-gone case from a normal lost-race, and have
`_deduct_user_credits` fall through to the keyed ledger RPC on the
orphan case while skipping the marker UPDATEs.

### 3.1 `_claim_research_settlement` (`mariana/main.py:412-471`)

```python
import asyncpg as _asyncpg

try:
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO research_settlements (...) "
            "VALUES (...) ON CONFLICT (task_id) DO NOTHING RETURNING task_id",
            ...
        )
except _asyncpg.exceptions.ForeignKeyViolationError:
    # AA-01: parent ``research_tasks`` row is gone — the user
    # cascaded the investigation away during the daemon's
    # settlement window.  Caller must still issue the keyed ledger
    # RPC so the reservation refund actually lands.
    return "orphan"
return "won" if row is not None else "lost"
```

Other DB errors propagate (the caller's outer `except Exception`
handles those exactly as before).

### 3.2 `_deduct_user_credits` orphan handling

Right after the claim call, the caller now recognises the
`"orphan"` sentinel and sets a local `orphan_parent = True` flag,
plus emits a structured `credit_settlement_orphan_parent` warning.
Two downstream branches consult the flag:

* **`delta_tokens == 0` noop branch:** skip the
  `_mark_research_settlement_completed` UPDATE on orphan (there is
  no claim row to mark and no parent to flip `credits_settled` on).
* **Post-RPC marker branch:** skip both
  `_mark_research_ledger_applied` and
  `_mark_research_settlement_completed` on orphan; instead emit
  `credit_settlement_orphan_refund_ok` for operator visibility.

The keyed `grant_credits` / `refund_credits` RPCs themselves are
unchanged. They use `(p_ref_type='research_task',
p_ref_id=task_id)` for refund and `(p_ref_type='research_task_overrun',
p_ref_id=task_id)` for overrun. The live ledger's `credit_transactions
UNIQUE(type, ref_type, ref_id)` (per T-01) deduplicates a daemon
retry so a worst-case replay returns `status='duplicate'` rather
than minting a second mutation.

### 3.3 Edge cases addressed

* **Parent gone but claim row still exists** (e.g. Z-01 cascade out
  of order — should not happen): the existing-claim lookup at the
  top of `_deduct_user_credits` finds the row, the function takes
  the marker-fixup or completed-short-circuit branches, never
  reaching the INSERT. No regression.
* **Parent exists but FK violation for some other reason** (e.g.
  bad user_id schema mismatch): NOT caught by the new branch —
  `_claim_research_settlement` raises `ForeignKeyViolationError`
  for ANY FK violation. In practice the only FK on
  `research_settlements` is `task_id → research_tasks(id)`, so
  this is the right discriminator. If a future schema change adds
  another FK, the orphan branch may treat it as orphan-parent — at
  worst still safe because the keyed RPC is idempotent.
* **Daemon retry idempotency:** two consecutive orphan-refund
  calls with the same task_id both issue the keyed RPC; the live
  ledger dedupes on `(ref_type, ref_id)`. No double-refund.
* **`user_id` availability:** the orphan branch does not need a
  claim row to source `user_id` — it is already in scope as the
  function's `user_id` parameter from `_run_single`.
* **Logging hygiene:** `credit_settlement_orphan_refund_ok` and
  `credit_settlement_orphan_parent` log only `task_id`, `user_id`,
  `reserved_credits`, `final_tokens`, and `delta_tokens`. No PII.

## 4. TDD trace

### RED at `1f11d99`

```
$ python -m pytest tests/test_aa01_daemon_mid_settle_orphan_refund.py -x
FAILED test_aa01_orphan_parent_refund_still_issues_grant_credits
AssertionError: orphan-parent refund must still issue exactly one
keyed grant_credits RPC; got 0: []
```

The `credit_settlement_claim_error` log line in the captured output
showed the FK violation being silently swallowed pre-fix.

### GREEN after fix

```
$ python -m pytest tests/test_aa01_daemon_mid_settle_orphan_refund.py -x
3 passed in 0.35s

$ python -m pytest --tb=short
403 passed, 13 skipped, 0 failed
```

Baseline pre-fix was 400 passed; +3 = 403 matches the three new
AA-01 regression tests with no other delta.

## 5. Regression tests

`tests/test_aa01_daemon_mid_settle_orphan_refund.py`:

1. `test_aa01_orphan_parent_refund_still_issues_grant_credits` —
   parent row absent, delta < 0; assert exactly one keyed
   `grant_credits` RPC issued with `p_ref_type='research_task'`,
   `p_ref_id=task_id`, `p_credits=470`; assert no claim row was
   inserted.
2. `test_aa01_orphan_replay_uses_same_ref_id` — two consecutive
   orphan-refund calls for the same task_id; assert both RPCs
   share the same `(p_ref_type, p_ref_id)` so the live ledger's
   UNIQUE constraint deduplicates the second mutation.
3. `test_aa01_orphan_parent_overrun_still_issues_refund_credits` —
   parent row absent, delta > 0; assert keyed `refund_credits`
   RPC issued with `p_ref_type='research_task_overrun'`.

## 6. Out of scope

- The agent-mode equivalent (`_settle_agent_credits` in
  `mariana/agent/loop.py`) does not have this defect because agent
  tasks are not user-deletable.
- No NestD migration needed — the change is purely in
  `mariana/main.py`.
- Reconciler behaviour is unchanged. An orphan task has no claim
  row, so the reconciler never picks it up. The keyed RPC's
  `(ref_type, ref_id)` UNIQUE in `credit_transactions` is the
  only durable idempotency anchor for orphan-refund tasks; this
  matches T-01's design philosophy of "live ledger primitives are
  the canonical fence".
