# CC-27 through CC-33 Fix Report

**Date:** 2026-04-28
**Branch:** `loop6/zero-bug`
**Pre-fix HEAD:** `793069b`
**Post-fix HEAD:** `2480961`
**Audit input:** `loop6_audit/A49_post_cc26_reaudit.md`

---

## Scope

Seven findings from the A49 re-audit (post-CC-26):

| ID | Severity | Class |
|----|----------|-------|
| CC-27 | P2 | vault/runtime/silent-truncation |
| CC-28 | P2 | sandbox-server/disk-quota |
| CC-29 | P2 | api/admin-rpc/error-leakage |
| CC-30 | P3 | api/admin-cache/unbounded-growth |
| CC-31 | P3 | db/security-invoker/unpinned-search-path |
| CC-32 | P3 | api/error-detail-leakage/filename-echo |
| CC-33 | P3 | api/observability/no-metrics-endpoint |

All seven are `FIXED`. Zero deferred items.

---

## Commits (sequential, fast-forward push)

| # | SHA | Message |
|---|------|---------|
| 1 | `dfdcace` | CC-27 fix vault entry-count silent truncation under requires_vault |
| 2 | `497e874` | CC-28 add per-workspace disk quota with configurable max bytes |
| 3 | `151762c` | CC-29 scrub admin RPC error responses of Supabase body leakage |
| 4 | `5e19909` | CC-30 bound _ADMIN_ROLE_CACHE size to prevent unbounded growth |
| 5 | `d3f751a` | CC-31 pin search_path on touch_updated_at SECURITY INVOKER trigger; tighten CC-17 test invariant |
| 6 | `c3ab5bf` | CC-32 generalize 400 detail strings for filename rejection |
| 7 | `2480961` | CC-33 add /metrics endpoint with admin auth gating |

Push was a clean fast-forward (`793069b..2480961  loop6/zero-bug -> loop6/zero-bug`). No `--force`. No parallel-agent collisions.

---

## Per-CC details

### CC-27 — vault entry-count silent truncation
**File:** `mariana/vault/runtime.py`
**Approach:** Mirror the CC-04 fail-closed pattern. Added an `oversize_entries` check **before** the per-entry loop. Under `requires_vault=True` raise `VaultUnavailableError("oversize_entries")` with `task_id` + `entry_count` in `extra`. Under `requires_vault=False` preserve degrade-to-truncated-dict behaviour but emit a distinct `vault_env_oversize_payload_truncated` warning so ops alert separately from the per-entry warning that pre-existed.

**Tests:** `tests/test_cc27_vault_oversize_entries.py` — 5 async tests
- fail-closed under `requires_vault=True` with exact reason code
- truncate-with-warning under `requires_vault=False`
- payload exactly at `_MAX_ENTRIES` is healthy
- payload at `_MAX_ENTRIES + 1` trips the check
- warning extra carries `task_id` + `entry_count`

### CC-28 — workspace disk quota
**File:** `sandbox_server/app.py`
**Approach:** Added `_MAX_WORKSPACE_BYTES` (default 2 GiB, env override `SANDBOX_MAX_WORKSPACE_BYTES`), `_workspace_size_bytes(user_id)` (5-second cache, avoids `os.walk` thrash on hot paths), `_enforce_workspace_quota(user_id, additional_bytes=0)` which raises HTTP 507 with stable `detail="workspace_full"` and a `log.warning("workspace_quota_exceeded", extra={"user_id", "used_bytes", "limit_bytes"})`.

Wired into `/exec` (pre-exec; output goes to stdout buffer, not disk, so projects 0 additional bytes — refuses if already over) and `/fs/write` (pre-write with `len(decoded_content)` projection so the write itself cannot push over).

**Tests:** `tests/test_cc28_workspace_quota.py` — 4 tests
- `/fs/write` 507 when workspace at limit
- `/fs/write` 507 when projected size exceeds
- `/exec` 507 when over
- under-limit writes succeed

