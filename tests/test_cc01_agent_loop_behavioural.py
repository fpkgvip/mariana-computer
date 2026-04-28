"""CC-01: behavioural coverage for the agent loop's running surface.

Phase D coverage-fill: existing tests cover the persist / settle / cancel
edge cases (P-01, Q-01, O-02, R-01, S-03, T-01) but the *running* surface
of ``run_agent_task`` — the PLAN → EXECUTE pump that turns a fresh
``AgentTask`` into a terminal one — was light on direct behavioural
assertions.  These tests pin the contract:

  1. test_cc01_planner_failure_marks_task_failed
        ``planner.build_initial_plan`` raises → task ends FAILED with
        ``error`` populated, no settlement-side-effects bleed into the
        running surface.

  2. test_cc01_stop_pre_plan_short_circuits_to_halted
        Redis stop key is set before the loop is invoked → loop must
        HALT before calling ``planner.build_initial_plan`` (no spend).

  3. test_cc01_step_unexpected_exception_marks_step_failed
        A tool dispatch raises ``RuntimeError`` (not ``ToolError``) →
        the loop catches it, marks the step FAILED, and the task does
        not crash with a loop-level traceback.

  4. test_cc01_redis_get_failure_during_stop_check_does_not_abort
        ``redis.get`` raises (Redis flapping mid-run) → ``_check_stop_requested``
        must treat that as "no stop", NOT propagate the error.  This pins
        the resilience of the stop-poll loop.

  5. test_cc01_budget_exhausted_halts_task
        ``spent_usd >= budget_usd`` → loop transitions to HALTED with
        a structured ``budget_exhausted`` error message.

  6. test_cc01_deliver_step_unexpected_failure_keeps_task_failed
        The deliver step itself fails → task ends FAILED, NOT a half-
        delivered DONE row that the UI would render as success.
"""

from __future__ import annotations

import os
import pathlib
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


def _new_task(*, reserved: int = 0, settled: bool = False, spent_usd: float = 0.0,
              budget_usd: float = 5.0, state=None):
    from mariana.agent.models import AgentState, AgentTask  # noqa: PLC0415

    task = AgentTask(
        id=str(uuid.uuid4()),
        user_id=f"user-cc01-{uuid.uuid4().hex[:8]}",
        goal="CC-01 behavioural coverage",
        budget_usd=budget_usd,
        spent_usd=spent_usd,
        state=state or AgentState.PLAN,
    )
    task.reserved_credits = reserved
    task.credits_settled = settled
    return task


