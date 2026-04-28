# CC-20 + CC-21 fix report

Date: 2026-04-28
Branch: `loop6/zero-bug`
Commits:
- CC-20: `f53c7cd` — `CC-20 scrub agent api_routes responses and SSE events of raw exception text and task IDs`
- CC-21: `443d15d` — `CC-21 scrub agent loop task-error fields of raw exception text in favor of stable codes`

## Canonical user-visible error_code list

The following stable codes are now the ONLY values that may appear in the user-visible `task.error` / `step.error` / SSE `error_code` fields. Raw exception class names, messages, and tracebacks remain available server-side via `logger.warning(...)` / `log.error(...)` / `log.exception(...)` with `error_type=type(exc).__name__` + `error=str(exc)` in structured `extra`.

Agent loop / agent api routes:
- `vault_unavailable` — any `VaultUnavailableError` (Redis down, missing client, missing payload)
- `vault_transport_violation` — `ValueError` from REDIS_URL transport policy (e.g. plaintext `redis://` to a remote host)
- `planner_failed` — `planner.build_initial_plan` / `planner.fix_step` / `planner.replan` raised
- `loop_crash` — outer safety net catch in `run_agent_task` (programming error)
- `unexpected` — defensive `except Exception` in `_run_one_step` after a non-`ToolError` from dispatch
- `stream_unavailable` — Redis xread failure inside the SSE stream generator
- `task_not_found` — log-only event name (the wire `detail` is `"task not found"`)

Pre-existing stable codes used elsewhere in the agent control plane (mentioned in audit context):
- `forbidden` — 403 wire detail `"not your task"`
- `quota_exceeded` — 402 wire detail (insufficient credits)

## CC-20 — `mariana/agent/api_routes.py`

| File:line (HEAD before) | Before | After |
|---|---|---|
| `mariana/agent/api_routes.py:443` | `raise HTTPException(status_code=422, detail=f"vault_env: {exc}")` | `logger.info("agent_vault_env_invalid", user_id=current_user["user_id"], error=str(exc))` then `raise HTTPException(status_code=422, detail="vault_env invalid")` |
| `mariana/agent/api_routes.py:336` | `raise HTTPException(404, f"agent task {task_id} not found")` (in `list_pending_approvals`) | `logger.info("agent_task_not_found", task_id=task_id)` then `raise HTTPException(404, "task not found")` |
| `mariana/agent/api_routes.py:399` | same (in `decide_approval`) | same scrub |
| `mariana/agent/api_routes.py:648` | same (in `get_agent_task`) | same scrub |
| `mariana/agent/api_routes.py:664` | same (in `get_agent_events`) | same scrub |
| `mariana/agent/api_routes.py:716` | same (in `mint_agent_stream_token`) | same scrub |
| `mariana/agent/api_routes.py:752` | same (in `stream_agent_events`) | same scrub |
| `mariana/agent/api_routes.py:872` | same (in `stop_agent_task`) | same scrub |
| `mariana/agent/api_routes.py:890` | same (in `stop_agent_task` after `FOR UPDATE`) | same scrub |
| `mariana/agent/api_routes.py:984` | same (in `list_task_artifacts`) | same scrub |
| `mariana/agent/api_routes.py:804` | `yield _sse_msg("error", {"error": str(exc)})` after Redis xread failure | `yield _sse_msg("error", {"error_code": "stream_unavailable"})` (the `logger.warning("agent_sse_xread_error", task_id=task_id, error=str(exc))` already present is preserved; raw exception text now stays only in that log) |
| `mariana/agent/api_routes.py:618-628` | `await conn.execute("UPDATE agent_tasks SET state='failed', error=$2 ...", task_id, ("vault transport policy violation: " if is_policy else "vault unavailable: ") + str(exc))` AND `raise HTTPException(503, detail=("Vault transport policy violation; refusing to store secrets" if is_policy else "Vault storage unavailable; cannot honour requested secrets")) from exc` | Persists `_vault_error_code = "vault_transport_violation"` or `"vault_unavailable"` into `agent_tasks.error`; 503 detail is now `"vault transport policy violation"` or `"vault unavailable"`; the `logger.error(...)` already on line 586-590 preserves the raw `str(exc)` server-side. |

Total error sites scrubbed in CC-20: **12** (1 vault_env validator + 9 task-not-found + 1 SSE error frame + 1 vault fail-closed dual-action site).

## CC-21 — `mariana/agent/loop.py`

