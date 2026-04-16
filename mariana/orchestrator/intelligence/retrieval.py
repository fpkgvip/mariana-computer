"""
Mariana Intelligence Engine — Retrieval Strategy Selector (System 13)

Not every question is best answered by web search. The system maintains a
portfolio of retrieval strategies and selects the optimal one per query:

- web_search: General web search (Perplexity, search engines)
- academic_search: Academic papers (arXiv, Google Scholar, SSRN)
- sec_filing: SEC EDGAR filings
- government_data: Government databases (BLS, Census, FRED)
- financial_api: Financial data APIs (Polygon, Unusual Whales)
- news_archive: Historical news databases
- company_filings: Company investor relations pages
"""

from __future__ import annotations

import structlog
from typing import Any

from pydantic import BaseModel, Field

from mariana.ai.session import spawn_model
from mariana.data.models import TaskType

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Available retrieval strategies
# ---------------------------------------------------------------------------

STRATEGIES: dict[str, dict[str, Any]] = {
    "web_search": {
        "description": "General web search via Perplexity API or search engines",
        "best_for": ["current events", "general knowledge", "company news", "product info"],
        "adapters": ["perplexity_search"],
    },
    "academic_search": {
        "description": "Academic papers, research publications, working papers",
        "best_for": ["theoretical frameworks", "methodology", "peer-reviewed evidence", "statistics"],
        "adapters": ["perplexity_search"],  # With academic-focused queries
    },
    "sec_filing": {
        "description": "SEC EDGAR filings (10-K, 10-Q, 8-K, proxy statements)",
        "best_for": ["financial data", "company financials", "insider transactions", "risk factors"],
        "adapters": ["sec_edgar_connector"],
    },
    "government_data": {
        "description": "Government statistical databases (BLS, Census, FRED)",
        "best_for": ["economic data", "employment stats", "inflation", "GDP", "trade data"],
        "adapters": ["fred_connector"],
    },
    "financial_api": {
        "description": "Real-time and historical financial data APIs",
        "best_for": ["stock prices", "options data", "market data", "unusual activity"],
        "adapters": ["polygon_connector", "unusual_whales_connector"],
    },
    "news_archive": {
        "description": "Historical news articles and media coverage",
        "best_for": ["historical context", "event timelines", "media sentiment"],
        "adapters": ["perplexity_search"],  # With date-constrained queries
    },
    "company_filings": {
        "description": "Company investor relations, earnings calls, press releases",
        "best_for": ["earnings data", "guidance", "management commentary", "strategic plans"],
        "adapters": ["perplexity_search", "sec_edgar_connector"],
    },
}


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class StrategyRecommendation(BaseModel):
    """A recommended retrieval strategy for a query."""
    strategy: str = Field(
        ...,
        description="One of: web_search, academic_search, sec_filing, "
        "government_data, financial_api, news_archive, company_filings",
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0,
        description="Confidence this is the right strategy",
    )
    rationale: str = Field(..., description="Why this strategy is recommended")
    modified_query: str = Field(
        ...,
        description="Query optimized for this specific retrieval strategy",
    )


class RetrievalStrategyOutput(BaseModel):
    """Output from the strategy selector LLM call."""
    recommendations: list[StrategyRecommendation] = Field(
        ..., min_length=1,
        description="Ranked list of retrieval strategies, best first",
    )


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

async def select_strategy(
    query: str,
    research_topic: str,
    task_id: str,
    db: Any,
    cost_tracker: Any,
    config: Any,
    quality_tier: str | None = None,
    diversity_constraints: str = "",
) -> list[dict[str, Any]]:
    """
    Select the optimal retrieval strategy for a query.

    Args:
        query: The search query or hypothesis statement.
        research_topic: The overall research topic.
        task_id: Task ID.
        db: asyncpg pool.
        cost_tracker: Cost tracker.
        config: App config.
        quality_tier: Optional quality tier.
        diversity_constraints: Optional diversity constraints to bias strategy.

    Returns:
        Ranked list of strategy recommendations.
    """
    log = logger.bind(component="select_strategy")

    strategies_desc = "\n".join(
        f"- {name}: {info['description']} (best for: {', '.join(info['best_for'])})"
        for name, info in STRATEGIES.items()
    )

    try:
        output, _session = await spawn_model(
            task_type=TaskType.RETRIEVAL_STRATEGY,
            context={
                "task_id": task_id,
                "query": query,
                "research_topic": research_topic,
                "available_strategies": strategies_desc,
                "diversity_constraints": diversity_constraints,
            },
            output_schema=RetrievalStrategyOutput,
            db=db,
            cost_tracker=cost_tracker,
            config=config,
            quality_tier=quality_tier,
        )
    except Exception as exc:
        log.warning("strategy_selection_failed", error=str(exc))
        # Fallback: default to web_search
        return [{
            "strategy": "web_search",
            "confidence": 0.5,
            "rationale": "Fallback — strategy selection failed",
            "modified_query": query,
            "adapters": STRATEGIES["web_search"]["adapters"],
        }]

    parsed: RetrievalStrategyOutput = output  # type: ignore[assignment]

    results = []
    for rec in parsed.recommendations:
        strategy_info = STRATEGIES.get(rec.strategy, STRATEGIES["web_search"])
        results.append({
            "strategy": rec.strategy,
            "confidence": rec.confidence,
            "rationale": rec.rationale,
            "modified_query": rec.modified_query,
            "adapters": strategy_info["adapters"],
        })

    log.info(
        "strategy_selected",
        task_id=task_id,
        primary=results[0]["strategy"] if results else "none",
        alternatives=len(results) - 1,
    )
    return results


def get_strategy_adapters(strategy_name: str) -> list[str]:
    """Get the adapter names for a given strategy."""
    return STRATEGIES.get(strategy_name, {}).get("adapters", ["perplexity_search"])
