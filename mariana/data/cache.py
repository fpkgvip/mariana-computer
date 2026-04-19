"""
Mariana Computer — Redis-backed caching layer.

Provides two classes:

``URLCache``
    Stores and retrieves fetched web-page content keyed by URL hash.
    TTL is automatically selected based on the source type.

``QueryDedup``
    Tracks query hashes per task to prevent re-issuing identical or
    near-duplicate queries.  Uses a sliding ring-buffer stored in a Redis
    sorted set (score = insertion timestamp) to bound memory usage.

No FAISS or vector similarity is used at this prototype stage — dedup is
implemented with exact SHA-256 matching for speed and simplicity.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any

import redis.asyncio as aioredis

from mariana.data.models import SourceType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TTL table (seconds) keyed by SourceType
# ---------------------------------------------------------------------------

_SOURCE_TYPE_TTL: dict[SourceType, int] = {
    SourceType.NEWS: 86_400,          # 24 hours
    SourceType.FILING: 604_800,       # 7 days
    SourceType.GOVERNMENT: 604_800,   # 7 days
    SourceType.EXCHANGE: 3_600,       # 1 hour
    SourceType.ANALYST_REPORT: 604_800,  # 7 days
    SourceType.UNOFFICIAL_API: 3_600, # 1 hour
}

_STATIC_TTL: int = 2_592_000   # 30 days
_DEFAULT_TTL: int = 86_400     # 24 hours (fallback)


def get_ttl_for_source_type(source_type: SourceType) -> int:
    """
    Return the cache TTL in seconds appropriate for the given source type.

    Args:
        source_type: The :class:`SourceType` enum value.

    Returns:
        TTL in seconds.
    """
    return _SOURCE_TYPE_TTL.get(source_type, _DEFAULT_TTL)


# ---------------------------------------------------------------------------
# Redis key helpers
# ---------------------------------------------------------------------------

_URL_CACHE_PREFIX = "mariana:url:"
_QUERY_DEDUP_PREFIX = "mariana:qdedup:"

# BUG-0041 fix: strip characters that could be used for Redis key injection.
_SAFE_KEY_RE = re.compile(r"[^a-zA-Z0-9_:\-.]")


def _sanitize_key_component(value: str) -> str:
    """Remove characters that could be used for Redis key injection."""
    return _SAFE_KEY_RE.sub("", value)


def _url_cache_key(url_hash: str) -> str:
    return f"{_URL_CACHE_PREFIX}{_sanitize_key_component(url_hash)}"


def _query_dedup_key(task_id: str) -> str:
    return f"{_QUERY_DEDUP_PREFIX}{_sanitize_key_component(task_id)}"


# ---------------------------------------------------------------------------
# URLCache
# ---------------------------------------------------------------------------


class URLCache:
    """
    Redis-backed cache for fetched page content.

    Content is stored as a JSON blob containing the raw text, metadata, and
    the expiry timestamp.  All keys are namespaced under ``mariana:url:``.

    Args:
        redis:       An open ``redis.asyncio.Redis`` client.
        default_ttl: Fallback TTL in seconds when source type is unknown.
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        default_ttl: int = _DEFAULT_TTL,
    ) -> None:
        self._redis = redis
        self._default_ttl = default_ttl

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_url(self, url_hash: str) -> dict[str, Any] | None:
        """
        Retrieve cached content for a URL hash.

        Args:
            url_hash: SHA-256 hex digest of the canonical URL.

        Returns:
            The cached payload dict (keys: ``content``, ``metadata``,
            ``cached_at``, ``expires_at``), or *None* on a cache miss.
        """
        raw: str | None = await self._redis.get(_url_cache_key(url_hash))  # BUG-NEW-16 fix: decode_responses=True returns str, not bytes
        if raw is None:
            logger.debug("URL cache miss url_hash=%s", url_hash)
            return None
        try:
            payload: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "Corrupt cache entry for url_hash=%s — deleting",
                url_hash,
            )
            await self._redis.delete(_url_cache_key(url_hash))
            return None
        logger.debug("URL cache hit url_hash=%s", url_hash)
        return payload

    async def set_url(
        self,
        url_hash: str,
        content: str,
        source_type: SourceType | None = None,
        metadata: dict[str, Any] | None = None,
        ttl: int | None = None,
    ) -> None:
        """
        Store page content in the cache.

        Args:
            url_hash:    SHA-256 hex digest of the canonical URL.
            content:     Raw text content of the fetched page.
            source_type: Source provenance; used to select the appropriate TTL.
            metadata:    Arbitrary key/value metadata to store alongside content.
            ttl:         Explicit TTL in seconds; overrides source-type selection.
        """
        effective_ttl: int
        if ttl is not None:
            effective_ttl = ttl
        elif source_type is not None:
            effective_ttl = get_ttl_for_source_type(source_type)
        else:
            effective_ttl = self._default_ttl

        now = time.time()
        payload: dict[str, Any] = {
            "content": content,
            "metadata": metadata or {},
            "cached_at": now,
            "expires_at": now + effective_ttl,
        }
        await self._redis.setex(
            _url_cache_key(url_hash),
            effective_ttl,
            json.dumps(payload),
        )
        logger.debug(
            "URL cache set url_hash=%s ttl=%ds source_type=%s",
            url_hash,
            effective_ttl,
            source_type,
        )

    async def delete_url(self, url_hash: str) -> None:
        """Explicitly evict a cached URL entry."""
        await self._redis.delete(_url_cache_key(url_hash))
        logger.debug("URL cache deleted url_hash=%s", url_hash)

    async def exists(self, url_hash: str) -> bool:
        """Return True if a (non-expired) entry exists for the given URL hash."""
        result: int = await self._redis.exists(_url_cache_key(url_hash))
        return result > 0


