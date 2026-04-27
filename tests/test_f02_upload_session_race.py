"""F-02 regression suite: upload-session race fix in start_investigation.

Phase E re-audit (F-02) found that POST /api/investigations could be called
concurrently with the same ``upload_session_uuid``: both callers could pass the
ownership check, both could reserve credits, and then race on shutil.move /
rmdir — resulting in two tasks created with duplicate credit reservations and
files split or missing.

The fix:
  1. Hold ``_get_upload_lock(f"pending-{session_uuid}")`` across the entire
     ownership check + atomic-claim + file-move sequence.
  2. Atomically rename ``pending/{uuid}`` → ``claimed/{uuid}-{task_id}`` using
     ``os.rename`` before moving files.  The second concurrent caller finds the
     pending directory gone and receives 409 Conflict.
  3. The losing caller's credit reservation is refunded via
     ``_supabase_add_credits`` before raising 409.

Tests:
  1. test_concurrent_start_investigation_with_same_session_uuid_only_one_succeeds
  2. test_second_call_after_consume_returns_409
  3. test_credit_reservation_refunded_on_conflict
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import mariana.api as mod
from mariana.config import AppConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> AppConfig:
    """Build a minimal AppConfig whose DATA_ROOT is a temp directory."""
    cfg = AppConfig.__new__(AppConfig)
    object.__setattr__(cfg, "SUPABASE_URL", "https://supabase.test")
    object.__setattr__(cfg, "SUPABASE_KEY", "anon")
    object.__setattr__(cfg, "SUPABASE_SERVICE_ROLE_KEY", "service_role_xxx")
    object.__setattr__(cfg, "STRIPE_SECRET_KEY", "sk_test_xxx")
    object.__setattr__(cfg, "STRIPE_PUBLISHABLE_KEY", "pk_test_xxx")
    object.__setattr__(cfg, "STRIPE_WEBHOOK_SECRET", "whsec_xxx")
    object.__setattr__(cfg, "DATA_ROOT", str(tmp_path / "data"))
    return cfg


def _user(user_id: str = "user-abc-123") -> dict[str, str]:
    return {"user_id": user_id, "role": "authenticated"}


def _make_session(
    data_root: str,
    session_uuid: str,
    user_id: str,
    filenames: list[str] | None = None,
) -> Path:
    """Create a pending upload session directory with an owner file and test files."""
    pending_dir = Path(data_root) / "uploads" / "pending" / session_uuid
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / ".owner").write_text(user_id, encoding="utf-8")
    for name in filenames or ["doc.txt"]:
        (pending_dir / name).write_text(f"content of {name}", encoding="utf-8")
    return pending_dir


class _FakeRequest:
    """Minimal stand-in for FastAPI Request."""

    def __init__(self, user_id: str = "user-abc-123") -> None:
        self.headers = {"authorization": f"Bearer fake-jwt-{user_id}"}


class _StartBody:
    """Minimal StartInvestigationRequest stand-in."""

    def __init__(
        self,
        topic: str = "Test topic",
        upload_session_uuid: str | None = None,
    ) -> None:
        self.topic = topic
        self.upload_session_uuid = upload_session_uuid
        self.budget_usd = 1.00  # $1.00 is above the minimum $0.10
        self.duration_hours = None
        self.tier = "quick"
        self.quality_tier = None
        self.selected_model = None
        self.plan_approved = False
        self.continuous_mode = False
        self.dont_kill_branches = False
        self.force_report_on_halt = False
        self.skip_skeptic = False
        self.skip_tribunal = False
        self.user_directives = {}
        self.user_flow_instructions = ""
        self.conversation_id = None


# ---------------------------------------------------------------------------
# Shared patch context for start_investigation
# ---------------------------------------------------------------------------


def _make_patches(
    cfg: AppConfig,
    user: dict[str, str],
    add_credits_tracker: list[dict[str, Any]] | None = None,
) -> list:
    """Return a list of (patch_target, return_value_or_side_effect) tuples."""

    async def _fake_supabase_rest(cfg_, method, path, **kwargs):  # noqa: ANN001
        """Return a 200 plan response for any /profiles call."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [{"plan": "pro"}]
        return resp

    async def _fake_deduct_credits(user_id, amount, cfg_):  # noqa: ANN001
        return "ok"

    async def _fake_add_credits(user_id, credits, cfg_):  # noqa: ANN001
        if add_credits_tracker is not None:
            add_credits_tracker.append({"user_id": user_id, "credits": credits})

    async def _fake_db_insert(db, task):  # noqa: ANN001
        pass

    fake_db = MagicMock()

    return [
        patch.object(mod, "_get_config", return_value=cfg),
        patch.object(mod, "_get_current_user", return_value=user),
        patch.object(mod, "_supabase_rest", side_effect=_fake_supabase_rest),
        patch.object(mod, "_supabase_rest_system", new_callable=AsyncMock),
        patch.object(mod, "_supabase_deduct_credits", side_effect=_fake_deduct_credits),
        patch.object(mod, "_supabase_add_credits", side_effect=_fake_add_credits),
        patch.object(mod, "_db_insert_research_task", side_effect=_fake_db_insert),
        patch.object(mod, "_get_db", return_value=fake_db),
    ]


