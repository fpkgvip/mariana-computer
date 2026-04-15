# Verification E1: Zero-Bug Final Audit (Round E1)

**Date:** 2026-04-15  
**Auditor:** Claude Opus 4.5  
**Scope:** Complete codebase audit — all 40+ backend, frontend, and infrastructure files read in full.

---

## Files Reviewed

### Backend (Python)
- `mariana/api.py` (3,197 lines)
- `mariana/main.py`
- `mariana/orchestrator/event_loop.py` (2,001 lines)
- `mariana/orchestrator/cost_tracker.py`
- `mariana/orchestrator/state_machine.py`
- `mariana/orchestrator/branch_manager.py`
- `mariana/orchestrator/checkpoint.py`
- `mariana/orchestrator/diminishing_returns.py`
- `mariana/orchestrator/sub_agents.py`
- `mariana/data/db.py`
- `mariana/data/models.py`
- `mariana/data/cache.py`
- `mariana/ai/session.py`
- `mariana/ai/router.py`
- `mariana/ai/prompt_builder.py`
- `mariana/ai/output_parser.py`
- `mariana/config.py`
- `mariana/timer.py`
- `mariana/tools/perplexity_search.py`
- `mariana/tools/finance.py`
- `mariana/tools/memory.py`
- `mariana/tools/skills.py`
- `mariana/tools/doc_gen.py`
- `mariana/tools/image_gen.py`
- `mariana/tools/video_gen.py`
- `mariana/report/generator.py`
- `mariana/report/renderer.py`
- `mariana/skills/registry.py`
- `mariana/skills/skill_selector.py`
- `mariana/skills/finance_skills.py`
- `mariana/skills/general_skills.py`
- `mariana/tribunal/adversarial.py`
- `mariana/tribunal/skeptic.py`
- `mariana/connectors/base.py`
- `mariana/connectors/fred_connector.py`
- `mariana/connectors/polygon_connector.py`
- `mariana/connectors/sec_edgar_connector.py`
- `mariana/connectors/unusual_whales_connector.py`
- `mariana/browser/pool_server.py`

### Frontend (TypeScript/React)
- `frontend/src/pages/Chat.tsx` (2,039 lines)
- `frontend/src/contexts/AuthContext.tsx`
- `frontend/src/pages/Login.tsx`
- `frontend/src/pages/Signup.tsx`
- `frontend/src/pages/Admin.tsx`
- `frontend/src/pages/Account.tsx`
- `frontend/src/pages/Checkout.tsx`
- `frontend/src/pages/BuyCredits.tsx`
- `frontend/src/pages/Pricing.tsx`
- `frontend/src/pages/Research.tsx`
- `frontend/src/pages/Index.tsx`
- `frontend/src/pages/Skills.tsx`
- `frontend/src/pages/NotFound.tsx`
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

## D-Round Fix Verification

### BUG-D1-01: Kill investigation (event_loop.py poll + _persist_task guard)

**Status: CONFIRMED WORKING ✅**

- `event_loop.py` lines 292–304: Kill check runs at `iteration % 5 == 0`. Uses `db.fetchval("SELECT status FROM research_tasks WHERE id = $1", task.id)` and breaks on `"HALTED"`.
- `_persist_task()` (lines 1869–1872): `WHERE id = $12 AND status != 'HALTED'` guard is present when `task.status == TaskStatus.RUNNING`.

### BUG-D1-02: Chat.tsx status_change checks ["HALT", "HALTED", "COMPLETED"]

**Status: CONFIRMED WORKING ✅**

- `Chat.tsx` line 804: `["HALT", "HALTED", "COMPLETED"].includes(parsed.state as string)` — all three values are checked.

---

## Bugs Found

### BUG-E1-01: `_persist_report_path` can override externally-set `HALTED` with `COMPLETED`

**File:** `mariana/report/generator.py`, lines 317–325  
**Severity:** Low-Medium  
**Category:** Race condition / logic error

**Description:**  
`_persist_report_path()` unconditionally sets `status = 'COMPLETED'` in the database without a `WHERE status != 'HALTED'` guard:

```sql
UPDATE research_tasks
   SET output_pdf_path = $1,
       status          = 'COMPLETED',
       completed_at    = NOW()
 WHERE id = $2
```

The D-round BUG-D1-01 fix correctly added a `WHERE status != 'HALTED'` guard to `_persist_task()` in `event_loop.py`, but this protection was not extended to `_persist_report_path()` in `generator.py`.

