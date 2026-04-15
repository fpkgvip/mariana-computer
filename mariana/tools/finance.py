"""Financial data tools — SEC EDGAR, Coinbase, etc.

Consolidates public-API financial data connectors used by the research
orchestrator.  Each function returns a ``FinancialDataResult`` with source
attribution and citations suitable for direct injection into AI context.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class FinancialDataResult:
    """Structured result from a financial data query."""

    source: str
    query: str
    data: dict[str, object] | list[object]
    citations: list[dict[str, str]] = field(default_factory=list)  # [{url, title}]


# ---------------------------------------------------------------------------
# SEC EDGAR (free, no API key)
# ---------------------------------------------------------------------------


async def search_sec_edgar(
    query: str,
    ticker: str = "",
    timeout: float = 30.0,
) -> FinancialDataResult:
    """Search the SEC EDGAR full-text search API.

    Parameters
    ----------
    query:
        Free-text search string (e.g. ``"revenue recognition"``)
    ticker:
        Optional ticker symbol to scope the search.
    timeout:
        HTTP request timeout in seconds.
    """
    params: dict[str, str] = {
        "q": f'"{ticker}" {query}' if ticker else query,
        "dateRange": "custom",
        "startdt": "2020-01-01",
        "forms": "10-K,10-Q,8-K,DEF 14A",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            "https://efts.sec.gov/LATEST/search-index",
            params=params,
            headers={"User-Agent": "Mariana Research research@mariana.co"},
        )
        resp.raise_for_status()
        data = resp.json()

    hits = data.get("hits", {}).get("hits", [])
    citations: list[dict[str, str]] = []
    for h in hits[:5]:
        src = h.get("_source", {})
        entity_id = src.get("entity_id", "")
        file_num = src.get("file_num", "")
        names = src.get("display_names", ["SEC Filing"])
        citations.append({
            "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={entity_id}&type=&dateb=&owner=include&count=40",
            "title": names[0] if names else "SEC Filing",
        })

    logger.info("sec_edgar_search", query=query[:60], hits=len(hits))
    return FinancialDataResult(source="SEC EDGAR", query=query, data=data, citations=citations)


# ---------------------------------------------------------------------------
# Coinbase (free, no API key)
# ---------------------------------------------------------------------------


async def get_coinbase_price(
    symbol: str,
    timeout: float = 15.0,
) -> FinancialDataResult:
    """Get the current spot price for a crypto asset from Coinbase.

    Parameters
    ----------
    symbol:
        Crypto ticker (e.g. ``"BTC"``, ``"ETH"``).
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"https://api.coinbase.com/v2/prices/{symbol}-USD/spot")
        resp.raise_for_status()
        price_data = resp.json()["data"]

    result_data: dict[str, object] = {
        "symbol": symbol,
        "price_usd": float(price_data["amount"]),
        "currency": price_data["currency"],
    }
    return FinancialDataResult(
        source="Coinbase",
        query=f"{symbol} price",
        data=result_data,
        citations=[{
            "url": f"https://www.coinbase.com/price/{symbol.lower()}",
            "title": f"Coinbase {symbol} Price",
        }],
    )


# ---------------------------------------------------------------------------
# Quartr (earnings call transcripts — requires API key)
# ---------------------------------------------------------------------------


# Quartr integration removed per user request.
