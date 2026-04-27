-- ============================================================
-- Revert 014: restore RLS policies to bare auth.uid() / auth.role() state
-- (pre-B-33 state — reverts the SELECT wrapping)
-- ============================================================

-- profiles_owner_update_safe: restore bare auth.uid() in USING and WITH CHECK
DROP POLICY IF EXISTS profiles_owner_update_safe ON public.profiles;
CREATE POLICY profiles_owner_update_safe ON public.profiles
  FOR UPDATE
  USING (auth.uid() = id)
  WITH CHECK (
    auth.uid() = id
    AND role = (SELECT p.role FROM public.profiles p WHERE p.id = auth.uid())
    AND plan = (SELECT p.plan FROM public.profiles p WHERE p.id = auth.uid())
    AND tokens = (SELECT p.tokens FROM public.profiles p WHERE p.id = auth.uid())
    AND COALESCE(stripe_customer_id, '') = COALESCE((SELECT p.stripe_customer_id FROM public.profiles p WHERE p.id = auth.uid()), '')
    AND COALESCE(stripe_subscription_id, '') = COALESCE((SELECT p.stripe_subscription_id FROM public.profiles p WHERE p.id = auth.uid()), '')
    AND COALESCE(subscription_status, 'none') = COALESCE((SELECT p.subscription_status FROM public.profiles p WHERE p.id = auth.uid()), 'none')
    AND COALESCE(subscription_plan, 'none') = COALESCE((SELECT p.subscription_plan FROM public.profiles p WHERE p.id = auth.uid()), 'none')
    AND COALESCE(subscription_current_period_end::text, '') = COALESCE((SELECT p.subscription_current_period_end::text FROM public.profiles p WHERE p.id = auth.uid()), '')
    AND COALESCE(suspended_at::text, '') = COALESCE((SELECT p.suspended_at::text FROM public.profiles p WHERE p.id = auth.uid()), '')
    AND COALESCE(suspended_reason, '') = COALESCE((SELECT p.suspended_reason FROM public.profiles p WHERE p.id = auth.uid()), '')
    AND COALESCE(admin_notes, '') = COALESCE((SELECT p.admin_notes FROM public.profiles p WHERE p.id = auth.uid()), '')
  );

-- credit_clawbacks: restore bare auth.uid()
DROP POLICY IF EXISTS credit_clawbacks_owner_select ON public.credit_clawbacks;
CREATE POLICY credit_clawbacks_owner_select ON public.credit_clawbacks
  FOR SELECT
  USING (auth.uid() = user_id);

-- investigations: restore bare auth.uid() (if table exists)
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'investigations'
  ) THEN
    DROP POLICY IF EXISTS "Users can read own investigations" ON public.investigations;
    DROP POLICY IF EXISTS "Users can create own investigations" ON public.investigations;
    EXECUTE $p$
      CREATE POLICY "Users can read own investigations" ON public.investigations
        FOR SELECT USING (auth.uid() = user_id)
    $p$;
    EXECUTE $p$
      CREATE POLICY "Users can create own investigations" ON public.investigations
        FOR INSERT WITH CHECK (auth.uid() = user_id)
    $p$;
  END IF;
END $$;
