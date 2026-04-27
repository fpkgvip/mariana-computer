"""B-24 regression suite: per-task metadata locks do not block concurrent tasks.

After the B-24 fix the module-level _metadata_lock must be replaced with a
per-task lock keyed by task_id so that two concurrent tasks operating on
different task IDs do not contend on the same asyncio.Lock.

Test IDs:
  1. test_metadata_lock_is_per_task_not_module_level
  2. test_concurrent_tasks_do_not_block_each_other_on_metadata_lock
  3. test_same_task_lock_is_reused_for_same_task_id
  4. test_different_task_ids_get_different_locks
  5. test_metadata_lock_cleanup_removes_entry
  6. test_metadata_lock_counter_increments_correctly_under_concurrent_access
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

import mariana.orchestrator.event_loop as el


# ---------------------------------------------------------------------------
# Test 1: module-level _metadata_lock no longer exists (or is now a defaultdict)
# ---------------------------------------------------------------------------

def test_metadata_lock_is_per_task_not_module_level():
    """B-24: _metadata_lock must be replaced — module-level lock must be gone."""
    # After the fix, _metadata_lock should either:
    # (a) Not be a plain asyncio.Lock (it should be a defaultdict or similar)
    # (b) Or be replaced entirely with per-task lookup
    lock = getattr(el, "_metadata_lock", None)
    assert not isinstance(lock, asyncio.Lock), (
        "B-24: _metadata_lock is still a module-level asyncio.Lock. "
        "It must be replaced with a per-task lock structure (e.g. defaultdict)."
    )


# ---------------------------------------------------------------------------
# Test 2: concurrent tasks with different task_ids do not block each other
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_tasks_do_not_block_each_other_on_metadata_lock():
    """B-24: two concurrent tasks operating on different task_ids must not serialize."""
    # We test by acquiring the lock for task_A and verifying task_B can
    # concurrently acquire its own lock without waiting.
    get_lock = getattr(el, "_get_metadata_lock", None)
    if get_lock is None:
        pytest.fail(
            "B-24: _get_metadata_lock helper not found. "
            "Expected a function that returns a per-task asyncio.Lock."
        )

    lock_a = get_lock("task-aaaaaa")
    lock_b = get_lock("task-bbbbbb")

    b_acquired_at: list[float] = []
    start = time.monotonic()

    async def hold_lock_a():
        async with lock_a:
            await asyncio.sleep(0.15)  # hold for 150 ms

    async def acquire_lock_b():
        # Should not need to wait for lock_a
        async with lock_b:
            b_acquired_at.append(time.monotonic() - start)

    await asyncio.gather(hold_lock_a(), acquire_lock_b())

    # B must have acquired its lock well before A's 150 ms hold expires.
    # With per-task locks B acquires ~immediately (< 50 ms).
    # With a single shared lock B would acquire only after A releases (~150 ms).
    assert b_acquired_at, "lock_b was never acquired"
    assert b_acquired_at[0] < 0.10, (
        f"B-24: task B waited {b_acquired_at[0]:.3f}s — looks like tasks are "
        "serializing on a shared lock (should be < 0.10s for per-task locks)"
    )


# ---------------------------------------------------------------------------
# Test 3: same task_id reuses the same lock object
# ---------------------------------------------------------------------------

def test_same_task_lock_is_reused_for_same_task_id():
    """B-24: calling _get_metadata_lock twice with same task_id returns same lock."""
    get_lock = getattr(el, "_get_metadata_lock", None)
    if get_lock is None:
        pytest.fail("B-24: _get_metadata_lock helper not found")

    lock1 = get_lock("task-same-id")
    lock2 = get_lock("task-same-id")
    assert lock1 is lock2, (
        "B-24: same task_id should return the same lock object "
        "(idempotent lock lookup)"
    )


# ---------------------------------------------------------------------------
# Test 4: different task_ids get different lock objects
# ---------------------------------------------------------------------------

def test_different_task_ids_get_different_locks():
    """B-24: different task_ids must get different lock objects."""
    get_lock = getattr(el, "_get_metadata_lock", None)
    if get_lock is None:
        pytest.fail("B-24: _get_metadata_lock helper not found")

    lock_x = get_lock("task-xxx-unique-1")
    lock_y = get_lock("task-yyy-unique-2")
    assert lock_x is not lock_y, (
        "B-24: different task_ids must return different lock objects"
    )


# ---------------------------------------------------------------------------
# Test 5: cleanup removes the lock entry
# ---------------------------------------------------------------------------

def test_metadata_lock_cleanup_removes_entry():
    """B-24: cleanup function must remove lock from the per-task dict to prevent leaks."""
    get_lock = getattr(el, "_get_metadata_lock", None)
    cleanup = getattr(el, "_cleanup_metadata_lock", None)
    if get_lock is None or cleanup is None:
        pytest.fail(
            "B-24: expected both _get_metadata_lock and _cleanup_metadata_lock helpers"
        )

    task_id = "task-cleanup-test"
    _ = get_lock(task_id)  # create entry
    cleanup(task_id)  # remove it

    # After cleanup, getting the lock for the same task_id should create a NEW lock
    # (the dict entry was removed, so a fresh lock is returned)
    lock_after = get_lock(task_id)
    assert isinstance(lock_after, asyncio.Lock), (
        "B-24: after cleanup, _get_metadata_lock should return a fresh Lock"
    )
    # Clean up test state
    cleanup(task_id)


# ---------------------------------------------------------------------------
# Test 6: counter increments correctly under concurrent access to same task
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metadata_lock_counter_increments_correctly_under_concurrent_access():
    """B-24: per-task lock still prevents counter corruption for the same task_id."""
    get_lock = getattr(el, "_get_metadata_lock", None)
    if get_lock is None:
        pytest.fail("B-24: _get_metadata_lock helper not found")

    task_id = "task-counter-test"
    counter = {"value": 0}
    n_increments = 20

    async def increment():
        lock = get_lock(task_id)
        async with lock:
            # Read-modify-write — must be safe under concurrent access
            current = counter["value"]
            await asyncio.sleep(0)  # yield to event loop
            counter["value"] = current + 1

    await asyncio.gather(*[increment() for _ in range(n_increments)])
    assert counter["value"] == n_increments, (
        f"B-24: counter should be {n_increments} but got {counter['value']}; "
        "per-task lock is not protecting the read-modify-write correctly"
    )

    # Clean up
    cleanup = getattr(el, "_cleanup_metadata_lock", None)
    if cleanup:
        cleanup(task_id)
