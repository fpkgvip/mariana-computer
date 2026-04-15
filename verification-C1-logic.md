# Verification C1: Backend Logic & Data Integrity
Date: 2026-04-15
Auditor: Claude Sonnet (systematic)

## Files Reviewed

- mariana/api.py (3159 lines) — REST API
- mariana/main.py (982 lines) — orchestrator daemon
- mariana/orchestrator/event_loop.py (1957 lines) — core execution loop
- mariana/data/db.py (1062 lines) — database layer
- mariana/data/models.py (896 lines) — Pydantic models
- mariana/ai/session.py (940 lines) — AI session management
- mariana/ai/prompt_builder.py (842 lines) — prompt construction
- mariana/orchestrator/state_machine.py (618 lines) — FSM
- mariana/orchestrator/branch_manager.py (544 lines) — branch lifecycle
- mariana/orchestrator/cost_tracker.py (256 lines) — cost tracking
- mariana/orchestrator/checkpoint.py (395 lines) — checkpoint save/load
- mariana/config.py (376 lines) — configuration
- mariana/data/cache.py (410 lines) — Redis cache/dedup
- mariana/timer.py (474 lines) — research timer
- mariana/ai/router.py (348 lines) — model routing
- mariana/ai/output_parser.py (237 lines) — output parsing
- mariana/tools/finance.py (123 lines)
- mariana/tools/perplexity_search.py (128 lines)
- mariana/tools/memory.py (163 lines)
- mariana/tools/skills.py (247 lines)
- mariana/tools/doc_gen.py (not reviewed — doc generation helper)
- mariana/tools/video_gen.py (not reviewed — video generation)
- mariana/tools/image_gen.py (not reviewed — image generation)
- mariana/report/generator.py (344 lines) — report generation
- mariana/report/renderer.py (not reviewed — PDF rendering)
- mariana/tribunal/adversarial.py (482 lines) — adversarial tribunal
- mariana/tribunal/skeptic.py (433 lines) — skeptic review
- mariana/orchestrator/sub_agents.py (230 lines) — sub-agent delegation
- mariana/orchestrator/diminishing_returns.py (210 lines)
- mariana/connectors/base.py (234 lines) — connector base class
- mariana/connectors/polygon_connector.py (not reviewed)
- mariana/connectors/sec_edgar_connector.py (not reviewed)
- mariana/connectors/unusual_whales_connector.py (not reviewed)
- mariana/connectors/fred_connector.py (not reviewed)
- mariana/browser/pool_server.py (256 lines) — browser pool placeholder
- Dockerfile (36 lines)
- docker-compose.yml (105 lines)
- requirements.txt (not reviewed)

---

## Bugs Found

### BUG-C1-01: Two incompatible `BudgetExhaustedError` exception classes — session.py budget errors bypass the event loop handler

- **File**: `mariana/ai/session.py`, line 150; `mariana/orchestrator/cost_tracker.py`, line 27; `mariana/orchestrator/event_loop.py`, line 70
- **Severity**: critical
- **Description**: Two completely separate, unrelated classes are both named `BudgetExhaustedError`:
  - `mariana.ai.session.BudgetExhaustedError` (defined in `session.py` at line 150) — raised by `_check_budget()` inside `spawn_model()` when the pre-call budget check fails.
  - `mariana.orchestrator.cost_tracker.BudgetExhaustedError` (defined in `cost_tracker.py` at line 27) — raised by `CostTracker.record_call()` after recording a cost.
  
  `event_loop.py` imports only `from mariana.orchestrator.cost_tracker import BudgetExhaustedError` (line 70) and catches that class at line 429. The `session.py` class is a completely different type, so when `_check_budget()` raises `session.BudgetExhaustedError`, the `except BudgetExhaustedError` in `event_loop.py` does **not** catch it. The exception propagates to the outer `except Exception` handler at line 443, which marks the task as `FAILED` and re-raises — instead of the correct `HALTED` behaviour.

  This also affects the per-handler re-raises at lines 1004, 1091, and 1150: they catch `cost_tracker.BudgetExhaustedError` (from `record_call()`) but not the `session.BudgetExhaustedError` (from pre-call `_check_budget()`). A pre-call budget check failure in `handle_search`, `handle_evaluate`, or `handle_deepen` propagates directly to the outer `except Exception`, causing FAILED instead of HALTED.

