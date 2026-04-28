# A33 — Phase E Re-audit #28 (Loop 6 zero-bug convergence)

- **Audit number:** A33
- **Auditor model:** claude_opus_4_7
- **Branch / HEAD:** `loop6/zero-bug` @ `08a82bf`
- **Date:** 2026-04-28
- **Streak entering:** 1/3 (A32 zero)

---

## Section 1 — Y-01 / Y-02 fix re-probe (fresh angles)

### Methodology

Picked angles A32 did not consider:

| Angle | Result |
|-------|--------|
| Mixed-deploy migration window | Old daemon (pre-Y-01) wrote no claim row and used legacy non-keyed RPCs. New daemon resuming an old `.running` file would issue the new keyed RPC against `(ref_type='research_task', ref_id=task_id)` — the live ledger has no prior tx with this key, so the second mutation is NOT deduped, doubling settlement. **Bounded transition window** — only `.running` files written by pre-Y-01 code are at risk; after first deploy, all new `.running` files use the new code path. Same risk class as T-01's deploy-window. Acknowledged in the Y-01 fix report's "residual risk" section. NOT a steady-state defect — operations should drain `.running` files before Y-01 deploy. Not filing as a finding. |
| `research_tasks` deletion via `DELETE /api/investigations/{task_id}` | **Z-01 (P2)** — see findings. The cascade list at `mariana/api.py:3589-3620` does NOT include `research_settlements`, but Y-01 added FK from `research_settlements.task_id → research_tasks(id) ON DELETE RESTRICT`. Any DELETE of a settled investigation now fails with `ForeignKeyViolationError`. **Reproduced locally — see Z-01 evidence in Section 4.** |
| auth.users cascade-delete to research_tasks | `research_tasks.user_id REFERENCES auth.users(id) ON DELETE CASCADE` — when Supabase deletes a user, cascade tries to delete research_tasks. The `research_settlements` FK with ON DELETE RESTRICT then RAISES, blocking the cascade. **Same root cause as Z-01** — fix simultaneously closes both paths. Folded into Z-01 remediation. |
| Reconciler row-stuck-claimed forever | `claimed_at = now()` bump only delays re-pickup by `max_age_seconds`. After the threshold, reconciler re-tries. No "give up after N attempts" mechanism, but T-01 has the same operator-visible behavior. Not a regression. NONE. |
| `research_settlements.task_id` collision with `agent_settlements.task_id` | Different tables, different namespaces. No cross-FK or cross-query. Even if a UUID collision existed, the two tables are independent. NONE. |
| `_mark_research_settlement_completed` transaction boundary | Inside `conn.transaction()`. If either UPDATE raises, both rollback. Outside the transaction, RPC has already mutated the ledger, but the row is still picked up by the reconciler via `ledger_applied_at IS NOT NULL` for marker fixup. NONE. |
| Test coverage gaps in `test_y01_research_settlement_idempotency.py` | 4 tests cover: first-settle keyed RPC, second-settle short-circuit, marker-loss reconciler no-replay, daemon-resume double-settle protection. Missing: claim INSERT failure mid-window (transient FK or unique violation), reconciler re-entry after partial RPC failure, concurrent same-process settle. These are covered by T-01 patterns implicitly but not pinned for the research path. P4 robustness gap, not a defect. NONE. |
| Y-01 schema applied on NestD live | `research_settlements` lives in BACKEND POSTGRES, not Supabase. Applied via `init_schema()` at daemon startup. No NestD migration needed. ✓ |
| Concurrent two-process `_deduct_user_credits` | First process: SELECT none → INSERT wins. Second process: SELECT none → INSERT loses (ON CONFLICT DO NOTHING returns no row) → "claim_lost" log + return. No double-RPC. ✓ |

### Y-02 fix re-probe

`022_revert.sql` was already verified in A32. The DROP INDEX before DROP TABLE is conservative; `IF EXISTS` makes it idempotent. NONE.

---

## Section 2 — Re-verification of A29 / A30 / A32 clean surfaces

