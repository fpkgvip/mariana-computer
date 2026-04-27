"""B-03 regression suite: two-phase Stripe webhook idempotency.

Covers four state transitions exhaustively without requiring a live Postgres:

  1. NEW         — first delivery; handler runs; finalize marks 'completed'
  2. RETRY       — prior pending row (handler crashed); handler runs again;
                   finalize marks 'completed'
  3. DUPLICATE   — prior 'completed' row; handler is skipped
  4. HANDLER FAIL — handler raises; finalize is NOT called; row stays 'pending'
                    (so the next Stripe retry re-runs the handler)

Strategy: the api module accesses the pool through ``_get_db()`` and calls
``pool.fetchrow`` / ``pool.execute``.  We swap ``_db_pool`` for a recording
fake pool that simulates the postgres semantics of our claim CTE.

We also bypass Stripe signature verification by patching
``_stripe.Webhook.construct_event`` and bypass ``_get_config`` so the test
does not depend on env vars.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request

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


class _FakeStripeWebhookEventsTable:
    """Minimal in-memory surrogate of stripe_webhook_events.

    Implements only the SQL paths used by ``_claim_webhook_event``,
    ``_finalize_webhook_event``, and ``_record_webhook_event_failure``.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.fetchrow_calls = 0
        self.execute_calls: list[tuple[str, tuple]] = []

    # ---- _claim_webhook_event ----
    async def claim(self, event_id: str, event_type: str) -> dict[str, Any]:
        self.fetchrow_calls += 1
        prior = self.rows.get(event_id)
        prior_status = prior["status"] if prior is not None else None

        if prior is None:
            self.rows[event_id] = {
                "event_id": event_id,
                "event_type": event_type,
                "status": "pending",
                "attempts": 1,
            }
            return {"prior_status": None, "post_status": "pending"}

        if prior["status"] == "pending":
            prior["attempts"] += 1
            return {"prior_status": "pending", "post_status": "pending"}

        # prior_status == 'completed' — UPDATE WHERE pending filtered out.
        return {"prior_status": "completed", "post_status": None}

    # ---- _finalize_webhook_event ----
    async def finalize(self, event_id: str) -> None:
        if event_id in self.rows:
            self.rows[event_id]["status"] = "completed"

    # ---- _record_webhook_event_failure ----
    async def record_failure(self, event_id: str, err: str) -> None:
        if event_id in self.rows:
            self.rows[event_id]["last_error"] = err


class _FakePool:
    """Fake asyncpg.Pool that dispatches to _FakeStripeWebhookEventsTable."""

    def __init__(self, table: _FakeStripeWebhookEventsTable) -> None:
        self._table = table

    async def fetchrow(self, sql: str, *args):  # noqa: ANN001
        # _claim_webhook_event is the only fetchrow call against this table.
        assert "stripe_webhook_events" in sql, sql
        assert "INSERT" in sql.upper(), "claim must use INSERT...ON CONFLICT"
        event_id, event_type = args
        return await self._table.claim(event_id, event_type)

    async def execute(self, sql: str, *args):  # noqa: ANN001
        self._table.execute_calls.append((sql, args))
        if "SET status        = 'completed'" in sql or "SET status" in sql and "'completed'" in sql:
            await self._table.finalize(args[0])
        elif "SET last_error" in sql:
            await self._table.record_failure(args[0], args[1])
        return "UPDATE 1"


@pytest.fixture
def fake_pool():
    table = _FakeStripeWebhookEventsTable()
    pool = _FakePool(table)
    original = mod._db_pool
    mod._db_pool = pool  # type: ignore[assignment]
    try:
        yield table
    finally:
        mod._db_pool = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Direct claim/finalize unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_first_delivery_returns_new(fake_pool):
    claim = await mod._claim_webhook_event("evt_new_1", "payment_intent.succeeded")
    assert claim == mod._WebhookClaim.NEW
    assert fake_pool.rows["evt_new_1"]["status"] == "pending"
    assert fake_pool.rows["evt_new_1"]["attempts"] == 1


@pytest.mark.asyncio
async def test_claim_pending_retry_returns_retry_and_bumps_attempts(fake_pool):
    fake_pool.rows["evt_retry_1"] = {
        "event_id": "evt_retry_1",
        "event_type": "invoice.paid",
        "status": "pending",
        "attempts": 1,
    }
    claim = await mod._claim_webhook_event("evt_retry_1", "invoice.paid")
    assert claim == mod._WebhookClaim.RETRY
    assert fake_pool.rows["evt_retry_1"]["status"] == "pending"
    assert fake_pool.rows["evt_retry_1"]["attempts"] == 2


@pytest.mark.asyncio
async def test_claim_completed_returns_duplicate(fake_pool):
    fake_pool.rows["evt_done_1"] = {
        "event_id": "evt_done_1",
        "event_type": "checkout.session.completed",
        "status": "completed",
        "attempts": 1,
    }
    claim = await mod._claim_webhook_event("evt_done_1", "checkout.session.completed")
    assert claim == mod._WebhookClaim.DUPLICATE
    # Critically: a duplicate must NOT mutate the row's attempts counter.
    assert fake_pool.rows["evt_done_1"]["attempts"] == 1
    assert fake_pool.rows["evt_done_1"]["status"] == "completed"


