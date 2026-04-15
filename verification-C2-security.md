# Verification C2: Security & Adversarial Audit
Date: 2026-04-15
Auditor: GPT-5.4 (adversarial)

## Files Reviewed
- /home/user/workspace/mariana/mariana/api.py
- /home/user/workspace/mariana/mariana/main.py
- /home/user/workspace/mariana/mariana/orchestrator/event_loop.py
- /home/user/workspace/mariana/mariana/data/db.py
- /home/user/workspace/mariana/mariana/data/models.py
- /home/user/workspace/mariana/mariana/config.py
- /home/user/workspace/mariana/mariana/ai/session.py
- /home/user/workspace/mariana/mariana/ai/router.py
- /home/user/workspace/mariana/mariana/orchestrator/cost_tracker.py
- /home/user/workspace/mariana/mariana/tools/finance.py
- /home/user/workspace/mariana/mariana/tools/doc_gen.py
- /home/user/workspace/mariana/mariana/tools/skills.py
- /home/user/workspace/mariana/mariana/tools/video_gen.py
- /home/user/workspace/mariana/mariana/tools/memory.py
- /home/user/workspace/mariana/mariana/tools/image_gen.py
- /home/user/workspace/mariana/mariana/tools/perplexity_search.py
- /home/user/workspace/mariana/mariana/tools/__init__.py
- /home/user/workspace/mariana/mariana/connectors/polygon_connector.py
- /home/user/workspace/mariana/mariana/connectors/sec_edgar_connector.py
- /home/user/workspace/mariana/mariana/connectors/unusual_whales_connector.py
- /home/user/workspace/mariana/mariana/connectors/fred_connector.py
- /home/user/workspace/mariana/mariana/connectors/base.py
- /home/user/workspace/mariana/mariana/connectors/__init__.py
- /home/user/workspace/mariana/mariana/browser/pool_server.py
- /home/user/workspace/mariana/mariana/report/generator.py
- /home/user/workspace/mariana/mariana/report/renderer.py
- /home/user/workspace/mariana/frontend/src/contexts/AuthContext.tsx
- /home/user/workspace/mariana/frontend/src/pages/Login.tsx
- /home/user/workspace/mariana/frontend/src/pages/Signup.tsx
- /home/user/workspace/mariana/frontend/src/pages/Chat.tsx
- /home/user/workspace/mariana/frontend/src/pages/Admin.tsx
- /home/user/workspace/mariana/frontend/src/pages/Account.tsx
- /home/user/workspace/mariana/frontend/src/pages/BuyCredits.tsx
- /home/user/workspace/mariana/frontend/src/pages/Checkout.tsx
- /home/user/workspace/mariana/frontend/src/lib/supabase.ts
- /home/user/workspace/mariana/Dockerfile
- /home/user/workspace/mariana/docker-compose.yml
- /home/user/workspace/mariana/requirements.txt
- /home/user/workspace/mariana/frontend/src/components/FileUpload.tsx
- /home/user/workspace/mariana/mariana/report/templates/report.html.j2

## Vulnerabilities Found

### VULN-C2-01: Credit settlement can fail open after post-call budget overrun
- **File**: /home/user/workspace/mariana/mariana/ai/session.py, line 583; /home/user/workspace/mariana/mariana/main.py, line 423; /home/user/workspace/mariana/mariana/api.py, line 757
- **Severity**: high
- **CVSS estimate**: 8.1
- **Attack vector**: A user can submit an investigation with an artificially tiny `budget_usd`, reserve only a trivial number of credits up front, still receive at least one AI call because budget checks happen before the call, and then rely on the settlement path to fail without rolling the result back when the extra deduction RPC is rejected ([ai/session.py](/home/user/workspace/mariana/mariana/ai/session.py), [main.py](/home/user/workspace/mariana/mariana/main.py), [api.py](/home/user/workspace/mariana/mariana/api.py)).
- **Impact**: Authenticated users can obtain underpaid or effectively free model work, especially on instant/quick flows, by forcing the system to spend more than the reserved amount and then leaving the final delta uncollected ([ai/session.py](/home/user/workspace/mariana/mariana/ai/session.py), [main.py](/home/user/workspace/mariana/mariana/main.py)).
- **Fix**: Enforce a minimum upfront reservation tied to the classified tier, make `record_call()` budget exceptions propagate instead of being swallowed, and treat failed final deductions as a hard failure that withholds completion/results or pauses the task until payment succeeds ([ai/session.py](/home/user/workspace/mariana/mariana/ai/session.py), [main.py](/home/user/workspace/mariana/mariana/main.py), [api.py](/home/user/workspace/mariana/mariana/api.py)).

