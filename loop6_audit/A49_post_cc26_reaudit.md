# A49 Re-audit #44 (opus) post-CC-26

**Branch:** `loop6/zero-bug`
**HEAD:** `793069b`
**Cumulative range audited:** `c108b1e..793069b` (twenty-three CC-XX fixes total)
**Streak target:** round 1/3 after CC-24/25/26 reset
**Date:** 2026-04-28

---

## One-line verdict
CC-24, CC-25, and CC-26 all hold cleanly; the audit found **no high-severity regressions**, but seven lower-severity items remain (1 medium contract-drift sibling, 1 medium production-readiness gap, the rest low/info). **Streak does NOT advance to 1/3** because of the medium-severity findings.

## Findings count + severities
- **7 findings total**
- **0 critical / 0 high**
- **2 medium** (Findings 1 & 2)
- **3 low** (Findings 3, 4, 5)
- **2 info / production-readiness** (Findings 6, 7)

## Production-ready Y/N
**Y, with caveats.** The codebase is materially production-ready. None of the findings is a security-critical bug or correctness defect under normal load. Findings 1 (vault entry-count slice) and 6 (no workspace disk quota) are the only items I would block a true zero-bug call on; both are containable with monitoring and operator policy in the short term.

---

## 1. CC-24 / CC-25 / CC-26 verification

### CC-24 — main API 404 detail leakage scrub — **PASS**

Re-grep results (`mariana/api.py`, branch HEAD `793069b`):

- `grep -nE 'detail=f"[^"]*not found' mariana/api.py` → **0 matches**
- `grep -nE 'HTTPException\(status_code=404, detail=f"' mariana/api.py` → **0 matches**
- `grep -n 'task_id!r' mariana/api.py` → **0 matches**
- `grep -n 'Task \{' mariana/api.py` → **0 matches**

All ten 404 sites called out in A48 (1551, 3366, 3412, 3438, 3480, 4385, 4466, 4560, 4621, 4800) were converted to the canonical pattern (`logger.info("task_not_found", task_id=...)` followed by `raise HTTPException(status_code=404, detail="task not found")`). The four sibling leaks (filename 4648, plan_id 5573, skill_id 8929, task_id 9211) were also fixed.

### CC-25 — ToolError scrub from agent step state and SSE — **PASS**

`mariana/agent/loop.py:951-975` now:

1. Logs `raw_message=str(exc), raw_detail=exc.detail` to the structured server log (operator-only).
2. Sets `step.error = "tool_error"` (stable code).
3. Sets `step.result = {"error_code": "tool_error", "tool": step.tool}` (no `error_detail` key, no raw text).
4. Emits a `step_failed` SSE payload of `{"error": "tool_error", "tool": step.tool}` only — no raw text, no `detail` key.

`mariana/agent/dispatcher.py` `ToolError` class docstring now explicitly states the message + detail are server-log-only as of CC-25. No `ToolError` construction was changed — diagnostic richness preserved.

The agent's only other exception path is the soft-failure path (`_infer_failure`, `mariana/agent/loop.py:902-920`), which returns short stable strings like `"timed_out after 5000ms"` / `"non-zero exit code 137"` / `"HTTP 500"` — no raw paths, file names, or response bodies. Vault paths use the stable `vault_unavailable` / `vault_transport_violation` codes.

The `tests/test_cc25_tool_error_scrub.py` suite (3 cases) passed locally.

### CC-26 — lockfile idempotence — **PASS**

```
$ sha256sum frontend/package-lock.json e2e/package-lock.json
741bd767be83508d7b0759e0b16b9589fa69a96ce500224d9e857f90913cbf77  frontend/package-lock.json
1533fd788a24ef44efb1cbf5930b197228b6c0f8f71885c7a863ef9ee2a77701  e2e/package-lock.json

$ cd frontend && npm install --no-audit --no-fund
up to date in 816ms

$ cd ../e2e && npm install --no-audit --no-fund
up to date in 269ms

$ sha256sum ../frontend/package-lock.json ./package-lock.json
741bd767be83508d7b0759e0b16b9589fa69a96ce500224d9e857f90913cbf77  ../frontend/package-lock.json
1533fd788a24ef44efb1cbf5930b197228b6c0f8f71885c7a863ef9ee2a77701  ./package-lock.json
```