@pytest.mark.asyncio
async def test_finalize_marks_completed(fake_pool):
    await mod._claim_webhook_event("evt_fin_1", "invoice.paid")
    assert fake_pool.rows["evt_fin_1"]["status"] == "pending"
    await mod._finalize_webhook_event("evt_fin_1")
    assert fake_pool.rows["evt_fin_1"]["status"] == "completed"


@pytest.mark.asyncio
async def test_record_webhook_event_failure_keeps_pending(fake_pool):
    await mod._claim_webhook_event("evt_fail_1", "invoice.paid")
    await mod._record_webhook_event_failure("evt_fail_1", "boom")
    assert fake_pool.rows["evt_fail_1"]["status"] == "pending"
    assert fake_pool.rows["evt_fail_1"]["last_error"] == "boom"


# ---------------------------------------------------------------------------
# End-to-end webhook handler — the core B-03 invariant
# ---------------------------------------------------------------------------


def _make_event(event_id: str = "evt_e2e_1") -> dict:
    return {
        "id": event_id,
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_1",
                "metadata": {
                    "deft_kind": "topup",
                    "deft_plan_id": "topup_pro",
                    "user_id": "00000000-0000-0000-0000-000000000abc",
                },
            }
        },
    }


async def _invoke_webhook(event: dict) -> tuple[int, dict]:
    """Drive ``mod.stripe_webhook`` directly without spinning up FastAPI.

    The handler signature is ``async stripe_webhook(request: Request) -> Response``.
    We construct a minimal ASGI scope + receive callable so that
    ``await request.body()`` returns our payload.  Stripe signature
    verification is patched to short-circuit and return our event dict.
    """
    payload = json.dumps(event).encode("utf-8")
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"stripe-signature", b"t=1,v1=anything")],
        "path": "/api/billing/webhook",
        "raw_path": b"/api/billing/webhook",
        "query_string": b"",
        "client": ("127.0.0.1", 0),
        "server": ("testserver", 80),
        "scheme": "http",
    }

    async def _receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def _send(_msg):  # pragma: no cover - not used
        pass

    request = Request(scope, _receive, _send)

    cfg = _cfg()
    with patch.object(mod, "_get_config", return_value=cfg), patch.object(
        mod._stripe.Webhook, "construct_event", return_value=event
    ):
        response = await mod.stripe_webhook(request)
    body = json.loads(response.body.decode("utf-8"))
    return response.status_code, body


@pytest.mark.asyncio
async def test_e2e_first_delivery_then_replay_is_skipped(fake_pool):
    """B-03 invariant: a successful handler call marks the event 'completed';
    a subsequent replay is a true no-op."""
    event = _make_event("evt_e2e_dup_1")
    handler = AsyncMock()
    with patch.object(mod, "_handle_payment_intent_succeeded", handler):
        status, body = await _invoke_webhook(event)
        assert status == 200, body
        assert body["status"] == "ok"
        assert handler.await_count == 1
        assert fake_pool.rows["evt_e2e_dup_1"]["status"] == "completed"

        # Replay — handler must NOT be invoked again
        status2, body2 = await _invoke_webhook(event)
        assert status2 == 200
        assert body2["status"] == "duplicate"
        assert handler.await_count == 1  # unchanged


@pytest.mark.asyncio
async def test_e2e_handler_failure_leaves_pending_so_retry_reruns(fake_pool):
    """B-03 critical invariant: when the handler raises, the row MUST stay
    'pending'.  Stripe will retry; on retry the claim returns RETRY and the
    handler runs again.  This is the bug we are fixing."""
    event = _make_event("evt_e2e_retry_1")
    fail_then_succeed = AsyncMock(side_effect=[RuntimeError("transient db error"), None])
    with patch.object(mod, "_handle_payment_intent_succeeded", fail_then_succeed):
        status, body = await _invoke_webhook(event)
        assert status == 500
        assert body["status"] == "handler_error"
        assert fake_pool.rows["evt_e2e_retry_1"]["status"] == "pending", (
            "pending row must remain so Stripe's retry re-runs the handler"
        )
        assert fake_pool.rows["evt_e2e_retry_1"]["last_error"]

        # Stripe retry — handler succeeds this time
        status2, body2 = await _invoke_webhook(event)
        assert status2 == 200
        assert body2["status"] == "ok"
        assert fail_then_succeed.await_count == 2
        assert fake_pool.rows["evt_e2e_retry_1"]["status"] == "completed"
        assert fake_pool.rows["evt_e2e_retry_1"]["attempts"] == 2


@pytest.mark.asyncio
async def test_e2e_finalize_failure_returns_500_so_retry_reruns(fake_pool):
    """If the handler succeeds but ``_finalize_webhook_event`` itself fails,
    we MUST return 500 so Stripe retries.  The next attempt is RETRY and
    re-runs the handler — protected by per-grant ref_id idempotency."""
    event = _make_event("evt_e2e_fin_fail_1")
    handler = AsyncMock()
    with patch.object(mod, "_handle_payment_intent_succeeded", handler), patch.object(
        mod, "_finalize_webhook_event", AsyncMock(side_effect=RuntimeError("network blip"))
    ):
        status, body = await _invoke_webhook(event)
        assert status == 500
        assert body["status"] == "finalize_error"
        # Row was claimed but never marked completed by the *real* finalize
        assert fake_pool.rows["evt_e2e_fin_fail_1"]["status"] == "pending"