**Trigger:**
1. A long-running investigation reaches the REPORT state and begins `handle_report()`.
2. The user sends a kill signal — the API sets `status = 'HALTED'` in the DB.
3. The event loop's kill check (which only runs at `iteration % 5 == 0`, i.e., at the top of the outer `while` loop) has already passed; it will not run again because REPORT→HALT is the terminal transition.
4. `generate_report()` completes normally and calls `_persist_report_path()`, which writes `status = 'COMPLETED'` to the DB — overwriting the externally-set `HALTED`.
5. Result: the investigation shows as `COMPLETED` in the DB and UI, even though the user killed it.

**Impact:** A user's kill action during the final report generation phase is silently overridden, showing the investigation as successfully completed instead of halted.

**Fix:** Add the same guard that `_persist_task()` uses:
```sql
UPDATE research_tasks
   SET output_pdf_path = $1,
       status          = 'COMPLETED',
       completed_at    = NOW()
 WHERE id = $2
   AND status != 'HALTED'
```

---

### BUG-E1-02: `config.DIMINISHING_SCORE_DELTA_THRESHOLD = 1.0` effectively disables the score-delta DR check

**File:** `mariana/config.py` line 83; `mariana/orchestrator/diminishing_returns.py` lines 162–170  
**Severity:** Medium  
**Category:** Logic error / behavioral regression

**Description:**  
The BUG-C1-03 fix (round C) made `diminishing_returns.py` read `config.DIMINISHING_SCORE_DELTA_THRESHOLD` instead of the hardcoded module-level constant `_SCORE_DELTA_THRESHOLD = 0.1`. However, the config default is `1.0`:

```python
# config.py line 83
DIMINISHING_SCORE_DELTA_THRESHOLD: float = 1.0
```

`EvaluationOutput.score` is validated on the 0–1 scale (`ge=0.0, le=1.0`; `models.py` line 701). The maximum possible `abs(score_history[-1] - score_history[-2])` is therefore `1.0`. The condition `score_delta < 1.0` is true for virtually every real score transition (a perfect 0→1 or 1→0 flip is the only exception), making the score-delta dimension permanently "stale".

**Before BUG-C1-03 fix (old behavior):**
- Effective threshold: 0.1
- DR flag requires: novelty stale **AND** sources stale **AND** score delta < 10%

**After BUG-C1-03 fix (current behavior):**
- Effective threshold: 1.0
- DR flag requires: novelty stale **AND** sources stale (score delta check is always true, so it no longer filters)

This means investigations where scores are rapidly improving (e.g., from 0.5 to 0.9) can now trigger DR flags purely because novelty and source counts are low — even though the score trend shows genuine research progress. The score_delta check was designed to prevent this exact false-positive scenario.

The comment in `diminishing_returns.py` at line 48 explicitly documents the intent: `"BUG-010: On 0–1 scale, a delta of 0.1 (10% change) is the plateau threshold"` — but the effective threshold after the C1 fix is 1.0, not 0.1.

**Trigger:** Any research cycle where score improvement is real but novelty and new-source counts are low.

**Impact:** DR flags fire more aggressively than intended. Investigations may be prematurely pivoted or halted despite active score improvement. Operators setting `DIMINISHING_SCORE_DELTA_THRESHOLD` in the environment to a reasonable value (e.g., `0.1`) would be surprised that the default behavior ignores score progress.

**Fix:** Change the config default to match the documented and intended threshold:
```python
# config.py line 83
DIMINISHING_SCORE_DELTA_THRESHOLD: float = 0.1
```
And update `load_config()` (line 325) correspondingly:
```python
DIMINISHING_SCORE_DELTA_THRESHOLD=_float("DIMINISHING_SCORE_DELTA_THRESHOLD", 0.1),
```

---

### BUG-E1-03: SSE `done` event maps `HALTED` status to `FAILED` in the UI

**File:** `frontend/src/pages/Chat.tsx`, lines 899–907  
**Severity:** Low  
**Category:** Logic error / incorrect status display

**Description:**  
In the `es.addEventListener("done", ...)` handler, when the SSE stream closes with `final_status === "HALTED"`, the UI calls `updateInvestigationStatus(taskId, "FAILED")` instead of `updateInvestigationStatus(taskId, "HALTED")`:

```typescript
} else if (finalStatus === "FAILED" || finalStatus === "HALTED") {
  updateInvestigationStatus(taskId, "FAILED");   // ← wrong: should be finalStatus
```