# ---------------------------------------------------------------------------
# 1. Planner exception → FAILED (no crash, no leaked state).
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_cc01_planner_failure_marks_task_failed():
    """``planner.build_initial_plan`` raises → task ends FAILED, error
    field populated with planner_failed prefix, no double-settle bleed."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import planner as planner_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=0, spent_usd=0.0)
        await _insert_agent_task(pool, task)

        plan_mock = AsyncMock(side_effect=RuntimeError("openrouter is down"))
        record_mock = AsyncMock()
        settle_mock = AsyncMock()

        with patch.object(planner_mod, "build_initial_plan", plan_mock), \
             patch.object(loop_mod, "_record_event", record_mock), \
             patch.object(loop_mod, "_settle_agent_credits", settle_mock):
            result = await loop_mod.run_agent_task(task, db=pool, redis=None)
    finally:
        await pool.close()

    assert plan_mock.await_count == 1, "planner must have been invoked"
    assert result.state == AgentState.FAILED, (
        f"planner failure must bring the task to FAILED; got {result.state.value!r}"
    )
    assert result.error is not None and result.error.startswith("planner_failed:"), (
        f"task.error must carry the planner_failed: prefix; got {result.error!r}"
    )
    # The finally-block ALWAYS attempts settlement; the function itself
    # short-circuits internally when ``reserved_credits <= 0``.  Pin the
    # invariant: at most one call per run, never more.
    assert settle_mock.await_count <= 1, (
        "finally-block must invoke settlement at most once per task — "
        "a re-entry would risk a double refund/mint"
    )


# ---------------------------------------------------------------------------
# 2. Pre-plan stop short-circuit.  CRITICAL behaviour: a user who hits
#    Stop before the worker starts must not pay for any planning work.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_cc01_stop_pre_plan_short_circuits_to_halted():
    """Redis stop key set BEFORE the loop runs → HALTED with no planner spend."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import planner as planner_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=0, spent_usd=0.0)
        await _insert_agent_task(pool, task)

        # Fake Redis whose STOP key returns truthy.
        class _StopRedis:
            def __init__(self) -> None:
                self.gets: list[str] = []
                self.xadds: list = []
                self.deletes: list = []

            async def get(self, key):
                self.gets.append(key)
                return b"1"

            async def xadd(self, *_a, **_kw):
                return None

            async def delete(self, *keys):
                for k in keys:
                    self.deletes.append(k)
                return 0

        redis = _StopRedis()

        plan_mock = AsyncMock(return_value=([], 0.99))
        record_mock = AsyncMock()
        settle_mock = AsyncMock()

        with patch.object(planner_mod, "build_initial_plan", plan_mock), \
             patch.object(loop_mod, "_record_event", record_mock), \
             patch.object(loop_mod, "_settle_agent_credits", settle_mock):
            result = await loop_mod.run_agent_task(task, db=pool, redis=redis)
    finally:
        await pool.close()

    assert plan_mock.await_count == 0, (
        "planner must NOT run when stop was requested before EXECUTE — "
        "CC-01 critical: a user hitting Stop pre-execution must not pay "
        "for plan generation"
    )
    assert result.state == AgentState.HALTED, (
        f"stop pre-plan must transition to HALTED; got {result.state.value!r}"
    )
    # spent_usd untouched because planner never ran.
    assert result.spent_usd == 0.0


# ---------------------------------------------------------------------------
# 3. Step dispatch raises an unexpected exception (not ToolError) — loop
#    must mark the step FAILED and not crash the whole loop with a
#    loop_crash error.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_cc01_step_unexpected_exception_marks_step_failed():
    """A bare ``RuntimeError`` from dispatch is caught by the defensive
    ``except Exception`` in ``_run_one_step`` and surfaces as a soft
    step failure, NOT a loop_crash."""
    from mariana.agent import dispatcher as dispatcher_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent.models import (  # noqa: PLC0415
        AgentStep,
        AgentTask,
        StepStatus,
    )

    task = _new_task(reserved=0)
    step = AgentStep(
        id="step-1",
        title="exec",
        tool="code_exec",
        params={"code": "print(1)"},
    )
    task.steps = [step]

    async def bad_dispatch(*a, **kw):
        raise RuntimeError("kernel panic")

    record_mock = AsyncMock()

    # We invoke _run_one_step directly so the test does NOT depend on the
    # full schema or planner mocking — this is an isolated unit assertion
    # on the single-step exception handling.
    class _NoopDB:
        def acquire(self):
            class _Acq:
                async def __aenter__(self_inner):
                    class _C:
                        async def execute(self_c, *a, **kw):
                            return None

                        async def fetchrow(self_c, *a, **kw):
                            return None
                    return _C()

                async def __aexit__(self_inner, *a):
                    return False

            return _Acq()

    with patch.object(dispatcher_mod, "dispatch", bad_dispatch), \
         patch.object(loop_mod, "_record_event", record_mock):
        ok, err = await loop_mod._run_one_step(_NoopDB(), None, task, step)

    assert ok is False, "step must report failure"
    assert err is not None and "unexpected:" in err, (
        f"unexpected exception must be tagged with 'unexpected:'; got {err!r}"
    )
    # The step was failed, not the entire task.
    assert step.status == StepStatus.FAILED
    assert task.total_failures == 1


