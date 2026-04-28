# Phase E re-audit #20

Header: model=gpt_5_4, commit=eb143aa, scope=NEW surfaces — clock/timezone, numeric precision, profile.tokens races, FIFO bucket TOCTOU, Redis vault, migration idempotency, Stripe ordering, message edit, cross-project auth, DoS, webhook rotation, T-01 spot-check.

## Surface walkthrough

### Probe 1 — Time / clock / timezone
- Repo-wide grep found no `datetime.utcnow()` / `utcnow` usage in the audited app paths. Python wall-clock writes use timezone-aware `datetime.now(tz=timezone.utc)` in task/conversation/profile timestamp patches (`mariana/agent/models.py:175-176`, `mariana/api.py:2604`, `2706`, `2755`).
- Settlement claim/reconcile timestamps are DB-clock only: `_mark_settlement_completed` and `_mark_ledger_applied` stamp `now()` in Postgres (`mariana/agent/loop.py:407-448`), and the reconciler also uses `claimed_at = now()` plus `claimed_at < now() - interval` in the same database (`mariana/agent/settlement_reconciler.py:96-117`). I found no Python-side datetime mixed into `agent_settlements.claimed_at` or `ledger_applied_at` comparisons.
- Stripe period-end / expiry writes are timezone-aware ISO strings from UTC timestamps (`mariana/api.py:5888-5919`, `5999-6013`, `6277-6279`), and ledger RPCs accept `timestamptz` `p_expires_at` (`mariana/billing/ledger.py:88-116`; live `grant_credits` def).
- Conclusion: no fresh timezone-skew bug in the requested expiry/claim windows.

### Probe 2 — Numeric precision / rounding
- `AgentTask.spent_usd` is a Python `float` (`mariana/agent/models.py:143-145`). Agent settlement converts it to credits via `final_tokens = int(task.spent_usd * 100)` (`mariana/agent/loop.py:451-467`, `522-523`).
- The legacy investigation path does the same with float markup: `total_with_markup = cost_tracker.total_spent * 1.20` and `final_tokens = int(total_with_markup * 100)` (`mariana/main.py:424-426`).
- Session costs are computed in float and rounded to 8 decimal places, not quantized to cents or stored as Decimal (`mariana/ai/session.py:320-334`). Repeated float accumulation flows into both settlement paths.
- Conclusion: finding U-02. Billing converts floating-dollar totals to integer credits by truncation, so values like 0.305 become 30 instead of 31 credits.

### Probe 3 — Race conditions on profile.tokens
- Live `grant_credits`, `refund_credits`, `spend_credits`, and `add_credits` all take `pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0))` before reading/mutating user credit state (live SQL defs fetched from Supabase project `afnbtbeayfkwznhzafay`).
- `deduct_credits` uses `SELECT ... FOR UPDATE` on `public.profiles` before computing `new_balance` and writing it back (live SQL def). `process_charge_reversal` takes its own per-charge advisory lock and then calls `refund_credits` in the same transaction (live SQL def).
- `profiles.tokens` writes are atomic SQL updates (`tokens = tokens + ...`, `tokens = GREATEST(0, tokens - ...)`, or a row-locked `new_balance`) rather than an unlocked read-compute-write path.
- Conclusion: no new linearizability bug found in the live credit RPC bodies.

### Probe 4 — TOCTOU on credit_buckets FIFO spend
- Live `spend_credits` and `refund_credits` both gate on `pg_advisory_xact_lock` per user, filter expired buckets with `expires_at IS NULL OR expires_at > clock_timestamp()`, iterate oldest-first with `ORDER BY granted_at ASC, id ASC`, and lock the candidate bucket rows `FOR UPDATE` before decrementing (`spend_credits` / `refund_credits` live defs).
- Because the same transaction both selects and updates the locked bucket rows, a bucket cannot independently drop to zero between the loop `SELECT` and `UPDATE`; concurrent spenders for the same user serialize on the advisory lock first.
- Conclusion: no fresh FIFO TOCTOU bug.

