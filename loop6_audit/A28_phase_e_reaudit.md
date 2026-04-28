# A28 — Phase E Re-audit #23 (Loop 6 zero-bug convergence)

- **Audit number:** A28
- **Auditor model:** Claude Opus 4.7
- **Branch / HEAD:** `loop6/zero-bug` @ `72e8545`
- **Date:** 2026-04-28
- **Scope:** (1) adversarial probe of the W-01 fix; (2) fresh sweep of high-leverage surfaces not yet drilled in A6..A27.

---

## Section 1 — W-01 fix probe

### Methodology

1. Read the fix module `mariana/util/redis_url.py` (factory + validator).
2. Read all 3 modified callsites:
   - `mariana/api.py:337-345` (`api_startup` lifespan)
   - `mariana/main.py:255-268` (`_create_redis` daemon helper)
   - `mariana/data/cache.py:405-447` (`create_redis_client`)
3. Read regression tests `tests/test_w01_redis_factory.py` (4 cases).
4. Repo-wide greps for any direct Redis client construction:
   - `redis.asyncio.from_url`, `aioredis.from_url`, `redis.from_url`
   - `redis.Redis(`, `aioredis.Redis(`, `StrictRedis(`
   - `ConnectionPool`, `connection_pool=`
5. Verified factory cannot be bypassed via kwargs (`url` is positional-validated; `**kwargs` flow only to `aioredis.from_url`; user-supplied `ssl=False` does not undo URL-scheme validation since `assert_local_or_tls` runs before the constructor).
6. Checked for TOCTOU between validation and connect — none; the validator is sync, the lazy import is sync, and there is no `await` between `assert_local_or_tls` and `aioredis.from_url`.
7. Checked surface-string injection — `surface` is interpolated into the `ValueError` message and into log lines via `f"…for {surface}…"`; no log-injection risk because surfaces are hardcoded literal strings at every callsite (`"api_startup"`, `"agent_daemon"`, `"cached data"`).
8. Checked for env-var leakage — `_redact_url_for_logs` strips passwords before logging the URL (`api.py:279-294`).
9. Checked the lazy-import race — `import redis.asyncio as aioredis` is performed inside the function under the GIL; subsequent module-cache hit is guaranteed; no race.
10. Compared factory behaviour to the V-01 validator — semantics identical: rediss accepted unconditionally; redis only for loopback/local hostnames; no host fails closed; substring bypass (`localhost.attacker.com`) rejected (test pinned).

### Findings — W-01 fix

NONE in the W-01 module or its three updated callsites.

The repo-wide grep for `from_url` returns only the factory itself and unrelated URL-domain helpers in `mariana/orchestrator/intelligence/credibility.py:166,241` (`get_domain_from_url` — not a redis constructor).

However, see Section 2 finding **X-01** for a *related* W-01 surface coverage gap that the W-01 fix scope did not address: the `slowapi` rate-limiter `storage_uri` reaches the same operator-controlled `REDIS_URL` but does **not** flow through the validated factory because slowapi performs its own internal `redis.from_url`. This is the same defense-in-depth class as W-01 itself.

---

## Section 2 — New-surface sweep

