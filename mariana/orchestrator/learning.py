"""
mariana/orchestrator/learning.py

Learning Loop — feedback ingestion, outcome tracking, pattern extraction,
and context injection for the Mariana Computer research engine.

This module implements:
  • Automatic investigation outcome recording on task completion.
  • User feedback ingestion (ratings, corrections, preferences).
  • Pattern extraction across investigations to build learning insights.
  • Learning context generation for prompt injection.

Architecture
------------
The learning loop is passive and non-blocking.  It never modifies the
state machine or event loop flow — it only enriches prompts with insights
extracted from prior investigations.

Tables used:
  • ``learning_events`` — raw user feedback
  • ``investigation_outcomes`` — per-investigation automated metrics
  • ``learning_insights`` — extracted cross-investigation patterns
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# ===========================================================================
# Outcome Recording (automated — called when investigation completes)
# ===========================================================================


async def record_investigation_outcome(
    task_id: str,
    user_id: str,
    topic: str,
    quality_tier: str | None,
    total_cost_usd: float,
    total_ai_calls: int,
    duration_seconds: int,
    final_state: str,
    report_generated: bool,
    db: Any,
) -> str:
    """Record the outcome of a completed investigation.

    Called automatically by the event loop when a task reaches HALT or
    COMPLETED.  This captures all automated metrics that don't require
    user input.

    Parameters
    ----------
    task_id:
        UUID of the completed task.
    user_id:
        UUID of the user who initiated the task.
    topic:
        The research topic.
    quality_tier:
        The model quality tier used.
    total_cost_usd:
        Total USD spent on AI calls.
    total_ai_calls:
        Number of AI calls made.
    duration_seconds:
        Wall-clock duration from start to completion.
    final_state:
        The final state machine state (e.g., HALT, REPORT).
    report_generated:
        Whether a PDF report was generated.
    db:
        asyncpg connection pool.

    Returns
    -------
    str
        UUID of the created outcome record.
    """
    outcome_id = str(uuid.uuid4())

    # Count hypotheses, findings, killed branches from DB
    hypotheses_count = await db.fetchval(
        "SELECT COUNT(*) FROM hypotheses WHERE task_id = $1", task_id
    ) or 0
    findings_count = await db.fetchval(
        "SELECT COUNT(*) FROM findings WHERE task_id = $1", task_id
    ) or 0
    killed_branches_count = await db.fetchval(
        "SELECT COUNT(*) FROM branches WHERE task_id = $1 AND status = 'KILLED'",
        task_id,
    ) or 0

    # Fetch tribunal verdicts
    tribunal_rows = await db.fetch(
        "SELECT verdict, judge_plaintiff_score, judge_defendant_score "
        "FROM tribunal_sessions WHERE task_id = $1 ORDER BY created_at",
        task_id,
    )
    tribunal_verdicts = [
        {
            "verdict": r["verdict"],
            "plaintiff_score": r["judge_plaintiff_score"],
            "defendant_score": r["judge_defendant_score"],
        }
        for r in tribunal_rows
    ]

    # Check skeptic pass
    skeptic_row = await db.fetchrow(
        "SELECT passes_publishing_threshold FROM skeptic_results "
        "WHERE task_id = $1 ORDER BY created_at DESC LIMIT 1",
        task_id,
    )
    skeptic_pass = skeptic_row["passes_publishing_threshold"] if skeptic_row else None

    try:
        await db.execute(
            """
            INSERT INTO investigation_outcomes (
                id, task_id, user_id, topic, quality_tier,
                total_cost_usd, total_ai_calls, duration_seconds,
                final_state, report_generated,
                hypotheses_count, findings_count, killed_branches_count,
                tribunal_verdicts, skeptic_pass,
                created_at
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8,
                $9, $10,
                $11, $12, $13,
                $14::jsonb, $15,
                NOW()
            )
            ON CONFLICT (task_id) DO UPDATE SET
                total_cost_usd = EXCLUDED.total_cost_usd,
                total_ai_calls = EXCLUDED.total_ai_calls,
                duration_seconds = EXCLUDED.duration_seconds,
                final_state = EXCLUDED.final_state,
                report_generated = EXCLUDED.report_generated,
                hypotheses_count = EXCLUDED.hypotheses_count,
                findings_count = EXCLUDED.findings_count,
                killed_branches_count = EXCLUDED.killed_branches_count,
                tribunal_verdicts = EXCLUDED.tribunal_verdicts,
                skeptic_pass = EXCLUDED.skeptic_pass
            """,
            outcome_id,
            task_id,
            user_id,
            topic,
            quality_tier,
            total_cost_usd,
            total_ai_calls,
            duration_seconds,
            final_state,
            report_generated,
            hypotheses_count,
            findings_count,
            killed_branches_count,
            json.dumps(tribunal_verdicts),
            skeptic_pass,
        )
        logger.info(
            "investigation_outcome_recorded",
            outcome_id=outcome_id,
            task_id=task_id,
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "investigation_outcome_record_failed",
            task_id=task_id,
            error=str(exc),
        )
        return ""

    return outcome_id


# ===========================================================================
# User Feedback Ingestion
# ===========================================================================


async def record_feedback(
    user_id: str,
    task_id: str | None,
    event_type: str,
    category: str | None,
    content: dict[str, Any],
    db: Any,
) -> str:
    """Record a user feedback event.

    Parameters
    ----------
    user_id:
        UUID of the user submitting feedback.
    task_id:
        Optional UUID of the related investigation.
    event_type:
        One of: 'rating', 'feedback', 'correction', 'preference'.
    category:
        Optional category: 'report_quality', 'search_depth',
        'branch_decision', 'general'.
    content:
        Structured feedback payload (varies by event_type).
    db:
        asyncpg connection pool.

    Returns
    -------
    str
        UUID of the created learning event.
    """
    event_id = str(uuid.uuid4())

    try:
        await db.execute(
            """
            INSERT INTO learning_events (id, user_id, task_id, event_type, category, content)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            event_id,
            user_id,
            task_id,
            event_type,
            category,
            json.dumps(content),
        )

        # If this is a rating, also update the investigation_outcomes table
        if event_type == "rating" and task_id:
            rating = content.get("rating")
            feedback_text = content.get("feedback", "")
            if rating is not None:
                await db.execute(
                    """
                    UPDATE investigation_outcomes
                    SET user_rating = $1, user_feedback = $2
                    WHERE task_id = $3
                    """,
                    int(rating),
                    feedback_text,
                    task_id,
                )

        logger.info(
            "feedback_recorded",
            event_id=event_id,
            user_id=user_id,
            task_id=task_id,
            event_type=event_type,
        )

        # Trigger incremental pattern extraction after feedback
        await _extract_patterns_incremental(user_id, event_type, content, db)

    except Exception as exc:  # noqa: BLE001
        logger.error(
            "feedback_record_failed",
            user_id=user_id,
            error=str(exc),
        )
        return ""

    return event_id


