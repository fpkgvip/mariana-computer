# A11 — Phase E re-audit #6

## Executive summary

I found two new issues that were missed by re-audits #1, #2, #3, #4, and #5 (and which were not introduced by the J-01/J-02 fix; one is structural and one was missed by the J-01/J-02 audit).

1. **K-01 [P2] billing/webhooks | a partial-amount dispute (`dispute.amount < charge.amount`) on a previously un-reversed charge over-debits the original credit grant**
   - `_handle_charge_dispute_created` and `_handle_charge_dispute_funds_withdrawn` build a pseudo-charge with `amount = dispute.amount` and `amount_refunded = dispute.amount`.
   - In `_reverse_credits_for_charge`, that always trips the `else` branch (`amount_refunded >= amount_total`), so `target_credits = original_credits` (the full grant), regardless of how much of the original charge the dispute actually covers.
   - For a $100 charge that granted 100 credits, a dispute filed for only $30 will debit all 100 credits instead of 30.

2. **K-02 [P1 under multi-replica / concurrent-retry; P2 single-replica] billing/webhooks | the J-01/J-02 fix is TOCTOU — two different `charge.refunded` events on the same charge processed concurrently both read `already_reversed = N`, both compute non-overlapping incremental debits, and together exceed the cumulative refund amount**
   - `_sum_reversed_credits_for_charge` is read BEFORE the dedup row is inserted, and the dedup row is keyed per-event (`refund_event:<event_id>`), not per-charge.
   - Two concurrent webhook handlers for events `evt_A` (cumulative `amount_refunded = 30%`) and `evt_B` (cumulative `amount_refunded = 50%`) both pass the SELECT (different reversal_keys), both see `already_reversed = 0`, both compute `incremental_debit = target` (30 and 50 respectively), and both call `refund_credits` with distinct `ref_id`s, so the RPC's idempotency check does not collapse them.
   - Net debit: 30 + 50 = 80 credits, against a true cumulative refund of 50 credits. Over-debit of 30.

I also re-checked every requested hot spot (the `_compute_reversal_key` fallback path, `_sum_reversed_credits_for_charge` network-error fallback, `amount_total == 0` and `amount_refunded > amount_total` corner cases, dispute amount inflation, the 0-credit dedup-row path, the test fixtures, the `event_id` source and Stripe signature verification, and the end-to-end webhook retry idempotency for the same `event_id`). None of those produced a separate reportable finding beyond K-01 and K-02. Migrations 015–019 and the standard surfaces (frontend `api.ts`, `AuthContext.tsx`, edge functions) were also reviewed without a new finding.

---

## K-01 [P2] billing/webhooks | partial-amount disputes over-debit the full original grant

**File(s) + line numbers**
- `mariana/api.py:6705-6733` — `_handle_charge_dispute_created` builds the pseudo-charge.
- `mariana/api.py:6736-6760` — `_handle_charge_dispute_funds_withdrawn` does the same.
- `mariana/api.py:6574-6579` — pro-rata branch: `if amount_total > 0 and amount_refunded < amount_total: target = floor(...)` else `target = original_credits`.
- `frontend/supabase/migrations/017_h01_h02_stripe_grant_linkage.sql:13-21` — `stripe_payment_grants` does NOT store the original charge amount in cents, so the grant row alone cannot be used to recover the true `amount_total`.
- `tests/test_j02_dispute_after_refund.py:303-325` — the only `dispute_only_no_prior_refund` regression test uses `dispute.amount = 10000` against a $100 charge (full dispute), so the partial-dispute case is uncovered.

**Reproduction steps (concrete code path)**

1. A user pays $100 (`amount_total = 10000` cents) and is granted 100 credits via the existing payment-intent grant flow. `stripe_payment_grants` records `payment_intent_id=pi_x, charge_id=ch_x, credits=100`.
2. Stripe later emits `charge.dispute.created` for that charge but for a partial amount, e.g. `dispute.amount = 3000` (a $30 chargeback). Stripe documents that some networks (American Express in particular, plus some `inquiry`-class disputes) allow `dispute.amount < charge.amount`. The dispute object also carries `dispute.amount` adjusted for currency conversion or partial dispute reasons.
3. `_handle_charge_dispute_created` (lines 6705-6733) constructs:
   ```
   charge_dict = {
       "id": "ch_x",
       "payment_intent": "pi_x",
       "amount": 3000,            # ← dispute.amount, NOT the charge.amount
       "amount_refunded": 3000,   # ← also dispute.amount
   }
   ```
