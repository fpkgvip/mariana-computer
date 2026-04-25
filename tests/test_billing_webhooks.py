"""Unit tests for the Stripe webhook handlers.

Mocks the outbound Supabase REST calls (and the small subset of Stripe SDK
calls used by the handlers). Exercises:
  - subscription create credits
  - invoice.paid renewal grant + first-invoice skip
  - payment_intent.succeeded top-up grant
  - non-Deft payment_intents are ignored
  - replays are idempotent (ref_type='stripe_event', ref_id=event.id)
  - missing user resolutions are no-ops
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mariana import api as mod
from mariana.config import AppConfig


def _cfg() -> AppConfig:
    """Synthesize an AppConfig with just enough fields for the handlers."""
    cfg = AppConfig.__new__(AppConfig)
    object.__setattr__(cfg, "SUPABASE_URL", "https://supabase.test")
    object.__setattr__(cfg, "SUPABASE_KEY", "anon")
    object.__setattr__(cfg, "SUPABASE_SERVICE_ROLE_KEY", "service_role_key_xxx")
    object.__setattr__(cfg, "STRIPE_SECRET_KEY", "sk_test_xxx")
    object.__setattr__(cfg, "STRIPE_PUBLISHABLE_KEY", "pk_test_xxx")
    object.__setattr__(cfg, "STRIPE_WEBHOOK_SECRET", "whsec_xxx")
    return cfg


class _FakeAsyncResp:
    def __init__(self, status_code: int, body: Any = None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {"status": "granted"}
        self.text = json.dumps(self._body)

    def json(self) -> Any:
        return self._body


class _RecordingClient:
    """Stand-in for httpx.AsyncClient that records all requests issued."""

    def __init__(self, *, response: _FakeAsyncResp | None = None, by_path: dict | None = None):
        self.response = response or _FakeAsyncResp(200, {"status": "granted", "balance_after": 100})
        self.by_path = by_path or {}
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):  # noqa: ANN001
        return None

    async def post(self, url: str, json=None, headers=None):  # noqa: A002
        self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers})
        for path, resp in self.by_path.items():
            if path in url:
                return resp
        return self.response

    async def get(self, url: str, params=None, headers=None):
        self.calls.append({"method": "GET", "url": url, "params": params, "headers": headers})
        for path, resp in self.by_path.items():
            if path in url:
                return resp
        return self.response

    async def patch(self, url: str, json=None, headers=None):
        self.calls.append({"method": "PATCH", "url": url, "json": json, "headers": headers})
        for path, resp in self.by_path.items():
            if path in url:
                return resp
        return self.response


@pytest.fixture(autouse=True)
def _patch_supabase_api_key():
    with patch.object(mod, "_supabase_api_key", lambda cfg: "service_role_key_xxx"):
        yield


@pytest.mark.asyncio
async def test_payment_intent_topup_grants_credits():
    cfg = _cfg()
    client = _RecordingClient()
    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_payment_intent_succeeded(
            {
                "id": "pi_1",
                "metadata": {
                    "deft_kind": "topup",
                    "deft_plan_id": "topup_pro",
                    "user_id": "00000000-0000-0000-0000-000000000abc",
                },
            },
            cfg,
            event_id="evt_topup_1",
        )
    rpc_calls = [c for c in client.calls if "rpc/grant_credits" in c["url"]]
    assert len(rpc_calls) == 1
    body = rpc_calls[0]["json"]
    assert body["p_credits"] == 3000
    assert body["p_source"] == "topup"
    assert body["p_ref_type"] == "stripe_event"
    assert body["p_ref_id"] == "evt_topup_1"


@pytest.mark.asyncio
async def test_payment_intent_non_topup_ignored():
    cfg = _cfg()
    client = _RecordingClient()
    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_payment_intent_succeeded(
            {"id": "pi_1", "metadata": {"deft_kind": "subscription"}},
            cfg,
            event_id="evt_2",
        )
    assert all("rpc/grant_credits" not in c["url"] for c in client.calls)


@pytest.mark.asyncio
async def test_payment_intent_topup_unknown_plan_skipped():
    cfg = _cfg()
    client = _RecordingClient()
    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_payment_intent_succeeded(
            {
                "id": "pi_x",
                "metadata": {
                    "deft_kind": "topup",
                    "deft_plan_id": "topup_does_not_exist",
                    "user_id": "uid",
                },
            },
            cfg,
            event_id="evt_3",
        )
    assert all("rpc/grant_credits" not in c["url"] for c in client.calls)


@pytest.mark.asyncio
async def test_invoice_paid_skip_first_invoice():
    cfg = _cfg()
    client = _RecordingClient()
    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_invoice_paid(
            {
                "id": "in_1",
                "billing_reason": "subscription_create",
                "status": "paid",
            },
            cfg,
            event_id="evt_4",
        )
    assert all("rpc/grant_credits" not in c["url"] for c in client.calls)


@pytest.mark.asyncio
async def test_invoice_paid_renewal_grants_credits():
    cfg = _cfg()
    starter = mod._PLAN_BY_ID["starter"]
    profile_resp = _FakeAsyncResp(200, [{"id": "user-uuid-aaa"}])
    grant_resp = _FakeAsyncResp(200, {"status": "granted", "balance_after": 2000})
    patch_resp = _FakeAsyncResp(200, [{"id": "user-uuid-aaa"}])
    client = _RecordingClient(
        by_path={
            "/rest/v1/profiles": profile_resp,  # first lookup; PATCH later also matches
            "rpc/grant_credits": grant_resp,
        }
    )
    with patch.object(httpx, "AsyncClient", return_value=client), patch.object(
        mod, "_supabase_patch_profile_by_customer", AsyncMock()
    ) as patch_mock:
        await mod._handle_invoice_paid(
            {
                "id": "in_2",
                "billing_reason": "subscription_cycle",
                "status": "paid",
                "customer": "cus_abc",
                "period_end": 1_750_000_000,
                "lines": {
                    "data": [
                        {
                            "price": {"id": starter["stripe_price_id"]},
                            "period": {"end": 1_750_000_000},
                        }
                    ]
                },
            },
            cfg,
            event_id="evt_inv_renewal",
        )
    rpc_calls = [c for c in client.calls if "rpc/grant_credits" in c["url"]]
    assert len(rpc_calls) == 1
    body = rpc_calls[0]["json"]
    assert body["p_credits"] == starter["credits_per_month"]
    assert body["p_source"] == "plan_renewal"
    assert body["p_ref_id"] == "evt_inv_renewal"
    assert patch_mock.await_count == 1


@pytest.mark.asyncio
async def test_invoice_paid_unknown_customer_no_grant():
    cfg = _cfg()
    profile_resp = _FakeAsyncResp(200, [])  # no profile matched
    client = _RecordingClient(by_path={"/rest/v1/profiles": profile_resp})
    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_invoice_paid(
            {
                "id": "in_3",
                "billing_reason": "subscription_cycle",
                "status": "paid",
                "customer": "cus_unknown",
                "lines": {"data": []},
            },
            cfg,
            event_id="evt_inv_unknown",
        )
    assert all("rpc/grant_credits" not in c["url"] for c in client.calls)


@pytest.mark.asyncio
async def test_checkout_completed_topup_deferred():
    cfg = _cfg()
    client = _RecordingClient()
    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_checkout_completed(
            {
                "id": "cs_1",
                "mode": "payment",
                "metadata": {
                    "deft_kind": "topup",
                    "deft_plan_id": "topup_pro",
                    "user_id": "uid-x",
                },
            },
            cfg,
            event_id="evt_checkout_topup",
        )
    assert all("rpc/grant_credits" not in c["url"] for c in client.calls)


@pytest.mark.asyncio
async def test_checkout_completed_subscription_grants():
    cfg = _cfg()
    fake_sub = MagicMock()
    fake_sub.get.return_value = 1_750_000_000
    grant_resp = _FakeAsyncResp(200, {"status": "granted", "balance_after": 5500})
    client = _RecordingClient(by_path={"rpc/grant_credits": grant_resp})
    with patch.object(httpx, "AsyncClient", return_value=client), patch.object(
        mod, "_supabase_patch_profile", AsyncMock()
    ), patch.object(mod._stripe.Subscription, "retrieve", return_value=fake_sub):
        await mod._handle_checkout_completed(
            {
                "id": "cs_2",
                "mode": "subscription",
                "customer": "cus_2",
                "subscription": "sub_2",
                "metadata": {
                    "deft_kind": "subscription",
                    "deft_plan_id": "pro",
                    "user_id": "uid-y",
                },
            },
            cfg,
            event_id="evt_checkout_sub",
        )
    rpc_calls = [c for c in client.calls if "rpc/grant_credits" in c["url"]]
    assert len(rpc_calls) == 1
    body = rpc_calls[0]["json"]
    assert body["p_credits"] == mod._PLAN_BY_ID["pro"]["credits_per_month"]
    assert body["p_source"] == "plan_renewal"
    assert body["p_ref_id"] == "evt_checkout_sub"


def test_plans_and_topups_pricing_invariants():
    """1 credit == $0.01: prices are integer cents, integer credits."""
    for plan in mod._PLANS:
        # Allow .0 floats as long as they're exact cents
        cents = round(plan["price_usd_monthly"] * 100)
        assert abs(plan["price_usd_monthly"] * 100 - cents) < 1e-6
        assert isinstance(plan["credits_per_month"], int)
        assert plan["credits_per_month"] > 0
    for tu in mod._TOPUPS:
        cents = round(tu["price_usd"] * 100)
        assert abs(tu["price_usd"] * 100 - cents) < 1e-6
        assert isinstance(tu["credits"], int)
        assert tu["credits"] > 0


def test_three_deft_tiers():
    ids = [p["id"] for p in mod._PLANS]
    assert ids == ["starter", "pro", "max"]
    assert mod._PLAN_BY_ID["starter"]["price_usd_monthly"] == 20.0
    assert mod._PLAN_BY_ID["pro"]["price_usd_monthly"] == 50.0
    assert mod._PLAN_BY_ID["max"]["price_usd_monthly"] == 200.0
