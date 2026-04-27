"""H-02 regression suite: stripe_dispute_reversals dedup.

Bugs fixed:
- charge.dispute.created and charge.dispute.funds_withdrawn both calling
  _reverse_credits_for_charge results in double clawback. The fix adds a
  reversal_key (dispute:<id> or charge:<id>:reversal) dedup check via
  stripe_dispute_reversals table.

Test inventory (>=5):
  1.  dispute_created_then_funds_withdrawn_same_dispute_only_one_reversal
  2.  charge_refunded_then_dispute_created_both_process_different_keys
  3.  successful_reversal_inserts_into_stripe_dispute_reversals
  4.  preexisting_reversal_row_short_circuits_refund_rpc
  5.  reversal_key_formatting_dispute_vs_no_dispute_paths
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

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
        by_path_method: dict[tuple[str, str], _FakeResp] | None = None,
    ) -> None:
        self.default_response = default_response or _FakeResp(200, {})
        self.by_path = by_path or {}
        self.by_path_method = by_path_method or {}
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url: str, json=None, headers=None):
        self.calls.append({"method": "POST", "url": url, "json": json})
        for (path, method), resp in self.by_path_method.items():
            if method == "POST" and path in url:
                return resp
        for path, resp in self.by_path.items():
            if path in url:
                return resp
        return self.default_response

    async def get(self, url: str, params=None, headers=None):
        self.calls.append({"method": "GET", "url": url, "params": params})
        for (path, method), resp in self.by_path_method.items():
            if method == "GET" and path in url:
                return resp
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


def _grant_lookup_resp(user_id: str = "user-uuid-aaa", credits: int = 2000) -> _FakeResp:
    """Canned stripe_payment_grants lookup response."""
    return _FakeResp(200, [{
        "user_id": user_id,
        "credits": credits,
        "event_id": "evt_grant_1",
    }])


def _no_reversal_row() -> _FakeResp:
    """stripe_dispute_reversals: no existing row."""
    return _FakeResp(200, [])


def _existing_reversal_row(reversal_key: str = "dispute:dp_1") -> _FakeResp:
    """stripe_dispute_reversals: row exists for this key."""
    return _FakeResp(200, [{
        "reversal_key": reversal_key,
        "user_id": "user-uuid-aaa",
        "credits": 2000,
        "first_event_id": "evt_dispute_created_1",
        "first_event_type": "charge.dispute.created",
    }])


def _refund_rpc_resp(credits: int = 2000) -> _FakeResp:
    return _FakeResp(200, {"status": "reversed", "credits_debited": credits, "balance_after": 0})


# ---------------------------------------------------------------------------
# 1. dispute.created then dispute.funds_withdrawn → only ONE reversal fires.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispute_created_then_funds_withdrawn_same_dispute_only_one_reversal():
    """When charge.dispute.created is processed first and writes to
    stripe_dispute_reversals, a subsequent charge.dispute.funds_withdrawn
    for the same dispute must detect the existing row and skip the refund RPC."""
    cfg = _cfg()
    dispute_id = "dp_1"
    reversal_key = f"dispute:{dispute_id}"

    # After first event processes, stripe_dispute_reversals has a row.
    # We simulate this by having the second call find an existing row.
    call_count = {"n": 0}

    class _StatefulClient:
        def __init__(self):
            self.calls: list[dict] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url: str, json=None, headers=None):
            self.calls.append({"method": "POST", "url": url, "json": json})
            if "rpc/grant_credits" in url:
                return _FakeResp(200, {"status": "granted"})
            if "rpc/refund_credits" in url:
                return _refund_rpc_resp()
            return _FakeResp(201, {})

        async def get(self, url: str, params=None, headers=None):
            self.calls.append({"method": "GET", "url": url, "params": params})
            call_count["n"] += 1
            if "stripe_dispute_reversals" in url:
                # First call (from dispute.created): no row yet
                # Second call (from dispute.funds_withdrawn): row exists
                if call_count["n"] <= 2:  # grant lookup + first reversal check
                    return _no_reversal_row()
                else:
                    return _existing_reversal_row(reversal_key)
            if "stripe_payment_grants" in url:
                return _grant_lookup_resp()
            return _FakeResp(200, [])

    client1 = _StatefulClient()
    client2 = _StatefulClient()

    # First call: dispute.created — should process
    with patch.object(httpx, "AsyncClient", return_value=client1):
        await mod._handle_charge_dispute_created(
            {
                "id": dispute_id,
                "charge": "ch_1",
                "payment_intent": "pi_1",
                "amount": 2000,
            },
            cfg,
            event_id="evt_dispute_created_1",
        )

    # Second call: funds_withdrawn — same dispute_id → should be deduped
    with patch.object(httpx, "AsyncClient", return_value=client2):
        await mod._handle_charge_dispute_funds_withdrawn(
            {
                "id": dispute_id,
                "charge": "ch_1",
                "payment_intent": "pi_1",
                "amount": 2000,
            },
            cfg,
            event_id="evt_dispute_fw_1",
        )

    # First handler should have called refund_credits
    refund_calls_1 = [c for c in client1.calls if "rpc/refund_credits" in c["url"]]
    assert len(refund_calls_1) == 1, "First dispute event must trigger refund"

    # Second handler should NOT have called refund_credits
    refund_calls_2 = [c for c in client2.calls if "rpc/refund_credits" in c["url"]]
    assert len(refund_calls_2) == 0, (
        "Second dispute event (funds_withdrawn) must be deduped via stripe_dispute_reversals"
    )


# ---------------------------------------------------------------------------
# 2. charge.refunded then charge.dispute.created → both process (different keys).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_charge_refunded_then_dispute_created_both_process_different_keys():
    """charge.refunded uses reversal_key='charge:<id>:reversal'.
    charge.dispute.created uses reversal_key='dispute:<dispute_id>'.
    These are different keys, so both events should process independently."""
    cfg = _cfg()
    charge_id = "ch_2"
    dispute_id = "dp_2"

    refund_resp = _refund_rpc_resp()
    grant_resp = _grant_lookup_resp(credits=3000)
    no_row = _no_reversal_row()

    # Both events see no existing reversal for their respective keys
    client = _RecordingClient(
        by_path={
            "stripe_payment_grants": grant_resp,
            "stripe_dispute_reversals": no_row,
            "rpc/refund_credits": refund_resp,
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        # charge.refunded
        await mod._handle_charge_refunded(
            {"id": charge_id, "payment_intent": "pi_2", "amount": 3000, "amount_refunded": 3000},
            cfg,
            event_id="evt_refund_2",
        )
        # charge.dispute.created (same charge, different event type)
        await mod._handle_charge_dispute_created(
            {"id": dispute_id, "charge": charge_id, "payment_intent": "pi_2", "amount": 3000},
            cfg,
            event_id="evt_dispute_created_2",
        )

    refund_calls = [c for c in client.calls if "rpc/refund_credits" in c["url"]]
    assert len(refund_calls) == 2, (
        "charge.refunded and charge.dispute.created use different reversal keys and must both fire"
    )


# ---------------------------------------------------------------------------
# 3. Successful reversal inserts into stripe_dispute_reversals.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_reversal_inserts_into_stripe_dispute_reversals():
    """After a successful credit reversal, a row must be inserted into
    stripe_dispute_reversals so subsequent duplicate events are deduped."""
    cfg = _cfg()

    grant_resp = _grant_lookup_resp(credits=1500)
    no_row = _no_reversal_row()
    refund_resp = _refund_rpc_resp(credits=1500)
    insert_resp = _FakeResp(201, {})

    client = _RecordingClient(
        by_path={
            "stripe_payment_grants": grant_resp,
            "stripe_dispute_reversals": no_row,
            "rpc/refund_credits": refund_resp,
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_dispute_created(
            {
                "id": "dp_3",
                "charge": "ch_3",
                "payment_intent": "pi_3",
                "amount": 1500,
            },
            cfg,
            event_id="evt_dc_3",
        )

    insert_calls = [
        c for c in client.calls
        if c["method"] == "POST" and "stripe_dispute_reversals" in c["url"]
    ]
    assert len(insert_calls) == 1, (
        "stripe_dispute_reversals row must be inserted after successful reversal"
    )
    body = insert_calls[0]["json"]
    assert body["reversal_key"] == "dispute:dp_3"
    assert body["first_event_id"] == "evt_dc_3"


# ---------------------------------------------------------------------------
# 4. Pre-existing stripe_dispute_reversals row short-circuits refund RPC.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preexisting_reversal_row_short_circuits_refund_rpc():
    """If stripe_dispute_reversals already has a row for the reversal_key,
    _reverse_credits_for_charge must return early without calling refund_credits."""
    cfg = _cfg()

    existing_row = _existing_reversal_row("dispute:dp_4")
    grant_resp = _grant_lookup_resp()

    client = _RecordingClient(
        by_path={
            "stripe_dispute_reversals": existing_row,
            "stripe_payment_grants": grant_resp,
            "rpc/refund_credits": _refund_rpc_resp(),
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_dispute_funds_withdrawn(
            {
                "id": "dp_4",
                "charge": "ch_4",
                "payment_intent": "pi_4",
                "amount": 2000,
            },
            cfg,
            event_id="evt_fw_4",
        )

    refund_calls = [c for c in client.calls if "rpc/refund_credits" in c["url"]]
    assert len(refund_calls) == 0, (
        "refund_credits must not be called when stripe_dispute_reversals already has this key"
    )


# ---------------------------------------------------------------------------
# 5. Reversal key formatting for dispute vs no-dispute paths.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reversal_key_formatting_dispute_vs_no_dispute_paths():
    """Dispute events produce reversal_key='dispute:<dispute_id>'.
    charge.refunded (no dispute object) produces 'charge:<charge_id>:reversal'."""
    cfg = _cfg()

    # Capture inserted rows
    inserted_keys: list[str] = []
    grant_resp = _grant_lookup_resp(credits=1000)
    no_row = _no_reversal_row()
    refund_resp = _refund_rpc_resp(credits=1000)

    class _KeyCapturingClient:
        def __init__(self):
            self.calls: list[dict] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url: str, json=None, headers=None):
            self.calls.append({"method": "POST", "url": url, "json": json})
            if "stripe_dispute_reversals" in url and json:
                inserted_keys.append(json.get("reversal_key", ""))
            if "rpc/refund_credits" in url:
                return refund_resp
            return _FakeResp(201, {})

        async def get(self, url: str, params=None, headers=None):
            self.calls.append({"method": "GET", "url": url, "params": params})
            if "stripe_dispute_reversals" in url:
                return no_row
            if "stripe_payment_grants" in url:
                return grant_resp
            return _FakeResp(200, [])

    # Test 1: dispute.created → reversal_key uses dispute id
    with patch.object(httpx, "AsyncClient", return_value=_KeyCapturingClient()):
        await mod._handle_charge_dispute_created(
            {"id": "dp_5", "charge": "ch_5", "payment_intent": "pi_5", "amount": 1000},
            cfg,
            event_id="evt_dc_5",
        )

    assert "dispute:dp_5" in inserted_keys, (
        f"dispute key should be 'dispute:dp_5', got: {inserted_keys}"
    )
    inserted_keys.clear()

    # Test 2: charge.refunded → reversal_key uses charge id
    with patch.object(httpx, "AsyncClient", return_value=_KeyCapturingClient()):
        await mod._handle_charge_refunded(
            {"id": "ch_6", "payment_intent": "pi_6", "amount": 1000, "amount_refunded": 1000},
            cfg,
            event_id="evt_ref_5",
        )

    assert "charge:ch_6:reversal" in inserted_keys, (
        f"charge refund key should be 'charge:ch_6:reversal', got: {inserted_keys}"
    )
