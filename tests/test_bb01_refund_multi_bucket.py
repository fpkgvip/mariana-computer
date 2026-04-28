"""BB-01 regression: refund_credits must succeed on multi-bucket refunds.

Bug
---
Phase E re-audit #31 (A36) found that
``frontend/supabase/migrations/009_f03_refund_debt.sql`` defines
``refund_credits(p_user_id, p_credits, p_ref_type, p_ref_id)`` to FIFO-debit
across ``credit_buckets`` and INSERT one ``credit_transactions`` row PER
bucket touched, all sharing ``(p_ref_type, p_ref_id, type='refund')``.

The unique index ``uq_credit_tx_idem`` from
``004b_credit_tx_idem_concurrent.sql`` covers
``(ref_type, ref_id, type) WHERE type IN ('grant','refund','expiry')``,
so the second loop iteration violates the constraint and the function
aborts with ``UniqueViolation``.

The 004b migration's own comment at lines 9-11 explicitly excludes
``type='spend'`` because spend writes per-bucket — the same exclusion
is required for ``'refund'`` but was overlooked.

Impact paths:
  * Stripe refund webhook on a charge whose user has multiple credit
    buckets — handler raises 500, Stripe retries until giving up,
    user keeps refunded credits.
  * K-02 dispute reversal on multi-bucket users.
  * AA-01 orphan-overrun on multi-bucket users — reservation never
    claws back.

The fix collapses the per-bucket INSERT into a single aggregate
``credit_transactions`` row per ``refund_credits`` call. The function
still updates ``credit_buckets.remaining_credits`` per bucket (FIFO),
but writes ONE ledger row with the total refunded credits, matching
the dedup contract in ``uq_credit_tx_idem``.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest


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


async def _open_pool():
    import asyncpg as _asyncpg  # noqa: PLC0415

    return await _asyncpg.create_pool(
        host=PGHOST,
        port=PGPORT,
        user=PGUSER,
        database=PGDATABASE,
        min_size=2,
        max_size=8,
    )


async def _make_user_with_buckets(
    pool: Any,
    bucket_remainings: list[int],
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Insert a profile + N credit_buckets and return (user_id, bucket_ids).

    Each bucket has the requested ``remaining_credits``.  ``original_credits``
    matches ``remaining_credits`` so the bucket is fully unused.
    """
    user_id = uuid.uuid4()
    email = f"bb01-{user_id.hex[:8]}@test.local"
    bucket_ids: list[uuid.UUID] = []
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO profiles (id, email, tokens) VALUES ($1, $2, $3)",
            user_id, email, sum(bucket_remainings),
        )
        for remaining in bucket_remainings:
            bid = uuid.uuid4()
            bucket_ids.append(bid)
            await conn.execute(
                "INSERT INTO credit_buckets "
                "(id, user_id, source, original_credits, remaining_credits) "
                "VALUES ($1, $2, 'topup', $3, $3)",
                bid, user_id, remaining,
            )
    return user_id, bucket_ids


async def _cleanup(pool: Any, user_id: uuid.UUID) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM credit_transactions WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM credit_clawbacks WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM credit_buckets WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM profiles WHERE id = $1", user_id)


# ---------------------------------------------------------------------------
# (1) Multi-bucket refund must succeed and write exactly one ledger row.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_bb01_multi_bucket_refund_succeeds():
    """Three buckets [10, 10, 10] sum to 30 credits; refund 25.

    Expected:
      - bucket1 = 0, bucket2 = 0, bucket3 = 5 (FIFO drain)
      - exactly ONE credit_transactions row of type='refund' with
        credits=25, ref_type='test_bb01', ref_id=<test ref>
      - status='reversed' (no deficit)
    """
    pool = await _open_pool()
    user_id = None
    try:
        user_id, bucket_ids = await _make_user_with_buckets(pool, [10, 10, 10])
        ref_type = "test_bb01"
        ref_id = uuid.uuid4().hex

        async with pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT public.refund_credits($1, $2, $3, $4)",
                user_id, 25, ref_type, ref_id,
            )

        # Result is a JSONB blob — verify status.
        import json as _json
        if isinstance(result, str):
            result = _json.loads(result)
        assert isinstance(result, dict), f"unexpected result shape: {result!r}"
        assert result.get("status") in ("reversed", "ok"), (
            f"expected status='reversed' or 'ok'; got {result!r}"
        )

        async with pool.acquire() as conn:
            # Bucket balances after FIFO drain.
            b1 = await conn.fetchval(
                "SELECT remaining_credits FROM credit_buckets WHERE id = $1",
                bucket_ids[0],
            )
            b2 = await conn.fetchval(
                "SELECT remaining_credits FROM credit_buckets WHERE id = $1",
                bucket_ids[1],
            )
            b3 = await conn.fetchval(
                "SELECT remaining_credits FROM credit_buckets WHERE id = $1",
                bucket_ids[2],
            )
            assert b1 == 0, f"bucket1 should be drained to 0, got {b1}"
            assert b2 == 0, f"bucket2 should be drained to 0, got {b2}"
            assert b3 == 5, f"bucket3 should retain 5, got {b3}"

            # Exactly one ledger row.
            tx_rows = await conn.fetch(
                "SELECT credits, ref_type, ref_id, type FROM credit_transactions "
                "WHERE ref_type = $1 AND ref_id = $2 AND type = 'refund'",
                ref_type, ref_id,
            )
            assert len(tx_rows) == 1, (
                f"BB-01: refund_credits must write exactly ONE aggregate "
                f"ledger row per call (matching the uq_credit_tx_idem dedup "
                f"contract); got {len(tx_rows)} rows: {[dict(r) for r in tx_rows]}"
            )
            assert tx_rows[0]["credits"] == 25, (
                f"aggregate row must carry the full debited amount; got "
                f"{tx_rows[0]['credits']}"
            )
    finally:
        if user_id is not None:
            await _cleanup(pool, user_id)
        await pool.close()


