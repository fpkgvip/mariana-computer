"""Mariana agent event loop — PLAN → EXECUTE → TEST → FIX → REPLAN → DELIVER.

This module owns the lifecycle of a single :class:`AgentTask`.  It is invoked
by ``mariana.main`` when a task is picked off the Redis queue, and by the API
when running a task synchronously in dev.

Design notes
------------
* Loop is fully async.  One task = one asyncio task.
* Checkpointing: after every state change we persist the task JSON back to
  ``agent_tasks`` and append an entry to ``agent_events``.  This lets the UI
  reconnect mid-run without losing progress.
* Streaming: every :class:`AgentEvent` is also ``XADD``-ed to the Redis stream
  ``agent:{task_id}:events``.  The SSE endpoint in ``api.py`` consumes that
  stream and forwards it to the browser.
* Budgets: hard caps on replans, fix-attempts-per-step, wall-clock duration,
  and USD spend.  Any breach transitions to HALTED.
* Self-correction: a step may fail up to ``max_fix_attempts_per_step`` times.
  On the final failure we bubble up and REPLAN, capped by ``max_replans``.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

import structlog

from mariana.agent import planner
from mariana.agent.dispatcher import ToolError, dispatch
from mariana.agent.models import (
    AgentArtifact,
    AgentEvent,
    AgentState,
    AgentStep,
    AgentTask,
    StepStatus,
)
from mariana.agent.state import assert_transition, is_terminal
from mariana.vault.runtime import (
    clear_vault_env,
    fetch_vault_env,
    get_redactor,
    redact_payload,
    set_task_context,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Redis keys / streams.
_STREAM_KEY = "agent:{task_id}:events"
_STOP_KEY = "agent:{task_id}:stop"

# Maximum size of a single SSE payload — the UI doesn't need more than this.
_MAX_EVENT_PAYLOAD_BYTES = 32 * 1024

# Hard ceilings defended in code regardless of LLM output.
_HARD_MAX_STEPS = 25
_HARD_MAX_FIX_PER_STEP = 5
_HARD_MAX_REPLANS = 3

# Output truncation for the LLM-visible result field (keeps fix prompts small).
_STEP_STDOUT_TAIL = 4000
_STEP_STDERR_TAIL = 4000


# ---------------------------------------------------------------------------
# Checkpoint + event helpers
# ---------------------------------------------------------------------------


async def _persist_task(db: Any, task: AgentTask) -> None:
    """Write the full task JSON back to Postgres.  Idempotent UPSERT."""
    task.updated_at = datetime.now(tz=timezone.utc)
    payload = task.model_dump(mode="json")
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_tasks (
                id, user_id, conversation_id, goal, user_instructions,
                state, selected_model, steps, artifacts,
                max_duration_hours, budget_usd, spent_usd,
                max_fix_attempts_per_step, max_replans, replan_count, total_failures,
                final_answer, stop_requested, error,
                created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8::jsonb, $9::jsonb,
                $10, $11, $12,
                $13, $14, $15, $16,
                $17, $18, $19,
                $20, $21
            )
            ON CONFLICT (id) DO UPDATE SET
                state = EXCLUDED.state,
                steps = EXCLUDED.steps,
                artifacts = EXCLUDED.artifacts,
                spent_usd = EXCLUDED.spent_usd,
                replan_count = EXCLUDED.replan_count,
                total_failures = EXCLUDED.total_failures,
                final_answer = EXCLUDED.final_answer,
                stop_requested = EXCLUDED.stop_requested,
                error = EXCLUDED.error,
                updated_at = EXCLUDED.updated_at
            """,
            task.id,
            task.user_id,
            task.conversation_id,
            task.goal,
            task.user_instructions,
            task.state.value,
            task.selected_model,
            json.dumps(payload["steps"]),
            json.dumps(payload["artifacts"]),
            task.max_duration_hours,
            task.budget_usd,
            task.spent_usd,
            task.max_fix_attempts_per_step,
            task.max_replans,
            task.replan_count,
            task.total_failures,
            task.final_answer,
            task.stop_requested,
            task.error,
            task.created_at,
            task.updated_at,
        )


