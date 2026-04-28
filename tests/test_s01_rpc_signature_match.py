"""S-01 regression suite: RPC payload contract matches live PostgREST signatures.

Bug
---
R-01's ``_settle_agent_credits`` (mariana/agent/loop.py:558-610) sends a third
JSON key ``ref_id`` to ``/rest/v1/rpc/add_credits`` and
``/rest/v1/rpc/deduct_credits``.  The live functions only accept
``(p_user_id, p_credits)`` and ``(target_user_id, amount)`` respectively.
PostgREST rejects unknown JSON keys with PGRST202 / HTTP 404, so every
agent settlement silently drops both refunds and overrun-deducts on the
floor.  R-01 unit tests mocked httpx to return 200 unconditionally and
never exercised the actual payload shape.

Worse: the prior implementation set ``task.credits_settled = True`` BEFORE
checking the HTTP status, AND inserted the ``agent_settlements`` claim
row before the RPC fired — so on RPC failure the row stayed locked with
``completed_at IS NULL`` forever and any retry short-circuited via
``agent_settlement_already_claimed``.

Fix
---
1. Drop the ``ref_id`` JSON key from both POST bodies.
2. Look up an existing ``agent_settlements`` claim row before inserting
   one.  If the claim exists with ``completed_at IS NOT NULL`` →
   already settled, set ``credits_settled=True`` and return.  If the
   claim exists with ``completed_at IS NULL`` → retry the RPC without
   re-inserting.  Otherwise insert and proceed.
3. Set ``task.credits_settled = True`` only on RPC 2xx OR pre-completed
   claim.  On RPC failure, leave the in-memory flag False so the
   reconciler can retry through the same code path.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import uuid
from typing import Any
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Local PG availability gate.
# ---------------------------------------------------------------------------

PGHOST = os.environ.get("PGHOST", "/tmp")
PGPORT = int(os.environ.get("PGPORT", "55432"))
PGUSER = os.environ.get("PGUSER", "postgres")
PGDATABASE = os.environ.get("PGDATABASE", "testdb")

try:
    import asyncpg  # type: ignore  # noqa: F401
    import psycopg2  # type: ignore

    _conn = psycopg2.connect(
        host=PGHOST, port=PGPORT, user=PGUSER, dbname=PGDATABASE
    )
    _conn.close()
    _PG_AVAILABLE = True
except Exception:
    _PG_AVAILABLE = False

_pg_only = pytest.mark.skipif(not _PG_AVAILABLE, reason="Local PG not available")


_AGENT_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "mariana"
    / "agent"
    / "schema.sql"
)


async def _open_pool():
    import asyncpg as _asyncpg  # noqa: PLC0415

    return await _asyncpg.create_pool(
        host=PGHOST,
        port=PGPORT,
        user=PGUSER,
        database=PGDATABASE,
        min_size=1,
        max_size=4,
    )


async def _ensure_schema(pool: Any) -> None:
    sql = _AGENT_SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)


def _new_task(
    *,
    reserved: int = 500,
    settled: bool = False,
    spent_usd: float = 0.0,
    state=None,
):
    from mariana.agent.models import AgentState, AgentTask  # noqa: PLC0415

    task = AgentTask(
        id=str(uuid.uuid4()),
        user_id=f"user-s01-{uuid.uuid4().hex[:8]}",
        goal="S-01 RPC payload contract",
        budget_usd=5.0,
        spent_usd=spent_usd,
        state=state or AgentState.DONE,
    )
    task.reserved_credits = reserved
    task.credits_settled = settled
    return task


def _cfg():
    from mariana.config import AppConfig  # noqa: PLC0415

    cfg = AppConfig.__new__(AppConfig)
    object.__setattr__(cfg, "SUPABASE_URL", "https://supabase.test")
    object.__setattr__(cfg, "SUPABASE_KEY", "anon")
    object.__setattr__(cfg, "SUPABASE_SERVICE_ROLE_KEY", "service_role_xxx")
    object.__setattr__(cfg, "STRIPE_SECRET_KEY", "sk_test_xxx")
    object.__setattr__(cfg, "STRIPE_PUBLISHABLE_KEY", "pk_test_xxx")
    object.__setattr__(cfg, "STRIPE_WEBHOOK_SECRET", "whsec_xxx")
    return cfg


class _ScriptedClient:
    """httpx.AsyncClient stand-in that records every POST and lets each test
    pick a status code (default 200) plus optional body text."""

    def __init__(
        self,
        calls: list[dict[str, Any]] | None = None,
        status: int = 200,
        body: str = "{}",
    ):
        self.calls = calls if calls is not None else []
        self.status = status
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json})

        outer = self

        class _R:
            status_code = outer.status
            text = outer.body

            def json(self_inner):
                return True

        return _R()


# ---------------------------------------------------------------------------
# 1. add_credits payload must contain exactly {p_user_id, p_credits}.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_s01_add_credits_payload_no_ref_id():
    """Refund branch: the JSON body sent to /rpc/add_credits must include
    exactly ``p_user_id`` and ``p_credits`` — and NOTHING else.  The live
    function signature is ``add_credits(p_user_id uuid, p_credits integer)``;
    PostgREST rejects unknown JSON keys with PGRST202/HTTP 404."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        # spent < reserved → refund branch (delta < 0).
        task = _new_task(reserved=500, settled=False, spent_usd=0.30)
        await _insert_agent_task(pool, task)

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls, status=200)
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            await loop_mod._settle_agent_credits(task, db=pool)

        refunds = [c for c in rpc_calls if "rpc/add_credits" in c["url"]]
    finally:
        await pool.close()

    assert len(refunds) == 1, f"expected 1 add_credits POST, got {len(refunds)}"
    payload = refunds[0]["json"]
    assert set(payload.keys()) == {"p_user_id", "p_credits"}, (
        f"add_credits POST body must contain ONLY 'p_user_id' and 'p_credits'; "
        f"got keys: {sorted(payload.keys())} — PostgREST rejects unknown keys "
        f"with PGRST202/HTTP 404"
    )
    assert payload["p_user_id"] == task.user_id
    assert payload["p_credits"] == 470  # 500 reserved - 30 final = 470 refund


