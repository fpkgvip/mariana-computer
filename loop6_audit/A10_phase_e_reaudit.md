# A10 — Phase E re-audit #5

## Executive summary

I found two new issues that were missed by re-audits #1, #2, #3, and #4.

1. **J-01 [P2] billing/webhooks | sequential partial `charge.refunded` events collapse onto one charge-scoped reversal key, so later partial refunds do not claw back the incremental credits**
   - `_handle_charge_refunded()` always routes plain refunds through `_compute_reversal_key(...)=charge:<charge_id>:reversal`.
   - `_reverse_credits_for_charge()` reads Stripe's cumulative `charge.amount_refunded`, but it still uses that same charge-scoped key for both the pre-check and the `refund_credits()` idempotency key.
   - Result: the first partial refund for a charge is processed, but every later partial refund for that same charge is treated as a duplicate instead of debiting only the newly refunded delta.

2. **J-02 [P2] billing/webhooks | a full-amount dispute after a partial refund can over-claw back the original credits**
   - `charge.refunded` and `charge.dispute.created` intentionally use different reversal keys (`charge:<id>:reversal` vs `dispute:<id>`), so both handlers run for the same underlying purchase.
   - The dispute path passes `dispute.amount` as both `amount` and `amount_refunded`, and `_reverse_credits_for_charge()` therefore debits the full original grant whenever the dispute amount equals the original charge amount.
   - If the charge already had a partial refund reversal, a later full-amount dispute debits the full grant again, overcharging the user by the refunded portion and potentially recording that excess as clawback debt.

I also re-checked the requested hot spots and did not find a reportable new issue in the CSP's `style-src 'unsafe-inline'` allowance or any raw `eval(`, `exec(`, or `shell=True` usage.

---

## J-01 [P2] billing/webhooks | sequential partial `charge.refunded` events collapse onto one charge-scoped reversal key, so later partial refunds do not claw back the incremental credits

**File(s) + line numbers**
- `mariana/api.py:6324-6337` — `_compute_reversal_key()` returns `charge:<charge_id>:reversal` whenever there is no dispute object.
- `mariana/api.py:6491-6552` — `_reverse_credits_for_charge()` reads `amount_refunded` from the charge payload, computes a pro-rata debit from that cumulative value, then calls `refund_credits(..., ref_id=reversal_key)`.
- `mariana/api.py:6573-6583` — successful reversals are inserted into `stripe_dispute_reversals` under that same `reversal_key`.
- `mariana/api.py:6586-6604` — `_handle_charge_refunded()` always uses the no-dispute path.
- `frontend/supabase/migrations/009_f03_refund_debt.sql:41-49` — `credit_clawbacks` is unique on `(ref_type, ref_id)`.
- `frontend/supabase/migrations/009_f03_refund_debt.sql:103-119` — `refund_credits()` also treats an existing `type='refund'` transaction or clawback row with the same `(ref_type, ref_id)` as a duplicate.
- `tests/test_b04_refund_dispute.py:307-345` — regression coverage includes only one partial refund event, not multiple sequential partial refunds for the same charge.

**Reproduction steps (concrete code path)**
1. A user receives 100 credits from an original $100 charge.
2. Stripe emits the first `charge.refunded` event for that charge with `amount=10000` and `amount_refunded=3000`.
3. `_handle_charge_refunded()` calls `_reverse_credits_for_charge()` with `dispute_obj=None`, so `_compute_reversal_key()` returns `charge:ch_1:reversal`.
4. `_reverse_credits_for_charge()` reads `amount_total=10000` and `amount_refunded=3000`, computes `credits_to_debit = floor(100 * 3000 / 10000) = 30`, and calls `refund_credits(..., ref_type='stripe_event', ref_id='charge:ch_1:reversal')`.
5. On success, `_insert_dispute_reversal()` records `reversal_key='charge:ch_1:reversal'`.
6. Stripe later emits a second `charge.refunded` event for the same charge after another partial refund, now with cumulative `amount_refunded=5000`.
7. The second event maps to the same `reversal_key='charge:ch_1:reversal'`. `_record_dispute_reversal_or_skip()` therefore sees the existing row and returns early before any new debit is attempted.
8. Even if that pre-check were bypassed, `refund_credits()` would still treat `(ref_type='stripe_event', ref_id='charge:ch_1:reversal')` as a duplicate and collapse the second reversal.
9. Net result: only 30 credits are clawed back, even though the charge is now 50 percent refunded and the total reversal should be 50 credits.

**Impact**
- Users keep excess credits after sequential partial refunds on the same charge.
- The under-debit scales with each later refund tranche, so a charge refunded in several steps can leave materially more credits than the customer ultimately paid for.
- Because the second and later refund events are treated as clean duplicates, the ledger looks internally consistent while the economic outcome is wrong.

**Recommended fix (specific)**
1. Stop using a charge-scoped reversal key as the idempotency key for all `charge.refunded` events on that charge.
2. Either process refund objects individually using a refund-scoped key, or persist the last processed cumulative `amount_refunded` per charge and debit only the delta when a later `charge.refunded` arrives.
3. Keep dispute dedup separate from refund dedup. The stable dispute key added for H-02/I-02 is correct for disputes, but plain refunds need finer-grained tracking than `charge:<id>:reversal`.
4. Add a regression test that processes two `charge.refunded` events for the same charge with cumulative amounts of 30 percent and then 50 percent, and assert that the second event debits only the additional 20 percent.

