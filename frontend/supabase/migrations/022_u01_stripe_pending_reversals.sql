-- ============================================================
-- Migration 022 — U-01: persist out-of-order Stripe reversal events
-- ============================================================
-- Bug U-01 (Phase E re-audit #20):
-- Stripe does not guarantee delivery ordering between charge.refunded /
-- charge.dispute.* and the charge.succeeded / payment_intent.succeeded
-- event that creates the stripe_payment_grants mapping row. When the
-- reversal event lands first, _reverse_credits_for_charge looks up the
-- grant, finds nothing, logs charge_reversal_no_grant_found, and returns
-- success. The outer webhook dispatcher then marks the event 'completed'
-- in stripe_webhook_events so Stripe stops retrying. Later the grant
-- arrives and is credited, but never reversed — credits remain on the
-- account permanently.
--
-- Fix: record an entry in stripe_pending_reversals whenever the grant
-- mapping is missing at reversal time. When the grant is later inserted
-- into stripe_payment_grants, the API checks this table for matching
-- pending rows, replays them through _reverse_credits_for_charge's
-- standard codepath, and stamps applied_at.
--
-- Idempotency:
--   * UNIQUE (event_id) — Stripe-replay of the same reversal event is
--     collapsed at insert time.
--   * applied_at — once stamped, the reconciliation pass at grant
--     insert time skips the row.
--   * The standard reversal codepath terminates at process_charge_reversal
--     (migration 021) which dedups on stripe_dispute_reversals.reversal_key,
--     so a double-trigger of the same pending row would be a no-op anyway.
-- ============================================================

CREATE TABLE IF NOT EXISTS public.stripe_pending_reversals (
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

CREATE INDEX IF NOT EXISTS idx_stripe_pending_reversals_charge_unapplied
  ON public.stripe_pending_reversals(charge_id)
  WHERE applied_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_stripe_pending_reversals_pi_unapplied
  ON public.stripe_pending_reversals(payment_intent_id)
  WHERE applied_at IS NULL;

ALTER TABLE public.stripe_pending_reversals ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.stripe_pending_reversals FROM PUBLIC;
REVOKE ALL ON public.stripe_pending_reversals FROM anon;
REVOKE ALL ON public.stripe_pending_reversals FROM authenticated;
GRANT ALL ON public.stripe_pending_reversals TO service_role;

COMMENT ON TABLE public.stripe_pending_reversals IS
  'U-01: parking lot for Stripe charge.refunded / charge.dispute.* events that arrived before the corresponding stripe_payment_grants row existed. Reconciled on grant insert.';
