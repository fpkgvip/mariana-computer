# U-01 Fix Report — Stripe out-of-order refund/dispute reversal

**Status:** FIXED 2026-04-28
**Severity:** P1
**Surface:** Stripe webhook ordering / reversal processing
**Branch:** loop6/zero-bug

---

## 1. Root cause

Stripe explicitly does not guarantee ordering between events. When
`charge.refunded` or `charge.dispute.*` was delivered BEFORE the
`charge.succeeded` / `payment_intent.succeeded` event that creates the
`stripe_payment_grants` mapping row, the reversal codepath was:

1. `_reverse_credits_for_charge` calls `_lookup_grant_tx_for_payment_intent`.
2. The lookup returns `None`.
3. The function logs `charge_reversal_no_grant_found` and returns success.
4. The outer dispatcher at `api.py:5734-5748` calls `_finalize_webhook_event`,
   marking the row 'completed' in `stripe_webhook_events`.
5. Stripe sees a 2xx response and stops retrying.
6. Later the grant arrives, credits are added, and never reversed.

Net: the user keeps refunded credits permanently. Rare but real on legitimate
out-of-order Stripe delivery.

References (pre-fix line numbers): `mariana/api.py:6606-6613` (return path),
`mariana/api.py:5734-5748` (event finalization).

---

## 2. Fix design

Three layers:

### (a) Persist on no-grant — recovery layer

When `_reverse_credits_for_charge` finds no grant mapping, it now calls
`_record_pending_reversal` which inserts a row into
`stripe_pending_reversals` with `event_id` UNIQUE for idempotency.
Insert failure surfaces as HTTP 503 so Stripe retries the original
webhook event. The reversal handler still returns success on a successful
parking-lot insert, which is correct: Stripe finalizes the event, but
the system has durably committed to performing the reversal.

### (b) Reconcile on grant arrival — main retirement path

`_grant_credits_for_event` ends with a call to
`_reconcile_pending_reversals_for_grant` which:

1. SELECTs `stripe_pending_reversals` rows where `applied_at IS NULL`
   matching either the `payment_intent_id` or the `charge_id` of the
   freshly inserted grant.
2. For each row, replays the reversal through the existing
   `_reverse_credits_for_charge` codepath. That function's lookup
   succeeds now that the grant exists, and it terminates at the K-02
   `process_charge_reversal` SECURITY DEFINER RPC (migration 021).
3. Stamps `applied_at = now()` after the RPC succeeds.

### (c) Defensive double-coverage at grant time — preventive layer

When `_handle_payment_intent_succeeded` (or another grant path) passes a
Stripe `Charge` object, `_grant_credits_for_event` inspects the
`refunded` / `amount_refunded` / `disputed` flags. If any indicate the
charge has already been reversed, a synthetic pending row is inserted
with `event_id = "defensive:<grant_event_id>:reversal"` and the
reconciler picks it up in the same call. The synthetic event_id is
deterministic, so Stripe-replay of the same grant event will UPSERT-noop.

### Idempotency layers

| Layer | Mechanism |
| --- | --- |
| OOO event Stripe-replay | `stripe_pending_reversals.event_id` UNIQUE — second insert is a no-op via `Prefer: resolution=ignore-duplicates`. |
| Reconciler runs twice for same row | `applied_at` filter — once stamped, the row is skipped by the partial index `WHERE applied_at IS NULL`. |
| Reversal RPC replay | K-02 `process_charge_reversal` already dedups on `stripe_dispute_reversals.reversal_key`. The synthetic refund key
`refund_event:<event_id>` is stable per pending row, so the second invocation collapses to `status='duplicate', credits=0`. |
| Defensive synthetic event | Deterministic `event_id` derived from grant `ref_id` collapses Stripe-replay of the grant event itself. |

K-02 is left fully intact: `process_charge_reversal` is unchanged and is
the only mutation path for `credit_transactions` debits. The new path
only adds a parking lot and a reconciliation step — no new ledger
primitives.

