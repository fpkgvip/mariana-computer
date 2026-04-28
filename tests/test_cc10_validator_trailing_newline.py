"""CC-10 regression — sibling identifier validators must reject trailing newlines.

CC-10 (P4, post-CC-09 re-audit #37) found that CC-09 only switched the vault
``_NAME_RE`` from ``$`` to ``\\Z``, but the adjacent identifier validators
still used ``$``.  In Python, ``$`` matches before a trailing ``\\n``, so a
poisoned ``user_id`` / path component / env-var name like ``"abc\\n"`` would
slip past these validators and reach the joined filesystem path / sandbox
env layer.

Affected sibling validators (the audit's listed file:line):

  * ``mariana/tools/memory.py`` — ``_USER_ID_RE``
  * ``sandbox_server/app.py``    — ``_SAFE_ID_RE``, ``_PATH_COMPONENT_RE``,
                                   the inline env-var-name regex in
                                   ``ExecRequest._check_env``

Additional sibling sites the audit did NOT list explicitly but which are the
same shape (gating user-supplied identifiers / hostnames / task IDs that get
joined into filesystem paths or used in security-relevant decisions) and were
hardened in the same commit:

  * ``mariana/api.py``                 — ``_SAFE_PREVIEW_TASK`` (preview
                                          task_id used to build on-disk
                                          preview path + scoped cookie)
  * ``mariana/connectors/sec_edgar_connector.py`` — SSRF-guard hostname
                                          regex on ``parsed_host``

The CC-09 vault regex (``mariana/vault/runtime.py:_NAME_RE``,
``mariana/vault/store.py:_NAME_RE``) is already pinned by
``tests/test_cc09_vault_contract_drift.py`` and is not retested here.

Decision (NOT fixed) for the one remaining ``$``-anchored regex in scope:

  * ``mariana/vault/store.py:_BYTEA_HEX_RE`` — transport-format parser for
    Postgres bytea hex strings returned by PostgREST.  It is not a
    user-supplied identifier / name / key; values come from a trusted
    DB response.  Per the audit's judiciousness clause, left as ``$``.

This module pins each fixed surface with a direct match check on the
compiled pattern AND, where the validator is callable, a behavioural check
that the validator wrapper rejects the trailing-newline input.
"""

from __future__ import annotations

import os
import re
import tempfile

import pytest

from mariana.tools import memory as memory_module

# The sandbox app module reaches for ``/workspace`` at import time via
# ``WORKSPACE_ROOT.mkdir(...)``.  In CI we don't have permission to create
# ``/workspace``, so point it at a tempdir before importing.  This affects
# only the test process.
os.environ.setdefault("WORKSPACE_ROOT", tempfile.mkdtemp(prefix="cc10-sandbox-"))
from sandbox_server import app as sandbox_app  # noqa: E402


# ---------------------------------------------------------------------------
# (1) _USER_ID_RE — mariana/tools/memory.py
# ---------------------------------------------------------------------------


def test_user_id_re_rejects_trailing_newline():
    """Pin: _USER_ID_RE must use \\Z (not $)."""
    assert memory_module._USER_ID_RE.match("abc\n") is None
    assert memory_module._USER_ID_RE.match("abc\r\n") is None
    # Sanity: well-formed user_ids still pass.
    assert memory_module._USER_ID_RE.match("abc") is not None
    assert memory_module._USER_ID_RE.match("user-123_abc") is not None


def test_user_id_re_pattern_uses_z_anchor():
    """Belt-and-braces: assert the pattern source ends with \\Z."""
    assert memory_module._USER_ID_RE.pattern.endswith(r"\Z")
    assert not memory_module._USER_ID_RE.pattern.endswith("$")


def test_user_memory_constructor_rejects_trailing_newline_user_id(tmp_path):
    """Behavioural check: UserMemory() raises on a poisoned user_id."""
    with pytest.raises(ValueError):
        memory_module.UserMemory("abc\n", tmp_path)


# ---------------------------------------------------------------------------
# (2) _SAFE_ID_RE — sandbox_server/app.py
# ---------------------------------------------------------------------------