| File:line (HEAD before) | Before | After |
|---|---|---|
| `mariana/agent/loop.py:956` | `step.error = f"unexpected: {type(exc).__name__}: {exc}"` | `logger.warning("agent_step_unexpected_exception", task_id=task.id, step_id=step.id, error_type=type(exc).__name__, error=str(exc))` then `step.error = "unexpected"` |
| `mariana/agent/loop.py:1043` | `payload={"phase": "fix", "error": str(exc)}` (in `_attempt_fix`) | `logger.warning("agent_fix_step_failed", task_id=task.id, failed_step_id=failed_step.id, error=str(exc))` then `payload={"phase": "fix", "error": "planner_failed"}` |
| `mariana/agent/loop.py:1073` | `payload={"phase": "replan", "error": str(exc)}` (in `_attempt_replan`) | `logger.warning("agent_replan_failed", task_id=task.id, reason=reason, error=str(exc))` then `payload={"phase": "replan", "error": "planner_failed"}` |
| `mariana/agent/loop.py:1189` | `task.error = f"Vault unavailable: {exc}"` (`VaultUnavailableError` branch) | `task.error = "vault_unavailable"` (the existing `log.error("vault_env_unavailable_fail_closed", ...)` carries the raw `error=str(exc)`) |
| `mariana/agent/loop.py:1200` | `task.error = f"Vault transport policy violation: {exc}"` (`ValueError` branch) | `task.error = "vault_transport_violation"` (the existing `log.error("vault_env_redis_url_policy_violation", ...)` carries the raw `error=str(exc)`) |
| `mariana/agent/loop.py:1211` | `task.error = f"Vault unavailable: {exc}"` (defensive `except Exception` with `requires_vault`) | `task.error = "vault_unavailable"` (the existing `log.error("vault_env_unexpected_error_fail_closed", ...)` carries the raw `error=str(exc)`) |
| `mariana/agent/loop.py:1290-1291` | `task.error = f"planner_failed: {exc}"` then `_emit("error", payload={"phase": "plan", "error": task.error})` | New `log.error("agent_planner_failed", task_id=task.id, error_type=type(exc).__name__, error=str(exc))` then `task.error = "planner_failed"` then the same `_emit("error", payload={"phase": "plan", "error": task.error})` (which now carries only the stable code) |
| `mariana/agent/loop.py:1412-1417` | `log.exception("agent_loop_crash")` then `task.error = f"loop_crash: {type(exc).__name__}: {exc}"` then `_emit("error", payload={"phase": "loop", "error": task.error})` | `log.exception("agent_loop_crash", task_id=task.id, error_type=type(exc).__name__, error=str(exc))` then `task.error = "loop_crash"` then the same `_emit` (which now carries only the stable code) |

Total error sites scrubbed in CC-21: **8**.

**Combined total error sites scrubbed across CC-20 + CC-21: 20.**

## Tests updated

Only one test file referenced the old prefixed/embedded exception strings:

`tests/test_cc01_agent_loop_behavioural.py`:
- Line ~148: `assert result.error is not None and result.error.startswith("planner_failed:")` → `assert result.error == "planner_failed"`
- Line ~285: `assert err is not None and "unexpected:" in err` → `assert err == "unexpected"`
- Line ~520: `assert result.error is not None and "Vault unavailable" in result.error` → `assert result.error == "vault_unavailable"`

3 assertions updated in 1 test file.

Other tests grepped for the old surfaces and confirmed no further updates required:
- `tests/test_vault_no_leak_live.py:160` only asserts `"vault_env" in r.text.lower()` — the new `detail="vault_env invalid"` still contains the `vault_env` substring; this test is also gated behind `DEFT_LIVE=1` so it does not run in the unit suite.
- `tests/test_u03_vault_redis_safety.py` does not assert on `task.error` content.
- No test asserts on `agent task {id} not found` literal text.
- No test asserts on the SSE `error` payload's `error` field content.

## Verification

```
PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb python -m pytest -q
513 passed, 11 skipped, 11 warnings in 7.34s
```

Pre-fix baseline (before CC-20 + CC-21 commits, but after CC-16/CC-17/CC-18/CC-19/CC-22/CC-23 had already landed): 511 passed / 11 skipped / 0 failed.
Post-fix (CC-20 + CC-21 applied, with the 3 updated assertions in test_cc01): 513 passed / 11 skipped / 0 failed (delta +2 from CC-17's added regression test landing in parallel; CC-20/CC-21 themselves add no new tests).

## Constraints honored

- 0 bug tolerance: no test failed at any point in the CC-20 / CC-21 commits.
- No `--force` push.
- Did not touch CC-16, CC-17, CC-18, CC-19, CC-22, CC-23 work — those were already committed by parallel agents on the same branch and were left intact.
- CC-20 and CC-21 are two separate logical commits as required.
