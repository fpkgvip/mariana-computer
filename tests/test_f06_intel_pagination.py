"""F-06 regression suite: intelligence endpoint pagination.

Phase E re-audit found that /api/intelligence/{task_id}/claims,
/source-scores, /contradictions, /hypotheses/rankings, and /perspectives
returned all rows with no LIMIT, risking OOM and slow responses for large tasks.

Fix: all five routes now accept ``limit`` (default 100, max 1000) and
``cursor`` query parameters; the helper functions in
mariana/orchestrator/intelligence/*.py also enforce the cap.

Tests focus on helper-level behaviour (no live DB or lifespan required) plus
FastAPI route-layer tests using dependency_overrides.

Inventory:
  test_default_limit_caps_response        — omit limit; helper returns ≤ 100.
  test_explicit_limit_respected           — limit=50 returns exactly 50.
  test_limit_above_cap_clamped_to_1000    — limit=5000 is clamped to 1000.
  test_cursor_pagination_returns_next_page — fetch first page, use next_cursor.
  test_helper_max_limit_constants         — all helper modules export 1000 cap.
  test_envelope_shape_claims              — envelope has items/next_cursor/limit.
  test_envelope_shape_source_scores       — ditto.
  test_envelope_shape_contradictions      — ditto.
  test_envelope_shape_hypotheses          — ditto.
  test_envelope_shape_perspectives        — ditto.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Inject env vars BEFORE any mariana imports so that load_config does not
# raise RuntimeError about missing POSTGRES_PASSWORD.
# ---------------------------------------------------------------------------
os.environ.setdefault("POSTGRES_DSN", "postgresql://fake@localhost/fake")
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "fake-anon-key")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")
os.environ.setdefault("STREAM_TOKEN_SECRET", "fake-stream-secret-f06")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_fake")
os.environ.setdefault("STRIPE_PUBLISHABLE_KEY", "pk_test_fake")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_fake")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(offset_seconds: int = 0) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _make_claim(i: int) -> dict:
    ts = _ts(i)
    return {
        "id": str(uuid.uuid4()),
        "task_id": "task-123",
        "subject": f"Subject {i}",
        "predicate": "is",
        "object": f"Object {i}",
        "claim_text": f"Claim text {i}",
        "confidence": 0.8,
        "corroboration_count": 0,
        "source_ids": [],
        "contradiction_ids": [],
        "is_resolved": False,
        "resolution_note": None,
        "credibility_score": None,
        "finding_id": None,
        "hypothesis_id": None,
        "finding_content": None,
        "hypothesis_statement": None,
        "temporal_start": None,
        "temporal_end": None,
        "temporal_type": "point",
        "created_at": ts,
    }


def _make_source_score(i: int) -> dict:
    ts = _ts(i)
    return {
        "id": str(uuid.uuid4()),
        "source_id": str(uuid.uuid4()),
        "task_id": "task-123",
        "domain": f"example{i}.com",
        "credibility": 0.7,
        "relevance": 0.6,
        "recency": 0.5,
        "composite_score": 0.65,
        "domain_authority": "medium",
        "publication_type": "news",
        "cross_ref_density": 2,
        "scoring_rationale": None,
        "url": f"https://example{i}.com",
        "title": f"Source {i}",
        "created_at": ts,
    }


def _make_contradiction(i: int) -> dict:
    ts = _ts(i)
    return {
        "id": str(uuid.uuid4()),
        "task_id": "task-123",
        "claim_a_id": str(uuid.uuid4()),
        "claim_b_id": str(uuid.uuid4()),
        "contradiction_type": "direct",
        "severity": 0.6,
        "resolution_status": "unresolved",
        "resolution_source_id": None,
        "resolution_note": None,
        "claim_a_text": f"Claim A {i}",
        "claim_b_text": f"Claim B {i}",
        "claim_a_confidence": 0.8,
        "claim_b_confidence": 0.7,
        "subject": f"Subject {i}",
        "created_at": ts,
    }


def _make_perspective(i: int) -> dict:
    ts = _ts(i)
    return {
        "id": str(uuid.uuid4()),
        "task_id": "task-123",
        "perspective": "bull",
        "synthesis_text": f"Perspective text {i}",
        "confidence": 0.7,
        "key_arguments": [],
        "cited_claim_ids": [],
        "created_at": ts,
    }


# ---------------------------------------------------------------------------
# Helper-level unit tests (no live DB, no lifespan)
# ---------------------------------------------------------------------------


class _FakeDB:
    """Minimal asyncpg-compatible fake that slices items per limit."""

    def __init__(self, items: list):
        self._items = items

    async def fetch(self, query: str, *args):
        if not args:
            return self._items
        limit_val = args[-1]
        if not isinstance(limit_val, int):
            return self._items

        # Detect cursor path: args = (task_id, cursor_ts, cursor_id, limit).
        if len(args) >= 4:
            cursor_id = args[2]
            pivot = next(
                (i for i, c in enumerate(self._items)
                 if isinstance(c, dict) and c.get("id") == cursor_id),
                -1,
            )
            return self._items[pivot + 1: pivot + 1 + limit_val]
        return self._items[:limit_val]


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_default_limit_caps_response():
    """Without a limit param the helper returns ≤ 100 items even if there are 200."""
    from mariana.orchestrator.intelligence.evidence_ledger import get_evidence_ledger

    all_claims = [_make_claim(i) for i in range(200)]
    db = _FakeDB(all_claims)
    result = _run(get_evidence_ledger("task-123", db))
    assert len(result) <= 100, f"Default cap exceeded: {len(result)}"


def test_explicit_limit_respected():
    """limit=50 returns exactly 50."""
    from mariana.orchestrator.intelligence.evidence_ledger import get_evidence_ledger

    all_claims = [_make_claim(i) for i in range(200)]
    db = _FakeDB(all_claims)
    result = _run(get_evidence_ledger("task-123", db, limit=50))
    assert len(result) == 50


def test_limit_above_cap_clamped_to_1000():
    """Passing limit=5000 is silently clamped to 1000 by the helper."""
    from mariana.orchestrator.intelligence.evidence_ledger import (
        get_evidence_ledger,
        _INTEL_MAX_LIMIT,
    )

    assert _INTEL_MAX_LIMIT == 1000

    all_claims = [_make_claim(i) for i in range(2000)]
    received_limits: list[int] = []

    class _CapCheckDB:
        async def fetch(self, query: str, *args):
            limit_val = args[-1] if args else 100
            if isinstance(limit_val, int):
                received_limits.append(limit_val)
                assert limit_val <= _INTEL_MAX_LIMIT, (
                    f"Helper sent limit={limit_val} > cap={_INTEL_MAX_LIMIT}"
                )
            return all_claims[: min(
                limit_val if isinstance(limit_val, int) else 100,
                _INTEL_MAX_LIMIT
            )]

    _run(get_evidence_ledger("task-123", _CapCheckDB(), limit=5000))
    assert received_limits, "fetch was never called"
    assert all(lim <= _INTEL_MAX_LIMIT for lim in received_limits)


def test_cursor_pagination_returns_next_page():
    """Using next_cursor from page 1 returns a non-overlapping page 2."""
    from mariana.orchestrator.intelligence.evidence_ledger import get_evidence_ledger

    PAGE_SIZE = 5
    all_claims = [_make_claim(i) for i in range(20)]
    db = _FakeDB(all_claims)

    page1 = _run(get_evidence_ledger("task-123", db, limit=PAGE_SIZE))
    assert len(page1) == PAGE_SIZE

    last = page1[-1]
    cursor = f"{last['created_at']}|{last['id']}"

    page2 = _run(get_evidence_ledger("task-123", db, limit=PAGE_SIZE, cursor=cursor))
    assert len(page2) == PAGE_SIZE

    ids1 = {c["id"] for c in page1}
    ids2 = {c["id"] for c in page2}
    assert ids1.isdisjoint(ids2), "Pages overlap — cursor not working"


def test_helper_max_limit_constants():
    """All five helper modules export _INTEL_MAX_LIMIT == 1000."""
    from mariana.orchestrator.intelligence.evidence_ledger import _INTEL_MAX_LIMIT as EL
    from mariana.orchestrator.intelligence.credibility import _INTEL_MAX_LIMIT as CR
    from mariana.orchestrator.intelligence.contradictions import _INTEL_MAX_LIMIT as CO
    from mariana.orchestrator.intelligence.hypothesis_engine import _INTEL_MAX_LIMIT as HE

    assert EL == 1000
    assert CR == 1000
    assert CO == 1000
    assert HE == 1000


# ---------------------------------------------------------------------------
# FastAPI route envelope tests (using dependency_overrides + mocked DB pool)
# ---------------------------------------------------------------------------


@contextmanager
def _route_client(mock_db, current_user, extra_patches=None):
    """
    Context manager that yields a TestClient with:
    - dependency_overrides for _get_current_user and _require_investigation_owner
    - _get_db patched to return mock_db
    - _is_admin_user patched to False
    Extra patches is a list of (target_str, side_effect_func) tuples.
    """
    import mariana.api as api_mod
    from mariana.api import app
    from fastapi.testclient import TestClient

    # FastAPI dependency overrides — bypasses authentication entirely.
    async def _override_user():
        return current_user

    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[api_mod._get_current_user] = _override_user
    app.dependency_overrides[api_mod._require_investigation_owner] = _override_user

    patches_started = []
    base_patches = [
        patch.object(api_mod, "_get_db", return_value=mock_db),
        patch.object(api_mod, "_is_admin_user", return_value=False),
    ]
    for p in base_patches + (extra_patches or []):
        p.__enter__()
        patches_started.append(p)

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        for p in reversed(patches_started):
            p.__exit__(None, None, None)
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)


def _build_mock_db(task_id: str, owner_id: str):
    class _FakeRecord(dict):
        pass

    task_row = _FakeRecord({
        "user_id": owner_id,
        "metadata": json.dumps({"user_id": owner_id}),
    })
    mock_db = AsyncMock()
    mock_db.fetchrow = AsyncMock(return_value=task_row)
    return mock_db


def test_envelope_shape_claims():
    """Claims endpoint returns {items, next_cursor, limit}."""
    task_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())
    mock_db = _build_mock_db(task_id, owner_id)
    current_user = {"user_id": owner_id, "role": "user"}

    claims = [_make_claim(i) for i in range(3)]

    async def _fake_ledger(tid, db, limit=100, cursor=None):
        return claims[:limit]

    extra = [
        patch(
            "mariana.orchestrator.intelligence.evidence_ledger.get_evidence_ledger",
            side_effect=_fake_ledger,
        ),
    ]

    with _route_client(mock_db, current_user, extra) as client:
        resp = client.get(
            f"/api/intelligence/{task_id}/claims",
            headers={"Authorization": "Bearer fake"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body, f"Missing 'items': {body}"
    assert "next_cursor" in body, f"Missing 'next_cursor': {body}"
    assert "limit" in body, f"Missing 'limit': {body}"
    assert body["limit"] == 100


def test_envelope_shape_source_scores():
    """Source-scores endpoint returns {items, next_cursor, limit, average_credibility}."""
    task_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())
    mock_db = _build_mock_db(task_id, owner_id)
    current_user = {"user_id": owner_id, "role": "user"}

    scores = [_make_source_score(i) for i in range(2)]

    async def _fake_scores(tid, db, limit=100, cursor=None):
        return scores[:limit]

    async def _fake_avg(tid, db):
        return 0.65

    extra = [
        patch(
            "mariana.orchestrator.intelligence.credibility.get_source_scores",
            side_effect=_fake_scores,
        ),
        patch(
            "mariana.orchestrator.intelligence.credibility.get_average_credibility",
            side_effect=_fake_avg,
        ),
    ]

    with _route_client(mock_db, current_user, extra) as client:
        resp = client.get(
            f"/api/intelligence/{task_id}/source-scores",
            headers={"Authorization": "Bearer fake"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert "next_cursor" in body
    assert "limit" in body
    assert "average_credibility" in body


def test_envelope_shape_contradictions():
    """Contradictions endpoint returns {items, next_cursor, limit, ...matrix fields}."""
    task_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())
    mock_db = _build_mock_db(task_id, owner_id)
    current_user = {"user_id": owner_id, "role": "user"}

    contradictions = [_make_contradiction(i) for i in range(2)]

    async def _fake_matrix(tid, db, limit=100, cursor=None):
        return {
            "total_contradictions": len(contradictions),
            "unresolved_count": len(contradictions),
            "resolved_count": 0,
            "contradictions": contradictions[:limit],
            "critical_unresolved": [],
        }

    extra = [
        patch(
            "mariana.orchestrator.intelligence.contradictions.get_contradiction_matrix",
            side_effect=_fake_matrix,
        ),
    ]

    with _route_client(mock_db, current_user, extra) as client:
        resp = client.get(
            f"/api/intelligence/{task_id}/contradictions",
            headers={"Authorization": "Bearer fake"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert "next_cursor" in body
    assert "limit" in body


def test_envelope_shape_hypotheses():
    """Hypotheses/rankings endpoint returns {items, next_cursor, limit, winner}."""
    task_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())
    mock_db = _build_mock_db(task_id, owner_id)
    current_user = {"user_id": owner_id, "role": "user"}

    rankings = [
        {
            "hypothesis_id": str(uuid.uuid4()),
            "statement": f"Hyp {i}",
            "prior": 0.5,
            "posterior": 0.6,
            "status": "ACTIVE",
            "branch_score": None,
            "evidence_count": 2,
            "posterior_change": 0.1,
            "_cursor_ts": _ts(i),
            "_cursor_id": str(uuid.uuid4()),
        }
        for i in range(2)
    ]

    async def _fake_rankings(tid, db, limit=100, cursor=None):
        return rankings[:limit]

    async def _fake_winner(tid, db):
        return None

    extra = [
        patch(
            "mariana.orchestrator.intelligence.hypothesis_engine.get_hypothesis_rankings",
            side_effect=_fake_rankings,
        ),
        patch(
            "mariana.orchestrator.intelligence.hypothesis_engine.get_winning_hypothesis",
            side_effect=_fake_winner,
        ),
    ]

    with _route_client(mock_db, current_user, extra) as client:
        resp = client.get(
            f"/api/intelligence/{task_id}/hypotheses/rankings",
            headers={"Authorization": "Bearer fake"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert "next_cursor" in body
    assert "limit" in body
    assert "winner" in body
    for item in body["items"]:
        assert "_cursor_ts" not in item
        assert "_cursor_id" not in item


def test_envelope_shape_perspectives():
    """Perspectives endpoint returns {items, next_cursor, limit}."""
    task_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())
    perspectives = [_make_perspective(i) for i in range(2)]

    class _FakePerspRow(dict):
        pass

    fake_rows = [_FakePerspRow(p) for p in perspectives]
    mock_db = _build_mock_db(task_id, owner_id)
    mock_db.fetch = AsyncMock(return_value=fake_rows)
    current_user = {"user_id": owner_id, "role": "user"}

    with _route_client(mock_db, current_user) as client:
        resp = client.get(
            f"/api/intelligence/{task_id}/perspectives",
            headers={"Authorization": "Bearer fake"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert "next_cursor" in body
    assert "limit" in body
