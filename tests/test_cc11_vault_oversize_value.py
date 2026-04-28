"""CC-11 regression — fetch_vault_env must fail-closed on oversize values.

CC-11 (P4, post-CC-09 re-audit #37) found that ``validate_vault_env`` (the
WRITE path) raises ``ValueError`` for any value longer than
``_MAX_VAULT_VALUE_LEN`` (16384), but ``fetch_vault_env`` (the READ path)
used to silently truncate with ``v[:_MAX_VAULT_VALUE_LEN]``.  Net effect:
the worker would run with a TRUNCATED secret instead of the original ---
a different value than the one ingest would have accepted, and a fresh
contract drift on the same vault surface CC-04 / CC-06 / CC-09 had been
closing.

This module pins the fix:

  * value of length ``_MAX_VAULT_VALUE_LEN + 1`` + ``requires_vault=True``
    -> raises ``VaultUnavailableError`` with reason ``oversize_value`` in
    the message; the offending key name appears too so ops can locate it,
    BUT the value bytes are not logged (we read only the length).
  * same payload + ``requires_vault=False``
    -> returns dict WITHOUT that key, plus a structured warning so ops can
    see the corruption.
  * value of EXACTLY ``_MAX_VAULT_VALUE_LEN`` (the boundary)
    -> still allowed under both modes; round-trips verbatim with no
    slicing on the read path.
  * mixed payload (valid keys + one oversize key) + ``requires_vault=True``
    -> raises ``VaultUnavailableError`` (does NOT return a partial dict
    of just the valid keys).
"""

from __future__ import annotations

import json
import logging

import pytest

from mariana.vault.runtime import (
    _MAX_VAULT_VALUE_LEN,
    VaultUnavailableError,
    fetch_vault_env,
)


# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/test_cc09_vault_contract_drift.py)
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


def _payload(env: dict[str, str]) -> bytes:
    return json.dumps(env).encode("utf-8")


# ---------------------------------------------------------------------------
# (1) Oversize value + requires_vault=True -> raises with reason
# ---------------------------------------------------------------------------


async def test_oversize_value_with_requires_vault_raises_with_reason():
    """A value of length _MAX_VAULT_VALUE_LEN + 1 must fail-closed."""
    oversize = "x" * (_MAX_VAULT_VALUE_LEN + 1)
    r = _StubRedis(_payload({"OPENAI_API_KEY": oversize}))
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-cc11-1", requires_vault=True, redis_url=_LOCAL_URL
        )
    msg = str(exc.value)
    assert "oversize_value" in msg
    # The offending key name is in the diagnostic so ops can locate it.
    assert "OPENAI_API_KEY" in msg
    # Belt-and-braces: the secret value itself must NOT be in the message.
    assert oversize not in msg


async def test_oversize_value_far_above_cap_with_requires_vault_raises():
    """Audit-mirroring repro: a 20_000-char value also fail-closes."""
    oversize = "x" * 20_000
    r = _StubRedis(_payload({"OPENAI_API_KEY": oversize}))
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-cc11-1b", requires_vault=True, redis_url=_LOCAL_URL
        )
    assert "oversize_value" in str(exc.value)


# ---------------------------------------------------------------------------
# (2) Oversize value + requires_vault=False -> dict without the key + warn
# ---------------------------------------------------------------------------


async def test_oversize_value_without_requires_vault_drops_key_and_warns(caplog):
    """Legacy semantics: degrade to dict-minus-key, with a structured warning.

    Critically, the dropped key must not be smuggled in as a TRUNCATED value
    --- that was the CC-11 contract drift.  Either the key is present with
    its full value, or it is absent entirely.
    """
    oversize = "x" * (_MAX_VAULT_VALUE_LEN + 1)
    valid = "real-token"
    r = _StubRedis(_payload({"OPENAI_API_KEY": valid, "ANTHROPIC_KEY": oversize}))
    with caplog.at_level(logging.WARNING, logger="mariana.vault.runtime"):
        out = await fetch_vault_env(
            r, "task-cc11-2", requires_vault=False, redis_url=_LOCAL_URL
        )
    # Valid entry survives verbatim; oversize entry is dropped (NOT
    # truncated --- a truncated secret value would silently appear here
    # under the old behaviour).
    assert out == {"OPENAI_API_KEY": valid}
    assert "ANTHROPIC_KEY" not in out
    # Operators get a structured warning so corruption is visible.
    matched = [
        rec
        for rec in caplog.records
        if "vault_env_corrupt_payload_degraded" in rec.getMessage()
        and getattr(rec, "reason", None) == "oversize_value"
    ]
    assert matched, "expected at least one oversize_value corruption warning"
    # The warning carries the offending key + length but not the value bytes.
    rec = matched[0]
    assert getattr(rec, "key", None) == "ANTHROPIC_KEY"
    assert getattr(rec, "length", None) == _MAX_VAULT_VALUE_LEN + 1
    # The full secret value must not appear in any logged field.
    assert oversize not in rec.getMessage()


