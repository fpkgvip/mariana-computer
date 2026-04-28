"""CC-04 regression — vault malformed payload fail-closed bypass.

CC-04 (P2, Phase D re-audit #35) found that
``fetch_vault_env(..., requires_vault=True)`` correctly raised
``VaultUnavailableError`` on Redis miss / transport failure but silently
returned ``{}`` when the persisted payload itself was corrupted:

  1. JSON that does not decode (e.g. ``b'{'``)
  2. JSON whose top-level value is not an object
     (e.g. ``b'[]'``, ``b'"just-a-string"'``, ``b'42'``)
  3. JSON object whose keys / values are not strings, or whose keys do
     not match the ``_NAME_RE`` env-var grammar

That re-opened the exact U-03 fail-closed surface: a task created with
``requires_vault=True`` would silently run with **no** injected secrets
when the Redis blob was corrupted or poisoned.

This module pins the fix:

  * malformed JSON       + ``requires_vault=True``  -> ``VaultUnavailableError``
  * non-object JSON      + ``requires_vault=True``  -> ``VaultUnavailableError``
  * invalid kv shapes    + ``requires_vault=True``  -> ``VaultUnavailableError``
  * each error message carries a distinct reason code
    (``malformed_payload`` / ``non_object_payload`` / ``invalid_kv_shape``)
  * ``requires_vault=False`` keeps the legacy degrade-to-``{}`` behaviour
    (back-compat for tasks that never asked for a vault) but still emits
    a ``warning`` so ops can see the corruption.
"""

from __future__ import annotations

import logging

import pytest

from mariana.vault.runtime import (
    VaultUnavailableError,
    fetch_vault_env,
)


# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/test_u03_vault_redis_safety.py style)
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
# (1) requires_vault=True  -> fail-closed on every corrupt-payload shape
# ---------------------------------------------------------------------------


async def test_malformed_json_with_requires_vault_raises():
    """``b'{'`` is not valid JSON; must raise, not return ``{}``."""
    r = _StubRedis(b"{")
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-1", requires_vault=True, redis_url=_LOCAL_URL
        )
    assert "malformed_payload" in str(exc.value)
    assert r.get_calls == 1


async def test_list_payload_with_requires_vault_raises():
    """``b'[]'`` is valid JSON but not a dict; must raise."""
    r = _StubRedis(b"[]")
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-2", requires_vault=True, redis_url=_LOCAL_URL
        )
    assert "non_object_payload" in str(exc.value)


async def test_string_payload_with_requires_vault_raises():
    """Top-level JSON string must raise under ``requires_vault=True``."""
    r = _StubRedis(b'"just-a-string"')
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-3", requires_vault=True, redis_url=_LOCAL_URL
        )
    assert "non_object_payload" in str(exc.value)


async def test_number_payload_with_requires_vault_raises():
    """Top-level JSON number is also a non-object payload — must raise."""
    r = _StubRedis(b"42")
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-4", requires_vault=True, redis_url=_LOCAL_URL
        )
    assert "non_object_payload" in str(exc.value)


async def test_non_string_value_inside_dict_with_requires_vault_raises():
    """Dict with a non-string value must raise — half-honoured env is unsafe."""
    r = _StubRedis(b'{"OPENAI_API_KEY": 12345}')
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-5", requires_vault=True, redis_url=_LOCAL_URL
        )
    assert "invalid_kv_shape" in str(exc.value)


async def test_invalid_key_grammar_with_requires_vault_raises():
    """Key that does not match the env-var grammar must raise."""
    r = _StubRedis(b'{"lowercase_bad": "value-string-is-fine"}')
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-6", requires_vault=True, redis_url=_LOCAL_URL
        )
    assert "invalid_kv_shape" in str(exc.value)


async def test_value_list_inside_dict_with_requires_vault_raises():
    """Value-shape guard catches non-string values too."""
    r = _StubRedis(b'{"OPENAI_API_KEY": ["list", "not", "string"]}')
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-7", requires_vault=True, redis_url=_LOCAL_URL
        )
    assert "invalid_kv_shape" in str(exc.value)


# ---------------------------------------------------------------------------
# (2) requires_vault=False  -> legacy degrade-to-{} but emits a warning
# ---------------------------------------------------------------------------


async def test_malformed_json_without_requires_vault_returns_empty_and_warns(caplog):
    """``b'{'`` + ``requires_vault=False`` -> ``{}`` + warning logged."""
    r = _StubRedis(b"{")
    with caplog.at_level(logging.WARNING, logger="mariana.vault.runtime"):
        out = await fetch_vault_env(r, "task-a", requires_vault=False)
    assert out == {}
    # Warning must mention the malformed_payload reason so ops can
    # alert separately from transport failures.
    reasons = [str(getattr(rec, "reason", "")) for rec in caplog.records]
    assert "malformed_payload" in reasons


async def test_list_payload_without_requires_vault_returns_empty_and_warns(caplog):
    """``b'[]'`` + ``requires_vault=False`` -> ``{}`` + warning logged."""
    r = _StubRedis(b"[]")
    with caplog.at_level(logging.WARNING, logger="mariana.vault.runtime"):
        out = await fetch_vault_env(r, "task-b", requires_vault=False)
    assert out == {}
    reasons = [str(getattr(rec, "reason", "")) for rec in caplog.records]
    assert "non_object_payload" in reasons


async def test_string_payload_without_requires_vault_returns_empty_and_warns(caplog):
    """Top-level JSON string + ``requires_vault=False`` -> ``{}`` + warning."""
    r = _StubRedis(b'"just-a-string"')
    with caplog.at_level(logging.WARNING, logger="mariana.vault.runtime"):
        out = await fetch_vault_env(r, "task-c", requires_vault=False)
    assert out == {}
    reasons = [str(getattr(rec, "reason", "")) for rec in caplog.records]
    assert "non_object_payload" in reasons


async def test_invalid_kv_shape_without_requires_vault_drops_entry_and_warns(caplog):
    """Dict with bad kv shape + ``requires_vault=False`` -> drop bad entry,
    keep good ones, and emit a warning so ops see the corruption."""
    # First entry is invalid (non-string value); second is valid.
    r = _StubRedis(b'{"OPENAI_API_KEY": 12345, "GOOD_KEY": "good-value"}')
    with caplog.at_level(logging.WARNING, logger="mariana.vault.runtime"):
        out = await fetch_vault_env(r, "task-d", requires_vault=False)
    # Bad entry stripped, good entry preserved.
    assert out == {"GOOD_KEY": "good-value"}
    reasons = [str(getattr(rec, "reason", "")) for rec in caplog.records]
    assert "invalid_kv_shape" in reasons


# ---------------------------------------------------------------------------
# (3) Sanity: a well-formed payload still round-trips under both modes
# ---------------------------------------------------------------------------


async def test_well_formed_payload_round_trips_under_requires_vault():
    r = _StubRedis(b'{"OPENAI_API_KEY": "sk-aaaaaaaaaaaa"}')
    out = await fetch_vault_env(
        r, "task-ok", requires_vault=True, redis_url=_LOCAL_URL
    )
    assert out == {"OPENAI_API_KEY": "sk-aaaaaaaaaaaa"}


async def test_well_formed_payload_round_trips_without_requires_vault():
    r = _StubRedis(b'{"OPENAI_API_KEY": "sk-aaaaaaaaaaaa"}')
    out = await fetch_vault_env(r, "task-ok", requires_vault=False)
    assert out == {"OPENAI_API_KEY": "sk-aaaaaaaaaaaa"}
