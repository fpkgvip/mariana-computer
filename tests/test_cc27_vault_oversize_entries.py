"""CC-27 regression — fetch_vault_env must fail-closed on oversize entry counts.

CC-27 (P2, post-CC-26 re-audit #44 Finding 1) found a sibling of the same
contract-drift class CC-11 closed: ``validate_vault_env`` (the WRITE path)
raises ``ValueError`` for any payload with more than ``_MAX_VAULT_ENV_ENTRIES``
(=50) keys, but ``fetch_vault_env`` (the READ path) used to silently slice
``list(data.items())[:_MAX_VAULT_ENV_ENTRIES]`` --- honouring the first 50
keys and silently dropping the rest.  Net effect: a poisoned blob with 51+
keys would let the worker run with a half-honoured env.

This module pins the fix:

  * payload with 51 keys + ``requires_vault=True``
    -> raises ``VaultUnavailableError`` with reason ``oversize_entries``
  * same payload + ``requires_vault=False``
    -> returns ``{}`` (legacy soft-fail) plus a structured warning
  * payload with EXACTLY 50 keys (the boundary)
    -> all returned under both modes
  * payload with 49 keys
    -> all returned under both modes
"""

from __future__ import annotations

import json
import logging

import pytest

from mariana.vault.runtime import (
    _MAX_VAULT_ENV_ENTRIES,
    VaultUnavailableError,
    fetch_vault_env,
)


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


def _make_env(n: int) -> dict[str, str]:
    """Build a dict of `n` valid VAULT keys with non-empty values."""
    return {f"K{i:03d}": f"v{i}" for i in range(n)}


# ---------------------------------------------------------------------------
# (1) Oversize entry count + requires_vault=True -> raises with reason
# ---------------------------------------------------------------------------


async def test_oversize_entries_with_requires_vault_raises_with_reason():
    """A 51-key payload must fail-closed under requires_vault=True."""
    env = _make_env(_MAX_VAULT_ENV_ENTRIES + 1)
    assert len(env) == _MAX_VAULT_ENV_ENTRIES + 1
    r = _StubRedis(_payload(env))
    with pytest.raises(VaultUnavailableError) as exc:
        await fetch_vault_env(
            r, "task-cc27-1", requires_vault=True, redis_url=_LOCAL_URL
        )
    msg = str(exc.value)
    assert "oversize_entries" in msg
    assert f"count={_MAX_VAULT_ENV_ENTRIES + 1}" in msg
    assert f"max={_MAX_VAULT_ENV_ENTRIES}" in msg


# ---------------------------------------------------------------------------
# (2) Oversize entry count + requires_vault=False -> returns {} + warns
# ---------------------------------------------------------------------------


async def test_oversize_entries_without_requires_vault_returns_empty_and_warns(caplog):
    """Legacy mode: degrade to ``{}`` rather than honour a partial first-50."""
    env = _make_env(_MAX_VAULT_ENV_ENTRIES + 1)
    r = _StubRedis(_payload(env))
    with caplog.at_level(logging.WARNING, logger="mariana.vault.runtime"):
        out = await fetch_vault_env(
            r, "task-cc27-2", requires_vault=False, redis_url=_LOCAL_URL
        )
    # Critical: returns ``{}`` rather than the first 50 keys.
    assert out == {}
    matched = [
        rec
        for rec in caplog.records
        if "vault_env_corrupt_payload_degraded" in rec.getMessage()
        and getattr(rec, "reason", None) == "oversize_entries"
    ]
    assert matched, "expected at least one oversize_entries corruption warning"
    rec = matched[0]
    assert getattr(rec, "count", None) == _MAX_VAULT_ENV_ENTRIES + 1
    assert getattr(rec, "max", None) == _MAX_VAULT_ENV_ENTRIES


# ---------------------------------------------------------------------------
# (3) Boundary: exactly _MAX_VAULT_ENV_ENTRIES keys is allowed
# ---------------------------------------------------------------------------


async def test_entries_exactly_at_max_count_is_allowed_with_requires_vault():
    """Boundary: len == _MAX_VAULT_ENV_ENTRIES must round-trip wholesale."""
    env = _make_env(_MAX_VAULT_ENV_ENTRIES)
    r = _StubRedis(_payload(env))
    out = await fetch_vault_env(
        r, "task-cc27-3", requires_vault=True, redis_url=_LOCAL_URL
    )
    assert out == env
    assert len(out) == _MAX_VAULT_ENV_ENTRIES


async def test_entries_exactly_at_max_count_is_allowed_without_requires_vault():
    """Same boundary check under the legacy mode."""
    env = _make_env(_MAX_VAULT_ENV_ENTRIES)
    r = _StubRedis(_payload(env))
    out = await fetch_vault_env(
        r, "task-cc27-3b", requires_vault=False, redis_url=_LOCAL_URL
    )
    assert out == env
    assert len(out) == _MAX_VAULT_ENV_ENTRIES


# ---------------------------------------------------------------------------
# (4) Under-cap: 49 keys returns wholesale
# ---------------------------------------------------------------------------


async def test_entries_under_cap_returns_all():
    """A 49-key payload survives both modes intact."""
    env = _make_env(_MAX_VAULT_ENV_ENTRIES - 1)
    r = _StubRedis(_payload(env))
    out_strict = await fetch_vault_env(
        r, "task-cc27-4", requires_vault=True, redis_url=_LOCAL_URL
    )
    assert out_strict == env
    out_loose = await fetch_vault_env(
        r, "task-cc27-4b", requires_vault=False, redis_url=_LOCAL_URL
    )
    assert out_loose == env
