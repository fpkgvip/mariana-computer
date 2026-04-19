"""
Mariana Intelligence Engine — Main Integration Module

Provides high-level hook functions that the event loop calls at key points
in the research lifecycle. Each hook orchestrates the appropriate intelligence
systems.

Hook points:
    after_search()    — Called after handle_search. Runs claim extraction,
                        source credibility, temporal tagging, contradiction check.
    after_evaluate()  — Called after handle_evaluate. Runs confidence calibration,
                        Bayesian update, gap detection, diversity check, replanning.
    before_report()   — Called before handle_report. Runs multi-perspective synthesis,
                        reasoning audit, executive summary generation.
"""

from __future__ import annotations

import asyncio
import structlog
from typing import Any

logger = structlog.get_logger(__name__)


async def after_search(
    task_id: str,
    topic: str,
    findings: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    db: Any,
    cost_tracker: Any,
    config: Any,
    quality_tier: str | None = None,
    tier: str = "standard",
) -> dict[str, Any]:
    """
    Post-search intelligence processing.

    Called after handle_search completes for all active branches.

    1. Extract atomic claims from each new finding
    2. Score source credibility for each source used
    3. Temporal tagging (done during claim extraction)
    4. Detect contradictions among new + existing claims
    5. Bayesian update for each new claim

    Args:
        task_id: Research task ID.
        topic: Research topic.
        findings: List of finding dicts (id, content, hypothesis_id, source_ids).
        sources: List of source dicts (id, url, title, fetched_at).
        db: asyncpg pool.
        cost_tracker: Cost tracker.
        config: App config.
        quality_tier: Optional quality tier.

    Returns:
        Summary of intelligence processing results.
    """
    log = logger.bind(component="after_search")
    log.info("intelligence_after_search_start", task_id=task_id, findings=len(findings), sources=len(sources), tier=tier)

    results: dict[str, Any] = {
        "claims_extracted": 0,
        "sources_scored": 0,
        "contradictions_found": 0,
        "bayesian_updates": 0,
    }

    # 1. Extract claims from each finding
    # Tier-aware cap: standard processes top 5 findings, deep processes all
    _findings_cap = {"instant": 0, "quick": 0, "standard": 5, "deep": 50}.get(tier, 5)
    _capped_findings = findings[:_findings_cap]
    if len(findings) > _findings_cap:
        log.info("findings_capped_for_claim_extraction", tier=tier, total=len(findings), processing=_findings_cap)

    if not _capped_findings:
        log.info("claim_extraction_skipped", tier=tier, reason="tier_cap_zero")
        return results

    from mariana.orchestrator.intelligence.evidence_ledger import extract_claims_from_finding

    all_new_claims: list[dict[str, Any]] = []
    for finding in _capped_findings:
        try:
            # Get hypothesis statement for context
            hyp_row = await db.fetchrow(
                "SELECT statement FROM hypotheses WHERE id = $1",
                finding.get("hypothesis_id", ""),
            )
            hyp_stmt = hyp_row["statement"] if hyp_row else ""

            claims = await extract_claims_from_finding(
                finding_id=finding["id"],
                finding_content=finding.get("content", ""),
                hypothesis_statement=hyp_stmt,
                task_id=task_id,
                source_ids=finding.get("source_ids", []),
                db=db,
                cost_tracker=cost_tracker,
                config=config,
                quality_tier=quality_tier,
            )
            all_new_claims.extend(claims)
        except Exception as exc:
            log.warning("claim_extraction_failed", finding_id=finding.get("id"), error=str(exc))

    results["claims_extracted"] = len(all_new_claims)

    # 2. Score source credibility
    # Tier-aware: standard caps at 3 sources, deep processes all
    _source_cap = {"instant": 0, "quick": 0, "standard": 3, "deep": 50}.get(tier, 3)
    _capped_sources = sources[:_source_cap]
    from mariana.orchestrator.intelligence.credibility import score_source

    scored_sources = 0
    for source in _capped_sources:
        try:
            fetched_at = source.get("fetched_at")
            if fetched_at is None:
                from datetime import datetime, timezone
                fetched_at = datetime.now(timezone.utc)

            await score_source(
                source_id=source["id"],
                source_url=source.get("url", ""),
                source_title=source.get("title"),
                fetched_at=fetched_at,
                task_id=task_id,
                research_topic=topic,
                db=db,
                cost_tracker=cost_tracker,
                config=config,
                quality_tier=quality_tier,
                use_llm=True,
            )
            scored_sources += 1
        except Exception as exc:
            log.warning("source_scoring_failed", source_id=source.get("id"), error=str(exc))

    results["sources_scored"] = scored_sources

    # 3. Detect contradictions (deep tier only — uses Sonnet, expensive for standard)
    if tier == "deep":
        claim_count_row = await db.fetchval("SELECT COUNT(*) FROM claims WHERE task_id = $1", task_id)
        if claim_count_row and claim_count_row >= 4:
            try:
                from mariana.orchestrator.intelligence.contradictions import detect_contradictions
                contradictions = await detect_contradictions(
                    task_id=task_id,
                    db=db,
                    cost_tracker=cost_tracker,
                    config=config,
                    quality_tier=quality_tier,
                )
                results["contradictions_found"] = len(contradictions)
            except Exception as exc:
                log.warning("contradiction_detection_failed", error=str(exc))
    else:
        log.info("contradiction_detection_skipped", tier=tier, reason="not_deep_tier")

    # 4. Bayesian update for each new claim
    # Standard tier: cap at 3 to save time; deep tier: cap at 10.
    from mariana.orchestrator.intelligence.hypothesis_engine import bayesian_update

    _bayes_cap = 3 if tier == "standard" else 10
    updates = 0
    for claim in all_new_claims[:_bayes_cap]:  # Tier-aware cap
        try:
            await bayesian_update(
                task_id=task_id,
                claim_id=claim["id"],
                claim_text=claim.get("claim_text", ""),
                db=db,
                cost_tracker=cost_tracker,
                config=config,
                quality_tier=quality_tier,
            )
            updates += 1
        except Exception as exc:
            log.warning("bayesian_update_failed", claim_id=claim.get("id"), error=str(exc))

    results["bayesian_updates"] = updates

    log.info("intelligence_after_search_complete", task_id=task_id, **results)
    return results


