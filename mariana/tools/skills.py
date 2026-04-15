"""Skills system — reusable instruction sets for common research workflows.

A *Skill* is a named bundle of: system prompt, description, and trigger
keywords.  The ``SkillManager`` auto-detects skills from the user's query
topic and enriches the AI system prompt accordingly.

Built-in skills cover the most common financial research patterns.  Users
can also create custom skills that are persisted as JSON files under
``DATA_ROOT/skills/``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class Skill:
    """A reusable research workflow definition."""

    id: str
    name: str
    description: str
    system_prompt: str
    trigger_keywords: list[str] = field(default_factory=list)
    category: str = "built-in"  # built-in | user | org
    owner_id: str | None = None  # None for built-in skills


# ---------------------------------------------------------------------------
# Built-in skills
# ---------------------------------------------------------------------------

BUILTIN_SKILLS: list[Skill] = [
    Skill(
        id="research-report",
        name="Research Report",
        description="Generate comprehensive research reports with citations, data analysis, and actionable conclusions.",
        system_prompt=(
            "You are generating a comprehensive research report. Structure your output with: "
            "Executive Summary, Methodology, Key Findings (with citations), Data Analysis, "
            "Risk Factors, and Conclusions. Every factual claim must have a citation."
        ),
        trigger_keywords=["report", "research report", "analysis", "deep dive", "investigation"],
        category="built-in",
    ),
    Skill(
        id="financial-analysis",
        name="Financial Analysis",
        description="Analyze financial statements, SEC filings, and market data to produce investment-grade analysis.",
        system_prompt=(
            "You are a CFA-level financial analyst. Analyze financial data with: "
            "Revenue/Earnings analysis, Balance Sheet review, Cash Flow assessment, "
            "Key Ratios (P/E, EV/EBITDA, ROE, etc.), Peer Comparison, and Forward estimates. "
            "Cite all data sources."
        ),
        trigger_keywords=["financial", "earnings", "valuation", "SEC filing", "balance sheet", "income statement"],
        category="built-in",
    ),
    Skill(
        id="competitive-analysis",
        name="Competitive Analysis",
        description="Map competitive landscapes, identify market positioning, and analyze competitive dynamics.",
        system_prompt=(
            "You are a strategy consultant. Produce: Market Overview, Key Players, "
            "Competitive Positioning Matrix, SWOT for each player, Market Share analysis, "
            "Competitive Dynamics, and Strategic Implications."
        ),
        trigger_keywords=["competitive", "competition", "market share", "landscape", "vs", "compare"],
        category="built-in",
    ),
    Skill(
        id="data-analysis",
        name="Data Analysis",
        description="Quantitative analysis with statistical methods, data visualization descriptions, and pattern identification.",
        system_prompt=(
            "You are a quantitative analyst. Apply statistical methods: Descriptive Statistics, "
            "Trend Analysis, Correlation/Regression, Hypothesis Testing, Anomaly Detection. "
            "Present results with methodology and confidence intervals."
        ),
        trigger_keywords=["data", "statistics", "quantitative", "correlation", "regression", "trend"],
        category="built-in",
    ),
    Skill(
        id="presentation-builder",
        name="Presentation Builder",
        description="Create structured slide presentations from research findings.",
        system_prompt=(
            "You are building a professional presentation. Create structured slide content with: "
            "Title Slide, Agenda, Key sections with bullet points, Data slides with chart "
            "descriptions, Summary/Conclusions, and Next Steps. Format output as JSON with slides array."
        ),
        trigger_keywords=["presentation", "slides", "pptx", "powerpoint", "deck"],
        category="built-in",
    ),
    Skill(
        id="excel-model",
        name="Excel Model Builder",
        description="Build financial models, DCF valuations, and data tables in Excel format.",
        system_prompt=(
            "You are building a financial model in Excel. Create structured workbook content with: "
            "Assumptions sheet, Revenue model, Cost structure, Cash flow projection, "
            "Valuation (DCF/comparables), Sensitivity analysis. Format output as JSON with sheets object."
        ),
        trigger_keywords=["excel", "model", "spreadsheet", "dcf", "valuation model", "financial model"],
        category="built-in",
    ),
]


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class SkillManager:
    """Manages built-in and custom skills."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.skills_dir = data_root / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._builtin: dict[str, Skill] = {s.id: s for s in BUILTIN_SKILLS}

    def detect_skill(self, topic: str) -> Skill | None:
        """Auto-detect which skill to activate based on the research topic.

        Checks custom skills first (so users can override built-ins), then
        built-in skills.  Returns ``None`` if no keyword matches.
        """
        topic_lower = topic.lower()
        for skill in self._load_custom_skills() + list(self._builtin.values()):
            for keyword in skill.trigger_keywords:
                if keyword.lower() in topic_lower:
                    return skill
        return None

    def get_skill(self, skill_id: str) -> Skill | None:
        """Look up a skill by ID (built-in or custom)."""
        if skill_id in self._builtin:
            return self._builtin[skill_id]
        return self._load_custom_skill(skill_id)

    def list_skills(self, owner_id: str | None = None) -> list[Skill]:
        """Return all available skills (built-in + custom)."""
        skills = list(self._builtin.values())
        custom = self._load_custom_skills()
        if owner_id:
            custom = [s for s in custom if s.owner_id == owner_id or s.category == "org"]
        skills.extend(custom)
        return skills

    def create_skill(
        self,
        name: str,
        description: str,
        system_prompt: str,
        trigger_keywords: list[str],
        owner_id: str,
        category: str = "user",
    ) -> Skill:
        """Create and persist a custom skill."""
        skill = Skill(
            id=f"custom-{name.lower().replace(' ', '-')}",
            name=name,
            description=description,
            system_prompt=system_prompt,
            trigger_keywords=trigger_keywords,
            category=category,
            owner_id=owner_id,
        )
        skill_file = self.skills_dir / f"{skill.id}.json"
        skill_file.write_text(
            json.dumps(
                {
                    "id": skill.id,
                    "name": skill.name,
                    "description": skill.description,
                    "system_prompt": skill.system_prompt,
                    "trigger_keywords": skill.trigger_keywords,
                    "category": skill.category,
                    "owner_id": skill.owner_id,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("skill_created", skill_id=skill.id, owner=owner_id)
        return skill

    def delete_skill(self, skill_id: str) -> bool:
        """Delete a custom skill by ID. Returns True if deleted."""
        f = self.skills_dir / f"{skill_id}.json"
        if f.exists():
            f.unlink()
            return True
        return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_custom_skills(self) -> list[Skill]:
        skills: list[Skill] = []
        for f in self.skills_dir.glob("custom-*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                skills.append(Skill(**data))
            except Exception:
                pass
        return skills

    def _load_custom_skill(self, skill_id: str) -> Skill | None:
        f = self.skills_dir / f"{skill_id}.json"
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                return Skill(**data)
            except Exception:
                return None
        return None
