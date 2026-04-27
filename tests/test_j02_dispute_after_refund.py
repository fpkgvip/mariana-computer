"""J-02 regression suite: dispute after partial refund debits only the
incremental remaining credits, not the full grant.

Bug fixed:
- dispute.created used dispute.amount as full amount_refunded, so a full-amount
  dispute after a partial refund would over-debit (e.g. 30 + 100 = 130 for a
  100-credit grant).
- Fix: _reverse_credits_for_charge subtracts already_reversed credits (summed
  across all prior reversal rows for this charge_id) before debiting.

Test inventory (>=4):
  1. partial_refund_30_then_full_dispute_debits_30_then_70
  2. partial_refund_30_then_partial_dispute_70_debits_30_then_70
  3. full_refund_then_full_dispute_refund_debits_100_dispute_skipped
  4. dispute_only_no_prior_refund_debits_full_grant_regression
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


def _grant_resp(user_id: str = "user-j02", credits: int = 100) -> _FakeResp:
    return _FakeResp(200, [{"user_id": user_id, "credits": credits, "event_id": "evt_grant"}])


def _no_row() -> _FakeResp:
    return _FakeResp(200, [])


def _refund_rpc_ok() -> _FakeResp:
    return _FakeResp(200, {"status": "reversed", "credits_debited": 1, "balance_after": 0})


class _StatefulClient:
    """Simulates the database state across sequential refund + dispute calls.

    Tracks inserted stripe_dispute_reversals rows and uses them to answer
    both the _sum_reversed_credits_for_charge query (charge_id=eq.<id>)
    and the dedup check (reversal_key=eq.<key>).
    """

    def __init__(self, grant_credits: int = 100):
        self.calls: list[dict] = []
        self.inserted_reversals: list[dict] = []
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
        return _FakeResp(200, {})

    async def get(self, url: str, params=None, headers=None):
        self.calls.append({"method": "GET", "url": url, "params": params})
        if "stripe_payment_grants" in url:
            return _grant_resp(credits=self.grant_credits)
        if "stripe_dispute_reversals" in url:
            params = params or {}
            charge_id_filter = ""
            reversal_key_filter = ""
            if isinstance(params, dict):
                charge_id_filter = params.get("charge_id", "")
                reversal_key_filter = params.get("reversal_key", "")
            if "reversal_key=eq." in url:
                reversal_key_filter = url.split("reversal_key=eq.")[1].split("&")[0]

            if charge_id_filter.startswith("eq."):
                cid = charge_id_filter[3:]
                rows = [r for r in self.inserted_reversals if r.get("charge_id") == cid]
                return _FakeResp(200, [{"credits": r.get("credits", 0)} for r in rows])

            if reversal_key_filter:
                key = reversal_key_filter.lstrip("eq.")
                for row in self.inserted_reversals:
                    if row.get("reversal_key") == key:
                        return _FakeResp(200, [row])
                return _FakeResp(200, [])

            return _FakeResp(200, [])
        return _FakeResp(200, [])


def _refund_calls(client: _StatefulClient) -> list[dict]:
    return [c for c in client.calls if "rpc/refund_credits" in c["url"]]


def _credits_arg(call: dict) -> int:
    body = call.get("json") or {}
    return int(body.get("p_credits", body.get("credits", 0)))


# ---------------------------------------------------------------------------
# 1. Partial refund 30%, then full-amount dispute → debit 30, then 70
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_refund_30_then_full_dispute_debits_30_then_70():
    """$100 charge with 100-credit grant.
    charge.refunded at 30%  → debit 30.
    charge.dispute.created at full amount → debit 70 (not 100).
    Total: 100. No over-clawback."""
    cfg = _cfg()
    client = _StatefulClient(grant_credits=100)

    with patch.object(httpx, "AsyncClient", return_value=client):
        # Step 1: partial refund 30%
        await mod._handle_charge_refunded(
            {"id": "ch_j02", "payment_intent": "pi_j02", "amount": 10000, "amount_refunded": 3000},
            cfg,
            event_id="evt_ref_j02",
        )
        # Step 2: full-amount dispute on same charge
        await mod._handle_charge_dispute_created(
            {
                "id": "dp_j02",
                "charge": "ch_j02",
                "payment_intent": "pi_j02",
                "amount": 10000,
            },
            cfg,
            event_id="evt_disp_j02",
        )

    calls = _refund_calls(client)
    assert len(calls) == 2, f"Expected 2 refund RPC calls (refund + dispute), got {len(calls)}"

    debit1 = _credits_arg(calls[0])
    debit2 = _credits_arg(calls[1])
    assert debit1 == 30, f"Refund debit should be 30, got {debit1}"
    assert debit2 == 70, (
        f"Dispute debit should be 70 (incremental after 30 already reversed), got {debit2}"
    )
    total = debit1 + debit2
    assert total == 100, f"Total debit must not exceed 100, got {total}"


# ---------------------------------------------------------------------------
# 2. Partial refund 30%, partial dispute 70% → debit 30, then 70
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_refund_30_then_partial_dispute_70_debits_30_then_70():
    """$100 charge: 30% partial refund, then dispute for 70% of the charge.
    Refund debits 30. Dispute target = 100 (because _handle_charge_dispute_created
    sets charge_dict amount=dispute.amount=amount_refunded=7000, so amount_refunded
    >= amount_total → target = original_credits = 100). already_reversed=30.
    incremental = 100 - 30 = 70.

    Note: _handle_charge_dispute_created builds:
      charge_dict = {amount: 7000, amount_refunded: 7000}
    Since amount_refunded == amount_total, the full-reversal branch fires:
      target_credits = original_credits = 100
    already_reversed = 30 (from prior refund)
    incremental = max(0, 100 - 30) = 70.
    Total debited: 30 + 70 = 100. No over-clawback.
    """
    cfg = _cfg()
    client = _StatefulClient(grant_credits=100)

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_refunded(
            {"id": "ch_j02b", "payment_intent": "pi_j02b", "amount": 10000, "amount_refunded": 3000},
            cfg,
            event_id="evt_ref_j02b",
        )
        await mod._handle_charge_dispute_created(
            {
                "id": "dp_j02b",
                "charge": "ch_j02b",
                "payment_intent": "pi_j02b",
                "amount": 7000,
            },
            cfg,
            event_id="evt_disp_j02b",
        )

    calls = _refund_calls(client)
    assert len(calls) == 2, f"Expected 2 refund RPC calls, got {len(calls)}"

    debit1 = _credits_arg(calls[0])
    debit2 = _credits_arg(calls[1])
    assert debit1 == 30, f"Refund debit should be 30, got {debit1}"
    # _handle_charge_dispute_created: amount=7000, amount_refunded=7000
    # → amount_refunded >= amount_total → target = 100 (full grant)
    # already_reversed = 30 → incremental = 70
    assert debit2 == 70, (
        f"Dispute incremental debit should be 70 (100 target - 30 already), got {debit2}"
    )
    total = debit1 + debit2
    assert total == 100, f"Total debit must not exceed original grant of 100, got {total}"


# ---------------------------------------------------------------------------
# 3. Full refund 100%, then full dispute → refund debits 100, dispute skipped (0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_refund_then_full_dispute_dispute_records_zero():
    """Full refund first debits all 100 credits. A subsequent full-amount dispute
    finds already_reversed=100, incremental=0, and records a 0-credit dedup row
    without calling the refund RPC for any positive amount."""
    cfg = _cfg()
    client = _StatefulClient(grant_credits=100)

    with patch.object(httpx, "AsyncClient", return_value=client):
        # Full refund
        await mod._handle_charge_refunded(
            {"id": "ch_j02c", "payment_intent": "pi_j02c", "amount": 10000, "amount_refunded": 10000},
            cfg,
            event_id="evt_ref_j02c",
        )
        # Full dispute on same charge
        await mod._handle_charge_dispute_created(
            {
                "id": "dp_j02c",
                "charge": "ch_j02c",
                "payment_intent": "pi_j02c",
                "amount": 10000,
            },
            cfg,
            event_id="evt_disp_j02c",
        )

    calls = _refund_calls(client)
    # Only the refund should trigger the RPC; dispute incremental=0 skips RPC
    assert len(calls) == 1, (
        f"Only the refund should call refund_credits; dispute with 0 incremental must skip RPC. "
        f"Got {len(calls)} calls."
    )
    debit1 = _credits_arg(calls[0])
    assert debit1 == 100, f"Full refund should debit all 100 credits, got {debit1}"

    # The dispute should still have inserted a 0-credit dedup row
    dispute_inserts = [
        r for r in client.inserted_reversals if r.get("reversal_key", "").startswith("dispute:")
    ]
    assert len(dispute_inserts) >= 1, (
        "Dispute must still insert a 0-credit dedup row to prevent future re-processing"
    )
    assert dispute_inserts[0].get("credits", -1) == 0, (
        f"Dispute dedup row credits should be 0, got {dispute_inserts[0].get('credits')}"
    )


# ---------------------------------------------------------------------------
# 4. Dispute only (no prior refund) → debits full grant (regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispute_only_no_prior_refund_debits_full_grant_regression():
    """Regression: when no prior refund reversal exists for a charge, a
    dispute.created for the full amount must still debit all 100 credits."""
    cfg = _cfg()
    client = _StatefulClient(grant_credits=100)

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_dispute_created(
            {
                "id": "dp_j02d",
                "charge": "ch_j02d",
                "payment_intent": "pi_j02d",
                "amount": 10000,
            },
            cfg,
            event_id="evt_disp_j02d",
        )

    calls = _refund_calls(client)
    assert len(calls) == 1, f"Expected 1 refund RPC call, got {len(calls)}"
    debit = _credits_arg(calls[0])
    assert debit == 100, f"Full dispute with no prior refund should debit all 100 credits, got {debit}"