- **Trigger**: Budget is exhausted before a `spawn_model()` call (e.g., when `cost_tracker.is_exhausted` is True at the start of a new iteration). `_check_budget()` is called first in `spawn_model()`, raises `session.BudgetExhaustedError`, which is a different class from what `event_loop.py` catches.
- **Impact**: Tasks that hit the budget cap via the pre-call check get marked FAILED (with error message) instead of HALTED (graceful stop). Emergency checkpoint is triggered but status is wrong. User-visible wrong status, incorrect credit settlement path.
- **Fix**: Either (a) make `session.BudgetExhaustedError` a subclass of `cost_tracker.BudgetExhaustedError`, or (b) import and re-export `cost_tracker.BudgetExhaustedError` from `session.py` instead of redefining it, or (c) have `_check_budget()` in `session.py` raise `cost_tracker.BudgetExhaustedError` instead of the local one:
  ```python
  # In session.py, replace local class with:
  from mariana.orchestrator.cost_tracker import BudgetExhaustedError
  # And update _check_budget() to raise BudgetExhaustedError(...) using the 
  # cost_tracker's constructor signature (scope, spent, cap), not the session one (cap_usd, spent_usd)
  ```

---

### BUG-C1-02: `_persist_task` does not save `metadata` field — in-memory task metadata mutations are lost on crash/restart

- **File**: `mariana/orchestrator/event_loop.py`, line 1829–1857
- **Severity**: major
- **Description**: The `_persist_task()` function updates the `research_tasks` row but omits the `metadata` column from the UPDATE statement. During an investigation, `handle_init()` updates `task.metadata` three times: adding `active_skill` (line 863), `user_memory_context` (line 879), and `sub_agent_findings` (line 900). None of these are persisted to the DB.

  If the container crashes and restarts, `load_latest_checkpoint` restores `task.current_state`, `task.diminishing_flags`, etc., but the restored `ResearchTask` will have whatever `metadata` was in the original DB row (only the initial `{user_id, tier, reserved_credits}` from task submission). The active skill, user memory context, and sub-agent findings are permanently lost.

  Additionally, the cost breakdown in `get_cost_breakdown` (api.py) reads `task.budget_usd` and `task.total_spent_usd` from the DB, which are kept in sync by `_sync_cost` — but any downstream code that relies on `task.metadata` after a resume will be missing these fields.

- **Trigger**: Any investigation that (1) triggers skill detection, user memory injection, or sub-agent delegation, AND (2) experiences a crash/restart mid-investigation, will lose the metadata context set in `handle_init`.
- **Impact**: After crash recovery, the AI will not benefit from the active skill's system prompt or user memory context (prompt injection is lost), resulting in less personalised/focused research. Sub-agent findings context is also lost.
- **Fix**: Add `metadata` to the `_persist_task` UPDATE:
  ```python
  # In _persist_task, add metadata to the SET clause:
  SET status = $1,
      current_state = $2,
      ...
      metadata = $12
  WHERE id = $13
  # And pass json.dumps(task.metadata) as parameter $12
  ```

---

### BUG-C1-03: `diminishing_returns.py` uses hardcoded `_SCORE_DELTA_THRESHOLD = 0.1` but ignores `config.DIMINISHING_SCORE_DELTA_THRESHOLD` (default 1.0)

