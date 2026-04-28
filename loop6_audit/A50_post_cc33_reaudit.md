# A50 Re-audit #45 (streak round 1/3) post-CC-33

**Branch:** `loop6/zero-bug`  
**HEAD:** `a5c6de1` (`a5c6de1636af48b22bb395d40dfbc75e67b205de`)  
**Cumulative range audited:** `c108b1e..a5c6de1`  
**Baseline provided by user:** 547 pytest pass / 11 skipped / 0 failed; 144 vitest pass  
**Mode:** read-only static audit; no tests run, no source changes

---

## One-line verdict
CC-27, CC-29, CC-30, CC-31, CC-32, and CC-33 hold; **CC-28 does not fully hold**, and the audit found one additional medium-severity agent error-contract regression. **CONVERGENCE STREAK does NOT advance to 1/3.**

## Findings count + severities
- **4 findings total**
- **0 critical / 0 high**
- **2 medium**
- **1 low**
- **1 info**

## Production-ready Y/N
**N** for the stated zero-bug / billion-dollar-launch bar.

The codebase is close, but the workspace quota fix is not route-complete in the way the fix report claims, and the agent still persists multiple non-canonical task/step error strings despite the documented stable error-code contract.

---

## 1. CC-27..CC-33 verification

### CC-27 — vault entry-count silent truncation — **PASS**
**Evidence:** `mariana/vault/runtime.py` now checks oversize entry count before iteration and fail-closes under `requires_vault=True` instead of slicing silently. The old `list(data.items())[:_MAX_VAULT_ENV_ENTRIES]` pattern is gone. `tests/test_cc27_vault_oversize_entries.py` contains the intended async regression coverage.

### CC-28 — workspace disk quota — **FAIL**
**Evidence:**
- `sandbox_server/app.py:131-144` defines `_enforce_workspace_quota(workspace_root)` with **no** `additional_bytes` or projected-size parameter.
- `sandbox_server/app.py:643-645` calls the helper before `/exec`, but only against the **current** workspace size.
- `sandbox_server/app.py:823-850` calls the helper before `/fs/write`, but again only against the **current** workspace size; the decoded payload size is not added before the write.
- `tests/test_cc28_workspace_quota.py` only exercises helper behavior / env override / boundary conditions; it does not pin projected-over-limit route behavior.

**Why this fails the hold:** the fix report described `_enforce_workspace_quota(user_id, additional_bytes=0)` and specifically claimed projected post-write enforcement. The implementation under review only blocks already-over-quota workspaces, so a workspace just under cap can still be pushed over cap by a single `/fs/write` or `/exec` artifact burst.

### CC-29 — admin RPC error response scrubbing — **PASS**
**Evidence:** `_admin_rpc_call` in `mariana/api.py:8851-8886` logs upstream details server-side and raises the stable detail `"admin RPC failed"`. Targeted grep found no remaining admin-handler `detail=str(...)` / interpolated upstream-body leaks in the audited admin RPC surfaces.

### CC-30 — bounded admin role cache — **PASS**
**Evidence:** `mariana/api.py` uses `_BoundedTTLCache` with `_ADMIN_ROLE_CACHE_MAX_ENTRIES = 10_000`, and `tests/test_cc30_admin_role_cache_bound.py` covers bound, FIFO eviction, TTL eviction, pop, clear, and type sanity.

### CC-31 — universal search_path pinning — **PASS**
**Evidence:** `frontend/supabase/migrations/003_deft_vault.sql` now pins `SET search_path = public, pg_temp` on `public.touch_updated_at()`, and the extended `tests/test_cc17_security_definer_search_path.py` scans all repo-owned `CREATE FUNCTION` blocks. A sweep of SQL function definitions found no repo-owned missing `search_path` cases.

### CC-32 — filename echo scrub — **PASS**
**Evidence:** `mariana/api.py` now uses canonical details `"invalid filename"` and `"symlinks are not allowed"`; targeted grep found no remaining `filename!r` / `safe_name!r` interpolation in the relevant API error details. `tests/test_cc32_filename_echo_scrub.py` pins the invariant.

### CC-33 — `/metrics` endpoint with admin auth gating — **PASS**
**Evidence:** `mariana/api.py` contains metrics middleware and an admin-gated `/metrics` route excluded from schema generation; `tests/test_cc33_metrics_endpoint.py` covers auth rejection, success, content type, metric presence, OpenAPI exclusion, and no self-instrumentation.

---

## 2. Cumulative review of all CC-XX fixes in scope

