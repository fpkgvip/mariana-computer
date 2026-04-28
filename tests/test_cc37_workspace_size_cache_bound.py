"""CC-37 — bound ``_WORKSPACE_SIZE_CACHE`` with TTL + FIFO eviction.

The A51 re-audit (Finding 1, Low) flagged that
``sandbox_server/app.py`` previously stored its workspace-size cache in a
plain ``dict`` keyed by workspace path string.  Each new ``user_id`` added
a fresh entry, and entries were never evicted — only refreshed in place —
so a long-running sandbox container serving many distinct user_ids would
grow the dict without bound (a quiet memory leak).

The fix mirrors the CC-30 ``_ADMIN_ROLE_CACHE`` pattern: replace the dict
with a hand-rolled ``_BoundedTTLCache`` parametrised for
``(monotonic_inserted_at, size_bytes)`` value tuples, FIFO eviction at
``_WORKSPACE_SIZE_CACHE_MAX_ENTRIES = 10_000`` distinct keys, TTL of
``_WORKSPACE_SIZE_TTL_SEC = 5.0`` enforced inside ``get``.

These tests pin the behaviour:

* bounded eviction — inserting more than ``maxsize`` distinct keys keeps
  ``len(cache) == maxsize`` and evicts in FIFO order;
* TTL expiry — an entry older than the TTL is evicted on next ``get`` and
  reports a miss;
* hit / miss correctness — the public helper still returns valid sizes
  across cache hits, cache misses, and overflow eviction;
* CC-34 invariant preserved — projected-size enforcement on ``/fs/write``
  still works after the cache swap (regression guard against the cache
  swap silently breaking the quota path).

The test file is self-contained: it imports ``sandbox_server.app`` once
under a tempdir ``WORKSPACE_ROOT`` and a deterministic
``SANDBOX_SHARED_SECRET``, mirroring the CC-34 / CC-36 fixtures.
"""

from __future__ import annotations

import importlib
import os
import tempfile

import pytest


# Sandbox app calls ``WORKSPACE_ROOT.mkdir`` + ``_configure_logging`` at
# import time and the default ``/workspace`` is unwritable in CI.  Point it
# at a tempdir before the test runner imports the module.  Match the
# CC-34 fixture so a tight ``SANDBOX_MAX_WORKSPACE_BYTES`` lets us exercise
# the projection path without huge allocations.
os.environ.setdefault("WORKSPACE_ROOT", tempfile.mkdtemp(prefix="cc37-sandbox-"))
os.environ.setdefault("SANDBOX_SHARED_SECRET", "cc37-test-secret")
os.environ.setdefault("SANDBOX_MAX_WORKSPACE_BYTES", "4096")


@pytest.fixture()
def sandbox_app():
    """Reload ``sandbox_server.app`` so cache state and env are clean."""
    import sandbox_server.app as mod  # noqa: PLC0415

    mod = importlib.reload(mod)
    mod._WORKSPACE_SIZE_CACHE.clear()
    yield mod
    mod._WORKSPACE_SIZE_CACHE.clear()


# ---------------------------------------------------------------------------
# 1. Bounded eviction — FIFO, len ≤ maxsize.
# ---------------------------------------------------------------------------


def test_cc37_bounded_eviction_fifo(sandbox_app) -> None:
    """Inserting more than ``maxsize`` keys evicts the oldest first and
    keeps ``len`` exactly at ``maxsize``."""
    cache_cls = sandbox_app._BoundedTTLCache
    cache = cache_cls(maxsize=4, ttl=60.0)
    cache["a"] = (1.0, 10)
    cache["b"] = (2.0, 20)
    cache["c"] = (3.0, 30)
    cache["d"] = (4.0, 40)
    assert len(cache) == 4
    # Insert one over capacity — oldest ("a") must drop.
    cache["e"] = (5.0, 50)
    assert len(cache) == 4
    assert "a" not in cache
    assert "b" in cache and "c" in cache and "d" in cache and "e" in cache
    # Insert another — next oldest ("b") drops.
    cache["f"] = (6.0, 60)
    assert len(cache) == 4
    assert "b" not in cache
    assert {"c", "d", "e", "f"} == set(
        k for k in ("a", "b", "c", "d", "e", "f") if k in cache
    )


