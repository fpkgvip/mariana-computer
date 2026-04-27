"""
Mariana Computer — core Pydantic v2 data models.

All domain entities, enumerations, and AI-output schemas live here.
Every other module imports exclusively from this file for type safety.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic import ConfigDict

# ---------------------------------------------------------------------------
# Shared model config
# ---------------------------------------------------------------------------

_COMMON_CONFIG = ConfigDict(from_attributes=True)


# ===========================================================================
# Enumerations
# ===========================================================================


from enum import Enum


class TaskStatus(str, Enum):
    """Life-cycle status of a top-level ResearchTask."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    HALTED = "HALTED"


class HypothesisStatus(str, Enum):
    """Life-cycle status of an individual Hypothesis node."""

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    KILLED = "KILLED"
    EXHAUSTED = "EXHAUSTED"
    CONFIRMED = "CONFIRMED"


class BranchStatus(str, Enum):
    """Operational status of a research Branch."""

    ACTIVE = "ACTIVE"
    KILLED = "KILLED"
    EXHAUSTED = "EXHAUSTED"
    COMPLETED = "COMPLETED"


class EvidenceType(str, Enum):
    """Relationship between a piece of evidence and its hypothesis."""

    FOR = "FOR"
    AGAINST = "AGAINST"
    NEUTRAL = "NEUTRAL"


class SourceType(str, Enum):
    """Provenance category of an information source."""

    GOVERNMENT = "GOVERNMENT"
    EXCHANGE = "EXCHANGE"
    NEWS = "NEWS"
    FILING = "FILING"
    UNOFFICIAL_API = "UNOFFICIAL_API"
    ANALYST_REPORT = "ANALYST_REPORT"


class TaskType(str, Enum):
    """Logical type of an AI sub-task dispatched by the orchestrator."""

    RESEARCH_ARCHITECTURE = "RESEARCH_ARCHITECTURE"
    HYPOTHESIS_GENERATION = "HYPOTHESIS_GENERATION"
    EVIDENCE_EXTRACTION = "EVIDENCE_EXTRACTION"
    EVALUATION = "EVALUATION"
    TRANSLATION = "TRANSLATION"
    SUMMARIZATION = "SUMMARIZATION"
    TRIBUNAL_PLAINTIFF = "TRIBUNAL_PLAINTIFF"
    TRIBUNAL_DEFENDANT = "TRIBUNAL_DEFENDANT"
    TRIBUNAL_REBUTTAL = "TRIBUNAL_REBUTTAL"
    TRIBUNAL_COUNTER = "TRIBUNAL_COUNTER"
    TRIBUNAL_JUDGE = "TRIBUNAL_JUDGE"
    SKEPTIC_QUESTIONS = "SKEPTIC_QUESTIONS"
    REPORT_DRAFT = "REPORT_DRAFT"
    REPORT_FINAL_EDIT = "REPORT_FINAL_EDIT"
    WATCHDOG = "WATCHDOG"
    COMPRESSION = "COMPRESSION"
    # Intelligence Engine task types
    CLAIM_EXTRACTION = "CLAIM_EXTRACTION"
    SOURCE_CREDIBILITY = "SOURCE_CREDIBILITY"
    CONTRADICTION_DETECTION = "CONTRADICTION_DETECTION"
    REPLAN = "REPLAN"
    BAYESIAN_UPDATE = "BAYESIAN_UPDATE"
    GAP_DETECTION = "GAP_DETECTION"
    PERSPECTIVE_SYNTHESIS = "PERSPECTIVE_SYNTHESIS"
    META_SYNTHESIS = "META_SYNTHESIS"
    RETRIEVAL_STRATEGY = "RETRIEVAL_STRATEGY"
    REASONING_AUDIT = "REASONING_AUDIT"
    EXECUTIVE_SUMMARY = "EXECUTIVE_SUMMARY"
    FAST_PATH = "FAST_PATH"


