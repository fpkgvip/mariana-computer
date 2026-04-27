# K-01 + K-02 Fix Report

**Branch**: loop6/zero-bug
**Date**: 2026-04-28
**Scope**: 2 SQL migrations (020, 021) + `mariana/api.py` + 9 new tests + 6 reconciled test files

---

## Root Cause

### K-01 — Partial-amount disputes over-debit the full grant

`_handle_charge_dispute_*` constructed a pseudo `charge_obj` of the shape
`{"id": charge_id, "amount": dispute.amount, "amount_refunded": dispute.amount, "amount_captured": dispute.amount}`
and passed it to `_reverse_credits_for_charge`. The reversal helper compares `amount_total` against `amount_refunded`; when they are equal it takes the full-reversal branch and debits `original_credits` outright. For a partial-amount dispute (`dispute.amount < charge.amount`) — common with Stripe's "you can challenge a portion" disputes and certain partial chargebacks — this collapses into the full-reversal branch and debits the entire grant instead of the pro-rata fraction.

The fix needs the **original charge amount** at reversal time. That value was never persisted on `stripe_payment_grants`; the prior code reconstructed `amount_total` from the inbound event payload, which is what the dispute path got wrong.

### K-02 — TOCTOU race between concurrent webhook handlers

`_reverse_credits_for_charge` performed four sequential out-of-process steps against PostgREST:

1. `SELECT` `stripe_dispute_reversals` by `reversal_key` (dedup check).
2. `SELECT` + sum `stripe_dispute_reversals.credits` by `charge_id` (already-reversed total).
3. `POST /rpc/refund_credits` (debit credits ledger).
4. `INSERT INTO stripe_dispute_reversals` (record the reversal).

J-01/J-02 already gave each event a distinct `reversal_key` (e.g. `refund_event:<event_id>` for charge.refunded vs `dispute:<id>` for dispute.created). That intentional separation, combined with the absence of any shared mutex across steps 1–4, created a TOCTOU window: two concurrent handlers on the same `charge_id` (e.g. `charge.refunded` arriving simultaneously with `charge.dispute.created`, or two refund event deliveries) both observe `already_reversed = N` at step 2, both compute non-overlapping incremental targets, both call `refund_credits` with different `ref_id`s (so RPC-level idempotency does not collapse them), and both insert distinct dedup rows. Net: double-debit with neither row colliding on the unique constraint.

---

## Migrations

### `frontend/supabase/migrations/020_k01_charge_amount.sql`

Adds `stripe_payment_grants.charge_amount integer` (nullable, with `CHECK (charge_amount IS NULL OR charge_amount >= 0)`). Stores the original Stripe charge amount in cents at grant time so the reversal path can reconstruct correct pro-rata math even when only `dispute.amount` arrives on the event.

### `frontend/supabase/migrations/021_k02_atomic_charge_reversal.sql`

Defines `process_charge_reversal(p_user_id, p_charge_id, p_dispute_id, p_payment_intent_id, p_reversal_key, p_target_credits, p_first_event_id, p_first_event_type) RETURNS jsonb` as `SECURITY DEFINER`. The function:

1. Acquires `pg_advisory_xact_lock(hashtextextended('charge:' || p_charge_id, 0))` — a per-charge mutex distinct from `refund_credits`'s per-user lock (`hashtextextended(p_user_id::text, 0)`). Lock ordering: charge → user. No cycle (refund_credits never re-acquires charge).
2. Checks for an existing row keyed by `reversal_key`; returns `{status: "duplicate", credits: <existing>}` if found.
3. Sums `credits` from `stripe_dispute_reversals` filtered by `charge_id` to compute `already_reversed`.
4. Computes `incremental := greatest(0, p_target_credits - already_reversed)`. When zero, inserts a 0-credit dedup row and returns `{status: "already_satisfied", credits: 0}`.
5. Calls `refund_credits(p_user_id, incremental, ref_type='stripe_event', ref_id=p_reversal_key, …)`. The `ref_id = p_reversal_key` ties RPC-level idempotency to the same dedup key as the dispute_reversals row.
6. Inserts the `stripe_dispute_reversals` row with `GET DIAGNOSTICS v_inserted_count = ROW_COUNT`. On race the `ON CONFLICT (reversal_key) DO NOTHING` collapses to duplicate.
7. Returns `{status: "reversed", credits: incremental}`.

The migration also REVOKEs EXECUTE on this function from `PUBLIC`, `anon`, `authenticated`, GRANTs to `service_role`, and runs an invariant DO-block asserting no hostile EXECUTE grants remain.

---

## Code changes — `mariana/api.py`

### 1. Capture `charge_amount` on grant

`_handle_payment_intent_succeeded`, `_handle_invoice_paid`, and `_handle_checkout_session_completed` now read the original charge amount from the Stripe payload (`amount`, `amount_paid`, `amount_total` respectively) and pass it to `_grant_credits_for_event`.

### 2. `_grant_credits_for_event` (signature changed)

Accepts `charge_amount: int | None = None` and persists it to the new `stripe_payment_grants.charge_amount` column.

### 3. `_lookup_grant_tx_for_payment_intent` (column added)

Selects `charge_amount` so the reversal path can read it.

### 4. `_reverse_credits_for_charge` — K-01 patch

