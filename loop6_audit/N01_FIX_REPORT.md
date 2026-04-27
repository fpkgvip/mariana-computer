# N-01 Fix Report — agent settlement metadata persistence

- **Bug:** P1 — M-01's `reserved_credits` / `credits_settled` were added only
  to the in-memory `AgentTask` Pydantic model. The Postgres table, INSERT,
  SELECT, and UPSERT all lacked these columns, so the queue-consumer worker
  reload path silently dropped the reservation metadata and
  `_settle_agent_credits` short-circuited on `task.reserved_credits <= 0`.
- **Branch:** `loop6/zero-bug`
- **Fix landed:** 2026-04-28
- **Audit source:** `loop6_audit/A15_phase_e_reaudit.md` (Phase E re-audit #10)

## 1. RED phase

New regression file `tests/test_n01_settlement_persistence.py` (6 tests) was
authored against current HEAD `99d93be` and run first to confirm all six
failed:

```
PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb \
  python -m pytest tests/test_n01_settlement_persistence.py
```

Output (RED):

```
FAILED tests/test_n01_settlement_persistence.py::test_n01_schema_columns_present
FAILED tests/test_n01_settlement_persistence.py::test_n01_insert_persists_reserved_credits
FAILED tests/test_n01_settlement_persistence.py::test_n01_persist_task_upserts_credits_settled
FAILED tests/test_n01_settlement_persistence.py::test_n01_persist_task_upserts_reserved_credits_change
FAILED tests/test_n01_settlement_persistence.py::test_n01_round_trip_through_settlement
FAILED tests/test_n01_settlement_persistence.py::test_n01_round_trip_no_double_settle
============================== 6 failed in 1.80s ===============================
```

Each test exercises a real local Postgres (PGHOST=/tmp PGPORT=55432) and
bootstraps the agent schema from `mariana/agent/schema.sql` exactly the way
`mariana.data.db.init_schema` does at startup, then drives the
`_insert_agent_task` → `_load_agent_task` → mutate → `_persist_task` →
`_load_agent_task` round-trip used by the production queue consumer.

## 2. Schema diff

`mariana/agent/schema.sql`

```sql
@@ inside CREATE TABLE IF NOT EXISTS agent_tasks
     spent_usd                DOUBLE PRECISION NOT NULL DEFAULT 0.0,
+
+    -- M-01 / N-01: agent credit reservation accounting.
+    reserved_credits         BIGINT NOT NULL DEFAULT 0,
+    credits_settled          BOOLEAN NOT NULL DEFAULT FALSE,

@@ after the table block (idempotent in-place upgrade)
+ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS reserved_credits BIGINT NOT NULL DEFAULT 0;
+ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS credits_settled  BOOLEAN NOT NULL DEFAULT FALSE;
```

The CREATE TABLE addition handles fresh databases. `ALTER TABLE … ADD COLUMN
IF NOT EXISTS` handles already-deployed databases where CREATE TABLE IF NOT
EXISTS would otherwise be a no-op.

## 3. Code diff

### `mariana/agent/api_routes.py:_insert_agent_task` (78–120)

- Added `reserved_credits, credits_settled` to the column list (after
  `spent_usd`).
- Added two more `$N` placeholders, renumbered the rest ($13… → $15…).
- Pass `task.reserved_credits` / `task.credits_settled` after `task.spent_usd`.

### `mariana/agent/api_routes.py:_load_agent_task` (123–177)

- Added the two columns to the SELECT.
- Added `"reserved_credits": int(row["reserved_credits"])` and
  `"credits_settled": bool(row["credits_settled"])` to the reconstruction
  dict so `AgentTask.model_validate(...)` rehydrates them.

### `mariana/agent/loop.py:_persist_task` (80–135)

- Added the columns to the INSERT and renumbered the placeholders.
- Added to the `ON CONFLICT (id) DO UPDATE SET` clause:
  ```sql
  reserved_credits = EXCLUDED.reserved_credits,
  credits_settled  = EXCLUDED.credits_settled,
  ```
- Added the two task fields to the parameter list immediately after
  `task.spent_usd`.

### `mariana/agent/loop.py:run_agent_task` finally block

Already calls `_settle_agent_credits(task)` BEFORE the final
`_persist_task(db, task)` (lines 885–902). Confirmed unchanged — the existing
ordering means `credits_settled=True` is captured by the same UPSERT that
records the terminal state, so a worker crash after settlement but before
requeue cannot produce a double-refund.

### `mariana/agent/models.py`

Updated the `reserved_credits` / `credits_settled` docstring to reflect that
the fields are now persisted to Postgres (was: "neither field is persisted").

## 4. Test results

GREEN run after the fix:

```
$ PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb \
    python -m pytest tests/test_n01_settlement_persistence.py -v
tests/test_n01_settlement_persistence.py::test_n01_schema_columns_present PASSED
tests/test_n01_settlement_persistence.py::test_n01_insert_persists_reserved_credits PASSED
tests/test_n01_settlement_persistence.py::test_n01_persist_task_upserts_credits_settled PASSED
tests/test_n01_settlement_persistence.py::test_n01_persist_task_upserts_reserved_credits_change PASSED
tests/test_n01_settlement_persistence.py::test_n01_round_trip_through_settlement PASSED
tests/test_n01_settlement_persistence.py::test_n01_round_trip_no_double_settle PASSED
============================== 6 passed in 1.70s ===============================
```

Full suite:

```
$ PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb \
    python -m pytest tests/
================= 315 passed, 13 skipped, 2 warnings in 6.24s ==================
```

309 prior tests + 6 new = **315 passed, 13 skipped, 0 failures**.

## 5. Files touched

- `mariana/agent/schema.sql` — schema columns + idempotent ALTERs.
- `mariana/agent/api_routes.py` — `_insert_agent_task`, `_load_agent_task`.
- `mariana/agent/loop.py` — `_persist_task` UPSERT.
- `mariana/agent/models.py` — docstring corrected (no behavior change).
- `tests/test_n01_settlement_persistence.py` — new (6 tests).
- `loop6_audit/REGISTRY.md` — N-01 row marked FIXED.
- `loop6_audit/N01_FIX_REPORT.md` — this file.

No existing tests were modified or removed (M-01 unit tests untouched).
