# Q-01 Fix Report — CAS guard incomplete; finally-block clobbers terminal state

**Branch:** `loop6/zero-bug`
**Date:** 2026-04-28
**Tests:** 334 python passed (328 baseline + 6 new), 144 vitest passed

---

## 1. Bug

`_persist_task`'s P-01 CAS guard only rejected UPSERTs whose
`EXCLUDED.credits_settled = FALSE`.  But `run_agent_task`'s finally
block deliberately sets `task.credits_settled = True` (after the
fresh DB re-read confirms another writer settled the row), then calls
`_persist_task` at `loop.py:1074`.  Because `EXCLUDED.credits_settled =
TRUE`, the CAS guard did NOT reject, and the worker's stale terminal
state (e.g. HALTED) overwrote the stop-endpoint-set CANCELLED, while
the worker's planner-accumulated `spent_usd` was written into a row
already settled at `spent_usd=0` — no further deduct ever fires.

Two impacts:
1. **Cancel-state contract violation.** UI/audit observe `halted` (or
   `done`) instead of `cancelled`.
2. **Free planner-LLM cost leak.** Stop refund covered the full
   reservation while the worker's planner cost was persisted without
   any reconciling deduct.

Empirically reproduced against the live local Postgres before fix:

```
Final DB row: state='halted', credits_settled=True, spent_usd=$0.40
(over a row the stop endpoint had set to state='cancelled', spent_usd=0)
```

## 2. RED test results (HEAD `e4b7cb7`)

```
tests/test_q01_cas_state_clobber.py::test_q01_cas_blocks_state_change_on_settled FAILED
tests/test_q01_cas_state_clobber.py::test_q01_cas_allows_legitimate_settle_transition PASSED
tests/test_q01_cas_state_clobber.py::test_q01_cas_allows_same_state_idempotent_resettle PASSED
tests/test_q01_cas_state_clobber.py::test_q01_cas_blocks_spent_usd_write_after_settle FAILED
tests/test_q01_cas_state_clobber.py::test_q01_finally_skip_persist_when_already_settled FAILED
tests/test_q01_cas_state_clobber.py::test_q01_full_race_state_preserved FAILED
========================= 4 failed, 2 passed in 1.90s ==========================
```

The two pre-fix passing cases (legitimate-settle, same-state idempotent)
verify that the new clause does NOT regress legitimate finalize
transitions.

## 3. Code diff

### 3.1 `mariana/agent/loop.py:_persist_task` CAS WHERE clause

```diff
-            WHERE NOT (
-                agent_tasks.credits_settled = TRUE
-                AND agent_tasks.state IN ('done','failed','halted','cancelled')
-                AND EXCLUDED.credits_settled = FALSE
-            )
+            WHERE (
+                -- Existing row is not yet finalized: any progression is fine.
+                agent_tasks.credits_settled = FALSE
+                -- OR: existing row is already settled, but the incoming
+                -- write preserves BOTH state and credits_settled=TRUE (an
+                -- idempotent self-write by the legitimate finalizer).  Any
+                -- other write to a settled row — un-finalize attempts
+                -- (P-01) and post-settle state/spent_usd clobber
+                -- attempts (Q-01) — is rejected.
+                OR (
+                    agent_tasks.state = EXCLUDED.state
+                    AND EXCLUDED.credits_settled = TRUE
+                )
+            )
```

This single predicate covers BOTH P-01 (un-finalize) and Q-01
(post-settle state/spent_usd clobber):

| Existing row | Incoming snapshot | New CAS verdict | Why |
|---|---|---|---|
| settled=False, state=PLAN | state=EXECUTE, settled=False | ALLOW | progression on un-settled row |
| settled=False, state=DONE | state=DONE, settled=True | ALLOW | legitimate worker finalize |
| settled=True, state=cancelled | state=cancelled, settled=True | ALLOW | idempotent self-write |
| settled=True, state=cancelled | state=PLAN, settled=False | BLOCK | P-01 un-finalize hole |
| settled=True, state=cancelled | state=halted, settled=True | BLOCK | **Q-01 hole — fixed** |
| settled=True, state=cancelled | state=halted, settled=False | BLOCK | both un-finalize and clobber |

### 3.2 `mariana/agent/loop.py` finally-block — skip trailing `_persist_task` when externally-settled

