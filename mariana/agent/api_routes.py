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
from fastapi import APIRouter, Depends, HTTPException, Query
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
    selected_model: str = "claude-opus-4-7-20260208"
    budget_usd: float = Field(default=5.0, ge=0.1, le=100.0)
    max_duration_hours: float = Field(default=2.0, ge=0.1, le=24.0)


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


def make_routes(*, get_current_user, get_db, get_redis, get_stream_user) -> APIRouter:
    """Build the agent APIRouter with auth/db dependencies injected.

    The api.py module calls this once during import and does
    ``app.include_router(result)``.
    """
    r = APIRouter(prefix="/api", tags=["Agent"])

    # -- POST /api/agent ------------------------------------------------
    @r.post("/agent", response_model=AgentStartResponse, status_code=202)
    async def start_agent_task(
        body: AgentStartRequest,
        current_user: dict = Depends(get_current_user),
    ) -> AgentStartResponse:
        db = get_db()
        task_id = str(uuid.uuid4())
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
        )
        await _insert_agent_task(db, task)

        # Enqueue.  If no redis, orchestrator isn't running — still return 202
        # so frontend can display the "pending" state and retry later.
        redis = None
        try:
            redis = get_redis()
        except Exception:
            redis = None
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

    # -- GET /api/agent/{task_id}/stream (SSE) --------------------------
    @r.get("/agent/{task_id}/stream")
    async def stream_agent_events(
        task_id: str,
        current_user: dict = Depends(get_stream_user),  # supports ?token= or Bearer
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
                user_id=user_id, path=path, binary=binary, max_bytes=16 * 1024 * 1024,
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
    """Format an SSE frame.  Stable encoding, ASCII-safe."""
    body = json.dumps(data, ensure_ascii=True, default=str)
    return f"event: {event}\ndata: {body}\n\n".encode("utf-8")
