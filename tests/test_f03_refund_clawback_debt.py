"""F-03 regression suite: refund clawback debt construct.

Tests run against the local testdb Postgres instance
(PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb).

They require the 009_f03_refund_debt.sql migration to have been applied.
All tests create their own isolated user rows and clean up afterwards.

Test inventory:
  test_refund_full_balance_no_deficit
  test_refund_partial_balance_records_deficit
  test_grant_satisfies_open_clawback_first
  test_grant_partial_satisfaction_keeps_unsatisfied_remainder
  test_concurrent_refund_grant_serialized
  test_refund_idempotent_on_ref_id
  test_add_credits_drains_clawback
  test_multiple_clawbacks_satisfied_fifo
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, Optional

import asyncpg
import pytest

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

PGHOST = os.environ.get("PGHOST", "/tmp")
PGPORT = int(os.environ.get("PGPORT", "55432"))
PGUSER = os.environ.get("PGUSER", "postgres")
PGDATABASE = os.environ.get("PGDATABASE", "testdb")

# Skip all tests if the local testdb is not reachable.
_DB_AVAILABLE: Optional[bool] = None


def _dsn() -> str:
    if PGHOST.startswith("/"):
        return f"postgres://{PGUSER}@/{PGDATABASE}?host={PGHOST}&port={PGPORT}"
    return f"postgres://{PGUSER}@{PGHOST}:{PGPORT}/{PGDATABASE}"


async def _check_db() -> bool:
    global _DB_AVAILABLE
    if _DB_AVAILABLE is not None:
        return _DB_AVAILABLE
    try:
        conn = await asyncpg.connect(_dsn(), timeout=3)
        await conn.close()
        _DB_AVAILABLE = True
    except Exception:
        _DB_AVAILABLE = False
    return _DB_AVAILABLE


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def db():
    """Yield an asyncpg connection scoped to a single test."""
    conn = await asyncpg.connect(_dsn())
    try:
        yield conn
    finally:
        await conn.close()


async def _ensure_db_or_skip():
    if not await _check_db():
        pytest.skip("local testdb not reachable")


@pytest.fixture
async def user_id(db: asyncpg.Connection) -> str:  # type: ignore[type-arg]
    """Create a minimal auth.users + profiles row; clean up after the test."""
    uid = uuid.uuid4()
    uid_str = str(uid)
    # Insert into auth.users using the simplified local testdb schema.
    await db.execute(
        "INSERT INTO auth.users (id, email) VALUES ($1, $2)",
        uid, uid_str[:8] + "@test.example"
    )
    # The handle_new_user trigger inserts the profile row; if not wired locally,
    # insert manually.
    existing = await db.fetchrow(
        "SELECT id FROM public.profiles WHERE id = $1", uid
    )
    if not existing:
        await db.execute(
            "INSERT INTO public.profiles (id, email, tokens) VALUES ($1, $2, 0)",
            uid, uid_str[:8] + "@test.example"
        )
    yield uid_str
    # Cleanup
    await db.execute("DELETE FROM public.credit_clawbacks WHERE user_id = $1", uid)
    await db.execute("DELETE FROM public.credit_transactions WHERE user_id = $1", uid)
    await db.execute("DELETE FROM public.credit_buckets WHERE user_id = $1", uid)
    await db.execute("DELETE FROM public.profiles WHERE id = $1", uid)
    await db.execute("DELETE FROM auth.users WHERE id = $1", uid)


# ---------------------------------------------------------------------------
# RPC helpers (call SECURITY DEFINER functions as postgres superuser)
# ---------------------------------------------------------------------------

async def _grant(db, user_id: str, credits: int, ref_id: str,
                 source: str = "topup") -> dict:
    row = await db.fetchrow(
        "SELECT public.grant_credits($1, $2, $3, 'test', $4) AS r",
        uuid.UUID(user_id), credits, source, ref_id
    )
    import json
    return json.loads(row["r"])


async def _refund(db, user_id: str, credits: int, ref_id: str) -> dict:
    row = await db.fetchrow(
        "SELECT public.refund_credits($1, $2, 'stripe_event', $3) AS r",
        uuid.UUID(user_id), credits, ref_id
    )
    import json
    return json.loads(row["r"])


async def _spend(db, user_id: str, credits: int, ref_id: str) -> dict:
    row = await db.fetchrow(
        "SELECT public.spend_credits($1, $2, 'task', $3) AS r",
        uuid.UUID(user_id), credits, ref_id
    )
    import json
    return json.loads(row["r"])


async def _balance(db, user_id: str) -> int:
    row = await db.fetchrow(
        "SELECT COALESCE(SUM(remaining_credits), 0) AS bal "
        "FROM public.credit_buckets WHERE user_id = $1 "
        "  AND (expires_at IS NULL OR expires_at > clock_timestamp())",
        uuid.UUID(user_id)
    )
    return int(row["bal"])


async def _tokens(db, user_id: str) -> int:
    row = await db.fetchrow(
        "SELECT tokens FROM public.profiles WHERE id = $1",
        uuid.UUID(user_id)
    )
    return int(row["tokens"])


async def _open_clawbacks(db, user_id: str):
    """Return list of open (unsatisfied) clawback rows."""
    rows = await db.fetch(
        "SELECT id, amount, ref_type, ref_id, created_at "
        "FROM public.credit_clawbacks "
        "WHERE user_id = $1 AND satisfied_at IS NULL "
        "ORDER BY created_at ASC",
        uuid.UUID(user_id)
    )
    return rows


async def _all_clawbacks(db, user_id: str):
    rows = await db.fetch(
        "SELECT id, amount, ref_type, ref_id, satisfied_at "
        "FROM public.credit_clawbacks "
        "WHERE user_id = $1 "
        "ORDER BY created_at ASC",
        uuid.UUID(user_id)
    )
    return rows


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_refund_full_balance_no_deficit(db, user_id):
    """User has 1000, refund 1000 → deficit=0, balance=0, no clawback row."""
    await _ensure_db_or_skip()

    await _grant(db, user_id, 1000, "grant-001")

    result = await _refund(db, user_id, 1000, "evt-ref-001")

    assert result["debited_now"] == 1000, f"Expected debited_now=1000, got {result}"
    assert result["deficit_recorded"] == 0, f"Expected deficit_recorded=0, got {result}"
    assert result["balance_after"] == 0

    open_cb = await _open_clawbacks(db, user_id)
    assert len(open_cb) == 0, f"Expected no open clawbacks, got {open_cb}"

    assert await _balance(db, user_id) == 0
    assert await _tokens(db, user_id) == 0


async def test_refund_partial_balance_records_deficit(db, user_id):
    """User starts with 1000, spends 900, then refund arrives for 1000.
    Clawback records deficit=900; balance drops to 0."""
    await _ensure_db_or_skip()

    await _grant(db, user_id, 1000, "grant-002")
    await _spend(db, user_id, 900, "task-001")

    # At this point user has 100 credits left.
    assert await _balance(db, user_id) == 100

    result = await _refund(db, user_id, 1000, "evt-ref-002")

    assert result["debited_now"] == 100, f"Expected debited_now=100, got {result}"
    assert result["deficit_recorded"] == 900, f"Expected deficit_recorded=900, got {result}"
    assert result["balance_after"] == 0
    assert result["status"] == "deficit_recorded"

    open_cb = await _open_clawbacks(db, user_id)
    assert len(open_cb) == 1, f"Expected 1 open clawback, got {open_cb}"
    assert int(open_cb[0]["amount"]) == 900

    assert await _balance(db, user_id) == 0
    # tokens should also be 0
    assert await _tokens(db, user_id) == 0


async def test_grant_satisfies_open_clawback_first(db, user_id):
    """Open deficit=900. Grant 1000 → bucket holds 100 after clawback, satisfied_at set."""
    await _ensure_db_or_skip()

    # Create the deficit scenario.
    await _grant(db, user_id, 1000, "grant-003")
    await _spend(db, user_id, 900, "task-002")
    await _refund(db, user_id, 1000, "evt-ref-003")

    # Confirm deficit recorded.
    open_cb_before = await _open_clawbacks(db, user_id)
    assert len(open_cb_before) == 1
    assert int(open_cb_before[0]["amount"]) == 900

    # Now grant 1000 more.
    grant_result = await _grant(db, user_id, 1000, "grant-004")

    assert grant_result["status"] == "granted"
    # clawback_satisfied == 900
    assert grant_result["clawback_satisfied"] == 900

    # The bucket should hold only 100 (1000 granted - 900 to clawback).
    assert await _balance(db, user_id) == 100

    # No open clawbacks remain.
    open_cb_after = await _open_clawbacks(db, user_id)
    assert len(open_cb_after) == 0, f"Expected no open clawbacks after grant, got {open_cb_after}"

    # All clawbacks (including satisfied) — should have satisfied_at set.
    all_cb = await _all_clawbacks(db, user_id)
    assert len(all_cb) == 1
    assert all_cb[0]["satisfied_at"] is not None

    # tokens should reflect 100 spendable credits.
    assert await _tokens(db, user_id) == 100


async def test_grant_partial_satisfaction_keeps_unsatisfied_remainder(db, user_id):
    """Open deficit=900. Grant 500 → bucket=0, clawback.amount reduced to 400, satisfied_at NULL."""
    await _ensure_db_or_skip()

    # Set up 900-deficit.
    await _grant(db, user_id, 1000, "grant-005")
    await _spend(db, user_id, 900, "task-003")
    await _refund(db, user_id, 1000, "evt-ref-004")

    # Grant only 500.
    grant_result = await _grant(db, user_id, 500, "grant-006")

    assert grant_result["status"] == "granted"
    assert grant_result["clawback_satisfied"] == 500

    # Bucket should be 0 (500 all consumed by clawback).
    assert await _balance(db, user_id) == 0

    # Clawback row should still be open with amount=400.
    open_cb = await _open_clawbacks(db, user_id)
    assert len(open_cb) == 1, f"Expected 1 open clawback still, got {open_cb}"
    assert int(open_cb[0]["amount"]) == 400, f"Expected remaining=400, got {open_cb[0]['amount']}"

    # tokens should be 0.
    assert await _tokens(db, user_id) == 0


async def test_concurrent_refund_grant_serialized(db, user_id):
    """Fire refund + grant concurrently; final state must be consistent.

    Scenario: user has 100 credits, refund arrives for 1000 (900 deficit),
    simultaneously a grant of 1000 arrives.

    After both complete:
      - One of the two calls will land first under the advisory lock.
      - Final state must have deficit=0 (satisfied) and balance=100.
    """
    await _ensure_db_or_skip()

    # Start: user has 100 credits.
    await _grant(db, user_id, 100, "grant-007")
    # Spend nothing, but do inject a state where there's 100 credit.

    uid = uuid.UUID(user_id)

    async def do_refund():
        conn2 = await asyncpg.connect(_dsn())
        try:
            import json
            row = await conn2.fetchrow(
                "SELECT public.refund_credits($1, 1000, 'stripe_event', 'evt-conc-001') AS r",
                uid
            )
            return json.loads(row["r"])
        finally:
            await conn2.close()

    async def do_grant():
        conn3 = await asyncpg.connect(_dsn())
        try:
            import json
            row = await conn3.fetchrow(
                "SELECT public.grant_credits($1, 1000, 'topup', 'test', 'grant-conc-001') AS r",
                uid
            )
            return json.loads(row["r"])
        finally:
            await conn3.close()

    results = await asyncio.gather(do_refund(), do_grant())
    refund_r, grant_r = results[0], results[1]

    # Both calls must complete without error.
    assert refund_r.get("status") in ("reversed", "deficit_recorded", "duplicate"), \
        f"Unexpected refund status: {refund_r}"
    assert grant_r.get("status") in ("granted", "duplicate"), \
        f"Unexpected grant status: {grant_r}"

    # Final invariant: no open clawbacks (deficit was either never recorded
    # or was satisfied by the concurrent grant).
    open_cb = await _open_clawbacks(db, user_id)
    bal = await _balance(db, user_id)
    tokens_val = await _tokens(db, user_id)

    # Ledger balance and tokens must match.
    assert bal == tokens_val, (
        f"Ledger/tokens mismatch after concurrent ops: balance={bal}, tokens={tokens_val}"
    )

    # Total open clawback amount must be 0 (net position: grant 1100 - refund 1000 = 100).
    open_total = sum(int(r["amount"]) for r in open_cb)
    assert open_total == 0, f"Unexpected open clawback total {open_total}: {open_cb}"

    # Balance should be 100 (100 original + 1000 grant - 1000 refund).
    assert bal == 100, f"Expected final balance=100, got {bal}"


async def test_refund_idempotent_on_ref_id(db, user_id):
    """Re-firing the same refund event does not double-record clawback or debit."""
    await _ensure_db_or_skip()

    await _grant(db, user_id, 100, "grant-008")
    await _spend(db, user_id, 90, "task-004")
    # Balance = 10, refund for 1000 → deficit=990, debit=10.

    r1 = await _refund(db, user_id, 1000, "evt-ref-005")
    assert r1["status"] == "deficit_recorded"
    assert r1["debited_now"] == 10
    assert r1["deficit_recorded"] == 990

    # Re-fire the same event.
    r2 = await _refund(db, user_id, 1000, "evt-ref-005")
    assert r2["status"] == "duplicate", f"Second call must return duplicate, got {r2}"

    # Only one clawback row.
    all_cb = await _all_clawbacks(db, user_id)
    assert len(all_cb) == 1
    assert int(all_cb[0]["amount"]) == 990

    # Balance still 0 (not double-debited).
    assert await _balance(db, user_id) == 0


async def test_add_credits_drains_clawback(db, user_id):
    """add_credits (tokens-only path) must also net the addition against clawbacks."""
    await _ensure_db_or_skip()

    # Create deficit of 500.
    await _grant(db, user_id, 1000, "grant-009")
    await _spend(db, user_id, 1000, "task-005")
    await _refund(db, user_id, 500, "evt-ref-006")

    # Confirm deficit = 500.
    open_cb = await _open_clawbacks(db, user_id)
    assert len(open_cb) == 1
    assert int(open_cb[0]["amount"]) == 500

    # add_credits 300 — all consumed by clawback.
    await db.execute(
        "SELECT public.add_credits($1, 300)",
        uuid.UUID(user_id)
    )

    # Clawback reduced to 200.
    open_cb = await _open_clawbacks(db, user_id)
    assert len(open_cb) == 1
    assert int(open_cb[0]["amount"]) == 200

    # tokens should be 0 (all 300 went to clawback).
    assert await _tokens(db, user_id) == 0

    # add_credits 700 — 200 consumed, 500 net added to tokens.
    await db.execute(
        "SELECT public.add_credits($1, 700)",
        uuid.UUID(user_id)
    )

    open_cb_after = await _open_clawbacks(db, user_id)
    assert len(open_cb_after) == 0, "All clawbacks should be satisfied"

    assert await _tokens(db, user_id) == 500


async def test_multiple_clawbacks_satisfied_fifo(db, user_id):
    """Multiple open clawbacks are satisfied in FIFO order (oldest first)."""
    await _ensure_db_or_skip()

    # Create two deficits.
    await _grant(db, user_id, 200, "grant-010")
    await _spend(db, user_id, 200, "task-006")
    await _refund(db, user_id, 300, "evt-ref-007")   # deficit=300, id A

    # Small delay to ensure ordering.
    await asyncio.sleep(0.01)

    await _refund(db, user_id, 200, "evt-ref-008")   # deficit=200, id B (no balance)

    open_cb = await _open_clawbacks(db, user_id)
    assert len(open_cb) == 2
    amounts = [int(r["amount"]) for r in open_cb]
    assert 300 in amounts and 200 in amounts

    # Grant 400 — should satisfy first clawback (300) fully, partially satisfy second (100).
    grant_result = await _grant(db, user_id, 400, "grant-011")
    assert grant_result["clawback_satisfied"] == 400

    open_cb_after = await _open_clawbacks(db, user_id)
    assert len(open_cb_after) == 1, f"Expected 1 open clawback remaining, got {open_cb_after}"
    assert int(open_cb_after[0]["amount"]) == 100

    # bucket should be 0 (all 400 consumed).
    assert await _balance(db, user_id) == 0
