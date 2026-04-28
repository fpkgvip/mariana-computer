#!/usr/bin/env bash
# Y-02 convention: every forward migration ``NNN_*.sql`` (or ``NNNb_*.sql``)
# in ``frontend/supabase/migrations/`` must have a paired
# ``NNN_revert.sql`` (or ``NNNb_revert.sql``).  Migrations 001-003 are
# foundational (initial schema, credit ledger, vault) and predate the
# revert convention; they are exempt.
#
# Exits non-zero when a forward migration with revision >= 004 lacks a
# revert pair.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MIGRATIONS_DIR="$ROOT/frontend/supabase/migrations"

EXEMPT_PREFIXES=("001_" "002_" "003_")

missing=()
for forward in "$MIGRATIONS_DIR"/*.sql; do
  base="$(basename "$forward")"
  case "$base" in
    *_revert.sql) continue ;;
  esac

  exempt=false
  for prefix in "${EXEMPT_PREFIXES[@]}"; do
    if [[ "$base" == ${prefix}* ]]; then
      exempt=true
      break
    fi
  done
  if $exempt; then
    continue
  fi

  # Strip suffix after the revision token to derive the revert glob.
  # Accepts NNN_*.sql or NNNb_*.sql; revert filename can be either
  # NNN_revert.sql or NNN_<name>_revert.sql (both conventions exist
  # in the repo: 010_f05_revert.sql, 009_f03_refund_debt_revert.sql).
  rev_token="$(echo "$base" | sed -E 's/^([0-9]+[a-z]?)_.*/\1/')"
  found=false
  for candidate in "$MIGRATIONS_DIR/${rev_token}_revert.sql" "$MIGRATIONS_DIR/${rev_token}_"*_revert.sql; do
    if [[ -f "$candidate" ]]; then
      found=true
      break
    fi
  done
  if ! $found; then
    missing+=("$base (expected pair: ${rev_token}_revert.sql or ${rev_token}_*_revert.sql)")
  fi
done

if (( ${#missing[@]} > 0 )); then
  echo "FAIL: forward migrations missing revert pair (Y-02 convention):" >&2
  for m in "${missing[@]}"; do
    echo "  $m" >&2
  done
  exit 1
fi

echo "All forward migrations (>= 004) have paired revert scripts."
