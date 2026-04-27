"""L-01 regression suite: stripe_payment_grants mapping insert failure handling.

Bug fixed:
- _grant_credits_for_event ignored the HTTP status of the auxiliary
  POST /rest/v1/stripe_payment_grants mapping insert. A non-2xx response
  (e.g. transient PostgREST 5xx, schema/permission mismatch) was treated as
  success — the webhook finalized, the user kept the credits, but the
  pi-to-grant mapping row was missing. Worse, a Stripe retry of the same
  event_id encountered grant_credits status='duplicate' and the prior code
  explicitly skipped the mapping insert in that case, so the missing row
  could never heal. Later refund/dispute events would find no exact mapping
  and silently skip the reversal — money leak.

Fix:
- Always attempt the mapping insert when pi_id is provided (the Prefer:
  resolution=ignore-duplicates header makes repeats safe).
- Check resp.status_code on the POST. If not in {200, 201, 204}, log the
  body and raise HTTPException(status_code=503) so Stripe retries the
  whole event and the mapping write becomes durable.
- Transport exceptions also raise 503 (previously they were swallowed).

Test inventory (5):
  A. grant succeeds + mapping returns 500 -> raises 503
  B. retry path: grant returns duplicate, mapping retried even on
     duplicate (heal the missing row)
  C. happy path regression: both succeed first try
  D. idempotency: same-event retry where both succeed twice (Prefer header
     handles duplicate row safely)
  E. duplicate grant + mapping ROW EXISTS (PostgREST 201 with empty body
     under ignore-duplicates) — no error
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException

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
        self.text = json.dumps(self._body) if not isinstance(self._body, str) else self._body

    def json(self) -> Any:
        return self._body


class _ScriptedClient:
    """Fake httpx.AsyncClient that returns canned responses by URL substring.

    Records every call. Supports per-path response queues so a single test
    can drive multiple retries with different outcomes.
    """

    def __init__(
        self,
        *,
        by_path: dict[str, list[_FakeResp]] | None = None,
        default_response: _FakeResp | None = None,
    ) -> None:
        self.queues: dict[str, list[_FakeResp]] = by_path or {}
        self.default_response = default_response or _FakeResp(201, {})
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def _next(self, url: str) -> _FakeResp:
        for path, queue in self.queues.items():
            if path in url:
                if not queue:
                    raise AssertionError(
                        f"_ScriptedClient: queue exhausted for path '{path}' (url={url})"
                    )
                # If only one item left, keep returning it (sticky behavior).
                if len(queue) == 1:
                    return queue[0]
                return queue.pop(0)
        return self.default_response

    async def post(self, url: str, json=None, headers=None):
        self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers})
        return self._next(url)

    async def get(self, url: str, params=None, headers=None):
        self.calls.append({"method": "GET", "url": url, "params": params, "headers": headers})
        return self._next(url)

    async def patch(self, url: str, json=None, headers=None):
        self.calls.append({"method": "PATCH", "url": url, "json": json, "headers": headers})
        return self._next(url)


@pytest.fixture(autouse=True)
def _patch_supabase_api_key():
    with patch.object(mod, "_supabase_api_key", lambda cfg: "service_role_xxx"):
        yield


def _mapping_post_calls(client: _ScriptedClient) -> list[dict[str, Any]]:
    return [
        c for c in client.calls
        if c["method"] == "POST" and "stripe_payment_grants" in c["url"]
        and "rpc/" not in c["url"]
    ]


def _grant_rpc_calls(client: _ScriptedClient) -> list[dict[str, Any]]:
    return [c for c in client.calls if "rpc/grant_credits" in c["url"]]


# ---------------------------------------------------------------------------
# A. Grant succeeds, mapping POST returns 500 -> must raise 503 so Stripe retries.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_mapping_insert_500_raises_503_for_stripe_retry():
    cfg = _cfg()

    grant_ok = _FakeResp(200, {"status": "granted", "credits": 1000, "balance_after": 1000})
    mapping_500 = _FakeResp(500, {"message": "internal server error"})

    client = _ScriptedClient(
        by_path={
            "rpc/grant_credits": [grant_ok],
            "stripe_payment_grants": [mapping_500],
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        with pytest.raises(HTTPException) as excinfo:
            await mod._grant_credits_for_event(
                user_id="user-uuid-l01-a",
                credits=1000,
                source="topup",
                ref_id="evt_l01_a",
                expires_at=None,
                cfg=cfg,
                pi_id="pi_l01_a",
                charge_id="ch_l01_a",
                charge_amount=1000,
            )

    assert excinfo.value.status_code == 503, (
        "Non-2xx mapping insert must raise 503 so Stripe retries the event"
    )
    # The mapping POST was actually attempted.
    assert len(_mapping_post_calls(client)) == 1


# ---------------------------------------------------------------------------
# B. Retry path: first call mapping fails, second call grant returns duplicate.
#    Mapping must be retried (not skipped on duplicate).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b_duplicate_grant_still_retries_mapping_to_heal_missing_row():
    cfg = _cfg()

    # First Stripe delivery: grant ok, mapping 500 -> 503 raised.
    # Second Stripe delivery: grant returns duplicate (idempotent), mapping
    # must STILL be attempted so the missing row can be healed.
    grant_dup = _FakeResp(200, {"status": "duplicate", "transaction_id": "tx-existing"})
    mapping_ok = _FakeResp(201, {})

    client = _ScriptedClient(
        by_path={
            "rpc/grant_credits": [grant_dup],
            "stripe_payment_grants": [mapping_ok],
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        # Must NOT raise — the heal succeeded.
        await mod._grant_credits_for_event(
            user_id="user-uuid-l01-b",
            credits=1000,
            source="topup",
            ref_id="evt_l01_b",
            expires_at=None,
            cfg=cfg,
            pi_id="pi_l01_b",
            charge_id="ch_l01_b",
            charge_amount=1000,
        )

    mapping_calls = _mapping_post_calls(client)
    assert len(mapping_calls) == 1, (
        "Mapping insert must be attempted even when grant_credits returns duplicate, "
        "so a missing mapping row from a prior failed attempt can heal on retry"
    )
    body = mapping_calls[0]["json"]
    assert body["payment_intent_id"] == "pi_l01_b"
    assert body["event_id"] == "evt_l01_b"
    # And the Prefer header keeps the duplicate-row case safe.
    headers = mapping_calls[0]["headers"]
    assert "ignore-duplicates" in headers.get("Prefer", "")


# ---------------------------------------------------------------------------
# C. Happy path regression: both succeed first try.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c_happy_path_grant_and_mapping_both_succeed():
    cfg = _cfg()

    grant_ok = _FakeResp(200, {"status": "granted", "credits": 500, "balance_after": 500})
    mapping_ok = _FakeResp(201, {})

    client = _ScriptedClient(
        by_path={
            "rpc/grant_credits": [grant_ok],
            "stripe_payment_grants": [mapping_ok],
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._grant_credits_for_event(
            user_id="user-uuid-l01-c",
            credits=500,
            source="topup",
            ref_id="evt_l01_c",
            expires_at=None,
            cfg=cfg,
            pi_id="pi_l01_c",
            charge_id="ch_l01_c",
            charge_amount=500,
        )

    assert len(_grant_rpc_calls(client)) == 1
    assert len(_mapping_post_calls(client)) == 1


# ---------------------------------------------------------------------------
# D. Same-event retry idempotency: second invocation with grant=duplicate
#    and mapping returning a 2xx (PostgREST under ignore-duplicates returns
#    201 with empty body whether the row was inserted or skipped). Must
#    succeed without error.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d_same_event_retry_idempotent_no_error():
    cfg = _cfg()

    grant_ok = _FakeResp(200, {"status": "granted", "credits": 750, "balance_after": 750})
    grant_dup = _FakeResp(200, {"status": "duplicate", "transaction_id": "tx-existing"})
    # Both mapping responses 201 — server-side ignore-duplicates collapses safely.
    mapping_ok_a = _FakeResp(201, {})
    mapping_ok_b = _FakeResp(201, {})

    # Two sequential responses for grant_credits, two for stripe_payment_grants.
    client = _ScriptedClient(
        by_path={
            "rpc/grant_credits": [grant_ok, grant_dup],
            "stripe_payment_grants": [mapping_ok_a, mapping_ok_b],
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        # First delivery.
        await mod._grant_credits_for_event(
            user_id="user-uuid-l01-d",
            credits=750,
            source="topup",
            ref_id="evt_l01_d",
            expires_at=None,
            cfg=cfg,
            pi_id="pi_l01_d",
            charge_id="ch_l01_d",
            charge_amount=750,
        )
        # Second delivery (Stripe retry of the same event).
        await mod._grant_credits_for_event(
            user_id="user-uuid-l01-d",
            credits=750,
            source="topup",
            ref_id="evt_l01_d",
            expires_at=None,
            cfg=cfg,
            pi_id="pi_l01_d",
            charge_id="ch_l01_d",
            charge_amount=750,
        )

    # Two mapping inserts attempted across the two deliveries.
    assert len(_mapping_post_calls(client)) == 2


# ---------------------------------------------------------------------------
# E. Duplicate grant + mapping returns 200 with empty array (PostgREST under
#    Prefer: resolution=ignore-duplicates,return=minimal commonly responds
#    with 201 and an empty body). Should not raise.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e_duplicate_grant_with_existing_mapping_no_error():
    cfg = _cfg()

    grant_dup = _FakeResp(200, {"status": "duplicate", "transaction_id": "tx-existing"})
    # 201 with empty body is the expected PostgREST response when the row
    # already exists under ignore-duplicates,return=minimal.
    mapping_ok = _FakeResp(201, "")

    client = _ScriptedClient(
        by_path={
            "rpc/grant_credits": [grant_dup],
            "stripe_payment_grants": [mapping_ok],
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        # Must not raise — duplicate row collapse is the success case.
        await mod._grant_credits_for_event(
            user_id="user-uuid-l01-e",
            credits=200,
            source="plan_renewal",
            ref_id="evt_l01_e",
            expires_at=None,
            cfg=cfg,
            pi_id="pi_l01_e",
        )

    assert len(_mapping_post_calls(client)) == 1


# ---------------------------------------------------------------------------
# F. Transport exception on mapping POST must also raise 503 (was previously
#    silently logged-and-swallowed).
# ---------------------------------------------------------------------------


class _RaisingClient:
    """httpx.AsyncClient stand-in: grant succeeds via POST, mapping POST raises."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url: str, json=None, headers=None):
        self.calls.append({"method": "POST", "url": url, "json": json})
        if "rpc/grant_credits" in url:
            return _FakeResp(200, {"status": "granted", "credits": 100, "balance_after": 100})
        if "stripe_payment_grants" in url:
            raise httpx.ConnectError("simulated transport error")
        return _FakeResp(200, {})

    async def get(self, url: str, params=None, headers=None):
        return _FakeResp(200, [])


@pytest.mark.asyncio
async def test_f_transport_exception_on_mapping_raises_503():
    cfg = _cfg()
    client = _RaisingClient()

    with patch.object(httpx, "AsyncClient", return_value=client):
        with pytest.raises(HTTPException) as excinfo:
            await mod._grant_credits_for_event(
                user_id="user-uuid-l01-f",
                credits=100,
                source="topup",
                ref_id="evt_l01_f",
                expires_at=None,
                cfg=cfg,
                pi_id="pi_l01_f",
                charge_id="ch_l01_f",
                charge_amount=100,
            )

    assert excinfo.value.status_code == 503, (
        "Transport exceptions on the mapping insert must raise 503 so Stripe retries; "
        "previously these were silently swallowed"
    )
