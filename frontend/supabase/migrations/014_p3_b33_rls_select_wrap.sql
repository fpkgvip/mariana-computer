-- ============================================================
-- Migration 014: B-33 — Wrap bare auth.uid()/auth.role() in RLS policies
-- Loop 6 / P3 DB cluster
-- ============================================================
-- Problem: 20+ RLS policies call auth.uid() / auth.role() bare — the
-- planner re-evaluates these volatile functions per row instead of once
-- per query. Wrapping in (SELECT ...) promotes them to InitPlan (evaluated
-- once, cached for all rows), dramatically reducing overhead on large tables.
--
-- Fix pattern: USING (auth.uid() = col) → USING ((SELECT auth.uid()) = col)
--
-- Tables affected by this migration:
--   profiles, credit_clawbacks — confirmed present in local testdb
--   investigations, messages, conversations, conversation_messages,
--   audit_log, feature_flags, admin_tasks, user_vaults, vault_secrets,
--   usage_rollup_daily — present on live NestD; guarded by IF EXISTS blocks
--
-- Ref: A1-13, loop6_audit/A1_db.md
-- ============================================================

-- ============================================================
-- profiles table policies
-- Only profiles_owner_update_safe exists (004 replaced the weak one).
-- Update the USING clause; WITH CHECK is handled by B-34 (migration 015).
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

-- ============================================================
-- credit_clawbacks: credit_clawbacks_owner_select
-- ============================================================
DROP POLICY IF EXISTS credit_clawbacks_owner_select ON public.credit_clawbacks;
CREATE POLICY credit_clawbacks_owner_select ON public.credit_clawbacks
  FOR SELECT
  USING ((SELECT auth.uid()) = user_id);

-- ============================================================
-- investigations: policies from live schema (guard: table may not exist locally)
-- ============================================================
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'investigations'
  ) THEN
    DROP POLICY IF EXISTS "Users can read own investigations" ON public.investigations;
    DROP POLICY IF EXISTS "Users can create own investigations" ON public.investigations;
    DROP POLICY IF EXISTS "Users can update own investigations" ON public.investigations;

    EXECUTE $p$
      CREATE POLICY "Users can read own investigations" ON public.investigations
        FOR SELECT USING ((SELECT auth.uid()) = user_id)
    $p$;
    EXECUTE $p$
      CREATE POLICY "Users can create own investigations" ON public.investigations
        FOR INSERT WITH CHECK ((SELECT auth.uid()) = user_id)
    $p$;
    -- Update policy if it exists (added in some live envs)
    IF EXISTS (
      SELECT 1 FROM pg_policies
      WHERE tablename = 'investigations' AND policyname = 'Users can update own investigations'
    ) THEN
      EXECUTE $p$
        CREATE POLICY "Users can update own investigations" ON public.investigations
          FOR UPDATE USING ((SELECT auth.uid()) = user_id)
      $p$;
    END IF;
  END IF;
END $$;

-- ============================================================
-- messages: policies from live schema
-- ============================================================
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'messages'
  ) THEN
    DROP POLICY IF EXISTS "Users can read own messages" ON public.messages;
    DROP POLICY IF EXISTS "Users can create own messages" ON public.messages;

    EXECUTE $p$
      CREATE POLICY "Users can read own messages" ON public.messages
        FOR SELECT USING (
          investigation_id IN (
            SELECT id FROM public.investigations WHERE user_id = (SELECT auth.uid())
          )
        )
    $p$;
    EXECUTE $p$
      CREATE POLICY "Users can create own messages" ON public.messages
        FOR INSERT WITH CHECK (
          investigation_id IN (
            SELECT id FROM public.investigations WHERE user_id = (SELECT auth.uid())
          )
        )
    $p$;
  END IF;
END $$;

-- ============================================================
-- conversations: policies from live schema
-- ============================================================
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'conversations'
  ) THEN
    DROP POLICY IF EXISTS "Users can read own conversations" ON public.conversations;
    DROP POLICY IF EXISTS "Users can create own conversations" ON public.conversations;
    DROP POLICY IF EXISTS "Users can update own conversations" ON public.conversations;
    DROP POLICY IF EXISTS "Users can delete own conversations" ON public.conversations;

    EXECUTE $p$
      CREATE POLICY "Users can read own conversations" ON public.conversations
        FOR SELECT USING ((SELECT auth.uid()) = user_id)
    $p$;
    EXECUTE $p$
      CREATE POLICY "Users can create own conversations" ON public.conversations
        FOR INSERT WITH CHECK ((SELECT auth.uid()) = user_id)
    $p$;
    EXECUTE $p$
      CREATE POLICY "Users can update own conversations" ON public.conversations
        FOR UPDATE USING ((SELECT auth.uid()) = user_id)
    $p$;
    EXECUTE $p$
      CREATE POLICY "Users can delete own conversations" ON public.conversations
        FOR DELETE USING ((SELECT auth.uid()) = user_id)
    $p$;
  END IF;
END $$;