- **File**: `mariana/orchestrator/diminishing_returns.py`, line 49; `mariana/config.py`, line 83
- **Severity**: major
- **Description**: `AppConfig.DIMINISHING_SCORE_DELTA_THRESHOLD` is defined as `1.0` (meaning: score delta must be less than 1.0 on a 0–1 scale — practically always true) and is configurable via the `DIMINISHING_SCORE_DELTA_THRESHOLD` environment variable. However, `diminishing_returns.py` never reads the config value; it uses a hardcoded module-level constant `_SCORE_DELTA_THRESHOLD: float = 0.1`.

  The docstring at line 49 says `config` is "reserved for future override support", but there's no mechanism to apply the override. The `check_diminishing_returns()` function receives `config: Any` as a parameter but never reads `config.DIMINISHING_SCORE_DELTA_THRESHOLD`.

  On the 0–1 score scale, 0.1 means any delta smaller than 10% is considered stale. The config default of 1.0 would essentially never trigger the flag (a delta of 1.0 on a 0–1 scale is impossible). This inconsistency means the operator cannot actually tune the score delta threshold via environment variable as advertised.

- **Trigger**: Setting `DIMINISHING_SCORE_DELTA_THRESHOLD` in the environment has no effect.
- **Impact**: The DR flag system operates with a fixed 0.1 threshold regardless of what the operator configures. Operators tuning this parameter will see no change in behaviour.
- **Fix**: Either use the config value:
  ```python
  score_delta_threshold = getattr(config, "DIMINISHING_SCORE_DELTA_THRESHOLD", _SCORE_DELTA_THRESHOLD)
  all_stale = (
      novelty < _NOVELTY_THRESHOLD
      and new_sources < _NEW_SOURCES_THRESHOLD
      and score_delta < score_delta_threshold
  )
  ```
  Or document that the config field is unused and remove `DIMINISHING_SCORE_DELTA_THRESHOLD` from `AppConfig` to avoid confusion.

---

### BUG-C1-04: `task.ai_call_counter` double-tracking — manual `+= 1` increments are silently overwritten by `_sync_cost`

- **File**: `mariana/orchestrator/event_loop.py`, lines 189, 747, 789, 1002, 1068, 1148, 1309, 1328, 1464, 1621 vs. line 1826
- **Severity**: minor
- **Description**: Throughout the event loop handlers, code does `task.ai_call_counter += 1` after each `spawn_model()` call. However, `_sync_cost()` (called before every `_persist_task()`) executes `task.ai_call_counter = cost_tracker.call_count` (line 1826), which overwrites all the `+= 1` increments with `cost_tracker.call_count`.

  Since `cost_tracker.call_count` is already incremented by `record_call()` inside `spawn_model()`, the value from `_sync_cost` is correct. However, between `spawn_model()` returning and `_sync_cost` being called, `task.ai_call_counter` temporarily reflects the manually incremented value. If anything reads `task.ai_call_counter` in that window, it will be off by the number of calls made since the last `_sync_cost`.

  More importantly, the `handle_report()` handler does `task.ai_call_counter += 2` (line 1621) to account for two AI calls inside `generate_report()`. But `generate_report()` calls `spawn_model()` twice, each time incrementing `cost_tracker.call_count` internally. When `_sync_cost` runs right after `handle_report()`, it replaces `task.ai_call_counter` with `cost_tracker.call_count` — which already includes both calls. The `+= 2` is therefore overwritten immediately and has no net effect.

- **Trigger**: Any investigation running the report generation phase.
- **Impact**: The `ai_call_counter` value on the task is always correct after `_sync_cost`, so the persisted DB value is correct. The issue is dead code confusion and a brief window where the in-memory counter is stale. No data corruption.
- **Fix**: Remove all `task.ai_call_counter += 1` (and `+= 2`) lines from the handlers, since `_sync_cost` handles the authoritative sync from `cost_tracker.call_count`. This makes the intent clear and eliminates the redundant increments.

---

### BUG-C1-05: `_supabase_patch_profile` uses unencoded `user_id` directly in URL query string

