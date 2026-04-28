"""U-02 regression suite: Decimal-quantized USD-to-credits conversion.

Bug fixed (U-02 P3, A25 Probe 2):
- ``AgentTask.spent_usd`` is a Python ``float``.  Settlement converts via
  ``final_tokens = int(task.spent_usd * 100)`` (``mariana/agent/loop.py:523``)
  and the legacy investigation path does ``int(total_with_markup * 100)``
  (``mariana/main.py:425``).  ``int(x * 100)`` truncates toward zero, so
  ``$0.305`` becomes ``30`` credits instead of the cent-quantized ``31``,
  and IEEE-754 float drift (``0.1 + 0.2 == 0.30000000000000004``) makes
  the under/overcharge unpredictable across a long task.

Fix:
- Introduce ``mariana.billing.precision.usd_to_credits`` which converts
  a USD amount (Decimal | float | str | int) to integer credits via
  ``Decimal`` quantization with ``ROUND_HALF_UP`` at the cent boundary.
  ``str(usd)`` is used to bypass float repr surprises when the input is
  a Python float.
- Replace the two billing-relevant ``int(x * 100)`` callsites
  (``mariana/agent/loop.py:_settle_agent_credits`` and
  ``mariana/main.py:_deduct_user_credits``) with the helper.

Rounding mode: ROUND_HALF_UP.
Rationale: predictable, simple, fair to platform on .x5 boundaries
(values like 0.305 round to 31 cents).  Documented in
``loop6_audit/U02_FIX_REPORT.md``.

Test inventory (5):
  1. test_30_5_cents_rounds_up                 — ROUND_HALF_UP at cent boundary
  2. test_no_float_drift_accumulation          — Decimal accumulation has no IEEE drift
  3. test_settlement_uses_quantized_amount     — _settle_agent_credits respects ROUND_HALF_UP
  4. test_legacy_investigation_quantize        — main.py:_deduct_user_credits respects ROUND_HALF_UP
  5. test_helper_accepts_decimal_float_str_int — input type tolerance for incremental migration
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from mariana import api as api_mod
from mariana.agent.models import AgentState, AgentTask
from mariana.config import AppConfig


# ---------------------------------------------------------------------------
# Helpers (mirrored from tests/test_m01_agent_billing_unit.py)
# ---------------------------------------------------------------------------


def _cfg() -> AppConfig:
    cfg = AppConfig.__new__(AppConfig)
    object.__setattr__(cfg, "SUPABASE_URL", "https://supabase.test")
    object.__setattr__(cfg, "SUPABASE_KEY", "anon")
    object.__setattr__(cfg, "SUPABASE_ANON_KEY", "anon_xxx")
    object.__setattr__(cfg, "SUPABASE_SERVICE_ROLE_KEY", "service_role_xxx")
    object.__setattr__(cfg, "SUPABASE_SERVICE_KEY", "service_role_xxx")
    object.__setattr__(cfg, "STRIPE_SECRET_KEY", "sk_test_xxx")
    object.__setattr__(cfg, "STRIPE_PUBLISHABLE_KEY", "pk_test_xxx")
    object.__setattr__(cfg, "STRIPE_WEBHOOK_SECRET", "whsec_xxx")
    return cfg


class _FakeResp:
    def __init__(self, status_code: int = 200, body: Any = None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}
        import json as _json

        self.text = (
            _json.dumps(self._body) if not isinstance(self._body, str) else self._body
        )

    def json(self) -> Any:
        return self._body


class _ScriptedClient:
    """Minimal httpx.AsyncClient stand-in keyed by URL substring."""

    def __init__(self, *, by_path: dict[str, list[_FakeResp]] | None = None) -> None:
        self.queues: dict[str, list[_FakeResp]] = by_path or {}
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def _next(self, url: str) -> _FakeResp:
        for path, queue in self.queues.items():
            if path in url and queue:
                if len(queue) == 1:
                    return queue[0]
                return queue.pop(0)
        return _FakeResp(200, True)

    async def post(self, url: str, json=None, headers=None):
        self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers})
        return self._next(url)


def _make_task(*, reserved: int, spent_usd: float, settled: bool = False) -> AgentTask:
    task = AgentTask(
        id="00000000-0000-0000-0000-00000000u02a",
        user_id="user-u02",
        goal="settle me",
        budget_usd=5.0,
        spent_usd=spent_usd,
        state=AgentState.DONE,
    )
    task.reserved_credits = reserved  # type: ignore[attr-defined]
    task.credits_settled = settled  # type: ignore[attr-defined]
    return task


# ---------------------------------------------------------------------------
# 1. usd_to_credits: ROUND_HALF_UP at the cent boundary
# ---------------------------------------------------------------------------


def test_30_5_cents_rounds_up():
    """0.305 USD must convert to 31 credits, not 30 (the truncating bug)."""
    from mariana.billing.precision import usd_to_credits

    # The canonical bug case from A25 Probe 2.
    assert usd_to_credits(Decimal("0.305")) == 31, (
        "ROUND_HALF_UP must convert $0.305 to 31 credits, not 30."
    )
    # Symmetric value just above a cent — must round up like normal.
    assert usd_to_credits(Decimal("0.306")) == 31
    # Just below — must round down.
    assert usd_to_credits(Decimal("0.304")) == 30
    # Whole cents — exact.
    assert usd_to_credits(Decimal("0.30")) == 30
    assert usd_to_credits(Decimal("0.31")) == 31
    # Zero is zero, never negative.
    assert usd_to_credits(Decimal("0")) == 0


# ---------------------------------------------------------------------------
# 2. Decimal accumulation: no float drift (0.1 + 0.2 + ... bug)
# ---------------------------------------------------------------------------


def test_no_float_drift_accumulation():
    """Accumulating 100 × Decimal('0.01') must yield exactly Decimal('1.00')
    and 100 credits, vs the float-drift path which does NOT.
    """
    # Decimal accumulator — fixed-point exact.
    total = Decimal("0")
    for _ in range(100):
        total += Decimal("0.01")
    assert total == Decimal("1.00"), f"Decimal accumulation must be exact; got {total}"

    from mariana.billing.precision import usd_to_credits

    assert usd_to_credits(total) == 100

    # Demonstrate the IEEE-754 drift the old float-only path was vulnerable
    # to: 100 × 0.01 in float arithmetic does NOT equal 1.0 exactly.  This
    # test pins the drift so future "let's just stay on float" regressions
    # are noticed.
    float_total = 0.0
    for _ in range(100):
        float_total += 0.01
    # The naive int(float_total * 100) is a flaky truncation — we don't
    # assert its value, only that Decimal yields exactly 100 regardless.
    assert float_total != 1.0, (
        "Sanity check: float accumulation MUST drift; the Decimal path "
        "above is what fixes the bug."
    )


# ---------------------------------------------------------------------------
# 3. Settlement (agent loop) — uses quantized amount, not truncation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settlement_uses_quantized_amount():
    """task.spent_usd = 0.305 must settle as 31 credits, not 30.

    With reserved=1 and final=31, delta=+30 → refund_credits RPC fires
    with p_credits=30 (the overrun).  The buggy path would compute
    final=int(0.305*100)=30 and fire with p_credits=29.  We assert the
    cent-level difference here so the regression is precisely pinned.
    """
    from mariana.agent.loop import _settle_agent_credits

    task = _make_task(reserved=1, spent_usd=0.305)
    client = _ScriptedClient(
        by_path={
            "rpc/grant_credits": [_FakeResp(200, True)],
            "rpc/refund_credits": [_FakeResp(200, True)],
        }
    )

    with patch.object(api_mod, "_get_config", lambda: _cfg()), \
         patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
         patch.object(httpx, "AsyncClient", return_value=client):
        await _settle_agent_credits(task)

    overrun_calls = [c for c in client.calls if "rpc/refund_credits" in c["url"]]
    assert len(overrun_calls) == 1, (
        f"expected one refund_credits (overrun) call; got {len(overrun_calls)}"
    )
    body = overrun_calls[0]["json"]
    p_credits = body.get("p_credits")
    # quantized: final = 31, delta = 31 - 1 = 30.
    # buggy:     final = int(0.305*100) = 30, delta = 30 - 1 = 29.
    assert p_credits == 30, (
        f"settlement must quantize $0.305 to 31 credits via ROUND_HALF_UP "
        f"so the overrun against reserved=1 is 30; got p_credits={p_credits}. "
        f"The pre-fix int() truncation produces 29."
    )


# ---------------------------------------------------------------------------
# 4. Legacy investigation settlement — main.py:_deduct_user_credits.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_investigation_quantize():
    """Legacy ``_deduct_user_credits`` must quantize total_with_markup the
    same way: $0.305 markup -> 31 credits, not 30.
    """
    from mariana import main as main_mod

    class _FakeTracker:
        # ``total_with_markup`` is a property in production; supply it directly.
        total_with_markup = 0.305
        total_spent = 0.305 / 1.20

    # Y-01: legacy investigation settlement now routes through the
    # idempotent ``refund_credits`` (overrun) / ``grant_credits``
    # (refund) RPCs instead of the unkeyed ``add_credits`` /
    # ``deduct_credits``.  This test still asserts the U-02 quantization
    # contract — the rounding mode and helper are unchanged — but on
    # the new RPC surface.
    client = _ScriptedClient(
        by_path={
            "rpc/refund_credits": [_FakeResp(200, True)],
            "rpc/grant_credits": [_FakeResp(200, True)],
        }
    )

    with patch.object(httpx, "AsyncClient", return_value=client):
        await main_mod._deduct_user_credits(
            user_id="user-u02-legacy",
            cost_tracker=_FakeTracker(),
            config=_cfg(),
            reserved_credits=0,
        )

    # delta_tokens = 31 - 0 = +31 → overrun path → refund_credits.
    overrun_calls = [c for c in client.calls if "rpc/refund_credits" in c["url"]]
    assert len(overrun_calls) == 1, (
        f"expected one refund_credits call; got {len(overrun_calls)}"
    )
    body = overrun_calls[0]["json"]
    amount = body.get("p_credits")
    assert amount == 31, (
        f"legacy settlement must quantize $0.305 to 31 credits via "
        f"ROUND_HALF_UP; got p_credits={amount}.  The pre-fix int() "
        f"truncation produces 30."
    )


# ---------------------------------------------------------------------------
# 5. Helper input tolerance — Decimal | float | str | int.
# ---------------------------------------------------------------------------


def test_helper_accepts_decimal_float_str_int():
    """The helper must accept any sane USD representation so callers can
    migrate to Decimal incrementally without converting at every site.
    """
    from mariana.billing.precision import usd_to_credits

    # Decimal: the canonical input.
    assert usd_to_credits(Decimal("1.234")) == 123  # 123.4 → ROUND_HALF_UP → 123

    # float: must use str(x) under the hood to avoid IEEE-754 surprises.
    assert usd_to_credits(0.305) == 31, (
        "float 0.305 must round to 31 (the helper uses str(x) to bypass "
        "the 0.30499999... float repr)."
    )

    # str: explicit decimal string.
    assert usd_to_credits("0.305") == 31

    # int: trivial.
    assert usd_to_credits(2) == 200
    assert usd_to_credits(0) == 0
