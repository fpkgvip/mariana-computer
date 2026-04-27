"""
mariana/orchestrator/branch_manager.py

Pure-Python branch lifecycle management.  NO AI calls; this is Layer 1
deterministic logic only.

All functions accept an ``asyncpg.Pool`` (typed as ``Any`` to avoid a
hard dependency on asyncpg at import time — the type annotation is kept
for documentation purposes).

Budget constants mirror ``mariana.config.AppConfig`` defaults and are
re-declared here so the module is testable without a live config object.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog

from mariana.data.db import _row_to_dict
from mariana.data.models import Branch, BranchStatus, HypothesisStatus

if TYPE_CHECKING:
    # Avoid circular imports at runtime; only used for type-checker hints.
    from mariana.orchestrator.cost_tracker import CostTracker

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Budget / scoring constants — read from AppConfig when available,
# falling back to defaults if config is not loaded yet.
# ---------------------------------------------------------------------------

_DEFAULT_SCORE_KILL_THRESHOLD: float = 0.4
_DEFAULT_SCORE_DEEPEN_THRESHOLD: float = 0.7
_DEFAULT_SCORE_TRIBUNAL_THRESHOLD: float = 0.8
_DEFAULT_BUDGET_INITIAL: float = 5.00
_DEFAULT_BUDGET_GRANT_SCORE7: float = 20.00
_DEFAULT_BUDGET_GRANT_SCORE8: float = 50.00
_DEFAULT_BUDGET_HARD_CAP: float = 75.00


def _cfg_val(attr: str, default: float) -> float:
    """Read a float config value, falling back to *default* if unset.

    B-23 fix: previously imported non-existent ``get_config`` which caused
    an ImportError caught silently by the bare ``except Exception``, so all
    six thresholds always resolved to their module-level hardcoded defaults
    and operator environment overrides were silently ignored.

    The fix reads the value directly from environment variables (the AppConfig
    attribute names are identical to the env-var names).  This avoids calling
    ``load_config()`` — which requires POSTGRES_DSN / POSTGRES_PASSWORD — from
    a lightweight helper that only needs scalar float values.  If the env var is
    absent or cannot be converted to float, the default is returned.
    """
    raw = os.environ.get(attr)
    if raw is None:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


# Module-level aliases for backward compatibility — now config-aware.
SCORE_KILL_THRESHOLD: float = _DEFAULT_SCORE_KILL_THRESHOLD
SCORE_DEEPEN_THRESHOLD: float = _DEFAULT_SCORE_DEEPEN_THRESHOLD
SCORE_TRIBUNAL_THRESHOLD: float = _DEFAULT_SCORE_TRIBUNAL_THRESHOLD
BUDGET_INITIAL: float = _DEFAULT_BUDGET_INITIAL
BUDGET_GRANT_SCORE7: float = _DEFAULT_BUDGET_GRANT_SCORE7
BUDGET_GRANT_SCORE8: float = _DEFAULT_BUDGET_GRANT_SCORE8
BUDGET_HARD_CAP: float = _DEFAULT_BUDGET_HARD_CAP


def _load_config_thresholds() -> None:
    """Refresh module-level constants from AppConfig. Called at score_branch entry."""
    global SCORE_KILL_THRESHOLD, SCORE_DEEPEN_THRESHOLD, SCORE_TRIBUNAL_THRESHOLD
    global BUDGET_INITIAL, BUDGET_GRANT_SCORE7, BUDGET_GRANT_SCORE8, BUDGET_HARD_CAP
    SCORE_KILL_THRESHOLD = _cfg_val("SCORE_KILL_THRESHOLD", _DEFAULT_SCORE_KILL_THRESHOLD)
    SCORE_DEEPEN_THRESHOLD = _cfg_val("SCORE_DEEPEN_THRESHOLD", _DEFAULT_SCORE_DEEPEN_THRESHOLD)
    SCORE_TRIBUNAL_THRESHOLD = _cfg_val("SCORE_TRIBUNAL_THRESHOLD", _DEFAULT_SCORE_TRIBUNAL_THRESHOLD)
    BUDGET_INITIAL = _cfg_val("BUDGET_BRANCH_INITIAL", _DEFAULT_BUDGET_INITIAL)
    BUDGET_GRANT_SCORE7 = _cfg_val("BUDGET_BRANCH_GRANT_SCORE7", _DEFAULT_BUDGET_GRANT_SCORE7)
    BUDGET_GRANT_SCORE8 = _cfg_val("BUDGET_BRANCH_GRANT_SCORE8", _DEFAULT_BUDGET_GRANT_SCORE8)
    BUDGET_HARD_CAP = _cfg_val("BUDGET_BRANCH_HARD_CAP", _DEFAULT_BUDGET_HARD_CAP)

# BUG-010: On 0–1 scale, plateau delta of 0.1 (10% change) is appropriate
_PLATEAU_DELTA_THRESHOLD: float = 0.1
"""Score improvement below this value across the last two cycles is
considered a plateau (when in the mid-score band, 0–1 scale)."""

_PLATEAU_MIN_CYCLES: int = 2
"""Minimum number of completed cycles before a plateau check fires."""


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------


@dataclass
class BranchDecision:
    """Outcome returned by :func:`score_branch`.

    Attributes
    ----------
    action:
        One of ``"KILL"``, ``"CONTINUE"``, ``"GRANT_20"``, ``"GRANT_50"``.
    reason:
        Human-readable explanation logged to the orchestrator journal.
    grant_amount:
        The USD amount that should be granted (0.0 unless action is a
        GRANT_* variant).
    """

    action: str
    reason: str
    grant_amount: float = 0.0


# ---------------------------------------------------------------------------
# Branch lifecycle helpers
# ---------------------------------------------------------------------------


async def create_branch(
    hypothesis_id: str,
    task_id: str,
    db: Any,  # asyncpg.Pool
) -> Branch:
    """Create and persist a new Branch for the given hypothesis.

    The branch is initialised with the default starting budget
    (:data:`BUDGET_INITIAL`) and inserted into the database.

    Parameters
    ----------
    hypothesis_id:
        UUID of the parent :class:`~mariana.data.models.Hypothesis`.
    task_id:
        UUID of the parent :class:`~mariana.data.models.ResearchTask`.
    db:
        An asyncpg connection pool.

    Returns
    -------
    Branch
        The newly created and persisted branch.
    """
    branch = Branch(
        id=str(uuid.uuid4()),
        hypothesis_id=hypothesis_id,
        task_id=task_id,
        status=BranchStatus.ACTIVE,
        score_history=[],
        budget_allocated=BUDGET_INITIAL,
        budget_spent=0.0,
        grants_log=[],
        cycles_completed=0,
    )

    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO branches (
                id, hypothesis_id, task_id, status,
                score_history, budget_allocated, budget_spent,
                grants_log, cycles_completed, kill_reason,
                sources_searched, created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7,
                $8, $9, $10,
                $11, $12, $13
            )
            """,
            branch.id,
            branch.hypothesis_id,
            branch.task_id,
            branch.status.value,
            json.dumps(branch.score_history),
            branch.budget_allocated,
            branch.budget_spent,
            json.dumps(branch.grants_log),
            branch.cycles_completed,
            branch.kill_reason,
            json.dumps(branch.sources_searched),
            branch.created_at,
            branch.updated_at,
        )

    logger.info(
        "branch_created",
        branch_id=branch.id,
        hypothesis_id=hypothesis_id,
        task_id=task_id,
        initial_budget=BUDGET_INITIAL,
    )
    return branch