# ---------------------------------------------------------------------------
# 1. Concurrent race: only one caller wins; the other gets 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_start_investigation_with_same_session_uuid_only_one_succeeds(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Two simultaneous POST /api/investigations with the same upload_session_uuid:
    - exactly one returns 202 (StartInvestigationResponse)
    - exactly one raises HTTPException with status 409
    - files are attached to exactly one task
    """
    from fastapi import HTTPException

    session_uuid = str(uuid.uuid4())
    user_id = "user-race-test"
    cfg = _cfg(tmp_path)
    user = _user(user_id)

    pending_dir = _make_session(cfg.DATA_ROOT, session_uuid, user_id, ["file_a.txt", "file_b.txt"])
    assert pending_dir.is_dir()

    add_calls: list[dict[str, Any]] = []
    patches = _make_patches(cfg, user, add_credits_tracker=add_calls)

    async def _call() -> Any:
        body = _StartBody(upload_session_uuid=session_uuid)
        request = _FakeRequest(user_id)
        return await mod.start_investigation(request, body, current_user=user)

    ctxs = [p.__enter__() for p in patches]
    try:
        results = await asyncio.gather(_call(), _call(), return_exceptions=True)
    finally:
        for p in reversed(patches):
            p.__exit__(None, None, None)

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]

    # Exactly one must succeed and one must fail with 409
    assert len(successes) == 1, f"Expected 1 success, got: {results}"
    assert len(failures) == 1, f"Expected 1 failure, got: {results}"

    the_failure = failures[0]
    assert isinstance(the_failure, HTTPException), (
        f"Expected HTTPException, got {type(the_failure)}: {the_failure}"
    )
    assert the_failure.status_code == 409, (
        f"Expected 409, got {the_failure.status_code}: {the_failure.detail}"
    )

    # Files must have been moved to exactly one task directory
    winning_task_id = successes[0].task_id
    task_files_dir = Path(cfg.DATA_ROOT) / "files" / winning_task_id
    moved_files = [f.name for f in task_files_dir.iterdir() if f.is_file()]
    assert sorted(moved_files) == ["file_a.txt", "file_b.txt"], (
        f"Files should be in winning task dir, got: {moved_files}"
    )

    # Pending dir must be gone (renamed → claimed → cleaned up)
    assert not pending_dir.is_dir(), "pending_dir should have been consumed"

    # The losing caller must have had credits refunded
    assert len(add_calls) == 1, (
        f"Expected exactly 1 refund call (for the 409 loser), got {len(add_calls)}: {add_calls}"
    )
    assert add_calls[0]["user_id"] == user_id


# ---------------------------------------------------------------------------
# 2. Sequential second call after session already consumed returns 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_call_after_consume_returns_409(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """After one call successfully consumes the upload session, a second call
    with the same upload_session_uuid must get 409 Conflict (not 200)."""
    from fastapi import HTTPException

    session_uuid = str(uuid.uuid4())
    user_id = "user-seq-test"
    cfg = _cfg(tmp_path)
    user = _user(user_id)

    _make_session(cfg.DATA_ROOT, session_uuid, user_id, ["report.pdf"])

    add_calls: list[dict[str, Any]] = []
    patches = _make_patches(cfg, user, add_credits_tracker=add_calls)

    ctxs = [p.__enter__() for p in patches]
    try:
        body = _StartBody(upload_session_uuid=session_uuid)

        # First call — should succeed
        result1 = await mod.start_investigation(
            _FakeRequest(user_id), body, current_user=user
        )
        assert result1.task_id  # Got a task_id back — first call succeeded

        # Second call — pending dir is gone, must 409
        with pytest.raises(HTTPException) as exc_info:
            await mod.start_investigation(
                _FakeRequest(user_id), body, current_user=user
            )
    finally:
        for p in reversed(patches):
            p.__exit__(None, None, None)

    assert exc_info.value.status_code == 409
    assert "consumed" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# 3. Credit reservation refunded on 409 conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credit_reservation_refunded_on_conflict(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """When the upload session is already gone (race-lost scenario), the losing
    caller's reserved credits must be returned via _supabase_add_credits."""
    from fastapi import HTTPException

    session_uuid = str(uuid.uuid4())
    user_id = "user-refund-test"
    cfg = _cfg(tmp_path)
    user = _user(user_id)

    # Do NOT create the pending session directory — simulates session already consumed
    # The pending dir doesn't exist at all, so the outer `if pending_dir.is_dir()`
    # is False → no 409 is raised (session simply not found).
    # Instead, simulate the race by creating a pending dir, letting the first call
    # consume it, then tracking that the second call refunds.

    _make_session(cfg.DATA_ROOT, session_uuid, user_id, ["data.csv"])

    add_calls: list[dict[str, Any]] = []
    patches = _make_patches(cfg, user, add_credits_tracker=add_calls)

    ctxs = [p.__enter__() for p in patches]
    try:
        body = _StartBody(upload_session_uuid=session_uuid)

        # First call consumes the session
        await mod.start_investigation(_FakeRequest(user_id), body, current_user=user)

        # Second call: pending dir is gone → should 409 and refund
        with pytest.raises(HTTPException) as exc_info:
            await mod.start_investigation(_FakeRequest(user_id), body, current_user=user)
    finally:
        for p in reversed(patches):
            p.__exit__(None, None, None)

    assert exc_info.value.status_code == 409

    # The second call must have issued exactly one refund
    # (first call has no refund since it succeeded)
    assert len(add_calls) == 1, (
        f"Expected 1 refund call for the losing caller, got {len(add_calls)}: {add_calls}"
    )
    refund = add_calls[0]
    assert refund["user_id"] == user_id
    assert refund["credits"] > 0, "Refund amount must be positive"


