# A29 — Phase E Re-audit #24 (Loop 6 zero-bug convergence)

- **Audit number:** A29
- **Auditor model:** claude_opus_4_7
- **Branch / HEAD:** `loop6/zero-bug` @ `89a9bfc`
- **Date:** 2026-04-28
- **Scope:** (1) adversarial probe of the X-01 fix; (2) fresh sweep of high-leverage surfaces not yet drilled in A6..A28.

---

## Section 1 — X-01 fix probe

### Methodology

1. Read the updated module-load region of `mariana/api.py` (`_load_rate_limit_storage_uri()` helper at lines 406-416 and the `_redis_rate_limit_url` assignment at 419) and the slowapi Limiter construction at 421-441.
2. Read `tests/test_x01_rate_limit_storage_url.py` (4 cases).
3. Re-read `mariana/util/redis_url.py:assert_local_or_tls` semantics.
4. Repo-wide greps to find any other `Limiter(` callsite or alternate env var that could re-introduce the gap:
   - `Limiter(` — only the existing api.py site (4 references including the slowapi-unavailable fallback) plus the B-21 unit-test fixture under `tests/test_b21_redis_rate_limit.py`.
   - `RATELIMIT_STORAGE_URL` / `RATELIMIT_STORAGE_URI` — no occurrences in `mariana/`.
   - `storage_uri=` — only the fixed call.
5. Examined non-`redis://` URL forms that slowapi/limits also accepts:
   - `redis+sentinel://...`, `redis-cluster://...`, `memcached://...`, `memory://`, `async+redis://...` — all rejected by `assert_local_or_tls` because the scheme branch only accepts `redis` or `rediss` and otherwise raises `ValueError("expected redis:// or rediss://")`. This is the right fail-closed default — an operator who configures `REDIS_URL=memcached://remote:11211` to back the rate limiter now gets a clear import-time error instead of a silent plaintext path.
6. Probed kwarg/bypass paths for the helper itself: the function takes no caller-supplied URL — it always reads `os.environ.get("REDIS_URL")` — so an attacker cannot pass a separately-validated URL in.
7. TOCTOU between import-time validation and slowapi's first request: the env var is read once, validated, and the same string is handed to the Limiter constructor. slowapi re-reads it from the constructor argument, not from env, so a later `os.environ["REDIS_URL"] = "..."` at runtime cannot bypass the validator.
8. Empty / unset URL behaviour: `_load_rate_limit_storage_uri()` returns `None`, `_redis_rate_limit_url` is falsy, the existing else-branch warns about per-process counters and constructs the Limiter without `storage_uri=`. This is the intended in-memory fallback documented under B-21; the validator's `if not url: return` is consistent with V-01 / W-01 callsite semantics.
9. Surface-string consistency: `surface="rate_limit_storage"` matches the `surface=` convention used by W-01 (`api_startup` / `agent_daemon` / `cached data`) and V-01 (`vault env Redis`). Error messages will be human-readable and keyword-greppable in logs.
10. Test quality (`tests/test_x01_rate_limit_storage_url.py`):
    - Substring-bypass case (`redis://localhost.attacker.com:6379`) is the same regression V-01 / W-01 pin, so a future weakening of `_LOCAL_REDIS_HOSTS` or the parser would trip three independent suites.
    - Plaintext-remote case is the ground-truth W-01 case.
    - Safe-URL acceptance covers loopback plaintext, TLS-remote, and unset/None.
    - `test_api_module_uses_validated_storage_uri` pins that the module-level constant equals the helper output, so a future refactor that re-introduces the direct env read would fail this test.
11. Verified no second `Limiter` instance exists for admin-only routes or any sub-router.

### Findings — X-01 fix

NONE. The fix is minimal, fails closed on every URL form `assert_local_or_tls` does not whitelist, and the test suite covers the substring-bypass, remote-plaintext, safe-URL, and module-level-constant integrity cases.

Cosmetic-only observation (NOT a finding): `mariana/api.py:103` already imports `make_redis_client` from `mariana.util.redis_url`; the new line 403 imports `assert_local_or_tls` separately. Could be combined, but the current placement matches the inline comment at 397-402 explaining the X-01 contract, which is a reasonable readability tradeoff.

---

## Section 2 — New-surface sweep