# ===========================================================================
# Pattern Extraction
# ===========================================================================


async def extract_patterns(user_id: str, db: Any) -> int:
    """Run full pattern extraction for a user across all their investigations.

    Analyzes investigation_outcomes and learning_events to extract
    durable insights that improve future investigations.

    Parameters
    ----------
    user_id:
        UUID of the user.
    db:
        asyncpg connection pool.

    Returns
    -------
    int
        Number of insights created or updated.
    """
    insights_count = 0

    # 1. Preferred quality tier (most frequently used with high ratings)
    tier_rows = await db.fetch(
        """
        SELECT quality_tier, AVG(COALESCE(user_rating, 3)) as avg_rating,
               COUNT(*) as count
        FROM investigation_outcomes
        WHERE user_id = $1 AND quality_tier IS NOT NULL
        GROUP BY quality_tier
        ORDER BY avg_rating DESC, count DESC
        """,
        user_id,
    )
    if tier_rows:
        best_tier = tier_rows[0]
        await _upsert_insight(
            user_id=user_id,
            insight_type="quality_preference",
            insight_key="preferred_tier",
            insight_value={
                "tier": best_tier["quality_tier"],
                "avg_rating": float(best_tier["avg_rating"]),
                "sample_count": int(best_tier["count"]),
            },
            confidence=min(0.9, 0.3 + int(best_tier["count"]) * 0.1),
            sample_count=int(best_tier["count"]),
            db=db,
        )
        insights_count += 1

    # 2. Average investigation depth preference
    depth_row = await db.fetchrow(
        """
        SELECT AVG(total_ai_calls) as avg_calls,
               AVG(total_cost_usd) as avg_cost,
               AVG(findings_count) as avg_findings,
               COUNT(*) as total
        FROM investigation_outcomes
        WHERE user_id = $1 AND user_rating >= 4
        """,
        user_id,
    )
    if depth_row and depth_row["total"] and depth_row["total"] > 0:
        await _upsert_insight(
            user_id=user_id,
            insight_type="depth_preference",
            insight_key="satisfying_depth",
            insight_value={
                "avg_ai_calls": float(depth_row["avg_calls"] or 0),
                "avg_cost_usd": float(depth_row["avg_cost"] or 0),
                "avg_findings": float(depth_row["avg_findings"] or 0),
                "sample_count": int(depth_row["total"]),
            },
            confidence=min(0.9, 0.3 + int(depth_row["total"]) * 0.15),
            sample_count=int(depth_row["total"]),
            db=db,
        )
        insights_count += 1

    # 3. Branch kill sensitivity
    kill_corrections = await db.fetchval(
        """
        SELECT COUNT(*) FROM learning_events
        WHERE user_id = $1
          AND event_type = 'correction'
          AND category = 'branch_decision'
        """,
        user_id,
    )
    total_investigations = await db.fetchval(
        "SELECT COUNT(*) FROM investigation_outcomes WHERE user_id = $1",
        user_id,
    )
    if total_investigations and total_investigations > 0:
        kill_correction_rate = (kill_corrections or 0) / total_investigations
        await _upsert_insight(
            user_id=user_id,
            insight_type="branch_preference",
            insight_key="kill_sensitivity",
            insight_value={
                "correction_rate": kill_correction_rate,
                "prefers_less_killing": kill_correction_rate > 0.3,
                "total_corrections": int(kill_corrections or 0),
            },
            confidence=min(0.9, 0.3 + int(total_investigations) * 0.1),
            sample_count=int(total_investigations),
            db=db,
        )
        insights_count += 1

    # 4. Topic clustering (what topics get high ratings)
    topic_rows = await db.fetch(
        """
        SELECT topic, user_rating FROM investigation_outcomes
        WHERE user_id = $1 AND user_rating IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 50
        """,
        user_id,
    )
    if topic_rows:
        high_rated_topics = [r["topic"] for r in topic_rows if (r["user_rating"] or 0) >= 4]
        low_rated_topics = [r["topic"] for r in topic_rows if (r["user_rating"] or 0) <= 2]
        await _upsert_insight(
            user_id=user_id,
            insight_type="topic_preference",
            insight_key="rated_topics",
            insight_value={
                "high_rated": high_rated_topics[:10],
                "low_rated": low_rated_topics[:10],
                "total_rated": len(topic_rows),
            },
            confidence=min(0.9, 0.3 + len(topic_rows) * 0.05),
            sample_count=len(topic_rows),
            db=db,
        )
        insights_count += 1

    # 5. Report preference (does user prefer reports generated?)
    report_rows = await db.fetch(
        """
        SELECT report_generated, AVG(COALESCE(user_rating, 3)) as avg_rating
        FROM investigation_outcomes
        WHERE user_id = $1
        GROUP BY report_generated
        """,
        user_id,
    )
    if len(report_rows) >= 1:
        report_prefs = {r["report_generated"]: float(r["avg_rating"]) for r in report_rows}
        await _upsert_insight(
            user_id=user_id,
            insight_type="output_preference",
            insight_key="report_preference",
            insight_value={
                "with_report_avg_rating": report_prefs.get(True, 0),
                "without_report_avg_rating": report_prefs.get(False, 0),
                "prefers_reports": report_prefs.get(True, 0) > report_prefs.get(False, 0),
            },
            confidence=0.5,
            sample_count=sum(1 for _ in report_rows),
            db=db,
        )
        insights_count += 1

    # 6. User explicit preferences (from preference-type events)
    pref_rows = await db.fetch(
        """
        SELECT content FROM learning_events
        WHERE user_id = $1 AND event_type = 'preference'
        ORDER BY created_at DESC
        LIMIT 20
        """,
        user_id,
    )
    if pref_rows:
        all_prefs = []
        for r in pref_rows:
            raw = r["content"]
            if isinstance(raw, str):
                try:
                    all_prefs.append(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    pass
            elif isinstance(raw, dict):
                all_prefs.append(raw)
        if all_prefs:
            await _upsert_insight(
                user_id=user_id,
                insight_type="explicit_preference",
                insight_key="user_stated",
                insight_value={"preferences": all_prefs},
                confidence=0.95,  # high confidence — user explicitly stated
                sample_count=len(all_prefs),
                db=db,
            )
            insights_count += 1

    logger.info(
        "pattern_extraction_complete",
        user_id=user_id,
        insights_created=insights_count,
    )
    return insights_count


async def _extract_patterns_incremental(
    user_id: str,
    event_type: str,
    content: dict[str, Any],
    db: Any,
) -> None:
    """Incrementally update insights based on a single new feedback event.

    This is lighter than full extraction — only updates insights relevant
    to the specific feedback type.
    """
    try:
        if event_type == "rating":
            # Re-run quality preference extraction
            await extract_patterns(user_id, db)

        elif event_type == "preference":
            # Directly store as explicit preference
            pref_key = content.get("key", "general")
            await _upsert_insight(
                user_id=user_id,
                insight_type="explicit_preference",
                insight_key=pref_key,
                insight_value=content,
                confidence=0.95,
                sample_count=1,
                db=db,
            )

        elif event_type == "correction":
            category = content.get("category", "general")
            if category == "branch_decision":
                # Increment branch kill correction counter
                await extract_patterns(user_id, db)

    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "incremental_extraction_failed",
            user_id=user_id,
            error=str(exc),
        )


