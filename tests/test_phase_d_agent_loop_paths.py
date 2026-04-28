"""Phase D coverage-fill: agent-loop behavioural paths not covered by CC-01.

CC-01 pinned the planner/stop/budget/replan-cap/clamp/vault surface.  This
file fills the remaining behavioural cold spots in
``mariana.agent.loop.run_agent_task`` so any future regression on these
paths trips a direct test:

  1. ``test_phase_d_replan_exhaustion_yields_unrecoverable``
        ``replan_count == max_replans`` after a step fails its fix budget
        → the loop transitions to FAILED with ``task.error =
        "unrecoverable"``.  Pin the canonical task error code on the
        terminal failure.

  2. ``test_phase_d_deliver_step_failure_marks_deliver_failed``
        The next pending step is a ``deliver`` whose dispatch fails →
        ``task.error = "deliver_failed"``, state = FAILED.  No half-
        delivered DONE row that the UI would render as success.

  3. ``test_phase_d_stop_mid_execute_halts_cleanly``
        Stop key flips to truthy AFTER planner runs but BEFORE the first
        step finishes → loop transitions to HALTED with
        ``task.error = "stop_requested"`` and the planner's prepared step
        is left PENDING.

  4. ``test_phase_d_stop_pre_plan_sets_stop_requested_error``
        Cancel before planner runs → HALTED with ``task.error =
        "stop_requested"`` (CC-01 #2 pinned the *transition*; this pins
        the canonical task error code on the persisted task).

  5. ``test_phase_d_budget_exhaustion_mid_execute``
        After plan+1 step ``spent_usd`` exceeds ``budget_usd`` →
        canonical ``budget_exhausted`` code AND a ``halted`` event with a
        structured ``detail`` payload carrying the numeric context.

  6. ``test_phase_d_duration_exhaustion_mid_execute``
        Wallclock guard trips after plan → canonical ``duration_exhausted``
        code, structured detail payload with ``elapsed_hours`` /
        ``max_duration_hours``.

  7-10. ``test_phase_d_infer_failure_*``
        Each ``_infer_failure`` branch returns the canonical step code:
        timed_out / process_killed / non_zero_exit / http_error.  Plus a
        success-path control test that returns ``None``.

  11. ``test_phase_d_infer_failure_does_not_misfire_on_unrelated_tool``
        ``http_error`` semantics must not leak to non-browser tools, and
        the ``timed_out`` / ``killed`` / ``exit_code`` fields must not
        leak to tools outside the exec family.
"""

from __future__ import annotations

import os
import pathlib
import time as _time
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


PGHOST = os.environ.get("PGHOST", "/tmp")
PGPORT = int(os.environ.get("PGPORT", "55432"))
PGUSER = os.environ.get("PGUSER", "postgres")
PGDATABASE = os.environ.get("PGDATABASE", "testdb")

try:
    import asyncpg  # type: ignore  # noqa: F401
    import psycopg2  # type: ignore

    _conn = psycopg2.connect(
        host=PGHOST, port=PGPORT, user=PGUSER, dbname=PGDATABASE
    )
    _conn.close()
    _PG_AVAILABLE = True
except Exception:
    _PG_AVAILABLE = False

_pg_only = pytest.mark.skipif(not _PG_AVAILABLE, reason="Local PG not available")


_AGENT_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "mariana"
    / "agent"
    / "schema.sql"
)


async def _open_pool():
    import asyncpg as _asyncpg  # noqa: PLC0415

    return await _asyncpg.create_pool(
        host=PGHOST,
        port=PGPORT,
        user=PGUSER,
        database=PGDATABASE,
        min_size=1,
        max_size=4,
    )


async def _ensure_schema(pool: Any) -> None:
    sql = _AGENT_SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)


def _new_task(
    *,
    reserved: int = 0,
    settled: bool = False,
    spent_usd: float = 0.0,
    budget_usd: float = 5.0,
    max_duration_hours: float = 2.0,
    state=None,
):
    from mariana.agent.models import AgentState, AgentTask  # noqa: PLC0415

    task = AgentTask(
        id=str(uuid.uuid4()),
        user_id=f"user-pd-loop-{uuid.uuid4().hex[:8]}",
        goal="Phase D agent-loop coverage fill",
        budget_usd=budget_usd,
        spent_usd=spent_usd,
        max_duration_hours=max_duration_hours,
        state=state or AgentState.PLAN,
    )
    task.reserved_credits = reserved
    task.credits_settled = settled
    return task