### VULN-C2-02: Bearer tokens are exposed in SSE query strings
- **File**: /home/user/workspace/mariana/frontend/src/pages/Chat.tsx, line 759; /home/user/workspace/mariana/mariana/api.py, line 546; /home/user/workspace/mariana/mariana/api.py, line 1193
- **Severity**: medium
- **CVSS estimate**: 6.8
- **Attack vector**: The chat client opens `EventSource` with `?token=<JWT>` in the URL, and the backend explicitly accepts auth from the query string, so bearer tokens can leak through browser history, reverse-proxy access logs, observability tooling, and any component that records full request URLs ([Chat.tsx](/home/user/workspace/mariana/frontend/src/pages/Chat.tsx), [api.py](/home/user/workspace/mariana/mariana/api.py)).
- **Impact**: Anyone who later obtains those logged URLs can replay the bearer token and act as the victim until the token expires or is revoked ([Chat.tsx](/home/user/workspace/mariana/frontend/src/pages/Chat.tsx), [api.py](/home/user/workspace/mariana/mariana/api.py)).
- **Fix**: Remove query-string auth for SSE, use `fetch`/streaming with an `Authorization` header or mint a short-lived single-purpose stream token, and redact any legacy `token` parameter at every logging layer until the old path is removed ([Chat.tsx](/home/user/workspace/mariana/frontend/src/pages/Chat.tsx), [api.py](/home/user/workspace/mariana/mariana/api.py)).

### VULN-C2-03: Stripe checkout allows arbitrary post-payment redirect targets
- **File**: /home/user/workspace/mariana/mariana/api.py, line 368; /home/user/workspace/mariana/mariana/api.py, line 2041
- **Severity**: medium
- **CVSS estimate**: 6.1
- **Attack vector**: An authenticated user can supply arbitrary `success_url` and `cancel_url` values to the checkout creation endpoint, and the backend forwards them directly into Stripe Checkout without host allowlisting or same-origin validation ([api.py](/home/user/workspace/mariana/mariana/api.py)).
- **Impact**: The app can be used as a trusted redirector in phishing flows, and users who finish a legitimate Stripe session can be bounced to attacker-controlled domains that impersonate Mariana or collect follow-on credentials and payment details ([api.py](/home/user/workspace/mariana/mariana/api.py)).
- **Fix**: Ignore caller-provided redirect URLs or strictly validate them against an allowlist of first-party origins and fixed paths before creating the Stripe session ([api.py](/home/user/workspace/mariana/mariana/api.py)).

### VULN-C2-04: No rate limiting on expensive, billing, and upload endpoints
- **File**: /home/user/workspace/mariana/mariana/api.py, line 158; /home/user/workspace/mariana/mariana/api.py, line 724; /home/user/workspace/mariana/mariana/api.py, line 1718; /home/user/workspace/mariana/mariana/api.py, line 2015
- **Severity**: medium
- **CVSS estimate**: 6.5
- **Attack vector**: The API defines high-cost endpoints for investigation creation, pending uploads, and Stripe checkout creation, but there is no limiter middleware, token bucket, IP throttle, or per-user request quota anywhere in the application path ([api.py](/home/user/workspace/mariana/mariana/api.py)).
- **Impact**: An attacker with one account can spam investigation submissions, fill disk with repeated uploads, and create large numbers of Stripe Checkout sessions, driving LLM spend, storage consumption, queue contention, and third-party billing noise ([api.py](/home/user/workspace/mariana/mariana/api.py)).
- **Fix**: Add server-side rate limits keyed by IP and authenticated user for `/api/investigations`, `/api/upload`, `/api/investigations/classify`, `/api/billing/create-checkout`, and the admin surface, with explicit burst and sustained quotas plus audit logging ([api.py](/home/user/workspace/mariana/mariana/api.py)).

### VULN-C2-05: Redis is left unauthenticated on the internal Docker network
- **File**: /home/user/workspace/mariana/docker-compose.yml, line 77
- **Severity**: medium
- **CVSS estimate**: 5.9
- **Attack vector**: The Compose file comments claim Redis should require a password when configured, but the runtime command never sets `requirepass`, keeps `protected-mode no`, and exposes the service to every container on the bridge network ([docker-compose.yml](/home/user/workspace/mariana/docker-compose.yml)).
- **Impact**: Any foothold in another container or any later-added internal service on `mariana-net` can publish fake log events, issue task-kill pub/sub messages, read cached data, or tamper with queue/state coordination through Redis without credentials ([docker-compose.yml](/home/user/workspace/mariana/docker-compose.yml)).
- **Fix**: Require Redis authentication in the container command, include the password in `REDIS_URL`, keep protected mode enabled when possible, and isolate Redis on a private network segment that only the API and orchestrator can reach ([docker-compose.yml](/home/user/workspace/mariana/docker-compose.yml)).