| # | Surface | Probed | Finding |
|---|---------|--------|---------|
| 1 | Conversation deletion cascade (`api.py:2644-2676`) | Yes — owner-scoped Supabase REST call (`id=eq.{conversation_id}&user_id=eq.{user_id}`). DB-level cascade handles messages; investigations are SET NULL. Affected-rows check returns 404 for missing rows. No agent_tasks coupling — agent tasks are scoped to `user_id`, not `conversation_id`, so deleting a conversation does not orphan agent_settlements credit data. | NONE |
| 2 | File upload — `POST /api/investigations/{task_id}/upload` (`api.py:4770-4892`) and pre-submission `POST /api/upload` (`api.py:4901-5036`) | Per-target asyncio lock serialises count-and-write (SEC-E3-R1-02 / G-01 strong-ref LRU). `_validate_upload_session_uuid` enforces UUID format. Filenames sanitised through `re.sub(r"[^\w\-.]", "_", filename)` then `os.path.basename`, dotfiles + `.` + `..` rejected, resolved-path containment check, symlinks rejected after write (race-safe). Atomic ownership binding via `os.O_EXCL` on `.owner` file. Size cap streamed (10 MB). Allowlist of extensions. Cross-tenant: pending session ownership verified by reading the atomic `.owner` file inside the lock. | NONE |
| 3 | File download — `GET /api/investigations/{task_id}/files/{filename:path}` (`api.py:4596-4651`) | UUID validation, owner-or-admin check using FK `user_id` with metadata fallback (F-05), `Path.resolve()` containment check, symlink rejection. | NONE |
| 4 | Preview HMAC tokens (`api.py:1357-1481`) | Scope marker (`preview\|user_id\|task_id\|exp\|sig`) prevents cross-replay against stream tokens, constant-time HMAC compare, 5s clock-skew grace, 4-hour TTL bound to `task_id`. Stable secret derivation across workers. Path traversal blocked in `/preview/{task_id}/{file_path:path}` (rejects `\x00`, `..`, escape via `relative_to`). 404 on missing manifest avoids existence leak. | NONE |
| 5 | OAuth / magic link / password reset | Delegated entirely to Supabase. Mariana only verifies tokens via `${SUPABASE_URL}/auth/v1/user`. No local password storage, no token-mint paths, no PKCE/state to audit on this side of the boundary. | NONE |
| 6 | Admin / debug / metrics routes | All `/api/admin/*` endpoints declare `Depends(_require_admin)`. `/api/health` is intentionally open (liveness). `/api/config` requires auth (VULN-C2-07). `/api/orchestrator-models` is open by design (model picker). No `/metrics`, `/healthz`, `/debug`, `/internal` exists. `/openapi.json` and `/docs` are exposed but redact `data_root` and never embed secrets. | NONE |
| 7 | Profile mass-assignment | No public `PATCH /users/me` or `PATCH /profile` endpoint exists in `mariana/api.py`. The only profile mutators are admin-gated (`/api/admin/users/{user_id}/role`, `/credits-v2`, `/suspend`). Stripe webhook profile patches use customer_id from signed webhook payload only. | NONE |
| 8 | Agent task body (`POST /api/agent` AgentStartRequest) | Pydantic-typed fields only (`goal`, `user_instructions`, `conversation_id`, `selected_model`, `budget_usd`, `max_duration_hours`, `vault_env`). `user_id` is taken from `current_user`, not body. `vault_env` validated via `validate_vault_env` before the task row. | NONE |
| 9 | Vault routes (`/api/vault/*`) | All endpoints scope by `user_id=current_user["user_id"]`. PostgREST DELETE/PATCH filters include both `id=eq.{secret_id}` and `user_id=eq.{user_id}`. DELETE returns 204 silently regardless of row match (intentional — avoids existence leak). PATCH raises `SecretNotFound` → 404 for both "doesn't exist" and "exists but belongs to another user", which is consistent fail-closed behaviour. UNIQUE `(user_id, name)` on the `vault_secrets` table prevents cross-tenant collisions. | NONE |
| 10 | Workspace IDOR — `GET /api/workspace/{user_id}` and `/file` (`agent/api_routes.py:994-1034`) | Strict `current_user["user_id"] != user_id → 403`, no admin override. Path argument forwarded to sandbox service which enforces its own containment. | NONE |
| 11 | Agent task ownership — `/agent/{task_id}*` (`agent/api_routes.py:328-922`) | Every endpoint that accepts `task_id` calls `_load_agent_task` then checks `task.user_id != current_user["user_id"]`. UUID format validated before DB to avoid 500. `_load_agent_task` uses `$1` parameter binding. Stop endpoint takes `FOR UPDATE` row lock inside a transaction. | NONE |
| 12 | Approval decision (`POST /agent/{task_id}/approvals/{approval_id}/decide`) | Owner-scoped, decision allowlisted (`approve`/`deny`), payload parameterised through `$3::jsonb`. The endpoint does not verify that `approval_id` corresponds to a real `approval_requested` event for the task, but the orchestrator's approval matching logic is task-scoped and approval_id is task-internal — the only effect of injecting a fake approval_id is a no-op. No cross-task or cross-tenant impact. | NONE |
| 13 | Logging hygiene | `_redact_url_for_logs` strips passwords. Stripe webhook bodies are not logged (only event id/type bound). Auth tokens hashed before use as rate-limit key. No `logger.*` lines log raw request bodies, vault env, or credit-card numbers. PostgREST error bodies are logged on failure — these contain schema/error text but not user secrets. | NONE |
| 14 | DB pool / asyncpg leaks | Every `pool.acquire()` is wrapped in `async with`; standalone `pool.execute` / `pool.fetchrow` acquire+release transparently. `lifespan` closes `_db_pool` on shutdown. cache.create_redis_client `aclose()`s on ping failure (BUG-055). No `acquire()` without context-manager release found (`grep` confirmed). | NONE |
| 15 | Settlement reconciler concurrency (`agent/settlement_reconciler.py`) | `UPDATE...SET claimed_at=now() WHERE...claimed_at < now() - interval RETURNING` — concurrent reconcilers see disjoint candidate sets because the WHERE no longer matches once claimed_at is bumped. Inner SELECT uses `FOR UPDATE SKIP LOCKED`. T-01 marker-fixup short-circuits ledger replay when `ledger_applied_at IS NOT NULL`. Per-row exceptions logged + swallowed. | NONE |
| 16 | Stripe webhook signature (`api.py:5586-5749`) | `_stripe.Webhook.construct_event(raw_payload, sig_header, secret)` performs HMAC + Stripe's built-in 5-min default tolerance. Raw body via `await request.body()` before any parsing. Dual-secret rotation supported. 503 fail-closed when secret unset. Two-phase idempotency (claim/finalize). | NONE |
| 17 | Rate-limit middleware (`api.py:556-589`) | Per-process deque keyed by `user:<sha256(token)[:16]>` or `ip:<host>`. Auth-prefix paths (`/api/auth/`, `/auth/`) get `20/minute`; default `60/minute`. Health/docs endpoints skipped. Limiter operates before slowapi global limit; both are layered. No exemption pathway for admin endpoints. | NONE |
| 18 | CSRF posture | All state-changing endpoints require Bearer JWT. Browsers do not auto-attach Authorization headers, so classic CSRF does not apply. Preview cookie is owner-scoped to read-only static asset reads under `/preview/{task_id}/`, not state mutation. | NONE |
| 19 | SQL injection — dynamic identifiers / ORDER BY | All ORDER BY clauses are static literals. `update_research_task` and `update_branch` interpolate column names but only after the `_ALLOWED_*_COLUMNS` allowlist + assert. Cascade-table f-string `DELETE FROM {table}` uses static hardcoded list. All values flow through `$N` parameters. | NONE |
| 20 | SSRF in agent tools | `_browser_post` and `_sandbox_post` URLs are constructed from `_browser_base()` / `_sandbox_base()` (config-controlled) plus a hardcoded path; only the JSON payload carries user/agent-supplied URLs which are forwarded to a sandboxed browser microservice. The agent reaching arbitrary URLs is the documented user-facing capability; outbound URL validation is the browser pool's responsibility. No `httpx.get(user_supplied_url)` directly in the api/agent code paths. | NONE |
| 21 | Memory tool path traversal (`tools/memory.py:81-92`) | `user_id` validated against `_USER_ID_RE` then `(data_root / "memory" / user_id).resolve()` containment check (H-03). | NONE |

---

## Section 3 — Findings table

(empty)

---

## Section 4 — Verdict

**ZERO FINDINGS.** Phase E re-audit #24 of HEAD `89a9bfc` finds:

- X-01 fix code itself: clean. Helper, single callsite, and 4-case regression test withstand adversarial probing across alternate URL schemes, env-var aliases, second-Limiter risk, surface-string drift, and TOCTOU.
- New surfaces (21 categories): all clean. Highlights: vault DELETE silent-204 and PATCH 404 are deliberately consistent (no existence leak); the settlement reconciler's claimed_at bump is the safer alternative to long-held `FOR UPDATE` locks across slow ledger RPCs; raw-body Stripe verification is correctly sequenced before any parsing; agent task / workspace / vault ownership checks all use strict equality.

Streak resets to **1 / 3** zero-finding rounds toward zero-bug convergence.
