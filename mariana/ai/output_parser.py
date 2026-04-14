"""
mariana/ai/output_parser.py

Parse raw LLM response text into validated Pydantic model instances.

Design decisions:
- We prefer JSON wrapped in markdown fences (```json…```) because all our
  prompts explicitly instruct the model to emit that format.  If no fence is
  found we fall back to treating the entire response as raw JSON, which covers
  models that occasionally skip the fence.
- ``OutputParseError`` carries the raw excerpt so callers can log / display it
  without re-fetching the text.
- ``build_error_hint()`` produces a terse, structured repair prompt that is
  injected as the next user message on the one allowed retry.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

# ─── Exceptions ──────────────────────────────────────────────────────────────


class OutputParseError(Exception):
    """
    Raised when the model output cannot be parsed or validated.

    Attributes:
        raw_excerpt: First 500 characters of the raw response for logging.
        detail: Human-readable explanation of what went wrong.
    """

    def __init__(self, detail: str, raw_excerpt: str = "") -> None:
        super().__init__(detail)
        self.detail = detail
        self.raw_excerpt = raw_excerpt

    def __str__(self) -> str:
        if self.raw_excerpt:
            return f"{self.detail} | raw_excerpt={self.raw_excerpt!r}"
        return self.detail


# ─── Internal helpers ─────────────────────────────────────────────────────────

# BUG-008 fix: use \r?\n so Windows-style CRLF endings are also matched.
# Also allow optional trailing whitespace before the closing fence.
# Matches ```json … ``` blocks only (language tag is required).
# Using a strict match ensures bare ``` … ``` fences fall through to _BARE_FENCE_RE.
_JSON_FENCE_RE = re.compile(
    r"```json\s*\r?\n(.*?)\r?\n\s*```",
    re.DOTALL | re.IGNORECASE,
)

# Matches bare ``` … ``` fences (no language tag).
# Only reached when _JSON_FENCE_RE does not match.
_BARE_FENCE_RE = re.compile(
    r"```\s*\r?\n(.*?)\r?\n\s*```",
    re.DOTALL,
)

# Additional fallback: strip ```json...``` markers even when no newline separates
# the fence from the JSON body (e.g. ```json{...}``` on a single line).
_INLINE_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(.*?)\s*```",
    re.DOTALL | re.IGNORECASE,
)


def _extract_json_text(text: str) -> str:
    """
    Find the first JSON block in *text*.

    Search order:
    1. ``json-fenced block (```json … ```)
    2. Bare fenced block (``` … ```)
    3. Entire text stripped of leading/trailing whitespace

    Returns the candidate JSON string without the fence markers.
    """
    m = _JSON_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()

    m = _BARE_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()

    # BUG-008 fix: handle inline fences like ```json{...}``` with no newlines.
    m = _INLINE_JSON_FENCE_RE.search(text)
    if m:
        candidate = m.group(1).strip()
        # Only use the inline match if it looks like JSON (starts with { or [).
        if candidate.startswith("{") or candidate.startswith("["):
            return candidate

    return text.strip()


def _schema_summary(schema: type[BaseModel]) -> str:
    """
    Return a compact JSON Schema-like summary of *schema* for the error hint.
    We list required fields and their types from the model's JSON schema.
    """
    try:
        js = schema.model_json_schema()
        props: dict[str, Any] = js.get("properties", {})
        required: list[str] = js.get("required", [])
        lines: list[str] = []
        for name, meta in props.items():
            req_marker = "*" if name in required else ""
            field_type = meta.get("type") or meta.get("$ref", "object").split("/")[-1]
            lines.append(f"  {req_marker}{name}: {field_type}")
        return "{\n" + "\n".join(lines) + "\n}"
    except Exception:
        return schema.__name__


# ─── Public API ──────────────────────────────────────────────────────────────


def parse_output(raw_text: str, output_schema: type[BaseModel]) -> BaseModel:
    """
    Extract and validate a JSON response from *raw_text*.

    Steps:
    1. Extract JSON from markdown code fences (```json … ```) if present.
    2. If no fence found, attempt to parse the entire text as JSON.
    3. Validate the parsed dict against *output_schema* via Pydantic.

    Args:
        raw_text: The full model response string.
        output_schema: Pydantic ``BaseModel`` subclass to validate against.

    Returns:
        A validated instance of *output_schema*.

    Raises:
        OutputParseError: If JSON extraction, JSON parsing, or Pydantic
            validation fails.
    """
    excerpt = raw_text[:500] if len(raw_text) > 500 else raw_text

    # Step 1 & 2 — extract JSON text
    json_text = _extract_json_text(raw_text)

    # Step 3 — parse JSON
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        logger.warning(
            "JSON decode failed for schema=%s: %s | excerpt=%r",
            output_schema.__name__,
            exc,
            excerpt,
        )
        raise OutputParseError(
            detail=f"JSON decode error: {exc}",
            raw_excerpt=excerpt,
        ) from exc

    if not isinstance(data, dict):
        raise OutputParseError(
            detail=f"Expected JSON object at top level, got {type(data).__name__}",
            raw_excerpt=excerpt,
        )

    # Step 4 — Pydantic validation
    try:
        return output_schema.model_validate(data, strict=False)
    except ValidationError as exc:
        error_summary = "; ".join(
            f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}"
            for e in exc.errors()[:5]  # cap at 5 errors for readability
        )
        logger.warning(
            "Pydantic validation failed for schema=%s: %s | excerpt=%r",
            output_schema.__name__,
            error_summary,
            excerpt,
        )
        raise OutputParseError(
            detail=f"Schema validation failed: {error_summary}",
            raw_excerpt=excerpt,
        ) from exc


def build_error_hint(
    error: OutputParseError,
    output_schema: type[BaseModel],
) -> str:
    """
    Build an error-repair message to inject into the next retry call.

    The hint is designed to be appended as an additional user message so the
    model understands exactly what went wrong and what it must fix.

    Args:
        error: The :class:`OutputParseError` from the failed parse attempt.
        output_schema: The target Pydantic schema the model must emit.

    Returns:
        A plain-text repair prompt (no markdown) ready to be used as message
        content in the next API call.
    """
    schema_summary = _schema_summary(output_schema)

    hint_lines = [
        "Your previous response could not be parsed. Fix it and respond again.",
        "",
        f"Parse error: {error.detail}",
        "",
        "Requirements:",
        "  1. Respond with ONLY a valid JSON object — no prose, no markdown, no fences.",
        "  2. The JSON object MUST match this schema (* = required field):",
        "",
        schema_summary,
        "",
        "Do not include any text outside the JSON object.",
    ]

    if error.raw_excerpt:
        hint_lines += [
            "",
            "The beginning of your previous response (for reference):",
            f"  {error.raw_excerpt!r}",
        ]

    return "\n".join(hint_lines)
