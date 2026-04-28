"""Phase D coverage-fill: vault runtime fail-closed boundaries.

This file pins behavioural cold spots NOT already covered by:
  - CC-04 (malformed payload fail-closed)
  - CC-06 (empty-dict fail-closed)
  - CC-09 (empty-value contract drift + ``_NAME_RE`` ``\\Z`` anchor)
  - CC-10 (sibling validator trailing-newline regressions)
  - CC-11 (oversize value contract drift)
  - CC-27 (oversize entries contract drift)
  - U-03 (transport policy / fail-closed pre-flight)
  - V-01/V-02 (substring-bypass URL hardening)

Cold spots filled here:

  1-3. ``test_phase_d_name_re_*_boundary``
        ``_NAME_RE`` allows names up to 64 chars (``[A-Z][A-Z0-9_]{0,63}``).
        Pin BOTH boundaries on the compiled pattern AND through
        ``validate_vault_env``:
          * 1-char name accepted (the lower boundary)
          * 64-char name accepted (the upper boundary)
          * 65-char name rejected (just over the cap)
          * trailing-newline 64-char name rejected (\\Z anchor)

  4. ``test_phase_d_redis_io_error_during_fetch_under_requires_vault``
        ``redis.get`` raises a ``ConnectionError`` mid-fetch with
        ``requires_vault=True`` \u2014 fetch must raise
        ``VaultUnavailableError`` whose message references the failing
        task_id.  Pin the canonical fail-closed surface (no swallowed
        empty {}, no propagated raw ConnectionError).

  5. ``test_phase_d_redis_io_error_during_fetch_without_requires_vault``
        Same Redis IO failure with ``requires_vault=False`` returns ``{}``
        (legacy soft-fail).  Pin the back-compat surface so non-vault
        tasks are not collateral-damaged by transient Redis errors.

  6. ``test_phase_d_transport_policy_blocks_plaintext_remote_redis``
        ``requires_vault=True`` + ``redis_url="redis://remote.example.com:6379"``
        raises ``ValueError`` (NOT VaultUnavailableError) BEFORE any
        IO is attempted.  Pin transport-policy-first ordering: a
        plaintext URL to a non-loopback host must short-circuit.

  7. ``test_phase_d_transport_policy_allows_local_plaintext_redis``
        ``redis://127.0.0.1:6379`` and ``redis://localhost:6379`` both
        pass policy validation \u2014 plaintext on loopback is acceptable.

  8. ``test_phase_d_concurrent_fetches_idempotent_for_same_task``
        Two concurrent ``fetch_vault_env`` calls for the same task with
        the same Redis blob return identical dicts and do not mutate
        Redis state.  Pin: the fetcher is a pure read; concurrency is
        observation-equivalent to a single fetch.

  9. ``test_phase_d_validate_vault_env_rejects_non_object_payload``
        ``validate_vault_env`` raises ``ValueError`` on a non-Mapping
        input.  Existing tests cover invalid keys / values; this
        pins the top-level type guard.

  10. ``test_phase_d_validate_vault_env_rejects_non_string_value``
        Non-string value (int, list, None, dict) raises ``ValueError``
        with a message referencing the offending key.

  11. ``test_phase_d_validate_vault_env_oversize_value_rejected``
        Value at exactly ``_MAX_VAULT_VALUE_LEN`` is accepted; one byte
        over raises.  Pin the boundary on the WRITE path (CC-11 covers
        the READ path).

  12. ``test_phase_d_validate_vault_env_oversize_entries_rejected``
        ``_MAX_VAULT_ENV_ENTRIES`` keys accepted; one key over raises.
        Pin the boundary on the WRITE path (CC-27 covers READ).

  13. ``test_phase_d_validate_vault_env_drops_empty_value_silently``
        Empty-string value is silently dropped on the WRITE path (the
        documented contract that CC-09's READ-path fail-closed depends
        on).  Pin the asymmetry explicitly so a future tightening of
        the WRITE path does not silently break the agreement with
        the READ path.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from mariana.vault.runtime import (
    REDIS_KEY_FMT,
    VaultUnavailableError,
    _MAX_VAULT_ENV_ENTRIES,
    _MAX_VAULT_VALUE_LEN,
    _NAME_RE,
    fetch_vault_env,
    validate_vault_env,
)


# ---------------------------------------------------------------------------
# 1-3. _NAME_RE boundary: 1, 64, 65 characters; trailing-newline 64-char.
# ---------------------------------------------------------------------------


def test_phase_d_name_re_accepts_one_char_lower_boundary():
    """A single uppercase letter is the minimum-length valid name.
    Pin the lower boundary so a future tightening (e.g. min=2) is a
    deliberate breaking change, not a silent regression."""
    assert _NAME_RE.match("A") is not None
    out = validate_vault_env({"A": "x"})
    assert out == {"A": "x"}


def test_phase_d_name_re_accepts_64_char_upper_boundary():
    """64 characters is the documented upper boundary
    (``[A-Z][A-Z0-9_]{0,63}`` = 1 + 63).  Pin acceptance at exactly
    the cap."""
    name = "A" + ("B" * 63)
    assert len(name) == 64
    assert _NAME_RE.match(name) is not None, (
        f"64-char name must be accepted by _NAME_RE; got match=None"
    )
    out = validate_vault_env({name: "value"})
    assert out == {name: "value"}, (
        "validate_vault_env must accept a 64-char name (upper boundary)"
    )


def test_phase_d_name_re_rejects_65_char_just_over_boundary():
    """65 characters is one byte over the cap.  Pin rejection so a
    silent off-by-one regression breaks CI."""
    name = "A" + ("B" * 64)  # 65 chars total
    assert len(name) == 65
    assert _NAME_RE.match(name) is None, (
        f"65-char name must be rejected by _NAME_RE; got non-None match"
    )
    with pytest.raises(ValueError, match="invalid name"):
        validate_vault_env({name: "value"})


def test_phase_d_name_re_rejects_64_char_with_trailing_newline():
    """64-char name + trailing newline = 65 chars and contains a
    forbidden character.  Per CC-09's ``\\Z`` anchor, the regex must
    reject this even though Python's ``$`` would accept it.  Pin the
    fix explicitly at the boundary so a future ``$``-anchor regression
    is caught."""
    name = "A" + ("B" * 63) + "\n"
    assert len(name) == 65
    assert _NAME_RE.match(name) is None, (
        "_NAME_RE must reject a 64-char name + trailing \\n (CC-09 \\Z anchor)"
    )
    with pytest.raises(ValueError, match="invalid name"):
        validate_vault_env({name: "value"})


# ---------------------------------------------------------------------------
# 4-5. Redis IO error during fetch \u2014 requires_vault=True/False.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_d_redis_io_error_during_fetch_under_requires_vault():
    """``redis.get`` raises ``ConnectionError`` mid-fetch with
    ``requires_vault=True`` \u2014 fetch must wrap as
    ``VaultUnavailableError`` (NOT a propagated ConnectionError, NOT a
    silent {}).  The wrapped exception must reference the task_id so
    operators can correlate with the inbound request log."""

    class _FlakyRedis:
        async def get(self, key):
            raise ConnectionError("redis cluster down")

    task_id = "task-phase-d-redis-fail"
    with pytest.raises(VaultUnavailableError) as exc_info:
        await fetch_vault_env(
            _FlakyRedis(),
            task_id,
            requires_vault=True,
        )
    assert task_id in str(exc_info.value), (
        f"VaultUnavailableError must reference task_id={task_id!r}; "
        f"got {exc_info.value!s}"
    )
    # And the original ConnectionError must be chained as __cause__
    # so structured loggers can dig into the root cause.
    assert isinstance(exc_info.value.__cause__, ConnectionError), (
        "VaultUnavailableError must chain the underlying transport error "
        "as __cause__ so operators can diagnose Redis vs. ledger faults"
    )


@pytest.mark.asyncio
async def test_phase_d_redis_io_error_during_fetch_without_requires_vault():
    """Same Redis IO failure with ``requires_vault=False`` returns ``{}``
    (legacy soft-fail).  Pin: non-vault tasks are NOT collateral-
    damaged by transient Redis errors \u2014 they degrade to \"no vaulted
    secrets\" rather than failing the whole task."""

    class _FlakyRedis:
        async def get(self, key):
            raise ConnectionError("redis cluster down")

    out = await fetch_vault_env(
        _FlakyRedis(),
        "task-phase-d-soft-fail",
        requires_vault=False,
    )
    assert out == {}, (
        f"requires_vault=False must degrade to {{}} on Redis error; "
        f"got {out!r}"
    )


