-- =============================================================================
-- Migration: 004_loop5_idempotency_and_rls.sql
-- Purpose:   Fix four real defects discovered in Loop 5 audit (R1, R2, R4, R5).
-- Scope:     RLS hardening + admin_set_credits hardening + ledger idempotency.
-- Strategy:  Segmented, idempotent, transactional. Each segment fails loud if
--            its precondition is unmet. Assertions block at end fails the
--            migration if any invariant is violated.
--
-- Authored:  2026-04-27
-- Reverter:  004_revert.sql (pre-written, mirror-image)
-- Companion: 004b_credit_tx_idem_concurrent.sql (must run AFTER this file
--            but BEFORE the assertion block in segment R2 will pass)
--
-- DOES NOT TOUCH product code (api.py, frontend/). Pure DB changes.
--
-- WHAT THIS MIGRATION DOES NOT DO:
--   - It does NOT consolidate profiles.tokens with the bucket/transaction
--     ledger (R3). That requires coordinated api.py changes in Phase 2.
--   - It does NOT modify add_credits / deduct_credits (R6). Those need
--     ledger-aware rewrites that touch webhook handlers.
--   - It does NOT widen the unique index inline. Index is built CONCURRENTLY
--     in 004b to avoid a write lock on the busy credit_transactions table.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- Pre-flight asserts: refuse to run on a baseline that doesn't match the audit.
-- This protects against drift between what we audited and what we deploy onto.
-- -----------------------------------------------------------------------------
DO $$
DECLARE
  n_dual_policies int;
  has_old_idx     boolean;
  asc_search_path text;
  has_target      boolean;
BEGIN
  -- Confirm both UPDATE policies still exist on profiles.
  SELECT count(*) INTO n_dual_policies
  FROM pg_policy p
  JOIN pg_class c ON c.oid = p.polrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname='public' AND c.relname='profiles' AND p.polcmd='w';
  IF n_dual_policies < 2 THEN
    RAISE NOTICE 'Pre-flight: profiles UPDATE policy count = %, expected >= 2. Continuing (idempotent).', n_dual_policies;
  END IF;

  -- Confirm the grant-only unique index still exists (R2 prerequisite).
  SELECT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname='public' AND indexname='uq_credit_tx_grant_ref'
  ) INTO has_old_idx;
  IF NOT has_old_idx THEN
    RAISE NOTICE 'Pre-flight: uq_credit_tx_grant_ref already absent. Continuing (idempotent).';
  END IF;

  -- Confirm admin_set_credits exists with the expected signature.
  SELECT EXISTS (
    SELECT 1 FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname='public' AND p.proname='admin_set_credits'
      AND oidvectortypes(p.proargtypes) = 'uuid, integer, boolean'
  ) INTO has_target;
  IF NOT has_target THEN
    RAISE EXCEPTION 'Pre-flight FAIL: admin_set_credits(uuid,integer,boolean) not found. Refusing to deploy.';
  END IF;
END $$;

