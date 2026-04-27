"""
Mariana Intelligence Engine — Source Credibility Scoring Engine (System 3)

Not all sources are equal. Every retrieved document receives a composite score:
    SourceScore = Credibility × Relevance × Recency

The credibility component factors in domain authority, publication type
(peer-reviewed vs. blog), historical accuracy, and cross-reference density.
This score determines how much weight that source's claims carry in synthesis.
"""

from __future__ import annotations

import structlog
import math
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from mariana.ai.session import spawn_model
from mariana.data.models import TaskType

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Domain authority lookup (static, updated by LLM classification)
# ---------------------------------------------------------------------------

# Base credibility scores for well-known domain categories.
# These are priors — the LLM can override for specific domains.
DOMAIN_AUTHORITY_SCORES: dict[str, float] = {
    # Tier 1: Government, central banks, international orgs
    "government": 0.95,
    "central_bank": 0.95,
    "international_org": 0.92,
    # Tier 2: Academic, peer-reviewed
    "academic": 0.90,
    "peer_reviewed": 0.92,
    # Tier 3: Major wire services, established financial press
    "wire_service": 0.85,
    "financial_press": 0.82,
    "major_news": 0.80,
    # Tier 4: Industry reports, company filings
    "industry_report": 0.78,
    "sec_filing": 0.88,
    "company_official": 0.75,
    # Tier 5: Analyst reports, trade publications
    "analyst_report": 0.72,
    "trade_publication": 0.70,
    # Tier 6: General news, magazines
    "general_news": 0.65,
    "magazine": 0.60,
    # Tier 7: Blogs, social media, forums
    "blog": 0.40,
    "social_media": 0.30,
    "forum": 0.25,
    "unknown": 0.50,
}

# Known domain → authority category mappings
KNOWN_DOMAINS: dict[str, str] = {
    # Government
    "sec.gov": "sec_filing",
    "treasury.gov": "government",
    "bls.gov": "government",
    "census.gov": "government",
    "federalregister.gov": "government",
    "data.gov": "government",
    "who.int": "international_org",
    "imf.org": "international_org",
    "worldbank.org": "international_org",
    "bis.org": "international_org",
    "federalreserve.gov": "central_bank",
    "ecb.europa.eu": "central_bank",
    "boj.or.jp": "central_bank",
    "pbc.gov.cn": "central_bank",
    # Academic
    "arxiv.org": "academic",
    "scholar.google.com": "academic",
    "pubmed.ncbi.nlm.nih.gov": "peer_reviewed",
    "nature.com": "peer_reviewed",
    "science.org": "peer_reviewed",
    "ssrn.com": "academic",
    "nber.org": "academic",
    # Financial press
    "reuters.com": "wire_service",
    "apnews.com": "wire_service",
    "bloomberg.com": "financial_press",
    "ft.com": "financial_press",
    "wsj.com": "financial_press",
    "economist.com": "financial_press",
    "cnbc.com": "financial_press",
    # Major news
    "nytimes.com": "major_news",
    "washingtonpost.com": "major_news",
    "bbc.com": "major_news",
    "theguardian.com": "major_news",
    # Trade / industry
    "techcrunch.com": "trade_publication",
    "arstechnica.com": "trade_publication",
    "seekingalpha.com": "analyst_report",
    # Company
    "investor.apple.com": "company_official",
    # Blogs / social
    "medium.com": "blog",
    "substack.com": "blog",
    "twitter.com": "social_media",
    "x.com": "social_media",
    "reddit.com": "forum",
}


# ---------------------------------------------------------------------------
# Pydantic schemas for LLM classification
# ---------------------------------------------------------------------------

class SourceClassification(BaseModel):
    """LLM classification of a source's authority."""
    domain_authority: str = Field(
        ...,
        description="One of: government, central_bank, international_org, academic, "
        "peer_reviewed, wire_service, financial_press, major_news, "
        "industry_report, sec_filing, company_official, analyst_report, "
        "trade_publication, general_news, magazine, blog, social_media, forum, unknown",
    )
    publication_type: str = Field(
        ...,
        description="One of: peer_reviewed, editorial, press_release, blog_post, "
        "official_report, data_release, opinion, research_note, news_article, unknown",
    )
    rationale: str = Field(..., description="Brief explanation for the classification")
    relevance_to_topic: float = Field(
        ..., ge=0.0, le=1.0,
        description="How relevant this source is to the research topic [0, 1]",
    )


