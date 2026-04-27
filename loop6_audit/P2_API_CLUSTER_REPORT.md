# P2 API Cluster Report — B-16..B-21

**Date:** 2026-04-27  
**Branch:** `loop6/zero-bug`  
**Subagent:** P2 API cluster B-16..B-21  

---

## Summary

All six P2 API-tier bugs (B-16..B-21) are **FIXED**. One new migration (012) was deployed
to both local Postgres (`PGHOST=/tmp PGPORT=55432`) and live NestD (`afnbtbeayfkwznhzafay`).
Six new test files and one new contract test were added. All pre-existing passing tests
continue to pass.

---

## Bugs Fixed

### B-16 — admin_set_credits skips ledger (R3 admin path)

**Root cause:** `admin_set_credits` updated `profiles.tokens` directly with no corresponding
`credit_buckets` or `credit_transactions` row, widening the R3 drift on every admin credit
adjustment.

**Fix — approach (a) direct set path:**

Migration `012_p2_b16_admin_set_credits_ledger.sql` rewrites `admin_set_credits`:

- **Grant (v_delta > 0):** Inserts a `credit_buckets` row
  (`source='admin_grant'`, `expires_at=NULL` — permanent) and a `credit_transactions` row
  (`type='grant'`, `metadata={'admin_action': true, 'actor': caller_uuid}`).
- **Debit (v_delta < 0):** Drains existing unexpired buckets FIFO via `FOR UPDATE SKIP LOCKED`;
  inserts a `credit_transactions` row (`type='spend'`) per bucket drained. Any shortfall not
  covered by existing buckets still writes a spend row against `NULL` bucket so the audit
  trail is complete.
- **No-op (v_delta == 0):** No ledger row written (idempotent).
- `profiles.tokens` is still updated to `v_final` (B-05 invariant preserved).
- Original audit_log write (`admin.set_credits`) preserved from 007.

**Applied to:** local DB + live NestD (via Supabase MCP `apply_migration`).  
**Migration files:** `frontend/supabase/migrations/012_p2_b16_admin_set_credits_ledger.sql`  
**Revert:** `frontend/supabase/migrations/012_revert.sql`  
**Contract test:** `tests/contracts/C15_admin_set_credits_writes_ledger.sql` — PASS  
**Pytest:** `tests/test_b16_admin_set_credits_ledger.py` — 5 tests PASS  
**C05/C06 status:** Both continue to PASS (search_path and audit_log invariants preserved).

---

### B-17 — Legacy admin credits endpoint keeps direct-token mutation path

**Root cause:** `POST /api/admin/users/{uid}/credits` (v1) called the `admin_set_credits` RPC
with a raw `httpx.AsyncClient` call, bypassing `_admin_rpc_call` and the audit+ledger path.
The v2 endpoint at `/credits-v2` existed but the v1 path remained live.

**Fix:**

Rewrote the v1 handler (`admin_set_credits` Python function in `api.py`) to delegate to
`admin_adjust_credits` via `_admin_rpc_call` — the same RPC used by the v2 endpoint. The
response shape `{user_id, new_balance}` is preserved for backward compatibility. The raw
`httpx.AsyncClient` call to `admin_set_credits` RPC was removed entirely.

As defence-in-depth, migration 012 also makes the `admin_set_credits` SQL function itself
ledger-aware, so any future code that calls it directly will still write ledger rows.

**Files changed:** `mariana/api.py` (lines ~6978-7024)  
**Pytest:** `tests/test_b17_legacy_admin_endpoint.py` — 4 tests PASS

---

### B-18 — Admin mutation routes silently ignore audit-log failures

**Root cause:** Four admin mutation routes (`admin_feature_flags_upsert`,
`admin_feature_flags_delete`, `admin_danger_flush_redis`, `admin_danger_halt_running`)
wrapped `admin_audit_insert` in `try: ... except HTTPException: pass`, meaning audit failures
were silently swallowed — the state change committed but no audit row was written.

**Fix:**

Added `_audit_or_503(request, actor, action, target_type, target_id, ...)` async helper
(~65 lines) that:
1. Calls `admin_audit_insert` via `_admin_rpc_call`.
2. On `HTTPException` or any other exception, logs at `CRITICAL` level and raises
   `HTTPException(503)` with a descriptive message.
3. Succeeded → returns normally.

All four silent `except HTTPException: pass` blocks replaced with `await _audit_or_503(...)`.
The 503 informs the caller that the mutation committed but the audit trail is incomplete,
allowing retries and operator investigation.

**Files changed:** `mariana/api.py` (helper at ~7149, four route replacements)  
**Pytest:** `tests/test_b18_audit_failure_returns_503.py` — 7 tests PASS

---

### B-19 — Shutdown route bypasses JWT admin check

**Root cause:** `POST /api/shutdown` validated only the `X-Shutdown-Token` / `X-Admin-Key`
header secret. Any party who obtained the secret (leaked env var, log scrape) could trigger a
shutdown without any identity check or audit trail.

**Fix:**

Added `caller: dict[str, str] = Depends(_require_admin)` and `request: Request` parameters
to `graceful_shutdown`. The `_require_admin` dependency verifies the Supabase JWT and confirms
the caller is an admin user (via `_is_admin_user`). The `X-Admin-Key` header check remains as
a secondary factor (defence-in-depth). Both must pass.

