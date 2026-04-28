# X-01 Fix Report — slowapi rate-limit storage_uri transport policy

Status: **FIXED 2026-04-28**
Severity: P3 (defense-in-depth coverage gap)
Branch: `loop6/zero-bug`

## Bug

Phase E re-audit #23 finding X-01: the V-01 / W-01 shared
`assert_local_or_tls()` validator covers every `redis.asyncio.from_url`
callsite under `mariana/`, but the slowapi rate-limiter at
`mariana/api.py:397-406` was constructed as

```python
_redis_rate_limit_url = os.environ.get("REDIS_URL") or None
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["60/minute"],
    storage_uri=_redis_rate_limit_url,
)
```

slowapi/`limits` performs its own internal `redis.from_url(storage_uri)` —
the URL never flows through our factory. On operator misconfiguration
(`REDIS_URL=redis://remote.example.com:6379`) the api/daemon/cache surfaces
correctly raise via the W-01 factory, but rate-limit counters (keyed by
sha256-truncated user-id or remote IP) would silently traverse plaintext to
the remote host. Same class of defense-in-depth gap that W-01 itself
addressed for `aioredis.from_url`. Missed because slowapi's constructor
lives in third-party code, so the W-01 post-fix `from_url\b` grep only
saw the `mariana/` callsites.

## Fix

Smallest blast radius: extract a tiny helper in `mariana/api.py` that
performs the env read and routes the URL through the existing V-01
validator before anything else consumes it. The slowapi Limiter then
consumes the validated module-level constant unchanged.

```python
from mariana.util.redis_url import assert_local_or_tls as _assert_local_or_tls


def _load_rate_limit_storage_uri() -> str | None:
    url = os.environ.get("REDIS_URL") or None
    _assert_local_or_tls(url, surface="rate_limit_storage")
    return url


_redis_rate_limit_url: str | None = _load_rate_limit_storage_uri()
```

The validator is a pure-stdlib function so this adds no new dependency, no
import-order issue, and no runtime cost beyond a single `urlparse` call at
module-load time. Empty / unset URLs are tolerated (returns `None`) so the
slowapi in-memory fallback path is unaffected. A misconfigured
plaintext-remote URL now raises `ValueError` at `import mariana.api` time,
matching the fail-closed behaviour at the api/daemon/cache surfaces.

### Callsite changed

| File | Region | Notes |
|------|--------|-------|
| `mariana/api.py:397` | env read + module-level `_redis_rate_limit_url` assignment | Now goes through `_load_rate_limit_storage_uri()`, which calls `assert_local_or_tls(url, surface="rate_limit_storage")` before returning the same string slowapi receives. |

No other callsites required changes. The helper is the single source of
truth for the rate-limit storage URL across the api module.

## TDD trace

### RED at `5af7c37`

```
$ python -m pytest -x --tb=short tests/test_x01_rate_limit_storage_url.py
FAILED tests/test_x01_rate_limit_storage_url.py::test_rate_limit_storage_rejects_substring_bypass
AttributeError: module 'mariana.api' has no attribute '_load_rate_limit_storage_uri'
```

The failure hit the missing helper symbol and the un-validated env-var read.

### GREEN after fix

```
$ python -m pytest -x --tb=short tests/test_x01_rate_limit_storage_url.py
4 passed in 1.68s

$ python -m pytest --tb=short
389 passed, 13 skipped
```

Baseline pre-fix was 385 passed, 13 skipped; +4 = 389 matches the four new
X-01 regression tests with no other delta. W-01 / V-01 / V-02 / U-03 / B-21
suites still pass unchanged.

## Regression tests

`tests/test_x01_rate_limit_storage_url.py` pins:

1. `test_rate_limit_storage_rejects_substring_bypass` — hostile subdomain
   `redis://localhost.attacker.com:6379` is rejected when
   `_load_rate_limit_storage_uri()` is called with that env var.
2. `test_rate_limit_storage_rejects_remote_plaintext` — plain remote
   `redis://remote.example.com:6379` is rejected at the rate-limit surface
   the same way it is rejected at the api/daemon/cache surfaces.
3. `test_rate_limit_storage_accepts_safe_urls` — local plaintext
   (`redis://localhost:6379`), TLS-remote (`rediss://remote.example.com:6379`),
   and unset / empty URLs (returns `None`) are all accepted.
4. `test_api_module_uses_validated_storage_uri` — pins that the
   module-level `_redis_rate_limit_url` constant slowapi consumes equals
   the helper output, so a future refactor that bypasses the helper and
   reads `os.environ` directly trips this test.

## Out of scope / non-goals

- DNS rebinding / hostile name resolution remains a documented limit of
  string-level URL validation (carried over from V-01 / W-01).
- No change to the slowapi/limits library itself — fix is a pre-validation
  guard at the URL boundary, not a wrapper around the third-party client.
- No change to the existing api/daemon/cache callsites or the W-01 factory.
