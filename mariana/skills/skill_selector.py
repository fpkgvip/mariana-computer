"""
mariana/skills/skill_selector.py

AI-powered skill selector for the Mariana research engine.

Given a user's research topic and an optional time budget, the selector:

1. Analyses the topic text to identify domains, entities, and intent.
2. Matches relevant skills from the registry.
3. Estimates time allocation per skill based on priority and the total budget.
4. Returns a :class:`SkillPlan` with ordered skill execution.

The selector operates synchronously (no LLM call) for speed — it uses
keyword and heuristic matching rather than an AI model.  This keeps the
skill-selection latency under 10 ms.

Usage::

    from mariana.skills.skill_selector import SkillSelector

    selector = SkillSelector()
    plan = selector.select(
        topic="Forensic analysis of Luckin Coffee SEC filings",
        duration_hours=8.0,
    )
    for step in plan.steps:
        print(step.skill_id, step.allocated_minutes, step.rationale)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

import structlog

from mariana.skills.registry import Skill, SkillRegistry, get_registry

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkillStep:
    """A single step in a skill execution plan.

    Attributes:
        skill_id: Identifier of the skill to activate.
        skill_name: Human-readable skill name.
        category: Skill category.
        allocated_minutes: Suggested time allocation in minutes.
        priority: Execution priority (higher = earlier).
        rationale: Explanation of why this skill was selected.
        tools: List of tool functions this step requires.
    """

    skill_id: str
    skill_name: str
    category: str
    allocated_minutes: int
    priority: int
    rationale: str
    tools: list[str] = field(default_factory=list)


@dataclass
class SkillPlan:
    """An ordered execution plan of skills for a research task.

    Attributes:
        topic: The original research topic.
        duration_hours: Total time budget in hours.
        steps: Ordered list of skill steps (execution order).
        total_allocated_minutes: Sum of all step allocations.
        unallocated_minutes: Remaining time not assigned to any skill.
        skill_ids: Convenience list of skill IDs in execution order.
    """

    topic: str
    duration_hours: float
    steps: list[SkillStep]
    total_allocated_minutes: int = 0
    unallocated_minutes: int = 0

    def __post_init__(self) -> None:
        self.total_allocated_minutes = sum(s.allocated_minutes for s in self.steps)
        total_budget_minutes = int(self.duration_hours * 60)
        self.unallocated_minutes = max(
            0, total_budget_minutes - self.total_allocated_minutes
        )

    @property
    def skill_ids(self) -> list[str]:
        """Skill IDs in execution order."""
        return [s.skill_id for s in self.steps]


# ---------------------------------------------------------------------------
# Topic analyser — lightweight keyword extraction
# ---------------------------------------------------------------------------

# Patterns that hint at specific research intents.
_INTENT_PATTERNS: list[tuple[str, list[str]]] = [
    # (regex pattern, list of skill IDs to boost)
    (r"\b(10-?k|10-?q|8-?k|def\s?14a|13f|sec\s+filing|edgar)\b",
     ["sec_filing_analysis"]),
    (r"\b(dcf|valuation|intrinsic\s+value|wacc|fair\s+value)\b",
     ["financial_modeling"]),
    (r"\b(earnings|eps|revenue\s+beat|guidance|quarterly\s+results)\b",
     ["earnings_analysis"]),
    (r"\b(options?\s+flow|unusual\s+activity|dark\s+pool|gamma)\b",
     ["options_flow_analysis"]),
    (r"\b(macro|gdp|inflation|yield\s+curve|fed|interest\s+rate)\b",
     ["macro_analysis"]),
    (r"\b(technical\s+analysis|chart\s+pattern|rsi|macd|support\s+resistance)\b",
     ["technical_analysis"]),
    (r"\b(risk|var|cvar|sharpe|drawdown|stress\s+test)\b",
     ["risk_assessment"]),
    (r"\b(governance|proxy|board|executive\s+comp|insider\s+trans)\b",
     ["corporate_governance"]),
    (r"\b(industry\s+analysis|porter|tam|market\s+share|competitive\s+landscape)\b",
     ["industry_analysis"]),
    (r"\b(credit|leverage|debt|covenant|bond\s+rating|interest\s+coverage)\b",
     ["credit_analysis"]),
    (r"\b(forensic|fraud|manipulation|beneish|channel\s+stuff|round\s*trip)\b",
     ["forensic_accounting"]),
    (r"\b(esg|sustainability|carbon|emissions|climate|diversity\s+metric)\b",
     ["esg_analysis"]),
    (r"\b(short\s+sell|hindenburg|muddy\s+waters|activist\s+short|osint)\b",
     ["activist_short_analysis"]),
    (r"\b(crypto|bitcoin|ethereum|defi|blockchain|token|on-?chain)\b",
     ["crypto_analysis"]),
    (r"\b(forex|fx|currency|carry\s+trade|exchange\s+rate)\b",
     ["fx_analysis"]),
    (r"\b(fixed\s+income|bond\s+pricing|duration|convexity|municipal)\b",
     ["fixed_income"]),
    (r"\b(quant|backtest|factor\s+model|stat\s*arb|momentum\s+strat)\b",
     ["quant_strategy"]),
    (r"\b(merger|m&a|acquisition|arb\s+spread|tender\s+offer)\b",
     ["merger_arbitrage"]),
    (r"\b(reit|real\s+estate|cap\s+rate|noi|ffo)\b",
     ["real_estate"]),
    (r"\b(commodity|oil|gold|natural\s+gas|futures\s+curve|contango)\b",
     ["commodities"]),
    # General skills
    (r"\b(research|investigate|deep\s+dive|multi-?source)\b",
     ["web_research"]),
    (r"\b(data\s+analysis|statistic|regression|correlat)\b",
     ["data_analysis"]),
    (r"\b(python|code|script|automat)\b",
     ["code_execution"]),
    (r"\b(report|document|pdf|powerpoint|excel\s+spread)\b",
     ["document_generation"]),
    (r"\b(monitor|real-?time|alert|track\s+price)\b",
     ["real_time_monitoring"]),
    (r"\b(chart|visuali[sz]|graph|dashboard|plot)\b",
     ["visualization"]),
    (r"\b(academic|paper|journal|literature\s+review|arxiv)\b",
     ["academic_research"]),
    (r"\b(competitive\s+intell|swot|competitor|benchmark)\b",
     ["competitive_intelligence"]),
    (r"\b(news|sentiment|media\s+monitor|headline)\b",
     ["news_analysis"]),
    (r"\b(regulat|fda|patent|compliance|filing\s+track)\b",
     ["regulatory_tracking"]),
]

_COMPILED_PATTERNS: list[tuple[re.Pattern[str], list[str]]] = [
    (re.compile(pattern, re.IGNORECASE), skill_ids)
    for pattern, skill_ids in _INTENT_PATTERNS
]


def _extract_intent_skills(topic: str) -> dict[str, int]:
    """Return a dict of skill_id → boost_score from regex intent matching.

    Multiple pattern hits for the same skill accumulate boost points.
    """
    boosts: dict[str, int] = {}
    for compiled, skill_ids in _COMPILED_PATTERNS:
        if compiled.search(topic):
            for sid in skill_ids:
                boosts[sid] = boosts.get(sid, 0) + 1
    return boosts


# ---------------------------------------------------------------------------
# SkillSelector
# ---------------------------------------------------------------------------


class SkillSelector:
    """Select and plan skill activation for a research topic.

    Combines intent-pattern matching with the registry's fuzzy topic
    matcher to produce a ranked, time-allocated skill plan.

    Args:
        registry: Optional explicit registry.  Defaults to the global
            singleton obtained via ``get_registry()``.
    """

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        self._registry = registry or get_registry()

    def select(
        self,
        topic: str,
        duration_hours: float = 4.0,
        max_skills: int = 10,
    ) -> SkillPlan:
        """Build an optimal skill plan for *topic*.

        Args:
            topic: The user's research topic or question.
            duration_hours: Total time budget in hours.
            max_skills: Maximum number of skills to include.

        Returns:
            A :class:`SkillPlan` with ordered, time-allocated steps.
        """
        total_minutes = int(duration_hours * 60)

        # Step 1: gather candidate skills from multiple signals.
        intent_boosts = _extract_intent_skills(topic)
        registry_matches = self._registry.get_skills_for_topic(topic)

        # Merge into a scored candidate list.
        scored: dict[str, float] = {}
        for skill in registry_matches:
            scored[skill.id] = skill.priority / 10.0  # Normalise to 0-1.

        for sid, boost in intent_boosts.items():
            scored[sid] = scored.get(sid, 0.0) + boost * 0.30

        # BUG-031 fix: always ensure web_research is present at a minimum score
        # of 0.20 — but only raise its score if it isn't already higher.
        # Previously the check was "not in scored" which meant a registry-matched
        # web_research with score 0.0 (unlikely) would not get the 0.20 floor.
        scored["web_research"] = max(scored.get("web_research", 0.0), 0.20)

        # Step 2: rank and cap.
        ranked_ids = sorted(scored, key=lambda sid: scored[sid], reverse=True)
        selected_ids = ranked_ids[:max_skills]

        # Step 3: resolve Skill objects.
        selected_skills: list[Skill] = []
        for sid in selected_ids:
            try:
                selected_skills.append(self._registry.get_skill(sid))
            except KeyError:
                logger.warning(
                    "skill_selector.missing_skill",
                    skill_id=sid,
                    msg="Intent pattern referenced a skill not in the registry",
                )

        if not selected_skills:
            logger.warning(
                "skill_selector.no_skills",
                topic=topic[:120],
                msg="No skills matched — returning minimal plan",
            )
            return SkillPlan(topic=topic, duration_hours=duration_hours, steps=[])

        # Step 4: allocate time proportionally to priority.
        steps = self._allocate_time(
            skills=selected_skills,
            total_minutes=total_minutes,
            intent_boosts=intent_boosts,
            topic=topic,
        )

        plan = SkillPlan(
            topic=topic,
            duration_hours=duration_hours,
            steps=steps,
        )

        logger.info(
            "skill_selector.plan_built",
            topic=topic[:120],
            duration_hours=duration_hours,
            skill_count=len(steps),
            total_allocated_minutes=plan.total_allocated_minutes,
            skill_ids=plan.skill_ids,
        )
        return plan

    # -- Time allocation ----------------------------------------------------

    def _allocate_time(
        self,
        skills: Sequence[Skill],
        total_minutes: int,
        intent_boosts: dict[str, int],
        topic: str,
    ) -> list[SkillStep]:
        """Allocate time across selected skills proportionally to their
        effective weight (priority + intent boost).

        Skills are ordered in execution sequence:
        1. Research/exploration skills first (web_research, academic_research)
        2. Domain analysis skills in priority order
        3. Synthesis/output skills last (visualization, document_generation)
        """

        # Calculate effective weights.
        weights: dict[str, float] = {}
        for skill in skills:
            base = skill.priority
            boost = intent_boosts.get(skill.id, 0) * 2.0
            weights[skill.id] = base + boost

        total_weight = sum(weights.values()) or 1.0

        # Reserve 10% for buffer/overrun.
        allocatable = int(total_minutes * 0.90)

        # Build steps with allocated time.
        raw_steps: list[SkillStep] = []
        for skill in skills:
            proportion = weights[skill.id] / total_weight
            minutes = max(10, int(allocatable * proportion))
            # Cap at the skill's estimated duration × 2 to avoid over-allocation.
            minutes = min(minutes, skill.estimated_duration_minutes * 2)

            rationale = self._generate_rationale(skill, topic, intent_boosts)
            raw_steps.append(
                SkillStep(
                    skill_id=skill.id,
                    skill_name=skill.name,
                    category=skill.category,
                    allocated_minutes=minutes,
                    priority=skill.priority,
                    rationale=rationale,
                    tools=list(skill.tools),
                )
            )

        # Sort into execution order.
        raw_steps.sort(key=lambda s: _execution_order_key(s))

        return raw_steps

    @staticmethod
    def _generate_rationale(
        skill: Skill,
        topic: str,
        intent_boosts: dict[str, int],
    ) -> str:
        """Generate a human-readable explanation of why this skill was
        selected."""
        reasons: list[str] = []

        boost = intent_boosts.get(skill.id, 0)
        if boost > 0:
            reasons.append(
                f"Direct keyword match in topic ({boost} pattern hit"
                f"{'s' if boost > 1 else ''})"
            )

        if skill.priority >= 8:
            reasons.append(f"High-priority skill (priority {skill.priority}/10)")
        elif skill.priority >= 6:
            reasons.append(f"Medium-priority skill (priority {skill.priority}/10)")

        reasons.append(skill.description)

        return ". ".join(reasons)


# ---------------------------------------------------------------------------
# Execution ordering
# ---------------------------------------------------------------------------

# Category order: research first, then domain analysis, then output.
_CATEGORY_ORDER: dict[str, int] = {
    "research": 0,
    "data": 1,
    "finance": 2,
    "coding": 3,
    "general": 4,
}

# Specific skills that should run early or late regardless of category.
_EARLY_SKILLS: frozenset[str] = frozenset({
    "web_research",
    "academic_research",
    "file_processing",
})

_LATE_SKILLS: frozenset[str] = frozenset({
    "document_generation",
    "email_notification",
    "visualization",
})


def _execution_order_key(step: SkillStep) -> tuple[int, int, str]:
    """Return a sort key that places research skills first, domain analysis
    in the middle, and output skills last."""
    if step.skill_id in _EARLY_SKILLS:
        phase = 0
    elif step.skill_id in _LATE_SKILLS:
        phase = 2
    else:
        phase = 1

    category_rank = _CATEGORY_ORDER.get(step.category, 3)
    return (phase, category_rank, step.skill_id)


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def build_skill_plan(
    topic: str,
    duration_hours: float = 4.0,
    max_skills: int = 10,
) -> SkillPlan:
    """One-shot convenience function: select skills and build a plan.

    Equivalent to ``SkillSelector().select(topic, duration_hours, max_skills)``.
    """
    selector = SkillSelector()
    return selector.select(
        topic=topic,
        duration_hours=duration_hours,
        max_skills=max_skills,
    )
