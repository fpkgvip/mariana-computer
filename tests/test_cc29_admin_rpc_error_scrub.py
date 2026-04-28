"""CC-29 regression — admin RPC error responses must not echo Supabase body.

CC-29 (P3, post-CC-26 re-audit #44 Finding 3) found that multiple admin-only
endpoints in ``mariana/api.py`` echoed up to 200-400 chars of the Supabase
REST error body to the admin client, e.g.::

    detail=f"RPC {fn} failed: {body}"
    detail=f"List admin_tasks failed: {resp.text[:200]}"
    detail=f"Flush failed: {exc}"

These admin routes are gated by ``_require_admin``, so blast radius is limited
to admins, but PostgREST/Postgres bodies can carry table/column names, FK and
RLS policy names, and value snippets.  Same canonical fix as CC-22/24:
stash diagnostics in a structured ``admin_rpc_failed`` log; surface a stable
generic detail.

This module pins the fix at the source level:

  * No detail string in ``mariana/api.py`` interpolates Supabase response
    bodies (``resp.text``, ``body``, or ``str(exc)``) into ``f"... failed: ..."``
    patterns.
  * The canonical stable detail strings ``"admin RPC failed"`` and
    ``"admin operation failed"`` appear and are used at every CC-29 site.
  * The ``admin_rpc_failed`` log key appears at every CC-29 site (one per
    raise) and never logs the body via the user-facing detail.
"""

from __future__ import annotations

import re
from pathlib import Path

API_PY = Path(__file__).resolve().parent.parent / "mariana" / "api.py"
SOURCE = API_PY.read_text()


# ---------------------------------------------------------------------------
# (1) No detail strings carry the leaked patterns
# ---------------------------------------------------------------------------


def test_no_admin_rpc_detail_echoes_body():
    """Forbid the regressed patterns:

    * ``detail=f"RPC {fn} failed: {body}"``
    * ``detail=f"... failed: {resp.text[:NNN]}"``
    * ``detail=f"Flush failed: {exc}"``
    """
    forbidden = [
        re.compile(r'detail=f"RPC \{fn\} failed: \{body\}"'),
        re.compile(r'detail=f"[^"]*failed: \{resp\.text\[:\d+\]\}"'),
        re.compile(r'detail=f"Flush failed: \{exc\}"'),
    ]
    for pat in forbidden:
        m = pat.search(SOURCE)
        assert m is None, (
            f"forbidden detail pattern still in mariana/api.py: {pat.pattern}"
        )


# ---------------------------------------------------------------------------
# (2) Stable generic detail strings appear at the CC-29 sites
# ---------------------------------------------------------------------------


def test_admin_rpc_canonical_details_present():
    """Both canonical detail strings must appear at least once."""
    assert 'detail="admin RPC failed"' in SOURCE
    assert 'detail="admin operation failed"' in SOURCE


def test_admin_rpc_canonical_count_matches_sites():
    """At least 9 raise sites total were rewritten in CC-29.

    1 ``admin RPC failed`` (the central ``_admin_rpc`` helper) plus 8
    ``admin operation failed`` raises across admin_tasks (4), feature_flags
    (3), and the redis flush handler (1).
    """
    n_central = SOURCE.count('detail="admin RPC failed"')
    n_generic = SOURCE.count('detail="admin operation failed"')
    assert n_central >= 1
    assert n_generic >= 8, (
        f"expected at least 8 generic admin operation raises, found {n_generic}"
    )


# ---------------------------------------------------------------------------
# (3) The structured logger key is paired with each CC-29 raise
# ---------------------------------------------------------------------------


def test_admin_rpc_failed_log_key_present():
    """Every CC-29 raise must be preceded by ``logger.error("admin_rpc_failed", ...)``.

    We assert the log key appears at least as many times as the number of
    raised stable details.
    """
    raises = SOURCE.count('detail="admin RPC failed"') + SOURCE.count(
        'detail="admin operation failed"'
    )
    n_log = SOURCE.count('"admin_rpc_failed"')
    assert n_log >= raises, (
        f"expected at least {raises} admin_rpc_failed log keys, found {n_log}"
    )
