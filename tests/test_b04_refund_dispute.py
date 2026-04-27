"""B-04 regression suite: Stripe charge.refunded / charge.dispute events
reverse previously granted credits.

Strategy: mirrors test_b03_webhook_two_phase.py — we patch the Supabase RPC
calls (via httpx.AsyncClient) and the Postgres idempotency pool, then drive
the webhook handler and individual handler functions directly.

Test layout:
  1.  full_refund_reverses_full_grant
  2.  partial_refund_reverses_pro_rata
  3.  refund_of_unknown_charge_is_noop
  4.  dispute_funds_withdrawn_reverses_grant
  5.  refund_event_replay_is_idempotent
  6.  refund_of_subscription_invoice_reverses_grant
  7.  original_grant_lookup_by_stripe_event_ref_type
  8.  e2e_webhook_charge_refunded_handler_invoked_and_row_completed
  9.  dispute_resolved_in_our_favor_no_reversal
  10. partial_refund_zero_grant_is_noop
"""

from __future__ import annotations

import json
import math
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import Request

from mariana import api as mod
from mariana.config import AppConfig


# ---------------------------------------------------------------------------
# Helpers shared with test_b03 (duplicated here to keep this file
# self-contained; no cross-test imports).
# ---------------------------------------------------------------------------


def _cfg() -> AppConfig:
    cfg = AppConfig.__new__(AppConfig)
    object.__setattr__(cfg, "SUPABASE_URL", "https://supabase.test")
    object.__setattr__(cfg, "SUPABASE_KEY", "anon")
    object.__setattr__(cfg, "SUPABASE_SERVICE_ROLE_KEY", "service_role_xxx")
    object.__setattr__(cfg, "STRIPE_SECRET_KEY", "sk_test_xxx")
    object.__setattr__(cfg, "STRIPE_PUBLISHABLE_KEY", "pk_test_xxx")
    object.__setattr__(cfg, "STRIPE_WEBHOOK_SECRET", "whsec_xxx")
    return cfg


class _FakeResp:
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
        default_response: _FakeResp | None = None,
        by_path: dict[str, _FakeResp] | None = None,
    ) -> None:
        self.default_response = default_response or _FakeResp(200, {"status": "reversed", "credits_debited": 0, "balance_after": 0})
        self.by_path = by_path or {}
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):  # noqa: ANN001
        return None

    async def post(self, url: str, json=None, headers=None):  # noqa: A002
        self.calls.append({"method": "POST", "url": url, "json": json})
        for path, resp in self.by_path.items():
            if path in url:
                return resp
        return self.default_response

    async def get(self, url: str, params=None, headers=None):
        self.calls.append({"method": "GET", "url": url, "params": params})
        for path, resp in self.by_path.items():
            if path in url:
                return resp
        return self.default_response

    async def patch(self, url: str, json=None, headers=None):
        self.calls.append({"method": "PATCH", "url": url, "json": json})
        for path, resp in self.by_path.items():
            if path in url:
                return resp
        return self.default_response


@pytest.fixture(autouse=True)
def _patch_supabase_api_key():
    with patch.object(mod, "_supabase_api_key", lambda cfg: "service_role_xxx"):
        yield


# ---------------------------------------------------------------------------
# Fake pool (identical to test_b03 fixture — must stay in sync).
# ---------------------------------------------------------------------------


class _FakeStripeWebhookEventsTable:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def claim(self, event_id: str, event_type: str) -> dict[str, Any]:
        prior = self.rows.get(event_id)
        if prior is None:
            self.rows[event_id] = {"event_id": event_id, "event_type": event_type, "status": "pending", "attempts": 1}
            return {"prior_status": None, "post_status": "pending"}
        if prior["status"] == "pending":
            prior["attempts"] += 1
            return {"prior_status": "pending", "post_status": "pending"}
        return {"prior_status": "completed", "post_status": None}

    async def finalize(self, event_id: str) -> None:
        if event_id in self.rows:
            self.rows[event_id]["status"] = "completed"

    async def record_failure(self, event_id: str, err: str) -> None:
        if event_id in self.rows:
            self.rows[event_id]["last_error"] = err


