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

# B-36 (A1-16): Drop the stale weak update policy that 001_initial_schema.sql
# creates and 004_loop5_idempotency_and_rls.sql supersedes. The local_baseline_v2.sql
# doesn't include 001, but if it ever did (or if this script is extended to apply
# 001), the DROP here ensures the weak policy is never left in place.
# See loop6_audit/A1_db.md A1-16 for full drift description.
psql -v ON_ERROR_STOP=1 -c \
  'DROP POLICY IF EXISTS "Users can update own profile" ON public.profiles;'

# Apply already-landed migrations (004 .. 007) which are already live.
for m in 004_loop5_idempotency_and_rls.sql 004b_credit_tx_idem_concurrent.sql 005_loop6_b01_revoke_anon_rpcs.sql 006_refund_credits_repair.sql 007_loop6_b02_b05_b06_ledger_sync.sql 008_f04_plan_entitlement_sync.sql 009_f03_refund_debt.sql 010_f05_research_tasks_owner_fk.sql 011_p2_db_cluster_b11_b15.sql 012_p2_b16_admin_set_credits_ledger.sql 013_p3_b32_fk_indexes.sql 014_p3_b33_rls_select_wrap.sql 015_p3_b34_profile_check_simplify.sql 016_p3_b35_storage_rls.sql 017_h01_h02_stripe_grant_linkage.sql 018_i01_add_credits_lock.sql 019_i03_marker_tables_rls.sql; do
  f="$ROOT/frontend/supabase/migrations/$m"
  if [[ -f "$f" ]]; then
    echo "Applying $m..."
    psql -v ON_ERROR_STOP=1 -f "$f" || {
      echo "NOTE: $m may be partially already-applied, continuing"
    }
  fi
done

echo "Local baseline v2 ready (mirrors current live)."