4. `_reverse_credits_for_charge` runs. Inside it (line 6552):
   ```
   amount_total = int(charge_obj.get("amount") or 0)              # 3000
   amount_refunded = int(charge_obj.get("amount_refunded") ...)   # 3000
   ```
5. Pro-rata branch (lines 6574-6579):
   ```
   if amount_total > 0 and amount_refunded < amount_total:        # 3000 < 3000 is FALSE
       target_credits = floor(...)
   else:
       target_credits = original_credits                          # = 100
   ```
6. `already_reversed` is 0 (no prior refunds), so `incremental_debit = max(0, 100 - 0) = 100`.
7. `refund_credits` is called with `credits=100` against a charge that was only partially disputed for 30%. The user is over-debited 70 credits.
8. The same flaw recurs in `_handle_charge_dispute_funds_withdrawn` (lines 6736-6760), which builds an identical pseudo-charge.

**Impact**

- A user disputing only a portion of a charge has their entire purchase clawed back.
- For a user who has already spent some credits, the over-debit is recorded as `credit_clawbacks` debt that will silently absorb future paid grants — a money-leak surface that compounds over time.
- The charge’s remaining undisputed portion is still kept by the merchant, so the user is double-penalized: they pay for the part Stripe did not refund, and their credits are also reversed for that part.
- The bug is symmetric across `dispute.created` and `dispute.funds_withdrawn`, so it fires whether the operator disputes-on-creation or only on funds-withdrawn.

**Why this was missed by re-audits #1–#5**

- A6’s F-04 / F-03 were billing-adjacent but focused on entitlement and refund-debt semantics, not dispute amount fidelity.
- A8’s H-02 fix made dispute dedup stable across `dispute.created` and `dispute.funds_withdrawn` but never re-examined whether `dispute.amount` is a faithful proxy for the original charge amount.
- A10’s J-02 fix added incremental-debit math, and the J-02 fix-report’s scenario table only covers `dispute.amount == charge.amount`. A J-02 test even calls out (`tests/test_j02_dispute_after_refund.py:201-207`) that `_handle_charge_dispute_created` sets `amount = amount_refunded`, but treats that as a feature for the “full-amount dispute after partial refund” case rather than as a bug for the standalone partial-dispute case.

**Recommended fix (specific)**

1. Stop conflating `dispute.amount` with the original charge amount. Either:
   - (a) extend `stripe_payment_grants` to persist the original `charge.amount` (cents) at grant time (`_record_grant_for_payment_intent` or equivalent), and have `_handle_charge_dispute_*` build `charge_dict` as `{amount: <stored charge_amount>, amount_refunded: dispute.amount}` so the pro-rata branch fires correctly; or
   - (b) fetch the canonical `Charge` object via `stripe.Charge.retrieve(charge_id)` from the dispute handler and pass its true `amount` as `amount_total`, while keeping `dispute.amount` as `amount_refunded`.
2. Add a partial-dispute regression test: $100 charge granting 100 credits, then `charge.dispute.created` with `dispute.amount = 3000` and no prior refund. Assert that `refund_credits` is called with `credits = 30`, not 100.
3. Add a second regression test that combines K-01 + J-02: prior partial refund 20%, then partial dispute 30% on the same charge. Assert total debit = 30, not 100.
4. Document in `_handle_charge_dispute_created` that the dispute object’s `amount` is the disputed portion, not the original charge amount.

**Confidence**: HIGH (verified by reading `_handle_charge_dispute_created`, `_reverse_credits_for_charge`, the absence of a charge-amount column on `stripe_payment_grants`, and the explicit J-02 regression test commentary at `tests/test_j02_dispute_after_refund.py:201-207`).

---

## K-02 [P1 multi-replica / P2 single-replica] billing/webhooks | TOCTOU between two concurrent `charge.refunded` events on the same charge bypasses incremental-debit math and double-debits

