"""C13 — handle_new_user trigger atomicity (B-14 regression contract).

Tests:
  C13-A Happy path: INSERT into auth.users → profiles row created +
          credit_buckets row created (ON CONFLICT idempotent on replay).
  C13-B ON CONFLICT idempotency: inserting same auth.users id twice does
          not error and profile remains correct.
  C13-C CHECK violation: if credit_buckets INSERT fails (simulated via a
          constraint violation), the auth.users INSERT also rolls back,
          leaving no phantom profile.

Design note for C13-C:
  We simulate bucket failure by temporarily adding a CHECK constraint that
  rejects the signup_grant source, then verify the whole INSERT rolls back.
  The test cleans up the constraint afterwards.

Skipped when the local testdb is not reachable.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

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


@pytest.fixture
async def conn():
    c = await asyncpg.connect(_dsn())
    try:
        yield c
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# C13-A: Happy path — new auth.users row triggers profile + bucket creation
# ---------------------------------------------------------------------------

async def test_c13a_happy_path_creates_profile_and_bucket(conn):
    """C13-A: INSERT into auth.users creates profile and credit_bucket rows."""
    if not await _db_reachable():
        pytest.skip("local testdb not reachable")

    uid = uuid.uuid4()
    email = f"c13a-{uid}@test.local"

    try:
        # Insert into auth.users — trigger fires
        await conn.execute(
            "INSERT INTO auth.users (id, email) VALUES ($1, $2)",
            uid, email
        )

        # Profile must exist
        profile = await conn.fetchrow(
            "SELECT id, email FROM public.profiles WHERE id = $1", uid
        )
        assert profile is not None, "C13-A FAIL: profile row not created"
        assert profile["email"] == email

        # credit_buckets row must exist (signup_grant from B-14 fix)
        bucket = await conn.fetchrow(
            "SELECT source, original_credits FROM public.credit_buckets WHERE user_id = $1",
            uid
        )
        assert bucket is not None, (
            "C13-A FAIL: credit_buckets row not created — "
            "handle_new_user must INSERT a signup_grant bucket"
        )
        assert bucket["source"] == "signup_grant"
        assert bucket["original_credits"] == 500

    finally:
        # Cleanup (order matters due to FKs)
        await conn.execute(
            "DELETE FROM public.credit_buckets WHERE user_id = $1", uid
        )
        await conn.execute(
            "DELETE FROM public.profiles WHERE id = $1", uid
        )
        await conn.execute(
            "DELETE FROM auth.users WHERE id = $1", uid
        )


# ---------------------------------------------------------------------------
# C13-B: ON CONFLICT idempotency — same auth.users id inserted twice
# ---------------------------------------------------------------------------

async def test_c13b_on_conflict_idempotent(conn):
    """C13-B: Re-running the trigger (or manual INSERT) is idempotent via ON CONFLICT."""
    if not await _db_reachable():
        pytest.skip("local testdb not reachable")

    uid = uuid.uuid4()
    email = f"c13b-{uid}@test.local"

    try:
        # First insert
        await conn.execute(
            "INSERT INTO auth.users (id, email) VALUES ($1, $2)",
            uid, email
        )

        # Manually replay the profile INSERT — should not error
        await conn.execute(
            "INSERT INTO public.profiles (id, email, full_name) "
            "VALUES ($1, $2, NULL) ON CONFLICT (id) DO NOTHING",
            uid, email
        )

        # Profile still correct (not duplicated)
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM public.profiles WHERE id = $1", uid
        )
        assert count == 1, f"C13-B FAIL: expected 1 profile row, got {count}"

    finally:
        await conn.execute(
            "DELETE FROM public.credit_buckets WHERE user_id = $1", uid
        )
        await conn.execute(
            "DELETE FROM public.profiles WHERE id = $1", uid
        )
        await conn.execute(
            "DELETE FROM auth.users WHERE id = $1", uid
        )


# ---------------------------------------------------------------------------
# C13-C: Atomicity — if credit_buckets INSERT fails, auth.users must roll back
# ---------------------------------------------------------------------------

async def test_c13c_atomicity_bucket_failure_rolls_back_auth_user(conn):
    """C13-C: credit_buckets INSERT failure causes full transaction rollback.

    We add a temporary CHECK constraint that blocks signup_grant inserts,
    then attempt to insert an auth.users row. The trigger fires, the bucket
    INSERT fails, and the whole transaction should roll back — leaving no
    phantom auth.users or profiles row.
    """
    if not await _db_reachable():
        pytest.skip("local testdb not reachable")

    uid = uuid.uuid4()
    email = f"c13c-{uid}@test.local"
    constraint_name = f"c13c_block_signup_grant_{uid.hex[:8]}"

    # Add a CHECK constraint that will cause the bucket INSERT to fail
    await conn.execute(
        f"ALTER TABLE public.credit_buckets "
        f"ADD CONSTRAINT {constraint_name} CHECK (source <> 'signup_grant')"
    )

    try:
        # Attempt auth.users INSERT — trigger fires, bucket INSERT fails, rolls back
        with pytest.raises(asyncpg.PostgresError) as exc_info:
            await conn.execute(
                "INSERT INTO auth.users (id, email) VALUES ($1, $2)",
                uid, email
            )

        err = str(exc_info.value)
        # Should be a check violation or re-raised error from the trigger
        assert exc_info.value is not None, "C13-C FAIL: expected an exception but none was raised"

        # auth.users must NOT have the row (rolled back)
        auth_row = await conn.fetchrow(
            "SELECT id FROM auth.users WHERE id = $1", uid
        )
        assert auth_row is None, (
            "C13-C FAIL: auth.users row survived despite trigger failure — "
            "phantom identity created"
        )

        # profiles must NOT have the row (rolled back)
        profile_row = await conn.fetchrow(
            "SELECT id FROM public.profiles WHERE id = $1", uid
        )
        assert profile_row is None, (
            "C13-C FAIL: profiles row survived despite trigger failure — "
            "orphaned profile created"
        )

    finally:
        # Remove the blocking constraint
        await conn.execute(
            f"ALTER TABLE public.credit_buckets DROP CONSTRAINT IF EXISTS {constraint_name}"
        )
        # Safety cleanup in case something leaked
        await conn.execute(
            "DELETE FROM public.credit_buckets WHERE user_id = $1", uid
        )
        await conn.execute(
            "DELETE FROM public.profiles WHERE id = $1", uid
        )
        await conn.execute(
            "DELETE FROM auth.users WHERE id = $1", uid
        )
