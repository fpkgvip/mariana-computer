# A8 — Phase E re-audit #3

## Executive summary

I found two new issues that were missed by re-audits #1 and #2.

1. **H-01 [P1] billing/webhooks | refund/dispute lookup can debit the latest unrelated Stripe grant**
   - The refund/dispute path tries to resolve the original grant via `metadata->>pi_id`, but the grant path never stores `pi_id` in `credit_transactions.metadata`.
   - The code then falls back to the single most recent `type='grant' AND ref_type='stripe_event'` row with no user, charge, or payment-intent scoping.
   - A refund/dispute for one charge can therefore claw back credits from a different user's more recent Stripe grant.

2. **H-02 [P2] billing/webhooks | standard dispute lifecycle can double-claw back the same charge**
   - Both `charge.dispute.created` and `charge.dispute.funds_withdrawn` call the same full-reversal path.
   - Idempotency is keyed only by Stripe `event_id`, so the second dispute event is treated as a fresh reversal.
   - If the first event already consumed the available balance, the second one records a new full clawback deficit, causing future grants/top-ups to be over-debited.

I also re-checked the requested hot spots and did **not** find a reportable new issue in the preview/stream HMAC comparison path, the unsigned intelligence cursors, or the sampled backend-created table exposure under the stated free-tier browser adversary model.

---

## H-01 [P1] billing/webhooks | refund/dispute lookup can debit the latest unrelated Stripe grant

**File(s) + line numbers**
- `mariana/api.py:6042-6077`
- `mariana/api.py:6212-6306`
- `mariana/api.py:6310-6403`
- `frontend/supabase/migrations/009_f03_refund_debt.sql:255-259`

**Reproduction steps (concrete attack chain)**
1. User A completes a Stripe-paid top-up or subscription renewal, which creates a `credit_transactions` grant row through `_grant_credits_for_event()`.
2. `_grant_credits_for_event()` writes the grant via `grant_credits(..., ref_type='stripe_event', ref_id=<event_id>)`, but does **not** persist the PaymentIntent ID anywhere in the grant metadata.
3. The current `grant_credits` function writes grant-row metadata as `jsonb_build_object('source', p_source)`, so `metadata->>'pi_id'` is absent from grant rows.
4. Later, a refund or dispute webhook arrives for User B's charge. `_lookup_grant_tx_for_payment_intent()` first queries `credit_transactions` with `metadata->>pi_id = <payment_intent_id>`.
5. Because grant rows do not store `pi_id`, that primary lookup returns no rows.
6. The function then falls back to `order=created_at.desc&limit=1` over **all** `type='grant' AND ref_type='stripe_event'` rows and returns the latest one globally.
7. `_reverse_credits_for_charge()` trusts that fallback row's `user_id` and `credits`, then calls `refund_credits()` against that user.
8. Result: User B's refund/dispute can claw back User A's credits if User A owns the most recent Stripe grant row at the time the webhook is processed.

A practical adversarial path is: attacker buys a small top-up, waits for any later Stripe grant to land on another account (or an attacker-controlled second account), then triggers a refund/dispute on the first charge. The refunded charge is not mapped back to the attacker’s original grant; the clawback instead lands on whichever account owns the most recent Stripe grant row.

**Impact**
- Cross-account credit theft / misattribution.
- An attacker can potentially receive a cash refund or dispute credit while the platform debits a different user's credits.
- The bug also corrupts ledger integrity because refund/debt history becomes attached to the wrong user and wrong purchase.

**Recommended fix (specific)**
1. Make the grant-to-charge linkage explicit and exact:
   - store `pi_id`, `charge_id` (if available), and/or invoice/payment identifiers in the persisted grant row metadata at grant time; or
   - add a dedicated immutable mapping table keyed by Stripe payment object IDs.
2. Delete the global “latest grant row” fallback entirely. If no exact mapping exists, log and skip the reversal rather than debiting an unrelated user.
3. Add a regression test that creates two users with distinct Stripe grants, sends a refund/dispute for one user’s PaymentIntent, and asserts only that user’s grant is selected.
4. Backfill historical linkage only if it can be done safely; otherwise keep legacy rows non-reversible rather than guessing.

**Confidence**: HIGH

---

## H-02 [P2] billing/webhooks | standard dispute lifecycle can double-claw back the same charge

**File(s) + line numbers**
- `mariana/api.py:6310-6403`
- `mariana/api.py:6420-6465`
- `frontend/supabase/migrations/009_f03_refund_debt.sql:103-118`
- `frontend/supabase/migrations/009_f03_refund_debt.sql:170-190`