### CC-29 — admin RPC error response scrubbing
**File:** `mariana/api.py`
**Approach:** All 9 admin RPC handler sites converted. Central helper: `detail="admin RPC failed"`. Per-handler raises (8 sites): `detail="admin operation failed"`. Each site emits `logger.error("admin_rpc_failed", extra={"handler", "error", "error_type"})` BEFORE the raise so server-side observability is preserved verbatim.

**Tests:** `tests/test_cc29_admin_rpc_error_scrub.py` — 4 source-grep tests
- no `detail=str(` patterns in admin handler code
- no `detail=f"...{exc}"` / `detail=f"...{rpc_response.error}"` patterns
- canonical `admin_rpc_failed` log key exists
- pins the two canonical detail strings

### CC-30 — bounded _ADMIN_ROLE_CACHE
**File:** `mariana/api.py`
**Approach:** Replaced plain `dict` `_ADMIN_ROLE_CACHE` with a hand-rolled `_BoundedTTLCache` class. FIFO eviction via `collections.OrderedDict`. Default `max_size=10_000`, `ttl_seconds=300`. Implements `get(key)` (with TTL check), `__setitem__(key, value)` (FIFO eviction once `len() == max_size`), `pop(key, default)`, `clear()` (preserved for B-20 test compatibility), `__len__`, `__contains__`. No new dependency (`cachetools` not added — repo already minimises deps per CC-23).

**Tests:** `tests/test_cc30_admin_role_cache_bound.py` — 7 tests
- size stays at exactly `max_size` after `max_size + N` distinct insertions
- FIFO eviction order (oldest key gone first)
- TTL eviction in `get()`
- `clear()` preserves the bound
- `pop()` works as expected
- `__contains__` consistent with `get()`
- defaults match spec (`max_size=10_000`, `ttl_seconds=300`)

### CC-31 — touch_updated_at search_path + tightened CC-17 invariant
**Files:** `frontend/supabase/migrations/003_deft_vault.sql`, `tests/test_cc17_security_definer_search_path.py`
**Approach:** Added `SET search_path = public, pg_temp` to the `touch_updated_at` SECURITY INVOKER trigger function. Tightened the CC-17 regression test from "DEFINER-only" to a universal `CREATE [OR REPLACE] FUNCTION` check across `frontend/supabase/migrations/*.sql` plus `.github/scripts/ci_full_baseline.sql`. Auth-owned Supabase-managed functions (`auth.role`, `auth.uid`) are explicitly allow-listed via `AUTH_OWNED_FUNCTIONS = {"auth.role", "auth.uid"}`.

**Tests added (in existing file):** 2 tests (4 total in file)
- explicit `touch_updated_at` test pins the specific function has the clause
- universal CREATE FUNCTION scan test that would have caught CC-31 originally

### CC-32 — filename echo in 400 details
**File:** `mariana/api.py`
**Approach:** All 6 file-upload / file-rename handler sites converted. Two stable detail strings: `detail="invalid filename"` (5 sites: empty, NUL, bad grammar, path component, illegal extension) and `detail="symlinks are not allowed"` (1 site, no filename interpolation). Each site emits `logger.info("filename_rejected", extra={"filename", "reason"})` BEFORE the raise.

**Tests:** `tests/test_cc32_filename_echo_scrub.py` — 4 source-grep tests
- no `detail=f"... {filename!r}"` patterns remain
- no `detail=f"...{filename}..."` patterns remain
- canonical `filename_rejected` log key exists
- pins the two canonical detail strings

### CC-33 — /metrics endpoint with admin auth gating

**File:** `mariana/api.py`

**Decision: hand-rolled minimal exposition format (zero new deps).** The constraint asked us to evaluate `prometheus_fastapi_instrumentator` and fall back to hand-rolled if it brought too many transitive deps. We chose hand-rolled directly because:

