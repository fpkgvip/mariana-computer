"""
FRED (Federal Reserve Economic Data) connector.

Covers economic time-series retrieval, keyword search, and category browsing.

Authentication: FRED accepts requests without an API key for limited access,
but a key dramatically raises rate limits.  If `config.FRED_API_KEY` is set
it will be included in every request; otherwise calls proceed unauthenticated.

Cache policy:
  - All responses → 24 hours (economic data is published infrequently)
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from .base import BaseConnector, ConnectorError

logger = structlog.get_logger(__name__)

# TTL (seconds)
_TTL = 24 * 3600

_BASE_URL = "https://api.stlouisfed.org/fred"

# Mapping of common topic keywords → FRED series IDs
# Used as fast-path hints before falling back to the FRED search API.
_TOPIC_HINTS: dict[str, list[str]] = {
    "inflation": ["CPIAUCSL", "CPILFESL", "PCEPI"],
    "gdp": ["GDP", "GDPC1", "GDPDEF"],
    "unemployment": ["UNRATE", "U6RATE", "ICSA"],
    "interest": ["FEDFUNDS", "DGS10", "DGS2"],
    "housing": ["HOUST", "CSUSHPINSA", "MSPUS"],
    "employment": ["PAYEMS", "MANEMP", "USCONS"],
    "retail": ["RSAFS", "RETAILSMNSA"],
    "manufacturing": ["IPMAN", "PMI"],
    "credit": ["TOTALSL", "DPSACBW027SBOG"],
    "money": ["M2SL", "M1SL", "BOGMBASE"],
    "trade": ["BOPGSTB", "IMPGS", "EXPGS"],
    "consumer": ["PCE", "UMCSENT"],
}


class FredConnector(BaseConnector):
    """
    Async connector for the St. Louis Fed FRED REST API.

    Supports economic series retrieval, keyword search, and category browsing.
    The `search_for_topic` method is the orchestrator's primary entry point.

    Args:
        config: Application config object. Reads `.FRED_API_KEY` if present.
        cache:  Optional async cache (get/set with ttl).
    """

    def __init__(self, config: Any, cache: Any | None = None) -> None:
        super().__init__(config, cache)
        self._api_key: str | None = getattr(config, "FRED_API_KEY", None) or None
        if not self._api_key:
            logger.info("fred_api_key_not_set_using_unauthenticated_access")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _base_params(self, extra: dict | None = None) -> dict:
        """Return common query params including file_type and optional api_key."""
        params: dict = {"file_type": "json"}
        if self._api_key:
            params["api_key"] = self._api_key
        if extra:
            params.update(extra)
        return params

    async def _get(self, path: str, params: dict | None = None) -> dict:
        """
        Cached GET helper. Merges base params, checks the cache, then fetches.
        """
        url = f"{_BASE_URL}{path}"
        merged = self._base_params(params)
        cache_key = self._cache_key("fred", url, str(sorted(merged.items())))

        cached = await self._cache_get(cache_key)
        if cached is not None:
            self._log.debug("cache_hit", url=url)
            return cached

        data = await self._request("GET", url, params=merged)
        await self._cache_set(cache_key, data, ttl=_TTL)
        return data

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def get_series(
        self,
        series_id: str,
        observation_start: str | None = None,
        observation_end: str | None = None,
    ) -> dict:
        """
        Fetch time-series observations for a FRED series.

        Endpoint: GET /series/observations

        Args:
            series_id:          FRED series ID, e.g. "CPIAUCSL".
            observation_start:  ISO date string "YYYY-MM-DD" for start of range.
            observation_end:    ISO date string "YYYY-MM-DD" for end of range.

        Returns:
            FRED observations payload with `observations` list.
        """
        self._log.info("get_series", series_id=series_id)
        params: dict = {"series_id": series_id}
        if observation_start:
            params["observation_start"] = observation_start
        if observation_end:
            params["observation_end"] = observation_end
        try:
            return await self._get("/series/observations", params=params)
        except Exception as exc:
            self._log.error("get_series_failed", series_id=series_id, error=str(exc))
            raise ConnectorError(f"Failed to get FRED series {series_id}") from exc

    async def search_series(self, query: str) -> dict:
        """
        Search FRED for series matching a keyword query.

        Endpoint: GET /series/search

        Args:
            query: Search text, e.g. "consumer price index".

        Returns:
            FRED series-search payload.
        """
        self._log.info("search_series", query=query)
        try:
            return await self._get("/series/search", params={"search_text": query})
        except Exception as exc:
            self._log.error("search_series_failed", query=query, error=str(exc))
            raise ConnectorError(f"FRED series search failed for '{query}'") from exc

    async def get_category_series(self, category_id: int) -> dict:
        """
        Fetch all series in a FRED category.

        Endpoint: GET /category/series

        Args:
            category_id: Numeric FRED category ID (e.g. 32455 for prices).

        Returns:
            FRED category-series payload.
        """
        if category_id <= 0:
            raise ValueError(f"category_id must be a positive integer, got {category_id}")
        self._log.info("get_category_series", category_id=category_id)
        try:
            return await self._get("/category/series", params={"category_id": category_id})
        except Exception as exc:
            self._log.error("get_category_series_failed", category_id=category_id, error=str(exc))
            raise ConnectorError(f"Failed to get FRED category {category_id}") from exc

    # ------------------------------------------------------------------
    # Orchestrator entry point
    # ------------------------------------------------------------------

    async def search_for_topic(self, topic: str) -> list[dict]:
        """
        High-level method for the orchestrator.

        Strategy:
          1. Match topic keywords against `_TOPIC_HINTS` for fast, curated
             series IDs — fetch each matched series directly.
          2. Also run a keyword search against the FRED API for any unmatched
             significant words; fetch the top-5 returned series.
          3. De-duplicate series IDs so we don't fetch the same data twice.

        Args:
            topic: Free-form research topic, e.g. "US inflation and interest rates".

        Returns:
            List of finding dicts, each containing a series_id and its data.
        """
        self._log.info("search_for_topic", topic=topic)

        topic_lower = topic.lower()

        # 1. Hint-based series IDs
        # Use word-boundary matching to avoid false positives like "interesting" matching "interest".
        hinted_series: list[str] = []
        for keyword, series_ids in _TOPIC_HINTS.items():
            if re.search(r"\b" + re.escape(keyword) + r"\b", topic_lower):
                hinted_series.extend(series_ids)

        # 2. FRED keyword search
        search_series_ids: list[str] = []
        try:
            search_result = await self.search_series(topic)
            for s in search_result.get("seriess", [])[:5]:
                sid = s.get("id")
                if sid:
                    search_series_ids.append(sid)
        except ConnectorError as exc:
            self._log.warning("search_series_failed", topic=topic, error=str(exc))

        # 3. Merge and de-duplicate, keeping hints first (higher quality)
        all_series = list(dict.fromkeys(hinted_series + search_series_ids))[:10]

        if not all_series:
            self._log.warning("no_series_found", topic=topic)
            return []

        findings: list[dict] = []
        for series_id in all_series:
            try:
                data = await self.get_series(series_id)
                findings.append(
                    {
                        "source": "fred",
                        "series_id": series_id,
                        "topic": topic,
                        "data": data,
                    }
                )
                self._log.info("series_fetched", series_id=series_id)
            except ConnectorError as exc:
                self._log.warning("series_fetch_failed", series_id=series_id, error=str(exc))
                findings.append(
                    {
                        "source": "fred",
                        "series_id": series_id,
                        "topic": topic,
                        "error": str(exc),
                    }
                )

        return findings
