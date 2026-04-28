"""CC-06 regression — empty-dict vault payload bypass under ``requires_vault=True``.

CC-06 (P2, Phase D re-audit #36, source `loop6_audit/A41_post_cc05_reaudit.md`)
found that the CC-04 fail-closed contract still had one escape: a literally
empty JSON object (``b'{}'`` or any whitespace equivalent) deserialised to
``{}`` and returned silently because:

  * ``not raw`` was False (the bytes ``b'{}'`` are non-empty).
  * ``json.loads('{}')`` returned ``{}`` (no decode error).
  * ``isinstance({}, dict)`` was True (passes the shape branch).
  * The kv-shape for-loop ran **zero iterations** (no entries to fail on).

So ``out`` remained the freshly-allocated ``{}`` and was returned.  A task
created with ``requires_vault=True`` then ran with empty env — the exact U-03
fail-closed surface CC-04 was meant to close.

This module pins the additional fix:

  * ``b'{}'`` + ``requires_vault=True`` → ``VaultUnavailableError``
    with ``empty_payload`` in the message.
  * Whitespace-only object ``b'{ }'`` / ``b'  {}\\n'`` + ``requires_vault=True``
    → same fail-closed behaviour.
  * ``b'{}'`` + ``requires_vault=False`` → legacy ``{}`` (legitimate
    "no vaulted secrets" state).
  * Nested empty ``b'{"x": {}}'`` is **not** an empty top-level dict; it is
    caught by the existing CC-04 ``invalid_kv_shape`` branch (because the
    value ``{}`` is not a string) — documented here so the contract is clear.
"""

from __future__ import annotations

import pytest

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
# (1) requires_vault=True  -> fail-closed on every empty-object payload shape
# ---------------------------------------------------------------------------


async def test_empty_dict_payload_with_requires_vault_raises():
    """``b'{}'`` is the canonical CC-06 bypass; must raise, not return ``{}``."""
    r = _StubRedis(b"{}")
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-cc06-1", requires_vault=True, redis_url=_LOCAL_URL
        )
    assert "empty_payload" in str(exc.value)
    assert r.get_calls == 1


async def test_whitespace_inside_empty_dict_with_requires_vault_raises():
    """``b'{ }'`` deserialises to ``{}`` — same fail-closed branch."""
    r = _StubRedis(b"{ }")
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-cc06-2", requires_vault=True, redis_url=_LOCAL_URL
        )
    assert "empty_payload" in str(exc.value)


async def test_padded_empty_dict_with_requires_vault_raises():
    """``b'  {}\\n'`` (leading/trailing whitespace around ``{}``) — same."""
    r = _StubRedis(b"  {}\n")
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-cc06-3", requires_vault=True, redis_url=_LOCAL_URL
        )
    assert "empty_payload" in str(exc.value)


# ---------------------------------------------------------------------------
# (2) requires_vault=False  -> empty dict is the legitimate "no secrets" state
# ---------------------------------------------------------------------------


async def test_empty_dict_payload_without_requires_vault_returns_empty():
    """``b'{}'`` + ``requires_vault=False`` -> ``{}`` (legacy / legitimate).

    A task that did not register any vault_env can legitimately observe a
    stored ``{}`` blob; this is not a corruption signal.  No raise.
    """
    r = _StubRedis(b"{}")
    out = await fetch_vault_env(r, "task-cc06-4", requires_vault=False)
    assert out == {}


async def test_whitespace_empty_dict_without_requires_vault_returns_empty():
    """``b'{ }'`` + ``requires_vault=False`` -> ``{}`` (same as ``b'{}'``)."""
    r = _StubRedis(b"{ }")
    out = await fetch_vault_env(r, "task-cc06-5", requires_vault=False)
    assert out == {}


# ---------------------------------------------------------------------------
# (3) Nested empty dict — falls under CC-04 invalid_kv_shape, NOT CC-06
# ---------------------------------------------------------------------------


async def test_nested_empty_dict_falls_under_cc04_invalid_kv_shape():
    """``b'{"x": {}}'`` is NOT an empty top-level dict.

    The top-level dict has one entry ``("x", {})`` — the kv-shape guard at
    runtime.py:254 rejects this because the *value* ``{}`` is not a string.
    Under ``requires_vault=True`` the error reason is ``invalid_kv_shape``,
    NOT ``empty_payload``.  Documented here so the contract between CC-04
    and CC-06 stays unambiguous.

    (Side note: the key ``"x"`` is also lower-case and would fail
    ``_NAME_RE`` regardless, but the kv-shape branch short-circuits on the
    type check first.)
    """
    r = _StubRedis(b'{"x": {}}')
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-cc06-6", requires_vault=True, redis_url=_LOCAL_URL
        )
    msg = str(exc.value)
    assert "invalid_kv_shape" in msg
    assert "empty_payload" not in msg


# ---------------------------------------------------------------------------
# (4) Sanity: a well-formed payload still round-trips under both modes
# ---------------------------------------------------------------------------


async def test_well_formed_payload_still_round_trips_under_requires_vault():
    """CC-06 fix must not regress the happy path under ``requires_vault=True``."""
    r = _StubRedis(b'{"OPENAI_API_KEY": "sk-aaaaaaaaaaaa"}')
    out = await fetch_vault_env(
        r, "task-cc06-ok-1", requires_vault=True, redis_url=_LOCAL_URL
    )
    assert out == {"OPENAI_API_KEY": "sk-aaaaaaaaaaaa"}


async def test_well_formed_payload_still_round_trips_without_requires_vault():
    """CC-06 fix must not regress the happy path under ``requires_vault=False``."""
    r = _StubRedis(b'{"OPENAI_API_KEY": "sk-aaaaaaaaaaaa"}')
    out = await fetch_vault_env(r, "task-cc06-ok-2", requires_vault=False)
    assert out == {"OPENAI_API_KEY": "sk-aaaaaaaaaaaa"}
