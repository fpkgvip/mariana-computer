"""Z-02 regression: Stripe checkout redirect-host allowlist must include
the production frontend host.

Bug
---
Phase E re-audit #28 (A33) found that
``mariana/api.py:create_checkout`` validates ``success_url`` /
``cancel_url`` against ``_ALLOWED_REDIRECT_HOSTS`` which contains only
``frontend-tau-navy-80.vercel.app``, ``localhost``, ``127.0.0.1``.  The
production frontend ``app.mariana.computer`` (already in the CORS list at
``api.py:_DEFAULT_PROD_CORS_ORIGINS``) is missing — checkout requests
from production are rejected with HTTP 400 ``Invalid success_url: host
'app.mariana.computer' is not allowed``.

This test pins the fix:
  * production-frontend host accepted
  * an attacker-controlled host still rejected (security regression check)
  * localhost still works for dev
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from fastapi import HTTPException

from mariana import api as api_mod
from mariana.config import AppConfig


def _cfg() -> AppConfig:
    cfg = AppConfig.__new__(AppConfig)
    object.__setattr__(cfg, "SUPABASE_URL", "https://supabase.test")
    object.__setattr__(cfg, "SUPABASE_KEY", "anon")
    object.__setattr__(cfg, "SUPABASE_SERVICE_ROLE_KEY", "svc")
    object.__setattr__(cfg, "SUPABASE_ANON_KEY", "anon")
    object.__setattr__(cfg, "SUPABASE_SERVICE_KEY", "svc")
    object.__setattr__(cfg, "STRIPE_SECRET_KEY", "sk_test_z02")
    object.__setattr__(cfg, "STRIPE_PUBLISHABLE_KEY", "pk_test_z02")
    object.__setattr__(cfg, "STRIPE_WEBHOOK_SECRET", "whsec_z02")
    return cfg


def _user() -> dict:
    return {"user_id": "user-z02-test", "email": "z02@test.local"}


def _stripe_session_mock() -> MagicMock:
    session = MagicMock()
    session.id = "cs_test_z02"
    session.url = "https://checkout.stripe.com/c/pay/cs_test_z02"
    return session


# ---------------------------------------------------------------------------
# (1) Production-frontend host must be accepted.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_z02_production_frontend_host_accepted():
    """``success_url=https://app.mariana.computer/checkout/return`` must
    be accepted now that the production host is in the allowlist."""
    body = api_mod.CreateCheckoutRequest(
        plan_id="starter",
        success_url="https://app.mariana.computer/checkout/return",
        cancel_url="https://app.mariana.computer/pricing",
    )

    fake_session = _stripe_session_mock()
    with patch.object(api_mod, "_get_config", return_value=_cfg()), \
         patch.object(api_mod._stripe.checkout.Session, "create", return_value=fake_session):
        resp = await api_mod.create_checkout(body=body, current_user=_user())

    assert resp.checkout_url == fake_session.url
    assert resp.session_id == fake_session.id


# ---------------------------------------------------------------------------
# (2) Attacker host must still be rejected.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_z02_attacker_host_rejected():
    """Open-redirect protection (VULN-C2-03) must still hold."""
    body = api_mod.CreateCheckoutRequest(
        plan_id="starter",
        success_url="https://attacker.example.com/phish",
        cancel_url="https://app.mariana.computer/pricing",
    )

    with patch.object(api_mod, "_get_config", return_value=_cfg()):
        with pytest.raises(HTTPException) as exc_info:
            await api_mod.create_checkout(body=body, current_user=_user())
    assert exc_info.value.status_code == 400
    assert "success_url" in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# (3) Localhost must still work for development.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_z02_localhost_dev_host_accepted():
    """``localhost`` and ``127.0.0.1`` remain valid dev redirect hosts."""
    body = api_mod.CreateCheckoutRequest(
        plan_id="starter",
        success_url="http://localhost:5173/checkout/return",
        cancel_url="http://127.0.0.1:5173/pricing",
    )

    fake_session = _stripe_session_mock()
    with patch.object(api_mod, "_get_config", return_value=_cfg()), \
         patch.object(api_mod._stripe.checkout.Session, "create", return_value=fake_session):
        resp = await api_mod.create_checkout(body=body, current_user=_user())

    assert resp.session_id == fake_session.id


# ---------------------------------------------------------------------------
# (4) Pin the source-level guarantee that the allowlist includes the
#     production host so a future refactor cannot silently drop it.
# ---------------------------------------------------------------------------


def test_z02_allowlist_source_includes_production_host():
    import inspect

    src = inspect.getsource(api_mod.create_checkout)
    assert "app.mariana.computer" in src, (
        "create_checkout must include 'app.mariana.computer' in its "
        "redirect-host allowlist (or derive it from the trusted-frontend "
        "constant) so the production frontend can complete checkout"
    )
