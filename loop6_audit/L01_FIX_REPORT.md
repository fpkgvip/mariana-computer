# L-01 Fix Report

**Branch**: loop6/zero-bug
**Date**: 2026-04-28
**Scope**: `mariana/api.py` (single function) + 6 new tests + 1 reconciled test

---

## Root Cause

`_grant_credits_for_event()` in `mariana/api.py` performed two writes per
grant event:

1. `grant_credits` RPC against the credits ledger (idempotent on
   `(ref_type, ref_id)`).
2. `POST /rest/v1/stripe_payment_grants` to record the
   `payment_intent_id -> grant` mapping that later refund/dispute handlers
   use for exact lookups.

Two latent defects on the second write created a money-leak path:

1. The auxiliary POST was wrapped only in `try/except Exception`, which
   handled transport faults. The code never inspected `resp.status_code`,
   so a PostgREST `4xx` or `5xx` response (transient failure, schema
   mismatch, role permission gap, etc.) returned control normally. The
   webhook then finalized the Stripe event as successful and the user
   kept the credits, but the mapping row was missing.

2. Worse, the insert was gated by `grant_status != "duplicate"`. Once the
   grant ledger row existed, a Stripe retry of the same event found
   `grant_credits` returning `status='duplicate'` and the code
   intentionally skipped the mapping POST. The missing mapping could
   never heal.

Downstream effect: `_lookup_grant_tx_for_payment_intent()` returned `None`,
`_reverse_credits_for_charge()` logged `charge_reversal_no_grant_found`,
and refund/dispute events for that purchase were silent no-ops. One
transient failure on the auxiliary mapping write permanently disabled all
future clawbacks for that `payment_intent_id`.

---

## Changes per file

### `mariana/api.py` — `_grant_credits_for_event` (lines 6142-6204)

1. **Removed the duplicate gate**: `if pi_id and grant_status != "duplicate":`
   becomes `if pi_id:`. The mapping insert always runs when the caller
   supplied a `pi_id`. The existing
   `Prefer: resolution=ignore-duplicates,return=minimal` header on the
   POST keeps repeat inserts against an already-present row a safe
   server-side no-op (PostgREST collapses to a 2xx with empty body).
2. **Capture and check the response**: `pg_resp = await client.post(...)`
   (was discarded). After the await, if `pg_resp.status_code` is not in
   `{200, 201, 204}`, log the status and a 500-char-truncated body, then
   raise `HTTPException(status_code=503, detail="Credit grant mapping
   failed")`. The 503 surfaces as a non-2xx webhook response, so Stripe
   retries the event delivery.
3. **Transport exceptions also raise 503**: the `except Exception as exc`
   branch previously logged and returned, leaving the webhook to
   finalize. It now logs `stripe_payment_grants_insert_transport_error`
   and raises 503 from `exc`. The grant ledger entry already exists; on
   retry `grant_credits` returns `duplicate` and (by change #1) the
   mapping insert is reattempted.
4. Removed the `grant_status` local that is no longer read.

The grant_credits RPC failure path was left untouched (already raised
503).

### `tests/test_h01_grant_pi_linkage.py` — case 4 reconciled

The H-01 fix asserted that duplicate grants must skip the
stripe_payment_grants insert. That assertion is now wrong (it was the
exact bug). Renamed the test to
`test_duplicate_grant_status_still_attempts_stripe_payment_grants_insert`
and inverted its expectation: when `grant_credits` returns `duplicate`
the mapping insert must still be attempted so a missing row from a prior
failed delivery can heal. The Prefer header makes the repeat safe.

The other five H-01 cases were unchanged.

---

## Correctness verification

| Scenario | Before | After |
|---|---|---|
| Grant ok + mapping POST 500 | Webhook 200, mapping row missing forever | Raises 503, Stripe retries, mapping persists |
| Grant ok + mapping transport error | Logged, webhook 200, mapping missing forever | Raises 503, Stripe retries, mapping persists |
| Grant ok + mapping POST 201 (happy) | Mapping persisted | Mapping persisted (regression) |
| Stripe retry: grant=duplicate, mapping was missing | Insert skipped, mapping permanently missing | Insert attempted, row written |
| Stripe retry: grant=duplicate, mapping already present | Insert skipped (correct outcome by accident) | Insert attempted; PostgREST `ignore-duplicates` collapses to safe no-op |
| `pi_id` is `None` (subscription checkout) | No mapping write | No mapping write (regression) |

Idempotency invariant preserved: the unique constraint on
`stripe_payment_grants` plus
`Prefer: resolution=ignore-duplicates,return=minimal` makes repeat
inserts of the same `(payment_intent_id, event_id)` safe — the server
returns `201` with empty body and no row is written twice. The grant
ledger remains protected by `grant_credits`'s
`(ref_type, ref_id)` idempotency.

No migration required: pure Python fix.

---

## Tests added

### New: `tests/test_l01_mapping_insert_failure.py` (6 tests)

1. `test_a_mapping_insert_500_raises_503_for_stripe_retry` — primary
   regression: grant ok, mapping 500 -> raises HTTPException(503).
2. `test_b_duplicate_grant_still_retries_mapping_to_heal_missing_row` —
   grant returns `duplicate`, mapping insert is still attempted with
   correct payload and the `ignore-duplicates` Prefer header.
3. `test_c_happy_path_grant_and_mapping_both_succeed` — happy-path
   regression.
4. `test_d_same_event_retry_idempotent_no_error` — two sequential
   deliveries of the same event (granted then duplicate); both mapping
   inserts attempted, neither raises.
5. `test_e_duplicate_grant_with_existing_mapping_no_error` — duplicate
   grant + 201 with empty body (PostgREST under
   `ignore-duplicates,return=minimal` when the row already exists);
   must not raise.
6. `test_f_transport_exception_on_mapping_raises_503` — `httpx.ConnectError`
   on mapping POST surfaces as 503 instead of being silently swallowed.

### Reconciled: `tests/test_h01_grant_pi_linkage.py`

Case 4 (`test_duplicate_grant_status_skips_stripe_payment_grants_insert`)
inverted to `test_duplicate_grant_status_still_attempts_stripe_payment_grants_insert`.
Asserts `len(insert_calls) == 1` (was `== 0`).

---

## Test counts before/after

| Suite | Before | After |
|---|---|---|
| pytest pass | 290 | 296 |
| pytest skip | 12 | 12 |
| Contract tests (18 + G01) | Pass | Pass |
| Frontend vitest (141 tests / 14 files) | Pass | Pass |

Clean +6 delta (6 new L-01 tests; the H-01 case 4 rename keeps file
count constant). No new skips. No regressions in any other suite.

---

## Tradeoffs and follow-ups

- The fix raises 503 on mapping failure rather than rolling back the
  grant. The grant is idempotent on `(ref_type, ref_id)`, so a Stripe
  retry collapses on the ledger side and only the mapping write is
  retried. This is the intended convergence behavior; no
  compensating-debit code path is needed.
- An alternative atomic design would move both writes inside a single
  SECURITY DEFINER SQL RPC. That would eliminate the dual-write window
  entirely. Deferred; the current fix already closes the
  observable money-leak path and the dual-write remains
  retry-convergent.
- No live migration was applied (pure Python fix).