# ---------------------------------------------------------------------------
# (2) Replay safety: second call with same ref returns duplicate, no extra row.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_bb01_multi_bucket_refund_replay_is_duplicate():
    """A second call with the same ref must return ``status='duplicate'``
    and NOT debit any bucket nor write a second ledger row."""
    pool = await _open_pool()
    user_id = None
    try:
        user_id, bucket_ids = await _make_user_with_buckets(pool, [10, 10, 10])
        ref_type = "test_bb01_replay"
        ref_id = uuid.uuid4().hex

        async with pool.acquire() as conn:
            await conn.fetchval(
                "SELECT public.refund_credits($1, $2, $3, $4)",
                user_id, 25, ref_type, ref_id,
            )
            # Second call.
            result2 = await conn.fetchval(
                "SELECT public.refund_credits($1, $2, $3, $4)",
                user_id, 25, ref_type, ref_id,
            )

        import json as _json
        if isinstance(result2, str):
            result2 = _json.loads(result2)
        assert result2.get("status") == "duplicate", (
            f"replay must return status='duplicate'; got {result2!r}"
        )

        async with pool.acquire() as conn:
            tx_count = await conn.fetchval(
                "SELECT COUNT(*) FROM credit_transactions "
                "WHERE ref_type = $1 AND ref_id = $2 AND type = 'refund'",
                ref_type, ref_id,
            )
            assert tx_count == 1, (
                f"replay must NOT add a second ledger row; got {tx_count}"
            )
            # Bucket balances unchanged from after the first call.
            b1 = await conn.fetchval(
                "SELECT remaining_credits FROM credit_buckets WHERE id = $1",
                bucket_ids[0],
            )
            b3 = await conn.fetchval(
                "SELECT remaining_credits FROM credit_buckets WHERE id = $1",
                bucket_ids[2],
            )
            assert b1 == 0
            assert b3 == 5
    finally:
        if user_id is not None:
            await _cleanup(pool, user_id)
        await pool.close()


# ---------------------------------------------------------------------------
# (3) Single-bucket regression: existing behaviour preserved.
# ---------------------------------------------------------------------------


@_pg_only
@pytest.mark.asyncio
async def test_bb01_single_bucket_refund_still_works():
    """Single-bucket refund (the case existing tests cover) must continue
    to succeed end-to-end and write exactly one ledger row."""
    pool = await _open_pool()
    user_id = None
    try:
        user_id, bucket_ids = await _make_user_with_buckets(pool, [50])
        ref_type = "test_bb01_single"
        ref_id = uuid.uuid4().hex

        async with pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT public.refund_credits($1, $2, $3, $4)",
                user_id, 30, ref_type, ref_id,
            )

        import json as _json
        if isinstance(result, str):
            result = _json.loads(result)
        assert result.get("status") in ("reversed", "ok"), (
            f"single-bucket refund must succeed; got {result!r}"
        )

        async with pool.acquire() as conn:
            b1 = await conn.fetchval(
                "SELECT remaining_credits FROM credit_buckets WHERE id = $1",
                bucket_ids[0],
            )
            assert b1 == 20

            tx_count = await conn.fetchval(
                "SELECT COUNT(*) FROM credit_transactions "
                "WHERE ref_type = $1 AND ref_id = $2 AND type = 'refund'",
                ref_type, ref_id,
            )
            assert tx_count == 1
    finally:
        if user_id is not None:
            await _cleanup(pool, user_id)
        await pool.close()