**File(s) + line numbers**
- `mariana/api.py:6477-6505` — `_sum_reversed_credits_for_charge` reads `stripe_dispute_reversals` filtered by `charge_id`.
- `mariana/api.py:6556-6586` — `_reverse_credits_for_charge` calls dedup SELECT, then `_sum_reversed_credits_for_charge`, then `_refund_rpc`, then `_insert_dispute_reversal`. The dedup row is inserted last (lines 6668-6678).
- `mariana/api.py:6324-6354` — `_compute_reversal_key`: refund events get a per-event key `refund_event:<event_id>`, so two different events on the same charge map to two different keys.
- `mariana/api.py:5660-5748` — `stripe_webhook` dispatch: each event_id gets its own `_claim_webhook_event` row; two different event_ids both return NEW and run their handlers concurrently (FastAPI co-routines + multi-replica deployments).
- `frontend/supabase/migrations/009_f03_refund_debt.sql:101-119` — `refund_credits` idempotency is keyed on `(type='refund', ref_type, ref_id)`. Because each event uses a distinct `ref_id = refund_event:<event_id>`, the RPC will NOT collapse the second concurrent call.
- `tests/test_j01_partial_refund_sequence.py:75-137` — the J-01 stateful client only services sequential calls; no asyncio.gather concurrency test exists.

**Reproduction steps (concrete attack chain)**

1. A user receives 100 credits from a $100 Stripe charge (`charge_id = ch_x`).
2. The merchant issues two partial refunds in close succession: refund #1 for $30, refund #2 for $20. Stripe emits two `charge.refunded` events:
   - `evt_A`: `amount = 10000`, `amount_refunded = 3000` (cumulative)
   - `evt_B`: `amount = 10000`, `amount_refunded = 5000` (cumulative)
3. Both events are dispatched to the webhook endpoint. Concurrent delivery is realistic in production for any of these reasons:
   - Stripe retried `evt_A` after a slow first attempt, and the retry overlaps the freshly delivered `evt_B`.
   - The deployment runs multiple uvicorn workers / replicas (the very B-21 fix was applied because the rate limiter was per-process — that confirms multi-process is a supported deployment shape).
   - Even on a single uvicorn process, FastAPI runs each handler as a coroutine, and both handlers `await` on httpx; they interleave inside the same event loop.
4. `_claim_webhook_event` returns NEW for each (different event_id), so both handlers proceed.
5. Handler A runs:
   - `_record_dispute_reversal_or_skip` for `reversal_key = refund_event:evt_A` → empty → False.
   - `_sum_reversed_credits_for_charge(ch_x)` → 0.
   - `incremental = max(0, 30 - 0) = 30`.
   - `_refund_rpc(credits=30, ref_id=refund_event:evt_A)` → pending across the network.
6. Handler B runs concurrently, before A’s `_insert_dispute_reversal` lands:
   - `_record_dispute_reversal_or_skip` for `reversal_key = refund_event:evt_B` → empty (different key) → False.
   - `_sum_reversed_credits_for_charge(ch_x)` → 0 (A has not inserted its dedup row yet).
   - `incremental = max(0, 50 - 0) = 50`.
   - `_refund_rpc(credits=50, ref_id=refund_event:evt_B)` → distinct ref_id, the RPC does NOT dedup against A’s 30-credit refund.
7. Both refunds commit. `credit_transactions` ends up with two rows totalling 80 credits debited; `stripe_dispute_reversals` ends up with two rows totalling 80 credits.
8. The economically correct cumulative debit for `amount_refunded = 5000` against a $100 charge is 50 credits. The user is over-debited by 30 credits, with the excess persisted as `credit_clawbacks` debt if the balance is insufficient.

A symmetric (and slightly more impactful) variant is the refund-then-dispute concurrency:
- `evt_A` is `charge.refunded` with cumulative `amount_refunded = 3000` → target 30, already 0, debit 30 (`refund_event:evt_A`).
- `evt_B` is `charge.dispute.created` with `dispute.amount = 10000` → target 100 (full reversal), already 0, debit 100 (`dispute:dp_x`).
- Both pass dedup (different keys) and both call `_refund_rpc` (different ref_ids).
- Total debit: 130 against an original 100-credit grant. This is the exact J-02 over-debit pattern that the J-01/J-02 fix was supposed to prevent — and it does, but only when the events arrive sequentially.

**Impact**

- Real-money over-debit on every concurrent dispute / refund pair on the same charge.
- The bug reverses J-02’s intended fix under concurrency: the incremental-debit math is only correct when each event’s dedup row has been committed before the next event runs `_sum_reversed_credits_for_charge`. The codebase guarantees no such ordering.
- The same race exists for any combination of refund/refund, refund/dispute, or dispute/dispute pairs as long as the two events have distinct `event_id`s and arrive concurrently.
- Multi-replica deployments amplify the window from “FastAPI coroutine interleave” (~tens of ms) to “cross-replica race” (~hundreds of ms), making this reachable in production with realistic Stripe retry timing.
- Unlike H-02 / I-02 (same dispute, different events, shared `dispute:<id>` key), K-02 is between events with different reversal keys, so the H-02 dedup machinery does not protect against it at all.