# ---------------------------------------------------------------------------
# 4. No double-refund: the outer HTTPException handler does not re-refund
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_double_refund_on_409(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """The outer except HTTPException: handler must NOT call _supabase_add_credits
    a second time when the 409 path already performed the refund."""
    from fastapi import HTTPException

    session_uuid = str(uuid.uuid4())
    user_id = "user-no-double-refund"
    cfg = _cfg(tmp_path)
    user = _user(user_id)

    _make_session(cfg.DATA_ROOT, session_uuid, user_id, ["note.md"])

    add_calls: list[dict[str, Any]] = []
    patches = _make_patches(cfg, user, add_credits_tracker=add_calls)

    ctxs = [p.__enter__() for p in patches]
    try:
        body = _StartBody(upload_session_uuid=session_uuid)

        # First call consumes
        await mod.start_investigation(_FakeRequest(user_id), body, current_user=user)

        # Second call races and loses
        with pytest.raises(HTTPException):
            await mod.start_investigation(_FakeRequest(user_id), body, current_user=user)
    finally:
        for p in reversed(patches):
            p.__exit__(None, None, None)

    # Must be exactly 1 refund, not 2
    assert len(add_calls) == 1, (
        f"Expected exactly 1 refund (no double-refund), got {len(add_calls)}: {add_calls}"
    )


# ---------------------------------------------------------------------------
# 5. Happy path: no upload session → task created without 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_upload_session_succeeds(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """When no upload_session_uuid is provided, start_investigation proceeds
    normally without any 409 / refund interaction."""
    user_id = "user-no-session"
    cfg = _cfg(tmp_path)
    user = _user(user_id)

    add_calls: list[dict[str, Any]] = []
    patches = _make_patches(cfg, user, add_credits_tracker=add_calls)

    ctxs = [p.__enter__() for p in patches]
    try:
        body = _StartBody(upload_session_uuid=None)
        result = await mod.start_investigation(_FakeRequest(user_id), body, current_user=user)
    finally:
        for p in reversed(patches):
            p.__exit__(None, None, None)

    assert result.task_id  # Got a task_id back
    assert len(add_calls) == 0  # No refunds


# ---------------------------------------------------------------------------
# 6. Claimed dir is cleaned up after successful consume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claimed_dir_cleaned_up_after_success(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """After the winning caller moves files to the task directory, the
    claimed/ directory must be removed."""
    session_uuid = str(uuid.uuid4())
    user_id = "user-cleanup-test"
    cfg = _cfg(tmp_path)
    user = _user(user_id)

    _make_session(cfg.DATA_ROOT, session_uuid, user_id, ["page.html"])

    patches = _make_patches(cfg, user)
    ctxs = [p.__enter__() for p in patches]
    try:
        body = _StartBody(upload_session_uuid=session_uuid)
        result = await mod.start_investigation(_FakeRequest(user_id), body, current_user=user)
    finally:
        for p in reversed(patches):
            p.__exit__(None, None, None)

    claimed_base = Path(cfg.DATA_ROOT) / "uploads" / "claimed"
    if claimed_base.exists():
        leftover = list(claimed_base.iterdir())
        assert leftover == [], f"claimed/ dir should be empty after cleanup, found: {leftover}"

    # Files must be in the task dir
    task_dir = Path(cfg.DATA_ROOT) / "files" / result.task_id
    assert task_dir.is_dir()
    assert (task_dir / "page.html").is_file()