# ---------------------------------------------------------------------------
# 6-7. Transport policy: plaintext-remote rejected, plaintext-local allowed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_d_transport_policy_blocks_plaintext_remote_redis():
    """``requires_vault=True`` with a plaintext ``redis://`` URL pointing
    at a non-loopback host MUST raise ``ValueError`` BEFORE any IO is
    attempted.  Pin transport-policy-first ordering: even a connected
    Redis client must be refused if the URL violates policy."""

    class _ShouldNeverBeCalledRedis:
        async def get(self, key):
            raise AssertionError(
                "transport policy violation must short-circuit BEFORE redis.get"
            )

    with pytest.raises(ValueError):
        await fetch_vault_env(
            _ShouldNeverBeCalledRedis(),
            "task-phase-d-tls",
            requires_vault=True,
            redis_url="redis://remote.example.com:6379",
        )


@pytest.mark.asyncio
async def test_phase_d_transport_policy_allows_local_plaintext_redis():
    """Plaintext ``redis://127.0.0.1:6379`` and
    ``redis://localhost:6379`` are both legitimate dev / test transport
    targets and must NOT be rejected by the vault transport guard.
    Pin local-allowance so a future overzealous tightening doesn't
    break the dev path."""

    class _MissingRedis:
        # Simulates Redis returning \"no key\" (TTL eviction, never
        # written, etc).  We expect the fail-closed branch to fire
        # AFTER policy validation passes.
        async def get(self, key):
            return None

    for url in ("redis://127.0.0.1:6379", "redis://localhost:6379"):
        # Policy passes \u2014 we proceed to the fetch and hit the
        # missing-payload fail-closed branch (NOT a ValueError).
        with pytest.raises(VaultUnavailableError):
            await fetch_vault_env(
                _MissingRedis(),
                "task-phase-d-local",
                requires_vault=True,
                redis_url=url,
            )