---

## 3. Schema diff

**New table** (migration 022):

```sql
CREATE TABLE public.stripe_pending_reversals (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id            text NOT NULL UNIQUE,
  charge_id           text,
  payment_intent_id   text,
  kind                text NOT NULL CHECK (
    kind IN ('refund','dispute_created','dispute_funds_withdrawn')
  ),
  amount_cents        bigint NOT NULL CHECK (amount_cents >= 0),
  currency            text NOT NULL,
  raw_event           jsonb NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  applied_at          timestamptz,
  CHECK (charge_id IS NOT NULL OR payment_intent_id IS NOT NULL)
);

CREATE INDEX idx_stripe_pending_reversals_charge_unapplied
  ON public.stripe_pending_reversals(charge_id) WHERE applied_at IS NULL;
CREATE INDEX idx_stripe_pending_reversals_pi_unapplied
  ON public.stripe_pending_reversals(payment_intent_id) WHERE applied_at IS NULL;

ALTER TABLE public.stripe_pending_reversals ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.stripe_pending_reversals FROM PUBLIC;
REVOKE ALL ON public.stripe_pending_reversals FROM anon;
REVOKE ALL ON public.stripe_pending_reversals FROM authenticated;
GRANT ALL ON public.stripe_pending_reversals TO service_role;
```

No changes to `stripe_payment_grants`, `stripe_dispute_reversals`,
`process_charge_reversal`, `refund_credits`, or `grant_credits`.

---

## 4. Code diff summary

`mariana/api.py`:

* New helpers (~250 LOC) inserted before `_reverse_credits_for_charge`:
  * `_classify_reversal_kind(event_type, dispute_obj)` — string enum.
  * `_record_pending_reversal(...)` — POSTs to `stripe_pending_reversals`
    with `Prefer: resolution=ignore-duplicates,return=minimal`. Raises
    503 on transport / non-2xx so Stripe retries.
  * `_fetch_pending_reversals_for_grant(pi_id, charge_id, cfg)` — GETs
    unapplied rows by either key. Hardened against non-list bodies so
    test fixtures returning canned dicts don't trip it.
  * `_mark_pending_reversal_applied(event_id, cfg)` — PATCH stamps
    `applied_at = now()`.
  * `_reconcile_pending_reversals_for_grant(pi_id, charge_id, cfg)` —
    SELECT then replay via `_reverse_credits_for_charge` then mark
    applied. Re-raises HTTPException so the outer webhook handler
    returns 500 and Stripe retries the grant event.

* `_reverse_credits_for_charge` no-grant branch:
  * Records a pending row via `_record_pending_reversal` before logging
    `charge_reversal_no_grant_found` and returning.

* `_grant_credits_for_event`:
  * New optional `stripe_charge` kwarg.
  * After the `stripe_payment_grants` insert succeeds, runs the
    defensive flag check — if `refunded` / `amount_refunded > 0` /
    `disputed` is true, records a synthetic pending row with
    deterministic `event_id = "defensive:<ref_id>:reversal"`.
  * Then runs `_reconcile_pending_reversals_for_grant` for the
    `pi_id` / `charge_id` pair.

`tests/test_u01_stripe_ooo_reversal.py` (new, ~340 LOC, 4 tests):

1. `test_charge_refunded_before_grant_persists_pending_row` — RED at HEAD.
2. `test_ooo_refund_then_grant_net_zero_credits` — RED at HEAD.
3. `test_ooo_refund_replayed_after_reconciliation_is_idempotent` — RED at HEAD.
4. `test_grant_with_refunded_flag_triggers_defensive_reversal` — RED at HEAD.

`frontend/supabase/migrations/022_u01_stripe_pending_reversals.sql` —
new migration.

`loop6_audit/REGISTRY.md` — U-01 row updated **OPEN** → **FIXED 2026-04-28**.

