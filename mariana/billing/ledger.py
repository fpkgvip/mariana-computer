"""Async wrappers around the Postgres credit-ledger RPCs.

All functions require Supabase service-role credentials. The RPCs themselves
are SECURITY DEFINER and only granted to ``service_role``; calling them with
a user JWT will fail with a 403/permission error.

CRITICAL invariants (enforced by the RPC implementation, not by Python):
  - amounts are non-negative integers (1 credit == $0.01)
  - per-user serialization via pg_advisory_xact_lock
  - append-only ledger (no UPDATE on credit_transactions)
  - grants are idempotent on (ref_type, ref_id)
  - spend never goes negative; partial debits are rolled back
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Optional

import httpx

logger = logging.getLogger(__name__)


class LedgerError(Exception):
    """Raised when the credit-ledger RPC fails for a non-business-logic reason."""


class InsufficientBalance(Exception):
    """Raised when a spend is rejected due to insufficient balance."""

    def __init__(self, balance: int, requested: int) -> None:
        super().__init__(
            f"insufficient balance: have {balance}, requested {requested}"
        )
        self.balance = balance
        self.requested = requested


@dataclass(frozen=True)
class Balance:
    balance: int
    next_expiry: Optional[str]


def _headers(service_key: str) -> dict[str, str]:
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "params=single-object,return=representation",
    }


async def _rpc(
    supabase_url: str,
    service_key: str,
    name: str,
    payload: dict[str, Any],
    *,
    timeout: float = 10.0,
) -> Any:
    if not supabase_url or not service_key:
        raise LedgerError("supabase service credentials missing")
    url = f"{supabase_url.rstrip('/')}/rest/v1/rpc/{name}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=_headers(service_key))
    except httpx.HTTPError as exc:
        logger.error("ledger_rpc_network_error", extra={"rpc": name, "err": str(exc)})
        raise LedgerError(f"network error calling {name}: {exc}") from exc

    if resp.status_code != 200:
        logger.error(
            "ledger_rpc_error",
            extra={"rpc": name, "status": resp.status_code, "body": resp.text[:500]},
        )
        raise LedgerError(
            f"{name} returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise LedgerError(f"{name} returned non-JSON body") from exc


async def grant_credits(
    *,
    supabase_url: str,
    service_key: str,
    user_id: str,
    credits: int,
    source: Literal[
        "signup_grant", "plan_renewal", "topup", "admin_grant", "refund"
    ],
    ref_type: Optional[str] = None,
    ref_id: Optional[str] = None,
    expires_at: Optional[str] = None,
) -> dict[str, Any]:
    """Grant ``credits`` (integer) to ``user_id``. Idempotent on (ref_type, ref_id)."""
    if not isinstance(credits, int) or credits <= 0:
        raise ValueError(f"credits must be a positive integer, got {credits!r}")
    return await _rpc(
        supabase_url,
        service_key,
        "grant_credits",
        {
            "p_user_id": user_id,
            "p_credits": credits,
            "p_source": source,
            "p_ref_type": ref_type,
            "p_ref_id": ref_id,
            "p_expires_at": expires_at,
        },
    )


async def spend_credits(
    *,
    supabase_url: str,
    service_key: str,
    user_id: str,
    credits: int,
    ref_type: str,
    ref_id: str,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Spend ``credits`` from FIFO buckets. Raises InsufficientBalance if short."""
    if not isinstance(credits, int) or credits <= 0:
        raise ValueError(f"credits must be a positive integer, got {credits!r}")
    result = await _rpc(
        supabase_url,
        service_key,
        "spend_credits",
        {
            "p_user_id": user_id,
            "p_credits": credits,
            "p_ref_type": ref_type,
            "p_ref_id": ref_id,
            "p_metadata": metadata or {},
        },
    )
    if isinstance(result, dict) and result.get("status") == "insufficient_balance":
        raise InsufficientBalance(
            balance=int(result.get("balance", 0)),
            requested=int(result.get("requested", credits)),
        )
    return result


async def refund_credits(
    *,
    supabase_url: str,
    service_key: str,
    user_id: str,
    credits: int,
    ref_type: str,
    ref_id: str,
) -> dict[str, Any]:
    """Refund ``credits`` to a new bucket. Idempotent on (ref_type, ref_id)."""
    if not isinstance(credits, int) or credits <= 0:
        raise ValueError(f"credits must be a positive integer, got {credits!r}")
    return await _rpc(
        supabase_url,
        service_key,
        "refund_credits",
        {
            "p_user_id": user_id,
            "p_credits": credits,
            "p_ref_type": ref_type,
            "p_ref_id": ref_id,
        },
    )


async def get_balance(
    *,
    supabase_url: str,
    service_key: str,
    user_id: str,
) -> Balance:
    """Read a user's current credit balance via direct view query (service role)."""
    if not supabase_url or not service_key:
        raise LedgerError("supabase service credentials missing")
    url = f"{supabase_url.rstrip('/')}/rest/v1/credit_balances"
    params = {"user_id": f"eq.{user_id}", "select": "balance,next_expiry"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, params=params, headers=_headers(service_key))
    except httpx.HTTPError as exc:
        raise LedgerError(f"balance read network error: {exc}") from exc
    if resp.status_code != 200:
        raise LedgerError(
            f"balance read returned {resp.status_code}: {resp.text[:200]}"
        )
    rows = resp.json()
    if not rows:
        return Balance(balance=0, next_expiry=None)
    row = rows[0]
    return Balance(balance=int(row["balance"]), next_expiry=row.get("next_expiry"))
