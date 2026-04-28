"""CC-32 regression — file-upload 400 details must not echo the filename.

CC-32 (P4, post-CC-26 re-audit #44 Finding 6) found six call sites in
``mariana/api.py`` (lines 4880, 4884, 4898, 5025, 5028, 5042 in the audited
revision) that interpolated the user-supplied ``filename!r`` / ``safe_name!r``
into the 400 detail string of the file-upload handlers.  This is the strict
letter of the CC-22/24 invariant ("don't interpolate user data into
user-facing HTTPException details").  Practical risk is bounded \u2014 the user
supplies the value and is the only recipient \u2014 but the same canonical fix
applies: log the raw filename to the structured operator log, surface a
generic stable detail.

This module pins the fix at the source level:

  * No ``detail=f"... {filename!r}"`` or ``detail=f"... {safe_name!r}"``
    pattern remains in ``mariana/api.py``.
  * The canonical stable details ``"invalid filename"`` and
    ``"symlinks are not allowed"`` are present.
  * Each rejection is paired with a ``logger.info("filename_rejected", ...)``
    structured log entry (the diagnostic preserved for ops).
"""

from __future__ import annotations

import re
from pathlib import Path

API_PY = Path(__file__).resolve().parent.parent / "mariana" / "api.py"
SOURCE = API_PY.read_text()


# ---------------------------------------------------------------------------
# (1) No detail strings echo the filename
# ---------------------------------------------------------------------------


def test_no_detail_echoes_filename():
    """Forbid every regressed pattern.

    The pattern ``detail=f"... {filename!r}"`` and the safe_name variant
    must never reappear.
    """
    forbidden = re.compile(r'detail=f"[^"]*\{(?:filename|safe_name)(?:!r)?\}[^"]*"')
    matches = forbidden.findall(SOURCE)
    assert matches == [], (
        "CC-32 regression \u2014 detail strings still echo filename/safe_name: "
        + repr(matches)
    )


# ---------------------------------------------------------------------------
# (2) Canonical stable details present
# ---------------------------------------------------------------------------


def test_canonical_stable_details_present():
    """The canonical generic details from CC-32 must appear."""
    assert 'detail="invalid filename"' in SOURCE
    assert 'detail="symlinks are not allowed"' in SOURCE


def test_invalid_filename_used_at_each_rejection():
    """At least 4 ``invalid filename`` raises remain after the fix.

    Two sites per upload handler (invalid_shape + path_escape), two upload
    handlers (the investigation upload + the pending upload).
    """
    n = SOURCE.count('detail="invalid filename"')
    assert n >= 4, f"expected at least 4 invalid-filename raises, found {n}"


# ---------------------------------------------------------------------------
# (3) ``filename_rejected`` log key is paired with each raise
# ---------------------------------------------------------------------------


def test_filename_rejected_log_key_paired_with_raises():
    """Every CC-32 raise must be preceded by a ``filename_rejected`` log entry.

    We assert the log key appears at least as many times as the total of
    the canonical stable raises, so each rejection has a structured log.
    """
    invalid = SOURCE.count('detail="invalid filename"')
    symlink = SOURCE.count('detail="symlinks are not allowed"')
    too_large = SOURCE.count('detail=f"File exceeds')
    total_raises = invalid + symlink + too_large
    n_log = SOURCE.count('"filename_rejected"')
    assert n_log >= total_raises, (
        f"expected at least {total_raises} filename_rejected log entries, found {n_log}"
    )
