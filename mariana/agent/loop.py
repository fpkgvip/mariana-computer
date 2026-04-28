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


# P-01: terminal states whose presence in DB plus credits_settled=TRUE means
# the row has been *finalized* by some writer (typically the stop endpoint's
# inline pre-execution cancel + settle path).  A stale snapshot from a
# worker that loaded the row BEFORE finalization must NOT be allowed to
# clobber it back to a non-terminal / non-settled state via _persist_task's
# UPSERT.  See tests/test_p01_stale_worker_race.py.
_TERMINAL_STATE_VALUES: tuple[str, ...] = (
    "done", "failed", "halted", "cancelled",
)


async def _persist_task(db: Any, task: AgentTask) -> bool:
    """Write the full task JSON back to Postgres.  Guarded UPSERT.

    P-01: the ON CONFLICT UPDATE branch carries a compare-and-swap WHERE
    clause that REJECTS any UPDATE which would un-finalize a row that has
    already been settled by another writer.  Concretely: if the existing DB
    row has ``credits_settled=TRUE`` AND ``state`` in (done/failed/halted/
    cancelled), and the incoming snapshot wants to set
    ``credits_settled=FALSE``, the UPDATE is silently skipped — the legitimate
    finalization stays intact and the stale worker's later finally-block
    settlement check will see ``credits_settled=TRUE`` and short-circuit.

    Returns ``True`` if the row was inserted or updated, ``False`` if the
    UPDATE branch was rejected by the CAS guard.  Callers that care about
    finalization may use the return value to abort gracefully; legacy callers
    can ignore it.
    """
    task.updated_at = datetime.now(tz=timezone.utc)
    payload = task.model_dump(mode="json")
    async with db.acquire() as conn:
        # asyncpg's ``execute`` returns the libpq command tag, e.g.
        # ``"INSERT 0 1"``, ``"UPDATE 1"``, or ``"UPDATE 0"`` when the
        # WHERE filtered out the conflict-update row.  We parse it to
        # produce the bool return.
        cmd_tag = await conn.execute(
            # N-01: ``reserved_credits`` and ``credits_settled`` are part of
            # the agent_tasks row in this release.  They MUST appear in both
            # the INSERT and the ON CONFLICT SET clause, otherwise the
            # ``finally:`` settlement (which writes credits_settled=True
            # before this UPSERT runs) is dropped on disk and a requeue
            # would re-settle.
            #
            # P-01: the WHERE on the ON CONFLICT branch refuses to write
            # when the existing row is already finalized and the incoming
            # row would un-finalize it (stale-worker race double-refund).
            """
            INSERT INTO agent_tasks (
                id, user_id, conversation_id, goal, user_instructions,
                state, selected_model, steps, artifacts,
                max_duration_hours, budget_usd, spent_usd,
                reserved_credits, credits_settled,
                max_fix_attempts_per_step, max_replans, replan_count, total_failures,
                final_answer, stop_requested, error,
                created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8::jsonb, $9::jsonb,
                $10, $11, $12,
                $13, $14,
                $15, $16, $17, $18,
                $19, $20, $21,
                $22, $23
            )
            ON CONFLICT (id) DO UPDATE SET
                state = EXCLUDED.state,
                steps = EXCLUDED.steps,
                artifacts = EXCLUDED.artifacts,
                spent_usd = EXCLUDED.spent_usd,
                reserved_credits = EXCLUDED.reserved_credits,
                credits_settled = EXCLUDED.credits_settled,
                replan_count = EXCLUDED.replan_count,
                total_failures = EXCLUDED.total_failures,
                final_answer = EXCLUDED.final_answer,
                stop_requested = EXCLUDED.stop_requested,
                error = EXCLUDED.error,
                updated_at = EXCLUDED.updated_at
            WHERE NOT (
                agent_tasks.credits_settled = TRUE
                AND agent_tasks.state IN ('done','failed','halted','cancelled')
                AND EXCLUDED.credits_settled = FALSE
            )
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
            task.reserved_credits,
            task.credits_settled,
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

    # cmd_tag formats:
    #   "INSERT 0 1"      -> first insert; the row landed.
    #   "INSERT 0 0"      -> conflict matched and ON CONFLICT updated AND
    #                         passed the CAS WHERE; ON CONFLICT in PG
    #                         actually reports as "INSERT 0 1" too even when
    #                         updating, but the WHERE skip path reports
    #                         "INSERT 0 0".  We treat 0 as blocked.
    affected = 1
    try:
        parts = (cmd_tag or "").split()
        if parts and parts[0] in ("INSERT", "UPDATE") and parts[-1].isdigit():
            affected = int(parts[-1])
    except Exception:  # pragma: no cover — defensive parse fallback.
        affected = 1
    if affected == 0:
        logger.warning(
            "agent_persist_task_blocked",
            task_id=task.id,
            snapshot_state=task.state.value,
            snapshot_credits_settled=task.credits_settled,
        )
        return False
    return True


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
# M-01: credit settlement
# ---------------------------------------------------------------------------


async def _settle_agent_credits(task: AgentTask) -> None:
    """Reconcile reserved credits against actual ``spent_usd`` at task end.

    Mirrors the research-task settlement in ``mariana/main.py:_deduct_user_credits``.
    Conversion is **100 credits per $1** — the canonical platform rule used
    by the frontend ``creditsFromUsd`` helper and the Pricing page.

    * No-op if ``credits_settled`` is already True or no credits were reserved.
    * ``delta = final_tokens - reserved`` where ``final_tokens = int(spent_usd * 100)``.
    * ``delta == 0`` → noop, just flip the flag.
    * ``delta > 0``  → user spent more than reserved → deduct the overrun via
      the ``deduct_credits`` RPC.
    * ``delta < 0``  → reservation exceeded actual cost → refund the unused
      portion via the ``add_credits`` RPC.

    The function always sets ``credits_settled = True`` once it has attempted
    a reconciliation, even if the RPC call returned a non-2xx status — this
    is what makes the helper safe to call from a retried orchestrator pass
    without double-charging or double-refunding.  Errors are logged with
    ``agent_credits_settle_*`` keys for ops-time investigation.

    Late imports avoid the api.py ↔ agent.loop circular import.
    """
    if task.credits_settled or task.reserved_credits <= 0:
        return

    # Late import to dodge the circular dependency between mariana.api and
    # the agent loop.  These helpers already exist in api.py and centralise
    # the Supabase URL / api-key wiring.
    from mariana.api import (  # noqa: PLC0415
        _get_config as _get_cfg,
        _supabase_api_key,
    )

    cfg = _get_cfg()
    api_key = _supabase_api_key(cfg)
    if not getattr(cfg, "SUPABASE_URL", "") or not api_key:
        # Without Supabase wiring there is nothing to settle.  Mark settled
        # so the next call doesn't keep retrying a dead service.
        task.credits_settled = True
        logger.info(
            "agent_credits_settle_skipped_no_supabase",
            task_id=task.id,
            reserved=task.reserved_credits,
        )
        return

    final_tokens = int(task.spent_usd * 100)
    delta = final_tokens - task.reserved_credits

    if delta == 0:
        task.credits_settled = True
        logger.info(
            "agent_credits_settle_noop",
            task_id=task.id,
            user_id=task.user_id,
            reserved=task.reserved_credits,
            final_tokens=final_tokens,
        )
        return

    import httpx  # type: ignore[import]  # noqa: PLC0415

    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if delta > 0:
                # User spent more than reserved → take the overrun.
                rpc_url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/deduct_credits"
                resp = await client.post(
                    rpc_url,
                    json={"target_user_id": task.user_id, "amount": delta},
                    headers=headers,
                )
                # Set the flag regardless of HTTP outcome so a retry of the
                # finally block can't double-charge.  A logged failure here
                # is reconciled offline.
                task.credits_settled = True
                if resp.status_code in (200, 204):
                    logger.info(
                        "agent_credits_settle_extra_deduct_ok",
                        task_id=task.id,
                        user_id=task.user_id,
                        reserved=task.reserved_credits,
                        final_tokens=final_tokens,
                        extra_deducted=delta,
                    )
                else:
                    logger.error(
                        "agent_credits_settle_extra_deduct_failed",
                        task_id=task.id,
                        user_id=task.user_id,
                        reserved=task.reserved_credits,
                        final_tokens=final_tokens,
                        extra=delta,
                        status=resp.status_code,
                    )
                return

            # delta < 0 → refund unused reservation.
            refund = abs(delta)
            rpc_url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/add_credits"
            resp = await client.post(
                rpc_url,
                json={"p_user_id": task.user_id, "p_credits": refund},
                headers=headers,
            )
            task.credits_settled = True
            if resp.status_code in (200, 204):
                logger.info(
                    "agent_credits_settle_refund_ok",
                    task_id=task.id,
                    user_id=task.user_id,
                    reserved=task.reserved_credits,
                    final_tokens=final_tokens,
                    refunded=refund,
                )
            else:
                logger.error(
                    "agent_credits_settle_refund_failed",
                    task_id=task.id,
                    user_id=task.user_id,
                    reserved=task.reserved_credits,
                    final_tokens=final_tokens,
                    refund=refund,
                    status=resp.status_code,
                )
    except Exception as exc:  # noqa: BLE001
        # Defensive: never let a settlement error bubble out of the finally
        # block in run_task.  Mark settled so we don't loop on a permanently
        # broken Supabase config.
        task.credits_settled = True
        logger.error(
            "agent_credits_settle_exception",
            task_id=task.id,
            user_id=task.user_id,
            reserved=task.reserved_credits,
            error=str(exc),
        )


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
        # ---- P-01 pre-flight: re-validate the DB row before any work -----
        # The queue worker loaded ``task`` via a plain SELECT in
        # ``_load_agent_task`` (no FOR UPDATE / no version check).  If the
        # user hit Stop in the window between that load and us, the stop
        # endpoint may already have locked + settled the row.  Without this
        # gate, the next ``_persist_task`` would clobber the finalized
        # row back to our stale snapshot, and our ``finally:`` would issue
        # a SECOND ``_settle_agent_credits`` — double refund / minted
        # credits.  Returning here is safe: the in-memory task state is
        # not advanced, so ``is_terminal(task.state)`` stays False and
        # the finally block does not re-settle.
        try:
            async with db.acquire() as conn:
                fresh_row = await conn.fetchrow(
                    "SELECT state, credits_settled FROM agent_tasks WHERE id = $1",
                    task.id,
                )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "agent_run_task_prevalidate_failed",
                task_id=task.id,
                error=str(exc),
            )
            fresh_row = None
        if fresh_row is None:
            logger.warning("agent_run_task_missing_row", task_id=task.id)
            return task
        fresh_state = fresh_row["state"]
        # ``is True`` (not ``bool(...)``) so a test passing a MagicMock for db
        # cannot accidentally trip the early-abort path — only a real DB row
        # with a literal True will do.
        fresh_settled = fresh_row["credits_settled"] is True
        if fresh_settled and fresh_state in _TERMINAL_STATE_VALUES:
            logger.info(
                "agent_run_task_already_finalized",
                task_id=task.id,
                fresh_state=fresh_state,
            )
            # Do nothing — another writer has already finalized this row.
            # We deliberately do NOT mutate ``task.state`` so that the
            # ``finally:`` ``is_terminal(task.state)`` check stays False
            # and ``_settle_agent_credits`` is not called a second time.
            return task

        # ---- PLAN --------------------------------------------------------
        await _persist_task(db, task)
        await _emit(db, redis, task, "state_change",
                    payload={"from": "init", "to": task.state.value})

        # O-02: bail BEFORE invoking the planner if a stop has already been
        # requested.  The stop endpoint finalises pre-execution tasks itself,
        # but a race or a recovered legacy row may still arrive here with
        # ``stop_requested=TRUE``.  Without this gate the planner would run,
        # ``spent_usd`` would tick up, and the user would pay for a cancelled
        # task. ``HALTED`` (not CANCELLED) keeps the existing transition map
        # legal from PLAN and signals "the worker honoured the stop".
        if await _check_stop_requested(redis, task):
            task.error = "stop_requested"
            await _emit(db, redis, task, "halted",
                        payload={"reason": "stop_requested_pre_plan"})
            await _transition(db, redis, task, AgentState.HALTED)
            return task

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
            # M-01: settle reserved credits BEFORE the final persist so the
            # ``credits_settled`` flag (and any post-settlement state) lands
            # in the same UPSERT.  Wrapped in try/except so a settlement
            # error never crashes the finally block.
            #
            # P-01: defense-in-depth — re-read ``credits_settled`` from DB
            # right before settlement.  If another writer (typically the
            # stop endpoint) has already finalized this task while we were
            # running, our in-memory ``task.credits_settled`` may still be
            # False from the stale snapshot, but the DB row already shows
            # the refund happened.  Calling ``_settle_agent_credits`` again
            # would issue a SECOND ``add_credits`` RPC — the exact
            # double-refund the P-01 fix prevents.
            already_settled_in_db = False
            try:
                async with db.acquire() as conn:
                    final_row = await conn.fetchrow(
                        "SELECT credits_settled FROM agent_tasks WHERE id = $1",
                        task.id,
                    )
                if final_row is not None:
                    # asyncpg returns a real Python bool here; use ``is True``
                    # rather than ``bool(...)`` so a mocked DB in tests can't
                    # accidentally short-circuit the settlement.
                    already_settled_in_db = (
                        final_row["credits_settled"] is True
                    )
            except Exception:  # noqa: BLE001 — defensive
                logger.exception(
                    "agent_finally_settle_check_failed", task_id=task.id
                )
            if already_settled_in_db:
                # Reflect the canonical DB truth on the in-memory object so
                # ``_settle_agent_credits`` short-circuits even if called
                # again, and so the subsequent ``_persist_task`` does not
                # re-flip the flag to False via the stale snapshot.
                task.credits_settled = True
                logger.info(
                    "agent_finally_settle_skipped_already_settled",
                    task_id=task.id,
                )
            else:
                try:
                    await _settle_agent_credits(task)
                except Exception as _settle_exc:  # noqa: BLE001
                    logger.error(
                        "agent_credits_settle_finally_error",
                        task_id=task.id,
                        error=str(_settle_exc),
                    )
            try:
                # The CAS guard inside _persist_task will quietly reject
                # this if another writer's finalization is more recent.
                await _persist_task(db, task)
            except Exception:
                logger.exception(
                    "agent_finally_persist_failed", task_id=task.id
                )
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