**Reproduction steps (concrete attack chain)**
1. A user receives credits from a Stripe charge.
2. Stripe emits `charge.dispute.created`. `_handle_charge_dispute_created()` builds a pseudo-charge and calls `_reverse_credits_for_charge()` for the full amount.
3. `_reverse_credits_for_charge()` calls `refund_credits(..., ref_type='stripe_event', ref_id=<event_id>)`.
4. Later, Stripe emits `charge.dispute.funds_withdrawn` for the same underlying dispute. `_handle_charge_dispute_funds_withdrawn()` again calls `_reverse_credits_for_charge()` for the full amount.
5. Because `refund_credits()` is idempotent only on `(type='refund', ref_type, ref_id)` or `credit_clawbacks(ref_type, ref_id)`, the second dispute event is **not** considered a duplicate: it has a different `event_id`.
6. If the first dispute event already reduced the user’s balance to zero, the second event computes a fresh full `v_deficit` and inserts a new `credit_clawbacks` row for the same charge.
7. Future grants/top-ups are then consumed by both the original reversal and the duplicate clawback, over-debiting the user.

This is not a theoretical corner case: the code intentionally handles both dispute event types, but the dedupe key is the webhook event ID rather than a stable charge/dispute identity.

**Impact**
- Users can be charged back twice for one disputed payment.
- Future paid credits may be silently absorbed by duplicate clawback debt.
- Financial support/debugging becomes difficult because each row appears individually valid even though the combined outcome is wrong.

**Recommended fix (specific)**
1. Deduplicate reversals on a stable business key, not the webhook event ID alone. Examples:
   - `dispute:<dispute_id>` for dispute-driven reversals; or
   - `charge:<charge_id>:reversal` for any irreversible reversal tied to a charge.
2. Choose exactly one canonical dispute event for credit reversal, preferably the final financial event, and make the other event a no-op or status update only.
3. Add a regression test that processes both `charge.dispute.created` and `charge.dispute.funds_withdrawn` for the same disputed charge and asserts only one net reversal/clawback is recorded.
4. Audit existing `credit_clawbacks` / `credit_transactions` for duplicate dispute-derived rows and repair affected accounts.

**Confidence**: HIGH

---

## Thoroughness evidence / areas checked with no new reportable finding

Reviewed files and paths included:
- `loop6_audit/AGENT_BRIEF.md`
- `loop6_audit/REGISTRY.md`
- `loop6_audit/A1_db.md`
- `loop6_audit/A2_api.md`
- `loop6_audit/A3_orchestrator.md`
- `loop6_audit/A4_frontend.md`
- `loop6_audit/A5_adversarial.md`
- `loop6_audit/A6_phase_e_reaudit.md`
- `loop6_audit/A7_phase_e_reaudit.md`
- `mariana/api.py`
- `mariana/data/db.py`
- `frontend/supabase/migrations/007_loop6_b02_b05_b06_ledger_sync.sql`
- `frontend/supabase/migrations/009_f03_refund_debt.sql`
- `frontend/supabase/migrations/010_f05_research_tasks_owner_fk.sql`
- `frontend/supabase/migrations/011_p2_db_cluster_b11_b15.sql`
- `frontend/supabase/migrations/012_p2_b16_admin_set_credits_ledger.sql`
- intelligence pagination helpers in `mariana/orchestrator/intelligence/*`
- targeted billing/webhook tests including `tests/test_b04_refund_dispute.py`, `tests/test_f03_refund_clawback_debt.py`, `tests/test_f04_plan_entitlement_sync.py`, and `tests/test_b31_billing_usage_plan.py`

Non-findings after re-check:
- Preview/stream token verification now uses `hmac.compare_digest`, and the preview token includes a fixed `preview` scope marker.
- The F-06 cursor format is unsigned, but current intelligence queries remain task-scoped and bad cursors fall back to page 1; I did not confirm a concrete cross-user or privilege-escalation path from cursor tampering alone.
- Sampled backend-created tables (`research_tasks`, `hypotheses`) do not have RLS enabled locally, but the sampled privilege view showed only `postgres` table grants, so I did not confirm direct exposure to the stated browser adversary from that posture alone.
- The `flagship`/`max` naming mismatch remains messy and likely causes entitlement inconsistencies, but I did not confirm a clean attacker-value authorization bypass from it within the requested adversary model.
