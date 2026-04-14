"""
mariana/tribunal/adversarial.py

5-session adversarial tribunal that stress-tests high-confidence findings.

The tribunal runs five sequential Opus sessions:

    Session 1 — TRIBUNAL_PLAINTIFF  : Builds the strongest case FOR the finding.
    Session 2 — TRIBUNAL_DEFENDANT  : Attacks every weakness.
    Session 3 — TRIBUNAL_REBUTTAL   : Plaintiff responds to defendant's best points.
    Session 4 — TRIBUNAL_COUNTER    : Defendant delivers final counter.
    Session 5 — TRIBUNAL_JUDGE      : Independent evaluation using summaries only.

Each session is a fresh ``spawn_model`` call so the judge truly receives only
distilled summaries — it cannot inherit the full argument text from a shared
conversation and be anchored by rhetoric.

Cost from all five sessions is accumulated and recorded in the returned
``TribunalSession``, which is also persisted to the database.

``spawn_model`` signature (from ``mariana.ai.session``):
    async def spawn_model(
        task_type, context: dict, output_schema,
        max_tokens=4096, use_batch=False, branch_id=None,
        db=None, cost_tracker=None, config=None
    ) -> tuple[BaseModel, AISession]

Context keys required per task_type (from ``prompt_builder``):
    TRIBUNAL_PLAINTIFF : finding_summary, supporting_evidence, sources
    TRIBUNAL_DEFENDANT : finding_summary, plaintiff_argument
    TRIBUNAL_REBUTTAL  : finding_summary, defendant_argument, plaintiff_original
    TRIBUNAL_COUNTER   : finding_summary, plaintiff_rebuttal, defendant_original
    TRIBUNAL_JUDGE     : finding_summary, plaintiff_summary, defendant_summary,
                         plaintiff_rebuttal_summary, defendant_counter_summary
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import structlog

from mariana.data.models import (
    Finding,
    Source,
    TaskType,
    TribunalArgumentOutput,
    TribunalSession,
    TribunalVerdictOutput,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Internal summary builders
# ---------------------------------------------------------------------------


def _build_finding_summary(
    finding: Finding,
    supporting_findings: list[Finding],
) -> str:
    """
    Produce a compact structured summary of the primary finding and its
    supporting evidence.  Injected into every tribunal context dict so all
    five sessions share identical framing.
    """
    lines: list[str] = [
        "=== FINDING UNDER REVIEW ===",
        f"ID          : {finding.id}",
        f"Confidence  : {finding.confidence:.2f}",
        f"Language    : {finding.content_language}",
        f"Evidence    : {finding.evidence_type.value}",
        "",
        "Content:",
        finding.content,
    ]

    if finding.content_en and finding.content_language != "en":
        lines += ["", "English translation:", finding.content_en]

    if supporting_findings:
        lines += [
            "",
            f"=== SUPPORTING FINDINGS ({len(supporting_findings)}) ===",
        ]
        for idx, sf in enumerate(supporting_findings, start=1):
            lines += [
                f"[{idx}] ID={sf.id}  confidence={sf.confidence:.2f}  type={sf.evidence_type.value}",
                sf.content[:800] + ("…" if len(sf.content) > 800 else ""),
            ]

    return "\n".join(lines)


def _build_source_summary(sources: list[Source]) -> str:
    """Return a numbered list of sources."""
    if not sources:
        return "(no sources provided)"
    lines: list[str] = []
    for idx, src in enumerate(sources, start=1):
        title = src.title_en or src.title or src.url
        lines.append(
            f"[{idx}] {title} — {src.url} ({src.source_type.value},"
            f" fetched {src.fetched_at.date()})"
        )
    return "\n".join(lines)


def _summarise_argument(arg: TribunalArgumentOutput, label: str) -> str:
    """
    Produce a bullet-point summary of one argument for the judge.

    The judge sees ONLY these summaries — not the full argument text — to
    prevent anchoring on verbose rhetoric.
    """
    key_points_text = "\n".join(f"  • {pt}" for pt in arg.key_points)
    weaknesses_text = (
        "\n".join(f"  • {w}" for w in arg.weaknesses_acknowledged)
        if arg.weaknesses_acknowledged
        else "  (none acknowledged)"
    )
    rebuttal_text = (
        arg.strongest_counterargument_rebuttal
        or "(no explicit rebuttal provided)"
    )
    return (
        f"=== {label} (confidence={arg.confidence:.2f}) ===\n"
        f"Summary: {arg.argument_summary}\n"
        f"Key points:\n{key_points_text}\n"
        f"Weaknesses acknowledged:\n{weaknesses_text}\n"
        f"Rebuttal to strongest opposition: {rebuttal_text}"
    )


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------


async def run_tribunal(
    finding: Finding,
    supporting_findings: list[Finding],
    sources: list[Source],
    task_id: str,
    db: Any,  # asyncpg.Pool
    cost_tracker: Any,  # mariana.orchestrator.cost_tracker.CostTracker
) -> TribunalSession:
    """
    Run the full 5-session adversarial tribunal for *finding*.

    Parameters
    ----------
    finding:
        The primary finding being stress-tested.
    supporting_findings:
        Other findings from the same task that corroborate the primary finding.
    sources:
        Source records whose URLs/titles are cited in the prompts.
    task_id:
        Parent ResearchTask UUID.
    db:
        Live asyncpg connection pool.
    cost_tracker:
        Live CostTracker instance.

    Returns
    -------
    TribunalSession
        Fully populated, already persisted to the database.

    Raises
    ------
    RuntimeError
        If any session fails to parse its output and the retry budget is
        exhausted (re-raised from ``spawn_model``).
    """
    # Lazy import to break potential circular imports at package init time.
    from mariana.ai.session import spawn_model  # noqa: PLC0415

    session_id = str(uuid.uuid4())
    total_cost: float = 0.0

    finding_summary = _build_finding_summary(finding, supporting_findings)
    source_summary = _build_source_summary(sources)

    log = logger.bind(
        tribunal_id=session_id,
        finding_id=finding.id,
        task_id=task_id,
    )
    log.info(
        "tribunal_start",
        supporting_count=len(supporting_findings),
        source_count=len(sources),
    )

    # ── Session 1: PLAINTIFF ─────────────────────────────────────────────────
    log.info("tribunal_session", role="PLAINTIFF")
    t0 = time.monotonic()

    # Build a distinct supporting_evidence block from individual supporting findings.
    if supporting_findings:
        supporting_evidence_block = "\n".join(
            f"[{i}] ID={sf.id}  confidence={sf.confidence:.2f}\n"
            f"{sf.content[:600]}{'\u2026' if len(sf.content) > 600 else ''}"
            for i, sf in enumerate(supporting_findings, start=1)
        )
    else:
        supporting_evidence_block = "(no separate supporting findings)"

    plaintiff_parsed, plaintiff_session = await spawn_model(
        task_type=TaskType.TRIBUNAL_PLAINTIFF,
        context={
            "finding_summary": finding_summary,
            "supporting_evidence": supporting_evidence_block,
            "sources": source_summary,
        },
        output_schema=TribunalArgumentOutput,
        db=db,
        cost_tracker=cost_tracker,
    )
    plaintiff_arg: TribunalArgumentOutput = plaintiff_parsed
    total_cost += plaintiff_session.cost_usd
    log.info(
        "tribunal_session_done",
        role="PLAINTIFF",
        cost_usd=plaintiff_session.cost_usd,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )

    # ── Session 2: DEFENDANT ─────────────────────────────────────────────────
    log.info("tribunal_session", role="DEFENDANT")
    t0 = time.monotonic()

    plaintiff_arg_text = (
        f"{plaintiff_arg.argument_summary}\n\n"
        "Key points:\n"
        + "\n".join(f"• {pt}" for pt in plaintiff_arg.key_points)
    )

    defendant_parsed, defendant_session = await spawn_model(
        task_type=TaskType.TRIBUNAL_DEFENDANT,
        context={
            "finding_summary": finding_summary,
            "plaintiff_argument": plaintiff_arg_text,
        },
        output_schema=TribunalArgumentOutput,
        db=db,
        cost_tracker=cost_tracker,
    )
    defendant_arg: TribunalArgumentOutput = defendant_parsed
    total_cost += defendant_session.cost_usd
    log.info(
        "tribunal_session_done",
        role="DEFENDANT",
        cost_usd=defendant_session.cost_usd,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )

    # ── Session 3: PLAINTIFF REBUTTAL ────────────────────────────────────────
    log.info("tribunal_session", role="PLAINTIFF_REBUTTAL")
    t0 = time.monotonic()

    defendant_arg_text = (
        f"{defendant_arg.argument_summary}\n\n"
        "Key attacks:\n"
        + "\n".join(f"• {pt}" for pt in defendant_arg.key_points)
    )

    rebuttal_parsed, rebuttal_session = await spawn_model(
        task_type=TaskType.TRIBUNAL_REBUTTAL,
        context={
            "finding_summary": finding_summary,
            "defendant_argument": defendant_arg_text,
            "plaintiff_original": plaintiff_arg_text,
        },
        output_schema=TribunalArgumentOutput,
        db=db,
        cost_tracker=cost_tracker,
    )
    rebuttal_arg: TribunalArgumentOutput = rebuttal_parsed
    total_cost += rebuttal_session.cost_usd
    log.info(
        "tribunal_session_done",
        role="PLAINTIFF_REBUTTAL",
        cost_usd=rebuttal_session.cost_usd,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )

    # ── Session 4: DEFENDANT COUNTER ─────────────────────────────────────────
    log.info("tribunal_session", role="DEFENDANT_COUNTER")
    t0 = time.monotonic()

    rebuttal_arg_text = (
        f"{rebuttal_arg.argument_summary}\n\n"
        "Rebuttal key points:\n"
        + "\n".join(f"• {pt}" for pt in rebuttal_arg.key_points)
    )

    counter_parsed, counter_session = await spawn_model(
        task_type=TaskType.TRIBUNAL_COUNTER,
        context={
            "finding_summary": finding_summary,
            "plaintiff_rebuttal": rebuttal_arg_text,
            "defendant_original": defendant_arg_text,
        },
        output_schema=TribunalArgumentOutput,
        db=db,
        cost_tracker=cost_tracker,
    )
    counter_arg: TribunalArgumentOutput = counter_parsed
    total_cost += counter_session.cost_usd
    log.info(
        "tribunal_session_done",
        role="DEFENDANT_COUNTER",
        cost_usd=counter_session.cost_usd,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )

    # ── Session 5: JUDGE ─────────────────────────────────────────────────────
    log.info("tribunal_session", role="JUDGE")
    t0 = time.monotonic()

    # The judge sees ONLY bullet-point summaries to prevent anchoring.
    plaintiff_summary = _summarise_argument(plaintiff_arg, "PLAINTIFF OPENING")
    defendant_summary = _summarise_argument(defendant_arg, "DEFENDANT OPENING")
    rebuttal_summary = _summarise_argument(rebuttal_arg, "PLAINTIFF REBUTTAL")
    counter_summary = _summarise_argument(counter_arg, "DEFENDANT COUNTER")

    verdict_parsed, judge_session = await spawn_model(
        task_type=TaskType.TRIBUNAL_JUDGE,
        context={
            "finding_summary": finding_summary,
            "plaintiff_summary": plaintiff_summary,
            "defendant_summary": defendant_summary,
            "plaintiff_rebuttal_summary": rebuttal_summary,
            "defendant_counter_summary": counter_summary,
        },
        output_schema=TribunalVerdictOutput,
        db=db,
        cost_tracker=cost_tracker,
    )
    verdict_output: TribunalVerdictOutput = verdict_parsed
    total_cost += judge_session.cost_usd
    log.info(
        "tribunal_session_done",
        role="JUDGE",
        verdict=verdict_output.verdict.value,
        plaintiff_score=verdict_output.plaintiff_score,
        defendant_score=verdict_output.defendant_score,
        cost_usd=judge_session.cost_usd,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )

    # ── Build TribunalSession record ─────────────────────────────────────────
    tribunal_session = TribunalSession(
        id=session_id,
        task_id=task_id,
        finding_id=finding.id,
        plaintiff_args=plaintiff_arg.argument_summary,
        defendant_args=defendant_arg.argument_summary,
        plaintiff_rebuttal=rebuttal_arg.argument_summary,
        defendant_counter=counter_arg.argument_summary,
        verdict=verdict_output.verdict,
        judge_plaintiff_score=verdict_output.plaintiff_score,
        judge_defendant_score=verdict_output.defendant_score,
        judge_reasoning=verdict_output.verdict_reasoning,
        unanswered_questions=verdict_output.unanswered_questions,
        total_cost_usd=total_cost,
    )

    # ── Persist to database ──────────────────────────────────────────────────
    # BUG-025 fix: swallow DB persistence errors like ai/session.py does in
    # _persist_session — a DB outage should not abort the tribunal result.
    try:
        await _persist_tribunal_session(db, tribunal_session, verdict_output)
    except Exception as exc:
        log.error(
            "tribunal_persist_failed",
            tribunal_id=session_id,
            error=str(exc),
            msg="DB persistence failed but tribunal result is still returned",
        )

    log.info(
        "tribunal_complete",
        verdict=tribunal_session.verdict.value if tribunal_session.verdict else "NONE",
        total_cost_usd=total_cost,
        confidence_after=verdict_output.finding_confidence_after_tribunal,
    )

    return tribunal_session


# ---------------------------------------------------------------------------
# Database persistence
# ---------------------------------------------------------------------------


async def _persist_tribunal_session(
    db: Any,
    session: TribunalSession,
    verdict_output: TribunalVerdictOutput,
) -> None:
    """
    Upsert the TribunalSession and update the parent finding's confidence.

    Both writes happen inside a single transaction so they succeed or fail
    together.
    """
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO tribunal_sessions (
                    id, task_id, finding_id,
                    plaintiff_args, defendant_args,
                    plaintiff_rebuttal, defendant_counter,
                    verdict,
                    judge_plaintiff_score, judge_defendant_score,
                    judge_reasoning, unanswered_questions,
                    total_cost_usd, created_at
                ) VALUES (
                    $1, $2, $3,
                    $4, $5,
                    $6, $7,
                    $8,
                    $9, $10,
                    $11, $12,
                    $13, NOW()
                )
                ON CONFLICT (id) DO NOTHING
                """,
                session.id,
                session.task_id,
                session.finding_id,
                session.plaintiff_args,
                session.defendant_args,
                session.plaintiff_rebuttal,
                session.defendant_counter,
                session.verdict.value if session.verdict else None,
                session.judge_plaintiff_score,
                session.judge_defendant_score,
                session.judge_reasoning,
                json.dumps(session.unanswered_questions, default=str),
                session.total_cost_usd,
            )

            # Update the finding's confidence based on the tribunal outcome.
            await conn.execute(
                """
                UPDATE findings
                   SET confidence = $1,
                       metadata   = metadata || $2::jsonb
                 WHERE id = $3
                """,
                verdict_output.finding_confidence_after_tribunal,
                json.dumps(
                    {
                        "tribunal_session_id": session.id,
                        "tribunal_verdict": (
                            session.verdict.value if session.verdict else None
                        ),
                        "publication_risk": verdict_output.publication_risk_assessment,
                    }
                ),
                session.finding_id,
            )
