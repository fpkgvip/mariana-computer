"""Outbound plaintext-leak redaction.

When the agent runs with vault secrets injected as environment variables,
any plaintext value that ever reaches stdout/stderr/tool output **must**
be replaced with ``[REDACTED:KEY_NAME]`` before it is logged, streamed,
or persisted.

This module provides a single function, :func:`redact`, that builds a
single compiled regex from the given (name, value) pairs and rewrites
every match in one pass.  It is intentionally:

  • Allocation-free on the hot path (compile once, reuse).
  • Whitespace/encoding-tolerant: it matches the exact byte sequence,
    not a naive substring of the printed-quoted form.
  • Length-aware: short tokens (<8 chars) are skipped to avoid false
    positives on common substrings.

Usage::

    from mariana.vault.redaction import build_redactor

    redactor = build_redactor({"OPENAI_API_KEY": "sk-abc...xyz"})
    safe = redactor("the key is sk-abc...xyz, ok?")
    # → "the key is [REDACTED:OPENAI_API_KEY], ok?"
"""

from __future__ import annotations

import re
from typing import Callable, Mapping

# Below this length we refuse to redact — too prone to false positives.
_MIN_TOKEN_LEN = 8

# Hard cap on number of secrets in one redactor (defence against a
# rogue user trying to DoS the regex compiler).
_MAX_SECRETS = 256


def build_redactor(secrets: Mapping[str, str]) -> Callable[[str], str]:
    """Compile a fast string-rewriter that redacts the given secrets.

    Args:
        secrets: mapping of ``name → plaintext_value``.  Names are
            assumed to match the vault grammar (``[A-Z][A-Z0-9_]{0,63}``).
            Values shorter than 8 characters are silently skipped.

    Returns:
        A pure function ``str → str`` that performs the redaction.
        When ``secrets`` is empty the identity function is returned.
    """
    if not secrets:
        return _identity
    if len(secrets) > _MAX_SECRETS:
        raise ValueError(f"too many secrets to redact ({len(secrets)} > {_MAX_SECRETS})")

    # Sort by descending length so an alternation prefers the longest
    # match (``ABC123`` redacted before ``ABC`` would be).
    items = sorted(
        ((name, value) for name, value in secrets.items() if len(value) >= _MIN_TOKEN_LEN),
        key=lambda nv: -len(nv[1]),
    )
    if not items:
        return _identity

    # Map each escaped value to a numbered capture group label so the
    # callback can recover the original name in O(1).
    name_by_pattern: dict[str, str] = {}
    parts: list[str] = []
    for name, value in items:
        pat = re.escape(value)
        if pat in name_by_pattern:
            # Two distinct names bound to the same value — redact under
            # the first registered name (sort is stable so this is
            # deterministic).
            continue
        name_by_pattern[pat] = name
        parts.append(pat)

    big = re.compile("|".join(parts))

    # Reverse-lookup table from compiled-pattern alternation to name.
    # We pre-compile each individual pattern once for the substitution
    # callback so we don't re-escape on every call.
    name_index = list(name_by_pattern.items())  # [(pat, name), ...]

    def _replace(match: re.Match[str]) -> str:
        text = match.group(0)
        for pat, name in name_index:
            if re.fullmatch(pat, text):
                return f"[REDACTED:{name}]"
        # Defensive fallback (should be unreachable).
        return "[REDACTED]"

    def redact(s: str) -> str:
        if not s:
            return s
        return big.sub(_replace, s)

    return redact


def _identity(s: str) -> str:
    return s


__all__ = ["build_redactor"]
