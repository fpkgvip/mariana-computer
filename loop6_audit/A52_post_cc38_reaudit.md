# A52 Re-audit #47 (gpt) — streak 2/3 post-CC-38

**Branch:** loop6/zero-bug
**HEAD:** 2141447
**Cumulative range:** c108b1e..2141447

## One-line verdict
PASS — streak advances to 2/3

## Findings count
- 1 total: 0 critical / 0 high / 0 medium / 0 low / 1 info

## Production-ready: Y

The one info finding is the carried-over backup/DR posture gap (A51 Finding 3 = A50 Finding 4). No new findings. CC-37 and CC-38 are both verified fixed.

---

## A. CC-37/CC-38 verification

- **CC-37: PASS** — `sandbox_server/app.py:208-271` defines `_BoundedTTLCache` with `OrderedDict` FIFO eviction and per-`get` TTL enforcement. `_WORKSPACE_SIZE_CACHE` instantiated at line 273 with `maxsize=10_000` (`_WORKSPACE_SIZE_CACHE_MAX_ENTRIES`) and `ttl=5.0` (`_WORKSPACE_SIZE_TTL_SEC`). All three call sites updated: `_workspace_size_bytes` (get + set at lines 291, 304), `_workspace_size_cache_set` (line 349). `tests/test_cc37_workspace_size_cache_bound.py` present and covers 5 cases (FIFO eviction, module-instance type/capacity pin, TTL expiry, hit/miss path, CC-34 quota regression guard).

- **CC-38: PASS** — `sandbox_server/app.py:_JsonLogFormatter.format` emits `"timestamp"` (line 121) and `"event"` (line 124); no `"msg":` or `"ts":` in the payload dict. `browser_server/app.py:_JsonLogFormatter.format` likewise emits `"timestamp"` (line 108) and `"event"` (line 111). The `"ts"` at `sandbox_server/app.py:460` and `browser_server/app.py:409` is the `/health` endpoint response body — not the log formatter. `tests/test_cc38_sidecar_log_field_parity.py` present with 4 tests covering event+timestamp presence, legacy-field absence, source-code revert guard, and orchestrator structlog alignment pin.

---

## B. Cumulative review (single-bullet each)

- **CC-02: PASS** — reconciler limit holds; no regression signal.
- **CC-04: PASS** — vault read path fail-closes for malformed/missing/invalid payloads under `requires_vault=True` (`mariana/vault/runtime.py`).
- **CC-05: PASS** — reconciler/config validation holds.
- **CC-06: PASS** — vault empty-object payload still treated as fail-closed.
- **CC-07: PASS** — frontend hero-copy scan (see Section E); no forbidden terms in `frontend/src/pages/`.
- **CC-08: PASS** — all `/api/admin/*` routes depend on `_require_admin`; admin observability remains admin-gated.
- **CC-09: PASS** — vault runtime key/value validation contract holds.
- **CC-10: PASS** — `\Z` anchors used in all identifier validators; only `_BYTEA_HEX_RE` (`mariana/vault/store.py:63`) uses `$` for a fixed bytea-format pattern. Verified `_SAFE_ID_RE` in `sandbox_server/app.py` still uses `\Z`.
- **CC-11: PASS** — oversize vault value still fail-closes.
- **CC-12: PASS** — every `uses:` in `.github/workflows/` is SHA-pinned; grep for floating tags returns 0 hits.
- **CC-13: PASS** — `ci.yml` and `deploy.yml` carry top-level `permissions: contents: read`.
- **CC-14: PASS** — no hardcoded `sk_(live|test)_` / `AKIA` / `ghp_` / `xox[bapr]-` patterns.
- **CC-15: PASS** — deploy concurrency hardening intact.
- **CC-16: PASS** — `SlowAPIMiddleware` wired globally at `mariana/api.py:545`; startup assertion `_RATE_LIMIT_STORAGE_VALIDATED` blocks boot without it.
- **CC-17: PASS** — SQL `search_path` pinning intact; only Supabase-built-in `auth.*` stubs unpinned (not repo-owned).
- **CC-18: PASS** — `detail=f"..."` in `browser_server/app.py` — 0 hits.
- **CC-19: PASS** — `detail=f"..."` in `sandbox_server/app.py` — 0 hits.
- **CC-20: PASS** — agent route 404 details remain generic.
- **CC-21: PASS** — canonical agent error contract enforced end-to-end via CC-35 AST invariant test.
- **CC-22: PASS** — main API 404 details remain canonical; no internal identifier echoing.
- **CC-23: PASS** — all `frontend/package.json` and `e2e/package.json` deps are exact-pinned; no `^`/`~`.
- **CC-24: PASS** — surviving `detail=f"..."` in `mariana/api.py` interpolate only constants, plan names, numeric bounds, file extensions, status codes, or user-supplied URL hostnames — no internal IDs leaked.
- **CC-25: PASS** — ToolError persistence uses canonical `tool_error`; no raw tool detail leak.
- **CC-26: PASS** — lockfile / package exact-pinning posture intact.
- **CC-27: PASS** — oversize vault entry-count handling fail-closes.
- **CC-28: PASS** — route-level projected enforcement in place (CC-34 hardening).
- **CC-29: PASS** — admin RPC error scrubbing (`"admin RPC failed"` only) intact at `mariana/api.py:8851-8886`.
- **CC-30: PASS** — `_ADMIN_ROLE_CACHE` remains a `_BoundedTTLCache(maxsize=10_000)`.
- **CC-31: PASS** — `public.touch_updated_at()` pins `SET search_path = public, pg_temp`.
- **CC-32: PASS** — filename echo scrub intact; no `safe_name!r`/`filename!r` in API error details.
- **CC-33: PASS** — `/metrics` is `include_in_schema=False`, depends on `_require_admin`, self-skips its counter.
- **CC-34: PASS** — projected workspace quota enforced on `/fs/write` and `/exec`; cache refresh post-write closes same-TTL race.
- **CC-35: PASS** — canonical task/step error code allow-lists pinned by AST invariant test.
- **CC-36: PASS** — sidecar JSON logging via `_JsonLogFormatter` and `LOG_FORMAT=json|text` on both sidecars.
- **CC-37: PASS** — `_WORKSPACE_SIZE_CACHE` is now a bounded TTL cache (maxsize=10k, TTL=5s, FIFO). See Section A.
- **CC-38: PASS** — sidecar JSON log fields aligned to `event`/`timestamp`. See Section A.