# ===========================================================================
# Learning Context Generation
# ===========================================================================


async def build_learning_context(user_id: str, db: Any) -> str:
    """Build a learning context string for prompt injection.

    Queries the user's learning insights and formats them into a
    concise instruction block that can be prepended to AI prompts.

    Parameters
    ----------
    user_id:
        UUID of the user.
    db:
        asyncpg connection pool.

    Returns
    -------
    str
        Formatted learning context string, or empty string if no
        insights are available.
    """
    rows = await db.fetch(
        """
        SELECT insight_type, insight_key, insight_value, confidence, sample_count
        FROM learning_insights
        WHERE user_id = $1 AND confidence >= 0.4
        ORDER BY confidence DESC
        LIMIT 20
        """,
        user_id,
    )

    if not rows:
        return ""

    parts: list[str] = []
    parts.append("=== LEARNING CONTEXT (from prior investigations) ===")

    for row in rows:
        itype = row["insight_type"]
        ikey = row["insight_key"]
        raw_val = row["insight_value"]
        confidence = row["confidence"]

        # Decode JSONB if needed
        if isinstance(raw_val, str):
            try:
                val = json.loads(raw_val)
            except (json.JSONDecodeError, TypeError):
                val = {"raw": raw_val}
        else:
            val = raw_val or {}

        if itype == "quality_preference":
            tier = val.get("tier", "balanced")
            parts.append(
                f"• Quality: User prefers '{tier}' tier "
                f"(avg rating {val.get('avg_rating', '?')}/5, "
                f"{val.get('sample_count', '?')} samples, "
                f"confidence {confidence:.0%})"
            )

        elif itype == "depth_preference":
            parts.append(
                f"• Depth: Satisfying investigations average "
                f"{val.get('avg_ai_calls', '?'):.0f} AI calls, "
                f"{val.get('avg_findings', '?'):.0f} findings, "
                f"${val.get('avg_cost_usd', '?'):.2f} cost "
                f"(confidence {confidence:.0%})"
            )

        elif itype == "branch_preference":
            if val.get("prefers_less_killing"):
                parts.append(
                    f"• Branches: User has corrected branch kills "
                    f"{val.get('total_corrections', '?')} times — "
                    f"prefer keeping branches alive longer "
                    f"(confidence {confidence:.0%})"
                )

        elif itype == "output_preference":
            if val.get("prefers_reports"):
                parts.append(
                    f"• Output: User prefers investigations that generate full reports "
                    f"(confidence {confidence:.0%})"
                )

        elif itype == "explicit_preference":
            prefs = val.get("preferences", [])
            if isinstance(prefs, list):
                for p in prefs[:5]:
                    if isinstance(p, dict):
                        desc = p.get("description", p.get("value", str(p)))
                        parts.append(f"• User preference: {desc}")
                    else:
                        parts.append(f"• User preference: {p}")
            elif isinstance(val, dict) and "description" in val:
                parts.append(f"• User preference: {val['description']}")

        elif itype == "topic_preference":
            high = val.get("high_rated", [])
            if high:
                parts.append(
                    f"• Topics: User rates these highly: {', '.join(str(t)[:50] for t in high[:3])}"
                )

    if len(parts) <= 1:
        return ""

    parts.append("=== END LEARNING CONTEXT ===")
    return "\n".join(parts)