`InvestigationStatus` (line 44) includes `"HALTED"` as a valid value. The `STATUS_COLORS` map (lines 112–119) has distinct entries for both `"FAILED"` and `"HALTED"` (though both happen to be red). The investigation sidebar, history list, and any downstream consumers that distinguish `"FAILED"` from `"HALTED"` will show the wrong semantic status.

**Impact:** When a user clicks the Stop button, the investigation is displayed as "FAILED" in the UI rather than "HALTED." This is semantically incorrect and misleading — the user explicitly stopped the investigation, so the correct label is "HALTED," not "FAILED."

**Fix:**
```typescript
} else if (finalStatus === "FAILED" || finalStatus === "HALTED") {
  updateInvestigationStatus(taskId, finalStatus as InvestigationStatus);
```

---

## All Previously Fixed Bugs — Verification Status

| # | Bug ID | Description | Status |
|---|--------|-------------|--------|
| 1 | BUG-V2-01 | JWT auth bypass | ✅ Verified fixed |
| 2 | BUG-S2-12 | Missing auth on endpoints | ✅ Verified fixed |
| 3 | Upload session | UUID validated | ✅ Verified fixed |
| 4 | SSE auth | Re-validates every 30s | ✅ Verified fixed |
| 5 | BUG-C2-05 | Credit race condition → atomic deduction | ✅ Verified fixed |
| 6 | Path traversal | Resolved paths checked | ✅ Verified fixed |
| 7 | BUG-C1-08 | Stripe webhook replay → idempotency table | ✅ Verified fixed |
| 8 | Docker root | Non-root user | ✅ Verified fixed (Dockerfile creates mariana user) |
| 9 | BUG-C1-01 | BudgetExhaustedError mismatch → unified import | ✅ Verified fixed |
| 10 | BUG-C1-02 | _persist_task missing metadata | ✅ Verified fixed |
| 11 | BUG-C1-03 | Diminishing returns ignoring config → reads config threshold | ✅ Reads config (but see BUG-E1-02: config default incorrect) |
| 12 | BUG-C1-06 | Daemon rename race → FileNotFoundError caught | ✅ Verified fixed |
| 13 | BUG-C1-08 | Webhook idempotency DB failure → returns 500 | ✅ Verified fixed |
| 14 | BUG-C1-09 | Non-atomic add_credits → fails loudly | ✅ Verified fixed |
| 15 | BUG-C2-01 | Credit settlement fail-open → minimum tier-based reservation | ✅ Verified fixed |
| 16 | BUG-C2-03 | Stripe checkout open redirect → URL host allowlist | ✅ Verified fixed |
| 17 | BUG-C2-07 | Config/connectors info disclosure → require auth | ✅ Verified fixed |
| 18 | BUG-C3-01 | SSE status_change wrong string | ✅ Verified fixed — checks "HALT"/"HALTED"/"COMPLETED" |
| 19 | BUG-R1-10 | Admin auth guard → 500ms grace period | ✅ Verified fixed |
| 20 | BUG-R2-06 | FileUpload concurrent race → synchronous ref update | ✅ Verified fixed |
| 21 | BUG-C3-05 | switchInvestigation stale deps → uses timelineStepsRef | ✅ Verified fixed |
| 22 | BUG-C3-06 | handleSend stale closure → uses startInvestigationRef | ✅ Verified fixed |
| 23 | BUG-D1-01 | Kill investigation broken → event loop polls DB every 5 iterations + _persist_task guards | ✅ Verified fixed |
| 24 | BUG-D1-02 | Chat.tsx status_change wrong string → checks "HALT"/"HALTED"/"COMPLETED" | ✅ Verified fixed |

---

## Summary

Three bugs found. None are critical (no crashes, no security vulnerabilities, no data corruption). Two are logic/behavioral errors introduced by incomplete fixes in earlier rounds; one is a UI display error.

**BUG-E1-01** (Medium): Report generator can override user kill signal with COMPLETED status. Narrow race window, real user-visible impact.

**BUG-E1-02** (Medium): Config default 1.0 for DIMINISHING_SCORE_DELTA_THRESHOLD effectively disables the score-delta dimension of the DR check, causing the system to ignore score improvement when making DR decisions. Behavioral regression from C-round partial fix.

**BUG-E1-03** (Low): UI shows "FAILED" instead of "HALTED" when user kills an investigation via the SSE done event path. Semantically misleading but functionally minor (same visual color).

**Verdict: NOT ZERO BUGS.** Three bugs found. Production deployment should await fixes for BUG-E1-01 and BUG-E1-02. BUG-E1-03 can be addressed concurrently.