def _mk_step(
    *, step_id: str = "s-1", tool: str = "code_exec", params: dict | None = None
):
    from mariana.agent.models import AgentStep  # noqa: PLC0415

    return AgentStep(
        id=step_id,
        title=f"Phase D step {step_id}",
        tool=tool,
        params=params if params is not None else {"code": "print(1)"},
    )


# ---------------------------------------------------------------------------
# 1. Replan exhaustion → unrecoverable.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_phase_d_replan_exhaustion_yields_unrecoverable():
    """A step that fails its fix budget AND then fails the replan budget
    must end the task FAILED with ``task.error == "unrecoverable"``.

    The loop already has a unit test for the replan-cap return value
    (CC-01 #6).  This test pins the OUTER-LOOP behaviour: when the replan
    is refused, the loop persists ``unrecoverable`` and stops.
    """
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import planner as planner_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=0, spent_usd=0.0)
        # Cap replan budget so a SINGLE failed step exhausts replans.
        task.max_replans = 0
        task.max_fix_attempts_per_step = 1
        await _insert_agent_task(pool, task)

        # Initial plan: a single code_exec step that will fail.
        step = _mk_step(step_id="rx-1")
        plan_mock = AsyncMock(return_value=([step], 0.0))

        async def bad_dispatch(*a, **kw):
            raise RuntimeError("kernel panic")

        record_mock = AsyncMock()

        with (
            patch.object(planner_mod, "build_initial_plan", plan_mock),
            patch.object(loop_mod, "dispatch", bad_dispatch),
            patch.object(loop_mod, "_record_event", record_mock),
            patch.object(loop_mod, "_settle_agent_credits", AsyncMock()),
        ):
            result = await loop_mod.run_agent_task(task, db=pool, redis=None)
    finally:
        await pool.close()

    assert result.state == AgentState.FAILED, (
        f"step that exhausts replan budget must end FAILED; got {result.state.value!r}"
    )
    assert result.error == "unrecoverable", (
        f"replan exhaustion must persist canonical 'unrecoverable'; got {result.error!r}"
    )


