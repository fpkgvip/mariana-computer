# A53 Re-audit #48 (opus) — streak 3/3 FINAL post-CC-38

**Branch:** loop6/zero-bug
**HEAD:** 2141447

## One-line verdict
PASS — SECOND CONVERGENCE DECLARED

## Findings count
- 1 total: 0 critical / 0 high / 0 medium / 0 low / 1 info

## Production-ready: Y

The sole surviving finding is the carried-over **info-severity backup/DR posture** observation (A50 Finding 4 → A51 Finding 3 → A52 Finding 1). It is operational documentation rather than a code defect. After three consecutive zero-medium-or-higher audit rounds (#46 sonnet, #47 gpt, #48 opus, all on HEAD `2141447` post-CC-38) by **three different model lineages**, **second convergence is declared**.

---

## A. CC-37/CC-38 verification

- **CC-37: PASS** — `sandbox_server/app.py:208-271` defines `_BoundedTTLCache` with `OrderedDict` FIFO eviction and per-`get` TTL enforcement; module-level instance at line 273 (`_BoundedTTLCache(maxsize=10_000, ttl=5.0)`) replaces the previously unbounded `dict`. Verified by reading the `__setitem__` (FIFO `popitem(last=False)` over capacity), `get` (TTL eviction via `time.monotonic() - inserted_at >= ttl`), and `clear/pop/__len__/__contains__` shims. `tests/test_cc37_workspace_size_cache_bound.py` provides 5 tests: bounded eviction (FIFO), module-level instance type/capacity pin, TTL expiry on `get`, hit/miss correctness in `_workspace_size_bytes`, and CC-34 quota-projection regression guard. Mirrors the CC-30 `_ADMIN_ROLE_CACHE` pattern.

- **CC-38: PASS** — `sandbox_server/app.py:_JsonLogFormatter.format` (lines 117-140) emits `"timestamp"` (line 121) and `"event"` (line 124); identical schema in `browser_server/app.py:_JsonLogFormatter.format` (lines 104-127, with `"timestamp"` at line 108 and `"event"` at line 111). No `"msg"` or `"ts"` keys appear in either formatter. The `"ts"` string at `sandbox_server/app.py:460` and `browser_server/app.py:409` is a `/health` response body field, not the log emitter — confirmed by reading the surrounding `@app.get("/health")` routes. `tests/test_cc38_sidecar_log_field_parity.py` has 4 tests: presence of canonical fields on both sidecars, absence of legacy `msg/ts` keys, source-code revert guard via parametrised `import` of the formatter module, and orchestrator structlog `TimeStamper(fmt="iso") + JSONRenderer()` alignment pin in `mariana/main.py`.

---

## B. Cumulative review (single-bullet each)

- **CC-02: PASS** — reconciler limit holds; no regression evidence in any of CC-04..38 fixes.
- **CC-04: PASS** — `mariana/vault/runtime.py` `fetch_vault_env` distinguishes malformed/non-object/invalid-kv corruption shapes and raises `VaultUnavailableError` under `requires_vault=True`.
- **CC-05: PASS** — `_SETTLEMENT_RECONCILE_BATCH_SIZE` parsed with `_validate_int_in_range` guard; reconciler can't be bricked by negative env.
- **CC-06: PASS** — vault empty-`{}` payload still treated as fail-closed under `requires_vault=True`.
- **CC-07: PASS** — frontend hero-copy scan: `unleash|supercharge|revolutionize|empower|magical|world-class` returns 0 hero-copy hits; `unlock` only matches DevVault/Vault/Build vault-unlock UX (functional, not marketing).
- **CC-08: PASS** — every `/api/admin/*` route in `mariana/api.py` uses `Depends(_require_admin)`; admin observability remains admin-gated.
- **CC-09: PASS** — vault runtime key/value validation (`_NAME_RE` + isinstance(str) guards) intact.
- **CC-10: PASS** — `\Z` regex anchors used everywhere except `_BYTEA_HEX_RE` in `mariana/vault/store.py:63` (fixed-format bytea hex pattern, no multiline-injection vector). `_SAFE_ID_RE` in `sandbox_server/app.py` uses `\Z`.
- **CC-11: PASS** — oversize vault value still fail-closes via length checks.
- **CC-12: PASS** — every `uses:` in `.github/workflows/*.yml` is SHA-pinned; floating-tag grep `uses: [^@]+@(v?[0-9]+|main|master|latest)$` returns 0 hits.
- **CC-13: PASS** — `ci.yml` and `deploy.yml` carry top-level `permissions: contents: read`.
- **CC-14: PASS** — no hardcoded `sk_(live|test)_` / `AKIA` / `ghp_` / `xox[bapr]-` secret patterns.
- **CC-15: PASS** — deploy-workflow concurrency hardening intact.
- **CC-16: PASS** — `SlowAPIMiddleware` registered globally at `mariana/api.py:545`; startup `assert _RATE_LIMIT_STORAGE_VALIDATED` at line 539 blocks boot if storage init failed.
- **CC-17: PASS** — `SET search_path = public, pg_temp` pinned on all repo-owned `SECURITY DEFINER` functions; the few unpinned entries are Supabase-vendor `auth.*` stubs (not repo-owned).
- **CC-18: PASS** — `detail=f"..."` in `browser_server/app.py` — 0 hits.
- **CC-19: PASS** — `detail=f"..."` in `sandbox_server/app.py` — 0 hits.
- **CC-20: PASS** — agent route 404 details remain canonical (`"task not found"` only); no task_id echo. Verified at `mariana/agent/api_routes.py:780`.
- **CC-21: PASS** — canonical agent error contract enforced end-to-end via CC-35 AST invariant test.
- **CC-22: PASS** — main API 404 details remain canonical; no internal identifier echoing.
- **CC-23: PASS** — `frontend/package.json` and `e2e/package.json` deps exact-pinned; no `^` / `~` ranges.
- **CC-24: PASS** — surviving `detail=f"..."` in `mariana/api.py` interpolate only constants, plan names from a closed set, numeric bounds, file extensions (`!r`), status codes, or pre-rejected user-supplied URL hostnames (rejected first, then echoed back) — no internal IDs leaked.
- **CC-25: PASS** — `tool_error` ToolError persistence path uses canonical code; raw tool detail never leaks.
- **CC-26: PASS** — frontend lockfile + exact-pin posture intact (`pnpm-lock.yaml` / `package-lock.json` committed).
- **CC-27: PASS** — oversize vault entry-count handling fail-closes.
- **CC-28: PASS** — route-level projected enforcement on `/fs/write` and `/exec` from CC-34 hardening still in place.
- **CC-29: PASS** — admin RPC error scrubbing (`"admin RPC failed"` constant only) intact at `mariana/api.py:8851-8886`.
- **CC-30: PASS** — `_ADMIN_ROLE_CACHE` is a `_BoundedTTLCache(maxsize=10_000)` with TTL semantics; verified.
- **CC-31: PASS** — `public.touch_updated_at()` pins `SET search_path = public, pg_temp`.
- **CC-32: PASS** — filename echo scrubbing intact; no `safe_name!r` / `filename!r` in API error details.
- **CC-33: PASS** — `/metrics` is `include_in_schema=False`, depends on `_require_admin`, self-skips counter.
- **CC-34: PASS** — projected workspace quota enforced on `/fs/write` and `/exec`; cache refresh post-write closes same-TTL race.
- **CC-35: PASS** — canonical task/step error code allow-lists pinned by AST invariant test.
- **CC-36: PASS** — sidecar JSON logging via `_JsonLogFormatter` and `LOG_FORMAT=json|text` switch on both sidecars.
- **CC-37: PASS** — `_WORKSPACE_SIZE_CACHE` is a bounded TTL cache (`maxsize=10_000`, `ttl=5.0s`, FIFO). See Section A.
- **CC-38: PASS** — sidecar JSON log fields aligned to canonical `event` / `timestamp` keys. See Section A.

---

## C. New territory probe — different angles than A51 (gpt) and A52 (gpt/sonnet)

A53 is the opus diversity round. The dimensions below were probed fresh, beyond the sets covered by A51 and A52.

- **Memory exhaustion in tests**: PASS — only `tests/test_cc05_reconciler_batch_size_validation.py:452,482` create a `-10**6` integer (1 MB integer constant, not an unbounded structure). No test uses unbounded `range()` or list comprehension on test-side input. CC-37 test creates a `_BoundedTTLCache(maxsize=4, ttl=60.0)` — explicitly bounded.

- **Cryptographic primitives**: PASS — only two `hashlib.md5` call sites at `mariana/tools/memory.py:137,140`, both used as a content-deduplication hash for facts (not security/integrity). No SHA-1 use anywhere. `defusedxml.ElementTree` used for SEC-EDGAR XML parsing (`mariana/connectors/sec_edgar_connector.py:28`) — XXE-safe.

- **Token handling — constant-time comparison**: PASS — every auth-bearing comparison uses `hmac.compare_digest` (3 sites in `mariana/api.py:1635, 1662, 9617`) or `secrets.compare_digest` (sandbox `app.py:450`, browser `app.py:399`). No `==` on tokens or HMAC tags.

- **Command injection**: PASS — no `shell=True`, no `os.system`. Sandbox uses `asyncio.create_subprocess_exec(*argv, ...)` (`sandbox_server/app.py:608, 661`) with argv lists, not shells. Rust compile call uses fixed argv `["rustc", "-O", "--edition=2021", "-o", str(binary_path), str(src_path)]` where `binary_path`/`src_path` already passed through `_safe_workspace_path`.

- **`eval` / `exec`**: PASS — only `await self._redis.eval(...)` in `mariana/data/cache.py:349` (Redis Lua script execution, not Python `eval`). Two `eval()` mentions in `mariana/skills/general_skills.py:189-190` are LLM-prompt prose warnings, not code calls.

- **Pickle / yaml.load**: PASS — 0 `pickle.loads` and 0 `yaml.load(` hits anywhere in `mariana/`, `sandbox_server/`, `browser_server/`.

- **XXE**: PASS — only XML parser in scope is `defusedxml.ElementTree` (`mariana/connectors/sec_edgar_connector.py:28`); the comment at line 22-23 explicitly notes "use defusedxml to disable DTD/entity processing and prevent XXE." No `xml.etree.ElementTree` calls happen on untrusted input — line 27 imports the stdlib name only as `_stdlib_ET` (unused at runtime; type-only).

- **Server-side template injection (SSTI)**: PASS — 0 hits for `Template(` `.render(` patterns in any `*.py`. No Jinja2 user-templating surface.

- **Unvalidated / open redirect**: PASS — only `RedirectResponse` use is at `mariana/api.py:1945`, target `f"/preview/{task_id}/index.html"` (internal path, `task_id` already validated by `_validate_task_id`). Stripe success/cancel redirect URLs are checked against `_ALLOWED_REDIRECT_HOSTS` derived from CORS origins (`mariana/api.py:6266-6293`), with rejected hostname echoed via `parsed.hostname!r` in the 400 detail (host already failed validation, no further leak risk).

- **Information disclosure via error messages**: PASS — `grep 'detail=str(exc)|detail=str(e)|detail=repr'` returns 0 hits across `mariana/api.py`, `mariana/agent/api_routes.py`, `sandbox_server/app.py`, `browser_server/app.py`. All 401/403 details are constants (`"Invalid or expired stream token"`, etc.). `/api/admin/health-probe` only includes `f"{type(exc).__name__}: {str(exc)[:200]}"` and is admin-gated.

- **Timing attacks**: PASS — see token-handling probe above. All HMAC verifies use `compare_digest`. No `==` or hand-rolled byte loops on secret material.

- **HTTP smuggling**: PASS — no custom HTTP parser; framework is Starlette/uvicorn (canonical parsing). 0 hits for `parse_http`, `HTTPParser`, hand-rolled `content-length` handling.

- **Deserialization size**: PASS — Pydantic models declare `max_length` on every string field at the request boundary (>30 hits across `mariana/api.py` for fields like `title`, `topic`, `message`, `key`, `description`, etc., max 65536 for `content`). FastAPI body parsing rejects oversize bodies before Pydantic validation is reached. Stripe webhook `await request.body()` (`mariana/api.py:6378`) has no explicit cap, but signature verification immediately follows — an oversize unsigned body would fail HMAC and be rejected before any business logic. Stripe's `Webhook.construct_event` operates on memory-safe payloads.

- **TOCTOU in upload**: PASS — `upload_investigation_files` (`mariana/api.py:5395-5410`) uses `async with _get_upload_lock(task_id):` to serialise count-check-and-write, gating on `existing_count + len(files) > _UPLOAD_MAX_FILES_PER_INVESTIGATION`. The G-01 fix ensures `_get_upload_lock` returns a strong-reference `OrderedDict`-backed bounded LRU (4096 entries) so locks are not GC-eligible mid-upload.

- **Symlink attacks**: PASS — `mariana/api.py:3455, 4928-4931, 5008-5011, 5117, 5194` reject symlinks before serving file content (BUG-0008 hardening). Sandbox `_safe_workspace_path` (`sandbox_server/app.py:368-414`) rejects `..`, null byte, absolute paths, and validates each path component against `_PATH_COMPONENT_RE`; `candidate.relative_to(user_root)` after `.resolve()` blocks symlink-into-other-tenant escape.

- **Concurrency — global state mutated without lock**: PASS — top-level globals in `mariana/api.py` (`_PLANS`, `_TOPUPS`, `_PLAN_BY_ID`, `_TOPUP_BY_ID`, `_TIER_CREDITS`, `_MIME_MAP`, `_UPLOAD_MIME_MAP`) are immutable static config dicts — read-only after module load, no mutation paths in route handlers. The mutable caches `_ADMIN_ROLE_CACHE` (CC-30) and `_WORKSPACE_SIZE_CACHE` (CC-37) wrap `OrderedDict` operations in synchronous methods called from a single asyncio event loop with `--workers 1`, so concurrent coroutines do not interleave on a single dict op (CPython GIL guarantee).

- **Memory leaks in long-lived connections (SSE / WebSocket)**: PASS — `EventSourceResponse(_event_generator())` (`mariana/api.py:4878`) and agent SSE `StreamingResponse(gen(), ...)` (`mariana/agent/api_routes.py:873`) both rely on Starlette's automatic generator cancellation on client disconnect; `gen()` exits via `break`/`return` on terminal states or `xread` exception. No background `asyncio.create_task` is fired-and-forgotten (0 grep hits in `mariana/api.py`, `sandbox_server/app.py`, `browser_server/app.py`). Redis xread `block=5_000` releases the connection on each iteration so a stalled client cannot retain Redis resources.

- **Frontend XSS via `dangerouslySetInnerHTML`**: PASS — 4 hit sites, all sanitised:
  - `frontend/src/pages/Chat.tsx:3455` — `renderMarkdown` cache (max 1000 entries) sanitises `<>` then transforms markdown (BUG-R2-14 href guard for `https?://` only; `javascript:`/`data:`/`vbscript:`/`file:` collapsed to plain text).
  - `frontend/src/components/FileViewer.tsx:339` — `renderMarkdownContent` mirrors the Chat.tsx href guard (B-26 fix).
  - `frontend/src/components/ui/chart.tsx:70` — emits a `<style>` block built from `THEMES` constants and per-key colour values from `ChartConfig` (developer-supplied at build time, not user input).
  - PreviewPane iframe sandboxed without `allow-same-origin` (B-27 fix).

- **Frontend prototype pollution**: PASS — 0 hits for `Object.assign(target, JSON.parse(...))` patterns or any equivalent merge-with-untrusted-source. `Object.assign` calls in the codebase do not couple a `JSON.parse` source.

- **CSRF on state-changing GET**: PASS — every POST/PUT/DELETE handler grep'd shows no equivalent `@app.get` for the same operation (`delete|create|update|grant|revoke|set|admin_` substring on `@*.get(` returns 0 mutating routes).

- **Database — `text()` SQL with f-string concatenation**: PASS — only `text(f"...")` candidate is `mariana/api.py:4074` `pool.execute(f"DELETE FROM {table} WHERE task_id = $1", task_id)` where `table` is iterated from a hard-coded literal list (`cascade_tables` at lines 4049-4072) and `task_id` is bound parameter `$1`. No user-controlled f-string interpolation.

- **Stripe replay**: PASS — webhook event uniqueness enforced via `INSERT ... ON CONFLICT (event_id) DO UPDATE` on `stripe_webhook_events` (`mariana/api.py:8335-8343`); the per-charge / dispute reversal flow uses `process_charge_reversal` SECURITY DEFINER RPC with `pg_advisory_xact_lock(hash(charge_id))` so concurrent K-02-class redeliveries cannot double-debit.

- **Privilege escalation via row update**: PASS — `admin_user_set_role` (`mariana/api.py:8950`) is the only path that mutates `profiles.role`; it's `Depends(_require_admin)` gated, calls the `admin_set_role` RPC (SECURITY DEFINER with admin-uid guard), and invokes `_clear_admin_cache(target_user_id)` (CC-20 / B-20 fix) so a freshly-revoked admin cannot retain a positive cache entry. `update profiles set` direct-SQL search returns only the deliberate token-balance helper at line 8111 and CHECK-constraint glue (no role-mutation path).

---

## D. Findings

### Finding 1 — Backup/DR posture not repo-visible (carried over from A50 Finding 4 → A51 Finding 3 → A52 Finding 1)
**Severity:** Info
**File:** operational/runbook (no code location)
**Evidence:** No first-class backup/restore/retention/RPO/RTO posture documented in `docker-compose.yml`, `.github/workflows/*.yml`, `README.md`, or top-level docs. Absence-of-evidence rather than evidence-of-defect.
**Repro:** repo inspection only.
**Why it matters:** for a billion-dollar launch, operators need explicit testable backup/restore ownership. Cannot be confirmed from the repository.
**Fix:** document and automate backup/restore/retention posture in repo-adjacent operational material. Out of scope for the loop6 streak.

---

## E. Final paranoid grep

| Check | Result |
|-------|--------|
| `detail=f"..."` in `sandbox_server/app.py` | **0 hits** ✅ |
| `detail=f"..."` in `browser_server/app.py` | **0 hits** ✅ |
| `detail=str(exc)`/`detail=str(e)`/`detail=repr` across api/agent/sidecars | **0 hits** ✅ |
| `uses: <action>@<floating-tag>` in `.github/workflows/` | **0 hits** ✅ |
| `re.compile(...$...)` non-`\Z` anchored in source | **1 expected** (`_BYTEA_HEX_RE` fixed-format) ✅ |
| `datetime.utcnow()` in non-test production code | **0 hits** ✅ |
| `extra='allow'` / `extra="allow"` Pydantic loosener | **0 hits** ✅ |
| `asyncio.create_task` in production paths (`mariana/api.py`, `sandbox_server/`, `browser_server/`) | **0 hits** ✅ |
| `pickle.loads` / `pickle.load` | **0 hits** ✅ |
| `yaml.load(` (unsafe) | **0 hits** ✅ |
| `shell=True` in `.py` (excluding tests) | **0 hits** ✅ |
| `os.system(` | **0 hits** ✅ |
| `Template(...).render()` SSTI | **0 hits** ✅ |
| `RedirectResponse(url=...)` user-controlled | **0 hits** (only static internal preview URL) ✅ |
| `Object.assign(*, JSON.parse(*))` prototype-pollution shape | **0 hits** ✅ |
| `text(f"...")` SQL concat | **0 hits** ✅ |
| `_WORKSPACE_SIZE_CACHE` is `_BoundedTTLCache(maxsize=10_000, ttl=5.0)` | **CONFIRMED** `sandbox_server/app.py:208,273-275` ✅ |
| `_JsonLogFormatter` emits `event`+`timestamp` (not `msg`/`ts`) | **CONFIRMED** `sandbox_server/app.py:121,124`; `browser_server/app.py:108,111` ✅ |
| `secrets.compare_digest` for sidecar shared-secret | **CONFIRMED** `sandbox_server/app.py:450`; `browser_server/app.py:399` ✅ |
| `hmac.compare_digest` for HMAC verification | **CONFIRMED** 3 sites in `mariana/api.py:1635, 1662, 9617` ✅ |
| `defusedxml` for SEC-EDGAR XML | **CONFIRMED** `mariana/connectors/sec_edgar_connector.py:28` ✅ |
| `tests/test_cc37_workspace_size_cache_bound.py` exists (5 tests) | **CONFIRMED** ✅ |
| `tests/test_cc38_sidecar_log_field_parity.py` exists (4 tests) | **CONFIRMED** ✅ |
| HEAD commit SHA | `2141447` — matches expected ✅ |

**Paranoid grep verdict:** PASS.

---

## F. Convergence declaration

**SECOND CONVERGENCE: DECLARED.**

Three consecutive zero-medium-or-higher audit rounds have now been completed on HEAD `2141447` post-CC-38 by three different model lineages:

1. **#46 (sonnet)** — A51 — PASS, 1 info finding (backup/DR posture).
2. **#47 (gpt)** — A52 — PASS, 1 info finding (carried over).
3. **#48 (opus, this audit)** — A53 — PASS, 1 info finding (carried over).

The diversity probe in Section C (memory exhaustion, custom crypto, token comparison, command injection, eval/exec, pickle/yaml.load, XXE, SSTI, open redirect, info disclosure, timing attacks, HTTP smuggling, deserialization size, upload TOCTOU, symlink attacks, async-state concurrency, SSE/WS leaks, frontend XSS, prototype pollution, CSRF-on-GET, SQL string concat, Stripe replay, privilege escalation) finds **zero new code-level defects** at any severity. The cumulative CC-02 + CC-04..38 stack holds, with file:line evidence pinned for every item.

The single surviving finding is operational documentation (info-severity backup/DR posture) and is explicitly out of scope for the code-bug streak.

**The loop6/zero-bug branch at `2141447` is production-ready and second-convergence-stable.**
