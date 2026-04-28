#!/usr/bin/env bash
# Loop 6 zero-bug convergence requires that loop6_audit/REGISTRY.md has
# zero rows in the **OPEN** state.  Every audit cycle that finds a
# defect MUST land a fix that flips the row to **FIXED** before the
# branch can merge.  This check enforces that gate.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REGISTRY="$ROOT/loop6_audit/REGISTRY.md"

if [[ ! -f "$REGISTRY" ]]; then
  echo "REGISTRY.md not found at $REGISTRY" >&2
  exit 1
fi

# A row in the open state has the literal token ``| **OPEN** |`` (the
# table column delimiter on either side of the bold OPEN marker).
open_rows=$(grep -E '\| \*\*OPEN\*\* \|' "$REGISTRY" || true)

if [[ -n "$open_rows" ]]; then
  echo "FAIL: REGISTRY.md has unfixed OPEN findings:" >&2
  echo "$open_rows" >&2
  exit 1
fi

echo "REGISTRY.md: zero OPEN findings — convergence invariant holds."
