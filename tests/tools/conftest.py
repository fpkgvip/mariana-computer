"""
conftest for reconcile_ledger tests.

Each test gets a freshly created schema-only fixture inside the
`reconcile_test` database on the local Postgres
(/tmp:55432). The fixture creates the minimal tables that
reconcile_ledger.py reads from: profiles, credit_buckets, credit_transactions.

We do NOT use the full local_baseline.sql because that file pulls
in unrelated RLS, RPCs, and indexes that are tested by the
contract suite. Reconciliation is a read-only query — we test it
in isolation against a minimal but schema-faithful fixture.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DSN = os.environ.get(
    "RECONCILE_TEST_DSN",
    "postgresql://postgres@/reconcile_test?host=/tmp&port=55432",
)


SCHEMA_SQL = """
DROP TABLE IF EXISTS public.credit_transactions CASCADE;
DROP TABLE IF EXISTS public.credit_buckets       CASCADE;
DROP TABLE IF EXISTS public.profiles             CASCADE;

CREATE TABLE public.profiles (
  id          uuid PRIMARY KEY,
  email       text NOT NULL,
  tokens      integer NOT NULL DEFAULT 500,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.credit_buckets (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           uuid NOT NULL REFERENCES public.profiles(id),
  source            text NOT NULL,
  original_credits  integer NOT NULL,
  remaining_credits integer NOT NULL,
  granted_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at        timestamptz
);

CREATE TABLE public.credit_transactions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES public.profiles(id),
  type          text NOT NULL,
  credits       integer NOT NULL,
  bucket_id     uuid,
  ref_type      text,
  ref_id        text,
  balance_after integer NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);
"""


@pytest.fixture
def db():
    """Yield a (dsn, conn) pair against a clean per-test schema."""
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    yield DSN, conn
    conn.close()


def _insert_profile(cur, user_id: str, email: str, tokens: int) -> None:
    cur.execute(
        "INSERT INTO public.profiles (id, email, tokens) VALUES (%s, %s, %s)",
        (user_id, email, tokens),
    )


def _insert_bucket(
    cur,
    user_id: str,
    remaining: int,
    *,
    original: int | None = None,
    expires_at=None,
    source: str = "signup_grant",
) -> None:
    cur.execute(
        """
        INSERT INTO public.credit_buckets
          (user_id, source, original_credits, remaining_credits, expires_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (user_id, source, original if original is not None else remaining, remaining, expires_at),
    )


def _insert_tx(
    cur,
    user_id: str,
    type_: str,
    credits: int,
    balance_after: int,
    *,
    ref_type: str | None = None,
    ref_id: str | None = None,
    created_at=None,
) -> None:
    if created_at is None:
        cur.execute(
            """
            INSERT INTO public.credit_transactions
              (user_id, type, credits, ref_type, ref_id, balance_after)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (user_id, type_, credits, ref_type, ref_id, balance_after),
        )
    else:
        cur.execute(
            """
            INSERT INTO public.credit_transactions
              (user_id, type, credits, ref_type, ref_id, balance_after, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (user_id, type_, credits, ref_type, ref_id, balance_after, created_at),
        )


@pytest.fixture
def fixtures():
    """Helpers for tests to seed data."""
    return {
        "profile": _insert_profile,
        "bucket":  _insert_bucket,
        "tx":      _insert_tx,
    }