One bullet each, confirming current hold status across the cumulative range:

- **CC-02 — PASS:** no regression signal surfaced in this sweep.
- **CC-04 — PASS:** vault read path still fail-closes for malformed / missing / invalid payloads under `requires_vault=True`.
- **CC-05 — PASS:** reconciler/config validation showed no obvious regression during cumulative inspection.
- **CC-06 — PASS:** vault fail-closed transport/error posture still holds.
- **CC-07 — PASS:** changed frontend hero copy scan showed no forbidden hype-language regression.
- **CC-08 — PASS:** admin auth/observability surfaces reviewed here remain admin-gated and operator-visible.
- **CC-09 — PASS:** vault runtime contract alignment still holds for key/value validation.
- **CC-10 — PASS:** identifier validators still use strict `\Z` anchors in sandbox path/user validation.
- **CC-11 — PASS:** oversize vault **value** handling remains fail-closed; no silent value slicing reappeared.
- **CC-12 — PASS:** workflow actions remain SHA-pinned.
- **CC-13 — PASS:** workflow files retain top-level minimal `permissions:` blocks.
- **CC-14 — PASS:** no secret-scan coverage regression surfaced.
- **CC-15 — PASS:** deploy concurrency hardening remains in place.
- **CC-16 — PASS:** no optional/no-op rate-limiter regression surfaced.
- **CC-17 — PASS:** SQL function `search_path` pinning holds repo-wide.
- **CC-18 — PASS:** no interpolated `detail=f"..."` leaks resurfaced in `browser_server`.
- **CC-19 — PASS:** no interpolated `detail=f"..."` leaks resurfaced in `sandbox_server`.
- **CC-20 — PASS:** agent route 404 details remain generic (`task not found`) without identifier echo.
- **CC-21 — FAIL sibling / medium finding below:** several task-level and soft-failure agent error strings still drift from the documented canonical stable-code contract.
- **CC-22 — PASS:** main API 404 detail scrubs still hold in the audited surfaces.
- **CC-23 — PASS:** exact npm pinning remains intact in frontend/e2e manifests.
- **CC-24 — PASS:** main API 404 detail scrubs still hold.
- **CC-25 — PASS at ToolError path:** direct ToolError persistence/emission still uses `tool_error`; no raw tool detail leak resurfaced there.
- **CC-26 — PASS:** lockfile/package exact-pinning posture remains intact; no range drift found.
- **CC-27 — PASS:** oversize vault entry-count handling now fail-closes correctly.
- **CC-28 — FAIL:** helper exists, but route-level enforcement is current-size-only rather than projected-size-safe.
- **CC-29 — PASS:** admin RPC responses remain scrubbed.
- **CC-30 — PASS:** admin role cache remains bounded.
- **CC-31 — PASS:** universal function-level `search_path` invariant holds.
- **CC-32 — PASS:** filename echo scrub remains in place.
- **CC-33 — PASS:** `/metrics` remains implemented, admin-gated, and self-skip safe.

---

## 3. New territory probe results

Every dimension below was checked, even where the result is PASS.

### SSRF posture — **PASS**
- `browser_server/app.py` blocks non-HTTP(S), localhost/internal hostnames, and private/loopback/link-local/reserved/multicast/unspecified IPs.
- `mariana/connectors/base.py` validates initial URLs and blocks redirects to internal/private addresses.
- Net result: browser and connector SSRF defenses look materially strong.

### Stripe webhook verification + idempotency — **PASS with minor note**
- `mariana/api.py:6361+` verifies signatures against primary + previous webhook secrets and fails closed if no secret is configured.
- `_claim_webhook_event` / finalize flow provides robust two-phase idempotency and duplicate suppression.
- Minor note only: some 500 responses to Stripe still include raw `str(exc)` in response JSON (`idempotency_error`, `handler_error`, `finalize_error`). I did **not** elevate this beyond a note because it is machine-to-machine and not an end-user leak.

### Sandbox/container hardening — **PASS**
- `docker-compose.yml` keeps the sandbox on an internal-only network, with `read_only: true`, `no-new-privileges:true`, `cap_drop: ALL`, tmpfs scratch space, and narrowly restored capabilities for user-dropping/runtime control.
- Overall sandbox deployment posture looks appropriately hardened for this architecture.

### Time-zone hygiene — **PASS**
- No naive `datetime.now()` / `datetime.utcnow()` regression surfaced in the audited paths; observed usage remained timezone-aware.

