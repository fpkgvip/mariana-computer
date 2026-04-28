# U-03 Fix Report — Vault / Redis transport TLS + fail-closed on Redis errors

- **Severity:** P2
- **Surface:** Task-scoped vault secret transport (Redis at `vault:env:{task_id}`)
- **Detected by:** Phase E re-audit #20 (`loop6_audit/A25_phase_e_reaudit.md` Probe 5)
- **Fixed:** 2026-04-28 on branch `loop6/zero-bug`

---

## Root cause

Two independent gaps converged on the per-task vault feature:

1.  **Plaintext transport allowed.** The vault env (per-task secrets) was JSON-stored
    in Redis under `vault:env:{task_id}`. The API and the worker built their Redis
    clients via `redis.asyncio.from_url(config.REDIS_URL, ...)` without enforcing
    `rediss://` for non-local hosts (`mariana/api.py:337-345`,
    `mariana/main.py:254-262`, default `redis://redis:6379/0` in
    `mariana/config.py:124`). The cache path at `mariana/data/cache.py:421-433`
    *did* enforce `rediss://` for non-loopback hosts (M-05 fix), so the vault path
    silently diverged from the platform's own transport policy.

2.  **Silent fail-open on Redis error.** `store_vault_env` swallowed Redis errors
    and returned success; `fetch_vault_env` returned `{}` on miss/error. Net
    result: a task that was created with non-empty `vault_env` (i.e. the user
    explicitly asked for secrets to be injected) could be enqueued and run with
    NO secrets injected if Redis was down, evicted under memory pressure, or
    expired before the worker started. That silently changes task behaviour
    instead of fail-closing.

## Mechanism of fix

### 1. URL transport policy (mirrors M-05 `data/cache.py`)

`mariana/vault/runtime.py` now provides:

- `_validate_redis_url_for_vault(url)` — raises `ValueError` if `url` is plain
  `redis://` and the host is not in the local allowlist
  `{localhost, 127.*, [::1], redis (Docker service name)}`. The same allowlist
  the cache enforces. Empty / `None` URLs are treated as local and ignored.

This is enforced from inside `store_vault_env` (when env is non-empty) and from
`fetch_vault_env` (when `requires_vault=True`), so the vault path can never use
plaintext Redis to a remote host even if a future caller forgets to validate at
client-construction time.

### 2. Fail-closed contract — `VaultUnavailableError`

A new exception `VaultUnavailableError` is raised by:

- `store_vault_env` — when env is **non-empty** and Redis is `None` or its
  `SET` raises. Empty env keeps the original no-op behaviour (back-compat).
- `fetch_vault_env(..., requires_vault=True)` — when Redis is `None`, the `GET`
  raises, OR the returned payload is missing/empty. The legacy
  `requires_vault=False` path still returns `{}` on miss/error so tasks without
  a vault never gain a Redis dependency.

### 3. State threading — `AgentTask.requires_vault`

Added a persisted boolean `requires_vault` to:

- `mariana/agent/models.py` (Pydantic field, default False, `extra="forbid"` is
  preserved by adding the field with a default).
- `mariana/agent/schema.sql` (column on `agent_tasks` plus
  `ALTER TABLE … ADD COLUMN IF NOT EXISTS requires_vault BOOLEAN NOT NULL
  DEFAULT FALSE` for in-place upgrade).
- `_insert_agent_task` / `_load_agent_task` in `mariana/agent/api_routes.py`
  (round-trips the column with a `_row_get_bool` defensive accessor for older
  rows that pre-date the column).

This flag is the worker's source of truth: the agent loop reads
`task.requires_vault` and passes it to `fetch_vault_env(..., requires_vault=...)`
so it knows whether to fail-closed.

### 4. Callsite updates

- **`mariana/agent/api_routes.py:526-575`** — task creation now passes the
  configured `REDIS_URL` to `store_vault_env`. On either `VaultUnavailableError`
  or `ValueError` (URL policy), the route:
    1. logs the structured event,
    2. refunds the credit reservation via `_supabase_add_credits`,
    3. updates the `agent_tasks` row to `state='failed'` with a clear `error`,
    4. raises `HTTPException(503, …)`.

- **`mariana/agent/loop.py:1138-1201`** — `run_agent_task` now reads
  `task.requires_vault`, threads it into `fetch_vault_env`, and on
  `VaultUnavailableError` / `ValueError` aborts the task to `FAILED` with a
  user-visible `error` BEFORE any tool execution. Non-vault tasks preserve the
  original soft-fail behaviour (logs a warning and continues with empty env).

