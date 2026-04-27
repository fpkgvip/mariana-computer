# A12 — Phase E re-audit #7

## Executive summary

I found **1 new issue** that the prior six re-audits missed.

1. **L-01 [P1] billing/webhooks | a non-2xx `stripe_payment_grants` write is silently treated as success, and same-event retries can never repair the missing mapping**
   - `_grant_credits_for_event()` checks `grant_credits()` but does **not** check the HTTP status of the follow-up `POST /rest/v1/stripe_payment_grants` mapping write.
   - If the grant RPC succeeds but the mapping insert returns a 4xx/5xx response, the webhook still succeeds and the Stripe event can be finalized.
   - Worse, a retry of the **same** Stripe event cannot heal the missing mapping because the grant RPC now returns `status='duplicate'`, and the code explicitly skips the `stripe_payment_grants` insert on duplicates.
   - Later refund/dispute handlers require an exact `stripe_payment_grants` row; if it is missing they log `grant_lookup_no_exact_mapping` / `charge_reversal_no_grant_found` and skip reversal entirely.

I also line-audited commit `97bf650`, migrations `020` and `021`, the new `process_charge_reversal` SECURITY DEFINER function, the webhook retry/error paths, lock ordering, migrations `015`–`021`, `mariana/billing/ledger.py`, `frontend/src/lib/api.ts`, `frontend/src/contexts/AuthContext.tsx`, and repo-wide sanity checks for auth gaps, SQL injection, forwarded-header trust, Stripe signature verification, and missing `SET search_path`. I did **not** find another reportable issue beyond L-01.

---

## L-01 [P1] billing/webhooks | a non-2xx `stripe_payment_grants` write is silently treated as success, and same-event retries can never repair the missing mapping

- **Severity:** P1
- **Surface:** api / billing / webhook correctness
- **File + line numbers:**
  - `mariana/api.py:6144-6169` — `_grant_credits_for_event()` only guards network exceptions for the `stripe_payment_grants` insert and never checks `resp.status_code`
  - `mariana/api.py:6145` — duplicate grants explicitly skip mapping insertion (`if pi_id and grant_status != "duplicate":`)
  - `mariana/api.py:6351-6356` — `_lookup_grant_tx_for_payment_intent()` returns `None` when the mapping row is absent
  - `mariana/api.py:6579-6586` — `_reverse_credits_for_charge()` skips reversal when no exact mapping exists

### Reproduction steps

1. Deliver a Stripe grant event (`checkout.session.completed`, `invoice.paid`, or `payment_intent.succeeded`) that reaches `_grant_credits_for_event()`.
2. Let `grant_credits(...)` succeed so the user receives credits and the Stripe event remains eligible to finalize normally.
3. Make the subsequent `POST /rest/v1/stripe_payment_grants` return a non-2xx HTTP response (for example a transient PostgREST 5xx or any schema/permission mismatch). No exception needs to be raised at the client layer — only a non-success status.
4. Observe the current code path:
   - `grant_status` is read from the successful ledger RPC result.
   - The mapping insert is attempted inside `async with httpx.AsyncClient(...): await client.post(...)`.
   - The code catches only transport exceptions; it does **not** inspect the HTTP status code or body.
   - Control returns normally, so the webhook handler can finalize the Stripe event as successful.
5. Retry the **same** Stripe event (for example because finalize failed, or by replaying the identical event through the local test harness). `grant_credits()` now returns `status='duplicate'`, so line 6145 suppresses the `stripe_payment_grants` insert entirely.
6. Later, send `charge.refunded` or `charge.dispute.*` for the same purchase. `_lookup_grant_tx_for_payment_intent()` finds no exact mapping row and returns `None`; `_reverse_credits_for_charge()` logs `charge_reversal_no_grant_found` and exits without clawing back credits.

### Impact

This creates a permanent money-leak path from a single transient failure on the auxiliary mapping write. The user keeps the granted credits, the original Stripe event is treated as successful, and all later refund/dispute handlers for that purchase become no-ops because the exact `payment_intent_id -> grant` linkage was never recorded. The retry behavior makes this worse: once the initial grant succeeded, reprocessing the same Stripe event cannot repair the missing mapping because duplicate grants intentionally skip the insert. In effect, one failed post-grant metadata write can permanently disable all future clawbacks for that payment.

### Recommended fix

1. Treat the `stripe_payment_grants` insert as part of the webhook’s correctness boundary, not as a best-effort side write.
2. After `client.post(...)`, require a success status (`200/201/204` as appropriate). On any non-2xx response, log the body and raise `HTTPException(status_code=503, detail="Credit grant mapping failed")` so Stripe retries the event instead of finalizing it.
3. Decouple mapping repair from grant idempotency. Even when `grant_status == "duplicate"`, retry the `stripe_payment_grants` upsert if the mapping row is missing, or replace the two-step grant+mapping flow with a single SQL RPC/transaction that records both atomically.
4. Add a regression test that simulates:
   - first call: `grant_credits()` succeeds, `stripe_payment_grants` insert returns 500;
   - second call for the same event: `grant_credits()` returns `duplicate`;
   - expected behavior after the fix: the mapping row is retried/repaired or the event remains retriable, and a later refund/dispute still reverses credits correctly.

### Confidence

HIGH

---

## Hot spots reviewed with no new finding

- Commit `97bf650` line-by-line, including `mariana/api.py`, migrations `020` and `021`, and the new tests.
- `020_k01_charge_amount.sql`: nullable `charge_amount` is deliberate for legacy rows; current live project has zero `stripe_payment_grants` rows, so no live legacy-row population exists yet.
- `021_k02_atomic_charge_reversal.sql`: `process_charge_reversal` correctly sets `search_path = public, pg_temp`, uses a distinct per-charge advisory-lock key shape, inserts the dedup row before `refund_credits`, and is granted only to `service_role`.
- Lock ordering audit: I did not find any current code path that acquires the per-user ledger advisory lock and then a per-charge reversal lock.
- Webhook retry path: if `process_charge_reversal` fails before commit, the transaction rolls back and Stripe’s event retry can safely re-enter.
- Repo-wide sanity checks: no new unauthenticated mutating HTTP route, no new forwarded-header trust issue, no Stripe-signature bypass, and no new SECURITY DEFINER function in migrations `015`–`021` missing `SET search_path`.
