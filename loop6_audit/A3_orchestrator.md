# A3 Orchestrator Audit — Loop 6

## Severity Summary

| Severity | Count |
|----------|-------|
| P0       | 0     |
| P1       | 0     |
| P2       | 3     |
| P3       | 5     |
| P4       | 0     |
| **Total**| **8** |

---

## BUG-01..16 Verification Table

| ID     | Status                   | Notes |
|--------|--------------------------|-------|
| BUG-01 | **FIXED**                | `run()` now declares `cost_tracker=None, shutdown_flag=None` params (event_loop.py lines 181–183). |
| BUG-02 | **FIXED**                | `config.DATA_ROOT` used at line 439; `config.data_root` never referenced in current code. |
| BUG-03 | **PARTIALLY FIXED**      | `getattr(config, "BUDGET_BRANCH_HARD_CAP", 75.0)` correct attr name. Internal CostTracker still created if `None` passed, but that is the intended fallback path. |
| BUG-04 | **FALSE POSITIVE (retracted)** | asyncpg.Pool proxies `.fetch()`, `.fetchrow()`, `.execute()` directly; no `.acquire()` needed. Retracted in BUG_AUDIT.md line 65. |
| BUG-05 | **FIXED**                | `main.py._create_db_pool` passes `dsn=config.POSTGRES_DSN, min_size=..., max_size=...` individually. |
| BUG-06 | **FIXED**                | `spawn_model` signature matches all event_loop call sites; return value unpacked as `(parsed_output, session)` correctly. |
| BUG-07 | **FIXED**                | `config.py` line 178: `FRED_API_KEY: str = ""` present. |
| BUG-08 | **FIXED**                | `config.py` line 179: `DEEPSEEK_API_KEY: str = ""` present. |
| BUG-09 | **FIXED**                | `event_loop.py` imports `generate_report`; `generator.py` exports `generate_report` at line 136. |
| BUG-10 | **FIXED**                | `db.py` line 281: `CREATE TABLE IF NOT EXISTS report_generations` present in `_SCHEMA_SQL`. |
| BUG-11 | **FALSE POSITIVE (retracted)** | asyncpg.Pool does proxy fetch/execute. Retracted in BUG_AUDIT.md line 65. |
| BUG-12 | **FIXED**                | `branch_manager.py` line 173: `json.dumps(branch.score_history)` used. |
| BUG-13 | **FIXED**                | `grant_budget` handles both `str` and `list` for `grants_log` (lines 536–541). |
| BUG-14 | **FIXED**                | `handle_evaluate` line 1911: `new_score = float(eval_output.score)` — no `* 10.0` multiplier present. |
| BUG-15 | **FIXED**                | `run()` line 443: `if shutdown_flag is not None and shutdown_flag.is_set():` checked in main loop. |
| BUG-16 | **FIXED**                | No duplicate `(CHECKPOINT, DIMINISHING_RETURNS)` entry in current `state_machine.py` TRANSITION_TABLE. |
| BUG-17 | **NOT VERIFIED**         | Missing `__init__.py` files — out of scope for orchestrator lens. |
| BUG-18 | **NOT VERIFIED**         | `requirements.txt` completeness — out of scope for orchestrator lens. |

---

## New Findings (YAML)

