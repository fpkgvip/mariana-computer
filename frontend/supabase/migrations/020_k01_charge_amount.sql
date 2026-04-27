-- ============================================================
-- Migration 020 — K-01: persist original charge.amount on
-- stripe_payment_grants so partial-amount disputes can compute
-- pro-rata reversal correctly.
-- ============================================================
-- Bug K-01 (Phase E re-audit #6):
-- _handle_charge_dispute_created and _handle_charge_dispute_funds_withdrawn
-- build a pseudo-charge with amount = amount_refunded = dispute.amount.
-- _reverse_credits_for_charge therefore always trips the else branch
-- (target = full grant), so a $30 partial dispute on a $100 charge
-- debits all 100 credits.
--
-- Fix: capture the original charge.amount (cents) at grant time on
-- stripe_payment_grants. _reverse_credits_for_charge then uses that
-- column as amount_total whenever dispute_obj is present, while
-- amount_refunded keeps coming from dispute.amount.
--
-- The CHECK constraint accepts NULL so existing rows backfilled to
-- NULL remain valid; new rows must populate it. Backfill is best-
-- effort: legacy rows without charge_amount fall through to the
-- existing (full-grant) behaviour with a warning log.
-- ============================================================

BEGIN;

ALTER TABLE public.stripe_payment_grants
  ADD COLUMN IF NOT EXISTS charge_amount integer;

ALTER TABLE public.stripe_payment_grants
  DROP CONSTRAINT IF EXISTS stripe_payment_grants_charge_amount_check;
ALTER TABLE public.stripe_payment_grants
  ADD CONSTRAINT stripe_payment_grants_charge_amount_check
  CHECK (charge_amount IS NULL OR charge_amount > 0);

COMMENT ON COLUMN public.stripe_payment_grants.charge_amount IS
  'Original Stripe charge.amount in cents at grant time. K-01: used by '
  '_reverse_credits_for_charge as amount_total when computing pro-rata '
  'reversal for partial disputes. NULL on legacy rows (best-effort '
  'backfill not feasible without re-fetching Stripe charges); the '
  'reversal flow falls back to the pre-K-01 behaviour and logs a warning '
  'when this column is NULL.';

COMMIT;
