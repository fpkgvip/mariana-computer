"""
mariana/orchestrator/event_loop.py

The main event loop that drives the Mariana Computer research process.

This module is the top-level entry-point for a research task.  It:

1. Loads task state (or resumes from the latest checkpoint).
2. Rebuilds ``ResearchSessionData`` from DB on every iteration.
3. Computes the appropriate ``TransitionTrigger`` based on current state.
4. Calls ``transition()`` to get the next state + action list.
5. Executes each action (SPAWN_AI, KILL_BRANCH, GRANT_BUDGET, etc.).
6. Persists the new state to the DB.
7. Handles ``BudgetExhaustedError`` by saving a checkpoint and halting.
8. On any unhandled exception: saves a checkpoint and marks task FAILED.

Architecture constraints enforced here
---------------------------------------
* All AI calls go through ``spawn_model()`` (ai layer) — never raw HTTP.
* Cost is recorded via ``CostTracker.record_call()`` immediately after
  every AI call.
* No blocking I/O — every operation is ``await``-ed.
* The loop runs until ``State.HALT`` is reached or an exception escapes.
"""

from __future__ import annotations

import asyncio
import json
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from mariana.ai.session import spawn_model
from mariana.data.models import (
    Branch,
    BranchStatus,
    EvidenceExtractionOutput,
    EvidenceType,
    EvaluationOutput,
    Finding,
    HypothesisGenerationOutput,
    ReportDraftOutput,
    ResearchArchitectureOutput,
    ResearchTask,
    SkepticQuestionsOutput,
    Source,
    SourceType,
    State,
    TaskStatus,
    TaskType,
    TribunalArgumentOutput,
    TribunalVerdict,
    TribunalVerdictOutput,
    QuestionClassification,
    QuestionSeverity,
)
from mariana.data.db import _row_to_dict
from mariana.orchestrator import checkpoint as checkpoint_module
from mariana.orchestrator.branch_manager import (
    create_branch,
    get_active_branches,
    grant_budget,
    kill_branch,
    score_branch,
)
from mariana.orchestrator.cost_tracker import BudgetExhaustedError, CostTracker
from mariana.orchestrator.diminishing_returns import check_diminishing_returns
from mariana.orchestrator.state_machine import (
    Action,
    InvalidTransitionError,
    ResearchSessionData,
    TransitionTrigger,
    transition,
)

if TYPE_CHECKING:
    pass  # AppConfig / redis imported below to avoid circular imports

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_ITERATIONS: int = 500
"""Safety ceiling on the main loop to prevent infinite runaway loops."""

_STRONG_FINDINGS_CONFIDENCE: float = 0.75
_STRONG_FINDINGS_MIN_COUNT: int = 3

_SCORE_HIGH_THRESHOLD: float = 0.7   # maps to BRANCH_SCORE_HIGH trigger (0–1 scale)
_SCORE_MED_THRESHOLD: float = 0.4    # maps to BRANCH_SCORE_MEDIUM trigger (0–1 scale)
# Below SCORE_MED_THRESHOLD → BRANCH_SCORE_LOW


# ===========================================================================
# Entry point
# ===========================================================================