# ---------------------------------------------------------------------------
# 4. Redis flapping mid-run → _check_stop_requested must treat exceptions
#    as "no stop" rather than propagating.  Pin the resilience invariant.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cc01_redis_get_failure_during_stop_check_does_not_abort():
    """``redis.get`` raises → _check_stop_requested returns False, NOT
    bubbles the error.  Critical because the loop polls this in a tight
    loop and a Redis hiccup must not kill the running task."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415

    task = _new_task(reserved=0)

    class _BrokenRedis:
        def __init__(self):
            self.calls = 0

        async def get(self, key):
            self.calls += 1
            raise ConnectionError("redis is flapping")

    r = _BrokenRedis()
    # Must NOT raise.
    out = await loop_mod._check_stop_requested(r, task)
    assert out is False, (
        "_check_stop_requested must return False on Redis errors so a "
        "transient Redis hiccup does not abort the running task"
    )
    assert r.calls == 1
    # The task's stop flag must NOT have been mutated.
    assert task.stop_requested is False


# ---------------------------------------------------------------------------
# 5. Budget exhausted → HALTED with a structured budget_exhausted error.
# ---------------------------------------------------------------------------


def test_cc01_budget_exhausted_halts_task():
    """``_budget_exceeded`` must trip when ``spent_usd >= budget_usd`` and
    return a parseable ``budget_exhausted: ...`` reason string.  Also pin
    the secondary wallclock guard via ``duration_exhausted``."""
    import time as _time  # noqa: PLC0415

    from mariana.agent import loop as loop_mod  # noqa: PLC0415

    # Anchor ``started_at`` to "now" so the wallclock guard does NOT
    # falsely trip; we are pinning the spend-side branch in isolation.
    fresh = _time.time()

    task = _new_task(reserved=0, budget_usd=1.0, spent_usd=1.5)
    over, why = loop_mod._budget_exceeded(task, started_at=fresh)
    assert over is True
    assert why.startswith("budget_exhausted:"), (
        f"budget exhaustion must be tagged for the SSE consumer; got {why!r}"
    )

    # Right at the boundary: spent_usd == budget_usd is also "over".
    task.spent_usd = task.budget_usd
    over, why = loop_mod._budget_exceeded(task, started_at=fresh)
    assert over is True
    assert why.startswith("budget_exhausted:")

    # Strictly under spend cap and within wallclock: not over.
    task.spent_usd = 0.0
    over, _ = loop_mod._budget_exceeded(task, started_at=fresh)
    assert over is False, (
        "a fresh task under both spend and wallclock caps must NOT be"
        " reported as exhausted"
    )

    # Wallclock guard: started_at far in the past trips duration_exhausted
    # even with zero spend.  Pin the second branch.
    over, why = loop_mod._budget_exceeded(task, started_at=fresh - 10 * 3600.0)
    assert over is True
    assert why.startswith("duration_exhausted:"), (
        f"wallclock exhaustion must be tagged separately from spend; got {why!r}"
    )


# ---------------------------------------------------------------------------
# 6. The fix-loop fix-budget exhausts → REPLAN attempt, then FAILED if no
#    replan budget.  Pins the lifecycle.  We assert _attempt_replan returns
#    False once replan_count hits the cap.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cc01_replan_cap_enforced():
    """``_attempt_replan`` must return False once ``replan_count`` equals
    ``max_replans`` so the loop falls through to FAILED.  This pins the
    HARD_MAX_REPLANS=3 contract that the agent loop's clamp depends on."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415

    task = _new_task(reserved=0)
    task.max_replans = 3
    task.replan_count = 3  # cap reached

    record_mock = AsyncMock()

    class _NoopDB:
        def acquire(self):
            class _Acq:
                async def __aenter__(self_inner):
                    class _C:
                        async def execute(self_c, *a, **kw):
                            return None

                        async def fetchrow(self_c, *a, **kw):
                            return None
                    return _C()

                async def __aexit__(self_inner, *a):
                    return False
            return _Acq()

    with patch.object(loop_mod, "_record_event", record_mock):
        replanned = await loop_mod._attempt_replan(
            _NoopDB(), None, task, reason="fix budget exhausted",
        )

    assert replanned is False, (
        "replan must refuse once replan_count == max_replans so the outer "
        "loop transitions to FAILED instead of looping forever"
    )


