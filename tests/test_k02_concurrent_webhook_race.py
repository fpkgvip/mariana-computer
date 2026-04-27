"""K-02 regression suite: two concurrent webhook handlers for the same charge
must NOT double-debit via TOCTOU between SELECT-sum and INSERT-dedup-row.

Bug fixed:
- _reverse_credits_for_charge had a check-then-act sequence:
    1. SELECT existing reversal_key (dedup)
    2. SELECT SUM(credits) WHERE charge_id (= already_reversed)
    3. compute incremental = target - already_reversed
    4. call refund_credits RPC
    5. INSERT dedup row
  Two concurrent webhooks on the same charge with distinct event_ids both pass
  step 1 (different reversal_keys), both observe already_reversed=N at step 2
  (neither has inserted its dedup row yet), both compute non-overlapping
  incremental debits at step 3, and the RPC at step 4 does not collapse them
  because ref_id differs. Net cumulative debit > true cumulative refund.

- Fix: a SECURITY DEFINER PL/pgSQL function process_charge_reversal that
  acquires pg_advisory_xact_lock(hashtextextended('charge:'||charge_id, 0))
  at the start, performs the dedup SELECT + sum + refund_credits + INSERT
  inside a single transaction, and releases the lock at commit. Two concurrent
  callers serialize: the second waits, sees the first's INSERT, and computes
  the correct incremental delta.

Test inventory (>=2 per task brief):
  1. concurrent_two_partial_refunds_30_then_50_total_debit_50
  2. concurrent_refund_30_and_dispute_full_total_debit_100
  3. concurrent_same_event_id_replay_idempotent
  4. sequential_two_partial_refunds_still_correct_30_then_20_regression
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


class _ConcurrentStatefulClient:
    """Shared-state simulator that supports concurrent dispatch.

    Each call to the constructor returns the same instance via __aenter__,
    so all httpx.AsyncClient() context managers in the module under test
    share the same state.

    process_charge_reversal is implemented atomically under per-instance
    asyncio.Lock to model the per-charge advisory_xact_lock. This means
    that even when handlers race, the RPC serializes them — that is exactly
    the property under test.

    A `gate` attribute lets a test pause the FIRST refund-RPC call mid-flight
    (legacy buggy code path) OR the FIRST process_charge_reversal call (fixed
    code path), so that the second handler races into the same code path. In
    both cases the second handler must observe the cumulative state and
    compute the correct incremental delta.
    """

    def __init__(self, grant_credits: int = 100, charge_amount: int = 10000) -> None:
        self.calls: list[dict] = []
        self.inserted_reversals: list[dict] = []
        self.grant_credits = grant_credits
        self.charge_amount = charge_amount
        # Models the per-charge pg_advisory_xact_lock taken inside the SQL function.
        self._charge_locks: dict[str, asyncio.Lock] = {}
        self.gate: asyncio.Event | None = None
        self.gate_pause_first_only = True
        self._first_seen_refund = False
        self._first_seen_rpc = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def _lock_for(self, charge_id: str) -> asyncio.Lock:
        lock = self._charge_locks.get(charge_id)
        if lock is None:
            lock = asyncio.Lock()
            self._charge_locks[charge_id] = lock
        return lock

    async def post(self, url: str, json=None, headers=None):
        self.calls.append({"method": "POST", "url": url, "json": json})
        if "stripe_dispute_reversals" in url and json:
            self.inserted_reversals.append(json)
            return _FakeResp(201, {})
        if "rpc/refund_credits" in url:
            # Legacy buggy path uses refund_credits directly; gate the FIRST
            # call so the second handler races in and observes already_reversed=0.
            if self.gate is not None:
                pause = (not self._first_seen_refund) if self.gate_pause_first_only else True
                if pause:
                    self._first_seen_refund = True
                    await self.gate.wait()
            return _FakeResp(200, {"status": "reversed", "credits_debited": 1})
        if "rpc/process_charge_reversal" in url:
            payload = json or {}
            charge_id = payload.get("p_charge_id") or ""
            reversal_key = payload.get("p_reversal_key")
            target = int(payload.get("p_target_credits") or 0)

            # Pause the FIRST entrant before it acquires the per-charge lock,
            # so that the second event has time to attempt the same RPC and
            # demonstrably blocks on the lock rather than racing.
            if self.gate is not None:
                pause = (not self._first_seen_rpc) if self.gate_pause_first_only else True
                if pause:
                    self._first_seen_rpc = True
                    await self.gate.wait()

            async with self._lock_for(charge_id):
                # dedup
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
                # Track the implied refund_credits debit so tests can assert totals.
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
            return _FakeResp(
                200,
                [
                    {
                        "user_id": "user-k02",
                        "credits": self.grant_credits,
                        "event_id": "evt_grant",
                        "charge_amount": self.charge_amount,
                    }
                ],
            )
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


def _refund_calls(client: _ConcurrentStatefulClient) -> list[dict]:
    return [c for c in client.calls if "rpc/refund_credits" in c["url"]]


def _credits_arg(call: dict) -> int:
    body = call.get("json") or {}
    return int(body.get("p_credits", body.get("credits", 0)))


# ---------------------------------------------------------------------------
# 1. Concurrent two partial refunds (30% + 50%) on same charge → total = 50
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_two_partial_refunds_30_then_50_total_debit_50():
    """Stripe emits two charge.refunded events concurrently for the same
    charge with cumulative amount_refunded=3000 then 5000. The FIRST is gated
    open mid-flight so the second handler races into the same code path. The
    cumulative debit must be 50, not 80."""
    cfg = _cfg()
    client = _ConcurrentStatefulClient(grant_credits=100, charge_amount=10000)
    client.gate = asyncio.Event()

    async def call_first():
        await mod._handle_charge_refunded(
            {
                "id": "ch_k02a",
                "payment_intent": "pi_k02a",
                "amount": 10000,
                "amount_refunded": 3000,
            },
            cfg,
            event_id="evt_ref_k02a_1",
        )

    async def call_second():
        # tiny delay so the first reaches the gate before second arrives
        await asyncio.sleep(0.01)
        await mod._handle_charge_refunded(
            {
                "id": "ch_k02a",
                "payment_intent": "pi_k02a",
                "amount": 10000,
                "amount_refunded": 5000,
            },
            cfg,
            event_id="evt_ref_k02a_2",
        )

    async def release_after_delay():
        await asyncio.sleep(0.05)
        client.gate.set()

    with patch.object(httpx, "AsyncClient", return_value=client):
        await asyncio.gather(call_first(), call_second(), release_after_delay())

    calls = _refund_calls(client)
    total = sum(_credits_arg(c) for c in calls)
    assert total == 50, (
        f"Concurrent refunds 30% + 50%: cumulative debit must be 50 (true refund), "
        f"got {total} across {len(calls)} RPC calls. Per-call debits: "
        f"{[_credits_arg(c) for c in calls]}"
    )
    # Either 1 or 2 RPC calls are acceptable depending on which event arrives
    # first into the lock. Both handlers acquire the lock; the first to get
    # it computes its incremental, the second sees it and computes the delta.
    assert 1 <= len(calls) <= 2


# ---------------------------------------------------------------------------
# 2. Concurrent partial refund 30% + full dispute → total = 100
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_refund_30_and_dispute_full_total_debit_100():
    """charge.refunded (cumulative 30%) and charge.dispute.created
    (full $100) arrive concurrently for the same charge. Cumulative debit
    must be 100, not 130."""
    cfg = _cfg()
    client = _ConcurrentStatefulClient(grant_credits=100, charge_amount=10000)
    client.gate = asyncio.Event()

    async def call_refund():
        await mod._handle_charge_refunded(
            {
                "id": "ch_k02b",
                "payment_intent": "pi_k02b",
                "amount": 10000,
                "amount_refunded": 3000,
            },
            cfg,
            event_id="evt_ref_k02b",
        )

    async def call_dispute():
        await asyncio.sleep(0.01)
        await mod._handle_charge_dispute_created(
            {
                "id": "dp_k02b",
                "charge": "ch_k02b",
                "payment_intent": "pi_k02b",
                "amount": 10000,
            },
            cfg,
            event_id="evt_disp_k02b",
        )

    async def release_after_delay():
        await asyncio.sleep(0.05)
        client.gate.set()

    with patch.object(httpx, "AsyncClient", return_value=client):
        await asyncio.gather(call_refund(), call_dispute(), release_after_delay())

    calls = _refund_calls(client)
    total = sum(_credits_arg(c) for c in calls)
    assert total == 100, (
        f"Concurrent refund 30% + full dispute: cumulative debit must equal "
        f"the 100-credit grant, got {total} across {len(calls)} RPC calls. "
        f"Per-call debits: {[_credits_arg(c) for c in calls]}"
    )


# ---------------------------------------------------------------------------
# 3. Same event_id replay → idempotent (only one debit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_same_event_id_replay_idempotent():
    """Stripe occasionally retries the same event_id mid-flight. Two
    concurrent handlers for the SAME event_id must only debit once."""
    cfg = _cfg()
    client = _ConcurrentStatefulClient(grant_credits=100, charge_amount=10000)
    client.gate = asyncio.Event()

    payload = {
        "id": "ch_k02c",
        "payment_intent": "pi_k02c",
        "amount": 10000,
        "amount_refunded": 4000,
    }

    async def call(idx: int):
        if idx == 1:
            await asyncio.sleep(0.01)
        await mod._handle_charge_refunded(payload, cfg, event_id="evt_ref_k02c_same")

    async def release():
        await asyncio.sleep(0.05)
        client.gate.set()

    with patch.object(httpx, "AsyncClient", return_value=client):
        await asyncio.gather(call(0), call(1), release())

    calls = _refund_calls(client)
    total = sum(_credits_arg(c) for c in calls)
    assert total == 40, f"Same-event replay must only debit 40 once, got {total}"
    assert len(calls) == 1, f"Same-event replay must collapse to 1 RPC call, got {len(calls)}"


# ---------------------------------------------------------------------------
# 4. Sequential regression: two partial refunds 30%, 50% → 30 then 20
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sequential_two_partial_refunds_still_correct_30_then_20_regression():
    """Sequential J-01 path must still work after the K-02 RPC migration.
    No gate; events run sequentially."""
    cfg = _cfg()
    client = _ConcurrentStatefulClient(grant_credits=100, charge_amount=10000)
    # No gate — sequential.

    with patch.object(httpx, "AsyncClient", return_value=client):
        await mod._handle_charge_refunded(
            {
                "id": "ch_k02d",
                "payment_intent": "pi_k02d",
                "amount": 10000,
                "amount_refunded": 3000,
            },
            cfg,
            event_id="evt_ref_k02d_1",
        )
        await mod._handle_charge_refunded(
            {
                "id": "ch_k02d",
                "payment_intent": "pi_k02d",
                "amount": 10000,
                "amount_refunded": 5000,
            },
            cfg,
            event_id="evt_ref_k02d_2",
        )

    calls = _refund_calls(client)
    assert len(calls) == 2, f"Two distinct events must produce 2 RPC calls, got {len(calls)}"
    debit1 = _credits_arg(calls[0])
    debit2 = _credits_arg(calls[1])
    assert debit1 == 30, f"First refund (30%) should debit 30, got {debit1}"
    assert debit2 == 20, f"Second refund (cumulative 50%) should debit 20, got {debit2}"
