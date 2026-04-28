"""Pydantic models for Mariana agent tasks, steps, and artifacts.

These are the persisted shape of an agent task.  They are stored in the
Postgres table ``agent_tasks`` (schema: ``mariana/agent/schema.sql``) and
streamed to the frontend over SSE.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AgentState(str, Enum):
    PLAN = "plan"
    EXECUTE = "execute"
    TEST = "test"
    FIX = "fix"
    REPLAN = "replan"
    DELIVER = "deliver"
    DONE = "done"
    FAILED = "failed"
    HALTED = "halted"
    # O-02: terminal state for tasks cancelled before any worker spend.
    # Distinct from HALTED (which is what the worker uses when it observes a
    # mid-run stop_requested) so the lifecycle log can tell "never ran" apart
    # from "ran and was stopped". Both are terminal and trigger settlement.
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


# The finite set of tool names the agent can call.  Anything else the LLM
# emits is rejected by the dispatcher with a diagnostic error that feeds
# back into the self-correction loop.
TOOL_NAMES = Literal[
    "code_exec",
    "bash_exec",
    "typescript_exec",
    "rust_exec",
    "fs_read",
    "fs_write",
    "fs_list",
    "fs_delete",
    "browser_fetch",
    "browser_screenshot",
    "browser_pdf",
    "browser_click_fetch",
    "web_search",
    "think",
    "deliver",
    # v3 additions.
    "generate_image",
    "generate_video",
    "describe_self",
    # Deft v2: ship a static preview of the workspace.
    "deploy_preview",
]


# ---------------------------------------------------------------------------
# Step
# ---------------------------------------------------------------------------


class AgentStep(BaseModel):
    """A single step in the agent plan."""

    model_config = ConfigDict(extra="forbid")

    id: str
    # Human-readable rationale for the step.  Shown in the UI.
    title: str
    description: str = ""
    tool: TOOL_NAMES
    # Tool-specific parameters.  Validated at dispatch time.
    params: dict[str, Any] = Field(default_factory=dict)
    # Execution result.
    status: StepStatus = StepStatus.PENDING
    result: dict[str, Any] | None = None
    error: str | None = None
    attempts: int = 0
    # Timestamps (unix seconds).
    started_at: float | None = None
    finished_at: float | None = None
    # Cost attribution.
    cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


class AgentArtifact(BaseModel):
    """A file the agent produced, persisted in the user workspace."""

    model_config = ConfigDict(extra="forbid")

    name: str
    workspace_path: str
    size: int
    sha256: str
    produced_by_step: str | None = None


class AgentTask(BaseModel):
    """A complete agent task: user request, plan, execution state, artifacts."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    conversation_id: str | None = None
    # The raw user request.
    goal: str
    # Any extra context the orchestrator / chat classifier attached.
    user_instructions: str | None = None

    # Plan and execution state.
    state: AgentState = AgentState.PLAN
    steps: list[AgentStep] = Field(default_factory=list)
    artifacts: list[AgentArtifact] = Field(default_factory=list)

    # Selected orchestrator model (Opus / Sonnet / Gemini / DeepSeek).
    selected_model: str = "claude-opus-4-7"

    # Budget.
    max_duration_hours: float = 2.0
    budget_usd: float = 5.0
    spent_usd: float = 0.0

    # M-01 fix: credit reservation accounting (persisted by N-01).
    # ``reserved_credits`` is the up-front Supabase deduction made by
    # ``POST /api/agent`` (canonical 100 credits/USD).  The runtime settles
    # this in :func:`mariana.agent.loop._settle_agent_credits` once the task
    # reaches a terminal state — refunding unused credits via ``add_credits``
    # or deducting the overrun via ``deduct_credits``.  ``credits_settled``
    # makes settlement idempotent: any subsequent call observes the flag and
    # short-circuits, so a retried orchestrator pass cannot double-charge or
    # double-refund.  Both fields are persisted to ``agent_tasks`` (see
    # ``mariana/agent/schema.sql``) so the queue-consumer reload path
    # (``mariana/main.py``) preserves them across worker restarts and
    # requeues — without persistence the worker would always observe
    # ``reserved_credits=0`` and skip settlement entirely.
    reserved_credits: int = 0
    credits_settled: bool = False

    # Retry budgets — global caps so the loop can't spin forever.
    max_fix_attempts_per_step: int = 5
    max_replans: int = 3
    replan_count: int = 0

    # Overall fail count, separate from attempts per step.
    total_failures: int = 0

    # Derived output: the final user-facing answer assembled in DELIVER.
    final_answer: str | None = None

    # Timestamps.
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))

    # Lifecycle flags.
    stop_requested: bool = False
    error: str | None = None

    # U-03 fix: True when the user submitted a non-empty ``vault_env``
    # alongside this task.  The agent loop reads this BEFORE invoking
    # any tool: if True and the per-task secrets cannot be retrieved
    # from Redis (transport down, payload missing/evicted, etc.) the
    # task fails closed via :class:`mariana.vault.runtime.VaultUnavailableError`
    # instead of running with empty env (which would silently strip
    # the user's secrets).  Default False keeps the no-vault path free
    # of any Redis dependency.
    requires_vault: bool = False


# ---------------------------------------------------------------------------
# Event emitted on the SSE channel
# ---------------------------------------------------------------------------


class AgentEvent(BaseModel):
    """One UI event sent over the SSE stream."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    event_type: Literal[
        "state_change",
        "plan_created",
        "step_started",
        "step_progress",
        "step_completed",
        "step_failed",
        "artifact_created",
        "terminal_output",
        "thinking",
        "delivered",
        "error",
        "halted",
    ]
    state: AgentState | None = None
    step_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=lambda: datetime.now(tz=timezone.utc).timestamp())
