"""
SEC EDGAR connector for US regulatory filings.

Covers full-text filing search, company submission history, XBRL financial
facts, raw filing text retrieval, and CIK lookup.

Rate limit: EDGAR enforces 10 requests/second; a semaphore caps concurrency.
Auth: SEC requires an identifying User-Agent header (no API key).

Cache policy:
  - Filing search results → 30 days
  - Company facts         → 30 days
  - Company submissions   → 30 days
  - Raw filing text       → 30 days
"""

from __future__ import annotations

import asyncio
import os
import re
# H-08 fix: use defusedxml to disable DTD/entity processing and prevent XXE
# against SEC EDGAR XML responses.  defusedxml.ElementTree mirrors the
# stdlib API so fromstring/ParseError continue to work identically.  We
# keep the stdlib ET around only for ``tostring`` (safe: it serialises an
# already-parsed tree, no entity expansion).
import xml.etree.ElementTree as _stdlib_ET
import defusedxml.ElementTree as ET  # noqa: N814
from typing import Any

import structlog

from .base import BaseConnector, ConnectorError

logger = structlog.get_logger(__name__)

# TTL constants (seconds)
_TTL_FILINGS = 30 * 24 * 3600

# Public EDGAR endpoints
# EFTS full-text search: returns Elasticsearch-style {hits: {hits: [{_source: ...}]}}
_EFTS_SEARCH = "https://efts.sec.gov/LATEST/search-index"
_DATA_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_DATA_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
_EDGAR_BROWSE = "https://www.sec.gov/cgi-bin/browse-edgar"

# SEC requires a real identifying User-Agent; configurable via EDGAR_USER_AGENT env var.
_USER_AGENT = os.getenv("EDGAR_USER_AGENT", "Mariana Research mariana-research@localhost")

# How many concurrent EDGAR requests we allow (<=10 req/s safety net)
_MAX_CONCURRENT = 5

# BUG-014 fix: require at least 2 uppercase letters (matching polygon_connector)
# to avoid spurious CIK lookups for single-letter words like "I", "A", "S".
_TICKER_RE = re.compile(r"\b([A-Z]{2,5})\b")
_WORD_RE = re.compile(r"[A-Za-z]{3,}")


def _zero_pad_cik(cik: str) -> str:
    """Return a 10-digit zero-padded CIK string as expected by EDGAR data APIs."""
    stripped = cik.strip()
    if not stripped.isdigit():
        raise ConnectorError(f"Invalid CIK (must be numeric): {cik!r}")
    return stripped.zfill(10)


