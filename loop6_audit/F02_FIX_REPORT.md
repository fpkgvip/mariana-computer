# F-02 Fix Report — Upload Session Race Fix

**Finding:** F-02 (Phase E re-audit)  
**Severity:** P1 (Category A — race / concurrency)  
**Fixed by:** Subagent on 2026-04-27  

---

## Summary

`POST /api/investigations` consumed a pending upload session (read `.owner`, moved files, removed directory) **without holding the `_get_upload_lock(f"pending-{session_uuid}")` lock** that the upload endpoint at `api.py:4677` uses for the same session.

Two concurrent callers with the same `upload_session_uuid` could both pass the ownership check, both reserve credits, and then race on `shutil.move` / `rmdir`, resulting in:
- Two tasks created for one session
- Double credit reservation (no refund for the loser)
- Files split or missing between tasks

---

## Files Changed

| File | Change |
|------|--------|
| `mariana/api.py` | Fixed `start_investigation` (lines ~2841–2955) |
| `tests/test_f02_upload_session_race.py` | New regression test file (6 tests) |

---

## Lock Semantics

The fix reuses the **exact same `asyncio.Lock` instance** that the upload endpoint acquires:

```python
async with _get_upload_lock(f"pending-{session_uuid}"):
    ...
```

`_get_upload_lock` returns a cached `asyncio.Lock` from a `weakref.WeakValueDictionary` keyed on `f"pending-{session_uuid}"`. As long as either the upload endpoint or `start_investigation` holds the lock, the same strong reference keeps the lock alive in the weak cache — so both endpoints share the identical lock object for the same session UUID.

The lock is held across:
1. **Existence check** — `pending_dir.is_dir()` (now inside the lock)
2. **Ownership check** — reading `.owner` file and comparing to `current_user["user_id"]`
3. **Atomic rename claim** — `os.rename(pending_dir, claimed_dir)`
4. **File moves** — `shutil.move` for each file to `files/{task_id}/`
5. **Claimed dir cleanup** — `shutil.rmtree(claimed_dir)`

The lock is released only after all these steps complete, preventing any concurrent caller from interleaving.

---

## Claim Mechanism

After passing the ownership check, the session is **atomically claimed** via:

```python
claimed_dir = Path(cfg.DATA_ROOT) / "uploads" / "claimed" / f"{session_uuid}-{task_id}"
claimed_dir.parent.mkdir(parents=True, exist_ok=True)
os.rename(str(pending_dir), str(claimed_dir))
```

`os.rename` is atomic on POSIX filesystems (same filesystem). Exactly one concurrent caller wins the rename:
- **Winner:** proceeds to move files from `claimed/{session_uuid}-{task_id}/` to `files/{task_id}/`, then removes the claimed dir.
- **Loser:** either finds `pending_dir.is_dir()` is False (inside the lock, after acquiring it) and raises 409, or gets `FileNotFoundError`/`OSError` from the rename itself and raises 409.

The second caller **cannot** sneak through the outer `if body.upload_session_uuid:` path without acquiring the lock first, because the entire existence/ownership/rename sequence is now inside the `async with` block.

---

## Credit Rollback on Conflict

When the losing caller raises 409:

1. `reserved_credits` is captured in `_credits_to_refund`
2. `reserved_credits` is **zeroed** before the refund call — this prevents the outer `except HTTPException:` handler from issuing a second `_supabase_add_credits` call (no double-refund)
3. `_supabase_add_credits(user_id, _credits_to_refund, cfg)` is called to refund the reservation
4. `HTTPException(status_code=409, detail="Upload session not found or already consumed")` is raised

This mirrors the existing rollback pattern in `start_investigation`'s `except HTTPException:` and `except OSError:` handlers.

---

## Behavioral Changes

| Scenario | Before Fix | After Fix |
|----------|-----------|-----------|
| Concurrent calls, same session | Both succeed; double credit reservation; files split | First succeeds; second gets 409 + credit refund |
| Sequential second call after session consumed | Second call silently creates task without files (no 409) | Second call gets 409 + credit refund |
| `upload_session_uuid` provided, pending dir never existed | Silently creates task without files | 409 returned (session must be created first) |
| No `upload_session_uuid` | Task created normally | Task created normally (unchanged) |

The third behavioral change (never-existed session → 409) is intentional and correct: callers that pass a session UUID are expected to have created it via the upload endpoint first.

---

## Test Results

### pytest (backend)

```
cd /home/user/workspace/mariana
PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb \
  python -m pytest tests/ -x --tb=short -q

139 passed, 10 skipped
```

The 10 skipped tests require a live Postgres instance or Redis and were skipped before this change as well.

### New regression tests (`tests/test_f02_upload_session_race.py`)

| Test | Description |
|------|-------------|
| `test_concurrent_start_investigation_with_same_session_uuid_only_one_succeeds` | `asyncio.gather` fires two concurrent calls with the same session UUID; asserts exactly one 202 and one 409; files attached exactly once to the winning task |
| `test_second_call_after_consume_returns_409` | Sequential second call after session consumed returns 409 |
| `test_credit_reservation_refunded_on_conflict` | The 409 loser's credits are refunded via `_supabase_add_credits` |
| `test_no_double_refund_on_409` | The outer `except HTTPException:` handler does NOT issue a second refund |
| `test_no_upload_session_succeeds` | No `upload_session_uuid` → normal task creation, no refunds |
| `test_claimed_dir_cleaned_up_after_success` | Claimed directory is removed after files are moved to task dir |

### vitest (frontend)

```
cd /home/user/workspace/mariana/frontend && npm run test

Test Files  6 passed (6)
     Tests  51 passed (51)
```

---

## Validation Checklist

- [x] `_get_upload_lock(f"pending-{session_uuid}")` held across entire consume
- [x] Atomic rename to `claimed/` before file moves; second caller gets 409
- [x] Credit reservation refunded on 409 conflict
- [x] `reserved_credits` zeroed before raise to prevent double-refund
- [x] All pytest tests green (139 passed, 10 skipped)
- [x] All vitest tests green (51 passed)
- [x] New tests cover: race + idempotency + rollback + no-double-refund + happy-path + cleanup
- [x] Report at `loop6_audit/F02_FIX_REPORT.md`