Both lockfiles are byte-stable across a fresh install. The "monorepo workspace" concern is moot — there is no top-level `package.json` defining workspaces; `frontend/` and `e2e/` are two independent npm packages, each with its own lockfile, and `cd` to each is the correct invocation.

---

## 2. Cumulative review of 23 CC-XX fixes (`c108b1e..793069b`)

### CC-04..06 vault fail-closed — see Finding 1

The `requires_vault=True` paths at every stage of `mariana/vault/runtime.py:fetch_vault_env` correctly raise `VaultUnavailableError` for: missing Redis client, transport-policy violation, Redis IO error, missing payload, malformed JSON, non-object payload, empty-object payload, invalid k/v shape, empty value, oversize value. **One sibling remaining:** see Finding 1 (entry count > MAX is silently truncated).

### CC-07..09 vault contract drift — **PASS**

`_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}\Z")` confirmed at `mariana/vault/runtime.py:119`. `\Z` (not `$`) so trailing-newline names are rejected. Empty-string values dropped in both write and read paths consistently.

A repo-wide grep for sibling `re.compile(r"...$")` patterns in non-test code found exactly one hit — `mariana/vault/store.py:63 _BYTEA_HEX_RE` — which parses Postgres bytea hex literals returned by asyncpg, never user input. A trailing newline cannot occur in that channel and would not change semantics.

### CC-10/11 sibling slicing/regex — see Finding 1

`mariana/vault/runtime.py:312` correctly fail-closes on oversize values (`v[:_MAX_VAULT_VALUE_LEN]` slice replaced with explicit raise). However, the entry-count cap one line earlier (`list(data.items())[:_MAX_VAULT_ENV_ENTRIES]`, line 276) still silently slices — see Finding 1.

### CC-12/13 GitHub Actions hardening — **PASS**

```
$ grep -rnE 'uses: [^@]+@[v0-9]' .github/workflows/ | grep -vE '@[a-f0-9]{40}'
(empty — all uses pinned to 40-char SHA)
```

Both `ci.yml` and `deploy.yml` carry top-level `permissions: { contents: read }`. Neither workflow contains a job that grants more without an explicit `permissions:` override at job level. CC-12 + CC-13 hold.

### CC-14 secret-scan blind spot — **PASS**

`.github/scripts/check_secrets.sh` `EXCLUDES` array now only excludes the scanner script itself (`':!.github/scripts/check_secrets.sh'`), not the entire `.github/scripts/` tree. Other helper scripts in that directory will be scanned.

### CC-15 deploy concurrency — **PASS**

`.github/workflows/deploy.yml` declares `concurrency: { group: deploy-hetzner-prod, cancel-in-progress: false }`. Two simultaneous pushes serialize against a single deploy slot; the running deploy is not cancelled mid-flight.

### CC-16 noop limiter removal — **PASS**

`mariana/api.py:67-76` hard-imports `slowapi`. No `_NoopLimiter` class anywhere in live code (only a historical comment at line 68). `requirements.txt:16` pins `slowapi==0.1.9`. No test fixture in `tests/` references a noop limiter.

### CC-17 search_path pinning — **PASS** (one informational sibling)

A custom static parser found **62** non-comment `SECURITY DEFINER` function definitions across 45 SQL migrations; **all 62** carry `SET search_path`. The `tests/test_cc17_security_definer_search_path.py` regression test passed locally.

There is one `SECURITY INVOKER` (i.e. default privilege) function — `public.touch_updated_at()` in `frontend/supabase/migrations/003_deft_vault.sql:166` — that does not pin `search_path`. As an INVOKER trigger it runs with the caller's privileges, so a search-path hijack does not escalate, but it remains a hardening hygiene gap. See **Finding 5**.

