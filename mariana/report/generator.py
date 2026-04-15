"""
mariana/report/generator.py

Two-pass AI report generation:

    Pass 1 — REPORT_DRAFT (Sonnet)
        Receives all confirmed findings and sources.  Produces a structured
        ``ReportDraftOutput`` with bilingual sections (EN + ZH), executive
        summary, conclusion, and disclaimer.

    Pass 2 — REPORT_FINAL_EDIT (Opus)
        Receives the draft output and polishes it: tightens prose, fixes
        inconsistencies, strengthens the narrative arc, ensures bilingual
        content is substantively equivalent.

After both AI passes, ``report_data`` is handed to ``renderer.render_pdf``
which produces the final PDF via Jinja2 + WeasyPrint.

For the prototype DOCX generation is skipped; the function returns
``(pdf_path, None)``.

``spawn_model`` context keys (from ``prompt_builder``):
    REPORT_DRAFT     : confirmed_findings, all_sources, task_topic,
                       [optional] failed_hypotheses
    REPORT_FINAL_EDIT: draft, all_sources
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from mariana.data.models import (
    Finding,
    ReportDraftOutput,
    ResearchTask,
    Source,
    TaskType,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------


def _build_findings_block(confirmed_findings: list[Finding]) -> str:
    """Produce a numbered block of findings for injection into REPORT_DRAFT context."""
    if not confirmed_findings:
        return "(No confirmed findings provided.)"
    lines: list[str] = [f"Total confirmed findings: {len(confirmed_findings)}", ""]
    for idx, finding in enumerate(confirmed_findings, start=1):
        lang_note = f" [{finding.content_language}]" if finding.content_language != "en" else ""
        lines += [
            f"[{idx}] ID={finding.id}{lang_note}  confidence={finding.confidence:.2f}"
            f"  type={finding.evidence_type.value}",
            finding.content[:1200] + ("…" if len(finding.content) > 1200 else ""),
        ]
        if finding.content_en and finding.content_language != "en":
            lines += [
                f"   EN: {finding.content_en[:600]}"
                f"{'…' if len(finding.content_en) > 600 else ''}",
            ]
        lines.append("")
    return "\n".join(lines)


def _build_sources_block(sources: list[Source]) -> str:
    """Produce a numbered block of sources with URLs for citation."""
    if not sources:
        return "(No sources provided.)"
    lines: list[str] = [f"Total sources: {len(sources)}", ""]
    for idx, src in enumerate(sources, start=1):
        title = src.title_en or src.title or "(untitled)"
        lines.append(
            f"[{idx}] {title}\n"
            f"    URL  : {src.url}\n"
            f"    Type : {src.source_type.value}\n"
            f"    Date : {src.fetched_at.date()}\n"
        )
    return "\n".join(lines)


def _build_failed_hypotheses_block(failed_hypotheses: list[str]) -> str:
    """List killed/exhausted hypotheses to inform the draft without reporting them."""
    if not failed_hypotheses:
        return "(None)"
    return "\n".join(f"  • {h}" for h in failed_hypotheses)


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------


async def generate_report(
    task: ResearchTask,
    confirmed_findings: list[Finding],
    all_sources: list[Source],
    failed_hypotheses: list[str],
    db: Any,  # asyncpg.Pool
    cost_tracker: Any,  # mariana.orchestrator.cost_tracker.CostTracker
    report_dir: str,
) -> tuple[str, str | None]:  # TODO: implement DOCX generation
    """
    Generate the full bilingual PDF research report.

    Parameters
    ----------
    task:
        The parent ResearchTask (topic, id, budget).
    confirmed_findings:
        Findings that survived tribunal and skeptic review.
    all_sources:
        Every source collected during the investigation.
    failed_hypotheses:
        Human-readable labels for killed or exhausted hypotheses.
    db:
        Live asyncpg pool (used to persist the report path).
    cost_tracker:
        Live CostTracker instance.
    report_dir:
        Absolute filesystem path where the PDF should be written.
        Created if it does not exist.

    Returns
    -------
    (pdf_path, None)
        ``pdf_path`` is the absolute path of the written PDF.
        The second element is always ``None`` (DOCX skipped in prototype).
    """
    from mariana.ai.session import spawn_model  # noqa: PLC0415
    from mariana.report.renderer import render_pdf  # noqa: PLC0415

    report_id = str(uuid.uuid4())
    log = logger.bind(report_id=report_id, task_id=task.id)
    log.info(
        "report_generation_start",
        confirmed_findings=len(confirmed_findings),
        sources=len(all_sources),
        failed_hypotheses=len(failed_hypotheses),
    )

    out_dir = Path(report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    findings_block = _build_findings_block(confirmed_findings)
    sources_block = _build_sources_block(all_sources)
    failed_block = _build_failed_hypotheses_block(failed_hypotheses)

    # ── Pass 1: REPORT_DRAFT (Sonnet) ─────────────────────────────────────────
    log.info("report_pass", pass_num=1, task_type=TaskType.REPORT_DRAFT.value)
    t0 = time.monotonic()

    draft_parsed, draft_session = await spawn_model(
        task_type=TaskType.REPORT_DRAFT,
        context={
            "task_id": task.id,           # BUG-A03 fix: include task_id for AISession cost attribution
            "confirmed_findings": findings_block,
            "all_sources": sources_block,
            "task_topic": task.topic,
            "failed_hypotheses": failed_block,
        },
        output_schema=ReportDraftOutput,
        db=db,
        cost_tracker=cost_tracker,
    )
    draft_output: ReportDraftOutput = draft_parsed

    log.info(
        "report_draft_done",
        sections=len(draft_output.sections),
        cost_usd=draft_session.cost_usd,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )

    # ── Pass 2: REPORT_FINAL_EDIT (Opus) ─────────────────────────────────────
    log.info("report_pass", pass_num=2, task_type=TaskType.REPORT_FINAL_EDIT.value)
    t0 = time.monotonic()

    final_parsed, edit_session = await spawn_model(
        task_type=TaskType.REPORT_FINAL_EDIT,
        context={
            "task_id": task.id,           # BUG-A03 fix: include task_id for AISession cost attribution
            "draft": draft_output.model_dump_json(indent=2),
            "all_sources": sources_block,
        },
        output_schema=ReportDraftOutput,
        db=db,
        cost_tracker=cost_tracker,
    )
    final_output: ReportDraftOutput = final_parsed

    log.info(
        "report_edit_done",
        cost_usd=edit_session.cost_usd,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )

    total_ai_cost = draft_session.cost_usd + edit_session.cost_usd

    # ── Compute word counts ───────────────────────────────────────────────────
    for section in final_output.sections:
        section.word_count_en = len((section.content_en or "").split())

    # ── Build template data dict ──────────────────────────────────────────────
    report_data: dict[str, Any] = {
        "title_en": final_output.title_en,
        "title_zh": final_output.title_zh,
        "executive_summary_en": final_output.executive_summary_en,
        "executive_summary_zh": final_output.executive_summary_zh,
        "sections": [
            {
                "section_id": s.section_id,
                "title_en": s.title_en,
                "title_zh": s.title_zh,
                "content_en": s.content_en,
                "content_zh": s.content_zh,
                "citations": s.citations,
                "word_count_en": s.word_count_en,
            }
            for s in final_output.sections
        ],
        "conclusion_en": final_output.conclusion_en,
        "conclusion_zh": final_output.conclusion_zh,
        "disclaimer_en": final_output.disclaimer_en,
        "disclaimer_zh": final_output.disclaimer_zh,
        "generated_at": datetime.now(timezone.utc),
        "task_topic": task.topic,
        # BUG-019 fix: guard against cost_tracker being None before accessing
        # .total_spent — the parameter is typed Any and may be None.
        "total_cost_usd": cost_tracker.total_spent if cost_tracker is not None else 0.0,
        "total_sources": len(all_sources),
        "total_findings": len(confirmed_findings),
    }

    # ── Render to PDF ─────────────────────────────────────────────────────────
    template_dir = str(Path(__file__).parent / "templates")
    safe_topic = "".join(
        c if c.isalnum() or c in "-_" else "_" for c in task.topic[:60]
    )
    pdf_filename = f"mariana_{safe_topic}_{report_id[:8]}.pdf"
    pdf_path = str(out_dir / pdf_filename)

    log.info("report_render_start", pdf_path=pdf_path)
    t0 = time.monotonic()

    import asyncio as _asyncio  # noqa: PLC0415
    try:
        # BUG-004 fix: use get_running_loop() instead of deprecated get_event_loop().
        await _asyncio.get_running_loop().run_in_executor(
            None, render_pdf, report_data, template_dir, pdf_path
        )
    except Exception as exc:
        log.error("report_render_failed", pdf_path=pdf_path, error=str(exc))
        # BUG-023 fix: wrap the DB status update in its own try/except so that
        # a DB failure does not replace the original render exception.
        try:
            async with db.acquire() as _conn:
                await _conn.execute(
                    "UPDATE research_tasks SET status = 'FAILED' WHERE id = $1",
                    task.id,
                )
        except Exception as db_exc:
            log.error(
                "report_render_db_update_failed",
                pdf_path=pdf_path,
                db_error=str(db_exc),
            )
        raise

    log.info(
        "report_render_done",
        pdf_path=pdf_path,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        size_bytes=os.path.getsize(pdf_path),
    )

    # ── Persist PDF path to database ──────────────────────────────────────────
    await _persist_report_path(db, task.id, pdf_path, total_ai_cost)

    log.info(
        "report_generation_complete",
        pdf_path=pdf_path,
        report_cost_usd=total_ai_cost,
    )

    logger.warning("docx_generation_skipped_prototype")
    return pdf_path, None


# ---------------------------------------------------------------------------
# Database persistence
# ---------------------------------------------------------------------------


async def _persist_report_path(
    db: Any,
    task_id: str,
    pdf_path: str,
    report_cost_usd: float,
) -> None:
    """
    Write the PDF output path back to the parent research_tasks row and
    record an audit entry in report_generations.
    """
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE research_tasks
                   SET output_pdf_path = $1,
                       status          = 'COMPLETED',
                       completed_at    = NOW()
                 WHERE id = $2
                   AND status != 'HALTED'
                """,
                pdf_path,
                task_id,
            )

            # BUG-S2-01 fix: report_generations.task_id has no UNIQUE
            # constraint (only an index), so ON CONFLICT (task_id) would
            # raise a PostgreSQL error.  Remove the conflict clause — multiple
            # report generations per task are valid (e.g. retries).  The
            # primary key is an auto-generated UUID, so there is no conflict.
            await conn.execute(
                """
                INSERT INTO report_generations (
                    task_id, pdf_path, report_cost_usd, generated_at
                ) VALUES ($1, $2, $3, NOW())
                """,
                task_id,
                pdf_path,
                report_cost_usd,
            )