# ---------------------------------------------------------------------------
# (3) Boundary: exactly _MAX_VAULT_VALUE_LEN is allowed
# ---------------------------------------------------------------------------


async def test_value_exactly_at_max_len_is_allowed_with_requires_vault():
    """Boundary: len == _MAX_VAULT_VALUE_LEN must round-trip verbatim."""
    boundary = "x" * _MAX_VAULT_VALUE_LEN
    r = _StubRedis(_payload({"OPENAI_API_KEY": boundary}))
    out = await fetch_vault_env(
        r, "task-cc11-3", requires_vault=True, redis_url=_LOCAL_URL
    )
    assert out == {"OPENAI_API_KEY": boundary}
    # Fetch must NOT slice --- the value's length is preserved exactly.
    assert len(out["OPENAI_API_KEY"]) == _MAX_VAULT_VALUE_LEN


async def test_value_exactly_at_max_len_is_allowed_without_requires_vault():
    """Same boundary check under the legacy mode."""
    boundary = "x" * _MAX_VAULT_VALUE_LEN
    r = _StubRedis(_payload({"OPENAI_API_KEY": boundary}))
    out = await fetch_vault_env(
        r, "task-cc11-3b", requires_vault=False, redis_url=_LOCAL_URL
    )
    assert out == {"OPENAI_API_KEY": boundary}
    assert len(out["OPENAI_API_KEY"]) == _MAX_VAULT_VALUE_LEN


# ---------------------------------------------------------------------------
# (4) Mixed payload — even one oversize key trips fail-closed (no partial)
# ---------------------------------------------------------------------------


async def test_mixed_payload_with_oversize_key_raises_no_partial_return():
    """A mixed payload with even one oversize key fail-closes wholesale.

    The previous behaviour silently truncated and returned the full dict
    with the truncated value; the new contract refuses to honour any
    partial state and refuses the entire fetch.
    """
    oversize = "x" * (_MAX_VAULT_VALUE_LEN + 1)
    payload_bytes = _payload(
        {
            "OPENAI_API_KEY": "real-openai",
            "ANTHROPIC_KEY": "real-anthropic",
            "BAD_KEY": oversize,
        }
    )
    r = _StubRedis(payload_bytes)
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-cc11-4", requires_vault=True, redis_url=_LOCAL_URL
        )
    assert "oversize_value" in str(exc.value)
    assert "BAD_KEY" in str(exc.value)


# ---------------------------------------------------------------------------
# (5) Belt-and-braces: under-cap value is no longer sliced
# ---------------------------------------------------------------------------


async def test_under_cap_value_is_not_sliced():
    """Pin: the read path now stores ``v`` verbatim, not ``v[:max]``.

    Pre-CC-11, the loop did ``out[k] = v[:_MAX_VAULT_VALUE_LEN]``, which is
    a no-op for under-cap values but masked the contract drift.  Pin the
    new shape behaviourally on a deliberately weird-but-valid string.
    """
    weird = "value-with-special-chars\t \"' \\ /"
    r = _StubRedis(_payload({"OPENAI_API_KEY": weird}))
    out = await fetch_vault_env(
        r, "task-cc11-5", requires_vault=True, redis_url=_LOCAL_URL
    )
    assert out == {"OPENAI_API_KEY": weird}
    assert out["OPENAI_API_KEY"] is not None
    assert len(out["OPENAI_API_KEY"]) == len(weird)