### CC-18/19 browser_server / sandbox_server scrubs — **PASS**

`grep -rnE 'detail=f"' sandbox_server/ browser_server/ --include='*.py'` → **0 matches**.

### CC-20/21 SSE / loop scrubs — **PASS**

`mariana/agent/api_routes.py` exception handlers (e.g. `validate_vault_env` `ValueError`, lines 446-457) all stash the cause to structured logs and surface only stable HTTPException details like `"vault_env invalid"`. `mariana/agent/loop.py` exception handlers all set `task.error` / `step.error` to stable codes (`vault_unavailable`, `vault_transport_violation`, `unexpected`, `tool_error`, `planner_failed`).

### CC-22/24 main api 404 scrubs — **PASS** (already covered above)

### CC-23/26 npm pin + lockfile idempotence — **PASS** (already covered above)

---

## 3. New territory

### Time zones — **PASS**

A repo-wide `grep -nE 'datetime\.now\(\s*\)|datetime\.utcnow\('` over non-test code returned zero naive constructions. Every observed use specifies `tz=timezone.utc`, e.g. `mariana/data/models.py:219, 271, 272, 310, 332, ...`, `mariana/api.py:2629, 2731, 2780, 4344, ...`, `mariana/orchestrator/branch_manager.py:279, 433, 507`. Stripe-supplied epoch timestamps go through `datetime.fromtimestamp(ts, tz=timezone.utc)`.

### i18n — **PASS** with minor observation

Frontend renders numbers with `Number.prototype.toLocaleString()` (Navbar, PreflightCard, ProjectsSidebar, PromptBar, chart) which honours browser locale. Dates render with `Date.prototype.toLocaleDateString(undefined, ...)` (AccountView). RTL is handled by browser shaping; no hardcoded `dir="ltr"` or `text-align: left` in user-facing copy. The few `toFixed(1)` / `toFixed(2)` sites are bytes-formatting (KB / MB) and dollar/credit cost displays — acceptable to keep `.` as the decimal point in those technical UIs.

### Concurrent writes — **PASS**

The credit ledger and agent-task state machine both serialize via `pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0))` (migrations 002, 007) for per-user RPCs and `SELECT ... FOR UPDATE` (`mariana/agent/api_routes.py:918`, `mariana/orchestrator/branch_manager.py:528`) for per-row writes. The settlement reconcilers use `FOR UPDATE SKIP LOCKED` to fan out across runners.

### Disk quota / agent task workspaces — see Finding 6

### Memory: caches with no TTL or maxsize — see Finding 4

### Connection-pool sizing — **PASS**

`asyncpg.create_pool(min_size=POSTGRES_POOL_MIN, max_size=POSTGRES_POOL_MAX, command_timeout=60.0)` is sized via env. `aioredis.from_url` defaults to a 50-connection pool, which is acceptable for a single API replica. `httpx.AsyncClient` is created per request with explicit `timeout=` everywhere; this is suboptimal vs. a process-wide singleton (creates / tears down a small connection pool on every call) but is not a leak — the `async with` ensures cleanup.

### Health / metrics / signal handling — see Finding 7

`/api/health` exists at `mariana/api.py:1944` and returns 200 on liveness only (no DB / Redis dependency check — appropriate for k8s liveness probes). Lifespan teardown (`mariana/api.py:289-348`) closes both `_db_pool.close()` and `_redis.aclose()` on shutdown.

The CLI worker (`mariana/main.py:216-222`) registers `signal.SIGINT` and `signal.SIGTERM` handlers that set a shutdown flag; long-running loops poll it. `mariana/orchestrator/event_loop.py:835` honours an externally-set `HALTED` state.

---

## 4. Findings

### Finding 1 — vault read path silently truncates entry count under `requires_vault=True`
**Severity:** Medium

`mariana/vault/runtime.py:276`:

