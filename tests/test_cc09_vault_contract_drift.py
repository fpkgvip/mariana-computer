"""CC-09 regression — vault fetch/store contract drift.

CC-09 (P4, Phase F re-audit #36) found two vault contract drifts:

1.  ``validate_vault_env`` (the API ingest / WRITE path) explicitly drops
    entries whose value is the empty string (runtime.py line ~105:
    ``if len(value) == 0: continue``).  ``fetch_vault_env`` (the worker
    READ path), however, accepted an empty-string value as long as
    ``isinstance(v, str)``.  Net effect: a corrupted or poisoned blob
    like ``b'{"FOO": ""}'`` round-tripped to ``{"FOO": ""}`` in the
    agent process, even though that shape is unreachable through the
    normal ingest API.  The agent loop would then install
    a present-but-empty ``FOO`` — observably distinct from "FOO
    missing" for shells / redactors that distinguish ``[ -z "$FOO" ]``
    from ``[ -v FOO ]``.

2.  ``_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")`` — Python's ``$``
    matches before a trailing ``\\n``, so ``_NAME_RE.match("FOO\\n")``
    returned truthy.  A poisoned payload like ``b'{"FOO\\n": "v"}'``
    therefore passed shape validation and could reach the env / log
    layer, where the trailing newline is a minor log-corruption /
    spoofing vector.

This module pins the fix:

  * empty-string value + ``requires_vault=True``  ->  ``VaultUnavailableError``
    with reason ``empty_value``
  * empty-string value + ``requires_vault=False`` ->  dict WITHOUT that key,
    plus a warning so ops can see the corruption
  * ``_NAME_RE`` rejects names with a trailing ``\\n`` (i.e. anchored
    with ``\\Z`` rather than ``$``); a payload ``b'{"FOO\\n": "v"}'``
    therefore raises under ``requires_vault=True`` and is dropped under
    ``requires_vault=False``.
"""

from __future__ import annotations

import logging
import re

import pytest

from mariana.vault import runtime as vault_runtime
from mariana.vault import store as vault_store
from mariana.vault.runtime import (
    VaultUnavailableError,
    fetch_vault_env,
)


# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/test_cc04_vault_malformed_payload_fail_closed.py)
# ---------------------------------------------------------------------------


class _StubRedis:
    """Fake redis whose ``get`` returns a caller-supplied raw payload."""

    def __init__(self, payload: bytes | str | None) -> None:
        self._payload = payload
        self.get_calls = 0

    async def get(self, _key):
        self.get_calls += 1
        return self._payload

    async def set(self, *_a, **_kw):  # pragma: no cover - unused
        return None

    async def delete(self, *_a, **_kw):  # pragma: no cover - unused
        return 0


_LOCAL_URL = "redis://localhost:6379/0"


# ---------------------------------------------------------------------------
# (1) empty-string value + requires_vault=True -> raises with reason
# ---------------------------------------------------------------------------


async def test_empty_value_with_requires_vault_raises():
    """A present-but-empty value must fail-closed under requires_vault=True.

    ``validate_vault_env`` would have dropped this entry on ingest; the
    fetch path must agree (or be stricter) so the two contracts cannot
    drift.  A ``{"FOO": ""}`` payload therefore raises rather than
    silently round-tripping ``""`` into the agent env.
    """
    r = _StubRedis(b'{"OPENAI_API_KEY": ""}')
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-cc09-1", requires_vault=True, redis_url=_LOCAL_URL
        )
    assert "empty_value" in str(exc.value)
    # The offending key name should appear in the diagnostic so ops can
    # locate the corrupted entry.
    assert "OPENAI_API_KEY" in str(exc.value)


async def test_empty_value_among_valid_entries_with_requires_vault_raises():
    """Mixed payload — even one empty-value key trips fail-closed."""
    r = _StubRedis(b'{"OPENAI_API_KEY": "real-token", "ANTHROPIC_KEY": ""}')
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-cc09-2", requires_vault=True, redis_url=_LOCAL_URL
        )
    assert "empty_value" in str(exc.value)


