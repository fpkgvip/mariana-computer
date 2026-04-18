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
    FastPathOutput,
    Finding,
    Hypothesis,
    HypothesisGenerationOutput,
    HypothesisStatus,
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
from mariana.orchestrator import graph_writer
from mariana.data.db import _row_to_dict
from mariana.orchestrator import checkpoint as checkpoint_module
from mariana.orchestrator import rotation
from mariana.orchestrator.rotation import OrchestratorContext
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
# Helper: manual stop check for continuous mode
# ===========================================================================


def _augment_context_from_task(base: dict[str, Any], task: ResearchTask) -> dict[str, Any]:
    """Augment a spawn_model context dict with user-flow fields from task metadata.

    This is the module-level equivalent of the ``_build_context`` closure in
    :func:`run`.  Use it inside handler functions (handle_search, handle_evaluate,
    etc.) which are defined outside ``run`` and cannot access the closure.

    Injects:
    - ``user_flow_instructions`` if present and non-empty in task.metadata.
    - ``quality_tier`` if present and not the default 'balanced'.

    Args:
        base: The base context dict to augment (mutated in place and returned).
        task: The current ResearchTask whose metadata is the source of truth.

    Returns:
        The augmented context dict (same object as ``base``).
    """
    meta = task.metadata or {}
    user_flow_instructions: str = meta.get("user_flow_instructions", "") or ""
    quality_tier: str = meta.get("quality_tier", "balanced") or "balanced"
    learning_context: str = meta.get("learning_context", "") or ""
    if user_flow_instructions:
        base["user_flow_instructions"] = user_flow_instructions
    if quality_tier and quality_tier != "balanced":
        base["quality_tier"] = quality_tier
    if learning_context:
        base["learning_context"] = learning_context
    return base


