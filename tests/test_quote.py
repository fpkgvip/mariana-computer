"""Unit tests for the pre-flight quote estimator."""

import pytest

from mariana.billing.quote import estimate_quote


def test_basic_lite():
    q = estimate_quote(prompt="hi", tier="lite")
    assert q.tier == "lite"
    assert 1 <= q.credits_min <= q.credits_max
    assert q.eta_seconds_min <= q.eta_seconds_max


def test_complexity_increases_with_length():
    short = estimate_quote(prompt="hi", tier="standard")
    long = estimate_quote(
        prompt="Build a full-stack SaaS app with auth, billing, " * 50,
        tier="standard",
    )
    assert long.credits_max > short.credits_max
    assert long.complexity_score > short.complexity_score


def test_tier_ordering():
    p = "Build a full-stack web app"
    lite = estimate_quote(prompt=p, tier="lite")
    standard = estimate_quote(prompt=p, tier="standard")
    max_ = estimate_quote(prompt=p, tier="max")
    assert lite.credits_max < standard.credits_max < max_.credits_max


def test_ceiling_caps_max():
    q = estimate_quote(prompt="Build something complex", tier="max", max_credits=100)
    assert q.credits_max <= 100
    assert q.credits_min <= q.credits_max


def test_ceiling_zero_is_ignored():
    q = estimate_quote(prompt="Build something", tier="standard", max_credits=0)
    assert q.credits_max > 0  # zero ceiling is treated as "no ceiling"


def test_invalid_tier_raises():
    with pytest.raises(ValueError):
        estimate_quote(prompt="x", tier="ultra")  # type: ignore[arg-type]


def test_quote_serializable():
    q = estimate_quote(prompt="Test", tier="standard")
    d = q.to_dict()
    assert d["tier"] == "standard"
    assert isinstance(d["credits_min"], int)
    assert isinstance(d["credits_max"], int)
    assert isinstance(d["complexity_score"], float)


def test_credits_are_integers():
    for tier in ("lite", "standard", "max"):
        q = estimate_quote(prompt="x" * 1000, tier=tier)  # type: ignore[arg-type]
        assert isinstance(q.credits_min, int)
        assert isinstance(q.credits_max, int)
        assert q.credits_min >= 1