**Why this was missed by re-audits #1–#5**

- A8’s H-02 / A9’s I-02 focused on the within-dispute lifecycle (`dispute.created` and `dispute.funds_withdrawn`) where a shared `dispute:<id>` key collapses both events. The J-01 fix introduced per-event refund keys (`refund_event:<event_id>`) explicitly so sequential partial refunds do NOT dedup, which is correct for the sequential case but reopens the I-02-shaped TOCTOU in a different shape.
- A10’s J-01/J-02 audit and fix-report focus on sequential semantics; the test suite (`tests/test_j01_partial_refund_sequence.py`) drives events sequentially with a stateful client and never tests `asyncio.gather` of two events.
- The J-01 fix-report’s “Correctness verification” table assumes the dedup row is committed before the next event runs `_sum_reversed_credits_for_charge`, which is true sequentially but false concurrently.

**Recommended fix (specific)**

1. Move the dedup-row INSERT to BEFORE the `_refund_rpc` call, so the SELECT/INSERT sequence becomes a true claim. Use `Prefer: resolution=ignore-duplicates,return=representation` so the response body distinguishes “I claimed this row” from “someone else already claimed it”. If the INSERT was a duplicate, skip the refund. If it was a fresh claim, run the refund; on RPC failure, DELETE the dedup row so the retry can re-claim.
2. Additionally, serialize `_reverse_credits_for_charge` per-charge by acquiring `pg_advisory_xact_lock(hashtextextended('charge:'||charge_id, 0))` at the start of the reversal flow. This collapses two concurrent events on the same charge into a sequential ordering even across replicas. The lock is released at transaction commit, so the second event sees the first’s INSERT and computes the correct `already_reversed`.
3. Alternatively, do the entire reversal computation inside a SECURITY DEFINER PL/pgSQL function that takes the per-charge advisory lock, sums `stripe_dispute_reversals` rows, decides incremental debit, calls `refund_credits`, and inserts the dedup row — all in one transaction. The webhook handler in `api.py` would just pass `(charge_id, target_credits, reversal_key, user_id, …)` to this function. This is the structurally cleanest fix because it is impossible for two concurrent webhooks to interleave the read/decide/write triplet.
4. Add a regression test that uses `asyncio.gather` to dispatch two distinct `charge.refunded` events on the same charge concurrently (with cumulative `amount_refunded = 3000` and `amount_refunded = 5000`) and asserts that the total `credits_to_debit` across both `_refund_rpc` calls is exactly 50 (not 80). The test must hold the first event’s `_refund_rpc` mid-flight (e.g., via an asyncio.Event in the mocked refund RPC) while the second event runs to its conclusion.
5. Add a second regression test for the refund-then-dispute concurrency variant (refund cumul 30%, dispute amount 10000, fired concurrently). Assert total debit = 100, not 130.
6. Audit the live `credit_transactions` / `credit_clawbacks` for refund pairs whose `metadata->>charge_id` (or `stripe_dispute_reversals.charge_id`) matches and whose timestamps are within seconds; refund affected accounts.

**Confidence**: HIGH (verified by reading the SELECT/RPC/INSERT ordering at `mariana/api.py:6556-6678`, by confirming the J-01 stateful client only services sequential calls at `tests/test_j01_partial_refund_sequence.py:75-137`, and by tracing `stripe_webhook` dispatch which gives different event_ids independent NEW claims at `mariana/api.py:5660-5748`).

---

## Thoroughness evidence / areas re-checked with no new reportable finding

I read the AGENT_BRIEF, _SHARED_CONTEXT, all five prior re-audits (A6 through A10), the J-01/J-02 fix report, and the relevant slices of the registry. I then re-executed each prior auditor’s key checks plus the targeted hot-spots in the task brief.

Re-checked items, with the conclusion:

- **`_compute_reversal_key` fallback at `api.py:6324-6354`**: when `dispute_obj is None` and `refund_event_id is None`, returns `charge:<id>:reversal` (the legacy J-01 key). Currently `_handle_charge_refunded` always passes `refund_event_id=event_id` (line 6701) and there is no other caller of `_reverse_credits_for_charge` with `dispute_obj=None`, so the legacy path is unreachable. If a future caller forgets to pass `refund_event_id`, the system silently regresses to J-01 collapse — a latent footgun, not a current finding.