### Probe 5 — Redis dependency / secrets in vault
- Vault secrets are serialized as raw JSON and stored directly in Redis under `vault:env:{task_id}` (`mariana/vault/runtime.py:98-123`). API submission stores them before enqueue, but `store_vault_env` swallows Redis errors and `fetch_vault_env` returns `{}` on Redis miss/error (`mariana/vault/runtime.py:109-124`), so secret injection degrades silently rather than failing closed.
- The API and worker Redis clients simply call `redis.asyncio.from_url(config.REDIS_URL, ...)` (`mariana/api.py:337-345`, `mariana/main.py:254-262`). Default config is plain `redis://redis:6379/0` (`mariana/config.py:124`). Unlike `mariana/data/cache.py:421-433`, this path does not enforce `rediss://` for remote Redis.
- I did not find a direct structured-log leak of secret values in the active vault path; fetch/store failures log only `task_id` and exception text (`mariana/agent/api_routes.py:526-531`, `mariana/agent/loop.py:1144-1147`).
- Conclusion: finding U-03. Task-scoped vault secrets are allowed onto plaintext/non-enforced-auth Redis infrastructure, and Redis outages silently strip secret injection instead of aborting the task.

### Probe 6 — Migration replay / forward-only assumption
- Migration 020 uses `ADD COLUMN IF NOT EXISTS`, `DROP CONSTRAINT IF EXISTS`, and re-adds the constraint safely (`frontend/supabase/migrations/020_k01_charge_amount.sql:24-43`).
- Migration 021 uses `CREATE OR REPLACE FUNCTION`, repeat-safe `REVOKE`/`GRANT`, and invariant checks in a `DO` block; I found no naked `CREATE TABLE`, `ADD COLUMN`, or `CREATE POLICY` replay hazard in 020/021 (`frontend/supabase/migrations/021_k02_atomic_charge_reversal.sql:35-191`).
- Conclusion: no rerun-safety issue in the new loop migrations.

### Probe 7 — Stripe webhook order / out-of-order events
- The webhook dispatcher correctly supports `customer.subscription.deleted`, `charge.refunded`, `charge.dispute.created`, and `charge.dispute.funds_withdrawn` (`mariana/api.py:5688-5707`).
- But `_reverse_credits_for_charge` looks up the original grant mapping by `payment_intent_id` and, if none is found, only logs `charge_reversal_no_grant_found` then returns success (`mariana/api.py:6606-6613`). The outer webhook handler then finalizes the event as completed (`mariana/api.py:5734-5748`).
- That means an out-of-order refund/dispute that lands before the grant mapping row exists is dropped permanently instead of retried once the original payment grant arrives.
- Conclusion: finding U-01.

### Probe 8 — Conversation persistence / message ordering on edit
- I found append-only message persistence (`POST /api/conversations/messages`) but no message-edit endpoint. `SaveMessageRequest` does not accept `created_at` or sequence numbers; server ordering is by DB `created_at asc` (`mariana/api.py:707-714`, `2511-2519`, `2655-2728`).
- A malicious client can append new messages to its own conversation, but cannot reorder persisted history or mutate old message bodies through the audited API surface.
- Conclusion: no edit/reorder billing bypass found.

### Probe 9 — Auth: cross-project token + service role exposure
- `_authenticate_supabase_token` delegates verification to `GET {SUPABASE_URL}/auth/v1/user` with the bearer token and optional anon key (`mariana/api.py:1216-1234`). If operators point `SUPABASE_URL` at the wrong project, auth will follow that configured project — but the rest of the app’s Supabase reads/writes follow the same URL too, so this is a deployment-integrity assumption rather than a code-level cross-project bypass.
- I found `SUPABASE_SERVICE_KEY` only in server-side code paths and secret redaction checks; no frontend `VITE_*` exposure or client-bound response surface surfaced in the grep results.
- Conclusion: no new code-level auth leak beyond the trusted-config assumption.

### Probe 10 — DoS / resource exhaustion
- Agent loop hard-caps replans/fixes via `_HARD_MAX_FIX_PER_STEP = 5`, `_HARD_MAX_REPLANS = 3` and enforces them at runtime (`mariana/agent/loop.py:65-68`, `1135-1136`, `1290-1292`). Step count is described as capped at 25 in self-knowledge (`mariana/agent/self_knowledge.py:64-67`), but I did not see a static path in this probe showing planner output can exceed the enforced runtime step list and actually execute unbounded work.
- Tool/stream payloads are bounded in several places (`_MAX_EVENT_PAYLOAD_BYTES = 32 * 1024`, `_STEP_STDOUT_TAIL = 4000`, `_STEP_STDERR_TAIL = 4000`; `mariana/agent/loop.py:62-72`).
- Conclusion: no fresh exploitable unbounded-execution issue found in the requested sweep.

