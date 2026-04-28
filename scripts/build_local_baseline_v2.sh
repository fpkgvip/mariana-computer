#!/usr/bin/env bash
# Rebuild local testdb from the canonical schema baseline that CI uses.
#
# Phase D unified the local-dev and CI baselines: both now apply
# ``.github/scripts/ci_pg_bootstrap.sql`` (roles + pgcrypto extension)
# followed by ``.github/scripts/ci_full_baseline.sql`` (full pg_dump
# of the schema after every committed migration through 024).  This
# script drops and recreates the local ``testdb`` and applies the
# same two artefacts so ``pytest`` runs against the exact schema CI
# enforces.
#
# Refresh procedure when a new migration lands: see
# ``loop6_audit/PHASE_D_CI_REPORT.md`` "How to refresh the baseline".
#
# Required: a Postgres instance reachable via the env defaults below
# (the standard local-dev convention).  Override via env to point at
# a different cluster.

set -euo pipefail

PGHOST="${PGHOST:-/tmp}"
PGPORT="${PGPORT:-55432}"
PGUSER="${PGUSER:-postgres}"
PGDATABASE="${PGDATABASE:-testdb}"
export PGHOST PGPORT PGUSER PGDATABASE

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BOOTSTRAP="$ROOT/.github/scripts/ci_pg_bootstrap.sql"
BASELINE="$ROOT/.github/scripts/ci_full_baseline.sql"

if [[ ! -f "$BOOTSTRAP" ]]; then
  echo "Bootstrap SQL missing: $BOOTSTRAP" >&2
  exit 1
fi
if [[ ! -f "$BASELINE" ]]; then
  echo "Baseline SQL missing: $BASELINE" >&2
  exit 1
fi

echo "Resetting $PGDATABASE..."
psql -d postgres -v ON_ERROR_STOP=1 <<EOSQL
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
  WHERE datname='$PGDATABASE' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS $PGDATABASE;
CREATE DATABASE $PGDATABASE;
EOSQL

echo "Bootstrapping roles + extensions..."
psql -v ON_ERROR_STOP=1 -f "$BOOTSTRAP"

echo "Applying full schema baseline..."
psql -v ON_ERROR_STOP=1 -f "$BASELINE"

echo "Verifying critical RPCs are present..."
EXPECTED=(grant_credits refund_credits spend_credits expire_credits add_credits process_charge_reversal admin_set_credits)
for fn in "${EXPECTED[@]}"; do
  count=$(psql -tAc "SELECT COUNT(*) FROM pg_proc WHERE proname = '$fn' AND pronamespace = 'public'::regnamespace")
  if [[ "$count" != "1" ]]; then
    echo "FAIL: expected exactly 1 public.$fn() function, found $count" >&2
    exit 1
  fi
done

echo "Local baseline v2 ready (mirrors CI baseline)."
