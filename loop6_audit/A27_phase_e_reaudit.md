# Phase E re-audit #22

Header: model=gpt_5_4, commit=2671eda, scope=adversarial probe of V-01/V-02 fixes + cross-callsite check + fresh broader sweep.

## Surface walkthrough

### Probe 1 — V-01 fix correctness (urlparse validator)
- Read the new shared helper `mariana/util/redis_url.py` in full plus the wrapper/callsites in `mariana/vault/runtime.py` and `mariana/data/cache.py`.
- The new helper is hostname-based, not substring-based: it parses with `urlparse()`, rejects `hostname is None`, allows `rediss://` for any hostname, and allows plaintext `redis://` only for exact local names or `ipaddress.ip_address(host).is_loopback`, plus literal `redis` for docker-compose.
- Confirmed parser behavior live:
  - `urlparse('redis://localhost:6379').hostname == 'localhost'`
  - `urlparse('redis://LOCALHOST:6379').hostname == 'localhost'` so uppercase localhost is accepted case-insensitively.
  - `urlparse('redis://[::1]:6379/0').hostname == '::1'` and the validator accepts it.
  - `urlparse('redis://%6Cocalhost:6379').hostname == '%6cocalhost'`; the hostname is not percent-decoded, so the validator rejects it. Safe fail-closed.
  - `urlparse('redis://[::ffff:127.0.0.1]:6379').hostname == '::ffff:127.0.0.1'`; `ipaddress.ip_address(...).is_loopback` is True, so the validator accepts IPv4-mapped loopback.
  - `unix:///var/run/redis.sock` is rejected as malformed because there is no hostname.
  - `redis+sentinel://localhost:26379` is rejected because only `redis://` and `rediss://` are allowed.
- Thin wrapper `_validate_redis_url_for_vault()` still exists in `mariana/vault/runtime.py`, but it now delegates to the shared helper. The old substring-matching validator logic is gone.
- DNS rebinding limit remains: the helper validates the URL string/hostname class, not the eventual resolved IP. `localhost` is intentionally trusted as local-dev and could still be abused if an operator environment does hostile name resolution. This is a documented limit of string-level URL validation, not a regression from the V-01 patch.
- Conclusion: the V-01 hostname-parser fix itself is correct on the requested edge cases.

### Probe 2 — V-02 fix correctness (settlement restructure)
- Read `mariana/agent/loop.py` around `run_agent_task()` and `_settle_agent_credits()` in full relevant slices.
- The vault fetch now happens inside the outer `try:`. The `VaultUnavailableError`, `ValueError`, and unexpected-exception fail-closed branches all `return task` from inside that `try`, so Python still executes the outer `finally:`. This fixes V-02’s original early-return hole.
- On vault fetch failure, code sets `task.state = AgentState.FAILED`, tries `_persist_task()`, and returns. Even if `_persist_task()` raises, the exception is swallowed and the `finally:` still runs.
- In the refund path, `_settle_agent_credits()` computes `final_tokens = usd_to_credits(task.spent_usd)` and `delta = final_tokens - task.reserved_credits`. For the probed case `spent_usd = 0.0`, `reserved_credits = 100`, delta is `-100`, so it calls `grant_credits` with `p_source='refund'`, `p_ref_type='agent_task'`, `p_ref_id=task.id`, and `p_credits=100`. This is the expected idempotent refund path.
- There is no shadowing bug with the nested `try`: the inner vault-fetch `try` only handles bootstrap failures; the outer `try/finally` still owns settlement and final persist.
- `db is None` is not a new regression in the worker refund path. The helper only short-circuits when Supabase config/api key is absent. If Supabase is configured and `db is None`, it can still issue the ledger RPCs; it just lacks the DB claim-row/reconciler anchor. That is acceptable for test-only paths and not the fixed production path.
- The stale-worker / previous-success race remains correct. `_settle_agent_credits()` first looks up `agent_settlements`; if a prior attempt already completed settlement (`completed_at IS NOT NULL`), it sets `task.credits_settled = True` and returns without issuing another refund. That avoids double-refund on stale retries.
- Conclusion: the V-02 control-flow fix is correct; the original stranded-reservation bug is closed.

### Probe 3 — V01_V02 test adequacy
- Read `tests/test_v01_v02_vault_hardening.py`.
- The V-01 bypass tests are exactly the four documented bypasses:
  1. `redis://localhost.attacker.com:6379`
  2. `redis://localhost@evil.com:6379`
  3. `redis://127.0.attacker.com:6379`
  4. `redis://redis:secret@evil.com:6379`
