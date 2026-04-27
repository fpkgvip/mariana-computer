"""FastAPI router for Deft vault endpoints.

All payloads are base64-encoded ciphertext / IV / salt. The server
deliberately does NOT have access to plaintext or master keys.

Endpoints
---------
GET    /api/vault                         → vault meta (or 404)
POST   /api/vault                         → create vault (idempotent guard)
DELETE /api/vault                         → drop vault + cascade secrets
GET    /api/vault/secrets                 → list ciphertexts
POST   /api/vault/secrets                 → add new ciphertext entry
PATCH  /api/vault/secrets/{id}            → update existing entry
DELETE /api/vault/secrets/{id}            → remove entry
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .store import (
    SecretExists,
    SecretNotFound,
    VaultError,
    VaultExists,
    VaultNotFound,
    create_secret,
    create_vault,
    delete_secret,
    delete_vault,
    get_vault,
    list_secrets,
    update_secret,
)

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Base64 helpers — bytea is opaque to the API; we transport as base64
# -----------------------------------------------------------------------------

def _b64decode_strict(s: str, *, expected_min: int, expected_max: int, label: str) -> bytes:
    if not isinstance(s, str):
        raise ValueError(f"{label}: must be a base64 string")
    try:
        raw = base64.b64decode(s, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{label}: invalid base64 ({exc})") from exc
    if not (expected_min <= len(raw) <= expected_max):
        raise ValueError(
            f"{label}: byte length {len(raw)} outside [{expected_min}, {expected_max}]"
        )
    return raw


def _b64encode(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


# -----------------------------------------------------------------------------
# Request / response models
# -----------------------------------------------------------------------------

class CreateVaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # All fields are base64 strings. Length enforcement happens in the
    # validators (and again in the DB CHECK constraints).
    passphrase_salt: str = Field(..., min_length=1)
    passphrase_iv: str = Field(..., min_length=1)
    passphrase_blob: str = Field(..., min_length=1)
    recovery_salt: str = Field(..., min_length=1)
    recovery_iv: str = Field(..., min_length=1)
    recovery_blob: str = Field(..., min_length=1)
    verifier_iv: str = Field(..., min_length=1)
    verifier_blob: str = Field(..., min_length=1)

    # Optional KDF tuning (defaults match m=64MiB/t=3/p=4)
    # B-39 fix: raise kdf_iterations lower bound from ge=1 to ge=2 to match
    # the OWASP 2023 argon2id minimum (t ≥ 2).  ge=1 allowed a client to
    # create a vault with deliberately weak KDF parameters that are trivially
    # brute-forced offline.
    kdf_memory_kib: int = Field(default=65536, ge=16384, le=1048576)
    kdf_iterations: int = Field(default=3, ge=2, le=16)
    kdf_parallelism: int = Field(default=4, ge=1, le=16)

    # B-39: server-side minimum enforcement (belt-and-suspenders over Pydantic).
    _KDF_ITERATIONS_MIN: int = 2  # OWASP 2023 argon2id minimum t parameter

    @field_validator("kdf_iterations")
    @classmethod
    def _kdf_iterations_min(cls, v: int) -> int:
        """B-39: reject any iteration count below the OWASP argon2id floor."""
        if v < 2:
            raise ValueError(
                f"kdf_iterations must be \u2265 2 (OWASP 2023 argon2id minimum); "
                f"got {v}"
            )
        return v


class VaultResponse(BaseModel):
    user_id: str
    kdf_algorithm: str
    kdf_memory_kib: int
    kdf_iterations: int
    kdf_parallelism: int
    passphrase_salt: str
    passphrase_iv: str
    passphrase_blob: str
    recovery_salt: str
    recovery_iv: str
    recovery_blob: str
    verifier_iv: str
    verifier_blob: str
    created_at: str
    updated_at: str


class CreateSecretRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    description: Optional[str] = Field(default=None, max_length=512)
    value_iv: str = Field(..., min_length=1)
    value_blob: str = Field(..., min_length=1)
    preview_iv: str = Field(..., min_length=1)
    preview_blob: str = Field(..., min_length=1)


class UpdateSecretRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value_iv: str = Field(..., min_length=1)
    value_blob: str = Field(..., min_length=1)
    preview_iv: str = Field(..., min_length=1)
    preview_blob: str = Field(..., min_length=1)
    description: Optional[str] = Field(default=None, max_length=512)


class SecretResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    value_iv: str
    value_blob: str
    preview_iv: str
    preview_blob: str
    created_at: str
    updated_at: str


# -----------------------------------------------------------------------------
# Router factory
# -----------------------------------------------------------------------------

def build_vault_router(
    *,
    get_current_user: Callable[..., Awaitable[dict[str, Any]]],
    get_supabase_url: Callable[[], str],
    get_service_key: Callable[[], str],
) -> APIRouter:
    router = APIRouter()

    # ------------------------ vault root ----------------------------------

    @router.get("/api/vault", response_model=VaultResponse)
    async def vault_get(current_user: dict = Depends(get_current_user)):
        try:
            v = await get_vault(
                get_supabase_url(), get_service_key(),
                user_id=current_user["user_id"],
            )
        except VaultNotFound:
            raise HTTPException(404, "vault not initialized")
        except VaultError as exc:
            logger.error("vault_get_failed", extra={"err": str(exc)})
            raise HTTPException(503, "vault unavailable")
        return VaultResponse(
            user_id=v.user_id,
            kdf_algorithm=v.kdf_algorithm,
            kdf_memory_kib=v.kdf_memory_kib,
            kdf_iterations=v.kdf_iterations,
            kdf_parallelism=v.kdf_parallelism,
            passphrase_salt=_b64encode(v.passphrase_salt),
            passphrase_iv=_b64encode(v.passphrase_iv),
            passphrase_blob=_b64encode(v.passphrase_blob),
            recovery_salt=_b64encode(v.recovery_salt),
            recovery_iv=_b64encode(v.recovery_iv),
            recovery_blob=_b64encode(v.recovery_blob),
            verifier_iv=_b64encode(v.verifier_iv),
            verifier_blob=_b64encode(v.verifier_blob),
            created_at=v.created_at,
            updated_at=v.updated_at,
        )

    @router.post("/api/vault", response_model=VaultResponse, status_code=201)
    async def vault_create(
        body: CreateVaultRequest,
        current_user: dict = Depends(get_current_user),
    ):
        # B-39: belt-and-suspenders server-side minimum check.
        # Pydantic ge=2 already rejects kdf_iterations < 2 with 422, but we
        # also check here to surface a clear 400 with a security-policy message.
        if body.kdf_iterations < 2:
            raise HTTPException(
                status_code=400,
                detail=(
                    "kdf_iterations must be \u2265 2 (OWASP 2023 argon2id minimum). "
                    "Values below the minimum are rejected server-side regardless "
                    "of client-supplied parameters."
                ),
            )
        try:
            kw = dict(
                passphrase_salt=_b64decode_strict(body.passphrase_salt, expected_min=16, expected_max=16, label="passphrase_salt"),
                passphrase_iv=_b64decode_strict(body.passphrase_iv, expected_min=12, expected_max=12, label="passphrase_iv"),
                passphrase_blob=_b64decode_strict(body.passphrase_blob, expected_min=16, expected_max=96, label="passphrase_blob"),
                recovery_salt=_b64decode_strict(body.recovery_salt, expected_min=16, expected_max=16, label="recovery_salt"),
                recovery_iv=_b64decode_strict(body.recovery_iv, expected_min=12, expected_max=12, label="recovery_iv"),
                recovery_blob=_b64decode_strict(body.recovery_blob, expected_min=16, expected_max=96, label="recovery_blob"),
                verifier_iv=_b64decode_strict(body.verifier_iv, expected_min=12, expected_max=12, label="verifier_iv"),
                verifier_blob=_b64decode_strict(body.verifier_blob, expected_min=16, expected_max=128, label="verifier_blob"),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        try:
            v = await create_vault(
                get_supabase_url(), get_service_key(),
                user_id=current_user["user_id"],
                kdf_memory_kib=body.kdf_memory_kib,
                kdf_iterations=body.kdf_iterations,
                kdf_parallelism=body.kdf_parallelism,
                **kw,
            )
        except VaultExists:
            raise HTTPException(409, "vault already exists")
        except VaultError as exc:
            logger.error("vault_create_failed", extra={"err": str(exc)})
            raise HTTPException(503, "vault unavailable")
        return VaultResponse(
            user_id=v.user_id,
            kdf_algorithm=v.kdf_algorithm,
            kdf_memory_kib=v.kdf_memory_kib,
            kdf_iterations=v.kdf_iterations,
            kdf_parallelism=v.kdf_parallelism,
            passphrase_salt=_b64encode(v.passphrase_salt),
            passphrase_iv=_b64encode(v.passphrase_iv),
            passphrase_blob=_b64encode(v.passphrase_blob),
            recovery_salt=_b64encode(v.recovery_salt),
            recovery_iv=_b64encode(v.recovery_iv),
            recovery_blob=_b64encode(v.recovery_blob),
            verifier_iv=_b64encode(v.verifier_iv),
            verifier_blob=_b64encode(v.verifier_blob),
            created_at=v.created_at,
            updated_at=v.updated_at,
        )

    @router.delete("/api/vault", status_code=204)
    async def vault_delete(current_user: dict = Depends(get_current_user)):
        try:
            await delete_vault(
                get_supabase_url(), get_service_key(),
                user_id=current_user["user_id"],
            )
        except VaultError as exc:
            logger.error("vault_delete_failed", extra={"err": str(exc)})
            raise HTTPException(503, "vault unavailable")
        return None

    # ------------------------ secrets -------------------------------------

    @router.get("/api/vault/secrets", response_model=list[SecretResponse])
    async def secrets_list(current_user: dict = Depends(get_current_user)):
        try:
            rows = await list_secrets(
                get_supabase_url(), get_service_key(),
                user_id=current_user["user_id"],
            )
        except VaultError as exc:
            logger.error("secrets_list_failed", extra={"err": str(exc)})
            raise HTTPException(503, "vault unavailable")
        return [
            SecretResponse(
                id=r.id,
                name=r.name,
                description=r.description,
                value_iv=_b64encode(r.value_iv),
                value_blob=_b64encode(r.value_blob),
                preview_iv=_b64encode(r.preview_iv),
                preview_blob=_b64encode(r.preview_blob),
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r in rows
        ]

    @router.post("/api/vault/secrets", response_model=SecretResponse, status_code=201)
    async def secrets_create(
        body: CreateSecretRequest,
        current_user: dict = Depends(get_current_user),
    ):
        try:
            kw = dict(
                value_iv=_b64decode_strict(body.value_iv, expected_min=12, expected_max=12, label="value_iv"),
                value_blob=_b64decode_strict(body.value_blob, expected_min=16, expected_max=65552, label="value_blob"),
                preview_iv=_b64decode_strict(body.preview_iv, expected_min=12, expected_max=12, label="preview_iv"),
                preview_blob=_b64decode_strict(body.preview_blob, expected_min=16, expected_max=64, label="preview_blob"),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        try:
            r = await create_secret(
                get_supabase_url(), get_service_key(),
                user_id=current_user["user_id"],
                name=body.name,
                description=body.description,
                **kw,
            )
        except SecretExists:
            raise HTTPException(409, "secret with that name already exists")
        except VaultError as exc:
            logger.error("secret_create_failed", extra={"err": str(exc)})
            raise HTTPException(503, "vault unavailable")
        return SecretResponse(
            id=r.id, name=r.name, description=r.description,
            value_iv=_b64encode(r.value_iv), value_blob=_b64encode(r.value_blob),
            preview_iv=_b64encode(r.preview_iv), preview_blob=_b64encode(r.preview_blob),
            created_at=r.created_at, updated_at=r.updated_at,
        )

    @router.patch("/api/vault/secrets/{secret_id}", response_model=SecretResponse)
    async def secrets_update(
        secret_id: str,
        body: UpdateSecretRequest,
        current_user: dict = Depends(get_current_user),
    ):
        try:
            kw = dict(
                value_iv=_b64decode_strict(body.value_iv, expected_min=12, expected_max=12, label="value_iv"),
                value_blob=_b64decode_strict(body.value_blob, expected_min=16, expected_max=65552, label="value_blob"),
                preview_iv=_b64decode_strict(body.preview_iv, expected_min=12, expected_max=12, label="preview_iv"),
                preview_blob=_b64decode_strict(body.preview_blob, expected_min=16, expected_max=64, label="preview_blob"),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        try:
            r = await update_secret(
                get_supabase_url(), get_service_key(),
                user_id=current_user["user_id"],
                secret_id=secret_id,
                description=body.description,
                **kw,
            )
        except SecretNotFound:
            raise HTTPException(404, "secret not found")
        except VaultError as exc:
            logger.error("secret_update_failed", extra={"err": str(exc)})
            raise HTTPException(503, "vault unavailable")
        return SecretResponse(
            id=r.id, name=r.name, description=r.description,
            value_iv=_b64encode(r.value_iv), value_blob=_b64encode(r.value_blob),
            preview_iv=_b64encode(r.preview_iv), preview_blob=_b64encode(r.preview_blob),
            created_at=r.created_at, updated_at=r.updated_at,
        )

    @router.delete("/api/vault/secrets/{secret_id}", status_code=204)
    async def secrets_delete(
        secret_id: str,
        current_user: dict = Depends(get_current_user),
    ):
        try:
            await delete_secret(
                get_supabase_url(), get_service_key(),
                user_id=current_user["user_id"],
                secret_id=secret_id,
            )
        except VaultError as exc:
            logger.error("secret_delete_failed", extra={"err": str(exc)})
            raise HTTPException(503, "vault unavailable")
        return None

    return router
