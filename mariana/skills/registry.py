"""
mariana/skills/registry.py

Skill registry for the Mariana research engine.

Manages the complete catalogue of available skills and capabilities.  Each
skill carries a system prompt that is injected into the AI context when
activated, a list of tool functions it requires, and metadata used by the
skill selector to build optimal investigation plans.

The registry is a singleton-style module object: call ``get_registry()`` to
obtain the process-wide instance populated with all built-in skills.

Usage::

    from mariana.skills.registry import get_registry

    registry = get_registry()
    skills = registry.get_skills_for_topic("SEC 10-K analysis of Apple")
    combined_prompt = registry.get_system_prompt([s.id for s in skills])
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Sequence

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Skill dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Skill:
    """A single capability that can be activated during a research session.

    Attributes:
        id: Machine-readable identifier (e.g. ``"sec_filing_analysis"``).
        name: Human-readable display name.
        category: Broad grouping — ``"finance"``, ``"data"``, ``"research"``,
            ``"coding"``, or ``"general"``.
        description: One-paragraph explanation of the skill's purpose.
        system_prompt: Expert instructions injected into the AI context when
            this skill is active.  Typically 200-500 words.
        tools: List of tool function identifiers that this skill uses.
        estimated_duration_minutes: Typical wall-clock time for this skill.
        priority: Ordering weight (1 = lowest, 10 = highest) used when
            multiple skills compete for time budget.
        keywords: Supplementary search terms used for fuzzy topic matching.
    """

    id: str
    name: str
    category: str
    description: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    estimated_duration_minutes: int = 30
    priority: int = 5
    keywords: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SkillRegistry:
    """Manages the full catalogue of available skills.

    Skills are registered via :meth:`register` (typically at import time by
    the finance_skills and general_skills modules) and queried via
    :meth:`get_skills_for_topic` or :meth:`get_skill`.
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        logger.info("skill_registry.init", msg="SkillRegistry initialised")

    # -- Registration -------------------------------------------------------

    def register(self, skill: Skill) -> None:
        """Register a skill.  Overwrites silently on duplicate ID."""
        if skill.id in self._skills:
            logger.warning(
                "skill_registry.overwrite",
                skill_id=skill.id,
                msg="Overwriting existing skill registration",
            )
        self._skills[skill.id] = skill
        logger.debug("skill_registry.register", skill_id=skill.id)

    def register_many(self, skills: Sequence[Skill]) -> None:
        """Convenience: register a batch of skills at once."""
        for skill in skills:
            self.register(skill)

    # -- Lookup -------------------------------------------------------------

    def get_skill(self, skill_id: str) -> Skill:
        """Return a skill by its exact ID.

        Raises:
            KeyError: If the skill ID is not registered.
        """
        try:
            return self._skills[skill_id]
        except KeyError:
            logger.error("skill_registry.not_found", skill_id=skill_id)
            raise KeyError(f"Skill '{skill_id}' is not registered") from None

    def list_all(self) -> list[Skill]:
        """Return every registered skill, sorted by category then priority
        (descending)."""
        return sorted(
            self._skills.values(),
            key=lambda s: (s.category, -s.priority),
        )

    # -- Topic matching -----------------------------------------------------

    def get_skills_for_topic(self, topic: str) -> list[Skill]:
        """Return skills relevant to *topic*, ordered by match quality.

        Matching strategy (applied in order, de-duplicated):

        1. **Keyword hit** — any keyword defined on the skill appears as a
           substring (case-insensitive) in the *topic*.
        2. **Fuzzy name/description** — SequenceMatcher ratio ≥ 0.35 between
           the normalised topic and the skill's name, description, or any
           keyword.

        Results are sorted by ``(match_score desc, priority desc)`` and
        capped at 15 skills to avoid flooding the prompt.
        """
        topic_lower = _normalise(topic)
        scored: list[tuple[float, Skill]] = []

        for skill in self._skills.values():
            best = _compute_relevance(topic_lower, skill)
            if best > 0.0:
                scored.append((best, skill))

        scored.sort(key=lambda pair: (pair[0], pair[1].priority), reverse=True)
        results = [skill for _score, skill in scored[:15]]

        logger.info(
            "skill_registry.topic_match",
            topic=topic[:120],
            matched=len(results),
            ids=[s.id for s in results],
        )
        return results

    # -- Prompt composition -------------------------------------------------

    def get_system_prompt(self, skill_ids: list[str]) -> str:
        """Combine the system prompts of all requested skills into a single
        coherent instruction block.

        Skills are ordered by priority (descending) so that the most
        important domain expertise appears first.

        Args:
            skill_ids: List of skill IDs whose prompts should be merged.

        Returns:
            A single string containing all skill prompts separated by section
            headers.  Returns an empty string if no valid skill IDs are
            provided.
        """
        skills: list[Skill] = []
        for sid in skill_ids:
            try:
                skills.append(self.get_skill(sid))
            except KeyError:
                logger.warning(
                    "skill_registry.prompt_skip",
                    skill_id=sid,
                    msg="Skipping unknown skill ID when building prompt",
                )

        if not skills:
            return ""

        skills.sort(key=lambda s: s.priority, reverse=True)

        sections: list[str] = [
            "═══════════════════════════════════════════════════════════════════"
            "════════════",
            "ACTIVE SKILL CONTEXT",
            "═══════════════════════════════════════════════════════════════════"
            "════════════",
            "",
        ]

        for skill in skills:
            sections.append(
                f"──── {skill.name} [{skill.category.upper()}] "
                f"(priority {skill.priority}/10) ────"
            )
            sections.append(skill.system_prompt.strip())
            sections.append("")

        combined = "\n".join(sections)
        logger.debug(
            "skill_registry.prompt_built",
            skill_count=len(skills),
            prompt_chars=len(combined),
        )
        return combined


