# Phase E re-audit #21

Header: model=claude_opus_4_7, commit=2d6a168, scope=adversarial probe of U-01/U-02/U-03 fixes + followups review + continued broader sweep.

## Surface walkthrough

### Probe 1 — U-01 fix correctness (NEW pending-reversal table)

- Migration `frontend/supabase/migrations/022_u01_stripe_pending_reversals.sql` creates `stripe_pending_reversals` with `event_id text NOT NULL UNIQUE`, partial indexes on `charge_id` / `payment_intent_id` `WHERE applied_at IS NULL`, RLS enabled, and only `service_role` granted (lines 31–60). REVOKE-then-GRANT-only-to-service_role is the correct fail-safe even without an explicit `FOR ALL USING` policy (matches `stripe_payment_grants` per U01_followup_findings #4).
- `_record_pending_reversal` (mariana/api.py:6658-6735) POSTs with `Prefer: resolution=ignore-duplicates,return=minimal`, so concurrent OOO refund replays of the same `event_id` collapse at insert time. Insert failure raises HTTP 503 → outer dispatcher will not finalize the event → Stripe retries.
- Reconciler is wired into `_grant_credits_for_event` AFTER the `stripe_payment_grants` insert succeeds and BEFORE the function returns (mariana/api.py:6273-6276). The K-02 `process_charge_reversal` RPC dedups on `reversal_key`, so two concurrent grant events that both find the same unapplied row cannot double-debit.
- **Concurrent `charge.refunded` + `charge.succeeded`:** these are sequential HTTP calls (Supabase REST + RPC), NOT one DB transaction. If `charge.refunded` lands while `_grant_credits_for_event` is between the grant_credits RPC and the `stripe_payment_grants` POST, the refund handler sees no grant row, parks a pending. Reconciler runs at end of `_grant_credits_for_event` and replays. K-02 RPC dedups via `reversal_key`. No double debit, no missed reversal. Confirmed safe.
- **Partial refund (`amount_refunded < amount`):** `_record_pending_reversal` stores the FULL Stripe charge object inside `raw_event` (api.py:6696-6707). On replay, `_reverse_credits_for_charge` reads `charge.amount` and `charge.amount_refunded` from that payload (api.py:6967-6968) and computes pro-rata via `floor(original_credits * amount_refunded / amount_total)` (api.py:7001). Partial-refund correctness preserved — only the refunded portion is reversed. OK.
- **`process_charge_reversal` failure during reconciliation:** `_reconcile_pending_reversals_for_grant` re-raises `HTTPException` (api.py:6881-6892); the outer webhook handler then returns 500 and Stripe retries the grant-creating event. The grant_credits RPC and the `stripe_payment_grants` insert both already succeeded, so on Stripe-retry `grant_credits` returns `duplicate`, mapping insert no-ops, reconciler tries again. **Caveat:** the K-02 RPC commits in its own DB transaction; if it succeeds but the surrounding HTTP returns transport error after commit, on retry it dedups via `reversal_key`. No double-debit. The grant insert is NOT rolled back on reconciler failure (different HTTP transactions) — this is acceptable because the next reconciler attempt will retry safely.
- **`applied_at` filter for re-apply:** `_mark_pending_reversal_applied` PATCHes unconditionally (api.py:6811-6815), no `applied_at=is.null` filter. A second reconcile call would still PATCH applied_at over itself — harmless idempotent overwrite. SELECT side filters via `applied_at=is.null` (api.py:6759-6761) so already-applied rows aren't re-fetched.
- **Stripe webhook replay of an already-parked OOO event:** `INSERT ... ON CONFLICT DO NOTHING` no-ops, handler returns success, Stripe stops retrying the OOO event. The grant arrival path is the retirement path. If the grant event also fails permanently (Stripe gives up after 3 days OR the user profile is deleted between checkout and processing), the pending row is stranded — already documented in `U01_followup_findings.md` #1 as "deferred operator dashboard". Not a new bug.
- **RLS on `stripe_pending_reversals`:** RLS enabled, REVOKE all roles, GRANT only `service_role`. Without an explicit policy, RLS denies all access to non-service roles — fail-safe. Mirrors existing `stripe_payment_grants` style. No issue.
- **Conclusion:** U-01 fix is structurally sound; idempotency layers are correct.

### Probe 2 — U-02 fix correctness (NEW Decimal helper)

- `mariana/billing/precision.py` (106 lines) implements `usd_to_credits(usd)` via `Decimal(str(x))` for floats (avoiding IEEE-754 surprises), `(amount * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)`, and explicit type dispatch with `bool` excluded before `int` (lines 85-102).
- **Edge case probes (tested live):**
  - `usd_to_credits(Decimal('NaN'))` → raises `ValueError: cannot convert NaN to integer` from `int(Decimal('NaN'))`. Loud failure, not silent garbage. Acceptable.
  - `usd_to_credits(Decimal('Infinity'))` → raises `decimal.InvalidOperation` at `quantize`. Loud failure.
  - `usd_to_credits(float('nan'))` → `str(nan)='nan'`, `Decimal('nan')` is `Decimal('NaN')`, then quantize+int → `ValueError`. Same loud failure path.
  - `usd_to_credits(-0.5)` → `-50`. Negative inputs are NOT clamped or rejected — the helper simply quantizes. Settlement passes `task.spent_usd` (always `>= 0` because cost accumulators only `+=`); legacy investigation path passes `total_with_markup` which can become non-positive only if `total_spent` goes negative, which would itself be a deeper bug. No new exposure here.
- **Remaining `int(x*100)` audit:** `grep -rn "int(.*\*\s*100)" mariana/` returns only docstring/comment occurrences in `billing/precision.py`, `agent/loop.py`, `main.py`, and the reservation site at `mariana/agent/api_routes.py:472`. The reservation `int(body.budget_usd * 100)` is intentionally left per U02_followup_findings F-U02-3 because the `max(100, ...)` floor guarantees the user is never under-reserved at the platform level (sub-$1 tasks pad to 100 credits). Settlement uses Decimal; any 1-cent reservation truncation is healed by the deduct-overage RPC at `_settle_agent_credits` delta>0 path.
- **Reservation vs settlement drift:** budget_usd=2.005 → reserved=int(200.5)=200; settlement spent_usd=2.005 → final=usd_to_credits=201; delta=1 → deduct overage RPC fires for 1 credit. User pays the correct 201 net. No free-credits exploit.
- **Conclusion:** U-02 fix is correct at billing boundaries. NaN/Inf raise loudly rather than silently corrupting. No exploitable drift.

### Probe 3 — U-03 fix correctness (NEW VaultUnavailableError + TLS check)

- `_validate_redis_url_for_vault` (mariana/vault/runtime.py:64-84) lowercases the URL and substring-matches against `_LOCAL_REDIS_HOST_TOKENS = ('://localhost', '://127.', '://[::1]', '://redis:', '://redis/')` (lines 55-61). If no token matches AND scheme is `redis://`, it raises `ValueError`.
- **CRITICAL — substring match bypass:** the validator does **substring** matching, not host-equality. The token `://localhost` matches the URL `redis://localhost.attacker.com:6379` — an attacker-controlled remote host containing `localhost` as a left-anchored DNS label is incorrectly classified as local and bypasses the TLS requirement. Same bypass: `redis://localhost@evil.com:6379` (URL userinfo = `localhost`, host = `evil.com`), `redis://127.0.attacker.com:6379` (label `127.0`), `redis://redis:foo@evil.com:6379` (userinfo `redis:foo`, host `evil.com`). See finding **V-01**.
- **VaultUnavailableError fail-closed in API:** `mariana/agent/api_routes.py:561-629` catches `VaultUnavailableError` and `ValueError`, refunds the reservation via `_supabase_add_credits`, stamps the agent_tasks row to `state='failed'`, and 503s. Refund is correct.
- **VaultUnavailableError fail-closed in worker:** `mariana/agent/loop.py:1182-1214` catches `VaultUnavailableError` / `ValueError` and unexpected `Exception` (when `requires_vault=True`), sets `task.state = AgentState.FAILED`, calls `_persist_task`, and `return task` — **BEFORE** the `try:` block at line 1220 whose `finally:` block at line 1418 calls `_settle_agent_credits`. Therefore the reservation is **never refunded** in the worker fail-closed path. The settlement reconciler at `mariana/agent/settlement_reconciler.py:96-117` only retries existing `agent_settlements` claim rows — `_settle_agent_credits` (which creates that row) is the only producer, and it is never called for these tasks. The reservation stays deducted from the user's balance. See finding **V-02**.
- **`requires_vault` round-trip:** `mariana/agent/api_routes.py:147` writes the column on insert; line 213 reads via `_row_get_bool(row, "requires_vault", default=False)`. Schema migration uses `ALTER TABLE ... ADD COLUMN IF NOT EXISTS requires_vault BOOLEAN NOT NULL DEFAULT FALSE` (`mariana/agent/schema.sql`), so existing rows back-fill to FALSE. For tasks created BEFORE the migration but with non-empty vault_env in Redis, the worker reload would treat `requires_vault=False` and `fetch_vault_env(..., requires_vault=False)` returns `{}` on miss — silent strip. Documented in U03_followup_findings #3 as backup-restore concern only. Not a new finding.
- **IPv6 brackets parser:** `redis://[::1]:6379/0` → substring `://[::1]` matches → local. OK. (Validator never calls urlparse — it works on substring tokens.)
- **`store_vault_env` input shape:** `validate_vault_env` (mariana/vault/runtime.py:113-139) requires every value to be `isinstance(value, str)`; raises `ValueError` otherwise. The route translates that to 422. Bytes / nested dicts cannot reach `json.dumps(dict(env))`. OK.
- **TTL vs long-running tasks:** API stores with `ttl_seconds = int(body.max_duration_hours * 3600) + 300` (api_routes.py:562). The vault floor is `_MIN_TTL_SECONDS = 600`. If a task sets `max_duration_hours=24`, TTL = 86400+300 = 86700. Tasks that run to their full max duration MAY have their secrets evicted by Redis TTL with only 5 minutes of headroom. After eviction, any subsequent `fetch_vault_env` call would raise `VaultUnavailableError` (requires_vault=True path) — but **the agent loop only fetches once at boot** (line 1176), installs into contextvars, and the secrets persist in process memory for the duration. So mid-task fetches don't happen. TTL eviction matters only on worker restart mid-task, which is also caught by the fail-closed path → task ends FAILED. The reservation-refund issue (V-02) applies here too: if a worker restart triggers re-fetch failure, the reservation is stranded.
- **Conclusion:** Two new findings — V-01 (URL validator substring bypass) and V-02 (worker vault fail-closed strands the reservation).

### Probe 4 — Followup gaps review

- **U01_followup_findings.md #1 (no reconciler for stale pending rows):** acknowledged future work, not an open exploitable bug. Not a new finding.
- **U01_followup_findings.md #2 (defensive flag heuristic for disputes is conservative):** synthesizes a refund-shaped event when only `disputed=True` is set. K-02 RPC behaviour identical, only `kind` field differs. Not a new finding.
- **U01_followup_findings.md #3 (raw_event size):** storage hygiene only. Not a new finding.
- **U01_followup_findings.md #4 (RLS without explicit policy):** intentional fail-safe; matches `stripe_payment_grants` style. Not a new finding.
- **U02_followup_findings.md F-U02-1, F-U02-2 (Decimal upstream):** in-flight log values still float, but `usd_to_credits` quantizes at billed boundary. No exploitable drift. Not a new finding.
- **U02_followup_findings.md F-U02-3 (reservation int truncation):** verified safe via `max(100, ...)` floor + deduct-overage at settlement (see Probe 2). Not a new finding.
- **U03_followup_findings.md #1 (Redis client construction in api.py / main.py not TLS-enforced):** vault SECRETS path is enforced; queue/SSE traffic remains plaintext-tolerant. Documented out-of-scope for U-03. **HOWEVER**, the substring-match weakness in V-01 means even the vault path is bypassable, which downgrades the trust placed in the vault-only enforcement model. Captured under V-01.
- **U03_followup_findings.md #2 (AUTH not required for remote hosts):** future hardening. Not a new finding by itself.
- **U03_followup_findings.md #5 (token list drift between cache.py and vault/runtime.py):** both copies share the substring-match bug. Captured under V-01.

### Probe 5 — Broader sweep on surfaces still untouched

- **Conversation deletion / cascade:** `grep` for `DELETE FROM conversations`, `ON DELETE CASCADE`, `ON DELETE RESTRICT` in migrations: cascade rules left intact since prior audits; no new code in this commit window. Out of scope for new-code probe.
- **File upload signed URL scope:** no signed-URL changes in the diff. Not in scope for this round.
- **Auth endpoint rate limiting:** no `/api/auth/*` endpoints exist in `mariana/api.py` (auth is delegated to Supabase `/auth/v1/user`). Not directly applicable.
- **Email send path:** no email send was changed in the diff (`git diff 2f2d576..2d6a168 --stat` shows no email-related files).
- **Subagent / unbounded subprocesses:** agent loop hard-caps at `_HARD_MAX_FIX_PER_STEP=5`, `_HARD_MAX_REPLANS=3`, `_MAX_EVENT_PAYLOAD_BYTES=32KB`; not regressed by this commit.
- **NEW grant-time `_record_pending_reversal` exception path:** when `_record_pending_reversal` is called from inside `_grant_credits_for_event` (defensive synthetic, api.py:6253) and Supabase is down, it raises HTTPException(503). The grant_credits RPC and stripe_payment_grants insert have ALREADY committed. On Stripe retry, both no-op; the synthetic event has a deterministic `event_id = "defensive:<ref_id>:reversal"` so insert is idempotent on retry; reconciler will pick it up next time. Safe.

### Probe 6 — T-01 spot-check

`grep -n "task.credits_settled = True" mariana/agent/loop.py mariana/agent/settlement_reconciler.py`:

- `mariana/agent/loop.py:519` — set when no Supabase config (skip-settle short circuit) — after explicit log.
- `mariana/agent/loop.py` other assignments: still at the previously-audited locations preceded by durable markers. Not regressed by this commit.
- `mariana/agent/settlement_reconciler.py:185` — sets `False` (forces retry), not `True`. Unchanged.

T-01 contract intact.

## Findings

### V-01 — Vault Redis URL TLS validator bypassable via substring match

- **Severity:** P2
- **Surface:** `mariana/vault/runtime.py:_validate_redis_url_for_vault`; same flaw mirrored in `mariana/data/cache.py:421-433`.
- **Root cause:** `mariana/vault/runtime.py:55-84` and `mariana/data/cache.py:425-433`. `_LOCAL_REDIS_HOST_TOKENS = ('://localhost', '://127.', '://[::1]', '://redis:', '://redis/')` are tested via `any(tok in u for tok in tokens)` — substring search, not hostname equality. A URL where the LEFT label of the hostname (or the userinfo) contains a local token but the actual hostname is remote bypasses TLS enforcement.
- **Repro (static-only):**
  - `redis://localhost.attacker.com:6379` → `'://localhost' in url == True` → classified local → plaintext allowed.
  - `redis://localhost@evil.com:6379` → urlparse hostname is `evil.com`, but substring `://localhost` matches userinfo → classified local.
  - `redis://127.0.attacker.com:6379` → matches `://127.` → classified local.
  - `redis://redis:secret@evil.com:6379` → URL with `redis:` userinfo prefix → matches `://redis:` → classified local.
- **Impact:** A misconfigured / malicious / mistyped REDIS_URL can route per-task vault secrets (and cached investigation data via `mariana/data/cache.py`) over plaintext to a remote attacker-controlled Redis. The U-03 fix's confidentiality guarantee is bypassable. Exploitability requires control over `config.REDIS_URL`, which is operator/env-only — typically a deployment-time mistake or a chained config-injection attack. Defense-in-depth gap; not a runtime user exploit, but the entire purpose of U-03 was transport hardening for sensitive payloads, so the bypass undoes the fix's stated goal.
- **Fix sketch:** Parse with `urllib.parse.urlparse(url)`; compare `parsed.hostname` against `{"localhost", "127.0.0.1", "::1", "redis"}` (or use `ipaddress.ip_address(parsed.hostname).is_loopback` plus the literal `"redis"` exception for docker-compose). Reject URLs where `parsed.hostname` is None. Apply the same fix in `mariana/data/cache.py` and extract to a shared helper per U03_followup_findings #5.

### V-02 — Vault fail-closed worker path strands credit reservation (no refund, no reconciler pickup)

- **Severity:** P2
- **Surface:** `mariana/agent/loop.py:run_agent_task` vault-fetch fail-closed early returns.
- **Root cause:** `mariana/agent/loop.py:1182-1214`. When `fetch_vault_env(..., requires_vault=True)` raises `VaultUnavailableError` / `ValueError`, the loop sets `task.state = AgentState.FAILED`, calls `_persist_task`, and `return task` directly — BEFORE the `try:` at line 1220 whose `finally:` block at line 1418 invokes `_settle_agent_credits`. Therefore `_settle_agent_credits` is NEVER called for these tasks. Because that helper is the sole creator of the `agent_settlements` claim row, the settlement reconciler at `mariana/agent/settlement_reconciler.py:96-112` (which only re-tries pre-existing claim rows) does NOT pick the task up either. The user's `reserved_credits` (deducted at task creation in `api_routes.py:472-474`) stays permanently locked.
- **Repro (static-only):**
  1. User submits agent task with non-empty `vault_env`. API path stores secrets to Redis OK, deducts reservation OK, returns 202. `requires_vault=True` is persisted.
  2. Redis evicts the key (memory pressure / TTL), or Redis becomes unreachable, OR the worker restarts and the per-process Redis client is broken before fetch.
  3. Worker invokes `run_agent_task` → `fetch_vault_env` raises `VaultUnavailableError`.
  4. Loop sets state=FAILED, persists, returns. `_settle_agent_credits` never runs. `agent_settlements` row never inserted. `task.credits_settled=False` permanently.
  5. Settlement reconciler (`reconcile_pending_settlements`) only retries existing claim rows where `claimed_at < now() - interval`. There is no claim row → no retry. Reservation is permanently locked.
- **Impact:** Every vault-task whose worker fail-closes after the API-side store succeeded forfeits the user's reserved credits with no recovery path. For the canonical 100-credit floor, that is at least $1 per failed task. Compounds across users on any Redis incident. Symmetric API-side fail-closed (api_routes.py:561-629) DOES refund — only the worker-side path leaks. The fix report (U03_FIX_REPORT.md §"Error-surface contract") documents the API behaviour but does not mention worker-side refund semantics.
- **Fix sketch:** Either (a) move the early-return inside the try-block so the finally-block's settlement runs (set state=FAILED, then `raise` an internal sentinel or restructure the control flow), or (b) explicitly `_supabase_add_credits` for `task.reserved_credits` before the `return task` in each of the three vault-fail branches (mirrors the API-side refund pattern). Option (a) is more robust because it routes through `_settle_agent_credits` which already handles the `delta = 0 - reserved < 0` case via the idempotent `grant_credits` refund RPC.

RE-AUDIT #21 COMPLETE findings=2 file=loop6_audit/A26_phase_e_reaudit.md