class ModelID(str, Enum):
    """Canonical identifiers for supported LLM back-ends."""

    OPUS_47 = "claude-opus-4-7"
    OPUS_46 = "claude-opus-4-6"
    SONNET_46 = "claude-sonnet-4-6"
    HAIKU_45 = "claude-haiku-4-5"
    GPT4O_MINI = "gpt-4o-mini"
    DEEPSEEK_CHAT = "deepseek-v3.2"
    DEEPSEEK_REASONER = "deepseek-r1-0528"
    GEMINI_31_PRO = "gemini-3.1-pro-preview"
    GPT5 = "gpt-5"
    GPT54_MINI = "gpt-5.4-mini"


class QualityTier(str, Enum):
    """User-selected model quality tier."""

    MAXIMUM = "maximum"    # Opus for everything
    HIGH = "high"          # Gemini 3.1 Pro + Opus for critical
    BALANCED = "balanced"  # Sonnet + DeepSeek blend (default)
    ECONOMY = "economy"    # DeepSeek + GPT-4o-mini for everything


class TribunalVerdict(str, Enum):
    """Outcome of a tribunal adversarial review session."""

    CONFIRMED = "CONFIRMED"
    WEAKENED = "WEAKENED"
    DESTROYED = "DESTROYED"


class QuestionClassification(str, Enum):
    """Skeptic classification for whether a question has been resolved."""

    RESOLVED = "RESOLVED"
    RESEARCHABLE = "RESEARCHABLE"
    OPEN = "OPEN"


class QuestionSeverity(str, Enum):
    """Importance weight assigned to a skeptic question."""

    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"


class QuestionCategory(str, Enum):
    """Thematic category for a skeptic question."""

    DATA_PROVENANCE = "DATA_PROVENANCE"
    ALTERNATIVE_EXPLANATION = "ALTERNATIVE_EXPLANATION"
    METHODOLOGY = "METHODOLOGY"
    LEGAL_EXPOSURE = "LEGAL_EXPOSURE"
    TEMPORAL_VALIDITY = "TEMPORAL_VALIDITY"


class DiminishingRecommendation(str, Enum):
    """Orchestrator recommendation produced by the diminishing-returns check."""

    CONTINUE = "CONTINUE"
    SEARCH_DIFFERENT_SOURCES = "SEARCH_DIFFERENT_SOURCES"
    PIVOT = "PIVOT"
    HALT = "HALT"


class State(str, Enum):
    """State-machine states for the top-level research orchestrator."""

    INIT = "INIT"
    SEARCH = "SEARCH"
    EVALUATE = "EVALUATE"
    DEEPEN = "DEEPEN"
    PIVOT = "PIVOT"
    TRIBUNAL = "TRIBUNAL"
    SKEPTIC = "SKEPTIC"
    CHECKPOINT = "CHECKPOINT"
    REPORT = "REPORT"
    HALT = "HALT"


# ===========================================================================
# Core Entity Models
# ===========================================================================


class ResearchTask(BaseModel):
    """Top-level research job submitted by the user."""

    model_config = _COMMON_CONFIG

    id: str = Field(..., min_length=1, description="UUID for the task")
    topic: str = Field(..., min_length=3, max_length=1024, description="Research topic / question")
    budget_usd: float = Field(..., gt=0.0, description="Maximum allowed spend in USD")
    status: TaskStatus = Field(default=TaskStatus.PENDING, description="Current life-cycle status")
    current_state: State = Field(default=State.INIT, description="Current state-machine state")
    total_spent_usd: float = Field(default=0.0, ge=0.0, description="Cumulative USD spent so far")
    diminishing_flags: int = Field(
        default=0,
        ge=0,
        description="Count of consecutive diminishing-returns signals",
    )
    ai_call_counter: int = Field(default=0, ge=0, description="Total AI API calls made")
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)
    error_message: str | None = Field(default=None, description="Last error if status==FAILED")
    output_pdf_path: str | None = Field(default=None, description="Path to generated PDF report")
    output_docx_path: str | None = Field(default=None, description="Path to generated DOCX report")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Arbitrary extension fields")
    # F-05 fix: relational owner FK column.  Nullable for backward-compat with
    # rows created before this column existed; all new rows must supply this.
    user_id: str | None = Field(default=None, description="Owner UUID (FK to auth.users)")


