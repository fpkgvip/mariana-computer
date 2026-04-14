# Mariana Computer — Full Bug Audit

## CRITICAL BUGS (will crash at runtime)

### BUG-01: event_loop.py `run()` — wrong signature, double cost tracking
- **File**: `event_loop.py:91-96`
- **Issue**: `run()` takes `(task, db, redis_client, config)` — 4 args.
  But `main.py:429-436` calls it with 6 kwargs: `(task, db, redis_client, config, cost_tracker, shutdown_flag)`.
- **Fix**: Add `cost_tracker` and `shutdown_flag` params to `run()`, use them instead of creating a new CostTracker inside.

### BUG-02: event_loop.py `run()` — references `config.data_root` (lowercase)
- **File**: `event_loop.py:135, 154`
- **Issue**: Config uses `DATA_ROOT` (UPPER_CASE). `config.data_root` is a lowercase property that works... but only from properties `checkpoints_dir`, `reports_dir` etc. Direct access at line 135 calls `config.data_root` which doesn't exist — `data_root` is not a property on AppConfig, only `DATA_ROOT` is the field and `checkpoints_dir` etc use `self.DATA_ROOT`.
- **Wait**: Actually re-reading config.py, `data_root` is NOT a property. Only `checkpoints_dir`, `reports_dir`, `findings_dir`, `inbox_dir` are properties that use `self.DATA_ROOT`. So `config.data_root` will raise AttributeError.
- **Fix**: Change `config.data_root` → `config.DATA_ROOT` at lines 135, 154.

### BUG-03: event_loop.py `run()` — creates its own CostTracker with wrong kwarg
- **File**: `event_loop.py:116-120`
- **Issue**: `CostTracker(task_id=..., task_budget=..., branch_hard_cap=getattr(config, "branch_hard_cap", 75.0))`.
  Config has `BUDGET_BRANCH_HARD_CAP`, not `branch_hard_cap`. getattr falls back to 75.0 which happens to be correct, but if main.py already passes a cost_tracker, this creates a DUPLICATE that gets out of sync.
- **Fix**: After fixing BUG-01, remove the internal CostTracker creation and use the one passed from main.py.

### BUG-04: event_loop.py — `_trigger_for_tribunal` and `_trigger_for_skeptic` call `db.fetchrow()` directly
- **File**: `event_loop.py:468, 496`
- **Issue**: `db` is an asyncpg.Pool, not a Connection. Pool doesn't have `.fetchrow()` directly — you need `async with db.acquire() as conn: conn.fetchrow(...)`.
- **Fix**: Wrap in `async with db.acquire() as conn:`.

### BUG-05: db.py `create_pool()` — expects bare DSN args, main.py passes config object
- **File**: `db.py:44-49`, `main.py:228-229`
- **Issue**: `db.create_pool(dsn, min_size, max_size, command_timeout)` — takes raw string+ints.
  `main.py:229` calls `create_pool(config)` — passes the entire config object.
- **Fix**: Change main.py's `_create_db_pool` to extract `config.POSTGRES_DSN, config.POSTGRES_POOL_MIN, config.POSTGRES_POOL_MAX` and pass them individually.

### BUG-06: event_loop.py — `spawn_model()` call signature mismatch
- **File**: Multiple handlers in event_loop.py (lines 540-549, 594-603, etc.)
- **Issue**: Event loop handlers call `spawn_model(task_id=..., task_type=..., branch_id=..., prompt_context=..., config=...)`.
  But `session.py:587` defines `spawn_model(task_type, context, output_schema, max_tokens, use_batch, branch_id, db, cost_tracker, config)`.
  The event loop: (a) passes `task_id` which isn't a param, (b) passes `prompt_context` instead of `context`, (c) doesn't pass `output_schema` (required), (d) doesn't pass `db` or `cost_tracker`.
  And spawn_model returns `(parsed_output, session)` tuple, but event_loop treats return as just `session`.
- **Fix**: Rewrite all handler calls to match spawn_model's actual signature.