```diff
             already_settled_in_db = False
+            db_terminal_state: str | None = None
             try:
                 async with db.acquire() as conn:
                     final_row = await conn.fetchrow(
-                        "SELECT credits_settled FROM agent_tasks WHERE id = $1",
+                        "SELECT credits_settled, state FROM agent_tasks "
+                        "WHERE id = $1",
                         task.id,
                     )
                 if final_row is not None:
                     already_settled_in_db = (
                         final_row["credits_settled"] is True
                     )
+                    db_terminal_state = final_row["state"]
             except Exception:
                 logger.exception("agent_finally_settle_check_failed", task_id=task.id)
             if already_settled_in_db:
-                task.credits_settled = True
+                # Q-01 + P-01: skip BOTH _settle_agent_credits and the
+                # trailing _persist_task to avoid leaking stale state /
+                # spent_usd into the canonical DB row.
                 logger.info(
                     "agent_finally_settle_skipped_already_settled",
                     task_id=task.id,
+                    db_state=db_terminal_state,
+                    in_memory_state=task.state.value,
                 )
             else:
                 try:
                     await _settle_agent_credits(task)
                 except Exception as _settle_exc:
                     logger.error("agent_credits_settle_finally_error", ...)
-            try:
-                await _persist_task(db, task)
-            except Exception:
-                logger.exception("agent_finally_persist_failed", task_id=task.id)
+                try:
+                    await _persist_task(db, task)
+                except Exception:
+                    logger.exception("agent_finally_persist_failed", task_id=task.id)
```

The trailing `_persist_task` is now nested inside the
`else: not already-settled` branch.  When the DB shows another writer
already finalized the row, the worker exits the finally without
touching `agent_tasks` again.

## 4. Tests

`tests/test_q01_cas_state_clobber.py` adds 6 regressions:

1. `test_q01_cas_blocks_state_change_on_settled` — direct CAS probe:
   state=cancelled+settled → state=halted+settled is rejected.
2. `test_q01_cas_allows_legitimate_settle_transition` — worker happy
   path: state=DONE+settled=False → state=DONE+settled=True lands.
3. `test_q01_cas_allows_same_state_idempotent_resettle` —
   state=cancelled+settled idempotent re-write succeeds.
4. `test_q01_cas_blocks_spent_usd_write_after_settle` — verifies
   `spent_usd` is not leaked into a settled row when state also differs.
5. `test_q01_finally_skip_persist_when_already_settled` — spy on
   `_persist_task` to confirm the finally branch never invokes it with
   a stale-snapshot `credits_settled=True` after the DB shows
   already-settled.
6. `test_q01_full_race_state_preserved` — end-to-end race: planner
   simulates concurrent stop endpoint settling +
   persisting CANCELLED, worker subsequently halts in-memory, finally
   block runs.  Asserts final DB stays state=CANCELLED, spent_usd=0,
   credits_settled=True, with exactly ONE add_credits RPC.

All 6 P-01 regression tests still pass — the new CAS clause covers
P-01's stale-snapshot blocking AND closes the Q-01 hole.

## 5. Full pytest tail

```
=========================== test session starts ============================
platform linux -- Python 3.12.8, pytest-9.0.3, pluggy-1.6.0
plugins: anyio-4.13.0, ddtrace-4.7.1, asyncio-1.3.0, timeout-2.4.0
asyncio: mode=Mode.AUTO ...
collected 347 items

tests/test_p01_stale_worker_race.py ......                          [...]
tests/test_q01_cas_state_clobber.py ......                          [...]
...

================= 334 passed, 13 skipped, 2 warnings in 7.10s =================
```

Frontend:

```
Test Files  15 passed (15)
     Tests  144 passed (144)
  Duration  11.11s
```

## 6. Surfaces re-checked, no regression

- P-01 stale-worker double-refund: 6 tests still green; the new CAS
  clause's first disjunct (`existing.credits_settled = FALSE`) covers
  every legitimate worker progression, and the second disjunct
  (`state==EXCLUDED.state AND EXCLUDED.credits_settled=TRUE`) is
  strictly stricter than the old un-finalize check.
- O-02 cancel-time settlement / O-01 sub-100 ceiling: untouched.
- N-01 settlement persistence: untouched, schema unchanged.
- Finally block double-refund defense: now even stronger — both the
  settle call AND the trailing persist are short-circuited when DB
  shows already-settled.