- **File**: `mariana/api.py`, line 2330
- **Severity**: minor
- **Description**: The `_supabase_patch_profile()` helper builds the URL as:
  ```python
  url = f"{cfg.SUPABASE_URL}/rest/v1/profiles?id=eq.{user_id}"
  ```
  The `user_id` is a Supabase auth UUID like `a34a319e-a046-4df2-8c98-9b83f6d512a0`, which only contains hexadecimal characters and hyphens. These are safe URL characters. However, unlike `_supabase_patch_profile_by_customer()` (which correctly URL-encodes the `stripe_customer_id` at line 2359), `_supabase_patch_profile()` does not encode `user_id`.

  In practice, Supabase UUIDs are always safe, so this is not a current vulnerability. However, if a future code path passes a non-UUID user_id (e.g., from a malformed JWT or a different auth provider), special characters could corrupt the PostgREST filter expression.

- **Trigger**: A user_id containing special URL characters (e.g., `%`, `?`, `&`).
- **Impact**: Malformed URL sent to Supabase, potentially incorrect profile update or 400 error.
- **Fix**: Apply `_url_quote(user_id, safe='')` consistently, matching the pattern already used in `_supabase_patch_profile_by_customer()`.

---

### BUG-C1-06: `_run_daemon` re-processes resumed `.running` files into `active_tasks` but `_run_single_guarded` may try to rename `.running` → `.done`/`.error` even if it was already renamed on a previous iteration

- **File**: `mariana/main.py`, lines 699–718
- **Severity**: minor
- **Description**: When the daemon starts, it scans for `*.running` files and spawns resume tasks. `_run_single_guarded` (line 597) accepts a `task_file: Path` parameter and renames it to `.done` or `.error` on completion (lines 634–638). However, if the file is at path `foo.running`, `task_file.with_suffix(".done")` becomes `foo.done` and `task_file.with_suffix(".error")` becomes `foo.error` — which is correct.

  The actual issue is that the `.running` file rename in the main polling loop (line 788) happens AFTER the file is read. On a concurrent filesystem or if two daemon instances start simultaneously (e.g., during a rolling deploy), the same `.task.json` file could be read and renamed by two daemons. The first rename succeeds; the second raises `FileNotFoundError` since the file was already renamed.

- **Trigger**: Two daemon processes running simultaneously (e.g., during container restart with `restart: unless-stopped` and overlapping start times), or the polling loop iteration and the resume-scan processing the same file.
- **Impact**: `FileNotFoundError` from `tf.rename(running_file)` at line 788 is not caught; it propagates out of the `for tf in task_files` loop, breaking the entire poll iteration. The `try/except (OSError, json.JSONDecodeError)` at line 749 does not wrap the rename.
- **Fix**: Wrap the rename in a try/except and treat `FileNotFoundError` as already-processed:
  ```python
  try:
      tf.rename(running_file)
  except FileNotFoundError:
      logger.warning("daemon_task_already_claimed", file=tf.name)
      continue
  ```

---

### BUG-C1-07: `handle_init` spawns sub-agents but the `architecture` variable may not be in scope if step 1 raises before step 2

- **File**: `mariana/orchestrator/event_loop.py`, line 894
- **Severity**: minor
- **Description**: In `handle_init()`, the sub-agent delegation block at line 884–903 references `architecture.topic_analysis` (line 894). However, `architecture` is assigned at line 749 (`architecture: ResearchArchitectureOutput = arch_output`). The sub-agent code is inside a `try/except` at line 902 that swallows all exceptions, so if `architecture` were somehow None or undefined (e.g., if the code path changed and step 1 were skipped), the AttributeError would be silently swallowed. This is not currently reachable since `architecture` is always assigned before the sub-agent block, but it is fragile.

  The actual code is safe as written. This is a minor code quality issue, not a runtime bug.

- **Trigger**: Not currently triggerable.
- **Impact**: If future refactoring moves the architecture step, silent AttributeError would be swallowed by the broad `except Exception`.
- **Fix**: Reference `arch_output` (the parsed output) directly, or guard with `if architecture:` before accessing attributes.

---

### BUG-C1-08: `_record_webhook_event_once` returns incorrect result when `INSERT ... ON CONFLICT DO NOTHING` returns "INSERT 0 0"