def test_cc37_module_cache_is_bounded(sandbox_app) -> None:
    """The module-level cache is a ``_BoundedTTLCache`` with the documented
    capacity (10_000) and TTL (5 seconds)."""
    assert isinstance(sandbox_app._WORKSPACE_SIZE_CACHE, sandbox_app._BoundedTTLCache)
    # The tuple shape is enforced by inserting a sample value and reading
    # it back through the ``get`` method.
    sandbox_app._WORKSPACE_SIZE_CACHE.clear()
    now = sandbox_app.time.monotonic()
    sandbox_app._WORKSPACE_SIZE_CACHE["/tmp/foo"] = (now, 42)
    entry = sandbox_app._WORKSPACE_SIZE_CACHE.get("/tmp/foo")
    assert entry == (now, 42)
    # Bound and TTL pinned to the documented constants.
    assert sandbox_app._WORKSPACE_SIZE_CACHE_MAX_ENTRIES == 10_000
    assert sandbox_app._WORKSPACE_SIZE_TTL_SEC == 5.0


# ---------------------------------------------------------------------------
# 2. TTL expiry — entry older than TTL is evicted on next get.
# ---------------------------------------------------------------------------


def test_cc37_ttl_expiry_evicts_on_get(sandbox_app, monkeypatch) -> None:
    """A cached entry older than ``ttl`` is evicted on next ``get`` and
    reports a miss."""
    cache_cls = sandbox_app._BoundedTTLCache
    cache = cache_cls(maxsize=8, ttl=5.0)

    # Pin time.monotonic to a controllable clock.
    fake_now = {"t": 100.0}

    def _fake_monotonic() -> float:
        return fake_now["t"]

    monkeypatch.setattr(sandbox_app.time, "monotonic", _fake_monotonic)

    cache["k"] = (fake_now["t"], 99)
    assert cache.get("k") == (100.0, 99)

    # Advance past TTL — get must report miss AND evict.
    fake_now["t"] = 100.0 + 5.0  # exactly ttl seconds later
    assert cache.get("k") is None
    assert "k" not in cache  # confirm eviction


# ---------------------------------------------------------------------------
# 3. Hit / miss correctness — helper still returns the right size.
# ---------------------------------------------------------------------------


def test_cc37_workspace_size_bytes_hit_miss(sandbox_app, tmp_path) -> None:
    """The public helper ``_workspace_size_bytes`` continues to return the
    correct size on cache miss, cache hit, and after invalidation."""
    workspace = tmp_path / "user-cc37"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"x" * 100)

    sandbox_app._WORKSPACE_SIZE_CACHE.clear()
    # First call: cache miss — recursive stat returns 100.
    size_first = sandbox_app._workspace_size_bytes(workspace)
    assert size_first == 100
    # Subsequent call inside TTL: cache hit returns the same value even if
    # the underlying filesystem changes (this is the documented behaviour
    # of the 5-second TTL).
    (workspace / "b.txt").write_bytes(b"y" * 50)
    size_cached = sandbox_app._workspace_size_bytes(workspace)
    assert size_cached == 100  # stale-by-design within TTL window
    # Force-clear and re-read: now sees the new total.
    sandbox_app._WORKSPACE_SIZE_CACHE.clear()
    size_fresh = sandbox_app._workspace_size_bytes(workspace)
    assert size_fresh == 150


# ---------------------------------------------------------------------------
# 4. CC-34 invariant — projected quota check still works after the swap.
# ---------------------------------------------------------------------------


def test_cc37_cc34_quota_projection_still_holds(sandbox_app, tmp_path) -> None:
    """Regression guard: swapping the cache must not break the projected
    quota check.  At cap minus epsilon, a write whose ``additional_bytes``
    pushes the total over cap must still raise HTTP 507 ``workspace_full``."""
    from fastapi import HTTPException  # noqa: PLC0415

    workspace = tmp_path / "user-cc34-regress"
    workspace.mkdir()
    # SANDBOX_MAX_WORKSPACE_BYTES is set to 4096 in the test env.  Fill the
    # workspace to 4000 and try to project 200 more bytes — must reject.
    (workspace / "fill.txt").write_bytes(b"z" * 4000)

    sandbox_app._WORKSPACE_SIZE_CACHE.clear()

    # Sanity: helper sees 4000 bytes.
    assert sandbox_app._workspace_size_bytes(workspace) == 4000

    # additional_bytes that does NOT push over cap (4000 + 96 = 4096) — passes.
    sandbox_app._enforce_workspace_quota(workspace, additional_bytes=96)

    # additional_bytes that DOES push over cap (4000 + 200 = 4200 > 4096) — rejects.
    with pytest.raises(HTTPException) as excinfo:
        sandbox_app._enforce_workspace_quota(workspace, additional_bytes=200)
    assert excinfo.value.status_code == 507
    assert excinfo.value.detail == "workspace_full"
