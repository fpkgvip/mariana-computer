#!/usr/bin/env bash
# build_local_baseline.sh
#
# Builds a local Postgres replica matching the live NestD project schema
# captured in loop5_research/. This is the "baseline" against which 004
# is RED-verified.
#
# Local Postgres expected on socket /tmp:55432, db=testdb, user=postgres.

set -euo pipefail

PGHOST=/tmp
PGPORT=55432
PGUSER=postgres
PGDATABASE=testdb
export PGHOST PGPORT PGUSER PGDATABASE

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SQL="$ROOT/scripts/local_baseline.sql"

if [[ ! -f "$SQL" ]]; then
  echo "ERROR: $SQL not found" >&2
  exit 1
fi

echo "Resetting testdb..."
psql -d postgres -v ON_ERROR_STOP=1 <<'EOSQL'
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
  WHERE datname='testdb' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS testdb;
CREATE DATABASE testdb;
EOSQL

echo "Applying baseline schema..."
psql -v ON_ERROR_STOP=1 -f "$SQL"
echo "Local baseline ready."