```py
out: dict[str, str] = {}
for k, v in list(data.items())[:_MAX_VAULT_ENV_ENTRIES]:
    ...
```

This is the same contract-drift class CC-11 fixed for `_MAX_VAULT_VALUE_LEN`. The write path (`validate_vault_env`, line 95) explicitly raises `ValueError` on `len(env) > _MAX_VAULT_ENV_ENTRIES`, so a legitimate ingest cannot exceed 50. But a poisoned / corrupted Redis blob with > 50 entries will silently take the first 50 and **run as if those were the user's full secret set** — exactly the fail-closed bypass shape CC-04..06 + CC-09 + CC-11 are meant to prevent.

**Reproduction (mental):** poison `vault:env:{task_id}` with a 51-key dict. `fetch_vault_env(requires_vault=True)` returns 50 keys without raising; the task runs and any tool referencing the 51st key sees it as missing.

**Recommended fix:** mirror the CC-11 oversize-value pattern. Under `requires_vault=True` raise `VaultUnavailableError("vault_env oversize_entries ...")`; under `requires_vault=False` warn and slice (legacy soft-fail).

### Finding 2 — sandbox/agent task workspaces have no disk quota
**Severity:** Medium

`sandbox_server/` does not enforce a per-task workspace size cap. `grep -rnE 'quota|disk|max.*size|du -' sandbox_server/` returns no production-relevant hits. A tool step can write arbitrary amounts to its workspace; a malicious or runaway plan can fill the host filesystem.

The HTTP upload endpoint (`mariana/api.py:4677-4678`) caps `_UPLOAD_MAX_FILE_SIZE = 10 * 1024 * 1024` and `_UPLOAD_MAX_FILES_PER_INVESTIGATION = 5`, but those are user uploads, not the LLM-driven `code_exec` / `bash_exec` workspace.

**Recommended fix:** enforce a per-task workspace ceiling either via cgroups / overlayfs quota at the sandbox container layer, or via a periodic `du` check that aborts the task with a stable `workspace_full` error code.

### Finding 3 — admin RPC error responses echo upstream Supabase response body
**Severity:** Low

Multiple admin-only endpoints in `mariana/api.py` echo up to 200-400 chars of the Supabase REST error body to the admin client:

- `8093:` `detail=f"RPC {fn} failed: {body}"` (400-char body)
- `8316, 8336, 8364, 8386, 8405, 8425, 8452:` `detail=f"... failed: {resp.text[:200]}"`
- `8614:` `detail=f"Flush failed: {exc}"` (admin Redis flush)

Postgres / PostgREST error bodies can contain table/column names, foreign-key constraint names, RLS policy names, and value snippets. These are admin-only routes (gated by `_require_admin`), so impact is limited to admins, but the pattern is the same class CC-22/24 closed for non-admin routes.

**Recommended fix:** apply the same canonical pattern — `logger.error("admin_rpc_failed", fn=fn, status=resp.status_code, body=body)` and surface a stable detail like `"admin RPC failed"`. The structured log preserves operator diagnostics.

### Finding 4 — `_ADMIN_ROLE_CACHE` is unbounded
**Severity:** Low

`mariana/api.py:122 _ADMIN_ROLE_CACHE: dict[str, tuple[float, bool]]` has a 5-second negative-only TTL but no per-key eviction floor and **no maximum size**. An attacker with the ability to make authenticated requests with many distinct (random) `user_id` values would grow this dict indefinitely until the API process is recycled. Each entry is small (~80 bytes) so the practical risk is bounded, but it remains an unbounded cache.

**Recommended fix:** add a `collections.OrderedDict` with `popitem(last=False)` when size > N (e.g. 10_000), or switch to `cachetools.TTLCache(maxsize=10_000, ttl=5.0)`.

### Finding 5 — `touch_updated_at()` SECURITY INVOKER trigger lacks `SET search_path`
**Severity:** Low

`frontend/supabase/migrations/003_deft_vault.sql:166`:

