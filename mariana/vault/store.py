"""Async Supabase wrappers for vault tables.

Server-side persistence layer. The server never sees plaintext key material;
all bytes that flow through here are ciphertext or KDF metadata. Bytes are
exchanged with PostgREST as base64-encoded ``\\x``-prefixed hex strings via
the ``bytea`` JSON contract — we use base64 over JSON for clean transport.

Concretely:
  • Inputs (Python → DB): pass raw ``bytes``; we hex-encode and prefix with
    ``\\x`` so PostgREST stores them as bytea.
  • Outputs (DB → Python): PostgREST returns bytea as ``\\x<hex>`` strings;
    we strip and unhex before returning.

Authorization model:
  • All calls run with the service-role key so the API can act on behalf
    of authenticated users (whose IDs come from JWT validation upstream).
  • RLS still applies because we always filter on user_id explicitly and
    pass it through to the policy via the SQL filter, providing defence
    in depth.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Errors
# -----------------------------------------------------------------------------

class VaultError(Exception):
    """Generic vault persistence failure."""


class VaultExists(VaultError):
    """Raised on attempted re-creation of a vault that already exists."""


class VaultNotFound(VaultError):
    """Raised when a vault read targets a user without one."""


class SecretExists(VaultError):
    """Raised when an INSERT collides with the (user_id, name) unique index."""


class SecretNotFound(VaultError):
    """Raised when a referenced secret does not exist for the user."""


# -----------------------------------------------------------------------------
# bytea ↔ bytes helpers
# -----------------------------------------------------------------------------

_BYTEA_HEX_RE = re.compile(r"^\\x([0-9a-fA-F]*)$")


def _bytea(b: bytes) -> str:
    """Encode raw bytes as a Postgres bytea hex literal."""
    if not isinstance(b, (bytes, bytearray)):
        raise TypeError("bytea encoding requires bytes")
    return "\\x" + b.hex()


def _from_bytea(value: Any) -> bytes:
    """Decode a Postgres bytea response (``\\x<hex>``) into raw bytes."""
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        m = _BYTEA_HEX_RE.match(value)
        if m:
            try:
                return bytes.fromhex(m.group(1))
            except ValueError as exc:
                raise VaultError(f"invalid bytea hex: {exc}")
        # Fallback: caller may already have base64 (unlikely from PostgREST,
        # but handled defensively).
        try:
            return base64.b64decode(value)
        except (binascii.Error, ValueError) as exc:
            raise VaultError(f"invalid bytea encoding: {exc}")
    raise VaultError(f"unexpected bytea type: {type(value).__name__}")


# -----------------------------------------------------------------------------
# Data classes
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class VaultRecord:
    user_id: str
    kdf_algorithm: str
    kdf_memory_kib: int
    kdf_iterations: int
    kdf_parallelism: int
    passphrase_salt: bytes
    passphrase_iv: bytes
    passphrase_blob: bytes
    recovery_salt: bytes
    recovery_iv: bytes
    recovery_blob: bytes
    verifier_iv: bytes
    verifier_blob: bytes
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SecretRecord:
    id: str
    user_id: str
    name: str
    description: Optional[str]
    value_iv: bytes
    value_blob: bytes
    preview_iv: bytes
    preview_blob: bytes
    created_at: str
    updated_at: str


# -----------------------------------------------------------------------------
# Validation helpers
# -----------------------------------------------------------------------------

# CC-09: anchor with \Z (not $) — Python's $ matches before a trailing \n,
# allowing a poisoned name like "FOO\n" to bypass shape validation.
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}\Z")


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise VaultError(
            "secret name must match ^[A-Z][A-Z0-9_]{0,63}$ (e.g. OPENAI_API_KEY)"
        )


def _validate_lengths(
    *,
    salt: Optional[bytes] = None,
    iv: Optional[bytes] = None,
    blob: Optional[bytes] = None,
    blob_max: int = 65552,
) -> None:
    if salt is not None and len(salt) != 16:
        raise VaultError(f"salt must be 16 bytes (got {len(salt)})")
    if iv is not None and len(iv) != 12:
        raise VaultError(f"iv must be 12 bytes (got {len(iv)})")
    if blob is not None and not (16 <= len(blob) <= blob_max):
        raise VaultError(
            f"blob length {len(blob)} outside allowed range [16, {blob_max}]"
        )


# -----------------------------------------------------------------------------
# HTTP helper
# -----------------------------------------------------------------------------

def _headers(service_key: str) -> dict[str, str]:
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def _request(
    supabase_url: str,
    service_key: str,
    *,
    method: str,
    path: str,
    json: Any = None,
    params: Optional[dict[str, str]] = None,
    timeout: float = 10.0,
) -> httpx.Response:
    if not supabase_url or not service_key:
        raise VaultError("supabase service credentials missing")
    url = f"{supabase_url.rstrip('/')}/rest/v1{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method, url, json=json, params=params, headers=_headers(service_key)
            )
    except httpx.HTTPError as exc:
        logger.error("vault_http_error", extra={"path": path, "err": str(exc)})
        raise VaultError(f"network error: {exc}") from exc
    return resp


# -----------------------------------------------------------------------------
# user_vaults CRUD
# -----------------------------------------------------------------------------

def _vault_from_row(row: dict[str, Any]) -> VaultRecord:
    return VaultRecord(
        user_id=row["user_id"],
        kdf_algorithm=row["kdf_algorithm"],
        kdf_memory_kib=int(row["kdf_memory_kib"]),
        kdf_iterations=int(row["kdf_iterations"]),
        kdf_parallelism=int(row["kdf_parallelism"]),
        passphrase_salt=_from_bytea(row["passphrase_salt"]),
        passphrase_iv=_from_bytea(row["passphrase_iv"]),
        passphrase_blob=_from_bytea(row["passphrase_blob"]),
        recovery_salt=_from_bytea(row["recovery_salt"]),
        recovery_iv=_from_bytea(row["recovery_iv"]),
        recovery_blob=_from_bytea(row["recovery_blob"]),
        verifier_iv=_from_bytea(row["verifier_iv"]),
        verifier_blob=_from_bytea(row["verifier_blob"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create_vault(
    supabase_url: str,
    service_key: str,
    *,
    user_id: str,
    passphrase_salt: bytes,
    passphrase_iv: bytes,
    passphrase_blob: bytes,
    recovery_salt: bytes,
    recovery_iv: bytes,
    recovery_blob: bytes,
    verifier_iv: bytes,
    verifier_blob: bytes,
    kdf_memory_kib: int = 65536,
    kdf_iterations: int = 3,
    kdf_parallelism: int = 4,
) -> VaultRecord:
    _validate_lengths(salt=passphrase_salt, iv=passphrase_iv, blob=passphrase_blob, blob_max=96)
    _validate_lengths(salt=recovery_salt, iv=recovery_iv, blob=recovery_blob, blob_max=96)
    _validate_lengths(iv=verifier_iv, blob=verifier_blob, blob_max=128)

    payload = {
        "user_id": user_id,
        "kdf_algorithm": "argon2id",
        "kdf_memory_kib": kdf_memory_kib,
        "kdf_iterations": kdf_iterations,
        "kdf_parallelism": kdf_parallelism,
        "passphrase_salt": _bytea(passphrase_salt),
        "passphrase_iv": _bytea(passphrase_iv),
        "passphrase_blob": _bytea(passphrase_blob),
        "recovery_salt": _bytea(recovery_salt),
        "recovery_iv": _bytea(recovery_iv),
        "recovery_blob": _bytea(recovery_blob),
        "verifier_iv": _bytea(verifier_iv),
        "verifier_blob": _bytea(verifier_blob),
    }
    resp = await _request(
        supabase_url, service_key,
        method="POST", path="/user_vaults", json=payload,
    )
    if resp.status_code == 409 or (
        resp.status_code == 400 and "duplicate key" in resp.text.lower()
    ):
        raise VaultExists(f"vault already exists for user {user_id}")
    if resp.status_code not in (200, 201):
        raise VaultError(f"create_vault failed: {resp.status_code} {resp.text[:200]}")
    rows = resp.json()
    if isinstance(rows, list):
        if not rows:
            raise VaultError("create_vault returned no rows")
        return _vault_from_row(rows[0])
    return _vault_from_row(rows)


async def get_vault(
    supabase_url: str,
    service_key: str,
    *,
    user_id: str,
) -> VaultRecord:
    resp = await _request(
        supabase_url, service_key,
        method="GET", path="/user_vaults",
        params={"user_id": f"eq.{user_id}", "select": "*", "limit": "1"},
    )
    if resp.status_code != 200:
        raise VaultError(f"get_vault failed: {resp.status_code} {resp.text[:200]}")
    rows = resp.json()
    if not rows:
        raise VaultNotFound(f"no vault for user {user_id}")
    return _vault_from_row(rows[0])


async def delete_vault(
    supabase_url: str,
    service_key: str,
    *,
    user_id: str,
) -> None:
    """Delete a user's vault and all its secrets (cascades via FK)."""
    resp = await _request(
        supabase_url, service_key,
        method="DELETE", path="/user_vaults",
        params={"user_id": f"eq.{user_id}"},
    )
    if resp.status_code not in (200, 204):
        raise VaultError(f"delete_vault failed: {resp.status_code} {resp.text[:200]}")


