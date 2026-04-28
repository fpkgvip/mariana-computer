#!/usr/bin/env bash
# Apply the CI Postgres baseline against the service container.
#
# We do NOT iterate over individual ``frontend/supabase/migrations/``
# files because those migrations target a Supabase project with a
# pre-existing rich auth schema and platform-managed roles.  A clean
# postgres:16 service container does not have those, and the migrations
# reference profile columns added through Supabase's own UI (admin
# tooling rather than committed migrations).  Instead we ship a full
# schema dump (``ci_full_baseline.sql``) that mirrors the local-dev
# baseline produced by ``scripts/build_local_baseline_v2.sh`` against
# NestD live.  Refresh procedure documented in PHASE_D_CI_REPORT.md.
#
# Required env: PGHOST, PGPORT, PGUSER, PGDATABASE, PGPASSWORD.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

echo "Bootstrapping roles + extensions..."
psql -v ON_ERROR_STOP=1 -f "$ROOT/.github/scripts/ci_pg_bootstrap.sql"

echo "Applying full schema baseline..."
psql -v ON_ERROR_STOP=1 -f "$ROOT/.github/scripts/ci_full_baseline.sql"

echo "Verifying critical RPCs are present..."
EXPECTED=(grant_credits refund_credits spend_credits expire_credits add_credits process_charge_reversal admin_set_credits)
for fn in "${EXPECTED[@]}"; do
  count=$(psql -tAc "SELECT COUNT(*) FROM pg_proc WHERE proname = '$fn' AND pronamespace = 'public'::regnamespace")
  if [[ "$count" != "1" ]]; then
    echo "FAIL: expected exactly 1 public.$fn() function, found $count" >&2
    exit 1
  fi
done

echo "Schema baseline + RPC inventory verified."
