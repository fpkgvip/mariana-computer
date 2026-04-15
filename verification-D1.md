# Verification D1: Full Codebase Audit (Final)
Date: 2026-04-15
Auditor: Claude Sonnet 4.5

---

## Files Reviewed

### Backend
- `mariana/api.py` (3,197 lines)
- `mariana/main.py`
- `mariana/orchestrator/event_loop.py`
- `mariana/data/db.py`
- `mariana/data/models.py`
- `mariana/ai/session.py`
- `mariana/config.py`
- `mariana/orchestrator/cost_tracker.py`
- `mariana/orchestrator/state_machine.py`
- `mariana/orchestrator/branch_manager.py`
- `mariana/orchestrator/checkpoint.py`
- `mariana/orchestrator/diminishing_returns.py`
- `mariana/ai/router.py`
- `mariana/ai/prompt_builder.py`
- `mariana/ai/output_parser.py`
- `mariana/data/cache.py`
- `mariana/timer.py`
- `mariana/tools/memory.py`, `skills.py`, `finance.py`, `doc_gen.py`, `image_gen.py`, `video_gen.py`, `perplexity_search.py`
- `mariana/report/generator.py`, `renderer.py`
- `mariana/skills/finance_skills.py`, `general_skills.py`, `registry.py`, `skill_selector.py`
- `mariana/tribunal/adversarial.py`, `skeptic.py`
- `mariana/connectors/base.py`, `fred_connector.py`, `polygon_connector.py`, `sec_edgar_connector.py`, `unusual_whales_connector.py`
- `mariana/orchestrator/sub_agents.py`
- `mariana/browser/pool_server.py`

### Frontend
- `frontend/src/pages/Chat.tsx` (2,038 lines)
- `frontend/src/contexts/AuthContext.tsx`
- `frontend/src/pages/Login.tsx`
- `frontend/src/pages/Signup.tsx`
- `frontend/src/pages/Admin.tsx`
- `frontend/src/pages/Account.tsx`
- `frontend/src/pages/Checkout.tsx`
- `frontend/src/pages/Pricing.tsx`
- `frontend/src/components/FileUpload.tsx`
- `frontend/src/components/FileViewer.tsx`
- `frontend/src/components/ProgressTimeline.tsx`
- `frontend/src/App.tsx`
- `frontend/src/lib/supabase.ts`

### Infrastructure
- `Dockerfile`
- `docker-compose.yml`
- `requirements.txt`

---

## Bugs Found

### BUG-D1-01 — kill_investigation API does not stop a running orchestrator

**Severity:** High  
**File:** `mariana/api.py` (kill_investigation endpoint) + `mariana/orchestrator/event_loop.py`

**Description:**  
`POST /api/investigations/{task_id}/kill` sets `status='HALTED'` in the DB and publishes `kill:{task_id}` to Redis, with the comment: _"so the orchestrator daemon can detect the signal on its next loop iteration."_ Neither mechanism works:

1. **Redis publish is never consumed.** The event loop subscribes only to `logs:{task_id}` (via `_emit_progress`). There is no subscriber for the `kill:{task_id}` channel anywhere in `event_loop.py` or `main.py`.

2. **DB HALTED status is overwritten on the next iteration.** `_persist_task()` is called unconditionally after every state transition and performs `SET status = $1` where `$1` is `task.status.value`. Since the in-memory `task.status` is still `TaskStatus.RUNNING`, every call to `_persist_task()` overwrites the API-set `HALTED` back to `RUNNING`. There is no guard like `WHERE status != 'HALTED'`.

**Impact:** A user-initiated kill has no effect on a running investigation. The investigation continues until natural completion, budget exhaustion, or an OS-level SIGTERM. The kill API returns 200 with `"Kill signal sent"` — a false success indication.

**Fix:**  
Option A (recommended): In the event loop's main loop, after the `shutdown_flag` check, add a periodic DB re-read of `task.status`:
```python
# Check every N iterations for an external kill
if iteration % 5 == 0:
    db_status = await db.fetchval(
        "SELECT status FROM research_tasks WHERE id = $1", task.id
    )
    if db_status == "HALTED":
        task.status = TaskStatus.HALTED
        task.current_state = State.HALT
        break
```

Option B: Subscribe to `kill:{task_id}` in the event loop and set the shutdown flag when a message arrives.

