-- ============================================================
-- Migration 011: P2 DB Cluster B-11..B-15
-- Loop 6 bug registry fixes
-- ============================================================
-- B-11: admin_count_profiles / admin_list_profiles — rewrite to use
--       public.is_admin(auth.uid()) instead of inline subquery.
-- B-12: admin_audit_insert — revoke EXECUTE from anon + authenticated;
--       grant to service_role only (body already uses is_admin check).
-- B-13: expire_credits — revoke EXECUTE from anon + authenticated (already
--       revoked on live; idempotent REVOKE for local baseline correctness).
-- B-14: handle_new_user — wrap in EXCEPTION handler, add ON CONFLICT DO
--       NOTHING on profiles INSERT, add credit_buckets INSERT with
--       sub-transaction, SECURITY DEFINER + SET search_path.
-- B-15: credit_buckets / credit_transactions — drop FKs to auth.users,
--       add FKs to public.profiles(id) ON DELETE CASCADE. Orphan check
--       aborts migration if any orphaned rows are found.
-- ============================================================

-- ============================================================
-- B-15 PRE-CHECK: abort if any orphaned rows would be left behind
-- ============================================================
DO $$
DECLARE
  orphan_buckets      integer;
  orphan_transactions integer;
BEGIN
  -- Count credit_buckets rows whose user_id has no matching profiles row
  SELECT COUNT(*) INTO orphan_buckets
    FROM public.credit_buckets cb
   WHERE NOT EXISTS (SELECT 1 FROM public.profiles p WHERE p.id = cb.user_id);

  -- Count credit_transactions rows whose user_id has no matching profiles row
  SELECT COUNT(*) INTO orphan_transactions
    FROM public.credit_transactions ct
   WHERE NOT EXISTS (SELECT 1 FROM public.profiles p WHERE p.id = ct.user_id);

  IF orphan_buckets > 0 OR orphan_transactions > 0 THEN
    RAISE EXCEPTION
      'B-15 orphan check FAILED: credit_buckets has % orphan row(s), '
      'credit_transactions has % orphan row(s). '
      'Manual cleanup required before this migration can complete.',
      orphan_buckets, orphan_transactions;
  END IF;

  RAISE NOTICE 'B-15 orphan check passed (0 orphans in credit_buckets and credit_transactions).';
END $$;

-- ============================================================
-- B-11: Rewrite admin_count_profiles to use public.is_admin(auth.uid())
-- ============================================================
CREATE OR REPLACE FUNCTION public.admin_count_profiles()
  RETURNS integer
  LANGUAGE sql
  STABLE
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
  SELECT
    CASE WHEN public.is_admin(auth.uid())
    THEN (SELECT COUNT(*)::integer FROM public.profiles)
    ELSE (SELECT CAST(NULL AS integer) FROM (SELECT public.is_admin(auth.uid())) t WHERE false
          UNION ALL SELECT NULL LIMIT 0)
    END;
$$;

-- Simpler rewrite: raise exception if not admin (matches existing pattern)
CREATE OR REPLACE FUNCTION public.admin_count_profiles()
  RETURNS integer
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
BEGIN
  IF NOT public.is_admin(auth.uid()) THEN
    RAISE EXCEPTION 'permission denied' USING ERRCODE = '42501';
  END IF;
  RETURN (SELECT COUNT(*)::integer FROM public.profiles);
END;
$$;

-- ============================================================
-- B-11: Rewrite admin_list_profiles to use public.is_admin(auth.uid())
-- ============================================================
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
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
BEGIN
  IF NOT public.is_admin(auth.uid()) THEN
    RAISE EXCEPTION 'permission denied' USING ERRCODE = '42501';
  END IF;
  RETURN QUERY
    SELECT p.id, p.email, p.full_name, p.tokens, p.role,
           p.stripe_customer_id, p.subscription_plan, p.subscription_status, p.created_at
      FROM public.profiles p
     ORDER BY p.created_at DESC;
END;
$$;

-- ============================================================
-- B-12: admin_audit_insert — revoke from anon + authenticated
-- ============================================================
-- The function body already uses public.is_admin(p_actor_id) check.
-- Additionally ensure auth.uid() = p_actor_id to prevent replay by a
-- service that passes a different actor_id.
-- NOTE: keep search_path as public,pg_temp (drop the 'auth' from proconfig)
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
  SET search_path = public, pg_temp
AS $$
DECLARE
  v_id    uuid;
  v_email text;
