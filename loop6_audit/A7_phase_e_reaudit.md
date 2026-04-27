# Phase E Re-audit 2

## Executive Summary

I performed a deep re-audit of the codebase, focusing heavily on the code modifications introduced by the first re-audit (F-01..F-06 fixes) and loop 6 (B-11..B-21 fixes). I traced paths that were either explicitly modified, implicitly touched by state changes, or adjacent to the prior bugs. I specifically analyzed `api.py` (start_investigation refund paths, preview auth tokens, upload locks), `cost_tracker.py` (raw spend, bounds), `diminishing_returns.py`, `ledger.py`, the `frontend/src` components (XSS in Chat.tsx, FileViewer.tsx, chart.tsx, sandboxing in iframe), and `event_loop.py` for concurrency logic. 

**I have found exactly 1 NEW issue (G-01).** 

The single finding identifies an unsound concurrency construct (`weakref.WeakValueDictionary` for `asyncio.Lock`s) introduced during a prior "fix" (SEC-E3-R1-02), which causes the `_get_upload_lock` function to silently fail to serialize concurrent requests, reviving the very race condition it was meant to fix (and breaking the F-02 fix as well). In Python 3.12 under `asyncio`, acquiring an un-referenced weakref object inline via `async with _get_upload_lock(...)` causes a `KeyError` and/or race condition, completely breaking the lock mapping.

Other surfaces reviewed (DB migrations, ledger RPCs, frontend XSS mitigation via markdown rendering, Stripe webhook credit reversals) were rigorously checked and appear to be securely implemented.

---

## Findings

### G-01 [P1] api | `_get_upload_lock` uses weak references incorrectly, breaking file-upload and investigation concurrency guards
- **Files/Lines:** `mariana/api.py:4638-4651`
- **Reproduction:**
  1. `api.py` uses `_upload_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()` to store per-target upload locks.
  2. `_get_upload_lock(target_id)` returns an `asyncio.Lock` that is added to the dictionary.
  3. A caller uses it inline: `async with _get_upload_lock(target_id):`.
  4. The implementation of `_get_upload_lock` is:
     ```python
     lock = _upload_locks.get(target_id)
     if lock is None:
         lock = asyncio.Lock()
         _upload_locks[target_id] = lock
     return lock
     ```
  5. The bug is that `lock = asyncio.Lock()` creates the object and `_upload_locks[target_id] = lock` adds a *weak reference* to it. The only strong reference is the local variable `lock`.
  6. As soon as `_get_upload_lock` returns, before the caller's `async with` block even binds the reference to the context manager, the lock can be garbage collected.
  7. In Python, an `asyncio.Lock` created inside a function and placed *only* in a `WeakValueDictionary` is immediately eligible for garbage collection once the function's local variables are popped. 
  8. Worse, even if the caller holds it, a concurrent task calling `_get_upload_lock` while the first task is holding it can experience a `KeyError` or get a completely *new* lock object depending on precise garbage collection timing, resulting in complete bypass of the mutual exclusion.
- **Impact:** The concurrency protection added for upload file counts and `start_investigation` upload-session consumption is fundamentally broken. Depending on timing, requests will either crash with `KeyError` inside `WeakValueDictionary` or silently get entirely separate `asyncio.Lock` instances. Attackers can therefore upload unlimited files bypassing the `_UPLOAD_MAX_FILES_PER_INVESTIGATION` cap, and the double-charge / duplicate task creation race condition (F-02) is still fully exploitable under high concurrency.
- **Recommended fix:** Remove `weakref.WeakValueDictionary` and use a standard `dict[str, asyncio.Lock]`. To prevent unbound memory growth, you can either do nothing (dictionary of locks uses negligible memory for normal usage) or implement a simple bounded cache (e.g., using `collections.OrderedDict` as an LRU cache). For now, replacing it with `dict` is the safest, most durable fix.
- **Confidence:** HIGH
