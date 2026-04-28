"""CC-03: behavioural coverage for vault encryption-byte invariants and
RLS defence-in-depth filters.

Existing vault tests cover:

  * runtime-level redaction / no-leak / KDF iteration floor (V-01 / V-02 /
    B-39 / vault_no_leak_live)
  * Redis URL validation (U-03 substring-bypass family)
  * the integration round-trip (live PostgREST)

This file pins the PURE-PYTHON crypto-byte invariants in
``mariana/vault/store.py`` AND the RLS defence-in-depth contract that the
PostgREST URL parameters must always carry both ``id=eq.<sid>`` and
``user_id=eq.<uid>`` filters on every secret-mutation request \u2014 so a
leaked secret-id alone cannot be cross-user weaponised even if RLS were
ever silently disabled at the DB level.

Tests:

  1. ``test_cc03_validate_lengths_rejects_short_blob``
        Lower-bound boundary on encrypted-blob length (GCM tag = 16).

  2. ``test_cc03_validate_lengths_rejects_oversize_blob``
        Upper-bound boundary on encrypted value-blob length (65552).

  3. ``test_cc03_validate_lengths_rejects_non16_salt_and_non12_iv``
        Salt MUST be exactly 16 bytes, IV MUST be exactly 12 bytes \u2014
        AES-GCM contract.  Pin both bounds.

  4. ``test_cc03_create_secret_rejects_oversize_blob_before_http``
        Defence-in-depth: oversize value_blob raises VaultError BEFORE
        a single httpx.AsyncClient is constructed.  Pin the early-exit.

  5. ``test_cc03_secret_name_grammar_rejects_shell_metacharacters``
        ``_validate_name`` rejects every shell metacharacter and lower-case
        starter \u2014 pins the env-injection-prevention contract.

  6. ``test_cc03_create_secret_carries_user_id_in_post_body``
        RLS defence-in-depth: ``create_secret`` POSTs a JSON body where
        ``user_id`` matches the caller; without it RLS would reject
        the row.

  7. ``test_cc03_update_secret_filters_both_id_and_user_id``
        RLS defence-in-depth: PATCH params MUST include BOTH
        ``id=eq.<secret_id>`` AND ``user_id=eq.<user_id>``.

  8. ``test_cc03_delete_secret_filters_both_id_and_user_id``
        Same as #7 for DELETE.

  9. ``test_cc03_get_vault_filters_user_id_eq``
        ``get_vault`` GET query carries ``user_id=eq.<uid>`` \u2014 a missing
        filter would let a service-role client return another user's
        ciphertext.

 10. ``test_cc03_bytea_decoder_rejects_invalid_hex``
        The bytea decoder rejects malformed hex with a VaultError;
        ensures a poisoned PostgREST response cannot crash the worker
        with an uncaught exception.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Shared fakes: a stand-in for ``httpx.AsyncClient`` that records every
# request.  The tests below inspect ``calls`` to assert the URL / params /
# body that store.py constructs without needing a live PostgREST server.
# ---------------------------------------------------------------------------


class _RecordingClient:
    """In-memory drop-in for ``httpx.AsyncClient(timeout=...)``.

    ``status`` and ``json_body`` set the canned response.
    """

    def __init__(self, status: int = 200, json_body: Any = None,
                 text_body: str = ""):
        self.calls: list[dict[str, Any]] = []
        self.status = status
        self.json_body = json_body if json_body is not None else []
        self.text_body = text_body

    def __call__(self, *args, **kwargs):
        # ``store.py`` does ``async with httpx.AsyncClient(timeout=...)``
        # \u2014 the constructor is called with kwargs.  Returning ``self``
        # lets the same instance act as the context manager.
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def request(self, method, url, json=None, params=None, headers=None):
        self.calls.append(
            {"method": method, "url": url, "json": json, "params": params,
             "headers": headers}
        )
        outer = self

        class _R:
            status_code = outer.status
            text = outer.text_body or "[]"

            def json(self_inner):
                return outer.json_body

        return _R()


# ---------------------------------------------------------------------------
# 1 \u2014 lower-bound on blob length.
# ---------------------------------------------------------------------------


def test_cc03_validate_lengths_rejects_short_blob():
    """A blob shorter than 16 bytes (GCM tag size) is invalid \u2014 the
    encryption layer would have produced at least the tag.  Pin the
    lower bound so a regression that drops the floor cannot land
    silently."""
    from mariana.vault.store import VaultError, _validate_lengths  # noqa: PLC0415

    with pytest.raises(VaultError, match="blob length 15"):
        _validate_lengths(blob=b"x" * 15, blob_max=64)


# ---------------------------------------------------------------------------
# 2 \u2014 upper-bound on blob length.
# ---------------------------------------------------------------------------


def test_cc03_validate_lengths_rejects_oversize_blob():
    """A blob of 65553 bytes (one over the secret value cap of 65552) is
    rejected.  Without this the DB CHECK would catch it but only after a
    round-trip; the local validator fails fast."""
    from mariana.vault.store import VaultError, _validate_lengths  # noqa: PLC0415

    with pytest.raises(VaultError, match="65553 outside allowed range"):
        _validate_lengths(blob=b"x" * 65553, blob_max=65552)


# ---------------------------------------------------------------------------
# 3 \u2014 salt and IV size pins.
# ---------------------------------------------------------------------------


def test_cc03_validate_lengths_rejects_non16_salt_and_non12_iv():
    """Salt MUST be 16 bytes (PBKDF2 / Argon2 input), IV MUST be 12 bytes
    (AES-GCM contract).  Off-by-one on either side is a silent
    cryptographic-correctness bug; pin both."""
    from mariana.vault.store import VaultError, _validate_lengths  # noqa: PLC0415

    with pytest.raises(VaultError, match="salt must be 16 bytes"):
        _validate_lengths(salt=b"x" * 15)
    with pytest.raises(VaultError, match="salt must be 16 bytes"):
        _validate_lengths(salt=b"x" * 17)
    with pytest.raises(VaultError, match="iv must be 12 bytes"):
        _validate_lengths(iv=b"x" * 11)
    with pytest.raises(VaultError, match="iv must be 12 bytes"):
        _validate_lengths(iv=b"x" * 13)


# ---------------------------------------------------------------------------
# 4 \u2014 oversize blob refused before any HTTP request fires.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cc03_create_secret_rejects_oversize_blob_before_http():
    """Defence-in-depth: a too-large value_blob raises VaultError BEFORE
    a single httpx.AsyncClient is constructed.  Without this the bad
    payload would still hit the network and surface as a 400 from the
    DB CHECK \u2014 we want the early refusal so a buggy client-side
    encryption pipeline cannot DDoS Supabase."""
    import httpx  # noqa: PLC0415

    from mariana.vault.store import VaultError, create_secret  # noqa: PLC0415

    rec = _RecordingClient(status=201, json_body=[{}])
    with patch.object(httpx, "AsyncClient", rec), \
         pytest.raises(VaultError, match="65553 outside allowed range"):
        await create_secret(
            "https://supabase.test", "service_role_xxx",
            user_id="u-1",
            name="OPENAI_API_KEY",
            value_iv=b"x" * 12,
            value_blob=b"x" * 65553,  # one over the cap
            preview_iv=b"y" * 12,
            preview_blob=b"y" * 16,
        )

    assert rec.calls == [], (
        "oversize blob must NOT reach the network; got "
        f"{len(rec.calls)} HTTP request(s)"
    )


# ---------------------------------------------------------------------------
# 5 \u2014 secret-name grammar enforces shell-safe identifiers only.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "openai_key",       # lowercase starter
        "1OPENAI",          # digit starter
        "OPENAI KEY",       # whitespace
        "OPENAI;rm -rf /",  # shell command separator
        "OPENAI$KEY",       # variable substitution
        "OPENAI`whoami`",   # backtick command sub
        "OPENAI|cat",       # pipe
        "OPENAI&bg",        # background fork
        "OPENAI>file",      # redirect
        "OPENAI/etc/pwd",   # path traversal
        "",                 # empty
        "A" * 65,           # exceeds 64-char tail (1+64 = 65 total OK; 1+65 fails)
    ],
)
def test_cc03_secret_name_grammar_rejects_shell_metacharacters(bad_name: str):
    """``_validate_name`` is the contract that prevents an attacker from
    smuggling shell metacharacters into the env-injection layer.  Pin the
    full denylist so a relaxation of the regex cannot land silently."""
    from mariana.vault.store import VaultError, _validate_name  # noqa: PLC0415

    with pytest.raises(VaultError, match=r"\^\[A-Z\]"):
        _validate_name(bad_name)


def test_cc03_secret_name_grammar_accepts_canonical_forms():
    """Pin the *positive* contract too \u2014 lest a regression tighten the
    grammar past the legitimate range."""
    from mariana.vault.store import _validate_name  # noqa: PLC0415

    # Each must NOT raise.
    for good in ["A", "OPENAI_API_KEY", "X" * 64, "X_1", "ABC123"]:
        _validate_name(good)


# ---------------------------------------------------------------------------
# 6 \u2014 create_secret carries user_id in the JSON body (RLS owner check).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cc03_create_secret_carries_user_id_in_post_body():
    """The PostgREST INSERT payload MUST include ``user_id`` matching the
    caller.  Without it RLS\u2019s ``WITH CHECK (auth.uid() = user_id)``
    would reject the row \u2014 but the worker code-path runs under the
    service-role key, so the LIBRARY-level filter is the only enforcement
    if RLS were ever silently disabled.  Pin the contract."""
    import httpx  # noqa: PLC0415

    from mariana.vault.store import create_secret  # noqa: PLC0415

    rec = _RecordingClient(
        status=201,
        json_body=[{
            "id": "11111111-1111-1111-1111-111111111111",
            "user_id": "u-cc03",
            "name": "OPENAI_API_KEY",
            "description": None,
            "value_iv": "\\x" + "11" * 12,
            "value_blob": "\\x" + "11" * 64,
            "preview_iv": "\\x" + "22" * 12,
            "preview_blob": "\\x" + "22" * 32,
            "created_at": "2026-04-28T00:00:00Z",
            "updated_at": "2026-04-28T00:00:00Z",
        }],
    )
    with patch.object(httpx, "AsyncClient", rec):
        rec_secret = await create_secret(
            "https://supabase.test", "service_role_xxx",
            user_id="u-cc03",
            name="OPENAI_API_KEY",
            value_iv=b"\x11" * 12,
            value_blob=b"\x11" * 64,
            preview_iv=b"\x22" * 12,
            preview_blob=b"\x22" * 32,
        )

    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/rest/v1/vault_secrets")
    assert call["json"]["user_id"] == "u-cc03", (
        "POST body must carry the caller's user_id so RLS WITH CHECK "
        "passes; got "
        f"{call['json'].get('user_id')!r}"
    )
    assert call["json"]["name"] == "OPENAI_API_KEY"
    # The bytea fields are sent as ``\x<hex>`` strings, never raw bytes.
    assert call["json"]["value_iv"].startswith("\\x")
    assert rec_secret.user_id == "u-cc03"


# ---------------------------------------------------------------------------
# 7 \u2014 update_secret filters BOTH id and user_id.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cc03_update_secret_filters_both_id_and_user_id():
    """The PATCH params MUST carry both ``id=eq.<secret_id>`` AND
    ``user_id=eq.<user_id>`` so a leaked or guessed secret_id cannot be
    used by a different user to overwrite this row \u2014 even if RLS were
    silently disabled."""
    import httpx  # noqa: PLC0415

    from mariana.vault.store import update_secret  # noqa: PLC0415

    rec = _RecordingClient(
        status=200,
        json_body=[{
            "id": "22222222-2222-2222-2222-222222222222",
            "user_id": "u-cc03",
            "name": "OPENAI_API_KEY",
            "description": None,
            "value_iv": "\\x" + "33" * 12,
            "value_blob": "\\x" + "33" * 64,
            "preview_iv": "\\x" + "44" * 12,
            "preview_blob": "\\x" + "44" * 32,
            "created_at": "2026-04-28T00:00:00Z",
            "updated_at": "2026-04-28T00:00:00Z",
        }],
    )
    with patch.object(httpx, "AsyncClient", rec):
        await update_secret(
            "https://supabase.test", "service_role_xxx",
            user_id="u-cc03",
            secret_id="22222222-2222-2222-2222-222222222222",
            value_iv=b"\x33" * 12,
            value_blob=b"\x33" * 64,
            preview_iv=b"\x44" * 12,
            preview_blob=b"\x44" * 32,
        )

    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["method"] == "PATCH"
    params = call["params"] or {}
    assert params.get("id") == "eq.22222222-2222-2222-2222-222222222222", (
        "PATCH must filter by id; got "
        f"{params.get('id')!r}"
    )
    assert params.get("user_id") == "eq.u-cc03", (
        "PATCH must ALSO filter by user_id (defence-in-depth against a "
        f"leaked secret_id); got {params.get('user_id')!r}"
    )


# ---------------------------------------------------------------------------
# 8 \u2014 delete_secret filters BOTH id and user_id.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cc03_delete_secret_filters_both_id_and_user_id():
    """Same defence-in-depth contract as PATCH, applied to DELETE."""
    import httpx  # noqa: PLC0415

    from mariana.vault.store import delete_secret  # noqa: PLC0415

    rec = _RecordingClient(status=204, text_body="")
    with patch.object(httpx, "AsyncClient", rec):
        await delete_secret(
            "https://supabase.test", "service_role_xxx",
            user_id="u-cc03",
            secret_id="33333333-3333-3333-3333-333333333333",
        )

    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["method"] == "DELETE"
    params = call["params"] or {}
    assert params.get("id") == "eq.33333333-3333-3333-3333-333333333333"
    assert params.get("user_id") == "eq.u-cc03", (
        "DELETE must filter by user_id; got "
        f"{params.get('user_id')!r}"
    )


# ---------------------------------------------------------------------------
# 9 \u2014 get_vault filters by user_id (no cross-user leak).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cc03_get_vault_filters_user_id_eq():
    """``get_vault`` must scope by ``user_id=eq.<uid>`` \u2014 a missing
    filter would let a service-role caller fetch arbitrary user vault
    rows.  Pin the WHERE clause."""
    import httpx  # noqa: PLC0415

    from mariana.vault.store import get_vault  # noqa: PLC0415

    rec = _RecordingClient(
        status=200,
        json_body=[{
            "user_id": "u-cc03",
            "kdf_algorithm": "argon2id",
            "kdf_memory_kib": 65536,
            "kdf_iterations": 3,
            "kdf_parallelism": 4,
            "passphrase_salt": "\\x" + "11" * 16,
            "passphrase_iv": "\\x" + "22" * 12,
            "passphrase_blob": "\\x" + "33" * 48,
            "recovery_salt": "\\x" + "44" * 16,
            "recovery_iv": "\\x" + "55" * 12,
            "recovery_blob": "\\x" + "66" * 48,
            "verifier_iv": "\\x" + "77" * 12,
            "verifier_blob": "\\x" + "88" * 48,
            "created_at": "2026-04-28T00:00:00Z",
            "updated_at": "2026-04-28T00:00:00Z",
        }],
    )
    with patch.object(httpx, "AsyncClient", rec):
        v = await get_vault(
            "https://supabase.test", "service_role_xxx", user_id="u-cc03",
        )

    assert v.user_id == "u-cc03"
    assert len(rec.calls) == 1
    params = rec.calls[0]["params"] or {}
    assert params.get("user_id") == "eq.u-cc03", (
        "GET must filter by user_id; got "
        f"{params.get('user_id')!r}"
    )
    assert params.get("limit") == "1", (
        "GET must limit to 1 row \u2014 a malformed query that returns the "
        "whole table would leak ciphertext for every user; pin LIMIT 1"
    )


# ---------------------------------------------------------------------------
# 10 \u2014 bytea decoder fail-closed on malformed input.
# ---------------------------------------------------------------------------


def test_cc03_bytea_decoder_rejects_invalid_hex():
    """If a poisoned PostgREST response carried a malformed bytea value
    (odd-length hex, non-hex chars), ``_from_bytea`` must raise
    ``VaultError`` so the worker fails fast \u2014 a silent ``b''`` return
    would forward bogus key material into the AES-GCM decryption layer."""
    from mariana.vault.store import VaultError, _from_bytea  # noqa: PLC0415

    # Invalid hex character in the canonical ``\x``-prefixed form.
    with pytest.raises(VaultError):
        _from_bytea("\\xZZ")
    # Odd-length hex --- cannot be a valid byte sequence.
    with pytest.raises(VaultError):
        _from_bytea("\\xabc")
    # Wrong type entirely (e.g. an int leaked through).
    with pytest.raises(VaultError):
        _from_bytea(12345)
    # Non-base64 ASCII garbage in the legacy fallback path.
    with pytest.raises(VaultError):
        _from_bytea("!!! invalid base64 !!!")


def test_cc03_bytea_decoder_handles_empty_and_canonical_forms():
    """The decoder must accept ``\\x`` (empty bytea), correctly parse
    valid hex, and accept raw ``bytes`` passthrough."""
    from mariana.vault.store import _from_bytea  # noqa: PLC0415

    assert _from_bytea(None) == b""
    assert _from_bytea("\\x") == b""
    assert _from_bytea("\\xdeadbeef") == b"\xde\xad\xbe\xef"
    assert _from_bytea(b"raw") == b"raw"