Also, `_persist_task()` should not overwrite an externally-set HALTED status:
```python
# In _persist_task: use conditional UPDATE
UPDATE research_tasks SET ...
WHERE id = $12 AND status != 'HALTED'
```

---

### BUG-D1-02 — Chat.tsx terminal state detection via status_change events uses wrong string

**Severity:** Low (functionally mitigated by the "done" event fallback, but still incorrect code)  
**File:** `frontend/src/pages/Chat.tsx` (line 803)

**Description:**  
The `handleLogEvent` handler checks for terminal state:
```typescript
if (eventType === "status_change" && ["HALTED", "COMPLETED"].includes(parsed.state as string))
```

However, `event_loop.py` emits:
```python
{"type": "status_change", "state": next_state.value, ...}
```
where `next_state.value` is `State.HALT.value = "HALT"` (from `data/models.py` line 168: `HALT = "HALT"`).

`TaskStatus.HALTED.value = "HALTED"` and `State.HALT.value = "HALT"` are **different strings**.

The check `["HALTED", "COMPLETED"].includes("HALT")` is **always false** — the status_change terminal detection is dead code and never fires. The BUG-C3-01 comment claims "Backend emits 'HALTED', 'COMPLETED', or 'FAILED' — not 'HALT'" but this is incorrect: the emitted `state` field IS "HALT".

**Impact (Mitigated):** The investigation **does** eventually get detected as terminal because the SSE stream's DB poll path (polling every ~0.1s when Redis is idle) emits a separate `"done"` SSE event with `final_status` from the DB (which uses `TaskStatus` values like "HALTED", "COMPLETED", "FAILED"). The `"done"` event handler in Chat.tsx correctly handles all these. Practical latency is 0–1 second.

**Fix:** Change the status_change check to use the correct state-machine string:
```typescript
if (eventType === "status_change" && ["HALT", "COMPLETED"].includes(parsed.state as string))
```

---

## Previous Fix Verification

- **BUG-C1-01** (BudgetExhaustedError class mismatch): **Confirmed working.** `session.py` imports `BudgetExhaustedError` from `mariana.orchestrator.cost_tracker` at line 71. The `_check_budget()` function raises it with `(scope=..., spent=..., cap=...)` matching the constructor signature `def __init__(self, scope: str, spent: float, cap: float)`. No circular import: `session.py → cost_tracker.py` (cost_tracker does not import session). ✅

- **BUG-C1-02** (`_persist_task` missing metadata): **Confirmed working.** `event_loop.py` has `import json` at line 29. `_persist_task()` includes `metadata = $11` in the UPDATE and passes `json.dumps(task.metadata)` as the 11th parameter. ✅

- **VULN-C2-03** (Stripe checkout URL validation): **Confirmed working.** `_ALLOWED_REDIRECT_HOSTS = {"frontend-tau-navy-80.vercel.app", "localhost", "127.0.0.1"}`. Both `Checkout.tsx` and `Pricing.tsx` send `window.location.origin` as the base, which resolves to those hostnames in production and development. The validation does not break normal checkout flow. ✅

- **BUG-C3-06** (`startInvestigationRef` stale closure): **Confirmed working.** `startInvestigation` is defined as a `useCallback` at line 1147. `startInvestigationRef` is declared at line 1299 **after** `startInvestigation`, and `startInvestigationRef.current = startInvestigation` is assigned at line 1300 (outside any callback). `handleSend` calls `startInvestigationRef.current(...)` at lines 1113, 1122, 1136. The ref is always up-to-date because it's re-assigned on every render after `startInvestigation` is (re)created. ✅

---

## Confidence Statement

The codebase has been thoroughly audited across all 40+ files listed above. Two bugs were found:

1. **BUG-D1-01** is a genuine production bug with high impact: the kill investigation API silently fails to stop running investigations. It is not a theoretical concern — any user who clicks "Stop" on a running investigation will see the stop appear to succeed (HTTP 200) while the investigation continues running and consuming credits.

2. **BUG-D1-02** is a low-impact correctness issue (dead code that never fires) mitigated by the "done" SSE event fallback. The frontend will still detect terminal states correctly, just via a different code path than intended.

All C-round fixes verified correct. No new security vulnerabilities found. No runtime crashes, SQL injection vectors, auth bypasses, or data integrity issues found beyond those listed above.

**Production-readiness verdict: NOT READY** — BUG-D1-01 must be fixed before declaring production-ready. The kill investigation feature is prominently exposed in the UI and currently non-functional.
