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
import re
from datetime import datetime, timezone
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# H-03: reject any user_id that isn't the plain UUID/alphanumeric shape we
# actually hand out.  Path separators, NUL, leading dots, etc., are all
# rejected so the joined directory can't escape DATA_ROOT/memory.
_USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}$")

# H-02: keep injected context bounded and defanged before it reaches the LLM.
_MEMORY_CONTEXT_MAX_CHARS = 5000
_MEMORY_FIELD_MAX_CHARS = 500

_INJECTION_PATTERNS = [
    re.compile(r"(?i)ignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|prompts|directives)[^\n]*"),
    re.compile(r"(?i)disregard\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|prompts|directives)[^\n]*"),
    re.compile(r"(?i)forget\s+(?:everything|all)\s+(?:above|before|prior)[^\n]*"),
    re.compile(r"(?im)^\s*system\s*:\s*"),
    re.compile(r"(?im)^\s*assistant\s*:\s*"),
    re.compile(r"(?i)new\s+instructions?\s*:"),
    re.compile(r"(?i)<\s*/?\s*system\s*>"),
]
_FENCE_RE = re.compile(r"```+")


def _sanitize_snippet(text: str, max_chars: int = _MEMORY_FIELD_MAX_CHARS) -> str:
    """Truncate and defang a stored-memory string before embedding in a prompt."""
    if not text:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return ""
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    for pat in _INJECTION_PATTERNS:
        text = pat.sub("[filtered]", text)
    text = _FENCE_RE.sub("'''", text)
    # Collapse any line that looks like a delimiter break-out attempt.
    text = text.replace("\x00", "")
    return text


class UserMemory:
    """Per-user persistent memory backed by a JSON file on disk."""

    def __init__(self, user_id: str, data_root: Path) -> None:
        # H-03 fix: validate user_id format, then verify the resolved directory
        # is still inside DATA_ROOT/memory (defence-in-depth vs. symlinks or
        # resolver edge cases).
        if not isinstance(user_id, str) or not _USER_ID_RE.match(user_id):
            raise ValueError(f"Invalid user_id for memory path: {user_id!r}")
        self.user_id = user_id

        memory_root = (data_root / "memory").resolve()
        memory_dir = (data_root / "memory" / user_id).resolve()
        if not memory_dir.is_relative_to(memory_root):
            raise ValueError(f"Resolved memory path escapes DATA_ROOT: {user_id!r}")

        self.memory_dir = memory_dir
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
        """Build a compact context string for LLM prompt injection.

        H-02 fix: stored user content is sanitised and length-capped before
        being embedded in a system prompt so a previously-stored malicious
        fact cannot inject instructions into every future AI session.
        """
        parts: list[str] = []

        prefs = self.get_preferences()
        if prefs:
            safe_prefs = [
                f"{_sanitize_snippet(str(k), 64)}: {_sanitize_snippet(str(v))}"
                for k, v in list(prefs.items())[:20]
            ]
            parts.append("User preferences: " + "; ".join(safe_prefs))

        facts = self.get_facts()
        if facts:
            safe_facts = [_sanitize_snippet(f) for f in facts[-10:]]
            parts.append("Known facts: " + "; ".join(safe_facts))

        history: list[dict[str, str]] = self._data.get("history", [])  # type: ignore[assignment]
        if history:
            recent = history[-5:]
            safe_topics = [_sanitize_snippet(h.get("topic", ""), 120) for h in recent]
            parts.append("Recent research: " + "; ".join(safe_topics))

        if not parts:
            return ""

        joined = "\n".join(parts)
        if len(joined) > _MEMORY_CONTEXT_MAX_CHARS:
            joined = joined[: _MEMORY_CONTEXT_MAX_CHARS - 3] + "..."
        return joined