- **`_sum_reversed_credits_for_charge` network-error fallback (`api.py:6477-6505`)**: returns 0 on httpx error or non-200. Worst case is a redundant debit, but the refund_credits RPC dedups on `(ref_type, ref_id=reversal_key)`, so a same-event retry of the same handler still collapses at the SQL layer. For DIFFERENT events on the same charge, this fallback amplifies K-02 (returns 0 even when prior reversals exist), but K-02 is reportable on its own merits without invoking the network-error path. RLS blocking the SELECT in the misconfiguration case (anon key only, `stripe_dispute_reversals` revokes anon) returns 401/403 → also non-200 → also 0; same conclusion.

- **`amount_total == 0` and `amount_refunded > amount_total`**: both fall into the else branch with `target_credits = original_credits`, capped by `incremental_debit = max(0, target - already_reversed)`. The cap saves us in normal flow. If `amount_total == 0` is delivered by Stripe for a real charge it would over-debit; this is unrealistic and not reportable.

- **0-credit dedup row insertion failure (lines 6588-6608)**: `_insert_dispute_reversal` swallows network errors. If the INSERT fails for a 0-credit row, the next webhook delivery for the SAME `event_id` is short-circuited at `_claim_webhook_event` (status='completed') so it cannot re-process. If the OUTER `_finalize_webhook_event` ALSO fails before the 0-credit insert, the next retry does re-enter the handler, hits `incremental_debit <= 0` again, and tries the INSERT again. Idempotent. Not a finding.

- **Test fixture realism (`tests/test_j01_*` and `tests/test_j02_*`)**: the `_StatefulClient` correctly simulates DB persistence across sequential async handler calls. It does NOT simulate concurrency (no asyncio.gather), and the J-01/J-02 audit explicitly designed the fixture for sequential semantics. This is what enabled K-02 to slip through.

- **`event_id` source / Stripe signature verification**: `event_id = event.get("id")` is read from the verified Stripe payload after `stripe.Webhook.construct_event` (signature check). A malicious request without a valid signature is rejected at signature verification, so an attacker cannot supply a forged event_id. Confirmed.

- **End-to-end retry idempotency for the same event_id**: `_claim_webhook_event` returns DUPLICATE on retries of completed events; same-event retries of in-flight events get RETRY and re-run the handler, which is safe because (a) `refund_credits` dedups on `(ref_type, ref_id=reversal_key)`, and (b) `reversal_key = refund_event:<event_id>` is stable across retries of the same event. Confirmed safe.

- **Migrations 015–019**: 015 (`check_profile_immutable`) introduces `COALESCE(p.col = p_col, true)` which is looser than the prior `col = (SELECT p.col …)` form, but `profiles.role`, `profiles.plan`, `profiles.tokens` are all NOT NULL on live, so a NULL on the supplied side would be rejected by the column-level NOT NULL constraint before WITH CHECK matters. The COALESCE-true behaviour for nullable columns (`stripe_customer_id`, `subscription_status`, etc.) matches the prior policy via `COALESCE(..., '') = COALESCE(..., '')`, so no security gap. 016/017/018/019 already reviewed in A8/A9; no new finding.

- **`add_credits` advisory lock (mig 018)**: I-01 fix. Lock is acquired immediately after input validation and before reads. Matches `grant_credits` / `refund_credits`. Confirmed correct.

- **Standard surfaces**: `frontend/src/lib/api.ts` is clean (Bearer attached to all auth’d calls, network-error normalised). `frontend/src/contexts/AuthContext.tsx` is unchanged from B-28 fix shape. No `frontend/supabase/functions/` directory exists; there are no edge functions.

- **`service_role` key exposure**: `_supabase_api_key(cfg)` prefers `SUPABASE_SERVICE_KEY` and falls back to `SUPABASE_ANON_KEY`. The service key never reaches the browser. The frontend uses the anon key only. No new exposure found.

- **`add_credits` / `spend_credits` / `refund_credits` race conditions (mig 009, 018)**: each acquires `pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0))` before any reads. The per-user serialization is correct.

Items intentionally NOT re-investigated (already canonical in REGISTRY.md or previous re-audits, no new evidence):

- F-01..F-06, G-01, H-01, H-02, I-01, I-02, I-03, J-01, J-02 (all FIXED, all verified by their respective fix reports).
- B-01..B-46 surface-level findings (consolidated in REGISTRY.md).
