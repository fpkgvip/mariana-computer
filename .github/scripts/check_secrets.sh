#!/usr/bin/env bash
# Lightweight secret scan: refuse to merge if any of the following
# high-confidence secret prefixes are present in tracked files.
# Designed for grep speed — not a substitute for trufflehog or gitleaks
# but catches the obvious cases.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

# Patterns intentionally narrow to high-precision Stripe / AWS / Slack
# / GitHub tokens.  Test fixtures use clearly-sentinel values
# (``sk_test_xxx``, ``whsec_xxx``) which are excluded by the trailing
# alphanumeric run requirement.
PATTERNS=(
  'sk_live_[A-Za-z0-9]{16,}'
  'pk_live_[A-Za-z0-9]{16,}'
  'whsec_[A-Za-z0-9]{32,}'
  'AKIA[0-9A-Z]{16}'
  'xox[pboa]-[A-Za-z0-9-]{20,}'
  'gh[pousr]_[A-Za-z0-9]{30,}'
)

# Ignore paths that legitimately mention these prefixes (audit reports,
# fixture comments, and the .github/scripts directory which contains
# this scanner itself).
EXCLUDES=(
  ':!loop6_audit/'
  ':!.github/scripts/'
  ':!tests/'
  ':!frontend/package-lock.json'
  ':!frontend/node_modules/'
  ':!frontend/src/test/'
)

found_any=0
for pattern in "${PATTERNS[@]}"; do
  matches=$(git grep -nE "$pattern" -- "${EXCLUDES[@]}" 2>/dev/null || true)
  if [[ -n "$matches" ]]; then
    echo "FAIL: secret-like token matching /$pattern/:" >&2
    echo "$matches" >&2
    found_any=1
  fi
done

if (( found_any > 0 )); then
  exit 1
fi

echo "Secret scan: no high-confidence tokens found in tracked files."