async def get_user_insights(user_id: str, db: Any) -> list[dict[str, Any]]:
    """Fetch all learning insights for a user.

    Parameters
    ----------
    user_id:
        UUID of the user.
    db:
        asyncpg connection pool.

    Returns
    -------
    list[dict]
        List of insight dicts with type, key, value, confidence.
    """
    rows = await db.fetch(
        """
        SELECT id, insight_type, insight_key, insight_value,
               confidence, sample_count, last_updated
        FROM learning_insights
        WHERE user_id = $1
        ORDER BY confidence DESC, last_updated DESC
        """,
        user_id,
    )

    results = []
    for row in rows:
        raw_val = row["insight_value"]
        if isinstance(raw_val, str):
            try:
                val = json.loads(raw_val)
            except (json.JSONDecodeError, TypeError):
                val = {"raw": raw_val}
        else:
            val = raw_val or {}

        results.append({
            "id": row["id"],
            "insight_type": row["insight_type"],
            "insight_key": row["insight_key"],
            "insight_value": val,
            "confidence": row["confidence"],
            "sample_count": row["sample_count"],
            "last_updated": row["last_updated"].isoformat() if row["last_updated"] else None,
        })

    return results


async def get_investigation_feedback(task_id: str, db: Any) -> list[dict[str, Any]]:
    """Fetch all feedback events for a specific investigation.

    Parameters
    ----------
    task_id:
        UUID of the investigation.
    db:
        asyncpg connection pool.

    Returns
    -------
    list[dict]
        List of feedback event dicts.
    """
    rows = await db.fetch(
        """
        SELECT id, user_id, event_type, category, content, created_at
        FROM learning_events
        WHERE task_id = $1
        ORDER BY created_at DESC
        """,
        task_id,
    )

    results = []
    for row in rows:
        raw_content = row["content"]
        if isinstance(raw_content, str):
            try:
                content = json.loads(raw_content)
            except (json.JSONDecodeError, TypeError):
                content = {"raw": raw_content}
        else:
            content = raw_content or {}

        results.append({
            "id": row["id"],
            "user_id": row["user_id"],
            "event_type": row["event_type"],
            "category": row["category"],
            "content": content,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        })

    return results


# ===========================================================================
# Internal helpers
# ===========================================================================


async def _upsert_insight(
    user_id: str,
    insight_type: str,
    insight_key: str,
    insight_value: dict[str, Any],
    confidence: float,
    sample_count: int,
    db: Any,
) -> None:
    """Insert or update a learning insight."""
    await db.execute(
        """
        INSERT INTO learning_insights (
            id, user_id, insight_type, insight_key, insight_value,
            confidence, sample_count, last_updated
        ) VALUES (
            $1, $2, $3, $4, $5::jsonb, $6, $7, NOW()
        )
        ON CONFLICT (user_id, insight_type, insight_key) DO UPDATE SET
            insight_value = EXCLUDED.insight_value,
            confidence = EXCLUDED.confidence,
            sample_count = EXCLUDED.sample_count,
            last_updated = NOW()
        """,
        str(uuid.uuid4()),
        user_id,
        insight_type,
        insight_key,
        json.dumps(insight_value),
        confidence,
        sample_count,
    )
