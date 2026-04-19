"""Sub-agent delegation system.

The orchestrator can break a complex research goal into subtasks, each
handled by a specialised sub-agent.  Sub-agents share the parent
investigation's ``CostTracker`` (for budget enforcement) and Redis channel
(for progress events) but execute independently.

Usage from the event loop::

    mgr = SubAgentManager(task.id, cost_tracker, redis_client, config)
    await mgr.delegate(SubAgentRole.RESEARCHER, "Deep dive on AAPL revenue recognition")
    await mgr.delegate(SubAgentRole.FACT_CHECKER, "Verify 2024 Q3 earnings claims")
    completed = await mgr.execute_all(session_kwargs)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class SubAgentRole(str, Enum):
    """Available sub-agent specialisations."""

    RESEARCHER = "researcher"
    DATA_ANALYST = "data_analyst"
    WRITER = "writer"
    FACT_CHECKER = "fact_checker"
    SEARCH = "search"
    MEDIA = "media"


@dataclass
class SubAgentTask:
    """A single sub-agent work item."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    role: SubAgentRole = SubAgentRole.RESEARCHER
    objective: str = ""
    context: str = ""
    result: str | None = None
    status: str = "pending"  # pending | running | completed | failed
    cost_usd: float = 0.0


_ROLE_PROMPTS: dict[SubAgentRole, str] = {
    SubAgentRole.RESEARCHER: (
        "You are a specialized research analyst. Conduct deep research on the "
        "assigned topic. Provide detailed findings with citations."
    ),
    SubAgentRole.DATA_ANALYST: (
        "You are a quantitative analyst. Analyze the provided data, identify "
        "patterns, calculate statistics, and present findings in structured format."
    ),
    SubAgentRole.WRITER: (
        "You are a professional financial writer. Create clear, well-structured "
        "documents from the provided research findings."
    ),
    SubAgentRole.FACT_CHECKER: (
        "You are a fact-checker. Verify each claim against primary sources. "
        "Flag any unverifiable or contradictory claims."
    ),
    SubAgentRole.SEARCH: (
        "You are a search specialist. Find relevant, authoritative sources for "
        "the given queries. Return structured results with full citations."
    ),
    SubAgentRole.MEDIA: (
        "You are a media content specialist. Generate appropriate visual "
        "content descriptions based on the requirements."
    ),
}


class SubAgentManager:
    """Manages sub-agent delegation for a single investigation."""

    def __init__(
        self,
        parent_task_id: str,
        cost_tracker: Any,
        redis_client: Any,
        config: Any,
    ) -> None:
        self.parent_task_id = parent_task_id
        self.cost_tracker = cost_tracker
        self.redis = redis_client
        self.config = config
        self.tasks: list[SubAgentTask] = []
        self._semaphore = asyncio.Semaphore(3)  # max 3 sub-agents per investigation

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def delegate(
        self,
        role: SubAgentRole,
        objective: str,
        context: str = "",
    ) -> SubAgentTask:
        """Create and queue a sub-agent task.

        The task is not executed until :meth:`execute_all` is called.
        """
        task = SubAgentTask(role=role, objective=objective, context=context)
        self.tasks.append(task)
        await self._emit(f"Sub-agent [{role.value}] queued: {objective[:80]}")
        return task

    async def execute_all(
        self,
        db: Any,
        config: Any,
    ) -> list[SubAgentTask]:
        """Execute all pending sub-agent tasks with a concurrency limit.

        Parameters
        ----------
        db:
            asyncpg pool — passed through to ``spawn_model``.
        config:
            AppConfig — passed through to ``spawn_model``.

        Returns
        -------
        list[SubAgentTask]
            Completed (or failed) sub-agent tasks.
        """
        pending = [t for t in self.tasks if t.status == "pending"]
        if not pending:
            return []

        async def _run(task: SubAgentTask) -> SubAgentTask:
            async with self._semaphore:
                # BUG-0044 fix: check budget before spawning any sub-agent.
                if self.cost_tracker.is_exhausted:
                    from mariana.orchestrator.cost_tracker import BudgetExhaustedError  # noqa: PLC0415
                    task.status = "failed"
                    task.result = "Budget exhausted before sub-agent could start"
                    logger.warning(
                        "sub_agent_budget_exhausted",
                        task_id=self.parent_task_id,
                        sub_agent_id=task.id,
                    )
                    raise BudgetExhaustedError(
                        "task",
                        self.cost_tracker.total_spent,
                        self.cost_tracker.task_budget,
                    )
                task.status = "running"
                await self._emit(f"Sub-agent [{task.role.value}] started: {task.objective[:60]}")
                try:
                    result = await self._execute_subtask(task, db, config)
                    task.result = result
                    task.status = "completed"
                    await self._emit(f"Sub-agent [{task.role.value}] completed")
                except Exception as exc:
                    task.status = "failed"
                    task.result = f"Error: {exc}"
                    await self._emit(f"Sub-agent [{task.role.value}] failed: {exc}")
                    logger.warning(
                        "sub_agent_failed",
                        task_id=self.parent_task_id,
                        sub_agent_id=task.id,
                        error=str(exc),
                    )
                return task

        results = await asyncio.gather(*[_run(t) for t in pending], return_exceptions=True)
        completed: list[SubAgentTask] = []
        for r in results:
            if isinstance(r, SubAgentTask):
                completed.append(r)
            elif isinstance(r, BaseException):
                logger.error("sub_agent_gather_exception", error=str(r), type=type(r).__name__)
        return completed

    def get_completed_context(self) -> str:
        """Aggregate results from all completed sub-agents into a context string."""
        parts: list[str] = []
        for t in self.tasks:
            if t.status == "completed" and t.result:
                parts.append(f"## Sub-agent [{t.role.value}]: {t.objective[:80]}\n{t.result}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _execute_subtask(
        self,
        task: SubAgentTask,
        db: Any,
        config: Any,
    ) -> str:
        """Execute a single sub-agent task using spawn_model."""
        from mariana.ai.session import spawn_model  # noqa: PLC0415
        from mariana.data.models import EvaluationOutput, TaskType  # noqa: PLC0415

        role_prompt = _ROLE_PROMPTS.get(task.role, _ROLE_PROMPTS[SubAgentRole.RESEARCHER])
        # Frame the objective as a hypothesis for the EVALUATION schema.
        # Include the role-specific instructions so the LLM understands its
        # specialisation while still producing the required structured output.
        hypothesis = f"[{task.role.value.upper()}] {role_prompt}\n\nObjective: {task.objective}"

        cost_before = self.cost_tracker.total_spent
        parsed_output, _session = await spawn_model(
            task_type=TaskType.EVALUATION,
            context={
                "task_id": self.parent_task_id,
                "hypothesis_id": task.id,
                "hypothesis_statement": hypothesis,
                "compressed_findings": task.context or "No prior findings.",
                "sources_searched": 0,
                "prior_scores": [],
                "budget_remaining": self.cost_tracker.budget_remaining,
            },
            output_schema=EvaluationOutput,
            branch_id=None,
            db=db,
            cost_tracker=self.cost_tracker,
            config=config,
        )
        task.cost_usd = self.cost_tracker.total_spent - cost_before
        # Extract the substantive content from the evaluation output
        return parsed_output.score_rationale

    async def _emit(self, message: str) -> None:
        """Emit a progress event to the parent investigation's Redis channel."""
        if self.redis is None:
            return
        event = json.dumps({"type": "text", "content": f"[SubAgent] {message}"})
        try:
            await self.redis.publish(f"logs:{self.parent_task_id}", event)
        except Exception:
            pass
