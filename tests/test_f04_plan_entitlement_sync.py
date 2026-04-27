"""F-04 regression suite: plan/entitlement unification.

Phase E re-audit found that Stripe webhook handlers updated only
subscription_plan / subscription_status / subscription_current_period_end,
while investigation gating reads profiles.plan.  A downgrade or cancel
webhook therefore left profiles.plan='pro' (or 'max') and the user retained
premium entitlements indefinitely.

Fix: every webhook handler now computes the effective plan and includes it in
the update_profile_by_stripe_customer payload, backed by migration 008 which
adds plan to the RPC's SET list plus a one-shot reconcile pass.

Tests:
  1. test_invoice_paid_updates_plan_field
     - fire invoice.paid for subscription_plan=pro,status=active
     - assert profiles.plan == 'pro' sent to Supabase

  2. test_subscription_deleted_downgrades_plan_to_free
     - fire customer.subscription.deleted
     - assert profiles.plan == 'free' sent to Supabase

  3. test_subscription_canceled_status_downgrades_plan
     - fire customer.subscription.updated with status=canceled
     - assert profiles.plan == 'free' sent to Supabase

  4. test_past_due_keeps_plan_active
     - fire customer.subscription.updated with status=past_due
     - assert profiles.plan is the paid plan (NOT 'free')

  5. test_full_downgrade_flow_blocks_continuous_mode
     - after downgrade-to-free (profiles.plan='free'), a subsequent
       POST /api/investigations with continuous_mode=True returns 403.

Mocks Supabase HTTP.  No live Postgres or Stripe required.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mariana import api as mod
from mariana.config import AppConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg() -> AppConfig:
    cfg = AppConfig.__new__(AppConfig)
    object.__setattr__(cfg, "SUPABASE_URL", "https://supabase.test")
    object.__setattr__(cfg, "SUPABASE_KEY", "anon")
    object.__setattr__(cfg, "SUPABASE_SERVICE_ROLE_KEY", "service_role_key_xxx")
    object.__setattr__(cfg, "STRIPE_SECRET_KEY", "sk_test_xxx")
    object.__setattr__(cfg, "STRIPE_PUBLISHABLE_KEY", "pk_test_xxx")
    object.__setattr__(cfg, "STRIPE_WEBHOOK_SECRET", "whsec_xxx")
    return cfg


class _FakeAsyncResp:
    def __init__(self, status_code: int = 200, body: Any = None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self) -> Any:
        return self._body


class _RecordingClient:
    """Fake httpx.AsyncClient that records calls and returns canned responses."""

    def __init__(
        self,
        *,
        default_response: _FakeAsyncResp | None = None,
        by_path: dict[str, _FakeAsyncResp] | None = None,
    ) -> None:
        self.default_response = default_response or _FakeAsyncResp(200, {})
        self.by_path = by_path or {}
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):  # noqa: ANN001
        return None

    def _pick(self, url: str) -> _FakeAsyncResp:
        for path, resp in self.by_path.items():
            if path in url:
                return resp
        return self.default_response

    async def post(self, url: str, json=None, headers=None):  # noqa: A002
        self.calls.append({"method": "POST", "url": url, "json": json})
        return self._pick(url)

    async def get(self, url: str, params=None, headers=None):
        self.calls.append({"method": "GET", "url": url, "params": params})
        return self._pick(url)

    async def patch(self, url: str, json=None, headers=None):
        self.calls.append({"method": "PATCH", "url": url, "json": json})
        return self._pick(url)


@pytest.fixture(autouse=True)
def _patch_api_key():
    with patch.object(mod, "_supabase_api_key", lambda cfg: "service_role_key_xxx"):
        yield


# ---------------------------------------------------------------------------
# Helper: extract the plan field sent to update_profile_by_stripe_customer
# ---------------------------------------------------------------------------


def _plan_sent_to_rpc(calls: list[dict[str, Any]]) -> str | None:
    """Return the 'plan' value from the last call to update_profile_by_stripe_customer."""
    rpc_calls = [
        c for c in calls
        if "rpc/update_profile_by_stripe_customer" in c.get("url", "")
    ]
    if not rpc_calls:
        return None
    payload = (rpc_calls[-1].get("json") or {}).get("payload") or {}
    return payload.get("plan")


# ---------------------------------------------------------------------------
# Test 1: invoice.paid for an active subscription updates plan to 'pro'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoice_paid_updates_plan_field() -> None:
    """invoice.paid for an active subscription must set profiles.plan='pro'."""
    cfg = _cfg()
    pro = mod._PLAN_BY_ID["pro"]

    profile_resp = _FakeAsyncResp(200, [{"id": "user-uuid-f04"}])
    grant_resp = _FakeAsyncResp(200, {"status": "granted", "balance_after": 5500})
    rpc_resp = _FakeAsyncResp(200, {})

    client = _RecordingClient(
        by_path={
            "/rest/v1/profiles": profile_resp,
            "rpc/grant_credits": grant_resp,
            "rpc/update_profile_by_stripe_customer": rpc_resp,
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client), patch.object(
        mod, "_supabase_patch_profile_by_customer", new_callable=AsyncMock
    ) as patch_mock:
        await mod._handle_invoice_paid(
            {
                "id": "in_f04_1",
                "billing_reason": "subscription_cycle",
                "status": "paid",
                "customer": "cus_f04",
                "period_end": 1_800_000_000,
                "lines": {
                    "data": [
                        {
                            "price": {"id": pro["stripe_price_id"]},
                            "period": {"end": 1_800_000_000},
                        }
                    ]
                },
            },
            cfg,
            event_id="evt_f04_invoice_paid",
        )

    # The patch was called once with plan='pro' in the payload.
    assert patch_mock.await_count == 1
    call_args = patch_mock.call_args  # (customer_id, payload, cfg)
    payload = call_args[0][1]  # positional arg index 1
    assert payload.get("plan") == "pro", (
        f"Expected plan='pro' in Supabase patch, got: {payload}"
    )
    assert payload.get("subscription_status") == "active"
    assert payload.get("subscription_plan") == "pro"


# ---------------------------------------------------------------------------
# Test 2: customer.subscription.deleted must downgrade plan to 'free'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscription_deleted_downgrades_plan_to_free() -> None:
    """customer.subscription.deleted must set profiles.plan='free'."""
    cfg = _cfg()

    with patch.object(mod, "_supabase_patch_profile_by_customer", new_callable=AsyncMock) as patch_mock:
        await mod._handle_subscription_deleted(
            {
                "id": "sub_f04_del",
                "customer": "cus_f04_del",
                "status": "canceled",
            },
            cfg,
        )

    assert patch_mock.await_count == 1
    payload = patch_mock.call_args[0][1]
    assert payload.get("plan") == "free", (
        f"Expected plan='free' on deletion, got: {payload}"
    )
    assert payload.get("subscription_status") == "canceled"


# ---------------------------------------------------------------------------
# Test 3: customer.subscription.updated with status=canceled → plan='free'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscription_canceled_status_downgrades_plan() -> None:
    """customer.subscription.updated status=canceled must set profiles.plan='free'."""
    cfg = _cfg()
    pro = mod._PLAN_BY_ID["pro"]

    with patch.object(mod, "_supabase_patch_profile_by_customer", new_callable=AsyncMock) as patch_mock:
        await mod._handle_subscription_updated(
            {
                "id": "sub_f04_upd_cancel",
                "customer": "cus_f04_cancel",
                "status": "canceled",
                "current_period_end": 1_800_000_000,
                "items": {
                    "data": [
                        {"price": {"id": pro["stripe_price_id"]}}
                    ]
                },
            },
            cfg,
        )

    assert patch_mock.await_count == 1
    payload = patch_mock.call_args[0][1]
    assert payload.get("plan") == "free", (
        f"Expected plan='free' for canceled status, got: {payload}"
    )
    assert payload.get("subscription_status") == "canceled"


# ---------------------------------------------------------------------------
# Test 4: customer.subscription.updated with status=past_due keeps paid plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_past_due_keeps_plan_active() -> None:
    """past_due status must NOT downgrade the plan — keep it at the paid tier."""
    cfg = _cfg()
    pro = mod._PLAN_BY_ID["pro"]

    with patch.object(mod, "_supabase_patch_profile_by_customer", new_callable=AsyncMock) as patch_mock:
        await mod._handle_subscription_updated(
            {
                "id": "sub_f04_past_due",
                "customer": "cus_f04_pd",
                "status": "past_due",
                "current_period_end": 1_800_000_000,
                "items": {
                    "data": [
                        {"price": {"id": pro["stripe_price_id"]}}
                    ]
                },
            },
            cfg,
        )

    assert patch_mock.await_count == 1
    payload = patch_mock.call_args[0][1]
    assert payload.get("plan") == "pro", (
        f"Expected plan='pro' for past_due (grace window), got: {payload}"
    )
    assert payload.get("subscription_status") == "past_due"


# ---------------------------------------------------------------------------
# Test 5: after downgrade-to-free, continuous_mode=True is blocked (403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_downgrade_flow_blocks_continuous_mode() -> None:
    """After a downgrade that sets profiles.plan='free', continuous_mode is rejected."""
    import uuid
    from fastapi import HTTPException
    from unittest.mock import MagicMock

    user_id = "user-f04-downgrade"
    cfg = _cfg()
    object.__setattr__(cfg, "DATA_ROOT", "/tmp/f04_test_data_root")
    object.__setattr__(cfg, "DEFT_INBOX_DIR", "/tmp/f04_inbox")

    user = {"user_id": user_id, "email": "f04@test.com"}

    # Simulate a plan lookup that returns 'free' (post-downgrade)
    async def _fake_supabase_rest(cfg_, method, path, **kwargs):  # noqa: ANN001
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [{"plan": "free"}]
        return resp

    class _FakeRequest:
        def __init__(self) -> None:
            self.headers = {"authorization": "Bearer fake-jwt"}

        def __getattr__(self, name: str) -> Any:
            return None

    body = mod.StartInvestigationRequest(
        topic="Analyze market trends",
        continuous_mode=True,
        tier="instant",
    )

    fake_request = _FakeRequest()

    with (
        patch.object(mod, "_get_config", return_value=cfg),
        patch.object(mod, "_supabase_rest", side_effect=_fake_supabase_rest),
        patch.object(mod, "_supabase_rest_system", new_callable=AsyncMock),
        patch.object(mod, "_supabase_deduct_credits", new_callable=AsyncMock),
        patch.object(mod, "_supabase_add_credits", new_callable=AsyncMock),
        patch.object(mod, "_db_insert_research_task", new_callable=AsyncMock),
        patch.object(mod, "_get_db", return_value=MagicMock()),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await mod.start_investigation(fake_request, body, current_user=user)

    assert exc_info.value.status_code == 403
    assert "continuous" in exc_info.value.detail.lower() or "flagship" in exc_info.value.detail.lower(), (
        f"Expected 403 about continuous mode / flagship, got: {exc_info.value.detail}"
    )


# ---------------------------------------------------------------------------
# Test 6: _effective_plan unit tests
# ---------------------------------------------------------------------------


def test_effective_plan_active_returns_plan_slug() -> None:
    assert mod._effective_plan("active", "pro") == "pro"
    assert mod._effective_plan("trialing", "starter") == "starter"
    assert mod._effective_plan("past_due", "max") == "max"


def test_effective_plan_canceled_returns_free() -> None:
    assert mod._effective_plan("canceled", "pro") == "free"
    assert mod._effective_plan("unpaid", "max") == "free"
    assert mod._effective_plan("incomplete_expired", "starter") == "free"
    assert mod._effective_plan("paused", "pro") == "free"
    assert mod._effective_plan(None, "pro") == "free"


def test_effective_plan_unknown_plan_slug_returns_free() -> None:
    # Unrecognised plan slugs (e.g. raw Stripe price IDs) fall back to 'free'.
    assert mod._effective_plan("active", "price_not_a_plan") == "free"
    assert mod._effective_plan("active", None) == "free"
    assert mod._effective_plan("active", "") == "free"


def test_effective_plan_all_known_plans_round_trip() -> None:
    for plan in mod._PLANS:
        assert mod._effective_plan("active", plan["id"]) == plan["id"]
        assert mod._effective_plan("canceled", plan["id"]) == "free"