### SQL hardening / search_path — **PASS**
- Function sweep found no repo-owned missing-`search_path` functions.

### Workflow hardening — **PASS**
- All workflow `uses:` entries remain SHA-pinned.
- `ci.yml` and `deploy.yml` retain top-level minimal permissions.

### Package pinning / frontend dependency drift — **PASS**
- `frontend/package.json` and `e2e/package.json` remained exact-pinned; no `^`/`~` drift surfaced.

### Admin route gating — **PASS**
- Audited `/api/admin/...` routes remained protected by `_require_admin`.

### Frontend hero-copy / forbidden-claim scan — **PASS**
- Grep over changed frontend files found no new “world-class / revolutionary / best-in-class / guaranteed / unlimited / billion / military-grade / bank-grade” style hero-copy claims.
- Reviewed `Index.tsx` / `Product.tsx` copy remained assertive but generally product-descriptive rather than obviously deceptive hype.

### Caches / memory-growth surfaces — **PASS with one low caveat below**
- The major prior cache bug (`_ADMIN_ROLE_CACHE`) is fixed.
- No new comparably large unbounded in-process cache surfaced in the inspected hot paths.

### Connection-pool sizing / lifecycle — **PASS**
- No connection-leak regression surfaced in the inspected API/browser/connector paths.

### Health / metrics / signal handling — **PASS**
- `/metrics` now exists and is admin-gated.
- No new health/signal regression surfaced in the inspected deployment/runtime surfaces.

### Backup / DR / retention evidence — **INFO gap**
- I did not find first-class backup/restore/retention operational evidence in `docker-compose.yml`, CI/deploy workflows, or top-level docs.
- This may exist outside the repo, so I am recording it as an operational visibility gap rather than a code defect.

### Logging consistency — **LOW gap below**
- Core API uses structured logging / JSON renderer in production.
- `sandbox_server/app.py` and `browser_server/app.py` still use plain `logging.basicConfig(...)` text logs rather than the same structured JSON posture.

---

## 4. Findings

### Finding 1 — CC-28 does not fully hold: workspace quota checks current usage only, not projected post-write usage
**Severity:** Medium  
**Evidence:**
- `sandbox_server/app.py:131-144` — `_enforce_workspace_quota(workspace_root)` accepts no `additional_bytes` / projected-size argument.
- `sandbox_server/app.py:643-645` — `/exec` calls the helper before running, but only against current size.
- `sandbox_server/app.py:823-850` — `/fs/write` calls the helper before decode/write, but does not add incoming payload bytes.
- `tests/test_cc28_workspace_quota.py:44-124` — tests helper/env/boundary behavior only; no route-level projected-overflow case.
- `loop6_audit/CC27_CC33_FIX_REPORT.md:58-68` described `_enforce_workspace_quota(..., additional_bytes=0)` and projected-size rejection, which the implementation under review does not match.

**Repro:**
1. Put a workspace at `limit - 1 KiB`.
2. POST `/fs/write` with a 10 MiB payload, or run `/exec` that writes a large file/artifact.
3. Pre-write helper sees “under cap” and allows the request.
4. Workspace crosses the quota after the write completes.

**Why it matters:** this re-opens the production-safety goal of CC-28. The route does not prevent a single large write from pushing storage over the configured cap.

**Recommended fix:**
- Extend `_enforce_workspace_quota` to accept `additional_bytes`.
- In `/fs/write`, compute decoded byte count and reject if `used + delta > max`.
- In `/exec`, reserve for source bytes up front and re-check post-run based on artifact/workspace deltas, or enforce a stricter filesystem-level quota underneath.
- Add route-level regression tests that prove projected over-limit writes return HTTP 507 `workspace_full`.

### Finding 2 — canonical agent error-code contract still drifts in task/soft-failure persistence
**Severity:** Medium  
**Evidence:**
- `mariana/agent/loop.py:21-31` documents a stable canonical set (`tool_error`, `unexpected`, `planner_failed`, `vault_unavailable`, `vault_transport_violation`, `stream_unavailable`, etc.).
- `mariana/agent/loop.py:363-369` returns raw budget strings such as `budget_exhausted: spent ...` and `duration_exhausted: ...`.
- `mariana/agent/loop.py:902-920` returns soft-failure strings such as `timed_out after ...ms`, `process killed (memory / signal)`, `non-zero exit code ...`, and `HTTP ...`.
- `mariana/agent/loop.py:1335-1384` persists `task.error = "stop_requested"` and emits halt reasons with that same raw string.
- `mariana/agent/loop.py:1403-1408` persists `deliver_failed: {err}`.
- `mariana/agent/loop.py:1465-1468` persists `unrecoverable: step {id} — {err}`.
- `mariana/agent/api_routes.py:975-979` also persists `terminal_task.error = "stop_requested"` in the pre-execution cancel path.