# ---------------------------------------------------------------------------
# 2. Deliver step dispatch failure → deliver_failed.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_phase_d_deliver_step_failure_marks_deliver_failed():
    """Plan returns a single ``deliver`` step whose dispatch fails →
    ``task.error == "deliver_failed"`` and ``state == FAILED``.  The
    loop must NOT fall back to ``_deliver`` (which would mark the task
    DONE)."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import planner as planner_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=0, spent_usd=0.0)
        await _insert_agent_task(pool, task)

        # The deliver branch transitions ``task.state`` -> DELIVER directly.
        # Legal predecessors of DELIVER include EXECUTE — we simulate the
        # production sequence by having the plan return [normal_exec,
        # deliver].  The normal exec dispatches successfully (state moves
        # PLAN -> EXECUTE), then the deliver step is picked and its
        # dispatch raises.
        normal_step = _mk_step(step_id="e-1", tool="code_exec")
        deliver_step = _mk_step(
            step_id="d-1",
            tool="deliver",
            params={"final_answer": "done"},
        )
        plan_mock = AsyncMock(return_value=([normal_step, deliver_step], 0.0))

        async def selective_dispatch(tool, *a, **kw):
            if tool == "deliver":
                raise RuntimeError("deliver kaboom")
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        with (
            patch.object(planner_mod, "build_initial_plan", plan_mock),
            patch.object(loop_mod, "dispatch", selective_dispatch),
            patch.object(loop_mod, "_record_event", AsyncMock()),
            patch.object(loop_mod, "_settle_agent_credits", AsyncMock()),
        ):
            result = await loop_mod.run_agent_task(task, db=pool, redis=None)
    finally:
        await pool.close()

    assert result.state == AgentState.FAILED, (
        f"deliver-step failure must end FAILED, NOT DONE; got {result.state.value!r}"
    )
    assert result.error == "deliver_failed", (
        f"task.error must be canonical 'deliver_failed'; got {result.error!r}"
    )
    # No final_answer must have been synthesised — the deliver branch
    # bails BEFORE _deliver runs.
    assert (result.final_answer or "") == "", (
        "a failed deliver step must NOT leave a synthesised final_answer "
        "(would render as success in the UI)"
    )


# ---------------------------------------------------------------------------
# 3. Stop mid-EXECUTE: planner runs, first step starts, stop trips, halt.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_phase_d_stop_mid_execute_halts_cleanly():
    """Plan succeeds, the loop enters EXECUTE, then a stop is requested
    in the gate at the top of the EXECUTE loop → HALTED with canonical
    ``stop_requested``.  Pin: no spent_usd is added by tool dispatch
    after the stop (dispatch is never called)."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import planner as planner_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=0, spent_usd=0.0)
        await _insert_agent_task(pool, task)

        # The loop calls redis.get for: (a) the vault_env fetch BEFORE plan,
        # (b) the pre-plan _check_stop_requested gate, then (c) the
        # top-of-EXECUTE-loop _check_stop_requested gate.  We need (a) and
        # (b) to return None / no-stop and (c) to return a truthy stop
        # marker so the planner runs but the first step is pre-empted.
        class _FlipRedis:
            def __init__(self) -> None:
                self.gets = 0
                self.keys_seen: list[str] = []

            async def get(self, key):
                self.gets += 1
                self.keys_seen.append(str(key))
                # Vault key always returns None (no vaulted secrets).
                if "vault:env" in str(key):
                    return None
                # Stop key: first hit (pre-plan) -> no stop; subsequent
                # hits (mid-EXECUTE) -> stop.
                stop_hits = sum(1 for k in self.keys_seen if "stop" in k)
                return b"1" if stop_hits >= 2 else None

            async def xadd(self, *_a, **_kw):
                return None

            async def delete(self, *keys):
                return 0

        redis = _FlipRedis()
        step = _mk_step(step_id="exec-1")
        plan_mock = AsyncMock(return_value=([step], 0.05))
        dispatch_mock = AsyncMock(return_value={"exit_code": 0})

        with (
            patch.object(planner_mod, "build_initial_plan", plan_mock),
            patch.object(loop_mod, "dispatch", dispatch_mock),
            patch.object(loop_mod, "_record_event", AsyncMock()),
            patch.object(loop_mod, "_settle_agent_credits", AsyncMock()),
        ):
            result = await loop_mod.run_agent_task(task, db=pool, redis=redis)
    finally:
        await pool.close()

    assert plan_mock.await_count == 1, "planner must have run before the stop"
    assert dispatch_mock.await_count == 0, (
        "dispatch must NOT run for a step that was pre-empted by a stop"
    )
    assert result.state == AgentState.HALTED, (
        f"mid-EXECUTE stop must HALT; got {result.state.value!r}"
    )
    assert result.error == "stop_requested", (
        f"task.error must be canonical 'stop_requested'; got {result.error!r}"
    )
    # Planner cost is preserved (the planner already ran; we billed for it).
    assert result.spent_usd == pytest.approx(0.05), (
        "planner cost must remain on the task; only dispatch was skipped"
    )


