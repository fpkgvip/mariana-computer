"""U-03 regression tests — Vault / Redis transport safety + fail-closed.

Bug U-03 P2 (Phase E re-audit #20) found that:

1.  Per-task vault secrets (``vault_env``) are JSON-stored in Redis at
    ``vault:env:{task_id}`` but the API/worker built the Redis client via
    plain ``redis.asyncio.from_url(REDIS_URL, ...)`` without enforcing
    ``rediss://`` for non-local hosts (unlike ``mariana/data/cache.py``).

2.  ``store_vault_env`` swallowed Redis errors and ``fetch_vault_env``
    returned ``{}`` on Redis errors — a Redis outage caused a task that
    REQUESTED specific env vars to run with NO secrets injected, instead
    of fail-closing.

This test module pins the fix:

  * ``test_rediss_enforced_for_remote_redis``      — TLS required for non-local
  * ``test_local_redis_allowed_plain``             — local hosts whitelisted
  * ``test_store_failure_with_requested_vault_fails_task``
                                                   — store error + non-empty env raises
  * ``test_fetch_failure_with_requested_vault_fails_task``
                                                   — fetch error + requires_vault raises
  * ``test_no_vault_no_redis_dependency``          — empty env never touches Redis
"""

from __future__ import annotations

import asyncio

import pytest

from mariana.vault.runtime import (
    REDIS_KEY_FMT,
    VaultUnavailableError,
    _validate_redis_url_for_vault,
    fetch_vault_env,
    store_vault_env,
)


# ---------------------------------------------------------------------------
# (1) URL policy — rediss:// required for non-local hosts
# ---------------------------------------------------------------------------


def test_rediss_enforced_for_remote_redis():
    """Plain ``redis://`` to a non-local host must raise ValueError."""
    with pytest.raises(ValueError):
        _validate_redis_url_for_vault("redis://example.com:6379/0")
    with pytest.raises(ValueError):
        _validate_redis_url_for_vault("redis://10.0.0.5:6379")
    with pytest.raises(ValueError):
        _validate_redis_url_for_vault("redis://prod-redis.internal:6380/0")
    # rediss:// (TLS) variants of the same hosts are fine.
    _validate_redis_url_for_vault("rediss://example.com:6379/0")
    _validate_redis_url_for_vault("rediss://10.0.0.5:6379")


def test_local_redis_allowed_plain():
    """Loopback / docker-compose service names may use plaintext."""
    # Must not raise.
    _validate_redis_url_for_vault("redis://localhost:6379")
    _validate_redis_url_for_vault("redis://127.0.0.1:6379/0")
    _validate_redis_url_for_vault("redis://[::1]:6379/0")
    _validate_redis_url_for_vault("redis://redis:6379/0")  # docker-compose service name


# ---------------------------------------------------------------------------
# (2) store fail-closed when env is non-empty
# ---------------------------------------------------------------------------


class _RaisingRedis:
    """A fake redis whose ``set`` and ``get`` always raise."""

    def __init__(self) -> None:
        self.set_calls = 0
        self.get_calls = 0

    async def set(self, *_a, **_kw):
        self.set_calls += 1
        raise RuntimeError("redis is down")

    async def get(self, *_a, **_kw):
        self.get_calls += 1
        raise RuntimeError("redis is down")

    async def delete(self, *_a, **_kw):
        return 0


class _OkRedis:
    """A fake redis with in-memory storage."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, k, v, ex=None):
        self.store[k] = v

    async def get(self, k):
        return self.store.get(k)

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)


def test_store_failure_with_requested_vault_fails_task():
    """Store with a non-empty env on a broken Redis MUST raise VaultUnavailableError.

    Previously ``store_vault_env`` swallowed the exception and returned
    success, so the task was enqueued but the secrets were never written.
    """
    async def _go():
        r = _RaisingRedis()
        with pytest.raises(VaultUnavailableError):
            await store_vault_env(
                r, "task-abc", {"OPENAI_API_KEY": "sk-aaaaaaaaaaaa"},
                ttl_seconds=900,
            )
        assert r.set_calls == 1  # we did try

    asyncio.run(_go())


def test_fetch_failure_with_requested_vault_fails_task():
    """Fetch with ``requires_vault=True`` on a broken Redis MUST raise.

    Previously ``fetch_vault_env`` returned ``{}``, silently stripping
    the secrets the task requested.
    """
    async def _go():
        r = _RaisingRedis()
        with pytest.raises(VaultUnavailableError):
            await fetch_vault_env(r, "task-abc", requires_vault=True)
        # And without requires_vault, the legacy behaviour stays no-op.
        out = await fetch_vault_env(r, "task-abc", requires_vault=False)
        assert out == {}

    asyncio.run(_go())


def test_fetch_missing_with_requires_vault_fails():
    """Even when Redis is up but the task's env blob is missing
    (e.g. evicted, never stored), a task that requires vault MUST fail —
    not silently run without secrets."""
    async def _go():
        r = _OkRedis()  # alive but empty
        with pytest.raises(VaultUnavailableError):
            await fetch_vault_env(r, "task-missing", requires_vault=True)
        # No requirement → legacy {} on miss.
        out = await fetch_vault_env(r, "task-missing", requires_vault=False)
        assert out == {}

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# (3) backwards compat — empty env, no Redis dep
# ---------------------------------------------------------------------------


def test_no_vault_no_redis_dependency():
    """Task with empty vault_env is unaffected even when Redis is None / down."""
    async def _go():
        # Store: empty env on None redis → no-op, no raise.
        await store_vault_env(None, "task-x", {}, ttl_seconds=900)
        # Store: empty env on broken redis → no-op, no raise (no SET attempted).
        broken = _RaisingRedis()
        await store_vault_env(broken, "task-x", {}, ttl_seconds=900)
        assert broken.set_calls == 0
        # Fetch: requires_vault=False on None redis → {}.
        out = await fetch_vault_env(None, "task-x", requires_vault=False)
        assert out == {}
        # Fetch: requires_vault=False on broken redis → {} (legacy).
        out = await fetch_vault_env(broken, "task-x", requires_vault=False)
        assert out == {}

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# (4) URL validation is enforced from the public store/fetch entry points
# ---------------------------------------------------------------------------


def test_store_validates_url_when_provided():
    """store_vault_env honours a remote_url=... argument and rejects plaintext."""
    async def _go():
        r = _OkRedis()
        with pytest.raises(ValueError):
            await store_vault_env(
                r, "t", {"FOO": "bar_value_long_enough"},
                ttl_seconds=900, redis_url="redis://prod.example.com:6379",
            )
        # rediss:// is fine.
        await store_vault_env(
            r, "t", {"FOO": "bar_value_long_enough"},
            ttl_seconds=900, redis_url="rediss://prod.example.com:6379",
        )

    asyncio.run(_go())