### VULN-C2-06: Pinned WeasyPrint version carries a known SSRF vulnerability
- **File**: /home/user/workspace/mariana/requirements.txt, line 7
- **Severity**: medium
- **CVSS estimate**: 5.3
- **Attack vector**: The application pins `weasyprint==63.1`, which is older than the fixed range for [CVE-2025-68616](https://nvd.nist.gov/vuln/detail/CVE-2025-68616), an SSRF protection bypass fixed in later WeasyPrint releases and documented in the [WeasyPrint changelog](https://github.com/Kozea/WeasyPrint/blob/master/docs/changelog.rst).
- **Impact**: If any future template or report content path introduces external resource fetching, the renderer can be driven toward internal network resources through redirect handling, turning PDF generation into an SSRF primitive ([CVE-2025-68616](https://nvd.nist.gov/vuln/detail/CVE-2025-68616), [WeasyPrint changelog](https://github.com/Kozea/WeasyPrint/blob/master/docs/changelog.rst)).
- **Fix**: Upgrade to WeasyPrint 68.0+ and keep the report template/resource model locked down so rendered documents cannot trigger arbitrary outbound fetches ([CVE-2025-68616](https://nvd.nist.gov/vuln/detail/CVE-2025-68616), [WeasyPrint changelog](https://github.com/Kozea/WeasyPrint/blob/master/docs/changelog.rst)).

### VULN-C2-07: Public status/config endpoints disclose internal deployment details
- **File**: /home/user/workspace/mariana/mariana/api.py, line 684; /home/user/workspace/mariana/mariana/api.py, line 1802
- **Severity**: low
- **CVSS estimate**: 3.7
- **Attack vector**: Unauthenticated callers can retrieve the internal `DATA_ROOT`, logging level, LLM gateway base URL, and service availability map from `/api/config` and `/api/connectors` without any access control ([api.py](/home/user/workspace/mariana/mariana/api.py)).
- **Impact**: This gives attackers useful reconnaissance about filesystem layout, upstream providers, and which subsystems are live before they begin targeted abuse of other endpoints ([api.py](/home/user/workspace/mariana/mariana/api.py)).
- **Fix**: Remove internal paths and provider metadata from public responses, or require authentication and return only the minimum client-safe fields needed by the frontend ([api.py](/home/user/workspace/mariana/mariana/api.py)).

## Attack Surfaces Verified Secure
- Investigation list, detail, kill, branch, finding, cost, report, and artifact-download routes now require authenticated ownership checks or explicit admin privilege, which blocks the prior cross-user access pattern ([api.py](/home/user/workspace/mariana/mariana/api.py)).
- Server-side Supabase verification is used for bearer tokens through `GET /auth/v1/user`, so simple unsigned JWT forgery is no longer accepted ([api.py](/home/user/workspace/mariana/mariana/api.py)).
- SSE ownership is checked before connection and re-validated every 30 seconds, so a token revoked after connect no longer keeps a stream alive indefinitely ([api.py](/home/user/workspace/mariana/mariana/api.py)).
- Report and artifact download endpoints resolve the requested path and verify it stays inside the intended task-specific directory, which closes the path traversal issue on those download surfaces ([api.py](/home/user/workspace/mariana/mariana/api.py)).
- Upload filenames are sanitized, session UUIDs are format-validated, file extensions are allowlisted, and per-file plus per-investigation upload counts are enforced server-side ([api.py](/home/user/workspace/mariana/mariana/api.py), [FileUpload.tsx](/home/user/workspace/mariana/frontend/src/components/FileUpload.tsx)).
- Stripe webhooks now require a configured webhook secret and use an idempotency table keyed by event ID, which blocks signature bypass and replay of already-processed events ([api.py](/home/user/workspace/mariana/mariana/api.py), [db.py](/home/user/workspace/mariana/mariana/data/db.py)).
- Admin user listing, stats, and credit adjustment endpoints are guarded by both the hardcoded admin identity check and downstream Supabase RPC authorization instead of trusting frontend role state alone ([api.py](/home/user/workspace/mariana/mariana/api.py), [Admin.tsx](/home/user/workspace/mariana/frontend/src/pages/Admin.tsx)).
- Dynamic SQL update helpers in the DB layer use explicit allowlists for mutable column names, and normal query values are parameterized throughout, which prevented straightforward SQL injection findings in the reviewed CRUD paths ([db.py](/home/user/workspace/mariana/mariana/data/db.py)).
- The Docker image now runs as the non-root `mariana` user, so the previously identified root-in-container issue is no longer present in the build artifact ([Dockerfile](/home/user/workspace/mariana/Dockerfile)).