# ---------------------------------------------------------------------------
# 4. Pre-plan stop: persisted task.error must be canonical stop_requested.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_phase_d_stop_pre_plan_persists_canonical_error_code():
    """CC-01 #2 pinned the *transition* (HALTED, planner not called).  This
    pins the canonical TASK ERROR CODE in addition: ``task.error`` must
    persist as the canonical ``stop_requested`` value (NOT raw exception
    text and NOT None)."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import planner as planner_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=0)
        await _insert_agent_task(pool, task)

        class _StopRedis:
            async def get(self, key):
                return b"1"

            async def xadd(self, *_a, **_kw):
                return None

            async def delete(self, *keys):
                return 0

        with (
            patch.object(planner_mod, "build_initial_plan", AsyncMock()),
            patch.object(loop_mod, "dispatch", AsyncMock()),
            patch.object(loop_mod, "_record_event", AsyncMock()),
            patch.object(loop_mod, "_settle_agent_credits", AsyncMock()),
        ):
            result = await loop_mod.run_agent_task(task, db=pool, redis=_StopRedis())
    finally:
        await pool.close()

    assert result.state == AgentState.HALTED
    assert result.error == "stop_requested", (
        "pre-plan stop must persist canonical 'stop_requested' on task.error; "
        f"got {result.error!r}"
    )
    assert result.error in loop_mod.CANONICAL_TASK_ERROR_CODES


# ---------------------------------------------------------------------------
# 5. Budget exhaustion mid-execution → halted + canonical code +
#    structured detail payload.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_phase_d_budget_exhaustion_mid_execute_emits_structured_detail():
    """Planner returns a step + cost that pushes ``spent_usd`` over
    ``budget_usd``.  The loop must:

    * persist canonical ``task.error == "budget_exhausted"``
    * emit a ``halted`` event with payload ``{"reason": "budget_exhausted",
      "detail": {"spent_usd": float, "budget_usd": float}}``
    * end in HALTED.
    """
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import planner as planner_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        # Tight budget so the planner cost alone breaks it.
        task = _new_task(reserved=0, spent_usd=0.0, budget_usd=0.50)
        await _insert_agent_task(pool, task)

        step = _mk_step(step_id="bx-1")
        plan_mock = AsyncMock(return_value=([step], 0.60))  # cost > budget

        events: list[tuple[str, dict]] = []

        async def _capture_emit(db, redis, task, kind, *, step_id=None, payload=None):
            events.append((kind, dict(payload or {})))

        with (
            patch.object(planner_mod, "build_initial_plan", plan_mock),
            patch.object(loop_mod, "_emit", _capture_emit),
            patch.object(loop_mod, "_record_event", AsyncMock()),
            patch.object(loop_mod, "_settle_agent_credits", AsyncMock()),
        ):
            result = await loop_mod.run_agent_task(task, db=pool, redis=None)
    finally:
        await pool.close()

    assert result.state == AgentState.HALTED
    assert result.error == "budget_exhausted", (
        f"task.error must be canonical 'budget_exhausted'; got {result.error!r}"
    )
    halted_evts = [(k, p) for (k, p) in events if k == "halted"]
    assert halted_evts, "loop must emit a 'halted' event on budget exhaustion"
    _, payload = halted_evts[-1]
    assert payload.get("reason") == "budget_exhausted"
    detail = payload.get("detail") or {}
    assert "spent_usd" in detail and "budget_usd" in detail, (
        f"halted detail must carry numeric context; got {detail!r}"
    )
    assert detail["spent_usd"] >= detail["budget_usd"]


# ---------------------------------------------------------------------------
# 6. Duration exhaustion mid-execution.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_phase_d_duration_exhaustion_mid_execute():
    """``elapsed_hours >= max_duration_hours`` after plan → HALTED with
    canonical ``duration_exhausted`` and a structured ``halted`` event
    detail.  We force the wallclock guard by patching ``time.time`` so
    the second invocation (inside ``_budget_exceeded``) reports a far-
    future timestamp."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import planner as planner_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(
            reserved=0, spent_usd=0.0, budget_usd=10.0, max_duration_hours=0.5
        )
        await _insert_agent_task(pool, task)

        step = _mk_step(step_id="dx-1")
        plan_mock = AsyncMock(return_value=([step], 0.0))

        events: list[tuple[str, dict]] = []

        async def _capture_emit(db, redis, task, kind, *, step_id=None, payload=None):
            events.append((kind, dict(payload or {})))

        # Track time.time invocations: the FIRST call captures
        # ``started_at``; from the SECOND call onward, jump 10h forward
        # so the wallclock guard trips on the next ``_budget_exceeded``.
        real_time = _time.time
        anchor = real_time()
        state = {"calls": 0}

        def _fake_time():
            state["calls"] += 1
            if state["calls"] == 1:
                return anchor
            return anchor + 10 * 3600.0

        with (
            patch.object(planner_mod, "build_initial_plan", plan_mock),
            patch.object(loop_mod, "_emit", _capture_emit),
            patch.object(loop_mod, "_record_event", AsyncMock()),
            patch.object(loop_mod, "_settle_agent_credits", AsyncMock()),
            patch.object(loop_mod.time, "time", _fake_time),
        ):
            result = await loop_mod.run_agent_task(task, db=pool, redis=None)
    finally:
        await pool.close()

    assert result.state == AgentState.HALTED
    assert result.error == "duration_exhausted", (
        f"wallclock-exhaustion must persist canonical 'duration_exhausted'; "
        f"got {result.error!r}"
    )
    halted_evts = [(k, p) for (k, p) in events if k == "halted"]
    assert halted_evts, "loop must emit a 'halted' event on duration exhaustion"
    _, payload = halted_evts[-1]
    assert payload.get("reason") == "duration_exhausted"
    detail = payload.get("detail") or {}
    assert "elapsed_hours" in detail and "max_duration_hours" in detail
    assert detail["elapsed_hours"] >= detail["max_duration_hours"]