# -----------------------------------------------------------------------------
# vault_secrets CRUD
# -----------------------------------------------------------------------------

def _secret_from_row(row: dict[str, Any]) -> SecretRecord:
    return SecretRecord(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        description=row.get("description"),
        value_iv=_from_bytea(row["value_iv"]),
        value_blob=_from_bytea(row["value_blob"]),
        preview_iv=_from_bytea(row["preview_iv"]),
        preview_blob=_from_bytea(row["preview_blob"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def list_secrets(
    supabase_url: str,
    service_key: str,
    *,
    user_id: str,
) -> list[SecretRecord]:
    resp = await _request(
        supabase_url, service_key,
        method="GET", path="/vault_secrets",
        params={
            "user_id": f"eq.{user_id}",
            "select": "*",
            "order": "name.asc",
        },
    )
    if resp.status_code != 200:
        raise VaultError(f"list_secrets failed: {resp.status_code} {resp.text[:200]}")
    return [_secret_from_row(r) for r in resp.json()]


async def create_secret(
    supabase_url: str,
    service_key: str,
    *,
    user_id: str,
    name: str,
    value_iv: bytes,
    value_blob: bytes,
    preview_iv: bytes,
    preview_blob: bytes,
    description: Optional[str] = None,
) -> SecretRecord:
    _validate_name(name)
    _validate_lengths(iv=value_iv, blob=value_blob, blob_max=65552)
    _validate_lengths(iv=preview_iv, blob=preview_blob, blob_max=64)

    payload = {
        "user_id": user_id,
        "name": name,
        "description": description,
        "value_iv": _bytea(value_iv),
        "value_blob": _bytea(value_blob),
        "preview_iv": _bytea(preview_iv),
        "preview_blob": _bytea(preview_blob),
    }
    resp = await _request(
        supabase_url, service_key,
        method="POST", path="/vault_secrets", json=payload,
    )
    if resp.status_code == 409 or (
        resp.status_code == 400 and "duplicate key" in resp.text.lower()
    ):
        raise SecretExists(f"secret {name!r} already exists")
    if resp.status_code not in (200, 201):
        raise VaultError(
            f"create_secret failed: {resp.status_code} {resp.text[:200]}"
        )
    rows = resp.json()
    if isinstance(rows, list) and rows:
        return _secret_from_row(rows[0])
    if isinstance(rows, dict):
        return _secret_from_row(rows)
    raise VaultError("create_secret returned no row")


async def update_secret(
    supabase_url: str,
    service_key: str,
    *,
    user_id: str,
    secret_id: str,
    value_iv: bytes,
    value_blob: bytes,
    preview_iv: bytes,
    preview_blob: bytes,
    description: Optional[str] = None,
) -> SecretRecord:
    _validate_lengths(iv=value_iv, blob=value_blob, blob_max=65552)
    _validate_lengths(iv=preview_iv, blob=preview_blob, blob_max=64)
    payload: dict[str, Any] = {
        "value_iv": _bytea(value_iv),
        "value_blob": _bytea(value_blob),
        "preview_iv": _bytea(preview_iv),
        "preview_blob": _bytea(preview_blob),
    }
    if description is not None:
        payload["description"] = description
    resp = await _request(
        supabase_url, service_key,
        method="PATCH", path="/vault_secrets",
        params={
            "id": f"eq.{secret_id}",
            "user_id": f"eq.{user_id}",
        },
        json=payload,
    )
    if resp.status_code not in (200, 204):
        raise VaultError(
            f"update_secret failed: {resp.status_code} {resp.text[:200]}"
        )
    rows = resp.json() if resp.text else []
    if not rows:
        raise SecretNotFound(f"secret {secret_id} not found for user {user_id}")
    return _secret_from_row(rows[0])


async def delete_secret(
    supabase_url: str,
    service_key: str,
    *,
    user_id: str,
    secret_id: str,
) -> None:
    resp = await _request(
        supabase_url, service_key,
        method="DELETE", path="/vault_secrets",
        params={
            "id": f"eq.{secret_id}",
            "user_id": f"eq.{user_id}",
        },
    )
    if resp.status_code not in (200, 204):
        raise VaultError(
            f"delete_secret failed: {resp.status_code} {resp.text[:200]}"
        )