# ---------------------------------------------------------------------------
# 2. deduct_credits payload must contain exactly {target_user_id, amount}.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_s01_deduct_credits_payload_no_ref_id():
    """Overrun branch: live signature is
    ``deduct_credits(target_user_id uuid, amount integer)`` — the POST body
    must have those two keys only, no ``ref_id``."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        # spent > reserved → overrun branch (delta > 0).
        task = _new_task(reserved=100, settled=False, spent_usd=2.50)
        await _insert_agent_task(pool, task)

        rpc_calls: list[dict[str, Any]] = []
        client = _ScriptedClient(calls=rpc_calls, status=200)
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            await loop_mod._settle_agent_credits(task, db=pool)

        deducts = [c for c in rpc_calls if "rpc/deduct_credits" in c["url"]]
    finally:
        await pool.close()

    assert len(deducts) == 1, f"expected 1 deduct_credits POST, got {len(deducts)}"
    payload = deducts[0]["json"]
    assert set(payload.keys()) == {"target_user_id", "amount"}, (
        f"deduct_credits POST body must contain ONLY 'target_user_id' and "
        f"'amount'; got keys: {sorted(payload.keys())} — PostgREST rejects "
        f"unknown keys with PGRST202/HTTP 404"
    )
    assert payload["target_user_id"] == task.user_id
    assert payload["amount"] == 150  # spent 250 - reserved 100 = 150 overrun


# ---------------------------------------------------------------------------
# 3. RPC 404 must leave the claim row uncompleted AND in-memory flag False.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_s01_rpc_404_marks_claim_uncompleted():
    """When the RPC returns 404 (the live PGRST202 case), the
    agent_settlements row must remain with ``completed_at IS NULL`` AND
    ``task.credits_settled`` must NOT be flipped to True — otherwise the
    reconciler cannot retry.

    This encodes the corrected contract: the claim row + in-memory flag
    together represent "in flight"; only RPC success transitions both to
    "settled"."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=500, settled=False, spent_usd=0.30)
        await _insert_agent_task(pool, task)

        client = _ScriptedClient(
            status=404,
            body='{"code":"PGRST202","message":"function not found"}',
        )
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            await loop_mod._settle_agent_credits(task, db=pool)

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT completed_at, delta_credits FROM agent_settlements "
                "WHERE task_id = $1",
                task.id,
            )
    finally:
        await pool.close()

    assert row is not None, "claim row must exist even though RPC failed"
    assert row["completed_at"] is None, (
        "completed_at must remain NULL on RPC 404 — the reconciler relies "
        "on this to identify rows needing retry"
    )
    assert task.credits_settled is False, (
        "task.credits_settled MUST stay False on RPC failure so the next "
        "settlement attempt does not short-circuit on the in-memory flag — "
        "S-01 root cause was that the prior code flipped it to True before "
        "checking the HTTP status, permanently stranding the claim"
    )


