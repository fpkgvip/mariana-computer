"""C08 — Credit clawback invariants (F-03 regression contract).

These SQL-driven assertions run against the local testdb and verify the
two core economic invariants introduced by migration 009_f03_refund_debt.sql:

  1. After a refund that leaves a deficit:
       SUM(open clawback amounts) > 0
     AND
       profiles.tokens == SUM(non-expired bucket remaining_credits)

  2. After a grant fully satisfies the deficit:
       no open clawbacks remain
     AND
       profiles.tokens == SUM(non-expired bucket remaining_credits)

The tests create their own isolated rows and clean up afterwards.
Skipped when the local testdb is not reachable.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

# ---------------------------------------------------------------------------
# Connection / skip helpers
# ---------------------------------------------------------------------------

PGHOST = os.environ.get("PGHOST", "/tmp")
PGPORT = int(os.environ.get("PGPORT", "55432"))
PGUSER = os.environ.get("PGUSER", "postgres")
PGDATABASE = os.environ.get("PGDATABASE", "testdb")

pytestmark = pytest.mark.asyncio


def _dsn() -> str:
    if PGHOST.startswith("/"):
        return f"postgres://{PGUSER}@/{PGDATABASE}?host={PGHOST}&port={PGPORT}"
    return f"postgres://{PGUSER}@{PGHOST}:{PGPORT}/{PGDATABASE}"


async def _db_reachable() -> bool:
    try:
        conn = await asyncpg.connect(_dsn(), timeout=3)
        await conn.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def conn():
    c = await asyncpg.connect(_dsn())
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def user_id(conn):
    """Create an isolated test user; clean up afterwards."""
    uid = uuid.uuid4()
    uid_str = str(uid)
    await conn.execute(
        "INSERT INTO auth.users (id, email) VALUES ($1, $2)",
        uid, uid_str[:8] + "@c08.example"
    )
    existing = await conn.fetchrow(
        "SELECT id FROM public.profiles WHERE id = $1", uid
    )
    if not existing:
        await conn.execute(
            "INSERT INTO public.profiles (id, email, tokens) VALUES ($1, $2, 0)",
            uid, uid_str[:8] + "@c08.example"
        )
    yield uid_str
    await conn.execute("DELETE FROM public.credit_clawbacks WHERE user_id = $1", uid)
    await conn.execute("DELETE FROM public.credit_transactions WHERE user_id = $1", uid)
    await conn.execute("DELETE FROM public.credit_buckets WHERE user_id = $1", uid)
    await conn.execute("DELETE FROM public.profiles WHERE id = $1", uid)
    await conn.execute("DELETE FROM auth.users WHERE id = $1", uid)


# ---------------------------------------------------------------------------
# Invariant helpers
# ---------------------------------------------------------------------------

async def _assert_ledger_token_sync(conn, uid_str: str, context: str):
    """Core invariant: profiles.tokens == SUM(non-expired bucket credits)."""
    uid = uuid.UUID(uid_str)
    row = await conn.fetchrow("""
        SELECT
          p.tokens,
          COALESCE(SUM(b.remaining_credits), 0)::integer AS ledger_balance
        FROM public.profiles p
        LEFT JOIN public.credit_buckets b
          ON b.user_id = p.id
         AND b.remaining_credits > 0
         AND (b.expires_at IS NULL OR b.expires_at > clock_timestamp())
        WHERE p.id = $1
        GROUP BY p.tokens
    """, uid)
    assert row is not None, f"{context}: profile not found"
    tokens = int(row["tokens"])
    ledger = int(row["ledger_balance"])
    assert tokens == ledger, (
        f"{context}: B-05 sync violated — profiles.tokens={tokens} "
        f"but ledger balance={ledger}"
    )


async def _open_clawback_total(conn, uid_str: str) -> int:
    uid = uuid.UUID(uid_str)
    row = await conn.fetchrow(
        "SELECT COALESCE(SUM(amount), 0)::integer AS total "
        "FROM public.credit_clawbacks WHERE user_id = $1 AND satisfied_at IS NULL",
        uid
    )
    return int(row["total"])


# ---------------------------------------------------------------------------
# C08-A: After a refund that leaves a deficit, open clawbacks > 0 AND tokens synced.
# ---------------------------------------------------------------------------

async def test_c08a_deficit_leaves_open_clawback_and_tokens_synced(conn, user_id):
    """C08-A invariant: deficit refund → open clawbacks > 0 AND ledger = tokens."""
    if not await _db_reachable():
        pytest.skip("local testdb not reachable")

    uid = uuid.UUID(user_id)

    # Grant 1000, spend 900, leaving 100 in ledger.
    import json
    r = await conn.fetchrow(
        "SELECT public.grant_credits($1, 1000, 'topup', 'test', 'c08a-grant') AS r", uid
    )
    assert json.loads(r["r"])["status"] == "granted"

    r = await conn.fetchrow(
        "SELECT public.spend_credits($1, 900, 'task', 'c08a-task') AS r", uid
    )
    assert json.loads(r["r"])["status"] == "spent"

    # Refund 1000 → debits 100, records deficit 900.
    r = await conn.fetchrow(
        "SELECT public.refund_credits($1, 1000, 'stripe_event', 'c08a-ref') AS r", uid
    )
    result = json.loads(r["r"])
    assert result["deficit_recorded"] == 900, f"Unexpected: {result}"

    # ---- INVARIANT 1: open clawbacks > 0 ----
    open_total = await _open_clawback_total(conn, user_id)
    assert open_total > 0, (
        f"C08-A FAIL: expected open clawbacks after deficit refund, got total={open_total}"
    )

    # ---- INVARIANT 2: profiles.tokens == ledger balance ----
    await _assert_ledger_token_sync(conn, user_id, "C08-A post-refund")


# ---------------------------------------------------------------------------
# C08-B: After a grant fully satisfies the deficit, no open clawbacks AND
#        profiles.tokens reflects the post-deduction balance.
# ---------------------------------------------------------------------------

async def test_c08b_grant_satisfies_deficit_no_open_clawbacks_tokens_correct(conn, user_id):
    """C08-B invariant: full satisfaction → no open clawbacks AND ledger = tokens."""
    if not await _db_reachable():
        pytest.skip("local testdb not reachable")

    uid = uuid.UUID(user_id)
    import json

    # Build up a 900-credit deficit.
    await conn.fetchrow(
        "SELECT public.grant_credits($1, 1000, 'topup', 'test', 'c08b-g1') AS r", uid
    )
    await conn.fetchrow(
        "SELECT public.spend_credits($1, 900, 'task', 'c08b-task') AS r", uid
    )
    await conn.fetchrow(
        "SELECT public.refund_credits($1, 1000, 'stripe_event', 'c08b-ref') AS r", uid
    )

    # Confirm deficit before.
    open_before = await _open_clawback_total(conn, user_id)
    assert open_before == 900

    # Grant 1000 — fully satisfies the deficit.
    r = await conn.fetchrow(
        "SELECT public.grant_credits($1, 1000, 'topup', 'test', 'c08b-g2') AS r", uid
    )
    result = json.loads(r["r"])
    assert result["status"] == "granted"

    # ---- INVARIANT 1: no open clawbacks ----
    open_after = await _open_clawback_total(conn, user_id)
    assert open_after == 0, (
        f"C08-B FAIL: expected no open clawbacks after full grant, got {open_after}"
    )

    # ---- INVARIANT 2: profiles.tokens == ledger balance (100 net) ----
    await _assert_ledger_token_sync(conn, user_id, "C08-B post-grant")

    # Concrete value check: 1000 granted - 900 clawback satisfied = 100.
    row = await conn.fetchrow(
        "SELECT tokens FROM public.profiles WHERE id = $1", uid
    )
    assert int(row["tokens"]) == 100, (
        f"C08-B FAIL: expected 100 tokens after clawback satisfaction, "
        f"got {row['tokens']}"
    )
