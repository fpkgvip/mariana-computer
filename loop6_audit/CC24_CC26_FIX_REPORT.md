# CC-24 / CC-25 / CC-26 Fix Report

**Branch:** `loop6/zero-bug`
**HEAD after fix:** `ac6314a`
**Audit input:** `loop6_audit/A48_post_cc23_reaudit.md`
**Date:** 2026-04-28

Three follow-up fixes from the post-CC23 re-audit (A48):

* **CC-24** — residual `task_id` disclosure in 10 more 404 detail strings in
  `mariana/api.py` (Finding 1 of A48; CC-22 was incomplete).
* **CC-25** — `ToolError` raw message + structured detail still persisted into
  the user-visible step record and SSE payload (Finding 2 of A48; CC-21 was
  incomplete on the tool-error path).
* **CC-26** — `npm install` was not idempotent: both `frontend/package-lock.json`
  and `e2e/package-lock.json` were rewritten on a fresh install (Finding 3 of
  A48; CC-23 was incomplete).

---

## CC-24 — main API 404 detail leakage scrub

### Problem
`mariana/api.py` still contained 10 user-facing 404 sites that interpolated
the raw `task_id` UUID into the response detail:

```py
raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
```

at lines **1551, 3366, 3412, 3438, 3480, 4385, 4466, 4560, 4621, 4800** — exactly
the class CC-22 was meant to remove. The original CC-22 fix had only touched
the four canonical sites called out in A47.

