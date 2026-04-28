# U-03 — follow-up findings

These items were observed while fixing U-03 but are out-of-scope for this
patch. None gate the U-03 close.

## 1. Redis client construction (`api.py`, `main.py`) still does not enforce TLS

`mariana/api.py:337-345` and `mariana/main.py:254-262` build the API and
worker Redis clients via `redis.asyncio.from_url(REDIS_URL, ...)` without
calling the `_validate_redis_url_for_vault` policy (or the equivalent
`mariana/data/cache.py:421-433` policy). The U-03 fix instead enforces the
policy at the vault store/fetch entry points so vault secrets cannot be
written/read over plaintext to a remote host.

This means:

* The Redis-backed agent queue, SSE event stream, stop signals, and rate
  limiters can still ride a plaintext `redis://` to a remote host.
* The vault-secrets payload is now blocked, but the **task IDs** and
  **streamed event metadata** (which can include redacted log lines, step
  names, and timing data) are not.

Recommendation: refactor cache.py + vault/runtime.py to share a single
`mariana.platform.redis.connect(...)` helper that always enforces the policy,
and route both api.py and main.py through it. Bookkeeping: tracked under
follow-up U-03B (transport-wide TLS).

## 2. Redis AUTH not required for remote hosts

`data/cache.py` and the vault path both check the SCHEME but not whether
remote URLs include credentials (`rediss://user:pass@host:6379`). A
deployment that points at a misconfigured external Redis without auth
silently works. Audit recommendation: when host is non-local AND credentials
are absent, fail closed at startup (not at first request).

## 3. Vault meta sentinel for crash recovery

`AgentTask.requires_vault` rides on the DB so the worker reload path knows to
fail-closed. If the DB is restored from a backup that pre-dates this column,
the `_row_get_bool(... default=False)` accessor will fall back to False —
i.e. tasks restored from such a backup would silently revert to the legacy
soft-fail behaviour. Idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS`
handles fresh deployments and live upgrades, but does **not** rewrite
existing rows' values; existing rows get `FALSE` by default. This is correct
for any task that was *running* at the time of upgrade (it had `vault_env={}`
in practice or it would already be failing on Redis hiccups), but operators
restoring backup data into a freshly-upgraded schema should be aware.

## 4. `clear_vault_env` still soft-fails

`clear_vault_env` is invoked from the agent loop's `finally:` block to evict
the per-task secrets from Redis when the task reaches a terminal state. We
keep the existing soft-fail behaviour there (a Redis hiccup at terminal
shouldn't cause the task to flip from DONE to FAILED), but a chronically
broken Redis means stale `vault:env:{task_id}` blobs may sit until their
TTL. The TTL is already bounded by `max_duration_hours + 5min`, so the
exposure is bounded. No change required.

## 5. Existing `mariana/data/cache.py` policy tokens drift risk

The local-host allowlist now lives in two places: `data/cache.py` (in-line)
and `vault/runtime.py:_LOCAL_REDIS_HOST_TOKENS`. They MUST stay in sync. A
future fix should extract this to a single helper. For now I matched both
sets and added the `://redis/` variant (no port) which `data/cache.py` is
arguably missing.
