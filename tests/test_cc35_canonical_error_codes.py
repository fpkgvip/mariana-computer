"""CC-35 regression — agent task/step error codes must match a canonical allow-list.

CC-35 (A50 Finding 2, Medium) found that `mariana/agent/loop.py` documented a
stable canonical error-code contract for ``task.error`` / ``step.error``
fields and SSE error payloads, but several call sites still persisted
free-form, formatted strings like ``budget_exhausted: spent ...``,
``deliver_failed: {err}``, ``unrecoverable: step {id} \u2014 {err}``,
``timed_out after {ms}ms``, ``HTTP {status}``.  Downstream consumers
therefore received a mix of canonical codes and ad-hoc text.

This module pins the contract:

  * ``mariana/agent/loop.py`` exposes ``CANONICAL_TASK_ERROR_CODES`` and
    ``CANONICAL_STEP_ERROR_CODES`` ``frozenset[str]`` constants.
  * Every literal assignment to ``task.error`` (and the alias
    ``terminal_task.error``) and ``step.error`` in
    ``mariana/agent/loop.py`` and ``mariana/agent/api_routes.py`` must
    carry a value drawn from the matching set.
  * ``_budget_exceeded`` returns canonical codes (``budget_exhausted`` /
    ``duration_exhausted``).
  * ``_infer_failure`` returns canonical step codes (``timed_out``,
    ``process_killed``, ``non_zero_exit``, ``http_error``) and never
    returns the raw formatted strings it used to.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from mariana.agent import loop as loop_mod


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
LOOP_PATH = REPO_ROOT / "mariana" / "agent" / "loop.py"
ROUTES_PATH = REPO_ROOT / "mariana" / "agent" / "api_routes.py"


# ---------------------------------------------------------------------------
# AST walker helpers
# ---------------------------------------------------------------------------


def _collect_error_assignments(
    src_path: pathlib.Path,
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Walk an AST and collect literal assignments to ``*.error``.

    Returns ``(task_assignments, step_assignments)`` — each entry is
    ``(lineno, literal_value)``.  Non-literal assignments (e.g.
    ``task.error = code`` where ``code`` is a Name) are skipped because
    those values flow through canonical-code paths already covered by
    the unit tests below; we only need to pin literal call sites.
    Assignments to ``None`` (e.g. ``step.error = None`` to clear) are
    also skipped — clearing is always allowed.
    """
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    task_lits: list[tuple[int, str]] = []
    step_lits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Attribute):
                continue
            if target.attr != "error":
                continue
            # Determine if this is task.error / terminal_task.error / step.error.
            if not isinstance(target.value, ast.Name):
                continue
            obj_name = target.value.id
            if obj_name in ("task", "terminal_task"):
                bucket = task_lits
            elif obj_name == "step":
                bucket = step_lits
            else:
                continue
            value = node.value
            # Plain string literal.
            if isinstance(value, ast.Constant):
                if value.value is None:
                    continue  # clearing
                if isinstance(value.value, str):
                    bucket.append((node.lineno, value.value))
                else:
                    raise AssertionError(
                        f"{src_path}:{node.lineno}: non-str literal assigned to "
                        f"{obj_name}.error: {value.value!r}"
                    )
            elif isinstance(value, ast.JoinedStr):
                # f-string — treated as a CC-35 violation (raw interpolation
                # is exactly what the canonical contract forbids).
                bucket.append((node.lineno, "<fstring>"))
            elif isinstance(value, ast.BinOp):
                # String concatenation — same class.
                bucket.append((node.lineno, "<binop>"))
            # else: Name / Call / etc. — flows through canonical paths
            # already pinned by the runtime unit tests below.
    return task_lits, step_lits


# ---------------------------------------------------------------------------
# (1) Allow-list constants exist and contain the documented codes.
# ---------------------------------------------------------------------------


def test_canonical_allow_lists_present_and_complete():
    """The allow-list constants must exist and contain the documented codes."""
    assert isinstance(loop_mod.CANONICAL_TASK_ERROR_CODES, frozenset)
    assert isinstance(loop_mod.CANONICAL_STEP_ERROR_CODES, frozenset)
    # Pin minimum membership — any future additions are fine, but these
    # are the ones the contract guarantees.
    required_task = {
        "stop_requested",
        "budget_exhausted",
        "duration_exhausted",
        "planner_failed",
        "deliver_failed",
        "unrecoverable",
        "vault_unavailable",
        "vault_transport_violation",
        "loop_crash",
    }
    required_step = {
        "tool_error",
        "unexpected",
        "timed_out",
        "process_killed",
        "non_zero_exit",
        "http_error",
    }
    assert required_task <= loop_mod.CANONICAL_TASK_ERROR_CODES
    assert required_step <= loop_mod.CANONICAL_STEP_ERROR_CODES
    # Sets must be disjoint by intent (task vs step domains differ).
    assert (
        loop_mod.CANONICAL_TASK_ERROR_CODES & loop_mod.CANONICAL_STEP_ERROR_CODES
    ) == frozenset()