---

## C. New territory probe

- **Race conditions / TOCTOU:** PASS — `mariana/api.py` documents TOCTOU mitigations at 3363, 3859, 5216, 5394, 7726 with atomic DB operations. Sandbox quota check has no await between read and write within a single async coroutine under `--workers 1` constraint. No new races introduced by CC-37/CC-38 changes.

- **Pydantic `extra='allow'`:** PASS — grep for `extra='allow'` and `extra="allow"` in all Python files returns 0 hits.

- **`json.loads` on untrusted input without size cap:** PASS — `sandbox_server/app.py` and `browser_server/app.py` contain no direct `json.loads` calls; FastAPI request body parsing handles deserialization with Pydantic model validation (field-level constraints). `mariana/api.py` has 23 `json.loads` calls but they operate on DB-retrieved data or validated payloads.

- **Path traversal coverage:** PASS — `sandbox_server/app.py:369-414` defines `_safe_sandbox_path` that rejects absolute paths, traversal sequences (`..`), null bytes, and validates resolved path stays inside user root with `is_relative_to`. `_SAFE_ID_RE` validates user_id before it is used as a path component.

- **Regex DoS:** PASS — all identifier validators use simple anchored character-class regexes (no catastrophic backtracking; no nested quantifiers). `_BYTEA_HEX_RE` in `mariana/vault/store.py:63` is a fixed-format hex validator with no catastrophic structure.

- **Time/clock skew:** PASS — no naive `datetime.now()` or `datetime.utcnow()` in non-test production code. All monotonic timing for cache TTL (`time.monotonic()`); all wall-clock uses are timezone-aware.

- **Sidecar log field user-input sanitization:** PASS — `sandbox_server/app.py` logger calls pass structured `extra=` dicts with controlled keys (`workspace`, `size`, `additional`, `projected`, `max`). No raw user-supplied path strings are interpolated into log messages; path strings are keyed under `workspace` in structured context. No PII (user_id, email, secrets) flows into log payloads.

- **HTTP client timeouts:** PASS — `browser_server/app.py` enforces `timeout_ms` via Playwright `set_default_timeout`; sandbox subprocess execution has `wall_timeout_sec` (enforced via `asyncio.wait_for` at line 622 and hard-kill at 634). Admin health probe uses `asyncio.wait_for(..., timeout=5.0)` per component.

- **`asyncio.create_task` exception swallowing:** PASS — no `asyncio.create_task` calls found in `sandbox_server/app.py`, `browser_server/app.py`, or `mariana/api.py`. Background work uses structured coroutine composition, not fire-and-forget tasks.

- **Cookie security flags:** PASS — `mariana/api.py:1986-1993` sets preview cookies with `httponly=True`, `secure=True`, `samesite="lax"` and scoped `path`. No other `set_cookie` calls found.

- **CSRF posture:** PASS — API is stateless JWT-bearer auth; no session cookies used for API state mutation. Preview cookies are read-only scope-scoped tokens, not CSRF-relevant. No form-POST mutation paths found.