### Probe 11 — Webhook secret rotation / multiple keys
- Webhook verification tries `STRIPE_WEBHOOK_SECRET_PRIMARY` first, then `STRIPE_WEBHOOK_SECRET_PREVIOUS`, and falls back to legacy `STRIPE_WEBHOOK_SECRET` if PRIMARY is unset (`mariana/api.py:5604-5639`). Config wiring for all three vars is present (`mariana/config.py:210-217`, `377-380`).
- If the previous secret verifies, the server accepts the event and logs a warning instead of failing the rotation window (`mariana/api.py:5635-5641`).
- Conclusion: no bug; dual-secret rotation works as intended.

### Probe 12 — Spot check T-01 still intact
- `task.credits_settled = True` assignments in `mariana/agent/loop.py` remain in the same guarded locations previously audited: no-Supabase escape hatch, already-completed short-circuit, marker-fixup after `_mark_settlement_completed`, noop after `_mark_settlement_completed`, and post-RPC after durable marker writes (`mariana/agent/loop.py:511-567`, `639-661`, `776-817`).
- Reconciler only stamps `claimed_at = now()` and can force `task.credits_settled = False` before retry; it does not set True itself (`mariana/agent/settlement_reconciler.py:96-117`, `175-188`).
- Conclusion: T-01 spot-check passes.

## Findings

### U-01 — Out-of-order Stripe refund/dispute events can be permanently dropped
- **Severity:** P1
- **Surface:** Stripe webhook ordering / reversal processing
- **Root cause:** `mariana/api.py:6606-6613`, `5734-5748`
- **Repro:** Static-only. If `charge.refunded` or `charge.dispute.*` is processed before the original grant mapping row exists in `stripe_payment_grants`, `_reverse_credits_for_charge` logs `charge_reversal_no_grant_found` and returns. The outer webhook handler then finalizes the event as completed, so Stripe retries stop and the later-arriving grant is never reversed.
- **Impact:** Refunded/disputed payments can leave credits permanently granted, producing real billing loss and inconsistent ledger state on rare but legitimate out-of-order Stripe delivery.
- **Fix sketch:** Treat “grant mapping not found yet” as retriable, not success: leave the webhook event pending / return 500, or persist a pending reversal keyed by charge/payment_intent and reconcile it when the original grant mapping appears. Also consider grant-time suppression when the payment is already refunded/disputed.

### U-02 — Float-to-int truncation misprices credit settlement by up to one credit per task
- **Severity:** P3
- **Surface:** Agent settlement and legacy investigation settlement
- **Root cause:** `mariana/agent/models.py:143-145`, `mariana/agent/loop.py:522-523`, `mariana/main.py:424-426`, `mariana/ai/session.py:320-334`
- **Repro:** Static-only. `spent_usd` / `total_with_markup` are floats. A value like `0.305` becomes `int(0.305 * 100) == 30`, but cent-quantized billing should be 31. The same floor conversion is used in both agent and legacy task settlement.
- **Impact:** The ledger can undercharge or overrefund by one credit on boundary values, creating drift between platform-reported dollar usage and integer-credit debits/refunds. High-volume usage compounds the mismatch.
- **Fix sketch:** Use `Decimal` end-to-end for USD amounts and quantize to cents with an explicit rounding policy (for example `ROUND_HALF_UP`) before converting to integer credits. Avoid `float` accumulation for billable totals.

### U-03 — Vault secrets are allowed onto plaintext Redis and Redis failure degrades silently
- **Severity:** P2
- **Surface:** Task vault secret storage / Redis transport
- **Root cause:** `mariana/vault/runtime.py:98-124`, `mariana/api.py:337-345`, `mariana/main.py:254-262`, `mariana/config.py:124`
- **Repro:** Static-only. Vault env values are JSON-stored in Redis, but the API/worker accept the default `redis://redis:6379/0` path with no TLS enforcement and do not require authentication material in the URL. If Redis is unavailable, `store_vault_env` / `fetch_vault_env` degrade to no-op / `{}` and the task proceeds without fail-closed secret injection.
- **Impact:** Task-scoped secrets can traverse or reside on plaintext Redis infrastructure, and Redis faults silently change task behavior instead of aborting sensitive runs. This weakens the confidentiality guarantees of the vault feature.
- **Fix sketch:** Reuse the `data/cache.py` policy here: require `rediss://` for non-local Redis, require credentials for remote Redis, and fail task startup when `vault_env` was requested but cannot be durably stored/fetched.

RE-AUDIT #20 COMPLETE findings=3 file=loop6_audit/A25_phase_e_reaudit.md