```sql
CREATE OR REPLACE FUNCTION public.touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$ ... $$;
```

No `SET search_path = public, pg_temp`. As an INVOKER function this runs with the caller's privileges, so a search_path hijack does not escalate; but the function references no schemas explicitly (it only uses `clock_timestamp()` and the trigger NEW row, both unqualified), so a malicious user with CREATE on a schema earlier in `search_path` cannot redirect those references to anything dangerous in practice. Still, hygiene parity with the 62 DEFINER functions is desirable.

**Recommended fix:** add `SET search_path = public, pg_temp` to the function body for parity with CC-17.

### Finding 6 — user-supplied filename echoed in 400 detail strings
**Severity:** Low (info)

`mariana/api.py:4880, 4884, 4898, 5025, 5028, 5042` all interpolate the user-supplied `filename!r` / `safe_name!r` into 400 detail strings. This is the strict letter of the CC-22/24 class invariant ("don't interpolate user data into user-facing HTTPException details"), but the practical risk is bounded — the user supplied the value in the same request and is the only recipient. There is no XSS surface because the response is `application/json` and clients render it as text.

**Recommended fix:** lowest priority. If pursued, replace with `detail="Invalid filename"` and log the raw filename to structured logs for operator diagnostics.

### Finding 7 — no `/metrics` endpoint exposed
**Severity:** Info / production-readiness

No `prometheus_client` instrumentation. Operators have to rely on log aggregation alone for metrics. For a multi-replica production deployment, a Prometheus-format `/metrics` endpoint (or Datadog StatsD) would normally be expected.

**Recommended fix:** add `prometheus_fastapi_instrumentator` or equivalent, scope `/metrics` to internal network or auth-gate it.

---

## 5. Final paranoid forbidden-word grep

A frontend-wide grep for: `unleash, unlock, supercharge, revolutionize, transform, empower, master, discover, effortless, seamless, build the future, leverage, cutting-edge`:

- `pages/Index.tsx, pages/Product.tsx, pages/Pricing.tsx, pages/Contact.tsx, pages/Skills.tsx` — **0 hero-copy hits**.
- The literal word "unlock" appears in `VaultUnlockDialog.tsx`, `VaultSetupWizard.tsx`, `vaultCrypto.ts`, `Vault.tsx` — all literal cryptographic vault-unlock language, not marketing copy. **Pass.**

Backend repo-wide grep for `detail.*\{.*(_id|_path|_user|filename)`:

- The only hits are the six filename-echo sites in Finding 6 plus already-known input-echo sites (`hostname!r`, `tier!r`, `user_plan!r`) which echo the user's own input back in 400 details. **Pass on the strict CC-22/24 invariant** for non-user-supplied identifiers.

---

## 6. Test execution

Focused regression tests passed locally:

```
$ python -m pytest tests/test_cc25_tool_error_scrub.py tests/test_cc17_security_definer_search_path.py -q
.....                                                                    [100%]
5 passed in 0.44s
```

I did not re-run the full pytest 516 / vitest 144 suites; the CC-24/25/26 fix report's full-suite numbers (516 / 144 passing) match the test files I observe in-tree.

---

## 7. Streak verdict

**One-line verdict:** Two medium-severity items remain (Finding 1 vault entry-count slice, Finding 2 no workspace disk quota); zero high-severity. **Streak does NOT advance to 1/3** under the strict zero-finding bar.

If the bar is "zero new high or critical findings introduced by CC-XX", the streak DOES advance — the two medium findings are pre-existing or production-readiness gaps, not regressions caused by CC-04..26.

## 8. Confidence

**High** on the CC-24/25/26 verification (direct re-grep, focused test runs, lockfile SHA256 idempotence check).

**Medium-high** on the broader 23-fix retrospective (static analysis, targeted greps, SQL migration parser, frontend forbidden-word sweep) — I did not run the full pytest / vitest suites in this pass, and prompt-injection / SSRF surfaces beyond what was already covered in A48 were out of scope.
