#!/usr/bin/env bash
# run_contract_tests.sh [expect_red|expect_green]
#
# Runs all SQL contract tests against /tmp:55432/testdb.
# - expect_red: a test that exits 0 is a FAILURE (the bug is gone — but we
#   expected it to be present on baseline).
# - expect_green: a test that exits non-zero is a FAILURE (we expected the
#   fix to make it pass).

set -uo pipefail

PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb
export PGHOST PGPORT PGUSER PGDATABASE

MODE="${1:-expect_green}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# C-tests: RED on baseline, GREEN post-004.
# G-tests: GREEN on both baseline and post-004 (regression guards).
TESTS=( "$ROOT"/tests/contracts/C*.sql )
GUARDS=( "$ROOT"/tests/contracts/G*.sql )

pass=0; fail=0; pass_list=(); fail_list=()

for t in "${TESTS[@]}"; do
  name=$(basename "$t" .sql)
  out=$(psql -v ON_ERROR_STOP=1 -X -A -t -f "$t" 2>&1)
  rc=$?
  if [[ "$MODE" == "expect_red" ]]; then
    if [[ $rc -ne 0 ]]; then
      pass=$((pass+1)); pass_list+=("$name (RED-as-expected)")
    else
      fail=$((fail+1)); fail_list+=("$name (UNEXPECTEDLY GREEN on baseline)")
    fi
  else
    if [[ $rc -eq 0 ]]; then
      pass=$((pass+1)); pass_list+=("$name (GREEN-as-expected)")
    else
      fail=$((fail+1)); fail_list+=("$name (FAILED post-fix): $out")
    fi
  fi
done

echo "================================"
echo "MODE: $MODE"
echo "PASS: $pass"
echo "FAIL: $fail"
echo "================================"
for p in "${pass_list[@]}"; do echo "  pass: $p"; done
for f in "${fail_list[@]}"; do echo "  FAIL: $f"; done

# Regression guards always-green regardless of mode.
for t in "${GUARDS[@]}"; do
  name=$(basename "$t" .sql)
  out=$(psql -v ON_ERROR_STOP=1 -X -A -t -f "$t" 2>&1)
  rc=$?
  if [[ $rc -eq 0 ]]; then
    echo "  guard pass: $name"
  else
    echo "  GUARD FAIL: $name : $out"
    fail=$((fail+1))
  fi
done

if [[ $fail -gt 0 ]]; then exit 1; fi
exit 0