---

## 5. Test plan

| Test | Scenario | Expected |
| --- | --- | --- |
| `test_charge_refunded_before_grant_persists_pending_row` | OOO refund event with empty `stripe_payment_grants`. | No reversal RPC; pending row stored with `applied_at=None`. |
| `test_ooo_refund_then_grant_net_zero_credits` | OOO refund, then `_grant_credits_for_event` for the same payment_intent. | Grant row exists; pending row's `applied_at` set; exactly one reversal RPC for full credits; `ref_id="refund_event:<event_id>"`. |
| `test_ooo_refund_replayed_after_reconciliation_is_idempotent` | OOO refund, grant, then Stripe replays the original refund event. | Still exactly ONE reversal RPC (process_charge_reversal dedups via `reversal_key`). |
| `test_grant_with_refunded_flag_triggers_defensive_reversal` | No OOO event was sent, but the Stripe Charge passed at grant time has `refunded=True, amount_refunded=full`. | Exactly one reversal RPC for full credits without any prior reversal event. |

Full suite: 359 passed, 13 skipped, 0 failed (was 355 / 13 / 0 before
this change — delta +4).

---

## 6. Live-apply notes

Migration 022 was applied to NestD (`afnbtbeayfkwznhzafay`) via Supabase
MCP `apply_migration`. Verified post-apply with:

```sql
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema='public' AND table_name='stripe_pending_reversals'
ORDER BY ordinal_position;
```

Returned all 10 columns with the expected types and nullability. No
backfill required: the table starts empty; only future OOO events will
populate it.

K-02 `process_charge_reversal` is unchanged. `stripe_payment_grants` is
unchanged. Existing reversal/dispute tests (B-04, H-02, I-02, J-01,
J-02, K-01, K-02) all still pass.

---

## 7. Residual risk

* **Unapplied pending rows that never see a grant.** If a reversal lands
  for a charge whose grant event also fails permanently (e.g. user
  deleted their profile and the FK insert into `stripe_payment_grants`
  hard-fails), the pending row stays parked indefinitely. Mitigation:
  the row is observable in Supabase, monitorable by `applied_at IS NULL
  AND created_at < now() - interval '24 hours'`. A future operator
  reconciler / dashboard alert would close this loop. Out of scope for
  U-01 (treat as a follow-up; recorded in
  `loop6_audit/U01_followup_findings.md`).
* **Reconcile-time grant insert race.** The reconciliation runs after
  the grant insert succeeds. A concurrent reversal webhook arriving
  in the interleave between grant insert and reconciler SELECT will
  succeed via the now-present grant mapping and the K-02 RPC will
  dedup against any prior pending replay via `reversal_key`. No
  double-debit.
* **Defensive flag false positives.** A Charge object can carry
  `refunded=True` only when `amount_refunded == amount`. `disputed=True`
  is set as soon as a dispute exists, even if the dispute will be lost
  in our favor. The defensive path always issues a `charge.refunded`
  shaped reversal, so a dispute-and-favor flow could now reverse credits
  prematurely. Mitigation: in practice, `disputed=True` arrives via
  `charge.dispute.*` events anyway and those carry the exact dispute
  amount; we keep the defensive branch but it only fires when the
  Charge is in fact already-refunded at grant time (the OOO race we are
  closing). Logged at WARNING with `refunded` and `disputed` so
  operators can audit.

---

## 8. Files changed

* `frontend/supabase/migrations/022_u01_stripe_pending_reversals.sql` (new)
* `mariana/api.py` (additions only; no existing function bodies removed)
* `tests/test_u01_stripe_ooo_reversal.py` (new)
* `loop6_audit/REGISTRY.md` (U-01 row)
* `loop6_audit/U01_FIX_REPORT.md` (this file)
* `loop6_audit/U01_followup_findings.md` (deferred reconciler hygiene)