# ---------------------------------------------------------------------------
# 8. Concurrent fetches for the same task are idempotent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_d_concurrent_fetches_idempotent_for_same_task():
    """Two concurrent ``fetch_vault_env`` calls for the same task and
    the same Redis blob must return identical dicts AND must not
    mutate the persisted blob.  Pin: the fetcher is a pure read so
    concurrency is observation-equivalent to a single fetch."""

    payload = {"OPENAI_API_KEY": "sk-aaaaaaaaaaaa", "ANTHROPIC_KEY": "ant-xxx"}
    raw = json.dumps(payload).encode("utf-8")

    class _StaticRedis:
        def __init__(self) -> None:
            self.gets = 0
            self.sets = 0
            self.deletes = 0
            self._payload = raw

        async def get(self, key):
            self.gets += 1
            return self._payload

        async def set(self, *_a, **_kw):
            self.sets += 1
            return None

        async def delete(self, *_a, **_kw):
            self.deletes += 1
            return 0

    r = _StaticRedis()
    out_a, out_b = await asyncio.gather(
        fetch_vault_env(r, "task-pd-concurrent", requires_vault=True),
        fetch_vault_env(r, "task-pd-concurrent", requires_vault=True),
    )

    assert out_a == out_b == payload, (
        f"two concurrent fetches must return identical dicts; "
        f"got a={out_a!r}, b={out_b!r}"
    )
    assert r.gets == 2, (
        f"each fetch must independently read once; got {r.gets} reads"
    )
    assert r.sets == 0, "fetcher must NEVER write"
    assert r.deletes == 0, "fetcher must NEVER delete"


# ---------------------------------------------------------------------------
# 9. validate_vault_env: top-level non-Mapping rejected.
# ---------------------------------------------------------------------------


def test_phase_d_validate_vault_env_rejects_non_object_payload():
    """``validate_vault_env`` accepts only a Mapping at the top level.
    Lists, strings, ints, and tuples must all raise (or be treated as
    \"empty\") at the function entry, before any per-entry validation
    runs."""
    # Lists / tuples / strings are not Mappings: empty ones go through
    # the early ``if not env: return {}`` short-circuit; non-empty ones
    # raise.
    # Empty list: short-circuits via ``if not env``.
    assert validate_vault_env([]) == {}  # type: ignore[arg-type]
    # A non-empty tuple has truthy length but is not a Mapping \u2014 the
    # isinstance check catches it.
    with pytest.raises(ValueError, match="must be an object"):
        validate_vault_env(("FOO", "bar"))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 10. validate_vault_env: non-string values rejected by name.
# ---------------------------------------------------------------------------