# ---------------------------------------------------------------------------
# (2) empty-string value + requires_vault=False -> dict without the key
# ---------------------------------------------------------------------------


async def test_empty_value_without_requires_vault_drops_key_and_warns(caplog):
    """Legacy semantics: degrade to ``{}``-minus-key, with a warning."""
    r = _StubRedis(b'{"OPENAI_API_KEY": "real-token", "ANTHROPIC_KEY": ""}')
    with caplog.at_level(logging.WARNING, logger="mariana.vault.runtime"):
        out = await fetch_vault_env(
            r, "task-cc09-3", requires_vault=False, redis_url=_LOCAL_URL
        )
    # The valid entry survives; the empty-value entry is dropped.
    assert out == {"OPENAI_API_KEY": "real-token"}
    # Operators get a structured warning so corruption is visible.
    matched = [
        rec for rec in caplog.records
        if "vault_env_corrupt_payload_degraded" in rec.getMessage()
        or getattr(rec, "reason", None) == "empty_value"
    ]
    assert matched, "expected at least one corruption warning"


async def test_only_empty_value_without_requires_vault_returns_empty_dict():
    """All-empty-values payload degrades to {} under requires_vault=False."""
    r = _StubRedis(b'{"FOO": "", "BAR": ""}')
    out = await fetch_vault_env(
        r, "task-cc09-4", requires_vault=False, redis_url=_LOCAL_URL
    )
    assert out == {}


# ---------------------------------------------------------------------------
# (3) _NAME_RE anchored with \Z — trailing newline rejected
# ---------------------------------------------------------------------------


def test_name_re_rejects_trailing_newline_runtime():
    """Pin: runtime._NAME_RE must use \\Z (not $)."""
    assert vault_runtime._NAME_RE.match("FOO\n") is None
    assert vault_runtime._NAME_RE.match("FOO") is not None
    assert vault_runtime._NAME_RE.match("OPENAI_API_KEY\n") is None
    assert vault_runtime._NAME_RE.match("OPENAI_API_KEY") is not None


def test_name_re_rejects_trailing_newline_store():
    """Same pin on the store-side regex (used by validate path)."""
    assert vault_store._NAME_RE.match("FOO\n") is None
    assert vault_store._NAME_RE.match("FOO") is not None


def test_name_re_pattern_uses_z_anchor_runtime():
    """Belt-and-braces: assert the pattern source ends with \\Z."""
    assert vault_runtime._NAME_RE.pattern.endswith(r"\Z")
    assert not vault_runtime._NAME_RE.pattern.endswith("$")


def test_name_re_pattern_uses_z_anchor_store():
    """Belt-and-braces: assert the pattern source ends with \\Z."""
    assert vault_store._NAME_RE.pattern.endswith(r"\Z")
    assert not vault_store._NAME_RE.pattern.endswith("$")


async def test_trailing_newline_key_with_requires_vault_raises():
    """End-to-end: a poisoned key with trailing \\n trips the kv-shape branch."""
    # JSON allows \n inside string keys when escaped; this is the exact
    # poisoned-blob shape CC-09 (2) flags.
    r = _StubRedis(b'{"FOO\\n": "value-string"}')
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-cc09-5", requires_vault=True, redis_url=_LOCAL_URL
        )
    assert "invalid_kv_shape" in str(exc.value)


async def test_trailing_newline_key_without_requires_vault_dropped(caplog):
    """requires_vault=False: legacy degrade path drops the key + warns."""
    r = _StubRedis(
        b'{"FOO\\n": "poisoned", "OPENAI_API_KEY": "real-token"}'
    )
    with caplog.at_level(logging.WARNING, logger="mariana.vault.runtime"):
        out = await fetch_vault_env(
            r, "task-cc09-6", requires_vault=False, redis_url=_LOCAL_URL
        )
    assert out == {"OPENAI_API_KEY": "real-token"}
    matched = [
        rec for rec in caplog.records
        if "vault_env_corrupt_payload_degraded" in rec.getMessage()
        or getattr(rec, "reason", None) == "invalid_kv_shape"
    ]
    assert matched, "expected at least one corruption warning"
