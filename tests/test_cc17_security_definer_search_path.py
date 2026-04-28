# tests/test_cc17_security_definer_search_path.py
"""
CC-17 regression: every SECURITY DEFINER function defined in a Supabase migration
MUST pin search_path inside the function definition.

A SECURITY DEFINER function with an unpinned search_path is vulnerable to
search-path hijack: an attacker who can create objects in any schema on the
function's resolution list can shadow `public.profiles` (etc.) with a malicious
object and run arbitrary code as the function's owner (typically `postgres`).

Standard Supabase practice — and the form already used by the live database
(see .github/scripts/ci_full_baseline.sql, which is a pg_dump) — is:

    SET search_path = public, pg_temp        -- forward migrations
    SET search_path TO 'public', 'pg_temp'   -- pg_dump output

Either spelling is accepted.

This test runs in pure Python, no DB required. It parses every .sql file under
frontend/supabase/migrations/ and the CI baseline, finds each
CREATE [OR REPLACE] FUNCTION ... SECURITY DEFINER block, and asserts that the
block contains a SET search_path clause anywhere between CREATE FUNCTION and
the closing ';' that terminates the statement.

The scope deliberately includes both forward migrations AND *_revert.sql
scripts, because a rollback or a fresh-baseline rebuild that runs revert
scripts can resurrect privileged functions with unpinned search_path — which
is the exact CC-17 attack vector.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "frontend" / "supabase" / "migrations"
CI_BASELINE = REPO_ROOT / ".github" / "scripts" / "ci_full_baseline.sql"


def _find_function_blocks(text: str):
    """
    Yield (start_line_1based, end_line_1based, block_text) for every
    CREATE [OR REPLACE] FUNCTION statement in *text*.

    A statement runs from the CREATE FUNCTION line until the ';' that
    terminates the post-body attribute clause. This means trailing
    `LANGUAGE plpgsql SECURITY DEFINER SET search_path = ...;` clauses
    AFTER the closing `$$` are still part of the block.
    """
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        if re.match(r"\s*CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\b", lines[i], re.IGNORECASE):
            start = i
            block = [lines[i]]
            i += 1
            tag = None
            body_closed = False
            while i < n:
                cur = lines[i]
                block.append(cur)
                if tag is None:
                    m = re.search(r"\bAS\s+(\$[A-Za-z_]*\$)", cur)
                    if m:
                        tag = m.group(1)
                        rest = cur[m.end():]
                        if tag in rest:
                            body_closed = True
                            if re.search(r";\s*$", cur):
                                i += 1
                                break
                    i += 1
                    continue
                if not body_closed:
                    if tag in cur:
                        body_closed = True
                        if re.search(r";\s*$", cur):
                            i += 1
                            break
                    i += 1
                    continue
                # body closed, scanning for terminator
                if re.search(r";\s*$", cur):
                    i += 1
                    break
                i += 1
            yield (start + 1, i, "\n".join(block))
        else:
            i += 1


SEC_DEFINER_RE = re.compile(r"SECURITY\s+DEFINER", re.IGNORECASE)
SEARCH_PATH_RE = re.compile(r"SET\s+search_path\s*(=|TO)\s+", re.IGNORECASE)
NAME_RE = re.compile(
    r"FUNCTION\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w\.]*)", re.IGNORECASE
)


def _collect_sql_files():
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if CI_BASELINE.exists():
        files.append(CI_BASELINE)
    return files


def _iter_security_definer_blocks():
    for fp in _collect_sql_files():
        text = fp.read_text()
        for start, end, block in _find_function_blocks(text):
            if SEC_DEFINER_RE.search(block):
                m = NAME_RE.search(block)
                name = m.group(1) if m else "?"
                yield fp, start, end, name, block


def test_every_security_definer_function_pins_search_path():
    """Every SECURITY DEFINER function in every SQL migration must SET search_path."""
    gaps = []
    total = 0
    for fp, start, end, name, block in _iter_security_definer_blocks():
        total += 1
        if not SEARCH_PATH_RE.search(block):
            gaps.append(
                f"{fp.relative_to(REPO_ROOT)}:{start}-{end}  {name}"
            )

    # Sanity: we should be finding plenty of SECURITY DEFINER functions.
    # If this drops to 0 the parser is silently broken.
    assert total >= 20, (
        f"Parser regression: only found {total} SECURITY DEFINER blocks "
        f"(expected >= 20)."
    )

    if gaps:
        msg = (
            "CC-17 regression — SECURITY DEFINER functions missing "
            "`SET search_path = ...`:\n  " + "\n  ".join(gaps)
        )
        pytest.fail(msg)


def test_parser_finds_known_function():
    """Smoke-test: parser must locate at least one well-known function."""
    names = {name for _, _, _, name, _ in _iter_security_definer_blocks()}
    assert "public.handle_new_user" in names, (
        f"Parser smoke-test failed; got names={sorted(names)[:10]}..."
    )