class Hypothesis(BaseModel):
    """A single falsifiable hypothesis being researched within a task."""

    model_config = _COMMON_CONFIG

    id: str = Field(..., min_length=1, description="UUID for the hypothesis")
    task_id: str = Field(..., min_length=1, description="Parent ResearchTask ID")
    parent_id: str | None = Field(
        default=None,
        description="Parent hypothesis ID (None for root hypotheses)",
    )
    depth: int = Field(default=0, ge=0, description="Tree depth; 0 = root")
    statement: str = Field(
        ...,
        min_length=10,
        max_length=2048,
        description="Hypothesis statement in English",
    )
    statement_zh: str | None = Field(
        default=None,
        max_length=2048,
        description="Hypothesis statement in Chinese (if translated)",
    )
    status: HypothesisStatus = Field(default=HypothesisStatus.PENDING)
    score: float | None = Field(
        default=None,
        ge=0.0,
        le=10.0,
        description="Latest evaluation score [0, 10]",
    )
    momentum_note: str | None = Field(
        default=None,
        max_length=512,
        description="Short qualitative note about score momentum",
    )
    rationale: str | None = Field(
        default=None,
        max_length=4096,
        description="Why this hypothesis was generated",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))


class Finding(BaseModel):
    """A discrete piece of evidence attached to a hypothesis."""

    model_config = _COMMON_CONFIG

    id: str = Field(..., min_length=1, description="UUID for the finding")
    task_id: str = Field(..., min_length=1)
    hypothesis_id: str = Field(..., min_length=1, description="Hypothesis this finding supports/refutes")
    content: str = Field(..., min_length=1, description="Finding content in its original language")
    content_en: str | None = Field(default=None, description="English translation of content")
    content_language: str = Field(
        default="en",
        min_length=2,
        max_length=10,
        description="BCP-47 language tag of `content`",
    )
    source_ids: list[str] = Field(
        default_factory=list,
        description="IDs of Source records that back this finding",
    )
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Confidence level for this finding",
    )
    evidence_type: EvidenceType = Field(default=EvidenceType.NEUTRAL)
    is_compressed: bool = Field(
        default=False,
        description="True once the raw content has been summarised and purged",
    )
    raw_content_path: str | None = Field(
        default=None,
        description="Filesystem path to raw content (pre-compression)",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class Source(BaseModel):
    """A web page, document, or API response used as evidence."""

    model_config = _COMMON_CONFIG

    id: str = Field(..., min_length=1, description="UUID for the source")
    task_id: str = Field(..., min_length=1)
    url: str = Field(..., min_length=7, max_length=2048, description="Canonical URL")
    url_hash: str = Field(
        default="",
        description="SHA-256 hex digest of `url`; auto-computed if empty",
    )
    title: str | None = Field(default=None, max_length=512, description="Page title in original language")
    title_en: str | None = Field(default=None, max_length=512, description="Translated English title")
    content_hash: str | None = Field(
        default=None,
        description="SHA-256 of fetched content body (for change detection)",
    )
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    cache_expiry: datetime | None = Field(default=None, description="When the cached copy expires")
    source_type: SourceType = Field(default=SourceType.NEWS)
    language: str = Field(default="en", min_length=2, max_length=10)
    adapter_name: str | None = Field(
        default=None,
        max_length=128,
        description="Name of the adapter/spider that fetched this source",
    )
    is_paywalled: bool = Field(default=False)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def compute_url_hash(self) -> Source:
        """Auto-compute SHA-256 of the URL when url_hash is blank."""
        if not self.url_hash and self.url:
            self.url_hash = hashlib.sha256(self.url.encode("utf-8")).hexdigest()
        return self


class AISession(BaseModel):
    """Metadata record for a single call to an AI back-end."""

    model_config = _COMMON_CONFIG

    id: str = Field(..., min_length=1, description="UUID for the AI session")
    task_id: str = Field(..., min_length=1)
    branch_id: str | None = Field(default=None, description="Branch that triggered this call")
    task_type: TaskType = Field(..., description="Logical role of the AI call")
    model_used: ModelID = Field(..., description="Which model was invoked")
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_creation_tokens: int = Field(
        default=0,
        ge=0,
        description="Tokens written to the prompt cache",
    )
    cache_read_tokens: int = Field(
        default=0,
        ge=0,
        description="Tokens served from the prompt cache",
    )
    cost_usd: float = Field(default=0.0, ge=0.0, description="Total cost for this call in USD")
    duration_ms: int = Field(default=0, ge=0, description="Wall-clock latency in milliseconds")
    used_batch_api: bool = Field(default=False, description="Whether the Batch API was used")
    batch_id: str | None = Field(default=None, description="Batch job ID if used_batch_api=True")
    cache_hit: bool = Field(default=False, description="Whether response came from prompt cache")
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    error: str | None = Field(default=None, description="Error message if the call failed")


class Branch(BaseModel):
    """An independent exploration thread attached to a hypothesis."""

    model_config = _COMMON_CONFIG

    id: str = Field(..., min_length=1, description="UUID for the branch")
    hypothesis_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    status: BranchStatus = Field(default=BranchStatus.ACTIVE)
    score_history: list[float] = Field(
        default_factory=list,
        description="Ordered history of evaluation scores for trend analysis",
    )
    budget_allocated: float = Field(
        default=5.0,
        ge=0.0,
        description="USD budget allocated to this branch",
    )
    budget_spent: float = Field(default=0.0, ge=0.0)
    grants_log: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Log of budget grant events [{reason, amount, timestamp}]",
    )
    cycles_completed: int = Field(default=0, ge=0, description="How many search/evaluate cycles ran")
    kill_reason: str | None = Field(
        default=None,
        max_length=512,
        description="Human-readable reason for termination",
    )
    sources_searched: list[str] = Field(
        default_factory=list,
        description="URL hashes already queried to prevent re-fetching",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @property
    def budget_remaining(self) -> float:
        """Remaining budget in USD."""
        return max(0.0, self.budget_allocated - self.budget_spent)

    @property
    def latest_score(self) -> float | None:
        """Most recent evaluation score, or None if no scores exist."""
        return self.score_history[-1] if self.score_history else None


class Checkpoint(BaseModel):
    """Serialised snapshot of the orchestrator state for recovery."""

    model_config = _COMMON_CONFIG

    id: str = Field(..., min_length=1, description="UUID for the checkpoint")
    task_id: str = Field(..., min_length=1)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    state_machine_state: State = Field(..., description="State at time of snapshot")
    active_branch_ids: list[str] = Field(default_factory=list)
    killed_branch_ids: list[str] = Field(default_factory=list)
    compressed_findings: list[str] = Field(
        default_factory=list,
        description="IDs of findings that have been compressed",
    )
    budget_remaining: float = Field(ge=0.0, description="Remaining budget at checkpoint time")
    total_spent: float = Field(ge=0.0, description="Total spend at checkpoint time")
    diminishing_flags: int = Field(default=0, ge=0)
    ai_call_counter: int = Field(default=0, ge=0)
    snapshot_path: str | None = Field(
        default=None,
        description="Path to the full serialised state blob on disk",
    )
    diminishing_result: DiminishingRecommendation | None = Field(
        default=None,
        description="Latest diminishing-returns recommendation",
    )


class TribunalSession(BaseModel):
    """Record of a full adversarial tribunal review for a finding."""

    model_config = _COMMON_CONFIG

    id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    finding_id: str = Field(..., min_length=1, description="Finding under review")
    plaintiff_args: str | None = Field(default=None, description="Opening plaintiff argument text")
    defendant_args: str | None = Field(default=None, description="Opening defendant argument text")
    plaintiff_rebuttal: str | None = Field(default=None)
    defendant_counter: str | None = Field(default=None)
    verdict: TribunalVerdict | None = Field(default=None)
    judge_plaintiff_score: float | None = Field(default=None, ge=0.0, le=10.0)
    judge_defendant_score: float | None = Field(default=None, ge=0.0, le=10.0)
    judge_reasoning: str | None = Field(default=None, max_length=4096)
    unanswered_questions: list[str] = Field(
        default_factory=list,
        description="Questions the tribunal could not resolve",
    )
    total_cost_usd: float = Field(default=0.0, ge=0.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))


