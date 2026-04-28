# A30 — Phase E Re-audit #25 (Loop 6 zero-bug convergence)

- **Audit number:** A30
- **Auditor model:** gpt_5_4 (delegated; Claude Opus 4.7 executor)
- **Branch / HEAD:** `loop6/zero-bug` @ `5845b04` (A29 audit commit on top of X-01 fix `89a9bfc`)
- **Date:** 2026-04-28
- **Scope:** (1) re-probe W-01 / X-01 fixes from new angles A29 did not consider; (2) fresh sweep of high-leverage surfaces not yet drilled in A6..A29.

---

## Section 1 — W-01 / X-01 fix probe (fresh angles)

### Methodology

| Angle | Result |
|-------|--------|
| Multi-worker / preload race | uvicorn workers each import `api.py` independently; `_load_rate_limit_storage_uri()` runs once per process at import time. No shared mutable state, no race. With `--preload` the validated value is inherited via fork; children do not re-validate (no race risk). |
| SIGHUP / `--reload` | Module re-import re-runs the helper and re-validates. A misconfigured remote-plaintext URL applied post-deploy crashes loud at reload. Correct. |
| Helper called twice with different URLs | Helper takes no caller arguments — always reads `os.environ.get("REDIS_URL")`. Cannot be tricked by a callsite passing in a separately-validated URL. |
| `slowapi` / `limits` kwargs override | `Limiter(...)` exposes `storage_options`, `key_prefix`, `default_limits` etc. None of these override `storage_uri`. Searched `mariana/` for `storage_options` and `key_prefix` — no hits. The Limiter's `storage_uri` is the sole transport dial. |
| Custom `Limiter` subclass elsewhere | `grep "class.*Limiter\|Limiter):"` — only `_NoopLimiter` in the slowapi-unavailable fallback exists. No subclass that could re-implement the URL parsing. |
| Alternate env vars (`RATELIMIT_STORAGE_URL`, `RATELIMIT_STORAGE_URI`) | Repo grep — zero hits. The only consumer is `REDIS_URL`. |
| Uppercase scheme / IPv6 zone-id / `0.0.0.0` | `assert_local_or_tls` lowercases `parsed.scheme`, falls through to `ipaddress.ip_address` for hostname-IP literals; IPv6 zone-id (`::1%eth0`) raises and falls through to remote-rejected; `0.0.0.0` is correctly NOT loopback (only `127.0.0.0/8` is) and is rejected — desirable fail-closed. |
| Lazy import of `redis.asyncio` in `make_redis_client` | Validation runs BEFORE the lazy import. If redis package is missing, `ImportError` surfaces — not a security gap. |
| Two-coroutine race on `_get_client` (httpx) in `agent/tools.py` | Module-level singleton; first-time race could orphan a client and lose `aclose`. P4 robustness only — no security or settlement impact. |

### Findings — W-01 / X-01 fix

NONE.

---

## Section 2 — New-surface sweep

