"""W-01 regression tests — centralized Redis client factory.

W-01 P3 (Phase E re-audit #22) found that the V-01 shared
``assert_local_or_tls()`` validator was only invoked from the vault and cache
surfaces. The global Redis clients in ``mariana/api.py`` (API startup) and
``mariana/main.py`` (agent daemon) constructed ``redis.asyncio`` clients
directly with no policy check, so queue / pubsub / stop-flag traffic could
traverse plaintext to a remote host on operator misconfiguration.

The fix routes every ``REDIS_URL`` client construction through a single
validated factory ``mariana.util.redis_url.make_redis_client``. These tests
pin:

  * the factory itself validates URLs and rejects substring bypass attempts
    (defense in depth on top of V-01),
  * the API startup path goes through the factory,
  * the daemon ``_create_redis`` helper goes through the factory.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# (1) Factory enforces transport policy.
# ---------------------------------------------------------------------------


def test_make_redis_client_validates_url():
    """``make_redis_client`` must reject remote plaintext URLs and accept TLS / local."""
    from mariana.util.redis_url import make_redis_client

    with pytest.raises(ValueError):
        make_redis_client("redis://remote.example.com:6379", surface="queue")

    # rediss:// (TLS) and local plaintext are accepted; client object returned.
    tls_client = make_redis_client("rediss://remote.example.com:6379", surface="queue")
    assert tls_client is not None
    local_client = make_redis_client("redis://localhost:6379", surface="queue")
    assert local_client is not None


def test_substring_bypass_rejected_at_factory():
    """Defense in depth: hostile subdomain of localhost must not slip through the factory."""
    from mariana.util.redis_url import make_redis_client

    with pytest.raises(ValueError):
        make_redis_client("redis://localhost.attacker.com:6379", surface="queue")


# ---------------------------------------------------------------------------
# (2) API startup uses the factory.
# ---------------------------------------------------------------------------


def test_api_redis_client_uses_factory(monkeypatch):
    """The API startup lifespan must build its Redis client through ``make_redis_client``."""
    # Provide minimum env so load_config() succeeds inside the lifespan.
    monkeypatch.setenv("POSTGRES_PASSWORD", "test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    import mariana.api as api_mod

    # Capture calls without performing real network IO.
    fake_redis = MagicMock()
    fake_redis.ping = AsyncMock(return_value=b"PONG")
    fake_redis.aclose = AsyncMock()
    factory = MagicMock(return_value=fake_redis)

    monkeypatch.setattr(api_mod, "make_redis_client", factory, raising=True)

    # Stand in for the DB pool so the lifespan does not try real Postgres.
    fake_pool = MagicMock()
    fake_pool.close = AsyncMock()
    create_pool = AsyncMock(return_value=fake_pool)
    init_schema = AsyncMock()
    monkeypatch.setattr(api_mod, "create_pool", create_pool, raising=True)
    monkeypatch.setattr(api_mod, "init_schema", init_schema, raising=True)

    async def _drive() -> None:
        async with api_mod.lifespan(api_mod.app):  # type: ignore[attr-defined]
            pass

    asyncio.run(_drive())

    assert factory.called, "API startup did not call make_redis_client"
    # surface kwarg must be supplied so the validator error message is contextful.
    _, kwargs = factory.call_args
    assert "surface" in kwargs and kwargs["surface"]


# ---------------------------------------------------------------------------
# (3) Daemon _create_redis uses the factory.
# ---------------------------------------------------------------------------


def test_daemon_redis_client_uses_factory(monkeypatch):
    """``mariana.main._create_redis`` must build its client via ``make_redis_client``."""
    import mariana.main as main_mod

    sentinel = object()
    factory = MagicMock(return_value=sentinel)
    monkeypatch.setattr(main_mod, "make_redis_client", factory, raising=True)

    cfg = MagicMock()
    cfg.REDIS_URL = "redis://localhost:6379/0"

    result = asyncio.run(main_mod._create_redis(cfg))
    assert result is sentinel
    assert factory.called, "daemon _create_redis did not call make_redis_client"
    args, kwargs = factory.call_args
    # URL is the first positional or keyword.
    passed_url = args[0] if args else kwargs.get("url")
    assert passed_url == "redis://localhost:6379/0"
    assert "surface" in kwargs and kwargs["surface"]