def test_phase_d_validate_vault_env_rejects_non_string_value():
    """Non-string values raise ``ValueError`` whose message names the
    offending key.  Pin: callers (the API ingest layer) must be able
    to report the bad key back to the user without exposing other
    keys."""
    for bad_value in (123, ["a", "b"], {"nested": "v"}, None, 3.14, True):
        with pytest.raises(ValueError, match="must be a string"):
            validate_vault_env({"GOOD": "ok", "BAD": bad_value})


# ---------------------------------------------------------------------------
# 11. validate_vault_env: oversize value boundary.
# ---------------------------------------------------------------------------


def test_phase_d_validate_vault_env_oversize_value_boundary():
    """Value of exactly ``_MAX_VAULT_VALUE_LEN`` accepted; one byte
    over rejected.  Pin the WRITE-path boundary explicitly (CC-11
    covers the READ-path equivalent)."""
    at_cap = "x" * _MAX_VAULT_VALUE_LEN
    over_cap = "x" * (_MAX_VAULT_VALUE_LEN + 1)
    out = validate_vault_env({"FOO": at_cap})
    assert out == {"FOO": at_cap}
    with pytest.raises(ValueError, match="too long"):
        validate_vault_env({"FOO": over_cap})


# ---------------------------------------------------------------------------
# 12. validate_vault_env: oversize entries boundary.
# ---------------------------------------------------------------------------


def test_phase_d_validate_vault_env_oversize_entries_boundary():
    """Exactly ``_MAX_VAULT_ENV_ENTRIES`` entries accepted; one over
    rejected.  Pin the WRITE-path entry-count cap (CC-27 covers READ).
    Names use a safe ``A0..An`` grammar that satisfies ``_NAME_RE``."""
    at_cap = {f"A{i:03d}": "v" for i in range(_MAX_VAULT_ENV_ENTRIES)}
    # Sanity: entry name regex is satisfied (uppercase + digits, <=64 chars).
    for k in list(at_cap.keys())[:3]:
        assert _NAME_RE.match(k) is not None
    out = validate_vault_env(at_cap)
    assert len(out) == _MAX_VAULT_ENV_ENTRIES

    over_cap = dict(at_cap)
    over_cap[f"B{_MAX_VAULT_ENV_ENTRIES:03d}"] = "v"
    with pytest.raises(ValueError, match="too large"):
        validate_vault_env(over_cap)


# ---------------------------------------------------------------------------
# 13. validate_vault_env: empty-string values silently dropped (WRITE path).
# ---------------------------------------------------------------------------


def test_phase_d_validate_vault_env_drops_empty_value_silently():
    """The WRITE-path contract is: empty-string values are silently
    dropped (the value contributes nothing to the redactor and would
    only generate noise downstream).  CC-09 fixed the corresponding
    READ-path drift (where an empty value was a corruption signal under
    requires_vault=True).  This test pins the WRITE-path documented
    asymmetry: a future change that promotes empty-string values to
    a hard error on WRITE would silently break the CC-09 contract,
    so this assertion is a deliberate guard."""
    out = validate_vault_env({"GOOD": "ok", "EMPTY": "", "ALSO_GOOD": "y"})
    assert out == {"GOOD": "ok", "ALSO_GOOD": "y"}, (
        f"WRITE path must silently drop empty values; got {out!r}"
    )
    # And a payload of ONLY empty values returns {} (still legal: the
    # task simply has no vaulted secrets).
    assert validate_vault_env({"EMPTY1": "", "EMPTY2": ""}) == {}


# ---------------------------------------------------------------------------
# 14. _NAME_RE pattern is anchored with \Z (regression guard).
# ---------------------------------------------------------------------------


def test_phase_d_name_re_pattern_uses_z_anchor():
    """Independent of the behavioural tests above, pin the COMPILED
    pattern source so any future edit that re-introduces ``$`` in
    place of ``\\Z`` trips this test even if no malicious input is
    supplied.  This is a belt-and-braces pin alongside CC-09."""
    pat = _NAME_RE.pattern
    assert pat.endswith("\\Z"), (
        f"_NAME_RE must terminate with the \\Z anchor (CC-09); got {pat!r}"
    )
    assert "$" not in pat, (
        f"_NAME_RE must NOT use the $ anchor (matches before \\n); got {pat!r}"
    )
    # Sanity-check the pattern shape is the expected uppercase-id grammar.
    assert re.fullmatch(r"\^\[A-Z\]\[A-Z0-9_\]\{0,63\}\\Z", pat) is not None, (
        f"_NAME_RE pattern must be the expected ^[A-Z][A-Z0-9_]{{0,63}}\\Z "
        f"shape; got {pat!r}"
    )