### BUG-07: FRED connector references `config.FRED_API_KEY`
- **File**: `fred_connector.py` (line ~60)
- **Issue**: AppConfig has no `FRED_API_KEY` attribute.
- **Fix**: Add `FRED_API_KEY: str = ""` to AppConfig and to `load_config()`.

### BUG-08: router.py DeepSeek health check references `config.DEEPSEEK_API_KEY`
- **File**: `router.py:308`
- **Issue**: AppConfig has no `DEEPSEEK_API_KEY`. DeepSeek is called through the LLM Gateway, not directly.
- **Fix**: Either add the field or change the health check to use the gateway URL.

### BUG-09: event_loop.py `handle_report` calls `compile_report` which doesn't exist
- **File**: `event_loop.py:902`
- **Issue**: `from mariana.report.generator import compile_report` — but generator.py exports `generate_report`, not `compile_report`.
- **Fix**: Change to `generate_report` and pass the right arguments.

### BUG-10: report/generator.py writes to `report_generations` table — not in schema
- **File**: `generator.py:307-317`
- **Issue**: `INSERT INTO report_generations` — this table doesn't exist in `db.py`'s `_SCHEMA_SQL`.
- **Fix**: Add the table to the schema.

### BUG-11: event_loop `_build_session_data` and `_persist_task` call `db.fetch/execute` directly on Pool
- **File**: `event_loop.py:1060, 1072, 1085, 1097, 1116`
- **Issue**: Same as BUG-04 — asyncpg.Pool doesn't expose fetch/execute directly (it does in some versions but it's Pool.fetch vs Connection.fetch).
- **Actually**: asyncpg.Pool DOES have .fetch(), .fetchrow(), .execute() as convenience methods. This is actually fine. Let me re-check... Yes, asyncpg.Pool does proxy these. So BUG-04 and BUG-11 are NOT bugs. Removing.

### BUG-12: branch_manager `create_branch` passes Python list to asyncpg for JSONB columns
- **File**: `branch_manager.py:150-158`
- **Issue**: Passes `branch.score_history` (a Python list) directly to asyncpg for a JSONB column. asyncpg expects a string for JSONB or the column needs codec setup.
- **Fix**: JSON-serialize list/dict values before passing: `json.dumps(branch.score_history)`.

### BUG-13: branch_manager `grant_budget` reads `grants_log` from DB — may be string not list
- **File**: `branch_manager.py:436`
- **Issue**: `current_grants: list[dict] = list(row["grants_log"] or [])` — if asyncpg returns a string (JSONB stored as text), this creates a list of characters.
- **Fix**: Parse with `json.loads()` if it's a string.

## MEDIUM BUGS (wrong behavior but won't crash)

### BUG-14: event_loop `handle_evaluate` — score multiplication
- **File**: `event_loop.py:663`
- **Issue**: `new_score = float(eval_row["score"]) * 10.0` — if the evaluation AI already outputs on a 0-10 scale (which the architecture spec says), this multiplies by 10 making scores 0-100.
- **Fix**: Remove the `* 10.0`.

### BUG-15: `_SHUTDOWN` flag in event_loop.py is never checked
- **Issue**: `run()` doesn't check `shutdown_flag`; the main loop just runs until HALT.
- **Fix**: Check `shutdown_flag.is_set()` in the main loop.

### BUG-16: CHECKPOINT duplicate transition table entry
- **File**: `state_machine.py:219-220`
- **Issue**: Two entries for `(CHECKPOINT, DIMINISHING_RETURNS)` — Python dict keeps the last one only, so line 220 (→SEARCH) overwrites line 219 (→PIVOT).
- **Fix**: Remove duplicate; the guard in `_apply_guards` handles both cases correctly.

## LOW PRIORITY

### BUG-17: Missing `__init__.py` files
- Several packages may be missing proper `__init__.py` exports.

### BUG-18: requirements.txt may be incomplete
- Need to verify all imports have corresponding packages.
