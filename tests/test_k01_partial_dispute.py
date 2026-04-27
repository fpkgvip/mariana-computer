"""K-01 regression suite: partial-amount disputes (dispute.amount < charge.amount)
must debit only the disputed portion, not the full original grant.

Bug fixed:
- _handle_charge_dispute_created and _handle_charge_dispute_funds_withdrawn build
  a pseudo-charge with amount = amount_refunded = dispute.amount. In
  _reverse_credits_for_charge that always trips the else branch (target = full
  grant) regardless of what fraction of the original charge was disputed.
- Fix: persist the original charge.amount on stripe_payment_grants
  (charge_amount column, migration 020). When dispute_obj is present,
  _reverse_credits_for_charge overrides amount_total with grant_tx['charge_amount']
  so the pro-rata branch sees the true ratio dispute.amount / charge.amount.

Test inventory (>=2 per task brief):
  1. partial_dispute_30_percent_no_prior_refund_debits_30
  2. partial_refund_20_then_partial_dispute_30_total_debit_30
  3. full_amount_dispute_no_prior_refund_debits_full_grant_regression
  4. partial_dispute_funds_withdrawn_path_also_pro_rata
  5. legacy_grant_without_charge_amount_falls_back_to_old_behaviour
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from mariana import api as mod
from mariana.config import AppConfig


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


def _refund_rpc_ok() -> _FakeResp:
    return _FakeResp(200, {"status": "reversed", "credits_debited": 1, "balance_after": 0})


class _StatefulClient:
    """Simulates DB state for grants + dispute_reversals across sequential calls.

    K-01: returns charge_amount in stripe_payment_grants response so the
    reversal flow can compute pro-rata correctly when dispute_obj is set.
    """

    def __init__(self, grant_credits: int = 100, charge_amount: int | None = 10000) -> None:
        self.calls: list[dict] = []
        self.inserted_reversals: list[dict] = []
        self.grant_credits = grant_credits
        self.charge_amount = charge_amount

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
            # K-02 RPC path — simulate the SECURITY DEFINER function:
            # 1. dedup on reversal_key
            # 2. sum credits_already_reversed by charge_id
            # 3. compute incremental, call refund_credits, INSERT dedup row
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
            # Record the refund call so existing assertions over rpc/refund_credits
            # continue to function for K-02 tests.
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
            row: dict[str, Any] = {
                "user_id": "user-k01",
                "credits": self.grant_credits,
                "event_id": "evt_grant",
            }
            if self.charge_amount is not None:
                row["charge_amount"] = self.charge_amount
            return _FakeResp(200, [row])
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
# 1. Partial dispute, no prior refund — debit only the disputed portion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_dispute_30_percent_no_prior_refund_debits_30():
    """$100 charge / 100-credit grant. Stripe emits dispute.created with
    dispute.amount=3000 (30% of charge). The handler must debit 30 credits,
    not the full 100."""
    cfg = _cfg()
    client = _StatefulClient(grant_credits=100, charge_amount=10000)

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_dispute_created(
            {
                "id": "dp_k01",
                "charge": "ch_k01",
                "payment_intent": "pi_k01",
                "amount": 3000,
            },
            cfg,
            event_id="evt_disp_k01",
        )

    calls = _refund_calls(client)
    assert len(calls) == 1, f"Expected 1 refund RPC call, got {len(calls)}"
    debit = _credits_arg(calls[0])
    assert debit == 30, (
        f"Partial dispute (3000/10000) on a 100-credit grant must debit 30, got {debit}"
    )


# ---------------------------------------------------------------------------
# 2. Combined K-01 + J-02: partial refund 20%, then partial dispute 30%
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_refund_20_then_partial_dispute_30_total_debit_30():
    """$100 charge / 100-credit grant.
    Step 1: charge.refunded with cumulative amount_refunded=2000 (20%) → debit 20.
    Step 2: charge.dispute.created with dispute.amount=3000 (30% of charge) →
    target=30, already_reversed=20, incremental=10. Total = 30."""
    cfg = _cfg()
    client = _StatefulClient(grant_credits=100, charge_amount=10000)

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_refunded(
            {
                "id": "ch_k01b",
                "payment_intent": "pi_k01b",
                "amount": 10000,
                "amount_refunded": 2000,
            },
            cfg,
            event_id="evt_ref_k01b",
        )
        await mod._handle_charge_dispute_created(
            {
                "id": "dp_k01b",
                "charge": "ch_k01b",
                "payment_intent": "pi_k01b",
                "amount": 3000,
            },
            cfg,
            event_id="evt_disp_k01b",
        )

    calls = _refund_calls(client)
    assert len(calls) == 2, f"Expected 2 refund RPC calls, got {len(calls)}"
    debit1 = _credits_arg(calls[0])
    debit2 = _credits_arg(calls[1])
    assert debit1 == 20, f"Partial refund 20% should debit 20, got {debit1}"
    assert debit2 == 10, (
        f"Partial dispute 30% after 20% prior refund: target=30, already=20, "
        f"incremental=10 expected, got {debit2}"
    )
    assert debit1 + debit2 == 30, f"Total debit must be 30, got {debit1 + debit2}"


# ---------------------------------------------------------------------------
# 3. Regression: full-amount dispute with no prior refund still debits all 100
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_amount_dispute_no_prior_refund_debits_full_grant_regression():
    """Regression: when dispute.amount == charge.amount the full 100-credit
    grant must still be debited (matches previous J-02 invariant)."""
    cfg = _cfg()
    client = _StatefulClient(grant_credits=100, charge_amount=10000)

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_dispute_created(
            {
                "id": "dp_k01c",
                "charge": "ch_k01c",
                "payment_intent": "pi_k01c",
                "amount": 10000,
            },
            cfg,
            event_id="evt_disp_k01c",
        )

    calls = _refund_calls(client)
    assert len(calls) == 1, f"Expected 1 refund RPC call, got {len(calls)}"
    debit = _credits_arg(calls[0])
    assert debit == 100, f"Full-amount dispute should debit all 100, got {debit}"


# ---------------------------------------------------------------------------
# 4. Funds-withdrawn path is also pro-rata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_dispute_funds_withdrawn_path_also_pro_rata():
    """charge.dispute.funds_withdrawn must debit pro-rata when dispute.amount
    < charge.amount (symmetric with charge.dispute.created)."""
    cfg = _cfg()
    client = _StatefulClient(grant_credits=100, charge_amount=10000)

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_dispute_funds_withdrawn(
            {
                "id": "dp_k01d",
                "charge": "ch_k01d",
                "payment_intent": "pi_k01d",
                "amount": 4500,
            },
            cfg,
            event_id="evt_disp_k01d_fw",
        )

    calls = _refund_calls(client)
    assert len(calls) == 1, f"Expected 1 refund RPC call, got {len(calls)}"
    debit = _credits_arg(calls[0])
    assert debit == 45, (
        f"Partial dispute funds_withdrawn (4500/10000) should debit 45, got {debit}"
    )


# ---------------------------------------------------------------------------
# 5. Legacy grant without charge_amount falls back to current behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_grant_without_charge_amount_falls_back_to_old_behaviour():
    """For legacy stripe_payment_grants rows that pre-date the charge_amount
    backfill, the reversal flow must NOT crash. It falls back to the existing
    behaviour (full grant debit on dispute, since charge_obj.amount equals
    dispute.amount in the pseudo-charge)."""
    cfg = _cfg()
    client = _StatefulClient(grant_credits=100, charge_amount=None)

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_dispute_created(
            {
                "id": "dp_k01e",
                "charge": "ch_k01e",
                "payment_intent": "pi_k01e",
                "amount": 3000,
            },
            cfg,
            event_id="evt_disp_k01e",
        )

    calls = _refund_calls(client)
    assert len(calls) == 1, f"Expected 1 refund RPC call, got {len(calls)}"
    debit = _credits_arg(calls[0])
    # Legacy fallback: charge_amount unknown, so reversal uses charge_obj.amount
    # (= dispute.amount = 3000) and falls into the else branch (target = full grant).
    # This is documented as best-effort for legacy rows.
    assert debit == 100, (
        f"Legacy row without charge_amount: documented fallback debits full grant, got {debit}"
    )
