"""CC-33 regression \u2014 ``/metrics`` exists and is admin-gated.

CC-33 (P4, post-CC-26 re-audit #44 Finding 7) called for an operator metrics
surface.  We landed a hand-rolled Prometheus-style ``/metrics`` endpoint
(zero new dependencies) gated behind ``_require_admin``.

This module pins:

  * ``/metrics`` returns 401 / 403 without admin auth.
  * ``/metrics`` returns 200 with admin auth, and the body is Prometheus
    text format (``http_requests_total``, ``process_uptime_seconds``).
  * The middleware counters do not double-count themselves \u2014 a /metrics
    scrape does not increment ``http_requests_total``.
  * The endpoint is excluded from OpenAPI schema (``include_in_schema=False``).
"""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.testclient import TestClient

from mariana import api as mod


# ---------------------------------------------------------------------------
# (1) Anonymous request to /metrics is rejected
# ---------------------------------------------------------------------------


def test_metrics_unauthenticated_rejected():
    """No auth \u2192 401 from ``_get_current_user`` (or 403 if reachable).

    Either status code is acceptable proof of secure-by-default gating.
    """
    client = TestClient(mod.app)
    resp = client.get("/metrics")
    assert resp.status_code in (401, 403), (
        f"unauthenticated /metrics must be rejected, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# (2) Non-admin auth is rejected with 403
# ---------------------------------------------------------------------------


def test_metrics_non_admin_rejected():
    """A logged-in non-admin user gets 403 from ``_require_admin``."""

    def _fake_user():
        return {"user_id": "non-admin-user-uuid"}

    mod.app.dependency_overrides[mod._get_current_user] = _fake_user
    try:
        client = TestClient(mod.app)
        # _is_admin_user(non-admin) returns False because ADMIN_USER_ID is "".
        resp = client.get("/metrics")
        assert resp.status_code == 403, (
            f"non-admin /metrics must return 403, got {resp.status_code}"
        )
    finally:
        mod.app.dependency_overrides.pop(mod._get_current_user, None)


# ---------------------------------------------------------------------------
# (3) Admin auth gets 200 + Prometheus body
# ---------------------------------------------------------------------------


def test_metrics_admin_authenticated_returns_200_prometheus():
    """An admin caller sees 200 + Prometheus text format."""
    admin = {"user_id": "admin-user-uuid"}
    mod.app.dependency_overrides[mod._require_admin] = lambda: admin
    try:
        client = TestClient(mod.app)
        resp = client.get("/metrics")
        assert resp.status_code == 200
        body = resp.text
        # Prometheus text format markers
        assert "# TYPE http_requests_total counter" in body
        assert "http_requests_total " in body
        assert "process_uptime_seconds " in body
        # Prometheus content type
        ct = resp.headers.get("content-type", "")
        assert ct.startswith("text/plain"), f"unexpected content-type: {ct}"
    finally:
        mod.app.dependency_overrides.pop(mod._require_admin, None)


# ---------------------------------------------------------------------------
# (4) /metrics is hidden from OpenAPI schema
# ---------------------------------------------------------------------------


def test_metrics_route_excluded_from_openapi():
    """/metrics carries ``include_in_schema=False`` so it is not advertised."""
    paths = (mod.app.openapi() or {}).get("paths", {})
    assert "/metrics" not in paths, (
        "/metrics must be hidden from the OpenAPI schema (include_in_schema=False)"
    )


# ---------------------------------------------------------------------------
# (5) /metrics middleware does not self-instrument
# ---------------------------------------------------------------------------


def test_metrics_endpoint_does_not_self_instrument():
    """A /metrics scrape must NOT bump ``http_requests_total``.

    Otherwise scrape rate would inflate the counter and confuse alerting.
    """
    admin = {"user_id": "admin-user-uuid"}
    mod.app.dependency_overrides[mod._require_admin] = lambda: admin
    try:
        client = TestClient(mod.app)
        # Hit /metrics three times back to back.
        client.get("/metrics")
        client.get("/metrics")
        before = mod._metrics_counters["http_requests_total"]
        client.get("/metrics")
        after = mod._metrics_counters["http_requests_total"]
        assert after == before, f"/metrics self-instrumented: {before} -> {after}"
    finally:
        mod.app.dependency_overrides.pop(mod._require_admin, None)
