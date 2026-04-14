"""
Unusual Whales connector for options flow, dark-pool prints, congressional
trades, and insider transactions.

Cache policy:
  - Live flow data (options, dark pool)  → 4 hours
  - Reference / slow-moving data         → 24 hours
"""

from __future__ import annotations

import asyncio  # BUG-021 fix: moved from inline import inside method body to top-level
import re
from typing import Any

import structlog

from .base import BaseConnector, ConnectorError

logger = structlog.get_logger(__name__)

# TTL constants (seconds)
_TTL_FLOW = 4 * 3600
_TTL_REFERENCE = 24 * 3600

_BASE_URL = "https://api.unusualwhales.com/api"

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


class UnusualWhalesConnector(BaseConnector):
    """
    Async connector for the Unusual Whales REST API.

    All methods check the shared cache before issuing a network request.
    The `search_for_topic` method is the orchestrator's primary entry point.

    Args:
        config: Must expose `.UNUSUAL_WHALES_API_KEY` attribute.
        cache:  Optional async cache (get/set with ttl).
    """

    def __init__(self, config: Any, cache: Any | None = None) -> None:
        super().__init__(config, cache)
        api_key = getattr(config, "UNUSUAL_WHALES_API_KEY", "")
        if not api_key:
            logger.warning("unusual_whales_api_key_missing")
            self._auth_headers: dict[str, str] = {}
        else:
            self._auth_headers = {"Authorization": f"Bearer {api_key}"}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: dict | None = None, ttl: int = _TTL_FLOW) -> dict:
        """
        Cached GET helper. Builds the full URL, checks the cache, then
        falls back to a live HTTP call and populates the cache.
        """
        url = f"{_BASE_URL}{path}"
        cache_key = self._cache_key("uw", url, str(sorted((params or {}).items())))

        cached = await self._cache_get(cache_key)
        if cached is not None:
            self._log.debug("cache_hit", url=url)
            return cached

        data = await self._request("GET", url, headers=self._auth_headers, params=params)
        await self._cache_set(cache_key, data, ttl=ttl)
        return data

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def get_options_flow(self, ticker: str | None = None) -> dict:
        """
        Fetch unusual options flow, optionally filtered to a single ticker.

        Endpoints:
          - With ticker:    GET /stock/{ticker}/options-volume
          - Without ticker: GET /options/flow

        Args:
            ticker: Optional uppercase ticker symbol.

        Returns:
            Options flow payload.
        """
        self._log.info("get_options_flow", ticker=ticker)
        try:
            if ticker:
                ticker = ticker.upper().strip()
                # BUG-016 fix: use /stock/{ticker}/options-volume as documented
                # in the docstring.  /stock/{ticker}/flow/options does not match
                # the documented UW API endpoint.
                return await self._get(f"/stock/{ticker}/options-volume", ttl=_TTL_FLOW)
            return await self._get("/options/flow", ttl=_TTL_FLOW)
        except Exception as exc:
            self._log.error("get_options_flow_failed", ticker=ticker, error=str(exc))
            raise ConnectorError(f"Failed to get options flow for {ticker!r}") from exc

    async def get_dark_pool_flow(self, ticker: str | None = None) -> dict:
        """
        Fetch dark pool print flow, optionally filtered to a single ticker.

        Endpoints:
          - With ticker:    GET /darkpool/{ticker}
          - Without ticker: GET /darkpool/flow  (market-wide)

        Args:
            ticker: Optional uppercase ticker symbol.

        Returns:
            Dark pool flow payload.
        """
        self._log.info("get_dark_pool_flow", ticker=ticker)
        try:
            if ticker:
                ticker = ticker.upper().strip()
                # BUG-017 fix: use /darkpool/{ticker} as documented in the docstring.
                # /darkpool/recent with a ticker query param is inconsistent with
                # the documented UW endpoint; the per-ticker dark pool endpoint is
                # /darkpool/{ticker} and the market-wide feed is /darkpool/flow.
                return await self._get(f"/darkpool/{ticker}", ttl=_TTL_FLOW)
            return await self._get("/darkpool/flow", ttl=_TTL_FLOW)
        except Exception as exc:
            self._log.error("get_dark_pool_flow_failed", ticker=ticker, error=str(exc))
            raise ConnectorError(f"Failed to get dark pool flow for {ticker!r}") from exc

    async def get_congressional_trades(self, ticker: str | None = None) -> dict:
        """
        Fetch congressional stock trade disclosures.

        Endpoint: GET /congress/trades[?ticker={ticker}]

        Args:
            ticker: Optional uppercase ticker symbol to filter results.

        Returns:
            Congressional trades payload.
        """
        self._log.info("get_congressional_trades", ticker=ticker)
        params = {"ticker": ticker.upper().strip()} if ticker else None
        try:
            return await self._get("/congress/trades", params=params, ttl=_TTL_REFERENCE)
        except Exception as exc:
            self._log.error("get_congressional_trades_failed", ticker=ticker, error=str(exc))
            raise ConnectorError(f"Failed to get congressional trades for {ticker!r}") from exc

    async def get_insider_transactions(self, ticker: str | None = None) -> dict:
        """
        Fetch SEC Form 4 insider transaction disclosures.

        Endpoint: GET /insider/transactions[?ticker={ticker}]

        Args:
            ticker: Optional uppercase ticker symbol to filter results.

        Returns:
            Insider transactions payload.
        """
        self._log.info("get_insider_transactions", ticker=ticker)
        params = {"ticker": ticker.upper().strip()} if ticker else None
        try:
            return await self._get("/insider/transactions", params=params, ttl=_TTL_REFERENCE)
        except Exception as exc:
            self._log.error("get_insider_transactions_failed", ticker=ticker, error=str(exc))
            raise ConnectorError(f"Failed to get insider transactions for {ticker!r}") from exc

    async def get_etf_exposure(self, ticker: str) -> dict:
        """
        Fetch which ETFs hold a given ticker and their weight.

        Endpoint: GET /etf/{ticker}/exposure

        Args:
            ticker: Uppercase ticker symbol (can be a stock or ETF).

        Returns:
            ETF exposure payload.
        """
        ticker = ticker.upper().strip()
        self._log.info("get_etf_exposure", ticker=ticker)
        try:
            return await self._get(f"/etf/{ticker}/exposure", ttl=_TTL_REFERENCE)
        except Exception as exc:
            self._log.error("get_etf_exposure_failed", ticker=ticker, error=str(exc))
            raise ConnectorError(f"Failed to get ETF exposure for {ticker}") from exc

    async def get_market_overview(self) -> dict:
        """
        Fetch a broad market-overview snapshot from Unusual Whales.

        Endpoint: GET /market/overview

        Returns:
            Market overview payload.
        """
        self._log.info("get_market_overview")
        try:
            return await self._get("/market/overview", ttl=_TTL_FLOW)
        except Exception as exc:
            self._log.error("get_market_overview_failed", error=str(exc))
            raise ConnectorError("Failed to get market overview") from exc

    # ------------------------------------------------------------------
    # Orchestrator entry point
    # ------------------------------------------------------------------

    async def search_for_topic(self, topic: str) -> list[dict]:
        """
        High-level method for the orchestrator.

        Extracts likely ticker symbols from `topic`, then fetches options
        flow, dark-pool flow, insider transactions, and congressional trades
        for each identified ticker.  Also retrieves a market overview when
        no specific tickers are found.

        Args:
            topic: Free-form research topic, e.g. "NVDA unusual call activity".

        Returns:
            List of finding dicts keyed by ticker with nested data.
        """
        self._log.info("search_for_topic", topic=topic)

        candidate_tickers = [
            t for t in list(dict.fromkeys(_TICKER_RE.findall(topic)))
            if t not in _TICKER_BLOCKLIST
        ][:5]

        findings: list[dict] = []

        if not candidate_tickers:
            self._log.info("no_tickers_found_fetching_market_overview", topic=topic)
            try:
                overview = await self.get_market_overview()
                findings.append(
                    {
                        "source": "unusual_whales",
                        "type": "market_overview",
                        "topic": topic,
                        "data": overview,
                    }
                )
            except ConnectorError as exc:
                self._log.warning("market_overview_failed", error=str(exc))
            return findings

        for ticker in candidate_tickers:
            finding: dict = {"source": "unusual_whales", "ticker": ticker, "topic": topic}

            results = await asyncio.gather(
                self.get_options_flow(ticker),
                self.get_dark_pool_flow(ticker),
                self.get_insider_transactions(ticker),
                self.get_congressional_trades(ticker),
                return_exceptions=True,
            )
            keys = ["options_flow", "dark_pool", "insider_transactions", "congressional_trades"]
            for key, result in zip(keys, results):
                if isinstance(result, Exception):
                    finding[f"{key}_error"] = str(result)
                else:
                    finding[key] = result

            findings.append(finding)
            self._log.info("topic_finding_collected", ticker=ticker)

        return findings
