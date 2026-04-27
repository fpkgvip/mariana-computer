"""B-39 regression suite: vault KDF enforces minimum kdf_iterations ≥ 2.

Before the fix, CreateVaultRequest had ge=1 for kdf_iterations so a client
could submit kdf_iterations=1 and the server would accept it.  argon2id with
t=1 is below the OWASP 2023 recommended minimum (t ≥ 2) and makes offline
brute-force attacks significantly easier.

After the fix:
  - Pydantic Field has ge=2 (validation error on < 2).
  - vault_create raises HTTP 400 with a clear security-policy message.

Test IDs:
  1. test_kdf_iterations_1_rejected_by_pydantic
  2. test_kdf_iterations_0_rejected_by_pydantic
  3. test_kdf_iterations_2_accepted (OWASP minimum)
  4. test_kdf_iterations_default_is_ge_minimum
  5. test_vault_create_endpoint_400_for_kdf_iterations_1
  6. test_vault_create_endpoint_accepts_valid_iterations
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from mariana.vault.router import CreateVaultRequest

# ---------------------------------------------------------------------------
# Shared test payload helpers
# ---------------------------------------------------------------------------

def _b64(n: int) -> str:
    """Return a base64 string of n random-ish bytes."""
    return base64.b64encode(bytes(range(n % 256)) * (n // 256 + 1))[:n * 2 // 3].decode() + "=="


def _valid_payload(kdf_iterations: int) -> dict:
    """Return a minimal valid vault creation payload with the given kdf_iterations."""
    return {
        "passphrase_salt": base64.b64encode(b"\x00" * 16).decode(),
        "passphrase_iv": base64.b64encode(b"\x01" * 12).decode(),
        "passphrase_blob": base64.b64encode(b"\x02" * 32).decode(),
        "recovery_salt": base64.b64encode(b"\x03" * 16).decode(),
        "recovery_iv": base64.b64encode(b"\x04" * 12).decode(),
        "recovery_blob": base64.b64encode(b"\x05" * 32).decode(),
        "verifier_iv": base64.b64encode(b"\x06" * 12).decode(),
        "verifier_blob": base64.b64encode(b"\x07" * 32).decode(),
        "kdf_iterations": kdf_iterations,
    }


# ---------------------------------------------------------------------------
# Test 1: kdf_iterations=1 rejected by Pydantic validation
# ---------------------------------------------------------------------------

def test_kdf_iterations_1_rejected_by_pydantic():
    """B-39: kdf_iterations=1 must fail Pydantic validation (ge=2)."""
    with pytest.raises(ValidationError) as exc_info:
        CreateVaultRequest(**_valid_payload(kdf_iterations=1))

    errors = exc_info.value.errors()
    fields = [e.get("loc", ()) for e in errors]
    assert any("kdf_iterations" in str(f) for f in fields), (
        f"B-39: validation error must reference kdf_iterations, got: {errors}"
    )


# ---------------------------------------------------------------------------
# Test 2: kdf_iterations=0 rejected by Pydantic validation
# ---------------------------------------------------------------------------

def test_kdf_iterations_0_rejected_by_pydantic():
    """kdf_iterations=0 must also be rejected."""
    with pytest.raises(ValidationError):
        CreateVaultRequest(**_valid_payload(kdf_iterations=0))


# ---------------------------------------------------------------------------
# Test 3: kdf_iterations=2 accepted (OWASP argon2id minimum)
# ---------------------------------------------------------------------------

def test_kdf_iterations_2_accepted():
    """kdf_iterations=2 is the minimum and must be accepted."""
    req = CreateVaultRequest(**_valid_payload(kdf_iterations=2))
    assert req.kdf_iterations == 2


# ---------------------------------------------------------------------------
# Test 4: default kdf_iterations is ≥ minimum
# ---------------------------------------------------------------------------

def test_kdf_iterations_default_is_ge_minimum():
    """The default kdf_iterations (3) must satisfy the OWASP minimum (≥ 2)."""
    req = CreateVaultRequest(**{k: v for k, v in _valid_payload(3).items() if k != "kdf_iterations"})
    assert req.kdf_iterations >= 2, (
        f"B-39: default kdf_iterations {req.kdf_iterations} must be ≥ 2"
    )


# ---------------------------------------------------------------------------
# Test 5: vault_create endpoint returns 400 for kdf_iterations=1
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vault_create_endpoint_400_for_kdf_iterations_1():
    """HTTP POST /api/vault with kdf_iterations=1 must return 400 or 422."""
    from httpx import AsyncClient, ASGITransport
    from fastapi import FastAPI
    from mariana.vault.router import build_vault_router

    # Build a minimal app with just the vault router
    test_app = FastAPI()

    def _get_fake_user():
        """Sync callable returning user dict — used as FastAPI dependency."""
        return {"user_id": "user-test-vault"}

    router = build_vault_router(
        get_current_user=_get_fake_user,
        get_supabase_url=lambda: "",
        get_service_key=lambda: "",
    )
    test_app.include_router(router)

    payload = _valid_payload(kdf_iterations=1)

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        resp = await client.post(
            "/api/vault",
            json=payload,
            headers={"Authorization": "Bearer test"},
        )

    assert resp.status_code in (400, 422), (
        f"B-39: kdf_iterations=1 must be rejected with 400 or 422, got {resp.status_code}. "
        f"Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# Test 6: vault_create endpoint accepts kdf_iterations=3 (default)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vault_create_endpoint_accepts_valid_iterations():
    """HTTP POST /api/vault with kdf_iterations=3 must not be rejected for iteration count."""
    from httpx import AsyncClient, ASGITransport
    from fastapi import FastAPI
    from mariana.vault.router import build_vault_router
    from mariana.vault.store import VaultExists

    test_app = FastAPI()

    def _get_fake_user():
        return {"user_id": "user-valid-vault"}

    # Patch create_vault to raise VaultExists (simulates vault already exists)
    # so we can confirm the validation passes (it would only 409 after passing validation)
    with patch("mariana.vault.router.create_vault", new_callable=AsyncMock, side_effect=VaultExists()):
        router = build_vault_router(
            get_current_user=_get_fake_user,
            get_supabase_url=lambda: "https://supabase.test",
            get_service_key=lambda: "service-key",
        )
        test_app.include_router(router)

        payload = _valid_payload(kdf_iterations=3)

        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            resp = await client.post(
                "/api/vault",
                json=payload,
                headers={"Authorization": "Bearer test"},
            )

    # Should not be 400/422 for the iteration count (may be 409 VaultExists or 201)
    assert resp.status_code not in (400, 422), (
        f"B-39: valid kdf_iterations=3 must not be rejected, got {resp.status_code}. "
        f"Body: {resp.text[:300]}"
    )
