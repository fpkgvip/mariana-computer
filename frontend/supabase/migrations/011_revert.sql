-- ============================================================
-- Revert 011: P2 DB Cluster B-11..B-15
-- Restores pre-fix state for testing purposes only.
-- WARNING: This re-introduces the security vulnerabilities fixed in 011.
-- ============================================================

-- ============================================================
-- Revert B-11: Restore inline subquery pattern in admin helpers
-- ============================================================
CREATE OR REPLACE FUNCTION public.admin_count_profiles()
  RETURNS integer
  LANGUAGE sql
  STABLE
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
  SELECT COUNT(*)::integer FROM public.profiles
   WHERE (SELECT role FROM public.profiles WHERE id = auth.uid()) = 'admin';
$$;

CREATE OR REPLACE FUNCTION public.admin_list_profiles()
  RETURNS TABLE (
    id                    uuid,
    email                 text,
    full_name             text,
    tokens                integer,
    role                  text,
    stripe_customer_id    text,
    subscription_plan     text,
    subscription_status   text,
    created_at            timestamptz
  )
  LANGUAGE sql
  STABLE
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
  SELECT p.id, p.email, p.full_name, p.tokens, p.role,
         p.stripe_customer_id, p.subscription_plan, p.subscription_status, p.created_at
    FROM public.profiles p
   WHERE (SELECT role FROM public.profiles WHERE id = auth.uid()) = 'admin'
   ORDER BY p.created_at DESC;
$$;

-- ============================================================
-- Revert B-12: Restore EXECUTE to authenticated on admin_audit_insert
-- ============================================================
GRANT EXECUTE ON FUNCTION public.admin_audit_insert(uuid, text, text, text, jsonb, jsonb, jsonb, text, text)
  TO authenticated;

-- Restore original body (without actor_id check, with search_path including auth)
CREATE OR REPLACE FUNCTION public.admin_audit_insert(
  p_actor_id    uuid,
  p_action      text,
  p_target_type text    DEFAULT NULL,
  p_target_id   text    DEFAULT NULL,
  p_before      jsonb   DEFAULT NULL,
  p_after       jsonb   DEFAULT NULL,
  p_metadata    jsonb   DEFAULT NULL,
  p_ip          text    DEFAULT NULL,
  p_user_agent  text    DEFAULT NULL
)
  RETURNS uuid
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = 'public', 'auth'
AS $$
DECLARE
  v_id    uuid;
  v_email text;
BEGIN
  IF NOT public.is_admin(p_actor_id) THEN
    RAISE EXCEPTION 'not_admin';
  END IF;
  SELECT email INTO v_email FROM public.profiles WHERE id = p_actor_id;
  INSERT INTO public.audit_log (actor_id, actor_email, action, target_type, target_id,
                                before, after, metadata, ip_address, user_agent)
  VALUES (p_actor_id, v_email, p_action, p_target_type, p_target_id,
          p_before, p_after, p_metadata, p_ip, p_user_agent)
  RETURNING id INTO v_id;
  RETURN v_id;
END;
$$;

-- ============================================================
-- Revert B-13: Restore EXECUTE to anon/authenticated on expire_credits
-- ============================================================
GRANT EXECUTE ON FUNCTION public.expire_credits()
  TO anon, authenticated;

-- ============================================================
-- Revert B-14: Restore simple handle_new_user
-- ============================================================
CREATE OR REPLACE FUNCTION public.handle_new_user()
  RETURNS trigger
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
BEGIN
  INSERT INTO public.profiles (id, email, full_name)
  VALUES (NEW.id, NEW.email, NEW.raw_user_meta_data->>'full_name');
  RETURN NEW;
END;
$$;

-- ============================================================
-- Revert B-15: Restore FK references to auth.users
-- ============================================================
ALTER TABLE public.credit_buckets
  DROP CONSTRAINT IF EXISTS credit_buckets_user_id_fkey;

ALTER TABLE public.credit_transactions
  DROP CONSTRAINT IF EXISTS credit_transactions_user_id_fkey;

ALTER TABLE public.credit_buckets
  ADD CONSTRAINT credit_buckets_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;

ALTER TABLE public.credit_transactions
  ADD CONSTRAINT credit_transactions_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;

DO $$
BEGIN
  RAISE NOTICE 'Revert 011 applied: B-11..B-15 fixes rolled back.';
END $$;