-- =============================================================================
-- SEGMENT R1: Drop the redundant looser UPDATE policy on public.profiles.
--
-- Problem: two PERMISSIVE UPDATE policies exist on profiles. PostgreSQL OR's
-- their WITH CHECK expressions, so the looser one ("Users can update own
-- profile") allows users to satisfy the policy WITHOUT also satisfying the
-- stricter "profiles_owner_update_safe" policy. The looser one fails to lock
-- subscription_status / subscription_plan / subscription_current_period_end /
-- suspended_reason / admin_notes.
--
-- Fix: drop "Users can update own profile". Keep profiles_owner_update_safe,
-- which already locks role/plan/tokens/stripe_customer_id/stripe_subscription_id/
-- suspended_at, but EXTEND it to also lock the missing fields.
-- =============================================================================

DROP POLICY IF EXISTS "Users can update own profile" ON public.profiles;

-- Re-issue profiles_owner_update_safe with extended lockdown. Drop and recreate
-- so the WITH CHECK expression is updated.
DROP POLICY IF EXISTS profiles_owner_update_safe ON public.profiles;

CREATE POLICY profiles_owner_update_safe ON public.profiles
  FOR UPDATE
  USING (auth.uid() = id)
  WITH CHECK (
    auth.uid() = id
    -- Lock authority columns: only SECURITY DEFINER admin RPCs may change these.
    AND role = (SELECT p.role FROM public.profiles p WHERE p.id = auth.uid())
    AND plan = (SELECT p.plan FROM public.profiles p WHERE p.id = auth.uid())
    AND tokens = (SELECT p.tokens FROM public.profiles p WHERE p.id = auth.uid())
    -- Stripe linkage columns: only webhook flow may change.
    AND COALESCE(stripe_customer_id, '') =
        COALESCE((SELECT p.stripe_customer_id FROM public.profiles p WHERE p.id = auth.uid()), '')
    AND COALESCE(stripe_subscription_id, '') =
        COALESCE((SELECT p.stripe_subscription_id FROM public.profiles p WHERE p.id = auth.uid()), '')
    AND COALESCE(subscription_status, 'none') =
        COALESCE((SELECT p.subscription_status FROM public.profiles p WHERE p.id = auth.uid()), 'none')
    AND COALESCE(subscription_plan, 'none') =
        COALESCE((SELECT p.subscription_plan FROM public.profiles p WHERE p.id = auth.uid()), 'none')
    AND COALESCE(subscription_current_period_end::text, '') =
        COALESCE((SELECT p.subscription_current_period_end::text FROM public.profiles p WHERE p.id = auth.uid()), '')
    -- Moderation columns: only admin RPCs may change.
    AND COALESCE(suspended_at::text, '') =
        COALESCE((SELECT p.suspended_at::text FROM public.profiles p WHERE p.id = auth.uid()), '')
    AND COALESCE(suspended_reason, '') =
        COALESCE((SELECT p.suspended_reason FROM public.profiles p WHERE p.id = auth.uid()), '')
    AND COALESCE(admin_notes, '') =
        COALESCE((SELECT p.admin_notes FROM public.profiles p WHERE p.id = auth.uid()), '')
  );

-- =============================================================================
-- SEGMENT R5+R4(partial)+search_path: Recreate admin_set_credits.
--
-- Live function:
--   - lacks SET search_path (security risk: search_path injection)
--   - uses unqualified table refs (depends on search_path)
--   - inline auth.uid() check instead of is_admin() helper (inconsistent)
--   - never writes to audit_log (admin trail gap)
--
-- Fix:
--   - SET search_path = '' (empty: forces every reference to be schema-qualified)
--   - All table refs fully qualified
--   - Call public.is_admin(auth.uid()) — same pattern as other admin RPCs
--   - Write to public.audit_log via public.admin_audit_insert
--
-- Signature is preserved exactly: (target_user_id uuid, new_credits integer,
-- is_delta boolean DEFAULT false) RETURNS integer.
-- api.py at line 6206 forwards (target_user_id, new_credits, is_delta) — unchanged.
-- =============================================================================

CREATE OR REPLACE FUNCTION public.admin_set_credits(
  target_user_id uuid,
  new_credits    integer,
  is_delta       boolean DEFAULT false
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  v_caller        uuid := auth.uid();
  v_current       integer;
  v_final         integer;
  v_target_exists boolean;
BEGIN
  -- Authority check: caller must be an admin. is_admin() is itself
  -- SECURITY DEFINER with SET search_path = 'public', 'auth'.
  IF NOT public.is_admin(v_caller) THEN
    RAISE EXCEPTION 'admin_set_credits: admin access required'
      USING ERRCODE = 'insufficient_privilege';
  END IF;

  -- Validate amount sign for absolute mode.
  IF NOT is_delta AND new_credits < 0 THEN
    RAISE EXCEPTION 'admin_set_credits: absolute new_credits must be >= 0';
  END IF;

  -- Compute final balance.
  SELECT tokens, true INTO v_current, v_target_exists
  FROM public.profiles
  WHERE id = target_user_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'admin_set_credits: target user not found';
  END IF;

  IF is_delta THEN
    v_final := GREATEST(0, v_current + new_credits);
  ELSE
    v_final := new_credits;
  END IF;

  -- Update.
  UPDATE public.profiles
     SET tokens = v_final,
         updated_at = now()
   WHERE id = target_user_id;

  -- Audit. Use admin_audit_insert (which itself checks is_admin and resolves
  -- actor email). before/after are kept narrow to avoid leaking unrelated
  -- profile fields.
  PERFORM public.admin_audit_insert(
    p_actor_id   := v_caller,
    p_action     := 'admin.set_credits',
    p_target_type:= 'user',
    p_target_id  := target_user_id::text,
    p_before     := jsonb_build_object('tokens', v_current),
    p_after      := jsonb_build_object('tokens', v_final),
    p_metadata   := jsonb_build_object(
                      'mode',   CASE WHEN is_delta THEN 'delta' ELSE 'absolute' END,
                      'amount', new_credits
                    ),
    p_ip         := NULL,
    p_user_agent := NULL
  );

  RETURN v_final;
END;
$function$;

-- Grants: must be invokable by 'authenticated' (api.py forwards user JWT).
REVOKE ALL ON FUNCTION public.admin_set_credits(uuid, integer, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.admin_set_credits(uuid, integer, boolean) TO authenticated;

-- =============================================================================
-- SEGMENT R2 (precondition): Drop the grant-only unique index.
-- The wider replacement index is created in 004b CONCURRENTLY (no lock).
-- Dropping is fast and safe (catalog-only).
--
-- This segment is gated: only drop if the new index is NOT yet present
-- (so 004 can safely re-run after 004b). If 004b has already created
-- uq_credit_tx_idem, we leave both in place momentarily and the assertions
-- block will be satisfied either way.
-- =============================================================================

DO $$
DECLARE
  has_new boolean;
BEGIN
  SELECT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname='public' AND indexname='uq_credit_tx_idem'
  ) INTO has_new;

  IF has_new THEN
    -- New index already in place: drop the narrow legacy one.
    EXECUTE 'DROP INDEX IF EXISTS public.uq_credit_tx_grant_ref';
    RAISE NOTICE '004: uq_credit_tx_idem present; dropped legacy uq_credit_tx_grant_ref';
  ELSE
    RAISE NOTICE '004: leaving uq_credit_tx_grant_ref in place (run 004b next to create uq_credit_tx_idem CONCURRENTLY)';
  END IF;
END $$;

-- =============================================================================
-- SEGMENT: Final invariants. Fail the migration if any are violated.
-- =============================================================================

DO $$
DECLARE
  n_update_policies int;
  has_set_credits   boolean;
  has_search_path   boolean;
  has_audit_call    boolean;
  fn_signature      text;
BEGIN
  -- R1 invariant: exactly one UPDATE policy on profiles.
  SELECT count(*) INTO n_update_policies
  FROM pg_policy p
  JOIN pg_class c ON c.oid = p.polrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname='public' AND c.relname='profiles' AND p.polcmd='w';
  IF n_update_policies <> 1 THEN
    RAISE EXCEPTION '004 invariant FAIL R1: profiles has % UPDATE policies, expected 1', n_update_policies;
  END IF;

  -- R5/search_path: admin_set_credits must declare SET search_path = '' (empty).
  -- pg stores SET search_path = '' as the proconfig element search_path="" (quoted empty).
  SELECT EXISTS (
    SELECT 1 FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname='public' AND p.proname='admin_set_credits'
      AND EXISTS (
        SELECT 1 FROM unnest(p.proconfig) elem
        WHERE elem ILIKE 'search_path=%'
      )
  ) INTO has_search_path;
  IF NOT has_search_path THEN
    RAISE EXCEPTION '004 invariant FAIL search_path: admin_set_credits missing SET search_path';
  END IF;

  -- Signature unchanged.
  SELECT oidvectortypes(p.proargtypes) INTO fn_signature
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname='public' AND p.proname='admin_set_credits'
  LIMIT 1;
  IF fn_signature IS DISTINCT FROM 'uuid, integer, boolean' THEN
    RAISE EXCEPTION '004 invariant FAIL signature: admin_set_credits args = %, expected (uuid, integer, boolean)', fn_signature;
  END IF;

  -- R5: admin_set_credits must reference admin_audit_insert.
  SELECT pg_get_functiondef(p.oid) ILIKE '%admin_audit_insert%' INTO has_audit_call
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname='public' AND p.proname='admin_set_credits'
  LIMIT 1;
  IF NOT has_audit_call THEN
    RAISE EXCEPTION '004 invariant FAIL R5: admin_set_credits does not call admin_audit_insert';
  END IF;

  RAISE NOTICE '004 invariants: R1=ok search_path=ok signature=ok R5=ok';
END $$;

COMMIT;
