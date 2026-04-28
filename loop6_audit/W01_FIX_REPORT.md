# W-01 Fix Report — Centralized Redis Client Factory

Status: **FIXED 2026-04-28**
Severity: P3 (defense-in-depth coverage gap)
Branch: `loop6/zero-bug`

## Bug

Phase E re-audit #22 finding W-01: the V-01 shared `assert_local_or_tls()`
validator in `mariana/util/redis_url.py` was only invoked from the vault and
cache surfaces. The global Redis clients constructed at API startup
(`mariana/api.py:337-345`) and in the agent daemon
(`mariana/main.py:254-262`) called `redis.asyncio.from_url(config.REDIS_URL,
...)` directly with no transport-policy check. On operator misconfiguration
(`REDIS_URL=redis://remote.example.com:6379/0`) the API and daemon would
quietly bring up plaintext Redis transport for queue, pub/sub, and stop-flag
traffic while the vault/cache callsites continued to enforce `rediss://`.

This was defense-in-depth rather than a direct user-to-user exploit, but it
contradicted the one-policy-for-Redis-transport expectation the V-01/U-03
lineage created.

## Fix

Smallest blast radius: extend the existing `mariana/util/redis_url.py` helper
with a single validated factory used everywhere a Redis client is constructed
from an operator-controlled URL.

```python
def make_redis_client(url: str, *, surface: str, **kwargs):
    import redis.asyncio as aioredis  # lazy on purpose
    assert_local_or_tls(url, surface=surface)
    return aioredis.from_url(url, **kwargs)
```

The lazy import keeps the module pure-stdlib so the V-01 unit tests still run
without `redis` installed.

### Replaced callsites

Every `redis.asyncio.from_url` callsite under `mariana/` now goes through the
factory:

| File | Surface kwarg | Notes |
|------|---------------|-------|
| `mariana/api.py:337-345` | `api_startup` | API lifespan-managed singleton. |
| `mariana/main.py:254-262` (`_create_redis`) | `agent_daemon` | Daemon Redis pool. |
| `mariana/data/cache.py:create_redis_client` | `cached data` | Investigation cache (already validated; refactored through factory to remove drift). |

`mariana/vault/runtime.py` does not construct a client itself — it accepts a
caller-supplied client — so no change was needed there. Its existing
`_validate_redis_url_for_vault` wrapper continues to delegate to
`assert_local_or_tls`.

A repo-wide grep for `from_url\b` confirmed no other Redis client
constructors exist under `mariana/` (the remaining matches are unrelated
URL/domain helpers in `mariana/orchestrator/intelligence/credibility.py`).

## TDD trace

### RED at `2f5a71e`

```
$ python -m pytest -q tests/test_w01_redis_factory.py
4 failed
```

The failures hit the missing `make_redis_client` symbol and the un-routed
`api.py` / `main.py` callsites.

### GREEN after fix

```
$ python -m pytest -q tests/test_w01_redis_factory.py
4 passed

$ python -m pytest -q
385 passed, 13 skipped
```

Baseline pre-fix was 381 passed, 13 skipped; +4 = 385 matches the four new
W-01 regression tests with no other delta. V-01 / V-02 / U-03 suites still
pass.

## Regression tests

`tests/test_w01_redis_factory.py` pins:

1. `test_make_redis_client_validates_url` — factory rejects
   `redis://remote.example.com:6379` and accepts both
   `rediss://remote.example.com:6379` and `redis://localhost:6379`.
2. `test_substring_bypass_rejected_at_factory` — defense-in-depth probe
   `redis://localhost.attacker.com:6379` is rejected at the factory layer
   even though V-01 already covers it at the validator layer.
3. `test_api_redis_client_uses_factory` — drives the FastAPI lifespan with a
   mocked `make_redis_client` and asserts the API startup path constructs
   its Redis singleton through the factory with a non-empty `surface=` kwarg.
4. `test_daemon_redis_client_uses_factory` — calls
   `mariana.main._create_redis` with a mocked factory and asserts the
   daemon helper passes both the configured `REDIS_URL` and a `surface=`
   kwarg through the factory.

## Out of scope / non-goals

- DNS rebinding / hostile name resolution remains a documented limit of
  string-level URL validation (carried over from V-01).
- No change to S/T/U-01/U-02/V/Stripe/agent-settlement code beyond the
  three Redis constructor sites listed above.
