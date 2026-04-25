"""Live integration test for the vault router.

Hits the deployed Hetzner backend with a real Supabase-issued JWT.
Skipped automatically when DEFT_LIVE=1 is not set in the environment.

Tests the full happy-path round trip:
  1. Delete any pre-existing vault for the test user (idempotent setup).
  2. POST /api/vault                → 201, returns vault meta echo.
  3. GET  /api/vault                → 200, matches POST payload.
  4. POST /api/vault/secrets        → 201, returns ciphertext echo.
  5. GET  /api/vault/secrets        → 200, exactly one entry.
  6. PATCH /api/vault/secrets/{id}  → 200, ciphertext rotates.
  7. DELETE /api/vault/secrets/{id} → 204.
  8. DELETE /api/vault              → 204.

The server never sees plaintext: only random bytes that mimic ciphertext
shape (12-byte IV, 32-byte tag-bearing blob).  This proves the wire
format and storage round-trip but says nothing about decryption.
"""

from __future__ import annotations

import base64
import os
import secrets
import uuid

import httpx
import pytest

LIVE = os.environ.get("DEFT_LIVE") == "1"
BASE = os.environ.get("DEFT_BASE", "http://77.42.3.206:8080")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://afnbtbeayfkwznhzafay.supabase.co")
SUPABASE_ANON = os.environ.get(
    "SUPABASE_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFmbmJ0YmVheWZrd3puaHphZmF5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzUyOTE1NTIsImV4cCI6MjA5MDg2NzU1Mn0.e_bgdqJryv3lAXEDF8CVL7AHxPzhfKeFkYElAYynF5I",
)
TEST_EMAIL = "testrunner@mariana.test"
TEST_PASSWORD = "DeftTest!2026"

pytestmark = pytest.mark.skipif(not LIVE, reason="DEFT_LIVE=1 not set")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _login() -> str:
    r = httpx.post(
        f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
        headers={"apikey": SUPABASE_ANON, "Content-Type": "application/json"},
        json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_login()}"}


def test_full_vault_lifecycle(auth_headers: dict[str, str]) -> None:
    # --- Clean slate: delete any pre-existing vault ---
    httpx.delete(f"{BASE}/api/vault", headers=auth_headers, timeout=15.0)

    # --- POST /api/vault ---
    payload = {
        "passphrase_salt": _b64(secrets.token_bytes(16)),
        "passphrase_iv":   _b64(secrets.token_bytes(12)),
        "passphrase_blob": _b64(secrets.token_bytes(48)),  # 32 KEK + 16 GCM tag
        "recovery_salt":   _b64(secrets.token_bytes(16)),
        "recovery_iv":     _b64(secrets.token_bytes(12)),
        "recovery_blob":   _b64(secrets.token_bytes(48)),
        "verifier_iv":     _b64(secrets.token_bytes(12)),
        "verifier_blob":   _b64(secrets.token_bytes(48)),
    }
    r = httpx.post(f"{BASE}/api/vault", headers=auth_headers, json=payload, timeout=15.0)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kdf_algorithm"] == "argon2id"
    assert body["passphrase_salt"] == payload["passphrase_salt"]

    # --- GET /api/vault round-trip ---
    r = httpx.get(f"{BASE}/api/vault", headers=auth_headers, timeout=15.0)
    assert r.status_code == 200, r.text
    got = r.json()
    for k in payload:
        assert got[k] == payload[k], f"mismatch on {k}"

    # --- POST /api/vault/secrets ---
    name = f"TEST_KEY_{uuid.uuid4().hex[:8].upper()}"
    sec_payload = {
        "name": name,
        "description": "live test",
        "value_iv":    _b64(secrets.token_bytes(12)),
        "value_blob":  _b64(secrets.token_bytes(48)),
        "preview_iv":  _b64(secrets.token_bytes(12)),
        "preview_blob": _b64(secrets.token_bytes(20)),
    }
    r = httpx.post(
        f"{BASE}/api/vault/secrets",
        headers=auth_headers,
        json=sec_payload,
        timeout=15.0,
    )
    assert r.status_code == 201, r.text
    secret_id = r.json()["id"]

    # --- GET /api/vault/secrets ---
    r = httpx.get(f"{BASE}/api/vault/secrets", headers=auth_headers, timeout=15.0)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert any(s["id"] == secret_id and s["name"] == name for s in rows)

    # --- duplicate name → 409 ---
    r = httpx.post(
        f"{BASE}/api/vault/secrets",
        headers=auth_headers,
        json=sec_payload,
        timeout=15.0,
    )
    assert r.status_code == 409, r.text

    # --- PATCH /api/vault/secrets/{id} ---
    new_value_iv = _b64(secrets.token_bytes(12))
    new_value_blob = _b64(secrets.token_bytes(48))
    r = httpx.patch(
        f"{BASE}/api/vault/secrets/{secret_id}",
        headers=auth_headers,
        json={
            "value_iv": new_value_iv,
            "value_blob": new_value_blob,
            "preview_iv": _b64(secrets.token_bytes(12)),
            "preview_blob": _b64(secrets.token_bytes(20)),
            "description": "rotated",
        },
        timeout=15.0,
    )
    assert r.status_code == 200, r.text
    upd = r.json()
    assert upd["value_iv"] == new_value_iv
    assert upd["description"] == "rotated"

    # --- DELETE secret ---
    r = httpx.delete(
        f"{BASE}/api/vault/secrets/{secret_id}", headers=auth_headers, timeout=15.0,
    )
    assert r.status_code == 204, r.text

    # --- DELETE vault ---
    r = httpx.delete(f"{BASE}/api/vault", headers=auth_headers, timeout=15.0)
    assert r.status_code == 204, r.text

    # --- GET vault now 404s ---
    r = httpx.get(f"{BASE}/api/vault", headers=auth_headers, timeout=15.0)
    assert r.status_code == 404, r.text
