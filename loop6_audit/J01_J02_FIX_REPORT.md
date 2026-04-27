# J-01 + J-02 Fix Report

**Branch**: loop6/zero-bug  
**Date**: 2026-04-27  
**Scope**: api.py only — no SQL migration required (stripe_dispute_reversals.charge_id column already present from migration 017)

---

## Root Cause (shared)

Both bugs share the same underlying flaw: `_reverse_credits_for_charge` computed the credits to debit from raw event payload values rather than from the remaining unreversed credits for that charge.

- **J-01**: `charge.refunded` events used a charge-scoped reversal key (`charge:<id>:reversal`). All sequential partial refunds on the same charge mapped to the same key. The second and later events were treated as duplicates by `_record_dispute_reversal_or_skip`, so only the first partial refund was ever debited.

- **J-02**: `charge.refunded` and `charge.dispute.created` intentionally used different keys (to avoid H-02 false-dedup), so both handlers fired. The dispute path built a pseudo-charge dict with `amount_refunded = dispute.amount` (the full charge amount), causing `_reverse_credits_for_charge` to debit the full original grant, regardless of any prior partial refund reversal. Net: 30 (refund) + 100 (dispute) = 130 debited against a 100-credit grant.

---

## Changes — `mariana/api.py` only

### 1. `_compute_reversal_key` (signature changed)

Added optional `refund_event_id: str | None = None` keyword argument. Dispute path is unchanged (`dispute:<dispute_id>` — H-02 intentional collapse). Refund path now returns `refund_event:<event_id>` when an event_id is provided, giving each webhook delivery a unique idempotency key. Legacy fallback `charge:<id>:reversal` retained for callers that pass no event_id.

### 2. `_record_dispute_reversal_or_skip` (signature changed)

Added `refund_event_id: str | None = None` keyword argument. Threads it through to `_compute_reversal_key` so the SELECT check uses the same per-event key as the subsequent INSERT.

### 3. `_insert_dispute_reversal` (signature changed)

Added `refund_event_id: str | None = None` keyword argument. Threads it through to `_compute_reversal_key` for consistent key generation across check and insert.

### 4. New: `_sum_reversed_credits_for_charge`

New async helper. Queries `stripe_dispute_reversals` filtered by `charge_id` and sums the `credits` column. Returns the total credits already reversed for a given charge across all reversal_key values (refund events, dispute events, etc.). Returns 0 on network error or misconfiguration (safe fallback — worst case is a redundant debit which the RPC idempotency would prevent anyway).

### 5. `_reverse_credits_for_charge` (core logic changed)

Added `refund_event_id: str | None = None` parameter. Changed the debit computation:

**Before**: `credits_to_debit = floor(original_credits * amount_refunded / amount_total)` (target amount from payload).

**After**:
```
target_credits = floor(original_credits * amount_refunded / amount_total)  # or full
already_reversed = await _sum_reversed_credits_for_charge(charge_id, cfg)
incremental_debit = max(0, target_credits - already_reversed)
```

When `incremental_debit <= 0`, the function logs `charge_reversal_already_satisfied` and inserts a 0-credit dedup row (so the event is not reprocessed) without calling the refund RPC.

### 6. `_handle_charge_refunded` (pass-through)

Now passes `refund_event_id=event_id` to `_reverse_credits_for_charge`, enabling the per-event reversal key path.

---

## Correctness verification

| Scenario | Before | After |
|---|---|---|
| J-01: refund 30%, then 50% | Debit 30 only (second collapsed) | Debit 30, then 20 (incremental) |
| J-01: refund 10%, 30%, 50% | Debit 10 only | Debit 10, 20, 20 |
| J-02: refund 30%, dispute 100% | Debit 30 + 100 = 130 (over) | Debit 30 + 70 = 100 (correct) |
| J-02: refund 100%, dispute 100% | Debit 100 + 100 = 200 (over) | Debit 100, dispute skipped (0) |
| Regression: single full refund | Debit 100 | Debit 100 (unchanged) |
| Regression: dispute only | Debit 100 | Debit 100 (unchanged) |
| H-02: dispute.created + dispute.funds_withdrawn | Dedup on dispute key | Unchanged (dispute key still shared) |

---

## Tests

### New: `tests/test_j01_partial_refund_sequence.py` (5 tests)

1. `test_two_sequential_refunds_30_50_percent_debit_30_then_20` — primary J-01 regression
2. `test_three_sequential_refunds_10_30_50_percent_debit_10_then_20_then_20`
3. `test_single_full_refund_debits_full_grant`
4. `test_same_event_id_replayed_is_idempotent` — webhook retry safety
5. `test_single_partial_refund_20_percent_regression` — B-04 regression

### New: `tests/test_j02_dispute_after_refund.py` (4 tests)

1. `test_partial_refund_30_then_full_dispute_debits_30_then_70` — primary J-02 regression
2. `test_partial_refund_30_then_partial_dispute_70_debits_30_then_70`
3. `test_full_refund_then_full_dispute_dispute_records_zero` — 0-credit dedup row path
4. `test_dispute_only_no_prior_refund_debits_full_grant_regression` — B-04 regression

### Updated: `tests/test_h02_dispute_dedup.py`

- `test_dispute_created_then_funds_withdrawn_same_dispute_only_one_reversal`: Replaced counter-based `_StatefulClient` with a shared-state client that correctly simulates DB persistence across two async handler calls and answers the new `_sum_reversed_credits_for_charge` GET query.
- `test_reversal_key_formatting_dispute_vs_no_dispute_paths`: Updated expected charge.refunded key from `charge:ch_6:reversal` to `refund_event:evt_ref_5` (reflects J-01 key change).

### Updated: `tests/test_b04_refund_dispute.py`

- `test_full_refund_reverses_full_grant`: Updated `p_ref_id` assertion from `charge:ch_1:reversal` to `refund_event:evt_ref_full_1`.
- `test_partial_refund_reverses_pro_rata`: Updated `p_ref_id` assertion from `charge:ch_2:reversal` to `refund_event:evt_ref_partial_1`.

---

## Test counts

| Suite | Before | After |
|---|---|---|
| pytest pass | 279 | 288 |
| pytest skip | 13 | 13 |
| Contract tests (18/18) | Pass | Pass |

---

## Migration

No migration applied. The `stripe_dispute_reversals.charge_id` column required by `_sum_reversed_credits_for_charge` was already added by `frontend/supabase/migrations/017_h01_h02_stripe_grant_linkage.sql` and populated by the existing `_insert_dispute_reversal` logic. No schema changes needed.