```yaml
- id: A3-01
  severity: P2
  category: correctness
  surface: orchestrator
  title: branch_manager imports non-existent `get_config` — config thresholds always silently use hardcoded defaults
  evidence:
    - file: /home/user/workspace/mariana/mariana/orchestrator/branch_manager.py
      lines: 48-53
      excerpt: |
        def _cfg_val(attr: str, default: float) -> float:
            """Read a config value from AppConfig, falling back to default."""
            try:
                from mariana.config import get_config  # noqa: PLC0415
                return float(getattr(get_config(), attr, default))
            except Exception:
                return default
    - file: /home/user/workspace/mariana/mariana/config.py
      lines: 238
      excerpt: |
        def load_config(env_file: str | Path | None = None) -> AppConfig:
        # No `get_config` function exists anywhere in config.py
    - reproduction: |
        python3 -c "from mariana.config import get_config"
        # ImportError: cannot import name 'get_config' from 'mariana.config'
        # _cfg_val catches this via bare `except Exception` and returns the
        # module-level default (e.g. 75.0 for BUDGET_BRANCH_HARD_CAP).
        # _load_config_thresholds() is called on every score_branch() entry
        # but always silently falls back — operator overrides via AppConfig
        # (e.g. SCORE_KILL_THRESHOLD, SCORE_DEEPEN_THRESHOLD) are never read.
  blast_radius: |
    Every call to score_branch() calls _load_config_thresholds() which calls
    _cfg_val() for six config attributes: SCORE_KILL_THRESHOLD,
    SCORE_DEEPEN_THRESHOLD, SCORE_TRIBUNAL_THRESHOLD, BUDGET_BRANCH_INITIAL,
    BUDGET_BRANCH_GRANT_SCORE7, BUDGET_BRANCH_GRANT_SCORE8. All six always
    resolve to their module-level defaults. Operator configuration of these
    thresholds via environment variables or AppConfig has zero effect on
    running investigations. This is a silent misconfiguration: no error is
    ever raised, and the system functions normally but ignores operator tuning.
    Affects every investigation on every deployment.
  proposed_fix: |
    Replace `from mariana.config import get_config` with the actual exported
    function. Either: (a) add `def get_config() -> AppConfig: return
    load_config()` to config.py and call it here, or (b) change the import
    to `from mariana.config import load_config` and call `load_config()` in
    _cfg_val. Option (b) is simpler. Note that load_config() reads env vars
    on each call, which is correct here since thresholds should reflect the
    current environment. If repeated calls are expensive, cache the result
    with a module-level singleton pattern (with a reset hook for tests).
  fix_type: api_patch
  test_to_add: |
    test_cfg_val_reads_appconfig: set SCORE_KILL_THRESHOLD env var to a
    non-default value, call _load_config_thresholds(), assert
    SCORE_KILL_THRESHOLD module constant equals the env-var value.
    Failure mode: constant equals the hardcoded default instead.
  blocking: [none]
  confidence: high

- id: A3-02
  severity: P2
  category: availability
  surface: orchestrator
  title: Module-level `_metadata_lock` serializes all concurrent investigations on a single asyncio.Lock
  evidence:
    - file: /home/user/workspace/mariana/mariana/orchestrator/event_loop.py
      lines: 99-102
      excerpt: |
        # BUG-0037 fix: asyncio.Lock for metadata counter read-modify-write operations.
        # Prevents lost updates when concurrent coroutines increment _tribunal_run_counter
        # or _skeptic_run_counter simultaneously.
        _metadata_lock = asyncio.Lock()
    - file: /home/user/workspace/mariana/mariana/orchestrator/event_loop.py
      lines: 2246-2247
      excerpt: |
        # BUG-0037 fix: wrap read-modify-write in asyncio.Lock to prevent lost updates.
        async with _metadata_lock:
    - file: /home/user/workspace/mariana/mariana/orchestrator/event_loop.py
      lines: 2504-2505
      excerpt: |
        # BUG-0037 fix: wrap read-modify-write in asyncio.Lock to prevent lost updates.
        async with _metadata_lock:
    - reproduction: |
        Start daemon mode with 4 parallel investigations. Each time
        _trigger_for_tribunal or _trigger_for_skeptic is called, all four
        coroutines queue behind the single module-level lock. Any slow DB
        await inside the locked section blocks the other three. Under normal
        asyncio scheduling this means only one investigation can increment its
        tribunal/skeptic counter at a time.
  blast_radius: |
    In daemon mode or any multi-task deployment, all concurrent run()
    coroutines contend on a single process-wide asyncio.Lock whenever a
    tribunal or skeptic trigger fires. The lock is acquired around DB
    read-modify-write operations (lines 2246, 2504), which involve at least
    one await. Under 4-task concurrency, three tasks stall while one holds
    the lock. This degrades throughput for tribunal/skeptic dispatch and can
    cause latency spikes proportional to the number of concurrent tasks. The
    original per-task counter correctness goal could be achieved with a per-
    task lock stored in the task's metadata dict, avoiding cross-task
    contention entirely.
  proposed_fix: |
    Replace the module-level `_metadata_lock = asyncio.Lock()` with a per-task
    lock. One approach: store the lock in the task metadata dict at task init
    (e.g. `task.metadata["_meta_lock"] = asyncio.Lock()`) and retrieve it
    inside _trigger_for_tribunal / _trigger_for_skeptic. Alternatively, since
    these counters are only incremented by the single run() coroutine for a
    given task_id (asyncio is single-threaded), the module-level counter
    increment itself may not need a lock at all if the counter is keyed by
    task_id in a dict; the lock is only needed when two coroutines for the
    same task_id could run concurrently, which does not happen with the
    current one-task-per-loop design.
  fix_type: api_patch
  test_to_add: |
    test_concurrent_tribunal_triggers_no_contention: run 4 simulated
    investigations concurrently, each firing tribunal trigger simultaneously;
    assert total wall-clock time is not significantly greater than a single
    trigger's time. Failure mode: total time ~4× single trigger due to
    serialization.
  blocking: [none]
  confidence: high

- id: A3-03
  severity: P2
  category: money
  surface: orchestrator
  title: Atomic credit probe calls `add_credits` with wrong RPC parameter names — probe refund always fails silently
  evidence:
    - file: /home/user/workspace/mariana/mariana/orchestrator/event_loop.py
      lines: 3353-3356
      excerpt: |
        for _attempt in range(3):
            try:
                await _rpc("add_credits", {"target_user_id": user_id, "amount": 1})
                refund_ok = True
    - file: /home/user/workspace/mariana/loop5_research/live_rpc_bodies.json
      lines: live DB — add_credits function definition
      excerpt: |
        CREATE OR REPLACE FUNCTION public.add_credits(p_user_id uuid, p_credits integer)
        -- Parameters are: p_user_id (not target_user_id), p_credits (not amount)
    - file: /home/user/workspace/mariana/mariana/orchestrator/event_loop.py
      lines: 3334-3335
      excerpt: |
        status, body = await _rpc(
            "deduct_credits", {"target_user_id": user_id, "amount": 1}
        )
        # deduct_credits(target_user_id uuid, amount integer) — MATCHES live DB.
        # Only add_credits has the mismatch.
    - reproduction: |
        # Live DB add_credits signature (from live_rpc_bodies.json):
        #   public.add_credits(p_user_id uuid, p_credits integer)
        # Code sends:
        #   {"target_user_id": user_id, "amount": 1}
        # PostgREST will raise:
        #   42883: function add_credits(target_user_id => uuid, amount => integer)
        #          does not exist
        # The except Exception block catches this and increments _attempt.
        # All 3 retry attempts fail with the same error.
        # refund_ok remains False; logger.error is called but 1 credit is lost.
  blast_radius: |
    Every time the orchestrator calls _atomic_probe_credits() to check whether
    a user has sufficient balance (H-09 fix path), it deducts 1 credit via
    deduct_credits (which succeeds — correct param names), then attempts to
    refund it via add_credits (which always fails — wrong param names). The
    1-credit refund fails on all three retry attempts. The user permanently
    loses 1 credit per credit-check probe. Depending on how frequently the
    probe is called per investigation, this could accumulate to tens of credits
    lost per run per user. The error is logged at ERROR level but no alerting
    or reconciliation is triggered. Because deduct_credits uses the legacy
    profiles.tokens column (not the credit_buckets ledger), and add_credits
    also operates on profiles.tokens, this also widens the R6 ledger drift
    (profiles.tokens vs credit_buckets) on every probe call.
  proposed_fix: |
    Change the refund call parameters to match the live function signature.
    In event_loop.py around line 3353, replace:
      await _rpc("add_credits", {"target_user_id": user_id, "amount": 1})
    with:
      await _rpc("add_credits", {"p_user_id": user_id, "p_credits": 1})
    Also audit the deduct call (line 3335): deduct_credits currently takes
    `target_user_id` and `amount` which do match live, but note that
    deduct_credits is the legacy function operating on profiles.tokens rather
    than the credit_buckets ledger. Consider migrating the probe to use
    spend_credits + refund_credits (both operate on the proper ledger) once
    the R6 ledger migration is complete.
  fix_type: api_patch
  test_to_add: |
    test_atomic_probe_refund_uses_correct_param_names: mock the _rpc helper,
    call _atomic_probe_credits with a valid user_id, assert that the add_credits
    call receives {"p_user_id": ..., "p_credits": 1}. Failure mode: call
    receives {"target_user_id": ..., "amount": 1}, causing a Postgres 42883
    error in production.
  blocking: [none]
  confidence: high

- id: A3-04
  severity: P3
  category: performance
  surface: orchestrator
  title: FRED connector includes API key in cache key — cross-deployment cache miss when key differs
  evidence:
    - file: /home/user/workspace/mariana/mariana/connectors/fred_connector.py
      lines: 70-93
      excerpt: |
        def _base_params(self, extra: dict | None = None) -> dict:
            """Return common query params including file_type and optional api_key."""
            params: dict = {"file_type": "json"}
            if self._api_key:
                params["api_key"] = self._api_key   # key included in params dict
            ...
            merged = self._base_params(params)
            cache_key = self._cache_key("fred", url, str(sorted(merged.items())))
            # api_key is present in merged, so two deployments with different
            # FRED_API_KEY values produce different cache keys for identical requests.
    - reproduction: |
        Deploy two instances (e.g. staging and production) pointing at the same
        Redis. Each uses a different FRED_API_KEY. Both request the same series
        (e.g. /series/observations?series_id=GDP). The cache keys differ because
        the api_key value is included in the hash input. Neither instance benefits
        from the other's cached response.
  blast_radius: |
    In a multi-instance or blue-green deployment sharing a Redis cache, every
    instance with a distinct FRED_API_KEY will have 100% cache misses for all
    FRED requests already cached by another instance. Cache hit rate halves per
    additional key in rotation. This is a performance and cost issue (extra FRED
    API calls) rather than a security issue (the API key is hashed, not stored
    in plaintext in Redis). Single-instance deployments are unaffected.
  proposed_fix: |
    Exclude the api_key from the cache key. In FredConnector._get(), compute the
    cache key from a params dict that omits the api_key:
      cache_params = {k: v for k, v in merged.items() if k != "api_key"}
      cache_key = self._cache_key("fred", url, str(sorted(cache_params.items())))
    The actual HTTP request still uses `merged` (which includes the api_key).
    This change makes the cache key depend only on the logical query, not the
    credential.
  fix_type: api_patch
  test_to_add: |
    test_fred_cache_key_excludes_api_key: create two FredConnector instances
    with different FRED_API_KEY values, call _get() with identical params on
    both, assert both produce the same cache_key string. Failure mode: keys
    differ because api_key is part of the hash input.
  blocking: [none]
  confidence: high

- id: A3-05
  severity: P3
  category: integrity
  surface: orchestrator
  title: URL content cache is global (no task/user isolation) — stale financial data from one investigation served to another
  evidence:
    - file: /home/user/workspace/mariana/mariana/data/cache.py
      lines: 68-84
      excerpt: |
        _URL_CACHE_PREFIX = "mariana:url:"

        def _url_cache_key(url_hash: str) -> str:
            return f"{_URL_CACHE_PREFIX}{_hash_url(url_hash)}"
        # No task_id or user_id in the key.
        # Query dedup keys DO include task_id (line 84-85):
        def _query_dedup_key(task_id: str) -> str:
            return f"{_QUERY_DEDUP_PREFIX}{_sanitize_key_component(task_id)}"
        # But URL content keys do not.
    - reproduction: |
        Investigation A (user X, topic "Apple Q1 2026 earnings") fetches
        https://example-news.com/apple-earnings at T=0. Content cached under
        mariana:url:<hash(url)> with TTL.
        Investigation B (user Y, same topic) starts at T=5s. Same URL is in
        cache. B receives A's fetched content — which may have been stale for
        A and is definitely stale for B if the page has since updated.
        For live exchange feeds or breaking news, the TTL may be many minutes.
  blast_radius: |
    All investigations (regardless of user, task, or timing) share a single
    URL content cache keyed only by URL hash. For slowly changing content
    (SEC filings, government data) this is desirable and intentional. For
    time-sensitive sources (exchange price feeds, news articles, earnings
    releases), an investigation started minutes after another may receive
    the earlier investigation's cached page content. In financial research
    context, stale price or news data could produce incorrect analysis. The
    impact is bounded by cache TTL and by the fraction of sources that are
    time-sensitive. The query dedup cache (mariana:qdedup:) is correctly
    isolated by task_id; only the URL content cache is shared.
  proposed_fix: |
    For time-sensitive source types, either skip the cache or add a short TTL.
    Two approaches: (a) in URLCache.get/set, accept an optional source_type
    parameter; for types in {EXCHANGE, NEWS, EARNINGS} use TTL=0 (skip cache)
    or TTL=60s; for FILING/GOVERNMENT keep the existing long TTL. (b) Add
    task_id to the key only for time-sensitive types. Approach (a) is simpler.
    The connector base class already classifies sources, so source_type is
    available at the call site.
  fix_type: api_patch
  test_to_add: |
    test_url_cache_no_cross_task_stale_for_news: mock a NEWS-type URL fetch,
    populate cache from task A, then retrieve from task B with a different
    task_id; assert cache miss (or expired) for NEWS type. Failure mode:
    task B receives task A's cached stale content.
  blocking: [none]
  confidence: medium

- id: A3-06
  severity: P3
  category: security
  surface: orchestrator
  title: Vault KDF allows client-supplied `kdf_iterations=1` — server enforces no minimum above 1
  evidence:
    - file: /home/user/workspace/mariana/mariana/vault/router.py
      lines: 84-87
      excerpt: |
        # Optional KDF tuning (defaults match m=64MiB/t=3/p=4)
        kdf_memory_kib: int = Field(default=65536, ge=16384, le=1048576)
        kdf_iterations: int = Field(default=3, ge=1, le=16)
        kdf_parallelism: int = Field(default=4, ge=1, le=16)
        # ge=1 for kdf_iterations means a client can send kdf_iterations=1
        # combined with kdf_memory_kib=16384 (minimum allowed).
    - reproduction: |
        POST /vault/create with body:
          {"kdf_iterations": 1, "kdf_memory_kib": 16384, "kdf_parallelism": 1, ...}
        Server accepts this. Vault is created with argon2id(m=16384, t=1, p=1).
        OWASP recommends minimum m=19456 KiB, t=2, p=1 (2023 cheatsheet).
        The parameters chosen here are below the OWASP minimum for time (t=1 < 2).
        Server stores the client-supplied params and uses them for verification.
  blast_radius: |
    A malicious or buggy client can create a vault with deliberately weak KDF
    parameters (t=1, m=16384). The server stores these params alongside the
    vault and uses them faithfully on every unlock attempt. A compromised vault
    blob becomes far easier to brute-force offline because the argon2id cost is
    a fraction of the recommended minimum. Since the server trusts client-
    supplied KDF params without a floor, this is a design-level security
    weakness. Impact is limited to vaults explicitly created with minimum params
    — the default (t=3, m=64MiB) is OWASP-compliant. Could be downgraded to P4
    if vault creation is restricted to authenticated admins only.
  proposed_fix: |
    Raise the Pydantic Field lower bound for kdf_iterations from ge=1 to ge=2
    (OWASP 2023 minimum). Also raise kdf_memory_kib from ge=16384 to ge=19456
    to match OWASP's recommended floor. Add a server-side validation step in
    the create endpoint (after model parsing) that rejects any parameter
    combination below the OWASP floor, returning HTTP 422 with a clear error
    message. This ensures that even if the Pydantic bounds are relaxed in future,
    the server enforces the security policy independently.
  fix_type: api_patch
  test_to_add: |
    test_vault_create_rejects_weak_kdf: POST /vault/create with kdf_iterations=1;
    assert HTTP 422 is returned. Failure mode: HTTP 200 accepted, vault created
    with substandard KDF parameters.
  blocking: [none]
  confidence: high

- id: A3-07
  severity: P3
  category: security
  surface: orchestrator
  title: Browser pool server has no authentication on `/dispatch` or `/pool/status` endpoints
  evidence:
    - file: /home/user/workspace/mariana/mariana/browser/pool_server.py
      lines: 108-200
      excerpt: |
        @app.get("/health")
        async def health() -> JSONResponse: ...   # no auth check

        @app.post("/dispatch", ...)
        async def dispatch_task(task: BrowserTask) -> DispatchResponse: ...
        # No token validation, no Authorization header check, no IP allowlist.

        @app.get("/pool/status", ...)
        async def pool_status() -> JSONResponse: ...
        # No auth check.
    - file: /home/user/workspace/mariana/mariana/browser/pool_server.py
      lines: 21
      excerpt: |
        BROWSER_POOL_HOST   — bind address (default: 127.0.0.1; set explicitly to
        # Default is localhost-only. Production deployment notes suggest this
        # may be changed via env var.
    - reproduction: |
        # With default BROWSER_POOL_HOST=127.0.0.1, from the same host:
        curl http://127.0.0.1:8888/pool/status
        # Returns pool metrics with no auth required.
        curl -X POST http://127.0.0.1:8888/dispatch -d '{"url":"http://internal-service/secret","task_id":"x"}'
        # Returns 503 today (prototype), but in production would dispatch a
        # browser task to any URL reachable from the pool server's network.
  blast_radius: |
    Currently the server only binds to 127.0.0.1 by default, so exposure is
    limited to processes running on the same host (or container). However,
    the BROWSER_POOL_HOST env var is explicitly documented as overridable to
    a public address. When the production Playwright implementation is
    activated, any process that can reach the port (SSRF from another service,
    container network exposure, or misconfigured BROWSER_POOL_HOST) can
    dispatch arbitrary browser tasks to any URL, exfiltrate page content, or
    probe internal services. The /pool/status endpoint also leaks operational
    metrics without auth. The risk is latent (prototype returns 503 today)
    but the production activation path has no auth gate.
  proposed_fix: |
    Add a shared-secret token check to all non-health endpoints. The orchestrator
    and pool server should share a BROWSER_POOL_TOKEN env var (generated at
    deploy time). Add a FastAPI dependency that reads the X-Browser-Pool-Token
    header and returns HTTP 401 if it does not match. Apply this dependency to
    /dispatch and /pool/status. The /health endpoint may remain unauthenticated
    for load balancer checks. This should be implemented before the Playwright
    pool goes live; the prototype's 503 behavior masks but does not fix the
    missing auth.
  fix_type: api_patch
  test_to_add: |
    test_dispatch_requires_token: POST /dispatch without X-Browser-Pool-Token
    header; assert HTTP 401. POST with wrong token; assert HTTP 401. POST with
    correct token; assert 503 (prototype) or 200 (production). Failure mode:
    unauthenticated request returns 503/200 instead of 401.
  blocking: [none]
  confidence: high

- id: A3-08
  severity: P3
  category: integrity
  surface: cross
  title: DB `total_spent_usd` stores raw model cost (not 1.20× markup) — diverges from credit settlement amount shown to users
  evidence:
    - file: /home/user/workspace/mariana/mariana/orchestrator/event_loop.py
      lines: 3131-3133
      excerpt: |
        def _sync_cost(task: ResearchTask, cost_tracker: CostTracker) -> None:
            task.total_spent_usd = cost_tracker.total_spent   # RAW cost, no markup
            task.ai_call_counter = cost_tracker.call_count
    - file: /home/user/workspace/mariana/mariana/orchestrator/event_loop.py
      lines: 3161
      excerpt: |
        total_spent_usd = $3,   # _persist_task writes task.total_spent_usd (raw)
    - file: /home/user/workspace/mariana/mariana/orchestrator/event_loop.py
      lines: 628-632
      excerpt: |
        # Use total_with_markup (raw cost × 1.20) so the frontend shows
        # the credit-equivalent spend.
        "spent_usd": round(cost_tracker.total_with_markup, 4),   # 1.20× markup
        ...
        "raw_spent_usd": round(cost_tracker.total_spent, 4),     # raw cost
    - file: /home/user/workspace/mariana/mariana/orchestrator/cost_tracker.py
      lines: 402-404
      excerpt: |
        def total_with_markup(self) -> float:
            """Total USD spent including 20% platform markup."""
            return self.total_spent * 1.20
    - reproduction: |
        Run an investigation costing $1.00 raw model spend.
        - DB research_tasks.total_spent_usd = 1.00 (raw)
        - Frontend WebSocket "spent_usd" = 1.20 (markup)
        - Credit settlement = 1.20 credits deducted
        Operator SQL query: SELECT total_spent_usd FROM research_tasks
        returns 1.00, understating the actual charge to the user by 20%.
  blast_radius: |
    All research_tasks records store raw model cost (pre-markup) in
    total_spent_usd while the user is charged 1.20× that amount in credits.
    Operators or analytics queries reading research_tasks.total_spent_usd will
    systematically understate user charges by 20%. Reconciliation between
    DB spend records and credit ledger deductions will show a permanent 20%
    discrepancy. Revenue reporting based on total_spent_usd will undercount
    actual credit consumption. This may be an intentional design (raw cost for
    internal accounting, markup computed at settlement) but is undocumented and
    inconsistent with the field name "total_spent_usd" which implies the user-
    facing amount. Confidence could be downgraded to low if the design intent
    is confirmed to store raw cost only.
  proposed_fix: |
    One of two options: (a) Store the markup amount in the DB by changing
    _sync_cost to set `task.total_spent_usd = cost_tracker.total_with_markup`.
    This aligns the DB column with what the user actually pays. Add a separate
    `raw_cost_usd` column if internal cost tracking needs the pre-markup figure.
    (b) Document the current behavior explicitly — rename the DB column to
    `raw_cost_usd` and add a `charged_usd` computed column (raw × 1.20) or
    view. Update all downstream queries accordingly. Option (a) is simpler
    but is a schema migration if the column type needs no change (it does not
    — both values are float).
  fix_type: api_patch
  test_to_add: |
    test_total_spent_usd_reflects_markup: run a task with a known cost, check
    research_tasks.total_spent_usd equals cost × 1.20 (if option a) or equals
    raw cost with separate charged_usd = cost × 1.20 (if option b). Failure
    mode: DB shows raw cost while user is charged markup, causing 20%
    reconciliation gap.
  blocking: [none]
  confidence: medium
```

---

## Methodology Gaps

The following areas were not fully covered by this lens and should be verified by other agents or a follow-up pass:

1. **`tribunal/adversarial.py` caller authentication**: The tribunal module itself has no auth logic (it is called internally by the orchestrator), but the API routes that expose tribunal results to external callers were not audited here. A2 (API lens) should verify those routes.

2. **`sandbox/` module** (if any): No `mariana/sandbox/` directory was found during the audit. If a sandbox or code-execution module exists under a different path, it was not reviewed.

3. **`api.py` routes invoking `_atomic_probe_credits`**: The A3-03 finding identifies the wrong param names in the orchestrator's internal probe. The API lens (A2) should confirm whether `api.py` makes any direct RPC calls to `add_credits` / `deduct_credits` with independent param name issues.

4. **BUG-17 / BUG-18** (`__init__.py`, `requirements.txt`): These were declared out of scope for the orchestrator lens. A general build/packaging audit should verify these.

5. **`deduct_credits` legacy ledger path**: Both A3-03 and R6 touch the fact that `deduct_credits` operates on `profiles.tokens` rather than `credit_buckets`. The full R6 remediation plan is tracked separately but the probe code should be migrated to `spend_credits` + `refund_credits` as part of that work.
