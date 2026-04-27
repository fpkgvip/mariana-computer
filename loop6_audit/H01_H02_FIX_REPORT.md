# H-01 + H-02 Fix Report

**Date:** 2026-04-27
**Branch:** loop6/zero-bug
**Migration:** 017_h01_h02_stripe_grant_linkage

---

## Summary

Two new Phase E re-audit findings (from A8_phase_e_reaudit.md) were fixed in a single change set.

---

## H-01 [P1] — Stripe refund/dispute lookup falls back to latest unrelated grant

### Root cause

`_grant_credits_for_event` wrote grant rows via the `grant_credits` RPC but never persisted the PaymentIntent ID anywhere. `_lookup_grant_tx_for_payment_intent` first queried `credit_transactions.metadata->>'pi_id'` (always empty), then fell back to a global `ORDER BY created_at DESC LIMIT 1` over all `type='grant' AND ref_type='stripe_event'` rows with no user or payment scoping. A refund for user B could debit user A's credits if user A owned the most recent Stripe grant row at that moment.

### Fix

**Migration 017** creates `public.stripe_payment_grants`:

```sql
CREATE TABLE public.stripe_payment_grants (
  payment_intent_id text PRIMARY KEY,
  charge_id         text,
  user_id           uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  credits           integer NOT NULL CHECK (credits > 0),
  event_id          text NOT NULL,
  source            text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
);
```

**`_grant_credits_for_event`** gains optional `pi_id` and `charge_id` parameters. After the grant RPC returns `status != 'duplicate'` and `pi_id` is set, it inserts a row into `stripe_payment_grants` via `POST /rest/v1/stripe_payment_grants` with `Prefer: resolution=ignore-duplicates` so retries are safe.

**Call sites updated** to pass `pi_id`:
- `_handle_checkout_completed`: `session_obj.get("payment_intent")`, fallback to latest_invoice if subscription.
- `_handle_invoice_paid`: `invoice_obj.get("payment_intent")`.
- `_handle_payment_intent_succeeded`: `pi_obj.get("id")`.

**`_lookup_grant_tx_for_payment_intent`** rewritten to query only `stripe_payment_grants` via exact `payment_intent_id=eq.<pi>` filter. The global `ORDER BY created_at DESC LIMIT 1` fallback is removed entirely. If no row exists, logs `grant_lookup_no_exact_mapping` and returns `None` (the caller skips the reversal).

### Tests (6)

File: `tests/test_h01_grant_pi_linkage.py`

1. `test_grant_with_pi_id_inserts_stripe_payment_grants_row` — POST to stripe_payment_grants fires when pi_id set and status=granted.
2. `test_lookup_returns_none_when_stripe_payment_grants_empty` — empty table returns None, no global fallback.
3. `test_refund_for_user_a_pi_resolves_user_a_not_user_b` — two users; refund for A's pi_id targets A, not B.
4. `test_duplicate_grant_status_skips_stripe_payment_grants_insert` — status=duplicate skips insert.
5. `test_grant_with_no_pi_id_runs_without_error` — pi_id=None skips insert, grant still completes.
6. `test_lookup_returns_correct_fields_from_stripe_payment_grants` — returned dict has user_id, credits, event_id.

---

## H-02 [P2] — charge.dispute.created and charge.dispute.funds_withdrawn both execute full reversal

### Root cause

`_reverse_credits_for_charge` was keyed only by Stripe `event_id` for idempotency. `charge.dispute.created` and `charge.dispute.funds_withdrawn` have different event IDs, so both processed as fresh reversals. The second event computed a full deficit and inserted a new `credit_clawbacks` row, causing future grants to be doubly consumed.

### Fix

**Migration 017** creates `public.stripe_dispute_reversals`:

```sql
CREATE TABLE public.stripe_dispute_reversals (
  reversal_key      text PRIMARY KEY,  -- 'dispute:<dispute_id>' or 'charge:<charge_id>:reversal'
  user_id           uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  charge_id         text,
  dispute_id        text,
  payment_intent_id text,
  credits           integer NOT NULL,
  first_event_id    text NOT NULL,
  first_event_type  text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
);
```

**New helpers:**
- `_record_dispute_reversal_or_skip(...)`: Computes `reversal_key` (`dispute:<id>` or `charge:<id>:reversal`), queries `stripe_dispute_reversals` — returns `True` (skip) if a row exists.
- `_insert_dispute_reversal(...)`: Inserts a row after successful reversal using `Prefer: resolution=ignore-duplicates`.

**`_reverse_credits_for_charge`** extended with optional `dispute_obj` and `event_type` parameters. Calls `_record_dispute_reversal_or_skip` before executing the reversal and `_insert_dispute_reversal` after success.

**Handler updates:**
- `_handle_charge_refunded`: passes `dispute_obj=None, event_type="charge.refunded"` → key = `charge:<id>:reversal`.
- `_handle_charge_dispute_created`: passes `dispute_obj=dispute_obj, event_type="charge.dispute.created"` → key = `dispute:<id>`.
- `_handle_charge_dispute_funds_withdrawn`: passes `dispute_obj=dispute_obj, event_type="charge.dispute.funds_withdrawn"` → same key = `dispute:<id>`. Second event is deduped.

### Tests (5)

File: `tests/test_h02_dispute_dedup.py`

1. `test_dispute_created_then_funds_withdrawn_same_dispute_only_one_reversal` — second event finds existing row, skips refund RPC.
2. `test_charge_refunded_then_dispute_created_both_process_different_keys` — charge.refunded uses `charge:<id>:reversal`, dispute uses `dispute:<id>` — both fire.
3. `test_successful_reversal_inserts_into_stripe_dispute_reversals` — POST to stripe_dispute_reversals fires with correct key.
4. `test_preexisting_reversal_row_short_circuits_refund_rpc` — pre-existing row stops refund RPC call.
5. `test_reversal_key_formatting_dispute_vs_no_dispute_paths` — key format verified for both code paths.

---

## Migration 017

Applied to NestD live (project_id=afnbtbeayfkwznhzafay) via Supabase MCP `apply_migration`. RLS enabled on both tables; `anon` and `authenticated` roles have no access; `service_role` has full access.

Revert: `017_revert.sql` (drops both tables).

---

## Test results

- Full pytest suite: 268 passed, 13 skipped (baseline was 257 + 13 skip; 11 new tests added).
- Contract tests: C19 green (16 existing + C19 = 17 passing contracts).

---

## Files changed

| File | Change |
|------|--------|
| `frontend/supabase/migrations/017_h01_h02_stripe_grant_linkage.sql` | New migration |
| `frontend/supabase/migrations/017_revert.sql` | Revert script |
| `mariana/api.py` | `_grant_credits_for_event` + pi_id call sites + `_lookup_grant_tx_for_payment_intent` + dispute dedup helpers + handler updates |
| `scripts/build_local_baseline_v2.sh` | Added 017 to migration list |
| `tests/test_h01_grant_pi_linkage.py` | 6 new H-01 regression tests |
| `tests/test_h02_dispute_dedup.py` | 5 new H-02 regression tests |
| `tests/test_b04_refund_dispute.py` | Updated existing tests to use stripe_payment_grants mock |
| `tests/contracts/C19_stripe_grant_linkage.sql` | New contract test |
| `loop6_audit/REGISTRY.md` | H-01 and H-02 marked FIXED |