class SkepticQuestion(BaseModel):
    """A single challenging question raised by the Skeptic agent."""

    model_config = _COMMON_CONFIG

    number: int = Field(..., ge=1, description="1-based index within a SkepticResult")
    question: str = Field(..., min_length=10, max_length=1024)
    category: QuestionCategory = Field(...)
    severity: QuestionSeverity = Field(...)
    classification: QuestionClassification = Field(...)
    resolution_note: str | None = Field(
        default=None,
        max_length=1024,
        description="How the question was resolved, if at all",
    )


class SkepticResult(BaseModel):
    """Aggregated output from a Skeptic review session."""

    model_config = _COMMON_CONFIG

    id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    finding_id: str = Field(..., min_length=1)
    tribunal_session_id: str | None = Field(
        default=None,
        description="Associated tribunal session, if any",
    )
    questions: list[SkepticQuestion] = Field(default_factory=list)
    # Computed counts — populated by model_validator
    open_count: int = Field(default=0, ge=0)
    researchable_count: int = Field(default=0, ge=0)
    resolved_count: int = Field(default=0, ge=0)
    critical_open_count: int = Field(default=0, ge=0)
    passes_publishing_threshold: bool = Field(
        default=False,
        description="True when no CRITICAL open questions remain",
    )
    # BUG-029: Make max_open_questions a proper field instead of using getattr
    max_open_questions: int = Field(
        default=2,
        description="Maximum open questions allowed to pass publishing threshold",
        exclude=True,  # Not persisted to DB
    )
    cost_usd: float = Field(default=0.0, ge=0.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @model_validator(mode="after")
    def compute_question_counts(self) -> SkepticResult:
        """Recompute all aggregated question metrics from the questions list."""
        open_qs = [q for q in self.questions if q.classification == QuestionClassification.OPEN]
        self.open_count = len(open_qs)
        self.researchable_count = sum(
            1 for q in self.questions if q.classification == QuestionClassification.RESEARCHABLE
        )
        self.resolved_count = sum(
            1 for q in self.questions if q.classification == QuestionClassification.RESOLVED
        )
        self.critical_open_count = sum(
            1 for q in open_qs if q.severity == QuestionSeverity.CRITICAL
        )
        # BUG-029: Use the proper field instead of getattr fallback
        self.passes_publishing_threshold = (
            self.critical_open_count == 0
            and self.open_count <= self.max_open_questions
        )
        return self


class CostTracker(BaseModel):
    """Serialisable snapshot of a running cost ledger for a research task.

    The live mutable version is ``mariana.orchestrator.cost_tracker.CostTracker``.
    This Pydantic model is only used for checkpoint serialisation.
    """

    model_config = _COMMON_CONFIG

    task_id: str = Field(..., min_length=1)
    total_spent: float = Field(default=0.0, ge=0.0, description="Total USD spent")
    task_budget: float = Field(..., gt=0.0, description="Budget ceiling in USD")
    per_model_breakdown: dict[str, float] = Field(
        default_factory=dict,
        description="USD spent per ModelID value",
    )
    per_branch_breakdown: dict[str, float] = Field(
        default_factory=dict,
        description="USD spent per branch_id",
    )
    call_count: int = Field(default=0, ge=0, description="Total AI API calls recorded")

    @property
    def budget_remaining(self) -> float:
        """Remaining budget in USD (never negative)."""
        return max(0.0, self.task_budget - self.total_spent)


# ===========================================================================
# AI Output Schemas
# ===========================================================================


class ResearchArchitectureHypothesis(BaseModel):
    """A hypothesis proposed during the architecture phase."""

    model_config = _COMMON_CONFIG

    statement: str = Field(..., min_length=10, max_length=2048, description="Testable hypothesis")
    test_strategy: str = Field(
        ..., min_length=10, max_length=2048,
        description="How to test this hypothesis — specific data sources, metrics, comparisons",
    )
    expected_outcome: str = Field(
        ..., min_length=5, max_length=1024,
        description="What finding would confirm or refute this hypothesis",
    )
    priority: int = Field(..., ge=1, le=10, description="Priority (1=lowest, 10=highest)")


class ResearchArchitectureOutput(BaseModel):
    """Structured output from the ResearchArchitecture planning phase.

    This is the first AI call in an investigation. It produces a detailed
    research plan BEFORE any search begins, ensuring the investigation is
    focused and cost-effective.
    """

    model_config = _COMMON_CONFIG

    topic_analysis: str = Field(
        ..., min_length=50, max_length=4096,
        description="Deep analysis of the research topic: key entities, relationships, regulatory context",
    )
    research_plan: str = Field(
        ..., min_length=50, max_length=4096,
        description="Step-by-step research plan with specific data sources and expected timeline",
    )
    hypotheses: list[ResearchArchitectureHypothesis] = Field(
        ..., min_length=1,
        description="Specific, actionable hypotheses to test during the investigation",
    )
    data_sources: list[str] = Field(
        ..., min_length=1,
        description="Prioritised list of data sources to check (Tier 1 first, then Tier 2)",
    )
    risk_factors: list[str] = Field(
        default_factory=list,
        description="Known risks or challenges for this investigation",
    )
    estimated_complexity: str = Field(
        ..., description="low / medium / high — affects branch count and search depth",
    )


class GeneratedHypothesis(BaseModel):
    """A single hypothesis produced by the HypothesisGeneration agent."""

    model_config = _COMMON_CONFIG

    statement: str = Field(..., min_length=10, max_length=2048)
    statement_zh: str | None = Field(default=None, max_length=2048, description="Chinese translation")
    rationale: str = Field(..., min_length=10, max_length=4096)
    priority: int = Field(..., ge=1, le=10, description="Suggested research priority (1=lowest, 10=highest)")
    suggested_sources: list[str] = Field(
        default_factory=list,
        description="Adapter names or URL patterns recommended for this hypothesis",
    )


class HypothesisGenerationOutput(BaseModel):
    """Full structured output from the HypothesisGeneration task."""

    model_config = _COMMON_CONFIG

    hypotheses: list[GeneratedHypothesis] = Field(..., min_length=1)
    overall_research_angle: str = Field(
        ...,
        min_length=10,
        max_length=2048,
        description="High-level strategic framing for the research",
    )
    recommended_starting_hypotheses: list[int] = Field(
        ...,
        description="0-based indices into `hypotheses` to pursue first",
    )


class FastPathOutput(BaseModel):
    """Lightweight output for instant/quick tier fast-path responses.

    Used instead of HypothesisGenerationOutput when the orchestrator
    takes the fast path (instant / quick tiers).
    """

    model_config = _COMMON_CONFIG

    answer: str = Field(
        ...,
        min_length=1,
        max_length=16384,
        description="The direct answer to the user's question or request",
    )
    sources: list[str] = Field(
        default_factory=list,
        description="Optional list of source URLs or references cited in the answer",
    )
    confidence: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Confidence in the answer (0-1)",
    )