async def after_evaluate(
    task_id: str,
    topic: str,
    evaluation_cycle: int,
    db: Any,
    cost_tracker: Any,
    config: Any,
    quality_tier: str | None = None,
    tier: str = "standard",
) -> dict[str, Any]:
    """
    Post-evaluation intelligence processing.

    Called after handle_evaluate completes.

    1. Calibrate all claim confidences
    2. Assess source diversity
    3. Detect gaps in evidence (deep/standard only — skipped for quick)
    4. Replan if needed (deep tier only — standard doesn't loop back)

    Args:
        tier: Investigation tier (instant/quick/standard/deep). Controls
              which intelligence systems run. Standard tier skips replanning
              and gap detection to stay within 3-5 min budget.

    Returns summary of processing results.
    """
    log = logger.bind(component="after_evaluate")
    log.info("intelligence_after_evaluate_start", task_id=task_id, cycle=evaluation_cycle, tier=tier)

    results: dict[str, Any] = {
        "calibrated": 0,
        "diversity_score": 0.0,
        "gaps_found": 0,
        "replanned": False,
    }

    # 1. Calibrate all claims (cheap — always run)
    try:
        from mariana.orchestrator.intelligence.confidence import calibrate_all_claims
        cal_result = await calibrate_all_claims(task_id, db)
        results["calibrated"] = cal_result.get("calibrated", 0)
    except Exception as exc:
        log.warning("calibration_failed", error=str(exc))

    # 2. Source diversity assessment (cheap — always run)
    try:
        from mariana.orchestrator.intelligence.diversity import assess_diversity
        div_result = await assess_diversity(task_id, db)
        results["diversity_score"] = div_result.get("diversity_score", 0.0)
        results["diversity_issues"] = len(div_result.get("issues", []))
    except Exception as exc:
        log.warning("diversity_assessment_failed", error=str(exc))

    # 3. Gap detection (skip for standard tier — it doesn't loop back)
    if tier == "deep":
        try:
            from mariana.orchestrator.intelligence.gap_detector import detect_gaps
            gap_result = await detect_gaps(
                task_id=task_id,
                research_topic=topic,
                db=db,
                cost_tracker=cost_tracker,
                config=config,
                quality_tier=quality_tier,
            )
            results["gaps_found"] = len(gap_result.get("gaps", []))
            results["completeness_score"] = gap_result.get("completeness_score", 0.0)
            results["follow_up_queries"] = gap_result.get("follow_up_queries", [])
        except Exception as exc:
            log.warning("gap_detection_failed", error=str(exc))
    else:
        log.info("gap_detection_skipped", tier=tier, reason="not_deep_tier")

    # 4. Replanning (deep tier only — standard doesn't benefit)
    if tier == "deep":
        try:
            from mariana.orchestrator.intelligence.replanner import should_replan, execute_replan
            if await should_replan(task_id, evaluation_cycle, db):
                replan_result = await execute_replan(
                    task_id=task_id,
                    research_topic=topic,
                    evaluation_cycle=evaluation_cycle,
                    db=db,
                    cost_tracker=cost_tracker,
                    config=config,
                    quality_tier=quality_tier,
                )
                results["replanned"] = replan_result.get("replanned", False)
                results["replan_modifications"] = len(replan_result.get("modifications", []))
        except Exception as exc:
            log.warning("replanning_failed", error=str(exc))
    else:
        log.info("replanning_skipped", tier=tier, reason="not_deep_tier")

    log.info("intelligence_after_evaluate_complete", task_id=task_id, **{
        k: v for k, v in results.items() if not isinstance(v, list)
    })
    return results


