"""H-01 regression suite: stripe_payment_grants pi linkage.

Bugs fixed:
- _grant_credits_for_event must persist pi_id into stripe_payment_grants after
  a new grant (status != 'duplicate').
- _lookup_grant_tx_for_payment_intent must query stripe_payment_grants only;
  the global latest-grant fallback is removed.

Test inventory (>=6):
  1.  grant_with_pi_id_inserts_stripe_payment_grants_row
  2.  lookup_returns_none_when_stripe_payment_grants_empty
  3.  refund_for_user_a_pi_resolves_user_a_not_user_b
  4.  duplicate_grant_status_still_attempts_stripe_payment_grants_insert (L-01)
  5.  grant_with_no_pi_id_runs_without_error
  6.  lookup_returns_correct_fields_from_stripe_payment_grants
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
    ) -> None:
        self.default_response = default_response or _FakeResp(201, {})
        self.by_path = by_path or {}
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
# 1. _grant_credits_for_event with pi_id inserts into stripe_payment_grants.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_with_pi_id_inserts_stripe_payment_grants_row():
    """When pi_id is provided and the grant RPC returns status='granted',
    a POST to /rest/v1/stripe_payment_grants must be made."""
    cfg = _cfg()

    grant_rpc_resp = _FakeResp(200, {"status": "granted", "credits": 1000, "balance_after": 1000})
    pg_insert_resp = _FakeResp(201, {})

    client = _RecordingClient(
        by_path={
            "rpc/grant_credits": grant_rpc_resp,
            "stripe_payment_grants": pg_insert_resp,
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._grant_credits_for_event(
            user_id="user-uuid-aaa",
            credits=1000,
            source="topup",
            ref_id="evt_pi_1",
            expires_at=None,
            cfg=cfg,
            pi_id="pi_abc123",
            charge_id="ch_abc123",
        )

    insert_calls = [
        c for c in client.calls
        if c["method"] == "POST" and "stripe_payment_grants" in c["url"]
    ]
    assert len(insert_calls) == 1, (
        "_grant_credits_for_event must POST to stripe_payment_grants when pi_id is set"
    )
    body = insert_calls[0]["json"]
    assert body["payment_intent_id"] == "pi_abc123"
    assert body["user_id"] == "user-uuid-aaa"
    assert body["credits"] == 1000
    assert body["event_id"] == "evt_pi_1"


# ---------------------------------------------------------------------------
# 2. _lookup_grant_tx_for_payment_intent returns None when stripe_payment_grants empty.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_returns_none_when_stripe_payment_grants_empty():
    """With the global fallback removed, an empty stripe_payment_grants response
    must return None — never a random other user's grant."""
    cfg = _cfg()

    empty_resp = _FakeResp(200, [])
    # A fallback would have returned something from credit_transactions.
    fallback_resp = _FakeResp(200, [
        {
            "id": "tx-WRONG",
            "user_id": "user-uuid-WRONG",
            "credits": 9999,
            "ref_id": "evt_wrong",
        }
    ])

    client = _RecordingClient(
        by_path={
            "stripe_payment_grants": empty_resp,
            "credit_transactions": fallback_resp,
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        result = await mod._lookup_grant_tx_for_payment_intent("pi_notfound", cfg)

    assert result is None, (
        "_lookup_grant_tx_for_payment_intent must return None when stripe_payment_grants "
        "is empty, not fall back to credit_transactions global query"
    )


# ---------------------------------------------------------------------------
# 3. Refund for user A's pi_id resolves user A, never user B.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refund_for_user_a_pi_resolves_user_a_not_user_b():
    """With two users having grants, refund for user A's pi_id must only
    touch user A's account, not the more recent user B grant."""
    cfg = _cfg()

    user_a_id = "user-uuid-aaaa"
    user_b_id = "user-uuid-bbbb"
    pi_a = "pi_user_a"

    # stripe_payment_grants returns user A's record for pi_a
    pg_resp_for_a = _FakeResp(200, [{
        "user_id": user_a_id,
        "credits": 500,
        "event_id": "evt_a",
    }])
    # credit_transactions would return user B's row if fallback were used
    ct_resp_fallback = _FakeResp(200, [{
        "id": "tx-b",
        "user_id": user_b_id,
        "credits": 999,
        "ref_id": "evt_b",
        "metadata": {},
    }])
    refund_resp = _FakeResp(200, {"status": "reversed", "credits_debited": 500, "balance_after": 0})

    client = _RecordingClient(
        by_path={
            "stripe_payment_grants": pg_resp_for_a,
            "credit_transactions": ct_resp_fallback,
            "rpc/refund_credits": refund_resp,
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._reverse_credits_for_charge(
            {"id": "ch_a", "payment_intent": pi_a, "amount": 1000, "amount_refunded": 1000},
            cfg,
            event_id="evt_refund_a",
        )

    rpc_calls = [c for c in client.calls if "rpc/refund_credits" in c["url"]]
    assert len(rpc_calls) == 1
    assert rpc_calls[0]["json"]["p_user_id"] == user_a_id, (
        "Refund must target user A, not user B"
    )
    assert rpc_calls[0]["json"]["p_user_id"] != user_b_id


# ---------------------------------------------------------------------------
# 4. Duplicate grant status MUST still attempt stripe_payment_grants insert.
#    L-01: a prior delivery may have granted credits but failed the mapping
#    write; Stripe-retry of the same event must heal the missing row. The
#    Prefer: resolution=ignore-duplicates,return=minimal header makes a
#    repeat insert against an already-present row a safe no-op.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_grant_status_still_attempts_stripe_payment_grants_insert():
    """L-01: When grant_credits RPC returns status='duplicate', we must STILL
    attempt the stripe_payment_grants insert so a prior failed mapping write
    can heal on retry. The ignore-duplicates Prefer header collapses repeats.
    """
    cfg = _cfg()

    dup_rpc_resp = _FakeResp(200, {"status": "duplicate", "transaction_id": "tx-existing"})
    pg_insert_resp = _FakeResp(201, {})

    client = _RecordingClient(
        by_path={
            "rpc/grant_credits": dup_rpc_resp,
            "stripe_payment_grants": pg_insert_resp,
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._grant_credits_for_event(
            user_id="user-uuid-bbb",
            credits=2000,
            source="plan_renewal",
            ref_id="evt_dup_1",
            expires_at=None,
            cfg=cfg,
            pi_id="pi_dup_123",
        )

    insert_calls = [
        c for c in client.calls
        if c["method"] == "POST" and "stripe_payment_grants" in c["url"]
    ]
    assert len(insert_calls) == 1, (
        "L-01: stripe_payment_grants insert must be attempted even on duplicate "
        "grant so a missing mapping row can heal on Stripe retry"
    )


# ---------------------------------------------------------------------------
# 5. pi_id=None on checkout still runs grant without error.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_with_no_pi_id_runs_without_error():
    """When pi_id is None (e.g. subscription checkout where pi is unavailable),
    _grant_credits_for_event must still complete successfully and NOT attempt
    to insert into stripe_payment_grants."""
    cfg = _cfg()

    grant_rpc_resp = _FakeResp(200, {"status": "granted", "credits": 500, "balance_after": 500})
    client = _RecordingClient(
        by_path={"rpc/grant_credits": grant_rpc_resp}
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        # Must not raise
        await mod._grant_credits_for_event(
            user_id="user-uuid-ccc",
            credits=500,
            source="plan_renewal",
            ref_id="evt_no_pi",
            expires_at=None,
            cfg=cfg,
            pi_id=None,
        )

    insert_calls = [
        c for c in client.calls
        if c["method"] == "POST" and "stripe_payment_grants" in c["url"]
    ]
    assert len(insert_calls) == 0, (
        "No stripe_payment_grants insert when pi_id is None"
    )
    rpc_calls = [c for c in client.calls if "rpc/grant_credits" in c["url"]]
    assert len(rpc_calls) == 1, "grant_credits RPC must still be called"


# ---------------------------------------------------------------------------
# 6. _lookup_grant_tx_for_payment_intent returns correct fields from row.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_returns_correct_fields_from_stripe_payment_grants():
    """When stripe_payment_grants has a matching row, the returned dict must
    expose user_id, credits, and event_id for use by the reversal logic."""
    cfg = _cfg()

    pg_row = {
        "user_id": "user-uuid-ddd",
        "credits": 3500,
        "event_id": "evt_topup_42",
    }
    pg_resp = _FakeResp(200, [pg_row])

    client = _RecordingClient(
        by_path={"stripe_payment_grants": pg_resp}
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        result = await mod._lookup_grant_tx_for_payment_intent("pi_topup_42", cfg)

    assert result is not None, "Must return a row when stripe_payment_grants has a match"
    assert result["user_id"] == "user-uuid-ddd"
    assert result["credits"] == 3500
    assert result["event_id"] == "evt_topup_42"