class EvidenceItem(BaseModel):
    """A single piece of evidence extracted from a source page."""

    model_config = _COMMON_CONFIG

    content: str = Field(..., min_length=1, max_length=8192, description="Extracted evidence text")
    evidence_type: EvidenceType = Field(...)
    confidence: float = Field(..., ge=0.0, le=1.0)
    quote: str | None = Field(
        default=None,
        max_length=2048,
        description="Verbatim quote from the source that supports this evidence item",
    )
    data_point: str | None = Field(
        default=None,
        max_length=512,
        description="Key numeric or factual data point (e.g. '营收同比+23%')",
    )
    relevance_explanation: str = Field(
        ...,
        min_length=5,
        max_length=1024,
        description="Why this item is relevant to the hypothesis",
    )


class EvidenceExtractionOutput(BaseModel):
    """Structured output from the EvidenceExtraction task."""

    model_config = _COMMON_CONFIG

    hypothesis_addressed: str = Field(..., min_length=1, description="Hypothesis statement being evaluated")
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    page_summary: str = Field(..., min_length=1, max_length=16384, description="Short summary of the page")
    page_relevance_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="How relevant this page is to the hypothesis [0, 1]",
    )
    red_flags: list[str] = Field(
        default_factory=list,
        description="Potential data quality or reliability concerns",
    )
    language_detected: str = Field(
        default="en",
        min_length=2,
        max_length=10,
        description="BCP-47 language tag of the source page",
    )


