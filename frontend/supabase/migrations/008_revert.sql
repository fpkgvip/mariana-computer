-- ============================================================
-- Revert Migration 008 — F-04: Plan / Entitlement Unification
--
-- Restores update_profile_by_stripe_customer to the 007 version
-- (without the plan column) and removes the guard table.
--
-- WARNING: This does NOT undo the reconcile DML (plan='free' rows
-- that were downgraded remain at 'free').  A data restore from backup
-- would be needed to undo that.
-- ============================================================

-- Restore update_profile_by_stripe_customer to the 007 definition
-- (no plan column).
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
         updated_at = now()
   WHERE stripe_customer_id = target_customer_id;
END;
$$;

-- Remove the guard table.
DROP TABLE IF EXISTS public.loop6_008_applied;
