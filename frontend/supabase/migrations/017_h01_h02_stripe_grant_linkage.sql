-- Migration 017: H-01 + H-02 stripe_payment_grants and stripe_dispute_reversals
-- H-01: Explicit payment-intent → grant mapping; removes global latest-grant fallback.
-- H-02: Dispute reversal deduplication via stable reversal_key.

BEGIN;

-- ---------------------------------------------------------------------------
-- stripe_payment_grants
-- Maps Stripe PaymentIntent IDs to the credit grant they produced.
-- Written at grant time; queried at refund/dispute time to resolve the owner.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.stripe_payment_grants (
  payment_intent_id text PRIMARY KEY,
  charge_id         text,
  user_id           uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  credits           integer NOT NULL CHECK (credits > 0),
  event_id          text NOT NULL,
  source            text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stripe_payment_grants_user
  ON public.stripe_payment_grants(user_id);

CREATE INDEX IF NOT EXISTS idx_stripe_payment_grants_charge
  ON public.stripe_payment_grants(charge_id);

ALTER TABLE public.stripe_payment_grants ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.stripe_payment_grants FROM PUBLIC;
REVOKE ALL ON public.stripe_payment_grants FROM anon;
REVOKE ALL ON public.stripe_payment_grants FROM authenticated;
GRANT ALL ON public.stripe_payment_grants TO service_role;

-- ---------------------------------------------------------------------------
-- stripe_dispute_reversals
-- Records one row per stable reversal_key so duplicate dispute events
-- (dispute.created + dispute.funds_withdrawn) are processed exactly once.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.stripe_dispute_reversals (
  reversal_key      text PRIMARY KEY,
  user_id           uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  charge_id         text,
  dispute_id        text,
  payment_intent_id text,
  credits           integer NOT NULL,
  first_event_id    text NOT NULL,
  first_event_type  text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stripe_dispute_reversals_charge
  ON public.stripe_dispute_reversals(charge_id);

CREATE INDEX IF NOT EXISTS idx_stripe_dispute_reversals_dispute
  ON public.stripe_dispute_reversals(dispute_id);

ALTER TABLE public.stripe_dispute_reversals ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.stripe_dispute_reversals FROM PUBLIC;
REVOKE ALL ON public.stripe_dispute_reversals FROM anon;
REVOKE ALL ON public.stripe_dispute_reversals FROM authenticated;
GRANT ALL ON public.stripe_dispute_reversals TO service_role;

COMMIT;