class EvaluationOutput(BaseModel):
    """Structured output from the Evaluation task."""

    model_config = _COMMON_CONFIG

    # BUG-010/BUG-030: Score is on 0–1 scale; thresholds in event_loop.py and
    # branch_manager.py have been updated to match (0.7 high, 0.4 medium/kill).
    score: float = Field(..., ge=0.0, le=1.0, description="Overall hypothesis score [0, 1]")
    score_rationale: str = Field(..., min_length=10, max_length=4096)
    momentum_note: str = Field(
        ...,
        min_length=5,
        max_length=512,
        description="Brief qualitative note on score trajectory",
    )
    recommendation: Literal["DEEPEN", "SEARCH_MORE", "KILL", "PLATEAU"] = Field(
        ...,
        description="Orchestrator instruction based on this evaluation",
    )
    evidence_gaps: list[str] = Field(
        default_factory=list,
        description="What evidence is missing to resolve this hypothesis",
    )
    next_search_keywords: list[str] = Field(
        default_factory=list,
        description="Suggested keywords for the next search cycle",
    )
    next_source_adapters: list[str] = Field(
        default_factory=list,
        description="Adapters recommended for the next cycle",
    )


class TranslationOutput(BaseModel):
    """Structured output from the Translation task."""

    model_config = _COMMON_CONFIG

    original_text: str = Field(..., min_length=1)
    translated_text: str = Field(..., min_length=1)
    source_language: str = Field(..., min_length=2, max_length=10)
    target_language: str = Field(..., min_length=2, max_length=10)
    confidence: float = Field(..., ge=0.0, le=1.0)
    ambiguities: list[str] = Field(
        default_factory=list,
        description="Phrases that were ambiguous or had multiple valid translations",
    )
    financial_terms_glossary: dict[str, str] = Field(
        default_factory=dict,
        description="Map of source-language financial terms to target-language equivalents",
    )