async def before_report(
    task_id: str,
    topic: str,
    db: Any,
    cost_tracker: Any,
    config: Any,
    quality_tier: str | None = None,
    tier: str = "standard",
) -> dict[str, Any]:
    """
    Pre-report intelligence processing.

    Called before handle_report generates the final report.

    For deep tier (full pipeline):
        1. Generate multi-perspective synthesis (bull/bear/skeptic/expert)
        2. Run reasoning chain audit (quality gate)
        3. Generate executive summaries at all compression levels

    For standard tier (fast pipeline):
        1. Skip perspectives (4× Opus calls = ~2 min saved)
        2. Skip reasoning audit (1× Opus call = ~30s saved)
        3. Keep executive summary (1× Sonnet call = ~15s, high value)

    Args:
        tier: Investigation tier. Controls which finalization hooks run.
              Standard tier skips perspectives + audit to stay under 5 min.

    Returns summary including audit pass/fail.
    """
    log = logger.bind(component="before_report")
    log.info("intelligence_before_report_start", task_id=task_id, tier=tier)

    results: dict[str, Any] = {
        "perspectives_generated": 0,
        "audit_passed": False,
        "audit_score": 0.0,
        "summaries_generated": False,
    }

    # 1. Multi-perspective synthesis (deep tier only — 4× Opus calls are expensive)
    if tier == "deep":
        try:
            from mariana.orchestrator.intelligence.perspectives import (
                generate_perspectives,
                meta_synthesize,
            )
            perspectives = await generate_perspectives(
                task_id=task_id,
                research_topic=topic,
                db=db,
                cost_tracker=cost_tracker,
                config=config,
                quality_tier=quality_tier,
            )
            results["perspectives_generated"] = len(perspectives)

            # Meta-synthesize
            if perspectives:
                meta = await meta_synthesize(
                    task_id=task_id,
                    perspectives=perspectives,
                    research_topic=topic,
                    db=db,
                    cost_tracker=cost_tracker,
                    config=config,
                    quality_tier=quality_tier,
                )
                results["meta_synthesis"] = meta
        except Exception as exc:
            log.warning("perspective_synthesis_failed", error=str(exc))
    else:
        log.info("perspectives_skipped", tier=tier, reason="not_deep_tier")

    # 2. Reasoning chain audit
    from mariana.orchestrator.intelligence.auditor import audit_reasoning_chain  # noqa: PLC0415
    if tier == "deep":
        try:
            audit = await audit_reasoning_chain(
                task_id=task_id,
                research_topic=topic,
                db=db,
                cost_tracker=cost_tracker,
                config=config,
                quality_tier=quality_tier,
            )
            results["audit_passed"] = audit.get("passed", False)
            results["audit_score"] = audit.get("overall_score", 0.0)
            results["audit_issues"] = audit.get("total_issues", 0)
        except Exception as exc:
            log.warning("reasoning_audit_failed", error=str(exc))
    else:
        # BUG-0021 fix: run a lite audit for non-deep tiers instead of auto-passing.
        # Auto-passing with score=1.0 let unreviewed content through.
        log.info("reasoning_audit_lite", tier=tier)
        try:
            audit = await audit_reasoning_chain(
                task_id=task_id,
                research_topic=topic,
                db=db,
                cost_tracker=cost_tracker,
                config=config,
                quality_tier=quality_tier,
                audit_type="lite",
            )
            results["audit_passed"] = audit.get("passed", False)
            results["audit_score"] = audit.get("overall_score", 0.0)
            results["audit_issues"] = audit.get("total_issues", 0)
        except Exception as exc:
            log.warning("reasoning_audit_lite_failed", error=str(exc))
            # On failure, default to not-passed to be safe
            results["audit_passed"] = False
            results["audit_score"] = 0.0

    # 3. Executive summaries (always run — cheap and high-value)
    try:
        from mariana.orchestrator.intelligence.executive_summary import generate_executive_summaries
        summaries = await generate_executive_summaries(
            task_id=task_id,
            research_topic=topic,
            db=db,
            cost_tracker=cost_tracker,
            config=config,
            quality_tier=quality_tier,
        )
        results["summaries_generated"] = True
        results["one_liner"] = summaries.get("one_liner", "")
    except Exception as exc:
        log.warning("executive_summary_failed", error=str(exc))

    log.info("intelligence_before_report_complete", task_id=task_id, **{
        k: v for k, v in results.items()
        if not isinstance(v, (dict, list)) and k != "one_liner"
    })
    return results
