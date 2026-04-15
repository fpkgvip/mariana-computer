"""Perplexity Sonar API integration for web search with citations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class SearchResult:
    """A single Perplexity search result with citations."""

    query: str
    answer: str
    citations: list[dict[str, str]] = field(default_factory=list)  # [{url, title, snippet}]


async def search_perplexity(
    query: str,
    api_key: str,
    model: str = "sonar",
    timeout: float = 60.0,
) -> SearchResult:
    """Execute a single search query via the Perplexity Sonar API.

    Parameters
    ----------
    query:
        Natural-language search query.
    api_key:
        Perplexity API key (``pplx-…``).
    model:
        Sonar model variant (default ``"sonar"``).
    timeout:
        HTTP request timeout in seconds.

    Returns
    -------
    SearchResult
        Contains the answer text and a list of citation dicts.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            "https://api.perplexity.ai/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": query}],
                "return_citations": True,
                "return_related_questions": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    content = data["choices"][0]["message"]["content"]
    raw_citations = data.get("citations", [])
    citations: list[dict[str, str]] = [
        {"url": c, "title": "", "snippet": ""} if isinstance(c, str) else c
        for c in raw_citations
    ]
    return SearchResult(query=query, answer=content, citations=citations)


async def parallel_search(
    queries: list[str],
    api_key: str,
    max_concurrent: int = 5,
) -> list[SearchResult]:
    """Run multiple Perplexity searches in parallel with a concurrency limit.

    Parameters
    ----------
    queries:
        List of search queries to execute.
    api_key:
        Perplexity API key.
    max_concurrent:
        Maximum number of concurrent API requests.

    Returns
    -------
    list[SearchResult]
        Results in the same order as *queries*.  Failed searches return a
        ``SearchResult`` with an error message in the ``answer`` field.
    """
    sem = asyncio.Semaphore(max_concurrent)

    async def _search(q: str) -> SearchResult:
        async with sem:
            try:
                return await search_perplexity(q, api_key)
            except Exception as exc:
                logger.warning("perplexity_search_failed", query=q[:80], error=str(exc))
                return SearchResult(query=q, answer=f"[Search failed: {exc}]", citations=[])

    return list(await asyncio.gather(*[_search(q) for q in queries]))


def format_results_with_citations(results: list[SearchResult]) -> str:
    """Format search results into a context string with inline citations.

    Each result is rendered as::

        Source: [Title](url)
        Finding: <answer excerpt>
    """
    parts: list[str] = []
    for r in results:
        parts.append(f"### Search: {r.query}")
        parts.append(r.answer)
        if r.citations:
            parts.append("\nSources:")
            for c in r.citations:
                title = c.get("title") or c.get("url", "")
                url = c.get("url", "")
                parts.append(f"  - [Source: {title}]({url})")
        parts.append("")
    return "\n".join(parts)
