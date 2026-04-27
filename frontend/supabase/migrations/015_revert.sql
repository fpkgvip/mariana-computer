-- ============================================================
-- Revert 015: restore profiles_owner_update_safe to B-33 state
-- (11 wrapped subqueries — SELECT-wrapped per B-33, not consolidated)
-- Also drops the check_profile_immutable helper function.
-- ============================================================

DROP POLICY IF EXISTS profiles_owner_update_safe ON public.profiles;

CREATE POLICY profiles_owner_update_safe ON public.profiles
  FOR UPDATE
  USING ((SELECT auth.uid()) = id)
  WITH CHECK (
    (SELECT auth.uid()) = id
    AND role = (SELECT p.role FROM public.profiles p WHERE p.id = (SELECT auth.uid()))
    AND plan = (SELECT p.plan FROM public.profiles p WHERE p.id = (SELECT auth.uid()))
    AND tokens = (SELECT p.tokens FROM public.profiles p WHERE p.id = (SELECT auth.uid()))
    AND COALESCE(stripe_customer_id, '') = COALESCE((SELECT p.stripe_customer_id FROM public.profiles p WHERE p.id = (SELECT auth.uid())), '')
    AND COALESCE(stripe_subscription_id, '') = COALESCE((SELECT p.stripe_subscription_id FROM public.profiles p WHERE p.id = (SELECT auth.uid())), '')
    AND COALESCE(subscription_status, 'none') = COALESCE((SELECT p.subscription_status FROM public.profiles p WHERE p.id = (SELECT auth.uid())), 'none')
    AND COALESCE(subscription_plan, 'none') = COALESCE((SELECT p.subscription_plan FROM public.profiles p WHERE p.id = (SELECT auth.uid())), 'none')
    AND COALESCE(subscription_current_period_end::text, '') = COALESCE((SELECT p.subscription_current_period_end::text FROM public.profiles p WHERE p.id = (SELECT auth.uid())), '')
    AND COALESCE(suspended_at::text, '') = COALESCE((SELECT p.suspended_at::text FROM public.profiles p WHERE p.id = (SELECT auth.uid())), '')
    AND COALESCE(suspended_reason, '') = COALESCE((SELECT p.suspended_reason FROM public.profiles p WHERE p.id = (SELECT auth.uid())), '')
    AND COALESCE(admin_notes, '') = COALESCE((SELECT p.admin_notes FROM public.profiles p WHERE p.id = (SELECT auth.uid())), '')
  );

-- Drop the helper function added by 015
DROP FUNCTION IF EXISTS public.check_profile_immutable(
  uuid, text, text, integer, text, text, text, text, timestamptz, timestamptz, text, text
);
