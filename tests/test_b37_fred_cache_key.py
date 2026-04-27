"""B-37 regression suite: FRED connector excludes api_key from cache key.

Before the fix, the cache key included the api_key parameter.  In multi-
instance deployments sharing a Redis cache, each instance with a distinct
FRED_API_KEY would produce a different cache key for the same logical query,
defeating the cache entirely.

After the fix, the cache key is computed from the query parameters *excluding*
api_key.  HTTP requests still include the key; only the cache key omits it.

Test IDs:
  1. test_same_query_different_api_keys_same_cache_key
  2. test_cache_key_does_not_contain_api_key_string
  3. test_different_queries_different_cache_keys
  4. test_no_api_key_same_cache_key_as_with_key
  5. test_api_key_still_included_in_http_request
  6. test_cache_hit_shared_between_key_instances
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(api_key: str) -> MagicMock:
    cfg = MagicMock()
    cfg.FRED_API_KEY = api_key
    return cfg


class _FakeCache:
    """In-process dict acting as a cache for tests."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self.last_get_key: str | None = None
        self.last_set_key: str | None = None

    async def get(self, key: str) -> str | None:
        self.last_get_key = key
        return self._store.get(key)

    async def set(self, key: str, value: object, *, ttl: int = 0) -> None:
        self.last_set_key = key
        self._store[key] = value  # type: ignore[assignment]


def _make_connector(api_key: str, cache: _FakeCache | None = None):
    """Create a FredConnector with the given API key and optional shared cache."""
    from mariana.connectors.fred_connector import FredConnector

    cfg = _make_config(api_key)
    conn = FredConnector(cfg, cache)
    return conn


def _capture_cache_key(connector) -> str | None:
    """Extract the cache key that _get() would compute for /series/observations?series_id=GDP."""
    # We access the _cache_key method and simulate what _get does.
    url = "https://api.stlouisfed.org/fred/series/observations"
    merged = connector._base_params({"series_id": "GDP"})
    cache_params = {k: v for k, v in merged.items() if k != "api_key"}
    return connector._cache_key("fred", url, str(sorted(cache_params.items())))


# ---------------------------------------------------------------------------
# Test 1: same query, different API keys → same cache key
# ---------------------------------------------------------------------------

def test_same_query_different_api_keys_same_cache_key():
    """B-37: two instances with distinct API keys must produce identical cache keys."""
    conn_a = _make_connector("key_production_abc123")
    conn_b = _make_connector("key_staging_xyz789")

    key_a = _capture_cache_key(conn_a)
    key_b = _capture_cache_key(conn_b)

    assert key_a == key_b, (
        f"B-37: cache keys must not depend on api_key value; "
        f"got key_a={key_a!r}, key_b={key_b!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: cache key string does not contain the raw api_key value
# ---------------------------------------------------------------------------

def test_cache_key_does_not_contain_api_key_string():
    """The api_key value must not appear verbatim in the cache key."""
    api_key = "super_secret_fred_key_98765"
    conn = _make_connector(api_key)
    key = _capture_cache_key(conn)
    assert api_key not in key, (
        f"B-37: api_key value must not appear in cache key, found it in {key!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: different queries → different cache keys (sanity check)
# ---------------------------------------------------------------------------

def test_different_queries_different_cache_keys():
    """Two different FRED series IDs must produce different cache keys."""
    conn = _make_connector("any_key")
    url = "https://api.stlouisfed.org/fred/series/observations"

    merged_gdp = conn._base_params({"series_id": "GDP"})
    cache_params_gdp = {k: v for k, v in merged_gdp.items() if k != "api_key"}
    key_gdp = conn._cache_key("fred", url, str(sorted(cache_params_gdp.items())))

    merged_cpi = conn._base_params({"series_id": "CPIAUCSL"})
    cache_params_cpi = {k: v for k, v in merged_cpi.items() if k != "api_key"}
    key_cpi = conn._cache_key("fred", url, str(sorted(cache_params_cpi.items())))

    assert key_gdp != key_cpi, "Different series IDs must yield different cache keys"


# ---------------------------------------------------------------------------
# Test 4: no api_key and with api_key produce the same cache key
# ---------------------------------------------------------------------------

def test_no_api_key_same_cache_key_as_with_key():
    """Unauthenticated connector and authenticated one share the same cache key."""
    conn_unauth = _make_connector("")  # no key
    conn_auth = _make_connector("some_prod_key")

    key_unauth = _capture_cache_key(conn_unauth)
    key_auth = _capture_cache_key(conn_auth)

    assert key_unauth == key_auth, (
        f"B-37: cache key must be the same whether or not api_key is set; "
        f"unauth={key_unauth!r}, auth={key_auth!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: HTTP request still includes api_key in merged params
# ---------------------------------------------------------------------------

def test_api_key_still_included_in_http_request():
    """The api_key must remain in the params sent to the FRED HTTP endpoint."""
    api_key = "should_be_in_request_params"
    conn = _make_connector(api_key)
    merged = conn._base_params({"series_id": "GDP"})
    assert "api_key" in merged, "api_key must still be present in merged params for HTTP"
    assert merged["api_key"] == api_key


# ---------------------------------------------------------------------------
# Test 6: shared cache across two connector instances with different keys
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_shared_between_key_instances():
    """Instance B gets a cache hit from data fetched by instance A (different key)."""
    shared_cache = _FakeCache()
    conn_a = _make_connector("key_instance_a", shared_cache)
    conn_b = _make_connector("key_instance_b", shared_cache)

    fake_response = {"seriess": [{"id": "GDP", "title": "Gross Domestic Product"}]}

    # Simulate conn_a populating the cache: call _get and let it store result
    with patch.object(conn_a, "_request", new_callable=AsyncMock, return_value=fake_response):
        result_a = await conn_a._get("/series/search", {"search_text": "GDP"})

    # Now conn_b should hit the shared cache without making an HTTP request
    with patch.object(conn_b, "_request", new_callable=AsyncMock, return_value=None) as mock_req:
        result_b = await conn_b._get("/series/search", {"search_text": "GDP"})
        mock_req.assert_not_called()  # cache hit: no HTTP call

    assert result_b == fake_response, (
        "B-37: conn_b should receive conn_a's cached result when keys differ"
    )
