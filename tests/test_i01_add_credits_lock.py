"""I-01 regression suite: add_credits advisory lock.

Bug fixed:
- add_credits SQL function lacked pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0))
  used by every sibling ledger function (refund_credits, grant_credits, spend_credits).
- Without the lock, add_credits could read v_open_total from a stale snapshot before a
  concurrent refund_credits commits its credit_clawbacks row, resulting in double-credit.
- Migration 018_i01_add_credits_lock.sql adds the lock as the first executable statement
  after input validation, matching the design in 009_f03_refund_debt.sql lines 101 and 231.

Test inventory (>=4):
  1. function_definition_contains_advisory_lock
  2. sequential_add_credits_no_clawback_adds_full_amount
  3. sequential_add_credits_with_existing_clawback_nets_correctly
  4. add_credits_raises_on_negative_credits
"""

from __future__ import annotations

import os

import pytest

# ---------------------------------------------------------------------------
# Local PG connection (used for DB-level tests).
# Tests that require a live DB are skipped when PGHOST / PGPORT are absent.
# ---------------------------------------------------------------------------

PGHOST = os.environ.get("PGHOST", "/tmp")
PGPORT = int(os.environ.get("PGPORT", "55432"))
PGUSER = os.environ.get("PGUSER", "postgres")
PGDATABASE = os.environ.get("PGDATABASE", "testdb")

try:
    import psycopg2  # type: ignore

    _conn = psycopg2.connect(
        host=PGHOST,
        port=PGPORT,
        user=PGUSER,
        dbname=PGDATABASE,
    )
    _conn.close()
    _PG_AVAILABLE = True
except Exception:
    _PG_AVAILABLE = False

_pg_only = pytest.mark.skipif(not _PG_AVAILABLE, reason="Local PG not available")


def _get_conn():
    return psycopg2.connect(
        host=PGHOST,
        port=PGPORT,
        user=PGUSER,
        dbname=PGDATABASE,
    )


def _make_user(conn) -> str:
    """Insert minimal auth.users + profiles rows and return the id."""
    import uuid

    uid = str(uuid.uuid4())
    with conn.cursor() as cur:
        # credit_clawbacks has FK → auth.users, so insert there first.
        cur.execute(
            """
            INSERT INTO auth.users (id, email)
            VALUES (%s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (uid, f"{uid}@test.example"),
        )
        cur.execute(
            """
            INSERT INTO public.profiles (id, email, tokens)
            VALUES (%s, %s, 0)
            ON CONFLICT (id) DO NOTHING
            """,
            (uid, f"{uid}@test.example"),
        )
    conn.commit()
    return uid


# ---------------------------------------------------------------------------
# 1. Function definition contains the advisory lock call.
#    This is a pure SQL metadata check — no concurrency required.
# ---------------------------------------------------------------------------


@_pg_only
def test_function_definition_contains_advisory_lock():
    """SELECT pg_get_functiondef for add_credits must include pg_advisory_xact_lock."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_get_functiondef('public.add_credits(uuid,integer)'::regprocedure)"
            )
            row = cur.fetchone()
        assert row is not None, "add_credits function not found in pg_proc"
        funcdef: str = row[0]
        assert "pg_advisory_xact_lock" in funcdef, (
            "Migration 018 must add pg_advisory_xact_lock to add_credits body.\n"
            f"Function definition:\n{funcdef}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. Sequential: add_credits with no open clawback adds the full amount.
# ---------------------------------------------------------------------------


@_pg_only
def test_sequential_add_credits_no_clawback_adds_full_amount():
    """With no open clawbacks, add_credits should increase profiles.tokens by p_credits."""
    conn = _get_conn()
    try:
        uid = _make_user(conn)
        with conn.cursor() as cur:
            # Ensure tokens start at 0
            cur.execute("UPDATE public.profiles SET tokens = 0 WHERE id = %s", (uid,))
            conn.commit()

            # Call add_credits
            cur.execute("SELECT public.add_credits(%s, %s)", (uid, 50))
            conn.commit()

            cur.execute("SELECT tokens FROM public.profiles WHERE id = %s", (uid,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 50, f"Expected tokens=50 after add_credits(50), got {row[0]}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. Sequential: add_credits against an open clawback nets correctly.
# ---------------------------------------------------------------------------


@_pg_only
def test_sequential_add_credits_with_existing_clawback_nets_correctly():
    """add_credits(uid, 100) with an open clawback of 60 should net 40 tokens."""
    conn = _get_conn()
    try:
        uid = _make_user(conn)
        with conn.cursor() as cur:
            # Reset tokens
            cur.execute("UPDATE public.profiles SET tokens = 0 WHERE id = %s", (uid,))
            # Insert a clawback of 60; use uid-based ref_id to avoid unique constraint
            # conflicts when the test re-runs against the same testdb.
            cur.execute(
                """
                INSERT INTO public.credit_clawbacks (user_id, amount, ref_type, ref_id)
                VALUES (%s, 60, 'stripe_event', %s)
                """,
                (uid, f"evt_cb_seq_{uid}"),
            )
            conn.commit()

            # Call add_credits(100): 60 consumed by clawback, 40 net
            cur.execute("SELECT public.add_credits(%s, %s)", (uid, 100))
            conn.commit()

            cur.execute("SELECT tokens FROM public.profiles WHERE id = %s", (uid,))
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 40, f"Expected tokens=40, got {row[0]}"

            # The clawback should now be satisfied
            cur.execute(
                """
                SELECT COUNT(*) FROM public.credit_clawbacks
                WHERE user_id = %s AND satisfied_at IS NULL
                """,
                (uid,),
            )
            open_count = cur.fetchone()[0]
        assert open_count == 0, f"Clawback should be satisfied, but {open_count} still open"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. add_credits raises on negative input (regression guard).
# ---------------------------------------------------------------------------


@_pg_only
def test_add_credits_raises_on_negative_credits():
    """add_credits must raise an exception for negative p_credits."""
    import psycopg2  # type: ignore  # noqa: F811

    conn = _get_conn()
    try:
        uid = _make_user(conn)
        with pytest.raises(psycopg2.errors.RaiseException):
            with conn.cursor() as cur:
                cur.execute("SELECT public.add_credits(%s, %s)", (uid, -1))
    finally:
        conn.rollback()
        conn.close()


# ---------------------------------------------------------------------------
# 5. Static check: migration file 018 contains the lock text.
#    Works without a live DB — purely a file content assertion.
# ---------------------------------------------------------------------------


def test_migration_018_file_contains_advisory_lock():
    """Migration 018 SQL file must contain pg_advisory_xact_lock in the function body."""
    import pathlib

    repo_root = pathlib.Path(__file__).parent.parent
    migration_file = (
        repo_root / "frontend" / "supabase" / "migrations" / "018_i01_add_credits_lock.sql"
    )
    assert migration_file.exists(), f"Migration file not found: {migration_file}"
    text = migration_file.read_text()
    assert "pg_advisory_xact_lock" in text, (
        "018_i01_add_credits_lock.sql must contain pg_advisory_xact_lock"
    )
    assert "hashtextextended" in text, (
        "018_i01_add_credits_lock.sql must use hashtextextended to match sibling functions"
    )
