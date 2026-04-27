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
        self.inserted_reversals: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url: str, json=None, headers=None):
        self.calls.append({"method": "POST", "url": url, "json": json})
        if "rpc/process_charge_reversal" in url:
            payload = json or {}
            reversal_key = payload.get("p_reversal_key")
            charge_id = payload.get("p_charge_id")
            target = int(payload.get("p_target_credits") or 0)
            for row in self.inserted_reversals:
                if row.get("reversal_key") == reversal_key:
                    return _FakeResp(200, {"status": "duplicate", "credits": 0})
            already = sum(
                int(r.get("credits") or 0)
                for r in self.inserted_reversals
                if r.get("charge_id") == charge_id
            )
            incremental = max(0, target - already)
            self.inserted_reversals.append(
                {
                    "reversal_key": reversal_key,
                    "user_id": payload.get("p_user_id"),
                    "charge_id": charge_id,
                    "dispute_id": payload.get("p_dispute_id"),
                    "payment_intent_id": payload.get("p_payment_intent_id"),
                    "credits": incremental,
                    "first_event_id": payload.get("p_first_event_id"),
                    "first_event_type": payload.get("p_first_event_type"),
                }
            )
            if incremental <= 0:
                return _FakeResp(200, {"status": "already_satisfied", "credits": 0})
            self.calls.append(
                {
                    "method": "POST",
                    "url": "https://supabase.test/rest/v1/rpc/refund_credits",
                    "json": {
                        "p_user_id": payload.get("p_user_id"),
                        "p_credits": incremental,
                        "p_ref_type": "stripe_event",
                        "p_ref_id": reversal_key,
                    },
                }
            )
            return _FakeResp(200, {"status": "reversed", "credits": incremental})
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

    # Use a shared state client that correctly simulates DB state across two calls.
    # The client tracks inserted reversal rows and answers GET queries from that state.
    class _SharedStateClient:
        """Simulates a single DB that persists inserted rows across multiple
        AsyncClient contexts (one per event handler call)."""

        # Shared class-level state between client1 and client2 instances
        _inserted_reversals: list[dict] = []

        def __init__(self):
            self.calls: list[dict] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url: str, json=None, headers=None):
            self.calls.append({"method": "POST", "url": url, "json": json})
            if "stripe_dispute_reversals" in url and json:
                _SharedStateClient._inserted_reversals.append(json)
                return _FakeResp(201, {})
            if "rpc/refund_credits" in url:
                return _refund_rpc_resp()
            if "rpc/process_charge_reversal" in url:
                payload = json or {}
                rkey = payload.get("p_reversal_key")
                cid = payload.get("p_charge_id")
                target = int(payload.get("p_target_credits") or 0)
                for row in _SharedStateClient._inserted_reversals:
                    if row.get("reversal_key") == rkey:
                        return _FakeResp(200, {"status": "duplicate", "credits": 0})
                already = sum(
                    int(r.get("credits") or 0)
                    for r in _SharedStateClient._inserted_reversals
                    if r.get("charge_id") == cid
                )
                incremental = max(0, target - already)
                _SharedStateClient._inserted_reversals.append(
                    {
                        "reversal_key": rkey,
                        "user_id": payload.get("p_user_id"),
                        "charge_id": cid,
                        "dispute_id": payload.get("p_dispute_id"),
                        "payment_intent_id": payload.get("p_payment_intent_id"),
                        "credits": incremental,
                        "first_event_id": payload.get("p_first_event_id"),
                        "first_event_type": payload.get("p_first_event_type"),
                    }
                )
                if incremental <= 0:
                    return _FakeResp(200, {"status": "already_satisfied", "credits": 0})
                self.calls.append(
                    {
                        "method": "POST",
                        "url": "https://supabase.test/rest/v1/rpc/refund_credits",
                        "json": {
                            "p_user_id": payload.get("p_user_id"),
                            "p_credits": incremental,
                            "p_ref_type": "stripe_event",
                            "p_ref_id": rkey,
                        },
                    }
                )
                return _FakeResp(200, {"status": "reversed", "credits": incremental})
            return _FakeResp(200, {})

        async def get(self, url: str, params=None, headers=None):
            self.calls.append({"method": "GET", "url": url, "params": params})
            if "stripe_payment_grants" in url:
                return _grant_lookup_resp()
            if "stripe_dispute_reversals" in url:
                params = params or {}
                charge_id_filter = params.get("charge_id", "") if isinstance(params, dict) else ""
                reversal_key_filter = ""
                if "reversal_key=eq." in url:
                    reversal_key_filter = url.split("reversal_key=eq.")[1].split("&")[0]

                if charge_id_filter.startswith("eq."):
                    cid = charge_id_filter[3:]
                    rows = [
                        r for r in _SharedStateClient._inserted_reversals
                        if r.get("charge_id") == cid
                    ]
                    return _FakeResp(200, [{"credits": r.get("credits", 0)} for r in rows])

                if reversal_key_filter:
                    key = reversal_key_filter.lstrip("eq.")
                    for row in _SharedStateClient._inserted_reversals:
                        if row.get("reversal_key") == key:
                            return _FakeResp(200, [row])
                    return _FakeResp(200, [])

                return _FakeResp(200, [])
            return _FakeResp(200, [])

    # Reset shared state before test
    _SharedStateClient._inserted_reversals = []

    client1 = _SharedStateClient()
    client2 = _SharedStateClient()

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
    """charge.refunded uses reversal_key='refund_event:<event_id>'.
    charge.dispute.created uses reversal_key='dispute:<dispute_id>'.

    Both events reach the process_charge_reversal RPC (different keys, no
    dedup short-circuit). The first fully reverses 3000; the second observes
    already_reversed=3000 and lands status='already_satisfied' with
    incremental=0. This is the correct economic outcome under J-01/J-02:
    a charge that has already been fully refunded must not be debited a second
    time when a dispute lands on the same charge.
    """
    cfg = _cfg()
    charge_id = "ch_2"
    dispute_id = "dp_2"

    refund_resp = _refund_rpc_resp()
    grant_resp = _grant_lookup_resp(credits=3000)
    no_row = _no_reversal_row()

    client = _RecordingClient(
        by_path={
            "stripe_payment_grants": grant_resp,
            "stripe_dispute_reversals": no_row,
            "rpc/refund_credits": refund_resp,
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_refunded(
            {"id": charge_id, "payment_intent": "pi_2", "amount": 3000, "amount_refunded": 3000},
            cfg,
            event_id="evt_refund_2",
        )
        await mod._handle_charge_dispute_created(
            {"id": dispute_id, "charge": charge_id, "payment_intent": "pi_2", "amount": 3000},
            cfg,
            event_id="evt_dispute_created_2",
        )

    rpc_calls = [c for c in client.calls if "rpc/process_charge_reversal" in c["url"]]
    assert len(rpc_calls) == 2, (
        "both events must reach the reversal RPC (different reversal_keys, "
        "no fast-path collapse)"
    )
    # Distinct reversal keys: refund_event:<event_id> vs dispute:<dispute_id>.
    keys = [c["json"]["p_reversal_key"] for c in rpc_calls]
    assert keys[0] == "refund_event:evt_refund_2"
    assert keys[1] == "dispute:dp_2"
    # Only the first event should produce an actual debit; the second is
    # already_satisfied because charge ch_2 was fully reversed.
    refund_calls = [c for c in client.calls if "rpc/refund_credits" in c["url"]]
    assert len(refund_calls) == 1, (
        "only the first event debits credits; second observes "
        "already_reversed=3000 and lands no incremental debit"
    )
    assert refund_calls[0]["json"]["p_credits"] == 3000


# ---------------------------------------------------------------------------
# 3. Successful reversal inserts into stripe_dispute_reversals.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_reversal_inserts_into_stripe_dispute_reversals():
    """After a successful credit reversal, a row must be inserted into
    stripe_dispute_reversals so subsequent duplicate events are deduped.

    K-02: insert now happens server-side inside process_charge_reversal as
    part of the single atomic transaction. We verify the RPC was invoked
    with the correct reversal_key + first_event_id payload, and that the
    server-side dedup row landed in our state simulator.
    """
    cfg = _cfg()

    grant_resp = _grant_lookup_resp(credits=1500)
    no_row = _no_reversal_row()
    refund_resp = _refund_rpc_resp(credits=1500)

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

    rpc_calls = [
        c for c in client.calls
        if c["method"] == "POST" and "rpc/process_charge_reversal" in c["url"]
    ]
    assert len(rpc_calls) == 1, (
        "process_charge_reversal must be invoked once for a successful reversal"
    )
    body = rpc_calls[0]["json"]
    assert body["p_reversal_key"] == "dispute:dp_3"
    assert body["p_first_event_id"] == "evt_dc_3"
    # Server-side dedup row must have landed (simulated by the RPC mock).
    assert any(
        r.get("reversal_key") == "dispute:dp_3"
        for r in client.inserted_reversals
    ), "server-side dedup row must be persisted by process_charge_reversal"


# ---------------------------------------------------------------------------
# 4. Pre-existing stripe_dispute_reversals row short-circuits refund RPC.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preexisting_reversal_row_short_circuits_refund_rpc():
    """If stripe_dispute_reversals already has a row for the reversal_key,
    _reverse_credits_for_charge must return early without calling refund_credits.

    K-02: dedup now happens inside process_charge_reversal (single SECURITY
    DEFINER RPC). We seed the client's inserted_reversals with the pre-existing
    row so the RPC simulator returns status='duplicate' and never synthesizes
    a refund_credits call.
    """
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
    client.inserted_reversals.append({
        "reversal_key": "dispute:dp_4",
        "charge_id": "ch_4",
        "credits": 2000,
    })

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
        "refund_credits must not fire when reversal_key dedup row already exists; "
        "process_charge_reversal must return status='duplicate'"
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
            if "rpc/process_charge_reversal" in url:
                # Capture the reversal_key directly from the RPC payload since
                # the dedup row insert now happens server-side.
                payload = json or {}
                rkey = payload.get("p_reversal_key", "")
                if rkey:
                    inserted_keys.append(rkey)
                return _FakeResp(200, {"status": "reversed", "credits": int(payload.get("p_target_credits") or 0)})
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

    # Test 2: charge.refunded → reversal_key uses per-event id (J-01 fix)
    # Key format changed from 'charge:<id>:reversal' to 'refund_event:<event_id>'
    # so sequential partial refunds on the same charge each get their own dedup row.
    with patch.object(httpx, "AsyncClient", return_value=_KeyCapturingClient()):
        await mod._handle_charge_refunded(
            {"id": "ch_6", "payment_intent": "pi_6", "amount": 1000, "amount_refunded": 1000},
            cfg,
            event_id="evt_ref_5",
        )

    assert "refund_event:evt_ref_5" in inserted_keys, (
        f"charge refund key should be 'refund_event:evt_ref_5' (J-01 fix), got: {inserted_keys}"
    )