class TribunalArgumentOutput(BaseModel):
    """Output from one side's argument in a tribunal session."""

    model_config = _COMMON_CONFIG

    role: Literal["PLAINTIFF", "DEFENDANT", "PLAINTIFF_REBUTTAL", "DEFENDANT_COUNTER"] = Field(...)
    argument_summary: str = Field(..., min_length=10, max_length=4096)
    key_points: list[str] = Field(..., min_length=1, description="Bullet-point key arguments")
    cited_evidence: list[str] = Field(
        default_factory=list,
        description="Finding IDs or direct quotes cited in this argument",
    )
    weaknesses_acknowledged: list[str] = Field(
        default_factory=list,
        description="Weaknesses in this side's position openly acknowledged",
    )
    strongest_counterargument_rebuttal: str | None = Field(
        default=None,
        max_length=2048,
        description="This side's rebuttal to the strongest opposing argument",
    )
    confidence: float = Field(..., ge=0.0, le=1.0)


class TribunalVerdictOutput(BaseModel):
    """Judge's verdict output from a tribunal session."""

    model_config = _COMMON_CONFIG

    plaintiff_score: float = Field(..., ge=0.0, le=10.0)
    defendant_score: float = Field(..., ge=0.0, le=10.0)
    verdict: TribunalVerdict = Field(...)
    verdict_reasoning: str = Field(..., min_length=10, max_length=4096)
    unanswered_questions: list[str] = Field(default_factory=list)
    finding_confidence_after_tribunal: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Updated confidence in the finding after the tribunal",
    )
    publication_risk_assessment: str = Field(
        ...,
        min_length=5,
        max_length=2048,
        description="Assessment of legal, factual, and reputational risks if published",
    )