-- ============================================================
-- conversation_messages: policies from live schema
-- ============================================================
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'conversation_messages'
  ) THEN
    DROP POLICY IF EXISTS "Users can read own conversation messages" ON public.conversation_messages;
    DROP POLICY IF EXISTS "Users can create own conversation messages" ON public.conversation_messages;
    DROP POLICY IF EXISTS "Users can delete own conversation messages" ON public.conversation_messages;

    EXECUTE $p$
      CREATE POLICY "Users can read own conversation messages" ON public.conversation_messages
        FOR SELECT USING (
          conversation_id IN (
            SELECT id FROM public.conversations WHERE user_id = (SELECT auth.uid())
          )
        )
    $p$;
    EXECUTE $p$
      CREATE POLICY "Users can create own conversation messages" ON public.conversation_messages
        FOR INSERT WITH CHECK (
          conversation_id IN (
            SELECT id FROM public.conversations WHERE user_id = (SELECT auth.uid())
          )
        )
    $p$;
    EXECUTE $p$
      CREATE POLICY "Users can delete own conversation messages" ON public.conversation_messages
        FOR DELETE USING (
          conversation_id IN (
            SELECT id FROM public.conversations WHERE user_id = (SELECT auth.uid())
          )
        )
    $p$;
  END IF;
END $$;

-- ============================================================
-- audit_log: audit_log_admin_read policy
-- ============================================================
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'audit_log'
  ) THEN
    DROP POLICY IF EXISTS audit_log_admin_read ON public.audit_log;

    EXECUTE $p$
      CREATE POLICY audit_log_admin_read ON public.audit_log
        FOR SELECT USING (public.is_admin((SELECT auth.uid())))
    $p$;
  END IF;
END $$;

-- ============================================================
-- feature_flags: policies from live schema
-- ============================================================
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'feature_flags'
  ) THEN
    -- Drop known policy names; adjust if live uses different names
    DROP POLICY IF EXISTS feature_flags_admin_all ON public.feature_flags;
    DROP POLICY IF EXISTS feature_flags_authenticated_select ON public.feature_flags;
    DROP POLICY IF EXISTS "feature_flags_admin_all" ON public.feature_flags;
    DROP POLICY IF EXISTS "feature_flags_read" ON public.feature_flags;

    EXECUTE $p$
      CREATE POLICY feature_flags_admin_all ON public.feature_flags
        FOR ALL USING (public.is_admin((SELECT auth.uid())))
    $p$;
    EXECUTE $p$
      CREATE POLICY feature_flags_authenticated_select ON public.feature_flags
        FOR SELECT USING ((SELECT auth.role()) = 'authenticated')
    $p$;
  END IF;
END $$;

-- ============================================================
-- admin_tasks: policies from live schema
-- ============================================================
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'admin_tasks'
  ) THEN
    DROP POLICY IF EXISTS admin_tasks_admin_all ON public.admin_tasks;

    EXECUTE $p$
      CREATE POLICY admin_tasks_admin_all ON public.admin_tasks
        FOR ALL USING (public.is_admin((SELECT auth.uid())))
    $p$;
  END IF;
END $$;

-- ============================================================
-- user_vaults: policies from live schema
-- ============================================================
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'user_vaults'
  ) THEN
    DROP POLICY IF EXISTS user_vaults_owner_select ON public.user_vaults;
    DROP POLICY IF EXISTS user_vaults_owner_insert ON public.user_vaults;
    DROP POLICY IF EXISTS user_vaults_owner_update ON public.user_vaults;
    DROP POLICY IF EXISTS user_vaults_owner_delete ON public.user_vaults;

    EXECUTE $p$
      CREATE POLICY user_vaults_owner_select ON public.user_vaults
        FOR SELECT USING ((SELECT auth.uid()) = user_id)
    $p$;
    EXECUTE $p$
      CREATE POLICY user_vaults_owner_insert ON public.user_vaults
        FOR INSERT WITH CHECK ((SELECT auth.uid()) = user_id)
    $p$;
    EXECUTE $p$
      CREATE POLICY user_vaults_owner_update ON public.user_vaults
        FOR UPDATE USING ((SELECT auth.uid()) = user_id)
    $p$;
    EXECUTE $p$
      CREATE POLICY user_vaults_owner_delete ON public.user_vaults
        FOR DELETE USING ((SELECT auth.uid()) = user_id)
    $p$;
  END IF;
END $$;

-- ============================================================
-- vault_secrets: policies from live schema
-- ============================================================
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'vault_secrets'
  ) THEN
    DROP POLICY IF EXISTS vault_secrets_owner_select ON public.vault_secrets;
    DROP POLICY IF EXISTS vault_secrets_owner_insert ON public.vault_secrets;
    DROP POLICY IF EXISTS vault_secrets_owner_delete ON public.vault_secrets;

    EXECUTE $p$
      CREATE POLICY vault_secrets_owner_select ON public.vault_secrets
        FOR SELECT USING ((SELECT auth.uid()) = user_id)
    $p$;
    EXECUTE $p$
      CREATE POLICY vault_secrets_owner_insert ON public.vault_secrets
        FOR INSERT WITH CHECK ((SELECT auth.uid()) = user_id)
    $p$;
    EXECUTE $p$
      CREATE POLICY vault_secrets_owner_delete ON public.vault_secrets
        FOR DELETE USING ((SELECT auth.uid()) = user_id)
    $p$;
  END IF;
END $$;

-- ============================================================
-- usage_rollup_daily: policies from live schema
-- ============================================================
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'usage_rollup_daily'
  ) THEN
    DROP POLICY IF EXISTS usage_rollup_daily_owner_select ON public.usage_rollup_daily;
    DROP POLICY IF EXISTS usage_rollup_daily_admin_all ON public.usage_rollup_daily;

    EXECUTE $p$
      CREATE POLICY usage_rollup_daily_owner_select ON public.usage_rollup_daily
        FOR SELECT USING ((SELECT auth.uid()) = user_id)
    $p$;
    EXECUTE $p$
      CREATE POLICY usage_rollup_daily_admin_all ON public.usage_rollup_daily
        FOR ALL USING (public.is_admin((SELECT auth.uid())))
    $p$;
  END IF;
END $$;
