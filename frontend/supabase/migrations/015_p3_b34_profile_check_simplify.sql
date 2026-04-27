-- ============================================================
-- Migration 015: B-34 — Simplify profiles_owner_update_safe WITH CHECK
-- Loop 6 / P3 DB cluster
-- ============================================================
-- Problem: profiles_owner_update_safe WITH CHECK contains 9 correlated
-- subqueries on public.profiles, each independently fetching the same row.
-- This causes 9 separate index scans per UPDATE, plus InitPlan overhead.
--
-- Fix: introduce a SECURITY DEFINER helper function
-- public.check_profile_immutable(uuid, ...) that accepts the NEW row
-- values as arguments and performs a single index scan to fetch all
-- locked columns from the current DB state, then returns true iff no
-- admin-locked column was changed.
--
-- The policy WITH CHECK becomes a single function call:
--   check_profile_immutable(id, role, plan, tokens, ...)
-- where the bare column names resolve to the NEW row values (standard
-- PostgreSQL policy WITH CHECK scoping). The function does one lookup.
--
-- Note: migration 014 (B-33) already updated the USING clause to use
-- (SELECT auth.uid()). This migration replaces only the WITH CHECK.
--
-- Ref: A1-14, loop6_audit/A1_db.md
-- ============================================================

-- ============================================================
-- Helper function: single-lookup immutability checker
-- ============================================================
-- B-34: replace 9 correlated subqueries with one index scan
CREATE OR REPLACE FUNCTION public.check_profile_immutable(
  p_id                              uuid,
  p_role                            text,
  p_plan                            text,
  p_tokens                          integer,
  p_stripe_customer_id              text,
  p_stripe_subscription_id          text,
  p_subscription_status             text,
  p_subscription_plan               text,
  p_subscription_current_period_end timestamptz,
  p_suspended_at                    timestamptz,
  p_suspended_reason                text,
  p_admin_notes                     text
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
  -- Returns true iff all admin-locked fields in the current DB row match
  -- the supplied (NEW) values. If the row doesn't exist, returns NULL
  -- (treated as false by WITH CHECK).
  SELECT
    COALESCE(p.role = p_role, true)
    AND COALESCE(p.plan = p_plan, true)
    AND COALESCE(p.tokens = p_tokens, true)
    AND COALESCE(p.stripe_customer_id, '') = COALESCE(p_stripe_customer_id, '')
    AND COALESCE(p.stripe_subscription_id, '') = COALESCE(p_stripe_subscription_id, '')
    AND COALESCE(p.subscription_status, 'none') = COALESCE(p_subscription_status, 'none')
    AND COALESCE(p.subscription_plan, 'none') = COALESCE(p_subscription_plan, 'none')
    AND COALESCE(p.subscription_current_period_end::text, '') = COALESCE(p_subscription_current_period_end::text, '')
    AND COALESCE(p.suspended_at::text, '') = COALESCE(p_suspended_at::text, '')
    AND COALESCE(p.suspended_reason, '') = COALESCE(p_suspended_reason, '')
    AND COALESCE(p.admin_notes, '') = COALESCE(p_admin_notes, '')
  FROM public.profiles p
  WHERE p.id = p_id;
$$;

-- ============================================================
-- Recreate profiles_owner_update_safe with simplified WITH CHECK
-- ============================================================
DROP POLICY IF EXISTS profiles_owner_update_safe ON public.profiles;

CREATE POLICY profiles_owner_update_safe ON public.profiles
  FOR UPDATE
  USING ((SELECT auth.uid()) = id)
  WITH CHECK (
    -- Ownership guard: can only update own row
    (SELECT auth.uid()) = id
    -- Single-scan immutability guard: one function call = one index scan
    AND public.check_profile_immutable(
      id,
      role,
      plan,
      tokens,
      stripe_customer_id,
      stripe_subscription_id,
      subscription_status,
      subscription_plan,
      subscription_current_period_end,
      suspended_at,
      suspended_reason,
      admin_notes
    )
  );

-- Grant EXECUTE to authenticated and service_role so the policy function
-- is callable under those security contexts.
GRANT EXECUTE ON FUNCTION public.check_profile_immutable(
  uuid, text, text, integer, text, text, text, text, timestamptz, timestamptz, text, text
) TO authenticated, service_role;
