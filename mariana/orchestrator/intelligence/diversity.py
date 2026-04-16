"""
Mariana Intelligence Engine — Source Diversity Enforcer (System 12)

A constraint system that ensures the evidence base isn't dominated by a single
source type, domain, or viewpoint. If 80% of evidence comes from one website,
the system actively seeks alternative sources before proceeding to synthesis.

Real analysts cross-reference across source types (academic, industry,
government, primary data).
"""

from __future__ import annotations

import structlog
from typing import Any

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Diversity thresholds
# ---------------------------------------------------------------------------

# Maximum allowed concentration from a single domain
MAX_SINGLE_DOMAIN_RATIO = 0.40

# Maximum allowed concentration from a single source type
MAX_SINGLE_TYPE_RATIO = 0.60

# Minimum distinct source types required for adequate diversity
MIN_SOURCE_TYPES = 3

# Minimum distinct domains required
MIN_DISTINCT_DOMAINS = 4


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

async def assess_diversity(
    task_id: str,
    db: Any,
) -> dict[str, Any]:
    """
    Assess source diversity for a research task.

    Returns a diversity report with flags for any concentration issues.
    """
    log = logger.bind(component="assess_diversity")

    # 1. Domain distribution
    domain_rows = await db.fetch(
        """
        SELECT domain, COUNT(*) as cnt
        FROM source_scores
        WHERE task_id = $1
        GROUP BY domain
        ORDER BY cnt DESC
        """,
        task_id,
    )

    total_sources = sum(r["cnt"] for r in domain_rows) if domain_rows else 0
    domain_dist = {r["domain"]: r["cnt"] for r in domain_rows} if domain_rows else {}

    # 2. Source type (domain_authority) distribution
    type_rows = await db.fetch(
        """
        SELECT domain_authority, COUNT(*) as cnt
        FROM source_scores
        WHERE task_id = $1
        GROUP BY domain_authority
        ORDER BY cnt DESC
        """,
        task_id,
    )
    type_dist = {r["domain_authority"]: r["cnt"] for r in type_rows} if type_rows else {}

    # 3. Publication type distribution
    pub_rows = await db.fetch(
        """
        SELECT publication_type, COUNT(*) as cnt
        FROM source_scores
        WHERE task_id = $1
        GROUP BY publication_type
        ORDER BY cnt DESC
        """,
        task_id,
    )
    pub_dist = {r["publication_type"]: r["cnt"] for r in pub_rows} if pub_rows else {}

    # 4. Assess issues
    issues: list[dict[str, Any]] = []

    # Check single domain dominance
    if total_sources > 0:
        for domain, count in domain_dist.items():
            ratio = count / total_sources
            if ratio > MAX_SINGLE_DOMAIN_RATIO:
                issues.append({
                    "type": "domain_concentration",
                    "severity": "high",
                    "domain": domain,
                    "ratio": ratio,
                    "message": f"Domain '{domain}' accounts for {ratio:.0%} of sources "
                    f"(threshold: {MAX_SINGLE_DOMAIN_RATIO:.0%})",
                    "recommendation": f"Seek alternative sources outside {domain}",
                })

        # Check source type dominance
        for stype, count in type_dist.items():
            ratio = count / total_sources
            if ratio > MAX_SINGLE_TYPE_RATIO:
                issues.append({
                    "type": "type_concentration",
                    "severity": "medium",
                    "source_type": stype,
                    "ratio": ratio,
                    "message": f"Source type '{stype}' accounts for {ratio:.0%} of sources "
                    f"(threshold: {MAX_SINGLE_TYPE_RATIO:.0%})",
                    "recommendation": f"Diversify beyond {stype} sources",
                })

        # Check minimum diversity
        if len(type_dist) < MIN_SOURCE_TYPES:
            issues.append({
                "type": "insufficient_types",
                "severity": "medium",
                "current_types": len(type_dist),
                "minimum": MIN_SOURCE_TYPES,
                "message": f"Only {len(type_dist)} source types found (minimum: {MIN_SOURCE_TYPES})",
                "recommendation": "Add academic, government, or industry sources",
            })

        if len(domain_dist) < MIN_DISTINCT_DOMAINS:
            issues.append({
                "type": "insufficient_domains",
                "severity": "low",
                "current_domains": len(domain_dist),
                "minimum": MIN_DISTINCT_DOMAINS,
                "message": f"Only {len(domain_dist)} distinct domains (minimum: {MIN_DISTINCT_DOMAINS})",
                "recommendation": "Broaden search to additional websites",
            })

    # 5. Compute diversity score (0-1)
    if total_sources == 0:
        diversity_score = 0.0
    else:
        # Simpson's diversity index (probability that two random sources differ)
        # D = 1 - Σ(p_i^2) where p_i is proportion of each type
        proportions = [c / total_sources for c in type_dist.values()]
        simpson = 1.0 - sum(p * p for p in proportions)
        diversity_score = min(simpson * 1.5, 1.0)  # Scale up since Simpson can be low with few types

    # 6. Build underrepresented source types list
    represented = set(type_dist.keys())
    all_desirable = {
        "academic", "government", "financial_press", "wire_service",
        "company_official", "sec_filing", "analyst_report", "industry_report",
    }
    missing_types = all_desirable - represented

    result = {
        "diversity_score": diversity_score,
        "total_sources": total_sources,
        "distinct_domains": len(domain_dist),
        "distinct_types": len(type_dist),
        "domain_distribution": domain_dist,
        "type_distribution": type_dist,
        "publication_distribution": pub_dist,
        "issues": issues,
        "is_diverse_enough": len(issues) == 0,
        "missing_types": list(missing_types),
        "recommendations": [i["recommendation"] for i in issues],
    }

    log.info(
        "diversity_assessed",
        task_id=task_id,
        score=f"{diversity_score:.2f}",
        issues=len(issues),
        types=len(type_dist),
    )
    return result


def build_diversity_constraints(diversity_report: dict[str, Any]) -> str:
    """
    Build a text block of diversity constraints for injection into search prompts.

    Used by the retrieval strategy selector to steer searches toward
    underrepresented source types.
    """
    if diversity_report.get("is_diverse_enough"):
        return ""

    lines: list[str] = ["=== SOURCE DIVERSITY CONSTRAINTS ==="]

    for issue in diversity_report.get("issues", []):
        lines.append(f"⚠ {issue['message']}")

    missing = diversity_report.get("missing_types", [])
    if missing:
        lines.append(f"\nUnderrepresented source types: {', '.join(missing)}")
        lines.append("PRIORITY: Seek sources from the underrepresented categories above.")

    for rec in diversity_report.get("recommendations", []):
        lines.append(f"→ {rec}")

    lines.append("=== END DIVERSITY CONSTRAINTS ===")
    return "\n".join(lines)