- **File**: `mariana/api.py`, line 2531
- **Severity**: major
- **Description**: The function `_record_webhook_event_once()` uses:
  ```python
  result = await db.execute(
      "INSERT INTO stripe_webhook_events ... ON CONFLICT (event_id) DO NOTHING",
      ...
  )
  return result.split()[-1] == "1"
  ```
  When asyncpg's `execute()` returns a tag for an `INSERT ... ON CONFLICT DO NOTHING`, it returns either `"INSERT 0 1"` (row inserted) or `"INSERT 0 0"` (conflict, nothing inserted). The function correctly checks if the last part is `"1"`.

  However, if the database returns an unexpected format (e.g., `"INSERT 1"` on some PostgreSQL versions or proxies), `result.split()[-1]` might return `"1"` for a non-insert result. Similarly, the check `== "1"` (string comparison, not integer) is correct for asyncpg's format but fragile if the format ever changes.

  More concretely: when a webhook replay occurs, `ON CONFLICT DO NOTHING` inserts 0 rows and returns `"INSERT 0 0"`. `result.split()[-1]` is `"0"`, so `return result.split()[-1] == "1"` correctly returns `False`. This is correct behaviour. **However**, if the `db.execute()` call itself raises an exception (e.g., DB unavailable), the exception propagates unhandled from `_record_webhook_event_once`, and the Stripe webhook handler at line 2129 will propagate it to the outer `except Exception` at line 2152 which returns a 200 response with `handler_error`. This is correct Stripe webhook semantics (don't retry handler bugs), but the event is not recorded in the DB, so on Stripe's retry the `_record_webhook_event_once` will attempt insert again — potentially crediting the user twice if the second attempt succeeds.

- **Trigger**: Database unavailability at the moment of webhook delivery.
- **Impact**: If DB is temporarily unavailable when a `checkout.session.completed` webhook arrives, Stripe retries, the second attempt succeeds, and the user gets credited twice. The idempotency guard only works if the first INSERT actually commits.
- **Fix**: This is an inherent limitation of the idempotency design when the DB can fail. A more robust approach would use a distributed lock or Redis for idempotency, or accept duplicate credits as a business risk. As a minimum, catch and re-raise DB exceptions from `_record_webhook_event_once` as a 500 (not wrapped in the generic `except Exception`) so Stripe retries but the in-process handling is clear.

---

### BUG-C1-09: `_supabase_add_credits` fallback read-modify-write is not atomic and can corrupt credit balances

- **File**: `mariana/api.py`, lines 2406–2440
- **Severity**: major
- **Description**: The `_supabase_add_credits()` function has a fallback when the `add_credits` RPC is unavailable. The fallback:
  1. GETs the current `tokens` value (line 2412)
  2. Computes `current_tokens + credits`
  3. PATCHes with the new value (line 2422)

  Steps 1 and 3 are not atomic. A race condition exists: if two checkout webhooks arrive for the same user simultaneously (which can happen if Stripe delivers events redundantly), both read the same `current_tokens`, both compute `current_tokens + credits`, and both PATCH the same new value — resulting in only ONE credit grant being recorded instead of two.

  The docstring for `_supabase_deduct_credits()` explicitly says "we do not attempt a read-modify-write fallback, because that sequence is not atomic" — the same reasoning applies here to `_supabase_add_credits`.

- **Trigger**: Two concurrent webhook deliveries for the same user when the `add_credits` RPC is unavailable.
- **Impact**: Under-crediting: user paid twice but only received credits once.
- **Fix**: If the RPC fails, either (a) fail loudly and let Stripe retry rather than using the unsafe fallback, or (b) use Supabase's PostgREST atomic increment: `PATCH /profiles?id=eq.{user_id}` with the header `Content-Type: application/json` and body `{"tokens": "tokens+{credits}"}` (PostgREST supports this). The latter eliminates the read-modify-write race.

---

### BUG-C1-10: `handle_init` mutates `task.metadata` without persisting — skill/memory context lost after `_persist_task`

- **File**: `mariana/orchestrator/event_loop.py`, lines 863, 879, 900; `_persist_task` at line 1829
- **Severity**: minor
- **Description**: `handle_init()` mutates `task.metadata` in three places (active skill, user memory context, sub-agent findings). After `handle_init()` completes, the action executor returns, and then `_persist_task()` is called in the main loop at line 376. But `_persist_task()` does NOT include `metadata` in its UPDATE. So even within a single live daemon run (no crash), the skill/memory/sub-agent context stored in `task.metadata` is not written to the DB.

  This means if anyone calls `GET /api/investigations/{task_id}` during the run and then uses the returned data to look up skill context, they won't find it. It also means the checkpoint blob (which does serialise `task.model_dump()`) has the metadata, but the `research_tasks` DB row doesn't.

- **Trigger**: Any investigation with skill detection, user memory injection, or sub-agent delegation active.
- **Impact**: `task.metadata` in DB remains the initial submission payload only. Any code reading task metadata from DB (vs. the in-memory task object) will not see skill/memory context. This is a data loss of metadata enrichment between in-memory and DB state.
- **Fix**: Same as BUG-C1-02 — add `metadata` to `_persist_task`.

---

### BUG-C1-11: `_SHUTDOWN.wait()` in `_run_daemon` poll loop has a logic issue — `asyncio.wait_for` timeout exception is silently swallowed instead of detecting real shutdown

- **File**: `mariana/main.py`, lines 810–813
- **Severity**: minor
- **Description**: The daemon poll loop uses:
  ```python
  try:
      await asyncio.wait_for(_SHUTDOWN.wait(), timeout=10.0)
  except asyncio.TimeoutError:
      pass  # Normal — no shutdown signal yet.
  ```
  `_SHUTDOWN.wait()` is an async method that calls `loop.run_in_executor(None, lambda: self._flag.wait(timeout=10.0))`. The executor wait itself has a 10-second timeout. So `_SHUTDOWN.wait()` will return after ~10 seconds regardless (whether or not shutdown is set). The outer `asyncio.wait_for(..., timeout=10.0)` adds another 10-second timeout layer on top.

  If shutdown IS set, `_flag.wait(timeout=10.0)` returns immediately (True), the executor completes, `_SHUTDOWN.wait()` returns, and `asyncio.wait_for` returns normally (no TimeoutError). The `while not _SHUTDOWN.is_set()` check at line 723 then exits the loop correctly.

  The issue is that if shutdown is NOT set, `_flag.wait(timeout=10.0)` in the executor takes 10 seconds (the executor thread blocks for 10 seconds), then returns False, the coroutine returns, and `asyncio.wait_for` also returns normally (not via TimeoutError). So both the `asyncio.TimeoutError` catch AND the normal return paths lead to the same place. The `except asyncio.TimeoutError: pass` branch is effectively dead code — `asyncio.wait_for` would only raise `TimeoutError` if the inner executor was still running at 10 seconds, but it completes within ~10 seconds anyway.

  This is not functionally broken (the loop works correctly), but the `asyncio.wait_for` wrapper is redundant and the comment "Normal — no shutdown signal yet" is misleading about why TimeoutError would be raised.

- **Trigger**: Normal daemon operation.
- **Impact**: No functional impact — the poll interval is ~10 seconds as intended. Dead code only.
- **Fix**: Remove the `asyncio.wait_for` wrapper and just `await _SHUTDOWN.wait()` directly (it already has its own 10-second timeout internally). Or replace with `await asyncio.sleep(10.0)` plus a break check.

---

## Verified Correct

The following logic was specifically checked and confirmed correct:

1. **JWT authentication (BUG-V2-01 fix)**: `_authenticate_supabase_token` correctly verifies tokens via Supabase `/auth/v1/user` instead of base64-decoding. Auth is applied to all protected endpoints.

2. **SQL injection protection**: `update_research_task` and `update_branch` in `db.py` correctly use module-level `_ALLOWED_TASK_COLUMNS` and `_ALLOWED_BRANCH_COLUMNS` allowlists. Dynamic column names are validated before interpolation. All other queries use parameterised `$N` placeholders.

3. **Stripe webhook idempotency**: `_record_webhook_event_once` uses `INSERT ... ON CONFLICT DO NOTHING` correctly; replay detection works for the normal case. Signature verification with `STRIPE_WEBHOOK_SECRET` is required (503 if unset).

4. **Credit race condition prevention**: `_supabase_deduct_credits` correctly requires the atomic `deduct_credits` RPC and refuses to fall back to read-modify-write. Credit reservation happens before task file write; refund on exception is implemented.

5. **Budget hard cap enforcement**: `CostTracker.record_call()` enforces both per-branch and task-level caps. The `is_exhausted` check in the main loop and state machine correctly redirects to HALT. `BudgetExhaustedError` from `cost_tracker.record_call()` (post-call) is correctly caught in `event_loop.py`.

6. **Branch scoring logic**: `score_branch()` correctly appends score first, then applies KILL/CONTINUE/GRANT logic. Plateau detection uses `_PLATEAU_MIN_CYCLES` guard. Grant amounts are capped at `BUDGET_HARD_CAP`.

7. **Checkpoint atomicity**: `save_checkpoint` writes to `.tmp` first, inserts into DB, then renames. If DB fails, the temp file is cleaned up. If rename fails, the DB snapshot_path is nulled. This is correct two-phase commit behaviour.

8. **Path traversal protection**: PDF/DOCX report download checks `resolved.is_relative_to(data_root)`. File download endpoint checks against `files_root` (task-scoped, not just DATA_ROOT). Upload filenames are sanitized with `re.sub(r"[^\w\-.]", "_", filename)`.

9. **SSE authentication re-check**: The log stream generator re-validates the auth token every 30 seconds to detect revoked tokens during long-running streams.

10. **State machine transitions**: All (state, trigger) pairs are defined in `TRANSITION_TABLE`. Guard conditions in `_apply_guards` correctly handle budget checks for PIVOT/HALT decisions. `InvalidTransitionError` is caught in the main loop and treated as HALT.

11. **Diminishing returns flag logic**: `check_diminishing_returns` correctly increments on all-stale and resets on any non-stale. The flag count drives recommendations: 0=CONTINUE, 1=SEARCH_DIFFERENT_SOURCES, 2=PIVOT, 3+=HALT.

12. **asyncpg JSONB handling**: JSONB columns are correctly serialised with `json.dumps()` when writing and decoded via `_row_to_dict()` when reading. The `ON CONFLICT ... DO NOTHING` patterns are correct for idempotent inserts.

13. **Redis pub/sub lifecycle**: SSE generator correctly unsubscribes and closes pubsub on disconnect (`finally: await pubsub.unsubscribe(); await pubsub.aclose()`). Fire-and-forget tasks hold strong references via `_background_tasks` set to prevent GC.

14. **Docker/deployment**: Dockerfile correctly runs as non-root `mariana` user. `docker-compose.yml` uses health checks before starting dependent services. Redis binds on the internal network only. No secrets in environment.

15. **Config validation**: `AppConfig.__post_init__` validates budget ordering invariants and grant amounts vs hard cap. `load_config()` raises `RuntimeError` if `POSTGRES_PASSWORD` is missing without `POSTGRES_DSN`.

16. **Output parser robustness**: Three-tier JSON extraction (json-fence → bare-fence → inline → raw) with correct CRLF handling. Pydantic validation errors are captured and re-raised as `OutputParseError`. One-shot repair retry with error hint injection.

17. **Memory file operations**: `UserMemory` uses `json.dumps(default=str)` for serialization safety. Fact deduplication by content hash. History capped at 100 entries.

18. **Skill path traversal**: `_safe_skill_path()` resolves the path and checks it stays within `base_dir`. Skill IDs are sanitized to `[a-z0-9_-]` only before filesystem operations.

19. **QueryDedup atomic check**: `check_and_record()` uses a Lua script for atomic check-and-set, preventing the race between `is_duplicate` and `record_query`.

20. **Concurrent investigation semaphore**: `_MAX_CONCURRENT_INVESTIGATIONS = 4` enforced via `asyncio.Semaphore` in daemon mode. Shutdown waits up to 120s for active tasks to complete before cancellation.