BEGIN
  -- Require caller to be an admin
  IF NOT public.is_admin(auth.uid()) THEN
    RAISE EXCEPTION 'permission denied' USING ERRCODE = '42501';
  END IF;
  -- Prevent replay: actor_id must match the calling user
  IF auth.uid() IS NOT NULL AND auth.uid() <> p_actor_id THEN
    RAISE EXCEPTION 'permission denied: actor_id mismatch' USING ERRCODE = '42501';
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

-- Revoke from anon and authenticated; service_role only
REVOKE EXECUTE ON FUNCTION public.admin_audit_insert(uuid, text, text, text, jsonb, jsonb, jsonb, text, text)
  FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_audit_insert(uuid, text, text, text, jsonb, jsonb, jsonb, text, text)
  TO service_role;

-- ============================================================
-- B-13: expire_credits — revoke from anon + authenticated (idempotent)
-- ============================================================
REVOKE EXECUTE ON FUNCTION public.expire_credits()
  FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.expire_credits()
  TO service_role;

-- ============================================================
-- B-14: handle_new_user — exception handler, ON CONFLICT, credit_buckets,
--        SECURITY DEFINER + SET search_path
-- ============================================================
CREATE OR REPLACE FUNCTION public.handle_new_user()
  RETURNS trigger
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
/*
  B-14 fix — atomic signup trigger.

  Design decisions:
  1. Profiles INSERT uses ON CONFLICT (id) DO NOTHING — trigger is idempotent
     (safe to replay if Supabase retries).
  2. Credit bucket INSERT is wrapped in a nested BEGIN/EXCEPTION block
     (savepoint semantics via PL/pgSQL). If the bucket grant fails, we log
     a NOTICE and RE-RAISE so the whole transaction rolls back — atomicity
     is preserved and the auth.users INSERT also rolls back.
  3. Any unexpected exception from the profiles INSERT is re-raised so that
     Postgres rolls back the auth.users row (AFTER trigger, same transaction).
*/
BEGIN
  BEGIN
    -- Insert profile (idempotent)
    INSERT INTO public.profiles (id, email, full_name)
    VALUES (NEW.id, NEW.email, NEW.raw_user_meta_data->>'full_name')
    ON CONFLICT (id) DO NOTHING;

    -- Insert initial credit bucket (nested sub-transaction)
    BEGIN
      INSERT INTO public.credit_buckets (user_id, source, original_credits, remaining_credits)
      VALUES (NEW.id, 'signup_grant', 500, 500);
    EXCEPTION WHEN OTHERS THEN
      -- Credit bucket failure is logged and re-raised to roll back everything.
      -- Rationale: atomicity — a user without a credit bucket is in an
      -- inconsistent state. Rolling back auth.users is safer than a phantom.
      RAISE NOTICE 'handle_new_user: credit_buckets INSERT failed for user %: % (SQLSTATE: %)',
        NEW.id, SQLERRM, SQLSTATE;
      RAISE;
    END;

  EXCEPTION WHEN OTHERS THEN
    -- Re-raise so Postgres rolls back the auth.users INSERT too.
    RAISE NOTICE 'handle_new_user: unexpected error for user %: % (SQLSTATE: %)',
      NEW.id, SQLERRM, SQLSTATE;
    RAISE;
  END;

  RETURN NEW;
END;
$$;

-- ============================================================
-- B-15: credit_buckets / credit_transactions — fix FK to public.profiles
-- ============================================================

-- Drop existing FKs referencing auth.users (if they exist)
ALTER TABLE public.credit_buckets
  DROP CONSTRAINT IF EXISTS credit_buckets_user_id_fkey;

ALTER TABLE public.credit_transactions
  DROP CONSTRAINT IF EXISTS credit_transactions_user_id_fkey;

-- Add new FKs referencing public.profiles(id) ON DELETE CASCADE
ALTER TABLE public.credit_buckets
  ADD CONSTRAINT credit_buckets_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

ALTER TABLE public.credit_transactions
  ADD CONSTRAINT credit_transactions_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;

-- ============================================================
-- Summary comment
-- ============================================================
DO $$
BEGIN
  RAISE NOTICE 'Migration 011 applied: B-11 admin helpers use is_admin(), '
    'B-12 admin_audit_insert service_role only, '
    'B-13 expire_credits service_role only (idempotent), '
    'B-14 handle_new_user atomic with exception handler, '
    'B-15 credit FK tables now reference public.profiles.';
END $$;
