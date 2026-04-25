"""Integration tests against the live Supabase credit ledger.

These tests hit the real RPCs on the dev project. They use a dedicated
testrunner user and clean up after themselves.

Skipped when SUPABASE_URL or SUPABASE_SERVICE_KEY are not set.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import httpx
import pytest

from mariana.billing.ledger import (
    InsufficientBalance,
    LedgerError,
    grant_credits,
    refund_credits,
    spend_credits,
    get_balance,
)


SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
TEST_USER_ID = os.environ.get(
    "DEFT_TEST_USER_ID", "0c9697d6-8bdd-4042-b8d4-59249393fa95"
)

pytestmark = pytest.mark.skipif(
    not SUPABASE_URL or not SUPABASE_SERVICE_KEY,
    reason="SUPABASE_URL/SUPABASE_SERVICE_KEY not configured",
)


async def _cleanup(user_id: str) -> None:
    """Delete all transactions + buckets for a user (test isolation only)."""
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        for table in ("credit_transactions", "credit_buckets"):
            await client.delete(
                f"{SUPABASE_URL}/rest/v1/{table}",
                params={"user_id": f"eq.{user_id}"},
                headers=headers,
            )


@pytest.fixture
async def clean_user():
    await _cleanup(TEST_USER_ID)
    yield TEST_USER_ID
    await _cleanup(TEST_USER_ID)


@pytest.mark.asyncio
async def test_grant_and_balance(clean_user):
    user_id = clean_user
    ref = f"test-{uuid.uuid4()}"
    res = await grant_credits(
        supabase_url=SUPABASE_URL,
        service_key=SUPABASE_SERVICE_KEY,
        user_id=user_id,
        credits=100,
        source="signup_grant",
        ref_type="test",
        ref_id=ref,
    )
    assert res["status"] == "granted"
    assert res["balance_after"] == 100

    bal = await get_balance(
        supabase_url=SUPABASE_URL, service_key=SUPABASE_SERVICE_KEY, user_id=user_id
    )
    assert bal.balance == 100


@pytest.mark.asyncio
async def test_idempotent_grant(clean_user):
    user_id = clean_user
    ref = f"idem-{uuid.uuid4()}"
    r1 = await grant_credits(
        supabase_url=SUPABASE_URL,
        service_key=SUPABASE_SERVICE_KEY,
        user_id=user_id,
        credits=50,
        source="topup",
        ref_type="test",
        ref_id=ref,
    )
    assert r1["status"] == "granted"
    r2 = await grant_credits(
        supabase_url=SUPABASE_URL,
        service_key=SUPABASE_SERVICE_KEY,
        user_id=user_id,
        credits=999,
        source="topup",
        ref_type="test",
        ref_id=ref,
    )
    assert r2["status"] == "duplicate"
    bal = await get_balance(
        supabase_url=SUPABASE_URL, service_key=SUPABASE_SERVICE_KEY, user_id=user_id
    )
    assert bal.balance == 50


@pytest.mark.asyncio
async def test_spend_fifo_multi_bucket(clean_user):
    user_id = clean_user
    # 3 grants of 50/30/20 in temporal order.
    for i, amount in enumerate((50, 30, 20)):
        await grant_credits(
            supabase_url=SUPABASE_URL,
            service_key=SUPABASE_SERVICE_KEY,
            user_id=user_id,
            credits=amount,
            source="topup",
            ref_type="test",
            ref_id=f"g{i}",
        )
    res = await spend_credits(
        supabase_url=SUPABASE_URL,
        service_key=SUPABASE_SERVICE_KEY,
        user_id=user_id,
        credits=60,
        ref_type="task",
        ref_id="taskA",
    )
    assert res["status"] == "spent"
    assert res["balance_after"] == 40
    # Should have spent across exactly 2 buckets (50 + 10).
    assert len(res["buckets"]) == 2
    assert res["buckets"][0]["credits"] == 50
    assert res["buckets"][1]["credits"] == 10


@pytest.mark.asyncio
async def test_spend_insufficient_raises(clean_user):
    user_id = clean_user
    await grant_credits(
        supabase_url=SUPABASE_URL,
        service_key=SUPABASE_SERVICE_KEY,
        user_id=user_id,
        credits=10,
        source="topup",
        ref_type="t",
        ref_id="x",
    )
    with pytest.raises(InsufficientBalance) as exc:
        await spend_credits(
            supabase_url=SUPABASE_URL,
            service_key=SUPABASE_SERVICE_KEY,
            user_id=user_id,
            credits=11,
            ref_type="task",
            ref_id="taskA",
        )
    assert exc.value.balance == 10
    assert exc.value.requested == 11


@pytest.mark.asyncio
async def test_refund_idempotent(clean_user):
    user_id = clean_user
    r1 = await refund_credits(
        supabase_url=SUPABASE_URL,
        service_key=SUPABASE_SERVICE_KEY,
        user_id=user_id,
        credits=5,
        ref_type="task",
        ref_id="taskA",
    )
    assert r1["status"] == "refunded"
    r2 = await refund_credits(
        supabase_url=SUPABASE_URL,
        service_key=SUPABASE_SERVICE_KEY,
        user_id=user_id,
        credits=5,
        ref_type="task",
        ref_id="taskA",
    )
    assert r2["status"] == "duplicate"


@pytest.mark.asyncio
async def test_concurrent_spend_serialization(clean_user):
    """Fire 20 concurrent spends; balance must never go negative."""
    user_id = clean_user
    await grant_credits(
        supabase_url=SUPABASE_URL,
        service_key=SUPABASE_SERVICE_KEY,
        user_id=user_id,
        credits=10,
        source="topup",
        ref_type="t",
        ref_id="seed",
    )
    # 20 concurrent spends of 1, only 10 can succeed.
    async def attempt(i: int):
        try:
            await spend_credits(
                supabase_url=SUPABASE_URL,
                service_key=SUPABASE_SERVICE_KEY,
                user_id=user_id,
                credits=1,
                ref_type="task",
                ref_id=f"c{i}",
            )
            return "ok"
        except InsufficientBalance:
            return "insufficient"

    results = await asyncio.gather(*(attempt(i) for i in range(20)))
    successes = sum(1 for r in results if r == "ok")
    failures = sum(1 for r in results if r == "insufficient")
    assert successes == 10
    assert failures == 10

    bal = await get_balance(
        supabase_url=SUPABASE_URL, service_key=SUPABASE_SERVICE_KEY, user_id=user_id
    )
    assert bal.balance == 0


@pytest.mark.asyncio
async def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        await grant_credits(
            supabase_url=SUPABASE_URL,
            service_key=SUPABASE_SERVICE_KEY,
            user_id=TEST_USER_ID,
            credits=0,
            source="topup",
        )
    with pytest.raises(ValueError):
        await spend_credits(
            supabase_url=SUPABASE_URL,
            service_key=SUPABASE_SERVICE_KEY,
            user_id=TEST_USER_ID,
            credits=-1,
            ref_type="t",
            ref_id="x",
        )