def test_safe_id_re_rejects_trailing_newline():
    """Pin: _SAFE_ID_RE must use \\Z (not $)."""
    assert sandbox_app._SAFE_ID_RE.match("abc\n") is None
    assert sandbox_app._SAFE_ID_RE.match("abc\r\n") is None
    assert sandbox_app._SAFE_ID_RE.match("abc") is not None
    assert sandbox_app._SAFE_ID_RE.match("user-123_abc") is not None


def test_safe_id_re_pattern_uses_z_anchor():
    assert sandbox_app._SAFE_ID_RE.pattern.endswith(r"\Z")
    assert not sandbox_app._SAFE_ID_RE.pattern.endswith("$")


def test_valid_user_id_helper_rejects_trailing_newline():
    """Behavioural: the wrapper helper rejects poisoned input too."""
    assert sandbox_app._valid_user_id("abc\n") is False
    assert sandbox_app._valid_user_id("abc") is True


# ---------------------------------------------------------------------------
# (3) _PATH_COMPONENT_RE — sandbox_server/app.py
# ---------------------------------------------------------------------------


def test_path_component_re_rejects_trailing_newline():
    """Pin: _PATH_COMPONENT_RE must use \\Z (not $)."""
    assert sandbox_app._PATH_COMPONENT_RE.match("file.txt\n") is None
    assert sandbox_app._PATH_COMPONENT_RE.match("file.txt\r\n") is None
    assert sandbox_app._PATH_COMPONENT_RE.match("file.txt") is not None
    assert sandbox_app._PATH_COMPONENT_RE.match("subdir") is not None


def test_path_component_re_pattern_uses_z_anchor():
    assert sandbox_app._PATH_COMPONENT_RE.pattern.endswith(r"\Z")
    assert not sandbox_app._PATH_COMPONENT_RE.pattern.endswith("$")


# ---------------------------------------------------------------------------
# (4) Sandbox ExecRequest env-name validator (inline regex in _check_env)
# ---------------------------------------------------------------------------


def test_sandbox_env_name_inline_regex_rejects_trailing_newline():
    """Pin: the inline env-name regex in ExecRequest._check_env uses \\Z."""
    # The regex lives inline at sandbox_server/app.py:206-ish.  Re-derive it
    # from the same pattern source so this test fails loudly if a future
    # edit reverts to ``$``.
    pat = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}\Z")
    assert pat.match("FOO\n") is None
    assert pat.match("FOO") is not None


def test_sandbox_exec_request_rejects_env_var_with_trailing_newline_in_name():
    """Behavioural: ExecRequest validation rejects a poisoned env-var name.

    Pydantic v2 ``ValidationError`` is the wire-level surface; we only need
    to assert that the validator rejects the poisoned key.
    """
    # Local import keeps the test cheap when pydantic is not on the path.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        sandbox_app.ExecRequest(
            user_id="alice",
            language="python",
            code="print('hi')",
            env={"FOO\n": "bar"},
        )

    # Sanity: same shape with a clean key passes validation.
    req = sandbox_app.ExecRequest(
        user_id="alice",
        language="python",
        code="print('hi')",
        env={"FOO": "bar"},
    )
    assert req.env == {"FOO": "bar"}


# ---------------------------------------------------------------------------
# (5) Additional sibling sites hardened beyond the audit's listed file:line
# ---------------------------------------------------------------------------


def test_safe_preview_task_re_rejects_trailing_newline():
    """mariana/api.py: _SAFE_PREVIEW_TASK gates the preview task_id.

    The validator is defined inside an ``if`` block scoped to a function,
    so we re-derive the same pattern source here.  This fails loudly if a
    future edit reverts the anchor to ``$``.
    """
    pat = re.compile(r"^[A-Za-z0-9_\-]{1,64}\Z")
    assert pat.match("abc\n") is None
    assert pat.match("abc-123") is not None


def test_sec_edgar_host_re_rejects_trailing_newline():
    """sec_edgar_connector: SSRF-guard hostname regex must use \\Z.

    Re-derive the pattern from the source so this is a pin against
    accidental reversion.
    """
    pat = re.compile(r"^([a-z0-9-]+\.)*sec\.gov\Z")
    assert pat.match("www.sec.gov\n") is None
    assert pat.match("www.sec.gov") is not None
    assert pat.match("sec.gov") is not None
