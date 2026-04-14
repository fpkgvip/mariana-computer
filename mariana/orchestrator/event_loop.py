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
import traceback
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from mariana.ai.session import spawn_model
from mariana.data.models import (
    Branch,
    BranchStatus,
    EvidenceExtractionOutput,
    EvaluationOutput,
    Finding,
    HypothesisGenerationOutput,
    ReportDraftOutput,
    ResearchTask,
    SkepticQuestionsOutput,
    State,
    TaskStatus,
    TaskType,
    TribunalArgumentOutput,
    TribunalVerdict,
    TribunalVerdictOutput,
    QuestionClassification,
    QuestionSeverity,
)
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

_SCORE_HIGH_THRESHOLD: float = 7.0   # maps to BRANCH_SCORE_HIGH trigger
_SCORE_MED_THRESHOLD: float = 4.0    # maps to BRANCH_SCORE_MEDIUM trigger
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
    task.started_at = datetime.utcnow()
    await _persist_task(task, db)

    log.info("event_loop_started", budget=task.budget_usd)

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
                next_state, actions = await transition(
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
            await _persist_task(task, db)

            log.info("state_advanced", new_state=next_state.value, iteration=iteration)

            # Allow other coroutines to run (cooperative multitasking)
            await asyncio.sleep(0)

        # -------------------------------------------------------------- #
        # Loop exit
        # -------------------------------------------------------------- #
        if iteration >= _MAX_ITERATIONS:
            log.error("max_iterations_reached", iterations=_MAX_ITERATIONS)
            task.status = TaskStatus.HALTED
        elif task.current_state == State.HALT:
            task.status = TaskStatus.COMPLETED
        
        task.completed_at = datetime.utcnow()
        await _persist_task(task, db)
        log.info(
            "event_loop_finished",
            status=task.status.value,
            iterations=iteration,
            total_spent=cost_tracker.total_spent,
        )

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
        task.completed_at = datetime.utcnow()
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
        task.completed_at = datetime.utcnow()
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

    return TransitionTrigger.STRONG_FINDINGS_EXIST


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
        # No tribunal result yet — re-run evaluation
        logger.warning("no_tribunal_result", task_id=session_data.task.id)
        return TransitionTrigger.TRIBUNAL_WEAKENED

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
        logger.warning("no_skeptic_result", task_id=session_data.task.id)
        return TransitionTrigger.SKEPTIC_CRITICAL_OPEN

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
    """Generate initial hypotheses for the task.

    Calls the AI hypothesis-generation model and persists the resulting
    Hypothesis records, creating one Branch per hypothesis.
    """
    log = logger.bind(task_id=task.id, handler="init")
    log.info("generating_hypotheses")

    parsed_output, ai_session = await spawn_model(
        task_type=TaskType.HYPOTHESIS_GENERATION,
        context={
            "task_id": task.id,
            "topic": task.topic,
            "budget_remaining": cost_tracker.budget_remaining,
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
    for gen_hyp in hypothesis_output.hypotheses:
        hyp = Hypothesis(
            id=str(_uuid.uuid4()),
            task_id=task.id,
            statement=gen_hyp.statement,
            statement_zh=gen_hyp.statement_zh,
            rationale=gen_hyp.rationale,
            status=HypothesisStatus.ACTIVE,
        )
        await db.execute(
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
        await create_branch(
            hypothesis_id=hyp.id,
            task_id=task.id,
            db=db,
        )
        created_hypotheses.append(hyp)

    log.info("hypotheses_ready", count=len(created_hypotheses))


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
    against the current search plan.
    """
    log = logger.bind(task_id=task.id, handler="search")
    active = session_data.active_branches

    log.info("dispatching_search", active_branches=len(active))

    for branch in active:
        if branch.status != BranchStatus.ACTIVE:
            continue

        try:
            _, ai_session = await spawn_model(
                task_type=TaskType.EVIDENCE_EXTRACTION,
                context={
                    "task_id": task.id,
                    "hypothesis_id": branch.hypothesis_id,
                    "hypothesis_statement": branch.hypothesis_id,  # fetched by prompt_builder
                    "page_content": "",
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
            _, ai_session = await spawn_model(
                task_type=TaskType.EVIDENCE_EXTRACTION,
                context={
                    "task_id": task.id,
                    "hypothesis_id": branch.hypothesis_id,
                    "hypothesis_statement": branch.hypothesis_id,
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
        "SELECT id, hypothesis_id, confidence, evidence_type, source_ids, "
        "is_compressed, content, created_at FROM findings WHERE task_id = $1",
        task.id,
    )
    findings = [Finding.model_validate(dict(r)) for r in finding_rows]

    # Count sources for DR analysis
    source_count = await db.fetchval(
        "SELECT COUNT(*) FROM sources WHERE task_id = $1", task.id
    )

    # Run DR check on the best active branch (if any)
    active = session_data.active_branches
    if active:
        best_branch = max(
            active,
            key=lambda b: b.latest_score if b.latest_score is not None else 0.0,
        )
        prev_finding_count = max(0, len(findings) - len(session_data.recent_findings))
        prev_source_count = max(0, int(source_count) - len(session_data.all_source_ids))

        check_diminishing_returns(
            branch=best_branch,
            findings_before=prev_finding_count,
            findings_after=len(findings),
            sources_before=prev_source_count,
            sources_after=int(source_count),
            task=task,
            config=config,
        )

    killed_rows = await db.fetch(
        "SELECT id, hypothesis_id, task_id, status, score_history, "
        "budget_allocated, budget_spent, grants_log, cycles_completed, "
        "kill_reason, sources_searched, created_at, updated_at "
        "FROM branches WHERE task_id = $1 AND status = 'KILLED'",
        task.id,
    )
    killed_branches = [Branch.model_validate(dict(r)) for r in killed_rows]

    checkpoint_id = await checkpoint_module.save_checkpoint(
        task=task,
        active_branches=active,
        killed_branches=killed_branches,
        findings=findings,
        current_state=task.current_state,
        cost_tracker=cost_tracker,
        db=db,
        data_root=data_root,
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

    # Judge verdict
    _, judge_session = await spawn_model(
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

    log.info("tribunal_complete", tribunal_id=tribunal_id, finding_id=finding_id)


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

    _, ai_session = await spawn_model(
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

    log.info("skeptic_complete")


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
    finding_rows = await db.fetch(
        """
        SELECT id, task_id, hypothesis_id, content, content_en, content_language,
               source_ids, confidence, evidence_type, is_compressed,
               raw_content_path, created_at
        FROM findings
        WHERE task_id = $1
        ORDER BY confidence DESC
        """,
        task.id,
    )
    confirmed_findings = [Finding.model_validate(dict(r)) for r in finding_rows]

    # Fetch all sources collected during the investigation
    from mariana.data.models import Source  # noqa: PLC0415
    source_rows = await db.fetch(
        """
        SELECT id, task_id, url, url_hash, title, title_en, content_hash,
               fetched_at, cache_expiry, source_type, language, adapter_name,
               is_paywalled
        FROM sources
        WHERE task_id = $1
        ORDER BY fetched_at ASC
        """,
        task.id,
    )
    all_sources = [Source.model_validate(dict(r)) for r in source_rows]

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
        log.error("report_compile_failed", error=str(exc))

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
            score_band = action.params.get("score_band", "high")
            amount = 20.0 if score_band == "high" else 50.0
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
    active_branches = [Branch.model_validate(dict(r)) for r in active_rows]

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
    dead_branches = [Branch.model_validate(dict(r)) for r in dead_rows]

    # "Recent" findings = last 50 findings for the task
    finding_rows = await db.fetch(
        """
        SELECT id, task_id, hypothesis_id, content, content_en, content_language,
               source_ids, confidence, evidence_type, is_compressed,
               raw_content_path, created_at
        FROM findings WHERE task_id = $1
        ORDER BY created_at DESC LIMIT 50
        """,
        task.id,
    )
    recent_findings = [Finding.model_validate(dict(r)) for r in finding_rows]

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


async def _persist_task(task: ResearchTask, db: Any) -> None:
    """Upsert the mutable task fields back to the database."""
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
            output_docx_path = $10
        WHERE id = $11
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
        dead = [Branch.model_validate(dict(r)) for r in dead_rows]
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