| Surface | Re-verification | Result |
|---------|----------------|--------|
| Conversation deletion cascade (A29 #1) | `delete_conversation` at api.py:2649-2679 — Supabase REST DELETE filtered by `user_id=eq.{user_id}`. Cascade handles messages, investigations SET NULL. Verified: no FK from research_settlements to conversations, so Y-01 doesn't introduce a similar regression here. NONE. |
| File upload signed URL flow (A29 #2) | Re-checked atomic owner binding in `os.O_EXCL` at api.py:4943, `_validate_upload_session_uuid` UUID format, symlink rejection after write. Y-01 didn't touch these. NONE. |
| Admin route exposure (A29 #6) | All `/api/admin/*` endpoints declare `Depends(_require_admin)`. Y-01 added no new admin routes. NONE. |
| Plan downgrade race (A30 #9) | `customer.subscription.deleted` immediately patches profile. In-flight reservations were already deducted. Y-01 doesn't change this. NONE. |
| Settlement reconciler concurrency (A29 #15) | T-01 reconciler has been working for many audits. Y-01's reconciler mirrors it exactly (FOR UPDATE SKIP LOCKED + claimed_at bump + ledger_applied_at short-cut). A32 verified concurrency. NONE. |

---

## Section 3 — Brand-new surfaces probed

| # | Surface | Probed | Result |
|---|---------|--------|--------|
| 1 | Stripe checkout `success_url` / `cancel_url` host allowlist | `_ALLOWED_REDIRECT_HOSTS = {"frontend-tau-navy-80.vercel.app", "localhost", "127.0.0.1"}` at `api.py:5527-5531`. The CORS list includes `https://app.mariana.computer` (api.py:452) but the redirect-host allowlist does NOT. Production-frontend checkout requests will be rejected with 400. **Z-02 (P2)** — see findings. |
| 2 | Stripe checkout idempotency on user double-click | Stripe Checkout sessions are created fresh on each call. Each call creates a new session ID. Stripe webhook idempotency on `event.id` (B-03 / U-01) prevents double-grant on retry. User clicking checkout twice creates two sessions; if they pay both, two separate `checkout.session.completed` events arrive with different event IDs but same `client_reference_id` — both grant credits. This is by-design — user paid twice, gets credits twice. NONE. |
| 3 | Customer-portal session validation | `billing_portal.Session.create(customer=stripe_customer_id)` where customer_id is fetched from Supabase by `user_id=current_user["user_id"]`. User cannot inject another user's customer_id. NONE. |
| 4 | TLS verification in HTTP clients | `httpx.AsyncClient(timeout=...)` — httpx defaults to `verify=True`. No `verify=False` kwarg anywhere in mariana/. `verify=False` grep returns zero hits. NONE. |
| 5 | Memory leaks in long-running daemon | `_upload_locks` is a bounded LRU at api.py:4670 (G-01 fix). `active_tasks` set is pruned on every iteration (`main.py:809-848`, `main.py:1031-1044`). Reconciler uses module-level imports — no growing cache. NONE. |
| 6 | Plan TOCTOU | Plan check happens at `_run_single_check` before reservation; reservation is deducted then. By the time agent runs, plan can change but reservation is already secured. Plan-tier-based feature gating (e.g., budget cap) is enforced at request start; downgrade mid-task does not retroactively cap a running task. By-design. NONE. |
| 7 | Scripts/ directory production hazards | No `scripts/` dir at repo root. Migration helper scripts live in `frontend/supabase/migrations/`. NONE. |
| 8 | Webhook event_id replay across stripe_dispute_reversals + stripe_pending_reversals + stripe_payment_grants | Each table has UNIQUE on `event_id` (per their migrations). A single webhook event creates rows in at most one of these (refund/dispute/grant), keyed by event_id. Replay returns duplicate. NONE. |
| 9 | credit_buckets FIFO consumption (any race?) | Bucket consumption happens inside `grant_credits` / `refund_credits` SQL functions which use per-user advisory lock (per T-01). Atomic. NONE. |
| 10 | Subscription proration math | Stripe sends `proration_amount` in invoice line items. The `_handle_invoice_paid` handler grants based on `plan["credits_per_month"]` (the full plan amount) — does NOT prorate. This means a mid-cycle upgrade gets the FULL plan credits, even though Stripe only charged a prorated amount. **However:** looking carefully, this was likely intentional — Mariana grants credits per "billing period," and the `subscription_create` event is excluded (line 5972: `if billing_reason == "subscription_create": return`). Proration on upgrades is handled by Stripe; Mariana grants credits on each `invoice.paid` event keyed by event_id (idempotent). User experience: pay prorated $, get full credit grant. By-design / business choice. NONE. |
| 11 | Database trigger / generated column gotchas | grep finds no `GENERATED ALWAYS AS` in mariana/data/db.py or agent/schema.sql. NONE. |

---

## Section 4 — Findings

| Bug ID | Priority | File:line | Evidence | Suggested fix |
|--------|----------|-----------|----------|---------------|
| **Z-01** | **P2** | `mariana/api.py:3589-3633` (`delete_investigation`) regressed by Y-01 schema in `mariana/data/db.py:153-167` (FK `research_settlements.task_id → research_tasks(id) ON DELETE RESTRICT`) | The investigation delete endpoint cascades through `cascade_tables` (intelligence/sessions/etc.) then `DELETE FROM research_tasks WHERE id = $1 RETURNING id` at line 3631. Y-01 added a FK from `research_settlements` to `research_tasks` with `ON DELETE RESTRICT`. After Y-01, any settled investigation (which has a `research_settlements` row) cannot be deleted — the parent DELETE raises `ForeignKeyViolationError` and the endpoint returns 500. **Reproduced locally:** `psycopg2.errors.ForeignKeyViolation: update or delete on table "research_tasks" violates foreign key constraint "research_settlements_task_id_fkey" on table "research_settlements". DETAIL: Key (id)=(test-z01-fk-violation) is still referenced from table "research_settlements".` Same root cause also blocks `auth.users` cascade-delete (Supabase user-deletion) because `research_tasks.user_id ON DELETE CASCADE` would try to delete research_tasks rows that have settlement children. **User impact:** delete-investigation broken for completed tasks; user-account deletion (GDPR right-to-erasure) may also fail with a 500 from Supabase's auth.users cascade. | Add `"research_settlements"` to the `cascade_tables` list at `mariana/api.py:3589-3620` so the user-driven delete path explicitly removes the settlement claim row before the parent. T-01's `agent_settlements` has the same `ON DELETE RESTRICT` but agent tasks have no user-facing delete endpoint, so the equivalent code path doesn't exist. Order: place `"research_settlements"` near the bottom of the list so cascades run children-first; the DELETE is owner-scoped via the upstream task ownership check at lines 3549-3566. Add a regression test in `tests/test_z01_research_settlements_cascade.py` that creates a settled task and asserts `DELETE /api/investigations/{task_id}` succeeds end-to-end. |
| **Z-02** | **P2** | `mariana/api.py:5527-5544` (`create_checkout`) | The Stripe checkout `success_url` / `cancel_url` validation hardcodes `_ALLOWED_REDIRECT_HOSTS = {"frontend-tau-navy-80.vercel.app", "localhost", "127.0.0.1"}` at line 5527-5531. The production frontend host `app.mariana.computer` is in the CORS list (`api.py:452`) but NOT in the redirect allowlist. Any user on production who clicks "Subscribe" with `success_url=https://app.mariana.computer/checkout/success` will receive `400 Invalid success_url: host 'app.mariana.computer' is not allowed`, breaking checkout for the production domain. **User impact:** no Stripe checkout / subscription / top-up flow works from production. Direct revenue loss. | Add `"app.mariana.computer"` to `_ALLOWED_REDIRECT_HOSTS`. Better: derive the allowlist from `_DEFAULT_PROD_CORS_ORIGINS` + `_DEFAULT_DEV_CORS_ORIGINS` (which already has `app.mariana.computer`) so the two surfaces stay in lockstep. Or accept any HTTPS host in the configured `CORS_ALLOWED_ORIGINS` env var. Add regression test asserting `success_url=https://app.mariana.computer/checkout/success` is accepted. |

---

## Section 5 — Verdict

**TWO FINDINGS.** Streak resets to **0 / 3**.

- **Z-01 (P2)** — Y-01's `ON DELETE RESTRICT` FK on `research_settlements.task_id` regresses the user-facing investigation delete endpoint and likely blocks Supabase auth.users cascade-delete. **Reproduced locally.** A direct introduced-by-Y-01 defect.
- **Z-02 (P2)** — Stripe checkout `success_url` host allowlist is missing `app.mariana.computer`. The production frontend cannot complete a checkout. Pre-existing defect missed by every prior audit (A6..A32).

The streak does NOT close. Both fixes are minimal: Z-01 adds one element to the cascade list; Z-02 adds one host to an allowlist (or unifies the source). Re-audit after both fix.