class SkepticQuestionsOutput(BaseModel):
    """Full structured output from the Skeptic Questions task."""

    model_config = _COMMON_CONFIG

    questions: list[SkepticQuestion] = Field(..., min_length=1)
    overall_skeptic_assessment: str = Field(..., min_length=10, max_length=4096)
    hardest_question_index: int = Field(
        ...,
        ge=0,
        description="0-based index of the question the AI considers hardest to answer",
    )


class ReportSection(BaseModel):
    """A single section of the research report."""

    model_config = _COMMON_CONFIG

    section_id: str = Field(..., min_length=1)
    title_en: str = Field(..., min_length=1, max_length=256)
    title_zh: str = Field(..., min_length=1, max_length=256)
    content_en: str = Field(..., min_length=1)
    content_zh: str = Field(..., min_length=1)
    charts_needed: list[str] = Field(
        default_factory=list,
        description="Descriptions of charts that should accompany this section",
    )
    citations: list[str] = Field(
        default_factory=list,
        description="Source IDs or URLs cited in this section",
    )
    word_count_en: int = Field(default=0, ge=0, description="Word count of `content_en`")


class ReportDraftOutput(BaseModel):
    """Full structured draft report produced by the ReportDraft task."""

    model_config = _COMMON_CONFIG

    title_en: str = Field(..., min_length=1, max_length=512)
    title_zh: str = Field(..., min_length=1, max_length=512)
    executive_summary_en: str = Field(..., min_length=50, max_length=32768)
    executive_summary_zh: str = Field(..., min_length=50, max_length=32768)
    sections: list[ReportSection] = Field(..., min_length=1)
    conclusion_en: str = Field(..., min_length=50, max_length=16384)
    conclusion_zh: str = Field(..., min_length=50, max_length=16384)
    disclaimer_en: str = Field(..., min_length=10, max_length=4096)
    disclaimer_zh: str = Field(..., min_length=10, max_length=4096)


class WatchdogOutput(BaseModel):
    """Output from the Watchdog circular-reasoning detector."""

    model_config = _COMMON_CONFIG

    is_circular: bool = Field(..., description="True if circular reasoning was detected")
    circular_pattern_description: str | None = Field(
        default=None,
        max_length=2048,
        description="Description of the circular pattern if detected",
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="Finding IDs or snippets that constitute the circular chain",
    )
    recommendation: Literal["CONTINUE", "FORCE_PIVOT", "KILL_BRANCH", "ALERT"] = Field(...)
    confidence: float = Field(..., ge=0.0, le=1.0)


class CompressedFindings(BaseModel):
    """Distilled summary of findings for a hypothesis (post-compression)."""

    model_config = _COMMON_CONFIG

    hypothesis_id: str = Field(..., min_length=1)
    hypothesis_statement: str = Field(..., min_length=1, max_length=2048)
    evidence_for: list[str] = Field(default_factory=list, description="Key supporting points")
    evidence_against: list[str] = Field(default_factory=list, description="Key counter-points")
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    key_sources: list[str] = Field(default_factory=list, description="Most important source URLs")
    momentum_note: str | None = Field(default=None, max_length=512)
    compressed_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    raw_finding_count: int = Field(..., ge=0, description="Number of raw findings replaced by this summary")


class SummarizationOutput(BaseModel):
    """Structured output from the Summarization / compression task."""

    model_config = _COMMON_CONFIG

    compressed: CompressedFindings = Field(..., description="The compressed findings object")
    purged_finding_ids: list[str] = Field(
        ...,
        description="IDs of raw Finding records that were purged after compression",
    )
    compression_ratio: float = Field(
        ...,
        ge=0.0,
        description="Ratio of raw token count to compressed token count",
    )
