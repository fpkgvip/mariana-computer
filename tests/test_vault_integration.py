"""End-to-end integration tests for the F4 Vault wiring (env injection + redaction).

These exercise the full chain at the Python level WITHOUT spinning up
FastAPI or a real sandbox:

  1. ``set_task_context`` installs the env + redactor under contextvars.
  2. ``dispatcher._h_code_exec`` merges the env (verified by stubbing
     ``tools.exec_code`` to capture the env dict it receives).
  3. ``loop._record_event`` and ``loop._summarise_result`` redact every
     plaintext occurrence before payloads are persisted.

The redaction tests use a fake DB + fake Redis to avoid any real I/O.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from mariana.agent import dispatcher, loop
from mariana.agent.models import AgentEvent, AgentState
from mariana.vault.runtime import set_task_context


# ---------------------------------------------------------------------------
# Dispatcher: vault_env merges into exec_code env
# ---------------------------------------------------------------------------


def test_code_exec_merges_vault_env_behind_plan_env(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_exec_code(**kwargs):
        captured.update(kwargs)
        return {"stdout": "", "stderr": "", "exit_code": 0, "duration_ms": 1}

    monkeypatch.setattr(dispatcher.tools, "exec_code", fake_exec_code)

    async def _go():
        h = set_task_context({"OPENAI_API_KEY": "sk-aaaaaaaaaaaaaaaa", "OTHER": "secondvalue123"})
        try:
            await dispatcher._h_code_exec(
                {"code": "print(1)", "language": "python", "env": {"PLAN_VAR": "planval"}},
                user_id="u",
                task_id="t",
            )
        finally:
            h.reset()

    asyncio.run(_go())
    env = captured["env"]
    # Vault env present
    assert env["OPENAI_API_KEY"] == "sk-aaaaaaaaaaaaaaaa"
    assert env["OTHER"] == "secondvalue123"
    # Plan env present and wins on conflict
    assert env["PLAN_VAR"] == "planval"


def test_code_exec_plan_env_shadows_vault(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_exec_code(**kwargs):
        captured.update(kwargs)
        return {"stdout": "", "stderr": "", "exit_code": 0, "duration_ms": 1}

    monkeypatch.setattr(dispatcher.tools, "exec_code", fake_exec_code)

    async def _go():
        h = set_task_context({"FOO": "vaultvalue1234"})
        try:
            await dispatcher._h_code_exec(
                {"code": "x", "env": {"FOO": "planoverride"}},
                user_id="u",
                task_id="t",
            )
        finally:
            h.reset()

    asyncio.run(_go())
    assert captured["env"]["FOO"] == "planoverride"


def test_code_exec_no_vault_no_change(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_exec_code(**kwargs):
        captured.update(kwargs)
        return {"stdout": "", "stderr": "", "exit_code": 0, "duration_ms": 1}

    monkeypatch.setattr(dispatcher.tools, "exec_code", fake_exec_code)

    async def _go():
        # No context installed -> get_task_env() == {}
        await dispatcher._h_code_exec(
            {"code": "x", "env": {"PLAN": "v"}}, user_id="u", task_id="t",
        )

    asyncio.run(_go())
    assert captured["env"] == {"PLAN": "v"}


# ---------------------------------------------------------------------------
# Loop: _summarise_result redacts every string field
# ---------------------------------------------------------------------------


def test_summarise_result_redacts_stdout_stderr_and_nested_artifacts():
    h = set_task_context({"AWS_KEY": "AKIAFAKEFAKEFAKEFAKE"})
    try:
        result = {
            "stdout": "echo prints AKIAFAKEFAKEFAKEFAKE here",
            "stderr": "warning: AKIAFAKEFAKEFAKEFAKE leaked",
            "exit_code": 0,
            "duration_ms": 12,
            "artifacts": [
                {"name": "out.txt", "workspace_path": "/workspace/out.txt", "size": 9, "sha256": "abc"},
            ],
            "extra_string": "secret AKIAFAKEFAKEFAKEFAKE is here",
        }
        out = loop._summarise_result(result)
        text = json.dumps(out)
        assert "AKIAFAKEFAKEFAKEFAKE" not in text
        assert "[REDACTED:AWS_KEY]" in out["stdout"]
        assert "[REDACTED:AWS_KEY]" in out["stderr"]
        assert "[REDACTED:AWS_KEY]" in out["extra_string"]
        # Non-string fields preserved
        assert out["exit_code"] == 0
        assert out["duration_ms"] == 12
    finally:
        h.reset()


def test_summarise_result_no_context_passthrough():
    # No context installed.
    result = {"stdout": "hello", "exit_code": 0}
    out = loop._summarise_result(result)
    assert out["stdout"] == "hello"
    assert out["exit_code"] == 0


# ---------------------------------------------------------------------------
# Loop: _record_event redacts payloads before DB / Redis writes
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, calls: list):
        self._calls = calls

    async def execute(self, sql, *args):
        self._calls.append((sql, args))


class _FakeAcquire:
    def __init__(self, conn: _FakeConn):
        self._c = conn

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *a):
        return False


class _FakeDB:
    def __init__(self):
        self.calls: list = []

    def acquire(self):
        return _FakeAcquire(_FakeConn(self.calls))


class _FakeRedis:
    def __init__(self):
        self.xadds: list = []

    async def xadd(self, key, fields, **kwargs):
        self.xadds.append((key, dict(fields)))


def test_record_event_redacts_payload_before_db_and_redis():
    h = set_task_context({"OPENAI_API_KEY": "sk-zzzzzzzzzzzzzzzz"})
    try:
        db = _FakeDB()
        redis = _FakeRedis()
        evt = AgentEvent(
            task_id="t1",
            event_type="terminal_output",
            state=AgentState.EXECUTE,
            step_id="s1",
            payload={
                "stdout": "ran with key sk-zzzzzzzzzzzzzzzz successfully",
                "stderr": "",
                "exit_code": 0,
            },
        )

        async def _go():
            await loop._record_event(db, redis, "t1", evt)

        asyncio.run(_go())

        # DB row payload (positional arg #5 in INSERT) must be redacted JSON.
        assert db.calls, "expected one DB insert"
        sql, args = db.calls[0]
        db_payload_json = args[4]  # 5th param is payload JSON string
        assert "sk-zzzzzzzzzzzzzzzz" not in db_payload_json
        assert "[REDACTED:OPENAI_API_KEY]" in db_payload_json

        # Redis xadd payload must also be redacted.
        assert redis.xadds, "expected one xadd"
        _, fields = redis.xadds[0]
        redis_data = fields["data"]
        assert "sk-zzzzzzzzzzzzzzzz" not in redis_data
        assert "[REDACTED:OPENAI_API_KEY]" in redis_data
    finally:
        h.reset()


def test_record_event_handles_truncation_with_redaction():
    """Even when a payload is so big it triggers the truncation branch,
    the sample we ship to clients must already be redacted."""
    h = set_task_context({"AWS_KEY": "AKIATRUNCATIONPAYLOAD"})
    try:
        db = _FakeDB()
        redis = _FakeRedis()
        # 80kB of repeating-secret content forces the truncation branch
        # (_MAX_EVENT_PAYLOAD_BYTES = 32k).
        big = ("AKIATRUNCATIONPAYLOAD foo bar baz quux " * 2200)[:80_000]
        evt = AgentEvent(
            task_id="t2",
            event_type="step_completed",
            state=AgentState.EXECUTE,
            step_id="s1",
            payload={"result": {"stdout": big}},
        )

        async def _go():
            await loop._record_event(db, redis, "t2", evt)

        asyncio.run(_go())

        sql, args = db.calls[0]
        db_payload_json = args[4]
        # Redaction happens BEFORE truncation, so the secret never appears.
        assert "AKIATRUNCATIONPAYLOAD" not in db_payload_json
        # And the truncation marker should be present.
        assert "_truncated" in db_payload_json
    finally:
        h.reset()
