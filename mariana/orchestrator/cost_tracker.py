"""
mariana/orchestrator/cost_tracker.py

In-memory cost tracking with hard budget enforcement.

All mutations happen in the asyncio event loop (single-threaded), so no
locking is required.  The companion Pydantic model `CostTracker` in
`mariana.data.models` is the *serialisable* snapshot; this class is the
*live* mutable state that owns the enforcement logic.
"""

from __future__ import annotations

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

        # BUG-AUD-04 fix: Dedup ledger. If a caller supplies a session_id
        # (typically AISession.id) to record_call / record_branch_spend we
        # remember it here and ignore repeat charges for the same session_id.
        # This protects against the double-count footgun where record_call
        # and record_branch_spend are both invoked for the same underlying
        # cost (docstring-only guarantee is not enough in practice).
        self._seen_session_ids: set[str] = set()

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
        # BUG-AUD-04 fix: dedup ledger — refuse to charge the same
        # session_id twice.  This prevents the common footgun where both
        # record_call() and record_branch_spend() attempt to charge for
        # the same underlying AI session.
        if session_id is not None:
            if session_id in self._seen_session_ids:
                logger.warning(
                    "cost_record_duplicate_session_id",
                    session_id=session_id,
                    branch_id=branch_id,
                )
                return
            self._seen_session_ids.add(session_id)

        cost = session.cost_usd

        # Accumulate totals
        self.total_spent += cost
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

        # Task-level cap check (bypassed in finalization mode)
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
        # BUG-AUD-04 fix: dedup ledger.  If the same session_id was already
        # charged (likely via record_call), silently no-op instead of
        # double-adding to both per_branch and total_spent.
        if session_id is not None:
            if session_id in self._seen_session_ids:
                logger.warning(
                    "branch_spend_duplicate_session_id",
                    session_id=session_id,
                    branch_id=branch_id,
                    amount=amount,
                )
                return
            self._seen_session_ids.add(session_id)
        self.per_branch[branch_id] = self.per_branch.get(branch_id, 0.0) + amount
        self.total_spent += amount
        logger.debug(
            "branch_spend_recorded",
            branch_id=branch_id,
            amount=amount,
            total=self.total_spent,
        )

    def record_raw_spend(self, amount: float, label: str = "misc") -> None:
        """Charge *amount* to the task total without a session or branch.

        Parameters
        ----------
        amount:
            USD to charge (must be ≥ 0).
        label:
            Human-readable category for logging only.
        """
        if amount < 0:
            raise ValueError(f"amount must be non-negative, got {amount!r}")
        self.total_spent += amount
        logger.debug("raw_spend_recorded", amount=amount, label=label, total=self.total_spent)

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
        consumed the entire budget.
        """
        if self.finalization_mode:
            return False
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