async def score_branch(
    branch_id: str,
    new_score: float,
    cost_spent_this_cycle: float,
    db: Any,  # asyncpg.Pool
    cost_tracker: CostTracker,
) -> BranchDecision:
    """Evaluate a branch after a research cycle and decide what to do next.

    Implements the exact decision tree from the architecture spec:

    1. Append *new_score* to ``score_history``.
    2. Hard budget cap check → ``KILL``.
    3. Score < :data:`SCORE_KILL_THRESHOLD` → ``KILL``.
    4. Score 4–6: plateau check (delta < :data:`_PLATEAU_DELTA_THRESHOLD`
       for two cycles after initial budget exhausted) → ``KILL``; else
       ``CONTINUE``.
    5. Score ≥ :data:`SCORE_DEEPEN_THRESHOLD`: grant $20 on first trigger;
       score ≥ :data:`SCORE_TRIBUNAL_THRESHOLD` with ≥1 prior grants:
       grant $50 (subject to hard cap).

    Parameters
    ----------
    branch_id:
        UUID of the branch to evaluate.
    new_score:
        The latest evaluation score (0–1 scale).
    cost_spent_this_cycle:
        USD spent during this cycle; used to update the branch's
        ``budget_spent`` field before running cap checks.
    db:
        asyncpg connection pool.
    cost_tracker:
        Live cost tracker; its ``per_branch`` ledger is updated.

    Returns
    -------
    BranchDecision
        The recommended action and (if a grant) the grant amount.
    """
    # ------------------------------------------------------------------ #
    # Step 0: Refresh config-driven thresholds
    # ------------------------------------------------------------------ #
    _load_config_thresholds()

    # ------------------------------------------------------------------ #
    # Step 0b: Load branch record from DB
    # ------------------------------------------------------------------ #
    row = await db.fetchrow(
        """
        SELECT id, hypothesis_id, task_id, status,
               score_history, budget_allocated, budget_spent,
               grants_log, cycles_completed, kill_reason,
               sources_searched, created_at, updated_at
        FROM branches WHERE id = $1
        """,
        branch_id,
    )
    if row is None:
        raise ValueError(f"Branch {branch_id!r} not found in database")

    # BUG-011: Use _row_to_dict() to decode JSONB columns before model_validate
    branch = Branch.model_validate({**_row_to_dict(row), "status": BranchStatus(row["status"])})

    # ------------------------------------------------------------------ #
    # Step 1: Record new score and update spend
    # ------------------------------------------------------------------ #
    branch.score_history.append(new_score)
    branch.budget_spent += cost_spent_this_cycle
    branch.cycles_completed += 1
    branch.updated_at = datetime.now(timezone.utc)  # BUG-001

    # BUG-NEW-07 fix: Do NOT call cost_tracker.record_branch_spend() here.
    # spawn_model() already called cost_tracker.record_call() internally,
    # which added cost_spent_this_cycle to total_spent (and optionally to
    # per_branch if a branch_id was passed).  Calling record_branch_spend()
    # again would double-count the cost in total_spent.  The per-branch
    # budget_spent DB field is updated directly via branch.budget_spent above.
    # (Intentionally no cost_tracker.record_branch_spend call here.)

    # ------------------------------------------------------------------ #
    # Step 2: Hard branch budget cap check
    # ------------------------------------------------------------------ #
    if branch.budget_spent >= BUDGET_HARD_CAP:
        decision = BranchDecision(
            action="KILL",
            reason=(
                f"Hard branch cap reached: ${branch.budget_spent:.2f} "
                f">= ${BUDGET_HARD_CAP:.2f}"
            ),
        )
        await kill_branch(branch_id, decision.reason, db)
        return decision

    # ------------------------------------------------------------------ #
    # Step 3: Hard score kill
    # ------------------------------------------------------------------ #
    if new_score < SCORE_KILL_THRESHOLD:
        decision = BranchDecision(
            action="KILL",
            reason=f"Score {new_score:.1f} < kill threshold {SCORE_KILL_THRESHOLD:.1f}",
        )
        await kill_branch(branch_id, decision.reason, db)
        return decision

    # ------------------------------------------------------------------ #
    # Step 4: Mid-range plateau detection (score 4–6)
    # ------------------------------------------------------------------ #
    if new_score < SCORE_DEEPEN_THRESHOLD:
        # Plateau check: only fires once the initial budget has been
        # consumed (i.e., after at least _PLATEAU_MIN_CYCLES cycles) and
        # when we have enough score history to compute a delta.
        past_initial = branch.cycles_completed >= _PLATEAU_MIN_CYCLES
        has_prior_scores = len(branch.score_history) >= 2

        if past_initial and has_prior_scores:
            score_delta = abs(
                branch.score_history[-1] - branch.score_history[-2]
            )
            if score_delta < _PLATEAU_DELTA_THRESHOLD:
                decision = BranchDecision(
                    action="KILL",
                    reason=(
                        f"Score plateau detected: delta {score_delta:.2f} < "
                        f"{_PLATEAU_DELTA_THRESHOLD:.1f} after "
                        f"{branch.cycles_completed} cycles (score={new_score:.1f})"
                    ),
                )
                await kill_branch(branch_id, decision.reason, db)
                return decision

        # No plateau; keep going in the mid-range
        await _persist_branch(branch, db)
        logger.info(
            "branch_continue",
            branch_id=branch_id,
            score=new_score,
            cycles=branch.cycles_completed,
        )
        return BranchDecision(action="CONTINUE", reason=f"Score {new_score:.1f} in mid-range, no plateau")

    # ------------------------------------------------------------------ #
    # Step 5: High score — grant budget
    # ------------------------------------------------------------------ #
    prior_grants = len(branch.grants_log)

    # Score ≥ 8 with at least one prior grant → try to give $50
    if new_score >= SCORE_TRIBUNAL_THRESHOLD and prior_grants >= 1:
        proposed_total = branch.budget_allocated + BUDGET_GRANT_SCORE8
        if proposed_total <= BUDGET_HARD_CAP:
            # grant_budget updates budget_allocated and grants_log directly in DB.
            # We persist score_history/budget_spent/cycles first, THEN grant,
            # so the grant is not overwritten by stale in-memory state.
            await _persist_branch(branch, db)
            await grant_budget(branch_id, BUDGET_GRANT_SCORE8, db, cost_tracker)
            return BranchDecision(
                action="GRANT_50",
                reason=(
                    f"Score {new_score:.1f} >= {SCORE_TRIBUNAL_THRESHOLD:.1f} "
                    f"with {prior_grants} prior grant(s); granting ${BUDGET_GRANT_SCORE8:.2f}"
                ),
                grant_amount=BUDGET_GRANT_SCORE8,
            )
        else:
            # Would exceed hard cap; just continue without a grant
            await _persist_branch(branch, db)
            return BranchDecision(
                action="CONTINUE",
                reason=(
                    f"Score {new_score:.1f} qualifies for $50 grant but "
                    f"would exceed hard cap (current alloc=${branch.budget_allocated:.2f})"
                ),
            )

    # Score ≥ 7 with no prior grants → give $20
    if new_score >= SCORE_DEEPEN_THRESHOLD and prior_grants == 0:
        proposed_total = branch.budget_allocated + BUDGET_GRANT_SCORE7
        if proposed_total <= BUDGET_HARD_CAP:
            # Persist cycle data first, then grant (avoids overwriting grant with stale state)
            await _persist_branch(branch, db)
            await grant_budget(branch_id, BUDGET_GRANT_SCORE7, db, cost_tracker)
            return BranchDecision(
                action="GRANT_20",
                reason=(
                    f"Score {new_score:.1f} >= {SCORE_DEEPEN_THRESHOLD:.1f}; "
                    f"first grant of ${BUDGET_GRANT_SCORE7:.2f}"
                ),
                grant_amount=BUDGET_GRANT_SCORE7,
            )
        else:
            await _persist_branch(branch, db)
            return BranchDecision(
                action="CONTINUE",
                reason=(
                    f"Score {new_score:.1f} qualifies for $20 grant but "
                    f"would exceed hard cap (current alloc=${branch.budget_allocated:.2f})"
                ),
            )

    # Score ≥ 7 but already granted once and not yet at $50 threshold;
    # continue deepening.
    await _persist_branch(branch, db)
    return BranchDecision(
        action="CONTINUE",
        reason=f"Score {new_score:.1f} high; continuing with existing allocation",
    )