**Repro:**
1. Cancel a task pre-plan or mid-run: persisted `task.error` becomes `stop_requested`.
2. Exhaust budget or duration: persisted halt reason becomes `budget_exhausted: ...` / `duration_exhausted: ...`.
3. Fail a deliver step or exhaust replans: persisted `task.error` becomes `deliver_failed: ...` / `unrecoverable: step ...`.

**Why it matters:** the codebase explicitly claims a stable error-code contract, but downstream consumers still receive mixed free-form strings and canonical codes. That is a contract drift / control-plane stability issue, even if it is not a direct security leak.

**Recommended fix:**
- Define and enforce one canonical enum for **all** persisted/emitted task and step errors.
- Map `stop_requested`, budget/duration exhaustion, deliver failure, and unrecoverable failure into stable codes (`quota_exceeded`, `forbidden`, `unexpected`, etc., or an expanded explicit enum).
- Keep human-readable diagnostics in structured logs and optional non-contract metadata fields only.
- Add source-level tests that forbid non-canonical `task.error` / `step.error` writes outside a single allow-list.

### Finding 3 — sidecar logging posture is not uniform with production JSON logging standard
**Severity:** Low  
**Evidence:**
- `mariana/main.py:84-85` configures `structlog.processors.JSONRenderer()` for production logging.
- `sandbox_server/app.py:65-68` uses `logging.basicConfig(...)` text formatting.
- `browser_server/app.py:52` also uses `logging.basicConfig(...)` text formatting.

**Repro:** inspect logs from API vs sandbox/browser sidecars in production; formats differ materially.

**Why it matters:** this is not a correctness or security failure by itself, but it weakens cross-service observability, machine parsing, and incident response consistency.

**Recommended fix:** standardize sandbox/browser logs on the same structured JSON logging contract used by the API/main services.

### Finding 4 — no repo-visible backup/restore/retention operating posture surfaced
**Severity:** Info  
**Evidence:**
- Broad targeted searches over top-level docs, workflows, and compose surfaces did not reveal first-class backup / restore / retention / RPO / RTO posture.
- `README.md` only surfaced a generic “Crash recovery” tree reference, not an operational DR plan.

**Repro:** repo inspection only; this is an absence-of-evidence finding, not proof that backups do not exist elsewhere.

**Why it matters:** for a billion-dollar launch, operators normally need explicit, testable backup/restore ownership and retention guarantees. I cannot confirm that from the repository itself.

**Recommended fix:** document and automate backup / restore / retention posture in repo-adjacent operational material, or make the relevant deployment/runbook references discoverable from this codebase.

---

## 5. Final paranoid grep

### Forbidden backend/frontend patterns
- `detail=f"..."` in `sandbox_server/**/*.py` — no hits.
- `detail=f"..."` in `browser_server/**/*.py` — no hits.
- Non-test regex `$` anchor regressions — no dangerous sibling surfaced beyond the already-acceptable bytea parser case.
- Workflow floating tags — no hits; actions remain SHA-pinned.

### Frontend hero-copy scan
Changed frontend pages/components in the cumulative range included `Index.tsx`, `Product.tsx`, `Research.tsx`, `Skills.tsx`, `Chat.tsx`, `Admin.tsx`, `Dev*` pages, `PromptBar`, `VaultSetupWizard`, and related components.

Targeted grep for aggressive/forbidden hype terms (`world-class`, `revolutionary`, `magic`, `best-in-class`, `guaranteed`, `unlimited`, `billion`, `military-grade`, `bank-grade`, etc.) returned **no hits** in the changed frontend source set.

Manual spot-check of the main changed marketing pages:
- `Index.tsx` hero copy is assertive but product-descriptive.
- `Product.tsx` copy remains largely descriptive of the execution model.
- Literal “unlock” language observed elsewhere is vault/crypto UX, not hero-copy hype.

**Paranoid grep verdict:** PASS, aside from the findings above.

---

## Final verdict
This re-audit is **not clean**. Six of the seven CC-27..33 fixes hold, but **CC-28 is materially incomplete at the route/projection level**, and the agent error contract still drifts from its own documented canonical model. For the zero-bug convergence program, that means **streak round 1/3 does not start yet**.