async def _check_manual_stop(redis_client: Any, task_id: str) -> bool:
    """Check if the user has requested a manual stop for this task.

    Used by continuous mode to decide whether to restart or terminate.
    Reads the Redis key ``stop:{task_id}`` set by the /stop endpoint.

    Args:
        redis_client: aioredis client, or None if Redis is unavailable.
        task_id:      The task to check.

    Returns:
        True if a manual stop has been requested, False otherwise.
    """
    if redis_client is None:
        return False
    try:
        val = await redis_client.get(f"stop:{task_id}")
        return val is not None
    except Exception:  # noqa: BLE001
        return False


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
    # Read user-defined flow control settings from task metadata
    # These are written by the API from StartInvestigationRequest fields.
    # ------------------------------------------------------------------
    _task_meta = task.metadata or {}
    tier = _task_meta.get("tier", "standard")
    quality_tier: str = _task_meta.get("quality_tier", "balanced") or "balanced"
    user_flow_instructions: str = _task_meta.get("user_flow_instructions", "") or ""
    continuous_mode: bool = bool(_task_meta.get("continuous_mode", False))

    # ── Inject uploaded file contents into user_flow_instructions ────────
    # Users can attach .md / .txt files containing custom research methodology,
    # constraints, or instructions.  Files are stored at
    # {DATA_ROOT}/files/{task_id}/ by the API.  We read them here so every
    # AI call in the pipeline sees the user's full intent.
    _files_dir = Path(getattr(config, "DATA_ROOT", "/data/mariana")) / "files" / task.id
    if _files_dir.is_dir():
        _file_parts: list[str] = []
        for _fp in sorted(_files_dir.iterdir()):
            if _fp.is_file() and _fp.suffix.lower() in (".md", ".txt", ".markdown"):
                try:
                    _content = _fp.read_text(encoding="utf-8", errors="replace")[:20_000]  # cap per file
                    _file_parts.append(f"--- Attached file: {_fp.name} ---\n{_content}")
                    log.info("uploaded_file_injected", file=_fp.name, chars=len(_content))
                except Exception as _fexc:
                    log.warning("uploaded_file_read_failed", file=_fp.name, error=str(_fexc))
        if _file_parts:
            _file_block = "\n\n".join(_file_parts)
            user_flow_instructions = (
                (user_flow_instructions + "\n\n" + _file_block)
                if user_flow_instructions
                else _file_block
            )
            # Persist back so _augment_context_from_task also sees it
            if task.metadata is not None:
                task.metadata["user_flow_instructions"] = user_flow_instructions

    # ── Tier-aware quality override ─────────────────────────────────────────
    # Standard tier ($1 budget, 3-5 min target) must use faster/cheaper models.
    # - Economy tier uses DeepSeek/GPT-4o-mini which are 3-5x faster than Sonnet.
    # - Deep tier keeps user's chosen quality (or balanced).
    # - Only override if user hasn't explicitly requested higher quality.
    if tier == "standard" and quality_tier == "balanced":
        quality_tier = "economy"
        log.info("quality_tier_downgraded_for_standard", original="balanced", new="economy")
        # Persist so spawn_model picks it up via metadata
        if task.metadata:
            task.metadata["quality_tier"] = quality_tier
    # dont_kill_branches is threaded via ResearchSessionData (built per iteration)

    if user_flow_instructions:
        log.info(
            "user_flow_instructions_active",
            length=len(user_flow_instructions),
        )
    if continuous_mode:
        log.info("continuous_mode_active", task_id=task.id)

    def _build_context(base: dict[str, Any]) -> dict[str, Any]:
        """Augment a spawn_model context dict with user flow instructions.

        Defined as a closure over ``user_flow_instructions``,
        ``quality_tier``, and task metadata so every spawn_model call
        inside run() picks them up automatically.

        For handlers that are standalone module-level functions, use
        :func:`_augment_context_from_task` instead.
        """
        if user_flow_instructions:
            base["user_flow_instructions"] = user_flow_instructions
        if quality_tier and quality_tier != "balanced":
            base["quality_tier"] = quality_tier
        # Inject learning context if available in task metadata
        _lc = (task.metadata or {}).get("learning_context", "")
        if _lc:
            base["learning_context"] = _lc
        return base

    # ------------------------------------------------------------------
    # Fast path for instant / quick tiers
    # ------------------------------------------------------------------
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
                "Answer the user's question directly and concisely. "
                "If the user is just greeting or testing, respond warmly and briefly. "
                "If the user asks a factual question, answer it with citations where possible. "
                "Provide your response in the 'answer' field as plain text or markdown. "
                "Never refuse. Always be helpful."
            )
            fast_output, fast_session = await _fast_spawn(
                task_type=TaskType.FAST_PATH,
                context=_build_context({
                    "task_id": task.id,
                    "topic": task.topic,
                    "budget_remaining": cost_tracker.budget_remaining,
                    "system_override": system_prompt,
                }),
                output_schema=FastPathOutput,
                branch_id=None,
                db=db,
                cost_tracker=cost_tracker,
                config=config,
                quality_tier=(task.metadata or {}).get("quality_tier"),
            )
            task.ai_call_counter += 1

            # Extract the answer text from the lightweight FastPathOutput
            answer_text = fast_output.answer if hasattr(fast_output, "answer") else str(fast_output)

            _emit_progress(redis_client, task.id, {
                "type": "text",
                "content": answer_text,
            })
            _emit_progress(redis_client, task.id, {
                "type": "status_change",
                "state": "HALT",
                "message": "Complete.",
            })
            # BUG-D6-01: Persist the fast-path answer in task metadata so it can
            # be replayed by the SSE endpoint if the frontend connects after the
            # pub/sub events have already been emitted and lost.
            if task.metadata is None:
                task.metadata = {}
            task.metadata["fast_path_answer"] = answer_text
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

            # --------------------------------------------------------- #
            # 0.1  External kill check (every 5 iterations)
            # --------------------------------------------------------- #
            # BUG-D1-01 fix: The kill_investigation API sets status='HALTED'
            # in the DB.  We poll for that every 5 iterations so the user's
            # Stop button actually halts the investigation.
            if iteration > 0 and iteration % 5 == 0:
                try:
                    _db_status = await db.fetchval(
                        "SELECT status FROM research_tasks WHERE id = $1",
                        task.id,
                    )
                    if _db_status is None:
                        # Task was deleted from the DB (user deleted investigation)
                        log.info("task_deleted_externally", task_id=task.id)
                        task.status = TaskStatus.FAILED
                        task.current_state = State.HALT
                        task.error_message = "Investigation deleted by user"
                        break
                    if _db_status in ("HALTED", "FAILED"):
                        log.info("external_kill_detected", task_id=task.id, db_status=_db_status)
                        task.status = TaskStatus.HALTED if _db_status == "HALTED" else TaskStatus.FAILED
                        task.current_state = State.HALT
                        _emit_progress(redis_client, task.id, {
                            "type": "text",
                            "content": "Investigation stopped by user.",
                        })
                        break
                except Exception as _kill_exc:  # noqa: BLE001
                    log.debug("kill_check_failed", error=str(_kill_exc))

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
                _sb_key = getattr(config, "SUPABASE_SERVICE_KEY", "") or getattr(config, "SUPABASE_ANON_KEY", "")
                if user_id and getattr(config, "SUPABASE_URL", "") and _sb_key:
                    try:
                        remaining_tokens = await _check_user_credits(user_id, config)
                        # Account for credits already reserved for this task;
                        # the reservation was deducted up-front at submission.
                        reserved = int((task.metadata or {}).get("reserved_credits", 0))
                        effective_balance = (remaining_tokens or 0) + reserved
                        if remaining_tokens is not None and effective_balance <= 0:
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
            # Capture pre-action state before HALT action can modify it
            _pre_action_state = task.current_state
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
            # Orchestrator rotation: capture handoff context on every state
            # change so the next fresh LLM call has full situational awareness
            # without depending on conversation history.
            # Use _pre_action_state (not task.current_state) because HALT
            # action may have prematurely set task.current_state = HALT.
            _prev_state = _pre_action_state
            task.current_state = next_state
            if next_state != _prev_state:
                await _write_handoff_context(
                    task=task,
                    cost_tracker=cost_tracker,
                    phase_name=_prev_state.value,
                    db=db,
                )
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
                # Use total_with_markup (raw cost × 1.20) so the frontend shows
                # the credit-equivalent amount the user is actually charged.
                "spent_usd": round(cost_tracker.total_with_markup, 4),
                "budget_usd": cost_tracker.task_budget,
                "raw_spent_usd": round(cost_tracker.total_spent, 4),
            })

            # --------------------------------------------------------- #
            # 5b. Intelligence hooks triggered on state transitions
            # --------------------------------------------------------- #
            # after_evaluate fires when leaving EVALUATE (i.e. evaluation
            # phase is complete and we're moving to CHECKPOINT/DEEPEN/etc.)
            if _prev_state == State.EVALUATE and next_state != State.EVALUATE:
                try:
                    from mariana.orchestrator.intelligence.engine import after_evaluate as _intel_after_eval_hook  # noqa: PLC0415
                    _intel_eval_result = await _intel_after_eval_hook(
                        task_id=task.id,
                        topic=task.topic,
                        evaluation_cycle=iteration,
                        db=db,
                        cost_tracker=cost_tracker,
                        config=config,
                        tier=tier,
                    )
                    log.info("intelligence_after_evaluate_complete_hook", **{
                        k: v for k, v in _intel_eval_result.items()
                        if not isinstance(v, (dict, list))
                    })
                except Exception as _intel_exc:
                    log.warning("intelligence_after_evaluate_hook_failed", error=str(_intel_exc))

            # before_report fires when entering REPORT state
            # NOTE: handle_report() internally calls before_report via the
            # intelligence engine, so we only enable finalization_mode here
            # to ensure the budget isn't blocking.  We do NOT call
            # before_report again to avoid duplicate executive-summary work.
            if next_state == State.REPORT and _prev_state != State.REPORT:
                cost_tracker.finalization_mode = True
                log.info("finalization_mode_enabled_for_report")

            # before_halt: when entering HALT, run after_evaluate + before_report
            # to ensure intelligence data is always generated (even without REPORT phase)
            if next_state == State.HALT and _prev_state != State.HALT:
                # Enable finalization mode so post-investigation intelligence
                # hooks can run even if the main loop exhausted the budget.
                cost_tracker.finalization_mode = True
                # Run after_evaluate if it wasn't already run
                if _prev_state != State.EVALUATE:
                    try:
                        from mariana.orchestrator.intelligence.engine import after_evaluate as _intel_halt_eval  # noqa: PLC0415
                        _halt_eval = await _intel_halt_eval(
                            task_id=task.id,
                            topic=task.topic,
                            evaluation_cycle=iteration,
                            db=db,
                            cost_tracker=cost_tracker,
                            config=config,
                            tier=tier,
                        )
                        log.info("intelligence_after_evaluate_on_halt", **{
                            k: v for k, v in _halt_eval.items()
                            if not isinstance(v, (dict, list))
                        })
                    except Exception as _intel_exc:
                        log.warning("intelligence_after_evaluate_on_halt_failed", error=str(_intel_exc))

                # Run before_report on halt ONLY if we didn't come from
                # REPORT state (handle_report already called before_report).
                # BUG-AUD-15 fix: Also skip when there are zero findings — no point
                # running perspective synthesis + exec summary on nothing.
                _halt_finding_count = await db.fetchval(
                    "SELECT COUNT(*) FROM findings WHERE task_id = $1", task.id
                )
                if _prev_state != State.REPORT and _halt_finding_count > 0:
                    try:
                        from mariana.orchestrator.intelligence.engine import before_report as _intel_halt_report  # noqa: PLC0415
                        _halt_report = await _intel_halt_report(
                            task_id=task.id,
                            topic=task.topic,
                            db=db,
                            cost_tracker=cost_tracker,
                            config=config,
                            quality_tier=(task.metadata or {}).get("quality_tier"),
                            tier=tier,
                        )
                        log.info("intelligence_before_report_on_halt", **{
                            k: v for k, v in _halt_report.items()
                            if not isinstance(v, (dict, list)) and k != "one_liner"
                        })
                    except Exception as _intel_exc:
                        log.warning("intelligence_before_report_on_halt_failed", error=str(_intel_exc))
                elif _prev_state == State.REPORT:
                    log.info("before_report_on_halt_skipped", reason="already_ran_in_handle_report")
                else:
                    log.info("before_report_on_halt_skipped", reason="zero_findings")

            # --------------------------------------------------------- #
            # 6. Continuous mode: restart instead of halting
            # --------------------------------------------------------- #
            if (
                task.current_state == State.HALT
                and continuous_mode
                and task.status not in (TaskStatus.HALTED, TaskStatus.FAILED)
                and not cost_tracker.is_exhausted
            ):
                # Check whether the user has explicitly requested a stop
                _manual_stop = await _check_manual_stop(redis_client, task.id)
                if not _manual_stop and not (shutdown_flag is not None and shutdown_flag.is_set()):
                    log.info(
                        "continuous_mode_restarting",
                        task_id=task.id,
                        iteration=iteration,
                        total_spent=cost_tracker.total_spent,
                    )
                    _emit_progress(redis_client, task.id, {
                        "type": "status_change",
                        "state": "INIT",
                        "message": "Continuous mode: restarting research loop from INIT...",
                    })
                    # BUG-D1-04 fix: reset to INIT so fresh hypotheses and architecture
                    # are generated on each continuous-mode pass.  Resetting to SEARCH
                    # skipped handle_init entirely, leaving stale hypotheses from the
                    # previous cycle as the sole research targets indefinitely.
                    # BUG-R5-03 fix: tag the restart so handle_init() retires prior
                    # ACTIVE branches / hypotheses before generating a fresh cycle.
                    task.metadata = {**(task.metadata or {}), "_init_reset_mode": "continuous_restart"}
                    task.current_state = State.INIT
                    task.status = TaskStatus.RUNNING
                    iteration = 0  # reset iteration counter
                    # BUG-AUD-21 fix: Reset finalization_mode so budget checks
                    # are enforced again on the new continuous-mode cycle.
                    cost_tracker.finalization_mode = False
                    _sync_cost(task, cost_tracker)
                    await _persist_task(task, db)
                    await asyncio.sleep(0)
                    continue
                else:
                    log.info(
                        "continuous_mode_stopped",
                        task_id=task.id,
                        manual_stop=_manual_stop,
                    )

            # Allow other coroutines to run (cooperative multitasking)
            await asyncio.sleep(0)

        # -------------------------------------------------------------- #
        # Loop exit
        # -------------------------------------------------------------- #

        # BUG-ZBA-02 safety net: If we're at HALT without a PDF and the task
        # has findings, force report generation now.  This catches any code
        # path (budget exhaustion, max iterations, unknown triggers) that
        # skipped the REPORT state despite having research output.
        # BUG-AUD-03 fix: Also run safety net for HALTED tasks (e.g. user pressed
        # Stop). Only skip for FAILED, which indicates an unrecoverable error.
        if (
            task.current_state == State.HALT
            and not task.output_pdf_path
            and task.status != TaskStatus.FAILED
        ):
            _finding_count = await db.fetchval(
                "SELECT COUNT(*) FROM findings WHERE task_id = $1",
                task.id,
            )
            if _finding_count and _finding_count > 0:
                log.warning(
                    "safety_net_forcing_report",
                    task_id=task.id,
                    finding_count=_finding_count,
                    reason="halt_without_pdf",
                )
                cost_tracker.finalization_mode = True
                try:
                    session_data = await _build_session_data(task, db)
                    await handle_report(task, session_data, cost_tracker, db, redis_client, config)
                    log.info("safety_net_report_generated", task_id=task.id, pdf=task.output_pdf_path)
                except Exception as _report_exc:
                    log.error("safety_net_report_failed", error=str(_report_exc))

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

        # ── Learning Loop: Record investigation outcome ────────────────────
        _learn_user_id = (task.metadata or {}).get("user_id", "")
        if _learn_user_id and task.status in (TaskStatus.COMPLETED, TaskStatus.HALTED):
            try:
                from mariana.orchestrator.learning import record_investigation_outcome  # noqa: PLC0415
                _duration = 0
                if task.started_at and task.completed_at:
                    _duration = int((task.completed_at - task.started_at).total_seconds())
                await record_investigation_outcome(
                    task_id=task.id,
                    user_id=_learn_user_id,
                    topic=task.topic,
                    quality_tier=(task.metadata or {}).get("quality_tier"),
                    total_cost_usd=cost_tracker.total_spent,
                    total_ai_calls=cost_tracker.call_count,
                    duration_seconds=_duration,
                    final_state=task.current_state.value,
                    report_generated=bool(task.output_pdf_path),
                    db=db,
                )
                log.info("learning_outcome_recorded", user_id=_learn_user_id)
            except Exception as _learn_exc:
                log.debug("learning_outcome_save_skipped", error=str(_learn_exc))

    except BudgetExhaustedError as exc:
        log.warning(
            "budget_exhausted_caught",
            scope=exc.scope,
            spent=exc.spent,
            cap=exc.cap,
        )
        await _emergency_checkpoint(task, cost_tracker, db, data_root)

        # BUG-ZBA-02b: Even on budget exhaustion, try to produce a report
        # if findings exist.  The user's credits are spent — they deserve output.
        if not task.output_pdf_path:
            try:
                _finding_count = await db.fetchval(
                    "SELECT COUNT(*) FROM findings WHERE task_id = $1", task.id,
                )
                if _finding_count and _finding_count > 0:
                    log.info("budget_exhausted_forcing_report", finding_count=_finding_count)
                    cost_tracker.finalization_mode = True
                    session_data = await _build_session_data(task, db)
                    await handle_report(task, session_data, cost_tracker, db, redis_client, config)
            except Exception as _report_exc:
                log.error("budget_exhausted_report_failed", error=str(_report_exc))

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

    # BUG-AUD-05 fix: Check not-active BEFORE DR flags. Previously, nonzero
    # diminishing_flags with zero active branches could route to SEARCH with
    # nothing to search, creating a no-op loop until _MAX_ITERATIONS.
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

    # BUG-R5-03 fix: pivots and continuous-mode restarts intentionally enter
    # handle_init() again to generate a fresh research cycle.  Without retiring
    # the prior ACTIVE branches / hypotheses first, every restart silently
    # accumulates duplicate root hypotheses and duplicate active branches.
    _init_reset_mode = (task.metadata or {}).pop("_init_reset_mode", None)
    _should_reset_existing = (
        task.current_state == State.PIVOT
        or _init_reset_mode == "continuous_restart"
    )
    if _should_reset_existing:
        _reset_reason = (
            "pivot_refresh"
            if task.current_state == State.PIVOT
            else "continuous_restart_refresh"
        )
        async with db.acquire() as _reset_conn:
            async with _reset_conn.transaction():
                await _reset_conn.execute(
                    """
                    UPDATE branches
                       SET status = $1,
                           kill_reason = COALESCE(kill_reason, $2),
                           updated_at = NOW()
                     WHERE task_id = $3 AND status = 'ACTIVE'
                    """,
                    BranchStatus.EXHAUSTED.value,
                    _reset_reason,
                    task.id,
                )
                await _reset_conn.execute(
                    """
                    UPDATE hypotheses
                       SET status = $1,
                           updated_at = NOW()
                     WHERE task_id = $2 AND status = 'ACTIVE'
                    """,
                    HypothesisStatus.EXHAUSTED.value,
                    task.id,
                )
        log.info("init_reset_existing_cycle", mode=_reset_reason)

    # ── Rotation handoff: inject prior orchestrator context into this fresh ──
    # context window so a newly rotated orchestrator has full situational
    # awareness without access to the previous conversation history.
    # BUG-D2-03 fix: read_handoff / build_rotation_prompt were never called
    # on the reading side — handoffs were written but never consumed.
    _prior_ctx = await rotation.read_handoff(db, task.id, "INIT")
    _rotation_injection: str = ""
    if _prior_ctx:
        from mariana.orchestrator.rotation import build_rotation_prompt  # noqa: PLC0415
        _rotation_injection = build_rotation_prompt(_prior_ctx)
        log.info(
            "rotation_handoff_loaded",
            prior_phase=_prior_ctx.phase,
            findings=len(_prior_ctx.key_findings),
        )

    # ── Step 1: Research Architecture ─────────────────────────────────────
    log.info("generating_research_architecture")
    _emit_progress(redis_client, task.id, {
        "type": "status_change",
        "state": "INIT",
        "message": "Analyzing topic and designing research architecture...",
    })

    _arch_context: dict[str, Any] = {
        "task_id": task.id,
        "topic": task.topic,
        "budget_remaining": cost_tracker.budget_remaining,
        "budget_usd": task.budget_usd,
    }
    if _rotation_injection:
        _arch_context["rotation_handoff"] = _rotation_injection

    arch_output, arch_session = await spawn_model(
        task_type=TaskType.RESEARCH_ARCHITECTURE,
        context=_augment_context_from_task(_arch_context, task),
        output_schema=ResearchArchitectureOutput,
        branch_id=None,
        db=db,
        cost_tracker=cost_tracker,
        config=config,
        quality_tier=(task.metadata or {}).get("quality_tier"),
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
        context=_augment_context_from_task({
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
        }, task),
        output_schema=HypothesisGenerationOutput,
        branch_id=None,
        db=db,
        cost_tracker=cost_tracker,
        config=config,
        quality_tier=(task.metadata or {}).get("quality_tier"),
    )
    # spawn_model already records cost internally via _record_cost — do NOT
    # call cost_tracker.record_call() again here (would double-count).
    task.ai_call_counter += 1

    # Use the parsed HypothesisGenerationOutput to persist hypotheses and
    # create branches.  spawn_model does NOT write hypotheses to the DB —
    # that is the orchestrator's responsibility.
    hypothesis_output: HypothesisGenerationOutput = parsed_output  # type: ignore[assignment]
    import uuid as _uuid  # noqa: PLC0415
    created_hypotheses = []

    # ── Tier-aware hypothesis cap ─────────────────────────────────────────
    # Standard tier: cap at 3 branches to keep research phase under 3 min.
    # Deep tier: use all generated hypotheses (typically 4-6).
    _tier = (task.metadata or {}).get("tier", "standard")
    _hyp_cap = {"instant": 1, "quick": 1, "standard": 3, "deep": 10}.get(_tier, 3)
    _all_hyps = hypothesis_output.hypotheses[:_hyp_cap]
    if len(hypothesis_output.hypotheses) > _hyp_cap:
        log.info(
            "hypothesis_cap_applied",
            tier=_tier,
            generated=len(hypothesis_output.hypotheses),
            capped_to=_hyp_cap,
        )

    # BUG-023: Wrap hypothesis + branch insertion in a transaction to avoid partial state
    async with db.acquire() as _conn:
        async with _conn.transaction():
            for gen_hyp in _all_hyps:
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

    # ── Write hypotheses + branches to investigation graph ───────────────
    for hyp in created_hypotheses:
        await graph_writer.add_hypothesis_node(db, task.id, hyp, redis_client)
    # Fetch branches we just created so we can write them to graph
    _new_branches = await db.fetch(
        "SELECT * FROM branches WHERE task_id = $1 AND status = 'ACTIVE'", task.id
    )
    for _br_row in _new_branches:
        _br_dict = _row_to_dict(_br_row)
        _br_obj = Branch(
            id=_br_dict["id"],
            hypothesis_id=_br_dict["hypothesis_id"],
            task_id=_br_dict["task_id"],
            status=BranchStatus(_br_dict["status"]),
            score_history=_br_dict.get("score_history", []),
            budget_allocated=float(_br_dict.get("budget_allocated", 5.0)),
            budget_spent=float(_br_dict.get("budget_spent", 0.0)),
            cycles_completed=int(_br_dict.get("cycles_completed", 0)),
        )
        await graph_writer.add_branch_node(db, task.id, _br_obj, redis_client)
        # Edge: branch → hypothesis
        await graph_writer.add_evidence_edge(
            db, task.id, _br_obj.id, _br_obj.hypothesis_id, "EXPLORES", redis_client
        )

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

    # ── Learning Loop: inject learning context ──────────────────────────
    if user_id:
        try:
            from mariana.orchestrator.learning import build_learning_context  # noqa: PLC0415
            _learning_ctx = await build_learning_context(user_id, db)
            if _learning_ctx:
                task.metadata = {**(task.metadata or {}), "learning_context": _learning_ctx}
                log.info("learning_context_injected", user_id=user_id, length=len(_learning_ctx))
        except Exception as exc:
            log.debug("learning_context_skipped", error=str(exc))

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

    # ── Intelligence Engine: Initialize hypothesis priors for Bayesian updates ─
    try:
        from mariana.orchestrator.intelligence.hypothesis_engine import initialize_priors  # noqa: PLC0415
        _prior_map = await initialize_priors(
            task_id=task.id,
            hypothesis_ids=[h.id for h in created_hypotheses],
            db=db,
        )
        log.info("intelligence_priors_initialized", count=len(_prior_map))
    except Exception as exc:
        log.debug("intelligence_priors_init_skipped", error=str(exc))


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

    # ── Parallel evidence extraction across all active branches ─────────
    # Running all branches concurrently saves ~2x wall-clock time for standard
    # tier (from ~2.5 min sequential to ~50s parallel with 3 branches).
    _budget_exhausted = False

    async def _extract_branch(branch: Branch) -> None:
        nonlocal _budget_exhausted
        try:
            _hyp_row = await db.fetchrow(
                "SELECT statement FROM hypotheses WHERE id = $1",
                branch.hypothesis_id,
            )
            _hyp_statement = _hyp_row["statement"] if _hyp_row else ""

            page_content = perplexity_context.get(branch.id, "")

            extraction_output, ai_session = await spawn_model(
                task_type=TaskType.EVIDENCE_EXTRACTION,
                context=_augment_context_from_task({
                    "task_id": task.id,
                    "hypothesis_id": branch.hypothesis_id,
                    "hypothesis_statement": _hyp_statement,
                    "page_content": page_content,
                    "source_url": "",
                    "sources_already_searched": branch.sources_searched,
                    "budget_remaining": cost_tracker.budget_remaining,
                }, task),
                output_schema=EvidenceExtractionOutput,
                branch_id=branch.id,
                db=db,
                cost_tracker=cost_tracker,
                config=config,
                quality_tier=(task.metadata or {}).get("quality_tier"),
            )
            task.ai_call_counter += 1

            _evidence_out: EvidenceExtractionOutput = extraction_output  # type: ignore[assignment]
            for _item in _evidence_out.evidence_items:
                _finding = Finding(
                    id=str(uuid.uuid4()),
                    task_id=task.id,
                    hypothesis_id=branch.hypothesis_id,
                    content=_item.content,
                    content_language=_evidence_out.language_detected or "en",
                    confidence=_item.confidence,
                    evidence_type=_item.evidence_type,
                    source_ids=[],
                    metadata={
                        "quote": _item.quote,
                        "data_point": _item.data_point,
                        "relevance_explanation": _item.relevance_explanation,
                        "page_relevance_score": _evidence_out.page_relevance_score,
                        "red_flags": _evidence_out.red_flags,
                    },
                )
                try:
                    async with db.acquire() as _fconn:
                        await _fconn.execute(
                            """
                            INSERT INTO findings (
                                id, task_id, hypothesis_id, content,
                                content_en, content_language,
                                source_ids, confidence, evidence_type,
                                is_compressed, raw_content_path,
                                created_at, metadata
                            ) VALUES (
                                $1, $2, $3, $4,
                                $5, $6,
                                $7, $8, $9,
                                $10, $11,
                                NOW(), $12
                            )
                            ON CONFLICT (id) DO NOTHING
                            """,
                            _finding.id,
                            _finding.task_id,
                            _finding.hypothesis_id,
                            _finding.content,
                            _finding.content_en,
                            _finding.content_language,
                            json.dumps(_finding.source_ids),
                            _finding.confidence,
                            _finding.evidence_type.value,
                            _finding.is_compressed,
                            _finding.raw_content_path,
                            json.dumps(_finding.metadata),
                        )
                    try:
                        await graph_writer.add_finding_node(db, task.id, _finding, redis_client)
                        await graph_writer.add_evidence_edge(
                            db, task.id, _finding.id, _finding.hypothesis_id,
                            _finding.evidence_type.value, redis_client,
                        )
                    except Exception:  # noqa: BLE001
                        pass  # graph writes are fire-and-forget
                except Exception as _fexc:  # noqa: BLE001
                    log.warning(
                        "finding_persist_failed",
                        branch_id=branch.id,
                        error=str(_fexc),
                    )

            log.info(
                "evidence_extracted",
                branch_id=branch.id,
                items_saved=len(_evidence_out.evidence_items),
                page_relevance=_evidence_out.page_relevance_score,
            )

        except BudgetExhaustedError:
            _budget_exhausted = True
            log.warning("search_budget_exhausted", branch_id=branch.id)

    _active_branches = [b for b in active if b.status == BranchStatus.ACTIVE]
    if _active_branches:
        await asyncio.gather(*[_extract_branch(b) for b in _active_branches])
    if _budget_exhausted:
        raise BudgetExhaustedError(
            scope="task",
            spent=cost_tracker.total_spent,
            cap=cost_tracker.task_budget,
        )

    log.info("search_batch_complete", branches_searched=len(_active_branches))

    # ── Intelligence Engine: after_search hook ────────────────────────────
    # Collect all newly persisted findings + sources for intelligence processing.
    # This runs claim extraction, source credibility, contradiction detection,
    # and Bayesian updates on the newly gathered evidence.
    try:
        from mariana.orchestrator.intelligence.engine import after_search as _intel_after_search  # noqa: PLC0415

        # Fetch findings created in the last 5 minutes (this search pass)
        _recent_findings_rows = await db.fetch(
            """
            SELECT id, task_id, hypothesis_id, content, source_ids, confidence
            FROM findings
            WHERE task_id = $1 AND created_at >= NOW() - INTERVAL '5 minutes'
            ORDER BY created_at DESC
            LIMIT 50
            """,
            task.id,
        )
        _recent_findings = [
            {
                "id": r["id"],
                "hypothesis_id": r["hypothesis_id"],
                "content": r["content"],
                "source_ids": json.loads(r["source_ids"]) if isinstance(r["source_ids"], str) else (r["source_ids"] or []),
            }
            for r in _recent_findings_rows
        ]

        # Fetch sources used in this task
        _recent_source_rows = await db.fetch(
            """
            SELECT id, url, title, fetched_at
            FROM sources
            WHERE task_id = $1
            ORDER BY fetched_at DESC
            LIMIT 50
            """,
            task.id,
        )
        _recent_sources = [
            {
                "id": r["id"],
                "url": r["url"],
                "title": r["title"],
                "fetched_at": r["fetched_at"],
            }
            for r in _recent_source_rows
        ]

        if _recent_findings:
            _intel_result = await _intel_after_search(
                task_id=task.id,
                topic=task.topic,
                findings=_recent_findings,
                sources=_recent_sources,
                db=db,
                cost_tracker=cost_tracker,
                config=config,
                quality_tier=(task.metadata or {}).get("quality_tier"),
                tier=(task.metadata or {}).get("tier", "standard"),
            )
            log.info("intelligence_after_search_complete", **{
                k: v for k, v in _intel_result.items() if not isinstance(v, (dict, list))
            })
    except Exception as _intel_exc:
        log.warning("intelligence_after_search_failed", error=str(_intel_exc))


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

            # BUG-R15-02: Fetch actual findings for this branch's hypothesis
            _findings_rows = await db.fetch(
                """
                SELECT content, confidence, evidence_type
                FROM findings
                WHERE task_id = $1 AND hypothesis_id = $2
                ORDER BY confidence DESC
                LIMIT 20
                """,
                task.id, branch.hypothesis_id,
            )
            _compressed_findings = "\n---\n".join(
                f"[{r['evidence_type']}] confidence={r['confidence']:.2f}\n{r['content'][:500]}"
                for r in _findings_rows
            ) or "(no findings yet)"

            eval_output, ai_session = await spawn_model(
                task_type=TaskType.EVALUATION,
                context=_augment_context_from_task({
                    "task_id": task.id,
                    "hypothesis_id": branch.hypothesis_id,
                    "hypothesis_statement": hyp_statement,
                    "compressed_findings": _compressed_findings,
                    "sources_searched": sources_searched_count,
                    "prior_scores": branch.score_history,
                    "budget_remaining": cost_tracker.budget_remaining,
                }, task),
                output_schema=EvaluationOutput,
                branch_id=branch.id,
                db=db,
                cost_tracker=cost_tracker,
                config=config,
                quality_tier=(task.metadata or {}).get("quality_tier"),
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

            # Update hypothesis graph node with latest score
            try:
                _hyp_for_graph = Hypothesis(
                    id=branch.hypothesis_id,
                    task_id=task.id,
                    statement=hyp_statement,
                    status=HypothesisStatus.ACTIVE,
                    score=new_score,
                )
                await graph_writer.add_hypothesis_node(db, task.id, _hyp_for_graph, redis_client)
            except Exception:  # noqa: BLE001
                pass  # graph writes are fire-and-forget

        except BudgetExhaustedError:
            log.warning("evaluate_budget_exhausted", branch_id=branch.id)
            raise

    # ── Intelligence Engine: after_evaluate hook ─────────────────────────
    # Runs confidence calibration, source diversity assessment, gap detection,
    # and adaptive replanning after all branches have been evaluated.
    try:
        from mariana.orchestrator.intelligence.engine import after_evaluate as _intel_after_evaluate  # noqa: PLC0415

        # Determine evaluation cycle from first branch's cycle count
        _eval_cycle = max(
            (b.cycles_completed for b in active if b.status == BranchStatus.ACTIVE),
            default=1,
        )

        _intel_eval_result = await _intel_after_evaluate(
            task_id=task.id,
            topic=task.topic,
            evaluation_cycle=_eval_cycle,
            db=db,
            cost_tracker=cost_tracker,
            config=config,
            quality_tier=(task.metadata or {}).get("quality_tier"),
        )
        log.info("intelligence_after_evaluate_complete", **{
            k: v for k, v in _intel_eval_result.items()
            if not isinstance(v, (dict, list))
        })

        # If replanning produced follow-up queries, store them in task metadata
        # for the next search cycle to pick up
        _follow_ups = _intel_eval_result.get("follow_up_queries", [])
        if _follow_ups:
            _meta = task.metadata or {}
            _meta["intelligence_follow_up_queries"] = _follow_ups[:5]
            task.metadata = _meta
    except Exception as _intel_exc:
        log.warning("intelligence_after_evaluate_failed", error=str(_intel_exc))


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

            deepen_output, ai_session = await spawn_model(
                task_type=TaskType.EVIDENCE_EXTRACTION,
                context=_augment_context_from_task({
                    "task_id": task.id,
                    "hypothesis_id": branch.hypothesis_id,
                    "hypothesis_statement": _hyp_statement_d,
                    "page_content": "",
                    "source_url": "",
                    "mode": "DEEPEN",
                    "sources_already_searched": branch.sources_searched,
                    "prior_score": branch.latest_score,
                    "budget_remaining": cost_tracker.budget_remaining,
                }, task),
                output_schema=EvidenceExtractionOutput,
                branch_id=branch.id,
                db=db,
                cost_tracker=cost_tracker,
                config=config,
                quality_tier=(task.metadata or {}).get("quality_tier"),
            )
            # spawn_model already records cost internally — do NOT double-count
            task.ai_call_counter += 1

            # BUG-R3-01 fix (deepen path): persist deepened evidence items as Finding records.
            # Same pattern as handle_search — output was previously discarded with `_`.
            _deepen_out: EvidenceExtractionOutput = deepen_output  # type: ignore[assignment]
            for _ditem in _deepen_out.evidence_items:
                _dfinding = Finding(
                    id=str(uuid.uuid4()),
                    task_id=task.id,
                    hypothesis_id=branch.hypothesis_id,
                    content=_ditem.content,
                    content_language=_deepen_out.language_detected or "en",
                    confidence=_ditem.confidence,
                    evidence_type=_ditem.evidence_type,
                    source_ids=[],
                    metadata={
                        "quote": _ditem.quote,
                        "data_point": _ditem.data_point,
                        "relevance_explanation": _ditem.relevance_explanation,
                        "page_relevance_score": _deepen_out.page_relevance_score,
                        "red_flags": _deepen_out.red_flags,
                        "mode": "DEEPEN",
                    },
                )
                try:
                    async with db.acquire() as _dfconn:
                        await _dfconn.execute(
                            """
                            INSERT INTO findings (
                                id, task_id, hypothesis_id, content,
                                content_en, content_language,
                                source_ids, confidence, evidence_type,
                                is_compressed, raw_content_path,
                                created_at, metadata
                            ) VALUES (
                                $1, $2, $3, $4,
                                $5, $6,
                                $7, $8, $9,
                                $10, $11,
                                NOW(), $12
                            )
                            ON CONFLICT (id) DO NOTHING
                            """,
                            _dfinding.id,
                            _dfinding.task_id,
                            _dfinding.hypothesis_id,
                            _dfinding.content,
                            _dfinding.content_en,
                            _dfinding.content_language,
                            json.dumps(_dfinding.source_ids),
                            _dfinding.confidence,
                            _dfinding.evidence_type.value,
                            _dfinding.is_compressed,
                            _dfinding.raw_content_path,
                            json.dumps(_dfinding.metadata),
                        )
                    # BUG-R4-01 fix (deepen path): write finding node + evidence edge to
                    # knowledge graph.  Mirrors the same fix in handle_search above.
                    try:
                        await graph_writer.add_finding_node(db, task.id, _dfinding, redis_client)
                        await graph_writer.add_evidence_edge(
                            db, task.id, _dfinding.id, _dfinding.hypothesis_id,
                            _dfinding.evidence_type.value, redis_client,
                        )
                    except Exception:  # noqa: BLE001
                        pass  # graph writes are fire-and-forget
                except Exception as _dfexc:  # noqa: BLE001
                    log.warning(
                        "deepen_finding_persist_failed",
                        branch_id=branch.id,
                        error=str(_dfexc),
                    )

            log.info(
                "deepen_evidence_extracted",
                branch_id=branch.id,
                items_saved=len(_deepen_out.evidence_items),
            )

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

    # BUG-R5-01 fix: re-read active branches from the DB before checkpointing.
    # session_data.active_branches is built before the current action list runs,
    # so it can be stale if a prior action in the same iteration just killed a
    # branch.  Using the stale list writes checkpoints with dead branches still
    # marked active and also feeds stale data into diminishing-returns analysis.
    active = await get_active_branches(task.id, db)
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
        # BUG-R5-02 fix: SAVE_CHECKPOINT actions run before the main loop
        # advances task.current_state to next_state.  Persisting the old state
        # here writes stale checkpoints (for example EVALUATE instead of
        # CHECKPOINT), causing resumes to restart from the wrong phase.
        current_state=State.CHECKPOINT,
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
    # BUG-AUD-25 fix: Exclude findings that already have a tribunal session
    # to prevent the same finding from being retried repeatedly.
    top_finding = await db.fetchrow(
        """
        SELECT f.id, f.hypothesis_id FROM findings f
        WHERE f.task_id = $1
          AND NOT EXISTS (
            SELECT 1 FROM tribunal_sessions ts
            WHERE ts.finding_id = f.id AND ts.task_id = $1
          )
        ORDER BY f.confidence DESC
        LIMIT 1
        """,
        task.id,
    )
    if top_finding is None:
        log.warning("no_finding_for_tribunal")
        return

    finding_id = top_finding["id"]
    tribunal_id = str(uuid.uuid4())

    # BUG-R15-01: Fetch finding content, supporting evidence, and sources for tribunal context
    _finding_row = await db.fetchrow(
        "SELECT content, confidence FROM findings WHERE id = $1", finding_id
    )
    _finding_summary = _finding_row["content"] if _finding_row else "[no content]"

    _supporting_rows = await db.fetch(
        "SELECT content, confidence FROM findings WHERE task_id = $1 AND id != $2 ORDER BY confidence DESC LIMIT 5",
        task.id, finding_id,
    )
    _supporting_evidence = "\n".join(
        f"[{i}] confidence={r['confidence']:.2f}\n{r['content'][:600]}"
        for i, r in enumerate(_supporting_rows, 1)
    ) or "(no supporting findings)"

    _source_rows = await db.fetch(
        "SELECT url, title FROM sources WHERE task_id = $1 LIMIT 20", task.id
    )
    _sources_text = "\n".join(f"- {r['title'] or r['url']}" for r in _source_rows) or "(no sources)"

    _quality_tier = (task.metadata or {}).get("quality_tier")
    total_tribunal_cost: float = 0.0

    def _format_arg(arg: "TribunalArgumentOutput") -> str:
        return (
            f"{arg.argument_summary}\n\nKey points:\n"
            + "\n".join(f"\u2022 {pt}" for pt in arg.key_points)
        )

    # PLAINTIFF
    _plaintiff_parsed, _plaintiff_session = await spawn_model(
        task_type=TaskType.TRIBUNAL_PLAINTIFF,
        context=_augment_context_from_task({
            "task_id": task.id,
            "tribunal_id": tribunal_id,
            "finding_id": finding_id,
            "finding_summary": _finding_summary,
            "supporting_evidence": _supporting_evidence,
            "sources": _sources_text,
            "budget_remaining": cost_tracker.budget_remaining,
        }, task),
        output_schema=TribunalArgumentOutput,
        branch_id=None, db=db, cost_tracker=cost_tracker, config=config,
        quality_tier=_quality_tier,
    )
    task.ai_call_counter += 1
    total_tribunal_cost += _plaintiff_session.cost_usd
    _plaintiff_text = _format_arg(_plaintiff_parsed)  # type: ignore[arg-type]

    # DEFENDANT
    _defendant_parsed, _defendant_session = await spawn_model(
        task_type=TaskType.TRIBUNAL_DEFENDANT,
        context=_augment_context_from_task({
            "task_id": task.id,
            "tribunal_id": tribunal_id,
            "finding_id": finding_id,
            "finding_summary": _finding_summary,
            "plaintiff_argument": _plaintiff_text,
            "budget_remaining": cost_tracker.budget_remaining,
        }, task),
        output_schema=TribunalArgumentOutput,
        branch_id=None, db=db, cost_tracker=cost_tracker, config=config,
        quality_tier=_quality_tier,
    )
    task.ai_call_counter += 1
    total_tribunal_cost += _defendant_session.cost_usd
    _defendant_text = _format_arg(_defendant_parsed)  # type: ignore[arg-type]

    # REBUTTAL (plaintiff responds to defendant)
    _rebuttal_parsed, _rebuttal_session = await spawn_model(
        task_type=TaskType.TRIBUNAL_REBUTTAL,
        context=_augment_context_from_task({
            "task_id": task.id,
            "tribunal_id": tribunal_id,
            "finding_id": finding_id,
            "finding_summary": _finding_summary,
            "defendant_argument": _defendant_text,
            "plaintiff_original": _plaintiff_text,
            "budget_remaining": cost_tracker.budget_remaining,
        }, task),
        output_schema=TribunalArgumentOutput,
        branch_id=None, db=db, cost_tracker=cost_tracker, config=config,
        quality_tier=_quality_tier,
    )
    task.ai_call_counter += 1
    total_tribunal_cost += _rebuttal_session.cost_usd
    _rebuttal_text = _format_arg(_rebuttal_parsed)  # type: ignore[arg-type]

    # COUNTER (defendant responds to rebuttal)
    _counter_parsed, _counter_session = await spawn_model(
        task_type=TaskType.TRIBUNAL_COUNTER,
        context=_augment_context_from_task({
            "task_id": task.id,
            "tribunal_id": tribunal_id,
            "finding_id": finding_id,
            "finding_summary": _finding_summary,
            "plaintiff_rebuttal": _rebuttal_text,
            "defendant_original": _defendant_text,
            "budget_remaining": cost_tracker.budget_remaining,
        }, task),
        output_schema=TribunalArgumentOutput,
        branch_id=None, db=db, cost_tracker=cost_tracker, config=config,
        quality_tier=_quality_tier,
    )
    task.ai_call_counter += 1
    total_tribunal_cost += _counter_session.cost_usd
    _counter_text = _format_arg(_counter_parsed)  # type: ignore[arg-type]

    # JUDGE — capture parsed output so we can persist the verdict
    judge_output, judge_session = await spawn_model(
        task_type=TaskType.TRIBUNAL_JUDGE,
        context=_augment_context_from_task({
            "task_id": task.id,
            "tribunal_id": tribunal_id,
            "finding_id": finding_id,
            "finding_summary": _finding_summary,
            "plaintiff_summary": _plaintiff_text,
            "defendant_summary": _defendant_text,
            "plaintiff_rebuttal_summary": _rebuttal_text,
            "defendant_counter_summary": _counter_text,
            "budget_remaining": cost_tracker.budget_remaining,
        }, task),
        output_schema=TribunalVerdictOutput,
        branch_id=None, db=db, cost_tracker=cost_tracker, config=config,
        quality_tier=_quality_tier,
    )
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

    # BUG-R15-03: Fetch finding content, confidence, and tribunal verdict for skeptic context
    _s_finding_content = ""
    _s_finding_confidence: float = 0.0
    if _top_finding_row:
        _s_full_finding = await db.fetchrow(
            "SELECT content, confidence FROM findings WHERE id = $1",
            _top_finding_row["id"],
        )
        if _s_full_finding:
            _s_finding_content = _s_full_finding["content"]
            _s_finding_confidence = float(_s_full_finding["confidence"])

    _s_tribunal_verdict = ""
    _s_unanswered_questions = ""
    if _tribunal_row:
        _s_full_tribunal = await db.fetchrow(
            "SELECT verdict, judge_reasoning, unanswered_questions FROM tribunal_sessions WHERE id = $1",
            _tribunal_row["id"],
        )
        if _s_full_tribunal:
            _s_tribunal_verdict = _s_full_tribunal["verdict"] or ""
            _s_unanswered_questions = _s_full_tribunal["unanswered_questions"] or ""
            # unanswered_questions may be a JSON string — decode if so
            if isinstance(_s_unanswered_questions, str) and _s_unanswered_questions.startswith("["):
                import json as _json_uq  # noqa: PLC0415
                try:
                    _s_unanswered_questions = "\n".join(_json_uq.loads(_s_unanswered_questions))
                except (ValueError, TypeError):
                    pass

    skeptic_output, ai_session = await spawn_model(
        task_type=TaskType.SKEPTIC_QUESTIONS,
        context=_augment_context_from_task({
            "task_id": task.id,
            "task_topic": task.topic,
            "finding_summary": _s_finding_content,
            "confidence_score": _s_finding_confidence,
            "tribunal_verdict": _s_tribunal_verdict,
            "unanswered_questions": _s_unanswered_questions,
            "budget_remaining": cost_tracker.budget_remaining,
        }, task),
        output_schema=SkepticQuestionsOutput,
        branch_id=None,
        db=db,
        cost_tracker=cost_tracker,
        config=config,
        quality_tier=(task.metadata or {}).get("quality_tier"),
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

    # ── Intelligence Engine: before_report hook ──────────────────────────
    # Runs multi-perspective synthesis, reasoning chain audit, and executive
    # summary generation BEFORE the final report is compiled.  Results are
    # stored in DB tables and injected into task metadata so the report
    # generator can reference them.
    _intel_report_ctx: dict[str, Any] = {}
    try:
        from mariana.orchestrator.intelligence.engine import before_report as _intel_before_report  # noqa: PLC0415

        _intel_report_ctx = await _intel_before_report(
            task_id=task.id,
            topic=task.topic,
            db=db,
            cost_tracker=cost_tracker,
            config=config,
            quality_tier=(task.metadata or {}).get("quality_tier"),
            tier=(task.metadata or {}).get("tier", "standard"),
        )
        log.info(
            "intelligence_before_report_complete",
            perspectives=_intel_report_ctx.get("perspectives_generated", 0),
            audit_passed=_intel_report_ctx.get("audit_passed", False),
            audit_score=_intel_report_ctx.get("audit_score", 0.0),
            summaries=_intel_report_ctx.get("summaries_generated", False),
        )

        # Inject intelligence context into task metadata for the report generator
        _meta = task.metadata or {}
        _meta["intelligence_report_context"] = {
            "audit_passed": _intel_report_ctx.get("audit_passed", False),
            "audit_score": _intel_report_ctx.get("audit_score", 0.0),
            "audit_issues": _intel_report_ctx.get("audit_issues", 0),
            "perspectives_generated": _intel_report_ctx.get("perspectives_generated", 0),
            "one_liner": _intel_report_ctx.get("one_liner", ""),
        }
        task.metadata = _meta
    except Exception as _intel_exc:
        log.warning("intelligence_before_report_failed", error=str(_intel_exc))

    # Fetch confirmed / high-confidence findings for the report
    # BUG-047: include metadata column
    # BUG-AUD-24 fix: Exclude findings that were DESTROYED by tribunal and
    # filter to confidence >= 0.3 to avoid feeding low-quality scraps to the
    # drafting LLM. LEFT JOIN ensures findings with no tribunal session pass.
    finding_rows = await db.fetch(
        """
        SELECT f.id, f.task_id, f.hypothesis_id, f.content, f.content_en,
               f.content_language, f.source_ids, f.confidence, f.evidence_type,
               f.is_compressed, f.raw_content_path, f.created_at, f.metadata
        FROM findings f
        LEFT JOIN tribunal_sessions ts ON ts.finding_id = f.id AND ts.task_id = f.task_id
        WHERE f.task_id = $1
          AND f.confidence >= 0.3
          AND (ts.verdict IS NULL OR ts.verdict != 'DESTROYED')
        ORDER BY f.confidence DESC
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
            config=config,
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
                case State.PIVOT:
                    # BUG-R4-03 fix: PIVOT+HYPOTHESES_READY emits SPAWN_AI but there was
                    # no PIVOT case here, causing a silent "spawn_ai_unhandled_state" warning
                    # and pivot hypothesis generation to be silently dropped.  Pivots need
                    # new hypotheses and branches just like INIT, so handle_init() is correct.
                    await handle_init(task, session_data, cost_tracker, db, redis_client, config)
                case State.CHECKPOINT:
                    # BUG-AUD-02 fix: CHECKPOINT+STRONG_FINDINGS_EXIST with skip_tribunal
                    # emits SPAWN_AI(task_type='SKEPTIC_QUESTIONS'), but task.current_state
                    # is still CHECKPOINT when actions execute (state advances after all
                    # actions complete). Without this case, SKEPTIC_QUESTIONS from CHECKPOINT
                    # fell through to the '_' wildcard and was silently dropped.
                    if task_type_str == "SKEPTIC_QUESTIONS":
                        await handle_skeptic(task, session_data, cost_tracker, db, redis_client, config)
                    else:
                        log.warning("spawn_ai_unexpected_checkpoint_task", task_type=task_type_str)
                case State.TRIBUNAL:
                    # BUG-R4-02 fix: TRIBUNAL+TRIBUNAL_CONFIRMED emits SPAWN_AI with
                    # task_type='SKEPTIC_QUESTIONS', but task.current_state is still TRIBUNAL
                    # when actions execute (state is only advanced after all actions complete).
                    # Routing on task.current_state caused handle_tribunal() to re-run the
                    # entire 5-step adversarial tribunal, persisting a second TribunalSession
                    # and potentially overwriting the original verdict.
                    # Fix: inspect action.params["task_type"]; if SKEPTIC_QUESTIONS, call
                    # handle_skeptic; otherwise call handle_tribunal (plain TRIBUNAL entry).
                    if task_type_str == "SKEPTIC_QUESTIONS":
                        await handle_skeptic(task, session_data, cost_tracker, db, redis_client, config)
                    else:
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
            # BUG-D1-01 fix: score8 → $50 grant, score7 → $20 grant, minimal → $2 keep-alive
            if score_band == "score8":
                amount = 50.0
            elif score_band == "score7":
                amount = 20.0
            else:
                # "minimal" (dont_kill_branches keep-alive) — intentionally small
                amount = 2.0
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

    # Read user flow control flags from task metadata
    _meta = task.metadata or {}
    _dont_kill = bool(_meta.get("dont_kill_branches", False))
    _force_report = bool(_meta.get("force_report_on_halt", False))
    _skip_skeptic = bool(_meta.get("skip_skeptic", False))
    _skip_tribunal = bool(_meta.get("skip_tribunal", False))

    # Tier-based overrides: standard tier skips tribunal + skeptic to stay under 5 min.
    # Only deep tier runs the full tribunal/skeptic pipeline.
    _tier = _meta.get("tier", "standard")
    if _tier in ("instant", "quick", "standard"):
        _skip_tribunal = True
        _skip_skeptic = True
    _user_directives = _meta.get("user_directives", {})
    if not isinstance(_user_directives, dict):
        _user_directives = {}

    return ResearchSessionData(
        task=task,
        active_branches=active_branches,
        dead_branches=dead_branches,
        recent_findings=recent_findings,
        all_source_ids=all_source_ids,
        ai_call_counter=task.ai_call_counter,
        recent_action_summaries=[],
        dont_kill_branches=_dont_kill,
        force_report_on_halt=_force_report,
        skip_skeptic=_skip_skeptic,
        skip_tribunal=_skip_tribunal,
        user_directives=_user_directives,
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

    BUG-D1-01 fix: Added ``AND status != 'HALTED'`` guard when the in-memory
    status is not HALTED.  This prevents a mid-loop persist from overwriting
    an externally-set HALTED (from the kill API) back to RUNNING.  When the
    event loop itself sets HALTED (via kill check, budget exhaustion, or
    shutdown), the guard passes because in-memory status is already HALTED.
    """
    # BUG-AUD-16 fix: Always protect against overwriting an externally-set
    # HALTED, unless we are intentionally writing HALTED or FAILED ourselves.
    # Previously, COMPLETED could overwrite HALTED if the kill API set it
    # between the event loop's last DB poll and the final persist.
    if task.status in (TaskStatus.HALTED, TaskStatus.FAILED):
        where_clause = "WHERE id = $12"
    else:
        where_clause = "WHERE id = $12 AND status != 'HALTED'"

    await db.execute(
        f"""
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
        {where_clause}
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
        active = await get_active_branches(task.id, db)  # BUG-R3-02 fix: correct arg order (task_id, db)
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

    api_key = config.SUPABASE_SERVICE_KEY or config.SUPABASE_ANON_KEY
    # Use RPC function (SECURITY DEFINER) — works with both service key and anon key
    rpc_url = f"{config.SUPABASE_URL}/rest/v1/rpc/get_user_tokens"
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with _httpx_check.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            rpc_url,
            json={"target_user_id": user_id},
            headers=headers,
        )
        if resp.status_code != 200:
            return None
        result = resp.json()
        if result is None:
            return None
        return int(result)


# BUG-S2-09 fix: hold references to fire-and-forget tasks to prevent GC from
# silently dropping them before they complete.  Python's asyncio will log
# "Task was destroyed but it is pending!" warnings otherwise.
_background_tasks: set[Any] = set()


async def _write_handoff_context(
    task: "ResearchTask",
    cost_tracker: Any,
    phase_name: str,
    findings_summary: str = "",
    db: Any = None,
) -> None:
    """Write orchestrator handoff context to task metadata AND orchestrator_handoffs table.

    Called between major phase transitions so the next 'fresh' orchestrator
    instance has all necessary context without relying on LLM conversation
    history.  This prevents AI degradation over long research tasks.

    The handoff record is stored under ``task.metadata["last_handoff"]`` (persisted
    to the DB via ``_persist_task``) AND inserted into the ``orchestrator_handoffs``
    table via ``rotation.write_handoff`` so that ``rotation.read_handoff`` can
    retrieve it on next startup.

    BUG-D1-02 fix: previously this function only wrote to in-memory task.metadata.
    rotation.read_handoff() queries orchestrator_handoffs, which was always empty.
    """
    handoff: dict[str, Any] = {
        "phase_completed": phase_name,
        "total_spent_usd": round(cost_tracker.total_spent, 4),
        "budget_remaining_usd": round(cost_tracker.budget_remaining, 4),
        "ai_calls_made": cost_tracker.call_count,
        "current_state": task.current_state.value,
        "diminishing_flags": getattr(task, "diminishing_flags", 0),
    }
    if findings_summary:
        handoff["findings_summary"] = findings_summary[:2000]

    meta: dict[str, Any] = dict(task.metadata or {})
    meta["last_handoff"] = handoff
    task.metadata = meta

    # BUG-D1-02 fix: persist to orchestrator_handoffs table so rotation.read_handoff works
    # BUG-R3-03 fix: populate key_findings, active_hypotheses, killed_hypotheses, and
    # sources_found from the DB so the rotation prompt contains real research state.
    # Previously OrchestratorContext was created with all list fields empty, making
    # build_rotation_prompt() return "(none)" for every section.
    if db is not None:
        task_meta = task.metadata or {}

        # --- Fetch high-confidence findings for handoff ---
        _hf_rows = []
        _ah_rows = []
        _kh_rows = []
        _src_rows = []
        try:
            _hf_rows = await db.fetch(
                """
                SELECT content FROM findings
                WHERE task_id = $1
                ORDER BY confidence DESC
                LIMIT 20
                """,
                task.id,
            )
            _ah_rows = await db.fetch(
                "SELECT statement FROM hypotheses WHERE task_id = $1 AND status = 'ACTIVE'",
                task.id,
            )
            _kh_rows = await db.fetch(
                "SELECT h.statement, b.kill_reason FROM branches b "
                "JOIN hypotheses h ON h.id = b.hypothesis_id "
                "WHERE b.task_id = $1 AND b.status = 'KILLED'",
                task.id,
            )
            _src_rows = await db.fetch(
                "SELECT url FROM sources WHERE task_id = $1 LIMIT 100",
                task.id,
            )
        except Exception as _ctx_exc:  # noqa: BLE001
            logger.debug("handoff_context_db_fetch_failed", task_id=task.id, error=str(_ctx_exc))

        _key_findings = [r["content"][:500] for r in _hf_rows]
        _active_hyps = [r["statement"] for r in _ah_rows]
        _killed_hyps = [
            f"{r['statement']} (killed: {r.get('kill_reason', 'unknown')})"
            for r in _kh_rows
        ]
        _sources = [r["url"] for r in _src_rows]

        ctx = OrchestratorContext(
            task_id=task.id,
            phase=phase_name,
            key_findings=_key_findings,
            active_hypotheses=_active_hyps,
            killed_hypotheses=_killed_hyps,
            sources_found=_sources,
            quality_tier=task_meta.get("quality_tier", "balanced"),
            user_instructions=task_meta.get("user_flow_instructions", ""),
            loop_config={
                "continuous_mode": task_meta.get("continuous_mode", False),
                "dont_kill_branches": task_meta.get("dont_kill_branches", False),
            },
        )
        await rotation.write_handoff(db, ctx)

    logger.debug(
        "handoff_context_written",
        task_id=task.id,
        phase_completed=phase_name,
        total_spent_usd=handoff["total_spent_usd"],
        budget_remaining_usd=handoff["budget_remaining_usd"],
    )


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
