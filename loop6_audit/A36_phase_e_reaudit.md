# A36 — Phase E Re-audit #31 (Loop 6 zero-bug convergence)

- **Audit number:** A36
- **Auditor model:** gpt_5_4 (delegated; Claude Opus 4.7 executor)
- **Branch / HEAD:** `loop6/zero-bug` @ `4537de8`
- **Date:** 2026-04-28
- **Streak entering:** 1/3 (A35 zero)

---

## Section 1 — AA-01 / W-X-Y-Z fresh-angle re-probe

### Methodology

1. Read the actual SQL function definitions of `grant_credits` (latest in
   `frontend/supabase/migrations/009_f03_refund_debt.sql:199+`) and
   `refund_credits` (latest in
   `frontend/supabase/migrations/009_f03_refund_debt.sql:72-192`).
2. Read the `credit_transactions` schema (`002_deft_credit_ledger.sql:48-60`)
   and the UNIQUE-index definition (`004b_credit_tx_idem_concurrent.sql:22-26`).
3. Verified live function definition via `pg_get_functiondef` against the
   local Postgres baseline.
4. Walked the AA-01 orphan path against multi-bucket refund scenarios.

### Angles considered

| Angle | Result |
|-------|--------|
| `grant_credits` dedup contract | Per-user advisory lock (`pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0))` line 231), then SELECT existing tx by `(type='grant', ref_type, ref_id)`. Returns `status='duplicate'` with the existing transaction id if found. Single INSERT into `credit_transactions` per call (no loop). ✓ |
| `refund_credits` dedup contract | Per-user advisory lock at line 101. SELECTs existing `credit_transactions` AND existing `credit_clawbacks` by `(type='refund', ref_type, ref_id)` / `(ref_type, ref_id)` respectively. **Then iterates over buckets in a FIFO loop and inserts a `credit_transactions` row PER BUCKET TOUCHED with the same `(p_ref_type, p_ref_id, type='refund')`.** | **BB-01 (P2)** — see findings. |
| `credit_transactions` UNIQUE coverage matrix | `uq_credit_tx_idem ON (ref_type, ref_id, type) WHERE type IN ('grant','refund','expiry') AND ref_type IS NOT NULL AND ref_id IS NOT NULL`. (a) grant + refund with same `(ref_type, ref_id)` are different rows — allowed. (b) Two grants with same key — second blocked (correct dedup). (c) Two refunds with same key — second blocked. **This is the bug:** the function inserts multiple refund rows per call. |
| AA-01 orphan reconciler interaction | Orphan path inserts NO claim row (FK violation skipped the INSERT entirely). The reconciler's candidate SELECT requires a row with `completed_at IS NULL`. Orphan tasks are never reconciler-eligible — they rely on the live ledger's `(type, ref_type, ref_id)` dedup as the sole idempotency anchor. Documented in AA01_FIX_REPORT §6 ("No NestD migration needed... the keyed RPC's `(ref_type, ref_id)` UNIQUE in `credit_transactions` is the only durable idempotency anchor for orphan-refund tasks"). ✓ |
| RPC version mismatch (older grant_credits returning `'ok'`) | Code at `mariana/main.py:702-722` only inspects `resp.status_code`, not body['status']. So both `'duplicate'` and `'granted'` and a hypothetical `'ok'` would all be accepted as success. No version-skew defect. | NONE |
| Daemon retry of orphan path | First call: claim INSERT FK-violation → "orphan" → keyed RPC → 200 (or 'duplicate' on retry). Same `_ref_id = task_id` keeps the dedup tight. On daemon SIGKILL / restart, the .running resume re-enters orphan path with same keys. ✓ | NONE |
| `clear_test_data` bypass | `grep clear_test_data` returns no matches — no test cleanup function that would bypass the `credit_transactions` UNIQUE constraint. Tests that directly DELETE rows do so explicitly per test (e.g., the AA-01 test's setup) and respect the index on re-insert. | NONE |
| Reconciler orphan-path interaction | Orphan tasks never picked up by reconciler (no claim row exists). The `(ref_type, ref_id, type)` UNIQUE in `credit_transactions` is the durable idempotency anchor across daemon retries. | NONE |
| W-01 / X-01 / Y-01 / Z-01 / Z-02 grep re-verify | All four grep checks (from A35) re-confirmed: no new direct `redis.from_url` callsite, only one `Limiter()` instance with validated URL, no new non-idempotent ledger callsites in research path, cascade list complete with `research_settlements`, redirect allowlist correctly derived from CORS list. | NONE |

---

## Section 2 — New-surface sweep

| # | Surface | Probed | Result |
|---|---------|--------|--------|
| 1 | Stripe checkout amount mismatch (test-mode, currency) | The webhook handler at `api.py:5586+` reads `amount_total` (cents) via `event["data"]["object"].get("amount_total")` and the plan map keys on `stripe_price_id` (env-configured). If a user pays a non-USD currency, Stripe reports `amount_total` in the smallest unit of that currency. Mariana's plan-map credits-per-month is fixed regardless of currency (`_PLANS[*]["credits_per_month"]` is a static int). A non-USD checkout would still grant the documented credit count — by-design for a single-currency platform. | NONE |
| 2 | Currency normalization | All Mariana code assumes USD — `budget_usd`, `cost_tracker.total_with_markup`, `usd_to_credits`. No currency-conversion table. Acceptable for a single-currency product; future multi-currency support would require a follow-up. Out-of-scope for this audit. | NONE (out of scope) |
| 3 | Plan tier definition source of truth | `_PLANS` and `_TOPUPS` lists at `api.py:1841-1908` are the canonical source. Stripe price IDs come from env vars. There is no DB-side plan table. Drift between code and Stripe dashboard is operator responsibility. | NONE |
| 4 | Three-way refund/dispute idempotency (K-02 + U-01 + stripe_payment_grants) | Verified in U-01 fix report and A29 row 8. UNIQUE on `event_id` per table; replays return early. ✓ | NONE |
| 5 | FastAPI BackgroundTasks vs daemon reconciler double-fire | No FastAPI BackgroundTasks in the API code. All async work routes through the daemon's asyncio loop. Reconciler runs only inside the daemon process. | NONE |
| 6 | Redis pubsub message ordering for kill signal | Single publisher (API DELETE handler), single subscriber pattern (orchestrator inside daemon). Redis guarantees per-channel FIFO from one publisher to subscribers. | NONE |
| 7 | CSRF on `DELETE /api/investigations/{task_id}` | All state-changing endpoints require Bearer JWT; CSRF cookies are not used. Authorization happens via `Depends(_get_current_user)` before any DB write. Owner check at `api.py:3565-3566`. | NONE |
| 8 | Auth check ordering inside `delete_investigation` | Owner check happens BEFORE cascade DELETE (lines 3549-3566 own-check, 3621+ cascade). A non-owner attacker cannot trigger any DELETE side effect. ✓ | NONE |
| 9 | `research_settlements` row visibility before commit | The `_claim_research_settlement` INSERT runs inside `db.acquire()` autocommit (no explicit transaction wrapping). The row is committed at the close of `async with` block. Subsequent reads in the same `_deduct_user_credits` call use a different connection from the pool — they see the just-committed row. ✓ | NONE |
| 10 | Admin endpoints bypassing ownership | All `/api/admin/*` endpoints declare `Depends(_require_admin)`. No admin route writes to research_tasks without owner-check. | NONE |
| 11 | GDPR DELETE /api/users/me | No such endpoint in mariana — handled by Supabase's auth admin API. | NONE (out of scope) |
| 12 | Test/dev backdoors | `grep TEST_MODE\|DEBUG.*admin` — only `DEBUG` env controls dev CORS origins. No backdoor auth path. | NONE |
| 13 | Async generator cleanup in agent loop | `mariana/agent/loop.py` uses async iteration over tool results inside `try/finally` blocks. The outer `finally` at `loop.py:1421-1493` runs on every return path including cancellation. ✓ | NONE |
| 14 | Dependency CVEs | Out of scope (would require CVE feed). | OUT OF SCOPE |

---

## Section 3 — Findings

| Bug ID | Priority | File:line | Evidence | Suggested fix |
|--------|----------|-----------|----------|---------------|
| **BB-01** | **P2** | `frontend/supabase/migrations/009_f03_refund_debt.sql:155-160` (and identical pattern in `006_refund_credits_repair.sql:94-98`) — affects every consumer of `refund_credits` (B-04 / U-01 / K-02 Stripe refund-dispute handlers and the AA-01 orphan-overrun path) | The `refund_credits(p_user_id, p_credits, p_ref_type, p_ref_id)` SQL function's FIFO bucket-debit loop `INSERT`s a `credit_transactions` row PER BUCKET TOUCHED with the SAME `(p_ref_type, p_ref_id, type='refund')`. The `uq_credit_tx_idem` UNIQUE INDEX created at `004b_credit_tx_idem_concurrent.sql:22-26` covers `(ref_type, ref_id, type) WHERE type IN ('grant','refund','expiry')` — so the SECOND iteration of the loop violates the unique constraint and the entire function aborts with `UniqueViolation`. **Reproduced locally:** `psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint "uq_credit_tx_idem". DETAIL: Key (ref_type, ref_id, type)=(aa36_test, ..., refund) already exists.` Single-bucket refunds work (loop runs once); multi-bucket refunds (a Stripe refund or AA-01 overrun spanning multiple credit buckets) fail. **Impact paths:** (a) Stripe refund webhook for a charge whose user has multiple credit buckets → handler raises 500, webhook retries until Stripe gives up, user keeps refunded credits. (b) AA-01 orphan-overrun on a user with multiple buckets → daemon's keyed `refund_credits` RPC raises, `rpc_succeeded=False`, no marker stamped (no row exists anyway in orphan path), reservation overrun never claws back. (c) The 004b migration comment at line 9-11 explicitly documents that `spend_credits` is excluded from the index because it writes per-bucket — the same exclusion is required for `refund_credits`. The migration author overlooked this asymmetry. **Latent since migration 004b** but not flagged because (1) most users have a single active bucket so the loop runs once; (2) multi-bucket refunds are tested only with a single bucket in `tests/test_b04_refund_dispute.py` (verified — no multi-bucket refund test). | Two equivalent fixes: **Option A (preferred):** Replace the per-bucket INSERT inside the loop with a SINGLE INSERT after the loop that aggregates `(p_user_id, 'refund', SUM(v_take), NULL, p_ref_type, p_ref_id, v_balance_after_final, metadata)` — one row per refund call, matching the dedup contract. The bucket-level `credit_buckets.remaining_credits` UPDATE in the loop already records the per-bucket impact, so the per-bucket transaction row is redundant for the operator audit trail. **Option B:** Widen `uq_credit_tx_idem` to exclude `type='refund'` the same way `'spend'` is excluded; rely on the existing `IF v_existing_tx IS NOT NULL THEN RETURN 'duplicate'` guard (which uses `LIMIT 1` so multiple rows are fine). Option A is cleaner — one ledger row per refund call matches grant semantics. Add a regression migration + a test in `tests/test_bb01_multi_bucket_refund.py` that creates two buckets summing > the refund amount, calls `refund_credits`, and asserts (i) success, (ii) `credits_debited == p_credits`, (iii) exactly one `credit_transactions` row exists for that `(ref_type, ref_id)`. |

---

## Section 4 — Verdict

**ONE FINDING.** Streak resets to **0 / 3**.

- **BB-01 (P2)** — `refund_credits` SQL function fails on multi-bucket refunds because the per-bucket INSERT loop violates the `uq_credit_tx_idem` UNIQUE constraint introduced in migration 004b. Latent since 004b but never flagged because existing tests cover single-bucket only. **Reproduced locally** with a clear UniqueViolation. Affects Stripe refund handler, K-02 dispute reversals, U-01 OOO reversal handler, AA-01 orphan-overrun path, and any future multi-bucket refund use case.
- AA-01 fix code itself: clean across 9 fresh angles (dedup contract, retry, version-skew, reconciler orphan interaction, etc.).
- W-01 / X-01 / Y-01 / Z-01 / Z-02: all hold.

The streak does NOT close. Fix is a one-shot SQL migration that either (a) collapses the per-bucket INSERT into a single ledger row per refund call (preferred — matches grant semantics) or (b) excludes `type='refund'` from the unique index. Re-audit after fix.
