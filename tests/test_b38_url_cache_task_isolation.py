"""B-38 regression suite: URL content cache is isolated per task_id.

Before the fix, the URL cache key was computed from the URL hash only.  Two
investigations sharing the same URL would share cached content, risking
cross-investigation staleness for time-sensitive sources.

After the fix, get_url/set_url accept an optional task_id parameter.  When
supplied, the Redis key is scoped to that task so two tasks do not share
a cache slot.

Test IDs:
  1. test_same_url_different_tasks_no_cross_cache
  2. test_same_url_same_task_cache_hit
  3. test_no_task_id_uses_global_key (backward-compat)
  4. test_task_id_included_in_key
  5. test_exists_task_scoped
  6. test_delete_url_task_scoped
  7. test_set_and_get_roundtrip_task_scoped
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mariana.data.cache import URLCache, _url_cache_key
from mariana.data.models import SourceType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeRedis:
    """Minimal in-process dict that mimics the async redis interface."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def exists(self, key: str) -> int:
        return 1 if key in self._store else 0


# ---------------------------------------------------------------------------
# Test 1: same URL different tasks → different cache slots (no cross-task hit)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_url_different_tasks_no_cross_cache():
    """B-38: content written for task A must not be visible to task B."""
    redis = _FakeRedis()
    cache = URLCache(redis)

    url_hash = "abc123deadbeef"
    task_a = "task-aaaaaa"
    task_b = "task-bbbbbb"

    await cache.set_url(url_hash, "content from task A", task_id=task_a)

    # Task B must not see task A's content
    result = await cache.get_url(url_hash, task_id=task_b)
    assert result is None, (
        f"B-38: task B must not receive task A's cached content, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: same URL, same task → cache hit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_url_same_task_cache_hit():
    """Same task_id fetching the same URL gets a cache hit."""
    redis = _FakeRedis()
    cache = URLCache(redis)

    url_hash = "abc123deadbeef"
    task_id = "task-cccccc"

    await cache.set_url(url_hash, "original content", task_id=task_id)
    result = await cache.get_url(url_hash, task_id=task_id)

    assert result is not None, "Cache miss: same task_id should yield a cache hit"
    assert result["content"] == "original content"


# ---------------------------------------------------------------------------
# Test 3: backward-compat — no task_id uses the global (non-scoped) key
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_task_id_uses_global_key():
    """When task_id is omitted, the original global key is used (backward-compat)."""
    redis = _FakeRedis()
    cache = URLCache(redis)

    url_hash = "global_hash_xyz"

    await cache.set_url(url_hash, "global content")  # no task_id
    result = await cache.get_url(url_hash)             # no task_id

    assert result is not None
    assert result["content"] == "global content"


# ---------------------------------------------------------------------------
# Test 4: task_id is included in the Redis key string
# ---------------------------------------------------------------------------

def test_task_id_included_in_key():
    """_url_cache_key(hash, task_id) must embed the task_id in the key."""
    url_hash = "somehash"
    task_id = "my-task-99"
    key_with_task = _url_cache_key(url_hash, task_id)
    key_without_task = _url_cache_key(url_hash)

    assert task_id in key_with_task or "mytask99" in key_with_task, (
        f"B-38: task_id must appear in the scoped key, got {key_with_task!r}"
    )
    assert key_with_task != key_without_task, (
        "B-38: task-scoped key must differ from the global key"
    )


# ---------------------------------------------------------------------------
# Test 5: exists() is task-scoped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exists_task_scoped():
    """exists() with task_id only returns True for that task's slot."""
    redis = _FakeRedis()
    cache = URLCache(redis)

    url_hash = "hash_for_exists"
    task_a = "task-exists-a"
    task_b = "task-exists-b"

    await cache.set_url(url_hash, "data", task_id=task_a)

    assert await cache.exists(url_hash, task_id=task_a) is True
    assert await cache.exists(url_hash, task_id=task_b) is False, (
        "B-38: exists() must not return True for a different task's cached entry"
    )


# ---------------------------------------------------------------------------
# Test 6: delete_url is task-scoped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_url_task_scoped():
    """Deleting task A's cache entry must not affect task B's entry."""
    redis = _FakeRedis()
    cache = URLCache(redis)

    url_hash = "hash_for_delete"
    task_a = "task-del-a"
    task_b = "task-del-b"

    await cache.set_url(url_hash, "content A", task_id=task_a)
    await cache.set_url(url_hash, "content B", task_id=task_b)

    await cache.delete_url(url_hash, task_id=task_a)

    assert await cache.get_url(url_hash, task_id=task_a) is None, "task A entry should be gone"
    result_b = await cache.get_url(url_hash, task_id=task_b)
    assert result_b is not None, "task B entry must survive task A's deletion"
    assert result_b["content"] == "content B"


# ---------------------------------------------------------------------------
# Test 7: full set/get roundtrip with task_id and source_type
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_and_get_roundtrip_task_scoped():
    """set_url/get_url roundtrip preserves content and metadata for a task."""
    redis = _FakeRedis()
    cache = URLCache(redis)

    url_hash = "roundtrip_hash"
    task_id = "task-roundtrip"
    content = "Hello from the future"
    metadata = {"source": "test", "lang": "en"}

    await cache.set_url(
        url_hash,
        content,
        source_type=SourceType.NEWS,
        metadata=metadata,
        task_id=task_id,
    )
    result = await cache.get_url(url_hash, task_id=task_id)

    assert result is not None
    assert result["content"] == content
    assert result["metadata"] == metadata
    assert result["cached_at"] > 0
