"""FastAPI router for Deft credit + quote endpoints.

Endpoints:
  GET  /api/credits/balance         -> {balance, next_expiry}
  GET  /api/credits/transactions    -> [{...}, ...]   (last 50)
  POST /api/agent/quote             -> {tier, credits_min, credits_max, eta_*}

All routes require authentication. Uses dependency-injection style so the
parent api.py can wire up its own auth/db helpers without coupling.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict

from .ledger import LedgerError, get_balance
from .quote import ModelTier, estimate_quote

logger = logging.getLogger(__name__)


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(..., min_length=1, max_length=20_000)
    tier: ModelTier = "standard"
    max_credits: Optional[int] = Field(None, ge=1, le=1_000_000)


class QuoteResponse(BaseModel):
    tier: str
    credits_min: int
    credits_max: int
    eta_seconds_min: int
    eta_seconds_max: int
    complexity_score: float
    breakdown: dict[str, Any]


class BalanceResponse(BaseModel):
    balance: int
    next_expiry: Optional[str] = None


def build_billing_router(
    *,
    get_current_user: Callable[..., Awaitable[dict[str, Any]]],
    get_supabase_url: Callable[[], str],
    get_service_key: Callable[[], str],
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/credits/balance", response_model=BalanceResponse)
    async def credits_balance(current_user: dict = Depends(get_current_user)):
        try:
            bal = await get_balance(
                supabase_url=get_supabase_url(),
                service_key=get_service_key(),
                user_id=current_user["user_id"],
            )
        except LedgerError as exc:
            logger.error("balance_read_failed", extra={"err": str(exc)})
            raise HTTPException(status_code=503, detail="balance unavailable")
        return BalanceResponse(balance=bal.balance, next_expiry=bal.next_expiry)

    @router.get("/api/credits/transactions")
    async def credits_transactions(
        current_user: dict = Depends(get_current_user),
        limit: int = 50,
    ):
        """Return the last ``limit`` (<=200) transactions for the current user."""
        if limit <= 0 or limit > 200:
            raise HTTPException(status_code=400, detail="limit must be 1..200")
        url = f"{get_supabase_url().rstrip('/')}/rest/v1/credit_transactions"
        params = {
            "user_id": f"eq.{current_user['user_id']}",
            "select": "id,type,credits,bucket_id,ref_type,ref_id,balance_after,metadata,created_at",
            "order": "created_at.desc",
            "limit": str(limit),
        }
        headers = {
            "apikey": get_service_key(),
            "Authorization": f"Bearer {get_service_key()}",
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            logger.error("transactions_network_error", extra={"err": str(exc)})
            raise HTTPException(status_code=503, detail="transactions unavailable")
        if resp.status_code != 200:
            logger.error(
                "transactions_read_failed",
                extra={"status": resp.status_code, "body": resp.text[:200]},
            )
            raise HTTPException(status_code=503, detail="transactions unavailable")
        return resp.json()

    @router.post("/api/agent/quote", response_model=QuoteResponse)
    async def agent_quote(
        body: QuoteRequest,
        current_user: dict = Depends(get_current_user),
    ):
        try:
            q = estimate_quote(
                prompt=body.prompt,
                tier=body.tier,
                max_credits=body.max_credits,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return QuoteResponse(**q.to_dict())

    return router