A repo-wide re-grep also surfaced **four additional sibling leaks** of the same
identifier-disclosure class that were not in the A48 list but were in scope
per the task spec ("Re-grep `mariana/api.py` for any remaining `Task {.*} not
found` or similar f-string interpolation. Find ALL. Don't miss any."):

| Line | Old detail | Identifier leaked |
|------|------------|-------------------|
| 4648 | `f"File {filename!r} not found"` | filename |
| 5573 | `f"Plan {body.plan_id!r} not found"` | plan_id |
| 8929 | `f"Skill {skill_id!r} not found"` | skill_id |
| 9211 | `f"No outcome found for task {task_id}"` | task_id |

Total 14 sites scrubbed.

### Fix
Every site now follows the canonical CC-22 pattern: a structured
`logger.info(...)` with the original identifier in `extra=`-style structlog
kwargs immediately before the raise, and a generic stable detail on the
HTTPException itself.

```py
# 10 task_id sites (1551, 3366, 3412, 3438, 3480, 4385, 4466, 4560, 4621, 4800)
logger.info("task_not_found", task_id=task_id)
raise HTTPException(status_code=404, detail="task not found")

# 4 sibling sites
logger.info("file_not_found", task_id=task_id, filename=filename)
raise HTTPException(status_code=404, detail="not found")
# ... and similar for plan_not_found, skill_not_found, outcome_not_found
```

The kwargs match the established structlog style at the canonical CC-22 sites
(`logger.info("task_not_found", task_id=task_id)` at lines 1324, 1494, 8999),
not `extra=` — `mariana/api.py` uses `structlog.get_logger(__name__)` which
takes positional kwargs.

### Re-grep verification
* `grep -n 'HTTPException(status_code=404, detail=f"' mariana/api.py` → **0 matches**
* `grep -n 'Task {task_id' mariana/api.py` → **0 matches**
* `grep -n 'detail=f' sandbox_server/ browser_server/` → **0 matches**
* Repo-wide `f".*not found.*\{` returns 6 hits, all internal exception messages
  (dispatcher.py ToolError — handled by CC-25; checkpoint / renderer / vault /
  test infrastructure — none user-facing HTTP details).

### Tests
Pre-existing tests/ did not assert on the old 404 detail strings:
* `grep -rn "Task .* not found" tests/` → 0 matches
* `grep -rn "task_id!r" tests/` → 0 matches

No test changes required for CC-24.

### Commit
`f2b98bd CC-24 scrub residual task_id leaks from 10 more 404 detail strings in mariana/api.py`

---

## CC-25 — ToolError raw message/detail scrub from agent step state and SSE

### Problem
`mariana/agent/loop.py:942-950` still persisted raw exception text into the
user-visible step record and SSE payload:

```py
except ToolError as exc:
    step.status = StepStatus.FAILED
    step.finished_at = time.time()
    step.error = str(exc)                        # raw message
    if exc.detail:
        step.result = {"error_detail": exc.detail}  # raw structured detail
    task.total_failures += 1
    await _persist_task(db, task)
    await _emit(
        db, redis, task, "step_failed",
        step_id=step.id,
        payload={"error": step.error, "detail": exc.detail},  # raw on SSE too
    )
    return False, step.error
```

`mariana/agent/dispatcher.py` constructs `ToolError` messages packed with raw
internals — workspace paths, file listings, upstream response bodies, etc.:

* `ToolError("", f"source_dir {src_dir!r} is empty or missing")`
* `ToolError("", f"entry {entry!r} not found in {src_dir!r}. Available: {src_relative[:10]}...")`
* `ToolError("", f"failed to read {sb_path}: {exc}")`
* `ToolError(tool, f"{tool} failed: {exc}", detail={"status": ..., "body": ...})`

Practical impact: a failed tool step exposed raw filesystem paths, filenames,
upstream HTTP response bodies, and traceback fragments via persisted
`step.error` / `step.result` and the live SSE stream — same disclosure class
CC-21 was meant to close for the unexpected-exception path.

### Fix
Mirror the CC-21 unexpected-exception path. The new `ToolError` handler in
`mariana/agent/loop.py`:

```py
except ToolError as exc:
    # CC-25: persist a stable error_code on the user-visible step record;
    # raw exception message + structured detail stay in the server log so
    # operators can still diagnose without exposing internals such as
    # workspace paths, file listings, or remote response bodies.
    logger.warning(
        "tool_error",
        task_id=task.id,
        step_id=step.id,
        tool=step.tool,
        raw_message=str(exc),
        raw_detail=exc.detail,
    )
    step.status = StepStatus.FAILED
    step.finished_at = time.time()
    step.error = "tool_error"                                 # stable code
    step.result = {"error_code": "tool_error", "tool": step.tool}
    task.total_failures += 1
    await _persist_task(db, task)
    await _emit(
        db, redis, task, "step_failed",
        step_id=step.id,
        payload={"error": step.error, "tool": step.tool},     # no raw detail
    )
    return False, step.error
```

Server-side observability is preserved verbatim via the new structured
`logger.warning("tool_error", ..., raw_message=..., raw_detail=...)`.

A new canonical user-visible error_code list was added to the
`mariana/agent/loop.py` module docstring so future contributors know not to
re-introduce raw exception strings:

```text
Canonical error codes (CC-20/CC-21/CC-25)
-----------------------------------------
* tool_error               — a tool dispatch raised ToolError.
* unexpected               — a tool dispatch raised a non-ToolError exception.
* planner_failed           — fix-step or replan planner call failed.
* vault_unavailable        — per-task secret bootstrap failed.
* vault_transport_violation — vault access violated transport policy.
* stream_unavailable       — SSE stream couldn't be established.
```

### Dispatcher cleanup
`mariana/agent/dispatcher.py`:

* `ToolError` class docstring now explicitly states the `message` and `detail`
  attributes are **server-log-only** as of CC-25 — future contributors are free
  to pack diagnostic context (workspace paths, file listings, upstream
  response bodies); none of that will leak to the API surface.
* The `(SandboxError, BrowserError)` wrapper at `dispatch()` line 100 has its
  comment updated from the misleading "Surface a structured error from the
  remote service" to a CC-25 note that the message + detail are server-log-only.
* No `ToolError(...)` construction was changed — the messages keep their
  diagnostic richness, they just no longer escape to the user surface.

### Tests
New regression test `tests/test_cc25_tool_error_scrub.py` (3 tests):

1. `test_cc25_tool_error_persists_only_stable_code_on_step` — constructs a
   `ToolError` with a path-rich message (`source_dir '/workspace/user-abc/secret-task/'
   is empty or missing`) and a sensitive detail dict (`{"status": 500,
   "body": "<html>internal traceback /var/lib/mariana/...</html>"}`), drives
   `_run_one_step`, asserts:
   * `step.error == "tool_error"` (stable code only)
   * `step.result == {"error_code": "tool_error", "tool": "code_exec"}`
     (no `error_detail` key)
   * Neither raw message nor `/workspace/` substring nor "internal traceback"
     appears anywhere in the persisted step record.

2. `test_cc25_tool_error_emits_only_stable_code_on_sse` — patches `_emit` to
   capture payloads, drives `_run_one_step` with another path-rich ToolError
   (`entry 'evil.html' not found in '/workspace/x/y/'. Available:
   ['secret.txt', 'private.key']`, detail `{"body": "/var/lib/mariana/runtime/leak.txt"}`),
   asserts the emitted `step_failed` payload:
   * Has `error == "tool_error"` and `tool == "code_exec"`.
   * Does NOT have a `detail` key.
   * Contains none of `/workspace/`, `secret.txt`, `private.key`, `leak.txt`,
     or `Available:` substring.

3. `test_cc25_tool_error_listed_as_canonical_code_in_loop_docstring` — pins
   `tool_error` (and the prior CC-21 codes `unexpected`, `planner_failed`)
   in the loop module docstring so future contributors don't drop the
   canonical list.

Pre-existing tests/ did not assert on the old `error_detail` / raw `str(exc)`
behaviour:
* `grep -rn "tool_error\|ToolError\|error_detail" tests/` → only 2 unrelated
  comment hits in `test_cc01_agent_loop_behavioural.py`.
* `grep -rn "step_failed" tests/` → only docstring references.

### Commit
`5a472fb CC-25 scrub ToolError raw message/detail from agent step state and SSE events`

---

## CC-26 — lockfile regeneration & idempotence

### Problem
After CC-23 exact-pinned `frontend/package.json` (74 deps) and
`e2e/package.json` (1 dep) without re-committing the regenerated
`package-lock.json` files, a fresh `npm install --no-audit --no-fund`
rewrote both lockfiles:

* `frontend/package-lock.json` — root dependency spec entries rewritten from
  caret ranges to exact pins (`react-router-dom`, `@testing-library/*`,
  `vite`, `jsdom`).
* `e2e/package-lock.json` — root `playwright` spec rewritten from
  `^1.59.1` to `1.59.1`.

Practical impact: fresh installs normalised lock metadata, which is
supply-chain noise and a CI drift footgun even though resolved package
versions stayed materially the same.

### Fix
Ran `npm install --no-audit --no-fund` once in each directory and committed
the resulting lockfile diffs:

* `frontend/package-lock.json`: 38 lines changed.
* `e2e/package-lock.json`: 37 lines changed.

No `package.json` or transitive-dep churn was needed — this was purely a
`packages[""].dependencies` / `packages[""].devDependencies` block sync.

### Idempotence verification (post-commit)
Re-ran `npm install --no-audit --no-fund` twice in each directory after the
commit. SHA256 hashes of both lockfiles before and after the second install
were identical:

```text
frontend/package-lock.json: 741bd767be83508d7b0759e0b16b9589fa69a96ce500224d9e857f90913cbf77
e2e/package-lock.json:      1533fd788a24ef44efb1cbf5930b197228b6c0f8f71885c7a863ef9ee2a77701
```

`git status --porcelain frontend/package-lock.json e2e/package-lock.json` →
**empty** after both follow-up installs. **Idempotence: yes.**

### Commit
`ac6314a CC-26 commit regenerated lockfiles after CC-23 exact-pinning (idempotent install)`

---

## Verification (full)

### pytest
```
PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb \
  python -m pytest -q
=> 516 passed, 11 skipped, 0 failed
```

The CC-25 fix added 3 new regression tests (`tests/test_cc25_tool_error_scrub.py`),
taking the suite from 513 → 516 passed. 11 skipped is unchanged (Postgres-gated).

### vitest
```
cd frontend && npm test -- --run
=> Test Files 15 passed (15)
   Tests 144 passed (144)
```

### lint
```
cd frontend && npm run lint
=> ✖ 27 problems (0 errors, 27 warnings)
```

0 errors. The 27 warnings are pre-existing (unused eslint-disable directives
and one missing `useEffect` dep in `InvestigationGraph.tsx`). No CC-24/25/26
change introduced a lint warning.

### Lockfile idempotence (final)
```
cd frontend && npm install --no-audit --no-fund   # up to date
cd e2e && npm install --no-audit --no-fund        # up to date
git status --porcelain frontend/package-lock.json e2e/package-lock.json
=> (empty)
```

---

## Summary

| Finding | Sites scrubbed | Tests added/updated | Commit |
|---------|----------------|---------------------|--------|
| CC-24   | 14 (10 task_id + 4 sibling: file/plan/skill/outcome) | 0 added, 0 updated (no pre-existing assertions) | `f2b98bd` |
| CC-25   | 1 ToolError handler in loop.py + 2 dispatcher comment/docstring sites | 3 added (`tests/test_cc25_tool_error_scrub.py`) | `5a472fb` |
| CC-26   | 2 lockfiles (`frontend/`, `e2e/`) | 0 (idempotence verified by SHA256) | `ac6314a` |

**HEAD:** `ac6314a` (on branch `loop6/zero-bug`).
**Final test counts:** pytest 516 passed / 11 skipped / 0 failed; vitest 144 / 144.
**Idempotence confirmed:** yes (SHA256 stable across two follow-up installs;
`git status --porcelain` empty).