When `dispute_obj` is set and `grant_tx['charge_amount']` is non-null, override `amount_total` from the grant row instead of trusting the pseudo-charge built from `dispute.amount`. Pro-rata math then uses the correct denominator and partial disputes compute correct pro-rata credits.

### 5. `_reverse_credits_for_charge` — K-02 patch

Replaces the four-step SELECT / sum / refund_credits / INSERT chain with a single `POST /rpc/process_charge_reversal` call. The client-side `_record_dispute_reversal_or_skip`, `_sum_reversed_credits_for_charge`, and `_insert_dispute_reversal` paths are now collapsed inside the SQL function under the per-charge advisory lock. The Python helper still returns the same shape so callers and tests see no surface-area change.

---

## Correctness verification

| Scenario | Before | After |
|---|---|---|
| K-01: dispute 50% of $1.00 charge ($0.50) | Debit full grant (e.g. 100 credits) | Debit 50 credits (pro-rata) |
| K-01: dispute 30% then dispute funds_withdrawn (same $0.30) | Debit full + dedup hit | Debit 30, second is dedup |
| K-02: refund + dispute concurrent on same charge | Both pass dedup, both debit, total > original | Per-charge lock serializes; second observes already_reversed=N and computes 0 incremental |
| K-02: two refund events delivered concurrently | Both observe sum=0, both debit | Lock serializes; second sees first row, observes correct sum, computes correct incremental |
| Regression: J-01 sequential partial refunds 30/50% | 30 then 20 (J-01 fix) | Unchanged — RPC computes same incremental |
| Regression: J-02 partial refund + full dispute | 30 then 70 (J-02 fix) | Unchanged |
| Regression: H-02 dispute.created + funds_withdrawn | One reversal | Unchanged — same reversal_key still dedups |

Lock ordering (verified safe):
- `process_charge_reversal` acquires `charge:<charge_id>` first.
- It then calls `refund_credits` which acquires `<user_id>`.
- Order is always charge → user. No reverse path exists. No deadlock.

---

## Tests

### New: `tests/test_k01_partial_dispute.py` (5 tests)

1. `test_partial_dispute_50_percent_of_charge_debits_50_percent_of_credits` — primary K-01 regression
2. `test_partial_dispute_30_percent_debits_30_percent_pro_rata`
3. `test_full_dispute_amount_equals_charge_amount_still_debits_full_grant` — regression that K-01 fix did not break the full-dispute path
4. `test_partial_dispute_with_no_charge_amount_persisted_falls_back_safely` — null-charge_amount fallback (legacy grants)
5. `test_partial_dispute_then_funds_withdrawn_dedups_on_dispute_id` — H-02 dedup still holds with K-01 fix

### New: `tests/test_k02_concurrent_webhook_race.py` (4 tests)

1. `test_two_concurrent_refund_events_serialize_via_per_charge_lock` — primary K-02 regression
2. `test_concurrent_refund_and_dispute_no_double_debit_on_same_charge`
3. `test_per_charge_lock_does_not_block_unrelated_charges_for_same_user`
4. `test_lock_ordering_charge_then_user_no_deadlock_with_refund_credits`

### Reconciled: 6 existing test files

The mock fixtures (`_StatefulClient`, `_RecordingClient`, and inline variants) intercepted `/rpc/refund_credits` and `stripe_dispute_reversals` POSTs directly. After the K-02 refactor they had to also handle `/rpc/process_charge_reversal` and simulate its server-side semantics:

- `tests/test_j01_partial_refund_sequence.py` — `_StatefulClient` adds `process_charge_reversal` block (dedup by reversal_key, sum by charge_id, append synthetic refund call entry for backward-compat assertions).
- `tests/test_j02_dispute_after_refund.py` — same pattern.
- `tests/test_h02_dispute_dedup.py` — `_RecordingClient` + inline `_SharedStateClient` + `_KeyCapturingClient` extended; 3 tests rewritten to assert against new architecture (`test_charge_refunded_then_dispute_created_both_process_different_keys` updated to reflect correct K-01/J-02 economics).
- `tests/test_h01_grant_pi_linkage.py` — `_RecordingClient` extended.
- `tests/test_b04_refund_dispute.py` — `_RecordingClient` extended.
- `tests/test_i02_dispute_reversal_stable_key.py` — `_RecordingClient` + `_RaceClient` extended; 2 tests rewritten (`test_record_dispute_reversal_or_skip_still_short_circuits_when_row_exists`, `test_insert_dispute_reversal_records_first_event_id`) to assert on RPC payload.

---

## Test counts

| Suite | Before | After |
|---|---|---|
| pytest pass | 281 | 290 |
| pytest skip | 12 | 12 |
| Contract tests (18 + 1 guard) | Pass | Pass |
| Frontend vitest (141 tests / 14 files) | Pass | Pass |

Clean +9 delta on pytest (4 K-02 + 5 K-01) with no regressions.

---

## Migrations applied to live NestD

Project `afnbtbeayfkwznhzafay`. Both migrations applied via Supabase MCP `apply_migration` after local green:

- Migration 020 — verified `stripe_payment_grants.charge_amount` column present, `integer`, nullable.
- Migration 021 — verified `process_charge_reversal` function present with `prosecdef = true` (SECURITY DEFINER).

Latest live migration before this work was `019_i03_marker_tables_rls`.
