# A47 Deep Sweep Re-audit

Repo: `/home/user/workspace/mariana`
Branch: `loop6/zero-bug`
HEAD reviewed: `6550ba7`
Mode: discovery only

## Executive summary

Total findings: **8**

Severity breakdown:
- **Medium:** 2
- **Low:** 6
- **High:** 0

Verdict: the codebase is close, but it is **not yet clean enough for a production-readiness sign-off** because there are still unresolved hardening gaps in rate limiting and rollback-path SQL security, plus several remaining internal-detail leaks.

## Findings

### 1) Public API rate limiting can silently disappear in environments built from `requirements.txt`
- **Severity:** Medium
- **File:line:** `mariana/api.py:67-98`, `mariana/api.py:429-454`, `requirements.txt:1-21`
- **Mechanism:** `mariana/api.py` explicitly treats `slowapi` as optional and replaces it with `_NoopLimiter` when the import fails. In that branch, `Limiter.limit()` becomes a pass-through decorator and `SlowAPIMiddleware` is not added. `requirements.txt` does not include `slowapi`, so a straightforward install from the declared Python dependencies can ship without the hardening dependency present. The app still boots, but the intended shared limiter layer silently disappears.
- **Why this matters:** This creates environment drift where production or staging can run with materially weaker abuse protection than developers expect. It especially matters for auth and other public endpoints because operators may believe the documented 60/minute policy is enforced when it is not.
- **Fix sketch:** Make `slowapi` a required pinned dependency, fail startup if the limiter backend is unavailable, or remove the optional-import path and rely on a single mandatory rate-limiting layer.

### 5) Agent API routes still leak task identifiers and backend exception text to clients/SSE consumers
- **Severity:** Low
- **File:line:** `mariana/agent/api_routes.py:443`, `648`, `664`, `716`, `752`, `804`, `618-628`
- **Mechanism:** The routes expose messages such as `vault_env: {exc}`, `agent task {task_id} not found`, and SSE `error` events containing `str(exc)`. The vault fail-closed branch also persists a user-visible task error assembled from the raw exception string before returning 503.

### 6) Agent loop still persists raw internal exception strings into task-visible error fields
- **Severity:** Low
- **File:line:** `mariana/agent/loop.py:956`, `1043`, `1073`, `1189`, `1200`, `1211`, `1290-1291`, `1412-1417`
- **Mechanism:** Several failure paths store or emit raw exception text directly, including `unexpected: {type(exc).__name__}: {exc}`, `Vault unavailable: {exc}`, `Vault transport policy violation: {exc}`, `planner_failed: {exc}`, and `loop_crash: ...`.