class SourceClassificationOutput(BaseModel):
    """Output from source classification LLM call."""
    classification: SourceClassification


# ---------------------------------------------------------------------------
# Core scoring functions
# ---------------------------------------------------------------------------

def compute_recency_score(fetched_at: datetime, half_life_days: float = 180.0) -> float:
    """
    Compute recency score using exponential decay.

    Half-life of 180 days: a source from 6 months ago gets 0.5.
    A source from today gets ~1.0. A source from 2 years ago gets ~0.06.
    """
    now = datetime.now(timezone.utc)
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    age_days = (now - fetched_at).total_seconds() / 86400.0
    if age_days < 0:
        return 1.0
    decay_constant = math.log(2) / half_life_days
    return math.exp(-decay_constant * age_days)


def get_domain_from_url(url: str) -> str:
    """Extract the registrable domain from a URL."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        # Strip www prefix
        if host.startswith("www."):
            host = host[4:]
        return host.lower()
    except Exception:
        return "unknown"


def lookup_domain_authority(domain: str) -> tuple[str, float]:
    """
    Look up domain authority from the static table.

    Returns (category, base_credibility_score).
    """
    # Check exact domain match
    if domain in KNOWN_DOMAINS:
        category = KNOWN_DOMAINS[domain]
        return category, DOMAIN_AUTHORITY_SCORES.get(category, 0.50)

    # Check if any known domain is a suffix (e.g., "blog.reuters.com" → reuters.com)
    for known_domain, category in KNOWN_DOMAINS.items():
        if domain.endswith("." + known_domain) or domain == known_domain:
            return category, DOMAIN_AUTHORITY_SCORES.get(category, 0.50)

    # TLD-based heuristics
    if domain.endswith(".gov") or domain.endswith(".gov.uk"):
        return "government", 0.90
    if domain.endswith(".edu") or domain.endswith(".ac.uk"):
        return "academic", 0.85
    if domain.endswith(".org"):
        return "unknown", 0.60  # .org is too varied

    return "unknown", 0.50


async def score_source(
    source_id: str,
    source_url: str,
    source_title: str | None,
    fetched_at: datetime,
    task_id: str,
    research_topic: str,
    db: Any,
    cost_tracker: Any,
    config: Any,
    quality_tier: str | None = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    """
    Compute the composite credibility score for a source.

    SourceScore = Credibility × Relevance × Recency

    Args:
        source_id: ID of the source record.
        source_url: URL of the source.
        source_title: Title of the source page.
        fetched_at: When the source was fetched.
        task_id: Parent research task ID.
        research_topic: The topic being researched (for relevance scoring).
        db: asyncpg pool.
        cost_tracker: Cost tracker instance.
        config: App config.
        quality_tier: Optional quality tier.
        use_llm: Whether to use LLM for classification (False = static only).

    Returns:
        Dict with all scoring components and composite score.
    """
    log = logger.bind(component="score_source")
    domain = get_domain_from_url(source_url)

    # 1. Static domain authority lookup
    domain_authority, base_credibility = lookup_domain_authority(domain)

    # 2. Recency score (algorithmic)
    recency = compute_recency_score(fetched_at)

    # 3. LLM classification for relevance + refined authority
    relevance = 0.5  # default
    publication_type = "unknown"
    rationale = "Static lookup only"

    if use_llm:
        try:
            output, _session = await spawn_model(
                task_type=TaskType.SOURCE_CREDIBILITY,
                context={
                    "task_id": task_id,
                    "source_url": source_url,
                    "source_title": source_title or "",
                    "domain": domain,
                    "research_topic": research_topic,
                },
                output_schema=SourceClassificationOutput,
                db=db,
                cost_tracker=cost_tracker,
                config=config,
                quality_tier=quality_tier,
            )
            parsed: SourceClassificationOutput = output  # type: ignore[assignment]
            classification = parsed.classification

            # Use LLM classification if it's more specific than our static lookup
            if classification.domain_authority in DOMAIN_AUTHORITY_SCORES:
                domain_authority = classification.domain_authority
                base_credibility = DOMAIN_AUTHORITY_SCORES[classification.domain_authority]

            publication_type = classification.publication_type
            relevance = classification.relevance_to_topic
            rationale = classification.rationale

        except Exception as exc:
            log.warning("source_classification_llm_failed", error=str(exc), source_id=source_id)

    # 4. Cross-reference density (count how many claims cite this source)
    # BUG-0009 fix: replace LIKE wildcard injection with array containment.
    # The old query used LIKE '%' || $2 || '%' which allowed LIKE wildcards
    # (% and _) in source_id to match unintended rows.
    cross_ref_row = await db.fetchrow(
        """
        SELECT COUNT(*) as ref_count
        FROM claims
        WHERE task_id = $1 AND source_ids @> ARRAY[$2]::text[]
        """,
        task_id,
        source_id,
    )
    cross_ref_density = int(cross_ref_row["ref_count"]) if cross_ref_row else 0

    # 5. Composite score
    composite = base_credibility * relevance * recency

    # 6. Persist to source_scores table
    try:
        await db.execute(
            """
            INSERT INTO source_scores (
                source_id, task_id, domain,
                credibility, relevance, recency, composite_score,
                domain_authority, publication_type, cross_ref_density,
                scoring_rationale
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (source_id) DO UPDATE SET
                credibility = EXCLUDED.credibility,
                relevance = EXCLUDED.relevance,
                recency = EXCLUDED.recency,
                composite_score = EXCLUDED.composite_score,
                domain_authority = EXCLUDED.domain_authority,
                publication_type = EXCLUDED.publication_type,
                cross_ref_density = EXCLUDED.cross_ref_density,
                scoring_rationale = EXCLUDED.scoring_rationale
            """,
            source_id, task_id, domain,
            base_credibility, relevance, recency, composite,
            domain_authority, publication_type, cross_ref_density,
            rationale,
        )
    except Exception as exc:
        log.warning("source_score_persist_failed", error=str(exc), source_id=source_id)

    result = {
        "source_id": source_id,
        "domain": domain,
        "credibility": base_credibility,
        "relevance": relevance,
        "recency": recency,
        "composite_score": composite,
        "domain_authority": domain_authority,
        "publication_type": publication_type,
        "cross_ref_density": cross_ref_density,
        "rationale": rationale,
    }
    log.info("source_scored", **result)
    return result


# F-06 pagination constants (mirrors evidence_ledger.py).
_INTEL_MAX_LIMIT = 1000
_INTEL_DEFAULT_LIMIT = 100


async def get_source_scores(
    task_id: str,
    db: Any,
    limit: int = _INTEL_DEFAULT_LIMIT,
    cursor: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve source scores for a task with pagination.

    F-06 fix: accepts ``limit`` (capped at 1000) and ``cursor`` (created_at|id)
    for keyset pagination. Ordered by created_at ASC, id ASC.
    """
    limit = max(1, min(limit, _INTEL_MAX_LIMIT))

    if cursor:
        try:
            cursor_ts, cursor_id = cursor.split("|", 1)
            rows = await db.fetch(
                """
                SELECT ss.*, s.url, s.title
                FROM source_scores ss
                JOIN sources s ON ss.source_id = s.id
                WHERE ss.task_id = $1
                  AND (ss.created_at, ss.id) > ($2::timestamptz, $3)
                ORDER BY ss.created_at ASC, ss.id ASC
                LIMIT $4
                """,
                task_id, cursor_ts, cursor_id, limit,
            )
        except Exception:
            rows = await db.fetch(
                """
                SELECT ss.*, s.url, s.title
                FROM source_scores ss
                JOIN sources s ON ss.source_id = s.id
                WHERE ss.task_id = $1
                ORDER BY ss.created_at ASC, ss.id ASC
                LIMIT $2
                """,
                task_id, limit,
            )
    else:
        rows = await db.fetch(
            """
            SELECT ss.*, s.url, s.title
            FROM source_scores ss
            JOIN sources s ON ss.source_id = s.id
            WHERE ss.task_id = $1
            ORDER BY ss.created_at ASC, ss.id ASC
            LIMIT $2
            """,
            task_id, limit,
        )
    return [dict(r) for r in rows]


async def get_average_credibility(task_id: str, db: Any) -> float:
    """Get the average source credibility score for a task."""
    row = await db.fetchrow(
        "SELECT AVG(composite_score) as avg_score FROM source_scores WHERE task_id = $1",
        task_id,
    )
    return float(row["avg_score"]) if row and row["avg_score"] else 0.5