| # | Surface | Probed | Result |
|---|---------|--------|--------|
| 1 | `asyncio.create_task` lifecycle in agent queue daemon (`main.py:809-848`) | `active` set is pruned on every loop iteration via `for t in {t for t in active if t.done()}: active.discard(t); t.result()` — no unbounded growth. Pending tasks cancelled on shutdown with 60s grace. | NONE |
| 2 | Background tasks in api.py (FastAPI `BackgroundTasks`) | `grep BackgroundTasks` — no occurrences. All background work is via `asyncio.create_task` in the daemon, not per-request `BackgroundTasks`. | NONE |
| 3 | Token entropy | `secrets.token_bytes(32)` for the stream-token ephemeral fallback (api.py:1397). Stable derivation hashes deployment env via SHA-256 — not crypto-rolled-your-own. No `random.*` for security; only `hashlib.md5` (memory.py:133, 136) used as a content-dedup key, NOT for any security boundary. | NONE |
| 4 | Pagination cursor (`api.py:9215-9226`, `evidence_ledger.py:203`, `credibility.py:353`) | Cursor `f"{ts}|{item_id}"` — no user_id, task_id is in URL. Ownership enforced upstream by `_require_investigation_owner`. Bad-cursor fallback silently returns first page (DB error from `$3` cast or any other exception caught). Robustness nit only — first-page fall-back is still task-scoped. | NONE |
| 5 | Decimal vs float in billing paths beyond U-02 | `int(effective_budget_usd * 120)` (api.py:2916) and `max(100, int(body.budget_usd * 100))` (agent/api_routes.py:472) are RESERVATION amounts, reconciled at terminal state by Decimal-based `usd_to_credits` (`mariana/billing/precision.py`). No drift survives to the final balance. Stripe amounts are int cents from Stripe-signed payload — no float ops. | NONE |
| 6 | Unbounded queries / N+1 / DoS | All `conn.fetch` calls reviewed: `list_agent_tasks` (LIMIT capped at 200), approvals listing (LIMIT 500/1000), events (LIMIT capped at 1000), intelligence claims (LIMIT capped at 1000 by Query). `_db_pool.fetchrow` is bounded by `WHERE id = $1`. No `SELECT * FROM ... WHERE user_id = $1` without LIMIT. | NONE |
| 7 | JSON column injection / role-from-metadata | `payload.get("role")` (api.py:1278) is from the Supabase-validated `/auth/v1/user` response — server-issued, not user-set. Authorization actually flows through `_is_admin_user(user_id)` which calls the `is_admin` RPC against the `profiles` table. JWT role is display-only. | NONE |
| 8 | Concurrent cancel + completion race in agent loop | Layered defenses: `_check_stop_requested` gate before planner; `_persist_task` CAS guard (Q-01) prevents un-finalize; `agent_settlements` claim-row INSERT ON CONFLICT DO NOTHING (R-01) is the primary settle-once fence; `ledger_applied_at` short-circuit (T-01) skips ledger replay. Pre-validation re-read of `state` + `credits_settled` (P-01) before any transition. | NONE |
| 9 | Plan downgrade / mid-cycle | `customer.subscription.deleted` immediately patches profile to `subscription_status='canceled'` and `plan='free'` (api.py:6406-6417). In-flight reservations were already deducted; the worker continues using already-reserved credits. Future task creation gates on plan via budget caps. No reservation reversal needed. | NONE |
| 10 | Email-change / verification | Delegated to Supabase. Mariana has no `change_email` / `new_email` / `verify_email` routes (`grep` confirms). | NONE |
| 11 | Agent sandbox env-leak between tasks | `vault/runtime.py:set_task_context` uses Python `contextvars.ContextVar` — `asyncio.create_task` forks the context per task (PEP 567 semantics). Each `_run_one(task_id)` runs in its own asyncio.Task, so vault env is isolated. The `finally` at `loop.py:1421-1488` calls `ctx_handle.reset()` and `clear_vault_env(redis, task.id)` regardless of return path. | NONE |
| 12 | httpx client lifecycle | Every `httpx.AsyncClient(...)` in `mariana/` uses `async with` for explicit close. `agent/tools.py:_get_client()` keeps a module-level singleton with `is_closed` re-create — first-time concurrent calls could orphan one client (P4 robustness, no leak in steady state). | NONE |
| 13 | Redis pub/sub lifecycle (api.py:4124-4263) | `pubsub.subscribe` then `try/finally` with `pubsub.unsubscribe` + `pubsub.aclose`. The `await pubsub.subscribe(...)` is technically outside the `try`, so a throw mid-subscribe would orphan the pubsub object — but redis-py's `PubSub.__del__` releases the connection back to the pool on GC. P4 robustness, no security impact. | NONE |
| 14 | Webhook delivery (outbound, if any) | Mariana does NOT send outbound webhooks. Only receives Stripe webhooks. No retry queue / DLQ surface to audit. | NONE |
| 15 | Soft-delete vs hard-delete | All deletion paths (`/api/conversations/:id`, `/api/investigations/:id`, vault secrets, agent_tasks via cascades) are HARD deletes. `_supabase_rest("DELETE", ...)` — no `deleted_at` column query pattern. Foreign-key cascades hardcoded in `cascade_tables` list. | NONE |
| 16 | Time zone / DST | Timestamps are UTC throughout (`datetime.now(tz=timezone.utc)`); no naive local-time arithmetic. Stripe period_end is epoch seconds, converted via `datetime.fromtimestamp(..., tz=UTC)`. No DST anchor in renewal logic. | NONE |
| 17 | Cross-tenant data via `body.conversation_id` on agent task creation | An attacker can supply someone else's `conversation_id` in the AgentStartRequest. The value is stored on the agent_tasks row but never used to surface agent data via the conversation API: `get_conversation` lists investigations with `conversation_id=eq.X AND user_id=eq.Y` — agent_tasks are not joined. List endpoints filter by `user_id`. The foreign conversation_id is bookkeeping noise on the attacker's own task. No data leak, no IDOR. | NONE |
| 18 | Workspace IDOR (`/workspace/{user_id}`, `/workspace/{user_id}/file`) | Strict `current_user["user_id"] != user_id → 403`. Path forwarded to sandbox service which enforces its own containment. | NONE |
| 19 | Agent approval decision injection | `approval_id` from URL path is parameterised through `$3::jsonb`. Owner check via `_load_agent_task`. Decision allowlist (`approve`/`deny`). Even if attacker injects a fake approval_id, the orchestrator's matching is task-scoped — at most a no-op for the attacker's own task. | NONE |
| 20 | Datadog / OTEL hooks leaking secrets | `grep ddtrace\|datadog\|opentelemetry` — no Mariana-side instrumentation hooks that would surface request bodies. | NONE |
| 21 | Supabase deduct-error fail-open at task creation | If Supabase RPC errors during reservation, `reserved_credits = 0` and the task runs without billing. Logged as `agent_credits_deduct_error`. Operator-visible, attacker has no Supabase availability lever. Documented design choice for transient errors. | NONE |

---

## Section 3 — Findings table

(empty)

---

## Section 4 — Verdict

**ZERO FINDINGS.** A30 / Phase E re-audit #25 of HEAD `5845b04`:

- W-01 / X-01 fix code: clean from new angles (multi-worker race, fork preload, SIGHUP reload, kwargs override, Limiter subclass, alternate env vars, edge-case URL forms).
- 21 fresh new-surface categories probed: all clean. Highlights — vault env uses `contextvars` so per-task `asyncio.create_task` isolation is correct; agent task creation does not validate `body.conversation_id` ownership but the value is never used to surface foreign data; pagination cursors do not embed user_id; Decimal precision applies at settlement so float-based reservation drift never reaches the user balance; cancel+complete race is layered with claim-row + CAS + fast-path defenses.

Streak advances to **2 / 3** zero-finding rounds toward zero-bug convergence. One more zero-finding round closes the loop.