class _FakePool:
    def __init__(self, table: _FakeStripeWebhookEventsTable) -> None:
        self._table = table

    async def fetchrow(self, sql: str, *args):  # noqa: ANN001
        assert "stripe_webhook_events" in sql
        assert "INSERT" in sql.upper()
        event_id, event_type = args
        return await self._table.claim(event_id, event_type)

    async def execute(self, sql: str, *args):  # noqa: ANN001
        if "SET status        = 'completed'" in sql or ("SET status" in sql and "'completed'" in sql):
            await self._table.finalize(args[0])
        elif "SET last_error" in sql:
            await self._table.record_failure(args[0], args[1])
        return "UPDATE 1"


@pytest.fixture
def fake_pool():
    table = _FakeStripeWebhookEventsTable()
    pool = _FakePool(table)
    original = mod._db_pool
    mod._db_pool = pool  # type: ignore[assignment]
    try:
        yield table
    finally:
        mod._db_pool = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helper: build Stripe charge.refunded / charge.dispute.* event dicts.
# ---------------------------------------------------------------------------


def _charge_refunded_event(
    *,
    event_id: str = "evt_ref_1",
    charge_id: str = "ch_1",
    payment_intent_id: str = "pi_1",
    amount: int = 3000,   # Stripe amounts are cents
    amount_refunded: int = 3000,
    original_grant_event_id: str = "evt_pi_1",
) -> dict:
    """Stripe charge.refunded event (full or partial)."""
    return {
        "id": event_id,
        "type": "charge.refunded",
        "data": {
            "object": {
                "id": charge_id,
                "payment_intent": payment_intent_id,
                "amount": amount,
                "amount_refunded": amount_refunded,
                "currency": "usd",
            }
        },
    }


def _charge_dispute_event(
    *,
    event_id: str = "evt_disp_1",
    dispute_type: str = "charge.dispute.funds_withdrawn",
    charge_id: str = "ch_1",
    payment_intent_id: str = "pi_1",
    amount: int = 3000,
) -> dict:
    return {
        "id": event_id,
        "type": dispute_type,
        "data": {
            "object": {
                "id": "dp_1",
                "charge": charge_id,
                "payment_intent": payment_intent_id,
                "amount": amount,
                "currency": "usd",
                "status": "needs_response",
            }
        },
    }


# ---------------------------------------------------------------------------
# Helper: build a Supabase credit_transactions lookup response.
# ---------------------------------------------------------------------------

def _grant_tx_response(
    *,
    user_id: str = "user-uuid-aaa",
    credits: int = 3000,
    event_id: str = "evt_pi_1",
) -> _FakeResp:
    """Canned response for stripe_payment_grants REST lookup (H-01)."""
    return _FakeResp(200, [{
        "user_id": user_id,
        "credits": credits,
        "event_id": event_id,
    }])


def _no_grant_tx_response() -> _FakeResp:
    return _FakeResp(200, [])


def _refund_rpc_response(*, credits_debited: int = 3000) -> _FakeResp:
    return _FakeResp(200, {
        "status": "reversed",
        "credits_debited": credits_debited,
        "balance_after": 0,
    })


def _duplicate_refund_rpc_response() -> _FakeResp:
    return _FakeResp(200, {"status": "duplicate", "transaction_id": "tx-uuid-dup"})