class SecEdgarConnector(BaseConnector):
    """
    Async connector for SEC EDGAR public APIs.

    All network calls are gated by an asyncio.Semaphore to stay within the
    10 req/s rate limit.  All methods check the shared cache first.

    Args:
        config: Application config object (no required attributes; CIK lookup
                uses public EDGAR search).
        cache:  Optional async cache (get/set with ttl).
    """

    def __init__(self, config: Any, cache: Any | None = None) -> None:
        super().__init__(config, cache)
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        # Attach identifying headers to the shared httpx client
        self.client.headers.update({"User-Agent": _USER_AGENT})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _edgar_get_json(self, url: str, params: dict | None = None, ttl: int = _TTL_FILINGS) -> dict:
        """Cached GET that expects a JSON response from EDGAR."""
        cache_key = self._cache_key("sec", url, str(sorted((params or {}).items())))
        cached = await self._cache_get(cache_key)
        if cached is not None:
            self._log.debug("cache_hit", url=url)
            return cached

        async with self._semaphore:
            data = await self._request("GET", url, params=params)

        await self._cache_set(cache_key, data, ttl=ttl)
        return data

    async def _edgar_get_text(self, url: str, ttl: int = _TTL_FILINGS) -> str:
        """Cached GET that returns raw text (for filing documents)."""
        cache_key = self._cache_key("sec_text", url)
        cached = await self._cache_get(cache_key)
        if cached is not None:
            self._log.debug("cache_hit_text", url=url)
            return cached

        async with self._semaphore:
            text = await self._request_text("GET", url)

        await self._cache_set(cache_key, text, ttl=ttl)
        return text

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def search_filings(
        self,
        query: str,
        date_range: str | None = None,
        form_type: str | None = None,
        startdt: str | None = None,   # BUG-A05 fix: e.g. "2023-01-01"
        enddt: str | None = None,     # BUG-A05 fix: e.g. "2023-12-31"
    ) -> dict:
        """
        Search SEC EDGAR full-text for filings matching a keyword query.

        Endpoint: GET https://efts.sec.gov/LATEST/search-index

        Args:
            query:      Search terms.
            date_range: Optional date range filter.  To filter by a specific
                        date window, pass ``date_range="custom"`` together with
                        ``startdt`` and ``enddt`` (the EFTS API requires all
                        three parameters for bounded date filtering).
            form_type:  Optional form type filter, e.g. "10-K", "8-K".
            startdt:    Start date for custom date range, format "YYYY-MM-DD".
                        Only used when ``date_range="custom"``.
            enddt:      End date for custom date range, format "YYYY-MM-DD".
                        Only used when ``date_range="custom"``.

        Returns:
            EFTS search-index payload.
        """
        self._log.info(
            "search_filings",
            query=query,
            date_range=date_range,
            form_type=form_type,
            startdt=startdt,
            enddt=enddt,
        )
        params: dict = {"q": query}
        if date_range:
            params["dateRange"] = date_range
        if startdt:
            params["startdt"] = startdt
        if enddt:
            params["enddt"] = enddt
        if form_type:
            params["forms"] = form_type
        try:
            return await self._edgar_get_json(_EFTS_SEARCH, params=params)
        except Exception as exc:
            self._log.error("search_filings_failed", query=query, error=str(exc))
            raise ConnectorError(f"SEC filing search failed for '{query}'") from exc

    async def get_company_submissions(self, cik: str) -> dict:
        """
        Fetch the submission history (recent filings list) for a company.

        Endpoint: GET https://data.sec.gov/submissions/CIK{cik}.json

        Args:
            cik: Company CIK number (will be zero-padded to 10 digits).

        Returns:
            EDGAR submissions payload.
        """
        padded = _zero_pad_cik(cik)
        self._log.info("get_company_submissions", cik=padded)
        url = _DATA_SUBMISSIONS.format(cik=padded)
        try:
            return await self._edgar_get_json(url)
        except Exception as exc:
            self._log.error("get_company_submissions_failed", cik=padded, error=str(exc))
            raise ConnectorError(f"Failed to get submissions for CIK {padded}") from exc

    async def get_company_facts(self, cik: str) -> dict:
        """
        Fetch XBRL financial facts for a company (all reported tags).

        Endpoint: GET https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json

        Args:
            cik: Company CIK number (will be zero-padded to 10 digits).

        Returns:
            XBRL company-facts payload.
        """
        padded = _zero_pad_cik(cik)
        self._log.info("get_company_facts", cik=padded)
        url = _DATA_FACTS.format(cik=padded)
        try:
            return await self._edgar_get_json(url)
        except Exception as exc:
            self._log.error("get_company_facts_failed", cik=padded, error=str(exc))
            raise ConnectorError(f"Failed to get facts for CIK {padded}") from exc

    async def get_filing_text(self, url: str) -> str:
        """
        Fetch the raw text of a filing document from SEC EDGAR.

        Args:
            url: Full URL to the filing document (htm/txt).

        Returns:
            Raw text content of the document.
        """
        self._log.info("get_filing_text", url=url)
        if not url.startswith("https://"):
            raise ConnectorError(f"Invalid filing URL (must be https://): {url}")
        from urllib.parse import urlparse  # noqa: PLC0415
        import re as _re  # noqa: PLC0415
        parsed_host = urlparse(url).hostname or ""
        # BUG-0010 fix: use strict regex instead of endswith(), which matched
        # "evil.sec.gov.attacker.com". Only allow exact sec.gov or subdomains.
        if not _re.match(r"^([a-z0-9-]+\.)*sec\.gov$", parsed_host):
            raise ConnectorError(f"Filing URL must be on *.sec.gov, got: {parsed_host}")
        try:
            return await self._edgar_get_text(url)
        except Exception as exc:
            self._log.error("get_filing_text_failed", url=url, error=str(exc))
            raise ConnectorError(f"Failed to fetch filing text from {url}") from exc

    async def full_text_search(self, query: str) -> dict:
        """
        Perform a full-text search across all EDGAR filings.

        Endpoint: GET https://efts.sec.gov/LATEST/search-index?q={query}

        This is an alias for search_filings() without date/form-type filters,
        kept as a dedicated method for clarity in the orchestrator.

        Args:
            query: Free-form search string.

        Returns:
            EFTS payload.
        """
        self._log.info("full_text_search", query=query)
        return await self.search_filings(query)

    async def lookup_cik(self, ticker: str) -> str | None:
        """
        Resolve a ticker symbol or company name to a CIK via the EDGAR browse
        Atom feed.

        Endpoint: GET https://www.sec.gov/cgi-bin/browse-edgar (Atom output)

        Args:
            ticker: Ticker symbol or partial company name.

        Returns:
            CIK string (no zero padding), or None if not found.
        """
        ticker = ticker.strip()
        self._log.info("lookup_cik", ticker=ticker)
        cache_key = self._cache_key("sec_cik", ticker.upper())
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return cached

        params = {
            "action": "getcompany",
            "company": ticker,
            "type": "",
            "dateb": "",
            "owner": "include",
            "count": "10",
            "search_text": "",
            "output": "atom",
        }

        try:
            async with self._semaphore:
                text = await self._request_text("GET", _EDGAR_BROWSE, params=params)
        except Exception as exc:
            self._log.error("lookup_cik_failed", ticker=ticker, error=str(exc))
            return None

        # Parse the Atom feed; the CIK lives in <company-info><cik>
        try:
            root = ET.fromstring(text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            # EDGAR Atom: <entry><content><company-info><cik>
            for entry in root.findall("atom:entry", ns):
                content = entry.find("atom:content", ns)
                if content is None:
                    continue
                # The CIK appears in a child tag with no namespace
                cik_el = content.find(".//{http://www.sec.gov/cgi-bin/browse-edgar}CIK")
                if cik_el is None:
                    # Fallback: look for text "CIK" pattern in raw text
                    raw = _stdlib_ET.tostring(content, encoding="unicode")
                    match = re.search(r"<CIK>(\d+)</CIK>", raw, re.IGNORECASE)
                    if match:
                        cik = match.group(1)
                        await self._cache_set(cache_key, cik, ttl=_TTL_FILINGS)
                        return cik
                else:
                    cik = cik_el.text.strip() if cik_el.text else None
                    if cik:
                        await self._cache_set(cache_key, cik, ttl=_TTL_FILINGS)
                        return cik
        except _stdlib_ET.ParseError as exc:
            self._log.warning("lookup_cik_parse_error", ticker=ticker, error=str(exc))

        # Last-resort regex on the raw text
        match = re.search(r"CIK[=\-]?(\d{7,10})", text, re.IGNORECASE)
        if match:
            cik = match.group(1)
            await self._cache_set(cache_key, cik, ttl=_TTL_FILINGS)
            return cik

        self._log.warning("lookup_cik_not_found", ticker=ticker)
        return None

    # ------------------------------------------------------------------
    # Orchestrator entry point
    # ------------------------------------------------------------------

    async def search_for_topic(self, topic: str) -> list[dict]:
        """
        High-level method for the orchestrator.

        Workflow:
          1. Extract ticker-like tokens and significant words from `topic`.
          2. For each candidate ticker, resolve its CIK.
          3. For resolved CIKs, fetch submissions and company facts.
          4. Also run a full-text EDGAR search on the raw topic string.

        Args:
            topic: Free-form research topic, e.g. "Apple 10-K revenue 2023".

        Returns:
            List of finding dicts with EDGAR data.
        """
        self._log.info("search_for_topic", topic=topic)

        # 1. Extract candidates
        tickers = list(dict.fromkeys(_TICKER_RE.findall(topic)))[:5]

        findings: list[dict] = []

        # 2. Full-text search on the raw topic
        try:
            ft_result = await self.full_text_search(topic)
            findings.append(
                {
                    "source": "sec_edgar",
                    "type": "full_text_search",
                    "topic": topic,
                    "data": ft_result,
                }
            )
        except ConnectorError as exc:
            self._log.warning("full_text_search_failed", topic=topic, error=str(exc))

        # 3. Per-ticker lookups
        for ticker in tickers:
            cik: str | None = None  # reset each iteration to avoid variable leak
            finding: dict = {"source": "sec_edgar", "ticker": ticker, "topic": topic}

            cik = await self.lookup_cik(ticker)
            if cik:
                finding["cik"] = cik

                try:
                    finding["submissions"] = await self.get_company_submissions(cik)
                except ConnectorError as exc:
                    finding["submissions_error"] = str(exc)

                try:
                    finding["facts"] = await self.get_company_facts(cik)
                except ConnectorError as exc:
                    finding["facts_error"] = str(exc)
            else:
                finding["cik_error"] = f"CIK not found for {ticker}"

            findings.append(finding)
            self._log.info("topic_finding_collected", ticker=ticker, cik=cik)

        return findings
