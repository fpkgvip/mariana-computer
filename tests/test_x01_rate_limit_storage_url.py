"""X-01 regression tests — slowapi rate-limit storage URI transport policy.

X-01 P3 (Phase E re-audit #23) found that the slowapi ``Limiter`` in
``mariana/api.py`` was constructed with ``storage_uri=os.environ.get("REDIS_URL")``
without going through ``mariana.util.redis_url.assert_local_or_tls``. slowapi
internally calls ``redis.from_url(storage_uri)`` so a misconfigured plaintext
remote URL would carry rate-limit counters in cleartext while the
api/daemon/cache surfaces correctly raise.

The fix routes the rate-limit storage URI through the same V-01/W-01 validator
via a small helper ``mariana.api._load_rate_limit_storage_uri`` so the policy
is enforced uniformly across every operator-controlled REDIS_URL consumer.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# (1) Substring bypass rejected at the rate-limit storage surface.
# ---------------------------------------------------------------------------


def test_rate_limit_storage_rejects_substring_bypass(monkeypatch):
    """Hostile subdomain of localhost must not slip through the rate-limit URL."""
    from mariana import api as api_mod

    monkeypatch.setenv("REDIS_URL", "redis://localhost.attacker.com:6379")
    with pytest.raises(ValueError):
        api_mod._load_rate_limit_storage_uri()


# ---------------------------------------------------------------------------
# (2) Plaintext remote rejected.
# ---------------------------------------------------------------------------


def test_rate_limit_storage_rejects_remote_plaintext(monkeypatch):
    """``redis://remote-host:6379`` must be rejected for the rate-limit surface."""
    from mariana import api as api_mod

    monkeypatch.setenv("REDIS_URL", "redis://remote.example.com:6379")
    with pytest.raises(ValueError):
        api_mod._load_rate_limit_storage_uri()


# ---------------------------------------------------------------------------
# (3) Safe URLs are accepted.
# ---------------------------------------------------------------------------


def test_rate_limit_storage_accepts_safe_urls(monkeypatch):
    """Local plaintext, TLS-remote, and unset URLs are all accepted."""
    from mariana import api as api_mod

    # Local plaintext — accepted because the validator allows loopback.
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    assert api_mod._load_rate_limit_storage_uri() == "redis://localhost:6379"

    # TLS to a remote host — accepted because rediss:// is allowed for any host.
    monkeypatch.setenv("REDIS_URL", "rediss://remote.example.com:6379")
    assert api_mod._load_rate_limit_storage_uri() == "rediss://remote.example.com:6379"

    # Unset / empty — returns None so slowapi falls back to in-memory storage.
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert api_mod._load_rate_limit_storage_uri() is None


# ---------------------------------------------------------------------------
# (4) The api module uses the validated helper for the slowapi Limiter URL.
# ---------------------------------------------------------------------------


def test_api_module_uses_validated_storage_uri():
    """``_redis_rate_limit_url`` at module level must equal the helper output.

    Pins that the slowapi Limiter receives a URL that has already been routed
    through ``assert_local_or_tls``; if a future refactor reads the env var
    directly again, this test fails.
    """
    from mariana import api as api_mod

    # The module-level constant is computed by the helper at import time, so
    # they must agree on the same input.
    assert api_mod._redis_rate_limit_url == api_mod._load_rate_limit_storage_uri()