# ---------------------------------------------------------------------------
# Fuzzy matching helpers
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")


def _normalise(text: str) -> str:
    """Lower-case, strip non-alphanumeric, collapse whitespace."""
    return " ".join(_WORD_RE.findall(text.lower()))


def _compute_relevance(topic_normalised: str, skill: Skill) -> float:
    """Score how relevant *skill* is to *topic_normalised* (0.0–1.0).

    Combines keyword substring hits with SequenceMatcher fuzzy ratios.
    """
    best = 0.0

    # Check keywords for substring match (strong signal).
    for kw in skill.keywords:
        kw_lower = kw.lower()
        if kw_lower in topic_normalised:
            # Longer keyword match = higher confidence.
            best = max(best, min(1.0, 0.50 + len(kw_lower) / 80.0))

    # Check skill name, description, and keywords via fuzzy ratio.
    for candidate_text in [
        skill.name,
        skill.description,
        " ".join(skill.keywords),
    ]:
        candidate_norm = _normalise(candidate_text)
        if not candidate_norm:
            continue
        ratio = SequenceMatcher(None, topic_normalised, candidate_norm).ratio()
        if ratio >= 0.35:
            best = max(best, ratio * 0.80)  # Scale down fuzzy vs exact.

    # Category-level boost: if the category word appears in the topic, small
    # bump even if individual matching was marginal.
    if skill.category.lower() in topic_normalised and best > 0.0:
        best = min(1.0, best + 0.05)

    return best


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

_GLOBAL_REGISTRY: SkillRegistry | None = None
# BUG-024 fix: guard singleton initialisation with a threading.Lock so that
# concurrent callers (e.g. from a thread pool) don't create multiple registries
# and run the skill-registration side-effects more than once.
_GLOBAL_REGISTRY_LOCK: threading.Lock = threading.Lock()


def get_registry() -> SkillRegistry:
    """Return (and lazily create) the process-wide :class:`SkillRegistry`.

    On first call this imports the built-in skill modules which register
    their skills as a side-effect.  Thread-safe via double-checked locking.
    """
    global _GLOBAL_REGISTRY

    # Fast path: no lock needed once the registry is already built.
    if _GLOBAL_REGISTRY is not None:
        return _GLOBAL_REGISTRY

    with _GLOBAL_REGISTRY_LOCK:
        # Re-check under the lock in case another thread initialised first.
        if _GLOBAL_REGISTRY is not None:
            return _GLOBAL_REGISTRY

        _GLOBAL_REGISTRY = SkillRegistry()

        # Import skill definition modules — they call register_many() on import.
        from mariana.skills.finance_skills import register_finance_skills  # noqa: F811
        from mariana.skills.general_skills import register_general_skills  # noqa: F811

        register_finance_skills(_GLOBAL_REGISTRY)
        register_general_skills(_GLOBAL_REGISTRY)

        logger.info(
            "skill_registry.loaded",
            total_skills=len(_GLOBAL_REGISTRY._skills),
        )

    return _GLOBAL_REGISTRY
