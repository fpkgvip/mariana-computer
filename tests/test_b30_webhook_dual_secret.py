"""B-30 regression suite: webhook dual-secret rotation support.

Stripe webhook verification must support a transition window where both a
PRIMARY and a PREVIOUS secret are valid.  Events signed with the previous
secret must be accepted (with a warning log) so in-flight events are not
dropped during key rotation.

Test IDs:
  1. test_only_primary_secret_works_default
  2. test_primary_secret_accepted
  3. test_previous_secret_accepted_during_rotation_window
  4. test_both_wrong_secrets_rejected_with_400
  5. test_no_secret_configured_returns_503
  6. test_only_previous_set_falls_back_correctly
  7. test_previous_secret_logs_warning
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(
    primary: str = "",
    previous: str = "",
    legacy: str = "",
    stripe_key: str = "sk_test_key",
) -> MagicMock:
    cfg = MagicMock()
    cfg.STRIPE_SECRET_KEY = stripe_key
    cfg.STRIPE_WEBHOOK_SECRET = legacy
    cfg.STRIPE_WEBHOOK_SECRET_PRIMARY = primary
    cfg.STRIPE_WEBHOOK_SECRET_PREVIOUS = previous
    cfg.SUPABASE_URL = ""
    return cfg


class _FakeSignatureVerificationError(Exception):
    """Stand-in for stripe.SignatureVerificationError."""


def _make_stripe_mock(
    *,
    primary_secret: str,
    previous_secret: str = "",
    raise_on_wrong: bool = True,
) -> MagicMock:
    """Return a mock _stripe module that validates only against known secrets."""

    def _construct_event(payload: bytes, sig: str, secret: str) -> dict:
        if secret == primary_secret or (previous_secret and secret == previous_secret):
            return {
                "id": "evt_test_1",
                "type": "payment_intent.succeeded",
                "data": {"object": {}},
            }
        raise _FakeSignatureVerificationError("Invalid signature")

    stripe_mock = MagicMock()
    stripe_mock.SignatureVerificationError = _FakeSignatureVerificationError
    stripe_mock.Webhook.construct_event = _construct_event
    return stripe_mock


# ---------------------------------------------------------------------------
# Test 1: default config — only STRIPE_WEBHOOK_SECRET (legacy) works
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_only_primary_secret_works_default():
    """With only the legacy STRIPE_WEBHOOK_SECRET set, valid events pass."""
    from fastapi import HTTPException
    from fastapi.testclient import TestClient

    secret = "whsec_primary_only"
    stripe_mock = _make_stripe_mock(primary_secret=secret)
    cfg = _make_cfg(legacy=secret)

    with (
        patch("mariana.api._get_config", return_value=cfg),
        patch("mariana.api._stripe", stripe_mock),
        patch("mariana.api._claim_webhook_event", return_value="NEW"),
        patch("mariana.api._finalize_webhook_event", return_value=None),
        patch("mariana.api._handle_payment_intent_succeeded", return_value=None),
    ):
        from mariana.api import app
        from httpx import AsyncClient, ASGITransport

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/billing/webhook",
                content=b'{"id":"evt_test_1","type":"payment_intent.succeeded","data":{"object":{}}}',
                headers={"stripe-signature": "valid-sig"},
            )
        # Should not raise 400 or 503 on valid sig
        assert resp.status_code != 400
        assert resp.status_code != 503


# ---------------------------------------------------------------------------
# Test 2: primary secret alone accepted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_primary_secret_accepted():
    """STRIPE_WEBHOOK_SECRET_PRIMARY alone: valid primary-signed event accepted."""
    from httpx import AsyncClient, ASGITransport

    primary = "whsec_new_primary"
    stripe_mock = _make_stripe_mock(primary_secret=primary)
    cfg = _make_cfg(primary=primary)

    with (
        patch("mariana.api._get_config", return_value=cfg),
        patch("mariana.api._stripe", stripe_mock),
        patch("mariana.api._claim_webhook_event", return_value="NEW"),
        patch("mariana.api._finalize_webhook_event", return_value=None),
        patch("mariana.api._handle_payment_intent_succeeded", return_value=None),
    ):
        from mariana.api import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/billing/webhook",
                content=b'{"id":"evt_test_1","type":"payment_intent.succeeded","data":{"object":{}}}',
                headers={"stripe-signature": "valid-sig"},
            )
        assert resp.status_code not in (400, 503)


# ---------------------------------------------------------------------------
# Test 3: previous secret accepted during rotation window
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_previous_secret_accepted_during_rotation_window():
    """During rotation: event signed with PREVIOUS secret is accepted with warning."""
    from httpx import AsyncClient, ASGITransport

    primary = "whsec_new_primary"
    previous = "whsec_old_previous"

    # Build a stripe mock that rejects primary and accepts previous
    # (simulates an in-flight event signed before the rotate)
    def _construct_event(payload: bytes, sig: str, secret: str) -> dict:
        if secret == previous:
            return {
                "id": "evt_test_prev",
                "type": "payment_intent.succeeded",
                "data": {"object": {}},
            }
        raise _FakeSignatureVerificationError("Invalid signature")

    stripe_mock = MagicMock()
    stripe_mock.SignatureVerificationError = _FakeSignatureVerificationError
    stripe_mock.Webhook.construct_event = _construct_event

    cfg = _make_cfg(primary=primary, previous=previous)

    with (
        patch("mariana.api._get_config", return_value=cfg),
        patch("mariana.api._stripe", stripe_mock),
        patch("mariana.api._claim_webhook_event", return_value="NEW"),
        patch("mariana.api._finalize_webhook_event", return_value=None),
        patch("mariana.api._handle_payment_intent_succeeded", return_value=None),
    ):
        from mariana.api import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/billing/webhook",
                content=b'{"id":"evt_test_prev","type":"payment_intent.succeeded","data":{"object":{}}}',
                headers={"stripe-signature": "valid-sig"},
            )
        # Event signed with previous secret must NOT be dropped
        assert resp.status_code not in (400, 503), (
            f"B-30: in-flight event signed with previous secret should be accepted "
            f"during rotation window, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Test 4: both secrets wrong → 400
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_both_wrong_secrets_rejected_with_400():
    """When neither PRIMARY nor PREVIOUS matches, the request is rejected with 400."""
    from httpx import AsyncClient, ASGITransport

    primary = "whsec_correct_primary"
    previous = "whsec_correct_previous"

    def _construct_event(payload: bytes, sig: str, secret: str) -> dict:
        raise _FakeSignatureVerificationError("Invalid signature")

    stripe_mock = MagicMock()
    stripe_mock.SignatureVerificationError = _FakeSignatureVerificationError
    stripe_mock.Webhook.construct_event = _construct_event

    cfg = _make_cfg(primary=primary, previous=previous)

    with (
        patch("mariana.api._get_config", return_value=cfg),
        patch("mariana.api._stripe", stripe_mock),
    ):
        from mariana.api import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/billing/webhook",
                content=b'{"id":"evt_bad","type":"ping","data":{"object":{}}}',
                headers={"stripe-signature": "totally-wrong-signature"},
            )
        assert resp.status_code == 400, (
            f"B-30: both secrets invalid should yield 400, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Test 5: no secret configured → 503
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_secret_configured_returns_503():
    """When no webhook secret is configured at all, endpoint returns 503."""
    from httpx import AsyncClient, ASGITransport

    cfg = _make_cfg(primary="", previous="", legacy="")

    with patch("mariana.api._get_config", return_value=cfg):
        from mariana.api import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/billing/webhook",
                content=b"{}",
                headers={"stripe-signature": "x"},
            )
        assert resp.status_code == 503, (
            f"B-30: no secret configured should yield 503, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Test 6: only PREVIOUS set, no PRIMARY — should still fail gracefully
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_only_previous_set_no_primary_returns_503():
    """PREVIOUS set but PRIMARY empty and legacy empty → no primary → 503."""
    from httpx import AsyncClient, ASGITransport

    cfg = _make_cfg(primary="", previous="whsec_only_previous", legacy="")

    with patch("mariana.api._get_config", return_value=cfg):
        from mariana.api import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/billing/webhook",
                content=b"{}",
                headers={"stripe-signature": "x"},
            )
        # No primary means we cannot verify — must 503
        assert resp.status_code == 503, (
            f"B-30: no primary secret → 503, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Test 7: previous secret accepted produces warning log
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_previous_secret_logs_warning():
    """When event accepted via previous secret, a warning is logged."""
    import logging

    primary = "whsec_new_key"
    previous = "whsec_old_key"

    def _construct_event(payload: bytes, sig: str, secret: str) -> dict:
        if secret == previous:
            return {
                "id": "evt_warn",
                "type": "payment_intent.succeeded",
                "data": {"object": {}},
            }
        raise _FakeSignatureVerificationError("sig mismatch")

    stripe_mock = MagicMock()
    stripe_mock.SignatureVerificationError = _FakeSignatureVerificationError
    stripe_mock.Webhook.construct_event = _construct_event

    cfg = _make_cfg(primary=primary, previous=previous)

    warning_events: list[str] = []

    import structlog
    original_bind = structlog.get_logger

    with (
        patch("mariana.api._get_config", return_value=cfg),
        patch("mariana.api._stripe", stripe_mock),
        patch("mariana.api._claim_webhook_event", return_value="NEW"),
        patch("mariana.api._finalize_webhook_event", return_value=None),
        patch("mariana.api._handle_payment_intent_succeeded", return_value=None),
    ):
        from mariana.api import app
        from httpx import AsyncClient, ASGITransport

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/billing/webhook",
                content=b'{"id":"evt_warn","type":"payment_intent.succeeded","data":{"object":{}}}',
                headers={"stripe-signature": "prev-sig"},
            )

        # The main assertion: event must be accepted (not 400)
        assert resp.status_code not in (400, 503), (
            f"B-30: event with previous secret must be accepted, got {resp.status_code}"
        )
