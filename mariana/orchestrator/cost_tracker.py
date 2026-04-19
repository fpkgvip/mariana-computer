"""
mariana/orchestrator/cost_tracker.py

In-memory cost tracking with hard budget enforcement.

All mutations happen in the asyncio event loop (single-threaded), so no
locking is required.  The companion Pydantic model `CostTracker` in
`mariana.data.models` is the *serialisable* snapshot; this class is the
*live* mutable state that owns the enforcement logic.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

import structlog

from mariana.data.models import AISession
from mariana.data.models import CostTracker as CostTrackerModel

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BudgetExhaustedError(Exception):
    """Raised when a cost-cap (task-level or branch-level) is exceeded.

    Attributes
    ----------
    scope:
        ``"task"`` when the task-level budget is blown;
        ``"branch"`` when a single branch exceeds its hard cap.
    spent:
        Cumulative USD spend that triggered the error.
    cap:
        The limit that was exceeded.
    """

    def __init__(self, scope: str, spent: float, cap: float) -> None:
        self.scope = scope
        self.spent = spent
        self.cap = cap
        super().__init__(
            f"Budget exhausted ({scope}): ${spent:.4f} / ${cap:.4f}"
        )


# ---------------------------------------------------------------------------
# Live cost tracker
# ---------------------------------------------------------------------------


class CostTracker:
    """Mutable in-memory cost tracker; thread-safe for asyncio (single-threaded).

    Parameters
    ----------
    task_id:
        UUID of the owning ResearchTask.
    task_budget:
        Maximum USD allowed for the entire task.
    branch_hard_cap:
        Per-branch hard spending ceiling (default $75).  A branch that
        exceeds this cap will be killed regardless of its score.
    """

    # ------------------------------------------------------------------
    # Safety constants
    # ------------------------------------------------------------------

    # H-11 fix: even in finalization_mode, spend is capped at this fraction
    # of the original task budget to prevent unbounded post-investigation
    # overspend.  15% is enough for report gen + intelligence hooks while
    # still bounding the blast radius.
    _FINALIZATION_BUDGET_FRACTION: float = 0.15

    # M-07 fix: cap the dedup set size so long investigations don't grow
    # memory without bound.  We keep the N most-recent session_ids and
    # evict oldest when the cap is hit.
    _DEDUP_MAX_ENTRIES: int = 50_000

    # M-08 fix: upper bound on a single record_raw_spend() call to stop a
    # runaway code path from charging an absurd value in one shot.
    _RAW_SPEND_MAX_PER_CALL: float = 100.0

    def __init__(
        self,
        task_id: str,
        task_budget: float,
        branch_hard_cap: float = 75.0,
    ) -> None:
        if task_budget <= 0:
            raise ValueError(f"task_budget must be positive, got {task_budget!r}")
        if branch_hard_cap <= 0:
            raise ValueError(
                f"branch_hard_cap must be positive, got {branch_hard_cap!r}"
            )

        self.task_id: str = task_id
        self.task_budget: float = task_budget
        self.branch_hard_cap: float = branch_hard_cap

        # Running totals
        self.total_spent: float = 0.0
        self.per_model: dict[str, float] = {}
        self.per_branch: dict[str, float] = {}

        # When True, budget checks are bypassed (for post-investigation
        # intelligence hooks that MUST run regardless of remaining budget).
        self.finalization_mode: bool = False
        self.call_count: int = 0

        # H-11 fix: track spend entered while in finalization_mode so the
        # finalization cap can be enforced separately from the main budget.
        self.finalization_spent: float = 0.0
        self.finalization_budget: float = task_budget * self._FINALIZATION_BUDGET_FRACTION

        # H-05 fix: serialise the accumulate-and-check sequence so two
        # concurrent record_call() invocations can't each individually pass
        # the cap check and collectively exceed the budget. A threading.Lock
        # is used (rather than asyncio.Lock) because these methods are
        # synchronous, get invoked from both asyncio tasks and persistence
        # callbacks, and the crit-section is short / non-blocking.
        self._lock: threading.RLock = threading.RLock()

        # BUG-AUD-04 fix: Dedup ledger. If a caller supplies a session_id
        # (typically AISession.id) to record_call / record_branch_spend we
        # remember it here and ignore repeat charges for the same session_id.
        # This protects against the double-count footgun where record_call
        # and record_branch_spend are both invoked for the same underlying
        # cost (docstring-only guarantee is not enough in practice).
        # M-07 fix: use a bounded OrderedDict as an LRU set so memory doesn't
        # grow unbounded across a long-running investigation.
        self._seen_session_ids: OrderedDict[str, None] = OrderedDict()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _seen_or_mark(self, session_id: str | None) -> bool:
        """Return True if *session_id* was already recorded; otherwise mark and return False.

        Bounded LRU eviction keeps the dedup ledger from unbounded growth.
        """
        if session_id is None:
            return False
        if session_id in self._seen_session_ids:
            self._seen_session_ids.move_to_end(session_id)
            return True
        self._seen_session_ids[session_id] = None
        if len(self._seen_session_ids) > self._DEDUP_MAX_ENTRIES:
            # Drop the oldest entry.
            self._seen_session_ids.popitem(last=False)
        return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_call(
        self,
        session: AISession,
        branch_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Record the cost from a completed AI session.

        Updates running totals and enforces both branch and task caps.

        Parameters
        ----------
        session:
            The completed ``AISession`` whose ``cost_usd`` is to be
            charged.
        branch_id:
            If provided, the cost is also charged to this branch's
            sub-ledger and the branch hard-cap is checked.
        session_id:
            Optional unique identifier for the cost event.  When
            provided, duplicate calls with the same ``session_id`` are
            logged and ignored (BUG-AUD-04 dedup ledger).

        Raises
        ------
        BudgetExhaustedError
            If the branch cap or task budget is exceeded *after* recording
            the cost (i.e., the call itself was the straw that broke the
            camel's back).
        """
        # H-05 fix: dedup, accumulate, and cap-check under a single lock so
        # concurrent callers can't both race past the cap.
        with self._lock:
            # BUG-AUD-04 fix: dedup ledger — refuse to charge the same
            # session_id twice.
            if self._seen_or_mark(session_id):
                logger.warning(
                    "cost_record_duplicate_session_id",
                    session_id=session_id,
                    branch_id=branch_id,
                )
                return

            cost = session.cost_usd

            # Accumulate totals
            self.total_spent += cost
            if self.finalization_mode:
                self.finalization_spent += cost
            model_key = session.model_used.value
            self.per_model[model_key] = self.per_model.get(model_key, 0.0) + cost
            self.call_count += 1

            # Branch-level accounting + cap check
            # BUG-053: Use `is not None` instead of truthiness check to allow empty-string
            # branch_id (though in practice all IDs are UUIDs or None)
            if branch_id is not None:
                self.per_branch[branch_id] = (
                    self.per_branch.get(branch_id, 0.0) + cost
                )
                if self.per_branch[branch_id] > self.branch_hard_cap:
                    logger.warning(
                        "branch_budget_exhausted",
                        branch_id=branch_id,
                        branch_spent=self.per_branch[branch_id],
                        cap=self.branch_hard_cap,
                    )
                    raise BudgetExhaustedError(
                        "branch",
                        self.per_branch[branch_id],
                        self.branch_hard_cap,
                    )

            # H-11 fix: even in finalization_mode, enforce a hard cap on
            # post-investigation spend (15% of task_budget by default).
            if (
                self.finalization_mode
                and self.finalization_spent > self.finalization_budget
            ):
                logger.warning(
                    "finalization_budget_exhausted",
                    finalization_spent=self.finalization_spent,
                    finalization_budget=self.finalization_budget,
                )
                raise BudgetExhaustedError(
                    "finalization",
                    self.finalization_spent,
                    self.finalization_budget,
                )

            # Task-level cap check (bypassed in finalization mode, but
            # finalization_budget above still applies).
            if not self.finalization_mode and self.total_spent > self.task_budget:
                logger.warning(
                    "task_budget_exhausted",
                    total_spent=self.total_spent,
                    task_budget=self.task_budget,
                )
                raise BudgetExhaustedError(
                    "task", self.total_spent, self.task_budget
                )

        logger.info(
            "cost_recorded",
            cost_usd=cost,
            total_usd=self.total_spent,
            budget_remaining_usd=self.budget_remaining,
            model=model_key,
            call_count=self.call_count,
            branch_id=branch_id,
        )

    def record_branch_spend(
        self,
        branch_id: str,
        amount: float,
        session_id: str | None = None,
    ) -> None:
        """Manually charge *amount* USD to *branch_id* without an AISession.

        Updates both the per-branch sub-ledger AND ``total_spent`` so that
        task-level budget enforcement remains accurate.  This is required
        for browser/connector costs that don't go through the AI layer.

        Parameters
        ----------
        branch_id:
            Branch to charge.
        amount:
            USD amount (must be ≥ 0).
        session_id:
            Optional unique identifier for the cost event.  When
            provided, duplicate calls with the same ``session_id`` are
            logged and ignored (BUG-AUD-04 dedup ledger).
        """
        if amount < 0:
            raise ValueError(f"amount must be non-negative, got {amount!r}")
        with self._lock:
            # BUG-AUD-04 fix: dedup ledger.
            if self._seen_or_mark(session_id):
                logger.warning(
                    "branch_spend_duplicate_session_id",
                    session_id=session_id,
                    branch_id=branch_id,
                    amount=amount,
                )
                return
            self.per_branch[branch_id] = self.per_branch.get(branch_id, 0.0) + amount
            self.total_spent += amount
            if self.finalization_mode:
                self.finalization_spent += amount
                if self.finalization_spent > self.finalization_budget:
                    logger.warning(
                        "finalization_budget_exhausted",
                        finalization_spent=self.finalization_spent,
                        finalization_budget=self.finalization_budget,
                    )
                    raise BudgetExhaustedError(
                        "finalization",
                        self.finalization_spent,
                        self.finalization_budget,
                    )
        logger.debug(
            "branch_spend_recorded",
            branch_id=branch_id,
            amount=amount,
            total=self.total_spent,
        )

    def record_raw_spend(self, amount: float, label: str = "misc") -> None:
        """Charge *amount* to the task total without a session or branch.

        M-08 fix: enforce a per-call sanity cap and task-level budget cap
        even for this bypass-style charge path so no code path can silently
        exceed the configured task budget.

        Parameters
        ----------
        amount:
            USD to charge (must be ≥ 0).
        label:
            Human-readable category for logging only.
        """
        if amount < 0:
            raise ValueError(f"amount must be non-negative, got {amount!r}")
        if amount > self._RAW_SPEND_MAX_PER_CALL:
            raise ValueError(
                f"record_raw_spend amount {amount!r} exceeds per-call cap "
                f"${self._RAW_SPEND_MAX_PER_CALL}"
            )
        with self._lock:
            self.total_spent += amount
            if self.finalization_mode:
                self.finalization_spent += amount
                if self.finalization_spent > self.finalization_budget:
                    raise BudgetExhaustedError(
                        "finalization",
                        self.finalization_spent,
                        self.finalization_budget,
                    )
            elif self.total_spent > self.task_budget:
                raise BudgetExhaustedError(
                    "task", self.total_spent, self.task_budget
                )
        logger.debug(
            "raw_spend_recorded",
            amount=amount,
            label=label,
            total=self.total_spent,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def budget_remaining(self) -> float:
        """Remaining task budget in USD (never negative)."""
        return max(0.0, self.task_budget - self.total_spent)

    @property
    def total_with_markup(self) -> float:
        """Total USD spent including 20% platform markup."""
        return self.total_spent * 1.20

    @property
    def is_exhausted(self) -> bool:
        """True when the task budget has been fully consumed.

        Returns False when ``finalization_mode`` is active so that
        post-investigation intelligence hooks (perspectives, audit,
        executive summary) can always run even if the main loop
        consumed the entire budget — unless the finalization cap has
        also been exhausted (H-11).
        """
        if self.finalization_mode:
            return self.finalization_spent >= self.finalization_budget
        return self.total_spent >= self.task_budget

    def branch_remaining(self, branch_id: str) -> float:
        """Remaining budget for a specific branch (never negative)."""
        spent = self.per_branch.get(branch_id, 0.0)
        return max(0.0, self.branch_hard_cap - spent)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_model(self) -> CostTrackerModel:
        """Return a serialisable Pydantic snapshot of the current state."""
        return CostTrackerModel(
            task_id=self.task_id,
            total_spent=self.total_spent,
            task_budget=self.task_budget,
            per_model_breakdown=dict(self.per_model),
            per_branch_breakdown=dict(self.per_branch),
            call_count=self.call_count,
        )

    def __repr__(self) -> str:
        return (
            f"CostTracker(task_id={self.task_id!r}, "
            f"spent=${self.total_spent:.4f}, "
            f"budget=${self.task_budget:.2f}, "
            f"calls={self.call_count})"
        )
