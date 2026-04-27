-- ============================================================
-- Migration 008 — F-04: Plan / Entitlement Unification
--
-- Problem: Stripe webhook handlers updated only subscription_plan /
--   subscription_status / subscription_current_period_end.  The
--   investigation-gating logic reads profiles.plan (free/starter/pro/max)
--   for entitlement enforcement.  A downgrade or cancel webhook left
--   profiles.plan='pro' (or 'max') and the user retained premium
--   entitlements indefinitely.
--
-- Fix:
--   1. Replace update_profile_by_stripe_customer to also SET
--      plan = COALESCE(payload->>'plan', plan).
--   2. One-shot reconcile: for every profile where subscription_status
--      is NOT in (active, trialing, past_due) AND plan != 'free',
--      force plan = 'free'.  Gated by loop6_008_applied marker so
--      this backfill never runs twice.
--
-- update_profile_by_id already supports plan via COALESCE (007:180)
--   and is left unchanged.
--
-- SECURITY DEFINER + SET search_path = public, pg_temp on all functions.
-- ============================================================

-- Guard table to make the reconcile pass idempotent.
CREATE TABLE IF NOT EXISTS public.loop6_008_applied (
    applied_at timestamptz NOT NULL DEFAULT now(),
    label      text        NOT NULL DEFAULT 'f04_plan_entitlement_sync'
);

-- -----------------------------------------------------------
-- Replace update_profile_by_stripe_customer to include plan.
-- -----------------------------------------------------------

CREATE OR REPLACE FUNCTION public.update_profile_by_stripe_customer(target_customer_id text, payload jsonb)
  RETURNS void
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
BEGIN
  UPDATE public.profiles
     SET subscription_status = COALESCE(payload->>'subscription_status', subscription_status),
         subscription_plan   = COALESCE(payload->>'subscription_plan',   subscription_plan),
         subscription_current_period_end = CASE
             WHEN payload ? 'subscription_current_period_end'
             THEN (payload->>'subscription_current_period_end')::timestamptz
             ELSE subscription_current_period_end
         END,
         -- F-04: sync plan from webhook payload so enforcement stays consistent.
         plan       = COALESCE(payload->>'plan', plan),
         updated_at = now()
   WHERE stripe_customer_id = target_customer_id;
END;
$$;

-- -----------------------------------------------------------
-- One-shot reconcile: profiles that have a non-active subscription
-- status but still carry a paid plan value must be downgraded to
-- 'free'.  This fixes any existing rows that were never updated by a
-- cancel/delete webhook.
-- -----------------------------------------------------------

DO $$
BEGIN
  -- Only run once (idempotent guard).
  IF EXISTS (SELECT 1 FROM public.loop6_008_applied) THEN
    RAISE NOTICE 'loop6_008_applied already set — skipping reconcile.';
    RETURN;
  END IF;

  UPDATE public.profiles
     SET plan       = 'free',
         updated_at = now()
   WHERE subscription_status NOT IN ('active', 'trialing', 'past_due')
     AND plan IS DISTINCT FROM 'free';

  RAISE NOTICE 'F-04 reconcile complete: profiles with non-active status now have plan=free.';

  INSERT INTO public.loop6_008_applied DEFAULT VALUES;
END;
$$;