# ---------------------------------------------------------------------------
# (2) Source-grep invariant: every literal task.error = "..." in the
#     two audited files must be in CANONICAL_TASK_ERROR_CODES.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [LOOP_PATH, ROUTES_PATH])
def test_task_error_literals_are_canonical(path):
    task_lits, _ = _collect_error_assignments(path)
    bad = [
        (lineno, value)
        for lineno, value in task_lits
        if value not in loop_mod.CANONICAL_TASK_ERROR_CODES
    ]
    assert not bad, (
        f"{path}: non-canonical task.error literal(s): {bad!r}. "
        f"Allowed: {sorted(loop_mod.CANONICAL_TASK_ERROR_CODES)}"
    )


# ---------------------------------------------------------------------------
# (3) Same source-grep for step.error.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [LOOP_PATH, ROUTES_PATH])
def test_step_error_literals_are_canonical(path):
    _, step_lits = _collect_error_assignments(path)
    bad = [
        (lineno, value)
        for lineno, value in step_lits
        if value not in loop_mod.CANONICAL_STEP_ERROR_CODES
    ]
    assert not bad, (
        f"{path}: non-canonical step.error literal(s): {bad!r}. "
        f"Allowed: {sorted(loop_mod.CANONICAL_STEP_ERROR_CODES)}"
    )


# ---------------------------------------------------------------------------
# (4) Unit: _budget_exceeded returns the canonical budget_exhausted code.
# ---------------------------------------------------------------------------


def _make_task(
    *, spent_usd: float = 0.0, budget_usd: float = 1.0, max_duration_hours: float = 1.0
):
    """Build a minimal AgentTask suitable for _budget_exceeded probing."""
    from mariana.agent.models import AgentTask  # noqa: PLC0415

    return AgentTask(
        id="00000000-0000-0000-0000-000000000000",
        user_id="u",
        goal="probe",
        budget_usd=budget_usd,
        spent_usd=spent_usd,
        max_duration_hours=max_duration_hours,
    )


def test_budget_exceeded_returns_canonical_budget_exhausted():
    task = _make_task(spent_usd=2.0, budget_usd=1.0)
    over, code, detail = loop_mod._budget_exceeded(
        task, started_at=__import__("time").time()
    )
    assert over is True
    assert code == "budget_exhausted"
    assert code in loop_mod.CANONICAL_TASK_ERROR_CODES
    # Detail must be structured, not a free-form string.
    assert isinstance(detail, dict)
    assert "spent_usd" in detail and "budget_usd" in detail


def test_budget_exceeded_returns_canonical_duration_exhausted():
    import time as _time  # noqa: PLC0415

    task = _make_task(spent_usd=0.0, budget_usd=1.0, max_duration_hours=0.001)
    # started_at way in the past trips the wallclock guard.
    over, code, detail = loop_mod._budget_exceeded(
        task, started_at=_time.time() - 3600.0
    )
    assert over is True
    assert code == "duration_exhausted"
    assert code in loop_mod.CANONICAL_TASK_ERROR_CODES
    assert isinstance(detail, dict)
    assert "elapsed_hours" in detail


# ---------------------------------------------------------------------------
# (5) Unit: _infer_failure returns canonical step codes only.
# ---------------------------------------------------------------------------


def test_infer_failure_canonical_timed_out():
    code = loop_mod._infer_failure(
        "code_exec",
        {"timed_out": True, "duration_ms": 1234, "exit_code": -9},
    )
    assert code == "timed_out"
    assert code in loop_mod.CANONICAL_STEP_ERROR_CODES
    # Must not be the legacy formatted string.
    assert "after" not in code
    assert "ms" not in code


def test_infer_failure_canonical_process_killed():
    code = loop_mod._infer_failure(
        "bash_exec", {"timed_out": False, "killed": True, "exit_code": -9}
    )
    assert code == "process_killed"
    assert code in loop_mod.CANONICAL_STEP_ERROR_CODES


def test_infer_failure_canonical_non_zero_exit():
    code = loop_mod._infer_failure(
        "code_exec",
        {"timed_out": False, "killed": False, "exit_code": 3},
    )
    assert code == "non_zero_exit"
    assert code in loop_mod.CANONICAL_STEP_ERROR_CODES
    # Legacy free-form text was "non-zero exit code 3"; pin we don't
    # accidentally re-introduce it.
    assert "exit code" not in code


def test_infer_failure_canonical_http_error_500():
    code = loop_mod._infer_failure("browser_fetch", {"status": 500})
    assert code == "http_error"
    assert code in loop_mod.CANONICAL_STEP_ERROR_CODES
    # Legacy free-form text was "HTTP 500".
    assert "500" not in code
    assert "HTTP" not in code


def test_infer_failure_canonical_http_error_404():
    code = loop_mod._infer_failure("browser_click_fetch", {"status": 404})
    assert code == "http_error"


def test_infer_failure_returns_none_on_success():
    assert loop_mod._infer_failure("code_exec", {"exit_code": 0}) is None
    assert loop_mod._infer_failure("browser_fetch", {"status": 200}) is None
