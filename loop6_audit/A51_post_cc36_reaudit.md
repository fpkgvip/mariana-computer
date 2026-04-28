# A51 Re-audit #46 (opus) post-CC-36 — streak round 1/3 candidate

**Branch:** `loop6/zero-bug`
**HEAD:** `9ee57f2` (`9ee57f251e1db029e838763d46fac481532b8d05`)
**Cumulative range audited:** `c108b1e..9ee57f2`
**Baseline provided by user:** 573 pytest pass / 11 skipped / 0 failed; 144 vitest pass
**Mode:** read-only static audit; no tests run, no source changes

---

## One-line verdict
CC-34, CC-35, and CC-36 all hold; the cumulative CC-02..CC-36 stack still holds; **no medium-or-higher findings surfaced**. **CONVERGENCE STREAK 1/3 ACHIEVED.**

## Findings count + severities
- **3 findings total**
- **0 critical / 0 high / 0 medium**
- **2 low**
- **1 info**

## Production-ready Y/N
**Y** for the zero-bug / billion-dollar bar at this moment. The two low and one info findings are observability/operational caveats rather than correctness or security defects, and none invalidate the CC-34..36 fixes or any prior CC-XX hold.

---

## 1. CC-34..36 verification

### CC-34 — workspace quota projection — **PASS**
**Evidence:**
- `sandbox_server/app.py:222-252` defines `_enforce_workspace_quota(workspace_root, additional_bytes=0)` that computes `projected = current + additional_bytes` and raises `HTTPException(507, "workspace_full")` when projected exceeds `_MAX_WORKSPACE_BYTES`.
- `sandbox_server/app.py:769-772` — `/exec` invokes the helper with `additional_bytes=len(req.code.encode("utf-8"))` for the source bytes that will be written immediately.
- `sandbox_server/app.py:950-1006` — `/fs/write` decodes the base64 (or measures the utf-8 string) BEFORE the quota check, computes `delta = max(additional_bytes - existing, 0)` so an overwrite that shrinks does not trip the projection, calls `_enforce_workspace_quota(workspace_root, additional_bytes=delta)`, and after a successful write calls `_workspace_size_cache_set(workspace_root, pre_size + delta)` to invalidate/refresh the cache and avoid a stale-cache double-pass on a tight boundary.
- `tests/test_cc34_workspace_quota_projection.py` exists and exercises 6 cases (under-cap success, projected-over-cap rejection, oversized `/exec` source rejection, shrinking overwrite passes, cache refresh prevents back-to-back race, and the helper's `additional_bytes=0` path).

### CC-35 — canonical agent error code contract — **PASS**
**Evidence:**
- `mariana/agent/loop.py:118-141` defines `CANONICAL_TASK_ERROR_CODES` and `CANONICAL_STEP_ERROR_CODES` as `frozenset[str]` constants matching the documented set: `{stop_requested, budget_exhausted, duration_exhausted, planner_failed, deliver_failed, unrecoverable, vault_unavailable, vault_transport_violation, loop_crash}` and `{tool_error, unexpected, timed_out, process_killed, non_zero_exit, http_error}`.
- `mariana/agent/loop.py:433-462` — `_budget_exceeded` returns `(over, code, detail)` with canonical codes only (`"budget_exhausted"` / `"duration_exhausted"`); structured numeric context lives in the returned dict for the SSE payload + structured server log.
- `mariana/agent/loop.py:1005-1050` — `_infer_failure` returns `"timed_out"` / `"process_killed"` / `"non_zero_exit"` / `"http_error"` only; numeric context goes to `logger.info(...)` `extra=` only.
- I AST-walked `mariana/agent/loop.py` and `mariana/agent/api_routes.py` myself: every literal `task.error =` / `terminal_task.error =` / `step.error =` site assigns either `None`, a canonical literal, or a Name flowing from `_budget_exceeded` / `_infer_failure` (`step.error = soft_err` at 1186; `task.error = code` at 1595 — both of which are guaranteed canonical at the source).
- I also grepped for the `setattr` bypass (`setattr(task, "error", x)` etc.) — no hits.
- `tests/test_cc35_canonical_error_codes.py` enforces the AST-walked invariant and treats f-strings / BinOps as violations.

### CC-36 — sidecar JSON structured logging — **PASS**
**Evidence:**
- `sandbox_server/app.py:102-159` and `browser_server/app.py:89-141` each expose `_JsonLogFormatter` (a `logging.Formatter` subclass that emits one valid JSON object per record) and `_configure_logging()` which wires the root logger based on `LOG_FORMAT=json|text`, default `json`.
- The formatter emits `ts`, `level`, `logger`, `msg`, surfaces caller `extra=` keys, serialises `exc_info` / `stack_info`, and coerces non-JSON-serialisable extras via `repr` so it never crashes.
- `_configure_logging()` removes any pre-existing root handlers before installing the new one, so `importlib.reload`-driven test runs don't double-emit.
- `tests/test_cc36_sidecar_json_logging.py` exists and parametrises over `["sandbox_server.app", "browser_server.app"]` with a `find_spec("playwright")` skip on the browser case (playwright is sidecar-only).

---

## 2. Cumulative review of CC-02 + CC-04..36

One bullet each, confirming current hold status:

- **CC-02 — PASS:** reconciler limit holds; no regression signal.
- **CC-04 — PASS:** vault read path fail-closes for malformed/missing/invalid payloads under `requires_vault=True` (`mariana/vault/runtime.py:179-340`).
- **CC-05 — PASS:** reconciler/config validation continues to hold.
- **CC-06 — PASS:** vault empty-object payload still treated as fail-closed under `requires_vault=True`.
- **CC-07 — PASS:** frontend hero-copy scan against the changed pages found no forbidden hype-language regression.
- **CC-08 — PASS:** admin observability remains admin-gated (every `/api/admin/*` route depends on `_require_admin`; AST-walk verified for routes at `mariana/api.py:8917-9357`).
- **CC-09 — PASS:** vault runtime contract alignment for key/value validation holds.
- **CC-10 — PASS:** sibling validators use `\Z` anchors (`sandbox_server/app.py:274-275,416`, `mariana/api.py:1866`, `mariana/connectors/sec_edgar_connector.py:236`, `mariana/tools/memory.py:33`, `mariana/vault/runtime.py:119`, `mariana/vault/store.py:138`); only legitimate `$` anchor is `_BYTEA_HEX_RE` in `mariana/vault/store.py:63` documented as bytea-fixed-format.
- **CC-11 — PASS:** oversize vault value still fail-closes under `requires_vault=True`.
- **CC-12 — PASS:** every `uses:` in `.github/workflows/{ci,deploy}.yml` is SHA-pinned (e.g. `actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1`); no floating tag (`@v4`, `@main`, `@master`, `@latest`) hits.
- **CC-13 — PASS:** both `ci.yml` and `deploy.yml` carry top-level `permissions: contents: read`.
- **CC-14 — PASS:** no secret-scan regression surfaced; no hardcoded `sk_(live|test)_...` / `AKIA...` / `ghp_...` / `xox[bapr]-...` patterns leaked into source.
- **CC-15 — PASS:** deploy concurrency hardening intact.
- **CC-16 — PASS:** rate limiting via `slowapi` is a hard dependency (`mariana/api.py:78-87`), `SlowAPIMiddleware` wired globally at `mariana/api.py:545`, and a startup assertion (`_RATE_LIMIT_STORAGE_VALIDATED`) refuses to boot without it.
- **CC-17 — PASS:** SQL function `search_path` pinning holds repo-wide; my own scan of every `CREATE [OR REPLACE] FUNCTION` block in `frontend/supabase/migrations/*.sql` and `.github/scripts/ci_full_baseline.sql` (excluding revert files) found only two unpinned blocks — `auth.role()` and `auth.uid()` — both Supabase-built-in stubs in the `auth` schema, not repo-owned `public` schema.
- **CC-18 — PASS:** `detail=f"..."` in `browser_server/**/*.py` returns zero hits.
- **CC-19 — PASS:** `detail=f"..."` in `sandbox_server/**/*.py` returns zero hits.
- **CC-20 — PASS:** agent route 404 details remain generic (`"task not found"`).
- **CC-21 — PASS:** canonical agent error contract is now enforced end-to-end via CC-35; non-canonical `task.error`/`step.error` writes are forbidden by AST invariant test.
- **CC-22 — PASS:** main API 404 details remain canonical; no `task_id!r` / `plan_id!r` / `skill_id!r` / `filename!r` / `safe_name!r` / `user_id!r` interpolation in 404-emitting code.
- **CC-23 — PASS:** every entry in `frontend/package.json` (74 deps) and `e2e/package.json` (1 dep) is exact-pinned; no `^`/`~` anywhere.
- **CC-24 — PASS:** main API 404 detail scrubs hold; the surviving `detail=f"..."` sites in `mariana/api.py` only interpolate constants, plan names, numeric bounds, file extensions, status codes, and user-supplied URL hostnames — no internal identifiers leaked.
- **CC-25 — PASS:** ToolError persistence still uses canonical `tool_error`; no raw tool detail leak.
- **CC-26 — PASS:** lockfile / package exact-pinning posture intact.
- **CC-27 — PASS:** oversize vault entry-count handling fail-closes (no silent slicing).
- **CC-28 — PASS (with CC-34's hardening):** route-level projected enforcement now in place.
- **CC-29 — PASS:** admin RPC error scrubbing (`"admin RPC failed"` only) intact at `mariana/api.py:8851-8886`.
- **CC-30 — PASS:** `_ADMIN_ROLE_CACHE` remains a `_BoundedTTLCache(maxsize=10_000)`.
- **CC-31 — PASS:** `public.touch_updated_at()` pins `SET search_path = public, pg_temp` (`frontend/supabase/migrations/003_deft_vault.sql:166-174`).
- **CC-32 — PASS:** filename echo scrub intact; no `safe_name!r` / `filename!r` in the relevant API error details.
- **CC-33 — PASS:** `/metrics` is `include_in_schema=False`, depends on `_require_admin`, and self-skips its own counter (`mariana/api.py:2189,2209-2236`).
- **CC-34 — PASS:** projected workspace size enforced on `/fs/write` (binary + text base64 byte size, with overwrite-shrink awareness) and `/exec` (source code size); cache refresh post-write closes the same-TTL race.
- **CC-35 — PASS:** canonical task/step error code allow-lists pinned by AST invariant test; `_budget_exceeded` and `_infer_failure` return canonical codes only.
- **CC-36 — PASS:** sidecar JSON logging via `_JsonLogFormatter` and `LOG_FORMAT=json|text` (default `json`) on both sandbox and browser sidecars.

---

## 3. New territory probe

Every dimension below was checked.

### CC-35 completeness — **PASS**
- I AST-walked `mariana/agent/loop.py` and `mariana/agent/api_routes.py` independently and found 16 `*.error =` write sites (`mariana/agent/loop.py:1061,1100,1126,1186,1423,1438,1455,1534,1552,1578,1595,1639,1661,1710,1735` + `mariana/agent/api_routes.py:979`).
- 14 are literal canonical strings, 1 is `step.error = None` (clearing — allowed), and 2 are Name nodes (`step.error = soft_err`, `task.error = code`) flowing from `_infer_failure` and `_budget_exceeded` respectively, both of which are pinned canonical at source.
- Repo-wide grep for `(task|step|terminal_task)\.error\s*=` outside the audited files — zero hits, so no other module bypasses the invariant.
- `setattr` bypass grep — zero hits.

### CC-34 completeness — **PASS**
- Sandbox routes are exhaustively enumerated: `@app.get("/health")`, `@app.post("/exec")`, `@app.post("/fs/read")`, `@app.post("/fs/write")`, `@app.post("/fs/list")`, `@app.post("/fs/delete")`. There is no `/fs/copy`, `/fs/mkdir`, `/fs/move`, `/fs/upload`, multipart, or websocket write path. The two write paths (`/fs/write`, `/exec`) are both quota-projected.
- `browser_server/app.py` exposes no FS write paths; it is a Playwright wrapper.
- The `mariana/api.py:5495+` `/api/upload` and `/api/investigations/{task_id}/upload` flows write to the host's `DATA_ROOT/uploads/...` — NOT the sandbox workspace — so they are out of scope for sandbox quota. They have per-file size caps (`_UPLOAD_MAX_FILE_SIZE`) and per-session count caps (`_UPLOAD_MAX_FILES_PER_INVESTIGATION`) but no aggregate disk quota at the orchestrator layer; that is a separate operational concern (filesystem-level quota / volume sizing) and not part of the CC-34 contract.

### CC-36 completeness — **PASS with low caveat (Finding 2)**
- Both sidecar JSON formatters emit the same field schema (`ts`, `level`, `logger`, `msg` + caller `extra=` keys + `exc_info` / `stack_info`).
- Compared against the orchestrator (`mariana/main.py:76-81`), which uses structlog's `JSONRenderer()` with `TimeStamper(fmt="iso")`, the field names diverge: structlog emits `event` and `timestamp`, sidecars emit `msg` and `ts`. This is acknowledged in `loop6_audit/CC34_CC36_FIX_REPORT.md:102-106`. Documented and intentional, but it is a real cross-service field-schema mismatch that operators must normalise at the aggregator. Recorded as **Finding 2 (Low)**.

### Concurrency — **PASS**
- The CC-34 cache update (`_workspace_size_bytes` → `_enforce_workspace_quota` → `p.write_*` → `_workspace_size_cache_set`) is bounded inside a single `async def` with no `await` between the read and the write. The Python asyncio event loop guarantees no other coroutine can interleave during that window. Combined with single-process sandbox containers (`Dockerfile.sandbox:102-103` runs `uvicorn ... --workers 1`), there is no realistic TOCTOU race on the quota.
- Multi-process or multi-container sandbox would re-introduce a stale-cache race; current deployment topology does not.

### Auth — **PASS**
- All `/api/admin/*` routes depend on `_require_admin` (verified by walking the route table; 6 routes, all pass).
- All token comparisons use constant-time primitives: `mariana/api.py:1635,1662,9617` use `hmac.compare_digest`; `sandbox_server/app.py:364` and `browser_server/app.py:392` use `secrets.compare_digest`. No `==` based comparison on any secret/token.

### SSRF — **PASS**
- `browser_server/app.py:201-203,260-287` blocks loopback / link-local / multicast / reserved / private IPs and resolves DNS with reject-on-private-resolution.
- `mariana/connectors/base.py:50-131` enforces both initial-URL and redirect-target IP categories (private / loopback / link-local).
- No outbound webhook delivery code in the orchestrator (no `webhook_url` / `notify_url` post sites), so there is no separate webhook SSRF surface to harden.

### Stripe — **PASS**
- `mariana/api.py:6375-6420` requires `STRIPE_WEBHOOK_SECRET_PRIMARY` (or legacy `STRIPE_WEBHOOK_SECRET`) and refuses webhooks with HTTP 503 when no secret is configured.
- Dual-secret rotation overlap supported via `STRIPE_WEBHOOK_SECRET_PREVIOUS`; success on the previous secret logs at `WARNING` so operators see the rotation window is open.
- `_claim_webhook_event` (`mariana/api.py:6433-6526`, `8295-8418`) implements two-phase idempotency with `pending` → `done` row transitions; per-grant `ref_id` idempotency on settlement.

### Sandbox container hardening — **PASS**
- `docker-compose.yml` keeps the sandbox on an `internal: true` network (no Internet egress), with `read_only: true`, `no-new-privileges:true`, `cap_drop: ALL`, `tmpfs` scratch, and only narrowly restored capabilities. Sidecar Dockerfile pins `--workers 1`.

### Time / timezone — **PASS**
- Every `datetime.now(...)` / `datetime.utcnow()` in `mariana/`, `sandbox_server/`, `browser_server/` is timezone-aware (`tz=timezone.utc` or `timezone.utc` argument). No naive `datetime.utcnow()` regression.

### Resource leaks — **PASS with one low caveat (Finding 1)**
- Postgres pool is bounded by `POSTGRES_POOL_MIN/MAX` config (`mariana/api.py:405-406`, `mariana/main.py:241-250`).
- Module-level `dict` caches (`_TASK_FRAMEWORK_CACHE`, `_PLAN_BY_ID`, `_TIER_CREDITS`, `_MIME_MAP`, `_TIER_ROUTING`, `_TASK_CATEGORY`, `_MODEL_PRICING`) are bounded by enum / config keys.
- `_ADMIN_ROLE_CACHE` is a bounded TTL cache (`maxsize=10_000`).
- **`_WORKSPACE_SIZE_CACHE` in `sandbox_server/app.py:193` is unbounded by user_id key.** It only ever grows; entries are refreshed in place but never evicted. With many distinct users on the same sandbox container, the dict grows monotonically. This is **Finding 1 (Low)**.

### Pydantic mass-assignment — **PASS**
- All Pydantic models in `mariana/agent/models.py`, `mariana/billing/router.py`, `mariana/vault/router.py` use `ConfigDict(extra="forbid")`.
- Repo-wide grep for `extra="allow"` / `extra='allow'` returns zero hits.

### Frontend hero-copy — **PASS**
- Grep over `frontend/src/**/*.tsx` for `world-class` / `revolutionary` / `best-in-class` / `guaranteed` / `military-grade` / `bank-grade` / `unmatched` / `unbeatable` returns zero hits. The remaining `unlimited` matches in `frontend/src/components/deft/PreflightCard.tsx` are admin-ceiling logic flags, not hero copy.

### Secrets — **PASS**
- No `sk_(live|test)_...` / `AKIA...` / `ghp_...` / `xox[bapr]-...` literals.
- No `EXAMPLE` / `YOUR_KEY_HERE` / `change.?me` / `placeholder.?key` leaked.

### Idempotency — **PASS within scope**
- Stripe webhook events: two-phase idempotency on `event_id` plus per-grant `ref_id`.
- Agent settlements: row-level INSERT-ON-CONFLICT-DO-NOTHING claim at `mariana/agent/loop.py:470-491`.
- Mutating user-facing POSTs (e.g. `/api/investigations`) do not require an `Idempotency-Key` header. That is a product-design choice rather than a security defect — the failure mode of a duplicate POST is at most a duplicate investigation, not a billing or auth defect, and is recoverable.

### Rate limiting — **PASS**
- Global `SlowAPIMiddleware` at `mariana/api.py:545` covers every endpoint without per-route opt-out.
- Auth endpoints get the stricter `_AUTH_RATE_LIMIT = 20/60s`; default is `_DEFAULT_RATE_LIMIT = 60/60s`.

---

## 4. Production-readiness gaps

### Logging / PII redaction — **PASS**
- Vault redaction (`mariana/vault/redaction.py`) replaces every plaintext occurrence of a vault value with `[REDACTED:KEY_NAME]` before logging, streaming, or persisting.
- `redact_payload` is wired into the agent emit path (`mariana/agent/loop.py:312`) before SSE / log emission.

### Backup / DR — **INFO (Finding 3, carried over from A50 Finding 4)**
- I did not find first-class backup/restore/retention operational evidence in `docker-compose.yml`, the GitHub workflows, or top-level docs. This is a carried-over visibility gap.

### Health checks — **INFO (note only)**
- `mariana/api.py:2157-2160` `/api/health` is a pure liveness probe (always 200 if the process is up). There is no separate readiness probe that checks DB / Redis / connectivity. For Kubernetes-style deployments the lack of a readiness/liveness split can cause traffic to be sent to a process whose dependencies are not yet up. Recorded as a note rather than a finding because the existing probe's contract is documented and the operator can build readiness checks externally.

### Graceful shutdown — **PASS for orchestrator; note for sandbox**
- `mariana/api.py:382-448` runs a lifespan context that closes infra cleanly on shutdown.
- `browser_server/app.py:331-381` has its own lifespan.
- `sandbox_server/app.py` has no `lifespan` / `on_event("shutdown")` handler. SIGTERM terminates in-flight `/exec` subprocesses without an explicit drain. For sandbox semantics (every request is a sandboxed subprocess with a wall-clock timeout) this is materially safe — the subprocess is reaped by the OS at container teardown — but it is worth noting for completeness. Not elevated to a finding.

---

## 5. Findings

### Finding 1 — `_WORKSPACE_SIZE_CACHE` is unbounded by user_id key
**Severity:** Low
**Evidence:**
- `sandbox_server/app.py:193` — `_WORKSPACE_SIZE_CACHE: dict[str, tuple[float, int]] = {}`.
- `_workspace_size_bytes` (`sandbox_server/app.py:218`) and `_workspace_size_cache_set` (`sandbox_server/app.py:263`) write to this dict keyed by workspace path string (which embeds the user_id), and never evict.

**Repro:** drive `/fs/write` from N distinct user_ids over the lifetime of a sandbox container; the dict size grows linearly in N and never shrinks.

**Why it matters:** for short-lived single-tenant sandbox containers (the current deployment topology) the upper bound is small and operationally irrelevant. For longer-lived multi-tenant sandboxes the dict would grow without bound, eventually contributing to RSS pressure. Bounded user_id length (≤128 chars per `_SAFE_ID_RE`) caps per-entry size at ~few hundred bytes, so the realistic ceiling is on the order of a few hundred MB at 1M distinct users — not an immediate availability risk, but a quiet memory leak.

**Recommended fix:** swap `_WORKSPACE_SIZE_CACHE` for the same `_BoundedTTLCache` primitive used by `_ADMIN_ROLE_CACHE` (or any small bounded LRU/TTL container), with a generous `maxsize` (e.g. 50k) and the existing `_WORKSPACE_SIZE_TTL_SEC = 5.0` TTL.

### Finding 2 — sidecar JSON log field names diverge from the orchestrator's structlog schema
**Severity:** Low
**Evidence:**
- `sandbox_server/app.py:113-118` and `browser_server/app.py:100-105` emit `{"ts", "level", "logger", "msg", ...}`.
- `mariana/main.py:76-81` configures structlog with `TimeStamper(fmt="iso")` + `JSONRenderer()`, which emits `{"event", "timestamp", "level", "logger", ...}`.

**Repro:** ingest sidecar and orchestrator logs into the same aggregator with the same schema; sidecar records appear under `msg`/`ts` while orchestrator records appear under `event`/`timestamp`. Filters built for one set will not match the other.

**Why it matters:** weakens cross-service log correlation and incident-response consistency. Already explicitly acknowledged in `loop6_audit/CC34_CC36_FIX_REPORT.md:102-106` as an intentional independence trade-off, but it is a real operator-visible mismatch that should at least be documented in the operator runbook (or aligned).

**Recommended fix:** rename the two sidecar JSON keys to `event` (in place of `msg`) and `timestamp` (in place of `ts`), so all three services emit the same canonical field schema. This stays self-contained inside each sidecar — no structlog dependency required.

### Finding 3 — no repo-visible backup / restore / retention operating posture
**Severity:** Info (carried over from A50 Finding 4)
**Evidence:**
- Broad searches over `docker-compose.yml`, `.github/workflows/*.yml`, `README.md`, and top-level docs surface no first-class backup / restore / retention / RPO / RTO posture.

**Repro:** repo inspection only; absence-of-evidence rather than evidence-of-defect.

**Why it matters:** for a billion-dollar launch, operators normally need explicit testable backup/restore ownership and retention guarantees. Cannot be confirmed from the repository itself.

**Recommended fix:** document and automate backup / restore / retention posture in repo-adjacent operational material, or surface the relevant deployment/runbook references from this codebase.

---

## 6. Final paranoid grep

### Forbidden backend / frontend patterns
- `detail=f"..."` in `sandbox_server/**/*.py` — **0 hits** ✅
- `detail=f"..."` in `browser_server/**/*.py` — **0 hits** ✅
- Identifier echo in `mariana/api.py` 404s (`task_id!r` / `plan_id!r` / `skill_id!r` / `filename!r` / `safe_name!r` / `user_id!r` interpolated into HTTPException(404, …)) — **0 hits** ✅
- Surviving `detail=f"..."` in `mariana/api.py` (15 hits) reviewed individually: each interpolates only constants, plan names, numeric bounds, file extensions, status codes, or user-supplied URL hostnames the user already owns — none echo internal identifiers. ✅
- Non-test regex `$` anchors in production code — only `_BYTEA_HEX_RE` in `mariana/vault/store.py:63` (legitimate bytea fixed-format parser). All sibling identifier validators use `\Z`. ✅
- Workflow floating tags (`@v[0-9]` / `@main` / `@master` / `@latest`) — **0 hits**; every `uses:` in `.github/workflows/*.yml` is SHA-pinned. ✅
- `extra="allow"` on Pydantic models — **0 hits** ✅
- `setattr(*, 'error', *)` bypass for CC-35 — **0 hits** ✅
- `(task|step|terminal_task)\.error =` outside `mariana/agent/loop.py` and `mariana/agent/api_routes.py` — **0 hits** ✅
- Naive `datetime.utcnow()` / `datetime.now()` (no tz) — **0 hits** ✅
- Plaintext token comparisons (`token ==` / `secret ==` / `api_key ==`) on secrets — **0 hits**; every comparison uses `hmac.compare_digest` or `secrets.compare_digest` ✅
- Frontend `^` / `~` semver ranges in `frontend/package.json` and `e2e/package.json` — **0 hits** ✅
- Hardcoded secret patterns (`sk_(live|test)_...`, `AKIA...`, `ghp_...`, `xox[bapr]-...`) — **0 hits** ✅
- Forbidden hero adjectives / verbs (`world-class` / `revolutionary` / `best-in-class` / `guaranteed` / `military-grade` / `bank-grade` / `unmatched` / `unbeatable`) — **0 hits** ✅

### Frontend hero-copy spot-check
- `frontend/src/pages/Index.tsx`, `Product.tsx`, `Research.tsx`, `Skills.tsx`, `Chat.tsx` — copy remains assertive but product-descriptive; no forbidden hype.

**Paranoid grep verdict:** PASS.

---

## Final verdict
This re-audit is **clean for the medium-and-above bar**. CC-34, CC-35, and CC-36 all hold; the cumulative CC-02..CC-36 stack still holds; only two low-severity observability/operational caveats and one info-severity carried-over backup/DR gap remain.

**CONVERGENCE STREAK 1/3 ACHIEVED.**
