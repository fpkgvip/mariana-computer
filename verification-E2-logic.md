# Verification E2 Logic Review

**Result: FAIL — 1 bug found**

---

## Scope

Six files audited for logic errors, state machine inconsistencies, data validation gaps, error handling issues, resource leaks, and concurrency issues:

1. `mariana/config.py` (376 lines)
2. `mariana/report/generator.py` (345 lines)
3. `mariana/tools/skills.py` (291 lines)
4. `mariana/orchestrator/event_loop.py` (2002 lines)
5. `mariana/api.py` (3225 lines)
6. `frontend/src/pages/Chat.tsx` (2040 lines)

---

## Bugs Found

### BUG-E2-L-01: `DIMINISHING_SCORE_DELTA_THRESHOLD` default mismatch between dataclass and `load_config()`

- **File:** `mariana/config.py`
- **Lines:** 83, 325
- **Severity:** Medium
- **Category:** Cross-file consistency / config default mismatch

**Description:**

The `AppConfig` dataclass declares the correct default on line 83:

```python
DIMINISHING_SCORE_DELTA_THRESHOLD: float = 0.1
```

However, the `load_config()` factory function on line 325 overrides it with the old, pre-fix value:

```python
DIMINISHING_SCORE_DELTA_THRESHOLD=_float("DIMINISHING_SCORE_DELTA_THRESHOLD", 1.0),
```

Since `load_config()` is the sole production constructor (called at `api.py` line 99), the dataclass default of `0.1` is never used. The operational code path always receives `1.0` unless the environment variable is explicitly set.

**Impact:**

The diminishing-returns guard checks whether successive investigation steps are producing meaningful new information by comparing score deltas against this threshold. With the threshold at `1.0` instead of `0.1`, the guard is 10x too lenient — it will almost never trigger, allowing investigations to continue running well past the point of diminishing returns. This wastes budget (LLM calls, tool invocations, credits) on unproductive steps.

**Fix:**

Change line 325 from:
```python
DIMINISHING_SCORE_DELTA_THRESHOLD=_float("DIMINISHING_SCORE_DELTA_THRESHOLD", 1.0),
```
to:
```python
DIMINISHING_SCORE_DELTA_THRESHOLD=_float("DIMINISHING_SCORE_DELTA_THRESHOLD", 0.1),
```

---

## Verification Notes

All previously-fixed items from prior audit rounds were confirmed in place across all six files. No regressions detected. No other new logic errors, state machine inconsistencies, data validation gaps, error handling issues, resource leaks, or concurrency issues were found.
