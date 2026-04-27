# F-03 Fix Report — Refund Clawback Debt Construct

**Finding:** F-03 (Phase E re-audit, loop 6)
**Severity:** P1
**Category:** F (Economic correctness)
**Status:** FIXED and applied to live NestD (project `afnbtbeayfkwznhzafay`)

---

## Problem Summary

The B-04 `refund_credits` RPC (patched in migration 006, further updated in 007)
clamped the requested debit to the user's current balance:

```sql
IF v_total_balance < p_credits THEN
    v_remaining := v_total_balance;  -- silent forgiveness
END IF;
```

Scenario that exposed the economic loss:
1. User receives 1000 credits (Stripe payment).
2. User spends 900 credits on work products.
3. User later disputes the payment / Stripe issues a refund.
4. `refund_credits(user_id, 1000, ...)` was called.
5. Only 100 credits were debited (the remaining balance).
6. The 900 credits' worth of work already delivered was silently forgiven.

---

## Files Changed

| File | Change |
|------|--------|
| `frontend/supabase/migrations/009_f03_refund_debt.sql` | **NEW** — Main migration |
| `frontend/supabase/migrations/009_f03_refund_debt_revert.sql` | **NEW** — Revert migration |
| `scripts/build_local_baseline_v2.sh` | Added `009_f03_refund_debt.sql` to migration apply order |
| `tests/test_f03_refund_clawback_debt.py` | **NEW** — 8 integration tests |
| `tests/contracts/C08_clawback_invariants.py` | **NEW** — 2 SQL contract invariant tests |

### Migration filename decision
F-04 (plan entitlement fix) already wrote `008_f04_plan_entitlement_sync.sql`.
Per the design spec, this migration is named `009_f03_refund_debt.sql`.

---

## Schema Changes (migration 009)

### New table: `public.credit_clawbacks`

```sql
CREATE TABLE public.credit_clawbacks (
  id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  amount       integer     NOT NULL CHECK (amount > 0),
  ref_type     text        NOT NULL,
  ref_id       text        NOT NULL,
  satisfied_at timestamptz NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (ref_type, ref_id)
);
CREATE INDEX idx_credit_clawbacks_user_unsatisfied
  ON public.credit_clawbacks (user_id) WHERE satisfied_at IS NULL;
```

RLS: enabled. Users can `SELECT` their own rows. Only service_role can DML.

### Modified: `credit_transactions.type` CHECK constraint

Extended to allow the new audit type `clawback_satisfy`:

```sql
CHECK (type = ANY (ARRAY['grant','spend','refund','expiry','clawback_satisfy']))
```

---

## RPC Behavior Matrix

### `refund_credits(p_user_id, p_credits, p_ref_type, p_ref_id)`

| Scenario | Before (006/007) | After (009) |
|----------|-----------------|-------------|
| User balance >= requested | Debits full amount, returns `reversed` | Debits full amount, returns `reversed` (unchanged) |
| User balance < requested (spent credits) | Debits available balance, **silently forgives** the rest | Debits available balance AND **records a `credit_clawbacks` row** for the deficit |
| User balance = 0 | Returns `no_credits`, debits 0 | Debits 0, records full amount as deficit clawback row |
| Duplicate call (same ref_type/ref_id) | Returns `duplicate` via tx lookup | Returns `duplicate` via tx lookup OR clawback row lookup |
| Return value | `{status, credits_debited, balance_after}` | `{status, debited_now, deficit_recorded, balance_after}` |

### `grant_credits(p_user_id, p_credits, p_source, ...)`

| Scenario | Before (007) | After (009) |
|----------|-------------|-------------|
| No open clawbacks | Creates bucket, grants full credits, syncs tokens | Identical (no change in behavior) |
| Open clawbacks exist | Creates bucket, adds full credits to tokens | Creates bucket, **drains clawback deficit FIFO from new bucket before returning**; net credits land in spendable balance |
| Clawback fully satisfied | N/A | Sets `satisfied_at=now()` on clawback row, records `clawback_satisfy` tx |
| Clawback partially satisfied | N/A | Reduces `clawback.amount` in place, leaves `satisfied_at=NULL` |
| Return value | `{status, bucket_id, credits, balance_after}` | Adds `clawback_satisfied` field |