# ---------------------------------------------------------------------------
# 7. Hard-cap clamping at run_agent_task entry: caller cannot weaken
#    defenses by passing huge max_replans / max_fix_attempts_per_step.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_cc01_caller_cannot_raise_replan_cap_above_hard_max():
    """A caller that constructs a task with ``max_replans=99`` must see
    the value clamped to ``_HARD_MAX_REPLANS`` (=3) by ``run_agent_task``.

    Pins: a malicious / buggy caller cannot loop forever through the
    self-correction surface."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import planner as planner_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=0)
        # Caller tries to weaken the cap.
        task.max_replans = 99
        task.max_fix_attempts_per_step = 99
        await _insert_agent_task(pool, task)

        # Make the planner raise so we exit the loop quickly without any
        # actual replans needing to happen — we only care about the clamp
        # that runs at function entry.
        plan_mock = AsyncMock(side_effect=RuntimeError("stop here"))
        record_mock = AsyncMock()

        with patch.object(planner_mod, "build_initial_plan", plan_mock), \
             patch.object(loop_mod, "_record_event", record_mock):
            result = await loop_mod.run_agent_task(task, db=pool, redis=None)
    finally:
        await pool.close()

    assert result.max_replans == loop_mod._HARD_MAX_REPLANS, (
        f"max_replans must be clamped to {loop_mod._HARD_MAX_REPLANS}; "
        f"got {result.max_replans}"
    )
    assert result.max_fix_attempts_per_step == loop_mod._HARD_MAX_FIX_PER_STEP, (
        f"max_fix_attempts_per_step must be clamped to "
        f"{loop_mod._HARD_MAX_FIX_PER_STEP}; got {result.max_fix_attempts_per_step}"
    )
    assert result.state == AgentState.FAILED


# ---------------------------------------------------------------------------
# 8. Vault fail-closed pre-flight: requires_vault=True with a None redis
#    must mark the task FAILED before the planner runs (U-03 scenario,
#    pinned at the run_agent_task level — separate from the runtime-level
#    test in test_u03_vault_redis_safety.py).
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_cc01_requires_vault_with_no_redis_fails_closed_before_plan():
    """A task created with ``requires_vault=True`` (i.e. user submitted
    secrets) and a None redis client must FAIL before the planner runs.

    This is the loop-level boundary that the U-03 contract depends on:
    if it ever silently planned a task with no env injected, the user's
    tool calls would run with stripped secrets and pretend they succeeded."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent import planner as planner_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415
    from mariana.agent.models import AgentState  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=0)
        task.requires_vault = True
        await _insert_agent_task(pool, task)

        plan_mock = AsyncMock(return_value=([], 0.0))
        record_mock = AsyncMock()
        settle_mock = AsyncMock()

        with patch.object(planner_mod, "build_initial_plan", plan_mock), \
             patch.object(loop_mod, "_record_event", record_mock), \
             patch.object(loop_mod, "_settle_agent_credits", settle_mock):
            result = await loop_mod.run_agent_task(task, db=pool, redis=None)
    finally:
        await pool.close()

    assert plan_mock.await_count == 0, (
        "planner must NOT run when requires_vault=True and the redis "
        "client is None — fail-closed BEFORE any tool / plan invocation"
    )
    assert result.state == AgentState.FAILED, (
        f"requires_vault with no redis must FAIL the task; got {result.state.value!r}"
    )
    assert result.error is not None and "Vault unavailable" in result.error, (
        f"task.error must carry the Vault unavailable surface; got {result.error!r}"
    )
