"""
Unit tests for tools/reconcile_ledger.py.

The reconciler is a READ-ONLY drift detector. We verify:
  - clean ledger -> 0 drift, exit 0
  - drift only between profiles.tokens and bucket sums
  - drift only between profiles.tokens and the latest tx balance_after
  - expired buckets are excluded from bucket_balance
  - --since-hours filters out users whose last tx is older than the window
  - --limit caps the result set
  - --json emits a parseable JSON document with drifted_users + rows
  - the script is idempotent: a second invocation with no data change
    produces the same output (proves it has no side effects)
  - the latest tx is determined by created_at DESC (then id DESC) per user
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tools import reconcile_ledger as rl


def _uid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------

def test_clean_ledger_reports_no_drift(db, fixtures):
    dsn, conn = db
    u = _uid()
    with conn.cursor() as cur:
        fixtures["profile"](cur, u, "a@example.com", 100)
        fixtures["bucket"](cur, u, 100)
        fixtures["tx"](cur, u, "grant", 100, 100, ref_type="signup", ref_id="s1")

    rows = rl.reconcile(dsn)
    assert rows == []


def test_drift_tokens_vs_bucket_only(db, fixtures):
    dsn, conn = db
    u = _uid()
    with conn.cursor() as cur:
        fixtures["profile"](cur, u, "b@example.com", 250)
        fixtures["bucket"](cur, u, 100)  # bucket short by 150
        # no transactions for this user

    rows = rl.reconcile(dsn)
    assert len(rows) == 1
    r = rows[0]
    assert r.tokens == 250
    assert r.bucket_balance == 100
    assert r.ledger_balance is None
    assert r.drift_tokens_vs_bucket == 150
    assert r.drift_tokens_vs_ledger is None


def test_drift_tokens_vs_ledger_only(db, fixtures):
    dsn, conn = db
    u = _uid()
    with conn.cursor() as cur:
        fixtures["profile"](cur, u, "c@example.com", 300)
        fixtures["bucket"](cur, u, 300)
        fixtures["tx"](cur, u, "grant", 200, 200, ref_type="signup", ref_id="s2")

    rows = rl.reconcile(dsn)
    assert len(rows) == 1
    r = rows[0]
    assert r.drift_tokens_vs_bucket == 0
    assert r.drift_tokens_vs_ledger == 100


def test_only_drifted_users_returned(db, fixtures):
    dsn, conn = db
    u_clean = _uid()
    u_drift = _uid()
    with conn.cursor() as cur:
        fixtures["profile"](cur, u_clean, "clean@example.com", 50)
        fixtures["bucket"](cur, u_clean, 50)
        fixtures["tx"](cur, u_clean, "grant", 50, 50, ref_type="x", ref_id="1")

        fixtures["profile"](cur, u_drift, "drift@example.com", 100)
        fixtures["bucket"](cur, u_drift, 25)

    rows = rl.reconcile(dsn)
    assert [r.user_id for r in rows] == [u_drift]


# ---------------------------------------------------------------------------
# Bucket-balance semantics
# ---------------------------------------------------------------------------

def test_expired_buckets_excluded_from_bucket_balance(db, fixtures):
    dsn, conn = db
    u = _uid()
    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = datetime.now(timezone.utc) + timedelta(days=30)
    with conn.cursor() as cur:
        fixtures["profile"](cur, u, "exp@example.com", 200)
        fixtures["bucket"](cur, u, 100, expires_at=future)
        fixtures["bucket"](cur, u, 100, expires_at=past)  # expired
        # If reconciler counted expired -> bucket_balance=200 -> no drift
        # We expect bucket_balance=100 and 100 drift.

    rows = rl.reconcile(dsn)
    assert len(rows) == 1
    assert rows[0].bucket_balance == 100
    assert rows[0].drift_tokens_vs_bucket == 100


def test_zero_or_negative_remaining_buckets_excluded(db, fixtures):
    dsn, conn = db
    u = _uid()
    with conn.cursor() as cur:
        fixtures["profile"](cur, u, "z@example.com", 100)
        fixtures["bucket"](cur, u, 100)
        fixtures["bucket"](cur, u, 0)  # exhausted

    rows = rl.reconcile(dsn)
    # tokens=100, bucket_balance=100 -> clean
    assert rows == []


def test_user_with_no_buckets_or_tx_drifts_against_zero(db, fixtures):
    dsn, conn = db
    u = _uid()
    with conn.cursor() as cur:
        fixtures["profile"](cur, u, "naked@example.com", 75)
    rows = rl.reconcile(dsn)
    assert len(rows) == 1
    assert rows[0].bucket_balance == 0
    assert rows[0].drift_tokens_vs_bucket == 75
    assert rows[0].ledger_balance is None
    assert rows[0].drift_tokens_vs_ledger is None


# ---------------------------------------------------------------------------
# Latest-tx selection
# ---------------------------------------------------------------------------

def test_latest_tx_by_created_at_descending(db, fixtures):
    dsn, conn = db
    u = _uid()
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        fixtures["profile"](cur, u, "latest@example.com", 80)
        fixtures["bucket"](cur, u, 80)
        # earlier tx says balance_after=999, latest says 80 (matches tokens)
        fixtures["tx"](cur, u, "grant", 999, 999,
                       ref_type="x", ref_id="old",
                       created_at=now - timedelta(hours=2))
        fixtures["tx"](cur, u, "spend", -919, 80,
                       ref_type="y", ref_id="new",
                       created_at=now - timedelta(minutes=5))

    rows = rl.reconcile(dsn)
    assert rows == []


# ---------------------------------------------------------------------------
# CLI options: --since-hours, --limit, --json
# ---------------------------------------------------------------------------

def test_since_hours_filters_old_users(db, fixtures):
    dsn, conn = db
    u_old = _uid()
    u_new = _uid()
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        fixtures["profile"](cur, u_old, "old@example.com", 200)
        fixtures["bucket"](cur, u_old, 100)
        fixtures["tx"](cur, u_old, "grant", 100, 100,
                       ref_type="x", ref_id="old1",
                       created_at=now - timedelta(hours=72))

        fixtures["profile"](cur, u_new, "new@example.com", 200)
        fixtures["bucket"](cur, u_new, 100)
        fixtures["tx"](cur, u_new, "grant", 100, 100,
                       ref_type="x", ref_id="new1",
                       created_at=now - timedelta(minutes=10))

    # Both drift if no window. With a 1-hour window, only u_new survives.
    all_rows = rl.reconcile(dsn)
    assert {r.user_id for r in all_rows} == {u_old, u_new}

    recent = rl.reconcile(dsn, since_hours=1)
    assert [r.user_id for r in recent] == [u_new]


def test_limit_caps_result_set(db, fixtures):
    dsn, conn = db
    with conn.cursor() as cur:
        for i in range(5):
            u = _uid()
            fixtures["profile"](cur, u, f"u{i}@example.com", 100 + i)
            fixtures["bucket"](cur, u, 0)

    rows = rl.reconcile(dsn, limit=2)
    assert len(rows) == 2


def test_json_output_shape(db, fixtures, tmp_path, capsys):
    dsn, conn = db
    u = _uid()
    with conn.cursor() as cur:
        fixtures["profile"](cur, u, "j@example.com", 50)
        fixtures["bucket"](cur, u, 0)

    report = tmp_path / "out.json"
    rc = rl.main(["--dsn", dsn, "--json", "--write-report", str(report)])
    assert rc == 2  # drift detected
    payload = json.loads(report.read_text())
    assert payload["drifted_users"] == 1
    assert payload["rows"][0]["user_id"] == u
    assert payload["rows"][0]["drift_tokens_vs_bucket"] == 50
    assert "generated_at" in payload


def test_human_output_clean(db, capsys):
    dsn, _ = db
    rc = rl.main(["--dsn", dsn])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no drift detected" in out


# ---------------------------------------------------------------------------
# Idempotency / no side effects
# ---------------------------------------------------------------------------

def test_reconcile_is_read_only(db, fixtures):
    dsn, conn = db
    u = _uid()
    with conn.cursor() as cur:
        fixtures["profile"](cur, u, "ro@example.com", 200)
        fixtures["bucket"](cur, u, 100)

    # Snapshot row counts and tokens
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.profiles");           p0 = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.credit_buckets");     b0 = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.credit_transactions"); t0 = cur.fetchone()[0]
        cur.execute("SELECT tokens FROM public.profiles WHERE id=%s", (u,))
        tokens0 = cur.fetchone()[0]

    rl.reconcile(dsn)
    rl.reconcile(dsn)
    rl.reconcile(dsn)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.profiles");           p1 = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.credit_buckets");     b1 = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.credit_transactions"); t1 = cur.fetchone()[0]
        cur.execute("SELECT tokens FROM public.profiles WHERE id=%s", (u,))
        tokens1 = cur.fetchone()[0]

    assert (p0, b0, t0, tokens0) == (p1, b1, t1, tokens1)


def test_repeated_calls_are_deterministic(db, fixtures):
    dsn, conn = db
    with conn.cursor() as cur:
        for i in range(3):
            u = _uid()
            fixtures["profile"](cur, u, f"d{i}@example.com", 100 + i * 10)
            fixtures["bucket"](cur, u, 50)

    a = rl.reconcile(dsn)
    b = rl.reconcile(dsn)
    assert {(r.user_id, r.drift_tokens_vs_bucket) for r in a} == \
           {(r.user_id, r.drift_tokens_vs_bucket) for r in b}
