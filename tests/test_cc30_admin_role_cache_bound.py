"""CC-30 regression — _ADMIN_ROLE_CACHE must be bounded.

CC-30 (P3, post-CC-26 re-audit #44 Finding 4) found that the
``_ADMIN_ROLE_CACHE`` ``dict[str, tuple[float, bool]]`` had a 5-second
negative TTL but no maximum size.  An attacker authenticated with many
distinct random ``user_id`` values could grow the cache indefinitely
until the API process was recycled.

This module pins the fix:

  * Writing more than ``_ADMIN_ROLE_CACHE_MAX_ENTRIES`` (=10_000) entries
    keeps the cache bounded; only the most-recent N are retained.
  * TTL is still respected on read \u2014 an entry older than
    ``_ADMIN_ROLE_CACHE_NEGATIVE_TTL`` returns ``None`` from ``get`` and is
    evicted lazily.
  * The legacy dict API used by ``_is_admin_user`` and
    ``_clear_admin_cache`` (``get``, ``__setitem__``, ``pop``) is preserved.
"""

from __future__ import annotations

import time

import pytest


from mariana import api as api_module
from mariana.api import (
    _ADMIN_ROLE_CACHE_MAX_ENTRIES,
    _ADMIN_ROLE_CACHE_NEGATIVE_TTL,
    _BoundedTTLCache,
)


# ---------------------------------------------------------------------------
# (1) Bounded write \u2014 10_001 inserts retain only 10_000
# ---------------------------------------------------------------------------


def test_cache_bounded_to_max_entries():
    """Writing one over capacity evicts the oldest insertion."""
    cache = _BoundedTTLCache(
        maxsize=_ADMIN_ROLE_CACHE_MAX_ENTRIES,
        ttl=_ADMIN_ROLE_CACHE_NEGATIVE_TTL,
    )
    now = time.time()
    for i in range(_ADMIN_ROLE_CACHE_MAX_ENTRIES + 1):
        cache[f"user-{i:06d}"] = (now, False)
    assert len(cache) == _ADMIN_ROLE_CACHE_MAX_ENTRIES
    # Oldest insertion (user-000000) was evicted.
    assert "user-000000" not in cache
    # Most recent insertion is still present.
    assert f"user-{_ADMIN_ROLE_CACHE_MAX_ENTRIES:06d}" in cache


def test_cache_fifo_eviction_order():
    """A small cache evicts in FIFO insertion order."""
    cache = _BoundedTTLCache(maxsize=3, ttl=60.0)
    now = time.time()
    cache["a"] = (now, False)
    cache["b"] = (now, False)
    cache["c"] = (now, False)
    cache["d"] = (now, False)  # evicts "a"
    assert "a" not in cache
    assert "b" in cache
    assert "c" in cache
    assert "d" in cache
    cache["e"] = (now, False)  # evicts "b"
    assert "b" not in cache


# ---------------------------------------------------------------------------
# (2) TTL respected on read \u2014 expired entries return None
# ---------------------------------------------------------------------------


def test_cache_get_evicts_expired_entry():
    """An entry older than ttl returns None from get and is evicted."""
    cache = _BoundedTTLCache(maxsize=10, ttl=0.05)
    cache["u"] = (time.time(), False)
    assert cache.get("u") is not None
    time.sleep(0.08)  # exceed the 50 ms TTL
    assert cache.get("u") is None
    assert "u" not in cache


def test_cache_get_returns_unexpired_entry_intact():
    """Within ttl, get() returns the inserted (timestamp, value) tuple."""
    cache = _BoundedTTLCache(maxsize=10, ttl=60.0)
    inserted_at = time.time()
    cache["u"] = (inserted_at, True)
    out = cache.get("u")
    assert out == (inserted_at, True)


# ---------------------------------------------------------------------------
# (3) Legacy dict API preserved \u2014 ``pop(key, default)`` works
# ---------------------------------------------------------------------------


def test_cache_pop_returns_default_on_miss():
    cache = _BoundedTTLCache(maxsize=10, ttl=60.0)
    assert cache.pop("missing", None) is None
    sentinel = object()
    assert cache.pop("missing", sentinel) is sentinel


def test_cache_pop_removes_existing_entry():
    cache = _BoundedTTLCache(maxsize=10, ttl=60.0)
    cache["u"] = (time.time(), False)
    cache.pop("u", None)
    assert "u" not in cache


# ---------------------------------------------------------------------------
# (4) Module-level cache is the bounded type
# ---------------------------------------------------------------------------


def test_module_admin_role_cache_is_bounded_type():
    """Sanity: the live module-level cache is a _BoundedTTLCache."""
    assert isinstance(api_module._ADMIN_ROLE_CACHE, _BoundedTTLCache)