| # | Surface | Probed | Finding |
|---|---------|--------|---------|
| 1 | Conversation deletion cascade (`api.py:2622-2654`) | Yes — relies on Supabase RLS + DB-level cascade (`updated_at` set; `Cascade delete handles messages. Unlink investigations (SET NULL).`). Also probed `delete_investigation` cascade table list at `api.py:3567-3603` — hardcoded array, ordered children-first, atomic `DELETE … RETURNING id` row claim. | NONE |
| 2 | Investigation cascade (`api.py:3567-3611`) | All 24 cascade tables hardcoded; child→parent order; per-table errors swallowed (`except Exception: pass`) but parent delete uses `RETURNING id` to detect concurrent-delete and returns 404. | NONE |
| 3 | Preview file serving (`api.py:1640-1750`) | Owner-gated via cookie/query/JWT; HMAC-signed scoped tokens (`preview\|user_id\|task_id\|exp\|sig`); path-traversal check (`\x00`, `..`); `Path.resolve` + `relative_to` containment; 404 on missing manifest to avoid existence leak. Tokens are 4-hour TTL but bound to `task_id`, can't be replayed cross-task. | NONE |
| 4 | Stream / preview HMAC tokens (`api.py:1335-1459`) | Scope-marker on preview tokens (`scope == "preview"`) prevents stream-token replay against preview, and vice versa. Constant-time compare via `hmac.compare_digest`. 5s clock-skew grace. Stable secret derivation across workers (sha256 of deployment env). | NONE |
| 5 | OAuth / magic link / signup | Delegated to Supabase via `${SUPABASE_URL}/auth/v1/user`. Mariana's auth surface is JWT verification only (`api.py:1223-1267`). No local password storage / PKCE / state to audit. | NONE |
| 6 | Admin route exposure | All `/api/admin/*` endpoints declare `Depends(_require_admin)`. Verified via `grep -n "_require_admin"` against full endpoint list (`grep "@app\." mariana/api.py`); 22 admin endpoints all guarded. `/api/health` is open, `/api/config` requires auth (VULN-C2-07), `/api/orchestrator-models` is public (model list, not sensitive). | NONE |
| 7 | Profile mass-assignment | All 3 `model_dump(exclude_none=True)` callsites are admin-only (`admin_admintasks_create/patch`, `admin_feature_flags_upsert`). No public endpoint accepts `**body` or `**payload`. `created_by` is forcibly set from `caller["user_id"]` after the dump (`api.py:8272`). | NONE |
| 8 | Logging hygiene | `_redact_url_for_logs` redacts username:password→`user:***@host`. Stripe webhook bodies are not logged (only event ID/type bound to logger). Tokens are SHA-256 truncated when used as rate-limit keys. PII (emails, names) not present in info-level logs. | NONE |
| 9 | DB pool / asyncpg leaks | `update_research_task` / `update_branch` use `async with pool.acquire() as conn:` — context manager guarantees release. `pool.execute` / `pool.fetchrow` acquire+release transparently. `lifespan` closes `_db_pool` on shutdown. `cache.create_redis_client` calls `await client.aclose()` on ping failure to avoid connection leak (BUG-055 fix). | NONE |
| 10 | Settlement reconciler edge cases | Out-of-order reversal handling in `_reconcile_pending_reversals_for_grant` (api.py:6836); idempotency keyed on `event_id` via `uq_credit_tx_idem` partial unique index; two-phase claim/finalize (B-03). Reconciler loop in `main.py:866-896` survives transient failures with try/except logging. | NONE (extensively covered in T-01, U-01..U-03, V-02 prior rounds) |
| 11 | Stripe webhook signature | `_stripe.Webhook.construct_event(payload, sig_header, secret)` performs full HMAC + timestamp tolerance check via the Stripe SDK. Dual-secret rotation supported (PRIMARY + PREVIOUS). 503 fail-closed when secret unset (B-30). Two-phase idempotency: `pending` claim before handler; `completed` only after success; 500 on handler failure for retry (B-03). | NONE |
| 12 | Rate-limit (slowapi) Redis storage URI | **FINDING X-01** — see table below. `storage_uri=os.environ.get("REDIS_URL")` is wired directly into `slowapi.Limiter` (`api.py:399-406`) without going through `assert_local_or_tls`. Same operator-misconfig case as W-01 — plaintext `redis://remote:6379` would carry rate-limit counters in cleartext while the rest of the codebase enforces TLS-only for non-loopback Redis. |
| 13 | Per-process rate limiter (`api.py:529-589`) | In-memory deque keyed by `user:<sha256(token)[:16]>` or `ip:<host>`. Per-process, but B-21 documents this as accepted fallback. No timing attack on the key (it's hashed, not the raw token). | NONE |
| 14 | CSRF on state-changing endpoints | All state-changing endpoints require Bearer JWT (Authorization header), not cookies. Therefore not subject to classic CSRF (browser does not auto-attach Authorization). Preview cookie is owner-scoped to read-only static asset reads under `/preview/{task_id}/`, not state mutation. | NONE |
| 15 | SQL injection (dynamic identifiers / ORDER BY) | All `ORDER BY` clauses use static column names (verified across api.py and data/db.py). Two `f"..."`-style UPDATEs in `data/db.py:813,1171` (`update_research_task`, `update_branch`) interpolate column names — but column names are validated against module-level allowlists (`_ALLOWED_TASK_COLUMNS`, `_ALLOWED_BRANCH_COLUMNS`) with both `set - allowlist` check **and** an `assert all(...)` defense-in-depth. Values flow through `$N` parameters. | NONE |
| 16 | Cascade-table `f"DELETE FROM {table}"` (`api.py:3601`) | Table names are hardcoded literals in the static `cascade_tables` list (`api.py:3567-3598`); not user-influenced. | NONE |

---

## Section 3 — Findings table

| Bug ID | Priority | File:line | Evidence | Suggested fix |
|--------|----------|-----------|----------|---------------|
| **X-01** | **P3** | `mariana/api.py:397-406` | `_redis_rate_limit_url = os.environ.get("REDIS_URL")` is passed *directly* to `slowapi.Limiter(storage_uri=…)` at module load. No call to `assert_local_or_tls`. slowapi's `limits` storage backend will then construct its own `redis.from_url` against this URI. If the operator misconfigures `REDIS_URL=redis://remote.example.com:6379` (plaintext, non-loopback), the API/cache/daemon callsites correctly raise via the W-01 factory, but the rate-limiter Redis traffic (counter increments keyed by user-id-sha256 or remote IP) silently flows over plaintext to a remote host. This is the **same class of defense-in-depth gap** that W-01 itself addressed for `aioredis.from_url`; the W-01 fix scope listed only the three direct constructor sites and the post-fix grep `from_url\b` did not surface slowapi because the constructor lives in third-party code. | Add a one-line validation before the slowapi constructor: `from mariana.util.redis_url import assert_local_or_tls; assert_local_or_tls(_redis_rate_limit_url, surface="rate_limit_storage")`. Place it inside the `if _redis_rate_limit_url:` branch at `api.py:400` so the module fails fast at import time on a misconfigured remote-plaintext URL, matching the fail-closed behaviour at the api/daemon/cache surfaces. Add a regression test (`tests/test_w01_slowapi_storage_uri.py`) pinning that a remote plaintext `REDIS_URL` raises at module construction; a `rediss://remote` and `redis://localhost` are accepted. |

---

## Section 4 — Verdict

**ONE FINDING.** Phase E re-audit #23 surfaces **X-01** (P3 defense-in-depth — slowapi rate-limit storage URI bypasses the V-01 / W-01 transport-policy contract). This breaks the streak of zero-finding rounds.

- W-01 fix code itself: clean. Factory, callsites, and tests pass adversarial probing.
- New surfaces: 16 categories swept; only the slowapi Redis storage URI gap surfaced.

The remediation is one line plus a 3-case regression test, mirroring the W-01 fix style.
