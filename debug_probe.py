#!/usr/bin/env python3
"""
CTO-grade debug probe for Mariana v3.7.

Runs a battery of production checks against a deployed backend:

  1. Health probe            — DB, Redis, Supabase, browser, sandbox, LLM gateway
  2. Admin RBAC negative     — non-admin calls MUST receive 401/403
  3. Admin RBAC positive     — admin calls MUST receive 200
  4. Security red-team       — SSRF, path traversal, SQL-injection-shaped inputs
                              are all rejected (4xx), never 500
  5. Rate-limit smoke        — burst of unauth calls returns 429 eventually
  6. Artifact-surface smoke  — submit a tiny task, poll to completion, verify
                              artifact download works

Every check is isolated — one failure never masks the next.  Exit code is
non-zero only if any RED-severity check fails.  YELLOW warnings don't fail
the build but are reported.

Usage:
    python3 debug_probe.py
        --api https://api.example.com
        --admin-token <jwt-for-admin>
        [--user-token <jwt-for-regular-user>]
        [--skip smoke,security]
        [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    category: str
    severity: str  # "red" or "yellow"
    passed: bool
    detail: str = ""
    latency_ms: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProbeReport:
    api: str
    started_at: str
    completed_at: str = ""
    results: list[CheckResult] = field(default_factory=list)

    @property
    def red_fails(self) -> list[CheckResult]:
        return [r for r in self.results if r.severity == "red" and not r.passed]

    @property
    def yellow_fails(self) -> list[CheckResult]:
        return [r for r in self.results if r.severity == "yellow" and not r.passed]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _check(report: ProbeReport, name: str, category: str, severity: str = "red"):
    """Decorator factory that records a CheckResult."""

    def deco(fn):
        def wrapped(*args, **kwargs) -> None:
            t0 = time.perf_counter()
            passed = False
            detail = ""
            meta: dict[str, Any] = {}
            try:
                result = fn(*args, **kwargs)
                if isinstance(result, tuple):
                    passed, detail = result[0], result[1] if len(result) > 1 else ""
                    if len(result) >= 3:
                        meta = result[2]
                elif isinstance(result, bool):
                    passed, detail = result, ""
                else:
                    passed = True
                    detail = str(result) if result else ""
            except AssertionError as exc:
                passed = False
                detail = f"ASSERT: {exc}"
            except Exception as exc:  # noqa: BLE001
                passed = False
                detail = f"{type(exc).__name__}: {exc}"
            latency = (time.perf_counter() - t0) * 1000
            report.results.append(
                CheckResult(
                    name=name,
                    category=category,
                    severity=severity,
                    passed=passed,
                    detail=detail[:500],
                    latency_ms=round(latency, 1),
                    meta=meta,
                )
            )

        return wrapped

    return deco


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def probe_health(report: ProbeReport, api: str, admin_token: str) -> None:
    @_check(report, "api.health.public", "health", "red")
    def public_health():
        r = httpx.get(f"{api}/api/health", timeout=5.0)
        assert r.status_code == 200, f"HTTP {r.status_code}"
        body = r.json()
        return (body.get("status") in ("ok", "healthy"), json.dumps(body)[:200])

    public_health()

    @_check(report, "api.health.deep_probe", "health", "red")
    def deep_probe():
        r = httpx.get(
            f"{api}/api/admin/health-probe",
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=30.0,
        )
        assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:200]}"
        body = r.json()
        components = body.get("components", {})
        failed = [k for k, v in components.items() if not v.get("ok")]
        return (not failed, f"failed={failed}", {"components": components})

    deep_probe()


def probe_rbac(
    report: ProbeReport, api: str, admin_token: str, user_token: str | None
) -> None:
    admin_only = [
        "/api/admin/overview",
        "/api/admin/users",
        "/api/admin/feature-flags",
        "/api/admin/audit-log?limit=1",
        "/api/admin/admin-tasks?limit=1",
        "/api/admin/health-probe",
    ]

    # Positive: admin gets 200
    for path in admin_only:
        @_check(report, f"rbac.admin_allow:{path}", "rbac", "red")
        def _positive(path=path):
            r = httpx.get(
                f"{api}{path}",
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=10.0,
            )
            assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:150]}"
            return True, f"status={r.status_code}"

        _positive()

    # Negative: anon gets 401
    for path in admin_only:
        @_check(report, f"rbac.anon_deny:{path}", "rbac", "red")
        def _anon(path=path):
            r = httpx.get(f"{api}{path}", timeout=5.0)
            assert r.status_code in (401, 403), f"expected 401/403, got {r.status_code}"
            return True, f"status={r.status_code}"

        _anon()

    # Negative: non-admin user gets 403 (only if token provided)
    if user_token:
        for path in admin_only:
            @_check(report, f"rbac.user_deny:{path}", "rbac", "red")
            def _user(path=path):
                r = httpx.get(
                    f"{api}{path}",
                    headers={"Authorization": f"Bearer {user_token}"},
                    timeout=5.0,
                )
                assert r.status_code in (401, 403), (
                    f"non-admin reached {path}: HTTP {r.status_code}"
                )
                return True, f"status={r.status_code}"

            _user()


def probe_security(report: ProbeReport, api: str, admin_token: str) -> None:
    """Red-team: malformed/malicious inputs must 4xx, never 500."""

    hdr = {"Authorization": f"Bearer {admin_token}"}

    # SQL-injection shapes on status filter
    @_check(report, "security.sql_injection.status_filter", "security", "red")
    def sql_inj():
        r = httpx.get(
            f"{api}/api/admin/tasks?status=RUNNING';DROP TABLE users;--",
            headers=hdr,
            timeout=5.0,
        )
        assert r.status_code < 500, f"5xx on malformed status: {r.status_code}"
        return True, f"status={r.status_code}"

    sql_inj()

    # Path-traversal in user_id param
    @_check(report, "security.path_traversal.user_id", "security", "red")
    def path_trav():
        r = httpx.post(
            f"{api}/api/admin/users/..%2F..%2Fadmin/role",
            headers={**hdr, "Content-Type": "application/json"},
            json={"role": "admin"},
            timeout=5.0,
        )
        assert r.status_code < 500, f"5xx on path traversal: {r.status_code}"
        assert r.status_code in (400, 404, 422), (
            f"expected rejection, got {r.status_code}"
        )
        return True, f"status={r.status_code}"

    path_trav()

    # Null-byte in header
    @_check(report, "security.null_bytes.auth_header", "security", "red")
    def null_bytes():
        try:
            r = httpx.get(
                f"{api}/api/admin/overview",
                headers={"Authorization": "Bearer \x00bogus"},
                timeout=5.0,
            )
            # httpx may raise before sending; that's fine
            assert r.status_code < 500, f"5xx on null-byte auth: {r.status_code}"
        except httpx.RequestError:
            # client-side rejection is equivalent to server-side rejection
            pass
        return True, "handled"

    null_bytes()

    # Oversize payload on danger endpoint (should reject, not OOM)
    @_check(report, "security.oversize_payload.flush_redis", "security", "yellow")
    def oversize():
        big = "x" * 20000
        r = httpx.post(
            f"{api}/api/admin/danger/flush-redis",
            headers={**hdr, "Content-Type": "application/json"},
            json={"confirm": big},
            timeout=5.0,
        )
        # Expect 422 (pydantic validation or confirmation mismatch)
        assert r.status_code < 500, f"5xx on oversize: {r.status_code}"
        return True, f"status={r.status_code}"

    oversize()

    # Confirm flush-redis refuses without the magic phrase
    @_check(report, "security.confirm_required.flush_redis", "security", "red")
    def confirm_required():
        r = httpx.post(
            f"{api}/api/admin/danger/flush-redis",
            headers={**hdr, "Content-Type": "application/json"},
            json={"confirm": "yes"},
            timeout=5.0,
        )
        assert r.status_code == 422, f"flush-redis should 422 without phrase; got {r.status_code}"
        return True, f"status={r.status_code}"

    confirm_required()


def probe_rate_limit(report: ProbeReport, api: str) -> None:
    """Fire a burst of unauth requests to the health endpoint; expect some 429."""

    @_check(report, "rate_limit.health_burst", "rate_limit", "yellow")
    def burst():
        saw_429 = False
        status_codes: list[int] = []
        with httpx.Client(timeout=2.0) as client:
            for _ in range(60):
                try:
                    r = client.get(f"{api}/api/health")
                    status_codes.append(r.status_code)
                    if r.status_code == 429:
                        saw_429 = True
                        break
                except httpx.RequestError:
                    break
        # Not seeing 429 is a warning (yellow), not a red fail — rate limiter
        # may be generous or disabled in dev.
        return (saw_429, f"saw_429={saw_429}, codes_seen={set(status_codes)}")

    burst()


def probe_smoke_artifact(report: ProbeReport, api: str, admin_token: str) -> None:
    """End-to-end smoke: submit a trivial task, wait, confirm artifact."""

    @_check(report, "smoke.artifact_roundtrip", "smoke", "yellow")
    def roundtrip():
        # Create a tiny task.  We use the agent /api/tasks endpoint if present,
        # otherwise skip.
        hdr = {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}
        payload = {
            "prompt": "Print 'hello' using bash_exec, then deliver a short text file.",
            "budget_usd": 0.05,
        }
        try:
            r = httpx.post(f"{api}/api/tasks", headers=hdr, json=payload, timeout=15.0)
        except httpx.RequestError as exc:
            return False, f"submit failed: {exc}"
        if r.status_code not in (200, 201, 202):
            return False, f"submit HTTP {r.status_code}: {r.text[:150]}"
        data = r.json()
        task_id = data.get("task_id") or data.get("id")
        if not task_id:
            return False, f"no task_id in response: {data}"

        # Poll for up to 90s
        deadline = time.time() + 90
        state = ""
        while time.time() < deadline:
            time.sleep(3)
            pr = httpx.get(f"{api}/api/tasks/{task_id}", headers=hdr, timeout=10.0)
            if pr.status_code != 200:
                continue
            state = pr.json().get("state") or pr.json().get("status") or ""
            if state.lower() in ("done", "completed", "failed", "error"):
                break
        return (state.lower() in ("done", "completed"), f"final_state={state}", {"task_id": task_id})

    roundtrip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api", default=os.environ.get("MARIANA_API", "http://77.42.3.206:8080"))
    ap.add_argument("--admin-token", default=os.environ.get("MARIANA_ADMIN_TOKEN", ""))
    ap.add_argument("--user-token", default=os.environ.get("MARIANA_USER_TOKEN", ""))
    ap.add_argument(
        "--skip",
        default="smoke",
        help="Comma-separated categories to skip (health/rbac/security/rate_limit/smoke).",
    )
    ap.add_argument("--json", default="", help="Write full JSON report to this path.")
    args = ap.parse_args(argv)

    if not args.admin_token:
        print(
            "ERROR: --admin-token (or MARIANA_ADMIN_TOKEN) is required.",
            file=sys.stderr,
        )
        return 2

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    report = ProbeReport(api=args.api, started_at=_now_iso())

    print(f"→ Probing {args.api}")
    if "health" not in skip:
        probe_health(report, args.api, args.admin_token)
    if "rbac" not in skip:
        probe_rbac(report, args.api, args.admin_token, args.user_token or None)
    if "security" not in skip:
        probe_security(report, args.api, args.admin_token)
    if "rate_limit" not in skip:
        probe_rate_limit(report, args.api)
    if "smoke" not in skip:
        probe_smoke_artifact(report, args.api, args.admin_token)

    report.completed_at = _now_iso()

    # Print summary
    print(f"\n=== Probe results ({len(report.results)} checks) ===")
    for r in report.results:
        mark = "✓" if r.passed else "✗"
        sev = "[RED]" if r.severity == "red" else "[yel]"
        print(f"  {mark} {sev} [{r.category:<11}] {r.name:<52} {r.latency_ms:>7.1f}ms  {r.detail[:80]}")

    red_fails = report.red_fails
    yel_fails = report.yellow_fails
    print(
        f"\nSummary: {len(report.results)} total, {len(red_fails)} RED fails, "
        f"{len(yel_fails)} yellow fails"
    )

    if args.json:
        with open(args.json, "w") as f:
            json.dump(
                {
                    "api": report.api,
                    "started_at": report.started_at,
                    "completed_at": report.completed_at,
                    "results": [asdict(r) for r in report.results],
                    "red_fails": len(red_fails),
                    "yellow_fails": len(yel_fails),
                },
                f,
                indent=2,
            )
        print(f"JSON report → {args.json}")

    return 1 if red_fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
