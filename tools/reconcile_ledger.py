"""
reconcile_ledger.py — detect drift between profiles.tokens and the credit ledger.

The Loop 5 audit (R3, R6) found that profiles.tokens and the
credit_buckets/credit_transactions FIFO ledger evolve independently:
  - add_credits / deduct_credits / admin_set_credits / admin_adjust_credits
    mutate profiles.tokens directly without writing to the ledger.
  - grant_credits / spend_credits / refund_credits / expire_credits
    mutate the ledger without touching profiles.tokens.

This script computes:
  - tokens          = profiles.tokens
  - bucket_balance  = SUM(credit_buckets.remaining_credits) FILTER WHERE
                      remaining_credits > 0 AND (expires_at IS NULL OR expires_at > now())
  - ledger_balance  = latest credit_transactions.balance_after for each user

and reports rows where these disagree.

Usage:
    python tools/reconcile_ledger.py --dsn postgresql://... [--json] [--limit N]
                                     [--since-hours N] [--write-report path]

Exit codes:
    0  no drift
    2  drift detected (exits non-zero so cron / CI can alarm)
    1  operational error (could not connect, bad SQL, etc.)

The script is READ-ONLY by design. It never modifies any data.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

try:
    import psycopg  # psycopg 3
except ImportError:  # pragma: no cover - import-time fallback
    psycopg = None  # type: ignore[assignment]


@dataclass
class DriftRow:
    user_id: str
    email: str | None
    tokens: int
    bucket_balance: int
    ledger_balance: int | None
    drift_tokens_vs_bucket: int
    drift_tokens_vs_ledger: int | None
    last_tx_at: str | None


# The reconciliation query. Pure SELECT, no row locks, no side effects.
# Uses LEFT JOIN so users with no credit_transactions still report.
RECONCILE_SQL = """
WITH bucket_sums AS (
  SELECT user_id,
         COALESCE(SUM(remaining_credits) FILTER (
           WHERE remaining_credits > 0
             AND (expires_at IS NULL OR expires_at > now())
         ), 0)::int AS bucket_balance
  FROM public.credit_buckets
  GROUP BY user_id
),
last_tx AS (
  SELECT DISTINCT ON (user_id)
         user_id,
         balance_after AS ledger_balance,
         created_at    AS last_tx_at
  FROM public.credit_transactions
  ORDER BY user_id, created_at DESC, id DESC
)
SELECT p.id::text AS user_id,
       p.email,
       p.tokens,
       COALESCE(b.bucket_balance, 0)        AS bucket_balance,
       lt.ledger_balance,
       p.tokens - COALESCE(b.bucket_balance, 0)
         AS drift_tokens_vs_bucket,
       CASE WHEN lt.ledger_balance IS NULL THEN NULL
            ELSE p.tokens - lt.ledger_balance
       END                                  AS drift_tokens_vs_ledger,
       to_char(lt.last_tx_at AT TIME ZONE 'UTC',
               'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS last_tx_at
FROM public.profiles p
LEFT JOIN bucket_sums b ON b.user_id = p.id
LEFT JOIN last_tx     lt ON lt.user_id = p.id
WHERE
  -- Filter to drifted rows only.
  (
    p.tokens <> COALESCE(b.bucket_balance, 0)
    OR (lt.ledger_balance IS NOT NULL AND p.tokens <> lt.ledger_balance)
  )
  -- Optional time window on the last_tx side.
  AND (
    %(since_ts)s::timestamptz IS NULL
    OR lt.last_tx_at IS NULL
    OR lt.last_tx_at >= %(since_ts)s::timestamptz
  )
ORDER BY ABS(p.tokens - COALESCE(b.bucket_balance, 0)) DESC,
         p.created_at DESC
LIMIT %(limit)s;
"""


def _resolve_dsn(cli_dsn: str | None) -> str:
    if cli_dsn:
        return cli_dsn
    env_dsn = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if env_dsn:
        return env_dsn
    raise SystemExit(
        "ERROR: provide --dsn or set DATABASE_URL / SUPABASE_DB_URL"
    )


def reconcile(
    dsn: str,
    limit: int = 1000,
    since_hours: int | None = None,
) -> list[DriftRow]:
    if psycopg is None:
        raise SystemExit("ERROR: psycopg not installed (pip install psycopg[binary])")

    since_ts = None
    if since_hours is not None:
        since_ts = datetime.now(timezone.utc) - timedelta(hours=since_hours)

    rows: list[DriftRow] = []
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                RECONCILE_SQL,
                {"limit": limit, "since_ts": since_ts},
            )
            for r in cur.fetchall():
                rows.append(
                    DriftRow(
                        user_id=r[0],
                        email=r[1],
                        tokens=r[2],
                        bucket_balance=r[3],
                        ledger_balance=r[4],
                        drift_tokens_vs_bucket=r[5],
                        drift_tokens_vs_ledger=r[6],
                        last_tx_at=r[7],
                    )
                )
    return rows


def _format_human(rows: Iterable[DriftRow]) -> str:
    rows = list(rows)
    if not rows:
        return "ledger reconciliation: no drift detected\n"
    lines = ["ledger reconciliation: drift detected", "-" * 78]
    fmt = (
        "{user_id}  email={email}\n"
        "  tokens={tokens}  bucket_balance={bucket}  ledger_balance={ledger}\n"
        "  drift_tokens_vs_bucket={d1}  drift_tokens_vs_ledger={d2}\n"
        "  last_tx_at={last}\n"
    )
    for r in rows:
        lines.append(
            fmt.format(
                user_id=r.user_id,
                email=r.email or "(none)",
                tokens=r.tokens,
                bucket=r.bucket_balance,
                ledger="(no tx)" if r.ledger_balance is None else r.ledger_balance,
                d1=r.drift_tokens_vs_bucket,
                d2="(n/a)" if r.drift_tokens_vs_ledger is None else r.drift_tokens_vs_ledger,
                last=r.last_tx_at or "(none)",
            )
        )
    lines.append("-" * 78)
    lines.append(f"total drifted users: {len(rows)}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n", 1)[0])
    ap.add_argument("--dsn", help="Postgres DSN (else $DATABASE_URL)")
    ap.add_argument("--limit", type=int, default=1000, help="max rows to report")
    ap.add_argument("--since-hours", type=int, default=None,
                    help="only consider users with a tx in the last N hours")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--write-report", default=None, help="write report to path")
    args = ap.parse_args(argv)

    try:
        dsn = _resolve_dsn(args.dsn)
        rows = reconcile(dsn, limit=args.limit, since_hours=args.since_hours)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"reconcile_ledger ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        out = json.dumps(
            {
                "drifted_users": len(rows),
                "rows": [asdict(r) for r in rows],
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    else:
        out = _format_human(rows)

    if args.write_report:
        with open(args.write_report, "w") as f:
            f.write(out)
    else:
        print(out)

    return 2 if rows else 0


if __name__ == "__main__":
    sys.exit(main())
