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

# ─── JSON repair helpers ─────────────────────────────────────────────────────


def _repair_json(text: str) -> str:
    """
    Attempt to repair common JSON issues produced by LLMs (especially Claude).

    Repairs applied in order:
    1. Remove trailing commas before } or ] (very common Claude habit).
    2. Replace single-quoted strings with double-quoted strings
       (only for simple cases — not inside already-double-quoted values).
    3. Remove control characters that are invalid in JSON strings.
    4. Fix unescaped newlines inside string values.
    5. Remove BOM / zero-width characters.
    """
    # Remove BOM and zero-width chars
    text = text.replace("\ufeff", "").replace("\u200b", "").replace("\u200c", "")

    # Remove trailing commas before closing braces/brackets
    text = re.sub(r",\s*([}\]])", r"\1", text)

    # Remove control characters (except \n, \r, \t which are valid in JSON strings
    # when properly escaped — but raw ones are not, so strip them outside strings)
    # This is a conservative pass: only remove truly unprintable chars.
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)

    return text


def _extract_json_object_greedy(text: str) -> str | None:
    """
    Find the outermost balanced { ... } in *text* using brace counting.

    This is the nuclear-option fallback when regex-based extraction fails.
    Handles nested braces and quoted strings (including escaped quotes).
    Returns None if no balanced object is found.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    i = start

    while i < len(text):
        ch = text[i]

        if escape:
            escape = False
            i += 1
            continue

        if ch == "\\" and in_string:
            escape = True
            i += 1
            continue

        if ch == '"' and not escape:
            in_string = not in_string
            i += 1
            continue

        if not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]

        i += 1

    return None

def _repair_truncated_json(text: str) -> str | None:
    """Attempt to close a truncated JSON object so it becomes parseable.

    When an LLM hits ``max_tokens`` the response is cut off mid-JSON.
    This function:
    1. Detects if the text starts with ``{`` but has no balanced closing ``}``.
    2. Finds the last position where a complete JSON value ended.
    3. Closes all open strings, arrays and objects.

    Returns the repaired text or None if repair is not applicable.
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None

    # Quick check: if the greedy extractor already finds a balanced object,
    # truncation repair is not needed.
    greedy = _extract_json_object_greedy(stripped)
    if greedy is not None:
        return None  # not actually truncated

    # Strategy: walk the string and track the stack of containers.
    # Record the position after each *complete* value (string, number, true,
    # false, null, or closing brace/bracket).  The "last safe position" is
    # the rightmost such point that is followed by a comma (or is the end of
    # a complete key: value pair).
    stack: list[str] = []  # '{' or '['
    in_string = False
    escape = False
    last_safe_pos = 0  # last position after a complete entry + comma

    i = 0
    while i < len(stripped):
        ch = stripped[i]

        if escape:
            escape = False
            i += 1
            continue

        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        # Outside a string
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("{")
        elif ch == "[":
            stack.append("[")
        elif ch == "}":
            if stack and stack[-1] == "{":
                stack.pop()
            # After closing a brace we have a complete value
        elif ch == "]":
            if stack and stack[-1] == "[":
                stack.pop()
        elif ch == ",":
            # A comma after a complete value means everything up to this
            # comma is safe (we can cut here and close containers).
            last_safe_pos = i

        i += 1

    if last_safe_pos == 0:
        return None  # couldn't find a safe cut point

    # Cut at the last safe comma (exclude the comma itself).
    truncated = stripped[:last_safe_pos]

    # Re-count open containers in the truncated portion.
    rstack: list[str] = []
    in_str = False
    esc = False
    for ch in truncated:
        if esc:
            esc = False
            continue
        if in_str:
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            rstack.append("{")
        elif ch == "[":
            rstack.append("[")
        elif ch == "}":
            if rstack and rstack[-1] == "{":
                rstack.pop()
        elif ch == "]":
            if rstack and rstack[-1] == "[":
                rstack.pop()

    # If we're inside an unclosed string, close it.
    if in_str:
        truncated += '"'

    # Close remaining open containers in reverse order.
    closers = "".join("}" if c == "{" else "]" for c in reversed(rstack))
    repaired = truncated + closers

    logger.info(
        "Truncated JSON repair: cut at pos %d/%d, closing %d containers",
        last_safe_pos,
        len(stripped),
        len(rstack),
    )
    return repaired


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
    3. Inline fenced block (```json{...}```)
    4. Greedy brace-matching extraction (handles prose around the JSON)
    5. Entire text stripped of leading/trailing whitespace

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

    # BUG-009 fix: Claude may produce prose before/after the JSON object.
    # Use greedy brace-matching to extract the outermost {...}.
    greedy = _extract_json_object_greedy(text)
    if greedy is not None:
        return greedy

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

    # Step 3 — parse JSON (with repair fallback)
    data: dict | None = None

    # Attempt 1: raw JSON parse
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        pass

    # Attempt 2: cosmetic repair (trailing commas, control chars, etc.)
    if data is None:
        repaired = _repair_json(json_text)
        try:
            data = json.loads(repaired)
            logger.info("JSON repair succeeded for schema=%s", output_schema.__name__)
        except json.JSONDecodeError:
            pass

    # Attempt 3: greedy brace extraction
    if data is None:
        greedy = _extract_json_object_greedy(_repair_json(json_text))
        if greedy is not None:
            try:
                data = json.loads(greedy)
                logger.info("Greedy brace extraction succeeded for schema=%s", output_schema.__name__)
            except json.JSONDecodeError:
                pass

    # Attempt 4 (BUG-020 fix): truncated JSON repair — the LLM hit max_tokens
    # and the JSON was cut off mid-object.  Close open containers.
    if data is None:
        trunc_repaired = _repair_truncated_json(_repair_json(json_text))
        if trunc_repaired is not None:
            try:
                data = json.loads(trunc_repaired)
                logger.info(
                    "Truncated JSON repair succeeded for schema=%s (cut from %d to %d chars)",
                    output_schema.__name__,
                    len(json_text),
                    len(trunc_repaired),
                )
            except json.JSONDecodeError:
                pass

    if data is None:
        logger.warning(
            "JSON decode failed after all repair attempts for schema=%s | excerpt=%r",
            output_schema.__name__,
            excerpt,
        )
        raise OutputParseError(
            detail="No valid JSON object found in model response",
            raw_excerpt=excerpt,
        )

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
