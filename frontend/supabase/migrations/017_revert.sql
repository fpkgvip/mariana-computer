-- Revert migration 017: drop stripe_payment_grants and stripe_dispute_reversals

BEGIN;

DROP TABLE IF EXISTS public.stripe_dispute_reversals;
DROP TABLE IF EXISTS public.stripe_payment_grants;

COMMIT;