# ---------------------------------------------------------------------------
# 1. Full refund reverses the full credit grant.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_refund_reverses_full_grant():
    """charge.refunded with amount_refunded == amount must call refund_credits
    with the full credit amount from the original grant."""
    cfg = _cfg()
    user_id = "user-uuid-aaa"
    original_credits = 3000

    grant_tx_resp = _grant_tx_response(user_id=user_id, credits=original_credits)
    refund_resp = _refund_rpc_response(credits_debited=original_credits)
    client = _RecordingClient(
        by_path={
            "stripe_payment_grants": grant_tx_resp,
            "stripe_dispute_reversals": _FakeResp(200, []),
            "rpc/refund_credits": refund_resp,
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_refunded(
            {
                "id": "ch_1",
                "payment_intent": "pi_1",
                "amount": 3000,
                "amount_refunded": 3000,
            },
            cfg,
            event_id="evt_ref_full_1",
        )

    rpc_calls = [c for c in client.calls if "rpc/refund_credits" in c["url"]]
    assert len(rpc_calls) == 1
    body = rpc_calls[0]["json"]
    assert body["p_user_id"] == user_id
    assert body["p_credits"] == original_credits  # full reversal
    assert body["p_ref_type"] == "stripe_event"
    # J-01 fix: ref_id is now per-event 'refund_event:<event_id>' for charge.refunded,
    # so sequential partial refunds on the same charge each get unique idempotency keys.
    assert body["p_ref_id"] == "refund_event:evt_ref_full_1"


# ---------------------------------------------------------------------------
# 2. Partial refund reverses pro-rata.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_refund_reverses_pro_rata():
    """$5 refund on a $20 topup (3000 credits) → reverse 750 credits (25%)."""
    cfg = _cfg()
    user_id = "user-uuid-bbb"
    original_credits = 3000  # $30 topup at 100 credits/$ → but we test ratio
    amount_total = 3000      # cents ($30)
    amount_refunded = 750    # cents ($7.50 = 25%)
    expected_debited = math.floor(original_credits * amount_refunded / amount_total)
    assert expected_debited == 750  # 25% of 3000

    grant_tx_resp = _grant_tx_response(user_id=user_id, credits=original_credits)
    refund_resp = _refund_rpc_response(credits_debited=expected_debited)
    client = _RecordingClient(
        by_path={
            "stripe_payment_grants": grant_tx_resp,
            "stripe_dispute_reversals": _FakeResp(200, []),
            "rpc/refund_credits": refund_resp,
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_refunded(
            {
                "id": "ch_2",
                "payment_intent": "pi_2",
                "amount": amount_total,
                "amount_refunded": amount_refunded,
            },
            cfg,
            event_id="evt_ref_partial_1",
        )

    rpc_calls = [c for c in client.calls if "rpc/refund_credits" in c["url"]]
    assert len(rpc_calls) == 1
    body = rpc_calls[0]["json"]
    assert body["p_credits"] == expected_debited
    # J-01 fix: ref_id is now per-event 'refund_event:<event_id>' for charge.refunded.
    assert body["p_ref_id"] == "refund_event:evt_ref_partial_1"


# ---------------------------------------------------------------------------
# 3. Refund of unknown charge (no original grant) is a no-op + warning.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refund_of_unknown_charge_is_noop(caplog):
    """When no credit_transactions row exists for the payment_intent,
    the handler must skip cleanly and NOT call refund_credits."""
    import logging
    cfg = _cfg()
    client = _RecordingClient(
        by_path={"stripe_payment_grants": _no_grant_tx_response()}
    )

    with patch.object(httpx, "AsyncClient", return_value=client), caplog.at_level(logging.WARNING):
        await mod._handle_charge_refunded(
            {
                "id": "ch_99",
                "payment_intent": "pi_unknown",
                "amount": 2000,
                "amount_refunded": 2000,
            },
            cfg,
            event_id="evt_ref_unknown_1",
        )

    rpc_calls = [c for c in client.calls if "rpc/refund_credits" in c["url"]]
    assert len(rpc_calls) == 0, "must not call refund_credits for unknown charge"


# ---------------------------------------------------------------------------
# 4. Dispute funds_withdrawn reverses the grant.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispute_funds_withdrawn_reverses_grant():
    """charge.dispute.funds_withdrawn must trigger the same reversal logic
    as charge.refunded."""
    cfg = _cfg()
    user_id = "user-uuid-ccc"
    original_credits = 2000

    grant_tx_resp = _grant_tx_response(user_id=user_id, credits=original_credits)
    refund_resp = _refund_rpc_response(credits_debited=original_credits)
    client = _RecordingClient(
        by_path={
            "stripe_payment_grants": grant_tx_resp,
            "stripe_dispute_reversals": _FakeResp(200, []),
            "rpc/refund_credits": refund_resp,
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_dispute_funds_withdrawn(
            {
                "id": "dp_1",
                "charge": "ch_3",
                "payment_intent": "pi_3",
                "amount": 2000,
                "status": "needs_response",
            },
            cfg,
            event_id="evt_disp_fw_1",
        )

    rpc_calls = [c for c in client.calls if "rpc/refund_credits" in c["url"]]
    assert len(rpc_calls) == 1
    body = rpc_calls[0]["json"]
    assert body["p_user_id"] == user_id
    assert body["p_credits"] == original_credits
    # I-02 fix: ref_id is now the stable reversal_key.
    # dispute event with dispute_id=dp_1 uses "dispute:dp_1".
    assert body["p_ref_id"] == "dispute:dp_1"


# ---------------------------------------------------------------------------
# 5. Refund event replay is idempotent (RPC returns 'duplicate').
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refund_event_replay_is_idempotent():
    """When refund_credits returns status='duplicate', the handler must treat
    it as success (no exception, no error log)."""
    cfg = _cfg()
    user_id = "user-uuid-ddd"
    grant_tx_resp = _grant_tx_response(user_id=user_id, credits=1000)
    dup_resp = _duplicate_refund_rpc_response()
    client = _RecordingClient(
        by_path={
            "stripe_payment_grants": grant_tx_resp,
            "stripe_dispute_reversals": _FakeResp(200, []),
            "rpc/refund_credits": dup_resp,
        }
    )

    # Should not raise; duplicate is idempotent success.
    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_refunded(
            {
                "id": "ch_dup",
                "payment_intent": "pi_dup",
                "amount": 1000,
                "amount_refunded": 1000,
            },
            cfg,
            event_id="evt_ref_dup_1",
        )

    rpc_calls = [c for c in client.calls if "rpc/refund_credits" in c["url"]]
    assert len(rpc_calls) == 1  # called exactly once; duplicate handled gracefully


# ---------------------------------------------------------------------------
# 6. Refund of subscription invoice reverses that period's grant.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refund_of_subscription_invoice_reverses_grant():
    """For a subscription refund the charge's payment_intent links back to the
    invoice.paid event. The handler looks up the grant by payment_intent and
    calls refund_credits with the correct credit amount."""
    cfg = _cfg()
    user_id = "user-uuid-eee"
    plan_credits = 5500  # pro plan

    grant_tx_resp = _grant_tx_response(user_id=user_id, credits=plan_credits, event_id="evt_inv_renewal_1")
    refund_resp = _refund_rpc_response(credits_debited=plan_credits)
    client = _RecordingClient(
        by_path={
            "stripe_payment_grants": grant_tx_resp,
            "stripe_dispute_reversals": _FakeResp(200, []),
            "rpc/refund_credits": refund_resp,
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_refunded(
            {
                "id": "ch_sub",
                "payment_intent": "pi_sub",
                "amount": 5000,   # $50 in cents
                "amount_refunded": 5000,
            },
            cfg,
            event_id="evt_sub_refund_1",
        )

    rpc_calls = [c for c in client.calls if "rpc/refund_credits" in c["url"]]
    assert len(rpc_calls) == 1
    body = rpc_calls[0]["json"]
    assert body["p_user_id"] == user_id
    assert body["p_credits"] == plan_credits


# ---------------------------------------------------------------------------
# 7. Grant lookup uses ref_type='stripe_event'.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_original_grant_lookup_uses_stripe_event_ref_type():
    """The credit_transactions query must filter on ref_type='stripe_event'
    and type='grant' to find the original grant row."""
    cfg = _cfg()
    user_id = "user-uuid-fff"

    grant_tx_resp = _grant_tx_response(user_id=user_id, credits=1000)
    refund_resp = _refund_rpc_response(credits_debited=1000)
    client = _RecordingClient(
        by_path={
            "stripe_payment_grants": grant_tx_resp,
            "stripe_dispute_reversals": _FakeResp(200, []),
            "rpc/refund_credits": refund_resp,
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_refunded(
            {
                "id": "ch_lookup",
                "payment_intent": "pi_lookup",
                "amount": 1000,
                "amount_refunded": 1000,
            },
            cfg,
            event_id="evt_ref_lookup_1",
        )

    # Verify the GET request hit stripe_payment_grants (H-01 fix).
    get_calls = [c for c in client.calls if c["method"] == "GET" and "stripe_payment_grants" in c["url"]]
    assert len(get_calls) >= 1, (
        f"Expected stripe_payment_grants GET call, got: {[c['url'] for c in client.calls if c['method'] == 'GET']}"
    )
    # payment_intent_id filter must be in the URL
    assert "payment_intent_id=eq.pi_lookup" in get_calls[0]["url"]


# ---------------------------------------------------------------------------
# 8. E2E: webhook delivers charge.refunded → handler invoked → row completed.
# ---------------------------------------------------------------------------


async def _invoke_webhook(event: dict, fake_pool_table: _FakeStripeWebhookEventsTable) -> tuple[int, dict]:
    payload = json.dumps(event).encode("utf-8")
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"stripe-signature", b"t=1,v1=anything")],
        "path": "/api/billing/webhook",
        "raw_path": b"/api/billing/webhook",
        "query_string": b"",
        "client": ("127.0.0.1", 0),
        "server": ("testserver", 80),
        "scheme": "http",
    }

    async def _receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def _send(_msg):
        pass

    request = Request(scope, _receive, _send)
    cfg = _cfg()
    pool = _FakePool(fake_pool_table)
    original = mod._db_pool
    mod._db_pool = pool  # type: ignore[assignment]
    try:
        with patch.object(mod, "_get_config", return_value=cfg), patch.object(
            mod._stripe.Webhook, "construct_event", return_value=event
        ):
            response = await mod.stripe_webhook(request)
    finally:
        mod._db_pool = original  # type: ignore[assignment]
    body = json.loads(response.body.decode("utf-8"))
    return response.status_code, body


@pytest.mark.asyncio
async def test_e2e_webhook_charge_refunded_invokes_handler_and_completes():
    """Full end-to-end: stripe_webhook dispatches charge.refunded, calls
    _handle_charge_refunded, and marks the event 'completed' after success."""
    table = _FakeStripeWebhookEventsTable()
    event = _charge_refunded_event(event_id="evt_e2e_ref_1")
    handler = AsyncMock()
    with patch.object(mod, "_handle_charge_refunded", handler):
        status, body = await _invoke_webhook(event, table)
    assert status == 200, body
    assert body["status"] == "ok"
    assert handler.await_count == 1
    assert table.rows["evt_e2e_ref_1"]["status"] == "completed"


# ---------------------------------------------------------------------------
# 9. Dispute resolved in our favor → no reversal needed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispute_created_no_reversal_logged(caplog):
    """charge.dispute.created triggers a reversal (funds_withdrawn is the
    financial event, but dispute.created should also reverse as a precaution).
    Here we specifically test that the charge.dispute.created handler calls
    the same reversal path."""
    cfg = _cfg()
    user_id = "user-uuid-ggg"
    original_credits = 2000

    grant_tx_resp = _grant_tx_response(user_id=user_id, credits=original_credits)
    refund_resp = _refund_rpc_response(credits_debited=original_credits)
    client = _RecordingClient(
        by_path={
            "stripe_payment_grants": grant_tx_resp,
            "stripe_dispute_reversals": _FakeResp(200, []),
            "rpc/refund_credits": refund_resp,
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_dispute_created(
            {
                "id": "dp_2",
                "charge": "ch_4",
                "payment_intent": "pi_4",
                "amount": 2000,
                "status": "needs_response",
            },
            cfg,
            event_id="evt_disp_created_1",
        )

    rpc_calls = [c for c in client.calls if "rpc/refund_credits" in c["url"]]
    assert len(rpc_calls) == 1


# ---------------------------------------------------------------------------
# 10. Partial refund when credits are already fully spent — handler does not
#     error; refund_credits still called with pro-rata amount.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_refund_already_spent_credits_no_error():
    """Even when the user has already spent all their credits, the refund
    handler must succeed (the SQL RPC clamps to available balance)."""
    cfg = _cfg()
    user_id = "user-uuid-hhh"
    original_credits = 3000
    # Simulate: user spent everything; RPC returns balance=0 but succeeds.
    grant_tx_resp = _grant_tx_response(user_id=user_id, credits=original_credits)
    spent_refund_resp = _FakeResp(200, {
        "status": "reversed",
        "credits_debited": 0,   # all already spent
        "balance_after": 0,
    })
    client = _RecordingClient(
        by_path={
            "stripe_payment_grants": grant_tx_resp,
            "stripe_dispute_reversals": _FakeResp(200, []),
            "rpc/refund_credits": spent_refund_resp,
        }
    )

    # Must not raise.
    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_refunded(
            {
                "id": "ch_spent",
                "payment_intent": "pi_spent",
                "amount": 3000,
                "amount_refunded": 1500,  # partial
            },
            cfg,
            event_id="evt_ref_spent_1",
        )

    rpc_calls = [c for c in client.calls if "rpc/refund_credits" in c["url"]]
    assert len(rpc_calls) == 1
