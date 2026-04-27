"""B-23 regression suite: branch_manager uses the real config import.

After the B-23 fix, branch_manager._cfg_val must successfully call load_config()
and return values from AppConfig rather than always falling back to hardcoded
defaults due to ImportError on the non-existent get_config function.

Test IDs:
  1. test_cfg_val_reads_score_kill_threshold_from_env
  2. test_cfg_val_reads_score_deepen_threshold_from_env
  3. test_cfg_val_reads_budget_hard_cap_from_env
  4. test_load_config_thresholds_updates_module_constants
  5. test_cfg_val_falls_back_when_attr_absent
  6. test_score_branch_uses_env_kill_threshold
"""

from __future__ import annotations

import importlib
import os
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Test 1: _cfg_val reads SCORE_KILL_THRESHOLD from environment
# ---------------------------------------------------------------------------

def test_cfg_val_reads_score_kill_threshold_from_env(monkeypatch):
    """B-23: _cfg_val must return env-set SCORE_KILL_THRESHOLD, not the hardcoded default."""
    monkeypatch.setenv("SCORE_KILL_THRESHOLD", "0.25")

    # Force reimport to pick up monkeypatched env
    import mariana.orchestrator.branch_manager as bm
    importlib.reload(bm)

    result = bm._cfg_val("SCORE_KILL_THRESHOLD", 0.4)
    assert result == pytest.approx(0.25), (
        f"B-23: _cfg_val should return env value 0.25 but got {result}; "
        "get_config import was probably failing silently"
    )


# ---------------------------------------------------------------------------
# Test 2: _cfg_val reads SCORE_DEEPEN_THRESHOLD from environment
# ---------------------------------------------------------------------------

def test_cfg_val_reads_score_deepen_threshold_from_env(monkeypatch):
    """B-23: _cfg_val must return env-set SCORE_DEEPEN_THRESHOLD, not 0.7."""
    monkeypatch.setenv("SCORE_DEEPEN_THRESHOLD", "0.65")

    import mariana.orchestrator.branch_manager as bm
    importlib.reload(bm)

    result = bm._cfg_val("SCORE_DEEPEN_THRESHOLD", 0.7)
    assert result == pytest.approx(0.65), (
        f"B-23: _cfg_val should return env value 0.65 but got {result}"
    )


# ---------------------------------------------------------------------------
# Test 3: _cfg_val reads BUDGET_BRANCH_HARD_CAP from environment
# ---------------------------------------------------------------------------

def test_cfg_val_reads_budget_hard_cap_from_env(monkeypatch):
    """B-23: _cfg_val must return env-set BUDGET_BRANCH_HARD_CAP, not 75.0."""
    monkeypatch.setenv("BUDGET_BRANCH_HARD_CAP", "99.0")

    import mariana.orchestrator.branch_manager as bm
    importlib.reload(bm)

    result = bm._cfg_val("BUDGET_BRANCH_HARD_CAP", 75.0)
    assert result == pytest.approx(99.0), (
        f"B-23: _cfg_val should return 99.0 from env but got {result}"
    )


# ---------------------------------------------------------------------------
# Test 4: _load_config_thresholds propagates env values to module constants
# ---------------------------------------------------------------------------

def test_load_config_thresholds_updates_module_constants(monkeypatch):
    """B-23: _load_config_thresholds must update SCORE_KILL_THRESHOLD from env."""
    monkeypatch.setenv("SCORE_KILL_THRESHOLD", "0.33")
    monkeypatch.setenv("BUDGET_BRANCH_HARD_CAP", "88.0")

    import mariana.orchestrator.branch_manager as bm
    importlib.reload(bm)

    bm._load_config_thresholds()

    assert bm.SCORE_KILL_THRESHOLD == pytest.approx(0.33), (
        f"B-23: SCORE_KILL_THRESHOLD should be 0.33 after _load_config_thresholds "
        f"but was {bm.SCORE_KILL_THRESHOLD}"
    )
    assert bm.BUDGET_HARD_CAP == pytest.approx(88.0), (
        f"B-23: BUDGET_HARD_CAP should be 88.0 after _load_config_thresholds "
        f"but was {bm.BUDGET_HARD_CAP}"
    )


# ---------------------------------------------------------------------------
# Test 5: _cfg_val falls back to default when attribute is absent in AppConfig
# ---------------------------------------------------------------------------

def test_cfg_val_falls_back_when_attr_absent(monkeypatch):
    """B-23: _cfg_val returns default when the AppConfig attribute does not exist."""
    import mariana.orchestrator.branch_manager as bm
    importlib.reload(bm)

    result = bm._cfg_val("NONEXISTENT_THRESHOLD_XYZ", 99.9)
    assert result == pytest.approx(99.9), (
        f"_cfg_val should fall back to default 99.9 for unknown attr but got {result}"
    )


# ---------------------------------------------------------------------------
# Test 6: all six threshold module constants are overridden via env
# ---------------------------------------------------------------------------

def test_all_six_constants_overridden_via_env(monkeypatch):
    """B-23: all six module-level constants must reflect env overrides after _load_config_thresholds."""
    monkeypatch.setenv("SCORE_KILL_THRESHOLD", "0.11")
    monkeypatch.setenv("SCORE_DEEPEN_THRESHOLD", "0.72")
    monkeypatch.setenv("SCORE_TRIBUNAL_THRESHOLD", "0.83")
    monkeypatch.setenv("BUDGET_BRANCH_INITIAL", "7.77")
    monkeypatch.setenv("BUDGET_BRANCH_GRANT_SCORE7", "22.0")
    monkeypatch.setenv("BUDGET_BRANCH_GRANT_SCORE8", "55.0")
    monkeypatch.setenv("BUDGET_BRANCH_HARD_CAP", "80.0")

    import mariana.orchestrator.branch_manager as bm
    importlib.reload(bm)

    bm._load_config_thresholds()

    # Each of the six constants should now reflect env, not hardcoded defaults
    assert bm.SCORE_KILL_THRESHOLD == pytest.approx(0.11), (
        f"B-23: SCORE_KILL_THRESHOLD expected 0.11, got {bm.SCORE_KILL_THRESHOLD}"
    )
    assert bm.SCORE_DEEPEN_THRESHOLD == pytest.approx(0.72), (
        f"B-23: SCORE_DEEPEN_THRESHOLD expected 0.72, got {bm.SCORE_DEEPEN_THRESHOLD}"
    )
    assert bm.SCORE_TRIBUNAL_THRESHOLD == pytest.approx(0.83), (
        f"B-23: SCORE_TRIBUNAL_THRESHOLD expected 0.83, got {bm.SCORE_TRIBUNAL_THRESHOLD}"
    )
    assert bm.BUDGET_INITIAL == pytest.approx(7.77), (
        f"B-23: BUDGET_INITIAL expected 7.77, got {bm.BUDGET_INITIAL}"
    )
    assert bm.BUDGET_GRANT_SCORE7 == pytest.approx(22.0), (
        f"B-23: BUDGET_GRANT_SCORE7 expected 22.0, got {bm.BUDGET_GRANT_SCORE7}"
    )
    assert bm.BUDGET_GRANT_SCORE8 == pytest.approx(55.0), (
        f"B-23: BUDGET_GRANT_SCORE8 expected 55.0, got {bm.BUDGET_GRANT_SCORE8}"
    )
