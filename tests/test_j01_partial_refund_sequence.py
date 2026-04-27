"""J-01 regression suite: sequential partial charge.refunded events each debit
the incremental delta rather than collapsing on a shared charge-scoped key.

Bug fixed:
- _compute_reversal_key used 'charge:<id>:reversal' for all charge.refunded events,
  so a second partial refund on the same charge was treated as a duplicate.
- Fix: use 'refund_event:<event_id>' per-event key + subtract already_reversed credits
  from the target so each event debits only the incremental delta.

Test inventory (>=5):
  1. two_sequential_refunds_30_50_percent_debit_30_then_20
  2. three_sequential_refunds_10_30_50_percent_debit_10_then_20_then_20
  3. single_full_refund_debits_full_grant
  4. same_event_id_replayed_is_idempotent
  5. single_partial_refund_20_percent_regression
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


@pytest.fixture(autouse=True)
def _patch_supabase_api_key():
    with patch.object(mod, "_supabase_api_key", lambda cfg: "service_role_xxx"):
        yield


def _grant_resp(user_id: str = "user-j01", credits: int = 100) -> _FakeResp:
    return _FakeResp(200, [{"user_id": user_id, "credits": credits, "event_id": "evt_grant"}])


def _no_row() -> _FakeResp:
    return _FakeResp(200, [])


def _refund_rpc_ok() -> _FakeResp:
    return _FakeResp(200, {"status": "reversed", "credits_debited": 1, "balance_after": 0})


class _StatefulClient:
    """Tracks inserted reversal rows and returns them on subsequent GET queries,
    simulating the actual database state across sequential event calls.

    For the _sum_reversed_credits_for_charge query (charge_id=eq.<id>),
    returns accumulated credits rows.
    For the dedup check (reversal_key=eq.<key>), returns [] unless that specific
    key was already inserted.
    """

    def __init__(self, grant_credits: int = 100):
        self.calls: list[dict] = []
        self.inserted_reversals: list[dict] = []  # rows inserted via POST
        self.grant_credits = grant_credits

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url: str, json=None, headers=None):
        self.calls.append({"method": "POST", "url": url, "json": json})
        if "stripe_dispute_reversals" in url and json:
            self.inserted_reversals.append(json)
            return _FakeResp(201, {})
        if "rpc/refund_credits" in url:
            return _refund_rpc_ok()
        if "rpc/process_charge_reversal" in url:
            # K-02 RPC simulation: dedup, sum, compute incremental, synthesize
            # refund_credits call so existing assertions still find it.
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
        return _FakeResp(200, {})

    async def get(self, url: str, params=None, headers=None):
        self.calls.append({"method": "GET", "url": url, "params": params})
        if "stripe_payment_grants" in url:
            return _grant_resp(credits=self.grant_credits)
        if "stripe_dispute_reversals" in url:
            params = params or {}
            # _sum_reversed_credits_for_charge uses charge_id=eq.<id>
            charge_id_filter = params.get("charge_id", "")
            # _record_dispute_reversal_or_skip uses reversal_key embedded in URL
            reversal_key_filter = ""
            if isinstance(params, dict):
                reversal_key_filter = params.get("reversal_key", "")
            # Also check URL string
            if "reversal_key=eq." in url:
                reversal_key_filter = url.split("reversal_key=eq.")[1].split("&")[0]

            if charge_id_filter.startswith("eq."):
                # sum query — return all rows for this charge
                cid = charge_id_filter[3:]
                rows = [r for r in self.inserted_reversals if r.get("charge_id") == cid]
                # Return only the credits field columns
                return _FakeResp(200, [{"credits": r.get("credits", 0)} for r in rows])

            if reversal_key_filter:
                key = reversal_key_filter.lstrip("eq.")
                for row in self.inserted_reversals:
                    if row.get("reversal_key") == key:
                        return _FakeResp(200, [row])
                return _FakeResp(200, [])

            # Default
            return _FakeResp(200, [])
        return _FakeResp(200, [])


def _refund_calls(client: _StatefulClient) -> list[dict]:
    return [c for c in client.calls if "rpc/refund_credits" in c["url"]]


def _credits_arg(call: dict) -> int:
    """Extract the 'p_credits' or 'credits' arg from an RPC POST body."""
    body = call.get("json") or {}
    return int(body.get("p_credits", body.get("credits", 0)))


# ---------------------------------------------------------------------------
# 1. Two sequential partial refunds: 30% then 50% → debit 30, then 20
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_sequential_refunds_30_50_percent_debit_30_then_20():
    """First charge.refunded (30%) debits 30 credits. Second (50% cumulative)
    debits only the incremental 20 credits, not the full 50."""
    cfg = _cfg()
    client = _StatefulClient(grant_credits=100)

    with patch.object(httpx, "AsyncClient", return_value=client):
        # Event 1: 30% refund
        await mod._handle_charge_refunded(
            {"id": "ch_j01", "payment_intent": "pi_j01", "amount": 10000, "amount_refunded": 3000},
            cfg,
            event_id="evt_ref_j01_a",
        )
        # Event 2: cumulative 50% refund
        await mod._handle_charge_refunded(
            {"id": "ch_j01", "payment_intent": "pi_j01", "amount": 10000, "amount_refunded": 5000},
            cfg,
            event_id="evt_ref_j01_b",
        )

    calls = _refund_calls(client)
    assert len(calls) == 2, f"Expected 2 refund RPC calls, got {len(calls)}"

    debit1 = _credits_arg(calls[0])
    debit2 = _credits_arg(calls[1])
    assert debit1 == 30, f"First debit should be 30, got {debit1}"
    assert debit2 == 20, f"Second debit should be 20 (incremental), got {debit2}"


# ---------------------------------------------------------------------------
# 2. Three sequential refunds: 10%, 30%, 50% → debit 10, 20, 20
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_sequential_refunds_10_30_50_percent_debit_10_then_20_then_20():
    """Three sequential partial refunds on the same charge each debit only the
    incremental delta from the prior state."""
    cfg = _cfg()
    client = _StatefulClient(grant_credits=100)

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_refunded(
            {"id": "ch_j01b", "payment_intent": "pi_j01b", "amount": 10000, "amount_refunded": 1000},
            cfg,
            event_id="evt_j01b_1",
        )
        await mod._handle_charge_refunded(
            {"id": "ch_j01b", "payment_intent": "pi_j01b", "amount": 10000, "amount_refunded": 3000},
            cfg,
            event_id="evt_j01b_2",
        )
        await mod._handle_charge_refunded(
            {"id": "ch_j01b", "payment_intent": "pi_j01b", "amount": 10000, "amount_refunded": 5000},
            cfg,
            event_id="evt_j01b_3",
        )

    calls = _refund_calls(client)
    assert len(calls) == 3, f"Expected 3 refund RPC calls, got {len(calls)}"

    debits = [_credits_arg(c) for c in calls]
    assert debits[0] == 10, f"First debit should be 10, got {debits[0]}"
    assert debits[1] == 20, f"Second debit should be 20, got {debits[1]}"
    assert debits[2] == 20, f"Third debit should be 20, got {debits[2]}"


# ---------------------------------------------------------------------------
# 3. Single full refund (100%) → debits full grant once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_full_refund_debits_full_grant():
    """A single full charge.refunded event for a $100 charge (100 credits)
    debits all 100 credits."""
    cfg = _cfg()
    client = _StatefulClient(grant_credits=100)

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_refunded(
            {"id": "ch_j01c", "payment_intent": "pi_j01c", "amount": 10000, "amount_refunded": 10000},
            cfg,
            event_id="evt_j01c_full",
        )

    calls = _refund_calls(client)
    assert len(calls) == 1, f"Expected 1 refund RPC call, got {len(calls)}"
    debit = _credits_arg(calls[0])
    assert debit == 100, f"Full refund should debit all 100 credits, got {debit}"


# ---------------------------------------------------------------------------
# 4. Same event_id replayed → idempotent (second call sees existing dedup row)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_event_id_replayed_is_idempotent():
    """If the same event_id is processed twice (webhook retry), the second call
    must be a no-op because the reversal_key='refund_event:<event_id>' already
    has a row in stripe_dispute_reversals."""
    cfg = _cfg()
    client = _StatefulClient(grant_credits=100)

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_refunded(
            {"id": "ch_j01d", "payment_intent": "pi_j01d", "amount": 10000, "amount_refunded": 5000},
            cfg,
            event_id="evt_j01d_dup",
        )
        # Replay exact same event
        await mod._handle_charge_refunded(
            {"id": "ch_j01d", "payment_intent": "pi_j01d", "amount": 10000, "amount_refunded": 5000},
            cfg,
            event_id="evt_j01d_dup",
        )

    calls = _refund_calls(client)
    assert len(calls) == 1, (
        f"Replayed event must be deduplicated; expected 1 refund RPC call, got {len(calls)}"
    )


# ---------------------------------------------------------------------------
# 5. Single partial refund (20%) → debits 20 (regression of existing B-04 test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_partial_refund_20_percent_regression():
    """Regression: a single partial charge.refunded (20%) on a 100-credit grant
    debits exactly 20 credits."""
    cfg = _cfg()
    client = _StatefulClient(grant_credits=100)

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_refunded(
            {"id": "ch_j01e", "payment_intent": "pi_j01e", "amount": 10000, "amount_refunded": 2000},
            cfg,
            event_id="evt_j01e_20pct",
        )

    calls = _refund_calls(client)
    assert len(calls) == 1, f"Expected 1 refund RPC call, got {len(calls)}"
    debit = _credits_arg(calls[0])
    assert debit == 20, f"20% partial refund on 100 credits should debit 20, got {debit}"
