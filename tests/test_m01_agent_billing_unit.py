"""M-01 regression suite: agent task credit reservation + settlement.

Bug fixed:
- POST /api/agent reserved `max(200, int(body.budget_usd * 500))` credits up
  front (500 credits/USD), but the agent runtime treats `budget_usd` /
  `spent_usd` as raw dollars and halts at `spent_usd >= budget_usd`. Frontend
  canonical conversion is `1 credit == $0.01` (100 credits/USD), and the
  Pricing page literally says "1c = $0.01". A $5 task therefore reserved 2500
  credits while the runtime/UI ceiling was 500 credits — 5x over-collection.
- After successful enqueue there was no settlement path; only a narrow
  pre-enqueue insert-failure refund existed.

Fix:
- Reservation formula corrected to canonical 100 credits/USD:
  ``reserved_credits = max(100, int(budget_usd * 100))``
- ``AgentTask`` gets ``reserved_credits`` and ``credits_settled`` fields so
  the loop can settle on its own.
- ``mariana/agent/loop.py:_settle_agent_credits`` runs in the ``run_task``
  ``finally`` block on terminal states (DONE / FAILED / HALTED), refunding
  unused reserved credits via ``add_credits`` or deducting overage via
  ``deduct_credits``. Idempotent via ``credits_settled``.

Tests in this file mirror the style of ``tests/test_l01_mapping_insert_failure.py``:
mocked httpx, FastAPI route closure invoked directly with stubbed
dependencies. No live Postgres / Redis required.

Test inventory (6):
  1. test_reserve_canonical_100_per_usd          — $5 reserves 500 (not 2500)
  2. test_reserve_floor_at_100                    — $0.10 reserves 100 (floor)
  3. test_settlement_refunds_unused_on_done       — reserved=500, spent=$0.30 -> refund 470
  4. test_settlement_refunds_full_on_failed       — reserved=500, spent=$0   -> refund 500
  5. test_settlement_extra_deduct_on_overrun      — reserved=500, spent=$5.40 -> deduct 40
  6. test_settlement_idempotent                   — second call is a noop
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from mariana import api as api_mod
from mariana.agent.models import AgentState, AgentTask
from mariana.config import AppConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg() -> AppConfig:
    cfg = AppConfig.__new__(AppConfig)
    object.__setattr__(cfg, "SUPABASE_URL", "https://supabase.test")
    object.__setattr__(cfg, "SUPABASE_KEY", "anon")
    object.__setattr__(cfg, "SUPABASE_ANON_KEY", "anon_xxx")
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
    """httpx.AsyncClient stand-in keyed by URL substring."""

    def __init__(
        self,
        *,
        by_path: dict[str, list[_FakeResp]] | None = None,
        default_response: _FakeResp | None = None,
    ) -> None:
        self.queues: dict[str, list[_FakeResp]] = by_path or {}
        self.default_response = default_response or _FakeResp(200, True)
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


async def _start_route(get_current_user_user_id: str = "user-m01"):
    """Build the start_agent_task route handler closure with stubbed deps.

    Returns the bound endpoint function (call it with body=AgentStartRequest(...)).
    """
    from mariana.agent.api_routes import make_routes

    router = make_routes(
        get_current_user=AsyncMock(return_value={"user_id": get_current_user_user_id}),
        get_db=MagicMock(return_value=MagicMock()),
        get_redis=MagicMock(return_value=None),
        get_stream_user=AsyncMock(return_value={"user_id": get_current_user_user_id}),
    )
    for route in router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == "/api/agent"
            and route.methods == {"POST"}
        ):
            return route.endpoint
    raise AssertionError("could not locate POST /api/agent route")


# ---------------------------------------------------------------------------
# 1. Reservation formula — canonical 100 credits/USD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reserve_canonical_100_per_usd():
    """A $5 budget must reserve 500 credits (NOT 2500 = the buggy 500/USD)."""
    from mariana.agent.api_routes import AgentStartRequest

    deduct_mock = AsyncMock(return_value="ok")
    insert_mock = AsyncMock()

    with patch.object(api_mod, "_supabase_deduct_credits", deduct_mock), \
         patch.object(api_mod, "_supabase_add_credits", AsyncMock()), \
         patch.object(api_mod, "_get_config", lambda: _cfg()), \
         patch("mariana.agent.api_routes._insert_agent_task", insert_mock), \
         patch("mariana.agent.api_routes._enqueue_agent_task", AsyncMock()):

        endpoint = await _start_route()
        resp = await endpoint(
            body=AgentStartRequest(goal="hello", budget_usd=5.0),
            current_user={"user_id": "user-m01"},
        )

    assert resp is not None
    assert deduct_mock.await_count == 1
    user_id_arg, credits_arg, _cfg_arg = deduct_mock.await_args.args
    assert user_id_arg == "user-m01"
    assert credits_arg == 500, (
        f"expected 500 credits for $5 budget under canonical 100c/USD; got {credits_arg}"
    )

    # And the $1.00 floor budget reserves exactly 100 credits (canonical
    # floor enforced by both Pydantic ge=1.0 and the backend max(100, ...)).
    deduct_mock.reset_mock()
    with patch.object(api_mod, "_supabase_deduct_credits", deduct_mock), \
         patch.object(api_mod, "_supabase_add_credits", AsyncMock()), \
         patch.object(api_mod, "_get_config", lambda: _cfg()), \
         patch("mariana.agent.api_routes._insert_agent_task", insert_mock), \
         patch("mariana.agent.api_routes._enqueue_agent_task", AsyncMock()):

        endpoint = await _start_route()
        await endpoint(
            body=AgentStartRequest(goal="cheap task", budget_usd=1.0),
            current_user={"user_id": "user-m01"},
        )

    _, credits_arg2, _ = deduct_mock.await_args.args
    assert credits_arg2 == 100, f"expected floor of 100 for $1.00 budget; got {credits_arg2}"


@pytest.mark.asyncio
async def test_reserve_floor_at_100():
    """A $1.00 budget (the floor enforced by Pydantic ge=1.0) reserves 100."""
    from mariana.agent.api_routes import AgentStartRequest

    deduct_mock = AsyncMock(return_value="ok")

    with patch.object(api_mod, "_supabase_deduct_credits", deduct_mock), \
         patch.object(api_mod, "_supabase_add_credits", AsyncMock()), \
         patch.object(api_mod, "_get_config", lambda: _cfg()), \
         patch("mariana.agent.api_routes._insert_agent_task", AsyncMock()), \
         patch("mariana.agent.api_routes._enqueue_agent_task", AsyncMock()):

        endpoint = await _start_route()
        await endpoint(
            body=AgentStartRequest(goal="tiny", budget_usd=1.0),
            current_user={"user_id": "user-m01"},
        )

    _, credits_arg, _ = deduct_mock.await_args.args
    assert credits_arg == 100, f"expected floor of 100; got {credits_arg}"


# ---------------------------------------------------------------------------
# 2. Settlement helper — must exist and reconcile reserved vs actual spend.
# ---------------------------------------------------------------------------


def _make_task(
    *,
    reserved: int,
    spent_usd: float,
    state: AgentState = AgentState.DONE,
    settled: bool = False,
) -> AgentTask:
    task = AgentTask(
        id="00000000-0000-0000-0000-00000000m01a",
        user_id="user-m01",
        goal="settle me",
        budget_usd=5.0,
        spent_usd=spent_usd,
        state=state,
    )
    # New fields added by the M-01 fix.
    task.reserved_credits = reserved  # type: ignore[attr-defined]
    task.credits_settled = settled  # type: ignore[attr-defined]
    return task


@pytest.mark.asyncio
async def test_settlement_refunds_unused_on_done():
    """reserved=500 credits, spent=$0.30 (=30 credits) must refund 470 via add_credits."""
    from mariana.agent.loop import _settle_agent_credits

    task = _make_task(reserved=500, spent_usd=0.30, state=AgentState.DONE)
    add_url = "rpc/grant_credits"
    deduct_url = "rpc/refund_credits"
    client = _ScriptedClient(
        by_path={
            add_url: [_FakeResp(200, True)],
            deduct_url: [_FakeResp(200, True)],
        },
        default_response=_FakeResp(200, True),
    )

    with patch.object(api_mod, "_get_config", lambda: _cfg()), \
         patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
         patch.object(httpx, "AsyncClient", return_value=client):
        await _settle_agent_credits(task)

    refund_calls = [c for c in client.calls if add_url in c["url"]]
    deduct_calls = [c for c in client.calls if deduct_url in c["url"]]
    assert len(refund_calls) == 1, "expected exactly one add_credits refund call"
    assert len(deduct_calls) == 0, "must not call deduct_credits when refunding"
    body = refund_calls[0]["json"]
    refund_amount = body.get("p_credits") or body.get("credits") or body.get("amount")
    assert refund_amount == 470, f"expected refund of 470; got {refund_amount}"
    assert task.credits_settled is True


@pytest.mark.asyncio
async def test_settlement_refunds_full_on_failed():
    """Terminal=FAILED, spent=$0 -> refund 500 (the entire reservation)."""
    from mariana.agent.loop import _settle_agent_credits

    task = _make_task(reserved=500, spent_usd=0.0, state=AgentState.FAILED)
    client = _ScriptedClient(
        by_path={
            "rpc/grant_credits": [_FakeResp(200, True)],
            "rpc/refund_credits": [_FakeResp(200, True)],
        },
    )

    with patch.object(api_mod, "_get_config", lambda: _cfg()), \
         patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
         patch.object(httpx, "AsyncClient", return_value=client):
        await _settle_agent_credits(task)

    refund_calls = [c for c in client.calls if "rpc/grant_credits" in c["url"]]
    assert len(refund_calls) == 1
    body = refund_calls[0]["json"]
    refund_amount = body.get("p_credits") or body.get("credits") or body.get("amount")
    assert refund_amount == 500, f"expected full refund of 500; got {refund_amount}"
    assert task.credits_settled is True


@pytest.mark.asyncio
async def test_settlement_extra_deduct_on_overrun():
    """At-budget = noop. Over-budget by 40 credits ($5.40 with reserved=500) -> deduct 40."""
    from mariana.agent.loop import _settle_agent_credits

    # Case A: exactly at budget — no calls.
    task_eq = _make_task(reserved=500, spent_usd=5.0, state=AgentState.DONE)
    client_eq = _ScriptedClient(
        by_path={
            "rpc/grant_credits": [_FakeResp(200, True)],
            "rpc/refund_credits": [_FakeResp(200, True)],
        },
    )
    with patch.object(api_mod, "_get_config", lambda: _cfg()), \
         patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
         patch.object(httpx, "AsyncClient", return_value=client_eq):
        await _settle_agent_credits(task_eq)
    assert all("rpc/" not in c["url"] for c in client_eq.calls), (
        "noop case must not call any credit RPC"
    )
    assert task_eq.credits_settled is True

    # Case B: $5.40 spent on $5 reservation -> deduct extra 40.
    task_over = _make_task(reserved=500, spent_usd=5.4, state=AgentState.DONE)
    client_over = _ScriptedClient(
        by_path={
            "rpc/grant_credits": [_FakeResp(200, True)],
            "rpc/refund_credits": [_FakeResp(200, True)],
        },
    )
    with patch.object(api_mod, "_get_config", lambda: _cfg()), \
         patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
         patch.object(httpx, "AsyncClient", return_value=client_over):
        await _settle_agent_credits(task_over)

    deduct_calls = [c for c in client_over.calls if "rpc/refund_credits" in c["url"]]
    refund_calls = [c for c in client_over.calls if "rpc/grant_credits" in c["url"]]
    assert len(deduct_calls) == 1, "expected one deduct_credits call for overrun"
    assert len(refund_calls) == 0, "must not refund when overrun"
    body = deduct_calls[0]["json"]
    extra = body.get("amount") or body.get("p_credits") or body.get("credits")
    assert extra == 40, f"expected extra deduct of 40; got {extra}"
    assert task_over.credits_settled is True


@pytest.mark.asyncio
async def test_settlement_idempotent():
    """Calling _settle_agent_credits twice on the same task must only settle once."""
    from mariana.agent.loop import _settle_agent_credits

    task = _make_task(reserved=500, spent_usd=0.30, state=AgentState.DONE)
    client = _ScriptedClient(
        by_path={
            "rpc/grant_credits": [_FakeResp(200, True)],
            "rpc/refund_credits": [_FakeResp(200, True)],
        },
    )
    with patch.object(api_mod, "_get_config", lambda: _cfg()), \
         patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
         patch.object(httpx, "AsyncClient", return_value=client):
        await _settle_agent_credits(task)
        # Second call should be a no-op.
        await _settle_agent_credits(task)

    refund_calls = [c for c in client.calls if "rpc/grant_credits" in c["url"]]
    assert len(refund_calls) == 1, (
        "second settlement must be a noop — credits_settled flag enforces idempotency"
    )
    assert task.credits_settled is True