# ---------------------------------------------------------------------------
# 7-10. _infer_failure: each canonical step error code fires for the right
#       tool/result shape, and unrelated cases return None.
# ---------------------------------------------------------------------------


def test_phase_d_infer_failure_timed_out_for_exec_tools():
    """``timed_out=True`` on any code-exec-family tool → canonical
    ``timed_out`` step error code."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415

    for tool in ("code_exec", "bash_exec", "typescript_exec", "rust_exec"):
        out = loop_mod._infer_failure(tool, {"timed_out": True, "exit_code": 0})
        assert out == "timed_out", (
            f"tool={tool!r} timed_out=True must surface canonical 'timed_out'; "
            f"got {out!r}"
        )
        assert out in loop_mod.CANONICAL_STEP_ERROR_CODES


def test_phase_d_infer_failure_process_killed_for_exec_tools():
    """``killed=True`` (and not timed_out) → canonical ``process_killed``.
    Order matters: timed_out is checked first; we test killed in
    isolation."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415

    for tool in ("code_exec", "bash_exec", "typescript_exec", "rust_exec"):
        out = loop_mod._infer_failure(
            tool,
            {"timed_out": False, "killed": True, "exit_code": -9},
        )
        assert out == "process_killed", (
            f"tool={tool!r} killed=True must surface canonical 'process_killed'; "
            f"got {out!r}"
        )


def test_phase_d_infer_failure_non_zero_exit_for_exec_tools():
    """``exit_code != 0`` (without timed_out / killed) → canonical
    ``non_zero_exit``."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415

    for tool in ("code_exec", "bash_exec", "typescript_exec", "rust_exec"):
        out = loop_mod._infer_failure(
            tool,
            {"timed_out": False, "killed": False, "exit_code": 1},
        )
        assert out == "non_zero_exit", (
            f"tool={tool!r} exit_code=1 must surface canonical 'non_zero_exit'; "
            f"got {out!r}"
        )


def test_phase_d_infer_failure_http_error_for_browser_tools():
    """``status >= 400`` on ``browser_fetch`` / ``browser_click_fetch`` →
    canonical ``http_error``."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415

    for tool in ("browser_fetch", "browser_click_fetch"):
        for s in (400, 404, 500, 503):
            out = loop_mod._infer_failure(tool, {"status": s})
            assert out == "http_error", (
                f"tool={tool!r} status={s} must surface canonical 'http_error'; "
                f"got {out!r}"
            )


def test_phase_d_infer_failure_clean_results_return_none():
    """Successful results across tool families must NOT trip a soft
    failure.  This pins the negative space so a future change does not
    accidentally over-report failures."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415

    # exec family: zero exit, no timeout, not killed.
    for tool in ("code_exec", "bash_exec", "typescript_exec", "rust_exec"):
        assert (
            loop_mod._infer_failure(
                tool, {"exit_code": 0, "timed_out": False, "killed": False}
            )
            is None
        )
    # browser family: 200, 301, 399 are all "ok".
    for tool in ("browser_fetch", "browser_click_fetch"):
        for s in (200, 301, 399):
            assert loop_mod._infer_failure(tool, {"status": s}) is None
    # Unrelated tool: even with a noisy result dict, returns None.
    assert (
        loop_mod._infer_failure(
            "think",
            {
                "exit_code": 1,
                "timed_out": True,
                "killed": True,
                "status": 500,
            },
        )
        is None
    ), (
        "_infer_failure must not misfire on tools outside the exec/browser "
        "families even when the result dict carries noisy fields"
    )


def test_phase_d_infer_failure_http_only_on_browser_family():
    """``status >= 400`` must NOT cause an exec-family tool to surface
    ``http_error`` (cross-family contamination guard)."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415

    out = loop_mod._infer_failure(
        "code_exec", {"exit_code": 0, "status": 500}
    )
    assert out is None, (
        "an exec-family tool with status=500 but exit_code=0 must NOT "
        "surface http_error — that field is browser-only"
    )


def test_phase_d_infer_failure_priority_timed_out_beats_exit_code():
    """When both ``timed_out=True`` AND ``exit_code != 0`` are present
    (a common SIGKILL-on-timeout shape), ``timed_out`` must win — it is
    the more specific failure cause and matters for retry semantics."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415

    out = loop_mod._infer_failure(
        "code_exec",
        {"timed_out": True, "killed": True, "exit_code": -9},
    )
    assert out == "timed_out", (
        f"timed_out must take precedence over killed/non_zero_exit; got {out!r}"
    )
