"""I-02 regression suite: dispute reversal stable reversal_key idempotency.

Bug fixed:
- _reverse_credits_for_charge called _refund_rpc(ref_id=event_id).
- charge.dispute.created (evt_A) and charge.dispute.funds_withdrawn (evt_B) for the
  same dispute have different event_ids, so refund_credits's duplicate check on
  (type='refund', ref_type, ref_id=event_id) did NOT collapse them.
- Two concurrent webhooks could both pass the SELECT short-circuit and each call
  refund_credits, double-debiting the user.
- Fix: pass ref_id=reversal_key (stable across all event types for the same dispute)
  so the RPC-level idempotency guard collapses the second concurrent call.

Test inventory (>=5):
  1. refund_rpc_called_with_reversal_key_not_event_id
  2. concurrent_dispute_events_same_dispute_only_one_refund_via_rpc_dedup
  3. record_dispute_reversal_or_skip_still_short_circuits_when_row_exists
  4. compute_reversal_key_returns_dispute_id_format
  5. insert_dispute_reversal_records_first_event_id
"""

from __future__ import annotations

import asyncio
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
    ) -> None:
        self.default_response = default_response or _FakeResp(200, {})
        self.by_path = by_path or {}
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url: str, json=None, headers=None):
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


@pytest.fixture(autouse=True)
def _patch_supabase_api_key():
    with patch.object(mod, "_supabase_api_key", lambda cfg: "service_role_xxx"):
        yield


def _grant_resp(user_id: str = "user-uuid-i02", credits: int = 1000) -> _FakeResp:
    return _FakeResp(200, [{"user_id": user_id, "credits": credits, "event_id": "evt_grant"}])


def _no_reversal() -> _FakeResp:
    return _FakeResp(200, [])


def _existing_reversal(key: str = "dispute:dp_X") -> _FakeResp:
    return _FakeResp(200, [{"reversal_key": key}])


def _refund_ok() -> _FakeResp:
    return _FakeResp(200, {"status": "reversed", "debited_now": 1000, "balance_after": 0})


def _refund_duplicate() -> _FakeResp:
    return _FakeResp(200, {"status": "duplicate"})


