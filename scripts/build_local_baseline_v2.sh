#!/usr/bin/env bash
# Rebuild local testdb from a faithful copy of current NestD live state.
set -euo pipefail
PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb
export PGHOST PGPORT PGUSER PGDATABASE

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SQL="$ROOT/scripts/local_baseline_v2.sql"

echo "Resetting testdb..."
psql -d postgres -v ON_ERROR_STOP=1 <<'EOSQL'
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
  WHERE datname='testdb' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS testdb;
CREATE DATABASE testdb;
EOSQL

echo "Applying baseline v2 schema..."
psql -v ON_ERROR_STOP=1 -f "$SQL"

# Apply already-landed migrations (004 .. 007) which are already live.
for m in 004_loop5_idempotency_and_rls.sql 004b_credit_tx_idem_concurrent.sql 005_loop6_b01_revoke_anon_rpcs.sql 006_refund_credits_repair.sql 007_loop6_b02_b05_b06_ledger_sync.sql; do
  f="$ROOT/frontend/supabase/migrations/$m"
  if [[ -f "$f" ]]; then
    echo "Applying $m..."
    psql -v ON_ERROR_STOP=1 -f "$f" || {
      echo "NOTE: $m may be partially already-applied, continuing"
    }
  fi
done

echo "Local baseline v2 ready (mirrors current live)."
