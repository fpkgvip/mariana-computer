-- =============================================================================
-- Migration: 004_revert.sql
-- Purpose:   Revert 004_loop5_idempotency_and_rls.sql.
-- Mirror of: 004 (RLS policies + admin_set_credits restoration).
--
-- WARNING: Reverting re-introduces the R1 privilege-escalation surface
-- on profiles.subscription_status / subscription_plan. Only run this if
-- 004 caused a regression that is worse than R1.
--
-- Companion: 004b_revert.sql restores the narrow uq_credit_tx_grant_ref index.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- Revert R1: restore both UPDATE policies on profiles.
-- -----------------------------------------------------------------------------

DROP POLICY IF EXISTS profiles_owner_update_safe ON public.profiles;

-- Re-create the older permissive policy verbatim from live state snapshot.
CREATE POLICY "Users can update own profile" ON public.profiles
  FOR UPDATE
  USING (auth.uid() = id)
  WITH CHECK (
    (auth.uid() = id)
    AND (NOT (role IS DISTINCT FROM (SELECT p.role FROM public.profiles p WHERE p.id = auth.uid())))
    AND (NOT (tokens IS DISTINCT FROM (SELECT p.tokens FROM public.profiles p WHERE p.id = auth.uid())))
    AND (NOT (plan IS DISTINCT FROM (SELECT p.plan FROM public.profiles p WHERE p.id = auth.uid())))
    AND (NOT (stripe_customer_id IS DISTINCT FROM (SELECT p.stripe_customer_id FROM public.profiles p WHERE p.id = auth.uid())))
    AND (NOT (stripe_subscription_id IS DISTINCT FROM (SELECT p.stripe_subscription_id FROM public.profiles p WHERE p.id = auth.uid())))
    AND (NOT (subscription_status IS DISTINCT FROM (SELECT p.subscription_status FROM public.profiles p WHERE p.id = auth.uid())))
    AND (NOT (subscription_plan IS DISTINCT FROM (SELECT p.subscription_plan FROM public.profiles p WHERE p.id = auth.uid())))
  );

-- Re-create the original profiles_owner_update_safe policy verbatim from live
-- state snapshot (loop5_research/live_policies.json).
CREATE POLICY profiles_owner_update_safe ON public.profiles
  FOR UPDATE
  USING (auth.uid() = id)
  WITH CHECK (
    (auth.uid() = id)
    AND (role = (SELECT p.role FROM public.profiles p WHERE p.id = auth.uid()))
    AND (plan = (SELECT p.plan FROM public.profiles p WHERE p.id = auth.uid()))
    AND (tokens = (SELECT p.tokens FROM public.profiles p WHERE p.id = auth.uid()))
    AND (COALESCE(stripe_customer_id, '') = COALESCE((SELECT p.stripe_customer_id FROM public.profiles p WHERE p.id = auth.uid()), ''))
    AND (COALESCE(stripe_subscription_id, '') = COALESCE((SELECT p.stripe_subscription_id FROM public.profiles p WHERE p.id = auth.uid()), ''))
    AND (COALESCE(suspended_at::text, '') = COALESCE((SELECT p.suspended_at::text FROM public.profiles p WHERE p.id = auth.uid()), ''))
  );

-- -----------------------------------------------------------------------------
-- Revert admin_set_credits to live snapshot body.
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.admin_set_credits(
  target_user_id uuid,
  new_credits    integer,
  is_delta       boolean DEFAULT false
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
AS $function$
DECLARE
  caller_role text;
  current_tokens integer;
  final_balance integer;
BEGIN
  SELECT role INTO caller_role FROM profiles WHERE id = auth.uid();
  IF caller_role IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION 'Admin access required';
  END IF;

  IF is_delta THEN
    SELECT tokens INTO current_tokens FROM profiles WHERE id = target_user_id;
    IF current_tokens IS NULL THEN
      RAISE EXCEPTION 'User not found';
    END IF;
    final_balance := GREATEST(0, current_tokens + new_credits);
  ELSE
    final_balance := new_credits;
  END IF;

  UPDATE profiles SET tokens = final_balance, updated_at = now()
  WHERE id = target_user_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'User not found';
  END IF;

  RETURN final_balance;
END;
$function$;

REVOKE ALL ON FUNCTION public.admin_set_credits(uuid, integer, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.admin_set_credits(uuid, integer, boolean) TO authenticated;

COMMIT;
