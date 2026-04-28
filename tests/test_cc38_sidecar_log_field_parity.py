"""CC-38 — sidecar JSON log fields match the orchestrator's structlog schema.

The A51 re-audit (Finding 2, Low) flagged that the sandbox and browser
sidecars emitted JSON records with field names ``msg`` and ``ts`` while
the orchestrator (``mariana/main.py``) configures structlog with
``TimeStamper(fmt="iso")`` and ``JSONRenderer()``, which emit ``event``
and ``timestamp``.  Cross-service log aggregation (ELK / Datadog /
Loki) needed format-translation rules per emitter to query both record
types under the same schema.

The fix renames the two top-level keys in each sidecar's
``_JsonLogFormatter``: ``msg`` → ``event`` and ``ts`` → ``timestamp``.
Both sidecars now emit the same canonical schema as the orchestrator.

These tests pin the contract:

* every emitted record from each sidecar contains ``event``, ``timestamp``
  and ``level`` as top-level JSON keys;
* legacy ``msg`` / ``ts`` keys never appear in the output (regression
  guard against accidental revert);
* ``extra=`` fields still round-trip as top-level keys (CC-36 invariant);
* the formatter source itself does not embed the legacy field names in
  its payload-building dict literal (paranoia check against an
  AST-walkable revert).
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import logging
import os
import tempfile

import pytest


# Match CC-36 fixture: point WORKSPACE_ROOT at a tempdir before importing
# ``sandbox_server.app`` and stub the sidecar shared secrets so import-time
# auth-middleware setup doesn't refuse to configure.
os.environ.setdefault("WORKSPACE_ROOT", tempfile.mkdtemp(prefix="cc38-sandbox-"))
os.environ.setdefault("SANDBOX_SHARED_SECRET", "cc38-test-secret")
os.environ.setdefault("BROWSER_SHARED_SECRET", "cc38-test-secret")


# Browser sidecar imports playwright at top-level — skip when the package
# isn't installed in CI (matches the CC-36 guard).
_HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None
_SIDECARS = ["sandbox_server.app"] + (["browser_server.app"] if _HAS_PLAYWRIGHT else [])


def _make_record(
    *,
    name: str = "sidecar",
    level: int = logging.INFO,
    msg: str = "hello",
    extra: dict | None = None,
) -> logging.LogRecord:
    rec = logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    if extra:
        for k, v in extra.items():
            setattr(rec, k, v)
    return rec


# ---------------------------------------------------------------------------
# 1. Both sidecars emit the canonical structlog field schema.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", _SIDECARS)
def test_cc38_sidecar_emits_event_and_timestamp(module_name: str) -> None:
    """A formatted record contains ``event``, ``timestamp`` and ``level``
    as top-level JSON keys, matching the orchestrator's structlog
    output."""
    mod = importlib.import_module(module_name)
    fmt = mod._JsonLogFormatter()
    rec = _make_record(name=module_name, msg="ready")
    payload = json.loads(fmt.format(rec))

    # Canonical structlog-aligned keys.
    assert payload["event"] == "ready", (
        f"{module_name}: record message must be emitted under 'event' (CC-38)"
    )
    assert payload["level"] == "INFO"
    assert (
        "timestamp" in payload
        and isinstance(payload["timestamp"], str)
        and payload["timestamp"]
    ), f"{module_name}: must emit non-empty 'timestamp' (CC-38)"
    assert payload["logger"] == module_name


# ---------------------------------------------------------------------------
# 2. Legacy field names must not appear in the emitted payload.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", _SIDECARS)
def test_cc38_sidecar_does_not_emit_legacy_fields(module_name: str) -> None:
    """``msg`` and ``ts`` must never appear as top-level keys in the
    emitted JSON \u2014 those were the pre-CC-38 sidecar names and any
    revert would re-introduce the cross-service schema mismatch."""
    mod = importlib.import_module(module_name)
    fmt = mod._JsonLogFormatter()

    # Plain INFO record.
    payload = json.loads(fmt.format(_make_record(name=module_name, msg="hello")))
    assert "msg" not in payload, f"{module_name}: must not emit legacy 'msg' key"
    assert "ts" not in payload, f"{module_name}: must not emit legacy 'ts' key"

    # Record carrying structured extras must also not leak the legacy keys.
    payload2 = json.loads(
        fmt.format(
            _make_record(
                name=module_name,
                msg="exec done",
                extra={"req_id": "abc", "duration_ms": 7},
            )
        )
    )
    assert "msg" not in payload2
    assert "ts" not in payload2
    # CC-36 invariant preserved: extras still round-trip.
    assert payload2["req_id"] == "abc"
    assert payload2["duration_ms"] == 7


# ---------------------------------------------------------------------------
# 3. Formatter source must not embed the legacy field literals in its
#    payload-building dict (paranoid grep against revert).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", _SIDECARS)
def test_cc38_formatter_source_uses_canonical_keys(module_name: str) -> None:
    """The ``format`` method's source must build its payload with the
    canonical ``event`` / ``timestamp`` literals, not the legacy
    ``msg`` / ``ts``."""
    mod = importlib.import_module(module_name)
    src = inspect.getsource(mod._JsonLogFormatter.format)

    # The two canonical keys must appear as quoted dict literals.
    assert '"event"' in src or "'event'" in src, (
        f"{module_name}: formatter must emit 'event' key (CC-38)"
    )
    assert '"timestamp"' in src or "'timestamp'" in src, (
        f"{module_name}: formatter must emit 'timestamp' key (CC-38)"
    )

    # Legacy literals must not appear as top-level payload keys.
    # The ``_JSON_RESERVED`` allow-list contains the LogRecord attribute
    # names ``msg`` (used to skip the LogRecord's own ``msg`` field while
    # iterating ``record.__dict__``) but the ``format()`` method itself
    # should not embed ``"msg":`` or ``"ts":`` as a payload literal.
    assert '"msg":' not in src and "'msg':" not in src, (
        f"{module_name}: formatter must not emit legacy 'msg' key (CC-38)"
    )
    assert '"ts":' not in src and "'ts':" not in src, (
        f"{module_name}: formatter must not emit legacy 'ts' key (CC-38)"
    )


# ---------------------------------------------------------------------------
# 4. Structlog parity check: orchestrator schema is in fact event+timestamp.
# ---------------------------------------------------------------------------


def test_cc38_orchestrator_structlog_uses_canonical_keys() -> None:
    """Sanity-pin the orchestrator side of the contract.  ``mariana/main.py``
    configures structlog with ``TimeStamper(fmt='iso')`` (which emits the
    ``timestamp`` key) and ``JSONRenderer()`` (which uses the default
    structlog ``event`` key for the message).  This test reads the source
    of ``mariana/main.py`` and confirms both processors are in the
    pipeline so the parity claim doesn't silently rot if the orchestrator
    side changes."""
    src = inspect.getsource(importlib.import_module("mariana.main"))
    assert 'TimeStamper(fmt="iso")' in src or "TimeStamper(fmt='iso')" in src, (
        "orchestrator must use structlog TimeStamper(fmt='iso') so its"
        " records emit a 'timestamp' field aligning with sidecars (CC-38)"
    )
    assert "JSONRenderer()" in src, (
        "orchestrator must use structlog JSONRenderer() so its records"
        " emit an 'event' field aligning with sidecars (CC-38)"
    )
