"""CC-25: ToolError raw message/detail must not leak to step state or SSE.

Pre-CC-25, the agent loop's ToolError handler persisted ``str(exc)`` into
``step.error`` and ``{"error_detail": exc.detail}`` into ``step.result``,
and the emitted ``step_failed`` SSE payload echoed both verbatim.  ToolError
messages and details routinely carry workspace paths, file listings, and
upstream response bodies (see ``mariana/agent/dispatcher.py``).

Post-CC-25, the loop must:

* set ``step.error = "tool_error"`` (stable code only),
* set ``step.result = {"error_code": "tool_error", "tool": <tool_name>}``
  (no ``error_detail`` key carrying raw exception text),
* emit ``step_failed`` payloads that reference the stable code and the
  tool name, **never** the raw message or structured detail.

The raw diagnostic stays available to operators via a structured
``logger.warning("tool_error", ...)`` on the server side.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


def _new_task():
    import uuid  # noqa: PLC0415

    from mariana.agent.models import AgentState, AgentTask  # noqa: PLC0415

    return AgentTask(
        id=str(uuid.uuid4()),
        user_id=f"user-cc25-{uuid.uuid4().hex[:8]}",
        goal="CC-25 ToolError scrub",
        budget_usd=5.0,
        spent_usd=0.0,
        state=AgentState.EXECUTE,
    )


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


@pytest.mark.asyncio
async def test_cc25_tool_error_persists_only_stable_code_on_step():
    """A ToolError carrying a path-rich message + structured detail must
    surface only as the stable ``tool_error`` code on ``step.error`` and
    ``step.result``.  No raw message / detail may persist to the user-
    visible step record."""
    from mariana.agent import dispatcher as dispatcher_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent.models import AgentStep, StepStatus  # noqa: PLC0415

    task = _new_task()
    step = AgentStep(
        id="step-cc25",
        title="exec",
        tool="code_exec",
        params={"code": "print(1)"},
    )
    task.steps = [step]

    # Construct a ToolError whose message + detail look exactly like the
    # path-rich strings dispatcher.py routinely raises (workspace path,
    # response body, etc.).  None of this content may leak to step state.
    raw_message = (
        "code_exec failed: source_dir '/workspace/user-abc/secret-task/' "
        "is empty or missing"
    )
    raw_detail: dict[str, Any] = {
        "status": 500,
        "body": "<html>internal traceback /var/lib/mariana/...</html>",
    }

    async def bad_dispatch(*a, **kw):
        raise dispatcher_mod.ToolError(
            "code_exec", raw_message, detail=raw_detail,
        )

    record_mock = AsyncMock()

    with patch.object(loop_mod, "dispatch", bad_dispatch), \
         patch.object(loop_mod, "_record_event", record_mock):
        ok, err = await loop_mod._run_one_step(_NoopDB(), None, task, step)

    assert ok is False
    assert step.status == StepStatus.FAILED
    # Stable code only.
    assert err == "tool_error", (
        f"err must be stable 'tool_error' code; got {err!r}"
    )
    assert step.error == "tool_error"
    # No raw message / paths / response body anywhere on the step record.
    assert raw_message not in (step.error or "")
    assert step.result == {"error_code": "tool_error", "tool": "code_exec"}
    # Defensive: result must NOT carry the legacy "error_detail" key.
    assert "error_detail" not in (step.result or {})
    # And the workspace-path substring must not appear anywhere we persist.
    persisted_blob = repr(step.error) + repr(step.result)
    assert "/workspace/" not in persisted_blob
    assert "internal traceback" not in persisted_blob


@pytest.mark.asyncio
async def test_cc25_tool_error_emits_only_stable_code_on_sse():
    """The ``step_failed`` event payload emitted by ``_run_one_step`` must
    carry the stable code + tool name only, never the raw message / detail
    strings ToolError carries."""
    from mariana.agent import dispatcher as dispatcher_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent.models import AgentStep  # noqa: PLC0415

    task = _new_task()
    step = AgentStep(
        id="step-cc25-sse",
        title="exec",
        tool="code_exec",
        params={"code": "print(1)"},
    )
    task.steps = [step]

    raw_message = (
        "code_exec failed: entry 'evil.html' not found in '/workspace/x/y/'."
        " Available: ['secret.txt', 'private.key']"
    )
    raw_detail = {"body": "/var/lib/mariana/runtime/leak.txt"}

    async def bad_dispatch(*a, **kw):
        raise dispatcher_mod.ToolError(
            "code_exec", raw_message, detail=raw_detail,
        )

    emitted_events: list[tuple[str, dict[str, Any]]] = []

    async def fake_emit(db, redis, t, kind, *, step_id=None, payload=None):
        emitted_events.append((kind, dict(payload or {})))

    with patch.object(loop_mod, "dispatch", bad_dispatch), \
         patch.object(loop_mod, "_emit", fake_emit), \
         patch.object(loop_mod, "_persist_task", AsyncMock()):
        ok, err = await loop_mod._run_one_step(_NoopDB(), None, task, step)

    assert ok is False
    assert err == "tool_error"

    failed_payloads = [p for kind, p in emitted_events if kind == "step_failed"]
    assert failed_payloads, "must emit a step_failed event"
    payload = failed_payloads[-1]

    assert payload.get("error") == "tool_error"
    assert payload.get("tool") == "code_exec"
    # The legacy "detail" key carrying raw exception detail must NOT be
    # part of the user-visible SSE payload anymore.
    assert "detail" not in payload, (
        f"step_failed payload must not include raw 'detail'; got {payload!r}"
    )
    # And no substring of the raw message / detail may appear in the
    # emitted payload, anywhere.
    blob = repr(payload)
    assert "/workspace/" not in blob
    assert "secret.txt" not in blob
    assert "private.key" not in blob
    assert "leak.txt" not in blob
    assert "Available:" not in blob


def test_cc25_tool_error_listed_as_canonical_code_in_loop_docstring():
    """The agent loop module docstring must list ``tool_error`` as a
    canonical user-visible error code so future contributors know not to
    re-introduce raw exception strings on the user-visible surface."""
    from mariana.agent import loop as loop_mod  # noqa: PLC0415

    doc = loop_mod.__doc__ or ""
    assert "tool_error" in doc, (
        "loop module docstring must enumerate 'tool_error' as a canonical "
        "stable error_code (CC-25)"
    )
    # Sanity: also still lists the prior CC-21 codes.
    assert "unexpected" in doc
    assert "planner_failed" in doc
