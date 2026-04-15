"""Persistent memory system — stores user preferences and context across sessions.

Each user gets a JSON file under ``DATA_ROOT/memory/{user_id}/memory.json``.
The file stores:

- **facts**: durable facts about the user or their research interests.
- **preferences**: key/value pairs (e.g. preferred output format).
- **history**: summaries of completed investigations (rolling window of 100).

The ``get_context_for_prompt()`` method builds a compact text block suitable
for injection into LLM system prompts so the AI can personalise responses.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


class UserMemory:
    """Per-user persistent memory backed by a JSON file on disk."""

    def __init__(self, user_id: str, data_root: Path) -> None:
        self.user_id = user_id
        self.memory_dir = data_root / "memory" / user_id
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.memory_file = self.memory_dir / "memory.json"
        self._data: dict[str, object] = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, object]:
        if self.memory_file.exists():
            try:
                return json.loads(self.memory_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("memory_load_failed", user_id=self.user_id, error=str(exc))
        return {
            "facts": [],
            "preferences": {},
            "history": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _save(self) -> None:
        self.memory_file.write_text(
            json.dumps(self._data, indent=2, default=str),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Facts
    # ------------------------------------------------------------------

    def store_fact(self, fact: str, category: str = "general") -> None:
        """Store a durable fact about the user (deduplicated by content hash)."""
        entry = {
            "fact": fact,
            "category": category,
            "stored_at": datetime.now(timezone.utc).isoformat(),
        }
        fact_hash = hashlib.md5(fact.encode()).hexdigest()
        facts: list[dict[str, str]] = self._data.get("facts", [])  # type: ignore[assignment]
        self._data["facts"] = [
            f for f in facts if hashlib.md5(f["fact"].encode()).hexdigest() != fact_hash
        ]
        self._data["facts"].append(entry)  # type: ignore[union-attr]
        self._save()

    def get_facts(self, category: str | None = None) -> list[str]:
        """Return stored facts, optionally filtered by category."""
        facts: list[dict[str, str]] = self._data.get("facts", [])  # type: ignore[assignment]
        if category:
            facts = [f for f in facts if f.get("category") == category]
        return [f["fact"] for f in facts]

    def delete_fact(self, fact: str) -> bool:
        """Remove a fact by its text content. Returns True if found and removed."""
        facts: list[dict[str, str]] = self._data.get("facts", [])  # type: ignore[assignment]
        original_len = len(facts)
        self._data["facts"] = [f for f in facts if f["fact"] != fact]
        if len(self._data["facts"]) < original_len:  # type: ignore[arg-type]
            self._save()
            return True
        return False

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def store_preference(self, key: str, value: str) -> None:
        """Store a user preference (key/value pair)."""
        prefs: dict[str, dict[str, str]] = self._data.get("preferences", {})  # type: ignore[assignment]
        prefs[key] = {"value": value, "updated_at": datetime.now(timezone.utc).isoformat()}
        self._data["preferences"] = prefs
        self._save()

    def get_preferences(self) -> dict[str, str]:
        """Return all preferences as a flat key→value mapping."""
        prefs: dict[str, dict[str, str]] = self._data.get("preferences", {})  # type: ignore[assignment]
        return {k: v["value"] for k, v in prefs.items()}

    def delete_preference(self, key: str) -> bool:
        """Remove a preference by key. Returns True if found and removed."""
        prefs: dict[str, dict[str, str]] = self._data.get("preferences", {})  # type: ignore[assignment]
        if key in prefs:
            del prefs[key]
            self._data["preferences"] = prefs
            self._save()
            return True
        return False

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def add_to_history(self, topic: str, summary: str) -> None:
        """Record a completed investigation summary (rolling window of 100)."""
        history: list[dict[str, str]] = self._data.get("history", [])  # type: ignore[assignment]
        history.append({
            "topic": topic,
            "summary": summary[:500],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        self._data["history"] = history[-100:]
        self._save()

    def get_history(self, limit: int = 10) -> list[dict[str, str]]:
        """Return the most recent investigation summaries."""
        history: list[dict[str, str]] = self._data.get("history", [])  # type: ignore[assignment]
        return history[-limit:]

    # ------------------------------------------------------------------
    # Prompt context
    # ------------------------------------------------------------------

    def get_context_for_prompt(self) -> str:
        """Build a compact context string for LLM prompt injection."""
        parts: list[str] = []

        prefs = self.get_preferences()
        if prefs:
            parts.append("User preferences: " + "; ".join(f"{k}: {v}" for k, v in prefs.items()))

        facts = self.get_facts()
        if facts:
            parts.append("Known facts: " + "; ".join(facts[-10:]))

        history: list[dict[str, str]] = self._data.get("history", [])  # type: ignore[assignment]
        if history:
            recent = history[-5:]
            parts.append("Recent research: " + "; ".join(h["topic"] for h in recent))

        return "\n".join(parts) if parts else ""