- Legitimate-allowed cases include both requested allow-list cases: `redis://[::1]:6379` and `redis://redis:6379`, plus `localhost` and `127.0.0.1`.
- The V-02 settlement tests do verify refund RPC activity, not just `state=FAILED`: they patch `httpx.AsyncClient`, capture RPC calls, assert `_settle_agent_credits()` is awaited once, then assert a `/rest/v1/rpc/grant_credits` call fired with `p_credits == 100`, `p_source == 'refund'`, `p_ref_type == 'agent_task'`, and `p_ref_id == task.id`.
- Adequacy gap: the new tests do not pin several requested edge cases (uppercase `LOCALHOST`, IPv4-mapped IPv6 loopback, percent-encoded hostnames, or non-standard schemes like `redis+sentinel://` / `unix://`). That is a coverage gap, but the code itself handled those probes safely in this audit.
- Conclusion: regression coverage is materially good for V-01/V-02, though not exhaustive on edge cases.

### Probe 4 — Newly introduced shared util drift / import cycles
- `mariana/util/redis_url.py` imports only `ipaddress` and `urllib.parse.urlparse`; `mariana/vault/runtime.py` and `mariana/data/cache.py` import the helper. No circular import was found.
- The legacy duplicate validator drift between vault and cache is gone; both now rely on the shared helper.
- However, other Redis client constructors still bypass the shared validator entirely:
  - `mariana/api.py` startup creates `_redis = aioredis.from_url(_config.REDIS_URL, ...)`
  - `mariana/main.py:_create_redis()` returns `aioredis.from_url(config.REDIS_URL, ...)`
- Those surfaces are outside the vault/cache callsites but still use the same operator-controlled `REDIS_URL`. They therefore remain plaintext-tolerant for non-vault Redis traffic if a remote `redis://` URL is configured.
- Conclusion: no import-cycle issue, but there is still shared-util adoption drift on other Redis constructors. See finding W-01.

### Probe 5 — Fresh broader sweep
- OAuth / Google / magic-link / password-reset: no first-party backend OAuth or reset flow was found in the sampled backend routes; auth appears delegated to Supabase token verification and frontend-managed auth UX.
- Admin surface spot-check: sampled `/api/admin/*` routes use `_require_admin` and the older owner/admin checks still route through `_is_admin_user()`. No fresh owner-check bypass stood out in the sampled slices.
- Profile-update / mass-assignment sweep: no obvious backend route surfaced that lets a user PATCH arbitrary profile fields such as `is_admin` / `role` through a broad JSON merge.
- Logging-hygiene spot-check found some verbose upstream-body logging on error paths, but this pass did not establish a stronger exploit than confidentiality-by-log-retention risk. No separate billing/security finding opened from this sample.
- Conclusion: no additional confirmed exploitable issue from the fresh sampled surfaces beyond W-01.

### Probe 6 — Spot-checks
- T-01: `task.credits_settled = True` assignments in `mariana/agent/loop.py` still occur only after the durable-marker logic paths (`completed_at` already set, marker-fixup, noop after marker write, or post-RPC marker writes). `mariana/agent/settlement_reconciler.py` has no `task.credits_settled = True` assignment. Contract intact.
- U-02: `int(... * 100)` grep in runtime code only shows the known reservation formula in `agent/api_routes.py` and docstring/comment/test references. No new billing truncation path in `mariana/` runtime logic.
- U-01: `_record_pending_reversal|stripe_pending_reversals` grep shows the same intended handlers/reconciler paths only; no obvious bypass introduced.
- U-03: `_validate_redis_url_for_vault` is kept as a compatibility wrapper, and `VaultUnavailableError` remains the fail-closed signal. Legacy substring validator logic is removed.
- Conclusion: spot-checks passed.

## Findings

### W-01 — Shared Redis transport validator not applied to global Redis client constructors
- **Severity:** P3
- **Surface:** API startup Redis client and daemon Redis client.
- **Root cause:** `mariana/api.py:337-345` and `mariana/main.py:254-262` still construct Redis clients directly with `aioredis.from_url(...REDIS_URL...)` and never call the new shared `assert_local_or_tls()` helper. The V-01 fix centralized validation for vault/cache only, but these constructors still accept remote plaintext `redis://` URLs.
- **Repro:** static-only. Configure `REDIS_URL=redis://remote.example.com:6379/0`; API startup (`mariana/api.py`) and daemon startup (`mariana/main.py`) will create and ping the client successfully with no transport-policy check, while only vault/cache callsites enforce the TLS-or-local rule.
- **Impact:** queue traffic, stop flags, pub/sub logs, and other non-vault Redis control-plane data can still traverse plaintext to a remote Redis if operators misconfigure `REDIS_URL`. This is defense-in-depth rather than a direct user-to-user exploit, but the new shared util creates an expectation of one policy for Redis transport and currently does not cover all constructors.
- **Fix sketch:** route all `REDIS_URL` client construction through a single validated factory (or call `assert_local_or_tls(config.REDIS_URL, surface=...)` before every `from_url()`), including the API lifespan client and daemon `_create_redis()` path.

RE-AUDIT #22 COMPLETE findings=1 file=loop6_audit/A27_phase_e_reaudit.md
