"""
mariana/tribunal/skeptic.py

20-question skeptic gauntlet — the final quality gate before publication.

The skeptic operates in two layers:

    Layer 0 (AI)   — Opus spawns ``SkepticQuestionsOutput`` containing up to 20
                     hard questions categorised by severity and thematic type.

    Layer 1 (pure Python, deterministic) — ``classify_questions()`` checks each
                     question against the existing corpus of confirmed findings
                     to determine whether it is RESOLVED, RESEARCHABLE, or OPEN.
                     No AI is involved in classification; it is pure keyword
                     matching over finding content.

A SkepticResult is inserted into the database and returned.  The
``passes_publishing_threshold`` flag (computed by the SkepticResult Pydantic
model validator) is the authoritative go/no-go signal for the report phase.

``spawn_model`` context keys for SKEPTIC_QUESTIONS (from ``prompt_builder``):
    finding_summary, confidence_score, tribunal_verdict
    [optional] unanswered_questions
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import structlog

from mariana.data.models import (
    Finding,
    QuestionClassification,
    QuestionSeverity,
    SkepticQuestion,
    SkepticQuestionsOutput,
    SkepticResult,
    TaskType,
    TribunalSession,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Stopword list for keyword extraction
# ---------------------------------------------------------------------------

_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "that", "this", "these",
        "those", "it", "its", "not", "no", "as", "if", "from", "into",
        "about", "than", "more", "also", "their", "there", "which", "who",
        "whom", "what", "when", "where", "how", "such", "any", "all", "both",
        "i", "we", "you", "he", "she", "they", "me", "him", "her", "us",
        "them", "my", "our", "your", "his", "very", "just", "so",
        "each", "every", "other", "some", "only", "then", "than",
        "must", "yet", "still", "however", "therefore",
    }
)

# Minimum keyword overlap between a question and a finding to count as resolved.
# BUG-036 fix: lowered from 5 to 3 so that short-but-legitimate questions
# (e.g. "Is cash pledged?" — 3 keywords after stopword removal) are not
# unconditionally blocked from being classified as RESOLVED when findings
# clearly address them.  3 is still high enough to avoid trivial false positives.
_MIN_KEYWORD_MATCH: int = 3


# ---------------------------------------------------------------------------
# Layer 1: pure-Python keyword classification
# ---------------------------------------------------------------------------


def _extract_keywords(text: str) -> list[str]:
    """
    Extract meaningful keywords from *text* by:
    1. Lowercasing and stripping punctuation.
    2. Splitting on whitespace.
    3. Removing stopwords and tokens shorter than 3 characters.
    4. Deduplicating while preserving insertion order.

    Returns a list of unique keyword strings.
    """
    # Replace underscores (treated as word chars by \w) and other punctuation with spaces
    # so that identifiers like debt_to_equity are split into separate keywords.
    cleaned = re.sub(r"[^\w\s]|_", " ", text.lower())
    tokens = cleaned.split()
    seen: set[str] = set()
    keywords: list[str] = []
    for token in tokens:
        if len(token) >= 3 and token not in _STOPWORDS and token not in seen:
            seen.add(token)
            keywords.append(token)
    return keywords


def _question_resolved_by_findings(
    question: SkepticQuestion,
    existing_findings: list[Finding],
) -> tuple[bool, str | None]:
    """
    Check whether *question* is resolved by *existing_findings*.

    A question is considered resolved when at least one finding's content
    (or its English translation) contains ``_MIN_KEYWORD_MATCH`` or more of
    the question's extracted keywords.

    Returns
    -------
    (is_resolved, resolution_note)
        ``is_resolved`` is True if a match was found.
        ``resolution_note`` is a human-readable description of which finding
        resolved the question (or None if not resolved).
    """
    question_keywords = _extract_keywords(question.question)
    if len(question_keywords) < _MIN_KEYWORD_MATCH:
        # Too few distinctive keywords — cannot reliably classify as resolved.
        return False, None

    kw_set = set(question_keywords)

    for finding in existing_findings:
        texts_to_search: list[str] = [finding.content]
        if finding.content_en:
            texts_to_search.append(finding.content_en)

        for text in texts_to_search:
            finding_keywords = set(_extract_keywords(text))
            overlap = kw_set & finding_keywords
            if len(overlap) >= _MIN_KEYWORD_MATCH:
                matched_words = ", ".join(sorted(overlap)[:5])
                return (
                    True,
                    f"Resolved by finding {finding.id[:8]} "
                    f"(matched keywords: {matched_words})",
                )

    return False, None


def classify_questions(
    questions: list[SkepticQuestion],
    existing_findings: list[Finding],
) -> list[SkepticQuestion]:
    """
    Classify each question as RESOLVED, RESEARCHABLE, or OPEN.

    Classification rules applied in priority order:

    1. If the question's keywords overlap with any existing finding
       by at least ``_MIN_KEYWORD_MATCH`` tokens: → RESOLVED
    2. If severity is CRITICAL and the question is not resolved: → OPEN
    3. All other unresolved questions: → RESEARCHABLE

    This function is deterministic, synchronous, and contains NO AI calls.
    It runs in O(Q × F × K) where Q = questions, F = findings,
    K = average keyword count.

    Parameters
    ----------
    questions:
        Questions produced by the AI skeptic agent.
    existing_findings:
        All confirmed/active findings for this task.

    Returns
    -------
    list[SkepticQuestion]
        New list with updated ``classification`` and ``resolution_note`` fields.
    """
    classified: list[SkepticQuestion] = []

    for question in questions:
        is_resolved, resolution_note = _question_resolved_by_findings(
            question, existing_findings
        )

        if is_resolved:
            classification = QuestionClassification.RESOLVED
        elif question.severity == QuestionSeverity.CRITICAL:
            classification = QuestionClassification.OPEN
            resolution_note = None
        else:
            classification = QuestionClassification.RESEARCHABLE
            resolution_note = None

        # Pydantic models are immutable — use model_copy to create updated instance.
        classified.append(
            question.model_copy(
                update={
                    "classification": classification,
                    "resolution_note": resolution_note,
                }
            )
        )

    return classified


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------


async def run_skeptic(
    finding: Finding,
    tribunal_session: TribunalSession,
    existing_findings: list[Finding],
    task_id: str,
    db: Any,  # asyncpg.Pool
    cost_tracker: Any,  # mariana.orchestrator.cost_tracker.CostTracker
) -> SkepticResult:
    """
    Run the two-layer skeptic gauntlet against *finding*.

    Layer 0 (AI): Spawns an Opus model to generate 20 hard questions using
    the ``SKEPTIC_QUESTIONS`` task type.

    Layer 1 (pure Python): Classifies each question via ``classify_questions``
    without any AI calls.

    Parameters
    ----------
    finding:
        The finding to scrutinise.
    tribunal_session:
        The tribunal session that reviewed this finding; its verdict and
        reasoning are included in the skeptic context.
    existing_findings:
        All findings available for the current task (used for Layer 1 lookup).
        Excludes the finding under review.
    task_id:
        Parent ResearchTask UUID.
    db:
        Live asyncpg connection pool.
    cost_tracker:
        Live CostTracker instance.

    Returns
    -------
    SkepticResult
        Fully classified skeptic result, persisted to the database.
    """
    from mariana.ai.session import spawn_model  # noqa: PLC0415

    result_id = str(uuid.uuid4())
    log = logger.bind(
        skeptic_id=result_id,
        finding_id=finding.id,
        task_id=task_id,
    )
    log.info(
        "skeptic_start",
        tribunal_verdict=(
            tribunal_session.verdict.value
            if tribunal_session.verdict
            else "NONE"
        ),
        existing_findings_count=len(existing_findings),
    )

    # ── Build finding summary for the context dict ───────────────────────────
    finding_text = finding.content
    if finding.content_en and finding.content_language != "en":
        finding_text += f"\n\n[English translation]\n{finding.content_en}"

    finding_summary = (
        f"ID         : {finding.id}\n"
        f"Confidence : {finding.confidence:.2f}\n"
        f"Language   : {finding.content_language}\n\n"
        f"{finding_text}"
    )

    # Build unanswered_questions block from tribunal.
    unanswered_block = ""
    if tribunal_session.unanswered_questions:
        unanswered_block = "\n".join(
            f"  • {q}" for q in tribunal_session.unanswered_questions
        )

    # ── Layer 0: AI question generation ──────────────────────────────────────
    skeptic_parsed, skeptic_session = await spawn_model(
        task_type=TaskType.SKEPTIC_QUESTIONS,
        context={
            "task_id": task_id,
            "finding_summary": finding_summary,
            "confidence_score": f"{finding.confidence:.2f}",
            "tribunal_verdict": (
                tribunal_session.verdict.value
                if tribunal_session.verdict
                else "N/A"
            ),
            "unanswered_questions": unanswered_block,
        },
        output_schema=SkepticQuestionsOutput,
        db=db,
        cost_tracker=cost_tracker,
    )
    skeptic_output: SkepticQuestionsOutput = skeptic_parsed

    log.info(
        "skeptic_ai_done",
        question_count=len(skeptic_output.questions),
        cost_usd=skeptic_session.cost_usd,
        hardest_question_index=skeptic_output.hardest_question_index,
    )

    # ── Layer 1: pure-Python classification ──────────────────────────────────
    # Exclude the finding under review so it doesn't trivially resolve its
    # own questions.
    corpus = [f for f in existing_findings if f.id != finding.id]
    classified_questions = classify_questions(skeptic_output.questions, corpus)

    # Tally for logging.
    # BUG-035 fix: the dict comprehension already initialises every enum key to 0;
    # using .get(q.classification, 0) + 1 is redundant and dead code.
    counts = {cls: 0 for cls in QuestionClassification}
    for q in classified_questions:
        counts[q.classification] += 1

    log.info(
        "skeptic_classification_done",
        resolved=counts[QuestionClassification.RESOLVED],
        researchable=counts[QuestionClassification.RESEARCHABLE],
        open_=counts[QuestionClassification.OPEN],
    )

    # ── Build SkepticResult — model validator computes aggregated counts ───────
    skeptic_result = SkepticResult(
        id=result_id,
        task_id=task_id,
        finding_id=finding.id,
        tribunal_session_id=tribunal_session.id,
        questions=classified_questions,
        cost_usd=skeptic_session.cost_usd,
    )

    log.info(
        "skeptic_result",
        passes_threshold=skeptic_result.passes_publishing_threshold,
        critical_open=skeptic_result.critical_open_count,
        open_total=skeptic_result.open_count,
    )

    # ── Persist ──────────────────────────────────────────────────────────────
    # BUG-A02 fix: wrap DB persistence in try/except so that a database failure
    # (network drop, pool exhaustion, constraint violation) does not abort the
    # skeptic result.  The AI computation already completed successfully; the
    # result is still returned to the caller even if persistence fails.
    try:
        await _persist_skeptic_result(
            db, skeptic_result, skeptic_output.overall_skeptic_assessment
        )
    except Exception as exc:
        log.error(
            "skeptic_persist_failed",
            skeptic_id=result_id,
            error=str(exc),
            msg="DB persistence failed but skeptic result is still returned",
        )

    return skeptic_result


# ---------------------------------------------------------------------------
# Database persistence
# ---------------------------------------------------------------------------


async def _persist_skeptic_result(
    db: Any,
    result: SkepticResult,
    overall_assessment: str,
) -> None:
    """
    Insert the SkepticResult and all its questions atomically.

    Questions are stored as a JSONB column alongside the summary counts so
    the record is self-contained and does not require joins for quick reporting.
    """
    questions_json = json.dumps(
        [
            {
                "number": q.number,
                "question": q.question,
                "category": q.category.value,
                "severity": q.severity.value,
                "classification": q.classification.value,
                "resolution_note": q.resolution_note,
            }
            for q in result.questions
        ]
    )

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO skeptic_results (
                    id, task_id, finding_id, tribunal_session_id,
                    questions,
                    open_count, researchable_count, resolved_count,
                    critical_open_count, passes_publishing_threshold,
                    cost_usd, created_at
                ) VALUES (
                    $1, $2, $3, $4,
                    $5::jsonb,
                    $6, $7, $8,
                    $9, $10,
                    $11, NOW()
                )
                ON CONFLICT (id) DO NOTHING
                """,
                result.id,
                result.task_id,
                result.finding_id,
                result.tribunal_session_id,
                questions_json,
                result.open_count,
                result.researchable_count,
                result.resolved_count,
                result.critical_open_count,
                result.passes_publishing_threshold,
                result.cost_usd,
            )
