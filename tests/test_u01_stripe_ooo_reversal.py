"""U-01 regression suite: out-of-order Stripe reversal handling.

Bug:
    When Stripe delivers ``charge.refunded`` or ``charge.dispute.*`` BEFORE
    the ``charge.succeeded`` / ``payment_intent.succeeded`` event that
    creates the ``stripe_payment_grants`` mapping row, the legacy
    ``_reverse_credits_for_charge`` finds no grant, logs
    ``charge_reversal_no_grant_found``, and returns success. The outer
    webhook dispatcher then marks the event 'completed' in
    ``stripe_webhook_events``, so Stripe stops retrying. The later-arriving
    grant is credited but never reversed — refunded credits remain.

Fix shape:
    1. The no-grant path now persists a row in ``stripe_pending_reversals``
       (event_id UNIQUE for idempotent Stripe-replays).
    2. When ``_grant_credits_for_event`` inserts the grant mapping, it
       reconciles any matching pending reversals via the same
       ``process_charge_reversal`` RPC and stamps ``applied_at``.
    3. The defensive double-coverage path also fires the reversal at
       grant time when the Stripe charge object already shows
       ``refunded == True`` or ``disputed == True``.

These tests stub ``httpx.AsyncClient`` so they exercise the full
``_handle_charge_refunded`` → ``_reverse_credits_for_charge`` → Supabase
REST surface, and the grant-insert reconciliation, in isolation.
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
# Shared helpers (mirrors test_b04 fixtures).
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
        if isinstance(self._body, str):
            return json.loads(self._body) if self._body else None
        return self._body


class _OOOFakeSupabase:
    """In-memory model of stripe_payment_grants + stripe_pending_reversals +
    stripe_dispute_reversals + the process_charge_reversal RPC.

    Implements just enough of the PostgREST surface that the U-01 test
    paths exercise.
    """

    def __init__(self) -> None:
        # keyed by payment_intent_id
        self.payment_grants: dict[str, dict[str, Any]] = {}
        # keyed by event_id
        self.pending_reversals: dict[str, dict[str, Any]] = {}
        # keyed by reversal_key
        self.dispute_reversals: dict[str, dict[str, Any]] = {}
        # ordered RPC calls for assertions
        self.refund_rpc_calls: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    # ---- httpx context-manager surface ---------------------------------
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):  # noqa: ANN001
        return None

    # ---- routing helpers ----------------------------------------------
    def _match(self, url: str) -> str:
        if "rpc/process_charge_reversal" in url:
            return "rpc:process_charge_reversal"
        if "stripe_pending_reversals" in url:
            return "table:stripe_pending_reversals"
        if "stripe_payment_grants" in url:
            return "table:stripe_payment_grants"
        if "stripe_dispute_reversals" in url:
            return "table:stripe_dispute_reversals"
        if "rpc/grant_credits" in url:
            return "rpc:grant_credits"
        return "unknown"

    @staticmethod
    def _parse_eq_filter(url: str, column: str) -> str | None:
        marker = f"{column}=eq."
        if marker not in url:
            return None
        tail = url.split(marker, 1)[1]
        # cut at first & or ?
        for term in ("&", "?"):
            if term in tail:
                tail = tail.split(term, 1)[0]
        return tail

    # ---- POST ----------------------------------------------------------
    async def post(self, url: str, json: Any = None, headers: Any = None):  # noqa: A002
        kind = self._match(url)
        self.calls.append({"method": "POST", "kind": kind, "url": url, "json": json})

        if kind == "table:stripe_payment_grants":
            payload = json or {}
            pi_id = payload.get("payment_intent_id")
            if pi_id and pi_id not in self.payment_grants:
                self.payment_grants[pi_id] = dict(payload)
            return _FakeResp(201, [])

        if kind == "table:stripe_pending_reversals":
            payload = json or {}
            event_id = payload.get("event_id")
            if event_id and event_id not in self.pending_reversals:
                row = dict(payload)
                row.setdefault("applied_at", None)
                self.pending_reversals[event_id] = row
            return _FakeResp(201, [])

        if kind == "rpc:process_charge_reversal":
            payload = json or {}
            reversal_key = payload.get("p_reversal_key")
            charge_id = payload.get("p_charge_id")
            target = int(payload.get("p_target_credits") or 0)
            if reversal_key in self.dispute_reversals:
                return _FakeResp(200, {"status": "duplicate", "credits": 0})
            already = sum(
                int(r.get("credits") or 0)
                for r in self.dispute_reversals.values()
                if r.get("charge_id") == charge_id
            )
            incremental = max(0, target - already)
            self.dispute_reversals[reversal_key] = {
                "reversal_key": reversal_key,
                "user_id": payload.get("p_user_id"),
                "charge_id": charge_id,
                "dispute_id": payload.get("p_dispute_id"),
                "payment_intent_id": payload.get("p_payment_intent_id"),
                "credits": incremental,
                "first_event_id": payload.get("p_first_event_id"),
                "first_event_type": payload.get("p_first_event_type"),
            }
            self.refund_rpc_calls.append(
                {
                    "user_id": payload.get("p_user_id"),
                    "credits": incremental,
                    "ref_id": reversal_key,
                    "charge_id": charge_id,
                    "first_event_id": payload.get("p_first_event_id"),
                }
            )
            if incremental <= 0:
                return _FakeResp(200, {"status": "already_satisfied", "credits": 0})
            return _FakeResp(200, {"status": "reversed", "credits": incremental})

        if kind == "rpc:grant_credits":
            # Mariana's billing.ledger.grant_credits posts to /rpc/grant_credits.
            # For U-01 tests we route the grant insert through
            # _grant_credits_for_event, which always posts to grant_credits
            # before the stripe_payment_grants insert.
            return _FakeResp(200, {"status": "granted", "credits": int((json or {}).get("p_credits") or 0)})

        return _FakeResp(200, [])

    # ---- PATCH ---------------------------------------------------------
    async def patch(self, url: str, json: Any = None, headers: Any = None):  # noqa: A002
        kind = self._match(url)
        self.calls.append({"method": "PATCH", "kind": kind, "url": url, "json": json})

        if kind == "table:stripe_pending_reversals":
            event_id = self._parse_eq_filter(url, "event_id")
            if event_id and event_id in self.pending_reversals:
                self.pending_reversals[event_id].update(json or {})
            return _FakeResp(204, [])

        return _FakeResp(204, [])

    # ---- GET -----------------------------------------------------------
    async def get(self, url: str, params: Any = None, headers: Any = None):
        kind = self._match(url)
        self.calls.append({"method": "GET", "kind": kind, "url": url, "params": params})

        if kind == "table:stripe_payment_grants":
            pi_id = self._parse_eq_filter(url, "payment_intent_id")
            row = self.payment_grants.get(pi_id) if pi_id else None
            return _FakeResp(200, [row] if row else [])

        if kind == "table:stripe_pending_reversals":
            charge_id = self._parse_eq_filter(url, "charge_id")
            pi_id = self._parse_eq_filter(url, "payment_intent_id")
            applied_filter = "applied_at=is.null" in url
            rows = list(self.pending_reversals.values())
            if charge_id is not None:
                rows = [r for r in rows if r.get("charge_id") == charge_id]
            if pi_id is not None:
                rows = [r for r in rows if r.get("payment_intent_id") == pi_id]
            if applied_filter:
                rows = [r for r in rows if r.get("applied_at") is None]
            return _FakeResp(200, rows)

        if kind == "table:stripe_dispute_reversals":
            charge_id = self._parse_eq_filter(url, "charge_id") or (params or {}).get("charge_id")
            if isinstance(charge_id, str) and charge_id.startswith("eq."):
                charge_id = charge_id[3:]
            rows = [r for r in self.dispute_reversals.values() if r.get("charge_id") == charge_id]
            return _FakeResp(200, rows)

        return _FakeResp(200, [])


@pytest.fixture(autouse=True)
def _patch_supabase_api_key():
    with patch.object(mod, "_supabase_api_key", lambda cfg: "service_role_xxx"):
        yield


# ---------------------------------------------------------------------------
# Test 1 — bare reversal: charge.refunded BEFORE the grant. The reversal must
# park a stripe_pending_reversals row keyed by event_id and return cleanly so
# the outer dispatcher can finalize the webhook event row.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_charge_refunded_before_grant_persists_pending_row():
    cfg = _cfg()
    fake = _OOOFakeSupabase()

    with patch.object(httpx, "AsyncClient", return_value=fake):
        await mod._handle_charge_refunded(
            {
                "id": "ch_ooo_1",
                "payment_intent": "pi_ooo_1",
                "amount": 3000,
                "amount_refunded": 3000,
                "currency": "usd",
            },
            cfg,
            event_id="evt_ref_ooo_1",
        )

    # No reversal RPC fires yet — there is nothing to reverse.
    assert len(fake.refund_rpc_calls) == 0

    # A pending row was recorded for the OOO refund event.
    assert "evt_ref_ooo_1" in fake.pending_reversals
    pending = fake.pending_reversals["evt_ref_ooo_1"]
    assert pending["payment_intent_id"] == "pi_ooo_1"
    assert pending["charge_id"] == "ch_ooo_1"
    assert pending["kind"] == "refund"
    assert pending["applied_at"] is None


# ---------------------------------------------------------------------------
# Test 2 — full out-of-order sequence: charge.refunded first, then the grant
# arrives via _grant_credits_for_event. The grant-insert reconciler must
# replay the pending reversal and stamp applied_at. Net credits must be zero.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ooo_refund_then_grant_net_zero_credits():
    cfg = _cfg()
    fake = _OOOFakeSupabase()
    user_id = "11111111-2222-3333-4444-555555555555"

    # Phase 1 — refund arrives first; grant table is still empty.
    with patch.object(httpx, "AsyncClient", return_value=fake):
        await mod._handle_charge_refunded(
            {
                "id": "ch_ooo_2",
                "payment_intent": "pi_ooo_2",
                "amount": 2000,
                "amount_refunded": 2000,
                "currency": "usd",
            },
            cfg,
            event_id="evt_ref_ooo_2",
        )

    # Pending row in place; no debit yet.
    assert "evt_ref_ooo_2" in fake.pending_reversals
    assert fake.pending_reversals["evt_ref_ooo_2"]["applied_at"] is None
    assert len(fake.refund_rpc_calls) == 0

    # Phase 2 — the original grant arrives. _grant_credits_for_event posts
    # to grant_credits then writes stripe_payment_grants then reconciles
    # any pending reversals for that payment_intent / charge.
    with patch.object(httpx, "AsyncClient", return_value=fake):
        await mod._grant_credits_for_event(
            user_id=user_id,
            credits=2000,
            source="topup",
            ref_id="evt_pi_ooo_2",
            expires_at=None,
            cfg=cfg,
            pi_id="pi_ooo_2",
            charge_id="ch_ooo_2",
            charge_amount=2000,
        )

    # Grant row exists.
    assert "pi_ooo_2" in fake.payment_grants
    # Pending row was applied.
    assert fake.pending_reversals["evt_ref_ooo_2"]["applied_at"] is not None
    # Exactly one reversal RPC fired and it debited the full 2000 credits.
    assert len(fake.refund_rpc_calls) == 1
    rpc = fake.refund_rpc_calls[0]
    assert rpc["user_id"] == user_id
    assert rpc["credits"] == 2000
    assert rpc["charge_id"] == "ch_ooo_2"
    assert rpc["ref_id"] == "refund_event:evt_ref_ooo_2"


# ---------------------------------------------------------------------------
# Test 3 — Stripe-replay of the original OOO refund event after the grant
# already arrived must NOT double-debit. The pending row's UNIQUE(event_id)
# plus applied_at and the underlying process_charge_reversal dedup all
# guard the second pass.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ooo_refund_replayed_after_reconciliation_is_idempotent():
    cfg = _cfg()
    fake = _OOOFakeSupabase()
    user_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    with patch.object(httpx, "AsyncClient", return_value=fake):
        await mod._handle_charge_refunded(
            {
                "id": "ch_ooo_3",
                "payment_intent": "pi_ooo_3",
                "amount": 1500,
                "amount_refunded": 1500,
                "currency": "usd",
            },
            cfg,
            event_id="evt_ref_ooo_3",
        )

        await mod._grant_credits_for_event(
            user_id=user_id,
            credits=1500,
            source="topup",
            ref_id="evt_pi_ooo_3",
            expires_at=None,
            cfg=cfg,
            pi_id="pi_ooo_3",
            charge_id="ch_ooo_3",
            charge_amount=1500,
        )

        # Stripe replays the original OOO refund event (3-day retry window).
        await mod._handle_charge_refunded(
            {
                "id": "ch_ooo_3",
                "payment_intent": "pi_ooo_3",
                "amount": 1500,
                "amount_refunded": 1500,
                "currency": "usd",
            },
            cfg,
            event_id="evt_ref_ooo_3",
        )

    # Still exactly ONE reversal RPC. The replay was deduped by the underlying
    # process_charge_reversal stripe_dispute_reversals.reversal_key uniqueness.
    assert len(fake.refund_rpc_calls) == 1
    assert fake.refund_rpc_calls[0]["credits"] == 1500


# ---------------------------------------------------------------------------
# Test 4 — defensive double-coverage: if charge.succeeded arrives carrying
# refunded=True / amount_refunded > 0 / disputed=True flags AND no OOO
# pending row was recorded, the grant path itself must trigger the reversal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_with_refunded_flag_triggers_defensive_reversal():
    cfg = _cfg()
    fake = _OOOFakeSupabase()
    user_id = "99999999-8888-7777-6666-555555555555"

    with patch.object(httpx, "AsyncClient", return_value=fake):
        await mod._grant_credits_for_event(
            user_id=user_id,
            credits=1000,
            source="topup",
            ref_id="evt_pi_def_1",
            expires_at=None,
            cfg=cfg,
            pi_id="pi_def_1",
            charge_id="ch_def_1",
            charge_amount=1000,
            stripe_charge={
                "id": "ch_def_1",
                "payment_intent": "pi_def_1",
                "amount": 1000,
                "amount_refunded": 1000,
                "refunded": True,
                "disputed": False,
                "currency": "usd",
            },
        )

    assert "pi_def_1" in fake.payment_grants
    # Defensive reversal must have fired exactly once.
    assert len(fake.refund_rpc_calls) == 1
    assert fake.refund_rpc_calls[0]["credits"] == 1000
    assert fake.refund_rpc_calls[0]["user_id"] == user_id
