"""Vault runtime: validation, contextvars, payload redaction, Redis IO.

These are pure-python tests that exercise the module without spinning up
the FastAPI app.  Async tests use ``asyncio.run`` to keep the surface
small and stay compatible with the existing pytest config.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from mariana.vault.runtime import (
    REDIS_KEY_FMT,
    clear_vault_env,
    fetch_vault_env,
    get_task_env,
    redact_payload,
    set_task_context,
    store_vault_env,
    validate_vault_env,
)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_vault_env_accepts_valid_grammar():
    out = validate_vault_env({"OPENAI_API_KEY": "sk-aaaaaaaaaaaa"})
    assert out == {"OPENAI_API_KEY": "sk-aaaaaaaaaaaa"}


def test_validate_vault_env_drops_empty_values():
    out = validate_vault_env({"FOO": ""})
    assert out == {}


def test_validate_vault_env_rejects_lowercase_name():
    with pytest.raises(ValueError):
        validate_vault_env({"openai_api_key": "sk-xxxx"})


def test_validate_vault_env_rejects_starts_with_digit():
    with pytest.raises(ValueError):
        validate_vault_env({"1FOO": "sk-xxxx"})


def test_validate_vault_env_rejects_dash():
    with pytest.raises(ValueError):
        validate_vault_env({"FOO-BAR": "sk-xxxx"})


def test_validate_vault_env_rejects_oversize_value():
    with pytest.raises(ValueError):
        validate_vault_env({"FOO": "x" * 16_385})


def test_validate_vault_env_rejects_too_many_entries():
    env = {f"K{i:03d}".replace("K", "K_"): "verysecretvalue1" for i in range(60)}
    # the regex allows trailing digits/underscores but make 60 distinct names
    env = {f"K_{i:03d}": "verysecretvalue1" for i in range(60)}
    with pytest.raises(ValueError):
        validate_vault_env(env)


def test_validate_vault_env_empty_in_empty_out():
    assert validate_vault_env({}) == {}
    assert validate_vault_env(None or {}) == {}


# ---------------------------------------------------------------------------
# Context vars + redaction
# ---------------------------------------------------------------------------


def test_set_task_context_isolation():
    env_a = {"A_KEY": "supersecretA1234"}
    env_b = {"B_KEY": "supersecretB1234"}

    h = set_task_context(env_a)
    try:
        assert get_task_env() == env_a
        # Nested context overrides
        h2 = set_task_context(env_b)
        try:
            assert get_task_env() == env_b
        finally:
            h2.reset()
        assert get_task_env() == env_a
    finally:
        h.reset()
    # After reset we're back to identity
    assert get_task_env() == {}


def test_redact_payload_walks_nested_structures():
    env = {"OPENAI_API_KEY": "sk-abcdefghij1234"}
    h = set_task_context(env)
    try:
        payload = {
            "stdout": "the key is sk-abcdefghij1234, ok",
            "nested": {
                "inner": ["sk-abcdefghij1234", "no secret here"],
                "stderr": "leak: sk-abcdefghij1234 (boom)",
            },
            "exit_code": 0,
            "duration_ms": 12,
        }
        red = redact_payload(payload)
        text = json.dumps(red)
        assert "sk-abcdefghij1234" not in text
        assert "[REDACTED:OPENAI_API_KEY]" in text
        # Non-string types pass through.
        assert red["exit_code"] == 0
        assert red["duration_ms"] == 12
    finally:
        h.reset()


def test_redact_payload_no_secrets_is_identity():
    h = set_task_context({})
    try:
        original = {"stdout": "hello world", "n": 5}
        out = redact_payload(original)
        assert out == original
    finally:
        h.reset()


def test_redact_short_token_not_redacted():
    # Tokens shorter than 8 chars are skipped to avoid false positives.
    env = {"X": "short"}
    h = set_task_context(env)
    try:
        out = redact_payload({"s": "I said short, ok"})
        assert out == {"s": "I said short, ok"}
    finally:
        h.reset()


# ---------------------------------------------------------------------------
# Redis IO (with a tiny in-memory fake)
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, k, v, ex=None):
        self.store[k] = v
        if ex:
            self.ttls[k] = ex

    async def get(self, k):
        return self.store.get(k)

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)
            self.ttls.pop(k, None)


def test_store_and_fetch_round_trip():
    async def _go():
        r = _FakeRedis()
        await store_vault_env(r, "task-1", {"KEY_A": "valuethatislongenough"}, ttl_seconds=900)
        # TTL was bounded to >= floor (600) but we passed 900 so it stays 900.
        assert r.ttls[REDIS_KEY_FMT.format(task_id="task-1")] == 900
        out = await fetch_vault_env(r, "task-1")
        assert out == {"KEY_A": "valuethatislongenough"}
        await clear_vault_env(r, "task-1")
        assert REDIS_KEY_FMT.format(task_id="task-1") not in r.store

    asyncio.run(_go())


def test_fetch_vault_env_handles_missing():
    async def _go():
        r = _FakeRedis()
        out = await fetch_vault_env(r, "nope")
        assert out == {}

    asyncio.run(_go())


def test_fetch_vault_env_filters_invalid_names():
    async def _go():
        r = _FakeRedis()
        # Inject something the API would never write but defend anyway.
        r.store[REDIS_KEY_FMT.format(task_id="t2")] = json.dumps({
            "GOOD_NAME": "valid_value_here_long_enough",
            "bad-name": "should-be-dropped",
            "9STARTS_WITH_DIGIT": "drop-me",
        })
        out = await fetch_vault_env(r, "t2")
        assert out == {"GOOD_NAME": "valid_value_here_long_enough"}

    asyncio.run(_go())


def test_store_vault_env_no_redis_is_noop():
    """Empty vault on a None redis is a no-op (no client needed).

    U-03 fix: when env is *non-empty* + redis is None we fail closed
    (covered in tests/test_u03_vault_redis_safety.py).  This test pins
    the back-compat path: an empty env never touches Redis.
    """
    async def _go():
        # Empty env on None redis: no-op, no raise.
        await store_vault_env(None, "t", {}, ttl_seconds=600)
        # Fetch with the legacy (requires_vault=False default) returns {}.
        out = await fetch_vault_env(None, "t")
        assert out == {}

    asyncio.run(_go())
