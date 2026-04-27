"""FastAPI routes for Mariana agent-mode.

Mounted by ``mariana.api`` via ``app.include_router(agent_router)``.  We keep
these here (not inline in api.py) so the 6k-line api module stays navigable.

Endpoints
---------
* ``POST   /api/agent``              — start a new agent task (202 Accepted)
* ``GET    /api/agent/{task_id}``    — get full task state (JSON)
* ``GET    /api/agent/{task_id}/stream`` — SSE stream of live events
* ``POST   /api/agent/{task_id}/stop``   — request graceful stop
* ``GET    /api/agent/{task_id}/events``  — recent events (paginated, JSON)
* ``GET    /api/workspace/{user_id}``     — list workspace files
* ``GET    /api/workspace/{user_id}/file`` — download a single file

Auth
----
All endpoints require a valid Supabase user JWT except when the shared
stream-token pattern (same as research SSE) is used for the SSE endpoint.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from typing import Any, AsyncIterator

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from mariana.agent import tools as sandbox_tools
from mariana.agent.models import AgentState, AgentTask

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class AgentStartRequest(BaseModel):
    """Body for POST /api/agent."""

    goal: str = Field(..., min_length=1, max_length=16_000)
    user_instructions: str | None = Field(default=None, max_length=8_000)
    conversation_id: str | None = None
    selected_model: str = "claude-opus-4-7"
    budget_usd: float = Field(default=5.0, ge=0.1, le=100.0)
    max_duration_hours: float = Field(default=2.0, ge=0.1, le=24.0)
    # F4 Vault: optional ephemeral env injection.  The frontend decrypts
    # vaulted secrets locally and sends only the names that the prompt
    # references, so the server holds plaintext only for the lifetime of
    # this single task.  Validated server-side; capped at 50 entries.
    vault_env: dict[str, str] | None = Field(default=None)


class AgentStartResponse(BaseModel):
    task_id: str
    state: str
    message: str = "Agent task enqueued."


class StopResponse(BaseModel):
    task_id: str
    stopped: bool
    message: str


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _insert_agent_task(db: Any, task: AgentTask) -> None:
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


async def _load_agent_task(db: Any, task_id: str) -> AgentTask | None:
    # Validate UUID format before hitting Postgres — otherwise asyncpg
    # raises InvalidTextRepresentation which bubbles up as a 500.
    try:
        uuid.UUID(task_id)
    except (ValueError, AttributeError, TypeError):
        return None
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, user_id, conversation_id, goal, user_instructions,
                   state, selected_model, steps, artifacts,
                   max_duration_hours, budget_usd, spent_usd,
                   max_fix_attempts_per_step, max_replans, replan_count, total_failures,
                   final_answer, stop_requested, error,
                   created_at, updated_at
            FROM agent_tasks
            WHERE id = $1
            """,
            task_id,
        )
    if row is None:
        return None

    steps = row["steps"]
    if isinstance(steps, str):
        steps = json.loads(steps)
    artifacts = row["artifacts"]
    if isinstance(artifacts, str):
        artifacts = json.loads(artifacts)

    data = {
        "id": str(row["id"]),
        "user_id": row["user_id"],
        "conversation_id": row["conversation_id"],
        "goal": row["goal"],
        "user_instructions": row["user_instructions"],
        "state": row["state"],
        "selected_model": row["selected_model"],
        "steps": steps or [],
        "artifacts": artifacts or [],
        "max_duration_hours": float(row["max_duration_hours"]),
        "budget_usd": float(row["budget_usd"]),
        "spent_usd": float(row["spent_usd"]),
        "max_fix_attempts_per_step": int(row["max_fix_attempts_per_step"]),
        "max_replans": int(row["max_replans"]),
        "replan_count": int(row["replan_count"]),
        "total_failures": int(row["total_failures"]),
        "final_answer": row["final_answer"],
        "stop_requested": bool(row["stop_requested"]),
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    return AgentTask.model_validate(data)


async def _enqueue_agent_task(redis: Any, task_id: str) -> None:
    """Push task id onto the Redis queue that the orchestrator pops."""
    if redis is None:
        # In dev without Redis, the orchestrator is not running either;
        # the caller will have to run the loop inline.
        return
    await redis.rpush("agent:queue", task_id)


# ---------------------------------------------------------------------------
# Router factory (parameterised by auth dep)
# ---------------------------------------------------------------------------


def make_routes(
    *,
    get_current_user,
    get_db,
    get_redis,
    get_stream_user,
    mint_stream_token=None,
    verify_stream_token=None,
) -> APIRouter:
    """Build the agent APIRouter with auth/db dependencies injected.

    The api.py module calls this once during import and does
    ``app.include_router(result)``.

    ``mint_stream_token(user_id, task_id) -> str`` and
    ``verify_stream_token(token, task_id) -> str`` are optional.  When
    provided, the SSE endpoint uses a short-lived stream token instead of
    the full JWT (B-09 fix).
    """
    r = APIRouter(prefix="/api", tags=["Agent"])

    # -- GET /api/agent/about -------------------------------------------
    # Public (auth still required for consistency) capability description.
    # Backed by mariana.agent.self_knowledge so the prompt / dispatcher /
    # frontend stay in sync.
    @r.get("/agent/about")
    async def agent_about(
        current_user: dict = Depends(get_current_user),
    ) -> dict:
        from mariana.agent.self_knowledge import describe_self_payload
        return describe_self_payload()

    # -- GET /api/agent  (task inbox) -----------------------------------
    # v3: paginated list of the caller's own agent tasks, newest first.
    # Supports filtering by state so the frontend can show "Running",
    # "Needs approval", and "History" tabs without spawning one query per
    # task row.
    @r.get("/agent")
    async def list_agent_tasks(
        state: str | None = Query(None, max_length=32),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0, le=10_000),
        current_user: dict = Depends(get_current_user),
    ) -> dict:
        db = get_db()
        # Build parameterised query.  State filter is optional.
        where = "user_id = $1"
        params: list[Any] = [current_user["user_id"]]
        if state:
            where += " AND state = $2"
            params.append(state)
        params.extend([limit, offset])
        lim_placeholder = f"${len(params) - 1}"
        off_placeholder = f"${len(params)}"
        sql = (
            "SELECT id, goal, state, selected_model, budget_usd, spent_usd, "
            "replan_count, total_failures, error, final_answer, "
            "created_at, updated_at FROM agent_tasks WHERE " + where
            + f" ORDER BY created_at DESC LIMIT {lim_placeholder} OFFSET {off_placeholder}"
        )
        count_sql = "SELECT COUNT(*) AS n FROM agent_tasks WHERE " + where
        async with db.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            total_row = await conn.fetchrow(count_sql, *params[: 2 if state else 1])
        return {
            "total": int(total_row["n"]) if total_row else 0,
            "limit": limit,
            "offset": offset,
            "tasks": [
                {
                    "id": str(row["id"]),
                    "goal": row["goal"],
                    "state": row["state"],
                    "selected_model": row["selected_model"],
                    "budget_usd": float(row["budget_usd"]),
                    "spent_usd": float(row["spent_usd"]),
                    "replan_count": int(row["replan_count"]),
                    "total_failures": int(row["total_failures"]),
                    "error": row["error"],
                    "has_final_answer": bool(row["final_answer"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ],
        }

    # -- GET /api/agent/{task_id}/approvals -----------------------------
    # v3: scan the event log for `approval_requested` events that have no
    # matching `approval_resolved` event.  Returns one entry per pending
    # approval so the frontend can render the approval queue.
    @r.get("/agent/{task_id}/approvals")
    async def list_pending_approvals(
        task_id: str,
        current_user: dict = Depends(get_current_user),
    ) -> dict:
        db = get_db()
        task = await _load_agent_task(db, task_id)
        if task is None:
            raise HTTPException(404, f"agent task {task_id} not found")
        if task.user_id != current_user["user_id"]:
            raise HTTPException(403, "not your task")
        async with db.acquire() as conn:
            req_rows = await conn.fetch(
                "SELECT id, payload, created_at FROM agent_events "
                "WHERE task_id = $1 AND event_type = 'approval_requested' "
                "ORDER BY id ASC LIMIT 500",
                task_id,
            )
            res_rows = await conn.fetch(
                "SELECT payload FROM agent_events "
                "WHERE task_id = $1 AND event_type = 'approval_resolved' "
                "ORDER BY id ASC LIMIT 1000",
                task_id,
            )
        resolved: set[str] = set()
        for row in res_rows:
            p = row["payload"]
            if isinstance(p, str):
                try:
                    p = json.loads(p)
                except Exception:
                    p = {}
            aid = (p or {}).get("approval_id")
            if aid:
                resolved.add(str(aid))
        pending: list[dict[str, Any]] = []
        for row in req_rows:
            p = row["payload"]
            if isinstance(p, str):
                try:
                    p = json.loads(p)
                except Exception:
                    p = {}
            aid = (p or {}).get("approval_id") or f"evt-{row['id']}"
            if str(aid) in resolved:
                continue
            pending.append({
                "approval_id": str(aid),
                "event_id": int(row["id"]),
                "requested_at": row["created_at"],
                "summary": (p or {}).get("summary", ""),
                "tool": (p or {}).get("tool", ""),
                "params": (p or {}).get("params", {}),
                "tier": (p or {}).get("tier", "B"),
            })
        return {"task_id": task_id, "count": len(pending), "approvals": pending}

    # -- POST /api/agent/{task_id}/approvals/{approval_id}/decide -------
    @r.post("/agent/{task_id}/approvals/{approval_id}/decide")
    async def decide_approval(
        task_id: str,
        approval_id: str,
        body: dict[str, Any],
        current_user: dict = Depends(get_current_user),
    ) -> dict:
        decision = str(body.get("decision", "")).lower()
        if decision not in ("approve", "deny"):
            raise HTTPException(422, "decision must be 'approve' or 'deny'")
        db = get_db()
        task = await _load_agent_task(db, task_id)
        if task is None:
            raise HTTPException(404, f"agent task {task_id} not found")
        if task.user_id != current_user["user_id"]:
            raise HTTPException(403, "not your task")
        payload = {
            "approval_id": approval_id,
            "decision": decision,
            "decided_by": current_user["user_id"],
            "note": str(body.get("note", ""))[:2000],
        }
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_events (task_id, event_type, state, payload) "
                "VALUES ($1, 'approval_resolved', $2, $3::jsonb)",
                task_id, task.state.value, json.dumps(payload),
            )
        # Signal the orchestrator via Redis pub/sub so a blocked task can
        # resume.  No-op if redis is unavailable.
        try:
            redis = get_redis()
            if redis is not None:
                await redis.publish(f"agent:approval:{task_id}", json.dumps(payload))
        except Exception as exc:
            logger.warning("approval_publish_failed", error=str(exc))
        return {"task_id": task_id, "approval_id": approval_id, "decision": decision}

    # -- POST /api/agent ------------------------------------------------
    @r.post("/agent", response_model=AgentStartResponse, status_code=202)
    async def start_agent_task(
        body: AgentStartRequest,
        current_user: dict = Depends(get_current_user),
    ) -> AgentStartResponse:
        db = get_db()
        task_id = str(uuid.uuid4())

        # F4 Vault: validate vault_env early so a malformed payload returns 422
        # before we burn credits or write a task row.
        from mariana.vault.runtime import (  # noqa: PLC0415
            validate_vault_env,
            store_vault_env,
        )
        try:
            vault_env_validated = validate_vault_env(body.vault_env or {})
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"vault_env: {exc}")

        # M-01 fix: Reserve credits before enqueueing using the canonical
        # platform conversion of **100 credits per $1** (i.e. 1 credit ==
        # $0.01).  This matches ``frontend/src/components/deft/studio/stage.ts``
        # ``creditsFromUsd`` and the Pricing page copy ("1c = $0.01"), and it
        # matches the agent runtime's enforcement: ``mariana/agent/loop.py``
        # halts at ``spent_usd >= budget_usd`` measured in the same dollars
        # the user paid for.  The previous formula ``max(200, budget_usd*500)``
        # over-collected by 5x because the runtime/UI ceiling is the
        # 100c/USD value but the reservation used 500c/USD.
        #
        # Settled at task completion (refund unused, deduct overage) by
        # ``mariana/agent/loop.py:_settle_agent_credits`` once the task
        # reaches a terminal state (DONE / FAILED / HALTED).  The narrow
        # pre-enqueue insert-failure refund below is kept for the case where
        # the row never makes it into Postgres (so the loop never runs).
        # Late import avoids the circular dependency between api.py and
        # this module.
        reserved_credits = 0
        try:
            from mariana.api import (  # noqa: PLC0415
                _get_config as _get_cfg,
                _supabase_deduct_credits as _deduct,
                _supabase_add_credits as _refund,
            )
            cfg = _get_cfg()
            # Canonical: 100 credits per $1 of budget, with a 100-credit
            # floor for sub-$1 tasks so we cover the planner round-trip.
            reserved_credits = max(100, int(body.budget_usd * 100))
            if cfg.SUPABASE_URL and cfg.SUPABASE_ANON_KEY:
                result = await _deduct(current_user["user_id"], reserved_credits, cfg)
                if result == "insufficient":
                    raise HTTPException(
                        status_code=402,
                        detail=(
                            f"Insufficient credits. This task needs ~{reserved_credits} credits "
                            f"(at 100 credits/$ canonical conversion). "
                            f"Subscribe or upgrade your plan on the Pricing page."
                        ),
                    )
                if result == "error":
                    # Don't block on transient Supabase errors. Log and continue.
                    logger.warning("agent_credits_deduct_error", user_id=current_user["user_id"])
                    reserved_credits = 0
        except HTTPException:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("agent_credits_reserve_skipped", error=str(exc))
            reserved_credits = 0

        task = AgentTask(
            id=task_id,
            user_id=current_user["user_id"],
            conversation_id=body.conversation_id,
            goal=body.goal,
            user_instructions=body.user_instructions,
            selected_model=body.selected_model,
            budget_usd=body.budget_usd,
            max_duration_hours=body.max_duration_hours,
            state=AgentState.PLAN,
            # M-01: hand the reservation amount to the runtime so the loop
            # can settle it (refund unused, deduct overage) at terminal
            # state.  ``credits_settled`` defaults to False on a fresh task.
            reserved_credits=reserved_credits,
        )

        # Refund-on-DB-failure guard: if the INSERT blows up, credits are
        # returned immediately so a crash never silently charges a user.
        try:
            await _insert_agent_task(db, task)
        except Exception as insert_exc:
            if reserved_credits > 0:
                try:
                    from mariana.api import (  # noqa: PLC0415
                        _get_config as _get_cfg2,
                        _supabase_add_credits as _refund2,
                    )
                    await _refund2(current_user["user_id"], reserved_credits, _get_cfg2())
                    logger.info(
                        "agent_credits_refunded_on_insert_failure",
                        user_id=current_user["user_id"],
                        amount=reserved_credits,
                    )
                except Exception as refund_exc:  # pragma: no cover
                    logger.error(
                        "agent_credits_refund_failed",
                        user_id=current_user["user_id"],
                        amount=reserved_credits,
                        error=str(refund_exc),
                    )
            raise insert_exc

        # Enqueue.  If no redis, orchestrator isn't running — still return 202
        # so frontend can display the "pending" state and retry later.
        redis = None
        try:
            redis = get_redis()
        except Exception:
            redis = None

        # F4 Vault: stash vault_env in Redis under vault:env:{task_id} with a
        # TTL that matches the task's max wall-clock budget plus a small
        # buffer so the loop can read it on first cold start.  We do this
        # BEFORE enqueueing so the consumer never picks up a task whose
        # secrets aren't yet in place.
        if vault_env_validated:
            ttl_seconds = int(body.max_duration_hours * 3600) + 300
            try:
                await store_vault_env(redis, task_id, vault_env_validated, ttl_seconds=ttl_seconds)
            except Exception as exc:
                logger.warning("vault_env_store_failed", task_id=task_id, error=str(exc))

        try:
            if redis is not None:
                await _enqueue_agent_task(redis, task_id)
        except Exception as exc:
            logger.warning("agent_enqueue_failed", task_id=task_id, error=str(exc))

        return AgentStartResponse(task_id=task_id, state=task.state.value)

    # -- GET /api/agent/{task_id} ---------------------------------------
    @r.get("/agent/{task_id}")
    async def get_agent_task(
        task_id: str,
        current_user: dict = Depends(get_current_user),
    ) -> dict:
        db = get_db()
        task = await _load_agent_task(db, task_id)
        if task is None:
            raise HTTPException(404, f"agent task {task_id} not found")
        if task.user_id != current_user["user_id"]:
            raise HTTPException(403, "not your task")
        return task.model_dump(mode="json")

    # -- GET /api/agent/{task_id}/events -------------------------------
    @r.get("/agent/{task_id}/events")
    async def get_agent_events(
        task_id: str,
        after_id: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
        current_user: dict = Depends(get_current_user),
    ) -> dict:
        db = get_db()
        task = await _load_agent_task(db, task_id)
        if task is None:
            raise HTTPException(404, f"agent task {task_id} not found")
        if task.user_id != current_user["user_id"]:
            raise HTTPException(403, "not your task")
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, event_type, state, step_id, payload, created_at
                FROM agent_events
                WHERE task_id = $1 AND id > $2
                ORDER BY id ASC
                LIMIT $3
                """,
                task_id, after_id, limit,
            )
        out = []
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            out.append({
                "id": int(row["id"]),
                "event_type": row["event_type"],
                "state": row["state"],
                "step_id": row["step_id"],
                "payload": payload,
                "created_at": row["created_at"].isoformat(),
            })
        return {"events": out, "next_after_id": out[-1]["id"] if out else after_id}

    # -- POST /api/agent/{task_id}/stream-token (B-09) --------------------
    # Mint a short-lived HMAC-signed stream token so the SSE client never
    # places the full Supabase JWT in the URL query string.
    @r.post("/agent/{task_id}/stream-token")
    async def mint_agent_stream_token(
        task_id: str,
        current_user: dict = Depends(get_current_user),
    ) -> dict:
        """Issue a short-lived stream token for the agent SSE endpoint.

        B-09: the SSE EventSource URL must never contain the raw Supabase JWT.
        Clients call this first, then pass the returned token as ?token=.
        Returns 501 if the server has no mint_stream_token implementation,
        which the client treats as a permanent error (no JWT fallback).
        """
        if mint_stream_token is None:
            raise HTTPException(
                status_code=501,
                detail="stream-token mint not configured on this server",
            )
        db = get_db()
        task = await _load_agent_task(db, task_id)
        if task is None:
            raise HTTPException(404, f"agent task {task_id} not found")
        if task.user_id != current_user["user_id"]:
            raise HTTPException(403, "not your task")
        token = mint_stream_token(current_user["user_id"], task_id)
        return {"stream_token": token, "expires_in_seconds": 120}

    # B-09: Build a stream-token-aware authenticator for the SSE endpoint.
    # When verify_stream_token is provided, a ?token= param is treated as a
    # signed stream token (not a raw JWT).  Falls back to get_stream_user
    # (Bearer header or raw JWT) only when verify_stream_token is absent.
    # TODO B-09-FOLLOWUP: remove the raw-JWT fallback path once all clients
    # use the stream-token mint flow.
    async def _authenticate_sse(
        task_id: str,
        authorization: str | None = Header(None),
        token: str | None = Query(None),
    ) -> dict:
        """Authenticate agent SSE requests — prefer stream token over raw JWT."""
        if token and verify_stream_token is not None:
            # Fast path: validate the short-lived HMAC-signed stream token.
            # HTTPException propagates automatically on invalid/expired token.
            user_id = verify_stream_token(token, task_id)
            return {"user_id": user_id}
        # Fallback: raw JWT in Authorization header (or legacy ?token= JWT).
        # This path will be removed in B-09-FOLLOWUP once all callers mint.
        return await get_stream_user(authorization=authorization, token=token)  # type: ignore[call-arg]

    # -- GET /api/agent/{task_id}/stream (SSE) --------------------------
    @r.get("/agent/{task_id}/stream")
    async def stream_agent_events(
        task_id: str,
        current_user: dict = Depends(_authenticate_sse),
    ) -> StreamingResponse:
        db = get_db()
        task = await _load_agent_task(db, task_id)
        if task is None:
            raise HTTPException(404, f"agent task {task_id} not found")
        if task.user_id != current_user["user_id"]:
            raise HTTPException(403, "not your task")

        redis = None
        try:
            redis = get_redis()
        except Exception:
            redis = None

        if redis is None:
            # Degraded mode — one-shot snapshot.
            async def single_shot() -> AsyncIterator[bytes]:
                yield _sse_msg("snapshot", task.model_dump(mode="json"))
                yield _sse_msg("eof", {"reason": "redis_unavailable"})
            return StreamingResponse(single_shot(), media_type="text/event-stream")

        async def gen() -> AsyncIterator[bytes]:
            # 1) Replay recent events from DB so the frontend can rebuild.
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, event_type, state, step_id, payload
                    FROM agent_events
                    WHERE task_id = $1
                    ORDER BY id ASC
                    """,
                    task_id,
                )
            for row in rows:
                payload = row["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                yield _sse_msg(row["event_type"], {
                    "task_id": task_id,
                    "event_type": row["event_type"],
                    "state": row["state"],
                    "step_id": row["step_id"],
                    "payload": payload,
                    "event_id": int(row["id"]),
                    "replay": True,
                })

            # 2) Live stream via Redis XREAD.
            stream_key = f"agent:{task_id}:events"
            last_id = "$"
            idle_ticks = 0
            while True:
                try:
                    msgs = await redis.xread({stream_key: last_id}, block=5_000, count=50)
                except Exception as exc:
                    logger.warning("agent_sse_xread_error", task_id=task_id, error=str(exc))
                    yield _sse_msg("error", {"error": str(exc)})
                    break
                if not msgs:
                    idle_ticks += 1
                    # Heartbeat every ~5s, plus check terminal state every 6 ticks (~30s).
                    yield b": ping\n\n"
                    if idle_ticks % 6 == 0:
                        latest = await _load_agent_task(db, task_id)
                        if latest and latest.state in (
                            AgentState.DONE, AgentState.FAILED, AgentState.HALTED,
                        ):
                            yield _sse_msg("eof", {"final_state": latest.state.value})
                            break
                    continue
                idle_ticks = 0
                for _key, entries in msgs:
                    for entry_id, data in entries:
                        last_id = entry_id
                        raw = data.get("data") or data.get(b"data") or "{}"
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        try:
                            obj = json.loads(raw)
                        except Exception:
                            obj = {"raw": raw[:4000]}
                        evt_type = obj.get("event_type", "message")
                        yield _sse_msg(evt_type, obj)
                        if evt_type in ("delivered", "halted") or obj.get("state") in (
                            AgentState.DONE.value, AgentState.FAILED.value, AgentState.HALTED.value,
                        ):
                            yield _sse_msg("eof", {"final_state": obj.get("state")})
                            return

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # -- POST /api/agent/{task_id}/stop ---------------------------------
    @r.post("/agent/{task_id}/stop", response_model=StopResponse)
    async def stop_agent_task(
        task_id: str,
        current_user: dict = Depends(get_current_user),
    ) -> StopResponse:
        db = get_db()
        task = await _load_agent_task(db, task_id)
        if task is None:
            raise HTTPException(404, f"agent task {task_id} not found")
        if task.user_id != current_user["user_id"]:
            raise HTTPException(403, "not your task")

        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE agent_tasks SET stop_requested = TRUE, updated_at = now() WHERE id = $1",
                task_id,
            )
        redis = None
        try:
            redis = get_redis()
        except Exception:
            redis = None
        if redis is not None:
            try:
                await redis.set(f"agent:{task_id}:stop", "1", ex=3600)
            except Exception:
                pass
        return StopResponse(task_id=task_id, stopped=True, message="stop requested")

    # -- GET /api/agent/{task_id}/artifacts --------------------------------
    # Convenience endpoint that returns the artefact manifest for a task
    # without having to pull the whole task JSON.  Powers the v3 Artifact
    # Gallery on the frontend.
    @r.get("/agent/{task_id}/artifacts")
    async def list_task_artifacts(
        task_id: str,
        current_user: dict = Depends(get_current_user),
    ) -> dict:
        db = get_db()
        task = await _load_agent_task(db, task_id)
        if task is None:
            raise HTTPException(404, f"agent task {task_id} not found")
        if task.user_id != current_user["user_id"]:
            raise HTTPException(403, "not your task")
        return {
            "task_id": task_id,
            "count": len(task.artifacts),
            "artifacts": [a.model_dump(mode="json") for a in task.artifacts],
        }

    # -- GET /api/workspace/{user_id}  (list) ----------------------------
    @r.get("/workspace/{user_id}")
    async def list_workspace(
        user_id: str,
        path: str = Query("", max_length=4096),
        current_user: dict = Depends(get_current_user),
    ) -> dict:
        if current_user["user_id"] != user_id:
            raise HTTPException(403, "cannot list another user's workspace")
        try:
            return await sandbox_tools.fs_list(
                user_id=user_id, path=path, recursive=True, max_entries=2000,
            )
        except sandbox_tools.SandboxError as exc:
            raise HTTPException(503, f"sandbox unavailable: {exc}") from exc

    # -- GET /api/workspace/{user_id}/file  (download) ------------------
    @r.get("/workspace/{user_id}/file")
    async def read_workspace_file(
        user_id: str,
        path: str = Query(..., min_length=1, max_length=4096),
        binary: bool = Query(True),
        current_user: dict = Depends(get_current_user),
    ):
        if current_user["user_id"] != user_id:
            raise HTTPException(403, "cannot read another user's workspace")
        try:
            result = await sandbox_tools.fs_read(
                user_id=user_id, path=path, binary=binary, max_bytes=10 * 1024 * 1024,
            )
        except sandbox_tools.SandboxError as exc:
            raise HTTPException(404, f"file error: {exc}") from exc

        if binary and "content_b64" in result:
            data = base64.b64decode(result["content_b64"])
            fname = os.path.basename(path) or "file.bin"
            return StreamingResponse(
                iter([data]),
                media_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{fname}"'},
            )
        return JSONResponse(result)

    return r


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sse_msg(event: str, data: dict) -> bytes:
    """Format an SSE frame.  Stable encoding, ASCII-safe.

    We emit every frame under the default ``message`` event so the browser's
    ``EventSource.onmessage`` handler fires for all of them.  The event kind
    is embedded in ``data.event_type`` for dispatch on the client.  Without
    this, a frame like ``event: step_started`` would only fire listeners
    registered via ``addEventListener('step_started', …)`` — never
    ``onmessage`` — which makes the SSE API fragile.
    """
    enriched = dict(data)
    enriched.setdefault("event_type", event)
    body = json.dumps(enriched, ensure_ascii=True, default=str)
    return f"data: {body}\n\n".encode("utf-8")
