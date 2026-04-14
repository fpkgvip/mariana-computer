"""
Polygon.io connector for US equity market data.

Covers ticker reference data, financial statements, OHLCV aggregates,
options snapshots, related companies, and news headlines.

Cache policy:
  - news            → 24 hours
  - financials      → 24 hours
  - reference data  → 7 days
  - aggregates      → 1 hour (intraday); callers may override via `ttl`
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import structlog

from .base import BaseConnector, ConnectorError

logger = structlog.get_logger(__name__)

# TTL constants (seconds)
_TTL_NEWS = 24 * 3600
_TTL_FINANCIALS = 24 * 3600
_TTL_REFERENCE = 7 * 24 * 3600
_TTL_AGGREGATES = 3600

_BASE_URL = "https://api.polygon.io"

# Rough regex to pull uppercase ticker symbols out of a free-form string.
# Requires 2-5 uppercase letters to avoid matching single-letter words (I, A).
_TICKER_RE = re.compile(r"\b([A-Z]{2,5})\b")

# Common uppercase acronyms/words that are not stock tickers.
_TICKER_BLOCKLIST: frozenset[str] = frozenset({
    "US", "GDP", "FED", "SEC", "ETF", "IPO", "CEO", "CFO", "COO", "CTO",
    "AI", "ML", "IT", "HR", "PR", "TV", "UK", "EU", "UN", "IMF", "WTO",
    "EPS", "PE", "YOY", "QOQ", "TTM", "ATH", "ATL", "NAV", "AUM",
    "USD", "EUR", "GBP", "JPY", "CNY", "BTC", "ETH",
})


class PolygonConnector(BaseConnector):
    """
    Async connector for the Polygon.io REST API.

    All methods check the shared cache before issuing a network request.
    The `search_for_topic` method is the orchestrator's primary entry point.

    Args:
        config: Must expose `.POLYGON_API_KEY` attribute.
        cache:  Optional async cache (get/set with ttl).
    """

    def __init__(self, config: Any, cache: Any | None = None) -> None:
        super().__init__(config, cache)
        self._api_key = getattr(config, "POLYGON_API_KEY", "")
        if not self._api_key:
            logger.warning("polygon_api_key_missing")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _auth_params(self, extra: dict | None = None) -> dict:
        """Return base query params including the API key."""
        params: dict = {"apiKey": self._api_key}
        if extra:
            params.update(extra)
        return params

    async def _get(self, path: str, params: dict | None = None, ttl: int = _TTL_REFERENCE) -> dict:
        """
        Cached GET helper. Builds the full URL, checks the cache, then
        falls back to a live HTTP call and populates the cache.
        """
        url = f"{_BASE_URL}{path}"
        merged_params = self._auth_params(params)
        cache_key = self._cache_key("polygon", url, str(sorted(merged_params.items())))

        cached = await self._cache_get(cache_key)
        if cached is not None:
            self._log.debug("cache_hit", url=url)
            return cached

        data = await self._request("GET", url, params=merged_params)
        await self._cache_set(cache_key, data, ttl=ttl)
        return data

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def get_ticker_details(self, ticker: str) -> dict:
        """
        Fetch reference data for a single ticker.

        Endpoint: GET /v3/reference/tickers/{ticker}

        Args:
            ticker: Uppercase ticker symbol, e.g. "AAPL".

        Returns:
            Polygon ticker-detail payload.
        """
        ticker = ticker.upper().strip()
        self._log.info("get_ticker_details", ticker=ticker)
        try:
            return await self._get(f"/v3/reference/tickers/{ticker}", ttl=_TTL_REFERENCE)
        except Exception as exc:
            self._log.error("get_ticker_details_failed", ticker=ticker, error=str(exc))
            raise ConnectorError(f"Failed to get ticker details for {ticker}") from exc

    async def get_financials(self, ticker: str, limit: int = 5) -> dict:
        """
        Fetch fundamental financial statements for a ticker.

        Endpoint: GET /vX/reference/financials

        Args:
            ticker: Uppercase ticker symbol.
            limit:  Number of periods to return (default 5).

        Returns:
            Polygon financials payload.
        """
        ticker = ticker.upper().strip()
        self._log.info("get_financials", ticker=ticker, limit=limit)
        try:
            return await self._get(
                "/vX/reference/financials",
                params={"ticker": ticker, "limit": limit},
                ttl=_TTL_FINANCIALS,
            )
        except Exception as exc:
            self._log.error("get_financials_failed", ticker=ticker, error=str(exc))
            raise ConnectorError(f"Failed to get financials for {ticker}") from exc

    async def get_stock_aggregates(
        self,
        ticker: str,
        multiplier: int,
        timespan: str,
        from_date: str,
        to_date: str,
    ) -> dict:
        """
        Fetch OHLCV aggregate bars for a ticker.

        Endpoint: GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}

        Args:
            ticker:     Uppercase ticker symbol.
            multiplier: Bar size multiplier (e.g. 1, 5, 15).
            timespan:   Bar size unit: "minute" | "hour" | "day" | "week" | "month".
            from_date:  Start date in "YYYY-MM-DD" format.
            to_date:    End date in "YYYY-MM-DD" format.

        Returns:
            Polygon aggregates payload.
        """
        ticker = ticker.upper().strip()
        self._log.info(
            "get_stock_aggregates",
            ticker=ticker,
            multiplier=multiplier,
            timespan=timespan,
            from_date=from_date,
            to_date=to_date,
        )
        path = f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from_date}/{to_date}"
        try:
            return await self._get(path, ttl=_TTL_AGGREGATES)
        except Exception as exc:
            self._log.error("get_stock_aggregates_failed", ticker=ticker, error=str(exc))
            raise ConnectorError(f"Failed to get aggregates for {ticker}") from exc

    async def get_related_companies(self, ticker: str) -> dict:
        """
        Fetch tickers that are related to the given ticker.

        Endpoint: GET /v1/related-companies/{ticker}

        Args:
            ticker: Uppercase ticker symbol.

        Returns:
            Polygon related-companies payload.
        """
        ticker = ticker.upper().strip()
        self._log.info("get_related_companies", ticker=ticker)
        try:
            return await self._get(f"/v1/related-companies/{ticker}", ttl=_TTL_REFERENCE)
        except Exception as exc:
            self._log.error("get_related_companies_failed", ticker=ticker, error=str(exc))
            raise ConnectorError(f"Failed to get related companies for {ticker}") from exc

    async def get_ticker_news(self, ticker: str, limit: int = 20) -> dict:
        """
        Fetch recent news articles mentioning a ticker.

        Endpoint: GET /v3/reference/news

        Args:
            ticker: Uppercase ticker symbol.
            limit:  Maximum number of articles to return (default 20).

        Returns:
            Polygon v3 news payload: ``{"results": [...], "next_url": "..."}``.
        """
        ticker = ticker.upper().strip()
        self._log.info("get_ticker_news", ticker=ticker, limit=limit)
        try:
            # BUG-A06 fix: use /v3/reference/news (the current active endpoint).
            # The v2 endpoint (/v2/reference/news) is deprecated.  The v3 endpoint
            # uses "ticker.any_of" as the query param (not plain "ticker") and
            # returns a paginated response: {"results": [...], "next_url": "..."}.
            return await self._get(
                "/v3/reference/news",
                params={"ticker.any_of": ticker, "limit": limit},
                ttl=_TTL_NEWS,
            )
        except Exception as exc:
            self._log.error("get_ticker_news_failed", ticker=ticker, error=str(exc))
            raise ConnectorError(f"Failed to get news for {ticker}") from exc

    async def search_tickers(self, query: str) -> dict:
        """
        Search for tickers matching a keyword query.

        Endpoint: GET /v3/reference/tickers?search={query}

        Args:
            query: Keyword string to search against ticker symbols and names.

        Returns:
            Polygon ticker-search payload.
        """
        self._log.info("search_tickers", query=query)
        try:
            return await self._get(
                "/v3/reference/tickers",
                params={"search": query, "active": "true", "limit": 10},
                ttl=_TTL_REFERENCE,
            )
        except Exception as exc:
            self._log.error("search_tickers_failed", query=query, error=str(exc))
            raise ConnectorError(f"Ticker search failed for '{query}'") from exc

    async def get_options_chain(self, ticker: str) -> dict:
        """
        Fetch the full options chain snapshot for a ticker.

        Endpoint: GET /v3/snapshot/options/{ticker}

        Args:
            ticker: Uppercase ticker symbol.

        Returns:
            Polygon options-chain snapshot payload.
        """
        ticker = ticker.upper().strip()
        self._log.info("get_options_chain", ticker=ticker)
        try:
            return await self._get(f"/v3/snapshot/options/{ticker}", ttl=_TTL_AGGREGATES)
        except Exception as exc:
            self._log.error("get_options_chain_failed", ticker=ticker, error=str(exc))
            raise ConnectorError(f"Failed to get options chain for {ticker}") from exc

    # ------------------------------------------------------------------
    # Orchestrator entry point
    # ------------------------------------------------------------------

    async def search_for_topic(self, topic: str) -> list[dict]:
        """
        High-level method for the orchestrator.

        Extracts likely ticker symbols from `topic`, then in parallel
        fetches news, financials, and reference details for each.  Falls
        back to a keyword ticker-search when no obvious tickers are found.

        Args:
            topic: Free-form research topic, e.g. "NVDA earnings outlook".

        Returns:
            List of finding dicts, each keyed by ticker with nested data.
        """
        self._log.info("search_for_topic", topic=topic)

        # Extract uppercase tokens that could be ticker symbols
        candidate_tickers = [
            t for t in list(dict.fromkeys(_TICKER_RE.findall(topic)))
            if t not in _TICKER_BLOCKLIST
        ]

        # If no plausible tickers found, do a keyword search first
        if not candidate_tickers:
            try:
                search_result = await self.search_tickers(topic)
                results = search_result.get("results", [])
                candidate_tickers = [r["ticker"] for r in results[:5] if "ticker" in r]
            except ConnectorError:
                self._log.warning("topic_ticker_search_failed", topic=topic)

        if not candidate_tickers:
            self._log.warning("no_tickers_found", topic=topic)
            return []

        findings: list[dict] = []

        for ticker in candidate_tickers[:5]:  # cap at 5 to respect rate limits
            finding: dict = {"source": "polygon", "ticker": ticker, "topic": topic}

            results = await asyncio.gather(
                self.get_ticker_details(ticker),
                self.get_ticker_news(ticker),
                self.get_financials(ticker),
                return_exceptions=True,
            )
            keys = ["details", "news", "financials"]
            for key, result in zip(keys, results):
                if isinstance(result, Exception):
                    finding[f"{key}_error"] = str(result)
                else:
                    finding[key] = result

            findings.append(finding)
            self._log.info("topic_finding_collected", ticker=ticker)

        return findings
