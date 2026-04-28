# U-01 follow-up findings

These items surfaced during the U-01 fix but are intentionally deferred so the
core fix stays minimal and surgical.

## 1. No reconciler / observability for stale `stripe_pending_reversals`

If a pending row is parked but the grant event never fires (e.g. the
grant insert path raises 503 forever, or the grant FK target — the
profile — was deleted before the grant could land), the pending row
stays unapplied indefinitely. The fix protects against the OOO race we
audited, but does not include an operator-facing reconciler / alert for
"pending reversals older than N hours with `applied_at IS NULL`".

Suggested follow-up: a periodic worker (parallel to the S-03 agent
settlement reconciler) that emits a metric / alert when
`applied_at IS NULL AND created_at < now() - interval '24 hours'`. No
mutation needed — operator decides whether to issue a manual
`process_charge_reversal` once the underlying ownership is determined.

## 2. Defensive flag heuristic for disputes is conservative

The defensive double-coverage path only triggers a reversal when the
Stripe Charge object reports `refunded=True` / `amount_refunded > 0` /
`disputed=True`. For a dispute, we synthesize a refund-shaped event
because the Charge does not carry the dispute amount. In the OOO
scenario this is fine (the dispute event itself will arrive with full
detail and the reconciler will replay it), but if the dispute event is
permanently lost AND the Charge object happened to expose
`disputed=True` at grant time, we will issue a refund-shaped full
reversal rather than a dispute-shaped one. The K-02 RPC behaviour is
identical (debit credits; insert a reversal row); the only difference
is the recorded `kind`.

Suggested follow-up: capture the `disputed` flag in the synthetic event
metadata so analytics / refund vs. dispute reconciliation reports stay
clean.

## 3. Pending row size

`raw_event` stores the full Stripe Charge / Dispute payload as JSONB.
At the typical OOO arrival rate (well under a row per day) this is
negligible, but if the rate ever climbs we may want a TTL / archive
policy on `applied_at IS NOT NULL` rows older than 90 days. Not
relevant to correctness — purely storage hygiene.

## 4. RLS is enabled but no `FOR ALL USING` policy is defined

`stripe_pending_reversals` enables RLS and grants only `service_role`.
Without an explicit policy, RLS denies all access for non-service
roles, which is the correct fail-safe — but it diverges stylistically
from `stripe_payment_grants` and `stripe_dispute_reversals` (also
enabled, also no policy). Documented for parity awareness; no action
needed.
