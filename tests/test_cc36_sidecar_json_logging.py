"""CC-36 — sandbox / browser sidecars must emit structured JSON logs.

The audit (A50, finding CC-36) flagged that both sidecars used Python's
default ``logging.basicConfig`` with a free-form text format.  Production
log aggregators cannot parse those records without bespoke regex, and the
sidecars cannot ship contextual fields to the orchestrator without losing
them in string interpolation.

Contract enforced here:

* Both sidecars expose a ``_JsonLogFormatter`` ``logging.Formatter`` and a
  ``_configure_logging()`` initialiser.
* When ``LOG_FORMAT`` is unset (or ``json``), records emit as one JSON
  object per line containing at minimum ``timestamp``, ``level``,
  ``logger`` and ``event`` (CC-38: aligned with the orchestrator's
  structlog schema in ``mariana/main.py``).
* Structured ``extra=`` fields are surfaced as top-level keys in the
  emitted JSON (so ``log.info(\"x\", extra={\"req_id\": \"abc\"})`` round-trips).
* Exception ``exc_info`` is serialised as a string under ``exc_info``.
* ``LOG_FORMAT=text`` falls back to the legacy human-readable format for
  local debugging.

Tests are pure unit tests against the formatter/configurator — no HTTP,
no Docker, no Playwright — so they remain deterministic in CI.
"""

from __future__ import annotations

import io
import json
import logging
import importlib.util
import os
import tempfile
from typing import Callable

import pytest


# The browser sidecar imports playwright at top-level.  Production CI for
# the orchestrator does not install playwright (it's a sidecar-container
# dependency), so skip the browser parametrisation when the package is
# unavailable.  The sandbox sidecar has no such dependency and is always
# importable.
_HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None
_SIDECARS = ["sandbox_server.app"] + (["browser_server.app"] if _HAS_PLAYWRIGHT else [])


# The sandbox app calls ``WORKSPACE_ROOT.mkdir`` at import time and the
# default ``/workspace`` is unwritable in CI.  Point it at a tempdir before
# the test runner imports the module — this matches the CC-28 / CC-34 test
# fixtures.  We do this at module scope so the parametrised tests below can
# import the module on first use.
os.environ.setdefault("WORKSPACE_ROOT", tempfile.mkdtemp(prefix="cc36-sandbox-"))
os.environ.setdefault("SANDBOX_SHARED_SECRET", "cc36-test-secret")
os.environ.setdefault("BROWSER_SHARED_SECRET", "cc36-test-secret")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    *,
    name: str = "sidecar",
    level: int = logging.INFO,
    msg: str = "hello",
    args: tuple = (),
    extra: dict | None = None,
    exc_info=None,
) -> logging.LogRecord:
    rec = logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )
    if extra:
        for k, v in extra.items():
            setattr(rec, k, v)
    return rec


def _capture(logger: logging.Logger, fn: Callable[[], None]) -> str:
    """Run ``fn``; return everything written to the root handler's stream."""
    buf = io.StringIO()
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        for h in saved:
            root.removeHandler(h)
        handler = logging.StreamHandler(buf)
        # Copy the formatter the sidecar's _configure_logging installed.
        handler.setFormatter(saved[0].formatter if saved else logging.Formatter())
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        logger.propagate = True
        fn()
        handler.flush()
        return buf.getvalue()
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved:
            root.addHandler(h)


# ---------------------------------------------------------------------------
# 1. Both sidecars expose the same JSON-logging contract.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", _SIDECARS)
def test_cc36_sidecar_exposes_json_formatter(module_name: str) -> None:
    """Each sidecar must export ``_JsonLogFormatter`` and
    ``_configure_logging`` with the documented signature."""
    import importlib  # noqa: PLC0415

    mod = importlib.import_module(module_name)
    assert hasattr(mod, "_JsonLogFormatter"), (
        f"{module_name} must expose _JsonLogFormatter for CC-36"
    )
    assert hasattr(mod, "_configure_logging"), (
        f"{module_name} must expose _configure_logging for CC-36"
    )
    assert issubclass(mod._JsonLogFormatter, logging.Formatter), (
        "formatter must inherit logging.Formatter"
    )


