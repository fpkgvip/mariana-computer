"""B-16 regression suite: admin_set_credits writes credit_transactions ledger.

Tests verify that after migration 012 the admin_set_credits Postgres function
inserts credit_transactions rows (type='grant' or type='spend') in addition to
updating profiles.tokens.

Strategy: direct Postgres calls against the local test DB
(PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb).
Each test runs inside a transaction that is always rolled back for isolation.

Test IDs:
  1. test_grant_absolute_inserts_credit_transaction_row
  2. test_grant_inserts_credit_bucket_row
  3. test_debit_absolute_inserts_spend_transaction_row
  4. test_delta_grant_inserts_credit_transaction_row
  5. test_profiles_tokens_equals_final_balance
"""

from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

PGHOST = os.environ.get("PGHOST", "/tmp")
PGPORT = int(os.environ.get("PGPORT", "55432"))
PGUSER = os.environ.get("PGUSER", "postgres")
PGDATABASE = os.environ.get("PGDATABASE", "testdb")

# asyncpg requires host as query param when using Unix socket paths.
if PGHOST.startswith("/"):
    DSN = f"postgresql://{PGUSER}@/{PGDATABASE}?host={PGHOST}&port={PGPORT}"
else:
    DSN = f"postgresql://{PGUSER}@{PGHOST}:{PGPORT}/{PGDATABASE}"


@pytest.fixture()
async def conn():
    """Provide a single asyncpg connection with auto-rollback after each test."""
    connection = await asyncpg.connect(dsn=DSN)
    tr = connection.transaction()
    await tr.start()
    yield connection
    await tr.rollback()
    await connection.close()


async def _make_admin_and_target(connection) -> tuple[str, str]:
    """Insert a fresh admin + target user pair as postgres superuser."""
    admin_id = str(uuid.uuid4())
    target_id = str(uuid.uuid4())
    await connection.execute(
        "INSERT INTO public.profiles (id, email, role, tokens) VALUES ($1,$2,'admin',0)",
        uuid.UUID(admin_id),
        f"admin_{admin_id[:8]}@test.local",
    )
    await connection.execute(
        "INSERT INTO public.profiles (id, email, role, tokens) VALUES ($1,$2,'user',0)",
        uuid.UUID(target_id),
        f"target_{target_id[:8]}@test.local",
    )
    return admin_id, target_id


async def _call_admin_set_credits(
    connection, admin_id: str, target_id: str, amount: int, is_delta: bool = False
) -> int:
    """Call admin_set_credits as authenticated role with admin_id JWT claim."""
    await connection.execute("SET LOCAL ROLE authenticated")
    await connection.execute(
        "SELECT set_config('request.jwt.claim.sub', $1, true)", admin_id
    )
    result = await connection.fetchval(
        "SELECT public.admin_set_credits($1, $2, $3)",
        uuid.UUID(target_id),
        amount,
        is_delta,
    )
    # Reset role to postgres for subsequent verifying SELECTs.
    await connection.execute("RESET ROLE")
    await connection.execute("SELECT set_config('request.jwt.claim.sub', '', true)")
    return result


@pytest.mark.asyncio
async def test_grant_absolute_inserts_credit_transaction_row(conn):
    """Absolute set that increases balance must insert a 'grant' transaction."""
    admin_id, target_id = await _make_admin_and_target(conn)
    result = await _call_admin_set_credits(conn, admin_id, target_id, 500, is_delta=False)
    assert result == 500

    rows = await conn.fetch(
        "SELECT type, credits, balance_after, metadata FROM public.credit_transactions "
        "WHERE user_id = $1 ORDER BY created_at DESC LIMIT 5",
        uuid.UUID(target_id),
    )
    assert len(rows) >= 1, "Expected at least one credit_transactions row after grant"
    tx = rows[0]
    assert tx["type"] == "grant", f"Expected type='grant', got {tx['type']!r}"
    assert tx["credits"] == 500
    assert tx["balance_after"] == 500
    raw_meta = tx["metadata"]
    if isinstance(raw_meta, str):
        import json
        meta = json.loads(raw_meta)
    else:
        meta = dict(raw_meta) if raw_meta else {}
    assert meta.get("admin_action") is True, "metadata must include admin_action=true"


@pytest.mark.asyncio
async def test_grant_inserts_credit_bucket_row(conn):
    """Absolute grant must also insert a credit_buckets row with source='admin_grant'."""
    admin_id, target_id = await _make_admin_and_target(conn)
    await _call_admin_set_credits(conn, admin_id, target_id, 200, is_delta=False)

    bucket = await conn.fetchrow(
        "SELECT source, original_credits, remaining_credits FROM public.credit_buckets "
        "WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
        uuid.UUID(target_id),
    )
    assert bucket is not None, "Expected a credit_buckets row after admin grant"
    assert bucket["source"] == "admin_grant"
    assert bucket["original_credits"] == 200
    assert bucket["remaining_credits"] == 200


@pytest.mark.asyncio
async def test_debit_absolute_inserts_spend_transaction_row(conn):
    """Absolute set that decreases balance must insert a 'spend' transaction."""
    admin_id, target_id = await _make_admin_and_target(conn)

    # First grant 1000 credits.
    await _call_admin_set_credits(conn, admin_id, target_id, 1000, is_delta=False)

    # Now reduce to 400 (debit 600).
    await _call_admin_set_credits(conn, admin_id, target_id, 400, is_delta=False)

    rows = await conn.fetch(
        "SELECT type, credits, balance_after FROM public.credit_transactions "
        "WHERE user_id = $1 ORDER BY created_at",
        uuid.UUID(target_id),
    )
    spend_rows = [r for r in rows if r["type"] == "spend"]
    assert len(spend_rows) >= 1, "Expected at least one 'spend' transaction for debit"
    total_spent = sum(r["credits"] for r in spend_rows)
    assert total_spent == 600, f"Expected total spend=600, got {total_spent}"


@pytest.mark.asyncio
async def test_delta_grant_inserts_credit_transaction_row(conn):
    """Delta grant (delta=True, positive amount) must insert a 'grant' transaction."""
    admin_id, target_id = await _make_admin_and_target(conn)
    result = await _call_admin_set_credits(conn, admin_id, target_id, 300, is_delta=True)
    assert result == 300

    rows = await conn.fetch(
        "SELECT type, credits FROM public.credit_transactions WHERE user_id=$1",
        uuid.UUID(target_id),
    )
    grant_rows = [r for r in rows if r["type"] == "grant"]
    assert len(grant_rows) >= 1, "Expected at least one 'grant' transaction for delta grant"
    assert sum(r["credits"] for r in grant_rows) == 300


@pytest.mark.asyncio
async def test_profiles_tokens_equals_final_balance(conn):
    """profiles.tokens must equal the balance returned by admin_set_credits."""
    admin_id, target_id = await _make_admin_and_target(conn)
    result = await _call_admin_set_credits(conn, admin_id, target_id, 750, is_delta=False)

    tokens = await conn.fetchval(
        "SELECT tokens FROM public.profiles WHERE id=$1",
        uuid.UUID(target_id),
    )
    assert tokens == result == 750, (
        f"profiles.tokens ({tokens}) must equal returned balance ({result}) = 750"
    )
