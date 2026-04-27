-- Revert migration 020 — K-01 charge_amount column.

BEGIN;

ALTER TABLE public.stripe_payment_grants
  DROP CONSTRAINT IF EXISTS stripe_payment_grants_charge_amount_check;
ALTER TABLE public.stripe_payment_grants
  DROP COLUMN IF EXISTS charge_amount;

COMMIT;