# ---------------------------------------------------------------------------
# 2. Default INFO record serialises as parseable JSON with required fields.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", _SIDECARS)
def test_cc36_info_record_emits_valid_json(module_name: str) -> None:
    import importlib  # noqa: PLC0415

    mod = importlib.import_module(module_name)
    fmt = mod._JsonLogFormatter()
    rec = _make_record(name=module_name, msg="ready")
    line = fmt.format(rec)
    payload = json.loads(line)  # must be parseable JSON
    assert payload["level"] == "INFO"
    assert payload["logger"] == module_name
    # CC-38: fields aligned with structlog (event/timestamp), not msg/ts.
    assert payload["event"] == "ready"
    assert (
        "timestamp" in payload
        and isinstance(payload["timestamp"], str)
        and payload["timestamp"]
    )
    # Legacy field names must not leak back in.
    assert "msg" not in payload
    assert "ts" not in payload


# ---------------------------------------------------------------------------
# 3. extra= fields round-trip as top-level JSON keys.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", _SIDECARS)
def test_cc36_extra_fields_round_trip(module_name: str) -> None:
    import importlib  # noqa: PLC0415

    mod = importlib.import_module(module_name)
    fmt = mod._JsonLogFormatter()
    rec = _make_record(
        msg="exec done",
        extra={"req_id": "abc-123", "duration_ms": 42, "exit_code": 0},
    )
    payload = json.loads(fmt.format(rec))
    assert payload["req_id"] == "abc-123"
    assert payload["duration_ms"] == 42
    assert payload["exit_code"] == 0


# ---------------------------------------------------------------------------
# 4. exc_info is serialised as a string under "exc_info".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", _SIDECARS)
def test_cc36_exception_path_serialises_traceback(module_name: str) -> None:
    import importlib  # noqa: PLC0415
    import sys  # noqa: PLC0415

    mod = importlib.import_module(module_name)
    fmt = mod._JsonLogFormatter()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        exc_info = sys.exc_info()
    rec = _make_record(level=logging.ERROR, msg="exec failed", exc_info=exc_info)
    payload = json.loads(fmt.format(rec))
    assert payload["level"] == "ERROR"
    assert "exc_info" in payload
    assert "RuntimeError" in payload["exc_info"]
    assert "boom" in payload["exc_info"]


# ---------------------------------------------------------------------------
# 5. Non-JSON-serialisable extras are coerced via repr — no formatter crash.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", _SIDECARS)
def test_cc36_unserialisable_extra_coerced_to_string(module_name: str) -> None:
    import importlib  # noqa: PLC0415

    mod = importlib.import_module(module_name)
    fmt = mod._JsonLogFormatter()

    class _Unjsonable:
        def __repr__(self) -> str:
            return "<UNJSONABLE-marker>"

    rec = _make_record(msg="weird", extra={"obj": _Unjsonable()})
    line = fmt.format(rec)  # must not raise
    payload = json.loads(line)
    assert "<UNJSONABLE-marker>" in payload["obj"]


# ---------------------------------------------------------------------------
# 6. LOG_FORMAT=text falls back to the legacy human-readable format.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", _SIDECARS)
def test_cc36_log_format_text_falls_back(
    module_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib  # noqa: PLC0415

    mod = importlib.import_module(module_name)
    monkeypatch.setenv("LOG_FORMAT", "text")
    try:
        mod._configure_logging()
        root = logging.getLogger()
        assert root.handlers, "configure must install a handler"
        formatter = root.handlers[0].formatter
        # Text mode must NOT be the JSON formatter.
        assert not isinstance(formatter, mod._JsonLogFormatter)
    finally:
        # Restore json mode so subsequent tests see the production default.
        monkeypatch.setenv("LOG_FORMAT", "json")
        mod._configure_logging()


# ---------------------------------------------------------------------------
# 7. Default (no LOG_FORMAT env) installs the JSON formatter.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", _SIDECARS)
def test_cc36_default_format_is_json(
    module_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib  # noqa: PLC0415

    mod = importlib.import_module(module_name)
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    try:
        mod._configure_logging()
        root = logging.getLogger()
        assert root.handlers, "configure must install a handler"
        formatter = root.handlers[0].formatter
        assert isinstance(formatter, mod._JsonLogFormatter), (
            "default LOG_FORMAT must be JSON for production aggregators"
        )
    finally:
        monkeypatch.setenv("LOG_FORMAT", "json")
        mod._configure_logging()