# ---------------------------------------------------------------------------
# 1. _refund_rpc is called with ref_id=reversal_key, NOT event_id.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refund_rpc_called_with_reversal_key_not_event_id():
    """_reverse_credits_for_charge must pass ref_id=reversal_key to _refund_rpc.

    The stable key is 'dispute:<dispute_id>' when dispute object is present.
    The event_id is stored in stripe_dispute_reversals.first_event_id only for forensics.
    """
    cfg = _cfg()
    dispute_obj = {"id": "dp_X"}
    expected_key = "dispute:dp_X"
    event_id = "evt_created_111"

    client = _RecordingClient(
        by_path={
            "stripe_payment_grants": _grant_resp(),
            "stripe_dispute_reversals": _no_reversal(),
            "rpc/refund_credits": _refund_ok(),
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_dispute_created(
            {"id": "dp_X", "charge": "ch_X", "payment_intent": "pi_X", "amount": 1000},
            cfg,
            event_id=event_id,
        )

    refund_calls = [c for c in client.calls if "rpc/refund_credits" in c["url"]]
    assert len(refund_calls) == 1, "Expected exactly one refund_credits call"
    payload = refund_calls[0]["json"]
    assert payload.get("p_ref_id") == expected_key, (
        f"ref_id must be reversal_key='{expected_key}', got: {payload.get('p_ref_id')!r}\n"
        "I-02 fix: pass ref_id=reversal_key so concurrent events with different event_ids "
        "are collapsed by refund_credits idempotency."
    )
    assert payload.get("p_ref_id") != event_id, (
        f"ref_id must NOT be event_id='{event_id}'"
    )


# ---------------------------------------------------------------------------
# 2. Two concurrent dispute events for the same dispute: only one refund fires.
#    Simulates the RPC-level dedup: second call returns status='duplicate'.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_dispute_events_same_dispute_only_one_refund_via_rpc_dedup():
    """When two dispute webhooks race past the SELECT check, the refund_credits
    server-side dedup on (ref_id=reversal_key) collapses the second call.

    We simulate this by having the second _refund_rpc call return 'duplicate'.
    The net result is only one credit debit regardless of how many calls fire.
    """
    cfg = _cfg()

    refund_call_count = {"n": 0}
    actual_debits = []

    class _RaceClient:
        def __init__(self):
            self.calls: list[dict] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url: str, json=None, headers=None):
            self.calls.append({"method": "POST", "url": url, "json": json})
            if "rpc/refund_credits" in url:
                refund_call_count["n"] += 1
                if refund_call_count["n"] == 1:
                    actual_debits.append(json.get("p_credits"))
                    return _refund_ok()
                else:
                    # Second concurrent call: RPC dedupes on reversal_key
                    return _refund_duplicate()
            return _FakeResp(201, {})

        async def get(self, url: str, params=None, headers=None):
            self.calls.append({"method": "GET", "url": url, "params": params})
            if "stripe_payment_grants" in url:
                return _grant_resp()
            # Both dispute events pass the SELECT (simulating the TOCTOU window)
            if "stripe_dispute_reversals" in url:
                return _no_reversal()
            return _FakeResp(200, [])

    client = _RaceClient()

    # Simulate both dispute.created and dispute.funds_withdrawn racing through
    with patch.object(httpx, "AsyncClient", return_value=client):
        await asyncio.gather(
            mod._handle_charge_dispute_created(
                {"id": "dp_X", "charge": "ch_X", "payment_intent": "pi_X", "amount": 1000},
                cfg,
                event_id="evt_A",
            ),
            mod._handle_charge_dispute_funds_withdrawn(
                {"id": "dp_X", "charge": "ch_X", "payment_intent": "pi_X", "amount": 1000},
                cfg,
                event_id="evt_B",
            ),
        )

    assert refund_call_count["n"] == 2, "Both events should call refund_credits (race)"
    assert len(actual_debits) == 1, (
        "Only ONE actual debit should have landed (second collapsed by RPC dedup)"
    )


# ---------------------------------------------------------------------------
# 3. _record_dispute_reversal_or_skip still short-circuits when row pre-exists.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_dispute_reversal_or_skip_still_short_circuits_when_row_exists():
    """The SELECT fast-path still works: if a row already exists in
    stripe_dispute_reversals, _reverse_credits_for_charge must return without
    calling refund_credits at all."""
    cfg = _cfg()

    client = _RecordingClient(
        by_path={
            "stripe_payment_grants": _grant_resp(),
            "stripe_dispute_reversals": _existing_reversal("dispute:dp_Y"),
            "rpc/refund_credits": _refund_ok(),
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_dispute_created(
            {"id": "dp_Y", "charge": "ch_Y", "payment_intent": "pi_Y", "amount": 500},
            cfg,
            event_id="evt_C",
        )

    refund_calls = [c for c in client.calls if "rpc/refund_credits" in c["url"]]
    assert len(refund_calls) == 0, (
        "SELECT short-circuit must prevent refund_credits when reversal row already exists"
    )


# ---------------------------------------------------------------------------
# 4. _compute_reversal_key returns 'dispute:<id>' when dispute has id.
# ---------------------------------------------------------------------------


def test_compute_reversal_key_returns_dispute_id_format():
    """_compute_reversal_key must return 'dispute:<id>' when dispute_obj has an id."""
    charge_obj = {"id": "ch_Z"}
    dispute_obj = {"id": "dp_Z"}
    key = mod._compute_reversal_key(charge_obj, dispute_obj)
    assert key == "dispute:dp_Z", f"Expected 'dispute:dp_Z', got {key!r}"


def test_compute_reversal_key_returns_charge_format_when_no_dispute():
    """_compute_reversal_key must return 'charge:<id>:reversal' when dispute is None."""
    charge_obj = {"id": "ch_W"}
    key = mod._compute_reversal_key(charge_obj, None)
    assert key == "charge:ch_W:reversal", f"Expected 'charge:ch_W:reversal', got {key!r}"


# ---------------------------------------------------------------------------
# 5. _insert_dispute_reversal records first_event_id=event_id for forensics.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_dispute_reversal_records_first_event_id():
    """_insert_dispute_reversal must include first_event_id=event_id in the payload
    so the triggering webhook can be traced even though ref_id is now reversal_key."""
    cfg = _cfg()
    event_id = "evt_forensic_999"

    client = _RecordingClient(
        by_path={
            "stripe_payment_grants": _grant_resp(),
            "stripe_dispute_reversals": _no_reversal(),
            "rpc/refund_credits": _refund_ok(),
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_dispute_created(
            {"id": "dp_forensic", "charge": "ch_forensic", "payment_intent": "pi_forensic", "amount": 1000},
            cfg,
            event_id=event_id,
        )

    insert_calls = [
        c for c in client.calls
        if c["method"] == "POST" and "stripe_dispute_reversals" in c["url"]
    ]
    assert len(insert_calls) == 1, "Must insert one row into stripe_dispute_reversals"
    body = insert_calls[0]["json"]
    assert body.get("first_event_id") == event_id, (
        f"first_event_id must be the triggering event_id='{event_id}', got {body.get('first_event_id')!r}"
    )
    assert body.get("reversal_key") == "dispute:dp_forensic", (
        f"reversal_key must be 'dispute:dp_forensic', got {body.get('reversal_key')!r}"
    )