**Security posture:** Shutdown now requires:
1. Valid admin Supabase JWT (proves identity, checked server-side).
2. Shared `ADMIN_SECRET_KEY` header (second factor, constant-time comparison).

**Files changed:** `mariana/api.py` (lines ~7825-7866)  
**Pytest:** `tests/test_b19_shutdown_jwt_admin_required.py` — 5 tests PASS

---

### B-20 — Admin authorization cache stale positive decisions

**Root cause:** `_is_admin_user` cached both positive (admin=True) and negative (admin=False)
decisions for 30 s. After admin role revocation, a revoked admin could continue to make admin
API calls for up to 30 s.

**Fix (lowest-risk approach — drop positive caching):**

- Positive decisions (is_admin=True) are **never cached**. Every admin check hits the DB
  (or the env-admin fast path). This ensures revocations take effect on the very next request.
- Negative decisions (is_admin=False) are cached for at most **5 s** to reduce load on
  repeated non-admin probes.
- New `_clear_admin_cache(user_id)` helper immediately evicts any cache entry. Called from
  `admin_user_set_role` on every role change.
- `_ADMIN_ROLE_CACHE_TTL = 30.0` replaced by `_ADMIN_ROLE_CACHE_NEGATIVE_TTL = 5.0`.

**Files changed:** `mariana/api.py` (lines ~143-223, +admin_user_set_role cache eviction)  
**Pytest:** `tests/test_b20_admin_cache_ttl.py` — 6 tests PASS

---

### B-21 — In-process rate limiter not shared across workers

**Root cause:** The `slowapi` Limiter was constructed without a `storage_uri`, defaulting to
in-memory storage (per-process). With `uvicorn --workers N`, each worker maintained a
separate counter, giving effective limit = N × 60 req/min per IP.

**Fix:**

The module-level `_redis_rate_limit_url` is read from `os.environ["REDIS_URL"]` at import
time. When set:
- `Limiter(key_func=get_remote_address, default_limits=["60/minute"], storage_uri=REDIS_URL)` —
  shared Redis backend, single counter across all workers.

When `REDIS_URL` is not set:
- Falls back to per-process in-memory limiter.
- Emits `RuntimeWarning` at module load so operators see the degraded mode in logs.

The existing `RateLimitMiddleware` (in-memory per-key deque) is unchanged and continues to
provide a secondary per-process guard.

**Files changed:** `mariana/api.py` (lines ~380-422)  
**Pytest:** `tests/test_b21_redis_rate_limit.py` — 6 tests PASS (3 skipped: slowapi/fakeredis optional)

---

## Test Results

### Contract tests (`bash scripts/run_contract_tests.sh`)

| Test | Status | Notes |
|------|--------|-------|
| C05_admin_set_credits_search_path | PASS | search_path preserved |
| C06_admin_set_credits_writes_audit | PASS | audit_log write preserved |
| C15_admin_set_credits_writes_ledger | PASS | new: ledger rows verified |
| C09_research_tasks_owner_fk | FAIL | **pre-existing** — F-05 migration 010 not applied to local; not caused by B-16..B-21 |

### pytest

```
183 passed, 13 skipped (pre-B-16 suite)
+ 31 passed, 3 skipped (B-16..B-21 new tests)
= 214 passed, 16 skipped total
```

5 `test_f06_intel_pagination` tests FAIL — **pre-existing** (failed before our changes,
owned by F-06 subagent).

### vitest (frontend)

```
51 passed (51 tests) — all green, unchanged.
```

---

## Files Changed

### Modified
- `mariana/api.py` — B-17 v1 alias, B-18 _audit_or_503, B-19 shutdown JWT, B-20 cache TTL, B-21 Redis limiter
- `loop6_audit/REGISTRY.md` — B-16..B-21 dedup table + DAG rows marked FIXED

### New
- `frontend/supabase/migrations/012_p2_b16_admin_set_credits_ledger.sql`
- `frontend/supabase/migrations/012_revert.sql`
- `tests/contracts/C15_admin_set_credits_writes_ledger.sql`
- `tests/test_b16_admin_set_credits_ledger.py`
- `tests/test_b17_legacy_admin_endpoint.py`
- `tests/test_b18_audit_failure_returns_503.py`
- `tests/test_b19_shutdown_jwt_admin_required.py`
- `tests/test_b20_admin_cache_ttl.py`
- `tests/test_b21_redis_rate_limit.py`

---

## Guardrails Compliance

- Migrations 008-011: **not touched** ✓
- Intelligence routes / research_tasks ownership (F-05/F-06): **not touched** ✓
- Frontend (B-25..B-29): **not touched** ✓
- B-01 split-revoke, B-02 search_path, B-04 refund, B-05 ledger sync, F-03 clawbacks: **preserved** ✓

---

## Known Limitations / Deferred

None. All six bugs were fully fixed within scope. No scope creep was required.

The C09 contract test failure is a pre-existing issue from migration 010 (F-05 subagent)
not having been applied to the local test DB. It is not caused by and does not affect
the B-16..B-21 fixes.
