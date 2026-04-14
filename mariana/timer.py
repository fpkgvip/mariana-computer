"""
mariana/timer.py

Research timer system for the Mariana research engine.

Provides :class:`ResearchTimer` for tracking elapsed time and research phases,
and :class:`TimerAwarePrompt` for injecting phase-appropriate guidance into
every AI call.

The timer divides a research budget into six phases, each with distinct
behavioural guidance:

    0–15 %   Exploration     — broad research, identify key areas
   15–40 %   Deep Analysis   — deep dive into identified areas
   40–65 %   Synthesis       — connect findings, build thesis
   65–80 %   Verification    — cross-check, adversarial review
   80–90 %   Report Writing  — compile final report
   90–100 %  Finalisation    — polish, final checks

Usage::

    from mariana.timer import ResearchTimer, TimerAwarePrompt

    timer = ResearchTimer(task_id="abc", duration_hours=12.0)
    prompt_helper = TimerAwarePrompt(timer)

    # In every AI call:
    time_context = prompt_helper.get_prompt_injection()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Phase definitions
# ---------------------------------------------------------------------------

ResearchPhase = Literal[
    "exploration",
    "deep_analysis",
    "synthesis",
    "verification",
    "report_writing",
    "finalization",
]

_PHASE_BOUNDARIES: list[tuple[float, float, ResearchPhase]] = [
    (0.00, 0.15, "exploration"),
    (0.15, 0.40, "deep_analysis"),
    (0.40, 0.65, "synthesis"),
    (0.65, 0.80, "verification"),
    (0.80, 0.90, "report_writing"),
    (0.90, 1.00, "finalization"),
]

_PHASE_LABELS: dict[ResearchPhase, str] = {
    "exploration": "EXPLORATION — Broad research, identify key areas",
    "deep_analysis": "DEEP ANALYSIS — Deep dive into identified areas",
    "synthesis": "SYNTHESIS — Connect findings and build thesis",
    "verification": "VERIFICATION — Cross-check, adversarial review",
    "report_writing": "REPORT WRITING — Compile final report",
    "finalization": "FINALIZATION — Polish, final checks",
}


# ---------------------------------------------------------------------------
# ResearchTimer
# ---------------------------------------------------------------------------


@dataclass
class ResearchTimer:
    """Tracks elapsed time and research phase for a timed research session.

    The timer is initialised with a :attr:`duration_hours` budget and
    automatically records :attr:`started_at` as the creation time (UTC)
    unless explicitly overridden.

    Attributes:
        task_id: Unique identifier of the research task.
        duration_hours: Total time budget in hours.
        started_at: UTC timestamp when the timer started.
        branches_completed: Number of research branches finished so far.
        branches_total: Total planned branches (updated during exploration).
        findings_count: Running count of discrete findings collected.
    """

    task_id: str
    duration_hours: float
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    branches_completed: int = 0
    branches_total: int = 0
    findings_count: int = 0

    def __post_init__(self) -> None:
        if self.duration_hours <= 0:
            raise ValueError(
                f"duration_hours must be positive, got {self.duration_hours}"
            )
        # Ensure timezone-aware.
        if self.started_at.tzinfo is None:
            self.started_at = self.started_at.replace(tzinfo=timezone.utc)
        logger.info(
            "timer.started",
            task_id=self.task_id,
            duration_hours=self.duration_hours,
            started_at=self.started_at.isoformat(),
        )

    # -- Core properties ----------------------------------------------------

    @property
    def deadline(self) -> datetime:
        """UTC datetime when the timer expires."""
        from datetime import timedelta

        return self.started_at + timedelta(hours=self.duration_hours)

    @property
    def elapsed_seconds(self) -> float:
        """Seconds elapsed since the timer started."""
        now = datetime.now(timezone.utc)
        return max(0.0, (now - self.started_at).total_seconds())

    @property
    def elapsed_hours(self) -> float:
        """Hours elapsed since the timer started."""
        return self.elapsed_seconds / 3600.0

    @property
    def remaining_seconds(self) -> float:
        """Seconds remaining until the deadline."""
        return max(0.0, self.duration_hours * 3600.0 - self.elapsed_seconds)

    @property
    def remaining_hours(self) -> float:
        """Hours remaining until the deadline."""
        return self.remaining_seconds / 3600.0

    @property
    def progress_pct(self) -> float:
        """Progress as a fraction from 0.0 to 1.0."""
        if self.duration_hours <= 0:
            return 1.0
        return min(1.0, self.elapsed_hours / self.duration_hours)

    @property
    def phase(self) -> ResearchPhase:
        """Current research phase based on elapsed progress.

        Phases:
            exploration   (0–15 %)
            deep_analysis (15–40 %)
            synthesis     (40–65 %)
            verification  (65–80 %)
            report_writing (80–90 %)
            finalization  (90–100 %)
        """
        pct = self.progress_pct
        for low, high, phase_name in _PHASE_BOUNDARIES:
            if low <= pct < high:
                return phase_name
        return "finalization"

    # -- Status helpers -----------------------------------------------------

    def get_time_context(self) -> str:
        """Return a human-readable time context string for prompt injection.

        Example output::

            Research Timer: 4h 23m remaining of 12h budget (63.5% elapsed)
            Current phase: SYNTHESIS — Connect findings and build thesis
            Pace: On track | Branches completed: 8/12 | Findings: 23
        """
        remaining = self.remaining_hours
        remaining_h = int(remaining)
        remaining_m = int((remaining - remaining_h) * 60)

        total_h = self.duration_hours
        pct = self.progress_pct * 100.0

        phase_label = _PHASE_LABELS.get(self.phase, self.phase.upper())
        pace = self._compute_pace_label()

        branches_str = (
            f"Branches completed: {self.branches_completed}/{self.branches_total}"
            if self.branches_total > 0
            else f"Branches completed: {self.branches_completed}"
        )

        lines = [
            f"Research Timer: {remaining_h}h {remaining_m:02d}m remaining "
            f"of {total_h:.0f}h budget ({pct:.1f}% elapsed)",
            f"Current phase: {phase_label}",
            f"Pace: {pace} | {branches_str} | Findings: {self.findings_count}",
        ]
        return "\n".join(lines)

    def _compute_pace_label(self) -> str:
        """Determine whether research is ahead, on track, or behind schedule."""
        if self.branches_total <= 0:
            return "Planning"

        expected_pct = self.progress_pct
        actual_pct = self.branches_completed / self.branches_total

        if actual_pct >= expected_pct + 0.10:
            return "Ahead of schedule"
        elif actual_pct <= expected_pct - 0.15:
            return "Behind schedule"
        else:
            return "On track"

    def should_deepen(self) -> bool:
        """Return True if the research is ahead of schedule and there is
        time to explore additional branches or go deeper on existing ones.

        Conditions:
        - Progress is in exploration or deep_analysis phase.
        - Completed branch ratio exceeds elapsed time ratio by at least 10 pp.
        """
        if self.phase not in ("exploration", "deep_analysis"):
            return False
        if self.branches_total <= 0:
            return False
        branch_pct = self.branches_completed / self.branches_total
        return branch_pct >= self.progress_pct + 0.10

    def should_wrap_up(self) -> bool:
        """Return True if the timer is in the final 20 % and the research
        should transition to synthesis/report writing."""
        return self.progress_pct >= 0.80

    def is_expired(self) -> bool:
        """Return True if the timer has reached or passed the deadline."""
        return self.remaining_seconds <= 0

    # -- Mutation helpers ---------------------------------------------------

    def record_branch_complete(self) -> None:
        """Increment the completed branch counter."""
        self.branches_completed += 1
        logger.debug(
            "timer.branch_complete",
            task_id=self.task_id,
            branches_completed=self.branches_completed,
        )

    def record_findings(self, count: int = 1) -> None:
        """Add to the running findings counter."""
        self.findings_count += count
        logger.debug(
            "timer.findings_recorded",
            task_id=self.task_id,
            findings_count=self.findings_count,
        )

    def set_branches_total(self, total: int) -> None:
        """Update the total planned branches (set during exploration phase)."""
        self.branches_total = total
        logger.info(
            "timer.branches_planned",
            task_id=self.task_id,
            branches_total=total,
        )


# ---------------------------------------------------------------------------
# TimerAwarePrompt
# ---------------------------------------------------------------------------

# Phase-specific behavioural guidance injected into the AI prompt.
_PHASE_GUIDANCE: dict[ResearchPhase, str] = {
    "exploration": (
        "You have substantial time. Explore broadly, identify all relevant "
        "angles. Cast a wide net across diverse sources and data types. "
        "Generate as many promising hypotheses as possible. Do not commit "
        "to a single narrative yet — keep options open. Prioritise breadth "
        "over depth in this phase. Map the full landscape of information "
        "before drilling down."
    ),
    "deep_analysis": (
        "Focus on the most promising leads. Go deep on 3-5 key areas that "
        "emerged from exploration. Extract detailed evidence: exact figures, "
        "dates, quotes, and source citations. Build quantitative models where "
        "applicable. Cross-reference claims across multiple independent sources. "
        "This is where the core analytical work happens — thoroughness matters "
        "more than speed."
    ),
    "synthesis": (
        "Begin connecting your findings. What is the narrative? Build the "
        "overarching thesis that ties together the evidence from multiple "
        "branches. Identify causal relationships, correlations, and patterns. "
        "Resolve any contradictions between different evidence sources. "
        "Start forming your conclusions, but remain open to revision. "
        "Quantify the strength of your evidence base."
    ),
    "verification": (
        "Challenge your thesis. What could be wrong? What is the strongest "
        "counter-argument? Actively seek disconfirming evidence. Perform "
        "adversarial review on your key conclusions. Check for survivorship "
        "bias, confirmation bias, and selection bias in your research process. "
        "Verify every quantitative claim against primary sources. Identify "
        "remaining uncertainties and rate their severity."
    ),
    "report_writing": (
        "Compile your findings into a structured report with evidence. "
        "Organise by importance: executive summary first, then key findings "
        "with supporting evidence, methodology, limitations, and appendices. "
        "Every claim must have a source citation. Express confidence levels "
        "explicitly. The report must stand on its own — a reader who has "
        "not followed the research process should understand the findings "
        "completely."
    ),
    "finalization": (
        "Final review. Ensure all claims have citations. Polish the report "
        "for clarity, consistency, and professionalism. Check for factual "
        "errors, logical inconsistencies, and formatting issues. Verify that "
        "all numbers, dates, and names are accurate. Review the executive "
        "summary to ensure it accurately represents the full report. This is "
        "your last chance to catch errors before publication."
    ),
}


@dataclass
class TimerAwarePrompt:
    """Generates phase-appropriate prompt injections based on the current
    timer state.

    Wraps a :class:`ResearchTimer` and produces context strings that should
    be prepended to or embedded within every AI prompt during a timed
    research session.

    Usage::

        prompt_helper = TimerAwarePrompt(timer)
        injection = prompt_helper.get_prompt_injection()
        # Prepend `injection` to the dynamic context block of the AI prompt.
    """

    timer: ResearchTimer

    def get_prompt_injection(self) -> str:
        """Return the complete prompt injection: time context + phase guidance.

        The returned string is designed to be added to the system or user
        prompt so the AI model is aware of time constraints and adjusts its
        research strategy accordingly.
        """
        if self.timer.is_expired():
            return self._expired_injection()

        time_ctx = self.timer.get_time_context()
        phase = self.timer.phase
        guidance = _PHASE_GUIDANCE.get(phase, "")
        pace_note = self._pace_note()

        lines = [
            "═══════════════════════════════════════════════════════════════",
            "RESEARCH TIMER STATUS",
            "═══════════════════════════════════════════════════════════════",
            "",
            time_ctx,
            "",
            f"Phase guidance: {guidance}",
        ]

        if pace_note:
            lines.append(f"\nPace advisory: {pace_note}")

        if self.timer.should_wrap_up():
            lines.append(
                "\n*** TIME WARNING: You are in the final phase. Prioritise "
                "completing and polishing the report over starting new "
                "research branches. ***"
            )

        return "\n".join(lines)

    def get_phase_guidance(self) -> str:
        """Return only the behavioural guidance for the current phase."""
        return _PHASE_GUIDANCE.get(self.timer.phase, "")

    def _pace_note(self) -> str:
        """Generate a pace-specific advisory note."""
        timer = self.timer

        if timer.should_deepen():
            return (
                "You are ahead of schedule. Consider exploring additional "
                "angles or going deeper on existing branches to strengthen "
                "the analysis."
            )

        if timer.branches_total > 0:
            branch_pct = timer.branches_completed / timer.branches_total
            if branch_pct < timer.progress_pct - 0.15:
                remaining_branches = timer.branches_total - timer.branches_completed
                remaining_time = timer.remaining_hours
                if remaining_branches > 0 and remaining_time > 0:
                    time_per_branch = remaining_time / remaining_branches
                    return (
                        f"You are behind schedule. {remaining_branches} branches "
                        f"remain with {remaining_time:.1f}h left "
                        f"(~{time_per_branch:.1f}h per branch). Consider "
                        f"triaging lower-priority branches or reducing depth."
                    )
        return ""

    def _expired_injection(self) -> str:
        """Return the injection for an expired timer."""
        time_ctx = self.timer.get_time_context()
        return (
            "═══════════════════════════════════════════════════════════════\n"
            "RESEARCH TIMER — EXPIRED\n"
            "═══════════════════════════════════════════════════════════════\n"
            "\n"
            f"{time_ctx}\n"
            "\n"
            "*** TIME EXPIRED. Stop all new research immediately. If a "
            "report has not been completed, compile the best available "
            "findings into a summary now. Mark any incomplete areas as "
            "'analysis incomplete due to time constraint' with the work "
            "done so far. ***"
        )


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------


def create_timer(
    task_id: str,
    duration_hours: float,
    started_at: datetime | None = None,
) -> tuple[ResearchTimer, TimerAwarePrompt]:
    """Create a timer and its associated prompt helper in one call.

    Args:
        task_id: Unique identifier of the research task.
        duration_hours: Total time budget in hours.
        started_at: Optional override for the start time (UTC).
            Defaults to now.

    Returns:
        A tuple of ``(timer, prompt_helper)``.
    """
    kwargs: dict = {"task_id": task_id, "duration_hours": duration_hours}
    if started_at is not None:
        kwargs["started_at"] = started_at

    timer = ResearchTimer(**kwargs)
    prompt_helper = TimerAwarePrompt(timer=timer)

    logger.info(
        "timer.created",
        task_id=task_id,
        duration_hours=duration_hours,
        deadline=timer.deadline.isoformat(),
    )
    return timer, prompt_helper
