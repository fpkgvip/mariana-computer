# P-01 Fix Report — stale-worker race double-refund

## 1. Bug

**P-01 [P1] — queued-task cancel race can double-refund the same reservation
and mint credits**

- The queue worker loaded a task via `_load_agent_task()`
  (`mariana/agent/api_routes.py:136-198`) — a plain `SELECT` with no row
  lock and no version check.
- If the user hit Stop in the window between worker load and worker start,
  `POST /api/agent/{task_id}/stop` (`mariana/agent/api_routes.py:756-869`)
  would `SELECT ... FOR UPDATE`, transition to `CANCELLED`, run
  `_settle_agent_credits` (issuing one `add_credits` RPC), and persist
  `credits_settled = TRUE`.
- The stale worker then entered `run_agent_task`
  (`mariana/agent/loop.py:758-776`) and called the unconditional
  `_persist_task` UPSERT, **clobbering** the freshly-finalized row back
  to `state=PLAN, credits_settled=False, stop_requested=...` from the
  in-memory snapshot.
- The worker proceeded, eventually saw the Redis stop key, halted, and
  the `finally:` block called `_settle_agent_credits` **again** —
  second `add_credits` RPC.
- Net effect: the user was refunded twice = minted credits = direct
  financial loss; the row also ended up as `HALTED` rather than
  `CANCELLED`.

## 2. RED tests

`tests/test_p01_stale_worker_race.py` adds six regression tests:

| # | Name | Coverage |
| - | ---- | -------- |
| 1 | `test_p01_stale_persist_does_not_clobber_settled` | `_persist_task` must reject stale snapshot vs `CANCELLED+settled` row |
| 2 | `test_p01_stale_persist_does_not_clobber_done` | Same, but `DONE+settled` row |
| 3 | `test_p01_run_agent_task_aborts_when_terminal_settled` | Pre-flight DB re-validation aborts before planner / persist |
| 4 | `test_p01_normal_persist_still_works` | Happy path UPSERT unaffected |
| 5 | `test_p01_full_race_simulation` | End-to-end repro asserts exactly **ONE** `add_credits` RPC fires |
| 6 | `test_p01_persist_task_normal_concurrent_overlap` | CAS guard does not block legitimate concurrent non-terminal writers |

Initial RED run on HEAD `036212c` (before fix):

```
FAILED tests/test_p01_stale_worker_race.py::test_p01_stale_persist_does_not_clobber_settled
FAILED tests/test_p01_stale_worker_race.py::test_p01_stale_persist_does_not_clobber_done
FAILED tests/test_p01_stale_worker_race.py::test_p01_run_agent_task_aborts_when_terminal_settled
FAILED tests/test_p01_stale_worker_race.py::test_p01_full_race_simulation
4 failed, 2 passed in 1.92s
```

The 4 race-specific tests fail; tests 4 and 6 pass since they probe the
already-correct happy path.

## 3. Code changes

### `mariana/agent/loop.py:_persist_task`

- Old: unconditional `INSERT … ON CONFLICT (id) DO UPDATE SET …`.
- New: same UPSERT plus a `WHERE NOT (...)` clause on the conflict-update
  branch that REJECTS the UPDATE if and only if:
  - the existing DB row has `credits_settled = TRUE`, **AND**
  - the existing DB row's state is one of `done | failed | halted | cancelled`, **AND**
  - the incoming `EXCLUDED.credits_settled = FALSE`.
- Function signature now `-> bool`. Parses asyncpg's libpq command tag
  (`"INSERT 0 0"` vs `"INSERT 0 1"`) to surface whether the row was
  actually written. Logs `agent_persist_task_blocked` at WARNING level
  when the CAS guard rejects the UPDATE.

### `mariana/agent/loop.py:run_agent_task`

1. **Pre-flight DB re-validation** at function entry, BEFORE any
   `_persist_task` or planner work. Reads `state, credits_settled`
   directly from `agent_tasks` and returns the unmodified task if the
   row is already `credits_settled=TRUE` AND in a terminal state. Crucially
   the in-memory `task.state` is NOT advanced — so the `finally:`
   `is_terminal(task.state)` check stays False and the second
   `_settle_agent_credits` call never happens.
2. **Finally-block defense in depth**: the terminal-state branch now
   re-reads `credits_settled` from the DB before invoking
   `_settle_agent_credits`. If the DB already shows `TRUE`, the in-memory
   flag is set to match and settlement is skipped (logged as
   `agent_finally_settle_skipped_already_settled`). The subsequent
   `_persist_task` runs through the CAS guard, which rejects any further
   un-finalize attempt.

Both new DB reads use `final_row["credits_settled"] is True` (rather than
`bool(...)`) so a `MagicMock` DB in unit tests can't accidentally trip
the short-circuit path. asyncpg returns a real Python `bool` so the
production check is unchanged.

### Schema / migrations

None — the fix only uses existing columns (`state`, `credits_settled`,
`updated_at`), per the task constraint.

## 4. Test results

After fix:

```
tests/test_p01_stale_worker_race.py ......                               [100%]
6 passed in 1.85s
```

Full python suite:

```
================= 328 passed, 13 skipped, 2 warnings in 6.87s ==================
```

Frontend vitest:

```
Test Files  15 passed (15)
     Tests  144 passed (144)
```

322 prior python tests + 6 new P-01 tests = 328 (matches the brief target).
144 vitest tests unchanged. No prior test broken.

## 5. Files touched

- `mariana/agent/loop.py` — `_persist_task` CAS guard + bool return;
  `run_agent_task` pre-flight DB re-validation; finally-block defense in
  depth.
- `tests/test_p01_stale_worker_race.py` — new file, 6 regression tests.
- `loop6_audit/REGISTRY.md` — P-01 row marked **FIXED 2026-04-28**.
- `loop6_audit/P01_FIX_REPORT.md` — this document.