async def _record_event(db: Any, redis: Any, task_id: str, event: AgentEvent) -> None:
    """Append to agent_events and XADD to the Redis stream for SSE."""
    payload = event.model_dump(mode="json")
    # Vault redaction: scrub every plaintext secret from the payload BEFORE
    # we serialise it into the truncation check, the DB row, or the SSE
    # stream.  ``redact_payload`` walks dicts/lists recursively and is a
    # no-op when no secrets are bound.
    payload["payload"] = redact_payload(payload.get("payload") or {})
    # Truncate huge payloads so Redis / browser stay responsive.
    enc = json.dumps(payload["payload"])
    if len(enc) > _MAX_EVENT_PAYLOAD_BYTES:
        # Redact the sample too — belt-and-suspenders against any string that
        # only became visible after truncation flattening.
        sample = get_redactor()(enc[:2000])
        payload["payload"] = {
            "_truncated": True,
            "size": len(enc),
            "sample": sample + "…[truncated]",
        }
    try:
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_events (task_id, event_type, state, step_id, payload)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                """,
                task_id,
                event.event_type,
                event.state.value if event.state else None,
                event.step_id,
                json.dumps(payload["payload"]),
            )
    except Exception as exc:
        logger.warning("agent_event_db_insert_failed", task_id=task_id, error=str(exc))

    if redis is not None:
        try:
            await redis.xadd(
                _STREAM_KEY.format(task_id=task_id),
                {"data": json.dumps(payload)},
                maxlen=5000,
                approximate=True,
            )
        except Exception as exc:
            logger.warning("agent_event_redis_xadd_failed", task_id=task_id, error=str(exc))


def _mk_event(
    task_id: str,
    event_type: str,
    *,
    state: AgentState | None = None,
    step_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AgentEvent:
    return AgentEvent(
        task_id=task_id,
        event_type=event_type,  # type: ignore[arg-type]
        state=state,
        step_id=step_id,
        payload=payload or {},
    )


async def _emit(
    db: Any,
    redis: Any,
    task: AgentTask,
    event_type: str,
    *,
    step_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    await _record_event(
        db, redis, task.id,
        _mk_event(task.id, event_type, state=task.state, step_id=step_id, payload=payload),
    )


# ---------------------------------------------------------------------------
# Transition helper — validates + persists + emits
# ---------------------------------------------------------------------------


async def _transition(db: Any, redis: Any, task: AgentTask, new_state: AgentState) -> None:
    if task.state == new_state:
        return
    assert_transition(task.state, new_state)
    old = task.state
    task.state = new_state
    await _persist_task(db, task)
    await _emit(
        db, redis, task, "state_change",
        payload={"from": old.value, "to": new_state.value},
    )


# ---------------------------------------------------------------------------
# Stop / budget checks
# ---------------------------------------------------------------------------


async def _check_stop_requested(redis: Any, task: AgentTask) -> bool:
    if task.stop_requested:
        return True
    if redis is None:
        return False
    try:
        v = await redis.get(_STOP_KEY.format(task_id=task.id))
    except Exception:
        return False
    if v:
        task.stop_requested = True
        return True
    return False


def _budget_exceeded(task: AgentTask, started_at: float) -> tuple[bool, str]:
    if task.spent_usd >= task.budget_usd:
        return True, f"budget_exhausted: spent ${task.spent_usd:.4f} >= ${task.budget_usd:.2f}"
    elapsed_h = (time.time() - started_at) / 3600.0
    if elapsed_h >= task.max_duration_hours:
        return True, f"duration_exhausted: {elapsed_h:.3f}h >= {task.max_duration_hours:.3f}h"
    return False, ""


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------


def _step_by_id(task: AgentTask, step_id: str) -> AgentStep | None:
    for s in task.steps:
        if s.id == step_id:
            return s
    return None


def _replace_step(task: AgentTask, new_step: AgentStep) -> None:
    for i, s in enumerate(task.steps):
        if s.id == new_step.id:
            # Preserve attempt counter across replacements so the cap still applies.
            new_step.attempts = s.attempts
            task.steps[i] = new_step
            return
    # If no match, append — defensive.
    task.steps.append(new_step)


def _tail(text: str, max_chars: int) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return "…" + text[-max_chars:]


def _summarise_result(result: dict[str, Any]) -> dict[str, Any]:
    """Trim a tool result for storage + LLM context without losing signal.

    Vault: every string value is run through the active redactor so a tool
    that echoed a plaintext secret never makes it into ``step.result`` (which
    is what the planner re-feeds to the LLM during fix attempts).
    """
    redactor = get_redactor()
    out: dict[str, Any] = {}
    for k, v in result.items():
        if isinstance(v, str):
            if k in ("stdout",):
                out[k] = redactor(_tail(v, _STEP_STDOUT_TAIL))
            elif k in ("stderr",):
                out[k] = redactor(_tail(v, _STEP_STDERR_TAIL))
            elif k in ("body", "content", "image_b64", "pdf_b64"):
                out[k] = redactor(_tail(v, 4000)) if k == "body" else f"<{len(v)} bytes omitted>"
            else:
                out[k] = redactor(_tail(v, 4000))
        else:
            # Recursively walk nested structures so e.g. result['artifacts']
            # entries don't carry plaintext through.
            out[k] = redact_payload(v)
    return out


def _infer_failure(tool: str, result: dict[str, Any]) -> str | None:
    """Detect "soft" failures (non-exception tool results we still want to fix).

    Returns a short error string if the step should be considered failed,
    otherwise None.
    """
    if tool in ("code_exec", "bash_exec", "typescript_exec", "rust_exec"):
        if bool(result.get("timed_out")):
            return f"timed_out after {result.get('duration_ms', 0)}ms"
        if bool(result.get("killed")):
            return "process killed (memory / signal)"
        exit_code = result.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            return f"non-zero exit code {exit_code}"
    if tool in ("browser_fetch", "browser_click_fetch"):
        status = result.get("status")
        if isinstance(status, int) and status >= 400:
            return f"HTTP {status}"
    return None


async def _run_one_step(
    db: Any,
    redis: Any,
    task: AgentTask,
    step: AgentStep,
) -> tuple[bool, str | None]:
    """Execute a single step.  Returns (success, error_message)."""
    step.attempts += 1
    step.status = StepStatus.RUNNING
    step.started_at = time.time()
    step.error = None
    step.result = None
    await _persist_task(db, task)
    await _emit(
        db, redis, task, "step_started",
        step_id=step.id,
        payload={
            "title": step.title,
            "tool": step.tool,
            "attempt": step.attempts,
            "params": planner._truncate_params(step.params),
        },
    )

    try:
        result = await dispatch(
            step.tool, step.params, user_id=task.user_id, task_id=task.id,
        )
    except ToolError as exc:
        step.status = StepStatus.FAILED
        step.finished_at = time.time()
        step.error = str(exc)
        if exc.detail:
            step.result = {"error_detail": exc.detail}
        task.total_failures += 1
        await _persist_task(db, task)
        await _emit(
            db, redis, task, "step_failed",
            step_id=step.id,
            payload={"error": step.error, "detail": exc.detail},
        )
        return False, step.error
    except Exception as exc:  # defensive: any unexpected error
        step.status = StepStatus.FAILED
        step.finished_at = time.time()
        step.error = f"unexpected: {type(exc).__name__}: {exc}"
        task.total_failures += 1
        await _persist_task(db, task)
        await _emit(
            db, redis, task, "step_failed",
            step_id=step.id,
            payload={"error": step.error},
        )
        return False, step.error

    # Success path — but check for soft failures.
    soft_err = _infer_failure(step.tool, result)
    summary = _summarise_result(result)
    step.result = summary

    # Stream terminal output for code_exec so the UI can render a live pane.
    if step.tool in ("code_exec", "bash_exec", "typescript_exec", "rust_exec"):
        await _emit(
            db, redis, task, "terminal_output",
            step_id=step.id,
            payload={
                "stdout": summary.get("stdout", ""),
                "stderr": summary.get("stderr", ""),
                "exit_code": summary.get("exit_code"),
                "duration_ms": summary.get("duration_ms"),
            },
        )

    # Register artifacts produced by the tool (code_exec returns them,
    # browser_screenshot/pdf persist via save_to and return saved_to).
    for art in result.get("artifacts", []) or []:
        try:
            artifact = AgentArtifact(
                name=str(art.get("name", "")),
                workspace_path=str(art.get("workspace_path", "")),
                size=int(art.get("size", 0)),
                sha256=str(art.get("sha256", "")),
                produced_by_step=step.id,
            )
            task.artifacts.append(artifact)
            await _emit(
                db, redis, task, "artifact_created",
                step_id=step.id,
                payload=artifact.model_dump(mode="json"),
            )
        except Exception as exc:
            logger.warning("artifact_record_failed", error=str(exc))

    if soft_err:
        step.status = StepStatus.FAILED
        step.finished_at = time.time()
        step.error = soft_err
        task.total_failures += 1
        await _persist_task(db, task)
        await _emit(
            db, redis, task, "step_failed",
            step_id=step.id,
            payload={"error": soft_err, "result": summary},
        )
        return False, soft_err

    step.status = StepStatus.DONE
    step.finished_at = time.time()
    await _persist_task(db, task)
    await _emit(
        db, redis, task, "step_completed",
        step_id=step.id,
        payload={"result": summary, "duration_ms": int((step.finished_at - (step.started_at or step.finished_at)) * 1000)},
    )
    return True, None


# ---------------------------------------------------------------------------
# FIX / REPLAN wrappers
# ---------------------------------------------------------------------------


async def _attempt_fix(
    db: Any, redis: Any, task: AgentTask, failed_step: AgentStep,
) -> bool:
    """Ask the LLM for a replacement step; swap it in.  Returns True on success."""
    await _transition(db, redis, task, AgentState.FIX)
    try:
        new_step, cost = await planner.fix_step(task, failed_step)
    except Exception as exc:
        await _emit(
            db, redis, task, "error",
            payload={"phase": "fix", "error": str(exc)},
        )
        return False

    task.spent_usd += cost
    _replace_step(task, new_step)
    await _persist_task(db, task)
    await _emit(
        db, redis, task, "plan_created",
        step_id=new_step.id,
        payload={
            "kind": "fix",
            "step": new_step.model_dump(mode="json"),
            "cost_usd": cost,
        },
    )
    return True


async def _attempt_replan(
    db: Any, redis: Any, task: AgentTask, reason: str,
) -> bool:
    if task.replan_count >= min(task.max_replans, _HARD_MAX_REPLANS):
        return False
    await _transition(db, redis, task, AgentState.REPLAN)
    try:
        new_steps, cost = await planner.replan(task, reason=reason)
    except Exception as exc:
        await _emit(
            db, redis, task, "error",
            payload={"phase": "replan", "error": str(exc)},
        )
        return False
    task.replan_count += 1
    task.spent_usd += cost
    # Preserve successful prior steps by marking them SKIPPED? Simpler: keep
    # the fresh plan as the authoritative step list.  Any state from earlier
    # runs is still in the user workspace.
    task.steps = new_steps
    await _persist_task(db, task)
    await _emit(
        db, redis, task, "plan_created",
        payload={
            "kind": "replan",
            "reason": reason,
            "replan_count": task.replan_count,
            "steps": [s.model_dump(mode="json") for s in new_steps],
            "cost_usd": cost,
        },
    )
    return True


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


async def _deliver(db: Any, redis: Any, task: AgentTask, final_answer: str) -> None:
    await _transition(db, redis, task, AgentState.DELIVER)
    task.final_answer = final_answer or _default_summary(task)
    await _persist_task(db, task)
    await _emit(
        db, redis, task, "delivered",
        payload={
            "final_answer": task.final_answer,
            "artifacts": [a.model_dump(mode="json") for a in task.artifacts],
        },
    )
    await _transition(db, redis, task, AgentState.DONE)


def _default_summary(task: AgentTask) -> str:
    lines = [f"Task: {task.goal}", ""]
    done_steps = [s for s in task.steps if s.status == StepStatus.DONE]
    if done_steps:
        lines.append(f"Completed {len(done_steps)} steps.")
    if task.artifacts:
        lines.append("")
        lines.append("Artifacts:")
        for a in task.artifacts[:20]:
            lines.append(f"  - {a.workspace_path} ({a.size} bytes)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_agent_task(
    task: AgentTask,
    *,
    db: Any,
    redis: Any = None,
) -> AgentTask:
    """Drive a single task from PLAN to a terminal state.

    Returns the final :class:`AgentTask`.  Never raises — any fatal error is
    recorded on the task and the task ends in state FAILED or HALTED.
    """
    started_at = time.time()
    log = logger.bind(agent_task_id=task.id, user_id=task.user_id)

    # Clamp caps so callers can't weaken defenses.
    task.max_replans = min(task.max_replans, _HARD_MAX_REPLANS)
    task.max_fix_attempts_per_step = min(task.max_fix_attempts_per_step, _HARD_MAX_FIX_PER_STEP)

    # F4 Vault: pull this task's ephemeral env from Redis (frontend POSTed it
    # alongside /api/agent) and install both the env and the matching
    # redactor into the current async context.  Every dispatcher.exec_code
    # call will see these as real env vars; every event payload + step
    # result will be auto-redacted before it leaves the process.
    vault_env: dict[str, str] = {}
    try:
        vault_env = await fetch_vault_env(redis, task.id)
    except Exception as exc:  # pragma: no cover
        logger.warning("vault_env_fetch_failed", task_id=task.id, error=str(exc))
    ctx_handle = set_task_context(vault_env)
    if vault_env:
        log.info("vault_env_installed", count=len(vault_env), names=sorted(vault_env.keys()))

    try:
        # ---- PLAN --------------------------------------------------------
        await _persist_task(db, task)
        await _emit(db, redis, task, "state_change",
                    payload={"from": "init", "to": task.state.value})
        try:
            steps, cost = await planner.build_initial_plan(task)
        except Exception as exc:
            task.error = f"planner_failed: {exc}"
            await _emit(db, redis, task, "error", payload={"phase": "plan", "error": task.error})
            task.state = AgentState.FAILED
            await _persist_task(db, task)
            return task

        task.spent_usd += cost
        task.steps = steps
        await _persist_task(db, task)
        await _emit(
            db, redis, task, "plan_created",
            payload={
                "kind": "initial",
                "steps": [s.model_dump(mode="json") for s in steps],
                "cost_usd": cost,
            },
        )

        # ---- EXECUTE loop -----------------------------------------------
        while True:
            if await _check_stop_requested(redis, task):
                task.error = "stop_requested"
                await _emit(db, redis, task, "halted", payload={"reason": "stop_requested"})
                await _transition(db, redis, task, AgentState.HALTED)
                return task

            over, why = _budget_exceeded(task, started_at)
            if over:
                task.error = why
                await _emit(db, redis, task, "halted", payload={"reason": why})
                await _transition(db, redis, task, AgentState.HALTED)
                return task

            # Pick next pending step.
            next_step = next((s for s in task.steps if s.status == StepStatus.PENDING), None)
            if next_step is None:
                # All steps processed.  If a deliver step was run, we're done.
                # Otherwise synthesise a delivery.
                deliver = next(
                    (s for s in task.steps
                     if s.tool == "deliver" and s.status == StepStatus.DONE),
                    None,
                )
                final = (deliver.result or {}).get("final_answer") if deliver else ""
                await _deliver(db, redis, task, final or "")
                return task

            # Special-case deliver so we don't route it through "test/fix".
            if next_step.tool == "deliver":
                await _transition(db, redis, task, AgentState.DELIVER)
                ok, err = await _run_one_step(db, redis, task, next_step)
                if not ok:
                    task.error = f"deliver_failed: {err}"
                    task.state = AgentState.FAILED
                    await _persist_task(db, task)
                    return task
                final = (next_step.result or {}).get("final_answer") or ""
                await _deliver(db, redis, task, final)
                return task

            # Normal step.
            if task.state != AgentState.EXECUTE:
                await _transition(db, redis, task, AgentState.EXECUTE)
            ok, err = await _run_one_step(db, redis, task, next_step)

            if ok:
                continue

            # FIX loop for this step.
            fixed = False
            while (
                not fixed
                and next_step.attempts < min(task.max_fix_attempts_per_step, _HARD_MAX_FIX_PER_STEP)
            ):
                if await _check_stop_requested(redis, task):
                    task.error = "stop_requested"
                    await _emit(db, redis, task, "halted", payload={"reason": "stop_requested"})
                    await _transition(db, redis, task, AgentState.HALTED)
                    return task

                got_fix = await _attempt_fix(db, redis, task, next_step)
                if not got_fix:
                    break

                # Re-fetch: _replace_step mutates steps in place but Pydantic
                # gave us a new instance, so pull the current one by id.
                refreshed = _step_by_id(task, next_step.id)
                if refreshed is None:
                    break
                next_step = refreshed

                await _transition(db, redis, task, AgentState.EXECUTE)
                ok2, err2 = await _run_one_step(db, redis, task, next_step)
                if ok2:
                    fixed = True
                    break
                err = err2

            if fixed:
                continue  # Go back to top of outer loop to pick next step.

            # FIX budget exhausted → REPLAN.
            log.warning("agent_step_fix_exhausted", step_id=next_step.id, error=err)
            replanned = await _attempt_replan(
                db, redis, task,
                reason=f"step {next_step.id} failed after {next_step.attempts} attempts: {err}",
            )
            if replanned:
                await _transition(db, redis, task, AgentState.EXECUTE)
                continue

            # Out of replans → FAILED.
            task.error = f"unrecoverable: step {next_step.id} — {err}"
            await _emit(db, redis, task, "error", payload={"phase": "replan", "error": task.error})
            task.state = AgentState.FAILED
            await _persist_task(db, task)
            return task

    except Exception as exc:
        # Final safety net.  Every expected error path above already records
        # state; this catches programming errors.
        log.exception("agent_loop_crash")
        task.error = f"loop_crash: {type(exc).__name__}: {exc}"
        task.state = AgentState.FAILED
        try:
            await _persist_task(db, task)
            await _emit(db, redis, task, "error",
                        payload={"phase": "loop", "error": task.error})
        except Exception:
            pass
        return task
    finally:
        if is_terminal(task.state):
            try:
                await _persist_task(db, task)
            except Exception:
                pass
        # Drop the per-task vault context AND the Redis blob.  This is the
        # only place plaintext can persist server-side, so we delete it as
        # soon as the loop exits regardless of state.
        try:
            ctx_handle.reset()
        except Exception:
            pass
        try:
            await clear_vault_env(redis, task.id)
        except Exception:
            pass