async def kill_branch(
    branch_id: str,
    reason: str,
    db: Any,  # asyncpg.Pool
) -> None:
    """Mark a branch as KILLED and persist the kill reason.

    Parameters
    ----------
    branch_id:
        UUID of the branch to kill.
    reason:
        Human-readable explanation stored in ``kill_reason``.
    db:
        asyncpg connection pool.
    """
    now = datetime.now(timezone.utc)  # BUG-001
    async with db.acquire() as conn:
        async with conn.transaction():
            hypothesis_id = await conn.fetchval(
                "SELECT hypothesis_id FROM branches WHERE id = $1",
                branch_id,
            )
            await conn.execute(
                """
                UPDATE branches
                SET status = $1,
                    kill_reason = $2,
                    updated_at = $3
                WHERE id = $4
                """,
                BranchStatus.KILLED.value,
                reason,
                now,
                branch_id,
            )
            # BUG-R5-04 fix: branch termination must retire the parent
            # hypothesis as well.  Otherwise hypotheses remain ACTIVE forever,
            # which breaks failed_hypotheses reporting and causes rotation
            # handoffs to keep listing dead hypotheses as still active.
            if hypothesis_id is not None:
                await conn.execute(
                    """
                    UPDATE hypotheses
                    SET status = $1,
                        updated_at = $2
                    WHERE id = $3 AND status = 'ACTIVE'
                    """,
                    HypothesisStatus.KILLED.value,
                    now,
                    hypothesis_id,
                )
    logger.info("branch_killed", branch_id=branch_id, reason=reason)