### `add_credits(p_user_id, p_credits)`  *(tokens-only path)*

| Scenario | Before (007) | After (009) |
|----------|-------------|-------------|
| No open clawbacks | Adds full amount to `profiles.tokens` | Identical |
| Open clawbacks exist | Adds full amount to tokens, ignores deficit | **Nets addition against open clawbacks FIFO**; only net amount lands in tokens |

---

## Live Apply Verification

### Pre-apply state
- Tables: no `credit_clawbacks` table
- Last migration: `008_f04_update_rpc`

### Post-apply state
- Tables: `public.credit_clawbacks` (rls_enabled=true, rows=0) — confirmed
- Last migration: `009_f03_refund_debt` (version `20260427140239`) — confirmed

---

## Test Results

### Python tests
```
cd /home/user/workspace/mariana
PGHOST=/tmp PGPORT=55432 PGUSER=postgres PGDATABASE=testdb \
  python -m pytest tests/ -x --tb=short -q

139 passed, 10 skipped in 4.88s
```

All existing tests remain green. 10 skipped are Supabase integration tests
(require `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` env vars which are not set locally).

### New F-03 tests (all pass)
```
tests/test_f03_refund_clawback_debt.py::test_refund_full_balance_no_deficit          PASS
tests/test_f03_refund_clawback_debt.py::test_refund_partial_balance_records_deficit  PASS
tests/test_f03_refund_clawback_debt.py::test_grant_satisfies_open_clawback_first     PASS
tests/test_f03_refund_clawback_debt.py::test_grant_partial_satisfaction_keeps_unsatisfied_remainder  PASS
tests/test_f03_refund_clawback_debt.py::test_concurrent_refund_grant_serialized      PASS
tests/test_f03_refund_clawback_debt.py::test_refund_idempotent_on_ref_id             PASS
tests/test_f03_refund_clawback_debt.py::test_add_credits_drains_clawback             PASS
tests/test_f03_refund_clawback_debt.py::test_multiple_clawbacks_satisfied_fifo       PASS
tests/contracts/C08_clawback_invariants.py::test_c08a_deficit_leaves_open_clawback_and_tokens_synced  PASS
tests/contracts/C08_clawback_invariants.py::test_c08b_grant_satisfies_deficit_no_open_clawbacks_tokens_correct  PASS
```

### Frontend npm tests
```
cd /home/user/workspace/mariana/frontend && npm run test

Test Files  6 passed (6)
      Tests  51 passed (51)
```

---

## Design Invariants Upheld

| Constraint | Status |
|-----------|--------|
| B-01: REVOKE from anon/authenticated on modified RPCs | ✅ REVOKE ALL FROM PUBLIC; GRANT only to service_role |
| B-02: SET search_path on SECURITY DEFINER functions | ✅ All three RPCs use `SET search_path = public, pg_temp` |
| B-05: profiles.tokens stays in sync with ledger | ✅ refund_credits debits only available amount from tokens; grant_credits adds only net (post-clawback) credits |
| Idempotency on (ref_type, ref_id) | ✅ dual check: credit_transactions type='refund' AND credit_clawbacks UNIQUE(ref_type, ref_id) |
| Advisory lock serialization | ✅ `pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0))` in all three RPCs |
| FIFO ordering | ✅ clawback satisfaction uses `ORDER BY created_at ASC` |
| Zero existing test regressions | ✅ 139 Python + 51 npm all green |

---

## Audit Trail

The `clawback_satisfy` transaction type records each satisfaction event with:
- `ref_type`/`ref_id` from the original clawback (so it links back to the Stripe event)
- `metadata.clawback_id` — the specific `credit_clawbacks.id` that was satisfied
- `metadata.grant_ref_type`/`grant_ref_id` — the grant that triggered satisfaction

This creates a complete, queryable audit trail linking the original Stripe event → clawback → satisfaction grant.
