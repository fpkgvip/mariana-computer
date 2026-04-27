"""G-01 regression: _get_upload_lock must return the same Lock instance for the
same target across calls, even if intervening GC sweeps occur. The prior
WeakValueDictionary implementation could return distinct Lock instances for the
same target_id when the caller's reference went out of scope, allowing
concurrent requests to bypass mutual exclusion.
"""
from __future__ import annotations

import asyncio
import gc

import pytest

from mariana import api as api_module


def test_same_target_returns_same_lock():
    api_module._upload_locks.clear()
    lock_a = api_module._get_upload_lock("target-A")
    lock_b = api_module._get_upload_lock("target-A")
    assert lock_a is lock_b


def test_lock_persists_across_gc_when_reference_dropped():
    """The fundamental G-01 failure mode: caller's local variable goes out of
    scope (e.g. across an ``await``), GC runs, the dict is supposed to still
    hold the lock so the next call gets the SAME instance.
    """
    api_module._upload_locks.clear()

    def _get_then_drop():
        return id(api_module._get_upload_lock("target-B"))

    first_id = _get_then_drop()
    gc.collect()
    gc.collect()
    second_id = id(api_module._get_upload_lock("target-B"))
    assert first_id == second_id, (
        "Lock identity changed across GC; mutual exclusion is broken. "
        "WeakValueDictionary regression."
    )


def test_different_targets_return_different_locks():
    api_module._upload_locks.clear()
    a = api_module._get_upload_lock("alpha")
    b = api_module._get_upload_lock("beta")
    assert a is not b


@pytest.mark.asyncio
async def test_concurrent_acquire_serializes():
    """Two coroutines targeting the same id must serialize through the same
    Lock instance. If the lock dictionary returned distinct instances they
    would run concurrently and the order would be non-deterministic; with a
    proper shared lock the second waiter only enters after the first releases.
    """
    api_module._upload_locks.clear()
    target = "race-target"
    order: list[str] = []

    async def worker(label: str, hold_ms: int):
        async with api_module._get_upload_lock(target):
            order.append(f"enter-{label}")
            await asyncio.sleep(hold_ms / 1000)
            order.append(f"exit-{label}")

    await asyncio.gather(worker("A", 50), worker("B", 5))
    # Critical sections must not interleave.
    assert order in (
        ["enter-A", "exit-A", "enter-B", "exit-B"],
        ["enter-B", "exit-B", "enter-A", "exit-A"],
    ), f"Critical sections interleaved: {order}"


def test_lru_evicts_only_unheld_locks():
    api_module._upload_locks.clear()
    held = api_module._get_upload_lock("held")

    async def _hold():
        async with held:
            # While held, fill the cache to force eviction pressure.
            saved = api_module._UPLOAD_LOCK_CACHE_MAX
            try:
                api_module._UPLOAD_LOCK_CACHE_MAX = 4
                for i in range(20):
                    api_module._get_upload_lock(f"filler-{i}")
                # Held lock must still be present.
                assert "held" in api_module._upload_locks
                assert api_module._upload_locks["held"] is held
            finally:
                api_module._UPLOAD_LOCK_CACHE_MAX = saved

    asyncio.get_event_loop().run_until_complete(_hold()) if False else asyncio.run(_hold())