- **Open redirect:** PASS — Stripe checkout redirect URLs are validated against `_ALLOWED_REDIRECT_HOSTS` derived from `_DEFAULT_PROD_CORS_ORIGINS`/`_DEFAULT_DEV_CORS_ORIGINS` (`mariana/api.py:6258-6283`). Only `/preview/{task_id}/index.html` redirect (`mariana/api.py:1945`) is a static internal path construction, not user-controlled.

- **CORS allow-list:** PASS — `CORSMiddleware` uses `allow_origins=_get_cors_origins()` (explicit list) with `allow_credentials=True`; wildcard origin is forbidden by CORS spec when credentials are enabled. Production list is `_DEFAULT_PROD_CORS_ORIGINS` only; dev origins added only when dev mode enabled.

- **Health endpoint info leak:** PASS — `GET /api/health` returns only `{status: "ok", version: _VERSION}` (liveness probe). The deep probe at `/api/admin/health-probe` is admin-gated (`Depends(_require_admin)`); exception details at line 9416 (`f"{type(exc).__name__}: {str(exc)[:200]}"`) are admin-visible only. Sandbox `/health` and browser `/health` return `status`, `workspace_root` (internal path), and `ts` — these sidecars are on an `internal: true` Docker network with no public exposure.

- **Error page stack-trace leak:** PASS — no `traceback.format_exc()` or raw exception message in public HTTP responses. FastAPI validation errors return structured Pydantic field-error lists. JSON parse errors return `{"detail": "Invalid JSON in request body", "type": "json_parse_error"}` (`mariana/api.py:10675`).

---

## D. Findings

### Finding 1 — Backup/DR posture not repo-visible (carried over from A50 Finding 4, A51 Finding 3)
**Severity:** Info
**File:** operational/runbook (no code location)
**Evidence:** No first-class backup/restore/retention/RPO/RTO posture documented in `docker-compose.yml`, `.github/workflows/*.yml`, `README.md`, or top-level docs. Absence-of-evidence rather than evidence-of-defect.
**Repro:** repo inspection only.
**Why it matters:** for a billion-dollar launch, operators need explicit testable backup/restore ownership. Cannot be confirmed from the repository.
**Fix:** document and automate backup/restore/retention posture in repo-adjacent operational material.

---

## E. Final paranoid grep

| Check | Result |
|-------|--------|
| `detail=f"..."` in `sandbox_server/app.py` | **0 hits** ✅ |
| `detail=f"..."` in `browser_server/app.py` | **0 hits** ✅ |
| `grep -rnE 'uses: [^@]+@[v0-9]' .github/workflows/` (floating tags) | **0 hits** ✅ |
| `re.compile(r"...$")` — only `_BYTEA_HEX_RE` in `mariana/vault/store.py:63` | **1 hit (expected)** ✅ |
| `datetime.(now\|utcnow)()` naive in non-test production code | **0 hits** ✅ |
| `unleash\|unlock\|supercharge\|revolutionize\|empower\|magical\|world-class` in `frontend/src/pages/` | `unlock` hits only in `DevVault.tsx`/`Vault.tsx`/`Build.tsx` — vault-unlock UX, not hero copy ✅ |
| `extra='allow'` or `extra="allow"` Pydantic | **0 hits** ✅ |
| `_WORKSPACE_SIZE_CACHE` is `_BoundedTTLCache` with `maxsize=10_000`, `ttl=5.0` | **CONFIRMED** `sandbox_server/app.py:208,273-275` ✅ |
| `_JsonLogFormatter` emits `"event"` and `"timestamp"` (not `"msg"` / `"ts"`) | **CONFIRMED** `sandbox_server/app.py:121,124`; `browser_server/app.py:108,111` ✅ |
| `ts` in sidecar `/health` responses is not a log field | **CONFIRMED** — health endpoint body only (`sandbox_server/app.py:460`, `browser_server/app.py:409`) ✅ |
| Cookie flags: `httponly=True, secure=True, samesite="lax"` | **CONFIRMED** `mariana/api.py:1991-1993` ✅ |
| `asyncio.create_task` in production paths | **0 hits** ✅ |
| `tests/test_cc37_workspace_size_cache_bound.py` exists | **CONFIRMED** ✅ |
| `tests/test_cc38_sidecar_log_field_parity.py` exists | **CONFIRMED** ✅ |
| HEAD commit SHA | `2141447` — matches expected ✅ |

**Paranoid grep verdict:** PASS.

---

## Summary

CC-37 and CC-38 are both correctly implemented and verified. The cumulative CC-02 + CC-04..38 stack holds. The sole surviving finding is the carried-over info-severity backup/DR posture gap — operational documentation work, not a code defect.

**CONVERGENCE STREAK 2/3 ACHIEVED.**