# ---------------------------------------------------------------------------
# 4. After RPC failure, a second _settle_agent_credits call must RETRY
#    (re-issue the RPC), not short-circuit on the existing claim row.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_s01_rpc_failure_does_not_short_circuit_retry():
    """Two sequential settle calls: first RPC fails (404), second RPC
    succeeds (200).  Expectation:
      * 2 RPC POSTs total (the second is the retry).
      * Final claim row has completed_at NOT NULL.
      * Final task.credits_settled is True.
    Pre-fix: the existing claim row caused the second call to log
    ``agent_settlement_already_claimed`` and return without retrying."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=500, settled=False, spent_usd=0.30)
        await _insert_agent_task(pool, task)

        # First attempt: RPC fails.
        rpc_calls: list[dict[str, Any]] = []
        fail_client = _ScriptedClient(calls=rpc_calls, status=404)
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=fail_client):
            await loop_mod._settle_agent_credits(task, db=pool)

        first_pass = list(rpc_calls)
        assert len(first_pass) == 1, "first settle should issue one RPC"
        assert task.credits_settled is False, (
            "credits_settled must remain False after RPC failure (else retry "
            "is impossible)"
        )

        # Second attempt: same task, same in-memory state, RPC succeeds.
        ok_client = _ScriptedClient(calls=rpc_calls, status=200)
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=ok_client):
            await loop_mod._settle_agent_credits(task, db=pool)

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT completed_at FROM agent_settlements WHERE task_id = $1",
                task.id,
            )
    finally:
        await pool.close()

    assert len(rpc_calls) == 2, (
        f"retry must re-issue the RPC; got {len(rpc_calls)} total POSTs — "
        f"the existing uncompleted claim row should be the trigger to retry, "
        f"not to short-circuit"
    )
    assert row is not None and row["completed_at"] is not None, (
        "completed_at must be stamped after the retry succeeds"
    )
    assert task.credits_settled is True


# ---------------------------------------------------------------------------
# 5. Happy path: RPC 200 → completed_at set, task.credits_settled = True.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_s01_rpc_success_sets_completed_at():
    """Happy path: claim row inserted, RPC 200, completed_at populated,
    in-memory flag flipped True."""
    import httpx  # noqa: PLC0415

    from mariana import api as api_mod  # noqa: PLC0415
    from mariana.agent import loop as loop_mod  # noqa: PLC0415
    from mariana.agent.api_routes import _insert_agent_task  # noqa: PLC0415

    pool = await _open_pool()
    try:
        await _ensure_schema(pool)
        task = _new_task(reserved=500, settled=False, spent_usd=0.30)
        await _insert_agent_task(pool, task)

        client = _ScriptedClient(status=200)
        with patch.object(api_mod, "_get_config", lambda: _cfg()), \
             patch.object(api_mod, "_supabase_api_key", lambda c: "service_role_xxx"), \
             patch.object(httpx, "AsyncClient", return_value=client):
            await loop_mod._settle_agent_credits(task, db=pool)

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT completed_at, reserved_credits, final_credits, "
                "delta_credits, ref_id FROM agent_settlements WHERE task_id = $1",
                task.id,
            )
    finally:
        await pool.close()

    assert row is not None
    assert row["completed_at"] is not None
    assert row["reserved_credits"] == 500
    assert row["final_credits"] == 30
    assert row["delta_credits"] == -470
    assert row["ref_id"] == f"agent_settle:{task.id}"
    assert task.credits_settled is True