async def grant_budget(
    branch_id: str,
    amount: float,
    db: Any,  # asyncpg.Pool
    cost_tracker: CostTracker,
) -> None:
    """Increase a branch's allocated budget by *amount* and log the grant.

    The grant is appended to ``grants_log`` with a timestamp.  The live
    cost tracker's branch ledger is **not** updated here (grants represent
    future authorised spend, not actual spend).

    H-06 fix: the previous implementation performed a read-check-update
    sequence which was vulnerable to a TOCTOU race: two concurrent grants
    could both observe a pre-update allocation, both pass the hard-cap
    check, and collectively stack past BUDGET_HARD_CAP.  We now use a
    single atomic ``UPDATE ... WHERE budget_allocated + $1 <= $3
    RETURNING ...`` statement, and atomically append to grants_log with
    PostgreSQL's JSONB concatenation operator in one SQL round-trip.

    Parameters
    ----------
    branch_id:
        UUID of the branch to grant.
    amount:
        USD to add to ``budget_allocated``.
    db:
        asyncpg connection pool.
    cost_tracker:
        Live cost tracker (currently unused but kept for future hard-cap
        cross-checks at grant time).
    """
    if amount <= 0:
        raise ValueError(f"Grant amount must be positive, got {amount!r}")

    now = datetime.now(timezone.utc)  # BUG-001/056: timezone-aware datetime
    grant_record = {
        "reason": "score_grant",
        "amount": amount,
        "timestamp": now.isoformat(),
    }

    # H-06 fix: atomic check-and-update against BUDGET_HARD_CAP. Row is
    # locked implicitly by the UPDATE. grants_log is stored as JSON (either
    # JSONB or TEXT depending on schema), so we handle both by running the
    # append inside a short transaction when the column is TEXT and using a
    # single UPDATE when it's JSONB.  The safest portable form: read + update
    # inside an explicit transaction with SELECT ... FOR UPDATE so the row
    # is locked and the TOCTOU window is closed.
    async with db.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT budget_allocated, grants_log
                FROM branches
                WHERE id = $1
                FOR UPDATE
                """,
                branch_id,
            )
            if row is None:
                raise ValueError(f"Branch {branch_id!r} not found")

            current_allocated: float = row["budget_allocated"]
            proposed_total = current_allocated + amount
            if proposed_total > BUDGET_HARD_CAP:
                logger.warning(
                    "budget_grant_blocked_hard_cap",
                    branch_id=branch_id,
                    current_allocated=current_allocated,
                    amount=amount,
                    hard_cap=BUDGET_HARD_CAP,
                )
                raise ValueError(
                    f"Refusing to grant: ${proposed_total:.2f} would exceed "
                    f"BUDGET_HARD_CAP=${BUDGET_HARD_CAP:.2f}"
                )

            raw_grants = row["grants_log"]
            if isinstance(raw_grants, str):
                current_grants = json.loads(raw_grants) if raw_grants else []
            elif isinstance(raw_grants, list):
                current_grants = list(raw_grants)
            else:
                current_grants = []
            current_grants.append(grant_record)

            new_allocated = proposed_total

            await conn.execute(
                """
                UPDATE branches
                SET budget_allocated = $1,
                    grants_log = $2,
                    updated_at = $3
                WHERE id = $4
                """,
                new_allocated,
                json.dumps(current_grants),
                now,
                branch_id,
            )

    logger.info(
        "budget_granted",
        branch_id=branch_id,
        amount=amount,
        new_allocated=new_allocated,
    )


async def get_active_branches(
    task_id: str,
    db: Any,  # asyncpg.Pool
) -> list[Branch]:
    """Fetch all ACTIVE branches for a given task.

    Parameters
    ----------
    task_id:
        UUID of the parent ResearchTask.
    db:
        asyncpg connection pool.

    Returns
    -------
    list[Branch]
        All branches with ``status == ACTIVE``, ordered by
        ``created_at ASC``.
    """
    rows = await db.fetch(
        """
        SELECT id, hypothesis_id, task_id, status,
               score_history, budget_allocated, budget_spent,
               grants_log, cycles_completed, kill_reason,
               sources_searched, created_at, updated_at
        FROM branches
        WHERE task_id = $1 AND status = $2
        ORDER BY created_at ASC
        """,
        task_id,
        BranchStatus.ACTIVE.value,
    )
    # BUG-011: Use _row_to_dict() to decode JSONB columns before model_validate
    return [
        Branch.model_validate({**_row_to_dict(row), "status": BranchStatus(row["status"])})
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _persist_branch(branch: Branch, db: Any) -> None:
    """Write the mutable fields of *branch* back to the database."""
    await db.execute(
        """
        UPDATE branches
        SET score_history = $1,
            budget_spent = $2,
            cycles_completed = $3,
            updated_at = $4
        WHERE id = $5
        """,
        json.dumps(branch.score_history),
        branch.budget_spent,
        branch.cycles_completed,
        branch.updated_at,
        branch.id,
    )
