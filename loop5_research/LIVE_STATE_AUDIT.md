# Loop 5 — Live State vs Prior Session Plan: Audit

## Date
2026-04-27

## Context
The prior session built a Phase 0 fix plan around four claimed defects (D1–D4) in a hypothetical migration 006. Phase 0 was never pushed; the local sandbox was recycled and all artifacts lost. To rebuild safely, I pulled the live NestD project state (`afnbtbeayfkwznhzafay`) as ground truth.

## Finding: most claimed defects don't exist in live state

The summary asserted that migration 006 introduced four defects that 006c had to repair. Comparing those claims against the live database:

### D1: "5 admin RPCs inserted into non-existent `public.admin_audit`"
- **Status: DOES NOT EXIST IN LIVE.**
- All admin RPCs (`admin_set_role`, `admin_suspend`, `admin_system_freeze`, `admin_adjust_credits`, `admin_audit_insert`) correctly target `public.audit_log` with the right column names (`actor_id`, `actor_email`, `action`, `target_type`, `target_id`, `before`, `after`, `metadata`, `ip_address`, `user_agent`).
- D1 was a defect in a proposed-but-never-applied migration 006, not in production.

### D2: "006 dropped admin_set_credits(uuid,integer,text); baseline signature is (uuid,integer,boolean)"
- **Status: DOES NOT EXIST IN LIVE.**
- Live `admin_set_credits(target_user_id uuid, new_credits integer, is_delta boolean DEFAULT false)` — already the boolean form.
- api.py at line 6206 calls this exact signature with `is_delta=body.delta`.
- No `(uuid,integer,text)` form exists or ever existed. D2 was about reverting a hypothetical breaking change that was never made.

### D3: "expire_credits inserts type='expire' but CHECK allows ('grant','spend','refund','expiry')"
- **Status: DOES NOT EXIST IN LIVE.**
- Live `expire_credits()` correctly inserts `'expiry'` (not `'expire'`). The CHECK constraint allows `'expiry'`.
- D3 referred to a typo in a proposed migration that never landed.

### D4: "admin_adjust_credits passed p_source='admin_adjust' violating credit_buckets.source CHECK"
- **Status: PARTIALLY APPLICABLE.**
- Live `admin_adjust_credits(p_caller, p_target, p_mode, p_amount, p_reason)` updates `profiles.tokens` directly via UPDATE — it does NOT touch `credit_buckets` at all.
- Therefore there is no `source` value to violate. The credit ledger and the `tokens` integer column are still parallel systems on live (this is itself a real concern — see "Real issues" below).

## Real issues found in live state (worth a fresh, evidence-based plan)

### R1: Two conflicting UPDATE policies on `profiles`
- Live has both `"Users can update own profile"` and `profiles_owner_update_safe`. Both are PERMISSIVE — they're OR'd together.
- The newer `profiles_owner_update_safe` does NOT lock `subscription_status` or `subscription_plan`.
- The older `"Users can update own profile"` DOES lock those.
- Permissive OR: a user can self-modify `subscription_status`/`subscription_plan` by satisfying the looser policy.
- **Severity: high.** Privilege escalation surface for the subscription state.

### R2: `uq_credit_tx_grant_ref` only covers `type='grant'`
- Live unique index: `WHERE type='grant' AND ref_type IS NOT NULL AND ref_id IS NOT NULL`.
- Refunds and expiries can be duplicated by replaying the same Stripe `charge.refunded` event or an expiry sweep that runs twice in a window.
- **Severity: high.** Idempotency gap for refunds. Stripe's webhook delivery guarantees at-least-once.

### R3: Two parallel credit systems still coexist
- `profiles.tokens` (integer column, used by `add_credits`, `deduct_credits`, `admin_set_credits`, `admin_adjust_credits`).
- `credit_buckets` + `credit_transactions` (FIFO ledger, used by `grant_credits`, `spend_credits`, `refund_credits`, `expire_credits`).
- **Severity: medium-high.** They can drift. `reconcile_ledger.py` is needed regardless.

### R4: `admin_set_credits` uses `auth.uid()` for caller; other admin RPCs use explicit `p_caller`
- Inconsistent admin-check pattern. The `auth.uid()` form fails when called from a backend service-role context where `auth.uid()` returns NULL.
- api.py forwards the user's JWT, so it works today, but it's a foot-gun.
- **Severity: medium.**

### R5: `admin_set_credits` doesn't write to `audit_log`
- Other admin RPCs call `admin_audit_insert`. This one updates `tokens` silently.
- **Severity: medium.** Audit trail gap.

### R6: `add_credits` and `deduct_credits` skip `audit_log` and don't update the ledger
- These RPCs are called by Stripe webhook handlers (per the migration name `add_deduct_credits_function`). They mutate `profiles.tokens` without writing to `credit_transactions`.
- **Severity: medium.** Reconciliation drift source.

### R7: `expire_credits()` has no advisory lock around iterating buckets
- It locks each user inside the loop, but the outer cursor is unfenced. Two concurrent `expire_credits()` calls can double-insert expiry transactions.
- **Severity: low.** No `expire_credits` cron is enabled today on this project (verifiable via `pg_cron` not being in the extension list).

## Implication for Phase 1

The prior session's migration 006 was designed against the wrong baseline. Applying it to staging would:
- Add code to fix nonexistent defects (D1, D2, D3 mostly)
- Possibly miss the real defects (R1, R2, R5, R6)
- Create migration churn without addressing actual production risk

## Recommended path

1. **Stop the rebuild of the prior 006/006b/006c stack.** Those artifacts targeted the wrong baseline.
2. **Run a fresh, evidence-based audit** of the live live RPC bodies + RLS + indexes (this document is the start).
3. **Write a tight, targeted migration** (call it `004_loop5_idempotency_and_rls.sql`) that fixes R1, R2, R4-R7 only, with assertions, segmented apply, and a pre-written reverter.
4. **Reconcile script** stays — `reconcile_ledger.py` is useful regardless.
5. **Push Phase 0 with this audit + reconcile script + targeted migration + contract tests** — much smaller scope than the lost rebuild.
6. **Then Phase 1 staging** with the targeted migration applied via a Supabase dev branch.

This is materially different from "rebuild Phase 0 verbatim then start Phase 1." It's the cautious thing to do because the original Phase 0 plan was wrong.

## Files
- `loop5_research/live_tables.json` — `list_tables(verbose=true)` snapshot
- `loop5_research/live_rpc_bodies.json` — `pg_get_functiondef` for all admin/credit RPCs