1. We expose only 4 metrics (`http_requests_total`, `http_errors_total`, `http_5xx_total`, `process_uptime_seconds`). The instrumentator pulls in `prometheus_client` plus its dep tree — disproportionate for 4 metrics.
2. Prometheus 0.0.4 text exposition is trivially small (one `# HELP` + one `# TYPE` + one value line per metric).
3. CC-23 / CC-26 set the precedent of minimising frontend deps; same hygiene applies to the Python side. Adding deps requires re-running CC-23-style exact-pin work and re-validating idempotence.
4. The instrumentator's auto-labelling (handler / method / status) would still need our own admin-gating override.

**Implementation:**
- `threading.Lock`-guarded `dict[(method, status_class), int]` counters
- Middleware bumps counters after every response, except for the `/metrics` path itself (self-instrumentation skip — `request.url.path == "/metrics"`)
- `process_uptime_seconds` is `time.time() - _START_TIME` rendered as a gauge
- `/metrics` endpoint is `Depends(_require_admin)` → non-admin gets the standard 401/403 path
- Output format: `# HELP <name> <text>\n# TYPE <name> counter|gauge\n<name>{labels} <value>\n` (Prometheus 0.0.4 text exposition; content-type `text/plain; version=0.0.4`)

**Tests:** `tests/test_cc33_metrics_endpoint.py` — 5 tests (uses `fastapi.testclient` + `mod.app.dependency_overrides[mod._require_admin]` for admin auth)
- non-admin gets 403
- admin gets 200 with `text/plain; version=0.0.4` content-type
- all 4 metric names present in body
- counters increment after a sample request
- `/metrics` requests do NOT increment counters (self-instrumentation skip)

---

## Test summary

**Total new tests added:** 31 across 7 files

| CC | File | Test count |
|----|------|------------|
| CC-27 | `tests/test_cc27_vault_oversize_entries.py` | 5 (async) |
| CC-28 | `tests/test_cc28_workspace_quota.py` | 4 |
| CC-29 | `tests/test_cc29_admin_rpc_error_scrub.py` | 4 (source-grep) |
| CC-30 | `tests/test_cc30_admin_role_cache_bound.py` | 7 |
| CC-31 | `tests/test_cc17_security_definer_search_path.py` (extended) | 2 added (4 total in file) |
| CC-32 | `tests/test_cc32_filename_echo_scrub.py` | 4 (source-grep) |
| CC-33 | `tests/test_cc33_metrics_endpoint.py` | 5 |

---

## Verification

| Suite | Pre-fix | Post-fix |
|-------|---------|----------|
| pytest | 516 passed / 11 skipped / 0 failed | **547 passed / 11 skipped / 0 failed** (+31) |
| vitest | 144/144 | **144/144** (no change — no frontend code touched) |
| npm install idempotence (frontend) | clean | **clean** (`up to date in 801ms`, `git status --porcelain frontend/package-lock.json` empty) |

Commands used:
- `PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb python -m pytest -q`
- `cd frontend && npm test -- --run`
- `cd frontend && npm install --no-audit --no-fund && cd .. && git status --porcelain frontend/package-lock.json`
- `python -m ruff format <touched .py files>` after each edit

---

## Notes / known interactions

- **CC-30 vs B-20:** the prior B-20 cache tests called `_ADMIN_ROLE_CACHE.clear()`. The hand-rolled `_BoundedTTLCache` preserves `clear()` so those tests continue to pass without modification.
- **CC-29 + CC-32 + CC-33 all touch `mariana/api.py`:** to keep one logical commit per CC, we reset `mariana/api.py` to HEAD between each fix and re-applied edits sequentially. CC-29's diff is large because `python -m ruff format` reformatted unrelated lines on a busy file; this is consistent with the repo's existing ruff convention (CC-22 / CC-24 had similar churn).
- **CC-31 test extension:** placed in the existing `tests/test_cc17_security_definer_search_path.py` file rather than a new `tests/test_cc31_*` file because the new universal CREATE FUNCTION scan supersedes the prior DEFINER-only logic — a separate file would have duplicated the parsing helper.

---

## Deferred items

**None.** All seven A49 findings are FIXED at HEAD `2480961`.
