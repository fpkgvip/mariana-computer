-- Revert migration 022 — U-01 stripe_pending_reversals out-of-order parking lot.

BEGIN;

DROP INDEX IF EXISTS public.idx_stripe_pending_reversals_pi_unapplied;
DROP INDEX IF EXISTS public.idx_stripe_pending_reversals_charge_unapplied;
DROP TABLE IF EXISTS public.stripe_pending_reversals;

COMMIT;
