# Verification E3-R1: Logic Audit

**Date:** 2026-04-15  
**Auditor:** Claude  
**Round:** E3-R1 (verification round 1 of 3)  
**Scope:** Logic errors, race conditions, state machine bugs, data validation gaps, error handling issues, resource leaks, concurrency problems, missing edge cases.

---

## Files Reviewed

| # | File | Lines |
|---|------|-------|
| 1 | `mariana/api.py` | 3,225 |
| 2 | `mariana/orchestrator/event_loop.py` | 2,002 |
| 3 | `mariana/report/generator.py` | 345 |
| 4 | `mariana/tools/skills.py` | 291 |
| 5 | `mariana/config.py` | 376 |
| 6 | `frontend/src/pages/Chat.tsx` | 2,046 |

---

## Result: FAIL — 1 new bug found

---

## Bugs Found

### BUG-E3-L-01: Render-failure error handler in `generator.py` overwrites HALTED with FAILED (missing guard)

- **File:** `mariana/report/generator.py`
- **Line:** 269
- **Severity:** Low
- **Category:** Race condition / status overwrite

**Description:**

The `_persist_report_path` success path (line 321–324) correctly includes the `AND status != 'HALTED'` guard added as a fix for BUG-E1-01. However, the render-failure error handler at line 269 uses a raw SQL `UPDATE` that unconditionally sets `status = 'FAILED'` without the same guard:

```python
# Line 262–271 (error handler for render failure)
except Exception as exc:
    log.error("report_render_failed", pdf_path=pdf_path, error=str(exc))
    try:
        async with db.acquire() as _conn:
            await _conn.execute(
                "UPDATE research_tasks SET status = 'FAILED' WHERE id = $1",
                task.id,
            )
    except Exception as db_exc:
        ...
    raise
```

Compare with the guarded success path:

```sql
-- Lines 318-325 (_persist_report_path — correctly guarded)
UPDATE research_tasks
   SET output_pdf_path = $1,
       status          = 'COMPLETED',
       completed_at    = NOW()
 WHERE id = $2
   AND status != 'HALTED'
```

**Trigger:**

1. An investigation reaches the REPORT state and begins PDF rendering.
2. The user sends a kill signal — the API sets `status = 'HALTED'` in the DB.
3. The PDF render fails (e.g., template error, disk full, out of memory).
4. The error handler at line 269 writes `status = 'FAILED'` to the DB, overwriting the externally-set `HALTED`.
5. The exception re-raises and propagates to the event loop's catch-all handler, which calls `_persist_task` — but by this point the DB status is already `FAILED`, so the HALTED guard in `_persist_task` (which only activates when `task.status == TaskStatus.RUNNING`) does not help.

**Impact:**

A user's kill action during a failing report render is silently overridden. The investigation shows as `FAILED` instead of `HALTED`. This is the same class of bug as BUG-E1-01 (the success path overwrite) but on the error path. The window is narrow (requires both a kill signal and a render failure during the same brief interval), so severity is Low.

**Fix:**

Add the HALTED guard to the error handler:

```python
await _conn.execute(
    "UPDATE research_tasks SET status = 'FAILED' WHERE id = $1 AND status != 'HALTED'",
    task.id,
)
```

---

## Previously Fixed Bugs — Verification Status

All 90+ previously fixed items were confirmed in place across all six files. Specific spot-checks:

| Fix | Status |
|-----|--------|
| `_persist_task` HALTED guard (event_loop.py line 1870) | ✅ Verified |
| `_persist_report_path` HALTED guard (generator.py line 324) | ✅ Verified |
| `DIMINISHING_SCORE_DELTA_THRESHOLD = 0.1` dataclass default (config.py line 83) | ✅ Verified |
| `DIMINISHING_SCORE_DELTA_THRESHOLD = 0.1` in `load_config()` (config.py line 325) | ✅ Verified |
| Chat.tsx SSE `done` handler preserves HALTED status (line 906: `finalStatus as InvestigationStatus`) | ✅ Verified |
| Chat.tsx polling handler preserves HALTED status (line 739: `data.status as InvestigationStatus`) | ✅ Verified |
| `renderMarkdown` XSS fix — HTML-escapes content first, function replacement for links with quote escaping (lines 195–230) | ✅ Verified |
| Skills namespaced per `owner_id` with `_owner_skills_dir` (skills.py line 244) | ✅ Verified |
| Path traversal protection via `_safe_skill_path` + `_sanitize_skill_id` (skills.py lines 27–38) | ✅ Verified |
| Kill check every 5 iterations (event_loop.py line 292) | ✅ Verified |
| BudgetExhaustedError imported from cost_tracker (event_loop.py line 71) | ✅ Verified |
| Config `__post_init__` budget validation (config.py line 131) | ✅ Verified |
| SSE auth re-validation every 30s (api.py line 1235) | ✅ Verified |
| Cleanup on unmount clears intervals and closes EventSource (Chat.tsx lines 492–497) | ✅ Verified |
| Delete skill ownership check (api.py lines 3140–3143) | ✅ Verified |
| InvalidTransitionError treated as HALT (event_loop.py line 379) | ✅ Verified |

No regressions detected.

---

## Areas Examined Without Findings

- **State machine transitions:** `InvalidTransitionError` caught and treated as HALT. All state transitions go through the `transition()` function with proper error handling.
- **Concurrency:** Credit reservation uses atomic RPC. `_persist_task` HALTED guard prevents concurrent status overwrites on the main code path. SSE re-validates auth.
- **Resource leaks:** Chat.tsx cleanup effect (line 492) properly clears intervals and closes EventSource on unmount. `stopConnectionsOnly` nulls out refs after cleanup.
- **Data validation:** Config `_int`/`_float`/`_bool` parsers fall back to defaults on `ValueError`. `_sanitize_skill_id` strips non-alphanumeric characters. Upload paths are resolved and checked.
- **Error handling:** Emergency checkpoint wrapped in try/except. Generator render failure properly re-raises after attempting DB status update. BudgetExhaustedError and generic Exception both handled in event loop.
- **Skills cross-user isolation:** `detect_skill` loads all users' custom skills (no owner filter), but it only uses the skill for keyword matching and stores the skill ID in metadata. The `active_skill` ID stored in metadata is never subsequently read by any downstream code in the event loop, so this is dead metadata rather than a data leak.

---

## Summary

One new bug found. **BUG-E3-L-01** is a missing HALTED guard on the render-failure error path in `generator.py`. The success path was correctly guarded in a prior round, but the error path was missed. Severity is Low due to the narrow trigger window (simultaneous kill signal + render failure).

**Verdict: NOT ZERO BUGS.** One bug found. Fix required before zero-bug certification.