async def run(
    task: ResearchTask,
    db: Any,       # asyncpg.Pool
    redis_client: Any,   # aioredis.Redis
    config: Any,   # mariana.config.AppConfig
    cost_tracker: Any = None,
    shutdown_flag: Any = None,
) -> None:
    """Run the research task to completion (or until halted/failed).

    Parameters
    ----------
    task:
        The ResearchTask to execute.  Its ``current_state`` and
        ``status`` fields are updated throughout.
    db:
        asyncpg connection pool.
    redis_client:
        aioredis client for pub/sub notifications and cache access.
    config:
        Application configuration (paths, model selection, timeouts, etc.).
    """
    log = logger.bind(task_id=task.id, topic=task.topic[:80])

    # ------------------------------------------------------------------
    # Initialise cost tracker
    # ------------------------------------------------------------------
    if cost_tracker is None:
        cost_tracker = CostTracker(
            task_id=task.id,
            task_budget=task.budget_usd,
            branch_hard_cap=getattr(config, "BUDGET_BRANCH_HARD_CAP", 75.0),
        )

    # ------------------------------------------------------------------
    # Mark task as RUNNING
    # ------------------------------------------------------------------
    task.status = TaskStatus.RUNNING
    task.started_at = datetime.now(timezone.utc)
    _sync_cost(task, cost_tracker)  # BUG-R3-01: sync cost fields before every persist
    await _persist_task(task, db)

    log.info("event_loop_started", budget=task.budget_usd)
    _emit_progress(redis_client, task.id, {
        "type": "status_change",
        "state": "INIT",
        "message": "Investigation starting...",
    })

    # ------------------------------------------------------------------
    # Fast path for instant / quick tiers
    # ------------------------------------------------------------------
    tier = (task.metadata or {}).get("tier", "standard")
    if tier in ("instant", "quick"):
        log.info("fast_path_tier", tier=tier)
        _emit_progress(redis_client, task.id, {
            "type": "status_change",
            "state": "SEARCH",
            "message": "Generating response...",
        })
        try:
            from mariana.ai.session import spawn_model as _fast_spawn  # noqa: PLC0415

            system_prompt = (
                "You are Mariana, an elite AI research assistant. "
                "Answer the user directly and concisely. "
                "If the user is just greeting or testing, respond warmly and briefly. "
                "If the user asks a factual question, answer it with citations where possible. "
                "Never refuse. Always be helpful."
            )
            fast_output, fast_session = await _fast_spawn(
                task_type=TaskType.HYPOTHESIS_GENERATION,
                context={
                    "task_id": task.id,
                    "topic": task.topic,
                    "budget_remaining": cost_tracker.budget_remaining,
                    "system_override": system_prompt,
                },
                output_schema=HypothesisGenerationOutput,
                branch_id=None,
                db=db,
                cost_tracker=cost_tracker,
                config=config,
            )
            task.ai_call_counter += 1

            # Extract the answer text from the output
            answer_text = ""
            if hasattr(fast_output, "hypotheses") and fast_output.hypotheses:
                answer_text = "\n\n".join(
                    h.statement for h in fast_output.hypotheses if hasattr(h, "statement")
                )
            if not answer_text and hasattr(fast_output, "score_rationale"):
                answer_text = fast_output.score_rationale
            if not answer_text:
                answer_text = str(fast_output)

            _emit_progress(redis_client, task.id, {
                "type": "text",
                "content": answer_text,
            })
            _emit_progress(redis_client, task.id, {
                "type": "status_change",
                "state": "HALT",
                "message": "Complete.",
            })
            # BUG-S5-02 fix: mark as COMPLETED only on success (set below)
            fast_success = True
        except Exception as fast_exc:
            fast_success = False
            log.error("fast_path_error", error=str(fast_exc), exc_info=True)
            _emit_progress(redis_client, task.id, {
                "type": "text",
                "content": f"I encountered an error: {fast_exc}",
            })
            _emit_progress(redis_client, task.id, {
                "type": "status_change",
                "state": "HALT",
                "message": "Failed.",
            })

        # BUG-S5-02 fix: mark task FAILED on error, not COMPLETED.
        # Previously the code unconditionally set status=COMPLETED even
        # when the fast path raised an exception.
        task.current_state = State.HALT
        task.status = TaskStatus.COMPLETED if fast_success else TaskStatus.FAILED
        if not fast_success:
            task.error_message = "Fast path LLM call failed"
        task.completed_at = datetime.now(timezone.utc)
        _sync_cost(task, cost_tracker)
        await _persist_task(task, db)
        return  # Skip the full research pipeline

    # ------------------------------------------------------------------
    # Attempt checkpoint resume
    # ------------------------------------------------------------------
    latest_cp = await checkpoint_module.load_latest_checkpoint(
        task.id, db, data_root=config.DATA_ROOT
    )
    if latest_cp is not None:
        log.info(
            "resuming_from_checkpoint",
            checkpoint_id=latest_cp.id,
            state=latest_cp.state_machine_state.value,
            total_spent=latest_cp.total_spent,
        )
        task.current_state = latest_cp.state_machine_state
        task.diminishing_flags = latest_cp.diminishing_flags
        task.ai_call_counter = latest_cp.ai_call_counter
        # Restore cost tracker totals from checkpoint
        cost_tracker.total_spent = latest_cp.total_spent
        # Restore per-branch breakdown from DB (BUG-017)
        branch_rows = await db.fetch(
            "SELECT branch_id, SUM(cost_usd) as total FROM ai_sessions "
            "WHERE task_id = $1 GROUP BY branch_id",
            task.id,
        )
        for _br in branch_rows:
            if _br["branch_id"]:
                cost_tracker.per_branch[_br["branch_id"]] = float(_br["total"] or 0)
        # Restore call count
        _call_count = await db.fetchval(
            "SELECT COUNT(*) FROM ai_sessions WHERE task_id = $1", task.id
        )
        cost_tracker.call_count = int(_call_count or 0)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    iteration = 0
    data_root = config.DATA_ROOT

    try:
        while task.current_state != State.HALT and iteration < _MAX_ITERATIONS:
            if shutdown_flag is not None and shutdown_flag.is_set():
                logger.info("shutdown_flag_detected", task_id=task.id)
                task.status = TaskStatus.HALTED
                task.current_state = State.HALT
                break
            iteration += 1
            log.debug(
                "loop_iteration",
                iteration=iteration,
                state=task.current_state.value,
                spent=cost_tracker.total_spent,
            )

            # --------------------------------------------------------- #
            # 0.5  Periodic credit balance check (every 50 iterations)
            # --------------------------------------------------------- #
            if iteration % 50 == 0:
                user_id = getattr(task, "metadata", {}).get("user_id", "")
                if user_id and getattr(config, "SUPABASE_URL", "") and getattr(config, "SUPABASE_SERVICE_KEY", ""):
                    try:
                        remaining_tokens = await _check_user_credits(user_id, config)
                        if remaining_tokens is not None and remaining_tokens <= 0:
                            log.warning("user_credits_exhausted", user_id=user_id)
                            _emit_progress(redis_client, task.id, {
                                "type": "text",
                                "content": "Investigation paused \u2014 please add credits to continue.",
                            })
                            task.status = TaskStatus.HALTED
                            task.current_state = State.HALT
                            break
                    except Exception as exc:
                        log.debug("credit_check_failed", error=str(exc))

            # --------------------------------------------------------- #
            # 1. Build session data
            # --------------------------------------------------------- #
            session_data = await _build_session_data(task, db)

            # --------------------------------------------------------- #
            # 2. Pre-state work + trigger computation
            # --------------------------------------------------------- #
            trigger = await compute_trigger(
                task.current_state,
                session_data,
                cost_tracker,
                db,
                redis_client,
                config,
            )

            log.info(
                "trigger_computed",
                state=task.current_state.value,
                trigger=trigger.value,
            )

            # --------------------------------------------------------- #
            # 3. Advance state machine
            # --------------------------------------------------------- #
            try:
                next_state, actions = transition(
                    current_state=task.current_state,
                    trigger=trigger,
                    session_data=session_data,
                    db=db,
                    cost_tracker=cost_tracker,
                )
            except InvalidTransitionError as exc:
                log.error(
                    "invalid_transition",
                    state=exc.state.value,
                    trigger=exc.trigger.value,
                )
                # Treat unknown transitions as HALT to avoid runaway
                next_state = State.HALT
                actions = [Action("HALT", {"reason": "invalid_transition"})]

            # --------------------------------------------------------- #
            # 4. Execute actions
            # --------------------------------------------------------- #
            for action in actions:
                await _execute_action(
                    action=action,
                    task=task,
                    session_data=session_data,
                    cost_tracker=cost_tracker,
                    db=db,
                    redis_client=redis_client,
                    config=config,
                    data_root=data_root,
                )

            # --------------------------------------------------------- #
            # 5. Advance state + persist
            # --------------------------------------------------------- #
            task.current_state = next_state
            _sync_cost(task, cost_tracker)  # BUG-R3-01: sync cost fields before every persist
            await _persist_task(task, db)

            log.info("state_advanced", new_state=next_state.value, iteration=iteration)
            _emit_progress(redis_client, task.id, {
                "type": "status_change",
                "state": next_state.value,
                "message": f"Transitioned to {next_state.value}",
            })
            _emit_progress(redis_client, task.id, {
                "type": "cost_update",
                "spent_usd": round(cost_tracker.total_spent, 4),
                "budget_usd": cost_tracker.task_budget,
            })

            # Allow other coroutines to run (cooperative multitasking)
            await asyncio.sleep(0)

        # -------------------------------------------------------------- #
        # Loop exit
        # -------------------------------------------------------------- #
        # Check HALT state before iteration limit (BUG-004)
        if task.current_state == State.HALT:
            # Only mark COMPLETED if not already explicitly HALTED (e.g. by SIGTERM handler)
            # BUG-NEW-02 fix: preserve HALTED status set by shutdown_flag handler
            # BUG-NEW-12 fix: also preserve FAILED; only promote to COMPLETED if status
            # is still RUNNING (i.e. a clean report_complete halt path)
            if task.status not in (TaskStatus.HALTED, TaskStatus.FAILED):
                task.status = TaskStatus.COMPLETED
        elif iteration >= _MAX_ITERATIONS:
            log.error("max_iterations_reached", iterations=_MAX_ITERATIONS)
            task.status = TaskStatus.HALTED

        task.completed_at = datetime.now(timezone.utc)
        _sync_cost(task, cost_tracker)  # BUG-R3-01: sync cost fields before every persist
        await _persist_task(task, db)
        log.info(
            "event_loop_finished",
            status=task.status.value,
            iterations=iteration,
            total_spent=cost_tracker.total_spent,
        )

        # ── Save investigation to user memory for cross-session context ──
        _mem_user_id = (task.metadata or {}).get("user_id", "")
        if _mem_user_id and task.status == TaskStatus.COMPLETED:
            try:
                from mariana.tools.memory import UserMemory  # noqa: PLC0415
                _mem = UserMemory(user_id=_mem_user_id, data_root=Path(config.DATA_ROOT))
                _mem.add_to_history(task.topic, f"Completed in {iteration} iterations, cost ${cost_tracker.total_spent:.2f}")
                log.info("user_memory_updated", user_id=_mem_user_id)
            except Exception as _mem_exc:
                log.debug("user_memory_save_skipped", error=str(_mem_exc))

    except BudgetExhaustedError as exc:
        log.warning(
            "budget_exhausted_caught",
            scope=exc.scope,
            spent=exc.spent,
            cap=exc.cap,
        )
        await _emergency_checkpoint(task, cost_tracker, db, data_root)
        task.status = TaskStatus.HALTED
        task.error_message = str(exc)
        task.completed_at = datetime.now(timezone.utc)
        _sync_cost(task, cost_tracker)  # BUG-R3-01: sync cost fields before every persist
        await _persist_task(task, db)

    except Exception as exc:  # noqa: BLE001
        log.error(
            "unhandled_exception",
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        try:
            await _emergency_checkpoint(task, cost_tracker, db, data_root)
        except Exception:  # noqa: BLE001
            log.error("emergency_checkpoint_failed")
        task.status = TaskStatus.FAILED
        task.error_message = str(exc)
        task.completed_at = datetime.now(timezone.utc)
        _sync_cost(task, cost_tracker)  # BUG-R3-01: sync cost fields before every persist
        await _persist_task(task, db)
        raise


# ===========================================================================
# Trigger computation
# ===========================================================================


async def compute_trigger(
    state: State,
    session_data: ResearchSessionData,
    cost_tracker: CostTracker,
    db: Any,
    redis_client: Any,
    config: Any,
) -> TransitionTrigger:
    """Determine the appropriate trigger for the current state.

    This function examines current session data and produces the single
    trigger that best describes what just happened / should happen next.

    Parameters
    ----------
    state:
        Current state-machine state.
    session_data:
        Current research session snapshot.
    cost_tracker:
        Live cost tracker.
    db, redis_client, config:
        Infrastructure dependencies (some states need DB queries).

    Returns
    -------
    TransitionTrigger
        The computed trigger.
    """
    # Budget cap always wins
    if cost_tracker.is_exhausted:
        return TransitionTrigger.BUDGET_HARD_CAP

    task = session_data.task

    match state:
        case State.INIT:
            # Always start by generating hypotheses
            return TransitionTrigger.HYPOTHESES_READY

        case State.SEARCH:
            # Search has run; batch is complete
            return TransitionTrigger.BATCH_COMPLETE

        case State.EVALUATE:
            return await _trigger_for_evaluate(session_data, db)

        case State.DEEPEN:
            return await _trigger_for_deepen(session_data, db)

        case State.CHECKPOINT:
            return await _trigger_for_checkpoint(session_data, db)

        case State.PIVOT:
            # After pivot, new hypotheses should have been generated
            return TransitionTrigger.HYPOTHESES_READY

        case State.TRIBUNAL:
            return await _trigger_for_tribunal(session_data, db)

        case State.SKEPTIC:
            return await _trigger_for_skeptic(session_data, db)

        case State.REPORT:
            # Report generation complete
            return TransitionTrigger.BATCH_COMPLETE

        case State.HALT:
            # Should never be called, but safe default
            return TransitionTrigger.BUDGET_HARD_CAP

        case _:
            logger.warning("unknown_state_for_trigger", state=state.value)
            return TransitionTrigger.BUDGET_HARD_CAP


# ---------------------------------------------------------------------------
# Per-state trigger helpers
# ---------------------------------------------------------------------------


async def _trigger_for_evaluate(
    session_data: ResearchSessionData,
    db: Any,
) -> TransitionTrigger:
    """Compute trigger for EVALUATE state.

    Precedence:
    1. All branches exhausted → ALL_BRANCHES_EXHAUSTED
    2. Strong findings exist → STRONG_FINDINGS_EXIST
    3. Best active branch score → HIGH / MEDIUM / LOW
    """
    active = session_data.active_branches
    findings = session_data.recent_findings

    if not active:
        return TransitionTrigger.ALL_BRANCHES_EXHAUSTED

    # Check for strong findings
    high_conf = [
        f for f in findings
        if f.confidence >= _STRONG_FINDINGS_CONFIDENCE
    ]
    if len(high_conf) >= _STRONG_FINDINGS_MIN_COUNT:
        return TransitionTrigger.STRONG_FINDINGS_EXIST

    # Use the best active branch score to decide direction
    best_score = _best_branch_score(active)
    if best_score is None:
        # No scores yet — keep searching
        return TransitionTrigger.BATCH_COMPLETE

    if best_score >= _SCORE_HIGH_THRESHOLD:
        return TransitionTrigger.BRANCH_SCORE_HIGH
    elif best_score >= _SCORE_MED_THRESHOLD:
        return TransitionTrigger.BRANCH_SCORE_MEDIUM
    else:
        return TransitionTrigger.BRANCH_SCORE_LOW


async def _trigger_for_deepen(
    session_data: ResearchSessionData,
    db: Any,
) -> TransitionTrigger:
    """Compute trigger for DEEPEN state."""
    active = session_data.active_branches

    if not active:
        return TransitionTrigger.ALL_BRANCHES_EXHAUSTED

    best_score = _best_branch_score(active)
    if best_score is None:
        return TransitionTrigger.BRANCH_SCORE_MEDIUM  # default: keep deepening

    if best_score >= _SCORE_HIGH_THRESHOLD:
        return TransitionTrigger.BRANCH_SCORE_HIGH
    elif best_score >= _SCORE_MED_THRESHOLD:
        return TransitionTrigger.BRANCH_SCORE_MEDIUM
    else:
        return TransitionTrigger.BRANCH_SCORE_LOW


async def _trigger_for_checkpoint(
    session_data: ResearchSessionData,
    db: Any,
) -> TransitionTrigger:
    """Compute trigger for CHECKPOINT state."""
    task = session_data.task
    active = session_data.active_branches
    findings = session_data.recent_findings

    # 3 consecutive DR flags → halt
    if task.diminishing_flags >= 3:
        return TransitionTrigger.CONSECUTIVE_DR_FLAGS_3

    # Strong findings exist → tribunal
    high_conf = [
        f for f in findings
        if f.confidence >= _STRONG_FINDINGS_CONFIDENCE
    ]
    if len(high_conf) >= _STRONG_FINDINGS_MIN_COUNT:
        return TransitionTrigger.STRONG_FINDINGS_EXIST

    # No active branches → pivot or halt
    if not active:
        return TransitionTrigger.ALL_BRANCHES_EXHAUSTED

    # DR flags at 1 or 2 → diminishing returns handling
    if task.diminishing_flags in (1, 2):
        return TransitionTrigger.DIMINISHING_RETURNS

    # BUG-026: Default fallthrough → continue normal research loop, not STRONG_FINDINGS_EXIST
    return TransitionTrigger.BATCH_COMPLETE


async def _trigger_for_tribunal(
    session_data: ResearchSessionData,
    db: Any,
) -> TransitionTrigger:
    """Compute trigger for TRIBUNAL state based on latest tribunal verdict in DB."""
    row = await db.fetchrow(
        """
        SELECT verdict FROM tribunal_sessions
        WHERE task_id = $1
        ORDER BY created_at DESC
        LIMIT 1
        """,
        session_data.task.id,
    )
    if row is None:
        # BUG-NEW-09 fix: returning TRIBUNAL_WEAKENED here would prematurely
        # regress the investigation to SEARCH (or HALT) before the tribunal
        # handler has written any result.  Return TRIBUNAL_CONFIRMED as the
        # safest neutral fallback — it keeps the loop advancing instead of
        # destroying the current branch on a race-condition miss.
        logger.warning("no_tribunal_result", task_id=session_data.task.id)
        return TransitionTrigger.TRIBUNAL_CONFIRMED

    verdict_str: str = row["verdict"]
    if verdict_str == TribunalVerdict.CONFIRMED.value:
        return TransitionTrigger.TRIBUNAL_CONFIRMED
    elif verdict_str == TribunalVerdict.WEAKENED.value:
        return TransitionTrigger.TRIBUNAL_WEAKENED
    else:  # DESTROYED
        return TransitionTrigger.TRIBUNAL_DESTROYED


async def _trigger_for_skeptic(
    session_data: ResearchSessionData,
    db: Any,
) -> TransitionTrigger:
    """Compute trigger for SKEPTIC state based on latest skeptic result in DB."""
    row = await db.fetchrow(
        """
        SELECT critical_open_count, researchable_count, passes_publishing_threshold
        FROM skeptic_results
        WHERE task_id = $1
        ORDER BY created_at DESC
        LIMIT 1
        """,
        session_data.task.id,
    )
    if row is None:
        # BUG-NEW-08 fix: returning SKEPTIC_CRITICAL_OPEN here would cause an
        # immediate HALT on first entry into the SKEPTIC state before the
        # handle_skeptic action has written any result.  Return a neutral
        # trigger that keeps the loop progressing instead.
        logger.warning("no_skeptic_result", task_id=session_data.task.id)
        return TransitionTrigger.SKEPTIC_RESEARCHABLE_EXIST

    if row["critical_open_count"] > 0:
        return TransitionTrigger.SKEPTIC_CRITICAL_OPEN
    if row["researchable_count"] > 0:
        return TransitionTrigger.SKEPTIC_RESEARCHABLE_EXIST
    return TransitionTrigger.SKEPTIC_QUESTIONS_RESOLVED


# ===========================================================================
# State handlers (do the actual AI/connector work)
# ===========================================================================


async def handle_init(
    task: ResearchTask,
    session_data: ResearchSessionData,
    cost_tracker: CostTracker,
    db: Any,
    redis_client: Any,
    config: Any,
) -> None:
    """Multi-step INIT: research architecture → hypothesis generation.

    Step 1: Generate a detailed research architecture (topic analysis,
    research plan, hypotheses to test, data sources, timeline).
    Step 2: Generate specific hypotheses informed by the architecture.
    This ensures investigations are focused and cost-effective.
    """
    log = logger.bind(task_id=task.id, handler="init")

    # ── Step 1: Research Architecture ─────────────────────────────────────
    log.info("generating_research_architecture")
    _emit_progress(redis_client, task.id, {
        "type": "status_change",
        "state": "INIT",
        "message": "Analyzing topic and designing research architecture...",
    })

    arch_output, arch_session = await spawn_model(
        task_type=TaskType.RESEARCH_ARCHITECTURE,
        context={
            "task_id": task.id,
            "topic": task.topic,
            "budget_remaining": cost_tracker.budget_remaining,
            "budget_usd": task.budget_usd,
        },
        output_schema=ResearchArchitectureOutput,
        branch_id=None,
        db=db,
        cost_tracker=cost_tracker,
        config=config,
    )
    task.ai_call_counter += 1

    architecture: ResearchArchitectureOutput = arch_output  # type: ignore[assignment]
    log.info(
        "research_architecture_ready",
        hypotheses_count=len(architecture.hypotheses),
        data_sources=len(architecture.data_sources),
        complexity=architecture.estimated_complexity,
    )

    _emit_progress(redis_client, task.id, {
        "type": "status_change",
        "state": "INIT",
        "message": f"Research plan ready ({len(architecture.hypotheses)} hypotheses, "
                   f"{len(architecture.data_sources)} data sources). Generating detailed hypotheses...",
    })

    # ── Step 2: Hypothesis Generation (informed by architecture) ──────────
    log.info("generating_hypotheses")

    parsed_output, ai_session = await spawn_model(
        task_type=TaskType.HYPOTHESIS_GENERATION,
        context={
            "task_id": task.id,
            "topic": task.topic,
            "budget_remaining": cost_tracker.budget_remaining,
            "research_architecture": architecture.topic_analysis,
            "research_plan": architecture.research_plan,
            "architecture_hypotheses": [
                {"statement": h.statement, "test_strategy": h.test_strategy}
                for h in architecture.hypotheses
            ],
            "data_sources": architecture.data_sources,
        },
        output_schema=HypothesisGenerationOutput,
        branch_id=None,
        db=db,
        cost_tracker=cost_tracker,
        config=config,
    )
    # spawn_model already records cost internally via _record_cost — do NOT
    # call cost_tracker.record_call() again here (would double-count).
    task.ai_call_counter += 1

    # Use the parsed HypothesisGenerationOutput to persist hypotheses and
    # create branches.  spawn_model does NOT write hypotheses to the DB —
    # that is the orchestrator's responsibility.
    hypothesis_output: HypothesisGenerationOutput = parsed_output  # type: ignore[assignment]
    from mariana.data.models import Hypothesis, HypothesisStatus  # noqa: PLC0415
    import uuid as _uuid  # noqa: PLC0415
    created_hypotheses = []
    # BUG-023: Wrap hypothesis + branch insertion in a transaction to avoid partial state
    async with db.acquire() as _conn:
        async with _conn.transaction():
            for gen_hyp in hypothesis_output.hypotheses:
                hyp = Hypothesis(
                    id=str(_uuid.uuid4()),
                    task_id=task.id,
                    statement=gen_hyp.statement,
                    statement_zh=gen_hyp.statement_zh,
                    rationale=gen_hyp.rationale,
                    status=HypothesisStatus.ACTIVE,
                )
                await _conn.execute(
                    """
                    INSERT INTO hypotheses (
                        id, task_id, parent_id, depth, statement, statement_zh,
                        status, score, momentum_note, rationale, created_at, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW(), NOW())
                    """,
                    hyp.id, hyp.task_id, None, 0,
                    hyp.statement, hyp.statement_zh,
                    hyp.status.value, None, None, hyp.rationale,
                )
                await _conn.execute(
                    """
                    INSERT INTO branches (
                        id, hypothesis_id, task_id, status,
                        score_history, budget_allocated, budget_spent,
                        grants_log, cycles_completed, kill_reason,
                        sources_searched, created_at, updated_at
                    ) VALUES (
                        $1, $2, $3, $4,
                        $5, $6, $7,
                        $8, $9, $10,
                        $11, NOW(), NOW()
                    )
                    """,
                    str(_uuid.uuid4()),
                    hyp.id,
                    hyp.task_id,
                    BranchStatus.ACTIVE.value,
                    "[]",  # score_history
                    5.0,   # budget_allocated = BUDGET_INITIAL
                    0.0,   # budget_spent
                    "[]",  # grants_log
                    0,     # cycles_completed
                    None,  # kill_reason
                    "[]",  # sources_searched
                )
                created_hypotheses.append(hyp)

    log.info("hypotheses_ready", count=len(created_hypotheses))
    _emit_progress(redis_client, task.id, {
        "type": "text",
        "content": f"Generated {len(created_hypotheses)} research hypotheses. Beginning investigation...",
    })

    # ── Skills detection ─────────────────────────────────────────────────
    try:
        from mariana.tools.skills import SkillManager  # noqa: PLC0415
        skill_mgr = SkillManager(data_root=Path(config.DATA_ROOT))
        detected_skill = skill_mgr.detect_skill(task.topic)
        if detected_skill:
            log.info("skill_detected", skill_id=detected_skill.id, skill_name=detected_skill.name)
            # Store skill context on the task metadata for later prompt injection
            task.metadata = {**(task.metadata or {}), "active_skill": detected_skill.id}
            _emit_progress(redis_client, task.id, {
                "type": "text",
                "content": f"Activated skill: {detected_skill.name}",
            })
    except Exception as exc:
        log.debug("skill_detection_skipped", error=str(exc))

    # ── User memory injection ────────────────────────────────────────────
    user_id = (task.metadata or {}).get("user_id", "")
    if user_id:
        try:
            from mariana.tools.memory import UserMemory  # noqa: PLC0415
            memory = UserMemory(user_id=user_id, data_root=Path(config.DATA_ROOT))
            memory_ctx = memory.get_context_for_prompt()
            if memory_ctx:
                task.metadata = {**(task.metadata or {}), "user_memory_context": memory_ctx}
                log.info("user_memory_loaded", user_id=user_id)
        except Exception as exc:
            log.debug("user_memory_skipped", error=str(exc))

    # ── Sub-agent delegation for complex multi-faceted queries ───────────
    if len(created_hypotheses) >= 3:
        try:
            from mariana.orchestrator.sub_agents import SubAgentManager, SubAgentRole  # noqa: PLC0415
            sub_mgr = SubAgentManager(task.id, cost_tracker, redis_client, config)
            # Delegate fact-checking to a sub-agent for the top hypothesis
            top_hyp = created_hypotheses[0]
            await sub_mgr.delegate(
                SubAgentRole.FACT_CHECKER,
                f"Preliminary fact-check: {top_hyp.statement}",
                context=architecture.topic_analysis,
            )
            completed = await sub_mgr.execute_all(db=db, config=config)
            if completed:
                sub_ctx = sub_mgr.get_completed_context()
                if sub_ctx:
                    task.metadata = {**(task.metadata or {}), "sub_agent_findings": sub_ctx[:2000]}
            log.info("sub_agents_complete", count=len(completed))
        except Exception as exc:
            log.debug("sub_agent_delegation_skipped", error=str(exc))


async def handle_search(
    task: ResearchTask,
    session_data: ResearchSessionData,
    cost_tracker: CostTracker,
    db: Any,
    redis_client: Any,
    config: Any,
) -> None:
    """Dispatch browser/connector searches for all active branches.

    For each active branch, dispatches evidence-extraction AI calls
    against the current search plan.  When ``PERPLEXITY_API_KEY`` is
    configured, a parallel Perplexity Sonar search is run first and
    the results (with citations) are injected into the AI context.
    """
    log = logger.bind(task_id=task.id, handler="search")
    active = session_data.active_branches

    log.info("dispatching_search", active_branches=len(active))
    _emit_progress(redis_client, task.id, {
        "type": "status_change",
        "state": "SEARCH",
        "message": f"Searching across {len(active)} active branches...",
    })

    # ── Optional Perplexity parallel search ──────────────────────────────
    perplexity_key: str = getattr(config, "PERPLEXITY_API_KEY", "") or ""
    perplexity_context: dict[str, str] = {}  # branch_id → formatted results

    if perplexity_key:
        # Build one query per active branch from its hypothesis statement
        branch_queries: list[tuple[str, str]] = []  # (branch_id, query)
        for branch in active:
            if branch.status != BranchStatus.ACTIVE:
                continue
            _h_row = await db.fetchrow(
                "SELECT statement FROM hypotheses WHERE id = $1",
                branch.hypothesis_id,
            )
            stmt = _h_row["statement"] if _h_row else ""
            if stmt:
                branch_queries.append((branch.id, stmt))

        if branch_queries:
            _emit_progress(redis_client, task.id, {
                "type": "status_change",
                "state": "SEARCH",
                "message": f"Running {len(branch_queries)} Perplexity searches in parallel...",
            })
            try:
                from mariana.tools.perplexity_search import (  # noqa: PLC0415
                    format_results_with_citations,
                    parallel_search,
                )
                queries = [q for _, q in branch_queries]
                results = await parallel_search(queries, perplexity_key, max_concurrent=5)
                for (bid, _query), result in zip(branch_queries, results):
                    perplexity_context[bid] = format_results_with_citations([result])
                log.info("perplexity_search_complete", results=len(results))
            except Exception as exc:
                log.warning("perplexity_search_failed", error=str(exc))
    # ─────────────────────────────────────────────────────────────────────

    for branch in active:
        if branch.status != BranchStatus.ACTIVE:
            continue

        try:
            # BUG-002: Fetch actual hypothesis statement instead of using the UUID
            _hyp_row = await db.fetchrow(
                "SELECT statement FROM hypotheses WHERE id = $1",
                branch.hypothesis_id,
            )
            _hyp_statement = _hyp_row["statement"] if _hyp_row else ""

            # Inject Perplexity results as additional page_content if available
            page_content = perplexity_context.get(branch.id, "")

            _, ai_session = await spawn_model(
                task_type=TaskType.EVIDENCE_EXTRACTION,
                context={
                    "task_id": task.id,
                    "hypothesis_id": branch.hypothesis_id,
                    "hypothesis_statement": _hyp_statement,
                    "page_content": page_content,
                    "source_url": "",
                    "sources_already_searched": branch.sources_searched,
                    "budget_remaining": cost_tracker.budget_remaining,
                },
                output_schema=EvidenceExtractionOutput,
                branch_id=branch.id,
                db=db,
                cost_tracker=cost_tracker,
                config=config,
            )
            # spawn_model already records cost internally — do NOT double-count
            task.ai_call_counter += 1

        except BudgetExhaustedError:
            log.warning("search_budget_exhausted", branch_id=branch.id)
            raise

    log.info("search_batch_complete", branches_searched=len(active))


async def handle_evaluate(
    task: ResearchTask,
    session_data: ResearchSessionData,
    cost_tracker: CostTracker,
    db: Any,
    redis_client: Any,
    config: Any,
) -> None:
    """Run evaluation AI calls for each active branch and score them.

    After evaluation, each branch is scored via :func:`score_branch`.
    The resulting :class:`BranchDecision` is logged; kills / grants are
    executed as side-effects of ``score_branch``.
    """
    log = logger.bind(task_id=task.id, handler="evaluate")
    active = session_data.active_branches

    log.info("evaluating_branches", count=len(active))
    _emit_progress(redis_client, task.id, {
        "type": "status_change",
        "state": "EVALUATE",
        "message": f"Evaluating {len(active)} active branches...",
    })

    for branch in active:
        if branch.status != BranchStatus.ACTIVE:
            continue

        try:
            # Fetch hypothesis statement for evaluation context
            hyp_row = await db.fetchrow(
                "SELECT statement FROM hypotheses WHERE id = $1",
                branch.hypothesis_id,
            )
            hyp_statement = hyp_row["statement"] if hyp_row else ""

            # Count sources searched for this branch as a proxy for sources_searched
            sources_searched_count = len(branch.sources_searched)

            eval_output, ai_session = await spawn_model(
                task_type=TaskType.EVALUATION,
                context={
                    "task_id": task.id,
                    "hypothesis_id": branch.hypothesis_id,
                    "hypothesis_statement": hyp_statement,
                    "compressed_findings": "",
                    "sources_searched": sources_searched_count,
                    "prior_scores": branch.score_history,
                    "budget_remaining": cost_tracker.budget_remaining,
                },
                output_schema=EvaluationOutput,
                branch_id=branch.id,
                db=db,
                cost_tracker=cost_tracker,
                config=config,
            )
            # spawn_model already records cost internally — do NOT double-count
            task.ai_call_counter += 1

            # Use the score directly from the parsed EvaluationOutput
            new_score = float(eval_output.score)

            decision = await score_branch(
                branch_id=branch.id,
                new_score=new_score,
                # BUG-NEW-07 fix: score_branch no longer calls
                # cost_tracker.record_branch_spend() internally to avoid
                # double-counting; it only updates branch.budget_spent in DB.
                # The actual cost was already recorded by spawn_model.
                cost_spent_this_cycle=ai_session.cost_usd,
                db=db,
                cost_tracker=cost_tracker,
            )
            log.info(
                "branch_scored",
                branch_id=branch.id,
                score=new_score,
                decision=decision.action,
            )

        except BudgetExhaustedError:
            log.warning("evaluate_budget_exhausted", branch_id=branch.id)
            raise


async def handle_deepen(
    task: ResearchTask,
    session_data: ResearchSessionData,
    cost_tracker: CostTracker,
    db: Any,
    redis_client: Any,
    config: Any,
) -> None:
    """Run deeper evidence-extraction passes on high-scoring branches.

    Mirrors :func:`handle_search` but uses focused deepening prompts
    derived from the evaluation's ``next_search_keywords``.
    """
    log = logger.bind(task_id=task.id, handler="deepen")
    active = session_data.active_branches

    log.info("deepening_branches", count=len(active))

    for branch in active:
        if branch.status != BranchStatus.ACTIVE:
            continue
        if branch.latest_score is None or branch.latest_score < _SCORE_HIGH_THRESHOLD:
            continue

        try:
            # BUG-002: Fetch actual hypothesis statement instead of using the UUID
            _hyp_row_d = await db.fetchrow(
                "SELECT statement FROM hypotheses WHERE id = $1",
                branch.hypothesis_id,
            )
            _hyp_statement_d = _hyp_row_d["statement"] if _hyp_row_d else ""

            _, ai_session = await spawn_model(
                task_type=TaskType.EVIDENCE_EXTRACTION,
                context={
                    "task_id": task.id,
                    "hypothesis_id": branch.hypothesis_id,
                    "hypothesis_statement": _hyp_statement_d,
                    "page_content": "",
                    "source_url": "",
                    "mode": "DEEPEN",
                    "sources_already_searched": branch.sources_searched,
                    "prior_score": branch.latest_score,
                    "budget_remaining": cost_tracker.budget_remaining,
                },
                output_schema=EvidenceExtractionOutput,
                branch_id=branch.id,
                db=db,
                cost_tracker=cost_tracker,
                config=config,
            )
            # spawn_model already records cost internally — do NOT double-count
            task.ai_call_counter += 1

        except BudgetExhaustedError:
            log.warning("deepen_budget_exhausted", branch_id=branch.id)
            raise

    log.info("deepen_complete")


async def handle_checkpoint(
    task: ResearchTask,
    session_data: ResearchSessionData,
    cost_tracker: CostTracker,
    db: Any,
    redis_client: Any,
    config: Any,
    data_root: str,
) -> None:
    """Save a checkpoint and run diminishing-returns analysis."""
    log = logger.bind(task_id=task.id, handler="checkpoint")

    # Fetch all findings for DR analysis
    finding_rows = await db.fetch(
        """
        SELECT id, task_id, hypothesis_id, content, content_en, content_language,
               source_ids, confidence, evidence_type, is_compressed,
               raw_content_path, created_at, metadata
        FROM findings WHERE task_id = $1
        """,
        task.id,
    )
    # BUG-013: Use _row_to_dict() to decode JSONB/enum fields before model_validate
    findings = [
        Finding.model_validate({**_row_to_dict(r), "evidence_type": EvidenceType(r["evidence_type"])})
        for r in finding_rows
    ]

    # Count sources for DR analysis
    source_count = await db.fetchval(
        "SELECT COUNT(*) FROM sources WHERE task_id = $1", task.id
    )

    # Run DR check on the best active branch (if any)
    active = session_data.active_branches
    dr_recommendation = None  # BUG-R3-05: track recommendation to pass to save_checkpoint
    if active:
        best_branch = max(
            active,
            key=lambda b: b.latest_score if b.latest_score is not None else 0.0,
        )
        prev_finding_count = max(0, len(findings) - len(session_data.recent_findings))
        prev_source_count = max(0, int(source_count) - len(session_data.all_source_ids))

        # BUG-003: Capture and log the return value of check_diminishing_returns
        dr_result = check_diminishing_returns(
            branch=best_branch,
            findings_before=prev_finding_count,
            findings_after=len(findings),
            sources_before=prev_source_count,
            sources_after=int(source_count),
            task=task,
            config=config,
        )
        dr_recommendation = dr_result.recommendation  # BUG-R3-05: capture for checkpoint
        log.info(
            "diminishing_returns_result",
            recommendation=dr_result.recommendation.value,
            novelty=dr_result.novelty,
            new_sources=dr_result.new_sources,
            score_delta=dr_result.score_delta,
            flag_triggered=dr_result.flag_triggered,
        )

    killed_rows = await db.fetch(
        "SELECT id, hypothesis_id, task_id, status, score_history, "
        "budget_allocated, budget_spent, grants_log, cycles_completed, "
        "kill_reason, sources_searched, created_at, updated_at "
        "FROM branches WHERE task_id = $1 AND status = 'KILLED'",
        task.id,
    )
    # BUG-013: Use _row_to_dict() to decode JSONB/enum fields before model_validate
    killed_branches = [
        Branch.model_validate({**_row_to_dict(r), "status": BranchStatus(r["status"])})
        for r in killed_rows
    ]

    checkpoint_id = await checkpoint_module.save_checkpoint(
        task=task,
        active_branches=active,
        killed_branches=killed_branches,
        findings=findings,
        current_state=task.current_state,
        cost_tracker=cost_tracker,
        db=db,
        data_root=data_root,
        diminishing_result=dr_recommendation,  # BUG-R3-05
    )
    log.info("checkpoint_complete", checkpoint_id=checkpoint_id)


async def handle_tribunal(
    task: ResearchTask,
    session_data: ResearchSessionData,
    cost_tracker: CostTracker,
    db: Any,
    redis_client: Any,
    config: Any,
) -> None:
    """Run the adversarial tribunal against the strongest findings.

    Spawns four sequential AI calls: plaintiff, defendant, rebuttal,
    counter-rebuttal, then judge verdict.
    """
    log = logger.bind(task_id=task.id, handler="tribunal")
    _emit_progress(redis_client, task.id, {
        "type": "status_change",
        "state": "TRIBUNAL",
        "message": "Running adversarial tribunal review...",
    })

    # Find the highest-confidence finding to put on trial
    top_finding = await db.fetchrow(
        """
        SELECT id, hypothesis_id FROM findings
        WHERE task_id = $1
        ORDER BY confidence DESC
        LIMIT 1
        """,
        task.id,
    )
    if top_finding is None:
        log.warning("no_finding_for_tribunal")
        return

    finding_id = top_finding["id"]
    tribunal_id = str(uuid.uuid4())

    # Argument stages use TribunalArgumentOutput; judge uses TribunalVerdictOutput
    argument_stages = (
        TaskType.TRIBUNAL_PLAINTIFF,
        TaskType.TRIBUNAL_DEFENDANT,
        TaskType.TRIBUNAL_REBUTTAL,
        TaskType.TRIBUNAL_COUNTER,
    )
    total_tribunal_cost: float = 0.0
    for task_type in argument_stages:
        _, ai_session = await spawn_model(
            task_type=task_type,
            context={
                "task_id": task.id,
                "tribunal_id": tribunal_id,
                "finding_id": finding_id,
                "budget_remaining": cost_tracker.budget_remaining,
            },
            output_schema=TribunalArgumentOutput,
            branch_id=None,
            db=db,
            cost_tracker=cost_tracker,
            config=config,
        )
        # spawn_model already records cost internally — do NOT double-count
        task.ai_call_counter += 1
        total_tribunal_cost += ai_session.cost_usd

    # Judge verdict — capture parsed output so we can persist the verdict
    judge_output, judge_session = await spawn_model(
        task_type=TaskType.TRIBUNAL_JUDGE,
        context={
            "task_id": task.id,
            "tribunal_id": tribunal_id,
            "finding_id": finding_id,
            "budget_remaining": cost_tracker.budget_remaining,
        },
        output_schema=TribunalVerdictOutput,
        branch_id=None,
        db=db,
        cost_tracker=cost_tracker,
        config=config,
    )
    # spawn_model already records cost internally — do NOT double-count
    task.ai_call_counter += 1
    total_tribunal_cost += judge_session.cost_usd

    # BUG-R3-02: Persist the TribunalSession to the DB so that
    # _trigger_for_tribunal() can read the verdict on the next iteration.
    verdict_output: TribunalVerdictOutput = judge_output  # type: ignore[assignment]
    from mariana.data.models import TribunalSession as _TribunalSession  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    tribunal_session_record = _TribunalSession(
        id=tribunal_id,
        task_id=task.id,
        finding_id=finding_id,
        verdict=verdict_output.verdict,
        judge_plaintiff_score=verdict_output.plaintiff_score,
        judge_defendant_score=verdict_output.defendant_score,
        judge_reasoning=verdict_output.verdict_reasoning,
        unanswered_questions=verdict_output.unanswered_questions,
        total_cost_usd=total_tribunal_cost,
    )
    try:
        async with db.acquire() as _tconn:
            async with _tconn.transaction():
                await _tconn.execute(
                    """
                    INSERT INTO tribunal_sessions (
                        id, task_id, finding_id,
                        verdict,
                        judge_plaintiff_score, judge_defendant_score,
                        judge_reasoning, unanswered_questions,
                        total_cost_usd, created_at
                    ) VALUES (
                        $1, $2, $3,
                        $4,
                        $5, $6,
                        $7, $8,
                        $9, NOW()
                    )
                    ON CONFLICT (id) DO NOTHING
                    """,
                    tribunal_session_record.id,
                    tribunal_session_record.task_id,
                    tribunal_session_record.finding_id,
                    tribunal_session_record.verdict.value if tribunal_session_record.verdict else None,
                    tribunal_session_record.judge_plaintiff_score,
                    tribunal_session_record.judge_defendant_score,
                    tribunal_session_record.judge_reasoning,
                    _json.dumps(tribunal_session_record.unanswered_questions, default=str),
                    tribunal_session_record.total_cost_usd,
                )
                # Update finding confidence based on tribunal outcome
                await _tconn.execute(
                    """
                    UPDATE findings
                       SET confidence = $1,
                           metadata   = metadata || $2::jsonb
                     WHERE id = $3
                    """,
                    verdict_output.finding_confidence_after_tribunal,
                    _json.dumps({
                        "tribunal_session_id": tribunal_id,
                        "tribunal_verdict": (
                            verdict_output.verdict.value
                            if verdict_output.verdict else None
                        ),
                        "publication_risk": verdict_output.publication_risk_assessment,
                    }),
                    finding_id,
                )
    except Exception as _exc:  # noqa: BLE001
        log.error(
            "tribunal_persist_failed",
            tribunal_id=tribunal_id,
            error=str(_exc),
        )

    log.info(
        "tribunal_complete",
        tribunal_id=tribunal_id,
        finding_id=finding_id,
        verdict=verdict_output.verdict.value if verdict_output.verdict else None,
    )


async def handle_skeptic(
    task: ResearchTask,
    session_data: ResearchSessionData,
    cost_tracker: CostTracker,
    db: Any,
    redis_client: Any,
    config: Any,
) -> None:
    """Run the Skeptic agent to generate challenging questions.

    Spawns a SKEPTIC_QUESTIONS AI call against the tribunal-confirmed
    finding.
    """
    log = logger.bind(task_id=task.id, handler="skeptic")

    # Fetch the top finding for context (same as tribunal uses)
    _top_finding_row = await db.fetchrow(
        """
        SELECT id FROM findings
        WHERE task_id = $1
        ORDER BY confidence DESC
        LIMIT 1
        """,
        task.id,
    )
    _finding_id: str | None = _top_finding_row["id"] if _top_finding_row else None

    # Fetch the latest tribunal session for linking
    _tribunal_row = await db.fetchrow(
        """
        SELECT id FROM tribunal_sessions
        WHERE task_id = $1
        ORDER BY created_at DESC
        LIMIT 1
        """,
        task.id,
    )
    _tribunal_session_id: str | None = _tribunal_row["id"] if _tribunal_row else None

    skeptic_output, ai_session = await spawn_model(
        task_type=TaskType.SKEPTIC_QUESTIONS,
        context={
            "task_id": task.id,
            "task_topic": task.topic,
            "budget_remaining": cost_tracker.budget_remaining,
        },
        output_schema=SkepticQuestionsOutput,
        branch_id=None,
        db=db,
        cost_tracker=cost_tracker,
        config=config,
    )
    # spawn_model already records cost internally — do NOT double-count
    task.ai_call_counter += 1

    # BUG-R3-03: Persist the SkepticResult so that _trigger_for_skeptic()
    # can read the question counts on the next iteration.
    if _finding_id is not None:
        from mariana.data.models import SkepticResult as _SkepticResult  # noqa: PLC0415
        import json as _json_s  # noqa: PLC0415
        import uuid as _uuid_s  # noqa: PLC0415

        _skeptic_output: SkepticQuestionsOutput = skeptic_output  # type: ignore[assignment]

        # Build SkepticResult — model_validator computes aggregated counts
        _skeptic_result = _SkepticResult(
            id=str(_uuid_s.uuid4()),
            task_id=task.id,
            finding_id=_finding_id,
            tribunal_session_id=_tribunal_session_id,
            questions=_skeptic_output.questions,
            cost_usd=ai_session.cost_usd,
        )

        _questions_json = _json_s.dumps(
            [
                {
                    "number": q.number,
                    "question": q.question,
                    "category": q.category.value,
                    "severity": q.severity.value,
                    "classification": q.classification.value,
                    "resolution_note": q.resolution_note,
                }
                for q in _skeptic_result.questions
            ]
        )

        try:
            async with db.acquire() as _sconn:
                async with _sconn.transaction():
                    await _sconn.execute(
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
                        _skeptic_result.id,
                        _skeptic_result.task_id,
                        _skeptic_result.finding_id,
                        _skeptic_result.tribunal_session_id,
                        _questions_json,
                        _skeptic_result.open_count,
                        _skeptic_result.researchable_count,
                        _skeptic_result.resolved_count,
                        _skeptic_result.critical_open_count,
                        _skeptic_result.passes_publishing_threshold,
                        _skeptic_result.cost_usd,
                    )
        except Exception as _exc:  # noqa: BLE001
            log.error(
                "skeptic_persist_failed",
                error=str(_exc),
            )

    log.info("skeptic_complete", finding_id=_finding_id)


async def handle_report(
    task: ResearchTask,
    session_data: ResearchSessionData,
    cost_tracker: CostTracker,
    db: Any,
    redis_client: Any,
    config: Any,
) -> None:
    """Generate the final research report.

    Fetches confirmed findings and sources from the DB, then delegates to
    :func:`mariana.report.generator.generate_report` which internally runs
    the REPORT_DRAFT and REPORT_FINAL_EDIT AI passes and renders the PDF.
    Output paths are written back to the task record.
    """
    from mariana.report.generator import generate_report  # noqa: PLC0415

    log = logger.bind(task_id=task.id, handler="report")

    # Fetch confirmed / high-confidence findings for the report
    # BUG-047: include metadata column
    finding_rows = await db.fetch(
        """
        SELECT id, task_id, hypothesis_id, content, content_en, content_language,
               source_ids, confidence, evidence_type, is_compressed,
               raw_content_path, created_at, metadata
        FROM findings
        WHERE task_id = $1
        ORDER BY confidence DESC
        """,
        task.id,
    )
    # BUG-014: Use _row_to_dict() to decode JSONB/enum fields before model_validate
    confirmed_findings = [
        Finding.model_validate({**_row_to_dict(r), "evidence_type": EvidenceType(r["evidence_type"])})
        for r in finding_rows
    ]

    # Fetch all sources collected during the investigation
    # BUG-046: include metadata column
    source_rows = await db.fetch(
        """
        SELECT id, task_id, url, url_hash, title, title_en, content_hash,
               fetched_at, cache_expiry, source_type, language, adapter_name,
               is_paywalled, metadata
        FROM sources
        WHERE task_id = $1
        ORDER BY fetched_at ASC
        """,
        task.id,
    )
    # BUG-014: Use _row_to_dict() to decode JSONB/enum fields before model_validate
    all_sources = [
        Source.model_validate({**_row_to_dict(r), "source_type": SourceType(r["source_type"])})
        for r in source_rows
    ]

    # Fetch killed/exhausted hypothesis statements for context
    hyp_rows = await db.fetch(
        """
        SELECT statement FROM hypotheses
        WHERE task_id = $1 AND status IN ('KILLED', 'EXHAUSTED')
        """,
        task.id,
    )
    failed_hypotheses = [r["statement"] for r in hyp_rows]

    # Delegate to generate_report (handles both AI passes + PDF rendering)
    try:
        pdf_path, docx_path = await generate_report(
            task=task,
            confirmed_findings=confirmed_findings,
            all_sources=all_sources,
            failed_hypotheses=failed_hypotheses,
            db=db,
            cost_tracker=cost_tracker,
            report_dir=config.reports_dir,
        )
        task.output_pdf_path = pdf_path
        task.output_docx_path = docx_path
        # generate_report internally calls spawn_model twice; bump counters
        task.ai_call_counter += 2
        log.info("report_compiled", pdf=pdf_path, docx=docx_path)
    except Exception as exc:  # noqa: BLE001
        # BUG-005: Re-raise so the outer handler marks task FAILED
        log.error("report_compile_failed", error=str(exc), exc_info=True)
        task.error_message = f"Report generation failed: {exc}"
        raise

    log.info("report_complete")


# ===========================================================================
# Action executor
# ===========================================================================


async def _execute_action(
    action: Action,
    task: ResearchTask,
    session_data: ResearchSessionData,
    cost_tracker: CostTracker,
    db: Any,
    redis_client: Any,
    config: Any,
    data_root: str,
) -> None:
    """Dispatch an Action to the appropriate handler.

    Parameters
    ----------
    action:
        The action produced by the state machine.
    task, session_data, cost_tracker, db, redis_client, config:
        Runtime dependencies passed through from ``run()``.
    data_root:
        Filesystem root for checkpoint writes.
    """
    log = logger.bind(task_id=task.id, action=action.action_type)

    match action.action_type:
        case "SPAWN_AI":
            task_type_str: str = action.params.get("task_type", "")
            log.debug("spawn_ai_action", task_type=task_type_str)
            # Route to the appropriate handler based on the current state
            match task.current_state:
                case State.INIT:
                    await handle_init(task, session_data, cost_tracker, db, redis_client, config)
                case State.SEARCH:
                    await handle_search(task, session_data, cost_tracker, db, redis_client, config)
                case State.EVALUATE:
                    await handle_evaluate(task, session_data, cost_tracker, db, redis_client, config)
                case State.DEEPEN:
                    await handle_deepen(task, session_data, cost_tracker, db, redis_client, config)
                case State.TRIBUNAL:
                    await handle_tribunal(task, session_data, cost_tracker, db, redis_client, config)
                case State.SKEPTIC:
                    await handle_skeptic(task, session_data, cost_tracker, db, redis_client, config)
                case State.REPORT:
                    await handle_report(task, session_data, cost_tracker, db, redis_client, config)
                case _:
                    log.warning("spawn_ai_unhandled_state", state=task.current_state.value)

        case "DISPATCH_BROWSER":
            log.debug("dispatch_browser_action", params=action.params)
            # Browser dispatch is handled inside spawn_model's EVIDENCE_EXTRACTION
            # path — this action type is a future hook for direct connector calls.

        case "KILL_BRANCH":
            reason = action.params.get("reason", "state_machine_kill")
            active = session_data.active_branches
            if active:
                # Kill the lowest-scoring active branch
                worst = min(
                    active,
                    key=lambda b: b.latest_score if b.latest_score is not None else 0.0,
                )
                await kill_branch(worst.id, reason=reason, db=db)
                log.info("kill_branch_action", branch_id=worst.id, reason=reason)

        case "GRANT_BUDGET":
            score_band = action.params.get("score_band", "score7")
            # BUG-020: score8 → $50 grant, score7 → $20 grant
            amount = 50.0 if score_band == "score8" else 20.0
            active = session_data.active_branches
            if active:
                best = max(
                    active,
                    key=lambda b: b.latest_score if b.latest_score is not None else 0.0,
                )
                await grant_budget(
                    branch_id=best.id,
                    amount=amount,
                    db=db,
                    cost_tracker=cost_tracker,
                )
                log.info("grant_budget_action", branch_id=best.id, amount=amount)

        case "SAVE_CHECKPOINT":
            await handle_checkpoint(
                task=task,
                session_data=session_data,
                cost_tracker=cost_tracker,
                db=db,
                redis_client=redis_client,
                config=config,
                data_root=data_root,
            )

        case "GENERATE_REPORT":
            await handle_report(task, session_data, cost_tracker, db, redis_client, config)

        case "HALT":
            reason = action.params.get("reason", "halt_action")
            log.info("halt_action_executed", reason=reason)
            task.current_state = State.HALT

        case _:
            log.error("unknown_action_type", action_type=action.action_type)


# ===========================================================================
# Internal helpers
# ===========================================================================


async def _build_session_data(
    task: ResearchTask,
    db: Any,
) -> ResearchSessionData:
    """Construct a fresh ResearchSessionData from the current DB state."""
    active_rows = await db.fetch(
        """
        SELECT id, hypothesis_id, task_id, status, score_history,
               budget_allocated, budget_spent, grants_log, cycles_completed,
               kill_reason, sources_searched, created_at, updated_at
        FROM branches WHERE task_id = $1 AND status = 'ACTIVE'
        ORDER BY created_at ASC
        """,
        task.id,
    )
    # BUG-012: Use _row_to_dict() to decode JSONB/enum fields before model_validate
    active_branches = [
        Branch.model_validate({**_row_to_dict(r), "status": BranchStatus(r["status"])})
        for r in active_rows
    ]

    dead_rows = await db.fetch(
        """
        SELECT id, hypothesis_id, task_id, status, score_history,
               budget_allocated, budget_spent, grants_log, cycles_completed,
               kill_reason, sources_searched, created_at, updated_at
        FROM branches WHERE task_id = $1 AND status != 'ACTIVE'
        ORDER BY created_at ASC
        """,
        task.id,
    )
    # BUG-012: Use _row_to_dict() to decode JSONB/enum fields before model_validate
    dead_branches = [
        Branch.model_validate({**_row_to_dict(r), "status": BranchStatus(r["status"])})
        for r in dead_rows
    ]

    # "Recent" findings = last 50 findings for the task
    finding_rows = await db.fetch(
        """
        SELECT id, task_id, hypothesis_id, content, content_en, content_language,
               source_ids, confidence, evidence_type, is_compressed,
               raw_content_path, created_at, metadata
        FROM findings WHERE task_id = $1
        ORDER BY created_at DESC LIMIT 50
        """,
        task.id,
    )
    # BUG-012: Use _row_to_dict() to decode JSONB/enum fields before model_validate
    recent_findings = [
        Finding.model_validate({**_row_to_dict(r), "evidence_type": EvidenceType(r["evidence_type"])})
        for r in finding_rows
    ]

    source_rows = await db.fetch(
        "SELECT id FROM sources WHERE task_id = $1",
        task.id,
    )
    all_source_ids = {str(r["id"]) for r in source_rows}

    return ResearchSessionData(
        task=task,
        active_branches=active_branches,
        dead_branches=dead_branches,
        recent_findings=recent_findings,
        all_source_ids=all_source_ids,
        ai_call_counter=task.ai_call_counter,
        recent_action_summaries=[],
    )


def _sync_cost(task: ResearchTask, cost_tracker: CostTracker) -> None:
    """Sync live cost-tracker totals into the task model before persistence.

    BUG-R3-01 fix: ``task.total_spent_usd`` was never updated from
    ``cost_tracker.total_spent``, causing the DB to always record 0.0.
    ``task.ai_call_counter`` is also synced from the tracker's call_count
    so both fields reflect the latest values at every checkpoint.
    """
    task.total_spent_usd = cost_tracker.total_spent
    task.ai_call_counter = cost_tracker.call_count


async def _persist_task(task: ResearchTask, db: Any) -> None:
    """Upsert the mutable task fields back to the database.

    BUG-C1-02 fix: Added ``metadata`` to the UPDATE so skill/memory/sub-agent
    context set in ``handle_init`` is persisted and survives crash recovery.
    """
    await db.execute(
        """
        UPDATE research_tasks
        SET status = $1,
            current_state = $2,
            total_spent_usd = $3,
            diminishing_flags = $4,
            ai_call_counter = $5,
            started_at = $6,
            completed_at = $7,
            error_message = $8,
            output_pdf_path = $9,
            output_docx_path = $10,
            metadata = $11
        WHERE id = $12
        """,
        task.status.value,
        task.current_state.value,
        task.total_spent_usd,
        task.diminishing_flags,
        task.ai_call_counter,
        task.started_at,
        task.completed_at,
        task.error_message,
        task.output_pdf_path,
        task.output_docx_path,
        json.dumps(task.metadata),
        task.id,
    )


async def _emergency_checkpoint(
    task: ResearchTask,
    cost_tracker: CostTracker,
    db: Any,
    data_root: str,
) -> None:
    """Best-effort checkpoint write during exception handling.

    Uses empty findings / branch lists if the DB is unavailable.
    """
    try:
        active = await get_active_branches(task.id, db)
    except Exception:  # noqa: BLE001
        active = []

    try:
        dead_rows = await db.fetch(
            "SELECT id, hypothesis_id, task_id, status, score_history, "
            "budget_allocated, budget_spent, grants_log, cycles_completed, "
            "kill_reason, sources_searched, created_at, updated_at "
            "FROM branches WHERE task_id = $1 AND status != 'ACTIVE'",
            task.id,
        )
        dead = [
            Branch.model_validate({**_row_to_dict(r), "status": BranchStatus(r["status"])})
            for r in dead_rows
        ]
    except Exception:  # noqa: BLE001
        dead = []

    await checkpoint_module.save_checkpoint(
        task=task,
        active_branches=active,
        killed_branches=dead,
        findings=[],
        current_state=task.current_state,
        cost_tracker=cost_tracker,
        db=db,
        data_root=data_root,
    )


def _best_branch_score(branches: list[Branch]) -> float | None:
    """Return the highest latest score among a list of branches, or None."""
    scores = [b.latest_score for b in branches if b.latest_score is not None]
    return max(scores) if scores else None


async def _check_user_credits(user_id: str, config: Any) -> int | None:
    """Check the user's remaining credit balance via Supabase REST API.

    Returns the token count, or ``None`` if the check could not be performed.
    """
    import httpx as _httpx_check  # noqa: PLC0415

    url = f"{config.SUPABASE_URL}/rest/v1/profiles?id=eq.{user_id}&select=tokens"
    headers = {
        "apikey": config.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_KEY}",
    }
    async with _httpx_check.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        rows = resp.json()
        if not rows:
            return None
        return int(rows[0].get("tokens", 0) or 0)


# BUG-S2-09 fix: hold references to fire-and-forget tasks to prevent GC from
# silently dropping them before they complete.  Python's asyncio will log
# "Task was destroyed but it is pending!" warnings otherwise.
_background_tasks: set[Any] = set()


def _emit_progress(redis_client: Any, task_id: str, event: dict[str, Any]) -> None:
    """Publish a structured progress event to the Redis logs channel.

    Fire-and-forget: errors are logged but never raised, since progress
    events are advisory and must not abort the investigation.
    """
    if redis_client is None:
        return
    import json as _json_emit  # noqa: PLC0415

    try:
        import asyncio as _asyncio_emit  # noqa: PLC0415
        loop = _asyncio_emit.get_running_loop()
        task = loop.create_task(
            redis_client.publish(f"logs:{task_id}", _json_emit.dumps(event))
        )
        # Hold a strong reference until the task completes
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except Exception as exc:
        logger.debug("emit_progress_failed", task_id=task_id, error=str(exc))