**Confidence**: HIGH

---

## J-02 [P2] billing/webhooks | a full-amount dispute after a partial refund can over-claw back the original credits

**File(s) + line numbers**
- `mariana/api.py:6324-6337` — refunds and disputes intentionally get different reversal keys (`charge:<id>:reversal` vs `dispute:<id>`).
- `mariana/api.py:6489-6518` — `_reverse_credits_for_charge()` always starts from `original_credits` on the grant row and debits the full grant whenever `amount_refunded >= amount_total`.
- `mariana/api.py:6540-6552` — the reversal RPC is keyed on `ref_id=reversal_key`, so the earlier refund reversal does not dedup the later dispute reversal.
- `mariana/api.py:6607-6635` — `_handle_charge_dispute_created()` builds a pseudo-charge with `amount=dispute.amount` and `amount_refunded=dispute.amount`, forcing the full-reversal branch when the dispute amount equals the original charge amount.
- `tests/test_h02_dispute_dedup.py:235-274` — current regression coverage explicitly expects `charge.refunded` and `charge.dispute.created` for the same charge to both call the reversal path because they use different keys.
- `tests/test_b04_refund_dispute.py:307-345` — current refund tests cover only a single partial refund and do not exercise a later dispute on the same partially refunded charge.

**Reproduction steps (concrete code path)**
1. A $100 charge grants a user 100 credits.
2. The merchant later issues a partial refund of $30, so `charge.refunded` arrives with `amount=10000` and `amount_refunded=3000`. `_reverse_credits_for_charge()` debits 30 credits under `reversal_key='charge:ch_1:reversal'`.
3. After that partial refund, Stripe can still produce a dispute on the same charge for the full original amount. Stripe's own support article describes the exact case where a charge that was partially refunded is later disputed for the full amount, with issuers often correcting it later.
4. `_handle_charge_dispute_created()` builds `charge_dict = {'id': ch_1, 'payment_intent': pi_1, 'amount': 10000, 'amount_refunded': 10000}` from the dispute object and calls `_reverse_credits_for_charge()`.
5. `_reverse_credits_for_charge()` looks up the original grant again, sets `original_credits=100`, `amount_total=10000`, and `amount_refunded=10000`, then takes the `else` branch and sets `credits_to_debit = original_credits = 100`.
6. Because the dispute path uses `reversal_key='dispute:dp_1'`, neither `_record_dispute_reversal_or_skip()` nor `refund_credits()` treat the earlier refund reversal `charge:ch_1:reversal` as the same business event.
7. Net effect: the user is debited 30 credits for the partial refund and then 100 more credits for the full-amount dispute, for a total clawback of 130 credits against a purchase that originally granted only 100.
8. If the user has already spent credits, the extra 30 is not merely a transient balance issue. `refund_credits()` records the overage as clawback debt that will consume future grants/top-ups.

**Impact**
- A user can be over-debited after a legitimate refund-plus-dispute sequence on the same charge.
- The overcharge equals the portion that was already refunded before the full-amount dispute was processed.
- If the balance is already low, the excess is persisted as debt and silently absorbed from future paid credits.

**Recommended fix (specific)**
1. Track cumulative reversed credits per original charge or payment-intent grant, not per webhook category only.
2. Before processing a dispute reversal, subtract any credits already reversed for prior refunds on that same charge and debit only the remaining unreversed portion.
3. If the platform wants to keep separate refund and dispute ledger rows, compute the dispute debit as `max(0, full_dispute_equivalent - credits_already_reversed_for_this_charge)`.
4. Add a regression test for: original grant 100 credits, partial refund 30 percent, then `charge.dispute.created` with `dispute.amount == original charge amount`; assert that the dispute path debits only the remaining 70 credits, not 100.

**Confidence**: HIGH

---

## Thoroughness evidence / areas checked with no new reportable finding

Reviewed files and paths in this audit, in addition to the prior re-audit baselines (A8, A9):

- `loop6_audit/REGISTRY.md`
- `loop6_audit/A8_phase_e_reaudit.md`
- `loop6_audit/A9_phase_e_reaudit.md`
- `mariana/api.py` — webhook dispatch (`5660-5739`), `_compute_reversal_key` / `_record_dispute_reversal_or_skip` / `_insert_dispute_reversal` / `_reverse_credits_for_charge` / `_handle_charge_refunded` / `_handle_charge_dispute_created` / `_handle_charge_dispute_funds_withdrawn` (`6324-6662`)
- `frontend/supabase/migrations/009_f03_refund_debt.sql` — `credit_clawbacks`, `refund_credits`, and clawback semantics
- `tests/test_b04_refund_dispute.py`
- `tests/test_h02_dispute_dedup.py`
- `frontend/vercel.json`
- Targeted Stripe documentation for dispute/refund reachability and charge/dispute amount semantics

Non-findings after re-check:
- `frontend/vercel.json` still allows `style-src 'unsafe-inline'`, but I did not confirm a concrete exploit from style injection alone under the current CSP.
- Targeted grep did not find any raw `eval(`, `exec(`, or `shell=True` usage in the requested code surfaces.
- I did not identify an additional reportable issue beyond J-01 and J-02 from sequence-reading the current refund/dispute handlers.