## Tests

`tests/test_u03_vault_redis_safety.py` (7 new tests):

| Test | What it pins |
| --- | --- |
| `test_rediss_enforced_for_remote_redis` | `redis://example.com`, `redis://10.0.0.5`, `redis://prod-redis.internal` all raise `ValueError`; `rediss://` variants pass. |
| `test_local_redis_allowed_plain` | `redis://localhost`, `redis://127.0.0.1`, `redis://[::1]`, `redis://redis` (Docker compose service name) all allowed. |
| `test_store_failure_with_requested_vault_fails_task` | Non-empty env + Redis `SET` raises ⇒ `VaultUnavailableError` (instead of silent success). |
| `test_fetch_failure_with_requested_vault_fails_task` | `requires_vault=True` + Redis `GET` raises ⇒ `VaultUnavailableError`; legacy `requires_vault=False` still returns `{}`. |
| `test_fetch_missing_with_requires_vault_fails` | Even with a healthy Redis, missing payload + `requires_vault=True` ⇒ `VaultUnavailableError` (covers TTL eviction). |
| `test_no_vault_no_redis_dependency` | Empty env / `requires_vault=False` never raises and never touches Redis. |
| `test_store_validates_url_when_provided` | Plaintext `redis_url=...` to a remote host raises before any IO; `rediss://` succeeds. |

Plus a pinned update to the existing
`tests/test_vault_runtime.py::test_store_vault_env_no_redis_is_noop` which used
to assert the (buggy) silent-success path on `redis=None`. That test now pins
the back-compat semantics: empty env on `None` redis is a true no-op.

## Test count delta

Baseline: 359 passed, 13 skipped.
After fix: 366 passed, 13 skipped (+7).

## Files touched

- `mariana/vault/runtime.py` (new exception, URL validator, fail-closed
  semantics, new `requires_vault` / `redis_url` kwargs).
- `mariana/agent/models.py` (`AgentTask.requires_vault`).
- `mariana/agent/schema.sql` (column + idempotent `ALTER TABLE`).
- `mariana/agent/api_routes.py` (insert/select column, set on creation, 503 +
  refund + DB error stamp on vault failure).
- `mariana/agent/loop.py` (thread `requires_vault` through to fetch, FAIL the
  task pre-tool-dispatch on `VaultUnavailableError`).
- `tests/test_u03_vault_redis_safety.py` (new — 7 cases).
- `tests/test_vault_runtime.py` (pinned existing test to back-compat contract).
- `loop6_audit/REGISTRY.md` (U-03 row → FIXED 2026-04-28).

## Error-surface contract (locked)

- `POST /api/agent` with `vault_env != {}` and Redis unavailable ⇒
  `503 {"detail": "Vault storage unavailable; cannot honour requested secrets"}`,
  reservation refunded, task row stamped FAILED.
- `POST /api/agent` with `REDIS_URL=redis://prod-host:6379` (plaintext, non-local)
  ⇒ `503 {"detail": "Vault transport policy violation; refusing to store secrets"}`,
  reservation refunded.
- Worker run with `task.requires_vault=True` and Redis fetch failure ⇒ task
  ends in `FAILED` with `error="Vault unavailable: …"` BEFORE any tool runs.
- Worker run with `task.requires_vault=False` (no vault requested) ⇒ unchanged:
  Redis errors are warnings, task proceeds with empty env (the legacy path).

## Out-of-scope

- The Redis client construction in `mariana/api.py` and `mariana/main.py` is
  intentionally left as `from_url` without TLS enforcement — that path serves
  the queue, SSE streaming, and other non-secret traffic and a global change
  there is broader than U-03. The vault now validates the URL on demand
  whenever it is asked to store/fetch secrets, which is sufficient for the
  confidentiality guarantee called out in the audit.
- AUTH (password) requirement for remote Redis is not enforced here; that
  belongs in a follow-up alongside the same change for the cache path. Tracked
  in `loop6_audit/U03_followup_findings.md`.

## Commit & push

- Commit subject: `U-03 fix vault redis TLS enforcement and fail-closed on store/fetch failures`
- Branch: `loop6/zero-bug`
- Pushed to origin via `git push` with `api_credentials=["github"]`. No `--force`.