# ---------------------------------------------------------------------------
# QueryDedup
# ---------------------------------------------------------------------------


class QueryDedup:
    """
    Deduplication tracker for search queries within a research task.

    Queries are hashed (SHA-256) and stored in a Redis sorted set, with the
    insertion Unix timestamp as the score.  The set is trimmed to at most
    ``window_size`` entries by removing the oldest members, bounding memory
    usage while providing a sliding-window view of recent activity.

    This prototype uses exact hash matching only.  Semantic / Jaccard
    similarity can be layered on top later if needed.

    Args:
        redis:       An open ``redis.asyncio.Redis`` client.
        window_size: Maximum number of query hashes to keep per task.
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        window_size: int = 200,
    ) -> None:
        self._redis = redis
        self._window_size = window_size

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_query(query: str) -> str:
        """Return the SHA-256 hex digest of a normalised query string."""
        normalised = query.strip().lower()
        return hashlib.sha256(normalised.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def is_duplicate(self, task_id: str, query: str) -> bool:
        """
        Check whether an identical query has already been issued for this task.

        Args:
            task_id: The research task identifier (namespace).
            query:   The search query string to check.

        Returns:
            *True* if the exact same query (case-insensitively normalised) has
            been seen before within the current window; *False* otherwise.
        """
        query_hash = self._hash_query(query)
        key = _query_dedup_key(task_id)
        # ZSCORE returns None if the member is absent
        score: float | None = await self._redis.zscore(key, query_hash)
        is_dup = score is not None
        if is_dup:
            logger.debug(
                "Query dedup HIT task=%s query_hash=%s",
                task_id,
                query_hash,
            )
        return is_dup

    async def record_query(self, task_id: str, query: str) -> None:
        """
        Record that a query has been issued, maintaining the sliding window.

        Adds the query hash to the sorted set with the current timestamp as
        the score, then trims the set to at most ``window_size`` entries by
        removing the members with the lowest scores (oldest inserts).

        Args:
            task_id: The research task identifier (namespace).
            query:   The search query string that was just issued.
        """
        query_hash = self._hash_query(query)
        key = _query_dedup_key(task_id)
        now = time.time()

        # Use transaction=True to wrap in MULTI/EXEC, preventing partial failure
        pipe = self._redis.pipeline(transaction=True)
        pipe.zadd(key, {query_hash: now})
        # Trim to window: keep only the top window_size members (highest scores = most recent)
        pipe.zremrangebyrank(key, 0, -(self._window_size + 1))
        await pipe.execute()

        logger.debug(
            "Query dedup recorded task=%s query_hash=%s",
            task_id,
            query_hash,
        )

    # Lua script for atomic check-and-set:
    # Returns 1 if the member already existed (duplicate), 0 if it was novel.
    _CHECK_AND_RECORD_SCRIPT = """
        local key = KEYS[1]
        local member = ARGV[1]
        local now = tonumber(ARGV[2])
        local window = tonumber(ARGV[3])
        if redis.call('ZSCORE', key, member) then
            return 1
        end
        redis.call('ZADD', key, now, member)
        redis.call('ZREMRANGEBYRANK', key, 0, -(window + 1))
        return 0
    """

    async def check_and_record(self, task_id: str, query: str) -> bool:
        """
        Atomically check for a duplicate and, if novel, record the query.

        Uses a Redis Lua script executed atomically to avoid the race
        condition between is_duplicate and record_query.

        Args:
            task_id: The research task identifier (namespace).
            query:   The search query string.

        Returns:
            *True* if the query was a duplicate (should be skipped).
            *False* if the query is novel (was just recorded).
        """
        query_hash = self._hash_query(query)
        key = _query_dedup_key(task_id)
        now = time.time()

        result = await self._redis.eval(
            self._CHECK_AND_RECORD_SCRIPT,
            1,
            key,
            query_hash,
            now,
            self._window_size,
        )
        is_dup = bool(result)
        if is_dup:
            logger.debug(
                "Query dedup HIT (atomic) task=%s query_hash=%s",
                task_id,
                query_hash,
            )
        else:
            logger.debug(
                "Query dedup recorded (atomic) task=%s query_hash=%s",
                task_id,
                query_hash,
            )
        return is_dup

    async def get_seen_hashes(self, task_id: str) -> set[str]:
        """
        Return the full set of query hashes currently in the window.

        Args:
            task_id: The research task identifier (namespace).

        Returns:
            A set of SHA-256 hex digest strings.
        """
        key = _query_dedup_key(task_id)
        members: list[bytes] = await self._redis.zrange(key, 0, -1)
        # BUG-024: With decode_responses=True, members are already str; the
        # isinstance guard handles both modes for backward compatibility.
        return {m.decode("utf-8") if isinstance(m, bytes) else m for m in members}

    async def clear(self, task_id: str) -> None:
        """Delete the entire dedup window for a task (e.g. on task reset)."""
        await self._redis.delete(_query_dedup_key(task_id))
        logger.debug("Query dedup cleared for task=%s", task_id)

    async def window_size_used(self, task_id: str) -> int:
        """Return the current number of entries in the dedup window."""
        key = _query_dedup_key(task_id)
        count: int = await self._redis.zcard(key)
        return count


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


async def create_redis_client(
    redis_url: str,
    max_connections: int = 20,
    socket_timeout: float = 5.0,
) -> aioredis.Redis:
    """
    Create and return an async Redis client.

    Args:
        redis_url:       Redis connection URL (e.g. ``redis://redis:6379/0``
                         or ``rediss://...`` for TLS).
        max_connections: Maximum number of connections in the pool.
        socket_timeout:  Socket-level timeout in seconds.

    Returns:
        A connected :class:`redis.asyncio.Redis` instance.

    M-05 fix: require ``rediss://`` (TLS) for any non-loopback Redis URL
    so cached investigation data / user context is not transmitted in
    cleartext over the network.
    """
    _u = (redis_url or "").lower()
    _is_local = any(
        tok in _u
        for tok in ("://localhost", "://127.", "://[::1]", "://redis:")
    )
    if not _is_local and _u.startswith("redis://"):
        raise ValueError(
            "Remote Redis URLs must use rediss:// (TLS) to protect cached data"
        )
    client: aioredis.Redis = aioredis.from_url(
        redis_url,
        max_connections=max_connections,
        socket_timeout=socket_timeout,
        # BUG-024: Use decode_responses=True for consistency with the API's Redis client
        decode_responses=True,
    )
    # Verify connectivity; clean up on failure to avoid connection pool leak (BUG-055)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        raise
    logger.info("Redis client connected to %s", redis_url)
    return client
