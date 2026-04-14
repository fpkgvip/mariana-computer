# Mariana Computer — Core Bug Fix Summary

All fixes applied with targeted edits. No full-file rewrites.
Verification: `python -c "from mariana.config import ...; from mariana.orchestrator.state_machine import *; ..."` — **All core modules import OK**.

---

## BUG-01: event_loop.py `run()` signature mismatch
**File**: `mariana/orchestrator/event_loop.py`

Added `cost_tracker: Any = None` and `shutdown_flag: Any = None` parameters to `run()`.
The function now accepts the 6-argument call from `main.py:429-436` without a `TypeError`.

---

## BUG-02: event_loop.py references `config.data_root` (lowercase)
**File**: `mariana/orchestrator/event_loop.py` lines 135, 154

Changed both occurrences of `config.data_root` → `config.DATA_ROOT`.
`AppConfig` has no `data_root` property — only `DATA_ROOT` (the field) and derived properties (`checkpoints_dir`, etc.).

---

## BUG-03 (part of BUG-01): CostTracker created unconditionally / wrong kwarg
**File**: `mariana/orchestrator/event_loop.py` lines 116-120

Changed from always creating a new `CostTracker` to only creating one when `cost_tracker is None`.
Also fixed the fallback kwarg: `getattr(config, "branch_hard_cap", 75.0)` → `getattr(config, "BUDGET_BRANCH_HARD_CAP", 75.0)` (matches the actual `AppConfig` field name).

---

## BUG-05: main.py `_create_db_pool` passes config object to `db.create_pool`
**File**: `mariana/main.py` lines 225-238

`db.create_pool(dsn, min_size, max_size, command_timeout)` takes raw args, not a config object.
Changed `create_pool(config)` to `create_pool(dsn=config.POSTGRES_DSN, min_size=config.POSTGRES_POOL_MIN, max_size=config.POSTGRES_POOL_MAX)`.

---

## BUG-07: Add `FRED_API_KEY` to config
**File**: `mariana/config.py`

Added `FRED_API_KEY: str = ""` to `AppConfig` dataclass (after `UNUSUAL_WHALES_API_KEY`).
Added `FRED_API_KEY=_str("FRED_API_KEY", ""),` to `load_config()` return statement.

---

## BUG-08: Add `DEEPSEEK_API_KEY` to config
**File**: `mariana/config.py`

Added `DEEPSEEK_API_KEY: str = ""` to `AppConfig` dataclass (after `FRED_API_KEY`).
Added `DEEPSEEK_API_KEY=_str("DEEPSEEK_API_KEY", ""),` to `load_config()` return statement.

---

## BUG-10: Add `report_generations` table to schema
**File**: `mariana/data/db.py`

Added to `_SCHEMA_SQL`:
```sql
CREATE TABLE IF NOT EXISTS report_generations (
    id              SERIAL      PRIMARY KEY,
    task_id         TEXT        NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    pdf_path        TEXT,
    docx_path       TEXT,
    report_cost_usd NUMERIC    NOT NULL DEFAULT 0,
    generated_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_report_generations_task_id ON report_generations(task_id);
```

---

## BUG-12: branch_manager `create_branch` — JSON serialize lists for asyncpg JSONB
**File**: `mariana/orchestrator/branch_manager.py`

Added `import json` at the top.
In `create_branch` (line ~150), wrapped three list params with `json.dumps()`:
- `branch.score_history` → `json.dumps(branch.score_history)`
- `branch.grants_log` → `json.dumps(branch.grants_log)`
- `branch.sources_searched` → `json.dumps(branch.sources_searched)`

---

## BUG-13: branch_manager `grant_budget` — parse `grants_log` safely
**File**: `mariana/orchestrator/branch_manager.py` line 436

Replaced `list(row["grants_log"] or [])` with safe type-dispatch:
```python
raw_grants = row["grants_log"]
if isinstance(raw_grants, str):
    current_grants = json.loads(raw_grants) if raw_grants else []
elif isinstance(raw_grants, list):
    current_grants = list(raw_grants)
else:
    current_grants = []
```
When writing back, changed `current_grants` → `json.dumps(current_grants)`.

---

## BUG-14: event_loop `handle_evaluate` — spurious score × 10.0
**File**: `mariana/orchestrator/event_loop.py` line 663

Changed: `new_score = float(eval_row["score"]) * 10.0 if eval_row else 5.0`
To:      `new_score = float(eval_row["score"]) if eval_row else 5.0`

The evaluation AI already outputs on a 0–10 scale per the architecture spec. Multiplying by 10 was producing scores of 0–100.

---

## BUG-15: Add `evaluation_results` table to schema
**File**: `mariana/data/db.py`

Added to `_SCHEMA_SQL`:
```sql
CREATE TABLE IF NOT EXISTS evaluation_results (
    id          TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    task_id     TEXT        NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
    branch_id   TEXT        NOT NULL,
    score       NUMERIC     NOT NULL,
    reasoning   TEXT,
    next_search_keywords JSONB DEFAULT '[]',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_evaluation_results_task_id ON evaluation_results(task_id);
CREATE INDEX IF NOT EXISTS idx_evaluation_results_branch_id ON evaluation_results(branch_id);
```
Required by `handle_evaluate` which queries `SELECT score FROM evaluation_results WHERE ...`.

---

## BUG-16: state_machine.py duplicate CHECKPOINT transition
**File**: `mariana/orchestrator/state_machine.py` line 220

Removed the duplicate dict entry:
```python
# REMOVED:
(State.CHECKPOINT, TransitionTrigger.DIMINISHING_RETURNS): State.SEARCH,  # flags==1 (see guard)
```
Python dicts keep the last assignment, so the duplicate `→ SEARCH` entry was silently overwriting `→ PIVOT`. The single remaining entry is `→ PIVOT`; `_apply_guards` already handles the `flags==1 → SEARCH` case correctly via guard logic.

---

## Also fixed in BUG-12: `_persist_branch` JSON serialization
**File**: `mariana/orchestrator/branch_manager.py` (~line 514)

`_persist_branch` was also passing the raw `branch.score_history` list directly to asyncpg.
Changed to `json.dumps(branch.score_history)` for consistency.

---

## Verification

```
cd /home/user/workspace/mariana && python -c "
import sys; sys.path.insert(0, '.')
from mariana.config import AppConfig, load_config
from mariana.data.models import *
from mariana.orchestrator.state_machine import *
from mariana.orchestrator.cost_tracker import *
from mariana.orchestrator.branch_manager import *
from mariana.orchestrator.checkpoint import *
from mariana.orchestrator.diminishing_returns import *
print('All core modules import OK')
"
```

**Result: All core modules import OK** ✓
